"""Narrow arrows use the same monster contact envelope throughout a cast."""

from dataclasses import replace
from functools import lru_cache
import math
import sqlite3

import pytest

from dso_server.gameplay.combat_entities import CombatEntity
from dso_server.gameplay.local_combat_state import (
    LocalCombatState, _client_bullet_lateral_reach,
)
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.combat_protocol import BulletSkill


ARROWS = ("markshot", "mightyshot", "explosiveshot", "thorntrail")


@lru_cache(maxsize=None)
def skill_id(resource):
    with sqlite3.connect(f"{DEFAULT_STATIC_DB_PATH.as_uri()}?mode=ro", uri=True) as database:
        return database.execute(
            "SELECT rowid-1 FROM _Template_Skill WHERE Id=?", (resource,),
        ).fetchone()[0]


@pytest.fixture
def combat(monkeypatch):
    encounter = load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False,
    )
    state = LocalCombatState(encounter, player_actor_id=1, player_health=1000)
    # Synthetic positions test contact geometry independently of map obstacles.
    monkeypatch.setattr(state, "_bullet_static_reach", lambda _offset: None)
    return state


def shoot(combat, resource="markshot", *, explicit=False, visible=None):
    actor = combat.encounter.actor_ids[0]
    return combat.start_bullet_skill_hits(
        BulletSkill(0, skill_id(resource), 0.0, 2000, 321, (0.0, 0.0, 1.0, 0.0), 0),
        combat.encounter.coordinate_frame.world_to_wire((0.0, 2.0, 0.0)),
        source_actor_id=1, now=100.0, eligible_actor_ids=(actor,),
        explicit_target_actor_ids=(actor,) if explicit else (),
        visible_actor_positions=visible, player_damage_range=(10.0, 10.0),
    )


@pytest.mark.parametrize("resource", ARROWS)
@pytest.mark.parametrize("explicit", (False, True))
@pytest.mark.parametrize("delayed_movement", (False, True))
def test_ranger_arrow_edge_contact_is_scheduled_and_deals_damage(
        combat, resource, explicit, delayed_movement):
    actor = combat.encounter.actor_ids[0]
    geometry = combat._load_player_skill_geometry(skill_id(resource))
    # Inside the configured impact tolerance, but outside the old acquisition
    # capsule. A hit discarded at acquisition never reached impact validation.
    lateral = combat.monsters[actor].collision_radius + max(0.25, geometry.projectile_radius) + 0.10
    visible_position = (lateral, 2.0, 8.0)
    authoritative = (lateral + 3.0, 2.0, 8.0) if delayed_movement else visible_position
    combat.monster_positions[actor] = authoritative
    combat.monster_last_published_positions[actor] = authoritative
    combat.monster_health[actor] = 1000
    visible = {actor: visible_position}

    (pending,) = shoot(combat, resource, explicit=explicit, visible=visible)
    assert combat.resolve_runtime_player_hit(1, 321, 0, now=pending.due_at - 0.001) is None
    assert combat.runtime_player_hit_remaining(
        1, 321, 0, now=pending.due_at, visible_actor_positions=visible,
    ) == 0.0
    result = combat.resolve_runtime_player_hit(1, 321, 0, now=pending.due_at)
    assert result is not None and result.damage > 0
    assert combat.monster_health[actor] == 1000 - result.damage


@pytest.mark.parametrize("resource", ARROWS)
def test_ranger_arrow_still_misses_outside_contact_envelope(combat, resource):
    actor = combat.encounter.actor_ids[0]
    combat.monster_positions[actor] = (2.0, 2.0, 8.0)
    combat.monster_last_published_positions[actor] = combat.monster_positions[actor]
    assert not shoot(combat, resource)


@pytest.mark.parametrize("explicit", (False, True))
def test_ranger_arrow_cannot_hit_beyond_its_range(combat, explicit):
    actor = combat.encounter.actor_ids[0]
    geometry = combat._load_player_skill_geometry(skill_id("markshot"))
    combat.monster_positions[actor] = (0.0, 2.0, max(geometry.attack_range, geometry.projectile_range) + 2.0)
    combat.monster_last_published_positions[actor] = combat.monster_positions[actor]
    assert not shoot(combat, explicit=explicit)


@pytest.mark.parametrize("explicit", (False, True))
def test_ranger_arrow_edge_tolerance_does_not_bypass_walls(combat, monkeypatch, explicit):
    actor = combat.encounter.actor_ids[0]
    combat.monster_positions[actor] = (0.65, 2.0, 8.0)
    combat.monster_last_published_positions[actor] = combat.monster_positions[actor]
    monkeypatch.setattr(combat, "_bullet_static_reach", lambda _offset: (
        lambda start, end: min(4.0, math.dist(start, end))
    ))
    assert not shoot(combat, explicit=explicit)


def age_published_trail(combat, actor, seconds):
    """Pretend every transform published so far went out ``seconds`` ago."""
    trail = combat.monster_last_published_positions.trails[actor]
    for index, (stamp, position) in enumerate(trail):
        trail[index] = (stamp - seconds, position)


def walk(combat, actor, start, end, step=0.4):
    """Publish a walk from ``start`` to ``end`` in ordinary-sized steps."""
    count = max(1, math.ceil(math.dist(start, end) / step))
    for index in range(count + 1):
        position = tuple(a + (b - a) * index / count for a, b in zip(start, end))
        combat.monster_positions[actor] = position
        combat.monster_last_published_positions[actor] = position


def test_ranger_arrow_is_cancelled_when_target_leaves_contact_envelope(combat):
    actor = combat.encounter.actor_ids[0]
    combat.monster_positions[actor] = (0.65, 2.0, 8.0)
    combat.monster_last_published_positions[actor] = combat.monster_positions[actor]
    (pending,) = shoot(combat)
    sidestepped = (3.0, 2.0, 8.0)
    combat.monster_positions[actor] = sidestepped
    combat.monster_last_published_positions[actor] = sidestepped
    # Long enough ago that no client still draws it in the arrow's path.
    age_published_trail(combat, actor, 1.0)
    assert combat.runtime_player_hit_remaining(
        1, 321, 0, now=pending.due_at, visible_actor_positions={actor: sidestepped},
    ) is None
    assert not combat.pending_runtime_player_hits


def test_monster_contact_tolerance_preserves_other_entity_capsules(combat):
    actor = combat.encounter.actor_ids[0]
    combat.monster_positions[actor] = (0.65, 2.0, 8.0)
    combat.monster_last_published_positions[actor] = combat.monster_positions[actor]
    # A PvP player or summon retains its adapter's 0.75 radius. It must not
    # consume the single arrow before the monster when it is outside that ray.
    other_entity = CombatEntity(999999, (1.10, 2.0, 4.0), 0.75)
    plan = combat.plan_entity_skill_hits(
        skill_index=skill_id("markshot"), projectile=True,
        player_position=combat.encounter.coordinate_frame.world_to_wire((0.0, 2.0, 0.0)),
        direction=(0.0, 0.0, 1.0, 0.0), eligible_actor_ids=(actor,),
        additional_entities=(other_entity,),
    )
    assert [impact.target.actor_id for impact in plan.impacts] == [actor]


def test_legacy_arrow_acquisition_uses_the_same_contact_tolerance(combat):
    actor, other = combat.encounter.actor_ids[:2]
    combat.monster_positions[actor] = (0.65, 2.0, 8.0)
    combat.monster_last_published_positions[actor] = combat.monster_positions[actor]
    combat.monster_positions[other] = (20.0, 2.0, 8.0)
    combat.monster_last_published_positions[other] = combat.monster_positions[other]
    pending = combat.start_bullet_skill(
        BulletSkill(0, skill_id("markshot"), 0.0, 2000, 321, (0.0, 0.0, 1.0, 0.0), 0),
        combat.encounter.coordinate_frame.world_to_wire((0.0, 2.0, 0.0)), now=100.0,
    )
    assert pending is not None and pending.target_actor_id == actor


@pytest.mark.parametrize("resource", ("markshot", "mightyshot"))
def test_ranger_arrow_hits_a_monster_still_drawn_on_its_recent_path(combat, resource):
    # The player aimed at the monster where the client drew it; it has since
    # walked out of the arrow's lane on the server.
    actor = combat.encounter.actor_ids[0]
    combat.monster_health[actor] = 1000
    walk(combat, actor, (0.0, 2.0, 8.0), (3.5, 2.0, 8.0))
    (pending,) = shoot(combat, resource)
    assert combat.runtime_player_hit_remaining(1, 321, 0, now=pending.due_at) == 0.0
    result = combat.resolve_runtime_player_hit(1, 321, 0, now=pending.due_at)
    assert result is not None and result.damage > 0


def test_ranger_arrow_ignores_a_path_older_than_the_lag_window(combat):
    actor = combat.encounter.actor_ids[0]
    walk(combat, actor, (0.0, 2.0, 8.0), (3.5, 2.0, 8.0))
    age_published_trail(combat, actor, 1.0)
    assert not shoot(combat)


def test_ranger_arrow_path_does_not_sweep_across_a_teleport(combat):
    actor = combat.encounter.actor_ids[0]
    for position in ((-6.0, 2.0, 8.0), (6.0, 2.0, 8.0)):
        combat.monster_positions[actor] = position
        combat.monster_last_published_positions[actor] = position
    assert not shoot(combat)


def scatter_shot(combat, actor):
    frame = combat.encounter.coordinate_frame
    return combat.start_spatial_skill_hits(
        skill_index=skill_id("multishot"), cast_id=321, server_tick=2000,
        player_position=frame.world_to_wire((0.0, 2.0, 0.0)), source_actor_id=1,
        eligible_actor_ids=(actor,), now=100.0, player_damage_range=(10.0, 10.0),
    )


def test_scatter_shot_hits_a_monster_still_drawn_inside_its_cone(combat):
    # Aim angle 0 faces -z. The monster crossed the cone and now stands
    # beside it, still inside the skill's reach.
    actor = combat.encounter.actor_ids[0]
    combat.monster_health[actor] = 1000
    walk(combat, actor, (2.0, 2.0, -6.0), (8.0, 2.0, -6.0))
    (pending,) = scatter_shot(combat, actor)
    result = combat.resolve_runtime_player_hit(1, 321, 0, now=pending.due_at)
    assert result is not None and result.damage > 0


def test_scatter_shot_still_misses_a_monster_that_left_its_cone_long_ago(combat):
    actor = combat.encounter.actor_ids[0]
    walk(combat, actor, (2.0, 2.0, -6.0), (8.0, 2.0, -6.0))
    age_published_trail(combat, actor, 1.0)
    assert not scatter_shot(combat, actor)


@pytest.mark.parametrize("radius,reach", [
    # BulletRadius + ActorShapeId radius against Velocity 21 * 0.04: the
    # widest miss the client's own sweep still stops the arrow on.
    (0.2 + 0.525, 1.506), (0.25 + 0.525, 1.582),
    (0.2 + 1.053, 2.093), (0.2 + 1.581, 2.621),
])
def test_client_bullet_sweep_reach_matches_the_native_test(radius, reach):
    assert _client_bullet_lateral_reach(radius, 0.84) == pytest.approx(reach, abs=0.01)


def place(combat, actor, position):
    """A standing monster, published where it stands."""
    combat.monster_positions[actor] = position
    combat.monster_last_published_positions[actor] = position
    age_published_trail(combat, actor, 1.0)


def medium_monster(combat, actor):
    # ActorShapeId "medium" (1.053) with the 0.3 capsule most monsters carry.
    combat.monsters[actor] = replace(
        combat.monsters[actor], collision_radius=0.3, target_radius=1.053,
    )


@pytest.mark.parametrize("resource", ("markshot", "mightyshot"))
def test_ranger_arrow_hits_where_the_client_stops_it_on_a_medium_monster(combat, resource):
    actor = combat.encounter.actor_ids[0]
    medium_monster(combat, actor)
    combat.monster_health[actor] = 1000
    place(combat, actor, (1.9, 2.0, 8.0))
    (pending,) = shoot(combat, resource)
    assert combat.runtime_player_hit_remaining(1, 321, 0, now=pending.due_at) == 0.0
    result = combat.resolve_runtime_player_hit(1, 321, 0, now=pending.due_at)
    assert result is not None and result.damage > 0


def test_ranger_arrow_still_misses_beyond_the_client_sweep(combat):
    actor = combat.encounter.actor_ids[0]
    medium_monster(combat, actor)
    place(combat, actor, (2.6, 2.0, 8.0))
    assert not shoot(combat)


def test_scatter_shot_cone_uses_the_monster_shape_radius(combat):
    # 6 m ahead the cone is 3.32 m wide each side; a medium monster 4 m
    # aside overlaps it by its 1.05 m shape, as the client's cone test sees.
    actor = combat.encounter.actor_ids[0]
    medium_monster(combat, actor)
    combat.monster_health[actor] = 1000
    place(combat, actor, (4.0, 2.0, -6.0))
    (pending,) = scatter_shot(combat, actor)
    result = combat.resolve_runtime_player_hit(1, 321, 0, now=pending.due_at)
    assert result is not None and result.damage > 0
    place(combat, actor, (5.0, 2.0, -6.0))
    assert not scatter_shot(combat, actor)
