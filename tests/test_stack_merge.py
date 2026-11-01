"""MergeStack (inventory operations 0x0d..0x10): a stack dropped on another
joins it up to MaxStackSize; S's server ignored it, so stacks never combined."""

import pytest

from dso_server.protocol.gameplay_protocol import MERGE_STACK_OPERATIONS
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.protocol.inventory_economy_protocol import InventoryMutationBuilder


def _inventory():
    a = ItemInfo(item_id=10, template_id="ammunition_damage_epic", amount=30, field_30=300)
    b = ItemInfo(item_id=11, template_id="ammunition_damage_epic", amount=50, field_30=500)
    return InventoryInfo(items=[a, b], storage_58={10: 0, 11: 1})


def test_the_merge_operations_name_the_four_storages():
    assert MERGE_STACK_OPERATIONS == {0x0D: "storage_58", 0x0E: "storage_80", 0x0F: "storage_a8", 0x10: "storage_d0"}


def test_a_whole_stack_joins_the_other():
    builder = InventoryMutationBuilder(_inventory(), 100)
    assert builder.merge_stacks(10, 11, 50000) == 30
    mutation = builder.finish()
    items = mutation.inventory.items
    assert [(i.amount, i.field_30) for i in items] == [(80, 800)]
    assert 10 not in mutation.inventory.storage_58 and 11 not in mutation.inventory.storage_58
    assert list(mutation.inventory.storage_58.values()) == [1]


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
