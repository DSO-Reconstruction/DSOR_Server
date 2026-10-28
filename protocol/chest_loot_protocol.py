"""Resolve inventory chest contents from ``chest_contents.json``.

The static client database names most server-side loot lists but does not ship
their contents.  ``chest_contents.json`` combines the explicit database
edges with contents recovered from the client tooltip text.  This module is a
strict runtime adapter for that report:

* explicit item-table rewards are preferred;
* tooltip candidates must resolve to a real item template;
* a complete consecutive 100% chance block is one weighted choice;
* remaining percentage entries are independent bonus rolls; and
* nested containers are awarded as items rather than opened recursively.

Unresolved or genuinely ambiguous mandatory entries reject the open before
the caller consumes the chest.  This prevents a partial/guessed reward from
silently destroying an item.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import random
import re
import sqlite3
from typing import Any

from dso_server.runtime import resident_content
from dso_server.paths import DEFAULT_CHEST_CONTENTS_MAP

from dso_server.protocol.commerce_protocol import (
    OFFER_RARITY_IDS,
    PurchaseItemDefinition,
    load_purchase_item_definition,
)
from dso_server.protocol.currency_protocol import GameCurrencyChanged
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.inventory_economy_protocol import (
    InventoryMutationBuilder,
    credit_currency_template,
    refresh_inventory_economy_values,
)
from dso_server.protocol.player_init_protocol import CurrencyChanged
from dso_server.protocol.world_loot_protocol import (
    EQUIPMENT_RARITIES,
    roll_equipment_reward,
    roll_new_equipment_enchantments,
)



_INVENTORY_CONTAINER_CATEGORIES = {
    "LootGenItem",
    "LootGenItemAutoIdent",
    "Selective",
    "LotteryTicket",
    "Consumable",
    "HatchEgg",
}
_RARITY_HINT = re.compile(
    r"\((common|uncommon|improved|magic|magical|rare|extraordinary|epic|legendary|unique|set|mythic)\)\s*$",
    re.IGNORECASE,
)
_LEVEL_SUFFIX = re.compile(r"(?:^|_)Lv(\d+)$", re.IGNORECASE)
_ADVENTURER_CHEST_LEVEL = re.compile(r"^levelchest_lvl_?(\d+)$", re.IGNORECASE)
_RARITY_NAMES = {
    "common": "Common",
    "uncommon": "Uncommon",
    "improved": "Uncommon",
    "magic": "Magic",
    "magical": "Magic",
    "rare": "Rare",
    "extraordinary": "Rare",
    "epic": "Epic",
    "legendary": "Epic",
    "unique": "Unique",
    "set": "Set",
    "mythic": "Mythic",
}
_CANONICAL_CURRENCY_IDS = (
    "money_rc",
    "money_vc_gold",
    "money_vc",
    "money_gc_no_auto_use",
    "money_uc_no_auto_use",
    "money_hc_no_auto_use",
    "e_chr_2012_event_currency",
)
_RARITY_VARIANT_NUMBERS = {
    "Common": "01",
    "Uncommon": "02",
    "Magic": "03",
    "Rare": "04",
    "Epic": "05",
}
_STANDARD_GEM_FAMILIES = (
    "amethyst",
    "cyanit",
    "diamond",
    "onyx",
    "ruby",
)
_TOOLTIP_ITEM_ALIASES = {
    # Marketing names are not the internal item Name values, but the
    # stickerbook database identifies the exact live pet resources.
    "gloomy harpy pet (magic, increased armor value)": (
        "pet_harpy_black_10_percent_armor"
    ),
    "bug-collecting jabbax pet (epic)": "pet_frogman_epic",
    "radiant cyanit": "gem_cyanit_f",
}


@dataclass(frozen=True)
class ChestReward:
    definition: PurchaseItemDefinition
    amount: int
    item_level: int | None = None
    rarity: int | None = None
    appearance: str = ""


@dataclass(frozen=True)
class ChestContents:
    box_template_id: str
    source: str
    rewards: tuple[ChestReward, ...]
    used_tooltip_mapping: bool = False


@dataclass(frozen=True)
class ChestOpenMutation:
    contents: ChestContents
    inventory: InventoryInfo
    currency: CurrencyChanged
    game_currencies: dict[str, int]
    game_currency_updates: tuple[GameCurrencyChanged, ...]
    currency_changed: bool
    delta: InventoryInfo
    final: InventoryInfo
    next_item_id: int


@dataclass(frozen=True)
class _MappedReward:
    template_id: str
    amount: int
    chance_percent: float | None = None
    equipment_rarity: str | None = None
    equipment_slot_types: tuple[str, ...] = ()
    # An event skin region for the rolled item (christmas presents: e_chr).
    equipment_appearance_region: str = ""


def _positive_amount(value: Any, *, default: int = 1) -> int:
    if value is None or value == "":
        return default
    text = str(value).strip()
    if not text.isdigit():
        raise ValueError(f"reward amount {value!r} is not a positive integer")
    amount = int(text, 10)
    if amount <= 0:
        raise ValueError(f"reward amount must be positive, got {value!r}")
    return amount


def _split_embedded_amount(
    template_id: str, amount: Any,
) -> tuple[str, int]:
    """Handle report edges such as ``item_template,5`` from wishing wells."""

    template_id = str(template_id or "").strip()
    if amount is None and "," in template_id:
        possible_id, possible_amount = template_id.rsplit(",", 1)
        if possible_amount.strip().isdigit():
            template_id = possible_id.strip()
            amount = possible_amount.strip()
    if not template_id:
        raise ValueError("reward has no item template id")
    return template_id, _positive_amount(amount)


@lru_cache(maxsize=8)
def _load_index_cached(
    path_string: str, modified_ns: int, size: int,
) -> dict[str, tuple[dict[str, Any], ...]]:
    del modified_ns, size
    path = Path(path_string)
    payload = json.loads(resident_content.read_text(path, encoding="utf-8"))
    containers = payload.get("containers")
    if not isinstance(containers, list):
        raise ValueError("chest contents map has no containers array")
    indexed: dict[str, list[dict[str, Any]]] = {}
    for raw in containers:
        if not isinstance(raw, dict):
            continue
        container_id = str(raw.get("container") or "").strip()
        if container_id:
            indexed.setdefault(container_id, []).append(raw)
    return {key: tuple(values) for key, values in indexed.items()}


def _load_index(path: str | Path) -> dict[str, tuple[dict[str, Any], ...]]:
    resolved = resident_content.resolved(path)
    if not resident_content.is_file(resolved):
        raise FileNotFoundError(f"chest contents map not found: {resolved}")
    stat = resident_content.stat(resolved)
    return _load_index_cached(
        str(resolved), int(stat.st_mtime_ns), int(stat.st_size)
    )


def _inventory_record(
    records: tuple[dict[str, Any], ...], box_template_id: str,
) -> dict[str, Any]:
    """Choose the item record, not a same-named offer/stickerbook alias."""

    preferred_sources = (
        "_Template_Item.Loot",
        "_Template_HatchEggs.EggGetAwardItem",
        "none",
    )
    for source in preferred_sources:
        for record in reversed(records):
            if str(record.get("source") or "") == source:
                return record
    for record in reversed(records):
        if str(record.get("category") or "") in _INVENTORY_CONTAINER_CATEGORIES:
            return record
    raise ValueError(
        f"mapping {box_template_id!r} is not an inventory container"
    )


def _definition(
    static_database_path: str | Path, template_id: str,
) -> PurchaseItemDefinition:
    try:
        return load_purchase_item_definition(static_database_path, template_id)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"mapped reward {template_id!r} is not a valid item template"
        ) from exc


@lru_cache(maxsize=4096)
def _definitions_for_label(
    static_database_path: str,
    label: str,
    rarity_name: str,
) -> tuple[PurchaseItemDefinition, ...]:
    """Resolve tooltip-only rewards by their authored item display name."""

    path = Path(static_database_path)
    database = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            "SELECT Id, DefaultRarity FROM _Template_Item "
            "WHERE lower(Name)=lower(?) AND Obsolete=0",
            (str(label),),
        ).fetchall()
    finally:
        database.close()
    if rarity_name:
        narrowed = tuple(
            row for row in rows
            if str(row["DefaultRarity"] or "Common") == rarity_name
        )
        if narrowed:
            rows = narrowed
    return tuple(
        _definition(path, str(row["Id"])) for row in rows
    )


def _candidate_definition(
    static_database_path: str | Path,
    entry: dict[str, Any],
    *,
    player_level: int,
    rarity_hint: str = "",
    class_name: str = "",
) -> PurchaseItemDefinition:
    candidates = tuple(dict.fromkeys(
        str(value).strip()
        for value in (entry.get("candidates") or ())
        if str(value).strip()
    ))
    text = str(entry.get("text") or "")
    rarity_match = _RARITY_HINT.search(text)
    rarity_name = (
        _RARITY_NAMES[rarity_match.group(1).lower()]
        if rarity_match is not None
        else str(rarity_hint or "")
    )
    definitions = tuple(
        _definition(static_database_path, candidate)
        for candidate in candidates
    )
    normalized_class = str(class_name or "").casefold()
    if normalized_class in {"warrior", "mage", "ranger", "dwarf"}:
        class_specific = tuple(
            definition
            for definition in definitions
            if definition.template_id.casefold().startswith(
                normalized_class + "_"
            )
        )
        if class_specific:
            definitions = class_specific
    if not definitions:
        aliased = _TOOLTIP_ITEM_ALIASES.get(text.casefold().strip())
        if aliased:
            definitions = (_definition(static_database_path, aliased),)
    if not definitions:
        label = _RARITY_HINT.sub("", text).strip()
        definitions = _definitions_for_label(
            str(Path(static_database_path)), label, rarity_name
        )
    if not definitions:
        raise ValueError(
            f"tooltip reward {entry.get('text')!r} has no item candidate"
        )
    if len(definitions) == 1:
        return definitions[0]

    if rarity_name:
        rarity_id = OFFER_RARITY_IDS[rarity_name]
        narrowed = tuple(
            definition
            for definition in definitions
            if int(definition.rarity) == int(rarity_id)
        )
        if len(narrowed) == 1:
            return narrowed[0]
        numbered_suffix = "_rarity_" + _RARITY_VARIANT_NUMBERS.get(
            rarity_name, ""
        )
        narrowed = tuple(
            definition
            for definition in definitions
            if numbered_suffix != "_rarity_"
            and definition.template_id.lower().endswith(numbered_suffix)
        )
        if len(narrowed) == 1:
            return narrowed[0]
        suffix = "_" + rarity_name.lower()
        narrowed = tuple(
            definition
            for definition in definitions
            if definition.template_id.lower().endswith(suffix)
        )
        if len(narrowed) == 1:
            return narrowed[0]
        standard = tuple(
            definition
            for definition in narrowed
            if not definition.template_id.casefold().startswith("e_")
        )
        if len(standard) == 1:
            return standard[0]

    for canonical in _CANONICAL_CURRENCY_IDS:
        for definition in definitions:
            if definition.template_id == canonical:
                return definition

    level_variants = []
    for definition in definitions:
        match = _LEVEL_SUFFIX.search(definition.template_id)
        if match is not None:
            level_variants.append((int(match.group(1)), definition))
    if len(level_variants) == len(definitions):
        eligible = [pair for pair in level_variants if pair[0] <= player_level]
        return max(eligible or level_variants, key=lambda pair: pair[0])[1]

    # A number of consumables use rarity suffixes but the tooltip deliberately
    # omits a rarity.  The unsuffixed/common/normal variant is the authored
    # base reward and is preferable to randomly changing reward rank.
    base_variants = tuple(
        definition
        for definition in definitions
        if definition.template_id.lower().endswith(("_normal", "_common"))
    )
    if len(base_variants) == 1:
        return base_variants[0]

    numbered_variants: list[tuple[int, PurchaseItemDefinition]] = []
    for definition in definitions:
        match = re.search(
            r"_rarity_(\d+)$", definition.template_id, re.IGNORECASE
        )
        if match is not None:
            numbered_variants.append((int(match.group(1)), definition))
    if len(numbered_variants) == len(definitions):
        # An uncoloured tooltip means the base/common consumable. Coloured
        # tooltip markup is handled above. This avoids arbitrarily awarding a
        # more valuable rank just because every database variant shares a
        # display name.
        return min(numbered_variants, key=lambda pair: pair[0])[1]

    non_quest = tuple(
        definition
        for definition in definitions
        if not definition.template_id.casefold().startswith("quest_")
        and definition.item_category != "QuestItem"
    )
    if len(non_quest) == 1:
        return non_quest[0]

    conventional_gems = tuple(
        definition
        for definition in definitions
        if re.fullmatch(
            r"gem_[a-z]+_[a-z]",
            definition.template_id,
            re.IGNORECASE,
        )
    )
    if len(conventional_gems) == 1:
        return conventional_gems[0]

    # Generated tooltip mappings frequently list an unsuffixed authored item
    # together with its upgrade variants. The unsuffixed record is the exact
    # base reward named by the tooltip.
    suffixed_rarities = tuple(
        "_" + name.casefold() for name in EQUIPMENT_RARITIES
    )
    unsuffixed = tuple(
        definition for definition in definitions
        if not definition.template_id.casefold().endswith(suffixed_rarities)
    )
    if len(unsuffixed) == 1:
        return unsuffixed[0]

    raise ValueError(
        f"tooltip reward {text!r} is ambiguous: "
        f"{tuple(definition.template_id for definition in definitions)!r}"
    )


def _direct_entries(
    static_database_path: str | Path,
    record: dict[str, Any],
    *,
    class_name: str = "",
) -> tuple[list[_MappedReward], list[_MappedReward], list[_MappedReward], bool]:
    guaranteed: list[_MappedReward] = []
    choices: list[_MappedReward] = []
    chance: list[_MappedReward] = []
    saw_choose = False
    for entry in record.get("yields") or ():
        mode = str(entry.get("mode") or "guaranteed")
        if mode == "rarity_upgrade":
            continue
        if mode == "one_of_equipment":
            rarity = _RARITY_NAMES.get(
                str(entry.get("rarity") or "").casefold()
            )
            if rarity not in EQUIPMENT_RARITIES:
                raise ValueError(
                    f"invalid generated equipment rarity "
                    f"{entry.get('rarity')!r} for "
                    f"{record.get('container')!r}"
                )
            raw_slots = entry.get("slot_types") or ()
            if not isinstance(raw_slots, (list, tuple)):
                raise ValueError("generated equipment slot_types must be a list")
            choices.append(_MappedReward(
                template_id="@equipment",
                amount=1,
                equipment_rarity=rarity,
                equipment_slot_types=tuple(str(slot) for slot in raw_slots),
                equipment_appearance_region=str(
                    entry.get("appearance_region") or ""
                ),
            ))
            continue
        if mode == "class_set":
            normalized_class = str(
                entry.get("fixed_class") or class_name or ""
            ).casefold()
            class_items = (entry.get("by_class") or {}).get(normalized_class)
            if normalized_class not in {"warrior", "mage", "ranger", "dwarf"}:
                raise ValueError(
                    f"class-specific container "
                    f"{record.get('container')!r} requires a valid player class"
                )
            if not isinstance(class_items, list) or not class_items:
                raise ValueError(
                    f"class-specific container "
                    f"{record.get('container')!r} has no rewards for "
                    f"class {normalized_class!r}"
                )
            for class_item in class_items:
                if isinstance(class_item, dict):
                    class_template_id, class_amount = _split_embedded_amount(
                        class_item.get("item"), class_item.get("amount")
                    )
                else:
                    class_template_id, class_amount = _split_embedded_amount(
                        class_item, None
                    )
                if class_template_id == str(record.get("container") or ""):
                    raise ValueError(
                        f"container {class_template_id!r} maps to itself"
                    )
                _definition(static_database_path, class_template_id)
                guaranteed.append(_MappedReward(class_template_id, class_amount))
            continue
        if mode == "one_of_class":
            normalized_class = str(class_name or "").casefold()
            class_items = (entry.get("by_class") or {}).get(normalized_class)
            if normalized_class not in {"warrior", "mage", "ranger", "dwarf"}:
                raise ValueError(
                    f"class-specific container "
                    f"{record.get('container')!r} requires a valid player class"
                )
            if class_items is None:
                raise ValueError(
                    f"class-specific container "
                    f"{record.get('container')!r} has no reward for "
                    f"class {normalized_class!r}"
                )
            class_template_id, class_amount = _split_embedded_amount(
                class_items.get("item") if isinstance(class_items, dict)
                else class_items,
                class_items.get("amount") if isinstance(class_items, dict)
                else None,
            )
            if class_template_id == str(record.get("container") or ""):
                raise ValueError(
                    f"container {class_template_id!r} maps to itself"
                )
            _definition(static_database_path, class_template_id)
            choices.append(_MappedReward(class_template_id, class_amount))
            continue
        template_id, amount = _split_embedded_amount(
            entry.get("item"), entry.get("amount")
        )
        if template_id == str(record.get("container") or ""):
            continue
        _definition(static_database_path, template_id)
        chance_percent = entry.get("chance_percent")
        reward = _MappedReward(
            template_id,
            amount,
            float(chance_percent) if chance_percent is not None else None,
        )
        if mode == "guaranteed":
            guaranteed.append(reward)
        elif mode == "one_of":
            choices.append(reward)
        elif mode == "chance":
            if chance_percent is None or not 0.0 < reward.chance_percent <= 100.0:
                raise ValueError(
                    f"invalid reward chance {chance_percent!r} for "
                    f"{template_id!r}"
                )
            chance.append(reward)
        elif mode == "choose":
            saw_choose = True
        else:
            raise ValueError(f"unsupported direct reward mode {mode!r}")
    return guaranteed, choices, chance, saw_choose


def _tooltip_entries(
    static_database_path: str | Path,
    record: dict[str, Any],
    *,
    player_level: int,
    class_name: str,
) -> tuple[list[_MappedReward], list[_MappedReward], bool]:
    ui = record.get("ui") or {}
    guaranteed: list[_MappedReward] = []
    chance: list[_MappedReward] = []
    saw_choose = False
    prose = str(ui.get("prose") or "")
    for entry in ui.get("entries") or ():
        mode = str(entry.get("mode") or ui.get("mode") or "unknown")
        if mode == "choose":
            saw_choose = True
            continue
        if mode == "chance" and entry.get("chance_percent") is None:
            # The tooltip names possible jackpot items but not their odds.
            # The authoritative list remains server-side; never invent a
            # probability or reject the box while resolving guaranteed loot.
            continue
        rarity_hint = ""
        entry_text = str(entry.get("text") or "")
        entry_offset = prose.find(entry_text)
        if entry_offset >= 0:
            prefix = prose[max(0, entry_offset - 96):entry_offset]
            # A tag applies only until its matching ``</>`` marker. Do not
            # leak the previous bullet's colour/rarity into an uncoloured
            # reward that happens to be nearby in the prose string.
            last_close = prefix.rfind("</>")
            tagged_tail = prefix[last_close + 3:]
            tags = re.findall(
                r"<Rarity([A-Za-z]+)Start>",
                tagged_tail,
                re.IGNORECASE,
            )
            if tags:
                rarity_hint = _RARITY_NAMES.get(tags[-1].casefold(), "")
        definition = _candidate_definition(
            static_database_path,
            entry,
            player_level=player_level,
            rarity_hint=rarity_hint,
            class_name=class_name,
        )
        amount = _positive_amount(entry.get("amount"))
        chance_percent = entry.get("chance_percent")
        if chance_percent is not None:
            chance_percent = float(chance_percent)
            if not 0.0 < chance_percent <= 100.0:
                raise ValueError(
                    f"invalid reward chance {chance_percent!r} for "
                    f"{definition.template_id!r}"
                )
            mode = "chance"
        reward = _MappedReward(
            definition.template_id, amount, chance_percent
        )
        if mode == "guaranteed":
            guaranteed.append(reward)
        elif mode == "chance":
            chance.append(reward)
        else:
            raise ValueError(
                f"tooltip reward {entry.get('text')!r} has unknown mode"
            )
    return guaranteed, chance, saw_choose


def _pick_weighted(
    entries: list[_MappedReward], rng: random.Random,
) -> _MappedReward:
    total = sum(float(entry.chance_percent or 0.0) for entry in entries)
    marker = rng.random() * total
    cumulative = 0.0
    for entry in entries:
        cumulative += float(entry.chance_percent or 0.0)
        if marker < cumulative:
            return entry
    return entries[-1]


def _roll_chances(
    entries: list[_MappedReward], rng: random.Random,
) -> list[_MappedReward]:
    """Roll consecutive complete 100% blocks, then independent bonuses."""

    selected: list[_MappedReward] = []
    pending: list[_MappedReward] = []
    pending_total = 0.0

    def flush_independent() -> None:
        nonlocal pending, pending_total
        for entry in pending:
            if rng.random() * 100.0 < float(entry.chance_percent or 0.0):
                selected.append(entry)
        pending = []
        pending_total = 0.0

    for entry in entries:
        chance = float(entry.chance_percent or 0.0)
        if pending and pending_total + chance > 100.000001:
            flush_independent()
        pending.append(entry)
        pending_total += chance
        if abs(pending_total - 100.0) <= 0.000001:
            selected.append(_pick_weighted(pending, rng))
            pending = []
            pending_total = 0.0
    flush_independent()
    return selected


def _aggregate(entries: list[_MappedReward]) -> tuple[tuple[str, int], ...]:
    totals: dict[str, int] = {}
    for entry in entries:
        totals[entry.template_id] = (
            totals.get(entry.template_id, 0) + int(entry.amount)
        )
    return tuple(totals.items())


def _equipment_chest_reward(
    static_database_path: str | Path,
    record: dict[str, Any],
    *,
    class_name: str,
    player_level: int,
    rng: random.Random,
) -> ChestReward | None:
    """Resolve server-owned ``LOOTBOX_*equipment*`` reward lists."""

    loot_list = str(record.get("loot_list") or "")
    lowered = loot_list.casefold()
    if "equipment" not in lowered and "backitem" not in lowered:
        return None
    hints = tuple((record.get("ui") or {}).get("rarity_hints") or ())
    rarity = next(
        (
            _RARITY_NAMES[str(hint).casefold()]
            for hint in hints
            if str(hint).casefold() in _RARITY_NAMES
        ),
        "",
    )
    if not rarity:
        rarity = next(
            (
                name for name in EQUIPMENT_RARITIES
                if re.search(
                    rf"(?:^|_){re.escape(name.casefold())}(?:_|$)",
                    lowered,
                )
            ),
            "",
        )
    if not rarity:
        return None
    slot_types: tuple[str, ...] = ()
    if "backitem" in lowered:
        slot_types = ("CloakSlot",)
    roll = roll_equipment_reward(
        static_database_path,
        class_name=str(class_name),
        item_level=max(1, int(player_level)),
        rarity=rarity,
        slot_types=slot_types,
        rng=rng,
    )
    return ChestReward(
        _definition(static_database_path, roll.template_id),
        1,
        item_level=roll.item_level,
        rarity=roll.rarity,
        appearance=roll.appearance,
    )


def _gem_chest_reward(
    static_database_path: str | Path,
    record: dict[str, Any],
    *,
    rng: random.Random,
) -> ChestReward | None:
    """Resolve generic server-owned gem-bag lists by their rank suffix."""

    loot_list = str(record.get("loot_list") or "")
    match = re.search(r"(?:^|_)gem_bag_([a-s])$", loot_list, re.IGNORECASE)
    # This older starter bag predates the lettered bag tiers in the loot
    # report. It still needs to yield a real gem rather than disappear.
    rank = match.group(1).casefold() if match is not None else None
    if rank is None and loot_list.casefold() == "lootbox_e_misc_gem_bag_01":
        rank = "c"
    if rank is None:
        return None
    candidates: list[PurchaseItemDefinition] = []
    for family in _STANDARD_GEM_FAMILIES:
        try:
            candidates.append(
                _definition(static_database_path, f"gem_{family}_{rank}")
            )
        except ValueError:
            continue
    if not candidates:
        return None
    return ChestReward(candidates[rng.randrange(len(candidates))], 1)


def _rune_bag_reward(
    static_database_path: str | Path,
    record: dict[str, Any],
    *,
    rng: random.Random,
) -> ChestReward | None:
    """Resolve the rune bag to one of its 22 base-level standard runes."""

    loot_list = str(record.get("loot_list") or "").casefold()
    if loot_list != "lootbox_lootbox_runes_00":
        return None
    path = Path(static_database_path)
    database = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        rows = database.execute(
            "SELECT Id FROM _Template_Item "
            "WHERE ItemCategory='EquipmentRune' AND RuneGem=0 "
            "AND Id IN ("
            "'unique_gem_xp_gain_00', "
            "'unique_gem_wisdom_find_00', "
            "'unique_gem_resource_00', "
            "'unique_gem_resourceregen_00', "
            "'unique_gem_resistance_poison_00', "
            "'unique_gem_resistance_lightning_00', "
            "'unique_gem_resistance_ice_00', "
            "'unique_gem_resistance_fire_00', "
            "'unique_gem_resistance_andermagic_00', "
            "'unique_gem_resistance_00', "
            "'unique_gem_materi_find_00', "
            "'unique_gem_hitpoints_00', "
            "'unique_gem_healthregen_00', "
            "'unique_gem_grouptalent_all_prog_wisdom_00', "
            "'unique_gem_grouptalent_all_prog_xp_00', "
            "'unique_gem_grouptalent_all_prog_shards_00', "
            "'unique_gem_grouptalent_all_essentials_vc_00', "
            "'unique_gem_grouptalent_all_essentials_rf_00', "
            "'unique_gem_grouptalent_all_essentials_rc_00', "
            "'unique_gem_damage_00', "
            "'unique_gem_faster_run_00', "
            "'unique_gem_critical_00') AND Obsolete=0 "
            "ORDER BY Id"
        ).fetchall()
    finally:
        database.close()
    candidates = [
        _definition(path, str(row[0])) for row in rows
    ]
    if not candidates:
        return None
    return ChestReward(candidates[rng.randrange(len(candidates))], 1)


def _adventurer_chest_contents(
    static_database_path: str | Path,
    box_template_id: str,
    *,
    player_level: int,
    class_name: str,
    rng: random.Random,
) -> ChestContents | None:
    """Build the authored level-up chest chain when server loot lists are absent.

    The client static database contains the chest templates and their next-level
    names, but not the server-side LOOTBOX contents. Keep the progression
    available from the server's known item templates and the published chest
    reward tables. Unsupported legacy rewards are omitted rather than turned
    into guessed or invalid item templates.
    """

    match = _ADVENTURER_CHEST_LEVEL.fullmatch(str(box_template_id))
    if match is None:
        return None
    level = int(match.group(1))
    if level < 10 or level > 100 or level % 5:
        return None
    if int(player_level) < level:
        raise ValueError(
            f"container {box_template_id!r} requires level {level}"
        )

    # The first era of the chest chain has published rewards. Dark Legacy
    # changed the 60+ contents; those rewards are represented with current
    # server item templates (glyph currency, gem/rune bags and anxiety shards).
    loot: dict[int, tuple[tuple[str, int], ...]] = {
        10: (("potion_health_restore_rarity_01", 2), ("ammunition_damage_common", 250)),
        15: (("money_vc_gold", 25), ("lockpick", 10), ("lootbox_gems_a", 2)),
        20: (("mount_common_horse_01_3d", 1),),
        25: (("money_vc_gold", 75), ("respawn_soulguardian_2", 3), ("ammunition_damage_common", 999)),
        30: (("lockpick", 35), ("e_misc_gem_bag_01", 1)),
        35: (("ammunition_damage_common", 999), ("mount_terror_horse_blue_7d", 1)),
        40: (("respawn_soulguardian_2", 5), ("potion_health_restore_rarity_01", 5)),
        45: (("money_vc_gold", 150), ("lootbox_gems_e", 2), ("ammunition_damage_common", 999)),
        50: (("lockpick", 50), ("ammunition_damage_common", 1998), ("lootbox_gems_f", 1)),
        55: (("ammunition_damage_common", 3000), ("lootbox_runes_00", 1)),
        60: (("money_gc", 5000), ("lootbox_gems_f", 2), ("lootbox_runes_00", 1)),
        65: (("lootbox_gems_f", 2), ("lootbox_runes_00", 1)),
        70: (("money_gc", 17000), ("soul_shard_common", 20), ("lootbox_gems_f", 3)),
        75: (("money_gc", 18000), ("lockpick", 50), ("lootbox_gems_f", 3)),
        80: (("soul_shard_uncommon", 20), ("respawn_soulguardian_2", 10), ("lootbox_gems_g", 1)),
        85: (("soul_shard_magic", 25), ("lootbox_runes_00", 1), ("lockpick", 150)),
        90: (("money_gc", 30000), ("lootbox_gems_g", 1), ("respawn_soulguardian_2", 15)),
        95: (("soul_shard_rare", 32), ("money_gc", 35000), ("lockpick", 175)),
        100: (("lootbox_runes_00", 1), ("lootbox_gems_g", 1), ("money_gc", 50000), ("soul_shard_epic", 35), ("lockpick", 150), ("respawn_soulguardian_2", 15)),
    }

    rewards: list[ChestReward] = []
    for template_id, amount in loot.get(level, ()):
        try:
            rewards.append(ChestReward(_definition(static_database_path, template_id), amount))
        except ValueError:
            # A downstream client database may be older than the server's
            # static data. Skip an unavailable optional reward, but still keep
            # the chest progression itself intact.
            continue

    next_level = level + 5
    next_template_id = (
        "levelchest_lvl_60" if next_level == 60
        else f"levelchest_lvl{next_level}"
    )
    try:
        rewards.append(
            ChestReward(
                _definition(static_database_path, next_template_id),
                1,
            )
        )
    except ValueError:
        pass

    if level in {60, 85, 95} and str(class_name):
        try:
            roll = roll_equipment_reward(
                static_database_path,
                class_name=str(class_name),
                item_level=max(level, int(player_level)),
                rarity="Epic",
                rng=rng,
            )
            rewards.append(
                ChestReward(
                    _definition(static_database_path, roll.template_id),
                    1,
                    item_level=roll.item_level,
                    rarity=roll.rarity,
                    appearance=roll.appearance,
                )
            )
        except (OSError, ValueError):
            pass

    if not rewards:
        raise ValueError(f"adventurer chest {box_template_id!r} has no rewards")
    return ChestContents(
        box_template_id=box_template_id,
        source="adventurer_chest_progression",
        rewards=tuple(rewards),
        used_tooltip_mapping=False,
    )


def roll_chest_contents(
    static_database_path: str | Path,
    box_template_id: str,
    *,
    map_path: str | Path = DEFAULT_CHEST_CONTENTS_MAP,
    player_level: int = 1,
    class_name: str = "",
    rng: random.Random | None = None,
) -> ChestContents:
    """Resolve and roll one mapped inventory chest without mutating state."""

    box_template_id = str(box_template_id)
    box_definition = _definition(static_database_path, box_template_id)
    if box_definition.item_category not in _INVENTORY_CONTAINER_CATEGORIES:
        raise ValueError(
            f"item {box_template_id!r} is category "
            f"{box_definition.item_category!r}, not an inventory container"
        )
    records = _load_index(map_path).get(box_template_id)
    if not records:
        adventurer_contents = _adventurer_chest_contents(
            static_database_path,
            box_template_id,
            player_level=player_level,
            class_name=class_name,
            rng=rng or random.SystemRandom(),
        )
        if adventurer_contents is not None:
            return adventurer_contents
        if box_template_id in {"e_misc_gem_bag_01", "lootbox_runes_00"}:
            if int(player_level) < int(box_definition.required_level):
                raise ValueError(
                    f"container {box_template_id!r} requires level "
                    f"{int(box_definition.required_level)}"
                )
            database = sqlite3.connect(
                f"file:{Path(static_database_path).as_posix()}?mode=ro",
                uri=True,
            )
            try:
                row = database.execute(
                    "SELECT Loot FROM _Template_Item WHERE Id=?",
                    (box_template_id,),
                ).fetchone()
            finally:
                database.close()
            synthetic_record = {"loot_list": str(row[0] or "")} if row else {}
            fallback_rng = rng or random.SystemRandom()
            nested_reward = _gem_chest_reward(
                static_database_path, synthetic_record, rng=fallback_rng
            )
            if nested_reward is None:
                nested_reward = _rune_bag_reward(
                    static_database_path, synthetic_record, rng=fallback_rng
                )
            if nested_reward is not None:
                return ChestContents(
                    box_template_id=box_template_id,
                    source=str(synthetic_record.get("loot_list") or "static item loot"),
                    rewards=(nested_reward,),
                    used_tooltip_mapping=False,
                )
        raise ValueError(
            f"inventory container {box_template_id!r} has no JSON mapping"
        )
    record = _inventory_record(records, box_template_id)
    required_level = max(1, int(record.get("required_level") or 1))
    if int(player_level) < required_level:
        raise ValueError(
            f"container {box_template_id!r} requires level {required_level}"
        )
    if rng is None:
        rng = random.SystemRandom()

    guaranteed, one_of, chance, saw_choose = _direct_entries(
        static_database_path, record, class_name=str(class_name)
    )
    used_tooltip = False
    if not guaranteed and not one_of and not chance:
        guaranteed, tooltip_chance, saw_ui_choose = _tooltip_entries(
            static_database_path,
            record,
            player_level=max(1, int(player_level)),
            class_name=str(class_name),
        )
        chance = tooltip_chance
        saw_choose = saw_choose or saw_ui_choose
        used_tooltip = True
    if saw_choose:
        raise ValueError(
            f"selective container {box_template_id!r} requires a client "
            "reward selection"
        )
    if not guaranteed and not one_of and not chance:
        equipment_reward = _equipment_chest_reward(
            static_database_path,
            record,
            class_name=str(class_name),
            player_level=max(1, int(player_level)),
            rng=rng,
        )
        if equipment_reward is not None:
            return ChestContents(
                box_template_id=box_template_id,
                source=str(record.get("loot_list") or record.get("source")),
                rewards=(equipment_reward,),
                used_tooltip_mapping=False,
            )
        gem_reward = _gem_chest_reward(
            static_database_path, record, rng=rng
        )
        if gem_reward is not None:
            return ChestContents(
                box_template_id=box_template_id,
                source=str(record.get("loot_list") or record.get("source")),
                rewards=(gem_reward,),
                used_tooltip_mapping=False,
            )
        rune_reward = _rune_bag_reward(
            static_database_path, record, rng=rng
        )
        if rune_reward is not None:
            return ChestContents(
                box_template_id=box_template_id,
                source=str(record.get("loot_list") or record.get("source")),
                rewards=(rune_reward,),
                used_tooltip_mapping=False,
            )
        raise ValueError(
            f"inventory container {box_template_id!r} has no resolvable rewards"
        )

    selected = list(guaranteed)
    if one_of:
        selected.append(one_of[rng.randrange(len(one_of))])
    selected.extend(_roll_chances(chance, rng))
    if any(entry.template_id == box_template_id for entry in selected):
        raise ValueError(
            f"container {box_template_id!r} maps to itself rather than loot"
        )
    rewards_list: list[ChestReward] = []
    ordinary_selected: list[_MappedReward] = []
    for entry in selected:
        if entry.equipment_rarity:
            roll = roll_equipment_reward(
                static_database_path,
                class_name=str(class_name),
                item_level=max(1, int(player_level)),
                rarity=entry.equipment_rarity,
                slot_types=entry.equipment_slot_types,
                rng=rng,
                appearance_region=entry.equipment_appearance_region,
            )
            rewards_list.append(ChestReward(
                _definition(static_database_path, roll.template_id),
                entry.amount,
                item_level=roll.item_level,
                rarity=roll.rarity,
                appearance=roll.appearance,
            ))
        else:
            ordinary_selected.append(entry)
    rewards_list.extend(
        ChestReward(_definition(static_database_path, template_id), amount)
        for template_id, amount in _aggregate(ordinary_selected)
    )
    rewards = tuple(rewards_list)
    return ChestContents(
        box_template_id=box_template_id,
        source=str(record.get("source") or "chest_contents.json"),
        rewards=rewards,
        used_tooltip_mapping=used_tooltip,
    )


def open_mapped_chest(
    inventory: InventoryInfo,
    currency: CurrencyChanged,
    game_currencies: dict[str, int],
    contents: ChestContents,
    item_id: int,
    *,
    static_database_path: str | Path,
    next_item_id: int,
    player_level: int,
    class_name: str,
    map_difficulty: int = 0,
    rng: random.Random | None = None,
) -> ChestOpenMutation:
    """Atomically consume one chest and materialize its already-rolled loot."""

    builder = InventoryMutationBuilder(inventory, next_item_id)
    consumed = builder.consume(item_id, 1)
    if consumed.template_id != contents.box_template_id:
        raise ValueError(
            f"inventory item {item_id} is {consumed.template_id!r}, expected "
            f"{contents.box_template_id!r}"
        )

    updated_currency = currency
    updated_game_currencies = dict(game_currencies)
    game_updates: dict[str, GameCurrencyChanged] = {}
    fixed_currency_changed = False
    for reward in contents.rewards:
        credit = credit_currency_template(
            static_database_path,
            updated_currency,
            updated_game_currencies,
            reward.definition.template_id,
            reward.amount,
            scale_by_item_value=False,
        )
        if credit is not None:
            fixed_currency_changed = (
                fixed_currency_changed or credit.currency != updated_currency
            )
            updated_currency = credit.currency
            updated_game_currencies = dict(credit.game_currencies)
            if credit.game_currency_update is not None:
                game_updates[
                    credit.game_currency_update.resource_id
                ] = credit.game_currency_update
            continue
        item_level = max(
            int(reward.definition.minimum_item_level),
            min(
                max(
                    1,
                    int(
                        reward.item_level
                        if reward.item_level is not None
                        else player_level
                    ),
                ),
                int(reward.definition.maximum_item_level),
            ),
        )
        before_ids = {
            int(item.item_id) for item in builder.inventory.items
        }
        builder.add_item(
            reward.definition,
            reward.amount,
            item_level=item_level,
        )
        if reward.rarity is not None:
            for added in builder.inventory.items:
                if int(added.item_id) in before_ids:
                    continue
                added.field_08 = int(reward.rarity)
                added.item_level = int(item_level)
                if reward.appearance:
                    added.field_80 = str(reward.appearance)

    changed_ids = tuple(
        int(item.item_id) for item in builder.changed_items
    )
    if changed_ids:
        roll_new_equipment_enchantments(
            static_database_path,
            builder.inventory,
            changed_ids,
            class_name=class_name,
            map_difficulty=map_difficulty,
            rng=rng,
        )
        refresh_inventory_economy_values(
            static_database_path,
            builder.inventory,
            item_ids=changed_ids,
            force_melt_value=True,
        )
        current_items = {
            int(item.item_id): item for item in builder.inventory.items
        }
        builder.changed_items = [
            current_items.get(int(item.item_id), item)
            for item in builder.changed_items
        ]
    mutation = builder.finish()
    updates = tuple(game_updates.values())
    return ChestOpenMutation(
        contents=contents,
        inventory=mutation.inventory,
        currency=updated_currency,
        game_currencies=updated_game_currencies,
        game_currency_updates=updates,
        currency_changed=fixed_currency_changed or bool(updates),
        delta=mutation.delta,
        final=mutation.final,
        next_item_id=mutation.next_item_id,
    )


__all__ = [
    "ChestContents",
    "ChestOpenMutation",
    "ChestReward",
    "DEFAULT_CHEST_CONTENTS_MAP",
    "open_mapped_chest",
    "roll_chest_contents",
]
