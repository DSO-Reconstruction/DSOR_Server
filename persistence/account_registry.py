"""Durable account and session registry for the login/register service.

This is the authority the launcher talks to over the network and the game
services consult when they need to know whether a connection was logged in.
Accounts, their claimed characters, and issued sessions live in a WAL-backed
SQLite database so a restart of either side keeps everyone logged in.

Sessions are the same GUIDs the game client receives through ``-sessionId``,
which is what makes them checkable: the chat service is handed those sixteen
bytes by the client and can look the session up here.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid


ACCOUNT_REGISTRY_SCHEMA_VERSION = 1

# Continues the historical launch-client.ps1 identities so existing captures
# and logs stay recognizable.
FIRST_ACCOUNT_ID = 777_777
MAX_ACCOUNT_ID = 0xFFFF_FFFF

DEFAULT_SESSION_TTL_SECONDS = 12 * 60 * 60
MIN_SESSION_TTL_SECONDS = 60
MAX_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60

PASSWORD_ITERATIONS = 200_000
MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 256

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{2,23}$")


class AccountRegistryError(Exception):
    """Base class for the registry's expected, reportable failures."""


class InvalidUsernameError(AccountRegistryError):
    pass


class InvalidPasswordError(AccountRegistryError):
    pass


class UsernameTakenError(AccountRegistryError):
    pass


class UnknownAccountError(AccountRegistryError):
    pass


class InvalidCredentialsError(AccountRegistryError):
    pass


class AccountDisabledError(AccountRegistryError):
    pass


class UnknownSessionError(AccountRegistryError):
    pass


class CharacterClaimedError(AccountRegistryError):
    pass


@dataclass(frozen=True)
class AccountRecord:
    account_id: int
    username: str
    created_at: float
    last_login_at: float
    disabled: bool
    character_ids: tuple[int, ...] = ()

    def to_document(self) -> dict:
        return {
            "account_id": self.account_id,
            "username": self.username,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
            "disabled": self.disabled,
            "character_ids": list(self.character_ids),
        }


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    account_id: int
    username: str
    issued_at: float
    expires_at: float
    client_ip: str = ""
    character_ids: tuple[int, ...] = field(default=())

    def is_valid(self, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.expires_at > now

    def expires_in(self, *, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return max(0.0, self.expires_at - now)

    def to_document(self) -> dict:
        return {
            "session_id": self.session_id,
            "account_id": self.account_id,
            "username": self.username,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "expires_in": self.expires_in(),
            "character_ids": list(self.character_ids),
        }


def validate_username(username: str) -> str:
    """Return the stored form of ``username`` or explain why it is invalid."""

    candidate = str(username or "").strip()
    if not USERNAME_PATTERN.match(candidate):
        raise InvalidUsernameError(
            "username must be 3-24 characters, start with a letter or digit, "
            "and use only letters, digits, dot, dash, or underscore"
        )
    return candidate


def validate_password(password: str) -> str:
    if not isinstance(password, str):
        raise InvalidPasswordError("password must be text")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise InvalidPasswordError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise InvalidPasswordError(
            f"password must be at most {MAX_PASSWORD_LENGTH} characters"
        )
    return password


def hash_password(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )


def session_id_candidates(value: str | bytes | bytearray | memoryview):
    """Every canonical session ID a client value could refer to.

    The launcher sends the GUID as text. The game client hands the chat
    service sixteen raw bytes, and Windows GUIDs are serialized in both
    big-endian and mixed-endian layouts depending on who wrote the struct, so
    both readings are resolved against the database.
    """

    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) != 16:
            return ()
        candidates = [str(uuid.UUID(bytes=raw)), str(uuid.UUID(bytes_le=raw))]
        return tuple(dict.fromkeys(candidates))
    try:
        return (str(uuid.UUID(str(value))),)
    except (AttributeError, TypeError, ValueError):
        return ()


class AccountRegistry:
    """Thread-safe SQLite store of accounts, character claims, and sessions."""

    def __init__(
        self,
        path: str | Path,
        *,
        session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
        password_iterations: int = PASSWORD_ITERATIONS,
    ) -> None:
        self.path = Path(path)
        self.session_ttl_seconds = self._checked_ttl(session_ttl_seconds)
        self.password_iterations = max(10_000, int(password_iterations))
        self._lock = threading.RLock()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with closing(self._connection.cursor()) as cursor:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
        self._create_schema()
        # Claims change only through this registry. Gameplay wallet lookups
        # need ownership, not a fresh account/character SQL query chain.
        self._character_owners: dict[int, int] = {}
        self._owned_characters: dict[int, set[int]] = {}
        for row in self._connection.execute(
            "SELECT character_id, account_id FROM account_characters"
        ):
            character_id, account_id = int(row[0]), int(row[1])
            self._character_owners[character_id] = account_id
            self._owned_characters.setdefault(account_id, set()).add(character_id)

    # -- schema ------------------------------------------------------------

    def _create_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                BEGIN;
                CREATE TABLE IF NOT EXISTS registry_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    iterations INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    last_login_at REAL NOT NULL DEFAULT 0,
                    disabled INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS account_characters (
                    character_id INTEGER PRIMARY KEY,
                    account_id INTEGER NOT NULL
                        REFERENCES accounts(account_id) ON DELETE CASCADE,
                    claimed_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS account_characters_by_account
                    ON account_characters(account_id);
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    account_id INTEGER NOT NULL
                        REFERENCES accounts(account_id) ON DELETE CASCADE,
                    issued_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    client_ip TEXT NOT NULL DEFAULT '',
                    revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS sessions_by_account
                    ON sessions(account_id);
                CREATE INDEX IF NOT EXISTS sessions_by_expiry
                    ON sessions(expires_at);
                CREATE INDEX IF NOT EXISTS sessions_by_ip
                    ON sessions(client_ip);
                COMMIT;
                """
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO registry_metadata(key, value) "
                "VALUES('schema_version', ?)",
                (str(ACCOUNT_REGISTRY_SCHEMA_VERSION),),
            )
            row = self._connection.execute(
                "SELECT value FROM registry_metadata WHERE key='schema_version'"
            ).fetchone()
            stored = int(row["value"]) if row is not None else 0
            if stored != ACCOUNT_REGISTRY_SCHEMA_VERSION:
                raise AccountRegistryError(
                    f"{self.path} has account registry schema version "
                    f"{stored}; this build expects "
                    f"{ACCOUNT_REGISTRY_SCHEMA_VERSION}"
                )

    @staticmethod
    def _checked_ttl(seconds: float) -> float:
        value = float(seconds)
        if not MIN_SESSION_TTL_SECONDS <= value <= MAX_SESSION_TTL_SECONDS:
            raise ValueError(
                "session lifetime must be between "
                f"{MIN_SESSION_TTL_SECONDS} and {MAX_SESSION_TTL_SECONDS} "
                "seconds"
            )
        return value

    # -- accounts ----------------------------------------------------------

    def _character_ids(self, account_id: int) -> tuple[int, ...]:
        rows = self._connection.execute(
            "SELECT character_id FROM account_characters WHERE account_id=? "
            "ORDER BY character_id",
            (int(account_id),),
        ).fetchall()
        return tuple(int(row["character_id"]) for row in rows)

    def _account_from_row(self, row: sqlite3.Row) -> AccountRecord:
        return AccountRecord(
            account_id=int(row["account_id"]),
            username=str(row["username"]),
            created_at=float(row["created_at"]),
            last_login_at=float(row["last_login_at"]),
            disabled=bool(row["disabled"]),
            character_ids=self._character_ids(int(row["account_id"])),
        )

    def _next_account_id(self) -> int:
        row = self._connection.execute(
            "SELECT MAX(account_id) AS highest FROM accounts"
        ).fetchone()
        highest = int(row["highest"]) if row["highest"] is not None else 0
        candidate = max(FIRST_ACCOUNT_ID, highest + 1)
        if candidate > MAX_ACCOUNT_ID:
            raise AccountRegistryError("no free account id remains")
        return candidate

    def register(
        self,
        username: str,
        password: str,
        *,
        client_ip: str = "",
        account_id: int | None = None,
        character_ids: tuple[int, ...] | list[int] = (),
    ) -> AccountRecord:
        """Create an account. The username is unique, case-insensitively."""

        username = validate_username(username)
        validate_password(password)
        salt = secrets.token_bytes(16)
        digest = hash_password(password, salt, self.password_iterations)
        now = time.time()
        with self._lock:
            if account_id is None:
                account_id = self._next_account_id()
            account_id = int(account_id)
            if not 0 < account_id <= MAX_ACCOUNT_ID:
                raise AccountRegistryError(
                    "account id must be a non-zero uint32"
                )
            try:
                self._connection.execute(
                    "INSERT INTO accounts(account_id, username, password_salt,"
                    " password_hash, iterations, created_at, last_login_at,"
                    " disabled) VALUES(?, ?, ?, ?, ?, ?, 0, 0)",
                    (
                        account_id,
                        username,
                        salt,
                        digest,
                        self.password_iterations,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise UsernameTakenError(
                    f"the name {username!r} is already registered"
                ) from exc
            for character_id in character_ids:
                self._claim_character_locked(int(character_id), account_id)
            row = self._connection.execute(
                "SELECT * FROM accounts WHERE account_id=?", (account_id,)
            ).fetchone()
            return self._account_from_row(row)

    def account(self, username: str) -> AccountRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM accounts WHERE username=? COLLATE NOCASE",
                (str(username),),
            ).fetchone()
            if row is None:
                raise UnknownAccountError(f"no account named {username!r}")
            return self._account_from_row(row)

    def account_by_id(self, account_id: int) -> AccountRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM accounts WHERE account_id=?", (int(account_id),)
            ).fetchone()
            if row is None:
                raise UnknownAccountError(f"no account {account_id}")
            return self._account_from_row(row)

    def accounts(self) -> tuple[AccountRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM accounts ORDER BY account_id"
            ).fetchall()
            return tuple(self._account_from_row(row) for row in rows)

    def count_accounts(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM accounts"
            ).fetchone()
            return int(row["total"])

    def verify_password(self, username: str, password: str) -> AccountRecord:
        """Check credentials without issuing a session."""

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM accounts WHERE username=? COLLATE NOCASE",
                (str(username or ""),),
            ).fetchone()
        if row is None:
            # Keep unknown-name timing comparable without serializing every
            # session lookup behind deliberately expensive password hashing.
            hash_password(
                str(password or ""),
                b"\x00" * 16,
                self.password_iterations,
            )
            raise InvalidCredentialsError("unknown username or password")
        expected = bytes(row["password_hash"])
        actual = hash_password(
            str(password or ""), bytes(row["password_salt"]),
            int(row["iterations"]),
        )
        if not hmac.compare_digest(expected, actual):
            raise InvalidCredentialsError("unknown username or password")
        with self._lock:
            current = self._connection.execute(
                "SELECT * FROM accounts WHERE account_id=?",
                (int(row["account_id"]),),
            ).fetchone()
            # A reset/deletion during hashing must invalidate the old proof.
            if current is None or any(
                current[field] != row[field]
                for field in ("password_salt", "password_hash", "iterations")
            ):
                raise InvalidCredentialsError("unknown username or password")
            if bool(current["disabled"]):
                raise AccountDisabledError(
                    f"account {current['username']!r} is disabled"
                )
            return self._account_from_row(current)

    def set_password(
        self, username: str, current_password: str, new_password: str
    ) -> AccountRecord:
        account = self.verify_password(username, current_password)
        validate_password(new_password)
        salt = secrets.token_bytes(16)
        digest = hash_password(new_password, salt, self.password_iterations)
        with self._lock:
            self._connection.execute(
                "UPDATE accounts SET password_salt=?, password_hash=?, "
                "iterations=? WHERE account_id=?",
                (salt, digest, self.password_iterations, account.account_id),
            )
            # A password change ends every session issued under the old one.
            self._connection.execute(
                "UPDATE sessions SET revoked=1 WHERE account_id=?",
                (account.account_id,),
            )
        return self.account_by_id(account.account_id)

    def set_disabled(self, username: str, disabled: bool) -> AccountRecord:
        account = self.account(username)
        with self._lock:
            self._connection.execute(
                "UPDATE accounts SET disabled=? WHERE account_id=?",
                (1 if disabled else 0, account.account_id),
            )
            if disabled:
                self._connection.execute(
                    "UPDATE sessions SET revoked=1 WHERE account_id=?",
                    (account.account_id,),
                )
        return self.account_by_id(account.account_id)

    def delete_account(self, username: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT account_id FROM accounts WHERE username=? COLLATE NOCASE",
                (str(username),),
            ).fetchone()
            cursor = self._connection.execute(
                "DELETE FROM accounts WHERE username=? COLLATE NOCASE",
                (str(username),),
            )
            if cursor.rowcount and row is not None:
                for character_id in self._owned_characters.pop(int(row[0]), ()):
                    self._character_owners.pop(character_id, None)
            return cursor.rowcount > 0

    # -- character claims --------------------------------------------------

    def _claim_character_locked(
        self, character_id: int, account_id: int
    ) -> None:
        row = self._connection.execute(
            "SELECT account_id FROM account_characters WHERE character_id=?",
            (int(character_id),),
        ).fetchone()
        if row is not None and int(row["account_id"]) != int(account_id):
            raise CharacterClaimedError(
                f"character {character_id} belongs to another account"
            )
        self._connection.execute(
            "INSERT OR REPLACE INTO account_characters"
            "(character_id, account_id, claimed_at) VALUES(?, ?, ?)",
            (int(character_id), int(account_id), time.time()),
        )
        self._character_owners[int(character_id)] = int(account_id)
        self._owned_characters.setdefault(int(account_id), set()).add(int(character_id))

    def claim_character(
        self, username: str, character_id: int
    ) -> AccountRecord:
        account = self.account(username)
        with self._lock:
            self._claim_character_locked(character_id, account.account_id)
        return self.account_by_id(account.account_id)

    def release_character(self, character_id: int) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM account_characters WHERE character_id=?",
                (int(character_id),),
            )
            account_id = self._character_owners.pop(int(character_id), None)
            if account_id is not None:
                self._owned_characters[account_id].discard(int(character_id))
            return cursor.rowcount > 0

    def wallet_owner(self, character_id: int) -> tuple[int, tuple[int, ...]] | None:
        """Return the RAM ownership needed for an account-wide currency wallet."""
        with self._lock:
            account_id = self._character_owners.get(int(character_id))
            if account_id is None:
                return None
            return account_id, tuple(sorted(self._owned_characters[account_id]))

    def character_owner(self, character_id: int) -> AccountRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT account_id FROM account_characters WHERE "
                "character_id=?",
                (int(character_id),),
            ).fetchone()
            if row is None:
                return None
            return self.account_by_id(int(row["account_id"]))

    def claimed_characters(self) -> dict[int, str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT c.character_id AS character_id, a.username AS username "
                "FROM account_characters c JOIN accounts a "
                "ON a.account_id = c.account_id"
            ).fetchall()
            return {
                int(row["character_id"]): str(row["username"]) for row in rows
            }

    # -- sessions ----------------------------------------------------------

    def _session_from_row(self, row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_id=str(row["session_id"]),
            account_id=int(row["account_id"]),
            username=str(row["username"]),
            issued_at=float(row["issued_at"]),
            expires_at=float(row["expires_at"]),
            client_ip=str(row["client_ip"]),
            character_ids=self._character_ids(int(row["account_id"])),
        )

    def _issue_locked(
        self,
        account: AccountRecord,
        *,
        client_ip: str,
        ttl_seconds: float,
        session_id: str | None = None,
    ) -> SessionRecord:
        now = time.time()
        identifier = (
            str(uuid.UUID(str(session_id)))
            if session_id is not None
            else str(uuid.uuid4())
        )
        self._connection.execute(
            "INSERT OR REPLACE INTO sessions(session_id, account_id, "
            "issued_at, expires_at, client_ip, revoked) VALUES(?, ?, ?, ?, ?, 0)",
            (
                identifier,
                account.account_id,
                now,
                now + ttl_seconds,
                str(client_ip or ""),
            ),
        )
        self._connection.execute(
            "UPDATE accounts SET last_login_at=? WHERE account_id=?",
            (now, account.account_id),
        )
        return SessionRecord(
            session_id=identifier,
            account_id=account.account_id,
            username=account.username,
            issued_at=now,
            expires_at=now + ttl_seconds,
            client_ip=str(client_ip or ""),
            character_ids=account.character_ids,
        )

    def login(
        self,
        username: str,
        password: str,
        *,
        client_ip: str = "",
        ttl_seconds: float | None = None,
        replace_existing: bool = True,
    ) -> SessionRecord:
        """Verify credentials and issue a session GUID for the game client."""

        account = self.verify_password(username, password)
        ttl = self._checked_ttl(
            self.session_ttl_seconds if ttl_seconds is None else ttl_seconds
        )
        with self._lock:
            # Recheck disabling/deletion between verification and issuance.
            account = self.account_by_id(account.account_id)
            if account.disabled:
                raise AccountDisabledError(f"account {account.username!r} is disabled")
            if replace_existing:
                # One live session per account: logging in again anywhere ends
                # the previous one, so a stolen GUID cannot outlive a re-login.
                self._connection.execute(
                    "UPDATE sessions SET revoked=1 WHERE account_id=? "
                    "AND revoked=0",
                    (account.account_id,),
                )
            return self._issue_locked(
                account, client_ip=client_ip, ttl_seconds=ttl
            )

    def refresh(
        self,
        session_id: str | bytes,
        *,
        client_ip: str = "",
        ttl_seconds: float | None = None,
    ) -> SessionRecord:
        """Rotate a live session into a new GUID with a fresh lifetime."""

        current = self.validate(session_id)
        if current is None:
            raise UnknownSessionError("session is unknown, expired, or revoked")
        ttl = self._checked_ttl(
            self.session_ttl_seconds if ttl_seconds is None else ttl_seconds
        )
        with self._lock:
            account = self.account_by_id(current.account_id)
            if account.disabled:
                raise AccountDisabledError(
                    f"account {account.username!r} is disabled"
                )
            self._connection.execute(
                "UPDATE sessions SET revoked=1 WHERE session_id=?",
                (current.session_id,),
            )
            return self._issue_locked(
                account,
                client_ip=client_ip or current.client_ip,
                ttl_seconds=ttl,
            )

    def validate(
        self, session_id: str | bytes, *, now: float | None = None
    ) -> SessionRecord | None:
        """Resolve a session GUID, in text or raw client byte form."""

        candidates = session_id_candidates(session_id)
        if not candidates:
            return None
        now = time.time() if now is None else now
        placeholders = ",".join("?" for _ in candidates)
        with self._lock:
            row = self._connection.execute(
                "SELECT s.*, a.username AS username, a.disabled AS disabled "
                "FROM sessions s JOIN accounts a ON a.account_id=s.account_id "
                f"WHERE s.session_id IN ({placeholders}) AND s.revoked=0 "
                "AND s.expires_at > ? AND a.disabled=0",
                (*candidates, now),
            ).fetchone()
            if row is None:
                return None
            return self._session_from_row(row)

    def find_session(
        self, payload: bytes, *, now: float | None = None, limit: int = 512
    ) -> SessionRecord | None:
        """Locate a live session inside a client packet.

        The client's ``0x8a`` hello to the login and character services
        carries its identity as a tagged field stream: ``01 01`` character id,
        ``02 01`` account id, ``03 05`` the length-prefixed session GUID. That
        layout differs between client patches, so rather than trusting an
        offset this checks every sixteen-byte window against the issued
        sessions and takes the one that resolves. A window can only match if
        the server itself handed that GUID out, so a wrong guess authenticates
        nobody -- it simply finds nothing.
        """

        if not payload:
            return None
        window = payload[: max(16, int(limit))]
        anchor = window.find(b"\x03\x05\x10\x00")
        if anchor >= 0:
            record = self.validate(window[anchor + 4 : anchor + 20], now=now)
            if record is not None:
                return record
        candidates: list[str] = []
        seen: set[str] = set()
        for offset in range(0, len(window) - 15):
            for candidate in session_id_candidates(window[offset : offset + 16]):
                if candidate not in seen:
                    seen.add(candidate)
                    candidates.append(candidate)
        return self._first_live_session(candidates, now=now)

    def _first_live_session(
        self, candidates: list[str], *, now: float | None = None
    ) -> SessionRecord | None:
        if not candidates:
            return None
        now = time.time() if now is None else now
        with self._lock:
            # Chunked to stay under SQLite's bound-parameter limit.
            for start in range(0, len(candidates), 400):
                chunk = candidates[start : start + 400]
                placeholders = ",".join("?" for _ in chunk)
                row = self._connection.execute(
                    "SELECT s.*, a.username AS username FROM sessions s "
                    "JOIN accounts a ON a.account_id=s.account_id "
                    f"WHERE s.session_id IN ({placeholders}) AND s.revoked=0 "
                    "AND s.expires_at > ? AND a.disabled=0",
                    (*chunk, now),
                ).fetchone()
                if row is not None:
                    return self._session_from_row(row)
        return None

    def revoke(self, session_id: str | bytes) -> bool:
        candidates = session_id_candidates(session_id)
        if not candidates:
            return False
        placeholders = ",".join("?" for _ in candidates)
        with self._lock:
            cursor = self._connection.execute(
                f"UPDATE sessions SET revoked=1 WHERE session_id IN "
                f"({placeholders}) AND revoked=0",
                candidates,
            )
            return cursor.rowcount > 0

    def active_sessions(
        self, *, now: float | None = None
    ) -> tuple[SessionRecord, ...]:
        now = time.time() if now is None else now
        with self._lock:
            rows = self._connection.execute(
                "SELECT s.*, a.username AS username FROM sessions s "
                "JOIN accounts a ON a.account_id=s.account_id "
                "WHERE s.revoked=0 AND s.expires_at > ? AND a.disabled=0 "
                "ORDER BY s.issued_at",
                (now,),
            ).fetchall()
            return tuple(self._session_from_row(row) for row in rows)

    def session_for_character(
        self, character_id: int, *, now: float | None = None
    ) -> SessionRecord | None:
        """The live session of the account owning ``character_id``."""

        now = time.time() if now is None else now
        with self._lock:
            row = self._connection.execute(
                "SELECT s.*, a.username AS username FROM sessions s "
                "JOIN accounts a ON a.account_id=s.account_id "
                "JOIN account_characters c ON c.account_id=s.account_id "
                "WHERE c.character_id=? AND s.revoked=0 AND s.expires_at > ? "
                "AND a.disabled=0 ORDER BY s.issued_at DESC",
                (int(character_id), now),
            ).fetchone()
            if row is None:
                return None
            return self._session_from_row(row)

    def authorized_ip(self, client_ip: str, *, now: float | None = None) -> bool:
        """Whether any live session was issued to ``client_ip``.

        The RakNet services never receive the session GUID, so this is what
        lets them tell a logged-in client from an arbitrary one.
        """

        if not client_ip:
            return False
        now = time.time() if now is None else now
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM sessions s JOIN accounts a "
                "ON a.account_id=s.account_id WHERE s.client_ip=? "
                "AND s.revoked=0 AND s.expires_at > ? AND a.disabled=0 LIMIT 1",
                (str(client_ip), now),
            ).fetchone()
            return row is not None

    def purge_expired(
        self,
        *,
        older_than_seconds: float = 86_400,
        now: float | None = None,
    ) -> int:
        """Delete sessions that expired or were revoked a while ago."""

        now = time.time() if now is None else now
        cutoff = now - max(0.0, float(older_than_seconds))
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM sessions WHERE expires_at < ? OR "
                "(revoked=1 AND issued_at < ?)",
                (cutoff, cutoff),
            )
            return int(cursor.rowcount)

    def count_active_sessions(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM sessions s "
                "JOIN accounts a ON a.account_id=s.account_id "
                "WHERE s.revoked=0 AND s.expires_at > ? AND a.disabled=0",
                (now,),
            ).fetchone()
            return int(row["total"])

    def close(self) -> None:
        with self._lock:
            self._connection.close()
