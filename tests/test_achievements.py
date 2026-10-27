"""Achievement definitions, the progress engine, and the service flow.

The retail reference is the tutorial run of 2026-09-09
(docs/QUEST_PROTOCOL.md §11): ``lore_enter_grimford_01`` arrived as
``(1, 2026, 26, 13, 14, 58, 0, 6, 9)`` when Grimford was entered.
"""

from __future__ import annotations

from datetime import datetime
import threading
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import patch

from dso_server.gameplay.achievement_content import (
    AchievementCatalog,
    AchievementDefinition,
    AchievementProgress,
    apply_achievement_event,
    complete_achievement,
    load_achievement_catalog,
    refresh_derived_achievements,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence.account_state import (
    AccountState,
    _state_from_document,
    _state_to_document,
)
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.player_init_protocol import (
    ACHIEVEMENT_INFO_COMMAND,
    CurrencyChanged,
    QuickbarInfo,
    decode_server_command_batch,
)
from dso_server.protocol.quest_protocol import CHAPTER_UNLOCKED_COMMAND
from dso_server.runtime.transport import Session
from dso_server.services.achievements import AchievementService
from dso_server.services.character_server import CharacterServer
from dso_server.tests.test_pvp_matchmaking import _FakeSocket, _Recorder, _quiet_server

GRIMFORD = "a0006_grimford_hub"
GRIMFORD_ACHIEVEMENT = "lore_enter_grimford_01"
RETAIL_GRIMFORD_VALUES = (1, 2026, 26, 13, 14, 58, 0, 6, 9)
RETAIL_MOMENT = datetime(2026, 9, 26, 13, 14, 58)


def _definition(achievement_id, kind, params, amount=1, *, points=10, prerequisites=()):
    # Params are ';' lists in the template, like "Ranger;" or "a;b;".
    tokens = tuple(
        t.strip().casefold() for p in params for t in p.split(";") if t.strip()
    )
    return AchievementDefinition(
        achievement_id=achievement_id, kind=kind,
        params=tokens, amount=amount, points=points,
        prerequisites=tuple(p.casefold() for p in prerequisites), category="test",
        hidden=False, account_wide=False, event_id="", unlocks_title=False,
        reward_item="", reward_amount=0,
    )


def _catalog(*definitions):
    by_kind = {}
    for definition in definitions:
        if definition.event_kind:
            by_kind.setdefault(definition.event_kind, []).append(definition)
    return AchievementCatalog(
        MappingProxyType({d.key: d for d in definitions}),
        MappingProxyType({k: tuple(v) for k, v in by_kind.items()}),
    )


class AchievementCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_achievement_catalog(DEFAULT_STATIC_DB_PATH)

    def test_catalog_shape(self):
        grimford = self.catalog.achievement(GRIMFORD_ACHIEVEMENT)
        self.assertEqual(grimford.kind, "enter_map")
        self.assertEqual(grimford.params, (GRIMFORD,))
        self.assertEqual((grimford.target, grimford.points), (1, 10))
        self.assertIn(grimford, self.catalog.for_event("enter_map"))
        # 'nomob' kill rules have nothing the server can count.
        self.assertTrue(all(
            "nomob" not in d.params for d in self.catalog.for_event("kill")
        ))
        self.assertIsNotNone(self.catalog.achievement("class_create_ranger_lvl_20"))
        self.assertEqual(self.catalog.achievement("general_score_50").kind, "points")

    def test_wire_values_match_retail_record(self):
        progress = AchievementProgress(1, "2026-09-26 13:14:58")
        self.assertEqual(progress.wire_values(), RETAIL_GRIMFORD_VALUES)
        self.assertEqual(AchievementProgress(3).wire_values(), (3, 0, 0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(
            AchievementProgress.from_document(progress.to_document()), progress,
        )

    def test_enter_map_completes_once(self):
        state = {}
        changes = apply_achievement_event(
            self.catalog, state, "enter_map", (GRIMFORD, GRIMFORD, ""), now=RETAIL_MOMENT,
        )
        completed = [c for c in changes if c.completed_now]
        self.assertEqual([c.definition.achievement_id for c in completed], [GRIMFORD_ACHIEVEMENT])
        self.assertEqual(completed[0].progress.wire_values(), RETAIL_GRIMFORD_VALUES)
        # The score milestones move with the completed points.
        self.assertEqual(state["general_score_50"].progress, 10)
        self.assertEqual(
            apply_achievement_event(self.catalog, state, "enter_map", (GRIMFORD,)), (),
        )


class AchievementEngineTests(unittest.TestCase):
    def test_counting_and_prerequisites(self):
        first = _definition("kill_a", "kill", ("mob_a",), 5)
        second = _definition("kill_b", "kill", ("mob_a", "mob_b"), 3, prerequisites=("kill_a",))
        catalog = _catalog(first, second)
        state = {}
        changes = apply_achievement_event(catalog, state, "kill", ("mob_a",), count=3)
        self.assertEqual(
            [(c.definition.achievement_id, c.progress.progress, c.completed_now) for c in changes],
            [("kill_a", 3, False), ("kill_b", 3, False)],
        )
        # kill_b reached its target but waits for kill_a.
        self.assertFalse(state["kill_b"].completed)
        changes = apply_achievement_event(catalog, state, "kill", ("mob_a",), count=2)
        self.assertEqual(
            [(c.definition.achievement_id, c.completed_now) for c in changes],
            [("kill_a", True), ("kill_b", True)],
        )
        self.assertEqual(apply_achievement_event(catalog, state, "kill", ("mob_a",)), ())

    def test_class_level_and_derived_rules(self):
        ranger = _definition("ranger_20", "class_level", ("Ranger;",), 20)
        mage = _definition("mage_20", "class_level", ("Mage;",), 20)
        score = _definition("score_20", "points", ("score",), 20, points=0)
        both = _definition("both", "unlockachievements", ("ranger_20", "mage_20"), 2, points=5)
        catalog = _catalog(ranger, mage, score, both)
        state = {}
        changes = apply_achievement_event(catalog, state, "class_level", ("ranger", "12"))
        self.assertEqual([(c.definition.achievement_id, c.progress.progress) for c in changes], [("ranger_20", 12)])
        self.assertNotIn("mage_20", state)
        changes = apply_achievement_event(catalog, state, "class_level", ("ranger", "20"))
        self.assertEqual(
            [(c.definition.achievement_id, c.progress.progress, c.completed_now) for c in changes],
            [("ranger_20", 20, True), ("score_20", 10, False), ("both", 1, False)],
        )
        changes = apply_achievement_event(catalog, state, "class_level", ("mage", "45"))
        ids = [(c.definition.achievement_id, c.completed_now) for c in changes]
        self.assertEqual(ids[0], ("mage_20", True))
        self.assertIn(("score_20", True), ids)
        self.assertIn(("both", True), ids)
        # both carries 5 points on top: 25 total, capped at the score target.
        self.assertEqual(state["score_20"].progress, 20)

    def test_complete_outright_is_idempotent(self):
        rule = _definition("recipe_gate", "crafting", (), 1)
        state = {}
        self.assertTrue(complete_achievement(state, rule, now=RETAIL_MOMENT))
        self.assertFalse(complete_achievement(state, rule))
        self.assertEqual(state["recipe_gate"].wire_values()[1:], RETAIL_GRIMFORD_VALUES[1:])
        self.assertEqual(refresh_derived_achievements(_catalog(rule), state), ())


def _account_state(identity, **values):
    return AccountState(
        character_id=identity, map_id="a0200_kingscity", position=None,
        inventory=InventoryInfo(), currency=CurrencyChanged(0, 0, (0, 0, 0), 0, False),
        currency_ic=0, next_item_id=1, quickbar_info=QuickbarInfo(0, 0),
        timed_offer_purchases={}, status_effects=(), mount_ride=None, **values,
    )


class AchievementServiceTests(unittest.TestCase):
    def setUp(self):
        self.server = _quiet_server(self)
        self.server.sessions = {}
        self.server.gameplay_world_lock = threading.RLock()
        self.saves = []
        patcher = patch.object(
            CharacterServer, "_request_account_save",
            lambda s, reason, **kwargs: self.saves.append(reason),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session = self._session(50201)

    def _session(self, port):
        session = Session(("127.0.0.1", port), _FakeSocket())
        session.connected = True
        session.gameplay_mode = True
        session.gameplay_map = GRIMFORD
        session.gameplay_character = SimpleNamespace(
            character_id=port, name="P", level=1, class_id=2,
        )
        session.gameplay_achievements = {}
        session.gameplay_chapters_unlocked = set()
        recorder = _Recorder()
        session.send_reliable_ordered = recorder
        session.send_reliable_ordered_split = (
            lambda payload, **kwargs: recorder(payload, label=kwargs.get("label_prefix", "split"))
        )
        session.recorder = recorder
        return session

    def _records(self, session, since=0):
        out = []
        for data, bits, _label in session.recorder.messages[since:]:
            if data[0] != 0x85:
                continue
            for command in decode_server_command_batch(data, bits):
                if command.command_id == ACHIEVEMENT_INFO_COMMAND:
                    self.assertEqual(len(command.value.achievements), 1)
                    out.append(command.value.achievements[0])
        return out

    def test_enter_map_sends_completion_once_and_persists(self):
        server, s = self.server, self.session
        server._achievement_on_map_entered(s, (GRIMFORD, GRIMFORD, ""))
        records = self._records(s)
        grimford = [r for r in records if r.resource_id == GRIMFORD_ACHIEVEMENT]
        self.assertEqual(len(grimford), 1)
        self.assertEqual(grimford[0].values[0], 1)
        self.assertTrue(grimford[0].values[1] >= 2026)
        self.assertIn("achievement-enter_map", self.saves)
        # The same login replays nothing.
        count = len(s.recorder.messages)
        server._achievement_on_map_entered(s, (GRIMFORD,))
        self.assertEqual(len(s.recorder.messages), count)
        # A later login restores the same stamp and stays quiet as well.
        document = AchievementService._achievement_document(s)
        self.assertIn(GRIMFORD_ACHIEVEMENT, document)
        other = self._session(50202)
        AchievementService._restore_achievement_state(
            other, SimpleNamespace(achievements=document, chapters_unlocked=()),
        )
        info = server._achievement_init_info(other)
        restored = {a.resource_id: a.values for a in info.achievements}
        self.assertEqual(restored[GRIMFORD_ACHIEVEMENT], grimford[0].values)
        server._achievement_on_map_entered(other, (GRIMFORD,))
        self.assertEqual(self._records(other), [])

    def test_initialization_preserves_earned_gates_without_free_unlocks(self):
        server, s = self.server, self.session
        first = {a.resource_id: a.values for a in server._achievement_init_info(s).achievements}
        self.assertEqual(first, {})
        catalog = server._achievement_catalog()
        definition = next(d for d in catalog.achievements.values() if d.kind == "crafting")
        complete_achievement(s.gameplay_achievements, definition, now=RETAIL_MOMENT)
        second = {a.resource_id: a.values for a in server._achievement_init_info(s).achievements}
        self.assertEqual(second[definition.achievement_id][1:], RETAIL_GRIMFORD_VALUES[1:])
        self.assertEqual(self.saves, [])

    def test_kill_quest_and_level_hooks(self):
        server, s = self.server, self.session
        server._achievement_on_monsters_killed(
            s, (SimpleNamespace(template_id="a0404_normal_boss_cultistleader"),),
        )
        self.assertIn("general_kill_khalys_01", [r.resource_id for r in self._records(s)])
        since = len(s.recorder.messages)
        server._achievement_on_quest_closed(s, "a0200-HeroesResurrection-02-n")
        self.assertIn(
            "lore_quest_a0200-HeroesResurrection-02-n",
            [r.resource_id for r in self._records(s, since)],
        )
        since = len(s.recorder.messages)
        server._handle_presence_event(s, ("level_up", 2, 20))
        ranger = [r for r in self._records(s, since) if r.resource_id == "class_create_ranger_lvl_20"]
        self.assertEqual(ranger[0].values[0], 20)
        self.assertTrue(ranger[0].values[1] > 0)

    def test_chapter_unlocked_once_per_character(self):
        server, s = self.server, self.session
        commands = server._chapter_unlock_commands(s, "act1_chapter01")
        self.assertEqual(
            [(c.command_id, c.value.chapter_id) for c in commands],
            [(CHAPTER_UNLOCKED_COMMAND, "act1_chapter01")],
        )
        self.assertEqual(server._chapter_unlock_commands(s, "ACT1_chapter01"), ())
        self.assertEqual(AchievementService._chapters_unlocked_document(s), ("act1_chapter01",))
        other = self._session(50203)
        AchievementService._restore_achievement_state(
            other, SimpleNamespace(achievements={}, chapters_unlocked=("act1_chapter01",)),
        )
        self.assertEqual(server._chapter_unlock_commands(other, "act1_chapter01"), ())
        self.assertEqual(len(server._chapter_unlock_commands(other, "act1_chapter02")), 1)
        # The tutorial chapter is never announced (no retail command, no artwork).
        self.assertEqual(server._chapter_unlock_commands(other, "act_00_chapter_10"), ())
        self.assertNotIn("act_00_chapter_10", AchievementService._chapters_unlocked_document(other))

    def test_account_state_round_trip(self):
        state = _account_state(
            7,
            achievements={GRIMFORD_ACHIEVEMENT: {"progress": 1, "completed_at": "2026-09-26 13:14:58"}},
            chapters_unlocked=("act1_chapter01", "act1_chapter02"),
        )
        restored = _state_from_document(_state_to_document(state))
        self.assertEqual(restored.achievements, state.achievements)
        self.assertEqual(restored.chapters_unlocked, state.chapters_unlocked)
        legacy = _state_to_document(state)
        del legacy["achievements"], legacy["chapters_unlocked"]
        self.assertEqual(_state_from_document(legacy).achievements, {})
        self.assertEqual(_state_from_document(legacy).chapters_unlocked, ())


if __name__ == "__main__":
    unittest.main()
