"""Regressions for the populated-map lag spikes.

A busy map used to stall for everyone whenever the one session that happened
to own the instance's simulation fell behind on reliable delivery, and target
selection re-derived the map's living-actor tuple once per candidate player
while holding the lock a player's own hit needs.
"""

from __future__ import annotations

from types import SimpleNamespace
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from dso_server.paths import (
    DEFAULT_STATIC_DB_PATH,
)
from dso_server.services.character_server import CharacterServer
from dso_server.tests.fixtures.characters import character_server
from dso_server.runtime.context import RELIABLE_BACKLOG_SOFT_LIMIT
from dso_server.runtime.gameplay_scheduler import GameplayDeadlineScheduler
from dso_server.runtime.transport import (
    RELIABLE_DATAGRAM_LIMIT,
    RakNetServer,
    Session,
)
from dso_server.protocol.gameplay_protocol import Move
from dso_server.gameplay.local_combat_state import (
    DamageResolution,
    LocalCombatState,
    PendingPlayerHit,
    VictimStatusEffectDefinition,
)
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog

MAP_ID = "b1201_pve"


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, address: tuple[str, int]) -> None:
        self.sent.append((data, address))


class SignalingLock:
    """Lock whose acquisition attempt can be observed by another thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.attempted = threading.Event()

    def acquire(self, *args, **kwargs):
        self.attempted.set()
        return self._lock.acquire(*args, **kwargs)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args) -> None:
        self.release()


class PopulatedMapSimulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sock = FakeSocket()
        self.scheduler = GameplayDeadlineScheduler(
            name="ten-player-combat-test", worker_count=2
        )
        self.scheduler.start()
        self.server = character_server(self)
        self.server.sock = self.sock
        self.encounter = load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH, MAP_ID, include_spawn_areas=False
        )
        self.assertIsNotNone(self.encounter)
        self.combat = LocalCombatState(
            self.encounter,
            player_actor_id=1,
            player_health=1000,
        )
        self.combat.activate_monsters(tuple(self.encounter.actor_ids))
        self.owner = self._session(40000, 1001)
        self.peer = self._session(40001, 1002)

    def tearDown(self) -> None:
        self.scheduler.shutdown(wait=True, cancel_pending=True)

    def _session(self, port: int, character_id: int) -> Session:
        import copy

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
            0.0,
            0.0,
            0.0,
            1900,
            0,
        )
        session.gameplay_presence_callback = (
            lambda value, event: self.server._handle_presence_event(
                value, event
            )
        )
        self.server.sessions[session.addr] = session
        return session

    @staticmethod
    def _saturate(session: Session) -> None:
        with session.pending_lock:
            session.pending_reliable.update(
                {
                    index: {"label": "busy"}
                    for index in range(RELIABLE_BACKLOG_SOFT_LIMIT)
                }
            )

    def _tick(self) -> bool:
        import time

        self.sock.sent.clear()
        self.owner.gameplay_chase_generation += 1
        # A real worker ticks at a fixed rate; without elapsed time nothing
        # advances and the test would pass for the wrong reason.
        self.owner.gameplay_chase_last_at = time.perf_counter() - 0.04
        return self.server._monster_chase_tick(
            self.owner, self.owner.gameplay_chase_generation
        )

    def _recipients(self) -> set:
        return {address for _packet, address in self.sock.sent}

    def test_separate_packs_receive_separate_player_targets_in_one_tick(self):
        first, second = self.encounter.actor_ids[:2]
        self.combat.monster_positions[first] = (0.0, 0.0, 0.0)
        self.combat.monster_positions[second] = (100.0, 0.0, 0.0)
        for viewer, actor, position in ((self.owner, first, (1.0, 0.0, 0.0)),
                                         (self.peer, second, (101.0, 0.0, 0.0))):
            viewer.gameplay_spawned_monster_actor_ids = {actor}
            viewer.gameplay_last_move = Move.from_values(
                self.encounter.coordinate_frame.world_to_wire(position), 0, 0, 0, 1900, 0)
        with patch.object(self.combat, "advance_monsters", return_value=()) as movement, \
             patch.object(self.combat, "maybe_start_monster_attacks", return_value=()) as attacks:
            self.assertTrue(self._tick())
        self.assertEqual(movement.call_count, 1)
        targets = movement.call_args.kwargs["targets_by_actor"]
        self.assertEqual(targets[first][0], self.server._remote_actor_id(self.owner.gameplay_character.character_id))
        self.assertEqual(targets[second][0], self.server._remote_actor_id(self.peer.gameplay_character.character_id))
        self.assertIs(attacks.call_args.kwargs["targets_by_actor"], targets)

    def test_a_backlogged_owner_does_not_freeze_the_map(self):
        """The instance keeps serving every other player.

        Reliable backpressure is per client. Skipping the whole tick because
        the owning session was behind stopped monster updates for everyone
        else on the map, which is the "monsters stop updating" spike.
        """

        self._saturate(self.owner)

        self.assertTrue(self._tick(), "the simulation worker must keep running")

        self.assertIn(
            self.peer.addr,
            self._recipients(),
            "the unaffected peer received no monster activity",
        )

    def test_failed_post_commit_publication_does_not_freeze_the_map(self):
        """A publication error after the tick committed keeps the fanout.

        A socket proc raising on every turret hit aborted the tick before
        ``monster-activity-fanout``; the loop kept ticking but no viewer
        received monster movement or attacks again (2026-10-27 live report).
        """

        from dso_server.services import monster_combat

        monster_combat._post_commit_failure_logged_at.clear()
        with patch.object(
            type(self.server), "_dispatch_player_owned_resolutions",
            side_effect=AttributeError("publication failed"),
        ) as dispatch, patch("builtins.print") as log:
            self.assertTrue(self._tick())
            self.assertIn(self.peer.addr, self._recipients(),
                          "monster activity was not published after the failure")
            self.assertTrue(self._tick())

        self.assertEqual(dispatch.call_count, 2)
        reports = [call for call in log.call_args_list
                   if "player-owned-resolutions" in str(call)]
        self.assertEqual(len(reports), 1, "failure must be logged, rate-limited")
        self.assertIn("AttributeError", str(reports[0]))

    def test_a_backlogged_peer_is_skipped_but_the_map_continues(self):
        self._saturate(self.peer)

        self.assertTrue(self._tick())

        recipients = self._recipients()
        self.assertIn(self.owner.addr, recipients)
        self.assertNotIn(
            self.peer.addr,
            recipients,
            "a backlogged viewer must not be sent optional simulation traffic",
        )

    def test_a_lone_backlogged_session_still_pauses(self):
        """Without peers the per-viewer filter has nothing to fall back on."""

        self.server.sessions.pop(self.peer.addr)
        self.peer.connected = False
        self.owner.gameplay_presence_callback = None
        self._saturate(self.owner)

        self.assertTrue(self._tick())

        self.assertEqual(
            self.sock.sent,
            [],
            "a lone backlogged session must not keep producing packets",
        )

    def test_target_selection_cost_does_not_scale_with_player_count(self):
        """Guards the O(players x monsters) tuple rebuild that was removed.

        ``living_actor_ids`` walks the whole encounter on every read. Reading
        it per candidate player, inside the shared combat lock, is what made a
        crowded map delay unrelated players' hits.
        """

        original = type(self.combat).living_actor_ids.fget

        def count_reads() -> int:
            reads = []

            def counting(combat_state):
                reads.append(1)
                return original(combat_state)

            with patch.object(
                type(self.combat), "living_actor_ids", property(counting)
            ):
                self._tick()
            return len(reads)

        baseline = count_reads()
        for index in range(8):
            self._session(40100 + index, 2000 + index)
        crowded = count_reads()

        self.assertEqual(
            crowded,
            baseline,
            "living_actor_ids reads grew with the number of players "
            f"({baseline} -> {crowded})",
        )

    def test_tick_takes_a_bounded_number_of_locks_per_viewer(self):
        """The map worker must not sweep every viewer's lock repeatedly.

        This thread is the whole map's simulation, and it takes each viewer's
        ``flow_lock`` on that map's behalf. Every additional pass multiplies
        the map's exposure to one player's lock hold by the player count, so
        the per-viewer acquisition count is the property worth pinning -- not a
        timing. Visibility and periodic socket auras used to be two separate
        sweeps; the victim gates added three more feature queries each.
        """

        counts = {}

        def counting_lock(session):
            real = session.flow_lock

            class Counting:
                def acquire(self, *args, **kwargs):
                    acquired = real.acquire(*args, **kwargs)
                    if acquired:
                        counts[session.addr] = counts.get(session.addr, 0) + 1
                    return acquired

                def release(self):
                    real.release()

                def __enter__(self):
                    self.acquire()
                    return self

                def __exit__(self, *_args):
                    self.release()

            return real, Counting()

        peers = [self._session(40300 + index, 4000 + index) for index in range(3)]
        viewers = [self.owner, self.peer, *peers]
        restore = []
        try:
            for viewer in viewers:
                real, counting = counting_lock(viewer)
                restore.append((viewer, real))
                viewer.flow_lock = counting
            self._tick()
        finally:
            for viewer, real in restore:
                viewer.flow_lock = real

        # The owner legitimately does more: it also carries the tick clock
        # snapshot/commit and the presence fan-out for the whole map.
        for viewer in peers:
            with self.subTest(viewer.addr):
                self.assertLessEqual(
                    counts.get(viewer.addr, 0),
                    6,
                    "the map worker acquired one viewer's session lock "
                    f"{counts.get(viewer.addr, 0)} times in a single tick",
                )

    def test_grouped_pvp_players_with_summon_do_not_deadlock_tick(self):
        """Presence hostility must be resolved before simulation locks.

        The summon cycle asks presence whether mutually PvP-enabled players
        are group allies. Presence takes the source session's non-reentrant
        flow lock. Doing that while the monster tick already held the owner
        flow lock permanently froze gameplay without terminating the process.
        """

        owner_actor_id = self.server._remote_actor_id(
            self.owner.gameplay_character.character_id
        )
        peer_actor_id = self.server._remote_actor_id(
            self.peer.gameplay_character.character_id
        )
        self.owner.gameplay_remote_actor_id = owner_actor_id
        self.peer.gameplay_remote_actor_id = peer_actor_id
        self.owner.gameplay_pvp_enabled = True
        self.peer.gameplay_pvp_enabled = True
        with sqlite3.connect(
            f'file:{DEFAULT_STATIC_DB_PATH.as_posix()}?mode=ro', uri=True
        ) as database:
            skill_index = database.execute(
                'SELECT rowid - 1 FROM _Template_Skill WHERE Id = ?',
                ('TurretMachineGun',),
            ).fetchone()[0]
        definition = self.combat.player_summon_definition(skill_index)
        self.assertIsNotNone(definition)
        with self.combat.lock:
            self.combat.spawn_player_summon(
                definition,
                owner_actor_id=owner_actor_id,
                position=self.encounter.trigger_center,
                yaw=0.0,
                level=100,
                server_tick=500,
                player_damage_range=(100.0, 200.0),
                now=time.perf_counter(),
            )

        result = []
        failures = []

        def run_tick():
            try:
                result.append(self._tick())
            except BaseException as exc:  # preserve worker diagnostics
                failures.append(exc)

        group = SimpleNamespace(group_id=77)
        with patch.object(
            self.server.social_store, "group_for", return_value=group
        ):
            worker = threading.Thread(target=run_tick, daemon=True)
            worker.start()
            worker.join(timeout=2.0)

        self.assertFalse(
            worker.is_alive(),
            "monster tick deadlocked in grouped summon PvP hostility",
        )
        if failures:
            raise failures[0]
        self.assertEqual(result, [True])

    def test_pvp_hostility_presence_query_is_reentrant_safe(self):
        """A caller already snapshotting its session may query group immunity."""

        self.owner.gameplay_pvp_enabled = True
        self.peer.gameplay_pvp_enabled = True
        result = []

        def query_while_snapshotting():
            with self.owner.flow_lock:
                result.append(self.owner.gameplay_presence_callback(
                    self.owner, ("pvp_hostile", self.peer)
                ))

        group = SimpleNamespace(group_id=77)
        with patch.object(
            self.server.social_store, "group_for", return_value=group
        ):
            worker = threading.Thread(
                target=query_while_snapshotting, daemon=True
            )
            worker.start()
            worker.join(timeout=2.0)

        self.assertFalse(
            worker.is_alive(),
            "presence PvP query re-entered the source flow lock",
        )
        self.assertEqual(result, [False])

    def test_instant_command_aura_reaches_only_same_instance_group_allies(self):
        from dso_server.protocol.consumable_protocol import load_instant_consumable_definition

        self.owner.gameplay_map_access_group_id = 'e_doe_map_02_white'
        self.peer.gameplay_map_access_group_id = 'e_doe_map_02_white'
        other_group = self._session(40002, 1003)
        other_group.gameplay_map_access_group_id = 'e_doe_map_02_white'
        other_variant = self._session(40003, 1004)
        other_variant.gameplay_map_access_group_id = 'e_doe_map_02_purple'
        groups = {
            1001: SimpleNamespace(group_id=77),
            1002: SimpleNamespace(group_id=77),
            1003: SimpleNamespace(group_id=88),
            1004: SimpleNamespace(group_id=77),
        }
        definition = load_instant_consumable_definition(
            DEFAULT_STATIC_DB_PATH, 'pvp_command_buff'
        )
        effect_ids = load_status_runtime_catalog(DEFAULT_STATIC_DB_PATH).effect_ids
        receiver_effect_id = effect_ids['pvp_protection_aura_effect']

        with patch.object(
            self.server.social_store,
            'group_for',
            side_effect=lambda character_id: groups.get(character_id),
        ):
            applied = self.server._apply_instant_consumable_effect(
                self.owner,
                definition,
                database_path=DEFAULT_STATIC_DB_PATH,
            )

        self.assertTrue(applied)
        owner_receiver_ids = tuple(
            instance_id
            for instance_id, (entry, _expires_at)
            in self.owner.gameplay_runtime_status_effects.items()
            if entry.effect_id == receiver_effect_id
        )
        peer_receiver_ids = tuple(
            instance_id
            for instance_id, (entry, _expires_at)
            in self.peer.gameplay_runtime_status_effects.items()
            if entry.effect_id == receiver_effect_id
        )
        self.assertEqual(len(owner_receiver_ids), 1)
        self.assertEqual(len(peer_receiver_ids), 1)
        self.assertNotIn(
            effect_ids['pvp_command_aura'],
            tuple(
                entry.effect_id
                for entry, _expires_at
                in self.peer.gameplay_runtime_status_effects.values()
            ),
        )
        self.assertTrue(any(
            key == ('player-status-tick', peer_receiver_ids[0])
            for key in self.peer.gameplay_projectile_timers
        ))
        self.assertEqual(other_group.gameplay_runtime_status_effects, {})
        self.assertEqual(other_variant.gameplay_runtime_status_effects, {})

    def test_periodic_status_tick_releases_flow_before_combat_lock(self):
        """A blocked heal mutation must not monopolize gameplay flow state."""

        lock = SignalingLock()

        class Combat:
            player_maximum_health = 100
            player_health = {}

            def __init__(self):
                self.lock = lock

            def heal_player_relative(self, actor_id, _fraction):
                self.player_health[actor_id] = [60, 100]
                return 60, 100

        combat = Combat()
        session = self.owner
        session.gameplay_combat_state = combat
        session.gameplay_chase_generation = 7
        session.gameplay_inventory = SimpleNamespace(health_points=50.0)
        instance_id = 9001
        timer_key = ("player-status-tick", instance_id)
        session.gameplay_runtime_status_effects[instance_id] = (
            SimpleNamespace(), time.perf_counter() + 10.0
        )
        session.gameplay_projectile_timers[timer_key] = SimpleNamespace()

        failures = []

        def tick():
            try:
                self.server._tick_player_status_effect(
                    session, 7, timer_key, instance_id, 1.0, 0.1, 1
                )
            except BaseException as exc:
                failures.append(exc)

        lock.acquire()
        lock.attempted.clear()
        worker = threading.Thread(target=tick, daemon=True)
        worker.start()
        self.assertTrue(lock.attempted.wait(timeout=1.0))
        flow_available = session.flow_lock.acquire(timeout=0.5)
        if flow_available:
            session.flow_lock.release()
        lock.release()
        worker.join(timeout=2.0)

        self.assertTrue(
            flow_available,
            "periodic status tick held flow_lock while waiting for combat",
        )
        self.assertFalse(worker.is_alive())
        if failures:
            raise failures[0]

    def test_monster_tick_does_not_hold_flow_while_waiting_for_combat(self):
        """A busy map lock must not freeze the owning gameplay session."""

        combat_lock = SignalingLock()
        original_lock = self.combat.lock
        failures = []

        def tick():
            try:
                self._tick()
            except BaseException as exc:
                failures.append(exc)

        self.combat.lock = combat_lock
        combat_lock.acquire()
        combat_lock.attempted.clear()
        worker = threading.Thread(target=tick, daemon=True)
        try:
            worker.start()
            self.assertTrue(combat_lock.attempted.wait(timeout=1.0))
            flow_available = self.owner.flow_lock.acquire(timeout=0.5)
            if flow_available:
                self.owner.flow_lock.release()
        finally:
            combat_lock.release()
            worker.join(timeout=2.0)
            self.combat.lock = original_lock

        self.assertTrue(
            flow_available,
            "monster tick held flow_lock while waiting for combat.lock",
        )
        self.assertFalse(worker.is_alive())
        if failures:
            raise failures[0]

    def test_monster_tick_does_not_hold_combat_while_waiting_for_flow(self):
        """A busy session lock must not block unrelated map mutations."""

        flow_lock = SignalingLock()
        original_lock = self.owner.flow_lock
        failures = []

        def tick():
            try:
                self._tick()
            except BaseException as exc:
                failures.append(exc)

        self.owner.flow_lock = flow_lock
        flow_lock.acquire()
        flow_lock.attempted.clear()
        worker = threading.Thread(target=tick, daemon=True)
        try:
            worker.start()
            self.assertTrue(flow_lock.attempted.wait(timeout=1.0))
            combat_available = self.combat.lock.acquire(timeout=0.5)
            if combat_available:
                self.combat.lock.release()
        finally:
            flow_lock.release()
            worker.join(timeout=2.0)
            self.owner.flow_lock = original_lock

        self.assertTrue(
            combat_available,
            "monster tick held combat.lock while waiting for flow_lock",
        )
        self.assertFalse(worker.is_alive())
        if failures:
            raise failures[0]

    def test_monster_tick_does_not_deepcopy_immutable_moves(self):
        """Fan-out retains the frozen Move snapshot instead of copying it."""

        import copy

        original = copy.deepcopy

        def reject_move(value, *args, **kwargs):
            if isinstance(value, Move):
                raise AssertionError(
                    "monster tick deep-copied immutable Move state"
                )
            return original(value, *args, **kwargs)

        with patch("copy.deepcopy", side_effect=reject_move):
            self.assertTrue(self._tick())

    def test_tick_reconciles_simulation_to_spawned_viewer_union(self):
        first_actor, untouched_actor = self.encounter.actor_ids[:2]
        self.owner.gameplay_spawned_monster_actor_ids = {first_actor}
        self.owner.gameplay_announced_monster_actor_ids = {first_actor}
        self.peer.gameplay_spawned_monster_actor_ids = set()
        self.peer.gameplay_announced_monster_actor_ids = set()
        untouched_position = self.combat.monster_positions[untouched_actor]
        untouched_health = self.combat.monster_health[untouched_actor]

        self.assertTrue(self._tick())

        self.assertEqual(
            self.combat.active_monster_actor_ids,
            {first_actor},
        )
        self.assertEqual(
            self.combat.monster_positions[untouched_actor],
            untouched_position,
        )
        self.assertEqual(
            self.combat.monster_health[untouched_actor],
            untouched_health,
            "deactivation must not reset or revive shared map state",
        )

    def test_combat_fanout_reuses_identical_raw_payload_encoding(self):
        """Viewers keep independent transport/state with shared wire payloads."""

        from dso_server.services import combat_resolution as protocol

        actor_id = int(self.encounter.actor_ids[0])
        resolution = DamageResolution(
            target_actor_id=actor_id,
            attacker_actor_id=900_001,
            current_health=900,
            maximum_health=1000,
            damage=100,
            killed=False,
            position=self.encounter.trigger_center,
            server_tick=2000,
            hit_tags=(3,),
            skill_index=1857,
            victim_status_effects=(
                VictimStatusEffectDefinition(
                    resource_id="debuff_dot_burn_triggerExplosion",
                    effect_id=4717,
                    chance=1.0,
                    duration_ticks=125,
                    values=(),
                    trigger="hit",
                ),
            ),
            source_actor_id=900_001,
        )
        packet_cache = {}
        owner_next_id = (
            self.owner.gameplay_next_status_effect_instance_id
        )
        peer_next_id = self.peer.gameplay_next_status_effect_instance_id
        with patch.object(
            protocol,
            "encode_combat_batch",
            wraps=protocol.encode_combat_batch,
        ) as encode_combat:
            self.server._send_combat_resolutions(
                self.owner,
                (resolution,),
                label="cache-regression",
                packet_cache=packet_cache,
            )
            self.server._send_combat_resolutions(
                self.peer,
                (resolution,),
                label="cache-regression",
                packet_cache=packet_cache,
            )

        self.assertEqual(encode_combat.call_count, 1)
        self.assertEqual(len(packet_cache), 1)
        self.assertEqual(len(self.sock.sent), 2)
        self.assertEqual(
            {address for _packet, address in self.sock.sent},
            {self.owner.addr, self.peer.addr},
        )
        self.assertEqual(self.sock.sent[0][0], self.sock.sent[1][0])

        effect_key = (actor_id, 4717)
        owner_effect = (
            self.owner.gameplay_victim_status_effect_instances[effect_key]
        )
        peer_effect = (
            self.peer.gameplay_victim_status_effect_instances[effect_key]
        )
        self.assertIsNot(
            self.owner.gameplay_victim_status_effect_instances,
            self.peer.gameplay_victim_status_effect_instances,
        )
        self.assertIsNot(owner_effect, peer_effect)
        self.assertIsNot(owner_effect[0], peer_effect[0])
        self.assertEqual(owner_effect[0], peer_effect[0])
        self.assertEqual(
            self.owner.gameplay_next_status_effect_instance_id,
            owner_next_id + 1,
        )
        self.assertEqual(
            self.peer.gameplay_next_status_effect_instance_id,
            peer_next_id + 1,
        )
        self.assertEqual(len(self.owner.pending_reliable), 1)
        self.assertEqual(len(self.peer.pending_reliable), 1)
        owner_pending = next(iter(self.owner.pending_reliable.values()))
        peer_pending = next(iter(self.peer.pending_reliable.values()))
        self.assertIsNot(owner_pending, peer_pending)
        self.assertEqual(owner_pending["data"], peer_pending["data"])

    def test_player_owned_tick_batches_presence_scan_and_packets(self):
        """Fifteen DoT/turret hits are one owner transaction per viewer."""

        source_actor_id = self.server._remote_actor_id(
            self.owner.gameplay_character.character_id
        )
        fixture_actor_ids = tuple(self.encounter.actor_ids)
        self.assertTrue(fixture_actor_ids)
        actor_ids = tuple(
            fixture_actor_ids[index % len(fixture_actor_ids)]
            for index in range(15)
        )
        resolutions = tuple(
            DamageResolution(
                target_actor_id=int(actor_id),
                attacker_actor_id=source_actor_id,
                current_health=900,
                maximum_health=1000,
                damage=100,
                killed=False,
                position=self.encounter.trigger_center,
                server_tick=2200,
                hit_tags=(5,),
                skill_index=1857,
                victim_status_effects=(),
                source_actor_id=source_actor_id,
                periodic_damage=True,
            )
            for actor_id in actor_ids
        )
        self.sock.sent.clear()

        with (
            patch.object(
                self.server,
                "_active_gameplay_sessions",
                wraps=self.server._active_gameplay_sessions,
            ) as presence_scans,
            patch.object(
                CharacterServer,
                "_apply_player_damage_healing",
                return_value=0,
            ) as healing,
        ):
            self.server._dispatch_player_owned_resolutions(
                self.combat,
                {source_actor_id: self.owner},
                resolutions,
            )

        self.assertEqual(presence_scans.call_count, 1)
        self.assertEqual(healing.call_count, 1)
        self.assertEqual(healing.call_args.args[1], resolutions)
        # Fifteen hits overflow one datagram, so the transport fragments the
        # batch: count ordered messages, not datagrams.
        messages = {
            (address, frame["order_idx"])
            for data, address in self.sock.sent
            for frame in RakNetServer._parse_frames(data)
        }
        self.assertEqual(
            len(messages),
            2,
            "one combat batch should be sent to each of two viewers",
        )
        self.assertTrue(
            all(
                len(data) <= RELIABLE_DATAGRAM_LIMIT
                for data, _address in self.sock.sent
            )
        )
        for viewer in (self.owner, self.peer):
            self.assertEqual(
                viewer.reliable_backlog_size(),
                sum(
                    1 for _data, address in self.sock.sent
                    if address == viewer.addr
                ),
            )

    def test_player_owned_tick_batches_without_presence(self):
        """The standalone path also emits one combat transaction per tick."""

        source_actor_id = self.server._remote_actor_id(
            self.owner.gameplay_character.character_id
        )
        self.owner.gameplay_presence_callback = None
        fixture_actor_ids = tuple(self.encounter.actor_ids)
        actor_ids = tuple(
            fixture_actor_ids[index % len(fixture_actor_ids)]
            for index in range(8)
        )
        resolutions = tuple(
            DamageResolution(
                target_actor_id=int(actor_id),
                attacker_actor_id=source_actor_id,
                current_health=900,
                maximum_health=1000,
                damage=100,
                killed=False,
                position=self.encounter.trigger_center,
                server_tick=2300,
                hit_tags=(),
                skill_index=1857,
                victim_status_effects=(),
                source_actor_id=source_actor_id,
            )
            for actor_id in actor_ids
        )
        self.sock.sent.clear()

        with patch.object(
            CharacterServer,
            "_apply_player_damage_healing",
            return_value=0,
        ) as healing:
            self.server._dispatch_player_owned_resolutions(
                self.combat,
                {source_actor_id: self.owner},
                resolutions,
            )

        self.assertEqual(healing.call_count, 1)
        self.assertEqual(len(self.sock.sent), 1)
        self.assertEqual(self.owner.reliable_backlog_size(), 1)

    def test_ten_player_projectile_burst_has_no_lost_impacts(self):
        """Exercise scheduler -> shared world -> ten-viewer reliable fanout.

        This is intentionally a loss/latency guard rather than a fragile
        microsecond benchmark.  Ten players each land ten independently timed
        projectiles while two fixed scheduler workers serialize mutations on
        one map.  Every source must receive every authoritative Hit even after
        its reliable backlog crosses the optional-traffic soft limit.
        """

        for index in range(8):
            self._session(40200 + index, 3000 + index)
        players = tuple(self.server.sessions.values())
        self.assertEqual(len(players), 10)
        targets = tuple(self.combat.monsters.values())
        self.assertTrue(targets)

        callbacks = []
        scheduled_at = time.perf_counter()
        due_at = scheduled_at + 0.025
        impacts_per_player = 10
        with self.combat.lock:
            for player_index, session in enumerate(players):
                session.gameplay_chase_generation = 1
                source_actor_id = self.server._remote_actor_id(
                    session.gameplay_character.character_id
                )
                for cast_index in range(impacts_per_player):
                    cast_id = 800_000 + player_index * 100 + cast_index
                    target = targets[
                        (player_index + cast_index) % len(targets)
                    ]
                    timer_key = (source_actor_id, cast_id, 0)
                    self.combat.pending_runtime_player_hits[timer_key] = (
                        PendingPlayerHit(
                            cast_id=cast_id,
                            target_actor_id=target.actor_id,
                            skill_index=1857,
                            due_at=due_at,
                            impact_tick=2500 + cast_index,
                            distance=1.0,
                            travel_distance=1.0,
                            emit_delay=0.0,
                            victim_status_effects=(),
                            source_actor_id=source_actor_id,
                            impact_index=0,
                            motion_type="Area",
                            damage_modifier=1.0,
                            player_minimum_damage=1.0,
                            player_maximum_damage=1.0,
                        )
                    )
                    timer = session.gameplay_deadline_timer(
                        max(0.0, due_at - time.perf_counter()),
                        self.server._resolve_player_projectile,
                        args=(session, 1, cast_id, source_actor_id, 0),
                    )
                    session.gameplay_projectile_timers[timer_key] = timer
                    callbacks.append(timer)

        started_at = time.perf_counter()
        for callback in callbacks:
            callback.start()
        deadline = started_at + 10.0
        for callback in callbacks:
            callback.join(max(0.0, deadline - time.perf_counter()))
        elapsed = time.perf_counter() - started_at

        unfinished = [callback for callback in callbacks if callback.is_alive()]
        self.assertFalse(
            unfinished,
            f"{len(unfinished)} projectile callbacks unfinished after "
            f"{elapsed:.3f}s",
        )
        with self.combat.lock:
            self.assertFalse(self.combat.pending_runtime_player_hits)
        self.assertEqual(self.scheduler.pending_count, 0)
        self.assertEqual(
            len(self.scheduler.threads),
            2,
            "projectile load must not grow one thread per impact",
        )
        expected_hits_per_viewer = len(players) * impacts_per_player
        for session in players:
            self.assertEqual(
                session.reliable_backlog_size(),
                expected_hits_per_viewer,
                f"viewer {session.addr} lost a critical Hit transaction",
            )


if __name__ == "__main__":
    unittest.main()
