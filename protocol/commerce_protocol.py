"""Typed vendor transactions and database-driven inventory delivery.

The client sends ``TransactionCommand`` (0x005b) after an ordinary NPC offer
is selected.  Item destinations are not inferred from names: live entries in
``_Template_IngredientBag`` go to InventoryInfo's ingredient/currency-bag
arrays, while other items use ordinary ``storage_58`` slots and then the
unlocked premium ``storage_80`` bag.
"""

from __future__ import annotations

from dso_server.runtime import resident_content
from dso_server.protocol.command_ids import command_id
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from dso_server.protocol.character_selection import BitReader
from dso_server.protocol.currency_protocol import (
    GAME_CURRENCY_BY_WIRE_ID,
    GameCurrencyChanged,
    normalize_game_currency_balances,
)
from dso_server.protocol.inventory_codec import (
    InventoryInfo,
    ItemInfo,
    clone_inventory_info,
    free_inventory_location,
)
from dso_server.protocol.player_init_protocol import CurrencyChanged
from dso_server.protocol.shop_protocol import OFFER_RARITY_IDS, ShopOffer


TRANSACTION_COMMAND = command_id("Commands::TransactionCommand", 0x005B)
@dataclass(frozen=True)
class TransactionEntry:
    offer_id: str
    field_58: int
    field_5c: int
    amount: int


@dataclass(frozen=True)
class Transaction:
    entries: tuple[TransactionEntry | None, ...]
    shop_id: str
    field_80: bool


@dataclass(frozen=True)
class PurchaseItemDefinition:
    template_id: str
    max_stack_size: int
    rarity: int
    required_level: int
    item_category: str
    ingredient_bag: bool
    slot_types: tuple[str, ...] = ()
    minimum_item_level: int = 1
    maximum_item_level: int = 200
    unit_vc_price: int = 0


@dataclass(frozen=True)
class PurchasePrice:
    rc: int = 0
    vc: int = 0
    gc: int = 0
    ec: int = 0
    uc: int = 0
    arena: int = 0
    ic: int = 0

    def __add__(self, other: "PurchasePrice") -> "PurchasePrice":
        return PurchasePrice(*(
            left + right
            for left, right in zip(self.as_tuple(), other.as_tuple())
        ))

    def as_tuple(self) -> tuple[int, int, int, int, int, int, int]:
        return (
            self.rc, self.vc, self.gc, self.ec,
            self.uc, self.arena, self.ic,
        )


@dataclass(frozen=True)
class InventoryPurchaseMutation:
    changed_items: tuple[ItemInfo, ...]
    replaced_item_ids: tuple[int, ...]
    replacements: dict[int, int]
    delta: InventoryInfo
    final: InventoryInfo
    next_item_id: int


def decode_transaction(
    payload: bytes, bit_length: int | None = None
) -> Transaction:
    """Decode the generated TransactionCommand without byte-alignment guesses."""

    if bit_length is None:
        bit_length = len(payload) * 8
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x8B:
        raise ValueError("Transaction is not a client command")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != TRANSACTION_COMMAND:
        raise ValueError(
            f"expected Transaction 0x005b, got 0x{command_id:04x}"
        )

    entries: list[TransactionEntry | None] = []
    for _ in range(reader.read_uint32_le()):
        if not reader.read_bool():
            entries.append(None)
            continue
        entries.append(
            TransactionEntry(
                reader.read_string(),
                reader.read_uint32_le(),
                reader.read_uint32_le(),
                reader.read_uint32_le(),
            )
        )
    result = Transaction(
        tuple(entries), reader.read_string(), reader.read_bool()
    )
    if reader.remaining:
        padding = reader.remaining
        if bit_length != len(payload) * 8 or padding >= 8:
            raise ValueError(f"Transaction has {padding} unread bits")
        if reader.read_bits(padding):
            raise ValueError("Transaction has non-zero byte padding")
    return result


def _open_read_only(path: Path) -> sqlite3.Connection:
    if not resident_content.is_file(path):
        raise FileNotFoundError(f"static database not found: {path}")
    database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    return database


def load_purchase_item_definition(
    static_database_path: str | Path, template_id: str
) -> PurchaseItemDefinition:
    """Load serializer-relevant item metadata and its inventory destination."""

    database = _open_read_only(Path(static_database_path))
    try:
        row = database.execute(
            "SELECT Id, MaxStackSize, DefaultRarity, RequiredLevel, "
            "ItemCategory, PriceVC FROM _Template_Item WHERE Id=?",
            (template_id,),
        ).fetchone()
        level_row = database.execute(
            'SELECT SlotType, MinDropLevel, MaxDropLevel '
            'FROM _Template_Item WHERE Id=?',
            (template_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown item template {template_id!r}")
        ingredient_bag = database.execute(
            "SELECT 1 FROM _Template_IngredientBag "
            "WHERE ItemTemplateId=? AND IsLive=1 LIMIT 1",
            (template_id,),
        ).fetchone() is not None
        rarity_name = str(row["DefaultRarity"] or "Common")
        try:
            rarity = OFFER_RARITY_IDS[rarity_name]
        except KeyError as exc:
            raise ValueError(
                f"item {template_id!r} uses unknown rarity {rarity_name!r}"
            ) from exc
        return PurchaseItemDefinition(
            template_id=str(row["Id"]),
            max_stack_size=max(1, int(row["MaxStackSize"] or 1)),
            rarity=rarity,
            required_level=max(1, int(row["RequiredLevel"] or 1)),
            item_category=str(row["ItemCategory"] or ""),
            ingredient_bag=ingredient_bag,
            slot_types=tuple(
                value.strip()
                for value in str(level_row['SlotType'] or '')
                .replace(';', ',').split(',')
                if value.strip()
            ),
            minimum_item_level=max(
                1, int(level_row['MinDropLevel'] or 1)
            ),
            maximum_item_level=max(
                1, int(level_row['MaxDropLevel'] or 200)
            ),
            unit_vc_price=max(0, int(row["PriceVC"] or 0)),
        )
    finally:
        database.close()


def price_for_offer(offer: ShopOffer, amount: int) -> PurchasePrice:
    """Resolve the effective static price, including the live Veteran discount."""

    if amount <= 0:
        raise ValueError("transaction amount must be positive")
    discount = max(
        (float(value.value) for value in offer.veteran_discounts),
        default=0.0,
    )

    def discounted(value: int) -> int:
        # Retail charged 38 for the 40-RC soul-stone offer with Veteran3's
        # 5% discount. Round-to-nearest preserves that observed result.
        return max(0, round(int(value) * (1.0 - discount))) * amount

    return PurchasePrice(*(
        discounted(value)
        for value in (
            offer.price_rc,
            offer.price_vc,
            offer.price_gc,
            offer.price_ec,
            offer.price_uc,
            offer.price_arena,
            offer.price_ic,
        )
    ))


def debit_currency(
    state: CurrencyChanged, price: PurchasePrice, *, ic_balance: int = 0
) -> tuple[CurrencyChanged, int]:
    """Debit the seven ordinary shop price domains from session state."""

    # Database price domains map to the verified CurrencyChanged fields:
    # RC=Andermant, VC=gold, GC=Glyphs, EC=Draken, UC=the remaining
    # legacy progression balance, Arena=Badge of Honor.  IC is shop-specific
    # and is handled as a keyed game currency by debit_purchase_currency().
    current = (
        int(state.andermant),
        int(state.gold),
        int(state.glyph_of_power),
        int(state.draken),
        int(state.legacy_progression),
        int(state.badge_of_honor),
        int(ic_balance),
    )
    required = price.as_tuple()
    if any(need > have for have, need in zip(current, required)):
        raise ValueError("insufficient vendor currency")
    remaining = tuple(
        have - need for have, need in zip(current, required)
    )
    return (
        CurrencyChanged(
            remaining[0],
            remaining[1],
            (remaining[5], remaining[4], remaining[3]),
            remaining[2],
            False,
            state.wallet_base,
        ),
        remaining[6],
    )


def load_shop_currency_resource(
    static_database_path: str | Path, shop_id: str
) -> str | None:
    """Return the PriceIC wallet resource selected by a shop.

    ``_Template_ItemCurrency.Id`` is the shop ID and ``CurrencyItem`` is the
    exact key used by the player's game-currency wallet.  Honor and Draken
    retain historical item IDs but map to fixed CurrencyChanged fields.
    """

    database = _open_read_only(Path(static_database_path))
    try:
        row = database.execute(
            "SELECT CurrencyItem FROM _Template_ItemCurrency WHERE Id=?",
            (str(shop_id),),
        ).fetchone()
        if row is None or not str(row["CurrencyItem"] or ""):
            return None
        return str(row["CurrencyItem"])
    finally:
        database.close()


def debit_purchase_currency(
    state: CurrencyChanged,
    game_currencies: dict[str, int],
    price: PurchasePrice,
    *,
    shop_currency_resource: str | None,
) -> tuple[
    CurrencyChanged,
    dict[str, int],
    GameCurrencyChanged | None,
]:
    """Debit all fixed and shop-specific price domains atomically."""

    fixed, _unused_ic = debit_currency(
        state,
        PurchasePrice(
            rc=price.rc,
            vc=price.vc,
            gc=price.gc,
            ec=price.ec,
            uc=price.uc,
            arena=price.arena,
        ),
    )
    wallet = normalize_game_currency_balances(game_currencies)
    update = None
    if price.ic:
        resource_id = str(shop_currency_resource or "")
        if not resource_id:
            raise ValueError(
                "PriceIC transaction has no _Template_ItemCurrency mapping"
            )
        if resource_id == "money_hc_no_auto_use":
            if price.ic > fixed.badge_of_honor:
                raise ValueError("insufficient Badge of Honor")
            fixed = CurrencyChanged(
                fixed.andermant,
                fixed.gold,
                (
                    fixed.badge_of_honor - price.ic,
                    fixed.legacy_progression,
                    fixed.draken,
                ),
                fixed.glyph_of_power,
                False,
                fixed.wallet_base,
            )
        elif resource_id == "e_chr_2012_event_currency":
            if price.ic > fixed.draken:
                raise ValueError("insufficient Draken")
            fixed = CurrencyChanged(
                fixed.andermant,
                fixed.gold,
                (
                    fixed.badge_of_honor,
                    fixed.legacy_progression,
                    fixed.draken - price.ic,
                ),
                fixed.glyph_of_power,
                False,
                fixed.wallet_base,
            )
        else:
            definition = GAME_CURRENCY_BY_WIRE_ID.get(resource_id)
            if definition is None:
                raise ValueError(
                    f"shop uses unknown game currency {resource_id!r}"
                )
            current = int(wallet.get(resource_id, 0))
            if price.ic > current:
                raise ValueError(
                    f"insufficient {definition.name.replace('_', ' ')}"
                )
            remaining = current - price.ic
            wallet[resource_id] = remaining
            update = GameCurrencyChanged(resource_id, remaining)
    return fixed, wallet, update


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
            if value == old_id:
                values[index] = new_id


def _new_item(
    definition: PurchaseItemDefinition,
    item_id: int,
    amount: int,
    item_level: int | None = None,
) -> ItemInfo:
    now = datetime.now(timezone.utc)
    resolved_level = definition.required_level
    if definition.slot_types and item_level is not None:
        resolved_level = min(
            max(1, int(item_level)),
            definition.maximum_item_level,
        )
    item = ItemInfo(
        item_id=item_id,
        amount=amount,
        field_08=definition.rarity,
        socket_count=0,
        socketed_templates=[],
        item_level=resolved_level,
        field_18=item_id & 0xFFFF,
        field_30=max(0, int(definition.unit_vc_price)) * int(amount),
        field_38=0,
        template_id=definition.template_id,
        enchantments=[],
        created_at=(
            now.year, now.month, now.day,
            now.hour, now.minute, now.second,
        ),
    )
    if definition.slot_types:
        from dso_server.gameplay.item_economy_values import equipment_melt_value

        item.field_34 = equipment_melt_value(
            item,
            {
                "ItemCategory": definition.item_category,
                "SlotType": ",".join(definition.slot_types),
                "PriceVC": definition.unit_vc_price,
            },
            {},
        )
    return item


def add_purchased_item(
    info: InventoryInfo,
    definition: PurchaseItemDefinition,
    amount: int,
    *,
    next_item_id: int,
    item_level: int | None = None,
) -> InventoryPurchaseMutation:
    """Mutate session inventory and create the two retail InventoryInfo views."""

    if amount <= 0:
        raise ValueError("purchased item amount must be positive")
    changed_items: list[ItemInfo] = []
    replaced_ids: list[int] = []
    replacements: dict[int, int] = {}
    remaining = amount

    def in_destination(item_id: int) -> bool:
        return (
            item_id in info.field_180
            if definition.ingredient_bag
            else item_id in info.storage_58 or item_id in info.storage_80
        )

    # Retail replaces a modified stack with a newly allocated item id and
    # publishes new->old in field_158. Preserve that identity transition.
    for index, old_item in enumerate(list(info.items)):
        if (
            remaining <= 0
            or old_item.template_id != definition.template_id
            # Ordinary grants must not become owned by an unrelated quest.
            or old_item.quest_id
            or old_item.amount >= definition.max_stack_size
            or not in_destination(old_item.item_id)
        ):
            continue
        added = min(
            remaining, definition.max_stack_size - old_item.amount
        )
        new_id = next_item_id
        next_item_id += 1
        new_amount = old_item.amount + added
        new_item = replace(
            old_item,
            item_id=new_id,
            amount=new_amount,
            field_18=new_id & 0xFFFF,
            field_30=(
                max(0, int(definition.unit_vc_price)) * new_amount
            ),
        )
        info.items[index] = new_item
        _replace_item_id(info, old_item.item_id, new_id)
        changed_items.append(new_item)
        replaced_ids.append(old_item.item_id)
        replacements[new_id] = old_item.item_id
        remaining -= added

    while remaining:
        stack_amount = min(remaining, definition.max_stack_size)
        new_id = next_item_id
        next_item_id += 1
        new_item = _new_item(
            definition, new_id, stack_amount, item_level
        )
        info.items.append(new_item)
        if definition.ingredient_bag:
            info.field_180.append(new_id)
            if definition.template_id not in info.field_1a0:
                info.field_1a0.append(definition.template_id)
        else:
            location = free_inventory_location(info)
            if location is None:
                raise ValueError("ordinary inventory storage is full")
            storage_name, slot = location
            getattr(info, storage_name)[new_id] = slot
        changed_items.append(new_item)
        remaining -= stack_amount

    # Persistent state does not retain one-shot delta bookkeeping.
    info.changed_item_ids = []
    info.field_158 = {}
    info.field_1e0 = False

    delta = clone_inventory_info(info, changed_items)
    delta.changed_item_ids = list(replaced_ids)
    delta.field_158 = dict(replacements)
    delta.field_1e0 = True

    final = clone_inventory_info(info, ())
    final.items = []
    final.changed_item_ids = []
    final.field_158 = {}
    final.field_1e0 = False
    return InventoryPurchaseMutation(
        tuple(changed_items),
        tuple(replaced_ids),
        replacements,
        delta,
        final,
        next_item_id,
    )
