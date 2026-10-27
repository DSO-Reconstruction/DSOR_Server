"""Magic lamps and selective chests: SelectWishingWellMsg lifecycle."""

import pytest

from dso_server.protocol import seasonal_activity_protocol as proto
from dso_server.protocol.item_receipt_protocol import ITEM_RECEIVED_MESSAGE_TYPE
from dso_server.protocol.player_init_protocol import (
    CURRENCY_CHANGED_COMMAND,
    INVENTORY_INFO_COMMAND,
)
from dso_server.protocol.gameplay_protocol import MESSAGE_COMMAND
from dso_server.services.seasonal_activities import WISHING_WELL_KEY
from dso_server.tests.test_seasonal_protocol import seasonal  # noqa: F401


def wish(world, item_id, selections, amount=1):
    return world.dispatch(proto.encode_select_wishing_well(item_id, selections, amount))


def test_lamp_wish_consumes_one_lamp_and_grants_offer_group(seasonal):
    world = seasonal
    lamp = world.add_item("magic_lamp_1_filthy", 2)
    wish(world, lamp, (0,))
    # wishingwell_filthy_oil_lamp_wealth = next lamp + golden_goose_Lv50.
    assert world.count("magic_lamp_1_filthy") == 1
    assert world.count("magic_lamp_2_grimy") == 1
    assert world.count("golden_goose_Lv50") == 1
    batch = world.batches()[-1][1]
    ids = [command.command_id for command in batch]
    assert ids[0] == INVENTORY_INFO_COMMAND and ids[-1] == INVENTORY_INFO_COMMAND
    assert ids.count(MESSAGE_COMMAND) == 2
    assert all(
        command.value.message_type == ITEM_RECEIVED_MESSAGE_TYPE
        for command in batch if command.command_id == MESSAGE_COMMAND
    )
    saved = world.saved()
    assert saved.timed_offer_purchases[WISHING_WELL_KEY.format(template="magic_lamp_1_filthy")] == 1
    assert sum(item.amount for item in saved.inventory.items if item.template_id == "magic_lamp_2_grimy") == 1

    # The next lamp in the chain uses the power wish (unique item package).
    grimy = next(
        item.item_id for item in world.session.gameplay_inventory.items
        if item.template_id == "magic_lamp_2_grimy"
    )
    wish(world, grimy, (1,))
    assert world.count("magic_lamp_2_grimy") == 0
    assert world.count("magic_lamp_3_dusty") == 1
    assert world.count("unique_item_package") == 1


def test_consumed_lamp_cannot_be_replayed(seasonal):
    world = seasonal
    lamp = world.add_item("magic_lamp_3_dusty", 1)
    payload = proto.encode_select_wishing_well(lamp, (2,), 1)
    world.dispatch(payload)
    assert world.count("magic_lamp_4_clean") == 1
    assert world.count("horn_of_harvest_lv20") == 1
    world.dispatch(payload)
    assert world.count("magic_lamp_4_clean") == 1
    assert world.count("horn_of_harvest_lv20") == 1


def test_selective_chest_uses_slider_amount_and_credits_currency(seasonal):
    world = seasonal
    chest = world.add_item("mount_selection_chest_2", 3)
    andermant = world.session.gameplay_currency_state.andermant
    wish(world, chest, (4,), 2)  # money_rc,200000 twice
    assert world.count("mount_selection_chest_2") == 1
    assert world.session.gameplay_currency_state.andermant == andermant + 400000
    assert CURRENCY_CHANGED_COMMAND in [c.command_id for c in world.batches()[-1][1]]
    assert world.saved().currency.andermant == andermant + 400000

    material = world.add_item("frost_age_material_selection_chest", 2)
    wish(world, material, (1,), 2)
    assert world.count("item_ingredient_frost_age_gloves") == 2
    assert world.count("frost_age_material_selection_chest") == 0


@pytest.mark.parametrize("case", [
    "two-selections", "index", "amount", "lamp-amount", "foreign-item",
    "no-wish-row", "equipped", "level",
])
def test_invalid_selections_change_nothing(seasonal, case):
    world = seasonal
    lamp = world.add_item("magic_lamp_1_filthy", 2)
    chest = world.add_item("mount_selection_chest_2", 1)
    plain = world.add_item("golden_goose_feather", 1)
    item_id, selections, amount = chest, (0,), 1
    if case == "two-selections":
        selections = (0, 1)
    elif case == "index":
        selections = (5,)
    elif case == "amount":
        amount = 2
    elif case == "lamp-amount":
        item_id, amount = lamp, 2
    elif case == "foreign-item":
        item_id = chest + 1000
    elif case == "no-wish-row":
        item_id = plain
    elif case == "equipped":
        world.session.gameplay_inventory.storage_58.pop(chest)
        world.session.gameplay_inventory.equipment_slots[chest] = (37,)
    else:
        item_id = lamp
        world.session.gameplay_character.level = 10  # lamp offers need 20
    inventory_before = [
        (item.item_id, item.template_id, item.amount)
        for item in world.session.gameplay_inventory.items
    ]
    claims_before = dict(world.session.gameplay_timed_offer_purchases)
    currency_before = world.session.gameplay_currency_state
    wish(world, item_id, selections, amount)
    assert [
        (item.item_id, item.template_id, item.amount)
        for item in world.session.gameplay_inventory.items
    ] == inventory_before
    assert world.session.gameplay_timed_offer_purchases == claims_before
    assert world.session.gameplay_currency_state == currency_before
