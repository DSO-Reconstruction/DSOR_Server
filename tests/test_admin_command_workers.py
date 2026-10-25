"""Admin mutations share player gates without occupying the UDP receiver."""

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
import threading
from types import SimpleNamespace

import pytest

from dso_server.runtime.admin_commands import AdminCommandQueue, call_admin_command


class ObservedGate:
    def __init__(self):
        self.lock = threading.RLock()
        self.waiting = threading.Event()

    def acquire(self, **kwargs):
        self.waiting.set()
        return self.lock.acquire(**kwargs)

    def release(self):
        self.lock.release()


@pytest.fixture
def controls():
    queue = AdminCommandQueue(SimpleNamespace(sock=object(), service_name='admin-worker-test'))
    try:
        yield queue
    finally:
        queue.close()


def test_session_mutation_waits_for_gameplay_without_running_on_drain_thread(controls):
    gate = ObservedGate()
    session = SimpleNamespace(gameplay_mutation_lock=gate, balance=10)
    with ThreadPoolExecutor(max_workers=1) as callers:
        with gate.lock:
            result = callers.submit(controls.call, lambda: (session.balance, threading.get_ident()), session=session)
            assert gate.waiting.wait(1)
            controls.drain_one()
            assert not result.done()
            # A trade/gameplay command publishes before the admin operation
            # acquires the same mutation gate.
            session.balance = 20
        value, thread_id = result.result(timeout=1)
    assert value == 20
    assert thread_id == controls.thread_id
    assert thread_id != threading.get_ident()


def test_timeout_while_waiting_for_mutation_gate_cancels_without_late_write(controls):
    controls._wait_timeout = 0.02
    gate = ObservedGate()
    session = SimpleNamespace(gameplay_mutation_lock=gate)
    applied = []
    with ThreadPoolExecutor(max_workers=1) as callers:
        with gate.lock:
            result = callers.submit(controls.call, lambda: applied.append(True), session=session)
            assert gate.waiting.wait(1)
            with pytest.raises(ValueError, match='action was not applied'):
                result.result(timeout=1)
        controls._wait_timeout = 1
        assert controls.call(lambda: 'after cancelled command') == 'after cancelled command'
    assert applied == []


def test_running_mutation_reports_actual_result_after_initial_wait_timeout(controls):
    controls._wait_timeout = 0.01
    entered, release = threading.Event(), threading.Event()

    def operation():
        entered.set()
        assert release.wait(2)
        return 'applied'

    with ThreadPoolExecutor(max_workers=1) as callers:
        result = callers.submit(controls.call, operation)
        try:
            assert entered.wait(1)
            with pytest.raises(TimeoutError):
                result.result(timeout=0.05)
        finally:
            release.set()
        assert result.result(timeout=1) == 'applied'


def test_worker_has_bounded_pending_capacity(controls):
    entered, release = threading.Event(), threading.Event()

    def operation():
        entered.set()
        assert release.wait(2)

    with ThreadPoolExecutor(max_workers=1) as callers:
        running = callers.submit(controls.call, operation)
        try:
            assert entered.wait(1)
            for _ in range(controls.pending.maxsize):
                controls.pending.put_nowait((Future(), lambda: None, None))
            with pytest.raises(ValueError, match='busy; try again'):
                controls.call(lambda: None)
        finally:
            release.set()
        running.result(timeout=1)


def test_close_cancels_work_waiting_for_session_gate(controls):
    gate = ObservedGate()
    session = SimpleNamespace(gameplay_mutation_lock=gate)
    applied = []
    with ThreadPoolExecutor(max_workers=1) as callers:
        with gate.lock:
            result = callers.submit(controls.call, lambda: applied.append(True), session=session)
            assert gate.waiting.wait(1)
            controls.close()
            with pytest.raises(ValueError, match='stopped; action was not applied'):
                result.result(timeout=1)
        assert not controls._thread.is_alive()
    assert applied == []


def test_invalid_session_does_not_retire_worker(controls):
    with pytest.raises(AttributeError):
        controls.call(lambda: None, session=object())
    assert controls.call(lambda: 42) == 42


def test_single_argument_queue_adapter_still_uses_session_gate():
    session = SimpleNamespace(gameplay_mutation_lock=threading.RLock())
    adapter = SimpleNamespace(call=lambda operation: operation())
    assert call_admin_command(
        adapter, session.gameplay_mutation_lock._is_owned, session=session,
    )
