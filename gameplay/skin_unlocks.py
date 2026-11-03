"""Repair transmog source records and normalize client inventory storage."""

from __future__ import annotations

from pathlib import Path
import sqlite3

from dso_server.runtime import resident_content
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.player_init_protocol import ItemCustomization

# Seven bank bags of 40 slots (BagButton..BagButton6 in bankwindow.bxml):
# default_player's 10 slots at BankStorageUpgrade 1, plus five per
# extension (_Globals BankExtensionSlotAmount), full at upgrade 55.
MAX_BANK_STORAGE_SLOTS = 280
MAX_BANK_STORAGE_UPGRADE = 55


def load_skin_unlock_customizations(
    static_database_path: str | Path,
    *,
    class_name: str,
) -> tuple[ItemCustomization, ...]:
    """Build the client's transmog offer list for one character class."""

    class_name = str(class_name).strip().lower()
    database = sqlite3.connect(
        resident_content.resolved(static_database_path).as_uri() + "?mode=ro",
        uri=True,
    )
    try:
        rows = database.execute(
            "SELECT Id FROM _Template_EquipmentSkin "
            "WHERE Visible=1 AND "
            "(';' || lower(COALESCE(CharClass, '')) || ';') LIKE ? "
            "ORDER BY Id",
            (f"%;{class_name};%",),
        ).fetchall()
    finally:
        database.close()
    # The transmog manager consumes these records as skin offers. Supplying
    # the skin resource in both string fields seeds the offer and its matching
    # customization entry without fabricating inventory items.
    return tuple(
        ItemCustomization(str(row[0]), str(row[0])) for row in rows
    )


def repair_skin_source_inventory(
    inventory: InventoryInfo,
    *,
    static_database_path: str | Path,
    class_name: str,
) -> InventoryInfo:
    """Remove invalid source records from older unlock attempts.

    Every item in InventoryInfo needs a concrete equipment or storage slot.
    Previous attempts expanded the bank beyond the native limit and later
    left some source records unallocated; both shapes crash the client while
    processing inventory setup.
    """

    class_name = str(class_name).strip().lower()
    database = sqlite3.connect(
        resident_content.resolved(static_database_path).as_uri() + "?mode=ro",
        uri=True,
    )
    try:
        rows = database.execute(
            "SELECT skin.Id, skin.TooltipItem, item.Id "
            "FROM _Template_EquipmentSkin AS skin "
            "LEFT JOIN _Template_Item AS consumable "
            "ON consumable.EquipmentSkinId=skin.Id "
            "AND consumable.ItemCategory='SkinConsumable' "
            "LEFT JOIN _Template_Item AS item "
            "ON item.Id=CASE WHEN COALESCE(skin.TooltipItem, '') <> '' "
            "THEN skin.TooltipItem ELSE consumable.Id END "
            "WHERE skin.Visible=1 AND "
            "(';' || lower(COALESCE(skin.CharClass, '')) || ';') LIKE ? "
            "AND item.Id IS NOT NULL ORDER BY item.Id",
            (f"%;{class_name};%",),
        ).fetchall()
    finally:
        database.close()

    source_templates = {str(row[2]) for row in rows if row[2]}
    # The client asserts when bank storage exceeds its fixed native limit.
    # Repair any capacity/allocation written by the earlier implementation.
    # The server sells no bank extensions, so every character gets the full
    # bank; saves from the 53-extension capture had a 30-slot last bag.
    inventory.bank_storage_size = MAX_BANK_STORAGE_SLOTS
    inventory.bank_storage_upgrade = MAX_BANK_STORAGE_UPGRADE
    inventory.bank_storage_slots = {
        item_id: slot
        for item_id, slot in inventory.bank_storage_slots.items()
        if 0 <= int(slot) < inventory.bank_storage_size
    }
    allocated_item_ids = (
        set(inventory.equipment_slots)
        | set(inventory.storage_slots)
        | set(inventory.extra_storage_slots)
        | set(inventory.bank_storage_slots)
        | set(inventory.extra_bank_storage_slots)
        | set(inventory.ingredient_bag_item_ids)
    )
    # Remove only source records made unreachable by the previous grant
    # implementation. InventoryInfo requires every wire item to resolve to a
    # client slot; leaving one unallocated trips LocateItem during login.
    inventory.items = [
        item for item in inventory.items
        if int(item.item_id) in allocated_item_ids
        or str(item.template_id) not in source_templates
    ]
    return inventory
