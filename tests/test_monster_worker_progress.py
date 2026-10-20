"""Worker lifecycle failures must not strand a live map without simulation."""
from types import SimpleNamespace
import threading
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from dso_server.services.character_server import CharacterServer
from dso_server.services.monster_combat import _FixedRateChaseLoop
from dso_server.runtime.transport import Session
from dso_server.runtime.monster_runtime_diagnostics import (
    mark_phase,
    worker_snapshot,
    drain_worker_exits,
)
from dso_server.runtime.stall_watchdog import StallWatchdog
from dso_server.protocol.gameplay_protocol import Move


class MonsterWorkerLifecycleTests(unittest.TestCase):
    def session(self):
        session = Session(("127.0.0.1", 41991), object())
        session.connected = True
        session.gameplay_monsters_spawned = True
        session.gameplay_combat_state = SimpleNamespace(
            player_summons={}, has_regenerating_players=False,
        )
        return session

    def test_ownership_lock_timeout_retries_instead_of_killing_worker(self):
        session = self.session()
        loop = _FixedRateChaseLoop(session, 0)
        session.gameplay_chase_timer = loop
        checked = threading.Event()
        attempts = []
        original = loop._is_installed_worker

        def transient_timeout():
            attempts.append(1)
            if len(attempts) == 1:
                raise TimeoutError("injected ownership lock contention")
            return original()

        def tick(*args):
            checked.set()
            return False

        with (
            patch.object(loop, "_is_installed_worker", transient_timeout),
            patch.object(CharacterServer, "_monster_chase_tick", tick),
            patch("threading.excepthook") as thread_error,
        ):
            loop.start()
            try:
                self.assertTrue(checked.wait(1.0), "ownership timeout killed the worker")
            finally:
                loop.cancel()
                loop._thread.join(2.0)
            thread_error.assert_not_called()
        self.assertIsNone(session.gameplay_chase_timer)

    def test_dead_installed_worker_can_be_replaced_without_changing_generation(self):
        session = self.session()
        old = _FixedRateChaseLoop(session, session.gameplay_chase_generation)
        # An exited thread whose cleanup could not acquire flow_lock leaves
        # precisely this stale installed handle behind.
        old._thread = threading.Thread(target=lambda: None)
        old._thread.start()
        old._thread.join()
        session.gameplay_chase_timer = old
        generation = session.gameplay_chase_generation
        entered = threading.Event()

        def tick(*args):
            entered.set()
            return True

        with patch.object(CharacterServer, "_monster_chase_tick", tick):
            started = CharacterServer._start_monster_chase(session)
            try:
                self.assertTrue(started, "a dead installed worker prevented recovery")
                self.assertTrue(entered.wait(1.0))
                self.assertIsNot(session.gameplay_chase_timer, old)
                self.assertEqual(session.gameplay_chase_generation, generation)
            finally:
                loop = session.gameplay_chase_timer
                if loop is not None:
                    loop.cancel()
                    loop._thread.join(2.0)

    def test_restarting_worker_preserves_forward_server_clock(self):
        for clock_tick, client_tick, expected in (
            (5000, 1900, 5000),
            (1900, 5000, 5000),
            (0xFFFFFFFE, 2, 2),
            (2, 0xFFFFFFFE, 2),
        ):
            with self.subTest(clock_tick=clock_tick, client_tick=client_tick):
                session = self.session()
                session.gameplay_chase_server_tick = clock_tick
                session.gameplay_last_move = Move.from_values(
                    (0, 0, 0), 0, 0, 0, client_tick, 0,
                )
                # Inspect the reservation before any tick advances the clock.
                with patch.object(_FixedRateChaseLoop, "start"):
                    self.assertTrue(CharacterServer._start_monster_chase(session))
                self.assertEqual(session.gameplay_chase_server_tick, expected)

    def test_reserved_worker_must_not_be_replaced_before_thread_start(self):
        session = self.session()
        reserved = _FixedRateChaseLoop(session, 0)
        session.gameplay_chase_timer = reserved
        self.assertFalse(CharacterServer._start_monster_chase(session))
        self.assertIs(session.gameplay_chase_timer, reserved)

    def test_live_blocked_worker_must_not_be_replaced(self):
        session = self.session()
        entered = threading.Event()
        release = threading.Event()

        def tick(*args):
            entered.set()
            release.wait(3.0)
            return True

        with patch.object(CharacterServer, "_monster_chase_tick", tick):
            self.assertTrue(CharacterServer._start_monster_chase(session))
            loop = session.gameplay_chase_timer
            try:
                self.assertTrue(entered.wait(1.0))
                self.assertFalse(CharacterServer._start_monster_chase(session))
                self.assertIs(session.gameplay_chase_timer, loop)
            finally:
                loop.cancel()
                release.set()
                loop._thread.join(2.0)


class MonsterWorkerDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        drain_worker_exits()

    def test_clean_worker_exit_is_reported_after_handle_is_cleared(self):
        session = Session(("127.0.0.1", 41990), object())
        loop = _FixedRateChaseLoop(session, 0)
        session.gameplay_chase_timer = loop
        with patch.object(CharacterServer, "_monster_chase_tick", return_value=False):
            loop.start()
            loop._thread.join(2.0)
        self.assertFalse(loop._thread.is_alive())
        self.assertIsNone(session.gameplay_chase_timer)
        reports = []
        watchdog = StallWatchdog(monitor_monsters=True, report=reports.append)
        watchdog._sample_monsters(time.perf_counter())
        self.assertTrue(any('"event": "worker-exited"' in line for line in reports))
        self.assertTrue(any('"last_result": false' in line for line in reports))

    def test_blocked_worker_is_captured_without_console_or_gameplay_locks(self):
        session = Session(("127.0.0.1", 41992), object())
        session.gameplay_map = "a0303_wildcave"
        entered = threading.Event()
        release = threading.Event()

        def blocked_monster_fanout(tick_session, *args):
            # The patch is class-wide: a monster worker another test left
            # running must neither block here nor signal ``entered``.
            if tick_session is not session:
                return True
            mark_phase(session, "monster-activity-fanout")
            entered.set()
            release.wait(5.0)
            return True

        loop = _FixedRateChaseLoop(session, 0)
        session.gameplay_chase_timer = loop
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(CharacterServer, "_monster_chase_tick", blocked_monster_fanout),
        ):
            log = Path(directory) / "monster-stalls.log"
            watchdog = StallWatchdog(
                monitor_monsters=True, worker_provider=lambda: (loop,),
                report=None, diagnostic_path=log, interval_seconds=0.01,
            )
            loop.start()
            try:
                self.assertTrue(entered.wait(1.0))
                self.assertIn(loop, worker_snapshot())
                # Age the real blocked thread, avoiding a one-second sleep.
                loop.diagnostic_progress_at = time.perf_counter() - 2.0
                with (
                    session.flow_lock,
                    session.outbound_lock,
                    session.pending_lock,
                    patch("builtins.print", side_effect=AssertionError("console unavailable")),
                ):
                    watchdog.start()
                    deadline = time.perf_counter() + 2.0
                    captured = ""
                    while time.perf_counter() < deadline:
                        if log.exists():
                            captured = log.read_text(encoding="utf-8")
                            if "blocked_monster_fanout" in captured:
                                break
                        time.sleep(0.01)
                    watchdog.stop()
                    self.assertIn("worker stopped progressing", captured)
                    self.assertIn("blocked_monster_fanout", captured)
                    self.assertIn("a0303_wildcave", captured)
                    self.assertIn('"phase": "monster-activity-fanout"', captured)
                    self.assertTrue(loop._thread.is_alive())
            finally:
                watchdog.stop()
                loop.cancel()
                release.set()
                loop._thread.join(2.0)

    def test_sustained_stall_is_rate_limited_and_recovery_is_sampled(self):
        session = Session(("127.0.0.1", 41993), object())
        loop = _FixedRateChaseLoop(session, 0)
        session.gameplay_chase_timer = loop
        now = time.perf_counter()
        loop.diagnostic_progress_at = now - 2.0
        reports = []
        watchdog = StallWatchdog(
            monitor_monsters=True, worker_provider=lambda: (loop,),
            report=reports.append,
        )
        watchdog._sample_monsters(now)
        watchdog._sample_monsters(now + 0.1)
        watchdog._sample_monsters(now + 9.0)
        self.assertEqual(len(reports), 1)
        loop.diagnostic_progress_at = now + 11.0
        loop.diagnostic_ticks_completed = 8
        loop.diagnostic_last_result = True
        watchdog._sample_monsters(now + 11.0)
        self.assertEqual(len(reports), 2)
        self.assertIn('"ticks_completed": 8', reports[-1])
        self.assertIn("[MonsterWatchdog] sample:", reports[-1])

    def test_retired_worker_cannot_mark_replacement_phase(self):
        session = Session(("127.0.0.1", 41994), object())
        replacement = _FixedRateChaseLoop(session, 0)
        session.gameplay_chase_timer = replacement
        mark_phase(session, "old-worker-finishing")
        self.assertEqual(replacement.diagnostic_phase, "reserved")

    def test_bad_snapshot_does_not_hide_other_stalled_workers(self):
        sessions = [Session(("127.0.0.1", port), object()) for port in (41995, 41996)]
        loops = [_FixedRateChaseLoop(session, 0) for session in sessions]
        now = time.perf_counter()
        for session, loop in zip(sessions, loops):
            session.gameplay_chase_timer = loop
            loop.diagnostic_progress_at = now - 2.0
        sessions[0].pending_reliable = {1: None}  # malformed diagnostic fixture
        reports = []
        watchdog = StallWatchdog(
            monitor_monsters=True, worker_provider=lambda: loops, report=reports.append,
        )
        watchdog._sample_monsters(now)
        self.assertTrue(any("snapshot failed" in line for line in reports))
        self.assertTrue(any("worker stopped progressing" in line for line in reports))

    def test_diagnostic_log_rotation_keeps_the_previous_report(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "monster-stalls.log"
            log.write_text("x" * (4 * 1024 * 1024 + 1), encoding="utf-8")
            watchdog = StallWatchdog(report=None, diagnostic_path=log)
            watchdog._emit("next report")
            self.assertIn("next report", log.read_text(encoding="utf-8"))
            self.assertGreater(log.with_suffix(".previous.log").stat().st_size, 4 * 1024 * 1024)
