"""Objective-mode wire codecs and their use in a capture-the-flag match."""

from __future__ import annotations

import sqlite3
import unittest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.gameplay.pvp_objectives import FLAG_AT_BASE, FLAG_DROPPED
from dso_server.protocol.gameplay_protocol import (
    Message,
    MessageArgument,
    decode_message_batch,
    encode_message_batch,
)
from dso_server.protocol.player_init_protocol import MESSAGE_COMMAND
from dso_server.protocol.pvp_match_protocol import (
    AUTHORED_BATTLEGROUND_MAPS,
    BATTLEGROUND_MAP_RULESETS,
    CTF_MESSAGE_ENEMY_FLAG_RESET,
    CTF_MESSAGE_ENEMY_SCORED,
    CTF_MESSAGE_ENEMY_TOOK_FLAG,
    CTF_MESSAGE_TEAM_FLAG_RESET,
    CTF_MESSAGE_TEAM_SCORED,
    CTF_MESSAGE_WE_TOOK_FLAG_SELF,
    CTF_MESSAGE_WE_TOOK_FLAG_TEAMMATE,
    PVP_EVENT_COMMAND,
    PVP_OBJECTIVE_COMMANDS,
    PVP_STATE_BLUE_FLAG_CARRIED,
    PVP_STATE_BLUE_FLAG_DROPPED,
    PVP_STATE_COMMAND,
    PVP_STATE_FLAGS_AT_BASE,
    PVP_STATE_NONE,
    RULESET_VALUES,
    MatchInfo,
    MatchPlayer,
    MatchTeam,
    PVPEvent,
    PVPState,
    StageInfo,
    battleground_ruleset,
    decode_match_info,
    decode_pvp_event,
    decode_pvp_state,
    decode_stage_info,
    encode_match_info,
    encode_pvp_event,
    encode_pvp_state,
    encode_stage_info,
)
from dso_server.services.base import CharacterService
from dso_server.tests import test_pvp_capture_the_flag as ctf

BLUE, RED = ctf.BLUE, ctf.RED


class ObjectiveCodecTests(unittest.TestCase):
    def test_pvp_event_is_264_bits_of_body_and_round_trips(self):
        payload, bits = encode_pvp_event(PVPEvent())
        self.assertEqual(bits, 8 + 16 + 264)
        self.assertEqual(decode_pvp_event(payload, bits), PVPEvent(2, 0, 0, 0, 0, -1, 0, 0, 0))
        self.assertIn(PVP_EVENT_COMMAND, PVP_OBJECTIVE_COMMANDS)
        # The client folds unknown types above 9 to -1.
        raw = bytearray(payload)
        raw[3] = 12
        self.assertEqual(decode_pvp_event(bytes(raw), bits).event_type, -1)

    def test_pvp_state_and_stage_info_round_trip(self):
        state = PVPState(PVP_STATE_BLUE_FLAG_CARRIED, (1, 0x10007))
        payload, bits = encode_pvp_state(state)
        self.assertEqual(int.from_bytes(payload[1:3], "little"), PVP_STATE_COMMAND)
        self.assertEqual(bits, 8 + 16 + 8 + 32 + 64 + 8)
        self.assertEqual(decode_pvp_state(payload, bits), state)
        stage = StageInfo((("pvp_a0209_drawbridge_trigger", 0.5),))
        self.assertEqual(decode_stage_info(*encode_stage_info(stage)), stage)

    def test_scoreboard_achievement_dictionary_round_trips(self):
        info = MatchInfo(1, 2, (MatchTeam(0, 3, 0, 0.5, (MatchPlayer(
            actor_id=1, name="P", stats=(("achievement_score", 75, 3, ""),),
        ),)),), "CaptureTheFlag_5v5_01", 0)
        self.assertEqual(decode_match_info(*encode_match_info(info)), info)

    def test_message_actor_argument_round_trips(self):
        value = Message(CTF_MESSAGE_TEAM_SCORED, (MessageArgument(3, 0x10002),))
        decoded, actor = decode_message_batch(*encode_message_batch(value, actor_id=0))
        self.assertEqual((decoded, actor), (value, 0))

    def test_map_rulesets_follow_the_battleground_settings(self):
        self.assertEqual(battleground_ruleset("b1102_dm"), 14)
        self.assertEqual(battleground_ruleset("c2103_ctf"), 8)
        self.assertEqual(battleground_ruleset("f4103_atf"), 13)
        self.assertEqual(battleground_ruleset("a0206_8ffa_lms"), 9)
        with sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH.as_posix()}?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT MapName, Ruleset FROM _Template_BattlegroundSettings"
            ).fetchall()
        for map_name, ruleset in rows:
            self.assertEqual(
                BATTLEGROUND_MAP_RULESETS[map_name], RULESET_VALUES[ruleset.casefold()],
                map_name,
            )
        self.assertEqual(set(BATTLEGROUND_MAP_RULESETS), set(AUTHORED_BATTLEGROUND_MAPS))


class ObjectivePublicationTests(unittest.TestCase):
    """The full CTF match fixture, with HUD and message assertions."""

    setUp = ctf.CaptureTheFlagMatchTests.setUp
    team = ctf.CaptureTheFlagMatchTests.team
    move = ctf.CaptureTheFlagMatchTests.move
    kill = ctf.CaptureTheFlagMatchTests.kill
    capture = ctf.CaptureTheFlagMatchTests.capture

    def states(self, session):
        return [
            decode_pvp_state(data, bits)
            for data, bits, _label in session.recorder.messages
            if int.from_bytes(data[1:3], "little") == PVP_STATE_COMMAND
        ]

    def messages(self, session):
        return [
            decode_message_batch(data, bits)[0]
            for data, bits, label in session.recorder.messages
            if label == "pvp-objective-message"
        ]

    def test_round_start_shows_both_flags_at_base(self):
        for session in self.sessions:
            self.assertEqual(self.states(session)[-1], PVPState(PVP_STATE_FLAGS_AT_BASE, ()))

    def test_hud_state_and_messages_follow_the_viewer(self):
        carrier, teammate = self.team(RED)[:2]
        defender = self.team(BLUE)[0]
        for session in self.sessions:
            session.recorder.messages.clear()
        self.move(carrier, self.layout.home(BLUE))
        carrier_actor = carrier.gameplay_remote_actor_id
        self.assertEqual(self.states(carrier)[-1], PVPState(PVP_STATE_BLUE_FLAG_CARRIED, (1,)))
        self.assertEqual(self.states(defender)[-1], PVPState(PVP_STATE_BLUE_FLAG_CARRIED, (carrier_actor,)))
        self.assertEqual(self.messages(carrier)[-1].message_type, CTF_MESSAGE_WE_TOOK_FLAG_SELF)
        self.assertEqual(self.messages(teammate)[-1].message_type, CTF_MESSAGE_WE_TOOK_FLAG_TEAMMATE)
        self.assertEqual(self.messages(defender)[-1], Message(
            CTF_MESSAGE_ENEMY_TOOK_FLAG, (MessageArgument(3, carrier_actor),),
        ))
        self.move(carrier, self.layout.home(RED))
        self.assertEqual(self.messages(carrier)[-1], Message(
            CTF_MESSAGE_TEAM_SCORED, (MessageArgument(3, 1),),
        ))
        self.assertEqual(self.messages(defender)[-1].message_type, CTF_MESSAGE_ENEMY_SCORED)
        self.assertEqual(self.states(defender)[-1].state, PVP_STATE_FLAGS_AT_BASE)

    def test_client_base_signal_captures_only_with_a_flag_in_the_own_base(self):
        carrier = self.team(RED)[0]
        payload, bits = encode_pvp_event(PVPEvent())
        carrier.current_message_bit_length = bits
        # No flag yet: the signal does nothing.
        self.move(carrier, self.layout.home(RED))
        CharacterService._handle_pvp_objective_command(self.server, carrier, payload, PVP_EVENT_COMMAND)
        self.assertEqual(self.match.objective.captures, [0, 0])
        self.move(carrier, self.layout.home(BLUE))
        # Standing in the enemy base with their flag scores nothing either.
        CharacterService._handle_pvp_objective_command(self.server, carrier, payload, PVP_EVENT_COMMAND)
        self.assertEqual(self.match.objective.captures, [0, 0])
        # The signal from the own base completes the capture even when the
        # last move was not itself evaluated.
        from dso_server.runtime.context import Move
        carrier.gameplay_last_move = Move.from_values(
            self.encounter.coordinate_frame.world_to_wire(self.layout.home(RED)), 0.0, 0.0, 0.0, 0, 0,
        )
        CharacterService._handle_pvp_objective_command(self.server, carrier, payload, PVP_EVENT_COMMAND)
        self.assertEqual(self.match.objective.captures, [0, 1])

    def test_dropped_flag_resets_after_the_local_delay(self):
        carrier = self.team(RED)[0]
        self.move(carrier, self.layout.home(BLUE))
        self.move(carrier, (0.0, 0.0, 0.0))
        self.kill(self.team(BLUE)[0], carrier)
        self.assertEqual(self.flags[BLUE].status, FLAG_DROPPED)
        self.assertEqual(self.states(self.team(BLUE)[0])[-1].state, PVP_STATE_BLUE_FLAG_DROPPED)
        delay, callback = self.armed[f"flag-reset-{BLUE}"]
        self.assertEqual(delay, 30.0)
        dropped_item = self.flags[BLUE].item_id
        for session in self.sessions:
            session.recorder.messages.clear()
        callback(self.server, self.match, self.match.info_generation)
        self.assertEqual(self.flags[BLUE].status, FLAG_AT_BASE)
        for session in self.sessions:
            self.assertNotIn(dropped_item, session.gameplay_world_loot)
        self.assertEqual(self.messages(self.team(BLUE)[1])[-1].message_type, CTF_MESSAGE_TEAM_FLAG_RESET)
        self.assertEqual(self.messages(self.team(RED)[1])[-1].message_type, CTF_MESSAGE_ENEMY_FLAG_RESET)
        # A stale timer from an earlier drop cannot reset a newer one.
        respawned = self.team(RED)[1]
        self.move(respawned, self.layout.home(BLUE))
        self.move(respawned, (2.0, 0.0, 2.0))
        self.kill(self.team(BLUE)[1], respawned)
        self.assertEqual(self.flags[BLUE].status, FLAG_DROPPED)
        callback(self.server, self.match, self.match.info_generation)
        self.assertEqual(self.flags[BLUE].status, FLAG_DROPPED)

    def test_match_end_clears_the_hud_line(self):
        for _ in range(3):
            self.capture(RED)
        CharacterService._finish_match(self.server, self.match, self.match.info_generation)
        for session in self.sessions:
            self.assertEqual(self.states(session)[-1], PVPState(PVP_STATE_NONE, ()))

    def test_message_command_id_is_the_shared_message_command(self):
        self.capture(BLUE)
        data, _bits, _label = next(
            m for m in self.team(BLUE)[0].recorder.messages if m[2] == "pvp-objective-message"
        )
        self.assertEqual(int.from_bytes(data[1:3], "little"), MESSAGE_COMMAND)


if __name__ == "__main__":
    unittest.main()
