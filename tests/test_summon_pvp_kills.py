"""Owned minions use player PvP death rules and award the owner arena kills."""

import unittest
from unittest.mock import patch
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence.readonly_db import connect

from dso_server.protocol.combat_protocol import KillServerCommand
from dso_server.protocol.pvp_match_protocol import MATCH_MODE_BEST_OF_3_1V1
from dso_server.gameplay.pvp_match_state import MatchParticipant, MatchPhase, PVPMatch
from dso_server.services.base import CharacterService
from dso_server.tests import test_pvp_player_effects as fixtures


class SummonPVPKillTests(unittest.TestCase):
    setUp = fixtures.PVPPlayerEffectTests.setUp
    move = fixtures.PVPPlayerEffectTests.move
    commands = fixtures.PVPPlayerEffectTests.commands

    def spawn_attack(self, skill_resource="packofwolves"):
        with connect(DEFAULT_STATIC_DB_PATH) as database:
            skill_index = int(database.execute(
                "SELECT rowid - 1 FROM _Template_Skill WHERE Id = ?",
                (skill_resource,),
            ).fetchone()[0])
        owner = self.source.gameplay_remote_actor_id
        target = self.target.gameplay_remote_actor_id
        definition = self.combat.player_summon_definition(skill_index)
        spawn = self.combat.spawn_player_summon(
            definition, owner_actor_id=owner, position=(25.0, 0.0, 53.0),
            yaw=0.0, level=100, server_tick=100, now=100.0,
            player_damage_range=(10000.0, 10000.0),
        )
        summon_position = spawn.actors[0].position
        self.move(self.source, summon_position)
        self.move(self.target, (summon_position[0] + 0.5,
                                summon_position[1], summon_position[2]))
        self.targets = tuple((s.gameplay_remote_actor_id,
                              s.gameplay_last_move.relative_position, s)
                             for s in (self.source, self.target))
        self.positions = {actor: self.frame.wire_to_world(position)
                          for actor, position, _ in self.targets}
        self.combat.player_health[target][0] = 20
        attacks = self.combat.maybe_start_player_summon_attacks(
            self.positions, now=max(101.0, spawn.actors[0].attack_ready_at),
            server_tick=126,
            hostile_player_actor_ids_by_owner={owner: frozenset({target})},
        )
        self.assertTrue(attacks)
        self.due_at = max(p.attack.due_at
                          for p in self.combat.pending_player_summon_attacks.values()) + 0.01
        return spawn

    def resolve(self):
        context = CharacterService._prepare_player_summon_cycle_context(
            self.combat, self.targets, now=self.due_at,
        )
        with self.combat.lock:
            result = CharacterService._resolve_player_summon_cycle(
                self.combat, self.targets, self.positions, now=self.due_at,
                server_tick=200, hostile_player_actor_ids_by_owner=context[0],
                player_damage_multipliers=context[1],
                lethal_player_actor_ids_by_owner=context[2],
            )
        return result[3]

    def dispatch(self, resolutions):
        CharacterService._dispatch_pvp_summon_resolutions(
            self.combat, {s.gameplay_remote_actor_id: s for s in (self.source, self.target)},
            resolutions,
        )

    def arena(self, phase=MatchPhase.RUNNING):
        players = tuple(MatchParticipant(
            character_id=s.gameplay_character.character_id, account_id=s.account_id,
            name=s.gameplay_character.name, level=100, class_id=1, team=team,
            session=s, accepted=True, arrived=True, actor_id=s.gameplay_remote_actor_id,
        ) for team, s in enumerate((self.source, self.target)))
        match = PVPMatch(MATCH_MODE_BEST_OF_3_1V1, players)
        match.phase = phase
        for team, s in enumerate((self.source, self.target)):
            s.gameplay_match = match
            s.gameplay_match_team = team
            s.gameplay_respawn_allowed = False
            s.gameplay_match_callback = lambda owner, event: CharacterService._handle_match_event(
                self.server, owner, event,
            )
        return match

    def test_wolves_can_kill_and_send_death_to_both_players(self):
        spawn = self.spawn_attack()
        resolutions = self.resolve()
        self.assertEqual(len(resolutions), 1)  # The next wolf cannot kill the same body again.
        resolution = resolutions[0]
        self.assertTrue(resolution.killed)
        self.assertEqual(resolution.damage, 20)
        self.assertEqual(resolution.current_health, 0)
        self.assertIn(resolution.attacker_actor_id, {a.actor_id for a in spawn.actors})
        self.assertEqual(resolution.source_actor_id, self.source.gameplay_remote_actor_id)
        with patch.object(CharacterService, "_schedule_pvp_status_damage") as dot:
            self.dispatch(resolutions)
        dot.assert_not_called()
        for session, victim in ((self.source, self.target.gameplay_remote_actor_id),
                                 (self.target, 1)):
            kill = next(c for c in self.commands(session) if isinstance(c, KillServerCommand))
            self.assertEqual(kill.actor_id, victim)
            for actual, expected in zip(kill.value.position, self.positions[self.target.gameplay_remote_actor_id]):
                self.assertAlmostEqual(actual, expected, places=4)
        self.assertTrue(self.target.gameplay_death_pending)

    def test_turret_kill_credits_owner_and_ends_arena_round_once(self):
        match = self.arena()
        self.spawn_attack(skill_resource="TurretCombat")
        resolutions = self.resolve()
        self.assertTrue(resolutions[0].killed)
        self.dispatch(resolutions)
        self.assertEqual(match.participant(1).kills, 1)
        self.assertEqual(match.participant(1).damage_dealt, 20)
        self.assertEqual(match.participant(2).deaths, 1)
        self.assertEqual(match.team_rounds_won, [1, 0])
        self.assertEqual(match.phase, MatchPhase.INTERMISSION)
        self.dispatch(resolutions)
        self.assertEqual(match.participant(1).kills, 1)
        self.assertEqual(match.participant(1).damage_dealt, 20)

    def test_nonlethal_map_keeps_one_health_floor(self):
        self.target.gameplay_respawn_allowed = False
        self.spawn_attack()
        resolutions = self.resolve()
        self.assertEqual(self.combat.player_health[self.target.gameplay_remote_actor_id][0], 1)
        self.assertFalse(any(r.killed for r in resolutions))

    def test_countdown_blocks_an_already_queued_minion_attack(self):
        match = self.arena()
        self.spawn_attack()
        match.phase = MatchPhase.COUNTDOWN
        self.assertFalse(self.resolve())
        self.assertEqual(self.combat.player_health[self.target.gameplay_remote_actor_id][0], 20)

    def test_damage_immunity_blocks_queued_minion_damage(self):
        self.spawn_attack()
        with patch.object(CharacterService, "_player_victim_feature_locked",
                          side_effect=lambda s, *_: s is self.target):
            self.assertFalse(self.resolve())
        self.assertEqual(self.combat.player_health[self.target.gameplay_remote_actor_id][0], 20)

    def test_friendly_players_are_not_damaged(self):
        self.spawn_attack()
        with patch.object(CharacterService, "_pvp_target_is_hostile", return_value=False):
            self.assertFalse(self.resolve())
        self.assertEqual(self.combat.player_health[self.target.gameplay_remote_actor_id][0], 20)

    def test_already_dead_target_is_not_hit_or_resurrected(self):
        self.spawn_attack()
        self.combat.player_health[self.target.gameplay_remote_actor_id][0] = 0
        self.assertFalse(self.resolve())
        self.assertEqual(self.combat.player_health[self.target.gameplay_remote_actor_id][0], 0)


if __name__ == "__main__":
    unittest.main()
