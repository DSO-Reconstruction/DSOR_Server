"""Parallel World reward chests drop the mode's starting jewels only.

The class roster is fixed: warrior and ranger share the Thundering Flower
Jewel; mage and dwarf share the Fiery Flower Jewel. Focus, Rage,
Relentlessness, and Amplified Healing are universal. Every template in the
pool is the unupgraded (Common) version, so old or upgraded jewels can no
longer drop from the reward chest.
"""

import sqlite3
import unittest
from pathlib import Path

from dso_server.services.map_events import (
    _parallel_world_reward_jewel_candidates,
)

THUNDERING = {
    "warrior": "jewel_unique_damage_AreaDamageLightningOnHit_common_warrior",
    "ranger": "jewel_unique_damage_AreaDamageLightningOnHit_common_ranger",
}
FIERY = {
    "mage": "jewel_unique_damage_AreaDamageFireOnHit_common_mage",
    "dwarf": "jewel_unique_damage_AreaDamageFireOnHit_common_dwarf",
}
SHARED = (
    "jewel_unique_damage_HealthGlobeDamageBuff_common",  # Amplified Healing
    "jewel_skill_cooldown_common",                       # Focus
    "jewel_buff_critical_attackspeed_onHitCritical_common",  # Rage
    "jewel_resource_cost_common",                        # Relentlessness
)


class ParallelWorldJewelPoolTests(unittest.TestCase):
    path = Path(__file__).resolve().parents[1] / "data/db/static.db4"

    def test_each_class_gets_its_flower_jewel_before_the_shared_ones(self):
        expected = {
            "warrior": (THUNDERING["warrior"], *SHARED),
            "ranger": (THUNDERING["ranger"], *SHARED),
            "mage": (FIERY["mage"], *SHARED),
            "dwarf": (FIERY["dwarf"], *SHARED),
        }
        for class_name, candidates in expected.items():
            with self.subTest(class_name=class_name):
                self.assertEqual(
                    _parallel_world_reward_jewel_candidates(class_name),
                    candidates,
                )

    def test_unknown_classes_cannot_receive_a_flower_jewel(self):
        self.assertEqual(
            _parallel_world_reward_jewel_candidates("niwalk"), SHARED
        )
        self.assertEqual(_parallel_world_reward_jewel_candidates(""), SHARED)

    def test_every_pool_jewel_is_an_unupgraded_template(self):
        connection = sqlite3.connect(self.path)
        try:
            for class_name in ("warrior", "mage", "ranger", "dwarf"):
                for jewel_id in _parallel_world_reward_jewel_candidates(
                    class_name
                ):
                    with self.subTest(jewel=jewel_id):
                        row = connection.execute(
                            "SELECT ItemCategory, DefaultRarity, Obsolete,"
                            " RequiredLevel, TemplateCollection"
                            " FROM _Template_Item WHERE Id=?",
                            (jewel_id,),
                        ).fetchone()
                        self.assertIsNotNone(row)
                        category, rarity, obsolete, level, collection = row
                        self.assertEqual(category, "EquipmentJewel")
                        self.assertEqual(rarity, "Common")
                        self.assertEqual(obsolete, 0)
                        self.assertEqual(level, 1)
        finally:
            connection.close()

    def test_flower_jewels_exist_with_the_published_class_roster(self):
        connection = sqlite3.connect(self.path)
        try:
            for class_name, template_id in (*THUNDERING.items(), *FIERY.items()):
                with self.subTest(class_name=class_name):
                    (char_class,) = connection.execute(
                        "SELECT CharClass FROM _Template_Item WHERE Id=?",
                        (template_id,),
                    ).fetchone()
                    self.assertEqual(char_class, class_name)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
