"""Authoritative whole-stack food conversion at an active recycling NPC."""

from __future__ import annotations

import math
import sqlite3

from dso_server.protocol.currency_protocol import GAME_CURRENCY_CHANGED_COMMAND, update_game_currency
from dso_server.protocol.food_recycling_protocol import (
    SALVAGE_COOK_ITEM_COMMAND, decode_food_recycling_request, load_food_recycling_catalog,
)
from dso_server.protocol.gameplay_commands import LOCAL_PLAYER_ACTOR_ID
from dso_server.protocol.inventory_economy_protocol import InventoryMutationBuilder
from dso_server.protocol.player_init_protocol import (
    INVENTORY_INFO_COMMAND, ServerCommand, encode_server_command_batch,
)
from dso_server.services.base import CharacterService


class FoodRecyclingService:
    def _food_recycling_npc(self, session):
        definitions = self._active_npc_definitions(session)
        with session.flow_lock:
            encounter = session.gameplay_monster_encounter
            move = session.gameplay_last_move
            spawn = session.gameplay_spawn_position
        position = (
            encounter.coordinate_frame.wire_to_world(move.relative_position)
            if encounter is not None and move is not None else spawn
        )
        if position is None:
            return None
        # NPC requests already use this horizontal interaction radius. The
        # recycling command has no actor field, so resolve the active role
        # nearby instead of trusting an earlier shop ID across maps/events.
        return next((npc for npc in definitions
                     if npc.profession == "food_recycling"
                     and math.hypot(npc.position[0] - position[0],
                                    npc.position[2] - position[2]) <= 8.0), None)

    def _handle_food_recycling(self, session, payload: bytes) -> None:
        try:
            request = decode_food_recycling_request(payload)
            catalog = load_food_recycling_catalog(self.static_database_path)
        except (OSError, ValueError, sqlite3.Error) as exc:
            print(f"[food recycling] rejected request: {exc}")
            return
        with session.gameplay_mutation_lock:
            with session.flow_lock:
                if session.gameplay_exit_transition_started:
                    return
            try:
                if self._food_recycling_npc(session) is None:
                    raise ValueError("no active food recycling NPC within interaction range")
                with session.flow_lock:
                    inventory = session.gameplay_inventory
                    if inventory is None or session.gameplay_character is None:
                        raise ValueError("player inventory is not initialized")
                    builder = InventoryMutationBuilder(inventory, session.gameplay_next_item_id)
                    gained = 0
                    for item_id in request.item_ids:
                        item = builder.item(item_id)
                        unit_yield = catalog.unit_yields.get(item.template_id, 0)
                        if unit_yield <= 0 or not 1 <= int(item.amount) <= 0xFFFFFFFF:
                            raise ValueError(f"item {item_id} has no authored recycling yield")
                        if item_id in inventory.equipment_slots:
                            raise ValueError("equipped items cannot be recycled")
                        gained += unit_yield * int(item.amount)
                        builder.consume(item_id)
                    mutation = builder.finish()
                    currencies, update = update_game_currency(
                        session.gameplay_game_currencies, catalog.currency_id,
                        int(session.gameplay_game_currencies.get(catalog.currency_id, 0)) + gained,
                    )
                    packet, bits = encode_server_command_batch((
                        ServerCommand(INVENTORY_INFO_COMMAND, mutation.delta, LOCAL_PLAYER_ACTOR_ID),
                        ServerCommand(GAME_CURRENCY_CHANGED_COMMAND, update, LOCAL_PLAYER_ACTOR_ID),
                        ServerCommand(INVENTORY_INFO_COMMAND, mutation.final, LOCAL_PLAYER_ACTOR_ID),
                    ), include_terminator=True)
                    session.gameplay_inventory = mutation.inventory
                    session.gameplay_next_item_id = mutation.next_item_id
                    session.gameplay_game_currencies = currencies
                    session.gameplay_client_command_state[SALVAGE_COOK_ITEM_COMMAND] = request
            except (OSError, ValueError) as exc:
                print(f"[food recycling] rejected conversion: {exc}")
                return

            CharacterService._accept_gameplay_state_command(session, SALVAGE_COOK_ITEM_COMMAND)
            CharacterService._request_account_save(session, "food recycled", flush=True)
            deferred = []
            CharacterService._achievement_feed(
                self, session, "collectedgamecurrency", (catalog.currency_id,),
                count=gained, deferred_packets=deferred,
            )
            CharacterService._send_world_loot_state_batch(
                session, packet, bits, label="food-recycled"
            )
            for packet, bits, label in deferred:
                CharacterService._send_world_loot_state_batch(session, packet, bits, label=label)
