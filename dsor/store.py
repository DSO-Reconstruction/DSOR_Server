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

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

#: The schema this code expects. A stored file that says anything else is migrated
#: forward, or refused if it is from the future -- a newer server's file opened by an
#: older one would be quietly half-read otherwise.
SCHEMA = 1

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
    saves      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS characters_account ON characters (account);
"""


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
        # Nothing to migrate yet. When there is, it goes here, one step per version,
        # and this comment is the reminder that a migration is code and not a hope.

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
                 resource, x, elevation, y, map_name, seen_at, saves)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
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
                saves = characters.saves + 1
            """,
            (
                key, account, character, level, experience, health, max_health,
                resource, x, elevation, y, map_name, time.time(),
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
