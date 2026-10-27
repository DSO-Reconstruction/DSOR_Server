"""Narrow arrows use the same monster contact envelope throughout a cast."""

from functools import lru_cache
import math
import sqlite3

import pytest

from dso_server.gameplay.combat_entities import CombatEntity
from dso_server.gameplay.local_combat_state import LocalCombatState
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


def test_ranger_arrow_is_cancelled_when_target_leaves_contact_envelope(combat):
    actor = combat.encounter.actor_ids[0]
    combat.monster_positions[actor] = (0.65, 2.0, 8.0)
    combat.monster_last_published_positions[actor] = combat.monster_positions[actor]
    (pending,) = shoot(combat)
    sidestepped = (3.0, 2.0, 8.0)
    combat.monster_positions[actor] = sidestepped
    combat.monster_last_published_positions[actor] = sidestepped
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
