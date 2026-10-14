"""Login enforcement must bind every connection to exactly one account.

The address gate (``connection_filter``) runs before the client has said who
it is, so it can only prove that *someone* at that address logged in. Behind a
shared NAT or VPN exit that is everybody. These guard the second layer: a hello
that presents no live session is dropped rather than served the whole roster,
an unidentified connection owns no characters, and an identified account never
claims a gameplay route armed for someone else's character.
"""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from dso_server.persistence.account_registry import AccountRegistry
from dso_server.runtime.gameplay_routes import GameplayRouteTable
from dso_server.runtime.transport import RakNetServer, Session
from dso_server.services.lifecycle import LifecycleService


class _Socket:
    def sendto(self, *_args, **_kwargs):
        return 0


class _Server(RakNetServer):
    def __init__(self):
        super().__init__("127.0.0.1", 0, "TestService")
        self.hellos = []
        self.dropped = []

    def _on_hello(self, s, payload):
        self.hellos.append(s.addr)

    def drop_session(self, session, *, reason):
        self.dropped.append(reason)
        return True


def _hello_with_session(session_id: str) -> bytes:
    # ``03 05`` tag, 16-byte length prefix, then the GUID: the layout
    # AccountRegistry.find_session anchors on.
    return b"\x01\x01\x00\x00\x02\x01\x00\x00" + b"\x03\x05\x10\x00" + uuid.UUID(session_id).bytes


class LoginEnforcementTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.registry = AccountRegistry(Path(self._tmp.name) / "accounts.sqlite3")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.registry.close)
        self.registry.register("alice", "Correct-Horse-9", client_ip="10.0.0.5")
        self.session = self.registry.login(
            "alice", "Correct-Horse-9", client_ip="10.0.0.5"
        )

    def _server(self, *, strict: bool) -> _Server:
        server = _Server()
        server.account_registry = self.registry
        server.require_account_identity = strict
        return server

    def test_unidentified_hello_is_dropped_under_enforcement(self):
        server = self._server(strict=True)
        stranger = Session(("10.0.0.5", 40001), _Socket())
        server._on_message(stranger, 0x8A, b"\x00" * 48)
        self.assertEqual(server.hellos, [])
        self.assertEqual(len(server.dropped), 1)
        self.assertIsNone(stranger.account_id)

    def test_identified_hello_is_served_under_enforcement(self):
        server = self._server(strict=True)
        alice = Session(("10.0.0.5", 40002), _Socket())
        server._on_message(alice, 0x8A, _hello_with_session(self.session.session_id))
        self.assertEqual(server.hellos, [alice.addr])
        self.assertEqual(server.dropped, [])
        self.assertEqual(alice.account_username, "alice")

    def test_unidentified_hello_keeps_working_without_enforcement(self):
        server = self._server(strict=False)
        local = Session(("127.0.0.1", 40003), _Socket())
        server._on_message(local, 0x8A, b"\x00" * 48)
        self.assertEqual(server.hellos, [local.addr])
        self.assertEqual(server.dropped, [])

    def test_unidentified_connection_owns_nothing_under_enforcement(self):
        session = Session(("10.0.0.5", 40004), _Socket())
        strict = SimpleNamespace(
            account_registry=self.registry, require_account_identity=True
        )
        lenient = SimpleNamespace(
            account_registry=self.registry, require_account_identity=False
        )
        self.assertEqual(LifecycleService._owned_character_ids(strict, session), ())
        self.assertIsNone(LifecycleService._owned_character_ids(lenient, session))


class SharedAddressRouteTests(unittest.TestCase):
    def setUp(self):
        self.routes = GameplayRouteTable(ttl_seconds=60.0)
        self.routes.arm(
            "10.0.0.5", "a0200_kingscity", character=SimpleNamespace(character_id=1)
        )

    def test_identified_account_never_takes_another_accounts_route(self):
        self.assertIsNone(
            self.routes.peek("10.0.0.5", character_ids=(2,), identified=True)
        )
        self.assertIsNone(
            self.routes.claim("10.0.0.5", character_ids=(2,), identified=True)
        )
        # Still there for its rightful owner.
        self.assertEqual(self.routes.pending("10.0.0.5"), 1)
        route = self.routes.claim("10.0.0.5", character_ids=(1,), identified=True)
        self.assertEqual(route.character_id, 1)

    def test_account_without_characters_takes_nothing(self):
        self.assertIsNone(
            self.routes.claim("10.0.0.5", character_ids=(), identified=True)
        )
        self.assertEqual(self.routes.pending("10.0.0.5"), 1)

    def test_unattributed_route_stays_claimable(self):
        self.routes.arm("10.0.0.5", "a0000_char", login_handoff=True)
        route = self.routes.claim("10.0.0.5", character_ids=(2,), identified=True)
        self.assertIsNone(route.character_id)
        self.assertEqual(self.routes.pending("10.0.0.5"), 1)

    def test_unidentified_connection_keeps_historical_oldest_first(self):
        route = self.routes.claim("10.0.0.5", character_ids=(), identified=False)
        self.assertEqual(route.character_id, 1)


if __name__ == "__main__":
    unittest.main()
