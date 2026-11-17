"""Aimed PvP casts must reach hostile players, not only monsters.

`plan_entity_skill` sweeps the monster population, so a radius/point bullet
aimed at a player could come back empty even though the client resolved the
hit.  The fallback accepts the players inside the authored reach in front of
the caster.
"""

import math
from types import SimpleNamespace

import pytest

from dso_server.gameplay.combat_entities import (
    CombatEntity, EntitySkillPlan,
)
from dso_server.services import character_server  # noqa: F401  (facade)
from dso_server.services.base import CharacterService


class Frame:
    @staticmethod
    def wire_to_world(position):
        return (float(position[0]), float(position[1]), float(position[2]))


def combat():
    return SimpleNamespace(
        encounter=SimpleNamespace(coordinate_frame=Frame()),
    )


def plan(geometry):
    return EntitySkillPlan(geometry, ())


GEOMETRY = SimpleNamespace(
    attack_range=10.0, hit_range=10.0, projectile_range=0.0, impact_count=1,
)


def test_a_player_inside_the_reach_is_accepted_when_the_plan_is_empty():
    entities = (
        CombatEntity(11, (0.0, 0.0, -4.0), 0.75),
        CombatEntity(12, (0.0, 0.0, 40.0), 0.75),
    )
    skill = SimpleNamespace(aim_angle=0.0)  # faces -Z
    result = CharacterService._pvp_area_fallback_plan(
        combat(), plan(GEOMETRY), entities, skill=skill,
        source_position=(0.0, 0.0, 0.0),
    )
    assert [impact.target.actor_id for impact in result.impacts] == [11]


def test_players_behind_the_caster_are_ignored():
    entities = (CombatEntity(11, (0.0, 0.0, 5.0), 0.75),)
    skill = SimpleNamespace(aim_angle=0.0)
    result = CharacterService._pvp_area_fallback_plan(
        combat(), plan(GEOMETRY), entities, skill=skill,
        source_position=(0.0, 0.0, 0.0),
    )
    assert result.impacts == ()


def test_an_existing_plan_is_untouched():
    entities = (CombatEntity(11, (0.0, 0.0, -4.0), 0.75),)
    original = EntitySkillPlan(GEOMETRY, ("sentinel",))
    result = CharacterService._pvp_area_fallback_plan(
        combat(), original, entities, skill=SimpleNamespace(aim_angle=0.0),
        source_position=(0.0, 0.0, 0.0),
    )
    assert result is original
