"""Open the Suburb portal beside Annagreth when quest progress calls for it.

The portal (``pw000_exit_to_pw200``) is authored as a temporary gateway: its
start signals fire when the player opens pw200-cardhun-village-02-n and when
they enter the hub with that quest accepted. Once the Suburb has been
visited, the saved unlock keeps the portal.
"""

from pathlib import Path
from threading import Lock
from types import SimpleNamespace
import unittest

from dso_server.gameplay.local_map_state import load_local_map_initialization
from dso_server.gameplay.quest_content import (
    QUEST_STATE_ACCEPTED,
    QUEST_STATE_CLOSED,
    QuestProgress,
)
from dso_server.protocol.player_init_protocol import VARIATION_INFO_COMMAND
from dso_server.services.events import _baseline_map_local_event_entries
from dso_server.services.quests import QuestService


class CardhunHubGateTests(unittest.TestCase):
    def setUp(self):
        self.service = QuestService()
        self.service.static_database_path = (
            Path(__file__).resolve().parents[1] / "data/db/static.db4"
        )
        self.service._quest_active_events = lambda _session: frozenset()
        self.session = SimpleNamespace(
            flow_lock=Lock(),
            gameplay_character=SimpleNamespace(class_id=0, level=55),
            gameplay_quest_log={},
            gameplay_unlocked_maps=set(),
        )

    def assert_suburb_portal(self):
        flags = self.service._quest_reveal_flags(self.session, "pw000_hub")
        self.assertIn("pw000_exit_to_pw200", flags)
        entries = _baseline_map_local_event_entries(
            self.service.static_database_path, "pw000_hub", tuple(sorted(flags)),
        )
        event = next(e for e in entries if e.event_name == "pw000_exit_to_pw200")
        self.assertTrue(event.flags[0])
        commands, exits = load_local_map_initialization(
            self.service.static_database_path, "pw000_hub",
            active_event_flags=flags, include_inactive_event_sets=True,
        )
        exit_entry = next(
            e for e in exits.exits
            if e.exit_id == "pw000_hub_exit_to_pw200_cardhun_dun_00"
        )
        self.assertTrue(exit_entry.field_10)
        variations = [
            e for command in commands
            if command.command_id == VARIATION_INFO_COMMAND
            for e in command.value.entries
        ]
        self.assertTrue(any(e.event_set_row == 3398 for e in variations))

    def test_active_payment_quest_does_not_prematurely_open_suburb(self):
        quest_id = "pw200-cardhun-village-01-n"
        self.session.gameplay_quest_log[quest_id] = QuestProgress(
            quest_id, state=QUEST_STATE_ACCEPTED,
        )
        self.assertNotIn(
            "pw000_exit_to_pw200",
            self.service._quest_reveal_flags(self.session, "pw000_hub"),
        )

    def assert_no_suburb_portal(self):
        self.assertNotIn(
            "pw000_exit_to_pw200",
            self.service._quest_reveal_flags(self.session, "pw000_hub"),
        )

    def test_completed_payment_quest_opens_the_next_gate_not_the_portal(self):
        quest_id = "pw200-cardhun-village-01-n"
        self.session.gameplay_quest_log[quest_id] = QuestProgress(
            quest_id, state=QUEST_STATE_CLOSED,
        )
        self.assertIn(
            "pw200_way_to_cardhun_02_activate_exit_pw000",
            self.service._quest_reveal_flags(self.session, "pw000_hub"),
        )
        self.assert_no_suburb_portal()

    def test_suburb_quest_opens_the_portal_while_accepted(self):
        quest_id = "pw200-cardhun-village-02-n"
        self.session.gameplay_quest_log[quest_id] = QuestProgress(
            quest_id, state=QUEST_STATE_ACCEPTED,
        )
        self.assert_suburb_portal()

    def test_portal_closes_after_its_quest_unless_the_suburb_was_visited(self):
        quest_id = "pw200-cardhun-village-02-n"
        self.session.gameplay_quest_log[quest_id] = QuestProgress(
            quest_id, state=QUEST_STATE_CLOSED,
        )
        self.assert_no_suburb_portal()
        self.session.gameplay_unlocked_maps.add("pw200_cardhun_dun")
        self.assert_suburb_portal()

    def test_later_village_quest_alone_does_not_open_the_portal(self):
        quest_id = "pw200-cardhun-village-05-n"
        self.session.gameplay_quest_log[quest_id] = QuestProgress(
            quest_id, state=QUEST_STATE_ACCEPTED,
        )
        self.assert_no_suburb_portal()

    def test_saved_suburb_visit_restores_portal_without_quest_log(self):
        self.session.gameplay_unlocked_maps.add("pw200_cardhun_dun")
        self.assert_suburb_portal()
