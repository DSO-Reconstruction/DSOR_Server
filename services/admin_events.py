"""Audited CMS edits to the existing game-event schedule."""

import copy
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import xml.etree.ElementTree as ElementTree

from dso_server.gameplay.event_system import (
    load_event_catalog, scheduled_event_flag_names, UINT32_MAX,
)
from dso_server.gameplay.quest_content import (
    NO_TIMESTAMP, QUEST_STATE_CANCELLED, load_quest_catalog,
)
from dso_server.services import events as game_events
from dso_server.paths import (
    DEFAULT_LANGUAGE_ROOT, DEFAULT_QUESTS_PATH, PACKAGE_ROOT,
)
from dso_server.runtime.admin_commands import call_admin_command
from dso_server.runtime.file_revisions import invalidate as invalidate_file_revision
from types import MappingProxyType
from typing import Mapping

from dso_server.services.admin_items import _display_name, item_language_directory, locale_titles


_SCHEDULE_LOCK = threading.RLock()

LINKED_SILENT_EVENTS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "christmas_2012": (
        "christmas_2012_silent",
        "e_chr_calendar_map_silent",
        "event_chr_event_2023_silent",
    ),
    "event_kingshill_sewers_01": (
        "e_ks_01_blitz_payment_offer_silent",
        "e_ks_01_rat_king_silent_cave",
        "e_ks_01_rat_king_silent_crypt",
        "e_ks_01_rat_king_silent_sewers",
        "event_kingshill_sewers_01_challenge_01_cave_silent",
        "event_kingshill_sewers_01_challenge_01_crypt_silent",
        "event_kingshill_sewers_01_key_rats_silent",
    ),
})


def _event_quest_ids(template_id, event_flags):
    catalog = load_quest_catalog(DEFAULT_QUESTS_PATH)
    flags = {str(value).casefold() for value in event_flags}
    flags.add(str(template_id).casefold())
    return frozenset(
        quest.key for quest in catalog.quests.values()
        if flags.intersection(
            value.strip().casefold()
            for value in str(quest.event_id or "").split(";")
            if value.strip()
        )
    )


def _clear_anniversary_golden_pinata_claims(claims, event_id):
    """Clear per-pinata claims when the Anniversary event run ends."""

    legacy_key = f"anniversary-golden-pinata:{int(event_id)}"
    prefix = legacy_key + ":"
    changed = False
    for key in tuple(claims):
        normalized = str(key).casefold()
        if normalized == legacy_key or normalized.startswith(prefix):
            claims.pop(key, None)
            changed = True
    return changed


def _clear_christmas_calendar_present_claims(claims, event_id):
    """Clear Solstice Cave advent calendar present claims when the event run ends."""

    legacy_key = f"christmas-calendar-present:{int(event_id)}"
    prefix = legacy_key + ":"
    changed = False
    for key in tuple(claims):
        normalized = str(key).casefold()
        if normalized == legacy_key or normalized.startswith(prefix):
            claims.pop(key, None)
            changed = True
    return changed


def _clear_event_run_state(
        state, event_id, quest_ids, *, reset_anniversary_pinatas=False, reset_christmas_calendar=False,
):
    changed = False
    progress = state.event_progress
    for key in (str(event_id), int(event_id)):
        changed = progress.pop(key, None) is not None or changed
    participation = state.event_participation
    for key in (str(event_id), int(event_id)):
        changed = participation.pop(key, None) is not None or changed
    for quest_id in tuple(state.quest_log):
        if str(quest_id).casefold() in quest_ids:
            del state.quest_log[quest_id]
            changed = True
    if reset_anniversary_pinatas:
        changed = _clear_anniversary_golden_pinata_claims(
            state.timed_offer_purchases, event_id,
        ) or changed
    if reset_christmas_calendar:
        changed = _clear_christmas_calendar_present_claims(
            state.timed_offer_purchases, event_id,
        ) or changed
    return changed


def _reset_event_run(service, event):
    """Clear a finished run's saved progress and its scheduled event quests."""

    flags = scheduled_event_flag_names(event)
    quest_ids = _event_quest_ids(event.template.resource_id, flags)
    reset_anniversary_pinatas = (
        str(event.template.resource_id).casefold() == "anniversary_event"
    )
    reset_christmas_calendar = (
        str(event.template.resource_id).casefold() == "christmas_2012"
    )
    app = getattr(service, "application", None)
    servers = tuple(
        server for server in (
            getattr(app, "characters", None), getattr(app, "login", None),
        ) if server is not None
    )

    # Update live sessions first so their next autosave cannot put completed
    # event progress back after the durable records have been cleared.
    seen_sessions = set()
    for server in servers:
        for session in server.session_snapshot():
            identity = id(session)
            if identity in seen_sessions:
                continue
            seen_sessions.add(identity)
            removed_quests = []
            with session.flow_lock:
                if getattr(session, "gameplay_character", None) is None:
                    continue
                changed = False
                progress = getattr(session, "gameplay_event_progress", {})
                participation = getattr(
                    session, "gameplay_event_participation", {},
                )
                for key in (str(event.event_id), int(event.event_id)):
                    changed = progress.pop(key, None) is not None or changed
                    changed = participation.pop(key, None) is not None or changed
                for attr in ("gameplay_quest_log", "gameplay_dormant_quests"):
                    quest_log = getattr(session, attr, {}) or {}
                    for quest_id in tuple(quest_log):
                        if str(quest_id).casefold() in quest_ids:
                            del quest_log[quest_id]
                            removed_quests.append(str(quest_id))
                            changed = True
                if reset_anniversary_pinatas:
                    claims = getattr(
                        session, "gameplay_timed_offer_purchases", None,
                    )
                    if claims is not None:
                        changed = _clear_anniversary_golden_pinata_claims(
                            claims, event.event_id,
                        ) or changed
                if reset_christmas_calendar:
                    claims = getattr(
                        session, "gameplay_timed_offer_purchases", None,
                    )
                    if claims is not None:
                        changed = _clear_christmas_calendar_present_claims(
                            claims, event.event_id,
                        ) or changed
                tracking = getattr(session, "gameplay_quest_tracking", None)
                if tracking and str(tracking[0]).casefold() in quest_ids:
                    session.gameplay_quest_tracking = None
                    changed = True
            if removed_quests:
                try:
                    state_commands = tuple(
                        server._quest_state_commands(
                            session, quest_id, QUEST_STATE_CANCELLED,
                            NO_TIMESTAMP,
                        )[0]
                        for quest_id in dict.fromkeys(removed_quests)
                    )
                    state_commands += server._quest_state_commands(
                        session, removed_quests[0], QUEST_STATE_CANCELLED,
                        NO_TIMESTAMP,
                    )[1:]
                    server._send_quest_commands(
                        session, state_commands,
                        label="admin-event-reset-quests",
                    )
                except Exception as exc:
                    print(
                        f"[Admin events] could not clear event quest UI for "
                        f"character {getattr(session.gameplay_character, 'character_id', 0)}: "
                        f"{exc}"
                    )
            if changed:
                persist = getattr(session, "gameplay_persist_callback", None)
                if persist is not None:
                    persist(session, "event reset", flush=False)

    seen_stores = set()
    for server in servers:
        store = getattr(server, "account_store", None)
        if store is None or id(store) in seen_stores:
            continue
        seen_stores.add(id(store))
        with store.lock:
            character_ids = tuple(store.states)
        for character_id in character_ids:
            state = store.get(character_id)
            if state is not None and _clear_event_run_state(
                state, event.event_id, quest_ids,
                reset_anniversary_pinatas=reset_anniversary_pinatas,
                reset_christmas_calendar=reset_christmas_calendar,
            ):
                store.put(state)
        # Persist the full reset once after every character snapshot has been
        # queued; event transitions are rare but can affect the whole roster.
        store.flush()


class EventScheduleConflict(ValueError):
    pass


@lru_cache(maxsize=4)
def _event_title_aliases():
    """Collect event title translations so searches work across client locales."""
    roots = (DEFAULT_LANGUAGE_ROOT, PACKAGE_ROOT / "cdndata" / "export_win32" / "language")
    titles = {}
    for root in roots:
        if not root.is_dir():
            continue
        for directory in root.iterdir():
            source = directory / "db.event.xml"
            if not source.is_file():
                continue
            try:
                for entry in ElementTree.parse(source).getroot().iter("item"):
                    key = str(entry.get("name") or "")
                    value = str(entry.text or "").strip()
                    if key and value and not value.startswith("Localize:"):
                        titles.setdefault(key, set()).add(value)
            except (ElementTree.ParseError, OSError):
                continue
    return {key: tuple(values) for key, values in titles.items()}


@lru_cache(maxsize=4)
def _templates(path, revision, language_directory):
    display_titles = locale_titles(language_directory)
    aliases = _event_title_aliases()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT Id AS template_id,Name AS template_name,LocaleIdTitle AS locale_title,MinPlayerLevel AS min_level,"
            "MaxPlayerLevel AS max_level,Map AS map_id,TeleportTarget AS teleport_target,"
            "EventStatusEffects AS status_effects,EventProgressBars AS progress_bars "
            "FROM _Template_Events WHERE COALESCE(ClientOnly,0)=0 AND COALESCE(MapLocal,0)=0 ORDER BY Id").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        template_id = str(item["template_id"])
        raw_name = str(item.pop("template_name") or "")
        locale_key = str(item.pop("locale_title") or "")
        item["name"] = _display_name(display_titles, template_id, raw_name, locale_key)
        item["template_name"] = raw_name
        item["search_names"] = tuple(dict.fromkeys((
            *aliases.get(locale_key, ()), *aliases.get(f"{template_id}Title", ()),
            *aliases.get(f"{template_id}ShortTitle", ()),
        )))
        result.append(item)
    return tuple(result)


class EventAdministration:
    def __init__(self, service, schedule_path=None):
        self.service = service
        self.schedule_path = Path(schedule_path or game_events.EVENT_SCHEDULE_PATH).resolve()
        self._observed = None
        self._pending_refresh = None
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread = None

    @staticmethod
    def _snapshot(catalog, now):
        # Freeze the previously announced states, so an elapsed deadline still
        # produces an explicit inactive packet even though the file is unchanged.
        return replace(catalog, events=tuple(
            replace(event, forced_state=event.state_at(now)) for event in catalog.events))

    def refresh_schedule(self):
        with _SCHEDULE_LOCK:
            _, _, catalog = self._read()
            now = datetime.now(timezone.utc)
            snapshot = self._snapshot(catalog, now)
            previous = self._pending_refresh or self._observed
            if previous is not None and (
                    self._pending_refresh is not None or previous.events != snapshot.events):
                result = self._publish(previous, catalog)
                self.service.repository.audit("system", "event.schedule.refresh", "schedule", {
                    "states": {str(event.event_id): event.forced_state for event in snapshot.events}, "refresh": result})
            self._observed = snapshot
            if self._pending_refresh is not None:
                return 1.0
            deadlines = [moment for event in catalog.events
                         if event.forced_state not in {"active", "upcoming", "expired"}
                         for moment in (event.starts_at, event.ends_at)
                         if moment is not None and moment > now]
            # Recheck wall-clock adjustments and external schedule edits too.
            return max(0.05, min(60.0, (min(deadlines) - datetime.now(timezone.utc)).total_seconds())) if deadlines else 60.0

    def _watch_schedule(self):
        while not self._stopping.is_set():
            self._wake.clear()
            try:
                delay = self.refresh_schedule()
            except Exception as exc:
                print(f"[Admin events] schedule refresh failed: {exc}")
                delay = 5.0
            if not self._stopping.is_set():
                self._wake.wait(delay)

    def start(self):
        if self._thread is not None or self.service.application is None:
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._watch_schedule, name="admin-event-schedule", daemon=True)
        self._thread.start()

    def stop(self):
        self._stopping.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    @staticmethod
    def _end_at(payload, now):
        days, value = payload.get("duration_days"), payload.get("ends_at")
        if days is not None:
            if value is not None:
                raise ValueError("choose a duration or a custom end date, not both")
            if type(days) is not int or days not in {3, 7, 14, 31}:
                raise ValueError("event duration must be 3, 7, 14 or 31 days")
            return now + timedelta(days=days)
        if value is None:
            return None
        try:
            if not isinstance(value, str):
                raise ValueError()
            end = datetime.fromisoformat(value)
            if end.tzinfo is None or end.utcoffset() is None:
                raise ValueError()
            end = end.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            raise ValueError("end date must include a valid date, time and timezone") from None
        if end <= now:
            raise ValueError("end date must be in the future")
        return end

    @staticmethod
    def _run_settings(payload, defaults):
        """Validated drop chance, amount and participation for a run."""
        chance = payload.get("collectible_drop_chance", defaults["collectible_drop_chance"])
        amount = payload.get("collectible_amount", defaults["collectible_amount"])
        participation = payload.get("participation_default", defaults["participation_default"])
        if type(chance) not in (int, float) or not math.isfinite(chance) or not 0 <= chance <= 1:
            raise ValueError("collectible drop chance must be between 0 and 1")
        if type(amount) is not int or not 1 <= amount <= 1_000_000:
            raise ValueError("collectible amount must be a whole number from 1 to 1,000,000")
        if type(participation) is not bool:
            raise ValueError("default participation must be true or false")
        return {"participation_default": participation, "collectible_drop_chance": chance,
                "collectible_amount": amount}

    def templates(self):
        path = self.service.static_database_path.resolve()
        stat = path.stat()
        language_directory = item_language_directory()
        return _templates(path, (stat.st_mtime_ns, stat.st_size), language_directory)

    def _read(self):
        raw = self.schedule_path.read_bytes()
        document = json.loads(raw)
        if not isinstance(document, dict) or not isinstance(document.get("events"), list):
            raise ValueError("event schedule must contain an events array")
        catalog = load_event_catalog(self.service.static_database_path, self.schedule_path)
        return document, hashlib.sha256(raw).hexdigest(), catalog

    @staticmethod
    def event_document(event, config):
        template = event.template
        return {"event_id": event.event_id, "template_id": template.resource_id,
                "name": template.client_name, "state": event.state_at(datetime.now(timezone.utc)),
                "mode": event.forced_state, "starts_at": event.starts_at.isoformat() if event.starts_at else None,
                "ends_at": event.ends_at.isoformat() if event.ends_at else None,
                "min_level": template.minimum_level, "max_level": template.maximum_level,
                "map_id": template.map_id, "teleport_target": template.teleport_target,
                "status_effects": list(template.status_effects),
                "progress_bars": [bar.resource_id for bar in template.progress_bars],
                "manageable": not template.client_only and not template.map_local,
                "config": copy.deepcopy(config)}

    def listing(self):
        with _SCHEDULE_LOCK:
            document, revision, catalog = self._read()
            configs = {int(entry["event_id"]): entry for entry in document["events"]}
            rows = [self.event_document(event, configs[event.event_id]) for event in catalog.events]
            rank = {"active": 0, "upcoming": 1, "expired": 2}
            rows.sort(key=lambda row: (rank[row["state"]], row["template_id"], -row["event_id"]))
            return {"rows": rows, "revision": revision,
                    "counts": {state: sum(row["state"] == state for row in rows) for state in rank}}

    def detail(self, event_id):
        listing = self.listing()
        row = next((row for row in listing["rows"] if row["event_id"] == event_id), None)
        if row is None:
            raise ValueError("event instance not found")
        return {**row, "revision": listing["revision"]}

    def change(self, actor, reason, payload, *, event_id=None, enabled=True, reschedule=False):
        with _SCHEDULE_LOCK:
            document, revision, previous = self._read()
            if payload.get("revision") != revision:
                raise EventScheduleConflict("the event schedule changed; refresh the page before applying this action")
            now = datetime.now(timezone.utc)
            before = None
            end = self._end_at(payload, now) if enabled else None
            if event_id is None:
                template_id = payload.get("template_id")
                if not isinstance(template_id, str) or not any(t["template_id"] == template_id for t in self.templates()):
                    raise ValueError("choose a global game-event template")
                settings = self._run_settings(payload, {
                    "collectible_drop_chance": 0.35, "collectible_amount": 1, "participation_default": True})
                event_id = max((65535, *(event.event_id for event in previous.events))) + 1
                if event_id > UINT32_MAX:
                    raise ValueError("event instance ID limit reached")
                # Carry forward an event template's explicit drop-map scope
                # when creating another schedule instance. Without this,
                # templates whose collectible maps are defined per schedule
                # can start dropping progress items in every map.
                collectible_maps = next((
                    sorted(event.collectible_maps)
                    for event in previous.events
                    if event.template.resource_id == template_id
                    and event.collectible_maps
                ), None)
                entry = {"event_id": event_id, "template_id": template_id,
                         "state": "scheduled" if end else "active", "starts_at": now.isoformat(),
                         "ends_at": end.isoformat() if end else None, **settings}
                if collectible_maps is not None:
                    entry["collectible_maps"] = collectible_maps
                document["events"].append(entry)
                action = "event.add"
            else:
                event = next((event for event in previous.events if event.event_id == event_id), None)
                if event is None:
                    raise ValueError("event instance not found")
                if event.template.client_only or event.template.map_local:
                    raise ValueError("map-local and client-owned events are managed by the game")
                entry = next(entry for entry in document["events"] if int(entry["event_id"]) == event_id)
                before = copy.deepcopy(entry)
                if reschedule and event.state_at(now) != "active":
                    raise ValueError("only active events can have their end date changed; activate the event first")
                entry["state"] = ("scheduled" if end else "active") if enabled else "expired"
                if enabled:
                    if not reschedule:
                        entry["starts_at"] = now.isoformat()
                        # Reactivation may also adjust the run's drop settings;
                        # omitted fields keep the instance's stored values.
                        if any(key in payload for key in (
                                "collectible_drop_chance", "collectible_amount", "participation_default")):
                            entry.update(self._run_settings(payload, {
                                "collectible_drop_chance": event.collectible_drop_chance,
                                "collectible_amount": event.collectible_amount,
                                "participation_default": event.participation_default}))
                    entry["ends_at"] = end.isoformat() if end else None
                else:
                    entry["ends_at"] = now.isoformat()
                template_id = event.template.resource_id
                action = "event.schedule" if reschedule else "event.activate" if enabled else "event.deactivate"
            if enabled and any(event.event_id != event_id and event.template.resource_id == template_id
                               and event.state_at(now) != "expired" for event in previous.events):
                raise ValueError("this template already has an active or upcoming event instance")
            linked_silent_templates = LINKED_SILENT_EVENTS.get(
                str(template_id).casefold() if template_id else "", ()
            )
            for silent_tmpl in linked_silent_templates:
                silent_entry = next(
                    (
                        e for e in document.get("events", [])
                        if str(e.get("template_id", "")).casefold() == silent_tmpl.casefold()
                    ),
                    None,
                )
                if enabled:
                    if silent_entry is not None:
                        silent_entry["state"] = entry["state"]
                        silent_entry["starts_at"] = entry["starts_at"]
                        silent_entry["ends_at"] = entry["ends_at"]
                        for opt in ("participation_default", "collectible_drop_chance", "collectible_amount"):
                            if opt in entry:
                                silent_entry[opt] = entry[opt]
                    else:
                        silent_id = max((65535, *(int(e.get("event_id", 0)) for e in document.get("events", [])))) + 1
                        new_silent = {
                            "event_id": silent_id,
                            "template_id": silent_tmpl,
                            "state": entry["state"],
                            "starts_at": entry["starts_at"],
                            "ends_at": entry["ends_at"],
                            "participation_default": entry.get("participation_default", True),
                            "collectible_drop_chance": entry.get("collectible_drop_chance", 0.35),
                            "collectible_amount": entry.get("collectible_amount", 1),
                        }
                        document.setdefault("events", []).append(new_silent)
                else:
                    if silent_entry is not None and silent_entry.get("state") != "expired":
                        silent_entry["state"] = "expired"
                        silent_entry["ends_at"] = now.isoformat()
            record = {"reason": reason, "before": before, "after": copy.deepcopy(entry)}
            catalog = self._write(document, revision, actor, action, event_id, record)
            # Keep publication ordered with schedule edits so a slower request
            # cannot restore an older catalog on an online session.
            self._observed = self._snapshot(catalog, now)
            refresh = self._publish(previous, catalog)
            self._wake.set()
            self.service.repository.audit(actor, action + ".refresh", event_id, refresh)
            return {"event_id": event_id, "refresh": refresh, "revision": self._read()[1]}

    def _write(self, document, revision, actor, action, event_id, record):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", delete=False,
                    dir=self.schedule_path.parent, prefix=".cms-events-", suffix=".json") as output:
                temporary = Path(output.name)
                json.dump(document, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            # Validate with the exact parser used by gameplay before replacement.
            load_event_catalog(self.service.static_database_path, temporary)
            if hashlib.sha256(self.schedule_path.read_bytes()).hexdigest() != revision:
                raise EventScheduleConflict("the event schedule changed; refresh and try again")
            self.service.repository.audit(actor, action + ".requested", event_id, record)
            os.replace(temporary, self.schedule_path)
            temporary = None
            # Lookups reuse the last observed schedule revision; publish this
            # write to the very next one.
            invalidate_file_revision(self.schedule_path)
            self.service.repository.audit(actor, action, event_id, record)
            return load_event_catalog(self.service.static_database_path, self.schedule_path)
        except OSError as exc:
            raise ValueError(f"could not save the event schedule: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _publish(self, previous, catalog):
        from dso_server.services.admin_event_refresh import refresh_session_events
        from dso_server.services.global_event_refresh import refresh_map_events
        from dso_server.protocol.player_init_protocol import EVENT_UPDATE_COMMAND, encode_event_update
        result = {"updated": 0, "deferred": 0, "errors": []}
        now = datetime.now(timezone.utc)
        current_by_id = {event.event_id: event for event in catalog.events}
        for event in previous.events:
            current = current_by_id.get(event.event_id)
            if event.active_at(now) and (
                current is None or not current.active_at(now)
            ):
                _reset_event_run(self.service, event)
        app = self.service.application
        if app is None:
            return result
        update_packet, update_bits = encode_event_update(catalog.event_update())
        for server in (app.characters, app.login):
            worlds = {}
            if hasattr(server, "post_selection_tail"):
                server.post_selection_tail = tuple(
                    (command, update_packet, update_bits) if command == EVENT_UPDATE_COMMAND else (command, packet, bits)
                    for command, packet, bits in server.post_selection_tail)
            for session in server.session_snapshot():
                with session.flow_lock:
                    character = session.gameplay_character
                    cid = int(character.character_id) if character is not None else None
                    context = (session.gameplay_map, session.gameplay_chase_generation)
                    runtime = getattr(session, "gameplay_map_event_runtime", None)
                if cid is None:
                    continue
                try:
                    refreshed = call_admin_command(server.admin_commands,
                        lambda: refresh_session_events(server, session, cid, context, previous, catalog),
                        session=session)
                    result["updated" if refreshed else "deferred"] += 1
                    if refreshed and runtime is not None:
                        _, viewers = worlds.setdefault(id(runtime), (runtime, []))
                        viewers.append((session, cid, context))
                except Exception as exc:
                    result["deferred"] += 1
                    result["errors"].append({"character_id": cid, "error": str(exc)[:200]})
            # Publish global snapshots to every viewer before changing world
            # entities. A per-session transition would otherwise send NPCs or
            # exits to peers whose event-set controller is still inactive.
            for runtime, viewers in worlds.values():
                try:
                    completed, errors = call_admin_command(
                        server.admin_commands,
                        lambda: refresh_map_events(server, runtime, viewers, previous, catalog),
                    )
                    deferred = len(viewers) - len(completed)
                    result["errors"].extend(errors)
                except Exception as exc:
                    deferred = len(viewers)
                    result["errors"].extend(
                        {"character_id": cid, "error": str(exc)[:200]}
                        for _session, cid, _context in viewers
                    )
                result["updated"] -= deferred
                result["deferred"] += deferred
        # A busy gameplay gate or failed client delivery must not permanently
        # lose the world refresh just because the saved schedule is unchanged.
        self._pending_refresh = (
            (self._pending_refresh or previous) if result["deferred"] else None
        )
        return result
