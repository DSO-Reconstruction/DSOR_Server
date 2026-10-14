"""Player spells target hostile minion actors without touching their owners."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dso_server.protocol.combat_protocol import (
    HitServerCommand,
    KillServerCommand,
    SkillCast,
    TargetBulletSkill,
)
from dso_server.protocol.gameplay_protocol import (
    NewMonsterServerCommand,
    StatusEffectServerCommand,
)
from dso_server.services.base import CharacterService
from dso_server.tests import test_pvp_player_effects as fixtures


class EnemyMinionTargetTests(unittest.TestCase):
    setUp = fixtures.PVPPlayerEffectTests.setUp
    move = fixtures.PVPPlayerEffectTests.move
    commands = fixtures.PVPPlayerEffectTests.commands
    effect = fixtures.PVPPlayerEffectTests.effect

    def spawn(self, *, owner=None, amount=1):
        owner = owner or self.target
        owner.gameplay_deadline_timer = self.source.gameplay_deadline_timer
        definition = replace(self.combat.player_summon_definition(1877), amount=amount)
        spawn = self.combat.spawn_player_summon(
            definition, owner_actor_id=owner.gameplay_remote_actor_id,
            position=(25.0, 0.0, 53.0), yaw=0.0, level=100,
            server_tick=100, now=100.0, player_damage_range=(100.0, 100.0),
        )
        self.move(self.source, (25.0, 0.0, 50.0))
        self.move(self.target, (100.0, 0.0, 100.0))
        for summon in spawn.actors:
            self.combat.player_summon_health[summon.actor_id] = [1000, 1000]
        for viewer in (self.source, self.target):
            CharacterService._send_player_summon_spawn_to(viewer, spawn)
        return spawn

    def cast(self, *, target_ids=(), damage=100.0, effects=(), area=False, execute=True, projectile=False):
        geometry = replace(
            self.combat._load_player_skill_geometry(1848),
            scale_skill_timing=False, hit_frame=0, bullet_emit_delay=0.0,
            targeting_type="Radius" if area else "Actor", hit_range=6.0, impact_radius=6.0,
            attack_range=20.0, projectile_range=0.0, projectile_velocity=0.0,
            damage_modifier=1.0 if damage else 0.0, damage_type="Fire",
            victim_status_effects=effects, location_status_effects=(),
            bullet_location_effects=(), bullet_death_effects=(),
        )
        skill = (TargetBulletSkill(0, 1848, 0.0, 200, len(self.timers) + 1,
                                  (0.0, 0.0, 1.0, 0.0), 0, target_ids[0],
                                  target_positions=((25.0, 0.0, 53.0),))
                 if projectile else SkillCast(0, 1848, 0.0, 200, len(self.timers) + 1))
        with patch.object(self.combat, "_load_player_skill_geometry", return_value=geometry), \
                patch.object(self.combat, "_modified_player_skill_geometry", side_effect=lambda g, _: g), \
                patch.object(self.combat, "player_skill_impact_delay_seconds", return_value=0.1):
            timers = CharacterService._prepare_pvp_skill_hits(
                self.source, self.combat, skill,
                source_position=self.source.gameplay_last_move.relative_position,
                source_actor_id=self.source.gameplay_remote_actor_id,
                player_damage_range=(damage, damage), attack_speed_multiplier=1.0,
                modifier_sources=(), explicit_target_actor_ids=target_ids,
            )
            if execute:
                for timer in timers:
                    timer.function(*timer.args)
        return timers

    def test_enemy_spawn_is_targetable_while_owner_sees_friendly_minion(self):
        summon = self.spawn().actors[0]
        for viewer, hostile, owner_id in ((self.source, True, self.target.gameplay_remote_actor_id),
                                          (self.target, False, 1)):
            group = CharacterService._summon_group_for_session(viewer, summon, summon.spawn_group)
            actor = next(c.value for c in group.spawn_batch.commands if isinstance(c, NewMonsterServerCommand))
            self.assertEqual(actor.placement.field_00, 0x10001 if hostile else 0x10004)
            self.assertEqual(actor.placement.field_08, 2 if hostile else 0)
            self.assertEqual(actor.field_d8, owner_id)
            self.assertEqual((actor.field_88, actor.field_90), (1000, 1000))
        self.source.gameplay_pvp_enabled = False
        group = CharacterService._summon_group_for_session(self.source, summon, summon.spawn_group)
        self.assertEqual(group.spawn_batch.commands[0].value.placement.field_00, 0x10004)

    def test_targeted_damage_and_stun_affect_minion_on_both_clients(self):
        summon = self.spawn().actors[0]
        effect = self.effect()
        self.assertEqual(len(self.cast(target_ids=(summon.actor_id,), effects=(effect,), projectile=True)), 1)
        self.assertEqual(self.combat.player_summon_health[summon.actor_id][0], 900)
        self.assertEqual(self.combat.player_health[self.target.gameplay_remote_actor_id][0], 1000)
        for viewer in (self.source, self.target):
            commands = self.commands(viewer)
            self.assertTrue(any(isinstance(c, HitServerCommand) and c.actor_id == summon.actor_id for c in commands))
            self.assertTrue(any(isinstance(c, StatusEffectServerCommand) and c.actor_id == summon.actor_id for c in commands))
            entry = viewer.gameplay_victim_status_effect_instances[(summon.actor_id, effect.effect_id)][0]
            self.assertEqual(entry.field_10, 50)  # PvP duration, not the PvE duration.
        self.assertTrue(self.combat._monster_actions_disabled(summon.actor_id))
        self.assertFalse(CharacterService._player_disabled_actor_features(self.target))

    def test_status_only_skill_does_not_manufacture_damage(self):
        summon = self.spawn().actors[0]
        self.cast(target_ids=(summon.actor_id,), damage=0, effects=(self.effect(),))
        self.assertEqual(self.combat.player_summon_health[summon.actor_id][0], 1000)
        self.assertTrue(self.commands(self.source))
        self.assertTrue(all(isinstance(c, StatusEffectServerCommand) for c in self.commands(self.source)))

    def test_area_damage_hits_enemy_pack_but_not_friendly_summons(self):
        enemies = self.spawn(amount=3).actors
        friendly = self.spawn(owner=self.source).actors[0]
        timers = self.cast(area=True)
        self.assertEqual(len(timers), 3)
        self.assertTrue(all(self.combat.player_summon_health[a.actor_id][0] == 900 for a in enemies))
        self.assertEqual(self.combat.player_summon_health[friendly.actor_id][0], 1000)

    def test_kill_removes_minion_without_owner_death_or_match_credit(self):
        from dso_server.tests import test_summon_pvp_kills as arena_fixtures
        from dso_server.gameplay.pvp_match_state import MatchPhase
        match = arena_fixtures.SummonPVPKillTests.arena(self)
        summon = self.spawn().actors[0]
        self.source.gameplay_match_callback = Mock(wraps=self.source.gameplay_match_callback)
        self.target.gameplay_inventory = SimpleNamespace(health_points=777.0)
        self.cast(target_ids=(summon.actor_id,), damage=1000)
        self.assertNotIn(summon.actor_id, self.combat.player_summons)
        self.assertNotIn(summon.actor_id, self.combat.player_summon_health)
        self.assertEqual(self.target.gameplay_inventory.health_points, 777.0)
        self.assertFalse(self.target.gameplay_death_pending)
        self.source.gameplay_match_callback.assert_not_called()
        self.assertEqual(match.phase, MatchPhase.RUNNING)
        self.assertEqual(match.participant(1).kills, 0)
        self.assertEqual(match.participant(2).deaths, 0)
        for viewer in (self.source, self.target):
            self.assertTrue(any(isinstance(c, KillServerCommand) and c.actor_id == summon.actor_id
                                for c in self.commands(viewer)))

    def test_dot_can_kill_minion_and_stops_after_removal(self):
        summon = self.spawn().actors[0]
        self.combat.player_summon_health[summon.actor_id][0] = 100
        with patch.object(self.combat, "monster_status_damage_program",
                          return_value=(0.1, 50.0, "Fire", False)):
            self.cast(target_ids=(summon.actor_id,), damage=0, effects=(self.effect(),))
        dot = next(timer for timer in reversed(self.timers) if timer.function == CharacterService._tick_pvp_status_damage)
        dot.function(*dot.args)
        self.assertEqual(self.combat.player_summon_health[summon.actor_id][0], 50)
        dot.function(*dot.args)
        self.assertNotIn(summon.actor_id, self.combat.player_summons)
        dot.function(*dot.args)
        self.assertEqual(self.combat.player_health[self.target.gameplay_remote_actor_id][0], 1000)

    def test_expired_minion_cannot_redirect_pending_hit_to_owner(self):
        summon = self.spawn().actors[0]
        timers = self.cast(target_ids=(summon.actor_id,), execute=False)
        self.combat.expire_player_summon(summon.actor_id)
        for timer in timers:
            timer.function(*timer.args)
        self.assertFalse(self.commands(self.source))
        self.assertEqual(self.combat.player_health[self.target.gameplay_remote_actor_id][0], 1000)

    def queue_minion_attack(self, summon, now):
        owner = self.target.gameplay_remote_actor_id
        victim = self.source.gameplay_remote_actor_id
        positions = {owner: summon.position,
                     victim: (summon.position[0] + 0.5, summon.position[1], summon.position[2])}
        hostility = {owner: frozenset({victim})}
        attacks = self.combat.maybe_start_player_summon_attacks(
            positions, now=now, server_tick=200, hostile_player_actor_ids_by_owner=hostility,
        )
        return attacks, positions, hostility

    def test_stun_cancels_pending_attack_and_blocks_new_attacks_until_expiry(self):
        summon = self.spawn().actors[0]
        attacks, positions, hostility = self.queue_minion_attack(summon, 101.0)
        self.assertTrue(attacks)
        self.cast(target_ids=(summon.actor_id,), damage=0, effects=(self.effect(),))
        damage, _ = self.combat.resolve_due_player_summon_attacks(
            now=110.0, player_positions=positions, hostile_player_actor_ids_by_owner=hostility,
        )
        self.assertFalse(damage)
        self.assertFalse(self.queue_minion_attack(summon, 111.0)[0])
        self.combat.monster_status_effects.pop(summon.actor_id)
        self.assertTrue(self.queue_minion_attack(summon, 112.0)[0])

    def test_weaken_reduces_minion_damage_without_weakening_owner(self):
        summon = self.spawn().actors[0]
        attacks, positions, hostility = self.queue_minion_attack(summon, 101.0)
        self.assertTrue(attacks)
        baseline, _ = self.combat.resolve_due_player_summon_attacks(
            now=103.0, player_positions=positions, hostile_player_actor_ids_by_owner=hostility,
        )
        self.assertTrue(baseline)
        self.cast(target_ids=(summon.actor_id,), damage=0,
                  effects=(self.effect("debuff_all_weaken,D:5"),))
        self.assertTrue(self.queue_minion_attack(summon, 104.0)[0])
        weakened, _ = self.combat.resolve_due_player_summon_attacks(
            now=106.0, player_positions=positions, hostile_player_actor_ids_by_owner=hostility,
        )
        self.assertTrue(weakened)
        self.assertAlmostEqual(weakened[0].damage, baseline[0].damage * 0.1, delta=1.0)
        self.assertFalse(CharacterService._active_pvp_victim_modifier_sources(self.target))

    def test_pvp_toggle_updates_existing_minion_faction(self):
        from dso_server.protocol.gameplay_protocol import decode_monster_update_batch
        summon = self.spawn().actors[0]
        for enabled in (False, True):
            self.target.gameplay_pvp_enabled = enabled
            self.target.gameplay_presence_callback(self.target, ("pvp_flag", enabled, 300))
            data, bits, label = self.source.recorder.messages[-1]
            self.assertEqual(label, "player-summon-faction-refresh")
            batch = decode_monster_update_batch(data, bits)
            self.assertEqual(batch.monster_actor_id, summon.actor_id)
            self.assertEqual(batch.faction.placement.field_00, 0x10001 if enabled else 0x10004)

    def test_owner_immunity_does_not_protect_enemy_minion(self):
        summon = self.spawn().actors[0]
        with patch.object(CharacterService, "_player_victim_feature_locked", return_value=True):
            self.cast(target_ids=(summon.actor_id,))
        self.assertEqual(self.combat.player_summon_health[summon.actor_id][0], 900)


if __name__ == "__main__":
    unittest.main()
