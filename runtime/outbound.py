"""Bounded, paced UDP output with one FIFO per endpoint."""

from collections import deque
from dataclasses import dataclass, field
import threading
import time

from dso_server.runtime.gameplay_scheduler import GameplayDeadlineScheduler


class OutboundCapacityError(RuntimeError):
    """The peer must be retired; silently losing ordered output is unsafe."""


@dataclass
class _Mailbox:
    packets: deque = field(default_factory=deque)
    bytes: int = 0
    next_due: float = 0.0


class BoundedOutboundSender:
    """Keep socket waits and pacing off receive and simulation threads.

    Each endpoint has one scheduled drain. A bounded slice yields to other
    endpoints, including when a large response is being paced. Frame indices
    are allocated by Session before enqueueing, under its outbound lock.
    """

    def __init__(self, *, name, max_packets=32768, max_bytes=64 << 20,
                 peer_packets=4096, peer_bytes=8 << 20, worker_count=2):
        self._scheduler = GameplayDeadlineScheduler(
            name=name, worker_count=worker_count, max_pending=max_packets,
        )
        self._lock = threading.Lock()
        self._mailboxes = {}
        self._packets = self._bytes = 0
        self._closed = False
        self.max_packets, self.max_bytes = max_packets, max_bytes
        self.peer_packets, self.peer_bytes = peer_packets, peer_bytes

    def start(self):
        self._scheduler.start()

    def enqueue(self, session, data, *, pace_after=0.0):
        schedule = False
        with self._lock:
            mailbox = self._mailboxes.get(session)
            if (self._closed or self._packets >= self.max_packets
                    or self._bytes + len(data) > self.max_bytes
                    or len(data) > self.peer_bytes
                    or (mailbox is not None and (
                        len(mailbox.packets) >= self.peer_packets
                        or mailbox.bytes + len(data) > self.peer_bytes))):
                raise OutboundCapacityError("outbound queue capacity exceeded")
            if mailbox is None:
                mailbox = self._mailboxes[session] = _Mailbox()
                schedule = True
            due = max(time.perf_counter(), mailbox.next_due)
            mailbox.next_due = due + max(0.0, pace_after)
            mailbox.packets.append((due, data))
            mailbox.bytes += len(data)
            self._packets += 1
            self._bytes += len(data)
        if schedule:
            try:
                self._schedule(session, mailbox, 0.0)
            except Exception:
                self.discard(session)
                raise

    def _schedule(self, session, mailbox, delay):
        self._scheduler.timer(delay, self._drain, (session, mailbox)).start()

    def _drain(self, session, mailbox):
        for _ in range(16):
            with self._lock:
                if self._closed or self._mailboxes.get(session) is not mailbox:
                    return
                if not mailbox.packets:
                    del self._mailboxes[session]
                    return
                due, data = mailbox.packets[0]
                delay = due - time.perf_counter()
                if delay > 0:
                    break
                mailbox.packets.popleft()
                mailbox.bytes -= len(data)
                self._packets -= 1
                self._bytes -= len(data)
            if not session.retired and not session.retiring and not session.transport_failure_reason:
                try:
                    session.sock.sendto(data, session.addr)
                except OSError:
                    # Reliable datagrams remain pending and will be retried.
                    pass
                session._mark_send_attempt(data)
        else:
            delay = 0.0
        try:
            self._schedule(session, mailbox, max(0.0, delay))
        except RuntimeError:
            self.discard(session)
            if not self._closed:
                session.transport_failure_reason = "outbound scheduler stopped"

    def discard(self, session):
        with self._lock:
            mailbox = self._mailboxes.pop(session, None)
            if mailbox is not None:
                self._packets -= len(mailbox.packets)
                self._bytes -= mailbox.bytes

    def close(self):
        with self._lock:
            self._closed = True
            self._mailboxes.clear()
            self._packets = self._bytes = 0
        self._scheduler.shutdown(wait=True, cancel_pending=True)
