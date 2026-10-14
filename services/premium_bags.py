"""Keep premium capacity and recoverable overflow in sync with membership."""

from datetime import datetime, timezone

from dso_server.gameplay.premium_bags import (
    PREMIUM_INVENTORY_SLOTS, PREMIUM_STORAGE_SLOTS, reconcile_premium_bags,
)
from dso_server.protocol.inventory_codec import (
    INVENTORY_INFO_COMMAND, InventoryInfo, clone_inventory_info, equipment_allocation_snapshot,
)
from dso_server.protocol.membership_protocol import (
    MEMBERSHIP_UPDATE_COMMAND, membership_is_active,
    membership_update_from_expiry, parse_membership_expiry,
)
from dso_server.protocol.player_init_protocol import (
    ServerCommand, encode_server_command_batch,
)
from dso_server.services.base import CharacterService


class PremiumBagService:
    @staticmethod
    def _refresh_premium_bags(s) -> bool:
        """Publish only changed registrations plus complete allocations.

        Run before client actions and after inventory writes. Unavailable
        items live outside InventoryInfo.items, so normal item lookup,
        crafting, selling, equipping, and consuming cannot access them.
        """

        with s.flow_lock:
            inventory = getattr(s, "gameplay_inventory", None)
            if not isinstance(inventory, InventoryInfo) or s.gameplay_exit_transition_started:
                return False
            active = membership_is_active(
                s.gameplay_membership_type, s.gameplay_membership_active,
                s.gameplay_membership_expires_at,
            )
            membership_changed = bool(s.gameplay_membership_active) != active
            capacity_changed = (
                inventory.extra_storage_size != (PREMIUM_INVENTORY_SLOTS if active else 0)
                or inventory.extra_bank_storage_size != (PREMIUM_STORAGE_SLOTS if active else 0)
            )
            pending = (
                bool(inventory.premium_inventory_overflow)
                and (len(inventory.storage_58) < inventory.storage_size
                     or len(inventory.storage_80) < inventory.extra_storage_size)
            ) or (
                bool(inventory.premium_storage_overflow)
                and (len(inventory.storage_a8) < inventory.bank_storage_size
                     or len(inventory.storage_d0) < inventory.extra_bank_storage_size)
            )
            locked_allocations = not active and bool(inventory.storage_80 or inventory.storage_d0)
            commands = []
            if membership_changed:
                s.gameplay_membership_active = active
                commands.append(ServerCommand(
                    MEMBERSHIP_UPDATE_COMMAND,
                    membership_update_from_expiry(
                        s.gameplay_membership_expires_at,
                        membership_type=s.gameplay_membership_type, active=active,
                    ), inventory.actor_id,
                ))
            inventory_changed = False
            if capacity_changed or pending or locked_allocations:
                updated = clone_inventory_info(inventory)
                changes = reconcile_premium_bags(updated, active=active)
                inventory_changed = bool(
                    capacity_changed or locked_allocations or changes.removed_ids or changes.restored_items
                )
                if inventory_changed:
                    s.gameplay_inventory = updated
                    delta = equipment_allocation_snapshot(updated)
                    delta.items = list(changes.restored_items)
                    delta.changed_item_ids = list(changes.removed_ids)
                    delta.field_1e0 = True
                    commands.extend((
                        ServerCommand(INVENTORY_INFO_COMMAND, delta, updated.actor_id),
                        ServerCommand(INVENTORY_INFO_COMMAND,
                                      equipment_allocation_snapshot(updated), updated.actor_id),
                    ))
        if commands and s.connected and s.gameplay_presence_ready:
            packet, bits = encode_server_command_batch(commands, include_terminator=True)
            CharacterService._send_world_loot_state_batch(
                s, packet, bits, label="premium-bags-updated",
            )
        CharacterService._schedule_premium_bag_expiry(s)
        return membership_changed or inventory_changed

    @staticmethod
    def _schedule_premium_bag_expiry(s) -> None:
        """Use one cancellable deadline, including while the player is idle."""

        with s.flow_lock:
            signature = (
                s.gameplay_membership_type, s.gameplay_membership_active,
                s.gameplay_membership_expires_at,
            )
            if not s.connected or not s.gameplay_presence_ready or s.gameplay_exit_transition_started:
                return
            if signature == s.gameplay_premium_bag_deadline:
                return
            old_timer = s.gameplay_premium_bag_timer
            if old_timer is not None:
                old_timer.cancel()
            s.gameplay_premium_bag_timer = None
            s.gameplay_premium_bag_deadline = signature
            if not membership_is_active(signature[0], signature[1], signature[2]):
                return
            delay = max(0.0, (parse_membership_expiry(signature[2])
                             - datetime.now(timezone.utc)).total_seconds())

            def expire():
                with s.flow_lock:
                    if s.gameplay_premium_bag_timer is not timer or not s.connected:
                        return
                    s.gameplay_premium_bag_timer = None
                    s.gameplay_premium_bag_deadline = None
                if CharacterService._refresh_premium_bags(s):
                    CharacterService._request_account_save(s, "premium membership expired", flush=True)

            # Windows condition waits cannot span multi-year memberships.
            # Recheck daily until the expiry is close enough to schedule exactly.
            timer = s.gameplay_deadline_timer(min(delay, 86400.0), expire)
            s.gameplay_premium_bag_timer = timer
        timer.start()
