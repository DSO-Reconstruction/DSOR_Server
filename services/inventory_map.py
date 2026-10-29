"""Inventory actions, NPC/prop interaction, and map transitions."""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session


class InventoryMapService:
    """Inventory actions, NPC/prop interaction, and map transitions."""

    def _activate_season_pass_item(
            self, s: Session, item_id: int, template_id: str) -> bool:
        pass_unlocks = {
            "gold_pass": (1,),
            "gold_pass_2": (1, 3),
            "red_pass": (2,),
            "red_pass_2": (2, 4),
        }
        unlocks = pass_unlocks.get(str(template_id).casefold())
        if unlocks is None:
            return False

        from dso_server.gameplay.season_pass import normalize_season_pass_state
        from dso_server.protocol.inventory_economy_protocol import InventoryMutationBuilder
        from dso_server.protocol.player_init_protocol import (
            INVENTORY_INFO_COMMAND,
            SEASON_PASS_REWARDS_INFO_RESULT_COMMAND,
            SeasonPassRewardsInfoResult,
            ServerCommand,
            encode_server_command_batch,
        )

        with s.flow_lock:
            inventory = s.gameplay_inventory
            if inventory is None or s.gameplay_character is None:
                raise ValueError("player inventory is not initialized")
            item = next((entry for entry in inventory.items
                         if int(entry.item_id) == int(item_id)), None)
            if item is None or str(item.template_id).casefold() != str(template_id).casefold():
                raise ValueError("Season Pass ticket is no longer in inventory")
            state = normalize_season_pass_state(
                getattr(s, "gameplay_season_pass", None) or {}
            )
            current_types = set(state["pass_types"])
            newly_unlocked = set(unlocks) - current_types
            if not newly_unlocked:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] retained duplicate Season Pass "
                    f"ticket {template_id!r}; its reward track is already active"
                )
                return True

            builder = InventoryMutationBuilder(
                inventory, s.gameplay_next_item_id,
            )
            builder.consume(int(item_id), 1)
            mutation = builder.finish()
            state["pass_types"] = tuple(sorted(current_types | set(unlocks)))
            result = SeasonPassRewardsInfoResult(
                int(state["level"]), int(state["experience"]),
                tuple(state["pass_types"]), tuple(state["claimed_awards"]),
            )
            packet, packet_bits = encode_server_command_batch((
                ServerCommand(
                    INVENTORY_INFO_COMMAND, mutation.delta,
                    LOCAL_PLAYER_ACTOR_ID,
                ),
                ServerCommand(
                    SEASON_PASS_REWARDS_INFO_RESULT_COMMAND, result, 0,
                ),
                ServerCommand(
                    INVENTORY_INFO_COMMAND, mutation.final,
                    LOCAL_PLAYER_ACTOR_ID,
                ),
            ), include_terminator=True)
            s.gameplay_inventory = mutation.inventory
            s.gameplay_next_item_id = mutation.next_item_id
            s.gameplay_season_pass = state

        s.send_reliable_ordered_split(
            packet,
            total_bit_length=packet_bits,
            track=True,
            label_prefix="season-pass-ticket-activated",
        )
        CharacterService._request_account_save(
            s, "Season Pass ticket activated", flush=True,
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] activated Season Pass ticket "
            f"{template_id!r}; pass types={state['pass_types']}"
        )
        return True

    def _handle_inventory_sort(
            self, s: Session, action, inventory) -> None:
        """Sort the active inventory page reported by operation 0x19.

        The client sends the active page's first storage slot in ``field_0c``
        and its slot count in ``field_10`` (28 for a normal inventory page).
        Item ID is zero and the argument count is zero because this is a
        container operation, not an item operation.
        """

        try:
            if inventory is None:
                raise ValueError("player inventory is not initialized")
            if int(action.item_id) != 0 or action.argument_count != 0:
                raise ValueError(
                    "sort request must target the inventory with no item "
                    "arguments"
                )
            page_start = int(action.field_0c)
            page_size = int(action.field_10)
            if page_size <= 0 or page_size > 224:
                raise ValueError(
                    f"invalid inventory sort page size {page_size}"
                )
            if page_start < 0:
                raise ValueError(
                    f"invalid inventory sort page start {page_start}"
                )
            if page_start < 0 or page_start + page_size > int(
                inventory.storage_size
            ):
                raise ValueError(
                    f"inventory sort page [{page_start}, "
                    f"{page_start + page_size}) is outside capacity "
                    f"{inventory.storage_size}"
                )

            page_end = page_start + page_size
            page_items = [
                (int(item_id), int(slot))
                for item_id, slot in inventory.storage_58.items()
                if page_start <= int(slot) < page_end
            ]
            item_by_id = {
                int(item.item_id): item for item in inventory.items
            }
            if any(item_id not in item_by_id for item_id, _ in page_items):
                raise ValueError(
                    "inventory page contains an allocation without an item"
                )

            sorted_item_ids = sorted(
                (item_id for item_id, _ in page_items),
                key=lambda item_id: (
                    str(item_by_id[item_id].template_id).casefold(),
                    int(item_by_id[item_id].item_level),
                    int(item_by_id[item_id].field_1c),
                    item_id,
                ),
            )
            updated_inventory = clone_inventory_info(inventory)
            for item_id, _ in page_items:
                updated_inventory.storage_58.pop(item_id, None)
            for offset, item_id in enumerate(sorted_item_ids):
                updated_inventory.storage_58[item_id] = page_start + offset

            response_inventory = equipment_allocation_snapshot(
                updated_inventory
            )
            packet, packet_bits = encode_server_command_batch(
                (
                    ServerCommand(
                        INVENTORY_INFO_COMMAND,
                        response_inventory,
                        LOCAL_PLAYER_ACTOR_ID,
                    ),
                ),
                include_terminator=True,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected inventory sort: {exc}"
            )
            return

        with s.flow_lock:
            s.gameplay_inventory = updated_inventory
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label="inventory-page-sorted"
        )
        CharacterService._request_account_save(
            s, "inventory page sorted", flush=True
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] sorted inventory page "
            f"[{page_start}, {page_end}) containing "
            f"{len(sorted_item_ids)} item(s)"
        )

    def _handle_inventory_split(
            self, s: Session, action, inventory, next_item_id: int) -> None:
        """Apply ``SplitStack`` operations 0x1e..0x21.

        The client splits its own stack optimistically but cannot invent the
        second stack's item id, so it needs the authoritative pair back before
        the new stack becomes usable.  Without this the split silently
        reverted on the next inventory snapshot.
        """

        storage_name = SPLIT_STACK_OPERATIONS[action.operation]
        try:
            if inventory is None:
                raise ValueError("player inventory is not initialized")
            arguments = tuple(action.arguments)
            if len(arguments) != 2:
                raise ValueError(
                    f"split operation 0x{action.operation:02x} needs a "
                    f"remaining amount and a slot, got {arguments}"
                )
            remaining_amount, storage_slot = (
                int(arguments[0]), int(arguments[1])
            )
            builder = InventoryMutationBuilder(inventory, next_item_id)
            source_amount = int(builder.item(action.item_id).amount)
            moved = builder.split_stack(
                action.item_id,
                remaining_amount,
                storage_name=storage_name,
                storage_slot=storage_slot,
            )
            mutation = builder.finish()
            packet, packet_bits = encode_server_command_batch(
                (
                    ServerCommand(
                        INVENTORY_INFO_COMMAND,
                        mutation.delta,
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
        except ValueError as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected stack split for item "
                f"{action.item_id}: {exc}"
            )
            return
        with s.flow_lock:
            s.gameplay_inventory = mutation.inventory
            s.gameplay_next_item_id = mutation.next_item_id
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label="inventory-stack-split"
        )
        CharacterService._request_account_save(
            s, "inventory stack split", flush=True
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] split {moved.template_id!r} x"
            f"{source_amount} into {remaining_amount} + {moved.amount}; "
            f"new stack {moved.item_id} at {storage_name}[{storage_slot}]"
        )

    def _handle_inventory_move(
            self, s: Session, action, inventory, next_item_id: int) -> None:
        """Apply ``MoveItem`` operations 0x07..0x0a (storage relocation).

        Slot allocation is authoritative state: the previous build ignored
        these, so every drag survived only until the next full InventoryInfo
        and items appeared to jump back on relog or map change.
        """

        storage_name = MOVE_ITEM_STORAGE_OPERATIONS[action.operation]
        try:
            if inventory is None:
                raise ValueError("player inventory is not initialized")
            arguments = tuple(action.arguments)
            if len(arguments) != 1:
                raise ValueError(
                    f"move operation 0x{action.operation:02x} needs exactly "
                    f"one destination slot, got {arguments}"
                )
            storage_slot = int(arguments[0])
            was_worn = int(action.item_id) in {
                int(item_id) for item_id in inventory.equipment_slots
            }
            builder = InventoryMutationBuilder(inventory, next_item_id)
            item = builder.move_item(
                action.item_id,
                storage_name=storage_name,
                storage_slot=storage_slot,
            )
            mutation = builder.finish()
            # The client already holds the item object; only the allocation
            # changed. Replaying the definition makes it rebuild the item.
            packet, packet_bits = encode_server_command_batch(
                (
                    ServerCommand(
                        INVENTORY_INFO_COMMAND,
                        equipment_allocation_snapshot(mutation.inventory),
                        LOCAL_PLAYER_ACTOR_ID,
                    ),
                ),
                include_terminator=True,
            )
        except ValueError as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected inventory move for item "
                f"{action.item_id}: {exc}"
            )
            return
        with s.flow_lock:
            s.gameplay_inventory = mutation.inventory
            s.gameplay_next_item_id = mutation.next_item_id
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label="inventory-slot-moved"
        )
        if was_worn:
            # Taken off into a bag cell (the client's drop on a bag sends
            # 0x07): the same follow-ups as an equipment change, the other
            # players' view of us included.
            # FAILURE: "je retire mon arme l'autre me voit toujours avec
            #   l'arme".
            from dso_server.services.item_set_programs import reconcile_item_set_programs
            reconcile_item_set_programs(s)
            CharacterService._achievement_on_equipment_changed(self, s)
            CharacterService._refresh_player_combat_vitals(s)
            if s.gameplay_presence_callback is not None:
                s.gameplay_presence_callback(s, "appearance")
        CharacterService._request_account_save(
            s, "inventory slot changed", flush=True
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] moved item {action.item_id} "
            f"({item.template_id}) to {storage_name}[{storage_slot}]"
        )

    def _handle_inventory_storage_swap(
            self, s: Session, action, inventory, next_item_id: int) -> None:
        '''Apply operation 0x0b when both items are in storage.'''

        try:
            arguments = tuple(action.arguments)
            if inventory is None:
                raise ValueError('player inventory is not initialized')
            if len(arguments) != 1 or not int(arguments[0]):
                raise ValueError(
                    'storage swap requires one displaced item id'
                )
            builder = InventoryMutationBuilder(inventory, next_item_id)
            item, displaced = builder.swap_storage_items(
                action.item_id, int(arguments[0])
            )
            mutation = builder.finish()
            packet, packet_bits = encode_server_command_batch(
                (
                    ServerCommand(
                        INVENTORY_INFO_COMMAND,
                        equipment_allocation_snapshot(mutation.inventory),
                        LOCAL_PLAYER_ACTOR_ID,
                    ),
                ),
                include_terminator=True,
            )
        except ValueError as exc:
            print(
                f'[{GAMEPLAY_SERVICE_NAME}] rejected inventory swap for '
                f'item {action.item_id}: {exc}'
            )
            return
        with s.flow_lock:
            s.gameplay_inventory = mutation.inventory
            s.gameplay_next_item_id = mutation.next_item_id
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label='inventory-slots-swapped'
        )
        CharacterService._request_account_save(
            s, 'inventory slots swapped', flush=True
        )
        print(
            f'[{GAMEPLAY_SERVICE_NAME}] swapped stored items '
            f'{item.item_id} ({item.template_id}) and '
            f'{displaced.item_id} ({displaced.template_id})'
        )

    def _return_displaced_equipment(
            self, inventory, displaced_ids, former_slots):
        """Re-equip a displaced item into the slot the mover just left.

        Only when that item may legally occupy exactly those slots and they
        are still free; otherwise it stays where ``equip_inventory_item`` put
        it.  Returns the inventory and the ids that were swapped back.
        """

        items_by_id = {
            int(item.item_id): item for item in inventory.items
        }
        wanted = set(int(slot) for slot in former_slots)
        swapped: list[int] = []
        for displaced_id in displaced_ids:
            displaced_item = items_by_id.get(int(displaced_id))
            if displaced_item is None:
                continue
            occupied = tuple(
                int(slot)
                for equipped_item_id, equipped_slots
                in inventory.equipment_slots.items()
                if int(equipped_item_id) != int(displaced_id)
                for slot in equipped_slots
            )
            if wanted.intersection(occupied):
                break
            try:
                allowed = self.static_database.item_equipment_slots(
                    displaced_item.template_id,
                    preferred_slots=tuple(former_slots),
                    occupied_slots=occupied,
                )
            except Exception:
                continue
            if set(int(slot) for slot in allowed) != wanted:
                continue
            inventory = equip_inventory_item(
                inventory, int(displaced_id), tuple(allowed)
            )
            swapped.append(int(displaced_id))
            break
        return inventory, tuple(swapped)

    def _handle_inventory_item_action(
            self, s: Session, payload: bytes) -> None:
        # Socket insertion updates the item, consumes the rune, and persists
        # one copy-on-write inventory. Serialize that whole transaction with
        # source-map snapshots so a concurrent handoff cannot save the old
        # empty socket after the client has already completed the action.
        with s.gameplay_inventory_action_lock:
            with s.flow_lock:
                if s.gameplay_exit_transition_started:
                    return
            self._handle_inventory_item_action_locked(s, payload)

    def _handle_inventory_item_action_locked(
            self, s: Session, payload: bytes) -> None:
        from dso_server.services.rune_progression import unlocked_rune_slots

        action = decode_inventory_item_action(payload)
        with s.flow_lock:
            s.gameplay_client_command_state[0x0055] = action
            inventory = s.gameplay_inventory
            # Preserve occupied legacy extra slots before an unequip/swap.
            s.gameplay_rune_trinket_unlocked_slots = set(unlocked_rune_slots(s))
            currency = s.gameplay_currency_state
            next_item_id = s.gameplay_next_item_id
            character = s.gameplay_character
            item_set_effects = dict(s.gameplay_item_set_effects)
            socket_effects = dict(s.gameplay_socket_effects)
            next_status_effect_instance_id = (
                s.gameplay_next_status_effect_instance_id
            )
        CharacterService._accept_gameplay_state_command(s, 0x0055)

        if action.operation == 0x19:
            self._handle_inventory_sort(s, action, inventory)
            return

        if action.action == UNLOCK_GEM_SOCKET_ACTION:
            try:
                if inventory is None or currency is None:
                    raise ValueError("player inventory is not initialized")
                mutation = unlock_gem_socket(
                    inventory,
                    action.item_id,
                    static_database_path=self.static_database_path,
                    owner_level=int(
                        getattr(s.gameplay_character, "level", 0) or 0
                    ),
                )
                updated_currency, updated_ic = debit_currency(
                    currency,
                    PurchasePrice(rc=mutation.price_rc),
                    ic_balance=s.gameplay_currency_ic,
                )
                packet, packet_bits = encode_socket_unlock_sequence(
                    mutation.inventory,
                    equipment_item=mutation.equipment_item,
                    currency=updated_currency,
                    actor_id=LOCAL_PLAYER_ACTOR_ID,
                )
            except (OSError, ValueError) as exc:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] rejected socket unlock "
                    f"for item {action.item_id}: {exc}"
                )
                return
            with s.flow_lock:
                s.gameplay_currency_state = updated_currency
                s.gameplay_inventory = mutation.inventory
                s.gameplay_currency_ic = updated_ic
            CharacterService._send_world_loot_state_batch(
                s,
                packet,
                packet_bits,
                label="gem-socket-unlocked",
            )
            CharacterService._request_account_save(
                s, "equipment gem socket unlocked", flush=True
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] unlocked socket "
                f"{mutation.unlocked_socket_count}/"
                f"{mutation.maximum_socket_count} on item "
                f"{mutation.equipment_item_id}; debited "
                f"{mutation.price_rc} RC"
            )
            return

        if (
            action.action == INSERT_GEM_ACTION
            and inventory is not None
            and any(
                int(candidate.item_id) == int(action.item_id)
                and candidate.template_id == "gem_crafting"
                for candidate in inventory.items
            )
        ):
            try:
                if not action.field_0c:
                    raise ValueError(
                        "gem upgrade has no target gem item id"
                    )
                # The Gem Crafting Joker reuses the insert-gem inventory
                # action. Its target is the gem to upgrade, not a socket.
                action.insertion_socket_index
                mutation = upgrade_gem(
                    inventory,
                    action.item_id,
                    action.field_0c,
                    static_database_path=self.static_database_path,
                    next_item_id=next_item_id,
                )
                packet, packet_bits = encode_server_command_batch(
                    (
                        ServerCommand(
                            INVENTORY_INFO_COMMAND,
                            mutation.delta,
                            LOCAL_PLAYER_ACTOR_ID,
                        ),
                    ),
                    include_terminator=True,
                )
            except (OSError, ValueError) as exc:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] rejected gem upgrade "
                    f"0x{action.action:03x}: {exc}"
                )
                return
            with s.flow_lock:
                s.gameplay_inventory = mutation.inventory
                s.gameplay_next_item_id = mutation.next_item_id
            CharacterService._send_world_loot_state_batch(
                s,
                packet,
                packet_bits,
                label="gem-upgraded",
            )
            CharacterService._request_account_save(
                s, "equipment gem upgraded", flush=True
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] upgraded gem "
                f"{action.field_0c}: {mutation.previous_template_id} -> "
                f"{mutation.upgraded_template_id} using item "
                f"{action.item_id}"
            )
            return

        if (
            action.action == INSERT_GEM_ACTION
            and inventory is not None
            and any(
                int(candidate.item_id) == int(action.item_id)
                and candidate.template_id == "unique_gem_crafting"
                for candidate in inventory.items
            )
        ):
            try:
                if not action.field_0c:
                    raise ValueError(
                        "rune upgrade has no target rune item id"
                    )
                # The client reuses InventoryCommand's insert-item opcode for
                # the Rune Crafting Joker drop. Its target is a rune stack,
                # not an equipment socket container.
                action.insertion_socket_index
                mutation = upgrade_rune(
                    inventory,
                    action.item_id,
                    action.field_0c,
                    static_database_path=self.static_database_path,
                    next_item_id=next_item_id,
                )
                mutation.delta.field_1e0 = False
                packet, packet_bits = encode_server_command_batch(
                    (
                        ServerCommand(
                            INVENTORY_INFO_COMMAND,
                            mutation.delta,
                            LOCAL_PLAYER_ACTOR_ID,
                        ),
                    ),
                    include_terminator=True,
                )
            except (OSError, ValueError) as exc:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] rejected rune upgrade "
                    f"0x{action.action:03x}: {exc}"
                )
                return
            with s.flow_lock:
                s.gameplay_inventory = mutation.inventory
                s.gameplay_next_item_id = mutation.next_item_id
            CharacterService._send_world_loot_state_batch(
                s,
                packet,
                packet_bits,
                label="rune-upgraded",
            )
            CharacterService._request_account_save(
                s, "rune upgraded", flush=True
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] upgraded rune "
                f"{action.field_0c}: {mutation.previous_template_id} -> "
                f"{mutation.upgraded_template_id} using item "
                f"{action.item_id}"
            )
            return

        if action.action in {REMOVE_GEM_ACTION, INSERT_GEM_ACTION}:
            socket_effect_evaluation = None
            socket_effect_transition = None
            try:
                if inventory is None:
                    raise ValueError("player inventory is not initialized")
                if action.action == REMOVE_GEM_ACTION:
                    mutation = remove_gem(
                        inventory,
                        action.item_id,
                        static_database_path=self.static_database_path,
                        next_item_id=next_item_id,
                        socket_index=action.removal_socket_index,
                    )
                    next_item_id = mutation.next_item_id
                else:
                    if not action.field_0c:
                        raise ValueError(
                            "gem insertion has no destination item id"
                        )
                    mutation = insert_gem(
                        inventory,
                        action.item_id,
                        action.field_0c,
                        static_database_path=self.static_database_path,
                        socket_index=action.insertion_socket_index,
                    )
                if action.action == REMOVE_GEM_ACTION:
                    if currency is None:
                        raise ValueError(
                            "player currency is not initialized"
                        )
                    if mutation.response_inventory is None:
                        raise ValueError(
                            "gem removal has no InventoryInfo response"
                        )
                    packet, packet_bits = encode_gem_removal_sequence(
                        mutation.response_inventory,
                        gem_item_id=mutation.gem_item_id,
                        replaced_gem_item_id=(
                            mutation.replaced_gem_item_id
                        ),
                        equipment_item=mutation.equipment_item,
                        currency=currency,
                        actor_id=LOCAL_PLAYER_ACTOR_ID,
                    )
                else:
                    packet, packet_bits = encode_item_info_response(
                        mutation.equipment_item,
                        actor_id=LOCAL_PLAYER_ACTOR_ID,
                    )
                if self.socket_effect_database is not None:
                    socket_effect_evaluation = (
                        self.socket_effect_database.evaluate_inventory(
                            mutation.inventory
                        )
                    )
                    socket_effect_transition = (
                        self.socket_effect_database.reconcile_effects(
                            socket_effect_evaluation,
                            socket_effects,
                            actor_id=LOCAL_PLAYER_ACTOR_ID,
                            next_instance_id=(
                                next_status_effect_instance_id
                            ),
                            server_tick=(
                                int(time.perf_counter() * 25) & 0xFFFFFFFF
                            ),
                        )
                    )
            except (OSError, ValueError) as exc:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] rejected gem action "
                    f"0x{action.action:03x}: {exc}"
                )
                return
            with s.flow_lock:
                s.gameplay_inventory = mutation.inventory
                s.gameplay_next_item_id = next_item_id
                if socket_effect_transition is not None:
                    s.gameplay_socket_effects = (
                        socket_effect_transition.effects
                    )
                    s.gameplay_socket_effect_evaluation = (
                        socket_effect_evaluation
                    )
                    active_cadence_keys = {
                        ("periodic-aura", aura.key)
                        for aura in socket_effect_evaluation.periodic_auras
                    }
                    s.gameplay_socket_event_state = {
                        key: value
                        for key, value in s.gameplay_socket_event_state.items()
                        if (
                            not (
                                isinstance(key, tuple)
                                and key[:1] == ("periodic-aura",)
                            )
                            or key in active_cadence_keys
                        )
                    }
                    s.gameplay_next_status_effect_instance_id = (
                        socket_effect_transition.next_instance_id
                    )
            CharacterService._send_world_loot_state_batch(
                s,
                packet,
                packet_bits,
                label=(
                    "gem-removed"
                    if action.action == REMOVE_GEM_ACTION
                    else "gem-inserted"
                ),
            )
            if (
                socket_effect_transition is not None
                and socket_effect_transition.status_effect is not None
            ):
                s.send_reliable_ordered(
                    encode_status_effect_batch(
                        socket_effect_transition.status_effect,
                        actor_id=LOCAL_PLAYER_ACTOR_ID,
                    ),
                    track=True,
                    label="socket-effects-updated",
                )
            CharacterService._refresh_player_combat_vitals(s)
            CharacterService._request_account_save(
                s, "equipment gems changed", flush=True
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] "
                f"{'removed' if action.action == REMOVE_GEM_ACTION else 'inserted'} "
                f"gem {mutation.gem_template_id!r} on item "
                f"{mutation.equipment_item_id} socket "
                f"{mutation.socket_index}"
            )
            return

        if action.operation == 0x12:
            # Retail predicts this flag change on right click; commit it so
            # inventory refreshes and reconnects cannot undo identification.
            with s.flow_lock:
                current = s.gameplay_inventory
                if current is None:
                    raise ValueError("player inventory is not initialized")
                item = next((i for i in current.items if i.item_id == action.item_id), None)
                if item is None or not any(action.item_id in getattr(current, name)
                                          for name in ("storage_58", "storage_80", "storage_a8", "storage_d0")):
                    raise ValueError("identification requires an owned stored item")
                if not item.field_3c:
                    return
                inventory = copy.deepcopy(current)
                item = next(i for i in inventory.items if i.item_id == action.item_id)
                item.field_3c = False
                packet, packet_bits = encode_item_info_response(item, actor_id=LOCAL_PLAYER_ACTOR_ID)
                s.gameplay_inventory = inventory
            CharacterService._send_world_loot_state_batch(
                s, packet, packet_bits, label="inventory-item-identified")
            CharacterService._request_account_save(s, "inventory item identified", flush=True)
            return

        if action.operation in SPLIT_STACK_OPERATIONS:
            self._handle_inventory_split(s, action, inventory, next_item_id)
            return

        if action.operation in MOVE_ITEM_STORAGE_OPERATIONS:
            self._handle_inventory_move(s, action, inventory, next_item_id)
            return

        if action.operation == 0x0B:
            try:
                swap_arguments = tuple(action.arguments)
            except ValueError as exc:
                print(
                    f'[{GAMEPLAY_SERVICE_NAME}] rejected inventory swap '
                    f'for item {action.item_id}: {exc}'
                )
                return
            if (
                inventory is not None
                and swap_arguments
                and action.item_id not in inventory.equipment_slots
                and all(
                    int(item_id) not in inventory.equipment_slots
                    for item_id in swap_arguments
                )
            ):
                self._handle_inventory_storage_swap(
                    s, action, inventory, next_item_id
                )
                return
            if (
                inventory is not None
                and len(swap_arguments) == 1
                and action.item_id in inventory.equipment_slots
                and int(swap_arguments[0]) not in inventory.equipment_slots
                and int(swap_arguments[0])
            ):
                # Normalize the inverse drag direction. Equipping the stored
                # target automatically moves this equipped source into the
                # target's exact storage allocation.
                source_item_id = int(action.item_id)
                target_item_id = int(swap_arguments[0])
                action = type(action)(
                    target_item_id,
                    action.action,
                    action.field_08,
                    source_item_id,
                    action.field_10,
                    action.field_14,
                    action.field_18,
                )

        # NetworkCommandCreatorProperty emits distinct allocation operations:
        #   1     equip (optional destination EquipmentSlotId argument)
        #   2..5  store in ordinary/extra/bank/extra-bank inventory
        #   6..0xa move to equipment/ordinary/extra/bank/extra-bank slot
        #   0x0b  occupied-slot swap (arguments are displaced item IDs)
        #   0x1e..0x21 split a stack into ordinary/extra/bank/extra-bank
        # All are applied by the client before they arrive.  Ignoring operation
        # 1 left empty-slot accessory equips client-only; a later operation
        # 0x0b replacement then exposed the stale authoritative allocation and
        # appeared to remove every such item.
        if action.operation in {
            0x01, 0x02, 0x03, 0x04, 0x05,
            MOVE_ITEM_EQUIPMENT_OPERATION, 0x0B,
        }:
            try:
                if inventory is None:
                    raise ValueError("player inventory is not initialized")
                # Repair saves made while the incorrect slot 34 was still used
                # for a second essence before interpreting a switch/equip.
                # Otherwise the client cannot display that item, and a later
                # X-key swap can displace it into ordinary inventory.
                inventory, _relocated_essences = (
                    repair_ammunition_slot_allocations(inventory)
                )
                item = next(
                    (
                        item for item in inventory.items
                        if item.item_id == action.item_id
                    ),
                    None,
                )
                if item is None:
                    raise ValueError(
                        f"inventory item {action.item_id} does not exist"
                    )
                arguments = tuple(action.arguments)
                replaced_ids: tuple[int, ...] = ()
                slots: tuple[int, ...] = ()
                storage_name: str | None = None
                storage_slot: int | None = None

                if action.operation in {
                    0x01, MOVE_ITEM_EQUIPMENT_OPERATION, 0x0B
                }:
                    if action.operation != 0x0B:
                        # Operation 6 is MoveItemToEquipmentAt: the same
                        # destination-slot argument as an ordinary equip.
                        if len(arguments) > 1:
                            raise ValueError(
                                "equip operation has more than one "
                                "destination slot"
                            )
                        preferred_slots = tuple(
                            int(value) for value in arguments
                        )
                        # Older clients may still name the retired secondary
                        # slot. Treat that request as the supported partner;
                        # the item's SlotType resolver still rejects items
                        # that cannot occupy an essence slot.
                        preferred_slots = tuple(
                            SECONDARY_AMMUNITION_SLOT
                            if slot == LEGACY_SECONDARY_AMMUNITION_SLOT
                            else slot
                            for slot in preferred_slots
                        )
                    else:
                        replaced_ids = tuple(
                            int(value) for value in arguments if value
                        )
                        preferred_slots = tuple(
                            slot
                            for replaced_id in replaced_ids
                            for slot in inventory.equipment_slots.get(
                                replaced_id, ()
                            )
                        )
                    # The X-key essence toggle can arrive as a move/swap with
                    # no explicit destination or displaced-item list. Infer
                    # the opposite supported essence slot from the current
                    # allocation so the client and authoritative inventory
                    # cannot disagree about which essence is active.
                    if (
                        not preferred_slots
                        and action.operation in {
                            MOVE_ITEM_EQUIPMENT_OPERATION, 0x0B
                        }
                    ):
                        former_ammunition_slots = tuple(
                            int(slot)
                            for slot in inventory.equipment_slots.get(
                                int(action.item_id), ()
                            )
                            if int(slot) in AMMUNITION_SLOTS
                        )
                        if former_ammunition_slots:
                            current_slot = former_ammunition_slots[0]
                            preferred_slots = (
                                SECONDARY_AMMUNITION_SLOT
                                if current_slot == ACTIVE_AMMUNITION_SLOT
                                else ACTIVE_AMMUNITION_SLOT,
                            )
                    occupied_slots = tuple(
                        int(slot)
                        for equipped_item_id, equipped_slots
                        in inventory.equipment_slots.items()
                        if int(equipped_item_id) != int(action.item_id)
                        for slot in equipped_slots
                    )
                    slots = self.static_database.item_equipment_slots(
                        item.template_id,
                        preferred_slots=preferred_slots,
                        occupied_slots=occupied_slots,
                        unavailable_slots=tuple(
                            slot for slot in (18, 19)
                            if slot not in s.gameplay_rune_trinket_unlocked_slots
                        ),
                    )
                    if (
                        action.operation != 0x0B
                        and preferred_slots
                        and not set(preferred_slots).intersection(slots)
                    ):
                        raise ValueError(
                            f"item {item.template_id!r} cannot be equipped "
                            f"in requested slot {preferred_slots[0]}"
                        )
                    # An equipped item moving between equivalent slots (the
                    # essence switch, one ring onto the other) swaps with
                    # whatever it displaces: the displaced item takes the
                    # mover's former slot instead of dropping into a bag,
                    # which is where ``equip_inventory_item`` would put it.
                    former_slots = tuple(
                        int(slot)
                        for slot in inventory.equipment_slots.get(
                            int(action.item_id), ()
                        )
                    )
                    displaced_ids = tuple(
                        int(equipped_item_id)
                        for equipped_item_id, equipped_slots
                        in inventory.equipment_slots.items()
                        if int(equipped_item_id) != int(action.item_id)
                        and set(slots).intersection(
                            int(slot) for slot in equipped_slots
                        )
                    )
                    inventory = equip_inventory_item(
                        inventory, action.item_id, slots
                    )
                    swapped_ids: tuple[int, ...] = ()
                    if (
                        former_slots
                        and displaced_ids
                        and not set(former_slots).intersection(slots)
                    ):
                        inventory, swapped_ids = (
                            self._return_displaced_equipment(
                                inventory, displaced_ids, former_slots
                            )
                        )
                else:
                    swapped_ids = ()
                    if len(arguments) > 1:
                        raise ValueError(
                            "store operation has more than one destination "
                            "slot"
                        )
                    storage_name = {
                        0x02: "storage_58",
                        0x03: "storage_80",
                        0x04: "storage_a8",
                        0x05: "storage_d0",
                    }[action.operation]
                    storage_slot = (
                        int(arguments[0]) if arguments else None
                    )
                    inventory = store_inventory_item(
                        inventory,
                        action.item_id,
                        storage_name=storage_name,
                        storage_slot=storage_slot,
                    )
                # InventoryCommand already applied the visual/object swap on
                # the client.  Acknowledge only the resulting allocations;
                # replaying existing ItemInfo objects makes it rebuild them.
                response_inventory = equipment_allocation_snapshot(inventory)
                item_set_evaluation = None
                item_set_transition = None
                socket_effect_evaluation = None
                socket_effect_transition = None
                if self.item_set_database is not None:
                    item_set_evaluation = (
                        self.item_set_database.evaluate_inventory(
                            inventory,
                            class_name=(
                                CLASS_NAMES[int(character.class_id)]
                                if character is not None
                                else ""
                            ),
                        )
                    )
                    item_set_transition = (
                        self.item_set_database.reconcile_effects(
                            item_set_evaluation,
                            item_set_effects,
                            actor_id=LOCAL_PLAYER_ACTOR_ID,
                            next_instance_id=(
                                next_status_effect_instance_id
                            ),
                            server_tick=(
                                int(time.perf_counter() * 25)
                                & 0xFFFFFFFF
                            ),
                        )
                    )
                if self.socket_effect_database is not None:
                    socket_effect_evaluation = (
                        self.socket_effect_database.evaluate_inventory(
                            inventory
                        )
                    )
                    socket_effect_transition = (
                        self.socket_effect_database.reconcile_effects(
                            socket_effect_evaluation,
                            socket_effects,
                            actor_id=LOCAL_PLAYER_ACTOR_ID,
                            next_instance_id=(
                                item_set_transition.next_instance_id
                                if item_set_transition is not None
                                else next_status_effect_instance_id
                            ),
                            server_tick=(
                                int(time.perf_counter() * 25)
                                & 0xFFFFFFFF
                            ),
                        )
                    )
                commands = [
                    ServerCommand(
                        INVENTORY_INFO_COMMAND,
                        response_inventory,
                        LOCAL_PLAYER_ACTOR_ID,
                    )
                ]
                if (
                    item_set_transition is not None
                    and item_set_transition.status_effect is not None
                ):
                    commands.append(
                        ServerCommand(
                            STATUS_EFFECT_COMMAND,
                            item_set_transition.status_effect,
                            LOCAL_PLAYER_ACTOR_ID,
                        )
                    )
                if (
                    socket_effect_transition is not None
                    and socket_effect_transition.status_effect is not None
                ):
                    commands.append(
                        ServerCommand(
                            STATUS_EFFECT_COMMAND,
                            socket_effect_transition.status_effect,
                            LOCAL_PLAYER_ACTOR_ID,
                        )
                    )
                packet, packet_bits = encode_server_command_batch(
                    tuple(commands),
                    include_terminator=True,
                )
            except ValueError as exc:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] rejected equipment action "
                    f"{action.item_id}: {exc}"
                )
                return
            with s.flow_lock:
                previous_inventory = s.gameplay_inventory
                s.gameplay_inventory = inventory
                if item_set_transition is not None:
                    s.gameplay_item_set_effects = (
                        item_set_transition.effects
                    )
                    s.gameplay_item_set_evaluation = (
                        item_set_evaluation
                    )
                    s.gameplay_next_status_effect_instance_id = (
                        item_set_transition.next_instance_id
                    )
                if socket_effect_transition is not None:
                    s.gameplay_socket_effects = (
                        socket_effect_transition.effects
                    )
                    s.gameplay_socket_effect_evaluation = (
                        socket_effect_evaluation
                    )
                    active_cadence_keys = {
                        ("periodic-aura", aura.key)
                        for aura in socket_effect_evaluation.periodic_auras
                    }
                    s.gameplay_socket_event_state = {
                        key: value
                        for key, value in s.gameplay_socket_event_state.items()
                        if (
                            not (
                                isinstance(key, tuple)
                                and key[:1] == ("periodic-aura",)
                            )
                            or key in active_cadence_keys
                        )
                    }
                    s.gameplay_next_status_effect_instance_id = (
                        socket_effect_transition.next_instance_id
                    )
            CharacterService._send_world_loot_state_batch(
                s,
                packet,
                packet_bits,
                label="equipment-snapshot",
            )
            from dso_server.services.item_set_programs import (
                cleanse_unequipped_items, reconcile_item_set_programs,
            )
            reconcile_item_set_programs(s)
            cleanse_unequipped_items(s, previous_inventory, inventory)
            CharacterService._achievement_on_equipment_changed(self, s)
            CharacterService._refresh_player_combat_vitals(s)
            CharacterService._request_account_save(
                s, "equipment changed", flush=True
            )
            if s.gameplay_presence_callback is not None:
                s.gameplay_presence_callback(s, "appearance")
            if action.operation in {
                0x01, MOVE_ITEM_EQUIPMENT_OPERATION, 0x0B
            }:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] equipped item "
                    f"{action.item_id} ({item.template_id}) in slots {slots}; "
                    f"operation=0x{action.operation:02x}; "
                    f"replaced={replaced_ids}; "
                    f"swapped_into_former_slot={swapped_ids}; "
                    f"active_sets="
                    f"{getattr(item_set_evaluation, 'active_set_ids', ())}"
                )
            else:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] stored equipped item "
                    f"{action.item_id} ({item.template_id}) in "
                    f"{storage_name}[{storage_slot if storage_slot is not None else 'first-free'}]; "
                    f"operation=0x{action.operation:02x}; active_sets="
                    f"{getattr(item_set_evaluation, 'active_set_ids', ())}"
                )
            return

        # Discard is predicted by the client, but it must still remove the
        # item and its allocation from authoritative state.  Previously this
        # fell through to the payment-package path, leaving an invisible item
        # occupying what looked like a free slot after the next refresh.
        if action.operation == 0x00:
            try:
                if inventory is None:
                    raise ValueError("player inventory is not initialized")
                builder = InventoryMutationBuilder(inventory, next_item_id)
                discarded = builder.consume(action.item_id)
                mutation = builder.finish()
                packet, packet_bits = encode_server_command_batch(
                    (
                        ServerCommand(
                            INVENTORY_INFO_COMMAND,
                            mutation.delta,
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
            except ValueError as exc:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] rejected inventory discard "
                    f"for item {action.item_id}: {exc}"
                )
                return
            with s.flow_lock:
                s.gameplay_inventory = mutation.inventory
                s.gameplay_next_item_id = mutation.next_item_id
            CharacterService._send_world_loot_state_batch(
                s, packet, packet_bits, label="inventory-discarded"
            )
            CharacterService._request_account_save(
                s, "inventory item discarded", flush=True
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] discarded inventory item "
                f"{discarded.item_id} ({discarded.template_id})"
            )
            return

        # Some legacy/event mounts deliberately exist only as reusable
        # inventory items and have no _Template_StickerbookItem row. Their
        # ordinary use operation still points to the same authored
        # cast_mount_* -> ride_mount_* status-effect flow.
        if action.operation == 0x13 and inventory is not None:
            from dso_server.services.rune_progression import handle_rune_extension_use
            from dso_server.services.recipe_progression import handle_recipe_item_use

            if handle_rune_extension_use(self, s, action.item_id):
                return
            if handle_recipe_item_use(self, s, action.item_id):
                return
            from dso_server.services.membership_items import handle_membership_item_use
            if handle_membership_item_use(self, s, action.item_id):
                return
            inventory_item = next(
                (
                    candidate
                    for candidate in inventory.items
                    if candidate.item_id == action.item_id
                ),
                None,
            )
            if CharacterService._arm_travel_item(
                self, s, inventory_item
            ):
                return
            mount_definition = (
                load_inventory_mount_definition(
                    self.static_database_path,
                    inventory_item.template_id,
                )
                if inventory_item is not None
                else None
            )
            if mount_definition is not None:
                CharacterService._toggle_mount_definition(
                    self,
                    s,
                    mount_definition,
                    source="inventory item",
                )
                return

            # Costumes and pets are ``Switchable`` items: using one from the
            # bag wears it, using it again takes it off, and nothing is
            # consumed.  This is the same toggle the sticker book drives,
            # resolved from the item's own status effect when the costume is
            # not a collectible.  Previously these fell through to the
            # package/chest path and were rejected as "not an inventory
            # container".
            cosmetic_definition = (
                load_cosmetic_definition(
                    self.static_database_path,
                    inventory_item.template_id,
                )
                if inventory_item is not None
                else None
            )
            if (
                cosmetic_definition is not None
                and cosmetic_definition.kind in {"costume", "pet", "decoration"}
            ):
                CharacterService._toggle_switchable_sticker(
                    self, s, cosmetic_definition
                )
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] toggled "
                    f"{cosmetic_definition.kind} "
                    f"{inventory_item.template_id!r} from inventory item "
                    f"{action.item_id}"
                )
                return

            # Shadow souls are reusable companions.  Their inventory action
            # must not fall through to the consumable/chest route, which
            # either rejects the item or consumes the quest reward.
            mercenary_definition = (
                load_sticker_definition(
                    self.static_database_path,
                    inventory_item.template_id,
                )
                if inventory_item is not None
                else None
            )
            if (
                mercenary_definition is not None
                and mercenary_definition.kind == "mercenary"
            ):
                CharacterService._activate_mercenary(
                    self, s, mercenary_definition, source="inventory item"
                )
                return

            # Currency items are consumed into either the fixed legacy
            # balances or the keyed currency bag.  Perform this before the
            # LootGenItem/payment-package route because currencies such as a
            # gilded clover are authored as ItemCurrency rather than boxes.
            try:
                if currency is None:
                    raise ValueError("player currency is not initialized")
                currency_credit = (
                    credit_currency_item(
                        self.static_database_path,
                        currency,
                        dict(s.gameplay_game_currencies),
                        inventory_item,
                    )
                    if inventory_item is not None
                    else None
                )
                if currency_credit is not None:
                    builder = InventoryMutationBuilder(
                        inventory, next_item_id
                    )
                    builder.consume(action.item_id)
                    currency_mutation = builder.finish()
                    commands = [
                        ServerCommand(
                            INVENTORY_INFO_COMMAND,
                            currency_mutation.delta,
                            LOCAL_PLAYER_ACTOR_ID,
                        ),
                        ServerCommand(
                            CURRENCY_CHANGED_COMMAND,
                            currency_credit.currency,
                            LOCAL_PLAYER_ACTOR_ID,
                        ),
                    ]
                    if currency_credit.game_currency_update is not None:
                        commands.append(
                            ServerCommand(
                                GAME_CURRENCY_CHANGED_COMMAND,
                                currency_credit.game_currency_update,
                                LOCAL_PLAYER_ACTOR_ID,
                            )
                        )
                    commands.append(
                        ServerCommand(
                            INVENTORY_INFO_COMMAND,
                            currency_mutation.final,
                            LOCAL_PLAYER_ACTOR_ID,
                        )
                    )
                if currency_credit is not None:
                    packet, packet_bits = encode_server_command_batch(
                        tuple(commands), include_terminator=True
                    )
            except ValueError as exc:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] rejected currency item "
                    f"{action.item_id}: {exc}"
                )
                return
            if currency_credit is not None:
                with s.flow_lock:
                    s.gameplay_inventory = currency_mutation.inventory
                    s.gameplay_next_item_id = (
                        currency_mutation.next_item_id
                    )
                    s.gameplay_currency_state = currency_credit.currency
                    s.gameplay_game_currencies = dict(
                        currency_credit.game_currencies
                    )
                CharacterService._request_account_save(
                    s, "currency item consumed", flush=True
                )
                deferred_achievement_packets = []
                if currency_credit.game_currency_update is not None:
                    CharacterService._achievement_feed(
                        self, s, "collectedgamecurrency",
                        (inventory_item.template_id,), count=currency_credit.amount,
                        deferred_packets=deferred_achievement_packets,
                    )
                CharacterService._send_world_loot_state_batch(
                    s, packet, packet_bits, label="currency-item-consumed"
                )
                for achievement_packet, achievement_bits, achievement_label in deferred_achievement_packets:
                    CharacterService._send_world_loot_state_batch(
                        s, achievement_packet, achievement_bits, label=achievement_label
                    )
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] consumed currency item "
                    f"{inventory_item.template_id!r}: credited "
                    f"{currency_credit.amount} {currency_credit.name}"
                )
                return

            # Buff extracts, flat-stat tonics, and health potions are all
            # ordinary operation-0x13 consumables. Their effect identity,
            # parameters, duration and exclusive family come from static.db4.
            if CharacterService._handle_consumable_item_use(
                self, s, action.item_id
            ):
                return

        if action.operation != 0x13:
            detail = ""
            if action.operation == 0x19:
                try:
                    detail = (
                        f" (action=0x{action.action:08x}, "
                        f"argument_count={action.argument_count}, "
                        f"arguments={action.arguments}, "
                        f"fields=({action.field_08}, {action.field_0c}, "
                        f"{action.field_10}, {action.field_14}, "
                        f"{action.field_18}))"
                    )
                except ValueError as exc:
                    detail = f" (could not decode arguments: {exc})"
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] ignored unsupported inventory "
                f"item operation 0x{action.operation:02x} for "
                f"item {action.item_id}{detail}"
            )
            return

        try:
            if inventory is None or currency is None:
                raise ValueError("player inventory is not initialized")
            item = next(
                (
                    item for item in inventory.items
                    if item.item_id == action.item_id
                ),
                None,
            )
            if item is None:
                raise ValueError(
                    f"inventory item {action.item_id} does not exist"
                )
            if self._activate_season_pass_item(
                s, action.item_id, item.template_id,
            ):
                return
            try:
                contents = load_payment_package_contents(
                    self.static_database_path,
                    item.template_id,
                )
                mutation = open_payment_package(
                    inventory,
                    currency,
                    contents,
                    action.item_id,
                    next_item_id=next_item_id,
                )
                route_name = "payment-package"
                route_id = contents.package_id
                game_currencies = dict(s.gameplay_game_currencies)
                game_currency_updates = ()
                receipt_grants = tuple(
                    ItemReceiptGrant(
                        reward.definition.template_id,
                        reward.amount,
                        materialized=(
                            reward.definition.item_category != "Currency"
                        ),
                        include_template_id=(
                            reward.definition.item_category
                            in ITEM_RECEIVED_TEMPLATE_CATEGORIES
                        ),
                    )
                    for reward in contents.rewards
                )
            except ValueError:
                contents = roll_chest_contents(
                    self.static_database_path,
                    item.template_id,
                    map_path=CHEST_CONTENTS_MAP_PATH,
                    player_level=(
                        max(1, int(character.level))
                        if character is not None
                        else 1
                    ),
                    class_name=(
                        CLASS_NAMES[int(character.class_id)]
                        if character is not None
                        else ""
                ),
            )
                mutation = open_mapped_chest(
                    inventory,
                    currency,
                    dict(s.gameplay_game_currencies),
                    contents,
                    action.item_id,
                    static_database_path=self.static_database_path,
                    next_item_id=next_item_id,
                    player_level=(
                        max(1, int(character.level))
                        if character is not None
                        else 1
                    ),
                    class_name=(
                        CLASS_NAMES[int(character.class_id)]
                        if character is not None
                        else ""
                    ),
                    map_difficulty=int(s.gameplay_map_difficulty),
                )
                route_name = "mapped-chest"
                route_id = contents.box_template_id
                game_currencies = dict(mutation.game_currencies)
                game_currency_updates = mutation.game_currency_updates
                receipt_grants = tuple(
                    ItemReceiptGrant(
                        reward.definition.template_id,
                        reward.amount,
                        materialized=not (
                            reward.definition.item_category in {
                                "Currency",
                                "ItemCurrency",
                                "NoAutoUseCurrency",
                            }
                            or reward.definition.template_id
                            in GAME_CURRENCY_BY_WIRE_ID
                        ),
                        include_template_id=(
                            reward.definition.item_category
                            in ITEM_RECEIVED_TEMPLATE_CATEGORIES
                        ),
                    )
                    for reward in contents.rewards
                )
            receipts, receipt_next_item_id = build_item_receipts(
                inventory,
                mutation.inventory,
                (item.item_id for item in mutation.delta.items),
                mutation.delta.field_158,
                receipt_grants,
                next_item_id=mutation.next_item_id,
            )
            commands = [
                ServerCommand(
                    INVENTORY_INFO_COMMAND,
                    mutation.delta,
                    LOCAL_PLAYER_ACTOR_ID,
                ),
                ServerCommand(
                    CURRENCY_CHANGED_COMMAND,
                    mutation.currency,
                    LOCAL_PLAYER_ACTOR_ID,
                ),
            ]
            commands.extend(
                ServerCommand(
                    GAME_CURRENCY_CHANGED_COMMAND,
                    update,
                    LOCAL_PLAYER_ACTOR_ID,
                )
                for update in game_currency_updates
            )
            commands.extend(
                ServerCommand(
                    MESSAGE_COMMAND,
                    item_received_message(receipt),
                    LOCAL_PLAYER_ACTOR_ID,
                )
                for receipt in receipts
            )
            commands.extend((
                ServerCommand(
                    INVENTORY_INFO_COMMAND,
                    mutation.final,
                    LOCAL_PLAYER_ACTOR_ID,
                ),
                ServerCommand(
                    QUICKBAR_INFO_COMMAND,
                    s.gameplay_quickbar_info,
                    LOCAL_PLAYER_ACTOR_ID,
                ),
                ServerCommand(
                    CURRENCY_CHANGED_COMMAND,
                    mutation.currency,
                    LOCAL_PLAYER_ACTOR_ID,
                ),
            ))
            packet, packet_bits = encode_server_command_batch(
                tuple(commands),
                include_terminator=True,
            )
        except (OSError, ValueError) as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] inventory item action "
                f"{action.item_id}/{action.action} could not open a fixed "
                f"package or mapped chest: {exc}"
            )
            # The client may optimistically remove a right-clicked chest.
            # Send the authoritative inventory snapshot back when the server
            # rejects the open, so an unsupported/under-level chest is not
            # lost from the visible inventory.
            if inventory is not None:
                try:
                    packet, packet_bits = encode_server_command_batch(
                        (
                            ServerCommand(
                                INVENTORY_INFO_COMMAND,
                                inventory,
                                LOCAL_PLAYER_ACTOR_ID,
                            ),
                        ),
                        include_terminator=True,
                    )
                    s.send_reliable_ordered_split(
                        packet,
                        total_bit_length=packet_bits,
                        track=True,
                        label_prefix="rejected-chest-inventory-resync",
                    )
                except (AttributeError, OSError, ValueError) as resync_exc:
                    print(
                        f"[{GAMEPLAY_SERVICE_NAME}] could not restore "
                        f"inventory after rejected item action: {resync_exc}"
                    )
            return

        with s.flow_lock:
            previous_game_currencies = dict(s.gameplay_game_currencies)
            s.gameplay_currency_state = mutation.currency
            s.gameplay_inventory = mutation.inventory
            s.gameplay_game_currencies = game_currencies
            s.gameplay_next_item_id = receipt_next_item_id
        CharacterService._persist_gameplay_session(
            self, s, reason=f'{route_name} opened', flush=True
        )
        deferred_achievement_packets = []
        for currency_id, balance in game_currencies.items():
            gained = int(balance) - int(previous_game_currencies.get(currency_id, 0))
            if gained > 0:
                CharacterService._achievement_feed(
                    self, s, "collectedgamecurrency", (currency_id,), count=gained,
                    deferred_packets=deferred_achievement_packets,
                )
        s.send_reliable_ordered_split(
            packet,
            total_bit_length=packet_bits,
            track=True,
            label_prefix=(
                f'{route_name}-open-{route_name}-open'
            ),
        )
        for achievement_packet, achievement_bits, achievement_label in deferred_achievement_packets:
            CharacterService._send_world_loot_state_batch(
                s, achievement_packet, achievement_bits, label=achievement_label
            )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] opened {route_name} "
            f"{route_id!r} from item {action.item_id}; delivered "
            f"{[(reward.definition.template_id, reward.amount) for reward in contents.rewards]} "
            f"(currency_changed={mutation.currency_changed}, "
            f"{len(packet)} bytes/{packet_bits} bits)"
        )
