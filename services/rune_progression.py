"""Divination Stones earn the two additional rune-trinket equipment slots."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import sqlite3

from dso_server.protocol.gameplay_commands import LOCAL_PLAYER_ACTOR_ID
from dso_server.protocol.inventory_codec import ItemInfo
from dso_server.protocol.inventory_economy_protocol import InventoryMutationBuilder
from dso_server.protocol.player_init_protocol import (
    INVENTORY_INFO_COMMAND, ServerCommand, encode_server_command_batch,
)
from dso_server.services.base import CharacterService


RUNE_EXTENSION_ITEM = "item_rune_extension_stone"
EXTRA_RUNE_SLOTS = (18, 19)
RUNE_SLOT_ACHIEVEMENTS = (
    "item_use_item_rune_extension_stone", "item_use_item_rune_extension_stone_02",
)


def unlocked_rune_slots(session, *, inventory=None) -> frozenset[int]:
    """Include saved entitlements and evidence from legacy occupied slots."""
    unlocked = set(getattr(session, "gameplay_rune_trinket_unlocked_slots", ()))
    inventory = session.gameplay_inventory if inventory is None else inventory
    if inventory is not None:
        item_ids = {int(item.item_id) for item in inventory.items}
        unlocked.update(
            int(slot) for item_id, slots in inventory.equipment_slots.items()
            if int(item_id) in item_ids for slot in slots
        )
    achievements = getattr(session, "gameplay_achievements", {})
    unlocked.update(
        slot for slot, key in zip(EXTRA_RUNE_SLOTS, RUNE_SLOT_ACHIEVEMENTS)
        if getattr(achievements.get(key), "completed", False)
    )
    return frozenset(unlocked.intersection(EXTRA_RUNE_SLOTS))


@lru_cache(maxsize=4)
def _extension_minimum_level(path: str) -> int:
    with sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True) as database:
        row = database.execute(
            "SELECT RequiredLevel FROM _Template_Item WHERE Id=? AND ItemCategory='Consumable'",
            (RUNE_EXTENSION_ITEM,),
        ).fetchone()
        if row is None:
            raise ValueError("Divination Stone is missing from the item catalog")
        if database.execute(
            "SELECT 1 FROM _Template_Item WHERE Id='all_base_trinket_rune_01' "
            "AND ItemCategory='RuneTrinket'"
        ).fetchone() is None:
            raise ValueError("base rune trinket is missing from the item catalog")
    return max(1, int(row[0] or 1))


def handle_rune_extension_use(server, session, item_id: int) -> bool:
    """Commit one consumed stone and its occupied slot before publication."""
    with session.flow_lock:
        inventory = session.gameplay_inventory
        item = next((item for item in inventory.items if int(item.item_id) == int(item_id)), None) if inventory else None
        if item is None or item.template_id != RUNE_EXTENSION_ITEM:
            return False
    minimum_level = _extension_minimum_level(str(Path(server.static_database_path).resolve()))
    with session.flow_lock:
        inventory = session.gameplay_inventory
        item = next((item for item in inventory.items if int(item.item_id) == int(item_id)), None) if inventory else None
        if item is None or item.template_id != RUNE_EXTENSION_ITEM:
            return True
        character = session.gameplay_character
        unlocked = unlocked_rune_slots(session, inventory=inventory)
        missing = tuple(slot for slot in EXTRA_RUNE_SLOTS if slot not in unlocked)
        if character is None or int(character.level) < minimum_level or not missing:
            return True
        slot = missing[0]
        builder = InventoryMutationBuilder(inventory, session.gameplay_next_item_id)
        builder.consume(item_id, 1, preserve_item_id=True)
        new_id = int(builder.next_item_id)
        if not 0 < new_id <= 0xFFFFFFFF or any(item.item_id == new_id for item in builder.inventory.items):
            raise ValueError("inventory item ID allocator is exhausted or duplicated")
        builder.next_item_id += 1
        now = datetime.now(timezone.utc)
        trinket = ItemInfo(
            item_id=new_id, template_id="all_base_trinket_rune_01", amount=1,
            socket_count=5, socketed_templates=[""] * 5, item_level=1,
            field_18=new_id & 0xFFFF, field_30=1, field_38=0,
            created_at=(now.year, now.month, now.day, now.hour, now.minute, now.second),
        )
        # The client regards an occupied extra rune slot as usable. Allocate
        # directly there, so a full inventory cannot prevent the paid unlock.
        builder.inventory.items.append(trinket)
        builder.inventory.equipment_slots[new_id] = (slot,)
        builder.changed_items.append(copy.deepcopy(trinket))
        mutation = builder.finish()
        packet, bits = encode_server_command_batch((
            ServerCommand(INVENTORY_INFO_COMMAND, mutation.delta, LOCAL_PLAYER_ACTOR_ID),
            ServerCommand(INVENTORY_INFO_COMMAND, mutation.final, LOCAL_PLAYER_ACTOR_ID),
        ), include_terminator=True)
        session.gameplay_inventory = mutation.inventory
        session.gameplay_next_item_id = mutation.next_item_id
        session.gameplay_rune_trinket_unlocked_slots = set(unlocked) | {slot}

    CharacterService._request_account_save(session, "rune trinket slot unlocked", flush=True)
    deferred_packets = []
    CharacterService._achievement_feed(
        server, session, "use", (RUNE_EXTENSION_ITEM,), count=1,
        deferred_packets=deferred_packets,
    )
    CharacterService._send_world_loot_state_batch(session, packet, bits, label="rune-trinket-unlocked")
    for packet, bits, label in deferred_packets:
        CharacterService._send_world_loot_state_batch(session, packet, bits, label=label)
    return True
