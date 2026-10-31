"""Mount cast and ride times follow the Mount status group: cloaks and talent."""

import sqlite3

import pytest

from dso_server.gameplay.local_mount_state import BLACK_BEAR_MOUNT, with_duration_scale
from dso_server.gameplay.player_combat_stats import calculate_player_combat_stats
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import StatusEffectEntry
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.tests import test_admin_speed as fixtures


CLOAKS = [
    f"{class_name}_unique_backitem_{event}"
    for class_name in ("warrior", "ranger", "mage", "dwarf")
    for event in ("e_bos", "halloween")
]
CLOAK_SLOT = 7


def effect(resource_id, stacks=1):
    database = sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True)
    row = database.execute(
        "SELECT rowid - 1 FROM _Template_StatusEffect WHERE Id=?", (resource_id,),
    ).fetchone()
    return StatusEffectEntry(
        effect_id=row[0], field_04=1, field_0c=0, field_14=25, field_08=0, field_18=stacks - 1,
        field_10=25, field_1c=100, field_20=1, field_25=False, field_26=False,
        field_39=True, has_details=True, values=(0.0,) * 5,
    )


def talent(level):
    base = effect("trait_wisdom_all_faster_mount_cast_effect")
    if level == 1:
        return (base,)
    return (base, effect("trait_wisdom_all_faster_mount_cast_stackeffect", level - 1))


def wearing(template_id, *, equipped=True):
    inventory = InventoryInfo()
    inventory.items.append(ItemInfo(item_id=700, template_id=template_id, item_level=100))
    if equipped:
        inventory.equipment_slots[700] = (CLOAK_SLOT,)
    return inventory


def scale(inventory, sources=()):
    return calculate_player_combat_stats(
        DEFAULT_STATIC_DB_PATH, class_name="mage", level=100,
        inventory=inventory, modifier_sources=sources,
    ).mount_duration_scale


def timing(factor):
    mount = with_duration_scale(BLACK_BEAR_MOUNT, factor)
    return mount.cast_duration_ticks, mount.ride_duration_ticks


def test_a_plain_character_casts_for_the_items_time():
    assert scale(InventoryInfo()) == 1.0
    assert timing(1.0) == (88, 900_000)


@pytest.mark.parametrize("template_id", CLOAKS)
def test_each_cloak_halves_the_time(template_id):
    factor = scale(wearing(template_id))
    assert factor == pytest.approx(0.5)
    assert timing(factor) == (44, 450_000)


def test_a_cloak_in_the_bag_does_nothing():
    assert scale(wearing(CLOAKS[0], equipped=False)) == 1.0


@pytest.mark.parametrize("level,expected", [(1, 0.97), (2, 0.95), (15, 0.69)])
def test_the_talent_scales_with_its_level(level, expected):
    assert scale(InventoryInfo(), talent(level)) == pytest.approx(expected)


def test_the_full_talent_matches_the_retail_capture():
    assert timing(scale(InventoryInfo(), talent(15))) == (60, 621_000)


def test_cloak_and_talent_add_up():
    factor = scale(wearing(CLOAKS[0]), talent(15))
    assert factor == pytest.approx(0.19)
    assert timing(factor) == (17, 171_000)


def test_a_mount_cast_uses_the_shortened_time():
    case = fixtures.AdminSpeedTests(methodName="runTest")
    case.setUp()
    session = case.home
    session.gameplay_inventory = wearing(CLOAKS[0])
    try:
        case.gameplay_server._toggle_mount_definition(session, BLACK_BEAR_MOUNT, source="test")
        cast = session.gameplay_mount_cast
        assert cast.duration_ticks == 44
        assert cast.definition.ride_duration_ticks == 450_000
        assert session.gameplay_mount_cast_timer.interval == 1.75
    finally:
        if session.gameplay_mount_cast_timer is not None:
            session.gameplay_mount_cast_timer.cancel()
        case.doCleanups()
