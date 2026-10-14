"""Tutorial relogs return to their own hub without losing quest progress."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence.static_game_database import StaticGameDatabase
from dso_server.services.character_server import CharacterServer


class SafeLoginTests(unittest.TestCase):
    def setUp(self):
        self.server = CharacterServer.__new__(CharacterServer)
        self.server.static_database_path = DEFAULT_STATIC_DB_PATH
        self.server.static_database = StaticGameDatabase(DEFAULT_STATIC_DB_PATH)
        self.server.account_store = Mock()
        self.character = SimpleNamespace(character_id=42, name="TutorialPlayer", map_id="")

    def saved_state(self, map_id):
        state = SimpleNamespace(
            character_id=42, map_id=map_id, map_access_id=map_id,
            position=(12.0, 3.0, 45.0), map_difficulty=2, scaling_difficulty=3,
            quest_log={"a0002-WayToGrimmagstone-03-r": {
                "state": 1, "finished_mask": 7, "tokens": {"first-stage": 1}}},
            unlocked_maps=("a0002_start_hub", map_id),
        )
        self.character.map_id = map_id
        self.server.account_store.get.return_value = state
        self.server.account_store.put.reset_mock()
        return state

    def test_every_tutorial_dungeon_relogs_into_tutorial_hub(self):
        for map_id in ("a0001_start_tutorial_dun", "a0003_start_garden_dun",
                       "a0004_start_tomb_dun", "a0005_start_fortress_dun"):
            with self.subTest(map=map_id):
                state = self.saved_state(map_id)
                quests, discoveries = state.quest_log.copy(), state.unlocked_maps
                destination = self.server._persisted_gameplay_map(self.character, "a0200_kingscity")
                self.assertEqual(destination, "a0002_start_hub")
                self.assertEqual(state.map_id, destination)
                self.assertEqual(state.map_access_id, destination)
                self.assertEqual(self.character.map_id, destination)
                self.assertIsNone(state.position)  # use the hub's authored entry
                self.assertEqual((state.map_difficulty, state.scaling_difficulty), (0, 0))
                self.assertEqual(state.quest_log, quests)
                self.assertEqual(state.unlocked_maps, discoveries)
                self.server.account_store.put.assert_called_once_with(state, flush=True)
                # The next relog stays in the hub without moving/resetting again.
                self.server.account_store.put.reset_mock()
                self.assertEqual(self.server._persisted_gameplay_map(self.character, map_id), destination)
                self.server.account_store.put.assert_not_called()

    def test_hub_logout_retains_saved_position(self):
        for map_id in ("a0002_start_hub", "a0006_grimford_hub", "a0200_kingscity"):
            with self.subTest(map=map_id):
                state = self.saved_state(map_id)
                self.assertEqual(self.server._persisted_gameplay_map(self.character, "unused"), map_id)
                self.assertEqual(state.position, (12.0, 3.0, 45.0))
                self.server.account_store.put.assert_not_called()

    def test_post_tutorial_combat_map_still_uses_its_nearby_hub(self):
        self.saved_state("a0007_pastures_pve")
        self.assertEqual(self.server._persisted_gameplay_map(self.character, "unused"),
                         "a0006_grimford_hub")

    def test_tutorial_return_hub_overrides_an_old_cached_fallback(self):
        self.server._safe_login_hub_cache = {"a0004_start_tomb_dun": "a0200_kingscity"}
        self.assertEqual(self.server._nearest_login_hub("a0004_start_tomb_dun"), "a0002_start_hub")

    def test_character_without_saved_state_keeps_initial_tutorial_spawn(self):
        self.server.account_store.get.return_value = None
        self.assertEqual(self.server._persisted_gameplay_map(self.character, "a0001_start_tutorial_dun"),
                         "a0001_start_tutorial_dun")
        self.server.account_store.put.assert_not_called()
