from __future__ import annotations

from pathlib import Path
import unittest

from dso_server.services.events import _baseline_map_local_event_entries


STATIC_DATABASE = (
    Path(__file__).resolve().parents[1] / 'data' / 'db' / 'static.db4'
)


class MapLocalEventUpdateTests(unittest.TestCase):
    def test_kingshill_activates_thabo_before_npc_initialization(self):
        entries = _baseline_map_local_event_entries(
            STATIC_DATABASE, 'a0200_kingscity'
        )
        thabo = next(
            entry for entry in entries
            if entry.event_name == 'a0200_unlock_thabo'
        )
        self.assertEqual(thabo.flags, (True, True, False, False))
        self.assertIsNone(thabo.starts_at)
        self.assertIsNone(thabo.ends_at)

    def test_unconditional_hub_does_not_receive_kingshill_event(self):
        self.assertEqual(
            _baseline_map_local_event_entries(
                STATIC_DATABASE, 'b1100_hub'
            ),
            (),
        )

    def test_shared_desert_map_activates_only_selected_color(self):
        entries = _baseline_map_local_event_entries(
            STATIC_DATABASE,
            'e_doe_map_02',
            ('e_doe_map_02_white',),
        )
        self.assertEqual(
            tuple(entry.event_name for entry in entries),
            ('e_doe_map_02_white',),
        )


if __name__ == '__main__':
    unittest.main()
