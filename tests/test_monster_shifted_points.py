"""A multi-bullet Shifted monster cast falls on several points, one hit each.

Boss02HeraldSkill04 (Radius, BulletCount 5, HitRange 3, AttackRange 14): one
meteor on the target and one on each other player within 14 m.
h0005_boss_horseman_multimeteor_01 (RandomPosition, BulletCount 10,
AttackRange 10, HitRange 2.1): ten meteors scattered within 10 m of the
horseman.
"""

from dataclasses import replace
import math
from types import SimpleNamespace

from dso_server.gameplay.local_combat_state import (
    PLAYER_PROJECTILE_COLLISION_RADIUS, LocalCombatState, MonsterCombatant,
)
from dso_server.gameplay.local_monster_state import (
    _load_runtime_definition, load_local_monster_encounter,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH


def _caster(map_id, skill):
    encounter = load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH, map_id, include_spawn_areas=False,
    )
    state = LocalCombatState(encounter, player_actor_id=1, player_health=1_000_000)
    caster = next(
        actor_id for actor_id in encounter.actor_ids
        if any(a.resource_id == skill for a in state.monsters[actor_id].attacks)
    )
    monster = state.monsters[caster]
    attack = next(a for a in monster.attacks if a.resource_id == skill)
    state.monsters[caster] = replace(monster, attack=attack, attacks=(attack,))
    state.last_monster_attack_at[caster] = 0.0
    return encounter, state, caster


def test_herald_meteors_fall_on_each_player_in_reach():
    encounter, state, caster = _caster("a0604_heraldsslair", "Boss02HeraldSkill04")
    x, y, z = state.monster_positions[caster]
    # Beyond its 10.5 m Boss02Skill04Aura blow, within its 14 m reach.
    one, two, far = (x + 12.0, y, z), (x - 13.0, y, z), (x, y, z + 30.0)
    wire = encounter.coordinate_frame.world_to_wire
    (cast,) = state.maybe_start_monster_attacks(
        wire(one), server_tick=5000, now=1000.0, target_actor_id=1,
        eligible_actor_ids={caster},
        enemy_positions={1: one, 2: two, 3: far},
    )
    assert cast.target_position == one
    assert cast.extra_target_positions == (two,)
    hits = [
        hit
        for actor_id, position in ((1, one), (2, two), (3, far))
        for hit in state.resolve_due_monster_attacks(
            wire(position), now=cast.due_at + 0.001, player_actor_id=actor_id,
            finalize_area_attacks=actor_id == 3,
        )
    ]
    assert sorted(hit.target_actor_id for hit in hits) == [1, 2]
    assert not state.pending_monster_attacks
    assert not state.pending_monster_projectile_attacks


def _horseman():
    path = DEFAULT_STATIC_DB_PATH
    runtime = _load_runtime_definition(path, "h0005_miniboss_horseman", 100)
    skill = next(
        a for a in runtime.attacks
        if a.resource_id == "h0005_boss_horseman_multimeteor_01"
    )
    monster = MonsterCombatant(
        actor_id=100, template_id="h0005_miniboss_horseman", level=30,
        rank_id="Rank_boss", position=(0.0, 0.0, 0.0),
        maximum_health=100000, minimum_damage=100.0, maximum_damage=100.0,
        run_speed=runtime.run_speed, aggro_range=50.0, escape_range=150.0,
        travel_distance=150.0, attack_interval=runtime.attack_interval,
        collision_radius=runtime.capsule_radius, attack=skill, attacks=(skill,),
    )
    encounter = SimpleNamespace(
        static_database_path=path, map_id="horseman-test",
        coordinate_frame=SimpleNamespace(
            wire_to_world=lambda p: p, world_to_wire=lambda p: p,
        ),
        navigation=SimpleNamespace(project_position=lambda p, **kw: p),
        spawn_batch=SimpleNamespace(commands=()), actor_ids=(100,),
    )

    class Combat(LocalCombatState):
        def _combatants(self, encounter):
            return {100: monster}

    state = Combat(encounter, player_actor_id=1, player_health=1_000_000)
    state.last_monster_attack_at[100] = 0.0
    state._monster_skill_ready_at[100] = {}  # past its InitialCoolDown
    return state


def test_horseman_scatters_ten_meteors_around_itself():
    state = _horseman()
    near = (3.0, 0.0, 0.0)
    (cast,) = state.maybe_start_monster_attacks(
        near, server_tick=5000, now=1000.0, target_actor_id=1,
    )
    points = (cast.target_position, *cast.extra_target_positions)
    assert len(set(points)) == 10
    assert all(math.hypot(p[0], p[2]) <= 10.0 for p in points)
    # The same cast scatters them the same way.
    assert points == state._monster_shifted_points(
        100, cast.definition, (0.0, 0.0, 0.0), 1, near, cast.cast_id, None,
    )
    # One ground hit per meteor within reach of the player.
    reach = cast.definition.hit_range + PLAYER_PROJECTILE_COLLISION_RADIUS
    expected = sum(state._distance(near, p) <= reach for p in points)
    hits = state.resolve_due_monster_attacks(
        near, now=cast.due_at + 0.001, player_actor_id=1,
        finalize_area_attacks=True,
    )
    assert len(hits) == expected
    assert not state.pending_monster_attacks
    assert not state.pending_monster_projectile_attacks
