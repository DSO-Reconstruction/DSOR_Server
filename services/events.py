"""Runtime event participation, progress updates, and reward delivery."""

from __future__ import annotations

from dso_server.protocol.command_ids import command_id
from datetime import datetime, timezone
from functools import lru_cache
import os
import sqlite3
import threading
import time
import uuid

from dso_server.gameplay.map_event_state import load_map_event_content, map_key
from dso_server.persistence.account_registry import DEFAULT_SESSION_TTL_SECONDS
from dso_server.runtime.context import *
from dso_server.runtime.transport import Session


SET_EVENT_PARTICIPATION_COMMAND = command_id("Commands::SetEventParticipationCommand", 0x0078)
# Notification kinds follow the ``_Template_Notifications`` row order of the
# shipped database: a live check on 2026-09-09 showed kind 6 rendering as the
# daily-reward popup, which is row 6 (``loginreward``). Row 7 is
# ``eventnonpersistent``, the dismissable event popup whose JSON payload names
# the event; row 8 ``eventpersistent`` is the next candidate if 7 shows
# nothing. Override with DSO_EVENT_NOTIFICATION_KIND while measuring; see
# docs/EVENT_NOTIFICATIONS.md.
EVENT_NOTIFICATION_KIND = max(
    0, int(os.environ.get("DSO_EVENT_NOTIFICATION_KIND", "7"))
)
EVENT_NOTIFICATION_GUID_PREFIX = "dso-local-event-notification:"
# A login session lasts at most the registry TTL, so memory of what it was
# told can be dropped after that; the cap bounds the map for a busy server.
EVENT_NOTIFICATION_MEMORY_LIMIT = 4096
_EVENT_NOTIFICATION_MEMORY_LOCK = threading.Lock()


def event_notification_guid(event_id: int) -> bytes:
    """Deterministic per-event GUID; the client discards repeats by GUID."""

    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"{EVENT_NOTIFICATION_GUID_PREFIX}{int(event_id)}"
    ).bytes


def _epoch_seconds(moment: datetime | None) -> int:
    return int(moment.timestamp()) if moment is not None else 0


def event_notification_entries(
    catalog, *, now: datetime, already_sent=frozenset(),
) -> tuple[NotificationEntry, ...]:
    """Build one announcement per active event not yet shown this session.

    Client-only and Silent templates do not produce server announcements. The
    three values carry the event id and its schedule bounds; nothing is known
    to read them, so they are the harmless, informative choice.
    """

    entries = []
    for event in catalog.active_events(now):
        event_id = int(event.event_id)
        if (event_id in already_sent or event.template.client_only
                or event.template.category.casefold() == "silent"):
            continue
        entries.append(
            make_notification_entry(
                EVENT_NOTIFICATION_KIND,
                event_notification_guid(event_id),
                (
                    event_id,
                    _epoch_seconds(event.starts_at),
                    _epoch_seconds(event.ends_at),
                ),
                {
                    "eventId": event_id,
                    "templateId": event.template.resource_id,
                },
            )
        )
    return tuple(entries)


def event_notification_session_key(s: Session, character) -> str | None:
    """Identity under which announcements are remembered.

    The login session GUID survives map transfers and is rotated on relog,
    so it is the natural "once per session" key. Loopback development never
    logs in; there the character id stands in, which announces once per
    server run per character rather than once per login.
    """

    session_id = getattr(s, "login_session_id", None)
    if session_id:
        return f"session:{session_id}"
    character_id = getattr(character, "character_id", None)
    if character_id is None:
        return None
    return f"character:{int(character_id)}"


class EventNotificationMemory:
    """Which events each login session has already been told about."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
        limit: int = EVENT_NOTIFICATION_MEMORY_LIMIT,
        clock=time.monotonic,
    ) -> None:
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.limit = max(1, int(limit))
        self._clock = clock
        self._lock = threading.Lock()
        # key -> [first recorded at, announced event ids]
        self._sent: dict[str, list] = {}

    def claim(self, key: str, event_ids) -> tuple[int, ...]:
        """Record ``event_ids`` for ``key``; return the ones not seen before.

        Claiming is the step that makes "once" hold across both gameplay
        routes and two initializations racing for one session: whichever
        builds its batch first owns the announcement.
        """

        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            entry = self._sent.get(key)
            if entry is None:
                entry = self._sent[key] = [now, set()]
                # Hold the cap after the insert too, evicting the oldest
                # sessions; the one just added is the newest and survives.
                self._enforce_limit_locked()
            fresh = tuple(
                int(event_id)
                for event_id in dict.fromkeys(int(value) for value in event_ids)
                if int(event_id) not in entry[1]
            )
            entry[1].update(fresh)
            return fresh

    def forget(self, key: str) -> None:
        with self._lock:
            self._sent.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sent)

    def _prune_locked(self, now: float) -> None:
        expired = [
            key for key, (recorded_at, _ids) in self._sent.items()
            if now - recorded_at >= self.ttl_seconds
        ]
        for key in expired:
            del self._sent[key]
        self._enforce_limit_locked()

    def _enforce_limit_locked(self) -> None:
        if len(self._sent) <= self.limit:
            return
        for key, _entry in sorted(
            self._sent.items(), key=lambda item: item[1][0]
        )[: len(self._sent) - self.limit]:
            del self._sent[key]


@lru_cache(maxsize=16)
def _baseline_map_local_event_entries(
    database_path: str | Path,
    map_id: str,
    additional_resource_ids: tuple[str, ...] = (),
) -> tuple[EventUpdateEntry, ...]:
    '''Build client activations for selected map-local EventSets.

    Permanent baseline unlocks and logical routes both control authored map
    rows published by the server. Their matching EventUpdate is required or
    the client keeps that scene EventSet disabled even when initialization
    contains its actors.
    '''

    resource_ids = tuple(sorted({
        *(
            str(value).casefold()
            for value in BASELINE_MAP_EVENT_FLAGS.get(
                str(map_id), frozenset(),
            )
        ),
        *(
            str(value).casefold()
            for value in additional_resource_ids
            if str(value).strip()
        ),
    }))
    if not resource_ids:
        return ()
    placeholders = ','.join('?' for _value in resource_ids)
    database = Path(database_path).resolve()
    with sqlite3.connect(
        f'file:{database.as_posix()}?mode=ro', uri=True
    ) as connection:
        rows = connection.execute(
            'SELECT rowid - 1, Id, ClientOnly, MapLocal '
            'FROM _Template_Events '
            f'WHERE LOWER(Id) IN ({placeholders})',
            resource_ids,
        ).fetchall()
    return tuple(
        EventUpdateEntry(
            # Map-local controllers are keyed by name and always use event 0.
            # The catalog row id belongs to scheduled/global events; sending
            # it here makes the client ignore the scene EventSet update.
            event_id=0,
            event_name=str(row[1]),
            flags=(True, True, False, False),
            starts_at=None,
            ends_at=None,
            tags=(),
        )
        for row in sorted(rows, key=lambda value: str(value[1]))
        if bool(int(row[3] or 0))
    )


@lru_cache(maxsize=64)
def _declared_map_local_event_entries(
    database_path: str | Path, map_id: str,
) -> tuple[EventUpdateEntry, ...]:
    """Explicit "not running" entries for every event the map's sets name.

    The client resolves an EventSet from the event states it was told, so an
    event it never heard about leaves the sets that reference it unresolved
    and the actors they gate inert, even when initialization already carried
    them. The baseline unlocks and the instance runtime override these with
    running entries where the event is on.

    The client validates a map-local name against the event's template map
    and aborts the whole update on the first mismatching entry, so names
    authored for other maps are excluded even though this map's sets still
    reference them.
    """

    try:
        referenced = load_map_event_content(
            database_path, map_id
        ).referenced_events
    except Exception:
        return ()
    if not referenced:
        return ()
    database = Path(database_path).resolve()
    target_map = map_key(map_id)
    with sqlite3.connect(
        f'file:{database.as_posix()}?mode=ro', uri=True
    ) as connection:
        rows = connection.execute(
            'SELECT rowid - 1, Id, Map FROM _Template_Events '
            'WHERE MapLocal=1 AND LOWER(Id) IN '
            f'({",".join("?" for _value in referenced)})',
            tuple(sorted(referenced)),
        ).fetchall()
    matched_rows = [
        row for row in rows
        if target_map in {map_key(part) for part in str(row[2] or '').split(';') if part.strip()}
    ]
    return tuple(
        EventUpdateEntry(
            # Match targeted map-event updates and the retail map-local
            # controller contract: local events always carry id 0.
            event_id=0,
            event_name=str(row[1]),
            flags=(False, True, False, False),
            starts_at=None,
            ends_at=None,
            tags=(),
        )
        for row in sorted(matched_rows, key=lambda value: str(value[1]))
    )


class EventService:
    """Bridge the static event catalog to one character session."""

    def _event_catalog(self):
        return load_event_catalog(
            self.static_database_path,
            EVENT_SCHEDULE_PATH,
        )

    def _event_notification_memory(self) -> EventNotificationMemory:
        """Lazily attach the announcement memory to this service instance.

        Both the character service and the legacy 2190 route initialize
        players, so the memory lives on whichever service instance is doing
        it rather than on the character server alone.
        """

        memory = getattr(self, "event_notification_memory", None)
        if memory is None:
            with _EVENT_NOTIFICATION_MEMORY_LOCK:
                memory = getattr(self, "event_notification_memory", None)
                if memory is None:
                    registry = getattr(self, "account_registry", None)
                    memory = EventNotificationMemory(
                        ttl_seconds=getattr(
                            registry,
                            "session_ttl_seconds",
                            DEFAULT_SESSION_TTL_SECONDS,
                        ),
                    )
                    self.event_notification_memory = memory
        return memory

    def _event_notification_entries_for_session(
        self, s: Session, character, *, now: datetime | None = None,
    ) -> tuple[NotificationEntry, ...]:
        """Announce the active events this login session has not seen yet.

        Called while the initialization batch is built; the ids are claimed
        at that point, not on acknowledgement. A failed initialization
        retries through a new UDP session with the same login GUID, and the
        client discards a repeated GUID anyway.
        """

        key = event_notification_session_key(s, character)
        if key is None:
            return ()
        moment = now or datetime.now(timezone.utc)
        catalog = CharacterService._event_catalog(self)
        candidates = tuple(
            int(event.event_id)
            for event in catalog.active_events(moment)
            if not event.template.client_only
            and event.template.category.casefold() != "silent"
        )
        if not candidates:
            return ()
        fresh = CharacterService._event_notification_memory(self).claim(
            key, candidates
        )
        if not fresh:
            return ()
        entries = event_notification_entries(
            catalog,
            now=moment,
            already_sent=frozenset(candidates).difference(fresh),
        )
        if entries:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] announcing "
                f"{len(entries)} active event(s) to {s.addr} "
                f"({key}): {', '.join(str(value) for value in fresh)}"
            )
        return entries

    def _event_initialization_state(
        self, s: Session, character_override=None, *, map_id=None,
        map_access_group_id=None,
    ):
        catalog = CharacterService._event_catalog(self)
        with s.flow_lock:
            s.gameplay_event_catalog = catalog
            s.gameplay_event_progress_callback = (
                lambda session, collectible_id, amount:
                CharacterService._advance_event_progress(
                    self, session,
                    collectible_id=collectible_id,
                    amount=amount,
                )
            )
            character = copy.deepcopy(
                character_override or s.gameplay_character
            )
            progress = copy.deepcopy(s.gameplay_event_progress)
            participation = dict(s.gameplay_event_participation)
            active_map_id = str(map_id or s.gameplay_map or '')
            active_access_group_id = str(
                map_access_group_id
                or getattr(
                    s,
                    'gameplay_map_access_group_id',
                    None,
                )
                or s.gameplay_map_access_id
                or active_map_id
            )
        if character is None:
            raise ValueError("event initialization requires a character")
        player_level = max(1, int(getattr(character, "level", 1)))
        scheduled_update = catalog.event_update()
        try:
            quest_reveal_flags = tuple(
                CharacterService._quest_reveal_flags(
                    self, s, active_map_id,
                )
            )
        except Exception:
            quest_reveal_flags = ()
        local_entries = _baseline_map_local_event_entries(
            self.static_database_path,
            active_map_id,
            (active_access_group_id, *quest_reveal_flags),
        )
        scheduled_names = {
            entry.event_name.casefold()
            for entry in scheduled_update.events
        }
        local_entries = tuple(local_entries) + tuple(
            CharacterService._running_map_event_entries(s)
        ) + _declared_map_local_event_entries(
            self.static_database_path, active_map_id
        )
        seen_local: set[str] = set()
        update = EventUpdate((
            *scheduled_update.events,
            *(
                entry for entry in local_entries
                if entry.event_name.casefold() not in scheduled_names
                and not (
                    entry.event_name.casefold() in seen_local
                    or seen_local.add(entry.event_name.casefold())
                )
            ),
        ))
        if os.getenv("DSO_NPC_TRACE") == "1":
            local_state = [
                f"{entry.event_name}={'on' if entry.flags[0] else 'off'}"
                for entry in update.events if entry.flags[1]
            ]
            print(
                f"[npc-trace] init EventUpdate {active_map_id}: "
                f"{len(update.events)} entries; map-local: {local_state}"
            )
        return (
            update,
            catalog.progress_info(progress, player_level=player_level),
            catalog.participation_info(participation),
        )

    @staticmethod
    def _restore_event_state(s: Session, persisted_state) -> None:
        s.gameplay_event_progress = (
            copy.deepcopy(persisted_state.event_progress)
            if persisted_state is not None else {}
        )
        s.gameplay_event_participation = (
            dict(persisted_state.event_participation)
            if persisted_state is not None else {}
        )
        s.gameplay_pending_travel_item = None

    def _arm_travel_item(self, s: Session, inventory_item) -> bool:
        if inventory_item is None:
            return False
        catalog = CharacterService._event_catalog(self)
        definition = catalog.travel_item(inventory_item.template_id)
        if definition is None:
            return False
        character = s.gameplay_character
        player_level = max(1, int(getattr(character, "level", 1)))
        if not catalog.travel_item_available(
            definition, player_level=player_level
        ):
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected inactive or "
                f"level-locked TravelItem {inventory_item.template_id!r}"
            )
            return True
        with s.flow_lock:
            s.gameplay_pending_travel_item = {
                "item_id": int(inventory_item.item_id),
                "template_id": str(inventory_item.template_id),
                "target_map_id": str(definition.target_map_id),
                "expires_at": time.perf_counter() + 15.0,
            }
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] armed TravelItem "
            f"{inventory_item.template_id!r} -> "
            f"{definition.target_map_id!r}"
        )
        return True

    def _handle_active_events_request(
        self, s: Session, payload: bytes
    ) -> None:
        if (
            len(payload) < 3
            or payload[0] != 0x8B
            or int.from_bytes(payload[1:3], "little")
            != ACTIVE_EVENTS_COMMAND
        ):
            raise ValueError("ActiveEvents request has an invalid header")
        update, progress, participation = self._event_initialization_state(s)
        update_packet, update_bits = encode_event_update(update)
        CharacterService._send_world_loot_state_batch(
            s, update_packet, update_bits, label="active-events-update"
        )
        packet, packet_bits = encode_server_command_batch((
            ServerCommand(
                EVENT_PROGRESS_INFO_COMMAND,
                progress,
                LOCAL_PLAYER_ACTOR_ID,
            ),
            ServerCommand(
                EVENT_PARTICIPATION_INFO_COMMAND,
                participation,
                LOCAL_PLAYER_ACTOR_ID,
            ),
        ), include_terminator=True)
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label="active-events-state"
        )
        CharacterService._accept_gameplay_state_command(
            s, ACTIVE_EVENTS_COMMAND
        )

    def _handle_set_event_participation(
        self, s: Session, payload: bytes
    ) -> None:
        reader = BitReader(payload, len(payload) * 8)
        if reader.read_uint8() != 0x8B:
            raise ValueError("event participation is not a client command")
        command_id = int.from_bytes(reader.read_bytes(2), "little")
        if command_id != SET_EVENT_PARTICIPATION_COMMAND:
            raise ValueError(
                f"expected SetEventParticipation 0x0078, got 0x{command_id:04x}"
            )
        event_id = reader.read_uint32_le()
        participating = reader.read_bool()
        if reader.remaining:
            padding = reader.remaining
            if padding >= 8 or reader.read_bits(padding):
                raise ValueError("SetEventParticipation has trailing data")
        catalog = CharacterService._event_catalog(self)
        event = next(
            (
                value for value in catalog.active_events()
                if value.event_id == event_id
            ),
            None,
        )
        if event is None:
            raise ValueError(f"event {event_id} is not active")
        with s.flow_lock:
            participation_state = dict(s.gameplay_event_participation)
            participation_state[str(event_id)] = bool(participating)
            s.gameplay_event_participation = participation_state
        snapshot = catalog.participation_info(participation_state)
        packet, packet_bits = encode_server_command_batch((
            ServerCommand(
                EVENT_PARTICIPATION_INFO_COMMAND,
                snapshot,
                LOCAL_PLAYER_ACTOR_ID,
            ),
        ), include_terminator=True)
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label="event-participation"
        )
        CharacterService._request_account_save(
            s, "event participation changed"
        )

    def _advance_event_progress(
        self,
        s: Session,
        *,
        collectible_id: str,
        amount: int,
    ) -> bool:
        catalog = CharacterService._event_catalog(self)
        with s.flow_lock:
            progress = copy.deepcopy(s.gameplay_event_progress)
            character = copy.deepcopy(s.gameplay_character)
        if character is None:
            return False
        class_name = CLASS_NAMES.get(int(character.class_id), "Mage")
        advance = catalog.advance(
            progress,
            collectible_id=collectible_id,
            amount=max(0, int(amount)),
            player_level=max(1, int(character.level)),
            class_name=class_name,
        )
        if advance is None:
            return False
        progress[str(advance.event.event_id)] = advance.progress
        with s.flow_lock:
            s.gameplay_event_progress = progress

        changed = EventProgressChanged((
            int(advance.event.event_id),
            int(advance.progress["amount"]),
            int(advance.progress["level"]),
            int(advance.progress["currency_level"]),
            int(advance.progress["group"]),
        ))
        packet, packet_bits = encode_server_command_batch((
            ServerCommand(
                EVENT_PROGRESS_CHANGED_COMMAND,
                changed,
                LOCAL_PLAYER_ACTOR_ID,
            ),
        ), include_terminator=True)
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label="event-progress"
        )

        item_definitions = tuple(
            RewardDefinition(
                f"event:{advance.event.event_id}:{reward.key}",
                reward.item_for_class(class_name),
                reward.amount,
            )
            for reward in advance.item_rewards
            if reward.item_for_class(class_name)
        )
        if item_definitions:
            CharacterService._deliver_rewards(
                self,
                s,
                item_definitions,
                claim_keys=None,
                label=f"event-{advance.event.event_id}-reward",
                enchant_equipment=True,
            )

        event_currency = sum(
            reward.amount for reward in advance.currency_rewards
        )
        if event_currency:
            with s.flow_lock:
                current = s.gameplay_currency_state
                if current is None:
                    raise ValueError("event currency state is unavailable")
                values = (
                    int(current.values[0]),
                    int(current.values[1]),
                    min(UINT32_MAX, int(current.values[2]) + event_currency),
                )
                updated = CurrencyChanged(
                    int(current.currency_type),
                    int(current.amount),
                    values,
                    int(current.delta),
                    False,
                    current.wallet_base,
                )
                s.gameplay_currency_state = updated
            packet, packet_bits = encode_server_command_batch((
                ServerCommand(
                    CURRENCY_CHANGED_COMMAND,
                    updated,
                    LOCAL_PLAYER_ACTOR_ID,
                ),
            ), include_terminator=True)
            CharacterService._send_world_loot_state_batch(
                s, packet, packet_bits, label="event-currency-reward"
            )

        CharacterService._request_account_save(s, "event progress changed")
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] event {advance.event.event_id} "
            f"progress +{amount} {collectible_id!r} -> "
            f"{advance.progress['amount']}; item rewards="
            f"{[(value.item_id, value.amount) for value in item_definitions]}, "
            f"Draken={event_currency}"
        )
        return True


__all__ = ["EventService", "SET_EVENT_PARTICIPATION_COMMAND"]
