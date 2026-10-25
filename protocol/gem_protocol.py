"""Database-validated socket removal and insertion inventory mutations."""

from __future__ import annotations

from dso_server.protocol.command_ids import command_id
import copy
from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3

from dso_server.protocol.commerce_protocol import (
    add_purchased_item,
    load_purchase_item_definition,
)
from dso_server.protocol.inventory_codec import (
    INVENTORY_INFO_COMMAND,
    InventoryInfo,
    ItemInfo,
    write_inventory_info_body,
)
from dso_server.protocol.player_init_protocol import (
    CURRENCY_CHANGED_COMMAND,
    CurrencyChanged,
    _write_currency_changed_body,
)
from dso_server.protocol.inventory_economy_protocol import (
    InventoryMutationBuilder,
)
from dso_server.protocol.character_selection import BitWriter
from dso_server.protocol.shop_protocol import OFFER_RARITY_IDS

GEM_REMOVAL_RESPONSE_COMMAND = command_id(
    "Commands::GemRemovalResponseCommand", 0x0056
)


UNLOCK_GEM_SOCKET_ACTION = 0x0015
REMOVE_GEM_ACTION = 0x0114
INSERT_GEM_ACTION = 0x0211
ITEM_INFO_COMMAND = command_id("Commands::ItemUpdateCommand", 0x0030)
EQUIPMENT_GEM_CATEGORY = "EquipmentGem"
EQUIPMENT_JEWEL_CATEGORY = "EquipmentJewel"
EQUIPMENT_RUNE_CATEGORY = "EquipmentRune"
SOCKET_CATEGORY_BY_CONTAINER = {
    "JewelTrinket": EQUIPMENT_JEWEL_CATEGORY,
    "RuneTrinket": EQUIPMENT_RUNE_CATEGORY,
    "AncientTrinket": "EquipmentAncient",
}


@dataclass(frozen=True)
class GemMutation:
    inventory: InventoryInfo
    response_inventory: InventoryInfo | None
    equipment_item: ItemInfo
    equipment_item_id: int
    gem_template_id: str
    gem_item_id: int
    replaced_gem_item_id: int
    next_item_id: int
    socket_index: int


@dataclass(frozen=True)
class SocketUnlockMutation:
    inventory: InventoryInfo
    equipment_item: ItemInfo
    equipment_item_id: int
    unlocked_socket_count: int
    maximum_socket_count: int
    price_rc: int


@dataclass(frozen=True)
class RuneUpgradeMutation:
    inventory: InventoryInfo
    delta: InventoryInfo
    upgraded_item: ItemInfo
    next_item_id: int
    previous_template_id: str
    upgraded_template_id: str


@dataclass(frozen=True)
class GemUpgradeMutation:
    inventory: InventoryInfo
    delta: InventoryInfo
    upgraded_item: ItemInfo
    next_item_id: int
    previous_template_id: str
    upgraded_template_id: str


def _item(inventory: InventoryInfo, item_id: int) -> ItemInfo:
    item = next(
        (
            candidate
            for candidate in inventory.items
            if int(candidate.item_id) == int(item_id)
        ),
        None,
    )
    if item is None:
        raise ValueError(f"inventory item {item_id} does not exist")
    return item


def _normalize_sockets(item: ItemInfo) -> None:
    if item.socket_count < 0:
        raise ValueError("item socket count cannot be negative")
    if len(item.socketed_templates) < item.socket_count:
        item.socketed_templates.extend(
            "" for _ in range(item.socket_count - len(item.socketed_templates))
        )
    elif len(item.socketed_templates) > item.socket_count:
        del item.socketed_templates[item.socket_count :]


def _required_socket_category(
    static_database_path: str | Path,
    container: ItemInfo,
) -> str:
    """Return the only item category accepted by this socket container."""

    definition = load_purchase_item_definition(
        static_database_path, container.template_id
    )
    return SOCKET_CATEGORY_BY_CONTAINER.get(
        definition.item_category, EQUIPMENT_GEM_CATEGORY
    )


def _validate_socket_content(
    static_database_path: str | Path,
    container: ItemInfo,
    socket_item: ItemInfo,
) -> str:
    """Validate gem/jewel/rune compatibility and return its category."""

    definition = load_purchase_item_definition(
        static_database_path, socket_item.template_id
    )
    required = _required_socket_category(
        static_database_path, container
    )
    if definition.item_category != required:
        raise ValueError(
            f"item {socket_item.item_id} category "
            f"{definition.item_category!r} cannot be inserted into "
            f"{container.template_id!r}; expected {required!r}"
        )
    return definition.item_category


def _level_scaled_value(value: str, level: int) -> int:
    """Resolve the database's ``level:value`` progression notation."""

    result = 0
    for entry in str(value or "").split(","):
        if not entry.strip():
            continue
        threshold_text, separator, result_text = entry.partition(":")
        if not separator:
            raise ValueError(f"invalid level-scaled value {value!r}")
        threshold = int(threshold_text)
        if int(level) < threshold:
            break
        result = int(result_text)
    return result


def _socket_unlock_rules(
    static_database_path: str | Path,
    item: ItemInfo,
) -> tuple[int, int]:
    """Return the item's current maximum socket count and next RC price."""

    path = Path(static_database_path)
    if not path.is_file():
        raise FileNotFoundError(f"static database not found: {path}")
    database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    try:
        row = database.execute(
            """
            SELECT ItemCategory, MaxSocketCount
            FROM _Template_Item
            WHERE Id=?
            """,
            (item.template_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown item template {item.template_id!r}")
        if not str(row["ItemCategory"] or ""):
            raise ValueError(
                f"item {item.template_id!r} is not socketable equipment"
            )
        maximum = _level_scaled_value(
            str(row["MaxSocketCount"] or ""),
            max(1, int(item.item_level)),
        )
        if maximum <= 0:
            raise ValueError(
                f"item {item.template_id!r} has no purchasable sockets"
            )

        rarity_name = next(
            (
                name
                for name, rarity_id in OFFER_RARITY_IDS.items()
                if name != "None" and int(rarity_id) == int(item.field_08)
            ),
            None,
        )
        if rarity_name is None:
            raise ValueError(
                f"item {item.template_id!r} has unknown rarity "
                f"{item.field_08}"
            )
        next_count = int(item.socket_count) + 1
        if next_count > maximum:
            return maximum, 0
        cost = database.execute(
            f"""
            SELECT "{rarity_name}"
            FROM _Template_SocketExtensionCosts
            WHERE Amount=?
            """,
            (next_count,),
        ).fetchone()
        if cost is None:
            raise ValueError(
                f"socket {next_count} has no extension cost"
            )
        return maximum, max(0, int(cost[0] or 0))
    finally:
        database.close()


def unlock_gem_socket(
    inventory: InventoryInfo,
    equipment_item_id: int,
    *,
    static_database_path: str | Path,
) -> SocketUnlockMutation:
    """Purchase exactly the next sequential socket on an equipment item."""

    working = copy.deepcopy(inventory)
    equipment = _item(working, equipment_item_id)
    _normalize_sockets(equipment)
    maximum, price_rc = _socket_unlock_rules(
        static_database_path,
        equipment,
    )
    if equipment.socket_count >= maximum:
        raise ValueError(
            f"item {equipment_item_id} already has its "
            f"{maximum} available sockets"
        )

    # The client uses both fields: socket_count controls how many slots are
    # enabled, and socketed_templates carries the value in each enabled slot.
    # Advancing only the purchase cursor leaves the newly bought slot grey.
    equipment.socket_count += 1
    equipment.socketed_templates.append("")
    working.changed_item_ids = []
    working.field_158 = {}
    working.field_1e0 = False
    return SocketUnlockMutation(
        inventory=working,
        equipment_item=copy.deepcopy(equipment),
        equipment_item_id=int(equipment_item_id),
        unlocked_socket_count=int(equipment.socket_count),
        maximum_socket_count=int(maximum),
        price_rc=int(price_rc),
    )


def promote_local_loot_equipment_levels(
    inventory: InventoryInfo,
    *,
    static_database_path: str | Path,
    target_item_level: int,
) -> tuple[InventoryInfo, tuple[int, ...]]:
    """Raise old local test drops to the configured player's item level.

    Local world-loot equipment is serialized with ``field_30 == 11``. Earlier
    builds generated those items at the static monster level even while the
    forced-drop testing mode was active. That made a level-100 player receive,
    for example, a level-6 pistol whose client-owned ``MaxSocketCount`` rule
    permits only five of the ten displayed sockets.

    Only socketable equipment carrying that local-loot marker is changed.
    Vendor items, consumables, gems, skins, and equipment already at or above
    the requested level are preserved.
    """

    target = max(1, int(target_item_level))
    candidates = [
        item
        for item in inventory.items
        if (
            int(item.field_30) == 11
            and int(item.item_level) < target
        )
    ]
    if not candidates:
        return inventory, ()

    path = Path(static_database_path)
    if not path.is_file():
        raise FileNotFoundError(f"static database not found: {path}")
    database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        template_ids = tuple(
            dict.fromkeys(item.template_id for item in candidates)
        )
        placeholders = ",".join("?" for _ in template_ids)
        socketable = {
            str(row[0])
            for row in database.execute(
                f"""
                SELECT Id
                FROM _Template_Item
                WHERE Id IN ({placeholders})
                  AND COALESCE(SlotType, '')<>''
                  AND COALESCE(MaxSocketCount, '')<>''
                """,
                template_ids,
            )
        }
    finally:
        database.close()

    changed_ids = tuple(
        int(item.item_id)
        for item in candidates
        if item.template_id in socketable
    )
    if not changed_ids:
        return inventory, ()

    working = copy.deepcopy(inventory)
    changed_id_set = set(changed_ids)
    for item in working.items:
        if int(item.item_id) in changed_id_set:
            item.item_level = target
    return working, changed_ids


def remove_gem(
    inventory: InventoryInfo,
    equipment_item_id: int,
    *,
    static_database_path,
    next_item_id: int,
    socket_index: int | None = None,
) -> GemMutation:
    """Remove the requested socket and return its gem to storage.

    ``InventoryItemAction.field_0c`` is the zero-based socket selected by the
    client for remove action ``0x0114``. Falling back to the first occupied
    socket is retained for callers which do not originate from that wire
    command.
    """

    working = copy.deepcopy(inventory)
    equipment = _item(working, equipment_item_id)
    _normalize_sockets(equipment)
    if socket_index is None:
        socket_index = next(
            (
                index
                for index, template_id in enumerate(
                    equipment.socketed_templates
                )
                if template_id
            ),
            None,
        )
    if socket_index is None:
        raise ValueError(
            f"item {equipment_item_id} has no removable socketed gem"
        )
    socket_index = int(socket_index)
    if not 0 <= socket_index < equipment.socket_count:
        raise ValueError(
            f"item {equipment_item_id} socket {socket_index} is out of range"
        )
    if not equipment.socketed_templates[socket_index]:
        raise ValueError(
            f"item {equipment_item_id} socket {socket_index} is empty"
        )
    gem_template_id = equipment.socketed_templates[socket_index]
    definition = load_purchase_item_definition(
        static_database_path, gem_template_id
    )
    required_category = _required_socket_category(
        static_database_path, equipment
    )
    if definition.item_category != required_category:
        raise ValueError(
            f"socket {socket_index} contains {definition.item_category!r} "
            f"{gem_template_id!r}, expected {required_category!r}"
        )
    equipment.socketed_templates[socket_index] = ""
    added = add_purchased_item(
        working,
        definition,
        1,
        next_item_id=int(next_item_id),
    )

    # Retail's removal response is a deliberate full-state/delta hybrid.  Its
    # item array contains only the modified equipment and returned gem, while
    # every inventory relationship (equipment/storage maps, currency-bag
    # lists, capacities, and auxiliary template lists) is repeated in full.
    # Sending ``added.delta`` here emptied those relationship collections in
    # the client.  The socket icons remained visible, but only the first
    # widget retained a usable backing relationship; the other occupied
    # sockets then ignored clicks.
    response_inventory = copy.deepcopy(working)
    response_inventory.items = [
        copy.deepcopy(equipment),
        *copy.deepcopy(list(added.changed_items)),
    ]
    response_inventory.changed_item_ids = copy.deepcopy(
        added.delta.changed_item_ids
    )
    response_inventory.field_158 = copy.deepcopy(added.delta.field_158)
    # The retail no-stack removal has this flag clear. The replacement path
    # uses the same true value already proven by stacking vendor transactions.
    response_inventory.field_1e0 = bool(added.replaced_item_ids)
    return GemMutation(
        # add_purchased_item mutates ``working`` into the durable inventory.
        # Its ``final`` member is an intentionally item-empty follow-up
        # InventoryInfo command used only by the vendor wire sequence.
        inventory=working,
        response_inventory=response_inventory,
        equipment_item=copy.deepcopy(equipment),
        equipment_item_id=int(equipment_item_id),
        gem_template_id=gem_template_id,
        gem_item_id=int(added.changed_items[0].item_id),
        replaced_gem_item_id=(
            int(added.replaced_item_ids[0])
            if added.replaced_item_ids
            else 0
        ),
        next_item_id=added.next_item_id,
        socket_index=socket_index,
    )


def insert_gem(
    inventory: InventoryInfo,
    gem_item_id: int,
    equipment_item_id: int,
    *,
    static_database_path,
    socket_index: int | None = None,
) -> GemMutation:
    """Consume one gem and place it in the requested equipment socket.

    ``InventoryItemAction.field_10`` is the zero-based destination socket.
    """

    working = copy.deepcopy(inventory)
    gem = _item(working, gem_item_id)
    equipment = _item(working, equipment_item_id)
    if gem.template_id == "unique_gem_crafting":
        raise ValueError(
            "Rune Crafting Joker must be applied to a rune item, not a socket"
        )
    _validate_socket_content(
        static_database_path, equipment, gem
    )


    definition = load_purchase_item_definition(
        static_database_path, gem.template_id
    )
    _normalize_sockets(equipment)
    if socket_index is None:
        socket_index = next(
            (
                index
                for index, template_id in enumerate(
                    equipment.socketed_templates
                )
                if not template_id
            ),
            None,
        )
    if socket_index is None:
        raise ValueError(
            f"item {equipment_item_id} has no empty gem socket"
        )
    socket_index = int(socket_index)
    if not 0 <= socket_index < equipment.socket_count:
        raise ValueError(
            f"item {equipment_item_id} socket {socket_index} is out of range"
        )
    if equipment.socketed_templates[socket_index]:
        raise ValueError(
            f"item {equipment_item_id} socket {socket_index} is occupied"
        )
    if gem.amount <= 0:
        raise ValueError(f"gem item {gem_item_id} has an invalid stack")
    equipment.socketed_templates[socket_index] = gem.template_id
    if gem.amount > 1:
        gem.amount -= 1
    else:
        working.items.remove(gem)
        for values in (
            working.equipment_slots,
            working.storage_58,
            working.storage_80,
            working.storage_a8,
            working.storage_d0,
        ):
            values.pop(int(gem_item_id), None)
        for values in (
            working.field_f8,
            working.field_118,
            working.field_180,
        ):
            values[:] = [
                value for value in values
                if int(value) != int(gem_item_id)
            ]
    working.changed_item_ids = []
    working.field_158 = {}
    working.field_1e0 = False
    return GemMutation(
        inventory=working,
        response_inventory=None,
        equipment_item=copy.deepcopy(equipment),
        equipment_item_id=int(equipment_item_id),
        gem_template_id=definition.template_id,
        gem_item_id=int(gem_item_id),
        replaced_gem_item_id=0,
        next_item_id=0,
        socket_index=socket_index,
    )


def upgrade_rune(
    inventory: InventoryInfo,
    crafting_item_id: int,
    rune_item_id: int,
    *,
    static_database_path,
    next_item_id: int,
) -> RuneUpgradeMutation:
    """Use the Rune Crafting Joker to raise one rune by one tier."""

    from dso_server.protocol.commerce_protocol import (
        load_purchase_item_definition,
    )

    builder = InventoryMutationBuilder(inventory, next_item_id)
    crafting_item = builder.item(crafting_item_id)
    rune_item = builder.item(rune_item_id)
    if crafting_item.template_id != "unique_gem_crafting":
        raise ValueError(
            f"item {crafting_item_id} is not a Rune Crafting Joker"
        )
    if int(crafting_item.amount) <= 0 or int(rune_item.amount) <= 0:
        raise ValueError("rune upgrade requires non-empty item stacks")
    if int(crafting_item_id) == int(rune_item_id):
        raise ValueError("a crafting joker cannot upgrade itself")

    rune_definition = load_purchase_item_definition(
        static_database_path, rune_item.template_id
    )
    if rune_definition.item_category != "EquipmentRune":
        raise ValueError(
            f"item {rune_item_id} is {rune_definition.item_category!r}, "
            "not an equipment rune"
        )
    match = re.fullmatch(r"(.+)_([0-9]{2})", rune_item.template_id)
    if match is None:
        raise ValueError(
            f"rune {rune_item.template_id!r} has no next crafting tier"
        )
    prefix, current_tier_text = match.groups()
    next_tier = int(current_tier_text) + 1
    upgraded_template_id = f"{prefix}_{next_tier:02d}"
    upgraded_definition = load_purchase_item_definition(
        static_database_path, upgraded_template_id
    )
    if upgraded_definition.item_category != "EquipmentRune":
        raise ValueError(
            f"rune {rune_item.template_id!r} is already at its maximum tier"
        )

    builder.consume(crafting_item_id, 1, preserve_item_id=True)
    if int(rune_item.amount) > 1:
        builder.consume(rune_item_id, 1, preserve_item_id=True)
        builder.add_item(
            upgraded_definition,
            1,
            item_level=max(1, int(rune_item.item_level)),
        )
        upgraded_item = next(
            item for item in builder.changed_items
            if item.template_id == upgraded_template_id
        )
    else:
        rune_item.template_id = upgraded_template_id
        # ``field_08`` is the item's rarity ID, which drives the inventory
        # frame color in the client.  Changing only the template updates the
        # title and rune effect but leaves the old rarity border in place.
        rune_item.field_08 = upgraded_definition.rarity
        builder.changed_items.append(copy.deepcopy(rune_item))
        upgraded_item = copy.deepcopy(rune_item)

    mutation = builder.finish()
    return RuneUpgradeMutation(
        inventory=mutation.inventory,
        delta=mutation.delta,
        upgraded_item=upgraded_item,
        next_item_id=mutation.next_item_id,
        previous_template_id=rune_definition.template_id,
        upgraded_template_id=upgraded_template_id,
    )


def upgrade_gem(
    inventory: InventoryInfo,
    crafting_item_id: int,
    gem_item_id: int,
    *,
    static_database_path,
    next_item_id: int,
) -> GemUpgradeMutation:
    """Use a Gem Crafting Joker to upgrade one equipment gem by one tier."""

    builder = InventoryMutationBuilder(inventory, next_item_id)
    crafting_item = builder.item(crafting_item_id)
    gem = builder.item(gem_item_id)
    if crafting_item.template_id != "gem_crafting":
        raise ValueError(f"item {crafting_item_id} is not a Gem Crafting Joker")
    if int(crafting_item.amount) <= 0 or int(gem.amount) <= 0:
        raise ValueError("gem upgrade requires non-empty item stacks")
    if int(crafting_item_id) == int(gem_item_id):
        raise ValueError("a crafting joker cannot upgrade itself")

    gem_definition = load_purchase_item_definition(
        static_database_path, gem.template_id
    )
    if gem_definition.item_category != EQUIPMENT_GEM_CATEGORY:
        raise ValueError(
            f"item {gem_item_id} is {gem_definition.item_category!r}, "
            "not an equipment gem"
        )

    database = sqlite3.connect(
        f"file:{Path(static_database_path)}?mode=ro", uri=True
    )
    try:
        recipe = database.execute(
            "SELECT CraftedItemId FROM _Template_CraftingRecipes "
            "WHERE IngredientItemId1=? AND IngredientItemId2='gem_crafting:1' "
            "AND CraftingRecipeType='ItemUpgrade' AND IsLive=1 "
            "ORDER BY Id LIMIT 1",
            (f"{gem.template_id}:1",),
        ).fetchone()
    finally:
        database.close()
    if recipe is None or not recipe[0]:
        raise ValueError(
            f"gem {gem.template_id!r} has no Gem Crafting Joker upgrade"
        )

    upgraded_template_id = str(recipe[0]).split(":", 1)[0]
    upgraded_definition = load_purchase_item_definition(
        static_database_path, upgraded_template_id
    )
    if upgraded_definition.item_category != EQUIPMENT_GEM_CATEGORY:
        raise ValueError(
            f"upgrade result {upgraded_template_id!r} is not an equipment gem"
        )

    builder.consume(crafting_item_id, 1, preserve_item_id=True)
    if int(gem.amount) > 1:
        builder.consume(gem_item_id, 1, preserve_item_id=True)
        builder.add_item(
            upgraded_definition,
            1,
            item_level=max(1, int(gem.item_level)),
        )
        upgraded_item = next(
            item for item in builder.changed_items
            if item.template_id == upgraded_template_id
        )
    else:
        gem.template_id = upgraded_template_id
        gem.field_08 = upgraded_definition.rarity
        builder.changed_items.append(copy.deepcopy(gem))
        upgraded_item = copy.deepcopy(gem)

    mutation = builder.finish()
    return GemUpgradeMutation(
        inventory=mutation.inventory,
        delta=mutation.delta,
        upgraded_item=upgraded_item,
        next_item_id=mutation.next_item_id,
        previous_template_id=gem_definition.template_id,
        upgraded_template_id=upgraded_template_id,
    )

def _write_item_info_response_body(
    writer: BitWriter, item: ItemInfo
) -> None:
    _normalize_sockets(item)
    writer.write_uint32_le(int(item.item_id))
    writer.write_uint32_le(int(item.amount))
    writer.write_uint32_le(int(item.socket_count))
    writer.write_uint32_le(0)
    writer.write_uint32_le(int(item.field_34))
    writer.write_uint32_le(0)
    writer.write_uint32_le(int(item.item_level))
    writer.write_bool(bool(item.field_3c))
    writer.write_bool(bool(item.field_3d))
    writer.write_uint32_le(int(item.field_20))
    writer.write_uint32_le(int(item.field_1c))
    writer.write_uint32_le(len(item.socketed_templates))
    for template_id in item.socketed_templates:
        writer.write_string(template_id)
    writer.write_string(item.field_f8)
    writer.write_bool(bool(item.field_3e))


def encode_item_info_response(
    item: ItemInfo, *, actor_id: int
) -> tuple[bytes, int]:
    """Encode retail ItemInfoCommand (0x0030) for one changed item.

    This is the compact mutable-item representation used after gem insertion
    and removal, not the larger ``Game::ItemInfo`` embedded in InventoryInfo.
    Its complete field order was verified bit-for-bit against the retail gem
    insertion in ``capture_20260909_000000_sample_01.log``.
    """

    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(ITEM_INFO_COMMAND.to_bytes(2, "little"))
    _write_item_info_response_body(writer, item)
    writer.write_uint32_le(int(actor_id))
    writer.write_uint8(0xFF)
    return writer.finish()


def encode_socket_unlock_sequence(
    inventory: InventoryInfo,
    *,
    equipment_item: ItemInfo,
    currency: CurrencyChanged,
    actor_id: int,
) -> tuple[bytes, int]:
    """Publish a socket purchase and refresh the open jeweller selection.

    ``InventoryInfo`` commits the authoritative inventory, but the jeweller
    caches the currently selected item's compact mutable representation.
    Sending ``ItemInfoCommand`` before the currency acknowledgement rebuilds
    that cache immediately, so the new empty socket becomes interactive
    without selecting another item first.
    """

    writer = BitWriter()
    writer.write_uint8(0x85)

    writer.write_bytes(INVENTORY_INFO_COMMAND.to_bytes(2, "little"))
    write_inventory_info_body(writer, inventory)
    writer.write_uint32_le(int(actor_id))

    writer.write_uint8(0xFF)
    writer.write_bytes(ITEM_INFO_COMMAND.to_bytes(2, "little"))
    _write_item_info_response_body(writer, equipment_item)
    writer.write_uint32_le(int(actor_id))

    writer.write_uint8(0xFF)
    writer.write_bytes(CURRENCY_CHANGED_COMMAND.to_bytes(2, "little"))
    _write_currency_changed_body(writer, currency)
    writer.write_uint32_le(int(actor_id))

    writer.write_uint8(0xFF)
    return writer.finish()


def encode_gem_removal_response(
    gem_item_id: int,
    *,
    replaced_gem_item_id: int = 0,
    actor_id: int,
) -> tuple[bytes, int]:
    """Encode retail GemRemovalResponseCommand (0x0056).

    The two values are the newly allocated/rekeyed gem stack and the old stack
    it replaces. Disassembly of ``UI::ItemUpgradeWidget`` at 0x14042bde0
    confirms that the client walks all ten socket widgets and rewrites every
    reference matching the old id. The second value is zero only when removal
    creates a brand-new stack.
    """

    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(GEM_REMOVAL_RESPONSE_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(int(gem_item_id))
    writer.write_uint32_le(int(replaced_gem_item_id))
    writer.write_uint32_le(int(actor_id))
    writer.write_uint8(0xFF)
    return writer.finish()


def encode_gem_removal_sequence(
    inventory: InventoryInfo,
    *,
    gem_item_id: int,
    replaced_gem_item_id: int,
    equipment_item: ItemInfo,
    currency: CurrencyChanged,
    actor_id: int,
) -> tuple[bytes, int]:
    """Encode the ordered retail gem-removal state transition.

    InventoryInfo mutates storage/equipment first, GemRemovalResponse rewrites
    the UI's old stack references, ItemInfo refreshes the sparse socket array,
    and CurrencyChanged releases the jeweller's transaction/wait state.
    Keeping all four commands in one 0x85 batch prevents later ordered frames
    from overtaking a fragmented InventoryInfo response.
    """

    writer = BitWriter()
    writer.write_uint8(0x85)

    writer.write_bytes(INVENTORY_INFO_COMMAND.to_bytes(2, "little"))
    write_inventory_info_body(writer, inventory)
    writer.write_uint32_le(int(actor_id))

    writer.write_uint8(0xFF)
    writer.write_bytes(GEM_REMOVAL_RESPONSE_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(int(gem_item_id))
    writer.write_uint32_le(int(replaced_gem_item_id))
    writer.write_uint32_le(int(actor_id))

    writer.write_uint8(0xFF)
    writer.write_bytes(ITEM_INFO_COMMAND.to_bytes(2, "little"))
    _write_item_info_response_body(writer, equipment_item)
    writer.write_uint32_le(int(actor_id))

    writer.write_uint8(0xFF)
    writer.write_bytes(CURRENCY_CHANGED_COMMAND.to_bytes(2, "little"))
    _write_currency_changed_body(writer, currency)
    writer.write_uint32_le(int(actor_id))

    writer.write_uint8(0xFF)
    return writer.finish()
