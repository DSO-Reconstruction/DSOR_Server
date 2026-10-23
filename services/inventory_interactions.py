"""NPC, crafting, salvage, quick-slot, and prop inventory interactions."""

from __future__ import annotations

import time

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.gameplay.local_map_state import (
    LocalNPCDefinition,
    load_local_event_set_flags,
    load_local_npc_definitions,
    local_npc_actor_id,
)
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

        new_slots = tuple(slots)
        preserved_mount = False
        with s.flow_lock:
            current = s.gameplay_quick_slots
            bars = (
                list(current.quickbars)
                if isinstance(current, QuickSlotsInfo)
                else []
            )
            # The client sends an initial full QuickSlots snapshot while
            # rebuilding the map UI. That snapshot can still contain an empty
            # O/mount slot even though the authoritative initialization sent
            # the saved mount shortcut. Do not let that startup echo erase it.
            if (
                time.monotonic()
                < float(getattr(
                    s, "gameplay_quick_slots_init_echo_until", 0.0
                ))
                and new_slots[8].kind == 0xFFFFFFFF
            ):
                saved_mount = next((
                    bar[8]
                    for bar in bars
                    if len(bar) > 8
                    and bar[8].kind != 0xFFFFFFFF
                    and "mount" in str(bar[8].resource_id or "").casefold()
                ), None)
                if saved_mount is not None:
                    updated_slots = list(new_slots)
                    updated_slots[8] = saved_mount
                    new_slots = tuple(updated_slots)
                    preserved_mount = True
            empty_bar = tuple(
                new_slots[i] if i >= 8 else QuickSlotInfo(0xFFFFFFFF, None)
                for i in range(17)
            )
            while len(bars) <= bar_index:
                bars.append(empty_bar)
            changed = bars[bar_index] != new_slots
            if changed:
                bars[bar_index] = new_slots
                for idx in range(len(bars)):
                    if idx == bar_index:
                        continue
                    bar_l = list(bars[idx])
                    while len(bar_l) < 17:
                        bar_l.append(QuickSlotInfo(0xFFFFFFFF, None))
                    for slot_idx in range(8, 17):
                        bar_l[slot_idx] = new_slots[slot_idx]
                    bars[idx] = tuple(bar_l)
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
            + (" (preserved mount during map initialization)"
               if preserved_mount else "")
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
        """Use the map instance's event sets for both dialogs and quest refreshes.

        Quest-gated NPCs (e.g. a0002 hub smith_01/spellweaver_01 behind
        a0002_start_hub_02/04) have no map-local event driving them, so the
        shared runtime never enables their set. The quest log is the durable
        record of which gated target the player is meeting, so its open
        encounter/talk steps re-activate their flags per player
        (see _quest_reveal_flags).
        """
        gameplay_map = s.gameplay_map or DEFAULT_GAMEPLAY_MAP
        try:
            reveal = frozenset(
                CharacterService._quest_reveal_flags(self, s, gameplay_map)
            )
        except Exception:
            reveal = frozenset()
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        # _load_initialization_world publishes NPCs using the selected map
        # access group's event flag. Keep that flag in the request-side
        # catalog too, regardless of whether this session has a live shared
        # event runtime; otherwise the client can see the NPC but its
        # NPCRequest/Encounter actor id cannot be resolved after a map hop.
        access_group_flag = frozenset()
        access_group_id = getattr(s, "gameplay_map_access_group_id", None)
        if access_group_id:
            access_group_flag = frozenset(
                (str(access_group_id).casefold(),)
            )
        if runtime is None:
            active = frozenset(
                CharacterService._event_catalog(self).active_event_flags()
            ) | frozenset(reveal) | access_group_flag
            definitions = load_local_npc_definitions(
                self.static_database_path, gameplay_map,
                include_inactive_event_sets=False,
                active_event_flags=active,
            )
            if NPC_TRACE:
                print(
                    f"[NPC-TRACE] _active_npc_definitions(runtime=None) "
                    f"map={gameplay_map!r} flags={sorted(active)} "
                    f"access_group={getattr(s, 'gameplay_map_access_group_id', None)!r} "
                    f"npc_count={len(definitions)}"
                )
            return definitions
        with runtime.lock:
            running = frozenset(runtime.active_flags())
        try:
            scheduled = frozenset(
                CharacterService._event_catalog(self).active_event_flags()
            )
        except Exception:
            scheduled = frozenset()
        # Quest reveal does not override server-driven map-local events;
        # those sets stay gated on the running event so instance phasing is
        # preserved. Client-only events cannot be started by the server
        # runtime, however, so their per-player quest reveal flag must remain
        # eligible when resolving the quest NPC behind that event set.
        try:
            catalog = runtime.catalog
            reveal = frozenset(
                flag for flag in reveal
                if (lambda ev: (
                    ev is None
                    or not ev.applies_to(gameplay_map)
                    or ev.client_only
                ))(
                    catalog.event(str(flag).casefold())
                )
            )
        except Exception:
            pass
        active = (
            frozenset(running)
            | frozenset(scheduled)
            | frozenset(reveal)
            | access_group_flag
        )
        definitions = load_local_npc_definitions(
            self.static_database_path, gameplay_map,
            include_inactive_event_sets=False,
            active_event_flags=active,
        )
        if NPC_TRACE:
            print(
                f"[NPC-TRACE] _active_npc_definitions(runtime=active) "
                f"map={gameplay_map!r} flags={sorted(active)} "
                f"running={sorted(running)} npc_count={len(definitions)}"
            )
        return definitions

    def _resolve_npc_interaction(self, s: Session, actor_id: int) -> LocalNPCDefinition | None:
        definitions = CharacterService._active_npc_definitions(self, s)
        if int(actor_id) != 0:
            result = next((d for d in definitions if int(actor_id) in {
                int(d.actor_id), local_npc_actor_id(d),
            }), None)
            if NPC_TRACE:
                if result is not None:
                    print(
                        f"[NPC-TRACE] _resolve_npc_interaction "
                        f"actor_id={actor_id} found=yes "
                        f"template={result.template_id!r}"
                    )
                else:
                    print(
                        f"[NPC-TRACE] _resolve_npc_interaction "
                        f"actor_id={actor_id} found=NO "
                        f"(searched {len(definitions)} defs)"
                    )
            return result
        # Static-scene NPCs may request their role with actor zero before
        # their dynamic actor is attached. Prefer the normal close-range
        # lookup first.
        with s.flow_lock:
            encounter = s.gameplay_monster_encounter
            last_move = s.gameplay_last_move
            spawn_position = s.gameplay_spawn_position
        player_position = (
            encounter.coordinate_frame.wire_to_world(last_move.relative_position)
            if encounter is not None and last_move is not None else spawn_position
        )
        if player_position is None:
            if NPC_TRACE:
                print("[NPC-TRACE] actor zero has no player position")
            return None

        def distance(candidate):
            return math.hypot(
                candidate.position[0] - player_position[0],
                candidate.position[2] - player_position[2],
            )

        definition = min(definitions, key=distance, default=None)
        if definition is not None and distance(definition) <= 8.0:
            return definition

        # A quest-gated NPC can be clickable through the client's generic
        # actor-zero lookup before the player is close enough for the usual
        # interaction range. Resolve only a target revealed by this character's
        # active quest, and only within a short approach radius.
        try:
            map_id = s.gameplay_map or DEFAULT_GAMEPLAY_MAP
            reveal = CharacterService._quest_reveal_flags(self, s, map_id)
            if not reveal:
                return None
            event_sets = load_local_event_set_flags(
                self.static_database_path, map_id,
            )
            targets = load_local_npc_definitions(
                self.static_database_path, map_id,
                include_inactive_event_sets=True,
            )
            quest_targets = tuple(
                candidate for candidate in targets
                if int(getattr(candidate, "event_set_row", -1)) >= 0
                and event_sets.get(
                    int(candidate.event_set_row), frozenset(),
                ) & reveal
            )
            target = min(quest_targets, key=distance, default=None)
            if target is not None and distance(target) <= 24.0:
                if NPC_TRACE:
                    print(
                        f"[NPC-TRACE] actor zero resolved quest target "
                        f"{target.template_id!r} at {distance(target):.1f}m"
                    )
                return target
        except Exception as exc:
            if NPC_TRACE:
                print(
                    f"[NPC-TRACE] actor-zero quest target lookup failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        return None

    def _process_npc_interaction(self, s: Session, actor_id: int, *, label: str) -> None:
        definition = CharacterService._resolve_npc_interaction(self, s, actor_id)
        if definition is None:
            if NPC_TRACE:
                print(
                    f"[NPC-TRACE] _process_npc_interaction {label} "
                    f"FAILED: actor_id={actor_id} map={s.gameplay_map!r} "
                    f"access_group="
                    f"{getattr(s, 'gameplay_map_access_group_id', None)!r} "
                    f"runtime_attached="
                    f"{getattr(s, 'gameplay_map_event_runtime', None) is not None}"
                )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] {label} references unknown "
                f"local actor {actor_id}"
            )
            return

        # Actor zero can be a map-load lookup rather than an actual encounter.
        # Return the target NPCInfo while the player approaches, but only feed
        # quest progress once the lookup is inside the normal interaction range.
        in_interaction_range = True
        if int(actor_id) == 0:
            with s.flow_lock:
                encounter = s.gameplay_monster_encounter
                last_move = s.gameplay_last_move
                spawn_position = s.gameplay_spawn_position
            player_position = (
                encounter.coordinate_frame.wire_to_world(
                    last_move.relative_position
                )
                if encounter is not None and last_move is not None
                else spawn_position
            )
            if player_position is not None:
                in_interaction_range = math.hypot(
                    definition.position[0] - player_position[0],
                    definition.position[2] - player_position[2],
                ) <= 8.0
        if in_interaction_range:
            # An encounter advances CompleteTasks first, so the dialog that
            # follows already offers the turn-in (retail order).
            CharacterService._quest_on_npc_encounter(
                self, s, definition.template_id,
            )
        npc_info = CharacterService._quest_decorate_npc_info(
            # Actor 0 is a client-side proximity lookup, not the NPC's
            # identity. Once resolved, return NPCInfo under the concrete
            # actor the client knows from NewNPC; an actor-0 response cannot
            # be matched to the clicked NPC and its interaction list is lost.
            self, s, local_npc_info(definition),
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
            f"[{GAMEPLAY_SERVICE_NAME}] handled {label} for "
            f"{definition.template_id!r} actor {npc_info.actor_id}: "
            f"sent typed NPCInfo category {npc_info.category}"
        )

    def _handle_npc_encounter(self, s: Session, payload: bytes) -> None:
        request = decode_simple_gameplay_command(payload)
        CharacterService._accept_gameplay_state_command(s, ENCOUNTER_COMMAND)
        with s.flow_lock:
            s.gameplay_client_command_state[ENCOUNTER_COMMAND] = request
        self._process_npc_interaction(s, request.value, label="EncounterCommand")

    def _handle_npc_request(self, s: Session, payload: bytes) -> None:
        request = decode_simple_gameplay_command(payload)
        CharacterService._accept_gameplay_state_command(s, 0x0029)
        with s.flow_lock:
            s.gameplay_client_command_state[0x0029] = request
        self._process_npc_interaction(s, request.value, label="NPCRequest")

    def _handle_enchantment_transfer(self, s: Session, request) -> None:
        """Exchange all or one selected enchantment between two workbench items."""

        print(
            f"[{GAMEPLAY_SERVICE_NAME}] received enchantment-transfer request: "
            f"recipe={request.recipe_id!r}, payment={request.payment_currency}, "
            f"items={request.item_ids}, amount={request.amount}, "
            f"mode={request.mode}, selections="
            f"({request.field_10}, {request.field_14})"
        )
        with s.flow_lock:
            inventory = copy.deepcopy(s.gameplay_inventory)
            currency = copy.deepcopy(s.gameplay_currency_state)
            next_item_id = s.gameplay_next_item_id
        try:
            if inventory is None or currency is None:
                raise ValueError("player inventory is not initialized")
            recipe_key = str(request.recipe_id).strip().casefold()
            if recipe_key[-1:].isdigit():
                recipe_key = recipe_key[:-1]
            normalized_recipe = "".join(
                character for character in recipe_key if character.isalnum()
            )
            is_transfer_recipe = "enchantmenttransfer" in normalized_recipe
            all_transfer = is_transfer_recipe and (
                "all" in normalized_recipe
                or normalized_recipe == "enchantmenttransfer"
                or (
                    int(request.field_10) == 0xFFFFFFFF
                    and int(request.field_14) == 0xFFFFFFFF
                )
            )
            single_transfer = is_transfer_recipe and not all_transfer
            if not (all_transfer or single_transfer):
                raise ValueError(
                    "payment selector 5 has unsupported crafting rule "
                    f"{request.recipe_id!r}"
                )
            if len(request.item_ids) != 2 or int(request.amount) != 1:
                raise ValueError(
                    "enchantment transfer requires exactly two items and one execution"
                )
            first_id, second_id = (int(value) for value in request.item_ids)
            if first_id == second_id:
                raise ValueError("enchantment transfer requires two different items")

            transfer_factor = load_enchantment_transfer_factor(
                self.static_database_path, request.recipe_id
            )
            refresh_inventory_economy_values(
                self.static_database_path,
                inventory,
                item_ids=(first_id, second_id),
                force_melt_value=True,
            )
            builder = InventoryMutationBuilder(inventory, next_item_id)
            first = builder.item(first_id)
            second = builder.item(second_id)
            first_enchantments = copy.deepcopy(first.enchantments)
            second_enchantments = copy.deepcopy(second.enchantments)
            if not first_enchantments or not second_enchantments:
                raise ValueError(
                    "both items need at least one enchantment to transfer"
                )
            glyph_cost = transfer_factor * (
                max(0, int(first.field_34))
                + max(0, int(second.field_34))
            )
            if glyph_cost <= 0:
                raise ValueError("enchantment transfer has no Glyphs of Power cost")
            if int(currency.glyph_of_power) < glyph_cost:
                raise ValueError(
                    f"enchantment transfer requires {glyph_cost} Glyphs of Power; "
                    f"balance is {int(currency.glyph_of_power)}"
                )
            updated_currency = replace(
                currency,
                delta=int(currency.glyph_of_power) - glyph_cost,
                notify=True,
            )
            first_index = int(request.field_10)
            second_index = int(request.field_14)
            if all_transfer:
                # All-transfer exchanges the complete enchantment sets. Item
                # rarities can provide different line counts, so requiring
                # equal lengths incorrectly rejected otherwise valid pairs.
                first.enchantments = second_enchantments
                second.enchantments = first_enchantments
            else:
                if not (
                    0 <= first_index < len(first_enchantments)
                    and 0 <= second_index < len(second_enchantments)
                ):
                    raise ValueError(
                        "selected enchantment position is outside its item"
                    )
                first_enchantments[first_index], second_enchantments[second_index] = (
                    second_enchantments[second_index],
                    first_enchantments[first_index],
                )
                first.enchantments = first_enchantments
                second.enchantments = second_enchantments

            # Give the client fresh item objects so the workbench result and
            # inventory manager cannot keep displaying cached enchantments
            # under the old item IDs. Slot/equipment references are remapped
            # and published through InventoryInfo's replacement map.
            builder.rekey_item(first_id)
            builder.rekey_item(second_id)
            mutation = builder.finish()
            # These are replacement allocations with new IDs, so retain the
            # mutation flag that tells the client to apply changed-item and
            # old-to-new reference mappings.
            mutation.delta.field_1e0 = True
            result_items = tuple(copy.deepcopy(item) for item in mutation.changed_items)
            # The client accepts the workbench result but does not reliably
            # refresh existing item IDs from its delta. A full authoritative
            # InventoryInfo after CraftingResult mirrors the map-entry refresh
            # that otherwise makes the exchanged enchantments visible.
            full_inventory_refresh = copy.deepcopy(mutation.inventory)
            full_inventory_refresh.changed_item_ids = []
            full_inventory_refresh.field_158 = {}
            full_inventory_refresh.field_1e0 = False
            packet, packet_bits = encode_server_command_batch(
                (
                    ServerCommand(INVENTORY_INFO_COMMAND, mutation.delta, LOCAL_PLAYER_ACTOR_ID),
                    ServerCommand(CURRENCY_CHANGED_COMMAND, updated_currency, LOCAL_PLAYER_ACTOR_ID),
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
                    ServerCommand(
                        INVENTORY_INFO_COMMAND,
                        full_inventory_refresh,
                        LOCAL_PLAYER_ACTOR_ID,
                    ),
                ),
                include_terminator=True,
            )
        except (OSError, ValueError) as exc:
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected enchantment transfer: {exc}")
            return
        with s.flow_lock:
            s.gameplay_inventory = mutation.inventory
            s.gameplay_currency_state = updated_currency
            s.gameplay_next_item_id = mutation.next_item_id
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label="workbench-enchantment-transfer"
        )
        CharacterService._request_account_save(
            s, "workbench enchantments transferred", flush=True
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] transferred enchantments between "
            f"items {first_id}->{result_items[0].item_id} and "
            f"{second_id}->{result_items[1].item_id}; charged "
            f"{glyph_cost} Glyphs of Power; sent full inventory refresh"
        )

    def _handle_crafting(self, s: Session, payload: bytes) -> None:
        request = decode_crafting_request(payload)
        CharacterService._accept_gameplay_state_command(s, CRAFTING_COMMAND)
        with s.flow_lock:
            s.gameplay_client_command_state[CRAFTING_COMMAND] = request
        normalized_recipe_id = "".join(
            character
            for character in str(request.recipe_id).casefold()
            if character.isalnum()
        )
        if (
            request.payment_currency == 5
            or "enchantmenttransfer" in normalized_recipe_id
        ):
            self._handle_enchantment_transfer(s, request)
            return
        with s.flow_lock:
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
                player_level=int(character.level),
                # Keep a mixed rolling deployment of the interaction handler
                # and recipe protocol backward-compatible. Older recipe
                # objects do not carry the scaling flags yet.
                scale_rc_to_player_level=getattr(
                    recipe, "scale_price_rc", False
                ),
                scale_vc_to_player_level=getattr(
                    recipe, "scale_price_vc", False
                ),
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
