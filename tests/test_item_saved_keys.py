"""Saved items keep their original keys whatever the attributes are called."""

import json

from dso_server.persistence.account_state import (
    AccountState,
    _inventory_from_document,
    _inventory_to_document,
    _item_from_document,
    _item_to_document,
    _state_to_document,
)
from dso_server.protocol.inventory_codec import InventoryInfo, ItemEnchantmentInfo, ItemInfo
from dso_server.protocol.player_init_protocol import (
    CurrencyChanged,
    QuickbarInfo,
    TalentCategorySelection,
)


def item() -> ItemInfo:
    return ItemInfo(
        item_id=7, rarity=4, stack_vc_value=600, glyph_value=21291,
        dye_argb=0xD1050505, dye_color_id=107, dye_id="fk_Black_008",
        needs_identification=True, ground_position=(1.0, 2.0, 3.0),
        appearance="mage_rh_staff_02", base_enchantment_tier=15,
        equipment_skin="mage_basic_torso_06",
        enchantments=[ItemEnchantmentInfo(0.5, 3, 16, 40, "enchant")],
    )


def test_saved_item_keeps_offset_keys_in_their_original_order():
    document = _item_to_document(item())

    assert (document["field_08"], document["field_30"], document["field_34"]) == (4, 600, 21291)
    assert (document["field_20"], document["field_1c"], document["field_28"]) == (
        0xD1050505, 107, "fk_Black_008",
    )
    assert (document["field_3c"], document["field_100"]) == (True, (1.0, 2.0, 3.0))
    assert (document["field_80"], document["field_38"], document["field_f8"]) == (
        "mage_rh_staff_02", 15, "mage_basic_torso_06",
    )
    assert list(document)[:4] == ["item_id", "field_04", "amount", "field_08"]


def test_saved_item_key_set_is_pinned():
    # A rename missing from _ITEM_SAVED_KEYS must fail here: an unknown key makes
    # the store drop the whole character with only a warning.
    assert list(_item_to_document(item())) == [
        "item_id", "field_04", "amount", "field_08", "socket_count", "field_11", "field_12",
        "field_18", "field_20", "field_1c", "field_28", "item_level", "field_30", "field_34",
        "field_3c", "field_3d", "template_id", "field_88", "field_100", "field_80", "field_38",
        "field_f8", "field_3e", "enchantments", "socketed_templates", "created_at", "quest_id",
    ]


def test_saved_enchantment_keys_are_pinned():
    assert _item_to_document(item())["enchantments"] == [
        {"value": 0.5, "kind": 3, "field_08": 16, "field_0c": 40, "template_id": "enchant"},
    ]


def test_saved_inventory_key_set_is_pinned():
    assert list(_inventory_to_document(InventoryInfo())) == [
        "items", "premium_inventory_overflow", "premium_storage_overflow", "equipment_slots",
        "storage_58", "storage_80", "storage_a8", "storage_d0", "field_f8", "field_118",
        "changed_item_ids", "field_158", "field_180", "field_1a0", "storage_upgrade",
        "bank_storage_upgrade", "storage_size", "bank_storage_size", "extra_bank_storage_size",
        "extra_storage_size", "health_points", "secondary_resource", "field_1e0", "actor_id",
    ]


def test_old_save_document_loads_into_the_named_attributes():
    document = json.loads(json.dumps(_item_to_document(item())))

    assert _item_from_document(document) == item()


def test_save_without_the_optional_keys_uses_the_defaults():
    document = _item_to_document(item())
    for key in ("field_08", "field_1c", "field_20", "field_28", "field_30", "field_34",
                "field_38", "field_3c", "field_80", "field_f8"):
        del document[key]
    restored = _item_from_document(document)

    assert (restored.rarity, restored.stack_vc_value, restored.glyph_value) == (0, 11, 0)
    assert (restored.dye_argb, restored.dye_color_id, restored.dye_id) == (0, 0, "none")
    assert (restored.needs_identification, restored.appearance) == (False, "")
    assert (restored.base_enchantment_tier, restored.equipment_skin) == (7, "")


def inventory() -> InventoryInfo:
    return InventoryInfo(
        items=[item(), ItemInfo(item_id=8), ItemInfo(item_id=9), ItemInfo(item_id=10), ItemInfo(item_id=11)],
        storage_slots={7: 0}, extra_storage_slots={8: 1}, bank_storage_slots={9: 2},
        extra_bank_storage_slots={10: 3}, overflow_storage_item_ids=[21],
        invisible_storage_item_ids=[22], item_id_replacements={31: 32},
        ingredient_bag_item_ids=[11], ingredient_bag_templates=["ore_ingredient_rarity_01"],
    )


def test_inventory_round_trip_keeps_values():
    original = inventory()

    restored = _inventory_from_document(json.loads(json.dumps(_inventory_to_document(original))))

    assert restored.items == original.items
    assert restored.storage_slots == original.storage_slots
    assert restored.extra_storage_slots == original.extra_storage_slots
    assert restored.bank_storage_slots == original.bank_storage_slots
    assert restored.extra_bank_storage_slots == original.extra_bank_storage_slots
    assert restored.overflow_storage_item_ids == original.overflow_storage_item_ids
    assert restored.invisible_storage_item_ids == original.invisible_storage_item_ids
    assert restored.item_id_replacements == original.item_id_replacements
    assert restored.ingredient_bag_item_ids == original.ingredient_bag_item_ids
    assert restored.ingredient_bag_templates == original.ingredient_bag_templates


def test_saved_inventory_keeps_offset_keys_and_item_skin_stays_separate():
    document = _inventory_to_document(inventory())

    assert (document["storage_58"], document["storage_80"]) == ({7: 0}, {8: 1})
    assert (document["storage_a8"], document["storage_d0"]) == ({9: 2}, {10: 3})
    assert (document["field_f8"], document["field_118"]) == ([21], [22])
    assert (document["field_158"], document["field_180"]) == ({31: 32}, [11])
    assert document["field_1a0"] == ["ore_ingredient_rarity_01"]
    assert document["items"][0]["field_f8"] == "mage_basic_torso_06"


def test_cms_document_uses_attribute_names_and_saves_do_not():
    state = AccountState(
        1, "a0200_kingscity", None, inventory(),
        CurrencyChanged(100, 200, (3, 0, 4), 500, False), 0, 10000,
        QuickbarInfo(2, 0), {}, (), None,
        talent_categories=tuple(TalentCategorySelection(0, (), ()) for _ in range(4)),
    )
    state.vendor_buyback = ((item(), 5),)

    saved = _state_to_document(state)
    shown = _state_to_document(state, attribute_names=True)

    assert saved["inventory"]["items"][0]["field_08"] == 4
    assert saved["inventory"]["items"][0]["enchantments"][0]["field_0c"] == 40
    assert saved["vendor_buyback"][0]["item"]["field_30"] == 600
    assert shown["inventory"]["items"][0]["rarity"] == 4
    assert shown["inventory"]["items"][0]["enchantments"][0]["origin_item_level"] == 40
    assert shown["vendor_buyback"][0]["item"]["stack_vc_value"] == 600
    assert "field_34" not in shown["inventory"]["items"][0]
    assert saved["inventory"]["storage_58"] == {7: 0}
    assert shown["inventory"]["storage_slots"] == {7: 0}
    assert shown["inventory"]["ingredient_bag_item_ids"] == [11]
    assert "storage_58" not in shown["inventory"] and "field_180" not in shown["inventory"]
