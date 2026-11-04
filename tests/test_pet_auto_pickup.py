"""Pets with ``AutoPickupModifier`` collect the loot they advertise."""

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

GEM = "gem_amethyst_a"
JEWEL = "jewel_armor_critRating_damage_common"
RUNE = "unique_gem_hitpoints_00"
MYSTIC_CUBE = "mystic_cube"


@pytest.fixture
def world():
    case = fixtures.MapEventServiceTests(methodName="runTest")
    case.setUp()
    try:
        s = case.session
        s.gameplay_inventory = InventoryInfo()
        s.gameplay_next_item_id = 100
        s.gameplay_currency_state = CurrencyChanged(0, 0, (0, 0, 0), 0, False)
        yield case
    finally:
        case.doCleanups()


def place_drop(session, template_id, item_id, distance=0.0):
    definition = load_purchase_item_definition(DEFAULT_STATIC_DB_PATH, template_id)
    item = _new_item(definition, item_id, 1)
    x, y, z = session.gameplay_monster_encounter.coordinate_frame.wire_to_world(
        session.gameplay_last_move.relative_position
    )
    item.ground_position = (x + distance, y, z)
    session.gameplay_world_loot[int(item.item_id)] = WorldLootDrop(
        item, definition.item_category, "None"
    )
    return int(item.item_id)


def collect(world):
    s = world.session
    world.server._pickup_nearby_auto_loot(
        s, s.gameplay_last_move.relative_position
    )
    return s.gameplay_world_loot


def test_a_collector_pet_picks_up_gems_jewels_and_runes(world):
    s = world.session
    ids = [
        place_drop(s, GEM, 0x00F50001),
        place_drop(s, JEWEL, 0x00F50002),
        place_drop(s, RUNE, 0x00F50003),
    ]

    # Without a pet they stay on the ground: none of them auto-pick up.
    loot = collect(world)
    assert all(item_id in loot for item_id in ids)

    s.gameplay_active_pet = "pet_gnome_gem"
    loot = collect(world)
    assert all(item_id not in loot for item_id in ids)


def test_the_gem_pet_leaves_jewels_and_runes(world):
    s = world.session
    s.gameplay_active_pet = "pet_wisp_purple"
    gem = place_drop(s, GEM, 0x00F50004)
    jewel = place_drop(s, JEWEL, 0x00F50005)
    rune = place_drop(s, RUNE, 0x00F50006)

    loot = collect(world)
    assert gem not in loot
    assert jewel in loot
    assert rune in loot


def test_the_dog_also_collects_loot_boxes_like_mystic_cubes(world):
    s = world.session
    s.gameplay_active_pet = "pet_dog_summer"
    ids = [
        place_drop(s, MYSTIC_CUBE, 0x00F50007),
        place_drop(s, GEM, 0x00F50008),
    ]

    loot = collect(world)
    assert all(item_id not in loot for item_id in ids)


def test_a_collecting_pet_reaches_further_than_the_normal_auto_pickup(world):
    s = world.session
    s.gameplay_active_pet = "pet_gnome_gem"
    # The pet's 4.5 radius takes a gem at 4.4, outside the ordinary 2.5.
    near = place_drop(s, GEM, 0x00F50010, distance=4.4)
    beyond = place_drop(s, GEM, 0x00F50011, distance=5.9)
    loot = collect(world)
    assert near not in loot
    assert beyond in loot


def test_a_pet_without_the_modifier_collects_nothing(world):
    s = world.session
    s.gameplay_active_pet = "pet_jabbax"
    item_id = place_drop(s, GEM, 0x00F50009)

    assert item_id in collect(world)
