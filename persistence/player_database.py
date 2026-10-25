"""Asynchronous SQLite persistence for player profiles and gameplay state.

The network services keep authoritative objects in memory.  This module owns
the durable boundary: callers publish detached snapshots, a single writer
thread coalesces newer snapshots by character id, and SQLite commits happen
without blocking a packet handler.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any


PLAYER_DATABASE_SCHEMA_VERSION = 1


class AsyncPlayerDatabase:
    """WAL-backed, single-writer store for character documents."""

    def __init__(
        self,
        path: str | Path,
        *,
        encode_state: Callable[[Any], dict],
        encode_profile: Callable[[Any], dict],
        coalesce_seconds: float = 0.01,
    ) -> None:
        self.path = Path(path)
        self._encode_state = encode_state
        self._encode_profile = encode_profile
        self._coalesce_seconds = max(0.0, float(coalesce_seconds))
        self._condition = threading.Condition()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_wallets: dict[int, int] = {}
        self._pending_states: dict[int, Any] = {}
        self._pending_profiles: dict[int, Any] = {}
        self._pending_deletes: set[int] = set()
        self._queued_revision = 0
        self._written_revision = 0
        self._last_error: Exception | None = None
        self.warnings: list[str] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            self._create_schema(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,
        )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            BEGIN;
            CREATE TABLE IF NOT EXISTS player_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS account_wallets (
                account_id INTEGER PRIMARY KEY,
                andermant INTEGER NOT NULL CHECK(andermant >= 0)
            );
            CREATE TABLE IF NOT EXISTS character_profiles (
                character_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE,
                map_id TEXT NOT NULL,
                class_id INTEGER NOT NULL,
                level INTEGER NOT NULL,
                premium_currency INTEGER NOT NULL,
                admin_privileges INTEGER NOT NULL,
                admin_name_color INTEGER NOT NULL,
                profile_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS character_profiles_name
                ON character_profiles(name COLLATE NOCASE);
            CREATE TABLE IF NOT EXISTS character_states (
                character_id INTEGER PRIMARY KEY,
                map_id TEXT NOT NULL,
                saved_at TEXT NOT NULL,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO player_metadata(key, value)
            VALUES ('schema_version', '1')
            ON CONFLICT(key) DO NOTHING;
            COMMIT;
            """
        )
        row = connection.execute(
            "SELECT value FROM player_metadata WHERE key='schema_version'"
        ).fetchone()
        version = int(row[0]) if row is not None else 0
        if version != PLAYER_DATABASE_SCHEMA_VERSION:
            raise ValueError(
                "unsupported player database schema version "
                f"{version}; expected {PLAYER_DATABASE_SCHEMA_VERSION}"
            )

    def load_documents(self) -> tuple[dict[int, dict], dict[int, dict]]:
        """Load state and profile documents before gameplay starts."""

        states: dict[int, dict] = {}
        profiles: dict[int, dict] = {}
        with closing(self._connect()) as connection:
            for character_id, payload in connection.execute(
                "SELECT character_id, state_json FROM character_states "
                "ORDER BY character_id"
            ):
                try:
                    states[int(character_id)] = json.loads(payload)
                except (TypeError, ValueError) as exc:
                    self.warnings.append(
                        f"ignored SQLite state {character_id}: {exc}"
                    )
            for character_id, payload in connection.execute(
                "SELECT character_id, profile_json FROM character_profiles "
                "ORDER BY character_id"
            ):
                try:
                    profiles[int(character_id)] = json.loads(payload)
                except (TypeError, ValueError) as exc:
                    self.warnings.append(
                        f"ignored SQLite profile {character_id}: {exc}"
                    )
        return states, profiles

    @staticmethod
    def _encoded_profile(profile: Any, encode: Callable[[Any], dict]):
        document = encode(profile)
        return (
            int(document["character_id"]),
            str(document["name"]),
            str(document["map_id"]),
            int(document["class_id"]),
            int(document.get("level", 1)),
            int(document.get("premium_currency", 0)),
            int(bool(document.get("admin_privileges", False))),
            int(bool(document.get("admin_name_color", False))),
            json.dumps(document, separators=(",", ":"), sort_keys=True),
        )

    @staticmethod
    def _encoded_state(state: Any, encode: Callable[[Any], dict]):
        document = encode(state)
        return (
            int(document["character_id"]),
            str(document["map_id"]),
            str(document.get("saved_at", "")),
            json.dumps(document, separators=(",", ":"), sort_keys=True),
        )


    def load_wallets(self) -> dict[int, int]:
        with closing(self._connect()) as connection:
            return dict(connection.execute("SELECT account_id, andermant FROM account_wallets"))

    def queue_wallet(self, account_id: int, amount: int) -> None:
        with self._condition:
            self._pending_wallets[int(account_id)] = int(amount)
            self._queued_revision += 1
            self._ensure_writer_locked()
            self._wake_event.set()

    def queue_state(
        self, character_id: int, state: Any, *, wallet=None, profile=None
    ) -> None:
        """Queue one character snapshot and its matching profile atomically."""
        with self._condition:
            character_id = int(character_id)
            self._pending_states[character_id] = state
            if profile is not None:
                self._pending_profiles[character_id] = profile
            if wallet is not None:
                self._pending_wallets[int(wallet[0])] = int(wallet[1])
            self._pending_deletes.discard(character_id)
            self._queued_revision += 1
            self._ensure_writer_locked()
            self._wake_event.set()

    def queue_profile(self, character_id: int, profile: Any) -> None:
        with self._condition:
            character_id = int(character_id)
            self._pending_profiles[character_id] = profile
            self._pending_deletes.discard(character_id)
            self._queued_revision += 1
            self._ensure_writer_locked()
            self._wake_event.set()

    def queue_delete(self, character_id: int) -> None:
        with self._condition:
            character_id = int(character_id)
            self._pending_states.pop(character_id, None)
            self._pending_profiles.pop(character_id, None)
            self._pending_deletes.add(character_id)
            self._queued_revision += 1
            self._ensure_writer_locked()
            self._wake_event.set()

    def request_flush(self) -> bool:
        """Wake the writer without waiting for disk I/O."""

        with self._condition:
            pending = bool(
                self._pending_states
                or self._pending_profiles
                or self._pending_deletes
                or self._written_revision < self._queued_revision
            )
            if pending:
                self._ensure_writer_locked()
                self._wake_event.set()
            return pending

    def flush(self, timeout: float = 15.0) -> bool:
        """Wait for queued commits at a durable mutation or lifecycle boundary."""

        with self._condition:
            target = self._queued_revision
            dirty = self._written_revision < target
            if not dirty:
                return False
            self._ensure_writer_locked()
            self._wake_event.set()
            deadline = time.perf_counter() + max(0.1, float(timeout))
            while self._written_revision < target:
                remaining = deadline - time.perf_counter()
                if remaining <= 0.0:
                    detail = (
                        f": {self._last_error}"
                        if self._last_error is not None else ""
                    )
                    raise TimeoutError(
                        f"timed out flushing player database{detail}"
                    )
                self._condition.wait(remaining)
            return True

    def close(self, timeout: float = 15.0) -> None:
        """Drain and stop the writer, or fail without losing its handle.

        Python cannot safely terminate a thread that is blocked inside an
        encoder or SQLite. A timed-out writer therefore remains registered
        so callers and diagnostics can still observe it; a later close can
        join it after the blocking operation returns.
        """

        timeout = max(0.1, float(timeout))
        flush_error: BaseException | None = None
        try:
            self.flush(timeout=timeout)
        except BaseException as exc:
            flush_error = exc

        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is threading.current_thread():
            message = (
                "player database close was called from its writer thread"
            )
            self.warnings.append(message)
            if flush_error is not None:
                raise flush_error
            raise RuntimeError(message)
        if thread is not None:
            thread.join(timeout=timeout)
        if thread is not None and thread.is_alive():
            message = (
                "timed out stopping player database writer "
                f"{thread.name!r}; writer handle was preserved"
            )
            self.warnings.append(message)
            if flush_error is not None:
                raise flush_error
            raise TimeoutError(message)

        with self._condition:
            if self._thread is thread:
                self._thread = None
        if flush_error is not None:
            raise flush_error

    def _ensure_writer_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._writer_loop,
            name="player-database-writer",
            daemon=True,
        )
        self._thread.start()

    @staticmethod
    def _write_rows(
        connection: sqlite3.Connection,
        states: list[tuple],
        profiles: list[tuple],
        deletes: tuple[int, ...],
    ) -> None:
        if deletes:
            connection.executemany(
                "DELETE FROM character_states WHERE character_id=?",
                ((value,) for value in deletes),
            )
            connection.executemany(
                "DELETE FROM character_profiles WHERE character_id=?",
                ((value,) for value in deletes),
            )
        if profiles:
            connection.executemany(
                """
                INSERT INTO character_profiles(
                    character_id, name, map_id, class_id, level,
                    premium_currency, admin_privileges, admin_name_color,
                    profile_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(character_id) DO UPDATE SET
                    name=excluded.name,
                    map_id=excluded.map_id,
                    class_id=excluded.class_id,
                    level=excluded.level,
                    premium_currency=excluded.premium_currency,
                    admin_privileges=excluded.admin_privileges,
                    admin_name_color=excluded.admin_name_color,
                    profile_json=excluded.profile_json,
                    updated_at=excluded.updated_at
                """,
                profiles,
            )
        if states:
            connection.executemany(
                """
                INSERT INTO character_states(
                    character_id, map_id, saved_at, state_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(character_id) DO UPDATE SET
                    map_id=excluded.map_id,
                    saved_at=excluded.saved_at,
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                """,
                states,
            )

    def _writer_loop(self) -> None:
        while True:
            self._wake_event.wait()
            if self._coalesce_seconds:
                self._stop_event.wait(self._coalesce_seconds)
            with self._condition:
                if (
                    self._stop_event.is_set()
                    and not self._pending_wallets
                    and not self._pending_states
                    and not self._pending_profiles
                    and not self._pending_deletes
                ):
                    return
                wallets = self._pending_wallets
                self._pending_wallets = {}
                states = self._pending_states
                profiles = self._pending_profiles
                deletes = tuple(self._pending_deletes)
                revision = self._queued_revision
                self._pending_states = {}
                self._pending_profiles = {}
                self._pending_deletes = set()
                self._wake_event.clear()
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            try:
                with closing(self._connect()) as connection:
                    encoded_states = [
                        (*self._encoded_state(value, self._encode_state), now)
                        for value in states.values()
                    ]
                    encoded_profiles = [
                        (*self._encoded_profile(value, self._encode_profile), now)
                        for value in profiles.values()
                    ]
                    connection.execute("BEGIN IMMEDIATE")
                    self._write_rows(
                        connection,
                        encoded_states,
                        encoded_profiles,
                        deletes,
                    )
                    connection.executemany(
                        "INSERT INTO account_wallets(account_id, andermant) VALUES (?, ?) "
                        "ON CONFLICT(account_id) DO UPDATE SET andermant=excluded.andermant",
                        wallets.items(),
                    )
                    connection.commit()
            except Exception as exc:
                try:
                    connection.rollback()
                except (NameError, sqlite3.Error):
                    pass
                with self._condition:
                    # A newer snapshot always wins over the failed one.
                    for key, value in wallets.items():
                        self._pending_wallets.setdefault(key, value)
                    for key, value in states.items():
                        self._pending_states.setdefault(key, value)
                    for key, value in profiles.items():
                        self._pending_profiles.setdefault(key, value)
                    self._pending_deletes.update(deletes)
                    self._last_error = exc
                    self._condition.notify_all()
                self.warnings.append(
                    f"asynchronous SQLite write failed: {exc}"
                )
                if self._stop_event.wait(0.1):
                    self._wake_event.set()
                else:
                    self._wake_event.set()
                continue
            with self._condition:
                self._written_revision = max(
                    self._written_revision, revision
                )
                self._last_error = None
                self._condition.notify_all()
                if (
                    self._pending_wallets
                    or self._pending_states
                    or self._pending_profiles
                    or self._pending_deletes
                ):
                    self._wake_event.set()


__all__ = ["AsyncPlayerDatabase", "PLAYER_DATABASE_SCHEMA_VERSION"]
