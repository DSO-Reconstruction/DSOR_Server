"""Bounded administrative work, serialized with targeted gameplay mutations."""

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
        self._lifecycle_lock = threading.Lock()
        self._thread = None
        self._active_future = None
        self._wait_timeout = 5.0

    def call(self, operation, *, session=None):
        assert_no_state_locks_held("submit administrative game command")
        if self.closed or self.server.sock is None:
            raise ValueError("game service is unavailable")
        if self.thread_id == threading.get_ident():
            if session is None:
                return operation()
            with session.gameplay_mutation_lock:
                return operation()
        future = Future()
        with self._lifecycle_lock:
            if self.closed:
                raise ValueError("game service is unavailable")
            try:
                self.pending.put_nowait((future, operation, session))
            except Full:
                raise ValueError("game service is busy; try again") from None
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, daemon=True,
                    name=f"{getattr(self.server, 'service_name', 'game')}-admin",
                )
                self._thread.start()
        try:
            try:
                return future.result(timeout=self._wait_timeout)
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
        """Compatibility hook for receive loops; admin work never runs here."""

    def _run(self):
        self.thread_id = threading.get_ident()
        while True:
            entry = self.pending.get()
            if entry is None:
                self.pending.task_done()
                return
            future, operation, session = entry
            with self._lifecycle_lock:
                self._active_future = future
                if self.closed:
                    future.cancel()
            try:
                self._execute(future, operation, session)
            except BaseException as exc:
                # A bad session/gate must not retire the only admin worker.
                if not future.done() and (
                    future.running() or future.set_running_or_notify_cancel()
                ):
                    future.set_exception(exc)
            finally:
                with self._lifecycle_lock:
                    self._active_future = None
                self.pending.task_done()

    def _execute(self, future, operation, session):
        gate = session.gameplay_mutation_lock if session is not None else None
        acquired = False
        try:
            if gate is not None:
                # Waiting for gameplay is still pending work. A timeout or
                # shutdown can cancel it without a delayed unseen mutation.
                while not future.cancelled():
                    if gate.acquire(timeout=0.05):
                        acquired = True
                        break
                if not acquired:
                    return
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(operation())
                except BaseException as exc:
                    future.set_exception(exc)
        finally:
            if acquired:
                gate.release()

    def close(self):
        with self._lifecycle_lock:
            if self.closed:
                return
            self.closed = True
            if self._active_future is not None:
                self._active_future.cancel()
            while True:
                try:
                    future, _operation, _session = self.pending.get_nowait()
                except Empty:
                    break
                future.cancel()
                self.pending.task_done()
            thread = self._thread
            if thread is not None:
                self.pending.put_nowait(None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)


def call_admin_command(queue, operation, *, session=None):
    """Submit targeted work while retaining simple synchronous queue adapters."""
    if isinstance(queue, AdminCommandQueue):
        return queue.call(operation, session=session)

    def execute():
        if session is None:
            return operation()
        with session.gameplay_mutation_lock:
            return operation()

    assert_no_state_locks_held("submit administrative game command")
    return queue.call(execute)


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
