"""Boss recovery ends with the cast animation, not projectile travel."""

from pathlib import Path
from types import SimpleNamespace
import unittest

from dso_server.gameplay.local_combat_state import LocalCombatState, MonsterCombatant
from dso_server.gameplay.local_monster_state import _load_runtime_definition


class BossProjectileRecoveryTests(unittest.TestCase):
    def test_sigrismarr_melee_while_frostorb_is_still_flying(self):
        for template in ("e_bos_02_boss_sigrismar_normal", "boss_sigrismarr"):
            with self.subTest(template=template):
                path = Path(__file__).resolve().parents[1] / "data/db/static.db4"
                runtime = _load_runtime_definition(path, template, 100)
                monster = MonsterCombatant(
                    actor_id=100, template_id=template, level=100,
                    rank_id="Rank_boss", position=(0., 0., 0.),
                    maximum_health=100000, minimum_damage=100., maximum_damage=150.,
                    run_speed=runtime.run_speed, aggro_range=150., escape_range=150.,
                    travel_distance=150., attack_interval=runtime.attack_interval,
                    collision_radius=runtime.capsule_radius,
                    attack=runtime.attack, attacks=runtime.attacks,
                )
                encounter = SimpleNamespace(
                    static_database_path=path, map_id="sigrismarr-test",
                    coordinate_frame=SimpleNamespace(wire_to_world=lambda p: p),
                    navigation=SimpleNamespace(project_position=lambda p, **kw: p),
                    spawn_batch=SimpleNamespace(commands=()), actor_ids=(100,),
                )

                class Combat(LocalCombatState):
                    def _combatants(self, encounter):
                        return {100: monster}

                combat = Combat(encounter, player_actor_id=1, player_health=100000)
                combat.last_monster_attack_at[100] = 0.
                combat._monster_skill_ready_at[100] = {
                    skill.cooldown_category or skill.resource_id: float("inf")
                    for skill in runtime.attacks
                    if skill.cooldown > 0. and "frostorb" not in skill.resource_id.lower()
                }
                orb, = combat.maybe_start_monster_attacks(
                    (25., 0., 0.), now=1000., server_tick=100,
                )
                self.assertIn("frostorb", orb.definition.resource_id.lower())
                recovery = orb.skill_unblock_frame / 25.
                self.assertGreater(orb.due_at, 1000. + recovery + 1.)
                self.assertFalse(combat.maybe_start_monster_attacks(
                    (2., 0., 0.), now=1000. + recovery - .01, server_tick=164,
                ))
                melee, = combat.maybe_start_monster_attacks(
                    (2., 0., 0.), now=1000. + recovery + .01, server_tick=166,
                )
                self.assertIn("melee", melee.definition.resource_id.lower())
                self.assertEqual(combat.pending_monster_projectile_attacks[100], [orb])
                self.assertIs(combat.pending_monster_attacks[100], melee)
                melee_hits = combat.resolve_due_monster_attacks(
                    (2., 0., 0.), now=melee.due_at, finalize_area_attacks=True,
                )
                self.assertEqual(len(melee_hits), 1)
                self.assertEqual(combat.pending_monster_projectile_attacks[100], [orb])
                orb_hits = combat.resolve_due_monster_attacks(
                    (2., 0., 0.), now=orb.due_at, finalize_area_attacks=True,
                )
                self.assertEqual(len(orb_hits), 1)
                self.assertEqual(orb_hits[0].skill_index, orb.skill_index)
                self.assertFalse(combat.pending_monster_projectile_attacks)
                self.assertFalse(combat.resolve_due_monster_attacks(
                    (2., 0., 0.), now=orb.due_at + 1., finalize_area_attacks=True,
                ))


if __name__ == "__main__":
    unittest.main()
