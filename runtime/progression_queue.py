"""Off-loop execution of progression side effects.

Movement, casts, hits and the monster tick are the real-time loop: what a
player does must take effect within milliseconds. Quests, achievements,
daily challenges, rankings, kill XP and loose account saves are consequences
of that loop, not part of it. They used to run inline on whichever thread
landed the killing blow or picked up the gold, holding the player's input
gate, rebuilding state and waiting for SQLite while the next cast queued.

``defer`` hands such work to one ``ProgressionQueue`` worker per process:

* FIFO, one thread, so every session's side effects keep their order and
  never race each other (they used to run on up to ten hit workers at once);
* budgeted: it runs at most ``DSO_PROGRESSION_BUDGET_MS`` (6) of jobs per
  ``DSO_PROGRESSION_WINDOW_MS`` (20) and yields the interpreter lock after
  every job, so a backlog delays progression instead of the combat loop;
* guarded: a job carries a predicate captured at submission (same map
  instance, same character) and is skipped if the player has moved on;
* bounded: when full, ``defer`` refuses and the caller runs the work inline,
  which is slower but never loses it.

Composed test servers never install a queue, so ``defer`` returns False and
everything stays synchronous there. ``DSO_PROGRESSION_QUEUE=0`` restores the
inline behaviour in production.
"""

from __future__ import annotations

from collections import deque
import os
import threading
import time
import traceback

from dso_server.runtime import latency_stats


ENABLED = os.getenv("DSO_PROGRESSION_QUEUE", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
BUDGET_SECONDS = max(0.001, float(os.getenv("DSO_PROGRESSION_BUDGET_MS", "6")) / 1000.0)
WINDOW_SECONDS = max(
    BUDGET_SECONDS, float(os.getenv("DSO_PROGRESSION_WINDOW_MS", "20")) / 1000.0
)
CAPACITY = max(256, int(os.getenv("DSO_PROGRESSION_CAPACITY", "50000")))
# Report jobs that waited this long before running (rate limited).
SLOW_WAIT_SECONDS = 0.5


class ProgressionQueue:
    """One budgeted worker thread running deferred side effects in order."""

    def __init__(self, *, name: str = "progression") -> None:
        self.name = name
        self._jobs: deque = deque()
        self._condition = threading.Condition(threading.Lock())
        self._stopping = False
        self._thread: threading.Thread | None = None
        self.submitted = self.completed = self.skipped = self.failed = 0
        self.refused = 0
        self.max_wait_seconds = 0.0
        self._last_wait_report_at = 0.0

    @property
    def worker_ident(self) -> int | None:
        thread = self._thread
        return thread.ident if thread is not None else None

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run, name=f"{self.name}-worker", daemon=True,
            )
        self._thread.start()

    def stop(self, *, timeout: float = 3.0) -> None:
        """Run what is queued (up to ``timeout``), then stop the worker."""

        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def backlog(self) -> int:
        with self._condition:
            return len(self._jobs)

    def submit(self, function, args=(), kwargs=None, *, label: str = "",
               guard=None) -> bool:
        with self._condition:
            if self._stopping or self._thread is None or len(self._jobs) >= CAPACITY:
                self.refused += 1
                return False
            self._jobs.append(
                (function, tuple(args), dict(kwargs or {}), label, guard,
                 time.perf_counter())
            )
            self.submitted += 1
            self._condition.notify()
        return True

    def _run(self) -> None:
        window_started = time.perf_counter()
        used = 0.0
        while True:
            with self._condition:
                while not self._jobs and not self._stopping:
                    self._condition.wait(0.5)
                if not self._jobs:
                    return
                function, args, kwargs, label, guard, queued_at = self._jobs.popleft()
            started = time.perf_counter()
            waited = started - queued_at
            if waited > self.max_wait_seconds:
                self.max_wait_seconds = waited
            if waited >= SLOW_WAIT_SECONDS and started - self._last_wait_report_at >= 5.0:
                self._last_wait_report_at = started
                print(
                    f"[Progression] {label or 'job'} waited {waited * 1000:.0f} ms; "
                    f"backlog={self.backlog()}"
                )
            try:
                if guard is not None and not guard():
                    self.skipped += 1
                else:
                    function(*args, **kwargs)
                    self.completed += 1
            except Exception as exc:
                self.failed += 1
                print(
                    f"[Progression] {label or getattr(function, '__name__', 'job')} "
                    f"failed: {type(exc).__name__}: {exc}"
                )
                traceback.print_exc()
            finished = time.perf_counter()
            latency_stats.record("progression wait", waited)
            latency_stats.record("progression job", finished - started)
            if finished - window_started >= WINDOW_SECONDS:
                window_started, used = finished, 0.0
            used += finished - started
            if used >= BUDGET_SECONDS:
                # Leave the rest of the window to the real-time threads.
                time.sleep(max(0.0, WINDOW_SECONDS - (finished - window_started)))
                window_started, used = time.perf_counter(), 0.0
            else:
                # Release the interpreter lock between jobs.
                time.sleep(0)


_active: ProgressionQueue | None = None
_active_lock = threading.Lock()


def install(queue: ProgressionQueue | None) -> None:
    global _active
    with _active_lock:
        _active = queue


def active() -> ProgressionQueue | None:
    return _active


def defer(function, *args, label: str = "", guard=None, **kwargs) -> bool:
    """Queue ``function(*args, **kwargs)`` off the real-time loop.

    Returns False when no queue is running (tests, disabled, stopping) or it
    is full; the caller then runs the work inline as before.
    """

    queue = _active
    if not ENABLED or queue is None:
        return False
    return queue.submit(function, args, kwargs, label=label, guard=guard)


def same_map_instance(session):
    """Guard: the session is connected and still in the same map instance."""

    combat = getattr(session, "gameplay_combat_state", None)
    generation = getattr(session, "gameplay_chase_generation", None)

    def still_there() -> bool:
        return (
            bool(getattr(session, "connected", False))
            and getattr(session, "gameplay_combat_state", None) is combat
            and getattr(session, "gameplay_chase_generation", None) == generation
        )

    return still_there


def same_character(session):
    """Guard: the session is connected and plays the same character."""

    character = getattr(session, "gameplay_character", None)

    def still_playing() -> bool:
        return (
            bool(getattr(session, "connected", False))
            and character is not None
            and getattr(session, "gameplay_character", None) is character
        )

    return still_playing
