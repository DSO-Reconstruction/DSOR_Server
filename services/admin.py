"""Admin CMS domain operations and lifecycle; HTTP transport lives in admin_http."""

import argparse
from contextlib import closing
from dataclasses import asdict, replace
import hmac
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import unquote
import secrets
import threading
import time
from datetime import datetime, timezone

from dso_server.paths import DEFAULT_ACCOUNT_DATABASE_PATH, DEFAULT_QUESTS_PATH
from dso_server.persistence.account_registry import AccountRegistry, AccountRegistryError
from dso_server.persistence.account_state import _state_to_document
from dso_server.gameplay.quest_content import (
    QUEST_STATE_ACCEPTED, QUEST_STATE_CLOSED, QUEST_STATE_FINISHED,
    QuestProgress, load_quest_catalog,
)
from dso_server.gameplay.achievement_content import (
    AchievementProgress, complete_achievement, load_achievement_catalog,
)
from dso_server.persistence.admin_registry import AdminRepository
from dso_server.persistence.npc_shop_overrides import NpcShopOverrideStore
from dso_server.protocol.character_selection import CLASS_NAMES
from dso_server.protocol.inventory_codec import (
    INVENTORY_INFO_COMMAND, clone_inventory_info, clone_item_info,
    free_inventory_location, owned_inventory_items,
)
from dso_server.protocol.player_init_protocol import (
    CHAPTER_PROGRESS_COMMAND, ServerCommand, encode_server_command_batch,
)
from dso_server.protocol.quest_protocol import QUEST_UPDATE_COMMAND, QuestUpdate
from dso_server.runtime.admin_commands import call_admin_command
from dso_server.services.accounts import FailureThrottle
from dso_server.services.admin_bookings import BookingDelivery, CURRENCIES
from dso_server.services.admin_items import item_catalog, item_metadata
from dso_server.services.admin_live import (
    LiveAdministration, map_catalog, ready, state_token,
)
from dso_server.services.admin_events import EventAdministration, EventScheduleConflict
from dso_server.services.admin_season_pass import SeasonPassAdministration, SeasonPassConflict
from dso_server.services.player_persistence import PlayerPersistenceService
from dso_server.services.player_progression import player_level_range, player_level_xp


DEFAULT_ADMIN_PORT = 8081
# The NewPlayer administrator flags stored on a character profile.
GAME_PRIVILEGES = frozenset({"admin_privileges", "admin_name_color"})
IDENTITY_FIELDS = frozenset({"name", "level"})
# CharacterGeneration rejects exactly these names, so a CMS rename must not
# produce one the client would refuse the next time the character is created.
CHARACTER_NAME_MAX_BYTES = 32


def character_name(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("character name must not be empty or padded")
    if any(ord(character) < 0x20 for character in value):
        raise ValueError("character name cannot contain control characters")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("character name contains characters the client cannot encode")
    if len(encoded) > CHARACTER_NAME_MAX_BYTES:
        raise ValueError(f"character name must be at most {CHARACTER_NAME_MAX_BYTES} bytes")
    return value


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
        self.npc_shop_overrides = NpcShopOverrideStore(static_database_path)
        self.delivery = BookingDelivery(self.repository, store, static_database_path)
        self.static_database_path = Path(static_database_path)
        self.host, self.port = host, int(port)
        self.application = application
        self.live = LiveAdministration(self)
        self.events = EventAdministration(self)
        self.season_pass = SeasonPassAdministration(self)
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

    def _refresh_open_npc_shops(self, shop_id):
        """Refresh cached offers for players currently viewing this shop."""
        app = self.application
        if app is None:
            return 0
        refreshed = 0
        for server in (app.characters, app.login):
            for player_session in server.session_snapshot():
                with player_session.flow_lock:
                    open_shop_id = player_session.gameplay_open_shop_id
                if open_shop_id != shop_id:
                    continue
                try:
                    refreshed += bool(call_admin_command(server.admin_commands,
                        lambda server=server, player_session=player_session:
                            server._refresh_open_npc_shop(player_session, shop_id),
                        session=player_session,
                    ))
                except Exception as exc:
                    # The durable CMS write is already committed. A player who
                    # is disconnecting or whose game service is stopping can
                    # reopen the shop to receive the saved catalog.
                    print(f"[Admin] could not refresh NPC shop {shop_id!r}: {exc}")
        return refreshed

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

    def set_privileges(self, cid, changes, account, reason):
        """Grant or revoke the NewPlayer administrator flags for one character.

        The roster record is what every account save rebuilds the profile from,
        so writing only the profile would be undone by the next autosave.
        """
        if not isinstance(changes, dict) or not changes:
            raise ValueError("choose at least one privilege to change")
        if set(changes) - GAME_PRIVILEGES:
            raise ValueError("choose admin_privileges or admin_name_color")
        for key, value in changes.items():
            if type(value) is not bool:
                raise ValueError(f"{key} must be a boolean")
        owner = self.registry.character_owner(cid)
        if (changes.get("admin_privileges") is False and owner is not None
                and owner.account_id == account.account_id
                and self.repository.role(account.account_id) is None):
            raise ValueError("your CMS access comes from this character's game administrator "
                             "privileges; grant an explicit CMS role first")
        application, chat_sessions, gameplay_sessions = self.application, 0, 0
        roster = False
        if application is not None:
            service = application.characters
            with service.character_roster_lock:
                for character in service.character_selection.characters:
                    if int(character.character_id) == cid:
                        roster = True
                        for key, value in changes.items():
                            setattr(character, key, value)
        profile = replace(self.store.get_profile(cid), **changes)
        self.store.put_profile(profile, flush=True)
        if application is not None:
            chat = getattr(application, "chat", None)
            if chat is not None:
                chat_sessions = chat.refresh_identity(cid)
            for server in (application.login, application.characters):
                for session in server.session_snapshot():
                    character = session.gameplay_character
                    if character is None or int(character.character_id) != cid:
                        continue
                    gameplay_sessions += bool(self._update_session_character(
                        server, session, cid, changes,
                    ))
        details = {**changes, "reason": reason, "roster": roster,
                   "chat_sessions": chat_sessions, "gameplay_sessions": gameplay_sessions}
        self.repository.audit(account.username, "character.privileges", cid, details)
        return {"ok": True, "chat_sessions": chat_sessions, "gameplay_sessions": gameplay_sessions}

    def level_range(self, class_id):
        """The levels this class can hold, or None when it has no authored XP table."""
        try:
            return player_level_range(self.static_database_path, CLASS_NAMES.get(class_id, ""))
        except ValueError:
            return None

    def wisdom_level_range(self):
        with closing(sqlite3.connect(
                self.static_database_path.resolve().as_uri() + "?mode=ro", uri=True)) as database:
            row = database.execute(
                "SELECT MIN(Level), MAX(Level) FROM _Template_TalentXPLevel "
                "WHERE TalentCategory='Knowledge'").fetchone()
        if row is None or row[0] is None or row[1] is None:
            raise ValueError("Wisdom progression is not available in the static database")
        return int(row[0]), int(row[1])

    def wisdom_xp_for_level(self, level):
        with closing(sqlite3.connect(
                self.static_database_path.resolve().as_uri() + "?mode=ro", uri=True)) as database:
            row = database.execute(
                "SELECT XP FROM _Template_TalentXPLevel "
                "WHERE TalentCategory='Knowledge' AND Level=?", (level,)).fetchone()
        if row is None:
            raise ValueError(f"Wisdom level {level} is not defined")
        return int(row[0])

    def wisdom_progression_level(self, experience):
        with closing(sqlite3.connect(
                self.static_database_path.resolve().as_uri() + "?mode=ro", uri=True)) as database:
            row = database.execute(
                "SELECT MAX(Level) FROM _Template_TalentXPLevel "
                "WHERE TalentCategory='Knowledge' AND XP<=?", (max(0, int(experience)),)).fetchone()
        return max(0, int(row[0] or 0))

    def set_wisdom_level(self, cid, level, account, reason):
        if type(level) is not int:
            raise ValueError("Wisdom level must be a whole number")
        lowest, highest = self.wisdom_level_range()
        if not lowest <= level <= highest:
            raise ValueError(f"Wisdom level must be between {lowest} and {highest}")
        experience = self.wisdom_xp_for_level(level)
        state = self.store.get(cid)
        if state is None:
            raise ValueError("This character must enter the game once before Wisdom can be changed")
        points = tuple(state.talent_category_points)
        if len(points) != 4:
            raise ValueError("This character has no saved talent progression")
        online = cid in self.online_characters()
        target = self.live.find(cid) if online else None
        if online and target is None:
            raise ValueError("Character is changing maps or initializing; try again shortly")
        current_xp = points[1]
        if target is not None:
            with target[1].flow_lock:
                live_points = tuple(target[1].gameplay_talent_category_points)
            if len(live_points) != 4:
                raise ValueError("Character has no active talent progression")
            current_xp = live_points[1]
        current_level = self.wisdom_progression_level(current_xp)
        if level == current_level:
            raise ValueError("Character already has that Wisdom Level")
        if target is not None:
            result = self.live.set_wisdom_level(cid, experience)
            if not result:
                raise ValueError("Character session changed; refresh and try again")
            self.store.flush()
        else:
            updated = (int(points[0]), experience, int(points[2]), int(points[3]))
            self.store.put(replace(state, talent_category_points=updated), flush=True)
        details = {"from_level": current_level, "to_level": level,
                   "experience": experience, "reason": reason,
                   "online": target is not None}
        self.repository.audit(account.username, "character.wisdom", cid, details)
        return {"ok": True, "wisdom_level": level, "gameplay_sessions": int(target is not None)}

    def change_honor_points(self, cid, mode, amount, account, reason):
        """Set or increase a character's career PvP honor total."""
        if mode not in {"set", "increase"}:
            raise ValueError("choose whether to set or increase honor points")
        if type(amount) is not int or amount < 0:
            raise ValueError("honor points must be a non-negative whole number")
        if amount > 0xFFFFFFFF:
            raise ValueError("honor points cannot exceed 4,294,967,295")
        state = self.store.get(cid)
        if state is None:
            raise ValueError("This character must enter the game once before honor can be changed")
        online = cid in self.online_characters()
        target = self.live.find(cid) if online else None
        if online and target is None:
            raise ValueError("Character is changing maps or initializing; try again shortly")
        current = max(0, int(state.honor_points))
        if target is not None:
            with target[1].flow_lock:
                current = max(0, int(target[1].gameplay_honor_points))
        updated = amount if mode == "set" else current + amount
        if updated > 0xFFFFFFFF:
            raise ValueError("resulting honor points cannot exceed 4,294,967,295")
        if updated == current:
            raise ValueError("honor points already have that value")
        if target is not None:
            if not self.live.set_honor_points(cid, updated):
                raise ValueError("Character session changed; refresh and try again")
            self.store.flush()
        else:
            self.store.put(replace(state, honor_points=updated), flush=True)
        self.repository.audit(account.username, "character.honor_points", cid, {
            "mode": mode, "from": current, "to": updated,
            "amount": amount, "reason": reason, "online": target is not None,
        })
        return {"ok": True, "honor_points": updated,
                "gameplay_sessions": int(target is not None)}

    def set_identity(self, cid, changes, account, reason):
        """Rename a character or move it to another level.

        The roster record is what every account save rebuilds the profile from,
        and an online session holds its own copy whose level is carried back
        onto that record on the next autosave, so the profile, the roster and
        the live copies all have to change together.
        """
        if not isinstance(changes, dict) or not changes or set(changes) - IDENTITY_FIELDS:
            raise ValueError("choose a new name, a new level, or both")
        profile = self.store.get_profile(cid)
        class_name = CLASS_NAMES.get(profile.class_id, "")
        values = {}
        if "name" in changes:
            values["name"] = character_name(changes["name"])
        if "level" in changes:
            bounds = self.level_range(profile.class_id)
            if type(changes["level"]) is not int:
                raise ValueError("level must be a whole number")
            if bounds is None:
                raise ValueError("this class has no authored levels to choose from")
            lowest, highest = bounds
            if not lowest <= changes["level"] <= highest:
                raise ValueError(f"level must be between {lowest} and {highest} for this class")
            values["level"] = changes["level"]
        saved = dict(values)
        if "level" in values:
            saved["progression_u32"] = player_level_xp(self.static_database_path,
                                                       class_name, values["level"])[0]
        application, roster, chat_sessions, gameplay_sessions = self.application, False, 0, 0
        if "name" in values and any(other.character_id != cid
                                    and other.name.casefold() == values["name"].casefold()
                                    for other in self.store.profiles()):
            raise ValueError("another character already uses that name")
        if application is not None:
            service = application.characters
            with service.character_roster_lock:
                if "name" in values and any(
                        character.name.casefold() == values["name"].casefold()
                        and int(character.character_id) != cid
                        for character in service.character_selection.characters):
                    raise ValueError("another character already uses that name")
                for character in service.character_selection.characters:
                    if int(character.character_id) != cid:
                        continue
                    roster = True
                    for key, value in values.items():
                        setattr(character, key, value)
                    if "level" in values:
                        character.progression_u32 = saved["progression_u32"]
                    service.character_roster_revision += 1
            if roster:
                service._reencode_character_selection()
        self.store.put_profile(replace(profile, **saved), flush=True)
        if application is not None:
            chat = getattr(application, "chat", None)
            if chat is not None:
                chat_sessions = chat.refresh_identity(cid)
            for server in (application.login, application.characters):
                for session in server.session_snapshot():
                    character = session.gameplay_character
                    if character is None or int(character.character_id) != cid:
                        continue
                    gameplay_sessions += bool(self._update_session_character(
                        server, session, cid, values,
                        progression_xp=saved.get("progression_u32"),
                    ))
            if "level" in values and values["level"] != profile.level:
                self.live.refresh_progress(cid, previous_level=profile.level)
        details = {**values, "reason": reason, "experience": saved.get("progression_u32"),
                   "roster": roster, "chat_sessions": chat_sessions,
                   "gameplay_sessions": gameplay_sessions}
        self.repository.audit(account.username, "character.identity", cid, details)
        return {"ok": True, "chat_sessions": chat_sessions, "gameplay_sessions": gameplay_sessions}

    @staticmethod
    def _update_session_character(server, session, cid, changes, *, progression_xp=None):
        """Serialize live identity/privilege publication with gameplay commands."""
        def commit():
            if not server.is_session_current(session):
                return False
            with session.flow_lock:
                character = session.gameplay_character
                if character is None or int(character.character_id) != cid:
                    return False
                for key, value in changes.items():
                    setattr(character, key, value)
                if progression_xp is not None:
                    character.progression_u32 = progression_xp
                    points = tuple(session.gameplay_talent_category_points)
                    if len(points) == 4:
                        # XP grants compare both totals; update them together so
                        # a lowered level cannot inherit the previous XP floor.
                        session.gameplay_talent_category_points = (
                            progression_xp, points[1], points[2], points[3],
                        )
            return True

        return call_admin_command(server.admin_commands, commit, session=session)

    def copy_character_inventory(self, source_cid, target_cid, actor, reason):
        """Append copies of every owned item to a fully loaded live character."""

        if type(target_cid) is not int or target_cid <= 0:
            raise ValueError("target character ID must be a positive integer")
        if int(source_cid) == target_cid:
            raise ValueError("source and target characters must be different")
        source_profile = self.store.get_profile(source_cid)
        target_profile = self.store.get_profile(target_cid)
        if (source_profile is None or target_profile is None
                or self.registry.character_owner(source_cid) is None
                or self.registry.character_owner(target_cid) is None):
            raise ValueError("source and target must be owned characters")
        source_live = self.live.find(source_cid)
        if source_cid in self.online_characters() and source_live is None:
            raise ValueError("source character is online but not fully loaded into a map")
        if source_live is not None:
            with source_live[1].flow_lock:
                if getattr(source_live[1], "gameplay_inventory_rewards_in_progress", 0):
                    raise ValueError("source inventory is being updated; try again shortly")
            source = PlayerPersistenceService._capture_account_state(
                source_live[0], source_live[1]
            )
        else:
            source = self.store.get(source_cid)
        if source is None:
            raise ValueError("source character must have saved gameplay state")

        target_live = self.live.find(target_cid)
        if target_live is None:
            raise ValueError("target character must be online and fully loaded into a map")
        target_server, target_session = target_live
        with target_session.flow_lock:
            if getattr(target_session, "gameplay_inventory_rewards_in_progress", 0):
                raise ValueError("target inventory is being updated; try again shortly")
            target_token = state_token(target_session)
        target = PlayerPersistenceService._capture_account_state(
            target_server, target_session
        )
        if target is None:
            raise ValueError("target character state is not ready")
        with target_session.flow_lock:
            if target_token != state_token(target_session):
                raise ValueError("target inventory changed; try the copy again")

        source_inventory = source.inventory
        inventory = clone_inventory_info(target.inventory)
        existing_ids = {
            int(item.item_id) for item in owned_inventory_items(inventory)
        }
        next_item_id = max(
            int(target.next_item_id), max(existing_ids, default=0) + 1
        )
        source_banks = {}
        for name in ("storage_a8", "storage_d0"):
            for item_id, slot in getattr(source_inventory, name).items():
                source_banks[int(item_id)] = (name, int(slot))
        source_ingredients = set(map(int, source_inventory.field_180))

        def bank_slot(name, item_id):
            capacity = (inventory.bank_storage_size if name == "storage_a8"
                        else inventory.extra_bank_storage_size)
            slots = getattr(inventory, name)
            used = set(map(int, slots.values()))
            for slot in range(max(0, int(capacity))):
                if slot not in used:
                    slots[item_id] = slot
                    return True
            return False

        def bag_slot(item_id):
            location = free_inventory_location(inventory)
            if location is None:
                return False
            name, slot = location
            getattr(inventory, name)[item_id] = slot
            return True

        def place_in_bank(item_id):
            return bank_slot("storage_a8", item_id) or bank_slot(
                "storage_d0", item_id
            )

        copied = 0
        copied_ids = set()
        for original in owned_inventory_items(source_inventory):
            if next_item_id > 0xFFFFFFFF:
                raise ValueError("target has no available item IDs")
            duplicate = clone_item_info(original)
            old_id = int(original.item_id)
            duplicate.item_id = next_item_id
            next_item_id += 1
            new_id = duplicate.item_id
            copied_ids.add(new_id)
            inventory.items.append(duplicate)

            if old_id in source_ingredients:
                inventory.field_180.append(new_id)
            else:
                old_bank = source_banks.get(old_id)
                placed = (
                    bank_slot(old_bank[0], new_id) if old_bank else False
                )
                if not placed:
                    placed = bag_slot(new_id) or place_in_bank(new_id)
                if not placed:
                    raise ValueError(
                        "target has no free inventory or bank slots for all copied items"
                    )
            copied += 1

        if not copied:
            raise ValueError("source character has no items to copy")
        inventory.changed_item_ids = []
        inventory.field_1e0 = False
        delta = clone_inventory_info(inventory)
        delta.items = [item for item in delta.items if item.item_id in copied_ids]
        delta.changed_item_ids = []
        delta.field_1e0 = True
        final = clone_inventory_info(inventory)
        final.items = []
        final.changed_item_ids = []
        final.field_158 = {}
        final.field_1e0 = False
        packet, packet_bits = encode_server_command_batch((
            ServerCommand(INVENTORY_INFO_COMMAND, delta, 1),
            ServerCommand(INVENTORY_INFO_COMMAND, final, 1),
        ), include_terminator=True)

        def commit():
            if not target_server.is_session_current(target_session):
                raise ValueError("target session changed; refresh and retry")
            with target_session.flow_lock:
                if not ready(target_session, target_cid):
                    raise ValueError("target must remain fully loaded in a map")
                if getattr(target_session, "gameplay_inventory_rewards_in_progress", 0):
                    raise ValueError("target inventory is being updated; try again shortly")
                if target_token != state_token(target_session):
                    return False
                target_session.gameplay_inventory = inventory
                target_session.gameplay_next_item_id = next_item_id
            PlayerPersistenceService._persist_gameplay_session(
                target_server, target_session, reason="CMS inventory copy"
            )
            target_session.send_reliable_ordered_split(
                packet, total_bit_length=packet_bits,
                label_prefix="admin-inventory-copy",
            )
            return True

        if not call_admin_command(target_server.admin_commands, commit, session=target_session):
            raise ValueError("target inventory changed; try the copy again")
        self.store.flush()
        self.repository.audit(actor.username, "character.inventory.copy", target_cid, {
            "source_character_id": source_cid,
            "source_name": source_profile.name,
            "target_name": target_profile.name,
            "items_copied": copied,
            "reason": reason,
        })
        return {"ok": True, "items_copied": copied,
                "source_name": source_profile.name,
                "target_name": target_profile.name}

    def complete_character_main_quests(self, cid, actor, reason):
        profile = self.store.get_profile(cid)
        owner = self.registry.character_owner(cid)
        if profile is None or owner is None:
            raise ValueError("character must exist and belong to an account")
        catalog = load_quest_catalog(DEFAULT_QUESTS_PATH)
        class_name = CLASS_NAMES.get(int(profile.class_id), "")
        quests = list(
            quest for quest in catalog.quests.values()
            if quest.quest_type.casefold() == "world" and quest.accepts_class(class_name)
        )
        # Ammon's first Parallel Worlds access quest is a chapter prerequisite
        # authored as Heroic, so it is not part of the World quest type above.
        parallel_world_intro = catalog.quest("pw000-introduction-01-n")
        if parallel_world_intro is not None and parallel_world_intro not in quests:
            quests.append(parallel_world_intro)
        quests = tuple(quests)
        if not quests:
            raise ValueError("no main story quests are available for this character")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        online = cid in self.online_characters()
        target = self.live.find(cid) if online else None
        if online and target is None:
            raise ValueError("character is changing maps or initializing; try again shortly")

        completed = 0
        if target is None:
            state = self.store.get(cid)
            if state is None:
                raise ValueError("character must enter the game once before quests can be changed")
            quest_log = dict(state.quest_log or {})
            for quest in quests:
                previous = quest_log.get(quest.quest_id, {})
                if int(previous.get("state", -1)) != QUEST_STATE_CLOSED:
                    completed += 1
                finished_mask = sum(1 << task.index for task in quest.tasks)
                quest_log[quest.quest_id] = QuestProgress(
                    quest.quest_id, state=QUEST_STATE_CLOSED,
                    finished_mask=finished_mask, completed_at=stamp,
                ).to_document()
            from dataclasses import replace as replace_state
            self.store.put(replace_state(state, quest_log=quest_log), flush=True)
        else:
            server, session = target
            from dso_server.services.quests import _quest_log
            def commit():
                nonlocal completed
                if not server.is_session_current(session):
                    raise ValueError("character session changed; refresh and retry")
                with session.flow_lock:
                    if not ready(session, cid):
                        raise ValueError("character must remain fully loaded in a map")
                    log = _quest_log(session)
                    commands = []
                    for quest in quests:
                        progress = log.get(quest.quest_id)
                        if progress is None or progress.state != QUEST_STATE_CLOSED:
                            completed += 1
                        finished_mask = sum(1 << task.index for task in quest.tasks)
                        if progress is None:
                            progress = QuestProgress(quest.quest_id)
                            log[quest.quest_id] = progress
                        progress.state = QUEST_STATE_CLOSED
                        progress.finished_mask = finished_mask
                        progress.completed_at = stamp
                        progress.tokens.clear()
                        commands.append(ServerCommand(
                            QUEST_UPDATE_COMMAND,
                            QuestUpdate.quest_state(quest.quest_id, QUEST_STATE_CLOSED, stamp),
                            1,
                        ))
                    commands.extend(server._quest_npc_info_commands(session))
                    commands.append(ServerCommand(
                        CHAPTER_PROGRESS_COMMAND,
                        server._quest_chapter_progress(session), 1,
                    ))
                PlayerPersistenceService._persist_gameplay_session(
                    server, session, reason="CMS main quests completed", flush=True
                )
                server._send_quest_commands(
                    session, commands, label="admin-main-quests-completed"
                )
                return True
            if not call_admin_command(server.admin_commands, commit, session=session):
                raise ValueError("character session changed; refresh and retry")
        self.repository.audit(actor.username, "character.quests.complete_main", cid, {
            "quest_count": len(quests), "newly_completed": completed,
            "online": target is not None, "reason": reason,
        })
        return {"ok": True, "quest_count": len(quests),
                "newly_completed": completed, "online": target is not None}

    def unlock_character_all_recipes(self, cid, actor, reason):
        if self.registry.character_owner(cid) is None or self.store.get_profile(cid) is None:
            raise ValueError("owned character not found")
        state = self.store.get(cid)
        if state is None:
            raise ValueError("character must enter the game once before recipes can be changed")
        if cid in self.online_characters():
            raise ValueError("character must be offline; recipes will appear at next login")
        # The normal gameplay catalogue deliberately contains live recipes
        # only. CMS "all" must also include the legacy jewel crafting
        # schematic, which is retained in the recipe table with IsLive=0.
        with closing(sqlite3.connect(
            f"file:{self.static_database_path.as_posix()}?mode=ro", uri=True,
        )) as database:
            all_resources = {
                str(row[0]) for row in database.execute(
                    "SELECT DISTINCT RecipeItemReference "
                    "FROM _Template_CraftingRecipes "
                    "WHERE RecipeItemReference IS NOT NULL "
                    "AND RecipeItemReference<>''"
                )
            }
            recipe_achievement_ids = {
                str(row[0]).casefold() for row in database.execute(
                    "SELECT DISTINCT AchievementReference "
                    "FROM _Template_CraftingRecipes "
                    "WHERE AchievementReference IS NOT NULL "
                    "AND AchievementReference<>''"
                )
            }
        previous = set(state.recipe_book_resources)
        resources = tuple(sorted(previous | all_resources))
        achievements = {
            str(key).casefold(): AchievementProgress.from_document(value)
            for key, value in state.achievements.items()
            if isinstance(value, dict)
        }
        achievement_catalog = load_achievement_catalog(self.static_database_path)
        completed_achievements = 0
        for achievement_id in recipe_achievement_ids:
            definition = achievement_catalog.achievement(achievement_id)
            if definition is None:
                continue
            if complete_achievement(achievements, definition):
                completed_achievements += 1
        achievement_documents = {
            key: value.to_document() for key, value in achievements.items()
        }
        self.store.put(replace(
            state,
            recipe_book_resources=resources,
            achievements=achievement_documents,
        ), flush=True)
        newly_unlocked = len(set(resources) - previous)
        self.repository.audit(actor.username, "character.recipes.unlock_all", cid, {
            "newly_unlocked": newly_unlocked,
            "total_unlocked": len(resources),
            "achievements_completed": completed_achievements,
            "reason": reason,
        })
        return {"ok": True, "newly_unlocked": newly_unlocked,
                "total_unlocked": len(resources),
                "achievements_completed": completed_achievements}

    def complete_character_ammon_parallel_world_quests(self, cid, actor, reason):
        from dso_server.gameplay.map_unlocks import (
            map_unlock_catalog, quest_map_unlocks,
        )

        profile = self.store.get_profile(cid)
        owner = self.registry.character_owner(cid)
        if profile is None or owner is None:
            raise ValueError("character must exist and belong to an account")
        catalog = load_quest_catalog(DEFAULT_QUESTS_PATH)
        class_name = CLASS_NAMES.get(int(profile.class_id), "")
        quest_ids = {
            quest.quest_id.casefold()
            for quest in catalog.quests.values()
            if quest.quest_id.casefold().startswith("pw")
            and not quest.repeatable
            and "ammon" in f"{quest.giver} {quest.goal}".casefold()
        }
        # Ammon's hub doors and PW boss routes are gated by follow-up unlock
        # quests whose giver/goal is the regional quest NPC, not Ammon. Include
        # every non-repeatable quest explicitly named by a PW map access row.
        _travel_maps, map_accesses = map_unlock_catalog(
            str(self.static_database_path)
        )
        quest_ids.update(
            quest_id.casefold()
            for access_id, access_group, quest_id in map_accesses
            if quest_id
            and (access_id.casefold().startswith("pw")
                 or access_group.casefold().startswith("pw"))
        )
        quests = tuple(
            quest for quest in catalog.quests.values()
            if quest.quest_id.casefold() in quest_ids
            and not quest.repeatable
            and quest.accepts_class(class_name)
        )
        if not quests:
            raise ValueError("no Ammon Parallel Worlds quests are available")
        # Keep the unlock quest IDs for audit/UI reporting. The hub's entry
        # events use the persistent map grants to keep completed gates open.
        gate_quest_ids = {
            quest_id.casefold()
            for access_id, access_group, quest_id in map_accesses
            if quest_id
            and (access_id.casefold().startswith("pw")
                 or access_group.casefold().startswith("pw"))
        }
        gate_quest_ids.intersection_update(quest.quest_id.casefold() for quest in quests)
        unlocked_maps = quest_map_unlocks(
            str(self.static_database_path),
            (quest.quest_id for quest in quests),
        )
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        online = cid in self.online_characters()
        target = self.live.find(cid) if online else None
        if online and target is None:
            raise ValueError("character is changing maps or initializing; try again shortly")

        completed = 0
        if target is None:
            state = self.store.get(cid)
            if state is None:
                raise ValueError("character must enter the game once before quests can be changed")
            quest_log = dict(state.quest_log or {})
            for quest in quests:
                previous = quest_log.get(quest.quest_id, {})
                if int(previous.get("state", -1)) != QUEST_STATE_CLOSED:
                    completed += 1
                finished_mask = sum(1 << task.index for task in quest.tasks)
                quest_log[quest.quest_id] = QuestProgress(
                    quest.quest_id, state=QUEST_STATE_CLOSED,
                    finished_mask=finished_mask, completed_at=stamp,
                ).to_document()
            self.store.put(replace(
                state,
                quest_log=quest_log,
                unlocked_maps=tuple(sorted(
                    {*(state.unlocked_maps or ()), *unlocked_maps}
                )),
            ), flush=True)
        else:
            server, session = target
            from dso_server.services.quests import _quest_log
            def commit():
                nonlocal completed
                if not server.is_session_current(session):
                    raise ValueError("character session changed; refresh and retry")
                with session.flow_lock:
                    if not ready(session, cid):
                        raise ValueError("character must remain fully loaded in a map")
                    log = _quest_log(session)
                    commands = []
                    for quest in quests:
                        progress = log.get(quest.quest_id)
                        if progress is None or progress.state != QUEST_STATE_CLOSED:
                            completed += 1
                        finished_mask = sum(1 << task.index for task in quest.tasks)
                        if progress is None:
                            progress = QuestProgress(quest.quest_id)
                            log[quest.quest_id] = progress
                        progress.state = QUEST_STATE_CLOSED
                        progress.finished_mask = finished_mask
                        progress.completed_at = stamp
                        progress.tokens.clear()
                        commands.append(ServerCommand(
                            QUEST_UPDATE_COMMAND,
                            QuestUpdate.quest_state(quest.quest_id, QUEST_STATE_CLOSED, stamp),
                            1,
                        ))
                    commands.extend(server._quest_npc_info_commands(session))
                    commands.append(ServerCommand(
                        CHAPTER_PROGRESS_COMMAND,
                        server._quest_chapter_progress(session), 1,
                    ))
                for quest in quests:
                    server._quest_unlock_maps(session, quest.quest_id)
                server._quest_spawn_revealed_npcs(session)
                server._quest_refresh_map_phases(session)
                server._quest_sync_props(session)
                PlayerPersistenceService._persist_gameplay_session(
                    server, session, reason="CMS Ammon Parallel Worlds quests completed", flush=True
                )
                server._send_quest_commands(
                    session, commands, label="admin-ammon-pw-quests-completed"
                )
                return True
            if not call_admin_command(server.admin_commands, commit, session=session):
                raise ValueError("character session changed; refresh and retry")
        self.repository.audit(actor.username, "character.quests.complete_ammon_parallel_worlds", cid, {
            "quest_count": len(quests), "newly_completed": completed,
            "gate_quest_count": len(gate_quest_ids),
            "online": target is not None, "reason": reason,
        })
        return {"ok": True, "quest_count": len(quests),
                "newly_completed": completed,
                "gate_quest_count": len(gate_quest_ids),
                "online": target is not None}

    def complete_character_active_quests(self, cid, actor, reason):
        """Hand in every open quest through the regular turn-in path.

        _quest_turn_in grants the quest rewards and removes quest items, so
        this needs the live session; an offline save has no reward pipeline.
        """
        if self.store.get_profile(cid) is None or self.registry.character_owner(cid) is None:
            raise ValueError("character must exist and belong to an account")
        target = self.live.find(cid)
        if target is None:
            raise ValueError("character must be online and fully loaded into a map")
        server, session = target
        from dso_server.services.quests import _quest_log
        active, completed, failed = [], [], []

        def commit():
            if not server.is_session_current(session):
                raise ValueError("character session changed; refresh and retry")
            catalog = server._quest_catalog()
            with session.flow_lock:
                if not ready(session, cid):
                    raise ValueError("character must remain fully loaded in a map")
                for quest_id, progress in _quest_log(session).items():
                    if progress.state not in (QUEST_STATE_ACCEPTED, QUEST_STATE_FINISHED):
                        continue
                    quest = catalog.quest(quest_id)
                    if quest is None:
                        failed.append(quest_id)
                        continue
                    # Finish every task so the turn-in accepts the quest.
                    progress.finished_mask = sum(1 << task.index for task in quest.tasks)
                    active.append(quest.quest_id)
            if not active and not failed:
                raise ValueError("character has no active quests")
            # Snapshot only: follow-up quests opened by these turn-ins stay open.
            for quest_id in active:
                try:
                    closed = server._quest_turn_in(session, quest_id)
                except Exception:
                    closed = False
                (completed if closed else failed).append(quest_id)
            PlayerPersistenceService._persist_gameplay_session(
                server, session, reason="CMS active quests completed", flush=True
            )
            return True

        if not call_admin_command(server.admin_commands, commit, session=session):
            raise ValueError("character session changed; refresh and retry")
        self.repository.audit(actor.username, "character.quests.complete_active", cid, {
            "completed": completed, "failed": failed, "reason": reason,
        })
        return {"ok": True, "completed": len(completed), "failed": failed}

    def delete_character_item(self, cid, item_id, actor, reason):
        if type(item_id) is not int or item_id <= 0:
            raise ValueError("item ID must be a positive integer")
        profile = self.store.get_profile(cid)
        owner = self.registry.character_owner(cid)
        if profile is None or owner is None:
            raise ValueError("character must exist and belong to an account")
        online = cid in self.online_characters()
        target = self.live.find(cid) if online else None
        if online and target is None:
            raise ValueError("character is changing maps or initializing; try again shortly")

        if target is None:
            state = self.store.get(cid)
            if state is None:
                raise ValueError("character must enter the game once before inventory can be changed")
            inventory = clone_inventory_info(state.inventory)
        else:
            server, session = target
            with session.flow_lock:
                if not ready(session, cid):
                    raise ValueError("character must be fully loaded into a map")
                if getattr(session, "gameplay_inventory_rewards_in_progress", 0):
                    raise ValueError("character inventory is being updated; try again shortly")
                inventory_token = id(session.gameplay_inventory)
            state = PlayerPersistenceService._capture_account_state(server, session)
            if state is None:
                raise ValueError("character inventory is not ready")
            inventory = clone_inventory_info(state.inventory)

        item = next((entry for entry in owned_inventory_items(inventory)
                     if int(entry.item_id) == item_id), None)
        if item is None:
            raise ValueError("selected item is no longer in this character's inventory")
        inventory.items = [entry for entry in inventory.items if int(entry.item_id) != item_id]
        inventory.premium_inventory_overflow = [
            entry for entry in inventory.premium_inventory_overflow if int(entry.item_id) != item_id
        ]
        inventory.premium_storage_overflow = [
            entry for entry in inventory.premium_storage_overflow if int(entry.item_id) != item_id
        ]
        for name in ("equipment_slots", "storage_58", "storage_80", "storage_a8", "storage_d0", "field_158"):
            getattr(inventory, name).pop(item_id, None)
        for name in ("field_f8", "field_118", "field_180"):
            values = getattr(inventory, name)
            setattr(inventory, name, [value for value in values if int(value) != item_id])
        inventory.changed_item_ids = []
        inventory.field_1e0 = False

        if target is None:
            from dataclasses import replace as replace_state
            self.store.put(replace_state(state, inventory=inventory), flush=True)
        else:
            delta = clone_inventory_info(inventory)
            delta.changed_item_ids = [item_id]
            delta.field_1e0 = True
            final = clone_inventory_info(inventory)
            final.items = []
            final.changed_item_ids = []
            final.field_158 = {}
            packet, packet_bits = encode_server_command_batch((
                ServerCommand(INVENTORY_INFO_COMMAND, delta, inventory.actor_id),
                ServerCommand(INVENTORY_INFO_COMMAND, final, inventory.actor_id),
            ), include_terminator=True)
            def commit():
                if not server.is_session_current(session):
                    raise ValueError("character session changed; refresh and retry")
                with session.flow_lock:
                    if not ready(session, cid):
                        raise ValueError("character must remain fully loaded in a map")
                    if getattr(session, "gameplay_inventory_rewards_in_progress", 0):
                        raise ValueError("character inventory is being updated; try again shortly")
                    if id(session.gameplay_inventory) != inventory_token:
                        return False
                    session.gameplay_inventory = inventory
                PlayerPersistenceService._persist_gameplay_session(
                    server, session, reason="CMS inventory item deleted", flush=True
                )
                session.send_reliable_ordered_split(
                    packet, total_bit_length=packet_bits,
                    label_prefix="admin-inventory-item-delete",
                )
                return True
            if not call_admin_command(server.admin_commands, commit, session=session):
                raise ValueError("character inventory changed; refresh and try again")
        self.repository.audit(actor.username, "character.inventory.item.delete", cid, {
            "item_id": item_id, "template_id": item.template_id,
            "amount": item.amount, "online": target is not None, "reason": reason,
        })
        return {"ok": True, "item_id": item_id, "template_id": item.template_id,
                "amount": item.amount, "online": target is not None}

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
        if path == "/api/npc-shops":
            return page(self.npc_shop_overrides.list_shops(search), query)
        if path.startswith("/api/npc-shops/"):
            shop_id = unquote(path[len("/api/npc-shops/"):])
            offset, limit = paging(query)
            return self.npc_shop_overrides.detail(
                shop_id, search=search, offset=offset, limit=limit
            )
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
            points = tuple(state.talent_category_points) if state is not None else ()
            wisdom_xp = int(points[1]) if len(points) == 4 else 0
            wisdom_range = self.wisdom_level_range()
            return {"profile": asdict(profile), "owner": owner.username if owner else None,
                    "account": self.account_document(owner) if owner else None,
                    "level_range": self.level_range(profile.class_id),
                    "wisdom": {"level": self.wisdom_progression_level(wisdom_xp),
                               "experience": wisdom_xp, "range": wisdom_range},
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
            rows = [row for row in self.events.templates()
                    if search in row["template_id"].casefold()
                    or search in str(row["name"] or "").casefold()
                    or search in str(row.get("template_name") or "").casefold()
                    or any(search in str(name).casefold() for name in row.get("search_names", ()))]
            return page([{key: value for key, value in row.items()
                          if key not in {"template_name", "search_names"}} for row in rows], query)
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
        if path == "/api/season-pass":
            return self.season_pass.overview()
        if path == "/api/season-pass/rewards":
            return self.season_pass.rewards()
        if path == "/api/season-pass/missions":
            return self.season_pass.missions()
        if path == "/api/currencies":
            return {"currencies": list(CURRENCIES)}
        if path.startswith("/api/items/"):
            return item_metadata(self.static_database_path, unquote(path[len("/api/items/"):]))
        if path == "/api/items":
            sort = query.get("sort", "template")
            orders = {
                "template": (lambda row: row["template_id"], False),
                "name": (lambda row: (row["name"].casefold(), row["template_id"]), False),
                "category": (lambda row: (row["category"].casefold(), row["template_id"]), False),
                "category_desc": (lambda row: row["category"].casefold(), True),
            }
            if sort not in orders:
                raise ValueError("invalid item catalog sort order")
            rows = []
            for row in item_catalog(self.static_database_path):
                localized_matches = [
                    (locale, name) for locale, name in row["localized_names"]
                    if search and search in name.casefold()
                ]
                if (search and search not in row["template_id"].casefold()
                        and search not in row["category"].casefold()
                        and search not in row["name"].casefold()
                        and not any(search in name.casefold() for name in row["search_names"])):
                    continue
                if localized_matches:
                    _, matched_name = min(localized_matches, key=lambda item: (
                        item[1].casefold() != search,
                        item[0].casefold() != "en",
                        not item[1].casefold().startswith(search),
                        item[0].casefold(),
                    ))
                    row = {**row, "name": matched_name}
                rows.append(row)
            rows.sort(key=lambda row: row["template_id"])
            key, reverse = orders[sort]
            rows.sort(key=key, reverse=reverse)
            return page([{key: value for key, value in row.items()
                          if key not in {"search_names", "localized_names"}}
                         for row in rows], query)
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
        if path.startswith("/api/npc-shops/"):
            parts = path.strip("/").split("/")
            if len(parts) < 3 or parts[0:2] != ["api", "npc-shops"]:
                raise AdminError(404, "NPC shop action not found")
            shop_id = unquote(parts[2])
            from dso_server.protocol.shop_protocol import OFFER_CATEGORY_IDS
            if len(parts) == 4 and parts[3] == "categories":
                category_name = payload.get("category_name")
                if not isinstance(category_name, str) or category_name not in OFFER_CATEGORY_IDS:
                    raise ValueError("choose a category supported by the game client")
                self.npc_shop_overrides.create_category(
                    shop_id, OFFER_CATEGORY_IDS[category_name], category_name,
                    account.username, reason,
                )
                self.repository.audit(account.username, "npc_shop.category.create", shop_id, {
                    "category": category_name, "reason": reason,
                })
                return {"ok": True, "live_refreshed": self._refresh_open_npc_shops(shop_id)}
            if len(parts) == 4 and parts[3] == "offers":
                category_name = payload.get("category_name")
                item_id = payload.get("item_id")
                if not isinstance(category_name, str) or category_name not in OFFER_CATEGORY_IDS:
                    raise ValueError("choose a category supported by the game client")
                if not isinstance(item_id, str) or not 1 <= len(item_id) <= 200:
                    raise ValueError("item template ID is required")
                if type(payload.get("amount")) is not int:
                    raise ValueError("amount must be an integer")
                offer_id = self.npc_shop_overrides.add_offer(
                    shop_id, OFFER_CATEGORY_IDS[category_name], category_name,
                    item_id, payload["amount"], payload.get("prices"),
                    account.username, reason,
                )
                self.repository.audit(account.username, "npc_shop.offer.create", offer_id, {
                    "shop_id": shop_id, "item_id": item_id,
                    "category": category_name, "amount": payload["amount"],
                    "prices": payload.get("prices"), "reason": reason,
                })
                return {"ok": True, "offer_id": offer_id,
                        "live_refreshed": self._refresh_open_npc_shops(shop_id)}
            if len(parts) == 6 and parts[3] == "offers" and parts[5] == "delete":
                offer_id = unquote(parts[4])
                deleted = self.npc_shop_overrides.delete_offer(shop_id, offer_id)
                self.repository.audit(account.username, "npc_shop.offer.delete", offer_id, {
                    "shop_id": shop_id, **deleted, "reason": reason,
                })
                return {"ok": True, "live_refreshed": self._refresh_open_npc_shops(shop_id)}
            if len(parts) == 6 and parts[3] == "offers" and parts[5] == "price":
                offer_id = unquote(parts[4])
                self.npc_shop_overrides.update_prices(
                    shop_id, offer_id, payload.get("prices"),
                    account.username, reason,
                )
                self.repository.audit(account.username, "npc_shop.offer.price", offer_id, {
                    "shop_id": shop_id, "prices": payload.get("prices"),
                    "reason": reason,
                })
                return {"ok": True, "live_refreshed": self._refresh_open_npc_shops(shop_id)}
            raise AdminError(404, "NPC shop action not found")
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
        if path.startswith("/api/season-pass/"):
            parts = path.strip("/").split("/")
            try:
                if parts == ["api", "season-pass", "sync"]:
                    return self.season_pass.sync_clients(account.username, reason)
                if len(parts) == 4 and parts[2] == "rewards":
                    return self.season_pass.update_reward(account.username, reason, int(parts[3]), payload)
            except SeasonPassConflict as exc:
                raise AdminError(409, str(exc)) from exc
            raise AdminError(404, "Season Pass action not found")
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
            if len(parts) == 6 and parts[3] == "inventory" and parts[5] == "delete":
                cid, item_id = int(parts[2]), int(parts[4])
                if self.store.get_profile(cid) is None or self.registry.character_owner(cid) is None:
                    raise AdminError(404, "owned character not found")
                return self.delete_character_item(cid, item_id, account, reason)
            if len(parts) != 4:
                raise AdminError(404, "action not found")
            cid = int(parts[2])
            if self.store.get_profile(cid) is None or self.registry.character_owner(cid) is None:
                raise AdminError(404, "owned character not found")
            if parts[3] == "privileges":
                return self.set_privileges(cid, payload.get("changes"), account, reason)
            if parts[3] == "identity":
                return self.set_identity(cid, payload.get("changes"), account, reason)
            if parts[3] == "wisdom":
                return self.set_wisdom_level(cid, payload.get("level"), account, reason)
            if parts[3] == "honor-points":
                return self.change_honor_points(
                    cid, payload.get("mode"), payload.get("amount"), account, reason
                )
            if parts[3] == "copy-inventory":
                return self.copy_character_inventory(
                    cid, payload.get("target_character_id"), account, reason
                )
            if parts[3] == "complete-main-quests":
                return self.complete_character_main_quests(cid, account, reason)
            if parts[3] == "complete-ammon-pw-quests":
                return self.complete_character_ammon_parallel_world_quests(cid, account, reason)
            if parts[3] == "complete-active-quests":
                return self.complete_character_active_quests(cid, account, reason)
            if parts[3] == "unlock-all-recipes":
                return self.unlock_character_all_recipes(cid, account, reason)
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
        if path == "/api/bookings/batch":
            # Batch booking: multiple items/currencies in one delivery.
            cid = payload.get("character_id")
            if type(cid) is not int or cid <= 0:
                raise ValueError("character ID must be a positive integer")
            owner = self.registry.character_owner(cid)
            if owner is None or self.store.get_profile(cid) is None:
                raise ValueError("character must exist and belong to an account")
            if self.store.get(cid) is None:
                raise ValueError("character must enter the game once before receiving bookings")
            if payload.get("account_id") is not None and payload["account_id"] != owner.account_id:
                raise ValueError("the character does not belong to the selected account")
            items = payload.get("items")
            if not isinstance(items, list) or not items:
                raise ValueError("batch must contain at least one item")
            import json
            batch_resource = json.dumps(items)
            profile = self.store.get_profile(cid)
            options = self.delivery.validate("batch", batch_resource, 1, level=profile.level, class_id=profile.class_id)
            import secrets
            request_id = payload.get("request_id") or "batch-" + secrets.token_hex(12)
            if not isinstance(request_id, str) or not 16 <= len(request_id) <= 100:
                raise ValueError("a request ID of 16 to 100 characters is required")
            booking = self.repository.create_booking(account.username, owner.account_id, cid,
                                                     "batch", batch_resource, 1, reason, request_id, options)
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
