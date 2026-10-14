"""Active events are announced once per login session on the first map join.

See docs/EVENT_NOTIFICATIONS.md for the client contract these tests pin:
kind 7 (``eventnonpersistent`` in ``_Template_Notifications`` row order; kind
6 rendered as the daily-reward popup on the live client), a GUID derived from
the event id, and a payload naming the event by ``eventId`` and ``templateId``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dso_server.gameplay.event_system import (
    EventTemplateDefinition,
    ScheduledEvent,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.reward_protocol import NotificationAction
from dso_server.runtime.transport import Session
from dso_server.services import commerce as commerce_module
from dso_server.services import events as events_module
from dso_server.services.character_server import CharacterServer
from dso_server.services.events import (
    EVENT_NOTIFICATION_KIND,
    EventNotificationMemory,
    event_notification_entries,
    event_notification_guid,
    event_notification_session_key,
)


NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def template(resource_id: str, *, client_only: bool = False) -> EventTemplateDefinition:
    return EventTemplateDefinition(
        resource_id=resource_id, client_name=resource_id, client_only=client_only,
        map_local=False, minimum_level=1, maximum_level=200, map_id="",
        excluded_maps=frozenset(), teleport_target="", status_effects=(),
        progress_bars=(),
    )


def scheduled(event_id: int, resource_id: str, *, state: str = "scheduled",
              starts: datetime | None = NOW - timedelta(days=1),
              ends: datetime | None = NOW + timedelta(days=1),
              client_only: bool = False) -> ScheduledEvent:
    return ScheduledEvent(
        event_id=event_id, template=template(resource_id, client_only=client_only),
        starts_at=starts, ends_at=ends, forced_state=state, tags=(),
        participation_default=True, collectible_drop_chance=1.0,
        collectible_amount=1, collectible_maps=frozenset(),
        collectible_ranks=frozenset(),
    )


class FakeCatalog:
    def __init__(self, *events: ScheduledEvent) -> None:
        self.events = events

    def active_events(self, now=None):
        moment = now or NOW
        return tuple(event for event in self.events if event.active_at(moment))


class FakeSocket:
    def __init__(self) -> None:
        self.sent = []

    def sendto(self, data, address) -> None:
        self.sent.append((data, address))


class EntryBuilderTests(unittest.TestCase):
    def test_only_active_visible_events_become_entries(self):
        catalog = FakeCatalog(
            scheduled(35239, "scaling_dungeon_kill_event_01"),
            scheduled(35238, "desert_of_essences_02", ends=NOW - timedelta(days=30)),
            scheduled(35240, "upcoming_event", starts=NOW + timedelta(days=2)),
            scheduled(35241, "e_bonus_event_01", client_only=True),
        )
        entries = event_notification_entries(catalog, now=NOW)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.kind, EVENT_NOTIFICATION_KIND)
        # Row 7 of _Template_Notifications: eventnonpersistent. Row 6 is the
        # daily-reward popup, which is what kind 6 rendered as on the client.
        self.assertEqual(entry.kind, 7)
        self.assertEqual(entry.instance_guid, event_notification_guid(35239))
        self.assertEqual(
            entry.instance_guid,
            uuid.uuid5(uuid.NAMESPACE_URL, "dso-local-event-notification:35239").bytes,
        )
        self.assertEqual(
            json.loads(entry.payload),
            {"eventId": 35239, "templateId": "scaling_dungeon_kill_event_01"},
        )
        starts = int((NOW - timedelta(days=1)).timestamp())
        ends = int((NOW + timedelta(days=1)).timestamp())
        self.assertEqual(entry.values, (35239, starts, ends))
        self.assertEqual(entry.timestamp, "0000-00-00 00:00:00")

    def test_already_sent_events_are_skipped(self):
        catalog = FakeCatalog(
            scheduled(1, "first"), scheduled(2, "second"),
        )
        entries = event_notification_entries(
            catalog, now=NOW, already_sent=frozenset({1})
        )
        self.assertEqual([json.loads(e.payload)["eventId"] for e in entries], [2])

    def test_forced_active_without_schedule_bounds_encodes_zero_times(self):
        catalog = FakeCatalog(
            scheduled(7, "forced", state="active", starts=None, ends=None)
        )
        (entry,) = event_notification_entries(catalog, now=NOW)
        self.assertEqual(entry.values, (7, 0, 0))


class MemoryTests(unittest.TestCase):
    def test_second_claim_for_the_same_session_is_empty(self):
        memory = EventNotificationMemory()
        self.assertEqual(memory.claim("session:a", (35239, 35240)), (35239, 35240))
        self.assertEqual(memory.claim("session:a", (35239, 35240)), ())
        # A newly scheduled event is still announced to a running session.
        self.assertEqual(memory.claim("session:a", (35239, 35241)), (35241,))
        self.assertEqual(memory.claim("session:b", (35239,)), (35239,))

    def test_expired_sessions_and_overflow_are_pruned(self):
        clock = {"now": 0.0}
        memory = EventNotificationMemory(
            ttl_seconds=100.0, limit=2, clock=lambda: clock["now"]
        )
        memory.claim("session:a", (1,))
        clock["now"] = 50.0
        memory.claim("session:b", (1,))
        memory.claim("session:c", (1,))
        self.assertEqual(len(memory), 2)
        self.assertEqual(memory.claim("session:a", (1,)), (1,))
        clock["now"] = 200.0
        self.assertEqual(memory.claim("session:b", (1,)), (1,))
        memory.forget("session:b")
        self.assertEqual(memory.claim("session:b", (1,)), (1,))


class SessionKeyTests(unittest.TestCase):
    def test_login_session_wins_over_character(self):
        session = Session(("127.0.0.1", 40600), object())
        session.login_session_id = "abc123"
        character = SimpleNamespace(character_id=200_000_001)
        self.assertEqual(
            event_notification_session_key(session, character), "session:abc123"
        )

    def test_loopback_falls_back_to_the_character(self):
        session = Session(("127.0.0.1", 40601), object())
        self.assertEqual(
            event_notification_session_key(session, SimpleNamespace(character_id=7)),
            "character:7",
        )
        self.assertIsNone(event_notification_session_key(session, None))


class ServiceIntegrationTests(unittest.TestCase):
    """Real static database and a temporary schedule through the service."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        schedule = Path(directory.name) / "events.json"
        schedule.write_text(json.dumps({"events": [
            {
                "event_id": 35239,
                "template_id": "scaling_dungeon_kill_event_01",
                "state": "active",
                "starts_at": "2026-09-09T00:00:00Z",
                "ends_at": "2026-10-10T23:59:59Z",
            },
            {
                "event_id": 35238,
                "template_id": "desert_of_essences_02",
                "state": "expired",
                "starts_at": "2026-08-02T00:00:00Z",
                "ends_at": "2026-08-31T23:59:59Z",
            },
        ]}), encoding="utf-8")
        patcher = patch.object(events_module, "EVENT_SCHEDULE_PATH", schedule)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = SimpleNamespace(
            static_database_path=DEFAULT_STATIC_DB_PATH, account_registry=None,
        )
        self.character = SimpleNamespace(character_id=200_000_001)

    def _session(self, port: int, login_session_id: str | None) -> Session:
        session = Session(("127.0.0.1", port), FakeSocket())
        session.login_session_id = login_session_id
        return session

    def _entries(self, session):
        return CharacterServer._event_notification_entries_for_session(
            self.service, session, self.character, now=NOW
        )

    def test_first_map_join_announces_then_transfers_stay_quiet(self):
        first = self._entries(self._session(40610, "login-1"))
        self.assertEqual(
            [json.loads(e.payload) for e in first],
            [{"eventId": 35239, "templateId": "scaling_dungeon_kill_event_01"}],
        )
        # A map transfer is a new UDP session presenting the same login GUID.
        self.assertEqual(self._entries(self._session(40611, "login-1")), ())
        # A relog rotates the GUID and is announced again.
        self.assertEqual(len(self._entries(self._session(40612, "login-2"))), 1)

    def test_loopback_without_login_announces_once_per_character(self):
        self.assertEqual(len(self._entries(self._session(40620, None))), 1)
        self.assertEqual(self._entries(self._session(40621, None)), ())

    def test_entries_encode_through_the_notification_command(self):
        from dso_server.protocol.player_init_protocol import (
            NOTIFICATION_COMMAND, Notification, ServerCommand,
            decode_server_command_batch, encode_server_command_batch,
        )

        entries = self._entries(self._session(40630, "login-3"))
        packet, bits = encode_server_command_batch(
            (ServerCommand(NOTIFICATION_COMMAND, Notification(1, entries), 0),),
            include_terminator=True,
        )
        decoded = decode_server_command_batch(packet, bits)
        notification = next(
            command.value for command in decoded
            if command.command_id == NOTIFICATION_COMMAND
        )
        self.assertEqual(notification.notification_type, 1)
        self.assertEqual(notification.entries, entries)


class ReplyTests(unittest.TestCase):
    def _reply(self, kind: int, payload: dict, subtype: int = 1) -> Session:
        session = Session(("127.0.0.1", 40640), FakeSocket())
        action = NotificationAction(
            notification_type=2, subtype=subtype, kind=kind,
            instance_guid=event_notification_guid(35239), values=(0, 0, 0),
            payload=payload, timestamp="0000-00-00 00:00:00",
        )
        service = SimpleNamespace()
        with patch.object(
            commerce_module, "decode_notification_action", return_value=action
        ):
            CharacterServer._handle_notification_action(service, session, b"")
        return session

    def test_confirm_and_decline_are_acknowledged_without_raising(self):
        for subtype in (1, 2):
            session = self._reply(
                EVENT_NOTIFICATION_KIND,
                {"eventId": 35239, "templateId": "scaling_dungeon_kill_event_01"},
                subtype=subtype,
            )
            self.assertEqual(
                session.gameplay_client_command_counts.get(0x0088), 1
            )

    def test_payload_naming_an_event_is_enough_even_with_another_kind(self):
        session = self._reply(18, {"eventId": 35239})
        self.assertEqual(session.gameplay_client_command_counts.get(0x0088), 1)

    def test_unrelated_payloads_are_still_rejected(self):
        with self.assertRaises(ValueError):
            self._reply(9, {"talent": "x"})


if __name__ == "__main__":
    unittest.main()
