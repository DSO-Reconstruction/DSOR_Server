"""Authored extra missiles retain separate damage and status occurrences."""

import unittest
from unittest.mock import patch

from dso_server.paths import (
    DEFAULT_STATIC_DB_PATH,
)
from dso_server.protocol.combat_protocol import BulletSkill, HitServerCommand, SkillCast
from dso_server.protocol.gameplay_protocol import StatusEffectServerCommand
from dso_server.protocol.item_set_protocol import ItemSetDatabase
from dso_server.services.base import CharacterService
from dso_server.tests import test_pvp_player_effects as fixtures
from dso_server.tests import test_enemy_minion_targets as minion_fixtures
from dso_server.tests import test_local_combat_state as pve_fixtures


class ProjectileSetTests(unittest.TestCase):
    move = fixtures.PVPPlayerEffectTests.move
    commands = fixtures.PVPPlayerEffectTests.commands

    def setUp(self):
        fixtures.PVPPlayerEffectTests.setUp(self)
        # The two equipped Heredur pieces on Example Player 2, resolved through
        # the actual set database rather than a fabricated bullet-count value.
        database = ItemSetDatabase(DEFAULT_STATIC_DB_PATH)
        evaluation = database.evaluate((
            "mage_unique_ring_bgh_intensified",
            "mage_unique_backitem_bgh_heredur_intensified",
        ), class_name="mage")
        self.source.gameplay_item_set_evaluation = evaluation
        self.source.gameplay_item_set_effects = database.reconcile_effects(
            evaluation, {}, actor_id=1, next_instance_id=1000, server_tick=200,
        ).effects
        self.sources = CharacterService._active_skill_modifier_sources(self.source)
        self.move(self.source, (25.0, 0.0, 50.0))
        self.move(self.target, (25.0, 0.0, 55.0))
        self.skill = BulletSkill(0, 1849, 0.0, 200, 123, (0.0, 0.0, 1.0, 0.0), 0)
        geometry = self.combat._modified_player_skill_geometry(
            self.combat._load_player_skill_geometry(1849), self.sources)
        self.assertEqual(geometry.bullet_count, 3)

    def prepare(self, *, sources=None, target_ids=(), skill=None):
        return CharacterService._prepare_pvp_skill_hits(
            self.source, self.combat, skill or self.skill,
            source_position=self.source.gameplay_last_move.relative_position,
            source_actor_id=self.source.gameplay_remote_actor_id,
            player_damage_range=(100.0, 100.0), attack_speed_multiplier=1.0,
            modifier_sources=self.sources if sources is None else sources,
            explicit_target_actor_ids=target_ids,
        )

    def test_three_missiles_each_damage_and_apply_status_to_player(self):
        timers = self.prepare()
        self.assertEqual(len(timers), 3)
        self.assertEqual(len(self.source.gameplay_pending_pvp_hits), 3)
        pending = tuple(self.source.gameplay_pending_pvp_hits.values())
        self.assertEqual({p.projectile_index for p in pending}, {0, 1, 2})
        self.assertTrue(all(p.victim_status_effects for p in pending))
        target_id = self.target.gameplay_remote_actor_id
        for timer, hit in zip(timers, pending):
            previous = self.combat.player_health[target_id][0]
            timer.function(*timer.args)
            self.assertEqual(self.combat.player_health[target_id][0], previous - hit.damage)
        hits = [c for c in self.commands(self.target) if isinstance(c, HitServerCommand)]
        self.assertEqual(len(hits), 3)
        statuses = [e for c in self.commands(self.target)
                    if isinstance(c, StatusEffectServerCommand)
                    for e in c.value.entries if e.has_details]
        for effect in pending[0].victim_status_effects:
            self.assertEqual(sum(e.effect_id == effect.effect_id for e in statuses), 3)
        self.assertFalse(self.source.gameplay_pending_pvp_hits)
        for effect in pending[0].victim_status_effects:
            if effect.trigger.casefold() in {"hit", "immediate"}:
                self.assertIn((1, effect.effect_id), self.target.gameplay_victim_status_effect_instances)

    def test_removing_set_bonus_restores_one_projectile(self):
        self.assertEqual(len(self.prepare(sources=())), 1)

    def test_effect_rolls_have_distinct_projectile_seeds(self):
        with patch.object(self.combat, "_effect_applies", wraps=self.combat._effect_applies) as roll:
            self.prepare()
        seeds = {call.kwargs["cast_id"] for call in roll.call_args_list}
        base = self.skill.cast_id + self.target.gameplay_remote_actor_id
        self.assertEqual(seeds, {base, base + 257, base + 514})

    def test_impact_immunity_rechecks_every_missile(self):
        timers = self.prepare()
        timers[0].function(*timers[0].args)
        health = self.combat.player_health[self.target.gameplay_remote_actor_id][0]
        with patch.object(CharacterService, "_pvp_actor_damage_locked", return_value=True):
            for timer in timers[1:]:
                timer.function(*timer.args)
        self.assertEqual(self.combat.player_health[self.target.gameplay_remote_actor_id][0], health)

    def test_non_projectile_skill_is_not_multiplied(self):
        skill = SkillCast(0, 1849, 0.0, 200, 124)
        self.assertEqual(len(self.prepare(
            skill=skill, target_ids=(self.target.gameplay_remote_actor_id,))), 1)

    def test_enemy_minion_receives_all_three_hits_without_hurting_owner(self):
        summon = minion_fixtures.EnemyMinionTargetTests.spawn(self).actors[0]
        timers = self.prepare(target_ids=(summon.actor_id,))
        self.assertEqual(len(timers), 3)
        damage = sum(p.damage for p in self.source.gameplay_pending_pvp_hits.values())
        for timer in timers:
            timer.function(*timer.args)
        self.assertEqual(self.combat.player_summon_health[summon.actor_id][0], 1000 - damage)
        self.assertEqual(self.combat.player_health[self.target.gameplay_remote_actor_id][0], 1000)

    def test_pve_already_resolves_three_independent_missiles(self):
        fixture = pve_fixtures.LocalCombatStateTests()
        fixture.setUp()
        combat = fixture.state
        target_id = fixture.encounter.actor_ids[0]
        target = combat.monster_positions[target_id]
        source = fixture._wire((target[0], target[1], target[2] - 3.0))
        combat.monster_health[target_id] = 10000
        pending = combat.start_bullet_skill_hits(
            self.skill, source, source_actor_id=98305,
            modifier_effect_ids=self.sources, explicit_target_actor_ids=(target_id,),
            player_damage_range=(100.0, 100.0), now=100.0,
        )
        self.assertEqual(len(pending), 3)
        self.assertEqual({p.projectile_index for p in pending}, {0, 1, 2})
        resolutions = [combat.resolve_runtime_player_hit(
            p.source_actor_id, p.cast_id, p.impact_index, now=p.due_at,
        ) for p in pending]
        self.assertTrue(all(r is not None and r.damage > 0 for r in resolutions))
        self.assertEqual(combat.monster_health[target_id], 10000 - sum(r.damage for r in resolutions))
        self.assertTrue(all(r.victim_status_effects for r in resolutions))
