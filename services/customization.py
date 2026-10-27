"""Equipment appearance, mounts, costumes, pets, and sticker-book interactions."""

from __future__ import annotations

from dataclasses import replace
import sqlite3

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.protocol.item_customization_protocol import (
    APPLY_ITEM_CUSTOMIZATION_COMMAND, CUSTOMIZATION_SLOTS,
    decode_item_customization_changes, apply_item_customizations,
)
from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
from dso_server.services.admin_effects import (
    ADMIN_EFFECTS, matching_admin_effects, limited_admin_speed_entries,
)


# Dedicated templates keep admin toggles separate from native gameplay buffs.
ADMIN_SPEED_EFFECT_RESOURCE = ADMIN_EFFECTS["speed"]["resource"]
ADMIN_SPEED_EFFECT_VALUES = ADMIN_EFFECTS["speed"]["values"]


class CustomizationService:
    """Equipment appearance, mounts, costumes, pets, and sticker-book interactions."""

    @staticmethod
    def _ensure_mercenary_quickbar_shortcut(
            s: Session, resource_id: str) -> bool:
        """Put an owned Shadow Soul in the first free sticker shortcut slot."""

        resource_id = str(resource_id).strip()
        if resource_id.startswith("sb_mercenary_"):
            resource_id = "item_mercenary_" + resource_id.removeprefix(
                "sb_mercenary_"
            )
        if not resource_id.startswith("item_mercenary_"):
            return False

        with s.flow_lock:
            if resource_id not in s.gameplay_stickerbook_entries:
                return False
            quick_slots = s.gameplay_quick_slots
            if not isinstance(quick_slots, QuickSlotsInfo) or not quick_slots.quickbars:
                return False
            bars = list(quick_slots.quickbars)
            first_bar = list(bars[0])
            if len(first_bar) != 17:
                return False
            if any(
                slot.kind == 2 and slot.resource_id == resource_id
                for bar in bars
                for slot in bar
            ):
                return False

            target = 9
            first_bar[target] = QuickSlotInfo(2, resource_id)
            for b_idx in range(len(bars)):
                b_list = list(bars[b_idx])
                while len(b_list) < 17:
                    b_list.append(QuickSlotInfo(0xFFFFFFFF, None))
                b_list[target] = QuickSlotInfo(2, resource_id)
                if b_list[8].kind == 2 and "mercenary" in str(b_list[8].resource_id or ""):
                    b_list[8] = QuickSlotInfo(0xFFFFFFFF, None)
                bars[b_idx] = tuple(b_list)
            updated = QuickSlotsInfo(tuple(bars))
            s.gameplay_quick_slots = updated

        CharacterService._send_stickerbook_commands(
            s,
            (ServerCommand(
                QUICK_SLOTS_INFO_COMMAND,
                updated,
                LOCAL_PLAYER_ACTOR_ID,
            ),),
            label="mercenary-quickbar-shortcut",
        )
        return True

    @staticmethod
    def _select_mercenary_quickbar_shortcut(
            s: Session, resource_id: str) -> bool:
        """Point the mercenary quick slot at the Shadow Soul just activated."""

        resource_id = str(resource_id).strip()
        if resource_id.startswith("sb_mercenary_"):
            resource_id = "item_mercenary_" + resource_id.removeprefix(
                "sb_mercenary_"
            )
        if not resource_id.startswith("item_mercenary_"):
            return False

        with s.flow_lock:
            if resource_id not in s.gameplay_stickerbook_entries:
                return False
            quick_slots = s.gameplay_quick_slots
            if not isinstance(quick_slots, QuickSlotsInfo) or not quick_slots.quickbars:
                return False
            bars = list(quick_slots.quickbars)
            first_bar = list(bars[0])
            if len(first_bar) != 17:
                return False

            def mercenary_resource(slot):
                value = str(slot.resource_id or "").strip()
                if value.startswith("sb_mercenary_"):
                    value = "item_mercenary_" + value.removeprefix(
                        "sb_mercenary_"
                    )
                return value if value.startswith("item_mercenary_") else ""

            target = 9
            has_slot8_mercenary = first_bar[8].kind == 2 and bool(mercenary_resource(first_bar[8]))
            if not has_slot8_mercenary and first_bar[target].resource_id == resource_id:
                return False

            first_bar[target] = QuickSlotInfo(2, resource_id)
            for b_idx in range(len(bars)):
                b_list = list(bars[b_idx])
                while len(b_list) < 17:
                    b_list.append(QuickSlotInfo(0xFFFFFFFF, None))
                b_list[target] = QuickSlotInfo(2, resource_id)
                if b_list[8].kind == 2 and bool(mercenary_resource(b_list[8])):
                    b_list[8] = QuickSlotInfo(0xFFFFFFFF, None)
                bars[b_idx] = tuple(b_list)
            updated = QuickSlotsInfo(tuple(bars))
            s.gameplay_quick_slots = updated

        CharacterService._send_stickerbook_commands(
            s,
            (ServerCommand(
                QUICK_SLOTS_INFO_COMMAND,
                updated,
                LOCAL_PLAYER_ACTOR_ID,
            ),),
            label="active-mercenary-quickbar-shortcut",
        )
        return True

    @staticmethod
    def _mercenary_owner_aura_effects(
            s: Session, monster_id: str) -> tuple[int, ...]:
        """Resolve player-facing buffs authored by a Shadow Soul's auras."""

        combat = s.gameplay_combat_state
        if combat is None:
            return ()
        path = combat.encounter.static_database_path
        is_hub = str(combat.encounter.map_id).casefold() in {
            "a0200_kingscity", "b1200_hub",
        }
        with sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True
        ) as database:
            row = database.execute(
                "SELECT PermanentStatusEffect FROM _Template_Monster "
                "WHERE Id=? LIMIT 1",
                (str(monster_id),),
            ).fetchone()
            if row is None:
                return ()
            effect_ids: list[int] = []
            seen: set[str] = set()
            pending: list[str] = []
            for resource_id in str(row[0] or "").split(";"):
                resource_id = resource_id.strip()
                if not resource_id:
                    continue
                aura = database.execute(
                    "SELECT AuraEffectId, COALESCE(AuraTargets, '') "
                    "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
                    (resource_id,),
                ).fetchone()
                if aura is None or not ({
                    token.strip().casefold()
                    for token in str(aura[1] or "").replace(",", ";").split(";")
                } & {"summoner", "allies"}):
                    continue
                pending.extend(
                    child.strip() for child in str(aura[0] or "").split(";")
                    if child.strip()
                )
            while pending:
                resource_id = pending.pop(0)
                if resource_id in seen:
                    continue
                seen.add(resource_id)
                child = database.execute(
                    "SELECT rowid-1, Groups, COALESCE(StartModifiers, '') "
                    "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
                    (resource_id,),
                ).fetchone()
                if child is None:
                    continue
                groups = {
                    token.strip().casefold()
                    for token in str(child[1] or "").replace(",", ";").split(";")
                }
                if "buffui" in groups:
                    effect_ids.append(int(child[0]))
                    continue
                for instruction in str(child[2] or "").split(";"):
                    parts = [part.strip() for part in instruction.split(",")]
                    if not parts or not parts[0].casefold().startswith(
                        "actorstatuseffect:add"
                    ) or len(parts) < 2:
                        continue
                    options = {
                        part.partition(":")[0].casefold():
                        part.partition(":")[2].casefold()
                        for part in parts[2:] if ":" in part
                    }
                    if options.get("on", "immediate") not in {
                        "immediate", "immediatemanaged",
                    }:
                        continue
                    if is_hub and options.get("disableonruleset") == "hub":
                        continue
                    pending.append(parts[1])
            return tuple(dict.fromkeys(effect_ids))

    @staticmethod
    def _activate_mercenary(
            self, s: Session, definition, *, source: str) -> bool:
        """Activate an authored ShadowSoul item and publish its companion.

        Mercenary items are reusable inventory/sticker-book entries, not
        consumables.  Their ``ShadowSoul:<monster>`` status program starts the
        client effect, while the ordinary player-summon lifecycle creates the
        authoritative actor visible to every player on the map.
        """

        if definition is None or definition.kind != "mercenary":
            return False
        monster_id = ""
        for encoded in str(definition.start_modifiers or "").split(";"):
            kind, separator, arguments = encoded.partition(":")
            if not separator or kind.strip().casefold() != "shadowsoul":
                continue
            monster_id = arguments.split(";", 1)[0].strip()
            break
        if not monster_id:
            raise ValueError(
                f"mercenary {definition.resource_id!r} has no ShadowSoul target"
            )

        with s.flow_lock:
            combat = s.gameplay_combat_state
            character = s.gameplay_character
            move = s.gameplay_last_move
            if (
                not s.connected
                or combat is None
                or character is None
                or (move is None and s.gameplay_spawn_position is None)
            ):
                raise ValueError("mercenary use requires an active map session")
            owner_actor_id = (
                s.gameplay_remote_actor_id
                or CharacterService._remote_actor_id(character.character_id)
            )
            generation = s.gameplay_chase_generation
            tick = int(s.gameplay_chase_server_tick) & 0xFFFFFFFF
            if tick == 0:
                tick = int(
                    move.start_tick if move is not None else server_time() // 40
                ) & 0xFFFFFFFF
            level = max(1, int(character.level))
            class_name = CLASS_NAMES.get(int(character.class_id), "mage")
            position = (
                combat.encounter.coordinate_frame.wire_to_world(
                    move.relative_position
                )
                if move is not None
                else tuple(float(value) for value in s.gameplay_spawn_position)
            )
            yaw = float(getattr(move, "yaw", 0.0))

        summon_definition = load_socket_minion_definition(
            self.static_database_path,
            monster_id,
            1,
            (0.0, 0.0, 0.0, 0.0),
        )
        if summon_definition is None:
            raise ValueError(
                f"mercenary target {monster_id!r} is missing from the static database"
            )
        owner_auras = self._mercenary_owner_aura_effects(s, monster_id)
        summon_definition = replace(
            summon_definition,
            category="mercenary",
            amount=1,
            limit=1,
            lifetime_seconds=0.0,
            owner_despawn_range=float("inf"),
            follows_owner=True,
        )
        # Apply the database-authored status effect locally and to nearby
        # viewers. The summon lifecycle below owns the actual actor/combat;
        # the item's separate CoolDownCategory belongs to the client action UI.
        with s.flow_lock:
            instance_id = int(s.gameplay_next_status_effect_instance_id)
            s.gameplay_next_status_effect_instance_id += 1
            start_tick = int(s.gameplay_chase_server_tick or tick) & 0xFFFFFFFF
        status_entry = StatusEffectEntry(
            effect_id=int(definition.status_effect_wire_id),
            field_04=instance_id,
            # The companion remains active until another Shadow Soul replaces
            # it. A one-second database duration made the buff icon disappear
            # while the summoned actor kept following the player.
            field_0c=0,
            field_14=0,
            field_08=0,
            field_18=0,
            field_10=0,
            field_1c=100,
            field_20=LOCAL_PLAYER_ACTOR_ID,
            field_25=False,
            field_26=False,
            field_39=True,
            has_details=True,
            values=tuple(definition.values),
        )
        aura_entries = []
        with s.flow_lock:
            for effect_id in owner_auras:
                aura_entries.append(StatusEffectEntry(
                    effect_id=int(effect_id),
                    field_04=int(s.gameplay_next_status_effect_instance_id),
                    field_0c=0,
                    # Permanent monster effects and sticker-book collector
                    # buffs carry the server tick rate even with field_0c=0.
                    # A zero tick rate leaves these aura children visible but
                    # prevents the client from evaluating their modifiers.
                    field_14=SERVER_TICKS_PER_SECOND,
                    field_08=start_tick,
                    field_18=0,
                    field_10=0,
                    field_1c=100,
                    field_20=LOCAL_PLAYER_ACTOR_ID,
                    field_25=False,
                    field_26=False,
                    field_39=True,
                    has_details=True,
                    values=(0.0, 0.0, 0.0, 0.0, 0.0),
                ))
                s.gameplay_next_status_effect_instance_id += 1
        aura_entries = tuple(aura_entries)
        with s.flow_lock:
            previous_status = getattr(
                s, "gameplay_mercenary_status_effect", None
            )
            previous_aura = getattr(
                s, "gameplay_mercenary_aura_status_effect", None
            )
            previous_auras = tuple(getattr(
                s, "gameplay_mercenary_aura_status_effects", ()
            ))
            if previous_aura is not None:
                previous_auras = (previous_aura, *previous_auras)
            # The legacy single field repeats the first entry of the list: one
            # removal per aura instance, not two in the same status batch.
            unique_auras = {}
            for previous in previous_auras:
                unique_auras.setdefault(int(previous.field_04), previous)
            previous_auras = tuple(unique_auras.values())
            s.gameplay_mercenary_status_effect = status_entry
            s.gameplay_mercenary_aura_status_effect = (
                aura_entries[0] if aura_entries else None
            )
            s.gameplay_mercenary_aura_status_effects = aura_entries
            s.gameplay_active_mercenary = str(definition.resource_id)
            s.gameplay_mercenary_restore_pending = False
            for previous in (previous_status, *previous_auras):
                if previous is not None:
                    s.gameplay_persistent_status_effects.pop(
                        int(previous.field_04), None
                    )
            s.gameplay_persistent_status_effects[
                int(status_entry.field_04)
            ] = status_entry
            for aura_entry in aura_entries:
                s.gameplay_persistent_status_effects[
                    int(aura_entry.field_04)
                ] = aura_entry
        status_entries = []
        if previous_status is not None:
            status_entries.append(replace(
                previous_status,
                has_details=False,
                values=(),
            ))
        for previous_aura in previous_auras:
            status_entries.append(replace(
                previous_aura,
                has_details=False,
                values=(),
            ))
        status_entries.append(status_entry)
        status_entries.extend(aura_entries)
        status = StatusEffect(False, tuple(status_entries))
        s.send_reliable_ordered(
            encode_status_effect_batch(status, actor_id=LOCAL_PLAYER_ACTOR_ID),
            track=True,
            label="mercenary-shadow-soul-activated",
        )
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(s, ("status_effects", (status,)))

        # The owner aura's BuffUI child is also the server-side modifier
        # source. Rebuild vitals and summon damage after installing it so the
        # health/damage bonuses affect authoritative combat immediately.
        CharacterService._refresh_player_combat_vitals(
            s, inventory_changed=False,
        )
        modifier_sources = tuple(
            CharacterService._active_skill_modifier_sources(s)
        )
        with s.flow_lock:
            inventory = s.gameplay_inventory
        stats = calculate_player_combat_stats(
            combat.encounter.static_database_path,
            cache_owner=s,
            class_name=class_name,
            level=level,
            inventory=inventory,
            modifier_sources=modifier_sources,
        )

        CharacterService._resolve_player_summon(
            s,
            generation,
            ("mercenary-summon", owner_actor_id, start_tick),
            summon_definition,
            int(owner_actor_id),
            position,
            yaw,
            level,
            start_tick,
            (stats.minimum_damage, stats.maximum_damage),
            modifier_sources,
            round_token=CharacterService._pvp_round_token(s),
        )
        quickbar_updated = CharacterService._select_mercenary_quickbar_shortcut(
            s, definition.resource_id
        )
        CharacterService._persist_gameplay_session(
            self,
            s,
            reason=(
                f"Shadow Soul activated: {definition.resource_id}"
                + (" and quickbar shortcut added" if quickbar_updated else "")
            ),
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] activated mercenary "
            f"{definition.resource_id!r} -> {monster_id!r} from {source}"
        )
        return True

    def _handle_item_customization(self, s: Session, payload: bytes) -> None:
        changes = decode_item_customization_changes(payload)
        with s.flow_lock:
            if (s.gameplay_inventory is None or s.gameplay_currency_state is None
                    or s.gameplay_character is None):
                raise ValueError('customization requires initialized player state')
            previous_currency = s.gameplay_currency_state
            mutation, currency, items = apply_item_customizations(
                s.gameplay_inventory, s.gameplay_currency_state, changes,
                database_path=self.static_database_path,
                class_id=s.gameplay_character.class_id, level=s.gameplay_character.level,
                next_item_id=s.gameplay_next_item_id,
            )
            inventory_changed = bool(items) or currency != previous_currency
            response_items = {
                int(item.item_id): item for item in items
            }
            # GameItem::GetSkins returns no render skins when ItemInfo.field_3e
            # is false. Change that flag in authoritative item state, then send
            # the normal ItemUpdateCommand so the local client posts its
            # RefreshItemSkin event. The item and equipment allocation remain
            # intact; only this item's render-skin gate changes.
            for change in changes:
                if not (change.skin or change.effect_skin):
                    continue
                inventory_slot = CUSTOMIZATION_SLOTS[int(change.slot_type)]
                for item_id, slots in mutation.inventory.equipment_slots.items():
                    if inventory_slot not in slots:
                        continue
                    item = next(
                        (
                            value for value in mutation.inventory.items
                            if int(value.item_id) == int(item_id)
                        ),
                        None,
                    )
                    if item is not None:
                        item.field_3e = not bool(change.effect_skin)
                        response_items[int(item.item_id)] = item
                    break
            packets = []
            if inventory_changed:
                packets.append(encode_server_command_batch((
                    ServerCommand(CURRENCY_CHANGED_COMMAND, currency, LOCAL_PLAYER_ACTOR_ID),
                    ServerCommand(INVENTORY_INFO_COMMAND, mutation.delta, LOCAL_PLAYER_ACTOR_ID),
                    ServerCommand(INVENTORY_INFO_COMMAND, mutation.final, LOCAL_PLAYER_ACTOR_ID),
                )))
            packets.extend(
                encode_item_info_response(
                    item, actor_id=LOCAL_PLAYER_ACTOR_ID
                )
                for item in response_items.values()
            )
            s.gameplay_inventory = mutation.inventory
            s.gameplay_currency_state = currency
            s.gameplay_next_item_id = mutation.next_item_id
            s.gameplay_client_command_state[APPLY_ITEM_CUSTOMIZATION_COMMAND] = changes
            hidden_skins = dict(
                getattr(s, "gameplay_hidden_equipment_skins", {})
            )
            for change in changes:
                if change.effect_skin:
                    # Client enum order: AddCustomization, RemoveCustomization,
                    # ShowSkin, HideSkin, AddColor, RemoveColor.
                    hidden_skins[int(change.slot_type)] = change.effect_skin
                elif change.skin:
                    hidden_skins.pop(int(change.slot_type), None)
            s.gameplay_hidden_equipment_skins = hidden_skins
        CharacterService._accept_gameplay_state_command(s, APPLY_ITEM_CUSTOMIZATION_COMMAND)
        for packet, bits in packets:
            CharacterService._send_world_loot_state_batch(
                s, packet, bits, label='item-customization')
        CharacterService._request_account_save(s, 'equipment appearance changed', flush=True)
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(s, 'appearance')

    @staticmethod
    def _accept_gameplay_state_command(s: Session, command_id: int) -> int:
        """Record one decoded client gameplay command occurrence."""

        with s.flow_lock:
            count = s.gameplay_client_command_counts.get(command_id, 0) + 1
            s.gameplay_client_command_counts[command_id] = count
        if VERBOSE_PACKET_LOGGING:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] recorded client command "
                f"0x{command_id:04x} (occurrence {count})"
            )
        return count

    def _toggle_admin_speed(self, s: Session) -> bool:
        """Toggle a durable +100% movement bonus on this character only."""
        return CustomizationService._set_admin_effect(self, s, "speed", None)

    def _set_admin_effect(self, s: Session, effect: str, enabled: bool | None) -> bool:
        preset = ADMIN_EFFECTS.get(effect)
        if preset is None:
            raise ValueError("unknown admin effect")
        catalog = load_status_runtime_catalog(self.static_database_path)
        effect_id = catalog.effect_ids.get(preset["resource"])
        if effect_id is None:
            raise ValueError("admin buff data is missing; install it on the server and client")
        with s.flow_lock:
            if (
                not s.connected or not s.gameplay_mode
                or s.gameplay_character is None
                or not s.gameplay_initialization_sent
                or not s.gameplay_presence_ready
                or s.gameplay_presence_departed
                or s.gameplay_exit_transition_started
            ):
                raise ValueError("the character is not ready; try again after entering the map")
            current = matching_admin_effects(s.gameplay_persistent_status_effects.values(),
                                             effect_id, preset)
            enabled = not current if enabled is None else enabled
            if enabled == bool(current):
                return enabled
            if enabled:
                # Match live mount/sticker additions; map initialization
                # separately rebases saved admin effects to tick zero.
                last_move = s.gameplay_last_move
                start_tick = (
                    int(last_move.start_tick) + 1 if last_move is not None
                    else max(1, int(s.gameplay_chase_server_tick), server_time() // 40)
                ) & 0xFFFFFFFF
                instance_id = s.gameplay_next_status_effect_instance_id
                s.gameplay_next_status_effect_instance_id += 1
                entry = StatusEffectEntry(
                    effect_id=effect_id, field_04=instance_id,
                    field_0c=0, field_14=25, field_08=start_tick, field_18=0,
                    field_10=0, field_1c=100, field_20=LOCAL_PLAYER_ACTOR_ID,
                    field_25=False, field_26=False, field_39=True,
                    has_details=True, values=preset["values"],
                )
                s.gameplay_persistent_status_effects[instance_id] = entry
                entries = (entry,)
            else:
                entries = tuple(
                    replace(entry, has_details=False, values=()) for entry in current
                )
                for entry in current:
                    s.gameplay_persistent_status_effects.pop(entry.field_04)
        if enabled and effect == "speed":
            adjustment = CustomizationService._refresh_admin_speed(
                s, static_database_path=self.static_database_path, publish=False,
            )
            if adjustment is not None:
                entries = tuple(e for e in adjustment.entries if e.has_details)
        status = StatusEffect(clear_existing=False, entries=entries)
        s.send_reliable_ordered(
            encode_status_effect_batch(status, actor_id=LOCAL_PLAYER_ACTOR_ID),
            track=True, label="admin-speed-toggle" if effect == "speed" else "admin-effect",
        )
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(s, ("status_effects", (status,)))
        CharacterService._persist_gameplay_session(self, s, reason="admin effect changed")
        return enabled

    @staticmethod
    def _refresh_admin_speed(
            s: Session, *, stats=None, static_database_path=None, publish=True):
        """Keep the enabled admin bonus within the native Move command range."""
        with s.flow_lock:
            if not s.gameplay_persistent_status_effects or s.gameplay_character is None:
                return None
            combat = s.gameplay_combat_state
        path = static_database_path or (
            combat.encounter.static_database_path if combat is not None
            else DEFAULT_STATIC_DB_PATH
        )
        catalog = load_status_runtime_catalog(path)
        preset = ADMIN_EFFECTS["speed"]
        if not matching_admin_effects(tuple(s.gameplay_persistent_status_effects.values()),
                                     catalog.effect_ids.get(preset["resource"]), preset):
            return None
        sources = CharacterService._active_skill_modifier_sources(s) if stats is None else ()
        with s.flow_lock:
            character = s.gameplay_character
            inventory = s.gameplay_inventory
            previous = dict(s.gameplay_persistent_status_effects)
        if stats is None:
            stats = calculate_player_combat_stats(
                path, cache_owner=s,
                class_name=CLASS_NAMES.get(int(character.class_id), "mage"),
                level=max(1, int(character.level)), inventory=inventory,
                modifier_sources=sources,
            )
        replacements = limited_admin_speed_entries(previous.values(), catalog, stats)
        if not replacements:
            return None
        with s.flow_lock:
            if (s.gameplay_inventory is not inventory or s.gameplay_character is not character
                    or s.gameplay_persistent_status_effects != previous):
                return None
            tick = ((int(s.gameplay_last_move.start_tick) + 1)
                    if s.gameplay_last_move is not None else server_time() // 40) & 0xFFFFFFFF
            entries = []
            for replacement in replacements:
                old = s.gameplay_persistent_status_effects.pop(replacement.field_04)
                # Remove and re-add explicitly, so the client recomputes the
                # modifier even when its template and duration are unchanged.
                replacement = replace(replacement,
                    field_04=s.gameplay_next_status_effect_instance_id,
                    field_08=tick, field_18=0)
                s.gameplay_next_status_effect_instance_id += 1
                s.gameplay_persistent_status_effects[replacement.field_04] = replacement
                entries.extend((replace(old, has_details=False, values=()), replacement))
            status = StatusEffect(False, tuple(entries))
        if publish:
            s.send_reliable_ordered(
                encode_status_effect_batch(status, actor_id=LOCAL_PLAYER_ACTOR_ID),
                track=True, label="admin-speed-adjust",
            )
            if s.gameplay_presence_callback is not None:
                s.gameplay_presence_callback(s, ("status_effects", (status,)))
        return status

    @staticmethod
    def _send_mount_effect(
            s: Session, state, *, active: bool, label: str) -> None:
        effect = mount_status_effect(
            state,
            actor_id=LOCAL_PLAYER_ACTOR_ID,
            active=active,
        )
        packet = encode_status_effect_batch(
            effect,
            actor_id=LOCAL_PLAYER_ACTOR_ID,
        )
        s.send_reliable_ordered(
            packet,
            track=True,
            label=label,
        )
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(
                s, ("status_effect", effect)
            )

    @staticmethod
    def _remove_mount_ride(
            s: Session, *, reason: str, label: str = "mount-ride-remove",
            persist: bool = True) -> bool:
        """Remove the authoritative ride and its persisted status atomically."""

        with s.flow_lock:
            active_ride = s.gameplay_mount_ride
            if active_ride is None:
                return False
            s.gameplay_mount_ride = None
            s.gameplay_persistent_status_effects.pop(
                active_ride.instance_id, None
            )
        CharacterService._send_mount_effect(
            s,
            active_ride,
            active=False,
            label=label,
        )
        CharacterService._refresh_admin_speed(s)
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] unmounted "
            f"{active_ride.definition.mount_resource_id} ({reason})"
        )
        callback = s.gameplay_persist_callback
        if persist and callback is not None:
            callback(s, f"mount removed: {reason}")
        return True

    @staticmethod
    def _cancel_mount_cast(s: Session, reason: str = 'cancelled') -> bool:
        with s.flow_lock:
            state = s.gameplay_mount_cast
            timer = s.gameplay_mount_cast_timer
            if state is None:
                return False
            s.gameplay_mount_cast = None
            s.gameplay_mount_cast_timer = None
            s.gameplay_mount_generation += 1
        if timer is not None:
            timer.cancel()
        CharacterService._send_mount_effect(
            s,
            state,
            active=False,
            label='mount-cast-remove',
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] removed mount cast "
            f"{state.definition.stickerbook_resource_id} ({reason})"
        )
        return True

    def _complete_mount_cast(
            self, s: Session, generation: int) -> None:
        if not self.is_session_current(s):
            return
        with s.flow_lock:
            cast = s.gameplay_mount_cast
            if cast is None or generation != s.gameplay_mount_generation:
                return
            ride_instance_id = s.gameplay_next_status_effect_instance_id
            s.gameplay_next_status_effect_instance_id += 1
            ride = begin_mount_ride(
                cast,
                instance_id=ride_instance_id,
            )
            s.gameplay_mount_cast = None
            s.gameplay_mount_cast_timer = None
            s.gameplay_mount_ride = ride
            ride_effect = mount_status_effect(
                ride,
                actor_id=LOCAL_PLAYER_ACTOR_ID,
                active=True,
            ).entries[0]
            s.gameplay_persistent_status_effects[
                ride_effect.field_04
            ] = ride_effect

        CharacterService._achievement_feed(
            self, s, "use", (ride.definition.stickerbook_resource_id,),
        )

        # Retail puts both 0x004f commands in one reliable 0x85 batch: ride
        # addition first, followed by the completed-cast removal.
        transitions = mount_transition_status_effects(
            cast,
            ride,
            actor_id=LOCAL_PLAYER_ACTOR_ID,
        )
        adjustment = CharacterService._refresh_admin_speed(
            s, static_database_path=self.static_database_path, publish=False,
        )
        if adjustment is not None:
            transitions = (adjustment,) + transitions
        packet = encode_monster_spawn_batch(
            MonsterSpawnBatch(
                tuple(
                    StatusEffectServerCommand(
                        value,
                        LOCAL_PLAYER_ACTOR_ID,
                    )
                    for value in transitions
                )
            )
        )
        s.send_reliable_ordered(
            packet,
            track=True,
            label='mount-cast-to-ride',
        )
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(
                s, ("status_effects", transitions)
            )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] completed mount cast; applied "
            f"{ride.definition.mount_resource_id} to actor "
            f"{LOCAL_PLAYER_ACTOR_ID}"
        )
        CharacterService._persist_gameplay_session(
            self, s, reason='mount summoned'
        )

    def _achievement_on_stickerbook_changed(self, s: Session) -> None:
        """Count owned cosmetics, independent of favorites and tier upgrades."""

        with s.flow_lock:
            owned = tuple(s.gameplay_stickerbook_entries)
        by_kind = {}
        for resource in owned:
            definition = load_sticker_definition(self.static_database_path, resource)
            if definition is not None:
                by_kind.setdefault(definition.kind, set()).add(definition.resource_id)
        for kind, parameter in (("costume", "Costumes"), ("mount", "Mounts"),
                                ("pet", "Pets"), ("emote", "Emotes")):
            CharacterService._achievement_feed(
                self, s, "stickerbook", (parameter,),
                progress=len(by_kind.get(kind, ())),
            )

    def _handle_add_stickerbook_from_inventory(
            self, s: Session, payload: bytes) -> None:
        """Consume one inventory item and permanently unlock its cosmetic."""

        request = decode_add_stickerbook_from_inventory(payload)
        with s.flow_lock:
            inventory = copy.deepcopy(s.gameplay_inventory)
            next_item_id = int(s.gameplay_next_item_id)
            owned = dict(s.gameplay_stickerbook_entries)
            persistent_effects = dict(
                s.gameplay_persistent_status_effects
            )
            next_status_effect_instance_id = int(
                s.gameplay_next_status_effect_instance_id
            )
            s.gameplay_client_command_state[
                ADD_STICKER_BOOK_FROM_INVENTORY_COMMAND
            ] = request
        CharacterService._accept_gameplay_state_command(
            s, ADD_STICKER_BOOK_FROM_INVENTORY_COMMAND
        )
        if inventory is None:
            raise ValueError("player inventory is not initialized")
        item = next(
            (
                entry
                for entry in inventory.items
                if entry.item_id == request.item_id
            ),
            None,
        )
        if item is None:
            raise ValueError(
                f"sticker-book source item {request.item_id} is missing"
            )
        definition = load_sticker_definition(
            self.static_database_path, item.template_id
        )
        if definition is None:
            raise ValueError(
                f"item {item.template_id!r} is not a live sticker-book item"
            )
        if any(alias in owned for alias in definition.aliases):
            raise ValueError(
                f"sticker-book item {definition.resource_id!r} is owned"
            )

        mutation = consume_sticker_inventory_item(
            inventory,
            request.item_id,
            next_item_id=next_item_id,
        )
        entry = StickerbookEntry(2, 0, definition.resource_id)
        owned[definition.resource_id] = entry
        collector_transition = reconcile_collector_effects(
            self.static_database_path,
            set(owned),
            persistent_effects,
            actor_id=LOCAL_PLAYER_ACTOR_ID,
            next_instance_id=next_status_effect_instance_id,
            server_tick=(server_time() // 40) & 0xFFFFFFFF,
        )
        commands = [
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
                ServerCommand(
                    STICKERBOOK_ITEM_INFO_COMMAND,
                    StickerbookItemInfo(1, entry),
                    LOCAL_PLAYER_ACTOR_ID,
                ),
        ]
        if collector_transition.status_effect is not None:
            commands.append(
                ServerCommand(
                    STATUS_EFFECT_COMMAND,
                    collector_transition.status_effect,
                    LOCAL_PLAYER_ACTOR_ID,
                )
            )
        packet, packet_bits = encode_server_command_batch(
            tuple(commands),
            include_terminator=True,
        )
        with s.flow_lock:
            s.gameplay_inventory = mutation.inventory
            s.gameplay_next_item_id = mutation.next_item_id
            s.gameplay_stickerbook_entries = owned
            s.gameplay_persistent_status_effects = (
                collector_transition.effects
            )
            s.gameplay_next_status_effect_instance_id = (
                collector_transition.next_instance_id
            )
        s.send_reliable_ordered_split(
            packet,
            total_bit_length=packet_bits,
            track=True,
            label_prefix=(
                'stickerbook-unlock-stickerbook-unlock'
            ),
        )
        CharacterService._achievement_on_stickerbook_changed(self, s)
        if definition.kind == "mercenary":
            CharacterService._ensure_mercenary_quickbar_shortcut(
                s, definition.resource_id
            )
        CharacterService._persist_gameplay_session(
            self,
            s,
            reason=f"sticker-book unlocked: {definition.resource_id}",
            flush=True,
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] consumed item {request.item_id} and "
            f"unlocked {definition.kind} {definition.resource_id!r}"
        )

    @staticmethod
    def _send_stickerbook_commands(
            s: Session, commands: tuple[ServerCommand, ...], *,
            label: str) -> None:
        packet, packet_bits = encode_server_command_batch(
            commands, include_terminator=True
        )
        s.send_reliable_ordered_split(
            packet,
            total_bit_length=packet_bits,
            track=True,
            label_prefix=f'{label}-{label}',
        )

    def _handle_stickerbook_favorite(
            self, s: Session, payload: bytes) -> None:
        """Persist and refresh a sticker-book front-page assignment."""

        request = decode_set_stickerbook_favorite(payload)
        CharacterService._accept_gameplay_state_command(
            s, SET_STICKER_BOOK_FAVORITE_COMMAND
        )
        with s.flow_lock:
            owned = dict(s.gameplay_stickerbook_entries)
            entry = owned.get(request.resource_id)
            if entry is None:
                raise ValueError(
                    f"sticker-book item {request.resource_id!r} is not owned"
                )
            # The 2026-09-09 retail trace toggles the first uint32 from 2 to 3
            # for a favorite and back to 2 when removed.  The second uint32
            # stays zero (or retains an authored pet-tier value); it is not a
            # generated front-page slot.  Writing 1..64 into field_18 made
            # otherwise valid entries appear unavailable to the client.
            updated = replace(
                entry,
                field_14=3 if request.favorite else 2,
                field_18=0,
            )
            owned[request.resource_id] = updated
            s.gameplay_stickerbook_entries = owned
            s.gameplay_client_command_state[
                SET_STICKER_BOOK_FAVORITE_COMMAND
            ] = request
        CharacterService._send_stickerbook_commands(
            s,
            (
                ServerCommand(
                    STICKERBOOK_INFO_COMMAND,
                    StickerbookInfo(
                        2,
                        tuple(owned[key] for key in sorted(owned)),
                    ),
                    LOCAL_PLAYER_ACTOR_ID,
                ),
            ),
            label="stickerbook-favorite-refresh",
        )
        CharacterService._persist_gameplay_session(
            self,
            s,
            reason=(
                f"sticker favorite {'added' if request.favorite else 'removed'}: "
                f"{request.resource_id}"
            ),
            flush=True,
        )

    def _handle_upgrade_stickerbook_item(
            self, s: Session, payload: bytes) -> None:
        """Buy the next database-authored pet rarity tier."""

        request = decode_upgrade_stickerbook_item(payload)
        CharacterService._accept_gameplay_state_command(
            s, UPGRADE_STICKER_BOOK_ITEM_COMMAND
        )
        upgrade = load_sticker_upgrade(
            self.static_database_path, request.resource_id
        )
        if upgrade is None:
            raise ValueError(
                f"sticker-book item {request.resource_id!r} has no next tier"
            )
        target_definition = load_sticker_definition(
            self.static_database_path, upgrade.target_resource_id
        )
        if target_definition is None or target_definition.kind != "pet":
            raise ValueError(
                f"sticker tier {upgrade.target_resource_id!r} is not a pet"
            )
        currency_id = "currency_stickerbook_upgrade"
        with s.flow_lock:
            owned = dict(s.gameplay_stickerbook_entries)
            source_entry = owned.get(request.resource_id)
            if source_entry is None:
                raise ValueError(
                    f"sticker-book item {request.resource_id!r} is not owned"
                )
            if upgrade.target_resource_id in owned:
                raise ValueError(
                    f"sticker tier {upgrade.target_resource_id!r} is owned"
                )
            wallet = dict(s.gameplay_game_currencies)
            balance = int(wallet.get(currency_id, 0))
            if balance < int(upgrade.cost):
                raise ValueError(
                    "not enough altered crescerite for pet upgrade: "
                    f"need {upgrade.cost}, have {balance}"
                )
            wallet[currency_id] = balance - int(upgrade.cost)
            upgraded_entry = replace(
                source_entry,
                resource_id=upgrade.target_resource_id,
            )
            owned.pop(request.resource_id)
            owned[upgrade.target_resource_id] = upgraded_entry
            was_active_pet = (
                str(s.gameplay_active_pet or "") == request.resource_id
            )
            s.gameplay_stickerbook_entries = owned
            s.gameplay_game_currencies = wallet
            s.gameplay_client_command_state[
                UPGRADE_STICKER_BOOK_ITEM_COMMAND
            ] = request
        CharacterService._send_stickerbook_commands(
            s,
            (
                ServerCommand(
                    GAME_CURRENCY_CHANGED_COMMAND,
                    GameCurrencyChanged(
                        currency_id, wallet[currency_id]
                    ),
                    LOCAL_PLAYER_ACTOR_ID,
                ),
                ServerCommand(
                    STICKERBOOK_ITEM_INFO_COMMAND,
                    StickerbookItemInfo(
                        3,
                        upgraded_entry,
                        related_resource_id=request.resource_id,
                    ),
                    LOCAL_PLAYER_ACTOR_ID,
                ),
            ),
            label="stickerbook-upgrade",
        )
        if was_active_pet:
            CharacterService._toggle_switchable_sticker(
                self, s, target_definition
            )
        else:
            CharacterService._persist_gameplay_session(
                self,
                s,
                reason=(
                    f"sticker upgraded: {request.resource_id} -> "
                    f"{upgrade.target_resource_id}"
                ),
                flush=True,
            )

    def _toggle_switchable_sticker(
            self, s: Session, definition) -> None:
        """Toggle a cosmetic, replacing only its own equipment/effect group."""

        if definition.kind not in {"costume", "pet", "decoration"}:
            raise ValueError(
                f"unsupported switchable sticker kind {definition.kind!r}"
            )
        resource_attribute = (
            "gameplay_active_costume"
            if definition.kind == "costume"
            else "gameplay_active_pet"
        )
        instance_attribute = (
            "gameplay_active_costume_effect_instance_id"
            if definition.kind == "costume"
            else "gameplay_active_pet_effect_instance_id"
        )
        decoration_group = (
            definition.exclusive_group.casefold() or definition.resource_id
            if definition.kind == "decoration" else None
        )
        transitions = []
        with s.flow_lock:
            if decoration_group is not None:
                current_resource, current_instance_id = (
                    s.gameplay_active_decorations.get(decoration_group, ("", None))
                )
            else:
                current_resource = str(getattr(s, resource_attribute) or "")
                current_instance_id = getattr(s, instance_attribute)
            current_effect = (
                s.gameplay_persistent_status_effects.get(
                    int(current_instance_id)
                )
                if current_instance_id is not None
                else None
            )

            # Add the replacement first. Retail does the same for exclusive
            # effects so the actor never briefly falls back to its base skin.
            if current_resource != definition.resource_id:
                last_move = s.gameplay_last_move
                start_tick = (
                    (last_move.start_tick + 1) & 0xFFFFFFFF
                    if last_move is not None
                    else (server_time() // 40) & 0xFFFFFFFF
                )
                new_instance_id = (
                    s.gameplay_next_status_effect_instance_id
                )
                s.gameplay_next_status_effect_instance_id += 1
                new_effect = sticker_status_entry(
                    definition,
                    instance_id=new_instance_id,
                    start_tick=start_tick,
                    actor_id=LOCAL_PLAYER_ACTOR_ID,
                    active=True,
                )
                s.gameplay_persistent_status_effects[
                    new_instance_id
                ] = new_effect
                if decoration_group is not None:
                    s.gameplay_active_decorations[decoration_group] = (
                        definition.resource_id, new_instance_id,
                    )
                else:
                    setattr(s, resource_attribute, definition.resource_id)
                    setattr(s, instance_attribute, new_instance_id)
                transitions.append(
                    StatusEffect(
                        clear_existing=False,
                        entries=(new_effect,),
                    )
                )
            else:
                if decoration_group is not None:
                    s.gameplay_active_decorations.pop(decoration_group, None)
                else:
                    setattr(s, resource_attribute, "")
                    setattr(s, instance_attribute, None)

            if current_effect is not None:
                s.gameplay_persistent_status_effects.pop(
                    current_effect.field_04, None
                )
                transitions.append(
                    StatusEffect(
                        clear_existing=False,
                        entries=(
                            replace(
                                current_effect,
                                has_details=False,
                                values=(),
                                field_38=False,
                                field_3c=0xFF,
                                shape_values=(),
                            ),
                        ),
                    )
                )

        if current_resource != definition.resource_id:
            CharacterService._achievement_feed(
                self, s, "use", (definition.resource_id, *definition.source_item_ids),
            )
        if transitions:
            packet = encode_monster_spawn_batch(
                MonsterSpawnBatch(
                    tuple(
                        StatusEffectServerCommand(
                            transition,
                            LOCAL_PLAYER_ACTOR_ID,
                        )
                        for transition in transitions
                    )
                )
            )
            s.send_reliable_ordered(
                packet,
                track=True,
                label=f"{definition.kind}-toggle",
            )
            if s.gameplay_presence_callback is not None:
                s.gameplay_presence_callback(
                    s, ("status_effects", tuple(transitions))
                )
        CharacterService._refresh_admin_speed(s, static_database_path=self.static_database_path)
        CharacterService._persist_gameplay_session(
            self,
            s,
            reason=f"{definition.kind} toggled",
            flush=True,
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] "
            f"{'activated' if current_resource != definition.resource_id else 'removed'} "
            f"{definition.kind} {definition.resource_id!r}"
        )

    def _toggle_mount_definition(
            self, s: Session, definition, *, source: str) -> None:
        """Run the shared timed cast/ride flow for any authored mount source."""

        # Reusing the active mount is the normal unmount operation.
        with s.flow_lock:
            active_ride = s.gameplay_mount_ride
        if (
            active_ride is not None
            and active_ride.definition.stickerbook_resource_id
            == definition.stickerbook_resource_id
        ):
            CharacterService._remove_mount_ride(
                s,
                reason=f"{source} toggle",
            )
            return

        # A new request replaces any pending cast or different active mount.
        CharacterService._cancel_mount_cast(s, 'replaced by a new request')
        CharacterService._remove_mount_ride(
            s,
            reason="replaced by a new mount",
            label="mount-ride-replaced",
            persist=False,
        )

        with s.flow_lock:
            last_move = s.gameplay_last_move
            start_tick = (
                (last_move.start_tick + 1) & 0xFFFFFFFF
                if last_move is not None
                else (server_time() // 40) & 0xFFFFFFFF
            )
            instance_id = s.gameplay_next_status_effect_instance_id
            s.gameplay_next_status_effect_instance_id += 1
            cast = begin_mount_cast(
                definition,
                instance_id=instance_id,
                start_tick=start_tick,
            )
            s.gameplay_mount_generation += 1
            generation = s.gameplay_mount_generation
            s.gameplay_mount_cast = cast
            cast_delay = max(0.0, definition.cast_duration_seconds)
            timer = threading.Timer(
                cast_delay,
                CharacterService._complete_mount_cast,
                args=(self, s, generation),
            )
            timer.daemon = True
            s.gameplay_mount_cast_timer = timer

        CharacterService._send_mount_effect(
            s,
            cast,
            active=True,
            label='mount-cast-add',
        )
        timer.start()
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] started typed mount cast for "
            f"{definition.stickerbook_resource_id} from {source}; "
            f"ride effect follows in {cast_delay:.1f}s"
        )
        CharacterService._persist_gameplay_session(
            self, s, reason='mount cast started'
        )

    def _handle_use_stickerbook_item(
            self, s: Session, payload: bytes) -> None:
        request = decode_simple_gameplay_command(payload)
        with s.flow_lock:
            s.gameplay_client_command_state[0x0135] = request
        CharacterService._accept_gameplay_state_command(s, 0x0135)

        database_definition = load_sticker_definition(
            self.static_database_path, request.value
        )
        with s.flow_lock:
            owned_entry = s.gameplay_stickerbook_entries.get(request.value)
            if owned_entry is None and database_definition is not None:
                owned_entry = next((
                    s.gameplay_stickerbook_entries[alias]
                    for alias in database_definition.aliases
                    if alias in s.gameplay_stickerbook_entries
                ), None)
        if owned_entry is None:
            raise ValueError(
                f"sticker-book item {request.value!r} is not owned"
            )
        if database_definition is not None and database_definition.kind == "emote":
            effect = load_consumable_definition(
                self.static_database_path, database_definition.resource_id
            )
            if effect is None:
                raise ValueError(f"emote {request.value!r} has no status effect")
            CharacterService._cancel_mount_cast(s, "emote")
            CharacterService._remove_mount_ride(s, reason="emote")
            # Reusable emotes play their authored finite status animation.
            # Keep them map-local and restart a full playback on every use.
            CharacterService._apply_instant_consumable_effect(
                s, replace(effect, persistent=False, reset_run_time=True,
                           max_stack_size=1),
                database_path=self.static_database_path,
            )
            return
        if (
            database_definition is not None
            and database_definition.kind in {"costume", "pet", "decoration"}
        ):
            CharacterService._toggle_switchable_sticker(
                self, s, database_definition
            )
            return
        if (
            database_definition is not None
            and database_definition.kind == "mercenary"
        ):
            CharacterService._activate_mercenary(
                self, s, database_definition, source="sticker book"
            )
            return

        # The database's own definition first: its effect ids are its rows'
        # (gameplay/local_mount_state.py find_mount, with the captured timing).
        definition = (
            database_definition.mount
            if database_definition is not None
            and database_definition.mount is not None
            else find_mount(request.value, self.static_database_path)
        )
        if definition is None:
            raise ValueError(
                f"owned sticker-book item {request.value!r} has no "
                "supported mount/costume/pet/emote definition"
            )
        CharacterService._toggle_mount_definition(
            self,
            s,
            definition,
            source="sticker book",
        )
