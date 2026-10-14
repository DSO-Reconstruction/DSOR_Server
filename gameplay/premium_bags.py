"""Premium containers and durable items waiting for an available slot."""

from dataclasses import dataclass

from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo


# The client layouts use four rows of seven slots for inventory and eight
# rows of five for the bank (Inventory/BankExtensionSlotAmount in _Globals).
PREMIUM_INVENTORY_SLOTS = 28
PREMIUM_STORAGE_SLOTS = 40


@dataclass(frozen=True)
class PremiumBagChanges:
    removed_ids: tuple[int, ...]
    restored_items: tuple[ItemInfo, ...]


def reconcile_premium_bags(
    inventory: InventoryInfo, *, active: bool,
) -> PremiumBagChanges:
    """Update an owned snapshot, keeping overflow outside usable ``items``.

    Inventory overflow returns to inventory; bank overflow returns to the
    bank. Restore whole stacks in their original order without changing IDs
    or item data. A renewed membership also makes the extra slots available.
    """

    inventory.extra_storage_size = PREMIUM_INVENTORY_SLOTS if active else 0
    inventory.extra_bank_storage_size = PREMIUM_STORAGE_SLOTS if active else 0
    removed = []
    restored = []
    for ordinary, extra, capacity, extra_capacity, overflow in (
        (inventory.storage_58, inventory.storage_80, inventory.storage_size,
         inventory.extra_storage_size, inventory.premium_inventory_overflow),
        (inventory.storage_a8, inventory.storage_d0, inventory.bank_storage_size,
         inventory.extra_bank_storage_size, inventory.premium_storage_overflow),
    ):
        displaced = {
            item_id for item_id, slot in extra.items()
            if not 0 <= slot < extra_capacity
        }
        if displaced:
            items = {item.item_id: item for item in inventory.items}
            for item_id in sorted(displaced, key=extra.__getitem__):
                overflow.append(items[item_id])
                del extra[item_id]
            inventory.items = [
                item for item in inventory.items if item.item_id not in displaced
            ]
        destinations = []
        if overflow:
            for storage, size in ((ordinary, capacity), (extra, extra_capacity)):
                used = set(storage.values())
                destinations.extend(
                    (storage, slot) for slot in range(size) if slot not in used
                )
        for item, (storage, slot) in zip(overflow, destinations):
            storage[item.item_id] = slot
            inventory.items.append(item)
            if item.item_id in displaced:
                displaced.remove(item.item_id)
            else:
                restored.append(item)
        del overflow[:len(destinations)]
        removed.extend(sorted(displaced))
    return PremiumBagChanges(tuple(removed), tuple(restored))
