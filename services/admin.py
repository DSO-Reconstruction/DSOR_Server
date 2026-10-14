"""Admin CMS domain operations and lifecycle; HTTP transport lives in admin_http."""

import argparse
from contextlib import closing
from dataclasses import asdict
import hmac
import json
import os
from pathlib import Path
from urllib.parse import unquote
import secrets
import sqlite3
import threading
import time

from dso_server.paths import DEFAULT_ACCOUNT_DATABASE_PATH
from dso_server.persistence.account_registry import AccountRegistry, AccountRegistryError
from dso_server.persistence.account_state import _state_to_document
from dso_server.persistence.admin_registry import AdminRepository
from dso_server.services.accounts import FailureThrottle
from dso_server.services.admin_bookings import BookingDelivery, CURRENCIES
from dso_server.services.admin_items import item_metadata
from dso_server.services.admin_live import LiveAdministration, map_catalog
from dso_server.services.admin_events import EventAdministration, EventScheduleConflict


DEFAULT_ADMIN_PORT = 8081


class AdminError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def reason_from(payload):
    reason = payload.get("reason", "")
    if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 500:
        raise ValueError("a reason of 3 to 500 characters is required")
    return reason.strip()


def page(rows, query):
    offset, limit = paging(query)
    return {"rows": rows[offset:offset + limit], "total": len(rows), "offset": offset, "limit": limit}


def paging(query):
    offset, limit = int(query.get("offset", "0")), int(query.get("limit", "50"))
    if not 0 <= offset <= 1_000_000 or not 1 <= limit <= 100:
        raise ValueError("invalid page bounds")
    return offset, limit


class AdminService:
    def __init__(self, *, registry, store, static_database_path, host="127.0.0.1",
                 port=DEFAULT_ADMIN_PORT, application=None, secure_cookie=False):
        self.registry, self.store = registry, store
        self.repository = AdminRepository(registry)
        self.delivery = BookingDelivery(self.repository, store, static_database_path)
        self.static_database_path = Path(static_database_path)
        self.host, self.port = host, int(port)
        self.application = application
        self.live = LiveAdministration(self)
        self.events = EventAdministration(self)
        self.delivery.live_receipt = self.live.has_receipt
        self.secure_cookie = secure_cookie
        self.started_at = time.time()
        self.throttle = FailureThrottle(limit=8)
        self._sessions = {}
        self._lock = threading.RLock()
        self._httpd = self._thread = None

    @property
    def cookie_name(self):
        return f"dso_admin_{self.port}"

    @property
    def url(self):
        host = self.host if self.host not in {"", "0.0.0.0", "::"} else "127.0.0.1"
        return f"http://{host}:{self.port}"

    def is_admin(self, account):
        if account.disabled:
            return False
        role = self.repository.role(account.account_id)
        if role is not None:
            return role
        return any(
            profile is not None and profile.admin_privileges
            for profile in (self.store.get_profile(cid) for cid in account.character_ids)
        )

    def account_document(self, account):
        return {**account.to_document(), "admin": self.is_admin(account)}

    def login(self, username, password, address):
        # Serialize credential checks so concurrent guesses cannot bypass the limit.
        with self._lock:
            if self.throttle.blocked(address):
                raise AdminError(429, "too many login attempts; try again in five minutes")
            try:
                if not isinstance(username, str) or not isinstance(password, str) or len(password) > 256:
                    raise ValueError("invalid credentials")
                account = self.registry.verify_password(username, password)
                if not self.is_admin(account):
                    raise ValueError("not an administrator")
            except (AccountRegistryError, ValueError):
                self.throttle.record_failure(address)
                raise AdminError(401, "invalid credentials or administrator access required") from None
            self.throttle.clear(address)
            now = time.time()
            self._sessions = {key: value for key, value in self._sessions.items() if value["expires_at"] > now}
            if len(self._sessions) >= 2048:
                raise AdminError(503, "admin session capacity reached")
            token = secrets.token_urlsafe(32)
            session = {"account_id": account.account_id, "csrf": secrets.token_urlsafe(32),
                       "expires_at": now + 4 * 3600,
                       "stamp": self.repository.credential_stamp(account.account_id)}
            self._sessions[token] = session
            self.repository.audit(account.username, "admin.login", account.account_id, {"address": address})
            return token, {"account": self.account_document(account), "csrf": session["csrf"]}

    def authenticate(self, token):
        with self._lock:
            session = self._sessions.get(token)
            if session is None or session["expires_at"] <= time.time():
                self._sessions.pop(token, None)
                raise AdminError(401, "sign in with an administrator account")
            try:
                account = self.registry.account_by_id(session["account_id"])
                allowed = self.is_admin(account) and hmac.compare_digest(
                    self.repository.credential_stamp(account.account_id) or "", session["stamp"])
            except AccountRegistryError:
                allowed = False
            if not allowed:
                self._sessions.pop(token, None)
                raise AdminError(401, "administrator session is no longer valid")
            return account, session

    def invalidate(self, account_id):
        with self._lock:
            self._sessions = {key: value for key, value in self._sessions.items()
                              if value["account_id"] != account_id}

    def online_characters(self):
        result = {}
        app = self.application
        if app is not None:
            for server in (app.login, app.characters):
                for session in server.session_snapshot():
                    character = session.gameplay_character
                    if character is not None and session.connected and session.gameplay_mode:
                        if not session.gameplay_presence_departed:
                            result[int(character.character_id)] = session.gameplay_map
        return result

    def status(self):
        services = []
        app = self.application
        if app is not None:
            for service in app.services:
                threads = getattr(service, "_threads", ())
                services.append({"name": service.service_name, "port": service.port,
                                 "running": bool(threads) and all(t.is_alive() for t in threads),
                                 "connections": len(service.session_snapshot())})
            if app.accounts is not None:
                services.append({"name": "Account HTTP", "port": app.accounts.port,
                                 "running": bool(app.accounts._thread and app.accounts._thread.is_alive())})
        services.append({"name": "Admin HTTP", "port": self.port,
                         "running": bool(self._thread and self._thread.is_alive())})
        with self.repository.lock:
            booking_counts = dict(self.repository.db.execute(
                "SELECT status,COUNT(*) FROM admin_bookings GROUP BY status"))
        return {"uptime_seconds": int(time.time() - self.started_at),
                "accounts": self.registry.count_accounts(), "characters": len(self.store.profiles()),
                "online_characters": len(self.online_characters()),
                "game_sessions": self.registry.count_active_sessions(),
                "bookings": booking_counts, "services": services,
                "warnings": list(self.store.warnings),
                "watchdog": app.stall_watchdog.statistics() if app is not None else None}

    def character_summary(self, profile, *, owner=None, online=None):
        online = self.online_characters() if online is None else online
        state = self.store.get(profile.character_id)
        return {"character_id": profile.character_id, "name": profile.name,
                "class_id": profile.class_id, "level": profile.level,
                "map_id": online.get(profile.character_id, state.map_id if state else profile.map_id),
                "online": profile.character_id in online, "owner": owner,
                "saved_at": state.saved_at if state else None, "has_state": state is not None}

    def get(self, path, query, account, session):
        search = str(query.get("q", ""))[:100].casefold()
        if path == "/api/me":
            return {"account": self.account_document(account), "csrf": session["csrf"]}
        if path == "/api/status":
            return self.status()
        if path == "/api/accounts":
            return page([self.account_document(a) for a in self.registry.accounts()
                         if search in a.username.casefold() or search in str(a.account_id)], query)
        if path.startswith("/api/accounts/"):
            target = self.registry.account_by_id(int(path.rsplit("/", 1)[1]))
            online = self.online_characters()
            characters = [self.character_summary(p, owner=target.username, online=online)
                          for cid in target.character_ids if (p := self.store.get_profile(cid)) is not None]
            return {"account": self.account_document(target), "characters": characters,
                    "missing_character_ids": [cid for cid in target.character_ids
                                              if self.store.get_profile(cid) is None]}
        if path == "/api/characters":
            owners, online = self.registry.claimed_characters(), self.online_characters()
            owner_filter = None
            if query.get("account_id"):
                owner_filter = set(self.registry.account_by_id(int(query["account_id"])).character_ids)
            matches = [p for p in self.store.profiles()
                       if (owner_filter is None or p.character_id in owner_filter)
                       and (search in p.name.casefold() or search in str(p.character_id)
                            or search in str(owners.get(p.character_id, "")).casefold())]
            result = page(matches, query)
            result["rows"] = [self.character_summary(p, owner=owners.get(p.character_id), online=online)
                              for p in result["rows"]]
            return result
        if path == "/api/maps":
            return page([m for m in map_catalog(self.static_database_path)
                         if search in m["map_id"].casefold() or search in m["type"].casefold()], query)
        if path.startswith("/api/characters/") and path.endswith("/live"):
            cid = int(path.split("/")[-2])
            if self.store.get_profile(cid) is None:
                raise AdminError(404, "character not found")
            return self.live.inspect(cid)
        if path.startswith("/api/characters/"):
            cid = int(path.rsplit("/", 1)[1])
            profile = self.store.get_profile(cid)
            if profile is None:
                raise AdminError(404, "character not found")
            state = self.store.get(cid)
            owner = self.registry.character_owner(cid)
            return {"profile": asdict(profile), "owner": owner.username if owner else None,
                    "account": self.account_document(owner) if owner else None,
                    "online": cid in self.online_characters(),
                    "state": _state_to_document(state) if state is not None else None}
        if path.startswith("/api/bookings/"):
            result = self.repository.booking(int(path.rsplit("/", 1)[1]))
            result["account"] = self.account_document(self.registry.account_by_id(result["account_id"]))
            profile = self.store.get_profile(result["character_id"])
            result["character"] = asdict(profile) if profile else None
            return result
        if path.startswith("/api/audit/"):
            with self.repository.lock:
                row = self.repository.db.execute("SELECT * FROM admin_audit WHERE id=?",
                                                  (int(path.rsplit("/", 1)[1]),)).fetchone()
                if row is None:
                    raise AdminError(404, "audit entry not found")
                return {**dict(row), "details": json.loads(row["details"])}
        if path in {"/api/bookings", "/api/audit"}:
            offset, limit = paging(query)
            return self.repository.history("admin_" + path.rsplit("/", 1)[1],
                                           offset=offset, limit=limit, query=search,
                                           account_id=query.get("account_id"), character_id=query.get("character_id"))
        if path == "/api/event-templates":
            return page([row for row in self.events.templates()
                         if search in row["template_id"].casefold() or search in str(row["name"] or "").casefold()], query)
        if path == "/api/events":
            listing = self.events.listing()
            state = query.get("state", "")
            if state not in {"", "active", "upcoming", "expired"}:
                raise ValueError("invalid event state filter")
            rows = [row for row in listing["rows"] if (not state or row["state"] == state)
                    and (search in row["template_id"].casefold() or search in row["name"].casefold()
                         or search in str(row["event_id"]))]
            return {**page(rows, query), "revision": listing["revision"], "counts": listing["counts"]}
        if path.startswith("/api/events/"):
            return self.events.detail(int(path.rsplit("/", 1)[1]))
        if path == "/api/currencies":
            return {"currencies": list(CURRENCIES)}
        if path.startswith("/api/items/"):
            return item_metadata(self.static_database_path, unquote(path[len("/api/items/"):]))
        if path == "/api/items":
            offset, limit = paging(query)
            order = {
                "template": "Id ASC",
                "category": "ItemCategory COLLATE NOCASE ASC, Id ASC",
                "category_desc": "ItemCategory COLLATE NOCASE DESC, Id ASC",
            }.get(query.get("sort", "template"))
            if order is None:
                raise ValueError("invalid item catalog sort order")
            uri = self.static_database_path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as database:
                database.row_factory = sqlite3.Row
                clause = " WHERE Id LIKE ?"
                args = (f"%{search}%",)
                total = database.execute("SELECT COUNT(*) FROM _Template_Item" + clause, args).fetchone()[0]
                rows = [dict(row) for row in database.execute(
                    "SELECT Id AS template_id,ItemCategory AS category,DefaultRarity AS rarity,"
                    "RequiredLevel AS required_level,MaxStackSize AS max_stack_size,SlotType AS slot_types "
                    "FROM _Template_Item" + clause + " ORDER BY " + order + " LIMIT ? OFFSET ?",
                    (*args, limit, offset))]
                return {"rows": rows, "total": total, "offset": offset, "limit": limit}
        if path == "/api/sessions":
            # Do not expose game bearer tokens in the CMS.
            return page([{"account_id": s.account_id, "username": s.username,
                          "issued_at": s.issued_at, "expires_at": s.expires_at,
                          "client_ip": s.client_ip} for s in self.registry.active_sessions()
                         if (not query.get("account_id") or s.account_id == int(query["account_id"]))
                         and (search in s.username.casefold() or search in str(s.account_id))], query)
        raise AdminError(404, "page not found")

    def post(self, path, payload, account, token):
        if path == "/api/logout":
            with self._lock:
                self._sessions.pop(token, None)
            return {"ok": True}
        reason = reason_from(payload)
        if path == "/api/events" or path.startswith("/api/events/"):
            try:
                if path == "/api/events":
                    return self.events.change(account.username, reason, payload)
                parts = path.strip("/").split("/")
                if len(parts) != 4 or parts[3] not in {"activate", "deactivate", "schedule"}:
                    raise AdminError(404, "event action not found")
                return self.events.change(account.username, reason, payload,
                    event_id=int(parts[2]), enabled=parts[3] != "deactivate", reschedule=parts[3] == "schedule")
            except EventScheduleConflict as exc:
                raise AdminError(409, str(exc)) from exc
        if path.startswith("/api/accounts/"):
            parts = path.strip("/").split("/")
            if len(parts) not in {3, 4}:
                raise AdminError(404, "action not found")
            account_id = int(parts[2])
            if len(parts) == 4 and parts[3] == "revoke":
                self.repository.revoke_sessions(account.username, account_id, reason)
            elif len(parts) == 3:
                changes = payload.get("changes")
                if not isinstance(changes, dict):
                    raise ValueError("changes must be an object")
                if account_id == account.account_id and (changes.get("disabled") is True or changes.get("admin") is False):
                    raise ValueError("you cannot disable or demote your own account")
                self.repository.update_account(account.username, account_id, changes, reason)
            else:
                raise AdminError(404, "action not found")
            self.invalidate(account_id)
            return {"ok": True}
        if path.startswith("/api/characters/"):
            parts = path.strip("/").split("/")
            if len(parts) != 4:
                raise AdminError(404, "action not found")
            cid = int(parts[2])
            if self.store.get_profile(cid) is None or self.registry.character_owner(cid) is None:
                raise AdminError(404, "owned character not found")
            return self.live.action(cid, parts[3], payload, account.username, reason)
        if path == "/api/bookings":
            cid = payload.get("character_id")
            if type(cid) is not int or cid <= 0:
                raise ValueError("character ID must be a positive integer")
            owner = self.registry.character_owner(cid)
            if owner is None or self.store.get_profile(cid) is None:
                raise ValueError("character must exist and belong to an account")
            if self.store.get(cid) is None:
                raise ValueError("character must enter the game once before receiving bookings")
            kind, resource, amount = payload.get("kind"), payload.get("resource"), payload.get("amount")
            if not isinstance(resource, str) or len(resource) > 200:
                raise ValueError("invalid resource")
            if payload.get("account_id") is not None and payload["account_id"] != owner.account_id:
                raise ValueError("the character does not belong to the selected account")
            options = self.delivery.validate(kind, resource, amount, payload.get("options"),
                                             level=self.store.get_profile(cid).level, class_id=self.store.get_profile(cid).class_id)
            request_id = payload.get("request_id", "")
            if not isinstance(request_id, str) or not 16 <= len(request_id) <= 100:
                raise ValueError("a request ID of 16 to 100 characters is required")
            booking = self.repository.create_booking(account.username, owner.account_id, cid,
                                                     kind, resource, amount, reason, request_id, options)
            return self.live.deliver(booking["id"])
        if path.startswith("/api/bookings/") and path.endswith("/deliver"):
            booking_id = int(path.split("/")[-2])
            self.repository.audit(account.username, "booking.retry", booking_id, {"reason": reason})
            return self.live.deliver(booking_id)
        if path.startswith("/api/bookings/") and path.endswith("/cancel"):
            booking_id = int(path.split("/")[-2])
            self.delivery.cancel(account.username, booking_id, reason)
            return {"ok": True}
        raise AdminError(404, "action not found")

    def start(self):
        if self._httpd is not None:
            return
        from dso_server.services.admin_http import AdminHTTPServer, AdminRequestHandler
        httpd = AdminHTTPServer((self.host, self.port), AdminRequestHandler)
        httpd.service = self
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self.started_at = time.time()
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        kwargs={"poll_interval": 0.2}, name="admin-cms", daemon=True)
        self.live.start()
        self.events.start()
        self._thread.start()

    def stop(self):
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self.events.stop()
        self.live.stop()
        with self._lock:
            self._sessions.clear()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Grant/revoke CMS access for an existing game account.")
    parser.add_argument("--registry", default=os.getenv("DSO_ACCOUNT_REGISTRY", str(DEFAULT_ACCOUNT_DATABASE_PATH)))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--grant-admin", metavar="USERNAME")
    group.add_argument("--revoke-admin", metavar="USERNAME")
    args = parser.parse_args(argv)
    registry = AccountRegistry(args.registry)
    try:
        account = registry.account(args.grant_admin or args.revoke_admin)
        AdminRepository(registry).update_account("local operator", account.account_id,
            {"admin": bool(args.grant_admin)}, "local operator role assignment")
        print(f"CMS access {'granted' if args.grant_admin else 'revoked'} for {account.username}.")
    finally:
        registry.close()


if __name__ == "__main__":
    main()
