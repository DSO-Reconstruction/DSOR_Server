"""A retired connection must end on the client, not linger.

``drop_session`` used to send a bare 0x15, which a connected RakNet peer
ignores, and every later datagram from that endpoint re-created a session
that acknowledged it and answered its pings. The client's connection then
never timed out and the player sat in a world nothing updated. The same held
for a client returning after the receive timeout retired it.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from dso_server.runtime.context import RAKNET_MAGIC
from dso_server.runtime.transport import RakNetServer


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, address: tuple[str, int]) -> None:
        self.sent.append((data, address))


def reliable_frameset(sequence: int, index: int, payload: bytes) -> bytes:
    return (
        b"\x84" + sequence.to_bytes(3, "little")
        + b"\x60" + (len(payload) * 8).to_bytes(2, "big")
        + index.to_bytes(3, "little") + index.to_bytes(3, "little") + b"\x00"
        + payload
    )


class RetiredEndpointTests(unittest.TestCase):
    ADDRESS = ("127.0.0.1", 41980)

    def setUp(self) -> None:
        self.server = RakNetServer("127.0.0.1", 0, "retired-endpoint-test")
        self.server.sock = FakeSocket()
        self.session = self.server._session(self.ADDRESS)
        self.session.connected = True
        with patch("builtins.print"):
            self.server.drop_session(self.session, reason="test")

    def test_dropped_client_is_told_in_a_frameset(self):
        data, address = self.server.sock.sent[-1]

        self.assertEqual(address, self.ADDRESS)
        self.assertEqual(
            [
                (frame["reliability"], frame["payload"])
                for frame in RakNetServer._parse_frames(data)
            ],
            [(0, b"\x15")],
        )

    def test_retired_endpoint_is_not_acknowledged(self):
        sent = len(self.server.sock.sent)
        self.server._dispatch(
            reliable_frameset(9, 40, b"\x8b\x00"), self.ADDRESS
        )
        self.server._dispatch(b"\xc0\x00\x01\x01\x00\x00\x00", self.ADDRESS)
        self.server._dispatch(b"\xa0\x00\x01\x01\x00\x00\x00", self.ADDRESS)

        self.assertIsNone(self.server.sessions.get(self.ADDRESS))
        self.assertEqual(len(self.server.sock.sent), sent)

    def test_reconnect_from_a_retired_endpoint_opens_a_session(self):
        self.server._dispatch(
            b"\x05" + RAKNET_MAGIC + b"\x05" + bytes(1246), self.ADDRESS
        )

        replacement = self.server.sessions.get(self.ADDRESS)
        self.assertIsNotNone(replacement)
        self.assertIsNot(replacement, self.session)
        self.assertEqual(self.server.sock.sent[-1][0][0], 0x06)


if __name__ == "__main__":
    unittest.main()
