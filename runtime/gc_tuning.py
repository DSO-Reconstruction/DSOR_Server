"""Keep CPython's cyclic garbage collector off the gameplay hot path.

An automatic full collection walks every tracked container object with the
GIL held.  Nothing else in the process runs while it does: not the UDP receive
thread, not a map's simulation worker, not the deadline workers delivering
hits.  With four dense map populations cached that walk measured 70-100 ms,
and under multiplayer load CPython triggered it every ten to twenty seconds.
It was the largest in-process stall the packed-map benchmark recorded, and it
hits every connected player at the same instant (PERFORMANCE.md section 13).

The policy here is the one long-running CPython services converge on:

- everything alive after startup (static content caches, codecs, the service
  composition) is frozen into the permanent generation so no collection ever
  walks it again;
- the young generations keep their cheap default cadence, so short-lived
  per-tick garbage is still reclaimed promptly;
- automatic *full* collections are disabled, and a maintenance thread runs
  one on a fixed interval instead, preferring a moment when no map is
  simulating and never deferring past a hard bound so cyclic garbage cannot
  accumulate indefinitely;
- a generated map population is frozen as soon as it is cached (see
  ``freeze_long_lived_addition``), so the ordinary managed collection walks
  only session-lifetime state.  Every few cycles the permanent generation is
  unfrozen for one complete walk, which is what reclaims garbage cycles among
  long-lived objects and re-freezes the survivors.

``DSO_GC_POLICY=default`` leaves the interpreter's behaviour untouched.
"""

from __future__ import annotations

import gc
import os
import threading
import time


def _float_setting(name: str, default: str, minimum: float) -> float:
    raw = os.environ.get(name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if value != value or value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


GC_POLICY = os.environ.get("DSO_GC_POLICY", "managed").strip().lower()
if GC_POLICY not in {"managed", "default"}:
    raise RuntimeError("DSO_GC_POLICY must be 'managed' or 'default'")
# How often the maintenance thread wants to run a full collection.
FULL_COLLECTION_INTERVAL_SECONDS = _float_setting(
    "DSO_GC_FULL_COLLECTION_SECONDS", "300", 10.0
)
# How long a due collection may wait for the maps to go quiet before it runs
# anyway.  Zero collects on schedule regardless of load.
MAX_DEFERRAL_SECONDS = _float_setting("DSO_GC_MAX_DEFERRAL_SECONDS", "900", 0.0)
# CPython's defaults for the two young generations; kept because they are
# cheap and reclaim per-tick garbage promptly.  Only the full collection moves
# to the maintenance thread.
YOUNG_THRESHOLDS = (700, 10)
# Generation-2 collections are counted in gen-1 collections.  This value can
# never be reached in practice, which is the point: full collections happen
# when the maintenance thread asks for one, not when an allocation pattern
# trips a counter in the middle of a fight.
NEVER_AUTOMATIC_FULL_COLLECTION = 1_000_000_000
# Every this many managed cycles the frozen heap is unfrozen and walked too.
UNFREEZE_EVERY_CYCLES = max(
    1, int(_float_setting("DSO_GC_UNFREEZE_EVERY_CYCLES", "6", 1.0))
)

_freeze_additions = False


def freeze_long_lived_addition() -> None:
    """Move a freshly built long-lived structure out of the collector's way.

    Called by the monster-encounter loader once a generated population has
    been cached.  Populations live for the rest of the process, so walking
    their 60-70 thousand objects on every collection buys nothing.  A no-op
    unless the managed policy is installed.  Anything else alive at that
    instant is frozen too; the periodic unfreezing walk reclaims any cycles
    that were caught up in it.
    """

    if _freeze_additions:
        gc.freeze()


class ManagedGarbageCollector:
    """Freeze the startup heap and schedule full collections deliberately."""

    def __init__(
        self,
        *,
        busy_check=None,
        interval_seconds: float = FULL_COLLECTION_INTERVAL_SECONDS,
        max_deferral_seconds: float = MAX_DEFERRAL_SECONDS,
        policy: str = GC_POLICY,
        unfreeze_every_cycles: int = UNFREEZE_EVERY_CYCLES,
        report=print,
        clock=time.perf_counter,
        name: str = "gc-maintenance",
    ) -> None:
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.max_deferral_seconds = max(0.0, float(max_deferral_seconds))
        self.unfreeze_every_cycles = max(1, int(unfreeze_every_cycles))
        self._cycles_since_full = 0
        self.policy = str(policy)
        self._busy_check = busy_check or (lambda: False)
        self._report = report
        self._clock = clock
        self.name = str(name)
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._previous_thresholds: tuple[int, int, int] | None = None
        self.installed = False
        self._deferred_since: float | None = None
        self.collections = 0
        self.full_collections = 0
        self.deferrals = 0
        self.last_collection_seconds = 0.0
        self.worst_collection_seconds = 0.0
        self.frozen_objects = 0

    @property
    def enabled(self) -> bool:
        return self.policy == "managed"

    def install(self) -> None:
        """Freeze what exists now, stop automatic full collections, start."""

        if not self.enabled or self.installed:
            return
        self._previous_thresholds = gc.get_threshold()
        # One last automatic-style full pass before freezing, so the permanent
        # generation does not enshrine startup garbage.
        gc.collect()
        gc.freeze()
        self.frozen_objects = gc.get_freeze_count()
        gc.set_threshold(*YOUNG_THRESHOLDS, NEVER_AUTOMATIC_FULL_COLLECTION)
        global _freeze_additions
        _freeze_additions = True
        self.installed = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=self.name
        )
        self._thread.start()
        self._emit(
            f"[GC] managed policy: {self.frozen_objects} startup objects "
            f"frozen; collections every {self.interval_seconds:.0f}s when "
            f"maps are quiet (at most {self.max_deferral_seconds:.0f}s "
            f"later), whole-heap walk every {self.unfreeze_every_cycles} "
            "cycles, automatic full collections off"
        )

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._thread = None
        if self.installed:
            global _freeze_additions
            _freeze_additions = False
            if self._previous_thresholds is not None:
                gc.set_threshold(*self._previous_thresholds)
            self.installed = False

    def request_collection(self) -> None:
        """Ask the maintenance thread to run its next cycle now."""

        self._wake_event.set()

    def collect_now(
        self, *, reason: str = "requested", full: bool = False
    ) -> float:
        """Run one managed collection and return its wall-clock duration.

        The ordinary collection leaves the permanent generation alone, so it
        walks only what was created since startup and not frozen since: in
        practice session state.  ``full`` unfreezes first so the walk covers
        the whole heap and reclaims garbage cycles among long-lived objects,
        then freezes the survivors so the next ordinary collection is cheap
        again.
        """

        started = self._clock()
        if full:
            gc.unfreeze()
        try:
            gc.collect()
        finally:
            if full:
                gc.freeze()
        duration = self._clock() - started
        with self._lock:
            self.collections += 1
            if full:
                self.full_collections += 1
            self.last_collection_seconds = duration
            self.worst_collection_seconds = max(
                self.worst_collection_seconds, duration
            )
            self.frozen_objects = gc.get_freeze_count()
        if duration >= 0.050:
            self._emit(
                f"[GC] {'full' if full else 'managed'} collection ({reason}) "
                f"took {duration * 1000:.0f}ms; {self.frozen_objects} "
                "objects frozen"
            )
        return duration

    def statistics(self) -> dict[str, float | int]:
        with self._lock:
            return {
                "collections": self.collections,
                "full_collections": self.full_collections,
                "deferrals": self.deferrals,
                "last_collection_seconds": self.last_collection_seconds,
                "worst_collection_seconds": self.worst_collection_seconds,
                "frozen_objects": self.frozen_objects,
            }

    def _emit(self, message: str) -> None:
        if self._report is not None:
            self._report(message)

    def _run(self) -> None:
        due_at = self._clock() + self.interval_seconds
        while not self._stop_event.is_set():
            now = self._clock()
            remaining = due_at - now
            if remaining > 0.0:
                # Wake early on request; otherwise re-check at most once a
                # second so a stop is honoured promptly.
                if self._wake_event.wait(min(1.0, remaining)):
                    self._wake_event.clear()
                    if self._stop_event.is_set():
                        return
                    due_at = self._clock()
                continue
            due_at = self._cycle(now)

    def _cycle(self, now: float) -> float:
        """Run one due cycle; return when the next check is due.

        Separated from the thread loop so the deferral policy can be driven
        deterministically: a busy map postpones the collection in short steps
        until ``max_deferral_seconds`` has passed, after which it runs anyway.
        """

        busy = False
        try:
            busy = bool(self._busy_check())
        except Exception:
            busy = False
        if busy and self.max_deferral_seconds > 0.0:
            if self._deferred_since is None:
                self._deferred_since = now
            if now - self._deferred_since < self.max_deferral_seconds:
                with self._lock:
                    self.deferrals += 1
                # Maps are simulating: look again shortly rather than
                # pausing every player mid-fight.
                return now + min(5.0, self.interval_seconds)
        self._cycles_since_full += 1
        full = self._cycles_since_full >= self.unfreeze_every_cycles
        try:
            self.collect_now(
                reason="deferred maximum reached" if busy else "maps quiet",
                full=full,
            )
        except Exception as exc:
            self._emit(
                f"[GC] collection failed: {type(exc).__name__}: {exc}"
            )
        if full:
            self._cycles_since_full = 0
        self._deferred_since = None
        return self._clock() + self.interval_seconds


__all__ = [
    "FULL_COLLECTION_INTERVAL_SECONDS",
    "GC_POLICY",
    "MAX_DEFERRAL_SECONDS",
    "ManagedGarbageCollector",
    "NEVER_AUTOMATIC_FULL_COLLECTION",
    "UNFREEZE_EVERY_CYCLES",
    "YOUNG_THRESHOLDS",
    "freeze_long_lived_addition",
]
