"""Saved items keep their original keys whatever the attributes are called."""

from dataclasses import asdict

from dso_server.persistence.account_state import (
    AccountState,
    _inventory_from_document,
    _inventory_to_document,
    _item_from_document,
    _item_to_document,
    _state_to_document,
)
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.protocol.player_init_protocol import (
    CurrencyChanged,
    QuickbarInfo,
    TalentCategorySelection,
)


def item() -> ItemInfo:
    return ItemInfo(item_id=7, rarity=4, stack_vc_value=600, glyph_value=21291)


def test_saved_item_keeps_offset_keys_in_their_original_order():
    document = _item_to_document(item())

    assert (document["field_08"], document["field_30"], document["field_34"]) == (4, 600, 21291)
    assert not {"rarity", "stack_vc_value", "glyph_value"} & set(document)
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


def test_old_save_document_loads_into_the_named_attributes():
    document = _item_to_document(item())
    restored = _item_from_document(document)

    assert (restored.rarity, restored.stack_vc_value, restored.glyph_value) == (4, 600, 21291)


def test_save_without_the_keys_uses_the_defaults():
    document = _item_to_document(item())
    for key in ("field_08", "field_30", "field_34"):
        del document[key]
    restored = _item_from_document(document)

    assert (restored.rarity, restored.stack_vc_value, restored.glyph_value) == (0, 11, 0)


def test_enchantment_keys_are_left_alone():
    document = asdict(ItemInfo(item_id=7))
    document["enchantments"] = [{"value": 0.5, "kind": 3, "field_08": 2, "field_0c": 40, "template_id": "e"}]
    document["field_100"] = list(document["field_100"])

    restored = _item_from_document(document)

    assert (restored.enchantments[0].field_08, restored.enchantments[0].field_0c) == (2, 40)


def test_inventory_round_trip_keeps_values():
    inventory = InventoryInfo(items=[item()], storage_58={7: 0})

    restored = _inventory_from_document(_inventory_to_document(inventory))

    assert restored.items == inventory.items


def test_cms_document_uses_attribute_names_and_saves_do_not():
    state = AccountState(
        1, "a0200_kingscity", None, InventoryInfo(items=[item()], storage_58={7: 0}),
        CurrencyChanged(100, 200, (3, 0, 4), 500, False), 0, 10000,
        QuickbarInfo(2, 0), {}, (), None,
        talent_categories=tuple(TalentCategorySelection(0, (), ()) for _ in range(4)),
    )
    state.vendor_buyback = ((item(), 5),)

    saved = _state_to_document(state)
    shown = _state_to_document(state, item_document=asdict)

    assert saved["inventory"]["items"][0]["field_08"] == 4
    assert saved["vendor_buyback"][0]["item"]["field_30"] == 600
    assert shown["inventory"]["items"][0]["rarity"] == 4
    assert shown["vendor_buyback"][0]["item"]["stack_vc_value"] == 600
    assert "field_34" not in shown["inventory"]["items"][0]
