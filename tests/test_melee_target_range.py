"""Match native Rage Attack reach using the target navigation footprint."""

from dataclasses import replace
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dso_server.gameplay.combat_entities import CombatEntity, plan_entity_skill
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.navigation_shapes import navigation_shape_radii, monster_target_radii
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.services.character_server import CharacterServer
from dso_server.services import player_combat
from dso_server.tests import test_combat_entities as fixtures
from dso_server.tests.test_target_skill_input import target_packet


class RageAttackRangeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MixedEntityCastTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.session = self.fixture.source
        self.combat = self.fixture.combat
        self.monster = self.fixture.monster
        self.session.gameplay_skill_book_ids = {1831}
        self.session.gameplay_character.class_id = 0
        self.session.gameplay_presence_callback = None
        self.geometry = self.combat._load_player_skill_geometry(1831)
        self.origin = (25.0, 0.0, 50.0)

    def cast(self, actual, visible=None, *, first_cast=False):
        self.combat.monster_positions[self.monster] = actual
        self.session.gameplay_delivered_monster_positions[self.monster] = visible or actual
        if first_cast:
            self.session.gameplay_last_move = None
            self.session.gameplay_spawn_position = self.origin
        stats = SimpleNamespace(minimum_damage=10.0, maximum_damage=10.0,
            critical_hit_chance=0.0, critical_damage_multiplier=2.0, attack_speed=1.0, skill_modifier_sources=())
        payload, self.session.current_message_bit_length = target_packet(self.monster)
        with (
            patch.object(player_combat, "calculate_player_combat_stats", return_value=stats),
            patch.object(CharacterServer, "_apply_player_skill_resource", return_value=True),
            patch.object(CharacterServer, "_consume_active_essence"),
            patch.object(CharacterServer, "_start_monster_chase"),
        ):
            CharacterServer._handle_spatial_skill(self.session, payload)
        return tuple(self.combat.pending_runtime_player_hits.values())

    def test_native_shape_radii_and_real_monster_mapping(self):
        radii = navigation_shape_radii()
        for shape, vertex in (("small", (16, 50)), ("medium", (33, 100)),
                              ("large", (50, 150)), ("giant", (133, 400))):
            self.assertAlmostEqual(radii[shape], math.hypot(*vertex) / 100)
        self.assertEqual(radii["default"], radii["small"])
        monsters = monster_target_radii(DEFAULT_STATIC_DB_PATH)
        self.assertEqual(monsters["a0001_gen_anderworld_creature"], radii["small"])
        self.assertEqual(monsters["a0001_normal_anderworld_minion"], radii["medium"])
        target = self.combat.monsters[self.monster]
        self.assertEqual(target.target_radius, radii["small"])
        self.assertAlmostEqual(target.collision_radius, 0.3)

    def test_runtime_spawned_monsters_receive_the_same_navigation_radius(self):
        encounter = self.combat.encounter
        empty = replace(encounter, actor_ids=(), runtime_definitions=(), spawn_groups=(),
                        spawn_batch=replace(encounter.spawn_batch, commands=()))
        state = LocalCombatState(empty, player_actor_id=1, player_health=1000)
        group = next(g for g in encounter.groups if self.monster in g.actor_ids)
        self.assertIn(self.monster, state.add_runtime_monsters(group, encounter.runtime_definitions))
        self.assertEqual(state.monsters[self.monster].target_radius,
                         self.combat.monsters[self.monster].target_radius)

    def test_rage_attack_hits_in_native_reach_and_deals_damage_at_hit_frame(self):
        self.assertEqual(self.geometry.resource_id, "angrystrike")
        self.assertEqual(self.geometry.attack_range, 1.75)
        # 2.15 > 1.75 + physics capsule (0.3), but < 1.75 + nav radius (0.525).
        hits = self.cast((25.0, 0.0, 52.15))
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(hit.target_actor_id, self.monster)
        before = self.combat.monster_health[self.monster]
        self.assertIsNone(self.combat.resolve_runtime_player_hit(
            hit.source_actor_id, hit.cast_id, hit.impact_index, now=hit.due_at - 0.01))
        self.assertEqual(self.combat.monster_health[self.monster], before)
        result = self.combat.resolve_runtime_player_hit(
            hit.source_actor_id, hit.cast_id, hit.impact_index, now=hit.due_at)
        self.assertGreater(result.damage, 0)
        self.assertLess(self.combat.monster_health[self.monster], before)

    def test_reach_uses_each_authored_shape_and_rejects_beyond_its_boundary(self):
        for radius in navigation_shape_radii().values():
            for delta in (-0.001, 0.001):
                z = self.geometry.attack_range + radius + delta
                target = CombatEntity(99, (0.0, 0.0, z), 0.3, target_radius=radius)
                plan = plan_entity_skill(self.geometry, (target,), origin=(0.0, 0.0, 0.0),
                    direction=(0.0, 0.0, 1.0), projectile=False, explicit_targets=(99,))
                self.assertEqual(bool(plan.impacts), delta < 0)

    def test_target_navigation_radius_does_not_expand_projectile_capsule(self):
        geometry = replace(self.geometry, projectile_range=1.75, bullet_count=1)
        target = CombatEntity(99, (0.0, 0.0, 2.15), 0.3, target_radius=4.215)
        plan = plan_entity_skill(geometry, (target,), origin=(0.0, 0.0, 0.0),
            direction=(0.0, 0.0, 1.0), projectile=True, explicit_targets=(99,),
            require_explicit_target=True)
        self.assertFalse(plan.impacts)

    def test_current_nearby_position_is_not_rejected_by_older_delivered_sample(self):
        self.assertTrue(self.cast((25.0, 0.0, 51.5), (25.0, 0.0, 56.0)))

    def test_client_visible_nearby_position_is_still_hittable(self):
        self.assertTrue(self.cast((25.0, 0.0, 56.0), (25.0, 0.0, 51.5)))

    def test_distant_endpoints_do_not_create_a_melee_hit_along_their_path(self):
        self.assertFalse(self.cast((25.0, 0.0, 55.0), (25.0, 0.0, 45.0)))

    def test_vertical_separation_still_counts_like_native_validator(self):
        self.assertFalse(self.cast((25.0, 3.0, 51.0)))

    def test_no_move_packet_uses_actual_player_spawn(self):
        self.assertNotEqual(self.origin, self.combat.encounter.trigger_center)
        self.assertTrue(self.cast((25.0, 0.0, 52.15), first_cast=True))

    def test_out_of_range_explicit_target_does_not_hit_nearby_other_monster(self):
        other = next(a for a in self.combat.monsters if a != self.monster)
        self.combat.monster_positions[other] = (25.0, 0.0, 51.0)
        self.session.gameplay_delivered_monster_positions[other] = (25.0, 0.0, 51.0)
        self.assertFalse(self.cast((25.0, 0.0, 53.0)))

    def test_dead_target_is_not_revived_by_a_nearby_snapshot(self):
        self.combat.monster_health[self.monster] = 0
        self.assertFalse(self.cast((25.0, 0.0, 51.0)))
