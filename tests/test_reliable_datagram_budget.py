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

from dso_server.protocol.gameplay_protocol import (
    Move,
    MoveBatch,
    MoveServerCommand,
    encode_move_batch,
)
from dso_server.runtime.context import (
    CMD_001B,
    CMD_001B_BITS,
    MONSTER_MOVEMENT_PACKET_BUDGET,
    TAIL_FRAG_SIZE,
)
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


def sent_frames(sock: FakeSocket) -> list[dict]:
    return [
        frame
        for data, _address in sock.sent
        for frame in RakNetServer._parse_frames(data)
    ]


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


class OversizedBatchTests(unittest.TestCase):
    """Several frames in one call obey the same budget as a single one."""

    def test_batch_too_large_for_one_datagram_is_spread_in_order(self):
        # The character list that just fits one datagram on its own, with
        # cmd 0x001b behind it: together they used to make 1,236 bytes.
        sock = FakeSocket()
        session = Session(("127.0.0.1", 41963), sock)
        selection = bytes(range(200)) * 6
        with patch("builtins.print"):
            session.send_multi_reliable_ordered(
                selection, (CMD_001B, CMD_001B_BITS),
                label="character-selection",
            )

        self.assertGreater(len(sock.sent), 1)
        self.assertTrue(
            all(len(data) <= RELIABLE_DATAGRAM_LIMIT for data, _ in sock.sent)
        )
        frames = sent_frames(sock)
        self.assertEqual([frame["order_idx"] for frame in frames], [0, 1])
        self.assertEqual(frames[0]["payload"], selection)
        self.assertEqual(
            (frames[1]["payload"], frames[1]["bit_length"]),
            (CMD_001B, CMD_001B_BITS),
        )
        self.assertEqual(session.reliable_backlog_size(), len(sock.sent))

    def test_batch_that_fits_stays_one_datagram(self):
        # The captured gameplay bootstrap: map announcement plus bare 0x88.
        sock = FakeSocket()
        session = Session(("127.0.0.1", 41964), sock)
        session.send_multi_reliable_ordered(b"\x86" * 15, b"\x88")

        self.assertEqual(len(sock.sent), 1)
        self.assertEqual(len(sock.sent[0][0]), 4 + 10 + 15 + 10 + 1)
        self.assertEqual(
            [frame["order_idx"] for frame in sent_frames(sock)], [0, 1]
        )

    def test_message_no_datagram_holds_is_fragmented_within_the_batch(self):
        sock = FakeSocket()
        session = Session(("127.0.0.1", 41965), sock)
        large = bytes(range(256)) * 12
        with patch("builtins.print"):
            session.send_multi_reliable_ordered(b"\x01", large, b"\x02")

        self.assertTrue(
            all(len(data) <= RELIABLE_DATAGRAM_LIMIT for data, _ in sock.sent)
        )
        frames = sent_frames(sock)
        fragments = [frame for frame in frames if frame["split"] is not None]
        self.assertEqual(
            b"".join(
                frame["payload"]
                for frame in sorted(
                    fragments, key=lambda frame: frame["split"]["idx"]
                )
            ),
            large,
        )
        self.assertEqual(
            [frame["order_idx"] for frame in frames],
            [0, *[1] * len(fragments), 2],
        )

    def test_invalid_payload_takes_no_ordering_index(self):
        # A batch whose second frame fails to frame used to keep the index
        # its first frame had already taken -- a gap nothing ever fills.
        sock = FakeSocket()
        session = Session(("127.0.0.1", 41966), sock)
        with self.assertRaises(ValueError):
            session.send_multi_reliable_ordered(b"\x01\x02", (b"\x03\x04", 99))
        session.send_reliable_ordered(b"\x05", label="after")

        self.assertEqual(
            [frame["order_idx"] for frame in sent_frames(sock)], [0]
        )


class SplitTrailerTests(unittest.TestCase):
    """Payloads sent behind a split message never overfill a datagram."""

    def _send_selection(self, size: int):
        sock = FakeSocket()
        session = Session(("127.0.0.1", 41967), sock)
        packet = bytes(index % 251 for index in range(size))
        session.send_reliable_ordered_split(
            tuple(
                packet[offset:offset + TAIL_FRAG_SIZE]
                for offset in range(0, size, TAIL_FRAG_SIZE)
            ),
            trailing_payloads=((CMD_001B, CMD_001B_BITS),),
            label_prefix="character-selection",
            total_bit_length=size * 8,
        )
        return session, sock

    def test_trailer_rides_the_last_fragment_when_it_fits(self):
        # A live roster's 2,209-byte list, bundled as in the captured trace.
        _session, sock = self._send_selection(2209)

        self.assertEqual(len(sock.sent), 2)
        last = RakNetServer._parse_frames(sock.sent[-1][0])
        self.assertEqual([frame["payload"] for frame in last][1:], [CMD_001B])

    def test_trailer_after_a_full_last_fragment_gets_its_own_datagram(self):
        session, sock = self._send_selection(2 * TAIL_FRAG_SIZE)

        self.assertEqual(
            [len(data) for data, _ in sock.sent],
            [TAIL_FRAG_SIZE + 24, TAIL_FRAG_SIZE + 24, 4 + 10 + len(CMD_001B)],
        )
        self.assertEqual(
            [frame["order_idx"] for frame in sent_frames(sock)], [0, 0, 1]
        )
        self.assertEqual(session.reliable_backlog_size(), 3)

    def test_invalid_trailer_is_refused_before_any_fragment_is_sent(self):
        sock = FakeSocket()
        session = Session(("127.0.0.1", 41968), sock)
        with self.assertRaises(ValueError):
            session.send_reliable_ordered_split(
                bytes(3000), trailing_payloads=((b"\x01\x02", 99),)
            )
        self.assertEqual(sock.sent, [])
        session.send_reliable_ordered(b"\x05", label="after")

        self.assertEqual(
            [frame["order_idx"] for frame in sent_frames(sock)], [0]
        )


class MovementBatchBudgetTests(unittest.TestCase):
    """Unreliable walking samples cannot be fragmented, so they must fit."""

    @staticmethod
    def _datagram_size(moves: int) -> int:
        move = Move.from_values((1.0, 2.0, 3.0), 1.0, 0.0, 0.0, 100, 4)
        sock = FakeSocket()
        Session(("127.0.0.1", 41969), sock).send_unreliable_sequenced(
            encode_move_batch(
                MoveBatch(
                    tuple(
                        MoveServerCommand(move, 1000 + index)
                        for index in range(moves)
                    )
                )
            )
        )
        return len(sock.sent[0][0])

    def test_largest_configurable_batch_fits_one_datagram(self):
        self.assertLessEqual(MONSTER_MOVEMENT_PACKET_BUDGET, 55)
        self.assertLessEqual(self._datagram_size(55), RELIABLE_DATAGRAM_LIMIT)
        self.assertGreater(self._datagram_size(56), RELIABLE_DATAGRAM_LIMIT)


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
