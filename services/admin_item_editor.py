"""CMS equipment editor: enchantments, their tiers and the base-stat roll.

An item's stats come from three saved inputs. Each concrete enchantment
carries its template, a 0..1 roll (``value``), an enchantment tier and the
rarity it rolled at (``kind``); the tier and rarity pick the power-factor
range the roll moves through. Base stats are not saved at all: the client
and the server both derive them from ``ItemInfo.field_18`` (seedIdx), whose
dSFMT stream gives one roll per ranged base modifier, and from
``base_enchantment_tier``, which bounds those rolls. Previews reuse the
player-stat instruction builders, so the values shown are the ones combat
applies.
"""

from contextlib import closing
from functools import lru_cache
from pathlib import Path
import math
import sqlite3
import threading

from dso_server.gameplay.item_random import (
    base_enchantment_seed, deterministic_base_enchantment_rolls, leading_item_rolls,
)
from dso_server.gameplay.player_combat_stats import (
    _numeric_range, _resolved_range, _scaled_item_instruction,
    prime_player_combat_database,
)
from dso_server.protocol.inventory_codec import ItemEnchantmentInfo, clone_item_info
from dso_server.protocol.inventory_economy_protocol import refresh_item_economy_values
from dso_server.protocol.world_loot_protocol import (
    EQUIPMENT_RARITIES, LootItemDefinition, _compatible_random_enchantments,
    _is_equipment_row, _resource_ids, _tokens, prime_world_loot_cache,
)


# Retail rolls at most four random enchantments; fixed compositions run longer.
MAX_ENCHANTMENTS = 8
# World loot draws seedIdx from 1..0x7FFF and the client halves it, so these
# 16,384 base seeds are every roll an item can get; seedIdx 2s+1 selects s.
BASE_SEEDS = 0x4000
MAX_SEED_INDEX = 0x7FFF
# No template has more than three ranged base modifiers.
TABLE_ROLLS = 8
_FLAG_TOKENS = ("levelmodifier", "useclasshitpoints")

_roll_table = None
_roll_table_lock = threading.Lock()


def base_roll_table():
    """The first rolls of every base seed, built once (about two seconds)."""
    global _roll_table
    with _roll_table_lock:
        if _roll_table is None:
            _roll_table = tuple(leading_item_rolls(seed, TABLE_ROLLS) for seed in range(BASE_SEEDS))
        return _roll_table


def warm_base_roll_table():
    if _roll_table is None:
        threading.Thread(target=base_roll_table, name="admin-base-roll-table", daemon=True).start()


@lru_cache(maxsize=4)
def _labels(path, revision):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        names = {str(row[0]): str(row[1] or "") for row in db.execute("SELECT Id,Name FROM _Template_Enchantment")}
        difficulties = {}
        try:
            difficulties = {int(row[0]): str(row[1] or "")
                            for row in db.execute("SELECT Id,Name FROM _Template_MapDifficulty")}
        except sqlite3.Error:
            pass
    for prefix in ("Item Enchantment ", "Base Enchantment "):
        names = {key: value[len(prefix):] if value.startswith(prefix) else value for key, value in names.items()}
    return names, difficulties


def _loot_definition(row):
    return LootItemDefinition(
        template_id=str(row["Id"]), item_category=str(row.get("ItemCategory") or ""),
        auto_pick_up=str(row.get("AutoPickUp") or "None"),
        min_socket_count=str(row.get("MinSocketCount") or "0"),
        max_socket_count=str(row.get("MaxSocketCount") or "0"),
        appearance_collection=str(row.get("AppearanceCollection") or ""),
        slot_types=_tokens(row.get("SlotType")), char_classes=_tokens(row.get("CharClass")),
        base_enchantments=_resource_ids(row.get("BaseEnchantments")),
        enchantment_specification=str(row.get("Enchantments") or ""),
    )


class ItemEditor:
    """Validated stat edits of one equipment item for one character class."""

    def __init__(self, static_database_path, item, class_name):
        self.path = Path(static_database_path)
        self.item = clone_item_info(item)
        self.combat = prime_player_combat_database(self.path)
        loot = prime_world_loot_cache(self.path)
        self.row = loot.items_by_id.get(str(item.template_id))
        self.editable = _is_equipment_row(self.row)
        self.names, self.difficulties = _labels(str(self.path), self.combat.database_revision)
        if not self.editable:
            return
        self.definition = self.combat.items.get(str(item.template_id))
        classes = [value.casefold() for value in _tokens(self.row.get("CharClass"))]
        if classes and str(class_name).casefold() not in classes:
            class_name = classes[0]
        loot_definition = _loot_definition(self.row)
        compatible = _compatible_random_enchantments(
            loot, loot_definition, class_name=str(class_name),
            item_level=max(1, int(item.item_level)), rarity=max(0, min(7, int(item.rarity))))
        fixed = [entry.split("|")[0].strip() for entry in _resource_ids(loot_definition.enchantment_specification)]
        current = [str(e.template_id) for e in item.enchantments]
        self.compatible = {e.template_id for e in compatible}
        self.choices = [template_id for template_id in dict.fromkeys(
            [*fixed, *sorted(self.compatible, key=lambda value: (self.names.get(value, value), value)), *current])
            if template_id in self.combat.enchantments]
        # Trinket runes and similar equipment have neither base rolls nor
        # enchantments the game would place on them.
        self.editable = bool(self.choices or self._base_plan(self.item))

    # -- stat lines --------------------------------------------------------

    def _line(self, enchantment, modifier, *, multiplier=1.0, roll=None, low=None, high=None):
        def value(amount_multiplier, roll_fraction):
            instruction = _scaled_item_instruction(
                self.combat, modifier, item_level=enchantment.effective_level(self.item.item_level),
                scale_absolute=enchantment.scale_absolute, reference_level=enchantment.scale_reference_level,
                amount_multiplier=amount_multiplier, roll_fraction=roll_fraction)
            resolved = _resolved_range(instruction, {})
            return None if resolved is None else resolved[1]
        arguments = modifier.arguments
        relative = len(arguments) > 1 and arguments[1].casefold() == "relative"
        scopes = [token for token in (value.strip() for value in arguments[2:]) if token
                  and not token.casefold().endswith(("factorabsolute", "factorrelative"))
                  and token.casefold() not in _FLAG_TOKENS]
        kind = modifier.kind[4:] if modifier.kind.startswith("Item") and len(modifier.kind) > 4 else modifier.kind
        label = "".join(f" {c.lower()}" if c.isupper() and index else c for index, c in enumerate(kind))
        line = {"stat": modifier.kind, "label": label + (f" ({', '.join(scopes)})" if scopes else ""),
                "relative": relative, "value": value(multiplier, roll)}
        if low is not None:
            line["minimum"], line["maximum"] = value(*low), value(*high)
        line["text"] = self.format(line["value"], relative)
        if low is not None:
            line["range"] = f"{self.format(line['minimum'], relative, sign=False)} – {self.format(line['maximum'], relative, sign=False)}"
        return line

    @staticmethod
    def format(value, relative, sign=True):
        if value is None or not math.isfinite(value):
            return "—"
        prefix = "+" if sign and value >= 0 else ""
        if relative:
            return f"{prefix}{value * 100:,.1f}%"
        return f"{prefix}{value:,.0f}" if abs(value) >= 100 else f"{prefix}{value:,.2f}"

    def enchantment_lines(self, record):
        enchantment = self.combat.enchantments.get(str(record.template_id))
        if enchantment is None:
            return []
        factor = lambda roll: self.combat.enchantment_power_factor(int(record.tier), int(record.kind), roll)
        lines = []
        for modifier in enchantment.modifiers:
            if enchantment.use_tier_factor:
                lines.append(self._line(enchantment, modifier, multiplier=factor(record.value),
                                        low=(factor(0.0), None), high=(factor(1.0), None)))
            else:
                lines.append(self._line(enchantment, modifier, roll=float(record.value),
                                        low=(1.0, 0.0), high=(1.0, 1.0)))
        return lines

    def _base_plan(self, item):
        """Each base modifier with whether it takes a roll, in roll order."""
        plan = []
        for enchantment_id in (self.definition.base_enchantment_ids if self.definition else ()):
            enchantment = self.combat.enchantments.get(enchantment_id)
            if enchantment is None:
                continue
            for modifier in enchantment.modifiers:
                numeric = _numeric_range(modifier.arguments[0] if modifier.arguments else "", details=(), variables={})
                plan.append((enchantment_id, enchantment, modifier, numeric is not None and numeric[0] != numeric[1]))
        return plan

    def _quality(self, item, enchantment, roll):
        if not enchantment.use_tier_factor:
            return roll
        return self.combat.base_enchantment_power_factor(int(item.base_enchantment_tier), int(item.rarity), roll)

    def base_lines(self, item):
        rolls = deterministic_base_enchantment_rolls(int(item.field_18))
        lines = []
        for enchantment_id, enchantment, modifier, ranged in self._base_plan(item):
            if not ranged:
                line = self._line(enchantment, modifier)
                line.update(roll=None, quality=None)
            else:
                roll = next(rolls)
                quality = self._quality(item, enchantment, roll)
                line = self._line(enchantment, modifier, roll=quality,
                                  low=(1.0, self._quality(item, enchantment, 0.0)),
                                  high=(1.0, self._quality(item, enchantment, 1.0)))
                line.update(roll=roll, quality=quality)
            line.update(enchantment_id=enchantment_id, name=self.names.get(enchantment_id, enchantment_id),
                        tier_factor=enchantment.use_tier_factor)
            lines.append(line)
        return lines

    def best_seed(self, item, target):
        """The seedIdx whose base qualities lie closest to ``target``.

        Ties keep the item's own seed, so a target the tier already fixes
        (tier 16 is always 100%) does not change the item for nothing.
        """
        ranged = [enchantment for _, enchantment, _, is_ranged in self._base_plan(item) if is_ranged]
        if not ranged:
            return int(item.field_18)
        ranged = ranged[:TABLE_ROLLS]

        def score(rolls):
            gaps = [abs(self._quality(item, enchantment, roll) - target) for enchantment, roll in zip(ranged, rolls)]
            return max(gaps), sum(gaps)

        best_seed = int(item.field_18)
        best = score(leading_item_rolls(base_enchantment_seed(best_seed), len(ranged)))
        for seed, rolls in enumerate(base_roll_table()):
            candidate = score(rolls)
            if candidate < best:
                best, best_seed = candidate, 2 * seed + 1
        return best_seed

    # -- edits -------------------------------------------------------------

    def apply(self, changes):
        """Return a copy of the item with ``changes`` applied, or raise ValueError."""
        if not self.editable:
            raise ValueError("only equipment items have editable enchantments")
        if not isinstance(changes, dict):
            raise ValueError("changes must be an object")
        unknown = changes.keys() - {"enchantments", "base_enchantment_tier", "base_seed", "base_target"}
        if unknown:
            raise ValueError("unknown item change: " + ", ".join(sorted(unknown)))
        item = clone_item_info(self.item)
        if "base_enchantment_tier" in changes:
            tier = changes["base_enchantment_tier"]
            if type(tier) is not int or tier not in self.combat.base_enchantment_power_factors:
                raise ValueError("unknown base enchantment tier")
            item.base_enchantment_tier = tier
        if "base_seed" in changes:
            seed = changes["base_seed"]
            if type(seed) is not int or not (seed == int(self.item.field_18) or 1 <= seed <= MAX_SEED_INDEX):
                raise ValueError(f"base roll seed must be a whole number from 1 to {MAX_SEED_INDEX}")
            item.field_18 = seed
        if changes.get("base_target") is not None:
            target = changes["base_target"]
            if type(target) not in (int, float) or not 0 <= target <= 1:
                raise ValueError("base roll target must be from 0 to 1")
            item.field_18 = self.best_seed(item, float(target))
        if "enchantments" in changes:
            item.enchantments = self._enchantments(changes["enchantments"])
        return item

    def _enchantments(self, values):
        if not isinstance(values, list) or len(values) > MAX_ENCHANTMENTS:
            raise ValueError(f"an item can carry at most {MAX_ENCHANTMENTS} enchantments")
        result = []
        for position, entry in enumerate(values, 1):
            if not isinstance(entry, dict) or entry.keys() - {"template_id", "value", "tier", "kind", "origin_item_level"}:
                raise ValueError(f"enchantment {position} is malformed")
            template_id, value = entry.get("template_id"), entry.get("value")
            tier = entry.get("tier", 0)
            kind = entry.get("kind", int(self.item.rarity))
            origin = entry.get("origin_item_level", max(1, int(self.item.item_level)))
            if template_id not in self.choices:
                raise ValueError(f"enchantment {position} cannot be placed on this item")
            if type(value) not in (int, float) or isinstance(value, bool) or not 0 <= value <= 1:
                raise ValueError(f"enchantment {position} roll must be from 0 to 1")
            if type(tier) is not int or tier not in self.combat.enchantment_power_factors:
                raise ValueError(f"enchantment {position} has an unknown tier")
            if type(kind) is not int or not 0 <= kind < len(EQUIPMENT_RARITIES):
                raise ValueError(f"enchantment {position} has an unknown origin rarity")
            if type(origin) is not int or not 1 <= origin <= 10_000:
                raise ValueError(f"enchantment {position} origin item level must be from 1 to 10000")
            result.append(ItemEnchantmentInfo(value=float(value), kind=kind, tier=tier,
                                              origin_item_level=origin, template_id=template_id))
        return result

    def finish(self, item):
        """Recompute the melt value the enchantments feed into.

        Only glyph_value follows: stack_vc_value 11 marks old local loot.
        """
        item = clone_item_info(item)
        item.glyph_value = refresh_item_economy_values(
            self.path, clone_item_info(item), force_melt_value=True).glyph_value
        return item

    # -- document ----------------------------------------------------------

    def document(self, item=None):
        item = self.item if item is None else item
        result = {"editable": self.editable, "item": {
            "item_id": item.item_id, "template_id": item.template_id, "rarity": item.rarity,
            "rarity_name": EQUIPMENT_RARITIES[max(0, min(7, int(item.rarity)))],
            "item_level": item.item_level, "base_enchantment_tier": item.base_enchantment_tier,
            "base_seed": item.field_18, "glyph_value": item.glyph_value,
        }}
        if not self.editable:
            return result
        result["item"].update(category=str(self.row.get("ItemCategory") or ""),
                              slot_types=list(_tokens(self.row.get("SlotType"))))
        base = self.base_lines(item)
        if any(line["roll"] is not None for line in base):
            warm_base_roll_table()
        result["base"] = {"tier": item.base_enchantment_tier, "seed": item.field_18, "stats": base}
        result["enchantments"] = [{
            "template_id": e.template_id, "name": self.names.get(e.template_id, e.template_id),
            "value": e.value, "tier": e.tier, "kind": e.kind, "origin_item_level": e.origin_item_level,
            "tier_factor": bool(getattr(self.combat.enchantments.get(e.template_id), "use_tier_factor", False)),
            "lines": self.enchantment_lines(e),
        } for e in item.enchantments]
        rarity = max(0, min(7, int(item.rarity)))
        tier_name = lambda tier: self.difficulties.get(tier, "")
        result["options"] = {
            "max_enchantments": MAX_ENCHANTMENTS, "max_seed": MAX_SEED_INDEX,
            "rolled_count": rarity if rarity < 5 else None,
            "rarities": list(EQUIPMENT_RARITIES),
            "enchantments": [{
                "template_id": template_id, "name": self.names.get(template_id, template_id),
                "compatible": template_id in self.compatible,
                # The best roll at tier 0, whose top power factor is 1.0 for every rarity.
                "summary": ", ".join(f"{line['text']} {line['label']}" for line in self.enchantment_lines(
                    ItemEnchantmentInfo(value=1.0, kind=rarity, template_id=template_id))),
            } for template_id in self.choices],
            "tiers": [{"tier": tier, "name": tier_name(tier), "factors": [list(pair) for pair in factors]}
                      for tier, factors in sorted(self.combat.enchantment_power_factors.items())],
            "base_tiers": [{"tier": tier, "name": tier_name(tier), "factors": [list(pair) for pair in factors],
                            "range": list(factors[min(rarity, len(factors) - 1)])}
                           for tier, factors in sorted(self.combat.base_enchantment_power_factors.items())],
        }
        return result
