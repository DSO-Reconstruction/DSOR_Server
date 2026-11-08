"""MergeStack (inventory operations 0x0d..0x10): a stack dropped on another
joins it up to MaxStackSize; S's server ignored it, so stacks never combined."""

import pytest

from dso_server.protocol.gameplay_protocol import MERGE_STACK_OPERATIONS
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.protocol.inventory_economy_protocol import InventoryMutationBuilder


def _inventory():
    a = ItemInfo(item_id=10, template_id="ammunition_damage_epic", amount=30, stack_vc_value=300)
    b = ItemInfo(item_id=11, template_id="ammunition_damage_epic", amount=50, stack_vc_value=500)
    return InventoryInfo(items=[a, b], storage_slots={10: 0, 11: 1})


def test_the_merge_operations_name_the_four_storages():
    assert MERGE_STACK_OPERATIONS == {0x0D: "storage_slots", 0x0E: "extra_storage_slots", 0x0F: "bank_storage_slots", 0x10: "extra_bank_storage_slots"}


def test_a_whole_stack_joins_the_other():
    builder = InventoryMutationBuilder(_inventory(), 100)
    assert builder.merge_stacks(10, 11, 50000) == 30
    mutation = builder.finish()
    items = mutation.inventory.items
    assert [(i.amount, i.stack_vc_value) for i in items] == [(80, 800)]
    assert 10 not in mutation.inventory.storage_slots and 11 not in mutation.inventory.storage_slots
    assert list(mutation.inventory.storage_slots.values()) == [1]


def test_only_what_fits_under_the_stack_limit_moves():
    builder = InventoryMutationBuilder(_inventory(), 100)
    assert builder.merge_stacks(10, 11, 60) == 10
    amounts = sorted(i.amount for i in builder.finish().inventory.items)
    assert amounts == [20, 60]


def test_a_full_stack_or_another_item_is_refused():
    with pytest.raises(ValueError):
        InventoryMutationBuilder(_inventory(), 100).merge_stacks(10, 11, 50)
    inventory = _inventory()
    inventory.items[0].template_id = "ammunition_damage_rare"
    with pytest.raises(ValueError):
        InventoryMutationBuilder(inventory, 100).merge_stacks(10, 11, 50000)
