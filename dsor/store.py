"""Characters, saved.

Everything was in memory, so a restart lost the lot: level, experience, where you
stood, what was in your bag. This keeps the part that is genuinely a character's own,
under the account id the client already sends -- see :mod:`dsor.identity` -- and not
under anything session-shaped. An address changes, a RakNet GUID is new every run, and
a session id is new every login; saving a character under any of them saves it nowhere.

What is *not* here is deliberate. The world's creatures, the items on the ground and
the effects running are not a character's property, they are a moment in a world, and
writing them out would make a restart resume a fight rather than a character. And the
inventory is not here yet because this server does not model one: it replays a recorded
bag and rewrites cells in it, so there is nothing to save that would survive being
loaded.
"""

from __future__ import annotations

import logging

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

#: The schema this code expects. A stored file that says anything else is migrated
#: forward, or refused if it is from the future -- a newer server's file opened by an
#: older one would be quietly half-read otherwise.
log = logging.getLogger("store")

SCHEMA = 2

CREATE = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS characters (
    id         TEXT    PRIMARY KEY,
    account    INTEGER NOT NULL,
    character  INTEGER,
    level      INTEGER NOT NULL DEFAULT 1,
    experience INTEGER NOT NULL DEFAULT 0,
    health     REAL    NOT NULL DEFAULT 0,
    max_health REAL    NOT NULL DEFAULT 0,
    resource   REAL    NOT NULL DEFAULT 0,
    x          INTEGER,
    elevation  INTEGER,
    y          INTEGER,
    map_name   TEXT,
    seen_at    REAL    NOT NULL DEFAULT 0,
    saves      INTEGER NOT NULL DEFAULT 0,
    -- The action bar, as the client itself reported it: one skill id per slot,
    -- separated by newlines, an empty line for an empty slot. Schema 2.
    quickslots TEXT
);
CREATE INDEX IF NOT EXISTS characters_account ON characters (account);
"""


def _slots_from(text: str | None) -> tuple[str | None, ...] | None:
    """The action bar as stored: one id a line, an empty line for an empty slot."""
    if not text:
        return None
    return tuple(line or None for line in text.split("\n"))


def _slots_to(slots) -> str | None:
    """The other way. None when there is no bar to store."""
    if not slots:
        return None
    return "\n".join(name or "" for name in slots)


@dataclass(frozen=True)
class Saved:
    """A character as the file holds it."""

    id: str
    account: int
    character: int | None
    level: int
    experience: int
    health: float
    max_health: float
    resource: float
    position: tuple[int, int, int] | None
    map_name: str | None
    seen_at: float
    saves: int
    #: The action bar the client last reported, slot by slot. None for a character
    #: saved before schema 2, or one whose client has not reported one yet.
    quickslots: tuple[str | None, ...] | None = None


class Store:
    """The character file, and the two things anyone does with it."""

    def __init__(self, path: str | Path = "characters.sqlite") -> None:
        self.path = Path(path)
        # Write-ahead logging, because a save happens while the server is answering
        # datagrams and the default journal takes a lock across the whole file.
        # NORMAL rather than FULL: losing the last few seconds of a character on a
        # power cut is a fair trade for not waiting on the disk inside the tick.
        self.db = sqlite3.connect(str(self.path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = NORMAL")
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(CREATE)
        self._check_schema()

    def _check_schema(self) -> None:
        row = self.db.execute(
            "SELECT value FROM meta WHERE key = 'schema'"
        ).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO meta (key, value) VALUES ('schema', ?)", (str(SCHEMA),)
            )
            return
        found = int(row["value"])
        if found > SCHEMA:
            raise RuntimeError(
                f"{self.path} was written by a newer server (schema {found}, this one "
                f"understands {SCHEMA}). Half-reading it would lose characters."
            )
        if found < 2:
            # Schema 2 adds the action bar. CREATE only makes a table that is not
            # there, so an existing file needs the column added -- a migration is code
            # and not a hope.
            columns = {
                row[1] for row in self.db.execute("PRAGMA table_info(characters)")
            }
            if "quickslots" not in columns:
                self.db.execute("ALTER TABLE characters ADD COLUMN quickslots TEXT")
            log.info("%s: migrated to schema 2 (the action bar)", self.path)
        if found != SCHEMA:
            self.db.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema'", (str(SCHEMA),)
            )

    def close(self) -> None:
        self.db.close()

    def load(self, key: str) -> Saved | None:
        """The character stored under *key*, or None if there is none yet."""
        row = self.db.execute(
            "SELECT * FROM characters WHERE id = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        position = None
        if row["x"] is not None:
            position = (row["x"], row["elevation"], row["y"])
        return Saved(
            id=row["id"],
            account=row["account"],
            character=row["character"],
            level=row["level"],
            experience=row["experience"],
            health=row["health"],
            max_health=row["max_health"],
            resource=row["resource"],
            position=position,
            map_name=row["map_name"],
            seen_at=row["seen_at"],
            saves=row["saves"],
            quickslots=_slots_from(
                row["quickslots"] if "quickslots" in row.keys() else None
            ),
        )

    def save(
        self,
        key: str,
        account: int,
        character: int | None,
        level: int,
        experience: int,
        health: float,
        max_health: float,
        resource: float,
        position: tuple[int, int, int] | None = None,
        map_name: str | None = None,
        quickslots=None,
    ) -> None:
        """Write a character, replacing whatever was there.

        One statement, so a save is atomic without a transaction around it: a crash
        mid-save leaves the previous row rather than half of a new one.
        """
        x, elevation, y = position or (None, None, None)
        self.db.execute(
            """
            INSERT INTO characters
                (id, account, character, level, experience, health, max_health,
                 resource, x, elevation, y, map_name, seen_at, quickslots, saves)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT (id) DO UPDATE SET
                account = excluded.account,
                character = excluded.character,
                level = excluded.level,
                experience = excluded.experience,
                health = excluded.health,
                max_health = excluded.max_health,
                resource = excluded.resource,
                x = excluded.x,
                elevation = excluded.elevation,
                y = excluded.y,
                map_name = excluded.map_name,
                seen_at = excluded.seen_at,
                -- Only when there is one to write. A save from a moment before the
                -- client has reported its bar must not erase the bar already stored.
                quickslots = COALESCE(excluded.quickslots, characters.quickslots),
                saves = characters.saves + 1
            """,
            (
                key, account, character, level, experience, health, max_health,
                resource, x, elevation, y, map_name, time.time(),
                _slots_to(quickslots),
            ),
        )

    def characters_of(self, account: int) -> list[Saved]:
        """Every character saved under one account."""
        rows = self.db.execute(
            "SELECT id FROM characters WHERE account = ? ORDER BY id", (account,)
        ).fetchall()
        return [saved for row in rows if (saved := self.load(row["id"]))]

    @property
    def count(self) -> int:
        return self.db.execute("SELECT count(*) FROM characters").fetchone()[0]
