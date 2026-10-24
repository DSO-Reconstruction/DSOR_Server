"""No reliable datagram may exceed what the client accepts.

The client drops an oversized datagram without acknowledging it. A reliable
frame it never receives is retransmitted forever and holds the client's whole
ordered stream behind it, so monsters, hits and replies stop for that player
while the server keeps simulating. A pack that starts its attacks in one tick
builds such a batch, so the transport fragments anything over the budget.
"""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dso_server.runtime.transport import (
    RELIABLE_DATAGRAM_LIMIT,
    RakNetServer,
    Session,
)


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, address: tuple[str, int]) -> None:
        self.sent.append((data, address))


class OversizedReliableTests(unittest.TestCase):
    def test_oversized_message_is_fragmented_without_an_ordering_gap(self):
        sock = FakeSocket()
        session = Session(("127.0.0.1", 41960), sock)
        session.send_reliable_ordered(b"\x01" * 4, label="before")
        payload = bytes(range(256)) * 8
        with patch("builtins.print") as printed:
            self.assertIsNotNone(
                session.send_reliable_ordered(payload, label="monster-skill")
            )
        session.send_reliable_ordered(b"\x02" * 4, label="after")

        self.assertTrue(
            all(len(data) <= RELIABLE_DATAGRAM_LIMIT for data, _ in sock.sent)
        )
        frames = [
            frame
            for data, _address in sock.sent
            for frame in RakNetServer._parse_frames(data)
        ]
        fragments = [frame for frame in frames if frame["split"] is not None]
        self.assertGreater(len(fragments), 1)
        self.assertEqual(
            b"".join(
                frame["payload"]
                for frame in sorted(
                    fragments, key=lambda frame: frame["split"]["idx"]
                )
            ),
            payload,
        )
        # One message takes one ordering slot, with nothing skipped around it.
        self.assertEqual({frame["order_idx"] for frame in fragments}, {1})
        self.assertEqual(
            (frames[0]["order_idx"], frames[-1]["order_idx"]), (0, 2)
        )
        self.assertEqual(session.reliable_backlog_size(), len(sock.sent))
        self.assertIn("'monster-skill'", printed.call_args.args[0])

    def test_message_that_fits_one_datagram_is_sent_whole(self):
        sock = FakeSocket()
        session = Session(("127.0.0.1", 41961), sock)
        session.send_reliable_ordered(
            b"\x03" * (RELIABLE_DATAGRAM_LIMIT - 14), label="fits"
        )
        self.assertEqual(len(sock.sent), 1)
        self.assertEqual(len(sock.sent[0][0]), RELIABLE_DATAGRAM_LIMIT)
        self.assertIsNone(RakNetServer._parse_frames(sock.sent[0][0])[0]["split"])


class StuckReliableReportTests(unittest.TestCase):
    def _session(self, now: float, *, received_ago: float) -> Session:
        session = Session(("127.0.0.1", 41962), FakeSocket())
        session.pending_reliable[7] = {
            "data": b"\x84" + b"\x00" * 1245,
            "label": "prop-info-response",
            "sent_at": now,
            "queued_at": now - 6.0,
            "attempts": 25,
        }
        session.last_received_at = now - received_ago
        return session

    def test_frame_a_talking_client_never_acknowledges_is_named(self):
        now = time.perf_counter()
        session = self._session(now, received_ago=0.05)
        server = SimpleNamespace(service_name="DrasaOnlineMapServer")
        with patch("builtins.print") as printed:
            RakNetServer._report_stuck_reliable(server, session, now)
            RakNetServer._report_stuck_reliable(server, session, now + 1.0)
        self.assertEqual(printed.call_count, 1)
        message = printed.call_args.args[0]
        self.assertIn("'prop-info-response'", message)
        self.assertIn("1246 bytes", message)
        self.assertIn("25 attempts", message)

    def test_silent_client_is_left_to_the_receive_timeout(self):
        now = time.perf_counter()
        session = self._session(now, received_ago=6.0)
        server = SimpleNamespace(service_name="DrasaOnlineMapServer")
        with patch("builtins.print") as printed:
            RakNetServer._report_stuck_reliable(server, session, now)
        printed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
