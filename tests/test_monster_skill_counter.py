"""Repeated boss casts must survive the native client replay-counter check."""

from pathlib import Path
from types import SimpleNamespace
import unittest

from dso_server.gameplay.local_combat_state import LocalCombatState, MonsterCombatant
from dso_server.gameplay.local_monster_state import _load_runtime_definition
from dso_server.protocol.combat_protocol import (
    MonsterAttackBatch,
    SkillExecutionContext,
    TargetSkill,
    TargetSkillServerCommand,
    decode_monster_attack_batch,
    encode_monster_attack_batch,
)


DATABASE = Path(__file__).resolve().parents[1] / "data/db/static.db4"


class MonsterSkillCounterTests(unittest.TestCase):
    def test_repeated_normal_boss_strikes_pass_native_counter_check(self):
        for template_id, map_id in (
            ("a0406_normal_g_miniboss_03_bear_q_goddessOfHunt03_scale", "a0407_boss"),
            ("a0503_g_miniboss_02_witch_q_FoulArtefact05_scale", "a0506_boss"),
        ):
            with self.subTest(template=template_id):
                runtime = _load_runtime_definition(DATABASE, template_id, 100)
                monster = MonsterCombatant(
                    actor_id=100, template_id=template_id, level=100,
                    rank_id="Rank_boss", position=(0.0, 0.0, 0.0),
                    maximum_health=100000, minimum_damage=100.0,
                    maximum_damage=150.0, run_speed=runtime.run_speed,
                    aggro_range=runtime.aggro_range, escape_range=runtime.escape_range,
                    travel_distance=runtime.travel_distance,
                    attack_interval=runtime.attack_interval,
                    collision_radius=runtime.capsule_radius,
                    attack=runtime.attack, attacks=runtime.attacks,
                )
                encounter = SimpleNamespace(
                    static_database_path=DATABASE, map_id=map_id,
                    coordinate_frame=SimpleNamespace(wire_to_world=lambda p: p),
                    navigation=SimpleNamespace(project_position=lambda p, **kwargs: p),
                    spawn_batch=SimpleNamespace(commands=()), actor_ids=(100,),
                )

                class Combat(LocalCombatState):
                    def _combatants(self, encounter):
                        return {100: monster}

                combat = Combat(encounter, player_actor_id=1, player_health=100000)
                combat.last_monster_attack_at[100] = 0.0
                # The authored normal boss basic hit uses :2 -> level 1.
                # It must not become the fixed replay counter on the wire.
                self.assertEqual(monster.attack.skill_level, 1)
                accepted = 0
                last_counter = 0
                for tick in range(1, 751):
                    now = 1000.0 + tick / 25.0
                    combat.resolve_due_monster_attacks(
                        (2.0, 0.0, 0.0), now=now, player_actor_id=1,
                        finalize_area_attacks=True,
                    )
                    attacks = combat.maybe_start_monster_attacks(
                        (2.0, 0.0, 0.0), server_tick=tick, now=now,
                    )
                    for attack in attacks:
                        if not attack.definition.resource_id.endswith("base_strike"):
                            continue
                        context = SkillExecutionContext(
                            attack.impact_tick, attack.hit_frame,
                            attack.motion_unblock_frame, attack.skill_unblock_frame,
                            0, attack.position, False,
                        )
                        command = TargetSkillServerCommand(
                            TargetSkill(
                                attack.skill_level, attack.skill_index,
                                attack.aim_angle, attack.server_tick,
                                attack.cast_id, attack.target_actor_id, context,
                            ), attack.attacker_actor_id,
                        )
                        packet = encode_monster_attack_batch(
                            MonsterAttackBatch((), (command,))
                        )
                        counter = decode_monster_attack_batch(*packet).attacks[0].value.skill_level
                        # release261 HandleSkillCommand @ 0x14033bc21:
                        # zero bypasses; otherwise cmd counter must exceed
                        # the actor's last counter (uint16 at actor +0x72).
                        self.assertTrue(counter == 0 or counter > last_counter)
                        if counter:
                            last_counter = counter
                        accepted += 1
                self.assertGreater(accepted, 10)


if __name__ == "__main__":
    unittest.main()
