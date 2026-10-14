"""Bounded administrative work on the existing UDP application thread."""

from concurrent.futures import Future, TimeoutError, CancelledError
from queue import Queue, Empty, Full
import threading
from functools import wraps

from dso_server.runtime.concurrency import assert_no_state_locks_held


class AdminCommandQueue:
    def __init__(self, server):
        self.server = server
        self.pending = Queue(maxsize=64)
        self.thread_id = None
        self.closed = False

    def call(self, operation):
        assert_no_state_locks_held("submit administrative game command")
        if self.closed or self.server.sock is None:
            raise ValueError("game service is unavailable")
        if self.thread_id == threading.get_ident():
            return operation()
        future = Future()
        try:
            self.pending.put_nowait((future, operation))
        except Full:
            raise ValueError("game service is busy; try again") from None
        try:
            # Wake recvfrom without a polling timer or an additional worker.
            # This byte carries no command; only this process can enqueue work.
            host, port = self.server.sock.getsockname()[:2]
            self.server.sock.sendto(b"\x00", ("127.0.0.1" if host == "0.0.0.0" else host, port))
            try:
                return future.result(timeout=5)
            except TimeoutError:
                if future.cancel():
                    raise ValueError("game service is busy; action was not applied") from None
                # Already running: report its actual result, never a misleading
                # timeout followed by an unobserved mutation.
                return future.result()
        except CancelledError:
            raise ValueError("game service stopped; action was not applied") from None
        except BaseException:
            future.cancel()
            raise

    def drain_one(self):
        self.thread_id = threading.get_ident()
        try:
            future, operation = self.pending.get_nowait()
        except Empty:
            return
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(operation())
            except BaseException as exc:
                future.set_exception(exc)

    def close(self):
        self.closed = True
        while True:
            try:
                future, _ = self.pending.get_nowait()
            except Empty:
                return
            future.cancel()


def track_inventory_reward(function):
    """Let CMS commits yield to an existing off-thread reward computation.

    This is an activity counter, not a lock: it never delays game rewards or
    changes their execution thread. CMS refuses to publish while it is set.
    """
    @wraps(function)
    def tracked(self, s, *args, **kwargs):
        with s.flow_lock:
            s.gameplay_inventory_rewards_in_progress = getattr(s, "gameplay_inventory_rewards_in_progress", 0) + 1
        try:
            return function(self, s, *args, **kwargs)
        finally:
            with s.flow_lock:
                s.gameplay_inventory_rewards_in_progress -= 1
    return tracked
