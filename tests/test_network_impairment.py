"""Simulated loss and latency for netcode tests (runtime/network_impairment.py)."""

import random
import socket
import time
import unittest
from unittest.mock import patch

from dso_server.runtime import network_impairment
from dso_server.runtime.network_impairment import ImpairedSocket, impair_socket


class RecordingSocket:
    def __init__(self, incoming=()):
        self.sent = []
        self.incoming = list(incoming)
        self.timeout = None
        self.closed = False

    def sendto(self, data, address):
        self.sent.append((time.perf_counter(), bytes(data), address))
        return len(data)

    def recvfrom(self, size):
        if not self.incoming:
            raise socket.timeout()
        return self.incoming.pop(0), ("127.0.0.1", 1)

    def settimeout(self, value):
        self.timeout = value

    def close(self):
        self.closed = True


class NetworkImpairmentTests(unittest.TestCase):
    def test_off_by_default_returns_the_socket_itself(self):
        raw = RecordingSocket()
        with patch.multiple(network_impairment, LOSS_PERCENT=0.0, DELAY_MS=0.0, JITTER_MS=0.0):
            self.assertIs(impair_socket(raw, "test"), raw)

    def test_loss_drops_about_that_share_each_way(self):
        raw = RecordingSocket(incoming=[b"x"] * 20000)
        impaired = ImpairedSocket(raw, loss_percent=1, delay_ms=0, jitter_ms=0,
                                  rng=random.Random(5))
        for _ in range(20000):
            self.assertEqual(impaired.sendto(b"abc", ("127.0.0.1", 2)), 3)
        self.assertEqual(impaired.dropped_out + len(raw.sent), 20000)
        self.assertTrue(150 <= impaired.dropped_out <= 250, impaired.dropped_out)
        delivered = 0
        while True:
            try:
                impaired.recvfrom(4096)
            except socket.timeout:
                break
            delivered += 1
        self.assertEqual(impaired.dropped_in + delivered, 20000)
        self.assertTrue(150 <= impaired.dropped_in <= 250, impaired.dropped_in)

    def test_full_loss_sends_nothing_and_a_timeout_still_reaches_the_caller(self):
        raw = RecordingSocket(incoming=[b"x", b"y"])
        impaired = ImpairedSocket(raw, loss_percent=100, delay_ms=0, jitter_ms=0)
        impaired.sendto(b"abc", ("127.0.0.1", 2))
        self.assertEqual(raw.sent, [])
        with self.assertRaises(socket.timeout):
            impaired.recvfrom(4096)
        self.assertEqual(impaired.dropped_in, 2)

    def test_other_socket_calls_pass_through(self):
        raw = RecordingSocket()
        impaired = ImpairedSocket(raw, loss_percent=0, delay_ms=0, jitter_ms=0)
        impaired.settimeout(0.05)
        self.assertEqual(raw.timeout, 0.05)
        impaired.close()
        self.assertTrue(raw.closed)

    def test_delay_holds_each_send_and_keeps_order_without_jitter(self):
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(2.0)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        impaired = ImpairedSocket(sender, loss_percent=0, delay_ms=60, jitter_ms=0)
        try:
            started = time.perf_counter()
            for index in range(5):
                impaired.sendto(bytes([index]), receiver.getsockname())
            arrived = [receiver.recvfrom(16)[0][0] for _ in range(5)]
            elapsed = time.perf_counter() - started
        finally:
            impaired.close()
            receiver.close()
        self.assertEqual(arrived, [0, 1, 2, 3, 4])
        self.assertGreaterEqual(elapsed, 0.055)

    def test_jitter_varies_the_delay_but_never_reorders(self):
        raw = RecordingSocket()
        impaired = ImpairedSocket(raw, loss_percent=0, delay_ms=20, jitter_ms=15,
                                  rng=random.Random(3))
        try:
            for index in range(60):
                impaired.sendto(bytes([index]), ("127.0.0.1", 2))
                time.sleep(0.002)
            deadline = time.perf_counter() + 2.0
            while len(raw.sent) < 60 and time.perf_counter() < deadline:
                time.sleep(0.01)
        finally:
            impaired.close()
        self.assertEqual([data[0] for _t, data, _a in raw.sent], list(range(60)))

    def test_close_drops_what_is_still_queued(self):
        raw = RecordingSocket()
        impaired = ImpairedSocket(raw, loss_percent=0, delay_ms=500, jitter_ms=0)
        impaired.sendto(b"late", ("127.0.0.1", 2))
        impaired.close()
        time.sleep(0.05)
        self.assertEqual(raw.sent, [])
        self.assertFalse(impaired._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
