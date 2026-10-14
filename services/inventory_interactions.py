"""NPC, crafting, salvage, quick-slot, and prop inventory interactions."""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.gameplay.local_map_state import LocalNPCDefinition, local_npc_actor_id
from dso_server.protocol.gameplay_protocol import ENCOUNTER_COMMAND


class InventoryInteractionService:
    def _handle_salvage(self, s: Session, payload: bytes) -> None:
        request = decode_salvage_request(payload)
        CharacterService._accept_gameplay_state_command(s, SALVAGE_COMMAND)
        with s.flow_lock:
            s.gameplay_client_command_state[SALVAGE_COMMAND] = request
            inventory = copy.deepcopy(s.gameplay_inventory)
            currency = s.gameplay_currency_state
            next_item_id = s.gameplay_next_item_id
        try:
            if inventory is None or currency is None:
                raise ValueError("player inventory is not initialized")
            if not request.item_ids:
                raise ValueError("salvage request has no items")
            builder = InventoryMutationBuilder(inventory, next_item_id)
            glyphs = 0
            dust_rewards: dict[str, int] = {}
            salvaged = []
            for item_id in request.item_ids:
                item = builder.item(item_id)
                if int(item_id) in builder.inventory.equipment_slots:
                    raise ValueError("equipped items cannot be melted")
                reward = item_melt_reward(self.static_database_path, item)
                glyphs += int(reward.glyphs)
                if reward.dust_template_id:
                    dust_rewards[reward.dust_template_id] = (
                        dust_rewards.get(reward.dust_template_id, 0)
                        + int(reward.dust_amount)
                    )
                salvaged.append((item.template_id, item.item_level))
                builder.consume(item_id)
            for template_id, amount in sorted(dust_rewards.items()):
                builder.add_item(
                    load_purchase_item_definition(
                        self.static_database_path, template_id
                    ),
                    amount,
                    item_level=1,
                )
            mutation = builder.finish()
            updated_currency = credit_fixed_currency(currency, glyph=glyphs)
            packet, packet_bits = encode_server_command_batch(
                (
                    ServerCommand(
                        INVENTORY_INFO_COMMAND,
                        mutation.delta,
                        LOCAL_PLAYER_ACTOR_ID,
                    ),
                    ServerCommand(
                        CURRENCY_CHANGED_COMMAND,
                        updated_currency,
                        LOCAL_PLAYER_ACTOR_ID,
                    ),
                    ServerCommand(
                        INVENTORY_INFO_COMMAND,
                        mutation.final,
                        LOCAL_PLAYER_ACTOR_ID,
                    ),
                ),
                include_terminator=True,
            )
        except (OSError, ValueError) as exc:
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected salvage: {exc}")
            return
        with s.flow_lock:
            s.gameplay_currency_state = updated_currency
            s.gameplay_inventory = mutation.inventory
            s.gameplay_next_item_id = mutation.next_item_id
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label="items-salvaged"
        )
        CharacterService._request_account_save(
            s, "inventory items salvaged", flush=True
        )
        self._daily_challenge_feed(s, "ItemsSalvaged", count=len(salvaged))
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] melted {salvaged}; "
            f"credited {glyphs} Glyphs of Power and {dust_rewards}"
        )

    @staticmethod
    def _handle_gauntlet_notification(s: Session, payload: bytes) -> None:
        request = decode_gauntlet_notification(payload)
        CharacterService._accept_gameplay_state_command(s, 0x012B)
        response = encode_gauntlet_notification_response(request, actor_id=0)
        s.send_reliable_ordered(
            response,
            track=True,
            label="gauntlet-notification-response",
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] handled GauntletNotificationInfo "
            f"({request.category}, {request.notification}); sent serialized "
            "0x012c response"
        )

    def _handle_quick_slots(self, s: Session, payload: bytes) -> None:
        """Apply one indexed client quickbar and persist the complete set."""

        decoded = decode_simple_gameplay_command(payload)
        bar_index = int(decoded.bar_index)
        if not 0 <= bar_index < 10:
            raise ValueError(
                f"QuickSlots bar index {bar_index} is outside [0, 10)"
            )
        if len(decoded.slots) != 17:
            raise ValueError(
                f"QuickSlots bar {bar_index} has {len(decoded.slots)} "
                "entries; expected 17"
            )
        slots = []
        for slot in decoded.slots:
            kind = int(slot.kind)
            resource_id = (
                None if slot.resource_id is None else str(slot.resource_id)
            )
            if kind == 0xFFFFFFFF:
                if resource_id is not None:
                    raise ValueError(
                        "empty QuickSlots entry carries a resource id"
                    )
            elif not resource_id or len(resource_id) > 256:
                raise ValueError(
                    "occupied QuickSlots entry has an invalid resource id"
                )
            slots.append(QuickSlotInfo(kind, resource_id))

        empty_bar = tuple(
            QuickSlotInfo(0xFFFFFFFF, None) for _ in range(17)
        )
        new_slots = tuple(slots)
        with s.flow_lock:
            current = s.gameplay_quick_slots
            bars = (
                list(current.quickbars)
                if isinstance(current, QuickSlotsInfo)
                else []
            )
            while len(bars) <= bar_index:
                bars.append(empty_bar)
            changed = bars[bar_index] != new_slots
            if changed:
                bars[bar_index] = new_slots
                s.gameplay_quick_slots = QuickSlotsInfo(tuple(bars))
            s.gameplay_client_command_state[0x0051] = decoded
        if not changed:
            # The client repeats its complete ten-bar snapshot during login
            # and can repeat it while reliable traffic is recovering. An
            # identical bar is an acknowledgement of authoritative state,
            # not a new account mutation.
            return
        CharacterService._accept_gameplay_state_command(s, 0x0051)
        CharacterService._request_account_save(
            s, "quick slots changed", flush=True
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] saved 17 quick slots for "
            f"bar {bar_index}"
        )

    def _handle_prop_request(self, s: Session, payload: bytes) -> None:
        request = decode_simple_gameplay_command(payload)
        CharacterService._accept_gameplay_state_command(s, 0x0038)
        with s.flow_lock:
            s.gameplay_client_command_state[0x0038] = request
            gameplay_map = s.gameplay_map or DEFAULT_GAMEPLAY_MAP
        props = CharacterService._map_event_prop_info(s)
        if props is None:
            props = load_local_crafting_props(
                self.static_database_path, gameplay_map
            )
        packet, packet_bits = encode_server_command_batch(
            (
                ServerCommand(
                    PROP_INFO_COMMAND,
                    props,
                    LOCAL_PLAYER_ACTOR_ID,
                ),
            ),
            include_terminator=True,
        )
        # Event props can make this response larger than the client's MTU.
        # An unsplit reply then blocks every later reliable-ordered command,
        # including SwitchMap, even when the switch datagram itself is ACKed.
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label="prop-info-response",
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] handled PropRequest for "
            f"{gameplay_map!r}: sent {len(props.entries)} typed prop(s)"
        )

    def _active_npc_definitions(self, s: Session) -> tuple[LocalNPCDefinition, ...]:
        """Use the map instance's event sets for both dialogs and quest refreshes."""
        gameplay_map = s.gameplay_map or DEFAULT_GAMEPLAY_MAP
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            return load_local_npc_definitions(
                self.static_database_path, gameplay_map,
                include_inactive_event_sets=False,
                active_event_flags=CharacterService._event_catalog(self).active_event_flags(),
            )
        with runtime.lock:
            enabled = frozenset(runtime.enabled_event_sets)
        return tuple(
            definition for definition in load_local_npc_definitions(
                self.static_database_path, gameplay_map,
                include_inactive_event_sets=True,
            )
            if definition.event_set_row < 0 or definition.event_set_row in enabled
        )

    def _resolve_npc_interaction(self, s: Session, actor_id: int) -> LocalNPCDefinition | None:
        definitions = CharacterService._active_npc_definitions(self, s)
        if int(actor_id) != 0:
            return next((d for d in definitions if int(actor_id) in {
                int(d.actor_id), local_npc_actor_id(d),
            }), None)
        # Static-scene NPCs may request their role with actor zero before
        # their dynamic actor is attached. Keep the existing proximity limit.
        with s.flow_lock:
            encounter = s.gameplay_monster_encounter
            last_move = s.gameplay_last_move
            spawn_position = s.gameplay_spawn_position
        player_position = (
            encounter.coordinate_frame.wire_to_world(last_move.relative_position)
            if encounter is not None and last_move is not None else spawn_position
        )
        if player_position is None:
            return None

        def distance(candidate):
            return math.hypot(
                candidate.position[0] - player_position[0],
                candidate.position[2] - player_position[2],
            )

        definition = min(definitions, key=distance, default=None)
        return definition if definition is not None and distance(definition) <= 8.0 else None

    def _handle_npc_encounter(self, s: Session, payload: bytes) -> None:
        request = decode_simple_gameplay_command(payload)
        CharacterService._accept_gameplay_state_command(s, ENCOUNTER_COMMAND)
        with s.flow_lock:
            s.gameplay_client_command_state[ENCOUNTER_COMMAND] = request
        definition = CharacterService._resolve_npc_interaction(self, s, request.value)
        if definition is not None:
            CharacterService._quest_on_npc_encounter(self, s, definition.template_id)

    def _handle_npc_request(self, s: Session, payload: bytes) -> None:
        request = decode_simple_gameplay_command(payload)
        CharacterService._accept_gameplay_state_command(s, 0x0029)
        with s.flow_lock:
            s.gameplay_client_command_state[0x0029] = request
        definition = CharacterService._resolve_npc_interaction(self, s, request.value)
        response_actor_id = 0 if int(request.value) == 0 else None
        if definition is None:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] NPCRequest references unknown "
                f"local actor {request.value}"
            )
            return

        # Quests: an encounter with the NPC advances CompleteTasks first, so
        # the dialog that follows already offers the turn-in (retail order).
        CharacterService._quest_on_npc_encounter(self, s, definition.template_id)
        npc_info = CharacterService._quest_decorate_npc_info(
            self, s, local_npc_info(definition, actor_id=response_actor_id),
            definition.template_id,
        )
        with s.flow_lock:
            s.gameplay_interaction_npc_shop_id = (
                str(definition.shop_id or "").strip() or None
            )
        response = encode_npc_batch(NPCBatch((npc_info,)))
        s.send_reliable_ordered(
            response,
            track=True,
            label="npc-info-response",
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] handled NPCRequest for "
            f"{definition.template_id!r} actor {npc_info.actor_id}: "
            f"sent typed NPCInfo category {npc_info.category}"
        )

    def _handle_crafting(self, s: Session, payload: bytes) -> None:
        request = decode_crafting_request(payload)
        CharacterService._accept_gameplay_state_command(s, CRAFTING_COMMAND)
        with s.flow_lock:
            s.gameplay_client_command_state[CRAFTING_COMMAND] = request
            inventory = copy.deepcopy(s.gameplay_inventory)
            currency = s.gameplay_currency_state
            next_item_id = s.gameplay_next_item_id
            character = copy.deepcopy(s.gameplay_character)
        try:
            if inventory is None or currency is None or character is None:
                raise ValueError("player inventory is not initialized")
            if not 1 <= int(request.amount) <= 1_000_000:
                raise ValueError("craft amount is outside 1..1000000")
            items_by_id = {
                int(item.item_id): item for item in inventory.items
            }
            try:
                selected_template_ids = tuple(
                    items_by_id[int(item_id)].template_id
                    for item_id in request.item_ids
                )
            except KeyError as exc:
                raise ValueError(
                    f"crafting ingredient item {int(exc.args[0])} "
                    "does not exist"
                ) from exc
            recipe = load_crafting_recipe(
                self.static_database_path,
                request.recipe_id,
                selected_template_ids=selected_template_ids,
            )
            craft_count = crafting_execution_count(recipe, request)
            if int(character.level) < recipe.minimum_level:
                raise ValueError("character level is below recipe unlock")
            if (
                recipe.unlock_resource_id
                and recipe.unlock_resource_id
                not in s.gameplay_recipe_book_resources
            ):
                raise ValueError(
                    "recipe is not present in the character recipe book"
                )
            if recipe.result_is_loot_table:
                raise ValueError(
                    f"generated crafting result "
                    f"{recipe.result_template_id!r} is not an item template"
                )
            builder = InventoryMutationBuilder(inventory, next_item_id)
            equipment_before = copy.deepcopy(inventory.equipment_slots)
            for template_id, ingredient_amount in recipe.ingredients:
                builder.consume_template(
                    template_id,
                    ingredient_amount * craft_count,
                    preferred_item_ids=request.item_ids,
                    preserve_item_ids=True,
                )
            result_definition = load_purchase_item_definition(
                self.static_database_path, recipe.result_template_id
            )
            builder.add_item(
                result_definition,
                craft_count * int(recipe.result_amount),
                item_level=max(1, int(character.level)),
            )
            mutation = builder.finish()
            if mutation.inventory.equipment_slots != equipment_before:
                raise ValueError(
                    "crafting attempted to change unrelated equipment slots"
                )
            # Retail keeps this final InventoryInfo flag clear. The generic
            # mutation path sets it for predictive inventory actions, which
            # makes the workbench render the output as another input.
            mutation.delta.field_1e0 = False
            total_price = crafting_price_for_payment(
                recipe.price,
                request.payment_currency,
                craft_count,
            )
            updated_currency, updated_ic = debit_currency(
                currency,
                total_price,
                ic_balance=s.gameplay_currency_ic,
            )
            result_items = tuple(
                copy.deepcopy(item)
                for item in mutation.changed_items
                if item.template_id == recipe.result_template_id
            )
            if not result_items:
                raise ValueError(
                    "crafting mutation did not publish its result item"
                )
            packet, packet_bits = encode_server_command_batch(
                (
                    ServerCommand(
                        INVENTORY_INFO_COMMAND,
                        mutation.delta,
                        LOCAL_PLAYER_ACTOR_ID,
                    ),
                    ServerCommand(
                        CURRENCY_CHANGED_COMMAND,
                        updated_currency,
                        LOCAL_PLAYER_ACTOR_ID,
                    ),
                    ServerCommand(
                        CRAFTING_RESULT_COMMAND,
                        CraftingResult(
                            status=0,
                            items=result_items,
                            result_type=3,
                            item_relations=(),
                            field_70=0,
                        ),
                        0,
                    ),
                ),
                include_terminator=True,
            )
        except (OSError, ValueError) as exc:
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected crafting: {exc}")
            return
        with s.flow_lock:
            s.gameplay_currency_state = updated_currency
            s.gameplay_inventory = mutation.inventory
            s.gameplay_currency_ic = updated_ic
            s.gameplay_next_item_id = mutation.next_item_id
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label="workbench-crafting"
        )
        CharacterService._request_account_save(
            s, "workbench recipe crafted", flush=True
        )
        self._daily_challenge_feed(s, 'ItemsCrafted', count=craft_count * recipe.result_amount)
        from dso_server.protocol.consumable_protocol import load_consumable_catalog
        if load_consumable_catalog(self.static_database_path).definition(recipe.result_template_id) is not None:
            self._daily_challenge_feed(s, 'ConsumablesCrafted', count=craft_count * recipe.result_amount)
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] crafted {craft_count}x "
            f"{recipe.result_template_id!r} from {recipe.resource_id!r}; "
            f"output={craft_count * recipe.result_amount}, "
            f"mode={request.mode}, payment={request.payment_currency}, "
            f"source_slot={request.source_slot}, "
            f"price={total_price.as_tuple()}"
        )
