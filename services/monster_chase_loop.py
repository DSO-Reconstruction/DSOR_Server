"""Lifetime, recovery and progress reporting for each map's simulation thread."""
from __future__ import annotations

import os
import threading
import time

from dso_server.runtime.context import GAMEPLAY_SERVICE_NAME, MONSTER_CHASE_INTERVAL_SECONDS
from dso_server.runtime.transport import Session
from dso_server.runtime.monster_runtime_diagnostics import register_worker, record_worker_exit
from dso_server.services.base import CharacterService


# An emptied visible set is an idle map, not necessarily a retired one.
# Keep the worker at a reduced cadence through brief gaps between packs;
# map ownership and sustained idleness determine when it should retire.
MONSTER_TICK_IDLE = "idle"
MONSTER_CHASE_IDLE_INTERVAL_SECONDS = max(
    MONSTER_CHASE_INTERVAL_SECONDS,
    float(os.environ.get("DSO_MONSTER_CHASE_IDLE_INTERVAL_SECONDS", "0.5")),
)
# Retire only after a sustained quiet period. Zero disables retirement and
# keeps the worker for the lifetime of the session's map ownership.
MONSTER_CHASE_IDLE_RETIRE_SECONDS = max(
    0.0,
    float(os.environ.get("DSO_MONSTER_CHASE_IDLE_RETIRE_SECONDS", "30.0")),
)
# How long a map instance with simulation work may have no worker before the
# watchdog's supervisor starts one. An ordinary hand-off completes within a
# tick, so this only ever catches a map that genuinely lost its owner.
MONSTER_WORLD_ORPHAN_GRACE_SECONDS = max(
    0.0,
    float(os.environ.get("DSO_MONSTER_ORPHAN_GRACE_SECONDS", "1.0")),
)


class _FixedRateChaseLoop:
    """One reusable worker per authoritative map simulation."""

    def __init__(self, session: Session, generation: int):
        self.session = session
        self.generation = int(generation)
        self._stopped = threading.Event()
        self.diagnostic_phase = "reserved"
        self.diagnostic_phase_at = time.perf_counter()
        self.diagnostic_progress_at = self.diagnostic_phase_at
        self.diagnostic_ticks_completed = 0
        self.diagnostic_last_result = None
        # Set under the session's flow_lock by a start request that found this
        # worker still installed; see ``_release_unless_restarted``.
        self._restart_requested = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"dso-monsters-{session.addr[1]}-{generation}",
            daemon=True,
        )

    def start(self) -> None:
        register_worker(self)
        self._thread.start()

    def _set_phase(self, phase: str) -> None:
        self.diagnostic_phase = phase
        self.diagnostic_phase_at = time.perf_counter()

    def has_exited(self) -> bool:
        # A worker is installed under flow_lock before its thread starts.
        # is_alive() alone would let a concurrent starter replace that slot.
        return self._thread.ident is not None and not self._thread.is_alive()

    def cancel(self) -> None:
        self._stopped.set()

    def request_restart(self) -> None:
        """Record a start request that found this worker installed.

        The caller holds ``session.flow_lock``. ``_start_monster_chase`` cannot
        replace a live worker, so a request that arrives while this worker is
        deciding to retire -- idle timeout, or an ownership hand-off -- used to
        be dropped, and the map was left with no simulation thread at all.
        """

        self._restart_requested = True

    def _release_unless_restarted(self) -> bool:
        """Release the session's worker slot unless a start arrived meanwhile.

        Returns whether this worker should exit. Deciding under flow_lock is
        what makes the hand-off safe: a start request is recorded under the
        same lock, so it either lands before this decision and is honoured, or
        after the slot is released and starts a replacement worker.
        """

        try:
            with self.session.flow_lock:
                if self.session.gameplay_chase_timer is not self:
                    return True
                if self._restart_requested and not self._stopped.is_set():
                    self._restart_requested = False
                    return False
                self.session.gameplay_chase_timer = None
                return True
        except TimeoutError:
            # The exit path retries the release; failing that, the installed
            # handle of an exited thread is replaceable (``has_exited``).
            return True

    def _is_installed_worker(self) -> bool:
        """Report whether this thread still owns the session's simulation.

        ``_start_monster_chase`` no longer bumps the chase generation, so the
        generation alone can no longer distinguish a retired worker from its
        replacement. Object identity is the authoritative test, and the cancel
        sites that clear ``gameplay_chase_timer`` are exactly the ones that
        must stop this thread.
        """

        with self.session.flow_lock:
            return self.session.gameplay_chase_timer is self

    def _run(self) -> None:
        deadline = time.perf_counter() + MONSTER_CHASE_INTERVAL_SECONDS
        consecutive_failures = 0
        idle_since = None
        try:
            while not self._stopped.wait(
                max(0.0, deadline - time.perf_counter())
            ):
                try:
                    self._set_phase("ownership")
                    if not self._is_installed_worker():
                        break
                    # A request made before this tick is answered by the tick
                    # itself; only one that lands while it runs can contradict
                    # the retirement decision it returns.
                    self._restart_requested = False
                    self._set_phase("tick")
                    keep_running = CharacterService._monster_chase_tick(
                        self.session, self.generation
                    )
                except Exception as exc:
                    consecutive_failures += 1
                    self.diagnostic_last_result = type(exc).__name__
                    self._set_phase("error-report")
                    if (
                        consecutive_failures <= 3
                        or consecutive_failures % 30 == 0
                    ):
                        print(
                            f"[{GAMEPLAY_SERVICE_NAME}] monster loop tick "
                            f"failed ({consecutive_failures} consecutive): "
                            f"{type(exc).__name__}: {exc}"
                        )
                    # One malformed monster/packet must not permanently kill
                    # the map's only simulation worker. Back off before
                    # retrying so a persistent content error cannot spin.
                    self._set_phase("retry-wait")
                    deadline = time.perf_counter() + min(
                        1.0,
                        MONSTER_CHASE_INTERVAL_SECONDS
                        * (1 + consecutive_failures),
                    )
                    continue
                consecutive_failures = 0
                now = time.perf_counter()
                self.diagnostic_progress_at = now
                self.diagnostic_ticks_completed += 1
                self.diagnostic_last_result = keep_running
                self._set_phase("interval-wait")
                if keep_running is MONSTER_TICK_IDLE:
                    # Nothing to advance right now. Stay alive at a reduced
                    # cadence so the next spawn, summon or aggro is simulated
                    # immediately instead of waiting for an ActorRequest.
                    if idle_since is None:
                        idle_since = now
                    elif (
                        MONSTER_CHASE_IDLE_RETIRE_SECONDS
                        and now - idle_since
                        >= MONSTER_CHASE_IDLE_RETIRE_SECONDS
                    ):
                        if self._release_unless_restarted():
                            break
                        idle_since = None
                    interval = MONSTER_CHASE_IDLE_INTERVAL_SECONDS
                elif not keep_running:
                    if self._release_unless_restarted():
                        break
                    interval = MONSTER_CHASE_INTERVAL_SECONDS
                else:
                    idle_since = None
                    interval = MONSTER_CHASE_INTERVAL_SECONDS
                deadline += interval
                if deadline < now - interval:
                    # Do not execute a burst of overdue simulation ticks.
                    deadline = now + interval
        finally:
            self._set_phase("retiring")
            try:
                with self.session.flow_lock:
                    if self.session.gameplay_chase_timer is self:
                        self.session.gameplay_chase_timer = None
            except TimeoutError:
                # Leave the identity intact for diagnostics and safe recovery
                # by _start_monster_chase once this thread has actually exited.
                self.diagnostic_last_result = "retirement-lock-timeout"
            self._set_phase("exited")
            record_worker_exit(self)


