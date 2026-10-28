"""Typed world-loot generation, spawning, and pickup support.

Retail creates a dropped object with ``NewItemCommand`` (0x002d).  The
command body is the same generated ``Game::ItemInfo`` structure used by the
inventory protocol, followed by the normal server actor context.  Loot is not
an inventory item until it is picked up.

The client database contains monster/rank loot-list identifiers and every
eligible item template, but not the contents of server-owned ``MAIN_*`` loot
lists.  The fallback resolver therefore preserves the database gates (level,
class, event/obsolete state, item category, sockets, appearance, monster drop
chance) and uses a small rank policy only for the missing list cardinality.
"""

from __future__ import annotations

from dso_server.runtime import resident_content
from dso_server.protocol.command_ids import command_id
from collections.abc import Callable, Mapping
import copy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
import math
from pathlib import Path
import random
import re
import sqlite3

from dso_server.persistence import static_schema_compat as compat
from dso_server.protocol.character_selection import BitReader, BitWriter
from dso_server.protocol.inventory_codec import (
    INVENTORY_INFO_COMMAND,
    InventoryInfo,
    ItemEnchantmentInfo,
    ItemInfo,
    decode_item_info,
    encode_item_info,
    read_inventory_info_body,
    write_inventory_info_body,
    free_inventory_location,
)
from dso_server.protocol.player_init_protocol import CurrencyChanged
from dso_server.gameplay.item_economy_values import equipment_melt_value, item_stack_vc_value


NEW_ITEM_COMMAND = command_id("Commands::NewItemCommand", 0x002D)
DISCARD_ITEM_COMMAND = command_id("Commands::DiscardItemCommand", 0x002E)
PICKUP_ITEM_COMMAND = command_id("Commands::PickupItemCommand", 0x0064)
PICKUP_INFO_COMMAND = command_id("Commands::PickupInfoCommand", 0x0065)
CURRENCY_CHANGED_COMMAND = command_id("Commands::CurrencyChangedCommand", 0x0080)
MESSAGE_COMMAND = command_id("Commands::MessageCommand", 0x0089)
LOOT_RECEIVED_MESSAGE = 0x19

GOLD_ITEM_TEMPLATES = frozenset(("money_vc", "money_vc_gold"))
ANDERMANT_ITEM_TEMPLATE = "money_rc"
WISDOM_ITEM_TEMPLATE = "money_gc"
ANDERMANT_BASE_DROP_MULTIPLIER = 50
ENCHANTMENT_FREE_TRINKET_CATEGORIES = frozenset((
    'JewelTrinket',
    'RuneTrinket',
))
MERCENARY_EQUIPMENT_CATEGORIES = frozenset((
    'MercenaryDefenseBackitem',
    'MercenaryJewelry',
))
# Every ItemCategory this content revision authors under an
# ``item_equipment_*`` IconConfiguration.
#
# The world-drop generator only ever picks templates whose icon is one of
# those, so an icon test is enough to recognize equipment that dropped. It is
# not enough for equipment that arrived some other way: 440 slotted templates
# carry an equipment category with no IconConfiguration at all, including most
# of the older uniques a vendor can sell. Recognizing them by category instead
# is what lets a purchase roll the same stats as a drop, while still excluding
# the 102 Consumable and 3 Ammunition templates that also carry a SlotType.
EQUIPMENT_ITEM_CATEGORIES = frozenset((
    'Absorber',
    'AncientTrinket',
    'Armor',
    'Book',
    'DefenseBackitem',
    'DefenseQuiver',
    'EquipmentJewel',
    'JewelTrinket',
    'Jewelry',
    'LootGenItem',
    'MercenaryDefenseBackitem',
    'MercenaryJewelry',
    'OffenseBackitem',
    'OffenseQuiver',
    'OffenseShield',
    'Orb',
    'RuneTrinket',
    'Shield',
    'SkinConsumable',
    'SmallWeapon',
    'Weapon',
    'WeaponModifier',
    'Wrench',
))
BASE_WISDOM_DROP_CHANCE = 0.12
# Event progress dropped by an event map's final boss, in base drops.
EVENT_FINAL_BOSS_PROGRESS_MULTIPLIER = 25
EQUIPMENT_RARITIES = (
    "Common",
    "Uncommon",
    "Magic",
    "Rare",
    "Epic",
    "Unique",
    "Set",
    "Mythic",
)
ROLLED_EQUIPMENT_RARITIES = EQUIPMENT_RARITIES[:5]
FIXED_EQUIPMENT_RARITIES = EQUIPMENT_RARITIES[5:]


@dataclass(frozen=True)
class MonsterLootProfile:
    template_id: str
    monster_level: int
    rank_id: str
    rank_kind: str
    drop_chance: float
    loot_stack_size: float
    loot_list_id: str
    health_event_loot_lists: str
    andermant_drop_chance: float


@dataclass(frozen=True)
class LootItemDefinition:
    template_id: str
    item_category: str
    auto_pick_up: str
    min_socket_count: str
    max_socket_count: str
    appearance_collection: str
    slot_types: tuple[str, ...] = ()
    char_classes: tuple[str, ...] = ()
    base_enchantments: tuple[str, ...] = ()
    enchantment_specification: str = ""


@dataclass(frozen=True)
class LootEnchantmentDefinition:
    """Database-owned constraints for one concrete item enchantment."""

    template_id: str
    slot_types: frozenset[str]
    item_categories: frozenset[str]
    char_classes: frozenset[str]
    min_rarity: int
    max_rarity: int
    min_item_level: int
    max_item_level: int
    max_origin_item_level: int
    required_affinities: frozenset[str]
    provided_affinities: frozenset[str]
    probability_weight: float
    base_enchantment: bool
    min_vc_value: int
    max_vc_value: int


@dataclass(frozen=True)
class WorldLootDrop:
    item: ItemInfo
    item_category: str
    auto_pick_up: str
    # Server-only provenance: shared item templates must credit their own quest.
    quest_id: str = ""
    quest_collect_id: str = ""


def quest_item_template_id(item_id: str) -> str:
    """Resolve authored quest resources to the shipped inventory template."""
    return {"k0002_cremationashes": "quest_k0002_cremationashes"}.get(item_id, item_id)


@lru_cache(maxsize=1024)
def _quest_drop_definition(static_database_path: str | Path, template_id: str):
    from dso_server.protocol.commerce_protocol import load_purchase_item_definition

    # This quest omits the prefix used by its actual item resource. Keep the
    # authored collect ID separate from the item sent to the client.
    item_id = quest_item_template_id(template_id)
    definition = load_purchase_item_definition(static_database_path, item_id)
    with sqlite3.connect(f"file:{Path(static_database_path).as_posix()}?mode=ro", uri=True) as db:
        row = db.execute("SELECT AutoPickUp FROM _Template_Item WHERE Id=?",
                         (item_id,)).fetchone()
    return definition, str(row[0] or "")


def build_quest_world_drops(
    static_database_path: str | Path,
    quest_id: str,
    template_id: str,
    amount: int,
    position: tuple[float, float, float],
) -> tuple[WorldLootDrop, ...]:
    """Build authored quest loot with provisional IDs for shared allocation."""
    from dso_server.protocol.commerce_protocol import _new_item

    definition, auto_pick_up = _quest_drop_definition(static_database_path, template_id)
    result = []
    remaining = max(0, int(amount))
    while remaining:
        stack = min(remaining, definition.max_stack_size)
        item = _new_item(definition, len(result), stack)
        item.field_100 = tuple(float(v) for v in position)
        result.append(WorldLootDrop(
            item, definition.item_category, auto_pick_up, quest_id, template_id,
        ))
        remaining -= stack
    return tuple(result)


@dataclass(frozen=True)
class WorldLootGeneration:
    profile: MonsterLootProfile
    drops: tuple[WorldLootDrop, ...]
    next_item_id: int


@dataclass(frozen=True)
class EquipmentRewardRoll:
    """One concrete equipment result for a server-owned reward list."""

    template_id: str
    item_level: int
    rarity: int
    appearance: str


@dataclass(frozen=True)
class InventoryLootMutation:
    inventory: InventoryInfo
    delta: InventoryInfo
    final: InventoryInfo


@dataclass(frozen=True)
class LootReceivedMessage:
    currency_type: int
    amount: int
    item_id: int
    template_id: str
    actor_id: int


@dataclass(frozen=True)
class WorldPickupResponse:
    loot_received: LootReceivedMessage | None
    item_id: int
    notify: bool
    discard_actor_id: int
    template_id: str
    pickup_actor_id: int
    currency: CurrencyChanged | None
    inventory_states: tuple[InventoryInfo, ...]
    state_actor_ids: tuple[int, ...]


@dataclass(frozen=True)
class StaticLootCache:
    """Immutable kill-path view of the database-owned loot tables."""

    database_path: Path
    database_revision: tuple[int, int]
    item_rows: tuple[dict[str, object], ...]
    items_by_id: dict[str, dict[str, object]]
    monster_profiles: dict[str, MonsterLootProfile]
    appearance_rows: tuple[dict[str, object], ...]
    currency_factors: dict[int, float]
    difficulty_stack_factors: dict[int, tuple[float, float]]
    enchantments_by_id: dict[str, LootEnchantmentDefinition]
    random_enchantments: tuple[LootEnchantmentDefinition, ...]
    rarity_weights_by_difficulty: dict[
        int, tuple[float, float, float, float, float]
    ]
    enchantment_tiers: frozenset[int]
    standard_gems_by_rank: dict[
        str, tuple[LootItemDefinition, ...]
    ]

    def gems_for_rank(self, rank: str) -> tuple[LootItemDefinition, ...]:
        """Return the gem pool for ``rank``, falling back to the best one.

        The client content authors the gem ladder past its released end: the
        top ranks exist as items with prices and icons but their base
        enchantment carries a zero modifier, so awarding them hands the player
        a gem worth ``+0``.  Those ranks are excluded when the cache is built,
        so a request above the released end returns the best real pool rather
        than nothing.
        """

        available = sorted(self.standard_gems_by_rank)
        if not available:
            return ()
        return self.standard_gems_by_rank[min(str(rank), available[-1])]

    def close(self) -> None:
        """Compatibility no-op for the former scoped database helper."""


def _open_read_only(path: Path) -> sqlite3.Connection:
    if not resident_content.is_file(path):
        raise FileNotFoundError(path)
    database = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    return database


def _probability(value: object) -> float:
    return max(0.0, min(1.0, float(value or 0.0)))


def _tokens(value: object) -> tuple[str, ...]:
    """Split the generated comma/semicolon resource-list columns."""

    return tuple(
        token.strip()
        for token in re.split(r"[,;]", str(value or ""))
        if token.strip()
    )


def _resource_ids(value: object) -> tuple[str, ...]:
    return tuple(
        token.strip()
        for token in str(value or "").split(";")
        if token.strip()
    )


def _modifier_amounts(value: object) -> tuple[float, ...]:
    """Read the amounts out of a ``Kind:amount,mode[,tags]`` modifier list."""

    amounts = []
    for entry in str(value or "").split(";"):
        _kind, separator, arguments = entry.strip().partition(":")
        if not separator:
            continue
        try:
            amounts.append(float(arguments.split(",")[0]))
        except ValueError:
            continue
    return tuple(amounts)


def _zero_value_enchantment_ids(
    database: sqlite3.Connection,
) -> frozenset[str]:
    """Enchantments whose every modifier grants exactly nothing."""

    return frozenset(
        str(row["Id"])
        for row in database.execute(
            "SELECT Id, Modifiers FROM _Template_Enchantment"
        )
        if not any(_modifier_amounts(row["Modifiers"]))
    )


def _rarity_index(value: object, default: int) -> int:
    try:
        return EQUIPMENT_RARITIES.index(str(value))
    except ValueError:
        return default


def _positive_level(value: object, default: int) -> int:
    if value is None:
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _load_monster_ranks(
    database: sqlite3.Connection,
) -> dict[str, dict[str, object]]:
    """Rank rows keyed by whatever ``_Template_Monster.Rank`` refers to.

    The two content generations disagree about what a rank *is*. The current one
    normalises it: ``_Template_Monster.Rank`` is a foreign key such as
    ``'Rank_champion'`` into ``_Template_MonsterRanks.Id``, and the kind itself
    lives in a ``Rank`` column on that row. The legacy build keeps the kind on
    the monster -- ``Rank`` is plainly ``'champion'`` -- and uses
    ``_Template_MonsterRanks`` only to override loot for the few kinds that need
    it, so it holds a single row where the current one holds twenty.

    Reading the legacy table as-is would therefore resolve one rank and drop
    every monster of the other six kinds from the loot tables. So when the
    ``Rank`` column is absent, the lookup is rebuilt from the kinds the monsters
    actually use, with the sparse override row merged in where one exists.
    """

    if compat.has_columns(database, "_Template_MonsterRanks", "Rank"):
        return {
            str(row["Id"]): dict(row)
            for row in database.execute(
                """
                SELECT Id, Rank, Loot, HealthEventLootLists
                FROM _Template_MonsterRanks
                """
            ).fetchall()
        }

    overrides = {
        str(row["Id"]): dict(row)
        for row in database.execute(
            """
            SELECT Id, Loot, HealthEventLootLists
            FROM _Template_MonsterRanks
            """
        ).fetchall()
    }
    ranks: dict[str, dict[str, object]] = {}
    for row in database.execute(
        "SELECT DISTINCT Rank FROM _Template_Monster"
    ).fetchall():
        kind = str(row["Rank"] or "").strip()
        if not kind:
            continue
        override = overrides.get(kind, {})
        ranks[kind] = {
            "Id": kind,
            "Rank": kind,
            "Loot": override.get("Loot") or "",
            "HealthEventLootLists": (
                override.get("HealthEventLootLists") or ""
            ),
        }
    return ranks


@lru_cache(maxsize=4)
def _load_static_loot_cache(
    database_path: str, modified_ns: int, file_size: int
) -> StaticLootCache:
    """Load every kill-path table exactly once per database revision."""

    path = Path(database_path)
    database = _open_read_only(path)
    try:
        item_rows = tuple(
            dict(row)
            for row in database.execute(
                "SELECT * FROM _Template_Item"
            ).fetchall()
        )
        ranks = _load_monster_ranks(database)
        monster_profiles: dict[str, MonsterLootProfile] = {}
        monsters = database.execute(
            """
            SELECT Id, CharLevel, Rank, DropChance, LootStackSize, Loot,
                   HealthEventLootLists, PrivilegedLootDropChance
            FROM _Template_Monster
            """
        ).fetchall()
        for raw_monster in monsters:
            monster = dict(raw_monster)
            rank = ranks.get(str(monster["Rank"]))
            if rank is None:
                continue
            template_id = str(monster["Id"])
            monster_profiles[template_id] = MonsterLootProfile(
                template_id=template_id,
                monster_level=max(1, int(monster["CharLevel"] or 1)),
                rank_id=str(rank["Id"]),
                rank_kind=str(rank["Rank"] or "monster").lower(),
                drop_chance=_probability(monster["DropChance"]),
                loot_stack_size=max(
                    0.0, float(monster["LootStackSize"] or 0.0)
                ),
                loot_list_id=str(monster["Loot"] or rank["Loot"] or ""),
                health_event_loot_lists=str(
                    monster["HealthEventLootLists"]
                    or rank["HealthEventLootLists"]
                    or ""
                ),
                andermant_drop_chance=_probability(
                    monster["PrivilegedLootDropChance"]
                ),
            )
        # Region marks event skins (christmas weapons: e_chr); the legacy
        # build has no such column.
        appearance_region = (
            "Region"
            if "Region" in {
                str(row[1]) for row in database.execute(
                    "PRAGMA table_info(_Template_ItemAppearance)"
                )
            }
            else "'' AS Region"
        )
        appearance_rows = tuple(
            dict(row)
            for row in database.execute(
                f"""
                SELECT Id, AppearanceCollection, MinLevel, MaxLevel, Rarity,
                       {appearance_region}
                FROM _Template_ItemAppearance
                """
            ).fetchall()
        )
        # The legacy build pivots this table: instead of one LevelFactor per
        # LevelDifference row, it carries LevelFactor1..LevelFactor60, one per
        # character level. Collapsing that to the current shape would mean
        # picking a level arbitrarily, so the factors are left empty and the
        # real-currency scaling falls back to its neutral default.
        currency_factors = {}
        if compat.has_columns(
            database, "_Template_RealCurrencyGain", "LevelFactor"
        ):
            currency_factors = {
                int(row["LevelDifference"]): float(row["LevelFactor"])
                for row in database.execute(
                    """
                    SELECT LevelDifference, LevelFactor
                    FROM _Template_RealCurrencyGain
                    """
                ).fetchall()
                if row["LevelFactor"] is not None
            }
        difficulty_stack_factors = {
            int(row["Id"]): (
                max(0.0, float(row["VCStackSize"] or 1.0)),
                max(0.0, float(row["RCStackSize"] or 1.0)),
            )
            for row in database.execute(
                """
                SELECT Id, VCStackSize, RCStackSize
                FROM _Template_DifficultyFactors
                """
            ).fetchall()
        }
        enchantments = {}
        # The legacy build has no enchantment-affinity columns; the helpers
        # below already read an absent value as "no affinity".
        enchantment_columns = compat.projection(
            database,
            "_Template_Enchantment",
            (
                "Id", "SlotType", "ItemCategory", "CharClass",
                "MinItemRarity", "MaxItemRarity", "MinItemLevel",
                "MaxItemLevel", "MaxOriginItemLevel",
                "RequiresEnchantmentAffinity",
                "ProvidesEnchantmentAffinity",
                "ProbabilityWeighting", "BaseEnchantment",
                "MinVCValue", "MaxVCValue",
            ),
        )
        for row in database.execute(
            f"SELECT {enchantment_columns} FROM _Template_Enchantment"
        ):
            definition = LootEnchantmentDefinition(
                template_id=str(row["Id"]),
                slot_types=frozenset(
                    token.casefold() for token in _tokens(row["SlotType"])
                ),
                item_categories=frozenset(
                    token.casefold()
                    for token in _tokens(row["ItemCategory"])
                ),
                char_classes=frozenset(
                    token.casefold() for token in _tokens(row["CharClass"])
                ),
                min_rarity=_rarity_index(row["MinItemRarity"], 0),
                max_rarity=_rarity_index(
                    row["MaxItemRarity"],
                    len(EQUIPMENT_RARITIES) - 1,
                ),
                min_item_level=_positive_level(row["MinItemLevel"], 0),
                max_item_level=_positive_level(row["MaxItemLevel"], 10_000),
                max_origin_item_level=_positive_level(
                    row["MaxOriginItemLevel"], 10_000
                ),
                required_affinities=frozenset(
                    token.casefold()
                    for token in _tokens(
                        row["RequiresEnchantmentAffinity"]
                    )
                ),
                provided_affinities=frozenset(
                    token.casefold()
                    for token in _tokens(
                        row["ProvidesEnchantmentAffinity"]
                    )
                ),
                probability_weight=max(
                    0.0, float(row["ProbabilityWeighting"] or 0.0)
                ),
                base_enchantment=bool(row["BaseEnchantment"]),
                min_vc_value=max(0, int(row["MinVCValue"] or 0)),
                max_vc_value=max(0, int(row["MaxVCValue"] or 0)),
            )
            enchantments[definition.template_id] = definition
        random_enchantments = tuple(
            definition
            for definition in enchantments.values()
            if (
                not definition.base_enchantment
                and definition.probability_weight > 0.0
            )
        )
        # Per-difficulty rarity weighting arrived with the difficulty rework;
        # the legacy build has no table carrying these columns, so kill-path
        # rarity falls back to the caller's default weighting.
        rarity_weights_by_difficulty = {}
        difficulty_rows = ()
        if compat.table_exists(database, "_Template_MapDifficulty"):
            difficulty_rows = database.execute(
                """
                SELECT Id, Uncommon, Magic, Rare, Epic
                FROM _Template_MapDifficulty
                """
            ).fetchall()
        for row in difficulty_rows:
            uncommon = max(0.0, float(row["Uncommon"] or 0.0))
            magic = max(0.0, float(row["Magic"] or 0.0))
            rare = max(0.0, float(row["Rare"] or 0.0))
            epic = max(0.0, float(row["Epic"] or 0.0))
            common = max(0.0, 100.0 - uncommon - magic - rare - epic)
            rarity_weights_by_difficulty[int(row["Id"])] = (
                common,
                uncommon,
                magic,
                rare,
                epic,
            )
        enchantment_tiers = frozenset(
            int(row["Tier"])
            for row in database.execute(
                "SELECT Tier FROM _Template_EnchantmentTiers"
            )
        )
        standard_gems_by_rank: dict[str, list[LootItemDefinition]] = {}
        standard_gem_pattern = re.compile(
            r"^gem_(?:amethyst|cyanit|diamond|onyx|ruby)_([a-s])$",
            re.IGNORECASE,
        )
        zero_value_enchantments = _zero_value_enchantment_ids(database)
        for row in item_rows:
            match = standard_gem_pattern.fullmatch(str(row.get("Id") or ""))
            if (
                match is None
                or str(row.get("ItemCategory") or "").casefold()
                != "equipmentgem"
                or int(row.get("Obsolete") or 0) != 0
                or int(row.get("OnlyActiveDuringEvent") or 0) != 0
                or bool(str(row.get("ActiveEvents") or ""))
            ):
                continue
            # The ladder's unreleased top ranks are authored as ordinary items
            # whose base enchantment grants nothing.  Dropping one shows the
            # player a gem reading ``+0``, so those ranks never enter the pool.
            base_enchantments = _resource_ids(row.get("BaseEnchantments"))
            if base_enchantments and all(
                enchantment_id in zero_value_enchantments
                for enchantment_id in base_enchantments
            ):
                continue
            standard_gems_by_rank.setdefault(
                match.group(1).casefold(), []
            ).append(
                LootItemDefinition(
                    template_id=str(row["Id"]),
                    item_category="EquipmentGem",
                    auto_pick_up=str(row.get("AutoPickUp") or "None"),
                    min_socket_count="0",
                    max_socket_count="0",
                    appearance_collection="",
                )
            )
    finally:
        database.close()
    return StaticLootCache(
        database_path=path,
        database_revision=(int(modified_ns), int(file_size)),
        item_rows=item_rows,
        items_by_id={str(row["Id"]): row for row in item_rows},
        monster_profiles=monster_profiles,
        appearance_rows=appearance_rows,
        currency_factors=currency_factors,
        difficulty_stack_factors=difficulty_stack_factors,
        enchantments_by_id=enchantments,
        random_enchantments=random_enchantments,
        rarity_weights_by_difficulty=rarity_weights_by_difficulty,
        enchantment_tiers=enchantment_tiers,
        standard_gems_by_rank={
            rank: tuple(sorted(values, key=lambda value: value.template_id))
            for rank, values in standard_gems_by_rank.items()
        },
    )


def prime_world_loot_cache(
    static_database_path: str | Path,
) -> StaticLootCache:
    """Return the process cache, invalidated when the DB file changes."""

    path = _resolved_loot_database_path(str(static_database_path))
    stat = resident_content.stat(path)
    return _load_static_loot_cache(
        str(path), int(stat.st_mtime_ns), int(stat.st_size)
    )


@lru_cache(maxsize=8)
def _resolved_loot_database_path(database_path: str) -> Path:
    """Canonicalize the immutable game DB path once per process.

    Revision checks remain in ``prime_world_loot_cache``. Only the expensive
    Windows path resolution is cached, which keeps monster-death loot work
    from contending with projectile and monster simulation callbacks.
    """

    return resident_content.resolved(database_path)


def load_monster_loot_profile(
    static_database_path: str | Path, template_id: str
) -> MonsterLootProfile:
    """Resolve monster overrides and inherited rank loot identifiers."""

    try:
        return prime_world_loot_cache(
            static_database_path
        ).monster_profiles[str(template_id)]
    except KeyError as exc:
        raise ValueError(
            f"unknown monster template {template_id!r}"
        ) from exc


def _threshold_value(specification: str, level: int) -> int:
    result = 0
    for raw_entry in str(specification or "0").split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        threshold, separator, raw_value = entry.partition(":")
        try:
            if separator:
                if level >= int(threshold):
                    result = int(raw_value)
            else:
                result = int(entry)
        except ValueError as exc:
            raise ValueError(f"invalid level threshold {entry!r}") from exc
    return max(0, result)


def _context_item_level(
    database: StaticLootCache,
    definition: LootItemDefinition,
    context_level: int,
) -> int:
    '''Resolve database-owned level scaling for generated equipment.'''

    desired = max(1, int(context_level))
    if not definition.slot_types:
        return desired
    row = database.items_by_id.get(definition.template_id, {})
    minimum = max(1, int(row.get('MinDropLevel') or 1))
    maximum = max(minimum, int(row.get('MaxDropLevel') or desired))
    if str(row.get('ItemLevelScalingType') or '').casefold() == 'noscaling':
        desired = max(1, int(row.get('RequiredLevel') or desired))
    return min(maximum, max(minimum, desired))


def _loot_cache_key(
    database: StaticLootCache,
) -> tuple[str, int, int]:
    """Return the immutable database identity used by query caches."""

    modified_ns, file_size = database.database_revision
    return str(database.database_path), int(modified_ns), int(file_size)


def _eligible_equipment_uncached(
    database: StaticLootCache, class_name: str, item_level: int
) -> tuple[LootItemDefinition, ...]:
    class_prefix = f"{class_name}_base_"
    rows = sorted(
        (
            row for row in database.item_rows
            if str(row.get("IconConfiguration") or "")
            == "item_equipment_base"
            and bool(str(row.get("SlotType") or ""))
            and str(row.get("DefaultRarity") or "") == "Common"
            and str(row.get("Rank") or "") == "None"
            and int(row.get("Obsolete") or 0) == 0
            and int(row.get("OnlyActiveDuringEvent") or 0) == 0
            and not str(row.get("ActiveEvents") or "")
            and row.get("MinDropLevel") is not None
            and int(row["MinDropLevel"]) <= item_level
            and row.get("MaxDropLevel") is not None
            and int(row["MaxDropLevel"]) >= item_level
            and str(row.get("CharClass") or "") in {"", str(class_name)}
            and (
                str(row.get("Id") or "").startswith("all_base_")
                or str(row.get("Id") or "").startswith(class_prefix)
            )
            and not str(row.get("Id") or "").endswith("_crafted")
        ),
        key=lambda row: str(row["Id"]),
    )
    return tuple(
        LootItemDefinition(
            template_id=str(row["Id"]),
            item_category=str(row["ItemCategory"] or ""),
            auto_pick_up=str(row["AutoPickUp"] or "None"),
            min_socket_count=str(row["MinSocketCount"] or "0"),
            max_socket_count=str(row["MaxSocketCount"] or "0"),
            appearance_collection=str(row["AppearanceCollection"] or ""),
            slot_types=_tokens(row["SlotType"]),
            char_classes=_tokens(row["CharClass"]),
            base_enchantments=_resource_ids(row["BaseEnchantments"]),
            enchantment_specification=str(row["Enchantments"] or ""),
        )
        for row in rows
    )


@lru_cache(maxsize=1024)
def _eligible_equipment_cached(
    database_path: str,
    modified_ns: int,
    file_size: int,
    class_name: str,
    item_level: int,
) -> tuple[LootItemDefinition, ...]:
    database = _load_static_loot_cache(
        database_path, int(modified_ns), int(file_size)
    )
    return _eligible_equipment_uncached(
        database, str(class_name), int(item_level)
    )


def _eligible_equipment(
    database: StaticLootCache, class_name: str, item_level: int
) -> tuple[LootItemDefinition, ...]:
    return _eligible_equipment_cached(
        *_loot_cache_key(database), str(class_name), int(item_level)
    )


def _eligible_fixed_rarity_equipment_uncached(
    database: StaticLootCache,
    class_name: str,
    item_level: int,
    rarity: str,
) -> tuple[LootItemDefinition, ...]:
    """Return client-defined Unique/Set/Mythic templates for test drops."""

    if rarity not in FIXED_EQUIPMENT_RARITIES:
        raise ValueError(f"{rarity!r} is not a fixed equipment rarity")
    rows = sorted(
        (
            row for row in database.item_rows
            if bool(str(row.get("SlotType") or ""))
            and str(row.get("DefaultRarity") or "") == rarity
            and str(row.get("IconConfiguration") or "") in {
                "item_equipment_unique",
                "item_equipment_set",
                "item_equipment_mythic",
            }
            and str(row.get("Rank") or "") == "None"
            and int(row.get("Obsolete") or 0) == 0
            and int(row.get("OnlyActiveDuringEvent") or 0) == 0
            and not str(row.get("ActiveEvents") or "")
            and row.get("MinDropLevel") is not None
            and int(row["MinDropLevel"]) <= item_level
            and row.get("MaxDropLevel") is not None
            and int(row["MaxDropLevel"]) >= item_level
            and str(row.get("CharClass") or "") in {"", str(class_name)}
        ),
        key=lambda row: str(row["Id"]),
    )
    return tuple(
        LootItemDefinition(
            template_id=str(row["Id"]),
            item_category=str(row["ItemCategory"] or ""),
            auto_pick_up=str(row["AutoPickUp"] or "None"),
            min_socket_count=str(row["MinSocketCount"] or "0"),
            max_socket_count=str(row["MaxSocketCount"] or "0"),
            appearance_collection=str(row["AppearanceCollection"] or ""),
            slot_types=_tokens(row["SlotType"]),
            char_classes=_tokens(row["CharClass"]),
            base_enchantments=_resource_ids(row["BaseEnchantments"]),
            enchantment_specification=str(row["Enchantments"] or ""),
        )
        for row in rows
    )


@lru_cache(maxsize=2048)
def _eligible_fixed_rarity_equipment_cached(
    database_path: str,
    modified_ns: int,
    file_size: int,
    class_name: str,
    item_level: int,
    rarity: str,
) -> tuple[LootItemDefinition, ...]:
    database = _load_static_loot_cache(
        database_path, int(modified_ns), int(file_size)
    )
    return _eligible_fixed_rarity_equipment_uncached(
        database, str(class_name), int(item_level), str(rarity)
    )


def _eligible_fixed_rarity_equipment(
    database: StaticLootCache,
    class_name: str,
    item_level: int,
    rarity: str,
) -> tuple[LootItemDefinition, ...]:
    return _eligible_fixed_rarity_equipment_cached(
        *_loot_cache_key(database),
        str(class_name),
        int(item_level),
        str(rarity),
    )


def _appearance_candidates_uncached(
    database: StaticLootCache,
    collection: str,
    item_level: int,
    *,
    rarity: str = "Common",
) -> tuple[str, ...]:
    if not collection:
        return ()
    rows = sorted(
        (
            row for row in database.appearance_rows
            if str(row.get("AppearanceCollection") or "") == collection
            and int(row.get("MinLevel") or 0) <= item_level
            and int(row.get("MaxLevel") or 0) >= item_level
            and (
                not str(row.get("Rarity") or "")
                or rarity in str(row.get("Rarity") or "")
            )
        ),
        key=lambda row: str(row["Id"]),
    )
    # Numeric appearances are the ordinary base set.  The same collection
    # also contains event/PW/ghost skins, which a generic MAIN loot roll must
    # not silently grant.
    return tuple(
        str(row["Id"]) for row in rows
        if re.fullmatch(r".*_\d{2}", str(row["Id"]))
    )


@lru_cache(maxsize=4096)
def _appearance_candidates_cached(
    database_path: str,
    modified_ns: int,
    file_size: int,
    collection: str,
    item_level: int,
    rarity: str,
) -> tuple[str, ...]:
    database = _load_static_loot_cache(
        database_path, int(modified_ns), int(file_size)
    )
    return _appearance_candidates_uncached(
        database,
        str(collection),
        int(item_level),
        rarity=str(rarity),
    )


def _appearance(
    database: StaticLootCache,
    collection: str,
    item_level: int,
    rng: random.Random,
    *,
    rarity: str = "Common",
) -> str:
    if not collection:
        return ""
    candidates = _appearance_candidates_cached(
        *_loot_cache_key(database),
        str(collection),
        int(item_level),
        str(rarity),
    )
    return str(rng.choice(candidates)) if candidates else ""


def _roll_equipment_rarity(
    database: StaticLootCache,
    map_difficulty: int,
    rng: random.Random,
) -> int:
    """Roll the five ordinary rarities from MapDifficulty percentages."""

    weights = database.rarity_weights_by_difficulty.get(
        int(map_difficulty),
        database.rarity_weights_by_difficulty.get(
            0, (100.0, 0.0, 0.0, 0.0, 0.0)
        ),
    )
    if sum(weights) <= 0.0:
        return 0
    return int(rng.choices(range(len(weights)), weights=weights, k=1)[0])


def roll_equipment_reward(
    static_database_path: str | Path,
    *,
    class_name: str,
    item_level: int,
    rarity: str,
    slot_types: tuple[str, ...] = (),
    rng: random.Random | None = None,
    appearance_region: str = "",
) -> EquipmentRewardRoll:
    """Roll one authored equipment template for a chest or similar reward.

    Ordinary rarity boxes use the same common base templates as monster loot
    and override the concrete item's rarity. Unique/set/mythic rewards keep
    their authored templates. Optional slot filters are matched against the
    database ``SlotType`` column rather than inferred from item names.

    ``appearance_region`` asks for an event skin instead of a base one: only
    templates whose appearance collection has a skin of that Region (the
    christmas ``*_christmas`` weapon skins are region ``e_chr``) are rolled,
    and the item wears that skin.
    """

    if rarity not in EQUIPMENT_RARITIES:
        raise ValueError(f"unknown equipment rarity {rarity!r}")
    generator = rng if rng is not None else random.Random()
    database = prime_world_loot_cache(static_database_path)
    if rarity in ROLLED_EQUIPMENT_RARITIES:
        candidates = _eligible_equipment(
            database, str(class_name), max(1, int(item_level))
        )
    else:
        candidates = _eligible_fixed_rarity_equipment(
            database,
            str(class_name),
            max(1, int(item_level)),
            rarity,
        )
    wanted_slots = {
        str(value).casefold() for value in slot_types if str(value)
    }
    if wanted_slots:
        candidates = tuple(
            candidate
            for candidate in candidates
            if wanted_slots.intersection(
                slot.casefold() for slot in candidate.slot_types
            )
        )
    region = str(appearance_region).strip().casefold()
    if region:
        candidates = tuple(
            candidate for candidate in candidates
            if _region_appearances(
                database, candidate.appearance_collection,
                max(1, int(item_level)), rarity, region,
            )
        )
    if not candidates:
        suffix = (
            f" in slots {tuple(slot_types)!r}" if wanted_slots else ""
        ) + (f" with a {region!r} skin" if region else "")
        raise ValueError(
            f"no eligible {rarity} equipment for {class_name!r} at "
            f"level {item_level}{suffix}"
        )
    definition = generator.choice(candidates)
    resolved_level = _context_item_level(
        database, definition, max(1, int(item_level))
    )
    if region:
        skins = _region_appearances(
            database, definition.appearance_collection, resolved_level,
            rarity, region,
        ) or _region_appearances(
            database, definition.appearance_collection,
            max(1, int(item_level)), rarity, region,
        )
        appearance = generator.choice(skins)
    else:
        appearance = _appearance(
            database,
            definition.appearance_collection,
            resolved_level,
            generator,
            rarity=rarity,
        )
    return EquipmentRewardRoll(
        definition.template_id,
        resolved_level,
        EQUIPMENT_RARITIES.index(rarity),
        appearance,
    )


def _region_appearances(
    database: StaticLootCache,
    collection: str,
    item_level: int,
    rarity: str,
    region: str,
) -> tuple[str, ...]:
    """Event skins of ``collection`` belonging to ``region``.

    Base skins (``*_NN``) list the regions they drop in too, so an event skin
    is a non-numbered one tagged with the region (``*_christmas``: e_chr).
    """

    if not collection:
        return ()
    return tuple(sorted(
        str(row["Id"]) for row in database.appearance_rows
        if str(row.get("AppearanceCollection") or "") == collection
        and not re.fullmatch(r".*_\d{2}", str(row["Id"]))
        and int(row.get("MinLevel") or 0) <= item_level
        and int(row.get("MaxLevel") or 0) >= item_level
        and (
            not str(row.get("Rarity") or "")
            or rarity in str(row.get("Rarity") or "")
        )
        and region in {
            token.strip().casefold()
            for token in str(row.get("Region") or "").split(";")
        }
    ))


def _provided_affinities(
    database: StaticLootCache,
    definition: LootItemDefinition,
) -> frozenset[str]:
    result = set()
    for enchantment_id in definition.base_enchantments:
        enchantment = database.enchantments_by_id.get(enchantment_id)
        if enchantment is not None:
            result.update(enchantment.provided_affinities)
    # The old generic weapon rows intentionally have no slot/category
    # columns and use the universal weapon affinity instead.
    if definition.item_category.casefold() == "weapon":
        result.add("general")
    return frozenset(result)


def _generic_weapon_candidate_matches(
    enchantment: LootEnchantmentDefinition,
    definition: LootItemDefinition,
) -> bool:
    if (
        enchantment.slot_types
        or enchantment.item_categories
        or enchantment.required_affinities != frozenset(("general",))
    ):
        return True
    if definition.item_category.casefold() != "weapon":
        return False
    identifier = enchantment.template_id.casefold()
    slots = {slot.casefold() for slot in definition.slot_types}
    two_handed = {"righthandslot", "lefthandslot"}.issubset(slots)
    if "_1h_weapon" in identifier:
        return not two_handed
    if "_2h_weapon" in identifier:
        return two_handed
    return True


def _compatible_random_enchantments_uncached(
    database: StaticLootCache,
    definition: LootItemDefinition,
    *,
    class_name: str,
    item_level: int,
    rarity: int,
) -> tuple[LootEnchantmentDefinition, ...]:
    slots = {slot.casefold() for slot in definition.slot_types}
    item_category = definition.item_category.casefold()
    character_class = str(class_name).casefold()
    affinities = _provided_affinities(database, definition)
    result = []
    for enchantment in database.random_enchantments:
        if enchantment.slot_types and slots.isdisjoint(
            enchantment.slot_types
        ):
            continue
        if (
            enchantment.item_categories
            and item_category not in enchantment.item_categories
        ):
            continue
        if (
            enchantment.char_classes
            and character_class not in enchantment.char_classes
        ):
            continue
        if not (
            enchantment.min_rarity
            <= int(rarity)
            <= enchantment.max_rarity
        ):
            continue
        if not (
            enchantment.min_item_level
            <= int(item_level)
            <= enchantment.max_item_level
        ):
            continue
        if (
            enchantment.max_origin_item_level > 0
            and int(item_level) > enchantment.max_origin_item_level
        ):
            continue
        if (
            enchantment.required_affinities
            and affinities.isdisjoint(enchantment.required_affinities)
        ):
            continue
        if not _generic_weapon_candidate_matches(
            enchantment, definition
        ):
            continue
        result.append(enchantment)
    return tuple(result)


@lru_cache(maxsize=8192)
def _compatible_random_enchantments_cached(
    database_path: str,
    modified_ns: int,
    file_size: int,
    definition: LootItemDefinition,
    class_name: str,
    item_level: int,
    rarity: int,
) -> tuple[LootEnchantmentDefinition, ...]:
    database = _load_static_loot_cache(
        database_path, int(modified_ns), int(file_size)
    )
    return _compatible_random_enchantments_uncached(
        database,
        definition,
        class_name=str(class_name),
        item_level=int(item_level),
        rarity=int(rarity),
    )


def _compatible_random_enchantments(
    database: StaticLootCache,
    definition: LootItemDefinition,
    *,
    class_name: str,
    item_level: int,
    rarity: int,
) -> tuple[LootEnchantmentDefinition, ...]:
    return _compatible_random_enchantments_cached(
        *_loot_cache_key(database),
        definition,
        str(class_name),
        int(item_level),
        int(rarity),
    )


def _fixed_enchantment_ids(
    database: StaticLootCache,
    definition: LootItemDefinition,
    rng: random.Random,
) -> tuple[str, ...]:
    """Resolve the guaranteed/weighted Item.Enchantments expression.

    Plain entries are the modern fixed composition and may intentionally
    repeat. Pipe-delimited entries are the legacy weighted pool used by
    templates that do not carry a plain composition.
    """

    guaranteed = []
    weighted = []
    weights = []
    for entry in _resource_ids(definition.enchantment_specification):
        parts = tuple(part.strip() for part in entry.split("|"))
        template_id = parts[0]
        if template_id not in database.enchantments_by_id:
            continue
        if len(parts) == 1:
            guaranteed.append(template_id)
            continue
        try:
            weight = max(0.0, float(parts[1]))
        except (TypeError, ValueError):
            weight = 0.0
        if weight > 0.0:
            weighted.append(template_id)
            weights.append(weight)
    if guaranteed:
        return tuple(guaranteed[:4])
    if not weighted:
        return ()
    return tuple(rng.choices(weighted, weights=weights, k=4))


def _is_equipment_row(row: Mapping[str, object] | None) -> bool:
    """Report whether an item template is enchantable equipment.

    Accepts the icon the loot generator selects on, plus any slotted template
    whose ItemCategory is an equipment category, so equipment that never
    dropped is recognized too.
    """

    if row is None:
        return False
    if str(row.get("IconConfiguration") or "").startswith("item_equipment_"):
        return True
    return (
        bool(str(row.get("SlotType") or ""))
        and str(row.get("ItemCategory") or "") in EQUIPMENT_ITEM_CATEGORIES
    )


def _roll_item_enchantments(
    database: StaticLootCache,
    definition: LootItemDefinition,
    *,
    class_name: str,
    item_level: int,
    rarity: int,
    enchantment_tier: int,
    rng: random.Random,
) -> list[ItemEnchantmentInfo]:
    if rarity < len(ROLLED_EQUIPMENT_RARITIES):
        count = max(0, int(rarity))
        candidates = _compatible_random_enchantments(
            database,
            definition,
            class_name=class_name,
            item_level=item_level,
            rarity=rarity,
        )
        if count and not candidates:
            if (
                definition.item_category
                in ENCHANTMENT_FREE_TRINKET_CATEGORIES
            ):
                return []
            raise ValueError(
                f"no random enchantments are compatible with "
                f"{definition.template_id!r} at level {item_level}, "
                f"rarity {EQUIPMENT_RARITIES[rarity]}"
            )
        template_ids = (
            tuple(
                rng.choices(
                    candidates,
                    weights=tuple(
                        candidate.probability_weight
                        for candidate in candidates
                    ),
                    k=count,
                )
            )
            if count
            else ()
        )
        resolved_ids = tuple(
            definition.template_id for definition in template_ids
        )
    else:
        resolved_ids = _fixed_enchantment_ids(
            database, definition, rng
        )

    tier = (
        int(enchantment_tier)
        if int(enchantment_tier) in database.enchantment_tiers
        else 0
    )
    max_roll = definition.item_category in MERCENARY_EQUIPMENT_CATEGORIES
    enchantments = []
    for template_id in resolved_ids:
        random_roll = max(0.0, min(1.0, float(rng.random())))
        enchantments.append(ItemEnchantmentInfo(
            value=1.0 if max_roll else random_roll,
            kind=int(rarity),
            field_08=tier,
            field_0c=max(1, int(item_level)),
            template_id=template_id,
        ))
    return enchantments


def backfill_missing_equipment_enchantments(
    static_database_path: str | Path,
    info: InventoryInfo,
    *,
    class_name: str,
    map_difficulty: int = 0,
    rng: random.Random | None = None,
) -> tuple[InventoryInfo, tuple[int, ...]]:
    """Add concrete rolls to legacy equipped items that have none.

    Existing enchantments, item ids, socket contents and every allocation are
    preserved. The default seed is stable for the same inventory snapshot, so
    a migration interrupted before persistence produces the same rolls.
    """

    database = prime_world_loot_cache(static_database_path)
    inventory = copy.deepcopy(info)
    equipped_ids = {
        int(item_id) for item_id in inventory.equipment_slots
    }
    if rng is None:
        seed_parts = [
            "dso-item-enchantment-backfill-v1",
            str(class_name).casefold(),
        ]
        seed_parts.extend(
            (
                f"{item.template_id}:{int(item.field_18)}:"
                f"{int(item.item_level)}:{int(item.field_08)}"
            )
            for item in sorted(
                inventory.items, key=lambda value: int(value.item_id)
            )
            if int(item.item_id) in equipped_ids
        )
        rng = random.Random("|".join(seed_parts))

    changed = []
    for item in sorted(
        inventory.items, key=lambda value: int(value.item_id)
    ):
        if (
            int(item.item_id) not in equipped_ids
            or item.enchantments
        ):
            continue
        row = database.items_by_id.get(str(item.template_id))
        if not _is_equipment_row(row):
            continue
        definition = LootItemDefinition(
            template_id=str(row["Id"]),
            item_category=str(row.get("ItemCategory") or ""),
            auto_pick_up=str(row.get("AutoPickUp") or "None"),
            min_socket_count=str(row.get("MinSocketCount") or "0"),
            max_socket_count=str(row.get("MaxSocketCount") or "0"),
            appearance_collection=str(
                row.get("AppearanceCollection") or ""
            ),
            slot_types=_tokens(row.get("SlotType")),
            char_classes=_tokens(row.get("CharClass")),
            base_enchantments=_resource_ids(
                row.get("BaseEnchantments")
            ),
            enchantment_specification=str(
                row.get("Enchantments") or ""
            ),
        )
        item.enchantments = _roll_item_enchantments(
            database,
            definition,
            class_name=class_name,
            item_level=max(1, int(item.item_level)),
            rarity=max(0, min(7, int(item.field_08))),
            enchantment_tier=map_difficulty,
            rng=rng,
        )
        if item.enchantments:
            changed.append(int(item.item_id))
    return inventory, tuple(changed)


def roll_new_equipment_enchantments(
    static_database_path: str | Path,
    info: InventoryInfo,
    item_ids: tuple[int, ...] | list[int] | set[int],
    *,
    class_name: str,
    map_difficulty: int = 0,
    rng: random.Random | None = None,
) -> tuple[int, ...]:
    """Roll the same compatible stats for newly purchased equipment as loot.

    Unique/set/mythic templates keep their database-authored fixed
    enchantment composition. Ordinary rarities use the cached slot/category,
    class, level, rarity and affinity filters. Existing concrete rolls are
    always preserved.
    """

    database = prime_world_loot_cache(static_database_path)
    wanted_ids = {int(value) for value in item_ids}
    if rng is None:
        rng = random.Random()
    changed = []
    for item in info.items:
        if int(item.item_id) not in wanted_ids:
            continue
        row = database.items_by_id.get(str(item.template_id))
        if not _is_equipment_row(row):
            continue
        is_mercenary_equipment = str(
            row.get("ItemCategory") or ""
        ) in MERCENARY_EQUIPMENT_CATEGORIES
        base_values_changed = False
        if is_mercenary_equipment:
            if int(item.field_38) != 16:
                item.field_38 = 16
                base_values_changed = True
            if item.enchantments:
                maxed_enchantments = [
                    replace(enchantment, value=1.0)
                    for enchantment in item.enchantments
                ]
                if maxed_enchantments != item.enchantments:
                    item.enchantments = maxed_enchantments
                    base_values_changed = True
                if base_values_changed:
                    changed.append(int(item.item_id))
                continue
        elif item.enchantments:
            continue
        definition = LootItemDefinition(
            template_id=str(row["Id"]),
            item_category=str(row.get("ItemCategory") or ""),
            auto_pick_up=str(row.get("AutoPickUp") or "None"),
            min_socket_count=str(row.get("MinSocketCount") or "0"),
            max_socket_count=str(row.get("MaxSocketCount") or "0"),
            appearance_collection=str(
                row.get("AppearanceCollection") or ""
            ),
            slot_types=_tokens(row.get("SlotType")),
            char_classes=_tokens(row.get("CharClass")),
            base_enchantments=_resource_ids(row.get("BaseEnchantments")),
            enchantment_specification=str(row.get("Enchantments") or ""),
        )
        item.enchantments = _roll_item_enchantments(
            database,
            definition,
            class_name=str(class_name),
            item_level=max(1, int(item.item_level)),
            rarity=max(0, min(7, int(item.field_08))),
            enchantment_tier=int(map_difficulty),
            rng=rng,
        )
        if item.enchantments or base_values_changed:
            changed.append(int(item.item_id))
    return tuple(changed)


def _currency_factor(
    database: StaticLootCache, monster_level: int, player_level: int
) -> float:
    difference = int(monster_level) - int(player_level)
    if not database.currency_factors:
        return 1.0
    difference = max(
        min(database.currency_factors),
        min(max(database.currency_factors), difference),
    )
    return max(
        0.0, float(database.currency_factors.get(difference, 1.0))
    )


def _difficulty_stack_multiplier(
    database: StaticLootCache,
    template_id: str,
    map_difficulty: int,
    *,
    event_progress: bool = False,
) -> float:
    """Resolve the DB-authored VC/RC stack curve for one dropped item.

    GroupTags selects which curve an item uses. Event progress predates those
    tags in a few client rows (notably Desert of Essences), so the event
    catalog's explicit collectible path uses the VC curve as its fallback.
    Items without either tag intentionally remain unscaled; this preserves
    authored variants such as money_rc_no_scaling.
    """

    factors = database.difficulty_stack_factors
    if not factors:
        return 1.0
    difficulty = max(min(factors), min(max(factors), int(map_difficulty)))
    vc_factor, rc_factor = factors.get(difficulty, (1.0, 1.0))
    row = database.items_by_id.get(str(template_id), {})
    tags = {
        token.casefold() for token in _tokens(row.get("GroupTags"))
    }
    if "applystacksizedifficultyfactorvc" in tags or event_progress:
        return max(0.0, float(vc_factor))
    if "applystacksizedifficultyfactorrc" in tags:
        return max(0.0, float(rc_factor))
    return 1.0


def _difficulty_scaled_stack_amount(
    database: StaticLootCache,
    template_id: str,
    amount: int,
    map_difficulty: int,
    *,
    event_progress: bool = False,
) -> int:
    multiplier = _difficulty_stack_multiplier(
        database,
        template_id,
        map_difficulty,
        event_progress=event_progress,
    )
    return max(
        1,
        int(math.floor(max(1, int(amount)) * multiplier + 0.5)),
    )


def _health_template_chance(loot_list_id: str) -> tuple[str, float]:
    lowered = loot_list_id.lower()
    template_id = (
        "healthglobe_singleTarget_full"
        if "full" in lowered
        else "healthglobe_singleTarget"
    )
    if "always" in lowered:
        return template_id, 1.0
    suffix = lowered.rsplit("_", 1)[-1]
    try:
        probability = int(suffix) / 100.0
    except ValueError:
        probability = 0.25
    return template_id, max(0.0, min(1.0, probability))


def _health_drops(
    profile: MonsterLootProfile,
    rng: random.Random,
    *,
    force_drops: bool = False,
) -> list[str]:
    result = []
    for raw_entry in profile.health_event_loot_lists.split(";"):
        entry = raw_entry.strip()
        if not entry:
            continue
        _threshold, separator, loot_list_id = entry.partition(",")
        if not separator or not loot_list_id:
            continue
        template_id, chance = _health_template_chance(loot_list_id.strip())
        if force_drops or rng.random() <= chance:
            result.append(template_id)
    return result


def _rank_equipment_count(profile: MonsterLootProfile) -> int:
    if profile.loot_stack_size <= 0.0:
        return 0
    if profile.rank_kind in {"worldboss", "boss"}:
        base = 3
    elif profile.rank_kind in {"guardian", "champion"}:
        base = 2
    else:
        base = 1
    return max(1, round(base * profile.loot_stack_size))


def _boss_currency_instance_count(profile: MonsterLootProfile) -> int:
    """Return one chance roll's visual stack count.

    Boss loot uses several ground objects for presentation. This must not be
    implemented as several probability rolls: difficulty and boss rank change
    the awarded quantity, never the chance that the andermant roll succeeds.
    """

    return 3 if profile.rank_kind in {"worldboss", "boss"} else 1


def _scatter_positions(
    origin: tuple[float, float, float],
    count: int,
    rng: random.Random,
    position_projector: (
        Callable[
            [tuple[float, float, float]],
            tuple[float, float, float],
        ]
        | None
    ) = None,
) -> tuple[tuple[float, float, float], ...]:
    if count <= 0:
        return ()
    positions = []
    for index in range(count):
        angle = 2.0 * math.pi * index / count + rng.uniform(-0.18, 0.18)
        radius = 0.65 + 0.22 * (index % 3)
        candidate = (
            float(origin[0]) + math.cos(angle) * radius,
            float(origin[1]),
            float(origin[2]) + math.sin(angle) * radius,
        )
        if position_projector is not None:
            candidate = tuple(
                float(value) for value in position_projector(candidate)
            )
        positions.append(candidate)
    return tuple(positions)


def generate_monster_loot(
    static_database_path: str | Path,
    template_id: str,
    *,
    class_name: str,
    player_level: int,
    position: tuple[float, float, float],
    next_item_id: int,
    monster_level: int | None = None,
    rng: random.Random | None = None,
    force_drops: bool = False,
    wisdom_eligible: bool = False,
    map_difficulty: int = 0,
    extra_boss_gems: int = 0,
    guaranteed_gem_templates: tuple[str, ...] = (),
    additional_instant_consumables: tuple[tuple[str, int], ...] = (),
    position_projector: (
        Callable[
            [tuple[float, float, float]],
            tuple[float, float, float],
        ]
        | None
    ) = None,
    additional_event_loot: tuple[tuple[str, int], ...] = (),
) -> WorldLootGeneration:
    """Generate editable NewItem state for one lethal monster transition.

    ``monster_level`` is the authoritative runtime level serialized on the
    killed monster. It intentionally overrides the template's authored
    ``CharLevel`` on scaling maps. The resulting item level drives template
    eligibility, database level caps, sockets, base-stat scaling in the
    client, and every concrete enchantment's origin level.

    ``force_drops`` bypasses probability rolls and replaces normal equipment
    cardinality with one item of every client rarity. It never changes the
    runtime monster level used by those items.

    ``extra_boss_gems`` implements Jewel of Gem Fortune. Its five database
    status resources are localized as exactly one through five additional
    gems. The ordinary gem pool is pre-indexed in ``StaticLootCache`` and is
    consulted only for boss/world-boss profiles, so this adds no kill-path DB
    transaction or full item-table scan.
    """

    generator = rng if rng is not None else random.Random()
    database = prime_world_loot_cache(static_database_path)
    try:
        profile = database.monster_profiles[str(template_id)]
    except KeyError as exc:
        raise ValueError(
            f"unknown monster template {template_id!r}"
        ) from exc
    if monster_level is not None:
        profile = replace(
            profile,
            monster_level=max(1, int(monster_level)),
        )
    equipment_item_level = profile.monster_level
    try:
        specifications: list[
            tuple[LootItemDefinition, int, int, str, int]
        ] = []

        for health_template in _health_drops(
            profile, generator, force_drops=force_drops
        ):
            specifications.append(
                (
                    LootItemDefinition(
                        health_template,
                        "InstantConsumable",
                        "All",
                        "0",
                        "0",
                        "",
                    ),
                    1,
                    1,
                    "",
                    0,
                )
            )

        # Explicit empty-list overrides suppress the inherited generic list.
        main_enabled = bool(profile.loot_list_id) and not profile.loot_list_id.endswith(
            "_empty"
        )
        if force_drops or (
            main_enabled and generator.random() <= profile.drop_chance
        ):
            gold_amount = max(
                1,
                round(profile.monster_level * generator.uniform(2.5, 3.5)),
            )
            specifications.append(
                (
                    LootItemDefinition(
                        "money_vc", "Currency", "All", "0", "0", ""
                    ),
                    gold_amount,
                    1,
                    "",
                    0,
                )
            )
            candidates = list(
                _eligible_equipment(
                    database, class_name, equipment_item_level
                )
            )
            generator.shuffle(candidates)
            if force_drops:
                if len(candidates) < len(ROLLED_EQUIPMENT_RARITIES):
                    raise ValueError(
                        f"only {len(candidates)} base equipment templates are "
                        f"eligible for {class_name!r} at level "
                        f"{equipment_item_level}"
                    )
                forced_equipment: list[
                    tuple[LootItemDefinition, str]
                ] = list(
                    zip(
                        candidates[: len(ROLLED_EQUIPMENT_RARITIES)],
                        ROLLED_EQUIPMENT_RARITIES,
                        strict=True,
                    )
                )
                for rarity in FIXED_EQUIPMENT_RARITIES:
                    fixed_candidates = list(
                        _eligible_fixed_rarity_equipment(
                            database,
                            class_name,
                            equipment_item_level,
                            rarity,
                        )
                    )
                    if not fixed_candidates:
                        raise ValueError(
                            f"no eligible {rarity} equipment template for "
                            f"{class_name!r} at level {equipment_item_level}"
                        )
                    forced_equipment.append(
                        (generator.choice(fixed_candidates), rarity)
                    )
                for definition, rarity in forced_equipment:
                    specifications.append(
                        (
                            definition,
                            1,
                            equipment_item_level,
                            _appearance(
                                database,
                                definition.appearance_collection,
                                equipment_item_level,
                                generator,
                                rarity=rarity,
                            ),
                            EQUIPMENT_RARITIES.index(rarity),
                        )
                    )
            else:
                chosen: list[LootItemDefinition] = []
                categories: set[str] = set()
                for candidate in candidates:
                    if candidate.item_category in categories:
                        continue
                    chosen.append(candidate)
                    categories.add(candidate.item_category)
                    if len(chosen) >= _rank_equipment_count(profile):
                        break
                for definition in chosen:
                    rarity = _roll_equipment_rarity(
                        database, map_difficulty, generator
                    )
                    specifications.append(
                        (
                            definition,
                            1,
                            equipment_item_level,
                            _appearance(
                                database,
                                definition.appearance_collection,
                                equipment_item_level,
                                generator,
                                rarity=EQUIPMENT_RARITIES[rarity],
                            ),
                            rarity,
                        )
                    )

        if force_drops or generator.random() <= profile.andermant_drop_chance:
            factor = _currency_factor(
                database, profile.monster_level, int(player_level)
            )
            amount = (
                max(
                    1,
                    round(
                        factor
                        * max(1.0, profile.monster_level / 30.0)
                    ),
                )
                * ANDERMANT_BASE_DROP_MULTIPLIER
            )
            for _ in range(_boss_currency_instance_count(profile)):
                specifications.append(
                    (
                        LootItemDefinition(
                            ANDERMANT_ITEM_TEMPLATE,
                            "Currency",
                            "All",
                            "0",
                            "0",
                            "",
                        ),
                        amount,
                        1,
                        "",
                        0,
                    )
                )

        if wisdom_eligible and (
            force_drops or generator.random() <= BASE_WISDOM_DROP_CHANCE
        ):
            specifications.append(
                (
                    LootItemDefinition(
                        WISDOM_ITEM_TEMPLATE,
                        "Currency",
                        "All",
                        "0",
                        "0",
                        "",
                    ),
                    max(1, profile.monster_level // 25),
                    1,
                    "",
                    0,
                )
            )

        if (
            profile.rank_kind in {"worldboss", "boss"}
            and int(extra_boss_gems) > 0
        ):
            # Gem ranks a..s are the authored upgrade ladder. Select a
            # deterministic level band, then let the loot RNG choose among
            # the five ordinary families at that rank. High-level monsters
            # land past the ladder's released end, so the request is clamped
            # to the best rank that actually grants a stat.
            gem_rank = chr(
                ord("a") + min(18, max(0, (profile.monster_level - 1) // 10))
            )
            gem_pool = database.gems_for_rank(gem_rank)
            if gem_pool:
                for _ in range(max(0, int(extra_boss_gems))):
                    specifications.append(
                        (generator.choice(gem_pool), 1, 1, "", 0)
                    )

        # Encounter rewards bypass normal loot probability and random gem
        # selection. Resolve exact templates so a requested tier never falls
        # back to a different rank, and add one of each outside event scaling.
        for gem_template in dict.fromkeys(guaranteed_gem_templates):
            row = database.items_by_id.get(gem_template)
            if row is None or row.get("ItemCategory") != "EquipmentGem":
                raise ValueError(f"guaranteed gem {gem_template!r} is not an equipment gem")
            specifications.append((
                LootItemDefinition(
                    gem_template, "EquipmentGem",
                    str(row.get("AutoPickUp") or "None"), "0", "0", "",
                ),
                1, 1, "", 0,
            ))

        event_collectible_ids = {
            str(template_id)
            for template_id, _amount in additional_instant_consumables
        }
        # An event map's final boss (rank worldboss: Big Paws, Dragan...) pays
        # out a large stack of progress; minibosses a small one. The map
        # difficulty curve below scales both.
        event_rank_multiplier = (
            EVENT_FINAL_BOSS_PROGRESS_MULTIPLIER
            if profile.rank_kind == "worldboss"
            else 3 if profile.rank_kind == "boss" else 1
        )
        for template_id, amount in additional_instant_consumables:
            row = database.items_by_id.get(str(template_id))
            if row is None:
                raise ValueError(
                    f"event collectible {template_id!r} is not an item"
                )
            # Most collectibles are consumed on pickup; some are stored, like
            # the Rise of Dragan key retail drops for its event's first page,
            # which goes to the inventory and advances progress as well.
            item_category = str(
                row.get("ItemCategory") or "InstantConsumable"
            )
            specifications.append((
                LootItemDefinition(
                    str(template_id),
                    item_category,
                    str(row.get("AutoPickUp") or "All"),
                    "0", "0", "",
                ),
                max(1, int(amount)) * event_rank_multiplier,
                1,
                "",
                0,
            ))

        # Event Loot-list rolls (christmas sugar beets) are ordinary items of
        # any category; the event already sized them per monster rank.
        for template_id, amount in additional_event_loot:
            row = database.items_by_id.get(str(template_id))
            if row is None:
                raise ValueError(f"event loot {template_id!r} is not an item")
            rarity_name = str(row.get("DefaultRarity") or "Common")
            specifications.append((
                LootItemDefinition(
                    str(template_id),
                    str(row.get("ItemCategory") or "General"),
                    str(row.get("AutoPickUp") or "None"),
                    "0", "0", "",
                ),
                max(1, int(amount)),
                1,
                "",
                EQUIPMENT_RARITIES.index(rarity_name)
                if rarity_name in EQUIPMENT_RARITIES else 0,
            ))

        # Apply the database's map-difficulty stack curve in one final pass so
        # every generated Currency item, including future loot-list additions,
        # follows its GroupTags. Event progress uses the same VC curve even
        # for legacy rows that predate the tag.
        specifications = [
            (
                definition,
                _difficulty_scaled_stack_amount(
                    database,
                    definition.template_id,
                    amount,
                    map_difficulty,
                    event_progress=(
                        definition.template_id in event_collectible_ids
                    ),
                )
                if (
                    definition.item_category == "Currency"
                    or definition.template_id in event_collectible_ids
                )
                else amount,
                item_level,
                appearance,
                rarity,
            )
            for definition, amount, item_level, appearance, rarity
            in specifications
        ]

        positions = _scatter_positions(
            position,
            len(specifications),
            generator,
            position_projector,
        )
        now = datetime.now(timezone.utc)
        drops = []
        item_id = int(next_item_id)
        for (
            definition,
            amount,
            item_level,
            appearance,
            rarity,
        ), item_position in zip(
            specifications, positions, strict=True
        ):
            item_level = _context_item_level(
                database, definition, item_level
            )
            socket_count = (
                _threshold_value(definition.min_socket_count, item_level)
                if definition.item_category not in {"Currency", "InstantConsumable"}
                else 0
            )
            item_row = database.items_by_id.get(definition.template_id, {})
            item = ItemInfo(
                item_id=item_id,
                amount=amount,
                field_08=rarity,
                socket_count=socket_count,
                field_18=generator.randrange(1, 0x8000),
                field_28="none",
                item_level=max(1, int(item_level)),
                field_30=(
                    int(amount)
                    if definition.item_category in {"Currency", "InstantConsumable"}
                    else 0
                ),
                field_34=0,
                field_3c=bool(rarity > 0 and item_row.get("NeedsIdentify", False)),
                field_3d=False,
                template_id=definition.template_id,
                field_100=item_position,
                field_80=appearance,
                field_38=(
                    16
                    if definition.item_category
                    in MERCENARY_EQUIPMENT_CATEGORIES
                    else 0
                ),
                field_3e=True,
                enchantments=(
                    _roll_item_enchantments(
                        database,
                        definition,
                        class_name=class_name,
                        item_level=max(1, int(item_level)),
                        rarity=rarity,
                        enchantment_tier=map_difficulty,
                        rng=generator,
                    )
                    # Lootboxes keep their authored rarity (the christmas
                    # presents' glow) but carry no enchantments.
                    if definition.item_category
                    not in {"Currency", "InstantConsumable", "LootGenItem"}
                    else []
                ),
                socketed_templates=[""] * socket_count,
                created_at=(
                    now.year,
                    now.month,
                    now.day,
                    now.hour,
                    now.minute,
                    now.second,
                ),
            )
            if definition.item_category not in {"Currency", "InstantConsumable"}:
                item.field_30 = item_stack_vc_value(item, item_row)
                item.field_34 = equipment_melt_value(
                    item,
                    item_row,
                    database.enchantments_by_id,
                )
            drops.append(
                WorldLootDrop(item, definition.item_category, definition.auto_pick_up)
            )
            item_id += 1
        return WorldLootGeneration(profile, tuple(drops), item_id)
    finally:
        database.close()


def encode_new_item_batch(
    drops: tuple[WorldLootDrop, ...] | list[WorldLootDrop], *, actor_id: int = 0
) -> tuple[bytes, int]:
    if not drops:
        raise ValueError("NewItem batch requires at least one drop")
    writer = BitWriter()
    writer.write_uint8(0x85)
    for index, drop in enumerate(drops):
        if index:
            writer.write_uint8(0xFF)
        writer.write_bytes(NEW_ITEM_COMMAND.to_bytes(2, "little"))
        encode_item_info(writer, drop.item)
        writer.write_uint32_le(actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_new_item_batch(
    payload: bytes, bit_length: int
) -> tuple[tuple[ItemInfo, int], ...]:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("NewItem message is not a server batch")
    result = []
    while True:
        command_id = int.from_bytes(reader.read_bytes(2), "little")
        if command_id != NEW_ITEM_COMMAND:
            raise ValueError(f"expected NewItem 0x002d, got 0x{command_id:04x}")
        result.append((decode_item_info(reader), reader.read_uint32_le()))
        if reader.read_uint8() != 0xFF:
            raise ValueError("NewItem command is not followed by 0xff")
        if not reader.remaining:
            break
    return tuple(result)


def encode_discard_item_batch(
    item_ids: tuple[int, ...] | list[int],
    *,
    notify: bool = False,
    actor_id: int = 0,
) -> tuple[bytes, int]:
    if not item_ids:
        raise ValueError("DiscardItem batch requires at least one item")
    writer = BitWriter()
    writer.write_uint8(0x85)
    for index, item_id in enumerate(item_ids):
        if index:
            writer.write_uint8(0xFF)
        writer.write_bytes(DISCARD_ITEM_COMMAND.to_bytes(2, "little"))
        writer.write_uint32_le(int(item_id))
        writer.write_bool(notify)
        writer.write_uint32_le(actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def encode_pickup_info_batch(
    template_id: str,
    *,
    actor_id: int = 0,
) -> tuple[bytes, int]:
    """Publish the retail post-pickup staging notification.

    The native ``PickupInfoCommand`` serializer writes one resource-id string.
    The recorded boss session sends the picked template (for example
    ``money_vc``), followed by the normal server actor context.
    """

    if not template_id:
        raise ValueError("PickupInfo requires a template id")
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(PICKUP_INFO_COMMAND.to_bytes(2, "little"))
    writer.write_string(str(template_id))
    writer.write_uint32_le(int(actor_id))
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_pickup_info_batch(
    payload: bytes, bit_length: int
) -> tuple[str, int]:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("PickupInfo message is not a server batch")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != PICKUP_INFO_COMMAND:
        raise ValueError(f"expected PickupInfo 0x0065, got 0x{command_id:04x}")
    result = (reader.read_string(), reader.read_uint32_le())
    if reader.read_uint8() != 0xFF or reader.remaining:
        raise ValueError("PickupInfo has trailing data")
    return result


def _write_currency_changed_body(
    writer: BitWriter, value: CurrencyChanged
) -> None:
    if len(value.values) != 3:
        raise ValueError("CurrencyChanged requires three uint32 values")
    writer.write_uint32_le(value.currency_type)
    writer.write_uint64_le(value.amount)
    for number in value.values:
        writer.write_uint32_le(number)
    writer.write_uint64_le(value.delta)
    writer.write_bool(value.notify)


def _read_currency_changed_body(reader: BitReader) -> CurrencyChanged:
    return CurrencyChanged(
        reader.read_uint32_le(),
        reader.read_uint64_le(),
        tuple(reader.read_uint32_le() for _ in range(3)),
        reader.read_uint64_le(),
        reader.read_bool(),
    )


def _loot_received_currency_type(drop: WorldLootDrop) -> int:
    if drop.item.template_id in GOLD_ITEM_TEMPLATES:
        return 0
    if drop.item.template_id == ANDERMANT_ITEM_TEMPLATE:
        return 1
    if drop.item.template_id == WISDOM_ITEM_TEMPLATE:
        # The client's loot-received case shows types 3 and 5 as GrindCurrency
        # (the violet text); its currency names put Ancient Wisdom at 3.
        return 3
    raise ValueError(
        f"item {drop.item.template_id!r} is not a direct currency"
    )


def inventory_loot_text_type(item_category: str) -> int | None:
    """Loot-received text kind for a bag item retail announced on auto pickup.

    The 2026-10-28 amphora capture showed 6 for the event's progress key and
    7 for Draken (another session: 7 for an ore ingredient). Other bag items
    keep the silent pickup.
    """

    return {"Key": 6, "NoAutoUseCurrency": 7}.get(str(item_category))


def _write_loot_received(
    writer: BitWriter, drop: WorldLootDrop, actor_id: int,
    text_type: int | None = None,
) -> None:
    """MessageCommand 0x19: the "<amount> <currency> received" text over the player."""

    writer.write_bytes(MESSAGE_COMMAND.to_bytes(2, "little"))
    writer.write_uint8(LOOT_RECEIVED_MESSAGE)
    writer.write_uint32_le(4)
    writer.write_uint8(1)
    writer.write_uint32_le(
        _loot_received_currency_type(drop) if text_type is None else int(text_type)
    )
    writer.write_uint8(1)
    writer.write_uint32_le(max(0, int(drop.item.amount)))
    writer.write_uint8(4)
    writer.write_uint32_le(int(drop.item.item_id))
    writer.write_uint8(2)
    writer.write_string(drop.item.template_id)
    writer.write_uint32_le(int(actor_id))
    writer.write_uint8(0xFF)


def encode_wisdom_pickup_response(drop: WorldLootDrop, *, actor_id: int) -> tuple[bytes, int]:
    """Ancient Wisdom picked up: its loot-received text, then its DiscardItem.

    Wisdom is credited to talent progression, not to a wallet, so no currency
    state follows; without the message the client showed no text at all.
    """

    if drop.item.template_id != WISDOM_ITEM_TEMPLATE:
        raise ValueError("wisdom pickup requires the wisdom item")
    writer = BitWriter()
    writer.write_uint8(0x85)
    _write_loot_received(writer, drop, actor_id)
    writer.write_bytes(DISCARD_ITEM_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(int(drop.item.item_id))
    writer.write_bool(True)
    writer.write_uint32_le(0)
    writer.write_uint8(0xFF)
    return writer.finish()


def encode_world_pickup_response(
    drop: WorldLootDrop,
    *,
    actor_id: int,
    currency: CurrencyChanged | None = None,
    inventory: InventoryLootMutation | None = None,
    loot_text_type: int | None = None,
) -> tuple[bytes, int]:
    """Serialize the retail pickup transaction as one ordered command batch.

    Retail does not publish the discard, pickup notification, and destination
    state as independent application messages. Currency pickup additionally
    begins with ``MessageCommand`` type ``0x19`` so the still-registered world
    item can anchor the loot text before ``DiscardItem`` removes it; a bag
    item does too when ``loot_text_type`` names its text kind.
    """

    if drop.item_category == "Currency":
        if currency is None or inventory is not None:
            raise ValueError("currency pickup requires only currency state")
    elif drop.item_category == "InstantConsumable":
        if currency is not None or inventory is not None:
            raise ValueError("instant pickup has no persistent state")
    elif inventory is None or currency is not None:
        raise ValueError("inventory pickup requires only inventory mutation")

    writer = BitWriter()
    writer.write_uint8(0x85)
    if currency is not None:
        _write_loot_received(writer, drop, actor_id)
    elif inventory is not None and loot_text_type is not None:
        _write_loot_received(writer, drop, actor_id, loot_text_type)

    writer.write_bytes(DISCARD_ITEM_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(int(drop.item.item_id))
    # The live server sets this bit. It dispatches the world-item removal
    # event before later commands reuse the same item id in inventory state.
    writer.write_bool(True)
    writer.write_uint32_le(0)
    writer.write_uint8(0xFF)
    writer.write_bytes(PICKUP_INFO_COMMAND.to_bytes(2, "little"))
    writer.write_string(drop.item.template_id)
    writer.write_uint32_le(0)

    if currency is not None:
        writer.write_uint8(0xFF)
        writer.write_bytes(CURRENCY_CHANGED_COMMAND.to_bytes(2, "little"))
        _write_currency_changed_body(writer, currency)
        writer.write_uint32_le(int(actor_id))
    elif inventory is not None:
        for state in (inventory.delta, inventory.final):
            writer.write_uint8(0xFF)
            writer.write_bytes(INVENTORY_INFO_COMMAND.to_bytes(2, "little"))
            write_inventory_info_body(writer, state)
            writer.write_uint32_le(int(actor_id))

    writer.write_uint8(0xFF)
    return writer.finish()


def decode_world_pickup_response(
    payload: bytes, bit_length: int
) -> WorldPickupResponse:
    """Decode the fully modeled pickup topology used by the local server."""

    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("world pickup response is not a server batch")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    loot_received = None
    if command_id == MESSAGE_COMMAND:
        message_type = reader.read_uint8()
        if message_type != LOOT_RECEIVED_MESSAGE:
            raise ValueError(f"unexpected pickup message type {message_type}")
        if reader.read_uint32_le() != 4:
            raise ValueError("loot-received message requires four arguments")
        if reader.read_uint8() != 1:
            raise ValueError("loot currency type is not an integer")
        currency_type = reader.read_uint32_le()
        if reader.read_uint8() != 1:
            raise ValueError("loot amount is not an integer")
        amount = reader.read_uint32_le()
        if reader.read_uint8() != 4:
            raise ValueError("loot item id is not an item-id argument")
        message_item_id = reader.read_uint32_le()
        if reader.read_uint8() != 2:
            raise ValueError("loot template is not a string argument")
        message_template = reader.read_string()
        message_actor_id = reader.read_uint32_le()
        loot_received = LootReceivedMessage(
            currency_type,
            amount,
            message_item_id,
            message_template,
            message_actor_id,
        )
        if reader.read_uint8() != 0xFF:
            raise ValueError("loot message is not followed by a marker")
        command_id = int.from_bytes(reader.read_bytes(2), "little")

    if command_id != DISCARD_ITEM_COMMAND:
        raise ValueError(
            f"expected DiscardItem 0x002e, got 0x{command_id:04x}"
        )
    item_id = reader.read_uint32_le()
    notify = reader.read_bool()
    discard_actor_id = reader.read_uint32_le()
    if reader.read_uint8() != 0xFF:
        raise ValueError("DiscardItem is not followed by a marker")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != PICKUP_INFO_COMMAND:
        raise ValueError(
            f"expected PickupInfo 0x0065, got 0x{command_id:04x}"
        )
    template_id = reader.read_string()
    pickup_actor_id = reader.read_uint32_le()

    currency = None
    inventory_states = []
    state_actor_ids = []
    while True:
        if reader.read_uint8() != 0xFF:
            raise ValueError("pickup command is not followed by a marker")
        if not reader.remaining:
            break
        command_id = int.from_bytes(reader.read_bytes(2), "little")
        if command_id == CURRENCY_CHANGED_COMMAND:
            if currency is not None or inventory_states:
                raise ValueError("duplicate or mixed pickup destination state")
            currency = _read_currency_changed_body(reader)
            state_actor_ids.append(reader.read_uint32_le())
        elif command_id == INVENTORY_INFO_COMMAND:
            if currency is not None:
                raise ValueError("mixed currency and inventory pickup state")
            inventory_states.append(read_inventory_info_body(reader))
            state_actor_ids.append(reader.read_uint32_le())
        else:
            raise ValueError(
                f"unmodeled pickup state command 0x{command_id:04x}"
            )

    if loot_received is not None:
        if loot_received.item_id != item_id:
            raise ValueError("loot message and discard item ids disagree")
        if loot_received.template_id != template_id:
            raise ValueError("loot message and pickup templates disagree")
    return WorldPickupResponse(
        loot_received,
        item_id,
        notify,
        discard_actor_id,
        template_id,
        pickup_actor_id,
        currency,
        tuple(inventory_states),
        tuple(state_actor_ids),
    )


def decode_pickup_item(payload: bytes, bit_length: int | None = None) -> int:
    if bit_length is None:
        bit_length = len(payload) * 8
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x8B:
        raise ValueError("PickupItem is not a client command")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != PICKUP_ITEM_COMMAND:
        raise ValueError(f"expected PickupItem 0x0064, got 0x{command_id:04x}")
    item_id = reader.read_uint32_le()
    if reader.remaining:
        padding = reader.remaining
        if padding >= 8 or reader.read_bits(padding):
            raise ValueError("PickupItem has trailing data")
    return item_id


def credit_currency_drop(
    state: CurrencyChanged, drop: WorldLootDrop
) -> CurrencyChanged:
    amount = max(0, int(drop.item.amount))
    if drop.item.template_id == ANDERMANT_ITEM_TEMPLATE:
        value = int(state.currency_type) + amount
        if value > 0xFFFFFFFF:
            raise ValueError("Andermant balance overflows uint32")
        return CurrencyChanged(
            value, state.amount, state.values, state.delta, True
        )
    if drop.item.template_id in GOLD_ITEM_TEMPLATES:
        value = int(state.amount) + amount
        if value > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("gold balance overflows uint64")
        return CurrencyChanged(
            state.currency_type, value, state.values, state.delta, True
        )
    raise ValueError(f"item {drop.item.template_id!r} is not a direct currency")


def add_world_drop_to_inventory(
    info: InventoryInfo, drop: WorldLootDrop, *, max_stack_size: int = 1,
    next_item_id: int = 0, reserved_item_ids=(),
    ingredient_bag: bool = False,
) -> InventoryLootMutation:
    """Move one non-currency world item to its configured inventory bag.

    A stack it lands on is reissued under a fresh id, past ``next_item_id``
    and every ``reserved_item_ids`` (the items still on the ground: one loot's
    drops have consecutive ids, so the inventory's highest id + 1 can be the
    next drop's, which then could no longer be picked up). Ingredient-bag
    items use the inventory's dedicated ``field_180`` allocation and do not
    consume an ordinary storage slot.
    """

    if drop.item_category in {"Currency", "InstantConsumable"}:
        raise ValueError(f"{drop.item_category} is not stored in ordinary inventory")
    inventory = copy.deepcopy(info)
    if any(item.item_id == drop.item.item_id for item in inventory.items):
        raise ValueError(f"inventory already contains item {drop.item.item_id}")
    stored = copy.deepcopy(drop.item)
    stored.quest_id = drop.quest_id
    stored.field_100 = (0.0, 0.0, 0.0)
    changed_item_ids: list[int] = []
    replacements: dict[int, int] = {}
    remaining = max(1, int(stored.amount))
    maximum = max(1, int(max_stack_size))
    for old_item in tuple(inventory.items):
        in_destination = (
            int(old_item.item_id) in inventory.field_180
            if ingredient_bag
            else (
                int(old_item.item_id) in inventory.storage_58
                or int(old_item.item_id) in inventory.storage_80
            )
        )
        if (
            remaining <= 0 or old_item.template_id != stored.template_id
            or old_item.quest_id != stored.quest_id
            or int(old_item.amount) >= maximum
            or not in_destination
            or old_item.field_1c != stored.field_1c
            or old_item.field_04 != stored.field_04
            or old_item.item_level != stored.item_level
            or old_item.enchantments or stored.enchantments
            or old_item.socketed_templates or stored.socketed_templates
        ):
            continue
        added = min(remaining, maximum - int(old_item.amount))
        new_id = max(
            [int(item.item_id) for item in inventory.items]
            + [int(stored.item_id), int(next_item_id) - 1]
            + [int(value) for value in reserved_item_ids]
        ) + 1
        reserved_item_ids = (*reserved_item_ids, new_id)
        replacement = replace(
            old_item,
            item_id=new_id,
            amount=int(old_item.amount) + added,
            field_18=new_id & 0xFFFF,
            field_30=(
                (int(old_item.field_30) // max(1, int(old_item.amount)))
                * (int(old_item.amount) + added)
            ),
        )
        inventory.items[inventory.items.index(old_item)] = replacement
        for values in (
            inventory.equipment_slots, inventory.storage_58,
            inventory.storage_80, inventory.storage_a8, inventory.storage_d0,
        ):
            if int(old_item.item_id) in values:
                values[new_id] = values.pop(int(old_item.item_id))
        for values in (inventory.field_f8, inventory.field_118, inventory.field_180):
            for index, value in enumerate(values):
                if int(value) == int(old_item.item_id):
                    values[index] = new_id
        changed_item_ids.append(int(old_item.item_id))
        replacements[new_id] = int(old_item.item_id)
        remaining -= added
        if remaining <= 0:
            break
    if remaining > 0:
        stored.amount = remaining
        inventory.items.append(stored)
        if ingredient_bag:
            inventory.field_180.append(int(stored.item_id))
            if stored.template_id not in inventory.field_1a0:
                inventory.field_1a0.append(stored.template_id)
        else:
            location = free_inventory_location(inventory)
            if location is None:
                raise ValueError("ordinary inventory storage is full")
            storage_name, slot = location
            getattr(inventory, storage_name)[stored.item_id] = slot
    # Return changed stacks as authoritative item records; the pickup delta
    # also carries their retail ID replacement map.
    inventory.changed_item_ids = []
    inventory.field_158 = {}
    inventory.field_1e0 = False

    # Even with field_1e0=true, the client applies the equipment map as the
    # complete authoritative allocation. An empty map therefore unequips
    # everything. Carry the current allocation while limiting item/storage
    # records to the newly picked object.
    delta = copy.deepcopy(inventory)
    changed_ids = set(replacements) | {int(stored.item_id)}
    delta.items = [
        copy.deepcopy(item) for item in inventory.items
        if int(item.item_id) in changed_ids
    ]
    delta.equipment_slots = copy.deepcopy(inventory.equipment_slots)
    for name in ("storage_58", "storage_80", "storage_a8", "storage_d0"):
        allocation = getattr(inventory, name)
        setattr(delta, name, {
            int(allocated_id): int(slot)
            for allocated_id, slot in allocation.items()
            if int(allocated_id) in changed_ids
        })
    delta.field_f8 = []
    delta.field_118 = []
    delta.changed_item_ids = changed_item_ids
    delta.field_158 = replacements
    delta.field_180 = (
        copy.deepcopy(inventory.field_180) if ingredient_bag else []
    )
    delta.field_1a0 = (
        copy.deepcopy(inventory.field_1a0) if ingredient_bag else []
    )
    delta.field_1e0 = True

    # The second retail InventoryInfo closes the transaction. It also needs
    # the authoritative equipment map: an empty map is a real clear request,
    # not an omitted field.
    final = copy.deepcopy(inventory)
    final.items = []
    final.equipment_slots = copy.deepcopy(inventory.equipment_slots)
    final.storage_58 = {}
    final.storage_80 = {}
    final.storage_a8 = {}
    final.storage_d0 = {}
    final.field_f8 = []
    final.field_118 = []
    final.changed_item_ids = []
    final.field_158 = {}
    final.field_180 = (
        copy.deepcopy(inventory.field_180) if ingredient_bag else []
    )
    final.field_1a0 = (
        copy.deepcopy(inventory.field_1a0) if ingredient_bag else []
    )
    return InventoryLootMutation(inventory, delta, final)
