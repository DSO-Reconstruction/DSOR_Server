"""Leaderboards: honor, Parallel World, overall PvE and Fractured Planes.

Every board the leaderboard window can request is answered from durable,
server-measured results (docs/LEADERBOARDS.md):

* Honor boards read persisted career results with live sessions overlaid.
  Daily, weekly and PvP-season honor are the career counters minus the
  counters recorded when the period opened; solo/team boards accumulate the
  results of settled matches by mode.
* Parallel World boards score runs by speed through a structure's maps:
  each completed map is recorded as it completes, and killing the authored
  end boss finishes the run for far more; the PvE "Overall Rank" sums the
  authored ranking points of each board place.
* Fractured Planes (and the client's older "Challenge Ranking" tab, which
  asks for the same high-score list) record the highest floor cleared and
  the run time remaining at that clear.

Season boards follow the scheduled ``pvp_seasons_event``/``pve_season_event``
and the ``HighScoreRankingEventId`` event; when none is active the boards
show the all-time results. When a season ends a background tick freezes its
standings and grants the authored place rewards exactly once (receipts in
:mod:`dso_server.persistence.ranking_state`).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import math
import threading
import time

from dso_server.gameplay.leaderboards import (
    ALL_TIME_PERIOD, PARALLEL_WORLD_BOARD_COUNT, PVE_BOARD_TYPES,
    PVE_SEASON_EVENT, PVP_SEASON_EVENT, RankingPeriod, day_period,
    event_period, load_leaderboard_catalog, period_bound, reward_for_place,
    score_parallel_world_run, week_period,
)
from dso_server.persistence.account_state import HonorRankingRecord
from dso_server.persistence.ranking_state import (
    STATUS_DELIVERED, STATUS_NONE, STATUS_PENDING, Entitlement,
    RankingStateStore, SnapshotRow,
)
from dso_server.protocol.ranking_protocol import (
    ALL_CLASSES, FILTER_ALL, FILTER_FRIENDS, FILTER_GUILD, FILTER_PLAYER_PAGE,
    GET_HIGH_SCORE_RANK_ALL_DATA_COMMAND, HIGH_SCORE_COMMANDS,
    HONOR_LOCAL_USER_RANKING_INFO_COMMAND, HONOR_OVERALL_LOCAL_USER_RANKING_INFO_COMMAND,
    HONOR_OVERALL_USER_RANKING_INFO_COMMAND, MAX_RANKING_PAGE_SIZE,
    OVERALL_LOCAL_USER_RANKING_INFO_COMMAND, OVERALL_USER_RANKING_INFO_COMMAND,
    PARALLEL_WORLD_LOCAL_USER_RANKING_INFO_COMMAND,
    PARALLEL_WORLD_USER_RANKING_INFO_COMMAND, UNRANKED,
    HighScoreEntry, ParallelWorldRow, ParallelWorldSummary, RankingRow,
    decode_ranking_request, encode_high_score_all_data,
    encode_high_score_group_data, encode_local_ranking, encode_overall_local,
    encode_overall_page, encode_parallel_world_local, encode_parallel_world_page,
    encode_ranking_page,
)
from dso_server.protocol.pvp_match_protocol import (
    MATCH_MODE_BEST_OF_3_1V1, MATCH_MODE_LAST_MAN_STANDING_8FFA,
)
from dso_server.protocol.reward_protocol import RewardDefinition
from dso_server.services.base import CharacterService


RANKING_SNAPSHOT_SECONDS = 1.0
MAX_CACHED_RANKING_BOARDS = 8
# Local choice: how often the background tick opens periods, settles ended
# seasons and retries pending deliveries. Period boundaries are therefore
# observed within this many seconds.
RANKING_SCHEDULER_SECONDS = 30.0
# The high-score result carries the whole list (the client pages it itself);
# the last authored _Template_HighScoreRankRewards place range ends at 999.
MAX_HIGH_SCORE_ENTRIES = 999

# Period families (one settlement receipt per family and period).
HONOR_DAILY = "honor.daily"
HONOR_WEEKLY = "honor.weekly"
PVP_SEASON = "pvp.season"
PVE_SEASON = "pve.season"
FP_SEASON = "fp.season"
# Boards (score rows, history snapshots and entitlements).
BOARD_PVP_OVERALL = "pvp.overall"
BOARD_PVP_SOLO = "pvp.solo"
BOARD_PVP_TEAM = "pvp.team"
BOARD_FRACTURED_PLANES = "fp"
BOARD_FRACTURED_PLANES_GROUP = "fp.group"
HIGH_SCORE_ACHIEVEMENT_KIND = "highscoreranking"
_HONOR_BOARD_PERIODS = {2: HONOR_DAILY, 3: HONOR_WEEKLY}
# Solo board modes, as BattlegroundService counts solo/team matches.
SOLO_MATCH_MODES = frozenset({MATCH_MODE_BEST_OF_3_1V1, MATCH_MODE_LAST_MAN_STANDING_8FFA})


def parallel_world_board(board: int, board_type: int) -> str:
    return f"pw.{int(board)}.{int(board_type)}"


def overall_pve_board(board_type: int) -> str:
    return f"pve.overall.{int(board_type)}"


def _log(message: str) -> None:
    print(f"[DrasaOnlineMapServer] ranking: {message}")


def _utc(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime | None) -> str:
    return moment.isoformat() if moment is not None else ""


@dataclass(frozen=True)
class BoardRecord:
    """One ranked character on a non-honor board.

    ``values`` are the sortable columns (column 0 is the board score);
    ``order`` breaks ties after the requested column, best first.
    """

    character_id: int
    name: str
    class_id: int
    values: tuple[int, ...]
    order: tuple[int, ...] = ()
    extra: tuple[int, ...] = ()


def _sort_key(column):
    if isinstance(column, str):
        return lambda row: (-getattr(row, column, 0), row.character_id)
    return lambda row: (
        -row.values[column], *(-value for value in row.order), row.character_id,
    )


class _RankingSnapshot:
    """Share bounded, briefly cached rankings across all page requests."""

    def __init__(self):
        self.lock = threading.Lock()
        self.expires_at = 0.0
        self.records = {}
        self.boards = OrderedDict()

    def board(self, loader, class_id, column, allowed=None):
        with self.lock:
            if time.monotonic() >= self.expires_at:
                records = loader()
                self.records = {row.character_id: row for row in records}
                self.boards.clear()
                self.expires_at = time.monotonic() + RANKING_SNAPSHOT_SECONDS
            key = (class_id, column)
            if allowed is None and key in self.boards:
                self.boards.move_to_end(key)
                return self.boards[key]
            candidates = (
                self.records.values() if allowed is None else
                (self.records[identity] for identity in allowed
                 if identity in self.records)
            )
            ranked = tuple(sorted(
                (row for row in candidates if class_id == ALL_CLASSES or row.class_id == class_id),
                key=_sort_key(column),
            ))
            result = ranked, {row.character_id: i for i, row in enumerate(ranked)}
            if allowed is None:
                self.boards[key] = result
                if len(self.boards) > MAX_CACHED_RANKING_BOARDS:
                    self.boards.popitem(last=False)
            return result


@dataclass(frozen=True)
class PvPModeRecord:
    """A solo/team board row: honor and results of that mode's matches."""

    character_id: int
    name: str
    class_id: int
    honor_points: int = 0
    wins: int = 0
    losses: int = 0
    kills: int = 0
    deaths: int = 0
    draws: int = 0


def _wire_row(record, rank):
    return RankingRow(
        record.name, min(record.honor_points, 0xFFFFFFFF), rank,
        min(record.wins, 0xFFFFFFFF), min(record.losses, 0xFFFFFFFF),
        min(getattr(record, "draws", 0), 0xFFFFFFFF),
        min(record.kills, 0xFFFFFFFF), min(record.deaths, 0xFFFFFFFF),
    )


@dataclass
class ParallelWorldRun:
    """A Parallel World run in progress (see ``_ranking_runs``).

    ``map_seconds`` holds the time spent on each completed map; the map
    being played started at ``map_started`` (``time.monotonic()``).
    """

    board_id: int
    difficulty: int
    map_started: float
    order: int = 1
    kills: int = 0
    group: bool = False
    map_seconds: list = field(default_factory=list)


_STORES: dict[str, RankingStateStore] = {}
_STORES_LOCK = threading.Lock()


def ranking_store_for(path: str | Path) -> RankingStateStore:
    """One store per database file, shared by every service instance."""

    key = str(Path(path).resolve())
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = _STORES[key] = RankingStateStore(key)
        return store


class RankingService:
    """Serve leaderboard queries and own score recording and settlement."""

    # ------------------------------------------------------------------
    # Stores, catalogs and periods
    # ------------------------------------------------------------------

    def _ranking_store(self) -> RankingStateStore:
        store = getattr(self, "ranking_store", None)
        if store is not None:
            return store
        with _STORES_LOCK:
            store = self.__dict__.get("ranking_store")
            if store is not None:
                return store
            account_path = getattr(getattr(self, "account_store", None), "path", None)
        if account_path is not None:
            # Beside the player database, so they move and back up together.
            store = ranking_store_for(Path(account_path).with_name("ranking.sqlite3"))
        else:
            store = RankingStateStore(":memory:")
        self.__dict__.setdefault("ranking_store", store)
        return self.__dict__["ranking_store"]

    def _leaderboard_catalog(self):
        path = getattr(self, "static_database_path", None)
        if path is None:
            from dso_server.paths import DEFAULT_STATIC_DB_PATH
            path = DEFAULT_STATIC_DB_PATH
        return load_leaderboard_catalog(path)

    def _ranking_event_catalog(self):
        try:
            return CharacterService._event_catalog(self)
        except Exception as exc:  # A missing schedule means no seasons.
            if not getattr(self, "_ranking_event_catalog_warned", False):
                self.__dict__["_ranking_event_catalog_warned"] = True
                _log(f"event schedule unavailable ({type(exc).__name__}); no seasons")
            return None

    def _ranking_season_template(self, family: str) -> str:
        if family == PVP_SEASON:
            return PVP_SEASON_EVENT
        if family == PVE_SEASON:
            return PVE_SEASON_EVENT
        return self._leaderboard_catalog().high_score_event_id

    def _ranking_season_events(self, family: str, now: datetime, catalog=None):
        catalog = catalog if catalog is not None else self._ranking_event_catalog()
        if catalog is None:
            return ()
        template = self._ranking_season_template(family).casefold()
        return tuple(
            (event, event.state_at(now)) for event in catalog.events
            if event.template.resource_id.casefold() == template
        )

    def _ranking_active_season(self, family: str, now: datetime) -> RankingPeriod | None:
        active = [event for event, state in self._ranking_season_events(family, now)
                  if state == "active"]
        if not active:
            return None
        return event_period(min(active, key=lambda event: event.event_id))

    def _ranking_open_period(self, family: str, period: RankingPeriod,
                             now: datetime) -> None:
        store = self._ranking_store()
        if store.period(family, period.key) is not None:
            return
        if family in (HONOR_DAILY, HONOR_WEEKLY, PVP_SEASON):
            # Career counters at the period start; honor gained later is the
            # period result. Characters created later start from zero.
            store.put_baselines(period.key, {
                record.character_id: (
                    record.honor_points, record.wins, record.losses,
                    record.kills, record.deaths,
                )
                for record in self._honor_ranking_records(None)
            })
        if store.open_period(family, period.key, starts_at=_iso(period.starts_at),
                             ends_at=_iso(period.ends_at), now=now):
            _log(f"opened {family} period {period.key}")

    def _ranking_record_periods(self, family: str, now: datetime) -> tuple[str, ...]:
        """All-time plus the active season, which is opened on first use."""

        season = self._ranking_active_season(family, now)
        if season is None:
            return (ALL_TIME_PERIOD,)
        self._ranking_open_period(family, season, now)
        return (ALL_TIME_PERIOD, season.key)

    def _ranking_display_period(self, family: str, now: datetime) -> str:
        season = self._ranking_active_season(family, now)
        if season is None:
            return ALL_TIME_PERIOD
        self._ranking_open_period(family, season, now)
        return season.key

    # ------------------------------------------------------------------
    # Board records
    # ------------------------------------------------------------------

    def _honor_ranking_records(self, requester):
        store = getattr(self, "account_store", None)
        records = {row.character_id: row for row in store.honor_ranking_records()} if store else {}
        # Include offline characters, then overlay scores awaiting persistence.
        # Take each session lock separately, outside the account-store lock.
        sessions = list(self._active_gameplay_sessions())
        if requester is not None and requester not in sessions:
            sessions.append(requester)
        for session in sessions:
            with session.flow_lock:
                character = session.gameplay_character
                if character is None:
                    continue
                records[int(character.character_id)] = HonorRankingRecord(
                    int(character.character_id), str(character.name), int(character.class_id),
                    max(0, int(session.gameplay_honor_points)),
                    max(0, int(session.gameplay_pvp_matches_won)),
                    max(0, int(session.gameplay_pvp_matches_lost)),
                    max(0, int(session.gameplay_pvp_kills)),
                    max(0, int(session.gameplay_pvp_deaths)),
                )
        return tuple(records.values())

    def _honor_period_records(self, requester, period: str):
        """Counters gained since the period opened (characters with activity)."""

        baselines = self._ranking_store().baselines(period)
        result = []
        for record in self._honor_ranking_records(requester):
            base = tuple(baselines.get(record.character_id, ())) + (0,) * 5
            delta = HonorRankingRecord(
                record.character_id, record.name, record.class_id,
                *(max(0, value - base[index]) for index, value in enumerate((
                    record.honor_points, record.wins, record.losses,
                    record.kills, record.deaths,
                ))),
            )
            if any((delta.honor_points, delta.wins, delta.losses, delta.kills, delta.deaths)):
                result.append(delta)
        return tuple(result)

    def _ranking_profiles(self, requester=None) -> dict[int, tuple[str, int]]:
        return {
            record.character_id: (record.name, record.class_id)
            for record in self._honor_ranking_records(requester)
        }

    def _pvp_mode_records(self, requester, board: str, period: str):
        profiles = self._ranking_profiles(requester)
        result = []
        for identity, score in self._ranking_store().scores(board, period).items():
            if identity not in profiles:
                continue
            stats = tuple(score.stats) + (0,) * 6
            name, class_id = profiles[identity]
            result.append(PvPModeRecord(
                identity, name, class_id, stats[0], stats[1], stats[2], stats[4], stats[5],
                stats[3],
            ))
        return tuple(result)

    def _parallel_world_records(self, requester, board: int, board_type: int, period: str):
        profiles = self._ranking_profiles(requester)
        result = []
        for identity, score in self._ranking_store().scores(
                parallel_world_board(board, board_type), period).items():
            if identity not in profiles:
                continue
            stats = tuple(score.stats) + (0,) * 9
            name, class_id = profiles[identity]
            result.append(BoardRecord(identity, name, class_id, stats[:4], (score.tiebreak,), stats[4:9]))
        return tuple(result)

    def _overall_pve_records(self, requester, board_type: int, period: str):
        """Sum ``_Template_LeaderBoardOverallranking`` points over the boards."""

        catalog = self._leaderboard_catalog()
        profiles = self._ranking_profiles(requester)
        points: dict[int, int] = {}
        for board in range(PARALLEL_WORLD_BOARD_COUNT):
            ranked = sorted(self._parallel_world_records(requester, board, board_type, period),
                            key=_sort_key(0))
            for place, record in enumerate(ranked, 1):
                gained = catalog.overall_points_for_place(place)
                if gained:
                    points[record.character_id] = points.get(record.character_id, 0) + gained
        return tuple(
            BoardRecord(identity, *profiles[identity], (total,))
            for identity, total in points.items() if identity in profiles
        )

    def _fractured_planes_records(self, requester, period: str):
        profiles = self._ranking_profiles(requester)
        result = []
        for identity, score in self._ranking_store().scores(
                BOARD_FRACTURED_PLANES, period).items():
            if identity not in profiles or score.score <= 0:
                continue
            name, class_id = profiles[identity]
            result.append(BoardRecord(identity, name, class_id, (score.score,), (score.tiebreak,)))
        return tuple(result)

    def _ranking_snapshot(self, key):
        if key == ("honor", "career"):
            snapshot = getattr(self, "_honor_ranking_snapshot", None)
            if snapshot is None:
                snapshot = self.__dict__.setdefault("_honor_ranking_snapshot", _RankingSnapshot())
            return snapshot
        snapshots = self.__dict__.setdefault("_ranking_snapshots", {})
        snapshot = snapshots.get(key)
        if snapshot is None:
            snapshot = snapshots.setdefault(key, _RankingSnapshot())
            # Bound the number of distinct period snapshots kept alive.
            while len(snapshots) > 4 * MAX_CACHED_RANKING_BOARDS:
                snapshots.pop(next(iter(snapshots)))
        return snapshot

    def _ranking_allowed(self, character_id: int, flags: int):
        if flags & FILTER_ALL:
            return None
        allowed = set()
        social = getattr(self, "social_store", None)
        if flags & FILTER_FRIENDS:
            allowed.add(character_id)
            if social is not None:
                allowed.update(social.buddies_for(character_id))
        if flags & FILTER_GUILD and social is not None:
            guild = social.guild_for(character_id)
            if guild is not None:
                allowed.update(member.character_id for member in guild.members)
        return allowed

    @staticmethod
    def _ranking_page(request, ranked, positions, character_id):
        count = min(request.count, MAX_RANKING_PAGE_SIZE)
        start = request.start
        if request.flags & FILTER_PLAYER_PAGE:
            index = positions.get(character_id, 0)
            start = max(0, index - count // 2)
            start = min(start, max(0, len(ranked) - count))
        elif start == UNRANKED:
            start = 0
        start = min(start, len(ranked))
        return start, ranked[start:start + count]

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def _handle_ranking_request(self, s, payload: bytes) -> None:
        try:
            request = decode_ranking_request(payload, s.current_message_bit_length)
        except ValueError as exc:
            print(f"[DrasaOnlineMapServer] rejected ranking request: {exc}")
            return
        with s.flow_lock:
            character = s.gameplay_character
            if character is None:
                return
            character_id = int(character.character_id)
            s.gameplay_client_command_state[request.command_id] = request
        CharacterService._accept_gameplay_state_command(s, request.command_id)
        now = datetime.now(timezone.utc)
        if request.command_id in HIGH_SCORE_COMMANDS:
            response, label = self._high_score_response(s, request, now), "high-score-ranking"
        elif request.command_id in {PARALLEL_WORLD_USER_RANKING_INFO_COMMAND,
                                    PARALLEL_WORLD_LOCAL_USER_RANKING_INFO_COMMAND,
                                    OVERALL_USER_RANKING_INFO_COMMAND,
                                    OVERALL_LOCAL_USER_RANKING_INFO_COMMAND}:
            response, label = self._pve_ranking_response(
                s, request, character, character_id, now), "pve-ranking"
        else:
            response, label = self._honor_ranking_response(
                s, request, character, character_id, now), "honor-ranking"
        s.send_reliable_ordered(response, track=True, label=label)

    def _honor_ranking_response(self, s, request, character, character_id, now):
        local = request.command_id in {
            HONOR_LOCAL_USER_RANKING_INFO_COMMAND,
            HONOR_OVERALL_LOCAL_USER_RANKING_INFO_COMMAND,
        }
        class_id = int(character.class_id) if local else request.class_id
        allowed = None if local else self._ranking_allowed(character_id, request.flags)
        column = ("honor_points", "wins", "losses", "draws", "kills", "deaths")[request.sort_column]
        overall = request.command_id in {
            HONOR_OVERALL_USER_RANKING_INFO_COMMAND,
            HONOR_OVERALL_LOCAL_USER_RANKING_INFO_COMMAND,
        }
        ranked, positions = (), {}
        if overall:
            # PvP "Overall Rank": honor gained this PvP season, career honor
            # when no season is scheduled.
            period = self._ranking_display_period(PVP_SEASON, now)
            if period == ALL_TIME_PERIOD:
                ranked, positions = self._ranking_snapshot(("honor", "career")).board(
                    lambda: self._honor_ranking_records(s), class_id, column, allowed)
            else:
                ranked, positions = self._ranking_snapshot(("honor", period)).board(
                    lambda: self._honor_period_records(s, period), class_id, column, allowed)
        elif request.board == 4:
            ranked, positions = self._ranking_snapshot(("honor", "career")).board(
                lambda: self._honor_ranking_records(s), class_id, column, allowed)
        elif request.board in _HONOR_BOARD_PERIODS:
            family = _HONOR_BOARD_PERIODS[request.board]
            period = (day_period if family == HONOR_DAILY else week_period)(now)
            self._ranking_open_period(family, period, now)
            ranked, positions = self._ranking_snapshot(("honor", period.key)).board(
                lambda: self._honor_period_records(s, period.key), class_id, column, allowed)
        elif request.board in (0, 1):
            board = BOARD_PVP_SOLO if request.board == 0 else BOARD_PVP_TEAM
            period = self._ranking_display_period(PVP_SEASON, now)
            ranked, positions = self._ranking_snapshot((board, period)).board(
                lambda: self._pvp_mode_records(s, board, period), class_id, column, allowed)
        if local:
            index = positions.get(character_id)
            own = None if index is None else _wire_row(ranked[index], index)
            return encode_local_ranking(request, own)
        start, rows = self._ranking_page(request, ranked, positions, character_id)
        return encode_ranking_page(
            request, tuple(_wire_row(record, start + offset)
                           for offset, record in enumerate(rows)),
            start=start, total=len(ranked),
        )

    def _pve_ranking_response(self, s, request, character, character_id, now):
        period = self._ranking_display_period(PVE_SEASON, now)
        if request.command_id in {OVERALL_USER_RANKING_INFO_COMMAND,
                                  OVERALL_LOCAL_USER_RANKING_INFO_COMMAND}:
            loader = lambda: self._overall_pve_records(s, request.board_type, period)
            key = (overall_pve_board(request.board_type), period)
            column = 0
        else:
            loader = lambda: self._parallel_world_records(
                s, request.board, request.board_type, period)
            key = (parallel_world_board(request.board, request.board_type), period)
            column = request.sort_column
        local = request.command_id in {OVERALL_LOCAL_USER_RANKING_INFO_COMMAND,
                                       PARALLEL_WORLD_LOCAL_USER_RANKING_INFO_COMMAND}
        class_id = ALL_CLASSES if local else request.class_id
        allowed = None if local else self._ranking_allowed(character_id, request.flags)
        ranked, positions = self._ranking_snapshot(key).board(loader, class_id, column, allowed)
        if request.command_id == OVERALL_LOCAL_USER_RANKING_INFO_COMMAND:
            return encode_overall_local(request, positions.get(character_id))
        if request.command_id == PARALLEL_WORLD_LOCAL_USER_RANKING_INFO_COMMAND:
            index = positions.get(character_id)
            summary = None
            if index is not None:
                record = ranked[index]
                elapsed, kills, bosses = record.extra[:3]
                summary = ParallelWorldSummary(record.values[0], elapsed, kills, bosses)
            return encode_parallel_world_local(request, summary)
        start, rows = self._ranking_page(request, ranked, positions, character_id)
        if request.command_id == OVERALL_USER_RANKING_INFO_COMMAND:
            return encode_overall_page(request, tuple(
                RankingRow(record.name, record.values[0], start + offset)
                for offset, record in enumerate(rows)
            ), start=start, total=len(ranked))
        return encode_parallel_world_page(request, tuple(
            ParallelWorldRow(record.name, start + offset, tuple(record.values[:4]))
            for offset, record in enumerate(rows)
        ), start=start, total=len(ranked))

    def _high_score_response(self, s, request, now):
        period = self._ranking_display_period(FP_SEASON, now)
        class_id = (request.class_id
                    if request.command_id == GET_HIGH_SCORE_RANK_ALL_DATA_COMMAND
                    else ALL_CLASSES)
        ranked, _positions = self._ranking_snapshot((BOARD_FRACTURED_PLANES, period)).board(
            lambda: self._fractured_planes_records(s, period), class_id, 0)
        entries = tuple(
            HighScoreEntry(record.name, record.values[0], record.order[0])
            for record in ranked[:MAX_HIGH_SCORE_ENTRIES]
        )
        if request.command_id == GET_HIGH_SCORE_RANK_ALL_DATA_COMMAND:
            return encode_high_score_all_data(request.class_id, entries)
        return encode_high_score_group_data(entries)

    # ------------------------------------------------------------------
    # Score recording (authoritative transitions)
    # ------------------------------------------------------------------

    def _ranking_instance_players(self, s) -> int:
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            return 1
        try:
            return max(1, len(self._map_event_sessions(runtime)))
        except Exception:
            return 1

    def _ranking_runs(self):
        """Runs in progress by character id.

        A map transfer arrives on a new session object, so a run spanning a
        structure's maps lives on the service, not on the session. Runs are
        in memory only: every completed map is recorded the moment it
        completes, so a restart loses only the unfinished map and never
        leaves an entry for maps that were not completed.
        """

        runs = self.__dict__.get("_ranking_pw_runs")
        if runs is None:
            runs = self.__dict__.setdefault("_ranking_pw_runs", ({}, threading.Lock()))
        return runs

    def _ranking_on_map_entered(self, s) -> None:
        """Deliver settled rewards on arrival and track Parallel World runs."""

        try:
            self._ranking_ensure_scheduler()
            self._ranking_deliver_pending(s)
        except Exception as exc:
            _log(f"pending delivery failed: {type(exc).__name__}: {exc}")
        try:
            self._ranking_track_map(s)
        except Exception as exc:  # Never disturb map entry.
            _log(f"run tracking failed: {type(exc).__name__}: {exc}")

    def _ranking_track_map(self, s) -> None:
        """Start, advance or end a run on every map arrival.

        * The structure's first map starts a new run (ending any other).
        * The next map in order completes the current one: its time is
          added and the partial result is recorded immediately.
        * Any other arrival ends the run: a town or hub (leaving, logging
          out to town, respawning in town), another structure, or the run's
          current map again, which only a relog or reconnect re-initializes.
          Arriving mid-structure without a run (relog, teleport) starts none.
        """

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        map_id = str(getattr(runtime, "map_id", "") or getattr(s, "gameplay_map", "") or "")
        with s.flow_lock:
            character = s.gameplay_character
            difficulty = max(int(getattr(s, "gameplay_map_difficulty", 0) or 0),
                             int(getattr(s, "gameplay_scaling_difficulty", 0) or 0))
        if character is None:
            return
        catalog = self._leaderboard_catalog()
        found = catalog.parallel_world_for_map(map_id)
        players = self._ranking_instance_players(s) if found is not None else 1
        runs, lock = self._ranking_runs()
        identity = int(character.character_id)
        now = time.monotonic()
        completed = None
        with lock:
            run = runs.pop(identity, None)
            if found is None:
                return
            board, order = found
            if order == 1:
                run = ParallelWorldRun(board.board_id, difficulty, now)
            elif run is not None and run.board_id == board.board_id and order == run.order + 1:
                run.map_seconds.append(now - run.map_started)
                run.order, run.map_started = order, now
                completed = run
            else:
                return
            run.group = run.group or players > 1
            if completed is not None:
                completed = ParallelWorldRun(
                    run.board_id, run.difficulty, run.map_started, run.order,
                    run.kills, run.group, list(run.map_seconds))
            runs[identity] = run
        if completed is not None:
            self._ranking_record_parallel_world_run(
                character, catalog, board, completed, boss_killed=False)

    def _ranking_on_monsters_killed(self, s, monsters) -> None:
        """Count run kills; the authored end boss completes the run."""

        try:
            self._ranking_count_kills(s, tuple(monsters))
        except Exception as exc:  # Never disturb kill processing.
            _log(f"run kill tracking failed: {type(exc).__name__}: {exc}")

    def _ranking_count_kills(self, s, monsters) -> None:
        with s.flow_lock:
            character = s.gameplay_character
        if character is None or not monsters:
            return
        runs, lock = self._ranking_runs()
        identity = int(character.character_id)
        with lock:
            run = runs.get(identity)
        if run is None:
            return
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        map_id = str(getattr(runtime, "map_id", "") or getattr(s, "gameplay_map", "") or "")
        catalog = self._leaderboard_catalog()
        found = catalog.parallel_world_for_map(map_id)
        if found is None or found[0].board_id != run.board_id or found[1] != run.order:
            return
        board, order = found
        templates = {str(getattr(monster, "template_id", "")).casefold() for monster in monsters}
        players = self._ranking_instance_players(s)
        with lock:
            if runs.get(identity) is not run:
                return
            run.kills += len(monsters)
            run.group = run.group or players > 1
        if order != board.final_order or not (board.boss_templates & templates):
            return
        # The boss death ends the run for every participant in the instance.
        participants = [s]
        if runtime is not None:
            try:
                participants.extend(
                    viewer for viewer in self._map_event_sessions(runtime) if viewer is not s)
            except Exception:
                pass
        for participant in participants:
            self._ranking_complete_parallel_world_run(participant, catalog, board, len(participants))

    def _ranking_complete_parallel_world_run(self, s, catalog, board, players: int) -> None:
        with s.flow_lock:
            character = s.gameplay_character
        if character is None:
            return
        runs, lock = self._ranking_runs()
        with lock:
            run = runs.get(int(character.character_id))
            if run is None or run.board_id != board.board_id or run.order != board.final_order:
                return
            runs.pop(int(character.character_id), None)
            run.map_seconds.append(time.monotonic() - run.map_started)
            run.group = run.group or players > 1
        self._ranking_record_parallel_world_run(character, catalog, board, run, boss_killed=True)

    def _ranking_record_parallel_world_run(
            self, character, catalog, board, run: ParallelWorldRun, *,
            boss_killed: bool) -> None:
        """Keep the run's score if it beats the character's best on the board.

        A partial result is written as each map completes; each further map
        and the finished run of the same attempt score more and replace it.
        The best result per board and period is kept.
        """

        score = score_parallel_world_run(
            catalog, board, difficulty=run.difficulty, map_seconds=run.map_seconds,
            kills=run.kills, boss_killed=boss_killed,
        )
        board_type = 1 if run.group else 0
        now = datetime.now(timezone.utc)
        store = self._ranking_store()
        for period in self._ranking_record_periods(PVE_SEASON, now):
            store.improve_score(
                parallel_world_board(board.board_id, board_type), period,
                int(character.character_id), score=score.total,
                tiebreak=-score.elapsed_seconds,
                stats=(*score.row_stats, score.elapsed_seconds, score.kills,
                       score.bosses, score.difficulty, score.maps_completed),
                now=now,
            )
        state = "completed" if score.completed else (
            f"{score.maps_completed}/{board.final_order} maps")
        _log(
            f"{character.name!r} {board.group_id} run {state} "
            f"({'group' if run.group else 'solo'}, difficulty {score.difficulty}): "
            f"{score.total} points in {score.elapsed_seconds}s, {score.kills} kills"
        )

    def _ranking_record_fractured_planes_floor(
            self, s, floor: int, deadline: float | None) -> None:
        """Record a cleared floor and the run time left at that clear.

        ``deadline`` is the run's ``time.perf_counter()`` limit, which
        Fractured Planes carries from floor to floor (map_events
        ``fractured_planes_limit_deadline``); the high-score list shows the
        time left as ``%02u:%02u``.
        """

        try:
            with s.flow_lock:
                character = s.gameplay_character
            if character is None or not 1 <= int(floor) <= 100:
                return
            remaining = 0 if deadline is None else max(
                0, math.ceil(float(deadline) - time.perf_counter()))
            now = datetime.now(timezone.utc)
            store = self._ranking_store()
            for period in self._ranking_record_periods(FP_SEASON, now):
                store.improve_score(
                    BOARD_FRACTURED_PLANES, period, int(character.character_id),
                    score=int(floor), tiebreak=remaining, stats=(int(floor), remaining),
                    now=now,
                )
        except Exception as exc:  # Never disturb the floor completion.
            _log(f"Fractured Planes floor not recorded: {type(exc).__name__}: {exc}")

    def _ranking_on_pvp_match_settled(
            self, s, *, match_mode: int, honor: int, outcome: str,
            kills: int, deaths: int) -> None:
        """Accumulate one settled match on the solo or team board.

        Called once per participant from the battleground settlement claim;
        modes split as the battleground's own solo/team match counters do.
        """

        try:
            with s.flow_lock:
                character = s.gameplay_character
            if character is None:
                return
            now = datetime.now(timezone.utc)
            store = self._ranking_store()
            board = BOARD_PVP_SOLO if int(match_mode) in SOLO_MATCH_MODES else BOARD_PVP_TEAM
            deltas = (max(0, int(honor)), int(outcome == "won"), int(outcome == "lost"),
                      int(outcome == "draw"), max(0, int(kills)), max(0, int(deaths)))
            for period in self._ranking_record_periods(PVP_SEASON, now):
                store.add_to_score(board, period, int(character.character_id), deltas, now=now)
        except Exception as exc:  # Never disturb match settlement.
            _log(f"PvP result not recorded: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Periods, history and settlement
    # ------------------------------------------------------------------

    def _ranking_lock(self):
        lock = self.__dict__.get("_ranking_tick_lock")
        if lock is None:
            lock = self.__dict__.setdefault("_ranking_tick_lock", threading.RLock())
        return lock

    def _ranking_ensure_scheduler(self) -> None:
        """Start the settlement tick once the service is actually serving.

        Composed test servers never bind a socket, so they never start it.
        """

        if (getattr(self, "ranking_scheduler_disabled", False)
                or getattr(self, "sock", None) is None):
            return
        stop = getattr(self, "_stop_event", None) or threading.Event()
        with _STORES_LOCK:
            thread = self.__dict__.get("_ranking_scheduler")
            if thread is not None and thread.is_alive():
                return

            def run() -> None:
                while not stop.wait(RANKING_SCHEDULER_SECONDS):
                    try:
                        self._ranking_tick()
                    except Exception as exc:
                        _log(f"scheduled tick failed: {type(exc).__name__}: {exc}")

            thread = threading.Thread(target=run, name="leaderboard-settlement", daemon=True)
            self.__dict__["_ranking_scheduler"] = thread
            thread.start()

    def _ranking_tick(self, now: datetime | None = None) -> None:
        """Open current periods, settle ended ones, retry pending deliveries."""

        now = _utc(now)
        store = self._ranking_store()
        with self._ranking_lock():
            for family, period in ((HONOR_DAILY, day_period(now)),
                                   (HONOR_WEEKLY, week_period(now))):
                self._ranking_open_period(family, period, now)
            catalog = self._ranking_event_catalog()
            seasons = {
                family: self._ranking_season_events(family, now, catalog)
                for family in (PVP_SEASON, PVE_SEASON, FP_SEASON)
            }
            for family, events in seasons.items():
                for event, state in events:
                    if state == "active":
                        self._ranking_open_period(family, event_period(event), now)
            for record in store.periods(state="open"):
                if self._ranking_period_due(record, now, seasons):
                    self._ranking_settle(record.family, record.period, now)
        for session in tuple(self._active_gameplay_sessions()):
            try:
                self._ranking_deliver_pending(session)
            except Exception as exc:
                _log(f"pending delivery failed: {type(exc).__name__}: {exc}")

    @staticmethod
    def _ranking_period_due(record, now: datetime, seasons) -> bool:
        if record.family in (HONOR_DAILY, HONOR_WEEKLY):
            ends_at = period_bound(record.ends_at)
            return ends_at is not None and ends_at <= now
        if record.family not in seasons or not record.period.startswith("event:"):
            return False
        event_id = int(record.period.split(":", 1)[1])
        for event, state in seasons[record.family]:
            if int(event.event_id) == event_id:
                return state == "expired"
        # A schedule entry removed by an operator leaves its period open:
        # deletion is not a season end, and nothing is granted for it.
        return False

    @staticmethod
    def _snapshot(board: str, ranked, *, score=lambda record: record.values[0],
                  stats=lambda record: record.values) -> tuple[SnapshotRow, ...]:
        return tuple(
            SnapshotRow(board, place, record.character_id, record.name,
                        record.class_id, int(score(record)), tuple(stats(record)))
            for place, record in enumerate(ranked, 1)
        )

    def _ranking_settle(self, family: str, period: str, now: datetime) -> bool:
        """Freeze one ended period and create its entitlements, once."""

        store = self._ranking_store()
        catalog = self._leaderboard_catalog()
        honor_stats = lambda record: (
            record.honor_points, record.wins, record.losses,
            getattr(record, "draws", 0), record.kills, record.deaths)
        honor_score = lambda record: record.honor_points
        history: dict[str, tuple[SnapshotRow, ...]] = {}
        entitlements: list[Entitlement] = []

        def entitle(board, ranked, rewards, *, achievement=False):
            for place, record in enumerate(ranked, 1):
                reward = reward_for_place(rewards, place)
                items = reward.items if reward is not None else ()
                if not items and not achievement:
                    continue
                entitlements.append(Entitlement(
                    board, period, record.character_id, place, items,
                    STATUS_PENDING if items else STATUS_NONE,
                    place if achievement else None,
                    STATUS_PENDING if achievement else STATUS_NONE,
                ))

        if family in (HONOR_DAILY, HONOR_WEEKLY):
            ranked = sorted(self._honor_period_records(None, period), key=_sort_key("honor_points"))
            history[family] = self._snapshot(family, ranked, score=honor_score, stats=honor_stats)
        elif family == PVP_SEASON:
            overall = sorted((record for record in self._honor_period_records(None, period)
                              if record.honor_points > 0), key=_sort_key("honor_points"))
            history[BOARD_PVP_OVERALL] = self._snapshot(
                BOARD_PVP_OVERALL, overall, score=honor_score, stats=honor_stats)
            for board in (BOARD_PVP_SOLO, BOARD_PVP_TEAM):
                ranked = sorted(self._pvp_mode_records(None, board, period),
                                key=_sort_key("honor_points"))
                history[board] = self._snapshot(board, ranked, score=honor_score, stats=honor_stats)
            entitle(BOARD_PVP_OVERALL, overall, catalog.overall_pvp_rewards)
        elif family == PVE_SEASON:
            for board_type in PVE_BOARD_TYPES:
                for board in range(PARALLEL_WORLD_BOARD_COUNT):
                    key = parallel_world_board(board, board_type)
                    ranked = sorted(self._parallel_world_records(None, board, board_type, period),
                                    key=_sort_key(0))
                    history[key] = self._snapshot(
                        key, ranked, stats=lambda record: (*record.values, *record.extra))
                key = overall_pve_board(board_type)
                ranked = sorted(self._overall_pve_records(None, board_type, period),
                                key=_sort_key(0))
                history[key] = self._snapshot(key, ranked)
                entitle(key, ranked, catalog.overall_rewards.get(board_type, ()))
        elif family == FP_SEASON:
            # "Players with a Highest Passed Floor of 0 cannot receive Group
            # Ranking rewards" (gui.window.leaderboards warnning): only
            # characters with a cleared floor are ranked.
            ranked = sorted(self._fractured_planes_records(None, period), key=_sort_key(0))
            history[BOARD_FRACTURED_PLANES_GROUP] = self._snapshot(
                BOARD_FRACTURED_PLANES_GROUP, ranked,
                stats=lambda record: (record.values[0], record.order[0]))
            entitle(BOARD_FRACTURED_PLANES_GROUP, ranked, catalog.high_score_rewards,
                    achievement=True)
        else:
            return False
        settled = store.settle(family, period, history, entitlements, now=now)
        if settled:
            _log(f"settled {family} {period}: "
                 f"{sum(len(rows) for rows in history.values())} history rows, "
                 f"{len(entitlements)} entitlements")
        return settled

    # ------------------------------------------------------------------
    # Delivery (online now, offline at next login)
    # ------------------------------------------------------------------

    def _ranking_deliver_pending(self, s) -> None:
        with s.flow_lock:
            character = s.gameplay_character
            # A session handing off to another map must not write a snapshot
            # over the destination's; the arrival delivers instead.
            ready = (s.gameplay_inventory is not None
                     and not getattr(s, "gameplay_exit_transition_started", False))
        if character is None or not ready:
            return
        pending = self._ranking_store().entitlements(
            int(character.character_id), pending_only=True)
        if not pending:
            return
        # One delivery at a time per character: the claim ledger read and
        # the grant must not interleave with another delivery thread.
        with s.gameplay_mutation_lock:
            for entitlement in pending:
                self._ranking_deliver_one(s, entitlement)

    def _ranking_persist(self, s, reason: str) -> None:
        if getattr(self, "account_store", None) is not None:
            if self._persist_gameplay_session(s, reason=reason, flush=True):
                return
        CharacterService._request_account_save(s, reason, flush=True)

    def _ranking_deliver_one(self, s, entitlement: Entitlement) -> None:
        store = self._ranking_store()
        if entitlement.reward_status == STATUS_PENDING:
            with s.flow_lock:
                claimed = bool(dict(s.gameplay_timed_offer_purchases).get(entitlement.claim_key))
            if claimed:
                # Granted earlier (receipt not yet marked): make sure that
                # claim is durable before the receipt says so.
                self._ranking_persist(s, "leaderboard reward")
                store.mark_delivered(entitlement, reward=True)
            else:
                definitions = tuple(
                    RewardDefinition(entitlement.claim_key, item, amount)
                    for item, amount in entitlement.items
                )
                try:
                    CharacterService._deliver_rewards(
                        self, s, definitions, claim_keys=(entitlement.claim_key,),
                        label="leaderboard-reward",
                    )
                except (OSError, ValueError) as exc:
                    # A full bag keeps the entitlement for the next login or
                    # tick; the reason is kept on the receipt.
                    store.note_error(entitlement, str(exc))
                    _log(f"reward {entitlement.claim_key} for "
                         f"{entitlement.character_id} pending: {exc}")
                else:
                    self._ranking_persist(s, "leaderboard reward")
                    store.mark_delivered(entitlement, reward=True)
                    _log(f"granted {entitlement.claim_key} (place {entitlement.rank}) "
                         f"to {entitlement.character_id}")
        if entitlement.achievement_status == STATUS_PENDING and entitlement.achievement_rank:
            self._ranking_feed_achievement(s, entitlement)

    def _ranking_feed_achievement(self, s, entitlement: Entitlement) -> None:
        """Emit the final rank once, using a marker saved with the progress."""

        store = self._ranking_store()
        key = entitlement.achievement_key
        with s.flow_lock:
            claims = dict(s.gameplay_timed_offer_purchases)
            if claims.get(key):
                done = True
            else:
                done = False
                claims[key] = int(entitlement.achievement_rank)
                s.gameplay_timed_offer_purchases = claims
        if done:
            self._ranking_persist(s, "leaderboard achievement")
            store.mark_delivered(entitlement, achievement=True)
            return
        try:
            self._achievement_feed(
                s, HIGH_SCORE_ACHIEVEMENT_KIND, (str(int(entitlement.achievement_rank)),))
            self._ranking_persist(s, "leaderboard achievement")
        except Exception as exc:
            account = getattr(self, "account_store", None)
            saved = account.get(int(entitlement.character_id)) if account is not None else None
            if saved is None or not dict(saved.timed_offer_purchases).get(key):
                with s.flow_lock:
                    claims = dict(s.gameplay_timed_offer_purchases)
                    claims.pop(key, None)
                    s.gameplay_timed_offer_purchases = claims
                store.note_error(entitlement, f"achievement: {exc}")
                _log(f"achievement for {key} pending: {type(exc).__name__}: {exc}")
                return
        store.mark_delivered(entitlement, achievement=True)


__all__ = [
    "BOARD_FRACTURED_PLANES", "BOARD_FRACTURED_PLANES_GROUP", "BOARD_PVP_OVERALL",
    "BOARD_PVP_SOLO", "BOARD_PVP_TEAM", "FP_SEASON", "HONOR_DAILY", "HONOR_WEEKLY",
    "MAX_HIGH_SCORE_ENTRIES", "PVE_SEASON", "PVP_SEASON", "ParallelWorldRun",
    "RankingService", "overall_pve_board", "parallel_world_board", "ranking_store_for",
]
