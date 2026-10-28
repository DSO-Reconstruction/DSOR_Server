"""Authored leaderboard boards, run scoring, ranking periods and rewards.

Everything here is read from ``static.db4`` and the client UI:

* ``_Template_ParallelWorldStructure`` rows 0..8 are the nine
  ``Ranking::PvELeaderBoard::PWLeaderBoardId`` boards. The client resolves a
  board with ``GauntletManager::GetParallelWorldStructureById`` (assertion
  text at 0x140f5f630) and maps the leaderboard tree's PvE categories
  ``Grimmag`` .. ``Gorga`` (``_Template_LeaderBoardCategory`` 9..17) onto ids
  0..8 (0x14031be60). Each row names its three ordered maps, the boss whose
  death ends the run (``ParallelWorldEndTrigger``), the monster requirement,
  per-difficulty par minutes (``Diff0Time`` .. ``Diff10Time``) and the
  bronze/silver/gold score thresholds the client draws as league icons
  (0x140321000).
* ``_Globals.GauntletTimePoints`` / ``GauntletMonsterKillPoints`` /
  ``GauntletBossKillPoints`` / ``GauntletCompletionBonus`` and
  ``_Template_DifficultyFactors.GauntletScore`` are the scoring constants the
  shared ``GauntletManager::OnActivate`` loads (0x140a31cc4). The client never
  combines them; :func:`score_parallel_world_run` documents the local formula
  (speed-driven, partial runs keep their completed maps, finishing dominates).
* ``_Template_LeaderBoardOverallranking`` gives the ranking points per place
  that the PvE "Overall Rank" board sums across the nine boards.
* ``_Template_HighScoreRankRewards`` (Fractured Planes group ranking),
  ``_Template_OverallPVPRankingRewards`` (PvP "Overall Rank") and
  ``_Template_OverallRankingRewards`` (PvE "Overall Rank"; ``BoardType`` 0 is
  the solo table, 1 the group table) are the place-range rewards a season
  settlement grants.
* ``_Globals.HighScoreRankingEventId`` / ``HighScoreRankAwardEventId`` name
  the scheduled events that bound a Fractured Planes ranking period.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping
import sqlite3

from dso_server.runtime import resident_content


PARALLEL_WORLD_BOARD_COUNT = 9   # PWLeaderBoardId 0..8 (enum reader -1..8).
PVE_BOARD_TYPES = (0, 1)         # PvELeaderBoard::Type: 0 solo, 1 group.
DIFFICULTY_RANGE = range(11)     # Diff0Time .. Diff10Time, enum -1..10.

# Scheduled event templates that bound a leaderboard season. Both carry
# ``EventScheduleCategory`` "LEADERBOARD SEASON" in ``_Template_Events`` and
# advertise exactly the reward boxes of the tables settled for them.
PVP_SEASON_EVENT = "pvp_seasons_event"
PVE_SEASON_EVENT = "pve_season_event"
DEFAULT_HIGH_SCORE_EVENT = "event_fracturedplanes_rank_start"
DEFAULT_HIGH_SCORE_AWARD_EVENT = "event_fracturedplanes_rank_reward"


def _tokens(value: object, separator: str = ";") -> tuple[str, ...]:
    return tuple(
        token.strip() for token in str(value or "").split(separator)
        if token.strip()
    )


def _map_key(value: object) -> str:
    return str(value or "").strip().casefold()


def parse_reward_items(value: object) -> tuple[tuple[str, int], ...]:
    """``fp_box_01:1;money_vc_gold_no_auto_use:1500`` -> item/amount pairs."""

    result = []
    for token in _tokens(value):
        item_id, _separator, amount = token.partition(":")
        item_id = item_id.strip()
        if not item_id:
            continue
        try:
            count = int(amount) if amount.strip() else 1
        except ValueError:
            continue
        if count > 0:
            result.append((item_id, count))
    return tuple(result)


@dataclass(frozen=True)
class RankReward:
    place_from: int
    place_to: int
    items: tuple[tuple[str, int], ...]
    # ``_Template_HighScoreRankRewards.IntValue`` (+20 .. -20). The client
    # does not read it (ClientRankingManager 0x140319818 loads only the
    # place range and items) and nothing identifies what it adjusts, so the
    # server parses it but does not apply it.
    value: int = 0

    def covers(self, place: int) -> bool:
        return self.place_from <= int(place) <= self.place_to


def reward_for_place(
    rewards: Iterable[RankReward], place: int,
) -> RankReward | None:
    """First authored row whose one-based place range contains ``place``."""

    for reward in rewards:
        if reward.covers(place):
            return reward
    return None


@dataclass(frozen=True)
class ParallelWorldBoard:
    board_id: int
    group_id: str
    maps: Mapping[str, int]
    final_order: int
    boss_templates: frozenset[str]
    monster_requirement: int
    par_minutes: Mapping[int, int]
    bronze: int
    silver: int
    gold: int


@dataclass(frozen=True)
class ParallelWorldRunScore:
    """One scored run: the four row statistics plus the raw summary.

    ``time_points`` holds every speed-derived part (map speed points and
    the completion points); ``total`` adds the monster and boss points and
    the authored completion bonus.
    """

    total: int
    time_points: int
    monster_points: int
    boss_points: int
    elapsed_seconds: int
    kills: int
    bosses: int
    difficulty: int
    maps_completed: int = 0

    @property
    def completed(self) -> bool:
        return self.bosses > 0

    @property
    def row_stats(self) -> tuple[int, int, int, int]:
        """``ParallelWorldUserRankingResponse`` row order (0x1403207b4).

        The row reader fills ``StaticArray<uint,4>`` at +0x40..+0x4c; the
        table formats +0x40 as the league-checked rank score and the next
        three into the Time, Monster and Boss kill point columns.
        """

        return (self.total, self.time_points, self.monster_points, self.boss_points)


@dataclass(frozen=True)
class LeaderboardCatalog:
    parallel_worlds: tuple[ParallelWorldBoard, ...]
    difficulty_factors: Mapping[int, float]
    time_points: int
    monster_points: int
    boss_points: int
    completion_bonus: float
    overall_points: tuple[tuple[int, int, int], ...]
    high_score_rewards: tuple[RankReward, ...]
    overall_pvp_rewards: tuple[RankReward, ...]
    overall_rewards: Mapping[int, tuple[RankReward, ...]]
    high_score_event_id: str = DEFAULT_HIGH_SCORE_EVENT
    high_score_award_event_id: str = DEFAULT_HIGH_SCORE_AWARD_EVENT

    def parallel_world(self, board_id: int) -> ParallelWorldBoard | None:
        for board in self.parallel_worlds:
            if board.board_id == int(board_id):
                return board
        return None

    def parallel_world_for_map(
        self, map_id: str,
    ) -> tuple[ParallelWorldBoard, int] | None:
        key = _map_key(map_id)
        for board in self.parallel_worlds:
            order = board.maps.get(key)
            if order is not None:
                return board, order
        return None

    def overall_points_for_place(self, place: int) -> int:
        for place_from, place_to, points in self.overall_points:
            if place_from <= int(place) <= place_to:
                return points
        return 0


def _rank_rewards(rows, *, value_column: bool = False) -> tuple[RankReward, ...]:
    rewards = []
    for row in rows:
        try:
            place_from = int(row["PlaceFrom"])
            place_to = int(row["PlaceTo"])
        except (TypeError, ValueError):
            continue
        if place_from < 1 or place_to < place_from:
            continue
        rewards.append(RankReward(
            place_from, place_to, parse_reward_items(row["Items"]),
            int(row["IntValue"] or 0) if value_column else 0,
        ))
    return tuple(sorted(rewards, key=lambda reward: reward.place_from))


def _table_exists(database: sqlite3.Connection, table: str) -> bool:
    return database.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
    ).fetchone() is not None


def _load_catalog(path: str, _mtime: int, _size: int) -> LeaderboardCatalog:
    uri = f"file:{Path(path).as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as database:
        database.row_factory = sqlite3.Row
        boards = []
        if _table_exists(database, "_Template_ParallelWorldStructure"):
            for row in database.execute(
                "SELECT * FROM _Template_ParallelWorldStructure "
                "ORDER BY CAST(Id AS INTEGER)"
            ):
                try:
                    board_id = int(row["Id"])
                except (TypeError, ValueError):
                    continue
                if not 0 <= board_id < PARALLEL_WORLD_BOARD_COUNT:
                    continue
                maps = {}
                for token in _tokens(row["ParallelWorldStructure"]):
                    map_id, _separator, order = token.rpartition(":")
                    if map_id and order.strip().isdigit():
                        maps[_map_key(map_id)] = int(order)
                # ``KillOneMonsterOf;boss_grimmag;``: the run ends when any
                # listed template dies.
                trigger = _tokens(row["ParallelWorldEndTrigger"])
                bosses = frozenset(
                    _map_key(value) for value in trigger[1:]
                ) if trigger and trigger[0].casefold() == "killonemonsterof" else frozenset()
                par = {}
                for difficulty in DIFFICULTY_RANGE:
                    try:
                        par[difficulty] = max(0, int(row[f"Diff{difficulty}Time"] or 0))
                    except (IndexError, TypeError, ValueError):
                        par[difficulty] = 0
                boards.append(ParallelWorldBoard(
                    board_id=board_id,
                    group_id=str(row["ParallelWorldGroupId"] or ""),
                    maps=MappingProxyType(maps),
                    final_order=max(maps.values(), default=0),
                    boss_templates=bosses,
                    monster_requirement=max(0, int(row["MonsterRequirement"] or 0)),
                    par_minutes=MappingProxyType(par),
                    bronze=int(row["PWBronzeRequirement"] or 0),
                    silver=int(row["PWSilverRequirement"] or 0),
                    gold=int(row["PWGoldRequirement"] or 0),
                ))
        factors = {}
        if _table_exists(database, "_Template_DifficultyFactors"):
            for row in database.execute(
                "SELECT Id, GauntletScore FROM _Template_DifficultyFactors"
            ):
                try:
                    factors[int(row["Id"])] = float(row["GauntletScore"] or 0.0)
                except (TypeError, ValueError):
                    continue
        globals_row = database.execute("SELECT * FROM _Globals").fetchone()
        keys = set(globals_row.keys()) if globals_row is not None else set()

        def global_value(name, default):
            if name not in keys or globals_row[name] in (None, ""):
                return default
            return globals_row[name]

        overall_points = ()
        if _table_exists(database, "_Template_LeaderBoardOverallranking"):
            overall_points = tuple(sorted(
                (int(row["PlaceFrom"]), int(row["PlaceTo"]), int(row["RankingPoints"]))
                for row in database.execute(
                    "SELECT PlaceFrom, PlaceTo, RankingPoints "
                    "FROM _Template_LeaderBoardOverallranking"
                )
            ))
        high_score = ()
        if _table_exists(database, "_Template_HighScoreRankRewards"):
            high_score = _rank_rewards(database.execute(
                "SELECT PlaceFrom, PlaceTo, Items, IntValue "
                "FROM _Template_HighScoreRankRewards"
            ), value_column=True)
        pvp = ()
        if _table_exists(database, "_Template_OverallPVPRankingRewards"):
            pvp = _rank_rewards(database.execute(
                "SELECT PlaceFrom, PlaceTo, Items FROM _Template_OverallPVPRankingRewards"
            ))
        overall: dict[int, list] = {}
        if _table_exists(database, "_Template_OverallRankingRewards"):
            for row in database.execute(
                "SELECT PlaceFrom, PlaceTo, BoardType, Items "
                "FROM _Template_OverallRankingRewards"
            ):
                overall.setdefault(int(row["BoardType"] or 0), []).append(row)
    return LeaderboardCatalog(
        parallel_worlds=tuple(boards),
        difficulty_factors=MappingProxyType(factors),
        time_points=int(global_value("GauntletTimePoints", 0)),
        monster_points=int(global_value("GauntletMonsterKillPoints", 0)),
        boss_points=int(global_value("GauntletBossKillPoints", 0)),
        completion_bonus=float(global_value("GauntletCompletionBonus", 0.0)),
        overall_points=overall_points,
        high_score_rewards=high_score,
        overall_pvp_rewards=pvp,
        overall_rewards=MappingProxyType({
            board_type: _rank_rewards(rows)
            for board_type, rows in overall.items()
        }),
        high_score_event_id=str(global_value(
            "HighScoreRankingEventId", DEFAULT_HIGH_SCORE_EVENT)),
        high_score_award_event_id=str(global_value(
            "HighScoreRankAwardEventId", DEFAULT_HIGH_SCORE_AWARD_EVENT)),
    )


_load_catalog_cached = lru_cache(maxsize=4)(_load_catalog)


def load_leaderboard_catalog(static_database_path: str | Path) -> LeaderboardCatalog:
    path = Path(static_database_path)
    stat = resident_content.stat(path)
    return _load_catalog_cached(str(path), stat.st_mtime_ns, stat.st_size)


# Parallel World run scoring. Speed decides the score, a run that ends early
# keeps the points of the maps it completed, and finishing the whole chain is
# worth far more than any unfinished run.
#
# Authored inputs: ``_Globals.GauntletTimePoints`` (1200),
# ``GauntletMonsterKillPoints`` (2400), ``GauntletBossKillPoints`` (480),
# ``GauntletCompletionBonus`` (0.15, "By completing this dungeon, you will
# earn +15% bonus points"), ``_Template_DifficultyFactors.GauntletScore``
# (2.0 .. 15.0), and per structure ``DiffNTime`` (par minutes for the whole
# chain at difficulty N), ``MonsterRequirement`` and the ordered maps. The
# client only shows them as percentages ("Time {0:i}%", "Monsters defeated
# {0:i}%"); the retail combination ran on the ranking server, so the shape
# below is a local choice:
#
#   F       = GauntletScore(difficulty) * POINTS_PER_PERCENT
#   speed(par, t) = min(1, par / t)            ("Time %", faster scores more)
#   map k   : F * GauntletTimePoints * speed(par / N, seconds spent on map k)
#             for every completed map (k = 1..N)
#   monsters: F * GauntletMonsterKillPoints * min(1, kills / MonsterRequirement)
#             * completed / N * speed(par * completed / N, seconds so far)
#   boss    : F * GauntletBossKillPoints                     (full runs only)
#   bonus   : (maps + monsters + boss) * GauntletCompletionBonus (full only)
#   finish  : F * (N * GauntletTimePoints + GauntletMonsterKillPoints)
#             * (1 + speed(par, whole run seconds))          (full only)
#
# The finish component alone exceeds the most an unfinished run can earn
# (at most N - 1 maps of speed points plus (N - 1)/N of the monster points),
# whatever the speeds, so any completed run outranks every partial one.
# Monster points are paced like the maps, so below par a faster run gains
# more than one that kills more at the same pace: speed is the main driver.
POINTS_PER_PERCENT = 100


def _speed(par_seconds: float, seconds: float) -> float:
    if par_seconds <= 0:
        return 0.0
    return min(1.0, par_seconds / max(1.0, float(seconds)))


def score_parallel_world_run(
    catalog: LeaderboardCatalog,
    board: ParallelWorldBoard,
    *,
    difficulty: int,
    map_seconds: Iterable[float],
    kills: int,
    boss_killed: bool,
) -> ParallelWorldRunScore:
    """Score the completed maps of one run (see the formula above).

    ``map_seconds`` lists, in order, the time spent on each completed map:
    from entering it to entering the next map, and for the last map until
    the end boss died. ``boss_killed`` only counts once every map is done.
    """

    difficulty = max(0, min(max(DIFFICULTY_RANGE), int(difficulty)))
    scale = POINTS_PER_PERCENT * max(
        0.0, float(catalog.difficulty_factors.get(difficulty, 1.0)))
    maps = max(1, int(board.final_order))
    seconds = [max(0.0, float(value)) for value in map_seconds][:maps]
    completed = len(seconds)
    full = bool(boss_killed) and completed == maps
    par = int(board.par_minutes.get(difficulty, 0)) * 60
    run_seconds = sum(seconds)
    map_points = sum(
        scale * catalog.time_points * _speed(par / maps, value) for value in seconds
    )
    requirement = max(1, int(board.monster_requirement))
    monster_points = (
        scale * catalog.monster_points
        * min(1.0, max(0, int(kills)) / requirement) * completed / maps
        * _speed(par * completed / maps, run_seconds)
    )
    boss_points = scale * catalog.boss_points if full else 0.0
    bonus = (
        (map_points + monster_points + boss_points)
        * max(0.0, float(catalog.completion_bonus)) if full else 0.0
    )
    finish = (
        scale * (maps * catalog.time_points + catalog.monster_points)
        * (1.0 + _speed(par, run_seconds)) if full else 0.0
    )
    clamp = lambda value: max(0, min(0xFFFFFFFF, int(round(value))))
    time_points = clamp(map_points + finish)
    monster = clamp(monster_points)
    boss = clamp(boss_points)
    return ParallelWorldRunScore(
        clamp(time_points + monster + boss + bonus), time_points, monster, boss,
        clamp(run_seconds), clamp(kills), int(full), difficulty, completed,
    )


# ---------------------------------------------------------------------------
# Ranking periods
# ---------------------------------------------------------------------------

ALL_TIME_PERIOD = "all"


@dataclass(frozen=True)
class RankingPeriod:
    key: str
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    def contains(self, moment: datetime) -> bool:
        if self.starts_at is not None and moment < self.starts_at:
            return False
        return self.ends_at is None or moment < self.ends_at


def _utc(moment: datetime | None) -> datetime:
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def day_period(now: datetime | None = None) -> RankingPeriod:
    """UTC calendar day ("Daily Honor Points", honor board 2)."""

    moment = _utc(now)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return RankingPeriod(f"day:{start.date().isoformat()}", start, start + timedelta(days=1))


def week_period(now: datetime | None = None) -> RankingPeriod:
    """ISO week from Monday 00:00 UTC ("Weekly Honor Points", board 3)."""

    moment = _utc(now)
    start = (moment - timedelta(days=moment.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    year, week, _day = start.isocalendar()
    return RankingPeriod(f"week:{year}-W{week:02d}", start, start + timedelta(days=7))


def event_period(event) -> RankingPeriod:
    """Period of one scheduled season/ranking event instance."""

    return RankingPeriod(
        f"event:{int(event.event_id)}",
        getattr(event, "starts_at", None), getattr(event, "ends_at", None),
    )


def period_bound(text: str | None) -> datetime | None:
    if not text:
        return None
    return _utc(datetime.fromisoformat(str(text).replace("Z", "+00:00")))


__all__ = [
    "ALL_TIME_PERIOD", "DEFAULT_HIGH_SCORE_AWARD_EVENT", "DEFAULT_HIGH_SCORE_EVENT",
    "LeaderboardCatalog", "PARALLEL_WORLD_BOARD_COUNT", "POINTS_PER_PERCENT",
    "PVE_BOARD_TYPES", "PVE_SEASON_EVENT", "PVP_SEASON_EVENT",
    "ParallelWorldBoard", "ParallelWorldRunScore", "RankReward", "RankingPeriod",
    "day_period", "event_period", "load_leaderboard_catalog", "parse_reward_items",
    "period_bound", "reward_for_place", "score_parallel_world_run", "week_period",
]
