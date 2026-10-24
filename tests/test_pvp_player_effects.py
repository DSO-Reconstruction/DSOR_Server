"""Exercise PvP impact, victim state, and packets with a real arena frame."""

from dataclasses import replace
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dso_server.paths import (
    DEFAULT_STATIC_DB_PATH,
)
from dso_server.protocol.combat_protocol import (
    KillServerCommand,
    SkillCast,
    decode_combat_batch,
)
from dso_server.protocol.gameplay_protocol import StatusEffectServerCommand
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_player_only_encounter
from dso_server.gameplay.player_combat_stats import calculate_player_combat_stats
from dso_server.services.base import CharacterService
from dso_server.services import pvp_combat
from dso_server.tests.test_pvp_matchmaking import _quiet_server, _session


class PVPPlayerEffectTests(unittest.TestCase):
    def setUp(self):
        self.server = _quiet_server(self)
        self.source = _session(51001, 1, "Caster")
        self.target = _session(51002, 2, "Victim")
        self.combat = LocalCombatState(
            load_local_player_only_encounter(DEFAULT_STATIC_DB_PATH, "b1102_dm"),
            player_actor_id=self.source.gameplay_remote_actor_id, player_health=1000,
        )
        self.combat.register_player(self.target.gameplay_remote_actor_id, 1000)
        self.frame = self.combat.encounter.coordinate_frame
        for session in (self.source, self.target):
            session.gameplay_combat_state = self.combat
            session.gameplay_pvp_enabled = True
            session.gameplay_respawn_allowed = True
            session.gameplay_chase_server_tick = 200
            session.gameplay_presence_callback = lambda s, event: (
                CharacterService._handle_presence_event(self.server, s, event)
            )
            self.move(session, (25.49, 0.0, 53.57))
        self.server._active_gameplay_sessions = lambda **kwargs: tuple(
            s for s in (self.source, self.target) if s is not kwargs["excluding"]
        )
        self.server.social_store = SimpleNamespace(group_for=lambda _: None)
        self.timers = []
        def timer(delay, function, args=(), kwargs=None):
            value = SimpleNamespace(
                delay=delay, function=function, args=args, kwargs=kwargs or {},
                cancel=Mock(), start=Mock(), daemon=False,
            )
            self.timers.append(value)
            return value
        self.source.gameplay_deadline_timer = timer
        for name in ("_dispatch_socket_event", "_update_low_health_socket_state",
                     "_send_player_skill_status_effects"):
            patcher = patch.object(CharacterService, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        stats = SimpleNamespace(incoming_damage_multiplier=lambda _: 1.0)
        patcher = patch.object(pvp_combat, "calculate_player_combat_stats", return_value=stats)
        patcher.start()
        self.addCleanup(patcher.stop)

    def move(self, session, world):
        session.gameplay_last_move = SimpleNamespace(
            relative_position=self.frame.world_to_wire(world), yaw=0.0,
        )

    def effect(self, encoded="debuff_cc_stun,D:5,DP:2"):
        with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as database:
            return LocalCombatState._load_victim_status_effects(database, encoded)[0]

    def impact(self, damage=0, effects=()):
        key = ("pvp-hit", 1)
        pending = SimpleNamespace(
            round_token=None, combat=self.combat, target_session=self.target,
            target_actor_id=self.target.gameplay_remote_actor_id,
            source_actor_id=self.source.gameplay_remote_actor_id,
            skill_index=0, cast_id=1, server_tick=200,
            # Deliberately stale: the body must use the target's impact position.
            position=self.frame.world_to_wire((10.0, 0.0, 10.0)),
            damage=damage, victim_status_effects=effects, user_status_effects=(),
            frost_hit=False, damage_type="Fire", player_damage_range=(100.0, 100.0),
            critical_hit=False, blocked=False,
        )
        self.source.gameplay_pending_pvp_hits[key] = pending
        CharacterService._resolve_pvp_skill_hit(
            self.source, self.source.gameplay_chase_generation, key,
        )
        return pending

    def commands(self, session):
        return tuple(command
                     for data, bits, label in session.recorder.messages
                     if label == "pvp-player-hit"
                     for command in decode_combat_batch(data, bits).commands)

    def assert_body_position(self, world):
        for session, actor in ((self.source, self.target.gameplay_remote_actor_id),
                               (self.target, 1)):
            kill = next(c for c in self.commands(session) if isinstance(c, KillServerCommand))
            self.assertEqual(kill.actor_id, actor)
            self.assertEqual(kill.value.animation_time, 2_160_000)
            for actual, expected in zip(kill.value.position, world):
                self.assertAlmostEqual(actual, expected, places=4)

    def test_lethal_impact_uses_current_world_position_on_both_clients(self):
        self.impact(damage=1000)
        self.assert_body_position((25.49, 0.0, 53.57))
        self.assertTrue(self.target.gameplay_death_pending)

    def test_lethal_dot_uses_current_world_position_after_victim_moves(self):
        with patch.object(self.combat, "monster_status_damage_program",
                          return_value=(0.5, 1000.0, "Fire", False)):
            self.impact(effects=(self.effect(),))
        timer = self.timers[-1]
        self.move(self.target, (30.66, 0.0, 45.85))
        timer.function(*timer.args)
        self.assert_body_position((30.66, 0.0, 45.85))

    def test_status_only_hit_uses_pvp_duration_and_locks_victim(self):
        effect = self.effect()
        self.impact(effects=(effect,))
        self.assertEqual(self.combat.player_health[self.target.gameplay_remote_actor_id][0], 1000)
        for session in (self.source, self.target):
            commands = self.commands(session)
            self.assertTrue(commands)
            self.assertTrue(all(isinstance(c, StatusEffectServerCommand) for c in commands))
        entry = self.target.gameplay_victim_status_effect_instances[(1, effect.effect_id)][0]
        self.assertEqual(entry.field_10, 50)
        self.assertIn("regularskills", CharacterService._player_disabled_actor_features(self.target))
        self.assertIn(entry, CharacterService._active_skill_modifier_sources(self.target))
        self.assertNotIn(entry, CharacterService._active_skill_modifier_sources(self.source))

    def test_effects_are_from_caster_only_and_reach_distant_victim(self):
        caster_effect = self.effect()
        victim_effect = self.effect("debuff_cc_frost,D:5")
        self.move(self.target, (1000.0, 0.0, 1000.0))
        with patch.object(CharacterService, "_item_set_procs_for_skill",
                          side_effect=lambda s, _: {"victim": (caster_effect if s is self.source
                                                               else victim_effect,)}) as procs:
            self.impact()
        procs.assert_called_once_with(self.source, 0)
        self.assertEqual({key[1] for key in self.target.gameplay_victim_status_effect_instances},
                         {caster_effect.effect_id})
        self.assertEqual({key[1] for key in self.source.gameplay_victim_status_effect_instances},
                         {caster_effect.effect_id})

    def test_immunity_acquired_after_cast_blocks_status_at_impact(self):
        effect = self.effect()
        groups = self.combat._status_groups_for_effect(effect.effect_id)
        self.assertTrue(groups)
        with patch.object(CharacterService, "_player_status_immunity_groups", return_value=groups):
            self.impact(damage=10, effects=(effect,))
        self.assertFalse(self.target.gameplay_victim_status_effect_instances)
        self.assertEqual(self.combat.player_health[self.target.gameplay_remote_actor_id][0], 990)

    def test_replaced_dot_instance_cannot_keep_damaging_victim(self):
        effect = self.effect()
        with patch.object(self.combat, "monster_status_damage_program",
                          return_value=(0.5, 10.0, "Fire", False)):
            self.impact(effects=(effect,))
        timer = self.timers[-1]
        cache = self.target.gameplay_victim_status_effect_instances
        entry, expiry = cache[(1, effect.effect_id)]
        cache[(1, effect.effect_id)] = (replace(entry, field_04=entry.field_04 + 1), expiry)
        timer.function(*timer.args)
        self.assertEqual(self.combat.player_health[self.target.gameplay_remote_actor_id][0], 1000)

    def test_status_only_skill_schedules_zero_damage(self):
        geometry = replace(
            self.combat._load_player_skill_geometry(1848),
            scale_skill_timing=False, hit_frame=0, bullet_emit_delay=0.0,
            targeting_type="Actor", hit_range=1.0, impact_radius=0.0,
            attack_range=20.0, projectile_range=0.0, projectile_velocity=0.0,
            damage_modifier=0.0, damage_type="Physical",
            victim_status_effects=(self.effect(),), location_status_effects=(),
            bullet_location_effects=(), bullet_death_effects=(),
        )
        with patch.object(self.combat, "_load_player_skill_geometry", return_value=geometry), \
                patch.object(self.combat, "_modified_player_skill_geometry", side_effect=lambda g, _: g), \
                patch.object(self.combat, "player_skill_impact_delay_seconds", return_value=0.0):
            timers = CharacterService._prepare_pvp_skill_hits(
                self.source, self.combat, SkillCast(0, 0, 0.0, 200, 2),
                source_position=self.source.gameplay_last_move.relative_position,
                source_actor_id=self.source.gameplay_remote_actor_id,
                player_damage_range=(100.0, 100.0), attack_speed_multiplier=1.0,
                modifier_sources=(), explicit_target_actor_ids=(self.target.gameplay_remote_actor_id,),
            )
        self.assertEqual(len(timers), 1)
        self.assertEqual(next(iter(self.source.gameplay_pending_pvp_hits.values())).damage, 0)

    def test_impact_resends_only_hit_caster_effects(self):
        """Blood Mage's skillstart health cost must not repeat per PvP hit."""

        self_damage = self.effect(
            "talent_bloodmage_self_damage,C:1.0,D:1.0,$0:-0.1,ON:SkillStart"
        )
        on_hit = self.effect("debuff_cc_stun,D:5")
        geometry = replace(
            self.combat._load_player_skill_geometry(1848),
            scale_skill_timing=False, hit_frame=0, bullet_emit_delay=0.0,
            targeting_type="Actor", hit_range=1.0, impact_radius=0.0,
            attack_range=20.0, projectile_range=0.0, projectile_velocity=0.0,
            damage_type="Physical", victim_status_effects=(),
            user_status_effects=(self_damage, on_hit),
            location_status_effects=(), bullet_location_effects=(),
            bullet_death_effects=(),
        )
        with patch.object(self.combat, "_load_player_skill_geometry", return_value=geometry), \
                patch.object(self.combat, "_modified_player_skill_geometry", side_effect=lambda g, _: g), \
                patch.object(self.combat, "player_skill_impact_delay_seconds", return_value=0.0):
            CharacterService._prepare_pvp_skill_hits(
                self.source, self.combat, SkillCast(0, 0, 0.0, 200, 2),
                source_position=self.source.gameplay_last_move.relative_position,
                source_actor_id=self.source.gameplay_remote_actor_id,
                player_damage_range=(100.0, 100.0), attack_speed_multiplier=1.0,
                modifier_sources=(), explicit_target_actor_ids=(self.target.gameplay_remote_actor_id,),
            )
        pending = next(iter(self.source.gameplay_pending_pvp_hits.values()))
        self.assertEqual(self_damage.trigger, "skillstart")
        self.assertEqual(pending.user_status_effects, (on_hit,))

    def test_weaken_changes_victims_outgoing_damage_and_expires(self):
        def stats():
            return calculate_player_combat_stats(
                DEFAULT_STATIC_DB_PATH, class_name="mage", level=100,
                inventory=SimpleNamespace(items=[], equipment_slots={}),
                modifier_sources=CharacterService._active_skill_modifier_sources(self.target),
            )
        before = stats()
        effect = self.effect("debuff_all_weaken,D:5")
        self.impact(effects=(effect,))
        after = stats()
        self.assertAlmostEqual(after.minimum_damage, before.minimum_damage * 0.1)
        self.assertAlmostEqual(after.maximum_damage, before.maximum_damage * 0.1)
        cache = self.target.gameplay_victim_status_effect_instances
        entry, _expiry = cache[(1, effect.effect_id)]
        cache[(1, effect.effect_id)] = (entry, 0.0)
        self.assertEqual(stats().maximum_damage, before.maximum_damage)

    def test_old_dot_callback_leaves_replacement_timer_tracked(self):
        effect = self.effect()
        with patch.object(self.combat, "monster_status_damage_program",
                          return_value=(0.5, 10.0, "Fire", False)):
            self.impact(effects=(effect,))
            old = self.timers[-1]
            self.impact(effects=(effect,))
        replacement = self.timers[-1]
        old.function(*old.args)
        self.assertIs(self.source.gameplay_projectile_timers[old.args[2]], replacement)
        self.assertEqual(self.combat.player_health[self.target.gameplay_remote_actor_id][0], 1000)

    def test_duration_modifiers_keep_pve_and_pvp_separate(self):
        current = self.effect()
        encoded = "debuff_cc_stun,D:1<absolute,DP:0.5<absolute"
        modifier = self.effect(encoded)
        with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as database:
            result = self.combat._merge_status_effect_modifier(database, current, modifier, encoded, 2)
        self.assertEqual(result.duration_ticks, 175)
        self.assertEqual(result.pvp_duration_ticks, 75)

    @staticmethod
    def skill_index(resource_id):
        with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as database:
            return database.execute(
                "SELECT rowid - 1 FROM _Template_Skill WHERE Id=?", (resource_id,),
            ).fetchone()[0]

    def test_cleansing_skill_breaks_stun_on_both_clients(self):
        effect = self.effect()
        self.impact(effects=(effect,))
        teleport = self.skill_index("teleport")
        self.assertEqual(self.combat.player_skill_cleanse_groups(teleport),
                         frozenset({"debuff", "disabled"}))
        self.assertTrue(CharacterService._player_skill_cast_locked(
            self.target, self.skill_index("enragingleap")))
        self.assertFalse(CharacterService._player_skill_cast_locked(self.target, teleport))
        with patch.object(CharacterService, "_refresh_player_combat_vitals") as refresh:
            CharacterService._cleanse_player_skill_debuffs(
                self.target, self.combat, skill_index=teleport, modifier_sources=())
        refresh.assert_called_once()
        self.assertFalse(self.target.gameplay_victim_status_effect_instances)
        self.assertFalse(self.source.gameplay_victim_status_effect_instances)
        self.assertNotIn("regularskills",
                         CharacterService._player_disabled_actor_features(self.target))
        for session in (self.source, self.target):
            self.assertIn("player-skill-cleanse",
                          [label for _data, _bits, label in session.recorder.messages])

    def test_skills_lock_is_never_cleansed_away(self):
        with patch.object(CharacterService, "_player_disabled_actor_features",
                          return_value=frozenset({"movement", "skills", "regularskills"})):
            self.assertTrue(CharacterService._player_skill_cast_locked(
                self.target, self.skill_index("teleport")))

    def test_talent_cleanse_extends_skill_cleanse_groups(self):
        frenzyshout = self.skill_index("frenzyshout")
        self.assertFalse(self.combat.player_skill_cleanse_groups(frenzyshout))
        talent = SimpleNamespace(
            effect_id=self.combat._status_effect_ids["pvp_warrior_talent2_activator"],
            field_18=0, values=(),
        )
        self.assertIn("disabled", self.combat.player_skill_cleanse_groups(
            frenzyshout, modifier_sources=(talent,)))


if __name__ == "__main__":
    unittest.main()
