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

import hashlib
import logging
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

#: The schema this code expects. A stored file that says anything else is migrated
#: forward, or refused if it is from the future -- a newer server's file opened by an
#: older one would be quietly half-read otherwise.
log = logging.getLogger("store")

SCHEMA = 3

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
    quickslots TEXT,
    -- What the character is called and what class it is. Schema 3. In CREATE *and* in
    -- the migration, because a fresh file never runs the migration and an existing one
    -- never re-runs CREATE.
    name            TEXT,
    character_class TEXT
);
CREATE INDEX IF NOT EXISTS characters_account ON characters (account);
-- Schema 3. An account is a name, a secret and the session id the launcher was last
-- given -- which is the whole of what the client authenticates with. The capture
-- settles that: no password ever crosses the wire, only the account id and a session
-- GUID the launcher received out of band, in one 0x8A per tier.
CREATE TABLE IF NOT EXISTS accounts (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL UNIQUE,
    -- scrypt, as "salt$hash" in hex. Not a wire secret: the wire never sees it.
    secret     TEXT    NOT NULL,
    -- The session GUID issued at the last login. Replacing it is what makes an old
    -- cmd.txt stop working.
    session    TEXT,
    created_at REAL    NOT NULL DEFAULT 0,
    seen_at    REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS accounts_name ON accounts (name);
"""


#: How a password is stored. scrypt with the parameters the standard library
#: documents as interactive, a fresh 16-byte salt per account, written "salt$hash".
SCRYPT_COST = 2 ** 14
SCRYPT_BLOCK = 8
SCRYPT_PARALLEL = 1
SALT_BYTES = 16


def _sealed(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    return salt.hex() + "$" + hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_COST, r=SCRYPT_BLOCK,
        p=SCRYPT_PARALLEL,
    ).hex()


def _matches(password: str, sealed: str) -> bool:
    """Constant-time, because a timing difference is a password oracle."""
    salt, _, want = sealed.partition("$")
    try:
        raw = bytes.fromhex(salt)
    except ValueError:
        return False
    got = hashlib.scrypt(
        password.encode(), salt=raw, n=SCRYPT_COST, r=SCRYPT_BLOCK,
        p=SCRYPT_PARALLEL,
    ).hex()
    return secrets.compare_digest(got, want)


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
    #: What the character is called, and what class it is. Schema 3, and the reason
    #: they are here: until now the name every player carried was the recording's, so
    #: two clients were both "Username".
    name: str | None = None
    character_class: str | None = None


@dataclass(frozen=True)
class Account:
    """An account as the file holds it."""

    id: int
    name: str
    session: str | None
    created_at: float
    seen_at: float


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
        if found < 3:
            # Schema 3 adds the accounts table -- which CREATE has already made, since
            # it runs on every open -- and two columns on a character. Same reasoning:
            # a column is added rather than hoped for.
            columns = {
                row[1] for row in self.db.execute("PRAGMA table_info(characters)")
            }
            for column in ("name", "character_class"):
                if column not in columns:
                    self.db.execute(
                        f"ALTER TABLE characters ADD COLUMN {column} TEXT"
                    )
            log.info("%s: migrated to schema 3 (accounts, and a character's name)",
                     self.path)
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
            name=row["name"] if "name" in row.keys() else None,
            character_class=(
                row["character_class"] if "character_class" in row.keys() else None
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
        name: str | None = None,
        character_class: str | None = None,
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
                 resource, x, elevation, y, map_name, seen_at, quickslots,
                 name, character_class, saves)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
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
                -- And the same for the name and the class, which a gameplay save
                -- knows nothing about: they are set when the character is created.
                name = COALESCE(excluded.name, characters.name),
                character_class = COALESCE(
                    excluded.character_class, characters.character_class
                ),
                saves = characters.saves + 1
            """,
            (
                key, account, character, level, experience, health, max_health,
                resource, x, elevation, y, map_name, time.time(),
                _slots_to(quickslots), name, character_class,
            ),
        )

    #: The id space characters are numbered in. The live service's are around
    #: 111,383,506 and the recording's 111,935,374 -- nine digits, and the client puts
    #: whichever it is given straight back into its 0x8A and its selection command. So
    #: any distinct number works, and starting well below the real ones keeps this
    #: server's characters obviously its own.
    FIRST_CHARACTER = 1_000_001

    def add_character(
        self,
        account: int,
        name: str,
        character_class: str = "warrior",
        level: int = 1,
        map_name: str | None = None,
    ) -> Saved:
        """Create a character for *account* and return it.

        The character id is this file's own counter rather than anything the client
        chose: the client has a creation screen and nothing answers it, so a character
        exists because an account was made with one.
        """
        row = self.db.execute("SELECT MAX(character) AS top FROM characters").fetchone()
        character = max(int(row["top"] or 0) + 1, self.FIRST_CHARACTER)
        key = f"{account}:{character}"
        self.save(
            key, account, character, level, 0, 0.0, 0.0, 0.0,
            map_name=map_name, name=name, character_class=character_class,
        )
        log.info(
            "%s: character %d (%s the %s) created for account %d",
            self.path, character, name, character_class, account,
        )
        saved = self.load(key)
        assert saved is not None
        return saved

    def characters_of(self, account: int) -> list[Saved]:
        """Every character saved under one account."""
        rows = self.db.execute(
            "SELECT id FROM characters WHERE account = ? ORDER BY id", (account,)
        ).fetchall()
        return [saved for row in rows if (saved := self.load(row["id"]))]

    # ── accounts ─────────────────────────────────────────────────────────────
    #
    # What the client authenticates with, and nothing more. From the capture: no
    # password ever crosses the wire, on any tier, in either direction. The launcher is
    # given an account id and a session GUID out of band and sends them in one 0x8A per
    # tier -- byte-identical across all twelve occurrences of one session, the GUID
    # never rotated. So the secret lives here and is checked here, and what travels is
    # a pair this file issued.

    def add_account(self, name: str, password: str) -> Account:
        """Create an account. Raises if the name is taken."""
        secret = _sealed(password)
        now = time.time()
        try:
            cursor = self.db.execute(
                "INSERT INTO accounts (name, secret, created_at, seen_at) "
                "VALUES (?, ?, ?, ?)",
                (name, secret, now, now),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"there is already an account called {name!r}") from None
        log.info("%s: account %d created for %r", self.path, cursor.lastrowid, name)
        return Account(
            id=int(cursor.lastrowid), name=name, session=None,
            created_at=now, seen_at=now,
        )

    def account(self, name: str) -> Account | None:
        return self._account("SELECT * FROM accounts WHERE name = ?", (name,))

    def account_by_id(self, account: int) -> Account | None:
        return self._account("SELECT * FROM accounts WHERE id = ?", (account,))

    def _account(self, query: str, arguments: tuple) -> Account | None:
        row = self.db.execute(query, arguments).fetchone()
        if row is None:
            return None
        return Account(
            id=row["id"], name=row["name"], session=row["session"],
            created_at=row["created_at"], seen_at=row["seen_at"],
        )

    def sign_in(self, name: str, password: str) -> tuple[Account, str] | None:
        """Check the password and issue a session id, or None.

        Issuing replaces whatever the account had, so logging in again invalidates the
        launcher line the last login handed out. That is the only revocation there is,
        and it is worth having: the session id is the whole credential.
        """
        found = self.account(name)
        if found is None:
            return None
        row = self.db.execute(
            "SELECT secret FROM accounts WHERE id = ?", (found.id,)
        ).fetchone()
        if not _matches(password, row["secret"]):
            log.info("%s: wrong password for %r", self.path, name)
            return None
        session = str(uuid.uuid4())
        self.db.execute(
            "UPDATE accounts SET session = ?, seen_at = ? WHERE id = ?",
            (session, time.time(), found.id),
        )
        log.info("%s: %r signed in, session issued", self.path, name)
        return replace(found, session=session), session

    def session_matches(self, account: int, session: uuid.UUID | None) -> bool:
        """Whether *session* is the one this account was last given."""
        found = self.account_by_id(account)
        if found is None or not found.session or session is None:
            return False
        return str(session) == found.session

    def accounts(self) -> list[Account]:
        return [
            self._account("SELECT * FROM accounts WHERE id = ?", (row["id"],))
            for row in self.db.execute("SELECT id FROM accounts ORDER BY id")
        ]

    @property
    def count(self) -> int:
        return self.db.execute("SELECT count(*) FROM characters").fetchone()[0]
