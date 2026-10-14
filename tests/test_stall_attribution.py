"""A reported stall must say whether the server was running at all."""

from __future__ import annotations

import unittest

from dso_server.runtime.stall_watchdog import StallWatchdog, attribute_stall


class AttributeStallTests(unittest.TestCase):
    def test_idle_process_points_at_the_host(self):
        verdict = attribute_stall(
            wall_seconds=3.5, cpu_seconds=0.05, steal_seconds=3.2,
            gc_pause_seconds=0.0,
        )
        self.assertIn("mostly not running", verdict)
        self.assertIn("host steal +3.200s", verdict)

    def test_busy_process_is_reported_as_server_work(self):
        verdict = attribute_stall(
            wall_seconds=1.2, cpu_seconds=1.1, steal_seconds=0.0,
            gc_pause_seconds=0.01,
        )
        self.assertIn("busy", verdict)
        self.assertIn("GIL-bound", verdict)

    def test_collector_pause_is_named(self):
        verdict = attribute_stall(
            wall_seconds=0.6, cpu_seconds=0.6, steal_seconds=None,
            gc_pause_seconds=0.5,
        )
        self.assertIn("garbage-collection pause", verdict)
        self.assertIn("longest GC pause 500ms", verdict)
        self.assertNotIn("host steal", verdict)


class WatchdogGcTrackingTests(unittest.TestCase):
    def test_gc_callbacks_record_the_worst_pause_since_the_last_sample(self):
        clock = {"now": 10.0}
        watchdog = StallWatchdog(report=None, clock=lambda: clock["now"])
        watchdog._on_gc("start", {"generation": 2})
        clock["now"] += 0.09
        watchdog._on_gc("stop", {"generation": 2})
        watchdog._on_gc("start", {"generation": 0})
        clock["now"] += 0.001
        watchdog._on_gc("stop", {"generation": 0})
        self.assertAlmostEqual(watchdog._take_worst_gc_pause(), 0.09, places=6)
        self.assertEqual(watchdog._take_worst_gc_pause(), 0.0)
        statistics = watchdog.statistics()
        self.assertEqual(statistics["gc_pause_count"], 2)
        self.assertAlmostEqual(
            statistics["gc_worst_pause_seconds"], 0.09, places=6
        )


if __name__ == "__main__":
    unittest.main()
