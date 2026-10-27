"""Party-preserving matchmaking, authored search windows and group queueing."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dso_server.gameplay.pvp_match_state import (
    MatchmakingRegistry,
    MatchmakingRules,
    QueueEntry,
    select_match_teams,
)
from dso_server.gameplay.pvp_objectives import load_battleground_settings
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.pvp_match_protocol import (
    BATTLEGROUND_IDS,
    BATTLEGROUND_MAPS,
    JOIN_BATTLEGROUND_COMMAND,
    JOIN_MATCH_QUEUE_RESPONSE_COMMAND,
    MATCHMAKING_EVENT_ABORTED_BY_GROUP_LEADER,
    MATCHMAKING_EVENT_GROUP_LEVEL_RANGE,
    MATCHMAKING_EVENT_GROUP_TOO_BIG,
    MATCHMAKING_EVENT_JOINED_BY_GROUP_LEADER,
    MATCHMAKING_EVENT_JOINED_BY_USER,
    MATCHMAKING_INFO_COMMAND,
    MATCH_MODE_BEST_OF_3_1V1,
    MATCH_MODE_BEST_OF_5_5V5,
    MATCH_MODE_CAPTURE_THE_FLAG_5V5,
    QUEUE_KIND_GROUP,
    AbortMatchQueue,
    JoinBattlegroundResponse,
    JoinMatchQueue,
    decode_join_match_queue_response,
    decode_matchmaking_info,
    encode_abort_match_queue,
    encode_join_battleground_response,
    encode_join_match_queue,
    honor_level_for,
)
from dso_server.services.base import CharacterService
from dso_server.services.character_server import CharacterServer
from dso_server.tests import test_pvp_matchmaking as fixtures


def _entry(character_id, *, party=0, level=100, honor=0, joined=0.0,
           mode=MATCH_MODE_BEST_OF_5_5V5):
    return QueueEntry(
        character_id, character_id, mode, 1 if party else 0,
        joined_at=joined, party_id=party, level=level, honor_points=honor,
    )


def _team_ids(match, team):
    return sorted(entry.character_id for entry in match.team_members(team))


class SelectionTests(unittest.TestCase):
    def test_parties_are_never_split_and_fill_teams_exactly(self):
        registry = MatchmakingRegistry()
        # A party of three, a party of two, and five solos.
        for cid in (1, 2, 3):
            registry.enqueue(_entry(cid, party=7, joined=1.0))
        for cid in (4, 5):
            registry.enqueue(_entry(cid, party=8, joined=2.0))
        for cid in range(6, 11):
            registry.enqueue(_entry(cid, joined=3.0 + cid))
        match = registry.form_match(MATCH_MODE_BEST_OF_5_5V5, now=20.0)
        self.assertIsNotNone(match)
        teams = [set(_team_ids(match, team)) for team in (0, 1)]
        self.assertEqual(sorted(len(team) for team in teams), [5, 5])
        for party in ({1, 2, 3}, {4, 5}):
            self.assertTrue(any(party <= team for team in teams), party)
        party_ids = {entry.character_id: entry.party_id for entry in match.participants}
        self.assertEqual({party_ids[cid] for cid in (1, 2, 3)}, {7})
        self.assertEqual(registry.waiting_count(MATCH_MODE_BEST_OF_5_5V5), 0)

    def test_two_full_parties_face_each_other(self):
        registry = MatchmakingRegistry()
        for cid in range(1, 6):
            registry.enqueue(_entry(cid, party=1, joined=1.0))
        for cid in range(6, 11):
            registry.enqueue(_entry(cid, party=2, joined=2.0))
        match = registry.form_match(MATCH_MODE_BEST_OF_5_5V5, now=5.0)
        self.assertEqual(_team_ids(match, 0), [1, 2, 3, 4, 5])
        self.assertEqual(_team_ids(match, 1), [6, 7, 8, 9, 10])

    def test_parties_of_four_are_completed_by_solos_on_opposite_teams(self):
        registry = MatchmakingRegistry()
        # Two parties of four cannot share a team of five; each is completed
        # by one solo and the last solo keeps waiting.
        for cid in range(1, 5):
            registry.enqueue(_entry(cid, party=1, joined=1.0))
        for cid in range(5, 9):
            registry.enqueue(_entry(cid, party=2, joined=2.0))
        for cid in range(9, 12):
            registry.enqueue(_entry(cid, joined=float(cid)))
        match = registry.form_match(MATCH_MODE_BEST_OF_5_5V5, now=30.0)
        self.assertIsNotNone(match)
        teams = [set(_team_ids(match, team)) for team in (0, 1)]
        self.assertTrue(any({1, 2, 3, 4} <= team for team in teams))
        self.assertTrue(any({5, 6, 7, 8} <= team for team in teams))
        self.assertEqual(registry.waiting_count(MATCH_MODE_BEST_OF_5_5V5), 1)

    def test_balancing_spreads_levels_across_teams(self):
        parties = tuple(
            (_entry(cid, level=level, joined=float(cid)),)
            for cid, level in enumerate((100, 100, 90, 90, 80, 80, 70, 70, 60, 60), 1)
        )
        teams = select_match_teams(parties, (5, 5), now=100.0)
        sums = [sum(e.effective_level for p in team for e in p) for team in teams]
        self.assertEqual(sums[0], sums[1])

    def test_level_window_holds_until_the_authored_wait_lifts(self):
        rules = MatchmakingRules(level_range=5, pvp_level_range=20, max_wait_seconds=60.0)
        registry = MatchmakingRegistry()
        registry.enqueue(_entry(1, level=100, joined=0.0, mode=MATCH_MODE_BEST_OF_3_1V1))
        registry.enqueue(_entry(2, level=40, joined=1.0, mode=MATCH_MODE_BEST_OF_3_1V1))
        self.assertIsNone(registry.form_match(MATCH_MODE_BEST_OF_3_1V1, rules=rules, now=30.0))
        self.assertEqual(registry.waiting_count(MATCH_MODE_BEST_OF_3_1V1), 2)
        # A close match arrives and is preferred while the window holds.
        registry.enqueue(_entry(3, level=98, joined=2.0, mode=MATCH_MODE_BEST_OF_3_1V1))
        match = registry.form_match(MATCH_MODE_BEST_OF_3_1V1, rules=rules, now=31.0)
        self.assertEqual(sorted(e.character_id for e in match.participants), [1, 3])
        # Alone with an out-of-range opponent, the oldest waits for the lift.
        registry.enqueue(_entry(4, level=10, joined=40.0, mode=MATCH_MODE_BEST_OF_3_1V1))
        self.assertIsNone(registry.form_match(MATCH_MODE_BEST_OF_3_1V1, rules=rules, now=50.0))
        match = registry.form_match(MATCH_MODE_BEST_OF_3_1V1, rules=rules, now=61.0)
        self.assertEqual(sorted(e.character_id for e in match.participants), [2, 4])

    def test_pvp_level_window_uses_honor_ranks(self):
        self.assertEqual(honor_level_for(0), 0)
        self.assertEqual(honor_level_for(400), 1)
        self.assertEqual(honor_level_for(75_000_000), 20)
        rules = MatchmakingRules(level_range=100, pvp_level_range=2, max_wait_seconds=60.0)
        registry = MatchmakingRegistry()
        registry.enqueue(_entry(1, honor=0, joined=0.0, mode=MATCH_MODE_BEST_OF_3_1V1))
        registry.enqueue(_entry(2, honor=1_000_000, joined=1.0, mode=MATCH_MODE_BEST_OF_3_1V1))
        self.assertIsNone(registry.form_match(MATCH_MODE_BEST_OF_3_1V1, rules=rules, now=10.0))
        self.assertIsNotNone(registry.form_match(MATCH_MODE_BEST_OF_3_1V1, rules=rules, now=60.0))

    def test_rules_come_from_the_authored_settings(self):
        for mode, battleground_id in BATTLEGROUND_IDS.items():
            settings = load_battleground_settings(DEFAULT_STATIC_DB_PATH, battleground_id)
            self.assertIsNotNone(settings, battleground_id)
            self.assertEqual(settings.map_name, BATTLEGROUND_MAPS[mode])
        server = fixtures._quiet_server(self)
        rules = CharacterService._matchmaking_rules(server, MATCH_MODE_CAPTURE_THE_FLAG_5V5)
        self.assertEqual(rules, MatchmakingRules(5, 20, 60.0))


class _Store:
    def __init__(self, group):
        self.group = group

    def group_for(self, character_id):
        if self.group is None:
            return None
        if any(m.character_id == character_id for m in self.group.members):
            return self.group
        return None


def _group(owner, members):
    return SimpleNamespace(
        group_id=77, owner_character_id=owner,
        members=[SimpleNamespace(character_id=cid) for cid in members],
    )


class GroupQueueTests(unittest.TestCase):
    def setUp(self):
        self.server = fixtures._quiet_server(self)
        self.sessions = {
            cid: fixtures._session(53000 + cid, cid, f"P{cid}") for cid in range(1, 13)
        }
        patcher = patch.object(
            CharacterServer, "_active_gameplay_sessions",
            lambda _self, **_kw: tuple(self.sessions.values()),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def queue(self, session, mode=MATCH_MODE_BEST_OF_5_5V5, kind=QUEUE_KIND_GROUP):
        payload, bits = encode_join_match_queue(JoinMatchQueue(match_mode=mode, queue_kind=kind))
        session.current_message_bit_length = bits
        CharacterService._handle_join_match_queue(self.server, session, payload)

    def events(self, session):
        return [
            decode_matchmaking_info(data, bits).state
            for data, bits, _label in session.recorder.messages
            if int.from_bytes(data[1:3], "little") == MATCHMAKING_INFO_COMMAND
        ]

    def test_leader_queues_online_members_as_one_party(self):
        self.server.social_store = _Store(_group(1, (1, 2, 3)))
        self.queue(self.sessions[1])
        registry = CharacterService._matchmaking_registry(self.server)
        party = registry.party_members(registry.queued_for(MATCH_MODE_BEST_OF_5_5V5)[0].party_id)
        self.assertEqual(sorted(e.character_id for e in party), [1, 2, 3])
        self.assertEqual(self.events(self.sessions[1]), [MATCHMAKING_EVENT_JOINED_BY_USER])
        for cid in (2, 3):
            self.assertEqual(self.events(self.sessions[cid]), [MATCHMAKING_EVENT_JOINED_BY_GROUP_LEADER])
            self.assertEqual(self.sessions[cid].gameplay_match_queue_entry.party_id, party[0].party_id)
        # Seven solos complete the match; the party shares one team.
        for cid in range(4, 11):
            self.queue(self.sessions[cid], kind=0)
        match = registry.match_for_character(1)
        self.assertIsNotNone(match)
        self.assertEqual(len({match.participant(cid).team for cid in (1, 2, 3)}), 1)
        for session in self.sessions.values():
            if registry.match_for_character(session.gameplay_character.character_id):
                self.assertIsNotNone(session.recorder.find(JOIN_BATTLEGROUND_COMMAND))

    def test_group_too_big_for_a_team_is_refused(self):
        self.server.social_store = _Store(_group(1, (1, 2)))
        self.queue(self.sessions[1], mode=MATCH_MODE_BEST_OF_3_1V1)
        response = decode_join_match_queue_response(
            *self.sessions[1].recorder.find(JOIN_MATCH_QUEUE_RESPONSE_COMMAND)
        )
        self.assertFalse(response.accepted)
        self.assertEqual(self.events(self.sessions[1]), [MATCHMAKING_EVENT_GROUP_TOO_BIG])
        registry = CharacterService._matchmaking_registry(self.server)
        self.assertEqual(registry.waiting_count(MATCH_MODE_BEST_OF_3_1V1), 0)

    def test_group_level_spread_beyond_the_authored_range_is_refused(self):
        self.server.social_store = _Store(_group(1, (1, 2)))
        self.sessions[2].gameplay_character.level = 90
        self.queue(self.sessions[1])
        self.assertEqual(self.events(self.sessions[1]), [MATCHMAKING_EVENT_GROUP_LEVEL_RANGE])
        registry = CharacterService._matchmaking_registry(self.server)
        self.assertEqual(registry.waiting_count(MATCH_MODE_BEST_OF_5_5V5), 0)

    def test_only_the_leader_can_queue_the_group(self):
        self.server.social_store = _Store(_group(1, (1, 2)))
        self.queue(self.sessions[2])
        response = decode_join_match_queue_response(
            *self.sessions[2].recorder.find(JOIN_MATCH_QUEUE_RESPONSE_COMMAND)
        )
        self.assertFalse(response.accepted)

    def test_leader_abort_removes_the_party_member_abort_only_self(self):
        self.server.social_store = _Store(_group(1, (1, 2, 3)))
        self.queue(self.sessions[1])
        registry = CharacterService._matchmaking_registry(self.server)
        payload, bits = encode_abort_match_queue(AbortMatchQueue())
        self.sessions[3].current_message_bit_length = bits
        CharacterService._handle_abort_match_queue(self.server, self.sessions[3], payload)
        self.assertEqual(registry.waiting_count(MATCH_MODE_BEST_OF_5_5V5), 2)
        self.sessions[1].current_message_bit_length = bits
        CharacterService._handle_abort_match_queue(self.server, self.sessions[1], payload)
        self.assertEqual(registry.waiting_count(MATCH_MODE_BEST_OF_5_5V5), 0)
        self.assertIn(MATCHMAKING_EVENT_ABORTED_BY_GROUP_LEADER, self.events(self.sessions[2]))
        self.assertIsNone(self.sessions[2].gameplay_match_queue_entry)

    def test_out_of_range_players_are_matched_when_the_authored_wait_lifts(self):
        armed = []

        class _Timer:
            daemon = False

            def __init__(self, delay, function, args):
                self.delay, self.function, self.args = delay, function, args
                armed.append(self)

            def start(self):
                pass

            def cancel(self):
                pass

        low, high = self.sessions[1], self.sessions[2]
        low.gameplay_character.level = 40
        for session in (low, high):
            session.gameplay_deadline_timer = (
                lambda delay, function, args=None, kwargs=None: _Timer(delay, function, args)
            )
            self.queue(session, mode=MATCH_MODE_BEST_OF_3_1V1, kind=0)
        registry = CharacterService._matchmaking_registry(self.server)
        self.assertEqual(registry.waiting_count(MATCH_MODE_BEST_OF_3_1V1), 2)
        self.assertIsNone(registry.match_for_character(1))
        timer = armed[-1]
        # BestOf3_1v1_01_default authors PlayerRatingMaxWaitTime = 60 s.
        self.assertGreater(timer.delay, 59.0)
        self.assertLessEqual(timer.delay, 60.1)
        for entry in registry.queued_for(MATCH_MODE_BEST_OF_3_1V1):
            entry.joined_at -= 61.0
        timer.function(*timer.args)
        match = registry.match_for_character(1)
        self.assertIsNotNone(match)
        self.assertEqual(sorted(e.character_id for e in match.participants), [1, 2])

    def test_declined_match_requeues_the_rest_of_the_party_together(self):
        self.server.social_store = _Store(_group(1, (1, 2)))
        self.queue(self.sessions[1])
        for cid in range(3, 11):
            self.queue(self.sessions[cid], kind=0)
        registry = CharacterService._matchmaking_registry(self.server)
        match = registry.match_for_character(1)
        party_id = match.participant(1).party_id
        self.assertTrue(party_id)
        payload, bits = encode_join_battleground_response(JoinBattlegroundResponse(accepted=True))
        for cid in (1, 2):
            self.sessions[cid].current_message_bit_length = bits
            CharacterService._handle_join_battleground_response(self.server, self.sessions[cid], payload)
        payload, bits = encode_join_battleground_response(JoinBattlegroundResponse(accepted=False))
        self.sessions[5].current_message_bit_length = bits
        CharacterService._handle_join_battleground_response(self.server, self.sessions[5], payload)
        requeued = {e.character_id: e for e in registry.queued_for(MATCH_MODE_BEST_OF_5_5V5)}
        self.assertEqual(requeued[1].party_id, party_id)
        self.assertEqual(requeued[2].party_id, party_id)
        self.assertNotIn(5, requeued)


if __name__ == "__main__":
    unittest.main()
