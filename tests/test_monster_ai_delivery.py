"""Monster target ownership, navigation recovery, and loss-safe replication."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dso_server.tests import test_local_combat_state as fixtures
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import _DirectNavigationMesh
from dso_server.protocol.gameplay_protocol import Move, MoveServerCommand, decode_move_batch
from dso_server.runtime.transport import Session, RakNetServer
from dso_server.services.base import CharacterService
from dso_server.services import world
from dso_server.runtime.context import (
    RELIABLE_BACKLOG_SOFT_LIMIT,
    MONSTER_MOVEMENT_PACKET_BUDGET,
)


class FakeSocket:
    def __init__(self):
        self.sent = []

    def sendto(self, packet, address):
        self.sent.append((packet, address))


class MonsterDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.socket = FakeSocket()
        self.session = Session(("127.0.0.1", 40000), self.socket)

    @staticmethod
    def command(actor=101, tick=100, moving=True):
        return MoveServerCommand(Move.from_values(
            (float(tick), 0.0, 0.0), 1.0 if moving else 0.0,
            0.0, 0.0, tick, 4 if moving else 0), actor)

    def send(self, commands=(), combat=None):
        CharacterService._send_monster_movement_batches(self.session, commands, combat=combat)

    def frames(self):
        return [RakNetServer._parse_frames(packet)[0] for packet, _addr in self.socket.sent]

    def test_disjoint_stop_batches_are_sequenced_and_a_lost_batch_is_repeated(self):
        count = MONSTER_MOVEMENT_PACKET_BUDGET + 1
        stops = tuple(self.command(actor=101 + i, moving=False) for i in range(count))
        self.session.gameplay_spawned_monster_actor_ids.update(
            command.actor_id for command in stops
        )
        self.send(stops)
        frames = self.frames()
        self.assertEqual(len(frames), 2)
        # Retail-style: stops never enter the reliable-ordered stream.
        self.assertTrue(all(f["reliability"] == 1 for f in frames))
        self.assertFalse(self.session.pending_reliable)
        self.assertEqual(len(self.session.gameplay_monster_stop_repeats), count)
        # The first datagram is lost; its stops come again once due.
        for _command, deadlines in self.session.gameplay_monster_stop_repeats.values():
            deadlines[0] = 0.0
        self.session.gameplay_monster_stop_repeat_next_at = 0.0
        self.assertEqual(CharacterService._send_monster_stop_repeats(self.session, None), count)
        repeated = self.frames()[2:]
        self.assertEqual([f["payload"] for f in repeated], [f["payload"] for f in frames])
        self.assertTrue(all(f["reliability"] == 1 for f in repeated))
        self.assertTrue(all(len(packet) < 1200 for packet, _addr in self.socket.sent))

    def test_busy_socket_retains_latest_stop_without_waiting_or_allocating_frames(self):
        with self.session.outbound_lock:
            self.send((self.command(tick=100),))
            self.send((self.command(tick=104),))
            self.send((self.command(tick=105, moving=False),))
        self.assertFalse(self.socket.sent)
        self.assertFalse(self.session.pending_reliable)
        self.assertEqual(len(self.session.gameplay_pending_monster_movements), 1)
        self.send()
        frame = self.frames()[0]
        commands = decode_move_batch(frame["payload"], frame["bit_length"]).commands
        self.assertEqual(commands[0].value.start_tick, 105)
        self.assertEqual(commands[0].value.scalar_byte, 0)
        self.assertFalse(self.session.gameplay_pending_monster_movements)

    def test_backpressure_coalesces_per_actor_and_recovers_without_new_movement(self):
        self.session.pending_reliable.update({i: {} for i in range(RELIABLE_BACKLOG_SOFT_LIMIT)})
        for tick in range(100, 140):
            self.send((self.command(101, tick), self.command(202, tick, False)))
        self.assertFalse(self.socket.sent)
        self.assertEqual(len(self.session.gameplay_pending_monster_movements), 2)
        self.session.pending_reliable.clear()
        self.send()
        commands = tuple(command for frame in self.frames()
                         for command in decode_move_batch(frame["payload"], frame["bit_length"]).commands)
        self.assertEqual({c.actor_id for c in commands}, {101, 202})
        self.assertTrue(all(c.value.start_tick == 139 for c in commands))

    def test_discarded_actor_is_removed_from_deferred_delivery(self):
        fixture = fixtures.LocalCombatStateTests()
        fixture.setUp()
        combat = fixture.state
        actor = combat.encounter.actor_ids[0]
        self.session.gameplay_combat_state = combat
        self.session.gameplay_spawned_monster_actor_ids.add(actor)
        with self.session.outbound_lock:
            self.send((self.command(actor),), combat=combat)
        self.session.gameplay_spawned_monster_actor_ids.clear()
        self.send(combat=combat)
        self.assertFalse(self.socket.sent)
        self.assertFalse(self.session.gameplay_pending_monster_movements)

    def test_attack_transform_supersedes_an_older_deferred_move(self):
        fixture = fixtures.LocalCombatStateTests()
        fixture.setUp()
        combat = fixture.state
        actor = combat.encounter.actor_ids[0]
        self.session.gameplay_combat_state = combat
        self.session.gameplay_spawned_monster_actor_ids.add(actor)
        with self.session.outbound_lock:
            self.send((self.command(actor, 100),), combat=combat)
        CharacterService._record_delivered_monster_movements(
            self.session, combat, (self.command(actor, 104, False),))
        self.send(combat=combat)
        self.assertFalse(self.socket.sent)


class MonsterAITests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.LocalCombatStateTests()
        fixture.setUp()
        encounter = replace(fixture.encounter, navigation=_DirectNavigationMesh(), terrain=None)
        self.combat = LocalCombatState(encounter, player_actor_id=1, player_health=1000)
        self.actor = encounter.actor_ids[0]
        self.monster = replace(self.combat.monsters[self.actor], run_speed=1.0, travel_distance=100.0)
        self.combat.monsters[self.actor] = self.monster
        self.home = self.monster.position
        self.wire = encounter.coordinate_frame.world_to_wire

    def test_each_pack_selects_its_own_visible_living_player(self):
        first, second = self.combat.encounter.actor_ids
        self.combat.monster_positions[first] = (0.0, 0.0, 0.0)
        self.combat.monster_positions[second] = (100.0, 0.0, 0.0)
        self.combat.register_player(101, 100)
        self.combat.register_player(202, 100)
        candidates = ((101, (1.0, 0.0, 0.0), {first}),
                      (202, (101.0, 0.0, 0.0), {second}))
        targets = self.combat.assign_monster_targets((first, second), candidates)
        self.assertEqual(targets[first][0], 101)
        self.assertEqual(targets[second][0], 202)
        self.combat.player_health[101][0] = 0
        targets = self.combat.assign_monster_targets((first, second), candidates)
        self.assertNotIn(first, targets)
        self.assertEqual(targets[second][0], 202)

    def test_return_home_longer_than_four_seconds_keeps_walking(self):
        self.combat.monster_positions[self.actor] = (self.home[0] + 20, *self.home[1:])
        self.combat.monster_health[self.actor] = 1
        self.combat._set_returning_home(self.actor, True, 100, now=100.0)
        for index in range(1, 41):
            previous = self.combat.monster_positions[self.actor]
            self.combat.advance_monsters(self.wire(self.home), elapsed_seconds=0.25,
                server_tick=100 + index * 6, now=100 + index * 0.25, eligible_actor_ids={self.actor})
            current = self.combat.monster_positions[self.actor]
            self.assertLessEqual(self.combat._distance(previous, current), 0.25001)
        self.assertIn(self.actor, self.combat.returning_home_actor_ids)
        self.assertEqual(self.combat.monster_health[self.actor], 1)
        self.assertAlmostEqual(self.combat._distance(current, self.home), 10.0)
        for index in range(41, 85):
            self.combat.advance_monsters(self.wire(self.home), elapsed_seconds=0.25,
                server_tick=100 + index * 6, now=100 + index * 0.25, eligible_actor_ids={self.actor})
        self.assertNotIn(self.actor, self.combat.returning_home_actor_ids)
        self.assertEqual(self.combat.monster_health[self.actor], self.monster.maximum_health)

    def test_visibility_pause_preserves_return_state_without_timing_out(self):
        start = (self.home[0] + 10, *self.home[1:])
        self.combat.monster_positions[self.actor] = start
        self.combat._set_returning_home(self.actor, True, 100, now=100.0)
        self.combat.deactivate_monsters((self.actor,), reset=False)
        self.assertIn(self.actor, self.combat.returning_home_actor_ids)
        self.combat.activate_monsters((self.actor,))
        self.combat.advance_monsters(self.wire(self.home), elapsed_seconds=0.04,
            server_tick=200, now=1000.0, eligible_actor_ids={self.actor})
        self.assertIn(self.actor, self.combat.returning_home_actor_ids)
        self.assertLessEqual(self.combat._distance(start, self.combat.monster_positions[self.actor]), 0.04001)

    def test_repeated_activation_does_not_erase_publish_history(self):
        self.combat.monster_last_movement_publish_ticks[self.actor] = 123
        self.combat.activate_monsters((self.actor,))
        self.assertEqual(self.combat.monster_last_movement_publish_ticks[self.actor], 123)

    def test_cached_path_keeps_moving_when_refresh_budget_is_exhausted(self):
        goal = (self.home[0] + 10, *self.home[1:])
        self.combat._route_refresh_budget = 1
        first = self.combat._advance_on_cached_route(self.actor, self.home, goal, 0.1, 100,
            home=self.home, territory_radius=100)
        second = self.combat._advance_on_cached_route(self.actor, first, goal, 0.1, 200,
            home=self.home, territory_radius=100)
        self.assertGreater(self.combat._distance(self.home, second), self.combat._distance(self.home, first))

    def test_actor_creation_precedes_visibility_to_the_map_worker(self):
        session = Session(("127.0.0.1", 40000), FakeSocket())
        session.gameplay_combat_state = self.combat
        session.gameplay_monster_encounter = self.combat.encounter
        session.gameplay_announced_monster_actor_ids.add(self.actor)
        session.gameplay_last_move = Move.from_values(self.wire(self.home), 0, 0, 0, 100, 0)
        original = session.send_reliable_ordered
        observed = []
        def send(payload, **kwargs):
            if kwargs.get("label") == "monster-spawn-batch":
                observed.append(self.actor in session.gameplay_spawned_monster_actor_ids)
            return original(payload, **kwargs)
        with patch.object(world, "decode_simple_gameplay_command", return_value=SimpleNamespace(value=self.actor)), \
             patch.object(CharacterService, "_accept_gameplay_state_command"), \
             patch.object(CharacterService, "_start_monster_chase"), \
             patch.object(session, "send_reliable_ordered", side_effect=send):
            CharacterService._handle_actor_request(session, b"")
        self.assertEqual(observed, [False])
        self.assertIn(self.actor, session.gameplay_spawned_monster_actor_ids)

    def test_old_player_move_cannot_rewind_monster_publish_clock(self):
        session = Session(("127.0.0.1", 40000), FakeSocket())
        session.gameplay_chase_server_tick = 105
        move = Move.from_values((0, 0, 0), 0, 0, 0, 100, 0)
        with patch.object(world, "decode_move", return_value=move), \
             patch.object(CharacterService, "_player_victim_feature_locked", return_value=False), \
             patch.object(CharacterService, "_accept_gameplay_state_command"), \
             patch.object(CharacterService, "_cull_distant_monster_groups"), \
             patch.object(CharacterService, "_reconcile_world_loot_visibility"), \
             patch.object(CharacterService, "_pickup_nearby_auto_loot"):
            CharacterService._handle_gameplay_move(session, b"")
        self.assertEqual(session.gameplay_chase_server_tick, 105)

    def test_route_budget_reaches_new_actors_while_old_goals_keep_changing(self):
        actors = tuple(range(80001, 80007))
        for index, actor in enumerate(actors):
            home = (self.home[0], self.home[1], self.home[2] + index)
            self.combat.monsters[actor] = replace(self.monster, actor_id=actor, position=home, aggro_range=100)
            self.combat.monster_positions[actor] = home
            self.combat.monster_health[actor] = 100
            self.combat.monster_yaws[actor] = 0.0
            self.combat.monster_moving[actor] = False
            self.combat.monster_status_effects[actor] = {}
        self.combat.active_monster_actor_ids = set(actors)
        for index in range(3):
            goal = (self.home[0] + 10 + index * 2, self.home[1], self.home[2])
            self.combat.advance_monsters(self.wire(goal), elapsed_seconds=0.04,
                server_tick=100 + index, now=100 + index * 0.04)
        self.assertTrue(set(actors).issubset(self.combat.monster_routes))
