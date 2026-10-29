import math
import sqlite3
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from dso_server.paths import (
    DEFAULT_STATIC_DB_PATH,
)
from dso_server.protocol.combat_protocol import (
    BULLET_SKILL_COMMAND,
    SKILL_COMMAND,
    SUSTAINED_SKILL_COMMAND,
    SUSTAINED_SUMMON_BATCH_BITS,
    TARGET_BULLET_SKILL_COMMAND,
    TARGET_SKILL_COMMAND,
    BulletSkill,
    SustainedSkillServerCommand,
    decode_monster_attack_batch,
)
from dso_server.services.character_server import CharacterServer
from dso_server.gameplay.player_combat_stats import CombatModifier
from dso_server.protocol.gameplay_protocol import (
    Move,
    StatusEffectEntry,
    StatusEffectServerCommand,
    decode_move_batch,
)
from dso_server.protocol.gameplay_commands import LOCAL_PLAYER_ACTOR_ID
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.protocol.item_set_protocol import ItemSetModifier
from dso_server.gameplay.local_combat_state import (
    ATTACK_SPEED_BREAKPOINTS,
    MONSTER_ACQUISITION_RADIUS,
    MONSTER_CHASE_GRACE_DISTANCE,
    MONSTER_MOVEMENT_DURATION_TICKS,
    MONSTER_MOVEMENT_PUBLISH_INTERVAL_TICKS,
    MONSTER_RESET_GRACE_DISTANCE,
    MONSTER_RETURN_HOME_EFFECT_ID,
    MONSTER_RETURN_HOME_TIMEOUT_SECONDS,
    MonsterMovement,
    PLAYER_PROJECTILE_COLLISION_RADIUS,
    PLAYER_PROJECTILE_HITBOX_TOLERANCE,
    PLAYER_SUMMON_OWNER_RANGE,
    LocalCombatState,
    VictimStatusEffectDefinition,
    damage_type_ids,
    deterministic_combat_roll,
    effective_attack_speed,
)
from dso_server.gameplay.local_monster_state import (
    _DirectNavigationMesh,
    load_local_monster_encounter,
)


class LocalCombatStateTests(unittest.TestCase):
    def test_point_two_percent_block_rolls_are_independent_per_impact(self):
        chance = 0.002
        sample_size = 100_000
        blocked = sum(
            deterministic_combat_roll(
                0x424C4F43,
                sequence,
                10_000 + sequence * 25,
                196_627 + sequence % 32,
                0x103C2,
                71,
            ) < chance
            for sequence in range(1, sample_size + 1)
        )
        observed = blocked / sample_size

        self.assertGreater(observed, 0.0015)
        self.assertLess(observed, 0.0025)
        same_tick_pack = {
            deterministic_combat_roll(
                0x424C4F43, 1, 50_000, 196_627 + actor, 0x103C2, 71
            )
            for actor in range(64)
        }
        self.assertEqual(len(same_tick_pack), 64)

    def setUp(self):
        encounter = load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH,
            "b1201_pve",
            include_spawn_areas=False,
        )
        self.assertIsNotNone(encounter)
        self.encounter = encounter
        self.state = LocalCombatState(
            encounter, player_actor_id=1, player_health=1000
        )

    def _wire(self, world_position):
        return self.encounter.coordinate_frame.world_to_wire(world_position)

    @staticmethod
    def _distance_2d(first, second) -> float:
        return math.hypot(first[0] - second[0], first[2] - second[2])

    @staticmethod
    def _cast(tick: int) -> BulletSkill:
        return BulletSkill(
            0,
            1857,
            0.0,
            tick,
            tick + 1000,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )

    def test_reregistering_dead_player_preserves_zero_health(self):
        self.state.player_health[1][0] = 0
        self.state.register_player(1, 1500, current_health=0)
        self.assertEqual(self.state.player_health[1], [0, 1500])
        self.assertFalse(self.state.has_regenerating_players)

    def test_base_health_regeneration_preserves_each_due_heal(self):
        self.state.register_player(
            1,
            1000,
            current_health=500,
            regeneration_fraction=0.1,
            regeneration_interval=1.0,
        )
        now = self.state.last_player_health_regeneration_at[1] + 2.1

        transitions = self.state.regenerate_players(now=now)

        self.assertEqual(
            transitions,
            (
                (1, 500, 600, 1000, 100),
                (1, 600, 700, 1000, 100),
            ),
        )
        self.assertTrue(self.state.has_regenerating_players)
        self.state.player_health[1][0] = 1000
        self.assertFalse(self.state.has_regenerating_players)

    def test_passive_regeneration_never_heals_dead_players(self):
        self.state.register_player(
            1, 1000, current_health=0,
            regeneration_fraction=0.1, regeneration_absolute=50,
        )
        self.assertFalse(self.state.has_regenerating_players)
        now = self.state.last_player_health_regeneration_at[1] + 60
        self.assertEqual(self.state.regenerate_players(now=now), ())
        self.assertEqual(self.state.player_health[1], [0, 1000])
        # A living peer must still regenerate in the same simulation.
        self.state.register_player(99, 1000, current_health=500,
                                   regeneration_absolute=100)
        now = self.state.last_player_health_regeneration_at[99] + 1
        transitions = self.state.regenerate_players(now=now)
        self.assertEqual(transitions, ((99, 500, 600, 1000, 100),))
        self.assertEqual(self.state.player_health[1][0], 0)

    def test_standard_health_sphere_restores_one_quarter_maximum_health(self):
        self.state.player_health[1][0] = 400

        current, maximum = self.state.heal_player(actor_id=1)

        self.assertEqual((current, maximum), (650, 1000))

    def test_bullet_cast_mutates_one_monster_and_produces_death(self):
        result = self.state.apply_bullet_skill(
            self._cast(2000), self._wire(self.encounter.trigger_center)
        )
        self.assertIsNotNone(result)
        self.assertTrue(result.killed)
        self.assertEqual(result.current_health, 0)
        self.assertEqual(result.damage, result.maximum_health)
        self.assertEqual(result.attacker_actor_id, 1)
        self.assertNotIn(result.target_actor_id, self.state.living_actor_ids)
        self.assertEqual(len(self.state.living_actor_ids), 1)

    def test_fallback_navigation_supports_ground_loot_reachability(self):
        encounter = replace(
            self.encounter,
            navigation=_DirectNavigationMesh(),
            terrain=None,
        )
        state = LocalCombatState(
            encounter, player_actor_id=1, player_health=1000
        )
        actor_id = encounter.actor_ids[0]
        origin = state.monster_positions[actor_id]
        candidate = (origin[0] + 0.5, origin[1], origin[2] - 0.5)

        self.assertTrue(
            encounter.navigation.is_reachable(
                origin,
                candidate,
                max_horizontal_snap=1.5,
            )
        )
        self.assertEqual(
            state.ground_loot_position(actor_id, candidate),
            candidate,
        )

    def test_bullet_cast_opposite_every_monster_does_not_hit(self):
        player_world = (-70.0, 2.0, 29.0)
        cast = BulletSkill(
            0,
            1884,  # SimpleShot: straight projectile, 14.7-unit lifetime range
            0.0,
            2000,
            3000,
            (-1.0, 0.0, 0.0, 0.0),
            0,
        )
        health_before = dict(self.state.monster_health)
        result = self.state.apply_bullet_skill(cast, self._wire(player_world))
        self.assertIsNone(result)
        self.assertEqual(self.state.monster_health, health_before)

    def test_bullet_cast_hits_only_an_actor_intersecting_forward_ray(self):
        player_world = (-70.0, 2.0, 29.0)
        cast = BulletSkill(
            0,
            1884,
            0.0,
            2000,
            3000,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )
        result = self.state.apply_bullet_skill(cast, self._wire(player_world))
        self.assertIsNotNone(result)
        self.assertEqual(result.target_actor_id, 65703)

    def test_projectile_hit_waits_for_emit_and_distance_travel_time(self):
        player_world = (-70.0, 2.0, 29.0)
        cast = BulletSkill(
            0,
            1884,  # SimpleShot: Velocity=21, release frame 5
            0.0,
            2000,
            3000,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )
        health_before = dict(self.state.monster_health)

        pending = self.state.start_bullet_skill(
            cast, self._wire(player_world), now=100.0
        )

        self.assertIsNotNone(pending)
        self.assertAlmostEqual(
            pending.due_at - 100.0,
            5.0 / 25.0 + pending.travel_distance / 21.0,
            places=6,
        )
        self.assertLess(pending.travel_distance, pending.distance)
        self.assertEqual(self.state.monster_health, health_before)
        self.assertIsNone(
            self.state.resolve_player_hit(
                pending.cast_id, now=pending.due_at - 0.001
            )
        )
        result = self.state.resolve_player_hit(
            pending.cast_id, now=pending.due_at
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.target_actor_id, pending.target_actor_id)

    def test_attack_speed_scales_emit_phase_but_not_projectile_flight(self):
        player_world = (-70.0, 2.0, 29.0)
        cast = BulletSkill(
            0,
            1885,  # HeavyShot: Velocity=21, BulletEmitDelay=0.07
            0.0,
            2000,
            3002,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )

        normal = self.state.start_bullet_skill(
            cast,
            self._wire(player_world),
            now=100.0,
            attack_speed_multiplier=1.0,
        )
        faster = self.state.start_bullet_skill(
            cast,
            self._wire(player_world),
            now=200.0,
            attack_speed_multiplier=2.0,
        )

        self.assertIsNotNone(normal)
        self.assertIsNotNone(faster)
        self.assertEqual(normal.travel_distance, faster.travel_distance)
        normal_flight = normal.travel_distance / 21.0
        faster_flight = faster.travel_distance / 21.0
        self.assertAlmostEqual(
            normal.due_at - 100.0 - normal.emit_delay,
            normal_flight,
            places=6,
        )
        self.assertAlmostEqual(
            faster.due_at - 200.0 - faster.emit_delay,
            faster_flight,
            places=6,
        )
        self.assertAlmostEqual(
            faster.emit_delay, normal.emit_delay / 2.0, places=6
        )

    def test_ranger_arrows_cannot_kill_before_the_draw_and_flight_finish(self):
        for skill_index, draw_seconds in ((1867, 0.20), (1870, 0.36)):
            for attack_speed in (1.0, 2.0):
                for distance in (2.0, 8.0):
                    with self.subTest(skill=skill_index, speed=attack_speed, distance=distance):
                        state = LocalCombatState(self.encounter, player_actor_id=1, player_health=1000)
                        target_id = 65703
                        origin = (-70.0, 2.0, 29.0)
                        target_position = (origin[0] + distance, origin[1], origin[2])
                        state.monster_positions[target_id] = target_position
                        state.monster_health[target_id] = 1
                        skill = BulletSkill(0, skill_index, 0.0, 2000, 3000,
                                            (1.0, 0.0, 0.0, 0.0), 0)
                        (pending,) = state.start_bullet_skill_hits(
                            skill, self._wire(origin), source_actor_id=1, now=100.0,
                            attack_speed_multiplier=attack_speed,
                            eligible_actor_ids=(target_id,),
                            visible_actor_positions={target_id: target_position},
                            player_damage_range=(1000.0, 1000.0),
                        )
                        flight = pending.travel_distance / 21.0
                        arrival = 100.0 + draw_seconds / attack_speed + flight
                        # The old deadline omitted the bow draw entirely.
                        self.assertIsNone(state.resolve_runtime_player_hit(
                            1, skill.cast_id, pending.impact_index, now=100.0 + flight,
                        ))
                        self.assertEqual(state.monster_health[target_id], 1)
                        self.assertAlmostEqual(pending.emit_delay, draw_seconds / attack_speed)
                        self.assertAlmostEqual(pending.emitted_at, 100.0 + draw_seconds / attack_speed)
                        self.assertAlmostEqual(pending.due_at, arrival)
                        self.assertIsNone(state.resolve_runtime_player_hit(
                            1, skill.cast_id, pending.impact_index, now=arrival - 0.001,
                        ))
                        result = state.resolve_runtime_player_hit(
                            1, skill.cast_id, pending.impact_index, now=arrival + 1e-9,
                        )
                        self.assertTrue(result.killed)
                        self.assertEqual(result.current_health, 0)

    def test_ranger_point_blank_arrow_still_waits_for_release(self):
        target_id = 65703
        origin = self.state.monster_positions[target_id]
        skill = BulletSkill(0, 1867, 0.0, 2000, 3000, (1.0, 0.0, 0.0, 0.0), 0)
        (pending,) = self.state.start_bullet_skill_hits(
            skill, self._wire(origin), source_actor_id=1, now=100.0,
            eligible_actor_ids=(target_id,),
        )
        self.assertEqual(pending.travel_distance, 0.0)
        self.assertAlmostEqual(pending.due_at, 100.20)

    def test_runtime_projectile_deadline_uses_authored_skill_timing_rate(self):
        player_world = (-70.0, 2.0, 29.0)
        cast = BulletSkill(
            0,
            1884,
            0.0,
            2000,
            3003,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )
        geometry = self.state._load_player_skill_geometry(cast.skill_index)
        self.assertIsNotNone(geometry)
        self.state._player_skill_geometry[cast.skill_index] = replace(
            geometry,
            # A 0.5 SkillTiming program plays the complete client projectile
            # twice as fast, including its travelling object.
            skill_timing_multiplier=0.5,
        )

        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            now=100.0,
        )

        self.assertEqual(len(pending), 1)
        value = pending[0]
        self.assertAlmostEqual(
            value.projectile_velocity,
            geometry.projectile_velocity / 0.5,
            places=6,
        )
        self.assertAlmostEqual(
            value.due_at - 100.0,
            value.emit_delay
            + value.travel_distance / value.projectile_velocity,
            places=6,
        )
        self.assertEqual(
            self.state.runtime_player_hit_remaining(
                value.source_actor_id,
                value.cast_id,
                value.impact_index,
                now=value.due_at,
            ),
            0.0,
        )

    def test_runtime_attack_speed_does_not_accelerate_projectile_flight(self):
        player_world = (-70.0, 2.0, 29.0)
        normal_cast = BulletSkill(
            0,
            1849,  # Ice Sphere: Velocity=21, BulletEmitDelay=0.15
            0.0,
            2000,
            3004,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )
        fast_cast = replace(normal_cast, cast_id=3005)

        normal = self.state.start_bullet_skill_hits(
            normal_cast,
            self._wire(player_world),
            source_actor_id=98305,
            now=100.0,
            attack_speed_multiplier=1.0,
        )[0]
        fast = self.state.start_bullet_skill_hits(
            fast_cast,
            self._wire(player_world),
            source_actor_id=98305,
            now=200.0,
            attack_speed_multiplier=6.65,
        )[0]

        self.assertEqual(normal.travel_distance, fast.travel_distance)
        self.assertAlmostEqual(normal.projectile_velocity, 21.0)
        self.assertAlmostEqual(fast.projectile_velocity, 21.0)
        self.assertAlmostEqual(
            normal.due_at - 100.0 - normal.emit_delay,
            fast.due_at - 200.0 - fast.emit_delay,
            places=6,
        )
        self.assertAlmostEqual(
            fast.emit_delay,
            normal.emit_delay / 6.65,
            places=6,
        )

    def test_runtime_bullet_count_produces_distinct_authoritative_impacts(self):
        player_world = (-70.0, 2.0, 29.0)
        cast = BulletSkill(
            0,
            1884,
            0.0,
            2000,
            3100,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )
        geometry = self.state._load_player_skill_geometry(cast.skill_index)
        self.assertIsNotNone(geometry)
        self.state._player_skill_geometry[cast.skill_index] = replace(
            geometry,
            bullet_count=3,
            impact_count=1,
            reproduction_count=0,
        )

        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            now=100.0,
        )

        self.assertEqual(len(pending), 3)
        self.assertEqual(
            {value.projectile_index for value in pending}, {0, 1, 2}
        )
        self.assertEqual(len({value.target_actor_id for value in pending}), 1)
        resolutions = tuple(
            self.state.resolve_runtime_player_hit(
                value.source_actor_id,
                value.cast_id,
                value.impact_index,
                now=value.due_at,
            )
            for value in pending
        )
        self.assertTrue(all(value is not None for value in resolutions))
        self.assertFalse(resolutions[0].killed)
        self.assertFalse(resolutions[1].killed)
        self.assertTrue(resolutions[2].killed)

    def test_runtime_hit_uses_authoritative_player_damage_range(self):
        player_world = (-70.0, 2.0, 29.0)
        cast = BulletSkill(
            0,
            1884,
            0.0,
            2000,
            3199,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )
        geometry = self.state._load_player_skill_geometry(cast.skill_index)
        self.assertIsNotNone(geometry)
        self.state._player_skill_geometry[cast.skill_index] = replace(
            geometry,
            bullet_count=1,
            impact_count=1,
            reproduction_count=0,
            damage_modifier=2.0,
        )

        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            now=100.0,
            player_damage_range=(100.0, 100.0),
        )
        self.assertEqual(len(pending), 1)
        previous = self.state.monster_health[pending[0].target_actor_id]

        resolution = self.state.resolve_runtime_player_hit(
            pending[0].source_actor_id,
            pending[0].cast_id,
            pending[0].impact_index,
            now=pending[0].due_at,
        )

        self.assertEqual(resolution.damage, 200)
        self.assertEqual(resolution.current_health, previous - 200)

    def test_runtime_overkill_reports_rolled_damage_not_remaining_health(self):
        player_world = (-70.0, 2.0, 29.0)
        cast = BulletSkill(
            0,
            1884,
            0.0,
            2000,
            3200,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )
        geometry = self.state._load_player_skill_geometry(cast.skill_index)
        self.assertIsNotNone(geometry)
        self.state._player_skill_geometry[cast.skill_index] = replace(
            geometry,
            bullet_count=1,
            impact_count=1,
            reproduction_count=0,
            damage_modifier=2.0,
        )
        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            now=100.0,
            player_damage_range=(1000.0, 1000.0),
        )[0]
        self.state.monster_health[pending.target_actor_id] = 25

        resolution = self.state.resolve_runtime_player_hit(
            pending.source_actor_id,
            pending.cast_id,
            pending.impact_index,
            now=pending.due_at,
        )

        self.assertEqual(resolution.current_health, 0)
        self.assertEqual(resolution.damage, 2000)
        self.assertGreater(resolution.damage, 25)
        self.assertFalse(resolution.periodic_damage)

    def test_runtime_reproduction_chains_to_a_second_target_later(self):
        player_world = (-70.0, 2.0, 29.0)
        cast = BulletSkill(
            0,
            1884,
            0.0,
            2000,
            3101,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )
        geometry = self.state._load_player_skill_geometry(cast.skill_index)
        self.assertIsNotNone(geometry)
        self.state._player_skill_geometry[cast.skill_index] = replace(
            geometry,
            bullet_count=1,
            impact_count=1,
            reproduction_count=1,
            target_search_radius=100.0,
        )

        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            now=100.0,
        )

        self.assertEqual(len(pending), 2)
        self.assertNotEqual(
            pending[0].target_actor_id, pending[1].target_actor_id
        )
        self.assertGreater(pending[1].due_at, pending[0].due_at)

    def test_fireball_impact_radius_schedules_nearby_splash_target(self):
        cast = BulletSkill(
            0,
            1848,
            0.0,
            2000,
            3103,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )
        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire((-70.0, 2.0, 29.0)),
            source_actor_id=98305,
            now=100.0,
        )

        self.assertEqual(
            {value.target_actor_id for value in pending},
            {65703, 65704},
        )
        self.assertEqual(pending[0].due_at, pending[1].due_at)
        self.assertEqual(pending[1].motion_type, "Area")

    def test_conditional_xmas_aura_does_not_widen_ice_missile_hit(self):
        player_world = (0.0, 2.0, 0.0)
        direct_actor, off_axis_actor = self.encounter.actor_ids
        self.state.monster_positions[direct_actor] = (0.0, 2.0, -8.0)
        self.state.monster_positions[off_axis_actor] = (4.0, 2.0, -8.0)
        modifier = CombatModifier(
            "SkillStatusEffect",
            (
                "addLE",
                "iceball",
                "jewel_bonus_xmas_aura",
                "C:0.05",
                "ON:hit",
            ),
        )
        base = self.state._load_player_skill_geometry(1849)
        self.assertIsNotNone(base)
        modified = self.state._modified_player_skill_geometry(
            base, (modifier,)
        )
        self.assertTrue(
            any(
                effect.resource_id == "jewel_bonus_xmas_aura"
                and effect.chance == 0.05
                and effect.aura_radius >= 4.0
                for effect in modified.location_visual_effects
            )
        )
        cast = BulletSkill(
            0, 1849, 0.0, 2000, 3104,
            (0.0, 0.0, -1.0, 0.0), 1,
        )

        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            modifier_effect_ids=(modifier,),
            now=100.0,
        )

        self.assertEqual(
            {value.target_actor_id for value in pending},
            {direct_actor},
        )

    def test_conditional_turret_aura_does_not_widen_simple_shot_hit(self):
        player_world = (0.0, 2.0, 0.0)
        direct_actor, off_axis_actor = self.encounter.actor_ids
        self.state.monster_positions[direct_actor] = (0.0, 2.0, -8.0)
        self.state.monster_positions[off_axis_actor] = (4.0, 2.0, -8.0)
        modifier = CombatModifier(
            "SkillStatusEffect",
            (
                "addLE",
                "SimpleShot",
                "item_pw_sigrismarr_dwarf_turret_bonus_aura_t2",
                "C:0.6",
                "D:1.0",
                "DP:1.0",
                "ON:Hit",
            ),
        )
        cast = BulletSkill(
            0, 1884, 0.0, 2000, 3105,
            (0.0, 0.0, -1.0, 0.0), 1,
        )

        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            modifier_effect_ids=(modifier,),
            now=100.0,
        )

        self.assertEqual(
            {value.target_actor_id for value in pending},
            {direct_actor},
        )

    def test_projectile_target_is_not_scheduled_twice_after_splash(self):
        player_world = (0.0, 2.0, 0.0)
        first_actor, second_actor = self.encounter.actor_ids
        self.state.monster_positions[first_actor] = (0.0, 2.0, -8.0)
        self.state.monster_positions[second_actor] = (0.0, 2.0, -9.0)
        cast = BulletSkill(
            0, 1848, 0.0, 2000, 3106,
            (0.0, 0.0, -1.0, 0.0), 1,
        )
        geometry = self.state._load_player_skill_geometry(cast.skill_index)
        self.assertIsNotNone(geometry)
        self.state._player_skill_geometry[cast.skill_index] = replace(
            geometry,
            impact_count=2,
        )

        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            now=100.0,
        )

        pairs = [
            (value.projectile_index, value.target_actor_id)
            for value in pending
        ]
        self.assertEqual(len(pairs), 2)
        self.assertEqual(len(pairs), len(set(pairs)))

    def test_mod_location_effect_is_noop_when_target_is_missing(self):
        base = self.state._load_player_skill_geometry(1849)
        self.assertIsNotNone(base)
        modifier = CombatModifier(
            "SkillStatusEffect",
            (
                "modLE",
                "iceball",
                "jewel_bonus_xmas_aura",
                "C:1.0",
            ),
        )

        modified = self.state._modified_player_skill_geometry(
            base, (modifier,)
        )

        self.assertEqual(
            modified.location_status_effects,
            base.location_status_effects,
        )
        self.assertEqual(
            modified.location_visual_effects,
            base.location_visual_effects,
        )

    def test_runtime_straight_projectile_is_cancelled_after_target_sidesteps(self):
        player_world = (-70.0, 2.0, 29.0)
        cast = BulletSkill(
            0,
            1884,
            0.0,
            2000,
            3102,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )
        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            now=100.0,
        )
        self.assertEqual(len(pending), 1)
        value = pending[0]
        old_position = self.state.monster_positions[value.target_actor_id]
        self.state.monster_positions[value.target_actor_id] = (
            old_position[0],
            old_position[1],
            old_position[2] + 10.0,
        )

        remaining = self.state.runtime_player_hit_remaining(
            value.source_actor_id,
            value.cast_id,
            value.impact_index,
            now=value.due_at,
        )

        self.assertIsNone(remaining)
        self.assertNotIn(
            (value.source_actor_id, value.cast_id, value.impact_index),
            self.state.pending_runtime_player_hits,
        )

    def test_long_range_projectile_hits_target_advancing_through_its_path(self):
        player_world = (0.0, 2.0, 0.0)
        target_actor_id = self.encounter.actor_ids[0]
        self.state.monster_positions[target_actor_id] = (
            20.0, 2.0, 0.0
        )
        self.state.monster_positions[self.encounter.actor_ids[1]] = (
            20.0, 2.0, 10.0
        )
        cast = BulletSkill(
            0,
            1849,  # Ice Sphere: straight, 25-unit range
            0.0,
            2000,
            3105,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )
        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            now=100.0,
            eligible_actor_ids=(target_actor_id,),
        )

        self.assertEqual(len(pending), 1)
        value = pending[0]
        # The monster advances through the fixed projectile corridor during
        # its flight. At the original deadline the projectile front is beyond
        # the new transform, which proves that their continuous paths crossed.
        self.state.monster_positions[target_actor_id] = (
            15.0, 2.0, 0.0
        )
        self.assertEqual(
            self.state.runtime_player_hit_remaining(
                value.source_actor_id,
                value.cast_id,
                value.impact_index,
                now=value.due_at,
            ),
            0.0,
        )
        resolution = self.state.resolve_runtime_player_hit(
            value.source_actor_id,
            value.cast_id,
            value.impact_index,
            now=value.due_at,
        )
        self.assertIsNotNone(resolution)
        self.assertEqual(resolution.target_actor_id, target_actor_id)

    def test_runtime_projectile_accepts_last_published_collision_capsule(self):
        player_world = (-70.0, 2.0, 29.0)
        cast = BulletSkill(
            0, 1884, 0.0, 2000, 3107,
            (1.0, 0.0, 0.0, 0.0), 0,
        )
        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            now=100.0,
        )
        self.assertEqual(len(pending), 1)
        value = pending[0]
        actor_id = value.target_actor_id
        visible = self.state.monster_positions[actor_id]
        self.state.monster_last_published_positions[actor_id] = visible
        self.state.monster_positions[actor_id] = (
            visible[0], visible[1], visible[2] + 0.75,
        )

        self.assertEqual(
            self.state.runtime_player_hit_remaining(
                value.source_actor_id,
                value.cast_id,
                value.impact_index,
                now=value.due_at,
            ),
            0.0,
        )

    def test_runtime_projectile_uses_viewer_delivered_transform(self):
        player_world = (0.0, 2.0, 0.0)
        actor_id = self.encounter.actor_ids[0]
        other_actor_id = self.encounter.actor_ids[1]
        visible = (12.0, 2.0, 0.0)
        authoritative = (12.0, 2.0, 6.0)
        self.state.monster_positions[actor_id] = authoritative
        self.state.monster_last_published_positions[actor_id] = authoritative
        self.state.monster_positions[other_actor_id] = (12.0, 2.0, 12.0)
        self.state.monster_last_published_positions[other_actor_id] = (
            12.0, 2.0, 12.0
        )
        cast = BulletSkill(
            0, 1849, 0.0, 2000, 3111,
            (1.0, 0.0, 0.0, 0.0), 0,
        )

        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            now=100.0,
            eligible_actor_ids=(actor_id,),
            visible_actor_positions={actor_id: visible},
        )

        self.assertEqual(len(pending), 1)
        value = pending[0]
        self.assertEqual(value.target_actor_id, actor_id)
        self.assertEqual(
            self.state.runtime_player_hit_remaining(
                value.source_actor_id,
                value.cast_id,
                value.impact_index,
                now=value.due_at,
                visible_actor_positions={actor_id: visible},
            ),
            0.0,
        )

    def test_delivered_sidestep_still_cancels_fixed_projectile(self):
        player_world = (0.0, 2.0, 0.0)
        actor_id = self.encounter.actor_ids[0]
        initial = (12.0, 2.0, 0.0)
        sidestepped = (12.0, 2.0, 6.0)
        self.state.monster_positions[actor_id] = initial
        cast = BulletSkill(
            0, 1849, 0.0, 2000, 3112,
            (1.0, 0.0, 0.0, 0.0), 0,
        )
        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            now=100.0,
            eligible_actor_ids=(actor_id,),
            visible_actor_positions={actor_id: initial},
        )
        self.assertEqual(len(pending), 1)
        value = pending[0]
        self.state.monster_positions[actor_id] = sidestepped
        self.state.monster_last_published_positions[actor_id] = sidestepped

        self.assertIsNone(
            self.state.runtime_player_hit_remaining(
                value.source_actor_id,
                value.cast_id,
                value.impact_index,
                now=value.due_at,
                visible_actor_positions={actor_id: sidestepped},
            )
        )

    def test_explicit_hostile_target_repairs_stale_eligibility_membership(self):
        player_world = (0.0, 2.0, 0.0)
        actor_id = self.encounter.actor_ids[0]
        self.state.monster_positions[actor_id] = (10.0, 2.0, 0.0)
        cast = BulletSkill(
            0, 1849, 0.0, 2000, 3113,
            (1.0, 0.0, 0.0, 0.0), 0,
        )

        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            now=100.0,
            explicit_target_actor_ids=(actor_id,),
            eligible_actor_ids=(),
        )

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].target_actor_id, actor_id)

    def test_friendly_explicit_target_falls_back_to_hostile_cast_ray(self):
        player_world = (-70.0, 2.0, 29.0)
        cast = BulletSkill(
            0, 1884, 0.0, 2000, 3108,
            (1.0, 0.0, 0.0, 0.0), 0,
        )
        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            now=100.0,
            explicit_target_actor_ids=(0x0001F00D,),
        )

        self.assertEqual(len(pending), 1)
        self.assertIn(pending[0].target_actor_id, self.state.living_actor_ids)

    def test_running_target_cannot_postpone_an_accepted_projectile_deadline(self):
        player_world = (0.0, 2.0, 0.0)
        target_actor_id = self.encounter.actor_ids[0]
        self.state.monster_positions[target_actor_id] = (
            20.0, 2.0, 0.0
        )
        cast = BulletSkill(
            0,
            1849,
            0.0,
            2000,
            3106,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )
        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            now=100.0,
            eligible_actor_ids=(target_actor_id,),
        )
        self.assertEqual(len(pending), 1)
        value = pending[0]
        # Continue running away inside the authored range after the cast was
        # accepted. The cast-time collision plan remains authoritative.
        self.state.monster_positions[target_actor_id] = (
            23.0, 2.0, 0.0
        )
        checked_at = value.due_at - 0.10
        self.assertAlmostEqual(
            self.state.runtime_player_hit_remaining(
                value.source_actor_id,
                value.cast_id,
                value.impact_index,
                now=checked_at,
            ),
            value.due_at - checked_at,
            places=6,
        )

    def test_runtime_close_projectile_hits_inside_initial_muzzle_sweep(self):
        player_world = (-70.0, 2.0, 29.0)
        target_actor_id = 65703
        # SimpleShot starts 2.25 units in front of the player. A target one
        # unit ahead is inside that initial sweep but behind the muzzle.
        self.state.monster_positions[target_actor_id] = (
            player_world[0] + 1.0,
            player_world[1],
            player_world[2],
        )
        cast = BulletSkill(
            0,
            1884,
            0.0,
            2000,
            3104,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )

        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            now=100.0,
            eligible_actor_ids=(target_actor_id,),
        )

        self.assertEqual(len(pending), 1)
        value = pending[0]
        self.assertEqual(value.travel_distance, 0.0)
        self.assertAlmostEqual(value.spawn_sweep_distance, 2.25)
        self.assertEqual(
            self.state.runtime_player_hit_remaining(
                value.source_actor_id,
                value.cast_id,
                value.impact_index,
                now=value.due_at,
            ),
            0.0,
        )
        resolution = self.state.resolve_runtime_player_hit(
            value.source_actor_id,
            value.cast_id,
            value.impact_index,
            now=value.due_at,
        )
        self.assertIsNotNone(resolution)
        self.assertEqual(resolution.target_actor_id, target_actor_id)

    def test_database_victim_status_effects_follow_the_delayed_hit(self):
        player_world = (-70.0, 2.0, 29.0)
        cast = BulletSkill(
            0,
            1886,  # PositionBomb: armor break + stun at C:1
            0.0,
            2000,
            3001,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )

        pending = self.state.start_bullet_skill(
            cast, self._wire(player_world), now=100.0
        )
        self.assertIsNotNone(pending)
        result = self.state.resolve_player_hit(
            pending.cast_id, now=pending.due_at
        )

        self.assertIsNotNone(result)
        effects = {
            definition.resource_id: definition
            for definition in result.victim_status_effects
        }
        self.assertIn("skill_positionbomb_debuff_armor", effects)
        self.assertIn("debuff_cc_stun", effects)
        self.assertEqual(
            effects["skill_positionbomb_debuff_armor"].effect_id, 4717
        )
        self.assertEqual(
            effects["skill_positionbomb_debuff_armor"].duration_ticks,
            125,
        )
        self.assertEqual(
            effects["skill_positionbomb_debuff_armor"].values[0],
            -0.2,
        )

    def test_shifted_lightning_uses_attack_range_and_ground_hit_radius(self):
        player_world = (-70.0, 2.0, 29.0)
        target = self.state.monsters[65703].position
        pending = self.state.start_spatial_skill_hits(
            skill_index=1853,
            cast_id=3200,
            server_tick=2000,
            player_position=self._wire(player_world),
            target_position=self._wire(target),
            source_actor_id=98305,
            now=100.0,
        )

        self.assertTrue(pending)
        self.assertIn(
            65703, {value.target_actor_id for value in pending}
        )
        self.assertAlmostEqual(
            pending[0].due_at - 100.0, (9.0 + 35.0) / 25.0, places=6
        )
        self.assertIn(
            "debuff_cc_charge",
            {
                effect.resource_id
                for effect in pending[0].victim_status_effects
            },
        )

    def test_shifted_skill_rejects_target_beyond_database_attack_range(self):
        pending = self.state.start_spatial_skill_hits(
            skill_index=1853,
            cast_id=3201,
            server_tick=2000,
            player_position=self._wire((-70.0, 2.0, 29.0)),
            target_position=self._wire((100.0, 2.0, 100.0)),
            source_actor_id=98305,
            now=100.0,
        )
        self.assertEqual(pending, ())

    def test_melee_cone_uses_the_clients_pi_rotated_aim_angle(self):
        pending = self.state.start_spatial_skill_hits(
            skill_index=1854,
            cast_id=3203,
            server_tick=2000,
            player_position=self._wire((-59.5, 2.0, 38.5)),
            source_actor_id=98305,
            aim_angle=0.0,
            now=100.0,
        )

        self.assertTrue(pending)
        self.assertIn(
            "debuff_cc_frost",
            {
                effect.resource_id
                for effect in pending[0].victim_status_effects
            },
        )

    def test_dwarf_flamethrower_uses_authored_sixty_degree_angle(self):
        player_world = (0.0, 2.0, 0.0)
        front_actor, side_actor = self.encounter.actor_ids
        self.state.monster_positions[front_actor] = (0.0, 2.0, -5.0)
        self.state.monster_positions[side_actor] = (5.0, 2.0, 0.0)

        pending = self.state.start_spatial_skill_hits(
            skill_index=1898,
            cast_id=3300,
            server_tick=2000,
            player_position=self._wire(player_world),
            source_actor_id=98305,
            aim_angle=0.0,
            now=100.0,
        )

        self.assertEqual(
            {value.target_actor_id for value in pending},
            {front_actor},
        )

    def test_database_cone_width_is_a_forward_trapezoid(self):
        player_world = (0.0, 2.0, 0.0)
        inside_actor, outside_actor = self.encounter.actor_ids
        self.state.monster_positions[inside_actor] = (2.0, 2.0, -8.0)
        self.state.monster_positions[outside_actor] = (7.0, 2.0, -8.0)

        pending = self.state.start_spatial_skill_hits(
            skill_index=1876,  # ranger multishot: 3.0 -> 9.5 wide
            cast_id=3301,
            server_tick=2000,
            player_position=self._wire(player_world),
            source_actor_id=98305,
            aim_angle=0.0,
            now=100.0,
        )

        self.assertEqual(
            {value.target_actor_id for value in pending},
            {inside_actor},
        )

    def test_scatter_shot_hits_full_cone_at_the_authored_hit_frame(self):
        player_world = (0.0, 2.0, 0.0)
        hinted_actor, second_actor = self.encounter.actor_ids
        self.state.monster_positions[hinted_actor] = (
            -2.0, 2.0, -8.0
        )
        self.state.monster_positions[second_actor] = (2.0, 2.0, -8.0)

        pending = self.state.start_spatial_skill_hits(
            skill_index=1876,
            cast_id=3305,
            server_tick=2000,
            player_position=self._wire(player_world),
            source_actor_id=98305,
            target_position=self._wire(
                self.state.monster_positions[hinted_actor]
            ),
            explicit_target_actor_ids=(hinted_actor,),
            aim_angle=0.0,
            now=100.0,
        )

        self.assertEqual(
            {value.target_actor_id for value in pending},
            {hinted_actor, second_actor},
        )
        self.assertTrue(
            all(abs(value.due_at - 100.36) < 1e-9 for value in pending)
        )

    def test_target_point_projectile_impacts_at_world_endpoint(self):
        player_world = (0.0, 2.0, 0.0)
        endpoint_actor, ray_actor = self.encounter.actor_ids
        self.state.monster_positions[endpoint_actor] = (8.0, 2.0, 0.0)
        self.state.monster_positions[ray_actor] = (4.0, 2.0, 0.0)
        cast = BulletSkill(
            0,
            1886,  # PositionBombShot: 3.4 impact radius
            0.0,
            2000,
            3302,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )

        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            target_position=self._wire((8.0, 2.0, 0.0)),
            now=100.0,
        )

        self.assertEqual(
            {value.target_actor_id for value in pending},
            {endpoint_actor},
        )
        self.assertTrue(all(value.motion_type == "Area" for value in pending))

    def test_explosive_arrow_uses_location_aura_explosion_radius(self):
        player_world = (0.0, 2.0, 0.0)
        direct_actor, splash_actor = self.encounter.actor_ids
        self.state.monster_positions[direct_actor] = (0.0, 2.0, -8.0)
        self.state.monster_positions[splash_actor] = (3.0, 2.0, -8.0)
        cast = BulletSkill(
            0, 1871, 0.0, 2000, 3401,
            (0.0, 0.0, -1.0, 0.0), 1,
        )

        pending = self.state.start_bullet_skill_hits(
            cast,
            self._wire(player_world),
            source_actor_id=98305,
            now=100.0,
        )

        self.assertEqual(
            {value.target_actor_id for value in pending},
            {direct_actor, splash_actor},
        )
        self.assertEqual(
            {value.due_at for value in pending}, {pending[0].due_at}
        )

    def test_wild_pack_spawns_three_separated_wolves(self):
        definition = self.state.player_summon_definition(1877)
        self.assertEqual(definition.amount, 3)
        spawn = self.state.spawn_player_summon(
            definition,
            owner_actor_id=0x000103C1,
            position=self.encounter.trigger_center,
            yaw=0.0,
            level=100,
            server_tick=500,
            player_damage_range=(100.0, 200.0),
            now=100.0,
        )

        self.assertEqual(len(spawn.actors), 3)
        positions = [summon.position for summon in spawn.actors]
        self.assertEqual(len(set(positions)), 3)
        self.assertTrue(
            all(
                self.state._distance(left, right) >= 1.0
                for index, left in enumerate(positions)
                for right in positions[index + 1:]
            )
        )

    def test_thicket_trail_applies_persistent_poison_slow_and_armor_break(self):
        (
            rate,
            start_delay,
            maximum_range,
            velocity,
            visuals,
            receiver_effects,
        ) = self.state.player_skill_bullet_location_trail(1879)
        self.assertAlmostEqual(rate, 0.267, places=3)
        self.assertAlmostEqual(start_delay, 0.24, places=2)
        self.assertGreaterEqual(maximum_range, 18.0)
        self.assertEqual(velocity, 15.0)
        self.assertEqual(
            {effect.resource_id for effect in receiver_effects},
            {
                "skill_thorntrail_dmg_effect",
                "skill_thorntrail_debuff_movementspeed",
                "skill_thorntrail_debuff_armor",
            },
        )
        actor_id = self.state.living_actor_ids[0]
        position = self.state.monster_positions[actor_id]
        self.state.start_player_location_visuals(
            visuals,
            position=position,
            yaw=0.0,
            server_tick=500,
            owner_actor_id=98305,
            cast_id=4401,
            receiver_status_effects=receiver_effects,
            source_skill_index=1879,
            player_damage_range=(100.0, 100.0),
            now=100.0,
        )

        first = self.state.apply_player_location_auras(
            now=100.0, server_tick=500
        )
        repeated = self.state.apply_player_location_auras(
            now=100.04, server_tick=501
        )

        self.assertTrue(first)
        self.assertIn(actor_id, {
            application.target_actor_id for application in first
        })
        self.assertFalse(repeated)
        self.assertTrue(
            {
                "skill_thorntrail_dmg_effect",
                "skill_thorntrail_debuff_movementspeed",
                "skill_thorntrail_debuff_armor",
            }.issubset(self.state.monster_status_effects[actor_id])
        )
        damage = self.state.resolve_due_monster_status_damage(
            now=101.01, server_tick=526
        )
        self.assertTrue(damage)
        self.assertTrue(all(value.periodic_damage for value in damage))

    def test_actor_targeted_melee_hits_only_requested_actor(self):
        player_world = (0.0, 2.0, 0.0)
        first_actor, target_actor = self.encounter.actor_ids
        self.state.monster_positions[first_actor] = (1.0, 2.0, 0.0)
        self.state.monster_positions[target_actor] = (1.2, 2.0, 0.0)

        pending = self.state.start_spatial_skill_hits(
            skill_index=1878,  # ranger mightystab: MeleeTarget + Actor
            cast_id=3303,
            server_tick=2000,
            player_position=self._wire(player_world),
            source_actor_id=98305,
            explicit_target_actor_ids=(target_actor,),
            now=100.0,
        )

        self.assertEqual(
            {value.target_actor_id for value in pending},
            {target_actor},
        )

    def test_stacked_talent_and_item_set_modify_resolved_skill(self):
        geometry = self.state._load_player_skill_geometry(1884)
        self.assertIsNotNone(geometry)
        with sqlite3.connect(self.state.encounter.static_database_path) as db:
            effect_id = db.execute(
                "SELECT rowid - 1 FROM _Template_StatusEffect "
                "WHERE Id='skill_SimpleShot_lvl'"
            ).fetchone()[0]
        rank_ten = StatusEffectEntry(
            effect_id=effect_id,
            field_04=100,
            field_0c=0,
            field_14=25,
            field_08=0,
            field_18=9,
            field_10=25,
            field_1c=100,
            field_20=1,
            field_25=False,
            field_26=True,
            field_39=True,
            has_details=True,
            values=(0.0, 0.0, 0.0, 0.0, 0.0),
        )
        set_bonus = ItemSetModifier(
            "SkillDamage",
            ("0.5", "relative", "SimpleShot"),
            "",
            "SkillDamage:0.5,relative,SimpleShot",
        )

        modified = self.state._modified_player_skill_geometry(
            geometry, (rank_ten, set_bonus)
        )

        self.assertAlmostEqual(
            modified.damage_modifier,
            (geometry.damage_modifier + 0.10) * 1.5,
            places=5,
        )
        ammunition = {
            value.resource_id: value
            for value in modified.victim_status_effects
        }
        self.assertAlmostEqual(
            ammunition["ammunition_damage_fire"].values[0],
            -0.275,
            places=5,
        )

    def test_ranked_structural_projectile_modifier_applies_once(self):
        """A talent rank must not multiply an authored topology unlock."""

        geometry = self.state._load_player_skill_geometry(1849)  # iceball
        self.assertIsNotNone(geometry)
        frozen_master_rank_five = StatusEffectEntry(
            effect_id=6174,  # mage_talent_frozen_master_lvl
            field_04=101,
            field_0c=0,
            field_14=25,
            field_08=0,
            field_18=4,
            field_10=25,
            field_1c=100,
            field_20=1,
            field_25=False,
            field_26=True,
            field_39=True,
            has_details=True,
            values=(0.0, 0.0, 0.0, 0.0, 0.0),
        )

        modified = self.state._modified_player_skill_geometry(
            geometry, (frozen_master_rank_five,)
        )

        # The DB says ``BulletImpactCount:3,absolute,iceball``. Rank five
        # unlocks those three penetrations once; it does not add 3 * 5.
        self.assertEqual(modified.impact_count, geometry.impact_count + 3)

    def test_poison_thorns_hurt_the_wearer_only_when_they_hit(self):
        """An idle wearer with no enemy in range must not poison itself."""

        with sqlite3.connect(self.state.encounter.static_database_path) as db:
            ids = dict(db.execute(
                "SELECT Id, rowid - 1 FROM _Template_StatusEffect WHERE Id IN "
                "('rune_poison_damage_effect', 'rune_poison_damage_selfdamage')"
            ))
        values = (-1.0, -0.35, 0.0, 0.0, 0.0)
        aura = SimpleNamespace(
            aura_definition=SimpleNamespace(aura_radius=5.0),
            receiver_definitions=(SimpleNamespace(
                effect_id=ids["rune_poison_damage_effect"], values=values),),
            causer_definitions=(SimpleNamespace(
                effect_id=ids["rune_poison_damage_selfdamage"], values=values),),
        )
        owner = (0.0, 0.0, 0.0)
        for actor_id in self.state.living_actor_ids:
            self.state.monster_positions[actor_id] = (500.0, 0.0, 500.0)

        def pulse():
            return self.state.resolve_socket_periodic_auras(
                1, owner, (aura,), player_damage_range=(100.0, 100.0),
                now=0.0,
            )

        monsters, wearer = pulse()
        self.assertEqual((monsters, wearer), ((), ()))
        self.assertEqual(self.state.player_health[1][0], 1000)

        target = next(iter(self.state.living_actor_ids))
        self.state.monster_positions[target] = (1.0, 0.0, 0.0)
        self.state.monster_health[target] = 10_000
        monsters, wearer = pulse()
        self.assertEqual([hit.target_actor_id for hit in monsters], [target])
        self.assertEqual(len(wearer), 1)
        self.assertEqual(wearer[0].damage, 35)
        self.assertEqual(self.state.player_health[1][0], 965)

    def test_dwarf_mine_resolves_native_trap_and_talent_rank(self):
        base = self.state.player_trap_definition(1887)
        self.assertIsNotNone(base)
        self.assertEqual(base.resource_id, "dwarf_mine")
        self.assertEqual(base.trip_radius, 5.0)
        with sqlite3.connect(self.state.encounter.static_database_path) as db:
            effect_id = db.execute(
                "SELECT rowid - 1 FROM _Template_StatusEffect "
                "WHERE Id='skill_Mine_lvl'"
            ).fetchone()[0]
        rank_ten = StatusEffectEntry(
            effect_id=effect_id,
            field_04=100,
            field_0c=0,
            field_14=25,
            field_08=0,
            field_18=9,
            field_10=25,
            field_1c=100,
            field_20=1,
            field_25=False,
            field_26=True,
            field_39=True,
            has_details=True,
            values=(0.0, 0.0, 0.0, 0.0, 0.0),
        )

        ranked = self.state.player_trap_definition(
            1887, modifier_sources=(rank_ten,)
        )

        self.assertIsNotNone(ranked)
        self.assertEqual(ranked.resource_id, "dwarf_mine10")
        self.assertTrue(ranked.victim_status_effects)

    def test_native_mine_trip_uses_authored_radius_and_damage_pipeline(self):
        definition = self.state.player_trap_definition(1887)
        actor_id = self.state.living_actor_ids[0]
        trap_world = self.state.monster_positions[actor_id]

        pending = self.state.start_spatial_skill_hits(
            skill_index=1887,
            cast_id=98765,
            server_tick=500,
            player_position=self._wire(trap_world),
            source_actor_id=98305,
            now=100.0,
            player_damage_range=(100.0, 100.0),
            area_radius_override=definition.trip_radius,
            impact_delay_override=0.0,
            additional_victim_effects=definition.victim_status_effects,
        )
        self.assertTrue(pending)

        result = self.state.resolve_runtime_player_hit(
            98305,
            98765,
            pending[0].impact_index,
            now=100.0,
        )

        self.assertIsNotNone(result)
        self.assertGreater(result.damage, 0)
        self.assertEqual(result.skill_index, 1887)

    def test_special_ammunition_unlocks_matching_vulnerable_monster(self):
        actor_id = self.state.living_actor_ids[0]
        self.state.monster_ammo_vulnerabilities[actor_id] = frozenset({
            "ammotype01"
        })
        monster_world = self.state.monster_positions[actor_id]
        player_world = (monster_world[0] - 2.0, monster_world[1], monster_world[2])
        skill = BulletSkill(
            0,
            1884,
            0.0,
            500,
            600,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )

        blocked = self.state.start_bullet_skill_hits(
            skill,
            self._wire(player_world),
            source_actor_id=98305,
            explicit_target_actor_ids=(actor_id,),
            now=100.0,
        )
        allowed = self.state.start_bullet_skill_hits(
            replace(skill, cast_id=601),
            self._wire(player_world),
            source_actor_id=98305,
            explicit_target_actor_ids=(actor_id,),
            modifier_effect_ids=(
                ItemSetModifier(
                    "AmmunitionType",
                    ("AmmoType01",),
                    "",
                    "AmmunitionType:AmmoType01",
                ),
            ),
            now=100.0,
        )

        self.assertEqual(len(blocked), 1)
        self.assertTrue(blocked[0].damage_immune)
        previous_health = self.state.monster_health[actor_id]
        blocked_result = self.state.resolve_runtime_player_hit(
            98305,
            600,
            blocked[0].impact_index,
            now=blocked[0].due_at,
        )
        self.assertIsNotNone(blocked_result)
        self.assertTrue(blocked_result.damage_immune)
        self.assertEqual(blocked_result.damage, 0)
        self.assertEqual(blocked_result.current_health, previous_health)
        self.assertEqual(blocked_result.victim_status_effects, ())
        self.assertTrue(allowed)
        self.assertFalse(allowed[0].damage_immune)

    def test_spatial_hit_reports_special_ammunition_immunity(self):
        actor_id = self.state.living_actor_ids[0]
        self.state.monster_ammo_vulnerabilities[actor_id] = frozenset({
            "ammotype01"
        })
        monster_world = self.state.monster_positions[actor_id]

        pending = self.state.start_spatial_skill_hits(
            skill_index=1851,  # Frost Nova
            cast_id=602,
            server_tick=500,
            player_position=self._wire(monster_world),
            source_actor_id=98305,
            eligible_actor_ids=(actor_id,),
            now=100.0,
            player_damage_range=(100.0, 100.0),
        )

        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0].damage_immune)

    def test_poison_master_converts_damage_and_runs_typed_dot_ticks(self):
        geometry = self.state._load_player_skill_geometry(1847)
        with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as database:
            effect_id = database.execute(
                "SELECT rowid - 1 FROM _Template_StatusEffect "
                "WHERE Id='mage_talent_poison_master'"
            ).fetchone()[0]
        poison_master = StatusEffectEntry(
            effect_id=effect_id,
            field_04=101,
            field_0c=0,
            field_14=25,
            field_08=0,
            field_18=0,
            field_10=25,
            field_1c=100,
            field_20=1,
            field_25=False,
            field_26=True,
            field_39=True,
            has_details=True,
            values=(),
        )

        modified = self.state._modified_player_skill_geometry(
            geometry, (poison_master,)
        )
        self.assertEqual(damage_type_ids(modified.damage_type), (5,))
        poison = next(
            effect for effect in modified.victim_status_effects
            if effect.resource_id == "debuff_dot_poison"
        )
        self.assertEqual(poison.chance, 1.0)

        actor_id = self.encounter.actor_ids[0]
        self.state._record_monster_status_effects(
            actor_id,
            (poison,),
            now=100.0,
            source_actor_id=98305,
            player_damage_range=(1000.0, 1000.0),
            skill_index=1847,
        )
        ticks = self.state.resolve_due_monster_status_damage(
            now=101.5, server_tick=2500
        )
        self.assertEqual(len(ticks), 1)
        self.assertEqual(ticks[0].hit_tags, (5,))
        self.assertEqual(ticks[0].attacker_actor_id, 98305)
        self.assertGreater(ticks[0].damage, 0)
        self.assertTrue(ticks[0].periodic_damage)

    def test_poison_master_is_selected_as_remote_skill_sequence_dependency(self):
        with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as database:
            effect_id = database.execute(
                "SELECT rowid - 1 FROM _Template_StatusEffect "
                "WHERE Id='warrior_talent_poison_master'"
            ).fetchone()[0]
        poison_master = StatusEffectEntry(
            effect_id=effect_id,
            field_04=202,
            field_0c=0,
            field_14=25,
            field_08=0,
            field_18=0,
            field_10=0,
            field_1c=100,
            field_20=1,
            field_25=False,
            field_26=False,
            field_39=True,
            has_details=True,
            values=(),
        )

        selected = self.state.player_skill_visual_modifier_effects(
            1833, (poison_master,)
        )

        self.assertEqual(selected, (poison_master,))

    def test_projectile_field_and_death_explosions_are_database_authored(self):
        geometry = self.state._load_player_skill_geometry(1850)

        self.assertIn(
            "mage_frozen_orb_explosion_effect",
            {effect.resource_id for effect in geometry.bullet_location_effects},
        )
        final_explosion = next(
            effect for effect in geometry.bullet_death_effects
            if effect.resource_id
            == "mage_frozen_orb_final_explosion_aura_effect"
        )
        damage, tags = self.state.status_effect_immediate_damage(
            self.encounter.actor_ids[0],
            (final_explosion,),
            (100.0, 100.0),
            now=100.0,
        )
        self.assertEqual(damage, 200)
        self.assertEqual(tags, (2,))

    def test_passive_actor_status_effect_retains_its_kill_trigger(self):
        geometry = self.state._load_player_skill_geometry(1847)
        with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as database:
            effect_id = database.execute(
                "SELECT rowid - 1 FROM _Template_StatusEffect "
                "WHERE Id='mage_talent_poison_master_lvl'"
            ).fetchone()[0]
        poison_rank = StatusEffectEntry(
            effect_id=effect_id,
            field_04=102,
            field_0c=0,
            field_14=25,
            field_08=0,
            field_18=0,
            field_10=25,
            field_1c=100,
            field_20=1,
            field_25=False,
            field_26=True,
            field_39=True,
            has_details=True,
            values=(),
        )

        modified = self.state._modified_player_skill_geometry(
            geometry, (poison_rank,)
        )
        orbital = next(
            effect for effect in modified.user_status_effects
            if effect.resource_id == "mage_talent_poison_orbital_field"
        )
        self.assertEqual(orbital.trigger, "kill")
        self.assertAlmostEqual(orbital.chance, 0.05)

    def test_equipment_talent_level_bonus_adds_authored_skill_ranks(self):
        geometry = self.state._load_player_skill_geometry(1870)
        quiver_bonus = ItemSetModifier(
            "TalentLevelBonus",
            (
                "4",
                "ranger_talent_skill_cat_03_dmg",
                "StringParamLocaOverride",
            ),
            "",
            "TalentLevelBonus:4,ranger_talent_skill_cat_03_dmg",
        )

        modified = self.state._modified_player_skill_geometry(
            geometry, (quiver_bonus,)
        )
        self.assertAlmostEqual(
            modified.damage_modifier,
            geometry.damage_modifier + 0.04,
            places=6,
        )

    def test_freeze_actor_feature_stops_monster_movement_and_attacks(self):
        with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as database:
            frozen = self.state._load_victim_status_effects(
                database, "debuff_cc_freeze,C:1.0,D:2.0"
            )[0]
        actor_id = self.encounter.actor_ids[0]
        before = self.state.monster_positions[actor_id]
        self.state.monster_moving[actor_id] = True
        self.state._record_monster_status_effects(
            actor_id, (frozen,), now=100.0
        )
        player_world = (before[0] + 1.0, before[1], before[2])

        movement = self.state.advance_monsters(
            self._wire(player_world),
            elapsed_seconds=0.04,
            server_tick=2500,
        )
        attacks = self.state.maybe_start_monster_attacks(
            self._wire(player_world),
            now=100.1,
            server_tick=2501,
            eligible_actor_ids={actor_id},
        )

        self.assertEqual(self.state.monster_positions[actor_id], before)
        self.assertFalse(self.state.monster_moving[actor_id])
        self.assertTrue(any(value.actor_id == actor_id for value in movement))
        self.assertEqual(attacks, ())

    def test_statusless_monster_lookup_does_not_create_empty_state(self):
        actor_id = self.encounter.actor_ids[0]
        self.state.monster_status_effects.pop(actor_id, None)

        sources = self.state._active_monster_modifier_sources(
            actor_id, now=100.0
        )

        self.assertEqual(sources, ())
        self.assertNotIn(actor_id, self.state.monster_status_effects)

    def test_equipped_skill_speed_aligns_scaled_hit_frames(self):
        inventory = InventoryInfo(
            items=[
                ItemInfo(
                    100,
                    template_id="all_mythic_ring_death",
                    item_level=100,
                )
            ],
            equipment_slots={100: (8,)},
        )

        multiplier = self.state.player_skill_speed_multiplier(
            class_name="dwarf",
            inventory=inventory,
        )

        # Raw speed 6.0 (base 1.0 plus the ring's direct +5) crosses the
        # 5.7143 breakpoint and plays at 6.25 attacks/second.
        self.assertAlmostEqual(multiplier, 6.25)
        actor_id = self.encounter.actor_ids[0]
        self.state.monster_positions[actor_id] = (2.0, 2.0, 0.0)
        self.state.monster_positions[self.encounter.actor_ids[1]] = (
            30.0,
            2.0,
            0.0,
        )
        pending = self.state.start_spatial_skill_hits(
            skill_index=1898,  # Flamethrower: frame 13, scaled timing
            cast_id=3400,
            server_tick=2000,
            player_position=self._wire((0.0, 2.0, 0.0)),
            source_actor_id=98305,
            aim_angle=-1.5707963267948966,
            now=100.0,
            attack_speed_multiplier=multiplier,
        )

        self.assertTrue(pending)
        self.assertAlmostEqual(
            pending[0].due_at - 100.0,
            13.0 / 25.0 / 6.25,
            places=6,
        )

    def test_attack_speed_uses_discrete_retail_breakpoints(self):
        self.assertEqual(effective_attack_speed(1.0), 1.0)
        self.assertEqual(effective_attack_speed(1.02), 1.0)
        for index, (threshold, actual) in enumerate(
            ATTACK_SPEED_BREAKPOINTS
        ):
            self.assertAlmostEqual(
                effective_attack_speed(threshold), actual
            )
            if index + 1 < len(ATTACK_SPEED_BREAKPOINTS):
                next_threshold = ATTACK_SPEED_BREAKPOINTS[index + 1][0]
                midpoint = (threshold + next_threshold) / 2.0
                self.assertAlmostEqual(
                    effective_attack_speed(midpoint), actual
                )
        self.assertAlmostEqual(effective_attack_speed(99.0), 25.0)

    def test_unscaled_jump_hit_frame_ignores_equipment_speed(self):
        actor_id = self.encounter.actor_ids[0]
        target = (2.0, 2.0, 0.0)
        self.state.monster_positions[actor_id] = target
        self.state.monster_positions[self.encounter.actor_ids[1]] = (
            30.0,
            2.0,
            0.0,
        )

        pending = self.state.start_spatial_skill_hits(
            skill_index=1901,  # Iron Pounce: ScaleSkillTiming=false
            cast_id=3401,
            server_tick=2000,
            player_position=self._wire((0.0, 2.0, 0.0)),
            target_position=self._wire(target),
            source_actor_id=98305,
            now=100.0,
            attack_speed_multiplier=10.0,
        )

        self.assertTrue(pending)
        self.assertAlmostEqual(
            pending[0].due_at - 100.0,
            11.0 / 25.0,
            places=6,
        )

    def test_victim_mark_modifies_followup_damage_and_punch_through(self):
        player_world = (0.0, 2.0, 0.0)
        marked_actor, rear_actor = self.encounter.actor_ids
        self.state.monster_positions[marked_actor] = (4.0, 2.0, 0.0)
        self.state.monster_positions[rear_actor] = (7.0, 2.0, 0.0)
        markshot = self.state._load_player_skill_geometry(1867)
        mark = next(
            effect
            for effect in markshot.victim_status_effects
            if effect.resource_id == "debuff_cc_mark"
        )
        self.state._record_monster_status_effects(
            marked_actor, (mark,), now=100.0
        )
        mightyshot = BulletSkill(
            0,
            1870,
            0.0,
            2000,
            3304,
            (1.0, 0.0, 0.0, 0.0),
            0,
        )

        pending = self.state.start_bullet_skill_hits(
            mightyshot,
            self._wire(player_world),
            source_actor_id=98305,
            now=100.1,
        )

        self.assertEqual(
            {value.target_actor_id for value in pending},
            {marked_actor, rear_actor},
        )
        marked_hit = next(
            value
            for value in pending
            if value.target_actor_id == marked_actor
        )
        self.assertAlmostEqual(marked_hit.damage_modifier, 3.5)

    def test_ranger_mark_bonuses_cover_every_authored_skill(self):
        markshot = self.state._load_player_skill_geometry(1867)
        mark = next(
            effect
            for effect in markshot.victim_status_effects
            if effect.resource_id == 'debuff_cc_mark'
        )
        affected_skill_indexes = (
            1871,  # explosive arrow
            1878,  # deadly blow
            1870,  # precise shot
            1868,  # blade dance
            1875,  # bird of prey
            1876,  # scatter shot
        )

        for skill_index in affected_skill_indexes:
            with self.subTest(skill_index=skill_index):
                base = self.state._load_player_skill_geometry(skill_index)
                marked = self.state._modified_player_skill_geometry(
                    base, (mark,)
                )
                self.assertAlmostEqual(
                    marked.damage_modifier,
                    base.damage_modifier * 2.0,
                )

    def test_singularity_applies_its_location_aura_to_monsters(self):
        player_world = (-70.0, 2.0, 29.0)
        target = self.state.monsters[65703].position
        pending = self.state.start_spatial_skill_hits(
            skill_index=1859,
            cast_id=3202,
            server_tick=2000,
            player_position=self._wire(player_world),
            target_position=self._wire(target),
            source_actor_id=98305,
            now=100.0,
        )

        self.assertTrue(pending)
        effects = {
            effect.resource_id: effect
            for effect in pending[0].victim_status_effects
        }
        self.assertIn(
            "skill_arcanevortext_debuff_movementspeed", effects
        )
        self.assertIn(
            "skill_arcanevortext_debuff_attackspeed", effects
        )
        self.assertIn(
            "skill_arcanevortext_debuff_resistance_physical", effects
        )
        self.assertIn(
            "skill_arcanevortext_debuff_resist_elemantal", effects
        )
        self.assertEqual(
            effects[
                "skill_arcanevortext_debuff_movementspeed"
            ].duration_ticks,
            200,
        )
        resolution = self.state.resolve_runtime_player_hit(
            pending[0].source_actor_id,
            pending[0].cast_id,
            pending[0].impact_index,
            now=pending[0].due_at,
        )
        self.assertEqual(resolution.damage, 0)
        self.assertFalse(resolution.killed)
        self.assertIn(resolution.target_actor_id, self.state.living_actor_ids)

    def test_living_monster_animates_then_damages_at_database_hit_frame(self):
        world_position = (-60.86, 2.0, 30.1)
        wire_position = self._wire(world_position)
        now = max(self.state.last_monster_attack_at.values())
        early = self.state.maybe_start_monster_attacks(
            wire_position,
            server_tick=100,
            now=now + self.encounter.attack_interval / 2,
        )
        self.assertEqual(early, ())
        attacks = self.state.maybe_start_monster_attacks(
            wire_position,
            server_tick=101,
            now=now + self.encounter.attack_interval,
        )
        self.assertEqual(len(attacks), 1)
        attack = attacks[0]
        self.assertEqual(attack.skill_level, 0)
        self.assertEqual(attack.skill_index, 1359)
        self.assertEqual(attack.command_id, TARGET_SKILL_COMMAND)
        self.assertEqual(attack.cast_id, 0)
        self.assertEqual(attack.variant, 1)
        self.assertNotEqual(attack.direction[:3], (0.0, 0.0, 0.0))
        self.assertEqual(attack.hit_frame, 18)
        self.assertEqual(attack.impact_tick, 119)
        self.assertEqual(self.state.player_current_health, 1000)
        self.assertEqual(
            self.state.resolve_due_monster_attacks(
                wire_position, now=attack.due_at - 0.01
            ),
            (),
        )
        results = self.state.resolve_due_monster_attacks(
            wire_position, now=attack.due_at
        )
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.target_actor_id, 1)
        self.assertLess(result.current_health, result.maximum_health)
        self.assertGreater(result.damage, 0)
        self.assertFalse(result.killed)

    def test_lethal_monster_hit_kills_once_and_discards_other_due_impacts(self):
        wire = self._wire((-60.86, 2.0, 30.1))
        attacks = self.state.maybe_start_monster_attacks(
            wire, server_tick=100,
            now=max(self.state.last_monster_attack_at.values()) + 10,
        )
        self.assertEqual(len(attacks), 1)
        first = attacks[0]
        second_id = next(a for a in self.encounter.actor_ids if a != first.attacker_actor_id)
        self.state.monster_positions[second_id] = self.state.monster_positions[first.attacker_actor_id]
        self.state.pending_monster_attacks[second_id] = replace(first, attacker_actor_id=second_id)
        self.state.player_health[1][0] = 1
        self.state.player_current_health = 1
        results = self.state.resolve_due_monster_attacks(
            wire, now=first.due_at, block_chance=1, block_damage_reduction=0,
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].killed)
        self.assertEqual(results[0].damage, 1)
        self.assertEqual(self.state.player_current_health, 0)
        self.assertEqual(self.state.player_health[1][0], 0)
        self.assertFalse(self.state.pending_monster_attacks)
        self.assertFalse(self.state.has_regenerating_players)
        self.assertEqual(self.state.regenerate_players(now=first.due_at + 60), ())

    def test_damage_immunity_still_prevents_lethal_monster_hits(self):
        wire = self._wire((-60.86, 2.0, 30.1))
        attacks = self.state.maybe_start_monster_attacks(
            wire, server_tick=100,
            now=max(self.state.last_monster_attack_at.values()) + 10,
        )
        self.state.player_health[1][0] = 1
        results = self.state.resolve_due_monster_attacks(
            wire, now=attacks[0].due_at, incoming_damage_multiplier=0,
        )
        self.assertEqual(results, ())
        self.assertEqual(self.state.player_health[1][0], 1)

    def test_healing_cannot_resurrect_a_dead_player(self):
        self.state.player_health[1][0] = 0
        self.state.player_current_health = 0
        self.assertEqual(self.state.heal_player(actor_id=1), (0, 1000))
        self.assertEqual(self.state.heal_player(actor_id=1, full=True), (0, 1000))
        self.assertEqual(self.state.heal_player_relative(1, 1), (0, 1000))
        self.assertEqual(self.state.heal_player_amount(1, 1000), (0, 1000, 0))
        self.assertEqual(self.state.player_health[1][0], 0)
        self.assertEqual(self.state.player_current_health, 0)

    def test_idle_monster_cannot_acquire_a_player_outside_vicinity(self):
        actor_id = self.encounter.actor_ids[0]
        monster = self.state.monsters[actor_id]
        self.state.monsters[actor_id] = replace(
            monster,
            aggro_range=200.0,
            travel_distance=40.0,
            attack=replace(monster.attack, attack_range=200.0),
        )
        self.state.last_monster_attack_at[actor_id] = 0.0
        far_player = (
            monster.position[0] + MONSTER_ACQUISITION_RADIUS + 2.0,
            monster.position[1],
            monster.position[2],
        )

        movements = self.state.advance_monsters(
            self._wire(far_player),
            elapsed_seconds=0.2,
            server_tick=100,
        )
        attacks = self.state.maybe_start_monster_attacks(
            self._wire(far_player),
            server_tick=101,
            now=100.0,
            eligible_actor_ids={actor_id},
        )

        self.assertEqual(movements, ())
        self.assertEqual(attacks, ())
        self.assertEqual(
            self.state.monster_positions[actor_id], monster.position
        )

        near_player = (
            monster.position[0] + MONSTER_ACQUISITION_RADIUS - 2.0,
            monster.position[1],
            monster.position[2],
        )
        attacks = self.state.maybe_start_monster_attacks(
            self._wire(near_player),
            server_tick=102,
            now=100.0,
            eligible_actor_ids={actor_id},
        )
        self.assertEqual(len(attacks), 1)

    def test_dense_pack_attack_starts_are_bounded_and_fair(self):
        actor_ids = self.state.living_actor_ids
        self.assertGreaterEqual(len(actor_ids), 2)
        anchor = self.state.monsters[actor_ids[0]].position
        for actor_id in actor_ids:
            self.state.monsters[actor_id] = replace(
                self.state.monsters[actor_id],
                position=anchor,
            )
            self.state.monster_positions[actor_id] = anchor
            self.state.last_monster_attack_at[actor_id] = 0.0
        player_position = self._wire(anchor)

        with patch(
            'dso_server.gameplay.local_combat_state.MONSTER_ATTACK_START_BUDGET', 1
        ):
            first = self.state.maybe_start_monster_attacks(
                player_position, server_tick=200, now=100.0
            )
            second = self.state.maybe_start_monster_attacks(
                player_position, server_tick=201, now=100.04
            )

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(
            first[0].attacker_actor_id, second[0].attacker_actor_id
        )

    def test_monsters_path_to_melee_range_before_damage(self):
        player_world = (-70.0, 2.0, 35.0)
        player_position = self._wire(player_world)
        self.state.last_monster_attack_at = {
            actor_id: 0.0 for actor_id in self.state.monsters
        }
        self.assertEqual(
            self.state.maybe_start_monster_attacks(
                player_position, server_tick=100, now=10.0
            ),
            (),
        )
        initial = dict(self.state.monster_positions)
        published_movements = 0
        for tick in range(101, 151):
            movements = self.state.advance_monsters(
                player_position,
                elapsed_seconds=0.2,
                server_tick=tick,
            )
            published_movements += len(movements)
            self.assertTrue(
                all(movement.world_position[1] == 2.0 for movement in movements)
            )
            self.assertTrue(
                all(
                    abs(movement.wire_position[1] - 0.2763062) < 1e-5
                    for movement in movements
                )
            )
            if any(
                self.state._distance(player_world, position)
                <= self.encounter.attack.attack_range
                for position in self.state.monster_positions.values()
            ):
                break
        self.assertGreater(published_movements, 0)
        self.assertNotEqual(initial, self.state.monster_positions)
        self.assertTrue(
            any(
                self.state._distance(player_world, position)
                <= self.encounter.attack.attack_range
                for position in self.state.monster_positions.values()
            )
        )
        attacks = self.state.maybe_start_monster_attacks(
            player_position, server_tick=151, now=11.1
        )
        self.assertTrue(attacks)
        result = self.state.resolve_due_monster_attacks(
            player_position, now=max(attack.due_at for attack in attacks)
        )
        self.assertTrue(result)
        self.assertLess(result[0].current_health, result[0].maximum_health)

    def test_melee_does_not_reach_a_player_far_above_or_below(self):
        # A monster on a ledge must not strike a player standing under it:
        # range is flat, the vertical reach is MONSTER_MELEE_VERTICAL_REACH.
        player_world = (-70.0, 2.0, 35.0)
        player_position = self._wire(player_world)
        self.state.last_monster_attack_at = {
            actor_id: 0.0 for actor_id in self.state.monsters
        }
        for tick in range(101, 151):
            self.state.advance_monsters(
                player_position, elapsed_seconds=0.2, server_tick=tick
            )
            if any(
                self.state._distance(player_world, position)
                <= self.encounter.attack.attack_range
                for position in self.state.monster_positions.values()
            ):
                break
        above = (player_world[0], player_world[1] + 4.0, player_world[2])
        self.assertEqual(
            self.state.maybe_start_monster_attacks(
                self._wire(above), server_tick=151, now=11.1
            ),
            (),
        )
        attacks = self.state.maybe_start_monster_attacks(
            player_position, server_tick=152, now=11.2
        )
        self.assertTrue(attacks)
        # Gone up a ledge before the blow lands: it misses.
        self.assertEqual(
            self.state.resolve_due_monster_attacks(
                self._wire(above),
                now=max(attack.due_at for attack in attacks),
            ),
            (),
        )

    def test_radius_skill_reaches_a_monster_standing_higher(self):
        # bloody360 (Radius, HitRange 4.62): a monster 4.2 m away on flat
        # ground, standing 3 m higher, is inside its radius as the Angle and
        # Cone shapes measure it (flat).
        database = sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True)
        try:
            index = database.execute(
                "SELECT rowid - 1 FROM _Template_Skill WHERE Id='bloody360'"
            ).fetchone()[0]
        finally:
            database.close()
        actor = sorted(self.state.living_actor_ids)[0]
        monster = self.state.monster_positions[actor]
        for other in list(self.state.monster_positions):
            if other != actor:
                self.state.monster_positions[other] = (1e4, 0.0, 1e4)
        player = (monster[0], monster[1] - 3.0, monster[2] - 4.2)
        plan = self.state.plan_entity_skill_hits(
            skill_index=index, player_position=self._wire(player),
            projectile=False, aim_angle=math.pi,
        )
        self.assertEqual(len(plan.impacts), 1)

    def test_straight_projectile_lane_has_a_height(self):
        attack = SimpleNamespace(
            projectile_origin=(0.0, 10.0, 0.0),
            direction=(0.0, -0.5, 1.0, 0.0),
            target_position=(0.0, 0.0, 20.0),
            projectile_radius=0.3,
            maximum_projectile_range=0.0,
        )
        hit = LocalCombatState._fixed_projectile_intersects_player
        self.assertTrue(hit(attack, (0.0, 0.0, 20.0)))
        # Halfway the lane is 5 m up: a player on the ground there is under it.
        self.assertFalse(hit(attack, (0.0, 0.0, 10.0)))
        self.assertTrue(hit(attack, (0.0, 4.0, 10.0)))

    def test_collision_correction_never_exceeds_run_speed_step(self):
        player_world = (-51.75, 2.0, 12.45)
        player_position = self._wire(player_world)
        before = dict(self.state.monster_positions)
        elapsed = 0.04

        self.state.advance_monsters(
            player_position,
            elapsed_seconds=elapsed,
            server_tick=100,
        )

        for actor_id, start in before.items():
            monster = self.state.monsters[actor_id]
            distance = self.state._distance(
                start, self.state.monster_positions[actor_id]
            )
            self.assertLessEqual(
                distance, monster.run_speed * elapsed + 1e-6
            )

    def test_monsters_keep_database_capsule_clearance_while_converging(self):
        player_world = (-51.75, 2.0, 12.45)
        player_position = self._wire(player_world)
        for tick in range(100):
            self.state.advance_monsters(
                player_position,
                elapsed_seconds=0.12,
                server_tick=tick,
            )
        first, second = self.state.monster_positions.values()
        separation = self.state._distance(first, second)
        expected = self.encounter.capsule_radius * 2.0 + 0.15
        self.assertGreaterEqual(separation + 1e-6, expected)
        self.assertNotEqual(first, second)

    def test_homing_monster_bullet_carries_player_as_wire_target(self):
        encounter = load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH, "g0004_corrupted_jungle_01_pve"
        )
        state = LocalCombatState(
            encounter,
            player_actor_id=1,
            player_health=1000,
            initially_active=False,
        )
        shaman_actor_id = next(
            actor_id
            for actor_id, monster in state.monsters.items()
            if (
                monster.attack.resource_id
                == "EvilTribeShamanHomingShot_Andermight"
            )
        )
        state.activate_monsters((shaman_actor_id,))
        state.last_monster_attack_at[shaman_actor_id] = 0.0
        # Its InitialCoolDown has run out.
        state._monster_skill_ready_at[shaman_actor_id] = {}
        monster_position = state.monster_positions[shaman_actor_id]
        player_position = encounter.coordinate_frame.world_to_wire(
            (
                monster_position[0] + 1.0,
                monster_position[1],
                monster_position[2],
            )
        )
        attacks = state.maybe_start_monster_attacks(
            player_position,
            server_tick=2000,
            now=100.0,
        )
        self.assertEqual(len(attacks), 1)
        self.assertEqual(
            attacks[0].command_id, TARGET_BULLET_SKILL_COMMAND
        )
        self.assertEqual(attacks[0].target_actor_id, 1)
        distance = state._distance(
            monster_position,
            encounter.coordinate_frame.wire_to_world(player_position),
        )
        # A homing bullet is released then flown (_advance_homing_bullet);
        # impact_tick stays the cast-time estimate until it lands.
        self.assertAlmostEqual(
            attacks[0].due_at - 100.0,
            state.monsters[shaman_actor_id].attack.loop_start_frame / 25.0,
            places=6,
        )
        self.assertEqual(
            attacks[0].impact_tick,
            2000
            + state.monsters[shaman_actor_id].attack.loop_start_frame
            + round(distance / 3.0 * 25.0),
        )
        self.assertEqual(
            attacks[0].hit_frame,
            state.monsters[shaman_actor_id].attack.loop_start_frame,
        )

    def test_monster_motion_simulates_each_tick_but_publishes_sparsely(self):
        actor_id = self.encounter.actor_ids[0]
        monster = self.state.monsters[actor_id]
        self.state.active_monster_actor_ids = {actor_id}
        player_world = (
            monster.position[0] + min(5.0, monster.aggro_range - 0.5),
            monster.position[1],
            monster.position[2],
        )
        player_wire = self._wire(player_world)
        positions = []
        publications = []

        for offset in range(MONSTER_MOVEMENT_PUBLISH_INTERVAL_TICKS + 1):
            movements = self.state.advance_monsters(
                player_wire,
                elapsed_seconds=0.04,
                server_tick=100 + offset,
                eligible_actor_ids={actor_id},
            )
            positions.append(self.state.monster_positions[actor_id])
            publications.append(movements)

        self.assertNotEqual(positions[0], positions[1])
        self.assertEqual(len(publications[0]), 1)
        self.assertTrue(all(not value for value in publications[1:-1]))
        self.assertEqual(len(publications[-1]), 1)

    def test_monster_activity_encodes_a_horizon_beyond_the_publish_cadence(self):
        # The client dead-reckons a walking sample for at most its duration;
        # a duration equal to the cadence stood monsters still before every
        # late sample. Retail sends 18 whatever the resend gap.
        from dso_server.runtime.transport import Session

        class _Session(Session):

            def __init__(self):
                super().__init__(("127.0.0.1", 40000), None)
                self.sent = []

            def send_unreliable_sequenced(
                self, payload, *, skip_if_busy=False, **_kwargs
            ):
                self.skip_if_busy = bool(skip_if_busy)
                self.sent.append(payload)
                return b"sent"

        actor_id = self.encounter.actor_ids[0]
        monster = self.state.monsters[actor_id]
        movement = MonsterMovement(
            actor_id=actor_id,
            world_position=monster.position,
            wire_position=self._wire(monster.position),
            yaw=0.5,
            moving=True,
            server_tick=1234,
        )
        session = _Session()
        session.gameplay_combat_state = self.state
        session.gameplay_spawned_monster_actor_ids.add(actor_id)

        CharacterServer._send_monster_activity_to(
            session,
            self.state,
            (movement,),
            (),
            (),
            (),
        )

        self.assertEqual(len(session.sent), 1)
        self.assertTrue(session.skip_if_busy)
        batch = decode_move_batch(*session.sent[0])
        self.assertEqual(len(batch.commands), 1)
        self.assertEqual(batch.commands[0].actor_id, actor_id)
        self.assertEqual(
            batch.commands[0].value.duration_ticks,
            MONSTER_MOVEMENT_DURATION_TICKS,
        )
        self.assertGreater(
            MONSTER_MOVEMENT_DURATION_TICKS,
            MONSTER_MOVEMENT_PUBLISH_INTERVAL_TICKS,
        )

    def test_projectile_ray_uses_only_one_published_movement_interval(self):
        actor_id = self.encounter.actor_ids[0]
        monster = self.state.monsters[actor_id]
        self.state.active_monster_actor_ids = {actor_id}
        geometry = replace(
            self.state._load_player_skill_geometry(1857),
            targeting_type='Direction',
            attack_range=10.0,
            projectile_range=10.0,
            projectile_radius=0.25,
            player_width=0.0,
        )
        origin = (0.0, monster.position[1], 0.0)
        direction = (1.0, 0.0, 0.0, 0.0)
        # Acquisition and impact share the client's sweep reach plus the
        # configured tolerance; movement compensation stops after one
        # publication interval once the lag window has passed.
        collision_radius = self.state._player_projectile_contact_radius(
            self.state._monster_bullet_shape_radius(monster), 0.25, 0.0,
            geometry.projectile_velocity / max(0.01, geometry.skill_timing_multiplier),
        )
        current = (5.0, monster.position[1], collision_radius + 0.10)
        self.state.monster_positions[actor_id] = current
        self.state.monster_last_published_positions[actor_id] = current
        self.assertEqual(
            self.state._skill_targets(
                origin,
                direction,
                geometry,
                frozenset({actor_id}),
            ),
            (),
        )

        self.state.monster_last_published_positions[actor_id] = (
            5.0,
            monster.position[1],
            collision_radius - 0.10,
        )
        self.assertEqual(
            self.state._skill_targets(
                origin,
                direction,
                geometry,
                frozenset({actor_id}),
            ),
            (monster,),
        )

        maximum_sweep = (
            monster.run_speed
            * MONSTER_MOVEMENT_PUBLISH_INTERVAL_TICKS
            / 25.0
        )
        current = (
            5.0,
            monster.position[1],
            collision_radius + maximum_sweep + 0.10,
        )
        self.state.monster_positions[actor_id] = current
        self.state.monster_last_published_positions[actor_id] = (
            5.0, monster.position[1], 0.0
        )
        # Published before the lag-compensation window: no viewer still
        # draws the monster there.
        trail = self.state.monster_last_published_positions.trails[actor_id]
        for index, (stamp, position) in enumerate(trail):
            trail[index] = (stamp - 10.0, position)
        self.assertEqual(
            self.state._skill_targets(
                origin,
                direction,
                geometry,
                frozenset({actor_id}),
            ),
            (),
        )

        behind = (-2.0, monster.position[1], 0.0)
        self.state.monster_positions[actor_id] = behind
        self.state.monster_last_published_positions[actor_id] = behind
        self.assertEqual(
            self.state._skill_targets(
                origin,
                direction,
                geometry,
                frozenset({actor_id}),
            ),
            (),
        )
        origin_overlap = (
            -collision_radius / 2.0, monster.position[1], 0.0
        )
        self.state.monster_positions[actor_id] = origin_overlap
        self.state.monster_last_published_positions[actor_id] = origin_overlap
        self.assertEqual(
            self.state._skill_targets(
                origin,
                direction,
                geometry,
                frozenset({actor_id}),
            ),
            (monster,),
        )

    def test_ranged_monster_holds_database_firing_distance(self):
        encounter = load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH, "g0004_corrupted_jungle_01_pve"
        )
        state = LocalCombatState(
            encounter,
            player_actor_id=1,
            player_health=1000,
            initially_active=False,
        )
        actor_id = next(
            actor_id
            for actor_id, monster in state.monsters.items()
            if (
                monster.attack.resource_id
                == "EvilTribeShamanHomingShot_Andermight"
            )
        )
        state.activate_monsters((actor_id,))
        monster = state.monsters[actor_id]
        player_world = (
            monster.position[0] + 10.0,
            monster.position[1],
            monster.position[2],
        )
        player_wire = encounter.coordinate_frame.world_to_wire(player_world)

        for tick in range(100, 125):
            state.advance_monsters(
                player_wire,
                elapsed_seconds=0.04,
                server_tick=tick,
            )

        distance = state._distance(
            state.monster_positions[actor_id], player_world
        )
        self.assertGreaterEqual(
            distance + 1e-6,
            monster.attack.attack_range * 0.80,
        )
        self.assertLessEqual(distance, monster.attack.attack_range)

    def test_fixed_monster_projectile_misses_after_player_sidesteps(self):
        encounter = load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH, "g0004_corrupted_jungle_01_pve"
        )
        state = LocalCombatState(
            encounter,
            player_actor_id=1,
            player_health=1000,
            initially_active=False,
        )
        actor_id = next(
            actor_id
            for actor_id, monster in state.monsters.items()
            if monster.attack.command_id == BULLET_SKILL_COMMAND
        )
        state.activate_monsters((actor_id,))
        state.last_monster_attack_at[actor_id] = 0.0
        # Its shot's InitialCoolDown (1-3 s) has run out.
        state._monster_skill_ready_at[actor_id] = {}
        monster_position = state.monster_positions[actor_id]
        cast_target = (
            monster_position[0] + 6.0,
            monster_position[1],
            monster_position[2],
        )
        attacks = state.maybe_start_monster_attacks(
            encounter.coordinate_frame.world_to_wire(cast_target),
            server_tick=500,
            now=100.0,
        )
        self.assertEqual(len(attacks), 1)
        attack = attacks[0]
        self.assertEqual(attack.command_id, BULLET_SKILL_COMMAND)

        sidestepped = (
            cast_target[0],
            cast_target[1],
            cast_target[2] + 3.0,
        )
        health_before = state.player_current_health
        results = state.resolve_due_monster_attacks(
            encounter.coordinate_frame.world_to_wire(sidestepped),
            now=attack.due_at,
        )

        self.assertEqual(results, ())
        self.assertEqual(state.player_current_health, health_before)

        state.last_monster_attack_at[actor_id] = 0.0
        attacks = state.maybe_start_monster_attacks(
            encounter.coordinate_frame.world_to_wire(cast_target),
            server_tick=600,
            now=200.0,
        )
        self.assertEqual(len(attacks), 1)
        results = state.resolve_due_monster_attacks(
            encounter.coordinate_frame.world_to_wire(cast_target),
            now=attacks[0].due_at,
        )
        self.assertEqual(len(results), 1)
        self.assertLess(state.player_current_health, health_before)

    def test_monster_leash_applies_and_removes_return_home_effect(self):
        actor_id = self.encounter.actor_ids[0]
        monster = self.state.monsters[actor_id]
        # Inside the reset radius, so the actor walks home rather than
        # snapping; the player is outside it, so the chase is given up.
        reset_radius = (
            monster.travel_distance
            + MONSTER_CHASE_GRACE_DISTANCE
            + MONSTER_RESET_GRACE_DISTANCE
        )
        self.state.monster_positions[actor_id] = (
            monster.position[0] + reset_radius - 1.0,
            monster.position[1],
            monster.position[2],
        )
        far_player = self._wire(
            (
                monster.position[0] + reset_radius + 5.0,
                monster.position[1],
                monster.position[2],
            )
        )

        self.state.advance_monsters(
            far_player,
            elapsed_seconds=0.0,
            server_tick=300,
        )

        self.assertIn(actor_id, self.state.returning_home_actor_ids)
        transitions = self.state.drain_return_home_transitions()
        self.assertEqual(len(transitions), 1)
        self.assertTrue(transitions[0].active)
        self.assertEqual(
            transitions[0].effect_id, MONSTER_RETURN_HOME_EFFECT_ID
        )

        self.state.monster_positions[actor_id] = monster.position
        self.state.advance_monsters(
            far_player,
            elapsed_seconds=0.04,
            server_tick=301,
        )

        self.assertNotIn(actor_id, self.state.returning_home_actor_ids)
        transitions = self.state.drain_return_home_transitions()
        self.assertEqual(len(transitions), 1)
        self.assertFalse(transitions[0].active)
        self.assertEqual(
            transitions[0].instance_id,
            self.state._return_home_instance_id(actor_id),
        )

    def test_return_home_navigation_failure_cannot_leave_permanent_immunity(self):
        actor_id = self.encounter.actor_ids[0]
        monster = self.state.monsters[actor_id]
        self.state.monster_positions[actor_id] = (
            monster.position[0] + 2.0,
            monster.position[1],
            monster.position[2],
        )
        self.state.monster_health[actor_id] = 1
        self.state._set_returning_home(actor_id, True, 400)
        self.state.returning_home_started_at[actor_id] = (
            100.0 - MONSTER_RETURN_HOME_TIMEOUT_SECONDS - 0.1
        )

        movements = self.state.advance_monsters(
            self._wire(monster.position),
            elapsed_seconds=0.0,
            server_tick=401,
            now=100.0,
        )

        self.assertNotIn(actor_id, self.state.returning_home_actor_ids)
        self.assertEqual(
            self.state.monster_positions[actor_id], monster.position
        )
        self.assertEqual(
            self.state.monster_health[actor_id], monster.maximum_health
        )
        self.assertTrue(
            any(value.actor_id == actor_id for value in movements)
        )

    def test_monster_chase_grace_does_not_form_a_hard_patrol_line(self):
        actor_id = self.encounter.actor_ids[0]
        monster = self.state.monsters[actor_id]
        self.state.monster_positions[actor_id] = (
            monster.position[0] + monster.travel_distance + 2.0,
            monster.position[1],
            monster.position[2],
        )
        nearby_player = self._wire((
            monster.position[0] + monster.travel_distance + 3.0,
            monster.position[1],
            monster.position[2],
        ))

        self.state.advance_monsters(
            nearby_player, elapsed_seconds=0.04, server_tick=350
        )

        self.assertNotIn(actor_id, self.state.returning_home_actor_ids)

    def test_monster_pursues_past_its_authored_travel_distance(self):
        """Pursuit is no longer clamped to a sphere around the spawn.

        The goal used to be projected onto the pursuit radius, so a monster
        whose player stood outside its territory walked to that surface and
        stopped. Packs collected along the boundary doing nothing.
        """

        # Straight-line navigation so the assertion is about the leash policy
        # rather than about where this map's mesh happens to allow walking.
        encounter = replace(
            self.encounter,
            navigation=_DirectNavigationMesh(),
            terrain=None,
        )
        state = LocalCombatState(
            encounter, player_actor_id=1, player_health=1000
        )
        actor_id = encounter.actor_ids[0]
        monster = state.monsters[actor_id]
        pursuit_radius = (
            monster.travel_distance + MONSTER_CHASE_GRACE_DISTANCE
        )
        # Start just inside the pursuit sphere with the player just outside
        # it, close enough that the actor has not been outrun.
        state.monster_positions[actor_id] = (
            monster.position[0] + pursuit_radius - 3.0,
            monster.position[1],
            monster.position[2],
        )
        player_offset = pursuit_radius + 5.0
        self.assertLess(
            player_offset - (pursuit_radius - 3.0),
            monster.escape_range,
            "fixture must keep the player inside escape range",
        )
        player = encounter.coordinate_frame.world_to_wire((
            monster.position[0] + player_offset,
            monster.position[1],
            monster.position[2],
        ))

        for tick in range(40):
            state.advance_monsters(
                player, elapsed_seconds=0.2, server_tick=360 + tick
            )

        self.assertNotIn(
            actor_id,
            state.returning_home_actor_ids,
            "the monster gave up while the player was still reachable",
        )
        self.assertGreater(
            self._distance_2d(
                state.monster_positions[actor_id], monster.position
            ),
            pursuit_radius,
            "pursuit was pinned to the sphere around the spawn",
        )

    def test_monster_dragged_past_the_reset_radius_snaps_to_spawn(self):
        """The hard leash resets in place instead of walking back."""

        actor_id = self.encounter.actor_ids[0]
        monster = self.state.monsters[actor_id]
        reset_radius = (
            monster.travel_distance
            + MONSTER_CHASE_GRACE_DISTANCE
            + MONSTER_RESET_GRACE_DISTANCE
        )
        self.state.monster_positions[actor_id] = (
            monster.position[0] + reset_radius + 5.0,
            monster.position[1],
            monster.position[2],
        )
        self.state.monster_health[actor_id] = 1
        self.state.monster_moving[actor_id] = True
        # "debuff_cc_frost" is KeepOnWayHome=0 and must be dropped;
        # "player_immunities" is KeepOnWayHome=1 and must survive.
        for resource_id in ("debuff_cc_frost", "player_immunities"):
            self.state.monster_status_effects[actor_id][resource_id] = (
                VictimStatusEffectDefinition(
                    resource_id=resource_id,
                    effect_id=1,
                    chance=1.0,
                    duration_ticks=250,
                    values=(0.0,) * 5,
                    trigger="hit",
                ),
                1e18,
            )
        player = self._wire((
            monster.position[0] + reset_radius + 6.0,
            monster.position[1],
            monster.position[2],
        ))

        movements = self.state.advance_monsters(
            player, elapsed_seconds=0.04, server_tick=361
        )

        self.assertEqual(
            self.state.monster_positions[actor_id],
            monster.position,
            "the monster did not snap back to its spawn",
        )
        self.assertEqual(
            self.state.monster_health[actor_id], monster.maximum_health
        )
        self.assertFalse(self.state.monster_moving[actor_id])
        self.assertNotIn(
            actor_id,
            self.state.returning_home_actor_ids,
            "a reset monster is home, so it must not stay in the walk-back set",
        )
        self.assertEqual(
            set(self.state.monster_status_effects[actor_id]),
            {"player_immunities"},
            "the reset must drop only the effects KeepOnWayHome clears",
        )
        self.assertTrue(
            any(
                movement.actor_id == actor_id
                and movement.world_position == monster.position
                for movement in movements
            ),
            "the snap was not published to viewers",
        )

    def test_reset_raises_and_clears_the_return_home_effect(self):
        """The snap still carries the database's MonsterReturnHome effect."""

        actor_id = self.encounter.actor_ids[0]
        monster = self.state.monsters[actor_id]
        reset_radius = (
            monster.travel_distance
            + MONSTER_CHASE_GRACE_DISTANCE
            + MONSTER_RESET_GRACE_DISTANCE
        )
        self.state.monster_positions[actor_id] = (
            monster.position[0] + reset_radius + 5.0,
            monster.position[1],
            monster.position[2],
        )
        player = self._wire((
            monster.position[0] + reset_radius + 6.0,
            monster.position[1],
            monster.position[2],
        ))

        self.state.advance_monsters(
            player, elapsed_seconds=0.04, server_tick=362
        )

        transitions = self.state.drain_return_home_transitions()
        self.assertEqual(
            [transition.active for transition in transitions],
            [True, False],
            "a reset must raise and then clear MonsterReturnHome",
        )
        for transition in transitions:
            self.assertEqual(
                transition.effect_id, MONSTER_RETURN_HOME_EFFECT_ID
            )
            self.assertEqual(
                transition.instance_id,
                self.state._return_home_instance_id(actor_id),
            )

    def test_return_home_effect_is_resolved_from_the_static_database(self):
        """The wire id is a row index, so it must come from the database."""

        effect_id, duration_ticks = self.state._return_home_effect()

        self.assertEqual(
            effect_id,
            MONSTER_RETURN_HOME_EFFECT_ID,
            "the audited fallback no longer matches MonsterReturnHome's row",
        )
        self.assertGreater(duration_ticks, 0)

    def test_monster_entering_waypoint_zone_returns_home_immediately(self):
        actor_id = self.encounter.actor_ids[0]
        monster = self.state.monsters[actor_id]
        protected_position = (
            monster.position[0] + 3.0,
            monster.position[1],
            monster.position[2],
        )
        encounter = replace(
            self.encounter,
            protected_waypoints=((protected_position, 2.0),),
        )
        state = LocalCombatState(
            encounter, player_actor_id=1, player_health=1000
        )
        state.monster_positions[actor_id] = protected_position

        state.advance_monsters(
            self._wire((protected_position[0] + 1.0,) + protected_position[1:]),
            elapsed_seconds=0.04,
            server_tick=351,
        )

        self.assertIn(actor_id, state.returning_home_actor_ids)

    def test_player_inside_waypoint_zone_cannot_attract_monster_to_edge(self):
        actor_id = self.encounter.actor_ids[0]
        monster = self.state.monsters[actor_id]
        protected_position = (
            monster.position[0] + 3.0,
            monster.position[1],
            monster.position[2],
        )
        encounter = replace(
            self.encounter,
            protected_waypoints=((protected_position, 2.0),),
        )
        state = LocalCombatState(
            encounter, player_actor_id=1, player_health=1000
        )

        movements = state.advance_monsters(
            encounter.coordinate_frame.world_to_wire(protected_position),
            elapsed_seconds=0.25,
            server_tick=352,
        )

        self.assertEqual(movements, ())
        self.assertEqual(state.monster_positions[actor_id], monster.position)
        self.assertNotIn(actor_id, state.returning_home_actor_ids)

    def test_protected_authored_home_never_flaps_return_home_effect(self):
        actor_id = self.encounter.actor_ids[0]
        monster = self.state.monsters[actor_id]
        encounter = replace(
            self.encounter,
            protected_waypoints=((monster.position, 2.0),),
        )
        state = LocalCombatState(
            encounter, player_actor_id=1, player_health=1000
        )
        player_wire = encounter.coordinate_frame.world_to_wire(
            (monster.position[0] + 1.0,) + monster.position[1:]
        )

        for tick in range(400, 408):
            self.assertEqual(
                state.advance_monsters(
                    player_wire, elapsed_seconds=0.04, server_tick=tick
                ),
                (),
            )

        self.assertNotIn(actor_id, state.returning_home_actor_ids)
        self.assertEqual(state.drain_return_home_transitions(), ())

    def test_banner_of_war_applies_utility_and_enemy_count_buffs(self):
        with sqlite3.connect(self.state.encounter.static_database_path) as db:
            index = db.execute(
                "SELECT rowid - 1 FROM _Template_Skill WHERE Id='defiance'"
            ).fetchone()[0]
        effects = self.state.player_skill_location_owner_effects(
            index,  # defiance / Banner of War
            cast_id=4401,
            target_count=3,
        )
        resource_ids = [effect.resource_id for effect in effects]

        self.assertEqual(
            resource_ids.count("skill_defiance_buff_skill_damage"), 3
        )
        # The utility buffs are held inside the arena only
        # (test_banner_of_war_buffs_its_warrior_only_inside_the_arena).
        self.assertNotIn("skill_defiance_buff_resource_reg", resource_ids)
        self.assertNotIn("skill_defiance_buff_skill_costs", resource_ids)
        damage_effect = next(
            effect
            for effect in effects
            if effect.resource_id == "skill_defiance_buff_skill_damage"
        )
        self.assertEqual(damage_effect.max_stack_size, 10)
        self.assertEqual(damage_effect.values[1:3], (0.02, 0.05))

    def test_normal_boss_rotates_all_supported_authored_skills(self):
        encounter = load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH,
            "a0204_boss",
            include_spawn_areas=False,
            # The boss waits behind the BOSS_spawn EventSet, which the arena
            # trigger starts at run time; pre-enable it here.
            active_event_flags=frozenset({"BOSS_spawn"}),
        )
        state = LocalCombatState(
            encounter, player_actor_id=1, player_health=1000
        )
        actor_id = encounter.actor_ids[0]
        monster = state.monsters[actor_id]
        target = encounter.coordinate_frame.world_to_wire((
            monster.position[0] + 1.0,
            monster.position[1],
            monster.position[2],
        ))
        # Every 2 s for 40 s: the specials come when their cooldown allows
        # (double strike 5 s, missile 20 s), the plain strike fills the rest.
        used = []
        for ordinal in range(20):
            now = 1000.0 + ordinal * 2.0
            state.pending_monster_attacks.clear()
            state.last_monster_attack_at[actor_id] = 0.0
            attacks = state.maybe_start_monster_attacks(
                target, server_tick=1000 + ordinal * 50, now=now,
            )
            # The previous skill's own lock can still hold it.
            used.extend((now, attack.definition.resource_id) for attack in attacks)

        names = [name for _now, name in used]
        self.assertEqual(
            set(names),
            {"heredur_base_strike", "heredur_base_doublestrike", "heredur_base_missile",
             "heredur_summon_01"},
        )
        for skill, cooldown in (("heredur_base_doublestrike", 5.0), ("heredur_base_missile", 20.0)):
            times = [now for now, name in used if name == skill]
            self.assertTrue(all(b - a >= cooldown for a, b in zip(times, times[1:])))
        self.assertEqual(names.count("heredur_base_missile"), 2)

    def test_monster_skills_wait_their_initial_cooldown_and_match_difficulty(self):
        actor_id = sorted(self.state.monsters)[0]
        base = self.state.monsters[actor_id].attack
        plain = replace(base, resource_id="plain", cooldown=0.0, initial_cooldown=(0.0, 0.0), cooldown_category="")
        hounds = replace(base, resource_id="hounds", cooldown=35.0, initial_cooldown=(3.0, 5.0), cooldown_category="Skill05")
        hard = replace(base, resource_id="hard_only", cooldown=10.0, cooldown_category="Skill06", min_difficulty=4)
        skills = (plain, hounds, hard)
        choose = lambda now: self.state._monster_skill_choice(actor_id, skills, skills, 0, now).resource_id
        self.assertEqual(choose(100.0), "plain")  # hounds wait 3-5 s; hard_only is not this difficulty
        self.assertEqual(choose(105.1), "hounds")
        self.state._monster_skill_ready_at[actor_id]["Skill05"] = 140.1
        self.assertEqual(choose(106.0), "plain")

    def test_monster_self_buff_puts_its_effect_on_the_caster_and_strikes_nothing(self):
        from pathlib import Path
        from dso_server.gameplay import local_monster_state
        attacks = local_monster_state._load_runtime_template(
            Path(DEFAULT_STATIC_DB_PATH), "boss_heredur_scaling")[0]
        enrage = next(a for a in attacks if a.resource_id == "heredur_bonus_enrage")
        self.assertTrue(enrage.self_buff)
        self.assertFalse(attacks[0].self_buff)
        actor_id = sorted(self.state.monsters)[0]
        monster = self.state.monsters[actor_id]
        self.state.monsters[actor_id] = replace(monster, attacks=(enrage,), attack=enrage)
        self.state._monster_skill_ready_at[actor_id] = {}
        self.state.last_monster_attack_at[actor_id] = 0.0
        position = self.state.monster_positions[actor_id]
        target = self._wire((position[0] + 1.0, position[1], position[2]))
        attacks = self.state.maybe_start_monster_attacks(target, server_tick=500, now=100.0)
        self.assertEqual([a.definition.resource_id for a in attacks], ["heredur_bonus_enrage"])
        self.assertIn("enrage", self.state.monster_status_effects.get(actor_id, {}))
        self.assertEqual(
            self.state.resolve_due_monster_attacks(target, now=attacks[0].due_at + 1.0), ())

    def test_player_summon_can_damage_only_explicit_hostile_player(self):
        definition = self.state.player_summon_definition(1877)
        owner_actor_id = 0x000103C1
        hostile_actor_id = 0x000103C2
        target_position = self.state.monster_positions[
            self.encounter.actor_ids[0]
        ]
        self.state.register_player(
            hostile_actor_id, 1000, current_health=1000
        )
        spawn = self.state.spawn_player_summon(
            definition,
            owner_actor_id=owner_actor_id,
            position=(
                target_position[0] - 0.5,
                target_position[1],
                target_position[2],
            ),
            yaw=0.0,
            level=100,
            server_tick=500,
            player_damage_range=(100.0, 100.0),
            now=100.0,
        )
        summon_position = spawn.actors[0].position
        for monster_actor_id in self.state.monster_health:
            self.state.monster_health[monster_actor_id] = 0
        hostile_position = (
            summon_position[0] + 0.5,
            summon_position[1],
            summon_position[2],
        )
        player_positions = {
            owner_actor_id: summon_position,
            hostile_actor_id: hostile_position,
        }
        hostility = {owner_actor_id: frozenset({hostile_actor_id})}

        attacks = self.state.maybe_start_player_summon_attacks(
            player_positions,
            server_tick=600,
            now=110.0,
            hostile_player_actor_ids_by_owner=hostility,
        )
        self.assertTrue(attacks)
        self.assertTrue(all(
            attack.target_actor_id == hostile_actor_id for attack in attacks
        ))
        due_at = max(
            pending.attack.due_at
            for pending in self.state.pending_player_summon_attacks.values()
        )
        resolutions, _support = (
            self.state.resolve_due_player_summon_attacks(
                now=due_at + 0.01,
                player_positions=player_positions,
                server_tick=601,
                hostile_player_actor_ids_by_owner=hostility,
                player_damage_multipliers={hostile_actor_id: 0.5},
            )
        )

        self.assertTrue(resolutions)
        self.assertTrue(all(
            value.target_actor_id == hostile_actor_id
            for value in resolutions
        ))
        self.assertLess(
            self.state.player_health[hostile_actor_id][0], 1000
        )

    def test_wild_pack_wolves_move_and_attack_nearby_monsters(self):
        definition = self.state.player_summon_definition(1877)
        self.assertEqual(definition.amount, 3)
        self.assertEqual(definition.attack_skill_type, 'MeleeTarget')
        self.assertAlmostEqual(definition.move_speed, 6.5)
        self.assertAlmostEqual(definition.aggro_range, 20.0)
        target_actor_id = self.encounter.actor_ids[0]
        target = self.state.monster_positions[target_actor_id]
        spawn = self.state.spawn_player_summon(
            definition,
            owner_actor_id=0x000103C1,
            position=(target[0] - 8.0, target[1], target[2]),
            yaw=0.0,
            level=100,
            server_tick=500,
            player_damage_range=(100.0, 100.0),
            now=100.0,
        )

        movements = self.state.advance_player_summons(
            elapsed_seconds=0.5, server_tick=501
        )

        self.assertEqual(
            {value.actor_id for value in movements},
            {value.actor_id for value in spawn.actors},
        )
        for summon in spawn.actors:
            self.state.player_summons[summon.actor_id] = replace(
                self.state.player_summons[summon.actor_id],
                position=(target[0] - 1.0, target[1], target[2]),
            )
        attacks = self.state.maybe_start_player_summon_attacks(
            {}, server_tick=502, now=101.0
        )
        self.assertEqual(len(attacks), 3)
        self.assertTrue(
            all(value.command_id == SKILL_COMMAND for value in attacks)
        )

    def test_dwarf_turrets_are_stationary_despite_template_run_speed(self):
        definition = self.state.player_summon_definition(1892)
        self.assertTrue(definition.stationary)
        self.assertGreater(definition.move_speed, 0.0)
        target = self.state.monster_positions[self.encounter.actor_ids[0]]
        spawn = self.state.spawn_player_summon(
            definition,
            owner_actor_id=0x000103C1,
            position=(target[0] - 8.0, target[1], target[2]),
            yaw=0.0,
            level=100,
            server_tick=550,
            player_damage_range=(100.0, 100.0),
            now=100.0,
        )
        original_positions = {
            summon.actor_id: summon.position for summon in spawn.actors
        }

        movements = self.state.advance_player_summons(
            elapsed_seconds=0.5, server_tick=551
        )

        self.assertEqual(movements, ())
        self.assertEqual(
            {
                actor_id: self.state.player_summons[actor_id].position
                for actor_id in original_positions
            },
            original_positions,
        )

    def test_fury_of_the_dragon_puts_its_crack_ahead_of_the_warrior(self):
        """earthquake: every LocationStatusEffect carries OZ:-2.5."""
        with sqlite3.connect(self.state.encounter.static_database_path) as db:
            index = db.execute(
                "SELECT rowid - 1 FROM _Template_Skill WHERE Id='earthquake'"
            ).fetchone()[0]
        # Its ground effects as the cast places them (the essence variants,
        # chance 0, carry no offset).
        effects = tuple(e for e in self.state.player_skill_location_visuals(index, modifier_sources=())
                        if e.chance > 0.0)
        self.assertTrue(effects)
        self.assertTrue(all(e.offset == (0.0, 0.0, -2.5) for e in effects))
        # Placed once, 2.5 m ahead along the aim (forward (-sin, 0, -cos)).
        for yaw, ahead in ((0.0, (10.0, 0.0, 17.5)), (-math.pi / 2, (12.5, 0.0, 20.0))):
            visuals = self.state.start_player_location_visuals(
                effects[:1], position=(10.0, 0.0, 20.0), yaw=yaw,
                server_tick=100, owner_actor_id=1,
            )
            placed = max(visuals, key=lambda v: v.instance_id).position
            for got, want in zip(placed, ahead):
                self.assertAlmostEqual(got, want, places=4)

    def test_a_mind_controlled_monster_walks_to_another_monster(self):
        """skill_mindcontrol_effect: it turns on the nearest uncontrolled
        monster instead of chasing the player."""
        state, frame = self.state, self.encounter.coordinate_frame
        controlled, other = sorted(state.living_actor_ids)[:2]
        origin = state.monster_positions[controlled]
        navigation = self.encounter.navigation
        spot = None
        for angle in range(0, 360, 15):
            candidate = (origin[0] + 7.0 * math.cos(math.radians(angle)), origin[1],
                         origin[2] + 7.0 * math.sin(math.radians(angle)))
            try:
                candidate = navigation.project_position(candidate, max_horizontal_distance=0.5)
            except Exception:
                continue
            if navigation.is_reachable(origin, candidate):
                spot = candidate
                break
        self.assertIsNotNone(spot)
        state.monster_positions[other] = spot
        now = 1000.0
        state.monster_status_effects[controlled] = {
            "skill_mindcontrol_effect": (
                VictimStatusEffectDefinition(
                    resource_id="skill_mindcontrol_effect", effect_id=0, chance=1.0,
                    duration_ticks=250, values=(), trigger="",
                ),
                now + 10.0,
            )
        }
        before = self._distance_2d(origin, spot)
        # The player stands on the opposite side, within reach.
        player = frame.world_to_wire(tuple(o + (o - t) for o, t in zip(origin, spot)))
        for step in range(25):
            state.advance_monsters(player, elapsed_seconds=0.1, server_tick=100 + step,
                                   eligible_actor_ids={controlled}, now=now + step * 0.1)
        after = self._distance_2d(state.monster_positions[controlled], spot)
        self.assertLess(after, before - 1.0)

    def test_an_area_impact_spares_a_monster_that_walked_out_of_it(self):
        """A meteor's victims are chosen at the cast and hit 2.6 s later."""
        from dso_server.gameplay.local_combat_state import PendingPlayerHit
        state = self.state
        actor = sorted(state.living_actor_ids)[0]
        point = state.monster_positions[actor]

        def pending(cast_id):
            hit = PendingPlayerHit(
                cast_id=cast_id, target_actor_id=actor, skill_index=0, due_at=10.0,
                impact_tick=1, distance=0.0, travel_distance=0.0, emit_delay=0.0,
                victim_status_effects=(), origin=point, motion_type="Area",
                damage_modifier=1.0, player_minimum_damage=10.0,
                player_maximum_damage=10.0, area_radius=3.5,
            )
            state.pending_runtime_player_hits[(1, cast_id, 0)] = hit

        pending(1)
        self.assertIsNotNone(state.resolve_runtime_player_hit(1, 1, 0, now=10.0))
        state.monster_positions[actor] = (point[0] + 8.0, point[1], point[2])
        pending(2)
        self.assertIsNone(state.resolve_runtime_player_hit(1, 2, 0, now=10.0))
        self.assertNotIn((1, 2, 0), state.pending_runtime_player_hits)

    def test_banner_of_war_buffs_its_warrior_only_inside_the_arena(self):
        """skill_defiance_buff_creators_aura (AuraTargets:Creator, 5.75 m)
        gives skill_defiance_buff_skill_costs / _resource_reg to the warrior
        while inside, and takes them back on leaving."""
        state = self.state
        with sqlite3.connect(state.encounter.static_database_path) as db:
            index = db.execute(
                "SELECT rowid - 1 FROM _Template_Skill WHERE Id='defiance'"
            ).fetchone()[0]
        at_cast = {e.resource_id for e in state.player_skill_location_owner_effects(
            index, cast_id=1, target_count=0)}
        self.assertNotIn("skill_defiance_buff_skill_costs", at_cast)
        visuals = [v for v in state.player_skill_location_visuals(index)
                   if v.resource_id == "skill_defiance_buff_creators_aura"]
        self.assertEqual(len(visuals), 1)
        banner = (10.0, 0.0, 10.0)
        state.start_player_location_visuals(
            tuple(visuals), position=banner, yaw=0.0, server_tick=100,
            owner_actor_id=7, cast_id=1, source_skill_index=index, now=50.0)
        _, inside = state.apply_player_location_auras(
            now=51.0, server_tick=125, player_positions={7: (12.0, 0.0, 10.0)})
        given = {e.resource_id for a in inside if not a.remove for e in a.status_effects}
        self.assertIn("skill_defiance_buff_skill_costs", given)
        self.assertIn("skill_defiance_buff_resource_reg", given)
        _, outside = state.apply_player_location_auras(
            now=52.0, server_tick=150, player_positions={7: (30.0, 0.0, 10.0)})
        taken = {e.resource_id for a in outside if a.remove for e in a.status_effects}
        self.assertEqual(taken, given)

    def test_mage_guardians_never_walk(self):
        """arcanemanifestation and firemanifestation: their permanent idle effect
        switches Movement off, though the template has a RunSpeed."""
        with sqlite3.connect(self.state.encounter.static_database_path) as db:
            indices = [
                db.execute(
                    "SELECT rowid - 1 FROM _Template_Skill WHERE Id=?", (skill,)
                ).fetchone()[0]
                for skill in ("arcanemanifestation", "firemanifestation")
            ]
        target = self.state.monster_positions[self.encounter.actor_ids[0]]
        for index in indices:
            definition = self.state.player_summon_definition(index)
            self.assertTrue(definition.movement_locked)
            self.assertFalse(definition.stationary)
            self.assertGreater(definition.move_speed, 0.0)
            spawn = self.state.spawn_player_summon(
                definition,
                owner_actor_id=0x000103C1,
                position=(target[0] - 8.0, target[1], target[2]),
                yaw=0.0,
                level=100,
                server_tick=550,
                player_damage_range=(100.0, 100.0),
                now=100.0,
            )
            before = {s.actor_id: s.position for s in spawn.actors}
            movements = self.state.advance_player_summons(
                elapsed_seconds=5.0, server_tick=551, now=200.0
            )
            self.assertEqual(movements, ())
            self.assertEqual(
                {a: self.state.player_summons[a].position for a in before},
                before,
            )

    def test_tree_of_life_heals_owner_and_taunts_nearby_monsters(self):
        definition = self.state.player_summon_definition(1881)
        self.assertAlmostEqual(definition.heal_fraction, 0.05)
        self.assertAlmostEqual(definition.heal_radius, 10.0)
        self.assertAlmostEqual(definition.taunt_radius, 20.0)
        self.assertTrue(definition.support_status_effects)
        owner_actor_id = 0x000103C1
        target = self.state.monster_positions[self.encounter.actor_ids[0]]
        spawn = self.state.spawn_player_summon(
            definition,
            owner_actor_id=owner_actor_id,
            position=target,
            yaw=0.0,
            level=100,
            server_tick=600,
            player_damage_range=(100.0, 100.0),
            now=200.0,
        )
        self.state.register_player(owner_actor_id, 1000)
        self.state.player_health[owner_actor_id][0] = 500

        taunt = self.state.active_player_summon_taunt(now=200.0)
        damage, support = self.state.resolve_due_player_summon_attacks(
            now=spawn.actors[0].spawn_ready_at,
            player_positions={owner_actor_id: target},
            server_tick=601,
        )

        self.assertEqual(taunt.actor_id, spawn.actors[0].actor_id)
        self.assertEqual(damage, ())
        self.assertEqual(len(support), 1)
        self.assertEqual(self.state.player_health[owner_actor_id][0], 550)

    def test_dwarf_turret_definition_uses_authored_summon_fields(self):
        definition = self.state.player_summon_definition(1890)

        self.assertIsNotNone(definition)
        self.assertEqual(definition.skill_resource_id, "TurretCombat")
        self.assertEqual(
            definition.monster_template_id, "minion_combatTurret"
        )
        self.assertEqual(
            definition.client_monster_template_id,
            "minion_combatTurret",
        )
        self.assertEqual(definition.amount, 1)
        self.assertEqual(definition.limit, 1)
        self.assertEqual(definition.category, "Skill02")
        self.assertAlmostEqual(definition.spawn_delay_seconds, 8 / 25)
        self.assertEqual(definition.attack_skill_database_index, 1932)
        self.assertEqual(definition.attack_skill_index, 1932)
        self.assertEqual(
            definition.aura.resource_id,
            "skill_turretcombat_minion_buff_aura",
        )
        self.assertEqual(definition.aura.effect_id, 5452)
        self.assertEqual(definition.aura.radius, 60.0)
        self.assertEqual(definition.aura.height, 100.0)
        self.assertEqual(
            tuple(
                effect.resource_id
                for effect in definition.location_visual_effects
            ),
            ("skill_turretcombat_minion_despawn_aura",),
        )

    def test_turret_playback_indexes_match_bundled_client_templates(self):
        expected = {
            1890: (
                "minion_combatTurret",
                "minion_combatTurret",
                "minionskill_combatturret",
                1932,
                1932,
            ),
            1891: (
                "minion_teslaturret",
                "minion_teslaturret",
                "minionskill_teslaturret",
                1930,
                1930,
            ),
            1892: (
                "minion_machinegunturret",
                "minion_machinegunturret",
                "minionskill_machinegunturret",
                1929,
                1929,
            ),
            1895: (
                "minion_coveringfireturret",
                "minion_coveringfireturret",
                "minionskill_coveringfireturret",
                1931,
                1931,
            ),
        }
        with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as database:
            for cast_index, (
                monster_template,
                client_monster_template,
                playback_resource,
                source_index,
                playback_index,
            ) in expected.items():
                definition = self.state.player_summon_definition(cast_index)
                self.assertEqual(
                    definition.monster_template_id, monster_template
                )
                self.assertEqual(
                    definition.client_monster_template_id,
                    client_monster_template,
                )
                self.assertEqual(
                    definition.attack_skill_database_index, source_index
                )
                self.assertEqual(
                    definition.attack_skill_index, playback_index
                )
                resolved = database.execute(
                    "SELECT Id FROM _Template_Skill WHERE rowid=?",
                    (playback_index + 1,),
                ).fetchone()
                self.assertEqual(resolved, (playback_resource,))
                monster_row = database.execute(
                    """
                    SELECT Skills, Graphics
                    FROM _Template_Monster
                    WHERE Id=?
                    """,
                    (client_monster_template,),
                ).fetchone()
                if cast_index == 1892:
                    # This client does not parse a comma-joined compatibility
                    # list here. It must resolve one native skill resource;
                    # otherwise the actor later calls GetSkillInfo(-1).
                    self.assertEqual(monster_row[0], playback_resource)
                else:
                    self.assertIn(playback_resource, monster_row[0])

    def test_player_turret_spawn_has_friendly_owner_and_category_lifecycle(self):
        definition = self.state.player_summon_definition(1890)
        position = self.state.monster_positions[self.encounter.actor_ids[0]]

        first = self.state.spawn_player_summon(
            definition,
            owner_actor_id=0x000103C1,
            position=position,
            yaw=0.5,
            level=100,
            server_tick=500,
            player_damage_range=(100.0, 200.0),
            now=100.0,
        )
        monster = first.actors[0].spawn_group.spawn_batch.commands[0].value
        status = first.actors[0].spawn_group.spawn_batch.commands[1].value

        self.assertEqual(monster.category, 1)
        self.assertEqual(monster.field_d8, 0x000103C1)
        self.assertEqual(monster.placement.field_00, 0x00010004)
        self.assertEqual(monster.placement.field_08, 0)
        # NewMonster.field_e0 is indexed directly into the client's fixed
        # PvP TeamAttributes array. PvE summons must use InvalidTeamId.
        self.assertEqual(monster.field_e0, 0xFFFFFFFF)
        self.assertEqual(monster.field_c4, 500)
        self.assertTrue(status.clear_existing)
        self.assertEqual(status.entries[0].effect_id, 5452)
        self.assertEqual(status.entries[0].field_08, 501)
        self.assertEqual(status.entries[0].field_10, 12)
        self.assertEqual(status.entries[0].field_20, 0x000103C1)
        self.assertEqual(
            status.entries[0].shape_values,
            (60.0, 100.0, 0.5, *monster.position),
        )
        self.assertTrue(
            all(entry.field_08 == 500 for entry in status.entries[1:])
        )

        second = self.state.spawn_player_summon(
            definition,
            owner_actor_id=0x000103C1,
            position=position,
            yaw=0.5,
            level=100,
            server_tick=510,
            player_damage_range=(100.0, 200.0),
            now=101.0,
        )
        self.assertEqual(
            second.discarded_actor_ids,
            (first.actors[0].actor_id,),
        )
        self.assertNotIn(
            first.actors[0].actor_id, self.state.player_summons
        )
        self.assertIn(second.actors[0].actor_id, self.state.player_summons)

    def test_combat_turret_autonomously_casts_and_damages_nearest_monster(self):
        definition = self.state.player_summon_definition(1890)
        target_actor_id = self.encounter.actor_ids[0]
        target_position = self.state.monster_positions[target_actor_id]
        spawn = self.state.spawn_player_summon(
            definition,
            owner_actor_id=0x000103C1,
            position=(target_position[0] - 3.0, *target_position[1:]),
            yaw=0.0,
            level=100,
            server_tick=500,
            player_damage_range=(100.0, 100.0),
            now=100.0,
        )

        attacks = self.state.maybe_start_player_summon_attacks(
            {},
            server_tick=526,
            now=spawn.actors[0].attack_ready_at,
        )

        self.assertEqual(len(attacks), 1)
        self.assertEqual(attacks[0].attacker_actor_id, spawn.actors[0].actor_id)
        self.assertEqual(attacks[0].target_actor_id, target_actor_id)
        self.assertEqual(attacks[0].command_id, BULLET_SKILL_COMMAND)
        health_before = self.state.monster_health[target_actor_id]
        damage, support = self.state.resolve_due_player_summon_attacks(
            now=attacks[0].due_at
        )
        self.assertEqual(support, ())
        self.assertEqual(len(damage), 1)
        self.assertEqual(damage[0].damage, 120)
        self.assertEqual(
            self.state.monster_health[target_actor_id],
            health_before - 120,
        )

    def test_covering_fire_turret_targets_owner_with_database_buffs(self):
        definition = self.state.player_summon_definition(1895)
        owner_actor_id = 0x000103C1
        owner_position = self.state.monster_positions[
            self.encounter.actor_ids[0]
        ]
        self.state.spawn_player_summon(
            definition,
            owner_actor_id=owner_actor_id,
            position=owner_position,
            yaw=0.0,
            level=100,
            server_tick=600,
            player_damage_range=(100.0, 100.0),
            now=200.0,
        )

        attacks = self.state.maybe_start_player_summon_attacks(
            {owner_actor_id: owner_position},
            server_tick=626,
            now=200.0 + definition.startup_duration_seconds,
        )
        self.assertEqual(len(attacks), 1)
        self.assertEqual(attacks[0].command_id, TARGET_BULLET_SKILL_COMMAND)
        damage, support = self.state.resolve_due_player_summon_attacks(
            now=attacks[0].due_at
        )
        self.assertEqual(damage, ())
        self.assertEqual(len(support), 1)
        self.assertEqual(support[0].target_actor_id, owner_actor_id)
        self.assertEqual(
            {
                effect.resource_id
                for effect in support[0].status_effects
            },
            {
                "minionskill_coveringfireturret_buff_armor",
                "minionskill_coveringfireturret_buff_resistance",
                "minionskill_coveringfireturret_buff_attackspeed",
            },
        )

    def test_machine_gun_turret_uses_typed_sustained_command(self):
        definition = self.state.player_summon_definition(1892)
        self.assertEqual(definition.attack_skill_database_index, 1929)
        self.assertEqual(definition.attack_skill_index, 1929)
        self.assertEqual(
            definition.client_monster_template_id,
            "minion_machinegunturret",
        )
        self.assertEqual(
            tuple(
                effect.resource_id
                for effect in definition.location_visual_effects
            ),
            ("dwarf_summon_visualizer_MachineGunTurret",),
        )
        self.assertEqual(
            definition.location_visual_effects[0].duration_ticks,
            500,
        )
        self.assertTrue(definition.attack_playback_enabled)
        self.assertEqual(
            definition.attack_playback_interval_seconds,
            5.0,
        )
        self.assertEqual(
            definition.owner_despawn_range,
            PLAYER_SUMMON_OWNER_RANGE,
        )
        self.assertEqual(definition.despawn_duration_seconds, 3.0)
        actor_ids = self.encounter.actor_ids
        target_position = self.state.monster_positions[actor_ids[0]]
        spawn = self.state.spawn_player_summon(
            definition,
            owner_actor_id=0x000103C1,
            position=(target_position[0] - 3.0, *target_position[1:]),
            yaw=0.0,
            level=100,
            server_tick=650,
            player_damage_range=(2.0, 2.0),
            now=250.0,
        )
        self.assertEqual(
            spawn.actors[0].spawn_group.spawn_batch.commands[
                0
            ].value.template_id,
            "minion_machinegunturret",
        )
        self.assertEqual(len(spawn.actors[0].spawn_groups), 1)

        # Yaw zero faces world -Z. One monster starts inside the authored
        # 2.25-wide fixed cone and the other starts behind the construction.
        origin = spawn.actors[0].position
        front_actor_id, behind_actor_id = actor_ids
        self.state.monster_positions[front_actor_id] = (
            origin[0],
            origin[1],
            origin[2] - 3.0,
        )
        self.state.monster_positions[behind_actor_id] = (
            origin[0],
            origin[1],
            origin[2] + 3.0,
        )
        health_before = {
            actor_id: self.state.monster_health[actor_id]
            for actor_id in actor_ids
        }

        self.assertEqual(
            self.state.maybe_start_player_summon_attacks(
                {},
                server_tick=657,
                now=spawn.actors[0].attack_ready_at - 0.001,
            ),
            (),
        )
        attacks = self.state.maybe_start_player_summon_attacks(
            {},
            server_tick=661,
            now=spawn.actors[0].attack_ready_at + 0.001,
        )

        self.assertEqual(len(attacks), 1)
        sustained = attacks[0]
        self.assertEqual(
            sustained.command_id, SUSTAINED_SKILL_COMMAND
        )
        self.assertEqual(sustained.skill_index, 1929)
        self.assertEqual(sustained.aim_angle, 0.0)
        self.assertEqual(sustained.direction, (0.0, 0.0, -1.0, 0.0))
        self.assertEqual(sustained.loop_start_frame, 15)
        self.assertEqual(sustained.hit_frame, 8)
        self.assertEqual(sustained.motion_unblock_frame, 120)
        self.assertEqual(sustained.skill_unblock_frame, 120)
        self.assertEqual(
            sustained.execution_context_tick,
            661 + 2023,
        )

        class _Session:
            gameplay_pending_monster_movements = {}
            def __init__(self):
                self.sent = []
                self.gameplay_character = SimpleNamespace(character_id=1)
                self.gameplay_last_move = Move.from_values(
                    (7.0, 8.0, 9.0),
                    0.0,
                    1.0,
                    1.0,
                    660,
                    0,
                )

            def send(self, payload, **metadata):
                self.sent.append((payload, metadata))

            def send_reliable_ordered(self, payload, **metadata):
                self.sent.append((payload, metadata))

        session = _Session()
        CharacterServer._send_monster_activity_to(
            session,
            self.state,
            (),
            attacks,
            (),
            (),
        )
        self.assertEqual(len(session.sent), 1)
        encoded, metadata = session.sent[0]
        self.assertEqual(
            metadata["label"], "player-summon-sustained-skill"
        )
        self.assertEqual(encoded[1], SUSTAINED_SUMMON_BATCH_BITS)
        framed = decode_monster_attack_batch(*encoded)
        self.assertEqual(len(framed.movements), 1)
        self.assertEqual(
            framed.movements[0].actor_id,
            LOCAL_PLAYER_ACTOR_ID,
        )
        self.assertNotEqual(
            framed.movements[0].actor_id,
            sustained.attacker_actor_id,
        )
        self.assertEqual(
            framed.movements[0].value.relative_position,
            (7.0, 8.0, 9.0),
        )
        self.assertEqual(
            framed.movements[0].value.start_tick,
            sustained.server_tick - 1,
        )
        self.assertEqual(len(framed.attacks), 1)
        self.assertIsInstance(
            framed.attacks[0], SustainedSkillServerCommand
        )
        self.assertEqual(
            framed.attacks[0].value.skill_index, sustained.skill_index
        )
        self.assertEqual(
            set(
                next(
                    iter(
                        self.state
                        .pending_player_summon_attacks.values()
                    )
                ).target_actor_ids
            ),
            {front_actor_id},
        )
        first_due = sustained.due_at
        damage, support = self.state.resolve_due_player_summon_attacks(
            now=first_due
        )
        self.assertEqual(support, ())
        self.assertEqual(
            {resolution.target_actor_id for resolution in damage},
            {front_actor_id},
        )
        self.assertEqual(damage[0].source_actor_id, 0x000103C1)
        self.assertEqual(damage[0].hit_tags, (1,))
        self.assertLess(
            self.state.monster_health[front_actor_id],
            health_before[front_actor_id],
        )
        self.assertEqual(
            self.state.monster_health[behind_actor_id],
            health_before[behind_actor_id],
        )

        # The sustained animation remains active for 120 frames. Its damage
        # ticks continue without re-sending the 1,026-bit command every tick.
        # Moving the second actor into the fixed strip makes the next
        # authoritative tick include both actors without rotating the turret.
        self.state.monster_positions[behind_actor_id] = (
            origin[0] + 0.5,
            origin[1],
            origin[2] - 5.0,
        )
        playback = self.state.maybe_start_player_summon_attacks(
            {},
            server_tick=660,
            now=first_due + definition.attack_interval_seconds + 0.001,
        )
        self.assertEqual(playback, ())
        self.assertEqual(len(self.state.pending_player_summon_attacks), 1)
        self.assertEqual(
            next(
                iter(self.state.pending_player_summon_attacks.values())
            ).attack.command_id,
            0,
        )
        self.assertEqual(
            set(
                next(
                    iter(
                        self.state
                        .pending_player_summon_attacks.values()
                    )
                ).target_actor_ids
            ),
            set(actor_ids),
        )
        self.assertEqual(
            set(
                self.state.expire_player_summons_for_owner(
                    0x000103C1
                )
            ),
            set(spawn.actors[0].client_actor_ids),
        )
        self.assertEqual(self.state.player_summons, {})

    def test_machine_gun_turret_plays_sustained_fire_without_targets(self):
        definition = self.state.player_summon_definition(1892)
        spawn = self.state.spawn_player_summon(
            definition,
            owner_actor_id=0x000103C1,
            position=self.encounter.trigger_center,
            yaw=0.0,
            level=100,
            server_tick=800,
            player_damage_range=(100.0, 200.0),
            now=400.0,
        )
        origin = spawn.actors[0].position
        for offset, actor_id in enumerate(self.encounter.actor_ids):
            self.state.monster_positions[actor_id] = (
                origin[0] + offset,
                origin[1],
                origin[2] + 20.0,
            )

        attacks = self.state.maybe_start_player_summon_attacks(
            {},
            server_tick=812,
            now=spawn.actors[0].attack_ready_at + 0.001,
        )

        self.assertEqual(len(attacks), 1)
        self.assertEqual(attacks[0].command_id, SUSTAINED_SKILL_COMMAND)
        self.assertEqual(attacks[0].target_actor_id, 0)
        self.assertEqual(self.state.pending_player_summon_attacks, {})

    def test_player_summon_expires_outside_owner_horizontal_range(self):
        definition = self.state.player_summon_definition(1891)
        spawn = self.state.spawn_player_summon(
            definition,
            owner_actor_id=0x000103C1,
            position=self.encounter.trigger_center,
            yaw=0.0,
            level=100,
            server_tick=700,
            player_damage_range=(2.0, 2.0),
            now=300.0,
        )
        summon = spawn.actors[0]
        x, y, z = summon.position
        owner_id = summon.owner_actor_id

        self.assertEqual(
            self.state.player_summons_outside_owner_range(
                {
                    owner_id: (
                        x + definition.owner_despawn_range,
                        y + 500.0,
                        z,
                    )
                }
            ),
            (),
        )
        self.assertEqual(
            self.state.player_summons_outside_owner_range(
                {
                    owner_id: (
                        x + definition.owner_despawn_range + 0.01,
                        y,
                        z,
                    )
                }
            ),
            (summon,),
        )

    def test_player_summon_aura_renews_halfway_through_its_lease(self):
        definition = self.state.player_summon_definition(1892)
        spawn = self.state.spawn_player_summon(
            definition,
            owner_actor_id=0x000103C1,
            position=self.encounter.trigger_center,
            yaw=0.5,
            level=100,
            server_tick=629,
            player_damage_range=(2.0, 2.0),
            now=250.0,
        )
        summon = spawn.actors[0]
        self.assertIsNotNone(summon.initial_aura_instance_id)
        aura_snapshot = next(
            entry
            for command in summon.spawn_group.spawn_batch.commands
            if isinstance(command, StatusEffectServerCommand)
            for entry in command.value.entries
            if entry.effect_id == definition.aura.effect_id
        )
        self.assertEqual(
            aura_snapshot.field_04,
            summon.initial_aura_instance_id,
        )
        self.assertEqual(aura_snapshot.field_08, 630)
        self.assertEqual(aura_snapshot.field_10, 12)

        # The spawn snapshot owns a 12-tick lease beginning at tick 630. The
        # reconstructed transport renews halfway through it, retaining a
        # six-tick safety margin without producing a reliable packet at 25 Hz.
        self.assertEqual(
            self.state.due_player_summon_aura_refreshes(
                server_tick=630
            ),
            (),
        )
        self.assertEqual(
            self.state.due_player_summon_aura_refreshes(
                server_tick=635
            ),
            (),
        )
        refreshes = self.state.due_player_summon_aura_refreshes(
            server_tick=636
        )
        self.assertEqual(len(refreshes), 1)
        refresh = refreshes[0]
        self.assertEqual(refresh.actor_id, summon.actor_id)
        self.assertFalse(refresh.value.clear_existing)
        self.assertEqual(refresh.value.entries[0].field_08, 636)
        self.assertNotEqual(
            refresh.value.entries[0].field_04,
            summon.initial_aura_instance_id,
        )
        self.assertEqual(
            self.state.due_player_summon_aura_refreshes(
                server_tick=636
            ),
            (),
        )

    def test_tesla_empty_sequence_damages_without_invalid_playback(self):
        definition = self.state.player_summon_definition(1891)
        self.assertEqual(
            definition.client_monster_template_id,
            "minion_teslaturret",
        )
        self.assertEqual(definition.attack_skill_index, 1930)
        self.assertFalse(definition.attack_playback_enabled)
        self.assertEqual(definition.attack_initial_delay_seconds, 1.5)

        target_actor_id = self.encounter.actor_ids[0]
        spawn = self.state.spawn_player_summon(
            definition,
            owner_actor_id=0x000103C1,
            position=self.state.monster_positions[target_actor_id],
            yaw=0.0,
            level=100,
            server_tick=700,
            player_damage_range=(2.0, 2.0),
            now=300.0,
        )
        health_before = self.state.monster_health[target_actor_id]

        self.assertEqual(
            self.state.maybe_start_player_summon_attacks(
                {},
                server_tick=701,
                now=301.499,
            ),
            (),
        )
        self.assertEqual(self.state.pending_player_summon_attacks, {})
        effects = self.state.maybe_start_player_summon_attacks(
            {},
            server_tick=739,
            now=301.501,
        )
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0].command_id, 0)
        self.assertEqual(
            tuple(
                effect.resource_id
                for effect in effects[0].user_status_effects
            ),
            ("dwarf_tesla_graphics",),
        )
        graphics = effects[0].user_status_effects[0]
        self.assertEqual(graphics.effect_id, 4536)
        self.assertEqual(graphics.duration_ticks, 250)
        self.assertEqual(graphics.aura_radius, 3.0)
        self.assertEqual(graphics.aura_height, 100.0)
        self.assertEqual(graphics.aura_shape, "Sphere")
        pending = next(
            iter(self.state.pending_player_summon_attacks.values())
        )
        self.assertEqual(pending.attack.command_id, 0)
        damage, support = self.state.resolve_due_player_summon_attacks(
            now=pending.attack.due_at
        )
        self.assertEqual(support, ())
        self.assertTrue(damage)
        self.assertLess(
            self.state.monster_health[target_actor_id],
            health_before,
        )
        # The ten-second looped graphics aura starts once. Subsequent
        # server-side Tesla pulses deal damage without stacking duplicate
        # visual instances or sending the skill's empty execute sequence.
        self.assertEqual(
            self.state.maybe_start_player_summon_attacks(
                {},
                server_tick=759,
                now=302.302,
            ),
            (),
        )
        self.assertEqual(
            next(
                iter(self.state.pending_player_summon_attacks.values())
            ).attack.user_status_effects,
            (),
        )
        self.state.expire_player_summon(spawn.actors[0].actor_id)

    def test_player_location_visuals_are_timed_database_snapshots(self):
        definition = self.state.player_summon_definition(1890)
        visuals = self.state.start_player_location_visuals(
            definition.location_visual_effects,
            position=(1.0, 2.0, 3.0),
            yaw=0.75,
            server_tick=619,
            owner_actor_id=0x00010080,
            now=100.0,
        )

        self.assertEqual(len(visuals), 1)
        visual = visuals[0]
        self.assertEqual(visual.instance_id, 0x00010000)
        self.assertEqual(visual.starts_tick, 619)
        self.assertEqual(visual.expires_tick, 869)
        self.assertEqual(visual.position, (1.0, 2.0, 3.0))
        self.assertEqual(
            self.state.active_player_location_visuals(now=109.99),
            visuals,
        )
        self.assertEqual(
            self.state.active_player_location_visuals(now=110.0),
            (),
        )

    def test_location_visual_matching_removes_only_expired_turret(self):
        machine = self.state.player_summon_definition(1892)
        tesla = self.state.player_summon_definition(1891)
        now = time.perf_counter()
        self.state.start_player_location_visuals(
            machine.location_visual_effects,
            position=self.encounter.trigger_center,
            yaw=0.0,
            server_tick=100,
            owner_actor_id=0x000103C1,
            cast_id=901,
            now=now,
        )
        self.state.start_player_location_visuals(
            tesla.location_visual_effects,
            position=self.encounter.trigger_center,
            yaw=0.0,
            server_tick=101,
            owner_actor_id=0x000103C1,
            cast_id=902,
            now=now,
        )

        remaining = self.state.expire_player_location_visuals_matching(
            0x000103C1,
            {
                effect.resource_id
                for effect in machine.location_visual_effects
            },
            cast_id=901,
        )

        self.assertIsNotNone(remaining)
        self.assertTrue(remaining)
        self.assertTrue(all(
            visual.definition.resource_id
            in {effect.resource_id for effect in tesla.location_visual_effects}
            for visual in remaining
        ))

    def test_departed_owner_location_visuals_are_removed(self):
        definition = self.state.player_summon_definition(1890)
        self.state.start_player_location_visuals(
            definition.location_visual_effects,
            position=(1.0, 2.0, 3.0),
            yaw=0.75,
            server_tick=619,
            owner_actor_id=0x00010080,
        )
        self.state.start_player_location_visuals(
            definition.location_visual_effects,
            position=(4.0, 5.0, 6.0),
            yaw=0.0,
            server_tick=620,
            owner_actor_id=0x00010081,
        )

        remaining = self.state.expire_player_location_visuals_for_owner(
            0x00010080
        )

        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].owner_actor_id, 0x00010081)

    def test_command_aura_numeric_relative_heal_is_authoritative(self):
        receiver = VictimStatusEffectDefinition(
            resource_id='pvp_protection_aura_effect',
            effect_id=5903,
            chance=1.0,
            duration_ticks=90 * 25,
            values=(0.3, 0.0, 0.0, 0.0, 0.0),
            trigger='immediate',
        )

        interval, fraction, count, delay = (
            self.state.player_status_heal_program(receiver)
        )

        self.assertEqual((interval, fraction, count, delay), (1.0, 0.02, 90, 0.0))

    def test_steam_conductor_heal_and_covering_fire_resistance_are_authoritative(
        self,
    ):
        effects = self.state.player_skill_user_effects(
            1893,
            cast_id=700,
            trigger="skillstart",
        )
        healing = next(
            effect
            for effect in effects
            if effect.resource_id
            == "skill_steamconductor_buff_heal_OverTime"
        )
        interval, fraction, count, delay = (
            self.state.player_status_heal_program(healing)
        )
        self.assertEqual((interval, fraction, count, delay), (1.0, 0.02, 5, 0.48))
        self.state.player_health[1][0] = 500
        self.assertEqual(self.state.heal_player_relative(1, fraction), (520, 1000))

        covering = self.state._load_player_skill_geometry(1931)
        armor = next(
            effect
            for effect in covering.victim_status_effects
            if effect.resource_id
            == "minionskill_coveringfireturret_buff_armor"
        )
        source = StatusEffectEntry(
            effect_id=armor.effect_id,
            field_04=1,
            field_0c=0,
            field_14=25,
            field_08=0,
            field_18=0,
            field_10=armor.duration_ticks,
            field_1c=100,
            field_20=1,
            field_25=False,
            field_26=False,
            field_39=False,
            has_details=True,
            values=armor.values,
        )
        self.assertAlmostEqual(
            self.state.player_incoming_damage_multiplier((source,)),
            1.0 / 1.05,
        )

    def test_every_live_player_skill_has_runtime_geometry_and_summons(self):
        """Keep the class-wide skill audit executable as content evolves."""

        expected_counts = {
            "dwarf": 20,
            "mage": 19,
            "ranger": 16,
            "warrior": 16,
        }
        with sqlite3.connect(
            f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True
        ) as database:
            rows = database.execute(
                """
                SELECT rowid - 1, Id, CharClass, COALESCE(Summons, 0)
                FROM _Template_Skill
                WHERE COALESCE(IsLive, 0)=1
                  AND CharClass IN ('dwarf', 'mage', 'ranger', 'warrior')
                ORDER BY CharClass, rowid
                """
            ).fetchall()

        actual_counts = {class_name: 0 for class_name in expected_counts}
        for skill_index, resource_id, class_name, summons in rows:
            actual_counts[class_name] += 1
            geometry = self.state._load_player_skill_geometry(skill_index)
            self.assertIsNotNone(
                geometry,
                f"{class_name} skill {resource_id} has no runtime geometry",
            )
            self.assertEqual(geometry.resource_id, resource_id)
            if summons:
                self.assertIsNotNone(
                    self.state.player_summon_definition(skill_index),
                    f"{class_name} summon {resource_id} has no definition",
                )

        self.assertEqual(actual_counts, expected_counts)


if __name__ == "__main__":
    unittest.main()
