"""Assault the fortress: the authored CastleRush objective chain on f4103_atf."""

from __future__ import annotations

import unittest

from dso_server.gameplay.pvp_objectives import (
    FORTRESS_ATTACKERS_WON,
    FORTRESS_STAGE_COMPLETED,
    FortressState,
    load_battleground_settings,
    load_fortress_layout,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.pvp_match_protocol import (
    MATCH_MODE_ASSAULT_THE_FORTRESS_5V5,
    SUPPORTED_MATCH_MODES,
)

BLUE, RED = 0, 1


class FortressRulesTests(unittest.TestCase):
    def setUp(self):
        self.layout = load_fortress_layout(DEFAULT_STATIC_DB_PATH, "f4103_atf")
        self.state = FortressState(self.layout)

    def row(self, template_id):
        return next(o.row_id for o in self.layout.objectives if o.template_id == template_id)

    def destroy(self, *templates):
        events = []
        for template in templates:
            events.extend(self.state.destroy(self.row(template), attacker_team=RED))
        return events

    def test_roles_and_stage_targets_come_from_the_map(self):
        self.assertEqual(self.layout.defender_team, BLUE)
        self.assertEqual(self.layout.attacker_team, RED)
        self.assertEqual(self.layout.stage_targets, {
            0: ("a0209_atf_stage01_target",), 1: ("a0209_atf_stage02_target",),
        })
        settings = load_battleground_settings(DEFAULT_STATIC_DB_PATH, "AssaultTheFortress_5v5_01")
        self.assertEqual(settings.ruleset, "CastleRush")
        self.assertEqual(settings.map_name, "f4103_atf")
        # 2 s base respawn, +3 s for every earlier death.
        self.assertEqual(settings.respawn_delay_seconds(1), 2.0)
        self.assertEqual(settings.respawn_delay_seconds(3), 8.0)

    def test_lane_towers_unlock_in_order(self):
        t2 = self.row("pvp_defense_tower_left_lane_t2")
        self.assertFalse(self.state.vulnerable(t2))
        self.assertEqual(self.state.destroy(t2, attacker_team=RED), ())
        self.destroy("pvp_defense_tower_left_lane_t1")
        self.assertTrue(self.state.vulnerable(t2))
        invincible = self.row("pvp_defense_tower_left_lane_t2_invincible")
        self.assertFalse(self.state.vulnerable(invincible))

    def test_defenders_cannot_destroy_their_own_towers(self):
        t1 = self.row("pvp_defense_tower_left_lane_t1")
        self.assertEqual(self.state.destroy(t1, attacker_team=BLUE), ())
        self.assertTrue(self.state.vulnerable(t1))

    def test_drawbridge_needs_the_core_tower_and_completes_stage_zero(self):
        bridge = self.row("pvp_a0209_drawbridge_trigger")
        self.assertFalse(self.state.vulnerable(bridge))
        self.destroy(
            "pvp_defense_tower_mid_lane_t1A", "pvp_defense_tower_mid_lane_t1B",
            "pvp_defense_tower_mid_lane_t2", "pvp_defense_tower_mid_lane_t4",
        )
        self.assertFalse(self.state.vulnerable(bridge))
        self.assertTrue(self.state.vulnerable(self.row("pvp_defense_tower_t5")))
        self.destroy("pvp_defense_tower_t5")
        self.assertTrue(self.state.vulnerable(bridge))
        red_before = self.state.respawn(RED)
        events = self.destroy("pvp_a0209_drawbridge_trigger")
        self.assertIn(FORTRESS_STAGE_COMPLETED, [kind for kind, *_ in events])
        self.assertEqual(self.state.stage, 1)
        # Both sides move to their stage-1 respawn entries.
        self.assertNotEqual(self.state.respawn(RED), red_before)
        self.assertAlmostEqual(self.state.respawn(RED)[0][0], -24.28, places=1)
        self.assertAlmostEqual(self.state.respawn(BLUE)[0][0], 63.98, places=1)

    def test_gun_control_only_counts_in_stage_one_and_wins(self):
        gun = self.row("pvp_a0209_guncontrol")
        self.assertFalse(self.state.vulnerable(gun))
        self.destroy(
            "pvp_defense_tower_mid_lane_t1A", "pvp_defense_tower_mid_lane_t1B",
            "pvp_defense_tower_mid_lane_t2", "pvp_defense_tower_mid_lane_t4",
            "pvp_defense_tower_t5", "pvp_a0209_drawbridge_trigger",
        )
        events = self.destroy("pvp_a0209_guncontrol")
        self.assertIn(FORTRESS_ATTACKERS_WON, [kind for kind, *_ in events])
        self.assertTrue(self.state.attackers_won)
        self.assertEqual(self.destroy("pvp_defense_tower_top_01"), [])

    def test_fortress_is_queueable(self):
        self.assertIn(MATCH_MODE_ASSAULT_THE_FORTRESS_5V5, SUPPORTED_MATCH_MODES)

    def test_control_points_and_attacker_monsters_come_from_the_map(self):
        points = {(p.template_id, p.monster_id) for p in self.layout.control_points}
        self.assertEqual(points, {
            ("offensive_meanie_controlpoint", "pvp_spawner_bombs"),
            ("offensive_cyclops_controlpoint", "pvp_spawner_rammer"),
        })
        self.assertTrue(all(p.owner_team == RED and p.capture_seconds == 10.0
                            for p in self.layout.control_points))
        self.assertEqual(set(self.layout.attacker_templates),
                         {"pvp_spawner_bombs", "pvp_bomb", "pvp_spawner_rammer", "pvp_rammer"})
        teams = self.layout.team_templates()
        self.assertEqual(teams["pvp_defense_tower_t5"], BLUE)
        self.assertEqual(teams["pvp_rammer"], RED)
        present = [p for p in self.layout.control_points if self.state.control_point_present(p)]
        self.assertEqual([p.position[0] for p in present], [-116.0])


if __name__ == "__main__":
    unittest.main()
