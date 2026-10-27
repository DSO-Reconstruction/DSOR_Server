"""Typed serializers for the gameplay inventory bootstrap.

The field order comes from the generated client serializers at
``0x1408f9e84`` (``Game::ItemInfo``) and ``0x140941d88``
(``Commands::InventoryInfoCommand``).  Offset-based names are retained where
the gameplay meaning has not yet been proven; this is preferable to assigning
an incorrect semantic name while still making every field editable and
round-trippable.
"""

from __future__ import annotations

from dso_server.protocol.command_ids import command_id
import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from dso_server.protocol.character_selection import BitReader, BitWriter


INVENTORY_INFO_COMMAND = command_id("Commands::InventoryInfoCommand", 0x0054)
BOOTSTRAP_ITEM_ID = 0x00F00001
BOOTSTRAP_ITEM_TEMPLATE = "all_base_trinket_rune_01"
BOOTSTRAP_EQUIPMENT_SLOT = 14
# The client's essence pair.  Slot 35 holds the active essence: its damage
# factor and ammunition type apply to every cast.  Slot 36 parks the essence
# the player switches to; it must stay equipped but must not act.  An earlier
# server build incorrectly used slot 34 for the parked essence, so saved
# allocations there are migrated by ``repair_ammunition_slot_allocations``.
ACTIVE_AMMUNITION_SLOT = 35
SECONDARY_AMMUNITION_SLOT = 36
LEGACY_SECONDARY_AMMUNITION_SLOT = 34
AMMUNITION_SLOTS = (ACTIVE_AMMUNITION_SLOT, SECONDARY_AMMUNITION_SLOT)
BOOTSTRAP_SECOND_RUNE_ID = 0x00F00002
BOOTSTRAP_CONSUMABLE_ID = 0x00F00003
BOOTSTRAP_CONSUMABLE_TEMPLATE = "potion_combined_health_rarity_05"
BOOTSTRAP_TRAVEL_ITEM_ID = 0x00F00004
BOOTSTRAP_TRAVEL_ITEM_TEMPLATE = "misc_travelmap"
BOOTSTRAP_STARTER_RUNE_ID_START = 0x00F00200
BOOTSTRAP_STARTER_JEWEL_ID_START = 0x00F00210
BOOTSTRAP_STARTER_RING_DEATH_ID = 0x00F00220
BOOTSTRAP_STARTER_RING_LIFE_ID = 0x00F00221
BOOTSTRAP_STARTER_MOUNT_ID = 0x00F00222
BOOTSTRAP_STARTER_MORTIS_PET_ID = 0x00F00223
BOOTSTRAP_STARTER_SEASON_PASS_IDS = range(0x00F00224, 0x00F00228)

# UI::TransmogWidget::UpdateEquipmentSlots iterates its eight UI slots and,
# in normal mode, asks Game::Inventory for equipment slot ids 0 through 7.
# It asserts that every result is a valid item.  ``StartEquipment.CharGenItems``
# is only the character-editor wardrobe: in this client database those rows are
# obsolete and, for historical content reasons, are all tagged as dwarf gear.
# They therefore must never be sent as gameplay equipment.  The hand pair comes
# directly from ``StartEquipment.Items``; the remaining slots use the matching
# non-obsolete, class-restricted crafted base items.
_CLASS_CORE_EQUIPMENT: dict[str, tuple[tuple[str, tuple[int, ...]], ...]] = {
    "warrior": (
        ("warrior_base_helmet_crafted", (0,)),
        ("warrior_base_shoulders_crafted", (1,)),
        ("warrior_base_torso_crafted", (2,)),
        ("warrior_base_gloves_crafted", (3,)),
        ("warrior_base_boots_crafted", (4,)),
        ("warrior_base_rh_sword_speedAttack_damage", (5,)),
        (
            "warrior_base_lh_shield_armor_healthpoints_resistanceAll",
            (6,),
        ),
        ("warrior_base_backitem_defense_crafted", (7,)),
        ("all_base_amulet_crafted", (10,)),
        ("all_base_belt_crafted", (11,)),
        ("warrior_base_weaponmod_crafted", (12,)),
    ),
    "mage": (
        ("mage_base_helmet_crafted", (0,)),
        ("mage_base_shoulders_crafted", (1,)),
        ("mage_base_torso_crafted", (2,)),
        ("mage_base_gloves_crafted", (3,)),
        ("mage_base_boots_crafted", (4,)),
        ("mage_base_rh_staff_speedAttack_damage", (5,)),
        ("mage_base_lh_orb_armor_healthpoints_resistanceAll", (6,)),
        ("mage_base_backitem_defense_crafted", (7,)),
        ("all_base_amulet_crafted", (10,)),
        ("all_base_belt_crafted", (11,)),
        ("mage_base_weaponmod_crafted", (12,)),
    ),
    "ranger": (
        ("ranger_base_helmet_crafted", (0,)),
        ("ranger_base_shoulders_crafted", (1,)),
        ("ranger_base_torso_crafted", (2,)),
        ("ranger_base_gloves_crafted", (3,)),
        ("ranger_base_boots_crafted", (4,)),
        ("ranger_base_rh_shortbow_speedAttack_damage", (5,)),
        (
            "ranger_base_lh_defensequiver_armor_healthpoints_resistanceAll",
            (6,),
        ),
        ("ranger_base_backitem_defense_crafted", (7,)),
        ("all_base_amulet_crafted", (10,)),
        ("all_base_belt_crafted", (11,)),
        ("ranger_base_weaponmod_crafted", (12,)),
    ),
    "dwarf": (
        ("dwarf_base_helmet_crafted", (0,)),
        ("dwarf_base_shoulders_crafted", (1,)),
        ("dwarf_base_torso_crafted", (2,)),
        ("dwarf_base_gloves_crafted", (3,)),
        ("dwarf_base_boots_crafted", (4,)),
        ("dwarf_base_rh_pistol_speedAttack_damage", (5,)),
        (
            "dwarf_base_lh_absorber_armor_healthpoints_resistanceAll",
            (6,),
        ),
        ("dwarf_base_backitem_defense_crafted", (7,)),
        ("all_base_amulet_crafted", (10,)),
        ("all_base_belt_crafted", (11,)),
        ("dwarf_base_weaponmod_crafted", (12,)),
    ),
}

_BOOTSTRAP_CORE_ITEM_ID_START = 0x00F00100
_BOOTSTRAP_CORE_ITEM_ID_END = 0x00F00110


@dataclass(frozen=True)
class ItemEnchantmentInfo:
    value: float = 0.0
    kind: int = 0
    field_08: int = 0
    field_0c: int = 0
    template_id: str = ""


@dataclass
class ItemInfo:
    item_id: int
    field_04: int = 0
    amount: int = 1                 # structure offset +0x14
    field_08: int = 0
    socket_count: int = 0           # structure offset +0x10
    field_11: int = 0
    field_12: int = 0
    field_18: int = 8477
    field_20: int = 0              # packed ARGB equipment dye
    field_1c: int = 0              # _Template_ItemRarityColors.ColorId
    field_28: str = "none"         # equipment dye resource ID
    item_level: int = 135           # structure offset +0x0c
    field_30: int = 11
    field_34: int = 0
    field_3c: bool = False          # needs identification (true until identified)
    field_3d: bool = False
    template_id: str = BOOTSTRAP_ITEM_TEMPLATE  # structure offset +0x40
    field_88: str = ""
    field_100: tuple[float, float, float] = (0.0, 0.0, 0.0)
    field_80: str = ""
    field_38: int = 7
    field_f8: str = ""             # equipped transmog skin ID
    field_3e: bool = True
    enchantments: list[ItemEnchantmentInfo] = field(default_factory=list)
    socketed_templates: list[str] = field(default_factory=list)
    created_at: tuple[int, int, int, int, int, int] = (
        2025, 12, 12, 21, 23, 36
    )
    # Server-only provenance, persisted with the item but omitted from the wire.
    quest_id: str = ""


def encode_item_info(writer: BitWriter, item: ItemInfo) -> None:
    """Write one ``Game::ItemInfo`` in the exact generated field order."""

    writer.write_uint32_le(item.item_id)
    writer.write_uint32_le(item.field_04)
    writer.write_uint32_le(item.amount)
    writer.write_uint8(item.field_08)
    writer.write_uint8(item.socket_count)
    writer.write_uint8(item.field_11)
    writer.write_uint8(item.field_12)
    writer.write_uint32_le(item.field_18)
    writer.write_uint32_le(item.field_20)
    writer.write_uint32_le(item.field_1c)
    writer.write_string(item.field_28)
    writer.write_uint32_le(item.item_level)
    writer.write_uint32_le(item.field_30)
    writer.write_uint32_le(item.field_34)
    writer.write_bool(item.field_3c)
    writer.write_bool(item.field_3d)
    writer.write_string(item.template_id)
    writer.write_string(item.field_88)
    for value in item.field_100:
        writer.write_float(value)
    writer.write_string(item.field_80)
    writer.write_uint32_le(item.field_38)
    writer.write_string(item.field_f8)
    writer.write_bool(item.field_3e)

    writer.write_uint8(len(item.enchantments))
    for enchantment in item.enchantments:
        writer.write_float(enchantment.value)
        writer.write_uint8(enchantment.kind)
        writer.write_uint32_le(enchantment.field_08)
        writer.write_uint32_le(enchantment.field_0c)
        writer.write_string(enchantment.template_id)

    writer.write_uint8(len(item.socketed_templates))
    for template_id in item.socketed_templates:
        writer.write_string(template_id)

    if len(item.created_at) != 6:
        raise ValueError("ItemInfo.created_at must contain six uint32 values")
    for value in item.created_at:
        writer.write_uint32_le(value)


def decode_item_info(reader: BitReader) -> ItemInfo:
    """Read one ``Game::ItemInfo`` from the generated bit stream."""

    item_id = reader.read_uint32_le()
    field_04 = reader.read_uint32_le()
    amount = reader.read_uint32_le()
    field_08 = reader.read_uint8()
    socket_count = reader.read_uint8()
    field_11 = reader.read_uint8()
    field_12 = reader.read_uint8()
    field_18 = reader.read_uint32_le()
    field_20 = reader.read_uint32_le()
    field_1c = reader.read_uint32_le()
    field_28 = reader.read_string()
    item_level = reader.read_uint32_le()
    field_30 = reader.read_uint32_le()
    field_34 = reader.read_uint32_le()
    field_3c = reader.read_bool()
    field_3d = reader.read_bool()
    template_id = reader.read_string()
    field_88 = reader.read_string()
    field_100 = tuple(reader.read_float() for _ in range(3))
    field_80 = reader.read_string()
    field_38 = reader.read_uint32_le()
    field_f8 = reader.read_string()
    field_3e = reader.read_bool()

    enchantments = [
        ItemEnchantmentInfo(
            value=reader.read_float(),
            kind=reader.read_uint8(),
            field_08=reader.read_uint32_le(),
            field_0c=reader.read_uint32_le(),
            template_id=reader.read_string(),
        )
        for _ in range(reader.read_uint8())
    ]
    socketed_templates = [
        reader.read_string() for _ in range(reader.read_uint8())
    ]
    created_at = tuple(reader.read_uint32_le() for _ in range(6))

    return ItemInfo(
        item_id=item_id,
        field_04=field_04,
        amount=amount,
        field_08=field_08,
        socket_count=socket_count,
        field_11=field_11,
        field_12=field_12,
        field_18=field_18,
        field_20=field_20,
        field_1c=field_1c,
        field_28=field_28,
        item_level=item_level,
        field_30=field_30,
        field_34=field_34,
        field_3c=field_3c,
        field_3d=field_3d,
        template_id=template_id,
        field_88=field_88,
        field_100=field_100,
        field_80=field_80,
        field_38=field_38,
        field_f8=field_f8,
        field_3e=field_3e,
        enchantments=enchantments,
        socketed_templates=socketed_templates,
        created_at=created_at,
    )


@dataclass
class InventoryInfo:
    items: list[ItemInfo] = field(default_factory=list)          # +0x20
    equipment_slots: dict[int, tuple[int, ...]] = field(
        default_factory=dict
    )                                                           # +0x30
    # First inventory bag: values are zero-based slot indexes (0 = first slot).
    storage_58: dict[int, int] = field(default_factory=dict)
    storage_80: dict[int, int] = field(default_factory=dict)
    storage_a8: dict[int, int] = field(default_factory=dict)
    storage_d0: dict[int, int] = field(default_factory=dict)
    field_f8: list[int] = field(default_factory=list)
    field_118: list[int] = field(default_factory=list)
    changed_item_ids: list[int] = field(default_factory=list)   # +0x138
    field_158: dict[int, int] = field(default_factory=dict)
    field_180: list[int] = field(default_factory=list)
    # Generated helper 0x1409225a4: array<string>.
    field_1a0: list[str] = field(default_factory=list)
    storage_upgrade: int = 30
    bank_storage_upgrade: int = 53
    storage_size: int = 224
    bank_storage_size: int = 270
    extra_bank_storage_size: int = 0
    extra_storage_size: int = 0
    health_points: float = 3459715.25
    secondary_resource: float = 100.0
    field_1e0: bool = False
    actor_id: int = 1
    # Server-only records, deliberately excluded from wire items/allocations.
    premium_inventory_overflow: list[ItemInfo] = field(default_factory=list)
    premium_storage_overflow: list[ItemInfo] = field(default_factory=list)


def clone_item_info(item: ItemInfo) -> ItemInfo:
    """Return an independent ``ItemInfo`` without generic deepcopy traversal.

    ``ItemInfo`` has exactly two mutable members -- ``enchantments`` and
    ``socketed_templates`` -- and both hold immutable elements
    (``ItemEnchantmentInfo`` is a frozen dataclass; the templates are strings).
    Every other field is a scalar, a string, or an immutable tuple, so a shallow
    copy plus two list rebuilds is observationally identical to
    ``copy.deepcopy`` while doing none of its memo/reduce work.
    """

    clone = copy.copy(item)
    clone.enchantments = list(item.enchantments)
    clone.socketed_templates = list(item.socketed_templates)
    return clone


def clone_inventory_info(
    info: InventoryInfo, items: Iterable[ItemInfo] | None = None,
) -> InventoryInfo:
    """Return an independent ``InventoryInfo`` snapshot.

    ``items`` replaces the cloned item list, for the delta/final
    ``InventoryInfo`` views that carry only the changed items (or none):
    copying every owned item just to discard the list cost a vendor purchase
    two full deepcopies.

    ``copy.deepcopy`` of a fully-equipped level-100 inventory measured 6.5 ms
    at p50 (251 items) and was executed while holding the owner's session
    ``flow_lock`` on every account snapshot. The map simulation thread, the
    projectile deadline workers, and the movement/skill ingress workers all
    contend for that same lock, so the copy was direct combat latency for every
    player on the map. This structure-aware clone is 0.44 ms at p50 (~15x) and
    is exhaustively equivalent: every field below is either rebuilt or is an
    immutable scalar/string/tuple. ``test_inventory_codec`` pins that
    equivalence against ``copy.deepcopy`` so a new mutable field cannot silently
    start aliasing.
    """

    clone = copy.copy(info)
    clone.items = [
        clone_item_info(item)
        for item in (info.items if items is None else items)
    ]
    # Values are tuples, so the mapping itself is the only mutable level.
    clone.equipment_slots = dict(info.equipment_slots)
    clone.storage_58 = dict(info.storage_58)
    clone.storage_80 = dict(info.storage_80)
    clone.storage_a8 = dict(info.storage_a8)
    clone.storage_d0 = dict(info.storage_d0)
    clone.field_f8 = list(info.field_f8)
    clone.field_118 = list(info.field_118)
    clone.changed_item_ids = list(info.changed_item_ids)
    clone.field_158 = dict(info.field_158)
    clone.field_180 = list(info.field_180)
    clone.field_1a0 = list(info.field_1a0)
    clone.premium_inventory_overflow = [
        clone_item_info(item) for item in info.premium_inventory_overflow
    ]
    clone.premium_storage_overflow = [
        clone_item_info(item) for item in info.premium_storage_overflow
    ]
    return clone


def owned_inventory_items(info: InventoryInfo):
    """Iterate every owned item, including unavailable premium overflow."""

    yield from info.items
    yield from info.premium_inventory_overflow
    yield from info.premium_storage_overflow


def free_inventory_location(info: InventoryInfo) -> tuple[str, int] | None:
    """Find a normal inventory slot, then a slot in the premium bag."""

    for name, size in (
        ("storage_58", info.storage_size),
        ("storage_80", info.extra_storage_size),
    ):
        used = set(getattr(info, name).values())
        for slot in range(size):
            if slot not in used:
                return name, slot
    return None


def inventory_armament_state(info: InventoryInfo) -> int:
    """Return the GamePlayer armament enum implied by hand allocations.

    ``NewPlayerCommand +0xfc`` is copied directly to
    ``GamePlayer::armamentState``.  It is not the character class.  The client
    uses 3 for a right-hand weapon, 4 for a separate right/off-hand pair, and
    5 for one two-handed item occupying both slots.
    """

    hand_allocations = tuple(
        tuple(int(slot) for slot in slots)
        for slots in info.equipment_slots.values()
        if 5 in slots or 6 in slots
    )
    if any(5 in slots and 6 in slots for slots in hand_allocations):
        return 5
    has_right_hand = any(5 in slots for slots in hand_allocations)
    has_left_hand = any(6 in slots for slots in hand_allocations)
    if has_right_hand and has_left_hand:
        return 4
    if has_right_hand:
        return 3
    return 0


def _write_u32_array(writer: BitWriter, values: Iterable[int]) -> None:
    values = tuple(values)
    writer.write_uint32_le(len(values))
    for value in values:
        writer.write_uint32_le(value)


def _read_u32_array(reader: BitReader) -> list[int]:
    return [reader.read_uint32_le() for _ in range(reader.read_uint32_le())]


def _write_u32_map(writer: BitWriter, values: dict[int, int]) -> None:
    writer.write_uint32_le(len(values))
    for key, value in values.items():
        writer.write_uint32_le(key)
        writer.write_uint32_le(value)


def _read_u32_map(reader: BitReader) -> dict[int, int]:
    return {
        reader.read_uint32_le(): reader.read_uint32_le()
        for _ in range(reader.read_uint32_le())
    }


def encode_inventory_info(info: InventoryInfo) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(INVENTORY_INFO_COMMAND.to_bytes(2, "little"))

    write_inventory_info_body(writer, info)
    writer.write_uint32_le(info.actor_id)
    return writer.finish()


def write_inventory_info_body(writer: BitWriter, info: InventoryInfo) -> None:
    """Write generated InventoryInfo fields, excluding server actor context."""

    writer.write_uint32_le(len(info.items))
    for item in info.items:
        encode_item_info(writer, item)

    writer.write_uint32_le(len(info.equipment_slots))
    for item_id, slots in info.equipment_slots.items():
        writer.write_uint32_le(item_id)
        writer.write_uint32_le(len(slots))
        for slot in slots:
            writer.write_uint8(slot)

    for values in (
        info.storage_58,
        info.storage_80,
        info.storage_a8,
        info.storage_d0,
    ):
        _write_u32_map(writer, values)
    for values in (info.field_f8, info.field_118, info.changed_item_ids):
        _write_u32_array(writer, values)
    _write_u32_map(writer, info.field_158)
    _write_u32_array(writer, info.field_180)
    writer.write_uint32_le(len(info.field_1a0))
    for value in info.field_1a0:
        writer.write_string(value)

    for value in (
        info.storage_upgrade,
        info.bank_storage_upgrade,
        info.storage_size,
        info.bank_storage_size,
        info.extra_bank_storage_size,
        info.extra_storage_size,
    ):
        writer.write_uint32_le(value)
    writer.write_float(info.health_points)
    writer.write_float(info.secondary_resource)
    writer.write_bool(info.field_1e0)


def read_inventory_info_body(reader: BitReader) -> InventoryInfo:
    """Read generated InventoryInfo fields, excluding server actor context."""

    items = [decode_item_info(reader) for _ in range(reader.read_uint32_le())]
    equipment_slots = {}
    for _ in range(reader.read_uint32_le()):
        item_id = reader.read_uint32_le()
        equipment_slots[item_id] = tuple(
            reader.read_uint8() for _ in range(reader.read_uint32_le())
        )
    storage_maps = [_read_u32_map(reader) for _ in range(4)]
    field_f8 = _read_u32_array(reader)
    field_118 = _read_u32_array(reader)
    changed_item_ids = _read_u32_array(reader)
    field_158 = _read_u32_map(reader)
    field_180 = _read_u32_array(reader)
    field_1a0 = [
        reader.read_string() for _ in range(reader.read_uint32_le())
    ]
    capacities = tuple(reader.read_uint32_le() for _ in range(6))
    return InventoryInfo(
        items=items,
        equipment_slots=equipment_slots,
        storage_58=storage_maps[0],
        storage_80=storage_maps[1],
        storage_a8=storage_maps[2],
        storage_d0=storage_maps[3],
        field_f8=field_f8,
        field_118=field_118,
        changed_item_ids=changed_item_ids,
        field_158=field_158,
        field_180=field_180,
        field_1a0=field_1a0,
        storage_upgrade=capacities[0],
        bank_storage_upgrade=capacities[1],
        storage_size=capacities[2],
        bank_storage_size=capacities[3],
        extra_bank_storage_size=capacities[4],
        extra_storage_size=capacities[5],
        health_points=reader.read_float(),
        secondary_resource=reader.read_float(),
        field_1e0=reader.read_bool(),
        actor_id=0,
    )


def decode_inventory_info(payload: bytes, bit_length: int) -> InventoryInfo:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x84:
        raise ValueError("InventoryInfo packet does not begin with 0x84")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != INVENTORY_INFO_COMMAND:
        raise ValueError(f"expected InventoryInfo, got 0x{command_id:04x}")

    result = read_inventory_info_body(reader)
    result.actor_id = reader.read_uint32_le()
    if reader.remaining:
        raise ValueError(f"InventoryInfo has {reader.remaining} unread bits")
    return result


def equip_inventory_item(
    info: InventoryInfo,
    item_id: int,
    equipment_slots: tuple[int, ...],
) -> InventoryInfo:
    """Swap one stored item into authoritative equipment slots.

    The client predicts the visual change before sending InventoryAction
    operation ``0x01`` for an ordinary equip or ``0x0b`` for a move/swap.
    Keeping the same mutation in server state is what makes the loadout
    survive later full InventoryInfo snapshots and map transitions.
    """

    item_id = int(item_id)
    slots = tuple(dict.fromkeys(int(value) for value in equipment_slots))
    if not slots:
        raise ValueError("equipping an item requires at least one slot")
    inventory = copy.deepcopy(info)
    if not any(item.item_id == item_id for item in inventory.items):
        raise ValueError(f"inventory item {item_id} does not exist")

    storage_names = ("storage_58", "storage_80", "storage_a8", "storage_d0")
    source_locations: list[tuple[str, int]] = []
    for name in storage_names:
        storage = getattr(inventory, name)
        if item_id in storage:
            source_locations.append((name, storage.pop(item_id)))
    if item_id not in inventory.equipment_slots and not source_locations:
        raise ValueError(f"inventory item {item_id} is not allocated")

    requested_slots = set(slots)
    displaced = [
        equipped_item_id
        for equipped_item_id, occupied_slots
        in tuple(inventory.equipment_slots.items())
        if (
            equipped_item_id != item_id
            and requested_slots.intersection(occupied_slots)
        )
    ]
    inventory.equipment_slots.pop(item_id, None)
    for equipped_item_id in displaced:
        inventory.equipment_slots.pop(equipped_item_id, None)

    available_locations = list(source_locations)
    for equipped_item_id in displaced:
        if available_locations:
            storage_name, slot = available_locations.pop(0)
        else:
            location = free_inventory_location(inventory)
            if location is None:
                raise ValueError(
                    "ordinary inventory storage is full; cannot unequip "
                    f"item {equipped_item_id}"
                )
            storage_name, slot = location
        getattr(inventory, storage_name)[equipped_item_id] = slot

    inventory.equipment_slots[item_id] = slots
    inventory.changed_item_ids = []
    inventory.field_158 = {}
    inventory.field_1e0 = False
    return inventory


def store_inventory_item(
    info: InventoryInfo,
    item_id: int,
    *,
    storage_name: str = "storage_58",
    storage_slot: int | None = None,
) -> InventoryInfo:
    """Move one equipped item into an authoritative storage allocation.

    Generated ``InventoryCommand`` operations 2 through 5 select ordinary,
    extra, bank, and extra-bank storage respectively.  An omitted slot means
    the first free slot in that storage; an explicit slot must be in range and
    empty.  The client already performed the same move locally.
    """

    item_id = int(item_id)
    capacities = {
        "storage_58": int(info.storage_size),
        "storage_80": int(info.extra_storage_size),
        "storage_a8": int(info.bank_storage_size),
        "storage_d0": int(info.extra_bank_storage_size),
    }
    if storage_name not in capacities:
        raise ValueError(f"unknown inventory storage {storage_name!r}")
    capacity = capacities[storage_name]
    if capacity <= 0:
        raise ValueError(f"inventory storage {storage_name!r} is unavailable")

    inventory = copy.deepcopy(info)
    if not any(item.item_id == item_id for item in inventory.items):
        raise ValueError(f"inventory item {item_id} does not exist")
    if item_id not in inventory.equipment_slots:
        raise ValueError(f"inventory item {item_id} is not equipped")

    for name in capacities:
        getattr(inventory, name).pop(item_id, None)
    target = getattr(inventory, storage_name)
    used_slots = {
        int(slot)
        for allocated_item_id, slot in target.items()
        if int(allocated_item_id) != item_id
    }
    if storage_slot is None:
        storage_slot = next(
            (
                candidate
                for candidate in range(capacity)
                if candidate not in used_slots
            ),
            None,
        )
        if storage_slot is None:
            raise ValueError(f"inventory storage {storage_name!r} is full")
    else:
        storage_slot = int(storage_slot)
        if not 0 <= storage_slot < capacity:
            raise ValueError(
                f"storage slot {storage_slot} is outside "
                f"{storage_name!r} capacity {capacity}"
            )
        if storage_slot in used_slots:
            raise ValueError(
                f"storage slot {storage_slot} in {storage_name!r} is occupied"
            )

    inventory.equipment_slots.pop(item_id)
    target[item_id] = storage_slot
    inventory.changed_item_ids = []
    inventory.field_158 = {}
    inventory.field_1e0 = False
    return inventory


def consume_equipped_stack(
    info: InventoryInfo, item_id: int, amount: int,
) -> tuple[InventoryInfo, InventoryInfo, bool]:
    """Spend ``amount`` from one stack, keeping its item id, without a deep copy.

    Returns ``(inventory, delta, depleted)``. The delta has the shape the
    generic mutation builder publishes: the changed stack in ``items`` while
    some remains, or the removed id in ``changed_item_ids`` once the stack is
    gone, in which case every allocation that referenced it is dropped too.
    Built on ``clone_inventory_info`` because this runs once per accepted cast
    on the skill ingress workers; ``copy.deepcopy`` of a full inventory costs
    milliseconds there.
    """

    item_id = int(item_id)
    amount = int(amount)
    if amount <= 0:
        raise ValueError("consuming a stack requires a positive amount")
    index = next(
        (
            position for position, item in enumerate(info.items)
            if int(item.item_id) == item_id
        ),
        None,
    )
    if index is None:
        raise ValueError(f"inventory item {item_id} does not exist")
    result = clone_inventory_info(info)
    item = result.items[index]
    if amount > int(item.amount):
        raise ValueError(
            f"cannot consume {amount} from item stack {item_id} x{item.amount}"
        )
    depleted = amount == int(item.amount)
    if depleted:
        del result.items[index]
        result.equipment_slots.pop(item_id, None)
        for storage in (
            result.storage_58, result.storage_80,
            result.storage_a8, result.storage_d0,
        ):
            storage.pop(item_id, None)
        result.field_f8 = [value for value in result.field_f8 if value != item_id]
        result.field_118 = [value for value in result.field_118 if value != item_id]
        result.field_180 = [value for value in result.field_180 if value != item_id]
    else:
        item.amount = int(item.amount) - amount
    result.changed_item_ids = []
    result.field_158 = {}
    result.field_1e0 = False
    delta = clone_inventory_info(result)
    delta.items = [] if depleted else [clone_item_info(item)]
    delta.changed_item_ids = [item_id] if depleted else []
    delta.field_158 = {}
    delta.field_1e0 = True
    return result, delta, depleted


def active_equipment_slots(inventory) -> dict[int, tuple[int, ...]]:
    """Return the equipment allocations whose items act on the player.

    Everything equipped acts, except an essence parked in the secondary
    ammunition slot or stranded in the retired slot 34 by an older server.
    The client keeps the secondary essence equipped for the switch key, but
    only the active slot's essence contributes its damage factor and
    ammunition type.  Accepts any object with an ``equipment_slots`` mapping,
    including the light stand-ins and offline tooling pass to the stat
    calculator.
    """

    result: dict[int, tuple[int, ...]] = {}
    for raw_item_id, raw_slots in getattr(
        inventory, "equipment_slots", {}
    ).items():
        slots = tuple(int(slot) for slot in raw_slots)
        if any(
            slot in (SECONDARY_AMMUNITION_SLOT, LEGACY_SECONDARY_AMMUNITION_SLOT)
            for slot in slots
        ):
            continue
        result[int(raw_item_id)] = slots
    return result


def repair_ammunition_slot_allocations(
    info: InventoryInfo,
) -> tuple[InventoryInfo, tuple[int, ...]]:
    """Move essences saved in the retired slot 34 into the essence pair.

    Returns the (possibly unchanged) inventory and the relocated item ids.  A
    legacy item takes the secondary slot 36 when it is free, otherwise the
    first free ordinary storage slot; if storage is full it is left alone
    rather than blocking the map entry.
    """

    legacy_ids = sorted(
        int(item_id)
        for item_id, slots in info.equipment_slots.items()
        if LEGACY_SECONDARY_AMMUNITION_SLOT in tuple(int(s) for s in slots)
    )
    if not legacy_ids:
        return info, ()
    inventory = copy.deepcopy(info)
    moved: list[int] = []
    for item_id in legacy_ids:
        occupied = {
            int(slot)
            for other_id, slots in inventory.equipment_slots.items()
            if int(other_id) != item_id
            for slot in slots
        }
        if SECONDARY_AMMUNITION_SLOT not in occupied:
            inventory.equipment_slots[item_id] = (SECONDARY_AMMUNITION_SLOT,)
            moved.append(item_id)
            continue
        used = set(inventory.storage_58.values())
        free = next(
            (
                candidate for candidate in range(inventory.storage_size)
                if candidate not in used
            ),
            None,
        )
        if free is None:
            continue
        inventory.equipment_slots.pop(item_id, None)
        inventory.storage_58[item_id] = free
        moved.append(item_id)
    return inventory, tuple(moved)


def equipment_allocation_snapshot(info: InventoryInfo) -> InventoryInfo:
    """Build an allocation-only acknowledgement for a predicted equip.

    ``InventoryCommand`` mutates the local client's existing ``GameItem``
    objects before it reaches the server.  Re-sending the complete item array
    after an atomic replacement makes ``ClientInventoryManager`` process those
    objects as newly supplied items a second time.  In particular, replacing
    an occupied cloak/banner can make its equipment rebuild discard the
    adjacent off-hand/accessory allocations.

    No item definition changes during an equip operation.  The authoritative
    acknowledgement therefore carries an empty item array and repeats every
    allocation collection in full.  The equipment map is deliberately *not*
    reduced to the changed slot: the client treats that map as a complete
    allocation, so omitted entries are real unequips.
    """

    result = copy.deepcopy(info)
    result.items = []
    result.changed_item_ids = []
    result.field_158 = {}
    result.field_1e0 = False
    return result


def rekey_inventory_items(
    info: InventoryInfo,
    *,
    first_item_id: int,
) -> tuple[InventoryInfo, dict[int, int], int]:
    """Assign fresh connection-local IDs to a complete inventory snapshot.

    Retail changes every ``GameItem`` ID when the same character is created
    on a destination map server.  Reusing IDs across that handoff makes this
    client retain the old equipped objects and then call
    ``AddItemAtEquipmentSlot`` for them again, which asserts that the slot is
    occupied.  This operation preserves item order and every allocation while
    replacing all item-ID references atomically.

    Returns ``(inventory, old_to_new, next_free_id)``.
    """

    first_item_id = int(first_item_id)
    if first_item_id <= 0:
        raise ValueError("inventory rekey requires a positive first item id")
    old_ids = [int(item.item_id) for item in owned_inventory_items(info)]
    if len(old_ids) != len(set(old_ids)):
        raise ValueError("cannot rekey inventory with duplicate item ids")
    if first_item_id + len(old_ids) > 0x1_0000_0000:
        raise ValueError("inventory rekey would overflow uint32 item ids")

    mapping = {
        old_id: first_item_id + index
        for index, old_id in enumerate(old_ids)
    }
    result = copy.deepcopy(info)
    for item in owned_inventory_items(result):
        item.item_id = mapping[int(item.item_id)]

    def remap_keyed(values: dict[int, object]) -> dict[int, object]:
        return {
            mapping[int(item_id)]: value
            for item_id, value in values.items()
        }

    result.equipment_slots = remap_keyed(result.equipment_slots)
    result.storage_58 = remap_keyed(result.storage_58)
    result.storage_80 = remap_keyed(result.storage_80)
    result.storage_a8 = remap_keyed(result.storage_a8)
    result.storage_d0 = remap_keyed(result.storage_d0)
    result.field_f8 = [
        mapping.get(int(item_id), int(item_id))
        for item_id in result.field_f8
    ]
    result.field_118 = [
        mapping.get(int(item_id), int(item_id))
        for item_id in result.field_118
    ]
    result.field_180 = [
        mapping.get(int(item_id), int(item_id))
        for item_id in result.field_180
    ]
    # A full destination snapshot establishes new identity directly; carrying
    # a prior incremental new->old replacement map across servers is invalid.
    result.changed_item_ids = []
    result.field_158 = {}
    result.field_1e0 = False
    return result, mapping, first_item_id + len(old_ids)


def encode_bootstrap_inventory_info(
    actor_id: int = 1,
    *,
    class_name: str = "dwarf",
    item_level: int = 1,
) -> tuple[bytes, int]:
    """Create a typed inventory satisfying the client's equipment UI contract.

    ``TransmogWidget::UpdateEquipmentSlots`` indexes equipment slots 0..7 and
    requires each to contain a valid item.  Use the class' enchantment-free
    valid class-specific starter templates for those slots, followed by the three
    independently reconstructed trinket, consumable and travel allocations.
    """

    try:
        core_templates = _CLASS_CORE_EQUIPMENT[class_name]
    except KeyError as exc:
        raise ValueError(
            f"unsupported bootstrap inventory class {class_name!r}"
        ) from exc

    now = datetime.now()
    created_at = (
        now.year,
        now.month,
        now.day,
        now.hour,
        now.minute,
        now.second,
    )
    core_items: list[ItemInfo] = []
    core_allocations: dict[int, tuple[int, ...]] = {}
    for index, (template_id, slots) in enumerate(core_templates):
        item_id = 0x00F00100 + index
        core_items.append(
            ItemInfo(
                item_id=item_id,
                template_id=template_id,
                # All gameplay starter rows are Common.  CharCreation rows are
                # intentionally excluded from this packet.
                field_08=0,
                socket_count=0,
                socketed_templates=[],
                item_level=max(1, int(item_level)),
                field_18=0x1000 + index,
                field_30=1,
                field_38=0,
                enchantments=[],
                created_at=created_at,
            )
        )
        core_allocations[item_id] = slots

    starter_trinkets: list[ItemInfo] = []
    trinket_allocations: dict[int, tuple[int, ...]] = {}
    for index in range(5):
        item_id = BOOTSTRAP_STARTER_RUNE_ID_START + index
        starter_trinkets.append(
            ItemInfo(
                item_id=item_id,
                template_id="all_base_trinket_rune_01",
                socket_count=5,
                socketed_templates=["", "", "", "", ""],
                item_level=1,
                field_08=0,
                field_18=0x2100 + index,
                field_30=1,
                field_38=0,
                created_at=created_at,
            )
        )
        trinket_allocations[item_id] = (13 + index,)
    for index in range(3):
        item_id = BOOTSTRAP_STARTER_JEWEL_ID_START + index
        starter_trinkets.append(
            ItemInfo(
                item_id=item_id,
                template_id="all_base_trinket_jewel_01",
                socket_count=5,
                socketed_templates=["", "", "", "", ""],
                item_level=1,
                field_08=0,
                field_18=0x2110 + index,
                field_30=1,
                field_38=0,
                created_at=created_at,
            )
        )
        trinket_allocations[item_id] = (20 + index,)
    ring_death = ItemInfo(
        item_id=BOOTSTRAP_STARTER_RING_DEATH_ID,
        template_id="all_mythic_ring_death",
        field_08=6,
        socket_count=0,
        socketed_templates=[],
        item_level=1,
        field_18=0x2120,
        field_30=1,
        field_38=0,
        created_at=created_at,
    )
    ring_life = ItemInfo(
        item_id=BOOTSTRAP_STARTER_RING_LIFE_ID,
        template_id="all_mythic_ring_life",
        field_08=6,
        socket_count=0,
        socketed_templates=[],
        item_level=1,
        field_18=0x2121,
        field_30=1,
        field_38=0,
        created_at=created_at,
    )
    manticore = ItemInfo(
        item_id=BOOTSTRAP_STARTER_MOUNT_ID,
        template_id="mythical_mount_manticore_01",
        field_08=7,
        socket_count=0,
        socketed_templates=[],
        item_level=1,
        field_18=0x2122,
        field_30=1,
        field_38=0,
        created_at=created_at,
    )
    mortis_pet = ItemInfo(
        item_id=BOOTSTRAP_STARTER_MORTIS_PET_ID,
        template_id="pet_mortis_phase2_epic",
        field_08=4,
        socket_count=0,
        socketed_templates=[],
        item_level=1,
        field_18=0x2123,
        field_30=1,
        field_38=0,
        created_at=created_at,
    )
    health_potion = ItemInfo(
        item_id=BOOTSTRAP_CONSUMABLE_ID,
        template_id=BOOTSTRAP_CONSUMABLE_TEMPLATE,
        amount=1,
        field_08=4,
        socket_count=0,
        socketed_templates=[],
        item_level=1,
        field_18=22721,
        field_30=151600,
        field_38=0,
        created_at=created_at,
    )
    travel_map = ItemInfo(
        item_id=BOOTSTRAP_TRAVEL_ITEM_ID,
        template_id=BOOTSTRAP_TRAVEL_ITEM_TEMPLATE,
        amount=1,
        socket_count=0,
        socketed_templates=[],
        item_level=10,
        field_18=18045,
        field_30=22498,
        field_38=0,
        created_at=created_at,
    )
    season_pass_tickets = [
        ItemInfo(
            item_id=item_id,
            template_id=template_id,
            field_08=rarity,
            socket_count=0,
            socketed_templates=[],
            item_level=1,
            field_18=0x2124 + index,
            field_30=1,
            field_38=0,
            created_at=created_at,
        )
        for index, (item_id, template_id, rarity) in enumerate(zip(
            BOOTSTRAP_STARTER_SEASON_PASS_IDS,
            ("gold_pass", "gold_pass_2", "red_pass", "red_pass_2"),
            (5, 5, 7, 7),
        ))
    ]
    equipment_slots = {**core_allocations, **trinket_allocations}
    equipment_slots[ring_death.item_id] = (8,)
    equipment_slots[ring_life.item_id] = (9,)
    # Five rune and three jewel trinkets are available initially. Divination
    # Stones earn the two additional rune slots; each base item has five sockets.
    return encode_inventory_info(
        InventoryInfo(
            items=core_items + starter_trinkets + [
                ring_death,
                ring_life,
                manticore,
                mortis_pet,
                health_potion,
                travel_map,
            ] + season_pass_tickets,
            equipment_slots=equipment_slots,
            # Retail places TravelMapItem in ordinary inventory storage. It
            # must never enter equipment_slots: AddItemAtEquipmentSlot calls
            # CanBeEquipped(), which correctly rejects misc_travelmap.
            storage_58={
                travel_map.item_id: 0,
                health_potion.item_id: 1,
                manticore.item_id: 2,
                mortis_pet.item_id: 3,
                **{
                    item.item_id: 4 + index
                    for index, item in enumerate(season_pass_tickets)
                },
            },
            actor_id=actor_id,
        )
    )


def repair_bootstrap_class_equipment(
    inventory: InventoryInfo,
    *,
    class_name: str,
    item_level: int,
    overflowed: list[int] | None = None,
) -> InventoryInfo:
    """Replace only old server-generated core gear with the current loadout.

    Early localhost saves used the visual ``CharCreation_*`` hand items from
    ``_Template_StartEquipment.CharGenItems``.  Those are character-editor
    previews, not gameplay starter weapons; two-handed ranger/warrior previews
    can leave the client in armament state 3. Rebuild stale bootstrap gear
    from ``Items``-compatible templates while preserving every acquired item
    and its storage/equipment allocation. Bootstrap items are recognized by
    template after the destination server has assigned fresh item IDs.

    A starter item displaced from its equipment slot moves to the first free
    ordinary inventory slot. With the inventory full it waits in
    ``premium_inventory_overflow`` instead, which returns it to the first slot
    that frees up (gameplay/premium_bags.py); its ID is appended to
    ``overflowed`` when a list is passed.
    """

    def _overflow(target: InventoryInfo, item_id: int) -> None:
        for item in target.items:
            if int(item.item_id) == item_id:
                target.items.remove(item)
                target.premium_inventory_overflow.append(item)
                break
        if overflowed is not None:
            overflowed.append(item_id)

    generated_packet, generated_bits = encode_bootstrap_inventory_info(
        actor_id=inventory.actor_id,
        class_name=class_name,
        item_level=item_level,
    )
    generated = decode_inventory_info(generated_packet, generated_bits)
    generated_core = [
        copy.deepcopy(item)
        for item in generated.items
        if _BOOTSTRAP_CORE_ITEM_ID_START
        <= int(item.item_id)
        < _BOOTSTRAP_CORE_ITEM_ID_END
    ]

    # Migrate the old development-only auxiliary allocations before the core
    # early-return path.  Match the known bootstrap IDs so player-acquired
    # runes/potions are never unequipped.
    inventory = copy.deepcopy(inventory)
    auxiliary_ids = {
        int(item.item_id)
        for item in inventory.items
        if int(item.item_id) in {
            BOOTSTRAP_ITEM_ID,
            BOOTSTRAP_SECOND_RUNE_ID,
            BOOTSTRAP_CONSUMABLE_ID,
        }
    }
    used_storage_slots = set(inventory.storage_58.values())
    for item_id in sorted(auxiliary_ids):
        if item_id not in inventory.equipment_slots:
            continue
        inventory.equipment_slots.pop(item_id, None)
        slot = next(
            (
                candidate
                for candidate in range(inventory.storage_size)
                if candidate not in used_storage_slots
            ),
            None,
        )
        if slot is None:
            _overflow(inventory, item_id)
            continue
        inventory.storage_58[item_id] = slot
        used_storage_slots.add(slot)

    expected_template_ids = {
        item.template_id for item in generated_core
    }
    reserved_core_ids = {
        int(item.item_id)
        for item in inventory.items
        if _BOOTSTRAP_CORE_ITEM_ID_START
        <= int(item.item_id)
        < _BOOTSTRAP_CORE_ITEM_ID_END
    }
    selected_core_ids: set[int] = set()
    for template_id in expected_template_ids:
        candidates = [
            item
            for item in inventory.items
            if (
                item.template_id == template_id
                and int(item.item_id) not in selected_core_ids
            )
        ]
        if not candidates:
            continue
        selected = min(
            candidates,
            key=lambda item: (
                int(item.item_id) not in reserved_core_ids,
                inventory.items.index(item),
            ),
        )
        selected_core_ids.add(int(selected.item_id))
    existing_core_ids = reserved_core_ids | selected_core_ids
    templates_are_current = (
        len(selected_core_ids) == len(expected_template_ids)
        and all(
            item.template_id in expected_template_ids
            for item in inventory.items
            if int(item.item_id) in reserved_core_ids
        )
    )
    acquired_slots = {
        int(slot)
        for item_id, slots in inventory.equipment_slots.items()
        if int(item_id) not in existing_core_ids
        for slot in slots
    }
    conflicting_core_ids = {
        int(item_id)
        for item_id, slots in inventory.equipment_slots.items()
        if (
            int(item_id) in existing_core_ids
            and acquired_slots.intersection(int(slot) for slot in slots)
        )
    }
    if (
        templates_are_current
        and not conflicting_core_ids
    ):
        # A correctly generated starter item may have been deliberately
        # unequipped and moved to storage. Its allocation is player state,
        # not evidence that the class bootstrap needs rebuilding.
        return inventory

    result = copy.deepcopy(inventory)
    if not templates_are_current:
        # Starter items whose template is still in the loadout stay as they
        # are: level, sockets, gems and wherever the player put them. Only
        # stale templates are replaced, and loadout pieces the save lacks
        # (e.g. the amulet, belt and weapon mod a newer loadout adds) added.
        kept_templates = {
            item.template_id
            for item in result.items
            if int(item.item_id) in selected_core_ids
        }
        added_core = [
            item for item in generated_core
            if item.template_id not in kept_templates
        ]
        removed_ids = existing_core_ids - selected_core_ids
        held_ids = {
            int(item.item_id)
            for item in (*result.items, *result.premium_inventory_overflow)
        } - removed_ids
        if any(int(item.item_id) in held_ids for item in added_core):
            # A kept item already holds a generated ID: rebuild the set.
            added_core = list(generated_core)
            removed_ids = set(existing_core_ids)
        added_ids = {int(item.item_id) for item in added_core}
        result.items = [
            item
            for item in result.items
            if int(item.item_id) not in removed_ids
        ]
        result.items.extend(added_core)
        for item_id in removed_ids | added_ids:
            result.equipment_slots.pop(item_id, None)
            for storage_name in (
                "storage_58",
                "storage_80",
                "storage_a8",
                "storage_d0",
            ):
                getattr(result, storage_name).pop(item_id, None)
        # A kept starter that shares a slot with an acquired item yields it
        # and moves to storage below; removed ones need no storage.
        conflicting_core_ids -= removed_ids
        for item_id in conflicting_core_ids:
            result.equipment_slots.pop(item_id, None)

        acquired_slots = {
            int(slot)
            for item_id, slots in result.equipment_slots.items()
            if int(item_id) not in added_ids
            for slot in slots
        }
        for item_id, slots in generated.equipment_slots.items():
            if item_id not in added_ids:
                continue
            slots = tuple(int(slot) for slot in slots)
            if not acquired_slots.intersection(slots):
                result.equipment_slots[item_id] = slots
                acquired_slots.update(slots)
            else:
                conflicting_core_ids.add(item_id)
    else:
        # Repair account snapshots produced by the previous migration: if a
        # starter and an acquired item both claim a slot, retain the acquired
        # item and return the starter to ordinary storage.
        for item_id in conflicting_core_ids:
            result.equipment_slots.pop(item_id, None)

    allocated_ids = set(result.equipment_slots)
    for storage_name in (
        "storage_58",
        "storage_80",
        "storage_a8",
        "storage_d0",
    ):
        allocated_ids.update(getattr(result, storage_name))
    free_slots = (
        slot
        for slot in range(max(0, int(result.storage_size)))
        if slot not in set(result.storage_58.values())
    )
    for item_id in sorted(conflicting_core_ids):
        if item_id in allocated_ids:
            continue
        storage_slot = next(free_slots, None)
        if storage_slot is None:
            _overflow(result, item_id)
            continue
        result.storage_58[item_id] = storage_slot
        allocated_ids.add(item_id)

    result.changed_item_ids = []
    result.field_158 = {}
    result.field_1e0 = False
    return result
