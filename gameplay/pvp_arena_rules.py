"""Loadout and stat rules that only apply inside a PvP arena.

Retail authors the arena bans on ``_Template_BattlegroundSettings``:
``BanItemCategory`` names item categories that may not act on that
battleground and ``BanStatusEffectGroup`` names the status-effect groups that
may not act (the official arenas ban ``Mount`` and ``Pet``).  The arena rules
the game is balanced around go further than the authored columns:

* only the item's own base values, enchantments and the gems socketed into it
  keep working — rune trinkets, jewel trinkets, Dragon Crests (ancient
  trinkets), rift-force emblems, item-set bonuses and mercenary gear do not;
* the Ancient Wisdom (``Knowledge``) and ``Group`` talent trees are inert
  while the class (``Skill``) and Honor (``Fame``) trees stay active;
* pets and mercenaries cannot be out at all, and mounts may only be ridden on
  the five-versus-five arenas.

The predicates here are deliberately structural: they read a session's map
and match, never its sockets, so the combat resolver can ask them per impact.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import sqlite3

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.pvp_match_protocol import (
    BATTLEGROUND_MAPS,
    MATCH_MODE_ASSAULT_THE_FORTRESS_5V5,
    MATCH_MODE_BEST_OF_5_5V5,
    MATCH_MODE_CAPTURE_THE_FLAG_5V5,
    battleground_ruleset,
)


# Match modes that field five players per side.  Mounts are allowed there.
MOUNT_MATCH_MODES = frozenset({
    MATCH_MODE_BEST_OF_5_5V5,
    MATCH_MODE_CAPTURE_THE_FLAG_5V5,
    MATCH_MODE_ASSAULT_THE_FORTRESS_5V5,
})

# Arena maps of the five-versus-five modes, plus the authored prototypes that
# share their settings.  Used when only the map is known (map load, reconnect).
MOUNT_MAPS = frozenset(
    {BATTLEGROUND_MAPS[mode] for mode in MOUNT_MATCH_MODES}
    | {"pvp_tdm_01", "pvp_prototype_01", "pvp_prototype_02", "pvp_prototype_03"}
)

# Runes, jewels, Dragon Crests, rift-force emblems and every Shadow Soul item
# are inert in an arena.  Gems socketed into ordinary equipment are not listed.
MERCENARY_ITEM_CATEGORIES = frozenset({
    "mercenary", "mercenaryjewelry", "mercenarydefensebackitem",
})
DISABLED_ITEM_CATEGORIES = frozenset({
    "runetrinket", "jeweltrinket", "ancienttrinket",
    "equipmentrune", "equipmentjewel", "equipmentancient",
    "emblem",
    *MERCENARY_ITEM_CATEGORIES,
})

# The only socket category that still contributes in an arena.
ALLOWED_SOCKET_CATEGORY = "equipmentgem"

# The summoned Shadow Soul's amulet, cloak, and belt slots.
MERCENARY_EQUIPMENT_SLOTS = frozenset({25, 26, 27})

# Rift-force bonuses (flat mitigation, echo chance and echo damage).
DISABLED_MODIFIER_KINDS = frozenset({
    "itemriftforce", "riftechochance", "riftecho",
})

# Talent trees that do not act in an arena.  ``Knowledge`` is Ancient Wisdom,
# ``Group`` the group-aura tree; ``Skill`` and ``Fame`` (Honor) stay active.
# The numbers are ``TALENT_CATEGORY_NAMES`` indexes: (Skill, Knowledge, Group,
# Fame).
DISABLED_TALENT_CATEGORIES = ("Knowledge", "Group")
DISABLED_TALENT_CATEGORY_IDS = frozenset({1, 2})


def arena_active(session_or_map) -> bool:
    """Whether this session (or map id) is inside a PvP arena."""

    if isinstance(session_or_map, str) or session_or_map is None:
        return battleground_ruleset(str(session_or_map or "")) is not None
    if getattr(session_or_map, "gameplay_match", None) is not None:
        return True
    return battleground_ruleset(
        str(getattr(session_or_map, "gameplay_map", "") or "")
    ) is not None


def mount_allowed(session) -> bool:
    """Mounts are only usable outside arenas and on the 5v5 arenas."""

    if not arena_active(session):
        return True
    match = getattr(session, "gameplay_match", None)
    if match is not None:
        return int(getattr(match, "match_mode", -1)) in MOUNT_MATCH_MODES
    return str(getattr(session, "gameplay_map", "") or "") in MOUNT_MAPS


def arena_team_size_five(session) -> bool:
    """The 5v5 modes, used to decide whether a mount may stay ridden."""

    match = getattr(session, "gameplay_match", None)
    if match is not None:
        return int(getattr(match, "match_mode", -1)) in MOUNT_MATCH_MODES
    return str(getattr(session, "gameplay_map", "") or "") in MOUNT_MAPS


def item_definition_disabled(definition) -> bool:
    return (
        str(getattr(definition, "category", "")).casefold()
        in DISABLED_ITEM_CATEGORIES
    )


def mercenary_definition(definition) -> bool:
    """Whether an item belongs to the Shadow Soul, not the player."""

    return (
        str(getattr(definition, "category", "")).casefold()
        in MERCENARY_ITEM_CATEGORIES
    )


def socket_allowed(definition) -> bool:
    return (
        str(getattr(definition, "category", "")).casefold()
        == ALLOWED_SOCKET_CATEGORY
    )


def modifier_disabled(kind) -> bool:
    return str(kind).casefold() in DISABLED_MODIFIER_KINDS


def session_database_path(session) -> str:
    """The static database a session is playing against."""

    combat = getattr(session, "gameplay_combat_state", None)
    encounter = getattr(combat, "encounter", None) if combat is not None else None
    path = getattr(encounter, "static_database_path", None)
    if path is None:
        path = getattr(session, "gameplay_static_database_path", None)
    return str(path or DEFAULT_STATIC_DB_PATH)


def disabled_talent_effect_ids(database_path) -> frozenset[int]:
    """Wire ids of the Ancient Wisdom and Group talent effects."""

    path = Path(str(database_path))
    try:
        stat = path.stat()
    except OSError:
        return frozenset()
    return _disabled_talent_effect_ids(
        path.as_posix(), stat.st_mtime_ns, stat.st_size
    )


@lru_cache(maxsize=8)
def _disabled_talent_effect_ids(
    path: str, _modified_ns: int, _size: int,
) -> frozenset[int]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
        rows = database.execute(
            "SELECT rowid FROM _Template_StatusEffect WHERE Id IN ("
            "SELECT BaseStatusEffectId FROM _Template_Talents "
            "WHERE TalentCategory IN (?, ?) "
            "UNION SELECT StackStatusEffectId FROM _Template_Talents "
            "WHERE TalentCategory IN (?, ?))",
            (*DISABLED_TALENT_CATEGORIES, *DISABLED_TALENT_CATEGORIES),
        ).fetchall()
    return frozenset(int(row[0]) - 1 for row in rows if row[0] is not None)


def talent_effect_disabled(entry, disabled_ids: frozenset[int]) -> bool:
    """Whether an active status entry belongs to a disabled talent tree."""

    try:
        effect_id = int(getattr(entry, "effect_id", -1))
    except (TypeError, ValueError):
        return False
    return effect_id in disabled_ids


def filter_talent_categories(categories, arena: bool):
    """Talent selections as the client should receive them.

    The client's own talent manager rebuilds every tree from the selection
    list it is handed, so dropping the arena's Ancient Wisdom and Group rank
    effects is not enough: their selections have to be emptied too, or the
    client reapplies both trees locally.
    """

    if not arena or categories is None:
        return categories
    result = list(categories)
    for category_id in DISABLED_TALENT_CATEGORY_IDS:
        if not 0 <= category_id < len(result):
            continue
        selection = result[category_id]
        result[category_id] = type(selection)(
            selection.preselection, (), ()
        )
    return tuple(result)


@lru_cache(maxsize=8)
def _set_item_templates(path: str, _modified_ns: int, _size: int) -> frozenset[str]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
        rows = database.execute(
            "SELECT SetParts FROM _Template_ItemSet WHERE SetParts IS NOT NULL AND SetParts != ''"
        ).fetchall()
    parts = set()
    for row in rows:
        for part in row[0].split(';'):
            part = part.strip()
            if part:
                parts.add(part)
    return frozenset(parts)


def set_item_templates(database_path=None) -> frozenset[str]:
    """All item template IDs that belong to any set in the static database."""
    path = Path(str(database_path or DEFAULT_STATIC_DB_PATH))
    try:
        stat = path.stat()
    except OSError:
        return frozenset()
    return _set_item_templates(path.as_posix(), stat.st_mtime_ns, stat.st_size)


def mask_arena_inventory(inventory, arena: bool, database_path=None):
    """Mask equipped set items with _pvp template variants for arena client view.

    The client evaluates set bonuses locally whenever equipped items match
    _Template_ItemSet.SetParts. By masking equipped set items with their _pvp
    counterparts, the client displays all normal item stats, models, icons and
    enchantments, but evaluates 0 matching set pieces, preventing client-side
    stat boosts, extra projectiles, and active set tooltips.
    """
    if not arena or inventory is None:
        return inventory
    set_templates = set_item_templates(database_path)
    if not set_templates:
        return inventory
    from dso_server.protocol.inventory_codec import clone_inventory_info, clone_item_info

    equipped_ids = {int(k) for k, slots in inventory.equipment_slots.items() if slots}
    has_masked = False
    new_items = []
    for item in inventory.items:
        if int(item.item_id) in equipped_ids and item.template_id in set_templates:
            cloned = clone_item_info(item)
            cloned.template_id = f"{item.template_id}_pvp"
            new_items.append(cloned)
            has_masked = True
        else:
            new_items.append(item)

    if not has_masked:
        return inventory
    return clone_inventory_info(inventory, items=new_items)


def banned_item_categories(database_path, battleground_id) -> frozenset[str]:
    """Item categories the arena's own row forbids.

    ``_Template_BattlegroundSettings.BanItemCategory`` is authored content:
    the queue rows ban the rune and jewel trinkets outright.  The client reads
    the same row, so an item whose category is banned cannot be equipped there
    and its sockets never come into play.
    """

    if not battleground_id:
        return frozenset()
    path = Path(str(database_path))
    try:
        stat = path.stat()
    except OSError:
        return frozenset()
    return _banned_item_categories(
        path.as_posix(), stat.st_mtime_ns, stat.st_size, str(battleground_id),
    )


@lru_cache(maxsize=64)
def _banned_item_categories(
    path: str, _modified_ns: int, _size: int, battleground_id: str,
) -> frozenset[str]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
        row = database.execute(
            "SELECT BanItemCategory FROM _Template_BattlegroundSettings "
            "WHERE Id = ? COLLATE NOCASE ORDER BY rowid LIMIT 1",
            (str(battleground_id),),
        ).fetchone()
    if row is None:
        return frozenset()
    return frozenset(
        part.strip().casefold()
        for part in str(row[0] or "").split(";")
        if part.strip()
    )


def category_banned(definition, banned: frozenset[str]) -> bool:
    """Whether an equipped item's category is authored as banned."""

    return str(getattr(definition, "category", "")).casefold() in banned


def mercenary_effect_ids(database_path) -> frozenset[int]:
    """Wire ids of every status effect a Shadow Soul or its gear authors."""

    path = Path(str(database_path))
    try:
        stat = path.stat()
    except OSError:
        return frozenset()
    return _mercenary_effect_ids(path.as_posix(), stat.st_mtime_ns, stat.st_size)


def arena_disabled_effect_ids(database_path) -> frozenset[int]:
    """Status effects an arena must never apply.

    The Ancient Wisdom/Group talent ranks, the Shadow Soul and its gear, and
    item-set bonuses are authored as status effects. Dropping them here covers
    every storage container, including the ones a save restores into the
    player's persistent list.
    """

    from dso_server.services.item_set_persistence import persistent_set_effect_ids

    return (
        disabled_talent_effect_ids(database_path)
        | mercenary_effect_ids(database_path)
        | persistent_set_effect_ids(database_path)
    )


@lru_cache(maxsize=8)
def _mercenary_effect_ids(
    path: str, _modified_ns: int, _size: int,
) -> frozenset[int]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
        rows = database.execute(
            "SELECT rowid - 1 FROM _Template_StatusEffect "
            "WHERE Id LIKE 'mercenary%' OR Id LIKE 'minion_mercenary%'"
        ).fetchall()
    return frozenset(int(row[0]) for row in rows)


__all__ = [
    "ALLOWED_SOCKET_CATEGORY",
    "DISABLED_ITEM_CATEGORIES",
    "DISABLED_MODIFIER_KINDS",
    "DISABLED_TALENT_CATEGORIES",
    "DISABLED_TALENT_CATEGORY_IDS",
    "MERCENARY_EQUIPMENT_SLOTS",
    "MERCENARY_ITEM_CATEGORIES",
    "MOUNT_MAPS",
    "MOUNT_MATCH_MODES",
    "arena_active",
    "arena_disabled_effect_ids",
    "arena_team_size_five",
    "banned_item_categories",
    "category_banned",
    "disabled_talent_effect_ids",
    "filter_talent_categories",
    "item_definition_disabled",
    "mercenary_definition",
    "mercenary_effect_ids",
    "modifier_disabled",
    "mount_allowed",
    "session_database_path",
    "socket_allowed",
    "talent_effect_disabled",
]
