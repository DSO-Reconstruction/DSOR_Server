"""Dungeon quest entry, cancellation, rewards and map-instance boundaries."""
from dataclasses import replace
import math
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dso_server.gameplay.local_combat_state import DamageResolution, LocalCombatState, PendingPlayerHit
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.gameplay.quest_content import (
    QuestProgress, QUEST_STATE_ACCEPTED, QUEST_STATE_FINISHED, QUEST_STATE_CLOSED,
    QUEST_STATE_CANCELLED, active_tasks,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.player_init_protocol import CurrencyChanged
from dso_server.protocol.quest_protocol import (
    QUEST_UPDATE_COMMAND, QUEST_MONSTER_INDICATION_INFO_COMMAND,
    QUEST_MONSTER_INDICATION_REQUEST_COMMAND, QuestIndicationRequest,
    decode_quest_batch, encode_quest_indication_request,
)
from dso_server.services.character_server import CharacterServer
from dso_server.services.player_progression import load_player_progression_catalog
from dso_server.services.quests import QuestService
from dso_server.services.map_transition import MapTransitionService
from dso_server.tests import test_quests as fixtures
from dso_server.tests.test_map_companions import Scheduler

QUEST = "bloodshed-challenge-01-n"
SLUDGE = "all_guardian_bloodshed_thing_blood_01"
MAP = "a0202_kingscrypt01"
SIGNAL = "bloodshed_activate_quest_signal"


class DungeonQuestTests(unittest.TestCase):
    _session = fixtures.QuestServiceTests._session

    def setUp(self):
        fixtures.QuestServiceTests.setUp(self)
        self.server.advertised_host = "127.0.0.1"
        self.server.port = 2190
        self.now = 100.0
        clock = SimpleNamespace(perf_counter=lambda: self.now, time=lambda: self.now)
        p = patch("dso_server.services.map_events.time", clock)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(CharacterServer, "_start_monster_chase", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        self.enter()

    def enter(self, *, map_id=MAP, tier=8, level=100, fresh=True, session=None):
        s = session or self._session(map_id, 50921, class_id=2)
        s.gameplay_map = map_id
        s.gameplay_map_access_id = f"{map_id}_{tier:02d}"
        s.gameplay_map_access_group_id = s.gameplay_map_access_id
        s.gameplay_scaling_difficulty = tier
        s.gameplay_character.level = level
        s.gameplay_currency_state = CurrencyChanged(0, 0, (0, 0, 0), 0, True)
        self.server.sessions = {s.addr: s}
        self.session = s
        self.runtime = self.server._attach_map_event_runtime(
            s, map_id=map_id, initial_flags=frozenset(), monster_level=level,
            scaling_difficulty=tier, fresh_instance=fresh)
        if fresh:
            self.runtime.scheduler = Scheduler()
        self.server._quest_prepare_map(s)
        self.server._on_map_entered(s)
        return s

    def finish(self):
        quest = self.server._quest_catalog().quest(QUEST)
        self.server._quest_feed(self.session, "regioncleared", next(iter(quest.tasks[0].clear_regions)))

    def chests(self):
        return [p for p in self.runtime.props.values() if p.definition.template_id == "bloodshed_chest"]

    def test_catalog_reads_all_map_bound_quests(self):
        self.assertEqual(sum(q.bound_to_map for q in self.server._quest_catalog().quests.values()), 40)
        self.assertTrue(self.server._quest_catalog().quest(QUEST).bound_to_map)

    def test_entry_starts_without_client_report_and_repeated_reports_keep_progress(self):
        progress = self.session.gameplay_quest_log[QUEST]
        progress.tokens["marker"] = 7
        deadline = dict(self.runtime.pending_start_deadlines)
        self.now += 10
        self.server._quest_on_signal(self.session, SIGNAL)
        self.server._on_map_entered(self.session)
        self.assertIs(self.session.gameplay_quest_log[QUEST], progress)
        self.assertEqual(progress.tokens["marker"], 7)
        self.assertEqual(self.runtime.pending_start_deadlines, deadline)
        self.assertEqual(len(self.runtime.scheduler.timers), 4)

    def test_entry_signal_checks_map_difficulty_and_level(self):
        for map_id, tier, level in ((MAP, 0, 100), (MAP, 7, 100), (MAP, 8, 54),
                                   ("a0200_kingscity", 8, 100)):
            with self.subTest(map=map_id, tier=tier, level=level):
                self.enter(map_id=map_id, tier=tier, level=level)
                self.server._quest_on_signal(self.session, SIGNAL)
                self.assertNotIn(QUEST, self.session.gameplay_quest_log)
        self.enter(level=55)
        self.assertIn(QUEST, self.session.gameplay_quest_log)

    def test_all_chest_tiers_use_elapsed_time_even_before_timer_callback(self):
        for elapsed, expected in ((0, 5), (29.999, 5), (30, 4), (59.999, 4),
                                  (60, 3), (90, 2), (120, 1), (600, 1)):
            with self.subTest(elapsed=elapsed):
                self.now = 100
                self.enter()
                self.now += elapsed
                self.finish()
                progress = self.session.gameplay_quest_log[QUEST]
                self.assertEqual(progress.state, QUEST_STATE_CLOSED)
                self.assertEqual(len(self.chests()), expected)
                self.assertEqual(self.session.gameplay_currency_state.amount, 965)
                # Completing twice, another entry report, and timer callbacks
                # must not pay twice or remove chests earned before the deadline.
                self.server._quest_on_signal(self.session, SIGNAL)
                self.finish()
                for timer in list(self.runtime.scheduler.timers):
                    timer.run()
                self.assertEqual(len(self.chests()), expected)
                self.assertEqual(self.session.gameplay_currency_state.amount, 965)

    def test_only_last_real_sludge_kill_completes_the_quest(self):
        encounter = load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, MAP,
            scaling_difficulty=8, include_spawn_areas=False)
        combat = LocalCombatState(encounter, player_actor_id=self.session.gameplay_remote_actor_id,
                                  player_health=1000)
        self.session.gameplay_combat_state = combat
        sludges = [m for m in combat.monsters.values() if m.template_id == SLUDGE]
        self.assertGreater(len(sludges), 1)
        for index, monster in enumerate(sludges):
            combat.monster_health[monster.actor_id] = 0
            self.server._on_monsters_killed(self.session, (monster,))
            state = self.session.gameplay_quest_log[QUEST].state
            self.assertEqual(state, QUEST_STATE_CLOSED if index == len(sludges)-1 else QUEST_STATE_ACCEPTED)
        self.assertEqual(len(self.chests()), 5)

    def test_real_impacts_complete_shared_challenge_even_if_killer_has_no_quest(self):
        for killer_has_quest in (True, False):
            with self.subTest(killer_has_quest=killer_has_quest):
                self.now = 100
                self.enter()
                killer = self.session
                encounter = load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, MAP,
                    monster_level=100, scaling_difficulty=8)
                combat = LocalCombatState(encounter,
                    player_actor_id=killer.gameplay_remote_actor_id, player_health=1000)
                killer.gameplay_combat_state = combat
                killer.gameplay_monster_encounter = encounter
                peer = self._session(MAP, 50931, class_id=2)
                peer.gameplay_combat_state = combat
                peer.gameplay_monster_encounter = encounter
                self.enter(session=peer, fresh=False)
                self.server.sessions[killer.addr] = killer
                killer.gameplay_presence_callback = (
                    lambda session, event: self.server._handle_presence_event(session, event))
                if not killer_has_quest:
                    killer.gameplay_quest_log.pop(QUEST)
                sludges = [m for m in combat.monsters.values() if m.template_id == SLUDGE]
                self.assertGreater(len(sludges), 1)
                killer.gameplay_spawned_monster_actor_ids.update(m.actor_id for m in sludges)
                for i, monster in enumerate(sludges):
                    pending = PendingPlayerHit(cast_id=i+1, target_actor_id=monster.actor_id,
                        skill_index=7, due_at=0, impact_tick=100+i, distance=0,
                        travel_distance=0, emit_delay=0, victim_status_effects=(),
                        source_actor_id=killer.gameplay_remote_actor_id, damage_modifier=2)
                    combat.pending_runtime_player_hits[(killer.gameplay_remote_actor_id, i+1, 0)] = pending
                    CharacterServer._resolve_player_projectile(killer,
                        killer.gameplay_chase_generation, i+1, killer.gameplay_remote_actor_id, 0)
                    self.assertEqual(peer.gameplay_quest_log[QUEST].state,
                        QUEST_STATE_CLOSED if i == len(sludges)-1 else QUEST_STATE_ACCEPTED)
                self.assertEqual(len(self.chests()), 5)
                self.assertEqual(peer.gameplay_currency_state.amount, 965)
                if killer_has_quest:
                    self.assertEqual(killer.gameplay_quest_log[QUEST].state, QUEST_STATE_CLOSED)
                else:
                    self.assertNotIn(QUEST, killer.gameplay_quest_log)
                # Late/duplicate death notifications cannot award more chests.
                self.server._notify_monsters_killed(killer, sludges[-1:])
                self.assertEqual(len(self.chests()), 5)
                self.assertEqual(peer.gameplay_currency_state.amount, 965)

    def test_non_player_final_kill_completes_without_tracking_or_visible_monsters(self):
        s = self.session
        encounter = load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, MAP,
            scaling_difficulty=8, include_spawn_areas=False)
        combat = LocalCombatState(encounter, player_actor_id=s.gameplay_remote_actor_id,
                                  player_health=1000)
        s.gameplay_combat_state = combat
        sludges = [m for m in combat.monsters.values() if m.template_id == SLUDGE]
        attacker = next(m for m in combat.monsters.values() if m.template_id != SLUDGE)
        self.assertFalse(s.gameplay_spawned_monster_actor_ids)
        # Entering the dungeon tracks its map-bound clear objective; drop it so
        # the clear below completes the quest without any tracking.
        self.assertEqual(s.gameplay_quest_tracking, (QUEST, 0))
        s.gameplay_quest_tracking = None
        for monster in sludges:
            combat.monster_health[monster.actor_id] = 0
        last = sludges[-1]
        resolution = DamageResolution(target_actor_id=last.actor_id,
            attacker_actor_id=attacker.actor_id, current_health=0,
            maximum_health=last.maximum_health, damage=last.maximum_health,
            killed=True, position=last.position, server_tick=100, hit_tags=(0,),
            skill_index=0)
        self.now += 45
        with patch.object(CharacterServer, "_quest_on_monsters_killed") as personal_kill:
            self.server._handle_presence_event(s, ("monster_activity", combat, (), (), (), (resolution,)))
            personal_kill.assert_not_called()
        self.assertEqual(s.gameplay_quest_log[QUEST].state, QUEST_STATE_CLOSED)
        self.assertEqual(len(self.chests()), 4)
        self.assertEqual(s.gameplay_currency_state.amount, 965)
        self.server._handle_presence_event(s, ("monster_activity", combat, (), (), (), (resolution,)))
        self.assertEqual(len(self.chests()), 4)
        self.assertEqual(s.gameplay_currency_state.amount, 965)

    def test_shared_clear_does_not_cross_combat_instances_or_credit_personal_tasks(self):
        catalog = self.server._quest_catalog()
        original = catalog.quest(QUEST)
        lookup = catalog.quest
        killer = self.session
        killer.gameplay_quest_log.pop(QUEST)
        peer = self._session(MAP, 50931, class_id=2)
        self.enter(session=peer, fresh=False)
        self.server.sessions[killer.addr] = killer
        # The map/event runtime alone is insufficient: combat instances must
        # also match, and a task must explicitly permit group progress.
        for shared_combat, group in ((False, True), (True, False)):
            with self.subTest(shared_combat=shared_combat, group=group):
                if shared_combat:
                    peer.gameplay_combat_state = killer.gameplay_combat_state
                custom = replace(original, tasks=(replace(original.tasks[0], group=group),))
                with patch.object(type(catalog), "quest", lambda _self, key: custom if key == QUEST else lookup(key)), \
                     patch.object(peer.gameplay_combat_state, "monster_template_counts", return_value=({SLUDGE: 5}, {})):
                    self.server._on_monsters_killed(killer, (SimpleNamespace(template_id=SLUDGE),))
                self.assertEqual(peer.gameplay_quest_log[QUEST].state, QUEST_STATE_ACCEPTED)
                self.assertEqual(self.chests(), [])

    def test_clear_region_tracker_shows_remaining_sludges_and_reconciles_missed_death(self):
        s = self.session
        encounter = load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, MAP,
            scaling_difficulty=8, include_spawn_areas=False)
        combat = LocalCombatState(encounter, player_actor_id=s.gameplay_remote_actor_id,
                                  player_health=1000, initially_active=False)
        s.gameplay_combat_state = combat
        sludges = [m for m in combat.monsters.values() if m.template_id == SLUDGE]
        self.assertGreater(len(sludges), 1)
        payload, bits = encode_quest_indication_request(
            QuestIndicationRequest(QUEST, 0), QUEST_MONSTER_INDICATION_REQUEST_COMMAND)
        s.current_message_bit_length = bits

        def tracked():
            return [c.value for data, count, label in s.recorder.messages
                    if label == "quest-monster-indication"
                    for c in decode_quest_batch(data, count)
                    if c.command_id == QUEST_MONSTER_INDICATION_INFO_COMMAND][-1].monsters

        self.server._handle_quest_monster_indication_request(s, payload)
        self.assertEqual({m.actor_id for m in tracked()}, {m.actor_id for m in sludges})
        # Never confuse unvisited/inactive monsters with defeated ones.
        self.assertFalse(combat.active_monster_actor_ids)
        self.assertEqual(s.gameplay_quest_log[QUEST].state, QUEST_STATE_ACCEPTED)
        moved = sludges[-1]
        combat.monster_positions[moved.actor_id] = (1.0, 2.0, 3.0)
        for monster in sludges[:-1]:
            combat.monster_health[monster.actor_id] = 0
            self.server._on_monsters_killed(s, (monster,))
        self.assertEqual([(m.actor_id, m.position) for m in tracked()], [(moved.actor_id, (1.0, 2.0, 3.0))])
        self.assertEqual(s.gameplay_quest_log[QUEST].state, QUEST_STATE_ACCEPTED)
        self.assertEqual(self.chests(), [])
        # A final death without a player notification is recovered from the
        # authoritative population on the client's next tracker request.
        combat.monster_health[moved.actor_id] = 0
        self.now += 75
        self.server._handle_quest_monster_indication_request(s, payload)
        self.assertEqual(s.gameplay_quest_log[QUEST].state, QUEST_STATE_CLOSED)
        self.assertEqual(len(self.chests()), 3)
        self.assertEqual(tracked(), ())
        self.server._handle_quest_monster_indication_request(s, payload)
        self.assertEqual(len(self.chests()), 3)
        self.assertEqual(s.gameplay_currency_state.amount, 965)

    def test_tracker_does_not_complete_an_empty_or_wrong_map_population(self):
        payload, bits = encode_quest_indication_request(
            QuestIndicationRequest(QUEST, 0), QUEST_MONSTER_INDICATION_REQUEST_COMMAND)
        self.session.current_message_bit_length = bits
        self.server._handle_quest_monster_indication_request(self.session, payload)
        self.assertEqual(self.session.gameplay_quest_log[QUEST].state, QUEST_STATE_ACCEPTED)
        self.assertEqual(self.chests(), [])
        self.session.gameplay_map = "a0200_kingscity"
        with patch.object(self.session.gameplay_combat_state, "monster_template_counts", return_value=({SLUDGE: 5}, {})):
            self.server._handle_quest_monster_indication_request(self.session, payload)
        self.assertEqual(self.session.gameplay_quest_log[QUEST].state, QUEST_STATE_ACCEPTED)

    def test_leaving_cancels_only_map_bound_progress_and_sends_removal(self):
        s = self.session
        ordinary = QuestProgress(fixtures.GRIMMAGSTONE_02)
        s.gameplay_quest_log[ordinary.quest_id] = ordinary
        s.gameplay_quest_tracking = (QUEST, 0)
        self.assertFalse(self.server._quest_cancel(s, QUEST))  # authored non-deletable
        self.server._quest_leave_map(s, "a0200_kingscity")
        self.assertNotIn(QUEST, s.gameplay_quest_log)
        self.assertIs(s.gameplay_quest_log[ordinary.quest_id], ordinary)
        self.assertIsNone(s.gameplay_quest_tracking)
        updates = [c.value for data, bits, label in s.recorder.messages if label == "quest-cancel"
                   for c in decode_quest_batch(data, bits) if c.command_id == QUEST_UPDATE_COMMAND]
        self.assertTrue(any(dict(u.ints).get(2) == QUEST_STATE_CANCELLED and dict(u.strs).get(0) == QUEST
                            for u in updates))
        self.assertEqual(s.gameplay_currency_state.amount, 0)

    def test_next_bloodshed_dungeon_and_same_map_reset_cancel_old_run(self):
        for destination in ("a0203_kingscrypt02", MAP):
            self.enter()
            self.server._quest_leave_map(self.session, destination)
            self.assertNotIn(QUEST, self.session.gameplay_quest_log)

    def test_restored_stale_run_is_removed_before_initialization_snapshot(self):
        old = self.session.gameplay_quest_log[QUEST]
        old.tokens["marker"] = 7
        doc = QuestService._quest_log_document(self.session)
        s = self._session(MAP, 50922, class_id=2)
        QuestService._restore_quest_state(s, SimpleNamespace(quest_log=doc))
        s.gameplay_map = MAP
        self.runtime = self.server._attach_map_event_runtime(s, map_id=MAP, initial_flags=frozenset(),
            monster_level=100, scaling_difficulty=8, fresh_instance=True)
        self.server._quest_prepare_map(s)
        self.assertNotIn(QUEST, s.gameplay_quest_log)
        self.assertNotIn(QUEST, [q.quest_id for q in self.server._quest_log_info(s).quests])

    def test_new_instance_can_repeat_but_same_instance_cannot_repay(self):
        self.finish()
        s = self.session
        self.enter(session=s, fresh=False)
        self.assertEqual(s.gameplay_quest_log[QUEST].state, QUEST_STATE_CLOSED)
        self.enter(session=s, fresh=True)
        self.assertEqual(s.gameplay_quest_log[QUEST].state, QUEST_STATE_ACCEPTED)

    def test_late_joiner_uses_original_instance_deadlines(self):
        original = self.runtime
        self.now += 75
        s = self._session(MAP, 50923, class_id=2)
        self.enter(session=s, fresh=False)
        self.assertIs(self.runtime, original)
        self.finish()
        self.assertEqual(len(self.chests()), 3)
        self.assertEqual(len(original.scheduler.timers), 4)

    def test_authored_multi_map_stage_handoff_preserves_progress(self):
        quest = self.server._quest_catalog().quest("pw001-pw002-pw003-n-re")
        origin, destination = "pw001_grimmagstone_01_dun", "pw002_alliance_fortress_01_dun"
        self.session.gameplay_map = origin
        progress = QuestProgress(quest.quest_id, map_instance=self.runtime.instance_id)
        for task in quest.tasks:
            if task.maps == {origin}:
                progress.finished_mask |= 1 << task.index
                progress.tokens.update({token: 1 for token in task.returns})
        self.session.gameplay_quest_log[quest.quest_id] = progress
        self.assertTrue(any(t.maps == {destination} for t in active_tasks(quest, progress)))
        self.server._quest_leave_map(self.session, destination)
        self.assertEqual(progress.continue_on_map, destination)
        documents = QuestService._quest_log_document(self.session)
        s = self._session(destination, 50924, class_id=2)
        QuestService._restore_quest_state(s, SimpleNamespace(quest_log=documents))
        self.server._attach_map_event_runtime(s, map_id=destination, initial_flags=frozenset(),
            monster_level=100, scaling_difficulty=0, fresh_instance=True)
        self.server._quest_prepare_map(s)
        restored = s.gameplay_quest_log[quest.quest_id]
        self.assertEqual(restored.tokens, progress.tokens)
        self.assertEqual(restored.continue_on_map, "")
        self.assertEqual(restored.map_instance, s.gameplay_map_event_runtime.instance_id)

    def test_map_bound_npc_quest_still_requires_its_turn_in(self):
        quest = self.server._quest_catalog().quest("e-tow-04-jarlshofn-in-danger-01-n")
        self.assertTrue(quest.goal)
        progress = QuestProgress(quest.quest_id, state=QUEST_STATE_FINISHED)
        self.session.gameplay_quest_log[quest.quest_id] = progress
        result = SimpleNamespace(changes=(), quest_finished=True)
        self.server._quest_publish_result(self.session, quest, result, "kill", "test")
        self.assertEqual(progress.state, QUEST_STATE_FINISHED)

    def test_stop_signal_cancels_quest_using_native_removal_update(self):
        quest = self.server._quest_catalog().quest("k5001-a-haunted-tale")
        self.session.gameplay_quest_log[quest.quest_id] = QuestProgress(quest.quest_id)
        self.server._quest_on_signal(self.session, quest.stop_signal)
        self.assertNotIn(quest.quest_id, self.session.gameplay_quest_log)

    def test_successful_transfer_cancels_before_source_persistence(self):
        def persist(server, session, **kwargs):
            self.assertNotIn(QUEST, session.gameplay_quest_log)
        with patch.object(CharacterServer, "_persist_gameplay_session", persist), \
             patch.object(self.server, "arm_gameplay"):
            self.assertTrue(MapTransitionService._begin_map_transfer(self.server, self.session, "a0200_kingscity", reason="admin-chat"))
        self.assertNotIn(QUEST, self.session.gameplay_quest_log)

    def test_rejected_transfer_preserves_quest(self):
        with self.assertRaises(ValueError):
            MapTransitionService._begin_map_transfer(self.server, self.session, "missing-map", reason="admin-chat")
        self.assertIn(QUEST, self.session.gameplay_quest_log)

    def test_dynamic_reward_is_level_percentage_with_quest_bonus_and_cap(self):
        self.session.gameplay_character.level = 55
        catalog = load_player_progression_catalog(DEFAULT_STATIC_DB_PATH)
        thresholds = catalog.player_level_xp["ranger"]
        base = thresholds[55]
        self.session.gameplay_character.progression_u32 = base
        expected = math.floor((thresholds[56] - base) * .01 + .5)
        self.assertEqual(self.server._quest_dynamic_xp(self.session, 1), expected)
        with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as db:
            premium = db.execute("SELECT rowid-1 FROM _Template_StatusEffect WHERE Id='Premium'").fetchone()[0]
        self.session.gameplay_persistent_status_effects[123] = SimpleNamespace(
            effect_id=premium, field_18=0, values=(.5, 10))
        boosted = math.floor((thresholds[56] - base) * .015 + .5)
        self.assertEqual(self.server._quest_dynamic_xp(self.session, 1), boosted)
        self.session.gameplay_character.level = 99
        self.session.gameplay_character.progression_u32 = thresholds[100]-1
        self.assertEqual(self.server._quest_dynamic_xp(self.session, 1), 1)
        self.session.gameplay_character.level = 100
        self.session.gameplay_character.progression_u32 = thresholds[100]
        self.assertEqual(self.server._quest_dynamic_xp(self.session, 1), 0)

    def test_disconnect_discards_map_quests_without_sending_to_dead_socket(self):
        count = len(self.session.recorder.messages)
        self.server._persist_gameplay_session(self.session, reason="client disconnect")
        self.assertNotIn(QUEST, self.session.gameplay_quest_log)
        self.assertEqual(len(self.session.recorder.messages), count)

    def test_character_switch_cancels_before_persistence(self):
        def persist(server, session, **kwargs):
            self.assertNotIn(QUEST, session.gameplay_quest_log)
        with patch.object(CharacterServer, "_persist_gameplay_session", persist):
            self.server._switch_character(self.session, SimpleNamespace(), reason="test")
        self.assertNotIn(QUEST, self.session.gameplay_quest_log)

    def test_source_map_kills_and_signals_cannot_progress_after_transfer_starts(self):
        self.session.gameplay_exit_transition_started = True
        self.finish()
        self.server._quest_on_signal(self.session, SIGNAL)
        self.assertEqual(self.session.gameplay_quest_log[QUEST].state, QUEST_STATE_ACCEPTED)
        self.assertEqual(self.chests(), [])

    def test_joining_an_already_cleared_population_finishes_without_another_kill(self):
        encounter = load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, MAP,
            scaling_difficulty=8, include_spawn_areas=False)
        combat = LocalCombatState(encounter, player_actor_id=self.session.gameplay_remote_actor_id,
                                  player_health=1000)
        for monster in combat.monsters.values():
            if monster.template_id == SLUDGE:
                combat.monster_health[monster.actor_id] = 0
        self.session.gameplay_combat_state = combat
        self.now += 75
        self.server._quest_on_map_entered(self.session)
        self.assertEqual(self.session.gameplay_quest_log[QUEST].state, QUEST_STATE_CLOSED)
        self.assertEqual(len(self.chests()), 3)

    def test_clear_region_checks_all_authored_monster_templates(self):
        catalog = self.server._quest_catalog()
        original = catalog.quest(QUEST)
        task = replace(original.tasks[0], clear_regions={
            next(iter(original.tasks[0].clear_regions)): SLUDGE + ";second_template"})
        custom = replace(original, tasks=(task,))
        lookup = catalog.quest
        counts = ({SLUDGE: 1, "second_template": 1}, {"second_template": 1})
        with patch.object(type(catalog), "quest", lambda _self, key: custom if key == QUEST else lookup(key)), \
             patch.object(self.session.gameplay_combat_state, "monster_template_counts", return_value=counts) as population:
            self.server._quest_check_cleared_regions(self.session, {SLUDGE: 1})
            self.assertEqual(self.session.gameplay_quest_log[QUEST].state, QUEST_STATE_ACCEPTED)
            population.return_value = (counts[0], {})
            self.server._quest_check_cleared_regions(self.session, {"second_template": 1})
            self.assertEqual(self.session.gameplay_quest_log[QUEST].state, QUEST_STATE_CLOSED)
