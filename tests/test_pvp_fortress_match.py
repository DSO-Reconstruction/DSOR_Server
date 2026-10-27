"""Assault the Fortress: full simulated matches on the authored f4103_atf arena."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.gameplay.pvp_match_state import MatchPhase
from dso_server.gameplay.pvp_objectives import FortressState
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.combat_protocol import BulletSkill
from dso_server.protocol.gameplay_protocol import decode_message_batch
from dso_server.protocol.pvp_match_protocol import (
    MATCH_MODE_ASSAULT_THE_FORTRESS_5V5,
    MATCH_TEAM_SIZES,
    PVP_MATCH_INFO_COMMAND,
    PVP_MATCH_OVER_COMMAND,
    PVP_MATCH_START_COMMAND,
    PVP_STATE_CASTLE_RUSH_STAGE_1,
    PVP_STATE_CASTLE_RUSH_STAGE_2,
    PVP_STATE_COMMAND,
    PVP_STATE_NONE,
    STAGE_INFO_COMMAND,
    JoinBattlegroundResponse,
    JoinMatchQueue,
    battleground_ruleset,
    decode_match_info,
    decode_match_over,
    decode_match_start,
    decode_pvp_state,
    decode_stage_info,
    encode_join_battleground_response,
    encode_join_match_queue,
)
from dso_server.runtime.context import Move
from dso_server.services.base import CharacterService
from dso_server.services.character_server import CharacterServer
from dso_server.tests import test_pvp_matchmaking as fixtures

BLUE, RED = 0, 1  # defenders, attackers
MAP = "f4103_atf"


class FortressMatchTests(unittest.TestCase):
    def setUp(self):
        self.server = fixtures._quiet_server(self)
        self.server.sessions = {}
        self.server.gameplay_world_lock = threading.RLock()
        self.achievements = []
        self.armed = {}
        for name, replacement in (
            ("_achievement_feed", lambda _self, s, kind, values, **_k:
                self.achievements.append((s.gameplay_character.character_id, kind, tuple(values)))),
            ("_daily_challenge_feed", lambda *_a, **_k: None),
            ("_quest_feed", lambda *_a, **_k: None),
            ("_request_account_save", lambda *_a, **_k: None),
            ("_quest_on_monsters_killed", lambda *_a, **_k: None),
            ("_achievement_on_monsters_killed", lambda *_a, **_k: None),
            ("_ranking_on_monsters_killed", lambda *_a, **_k: None),
            ("_start_monster_chase", lambda *_a, **_k: False),
            ("_accept_gameplay_state_command", lambda *_a, **_k: 0),
            ("_arm_match_timer", lambda _self, match, key, delay, callback:
                self.armed.__setitem__(key, (delay, callback))),
        ):
            patcher = patch.object(CharacterServer, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        mode = MATCH_MODE_ASSAULT_THE_FORTRESS_5V5
        self.sessions = tuple(
            fixtures._session(55000 + index, index, f"Player{index}")
            for index in range(1, sum(MATCH_TEAM_SIZES[mode]) + 1)
        )
        for session in self.sessions:
            payload, bits = encode_join_match_queue(JoinMatchQueue(match_mode=mode))
            session.current_message_bit_length = bits
            CharacterService._handle_join_match_queue(self.server, session, payload)
        self.match = CharacterService._matchmaking_registry(self.server).match_for_character(1)
        self.assertIsNotNone(self.match)
        self.assertIsInstance(self.match.objective, FortressState)
        payload, bits = encode_join_battleground_response(
            JoinBattlegroundResponse(accepted=True, match_mode=mode)
        )
        for session in self.sessions:
            session.current_message_bit_length = bits
            CharacterService._handle_join_battleground_response(self.server, session, payload)
        self.encounter = load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, MAP)
        self.combat = LocalCombatState(
            self.encounter, player_actor_id=self.sessions[0].gameplay_remote_actor_id,
            player_health=1000, initially_active=False,
        )
        self.match.gameplay_combat_state = self.combat
        for session in self.sessions:
            self.combat.register_player(session.gameplay_remote_actor_id, 1000)
            session.gameplay_map = MAP
            session.gameplay_map_access_group_id = MAP
            session.gameplay_instance_id = f"pvp-match-{self.match.match_id}"
            session.gameplay_initialization_sent = True
            session.gameplay_presence_ready = True
            session.gameplay_combat_state = self.combat
            session.gameplay_monster_encounter = self.encounter
            session.gameplay_coordinate_frame = self.encounter.coordinate_frame
            session.send_reliable_ordered_split = (
                lambda payload, recorder=session.recorder, **kw:
                recorder(payload, label=kw.get("label_prefix", "split"))
            )
            session.gameplay_match_callback = (
                lambda owner, event: CharacterService._handle_match_event(
                    self.server, owner, event,
                )
            )
            self.server.sessions[session.addr] = session
            self.server._attach_map_event_runtime(
                session, map_id=MAP, initial_flags=frozenset(),
                monster_level=None, scaling_difficulty=0, fresh_instance=False,
            )
            CharacterService._announce_battleground(self.server, session)
            self.assertTrue(CharacterService._bind_battleground_session(self.server, session))
        self.runtime = self.sessions[0].gameplay_map_event_runtime
        self.assertTrue(all(s.gameplay_map_event_runtime is self.runtime for s in self.sessions))
        CharacterService._start_match_round(self.server, self.match, self.match.info_generation)
        self.assertEqual(self.match.phase, MatchPhase.RUNNING)

    # -- helpers --------------------------------------------------------

    def team(self, team):
        return [s for s in self.sessions if s.gameplay_match_team == team]

    def monster(self, template):
        for actor_id, monster in self.combat.monsters.items():
            if (monster.template_id == template
                    and self.combat.monster_health.get(actor_id, 0) > 0):
                return actor_id, monster
        return None, None

    def destroy(self, template, by=None):
        attacker = by or self.team(RED)[0]
        actor_id, monster = self.monster(template)
        self.assertIsNotNone(actor_id, template)
        self.assertFalse(monster.invulnerable, template)
        self.combat.monster_health[actor_id] = 0
        self.server._on_monsters_killed(attacker, (monster,))

    def states(self, session):
        return [
            decode_pvp_state(data, bits)
            for data, bits, _label in session.recorder.messages
            if int.from_bytes(data[1:3], "little") == PVP_STATE_COMMAND
        ]

    def stage_infos(self, session):
        return [
            decode_stage_info(data, bits)
            for data, bits, _label in session.recorder.messages
            if int.from_bytes(data[1:3], "little") == STAGE_INFO_COMMAND
        ]

    def clear_stage_zero(self):
        for template in ("pvp_defense_tower_mid_lane_t1A", "pvp_defense_tower_mid_lane_t1B",
                         "pvp_defense_tower_mid_lane_t2", "pvp_defense_tower_mid_lane_t4",
                         "pvp_defense_tower_t5", "pvp_a0209_drawbridge_trigger"):
            self.destroy(template)

    # -- tests ------------------------------------------------------------

    def test_arena_loads_as_castle_rush_with_the_authored_round(self):
        self.assertEqual(battleground_ruleset(MAP), 13)
        start = decode_match_start(*self.sessions[0].recorder.find(PVP_MATCH_START_COMMAND))
        self.assertEqual(start.round_duration_ticks, 420 * 25)
        self.assertIn("pvp_castlerush_initial", self.runtime.running)
        for session in self.sessions:
            self.assertEqual(self.states(session)[-1].state, PVP_STATE_CASTLE_RUSH_STAGE_1)
            self.assertEqual(self.stage_infos(session)[-1].entries,
                             (("pvp_a0209_drawbridge_trigger", 1.0),))

    def test_structures_belong_to_the_defenders(self):
        tower, _ = self.monster("pvp_defense_tower_mid_lane_t1A")
        defender = self.team(BLUE)[0].gameplay_remote_actor_id
        attacker = self.team(RED)[0].gameplay_remote_actor_id
        self.assertEqual(self.combat.monster_team(tower), BLUE)
        self.assertFalse(self.combat.monster_hostile_to_player(tower, defender))
        self.assertTrue(self.combat.monster_hostile_to_player(tower, attacker))
        position = self.combat.monster_positions[tower]
        # A tower picks the attacker even when a defender stands closer.
        targets = self.combat.assign_monster_targets((tower,), (
            (defender, position, {tower}),
            (attacker, (position[0] + 5.0, position[1], position[2]), {tower}),
        ))
        self.assertEqual(targets[tower][0], attacker)
        self.assertEqual(self.combat.assign_monster_targets((tower,), (
            (defender, position, {tower}),
        )), {})

    def test_defenders_cannot_hurt_their_own_structures(self):
        tower, _ = self.monster("pvp_defense_tower_mid_lane_t1A")
        self.combat.activate_monsters((tower,))
        wire = self.encounter.coordinate_frame.world_to_wire(self.combat.monster_positions[tower])
        skill = BulletSkill(0, 1867, 0.0, 2000, 3000, (1.0, 0.0, 0.0, 0.0), 0)
        defender = self.team(BLUE)[0].gameplay_remote_actor_id
        attacker = self.team(RED)[0].gameplay_remote_actor_id
        self.assertEqual(self.combat.start_bullet_skill_hits(
            skill, wire, source_actor_id=defender, now=100.0, eligible_actor_ids=(tower,),
        ), ())
        self.assertEqual(len(self.combat.start_bullet_skill_hits(
            skill, wire, source_actor_id=attacker, now=100.0, eligible_actor_ids=(tower,),
        )), 1)

    def test_structures_look_friendly_to_defenders_and_hostile_to_attackers(self):
        tower, _ = self.monster("pvp_defense_tower_mid_lane_t1A")
        group = self.combat.group_for_actor(tower)
        for session, hostile in ((self.team(BLUE)[0], False), (self.team(RED)[0], True)):
            session.gameplay_new_monster_sent_actor_ids.discard(tower)
            batch = CharacterService._new_monster_batch_for_viewer(
                session, self.combat, group.spawn_batch, label="test",
            )
            monsters = [c.value for c in batch.commands if hasattr(c.value, "placement")
                        and c.value.actor_id == tower]
            self.assertEqual(monsters[0].field_e0, BLUE)
            self.assertEqual(monsters[0].placement.field_08, 2 if hostile else 0)

    def test_stage_targets_are_gated_and_stand_ins_swap_for_real_towers(self):
        bridge, monster = self.monster("pvp_a0209_drawbridge_trigger")
        self.assertTrue(monster.invulnerable)
        gun, gun_monster = self.monster("pvp_a0209_guncontrol")
        self.assertTrue(gun_monster.invulnerable)
        stand_in, monster = self.monster("pvp_defense_tower_mid_lane_t2_invincible")
        self.assertTrue(monster.invulnerable)
        self.assertEqual(self.monster("pvp_defense_tower_mid_lane_t2"), (None, None))
        self.destroy("pvp_defense_tower_mid_lane_t1A")
        self.destroy("pvp_defense_tower_mid_lane_t1B")
        # The authored MonstersEliminated signal started the t2 event: the
        # stand-in is retired and the real tower is up and vulnerable.
        self.assertIn("pvp_castlerush_mid_lane_tower_t2", self.runtime.running)
        self.assertEqual(self.combat.monster_health[stand_in], 0)
        real, monster = self.monster("pvp_defense_tower_mid_lane_t2")
        self.assertIsNotNone(real)
        self.assertFalse(monster.invulnerable)
        self.assertEqual(self.combat.monster_team(real), BLUE)
        self.destroy("pvp_defense_tower_mid_lane_t2")
        self.destroy("pvp_defense_tower_mid_lane_t4")
        _bridge, monster = self.monster("pvp_a0209_drawbridge_trigger")
        self.assertTrue(monster.invulnerable)
        self.destroy("pvp_defense_tower_t5")
        _bridge, monster = self.monster("pvp_a0209_drawbridge_trigger")
        self.assertFalse(monster.invulnerable)
        _gun, monster = self.monster("pvp_a0209_guncontrol")
        self.assertTrue(monster.invulnerable)

    def test_stage_zero_cleared_opens_stage_one_and_extends_the_round(self):
        attacker, defender = self.team(RED)[0], self.team(BLUE)[0]
        before = self.match.objective.respawn(RED)
        for session in self.sessions:
            session.recorder.messages.clear()
        self.clear_stage_zero()
        self.assertEqual(self.match.objective.stage, 1)
        self.assertNotIn("pvp_castlerush_initial", self.runtime.running)
        self.assertIn("pvp_castlerush_stage_1", self.runtime.running)
        self.assertNotEqual(self.match.objective.respawn(RED), before)
        for session in (attacker, defender):
            cleared = [decode_message_batch(d, b)[0] for d, b, label in session.recorder.messages
                       if label == "pvp-fortress-stage-cleared"]
            self.assertEqual([m.message_type for m in cleared], [24])
            self.assertEqual(self.states(session)[-1].state, PVP_STATE_CASTLE_RUSH_STAGE_2)
            self.assertEqual(self.stage_infos(session)[-1].entries,
                             (("pvp_a0209_guncontrol", 1.0),))
        infos = [decode_match_info(d, b) for d, b, _l in attacker.recorder.messages
                 if int.from_bytes(d[1:3], "little") == PVP_MATCH_INFO_COMMAND]
        # PVPMatchInfo carries the 240 s time bonus once, with the longer round.
        self.assertEqual([info.stage for info in infos].count(240 * 25), 1)
        self.assertEqual(infos[-1].round_duration_ticks, (420 + 240) * 25)
        attacker.recorder.messages.clear()
        CharacterService._send_match_info(self.server, attacker, self.match)
        self.assertEqual(decode_match_info(*attacker.recorder.find(PVP_MATCH_INFO_COMMAND)).stage, 0)
        self.assertEqual(self.armed["round"][1], CharacterServer._expire_match_round)
        _gun, monster = self.monster("pvp_a0209_guncontrol")
        self.assertFalse(monster.invulnerable)

    def test_attackers_win_by_destroying_the_gun_controls(self):
        self.clear_stage_zero()
        self.destroy("pvp_a0209_guncontrol")
        self.assertEqual(self.match.phase, MatchPhase.OVER)
        self.assertEqual(self.match.winning_team, RED)
        CharacterService._finish_match(self.server, self.match, self.match.info_generation)
        CharacterService._finish_match(self.server, self.match, self.match.info_generation)
        for session in self.sessions:
            self.assertEqual(decode_match_over(*session.recorder.find(PVP_MATCH_OVER_COMMAND)).winning_team, RED)
            self.assertEqual(self.states(session)[-1].state, PVP_STATE_NONE)
        winners = {s.gameplay_character.character_id for s in self.team(RED)}
        fed = [(cid, values) for cid, kind, values in self.achievements if kind == "pvp_match"]
        self.assertEqual(sorted(cid for cid, _ in fed), sorted(winners))
        self.assertTrue(all("AssaultTheFortress_6v6" in values for _cid, values in fed))
        attacker_entry = self.match.participant(self.team(RED)[0].gameplay_character.character_id)
        self.assertEqual(attacker_entry.objectives_destroyed, 7)

    def test_timeout_is_a_defender_win(self):
        self.destroy("pvp_defense_tower_mid_lane_t1A")
        CharacterService._expire_match_round(self.server, self.match, self.match.info_generation)
        self.assertEqual(self.match.phase, MatchPhase.OVER)
        self.assertEqual(self.match.winning_team, BLUE)

    def test_defenders_cannot_score_on_their_own_structures(self):
        defender = self.team(BLUE)[0]
        actor_id, monster = self.monster("pvp_defense_tower_mid_lane_t1A")
        self.combat.monster_health[actor_id] = 0
        self.server._on_monsters_killed(defender, (monster,))
        self.assertEqual(self.match.objective.destroyed_count, 0)

    def test_structure_damage_is_the_score_column(self):
        attacker = self.team(RED)[0]
        tower, _ = self.monster("pvp_defense_tower_mid_lane_t1A")
        CharacterService._handle_match_event(
            self.server, attacker, ("match_monster_damage", ((tower, 120), (999999, 50))),
        )
        CharacterService._handle_match_event(
            self.server, self.team(BLUE)[0], ("match_monster_damage", ((tower, 80),)),
        )
        attacker.recorder.messages.clear()
        CharacterService._send_match_info(self.server, attacker, self.match)
        info = decode_match_info(*attacker.recorder.find(PVP_MATCH_INFO_COMMAND))
        row = next(r for t in info.teams for r in t.players if r.actor_id == 1)
        self.assertEqual(row.score, 120)
        self.assertEqual([t.rounds_won for t in info.teams], [0, 0])
        self.destroy("pvp_defense_tower_mid_lane_t1A")
        attacker.recorder.messages.clear()
        CharacterService._send_match_info(self.server, attacker, self.match)
        info = decode_match_info(*attacker.recorder.find(PVP_MATCH_INFO_COMMAND))
        self.assertEqual([t.rounds_won for t in info.teams], [0, 1])

    def test_respawn_uses_the_stage_entries(self):
        victim = self.team(RED)[0]
        cid = victim.gameplay_character.character_id
        self.clear_stage_zero()
        victim.gameplay_death_pending = True
        CharacterService._handle_match_event(self.server, self.team(BLUE)[0], ("match_kill", victim))
        delay, callback = self.armed[f"respawn-{cid}"]
        self.assertEqual(delay, 2.0)
        callback(self.server, self.match, self.match.info_generation)
        position, _heading = self.match.objective.respawn(RED)
        self.assertEqual(victim.gameplay_spawn_position, tuple(position))
        self.assertAlmostEqual(position[0], -24.28, places=1)
        # A second death waits longer (TimeoutIncreasePerDeath = 3 s).
        victim.gameplay_death_pending = True
        CharacterService._handle_match_event(self.server, self.team(BLUE)[0], ("match_kill", victim))
        self.assertEqual(self.armed[f"respawn-{cid}"][0], 5.0)

    def test_round_start_moves_each_side_to_its_stage_zero_respawn(self):
        attacker, defender = self.team(RED)[0], self.team(BLUE)[0]
        self.assertAlmostEqual(attacker.gameplay_spawn_position[0], -121.52, places=1)
        self.assertAlmostEqual(defender.gameplay_spawn_position[0], -21.0, places=1)

    def test_attackers_capture_a_control_point_and_its_spawner_joins_them(self):
        attacker, defender = self.team(RED)[0], self.team(BLUE)[0]
        point = next(p for p in self.match.objective.layout.control_points
                     if self.match.objective.control_point_present(p))
        definition = next(
            state.definition for state in self.runtime.props.values()
            if state.definition.template_id == point.template_id
            and abs(state.definition.position[0] - point.position[0]) < 1.0
        )
        frame = self.encounter.coordinate_frame
        for session in (attacker, defender):
            session.gameplay_last_move = Move.from_values(frame.world_to_wire(point.position), 0.0, 0.0, 0.0, 0, 0)
        # Defenders cannot capture the attackers' point.
        self.assertTrue(CharacterService._handle_match_event(
            self.server, defender, ("match_prop_interaction", definition)))
        self.assertNotIn(f"capture-{point.row_id}", self.armed)
        self.assertTrue(CharacterService._handle_match_event(
            self.server, attacker, ("match_prop_interaction", definition)))
        delay, callback = self.armed[f"capture-{point.row_id}"]
        self.assertEqual(delay, 10.0)
        callback(self.server, self.match, self.match.info_generation)
        spawner, monster = self.monster(point.monster_id)
        self.assertIsNotNone(spawner)
        self.assertEqual(self.combat.monster_team(spawner), RED)
        self.assertTrue(self.combat.monster_hostile_to_player(spawner, defender.gameplay_remote_actor_id))
        self.assertFalse(self.combat.monster_hostile_to_player(spawner, attacker.gameplay_remote_actor_id))
        # A captured point with a live spawner cannot be captured again.
        self.armed.clear()
        CharacterService._handle_match_event(self.server, attacker, ("match_prop_interaction", definition))
        self.assertNotIn(f"capture-{point.row_id}", self.armed)

    def test_drawbridge_fall_wipes_stage_zero_towers_and_siege_units(self):
        attacker = self.team(RED)[0]
        # Capture the stage-0 rammer point first, so a spawner stands there.
        point = next(p for p in self.match.objective.layout.control_points
                     if self.match.objective.control_point_present(p))
        definition = next(
            state.definition for state in self.runtime.props.values()
            if state.definition.template_id == point.template_id
            and abs(state.definition.position[0] - point.position[0]) < 1.0
        )
        attacker.gameplay_last_move = Move.from_values(
            self.encounter.coordinate_frame.world_to_wire(point.position), 0.0, 0.0, 0.0, 0, 0)
        CharacterService._handle_match_event(self.server, attacker, ("match_prop_interaction", definition))
        self.armed[f"capture-{point.row_id}"][1](self.server, self.match, self.match.info_generation)
        self.assertIsNotNone(self.monster(point.monster_id)[0])
        left, _ = self.monster("pvp_defense_tower_left_lane_t1")
        top, _ = self.monster("pvp_defense_tower_top_01")
        for session in self.sessions:
            session.gameplay_spawned_monster_actor_ids.add(left)
            session.recorder.messages.clear()
        self.clear_stage_zero()
        self.assertIn("pvp_castlerush_kill_stage_01_tower", self.runtime.running)
        # Inside the aura box: the untouched left lane tower and the spawner.
        self.assertEqual(self.combat.monster_health[left], 0)
        self.assertEqual(self.monster(point.monster_id), (None, None))
        self.assertEqual(self.monster("pvp_defense_tower_left_lane_t2_invincible"), (None, None))
        # Outside it: the far side's towers stay.
        self.assertGreater(self.combat.monster_health[top], 0)
        self.assertTrue(any(label == "pvp-fortress-discard-monster"
                            for _d, _b, label in attacker.recorder.messages))
        # The point's spawner is gone, so the point could be captured again
        # (it is not present any more in stage 1, being an initial-only point).
        self.assertNotIn(point.row_id, self.match.fortress_captures)

    def test_capture_fails_if_the_capturer_walks_away(self):
        attacker = self.team(RED)[0]
        point = next(p for p in self.match.objective.layout.control_points
                     if self.match.objective.control_point_present(p))
        definition = next(
            state.definition for state in self.runtime.props.values()
            if state.definition.template_id == point.template_id
            and abs(state.definition.position[0] - point.position[0]) < 1.0
        )
        frame = self.encounter.coordinate_frame
        attacker.gameplay_last_move = Move.from_values(frame.world_to_wire(point.position), 0.0, 0.0, 0.0, 0, 0)
        CharacterService._handle_match_event(self.server, attacker, ("match_prop_interaction", definition))
        _delay, callback = self.armed[f"capture-{point.row_id}"]
        away = (point.position[0] + 20.0, point.position[1], point.position[2])
        attacker.gameplay_last_move = Move.from_values(frame.world_to_wire(away), 0.0, 0.0, 0.0, 0, 0)
        self.assertFalse(callback(self.server, self.match, self.match.info_generation))
        self.assertEqual(self.monster(point.monster_id), (None, None))

    def test_a_whole_side_leaving_hands_the_win_to_the_other(self):
        for session in self.team(BLUE):
            CharacterService._forfeit_battleground(
                self.server, self.match, session.gameplay_character.character_id,
                reason="test disconnect",
            )
        self.assertEqual(self.match.phase, MatchPhase.OVER)
        self.assertEqual(self.match.winning_team, RED)


class FortressInstanceTests(unittest.TestCase):
    def test_each_match_has_its_own_map_event_runtime(self):
        server = fixtures._quiet_server(self)
        server.sessions = {}
        server.gameplay_world_lock = threading.RLock()
        runtimes = []
        for match_id, port in ((901, 56001), (902, 56002)):
            session = fixtures._session(port, port, f"P{port}")
            session.gameplay_map = MAP
            session.gameplay_map_access_group_id = MAP
            session.gameplay_instance_id = f"pvp-match-{match_id}"
            runtimes.append(server._attach_map_event_runtime(
                session, map_id=MAP, initial_flags=frozenset(),
                monster_level=None, scaling_difficulty=0, fresh_instance=False,
            ))
        self.assertIsNot(runtimes[0], runtimes[1])
        self.assertNotEqual(runtimes[0].instance_key, runtimes[1].instance_key)


class FortressNewMonsterTests(unittest.TestCase):
    def test_new_monster_codec_carries_the_team_field(self):
        encounter = load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, MAP)
        self.assertIsNotNone(encounter)
        templates = {
            command.value.template_id
            for group in encounter.groups
            for command in group.spawn_batch.commands
            if hasattr(command.value, "template_id")
        }
        self.assertIn("pvp_defense_tower_mid_lane_t1A", templates)
        self.assertIn("pvp_a0209_drawbridge_trigger", templates)
        self.assertIn("pvp_defense_tower_t5_invincible", templates)


if __name__ == "__main__":
    unittest.main()
