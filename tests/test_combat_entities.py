"""One cast shares collision/topology budgets across all hostile entity kinds."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dso_server.protocol.combat_protocol import BULLET_SKILL_COMMAND, BulletSkill
from dso_server.gameplay.combat_entities import CombatEntity, plan_entity_skill
from dso_server.services.base import CharacterService
from dso_server.services import player_combat
from dso_server.tests import test_pvp_player_effects as player_fixtures
from dso_server.tests import test_local_combat_state as monster_fixtures


class EntityPlanningTests(unittest.TestCase):
    def setUp(self):
        fixture = monster_fixtures.LocalCombatStateTests()
        fixture.setUp()
        self.geometry = replace(
            fixture.state._load_player_skill_geometry(1849),
            impact_count=1, bullet_count=1, reproduction_count=0,
            impact_radius=0.0, location_status_effects=(), location_visual_effects=(),
            bullet_death_effects=(), start_offset=(0.0, 0.0, 0.0, 0.0),
            bullet_emit_delay=0.2, projectile_velocity=10.0,
            scale_skill_timing=True,
        )
        self.entities = (CombatEntity(101, (0.0, 0.0, 3.0), 0.5),
                         CombatEntity(202, (0.0, 0.0, 6.0), 0.5),
                         CombatEntity(303, (3.0, 0.0, 6.0), 0.5))

    def plan(self, geometry=None, **kwargs):
        return plan_entity_skill(
            geometry or self.geometry, self.entities, origin=(0.0, 0.0, 0.0),
            direction=(0.0, 0.0, 1.0, 0.0), projectile=True, **kwargs)

    def test_a_single_impact_bullet_with_its_own_radius_does_not_splash(self):
        """The fireball (BulletRadius 0.5, ImpactCount 1, ImpactRadius 2.35)
        strikes one enemy; the C14 micro-rocket (BulletRadius 0) explodes."""
        fireball = replace(self.geometry, impact_radius=3.5, projectile_radius=0.5)
        self.assertEqual([i.target.actor_id for i in self.plan(fireball).impacts], [101])
        rocket = replace(self.geometry, impact_radius=3.5, projectile_radius=0.0)
        self.assertEqual({i.target.actor_id for i in self.plan(rocket).impacts}, {101, 202})

    def test_projectile_release_uses_the_later_authored_delay_without_double_counting(self):
        for emit_seconds, expected in ((0.0, 0.2), (0.15, 0.2), (0.3, 0.3)):
            with self.subTest(emit=emit_seconds):
                geometry = replace(self.geometry, loop_start_frame=5, bullet_emit_delay=emit_seconds)
                normal, fast = self.plan(geometry), self.plan(geometry, attack_speed=2.0)
                self.assertAlmostEqual(normal.emit_delay, expected)
                self.assertAlmostEqual(fast.emit_delay, expected / 2.0)
                self.assertEqual(normal.projectile_velocity, fast.projectile_velocity)
                self.assertAlmostEqual(normal.impacts[0].delay - normal.emit_delay,
                                       fast.impacts[0].delay - fast.emit_delay)
                unscaled = self.plan(replace(geometry, scale_skill_timing=False), attack_speed=2.0)
                self.assertAlmostEqual(unscaled.emit_delay, expected)
                timed = self.plan(replace(geometry, skill_timing_multiplier=0.5))
                self.assertAlmostEqual(timed.emit_delay, expected * 0.5)

    def test_three_projectiles_share_one_collision_budget(self):
        plan = self.plan(replace(self.geometry, bullet_count=3))
        self.assertEqual([(p.target.actor_id, p.projectile_index) for p in plan.impacts],
                         [(101, 0), (101, 1), (101, 2)])

    def test_piercing_reaches_next_entity_and_preserves_travel_order(self):
        plan = self.plan(replace(self.geometry, impact_count=2, bullet_count=3))
        self.assertEqual([p.target.actor_id for p in plan.impacts], [101, 202] * 3)
        self.assertLess(plan.impacts[0].delay, plan.impacts[1].delay)

    def test_splash_and_direct_contact_do_not_double_hit_same_entity(self):
        plan = self.plan(replace(self.geometry, impact_count=2, impact_radius=3.0))
        self.assertEqual([p.target.actor_id for p in plan.impacts], [101, 202])
        self.assertEqual(plan.impacts[0].delay, plan.impacts[1].delay)

    def test_chain_selects_nearest_new_entity_off_the_original_ray(self):
        plan = self.plan(replace(self.geometry, reproduction_count=2, target_search_radius=4.0))
        self.assertEqual([p.target.actor_id for p in plan.impacts], [101, 202, 303])
        self.assertEqual([p.projectile_index for p in plan.impacts], [0, 1, 2])
        self.assertTrue(plan.impacts[0].delay < plan.impacts[1].delay < plan.impacts[2].delay)

    def test_target_point_uses_one_arrival_for_all_entities(self):
        plan = self.plan(replace(self.geometry, hit_range=4.0), point=(0.0, 0.0, 6.0))
        self.assertEqual({p.target.actor_id for p in plan.impacts}, {101, 202, 303})
        self.assertEqual(len({p.delay for p in plan.impacts}), 1)

    def test_actor_target_never_falls_back_to_a_different_entity(self):
        self.assertFalse(self.plan(explicit_targets=(999,), require_explicit_target=True).impacts)

    def test_area_targets_have_the_same_radius_rule(self):
        plan = plan_entity_skill(
            replace(self.geometry, targeting_type="Radius", hit_range=3.0), self.entities,
            origin=(0.0, 0.0, 3.0), direction=(0.0, 0.0, 1.0, 0.0),
            projectile=False, impact_delay=0.8)
        self.assertEqual([p.target.actor_id for p in plan.impacts], [101, 202])
        self.assertTrue(all(p.delay == 0.8 for p in plan.impacts))


class MixedEntityCastTests(unittest.TestCase):
    move = player_fixtures.PVPPlayerEffectTests.move

    def setUp(self):
        player_fixtures.PVPPlayerEffectTests.setUp(self)
        fixture = monster_fixtures.LocalCombatStateTests()
        fixture.setUp()
        self.combat = fixture.state
        self.frame = self.combat.encounter.coordinate_frame
        for session in (self.source, self.target):
            session.gameplay_combat_state = self.combat
            session.gameplay_monster_encounter = self.combat.encounter
            self.combat.register_player(session.gameplay_remote_actor_id, 1000)
        self.move(self.source, (25.0, 0.0, 50.0))
        self.move(self.target, (25.0, 0.0, 56.0))
        self.monster = self.combat.encounter.actor_ids[0]
        for actor in self.combat.encounter.actor_ids:
            self.combat.monster_positions[actor] = (100.0, 0.0, 100.0)
        self.combat.monster_positions[self.monster] = (25.0, 0.0, 53.0)
        self.source.gameplay_skill_book_ids = {1849}
        self.source.gameplay_spawned_monster_actor_ids = set(self.combat.encounter.actor_ids)
        self.source.gameplay_delivered_monster_positions = dict(self.combat.monster_positions)
        self.source.gameplay_character.class_id = 1
        self.skill = BulletSkill(0, 1849, 0.0, 200, 88, (0.0, 0.0, 1.0, 0.0), 0)
        self.combat._player_skill_geometry[1849] = replace(
            self.combat._load_player_skill_geometry(1849), bullet_count=3,
            impact_count=1, reproduction_count=0, impact_radius=0.0,
            location_status_effects=(), location_visual_effects=(), bullet_death_effects=(),
        )

    def cast(self):
        stats = SimpleNamespace(minimum_damage=10.0, maximum_damage=10.0,
                                critical_hit_chance=0.0, critical_damage_multiplier=2.0, attack_speed=1.0,
                                skill_modifier_sources=())
        with patch.object(player_combat, "decode_bullet_skill", return_value=self.skill), \
                patch.object(player_combat, "calculate_player_combat_stats", return_value=stats), \
                patch.object(CharacterService, "_apply_player_skill_resource", return_value=True), \
                patch.object(CharacterService, "_start_monster_chase"), \
                patch.object(CharacterService, "_record_monster_targeting_result"), \
                patch.object(CharacterService, "_send_remote_player_skill"), \
                patch.object(self.combat, "plan_entity_skill_hits", wraps=self.combat.plan_entity_skill_hits) as planner:
            CharacterService._handle_bullet_skill(
                self.source, b"\x8b" + BULLET_SKILL_COMMAND.to_bytes(2, "little"))
        self.assertEqual(planner.call_count, 1)
        return tuple(self.combat.pending_runtime_player_hits.values()), tuple(self.source.gameplay_pending_pvp_hits.values())

    def test_monster_in_front_consumes_projectile_contacts_before_player(self):
        monsters, players = self.cast()
        self.assertEqual(len(monsters), 3)
        self.assertFalse(players)

    def test_one_piercing_plan_dispatches_to_both_health_adapters(self):
        self.combat._player_skill_geometry[1849] = replace(
            self.combat._player_skill_geometry[1849], impact_count=2)
        self.combat.monster_health[self.monster] = 10000
        monsters, players = self.cast()
        self.assertEqual(len(monsters), 3)
        self.assertEqual(len(players), 3)
        self.assertEqual({p.target_actor_id for p in monsters}, {self.monster})
        self.assertEqual({p.target_actor_id for p in players}, {self.target.gameplay_remote_actor_id})
        resolutions = [self.combat.resolve_runtime_player_hit(
            p.source_actor_id, p.cast_id, p.impact_index, now=p.due_at,
        ) for p in monsters]
        self.assertTrue(all(r is not None and r.damage > 0 for r in resolutions))
        self.assertEqual(self.combat.monster_health[self.monster],
                         10000 - sum(r.damage for r in resolutions))
        for key in tuple(self.source.gameplay_pending_pvp_hits):
            CharacterService._resolve_pvp_skill_hit(
                self.source, self.source.gameplay_chase_generation, key)
        self.assertEqual(self.combat.player_health[self.target.gameplay_remote_actor_id][0],
                         1000 - sum(p.damage for p in players))

    def test_explicit_player_target_does_not_also_hit_monster_on_ray(self):
        snapshot = CharacterService._snapshot_pvp_skill_targets(self.source, self.combat)
        plan = self.combat.plan_entity_skill_hits(
            skill_index=1849, player_position=self.source.gameplay_last_move.relative_position,
            projectile=True, direction=self.skill.direction, additional_entities=snapshot[1],
            explicit_target_actor_ids=(self.target.gameplay_remote_actor_id,), require_explicit_target=True)
        monsters = self.combat.start_bullet_skill_hits(
            self.skill, self.source.gameplay_last_move.relative_position,
            source_actor_id=self.source.gameplay_remote_actor_id, hit_plan=plan)
        players = CharacterService._prepare_pvp_skill_hits(
            self.source, self.combat, self.skill,
            source_position=self.source.gameplay_last_move.relative_position,
            source_actor_id=self.source.gameplay_remote_actor_id,
            player_damage_range=(10.0, 10.0), attack_speed_multiplier=1.0,
            modifier_sources=(), hit_plan=plan, target_snapshot=snapshot)
        self.assertFalse(monsters)
        self.assertEqual(len(players), 3)
