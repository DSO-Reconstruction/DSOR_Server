"""Application-wide maintenance countdown and graceful shutdown request."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

from dso_server.protocol.gameplay_protocol import encode_maintenance_countdown


DEFAULT_SHUTDOWN_MINUTES = 5
MAX_SHUTDOWN_MINUTES = 24 * 60


class MaintenanceShutdown:
    """Own one deadline across gameplay endpoints and map handoffs.

    The timer only signals the application run loop. Socket retirement and
    database draining remain on the existing application stop path. A single
    timer is independent of map workers and of the issuing admin's session.
    """

    def __init__(
        self, gameplay_servers, request_stop: Callable[[], None], *,
        clock: Callable[[], float] = time.monotonic,
        timer_factory=threading.Timer,
    ) -> None:
        self.gameplay_servers = tuple(gameplay_servers)
        self.request_stop = request_stop
        self._clock = clock
        self._timer_factory = timer_factory
        self._lock = threading.RLock()
        self._deadline: float | None = None
        self._timer = None
        self._generation = 0
        self._closed = False
        self._expired = False

    def schedule(self, minutes: int = DEFAULT_SHUTDOWN_MINUTES) -> None:
        if type(minutes) is not int or not 0 <= minutes <= MAX_SHUTDOWN_MINUTES:
            raise ValueError(
                f"shutdown minutes must be an integer from 0 to {MAX_SHUTDOWN_MINUTES}"
            )
        with self._lock:
            if self._closed or self._expired:
                raise ValueError("server shutdown is already in progress")
            if self._timer is not None:
                self._timer.cancel()
            self._generation += 1
            self._deadline = self._clock() + minutes * 60
            # Serialize announcements with rescheduling and map-ready notices
            # so an older countdown cannot overwrite a newer one on the wire.
            seen = set()
            for server in self.gameplay_servers:
                try:
                    sessions = server.session_snapshot()
                except Exception as exc:
                    print(f"[Maintenance] could not list gameplay sessions: {exc}")
                    continue
                for session in sessions:
                    if id(session) not in seen:
                        seen.add(id(session))
                        self._notify_locked(session)
            self._timer = self._timer_factory(
                max(0.0, self._deadline - self._clock()),
                self._expire,
                args=(self._generation,),
            )
            self._timer.daemon = True
            self._timer.start()
        print(f"[Maintenance] server shutdown scheduled in {minutes} minute(s)")

    def notify_session(self, session) -> None:
        """Send the remaining countdown after this map's PlayerReady bundle."""

        with self._lock:
            if self._deadline is not None and not self._closed and not self._expired:
                self._notify_locked(session)

    def _notify_locked(self, session) -> None:
        if not (
            session.connected and session.gameplay_mode
            and session.gameplay_initialization_sent
            and session.gameplay_presence_ready
            and not session.gameplay_presence_departed
            and session.gameplay_character is not None
        ):
            return
        seconds = max(0, math.ceil(self._deadline - self._clock()))
        try:
            session.send_reliable_ordered(
                encode_maintenance_countdown(seconds),
                track=True, label="maintenance-countdown",
            )
        except Exception as exc:
            # A player disconnecting during the broadcast must not prevent
            # other players receiving the timer or prevent shutdown itself.
            print(f"[Maintenance] countdown delivery failed for {session.addr}: {exc}")

    def _expire(self, generation: int) -> None:
        with self._lock:
            if self._closed or self._expired or generation != self._generation:
                return
            self._expired = True
            self._timer = None
            # Event.set only: ServerApplication.serve_forever performs stop().
            self.request_stop()

    def close(self) -> None:
        """Cancel pending work when the application stops by any route."""

        with self._lock:
            self._closed = True
            self._generation += 1
            self._deadline = None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
