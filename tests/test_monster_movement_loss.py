"""Loss/ACK regressions for replaceable monster movement versus reliable events."""
import unittest
from dso_server.runtime.transport import Session, RakNetServer
from dso_server.services.base import CharacterService
from dso_server.tests.test_monster_ai_delivery import FakeSocket
from dso_server.protocol.gameplay_protocol import Move, MoveServerCommand, decode_move_batch


class MonsterMovementLossTests(unittest.TestCase):
    def setUp(self):
        self.socket = FakeSocket()
        self.session = Session(("127.0.0.1", 40100), self.socket)
        self.receiver = Session(("127.0.0.1", 40101), FakeSocket())

    @staticmethod
    def move(actor=101, tick=100, moving=True):
        return MoveServerCommand(Move.from_values(
            (tick / 25, 0, 0), float(moving), 0, 0, tick, 4 if moving else 0), actor)

    def publish(self, commands):
        CharacterService._send_monster_movement_batches(self.session, commands)

    def frames(self):
        return [RakNetServer._parse_frames(packet)[0] for packet, _ in self.socket.sent]

    def test_lost_walking_snapshot_does_not_block_following_attack(self):
        self.publish((self.move(),))
        self.session.send_reliable_ordered(b"\x90attack", label="monster-skill")
        movement, attack = self.frames()
        # The walking packet is lost, with no ACK or retransmit. The attack
        # must still deliver immediately using the next reliable order slot.
        delivered = RakNetServer._queue_or_deliver_inbound_order_locked(self.receiver, attack)
        self.assertEqual(delivered, [(attack["payload"], attack["bit_length"])])
        self.assertEqual(movement["reliability"], 1)
        self.assertEqual(len(self.session.pending_reliable), 1)

    def test_continuous_motion_with_no_acks_does_not_fill_reliable_window(self):
        for tick in range(100, 1100, 4):
            self.publish((self.move(tick=tick),))
        self.assertEqual(len(self.socket.sent), 250)
        self.assertFalse(self.session.pending_reliable)
        self.assertEqual(self.session.fb.order_idx, 0)
        retries, _ = self.session.due_retransmissions(interval=0)
        self.assertFalse(retries)

    def test_lost_stop_is_repeated_without_any_reliable_backlog(self):
        self.session.gameplay_spawned_monster_actor_ids.add(101)
        for tick in range(100, 140, 4):
            self.publish((self.move(tick=tick),))
        self.publish((self.move(tick=140, moving=False),))
        self.assertFalse(self.session.pending_reliable)
        stop = self.frames()[-1]
        self.assertEqual(stop["reliability"], 1)
        # The stop is lost; each scheduled re-send carries it again.
        _command, deadlines = self.session.gameplay_monster_stop_repeats[101]
        self.assertEqual(len(deadlines), 3)
        # Nothing is due yet, so nothing goes out.
        self.assertEqual(CharacterService._send_monster_stop_repeats(self.session, None), 0)
        for _ in range(3):
            self.session.gameplay_monster_stop_repeats[101][1][0] = 0.0
            self.session.gameplay_monster_stop_repeat_next_at = 0.0
            self.assertEqual(CharacterService._send_monster_stop_repeats(self.session, None), 1)
            repeated = self.frames()[-1]
            self.assertEqual(repeated["payload"], stop["payload"])
        self.assertNotIn(101, self.session.gameplay_monster_stop_repeats)
        delivered = RakNetServer._queue_or_deliver_inbound_order_locked(self.receiver, repeated)
        commands = decode_move_batch(*delivered[0]).commands
        self.assertEqual(commands[0].value.scalar_byte, 0)

    def test_newer_movement_cancels_a_pending_stop_repeat(self):
        self.session.gameplay_spawned_monster_actor_ids.add(101)
        self.publish((self.move(tick=140, moving=False),))
        self.publish((self.move(tick=141),))
        self.assertNotIn(101, self.session.gameplay_monster_stop_repeats)
        self.session.gameplay_monster_stop_repeat_next_at = 0.0
        self.assertEqual(CharacterService._send_monster_stop_repeats(self.session, None), 0)

    def test_mixed_batch_travels_as_one_sequenced_datagram(self):
        self.publish((self.move(101), self.move(202, moving=False)))
        (frame,) = self.frames()
        self.assertEqual(frame["reliability"], 1)
        self.assertFalse(self.session.pending_reliable)
        actors = [command.actor_id for command in
                  decode_move_batch(frame["payload"], frame["bit_length"]).commands]
        self.assertEqual(sorted(actors), [101, 202])
        self.assertEqual(set(self.session.gameplay_monster_stop_repeats), {202})

    def test_next_snapshot_repairs_loss_and_late_motion_cannot_undo_stop(self):
        self.publish((self.move(tick=100),))
        self.publish((self.move(tick=104),))
        old, current = self.frames()
        delivered = RakNetServer._queue_or_deliver_inbound_order_locked(self.receiver, current)
        self.assertEqual(decode_move_batch(*delivered[0]).commands[0].value.start_tick, 104)
        self.assertFalse(RakNetServer._queue_or_deliver_inbound_order_locked(self.receiver, old))
        self.publish((self.move(tick=105, moving=False),))
        stop = self.frames()[-1]
        delivered = RakNetServer._queue_or_deliver_inbound_order_locked(self.receiver, stop)
        self.assertEqual(decode_move_batch(*delivered[0]).commands[0].value.scalar_byte, 0)
        self.assertFalse(RakNetServer._queue_or_deliver_inbound_order_locked(self.receiver, current))

    def test_connected_pong_does_not_join_movement_retry_backlog(self):
        for tick in range(100, 300, 4):
            self.publish((self.move(tick=tick),))
        ping = b"\x00" + (12345).to_bytes(8, "big")
        RakNetServer._send_pong(None, self.session, ping)
        pong = self.frames()[-1]
        self.assertEqual(pong["reliability"], 0)
        self.assertEqual(pong["payload"][:9], b"\x03" + ping[1:9])
        self.assertFalse(self.session.pending_reliable)
