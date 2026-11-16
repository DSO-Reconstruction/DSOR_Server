"""Database-driven runtime game-event scheduling and progression.

The client owns the presentation of an event, but the server owns the event
instance: its runtime id, schedule, participation, progress, rewards, loot and
map availability.  Static definitions remain in ``static.db4``; the small JSON
schedule selects which definitions are active/upcoming without modifying the
shared game database.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
import json
import math
from pathlib import Path
import random
import re
import sqlite3
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping

from dso_server.runtime import resident_content
from dso_server.persistence import static_schema_compat as compat
from dso_server.protocol.gameplay_protocol import StatusEffectEntry
from dso_server.protocol.player_init_protocol import (
    EventParticipation,
    EventParticipationInfo,
    EventProgressInfo,
    EventProgressInfoEntry,
    EventUpdate,
    EventUpdateEntry,
)
from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
from dso_server.paths import DEFAULT_EVENT_ITEM_REGIONS_PATH, DEFAULT_EVENT_LOOT_PATH
from dso_server.runtime.file_revisions import observed_revision, resolved_path


UINT32_MAX = 0xFFFFFFFF
_CURRENCY_COLLECTIBLE_CATEGORIES = frozenset({"Currency", "NoAutoUseCurrency"})
_DIFFICULTY_SUFFIX = re.compile(r"_diff_\d+$", re.IGNORECASE)
_DRAGAN_DIFFICULTIES = (0, 1, 2, 3, 4, 6, 8)
# Difficulty ids 0..9 use 1, 2, 3, 4, 5, 5, 7, 7, 10, 10 beans;
# Bloodshed and Parallel World Bloodshed both give the requested 10x stack.
_SARGON_WORLD_BOSS_COFFEE_BEANS_BY_DIFFICULTY = (
    1, 2, 3, 4, 5, 5, 7, 7, 10, 10,
)
_DRAGAN_CASTLE_PROGRESS_AMOUNTS = (
    (2, 3), (3, 4), (4, 6), (6, 8), (8, 10), (12, 16), (20, 20),
)
_DRAGAN_ELEMENTAL_PROGRESS_AMOUNTS = (
    (2, 3), (3, 4), (4, 6), (6, 8), (8, 10), (12, 16), (20, 20),
)
_DRAGAN_GRAVE_BOSS_PROGRESS_AMOUNTS = (50, 75, 100, 125, 180, 300, 600)
_DRAGAN_REFUGE_BOSS_PROGRESS_AMOUNTS = (16, 24, 32, 40, 65, 120, 250)
_DRAGAN_GREAT_HALL_PROGRESS_AMOUNTS = (50, 75, 100, 125, 180, 300, 600)
_DRAGAN_ELEMENTAL_MAPS = frozenset({
    "e_dra_03_dun_gloomypastures_01",
    "e_dra_03_dun_ironforestnight_01",
    "e_dra_03_dun_jail_01",
    "e_dra_03_dun_storage_01",
    "e_dra_03_dun_mystranight_01",
    "e_dra_03_dun_wildforestnight_01",
})
_DRAGAN_ATTIRE_COSTUMES = frozenset({
    "costume_event_pass_dragan_warrior",
    "costume_event_pass_dragan_mage",
    "costume_event_pass_dragan_ranger",
    "costume_event_pass_dragan_dwarf",
})
_SARGON_ATTIRE_COSTUMES = frozenset({
    "costume_event_pass_sargon_warrior",
    "costume_event_pass_sargon_mage",
    "costume_event_pass_sargon_ranger",
    "costume_event_pass_sargon_dwarf",
})
_CHRISTMAS_ATTIRE_COSTUMES = frozenset({
    "costume_event_pass_xmas_warrior",
    "costume_event_pass_xmas_mage",
    "costume_event_pass_xmas_ranger",
    "costume_event_pass_xmas_dwarf",
    "costume_event_pass_xmas_three_hours_warrior",
    "costume_event_pass_xmas_three_hours_mage",
    "costume_event_pass_xmas_three_hours_ranger",
    "costume_event_pass_xmas_three_hours_dwarf",
})
_CHRISTMAS_DIFFICULTY_INDEX_MAP = {
    0: 0,  # Normal
    1: 1,  # Painful
    2: 2,  # Excruciating
    3: 3,  # Fatal
    4: 4,  # Infernal
    5: 4,  # PW Infernal
    6: 5,  # Merciless
    7: 5,  # PW Merciless
    8: 6,  # Bloodshed
    9: 6,  # PW Bloodshed
}
_KINGSHILL_SEWERS_DIFFICULTY_INDEX_MAP = {
    0: 0,  # Normal
    1: 1,  # Painful
    2: 2,  # Excruciating
    3: 3,  # Fatal
    4: 4,  # Infernal
    5: 4,  # PW Infernal
    6: 5,  # Merciless
    7: 5,  # PW Merciless
    8: 6,  # Bloodshed
    9: 6,  # PW Bloodshed
}
_KINGSHILL_SEWERS_MOB_PROGRESS_AMOUNTS = (6, 7, 8, 9, 10, 12, 14)
_KINGSHILL_SEWERS_BOSS_PROGRESS_AMOUNTS = (36, 36, 72, 108, 143, 287, 537)
# Sewers Blitz mini-events publish worm-boss stacks for the upper three
# difficulties. Lower difficulties retain their authored bar-base value.
_KINGSHILL_SEWERS_BLITZ_WORM_PROGRESS_AMOUNTS = (
    None, None, None, None, 25, 75, 95,
)
# Kia Krisal's armor boss has an explicit difficulty-sized progress stack,
# ending at the requested 170 on Bloodshed. Its static rank is Sentinel, so
# scope by the boss templates as well as the authored rank.
_DOE_KIA_KRISAL_BOSS_PROGRESS_AMOUNTS = (
    20, 39, 58, 76, 95, 114, 133, 151, 170, 170,
)
_DOE_KIA_KRISAL_BOSS_TEMPLATES = frozenset({
    "e_doe_boss_armor_white_steam_ranger",
    "e_doe_boss_armor_green_ranger",
    "e_doe_boss_armor_yellow_dwarf",
    "e_doe_boss_armor_blue_mage",
    "e_doe_boss_armor_red_dragon_knight",
    "e_doe_boss_armor_purple_battle_mage",
})
_CHRISTMAS_BOSS_PROGRESS_AMOUNTS = (100, 125, 150, 175, 300, 400, 550)
_CHRISTMAS_ELITE_PROGRESS_AMOUNTS = (5, 6, 8, 9, 15, 20, 28)
_CHRISTMAS_PIGLET_PROGRESS_AMOUNTS = (5, 6, 8, 9, 15, 20, 28)
YEAR_ROUND_MOON_EVENT_MAPS = frozenset({
    "e_fullmoon_varholm_dun",
    "e_newmoon_blakborg_city_dun",
})
YEAR_ROUND_MOON_TRAVEL_ITEMS = frozenset({
    "e_ful_01_teleport_01",
    "e_new_01_teleport_01",
})
_DROP_AMOUNT = re.compile(
    r"^DropAmount:([^,]+),(absolute|relative),([^#]+)", re.IGNORECASE
)


def _tokens(value: object) -> tuple[str, ...]:
    return tuple(
        token.strip()
        for token in str(value or "").replace(",", ";").split(";")
        if token.strip()
    )


def completed_achievement_ids(state: Mapping[str, object] | None) -> frozenset[str]:
    """Return completed achievement ids for event-bar eligibility checks."""

    result = set()
    for key, progress in (state or {}).items():
        completed = getattr(progress, "completed", None)
        if completed is None and isinstance(progress, Mapping):
            completed = bool(progress.get("completed_at"))
        if completed:
            result.add(str(key).casefold())
    return frozenset(result)


def _map_key(value: object) -> str:
    return _DIFFICULTY_SUFFIX.sub("", str(value or "").strip()).casefold()


def _dragan_progress_amount(
    *,
    item_id: str,
    map_ids: Iterable[str],
    monster_rank: str,
    map_difficulty: int,
    rng: random.Random,
) -> int | None:
    """Return Dragan's difficulty-specific progress stack from the Oct guide.

    The guide publishes two values for normal enemies and their stronger
    counterparts on the first two stages; boss stages have one amount per map
    difficulty.
    """

    maps = frozenset(_map_key(value) for value in map_ids)
    collectible = str(item_id or "").casefold()
    difficulty = max(0, int(map_difficulty))
    try:
        difficulty_index = _DRAGAN_DIFFICULTIES.index(difficulty)
    except ValueError:
        eligible = [
            (index, value)
            for index, value in enumerate(_DRAGAN_DIFFICULTIES)
            if value <= difficulty
        ]
        difficulty_index = eligible[-1][0] if eligible else 0
    rank = str(monster_rank or "normal").casefold()
    elite_roll = rank not in {"", "normal", "minion"}

    if (
        collectible == "e_dra_02_riseofdragan_key_for_maps_01"
        and (
            "e_dra_03_dun_01" in maps
            or maps & _DRAGAN_ELEMENTAL_MAPS
        )
    ):
        low, high = _DRAGAN_CASTLE_PROGRESS_AMOUNTS[difficulty_index]
        return high if elite_roll else low
    if (
        collectible == "e_dra_progress_item"
        and maps & _DRAGAN_ELEMENTAL_MAPS
    ):
        low, high = _DRAGAN_ELEMENTAL_PROGRESS_AMOUNTS[difficulty_index]
        return high if elite_roll else low
    if collectible == "e_dra_progress_item_boss":
        if "e_dra_03_dun_grave_01" in maps:
            return _DRAGAN_GRAVE_BOSS_PROGRESS_AMOUNTS[difficulty_index]
        if "e_dra_03_dun_boss_01" in maps:
            return _DRAGAN_REFUGE_BOSS_PROGRESS_AMOUNTS[difficulty_index]
        if "e_dra_03_dun_02" in maps:
            return _DRAGAN_GREAT_HALL_PROGRESS_AMOUNTS[difficulty_index]
    return None


def _christmas_progress_amount(
    *,
    item_id: str,
    monster_rank: str,
    map_difficulty: int = 0,
    monster_template_id: str = "",
) -> int | None:
    """Return the progress stack for Winter Solstice Festival (christmas_2012).

    Difficulty tiers: Normal (100/5), Painful (125/6), Excruciating (150/8),
    Fatal (175/9), Infernal (300/15), Merciless (400/20), Bloodshed (550/28).
    """

    if str(item_id or "").casefold() != "e_chr_progress_item":
        return None
    rank = str(monster_rank or "normal").casefold()
    diff_index = _CHRISTMAS_DIFFICULTY_INDEX_MAP.get(int(map_difficulty or 0), 0)

    # Piglets
    if "pig" in str(monster_template_id or "").casefold():
        return _CHRISTMAS_PIGLET_PROGRESS_AMOUNTS[diff_index]

    # Event Bosses
    if rank in {"worldboss", "boss"}:
        return _CHRISTMAS_BOSS_PROGRESS_AMOUNTS[diff_index]

    # Elite monsters (sentinel, champion, leader, elite)
    if rank in {"sentinel", "champion", "leader", "elite"}:
        return _CHRISTMAS_ELITE_PROGRESS_AMOUNTS[diff_index]

    return None


def _kingshill_sewers_progress_amount(
    *, monster_rank: str, map_difficulty: int,
) -> int:
    """Return the main Kingshill Sewers progress stack by rank and tier."""

    index = _KINGSHILL_SEWERS_DIFFICULTY_INDEX_MAP.get(
        int(map_difficulty or 0), 0
    )
    amounts = (
        _KINGSHILL_SEWERS_BOSS_PROGRESS_AMOUNTS
        if str(monster_rank or "").casefold() in {"boss", "worldboss"}
        else _KINGSHILL_SEWERS_MOB_PROGRESS_AMOUNTS
    )
    return amounts[index]


_CHRISTMAS_BIG_PAWS_FROST_SHARDS = {
    0: (1, 2),   # Normal
    1: (1, 2),   # Painful
    2: (2, 4),   # Excruciating
    3: (3, 5),   # Fatal
    4: (4, 6),   # Infernal
    6: (8, 10),  # Merciless
    8: (9, 11),  # Bloodshed
}

_CHRISTMAS_KRANPARUS_FROST_SHARDS = {
    0: (2, 6),    # Normal: 2-6
    1: (5, 10),   # Painful: 5-10
    2: (10, 15),  # Excruciating: 10-15
    3: (15, 20),  # Fatal: 15-20
    4: (15, 25),  # Infernal: 15-25
    6: (35, 45),  # Merciless: 35-45
    8: (45, 55),  # Bloodshed: 45-55
}

_CHRISTMAS_BIG_MOMMA_HEART_OF_WINTER = {
    0: (1, 1),   # Normal: 1
    1: (1, 1),   # Painful: 1
    2: (1, 2),   # Excruciating: 1-2
    3: (1, 2),   # Fatal: 1-2
    4: (2, 3),   # Infernal: 2-3
    6: (5, 7),   # Merciless: 5-7
    8: (7, 9),   # Bloodshed: 7-9
}

_WINTERS_GUARDIAN_AMULET_BY_CLASS = MappingProxyType({
    "warrior": "set_winters_guardian_amulet_warrior",
    "dragonknight": "set_winters_guardian_amulet_warrior",
    "dk": "set_winters_guardian_amulet_warrior",
    "mage": "set_winters_guardian_amulet_mage",
    "spellweaver": "set_winters_guardian_amulet_mage",
    "ranger": "set_winters_guardian_amulet_ranger",
    "dwarf": "set_winters_guardian_amulet_dwarf",
    "steammechanicus": "set_winters_guardian_amulet_dwarf",
})

_BIG_PAWS_WEAPON_BY_CLASS = MappingProxyType({
    "warrior": "warrior_unique_2h_mace_christmas_bear_set",
    "dragonknight": "warrior_unique_2h_mace_christmas_bear_set",
    "dk": "warrior_unique_2h_mace_christmas_bear_set",
    "mage": "mage_unique_2h_staff_christmas_bear_set",
    "spellweaver": "mage_unique_2h_staff_christmas_bear_set",
    "ranger": "ranger_unique_2h_bow_christmas_bear_set",
    "dwarf": "dwarf_unique_2h_rifle_christmas_bear_set",
    "steammechanicus": "dwarf_unique_2h_rifle_christmas_bear_set",
})

_BIG_PAWS_BACKITEM_BY_CLASS = MappingProxyType({
    "warrior": "warrior_unique_backitem_christmas_bear_set",
    "dragonknight": "warrior_unique_backitem_christmas_bear_set",
    "dk": "warrior_unique_backitem_christmas_bear_set",
    "mage": "mage_unique_backitem_christmas_bear_set",
    "spellweaver": "mage_unique_backitem_christmas_bear_set",
    "ranger": "ranger_unique_backitem_christmas_bear_set",
    "dwarf": "dwarf_unique_backitem_christmas_bear_set",
    "steammechanicus": "dwarf_unique_backitem_christmas_bear_set",
})


def _canonical_christmas_difficulty(map_difficulty: int) -> int:
    diff = int(map_difficulty or 0)
    if diff in (4, 5):
        return 4
    if diff in (6, 7):
        return 6
    if diff in (8, 9):
        return 8
    return min(max(0, diff), 3)


def christmas_boss_extra_loot(
    *,
    map_id: str,
    monster_template_id: str,
    map_difficulty: int = 0,
    class_name: str = "",
    rng: random.Random | None = None,
    map_aliases: Iterable[str] = (),
) -> tuple[tuple[str, int], ...]:
    """Calculate additional boss drops for the Winter Solstice Festival.

    Drop tables:
    - Pristine Frost Shard (item_zero_degree_ice_crystal) for Big Paws & Kranparus
    - Heart of Winter (item_heart_of_winter) for Big Paws, Kranparus & Big Momma
    - Frost Age Materials (Torso, Gloves, Boots) for Kranparus challenges, Frozen Field & Big Momma
    - Winter's Guardian Amulet for Big Momma on Bloodshed difficulty
    """
    generator = rng or random.Random()
    current_maps = frozenset(
        _map_key(m) for m in (map_id, *tuple(map_aliases or ())) if m
    )
    monster = str(monster_template_id or "").casefold()

    is_big_paws = (
        monster == "e_chr_boss_big_paws"
        or (
            "big_paws" in monster
            and "mom" not in monster
            and ("e_b1205_dun_chr_2012_02" in current_maps or not current_maps)
        )
    )

    is_big_momma = (
        monster == "e_chr_boss_big_paws_mom"
        or (
            "mom" in monster
            and ("e_b1205_dun_chr_2025_01" in current_maps or "big_paws" in monster or not current_maps)
        )
        or ("e_b1205_dun_chr_2025_01" in current_maps and "boss" in monster)
    )

    is_krampus_challenge_01 = (
        monster == "e_chr_2015_boss_krampus_challenge_01"
        or (
            "challenge_01" in monster
            and ("krampus" in monster or "kranparus" in monster)
        )
        or (
            "e_chr_challenge_dun_01" in current_maps
            and ("krampus" in monster or "kranparus" in monster or "boss" in monster)
        )
    )

    is_krampus_challenge_02 = (
        monster == "e_chr_2015_boss_krampus_challenge_02"
        or (
            "challenge_02" in monster
            and ("krampus" in monster or "kranparus" in monster)
        )
        or (
            "e_chr_challenge_dun_02" in current_maps
            and ("krampus" in monster or "kranparus" in monster or "boss" in monster)
        )
    )

    is_krampus_challenge_03 = (
        monster == "e_chr_2015_boss_krampus_challenge_03"
        or (
            "challenge_03" in monster
            and ("krampus" in monster or "kranparus" in monster)
        )
        or (
            "e_chr_challenge_dun_03" in current_maps
            and ("krampus" in monster or "kranparus" in monster or "boss" in monster)
        )
    )

    is_krampus_frozen_field = (
        "e_b1205_dun_chr_2024" in current_maps
        and (
            "krampus" in monster
            or "kranparus" in monster
            or monster.startswith("e_chr_2024_boss_krampus")
            or "boss" in monster
        )
    )

    is_kranparus_any = (
        is_krampus_challenge_01
        or is_krampus_challenge_02
        or is_krampus_challenge_03
        or is_krampus_frozen_field
        or (("krampus" in monster or "kranparus" in monster) and not is_big_paws and not is_big_momma)
    )

    if not (is_big_paws or is_big_momma or is_kranparus_any):
        return ()

    diff_key = _canonical_christmas_difficulty(map_difficulty)
    extra_drops: list[tuple[str, int]] = []

    # 1. Pristine Frost Shard (item_zero_degree_ice_crystal)
    if is_big_paws:
        low, high = _CHRISTMAS_BIG_PAWS_FROST_SHARDS.get(diff_key, (1, 2))
        extra_drops.append(("item_zero_degree_ice_crystal", generator.randint(low, high)))
    elif is_kranparus_any:
        low, high = _CHRISTMAS_KRANPARUS_FROST_SHARDS.get(diff_key, (2, 6))
        extra_drops.append(("item_zero_degree_ice_crystal", generator.randint(low, high)))

    # 2. Heart of Winter (item_heart_of_winter)
    if is_big_paws:
        if diff_key in (6, 8):
            extra_drops.append(("item_heart_of_winter", 1))
    elif is_kranparus_any:
        if diff_key == 6:
            extra_drops.append(("item_heart_of_winter", generator.randint(3, 4)))
        elif diff_key == 8:
            extra_drops.append(("item_heart_of_winter", generator.randint(4, 5)))
    elif is_big_momma:
        low, high = _CHRISTMAS_BIG_MOMMA_HEART_OF_WINTER.get(diff_key, (1, 1))
        extra_drops.append(("item_heart_of_winter", generator.randint(low, high)))

    # 3. Frost Age Materials (Torso, Gloves, Boots)
    frost_age_materials = (
        "item_ingredient_frost_age_torso",
        "item_ingredient_frost_age_gloves",
        "item_ingredient_frost_age_boots",
    )

    if is_krampus_challenge_01:
        if diff_key in (6, 8) and generator.random() < 0.03:
            extra_drops.append(("item_ingredient_frost_age_torso", 1))
    elif is_krampus_challenge_02:
        if diff_key in (6, 8) and generator.random() < 0.03:
            extra_drops.append(("item_ingredient_frost_age_gloves", 1))
    elif is_krampus_challenge_03:
        if diff_key in (6, 8) and generator.random() < 0.03:
            extra_drops.append(("item_ingredient_frost_age_boots", 1))
    elif is_krampus_frozen_field:
        if diff_key in (6, 8) and generator.random() < 0.03:
            extra_drops.append((generator.choice(frost_age_materials), 1))
    elif is_big_momma:
        if diff_key < 8:
            if generator.random() < 0.03:
                extra_drops.append((generator.choice(frost_age_materials), 1))
        else:
            # On Bloodshed, significantly higher chance, can drop all 3 ingredients
            roll = generator.random()
            if roll < 0.20:
                for mat in frost_age_materials:
                    extra_drops.append((mat, 1))
            elif roll < 0.40:
                extra_drops.append((generator.choice(frost_age_materials), 1))

    # 4. Big Momma Winter's Guardian (Amulet) on Bloodshed (2% chance)
    if is_big_momma and diff_key == 8:
        if generator.random() < 0.02:
            amulet_template = _WINTERS_GUARDIAN_AMULET_BY_CLASS.get(
                str(class_name or "").casefold(),
                "set_winters_guardian_amulet_warrior",
            )
            extra_drops.append((amulet_template, 1))

    # 5. Big Paws' Frosty Weapon & Greed's End (Backitem)
    if is_big_paws:
        class_key = str(class_name or "").casefold()
        if generator.random() < 0.25:
            weapon_template = _BIG_PAWS_WEAPON_BY_CLASS.get(
                class_key, "mage_unique_2h_staff_christmas_bear_set"
            )
            extra_drops.append((weapon_template, 1))
        if generator.random() < 0.25:
            backitem_template = _BIG_PAWS_BACKITEM_BY_CLASS.get(
                class_key, "mage_unique_backitem_christmas_bear_set"
            )
            extra_drops.append((backitem_template, 1))

    # 6. Frostpocalypse Recipe (recipe_cataclysmic_winter) on boss (10% chance)
    if (is_big_paws or is_big_momma) and generator.random() < 0.10:
        extra_drops.append(("recipe_cataclysmic_winter", 1))

    return tuple(extra_drops)


def _timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _wire_time(value: datetime | None) -> tuple[int, int, int, int, int, int]:
    if value is None:
        return (0, 0, 0, 0, 0, 0)
    value = value.astimezone(timezone.utc)
    return (
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
    )


@dataclass(frozen=True)
class EventReward:
    key: str
    tier: int
    threshold: int
    item_options: Mapping[str, str]
    amount: int
    currency: bool = False

    def item_for_class(self, class_name: str) -> str | None:
        if self.currency:
            return None
        folded = str(class_name).casefold()
        return self.item_options.get(folded) or self.item_options.get("")


@dataclass(frozen=True)
class EventCollectible:
    item_id: str
    item_category: str
    auto_pick_up: str
    base_value: int
    # _Template_Item.Region: loot regions, matched against the map's
    # _Template_MapTemplate.Region and the monster's _Template_Monster.Region
    # (plus map ids for a few items whose region names a map).
    maps: frozenset[str]
    # _Template_Item.EventId: the event family the item belongs to.
    owner_events: frozenset[str] = frozenset()
    # Paged bars (FixedBarCollectible, or one ``0/``, ``1/``... entry per
    # page): the page this item is collected on; None on ordinary bars.
    tier: int | None = None


@dataclass(frozen=True)
class EventProgressBarDefinition:
    resource_id: str
    minimum_level: int
    maximum_level: int
    priority: int
    collectibles: tuple[EventCollectible, ...]
    item_rewards: tuple[EventReward, ...]
    currency_rewards: tuple[EventReward, ...]
    required_achievements: frozenset[str] = frozenset()
    # Bars that advance through leaderboard placement or level-ups list a
    # collectible only as their progress unit; monsters never drop it.
    monster_drops: bool = True

    def final_item_threshold(self, tier: int) -> int:
        """The last item milestone for one page/group of a progress bar."""

        return max((
            int(reward.threshold)
            for reward in self.item_rewards
            if int(reward.tier) == int(tier)
        ), default=0)

    def page_end(self, tier: int) -> int:
        """Include both reward tracks when deciding that a page is complete."""
        return max((reward.threshold for reward in (
            *self.item_rewards, *self.currency_rewards,
        ) if reward.tier == tier), default=0)

    def anniversary_state(self, state: Mapping[str, object]) -> dict[str, object]:
        """Read the sequential counter, migrating the earlier regional saves."""
        result = dict(state)
        claimed = {str(value) for value in state.get("claimed", ())}
        groups = state.get("groups", {})
        if state.get("progress_model") != "sequential" and isinstance(groups, Mapping) and groups:
            total = 0
            for tier in sorted({value.tier for value in self.collectibles if value.tier is not None}):
                regional = groups.get(str(tier), {})
                if not isinstance(regional, Mapping):
                    regional = {}
                start = self.page_end(tier - 1) if tier else 0
                end = self.page_end(tier)
                local = min(end - start, max(0, int(regional.get("amount", 0))))
                total = start + local
                if total < end:
                    break
            for regional in groups.values():
                if isinstance(regional, Mapping):
                    claimed.update(str(value) for value in regional.get("claimed", ()))
        else:
            total = max(0, int(state.get("amount", 0)))
        last = max((reward.threshold for reward in (
            *self.item_rewards, *self.currency_rewards,
        )), default=0)
        result.update(amount=min(total, last), group=0,
                      progress_model="sequential", claimed=sorted(claimed))
        result.pop("groups", None)
        return result

    def accepts(
        self,
        player_level: int,
        completed_achievements: Iterable[str] = (),
    ) -> bool:
        return (
            self.minimum_level <= player_level <= self.maximum_level
            and self.required_achievements.issubset(
                {str(value).casefold() for value in completed_achievements}
            )
        )

    def current_tier(self, amount: int) -> int:
        """The bar page a player with ``amount`` progress is collecting on.

        Reward entries are ``page/threshold``; the page of the next reward
        still ahead is the current one, the last page once all are reached.
        """

        rewards = (*self.item_rewards, *self.currency_rewards)
        ahead = [reward.tier for reward in rewards if reward.threshold > amount]
        if ahead:
            return min(ahead)
        return max((reward.tier for reward in rewards), default=0)

    def droppable_collectibles(
        self, progress: Mapping[str, object] | None,
    ) -> tuple[EventCollectible, ...]:
        """The collectibles a kill can drop for a player with ``progress``.

        A FixedBarCollectible bar collects one item per page: the retail
        Dragan event drops the Rise of Dragan key until its first page is
        done, then progress items, then boss progress (2026-10-28 capture).
        """

        # Happy-hour bars count Andermant (money_rc): progress from spending
        # it, never premium currency raining from monsters.
        droppable = tuple(
            value for value in self.collectibles
            if value.item_category not in _CURRENCY_COLLECTIBLE_CATEGORIES
        )
        tiered = [value for value in droppable if value.tier is not None]
        if not tiered:
            return droppable
        tier = self.current_tier(max(0, int((progress or {}).get("amount", 0))))
        reached = [value for value in tiered if value.tier <= tier]
        if not reached:
            return ()
        page = max(value.tier for value in reached)
        return tuple(value for value in reached if value.tier == page)


@dataclass(frozen=True)
class EventTemplateDefinition:
    resource_id: str
    client_name: str
    client_only: bool
    map_local: bool
    minimum_level: int
    maximum_level: int
    map_id: str
    excluded_maps: frozenset[str]
    teleport_target: str
    status_effects: tuple[str, ...]
    progress_bars: tuple[EventProgressBarDefinition, ...]
    category: str = ""
    # _Template_Events.Loot: the list every worthy opponent rolls while the
    # event runs (christmas sugar beets). Contents: data/catalogs/event_loot.json.
    loot_list_id: str = ""


    def progress_bar(
        self,
        player_level: int,
        completed_achievements: Iterable[str] = (),
    ) -> EventProgressBarDefinition | None:
        eligible = tuple(
            bar for bar in self.progress_bars
            if bar.accepts(player_level, completed_achievements)
        )
        if not eligible:
            return None
        return max(eligible, key=lambda bar: (bar.priority, bar.minimum_level))


# Some map EventSets are authored against separate silent event flags even
# though those flags are part of one scheduled event from the operator's
# point of view. Keep these as aliases of the parent schedule so they follow
# its activation and deactivation without requiring separate CMS entries.
SCHEDULED_EVENT_FLAG_ALIASES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "anniversary_event": (
        "e_anniversary_challenge_silent_duria",
        "e_anniversary_challenge_silent_ellonidos",
        "e_anniversary_challenge_silent_lortac",
        "e_anniversary_challenge_silent_myrdosch",
        "e_anniversary_challenge_silent_norselands",
        "e_anniversary_challenge_silent_pw",
        "e_anniversary_challenge_silent_qaizah",
    ),
    "christmas_2012": (
        "christmas_2012_silent",
        "e_chr_calendar_map_silent",
        "event_chr_event_2023_silent",
    ),
    "event_kingshill_sewers_01": (
        "e_ks_01_blitz_payment_offer_silent",
        "e_ks_01_rat_king_silent_cave",
        "e_ks_01_rat_king_silent_crypt",
        "e_ks_01_rat_king_silent_sewers",
        "event_kingshill_sewers_01_challenge_01_cave_silent",
        "event_kingshill_sewers_01_challenge_01_crypt_silent",
        "event_kingshill_sewers_01_key_rats_silent",
    ),
})

def christmas_calendar_day_index(
    starts_at: datetime | None = None,
    now: datetime | None = None,
) -> int:
    """Return the active calendar day (1..25) for the Winter Solstice advent calendar."""
    moment = now or datetime.now(timezone.utc)
    if moment.month == 12:
        return min(25, max(1, moment.day))
    if starts_at is not None:
        start_date = starts_at.date() if isinstance(starts_at, datetime) else moment.date()
        days_passed = (moment.date() - start_date).days
        return min(25, max(1, days_passed + 1))
    return 1


def scheduled_event_flag_names(
    event: "ScheduledEvent", now: datetime | None = None
) -> tuple[str, ...]:
    """Return the flags owned by one scheduled event, including its aliases and dynamic daily flags."""

    resource_id = event.template.resource_id
    aliases = list(SCHEDULED_EVENT_FLAG_ALIASES.get(resource_id.casefold(), ()))
    if resource_id.casefold() == "christmas_2012":
        day = christmas_calendar_day_index(event.starts_at, now)
        aliases.append(f"e_chr_present_{day:02d}")
    return tuple(dict.fromkeys((resource_id, *event.tags, *aliases)))


@dataclass(frozen=True)
class ScheduledEvent:
    event_id: int
    template: EventTemplateDefinition
    starts_at: datetime | None
    ends_at: datetime | None
    forced_state: str
    tags: tuple[str, ...]
    participation_default: bool
    collectible_drop_chance: float
    collectible_amount: int
    collectible_maps: frozenset[str]
    collectible_ranks: frozenset[str]
    # Maps the event owns, derived from static data (see _event_owned_maps);
    # ``collectible_maps`` is the explicit schedule override.
    progress_maps: frozenset[str] = frozenset()

    def state_at(self, now: datetime) -> str:
        if self.forced_state in {"active", "upcoming", "expired"}:
            return self.forced_state
        if self.starts_at is not None and now < self.starts_at:
            return "upcoming"
        if self.ends_at is not None and now >= self.ends_at:
            return "expired"
        return "active"

    def active_at(self, now: datetime) -> bool:
        return self.state_at(now) == "active"


@dataclass(frozen=True)
class TravelItemDefinition:
    template_id: str
    target_map_id: str
    required_level: int
    event_ids: frozenset[str]
    only_during_event: bool


@dataclass(frozen=True)
class EventProgressAdvance:
    event: ScheduledEvent
    progress: dict[str, object]
    item_rewards: tuple[EventReward, ...]
    currency_rewards: tuple[EventReward, ...]


@dataclass(frozen=True)
class EventLootEntry:
    """One item of an event loot list, by monster rank class.

    Rank classes are ``monster``, ``elite`` (guardian, champion, leader) and
    ``boss`` (boss, worldboss); a class without its own values uses
    ``monster``'s. The chance is further scaled by the monster's
    ``_Template_Monster.EventDropChance``, unless the entry names
    ``regions``: then it drops from every monster on maps of those regions
    (or monsters of them), the event-map loot like christmas presents.
    """

    item_id: str
    chances: Mapping[str, float]
    amounts: Mapping[str, tuple[int, int]]
    # _Template_Item.MinDropLevel/MaxDropLevel, against the monster's level.
    minimum_monster_level: int = 1
    maximum_monster_level: int = 0
    regions: frozenset[str] = frozenset()
    # Per map difficulty (``..._Diff_{difficulty:02d}``); the last one serves
    # higher difficulties. Empty: always item_id.
    difficulty_items: tuple[str, ...] = ()
    # Fixed stack size for map difficulties 0..9; the last amount serves any
    # higher difficulty. Used for event drops with an authored tier table.
    difficulty_amounts: tuple[int, ...] = ()

    def item_for_difficulty(self, difficulty: int) -> str:
        if not self.difficulty_items:
            return self.item_id
        return self.difficulty_items[
            max(0, min(int(difficulty), len(self.difficulty_items) - 1))
        ]


@dataclass(frozen=True)
class MapLootEntry:
    """A map-owned drop scoped by monster templates, regions, ranks and event state."""

    loot: EventLootEntry
    monster_templates: frozenset[str]
    monster_regions: frozenset[str]
    monster_ranks: frozenset[str] = frozenset()
    scale_chance_with_difficulty: bool = False
    scale_stack_amount_with_difficulty: bool = True
    difficulty_amounts: tuple[int, ...] = ()
    item_options: tuple[str, ...] = ()
    item_options_by_class: Mapping[str, tuple[str, ...]] = MappingProxyType({})
    require_class_option: bool = False
    minimum_difficulty: int = 0
    required_costumes: frozenset[str] = frozenset()
    require_active_events: frozenset[str] = frozenset()
    require_inactive_events: frozenset[str] = frozenset()
    maximum_difficulty: int | None = None
    # Optional scope for wildcard entries shared across maps (for example all
    # Parallel World boss arenas). A configured region also matches its
    # suffixed map families, such as Parallel_World_grimmag.
    map_regions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class MapLootDrop:
    item_id: str
    amount: int
    scale_stack_amount_with_difficulty: bool


def _rank_class(rank_kind: str) -> str:
    rank = str(rank_kind).casefold()
    if rank in {"boss", "worldboss"}:
        return "boss"
    if rank in {"guardian", "champion", "leader"}:
        return "elite"
    return "monster"


@dataclass(frozen=True)
class DropAmountModifier:
    effect_id: int
    target_item_id: str
    value: str
    mode: str
    max_stack_size: int


@dataclass(frozen=True)
class EventCatalog:
    events: tuple[ScheduledEvent, ...]
    travel_items: Mapping[str, TravelItemDefinition]
    event_map_requirements: Mapping[str, frozenset[str]]
    drop_modifiers: Mapping[int, tuple[DropAmountModifier, ...]]
    # Casefolded template id -> maps it owns (also for unscheduled templates,
    # so a challenge event can fall back to its parent event's maps).
    event_maps: Mapping[str, frozenset[str]] = MappingProxyType({})
    # Map key (map template, access group or access id) -> its Region tokens.
    map_regions: Mapping[str, frozenset[str]] = MappingProxyType({})
    # Casefolded monster template id -> its Region tokens.
    monster_regions: Mapping[str, frozenset[str]] = MappingProxyType({})
    # Loot list id -> entries; casefolded monster id -> EventDropChance.
    event_loot: Mapping[str, tuple[EventLootEntry, ...]] = MappingProxyType({})
    map_loot: Mapping[str, tuple[MapLootEntry, ...]] = MappingProxyType({})
    monster_event_drop_chances: Mapping[str, float] = MappingProxyType({})

    def active_events(self, now: datetime | None = None) -> tuple[ScheduledEvent, ...]:
        moment = now or datetime.now(timezone.utc)
        return tuple(event for event in self.events if event.active_at(moment))

    def active_template_ids(self, now: datetime | None = None) -> frozenset[str]:
        return frozenset(
            event.template.resource_id.casefold()
            for event in self.active_events(now)
        )

    def active_event_flags(self, now: datetime | None = None) -> frozenset[str]:
        """Names understood by map EventSets and other authored gates."""
        moment = now or datetime.now(timezone.utc)
        return frozenset(
            value.casefold()
            for event in self.active_events(moment)
            for value in scheduled_event_flag_names(event, moment)
        )

    def event_update(self, now: datetime | None = None) -> EventUpdate:
        moment = now or datetime.now(timezone.utc)
        entries = []
        for event in self.events:
            state = event.state_at(moment)
            if state == "expired":
                continue
            is_active = state == "active"
            event_tags = list(event.tags)
            event_tags.extend(
                SCHEDULED_EVENT_FLAG_ALIASES.get(
                    event.template.resource_id.casefold(), ()
                )
            )
            if event.template.resource_id.casefold() == "christmas_2012" and is_active:
                day = christmas_calendar_day_index(event.starts_at, moment)
                day_flag = f"e_chr_present_{day:02d}"
                event_tags.append(day_flag)
                # Client only resolves EventSets from EventUpdateEntry.event_name.
                # Emit a dedicated entry for today's present so the client instantiates
                # the authored present StateObject and enables its daily champion spawn.
                entries.append(EventUpdateEntry(
                    event_id=0,
                    event_name=day_flag,
                    flags=(True, False, False, False),
                    starts_at=_wire_time(event.starts_at),
                    ends_at=_wire_time(event.ends_at),
                    tags=(),
                ))
            entries.append(EventUpdateEntry(
                event_id=event.event_id,
                event_name=event.template.resource_id,
                flags=(is_active, event.template.map_local, False, False),
                starts_at=_wire_time(event.starts_at),
                ends_at=_wire_time(event.ends_at),
                tags=tuple(dict.fromkeys(event_tags)),
            ))
        return EventUpdate(tuple(entries))

    def progress_info(
        self,
        progress: Mapping[str, Mapping[str, object]],
        *,
        player_level: int,
        completed_achievements: Iterable[str] = (),
        now: datetime | None = None,
    ) -> EventProgressInfo:
        result = []
        active_events = self.active_events(now)
        # The retail mini-kill variants all use the same
        # ``mini_kill_progress_item``. The client can only show one progress
        # bar for that collectible, so keep the Mantis bar as the shared bar
        # while it is active; otherwise use the first active mini-kill event.
        mini_kill_events = tuple(
            event for event in active_events
            if event.template.resource_id.casefold().startswith("mini_kill_event_")
            and event.template.progress_bar(
                player_level, completed_achievements
            ) is not None
            and any(
                collectible.item_id.casefold() == "mini_kill_progress_item"
                for bar in event.template.progress_bars
                for collectible in bar.collectibles
            )
        )
        mini_kill_owner = next((
            event for event in mini_kill_events
            if event.template.resource_id.casefold() == "mini_kill_event_1d_mantis"
        ), mini_kill_events[0] if mini_kill_events else None)
        for event in active_events:
            if (
                event.template.resource_id.casefold().startswith("mini_kill_event_")
                and mini_kill_owner is not None
                and event.event_id != mini_kill_owner.event_id
            ):
                continue
            if event.template.progress_bar(
                player_level, completed_achievements
            ) is None:
                continue
            state = progress.get(str(event.event_id), {})
            bar = event.template.progress_bar(
                player_level, completed_achievements
            )
            if (
                bar is not None
                and event.template.resource_id.casefold() == "anniversary_event"
                and any(
                    value.item_id.casefold().startswith(
                        "e_anniversary_main_progress_"
                    )
                    and value.tier is not None
                    for value in bar.collectibles
                )
            ):
                # One cumulative counter advances through regional pages.
                # The wire group selects the template, not the regional page.
                wire_amount = int(bar.anniversary_state(state)["amount"])
                item_tiers = [
                    reward.tier for reward in bar.item_rewards
                    if reward.threshold <= wire_amount
                ]
                currency_tiers = [
                    reward.tier for reward in bar.currency_rewards
                    if reward.threshold <= wire_amount
                ]
                result.append(EventProgressInfoEntry(
                    event.event_id,
                    (
                        wire_amount,
                        max(item_tiers) + 1 if item_tiers else UINT32_MAX,
                        max(currency_tiers) if currency_tiers else UINT32_MAX,
                        0,
                    ),
                ))
                continue
            result.append(EventProgressInfoEntry(
                event.event_id,
                (
                    max(0, int(state.get("amount", 0))),
                    int(state.get("level", UINT32_MAX)) & UINT32_MAX,
                    int(state.get("currency_level", UINT32_MAX)) & UINT32_MAX,
                    max(0, int(state.get("group", 0))),
                ),
            ))
        return EventProgressInfo(tuple(result))

    def participation_info(
        self,
        participation: Mapping[str, bool],
        *,
        now: datetime | None = None,
    ) -> EventParticipationInfo:
        values = tuple(
            EventParticipation(
                event.event_id,
                bool(participation.get(
                    str(event.event_id), event.participation_default
                )),
            )
            for event in self.active_events(now)
            if not event.participation_default
            or str(event.event_id) in participation
        )
        return EventParticipationInfo(values)

    def travel_item(self, template_id: str) -> TravelItemDefinition | None:
        return self.travel_items.get(str(template_id))

    def travel_item_available(
        self,
        definition: TravelItemDefinition,
        *,
        player_level: int,
        now: datetime | None = None,
    ) -> bool:
        if player_level < definition.required_level:
            return False
        if definition.template_id.casefold() in YEAR_ROUND_MOON_TRAVEL_ITEMS:
            return True
        if not definition.event_ids:
            return True
        active = self.active_template_ids(now)
        return bool(definition.event_ids & active)

    def map_available(self, map_id: str, now: datetime | None = None) -> bool:
        if _map_key(map_id) in YEAR_ROUND_MOON_EVENT_MAPS:
            return True
        requirements = self.event_map_requirements.get(_map_key(map_id))
        if not requirements:
            return True
        return bool(requirements & self.active_template_ids(now))

    def event_for_collectible(
        self, item_id: str, *, now: datetime | None = None,
        player_level: int | None = None,
        completed_achievements: Iterable[str] = (),
    ) -> tuple[ScheduledEvent, EventProgressBarDefinition] | None:
        active_events = self.active_events(now)
        matching_events = tuple(
            event for event in active_events
            if (
                player_level is None
                or event.template.progress_bar(
                    player_level, completed_achievements
                ) is not None
            )
            if any(
                value.item_id == item_id
                for bar in event.template.progress_bars
                for value in bar.collectibles
            )
        )
        if str(item_id).casefold() == "mini_kill_progress_item":
            mantis = next((
                event for event in matching_events
                if event.template.resource_id.casefold() == "mini_kill_event_1d_mantis"
            ), None)
            if mantis is not None:
                matching_events = (mantis,)
        for event in matching_events:
            for bar in event.template.progress_bars:
                if any(value.item_id == item_id for value in bar.collectibles):
                    return event, bar
        return None

    def is_event_collectible(self, item_id: str) -> bool:
        """Return true even when the owning event is not currently active."""

        return any(
            value.item_id == str(item_id)
            for event in self.events
            for bar in event.template.progress_bars
            for value in bar.collectibles
        )

    def collectible_drops(
        self,
        *,
        map_id: str,
        player_level: int,
        monster_rank: str,
        participation: Mapping[str, bool],
        status_effects: Iterable[StatusEffectEntry],
        rng: random.Random,
        now: datetime | None = None,
        map_aliases: Iterable[str] = (),
        monster_template_id: str = "",
        progress: Mapping[str, Mapping[str, object]] = MappingProxyType({}),
        completed_achievements: Iterable[str] = (),
        map_difficulty: int = 0,
        active_costume: str = "",
    ) -> tuple[tuple[str, int], ...]:
        rank = str(monster_rank).casefold()
        map_ids = (map_id, *tuple(map_aliases))
        # The public catalog is also used by tooling and protocol tests. Be
        # defensive about the runtime store's ``(entry, expires_at)`` shape so
        # malformed optional modifier state can never suppress base event
        # progress drops or the rest of a monster's loot transaction.
        active_effects = tuple(
            entry
            for value in status_effects
            for entry in (
                value
                if hasattr(value, "effect_id")
                else (
                    value[0]
                    if isinstance(value, tuple)
                    and value
                    and hasattr(value[0], "effect_id")
                    else None
                ),
            )
            if entry is not None
        )
        result = []
        seen_mini_kill_collectibles: set[str] = set()
        for event, collectible in self._collectibles_here(
            map_id=map_id,
            map_aliases=map_ids[1:],
            player_level=player_level,
            participation=participation,
            progress=progress,
            now=now,
            monster_template_id=monster_template_id,
            completed_achievements=completed_achievements,
        ):
            if event.template.resource_id.casefold().startswith("mini_kill_event_"):
                item_key = collectible.item_id.casefold()
                if item_key == "mini_kill_progress_item":
                    owner = self.event_for_collectible(
                        collectible.item_id, now=now,
                        player_level=player_level,
                        completed_achievements=completed_achievements,
                    )
                    if owner is not None and event.event_id != owner[0].event_id:
                        continue
                    if item_key in seen_mini_kill_collectibles:
                        continue
                    seen_mini_kill_collectibles.add(item_key)
            if event.collectible_ranks and rank not in event.collectible_ranks:
                continue
            if (
                event.template.resource_id.casefold()
                == "event_kingshill_sewers_01"
                and _map_key(map_id) == "e_ks_01_boss_01"
                and collectible.item_id.casefold()
                == "e_ks_01_event_progress_t0"
                and rank == "worldboss"
            ):
                # The Sewers beneath the Castle final-boss progress is paid
                # by its opened reward chest instead of directly by the kill.
                continue
            sewers_blitz_worm_progress = (
                event.template.resource_id.casefold().startswith(
                    "e_ks_blitz_"
                )
                and collectible.item_id.casefold()
                == "e_ks_blitz_01_event_progress_01"
                and str(monster_template_id).casefold()
                == "e_ks_blitz_champion_worm_01"
            )
            doe_kia_krisal_boss_progress = (
                event.template.resource_id.casefold()
                == "desert_of_essences_02"
                and rank == "sentinel"
                and str(monster_template_id).casefold()
                in _DOE_KIA_KRISAL_BOSS_TEMPLATES
                and any(
                    _map_key(value) == "e_doe_map_02"
                    or _map_key(value).startswith("e_doe_map_02_")
                    for value in map_ids
                )
            )
            # An event map's final boss (Big Paws, Dragan, Wendigo...) always
            # pays out; generate_monster_loot sizes its stack. The Sewers Blitz
            # champion worm is also guaranteed its event progression; its
            # authored rank is Champion, so the generic 35% collectible roll
            # would otherwise silently skip most kills.
            if (
                rank != "worldboss"
                and not sewers_blitz_worm_progress
                and not doe_kia_krisal_boss_progress
                and rng.random() > event.collectible_drop_chance
            ):
                continue
            base = max(
                1,
                int(event.collectible_amount),
                int(collectible.base_value),
            )
            if event.template.resource_id.casefold() == "event_kingshill_sewers_01":
                base = _kingshill_sewers_progress_amount(
                    monster_rank=rank,
                    map_difficulty=map_difficulty,
                )
            elif doe_kia_krisal_boss_progress:
                base = _DOE_KIA_KRISAL_BOSS_PROGRESS_AMOUNTS[max(
                    0,
                    min(
                        int(map_difficulty or 0),
                        len(_DOE_KIA_KRISAL_BOSS_PROGRESS_AMOUNTS) - 1,
                    ),
                )]
            elif sewers_blitz_worm_progress:
                difficulty_index = _KINGSHILL_SEWERS_DIFFICULTY_INDEX_MAP.get(
                    int(map_difficulty or 0), 0
                )
                blitz_amount = (
                    _KINGSHILL_SEWERS_BLITZ_WORM_PROGRESS_AMOUNTS[
                        difficulty_index
                    ]
                )
                if blitz_amount is not None:
                    base = blitz_amount
            elif event.template.resource_id.casefold() == "dragan_event_03":
                base = _dragan_progress_amount(
                    item_id=collectible.item_id,
                    map_ids=map_ids,
                    monster_rank=rank,
                    map_difficulty=map_difficulty,
                    rng=rng,
                ) or base
                # The worn Dragan Event Attire increases the stack sizes of
                # Elemental and Raven Pearls. Round half up to match item
                # stack quantities instead of Python's ties-to-even round.
                if (
                    str(active_costume or "").casefold()
                    in _DRAGAN_ATTIRE_COSTUMES
                ):
                    collectible_id = collectible.item_id.casefold()
                    if collectible_id == "e_dra_progress_item":
                        base = int(base * 1.5 + 0.5)
                    elif collectible_id == "e_dra_progress_item_boss":
                        base = int(base * 1.1 + 0.5)
            elif (
                event.template.resource_id.casefold() == "sargon_event"
                and str(active_costume or "").casefold()
                in _SARGON_ATTIRE_COSTUMES
                and collectible.item_id.casefold() == "e_sar_progress_item"
            ):
                # The Terrifying Shadows attire increases Shadow stack size
                # by 50%, with a minimum increase of one item.
                base = max(base + 1, int(base * 1.5 + 0.5))
            elif event.template.resource_id.casefold() == "christmas_2012":
                chr_amount = _christmas_progress_amount(
                    item_id=collectible.item_id,
                    monster_rank=rank,
                    map_difficulty=map_difficulty,
                    monster_template_id=monster_template_id,
                )
                if chr_amount is not None:
                    base = chr_amount
                # Winter Solstice Festival Attire increases Joy of Winter Solstice Festival stack size by 50%
                if (
                    str(active_costume or "").casefold() in _CHRISTMAS_ATTIRE_COSTUMES
                    and collectible.item_id.casefold() == "e_chr_progress_item"
                ):
                    base = int(base * 1.5 + 0.5)
            amount = self._modified_drop_amount(
                collectible.item_id, base, active_effects
            )
            result.append((collectible.item_id, amount))
        return tuple(result)

    def map_event_collectibles(
        self,
        *,
        map_id: str,
        player_level: int,
        participation: Mapping[str, bool],
        progress: Mapping[str, Mapping[str, object]] = MappingProxyType({}),
        completed_achievements: Iterable[str] = (),
        now: datetime | None = None,
        map_aliases: Iterable[str] = (),
    ) -> dict[str, tuple[str, ...]]:
        """Events whose progress applies on this map, with what a chest drops.

        Keys are casefolded event template ids, values the collectibles this
        player is collecting there (a paged bar's current page). Map chests
        (amphorae) drop these outright: unlike kills, no per-event drop
        chance or monster rank applies.
        """

        result: dict[str, list[str]] = {}
        for event, collectible in self._collectibles_here(
            map_id=map_id,
            map_aliases=map_aliases,
            player_level=player_level,
            participation=participation,
            progress=progress,
            now=now,
            completed_achievements=completed_achievements,
        ):
            items = result.setdefault(event.template.resource_id.casefold(), [])
            if collectible.item_id not in items:
                items.append(collectible.item_id)
        return {key: tuple(value) for key, value in result.items()}

    def _collectibles_here(
        self,
        *,
        map_id: str,
        map_aliases: Iterable[str],
        player_level: int,
        participation: Mapping[str, bool],
        progress: Mapping[str, Mapping[str, object]],
        now: datetime | None,
        monster_template_id: str = "",
        completed_achievements: Iterable[str] = (),
    ) -> Iterator[tuple[ScheduledEvent, EventCollectible]]:
        """Progress collectibles a drop on this map advances, per event."""

        # An instance is known by its physical map, access group and access
        # id; any of them can be the name static data uses for it.
        current_maps = frozenset(
            key
            for key in (_map_key(map_id), *(_map_key(v) for v in map_aliases))
            if key
        )
        loot_scopes = current_maps.union(
            *(self.map_regions.get(key, ()) for key in current_maps),
            self.monster_regions.get(
                str(monster_template_id).casefold(), ()
            ),
            (_map_key(monster_template_id),),
        )
        for event in self.active_events(now):
            template = event.template
            if not bool(participation.get(
                str(event.event_id), event.participation_default
            )):
                continue
            if not template.minimum_level <= player_level <= template.maximum_level:
                continue
            if current_maps & template.excluded_maps:
                continue
            if template.map_id and _map_key(template.map_id) not in current_maps:
                continue
            if template.resource_id.casefold() == "anniversary_event":
                # Regional arena rewards and forfeit chests grant Anniversary
                # progress explicitly. A kill/drop scan cannot infer one
                # region from these unscoped collectible template rows and
                # would award all seventeen items on the same map.
                continue
            bar = template.progress_bar(
                player_level, completed_achievements
            )
            if bar is None or not bar.monster_drops:
                continue
            for collectible in bar.droppable_collectibles(
                progress.get(str(event.event_id))
            ):
                if self._collectible_in_scope(
                    event, collectible, current_maps, loot_scopes
                ):
                    yield event, collectible

    def _collectible_in_scope(
        self,
        event: ScheduledEvent,
        collectible: EventCollectible,
        current_maps: frozenset[str],
        loot_scopes: frozenset[str],
    ) -> bool:
        """Whether a kill here may drop this event's progress collectible.

        Progress belongs to the event's own content (the Winter Summit for
        christmas honey cookies), while the "worthy opponents everywhere"
        drops are the event's Loot list. In order of authority:
        an explicit schedule ``collectible_maps``; the item's Region, or the
        one data/catalogs/event_item_regions.json authors for it, matched
        against the map, its regions, the monster and the monster's regions
        (or the event's own maps, which covers first-stage maps an item
        Region omits); the maps the event owns; the maps of the event family
        named by the item's EventId (challenge events reuse their parent's
        collectible). With none of those, only an event naming a Loot list
        hunts worthy opponents everywhere (Monster Hunt); any other event's
        progress does not come from kills (Live Stream's bonus codes).
        """

        if event.collectible_maps:
            return bool(event.collectible_maps & current_maps)
        own = event.progress_maps
        if collectible.maps:
            return bool(collectible.maps & loot_scopes or own & current_maps)
        if own:
            return bool(own & current_maps)
        family = frozenset().union(*(
            self.event_maps.get(owner, ())
            for owner in collectible.owner_events
        ))
        if family:
            return bool(family & current_maps)
        return bool(event.template.loot_list_id)

    def event_loot_drops(
        self,
        *,
        map_id: str,
        player_level: int,
        monster_template_id: str,
        monster_rank: str,
        monster_level: int,
        participation: Mapping[str, bool],
        rng: random.Random,
        now: datetime | None = None,
        map_aliases: Iterable[str] = (),
        map_difficulty: int = 0,
    ) -> tuple[tuple[str, int], ...]:
        """Roll the Loot lists of the active events for one kill.

        These are the "collect X from worthy opponents" drops (christmas
        sugar beets): any map the event does not exclude, from monsters whose
        authored EventDropChance is above zero, so event-dungeon monsters,
        adds and summons drop none. Entries with ``regions`` are the event
        maps' own loot instead (christmas presents on the e_chr maps).
        """

        worthiness = self.monster_event_drop_chances.get(
            str(monster_template_id).casefold(), 0.0
        )
        current_maps = frozenset(
            key
            for key in (_map_key(map_id), *(_map_key(v) for v in map_aliases))
            if key
        )
        loot_scopes = current_maps.union(
            *(self.map_regions.get(key, ()) for key in current_maps),
            self.monster_regions.get(str(monster_template_id).casefold(), ()),
        )
        rank = _rank_class(monster_rank)
        raw_rank = str(monster_rank).casefold()
        result = []
        for event in self.active_events(now):
            template = event.template
            is_sargon_event = (
                template.resource_id.casefold() == "sargon_event"
            )
            entries = self.event_loot.get(template.loot_list_id.casefold(), ())
            if not bool(participation.get(
                str(event.event_id), event.participation_default
            )):
                continue
            if not template.minimum_level <= player_level <= template.maximum_level:
                continue
            if is_sargon_event:
                if raw_rank == "worldboss":
                    difficulty = max(
                        0,
                        min(
                            int(map_difficulty),
                            len(_SARGON_WORLD_BOSS_COFFEE_BEANS_BY_DIFFICULTY)
                            - 1,
                        ),
                    )
                    result.append((
                        "e_sar_key_coffee_beans",
                        _SARGON_WORLD_BOSS_COFFEE_BEANS_BY_DIFFICULTY[
                            difficulty
                        ],
                    ))
                elif rng.random() < 0.05:
                    result.append(("e_sar_key_coffee_beans", 1))
            if not entries:
                continue
            if current_maps & template.excluded_maps:
                continue
            if template.map_id and _map_key(template.map_id) not in current_maps:
                continue
            for entry in entries:
                # The Sargon Coffee Bean drop is governed above so it can
                # also be guaranteed for world bosses with the configured
                # difficulty stack. Avoid a second roll from authored loot.
                if (
                    is_sargon_event
                    and entry.item_id.casefold() == "e_sar_key_coffee_beans"
                ):
                    continue
                if monster_level < entry.minimum_monster_level or (
                    entry.maximum_monster_level
                    and monster_level > entry.maximum_monster_level
                ):
                    continue
                if entry.regions:
                    if not entry.regions & loot_scopes:
                        continue
                    scale = 1.0
                elif worthiness <= 0.0:
                    continue
                else:
                    scale = worthiness
                chance = entry.chances.get(rank, entry.chances.get("monster", 0.0))
                if rng.random() >= chance * scale:
                    continue
                low, high = entry.amounts.get(
                    rank, entry.amounts.get("monster", (1, 1))
                )
                amount = (
                    entry.difficulty_amounts[
                        max(0, min(int(map_difficulty), len(entry.difficulty_amounts) - 1))
                    ]
                    if entry.difficulty_amounts
                    else rng.randint(low, high)
                )
                result.append((
                    entry.item_for_difficulty(map_difficulty),
                    amount,
                ))
        return tuple(result)

    def map_loot_drops(
        self,
        *,
        map_id: str,
        monster_template_id: str,
        monster_rank: str,
        monster_level: int,
        rng: random.Random,
        class_name: str = "",
        map_aliases: Iterable[str] = (),
        map_difficulty: int = 0,
        active_costume: str = "",
        privileged_difficulty_multiplier: float = 1.0,
        drop_chance_bonus_by_item: Mapping[str, float] = MappingProxyType({}),
        now: datetime | None = None,
    ) -> tuple[MapLootDrop, ...]:
        """Roll authored drops belonging to these maps, independent of schedule."""

        current_maps = frozenset(
            key
            for key in (_map_key(map_id), *(_map_key(value) for value in map_aliases))
            if key
        )
        template_id = str(monster_template_id).casefold()
        monster_regions = self.monster_regions.get(template_id, frozenset())
        raw_rank = str(monster_rank).casefold()
        rank = _rank_class(raw_rank)
        active_templates = self.active_template_ids(now)
        current_costume = str(active_costume or "").casefold()
        current_map_regions = frozenset().union(*(
            self.map_regions.get(current_map, ()) for current_map in current_maps
        ))
        result = []
        scoped_entries = [
            (current_map, entry)
            for current_map in current_maps
            for entry in self.map_loot.get(current_map, ())
        ]
        scoped_entries.extend(
            ("*", entry) for entry in self.map_loot.get("*", ())
        )
        for _scope, entry in scoped_entries:
            if int(map_difficulty) < entry.minimum_difficulty:
                continue
            if (
                entry.maximum_difficulty is not None
                and int(map_difficulty) > entry.maximum_difficulty
            ):
                continue
            if entry.required_costumes and current_costume not in entry.required_costumes:
                continue
            if entry.map_regions and not any(
                actual == expected
                or actual.startswith(f"{expected}_")
                for actual in current_map_regions
                for expected in entry.map_regions
            ):
                continue
            if not entry.require_active_events.issubset(active_templates):
                continue
            if entry.require_inactive_events & active_templates:
                continue
            if entry.monster_ranks and (
                raw_rank not in entry.monster_ranks
                and rank not in entry.monster_ranks
            ):
                continue
            if entry.monster_templates and template_id not in entry.monster_templates:
                continue
            if entry.monster_regions and not (
                entry.monster_regions & monster_regions
            ):
                continue
            loot = entry.loot
            if monster_level < loot.minimum_monster_level or (
                loot.maximum_monster_level
                and monster_level > loot.maximum_monster_level
            ):
                continue
            chance = loot.chances.get(
                raw_rank,
                loot.chances.get(rank, loot.chances.get("monster", 0.0)),
            )
            chance *= max(
                0.0,
                1.0 + float(drop_chance_bonus_by_item.get(
                    loot.item_id.casefold(), 0.0,
                )),
            )
            if entry.scale_chance_with_difficulty:
                chance *= max(0.0, float(privileged_difficulty_multiplier))
            if rng.random() >= min(1.0, chance):
                continue
            if entry.difficulty_amounts:
                amount = entry.difficulty_amounts[
                    max(0, min(
                        int(map_difficulty),
                        len(entry.difficulty_amounts) - 1,
                    ))
                ]
            else:
                low, high = loot.amounts.get(
                    raw_rank,
                    loot.amounts.get(rank, loot.amounts.get("monster", (1, 1))),
                )
                amount = rng.randint(low, high)
            item_options = entry.item_options_by_class.get(
                str(class_name).casefold(), ()
            )
            if not item_options and entry.item_options:
                item_options = entry.item_options
            if (
                not item_options
                and entry.item_options_by_class
                and entry.require_class_option
            ):
                continue
            if not item_options and entry.item_options_by_class:
                item_options = tuple(
                    item_id
                    for options in entry.item_options_by_class.values()
                    for item_id in options
                )
            result.append(MapLootDrop(
                rng.choice(item_options)
                if item_options
                else loot.item_for_difficulty(map_difficulty),
                amount,
                entry.scale_stack_amount_with_difficulty,
            ))
        return tuple(result)

    def _modified_drop_amount(
        self,
        item_id: str,
        base: int,
        status_effects: tuple[StatusEffectEntry, ...],
    ) -> int:
        absolute = 0.0
        relative = 0.0
        for effect in status_effects:
            for modifier in self.drop_modifiers.get(int(effect.effect_id), ()):
                if modifier.target_item_id != item_id:
                    continue
                stack_count = min(
                    max(1, int(getattr(effect, 'stack_index', 0)) + 1),
                    max(1, int(modifier.max_stack_size)),
                )
                raw = modifier.value.strip()
                if raw.startswith("$") and raw[1:].isdigit():
                    index = int(raw[1:])
                    value = (
                        float(effect.values[index])
                        if index < len(effect.values) else 0.0
                    )
                else:
                    try:
                        value = float(raw)
                    except ValueError:
                        continue
                if modifier.mode == "relative":
                    relative += value * stack_count
                else:
                    absolute += value * stack_count
        return max(1, int(math.floor(base * (1.0 + relative) + absolute + 0.5)))

    def advance(
        self,
        progress: Mapping[str, Mapping[str, object]],
        *,
        collectible_id: str,
        amount: int,
        player_level: int,
        class_name: str,
        completed_achievements: Iterable[str] = (),
        now: datetime | None = None,
    ) -> EventProgressAdvance | None:
        found = self.event_for_collectible(
            collectible_id, now=now, player_level=player_level,
            completed_achievements=completed_achievements,
        )
        if found is None:
            return None
        event, matching_bar = found
        bar = (
            matching_bar
            if event.template.resource_id.casefold() == "anniversary_event"
            else event.template.progress_bar(player_level, completed_achievements)
        )
        if bar is None or not any(
            value.item_id == collectible_id for value in bar.collectibles
        ):
            return None
        key = str(event.event_id)
        prior = dict(progress.get(key, {}))
        anniversary_group = None
        if (
            event.template.resource_id.casefold() == "anniversary_event"
            and str(collectible_id).casefold().startswith(
                "e_anniversary_main_progress_"
            )
        ):
            collectible = next(
                value for value in bar.collectibles
                if value.item_id == collectible_id
            )
            if collectible.tier is None:
                return None
            anniversary_group = max(0, int(collectible.tier))
            old = bar.anniversary_state(prior)
            current_page = bar.current_tier(int(old["amount"]))
            if (
                anniversary_group != current_page
                or int(old["amount"]) >= bar.page_end(current_page)
            ):
                return None
        else:
            old = prior
        previous = max(0, int(old.get("amount", 0)))
        current = min(UINT32_MAX, previous + max(0, int(amount)))
        completion_threshold = max((
            int(reward.threshold)
            for reward in (*bar.item_rewards, *bar.currency_rewards)
        ), default=0)
        if completion_threshold > 0:
            # Stop exactly on the bar's final authored reward threshold. This
            # also preserves the short remainder when a chest's progress award
            # is larger than the amount left (for example 530 + 10 on a 537
            # point bar becomes 537, so the client can disable the completed
            # bar without losing those last seven points).
            current = min(current, completion_threshold)
        if anniversary_group is not None:
            # Excess Duria progress must not fill Norselands. Unlock the next
            # page at this boundary and require its own collectible next time.
            current = min(current, bar.page_end(anniversary_group))
        claimed = {str(value) for value in old.get("claimed", ())}

        def crossed(reward: EventReward) -> bool:
            # When Dragan's achievement gate is ignored to match the client,
            # players already progressing on the old level-100 bar need any
            # newly eligible top-bar milestones up to their current amount.
            # Claimed reward keys still prevent duplicate delivery.
            threshold = reward.threshold
            reached = threshold <= current and (
                previous < threshold
                or event.template.resource_id.casefold() == "dragan_event_03"
            )
            return reached and reward.key not in claimed

        item_rewards = tuple(
            reward for reward in bar.item_rewards
            if (
                (anniversary_group is None or reward.tier == anniversary_group)
                and crossed(reward)
                and reward.item_for_class(class_name)
            )
        )
        currency_rewards = tuple(
            reward for reward in bar.currency_rewards
            if (
                (anniversary_group is None or reward.tier == anniversary_group)
                and crossed(reward)
            )
        )
        claimed.update(reward.key for reward in item_rewards)
        claimed.update(reward.key for reward in currency_rewards)

        reached_item_tiers = [
            reward.tier for reward in bar.item_rewards
            if reward.threshold <= current
        ]
        reached_currency_tiers = [
            reward.tier for reward in bar.currency_rewards
            if reward.threshold <= current
        ]
        item_level = (
            max(reached_item_tiers) + 1
            if reached_item_tiers else UINT32_MAX
        )
        currency_level = (
            max(reached_currency_tiers)
            if reached_currency_tiers else UINT32_MAX
        )
        state = {
            "template_id": event.template.resource_id,
            "amount": current,
            # Retail uses UINT_MAX for no progress and one-based item pages.
            "level": item_level,
            "currency_level": currency_level,
            "group": 0,
            "claimed": sorted(claimed),
        }
        if anniversary_group is not None:
            state["progress_model"] = "sequential"
        return EventProgressAdvance(event, state, item_rewards, currency_rewards)


def _parse_reward_entries(value: object, *, currency: bool) -> tuple[EventReward, ...]:
    rewards = []
    # Item rewards use commas inside a single class-conditioned choice
    # (``Warrior|foo,Ranger|bar``), while semicolons separate reward tiers.
    # Currency rewards have no class choices and occur with either delimiter
    # in different generations of the static data.
    raw_entries = (
        re.split(r"[;,]", str(value or ""))
        if currency else str(value or "").split(";")
    )
    for index, raw in enumerate(entry.strip() for entry in raw_entries if entry.strip()):
        parts = raw.split(":")
        try:
            if currency:
                if len(parts) != 2:
                    continue
                tier_threshold, amount_text = parts
                item_text = ""
            else:
                if len(parts) < 3:
                    continue
                tier_threshold, item_text, amount_text = parts[0], parts[1], parts[2]
            if "/" in tier_threshold:
                tier_text, threshold_text = tier_threshold.split("/", 1)
                tier = int(tier_text)
            else:
                tier = 0
                threshold_text = tier_threshold
            threshold = max(0, int(threshold_text))
            amount = max(1, int(amount_text.rstrip(",")))
        except ValueError:
            continue
        options: dict[str, str] = {}
        if not currency:
            for option in item_text.split(","):
                option = option.strip()
                if not option:
                    continue
                if "|" in option:
                    class_name, item_id = option.split("|", 1)
                    options[class_name.casefold()] = item_id
                else:
                    options[""] = option
        rewards.append(EventReward(
            key=f"{'currency' if currency else 'item'}:{index}:{threshold}",
            tier=tier,
            threshold=threshold,
            item_options=MappingProxyType(options),
            amount=amount,
            currency=currency,
        ))
    return tuple(sorted(rewards, key=lambda reward: reward.threshold))


def _normalize_reward_item_templates(
    value: str,
    item_rows: Mapping[str, Mapping[str, object]],
) -> str:
    """Repair reward IDs that accidentally include a chest display suffix.

    Some client static event-bar rows store an item ID followed by the word
    ``Chest`` (for example ``e_anniversary_arena_key_01 Chest``), although
    that template does not exist and the un-suffixed item does. Keep the
    static database read-only and normalize only when the exact ID is absent
    and the candidate template is present.
    """

    valid_items = set(item_rows)
    normalized_entries = []
    for raw in str(value or "").split(";"):
        parts = raw.split(":")
        if len(parts) >= 3:
            options = []
            for option in parts[1].split(","):
                option = option.strip()
                if not option:
                    options.append(option)
                    continue
                class_name, separator, item_id = option.partition("|")
                candidate = item_id if separator else option
                if (
                    candidate not in valid_items
                    and candidate.casefold().endswith(" chest")
                ):
                    base_item = candidate[:-len(" chest")].rstrip()
                    if base_item in valid_items:
                        candidate = base_item
                options.append(
                    f"{class_name}|{candidate}" if separator else candidate
                )
            parts[1] = ",".join(options)
        normalized_entries.append(":".join(parts))
    return ";".join(normalized_entries)


def _event_owned_maps(
    templates: Mapping[str, EventTemplateDefinition],
    item_rows: Mapping[str, Mapping[str, object]],
    map_rows: Iterable[Mapping[str, object]],
    access_rows: Iterable[Mapping[str, object]],
    travel_maps: Mapping[str, set[str]],
    scripted_maps: Mapping[str, set[str]] | None = None,
    exit_maps: Mapping[str, frozenset[str]] | None = None,
) -> dict[str, frozenset[str]]:
    """Map keys each event template owns, from static data alone.

    Seeds are the template's Map, its TravelItem targets, access groups that
    require the event, and access groups entered with a key item of the
    event (the Winter Summit takes christmas sugar beets). Event-type maps
    sharing a Region with a seed join it: the christmas maps are all region
    ``e_chr``, so its calendar and Kranparus dungeons count too.

    An event with none of those falls back to ``scripted_maps``, the maps
    whose ``<map>_<event>.eve`` script runs for it: Helios trademarks belong
    to its PvP arenas. Only then, as most seasonal events script every hub.
    """

    known = {resource_id.casefold() for resource_id in templates}
    seeds: dict[str, set[str]] = {
        event_id: set(maps) for event_id, maps in travel_maps.items()
    }
    for resource_id, template in templates.items():
        if template.map_id:
            seeds.setdefault(resource_id.casefold(), set()).add(
                _map_key(template.map_id)
            )

    group_maps: dict[str, set[str]] = {}
    event_maps_by_region: dict[str, set[str]] = {}
    map_info: dict[str, list[tuple[str, frozenset[str]]]] = {}
    for row in map_rows:
        key = _map_key(row.get("MapTemplate"))
        group = _map_key(row.get("AccessGroup"))
        map_type = str(row.get("MapType") or "")
        regions = frozenset(value.casefold() for value in _tokens(row.get("Region")))
        if group:
            group_maps.setdefault(group, set()).update((key, group))
        map_info.setdefault(key, []).append((map_type, regions))
    for key, variants in map_info.items():
        # Regional variants give a shared arena (the anniversary arena has
        # one row per themed region) a row in many families; only a map of
        # a single region belongs to that region's family.
        regions = frozenset().union(*(
            regions for map_type, regions in variants if map_type == "Event"
        ))
        if len(regions) == 1:
            event_maps_by_region.setdefault(next(iter(regions)), set()).add(key)
    for row in access_rows:
        group = _map_key(row.get("AccessGroup"))
        if group:
            group_maps.setdefault(group, {group}).add(_map_key(row.get("Id")))

    for row in access_rows:
        group = _map_key(row.get("AccessGroup"))
        owners = {
            value.casefold()
            for value in (
                *_tokens(row.get("RequiredActiveEvent")),
                *_tokens(row.get("EventId")),
            )
        }
        for key_entry in (
            *_tokens(row.get("KeyId")), *_tokens(row.get("TeleportKeyId")),
        ):
            item = item_rows.get(key_entry.split(":", 1)[0].strip())
            if item is not None:
                owners.update(
                    value.casefold() for value in _tokens(item.get("EventId"))
                )
        for owner in owners & known:
            seeds.setdefault(owner, set()).update(
                group_maps.get(group, ()), (_map_key(row.get("Id")),)
            )
    # Region-scoped collectibles (the sewer blitz worm's progress) already
    # say where they drop; a script map would widen them to every monster.
    unscoped = {
        resource_id.casefold()
        for resource_id, template in templates.items()
        if any(
            not collectible.maps
            for bar in template.progress_bars
            for collectible in bar.collectibles
        )
    }
    exits = {
        event_id: maps for event_id, maps in (exit_maps or {}).items()
        if event_id in known
    }
    for event_id, maps in (scripted_maps or {}).items():
        if (
            event_id in unscoped
            and not seeds.get(event_id)
            and not exits.get(event_id)
        ):
            seeds[event_id] = set(maps)

    owned: dict[str, frozenset[str]] = {}
    for event_id, maps in seeds.items():
        expanded = set(maps)
        for key in maps:
            expanded.update(group_maps.get(key, ()))
            for map_type, regions in map_info.get(key, ()):
                if map_type != "Event":
                    continue
                for region in regions:
                    expanded.update(event_maps_by_region.get(region, ()))
        expanded.discard("")
        if expanded:
            owned[event_id] = frozenset(expanded)
    # The maps an event's own exits lead to count exactly: no region
    # widening, or the Frozen Field (region e_chr) would take in the Winter
    # Summit that hosts its entrance.
    for event_id, maps in exits.items():
        exact = set(maps)
        for key in maps:
            exact.update(group_maps.get(key, ()))
        exact.discard("")
        if exact:
            owned[event_id] = owned.get(event_id, frozenset()) | frozenset(exact)
    return owned


@lru_cache(maxsize=4)
def _event_exit_destinations(
    database_path: str, database_mtime_ns: int, database_size: int,
) -> dict[str, frozenset[str]]:
    """Event id -> maps behind exits that only an ``<event>=True`` EventSet opens.

    A sub-event names its own map this way: the Winter Summit's exits to the
    Frozen Field stand behind ``e_chr_2015_challenge_04=True``, and those to
    Kranparus' lairs behind ``e_chr_2015_challenge_01..03=True``. The static
    exit list carries each exit's map and EventSet row; the flag text lives in
    that map's own EventSet table. Cached per static.db4 revision because it
    opens every map with gated exits.
    """

    del database_mtime_ns, database_size
    db_path = Path(database_path)
    maps_directory = db_path.parent.parent / "maps"
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as database:
        known = {
            str(row[0]).casefold()
            for row in database.execute("SELECT Id FROM _Template_Events")
        }
        if not compat.has_columns(
            database, "_Template_ExitList", "_Level", "EventSetRow", "ExitURL",
        ):
            return {}
        gated: dict[str, list[tuple[int, str]]] = {}
        for level, event_set, url in database.execute(
            "SELECT _Level, EventSetRow, ExitURL FROM _Template_ExitList "
            "WHERE EventSetRow IS NOT NULL AND EventSetRow != -1 "
            "AND COALESCE(ExitURL, '') != ''"
        ):
            gated.setdefault(str(level), []).append((int(event_set), str(url)))
    result: dict[str, set[str]] = {}
    for level, exits in gated.items():
        path = maps_directory / f"{level}.db4"
        if not path.is_file():
            continue
        wanted = sorted({str(event_set) for event_set, _url in exits})
        try:
            with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as map_database:
                flags = dict(map_database.execute(
                    "SELECT Id, EventFlags FROM _Instance_EventSets WHERE Id IN ({})".format(
                        ",".join("?" * len(wanted))
                    ),
                    wanted,
                ))
        except sqlite3.Error:
            continue
        for event_set, url in exits:
            for token in re.split(r"[;,]", str(flags.get(str(event_set)) or "")):
                name, _separator, value = token.partition("=")
                name = name.strip().casefold()
                if name in known and value.strip().casefold() == "true":
                    result.setdefault(name, set()).add(_map_key(url))
    return {event_id: frozenset(maps) for event_id, maps in result.items()}


def _event_scripted_maps(
    maps_directory: Path, templates: Mapping[str, EventTemplateDefinition],
) -> dict[str, set[str]]:
    """Event id -> maps shipping a ``<map>_<event>.eve`` script for it."""

    known = {resource_id.casefold() for resource_id in templates}
    result: dict[str, set[str]] = {}
    try:
        names = [path.stem for path in maps_directory.glob("*.eve")]
    except OSError:
        return result
    for stem in names:
        folded = stem.casefold()
        for index, character in enumerate(folded):
            if character == "_" and folded[index + 1:] in known:
                result.setdefault(folded[index + 1:], set()).add(
                    _map_key(folded[:index])
                )
                break
    return result


def _event_loot_lists(
    path: str, item_rows: Mapping[str, Mapping[str, object]],
) -> dict[str, tuple[EventLootEntry, ...]]:
    """Parse data/catalogs/event_loot.json (see docs there).

    The retail server owned these loot lists and the client data only names
    them, so their contents are authored here. A malformed file fails the
    catalog load like a malformed schedule does.
    """

    if not path:
        return {}
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    lists = document.get("loot_lists") if isinstance(document, dict) else None
    if not isinstance(lists, dict):
        raise ValueError("event loot must contain a loot_lists object")
    result: dict[str, tuple[EventLootEntry, ...]] = {}
    for list_id, entries in lists.items():
        if not isinstance(entries, list):
            raise ValueError(f"event loot list {list_id!r} must be an array")
        parsed = []
        for entry in entries:
            item_id = str(entry.get("item") or "")
            # ``{difficulty:02d}`` picks the map difficulty's variant
            # (e_chr_progress_box_rare_Diff_03); the variants run from 0 up
            # to the last one the database has.
            difficulty_items = []
            if "{difficulty" in item_id:
                for difficulty in range(0, 100):
                    variant = item_id.format(difficulty=difficulty)
                    if variant not in item_rows:
                        break
                    difficulty_items.append(variant)
                if not difficulty_items:
                    raise ValueError(f"event loot list {list_id!r} names unknown item {item_id!r}")
                item_id = difficulty_items[0]
            item = item_rows.get(item_id)
            if item is None:
                raise ValueError(f"event loot list {list_id!r} names unknown item {item_id!r}")
            chances = {
                str(rank): max(0.0, min(1.0, float(value)))
                for rank, value in dict(entry.get("chance") or {}).items()
            }
            amounts = {}
            for rank, value in dict(entry.get("amount") or {}).items():
                low, high = (int(value[0]), int(value[-1])) if isinstance(
                    value, list) else (int(value), int(value))
                if not 1 <= low <= high:
                    raise ValueError(f"event loot amount for {item_id!r} must be 1 <= min <= max")
                amounts[str(rank)] = (low, high)
            difficulty_amounts = tuple(
                int(value) for value in entry.get("amount_by_difficulty") or ()
            )
            if any(value < 1 for value in difficulty_amounts):
                raise ValueError(
                    f"event loot difficulty amounts for {item_id!r} must be positive"
                )
            if not chances:
                raise ValueError(f"event loot entry {item_id!r} needs a chance")
            parsed.append(EventLootEntry(
                item_id=item_id,
                chances=MappingProxyType(chances),
                amounts=MappingProxyType(amounts),
                minimum_monster_level=max(1, int(item.get("MinDropLevel") or 1)),
                maximum_monster_level=max(0, int(item.get("MaxDropLevel") or 0)),
                regions=frozenset(
                    str(value).casefold() for value in entry.get("regions") or ()
                ),
                difficulty_items=tuple(difficulty_items),
                difficulty_amounts=difficulty_amounts,
            ))
        result[str(list_id).casefold()] = tuple(parsed)
    return result


def _map_loot_lists(
    path: str, item_rows: Mapping[str, Mapping[str, object]],
) -> dict[str, tuple[MapLootEntry, ...]]:
    """Parse map drops, including optional difficulty and event-state gates."""

    if not path:
        return {}
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    lists = document.get("map_loot") if isinstance(document, dict) else None
    if not isinstance(lists, dict):
        return {}
    result = {}
    for map_id, entries in lists.items():
        if not isinstance(entries, list):
            raise ValueError(f"map loot for {map_id!r} must be an array")
        parsed = []
        for entry in entries:
            item_options = tuple(
                str(value) for value in entry.get("item_options") or ()
                if str(value)
            )
            raw_class_options = dict(entry.get("item_options_by_class") or {})
            class_options = {}
            for class_name, values in raw_class_options.items():
                options = tuple(str(value) for value in values if str(value))
                if not options:
                    raise ValueError(
                        f"map loot class options for {class_name!r} cannot be empty"
                    )
                class_options[str(class_name).casefold()] = options
            all_options = (
                *item_options,
                *(item_id for options in class_options.values() for item_id in options),
            )
            item_id = str(entry.get("item") or (all_options[0] if all_options else ""))
            item = item_rows.get(item_id)
            if item is None:
                raise ValueError(f"map loot for {map_id!r} names unknown item {item_id!r}")
            for option in all_options:
                if option not in item_rows:
                    raise ValueError(
                        f"map loot for {map_id!r} names unknown item option {option!r}"
                    )
            required_costumes = frozenset(
                str(value).casefold()
                for value in entry.get("require_active_costumes") or ()
                if str(value).strip()
            )
            if any(
                not any(item_id.casefold() == costume for item_id in item_rows)
                for costume in required_costumes
            ):
                raise ValueError(
                    f"map loot for {map_id!r} names an unknown costume"
                )
            chances = {
                str(rank).casefold(): max(0.0, min(1.0, float(value)))
                for rank, value in dict(entry.get("chance") or {}).items()
            }
            amounts = {}
            for rank, value in dict(entry.get("amount") or {}).items():
                low, high = (int(value[0]), int(value[-1])) if isinstance(
                    value, list
                ) else (int(value), int(value))
                if not 1 <= low <= high:
                    raise ValueError(
                        f"map loot amount for {item_id!r} must be 1 <= min <= max"
                    )
                amounts[str(rank).casefold()] = (low, high)
            difficulty_amounts = tuple(
                int(value) for value in entry.get("amount_by_difficulty") or ()
            )
            if any(value < 1 for value in difficulty_amounts):
                raise ValueError(
                    f"map loot difficulty amounts for {item_id!r} must be positive"
                )
            if not chances:
                raise ValueError(f"map loot entry {item_id!r} needs a chance")
            templates = frozenset(
                str(value).casefold()
                for value in entry.get("monsters") or ()
            )
            monster_regions = frozenset(
                str(value).casefold()
                for value in entry.get("monster_regions") or ()
            )
            monster_ranks = frozenset(
                str(value).casefold()
                for value in entry.get("ranks") or ()
            )
            inactive_event_values = entry.get("require_inactive_events") or ()
            if isinstance(inactive_event_values, str):
                inactive_event_values = (inactive_event_values,)
            inactive_events = frozenset(
                str(value).casefold()
                for value in inactive_event_values
                if str(value).strip()
            )
            active_event_values = entry.get("require_active_events") or ()
            if isinstance(active_event_values, str):
                active_event_values = (active_event_values,)
            active_events = frozenset(
                str(value).casefold()
                for value in active_event_values
                if str(value).strip()
            )
            map_regions = frozenset(
                _map_key(value)
                for value in entry.get("map_regions") or ()
                if str(value).strip()
            )
            if not templates and not monster_regions and not monster_ranks:
                if not map_regions:
                    raise ValueError(
                        f"map loot entry {item_id!r} needs monster, region, rank, or map-region scope"
                    )
            parsed.append(MapLootEntry(
                loot=EventLootEntry(
                    item_id=item_id,
                    chances=MappingProxyType(chances),
                    amounts=MappingProxyType(amounts),
                    minimum_monster_level=max(
                        1, int(item.get("MinDropLevel") or 1)
                    ),
                    maximum_monster_level=max(
                        0, int(item.get("MaxDropLevel") or 0)
                    ),
                ),
                monster_templates=templates,
                monster_regions=monster_regions,
                monster_ranks=monster_ranks,
                scale_chance_with_difficulty=bool(
                    entry.get("scale_chance_with_difficulty")
                ),
                scale_stack_amount_with_difficulty=bool(
                    entry.get("scale_stack_amount_with_difficulty", True)
                ),
                difficulty_amounts=difficulty_amounts,
                item_options=item_options,
                item_options_by_class=MappingProxyType(class_options),
                require_class_option=bool(entry.get("require_class_option")),
                minimum_difficulty=max(
                    0, int(entry.get("minimum_difficulty", 0))
                ),
                maximum_difficulty=(
                    max(0, int(entry["maximum_difficulty"]))
                    if entry.get("maximum_difficulty") is not None else None
                ),
                required_costumes=required_costumes,
                require_active_events=active_events,
                require_inactive_events=inactive_events,
                map_regions=map_regions,
            ))
        result[_map_key(map_id)] = tuple(parsed)
    return result


def _authored_item_regions() -> dict[str, frozenset[str]]:
    """Parse data/catalogs/event_item_regions.json (see the note there).

    The retail server knew where these items drop; the client leaves their
    Region empty and no table ties their event to a place. Optional, like
    the loot table.
    """

    try:
        document = json.loads(
            DEFAULT_EVENT_ITEM_REGIONS_PATH.read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return {}
    return {
        str(item_id): frozenset(_map_key(value) for value in regions)
        for item_id, regions in document["item_regions"].items()
    }


def _schedule_revision(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


@lru_cache(maxsize=2)
def _static_event_rows(
    database_path: str, database_mtime_ns: int, database_size: int,
) -> tuple:
    """The static.db4 rows the event catalogue reads, cached per revision.

    Reading ``_Template_Item`` alone takes about a second, and the catalogue
    is rebuilt for every schedule revision (each CMS edit, each test's own
    events.json), so the database half is kept apart from the schedule half.
    The rows are shared between catalogues: read them, never change them.
    """

    del database_mtime_ns, database_size
    db_path = Path(database_path)
    with sqlite3.connect(
        f"file:{db_path.as_posix()}?mode=ro", uri=True
    ) as database:
        database.row_factory = sqlite3.Row
        item_rows = {
            str(row["Id"]): dict(row)
            for row in database.execute("SELECT * FROM _Template_Item")
        }
        map_rows = tuple(
            dict(row)
            for row in database.execute(compat.map_template_query(
                database, ("MapTemplate", "AccessGroup", "MapType", "Region"),
            ))
        )
        access_rows = ()
        if compat.has_columns(
            database, "_Template_MapAccessGroup", "Id", "AccessGroup"
        ):
            access_columns = compat.columns(
                database, "_Template_MapAccessGroup"
            )
            access_rows = tuple(
                dict(row)
                for row in database.execute(
                    "SELECT {} FROM _Template_MapAccessGroup".format(", ".join(
                        name if name in access_columns else f"NULL AS {name}"
                        for name in (
                            "Id", "AccessGroup", "RequiredActiveEvent",
                            "EventId", "KeyId", "TeleportKeyId",
                        )
                    ))
                )
            )
        monster_region_rows = ()
        if compat.has_columns(database, "_Template_Monster", "Region"):
            monster_region_rows = tuple(database.execute(
                "SELECT Id, Region FROM _Template_Monster "
                "WHERE Region IS NOT NULL AND Region != ''"
            ))
        monster_event_drop_rows = ()
        if compat.has_columns(database, "_Template_Monster", "EventDropChance"):
            monster_event_drop_rows = tuple(database.execute(
                "SELECT Id, EventDropChance FROM _Template_Monster "
                "WHERE EventDropChance > 0"
            ))
        # Event progress bars postdate the legacy client, which has no table for
        # them; an empty catalogue reads as "this build shows no progress bars".
        progress_rows = {}
        if compat.table_exists(database, "_Template_EventProgressBar"):
            progress_rows = {
                str(row["Id"]): dict(row)
                for row in database.execute(
                    "SELECT * FROM _Template_EventProgressBar"
                )
            }
        event_rows = {
            str(row["Id"]): dict(row)
            for row in database.execute("SELECT * FROM _Template_Events")
        }
        status_rows = tuple(database.execute(
            "SELECT rowid - 1, Id, StartModifiers FROM _Template_StatusEffect"
        ))
        status_stack_limits = {
            int(row[0]): max(1, int(row[1] or 1))
            for row in database.execute(
                'SELECT rowid - 1, COALESCE(MaxStackSize, 1) '
                'FROM _Template_StatusEffect'
            )
        }
    return (
        item_rows, map_rows, access_rows, monster_region_rows,
        monster_event_drop_rows, progress_rows, event_rows, status_rows,
        status_stack_limits,
    )


@lru_cache(maxsize=4)
def _load_catalog(
    database_path: str,
    database_mtime_ns: int,
    database_size: int,
    schedule_path: str,
    schedule_mtime_ns: int,
    schedule_size: int,
    loot_path: str = "",
    loot_mtime_ns: int = 0,
    loot_size: int = 0,
) -> EventCatalog:
    del schedule_mtime_ns, schedule_size, loot_mtime_ns, loot_size
    db_path = Path(database_path)
    config = json.loads(resident_content.read_text(schedule_path, encoding="utf-8"))
    (
        item_rows, map_rows, access_rows, monster_region_rows,
        monster_event_drop_rows, progress_rows, event_rows, status_rows,
        status_stack_limits,
    ) = _static_event_rows(database_path, database_mtime_ns, database_size)

    item_regions = _authored_item_regions()
    templates: dict[str, EventTemplateDefinition] = {}
    for resource_id, row in event_rows.items():
        bars = []
        for bar_id in _tokens(row.get("EventProgressBars")):
            bar_row = progress_rows.get(bar_id)
            if bar_row is None:
                continue
            collectibles = []
            entries = []
            for encoded in _tokens(bar_row.get("EventCollectibles")):
                encoded_parts = encoded.split("/", 1)
                try:
                    prefix = (
                        int(encoded_parts[0]) if len(encoded_parts) == 2 else None
                    )
                except ValueError:
                    prefix = None
                entries.append((prefix, encoded_parts[-1].strip()))
            # Several bars list one entry per page (0/, 1/, 2/...) without the
            # FixedBarCollectible flag: Full Moon and Stellar Gold repeat the
            # same item per page, Lost Viscanium changes it. Read those as
            # pages too; as progress values they rolled one item three times.
            fixed_tiers = bool(int(bar_row.get("FixedBarCollectible") or 0)) or (
                len(entries) > 1
                and sorted(prefix for prefix, _ in entries if prefix is not None)
                == list(range(len(entries)))
            )
            seen_items = set()
            for prefix, item_id in entries:
                item = item_rows.get(item_id)
                if item is None:
                    continue
                if not fixed_tiers:
                    if item_id in seen_items:
                        continue
                    seen_items.add(item_id)
                if fixed_tiers:
                    # The prefix is the bar page, and one drop is worth the
                    # bar's ProgressbarBase: retail dropped the Dragan key in
                    # stacks of 2 on its 2.0-base bars (2026-10-28 capture).
                    tier = prefix or 0
                    base_value = max(1, round(float(
                        bar_row.get("ProgressbarBase") or 1
                    )))
                else:
                    # The prefix is the authored progress represented by one
                    # drop (for example ``2/e_sca_dun_01_progress_item``).
                    tier = None
                    base_value = max(
                        max(1, prefix or 1), int(item.get("BaseValue") or 1)
                    )
                collectibles.append(EventCollectible(
                    item_id=item_id,
                    item_category=str(item.get("ItemCategory") or "InstantConsumable"),
                    auto_pick_up=str(item.get("AutoPickUp") or "All"),
                    tier=tier,
                    base_value=base_value,
                    maps=frozenset(
                        _map_key(value) for value in _tokens(item.get("Region"))
                    ) | item_regions.get(item_id, frozenset()),
                    owner_events=frozenset(
                        value.casefold()
                        for value in _tokens(item.get("EventId"))
                    ),
                ))
            reward_items = str(bar_row.get("EventRewardItems") or "")
            reward_items = _normalize_reward_item_templates(
                reward_items, item_rows,
            )
            if (
                resource_id.casefold() == "e_ide_challenge_01"
                and bar_id.casefold() == "e_ide_challenge_01_progressbar_03"
            ):
                # The 2026 FAQ's level-100 progress rewards specify 400
                # Amphora Keys at this tier; the older static row still uses
                # Sargon chest keys.
                reward_items = reward_items.replace(
                    "2/23000:e_sar_chest_key:400",
                    "2/23000:e_ide_02_chest_key_01:400",
                )
            bars.append(EventProgressBarDefinition(
                resource_id=bar_id,
                minimum_level=max(1, int(bar_row.get("MinPlayerLevel") or 1)),
                maximum_level=max(1, int(bar_row.get("MaxPlayerLevel") or 200)),
                priority=int(bar_row.get("Priority") or 0),
                collectibles=tuple(collectibles),
                item_rewards=_parse_reward_entries(
                    reward_items, currency=False
                ),
                currency_rewards=_parse_reward_entries(
                    bar_row.get("EventRewardCurrency"), currency=True
                ),
                # The Dragan event bar is not achievement-gated on this
                # server. The client displays the level-100+ top bar without
                # waiting for general_score_5000, so applying that stale
                # static requirement here makes rewards come from a different
                # bar than the one shown to the player.
                required_achievements=(
                    frozenset()
                    if resource_id.casefold() == "dragan_event_03"
                    else frozenset(
                        value.casefold()
                        for value in _tokens(
                            bar_row.get("CompletedAchievementsRequirements")
                        )
                    )
                ),
                monster_drops=not any(
                    int(bar_row.get(column) or 0)
                    for column in (
                        "ProgressByPVELeaderboardProgress",
                        "ProgressByPVPLeaderboardProgress",
                        "ProgressByLevelUp",
                    )
                ),
            ))
        templates[resource_id] = EventTemplateDefinition(
            resource_id=resource_id,
            client_name=str(row.get("Name") or resource_id),
            client_only=bool(int(row.get("ClientOnly") or 0)),
            map_local=bool(int(row.get("MapLocal") or 0)),
            minimum_level=max(1, int(row.get("MinPlayerLevel") or 1)),
            maximum_level=max(1, int(row.get("MaxPlayerLevel") or 200)),
            map_id=str(row.get("Map") or ""),
            excluded_maps=frozenset(
                _map_key(value) for value in _tokens(row.get("ExcludeMaps"))
            ),
            teleport_target=str(row.get("TeleportTarget") or ""),
            status_effects=_tokens(row.get("EventStatusEffects")),
            progress_bars=tuple(bars),
            category=str(row.get("Category") or ""),
            loot_list_id=str(row.get("Loot") or "").strip(),
        )

    events = []
    seen_ids = set()
    for entry in config.get("events", ()):
        resource_id = str(entry.get("template_id") or "").strip()
        template = templates.get(resource_id)
        if template is None:
            raise ValueError(f"event schedule references unknown template {resource_id!r}")
        event_id = int(entry["event_id"])
        if event_id <= 0 or event_id > UINT32_MAX or event_id in seen_ids:
            raise ValueError(f"invalid or duplicate runtime event id {event_id}")
        seen_ids.add(event_id)
        forced_state = str(entry.get("state") or "scheduled").casefold()
        ranks = frozenset(str(value).casefold() for value in entry.get("collectible_ranks", ()))
        if not ranks and resource_id.casefold().startswith("mini_kill_event_"):
            ranks = frozenset(("worldboss",))
        elif not ranks and resource_id == "scaling_dungeon_kill_event_01":
            ranks = frozenset(("leader", "champion", "guardian", "boss", "worldboss"))
        events.append(ScheduledEvent(
            event_id=event_id,
            template=template,
            starts_at=_timestamp(entry.get("starts_at")),
            ends_at=_timestamp(entry.get("ends_at")),
            forced_state=forced_state,
            tags=tuple(str(value) for value in entry.get("tags", ())),
            participation_default=bool(entry.get("participation_default", True)),
            collectible_drop_chance=max(0.0, min(1.0, float(entry.get("collectible_drop_chance", 0.35)))),
            collectible_amount=max(1, int(entry.get("collectible_amount", 1))),
            collectible_maps=frozenset(_map_key(value) for value in entry.get("collectible_maps", ())),
            collectible_ranks=ranks,
        ))

    travel_items = {}
    map_requirements: dict[str, set[str]] = {}
    event_collectible_maps: dict[str, set[str]] = {}
    known_game_events = frozenset(
        resource_id.casefold() for resource_id in templates
    )
    for item_id, row in item_rows.items():
        if str(row.get("ItemCategory") or "") != "TravelItem":
            continue
        target = str(row.get("TravelItemTarget") or "").strip()
        if not target:
            continue
        authored_event_ids = frozenset(
            value.casefold()
            for value in (
                *_tokens(row.get("EventId")),
                *_tokens(row.get("ActiveEvents")),
            )
        )
        # ActiveEvents also stores quest/map signal names. Only actual
        # _Template_Events IDs are game-event schedule gates.
        event_ids = authored_event_ids & known_game_events
        definition = TravelItemDefinition(
            template_id=item_id,
            target_map_id=target,
            required_level=max(1, int(row.get("RequiredLevel") or 1)),
            event_ids=event_ids,
            only_during_event=bool(int(row.get("OnlyActiveDuringEvent") or 0)),
        )
        travel_items[item_id] = definition
        if event_ids:
            target_key = _map_key(target)
            map_requirements.setdefault(target_key, set()).update(event_ids)
            for event_id in event_ids:
                event_collectible_maps.setdefault(event_id, set()).add(
                    target_key
                )

    # The Azar boss ration is authored as a LootGenItem with an empty
    # TravelItemTarget, while the client still sends the ordinary inventory
    # use action for it. Its intended destination is the first Azar access
    # group; that group's normal MapAccess selection supplies the difficulty
    # choices. Keep this compatibility mapping explicit instead of treating
    # arbitrary LootGenItems as travel items.
    azar_ration_id = "boss_j0003_lava_dun_boss_travelitem"
    azar_destination_id = "boss_j0003_lava_dun_boss_01_travelitem"
    azar_destination = travel_items.get(azar_destination_id)
    if azar_destination is not None and azar_ration_id not in travel_items:
        travel_items[azar_ration_id] = replace(
            azar_destination, template_id=azar_ration_id
        )

    # Several global event definitions leave _Template_Events.Map empty.
    # Their map ownership is instead encoded on the event's TravelItems,
    # access groups and keys. Without carrying it into the loot gate, every
    # active global event rolls its progress item in every map (for example
    # Big Game Hunt progress inside Desert of Essences, or Winter Solstice
    # progress in Kingshill). An explicit schedule override stays
    # authoritative and is kept apart, so the CMS carries only that forward.
    database_revision = db_path.stat()
    event_maps = _event_owned_maps(
        templates, item_rows, map_rows, access_rows, event_collectible_maps,
        _event_scripted_maps(db_path.parent.parent / "maps", templates),
        _event_exit_destinations(
            str(db_path), database_revision.st_mtime_ns, database_revision.st_size,
        ),
    )
    events = [
        replace(
            event,
            progress_maps=event_maps.get(
                event.template.resource_id.casefold(), frozenset()
            ),
        )
        for event in events
    ]
    map_regions: dict[str, set[str]] = {}
    group_regions: dict[str, set[str]] = {}
    for row in map_rows:
        regions = {value.casefold() for value in _tokens(row.get("Region"))}
        map_regions.setdefault(_map_key(row.get("MapTemplate")), set()).update(
            regions
        )
        group = _map_key(row.get("AccessGroup"))
        if group:
            group_regions.setdefault(group, set()).update(regions)
    for group, regions in group_regions.items():
        map_regions.setdefault(group, set()).update(regions)
    for row in access_rows:
        regions = group_regions.get(_map_key(row.get("AccessGroup")))
        if regions:
            map_regions.setdefault(_map_key(row.get("Id")), set()).update(
                regions
            )
    monster_regions = {
        str(monster_id).casefold(): frozenset(
            value.casefold() for value in _tokens(region)
        )
        for monster_id, region in monster_region_rows
    }

    modifiers: dict[int, list[DropAmountModifier]] = {}
    for effect_id, _resource_id, program in status_rows:
        for instruction in str(program or "").split(";"):
            match = _DROP_AMOUNT.match(instruction.strip())
            if match is None:
                continue
            modifiers.setdefault(int(effect_id), []).append(DropAmountModifier(
                int(effect_id), match.group(3).strip(), match.group(1).strip(),
                match.group(2).casefold(),
                status_stack_limits.get(int(effect_id), 1),
            ))

    return EventCatalog(
        events=tuple(sorted(events, key=lambda event: event.event_id)),
        travel_items=MappingProxyType(travel_items),
        event_map_requirements=MappingProxyType({
            key: frozenset(values) for key, values in map_requirements.items()
        }),
        drop_modifiers=MappingProxyType({
            key: tuple(values) for key, values in modifiers.items()
        }),
        event_maps=MappingProxyType(event_maps),
        map_regions=MappingProxyType({
            key: frozenset(values)
            for key, values in map_regions.items() if values
        }),
        monster_regions=MappingProxyType(monster_regions),
        event_loot=MappingProxyType(_event_loot_lists(loot_path, item_rows)),
        map_loot=MappingProxyType(_map_loot_lists(loot_path, item_rows)),
        monster_event_drop_chances=MappingProxyType({
            str(monster_id).casefold(): min(1.0, float(chance))
            for monster_id, chance in monster_event_drop_rows
        }),
    )


def load_event_catalog(
    database_path: str | Path,
    schedule_path: str | Path,
    loot_path: str | Path | None = None,
) -> EventCatalog:
    # Looked up many times per map entry: use the last observed revisions
    # instead of stat()ing both files each time (runtime/file_revisions.py).
    # The CMS invalidates the schedule after writing it.
    database = resolved_path(str(database_path))
    schedule = resolved_path(str(schedule_path))
    loot = resolved_path(str(loot_path or DEFAULT_EVENT_LOOT_PATH))
    database_revision = observed_revision(database)
    schedule_revision = observed_revision(schedule)
    if database_revision is None:
        raise FileNotFoundError(database)
    if schedule_revision is None:
        raise FileNotFoundError(schedule)
    # The event loot table is optional: without it events drop no Loot list.
    loot_revision = observed_revision(loot)
    return _load_catalog(
        str(database), *database_revision, str(schedule), *schedule_revision,
        *((str(loot), *loot_revision) if loot_revision is not None else ()),
    )


@dataclass(frozen=True)
class EventStatusEffectSpec:
    """One durable buff an active event grants, resolved to its wire id."""

    resource_id: str
    effect_id: int
    values: tuple[float, ...]


def parse_event_status_effects(
    tokens, *, value_slots: int = 5,
) -> tuple[tuple[str, tuple[float, ...]], ...]:
    """Read ``_Template_Events.EventStatusEffects`` into (effect, values).

    The column mixes effect ids with ``$N:value`` parameter assignments for
    the effect named just before them: ``xpgain,$0:0.25`` is the XP bonus
    effect with its first program parameter set to 0.25, which the effect's
    ``XPGain:$0,relative`` modifier then reads. Effects without assignments
    keep zero parameters and rely on literal program values.
    """

    result: list[tuple[str, list[float]]] = []
    for token in tokens:
        text = str(token or "").strip()
        if not text:
            continue
        if text.startswith("$"):
            if not result:
                continue
            reference, _separator, raw_value = text[1:].partition(":")
            if not reference.strip().isdigit():
                continue
            try:
                value = float(raw_value.strip())
            except ValueError:
                continue
            index = int(reference)
            values = result[-1][1]
            while len(values) <= index:
                values.append(0.0)
            values[index] = value
            continue
        result.append((text, [0.0] * value_slots))
    return tuple(
        (resource_id, tuple(values)) for resource_id, values in result
    )


def event_status_effect_specs(
    catalog: EventCatalog,
    database_path: str | Path,
    *,
    now: datetime | None = None,
    player_level: int | None = None,
    include_inactive: bool = False,
) -> tuple[EventStatusEffectSpec, ...]:
    """Resolve the buffs the scheduled events grant a player.

    Only active events count unless ``include_inactive`` asks for every
    scheduled template, which the initialization phase uses to strip stale
    entries. A template's level window is honoured when ``player_level`` is
    given; the shipped XP events start at level 20.
    """

    runtime = load_status_runtime_catalog(database_path)
    events = (
        catalog.events if include_inactive else catalog.active_events(now)
    )
    specs: list[EventStatusEffectSpec] = []
    seen: set[str] = set()
    for event in events:
        template = event.template
        if player_level is not None and not (
            template.minimum_level <= int(player_level) <= template.maximum_level
        ):
            continue
        for resource_id, values in parse_event_status_effects(
            template.status_effects
        ):
            effect_id = runtime.effect_ids.get(resource_id)
            if effect_id is None or resource_id in seen:
                continue
            seen.add(resource_id)
            specs.append(
                EventStatusEffectSpec(resource_id, int(effect_id), values)
            )
    return tuple(specs)


def event_status_entry(
    spec: EventStatusEffectSpec, *, instance_id: int, actor_id: int,
) -> StatusEffectEntry:
    """Build the permanent, detailed entry that carries an event buff.

    Shaped like the costume and pet entries the initialization already
    sends: no duration, no start offset, the local player as causer, and the
    parameter values the effect program reads through ``$N``.
    """

    return StatusEffectEntry(
        effect_id=int(spec.effect_id),
        instance_id=int(instance_id),
        end_tick=0,
        tick_interval=0,
        start_tick=0,
        stack_index=0,
        total_duration=0,
        source_level=100,
        source_actor_id=int(actor_id),
        update_timing=False,
        stacked=False,
        permanent=True,
        has_details=True,
        values=tuple(float(value) for value in spec.values),
    )


def event_status_effect_ids(
    catalog: EventCatalog,
    database_path: str | Path,
    *,
    now: datetime | None = None,
) -> tuple[int, ...]:
    return tuple(
        spec.effect_id
        for spec in event_status_effect_specs(catalog, database_path, now=now)
    )


__all__ = [
    "EventCatalog",
    "EventProgressAdvance",
    "EventReward",
    "ScheduledEvent",
    "TravelItemDefinition",
    "UINT32_MAX",
    "completed_achievement_ids",
    "EventStatusEffectSpec",
    "event_status_effect_ids",
    "event_status_effect_specs",
    "event_status_entry",
    "load_event_catalog",
    "parse_event_status_effects",
]
