"""Deliver CMS bookings at the existing character initialization boundary."""

import copy
from dataclasses import replace
import threading

from dso_server.protocol.commerce_protocol import add_purchased_item, load_purchase_item_definition
from dso_server.protocol.currency_protocol import GAME_CURRENCY_BY_NAME
from dso_server.protocol.inventory_codec import owned_inventory_items
from dso_server.protocol.character_selection import CLASS_NAMES
from dso_server.protocol.world_loot_protocol import roll_new_equipment_enchantments
from dso_server.protocol.inventory_economy_protocol import refresh_inventory_economy_values
from dso_server.services.admin_items import item_metadata, equipment_options, validate_equipment_rolls


LEGACY_CURRENCIES = ("andermant", "gold", "badge_of_honor", "draken", "glyph_of_power")
CURRENCIES = (*LEGACY_CURRENCIES, *GAME_CURRENCY_BY_NAME)


class BookingDelivery:
    def __init__(self, repository, store, static_database_path):
        self.repository = repository
        self.store = store
        self.static_database_path = static_database_path
        self.lock = threading.RLock()

    def validate(self, kind, resource, amount, options=None, *, level=1, class_id=1):
        if type(amount) is not int or not 1 <= amount <= 1_000_000_000:
            raise ValueError("amount must be a whole number from 1 to 1,000,000,000")
        if options is not None and not isinstance(options, dict):
            raise ValueError("item options must be an object")
        if kind == "currency":
            if options:
                raise ValueError("currencies do not support equipment options")
            if resource not in CURRENCIES:
                raise ValueError("unknown currency")
        elif kind == "item":
            if amount > 1000:
                raise ValueError("item bookings are limited to 1,000 units")
            metadata = item_metadata(self.static_database_path, resource)
            if not metadata["bookable"]:
                raise ValueError("use a currency booking; instant consumables cannot be booked")
            if metadata["equipment"] and amount != 1:
                raise ValueError("book one equipment item at a time")
            if not metadata["equipment"] and amount > metadata["max_stack_size"]:
                raise ValueError("stack size exceeds this template's limit")
            normalized = equipment_options(metadata, options, level=level)
            if normalized:
                validate_equipment_rolls(self.static_database_path, metadata, normalized, class_id)
            return normalized
        else:
            raise ValueError("booking kind must be currency or item")
        return {}

    def _apply(self, state, booking, *, details=None):
        result = copy.deepcopy(state)
        resource, amount = booking["resource"], booking["amount"]
        profile = self.store.get_profile(state.character_id)
        raw_options = booking.get("options") or {}
        if booking["kind"] == "item" and not raw_options:
            # Pre-configuration bookings could contain multiple base equipment
            # items or multiple stacks. Keep their original delivery semantics.
            # New equipment bookings always store normalized, nonempty options.
            if type(amount) is not int or not 1 <= amount <= 1000:
                raise ValueError("item amount must be from 1 to 1,000")
            definition = load_purchase_item_definition(self.static_database_path, resource)
            if definition.item_category in {"Currency", "InstantConsumable"}:
                raise ValueError("this item category cannot be delivered to inventory")
            options = {}
        else:
            options = self.validate(booking["kind"], resource, amount, raw_options,
                                    level=profile.level, class_id=profile.class_id)
        if booking["kind"] == "item":
            definition = load_purchase_item_definition(self.static_database_path, resource)
            next_id = max(state.next_item_id, max((i.item_id for i in owned_inventory_items(state.inventory)), default=0) + 1)
            if next_id + amount > 0xFFFFFFFF:
                raise ValueError("item ID limit reached")
            if options:
                definition = replace(definition, rarity=options["rarity"], max_stack_size=1)
            mutation = add_purchased_item(result.inventory, definition, amount,
                next_item_id=next_id, item_level=options.get("item_level", profile.level))
            result.next_item_id = mutation.next_item_id
            if details is not None:
                details["mutation"] = mutation
            if options:
                metadata = item_metadata(self.static_database_path, resource)
                class_name = CLASS_NAMES.get(profile.class_id, "mage")
                if metadata["classes"] and class_name not in metadata["classes"]:
                    class_name = metadata["classes"][0]
                ids = tuple(item.item_id for item in mutation.changed_items)
                roll_new_equipment_enchantments(self.static_database_path, result.inventory, ids,
                    class_name=class_name, map_difficulty=options["enchantment_tier"])
                for item in result.inventory.items:
                    if item.item_id in ids:
                        item.socket_count = options["socket_count"]
                        item.socketed_templates = [""] * item.socket_count
                        if options["roll_quality"] is not None:
                            item.enchantments = [replace(e, value=options["roll_quality"] / 100)
                                                 for e in item.enchantments]
                refresh_inventory_economy_values(self.static_database_path, result.inventory,
                    item_ids=ids, force_melt_value=True)
        elif resource in GAME_CURRENCY_BY_NAME:
            key = GAME_CURRENCY_BY_NAME[resource].wire_id
            balance = result.game_currencies.get(key, 0) + amount
            if balance > 0xFFFFFFFFFFFFFFFF:
                raise ValueError("currency balance limit reached")
            result.game_currencies[key] = balance
        else:
            currency = result.currency
            balance = getattr(currency, resource) + amount
            limit = 0xFFFFFFFFFFFFFFFF if resource in {"gold", "glyph_of_power"} else 0xFFFFFFFF
            if balance > limit:
                raise ValueError("currency balance limit reached")
            if resource in {"andermant", "gold", "glyph_of_power"}:
                field = {"andermant": "currency_type", "gold": "amount", "glyph_of_power": "delta"}[resource]
                result.currency = replace(currency, **{field: balance})
            else:
                values = list(currency.values)
                values[0 if resource == "badge_of_honor" else 2] = balance
                result.currency = replace(currency, values=tuple(values))
        result.admin_booking_ids = (*state.admin_booking_ids, booking["id"])
        return result

    def deliver(self, character_id):
        """Run before initialization reads the saved snapshot, never during play.

        Reward and receipt share one player document/SQLite commit. Retrying an
        interrupted acknowledgement therefore cannot apply the reward twice.
        """
        with self.lock:
            pending = self.repository.pending(character_id)
            if not pending:
                return
            state = self.store.get(character_id)
            if state is None:
                return
            for booking in pending:
                if booking["id"] in state.admin_booking_ids:
                    self.store.flush()
                    self.repository.finish_booking(booking["id"])
                    continue
                owner = self.repository.registry.character_owner(character_id)
                if owner is None or owner.account_id != booking["account_id"]:
                    self.repository.booking_error(booking["id"], "character ownership changed")
                    continue
                try:
                    updated = self._apply(state, booking)
                except ValueError as exc:
                    self.repository.booking_error(booking["id"], exc)
                    continue
                # Fail initialization on durability failure, before gameplay can
                # spend a reward whose receipt has not reached disk.
                self.store.put(updated, flush=True)
                # Re-read the committed account balance/base before applying
                # another booking from this batch.
                state = self.store.get(character_id)
                self.repository.finish_booking(booking["id"])

    def cancel(self, actor, booking_id, reason):
        with self.lock:
            booking = self.repository.booking(booking_id)
            live_receipt = getattr(self, "live_receipt", None)
            if live_receipt is not None and live_receipt(booking["character_id"], booking_id):
                raise ValueError("booking was already applied online and cannot be cancelled")
            state = self.store.get(booking["character_id"])
            if state is not None and booking_id in state.admin_booking_ids:
                self.store.flush()
                if booking["status"] == "pending":
                    self.repository.finish_booking(booking_id)
                raise ValueError("booking was already delivered and cannot be cancelled")
            self.repository.cancel_booking(actor, booking_id, reason)
