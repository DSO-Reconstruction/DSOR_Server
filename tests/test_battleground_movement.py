"""Countdown movement corrects client prediction and cannot leak into play."""

from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from dso_server.protocol.gameplay_protocol import Move, decode_force_transform_batch
from dso_server.gameplay.pvp_match_state import MatchPhase
from dso_server.services.base import CharacterService
from dso_server.tests import test_battleground_polish as polish
from dso_server.tests import test_pvp_matchmaking as fixtures


class BattlegroundMovementTests(unittest.TestCase):
    _arrive = fixtures.BattlegroundTests._arrive
    _combat = polish.BattlegroundPolishTests._combat

    def setUp(self):
        fixtures.BattlegroundTests.setUp(self)
        self.combat = self._combat()
        self.frame = self.combat.encounter.coordinate_frame
        for session in (self.home, self.away):
            session.gameplay_coordinate_frame = self.frame
            session.send_unreliable_sequenced = Mock()
        self._arrive(self.home)
        self._arrive(self.away)
        for session in (self.home, self.away):
            session.recorder.messages.clear()
            session.gameplay_presence_callback = Mock()
        for name in ("_cull_distant_monster_groups", "_cancel_mount_cast",
                     "_reconcile_world_loot_visibility", "_pickup_nearby_auto_loot"):
            patcher = patch.object(CharacterService, name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(self, session, tick):
        """A step the player could really take: on walkable ground, in a
        straight line from where it stands (services/movement_validation.py
        refuses the rest)."""
        from dso_server.services import movement_validation as mv
        previous = session.gameplay_last_move
        start = previous.relative_position
        navigation = getattr(self.combat.encounter, "navigation", None)
        for dx, dz in ((1.0, 1.0), (1.0, -1.0), (-1.0, 1.0), (-1.0, -1.0), (0.5, 0.0), (0.0, 0.5), (-0.5, 0.0), (0.0, -0.5)):
            goal = (start[0] + dx, start[1], start[2] + dz)
            if not isinstance(navigation, mv.PathEngineNavigationMesh) or mv.walkable_path(
                navigation, self.frame.wire_to_world(start), self.frame.wire_to_world(goal),
            ):
                break
        return Move.from_values(goal, 3.0, 1.0, 1.0, tick, 25)

    @staticmethod
    def payload(move):
        return b"\x8b\x5f\x00" + move.encode_body()

    def test_both_players_are_held_and_corrected_during_every_countdown_digit(self):
        anchors = {s: s.gameplay_last_move for s in (self.home, self.away)}
        for seconds, tick in ((3, 500), (2, 525), (1, 550)):
            CharacterService._match_countdown_digit(
                self.server, self.match, self.match.info_generation, seconds=seconds,
            )
            for session, anchor in anchors.items():
                CharacterService._handle_gameplay_move(session, self.payload(self.request(session, tick)))
                held = session.gameplay_last_move
                self.assertEqual(held.relative_position, anchor.relative_position)
                self.assertEqual((held.scalar_byte, held.duration_ticks), (0, 0))
                self.assertEqual(held.start_tick, tick)
                self.assertEqual(session.gameplay_chase_server_tick, tick)
                data, bits, label = session.recorder.messages[-1]
                self.assertEqual(label, "player-locked-movement-correction")
                transform, actor = decode_force_transform_batch(data, bits)
                self.assertEqual(actor, 1)
                for actual, expected in zip(transform.position, self.frame.wire_to_world(anchor.relative_position)):
                    self.assertAlmostEqual(actual, expected, places=4)
                # The attempted position must not reach peers or world interactions.
                session.gameplay_presence_callback.assert_not_called()
        self.assertEqual(self.match.phase, MatchPhase.COUNTDOWN)

    def test_expired_client_freeze_still_cannot_move_before_round_start(self):
        session = self.home
        anchor = session.gameplay_last_move
        session.gameplay_runtime_status_effects.clear()
        CharacterService._handle_gameplay_move(session, self.payload(self.request(session, 10000)))
        self.assertEqual(session.gameplay_last_move.relative_position, anchor.relative_position)
        self.assertEqual(session.recorder.messages[-1][2], "player-locked-movement-correction")

    def test_corrective_move_cancels_existing_velocity(self):
        self.home.gameplay_last_move = replace(
            self.home.gameplay_last_move, scalar_byte=192, duration_ticks=50,
        )
        CharacterService._handle_gameplay_move(self.home, self.payload(self.request(self.home, 500)))
        packet, bits = self.home.send_unreliable_sequenced.call_args.args[0]
        # Server Move is the same body as the client command, followed by actor id.
        from dso_server.protocol.gameplay_protocol import decode_move
        stopped = decode_move(b"\x8b" + packet[1:18])
        self.assertEqual((stopped.scalar_byte, stopped.duration_ticks), (0, 0))
        self.assertEqual(stopped.start_tick, 500)

    def test_movement_is_accepted_once_round_starts(self):
        CharacterService._start_match_round(self.server, self.match, self.match.info_generation)
        for session in (self.home, self.away):
            session.recorder.messages.clear()
            session.gameplay_presence_callback.reset_mock()
            move = self.request(session, 600)
            CharacterService._handle_gameplay_move(session, self.payload(move))
            self.assertEqual(session.gameplay_last_move, move)
            session.gameplay_presence_callback.assert_called_once_with(session, "move")
            self.assertFalse(any(label == "player-locked-movement-correction"
                                 for _, _, label in session.recorder.messages))

    def test_queued_countdown_input_is_discarded_after_start(self):
        self.server.is_session_current = lambda _: True
        session = self.home
        move = self.request(session, 500)
        with patch.object(self.server, "_schedule_gameplay_skill_dispatch"), \
                patch.object(self.server, "_dispatch_gameplay_skill_command_now") as dispatch:
            self.server._enqueue_gameplay_skill_command(session, 0x5F, self.payload(move))
            token = session.gameplay_skill_dispatch_scheduled_token
            CharacterService._start_match_round(self.server, self.match, self.match.info_generation)
            self.server._drain_gameplay_skill_dispatch(session, token)
            dispatch.assert_not_called()
            self.server._enqueue_gameplay_skill_command(session, 0x5F, self.payload(self.request(session, 600)))
            self.server._drain_gameplay_skill_dispatch(session, session.gameplay_skill_dispatch_scheduled_token)
            dispatch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
