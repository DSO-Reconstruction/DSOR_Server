"""Event quest eligibility follows the live schedule and local event runtime."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dso_server.gameplay import event_system
from dso_server.gameplay.quest_content import (
    QUEST_STATE_CLOSED, QUEST_STATE_FINISHED, QuestProgress, load_quest_catalog,
)
from dso_server.paths import DEFAULT_QUESTS_PATH
from dso_server.runtime import file_revisions
from dso_server.services import events as events_service
from dso_server.tests import test_quests as quest_fixtures

START = datetime(2026, 9, 27, tzinfo=timezone.utc)
END = START + timedelta(days=1)
DESERT = "e-doe-02-introduction-01-n"
SARGON = "e-a0200-sar-kill-monster-01"
HALLOWEEN = "e-a0200-hal-2012-event-introduction-01"


class EventRequirementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_quest_catalog(DEFAULT_QUESTS_PATH)

    def test_all_authored_event_quests_require_a_matching_event(self):
        quests = [q for q in self.catalog.quests.values() if q.event_id]
        self.assertEqual(len(quests), 415)
        for quest in quests:
            with self.subTest(quest=quest.quest_id):
                self.assertFalse(quest.event_requirement_met(()))
                self.assertFalse(quest.event_requirement_met(("unrelated_event",)))
                self.assertTrue(quest.event_requirement_met(
                    (quest.event_id.split(";")[0],),
                ))

    def test_alternative_events_are_any_of_and_whitespace_is_ignored(self):
        quest = self.catalog.quest(SARGON)
        for event in quest.event_id.split(";"):
            self.assertTrue(self.catalog.available(
                quest, class_name="ranger", level=20, closed_quests=(),
                known_quests=(), active_events=(event.upper(),),
            ))
        self.assertTrue(self.catalog.quest(HALLOWEEN).event_requirement_met(
            (" HALLOWEEN_2012 ",),
        ))

    def test_ordinary_quests_need_no_event(self):
        quest = self.catalog.quest(quest_fixtures.TUTORIAL_01)
        self.assertTrue(self.catalog.available(
            quest, class_name="ranger", level=1,
            closed_quests=(), known_quests=(),
        ))

    def test_active_event_does_not_bypass_other_requirements(self):
        quest = self.catalog.quest("e-doe-02-map-01-gather-keys-01-n")
        facts = dict(class_name="ranger", level=20, closed_quests=quest.required,
                     known_quests=(), active_events=(quest.event_id,))
        self.assertTrue(self.catalog.available(quest, **facts))
        self.assertFalse(self.catalog.available(quest, **{**facts, "level": 1}))
        self.assertFalse(self.catalog.available(quest, **{**facts, "closed_quests": ()}))
        self.assertFalse(self.catalog.available(quest, **{**facts, "known_quests": (quest.quest_id,)}))


class EventQuestServiceTests(unittest.TestCase):
    _session = quest_fixtures.QuestServiceTests._session

    def setUp(self):
        quest_fixtures.QuestServiceTests.setUp(self)
        self.session.gameplay_character.level = 20
        directory = tempfile.TemporaryDirectory(prefix="dso-quest-events-")
        self.addCleanup(directory.cleanup)
        self.schedule = Path(directory.name) / "events.json"
        patcher = patch.object(events_service, "EVENT_SCHEDULE_PATH", self.schedule)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(event_system, "datetime", wraps=datetime)
        self.clock = patcher.start()
        self.addCleanup(patcher.stop)
        self.clock.now.return_value = START
        self.write_schedule("desert_of_essences_02")

    def write_schedule(self, template=None, **overrides):
        entries = [] if template is None else [{
            "event_id": 98765, "template_id": template, "state": "scheduled",
            "starts_at": START.isoformat(), "ends_at": END.isoformat(),
            **overrides,
        }]
        self.schedule.write_text(json.dumps({"events": entries}), encoding="utf-8")
        # Schedule writers publish their edit (as the CMS does); lookups
        # otherwise reuse the last observed revision for up to a second.
        file_revisions.invalidate(self.schedule)

    def offered(self, quest_id):
        quest = self.server._quest_catalog().quest(quest_id)
        interactions, _flags = self.server._quest_npc_interactions(self.session, quest.giver)
        return any(item.source == quest_id for item in interactions)

    def test_schedule_boundaries_control_offers_and_direct_acceptance(self):
        for moment, available in (
            (START - timedelta(microseconds=1), False),
            (START, True),
            (END - timedelta(microseconds=1), True),
            (END, False),
        ):
            with self.subTest(moment=moment):
                self.clock.now.return_value = moment
                self.session.gameplay_quest_log.clear()
                self.assertEqual(self.offered(DESERT), available)
                self.assertEqual(self.server._quest_accept(
                    self.session, DESERT, offered=True,
                ), available)
                self.assertEqual(DESERT in self.session.gameplay_quest_log, available)

    def test_unscheduled_event_is_unavailable(self):
        self.write_schedule()
        self.assertFalse(self.offered(DESERT))
        self.assertFalse(self.server._quest_accept(self.session, DESERT, offered=True))

    def test_schedule_edits_and_forced_states_are_applied_without_relogging(self):
        for state, available in (("active", True), ("expired", False), ("upcoming", False)):
            with self.subTest(state=state):
                self.write_schedule("desert_of_essences_02", state=state)
                self.assertEqual(self.offered(DESERT), available)
                self.assertEqual(self.server._quest_accept(
                    self.session, DESERT, offered=True,
                ), available)
                self.session.gameplay_quest_log.clear()

    def test_expired_map_entry_flags_cannot_keep_quests_available(self):
        self.runtime.initial_flags |= frozenset({"desert_of_essences_02"})
        # A session may also retain an older catalog snapshot after an edit.
        self.session.gameplay_event_catalog = self.server._event_catalog()
        self.write_schedule()
        self.assertFalse(self.offered(DESERT))
        self.assertFalse(self.server._quest_accept(self.session, DESERT, offered=True))

    def test_repeatable_quest_can_reopen_only_during_a_matching_event(self):
        closed = QuestProgress(SARGON, state=QUEST_STATE_CLOSED)
        self.session.gameplay_quest_log[SARGON] = closed
        self.assertFalse(self.offered(SARGON))
        self.assertFalse(self.server._quest_accept(self.session, SARGON, offered=True))
        self.assertIs(self.session.gameplay_quest_log[SARGON], closed)
        self.write_schedule("sargon_event_04")
        self.assertTrue(self.offered(SARGON))
        self.assertTrue(self.server._quest_accept(self.session, SARGON, offered=True))

    def test_auto_start_cannot_bypass_an_inactive_event(self):
        quest = self.server._quest_catalog().quest("e-easter-maprun-01-re")
        map_id = "e_easter_dun_01"
        self.session.gameplay_quest_log["e-easter-introduction-02-n"] = QuestProgress(
            "e-easter-introduction-02-n", state=QUEST_STATE_CLOSED)
        self.session.gameplay_map = map_id
        self.session.gameplay_map_access_group_id = map_id
        self.runtime = self.server._attach_map_event_runtime(
            self.session, map_id=map_id, initial_flags=frozenset(),
            monster_level=None, scaling_difficulty=0, fresh_instance=True)
        self.server._quest_on_signal(self.session, quest.start_signal)
        self.assertNotIn(quest.quest_id, self.session.gameplay_quest_log)
        self.write_schedule("easter_event_01")
        self.server._quest_on_signal(self.session, quest.start_signal)
        self.assertIn(quest.quest_id, self.session.gameplay_quest_log)

    def test_schedule_tags_can_enable_authored_quest_event_aliases(self):
        self.write_schedule("halloween_event_01", tags=["halloween_2012"])
        self.assertTrue(self.offered(HALLOWEEN))
        self.assertTrue(self.server._quest_accept(self.session, HALLOWEEN, offered=True))

    def test_running_local_event_enables_its_quest_until_it_stops(self):
        map_id = "a0200_kingscity"
        self.session.gameplay_map = map_id
        self.session.gameplay_map_access_group_id = map_id
        runtime = self.server._attach_map_event_runtime(
            self.session, map_id=map_id, initial_flags=frozenset(),
            monster_level=None, scaling_difficulty=0, fresh_instance=True,
        )
        quest_id = "e-bonus-hh-n"
        definition = runtime.catalog.event("e_bonus_event_01")
        self.assertIsNotNone(definition)
        self.assertFalse(self.offered(quest_id))
        runtime.begin_event(definition)
        self.assertTrue(self.offered(quest_id))
        runtime.end_event(definition)
        self.assertFalse(self.offered(quest_id))
        self.assertFalse(self.server._quest_accept(self.session, quest_id, offered=True))

    def test_expiry_preserves_accepted_progress_and_allows_turn_in(self):
        self.write_schedule("halloween_2012")
        self.assertTrue(self.server._quest_accept(self.session, HALLOWEEN, offered=True))
        progress = self.session.gameplay_quest_log[HALLOWEEN]
        self.clock.now.return_value = END
        self.assertIs(self.session.gameplay_quest_log[HALLOWEEN], progress)
        quest = self.server._quest_catalog().quest(HALLOWEEN)
        self.server._quest_feed(self.session, "encounter", quest.goal)
        self.assertEqual(progress.state, QUEST_STATE_FINISHED)
        interactions, _ = self.server._quest_npc_interactions(self.session, quest.goal)
        self.assertIn(HALLOWEEN, [item.source for item in interactions])
        # Verify eligibility without depending on inventory/currency fixtures.
        with patch.object(type(self.server), "_quest_grant_rewards") as grant:
            self.assertTrue(self.server._quest_turn_in(self.session, HALLOWEEN))
            grant.assert_called_once()
        self.assertEqual(progress.state, QUEST_STATE_CLOSED)
