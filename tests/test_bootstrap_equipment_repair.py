"""Starter-gear repair on login keeps player items and never blocks the login."""

import copy

from dso_server.protocol.inventory_codec import (
    ItemInfo,
    decode_inventory_info,
    encode_bootstrap_inventory_info,
    repair_bootstrap_class_equipment,
)

# The loadout's newer pieces: amulet, belt and weapon mod (slots 10, 11, 12).
NEW_PIECES = (0xF00108, 0xF00109, 0xF0010A)
SHIELD = 0xF00106


def _older_save():
    """A warrior save from before the loadout gained its last three pieces."""

    inventory = decode_inventory_info(*encode_bootstrap_inventory_info(
        actor_id=1, class_name="warrior", item_level=60,
    ))
    inventory.items = [i for i in inventory.items if i.item_id not in NEW_PIECES]
    for item_id in NEW_PIECES:
        inventory.equipment_slots.pop(item_id, None)
    shield = next(i for i in inventory.items if i.item_id == SHIELD)
    shield.socketed_templates = ["gem_amethyst_i"] * 3
    shield.socket_count = 3
    return inventory


def _repair(inventory, overflowed=None):
    return repair_bootstrap_class_equipment(
        inventory, class_name="warrior", item_level=60, overflowed=overflowed,
    )


def test_a_grown_loadout_adds_its_new_pieces_and_keeps_the_rest():
    before = _older_save()
    result = _repair(copy.deepcopy(before))
    by_id = {i.item_id: i for i in result.items}
    # Existing starter items are untouched, gems included.
    for item in before.items:
        assert by_id[item.item_id] == item
    assert by_id[SHIELD].socketed_templates == ["gem_amethyst_i"] * 3
    assert result.equipment_slots[SHIELD] == before.equipment_slots[SHIELD]
    # The new pieces are added and equipped in their free slots.
    assert [result.equipment_slots[i] for i in NEW_PIECES] == [(10,), (11,), (12,)]


def test_a_full_bag_parks_displaced_starter_items_instead_of_failing():
    inventory = _older_save()
    # The player wears their own amulet and belt, and the bag is full.
    for item_id, slot in ((900001, 10), (900002, 11)):
        inventory.items.append(ItemInfo(item_id, template_id=f"acquired_{slot}"))
        inventory.equipment_slots[item_id] = (slot,)
    inventory.storage_size = len(inventory.storage_58)
    overflowed = []
    result = _repair(inventory, overflowed)
    # The acquired items keep their slots; the starter amulet and belt wait
    # in overflow, which returns them once a bag slot frees up.
    assert result.equipment_slots[900001] == (10,)
    assert result.equipment_slots[900002] == (11,)
    assert overflowed == [0xF00108, 0xF00109]
    assert [i.item_id for i in result.premium_inventory_overflow] == overflowed
    assert not set(overflowed) & {i.item_id for i in result.items}
    assert result.equipment_slots[0xF0010A] == (12,)
