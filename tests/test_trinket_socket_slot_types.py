"""Trinket sockets only take runes and jewels authored for that trinket."""

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.commerce_protocol import _new_item, load_purchase_item_definition
from dso_server.protocol.gem_protocol import insert_gem
from dso_server.protocol.inventory_codec import InventoryInfo


def _item(template, item_id):
    return _new_item(
        load_purchase_item_definition(DEFAULT_STATIC_DB_PATH, template),
        item_id, 1, 100,
    )


def _socketed(container_template, stone_template):
    stone = _item(stone_template, 72001)
    container = _item(container_template, 72002)
    container.socket_count = 1
    container.socketed_templates = [""]
    inventory = InventoryInfo(
        items=[stone, container],
        equipment_slots={container.item_id: (14,)},
    )
    return insert_gem(
        inventory, stone.item_id, container.item_id,
        static_database_path=DEFAULT_STATIC_DB_PATH,
    )


@pytest.mark.parametrize("rune", [
    "unique_gem_damage_04",
    "unique_gem_resistance_damage_block_04",
])
def test_trinket_runes_fit_rune_trinkets(rune):
    mutation = _socketed("all_base_trinket_rune_01", rune)
    assert mutation.equipment_item.socketed_templates == [rune]


@pytest.mark.parametrize("rune", [
    "unique_gem_crit_damage_03",        # Offhand,Weapons
    "unique_gem_2h_criticalstrike_03",  # TwoHandedWeapons
    "unique_gem_cooldown_03",           # Neck
])
def test_weapon_and_neck_runes_are_refused_by_rune_trinkets(rune):
    with pytest.raises(ValueError, match="RuneTrinket"):
        _socketed("all_base_trinket_rune_01", rune)


def test_trinket_jewels_still_fit_jewel_trinkets():
    mutation = _socketed("all_base_trinket_jewel_01", "jewel_summer_event_rare")
    assert mutation.equipment_item.socketed_templates == ["jewel_summer_event_rare"]
