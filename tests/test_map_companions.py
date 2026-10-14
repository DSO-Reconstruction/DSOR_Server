"""Retail tutorial escort, boss, wounded-NPC and exit regressions."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dso_server.gameplay.map_companions import load_map_companion_spawns
from dso_server.gameplay.local_monster_state import load_local_monster_encounter, _DirectNavigationMesh
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.quest_content import QuestProgress, QUEST_STATE_CLOSED, QUEST_STATE_FINISHED, apply_quest_event
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.character_selection import CLASS_NAMES
from dso_server.protocol.gameplay_protocol import Move, NewMonsterServerCommand, decode_monster_spawn_batch, decode_npc_batch, NewNPC
from dso_server.protocol.combat_protocol import decode_combat_batch, KillServerCommand
from dso_server.services.character_server import CharacterServer
from dso_server.tests import test_quests as quest_fixtures

TOMB = "a0004_start_tomb_dun"
FORTRESS = "a0005_start_fortress_dun"
BOSS = "a0005_champion_grimmag_01_q_WayToGrimmagstone_03"
NPC = "a0005_start_fortress_spellweaver"


class Timer:
    def __init__(self, seconds, function, args=(), kwargs=None):
        self.seconds, self.function, self.args, self.kwargs = seconds, function, args, kwargs or {}
    def start(self):
        pass
    def cancel(self):
        pass
    def run(self):
        self.function(*self.args, **self.kwargs)


class Scheduler:
    def __init__(self):
        self.timers = []
    def timer(self, *args, **kwargs):
        timer = Timer(*args, **kwargs)
        self.timers.append(timer)
        return timer


class MapCompanionTests(unittest.TestCase):
    _session = quest_fixtures.QuestServiceTests._session

    def setUp(self):
        quest_fixtures.QuestServiceTests.setUp(self)
        patcher = patch.object(CharacterServer, "_start_monster_chase", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def enter(self, mid=FORTRESS, *, class_id=2):
        self.session = self._session(mid, 50601, class_id=class_id)
        self.session.gameplay_character.level = 5
        self.server.sessions = {self.session.addr: self.session}
        self.runtime = self.server._attach_map_event_runtime(
            self.session, map_id=mid, initial_flags=frozenset(), monster_level=None,
            scaling_difficulty=0, fresh_instance=True)
        self.runtime.scheduler = Scheduler()
        position = (39.17, 9.0, -11.65) if mid == FORTRESS else (-27.66, -6.0, 244.13)
        self.move(position)
        return self.session.gameplay_combat_state

    def move(self, position):
        frame = self.session.gameplay_coordinate_frame
        move = Move.from_values(frame.world_to_wire(position), 0, 1, 1, 100, 0)
        self.session.gameplay_last_move = move
        return move

    def spawn(self):
        self.server._check_map_companions(self.session, self.session.gameplay_last_move.relative_position)
        return next(iter(self.session.gameplay_combat_state.player_summons.values()))

    def test_each_map_spawns_its_authored_companion_once_with_retail_faction(self):
        previous = None
        for mid, expected in ((TOMB, "a0004_companion_spellweaver_01"),
                              (FORTRESS, "a0005_companion_spellweaver_01")):
            with self.subTest(map=mid):
                combat = self.enter(mid)
                actor = self.spawn()
                self.spawn()
                self.assertEqual(len(combat.player_summons), 1)
                self.assertEqual(actor.definition.monster_template_id, expected)
                self.assertIsNot(combat, previous)
                previous = combat
                packets = [decode_monster_spawn_batch(data, bits)
                           for data, bits, label in self.session.recorder.messages
                           if data[:3] == bytes.fromhex("852a00")]
                monster = next(c.value for p in packets for c in p.commands
                               if isinstance(c, NewMonsterServerCommand))
                self.assertEqual(monster.category, 2)
                self.assertEqual(monster.placement.field_00, 0x10001)
                self.assertEqual(monster.placement.field_08, 0)
                self.assertEqual(monster.field_9c, 4)
                self.assertEqual(monster.field_90, 452)

    def test_map_entry_spawns_companion_without_waiting_for_another_move(self):
        combat = self.enter()
        self.server._on_map_entered(self.session)
        self.assertEqual(len(combat.player_summons), 1)

    def test_spawn_requires_entering_enabled_authored_volume(self):
        combat = self.enter()
        move = self.move((1000, 0, 1000))
        self.server._check_map_companions(self.session, move.relative_position)
        self.assertFalse(combat.player_summons)
        spec = load_map_companion_spawns(DEFAULT_STATIC_DB_PATH, FORTRESS)[0]
        self.move((39.17, 9, -11.65))
        gated = replace(spec, volume=replace(spec.volume, event_set_row=31))
        with patch("dso_server.services.map_companions.load_map_companion_spawns", return_value=(gated,)):
            self.server._check_map_companions(self.session, self.session.gameplay_last_move.relative_position)
        self.assertFalse(combat.player_summons)

    def test_companion_follows_owner_and_does_not_expire_at_construction_leash(self):
        combat = self.enter()
        combat.encounter = replace(combat.encounter, navigation=_DirectNavigationMesh(), terrain=None)
        summon = self.spawn()
        owner = summon.owner_actor_id
        goal = (summon.position[0] + 50, summon.position[1], summon.position[2])
        self.assertFalse(combat.advance_player_summons(elapsed_seconds=.1, server_tick=100,
                         now=summon.created_at+.5, player_positions={owner: goal}))
        moves = combat.advance_player_summons(elapsed_seconds=.1, server_tick=150,
                                             now=summon.created_at+2, player_positions={owner: goal})
        self.assertEqual(len(moves), 1)
        self.assertGreater(moves[0].world_position[0], summon.position[0])
        self.assertFalse(combat.player_summons_outside_owner_range({owner: goal}))

    def test_grimmag_is_combat_boss_and_companion_casts_visible_iceball(self):
        self.enter()
        encounter = load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, FORTRESS)
        combat = LocalCombatState(encounter, player_actor_id=self.session.gameplay_remote_actor_id,
                                  player_health=1000)
        self.session.gameplay_combat_state = combat
        boss = next(m for m in combat.monsters.values() if m.template_id == BOSS)
        self.assertFalse(any(m.template_id.endswith("grimmag_01_spawner") for m in combat.monsters.values()))
        combat.active_actor_ids = {boss.actor_id}
        target = (boss.position[0]+4, boss.position[1], boss.position[2])
        self.move(target)
        summon = self.spawn()
        attacks = combat.maybe_start_player_summon_attacks(
            {summon.owner_actor_id: target}, server_tick=200, now=summon.created_at+2)
        self.assertTrue(attacks)
        geometry = combat._load_player_skill_geometry(summon.definition.attack_skill_index)
        self.assertEqual(geometry.resource_id, "a0004_companion_spellweaver_iceball")
        damage, _ = combat.resolve_due_player_summon_attacks(
            now=summon.created_at+10, server_tick=400,
            player_positions={summon.owner_actor_id: target})
        self.assertTrue(damage)
        self.assertTrue(any(r.damage > 0 for r in damage))
        attacks = combat.maybe_start_monster_attacks(
            encounter.coordinate_frame.world_to_wire(target), server_tick=450,
            now=summon.created_at+11, eligible_actor_ids={boss.actor_id})
        self.assertTrue(attacks)
        self.assertTrue(all("grimmag" in a.definition.resource_id for a in attacks))

    def test_boss_death_wounds_companion_and_opens_exit_through_quest_for_all_classes(self):
        for class_id, name in CLASS_NAMES.items():
            if name not in {"ranger", "mage", "warrior", "dwarf"}:
                continue
            with self.subTest(class_name=name):
                combat = self.enter(class_id=class_id)
                summon = self.spawn()
                combat.player_summons[summon.actor_id] = replace(summon, position=(-14.05, 0, 54.56))
                suffix = {"ranger":"r", "mage":"m", "warrior":"w", "dwarf":"d"}[name]
                quest_id = f"a0002-WayToGrimmagstone-03-{suffix}"
                followup = f"a0005-WayToGrimford-01-{suffix}"
                quest = self.server._quest_catalog().quest(quest_id)
                progress = QuestProgress(quest_id)
                for kind, value in (("trigger", "a0002explore01"), ("trigger", "a0002explore02"),
                                    ("kill", "a0004_champion_undead_mage_01_q_WayToGrimmagstone_03")):
                    apply_quest_event(quest, progress, kind, value)
                self.assertEqual(progress.finished_mask, 7)
                self.session.gameplay_quest_log[quest_id] = progress
                self.server._on_monsters_killed(self.session, (SimpleNamespace(template_id=BOSS, position=(-10.57, 0, 50.02)),))
                self.assertTrue(progress.task_finished(3))
                self.assertNotIn("a0005_start_fortress_npc_01", self.runtime.running)
                injury = next(t for t in self.runtime.scheduler.timers if t.seconds == 2.5)
                injury.run()
                self.assertNotIn(summon.actor_id, combat.player_summons)
                self.server._check_map_companions(self.session, self.session.gameplay_last_move.relative_position)
                self.assertFalse(combat.player_summons)
                kills = [c for data,bits,label in self.session.recorder.messages if label == "map-companion-wounded"
                         for c in decode_combat_batch(data,bits).commands if isinstance(c, KillServerCommand)]
                self.assertEqual([k.actor_id for k in kills], [summon.actor_id])
                reveal = next(t for t in self.runtime.scheduler.timers if t.seconds == 16.5)
                reveal.run()
                self.assertIn("a0005_start_fortress_npc_01", self.runtime.running)
                self.assertTrue(any(label == "map-event-new-npc" for _,_,label in self.session.recorder.messages))
                npc = next(c for data,bits,label in self.session.recorder.messages
                           if label == "map-event-new-npc"
                           for c in decode_npc_batch(data,bits).commands
                           if isinstance(c, NewNPC) and c.template_id == NPC)
                self.move(npc.position)
                self.server._handle_npc_request(self.session, bytes.fromhex("8b2900") + npc.actor_id.to_bytes(4, "little"))
                self.assertEqual(progress.state, QUEST_STATE_FINISHED)
                self.assertTrue(self.server._quest_turn_in(self.session, quest_id))
                self.assertEqual(progress.state, QUEST_STATE_CLOSED)
                self.assertTrue(self.server._quest_accept(self.session, followup, offered=True))
                self.assertIn("a0005_start_fortress_exit_01", self.runtime.running)
                self.assertTrue(any(e.active and e.destination_map_id == "a0006_grimford_hub"
                                    for e in self.session.gameplay_map_exits))

    def test_reentry_restores_handoff_without_duplicate_rewards_or_escort(self):
        combat = self.enter()
        quest_id = "a0002-WayToGrimmagstone-03-r"
        progress = QuestProgress(quest_id, finished_mask=15)
        self.session.gameplay_quest_log[quest_id] = progress
        self.server._on_map_entered(self.session)
        self.assertFalse(combat.player_summons)
        self.assertEqual(progress.finished_mask, 15)
        self.assertFalse(self.xp_awards)
        reveal = next(t for t in self.runtime.scheduler.timers if t.seconds == 16.5)
        reveal.run()
        self.assertIn("a0005_start_fortress_npc_01", self.runtime.running)

    def test_departure_cleanup_allows_companion_on_return_to_same_instance(self):
        combat = self.enter()
        summon = self.spawn()
        combat.expire_player_summons_for_owner(summon.owner_actor_id)
        self.assertFalse(combat.player_summons)
        replacement = self.spawn()
        self.assertNotEqual(replacement.actor_id, summon.actor_id)

    def test_behavior_aura_unlocks_authored_boss_attack_without_permanent_freeze(self):
        combat = self.enter()
        summon = self.spawn()
        summon = replace(summon, position=(-17.0, 0.0, 60.0))
        combat.player_summons[summon.actor_id] = summon
        combat.advance_player_summons(elapsed_seconds=0, server_tick=200,
                                      now=summon.created_at+2, player_positions={})
        changed = combat.player_summons[summon.actor_id]
        geometry = combat._load_player_skill_geometry(changed.definition.attack_skill_index)
        self.assertEqual(geometry.resource_id, "boss_grimmag_companion_spellweaver_iceball_01")
        self.assertFalse(geometry.victim_status_effects)

    def test_old_handoff_timer_cannot_affect_new_map_incarnation(self):
        self.enter()
        self.spawn()
        self.server._on_map_companion_boss_defeated(self.session, (SimpleNamespace(template_id=BOSS, position=(-10.57, 0, 50.02)),))
        timer = self.runtime.scheduler.timers[-1]
        combat = self.enter()
        summon = self.spawn()
        timer.run()
        self.assertIn(summon.actor_id, combat.player_summons)
        self.assertFalse(any(label == "map-companion-wounded" for _,_,label in self.session.recorder.messages))

    def test_companion_reaches_boss_formation_and_switches_cast_in_normal_movement(self):
        self.enter()
        encounter = load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, FORTRESS)
        combat = LocalCombatState(encounter, player_actor_id=self.session.gameplay_remote_actor_id,
                                  player_health=1000)
        self.session.gameplay_combat_state = combat
        boss = next(m for m in combat.monsters.values() if m.template_id == BOSS)
        combat.retire_monsters(tuple(a for a in combat.monsters if a != boss.actor_id))
        self.move((boss.position[0], boss.position[1], boss.position[2]-8))
        summon = self.spawn()
        for i in range(250):
            combat.advance_player_summons(elapsed_seconds=.1, server_tick=200+i,
                now=summon.created_at+2+i*.1, player_positions={summon.owner_actor_id: boss.position})
        current = combat.player_summons[summon.actor_id]
        self.assertEqual(current.patrol_index, 1)
        self.assertLess(combat._distance(current.position, current.definition.patrol_points[-1]), 1.0)
        self.assertEqual(combat._load_player_skill_geometry(current.definition.attack_skill_index).resource_id,
                         "boss_grimmag_companion_spellweaver_iceball_01")

    def test_duplicate_boss_notification_does_not_schedule_two_injuries(self):
        self.enter()
        self.spawn()
        for _ in range(2):
            self.server._on_map_companion_boss_defeated(self.session, (SimpleNamespace(template_id=BOSS, position=(-10.57, 0, 50.02)),))
        self.assertEqual(len(self.runtime.scheduler.timers), 1)

    def test_death_aura_uses_authored_delay_radius_and_native_corpse_duration(self):
        from dso_server.gameplay.map_companions import load_companion_handoffs
        handoff = next(h for h in load_companion_handoffs(DEFAULT_STATIC_DB_PATH) if h.defeated_id == BOSS)
        self.assertEqual(handoff.companion_id, "a0005_companion_spellweaver_01")
        self.assertEqual((handoff.injury_delay, handoff.radius, handoff.corpse_seconds), (2.5, 11.0, 6.0))
        combat = self.enter()
        summon = self.spawn()
        self.server._on_map_companion_boss_defeated(self.session,
            (SimpleNamespace(template_id=BOSS, position=(-10.57, 0, 50.02)),))
        self.runtime.scheduler.timers[-1].run()
        self.assertIn(summon.actor_id, combat.player_summons)  # outside the explosion

    def test_story_boss_resolution_keeps_other_rows_and_event_gates(self):
        from dso_server.gameplay.boss_encounter_policy import select_boss_spawn_rows
        rows = ((1, b"transform", "a0005_champion_grimmag_01_spawner", 31),
                (2, b"other", "a0005_champion_anderworld_creature", 28))
        result = select_boss_spawn_rows(DEFAULT_STATIC_DB_PATH, FORTRESS, rows)
        self.assertEqual(result, ((1, b"transform", BOSS, 31), rows[1]))
