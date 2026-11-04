"""Ores and herbs dropped by monsters reach a little further when collected."""

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.commerce_protocol import (
    _new_item,
    load_purchase_item_definition,
)
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.player_init_protocol import CurrencyChanged
from dso_server.protocol.world_loot_protocol import WorldLootDrop
from dso_server.tests import test_map_events as fixtures

ORE = "ore_ingredient_rarity_01"
HERB = "herb_ingredient_defensive_rarity_01"
MATERI = "materi_fragment_01"


@pytest.fixture
def world():
    case = fixtures.MapEventServiceTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def place_drop(session, template_id, item_id, distance):
    definition = load_purchase_item_definition(DEFAULT_STATIC_DB_PATH, template_id)
    item = _new_item(definition, item_id, 1)
    x, y, z = session.gameplay_monster_encounter.coordinate_frame.wire_to_world(
        session.gameplay_last_move.relative_position
    )
    item.ground_position = (x + distance, y, z)
    session.gameplay_world_loot[int(item.item_id)] = WorldLootDrop(
        item, definition.item_category, "All"
    )
    return int(item.item_id)


@pytest.mark.parametrize("template_id", [ORE, HERB])
def test_ore_and_herb_drops_are_collected_from_three_metres(world, template_id):
    s = world.session
    s.gameplay_currency_state = CurrencyChanged(0, 0, (0, 0, 0), 0, False)
    s.gameplay_inventory = InventoryInfo()
    s.gameplay_next_item_id = 100
    item_id = place_drop(s, template_id, 0x00F40001, 3.0)

    world.server._pickup_nearby_auto_loot(
        s, s.gameplay_last_move.relative_position
    )

    assert item_id not in s.gameplay_world_loot
    assert template_id in {
        item.template_id for item in s.gameplay_inventory.items
    }


def test_an_ore_beyond_the_ingredient_radius_is_left_on_the_ground(world):
    s = world.session
    item_id = place_drop(s, ORE, 0x00F40002, 4.0)

    world.server._pickup_nearby_auto_loot(
        s, s.gameplay_last_move.relative_position
    )

    assert item_id in s.gameplay_world_loot


def test_other_auto_pickups_keep_the_shorter_range(world):
    s = world.session
    s.gameplay_currency_state = CurrencyChanged(0, 0, (0, 0, 0), 0, False)
    s.gameplay_game_currencies = {MATERI: 100}
    item_id = place_drop(s, MATERI, 0x00F40003, 3.0)

    world.server._pickup_nearby_auto_loot(
        s, s.gameplay_last_move.relative_position
    )

    assert item_id in s.gameplay_world_loot
    assert s.gameplay_game_currencies[MATERI] == 100
