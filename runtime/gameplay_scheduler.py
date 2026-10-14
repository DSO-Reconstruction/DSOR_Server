"""Bounded monotonic scheduler for delayed gameplay work.

``threading.Timer`` creates one operating-system thread for every delayed
callback. Projectile-heavy fights can therefore create hundreds of threads at
once. This module keeps deadlines in one heap and executes callbacks on a
small, fixed set of workers.

The returned handle implements the ``Timer`` subset used by the server:
``start()``, ``cancel()``, ``daemon``, ``is_alive()``, and ``join()``.
"""

from __future__ import annotations

import heapq
import itertools
import os
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Mapping
from typing import Any


_NEW = "new"
_SCHEDULED = "scheduled"
_RUNNING = "running"
_FINISHED = "finished"
_CANCELLED = "cancelled"


def _default_worker_count() -> int:
    raw = os.environ.get("DSO_GAMEPLAY_DEADLINE_WORKERS", "10")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "DSO_GAMEPLAY_DEADLINE_WORKERS must be an integer from 1 to 32"
        ) from exc
    if not 1 <= value <= 32:
        raise RuntimeError(
            "DSO_GAMEPLAY_DEADLINE_WORKERS must be an integer from 1 to 32"
        )
    return value


DEFAULT_GAMEPLAY_DEADLINE_WORKERS = _default_worker_count()


def _default_ingress_worker_count() -> int:
    """Return the isolated movement/skill ingress worker count.

    Deadline callbacks can legitimately wait on projectile, status-effect, or
    summon state.  Movement and casts must not share that finite worker pool:
    starving their per-session FIFO leaves combat using an old player
    position even though both UDP receive and the monster simulation remain
    healthy.
    """

    raw = os.environ.get("DSO_GAMEPLAY_INGRESS_WORKERS", "4")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "DSO_GAMEPLAY_INGRESS_WORKERS must be an integer from 1 to 32"
        ) from exc
    if not 1 <= value <= 32:
        raise RuntimeError(
            "DSO_GAMEPLAY_INGRESS_WORKERS must be an integer from 1 to 32"
        )
    return value


DEFAULT_GAMEPLAY_INGRESS_WORKERS = _default_ingress_worker_count()


def _default_initialization_worker_count() -> int:
    """Return the worker count for local-player initialization.

    Building a player's 0x85 initialization batch costs 100-600 ms with a
    warm encounter cache and more than a second when a dense map population
    has to be generated first.  It used to run inline on the UDP receive
    thread, during which no datagram from *any* client was read or
    acknowledged; every player on the service saw their ping rise by that
    amount whenever anyone entered a map.  One worker keeps initializations
    serialized exactly as before while the receive loop keeps answering.
    """

    raw = os.environ.get("DSO_GAMEPLAY_INITIALIZATION_WORKERS", "1")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "DSO_GAMEPLAY_INITIALIZATION_WORKERS must be an integer from 1 to 8"
        ) from exc
    if not 1 <= value <= 8:
        raise RuntimeError(
            "DSO_GAMEPLAY_INITIALIZATION_WORKERS must be an integer from 1 to 8"
        )
    return value


DEFAULT_GAMEPLAY_INITIALIZATION_WORKERS = _default_initialization_worker_count()


class GameplayDeadlineHandle:
    """A ``threading.Timer``-compatible handle backed by a shared scheduler."""

    __slots__ = (
        "_scheduler", "interval", "function", "args", "kwargs", "daemon",
        "finished", "_deadline", "_state", "_sequence", "absolute_deadline",
    )

    def __init__(
        self,
        scheduler: GameplayDeadlineScheduler,
        interval: float,
        function: Callable[..., Any],
        args: Iterable[Any] | None = None,
        kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        self._scheduler = scheduler
        self.interval = max(0.0, float(interval))
        self.function = function
        self.args = tuple(args or ())
        self.kwargs = dict(kwargs or {})
        self.daemon = True
        self.finished = threading.Event()
        self._deadline: float | None = None
        # Authored combat impacts can be registered before cast preparation
        # finishes. Pin their due time so start() cannot add that work again.
        # Ordinary Timer-compatible handles retain relative start semantics.
        self.absolute_deadline: float | None = None
        self._state = _NEW
        self._sequence = -1

    @property
    def deadline(self) -> float | None:
        return self._deadline

    @property
    def cancelled(self) -> bool:
        with self._scheduler._condition:
            return self._state == _CANCELLED

    def start(self) -> None:
        self._scheduler._schedule(self)

    def cancel(self) -> None:
        self._scheduler._cancel(self)

    def is_alive(self) -> bool:
        with self._scheduler._condition:
            return self._state in (_SCHEDULED, _RUNNING)

    def join(self, timeout: float | None = None) -> None:
        if self._state == _NEW:
            raise RuntimeError("cannot join timer before it is started")
        self.finished.wait(timeout)


class GameplayDeadlineScheduler:
    """Run many monotonic deadlines using a bounded number of workers."""

    def __init__(
        self,
        *,
        name: str = "gameplay-deadline",
        worker_count: int = 2,
        max_pending: int = 32768,
    ) -> None:
        if int(worker_count) < 1:
            raise ValueError("worker_count must be at least one")
        if int(max_pending) < 1:
            raise ValueError("max_pending must be at least one")
        self.name = str(name)
        self.worker_count = int(worker_count)
        self.max_pending = int(max_pending)
        self._condition = threading.Condition()
        self._heap: list[
            tuple[float, int, GameplayDeadlineHandle]
        ] = []
        self._sequence = itertools.count()
        self._pending_count = 0
        self._cancelled_heap_entries = 0
        self._workers: list[threading.Thread] = []
        self._generation = 0
        self._shutdown = False
        self._ever_started = False

    @property
    def pending_count(self) -> int:
        with self._condition:
            return self._pending_count

    @property
    def threads(self) -> tuple[threading.Thread, ...]:
        with self._condition:
            return tuple(self._workers)

    def timer(
        self,
        interval: float,
        function: Callable[..., Any],
        args: Iterable[Any] | None = None,
        kwargs: Mapping[str, Any] | None = None,
    ) -> GameplayDeadlineHandle:
        return GameplayDeadlineHandle(
            self,
            interval,
            function,
            args=args,
            kwargs=kwargs,
        )

    def start(self) -> None:
        """Start the fixed worker set; safe to call more than once."""

        with self._condition:
            alive_workers = [
                worker for worker in self._workers if worker.is_alive()
            ]
            # A normal second ``start`` is a no-op. After ``shutdown``
            # timed out, however, one old-generation worker may still be
            # inside a user callback. It cannot be killed safely and it must
            # not prevent a fresh generation from starting. The generation
            # check makes that worker retire as soon as its callback returns.
            if alive_workers and not self._shutdown:
                return
            if self._pending_count or self._heap:
                raise RuntimeError(
                    "cannot restart scheduler with pending deadlines"
                )
            self._shutdown = False
            self._ever_started = True
            self._generation += 1
            generation = self._generation
            new_workers = [
                threading.Thread(
                    target=self._run,
                    args=(generation,),
                    daemon=True,
                    name=f"{self.name}-{ordinal + 1}",
                )
                for ordinal in range(self.worker_count)
            ]
            # Retain an outliving callback in the lifecycle list so a later
            # shutdown still joins it. This temporary surplus is bounded by
            # the previous worker generation and vanishes when callbacks
            # return; newly scheduled work runs only on ``new_workers``.
            self._workers = alive_workers + new_workers
            for worker in new_workers:
                worker.start()

    def shutdown(
        self,
        *,
        wait: bool = True,
        cancel_pending: bool = True,
        timeout: float | None = 1.0,
    ) -> None:
        """Stop accepting work and optionally cancel queued callbacks."""

        with self._condition:
            self._shutdown = True
            if cancel_pending:
                for _deadline, _sequence, handle in self._heap:
                    if handle._state != _SCHEDULED:
                        continue
                    handle._state = _CANCELLED
                    handle.finished.set()
                self._heap.clear()
                self._pending_count = 0
                self._cancelled_heap_entries = 0
            workers = tuple(self._workers)
            self._condition.notify_all()
        if wait:
            deadline = (
                None if timeout is None
                else time.perf_counter() + max(0.0, float(timeout))
            )
            for worker in workers:
                if worker is threading.current_thread():
                    continue
                remaining = (
                    None if deadline is None
                    else max(0.0, deadline - time.perf_counter())
                )
                worker.join(remaining)
        with self._condition:
            self._workers = [
                worker for worker in self._workers if worker.is_alive()
            ]

    def _ensure_started(self) -> None:
        with self._condition:
            alive = any(worker.is_alive() for worker in self._workers)
            if alive:
                return
            if self._shutdown and self._ever_started:
                raise RuntimeError("gameplay deadline scheduler is stopped")
        self.start()

    def _schedule(self, handle: GameplayDeadlineHandle) -> None:
        self._ensure_started()
        with self._condition:
            if self._shutdown:
                raise RuntimeError("gameplay deadline scheduler is stopped")
            # ``threading.Timer.cancel()`` is legal before ``start()``. Its
            # eventual start creates no callback; preserve that race-safe
            # behavior for session cleanup between registration and start.
            if handle._state == _CANCELLED and handle._sequence == -1:
                handle._sequence = -2
                return
            if handle._state != _NEW:
                raise RuntimeError("timers can only be started once")
            if self._pending_count >= self.max_pending:
                raise RuntimeError(
                    "gameplay deadline scheduler capacity exceeded "
                    f"({self.max_pending})"
                )
            sequence = next(self._sequence)
            deadline = (
                time.perf_counter() + handle.interval
                if handle.absolute_deadline is None
                else float(handle.absolute_deadline)
            )
            handle._sequence = sequence
            handle._deadline = deadline
            handle._state = _SCHEDULED
            self._pending_count += 1
            heapq.heappush(self._heap, (deadline, sequence, handle))
            self._condition.notify_all()

    def _cancel(self, handle: GameplayDeadlineHandle) -> None:
        with self._condition:
            if handle._state == _NEW:
                handle._state = _CANCELLED
                handle.finished.set()
                return
            if handle._state != _SCHEDULED:
                return
            handle._state = _CANCELLED
            handle.finished.set()
            self._pending_count -= 1
            self._cancelled_heap_entries += 1
            if (
                self._cancelled_heap_entries >= 256
                and self._cancelled_heap_entries * 2 >= len(self._heap)
            ):
                self._compact_heap_locked()
            self._condition.notify_all()

    def _compact_heap_locked(self) -> None:
        self._heap = [
            item for item in self._heap if item[2]._state == _SCHEDULED
        ]
        heapq.heapify(self._heap)
        self._cancelled_heap_entries = 0

    def _discard_cancelled_head_locked(self) -> None:
        while self._heap and self._heap[0][2]._state != _SCHEDULED:
            heapq.heappop(self._heap)
            if self._cancelled_heap_entries:
                self._cancelled_heap_entries -= 1

    def _run(self, generation: int) -> None:
        while True:
            handle: GameplayDeadlineHandle | None = None
            with self._condition:
                while handle is None:
                    if generation != self._generation:
                        return
                    self._discard_cancelled_head_locked()
                    if self._shutdown and not self._heap:
                        return
                    if not self._heap:
                        self._condition.wait()
                        continue
                    deadline, _sequence, candidate = self._heap[0]
                    remaining = deadline - time.perf_counter()
                    if remaining > 0.0:
                        self._condition.wait(remaining)
                        continue
                    heapq.heappop(self._heap)
                    if candidate._state != _SCHEDULED:
                        continue
                    candidate._state = _RUNNING
                    self._pending_count -= 1
                    handle = candidate
                    # A timed Condition wait may wake only one worker at the
                    # deadline on Windows. Wake peers before this callback
                    # runs so an equal-deadline burst is claimed in parallel
                    # instead of waiting for the first callback to finish.
                    self._discard_cancelled_head_locked()
                    if (
                        self._heap
                        and self._heap[0][0] <= time.perf_counter()
                    ):
                        self._condition.notify_all()
            try:
                handle.function(*handle.args, **handle.kwargs)
            except Exception as exc:
                print(
                    f"[{self.name}] scheduled callback "
                    f"{getattr(handle.function, '__qualname__', repr(handle.function))} "
                    f"failed: {type(exc).__name__}: {exc}"
                )
                traceback.print_exc()
            finally:
                with self._condition:
                    if handle._state == _RUNNING:
                        handle._state = _FINISHED
                    handle.finished.set()
                    self._condition.notify_all()
