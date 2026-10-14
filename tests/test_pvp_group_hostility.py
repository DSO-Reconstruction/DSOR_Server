"""Social groups must not grant friendly-fire protection inside the arena.

Two grouped friends who queued into a 1v1 could not damage each other: the
presence hostility check applied the open-world group immunity inside the
match, so the round could never be decided. The match roster now decides.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dso_server.gameplay.pvp_match_state import MatchPhase
from dso_server.persistence.social_state import SocialStateStore, make_social_member
from dso_server.protocol.pvp_match_protocol import (
    JoinBattlegroundResponse,
    encode_join_battleground_response,
)
from dso_server.services.base import CharacterService
from dso_server.tests.test_pvp_matchmaking import (
    JOIN_MATCH_QUEUE_1V1,
    _quiet_server,
    _session,
)


class GroupedDuelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _quiet_server(self)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.server.social_store = SocialStateStore(
            Path(directory.name) / "social.json"
        )
        self.home = _session(50021, 1, "Home")
        self.away = _session(50022, 2, "Away")
        # Both duelists share one open-world group.
        group = self.server.social_store.create_group(
            "Duelists", "", "a0200_kingscity",
            make_social_member(1, "Home", 1, 100),
        )
        self.server.social_store.add_group_member(
            group.group_id, 1, make_social_member(2, "Away", 1, 100)
        )
        shared_world = object()
        for session in (self.home, self.away):
            session.gameplay_pvp_enabled = True
            session.gameplay_combat_state = shared_world

    def _form_match(self) -> None:
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
        self.assertIsNotNone(self.match)
        self.match.phase = MatchPhase.RUNNING

    def _hostile(self, source, target) -> bool:
        return CharacterService._presence_pvp_hostility(
            self.server, source, target
        )

    def test_grouped_players_are_protected_in_the_open_world(self):
        self.assertFalse(self._hostile(self.home, self.away))
        self.assertFalse(self._hostile(self.away, self.home))

    def test_grouped_duelists_are_opponents_inside_the_arena(self):
        self._form_match()
        self.assertIsNotNone(self.home.gameplay_match)
        self.assertNotEqual(
            self.home.gameplay_match_team, self.away.gameplay_match_team
        )
        self.assertTrue(self._hostile(self.home, self.away))
        self.assertTrue(self._hostile(self.away, self.home))

    def test_arena_team_mates_stay_friendly_even_when_not_grouped(self):
        self._form_match()
        # Dissolve the social group; team assignment alone must protect.
        self.server.social_store = SocialStateStore(
            Path(tempfile.mkdtemp()) / "social.json"
        )
        self.away.gameplay_match_team = self.home.gameplay_match_team
        self.assertFalse(self._hostile(self.home, self.away))
        self.away.gameplay_match_team = 1 - int(self.home.gameplay_match_team)
        self.assertTrue(self._hostile(self.home, self.away))

    def test_team_falls_back_to_the_roster_when_the_session_has_none(self):
        self._form_match()
        self.home.gameplay_match_team = None
        self.away.gameplay_match_team = None
        self.assertTrue(
            CharacterService._battleground_hostility(self.home, self.away)
        )

    def test_players_in_different_matches_use_the_open_world_rule(self):
        self._form_match()
        self.away.gameplay_match = None
        self.assertIsNone(
            CharacterService._battleground_hostility(self.home, self.away)
        )
        self.assertFalse(self._hostile(self.home, self.away))


if __name__ == "__main__":
    unittest.main()
