"""Simulated packet loss and latency for netcode tests. Off by default.

The audit's exit tests ask how play holds up at 1% loss and a realistic round
trip, which a localhost client never sees. With any of these set, every
service socket is wrapped right after it binds:

``DSO_NET_LOSS_PERCENT``
    Share of datagrams dropped in each direction (0-100), as if lost on the
    wire. Dropped sends still report success, as UDP does.
``DSO_NET_DELAY_MS``
    Added to every datagram the server sends. The client's ACKs and moves
    arrive undelayed, so this is the whole extra round trip.
``DSO_NET_JITTER_MS``
    Uniform +/- spread on that delay. Datagrams keep their order, as on one
    queueing path: a datagram is never delivered before the one sent ahead
    of it. Independent jitter reordered datagrams sent 25 ms apart, and the
    client NAKed every such gap at once, so a test run measured needless
    repairs instead of losses.

Everything else (timeouts, buffers, close) passes through to the socket.
"""

from __future__ import annotations

import heapq
import itertools
import os
import random
import threading
import time


def _setting(name: str) -> float:
    try:
        return max(0.0, float(os.getenv(name, "0") or 0))
    except ValueError:
        return 0.0


LOSS_PERCENT = min(100.0, _setting("DSO_NET_LOSS_PERCENT"))
DELAY_MS = _setting("DSO_NET_DELAY_MS")
JITTER_MS = _setting("DSO_NET_JITTER_MS")


class ImpairedSocket:
    """A UDP socket that loses and delays datagrams on purpose."""

    def __init__(self, sock, *, loss_percent: float, delay_ms: float,
                 jitter_ms: float, name: str = "", rng=None) -> None:
        self._sock = sock
        self.loss = max(0.0, min(100.0, float(loss_percent))) / 100.0
        self.delay = max(0.0, float(delay_ms)) / 1000.0
        self.jitter = max(0.0, float(jitter_ms)) / 1000.0
        self.name = name
        self._rng = rng or random.Random()
        self._queue: list = []
        self._order = itertools.count()
        self._last_due = 0.0
        self._wake = threading.Condition()
        self._closed = False
        self._thread = None
        self.dropped_out = 0
        self.dropped_in = 0
        self.sent = 0
        self.received = 0

    def __getattr__(self, name):
        return getattr(self._sock, name)

    def sendto(self, data, address):
        self.sent += 1
        if self._rng.random() < self.loss:
            self.dropped_out += 1
            return len(data)
        if self.delay <= 0 and self.jitter <= 0:
            return self._sock.sendto(data, address)
        due = time.perf_counter() + max(
            0.0, self.delay + self._rng.uniform(-self.jitter, self.jitter)
        )
        with self._wake:
            if self._closed:
                return len(data)
            due = max(due, self._last_due)
            self._last_due = due
            heapq.heappush(self._queue, (due, next(self._order), bytes(data), address))
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._deliver, name=f"{self.name}-net-delay", daemon=True,
                )
                self._thread.start()
            self._wake.notify()
        return len(data)

    def recvfrom(self, size):
        while True:
            data, address = self._sock.recvfrom(size)
            self.received += 1
            if self._rng.random() >= self.loss:
                return data, address
            self.dropped_in += 1

    def close(self):
        with self._wake:
            self._closed = True
            self._queue.clear()
            self._wake.notify()
        print(
            f"[NetworkImpairment] {self.name}: dropped {self.dropped_out} of "
            f"{self.sent} sent and {self.dropped_in} of {self.received} received"
        )
        self._sock.close()

    def _deliver(self) -> None:
        while True:
            with self._wake:
                while not self._closed and (
                    not self._queue or self._queue[0][0] > time.perf_counter()
                ):
                    timeout = (
                        self._queue[0][0] - time.perf_counter() if self._queue else None
                    )
                    self._wake.wait(timeout)
                if self._closed:
                    return
                _due, _order, data, address = heapq.heappop(self._queue)
            try:
                self._sock.sendto(data, address)
            except OSError:
                # The datagram is lost, which is what this socket simulates.
                pass


def impair_socket(sock, name: str = ""):
    """Wrap ``sock`` when the environment asks for loss or delay."""

    if not (LOSS_PERCENT or DELAY_MS or JITTER_MS):
        return sock
    print(
        f"[NetworkImpairment] {name}: simulating {LOSS_PERCENT:g}% loss each way, "
        f"+{DELAY_MS:g} ms +/- {JITTER_MS:g} ms on sends"
    )
    return ImpairedSocket(
        sock, loss_percent=LOSS_PERCENT, delay_ms=DELAY_MS,
        jitter_ms=JITTER_MS, name=name,
    )
