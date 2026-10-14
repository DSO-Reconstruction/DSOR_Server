"""Keep authoritative monster movement aligned with client cast animations."""
from dataclasses import replace
import unittest

from dso_server.gameplay.local_combat_state import LocalCombatState, MonsterMovement
from dso_server.gameplay.local_monster_state import _DirectNavigationMesh, load_local_monster_encounter
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.combat_protocol import BULLET_SKILL_COMMAND, decode_monster_attack_batch
from dso_server.protocol.gameplay_protocol import Move, MoveServerCommand
from dso_server.runtime.transport import Session, RakNetServer
from dso_server.services.base import CharacterService
from dso_server.tests import test_local_combat_state as fixtures
from dso_server.tests.test_monster_ai_delivery import FakeSocket


class MonsterCastMovementTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.LocalCombatStateTests()
        fixture.setUp()
        self.use_encounter(fixture.encounter)

    def use_encounter(self, encounter, template_id=None):
        encounter = replace(encounter, navigation=_DirectNavigationMesh(), terrain=None)
        self.combat = LocalCombatState(encounter, player_actor_id=1, player_health=10000)
        self.actor = next(actor for actor, monster in self.combat.monsters.items()
                          if template_id is None or monster.template_id == template_id)
        self.combat.active_monster_actor_ids = {self.actor}
        self.combat.monsters[self.actor] = replace(
            self.combat.monsters[self.actor], aggro_range=100, escape_range=100, travel_distance=100)
        self.home = self.combat.monster_positions[self.actor]
        self.wire = encounter.coordinate_frame.world_to_wire
        self.combat.last_monster_attack_at[self.actor] = 0.0

    def point(self, x, z=0):
        return (self.home[0] + x, self.home[1], self.home[2] + z)

    def cast(self, *, now=100.0, tick=2500, distance=1.0, definition=None):
        if definition is not None:
            self.combat.monsters[self.actor] = replace(
                self.combat.monsters[self.actor], attack=definition, attacks=(definition,))
        attacks = self.combat.maybe_start_monster_attacks(
            self.wire(self.point(distance)), now=now, server_tick=tick)
        self.assertEqual(len(attacks), 1)
        return attacks[0]

    def advance(self, now, *, elapsed=.04, goal=None):
        return self.combat.advance_monsters(
            self.wire(self.point(23) if goal is None else goal),
            now=now, elapsed_seconds=elapsed, server_tick=round(now * 25))

    def test_grimmag_stays_at_cast_origin_through_authored_motion_lock(self):
        encounter = load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, "a0005_start_fortress_dun")
        template = "a0005_champion_grimmag_01_q_WayToGrimmagstone_03"
        self.use_encounter(encounter, template)
        definitions = self.combat.monsters[self.actor].attacks
        self.assertEqual({d.resource_id for d in definitions},
                         {"boss_grimmag_arcanetorrent_01", "boss_grimmag_fireball_01"})
        for definition in definitions:
            with self.subTest(skill=definition.resource_id):
                self.use_encounter(encounter, template)
                attack = self.cast(definition=definition, distance=5)
                origin = self.combat.monster_positions[self.actor]
                yaw = self.combat.monster_yaws[self.actor]
                unlock_at = 100 + attack.motion_unblock_frame / 25
                for frame in range(1, attack.motion_unblock_frame):
                    now = 100 + frame / 25
                    # Impact can finish while the animation still locks motion.
                    self.combat.resolve_due_monster_attacks(self.wire(self.point(5)), now=now)
                    moves = self.advance(now, goal=self.point(23, 4))
                    self.assertEqual(moves, ())
                    self.assertEqual(self.combat.monster_positions[self.actor], origin)
                    self.assertEqual(self.combat.monster_yaws[self.actor], yaw)
                self.assertEqual(self.actor in self.combat.pending_monster_attacks,
                                 attack.due_at > unlock_at - .04)
                moves = self.advance(unlock_at + .02, elapsed=.5)
                distance = self.combat._distance(origin, self.combat.monster_positions[self.actor])
                self.assertGreater(distance, 0)
                self.assertLessEqual(distance, self.combat.monsters[self.actor].run_speed * .02 + 1e-6)
                self.assertEqual(len(moves), 1)
                self.assertTrue(moves[0].moving)

    def test_melee_cast_prevents_chase_separation_and_turning_after_impact(self):
        definition = replace(self.combat.monsters[self.actor].attack,
                             hit_frame=5, motion_unblock_frame=40, skill_unblock_frame=40)
        self.combat.monster_moving[self.actor] = True
        attack = self.cast(definition=definition)
        self.assertFalse(self.combat.monster_moving[self.actor])
        self.combat.resolve_due_monster_attacks(self.wire(self.point(1)), now=attack.due_at)
        self.assertNotIn(self.actor, self.combat.pending_monster_attacks)
        for goal in (self.point(10), self.home, self.point(0, 1)):
            self.assertEqual(self.advance(101.0, goal=goal), ())
            self.assertEqual(self.combat.monster_positions[self.actor], self.home)
            self.assertEqual(self.combat.monster_yaws[self.actor], attack.aim_angle)

    def test_projectile_in_flight_does_not_extend_motion_lock(self):
        definition = replace(self.combat.monsters[self.actor].attack,
                             command_id=BULLET_SKILL_COMMAND, attack_range=10,
                             hit_frame=0, loop_start_frame=0, bullet_emit_delay=0,
                             projectile_velocity=1, projectile_lifetime=60,
                             motion_unblock_frame=10, skill_unblock_frame=10)
        attack = self.cast(definition=definition, distance=5)
        self.assertGreater(attack.due_at, 101)
        self.assertEqual(self.advance(100.3), ())
        self.assertTrue(self.advance(100.5))
        self.assertIn(self.actor, self.combat.pending_monster_attacks)

    def test_zero_motion_lock_allows_movement_while_impact_is_pending(self):
        definition = replace(self.combat.monsters[self.actor].attack,
                             hit_frame=25, motion_unblock_frame=0, skill_unblock_frame=25)
        self.cast(definition=definition)
        self.assertTrue(self.advance(100.1))
        self.assertIn(self.actor, self.combat.pending_monster_attacks)

    def test_next_short_spell_does_not_cut_off_previous_skill_lock(self):
        monster = self.combat.monsters[self.actor]
        long = replace(monster.attack, hit_frame=1, motion_unblock_frame=10, skill_unblock_frame=50)
        short = replace(long, resource_id="short-test-attack", motion_unblock_frame=1, skill_unblock_frame=1)
        self.combat.monsters[self.actor] = replace(monster, attack=long, attacks=(long, short), attack_interval=.1)
        attack = self.cast()
        self.combat.resolve_due_monster_attacks(self.wire(self.point(1)), now=attack.due_at)
        self.assertEqual(self.combat.maybe_start_monster_attacks(
            self.wire(self.point(1)), now=101, server_tick=2525), ())
        attacks = self.combat.maybe_start_monster_attacks(
            self.wire(self.point(1)), now=102, server_tick=2550)
        self.assertEqual(len(attacks), 1)
        self.assertEqual(attacks[0].definition.resource_id, short.resource_id)

    def test_return_home_and_visibility_removal_cancel_cast_locks(self):
        for transition in ("return", "deactivate", "reset"):
            with self.subTest(transition=transition):
                self.setUp()
                self.cast()
                if transition == "return":
                    self.combat._set_returning_home(self.actor, True, 2501, now=100.04)
                elif transition == "reset":
                    self.combat._reset_monster_to_spawn(self.actor, 2501)
                else:
                    self.combat.deactivate_monsters((self.actor,), reset=False)
                self.assertNotIn(self.actor, self.combat.monster_motion_unblock_at)
                self.assertNotIn(self.actor, self.combat.monster_skill_unblock_at)
                self.assertNotIn(self.actor, self.combat.pending_monster_attacks)

    def test_locked_actor_does_not_stop_another_monster(self):
        attack = self.cast()
        other = next(actor for actor in self.combat.monsters if actor != self.actor)
        self.combat.active_monster_actor_ids.add(other)
        other_home = self.combat.monster_positions[other]
        self.combat.monsters[other] = replace(self.combat.monsters[other], travel_distance=100)
        assignments = {self.actor: (1, self.point(10)),
                       other: (2, (other_home[0]+10, other_home[1], other_home[2]))}
        moves = self.combat.advance_monsters(self.wire(self.point(10)), now=100.1,
            elapsed_seconds=.04, server_tick=2503, targets_by_actor=assignments)
        self.assertEqual(self.combat.monster_positions[self.actor], tuple(attack.position[:3]))
        self.assertTrue(any(m.actor_id == other and m.moving for m in moves))

    def test_cast_stop_replaces_chase_and_survives_packet_loss(self):
        attack = self.cast()
        socket = FakeSocket()
        session = Session(("127.0.0.1", 40300), socket)
        session.gameplay_combat_state = self.combat
        session.gameplay_spawned_monster_actor_ids.add(self.actor)
        def command(tick):
            return MoveServerCommand(Move.from_values(self.wire(self.home), 1, 0, 0, tick, 4), self.actor)
        CharacterService._send_monster_movement_batches(session, (command(2499),), combat=self.combat)
        old_frame = RakNetServer._parse_frames(socket.sent[-1][0])[0]
        with session.outbound_lock:
            CharacterService._send_monster_movement_batches(session, (command(2500),), combat=self.combat)
        movement = MonsterMovement(self.actor, self.home, self.wire(self.home), 0, True, 2500)
        # Delivery must use the captured origin even if simulation has advanced.
        self.combat.monster_positions[self.actor] = self.point(5)
        CharacterService._send_monster_activity_to(session, self.combat, (movement,), (attack,), (), ())
        self.assertFalse(session.gameplay_pending_monster_movements)
        self.assertEqual(len(socket.sent), 2)
        frame = RakNetServer._parse_frames(socket.sent[-1][0])[0]
        self.assertEqual(frame["reliability"], 3)
        batch = decode_monster_attack_batch(frame["payload"], frame["bit_length"])
        self.assertEqual(len(batch.movements), 1)
        move = batch.movements[0].value
        self.assertEqual(move.scalar_byte, 0)
        self.assertEqual(move.duration_ticks, 0)
        self.assertEqual(move.start_tick, attack.server_tick)
        for expected, actual in zip(self.wire(self.home), move.relative_position):
            self.assertAlmostEqual(actual, expected, delta=1/64)
        receiver = Session(("127.0.0.1", 40301), FakeSocket())
        # Lose the entire cast packet; its retransmission carries the stop too.
        sequence = next(iter(session.pending_reliable))
        retries, _ = session.retransmit((sequence,), interval=0)
        retried = RakNetServer._parse_frames(retries[0][1])[0]
        delivered = RakNetServer._queue_or_deliver_inbound_order_locked(receiver, retried)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(decode_monster_attack_batch(*delivered[0]).movements[0].value.scalar_byte, 0)
        self.assertFalse(RakNetServer._queue_or_deliver_inbound_order_locked(receiver, old_frame))
