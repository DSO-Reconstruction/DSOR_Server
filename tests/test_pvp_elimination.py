"""Multiplayer rounds use every elimination and FFA competitors stay independent."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_player_only_encounter
from dso_server.gameplay.pvp_match_state import MatchPhase, QueueEntry
from dso_server.gameplay.player_skill_timing import PlayerSkillTimingState
from dso_server.protocol.gameplay_protocol import decode_force_transform_batch
from dso_server.protocol.pvp_match_protocol import (
    JOIN_BATTLEGROUND_COMMAND,
    JOIN_MATCH_QUEUE_RESPONSE_COMMAND,
    MATCH_MODE_BEST_OF_3_1V1,
    MATCH_MODE_BEST_OF_5_5V5,
    MATCH_MODE_LAST_MAN_STANDING_8FFA,
    MATCH_TEAM_SIZES,
    PVP_MATCH_INFO_COMMAND,
    PVP_MATCH_OVER_COMMAND,
    JoinBattlegroundResponse,
    JoinMatchQueue,
    decode_join_match_queue_response,
    decode_match_info,
    decode_match_over,
    encode_join_battleground_response,
    encode_join_match_queue,
)
from dso_server.services.base import CharacterService
from dso_server.services.character_server import CharacterServer
from dso_server.tests import test_pvp_matchmaking as fixtures


class MultiplayerEliminationTests(unittest.TestCase):
    def setUp(self):
        self.server = fixtures._quiet_server(self)

    def queue(self, session, mode):
        payload, bits = encode_join_match_queue(JoinMatchQueue(match_mode=mode))
        session.current_message_bit_length = bits
        CharacterService._handle_join_match_queue(self.server, session, payload)

    def arena(self, mode):
        self.sessions = tuple(
            fixtures._session(51000 + index, index, f"Player{index}")
            for index in range(1, sum(MATCH_TEAM_SIZES[mode]) + 1)
        )
        for session in self.sessions:
            self.queue(session, mode)
        self.match = CharacterService._matchmaking_registry(
            self.server
        ).match_for_character(1)
        payload, bits = encode_join_battleground_response(
            JoinBattlegroundResponse(accepted=True, match_mode=mode)
        )
        for session in self.sessions:
            session.current_message_bit_length = bits
            CharacterService._handle_join_battleground_response(
                self.server, session, payload
            )
        combat = LocalCombatState(
            load_local_player_only_encounter(self.server.static_database_path, self.match.map_id),
            player_actor_id=self.sessions[0].gameplay_remote_actor_id,
            player_health=1000,
        )
        for session in self.sessions:
            combat.register_player(session.gameplay_remote_actor_id, 1000)
            session.gameplay_combat_state = combat
            session.gameplay_map = self.match.map_id
            CharacterService._announce_battleground(self.server, session)
            self.assertTrue(CharacterService._bind_battleground_session(self.server, session))
        CharacterService._start_match_round(
            self.server, self.match, self.match.info_generation
        )
        self.assertEqual(self.match.phase, MatchPhase.RUNNING)
        return self.match

    def kill(self, killer, victim):
        victim.gameplay_death_pending = True
        CharacterService._handle_match_event(self.server, killer, ("match_kill", victim))

    def test_five_sequential_deaths_decide_one_team_round_and_duplicates_do_not_score(self):
        match = self.arena(MATCH_MODE_BEST_OF_5_5V5)
        home, away = self.sessions[:5], self.sessions[5:]
        for index, victim in enumerate(away):
            self.kill(home[0], victim)
            if index < 4:
                self.assertEqual(match.phase, MatchPhase.RUNNING)
                self.assertEqual(match.team_rounds_won, [0, 0])
            self.kill(home[0], victim)
        self.assertEqual(match.phase, MatchPhase.INTERMISSION)
        self.assertEqual(match.team_rounds_won, [1, 0])
        self.assertEqual(match.round_index, 1)
        self.assertEqual(match.participant(1).kills, 5)
        self.assertEqual([entry.deaths for entry in match.team_members(1)], [1] * 5)
        self.assertEqual(match.living_teams(), (0,))
        CharacterService._finish_match_round(self.server, match, 1)
        self.assertEqual(match.team_rounds_won, [1, 0])
        self.assertEqual(match.round_index, 1)

    def test_new_round_restores_eliminated_players_and_preserves_match_scores(self):
        match = self.arena(MATCH_MODE_BEST_OF_5_5V5)
        for victim in self.sessions[5:]:
            self.kill(self.sessions[0], victim)
        for session in self.sessions:
            session.gameplay_player_skill_timing = PlayerSkillTimingState(
                cooldowns={"test-arena-skill": 1e30}, cast_ready_at=1e30,
            )
        CharacterService._begin_match_round(self.server, match)
        self.assertFalse(match.round_eliminated)
        self.assertEqual(match.living_teams(), (0, 1))
        self.assertTrue(all(not session.gameplay_death_pending for session in self.sessions))
        CharacterService._start_match_round(self.server, match, match.info_generation)
        for session in self.sessions:
            self.assertFalse(session.gameplay_player_skill_timing.cooldowns)
            self.assertEqual(session.gameplay_player_skill_timing.cast_ready_at, 0.0)
        for victim in self.sessions[:5]:
            self.kill(self.sessions[5], victim)
        self.assertEqual(match.team_rounds_won, [1, 1])
        self.assertEqual(match.round_index, 2)
        self.assertEqual([entry.deaths for entry in match.participants], [1] * 10)
        self.assertEqual(match.participant(6).kills, 5)

    def test_duplicate_death_during_scoreboard_delivery_cannot_award_round_twice(self):
        match = self.arena(MATCH_MODE_BEST_OF_3_1V1)
        publishing, release = Event(), Event()

        def delayed_scoreboard(_server, _match):
            publishing.set()
            if not release.wait(3):
                raise AssertionError("scoreboard delivery did not resume")

        with patch.object(CharacterServer, "_broadcast_match_info", delayed_scoreboard):
            with ThreadPoolExecutor(max_workers=2) as workers:
                first = workers.submit(self.kill, *self.sessions)
                try:
                    self.assertTrue(publishing.wait(3))
                    duplicate = workers.submit(self.kill, *self.sessions)
                    duplicate.result(timeout=2)
                finally:
                    release.set()
                first.result(timeout=2)
        self.assertEqual(match.round_index, 1)
        self.assertEqual(match.team_rounds_won, [1, 0])
        self.assertEqual(match.participant(1).kills, 1)
        self.assertEqual(match.participant(2).deaths, 1)

    def test_teammates_remain_friendly_and_do_not_receive_kill_credit(self):
        match = self.arena(MATCH_MODE_BEST_OF_5_5V5)
        source, teammate, enemy = self.sessions[0], self.sessions[1], self.sessions[5]
        self.assertFalse(CharacterService._battleground_hostility(source, teammate))
        self.assertFalse(CharacterService._pvp_match_opponents(source, teammate))
        self.assertTrue(CharacterService._battleground_hostility(source, enemy))
        self.assertTrue(CharacterService._pvp_match_opponents(source, enemy))
        CharacterService._handle_match_event(self.server, source, ("match_kill", teammate))
        self.assertEqual(match.participant(1).kills, 0)
        self.assertEqual(match.participant(2).deaths, 0)
        self.assertFalse(match.round_eliminated)

    def test_ffa_has_eight_opponents_spawn_slots_and_scoreboard_teams(self):
        match = self.arena(MATCH_MODE_LAST_MAN_STANDING_8FFA)
        self.assertEqual(match.team_count, 8)
        self.assertEqual([entry.team for entry in match.participants], list(range(8)))
        self.assertEqual(match.win_chances(), (0.125,) * 8)
        self.assertEqual(len({transfer[2]["spawn_position"] for transfer in self.server.transfers}), 8)
        arena_positions = []
        for session in self.sessions:
            payload = next((data, bits) for data, bits, label in session.recorder.messages
                           if label == "pvp-arena-transform")
            moved, _ = decode_force_transform_batch(*payload)
            arena_positions.append(moved.position)
            for target in self.sessions:
                self.assertEqual(CharacterService._pvp_match_opponents(session, target),
                                 session is not target)
            info = CharacterService._match_info_for(self.server, session, match)
            self.assertEqual(len(info.teams), 8)
            self.assertEqual(sum(row.enemy for team in info.teams for row in team.players), 7)
        self.assertEqual(len(set(arena_positions)), 8)

    def test_ffa_last_survivor_wins_and_receives_solo_settlement_once(self):
        match = self.arena(MATCH_MODE_LAST_MAN_STANDING_8FFA)
        winner = self.sessions[7]
        for index, victim in enumerate(self.sessions[:7]):
            self.kill(winner, victim)
            if index < 6:
                self.assertEqual(match.phase, MatchPhase.RUNNING)
        self.assertEqual(match.phase, MatchPhase.OVER)
        self.assertEqual(match.winning_team, 7)
        self.assertEqual(match.team_rounds_won, [0] * 7 + [1])
        self.assertEqual(match.living_teams(), (7,))
        CharacterService._finish_match(self.server, match, match.info_generation)
        totals = tuple(session.gameplay_honor_points for session in self.sessions)
        CharacterService._finish_match(self.server, match, match.info_generation)
        self.assertEqual(tuple(session.gameplay_honor_points for session in self.sessions), totals)
        for session in self.sessions:
            result = decode_match_over(*session.recorder.find(PVP_MATCH_OVER_COMMAND))
            self.assertEqual(result.winning_team, 7)
            self.assertEqual(session.gameplay_pvp_solo_matches_played, 1)
            self.assertEqual(getattr(session, "gameplay_pvp_team_matches_played", 0), 0)
            self.assertEqual(session.gameplay_pvp_matches_won, int(session is winner))
            self.assertEqual(session.gameplay_pvp_matches_lost, int(session is not winner))
        self.assertEqual(winner.gameplay_pvp_kills_alone_total, 7)
        winner.recorder.messages.clear()
        CharacterService._send_match_info(self.server, winner, match)
        info = decode_match_info(*winner.recorder.find(PVP_MATCH_INFO_COMMAND))
        self.assertEqual(info.teams[7].players[0].actor_id, 1)
        self.assertEqual(info.teams[7].players[0].kills, 7)
        self.assertFalse(info.teams[7].players[0].enemy)

    def test_timeout_excludes_eliminated_ffa_competitor_with_most_damage(self):
        match = self.arena(MATCH_MODE_LAST_MAN_STANDING_8FFA)
        match.record_damage(1, 10000)
        match.record_damage(8, 100)
        self.kill(self.sessions[7], self.sessions[0])
        CharacterService._expire_match_round(self.server, match, match.info_generation)
        self.assertEqual(match.winning_team, 7)

    def test_timeout_uses_current_round_damage_without_erasing_scoreboard_totals(self):
        match = self.arena(MATCH_MODE_BEST_OF_3_1V1)
        match.record_damage(1, 10000)
        self.kill(self.sessions[0], self.sessions[1])
        CharacterService._begin_match_round(self.server, match)
        CharacterService._start_match_round(self.server, match, match.info_generation)
        match.record_damage(2, 100)
        CharacterService._expire_match_round(self.server, match, match.info_generation)
        self.assertEqual(match.team_rounds_won, [1, 1])
        self.assertEqual(match.participant(1).damage_dealt, 10000)
        self.assertEqual(match.participant(2).damage_dealt, 100)

    def test_leaving_last_living_teammate_closes_round_even_with_dead_teammates_present(self):
        match = self.arena(MATCH_MODE_BEST_OF_5_5V5)
        for victim in self.sessions[5:9]:
            self.kill(self.sessions[0], victim)
        CharacterService._forfeit_battleground(self.server, match, 10, reason="test disconnect")
        self.assertEqual(match.phase, MatchPhase.INTERMISSION)
        self.assertEqual(match.team_rounds_won, [1, 0])
        self.assertTrue(match.participant(10).left)
        CharacterService._begin_match_round(self.server, match)
        self.assertEqual(match.living_teams(), (0, 1))

    def test_team_deathmatch_win_feeds_the_authored_tdm_achievement_family(self):
        fed = []
        match = self.arena(MATCH_MODE_BEST_OF_5_5V5)
        with patch.object(CharacterServer, "_achievement_feed",
                          lambda _self, s, kind, values, **_k: fed.append(
                              (s.gameplay_character.character_id, kind, tuple(values)))), \
                patch.object(CharacterServer, "_quest_feed", lambda *a, **k: None), \
                patch.object(CharacterServer, "_daily_challenge_feed", lambda *a, **k: None), \
                patch.object(CharacterServer, "_request_account_save", lambda *a, **k: None):
            for _round in range(3):
                for victim in self.sessions[5:]:
                    self.kill(self.sessions[0], victim)
                if match.phase != MatchPhase.OVER:
                    CharacterService._begin_match_round(self.server, match)
                    CharacterService._start_match_round(self.server, match, match.info_generation)
            self.assertEqual(match.phase, MatchPhase.OVER)
            CharacterService._finish_match(self.server, match, match.info_generation)
        # pvp_win_tdm_01 ("Win 'Team Death Match'") names BestOf5_3v3: the
        # live 5v5 shares its map, ruleset and WinningCondition.
        self.assertEqual(sorted(cid for cid, _kind, _values in fed), [1, 2, 3, 4, 5])
        self.assertTrue(all("BestOf5_3v3" in values for _cid, _kind, values in fed))

    def test_unsupported_modes_are_rejected_without_an_invitation_or_queue_entry(self):
        # Both objective modes are playable now (test_pvp_capture_the_flag,
        # test_pvp_fortress_match); the wire's "unset" mode never is.
        for mode in (-1,):
            with self.subTest(mode=mode):
                session = fixtures._session(52000 + mode, 100 + mode, "ObjectivePlayer")
                self.queue(session, mode)
                response = decode_join_match_queue_response(
                    *session.recorder.find(JOIN_MATCH_QUEUE_RESPONSE_COMMAND)
                )
                self.assertFalse(response.accepted)
                self.assertIsNone(session.recorder.find(JOIN_BATTLEGROUND_COMMAND))
                registry = CharacterService._matchmaking_registry(self.server)
                self.assertEqual(registry.waiting_count(mode), 0)
                # Even an older queued roster cannot start an objective match.
                for index in range(10):
                    registry.enqueue(QueueEntry(index + 1000, None, mode, 0))
                self.assertIsNone(registry.form_match(mode))


if __name__ == "__main__":
    unittest.main()
