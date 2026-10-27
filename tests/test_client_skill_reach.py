"""Player area, angle and actor skills reach as far as the client's own queries.

SkillUpdateTask (dro_client64) measures monsters by their ActorShapeId radius
(small 0.525, medium 1.053), not the 0.3 physics capsule, and the actor test
subtracts the caster's radius too.
"""

from dataclasses import replace
import sqlite3

import pytest

from dso_server.gameplay.combat_entities import CombatEntity, plan_entity_skill
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.tests import test_local_combat_state as local_fixtures

MEDIUM = 1.0530432089900206
SMALL = 0.5249761899362675
SOURCE = 98305


@pytest.fixture
def fixture():
    value = local_fixtures.LocalCombatStateTests()
    value.setUp()
    return value


def skill(state, resource):
    with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as database:
        index = database.execute(
            "SELECT rowid-1 FROM _Template_Skill WHERE Id=?", (resource,),
        ).fetchone()[0]
    return index, state._load_player_skill_geometry(index)


def medium(actor_id, x, y=0.0, z=0.0):
    return CombatEntity(actor_id, (x, y, z), 0.3, target_radius=MEDIUM)


def hit_ids(geometry, entities, *, point=None, projectile=False, **kwargs):
    plan = plan_entity_skill(
        geometry, entities, origin=(0.0, 0.0, 0.0), direction=(1.0, 0.0, 0.0, 0.0),
        projectile=projectile, point=point, caster_radius=SMALL, **kwargs)
    return {impact.target.actor_id for impact in plan.impacts}


@pytest.mark.parametrize("resource", ["frostnova", "bloody360", "earthquake", "ultimate_windmill_off"])
def test_radius_skills_reach_hit_range_plus_actor_shape(fixture, resource):
    _, geometry = skill(fixture.state, resource)
    edge = geometry.hit_range + MEDIUM
    assert hit_ids(geometry, (medium(1, edge - 0.05), medium(2, edge + 0.05))) == {1}
    # The native query ignores actors more than 10 m above its centre.
    assert hit_ids(geometry, (medium(3, 0.5, y=10.5),)) == set()


@pytest.mark.parametrize("resource", ["mightyswing", "mightybash", "Flamethrower", "stab"])
def test_angle_skills_reach_hit_range_plus_actor_shape(fixture, resource):
    _, geometry = skill(fixture.state, resource)
    edge = geometry.hit_range + MEDIUM
    assert hit_ids(geometry, (medium(1, edge - 0.05), medium(2, edge + 0.05))) == {1}


@pytest.mark.parametrize("resource", ["angrystrike", "mightystab", "zero_slicedice"])
def test_actor_melee_subtracts_both_actor_shapes(fixture, resource):
    _, geometry = skill(fixture.state, resource)
    edge = geometry.attack_range + MEDIUM + SMALL
    inside, outside = medium(1, edge - 0.05), medium(2, edge + 0.05)
    assert hit_ids(geometry, (inside,), explicit_targets=(1,)) == {1}
    assert hit_ids(geometry, (outside,), explicit_targets=(2,)) == set()


def test_point_bullet_explosion_uses_actor_shape(fixture):
    _, geometry = skill(fixture.state, "frozenorb")
    geometry = replace(geometry, location_status_effects=(), location_visual_effects=(),
                       bullet_death_effects=())
    radius = max(geometry.hit_range, geometry.impact_radius)
    point = (8.0, 0.0, 0.0)
    near = medium(1, 8.0 + radius + MEDIUM - 0.05)
    far = medium(2, 8.0 + radius + MEDIUM + 0.05)
    assert hit_ids(geometry, (near, far), point=point, projectile=True) == {1}


def test_fireball_burns_the_monsters_around_its_victim(fixture):
    _, geometry = skill(fixture.state, "fireball")
    geometry = replace(geometry, location_status_effects=(), location_visual_effects=(),
                       bullet_death_effects=())
    edge = geometry.impact_radius + MEDIUM
    victim = medium(1, 10.0)
    beside = medium(2, 10.0, z=edge - 0.05)
    outside = medium(3, 10.0, z=-(edge + 0.05))
    assert hit_ids(geometry, (victim, beside, outside), projectile=True) == {1, 2}


@pytest.mark.parametrize("resource,offset", [
    ("frostnova", None), ("angrystrike", SMALL),
])
def test_accepted_area_and_actor_hits_survive_the_impact_recheck(fixture, resource, offset):
    state = fixture.state
    index, geometry = skill(state, resource)
    actor, *others = fixture.encounter.actor_ids
    for other in others:
        state.monster_positions[other] = (1000.0, 0.0, 1000.0)
    state.monsters[actor] = replace(state.monsters[actor], collision_radius=0.3,
                                    target_radius=MEDIUM)
    player = (25.0, 0.0, 50.0)
    reach = (geometry.attack_range if offset else geometry.hit_range) + MEDIUM + (offset or 0.0)
    state.monster_positions[actor] = (player[0] + reach - 0.1, 0.0, player[2])
    kwargs = dict(skill_index=index, player_position=fixture._wire(player),
                  aim_angle=0.0, explicit_target_actor_ids=(actor,) if offset else ())
    plan = state.plan_entity_skill_hits(projectile=False, now=100.0, **kwargs)
    assert [impact.target.actor_id for impact in plan.impacts] == [actor]
    pending = state.start_spatial_skill_hits(
        hit_plan=plan, cast_id=77, server_tick=1000, source_actor_id=SOURCE,
        now=100.0, player_damage_range=(10.0, 10.0), **kwargs)
    assert len(pending) == 1
    resolution = state.resolve_runtime_player_hit(
        SOURCE, 77, pending[0].impact_index, now=pending[0].due_at)
    assert resolution is not None and resolution.damage > 0
