"""The hub-teleport talent names its destination symbolically as ``Hub``.

The server used to hand that literal to the map loader and reject the travel
because no map database is called ``Hub``. It now resolves to the character's
last visited hub, then the nearest hub, then Kingshill.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dso_server.gameplay.local_map_state import load_local_travel_point_position
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence.account_state import _state_from_document, _state_to_document
from dso_server.protocol.gameplay_protocol import MapTravelRequest
from dso_server.runtime.transport import Session
from dso_server.services.character_server import CharacterServer
from dso_server.tests.fixtures.characters import character_server


class FakeSocket:
    def __init__(self) -> None:
        self.sent = []

    def sendto(self, data, address) -> None:
        self.sent.append((data, address))


HUB_REQUEST = MapTravelRequest(
    difficulty=0, map_id="Hub", entry_index=0, force_entry=False,
    travel_point_id="Talent", mode=None,
)


class HubTravelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = character_server(self)
        self.server.sock = FakeSocket()

    def _session(self, *, current_map: str, last_town: str) -> Session:
        session = Session(("127.0.0.1", 41000), self.server.sock)
        session.connected = True
        session.gameplay_mode = True
        session.gameplay_map = current_map
        session.gameplay_character = SimpleNamespace(
            class_id=1, character_id=0, level=100, name="Traveller"
        )
        session.gameplay_last_visited_town = last_town
        return session

    def test_discovered_maps_survive_travel_and_relogin_without_global_unlocks(self):
        hub, garden = "a0002_start_hub", "a0003_start_garden_dun"
        discovered = set()
        for index, map_id in enumerate((hub, garden, hub)):
            with self.subTest(map_id=map_id, index=index):
                session = Session(("127.0.0.1", 41010 + index), self.server.sock)
                session.gameplay_mode = True
                session.gameplay_map = map_id
                session.gameplay_character = self.server.character_selection.active_character
                self.server._send_local_player_initialization(session)
                discovered.add(map_id)
                self.assertEqual(session.gameplay_unlocked_maps, discovered)
                snapshot = self.server._capture_account_state(session)
                self.assertEqual(set(snapshot.unlocked_maps), discovered)
                saved = self.server.account_store.get(snapshot.character_id)
                self.assertEqual(set(saved.unlocked_maps), discovered)
                restored = _state_from_document(_state_to_document(snapshot))
                self.assertEqual(set(restored.unlocked_maps), discovered)

        # Saves from before map discovery tracking retain their known last
        # location; they cannot infer every map merely from fast-travel access.
        document = _state_to_document(snapshot)
        del document["unlocked_maps"]
        legacy = _state_from_document(document)
        self.assertEqual(legacy.unlocked_maps, ())
        other = Session(("127.0.0.1", 41020), self.server.sock)
        other.gameplay_map = garden
        other.gameplay_map_access_id = garden
        other.gameplay_map_access_group_id = garden
        self.server._restore_map_unlocks(other, legacy)
        self.assertEqual(other.gameplay_unlocked_maps, {hub, garden})

    def test_map_discovery_saves_even_without_a_scripted_event_runtime(self):
        session = self._session(current_map="a0002_start_hub", last_town="")
        saved = []
        session.gameplay_persist_callback = lambda s, reason, flush: saved.append(reason) or True
        self.assertIsNone(session.gameplay_map_event_runtime)
        self.server._on_map_entered(session)
        self.assertEqual(session.gameplay_unlocked_maps, {"a0002_start_hub"})
        self.assertEqual(saved, ["map discovered"])
        self.server._on_map_entered(session)
        self.assertEqual(saved, ["map discovered"])

    def _travel(self, session):
        with patch.object(
            CharacterServer, "_begin_map_transfer", return_value=True
        ) as begin:
            self.server._perform_map_travel(session, HUB_REQUEST, command_id=0x0061)
            state = getattr(session, "gameplay_home_teleport_state", None)
            if state is not None:
                self.server._complete_home_teleport(
                    session, state["generation"], state["instance_id"]
                )
        begin.assert_called_once()
        return begin.call_args

    def test_hub_resolves_to_the_last_visited_town(self):
        session = self._session(current_map="a0303_wildcave", last_town="b1100_hub")
        call = self._travel(session)
        self.assertEqual(call.args[2], "b1100_hub")
        # The hub is entered at its travel stone, not its Respawnpoint.
        self.assertEqual(
            call.kwargs["spawn_position"],
            load_local_travel_point_position(DEFAULT_STATIC_DB_PATH, "b1100_hub"),
        )
        self.assertEqual(call.kwargs["reason"], "fast-travel")

    def test_without_a_remembered_town_the_nearest_hub_is_used(self):
        session = self._session(current_map="a0303_wildcave", last_town="")
        destination = self._travel(session).args[2]
        self.assertEqual(self.server.static_database.gameplay_map_ruleset(destination), 5)

    def test_a_non_hub_memory_is_ignored(self):
        session = self._session(current_map="a0303_wildcave", last_town="a0303_wildcave")
        destination = self._travel(session).args[2]
        self.assertNotEqual(destination, "a0303_wildcave")
        self.assertEqual(self.server.static_database.gameplay_map_ruleset(destination), 5)

    def test_unknown_current_map_falls_back_to_kingshill(self):
        session = self._session(current_map="", last_town="")
        self.assertEqual(
            self.server._resolve_hub_travel_destination(session), "a0200_kingscity"
        )

    def test_mortis_return_stone_goes_to_cardhun_regardless_of_last_town(self):
        session = self._session(current_map="pw214_mortis_boss", last_town="a0200_kingscity")
        exit = SimpleNamespace(
            destination_map_id="home", destination_entry_name="", name="home",
        )
        with patch.object(CharacterServer, "_begin_map_transfer", return_value=True) as begin:
            self.assertTrue(self.server._activate_map_exit(session, exit, trigger="client cmd 0x0060"))
        begin.assert_called_once()
        self.assertEqual(begin.call_args.args[2], "pw000_hub")
        self.assertEqual(
            begin.call_args.kwargs["spawn_position"],
            load_local_travel_point_position(DEFAULT_STATIC_DB_PATH, "pw000_hub"),
        )
        self.assertTrue(begin.call_args.kwargs["gameplay_switch"])

    def test_other_home_exits_resolve_to_a_real_last_visited_town(self):
        session = self._session(current_map="a0303_wildcave", last_town="b1100_hub")
        exit = SimpleNamespace(
            destination_map_id="home", destination_entry_name="", name="home",
        )
        with patch.object(CharacterServer, "_begin_map_transfer", return_value=True) as begin:
            self.assertTrue(self.server._activate_map_exit(session, exit, trigger="client cmd 0x0060"))
        self.assertEqual(begin.call_args.args[2], "b1100_hub")

    def test_ordinary_destinations_arrive_at_their_travel_stone(self):
        session = self._session(current_map="a0303_wildcave", last_town="b1100_hub")
        session.gameplay_unlocked_maps.add("a0200_kingscity")
        request = MapTravelRequest(
            difficulty=0, map_id="a0200_kingscity", entry_index=0, force_entry=False,
        )
        with patch.object(
            CharacterServer, "_begin_map_transfer", return_value=True
        ) as begin:
            self.server._perform_map_travel(session, request, command_id=0x0061)
        # Not renamed like "Hub", but placed at the map's travel stone
        # (map_transition), as the world map and travel stones arrive.
        self.assertEqual(begin.call_args.args[2], "a0200_kingscity")
        self.assertEqual(
            begin.call_args.kwargs["spawn_position"],
            load_local_travel_point_position(DEFAULT_STATIC_DB_PATH, "a0200_kingscity"),
        )


    def test_hub_teleport_starts_channeling_with_castbar_status_effect(self):
        session = self._session(current_map="a0303_wildcave", last_town="b1100_hub")
        with patch.object(CharacterServer, "_begin_map_transfer", return_value=True) as begin:
            self.server._perform_map_travel(session, HUB_REQUEST, command_id=0x0061)
        # Should not transfer immediately
        begin.assert_not_called()
        state = getattr(session, "gameplay_home_teleport_state", None)
        self.assertIsNotNone(state)
        self.assertEqual(state["target_map_id"], "b1100_hub")
        entry = state["entry"]
        self.assertEqual(entry.effect_id, 656)  # hometeleport
        self.assertGreater(entry.total_duration, 0)
        self.assertEqual(state["difficulty"], 0)

    def test_hub_teleport_channeling_cancelled_by_movement(self):
        session = self._session(current_map="a0303_wildcave", last_town="b1100_hub")
        self.server._perform_map_travel(session, HUB_REQUEST, command_id=0x0061)
        self.assertIsNotNone(getattr(session, "gameplay_home_teleport_state", None))
        self.server._cancel_home_teleport(session, reason="player movement")
        self.assertIsNone(getattr(session, "gameplay_home_teleport_state", None))

    def test_hub_teleport_stationary_move_does_not_cancel(self):
        from dso_server.protocol.gameplay_protocol import Move
        session = self._session(current_map="a0303_wildcave", last_town="b1100_hub")
        session.gameplay_last_move = Move.from_values((1.0, 2.0, 3.0), 0.0, 0.0, 0.0, 100, 20)
        self.server._perform_map_travel(session, HUB_REQUEST, command_id=0x0061)
        self.assertIsNotNone(getattr(session, "gameplay_home_teleport_state", None))

        # Stationary move packet (same position, scalar=0, duration_ticks=20)
        move = Move.from_values((1.0, 2.0, 3.0), 0.0, 0.0, 0.0, 101, 20)
        payload = b"\x8b\x5f\x00" + move.encode_body()
        self.server._handle_gameplay_move(session, payload)

        # Teleport must NOT be cancelled
        self.assertIsNotNone(getattr(session, "gameplay_home_teleport_state", None))

    def test_hub_teleport_displacement_cancels_channeling(self):
        from dso_server.protocol.gameplay_protocol import Move
        session = self._session(current_map="a0303_wildcave", last_town="b1100_hub")
        session.gameplay_last_move = Move.from_values((1.0, 2.0, 3.0), 0.0, 0.0, 0.0, 100, 20)
        self.server._perform_map_travel(session, HUB_REQUEST, command_id=0x0061)
        self.assertIsNotNone(getattr(session, "gameplay_home_teleport_state", None))

        # Displaced move packet (> 0.5m away)
        move = Move.from_values((3.0, 2.0, 3.0), 1.0, 0.0, 0.0, 101, 20)
        payload = b"\x8b\x5f\x00" + move.encode_body()
        self.server._handle_gameplay_move(session, payload)

        # Teleport MUST be cancelled
        self.assertIsNone(getattr(session, "gameplay_home_teleport_state", None))


if __name__ == "__main__":
    unittest.main()
