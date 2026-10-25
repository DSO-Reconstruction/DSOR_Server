"""Authoritative inventory economy mutations and client request codecs.

The retail client predicts destructive inventory operations before their
commands reach the server.  The server must nevertheless commit the same
change and publish a complete allocation snapshot; otherwise the removed item
returns on refresh and its stale slot becomes an invisible occupied slot.
"""

from __future__ import annotations

from dso_server.protocol.command_ids import command_id
import copy
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
import sqlite3
from collections import Counter

from dso_server.protocol.character_selection import BitReader
from dso_server.protocol.commerce_protocol import (
    InventoryPurchaseMutation,
    PurchaseItemDefinition,
    PurchasePrice,
    add_purchased_item,
    load_purchase_item_definition,
    free_inventory_location,
)
from dso_server.protocol.currency_protocol import (
    GAME_CURRENCY_BY_WIRE_ID,
    GameCurrencyChanged,
    normalize_game_currency_balances,
)
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.protocol.player_init_protocol import CurrencyChanged


SALVAGE_COMMAND = command_id("Commands::SalvageCommand", 0x0031)
CRAFTING_COMMAND = command_id("Commands::CraftingCommand", 0x005C)
CRAFTING_RESULT_ACCEPTANCE_COMMAND = command_id(
    "Commands::CraftingResultAcceptanceCommand", 0x005E
)
@dataclass(frozen=True)
class SalvageRequest:
    item_ids: tuple[int, ...]
    mode: int = 0


@dataclass(frozen=True)
class CraftingRequest:
    item_ids: tuple[int, ...]
    recipe_id: str
    amount: int
    mode: int = 0
    payment_currency: int = 0
    source_slot: int = 0xFF
    field_00: int = 0
    field_10: int = 0
    field_14: int = 0


@dataclass(frozen=True)
class CraftingRecipe:
    resource_id: str
    result_template_id: str
    result_amount: int
    result_is_loot_table: bool
    ingredients: tuple[tuple[str, int], ...]
    price: PurchasePrice
    unlock_resource_id: str
    minimum_level: int
    crafting_recipe_type: str = ""
    scale_price_rc: bool = False
    scale_price_vc: bool = False


@dataclass(frozen=True)
class InventoryMutation:
    inventory: InventoryInfo
    delta: InventoryInfo
    final: InventoryInfo
    next_item_id: int
    changed_items: tuple[ItemInfo, ...]
    removed_item_ids: tuple[int, ...]
    replacements: dict[int, int]


@dataclass(frozen=True)
class CurrencyItemCredit:
    currency: CurrencyChanged
    game_currencies: dict[str, int]
    game_currency_update: GameCurrencyChanged | None
    amount: int
    name: str


@dataclass(frozen=True)
class ItemMeltReward:
    glyphs: int = 0
    dust_template_id: str = ""
    dust_amount: int = 0


def _request_reader(payload: bytes, expected: int) -> BitReader:
    reader = BitReader(payload)
    if reader.read_uint8() != 0x8B:
        raise ValueError("request is not a client command")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != expected:
        raise ValueError(
            f"expected command 0x{expected:04x}, got 0x{command_id:04x}"
        )
    return reader


def _consume_zero_padding(reader: BitReader, label: str) -> None:
    if reader.remaining and reader.read_bits(reader.remaining):
        raise ValueError(f"{label} has non-zero trailing data")


def decode_salvage_request(payload: bytes) -> SalvageRequest:
    """Decode retail ``SalvageCommand`` (0x0031)."""

    reader = _request_reader(payload, SALVAGE_COMMAND)
    count = reader.read_uint32_le()
    if count > 128:
        raise ValueError("salvage request contains too many items")
    item_ids = tuple(reader.read_uint32_le() for _ in range(count))
    mode = reader.read_uint32_le() if reader.remaining >= 32 else 0
    _consume_zero_padding(reader, "SalvageCommand")
    return SalvageRequest(item_ids, mode)


def decode_crafting_request(payload: bytes) -> CraftingRequest:
    """Decode the generated workbench ``CraftingCommand`` (0x005c)."""

    reader = _request_reader(payload, CRAFTING_COMMAND)
    field_00 = reader.read_uint8()
    count = reader.read_uint32_le()
    if count > 32:
        raise ValueError("crafting request contains too many ingredient IDs")
    item_ids = tuple(reader.read_uint32_le() for _ in range(count))
    # Live command order after the item ids:
    #
    #   payment selector (0=gold, 1=Andermant, 5=special recipe branch),
    #   source slot, requested recipe executions, recipe resource, operation
    #   mode, two reserved uint32s.
    #
    # Dismantling proves the distinction between the two uint32 fields.  The
    # value before the recipe tracks the selected stack (56, 54, ...), while
    # the value after every ``*_dismantle0`` recipe remains the mode value 2.
    payment_currency = reader.read_uint8()
    source_slot = reader.read_uint8()
    amount = reader.read_uint32_le()
    recipe_id = reader.read_string()
    mode = reader.read_uint32_le()
    field_10 = reader.read_uint32_le()
    field_14 = reader.read_uint32_le()
    # Selector 5 is used by special workbench recipes. Enchantment-transfer
    # recipes are identified by their recipe resource and do not debit a
    # wallet balance; other selector-5 recipes can charge their authored UC
    # price (the mercenary equipment recipes do this).
    if payment_currency not in (0, 1, 5):
        raise ValueError(
            f"CraftingCommand payment currency {payment_currency} "
            "is unsupported"
        )
    _consume_zero_padding(reader, "CraftingCommand")
    return CraftingRequest(
        item_ids=item_ids,
        recipe_id=recipe_id,
        amount=amount,
        mode=mode,
        payment_currency=payment_currency,
        source_slot=source_slot,
        field_00=field_00,
        field_10=field_10,
        field_14=field_14,
    )


def _open_database(path: str) -> sqlite3.Connection:
    database_path = Path(path)
    if not database_path.is_file():
        raise FileNotFoundError(f"static database not found: {database_path}")
    database = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    return database


def _parse_ingredient(value: object) -> tuple[str, int] | None:
    encoded = str(value or "").strip()
    if not encoded:
        return None
    template_id, separator, raw_amount = encoded.partition(":")
    amount = int(raw_amount) if separator and raw_amount else 1
    if not template_id or amount <= 0:
        raise ValueError(f"invalid crafting ingredient {encoded!r}")
    return template_id, amount


@lru_cache(maxsize=2048)
def _load_recipe_cached(
    path: str,
    recipe_id: str,
    selected_template_ids: tuple[str, ...] = (),
) -> CraftingRecipe:
    database = _open_database(path)
    try:
        row = database.execute(
            "SELECT * FROM _Template_CraftingRecipes WHERE Id=? AND IsLive=1",
            (recipe_id,),
        ).fetchone()
        # The client appends a decimal result selector to some direct recipes
        # (for example gem_diamond_fire_e_to_f0).
        if row is None and recipe_id[-1:].isdigit():
            row = database.execute(
                "SELECT * FROM _Template_CraftingRecipes "
                "WHERE Id=? AND IsLive=1",
                (recipe_id[:-1],),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown or locked recipe {recipe_id!r}")
        ingredients = tuple(
            parsed
            for parsed in (
                _parse_ingredient(row[f"IngredientItemId{index}"])
                for index in range(1, 5)
            )
            if parsed is not None
        )
        crafting_recipe_type = str(row["CraftingRecipeType"] or "")
        price_factor = 1.0
        encoded_result = str(row["CraftedItemId"] or "")
        if not ingredients and crafting_recipe_type == "OpalCreation":
            selected = Counter(str(value) for value in selected_template_ids)
            if len(selected_template_ids) != 4:
                raise ValueError(
                    "opal crafting requires exactly three gems and gem dust"
                )
            for opal_row in database.execute(
                "SELECT * FROM _Template_OpalCrafting"
            ):
                candidate = tuple(
                    parsed
                    for parsed in (
                        _parse_ingredient(
                            opal_row[f"IngredientItemId{index}"]
                        )
                        for index in range(1, 5)
                    )
                    if parsed is not None
                )
                if (
                    Counter(
                        template_id for template_id, _ in candidate
                    )
                    != selected
                ):
                    continue
                ingredients = candidate
                encoded_result = str(opal_row["CraftedItemId"] or "")
                price_factor = max(
                    0.0, float(opal_row["PriceFactor"] or 1.0)
                )
                break
            if not ingredients:
                raise ValueError(
                    "selected gems do not form an authored opal recipe"
                )
        elif not ingredients:
            raise ValueError(f"recipe {row['Id']!r} has no item ingredients")
        result_is_loot_table = bool(row["ResultIsLootTable"])
        if result_is_loot_table:
            result_template_id = encoded_result
            result_amount = 1
        else:
            parsed_result = _parse_ingredient(encoded_result)
            if parsed_result is None:
                raise ValueError(
                    f"recipe {row['Id']!r} has no crafted item"
                )
            result_template_id, result_amount = parsed_result
        return CraftingRecipe(
            str(row["Id"]),
            result_template_id,
            result_amount,
            result_is_loot_table,
            ingredients,
            PurchasePrice(
                rc=max(0, round(int(row["PriceRC"] or 0) * price_factor)),
                vc=max(0, round(int(row["PriceVC"] or 0) * price_factor)),
                uc=max(0, round(int(row["PriceUC"] or 0) * price_factor)),
            ),
            str(row["RecipeItemReference"] or ""),
            max(1, int(row["UnlockLevel"] or 1)),
            crafting_recipe_type,
            str(row["ScalePriceRC"] or "NoScaling") == "ToPlayerLevel",
            str(row["ScalePriceVC"] or "NoScaling") == "ToPlayerLevel",
        )
    finally:
        database.close()


def load_crafting_recipe(
    static_database_path: str | Path,
    recipe_id: str,
    *,
    selected_template_ids: tuple[str, ...] = (),
) -> CraftingRecipe:
    return _load_recipe_cached(
        str(Path(static_database_path)),
        str(recipe_id),
        tuple(sorted(str(value) for value in selected_template_ids)),
    )


def load_enchantment_transfer_factor(
    static_database_path: str | Path, recipe_id: str,
) -> int:
    """Read the authored Glyphs of Power factor for a transfer recipe."""

    recipe_id = str(recipe_id or "").strip()
    database = _open_database(static_database_path)
    try:
        row = database.execute(
            "SELECT CraftingRecipeType, FactorUC "
            "FROM _Template_CraftingRecipes WHERE Id=? AND IsLive=1",
            (recipe_id,),
        ).fetchone()
        if row is None and recipe_id[-1:].isdigit():
            row = database.execute(
                "SELECT CraftingRecipeType, FactorUC "
                "FROM _Template_CraftingRecipes WHERE Id=? AND IsLive=1",
                (recipe_id[:-1],),
            ).fetchone()
        if row is None or str(row["CraftingRecipeType"] or "") not in {
            "EnchantmentTransfer", "SingleEnchantmentTransfer",
        }:
            raise ValueError(
                f"unknown enchantment transfer recipe {recipe_id!r}"
            )
        factor = int(round(float(row["FactorUC"] or 0)))
        if factor <= 0:
            raise ValueError(
                f"enchantment transfer recipe {recipe_id!r} has no cost factor"
            )
        return factor
    finally:
        database.close()


def crafting_execution_count(
    recipe: CraftingRecipe, request: CraftingRequest,
) -> int:
    """Return actual result executions for the generated request shape.

    The uint32 before the recipe id is the requested output count for both
    ordinary recipes and OpalCreation. An opal happens to consume three
    different component gems per execution; treating the common value ``3``
    as a component count incorrectly capped every request to one result.
    """

    count = int(request.amount)
    if count <= 0:
        raise ValueError("crafting execution count must be positive")
    return count


def crafting_price_for_payment(
    price: PurchasePrice,
    payment_currency: int,
    executions: int,
    *,
    player_level: int = 1,
    scale_rc_to_player_level: bool = False,
    scale_vc_to_player_level: bool = False,
) -> PurchasePrice:
    """Select the client's workbench payment branch and apply recipe scaling."""

    count = int(executions)
    if count <= 0:
        raise ValueError("crafting execution count must be positive")
    level = max(1, int(player_level))
    if int(payment_currency) == 1:
        if int(price.rc) <= 0:
            raise ValueError("recipe cannot be paid with Andermant")
        unit_price = int(price.rc) * (level if scale_rc_to_player_level else 1)
        return PurchasePrice(rc=unit_price * count)
    if int(payment_currency) == 5:
        if int(price.uc) <= 0:
            raise ValueError(
                "recipe cannot be paid with legacy progression currency"
            )
        return PurchasePrice(uc=int(price.uc) * count)
    if int(payment_currency) != 0:
        raise ValueError("unsupported crafting payment currency")
    if int(price.vc) > 0:
        unit_price = int(price.vc) * (level if scale_vc_to_player_level else 1)
        return PurchasePrice(vc=unit_price * count)
    if int(price.uc) > 0:
        return PurchasePrice(uc=int(price.uc) * count)
    # Free recipes have no price branch at all.
    if not any(price.as_tuple()):
        return PurchasePrice()
    raise ValueError("recipe cannot be paid with gold")


@lru_cache(maxsize=8)
def _recipe_book_resources_cached(path: str) -> tuple[str, ...]:
    database = _open_database(path)
    try:
        return tuple(
            sorted({
                str(row[0])
                for row in database.execute(
                    "SELECT RecipeItemReference "
                    "FROM _Template_CraftingRecipes "
                    "WHERE IsLive=1 AND RecipeItemReference IS NOT NULL "
                    "AND RecipeItemReference != ''"
                )
            })
        )
    finally:
        database.close()


def load_recipe_book_resources(
    static_database_path: str | Path,
) -> tuple[str, ...]:
    """Return all live recipe unlock resources for the development server."""

    return _recipe_book_resources_cached(str(Path(static_database_path)))


@lru_cache(maxsize=8)
def _recipe_unlock_achievements_cached(
    path: str,
) -> tuple[tuple[str, int], ...]:
    database = _open_database(path)
    try:
        return tuple(
            (str(row[0]), max(1, int(row[1] or 1)))
            for row in database.execute(
                "SELECT DISTINCT achievement.Id, achievement.Amount "
                "FROM _Template_CraftingRecipes AS recipe "
                "JOIN _Template_Achievements AS achievement "
                "ON achievement.Id=recipe.AchievementReference "
                "WHERE recipe.IsLive=1 "
                "AND recipe.AchievementReference IS NOT NULL "
                "AND recipe.AchievementReference != '' "
                "AND achievement.EnabledLive=1 "
                "ORDER BY achievement.Id"
            )
        )
    finally:
        database.close()


def load_recipe_unlock_achievements(
    static_database_path: str | Path,
) -> tuple[tuple[str, int], ...]:
    """Return achievement progress required by client-side recipe gates."""

    return _recipe_unlock_achievements_cached(
        str(Path(static_database_path))
    )


def _replace_item_id(info: InventoryInfo, old_id: int, new_id: int) -> None:
    for values in (
        info.equipment_slots,
        info.storage_58,
        info.storage_80,
        info.storage_a8,
        info.storage_d0,
    ):
        if old_id in values:
            values[new_id] = values.pop(old_id)
    for values in (info.field_f8, info.field_118, info.field_180):
        for index, value in enumerate(values):
            if int(value) == old_id:
                values[index] = new_id


def _remove_item_references(info: InventoryInfo, item_id: int) -> None:
    info.equipment_slots.pop(item_id, None)
    for storage in (
        info.storage_58,
        info.storage_80,
        info.storage_a8,
        info.storage_d0,
    ):
        storage.pop(item_id, None)
    info.field_f8 = [value for value in info.field_f8 if value != item_id]
    info.field_118 = [value for value in info.field_118 if value != item_id]
    info.field_180 = [value for value in info.field_180 if value != item_id]


class InventoryMutationBuilder:
    """Apply a set of inventory changes and publish one coherent delta."""

    def __init__(self, inventory: InventoryInfo, next_item_id: int) -> None:
        self.inventory = copy.deepcopy(inventory)
        self.next_item_id = int(next_item_id)
        self.changed_items: list[ItemInfo] = []
        self.removed_item_ids: list[int] = []
        self.replacements: dict[int, int] = {}

    def item(self, item_id: int) -> ItemInfo:
        result = next(
            (
                item for item in self.inventory.items
                if int(item.item_id) == int(item_id)
            ),
            None,
        )
        if result is None:
            raise ValueError(f"inventory item {item_id} does not exist")
        return result

    def rekey_item(self, item_id: int) -> ItemInfo:
        """Publish an existing item as a new client object under a fresh ID.

        The replacement map lets the client move equipment/storage references
        before discarding the old object. This is needed when mutable item
        fields cannot be refreshed in place by the workbench result handler.
        """

        item = self.item(item_id)
        old_id = int(item.item_id)
        new_id = int(self.next_item_id)
        if new_id <= 0 or new_id > 0xFFFFFFFF:
            raise ValueError("inventory item ID allocator is exhausted")
        self.next_item_id += 1
        item.item_id = new_id
        item.field_18 = new_id & 0xFFFF
        _replace_item_id(self.inventory, old_id, new_id)
        self.removed_item_ids.append(old_id)
        self.replacements[new_id] = old_id
        self.changed_items.append(copy.deepcopy(item))
        return item

    def restore_item(self, item: ItemInfo, *, ingredient_bag: bool = False) -> None:
        """Restore an exact sold item, retaining its identity and upgrades."""
        if any(int(existing.item_id) == int(item.item_id)
               for existing in self.inventory.items):
            raise ValueError("repurchase item is already in inventory")
        restored = copy.deepcopy(item)
        if ingredient_bag:
            self.inventory.field_180.append(restored.item_id)
            if restored.template_id not in self.inventory.field_1a0:
                self.inventory.field_1a0.append(restored.template_id)
        else:
            location = free_inventory_location(self.inventory)
            if location is None:
                raise ValueError("ordinary inventory storage is full")
            storage, slot = location
            getattr(self.inventory, storage)[restored.item_id] = slot
        self.inventory.items.append(restored)
        self.changed_items.append(copy.deepcopy(restored))
        self.next_item_id = max(self.next_item_id, int(restored.item_id) + 1)

    def consume(
        self,
        item_id: int,
        amount: int | None = None,
        *,
        preserve_item_id: bool = False,
    ) -> ItemInfo:
        item = self.item(item_id)
        consume_amount = int(item.amount if amount is None else amount)
        if consume_amount <= 0 or consume_amount > int(item.amount):
            raise ValueError(
                f"cannot consume {consume_amount} from item stack "
                f"{item_id} x{item.amount}"
            )
        old_item = copy.deepcopy(item)
        old_id = int(item.item_id)
        self.removed_item_ids.append(old_id)
        if consume_amount == int(item.amount):
            self.inventory.items.remove(item)
            _remove_item_references(self.inventory, old_id)
            return old_item

        if preserve_item_id:
            # Retail workbench mutations keep partially consumed ingredient
            # stack IDs stable and publish the reduced ItemInfo in place.
            # Replacing every selected stack ID causes the client's predicted
            # workbench allocations to drift into invisible/unequipped state.
            item.amount = int(item.amount) - consume_amount
            self.changed_items.append(copy.deepcopy(item))
            self.removed_item_ids.pop()
            return old_item

        new_id = self.next_item_id
        self.next_item_id += 1
        item.item_id = new_id
        item.amount = int(item.amount) - consume_amount
        item.field_18 = new_id & 0xFFFF
        _replace_item_id(self.inventory, old_id, new_id)
        self.changed_items.append(copy.deepcopy(item))
        self.replacements[new_id] = old_id
        return old_item

    def consume_template(
        self,
        template_id: str,
        amount: int,
        *,
        preferred_item_ids: tuple[int, ...] = (),
        preserve_item_ids: bool = False,
    ) -> None:
        remaining = int(amount)
        candidates = sorted(
            (
                item for item in self.inventory.items
                if item.template_id == template_id
            ),
            key=lambda item: (
                int(item.item_id) not in set(preferred_item_ids),
                int(item.item_id),
            ),
        )
        if sum(int(item.amount) for item in candidates) < remaining:
            raise ValueError(
                f"insufficient crafting ingredient {template_id!r}: "
                f"need {amount}"
            )
        for candidate in candidates:
            if not remaining:
                break
            consumed = min(remaining, int(candidate.amount))
            current_id = int(candidate.item_id)
            self.consume(
                current_id,
                consumed,
                preserve_item_id=preserve_item_ids,
            )
            remaining -= consumed

    # ``Game::Inventory`` names the four containers storage, extra storage,
    # bank storage, and extra bank storage.  Every slot-addressed inventory
    # operation selects one of them in that order.
    STORAGE_NAMES = ("storage_58", "storage_80", "storage_a8", "storage_d0")

    def _storage_capacity(self, storage_name: str) -> int:
        capacities = {
            "storage_58": int(self.inventory.storage_size),
            "storage_80": int(self.inventory.extra_storage_size),
            "storage_a8": int(self.inventory.bank_storage_size),
            "storage_d0": int(self.inventory.extra_bank_storage_size),
        }
        if storage_name not in capacities:
            raise ValueError(f"unknown inventory storage {storage_name!r}")
        return capacities[storage_name]

    def split_stack(
        self,
        item_id: int,
        remaining_amount: int,
        *,
        storage_name: str,
        storage_slot: int,
    ) -> ItemInfo:
        """Split one stack, leaving ``remaining_amount`` in the source.

        ``Messaging::SplitStack`` carries the amount that stays behind, not
        the amount that moves: ``InventoryProperty::OnSplitStack`` assigns it
        straight onto the source item's amount and only when it is smaller
        than the current one.  The moved stack therefore receives
        ``amount - remaining_amount``.

        The new stack is a copy of the source, so enchantments, item level,
        rarity and sockets survive the split.  The source keeps the retail
        identity transition every other partial-stack mutation uses: it is
        republished under a fresh item id with ``field_158`` mapping new to
        old, because the client rebuilds a changed stack as a new object.
        """

        item = self.item(item_id)
        remaining_amount = int(remaining_amount)
        total = int(item.amount)
        if remaining_amount <= 0 or remaining_amount >= total:
            raise ValueError(
                f"cannot leave {remaining_amount} of stack {item_id} x{total}"
            )
        capacity = self._storage_capacity(storage_name)
        storage_slot = int(storage_slot)
        if capacity <= 0:
            raise ValueError(
                f"inventory storage {storage_name!r} is unavailable"
            )
        if not 0 <= storage_slot < capacity:
            raise ValueError(
                f"storage slot {storage_slot} is outside {storage_name!r} "
                f"capacity {capacity}"
            )
        target = getattr(self.inventory, storage_name)
        if storage_slot in {
            int(slot)
            for allocated_id, slot in target.items()
            if int(allocated_id) != int(item_id)
        }:
            raise ValueError(
                f"storage slot {storage_slot} in {storage_name!r} is occupied"
            )

        moved_amount = total - remaining_amount
        unit_value = int(item.field_30) // total if total else 0
        source = copy.deepcopy(item)
        # Reduce and re-key the source first so the split-off stack cannot be
        # be handed the id the source is about to release.  ``consume`` leaves
        # the reduced source in ``changed_items`` under its new id.
        self.consume(item_id, moved_amount)
        self.item(self.next_item_id - 1).field_30 = unit_value * remaining_amount
        for published in self.changed_items:
            if int(published.item_id) == self.next_item_id - 1:
                published.field_30 = unit_value * remaining_amount

        new_id = self.next_item_id
        self.next_item_id += 1
        moved = replace(
            source,
            item_id=new_id,
            amount=moved_amount,
            field_18=new_id & 0xFFFF,
            field_30=unit_value * moved_amount,
            enchantments=copy.deepcopy(source.enchantments),
            socketed_templates=list(source.socketed_templates),
        )
        self.inventory.items.append(moved)
        target[new_id] = storage_slot
        self.changed_items.append(copy.deepcopy(moved))
        return moved

    def move_item(
        self,
        item_id: int,
        *,
        storage_name: str,
        storage_slot: int,
    ) -> ItemInfo:
        """Relocate one allocated item to an explicit slot.

        This is the drag-and-drop path (``Messaging::MoveItem``).  The client
        has already applied it locally and does not need the item definition
        back, only the authoritative allocation, so no item is published as
        changed.  Empty destinations use operations 0x07 through 0x0a;
        occupied destinations use the generic swap operation 0x0b.
        """

        item = self.item(item_id)
        capacity = self._storage_capacity(storage_name)
        storage_slot = int(storage_slot)
        if capacity <= 0:
            raise ValueError(
                f"inventory storage {storage_name!r} is unavailable"
            )
        if not 0 <= storage_slot < capacity:
            raise ValueError(
                f"storage slot {storage_slot} is outside {storage_name!r} "
                f"capacity {capacity}"
            )
        target = getattr(self.inventory, storage_name)
        occupant = next(
            (
                int(allocated_id)
                for allocated_id, slot in target.items()
                if int(slot) == storage_slot
                and int(allocated_id) != int(item_id)
            ),
            None,
        )
        if occupant is not None:
            raise ValueError(
                f"storage slot {storage_slot} in {storage_name!r} is held by "
                f"item {occupant}"
            )
        self.inventory.equipment_slots.pop(int(item_id), None)
        for name in self.STORAGE_NAMES:
            getattr(self.inventory, name).pop(int(item_id), None)
        target[int(item_id)] = storage_slot
        return item

    def _stored_location(self, item_id: int) -> tuple[str, int]:
        if item_id in self.inventory.equipment_slots:
            raise ValueError(f'inventory item {item_id} is equipped')
        locations = [
            (name, int(getattr(self.inventory, name)[item_id]))
            for name in self.STORAGE_NAMES
            if item_id in getattr(self.inventory, name)
        ]
        if len(locations) != 1:
            raise ValueError(
                f'inventory item {item_id} has {len(locations)} storage '
                'allocations; expected one'
            )
        return locations[0]

    def swap_storage_items(
        self, item_id: int, displaced_item_id: int
    ) -> tuple[ItemInfo, ItemInfo]:
        '''Exchange two items' exact storage-container allocations.'''

        item_id = int(item_id)
        displaced_item_id = int(displaced_item_id)
        if item_id == displaced_item_id:
            raise ValueError('cannot swap an inventory item with itself')
        item = self.item(item_id)
        displaced = self.item(displaced_item_id)
        source_name, source_slot = self._stored_location(item_id)
        target_name, target_slot = self._stored_location(displaced_item_id)
        getattr(self.inventory, source_name).pop(item_id)
        getattr(self.inventory, target_name).pop(displaced_item_id)
        getattr(self.inventory, target_name)[item_id] = target_slot
        getattr(self.inventory, source_name)[displaced_item_id] = source_slot
        return item, displaced

    def add_item(
        self,
        definition: PurchaseItemDefinition,
        amount: int,
        *,
        item_level: int,
    ) -> None:
        mutation: InventoryPurchaseMutation = add_purchased_item(
            self.inventory,
            definition,
            amount,
            next_item_id=self.next_item_id,
            item_level=item_level,
        )
        self.next_item_id = mutation.next_item_id
        self.changed_items.extend(copy.deepcopy(mutation.changed_items))
        self.removed_item_ids.extend(mutation.replaced_item_ids)
        self.replacements.update(mutation.replacements)

    def finish(self) -> InventoryMutation:
        self.inventory.changed_item_ids = []
        self.inventory.field_158 = {}
        self.inventory.field_1e0 = False
        delta = copy.deepcopy(self.inventory)
        delta.items = copy.deepcopy(self.changed_items)
        delta.changed_item_ids = list(dict.fromkeys(self.removed_item_ids))
        delta.field_158 = dict(self.replacements)
        delta.field_1e0 = True
        final = copy.deepcopy(self.inventory)
        final.items = []
        final.changed_item_ids = []
        final.field_158 = {}
        final.field_1e0 = False
        return InventoryMutation(
            self.inventory,
            delta,
            final,
            self.next_item_id,
            tuple(self.changed_items),
            tuple(dict.fromkeys(self.removed_item_ids)),
            dict(self.replacements),
        )


@lru_cache(maxsize=4096)
def _item_economy_row(path: str, template_id: str) -> dict[str, object]:
    database = _open_database(path)
    try:
        row = database.execute(
            "SELECT Id, ItemCategory, BaseValue, VCSellFactor, "
            "PriceVC, PriceRC, PriceGC, SlotType "
            "FROM _Template_Item WHERE Id=?",
            (template_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown item template {template_id!r}")
        return dict(row)
    finally:
        database.close()


def item_sell_value(
    static_database_path: str | Path, item: ItemInfo, amount: int,
) -> int:
    row = _item_economy_row(
        str(Path(static_database_path)), item.template_id
    )
    unit_value = max(0, int(row.get("PriceVC") or 0))
    if not unit_value:
        unit_value = max(0, int(row.get("BaseValue") or 0))
    factor = max(0.0, float(row.get("VCSellFactor") or 0.0))
    return max(0, round(unit_value * factor)) * int(amount)


def item_salvage_value(
    static_database_path: str | Path, item: ItemInfo,
) -> int:
    """Resolve deterministic glyph yield from authored item progression.

    This client database has no separate salvage table.  Retail derives the
    melt yield from the item's level, rarity and VC base value; keep those
    same authored inputs and never trust a client-provided amount.
    """

    reward = item_melt_reward(static_database_path, item)
    if reward.dust_template_id:
        raise ValueError(
            f"{item.template_id!r} melts into {reward.dust_template_id}, "
            "not Glyphs of Power"
        )
    return reward.glyphs


@lru_cache(maxsize=8192)
def _enchantment_economy_row(path: str, template_id: str) -> dict[str, object]:
    database = _open_database(path)
    try:
        row = database.execute(
            "SELECT Id, MinVCValue, MaxVCValue "
            "FROM _Template_Enchantment WHERE Id=?",
            (template_id,),
        ).fetchone()
        return {} if row is None else dict(row)
    finally:
        database.close()


def refresh_item_economy_values(
    static_database_path: str | Path,
    item: ItemInfo,
    *,
    force_melt_value: bool = False,
) -> ItemInfo:
    """Populate the client-required VC and melt values on one item."""

    from dso_server.gameplay.item_economy_values import (
        equipment_melt_value,
        item_stack_vc_value,
    )

    path = str(Path(static_database_path))
    row = _item_economy_row(path, item.template_id)
    item.field_30 = item_stack_vc_value(item, row)
    if force_melt_value or int(item.field_34) <= 0:
        enchantments = {
            enchantment.template_id: _enchantment_economy_row(
                path, enchantment.template_id
            )
            for enchantment in item.enchantments
        }
        item.field_34 = equipment_melt_value(item, row, enchantments)
    return item


def refresh_inventory_economy_values(
    static_database_path: str | Path,
    inventory: InventoryInfo,
    *,
    item_ids: tuple[int, ...] | None = None,
    force_melt_value: bool = False,
) -> InventoryInfo:
    selected = None if item_ids is None else {int(value) for value in item_ids}
    for item in inventory.items:
        if selected is not None and int(item.item_id) not in selected:
            continue
        refresh_item_economy_values(
            static_database_path,
            item,
            force_melt_value=force_melt_value,
        )
    return inventory


def item_melt_reward(
    static_database_path: str | Path,
    item: ItemInfo,
) -> ItemMeltReward:
    """Resolve the authoritative melt destination and amount for an item."""

    from dso_server.gameplay.item_economy_values import (
        GEM_MELT_CATEGORIES,
        RUNE_MELT_CATEGORIES,
        equipment_melt_value,
        is_meltable_equipment,
        item_stack_vc_value,
    )

    path = str(Path(static_database_path))
    row = _item_economy_row(path, item.template_id)
    category = str(row.get("ItemCategory") or "")
    if category in GEM_MELT_CATEGORIES:
        amount = int(item.field_30) or item_stack_vc_value(item, row)
        if amount <= 0:
            raise ValueError(f"gem {item.template_id!r} has no dust value")
        return ItemMeltReward(0, "ingredient_gem_dust", amount)
    if category in RUNE_MELT_CATEGORIES:
        amount = int(item.field_30) or item_stack_vc_value(item, row)
        if amount <= 0:
            raise ValueError(f"rune {item.template_id!r} has no dust value")
        return ItemMeltReward(0, "ingredient_rune_dust", amount)
    if not is_meltable_equipment(row):
        raise ValueError(f"item {item.template_id!r} cannot be melted")
    glyphs = int(item.field_34)
    if glyphs <= 0:
        enchantments = {
            enchantment.template_id: _enchantment_economy_row(
                path, enchantment.template_id
            )
            for enchantment in item.enchantments
        }
        glyphs = equipment_melt_value(item, row, enchantments)
    if glyphs <= 0:
        raise ValueError(f"equipment {item.template_id!r} has no glyph value")
    return ItemMeltReward(glyphs=glyphs)


def credit_fixed_currency(
    state: CurrencyChanged,
    *,
    andermant: int = 0,
    gold: int = 0,
    glyph: int = 0,
    badge: int = 0,
    legacy: int = 0,
    draken: int = 0,
) -> CurrencyChanged:
    values = (andermant, gold, glyph, badge, legacy, draken)
    if any(int(value) < 0 for value in values):
        raise ValueError("currency credit cannot be negative")
    return CurrencyChanged(
        int(state.andermant) + int(andermant),
        int(state.gold) + int(gold),
        (
            int(state.badge_of_honor) + int(badge),
            int(state.legacy_progression) + int(legacy),
            int(state.draken) + int(draken),
        ),
        int(state.glyph_of_power) + int(glyph),
        False,
        state.wallet_base,
    )


def credit_currency_item(
    static_database_path: str | Path,
    state: CurrencyChanged,
    game_currencies: dict[str, int],
    item: ItemInfo,
) -> CurrencyItemCredit | None:
    return credit_currency_template(
        static_database_path,
        state,
        game_currencies,
        item.template_id,
        max(1, int(item.amount)),
    )


def credit_currency_template(
    static_database_path: str | Path,
    state: CurrencyChanged,
    game_currencies: dict[str, int],
    template_id: str,
    amount: int,
    *,
    scale_by_item_value: bool = True,
) -> CurrencyItemCredit | None:
    """Credit a mapped currency reward without materializing an item first."""

    if int(amount) <= 0:
        raise ValueError("currency reward amount must be positive")
    row = _item_economy_row(
        str(Path(static_database_path)), str(template_id)
    )
    category = str(row.get("ItemCategory") or "")
    template_id = str(template_id)
    if (
        category not in {"Currency", "NoAutoUseCurrency", "ItemCurrency"}
        and template_id not in GAME_CURRENCY_BY_WIRE_ID
    ):
        return None
    stack = int(amount)
    unit = 1
    if scale_by_item_value:
        unit = max(
            1,
            int(row.get("PriceRC") or 0),
            int(row.get("PriceVC") or 0),
            int(row.get("PriceGC") or 0),
            int(row.get("BaseValue") or 0),
        )
    amount = stack * unit
    fixed_name = {
        "money_rc": "andermant",
        "money_vc": "gold",
        "money_vc_gold": "gold",
        "money_gc_no_auto_use": "glyph",
        "money_uc_no_auto_use": "legacy",
        "money_hc_no_auto_use": "badge",
        "e_chr_2012_event_currency": "draken",
    }.get(template_id)
    if fixed_name is not None:
        return CurrencyItemCredit(
            credit_fixed_currency(state, **{fixed_name: amount}),
            normalize_game_currency_balances(game_currencies),
            None,
            amount,
            fixed_name,
        )
    if template_id in GAME_CURRENCY_BY_WIRE_ID:
        wallet = normalize_game_currency_balances(game_currencies)
        wallet[template_id] = int(wallet.get(template_id, 0)) + amount
        return CurrencyItemCredit(
            state,
            wallet,
            GameCurrencyChanged(template_id, wallet[template_id]),
            amount,
            GAME_CURRENCY_BY_WIRE_ID[template_id].name,
        )
    return None
