'''Normal character-XP rewards and authoritative progression updates.'''

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
import os
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Iterable, Mapping

from dso_server.runtime import resident_content
from dso_server.protocol.player_init_protocol import (
    XP_CHANGED_COMMAND,
    ServerCommand,
    XPChanged,
    encode_server_command_batch,
)

from dso_server.runtime.context import *
from dso_server.gameplay.status_modifier_runtime import (
    active_xp_gain_multiplier,
    snapshot_active_status_entries,
)
from dso_server.runtime.transport import Session
from dso_server.protocol.skill_book_protocol import experience_commands


UINT32_MAX = 0xFFFFFFFF
# The playable level cap: the database's top authored level unless
# DSO_PLAYER_LEVEL_CAP lowers it (100 matches the retail level cap).
PLAYER_LEVEL_CAP = int(os.getenv("DSO_PLAYER_LEVEL_CAP", "0") or 0) or UINT32_MAX


@dataclass(frozen=True)
class MonsterXPSource:
    coefficient: float
    authored_rank_id: str


@dataclass(frozen=True)
class PlayerXPTransition:
    awarded: int
    xp: int
    level: int
    level_base_xp: int
    next_level_base_xp: int
    level_up: bool


@dataclass(frozen=True)
class PlayerProgressionCatalog:
    monster_sources: Mapping[str, MonsterXPSource]
    monster_level_factors: Mapping[int, float]
    rank_factors: Mapping[str, float]
    difficulty_factors: Mapping[int, float]
    player_level_xp: Mapping[str, Mapping[int, int]]

    @staticmethod
    def _nearest_factor(factors: Mapping[int, float], key: int) -> float:
        if not factors:
            return 1.0
        wanted = int(key)
        if wanted in factors:
            return max(0.0, float(factors[wanted]))
        nearest = min(factors, key=lambda value: abs(value - wanted))
        return max(0.0, float(factors[nearest]))

    def monster_kill_xp(
        self,
        template_id: str,
        *,
        monster_level: int,
        rank_id: str = '',
        map_difficulty: int = 0,
    ) -> int:
        '''Resolve the database-authored solo XP for one monster death.'''

        source = self.monster_sources.get(str(template_id))
        if source is None or source.coefficient <= 0.0:
            return 0
        level_factor = self._nearest_factor(
            self.monster_level_factors, max(1, int(monster_level))
        )
        rank_factor = self.rank_factors.get(
            str(rank_id or source.authored_rank_id), 1.0
        )
        difficulty_factor = self._nearest_factor(
            self.difficulty_factors, max(0, int(map_difficulty))
        )
        reward = (
            source.coefficient
            * max(0.0, float(level_factor))
            * max(0.0, float(rank_factor))
            * max(0.0, float(difficulty_factor))
        )
        if reward <= 0.0:
            return 0
        return max(1, int(math.floor(reward + 0.5)))

    def dynamic_quest_xp(self, *, class_id: int, current_level: int,
                         current_xp: int, percent: float, multiplier: float = 1.0) -> int:
        """Native CalculateDynamicXPReward: percent of one level, capped at 100."""
        thresholds = self.player_level_xp.get(CLASS_NAMES.get(int(class_id), ""), {})
        if not thresholds:
            return 0
        current = max(0, int(current_xp), thresholds.get(int(current_level), 0))
        cap = thresholds[min(100, PLAYER_LEVEL_CAP, max(thresholds))]
        if current >= cap:
            return 0
        level = max(level for level, xp in thresholds.items() if xp <= current)
        span = thresholds[level + 1] - thresholds[level]
        return min(cap - current, max(0, int(math.floor(
            span * max(0.0, percent) / 100.0 * max(0.0, multiplier) + 0.5))))

    def advance_player(
        self,
        *,
        class_id: int,
        current_level: int,
        current_xp: int,
        amount: int,
    ) -> PlayerXPTransition | None:
        '''Advance cumulative XP, including multi-level and cap transitions.'''

        class_name = CLASS_NAMES.get(int(class_id))
        thresholds = self.player_level_xp.get(str(class_name or ''))
        if not thresholds or int(current_level) not in thresholds:
            return None
        requested = max(0, int(amount))
        if requested <= 0:
            return None
        level = max(1, int(current_level))
        current = max(
            0,
            int(current_xp),
            int(thresholds[level]),
        )
        maximum_level = min(PLAYER_LEVEL_CAP, max(thresholds))
        maximum_xp = int(thresholds[maximum_level])
        if level >= maximum_level or current >= maximum_xp:
            return None
        target = min(UINT32_MAX, maximum_xp, current + requested)
        new_level = max(
            candidate_level
            for candidate_level, threshold in thresholds.items()
            if candidate_level >= level and int(threshold) <= target
        )
        level_base = int(thresholds[new_level])
        next_base = (
            level_base + 1
            if new_level >= PLAYER_LEVEL_CAP
            else int(thresholds.get(new_level + 1, level_base + 1))
        )
        if next_base <= level_base:
            next_base = min(UINT32_MAX, level_base + 1)
        awarded = max(0, target - current)
        if awarded <= 0:
            return None
        return PlayerXPTransition(
            awarded=awarded,
            xp=target,
            level=new_level,
            level_base_xp=level_base,
            next_level_base_xp=next_base,
            level_up=new_level > level,
        )


def _database_signature(path: str | Path) -> tuple[str, int, int]:
    resolved = resident_content.resolved(path)
    stat = resident_content.stat(resolved)
    return str(resolved), int(stat.st_mtime_ns), int(stat.st_size)


@lru_cache(maxsize=4)
def _load_player_progression_catalog_cached(
    database_path: str,
    _mtime_ns: int,
    _size: int,
) -> PlayerProgressionCatalog:
    path = Path(database_path)
    with sqlite3.connect(
        f'file:{path.as_posix()}?mode=ro', uri=True
    ) as database:
        monster_rows = tuple(database.execute('''
            SELECT Id, COALESCE(XPForKill, 0), COALESCE(Rank, '')
            FROM _Template_Monster
        '''))
        level_factor_rows = tuple(database.execute('''
            SELECT ActorLevel, COALESCE(MonsterXPFactor, 0)
            FROM _Template_XpFactors
        '''))
        rank_rows = tuple(database.execute('''
            SELECT Id, COALESCE(XPForKillFactor, 1)
            FROM _Template_MonsterRanks
        '''))
        difficulty_rows = tuple(database.execute('''
            SELECT Id, COALESCE(XPGain, 1)
            FROM _Template_DifficultyFactors
        '''))
        player_rows = tuple(database.execute('''
            SELECT CharClass, Level, LevelXP
            FROM _Template_XPLevels
            ORDER BY CharClass, Level
        '''))

    difficulties = {}
    for difficulty_id, factor in difficulty_rows:
        try:
            key = int(difficulty_id)
        except (TypeError, ValueError):
            continue
        difficulties[key] = max(0.0, float(factor or 0.0))
    player_levels: dict[str, dict[int, int]] = {}
    for class_name, level, xp in player_rows:
        player_levels.setdefault(str(class_name), {})[int(level)] = int(xp)
    return PlayerProgressionCatalog(
        monster_sources=MappingProxyType({
            str(template_id): MonsterXPSource(
                max(0.0, float(coefficient or 0.0)),
                str(rank_id or ''),
            )
            for template_id, coefficient, rank_id in monster_rows
        }),
        monster_level_factors=MappingProxyType({
            int(level): max(0.0, float(factor or 0.0))
            for level, factor in level_factor_rows
        }),
        rank_factors=MappingProxyType({
            str(rank_id): max(0.0, float(factor or 0.0))
            for rank_id, factor in rank_rows
        }),
        difficulty_factors=MappingProxyType(difficulties),
        player_level_xp=MappingProxyType({
            class_name: MappingProxyType(levels)
            for class_name, levels in player_levels.items()
        }),
    )


def load_player_progression_catalog(
    database_path: str | Path,
) -> PlayerProgressionCatalog:
    return _load_player_progression_catalog_cached(
        *_database_signature(database_path)
    )


def player_level_range(database_path, class_name: str) -> tuple[int, int]:
    """Playable level range, bounded by PLAYER_LEVEL_CAP."""

    thresholds = _level_thresholds(database_path, class_name)
    return min(thresholds), min(PLAYER_LEVEL_CAP, max(thresholds))


def player_level_xp(database_path, class_name: str, level: int) -> tuple[int, int]:
    """Cumulative XP at ``level`` and at the following one.

    ``NewPlayerCommand`` carries both and the client asserts a positive span,
    so the topmost authored level receives a one-point next bound.
    """

    thresholds = _level_thresholds(database_path, class_name)
    level = int(level)
    if level > PLAYER_LEVEL_CAP:
        raise ValueError(
            f"{class_name} is capped at level {PLAYER_LEVEL_CAP}"
        )
    if level not in thresholds:
        raise ValueError(
            f"{class_name} has no authored XP progression for level {level}"
        )
    base = int(thresholds[level])
    if level >= min(PLAYER_LEVEL_CAP, max(thresholds)):
        return base, base + 1
    return base, max(base + 1, int(thresholds.get(level + 1, base + 1)))


def _level_thresholds(database_path, class_name: str) -> Mapping[int, int]:
    thresholds = load_player_progression_catalog(
        database_path
    ).player_level_xp.get(str(class_name), {})
    if not thresholds:
        raise ValueError(f"unknown character class {class_name!r}")
    return thresholds


class PlayerProgressionService:
    '''Award normal XP through one shared projectile/owned-effect path.'''

    @staticmethod
    def _award_monster_kill_xp(
        s: Session,
        monsters: Iterable[object],
        *,
        database_path: str | Path,
    ) -> int:
        monsters = tuple(monsters)
        if not monsters:
            return 0
        catalog = load_player_progression_catalog(database_path)
        with s.flow_lock:
            character = s.gameplay_character
            generation = int(s.gameplay_chase_generation)
            if character is None:
                return 0
            difficulty = max(
                int(s.gameplay_map_difficulty),
                int(getattr(s, 'gameplay_scaling_difficulty', 0)),
            )
            status_effects = snapshot_active_status_entries(
                s.gameplay_persistent_status_effects,
                s.gameplay_timed_status_effect_expiries,
                s.gameplay_runtime_status_effects,
                s.gameplay_item_set_effects,
                s.gameplay_socket_effects,
            )

        multiplier = active_xp_gain_multiplier(
            database_path, status_effects
        )
        requested = sum(
            max(0, int(math.floor(
                catalog.monster_kill_xp(
                    str(getattr(monster, 'template_id', '')),
                    monster_level=max(
                        1, int(getattr(monster, 'level', 1))
                    ),
                    rank_id=str(getattr(monster, 'rank_id', '')),
                    map_difficulty=difficulty,
                ) * multiplier + 0.5
            )))
            for monster in monsters
        )
        if requested <= 0:
            return 0
        return CharacterService._award_experience(
            s, requested, database_path=database_path, label='monster-kill-xp',
            character=character, generation=generation,
        )

    @staticmethod
    def _award_experience(
        s: Session,
        amount: int,
        *,
        database_path: str | Path,
        label: str,
        character=None,
        generation: int | None = None,
    ) -> int:
        """Grant ``amount`` XP to the local player (kills, quests).

        ``character``/``generation`` pin the session state a caller already
        sampled; without them the current character is used.
        """

        if int(amount) <= 0:
            return 0
        catalog = load_player_progression_catalog(database_path)
        if character is None:
            with s.flow_lock:
                character = s.gameplay_character
                generation = int(s.gameplay_chase_generation)
            if character is None:
                return 0

        with s.flow_lock:
            if (
                s.gameplay_character is not character
                or int(s.gameplay_chase_generation) != generation
            ):
                return 0
            points = tuple(s.gameplay_talent_category_points)
            current_xp = max(
                int(getattr(character, 'progression_u32', 0)),
                int(points[0]) if len(points) == 4 else 0,
            )
            transition = catalog.advance_player(
                class_id=int(character.class_id),
                current_level=max(1, int(character.level)),
                current_xp=current_xp,
                amount=int(amount),
            )
            if transition is None:
                return 0
            character.progression_u32 = transition.xp
            character.level = transition.level
            if len(points) == 4:
                s.gameplay_talent_category_points = (
                    transition.xp,
                    int(points[1]),
                    int(points[2]),
                    int(points[3]),
                )

        packet, packet_bits = encode_server_command_batch(experience_commands(
            database_path, character.class_id, transition,
            actor_id=LOCAL_PLAYER_ACTOR_ID, quick_slots=s.gameplay_quick_slots,
        ), include_terminator=True)
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label=label
        )
        if transition.level_up:
            CharacterService._refresh_player_combat_vitals(s)
            CharacterService._achievement_on_level_up(
                s, int(character.class_id), int(transition.level),
            )
        CharacterService._request_account_save(s, label)
        return transition.awarded


__all__ = [
    'MonsterXPSource',
    'PlayerProgressionCatalog',
    'PlayerProgressionService',
    'PlayerXPTransition',
    'PLAYER_LEVEL_CAP',
    'load_player_progression_catalog',
]
