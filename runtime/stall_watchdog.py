"""Capture process scheduling stalls and independently blocked map workers.

A responsive watchdog does not imply a responsive simulation worker. Worker
progress is sampled separately, and production reports go directly to a file
so a blocked console cannot hide the blocked thread's stack.
"""

from __future__ import annotations

import gc
import json
import math
import os
import threading
import time
from pathlib import Path
from datetime import datetime, timezone
import weakref

from dso_server.runtime.concurrency import all_thread_tracebacks
from dso_server.runtime.monster_runtime_diagnostics import (
    worker_snapshot,
    describe_worker,
    drain_worker_exits,
)


def _positive_seconds(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be a non-negative number") from exc
    if not math.isfinite(value) or value < 0:
        raise RuntimeError(f"{name} must be a non-negative number")
    return value


# Zero disables the watchdog entirely.
DEFAULT_STALL_THRESHOLD_SECONDS = _positive_seconds(
    "DSO_STALL_WATCHDOG_SECONDS", "0.5"
)
DEFAULT_SAMPLE_INTERVAL_SECONDS = max(
    0.01, _positive_seconds("DSO_STALL_WATCHDOG_INTERVAL_SECONDS", "0.1")
)
# A stall long enough to be worth a full stack dump rather than one line.
DEFAULT_TRACEBACK_THRESHOLD_SECONDS = _positive_seconds(
    "DSO_STALL_WATCHDOG_TRACEBACK_SECONDS", "1.0"
)
# How often the map supervisor looks for instances left without a worker.
DEFAULT_SUPERVISOR_INTERVAL_SECONDS = max(
    0.1, _positive_seconds("DSO_MONSTER_SUPERVISOR_INTERVAL_SECONDS", "1.0")
)


def _read_host_steal_seconds() -> float | None:
    """Seconds the hypervisor withheld from this guest, from ``/proc/stat``.

    ``None`` where the counter is unavailable (Windows, containers without
    procfs). The value is cumulative; callers take differences.
    """

    try:
        with open("/proc/stat", "r", encoding="ascii") as handle:
            first = handle.readline().split()
        if not first or first[0] != "cpu" or len(first) < 9:
            return None
        return int(first[8]) / float(os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, AttributeError, IndexError):
        return None


def attribute_stall(
    *,
    wall_seconds: float,
    cpu_seconds: float,
    steal_seconds: float | None,
    gc_pause_seconds: float,
) -> str:
    """Say what the process was doing while the watchdog could not run.

    The recovery stacks alone cannot: they show what the process resumed
    into. Process CPU time over the same interval can. A stall during which
    the process consumed almost no CPU was not caused by server work at all;
    the host did not run us (VM steal, swap, a paused guest). One that
    consumed CPU for most of the wall time was GIL-bound work, and if the
    collector reports a pause of about the same length, it was the collector.
    """

    wall = max(1e-9, float(wall_seconds))
    cpu = max(0.0, float(cpu_seconds))
    parts = [f"process CPU {cpu:.3f}s of {wall:.3f}s wall"]
    if steal_seconds is not None:
        parts.append(f"host steal +{max(0.0, steal_seconds):.3f}s")
    if gc_pause_seconds > 0.0:
        parts.append(f"longest GC pause {gc_pause_seconds * 1000:.0f}ms")
    if gc_pause_seconds >= 0.5 * wall:
        verdict = "verdict: garbage-collection pause (interpreter frozen)"
    elif cpu <= 0.25 * wall:
        verdict = (
            "verdict: the process was mostly not running; host/VM "
            "scheduling or blocking I/O, not server work"
        )
    else:
        verdict = "verdict: the process was busy (GIL-bound work)"
    return "; ".join(parts) + "; " + verdict


class StallWatchdog:
    """Report scheduling delays and map workers that stop making progress."""

    def __init__(
        self,
        *,
        threshold_seconds: float = DEFAULT_STALL_THRESHOLD_SECONDS,
        interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
        traceback_seconds: float = DEFAULT_TRACEBACK_THRESHOLD_SECONDS,
        name: str = "stall-watchdog",
        report=print,
        clock=time.perf_counter,
        monitor_monsters=False,
        worker_provider=worker_snapshot,
        diagnostic_path=None,
        supervisor=None,
        supervisor_interval_seconds=DEFAULT_SUPERVISOR_INTERVAL_SECONDS,
    ) -> None:
        self.threshold_seconds = max(0.0, float(threshold_seconds))
        self.interval_seconds = max(0.01, float(interval_seconds))
        self.traceback_seconds = max(0.0, float(traceback_seconds))
        self.name = str(name)
        self._report = report
        self.diagnostic_path = (
            Path(diagnostic_path) if diagnostic_path is not None else None
        )
        self.monitor_monsters = bool(monitor_monsters)
        self._worker_provider = worker_provider
        self._worker_reported_at = weakref.WeakKeyDictionary()
        self._worker_sampled_at = weakref.WeakKeyDictionary()
        # Sampling can only describe a worker that exists. The supervisor
        # finds map instances that lost theirs and returns one event per
        # recovery; it is recovery rather than diagnostics, so it keeps
        # running when stall reporting is disabled.
        self._supervisor = supervisor
        self.supervisor_interval_seconds = max(
            0.1, float(supervisor_interval_seconds)
        )
        self._supervised_at = -float("inf")
        self._supervisor_failed_at = -float("inf")
        # Injectable so the detection logic can be exercised without racing a
        # real stall.
        self._clock = clock
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.stall_count = 0
        self.worst_stall_seconds = 0.0
        self.last_stall_seconds = 0.0
        # Collector pauses are measured from the interpreter's own callbacks
        # so a stall can be attributed to them without guessing.
        self._gc_lock = threading.Lock()
        self._gc_started_at: dict[int, float] = {}
        self._gc_worst_pause = 0.0
        self.gc_worst_pause_seconds = 0.0
        self.gc_pause_count = 0
        self._gc_callback_installed = False

    @property
    def enabled(self) -> bool:
        return self.threshold_seconds > 0.0

    def start(self) -> None:
        if not self.enabled and self._supervisor is None:
            self._emit(
                "[StallWatchdog] disabled "
                "(DSO_STALL_WATCHDOG_SECONDS=0)"
            )
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        if not self._gc_callback_installed:
            gc.callbacks.append(self._on_gc)
            self._gc_callback_installed = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=self.name
        )
        self._thread.start()
        if not self.enabled:
            self._emit(
                "[StallWatchdog] stall reporting disabled "
                "(DSO_STALL_WATCHDOG_SECONDS=0); map supervision stays on"
            )
            return
        self._emit(
            f"[StallWatchdog] armed: reporting stalls over "
            f"{self.threshold_seconds:.2f}s "
            f"(sampling every {self.interval_seconds * 1000.0:.0f}ms, "
            f"stacks over {self.traceback_seconds:.2f}s)"
        )

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self.interval_seconds * 5.0)
        self._thread = None
        if self._gc_callback_installed:
            try:
                gc.callbacks.remove(self._on_gc)
            except ValueError:
                pass
            self._gc_callback_installed = False

    def _on_gc(self, phase: str, info) -> None:
        # Runs on whichever thread triggered the collection, with the GIL.
        # Keep it to two dictionary operations.
        generation = int(info.get("generation", 0))
        now = self._clock()
        with self._gc_lock:
            if phase == "start":
                self._gc_started_at[generation] = now
                return
            started = self._gc_started_at.pop(generation, None)
            if started is None:
                return
            pause = now - started
            self.gc_pause_count += 1
            if pause > self._gc_worst_pause:
                self._gc_worst_pause = pause
            if pause > self.gc_worst_pause_seconds:
                self.gc_worst_pause_seconds = pause

    def _take_worst_gc_pause(self) -> float:
        with self._gc_lock:
            worst, self._gc_worst_pause = self._gc_worst_pause, 0.0
            return worst

    def statistics(self) -> dict[str, float | int]:
        with self._lock:
            return {
                "stall_count": self.stall_count,
                "worst_stall_seconds": self.worst_stall_seconds,
                "last_stall_seconds": self.last_stall_seconds,
                "gc_pause_count": self.gc_pause_count,
                "gc_worst_pause_seconds": self.gc_worst_pause_seconds,
            }

    def _emit(self, message):
        if self.diagnostic_path is not None:
            try:
                path = self.diagnostic_path
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() and path.stat().st_size > 4 * 1024 * 1024:
                    path.replace(path.with_suffix(".previous.log"))
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(
                        datetime.now(timezone.utc).isoformat()
                        + " " + message + "\n"
                    )
            except OSError:
                # Diagnostic I/O must never retire the monitor.
                pass
        if self._report is not None:
            self._report(message)

    def _sample_monsters(self, now):
        if not self.monitor_monsters:
            return
        for event in drain_worker_exits():
            self._emit("[MonsterWatchdog] " + json.dumps(event, sort_keys=True))
        for worker in self._worker_provider():
            try:
                self._sample_worker(worker, now)
            except Exception as exc:
                # A racy diagnostic snapshot must not disable the monitor.
                if now - self._worker_reported_at.get(worker, -float("inf")) >= 10.0:
                    self._worker_reported_at[worker] = now
                    self._emit(
                        "[MonsterWatchdog] snapshot failed: "
                        + type(exc).__name__
                    )

    def _sample_worker(self, worker, now):
        session = worker.session
        if getattr(session, "gameplay_chase_timer", None) is not worker:
            return
        age = now - worker.diagnostic_progress_at
        stalled = age >= max(1.0, self.threshold_seconds)
        last_report = self._worker_reported_at.get(worker, -float("inf"))
        last_sample = self._worker_sampled_at.get(worker, -float("inf"))
        if stalled and now - last_report >= 10.0:
            snapshot = describe_worker(worker, now)
            self._worker_reported_at[worker] = now
            self._worker_sampled_at[worker] = now
            self._emit(
                "[MonsterWatchdog] worker stopped progressing: " + snapshot
                + "\nThread stacks sampled during the stall:\n"
                + all_thread_tracebacks()
            )
        elif now - last_sample >= 10.0:
            snapshot = describe_worker(worker, now)
            self._worker_sampled_at[worker] = now
            self._emit("[MonsterWatchdog] sample: " + snapshot)

    def _supervise(self, now):
        if (
            self._supervisor is None
            or now - self._supervised_at < self.supervisor_interval_seconds
        ):
            return
        self._supervised_at = now
        try:
            events = tuple(self._supervisor() or ())
        except Exception as exc:
            # Recovery is best effort; a failing pass must not stop sampling.
            if now - self._supervisor_failed_at >= 10.0:
                self._supervisor_failed_at = now
                self._emit(
                    "[MonsterWatchdog] map supervisor failed: "
                    f"{type(exc).__name__}: {exc}"
                )
            return
        for event in events:
            self._emit("[MonsterWatchdog] " + json.dumps(event, sort_keys=True))

    def _run(self) -> None:
        previous = self._clock()
        cpu_previous = time.process_time()
        steal_previous = _read_host_steal_seconds()
        while not self._stop_event.wait(self.interval_seconds):
            now = self._clock()
            cpu_now = time.process_time()
            steal_now = _read_host_steal_seconds()
            # Everything beyond the interval we asked for is time in which
            # this thread was ready but not running.
            wall = now - previous
            overshoot = wall - self.interval_seconds
            cpu_delta = cpu_now - cpu_previous
            steal_delta = (
                None
                if steal_now is None or steal_previous is None
                else steal_now - steal_previous
            )
            gc_pause = self._take_worst_gc_pause()
            previous = now
            cpu_previous = cpu_now
            steal_previous = steal_now
            if self.monitor_monsters or self._supervisor is not None:
                if self.enabled:
                    self._sample_monsters(now)
                self._supervise(now)
                # File I/O, stack formatting and recovery are our own work,
                # not evidence that the process missed a scheduling deadline.
                previous = self._clock()
                cpu_previous = time.process_time()
                steal_previous = _read_host_steal_seconds()
            if not self.enabled or overshoot < self.threshold_seconds:
                continue
            with self._lock:
                self.stall_count += 1
                self.last_stall_seconds = overshoot
                self.worst_stall_seconds = max(
                    self.worst_stall_seconds, overshoot
                )
                count = self.stall_count
            message = (
                f"[StallWatchdog] process stalled {overshoot:.3f}s "
                f"(stall #{count}); watchdog missed its sampling interval; "
                + attribute_stall(
                    wall_seconds=wall,
                    cpu_seconds=cpu_delta,
                    steal_seconds=steal_delta,
                    gc_pause_seconds=gc_pause,
                )
            )
            if overshoot >= self.traceback_seconds:
                # Stacks are sampled after the stall, so they show what the
                # process resumed into rather than a frozen instant. A thread
                # still inside the same call is a useful lead, but these
                # recovery stacks alone do not prove what caused the delay.
                message += (
                    "\nAll Python thread tracebacks (sampled on recovery):\n"
                    + all_thread_tracebacks()
                )
            self._emit(message)
            # Do not let time spent reporting count as a second stall.
            previous = self._clock()
            cpu_previous = time.process_time()
            steal_previous = _read_host_steal_seconds()


__all__ = [
    "DEFAULT_SAMPLE_INTERVAL_SECONDS",
    "DEFAULT_STALL_THRESHOLD_SECONDS",
    "DEFAULT_SUPERVISOR_INTERVAL_SECONDS",
    "DEFAULT_TRACEBACK_THRESHOLD_SECONDS",
    "StallWatchdog",
    "attribute_stall",
]
