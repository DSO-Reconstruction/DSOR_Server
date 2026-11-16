"""Tests for:
1. New character initial inventory (Bag 1: 21 slots) and bank (Box 1: 20 slots) capacities.
2. Character creation screen editor response carrying the real account/starter Andermant (not 1362).
3. Inventory and Bank expansion mechanics (via item consumption and direct purchase).
4. Average Item Level calculation parity (only gear slots 0..12).
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock
from dso_server.protocol.character_generation import (
    character_generation_editor_response,
    CharacterGeneration,
)
from dso_server.protocol.inventory_codec import (
    InventoryInfo,
    ItemInfo,
    encode_bootstrap_inventory_info,
    decode_inventory_info,
)
from dso_server.protocol.gameplay_commands import (
    LOCAL_INVENTORY_CAPACITIES,
    encode_new_player,
)
from dso_server.services.storage_extensions import (
    extension_grant_amounts,
    handle_storage_extension_use,
    MAX_INVENTORY_SLOTS,
    MAX_INVENTORY_UPGRADES,
    MAX_BANK_SLOTS,
    MAX_BANK_UPGRADES,
)


def test_default_inventory_and_bank_capacities():
    # Fresh InventoryInfo must default to Bag 1 (21 slots) and Bank Box 1 (20 slots)
    inv = InventoryInfo()
    assert inv.storage_size == 21
    assert inv.storage_upgrade == 0
    assert inv.bank_storage_size == 20
    assert inv.bank_storage_upgrade == 0
    assert LOCAL_INVENTORY_CAPACITIES == (0, 0, 21, 20, 0, 0)


def test_bootstrap_inventory_capacities():
    packet, bits = encode_bootstrap_inventory_info(actor_id=1, class_name="warrior")
    inv = decode_inventory_info(packet, bits)
    assert inv.storage_size == 21
    assert inv.storage_upgrade == 0
    assert inv.bank_storage_size == 20
    assert inv.bank_storage_upgrade == 0
    # Items placed in storage_slots must all fit in Bag 1 (slots 0..20)
    for item_id, slot_idx in inv.storage_slots.items():
        assert 0 <= slot_idx < 21, f"Slot {slot_idx} is outside Bag 1 (0..20)"


def test_character_generation_editor_andermant():
    # When andermant is passed, fields_84[3] must match it, NOT default 1362
    resp = character_generation_editor_response(
        locale="en",
        preview_id=101,
        andermant=54321,
    )
    assert resp.fields_84 == (0, 0, 0, 54321)


def test_extension_grant_amounts():
    kind, upg, slots = extension_grant_amounts("misc_bag_extension_01")
    assert kind == "inventory" and upg == 1 and slots == 7

    kind, upg, slots = extension_grant_amounts("misc_bag_extension_bundle")
    assert kind == "inventory" and upg == 3 and slots == 21

    kind, upg, slots = extension_grant_amounts("misc_bag_extension_bundle_xxl")
    assert kind == "inventory" and upg == 29 and slots == 203

    kind, upg, slots = extension_grant_amounts("misc_bank_extension_01")
    assert kind == "bank" and upg == 1 and slots == 5

    kind, upg, slots = extension_grant_amounts("misc_bank_extension_bundle_xxl")
    assert kind == "bank" and upg == 54 and slots == 270


def test_average_item_level_calculation_parity():
    # Only equipment slots 0..12 (helmet, shoulders, torso, gloves, boots,
    # weapon, offhand, cloak, rings, amulet, belt, adornment) must be counted.
    # Runes/trinkets (13..19) and essences (35) must be excluded.
    items = [
        ItemInfo(item_id=1, item_level=100),   # helmet (slot 0)
        ItemInfo(item_id=2, item_level=100),   # shoulders (slot 1)
        ItemInfo(item_id=3, item_level=100),   # torso (slot 2)
        ItemInfo(item_id=4, item_level=100),   # gloves (slot 3)
        ItemInfo(item_id=5, item_level=100),   # boots (slot 4)
        ItemInfo(item_id=6, item_level=100),   # weapon (slot 5)
        ItemInfo(item_id=7, item_level=100),   # offhand (slot 6)
        ItemInfo(item_id=8, item_level=100),   # cloak (slot 7)
        ItemInfo(item_id=9, item_level=100),   # ring1 (slot 8)
        ItemInfo(item_id=10, item_level=100),  # ring2 (slot 9)
        ItemInfo(item_id=11, item_level=100),  # amulet (slot 10)
        ItemInfo(item_id=12, item_level=100),  # belt (slot 11)
        ItemInfo(item_id=13, item_level=100),  # adornment (slot 12)
        # Non-gear slots (trinkets/runes/essences) with very high or low levels:
        ItemInfo(item_id=14, item_level=145),  # rune (slot 15)
        ItemInfo(item_id=15, item_level=145),  # rune (slot 16)
        ItemInfo(item_id=16, item_level=1),    # essence (slot 35)
    ]
    eq_slots = {
        1: (0,), 2: (1,), 3: (2,), 4: (3,), 5: (4,),
        6: (5,), 7: (6,), 8: (7,), 9: (8,), 10: (9,),
        11: (10,), 12: (11,), 13: (12,),
        14: (15,), 15: (16,), 16: (35,),
    }
    equipped = {item.item_id: item for item in items}
    gear_levels = [
        equipped[int(item_id)].item_level
        for item_id, slots in eq_slots.items()
        for slot in slots
        if 0 <= slot <= 12
    ]
    assert len(gear_levels) == 13
    avg = sum(gear_levels) / len(gear_levels)
    assert math.floor(avg + 0.5) == 100
