"""Native defense requests must authorize a live quest/NPC, without credit."""

from copy import deepcopy
import threading
import unittest
from unittest.mock import Mock, patch

from dso_server.gameplay.local_map_state import load_local_npc_definitions, local_npc_actor_id, local_npc_info
from dso_server.gameplay.quest_content import (
    QUEST_STATE_ACCEPTED, QUEST_STATE_CLOSED, QUEST_STATE_FAILED,
    QuestProgress,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import Move
from dso_server.protocol.quest_protocol import (
    Defend, NPC_DEFEND_DIALOG, NPC_QUEST_DIALOG, NPC_TASK_TARGET, PARAM_QUEST_STATE,
    QUEST_SUBJECT_QUEST_STATE, decode_quest_batch, encode_defend,
)
from dso_server.services.character_server import CharacterServer
from dso_server.services import quest_holdgrounds
from dso_server.tests import test_quests as fixtures
from dso_server.tests.test_pvp_matchmaking import _quiet_server


class QuestDefendServiceTests(unittest.TestCase):
    _session = fixtures.QuestServiceTests._session

    def setUp(self):
        self.server = _quiet_server(self)
        self.server.sessions = {}
        self.server.gameplay_world_lock = threading.RLock()
        self.saved = []

        def save(session, reason, **kwargs):
            self.saved.append((reason, kwargs, deepcopy(self.server._quest_log_document(session))))

        for name, replacement in (
            ("_accept_gameplay_state_command", lambda *a, **k: 0),
            ("_request_account_save", save),
            ("_start_monster_chase", lambda *a, **k: None),
        ):
            patcher = patch.object(CharacterServer, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.session = self._session("c2203_dun", 58301, class_id=2)
        self.session.gameplay_character.level = 40
        self.server.sessions[self.session.addr] = self.session
        self.runtime = self.server._attach_map_event_runtime(
            self.session, map_id="c2203_dun", initial_flags=frozenset(),
            monster_level=None, scaling_difficulty=0, fresh_instance=True,
        )
        self.quest = self.server._quest_catalog().quest("c2200-Frogprince-02-n")
        self.binding = self.quest.tasks[0].holdgrounds[0]
        self.session.gameplay_quest_log[self.quest.quest_id] = QuestProgress(self.quest.quest_id)
        for quest_id in self.quest.required:
            self.session.gameplay_quest_log[quest_id] = QuestProgress(quest_id, QUEST_STATE_CLOSED)
        self.npc = next(npc for npc in load_local_npc_definitions(
            DEFAULT_STATIC_DB_PATH, "c2203_dun",
        ) if npc.template_id == self.binding.actor)
        self.actor_id = local_npc_actor_id(self.npc)
        self._position(0.0)

    def _position(self, distance):
        x, y, z = self.npc.position
        self.session.gameplay_last_move = Move.from_values(
            self.session.gameplay_coordinate_frame.world_to_wire((x + distance, y, z)),
            0, 1.0, 1.0, 100, 0,
        )

    def _request(self, *, actor_id=None, holdground_id=None):
        packet, bits = encode_defend(Defend(
            self.actor_id if actor_id is None else actor_id,
            self.binding.holdground_id if holdground_id is None else holdground_id,
        ))
        self.session.current_message_bit_length = bits
        self.server._handle_defend_command(self.session, packet)

    def test_authorized_request_at_eight_meters_starts_without_quest_credit(self):
        self._position(8.0)
        with patch.object(quest_holdgrounds, "start_holdground") as start:
            self._request()
        start.assert_called_once_with(
            self.server, self.session, self.quest.quest_id, self.binding,
            npc_actor_id=self.actor_id,
        )
        progress = self.session.gameplay_quest_log[self.quest.quest_id]
        self.assertEqual((progress.state, progress.finished_mask, progress.tokens),
                         (QUEST_STATE_ACCEPTED, 0, {}))

    def test_invalid_identity_map_guard_distance_or_lifecycle_never_starts(self):
        cases = ("unknown-defense", "unknown-npc", "wrong-map", "guard-closed",
                 "too-far", "dead", "transferring", "unaccepted")
        for case in cases:
            with self.subTest(case=case):
                progress = self.session.gameplay_quest_log[self.quest.quest_id]
                old_map = self.session.gameplay_map
                old_tokens = dict(progress.tokens)
                old_state = progress.state
                request = {}
                if case == "unknown-defense":
                    request["holdground_id"] = self.binding.holdground_id + "-forged"
                elif case == "unknown-npc":
                    request["actor_id"] = 0x7fffffff
                elif case == "wrong-map":
                    self.session.gameplay_map = "a0200_kingscity"
                elif case == "guard-closed":
                    progress.tokens[self.quest.tasks[0].returns[0]] = 1
                elif case == "too-far":
                    self._position(8.1)
                elif case == "dead":
                    self.session.gameplay_death_pending = True
                elif case == "transferring":
                    self.session.gameplay_exit_transition_started = True
                elif case == "unaccepted":
                    progress.state = QUEST_STATE_FAILED
                try:
                    with patch.object(quest_holdgrounds, "start_holdground") as start:
                        self._request(**request)
                    start.assert_not_called()
                finally:
                    self.session.gameplay_map = old_map
                    self.session.gameplay_death_pending = False
                    self.session.gameplay_exit_transition_started = False
                    progress.state = old_state
                    progress.tokens = old_tokens
                    self._position(0.0)

    def test_defense_offer_uses_native_type_five_and_full_binding_id(self):
        interactions, _ = self.server._quest_npc_interactions(self.session, self.binding.actor)
        defense = [entry for entry in interactions if entry.field_08 == NPC_DEFEND_DIALOG]
        self.assertEqual([(entry.source, entry.target, entry.field_18) for entry in defense],
                         [(self.binding.holdground_id, self.quest.quest_id, self.quest.quest_type_id)])
        self.assertTrue(any(entry.field_08 == NPC_TASK_TARGET for entry in interactions))
        self.session.gameplay_quest_log[self.quest.quest_id].tokens[
            self.quest.tasks[0].returns[0]
        ] = 1
        interactions, _ = self.server._quest_npc_interactions(self.session, self.binding.actor)
        self.assertFalse(any(entry.field_08 == NPC_DEFEND_DIALOG for entry in interactions))

    def test_duplicate_native_request_keeps_one_encounter_and_one_wave(self):
        with patch.object(quest_holdgrounds, "_schedule_poll"):
            self._request()
            runs = tuple(self.runtime.holdgrounds.values())
            self.assertEqual(len(runs), 1)
            run = runs[0]
            attackers = set(run.owned_actor_ids)
            self.assertGreater(len(attackers), 0)
            self._request()
            self.assertEqual(tuple(self.runtime.holdgrounds.values()), runs)
            self.assertEqual(run.owned_actor_ids, attackers)
            self.assertEqual(run.wave_index, 0)
            refreshed = self.server._quest_decorate_npc_info(
                self.session, local_npc_info(self.npc), self.npc.template_id,
            )
            self.assertFalse(refreshed.enabled)
            self.assertFalse(refreshed.interactions)
            self.assertNotIn(self.actor_id, {local_npc_actor_id(npc)
                             for npc in self.server._active_npc_definitions(self.session)})
            quest_holdgrounds.cancel_holdgrounds(self.server, self.session)
        self.assertFalse(self.runtime.holdgrounds)
        self.assertTrue(self.server._quest_decorate_npc_info(
            self.session, local_npc_info(self.npc), self.npc.template_id,
        ).enabled)

    def test_failure_is_saved_as_four_reoffered_and_reaccept_resets_progress(self):
        progress = self.session.gameplay_quest_log[self.quest.quest_id]
        progress.tokens["prior-attempt"] = 9
        self.session.gameplay_quest_tracking = (self.quest.quest_id, 0)
        self.assertTrue(self.server._quest_feed(
            self.session, "holdgroundfailed", self.binding.holdground_id,
            quest_id=self.quest.quest_id,
        ))
        self.assertEqual(progress.state, 4)
        self.assertIsNone(self.session.gameplay_quest_tracking)
        saved = next(entry for entry in self.saved if entry[0] == "quest failed")
        self.assertTrue(saved[1]["flush"])
        self.assertEqual(saved[2][self.quest.quest_id]["state"], 4)
        messages = [decode_quest_batch(data, bits) for data, bits, label
                    in self.session.recorder.messages if label == "quest-failed"]
        self.assertTrue(any(command.value.subject == QUEST_SUBJECT_QUEST_STATE
                            and dict(command.value.ints).get(PARAM_QUEST_STATE) == 4
                            for batch in messages for command in batch))
        offers, _ = self.server._quest_npc_interactions(self.session, self.quest.giver)
        self.assertTrue(any(entry.field_08 == NPC_QUEST_DIALOG
                            and entry.source == self.quest.quest_id for entry in offers))
        self.assertTrue(self.server._quest_accept(self.session, self.quest.quest_id, offered=True))
        retry = self.session.gameplay_quest_log[self.quest.quest_id]
        self.assertIsNot(retry, progress)
        self.assertEqual((retry.state, retry.tokens, retry.finished_mask),
                         (QUEST_STATE_ACCEPTED, {}, 0))

    def test_failure_survives_failed_packet_delivery_before_reconnect(self):
        original = self.session.send_reliable_ordered

        def fail_packet(payload, **kwargs):
            if kwargs.get("label") == "quest-failed":
                raise OSError("closed transport")
            return original(payload, **kwargs)

        self.session.send_reliable_ordered = fail_packet
        with self.assertRaises(OSError):
            self.server._quest_feed(self.session, "holdgroundfailed",
                                   self.binding.holdground_id, quest_id=self.quest.quest_id)
        saved = next(entry for entry in self.saved if entry[0] == "quest failed")
        restored = QuestProgress.from_document(self.quest.quest_id, saved[2][self.quest.quest_id])
        self.assertEqual(restored.state, QUEST_STATE_FAILED)

    def _start_run(self):
        with patch.object(quest_holdgrounds, "_schedule_poll"):
            self._request()
        run = next(iter(self.runtime.holdgrounds.values()))
        run.timer = Mock()
        return run

    def _finish(self, run, outcome):
        with run.owner.gameplay_mutation_lock, run.lock:
            quest_holdgrounds._finish(self.server, run, outcome)

    def test_failure_reentry_cleans_and_restores_the_npc_exactly_once(self):
        run = self._start_run()
        timer = run.timer
        with patch.object(quest_holdgrounds, "_publish_npc_enabled", wraps=quest_holdgrounds._publish_npc_enabled) as publish:
            self._finish(run, "holdgroundfailed")
            self._finish(run, "holdgroundfailed")
        self.assertEqual(run.outcome, "holdgroundfailed")
        self.assertTrue(run.stopped)
        self.assertFalse(self.runtime.holdgrounds)
        self.assertFalse(self.session.gameplay_combat_state.player_summons)
        self.assertFalse(self.session.gameplay_combat_state.holdground_targets)
        self.assertEqual([call.args[2] for call in publish.call_args_list], [True])
        timer.cancel.assert_called_once()

    def test_terminal_packet_failure_retains_retry_then_replays_durable_state(self):
        run = self._start_run()
        timer = run.timer
        sender = self.session.send_reliable_ordered

        def fail_once(payload, **kwargs):
            if kwargs.get("label") == "quest-failed":
                raise OSError("closed transport")
            return sender(payload, **kwargs)

        self.session.send_reliable_ordered = fail_once
        with self.assertRaises(OSError):
            self._finish(run, "holdgroundfailed")
        self.assertFalse(run.stopped)
        self.assertTrue(run.cleaned)
        self.assertEqual(run.outcome, "holdgroundfailed")
        timer.cancel.assert_not_called()
        self.assertTrue(any(reason == "quest defense result" and options.get("flush")
                            for reason, options, _ in self.saved))
        self.session.send_reliable_ordered = sender
        self._finish(run, "holdgroundfailed")
        self.assertTrue(run.stopped)
        timer.cancel.assert_called_once()
        self.assertTrue(any(reason == "quest defense result" and options.get("flush")
                            for reason, options, _ in self.saved))
        self.assertTrue(any(label == "holdground-quest-replay"
                            for _, _, label in self.session.recorder.messages))

    def test_terminal_save_failure_retries_even_after_progress_already_changed(self):
        run = self._start_run()
        timer = run.timer
        saver = CharacterServer._request_account_save

        def fail_save(session, reason, **kwargs):
            if reason == "quest failed":
                raise OSError("temporary storage failure")
            return saver(session, reason, **kwargs)

        with patch.object(CharacterServer, "_request_account_save", fail_save):
            with self.assertRaises(OSError):
                self._finish(run, "holdgroundfailed")
        self.assertEqual(self.session.gameplay_quest_log[self.quest.quest_id].state, QUEST_STATE_FAILED)
        timer.cancel.assert_not_called()
        self.assertFalse(run.stopped)
        self._finish(run, "holdgroundfailed")
        committed = [document for reason, options, document in self.saved
                     if reason == "quest defense result" and options.get("flush")]
        self.assertEqual(committed[-1][self.quest.quest_id]["state"], QUEST_STATE_FAILED)
        self.assertTrue(run.stopped)

    def test_first_success_packet_failure_still_flushes_earned_progress(self):
        run = self._start_run()
        sender = self.session.send_reliable_ordered

        def fail_progress(payload, **kwargs):
            if kwargs.get("label") == "quest-progress":
                raise OSError("failed first success packet")
            return sender(payload, **kwargs)

        self.session.send_reliable_ordered = fail_progress
        with self.assertRaises(OSError):
            self._finish(run, "holdgroundsucceeded")
        self.assertFalse(run.stopped)
        committed = [document for reason, options, document in self.saved
                     if reason == "quest defense result" and options.get("flush")]
        restored = QuestProgress.from_document(self.quest.quest_id, committed[-1][self.quest.quest_id])
        self.assertTrue(restored.task_finished(0))
        self.session.send_reliable_ordered = sender
        self._finish(run, "holdgroundsucceeded")
        self.assertTrue(run.stopped)

    def _group_arena(self):
        self.quest = self.server._quest_catalog().quest("ce2025-main-quest-10")
        self.binding = self.quest.tasks[0].holdgrounds[0]
        self.assertTrue(self.quest.tasks[0].group)
        self.session = self._session("ce202504", 58311, class_id=2)
        self.session.gameplay_character.level = 100
        self.server.sessions = {self.session.addr: self.session}
        self.runtime = self.server._attach_map_event_runtime(
            self.session, map_id="ce202504", initial_flags=frozenset(),
            monster_level=None, scaling_difficulty=0, fresh_instance=True,
        )
        self.session.gameplay_quest_log[self.quest.quest_id] = QuestProgress(self.quest.quest_id)
        self.npc = next(npc for npc in load_local_npc_definitions(
            DEFAULT_STATIC_DB_PATH, "ce202504",
        ) if npc.template_id == self.binding.actor)
        self.actor_id = local_npc_actor_id(self.npc)
        self._position(0.0)
        return self._join_peer(58312)

    def _join_peer(self, port):
        peer = self._session(self.session.gameplay_map, port, class_id=2)
        peer.gameplay_character.level = 100
        peer.gameplay_map_event_runtime = self.runtime
        peer.gameplay_combat_state = self.session.gameplay_combat_state
        peer.gameplay_quest_log[self.quest.quest_id] = QuestProgress(self.quest.quest_id)
        self.server.sessions[peer.addr] = peer
        return peer

    def test_group_participants_receive_success_and_failure_but_late_joiners_do_not(self):
        for outcome in ("holdgroundsucceeded", "holdgroundfailed"):
            with self.subTest(outcome=outcome):
                peer = self._group_arena()
                run = self._start_run()
                late = self._join_peer(58313)
                self._finish(run, outcome)
                owner_progress = self.session.gameplay_quest_log[self.quest.quest_id]
                peer_progress = peer.gameplay_quest_log[self.quest.quest_id]
                self.assertEqual(peer_progress.state, owner_progress.state)
                self.assertEqual(peer_progress.finished_mask, owner_progress.finished_mask)
                if outcome == "holdgroundsucceeded":
                    self.assertTrue(peer_progress.task_finished(0))
                else:
                    self.assertEqual(peer_progress.state, QUEST_STATE_FAILED)
                self.assertEqual(late.gameplay_quest_log[self.quest.quest_id].to_document(),
                                 QuestProgress(self.quest.quest_id).to_document())

    def test_group_recipient_reaccept_or_transfer_cannot_receive_old_result(self):
        for stale in ("reaccepted", "transferred"):
            with self.subTest(stale=stale):
                peer = self._group_arena()
                run = self._start_run()
                if stale == "reaccepted":
                    peer.gameplay_quest_log[self.quest.quest_id] = QuestProgress(self.quest.quest_id)
                else:
                    peer.gameplay_chase_generation += 1
                self._finish(run, "holdgroundsucceeded")
                self.assertEqual(peer.gameplay_quest_log[self.quest.quest_id].finished_mask, 0)
                self.assertTrue(self.session.gameplay_quest_log[self.quest.quest_id].task_finished(0))

    def test_other_live_npc_cannot_start_a_different_defense_binding(self):
        self._group_arena()
        wrong = next(npc for npc in load_local_npc_definitions(DEFAULT_STATIC_DB_PATH, "ce202504")
                     if npc.template_id == "ce_2025_zador_benniss_hold_quest_02")
        self.session.gameplay_last_move = Move.from_values(
            self.session.gameplay_coordinate_frame.world_to_wire(wrong.position),
            0, 1.0, 1.0, 100, 0,
        )
        self.assertIsNotNone(self.server._resolve_npc_interaction(self.session, local_npc_actor_id(wrong)))
        with patch.object(quest_holdgrounds, "start_holdground") as start:
            self._request(actor_id=local_npc_actor_id(wrong))
        start.assert_not_called()

    def test_private_defense_does_not_credit_an_existing_peer(self):
        self.assertFalse(self.quest.tasks[0].group)
        peer = self._join_peer(58314)
        run = self._start_run()
        self._finish(run, "holdgroundsucceeded")
        self.assertTrue(self.session.gameplay_quest_log[self.quest.quest_id].task_finished(0))
        self.assertEqual(peer.gameplay_quest_log[self.quest.quest_id].finished_mask, 0)

    def test_busy_group_recipient_defers_without_blocking_and_retries_once(self):
        peer = self._group_arena()
        run = self._start_run()
        held, release = threading.Event(), threading.Event()

        def occupy_gate():
            with peer.gameplay_mutation_lock:
                held.set()
                release.wait(5)

        worker = threading.Thread(target=occupy_gate)
        worker.start()
        self.assertTrue(held.wait(2))
        try:
            self._finish(run, "holdgroundsucceeded")
            self.assertFalse(run.stopped)
            self.assertTrue(self.session.gameplay_quest_log[self.quest.quest_id].task_finished(0))
            self.assertFalse(peer.gameplay_quest_log[self.quest.quest_id].task_finished(0))
        finally:
            release.set()
            worker.join(2)
        self._finish(run, "holdgroundsucceeded")
        self.assertTrue(run.stopped)
        self.assertTrue(peer.gameplay_quest_log[self.quest.quest_id].task_finished(0))
        self.assertEqual(sum(reason == "quest defense result" and self.quest.quest_id in document
                             for reason, _, document in self.saved), 2)

    def test_recorded_group_success_survives_owner_departure_before_peer_retry(self):
        peer = self._group_arena()
        run = self._start_run()
        held, release = threading.Event(), threading.Event()

        def occupy_gate():
            with peer.gameplay_mutation_lock:
                held.set()
                release.wait(5)

        worker = threading.Thread(target=occupy_gate)
        worker.start()
        self.assertTrue(held.wait(2))
        try:
            self._finish(run, "holdgroundsucceeded")
            self.assertEqual(run.outcome, "holdgroundsucceeded")
            self.assertFalse(run.stopped)
            self.session.gameplay_chase_generation += 1
            self.session.connected = False
            self.session.gameplay_map_event_runtime = None
        finally:
            release.set()
            worker.join(2)
        quest_holdgrounds.advance_holdgrounds(self.server, peer)
        self.assertTrue(run.stopped)
        self.assertTrue(peer.gameplay_quest_log[self.quest.quest_id].task_finished(0))
        self.assertFalse(self.runtime.holdgrounds)


if __name__ == "__main__":
    unittest.main()
