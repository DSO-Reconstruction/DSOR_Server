"""Per-skill projectile collider bonuses, currently only Ice Missile."""

from __future__ import annotations

import unittest
import sqlite3

from dso_server.gameplay.local_combat_state import (
    PLAYER_SKILL_PROJECTILE_RADIUS_BONUS,
    LocalCombatState,
    _parse_skill_radius_bonuses,
)
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.paths import DEFAULT_STATIC_DB_PATH


class SkillHitboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        encounter = load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False
        )
        cls.state = LocalCombatState(encounter, player_actor_id=1, player_health=1000)
        with sqlite3.connect(f"{DEFAULT_STATIC_DB_PATH.as_uri()}?mode=ro", uri=True) as database:
            cls.skill_ids = dict(database.execute(
                "SELECT Id, rowid-1 FROM _Template_Skill WHERE Id IN ('iceball', 'magicmissile')",
            ))

    def test_default_bonus_targets_ice_missile_only(self):
        self.assertEqual(PLAYER_SKILL_PROJECTILE_RADIUS_BONUS, {"iceball": 0.15})

    def test_ice_missile_geometry_carries_the_bonus(self):
        ice = self.state._load_player_skill_geometry(self.skill_ids["iceball"])
        self.assertEqual(ice.resource_id, "iceball")
        self.assertAlmostEqual(ice.projectile_radius, 0.25 + 0.15, places=5)

    def test_other_projectiles_keep_their_authored_radius(self):
        magic = self.state._load_player_skill_geometry(self.skill_ids["magicmissile"])
        self.assertEqual(magic.resource_id, "magicmissile")
        self.assertAlmostEqual(magic.projectile_radius, 0.20, places=5)

    def test_bonus_setting_parser_is_forgiving(self):
        self.assertEqual(
            _parse_skill_radius_bonuses("iceball=0.15; Fireball = 0.1,bad,x=nope,=1"),
            {"iceball": 0.15, "fireball": 0.1},
        )
        self.assertEqual(_parse_skill_radius_bonuses(""), {})


if __name__ == "__main__":
    unittest.main()
