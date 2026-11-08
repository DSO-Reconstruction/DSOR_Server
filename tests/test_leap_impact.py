import unittest
from types import SimpleNamespace

from dso_server.gameplay.local_combat_state import LocalCombatState


class LeapImpactTests(unittest.TestCase):
    def geometry(self, skill_type):
        return SimpleNamespace(skill_type=skill_type, hit_frame=11, loop_start_frame=4,
                               skill_timing_multiplier=1.0, scale_skill_timing=False)

    def test_a_jump_hits_when_it_lands_after_its_release_phase(self):
        # Rage Jump (enragingleap): LoopStartFrame 4 + HitFrame 11 at 25 Hz.
        self.assertAlmostEqual(LocalCombatState._player_skill_impact_delay(self.geometry("Jump"), 1.0), 0.6)

    def test_an_ordinary_skill_still_hits_at_its_hit_frame(self):
        self.assertAlmostEqual(LocalCombatState._player_skill_impact_delay(self.geometry("Melee"), 1.0), 0.44)


if __name__ == "__main__":
    unittest.main()
