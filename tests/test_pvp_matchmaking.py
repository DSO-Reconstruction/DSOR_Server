"""Service-level tests for the PvP matchmaking flow.

These drive the real command dispatch path with the exact request bytes the
retail client sent, and read back what the server put on each socket.
"""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dso_server.services.character_server import CharacterServer
from dso_server.services.base import CharacterService
from dso_server.runtime.context import DEFAULT_GAMEPLAY_MAP
from dso_server.paths import (
    DEFAULT_STATIC_DB_PATH,
)
from dso_server.runtime.transport import Session
from dso_server.persistence.readonly_db import connect

from dso_server.protocol.pvp_match_protocol import (
    JOIN_BATTLEGROUND_COMMAND,
    JOIN_MATCH_QUEUE_INFO_COMMAND,
    JOIN_MATCH_QUEUE_RESPONSE_COMMAND,
    MATCHMAKING_INFO_COMMAND,
    MATCH_MODE_BEST_OF_3_1V1,
    PVP_MATCH_INFO_COMMAND,
    PVP_MATCH_OVER_COMMAND,
    PVP_MATCH_START_COMMAND,
    PVP_MATCH_COUNTDOWN_COMMAND,
    HONOR_POINTS_CHANGED_COMMAND,
    JoinBattlegroundResponse,
    JoinMatchQueue,
    LeavePVP,
    decode_join_battleground,
    decode_match_info,
    decode_match_over,
    decode_match_start,
    encode_join_battleground_response,
    encode_join_match_queue,
    encode_leave_pvp,
)
from dso_server.gameplay.pvp_match_state import MatchPhase
from dso_server.protocol.pvp_protocol import PVP_FLAG_COMMAND
from dso_server.protocol.gameplay_protocol import (
    FORCE_TRANSFORM_COMMAND,
    decode_force_transform_batch,
)


JOIN_MATCH_QUEUE_1V1 = bytes.fromhex("8bce00000000000000")


def _status_effect_id(resource_id):
    with connect(DEFAULT_STATIC_DB_PATH) as database:
        return int(database.execute(
            "SELECT rowid - 1 FROM _Template_StatusEffect WHERE Id = ?",
            (resource_id,),
        ).fetchone()[0])


class _FakeSocket:
    def __init__(self) -> None:
        self.sent = []

    def sendto(self, data: bytes, addr) -> None:
        self.sent.append((data, addr))


def _quiet_server(test):
    """A composed server with no UDP socket, database, or live timers.

    ``__init__`` is skipped so nothing binds a port. Cross-service calls run
    through ``CharacterService``, which resolves to ``CharacterServer`` rather
    than to the instance, so the map transfer and the deadline timers are
    replaced by patching the class.
    """

    server = CharacterServer.__new__(CharacterServer)
    server.transfers = []
    server.static_database_path = DEFAULT_STATIC_DB_PATH

    def _transfer(_self, session, destination_map_id, **kwargs):
        server.transfers.append((session, destination_map_id, kwargs))
        return True

    for target, replacement in (
        ("_begin_map_transfer", _transfer),
        ("_arm_match_timer", lambda *a, **k: None),
    ):
        patcher = patch.object(CharacterServer, target, replacement)
        patcher.start()
        test.addCleanup(patcher.stop)
    return server


class _Recorder:
    """Collects the commands a session was sent, by command id."""

    def __init__(self) -> None:
        self.messages = []

    def __call__(self, payload, *, track=True, label="reliable"):
        data = payload[0] if isinstance(payload, tuple) else payload
        bits = payload[1] if isinstance(payload, tuple) else len(data) * 8
        self.messages.append((data, bits, label))
        return data

    def ids(self):
        return [
            int.from_bytes(data[1:3], "little")
            for data, _bits, _label in self.messages
            if len(data) >= 3
        ]

    def find(self, command_id):
        for data, bits, _label in self.messages:
            if len(data) >= 3 and int.from_bytes(data[1:3], "little") == command_id:
                return data, bits
        return None


def _session(addr_port: int, character_id: int, name: str) -> Session:
    session = Session(("127.0.0.1", addr_port), _FakeSocket())
    session.connected = True
    session.gameplay_mode = True
    session.gameplay_map = "a0200_kingscity"
    session.gameplay_character = SimpleNamespace(
        character_id=character_id,
        name=name,
        level=100,
        class_id=1,
    )
    session.account_id = character_id
    session.current_message_bit_length = len(JOIN_MATCH_QUEUE_1V1) * 8
    session.gameplay_remote_actor_id = 0x10000 + character_id
    recorder = _Recorder()
    session.send_reliable_ordered = recorder
    session.recorder = recorder
    return session


class MatchmakingQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _quiet_server(self)
        self.home = _session(50001, 1, "Home")
        self.away = _session(50002, 2, "Away")

    def _queue(self, session):
        session.current_message_bit_length = 72
        CharacterService._handle_join_match_queue(
            self.server, session, JOIN_MATCH_QUEUE_1V1
        )

    def test_the_recorded_queue_request_is_accepted_and_answered(self):
        self._queue(self.home)
        ids = self.home.recorder.ids()
        self.assertIn(JOIN_MATCH_QUEUE_RESPONSE_COMMAND, ids)
        self.assertIn(MATCHMAKING_INFO_COMMAND, ids)
        self.assertIn(JOIN_MATCH_QUEUE_INFO_COMMAND, ids)
        registry = CharacterService._matchmaking_registry(self.server)
        self.assertEqual(
            registry.waiting_count(MATCH_MODE_BEST_OF_3_1V1), 1
        )

    def test_one_player_alone_is_not_sent_to_a_battleground(self):
        self._queue(self.home)
        self.assertIsNone(
            self.home.recorder.find(JOIN_BATTLEGROUND_COMMAND)
        )

    def test_a_second_player_produces_the_match_found_dialog(self):
        self._queue(self.home)
        self._queue(self.away)
        for session in (self.home, self.away):
            found = session.recorder.find(JOIN_BATTLEGROUND_COMMAND)
            self.assertIsNotNone(found, session.gameplay_character.name)
            invitation = decode_join_battleground(*found)
            self.assertEqual(invitation.map_id, "b1102_dm")
            self.assertEqual(
                invitation.match_mode, MATCH_MODE_BEST_OF_3_1V1
            )
        registry = CharacterService._matchmaking_registry(self.server)
        self.assertEqual(
            registry.waiting_count(MATCH_MODE_BEST_OF_3_1V1), 0
        )

    def test_accepting_on_both_sides_transfers_to_the_arena(self):
        self._queue(self.home)
        self._queue(self.away)
        self._accept(self.home)
        self.assertEqual(self.server.transfers, [])
        self._accept(self.away)
        self.assertEqual(
            [entry[1] for entry in self.server.transfers],
            ["b1102_dm", "b1102_dm"],
        )

    def test_declining_cancels_the_match_and_requeues_the_other_player(self):
        self._queue(self.home)
        self._queue(self.away)
        self._accept(self.home)
        self._accept(self.away, accepted=False)
        self.assertEqual(self.server.transfers, [])
        registry = CharacterService._matchmaking_registry(self.server)
        # The player who accepted goes back into the queue; the one who
        # declined does not.
        self.assertEqual(
            registry.waiting_count(MATCH_MODE_BEST_OF_3_1V1), 1
        )

    def test_a_disconnect_frees_the_queue_slot(self):
        self._queue(self.home)
        CharacterService._release_matchmaking_session(self.server, self.home)
        registry = CharacterService._matchmaking_registry(self.server)
        self.assertEqual(
            registry.waiting_count(MATCH_MODE_BEST_OF_3_1V1), 0
        )

    def _accept(self, session, *, accepted: bool = True):
        payload, bits = encode_join_battleground_response(
            JoinBattlegroundResponse(accepted=accepted)
        )
        session.current_message_bit_length = bits
        CharacterService._handle_join_battleground_response(
            self.server, session, payload
        )


class BattlegroundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _quiet_server(self)
        self.home = _session(50011, 1, "Home")
        self.away = _session(50012, 2, "Away")
        for session in (self.home, self.away):
            session.current_message_bit_length = 72
            CharacterService._handle_join_match_queue(
                self.server, session, JOIN_MATCH_QUEUE_1V1
            )
        payload, bits = encode_join_battleground_response(
            JoinBattlegroundResponse(accepted=True)
        )
        for session in (self.home, self.away):
            session.current_message_bit_length = bits
            CharacterService._handle_join_battleground_response(
                self.server, session, payload
            )
        self.match = CharacterService._matchmaking_registry(
            self.server
        ).match_for_character(1)
        # Stand in for the arena map load: the transfer replaces the socket,
        # so arrival is what binds the roster.
        for session in (self.home, self.away):
            session.gameplay_map = "b1102_dm"
            session.recorder.messages.clear()

    def _arrive(self, session):
        # Mirrors the initialization order: the battleground id goes out
        # ahead of the init batch, and the roster is bound after it.
        CharacterService._announce_battleground(self.server, session)
        return CharacterService._bind_battleground_session(
            self.server, session
        )

    def test_arriving_in_the_arena_sends_the_battleground_id_and_scoreboard(self):
        self.assertTrue(self._arrive(self.home))
        ids = self.home.recorder.ids()
        # SetBattlegroundId precedes the scoreboard, and in the live flow the
        # whole initialization batch as well.
        self.assertIn(0x00CB, ids)
        self.assertLess(ids.index(0x00CB), ids.index(PVP_MATCH_INFO_COMMAND))
        self.assertIn(PVP_MATCH_INFO_COMMAND, ids)
        self.assertTrue(self.home.gameplay_pvp_enabled)
        self.assertEqual(self.home.gameplay_match_team, 0)

    def test_the_scoreboard_uses_the_viewer_actor_table(self):
        self._arrive(self.home)
        self._arrive(self.away)
        info = decode_match_info(
            *self.home.recorder.find(PVP_MATCH_INFO_COMMAND)
        )
        self.assertEqual(info.battleground_id, "BestOf3_1v1_01_default")
        self.assertEqual(len(info.teams), 2)
        home_row = info.teams[0].players[0]
        away_row = info.teams[1].players[0]
        # Our local NewPlayer has actor 1; only peers have remote actor IDs.
        self.assertEqual(home_row.actor_id, 1)
        self.assertEqual(away_row.actor_id, 0x10002)
        self.assertFalse(home_row.enemy)
        self.assertTrue(away_row.enemy)
        self.assertEqual(home_row.name, "Home")

    def test_opponents_are_lethal_to_each_other_only_while_a_round_runs(self):
        self._arrive(self.home)
        self._arrive(self.away)
        self.match.phase = MatchPhase.COUNTDOWN
        self.assertFalse(
            CharacterService._pvp_match_opponents(self.home, self.away)
        )
        self.match.phase = MatchPhase.RUNNING
        self.assertTrue(
            CharacterService._pvp_match_opponents(self.home, self.away)
        )
        # Never against yourself, and never against a teammate.
        self.assertFalse(
            CharacterService._pvp_match_opponents(self.home, self.home)
        )

    def test_the_recorded_scoreline_finishes_the_match_and_pays_honor(self):
        self._arrive(self.home)
        self._arrive(self.away)
        for session in (self.home, self.away):
            session.recorder.messages.clear()
        # Round one to Away, round two to Home, round three to Away.
        self.match.phase = MatchPhase.RUNNING
        self.match.record_damage(2, 18_653_804)
        CharacterService._finish_match_round(self.server, self.match, 1)
        self.match.phase = MatchPhase.RUNNING
        self.match.record_damage(1, 2_106_226)
        CharacterService._finish_match_round(self.server, self.match, 0)
        self.match.phase = MatchPhase.RUNNING
        CharacterService._finish_match_round(self.server, self.match, 1)
        self.assertEqual(self.match.phase, MatchPhase.OVER)
        CharacterService._finish_match(self.server, self.match, 0)

        for session in (self.home, self.away):
            ids = session.recorder.ids()
            self.assertIn(PVP_MATCH_OVER_COMMAND, ids)
            self.assertIn(HONOR_POINTS_CHANGED_COMMAND, ids)
            result = decode_match_over(
                *session.recorder.find(PVP_MATCH_OVER_COMMAND)
            )
            self.assertEqual(result.winning_team, 1)

        self.assertEqual(self.home.gameplay_pvp_matches_lost, 1)
        self.assertEqual(self.away.gameplay_pvp_matches_won, 1)
        self.assertGreater(
            self.away.gameplay_honor_points, self.home.gameplay_honor_points
        )

    def test_honor_is_credited_once_even_if_completion_runs_twice(self):
        self._arrive(self.home)
        self._arrive(self.away)
        self.match.phase = MatchPhase.RUNNING
        CharacterService._finish_match_round(self.server, self.match, 1)
        self.match.phase = MatchPhase.RUNNING
        CharacterService._finish_match_round(self.server, self.match, 1)
        CharacterService._finish_match(self.server, self.match, 0)
        credited = self.away.gameplay_honor_points
        self.assertGreater(credited, 0)
        # The result timer and a leaving participant can both get here.
        CharacterService._finish_match(self.server, self.match, 0)
        self.assertEqual(self.away.gameplay_honor_points, credited)

    def test_a_finished_match_feeds_arena_quests_once(self):
        self._arrive(self.home)
        self._arrive(self.away)
        self.match.phase = MatchPhase.RUNNING
        CharacterService._finish_match_round(self.server, self.match, 1)
        self.match.phase = MatchPhase.RUNNING
        CharacterService._finish_match_round(self.server, self.match, 1)
        with patch.object(CharacterServer, "_quest_feed") as feed:
            CharacterService._finish_match(self.server, self.match, 0)
            CharacterService._finish_match(self.server, self.match, 0)
        battleground = "BestOf3_1v1_01_default"
        self.assertEqual(
            [tuple(c.args) for c in feed.call_args_list],
            [
                (self.home, "battle", battleground),
                (self.away, "battle", battleground),
                (self.away, "victory", battleground),
            ],
        )
        self.assertEqual(self.away.gameplay_pvp_matches_won, 1)

    def test_a_round_that_times_out_goes_to_the_higher_damage_side(self):
        self._arrive(self.home)
        self._arrive(self.away)
        self.match.phase = MatchPhase.RUNNING
        self.match.record_damage(2, 5_000)
        self.match.record_damage(1, 1_000)
        CharacterService._expire_match_round(self.server, self.match, 0)
        self.assertEqual(self.match.team_rounds_won, [0, 1])

    def test_a_disconnect_mid_match_awards_the_last_player_standing(self):
        self._arrive(self.home)
        self._arrive(self.away)
        self.match.phase = MatchPhase.RUNNING
        for session in (self.home, self.away):
            session.recorder.messages.clear()
        # Home drops: no LeavePVP, just the connection going away.
        CharacterService._release_matchmaking_session(self.server, self.home)
        self.assertEqual(self.match.phase, MatchPhase.OVER)
        self.assertEqual(self.match.winning_team, 1)
        result = decode_match_over(
            *self.away.recorder.find(PVP_MATCH_OVER_COMMAND)
        )
        self.assertEqual(result.winning_team, 1)
        self.assertEqual(self.away.gameplay_pvp_matches_won, 1)
        self.assertGreater(self.away.gameplay_honor_points, 0)

    def test_pressing_leave_awards_the_other_side(self):
        self._arrive(self.home)
        self._arrive(self.away)
        self.match.phase = MatchPhase.RUNNING
        for session in (self.home, self.away):
            session.recorder.messages.clear()
        payload, bits = encode_leave_pvp(LeavePVP(leave=True))
        self.home.current_message_bit_length = bits
        CharacterService._handle_leave_pvp(self.server, self.home, payload)
        self.assertEqual(self.match.winning_team, 1)
        self.assertIsNotNone(
            self.away.recorder.find(PVP_MATCH_OVER_COMMAND)
        )
        # The leaver is returned to the hub.
        self.assertEqual(
            [entry[1] for entry in self.server.transfers][-1:],
            [DEFAULT_GAMEPLAY_MAP],
        )

    def test_a_transfer_into_the_arena_is_not_a_forfeit(self):
        # The source socket is retired while the destination is still
        # connecting; that must not end the match before it starts.
        CharacterService._release_matchmaking_session(self.server, self.home)
        self.assertNotEqual(self.match.phase, MatchPhase.OVER)
        self.assertTrue(self._arrive(self.home))
        self.assertTrue(self._arrive(self.away))

    def test_a_departed_player_keeps_a_usable_actor_id_on_the_scoreboard(self):
        self._arrive(self.home)
        self._arrive(self.away)
        self.match.phase = MatchPhase.RUNNING
        CharacterService._release_matchmaking_session(self.server, self.away)
        self.away.recorder.messages.clear()
        self.home.recorder.messages.clear()
        CharacterService._send_match_info(self.server, self.home, self.match)
        info = decode_match_info(
            *self.home.recorder.find(PVP_MATCH_INFO_COMMAND)
        )
        # Actor 0 is not a resolvable actor; the row must still name the
        # player the client already knows.
        for team in info.teams:
            for row in team.players:
                self.assertNotEqual(row.actor_id, 0, row.name)

    def test_the_round_clock_uses_each_client_s_own_tick_base(self):
        # The client counts the round down against its own clock, which the
        # server only learns by echo. A match-relative tick would make the
        # remaining time nonsensical and the HUD timer would never move.
        self.home.gameplay_chase_server_tick = 5_000_000
        self.away.gameplay_chase_server_tick = 9_100_000
        self._arrive(self.home)
        self._arrive(self.away)
        for session in (self.home, self.away):
            session.recorder.messages.clear()
        self.match.phase = MatchPhase.COUNTDOWN
        CharacterService._start_match_round(self.server, self.match, self.match.info_generation)

        for session, expected in (
            (self.home, 5_000_000), (self.away, 9_100_000)
        ):
            start = decode_match_start(
                *session.recorder.find(PVP_MATCH_START_COMMAND)
            )
            self.assertEqual(start.round_start_tick, expected)
            self.assertEqual(start.round_duration_ticks, 3000)
            # The scoreboard has to agree, or the timer jumps every refresh.
            info = decode_match_info(
                *session.recorder.find(PVP_MATCH_INFO_COMMAND)
            )
            self.assertEqual(info.round_start_tick, expected)

    def test_the_round_clock_is_held_steady_for_the_whole_round(self):
        self.home.gameplay_chase_server_tick = 1_000
        self._arrive(self.home)
        self._arrive(self.away)
        self.match.phase = MatchPhase.COUNTDOWN
        CharacterService._start_match_round(self.server, self.match, self.match.info_generation)
        # The client clock keeps advancing while the round runs; the start it
        # was given must not move with it.
        self.home.gameplay_chase_server_tick = 1_500
        self.home.recorder.messages.clear()
        CharacterService._send_match_info(self.server, self.home, self.match)
        info = decode_match_info(
            *self.home.recorder.find(PVP_MATCH_INFO_COMMAND)
        )
        self.assertEqual(info.round_start_tick, 1_000)

    def test_entering_the_arena_forces_and_announces_pvp(self):
        # The server's own hostility check is not enough: the client keeps its
        # own PvP flag and will not let a player attack without it.
        self.home.gameplay_pvp_enabled = False
        self.away.gameplay_pvp_enabled = False
        events = []
        for session in (self.home, self.away):
            session.gameplay_presence_callback = (
                lambda s, event, sink=events: sink.append(event)
            )
        self._arrive(self.home)
        self._arrive(self.away)
        self.assertTrue(self.home.gameplay_pvp_enabled)
        self.assertTrue(self.away.gameplay_pvp_enabled)
        self.assertIn(PVP_FLAG_COMMAND, self.home.recorder.ids())
        self.assertEqual(
            [event[1] for event in events if event[0] == "pvp_flag"],
            [True, True],
        )
        # And the two are hostile to each other once a round is running.
        self.match.phase = MatchPhase.RUNNING
        self.assertTrue(
            CharacterService._pvp_match_opponents(self.home, self.away)
        )

    def test_the_two_sides_stage_at_opposite_ends_of_the_arena(self):
        starts = CharacterService._battleground_team_starts(
            self.server, "b1102_dm"
        )
        self.assertEqual(len(starts), 2)
        self.assertNotEqual(starts[0], starts[1])
        # b1102_dm authors its two entries about 160 units apart.
        self.assertGreater(abs(starts[0][0] - starts[1][0]), 50.0)

    def test_an_arena_with_no_monsters_still_gets_a_combat_world(self):
        # "No monsters" otherwise means "no combat state", which leaves the
        # players unregistered and every cast rejected with
        # "the current map has no local combat encounter".
        from dso_server.gameplay.local_monster_state import (
            load_local_monster_encounter,
            load_local_player_only_encounter,
        )

        self.assertIsNone(
            load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, "b1102_dm"),
            "b1102_dm is expected to spawn nothing",
        )
        encounter = load_local_player_only_encounter(
            DEFAULT_STATIC_DB_PATH, "b1102_dm"
        )
        self.assertEqual(encounter.map_id, "b1102_dm")
        self.assertEqual(encounter.actor_ids, ())
        # The map geometry the combat state needs is still present.
        self.assertIsNotNone(encounter.navigation)
        self.assertIsNotNone(encounter.coordinate_frame)
        # Both players must land on the same object: the shared combat state
        # is selected by encounter identity, so two of them would give the
        # two sides separate combat worlds and no way to touch each other.
        self.assertIs(
            encounter,
            load_local_player_only_encounter(
                DEFAULT_STATIC_DB_PATH, "b1102_dm"
            ),
        )

    def test_the_arena_encounter_is_only_used_for_a_real_match(self):
        # A town also spawns nothing; it must not gain a combat world.
        self.assertTrue(
            CharacterService._entering_battleground(
                self.server, self.home, "b1102_dm"
            )
        )
        self.assertFalse(
            CharacterService._entering_battleground(
                self.server, self.home, DEFAULT_GAMEPLAY_MAP
            )
        )
        stranger = _session(50099, 99, "Stranger")
        self.assertFalse(
            CharacterService._entering_battleground(
                self.server, stranger, "b1102_dm"
            )
        )

    def test_the_arena_is_announced_with_the_battleground_ruleset(self):
        # Retail loaded b1102_dm with ruleset 14, where it loads a hub with 5
        # and an ordinary PvE map with 1. The map template calls the arena
        # ClosedPVE, so the value cannot come from there -- and without it the
        # client never brings its PvP subsystem up, leaving the countdown, its
        # movement lock and the overview inert.
        from dso_server.protocol.pvp_match_protocol import (
            BATTLEGROUND_RULESET,
            battleground_ruleset,
        )

        self.assertEqual(BATTLEGROUND_RULESET, 14)
        self.assertEqual(battleground_ruleset("b1102_dm"), 14)
        self.assertEqual(battleground_ruleset("pvp_tdm_01"), 14)
        self.assertIsNone(battleground_ruleset(DEFAULT_GAMEPLAY_MAP))

    def test_the_arena_announces_a_match_not_just_a_battleground_id(self):
        # Retail batches PVPMatchDescription immediately behind
        # SetBattlegroundId. Without it the manager holds a battleground and
        # no match, and every later PvP command lands on something that is
        # not running one.
        from dso_server.protocol.pvp_match_protocol import (
            PVP_MATCH_DESCRIPTION_COMMAND,
            decode_match_description,
        )

        self._arrive(self.home)
        ids = self.home.recorder.ids()
        self.assertIn(PVP_MATCH_DESCRIPTION_COMMAND, ids)
        self.assertLess(
            ids.index(0x00CB), ids.index(PVP_MATCH_DESCRIPTION_COMMAND)
        )
        description = decode_match_description(
            *self.home.recorder.find(PVP_MATCH_DESCRIPTION_COMMAND)
        )
        self.assertTrue(description.active)

    def test_a_new_round_revives_and_refills_each_participant(self):
        from dso_server.protocol.gameplay_protocol import ACTOR_STATS_UPDATE_COMMAND
        from dso_server.protocol.respawn_protocol import REVIVE_COMMAND

        self._arrive(self.home)
        self._arrive(self.away)
        # The combat world keys players by the character-scoped actor id
        # world initialization registered them under, not by the client's
        # own actor 1. Keying it by 1 is what made the restore a silent
        # no-op in the first place.
        combat = SimpleNamespace(
            lock=__import__("threading").RLock(),
            player_health={0x10001: [0, 4200], 0x10002: [0, 3300]},
            player_resource={0x10001: [17.0, 100.0], 0x10002: [9.0, 80.0]},
        )
        for session in (self.home, self.away):
            session.gameplay_combat_state = combat
            session.gameplay_inventory = SimpleNamespace(health_points=0.0)
            session.recorder.messages.clear()
        self.match.phase = MatchPhase.INTERMISSION
        with patch.object(CharacterServer, "_refresh_player_combat_vitals"):
            CharacterService._begin_match_round(self.server, self.match)
        self.assertEqual(self.match.phase, MatchPhase.COUNTDOWN)

        # Both sides come back to full, server-side and on the wire; the
        # client is dead until it is told otherwise.
        self.assertEqual(combat.player_health[0x10001][0], 4200)
        self.assertEqual(combat.player_health[0x10002][0], 3300)
        for session in (self.home, self.away):
            self.assertIn(
                ACTOR_STATS_UPDATE_COMMAND, session.recorder.ids()
            )
            self.assertIn(REVIVE_COMMAND, session.recorder.ids())
            self.assertFalse(session.gameplay_death_pending)
        self.assertEqual(self.home.gameplay_inventory.health_points, 4200.0)
        self.assertEqual(self.away.gameplay_inventory.health_points, 3300.0)

    def test_countdown_sends_the_recorded_digits_and_rejects_cancelled_ticks(self):
        from dso_server.protocol.player_init_protocol import decode_server_command_batch

        self._arrive(self.home)
        self._arrive(self.away)
        armed = {}
        with patch.object(CharacterServer, "_arm_match_timer",
                          lambda server, match, key, delay, callback:
                          armed.update({key: (delay, callback)})):
            CharacterService._begin_match_round(self.server, self.match)
        generation = self.match.info_generation
        self.assertEqual(armed["countdown"][0], 4.0)
        for seconds, delay in ((3, 1.0), (2, 2.0), (1, 3.0)):
            self.assertEqual(armed[f"countdown-{seconds}"][0], delay)
            for session in (self.home, self.away):
                session.recorder.messages.clear()
            armed[f"countdown-{seconds}"][1](self.server, self.match, generation)
            for session in (self.home, self.away):
                packet, bits = session.recorder.find(0x89)
                self.assertEqual(bits, 144)
                command = decode_server_command_batch(packet, bits)[0]
                self.assertEqual(command.value.message_type, 5)
                self.assertEqual(command.value.arguments[0].uint32_value, seconds)
                self.assertEqual(command.actor_id, 0)
        CharacterService._cancel_match_timers(self.match)
        for session in (self.home, self.away):
            session.recorder.messages.clear()
        armed["countdown-3"][1](self.server, self.match, generation)
        self.assertFalse(self.home.recorder.messages)
        self.assertFalse(self.away.recorder.messages)
        # A queued start from the old countdown cannot start the new one.
        armed["countdown"][1](self.server, self.match, generation)
        self.assertEqual(self.match.phase, MatchPhase.COUNTDOWN)

    def test_setup_locks_end_at_start_and_do_not_apply_outside_matches(self):
        self._arrive(self.home)
        self._arrive(self.away)
        for phase in (MatchPhase.LOADING, MatchPhase.COUNTDOWN,
                      MatchPhase.INTERMISSION, MatchPhase.OVER):
            self.match.phase = phase
            for session in (self.home, self.away):
                self.assertTrue({"movement", "skills", "damage"}.issubset(
                    CharacterService._player_disabled_actor_features(session)))
        self.match.phase = MatchPhase.COUNTDOWN
        CharacterService._start_match_round(
            self.server, self.match, self.match.info_generation)
        from dso_server.protocol.gameplay_protocol import decode_status_effect_batch
        for session in (self.home, self.away):
            packet = next((data, bits) for data, bits, label in reversed(session.recorder.messages)
                          if label == "pvp-round-effects")
            value, actor = decode_status_effect_batch(*packet)
            self.assertEqual(actor, 1)
            reset = next(entry for entry in value.entries if entry.has_details)
            self.assertEqual((reset.effect_id, reset.field_10),
                             (_status_effect_id("pvp_cooldown_reset"), 0))
            self.assertFalse(session.gameplay_runtime_status_effects)
            self.assertFalse(CharacterService._player_disabled_actor_features(session))
            session.gameplay_match = None
            self.assertFalse(CharacterService._player_disabled_actor_features(session))

    def test_old_round_damage_cannot_reappear_after_revive(self):
        self._arrive(self.home)
        self._arrive(self.away)
        self.match.phase = MatchPhase.RUNNING
        old_token = CharacterService._pvp_round_token(self.home)
        self.match.round_index += 1
        combat = SimpleNamespace()
        self.home.gameplay_combat_state = combat
        self.away.gameplay_combat_state = combat
        pending = SimpleNamespace(round_token=old_token, target_session=self.away)
        key = ("pvp-hit", 1)
        self.home.gameplay_pending_pvp_hits[key] = pending
        # These can already be queued when reset cancels their timer handles.
        with patch.object(CharacterServer, "_pvp_target_is_hostile") as hostile:
            CharacterService._resolve_pvp_skill_hit(
                self.home, self.home.gameplay_chase_generation, key)
            CharacterService._tick_pvp_status_damage(
                self.home, self.home.gameplay_chase_generation,
                ("pvp-status-damage", 1), pending)
        hostile.assert_not_called()
        self.assertFalse(self.home.gameplay_pending_pvp_hits)

    def test_new_round_cleanses_survivor_and_corpse_and_notifies_peers(self):
        from dataclasses import replace
        from unittest.mock import Mock
        from dso_server.protocol.gameplay_protocol import decode_status_effect_batch

        self._arrive(self.home)
        self._arrive(self.away)
        combat = SimpleNamespace(
            lock=__import__("threading").RLock(),
            player_health={0x10001: [0, 4200], 0x10002: [50, 3300]},
            player_resource={0x10001: [17.0, 100.0], 0x10002: [0.0, 80.0]},
        )
        callbacks = []
        timers = []
        removed_ids = {}
        for session in (self.home, self.away):
            seed = next(iter(session.gameplay_runtime_status_effects.values()))[0]
            runtime = replace(seed, effect_id=5905, field_04=800001)
            victim = replace(seed, effect_id=5905, field_04=800002,
                             field_20=0x10002 if session is self.home else 0x10001)
            debuff = replace(seed, effect_id=_status_effect_id("debuff_dot_burn"),
                             field_04=800003)
            permanent = replace(seed, effect_id=5906, field_04=800004)
            session.gameplay_runtime_status_effects = {800001: (runtime, None)}
            session.gameplay_victim_status_effect_instances = {(1, 5905): (victim, 1e20)}
            session.gameplay_persistent_status_effects = {800003: debuff, 800004: permanent}
            session.gameplay_timed_status_effect_expiries = {800003: 1e20}
            session.gameplay_combat_state = combat
            session.gameplay_inventory = SimpleNamespace(health_points=0.0)
            session.gameplay_death_pending = session is self.home
            session.gameplay_presence_callback = lambda s, event: callbacks.append((s, event))
            for key in (("pvp-hit", 1), ("pvp-status-damage", 1), ("player-status-tick", 800001)):
                timer = Mock()
                timers.append(timer)
                session.gameplay_projectile_timers[key] = timer
            session.gameplay_pending_pvp_hits[("pvp-hit", 1)] = object()
            removed_ids[session.addr] = {800001, 800002, 800003}
            session.recorder.messages.clear()
        with patch.object(CharacterServer, "_refresh_player_combat_vitals") as refresh:
            CharacterService._begin_match_round(self.server, self.match)
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(combat.player_health[0x10001][0], 4200)
        self.assertEqual(combat.player_health[0x10002][0], 3300)
        self.assertEqual(self.match.phase, MatchPhase.COUNTDOWN)
        for timer in timers:
            timer.cancel.assert_called_once()
        for session in (self.home, self.away):
            self.assertFalse(session.gameplay_death_pending)
            self.assertFalse(session.gameplay_pending_pvp_hits)
            self.assertFalse(session.gameplay_victim_status_effect_instances)
            self.assertEqual(set(session.gameplay_persistent_status_effects), {800004})
            self.assertFalse(session.gameplay_timed_status_effect_expiries)
            packet = next((data, bits) for data, bits, label in session.recorder.messages
                          if label == "pvp-round-cleanse")
            value, actor_id = decode_status_effect_batch(*packet)
            self.assertEqual(actor_id, 1)
            self.assertEqual({entry.field_04 for entry in value.entries}, removed_ids[session.addr])
            self.assertTrue(all(not entry.has_details for entry in value.entries))
            self.assertTrue(any(owner is session and event[0] == "player_revived"
                                for owner, event in callbacks))
            self.assertTrue(any(owner is session and event[0] == "status_effects"
                                and any(not entry.has_details for entry in event[1][0].entries)
                                for owner, event in callbacks))

    def test_each_viewer_resolves_their_result_and_exact_honor_for_either_winner(self):
        self._arrive(self.home)
        self._arrive(self.away)
        for winning_team in (0, 1):
            with self.subTest(winning_team=winning_team):
                self.match.phase = MatchPhase.OVER
                self.match.winning_team = winning_team
                self.match.round_index = 3
                self.match.settled = False
                before = {s.addr: s.gameplay_honor_points for s in (self.home, self.away)}
                before_kd = {s.addr: (s.gameplay_pvp_kills, s.gameplay_pvp_deaths)
                             for s in (self.home, self.away)}
                for entry in self.match.participants:
                    entry.rounds_won = 2 if entry.team == winning_team else 1
                    entry.rounds_lost = 3 - entry.rounds_won
                    entry.kills = entry.rounds_won
                    entry.deaths = entry.rounds_lost
                    self.match.team_rounds_won[entry.team] = entry.rounds_won
                    entry.session.recorder.messages.clear()
                CharacterService._finish_match(self.server, self.match, self.match.info_generation)
                for session in (self.home, self.away):
                    info = decode_match_info(*session.recorder.find(PVP_MATCH_INFO_COMMAND))
                    result = decode_match_over(*session.recorder.find(PVP_MATCH_OVER_COMMAND))
                    own_team, own_row = next((team, row) for team in info.teams
                                             for row in team.players if row.actor_id == 1)
                    self.assertEqual(own_team.team_id, session.gameplay_match_team)
                    self.assertEqual(own_row.class_id,
                                     self.match.participant(session.gameplay_character.character_id).class_id)
                    self.assertEqual(own_team.team_id == result.winning_team,
                                     session.gameplay_match_team == winning_team)
                    self.assertEqual(own_row.honor_total,
                                     session.gameplay_honor_points - before[session.addr])
                    self.assertEqual(session.gameplay_pvp_kills - before_kd[session.addr][0],
                                     own_row.kills)
                    self.assertEqual(session.gameplay_pvp_deaths - before_kd[session.addr][1],
                                     own_row.deaths)
                    for team in info.teams:
                        entry = self.match.team_members(team.team_id)[0]
                        self.assertEqual(team.players[0].honor_total, entry.honor_earned(
                            3, match_won=entry.team == winning_team))
                credited = [s.gameplay_honor_points for s in (self.home, self.away)]
                credited_kd = [(s.gameplay_pvp_kills, s.gameplay_pvp_deaths)
                               for s in (self.home, self.away)]
                CharacterService._finish_match(self.server, self.match, self.match.info_generation)
                self.assertEqual(credited, [s.gameplay_honor_points for s in (self.home, self.away)])
                self.assertEqual(credited_kd, [(s.gameplay_pvp_kills, s.gameplay_pvp_deaths)
                                               for s in (self.home, self.away)])

    def test_the_overview_can_show_the_honor_gained(self):
        self.home.gameplay_honor_points = 22_420_171
        self.away.gameplay_honor_points = 26_754_872
        self._arrive(self.home)
        self._arrive(self.away)
        self.home.recorder.messages.clear()
        CharacterService._send_match_info(self.server, self.home, self.match)
        info = decode_match_info(
            *self.home.recorder.find(PVP_MATCH_INFO_COMMAND)
        )
        # Preserve the captured career metadata independently of match gain.
        self.assertEqual(info.teams[0].players[0].rating, 22_420_171)
        self.assertEqual(info.teams[1].players[0].rating, 26_754_872)

    def test_the_honor_total_is_sent_before_the_result(self):
        self._arrive(self.home)
        self._arrive(self.away)
        for session in (self.home, self.away):
            session.recorder.messages.clear()
        self.match.phase = MatchPhase.RUNNING
        CharacterService._finish_match_round(self.server, self.match, 1)
        self.match.phase = MatchPhase.RUNNING
        CharacterService._finish_match_round(self.server, self.match, 1)
        CharacterService._finish_match(self.server, self.match, 0)
        ids = self.away.recorder.ids()
        self.assertLess(
            ids.index(HONOR_POINTS_CHANGED_COMMAND),
            ids.index(PVP_MATCH_OVER_COMMAND),
        )

    def test_the_countdown_moves_each_side_into_the_arena(self):
        # The team entries are a staging area; the fight happens at the team
        # respawns, and retail bridges the two with a ForceTransform in the
        # countdown batch.
        self._arrive(self.home)
        self._arrive(self.away)
        for session in (self.home, self.away):
            session.recorder.messages.clear()
        CharacterService._begin_match_round(self.server, self.match)

        entries = CharacterService._battleground_team_entries(
            self.server, "b1102_dm"
        )
        for session, team in ((self.home, 0), (self.away, 1)):
            ids = session.recorder.ids()
            self.assertIn(PVP_MATCH_COUNTDOWN_COMMAND, ids)
            self.assertIn(FORCE_TRANSFORM_COMMAND, ids)
            moved, actor_id = decode_force_transform_batch(
                *session.recorder.find(FORCE_TRANSFORM_COMMAND)
            )
            self.assertEqual(actor_id, 1)
            self.assertEqual(moved.position, entries[team].arena_position)
            # And it is not the staging position they arrived on.
            self.assertNotEqual(
                moved.position, entries[team].entry_position
            )
        # The two sides are moved to different places.
        home_move, _ = decode_force_transform_batch(
            *self.home.recorder.find(FORCE_TRANSFORM_COMMAND)
        )
        away_move, _ = decode_force_transform_batch(
            *self.away.recorder.find(FORCE_TRANSFORM_COMMAND)
        )
        self.assertNotEqual(home_move.position, away_move.position)

    def test_the_transfer_sends_each_side_to_its_own_start(self):
        server = _quiet_server(self)
        home = _session(50021, 1, "Home")
        away = _session(50022, 2, "Away")
        for session in (home, away):
            session.current_message_bit_length = 72
            CharacterService._handle_join_match_queue(
                server, session, JOIN_MATCH_QUEUE_1V1
            )
        payload, bits = encode_join_battleground_response(
            JoinBattlegroundResponse(accepted=True)
        )
        for session in (home, away):
            session.current_message_bit_length = bits
            CharacterService._handle_join_battleground_response(
                server, session, payload
            )
        spawns = [entry[2].get("spawn_position") for entry in server.transfers]
        self.assertEqual(len(spawns), 2)
        self.assertTrue(all(spawn is not None for spawn in spawns))
        self.assertNotEqual(spawns[0], spawns[1])

    def test_the_result_reaches_the_arena_even_if_settlement_already_ran(self):
        # A forfeit and the result timer can both arrive; whoever is still in
        # the arena has to be shown the overview either way.
        self._arrive(self.home)
        self._arrive(self.away)
        self.match.phase = MatchPhase.RUNNING
        CharacterService._finish_match_round(self.server, self.match, 1)
        self.match.phase = MatchPhase.RUNNING
        CharacterService._finish_match_round(self.server, self.match, 1)
        CharacterService._finish_match(self.server, self.match, 0)
        credited = self.away.gameplay_honor_points
        self.away.recorder.messages.clear()
        CharacterService._finish_match(self.server, self.match, 0)
        self.assertIsNotNone(
            self.away.recorder.find(PVP_MATCH_OVER_COMMAND)
        )
        self.assertEqual(self.away.gameplay_honor_points, credited)

    def test_the_final_scoreboard_carries_the_match_totals(self):
        self._arrive(self.home)
        self._arrive(self.away)
        self.match.phase = MatchPhase.RUNNING
        self.match.record_damage(1, 2_529_469)
        self.match.record_kill(killer_id=1, victim_id=2)
        info = decode_match_info(
            *self.home.recorder.find(PVP_MATCH_INFO_COMMAND)
        )
        del info
        self.home.recorder.messages.clear()
        CharacterService._send_match_info(self.server, self.home, self.match)
        info = decode_match_info(
            *self.home.recorder.find(PVP_MATCH_INFO_COMMAND)
        )
        self.assertEqual(info.teams[0].players[0].damage_dealt, 2_529_469)
        self.assertEqual(info.teams[0].players[0].kills, 1)
        self.assertEqual(info.teams[1].players[0].deaths, 1)


if __name__ == "__main__":
    unittest.main()
