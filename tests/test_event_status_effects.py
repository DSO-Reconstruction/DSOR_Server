"""Active game events grant their authored buffs on every map entry.

``_Template_Events.EventStatusEffects`` names the durable effects an event
gives players (``xpgain,$0:0.25`` for the XP bonus event). They are applied
as ordinary persistent entries so combat, XP awards and the client all see
them, but they are rebuilt from the schedule each initialization and never
saved, so nothing outlives the event.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from dso_server.gameplay.event_system import (
    event_status_effect_specs,
    event_status_entry,
    load_event_catalog,
    parse_event_status_effects,
)
from dso_server.gameplay.status_modifier_runtime import (
    active_xp_gain_multiplier,
    snapshot_active_status_entries,
)
from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.runtime.transport import Session
from dso_server.services import events as events_module
from dso_server.tests.fixtures.characters import character_server


NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def write_schedule(directory: str, *, xp_state: str = "active") -> Path:
    schedule = Path(directory) / "events.json"
    schedule.write_text(json.dumps({"events": [
        {"event_id": 35241, "template_id": "xp_bonus_event_01",
         "state": xp_state, "starts_at": "2026-09-24T00:00:00Z",
         "ends_at": "2026-10-24T23:59:59Z"},
        {"event_id": 35240, "template_id": "newmoon_event_02",
         "state": "active"},
    ]}), encoding="utf-8")
    return schedule


class ParseTests(unittest.TestCase):
    def test_parameter_assignments_bind_to_the_preceding_effect(self):
        parsed = parse_event_status_effects(("xpgain", "$0:0.25"))
        self.assertEqual(parsed, (("xpgain", (0.25, 0.0, 0.0, 0.0, 0.0)),))

    def test_several_effects_keep_their_own_parameters(self):
        parsed = parse_event_status_effects(
            ("first", "$0:1", "$2:3", "second", "$bad", "$1:x", "third")
        )
        self.assertEqual(parsed, (
            ("first", (1.0, 0.0, 3.0, 0.0, 0.0)),
            ("second", (0.0,) * 5),
            ("third", (0.0,) * 5),
        ))

    def test_leading_assignment_without_an_effect_is_ignored(self):
        self.assertEqual(parse_event_status_effects(("$0:1",)), ())


class SpecTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.schedule = write_schedule(directory.name)
        self.catalog = load_event_catalog(DEFAULT_STATIC_DB_PATH, self.schedule)
        self.xpgain_id = load_status_runtime_catalog(
            DEFAULT_STATIC_DB_PATH
        ).effect_ids["xpgain"]

    def test_active_xp_event_resolves_to_the_xpgain_effect(self):
        specs = event_status_effect_specs(
            self.catalog, DEFAULT_STATIC_DB_PATH, now=NOW, player_level=50
        )
        self.assertEqual([spec.resource_id for spec in specs], ["xpgain"])
        self.assertEqual(specs[0].effect_id, self.xpgain_id)
        self.assertEqual(specs[0].values[0], 0.25)

    def test_template_level_window_is_honoured(self):
        self.assertEqual(
            event_status_effect_specs(
                self.catalog, DEFAULT_STATIC_DB_PATH, now=NOW, player_level=10
            ),
            (),
        )

    def test_inactive_events_are_listed_only_on_request(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        expired = load_event_catalog(
            DEFAULT_STATIC_DB_PATH,
            write_schedule(directory.name, xp_state="expired"),
        )
        self.assertEqual(
            event_status_effect_specs(expired, DEFAULT_STATIC_DB_PATH, now=NOW),
            (),
        )
        self.assertEqual(
            [spec.effect_id for spec in event_status_effect_specs(
                expired, DEFAULT_STATIC_DB_PATH, include_inactive=True
            )],
            [self.xpgain_id],
        )

    def test_entry_feeds_the_xp_multiplier(self):
        (spec,) = event_status_effect_specs(
            self.catalog, DEFAULT_STATIC_DB_PATH, now=NOW, player_level=50
        )
        entry = event_status_entry(spec, instance_id=77, actor_id=1)
        self.assertEqual(entry.field_04, 77)
        self.assertTrue(entry.has_details)
        multiplier = active_xp_gain_multiplier(
            DEFAULT_STATIC_DB_PATH,
            snapshot_active_status_entries({77: entry}, {}, {}),
        )
        self.assertAlmostEqual(multiplier, 1.25)


class FakeSocket:
    def __init__(self) -> None:
        self.sent = []

    def sendto(self, data, address) -> None:
        self.sent.append((data, address))


class InitializationTests(unittest.TestCase):
    """Whole initialization with the level-100 fixture character."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.object(
            events_module, "EVENT_SCHEDULE_PATH", write_schedule(directory.name)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = character_server(self)
        self.server.sock = FakeSocket()
        self.xpgain_id = load_status_runtime_catalog(
            DEFAULT_STATIC_DB_PATH
        ).effect_ids["xpgain"]

    def _initialize(self, port: int) -> Session:
        session = Session(("127.0.0.1", port), self.server.sock)
        session.gameplay_mode = True
        session.gameplay_map = "a0200_kingscity"
        session.gameplay_character = (
            self.server.character_selection.active_character
        )
        session.gameplay_template_row_index = (
            self.server.player_template_rows[
                session.gameplay_character.class_id
            ]
        )
        self.server._send_local_player_initialization(session)
        return session

    def test_xp_bonus_is_granted_but_never_saved(self):
        session = self._initialize(40700)
        event_entries = [
            entry for entry in session.gameplay_persistent_status_effects.values()
            if entry.effect_id == self.xpgain_id
        ]
        self.assertEqual(len(event_entries), 1)
        (entry,) = event_entries
        self.assertEqual(entry.values[0], 0.25)
        self.assertEqual(
            session.gameplay_event_status_effect_instance_ids,
            frozenset({entry.field_04}),
        )
        multiplier = active_xp_gain_multiplier(
            DEFAULT_STATIC_DB_PATH,
            snapshot_active_status_entries(
                session.gameplay_persistent_status_effects,
                session.gameplay_timed_status_effect_expiries,
                session.gameplay_runtime_status_effects,
            ),
        )
        self.assertAlmostEqual(multiplier, 1.25)
        snapshot = self.server._capture_account_state(session)
        self.assertIsNotNone(snapshot)
        self.assertNotIn(
            self.xpgain_id,
            {effect.effect_id for effect in snapshot.status_effects},
        )

    def test_reinitializing_does_not_stack_the_buff(self):
        first = self._initialize(40701)
        second = self._initialize(40702)
        for session in (first, second):
            self.assertEqual(
                sum(
                    1 for entry in session.gameplay_persistent_status_effects.values()
                    if entry.effect_id == self.xpgain_id
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()
