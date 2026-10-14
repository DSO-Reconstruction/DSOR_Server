"""The gameplay-ready command must not run initialization on the receive thread.

``_send_local_player_initialization`` measured 100-600 ms with a warm
encounter cache and 1.3-1.6 s when the destination population had to be
generated.  Run inline from ``_on_char_op`` it stopped the UDP receive thread
reading or acknowledging any client's datagrams for that long, so every
player's ping rose by the full amount each time anyone entered a map.
"""

from __future__ import annotations

import struct
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dso_server.runtime.gameplay_scheduler import (
    DEFAULT_GAMEPLAY_INITIALIZATION_WORKERS,
)
from dso_server.runtime.transport import Session
from dso_server.services.character_server import CharacterServer
from dso_server.tests.fixtures.characters import character_server


GAMEPLAY_READY = b"\x8b" + struct.pack("<H", 0x006F) + struct.pack("<I", 1)


class FakeSocket:
    def __init__(self) -> None:
        self.sent = []

    def sendto(self, data, address) -> None:
        self.sent.append((data, address))


class InitializationOffloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = character_server(self)
        self.server.sock = FakeSocket()
        self.server.gameplay_initialization_scheduler.start()
        self.addCleanup(
            self.server.gameplay_initialization_scheduler.shutdown,
            wait=True, cancel_pending=True,
        )

    def _gameplay_session(self) -> Session:
        session = self.server._session(("127.0.0.1", 40510))
        session.connected = True
        session.gameplay_mode = True
        session.gameplay_map = "a0200_kingscity"
        return session

    def test_character_service_owns_a_dedicated_initialization_worker(self):
        scheduler = self.server.gameplay_initialization_scheduler
        self.assertIsNot(scheduler, self.server.gameplay_deadline_scheduler)
        self.assertIsNot(scheduler, self.server.gameplay_ingress_scheduler)
        self.assertEqual(
            scheduler.worker_count, DEFAULT_GAMEPLAY_INITIALIZATION_WORKERS
        )
        self.assertIn("initialization", scheduler.name)

    def test_gameplay_ready_runs_initialization_on_the_worker(self):
        session = self._gameplay_session()
        ran_on = []
        finished = threading.Event()

        def record(_self, s, *, scheduled=False):
            ran_on.append((threading.current_thread().name, s))
            finished.set()

        with patch.object(
            CharacterServer, "_send_local_player_initialization", record
        ):
            self.server._on_char_op(session, GAMEPLAY_READY)
            self.assertTrue(
                finished.wait(2.0), "initialization never reached the worker"
            )
        thread_name, initialized = ran_on[0]
        self.assertIs(initialized, session)
        self.assertNotEqual(thread_name, threading.current_thread().name)
        self.assertIn("gameplay-initialization", thread_name)

    def test_gameplay_ready_returns_to_the_receive_loop_before_initializing(self):
        session = self._gameplay_session()
        started = threading.Event()
        release = threading.Event()

        def slow(_self, s, *, scheduled=False):
            started.set()
            release.wait(2.0)

        with patch.object(
            CharacterServer, "_send_local_player_initialization", slow
        ):
            self.server._on_char_op(session, GAMEPLAY_READY)
            # The receive path has already returned; the work is in flight.
            self.assertTrue(started.wait(2.0))
            release.set()

    def test_a_replaced_session_is_not_initialized(self):
        stale = Session(("127.0.0.1", 40511), self.server.sock)
        stale.connected = True
        stale.gameplay_mode = True
        stale.gameplay_map = "a0200_kingscity"
        calls = []
        done = threading.Event()

        def record(_self, s, *, scheduled=False):
            calls.append(s)

        with patch.object(
            CharacterServer, "_send_local_player_initialization", record
        ):
            self.server._schedule_local_player_initialization(stale)
            # Let the single worker drain by scheduling a marker behind it.
            self.server.gameplay_initialization_scheduler.timer(
                0.0, done.set
            ).start()
            self.assertTrue(done.wait(2.0))
        self.assertEqual(calls, [])

    def test_a_bare_service_without_a_scheduler_initializes_inline(self):
        calls = []
        bare = SimpleNamespace(
            _send_local_player_initialization=lambda s: calls.append(s)
        )
        session = Session(("127.0.0.1", 40512), self.server.sock)
        offloaded = CharacterServer._schedule_local_player_initialization(
            bare, session
        )
        self.assertFalse(offloaded)
        self.assertEqual(calls, [session])


if __name__ == "__main__":
    unittest.main()
