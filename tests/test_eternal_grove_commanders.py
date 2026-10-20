"""Eternal Grove's ClearRegion needs one living instance of every template."""

from pathlib import Path
import math
import unittest

from dso_server.gameplay.local_monster_state import (
    LOCAL_MONSTER_ACTOR_BASE,
    load_local_monster_encounter,
    load_local_spawn_position,
)
from dso_server.protocol.gameplay_protocol import NewMonsterServerCommand


class EternalGroveCommanderTests(unittest.TestCase):
    map_id = "pw011_eternal_grove_01_dun"
    required_templates = frozenset({
        "pw011_01_champion_thing_roots_heroic",
        "pw011_01_champion_beastman_stag_heroic",
        "pw011_01_champion_satyr_melee_heroic",
        "pw011_01_champion_beast_hound_heroic",
        "pw011_02_champion_troll_heroic",
        "pw011_02_champion_satyr_ranged_heroic",
    })

    def test_clear_region_templates_are_present(self):
        root = Path(__file__).resolve().parents[1]
        static_database = root / "data/db/static.db4"
        spawn = load_local_spawn_position(static_database, self.map_id)
        encounter = load_local_monster_encounter(
            static_database,
            self.map_id,
            include_spawn_areas=False,
            monster_level=1,
            player_position=spawn,
        )

        self.assertIsNotNone(encounter)
        templates = {
            command.value.template_id.casefold()
            for command in encounter.spawn_batch.commands
            if isinstance(command, NewMonsterServerCommand)
        }
        self.assertEqual(
            templates & self.required_templates,
            self.required_templates,
        )

    def test_client_owned_actor_slot_stays_unused(self):
        root = Path(__file__).resolve().parents[1]
        static_database = root / "data/db/static.db4"
        spawn = load_local_spawn_position(static_database, self.map_id)
        encounter = load_local_monster_encounter(
            static_database,
            self.map_id,
            include_spawn_areas=True,
            monster_level=1,
            player_position=spawn,
        )

        self.assertIsNotNone(encounter)
        actor_ids = {
            command.value.actor_id
            for group in encounter.groups
            for command in group.spawn_batch.commands
            if isinstance(command, NewMonsterServerCommand)
        }
        self.assertNotIn(
            LOCAL_MONSTER_ACTOR_BASE + 1,
            actor_ids,
            "the client owns that actor slot; a server monster there never "
            "produces a model",
        )

    def test_terrace_commander_is_replaced_by_a_visible_model(self):
        root = Path(__file__).resolve().parents[1]
        static_database = root / "data/db/static.db4"
        spawn = load_local_spawn_position(static_database, self.map_id)
        encounter = load_local_monster_encounter(
            static_database,
            self.map_id,
            include_spawn_areas=False,
            monster_level=1,
            player_position=spawn,
        )

        self.assertIsNotNone(encounter)
        commanders = {
            command.value.actor_id: (
                command.value.template_id.casefold(),
                command.value.position,
            )
            for command in encounter.spawn_batch.commands
            if isinstance(command, NewMonsterServerCommand)
            and "champion" in command.value.template_id.casefold()
        }
        for actor_id, (template_id, position) in commanders.items():
            with self.subTest(actor=actor_id):
                self.assertFalse(
                    math.hypot(
                        position[0] - 23.25,
                        position[2] + 17.76,
                    ) <= 0.75,
                    "the north-east terrace commander is not rendered by the "
                    "client and must not be authored there",
                )
        entry_distance = min(
            math.hypot(
                position[0] - spawn[0],
                position[2] - spawn[2],
            )
            for _template_id, position in commanders.values()
        )
        self.assertLess(entry_distance, 20.0)


if __name__ == "__main__":
    unittest.main()
