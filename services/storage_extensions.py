"""Handle Inventory and Bank extension purchases and consumable item uses."""

from __future__ import annotations

import copy
import sqlite3
from pathlib import Path
from dso_server.protocol.gameplay_commands import LOCAL_PLAYER_ACTOR_ID
from dso_server.protocol.inventory_codec import (
    InventoryInfo,
    clone_inventory_info,
)
from dso_server.protocol.inventory_economy_protocol import InventoryMutationBuilder
from dso_server.protocol.player_init_protocol import (
    INVENTORY_INFO_COMMAND,
    ServerCommand,
    encode_server_command_batch,
)
from dso_server.services.base import CharacterService

MAX_INVENTORY_SLOTS = 224
MAX_INVENTORY_UPGRADES = 29
INVENTORY_SLOTS_PER_UPGRADE = 7

MAX_BANK_SLOTS = 280
MAX_BANK_UPGRADES = 55
BANK_SLOTS_PER_UPGRADE = 5


def extension_grant_amounts(template_id: str) -> tuple[str, int, int]:
    """Return ('inventory' | 'bank', upgrades_to_add, slots_to_add) for an item template."""
    tid = str(template_id).lower()
    if "bank" in tid:
        kind = "bank"
        if "xxl" in tid:
            upgrades = 54
        elif "bundle" in tid:
            upgrades = 3
        else:
            upgrades = 1
        slots = upgrades * BANK_SLOTS_PER_UPGRADE
        return kind, upgrades, slots
    elif "bag" in tid or "inventory" in tid:
        kind = "inventory"
        if "xxl" in tid:
            upgrades = 29
        elif "bundle" in tid:
            upgrades = 3
        else:
            upgrades = 1
        slots = upgrades * INVENTORY_SLOTS_PER_UPGRADE
        return kind, upgrades, slots
    return "", 0, 0


def handle_storage_extension_use(server, session, item_id: int) -> bool:
    """Commit one consumed inventory/bank extension item and update capacities."""
    with session.flow_lock:
        inventory = session.gameplay_inventory
        if not inventory:
            return False
        item = next(
            (it for it in inventory.items if int(it.item_id) == int(item_id)),
            None,
        )
        if item is None:
            return False

    kind, upgrades, slots = extension_grant_amounts(item.template_id)
    if not kind:
        # Check if item category in DB is InventoryExtension or BankExtension
        try:
            with sqlite3.connect(
                server.static_database_path.resolve().as_uri() + "?mode=ro",
                uri=True,
            ) as database:
                row = database.execute(
                    "SELECT ItemCategory FROM _Template_Item WHERE Id=?",
                    (item.template_id,),
                ).fetchone()
                if row:
                    cat = str(row[0])
                    if cat == "InventoryExtension":
                        kind, upgrades, slots = "inventory", 1, INVENTORY_SLOTS_PER_UPGRADE
                    elif cat == "BankExtension":
                        kind, upgrades, slots = "bank", 1, BANK_SLOTS_PER_UPGRADE
        except Exception:
            pass

    if not kind:
        return False

    with session.flow_lock:
        inventory = session.gameplay_inventory
        item = next(
            (it for it in inventory.items if int(it.item_id) == int(item_id)),
            None,
        )
        if item is None:
            return True

        curr_storage_size = int(getattr(inventory, "storage_size", 21))
        curr_storage_upgrade = int(getattr(inventory, "storage_upgrade", 0))
        curr_bank_size = int(getattr(inventory, "bank_storage_size", 20))
        curr_bank_upgrade = int(getattr(inventory, "bank_storage_upgrade", 0))

        if kind == "inventory":
            if curr_storage_size >= MAX_INVENTORY_SLOTS:
                return True
            new_storage_size = min(MAX_INVENTORY_SLOTS, curr_storage_size + slots)
            new_storage_upgrade = min(MAX_INVENTORY_UPGRADES, curr_storage_upgrade + upgrades)
            new_bank_size = curr_bank_size
            new_bank_upgrade = curr_bank_upgrade
        else:
            if curr_bank_size >= MAX_BANK_SLOTS:
                return True
            new_bank_size = min(MAX_BANK_SLOTS, curr_bank_size + slots)
            new_bank_upgrade = min(MAX_BANK_UPGRADES, curr_bank_upgrade + upgrades)
            new_storage_size = curr_storage_size
            new_storage_upgrade = curr_storage_upgrade

        builder = InventoryMutationBuilder(inventory, session.gameplay_next_item_id)
        builder.consume(item_id, 1, preserve_item_id=True)
        mutation = builder.finish()

        # Update capacities on the resulting inventory object and command deltas
        for target in (mutation.inventory, mutation.delta, mutation.final):
            target.storage_size = new_storage_size
            target.storage_upgrade = new_storage_upgrade
            target.bank_storage_size = new_bank_size
            target.bank_storage_upgrade = new_bank_upgrade

        from dso_server.protocol.reward_protocol import (
            resolve_inventory_extension_offer,
            resolve_bank_extension_offer,
        )
        from dso_server.protocol.player_init_protocol import (
            SPECIAL_OFFER_COMMAND,
            SpecialOffer,
        )
        updated_special_offer = SpecialOffer(
            resolve_inventory_extension_offer(
                server.static_database_path, new_storage_upgrade
            ),
            resolve_bank_extension_offer(
                server.static_database_path, new_bank_upgrade
            ),
            None,
            False,
            "0000-00-00 00:00:00",
            (),
            (),
            (),
            (),
        )

        packet, bits = encode_server_command_batch(
            (
                ServerCommand(INVENTORY_INFO_COMMAND, mutation.delta, LOCAL_PLAYER_ACTOR_ID),
                ServerCommand(INVENTORY_INFO_COMMAND, mutation.final, LOCAL_PLAYER_ACTOR_ID),
                ServerCommand(SPECIAL_OFFER_COMMAND, updated_special_offer, 0),
            ),
            include_terminator=True,
        )
        session.gameplay_inventory = mutation.inventory
        session.gameplay_next_item_id = mutation.next_item_id

    CharacterService._send_world_loot_state_batch(
        session, packet, bits, label=f"{kind}-extension-applied"
    )
    CharacterService._request_account_save(
        session, f"{kind} capacity expanded via item", flush=True
    )
    print(
        f"[DrasaGameplayService] expanded {kind} capacity via item "
        f"{item.template_id!r} -> storage={mutation.inventory.storage_size} "
        f"(upg {mutation.inventory.storage_upgrade}), bank={mutation.inventory.bank_storage_size} "
        f"(upg {mutation.inventory.bank_storage_upgrade})"
    )
    return True
