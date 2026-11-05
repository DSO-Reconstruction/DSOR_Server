"""Netcode counters: refused moves, held streams and expired movement segments."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dso_server.protocol.gameplay_protocol import Move, MoveServerCommand
from dso_server.runtime import latency_stats
from dso_server.runtime.transport import Session
from dso_server.services.base import CharacterService
from dso_server.services import presence_movement
from dso_server.tests.test_monster_ai_delivery import FakeSocket


def walking(actor=101, tick=100, moving=True):
    return MoveServerCommand(Move.from_values(
        (tick / 25, 0, 0), float(moving), 0, 0, tick, 18 if moving else 0), actor)


class CounterTestCase(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.events = []
        for name, sink in (("record", self.records), ("count", self.events)):
            patcher = patch.object(latency_stats, name, side_effect=lambda *args, sink=sink: sink.append(args))
            patcher.start()
            self.addCleanup(patcher.stop)


class MonsterSegmentExpiryTests(CounterTestCase):
    def setUp(self):
        super().setUp()
        self.session = Session(("127.0.0.1", 40200), FakeSocket())
        self.session.gameplay_spawned_monster_actor_ids.add(101)
        self.clock = 100.0
        patcher = patch("dso_server.services.monster_activity.time.perf_counter",
                        side_effect=lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def publish(self, *commands):
        CharacterService._send_monster_movement_batches(self.session, commands)

    def test_a_sample_inside_the_horizon_is_not_an_expiry(self):
        self.publish(walking(tick=100))
        self.clock += 0.5
        self.publish(walking(tick=112))
        self.assertEqual(self.events, [])

    def test_a_sample_after_the_horizon_counts_once_with_its_gap(self):
        self.publish(walking(tick=100))
        self.clock += 0.72 + 0.3
        self.publish(walking(tick=125))
        self.assertEqual(self.events, [("monster segment expired", "127.0.0.1:40200")])
        (kind, gap), = self.records
        self.assertEqual(kind, "monster segment gap")
        self.assertAlmostEqual(gap, 0.3, places=6)

    def test_a_stop_ends_the_horizon(self):
        self.publish(walking(tick=100))
        self.publish(walking(tick=101, moving=False))
        self.clock += 5.0
        self.publish(walking(tick=226))
        self.assertEqual(self.events, [])

    def test_resync_restarts_the_horizon_without_counting(self):
        self.publish(walking(tick=100))
        self.clock += 2.0
        CharacterService._record_delivered_monster_movements(
            self.session, None, (walking(tick=150),))
        self.assertEqual(self.events, [])


class HeldStreamTests(CounterTestCase):
    def test_a_repaired_world_frame_counts_its_hold_beyond_one_round_trip(self):
        session = Session(("127.0.0.1", 40201), FakeSocket())
        session._note_repaired_reliable(
            (("monster-skill", 0.35), ("achievement-progress", 0.4)), 0.1)
        self.assertIn(("held stream (channel 0)", 0.25), [
            (kind, round(value, 6)) for kind, value in self.records])
        self.assertEqual(self.events, [("held stream over RTT + 50 ms", "127.0.0.1:40201")])
        self.assertIn("held stream (meta lane)", [kind for kind, _value in self.records])

    def test_a_repair_within_rtt_plus_50_ms_is_recorded_but_not_a_freeze(self):
        session = Session(("127.0.0.1", 40202), FakeSocket())
        # Held 120 ms: over a flat 50 ms, inside a 100 ms round trip + 50 ms.
        session._note_repaired_reliable((("monster-skill", 0.12), ("monster-skill", 0.22)), 0.1)
        self.assertEqual(self.events, [])
        self.assertEqual(len(self.records), 2)


class PlayerSegmentExpiryTests(CounterTestCase):
    def viewer(self):
        viewer = Session(("127.0.0.1", 40203), FakeSocket())
        viewer.send_unreliable_sequenced = lambda *args, **kwargs: object()
        return viewer

    def publish(self, viewer, tick, source_move):
        own = Move.from_values((0, 0, 0), 0, 0, 0, tick, 0)
        relayed = presence_movement.relayed_player_move(source_move, None)
        return CharacterService._publish_viewer_movement(
            viewer, own, ((object(), 7, source_move, relayed),), tick)

    def test_a_late_moving_player_counts_once_after_its_18_ticks(self):
        viewer = self.viewer()
        moving = Move.from_values((1, 0, 0), 1.0, 0, 0, 100, 20)
        self.publish(viewer, 100, moving)
        self.publish(viewer, 125, Move.from_values((2, 0, 0), 1.0, 0, 0, 125, 20))
        self.assertEqual(self.events, [("player segment expired", "127.0.0.1:40203")])
        (kind, gap), = self.records
        self.assertEqual(kind, "player segment gap")
        self.assertAlmostEqual(gap, 7 / 25)

    def test_regular_relay_is_not_an_expiry(self):
        viewer = self.viewer()
        for tick in range(100, 160, 3):
            self.publish(viewer, tick, Move.from_values((tick / 25, 0, 0), 1.0, 0, 0, tick, 20))
        self.assertEqual(self.events, [])


class ReportTests(unittest.TestCase):
    def test_events_print_per_session_in_the_report(self):
        with patch.object(latency_stats, "_samples", {}), \
                patch.object(latency_stats, "_counts", {}), \
                patch.object(latency_stats, "_maxima", {}), \
                patch.object(latency_stats, "_events", {
                    "move refused": {"10.0.0.1:1": 3, "10.0.0.2:2": 1},
                }):
            report = latency_stats._snapshot_locked(30.0)
        self.assertIn("move refused", report)
        self.assertIn("4: 10.0.0.1:1 3, 10.0.0.2:2 1", report)


if __name__ == "__main__":
    unittest.main()
