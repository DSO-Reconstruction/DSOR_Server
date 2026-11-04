"""CMS roles, audit trail, bookings and bonus codes in the game's account database."""

from contextlib import contextmanager
import hashlib
import json
import secrets
import time

from dso_server.persistence.account_registry import (
    hash_password, validate_password, validate_username,
)


class BonusCodeRejected(ValueError):
    """A player's code was refused; ``reason`` is invalid, used or ineligible."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class AdminRepository:
    """Share the registry's connection and lock; never create a second identity store."""

    def __init__(self, registry):
        self.registry = registry
        self.lock = registry._lock
        self.db = registry._connection
        with self.lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS admin_roles (
                    account_id INTEGER PRIMARY KEY REFERENCES accounts(account_id)
                        ON DELETE CASCADE,
                    enabled INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_remembered_sessions (
                    token_hash TEXT PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                    csrf TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    stamp TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS admin_remembered_sessions_account
                    ON admin_remembered_sessions(account_id);
                CREATE TABLE IF NOT EXISTS admin_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    details TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    character_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    delivered_at REAL,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS admin_bookings_character
                    ON admin_bookings(character_id, status);
                CREATE TABLE IF NOT EXISTS admin_bonus_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    grants TEXT NOT NULL,
                    max_redemptions INTEGER NOT NULL DEFAULT 0,
                    expires_at REAL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    note TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_bonus_code_redemptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code_id INTEGER NOT NULL REFERENCES admin_bonus_codes(id),
                    account_id INTEGER NOT NULL,
                    character_id INTEGER NOT NULL,
                    booking_id INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(code_id, account_id)
                );
            """)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(admin_bookings)")}
            if "options" not in columns:
                self.db.execute("ALTER TABLE admin_bookings ADD COLUMN options TEXT NOT NULL DEFAULT '{}'")

    @staticmethod
    def remembered_token_hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def save_remembered_session(self, token, session, now):
        with self.transaction():
            self.db.execute("DELETE FROM admin_remembered_sessions WHERE expires_at <= ?", (now,))
            if self.db.execute("SELECT COUNT(*) FROM admin_remembered_sessions").fetchone()[0] >= 2048:
                return False
            self.db.execute(
                "INSERT INTO admin_remembered_sessions(token_hash,account_id,csrf,expires_at,stamp) VALUES(?,?,?,?,?)",
                (self.remembered_token_hash(token), session["account_id"], session["csrf"],
                 session["expires_at"], session["stamp"]),
            )
            return True

    def remembered_session(self, token):
        with self.lock:
            row = self.db.execute(
                "SELECT account_id,csrf,expires_at,stamp FROM admin_remembered_sessions WHERE token_hash=?",
                (self.remembered_token_hash(token),),
            ).fetchone()
        return dict(row) if row is not None else None

    def forget_session(self, token):
        with self.lock:
            self.db.execute("DELETE FROM admin_remembered_sessions WHERE token_hash=?",
                            (self.remembered_token_hash(token),))

    def forget_account_sessions(self, account_id):
        with self.lock:
            self.db.execute("DELETE FROM admin_remembered_sessions WHERE account_id=?", (account_id,))

    @staticmethod
    def booking_document(row):
        document = dict(row)
        document["options"] = json.loads(document.get("options") or "{}")
        return document

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def audit(self, actor, action, target, details):
        with self.lock:
            self.db.execute(
                "INSERT INTO admin_audit(created_at,actor,action,target,details) "
                "VALUES(?,?,?,?,?)",
                (time.time(), str(actor), action, str(target), json.dumps(details)),
            )

    def role(self, account_id):
        with self.lock:
            row = self.db.execute(
                "SELECT enabled FROM admin_roles WHERE account_id=?", (account_id,)
            ).fetchone()
            return None if row is None else bool(row[0])

    def credential_stamp(self, account_id):
        with self.lock:
            row = self.db.execute(
                "SELECT username,password_hash,disabled FROM accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if row is None or row[2]:
                return None
            return hashlib.sha256(row[0].encode() + bytes(row[1])).hexdigest()

    def update_account(self, actor, account_id, changes, reason):
        """Atomically edit allowlisted fields, revoke game tokens, and record the change."""
        allowed = {"username", "password", "disabled", "admin"}
        if not changes or set(changes) - allowed:
            raise ValueError("choose username, password, disabled or admin")
        values = dict(changes)
        if "username" in values:
            values["username"] = validate_username(values["username"])
        for key in ("disabled", "admin"):
            if key in values and type(values[key]) is not bool:
                raise ValueError(f"{key} must be a boolean")
        salt = digest = None
        if "password" in values:
            validate_password(values["password"])
            salt = secrets.token_bytes(16)
            digest = hash_password(values["password"], salt, self.registry.password_iterations)
        with self.transaction():
            before = self.registry.account_by_id(account_id)
            for key in ("username", "disabled"):
                if key in values:
                    self.db.execute(f"UPDATE accounts SET {key}=? WHERE account_id=?",
                                    (values[key], account_id))
            if digest is not None:
                self.db.execute(
                    "UPDATE accounts SET password_salt=?,password_hash=?,iterations=? "
                    "WHERE account_id=?",
                    (salt, digest, self.registry.password_iterations, account_id),
                )
            if "admin" in values:
                self.db.execute(
                    "INSERT INTO admin_roles(account_id,enabled) VALUES(?,?) "
                    "ON CONFLICT(account_id) DO UPDATE SET enabled=excluded.enabled",
                    (account_id, values["admin"]),
                )
            self.db.execute("UPDATE sessions SET revoked=1 WHERE account_id=?", (account_id,))
            self.audit(actor, "account.update", account_id, {
                "reason": reason, "previous_username": before.username,
                "changes": {k: ("[reset]" if k == "password" else v) for k, v in values.items()},
            })
        return self.registry.account_by_id(account_id)

    def revoke_sessions(self, actor, account_id, reason):
        with self.transaction():
            self.registry.account_by_id(account_id)
            self.db.execute("UPDATE sessions SET revoked=1 WHERE account_id=?", (account_id,))
            self.audit(actor, "sessions.revoke", account_id, {"reason": reason})

    def create_booking(self, actor, account_id, character_id, kind, resource, amount, reason, request_id, options=None):
        with self.transaction():
            return self._insert_booking(actor, account_id, character_id, kind, resource, amount,
                                        reason, request_id, options)

    def _insert_booking(self, actor, account_id, character_id, kind, resource, amount, reason, request_id, options=None):
        existing = self.db.execute(
            "SELECT * FROM admin_bookings WHERE request_id=?", (request_id,)
        ).fetchone()
        fields = dict(actor=actor, account_id=account_id, character_id=character_id,
                      kind=kind, resource=resource, amount=amount, reason=reason,
                      options=options or {})
        if existing is not None:
            existing = self.booking_document(existing)
            if any(existing[key] != value for key, value in fields.items()):
                raise ValueError("request ID was already used for a different booking")
            return dict(existing)
        cursor = self.db.execute(
            "INSERT INTO admin_bookings(request_id,account_id,character_id,actor,kind,"
            "resource,amount,reason,created_at,options) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (request_id, account_id, character_id, actor, kind, resource, amount, reason, time.time(),
             json.dumps(options or {}, sort_keys=True)),
        )
        self.audit(actor, "booking.create", cursor.lastrowid, fields)
        return self.booking_document(self.db.execute("SELECT * FROM admin_bookings WHERE id=?",
                                    (cursor.lastrowid,)).fetchone())

    def pending(self, character_id):
        with self.lock:
            return [self.booking_document(row) for row in self.db.execute(
                "SELECT * FROM admin_bookings WHERE character_id=? AND status='pending' ORDER BY id",
                (character_id,),
            )]

    def booking(self, booking_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM admin_bookings WHERE id=?", (booking_id,)).fetchone()
            if row is None:
                raise ValueError("booking not found")
            return self.booking_document(row)

    def finish_booking(self, booking_id):
        with self.transaction():
            cursor = self.db.execute(
                "UPDATE admin_bookings SET status='delivered',delivered_at=?,error='' "
                "WHERE id=? AND status='pending'", (time.time(), booking_id),
            )
            if cursor.rowcount:
                self.audit("system", "booking.deliver", booking_id, {})

    def booking_error(self, booking_id, error):
        with self.lock:
            self.db.execute("UPDATE admin_bookings SET error=? WHERE id=? AND status='pending'",
                            (str(error)[:500], booking_id))

    def cancel_booking(self, actor, booking_id, reason):
        with self.transaction():
            cursor = self.db.execute(
                "UPDATE admin_bookings SET status='cancelled' WHERE id=? AND status='pending'",
                (booking_id,),
            )
            if not cursor.rowcount:
                raise ValueError("only pending bookings can be cancelled")
            self.audit(actor, "booking.cancel", booking_id, {"reason": reason})

    @staticmethod
    def bonus_code_document(row):
        document = dict(row)
        document["enabled"] = bool(document["enabled"])
        document["grants"] = json.loads(document["grants"])
        return document

    _BONUS_CODE_ROW = ("SELECT c.*,(SELECT COUNT(*) FROM admin_bonus_code_redemptions r "
                       "WHERE r.code_id=c.id) AS redemptions FROM admin_bonus_codes c ")

    def create_bonus_code(self, actor, code, grants, max_redemptions, expires_at, note, reason):
        with self.transaction():
            if self.db.execute("SELECT 1 FROM admin_bonus_codes WHERE code=?", (code,)).fetchone():
                raise ValueError("this bonus code already exists")
            cursor = self.db.execute(
                "INSERT INTO admin_bonus_codes(code,grants,max_redemptions,expires_at,note,actor,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (code, json.dumps(grants, sort_keys=True), max_redemptions, expires_at, note, actor, time.time()),
            )
            self.audit(actor, "bonus_code.create", cursor.lastrowid, {
                "reason": reason, "code": code, "grants": grants,
                "max_redemptions": max_redemptions, "expires_at": expires_at, "note": note,
            })
        return self.bonus_code(cursor.lastrowid)

    def update_bonus_code(self, actor, code_id, changes, reason):
        allowed = {"enabled", "max_redemptions", "expires_at", "note"}
        if not changes or set(changes) - allowed:
            raise ValueError("choose enabled, max_redemptions, expires_at or note")
        with self.transaction():
            self.bonus_code(code_id)
            for key, value in changes.items():
                self.db.execute(f"UPDATE admin_bonus_codes SET {key}=? WHERE id=?", (value, code_id))
            self.audit(actor, "bonus_code.update", code_id, {"reason": reason, "changes": changes})
        return self.bonus_code(code_id)

    def bonus_code(self, code_id):
        with self.lock:
            row = self.db.execute(self._BONUS_CODE_ROW + "WHERE c.id=?", (code_id,)).fetchone()
            if row is None:
                raise ValueError("bonus code not found")
            return self.bonus_code_document(row)

    def bonus_codes(self, *, offset=0, limit=50, query=""):
        where, args = "WHERE c.code LIKE ? OR c.note LIKE ? ", (f"%{query}%", f"%{query}%")
        with self.lock:
            total = self.db.execute("SELECT COUNT(*) FROM admin_bonus_codes c " + where, args).fetchone()[0]
            rows = [self.bonus_code_document(row) for row in self.db.execute(
                self._BONUS_CODE_ROW + where + "ORDER BY c.id DESC LIMIT ? OFFSET ?", (*args, limit, offset))]
            return {"rows": rows, "total": total, "offset": offset, "limit": limit}

    def bonus_code_redemptions(self, code_id, *, offset=0, limit=50):
        with self.lock:
            total = self.db.execute("SELECT COUNT(*) FROM admin_bonus_code_redemptions WHERE code_id=?",
                                    (code_id,)).fetchone()[0]
            rows = [dict(row) for row in self.db.execute(
                "SELECT r.*,a.username,b.status AS booking_status,b.error AS booking_error "
                "FROM admin_bonus_code_redemptions r "
                "LEFT JOIN accounts a ON a.account_id=r.account_id "
                "LEFT JOIN admin_bookings b ON b.id=r.booking_id "
                "WHERE r.code_id=? ORDER BY r.id DESC LIMIT ? OFFSET ?", (code_id, limit, offset))]
            return {"rows": rows, "total": total, "offset": offset, "limit": limit}

    def _open_bonus_code(self, code, account_id):
        row = self.db.execute(self._BONUS_CODE_ROW + "WHERE c.code=?", (code,)).fetchone()
        if row is None or not row["enabled"] or (row["expires_at"] is not None and row["expires_at"] <= time.time()):
            raise BonusCodeRejected("invalid")
        if self.db.execute("SELECT 1 FROM admin_bonus_code_redemptions WHERE code_id=? AND account_id=?",
                           (row["id"], account_id)).fetchone():
            raise BonusCodeRejected("used")
        if row["max_redemptions"] and row["redemptions"] >= row["max_redemptions"]:
            raise BonusCodeRejected("used")
        return self.bonus_code_document(row)

    def open_bonus_code(self, code, account_id):
        """Return a code this account may redeem, or raise BonusCodeRejected."""
        with self.lock:
            return self._open_bonus_code(code, account_id)

    def redeem_bonus_code(self, code, account_id, character_id):
        """Claim the code and queue its booking in one commit."""
        with self.transaction():
            record = self._open_bonus_code(code, account_id)
            booking = self._insert_booking(
                "bonus code", account_id, character_id, "batch", json.dumps(record["grants"]), 1,
                f"Bonus code {record['code']}", f"bonus-code-{record['id']}-{account_id}",
                {"items": record["grants"]})
            self.db.execute(
                "INSERT INTO admin_bonus_code_redemptions(code_id,account_id,character_id,booking_id,created_at) "
                "VALUES(?,?,?,?,?)", (record["id"], account_id, character_id, booking["id"], time.time()),
            )
            return booking

    def history(self, table, *, offset=0, limit=50, query="", account_id=None, character_id=None):
        if table not in {"admin_bookings", "admin_audit"}:
            raise ValueError("invalid history")
        with self.lock:
            field = "reason" if table == "admin_bookings" else "details"
            where = f" WHERE (actor LIKE ? OR {field} LIKE ? OR CAST(id AS TEXT)=?"
            args = [f"%{query}%", f"%{query}%", query]
            if table == "admin_bookings":
                where += " OR resource LIKE ? OR CAST(character_id AS TEXT)=?"
                args.extend([f"%{query}%", query])
            where += ")"
            if table == "admin_bookings":
                for field, value in (("account_id", account_id), ("character_id", character_id)):
                    if value is not None:
                        where += f" AND {field}=?"
                        args.append(int(value))
            total = self.db.execute(f"SELECT COUNT(*) FROM {table}" + where, args).fetchone()[0]
            rows = [dict(row) for row in self.db.execute(
                f"SELECT * FROM {table}" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            )]
            if table == "admin_bookings":
                rows = [self.booking_document(row) for row in rows]
            if table == "admin_audit":
                for row in rows:
                    row["details"] = json.loads(row["details"])
            return {"rows": rows, "total": total, "offset": offset, "limit": limit}
