"""A shared map must keep a monster simulation thread whatever the election.

Monsters froze for every player on an instance -- while map travel, chat and
everything else kept working -- when the session that owned the instance's
only simulation thread handed it off and nobody took it over: the claim and
the re-elections disagreed about player summons, a start request could land
while the old worker was retiring, and nothing noticed a map left with no
worker at all.
"""

from __future__ import annotations

import copy
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.character_selection import LOCAL_CHARACTER_ID_BASE
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.protocol.gameplay_protocol import Move
from dso_server.runtime.gameplay_scheduler import GameplayDeadlineScheduler
from dso_server.runtime.monster_runtime_diagnostics import worker_snapshot
from dso_server.runtime.stall_watchdog import StallWatchdog
from dso_server.runtime.transport import Session
from dso_server.services import monster_chase_loop
from dso_server.services.character_server import CharacterServer
from dso_server.services.monster_chase_loop import (
    MONSTER_TICK_IDLE,
    _FixedRateChaseLoop,
)
from dso_server.tests.fixtures.characters import character_server

MAP_ID = "b1201_pve"


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, address: tuple[str, int]) -> None:
        self.sent.append((data, address))


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _stop_workers(*sessions) -> None:
    workers = [
        worker for worker in worker_snapshot() if worker.session in sessions
    ]
    for worker in workers:
        worker.cancel()
    for worker in workers:
        if worker._thread.ident is not None:
            worker._thread.join(2.0)


class SharedMapOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sock = FakeSocket()
        self.scheduler = GameplayDeadlineScheduler(
            name="map-ownership-test", worker_count=2
        )
        self.scheduler.start()
        self.server = character_server(self)
        self.server.sock = self.sock
        self.encounter = load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH, MAP_ID, include_spawn_areas=False
        )
        self.assertIsNotNone(self.encounter)
        self.combat = LocalCombatState(
            self.encounter, player_actor_id=1, player_health=1000
        )
        # Remote actor ids derive from the character id, and every id below
        # the local base maps to one actor, so each player needs a real one.
        # Lower character id, standing somewhere away from every pack.
        self.idle = self._session(
            40100, LOCAL_CHARACTER_ID_BASE + 1, spawned=False
        )
        # Higher character id, fighting.
        self.fighter = self._session(
            40101, LOCAL_CHARACTER_ID_BASE + 2, spawned=True
        )

    def tearDown(self) -> None:
        _stop_workers(*tuple(self.server.sessions.values()))
        self.scheduler.shutdown(wait=True, cancel_pending=True)

    def _session(self, port: int, character_id: int, *, spawned: bool):
        character = copy.deepcopy(
            self.server.character_selection.active_character
        )
        character.character_id = character_id
        character.name = f"P{character_id}"
        session = Session(
            ("127.0.0.1", port),
            self.sock,
            gameplay_deadline_scheduler=self.scheduler,
        )
        session.gameplay_mode = True
        session.connected = True
        session.gameplay_initialization_sent = True
        session.gameplay_presence_ready = True
        session.gameplay_map = MAP_ID
        session.gameplay_character = character
        session.gameplay_monster_encounter = self.encounter
        session.gameplay_combat_state = self.combat
        if spawned:
            session.gameplay_monsters_spawned = True
            session.gameplay_spawned_monster_actor_ids.update(
                self.encounter.actor_ids
            )
            session.gameplay_announced_monster_actor_ids.update(
                self.encounter.actor_ids
            )
        session.gameplay_last_move = Move.from_values(
            self.encounter.coordinate_frame.world_to_wire(
                self.encounter.trigger_center
            ),
            0.0, 0.0, 0.0, 1900, 0,
        )
        session.gameplay_presence_callback = (
            lambda value, event: self.server._handle_presence_event(
                value, event
            )
        )
        self.server.sessions[session.addr] = session
        return session

    def _summon_for(self, session) -> None:
        """Give ``session`` a long-lived summon from the installed content."""

        path = Path(DEFAULT_STATIC_DB_PATH)
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT rowid FROM _Template_Skill "
                "WHERE COALESCE(Summons, 0) != 0 "
                "AND COALESCE(SummonMonsterId, '') != '' "
                "AND COALESCE(SummonMonsterAmount, 0) > 0"
            ).fetchall()
        for (rowid,) in rows:
            definition = self.combat.player_summon_definition(int(rowid) - 1)
            if (
                definition is not None
                and not 0 < definition.lifetime_seconds < 30
                and not 0 < definition.owner_despawn_range < 30
            ):
                break
        else:
            self.skipTest("installed content has no long-lived player summon")
        owner_actor_id = CharacterServer._remote_actor_id(
            session.gameplay_character.character_id
        )
        with self.combat.lock:
            self.combat.spawn_player_summon(
                definition,
                owner_actor_id=owner_actor_id,
                position=self.encounter.trigger_center,
                yaw=0.0,
                level=100,
                server_tick=1900,
                player_damage_range=(100.0, 200.0),
                now=time.perf_counter(),
            )
        self.assertTrue(self.combat.player_summons)

    def test_claim_and_reelection_agree_once_a_summon_is_out(self):
        self._summon_for(self.fighter)
        event = self.server._handle_presence_event
        self.assertTrue(event(self.idle, ("claim_monster_world",)))
        self.assertFalse(event(self.fighter, ("claim_monster_world",)))
        with patch.object(CharacterServer, "_start_monster_chase") as start:
            event(self.fighter, ("monster_visibility_changed",))
        # The fighter used to re-elect itself here and be refused.
        start.assert_called_once_with(self.idle)

    def test_departure_reelects_by_the_claims_rule(self):
        self._summon_for(self.fighter)
        departing_owner = self._session(
            40102, LOCAL_CHARACTER_ID_BASE, spawned=True
        )
        with patch.object(CharacterServer, "_start_monster_chase") as start:
            self.server._handle_presence_event(departing_owner, "disconnect")
        start.assert_called_once_with(self.idle)

    def test_a_summon_never_leaves_the_instance_without_a_worker(self):
        self.assertTrue(CharacterServer._start_monster_chase(self.fighter))
        fighter_worker = self.fighter.gameplay_chase_timer
        self.assertTrue(
            _wait_for(lambda: fighter_worker.diagnostic_ticks_completed > 0)
        )
        self._summon_for(self.fighter)
        # Summons make every player eligible, so the lower-id idle player now
        # owns the map; the fighter's worker must hand it over, not drop it.
        self.assertTrue(
            _wait_for(fighter_worker.has_exited),
            "the fighter's worker kept a map it no longer owns",
        )
        self.assertTrue(
            _wait_for(lambda: CharacterServer._live_monster_worker(self.idle)),
            "the map was left with no simulation thread",
        )
        owner_worker = self.idle.gameplay_chase_timer
        ticks = owner_worker.diagnostic_ticks_completed
        self.assertTrue(
            _wait_for(
                lambda: owner_worker.diagnostic_ticks_completed >= ticks + 3
            ),
            f"the new owner is not simulating: "
            f"{owner_worker.diagnostic_last_result!r}",
        )


class RetiringWorkerRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = Session(("127.0.0.1", 41900), object())
        self.session.connected = True
        self.session.gameplay_monsters_spawned = True
        self.session.gameplay_combat_state = SimpleNamespace(
            player_summons={}, has_regenerating_players=False,
        )

    def tearDown(self) -> None:
        _stop_workers(self.session)

    def test_start_request_while_the_tick_decides_to_retire_is_kept(self):
        session = self.session
        requests = []
        resumed = threading.Event()

        def tick(tick_session, _generation):
            if tick_session is not session:
                return True
            if not requests:
                # An ActorRequest lands while this tick decides to retire.
                requests.append(CharacterServer._start_monster_chase(session))
                return False
            resumed.set()
            return True

        with patch.object(CharacterServer, "_monster_chase_tick", tick):
            self.assertTrue(CharacterServer._start_monster_chase(session))
            worker = session.gameplay_chase_timer
            self.assertTrue(
                resumed.wait(2.0),
                "the start request was dropped with the retiring worker",
            )
        self.assertEqual(requests, [False])
        self.assertIs(session.gameplay_chase_timer, worker)

    def test_idle_retirement_honours_a_start_that_arrives_meanwhile(self):
        session = self.session
        calls = []
        resumed = threading.Event()

        def tick(tick_session, _generation):
            if tick_session is not session:
                return True
            calls.append(1)
            if len(calls) == 2:
                # The tick whose idleness completes the retirement period,
                # however soon after the first one the loop runs it.
                time.sleep(0.005)
                CharacterServer._start_monster_chase(session)
                return MONSTER_TICK_IDLE
            if len(calls) == 1:
                return MONSTER_TICK_IDLE
            resumed.set()
            return True

        with (
            patch.object(
                monster_chase_loop, "MONSTER_CHASE_IDLE_RETIRE_SECONDS", 0.001
            ),
            patch.object(
                monster_chase_loop, "MONSTER_CHASE_IDLE_INTERVAL_SECONDS", 0.01
            ),
            patch.object(CharacterServer, "_monster_chase_tick", tick),
        ):
            self.assertTrue(CharacterServer._start_monster_chase(session))
            self.assertTrue(resumed.wait(2.0), "idle retirement lost a start")

    def test_request_older_than_the_tick_does_not_keep_the_worker(self):
        session = self.session
        worker = _FixedRateChaseLoop(session, 0)
        session.gameplay_chase_timer = worker
        with session.flow_lock:
            worker.request_restart()
        with patch.object(
            CharacterServer,
            "_monster_chase_tick",
            lambda tick_session, _generation: tick_session is not session,
        ):
            worker.start()
            worker._thread.join(2.0)
        self.assertFalse(worker._thread.is_alive())
        self.assertIsNone(session.gameplay_chase_timer)


class OrphanedMapSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.combat = SimpleNamespace(
            player_summons={}, has_regenerating_players=False,
        )
        self.fighter = self._session(41950, 2002, spawned=True)
        self.bystander = self._session(41951, 2001, spawned=False)
        self.server = SimpleNamespace(
            sessions={
                session.addr: session
                for session in (self.fighter, self.bystander)
            }
        )

    def _session(self, port: int, character_id: int, *, spawned: bool):
        session = Session(("127.0.0.1", port), object())
        session.connected = True
        session.gameplay_mode = True
        session.gameplay_initialization_sent = True
        session.gameplay_presence_ready = True
        session.gameplay_map = MAP_ID
        session.gameplay_character = SimpleNamespace(character_id=character_id)
        session.gameplay_combat_state = self.combat
        session.gameplay_monsters_spawned = spawned
        return session

    def test_orphaned_instance_is_restarted_after_the_grace_period(self):
        supervise = CharacterServer._supervise_monster_worlds
        with patch.object(_FixedRateChaseLoop, "start"):
            self.assertEqual(supervise(self.server, now=100.0), ())
            self.assertEqual(supervise(self.server, now=100.5), ())
            self.assertIsNone(self.fighter.gameplay_chase_timer)
            events = supervise(self.server, now=101.5)
            self.assertEqual(
                [event["event"] for event in events],
                ["orphaned-map-restarted"],
            )
            self.assertEqual(events[0]["owner_character_id"], 2002)
            self.assertIsInstance(
                self.fighter.gameplay_chase_timer, _FixedRateChaseLoop
            )
            self.assertIsNone(self.bystander.gameplay_chase_timer)
            # The instance has a worker again; nothing more to do.
            self.assertEqual(supervise(self.server, now=110.0), ())

    def test_an_instance_with_a_worker_or_without_work_is_left_alone(self):
        supervise = CharacterServer._supervise_monster_worlds
        # A worker on a session that cannot be elected still counts:
        # starting a second one would double the simulation.
        joining = self._session(41952, 2000, spawned=True)
        joining.gameplay_presence_ready = False
        joining.gameplay_chase_timer = _FixedRateChaseLoop(joining, 0)
        self.server.sessions[joining.addr] = joining
        with patch.object(_FixedRateChaseLoop, "start"):
            self.assertEqual(supervise(self.server, now=100.0), ())
            self.assertEqual(supervise(self.server, now=105.0), ())
            self.assertIsNone(self.fighter.gameplay_chase_timer)
            del self.server.sessions[joining.addr]
            self.fighter.gameplay_monsters_spawned = False
            self.assertEqual(supervise(self.server, now=106.0), ())
            self.assertEqual(supervise(self.server, now=110.0), ())
            self.assertIsNone(self.fighter.gameplay_chase_timer)

    def test_watchdog_runs_the_supervisor_even_with_stall_reports_off(self):
        ran = threading.Event()

        def supervisor():
            ran.set()
            return ({"event": "orphaned-map-restarted", "map": MAP_ID},)

        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "monster-stalls.log"
            watchdog = StallWatchdog(
                threshold_seconds=0.0,
                interval_seconds=0.01,
                report=None,
                diagnostic_path=log,
                supervisor=supervisor,
                supervisor_interval_seconds=0.1,
            )
            watchdog.start()
            try:
                self.assertTrue(ran.wait(2.0))
                self.assertTrue(
                    _wait_for(
                        lambda: log.exists()
                        and "orphaned-map-restarted"
                        in log.read_text(encoding="utf-8")
                    )
                )
            finally:
                watchdog.stop()


if __name__ == "__main__":
    unittest.main()
