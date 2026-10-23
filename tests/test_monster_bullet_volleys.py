"""Every bullet of a monster volley (BulletCount > 1, BulletEmitDelay > 0) hits.

Boss01Skill02 (the cult leader's homing orbs): BulletCount 3, BulletEmitDelay
0.25 s, LoopStartFrame 24, bullet velocity 6.5, LifeTime 5: three hits, 0.25 s
apart, each when its orb reaches the player; a player running at 5 m/s from
12 m outruns them and takes nothing.
Boss01Skill02_nonhoming flies straight: a bullet misses a player who left
its path.
"""

from dataclasses import replace

import pytest

from dso_server.gameplay.local_combat_state import (
    PLAYER_PROJECTILE_COLLISION_RADIUS, SERVER_TICKS_PER_SECOND,
    LocalCombatState,
)
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.paths import DEFAULT_STATIC_DB_PATH


def _cult_leader(skill, distance=4.0):
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
        (position[0] + distance, position[1], position[2])
    )
    state.last_monster_attack_at[caster] = 0.0
    (first,) = state.maybe_start_monster_attacks(
        near, server_tick=5000, now=1000.0, target_actor_id=1,
        eligible_actor_ids={caster},
    )
    return encounter, state, caster, near, first


def _run(state, encounter, caster, start, speed, until):
    """Resolve every server tick while the player runs along x (no wall)."""

    x, y, z = state.monster_positions[caster]
    hits = []
    for tick in range(int((until - 1000.0) * SERVER_TICKS_PER_SECOND) + 1):
        now = 1000.0 + tick / SERVER_TICKS_PER_SECOND
        wire = encounter.coordinate_frame.world_to_wire(
            (x + start + speed * (now - 1000.0), y, z)
        )
        hits += state.resolve_due_monster_attacks(
            wire, now=now, player_actor_id=1,
        )
    return hits


def test_homing_volley_lands_every_orb_when_it_reaches_the_player():
    encounter, state, caster, _near, first = _cult_leader("Boss01Skill02")
    release = 1000.0 + 24 / SERVER_TICKS_PER_SECOND
    later = list(state.pending_monster_projectile_attacks[caster])
    assert first.due_at == pytest.approx(release)
    assert [b.due_at - first.due_at for b in later] == [
        pytest.approx(0.25), pytest.approx(0.5),
    ]
    hits = _run(state, encounter, caster, 4.0, 0.0, 1003.0)
    flight = (4.0 - 0.3 - PLAYER_PROJECTILE_COLLISION_RADIUS) / 6.5
    assert [h.server_tick for h in hits] == [
        5000 + round((release - 1000.0 + flight + k * 0.25) * 25)
        for k in range(3)
    ]
    assert not state.pending_monster_projectile_attacks
    assert not state.pending_monster_attacks


def test_homing_orbs_catch_a_close_runner_but_not_a_far_one():
    encounter, state, caster, _near, _first = _cult_leader(
        "Boss01Skill02", distance=-1.5
    )
    # At 4 m/s from 1.5 m: 5.3 m at the release, caught 1.8 s later.
    assert len(_run(state, encounter, caster, -1.5, -4.0, 1008.0)) == 3
    encounter, state, caster, _near, _first = _cult_leader(
        "Boss01Skill02", distance=-12.0
    )
    assert _run(state, encounter, caster, -12.0, -5.0, 1008.0) == []
    # Their LifeTime ran out: they are gone.
    assert not state.pending_monster_projectile_attacks
    assert not state.pending_monster_attacks


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
