"""Every bullet of a monster volley (BulletCount > 1, BulletEmitDelay > 0) hits.

Boss01Skill02 (the cult leader's homing orbs): BulletCount 3, BulletEmitDelay
0.25 s, LoopStartFrame 24, bullet velocity 6.5: three hits, 0.25 s apart.
Boss01Skill02_nonhoming flies straight: a bullet misses a player who left
its path.
"""

from dataclasses import replace

import pytest

from dso_server.gameplay.local_combat_state import (
    SERVER_TICKS_PER_SECOND, LocalCombatState,
)
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.paths import DEFAULT_STATIC_DB_PATH


def _cult_leader(skill):
    encounter = load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH, "a0404_culttower03", include_spawn_areas=False,
    )
    state = LocalCombatState(encounter, player_actor_id=1, player_health=1_000_000)
    caster = next(
        actor_id for actor_id in encounter.actor_ids
        if any(a.resource_id == skill for a in state.monsters[actor_id].attacks)
    )
    monster = state.monsters[caster]
    attack = next(a for a in monster.attacks if a.resource_id == skill)
    state.monsters[caster] = replace(monster, attack=attack, attacks=(attack,))
    position = state.monster_positions[caster]
    near = encounter.coordinate_frame.world_to_wire(
        (position[0] + 4.0, position[1], position[2])
    )
    state.last_monster_attack_at[caster] = 0.0
    (first,) = state.maybe_start_monster_attacks(
        near, server_tick=5000, now=1000.0, target_actor_id=1,
        eligible_actor_ids={caster},
    )
    return encounter, state, caster, near, first


def test_homing_volley_lands_every_orb_emit_delay_apart():
    _encounter, state, caster, near, first = _cult_leader("Boss01Skill02")
    later = list(state.pending_monster_projectile_attacks[caster])
    assert [b.due_at - first.due_at for b in later] == [
        pytest.approx(0.25), pytest.approx(0.5),
    ]
    assert [b.impact_tick - first.impact_tick for b in later] == [6, 12]
    hits = []
    for bullet in (first, *later):
        hits += state.resolve_due_monster_attacks(
            near, now=bullet.due_at + 0.001, player_actor_id=1,
        )
    assert len(hits) == 3
    assert [h.server_tick for h in hits] == [
        first.impact_tick, *(b.impact_tick for b in later)
    ]
    assert not state.pending_monster_projectile_attacks
    assert first.due_at == pytest.approx(
        1000.0 + 24 / SERVER_TICKS_PER_SECOND + 4.0 / 6.5, abs=0.05
    )


def test_straight_volley_bullet_misses_a_player_who_left_its_path():
    encounter, state, caster, near, first = _cult_leader("Boss01Skill02_nonhoming")
    (second, third) = state.pending_monster_projectile_attacks[caster]
    assert len(state.resolve_due_monster_attacks(
        near, now=first.due_at + 0.001, player_actor_id=1,
    )) == 1
    position = state.monster_positions[caster]
    aside = encounter.coordinate_frame.world_to_wire(
        (position[0] + 4.0, position[1], position[2] + 5.0)
    )
    assert state.resolve_due_monster_attacks(
        aside, now=second.due_at + 0.001, player_actor_id=1,
    ) == ()
    assert len(state.resolve_due_monster_attacks(
        near, now=third.due_at + 0.001, player_actor_id=1,
    )) == 1
