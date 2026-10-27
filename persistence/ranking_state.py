"""Durable leaderboard scores, period history and settlement receipts.

Leaderboards outlive a session and the player document: scores are recorded
from server transitions (a Parallel World boss kill, a cleared Fractured
Planes floor, a settled PvP match), honor periods keep the career counters
each character started a day/week/season with, and a settled period keeps
its final standings and one entitlement per rewarded character.

A settlement writes its history rows, entitlements and receipt in a single
SQLite transaction, so a restart either sees the whole settlement or none of
it and recomputes the same frozen standings. Entitlements are delivered
later through the character's own claim ledger (``timed_offer_purchases``),
which commits atomically with the inventory; this store only records that
the delivery reached the player document.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Iterable, Mapping


RANKING_DATABASE_SCHEMA_VERSION = 1

STATUS_NONE = "none"
STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"


def _stamp(moment: datetime | None = None) -> str:
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _encode(values: Iterable[int]) -> str:
    return json.dumps([int(value) for value in values], separators=(",", ":"))


def _decode(text: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(value) for value in json.loads(text or "[]"))
    except (TypeError, ValueError):
        return ()


@dataclass(frozen=True)
class ScoreRecord:
    character_id: int
    score: int
    tiebreak: int
    stats: tuple[int, ...]
    achieved_at: str


@dataclass(frozen=True)
class PeriodRecord:
    family: str
    period: str
    starts_at: str
    ends_at: str
    state: str
    opened_at: str
    closed_at: str


@dataclass(frozen=True)
class SnapshotRow:
    board: str
    rank: int
    character_id: int
    name: str
    class_id: int
    score: int
    stats: tuple[int, ...] = ()


@dataclass(frozen=True)
class Entitlement:
    board: str
    period: str
    character_id: int
    rank: int
    items: tuple[tuple[str, int], ...]
    reward_status: str
    achievement_rank: int | None
    achievement_status: str
    created_at: str = ""
    reward_delivered_at: str = ""
    achievement_delivered_at: str = ""
    last_error: str = ""

    @property
    def claim_key(self) -> str:
        """Per-character claim recorded with the granted inventory."""

        return f"ranking-reward:{self.board}:{self.period}"

    @property
    def achievement_key(self) -> str:
        return f"ranking-achievement:{self.board}:{self.period}"

    @property
    def pending(self) -> bool:
        return STATUS_PENDING in (self.reward_status, self.achievement_status)


class RankingStateStore:
    """Small synchronous WAL store; every write is a short transaction."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.lock = threading.RLock()
        # A file store opens one short-lived connection per operation, like
        # AsyncPlayerDatabase, so no handle outlives the call. ":memory:"
        # (tests, stores without a player database) keeps its only one.
        self._memory = (
            sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
            if self.path == ":memory:" else None
        )
        if self._memory is None:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._create_schema()

    @contextmanager
    def _connection(self):
        with self.lock:
            if self._memory is not None:
                yield self._memory
                return
            connection = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
            try:
                connection.execute("PRAGMA busy_timeout=10000")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                yield connection
            finally:
                connection.close()

    def _create_schema(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                BEGIN;
                CREATE TABLE IF NOT EXISTS ranking_metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ranking_scores (
                    board TEXT NOT NULL, period TEXT NOT NULL,
                    character_id INTEGER NOT NULL,
                    score INTEGER NOT NULL, tiebreak INTEGER NOT NULL,
                    stats_json TEXT NOT NULL, achieved_at TEXT NOT NULL,
                    PRIMARY KEY (board, period, character_id));
                CREATE TABLE IF NOT EXISTS ranking_baselines (
                    period TEXT NOT NULL, character_id INTEGER NOT NULL,
                    values_json TEXT NOT NULL,
                    PRIMARY KEY (period, character_id));
                CREATE TABLE IF NOT EXISTS ranking_periods (
                    family TEXT NOT NULL, period TEXT NOT NULL,
                    starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
                    state TEXT NOT NULL, opened_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (family, period));
                CREATE TABLE IF NOT EXISTS ranking_history (
                    board TEXT NOT NULL, period TEXT NOT NULL,
                    rank INTEGER NOT NULL, character_id INTEGER NOT NULL,
                    name TEXT NOT NULL, class_id INTEGER NOT NULL,
                    score INTEGER NOT NULL, stats_json TEXT NOT NULL,
                    PRIMARY KEY (board, period, character_id));
                CREATE TABLE IF NOT EXISTS ranking_settlements (
                    family TEXT NOT NULL, period TEXT NOT NULL,
                    settled_at TEXT NOT NULL, entitlements INTEGER NOT NULL,
                    PRIMARY KEY (family, period));
                CREATE TABLE IF NOT EXISTS ranking_entitlements (
                    board TEXT NOT NULL, period TEXT NOT NULL,
                    character_id INTEGER NOT NULL, rank INTEGER NOT NULL,
                    items_json TEXT NOT NULL,
                    reward_status TEXT NOT NULL,
                    achievement_rank INTEGER,
                    achievement_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reward_delivered_at TEXT NOT NULL DEFAULT '',
                    achievement_delivered_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (board, period, character_id));
                CREATE INDEX IF NOT EXISTS ranking_entitlements_character
                    ON ranking_entitlements(character_id);
                INSERT INTO ranking_metadata(key, value)
                VALUES ('schema_version', '1') ON CONFLICT(key) DO NOTHING;
                COMMIT;
                """
            )
            row = connection.execute(
                "SELECT value FROM ranking_metadata WHERE key='schema_version'"
            ).fetchone()
        if row is None or int(row[0]) != RANKING_DATABASE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported ranking database schema version {row and row[0]}"
            )

    def close(self) -> None:
        with self.lock:
            if self._memory is not None:
                self._memory.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    def _read(self, query: str, arguments=()) -> list:
        with self._connection() as connection:
            return connection.execute(query, arguments).fetchall()

    # ------------------------------------------------------------------
    # Scores
    # ------------------------------------------------------------------

    def improve_score(
        self, board: str, period: str, character_id: int, *,
        score: int, tiebreak: int = 0, stats: Iterable[int] = (),
        now: datetime | None = None,
    ) -> bool:
        """Keep the better ``(score, tiebreak)`` result; True when replaced."""

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT score, tiebreak FROM ranking_scores "
                "WHERE board=? AND period=? AND character_id=?",
                (board, period, int(character_id)),
            ).fetchone()
            if row is not None and (int(row[0]), int(row[1])) >= (int(score), int(tiebreak)):
                return False
            connection.execute(
                "INSERT INTO ranking_scores(board, period, character_id, score, "
                "tiebreak, stats_json, achieved_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(board, period, character_id) DO UPDATE SET "
                "score=excluded.score, tiebreak=excluded.tiebreak, "
                "stats_json=excluded.stats_json, achieved_at=excluded.achieved_at",
                (board, period, int(character_id), int(score), int(tiebreak),
                 _encode(stats), _stamp(now)),
            )
            return True

    def add_to_score(
        self, board: str, period: str, character_id: int,
        deltas: Iterable[int], *, now: datetime | None = None,
    ) -> ScoreRecord:
        """Accumulate counters; ``stats[0]`` is the ranking score."""

        deltas = tuple(int(value) for value in deltas)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT stats_json FROM ranking_scores "
                "WHERE board=? AND period=? AND character_id=?",
                (board, period, int(character_id)),
            ).fetchone()
            current = list(_decode(row[0])) if row is not None else []
            current.extend([0] * max(0, len(deltas) - len(current)))
            values = tuple(
                max(0, current[index] + (deltas[index] if index < len(deltas) else 0))
                for index in range(len(current))
            )
            stamp = _stamp(now)
            connection.execute(
                "INSERT INTO ranking_scores(board, period, character_id, score, "
                "tiebreak, stats_json, achieved_at) VALUES (?, ?, ?, ?, 0, ?, ?) "
                "ON CONFLICT(board, period, character_id) DO UPDATE SET "
                "score=excluded.score, stats_json=excluded.stats_json, "
                "achieved_at=excluded.achieved_at",
                (board, period, int(character_id), values[0] if values else 0,
                 _encode(values), stamp),
            )
        return ScoreRecord(int(character_id), values[0] if values else 0, 0, values, stamp)

    def scores(self, board: str, period: str) -> dict[int, ScoreRecord]:
        rows = self._read(
            "SELECT character_id, score, tiebreak, stats_json, achieved_at "
            "FROM ranking_scores WHERE board=? AND period=?",
            (board, period),
        )
        return {
            int(row[0]): ScoreRecord(int(row[0]), int(row[1]), int(row[2]),
                                     _decode(row[3]), str(row[4]))
            for row in rows
        }

    # ------------------------------------------------------------------
    # Honor baselines and periods
    # ------------------------------------------------------------------

    def put_baselines(
        self, period: str, values: Mapping[int, Iterable[int]],
    ) -> int:
        """Record each character's start counters once per period."""

        with self._transaction() as connection:
            before = connection.total_changes
            connection.executemany(
                "INSERT INTO ranking_baselines(period, character_id, values_json) "
                "VALUES (?, ?, ?) ON CONFLICT(period, character_id) DO NOTHING",
                ((period, int(identity), _encode(counters))
                 for identity, counters in values.items()),
            )
            return connection.total_changes - before

    def baselines(self, period: str) -> dict[int, tuple[int, ...]]:
        rows = self._read(
            "SELECT character_id, values_json FROM ranking_baselines "
            "WHERE period=?", (period,),
        )
        return {int(row[0]): _decode(row[1]) for row in rows}

    def open_period(
        self, family: str, period: str, *, starts_at: str = "",
        ends_at: str = "", now: datetime | None = None,
    ) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO ranking_periods(family, period, starts_at, ends_at, "
                "state, opened_at) VALUES (?, ?, ?, ?, 'open', ?) "
                "ON CONFLICT(family, period) DO NOTHING",
                (family, period, starts_at or "", ends_at or "", _stamp(now)),
            )
            return cursor.rowcount == 1

    def period(self, family: str, period: str) -> PeriodRecord | None:
        rows = self._read(
            "SELECT family, period, starts_at, ends_at, state, opened_at, "
            "closed_at FROM ranking_periods WHERE family=? AND period=?",
            (family, period),
        )
        return PeriodRecord(*rows[0]) if rows else None

    def periods(self, *, state: str | None = None) -> tuple[PeriodRecord, ...]:
        query = (
            "SELECT family, period, starts_at, ends_at, state, opened_at, closed_at "
            "FROM ranking_periods"
        )
        arguments: tuple = ()
        if state is not None:
            query += " WHERE state=?"
            arguments = (state,)
        rows = self._read(query + " ORDER BY opened_at, family, period", arguments)
        return tuple(PeriodRecord(*row) for row in rows)

    # ------------------------------------------------------------------
    # History and settlement
    # ------------------------------------------------------------------

    def settle(
        self, family: str, period: str,
        history: Mapping[str, Iterable[SnapshotRow]],
        entitlements: Iterable[Entitlement] = (), *,
        now: datetime | None = None,
    ) -> bool:
        """Freeze a period exactly once: history, entitlements and receipt.

        Returns False (and writes nothing) when the period already carries a
        settlement receipt, so a replayed or concurrent settlement is inert.
        """

        stamp = _stamp(now)
        entitlements = tuple(entitlements)
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM ranking_settlements WHERE family=? AND period=?",
                (family, period),
            ).fetchone() is not None:
                return False
            for board, rows in history.items():
                connection.executemany(
                    "INSERT INTO ranking_history(board, period, rank, character_id, "
                    "name, class_id, score, stats_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(board, period, character_id) DO NOTHING",
                    ((board, period, int(row.rank), int(row.character_id),
                      str(row.name), int(row.class_id), int(row.score),
                      _encode(row.stats)) for row in rows),
                )
            connection.executemany(
                "INSERT INTO ranking_entitlements(board, period, character_id, rank, "
                "items_json, reward_status, achievement_rank, achievement_status, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(board, period, character_id) DO NOTHING",
                ((value.board, value.period, int(value.character_id), int(value.rank),
                  json.dumps([[item, int(amount)] for item, amount in value.items]),
                  value.reward_status, value.achievement_rank,
                  value.achievement_status, stamp) for value in entitlements),
            )
            connection.execute(
                "INSERT INTO ranking_settlements(family, period, settled_at, "
                "entitlements) VALUES (?, ?, ?, ?)",
                (family, period, stamp, len(entitlements)),
            )
            connection.execute(
                "INSERT INTO ranking_periods(family, period, starts_at, ends_at, "
                "state, opened_at, closed_at) VALUES (?, ?, '', '', 'settled', ?, ?) "
                "ON CONFLICT(family, period) DO UPDATE SET state='settled', "
                "closed_at=excluded.closed_at",
                (family, period, stamp, stamp),
            )
            return True

    def settlement(self, family: str, period: str) -> tuple[str, int] | None:
        rows = self._read(
            "SELECT settled_at, entitlements FROM ranking_settlements "
            "WHERE family=? AND period=?", (family, period),
        )
        return (str(rows[0][0]), int(rows[0][1])) if rows else None

    def history(self, board: str, period: str) -> tuple[SnapshotRow, ...]:
        rows = self._read(
            "SELECT board, rank, character_id, name, class_id, score, stats_json "
            "FROM ranking_history WHERE board=? AND period=? ORDER BY rank",
            (board, period),
        )
        return tuple(
            SnapshotRow(row[0], int(row[1]), int(row[2]), str(row[3]), int(row[4]),
                        int(row[5]), _decode(row[6]))
            for row in rows
        )

    def history_periods(self, board: str) -> tuple[str, ...]:
        rows = self._read(
            "SELECT DISTINCT period FROM ranking_history WHERE board=? "
            "ORDER BY period", (board,),
        )
        return tuple(str(row[0]) for row in rows)

    # ------------------------------------------------------------------
    # Entitlements
    # ------------------------------------------------------------------

    @staticmethod
    def _entitlement(row) -> Entitlement:
        try:
            items = tuple((str(item), int(amount)) for item, amount in json.loads(row[4]))
        except (TypeError, ValueError):
            items = ()
        return Entitlement(
            board=str(row[0]), period=str(row[1]), character_id=int(row[2]),
            rank=int(row[3]), items=items, reward_status=str(row[5]),
            achievement_rank=None if row[6] is None else int(row[6]),
            achievement_status=str(row[7]), created_at=str(row[8]),
            reward_delivered_at=str(row[9]), achievement_delivered_at=str(row[10]),
            last_error=str(row[11]),
        )

    def entitlements(
        self, character_id: int | None = None, *, pending_only: bool = False,
    ) -> tuple[Entitlement, ...]:
        query = (
            "SELECT board, period, character_id, rank, items_json, reward_status, "
            "achievement_rank, achievement_status, created_at, reward_delivered_at, "
            "achievement_delivered_at, last_error FROM ranking_entitlements"
        )
        clauses, arguments = [], []
        if character_id is not None:
            clauses.append("character_id=?")
            arguments.append(int(character_id))
        if pending_only:
            clauses.append("(reward_status='pending' OR achievement_status='pending')")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        rows = self._read(query + " ORDER BY created_at, board, period", arguments)
        return tuple(self._entitlement(row) for row in rows)

    def mark_delivered(
        self, entitlement: Entitlement, *, reward: bool = False,
        achievement: bool = False, now: datetime | None = None,
    ) -> None:
        stamp = _stamp(now)
        with self._transaction() as connection:
            if reward:
                connection.execute(
                    "UPDATE ranking_entitlements SET reward_status='delivered', "
                    "reward_delivered_at=?, last_error='' WHERE board=? AND period=? "
                    "AND character_id=? AND reward_status='pending'",
                    (stamp, entitlement.board, entitlement.period,
                     int(entitlement.character_id)),
                )
            if achievement:
                connection.execute(
                    "UPDATE ranking_entitlements SET achievement_status='delivered', "
                    "achievement_delivered_at=? WHERE board=? AND period=? "
                    "AND character_id=? AND achievement_status='pending'",
                    (stamp, entitlement.board, entitlement.period,
                     int(entitlement.character_id)),
                )

    def note_error(self, entitlement: Entitlement, error: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE ranking_entitlements SET last_error=? WHERE board=? "
                "AND period=? AND character_id=?",
                (str(error)[:500], entitlement.board, entitlement.period,
                 int(entitlement.character_id)),
            )


__all__ = [
    "Entitlement", "PeriodRecord", "RANKING_DATABASE_SCHEMA_VERSION",
    "RankingStateStore", "STATUS_DELIVERED", "STATUS_NONE", "STATUS_PENDING",
    "ScoreRecord", "SnapshotRow",
]
