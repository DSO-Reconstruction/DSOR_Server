"""Full garbage collections run on the server's schedule, not CPython's."""

from __future__ import annotations

import gc
import unittest

from dso_server.runtime.gc_tuning import (
    NEVER_AUTOMATIC_FULL_COLLECTION,
    YOUNG_THRESHOLDS,
    ManagedGarbageCollector,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ManagedGarbageCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_thresholds = gc.get_threshold()
        self.addCleanup(gc.set_threshold, *self.previous_thresholds)
        self.addCleanup(gc.unfreeze)

    def test_install_freezes_the_heap_and_stops_automatic_full_collections(self):
        reports = []
        collector = ManagedGarbageCollector(
            policy="managed", report=reports.append, interval_seconds=60,
        )
        collector.install()
        try:
            self.assertTrue(collector.installed)
            self.assertGreater(gc.get_freeze_count(), 0)
            self.assertEqual(
                gc.get_threshold(),
                (*YOUNG_THRESHOLDS, NEVER_AUTOMATIC_FULL_COLLECTION),
            )
            self.assertTrue(any("managed policy" in line for line in reports))
        finally:
            collector.stop()
        self.assertEqual(gc.get_threshold(), self.previous_thresholds)

    def test_default_policy_leaves_the_interpreter_alone(self):
        collector = ManagedGarbageCollector(policy="default", report=None)
        collector.install()
        self.assertFalse(collector.installed)
        self.assertEqual(gc.get_threshold(), self.previous_thresholds)
        collector.stop()

    def test_busy_maps_defer_the_collection_until_the_bound(self):
        clock = FakeClock()
        busy = {"value": True}
        collector = ManagedGarbageCollector(
            policy="managed", report=None, clock=clock,
            busy_check=lambda: busy["value"],
            interval_seconds=60, max_deferral_seconds=120,
        )
        # First due cycle: maps are busy, so it waits and re-checks soon.
        next_due = collector._cycle(clock.now)
        self.assertEqual(collector.collections, 0)
        self.assertEqual(collector.deferrals, 1)
        self.assertLessEqual(next_due - clock.now, 5.0)
        # Still busy inside the deferral window: keep waiting.
        clock.now += 60
        collector._cycle(clock.now)
        self.assertEqual(collector.collections, 0)
        # Past the bound the collection runs even though maps are busy.
        clock.now += 61
        next_due = collector._cycle(clock.now)
        self.assertEqual(collector.collections, 1)
        self.assertEqual(next_due, clock.now + 60)

    def test_quiet_maps_collect_immediately(self):
        clock = FakeClock()
        collector = ManagedGarbageCollector(
            policy="managed", report=None, clock=clock,
            busy_check=lambda: False, interval_seconds=60,
            unfreeze_every_cycles=3,
        )
        garbage = []
        for _ in range(100):
            node = []
            node.append(node)
            garbage.append(node)
        del garbage
        collector._cycle(clock.now)
        stats = collector.statistics()
        self.assertEqual(stats["collections"], 1)
        self.assertEqual(stats["full_collections"], 0)
        self.assertGreaterEqual(stats["worst_collection_seconds"], 0.0)

    def test_every_nth_cycle_walks_the_frozen_heap_and_refreezes(self):
        clock = FakeClock()
        collector = ManagedGarbageCollector(
            policy="managed", report=None, clock=clock,
            busy_check=lambda: False, interval_seconds=60,
            unfreeze_every_cycles=3,
        )
        gc.collect()
        gc.freeze()
        frozen_before = gc.get_freeze_count()
        for _ in range(2):
            collector._cycle(clock.now)
        self.assertEqual(collector.statistics()["full_collections"], 0)
        self.assertEqual(gc.get_freeze_count(), frozen_before)
        collector._cycle(clock.now)
        self.assertEqual(collector.statistics()["full_collections"], 1)
        # Survivors are frozen again, so the next ordinary walk stays small.
        self.assertGreater(gc.get_freeze_count(), 0)

    def test_encounter_freeze_hook_is_inert_without_the_policy(self):
        from dso_server.runtime import gc_tuning

        gc.unfreeze()
        gc_tuning.freeze_long_lived_addition()
        self.assertEqual(gc.get_freeze_count(), 0)
        collector = ManagedGarbageCollector(policy="managed", report=None)
        collector.install()
        try:
            gc.unfreeze()
            gc_tuning.freeze_long_lived_addition()
            self.assertGreater(gc.get_freeze_count(), 0)
        finally:
            collector.stop()
        gc.unfreeze()
        gc_tuning.freeze_long_lived_addition()
        self.assertEqual(gc.get_freeze_count(), 0)


if __name__ == "__main__":
    unittest.main()
