"""Closing the character screen must not arm a gameplay login."""

from unittest import TestCase
from unittest.mock import Mock, patch

from dso_server.protocol.character_selection import BitReader
from dso_server.protocol.gameplay_protocol import Logout, encode_logout
from dso_server.protocol.map_packets import SWITCH_MAP_COMMAND
from dso_server.runtime.transport import Session
from dso_server.services.character_server import CharacterServer
from dso_server.services.login_game import LoginGameServer
from dso_server.tests.fixtures.characters import character_profile, character_server


class CharacterScreenLogoutTests(TestCase):
    def setUp(self):
        self.server = character_server(self, [character_profile(42, "P2W")])
        self.router = LoginGameServer(
            "127.0.0.2", "127.0.0.2:2192",
            account_store=self.server.account_store,
            social_store=self.server.social_store,
        )
        self.server.gameplay_router = self.router
        prewarm = patch.object(CharacterServer, "_prewarm_map_encounter")
        prewarm.start()
        self.addCleanup(prewarm.stop)

    def session(self, port=41000):
        session = Session(("127.0.0.1", port), Mock())
        session.connected = True
        session.send_reliable_ordered = Mock()
        session.send_wire_first_delivery_second = Mock()
        session.send_multi_reliable_ordered = Mock()
        return session

    def logout(self, session, request):
        payload, bits = encode_logout(request)
        session.current_message_bit_length = bits
        self.server._on_char_op(session, payload)

    def assert_character_login(self):
        login = self.session(41001)
        self.router._on_connected(login)
        self.router._on_hello(login, b"\x8a")
        self.assertFalse(login.gameplay_mode)
        redirect = login.send_wire_first_delivery_second.call_args.args[1]
        reader = BitReader(*redirect)
        self.assertEqual(reader.read_uint8(), 0x84)
        self.assertEqual(int.from_bytes(reader.read_bytes(2), "little"), SWITCH_MAP_COMMAND)
        self.assertEqual(reader.read_string(), "127.0.0.2:2192")
        self.assertTrue(reader.read_bool())

        destination = self.session(41002)
        self.server._on_connected(destination)
        self.server._on_hello(destination, b"\x8a")
        self.assertFalse(destination.gameplay_mode)
        self.assertIsNone(destination.gameplay_character)
        self.assertTrue(destination.map_bootstrap_sent)

    def test_logout_then_immediate_relogin_stays_on_character_screen(self):
        for selected in (None, 42):
            for request in (Logout(False, False), Logout(False, True), Logout(True, False), Logout(True, True)):
                with self.subTest(selected=selected, request=request):
                    session = self.session()
                    session.selected_character_id = selected
                    self.logout(session, request)
                    self.assertFalse(self.server.gameplay_routes.is_armed(session.addr[0]))
                    self.assertFalse(self.router.gameplay_routes.is_armed(session.addr[0]))
                    self.assert_character_login()

    def test_late_logout_does_not_rearm_a_completed_play_handoff(self):
        session = self.session()
        request = b"\x8b\x87\x00\x03\x00" + (42).to_bytes(4, "little")
        self.server._on_char_op(session, request)
        self.assertEqual(self.router.gameplay_routes.claim(session.addr[0]).character_id, 42)
        self.assertEqual(self.server.gameplay_routes.claim(session.addr[0]).character_id, 42)

        self.logout(session, Logout(False, True))
        self.assertFalse(self.router.gameplay_routes.is_armed(session.addr[0]))
        self.assertFalse(self.server.gameplay_routes.is_armed(session.addr[0]))
        self.assert_character_login()

    def test_logout_preserves_a_play_handoff_still_in_progress(self):
        session = self.session()
        request = b"\x8b\x87\x00\x03\x00" + (42).to_bytes(4, "little")
        self.server._on_char_op(session, request)
        login_route = self.router.gameplay_routes.peek(session.addr[0])
        map_route = self.server.gameplay_routes.peek(session.addr[0])

        self.logout(session, Logout(False, True))
        self.assertIs(self.router.gameplay_routes.peek(session.addr[0]), login_route)
        self.assertIs(self.server.gameplay_routes.peek(session.addr[0]), map_route)

    def test_explicit_play_still_enters_gameplay_with_the_correct_ui_state(self):
        session = self.session()
        request = b"\x8b\x87\x00\x03\x00" + (42).to_bytes(4, "little")
        self.server._on_char_op(session, request)
        redirect = session.send_reliable_ordered.call_args.args[0]
        reader = BitReader(*redirect)
        self.assertEqual(reader.read_uint8(), 0x84)
        self.assertEqual(int.from_bytes(reader.read_bytes(2), "little"), SWITCH_MAP_COMMAND)
        self.assertEqual(reader.read_string(), "127.0.0.2:2190")
        self.assertFalse(reader.read_bool())

        login = self.session(41001)
        self.router._on_connected(login)
        self.router._on_hello(login, b"\x8a")
        redirect = login.send_wire_first_delivery_second.call_args.args[1]
        reader = BitReader(*redirect)
        reader.read_bytes(3)
        self.assertEqual(reader.read_string(), "127.0.0.2:2192")
        self.assertFalse(reader.read_bool())

        destination = self.session(41002)
        self.server._on_connected(destination)
        self.server._on_hello(destination, b"\x8a")
        self.assertTrue(destination.gameplay_mode)
        self.assertEqual(destination.gameplay_character.character_id, 42)
        self.assertEqual(destination.gameplay_map, "a0200_kingscity")
        self.assertFalse(self.router.gameplay_routes.is_armed(session.addr[0]))
        self.assertFalse(self.server.gameplay_routes.is_armed(session.addr[0]))

    def test_truncated_logout_does_not_create_a_route_or_disconnect(self):
        session = self.session()
        self.server._on_char_op(session, encode_logout(Logout(False, True))[0][:3])
        self.assertFalse(self.router.gameplay_routes.is_armed(session.addr[0]))
        self.assertFalse(self.server.gameplay_routes.is_armed(session.addr[0]))
        session.send_reliable_ordered.assert_not_called()
