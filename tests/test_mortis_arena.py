"""Exercise Mortis' real map content through server phase transitions."""

from dataclasses import replace
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dso_server.gameplay.local_map_state import load_map_coordinate_frame
from dso_server.gameplay.local_monster_state import load_local_monster_encounter, _load_runtime_definition
from dso_server.gameplay.mortis_arena import (
    MAP_ID, DRAGON, ADD_DRAGON, MOUNTED, UNBOUND, freeze_effect_ids,
    released_spawn_batch, select_spawn_rows,
)
from dso_server.gameplay.map_event_state import load_map_event_content
from dso_server.paths import DEFAULT_STATIC_DB_PATH as STATIC
from dso_server.protocol.combat_protocol import (
    SHIFTED_SKILL_COMMAND, TARGET_POINT_BULLET_SKILL_COMMAND,
    decode_monster_attack_batch,
)
from dso_server.protocol.gameplay_protocol import StatusEffectServerCommand
from dso_server.services.character_server import CharacterServer
from dso_server.services.mortis_arena import arena
from dso_server.tests.test_pvp_matchmaking import _quiet_server


class MortisArenaTests(unittest.TestCase):
    def setUp(self):
        from dso_server.tests.test_map_events import MapEventServiceTests
        self.server = _quiet_server(self)
        self.server.sessions = {}
        self.server.gameplay_world_lock = threading.RLock()
        self.encounter = load_local_monster_encounter(
            STATIC, MAP_ID, monster_level=100, scaling_difficulty=7,
            include_spawn_areas=False,
        )
        self.frame = load_map_coordinate_frame(STATIC, MAP_ID)
        self.session = MapEventServiceTests._session(self, 50114, 1)
        self.session.gameplay_map = MAP_ID
        self.session.gameplay_map_access_group_id = MAP_ID
        self.server.sessions[self.session.addr] = self.session
        self.runtime = self.server._attach_map_event_runtime(
            self.session, map_id=MAP_ID, initial_flags=frozenset(),
            monster_level=100, scaling_difficulty=7, fresh_instance=True,
        )
        self.runtime.scheduler = None  # drive delayed edges explicitly, no threads
        self.combat = self.session.gameplay_combat_state
        patcher = patch.object(CharacterServer, "_start_monster_chase", lambda *a: False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _alive(self, template=None):
        return tuple(m for a, m in self.combat.monsters.items()
                     if self.combat.monster_health.get(a, 0) > 0
                     and (template is None or m.template_id.casefold() == template))

    def _kill(self, monsters):
        for m in monsters:
            self.combat.monster_health[m.actor_id] = 0
        self.server._on_monsters_killed(self.session, tuple(monsters))

    def _start(self):
        self.server._fire_map_signal(self.session, "PlayerHitTrigger", ("BOSS_start_trigger",))

    def _reach_unbound(self):
        self._start()
        self._kill(self._alive(DRAGON))
        actors = self.runtime.event_set_actor_ids[4216]
        self._kill(tuple(self.combat.monsters[a] for a in actors))
        self._kill(self._alive(MOUNTED))
        return self._alive(UNBOUND)[0]

    def test_statues_awaken_in_order_and_late_viewers_do_not_refreeze_them(self):
        self.assertEqual({m.template_id for m in self._alive()}, {DRAGON, ADD_DRAGON})
        dragon = self._alive(DRAGON)[0]
        add = self._alive(ADD_DRAGON)[0]
        self.assertTrue(dragon.invulnerable)
        self.assertEqual(self.combat.active_monster_actor_ids, set())
        self._start()
        self.assertIn(dragon.actor_id, self.combat.mortis_released_actor_ids)
        self.assertNotIn(add.actor_id, self.combat.mortis_released_actor_ids)
        batch = released_spawn_batch(self.combat, self.encounter.spawn_batch)
        locks = freeze_effect_ids(str(STATIC))
        statuses = {c.actor_id: {e.effect_id for e in c.value.entries}
                    for c in batch.commands if isinstance(c, StatusEffectServerCommand)}
        self.assertFalse(statuses[dragon.actor_id] & locks)
        self.assertTrue(statuses[add.actor_id] & locks)
        controller = arena(self.server, self.runtime)
        with patch("dso_server.services.mortis_arena.time.perf_counter", return_value=time.perf_counter() + 10):
            controller._tick()
        self.assertFalse(self.combat.monsters[dragon.actor_id].invulnerable)
        self.assertTrue(self.combat.monsters[add.actor_id].invulnerable)
        self.combat.last_monster_attack_at[dragon.actor_id] = 0
        self.combat._monster_skill_ready_at[dragon.actor_id] = {}
        x, y, z = dragon.position
        casts = self.combat.maybe_start_monster_attacks(
            self.frame.world_to_wire((x - 3.0, y, z)), server_tick=5000,
            now=time.perf_counter() + 11, target_actor_id=self.session.gameplay_remote_actor_id,
            eligible_actor_ids={dragon.actor_id},
        )
        self.assertEqual(len(casts), 1)
        self.assertTrue(casts[0].definition.resource_id.startswith("Boss_Bone_Dragon_"))

    def test_wave_requires_every_real_actor_and_spawns_only_one_mounted_boss(self):
        self._start()
        self._kill(self._alive(DRAGON))
        actors = self.runtime.event_set_actor_ids[4216]
        self.assertGreaterEqual(len(actors), 17)
        self.assertLessEqual(len(actors), 21)
        wave = tuple(self.combat.monsters[a] for a in actors)
        # Obsolete static selectors used to declare victory after the archers.
        first = wave[:len(wave) - 1]
        self._kill(first)
        self.assertFalse(self._alive(MOUNTED))
        self._kill(wave[-1:])
        self.assertEqual(len(self._alive(MOUNTED)), 1)
        self.server._fire_map_signal(self.session, "MonstersEliminated", ())
        self.assertEqual(len(self._alive(MOUNTED)), 1)

    def test_final_form_incidents_are_once_only_and_death_cleans_up_arena(self):
        boss = self._reach_unbound()
        self.assertEqual(len(self._alive(UNBOUND)), 1)
        self.assertNotIn("boss_death", self.runtime.running)
        controller = arena(self.server, self.runtime)
        self.combat.monster_health[boss.actor_id] -= 1
        controller._tick()
        count = len(self.combat.monsters)
        self.assertGreater(count, 20)
        controller._tick()
        self.assertEqual(len(self.combat.monsters), count)
        self.combat.monster_health[boss.actor_id] = max(1, boss.maximum_health // 4)
        controller._tick()
        add = self._alive(ADD_DRAGON)[0]
        self.assertIn(add.actor_id, self.combat.mortis_released_actor_ids)
        self.assertEqual(len(controller.incidents), 2)
        self._kill((boss,))
        self.assertTrue(controller.completed)
        self.assertFalse(self._alive())
        self.assertIn("boss_death", self.runtime.running)
        self.assertTrue(any(exit.active for exit in self.session.gameplay_map_exits))

    def test_random_pack_choice_is_stable_and_tiers_never_stack(self):
        rows = load_map_event_content(STATIC, MAP_ID).spawn_rows
        for tier in range(10):
            self.runtime.scaling_difficulty = tier
            wave = tuple(r for r in rows if r.event_set_row == 4216)
            chosen = select_spawn_rows(STATIC, self.runtime, wave)
            self.assertTrue(17 <= len(chosen) <= 21)
            self.assertEqual(chosen, select_spawn_rows(STATIC, self.runtime, wave))
            gate = select_spawn_rows(STATIC, self.runtime, tuple(r for r in rows if r.event_set_row == 4214))
            self.assertTrue(1 <= len(gate) <= 6)
            self.assertTrue(all(r.position[2] > 0 for r in gate))

    def test_unbound_waits_for_mounted_death_animation(self):
        self._start()
        self._kill(self._alive(DRAGON))
        self._kill(tuple(self.combat.monsters[a] for a in self.runtime.event_set_actor_ids[4216]))
        self.runtime.scheduler = SimpleNamespace(timer=lambda *a, **k: SimpleNamespace(start=lambda: None, cancel=lambda: None))
        self._kill(self._alive(MOUNTED))
        controller = arena(self.server, self.runtime)
        self.server._start_map_local_event(
            self.runtime, self.runtime.catalog.event("boss_mortis_unbound_spawn"),
        )
        deadline = controller.unbound_due_at
        self.assertAlmostEqual(deadline - controller.mounted_death_at, 6.36, places=2)
        with patch("dso_server.services.mortis_arena.time.perf_counter", return_value=deadline - 0.01):
            controller._tick()
        self.assertFalse(self._alive(UNBOUND))
        with patch("dso_server.services.mortis_arena.time.perf_counter", return_value=deadline + 0.01):
            controller._tick()
        self.assertEqual(len(self._alive(UNBOUND)), 1)

    def test_final_form_loads_all_four_authored_skills_with_point_payloads(self):
        definition = _load_runtime_definition(STATIC, UNBOUND, 1)
        skills = {a.resource_id: a for a in definition.attacks}
        self.assertEqual(len(skills), 4)
        self.assertEqual(skills["BossMortisUnbound_Charge"].command_id, TARGET_POINT_BULLET_SKILL_COMMAND)
        self.assertTrue(skills["BossMortisUnbound_Charge"].movement_skill)
        self.assertEqual(skills["BossMortisUnbound_GraveDigger"].command_id, SHIFTED_SKILL_COMMAND)

    def test_surviving_gate_minions_transform_but_killed_minions_do_not(self):
        self._reach_unbound()
        controller = arena(self.server, self.runtime)
        controller._spawn((("boss_mortis_unbound_add_skeleton_warrior_01", (42.0, 0.0, 14.0)),))
        skeleton = self._alive("boss_mortis_unbound_add_skeleton_warrior_01")[0]
        deadline, despawn = controller.lifetimes[skeleton.actor_id]
        with patch("dso_server.services.mortis_arena.time.perf_counter", return_value=deadline + despawn + 0.01):
            controller._tick()
        self.assertEqual(self.combat.monster_health[skeleton.actor_id], 0)
        (ripper,) = self._alive("boss_mortis_unbound_add_ripper_01")
        expiry = controller.lifetimes[ripper.actor_id][0]
        self._kill((ripper,))
        with patch("dso_server.services.mortis_arena.time.perf_counter", return_value=expiry + 10):
            controller._tick()
        self.assertFalse(self._alive("boss_mortis_unbound_add_white_mage"))

    def test_incident_minions_are_reachable_from_the_fight(self):
        boss = self._reach_unbound()
        self.combat.monster_health[boss.actor_id] = max(1, boss.maximum_health // 4)
        arena(self.server, self.runtime)._tick()
        unreachable = [
            (m.template_id, self.combat.monster_positions[m.actor_id])
            for m in self._alive()
            if not self.encounter.navigation.is_reachable(
                (42.0, 0.0, 14.0), self.combat.monster_positions[m.actor_id],
            )
        ]
        self.assertEqual(unreachable, [])

    def test_white_mage_ground_aura_heals_mortis_once_on_entry(self):
        boss = self._reach_unbound()
        controller = arena(self.server, self.runtime)
        position = self.combat.monster_positions[boss.actor_id]
        controller._spawn((("boss_mortis_unbound_add_white_mage", position),))
        (mage,) = self._alive("boss_mortis_unbound_add_white_mage")
        heal_skill = next(a for a in mage.attacks if a.resource_id == "unbound_white_mage_heal_meteor")
        visuals = self.combat.player_skill_location_visuals(heal_skill.skill_manager_index)
        self.assertTrue(visuals)
        self.combat.start_player_location_visuals(
            visuals, position=position, yaw=0.0, server_tick=5000,
            owner_actor_id=mage.actor_id, source_skill_index=heal_skill.skill_manager_index, now=0.0,
        )
        hp = boss.maximum_health // 2
        self.combat.monster_health[boss.actor_id] = hp
        controller._heal_auras(self.combat, (self.session,), 0.1)
        self.assertEqual(self.combat.monster_health[boss.actor_id], hp + round(boss.maximum_health * 0.12))
        healed = self.combat.monster_health[boss.actor_id]
        controller._heal_auras(self.combat, (self.session,), 0.2)
        self.assertEqual(self.combat.monster_health[boss.actor_id], healed)
        self.combat.monster_positions[boss.actor_id] = (position[0] + 30, position[1], position[2])
        controller._heal_auras(self.combat, (self.session,), 0.3)
        self.assertEqual(self.combat.monster_health[boss.actor_id], healed)

    def test_skills_serialize_and_land_damage_with_synchronized_charge_endpoint(self):
        boss = self._reach_unbound()
        origin, target = (42.0, 0.0, 14.0), (47.0, 0.0, 14.0)
        now = time.perf_counter() + 100
        for definition in boss.attacks:
            with self.subTest(skill=definition.resource_id):
                self.combat.monsters[boss.actor_id] = replace(boss, attack=definition, attacks=(definition,))
                self.combat.monster_positions[boss.actor_id] = origin
                self.combat.pending_monster_attacks.clear()
                self.combat.pending_monster_projectile_attacks.clear()
                self.combat.last_monster_attack_at[boss.actor_id] = 0
                self.combat.monster_skill_unblock_at[boss.actor_id] = 0
                self.combat.monster_motion_unblock_at[boss.actor_id] = 0
                self.combat._monster_skill_ready_at[boss.actor_id] = {}
                self.combat.player_health[self.session.gameplay_remote_actor_id] = [10**9, 10**9]
                (cast,) = self.combat.maybe_start_monster_attacks(
                    self.frame.world_to_wire(target), server_tick=100000, now=now,
                    target_actor_id=self.session.gameplay_remote_actor_id,
                    eligible_actor_ids={boss.actor_id},
                )
                self.session.recorder.messages.clear()
                CharacterServer._send_monster_activity_to(
                    self.session, self.combat, (), (cast,), (), (),
                )
                batches = [
                    decode_monster_attack_batch(data, bits)
                    for data, bits, label in self.session.recorder.messages
                    if label == "monster-skill"
                ]
                self.assertTrue(batches)
                command = batches[-1].attacks[0]
                self.assertEqual(command.value.skill_index, definition.skill_manager_index)
                if definition.command_id in {SHIFTED_SKILL_COMMAND, TARGET_POINT_BULLET_SKILL_COMMAND}:
                    self.assertEqual(tuple(command.value.target_position), tuple(cast.target_position))
                hits = self.combat.resolve_due_monster_attacks(
                    self.frame.world_to_wire(target), now=cast.due_at + 0.001,
                    player_actor_id=self.session.gameplay_remote_actor_id,
                )
                self.assertTrue(any(hit.damage > 0 for hit in hits))
                if definition.movement_skill:
                    self.assertEqual(self.combat.monster_positions[boss.actor_id], cast.target_position)
                now += 100

    def test_charge_hits_each_player_in_its_lane_once(self):
        boss = self._reach_unbound()
        charge = next(a for a in boss.attacks if a.movement_skill)
        self.combat.monsters[boss.actor_id] = replace(boss, attack=charge, attacks=(charge,))
        self.combat.monster_positions[boss.actor_id] = (42.0, 0.0, 14.0)
        self.combat.last_monster_attack_at[boss.actor_id] = 0
        self.combat.monster_skill_unblock_at[boss.actor_id] = 0
        self.combat.monster_motion_unblock_at[boss.actor_id] = 0
        self.combat._monster_skill_ready_at[boss.actor_id] = {}
        owner = self.session.gameplay_remote_actor_id
        positions = ((owner, (52.0, 0.0, 14.0)), (2, (47.0, 0.0, 14.0)), (3, (47.0, 0.0, 20.0)))
        for actor, _ in positions:
            self.combat.player_health[actor] = [10**9, 10**9]
        now = time.perf_counter() + 100
        (cast,) = self.combat.maybe_start_monster_attacks(
            self.frame.world_to_wire(positions[0][1]), server_tick=50000, now=now,
            target_actor_id=owner, eligible_actor_ids={boss.actor_id},
        )
        self.assertLessEqual(cast.due_at, self.combat.monster_skill_unblock_at[boss.actor_id])
        hits = tuple(
            hit for actor, position in positions
            for hit in self.combat.resolve_due_monster_attacks(
                self.frame.world_to_wire(position), now=cast.due_at + 0.001,
                player_actor_id=actor, finalize_area_attacks=actor == 3,
            )
        )
        self.assertEqual({h.target_actor_id for h in hits}, {owner, 2})
        self.assertEqual(len(hits), 2)
        self.assertNotIn(boss.actor_id, self.combat.pending_monster_attacks)
