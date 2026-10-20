"""The generated monster population is trimmed by a bounded, tunable amount."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from dso_server.gameplay import local_monster_state as monster_state
from dso_server.gameplay.local_monster_state import _MonsterSpawnArea


def square_area(side: float, density: float) -> _MonsterSpawnArea:
    matrix = (
        side, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, side, 0.0,
        0.0, 0.0, 0.0, 1.0,
    )
    return _MonsterSpawnArea(
        row_id=1, parent_node_index=0, event_set_row=-1, matrix=matrix,
        spawn_list_id="pack", density=density, priority=0,
    )


class SpawnAreaDensityScaleTests(unittest.TestCase):
    def test_default_scale_trims_about_fifteen_percent(self):
        self.assertAlmostEqual(monster_state.SPAWN_AREA_DENSITY_SCALE, 0.85)
        area = square_area(10.0, 1.0)
        self.assertEqual(len(area.candidate_positions()), 85)

    def test_scale_of_one_restores_the_authored_density(self):
        area = square_area(10.0, 1.0)
        with patch.object(monster_state, "SPAWN_AREA_DENSITY_SCALE", 1.0):
            self.assertEqual(len(area.candidate_positions()), 100)
        with patch.object(monster_state, "SPAWN_AREA_DENSITY_SCALE", 0.5):
            self.assertEqual(len(area.candidate_positions()), 50)

    def test_whole_map_cap_is_a_quarter_of_the_previous_value(self):
        self.assertEqual(monster_state.SPAWN_AREA_MAX_TOTAL_MONSTERS, 650 // 4)
        self.assertLess(monster_state.SPAWN_AREA_MAX_TOTAL_MONSTERS, 768)

    def test_encounter_cache_holds_a_live_evening_of_maps(self):
        self.assertGreaterEqual(monster_state.MONSTER_ENCOUNTER_CACHE_SIZE, 7)


if __name__ == "__main__":
    unittest.main()
