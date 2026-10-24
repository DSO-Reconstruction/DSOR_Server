"""Sequenced frames restart their index with every ordering index.

RakNet's sender resets the sequencing index whenever it assigns an ordering
index, and the official server's captures follow it: (order 8, seq 1..8),
(9, 0), (10, 0..11). The client sorts frames buffered behind a missing ordered
frame by ``hole * 2**20 + sequencing index``, so a counter that never restarts
reaches into the next hole's range after 2**20 sequenced sends -- several
hours of monster movement on one map. The next lost ordered frame then leaves
a stale sequenced frame at the head of that heap, and the client's ordered
delivery stops for good while every datagram is still acknowledged.
"""

from __future__ import annotations

import unittest

from dso_server.runtime.transport import RakNetServer, Session


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, address: tuple[str, int]) -> None:
        self.sent.append((data, address))


def sequenced(sock: FakeSocket) -> list[tuple[int, int]]:
    return [
        (frame["order_idx"], frame["sequence_idx"])
        for data, _address in sock.sent
        for frame in RakNetServer._parse_frames(data)
        if frame["reliability"] == 1
    ]


class SequencedFrameTests(unittest.TestCase):
    def test_sequencing_restarts_after_each_ordered_message(self):
        sock = FakeSocket()
        session = Session(("127.0.0.1", 41970), sock)
        session.send_unreliable_sequenced(b"\x85")
        session.send_unreliable_sequenced(b"\x85")
        session.send_reliable_ordered(b"\x01")
        session.send_unreliable_sequenced(b"\x85")
        session.send_unreliable_sequenced(b"\x85")
        session.send_multi_reliable_ordered(b"\x02", b"\x03")
        session.send_unreliable_sequenced(b"\x85")
        session.send_reliable_ordered_split(bytes(3000))
        session.send_unreliable_sequenced(b"\x85")

        self.assertEqual(
            sequenced(sock),
            [(0, 0), (0, 1), (1, 0), (1, 1), (3, 0), (4, 0)],
        )

    def test_long_session_counter_does_not_survive_an_ordered_message(self):
        sock = FakeSocket()
        session = Session(("127.0.0.1", 41971), sock)
        session.fb.sequence_idx = (1 << 20) + 5
        session.send_reliable_ordered(b"\x01")
        session.send_unreliable_sequenced(b"\x85")

        self.assertEqual(sequenced(sock), [(1, 0)])


if __name__ == "__main__":
    unittest.main()
