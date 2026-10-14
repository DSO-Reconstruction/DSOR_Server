"""Switch-character (LogoutCommand) handling: hubs, the dungeon countdown, cancels."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dso_server.protocol.gameplay_protocol import (
    Logout,
    PLAYER_LOGOUT_INFO_COMMAND,
    encode_logout,
    encode_player_logout_info,
)
from dso_server.runtime.transport import Session
from dso_server.services.character_server import CharacterServer
from dso_server.services.map_transition import LOGOUT_COUNTDOWN_SECONDS
from dso_server.tests.test_pvp_matchmaking import _FakeSocket, _Recorder, _quiet_server

HUB = "a0200_kingscity"
TUTORIAL = "a0001_start_tutorial_dun"
SWITCH_MAP_COMMAND = 0x0070


class _FakeTimer:
    def __init__(self, interval, function, args):
        self.interval, self.function, self.args = interval, function, tuple(args or ())
        self.started = self.cancelled = False
        self.daemon = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.function(*self.args)


class LogoutTests(unittest.TestCase):
    def setUp(self):
        self.server = _quiet_server(self)
        self.server.static_database = SimpleNamespace(
            gameplay_map_ruleset=lambda map_id: 5 if map_id == HUB else 1,
        )
        self.server.advertised_host = "127.0.0.1"
        self.server.port = 2190
        self.server.gameplay_router = None
        for name in ("_accept_gameplay_state_command", "_persist_gameplay_session"):
            patcher = patch.object(CharacterServer, name, lambda *a, **k: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.request = encode_logout(Logout(False, False))

    def _session(self, map_id):
        session = Session(("127.0.0.1", 50301), _FakeSocket())
        session.connected = True
        session.gameplay_mode = True
        session.gameplay_map = map_id
        session.gameplay_presence_callback = None
        session.timers = []

        def timer(interval, function, args=None, kwargs=None):
            handle = _FakeTimer(interval, function, args)
            session.timers.append(handle)
            return handle

        session.gameplay_deadline_timer = timer
        recorder = _Recorder()
        session.send_reliable_ordered = recorder
        session.recorder = recorder
        return session

    def _sent(self, session, command_id):
        return [
            (data, bits) for data, bits, _label in session.recorder.messages
            if len(data) >= 3 and int.from_bytes(data[1:3], "little") == command_id
        ]

    def test_hub_switch_is_immediate(self):
        s = self._session(HUB)
        self.server._handle_logout(s, self.request[0])
        self.assertEqual(len(self._sent(s, SWITCH_MAP_COMMAND)), 1)
        self.assertTrue(s.gameplay_exit_transition_started)
        self.assertEqual(s.timers, [])
        self.assertEqual(self._sent(s, PLAYER_LOGOUT_INFO_COMMAND), [])

    def test_dungeon_switch_waits_for_the_countdown(self):
        s = self._session(TUTORIAL)
        self.server._handle_logout(s, self.request[0])
        # Nothing leaves yet; the client is told the countdown.
        self.assertEqual(self._sent(s, SWITCH_MAP_COMMAND), [])
        self.assertEqual(
            self._sent(s, PLAYER_LOGOUT_INFO_COMMAND),
            [encode_player_logout_info(int(LOGOUT_COUNTDOWN_SECONDS))],
        )
        (timer,) = s.timers
        self.assertEqual(timer.interval, LOGOUT_COUNTDOWN_SECONDS)
        self.assertTrue(timer.started)
        self.assertFalse(s.gameplay_exit_transition_started)
        # A repeated request while counting down changes nothing.
        self.server._handle_logout(s, self.request[0])
        self.assertEqual(len(s.timers), 1)
        # The countdown elapses without a hit: the switch goes out.
        timer.fire()
        self.assertEqual(len(self._sent(s, SWITCH_MAP_COMMAND)), 1)
        self.assertTrue(s.gameplay_exit_transition_started)
        self.assertIsNone(s.gameplay_logout_timer)

    def test_a_hit_cancels_the_countdown(self):
        s = self._session(TUTORIAL)
        self.server._handle_logout(s, self.request[0])
        (timer,) = s.timers
        self.assertTrue(CharacterServer._cancel_pending_logout(s, "attacked"))
        self.assertTrue(timer.cancelled)
        self.assertEqual(
            self._sent(s, PLAYER_LOGOUT_INFO_COMMAND)[-1], encode_player_logout_info(0),
        )
        # A late fire of the cancelled timer is ignored.
        timer.fire()
        self.assertEqual(self._sent(s, SWITCH_MAP_COMMAND), [])
        self.assertFalse(s.gameplay_exit_transition_started)
        self.assertFalse(CharacterServer._cancel_pending_logout(s, "again"))
        # The player may ask again afterwards.
        self.server._handle_logout(s, self.request[0])
        self.assertEqual(len(s.timers), 2)
        s.timers[1].fire()
        self.assertEqual(len(self._sent(s, SWITCH_MAP_COMMAND)), 1)


if __name__ == "__main__":
    unittest.main()
