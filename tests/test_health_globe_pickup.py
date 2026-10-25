"""A health globe is only taken by a wounded character.

At full health the globe stays on the ground instead of being used up for
nothing; auto pickup runs again on every move in range, so the character
collects it once health has dropped.
"""

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.commerce_protocol import (
    _new_item,
    load_purchase_item_definition,
)
from dso_server.protocol.world_loot_protocol import WorldLootDrop
from dso_server.tests import test_map_events as fixtures

GLOBE = "healthglobe_singleTarget"
FULL_GLOBE = "healthglobe_singleTarget_full"


@pytest.fixture
def world():
    case = fixtures.MapEventServiceTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def place_globe(session, template_id=GLOBE):
    definition = load_purchase_item_definition(DEFAULT_STATIC_DB_PATH, template_id)
    item = _new_item(definition, 0x00F20001, 1)
    item.field_100 = session.gameplay_monster_encounter.coordinate_frame.wire_to_world(
        session.gameplay_last_move.relative_position
    )
    drop = WorldLootDrop(item, definition.item_category, "All")
    session.gameplay_world_loot[int(item.item_id)] = drop
    return int(item.item_id)


def set_health(session, current):
    combat = session.gameplay_combat_state
    combat.player_health[combat.player_actor_id] = [
        current, combat.player_maximum_health,
    ]


def current_health(session):
    combat = session.gameplay_combat_state
    return combat.player_health[combat.player_actor_id][0]


def auto_pickup(world):
    s = world.session
    world.server._pickup_nearby_auto_loot(s, s.gameplay_last_move.relative_position)


@pytest.mark.parametrize("template_id", [GLOBE, FULL_GLOBE])
def test_full_health_leaves_the_globe_on_the_ground(world, template_id):
    s = world.session
    maximum = s.gameplay_combat_state.player_maximum_health
    set_health(s, maximum)
    item_id = place_globe(s, template_id)

    assert not world.server._pickup_world_item(s, item_id, automatic=True)
    assert not world.server._pickup_world_item(s, item_id, automatic=False)
    assert item_id in s.gameplay_world_loot
    assert current_health(s) == maximum


def test_wounded_character_takes_the_globe_and_heals(world):
    s = world.session
    maximum = s.gameplay_combat_state.player_maximum_health
    set_health(s, maximum // 2)
    item_id = place_globe(s)

    assert world.server._pickup_world_item(s, item_id, automatic=True)
    assert item_id not in s.gameplay_world_loot
    assert current_health(s) == min(maximum, maximum // 2 + round(maximum * 0.25))


def test_globe_left_at_full_health_is_collected_after_damage(world):
    s = world.session
    maximum = s.gameplay_combat_state.player_maximum_health
    set_health(s, maximum)
    item_id = place_globe(s)

    auto_pickup(world)
    assert item_id in s.gameplay_world_loot

    set_health(s, maximum - 10)
    auto_pickup(world)
    assert item_id not in s.gameplay_world_loot
    assert current_health(s) == maximum
