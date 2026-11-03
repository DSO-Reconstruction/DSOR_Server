"""Boss arena entrances and their corresponding return exits need native scene creation."""

from pathlib import Path
import unittest

from dso_server.gameplay.local_map_state import (
    load_local_map_exits,
    load_local_map_initialization,
    load_local_entry_position,
)
from dso_server.protocol.player_init_protocol import (
    VARIATION_INFO_COMMAND,
)


class BossExitVisibilityTests(unittest.TestCase):
    path = Path(__file__).resolve().parents[1] / "data/db/static.db4"

    def test_eternal_grove_creates_bearach_boss_exit_all_tiers(self):
        map_id = "a0406_dun"
        boss_destination = "a0407_boss"
        authored = load_local_map_exits(self.path, map_id)
        for tier in (0, 1, 2, 3, 4, 6, 8):
            suffix = f"_{tier:02}" if tier else ""
            with self.subTest(tier=tier):
                commands, exits = load_local_map_initialization(
                    self.path, map_id, selected_map_access_id=map_id + suffix,
                    selected_map_difficulty=tier,
                    include_inactive_event_sets=True,
                )
                create_commands = [c for c in commands if c.command_id == VARIATION_INFO_COMMAND]
                self.assertEqual(len(create_commands), 1)
                create = create_commands[0]
                expected = {
                    e.instance_guid for e in authored
                    if e.destination_map_id == boss_destination + suffix
                }
                self.assertEqual(len(expected), 1)
                self.assertEqual({e.instance_guid for e in create.value.entries}, expected)
                self.assertTrue(all(e.event_set_row == -1 for e in create.value.entries))

    def test_bearach_boss_arena_creates_return_exit_to_eternal_grove(self):
        boss_map = "a0407_boss"
        return_destination = "a0406_dun"
        authored = load_local_map_exits(self.path, boss_map)
        for tier in (0, 1, 2, 3, 4, 6, 8):
            suffix = f"_{tier:02}" if tier else ""
            with self.subTest(tier=tier):
                commands, exits = load_local_map_initialization(
                    self.path, boss_map, selected_map_access_id=boss_map + suffix,
                    selected_map_difficulty=tier,
                    include_inactive_event_sets=True,
                )
                create_commands = [c for c in commands if c.command_id == VARIATION_INFO_COMMAND]
                self.assertEqual(len(create_commands), 1)
                create = create_commands[0]
                expected = {
                    e.instance_guid for e in authored
                    if e.destination_map_id == return_destination + suffix
                }
                self.assertEqual(len(expected), 1)
                self.assertEqual({e.instance_guid for e in create.value.entries}, expected)

    def test_bearach_return_exit_entry_position_resolves_to_grove_gate(self):
        boss_exits = load_local_map_exits(self.path, "a0407_boss")
        return_exit = next(e for e in boss_exits if e.destination_map_id == "a0406_dun")
        grove_entry_pos = load_local_entry_position(
            self.path, "a0406_dun", return_exit.destination_entry_name,
        )
        # Entry in Eternal Grove should be right at the stone plaza near (-139.49, 9.35, -30.68)
        self.assertAlmostEqual(grove_entry_pos[0], -139.491699, places=2)
        self.assertAlmostEqual(grove_entry_pos[2], -30.678979, places=2)

    def test_all_boss_return_exits_create_native_scene_entities(self):
        pairs = (
            ("a0204_boss", "a0203_kingscrypt02"),
            ("a0404_culttower03", "a0403_culttower02"),
            ("a0407_boss", "a0406_dun"),
            ("a0506_boss", "a0503_dun"),
            ("a0604_heraldsslair", "a0603_dragonslair02"),
            ("c2304_boss", "c2303_dun"),
            ("f4203_boss", "f4202_dun"),
        )
        for boss_map, return_map in pairs:
            with self.subTest(boss_map=boss_map, return_map=return_map):
                commands, _ = load_local_map_initialization(
                    self.path, boss_map, selected_map_difficulty=0,
                )
                create_commands = [c for c in commands if c.command_id == VARIATION_INFO_COMMAND]
                self.assertTrue(create_commands, f"VariationInfo command missing for {boss_map}")
                create = create_commands[0]
                authored = load_local_map_exits(self.path, boss_map)
                expected = {
                    e.instance_guid for e in authored
                    if e.destination_map_id == return_map
                }
                self.assertTrue(expected, f"No authored return exit found from {boss_map} to {return_map}")
                self.assertTrue(
                    expected <= {e.instance_guid for e in create.value.entries},
                    f"Return exit GUID not in VariationInfo for {boss_map}",
                )


if __name__ == "__main__":
    unittest.main()
