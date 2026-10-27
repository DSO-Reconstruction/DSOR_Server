"""Capture the flag: full simulated matches on the authored c2103_ctf arena."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_player_only_encounter
from dso_server.gameplay.pvp_match_state import MatchPhase
from dso_server.gameplay.pvp_objectives import (
    FLAG_AT_BASE,
    FLAG_CARRIED,
    FLAG_DROPPED,
    CaptureTheFlagState,
    load_capture_the_flag_layout,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.pvp_match_protocol import (
    MATCH_MODE_CAPTURE_THE_FLAG_5V5,
    MATCH_TEAM_SIZES,
    MATCH_WINNER_DRAW,
    PVP_MATCH_INFO_COMMAND,
    PVP_MATCH_OVER_COMMAND,
    PVP_MATCH_START_COMMAND,
    SHOW_PVP_DEATH_DIALOG_COMMAND,
    JoinBattlegroundResponse,
    JoinMatchQueue,
    decode_match_info,
    decode_match_over,
    decode_match_start,
    decode_show_pvp_death_dialog,
    encode_join_battleground_response,
    encode_join_match_queue,
)
from dso_server.protocol.world_loot_protocol import PICKUP_ITEM_COMMAND
from dso_server.runtime.context import Move
from dso_server.services.base import CharacterService
from dso_server.services.character_server import CharacterServer
from dso_server.tests import test_pvp_matchmaking as fixtures


BLUE, RED = 0, 1


def _pickup_payload(item_id: int) -> bytes:
    return bytes((0x8B,)) + PICKUP_ITEM_COMMAND.to_bytes(2, "little") + int(item_id).to_bytes(4, "little")


class CaptureTheFlagLayoutTests(unittest.TestCase):
    def test_authored_bases_flags_and_rules(self):
        layout = load_capture_the_flag_layout(DEFAULT_STATIC_DB_PATH, "c2103_ctf")
        self.assertEqual(layout.flag_items, ("pvp_ctf_blue_flag", "pvp_ctf_red_flag"))
        self.assertEqual(layout.home(BLUE)[0::2], (-30.0, -22.0))
        self.assertEqual(layout.home(RED)[0::2], (38.0, 46.0))
        self.assertTrue(layout.bases[RED].contains((41.9, 0.0, 49.9)))
        self.assertFalse(layout.bases[RED].contains((42.1, 0.0, 46.0)))

    def test_state_machine_rules_and_idempotency(self):
        layout = load_capture_the_flag_layout(DEFAULT_STATIC_DB_PATH, "c2103_ctf")
        state = CaptureTheFlagState(layout, 3)
        blue_home, red_home = layout.home(BLUE), layout.home(RED)
        # Own flag at home cannot be taken by its owners.
        self.assertEqual(state.touch(1, BLUE, BLUE, blue_home), ())
        taken = state.touch(6, RED, BLUE, blue_home)
        self.assertEqual([event.kind for event in taken], ["flag_taken"])
        self.assertEqual(state.touch(7, RED, BLUE, blue_home), ())
        # Red's own flag must be home: blue takes it first, red cannot score.
        state.touch(1, BLUE, RED, red_home)
        self.assertEqual(state.reach_base(6, RED, red_home), ())
        dropped = state.drop(1, BLUE, (10.0, 0.0, 10.0))
        self.assertEqual(dropped[0].kind, "flag_dropped")
        self.assertEqual(state.flags[RED].status, FLAG_DROPPED)
        returned = state.touch(7, RED, RED, (10.0, 0.0, 10.0))
        self.assertEqual(returned[0].kind, "flag_returned")
        captured = state.reach_base(6, RED, red_home)
        self.assertEqual(captured[0].kind, "flag_captured")
        self.assertEqual(state.captures, [0, 1])
        self.assertEqual(state.reach_base(6, RED, red_home), ())
        self.assertEqual(state.flags[BLUE].status, FLAG_AT_BASE)
        self.assertIsNone(state.winner())
        self.assertEqual(state.leading_team(), RED)


class CaptureTheFlagMatchTests(unittest.TestCase):
    def setUp(self):
        self.server = fixtures._quiet_server(self)
        self.achievements = []
        self.armed = {}
        for name, replacement in (
            ("_achievement_feed", lambda _self, s, kind, values, **_k:
                self.achievements.append((s.gameplay_character.character_id, kind, tuple(values)))),
            ("_daily_challenge_feed", lambda *_a, **_k: None),
            ("_quest_feed", lambda *_a, **_k: None),
            ("_request_account_save", lambda *_a, **_k: None),
            ("_arm_match_timer", lambda _self, match, key, delay, callback:
                self.armed.__setitem__(key, (delay, callback))),
        ):
            patcher = patch.object(CharacterServer, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        mode = MATCH_MODE_CAPTURE_THE_FLAG_5V5
        self.sessions = tuple(
            fixtures._session(54000 + index, index, f"Player{index}")
            for index in range(1, sum(MATCH_TEAM_SIZES[mode]) + 1)
        )
        for session in self.sessions:
            payload, bits = encode_join_match_queue(JoinMatchQueue(match_mode=mode))
            session.current_message_bit_length = bits
            CharacterService._handle_join_match_queue(self.server, session, payload)
        self.match = CharacterService._matchmaking_registry(self.server).match_for_character(1)
        self.assertIsNotNone(self.match)
        self.assertIsInstance(self.match.objective, CaptureTheFlagState)
        payload, bits = encode_join_battleground_response(
            JoinBattlegroundResponse(accepted=True, match_mode=mode)
        )
        for session in self.sessions:
            session.current_message_bit_length = bits
            CharacterService._handle_join_battleground_response(self.server, session, payload)
        self.encounter = load_local_player_only_encounter(DEFAULT_STATIC_DB_PATH, self.match.map_id)
        combat = LocalCombatState(
            self.encounter,
            player_actor_id=self.sessions[0].gameplay_remote_actor_id,
            player_health=1000,
        )
        for session in self.sessions:
            combat.register_player(session.gameplay_remote_actor_id, 1000)
            session.gameplay_combat_state = combat
            session.gameplay_monster_encounter = self.encounter
            session.gameplay_coordinate_frame = self.encounter.coordinate_frame
            session.gameplay_map = self.match.map_id
            # World code reaches the battleground through this closure,
            # wired by player initialization in the live server.
            session.gameplay_match_callback = (
                lambda owner, event: CharacterService._handle_match_event(
                    self.server, owner, event,
                )
            )
            CharacterService._announce_battleground(self.server, session)
            self.assertTrue(CharacterService._bind_battleground_session(self.server, session))
        CharacterService._start_match_round(self.server, self.match, self.match.info_generation)
        self.assertEqual(self.match.phase, MatchPhase.RUNNING)
        self.flags = self.match.objective.flags
        self.layout = self.match.objective.layout

    # -- helpers --------------------------------------------------------

    def team(self, team):
        return [s for s in self.sessions if s.gameplay_match_team == team]

    def move(self, session, world):
        wire = self.encounter.coordinate_frame.world_to_wire(world)
        with session.flow_lock:
            session.gameplay_last_move = Move.from_values(wire, 0.0, 0.0, 0.0, 0, 0)
        CharacterService._handle_match_event(self.server, session, ("match_moved", wire))

    def kill(self, killer, victim):
        victim.gameplay_death_pending = True
        CharacterService._handle_match_event(self.server, killer, ("match_kill", victim))

    def flag_items(self, session):
        return {
            item_id: drop.item.template_id
            for item_id, drop in session.gameplay_world_loot.items()
        }

    def carrier_effects(self, session):
        return getattr(session, "gameplay_flag_carrier_effects", {}) or {}

    def capture(self, carrier_team):
        carrier = self.team(carrier_team)[0]
        enemy = 1 - carrier_team
        self.move(carrier, self.layout.home(enemy))
        self.assertEqual(self.flags[enemy].status, FLAG_CARRIED)
        self.move(carrier, self.layout.home(carrier_team))
        return carrier

    # -- tests ------------------------------------------------------------

    def test_round_uses_authored_duration_and_flags_stand_on_both_bases(self):
        start = decode_match_start(*self.sessions[0].recorder.find(PVP_MATCH_START_COMMAND))
        self.assertEqual(start.round_duration_ticks, 900 * 25)
        self.assertEqual(self.match.rounds_to_win, 1)
        for session in self.sessions:
            self.assertEqual(
                sorted(self.flag_items(session).values()),
                ["pvp_ctf_blue_flag", "pvp_ctf_red_flag"],
            )
        self.assertEqual(self.flags[BLUE].status, FLAG_AT_BASE)
        self.assertNotEqual(self.flags[BLUE].item_id, self.flags[RED].item_id)

    def test_take_carry_and_capture_scores_and_resets_the_flag(self):
        carrier = self.team(RED)[0]
        blue_item = self.flags[BLUE].item_id
        self.move(carrier, self.layout.home(BLUE))
        self.assertEqual(self.flags[BLUE].carrier, carrier.gameplay_character.character_id)
        for session in self.sessions:
            self.assertNotIn(blue_item, session.gameplay_world_loot)
        effect = self.carrier_effects(carrier)[BLUE]
        self.assertIn(effect.field_04, carrier.gameplay_persistent_status_effects)
        self.assertAlmostEqual(effect.values[0], -0.3, places=5)
        self.move(carrier, self.layout.home(RED))
        self.assertEqual(self.match.objective.captures, [0, 1])
        self.assertEqual(self.flags[BLUE].status, FLAG_AT_BASE)
        self.assertNotEqual(self.flags[BLUE].item_id, blue_item)
        self.assertNotIn(BLUE, self.carrier_effects(carrier))
        self.assertNotIn(effect.field_04, carrier.gameplay_persistent_status_effects)
        entry = self.match.participant(carrier.gameplay_character.character_id)
        self.assertEqual(entry.flag_captures, 1)
        carrier.recorder.messages.clear()
        CharacterService._send_match_info(self.server, carrier, self.match)
        info = decode_match_info(*carrier.recorder.find(PVP_MATCH_INFO_COMMAND))
        self.assertEqual([team.rounds_won for team in info.teams], [0, 1])
        carrier_row = next(row for team in info.teams for row in team.players if row.actor_id == 1)
        self.assertEqual(carrier_row.score, 1)

    def test_pickup_request_takes_the_flag_without_touching_inventory(self):
        carrier = self.team(BLUE)[0]
        red_item = self.flags[RED].item_id
        self.move(carrier, (self.layout.home(RED)[0] + 3.5, 0.0, self.layout.home(RED)[2]))
        self.assertEqual(self.flags[RED].status, FLAG_AT_BASE)
        payload = _pickup_payload(red_item)
        inventory_before = carrier.gameplay_inventory
        carrier.current_message_bit_length = len(payload) * 8
        CharacterService._handle_pickup_item(carrier, payload)
        self.assertEqual(self.flags[RED].status, FLAG_CARRIED)
        self.assertIs(carrier.gameplay_inventory, inventory_before)
        # A far-away request for the new state is refused.
        far = self.team(BLUE)[1]
        CharacterService._handle_pickup_item(far, _pickup_payload(self.flags[BLUE].item_id))
        self.assertEqual(self.flags[BLUE].status, FLAG_AT_BASE)

    def test_carrier_death_drops_flag_and_teammate_returns_it(self):
        carrier = self.team(RED)[0]
        defender, returner = self.team(BLUE)[:2]
        self.move(carrier, self.layout.home(BLUE))
        drop_at = (-10.0, 0.0, -5.0)
        self.move(carrier, drop_at)
        self.kill(defender, carrier)
        self.assertEqual(self.flags[BLUE].status, FLAG_DROPPED)
        self.assertAlmostEqual(self.flags[BLUE].position[0], drop_at[0], places=2)
        self.assertAlmostEqual(self.flags[BLUE].position[2], drop_at[2], places=2)
        dropped_item = self.flags[BLUE].item_id
        self.assertTrue(dropped_item)
        for session in self.sessions:
            self.assertIn(dropped_item, session.gameplay_world_loot)
        self.assertNotIn(BLUE, self.carrier_effects(carrier))
        killer_entry = self.match.participant(defender.gameplay_character.character_id)
        self.assertEqual(killer_entry.flag_carrier_kills, 1)
        # The dead player cannot pick anything up.
        self.move(carrier, drop_at)
        self.assertEqual(self.flags[BLUE].status, FLAG_DROPPED)
        self.move(returner, drop_at)
        self.assertEqual(self.flags[BLUE].status, FLAG_AT_BASE)
        for session in self.sessions:
            self.assertNotIn(dropped_item, session.gameplay_world_loot)
        self.assertEqual(
            self.match.participant(returner.gameplay_character.character_id).flag_returns, 1
        )

    def test_enemy_retakes_a_dropped_flag(self):
        first, second = self.team(RED)[:2]
        self.move(first, self.layout.home(BLUE))
        self.move(first, (0.0, 0.0, 0.0))
        self.kill(self.team(BLUE)[0], first)
        self.move(second, (0.0, 0.0, 0.0))
        self.assertEqual(self.flags[BLUE].carrier, second.gameplay_character.character_id)

    def test_respawn_after_the_authored_delay_at_the_team_respawn(self):
        victim = self.team(BLUE)[0]
        self.kill(self.team(RED)[0], victim)
        cid = victim.gameplay_character.character_id
        self.assertTrue(self.match.is_dead(cid))
        dialog = decode_show_pvp_death_dialog(*victim.recorder.find(SHOW_PVP_DEATH_DIALOG_COMMAND))
        self.assertEqual((dialog.event_progress, dialog.actor_id), (0, 1))
        delay, callback = self.armed[f"respawn-{cid}"]
        self.assertEqual(delay, 8.0)
        # A kill of a dead player is not counted twice.
        self.kill(self.team(RED)[0], victim)
        self.assertEqual(self.match.participant(cid).deaths, 1)
        victim.recorder.messages.clear()
        callback(self.server, self.match, self.match.info_generation)
        self.assertFalse(self.match.is_dead(cid))
        self.assertEqual(victim.gameplay_spawn_position[0::2], (-22.0, -31.722890853881836))
        labels = [label for _d, _b, label in victim.recorder.messages]
        self.assertIn("pvp-arena-transform", labels)
        self.assertIn("pvp-round-revive", labels)
        self.assertIn("pvp-respawn-effects", labels)
        # Respawning twice is impossible.
        self.assertFalse(CharacterService._respawn_objective_participant(
            self.server, self.match, self.match.info_generation, character_id=cid,
        ))
        # The match did not end on a death.
        self.assertEqual(self.match.phase, MatchPhase.RUNNING)

    def test_death_dialog_respawn_request_waits_for_the_authored_delay(self):
        victim = self.team(RED)[0]
        cid = victim.gameplay_character.character_id
        self.kill(self.team(BLUE)[0], victim)
        payload = b"\x8b\x62\x00\x80"  # RespawnCommand(nearest_exit=True)
        victim.current_message_bit_length = 26
        CharacterService._handle_respawn(self.server, victim, payload)
        self.assertTrue(self.match.is_dead(cid))
        self.match.participant(cid).respawn_due = 0.0
        self.match.timers["respawn-%d" % cid] = SimpleNamespace(cancel=lambda: None)
        CharacterService._handle_respawn(self.server, victim, payload)
        self.assertFalse(self.match.is_dead(cid))
        self.assertNotIn("respawn-%d" % cid, self.match.timers)

    def test_three_captures_win_and_feed_the_ctf_achievement_once(self):
        for _ in range(3):
            self.capture(RED)
        self.assertEqual(self.match.phase, MatchPhase.OVER)
        self.assertEqual(self.match.winning_team, RED)
        self.assertIn("over", self.armed)
        CharacterService._finish_match(self.server, self.match, self.match.info_generation)
        CharacterService._finish_match(self.server, self.match, self.match.info_generation)
        for session in self.sessions:
            over = decode_match_over(*session.recorder.find(PVP_MATCH_OVER_COMMAND))
            self.assertEqual(over.winning_team, RED)
            self.assertFalse(self.flag_items(session))
            self.assertFalse(self.carrier_effects(session))
        winners = {s.gameplay_character.character_id for s in self.team(RED)}
        fed = [(cid, values) for cid, kind, values in self.achievements if kind == "pvp_match"]
        self.assertEqual({cid for cid, _ in fed}, winners)
        self.assertEqual(len(fed), len(winners))
        self.assertTrue(all("CaptureTheFlag_5v5" in values for _cid, values in fed))
        self.assertTrue(all(s.gameplay_pvp_matches_won == 1 for s in self.team(RED)))
        self.assertTrue(all(s.gameplay_pvp_matches_lost == 1 for s in self.team(BLUE)))

    def test_timeout_awards_the_leading_team(self):
        self.capture(BLUE)
        CharacterService._expire_match_round(self.server, self.match, self.match.info_generation)
        self.assertEqual(self.match.phase, MatchPhase.OVER)
        self.assertEqual(self.match.winning_team, BLUE)

    def test_timeout_with_level_score_is_a_draw(self):
        self.capture(BLUE)
        self.capture(RED)
        CharacterService._expire_match_round(self.server, self.match, self.match.info_generation)
        self.assertEqual(self.match.winning_team, MATCH_WINNER_DRAW)
        CharacterService._finish_match(self.server, self.match, self.match.info_generation)
        for session in self.sessions:
            over = decode_match_over(*session.recorder.find(PVP_MATCH_OVER_COMMAND))
            self.assertEqual(over.winning_team, MATCH_WINNER_DRAW)
            self.assertEqual(getattr(session, "gameplay_pvp_matches_won", 0), 0)
            self.assertEqual(getattr(session, "gameplay_pvp_matches_lost", 0), 0)
        self.assertFalse([a for a in self.achievements if a[1] == "pvp_match"])

    def test_carrier_leaving_drops_the_flag_and_keeps_the_match_running(self):
        carrier = self.team(RED)[0]
        self.move(carrier, self.layout.home(BLUE))
        self.move(carrier, (5.0, 0.0, 5.0))
        with patch.object(CharacterServer, "_begin_map_transfer", lambda *a, **k: True):
            CharacterService._leave_battleground(self.server, carrier, reason="test leave")
        self.assertEqual(self.flags[BLUE].status, FLAG_DROPPED)
        self.assertAlmostEqual(self.flags[BLUE].position[0], 5.0, places=2)
        self.assertAlmostEqual(self.flags[BLUE].position[2], 5.0, places=2)
        self.assertFalse(self.carrier_effects(carrier))
        self.assertEqual(self.match.phase, MatchPhase.RUNNING)

    def test_departure_rehosts_round_and_pending_respawn_timers(self):
        victim = self.team(BLUE)[0]
        cid = victim.gameplay_character.character_id
        self.kill(self.team(RED)[0], victim)
        self.armed.clear()
        leaver = self.team(RED)[1]
        CharacterService._forfeit_battleground(
            self.server, self.match, leaver.gameplay_character.character_id,
            reason="test disconnect",
        )
        self.assertEqual(self.match.phase, MatchPhase.RUNNING)
        self.assertIn("round", self.armed)
        self.assertIn("info", self.armed)
        delay, _callback = self.armed[f"respawn-{cid}"]
        self.assertLessEqual(delay, 8.0)

    def test_a_whole_team_leaving_hands_the_win_to_the_other(self):
        for session in self.team(BLUE):
            CharacterService._forfeit_battleground(
                self.server, self.match, session.gameplay_character.character_id,
                reason="test disconnect",
            )
        self.assertEqual(self.match.phase, MatchPhase.OVER)
        self.assertEqual(self.match.winning_team, RED)


class WorldHookTests(unittest.TestCase):
    """The MoveCommand handler reports accepted positions to the match."""

    def setUp(self):
        from dso_server.tests import test_battleground_movement as movement
        self.movement = movement.BattlegroundMovementTests("test_movement_is_accepted_once_round_starts")
        self.movement.setUp()
        self.addCleanup(self.movement.doCleanups)

    def test_accepted_move_reaches_the_match_callback(self):
        harness = self.movement
        CharacterService._start_match_round(harness.server, harness.match, harness.match.info_generation)
        session = harness.home
        events = []
        session.gameplay_match_callback = lambda owner, event: events.append((owner, event))
        move = harness.request(session, 600)
        CharacterService._handle_gameplay_move(session, harness.payload(move))
        self.assertEqual(events, [(session, ("match_moved", move.relative_position))])


if __name__ == "__main__":
    unittest.main()
