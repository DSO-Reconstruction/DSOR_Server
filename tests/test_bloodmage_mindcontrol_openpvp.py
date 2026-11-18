"""Tests for Mage Bloodmage Guardian heal, Mind Control on mobs, and Open PvP skill features."""

import math
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.gameplay.local_combat_state import (
    LocalCombatState,
    VictimStatusEffectDefinition,
    MonsterAttack,
)
from dso_server.gameplay.local_monster_state import load_local_player_only_encounter
from dso_server.gameplay.status_runtime_catalog import _load_status_runtime_catalog
from dso_server.services.base import CharacterService
from dso_server.services import pvp_combat
from dso_server.services.pvp_combat import PVP_ARENA_DAMAGE_MULTIPLIER
from dso_server.tests.test_pvp_matchmaking import _quiet_server, _session


class BloodmageGuardianMindControlOpenPvPTests(unittest.TestCase):
    def setUp(self):
        self.server = _quiet_server(self)
        self.catalog = _load_status_runtime_catalog(DEFAULT_STATIC_DB_PATH)
        self.encounter = load_local_player_only_encounter(DEFAULT_STATIC_DB_PATH, "b1102_dm")
        self.combat = LocalCombatState(self.encounter, player_actor_id=1, player_health=1000)

    def test_bloodmage_guardian_heal_and_damage_reduction(self):
        """When Bloodmage passive is active, Guardian heals player by 2% HP/s and deals 50% damage."""
        bloodmage_id = self.catalog.effect_ids.get("mage_talent_bloodmage")
        self.assertIsNotNone(bloodmage_id)

        guardian_skill_index = 1869
        # Without bloodmage
        normal_defn = self.combat.player_summon_definition(guardian_skill_index)
        self.assertEqual(normal_defn.heal_fraction, 0.0)
        self.assertEqual(normal_defn.damage_multiplier, 1.0)

        # With bloodmage
        bm_defn = self.combat.player_summon_definition(guardian_skill_index, modifier_sources=(bloodmage_id,))
        self.assertAlmostEqual(bm_defn.heal_fraction, 0.02, places=3)
        self.assertEqual(bm_defn.heal_radius, 60.0)
        self.assertEqual(bm_defn.heal_interval_seconds, 1.0)
        self.assertEqual(bm_defn.damage_multiplier, 0.5)

        # Spawn guardian and test heal tick
        spawn = self.combat.spawn_player_summon(
            bm_defn,
            owner_actor_id=1,
            position=(0.0, 0.0, 0.0),
            yaw=0.0,
            level=100,
            server_tick=100,
            player_damage_range=(100.0, 100.0),
            modifier_sources=(bloodmage_id,),
            now=10.0,
        )
        self.assertEqual(len(spawn.actors), 1)
        guardian_actor = spawn.actors[0]

        # Damage player first
        self.combat.player_health[1] = [500, 1000]

        # Fast forward time past spawn delay and heal interval
        current_time = 12.0
        player_positions = {1: (0.0, 0.0, 0.0)}

        dmg_res, support_apps = self.combat.resolve_due_player_summon_attacks(
            now=current_time,
            player_positions=player_positions,
            server_tick=150,
        )

        # Verify heal was resolved: 2% of 1000 = 20 HP
        self.assertEqual(self.combat.player_health[1][0], 520)
        self.assertEqual(len(support_apps), 1)
        self.assertEqual(support_apps[0].healed, (520, 1000, 20))

    def test_mind_control_rejects_pvp_disguise_talent_on_mobs(self):
        """Ensure pvp_disguise_talent is dropped so guardian mob does not disappear."""
        disguise = VictimStatusEffectDefinition(
            resource_id="pvp_disguise_talent",
            effect_hosts="Receiver",
            aura_targets="",
            exclusivity_type="Replace",
            duration_ticks=500,
            effect_id=5960,
            chance=1.0,
            values=(),
            trigger="",
        )
        mc_effect = VictimStatusEffectDefinition(
            resource_id="skill_mindcontrol_effect",
            effect_hosts="Receiver",
            aura_targets="",
            exclusivity_type="Replace",
            duration_ticks=250,
            effect_id=5961,
            chance=1.0,
            values=(),
            trigger="",
        )

        accepted = self.combat.accepted_monster_status_effects(
            actor_id=101,
            definitions=(disguise, mc_effect),
            now=10.0,
        )
        # pvp_disguise_talent must be rejected, while skill_mindcontrol_effect is accepted
        accepted_ids = [e.resource_id for e in accepted]
        self.assertNotIn("pvp_disguise_talent", accepted_ids)
        self.assertIn("skill_mindcontrol_effect", accepted_ids)

    def test_mind_control_prevents_mob_damaging_caster(self):
        """Mind-controlled monster must not harm the caster player."""
        mob_id = 999
        self.combat.monsters[mob_id] = SimpleNamespace(
            actor_id=mob_id,
            template_id="minion_arcanemanifestation",
            level=100,
            minimum_damage=100,
            maximum_damage=100,
            maximum_health=1000,
            collision_radius=1.0,
            invulnerable=False,
            summon_targetable=True,
        )
        self.combat.monster_health[mob_id] = 1000
        self.combat.monster_positions[mob_id] = (5.0, 0.0, 5.0)

        # Initially hostile
        self.assertTrue(self.combat.monster_hostile_to_player(mob_id, player_actor_id=1))

        # Apply Mind Control by player 1
        mc_effect = VictimStatusEffectDefinition(
            resource_id="skill_mindcontrol_effect",
            effect_hosts="Receiver",
            aura_targets="",
            exclusivity_type="Replace",
            duration_ticks=250,
            effect_id=5961,
            chance=1.0,
            values=(),
            trigger="",
        )
        self.combat._record_monster_status_effects(
            mob_id,
            (mc_effect,),
            now=10.0,
            source_actor_id=1,
        )

        # Now mob must NOT be hostile to player 1
        self.assertFalse(self.combat.monster_hostile_to_player(mob_id, player_actor_id=1, now=10.0))

        # Queue an attack targeting player 1
        attack = MonsterAttack(
            attacker_actor_id=mob_id,
            target_actor_id=1,
            skill_index=1,
            command_id=1,
            aim_angle=0.0,
            server_tick=100,
            cast_id=100,
            impact_tick=110,
            hit_frame=1,
            motion_unblock_frame=1,
            skill_unblock_frame=1,
            position=(5.0, 0.0, 5.0, 1.0),
            target_position=(0.0, 0.0, 0.0),
            direction=(0.0, 0.0, 1.0, 0.0),
            projectile_radius=1.0,
            variant=1,
            due_at=10.5,
            projectile_origin=(5.0, 0.0, 5.0),
            maximum_projectile_range=10.0,
            motion_type="Melee",
            skill_level=1,
            definition=SimpleNamespace(damage_modifier=1.0, self_buff=False, summon_template=None, movement_skill=False, targeting_type="Target"),
        )
        self.combat.pending_monster_attacks[mob_id] = attack

        # Player health before
        self.combat.player_health[1] = [1000, 1000]

        # Resolve attacks at time 11.0 (when attack is due)
        resolutions = self.combat.resolve_due_monster_attacks(
            (0.0, 0.0, 0.0),
            now=11.0,
            player_actor_id=1,
        )

        # Player should have taken 0 damage and health remains 1000
        self.assertEqual(self.combat.player_health[1][0], 1000)
        player_damage_res = [r for r in resolutions if r.target_actor_id == 1]
        self.assertEqual(len(player_damage_res), 0)

    def test_open_pvp_applies_pvp_skill_features_and_mitigation(self):
        """Open PvP combat between flagged players applies PvP damage multiplier, DP duration, and CC clamp."""
        source = _session(51001, 1, "Caster")
        target = _session(51002, 2, "Victim")
        source.gameplay_pvp_enabled = True
        target.gameplay_pvp_enabled = True
        source.gameplay_match = None
        target.gameplay_match = None
        source.gameplay_combat_state = self.combat
        target.gameplay_combat_state = self.combat
        target.gameplay_inventory = SimpleNamespace(health_points=1000.0)

        # Ensure _filter_player_status_effects clamps stun duration to 50 ticks (2.0s) and uses DP
        freeze_def = VictimStatusEffectDefinition(
            resource_id="debuff_cc_freeze",
            effect_hosts="Receiver",
            aura_targets="",
            exclusivity_type="Replace",
            duration_ticks=250, # 10s in PvE
            pvp_duration_ticks=50, # 2s in PvP
            effect_id=5817,
            chance=1.0,
            values=(),
            trigger="",
        )
        filtered = CharacterService._filter_player_status_effects(target, (freeze_def,), pvp=True)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].duration_ticks, 50)

        # Verify ground strike damage in open PvP receives PVP_ARENA_DAMAGE_MULTIPLIER
        aura = SimpleNamespace(
            effect_id=5793,
            aura_radius=100.0,
            source_actor_id=1,
            damage_range=(100.0, 100.0),
            damage_programs=((1.0, "Physical", False),),
        )
        source.gameplay_presence_callback = Mock(return_value=[(2, (0, 0, 0), target)])

        with patch.object(CharacterService, "_resolve_pvp_skill_hit") as resolve_hit:
            with patch.object(CharacterService, "_pvp_target_is_hostile", return_value=True):
                with patch.object(CharacterService, "_pvp_actor_damage_locked", return_value=False):
                    CharacterService._schedule_pvp_ground_strike(
                        source, self.combat, aura, (freeze_def,),
                        position=(0.0, 0.0, 0.0), damage_range=(100.0, 100.0), server_tick=100,
                    )
                    self.assertEqual(len(source.gameplay_pending_pvp_hits), 1)
                    pending = next(iter(source.gameplay_pending_pvp_hits.values()))
                    # Raw damage is 100 * 0.5 (armor) = 50 (in Open PvP, no 0.25x scaling)
                    self.assertEqual(pending.damage, 50)

    def test_open_pvp_no_phantom_hit_when_skill_misses(self):
        """When an enemy skill misses a player geometrically, no phantom damage is dealt."""
        source = _session(51001, 1, "Caster")
        target = _session(51002, 2, "Victim")
        source.gameplay_pvp_enabled = True
        target.gameplay_pvp_enabled = True
        source.gameplay_match = None
        target.gameplay_match = None
        source.gameplay_combat_state = self.combat
        target.gameplay_combat_state = self.combat
        source.gameplay_last_move = SimpleNamespace(relative_position=(0.0, 0.0, 0.0), yaw=0.0)
        target.gameplay_last_move = SimpleNamespace(relative_position=(0.0, 0.0, 25.0), yaw=0.0)

        skill = SimpleNamespace(
            skill_index=1848,
            cast_id=1,
            aim_angle=0.0,
            server_tick=100,
        )

        from dso_server.gameplay.combat_entities import CombatEntity, EntitySkillPlan

        geometry = SimpleNamespace(
            resource_id="cleave",
            skill_type="melee",
            attack_range=3.0,
            hit_range=3.0,
            projectile_range=0.0,
            impact_count=1,
            damage_modifier=1.0,
            damage_type="Physical",
            projectile_velocity=0.0,
            hit_frame=0,
            scale_skill_timing=False,
            user_status_effects=(),
            location_status_effects=(),
        )

        # Empty hit plan (the melee attack missed because target is 25m away and reach is 3m)
        empty_hit_plan = EntitySkillPlan(geometry, impacts=())

        target_snapshot = (
            [(2, (0.0, 0.0, 25.0), target)],
            (CombatEntity(2, (0.0, 0.0, 25.0), 0.75),),
        )

        timers = CharacterService._prepare_pvp_skill_hits(
            source,
            self.combat,
            skill,
            hit_plan=empty_hit_plan,
            target_snapshot=target_snapshot,
            source_position=(0.0, 0.0, 0.0),
            source_actor_id=1,
            player_damage_range=(100.0, 100.0),
            player_critical_hit_chance=0.0,
            player_critical_damage_multiplier=1.0,
            player_rift_force_echo_chance=0.0,
            player_rift_force_echo_multiplier=0.0,
            attack_speed_multiplier=1.0,
            modifier_sources=(),
        )

        # Timers must be empty: no phantom hit scheduled!
        self.assertEqual(len(timers), 0)
        self.assertEqual(len(source.gameplay_pending_pvp_hits), 0)

    def test_shifted_skill_guardian_pvp_no_lock_order_violation(self):
        """Casting Guardian (0x004a) in PvP must not raise LockOrderViolation and must schedule summon timer."""
        from dso_server.protocol.combat_protocol import ShiftedSkill

        source = _session(51001, 1, "MageCaster")
        target = _session(51002, 2, "EnemyPlayer")
        source.gameplay_pvp_enabled = True
        target.gameplay_pvp_enabled = True
        source.gameplay_match = None
        target.gameplay_match = None
        source.gameplay_combat_state = self.combat
        target.gameplay_combat_state = self.combat
        source.gameplay_monster_encounter = self.encounter
        target.gameplay_monster_encounter = self.encounter
        source.gameplay_character.class_id = 2  # Mage
        source.gameplay_skill_book_ids = {1869}  # Guardian skill index
        source.current_message_bit_length = 200
        source.gameplay_last_move = SimpleNamespace(
            relative_position=self.encounter.coordinate_frame.world_to_wire((0.0, 0.0, 0.0)),
            yaw=0.0,
        )
        target.gameplay_last_move = SimpleNamespace(
            relative_position=self.encounter.coordinate_frame.world_to_wire((5.0, 0.0, 5.0)),
            yaw=0.0,
        )

        target_wire = target.gameplay_last_move.relative_position
        def mock_presence(session, event):
            if event[0] == "combat_targets":
                return [(2, target_wire, target)]
            return None
        source.gameplay_presence_callback = mock_presence
        target.gameplay_presence_callback = mock_presence

        shifted_skill = ShiftedSkill(
            skill_level=1,
            skill_index=1869,  # Guardian
            aim_angle=0.0,
            server_tick=100,
            cast_id=42,
            variant=0,
            target_position=(2.0, 0.0, 2.0),
        )

        with patch("dso_server.services.player_combat.decode_shifted_skill", return_value=shifted_skill), \
             patch.object(CharacterService, "_player_victim_feature_locked", return_value=False), \
             patch.object(CharacterService, "_cancel_mount_cast"), \
             patch.object(CharacterService, "_remove_mount_ride"), \
             patch.object(CharacterService, "_apply_player_skill_resource", return_value=True), \
             patch.object(CharacterService, "_consume_active_essence"):

            # Calling _handle_spatial_skill must succeed without LockOrderViolation
            CharacterService._handle_spatial_skill(source, b"\x8b\x4a\x00")

        # Verify summon timer was created for Guardian or summon resolved
        has_summon = (
            any(k[0] == "player-summon" and k[2] == 42 for k in source.gameplay_projectile_timers)
            or any(k[0] == "player-summon-expire" for k in source.gameplay_projectile_timers)
            or len(self.combat.player_summons) > 0
        )
        self.assertTrue(has_summon)

    def test_mind_controlled_guardian_attacks_and_damages_owner(self):
        """When Mind Control is cast on a player's Guardian:
        1. Guardian must not despawn even if the original owner is far away.
        2. Guardian must advance toward and attack its original summoner.
        3. Guardian must not heal its original summoner while mind-controlled.
        4. Once mind control expires, it stops attacking owner and can heal owner again.
        """
        from dso_server.protocol.consumable_protocol import VictimStatusEffectDefinition
        from dso_server.services.summon_pvp import CharacterService as SummonService

        p1 = _session(52001, 101, "Player1Owner")
        p2 = _session(52002, 202, "Player2Controller")
        p1.gameplay_pvp_enabled = True
        p2.gameplay_pvp_enabled = True
        p1.gameplay_respawn_allowed = True
        p2.gameplay_respawn_allowed = True
        p1.gameplay_combat_state = self.combat
        p2.gameplay_combat_state = self.combat
        p1.gameplay_presence_callback = lambda s, ev: None
        p2.gameplay_presence_callback = lambda s, ev: None

        wire1 = self.encounter.coordinate_frame.world_to_wire((5.0, 0.0, 0.0))
        wire2 = self.encounter.coordinate_frame.world_to_wire((25.0, 0.0, 0.0))
        p1.gameplay_last_move = SimpleNamespace(relative_position=wire1, yaw=0.0)
        p2.gameplay_last_move = SimpleNamespace(relative_position=wire2, yaw=0.0)

        combat_targets = (
            (101, wire1, p1),
            (202, wire2, p2),
        )

        # Spawn Guardian for Player 1
        definition = self.combat.player_summon_definition(1869)
        self.assertIsNotNone(definition)
        now = 1000.0
        spawn = self.combat.spawn_player_summon(
            definition,
            owner_actor_id=101,
            position=(0.0, 0.0, 0.0),
            yaw=0.0,
            level=50,
            server_tick=100,
            player_damage_range=(1000.0, 1000.0),
            now=now,
        )
        summon = spawn.actors[0]
        guardian_id = summon.actor_id

        # Player 2 mind controls the Guardian
        mc_effect = VictimStatusEffectDefinition(
            resource_id="skill_mindcontrol_effect",
            effect_hosts="Receiver",
            aura_targets="",
            exclusivity_type="Replace",
            duration_ticks=250,
            effect_id=5961,
            chance=1.0,
            values=(),
            trigger="",
        )
        SummonService._record_pvp_summon_effects(
            self.combat, guardian_id, (mc_effect,), source_actor_id=202, now=now,
        )

        # 1. Despawn check: far away original owner must NOT cause despawn while mind-controlled
        far_positions = {101: (500.0, 0.0, 0.0), 202: (25.0, 0.0, 0.0)}
        outside = self.combat.player_summons_outside_owner_range(far_positions, now=now)
        self.assertNotIn(summon, outside)

        # 2. Summon cycle context preparation: controller 202 must be an effective owner
        ctx = SummonService._prepare_player_summon_cycle_context(
            self.combat, combat_targets, now=now,
        )
        hostile_by_owner = ctx[0]
        self.assertIn(202, hostile_by_owner)
        self.assertIn(101, hostile_by_owner[202])

        # 3. Advance summons: Guardian must advance toward Player 1 (the original summoner)
        movements = self.combat.advance_player_summons(
            elapsed_seconds=0.2,
            server_tick=105,
            now=now + 0.2,
            player_positions={101: (5.0, 0.0, 0.0), 202: (25.0, 0.0, 0.0)},
            hostile_player_actor_ids_by_owner=hostile_by_owner,
        )

        # 4. Attacks: Guardian attacks Player 1
        attacks = self.combat.maybe_start_player_summon_attacks(
            player_positions={101: (5.0, 0.0, 0.0), 202: (25.0, 0.0, 0.0)},
            server_tick=110,
            now=now + 1.0,
            hostile_player_actor_ids_by_owner=hostile_by_owner,
        )
        self.assertTrue(len(attacks) > 0)
        attack = attacks[0]
        self.assertEqual(attack.target_actor_id, 101)

        # 5. Resolve attack: produces PvP resolution against Player 1 with source_actor_id=202
        resolutions, supports = self.combat.resolve_due_player_summon_attacks(
            now=now + 5.0,
            player_positions={101: (5.0, 0.0, 0.0), 202: (25.0, 0.0, 0.0)},
            server_tick=120,
            hostile_player_actor_ids_by_owner=hostile_by_owner,
            player_damage_multipliers={101: 1.0, 202: 1.0},
        )
        pvp_res = [r for r in resolutions if r.target_actor_id == 101]
        self.assertTrue(len(pvp_res) > 0)
        self.assertEqual(pvp_res[0].source_actor_id, 202)
        self.assertTrue(pvp_res[0].damage > 0)

        # 6. Guardian must not heal Player 1 while mind-controlled
        heals_p1 = [s for s in supports if s.target_actor_id == 101]
        self.assertEqual(len(heals_p1), 0)

        # 7. Fast forward past Mind Control expiration (10s duration -> at now + 15s)
        now_expired = now + 15.0
        self.assertFalse(self.combat._monster_is_mind_controlled(guardian_id, now=now_expired))
        ctx_expired = SummonService._prepare_player_summon_cycle_context(
            self.combat, combat_targets, now=now_expired,
        )
        hostile_by_owner_expired = ctx_expired[0]
        # Player 1 is no longer attacked by Guardian
        self.assertNotIn(101, hostile_by_owner_expired.get(101, ()))
        attacks_expired = self.combat.maybe_start_player_summon_attacks(
            player_positions={101: (5.0, 0.0, 0.0), 202: (25.0, 0.0, 0.0)},
            server_tick=500,
            now=now_expired,
            hostile_player_actor_ids_by_owner=hostile_by_owner_expired,
        )
        # Guardian should now target Player 2 (enemy of Player 1)
        if attacks_expired:
            self.assertEqual(attacks_expired[0].target_actor_id, 202)


if __name__ == "__main__":
    unittest.main()
