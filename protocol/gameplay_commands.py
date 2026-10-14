"""Bit-exact serializers for the first local-player gameplay commands.

The command layouts in this module follow the generated client serializers in
``dro_client64.exe``.  Application packet 0x84 supplies the command id, then
the concrete command payload, followed by the common actor id as additional
server data.
"""

from __future__ import annotations

from dso_server.protocol.command_ids import command_id
from collections.abc import Mapping
from typing import Any

from dso_server.protocol.character_selection import BitReader, BitWriter
from dso_server.protocol.currency_protocol import (
    DEFAULT_BADGE_OF_HONOR_BALANCE,
    DEFAULT_DRAKEN_BALANCE,
    DEFAULT_GAME_CURRENCY_BALANCES,
    DEFAULT_GLYPH_OF_POWER_BALANCE,
    normalize_game_currency_balances,
)


NEW_PLAYER_COMMAND = command_id("Commands::NewPlayerCommand", 0x001D)
PLAYER_READY_COMMAND = command_id("Commands::PlayerReadyCommand", 0x001F)
QUICK_SLOTS_COMMAND = command_id("Commands::QuickSlotsInfoCommand", 0x0050)
QUICKBAR_INFO_COMMAND = command_id("Commands::QuickbarInfoCommand", 0x0052)
INVENTORY_INFO_COMMAND = command_id("Commands::InventoryInfoCommand", 0x0054)
LOCAL_PLAYER_ACTOR_ID = 1
# ActorPlacement's first value is the actor's faction instance. Retail assigns
# ordinary NPCs 0x10000, hostile map monsters 0x10001, and the local player a
# distinct non-zero player faction (0x10002 in the matching b1201 capture).
# Leaving this substructure at its constructor defaults makes the client treat
# hostile monsters as neutral actors, which produces the white NPC outline.
LOCAL_PLAYER_FACTION_ID = 0x00010002
LOCAL_GOLD_BALANCE = 999_999_999
LOCAL_PREMIUM_CURRENCY_BALANCE = 9_999_999

# The selected local-player bootstrap was reconstructed from an established
# character whose inventory has expanded ordinary/bank storage.  NewPlayer and
# InventoryInfo must publish the same six values: the inventory UI allocates
# its FixedArray<UI::Slot> from them and later indexes the expanded layout when
# the window opens.
LOCAL_INVENTORY_CAPACITIES = (30, 53, 224, 270, 0, 0)

CAPTURED_INVENTORY_HEALTH_POINTS = 3459715.25
CAPTURED_INVENTORY_SECONDARY_RESOURCE = 100.0

# First ordinary Entrypoint in export_win32/maps/a0200_kingscity.db4.
KINGS_CITY_SPAWN = (-0.14980199933052063, 1.0, 4.748802185058594)


def decode_command_actor(
    payload: bytes,
    bit_length: int,
    *,
    expected_command: int,
    body_decoder,
) -> int:
    """Decode a reconstructed command and return its trailing actor ID."""
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x84:
        raise ValueError("not a single-command game packet")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != expected_command:
        raise ValueError(
            f"expected command 0x{expected_command:04x}, got 0x{command_id:04x}"
        )
    body_decoder(reader)
    actor_id = reader.read_uint32_le()
    if reader.remaining:
        raise ValueError(f"command has {reader.remaining} unread bits")
    return actor_id


def decode_inventory_info_actor(payload: bytes, bit_length: int) -> int:
    def body(reader: BitReader) -> None:
        for _ in range(12):
            if reader.read_uint32_le() != 0:
                raise ValueError("barebones InventoryInfo contains a non-empty array")
        for _ in range(6):
            reader.read_uint32_le()
        reader.read_float()
        reader.read_float()
        reader.read_bool()

    return decode_command_actor(
        payload,
        bit_length,
        expected_command=INVENTORY_INFO_COMMAND,
        body_decoder=body,
    )


def decode_quick_slots_actor(payload: bytes, bit_length: int) -> int:
    def body(reader: BitReader) -> None:
        quickbars = reader.read_uint32_le()
        for _ in range(quickbars):
            count = reader.read_uint32_le()
            if count != 17:
                raise ValueError(f"invalid quick-slot count {count}")
            for _ in range(count):
                reader.read_uint32_le()

    return decode_command_actor(
        payload,
        bit_length,
        expected_command=QUICK_SLOTS_COMMAND,
        body_decoder=body,
    )


def decode_quickbar_info_actor(payload: bytes, bit_length: int) -> int:
    def body(reader: BitReader) -> None:
        reader.read_uint32_le()
        reader.read_uint32_le()

    return decode_command_actor(
        payload,
        bit_length,
        expected_command=QUICKBAR_INFO_COMMAND,
        body_decoder=body,
    )


def decode_player_ready_actor(payload: bytes, bit_length: int) -> int:
    return decode_command_actor(
        payload,
        bit_length,
        expected_command=PLAYER_READY_COMMAND,
        body_decoder=lambda reader: None,
    )


def _write_command_header(
    writer: BitWriter, command_id: int
) -> None:
    writer.write_uint8(0x84)
    writer.write_bytes(int(command_id).to_bytes(2, "little"))


def _write_command_actor(writer: BitWriter, actor_id: int) -> None:
    """Append DrasaClientHandler's per-command additional server data.

    The actor id is not part of the generated concrete command serializer.
    DrasaClientHandler reads it only after that serializer succeeds, so it must
    trail the command body even when the body ends on a non-byte boundary.
    """

    writer.write_uint32_le(actor_id)


def _write_zero_block(writer: BitWriter, count: int) -> None:
    for _ in range(count):
        writer.write_uint32_le(0)


def encode_new_player(
    character: Any,
    template_row_index: int,
    *,
    actor_id: int = LOCAL_PLAYER_ACTOR_ID,
    position: tuple[float, float, float] = KINGS_CITY_SPAWN,
    current_xp: int | None = None,
    level_base_xp: int = 0,
    next_level_base_xp: int = 1,
    travel_map_unlocks: tuple[int, int, int, int] = (0, 0, 0, 0),
    gold: int = LOCAL_GOLD_BALANCE,
    premium_currency: int = LOCAL_PREMIUM_CURRENCY_BALANCE,
    badge_of_honor: int = DEFAULT_BADGE_OF_HONOR_BALANCE,
    legacy_progression_currency: int = 0,
    draken: int = DEFAULT_DRAKEN_BALANCE,
    glyph_of_power: int = DEFAULT_GLYPH_OF_POWER_BALANCE,
    game_currencies: Mapping[str, int] | None = None,
    knowledge_xp: int = 0,
    fame_xp: int = 0,
    armament_state: int = 0,
) -> tuple[bytes, int]:
    """Serialize ``NewPlayerCommand`` (0x001d) for the selected character.

    Unknown state uses the command constructor's own defaults.  Appearance,
    class, level, name, spawn position and the database-owned player template
    row are populated because the client consumes those while constructing its
    local actor.
    """

    writer = BitWriter()
    _write_command_header(writer, NEW_PLAYER_COMMAND)

    # +0x20..+0x25: six flags, followed by a 32-bit enum at +0x28. The handler
    # copies +0x21 to GamePlayer +0x689. PlayerInputProperty checks that byte
    # before enabling administrator-only input and hub overrides. It copies
    # +0x22 to +0x691 separately; overhead UI tests that byte and uses the
    # client's OverheadAdminColor. Neither flag is an RGBA value.
    initial_flags = [False] * 6
    initial_flags[1] = bool(getattr(character, "admin_privileges", False))
    initial_flags[2] = bool(getattr(character, "admin_name_color", False))
    for flag in initial_flags:
        writer.write_bool(flag)
    writer.write_uint32_le(0)

    # +0x2c and +0x4c: two generated eight-float value blocks.
    _write_zero_block(writer, 8)
    _write_zero_block(writer, 8)

    # +0x6c/+0x6d flags and +0x70 byte enum.
    writer.write_bool(False)
    writer.write_bool(False)
    writer.write_uint8(0)

    # +0x80 position (x/y/z only), +0x90 movement/tick value and player name.
    for component in position:
        writer.write_float(component)
    writer.write_uint32_le(0)
    writer.write_string(str(getattr(character, "name", "Player")))

    # +0xd8 is the player class.  The NewPlayer handler copies it to
    # GamePlayer::classId (+0x3fc), which SetupPlayerAttributes immediately
    # uses to select a class-specific level-scaling table.  +0xd9..+0xdd are
    # gender and the four character-appearance selectors.
    writer.write_uint8(int(getattr(character, "class_id", 0)))
    writer.write_uint8(int(getattr(character, "gender", 0)))
    writer.write_uint8(int(getattr(character, "hair_style", 0)))
    writer.write_uint8(int(getattr(character, "beard_style", 0)))
    writer.write_uint8(int(getattr(character, "body_style", 0)))
    writer.write_uint8(int(getattr(character, "body_stature", 0)))

    writer.write_uint32_le(template_row_index)  # +0xe0
    writer.write_string("")                    # +0xe8
    writer.write_uint64_le(0)                   # +0xf0
    writer.write_uint32_le(int(getattr(character, "level", 1)))  # +0xf8
    # +0xfc is copied verbatim to GamePlayer::armamentState (+0x3f8).
    # Sending the class id here put Dwarfs in one-hand state 3 even when the
    # InventoryInfo that followed contained a two-handed weapon.
    if int(armament_state) not in {0, 3, 4, 5}:
        raise ValueError(f"invalid player armament state {armament_state}")
    writer.write_uint8(int(armament_state))                       # +0xfc

    # +0x100: ActorPlacement / faction identity. NewPlayer's handler copies
    # these five fields directly to GamePlayer +0xb0. The live b1201 player is
    # faction 0x10002, actor type 0, and marked as a player-owned faction
    # member. Monsters use faction 0x10001 and actor type 2, allowing the
    # client to derive the hostile red outline and attackability relationship.
    writer.write_uint32_le(LOCAL_PLAYER_FACTION_ID)
    writer.write_uint32_le(actor_id)
    writer.write_uint8(0)
    writer.write_bool(True)
    writer.write_bool(False)

    # +0x114 is the character level.  It is copied to GamePlayer +0x68 and
    # used together with +0xd8 by SetupPlayerAttributes.  Level zero has no
    # scaling-table row and makes this client build assert in
    # ClientGameWorld::SetupPlayerAttributes.
    player_level = max(1, int(getattr(character, "level", 1)))
    if current_xp is None:
        current_xp = int(getattr(character, "progression_u32", level_base_xp))
    current_xp = int(current_xp)
    level_base_xp = int(level_base_xp)
    next_level_base_xp = int(next_level_base_xp)
    if next_level_base_xp <= level_base_xp:
        raise ValueError(
            "next_level_base_xp must be greater than level_base_xp"
        )
    if not level_base_xp <= current_xp <= next_level_base_xp:
        raise ValueError(
            f"current_xp {current_xp} is outside level XP bounds "
            f"{level_base_xp}..{next_level_base_xp}"
        )

    writer.write_uint32_le(0)             # +0x110
    writer.write_uint32_le(player_level)  # +0x114
    # The local NewPlayer handler publishes these as the initial normal-XP
    # event. CharacterSheetWidget computes its bar maximum as +0x120 - +0x11c
    # and asserts when both constructor-default values are zero.
    writer.write_uint32_le(current_xp)             # +0x118, current XP
    writer.write_uint32_le(level_base_xp)          # +0x11c
    writer.write_uint32_le(next_level_base_xp)     # +0x120
    writer.write_uint32_le(0)                      # +0x124
    writer.write_uint32_le(max(0, int(fame_xp)))  # +0x128
    writer.write_uint32_le(0)                      # +0x12c

    # +0x130 is a fixed 128-bit travel-map access bitset. Each set bit is a
    # database MapLookupIndex. +0x150 is the separate dynamic exit-state list.
    if len(travel_map_unlocks) != 4:
        raise ValueError("travel_map_unlocks requires four uint32 words")
    writer.write_uint32_le(len(travel_map_unlocks))
    for word in travel_map_unlocks:
        writer.write_uint32_le(word)
    writer.write_uint32_le(0)

    # Inventory::Setup consumes these before InventoryInfo is handled. Keep
    # them consistent with the default_player row in static.db4 and with the
    # capacities sent by encode_inventory_info().
    for value in LOCAL_INVENTORY_CAPACITIES:
        writer.write_uint32_le(value)  # +0x170..+0x184
    # +0x188 is the premium-currency balance. The live soul-stone purchase
    # decrements this exact field through CurrencyChanged (0x0080).
    writer.write_uint32_le(max(0, int(premium_currency)))  # +0x188
    # +0x190 is the uint64 virtual-currency balance used by PriceVC travel
    # costs (gold). Keep the later CurrencyChanged snapshot consistent.
    writer.write_uint64_le(max(0, int(gold)))  # +0x190
    # +0x198..+0x1a8 are fixed legacy balances.  The 2026-09-09 retail capture
    # verifies their semantic order as Badge of Honor, a non-wallet legacy
    # progression field, Draken, then Glyphs of Power.
    writer.write_uint32_le(max(0, int(badge_of_honor)))
    writer.write_uint32_le(max(0, int(legacy_progression_currency)))
    writer.write_uint32_le(max(0, int(draken)))
    writer.write_uint64_le(max(0, int(glyph_of_power)))

    # +0x1b0 is the extensible game-currency wallet.  Its resource IDs are
    # deliberately serialized in retail order for deterministic packets.
    resolved_game_currencies = normalize_game_currency_balances(
        (
            DEFAULT_GAME_CURRENCY_BALANCES
            if game_currencies is None
            else game_currencies
        )
    )
    writer.write_uint32_le(len(resolved_game_currencies))
    for resource_id, balance in resolved_game_currencies.items():
        writer.write_string(resource_id)
        writer.write_uint64_le(balance)
    writer.write_uint32_le(0)     # +0x1d0
    writer.write_uint32_le(0)     # +0x1d4
    _write_zero_block(writer, 8)  # +0x1d8
    writer.write_uint32_le(0)     # +0x1f8
    writer.write_string("")      # +0x200
    writer.write_uint32_le(0)     # +0x208
    writer.write_uint32_le(0)     # +0x20c

    # +0x210 generated player-state structure: 22 scalar fields, one empty
    # dynamic array, four fixed scalar entries, then 2 * 7 scalar entries.
    player_state_scalars = [0] * 22
    # +0x254 is the Knowledge/Wisdom progression value mirrored by retail in
    # TalentCategoryPointsInfo category 1.
    player_state_scalars[17] = max(0, int(knowledge_xp))
    for value in player_state_scalars:
        writer.write_uint32_le(value)
    writer.write_uint32_le(0)
    _write_zero_block(writer, 4)
    _write_zero_block(writer, 14)

    writer.write_uint32_le(0)          # empty array at +0x368
    writer.write_uint32_le(0xFFFFFFFF) # +0x388 constructor default
    _write_zero_block(writer, 8)       # +0x38c
    writer.write_uint8(0)              # +0x3ac enum
    writer.write_string("")           # +0x3b0
    _write_zero_block(writer, 8)       # +0x3b8
    _write_command_actor(writer, actor_id)
    return writer.finish()


def decode_new_player_actor_id(payload: bytes, bit_length: int | None = None) -> int:
    """Return the actor id trailing a serialized ``NewPlayerCommand`` body.

    Walks the same field order as :func:`encode_new_player` over a captured
    payload (0x84 single command or the first sub-command of an 0x85 batch)
    and reads DrasaClientHandler's additional server data that follows the
    command body.  The replayed live batch creates the local player under the
    live server's actor id, and every follow-up command addressed to the local
    player must carry that same id or the client rejects it.
    """

    reader = BitReader(payload, bit_length)
    message_id = reader.read_uint8()
    if message_id not in (0x84, 0x85):
        raise ValueError(f"not an application command payload: 0x{message_id:02x}")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != NEW_PLAYER_COMMAND:
        raise ValueError(f"not a NewPlayerCommand payload: 0x{command_id:04x}")

    def skip_uint32(count: int) -> None:
        for _ in range(count):
            reader.read_uint32_le()

    for _ in range(6):                    # +0x20..+0x25 flags
        reader.read_bool()
    skip_uint32(1)                        # +0x28 enum
    skip_uint32(16)                       # +0x2c and +0x4c value blocks
    reader.read_bool()                    # +0x6c
    reader.read_bool()                    # +0x6d
    reader.read_uint8()                   # +0x70
    for _ in range(3):                    # +0x80 position
        reader.read_float()
    skip_uint32(1)                        # +0x90
    reader.read_string()                  # +0x98 name
    for _ in range(6):                    # +0xd8 class, +0xd9..+0xdd appearance
        reader.read_uint8()
    skip_uint32(1)                        # +0xe0 template row
    reader.read_string()                  # +0xe8
    reader.read_uint64_le()               # +0xf0
    skip_uint32(1)                        # +0xf8 level
    reader.read_uint8()                   # +0xfc
    skip_uint32(2)                        # +0x100 substruct
    reader.read_uint8()
    reader.read_bool()
    reader.read_bool()
    skip_uint32(8)                        # +0x110..+0x12c incl. XP triplet
    skip_uint32(reader.read_uint32_le())  # +0x130 array<u32>
    for _ in range(reader.read_uint32_le()):
        reader.read_string()              # +0x150 array<string>
    skip_uint32(7)                        # +0x170..+0x188
    reader.read_uint64_le()               # +0x190
    skip_uint32(3)                        # +0x198..+0x1a0
    reader.read_uint64_le()               # +0x1a8
    for _ in range(reader.read_uint32_le()):
        reader.read_string()              # +0x1b0 array<{string,u64}>
        reader.read_uint64_le()
    skip_uint32(2)                        # +0x1d0, +0x1d4
    skip_uint32(8)                        # +0x1d8
    skip_uint32(1)                        # +0x1f8
    reader.read_string()                  # +0x200
    skip_uint32(2)                        # +0x208, +0x20c

    # +0x210 player state: 22 scalars, the item-record array (element = two
    # strings + seven u32, confirmed against the live capture's five PvP
    # records), then 4 + 14 scalar entries.
    skip_uint32(22)
    for _ in range(reader.read_uint32_le()):
        reader.read_string()
        reader.read_string()
        skip_uint32(7)
    skip_uint32(4)
    skip_uint32(14)

    bulk_count = reader.read_uint32_le()  # +0x368 array
    if bulk_count:
        raise ValueError(
            f"+0x368 array has {bulk_count} elements; its element layout is "
            "unknown, so the trailing actor id cannot be located"
        )
    skip_uint32(1)                        # +0x388 constructor default
    skip_uint32(8)                        # +0x38c
    reader.read_uint8()                   # +0x3ac enum
    reader.read_string()                  # +0x3b0
    skip_uint32(8)                        # +0x3b8
    return reader.read_uint32_le()


def encode_quick_slots(
    quickbar_amount: int = 1,
    *,
    slots_per_quickbar: int = 17,
    actor_id: int = LOCAL_PLAYER_ACTOR_ID,
) -> tuple[bytes, int]:
    """Serialize empty slot contents for every advertised quickbar.

    ``QuickSlotsInfoCommand`` contains an outer array (one entry per
    quickbar), whose entries are fixed-size arrays of 17 quick-slot values.
    The client rejects any inner array whose count is not 17.  An empty slot
    is represented by the signed enum sentinel ``-1`` and has no trailing
    string payload.

    Keeping this outer count equal to ``QuickbarInfoCommand.quickbar_amount``
    is important: UI refreshes index the quickbar slot set after receiving
    both commands.
    """

    quickbar_amount = int(quickbar_amount)
    slots_per_quickbar = int(slots_per_quickbar)
    if quickbar_amount < 0:
        raise ValueError("quickbar_amount cannot be negative")
    if slots_per_quickbar != 17:
        raise ValueError("the client protocol requires 17 slots per quickbar")

    writer = BitWriter()
    _write_command_header(writer, QUICK_SLOTS_COMMAND)
    writer.write_uint32_le(quickbar_amount)
    for _ in range(quickbar_amount):
        writer.write_uint32_le(slots_per_quickbar)
        for _ in range(slots_per_quickbar):
            writer.write_uint32_le(0xFFFFFFFF)
    _write_command_actor(writer, actor_id)
    return writer.finish()


def encode_inventory_info(
    *,
    storage_upgrade: int = LOCAL_INVENTORY_CAPACITIES[0],
    bank_storage_upgrade: int = LOCAL_INVENTORY_CAPACITIES[1],
    storage_size: int = LOCAL_INVENTORY_CAPACITIES[2],
    bank_storage_size: int = LOCAL_INVENTORY_CAPACITIES[3],
    extra_bank_storage_size: int = 0,
    extra_storage_size: int = 0,
    health_points: float = 1000.0,
    secondary_resource: float = 100.0,
    actor_id: int = LOCAL_PLAYER_ACTOR_ID,
    force_ui_rebuild: bool = False,
) -> tuple[bytes, int]:
    """Serialize an empty, valid ``InventoryInfoCommand`` (0x0054).

    The twelve leading values are the element counts for the command's item,
    layout and patching arrays.  The storage defaults match the expanded local
    character values already carried by :func:`encode_new_player`; falling
    back to ``default_player``'s 21/10 starter capacities shrinks the UI slot
    arrays and makes this established character crash when inventory opens.

    The field order was recovered from the generated serializer at
    ``dro_client64.exe+0x941d88``.  The command id was independently confirmed
    by ``ClientInventoryManager`` registering its handler under id ``0x54``.
    """

    writer = BitWriter()
    _write_command_header(writer, INVENTORY_INFO_COMMAND)

    # +0x20, +0x30, +0x58, +0x80, +0xa8, +0xd0, +0xf8, +0x118,
    # +0x138, +0x158, +0x180 and +0x1a0: empty generated arrays.
    _write_zero_block(writer, 12)

    # +0x1c0..+0x1d4: player storage capacities.  The corresponding
    # GamePlayer setters identify +0x1c8/+0x1cc/+0x1d0/+0x1d4 as ordinary,
    # bank, extra-bank and extra inventory storage respectively.
    writer.write_uint32_le(storage_upgrade)
    writer.write_uint32_le(bank_storage_upgrade)
    writer.write_uint32_le(storage_size)
    writer.write_uint32_le(bank_storage_size)
    writer.write_uint32_le(extra_bank_storage_size)
    writer.write_uint32_le(extra_storage_size)

    writer.write_float(health_points)       # +0x1d8
    writer.write_float(secondary_resource)   # +0x1dc
    # +0x1e0 is the final serialized boolean in the generated command.  The
    # known-good live InventoryInfo carries false here.
    writer.write_bool(force_ui_rebuild)
    _write_command_actor(writer, actor_id)
    return writer.finish()


def encode_quickbar_info(
    quickbar_amount: int = 1,
    locked_quickbars: int = 0,
    *,
    actor_id: int = LOCAL_PLAYER_ACTOR_ID,
) -> tuple[bytes, int]:
    """Serialize ``QuickbarInfoCommand`` (0x0052)."""

    writer = BitWriter()
    _write_command_header(writer, QUICKBAR_INFO_COMMAND)
    writer.write_uint32_le(quickbar_amount)
    writer.write_uint32_le(locked_quickbars)
    _write_command_actor(writer, actor_id)
    return writer.finish()


def encode_player_ready(
    *, actor_id: int = LOCAL_PLAYER_ACTOR_ID
) -> tuple[bytes, int]:
    """Serialize fieldless ``PlayerReadyCommand`` and its trailing actor ID.

    The concrete command has no fields. DrasaClientHandler reads the actor ID as
    additional server data after command decoding, and the client rejects this
    packet unless a preceding NewPlayer created the same actor.
    """

    actor_id = int(actor_id)
    if not 0 < actor_id <= 0xFFFFFFFF:
        raise ValueError("PlayerReady actor_id must be a non-zero uint32")

    writer = BitWriter()
    _write_command_header(writer, PLAYER_READY_COMMAND)
    _write_command_actor(writer, actor_id)
    return writer.finish()


def encode_local_player_initialization(
    character: Any,
    template_row_index: int,
    *,
    actor_id: int = LOCAL_PLAYER_ACTOR_ID,
    position: tuple[float, float, float] = KINGS_CITY_SPAWN,
    current_xp: int | None = None,
    level_base_xp: int = 0,
    next_level_base_xp: int = 1,
) -> tuple[tuple[bytes, int], ...]:
    """Return the handler-safe local player initialization order."""

    quickbar_amount = 1

    return (
        encode_new_player(
            character,
            template_row_index,
            actor_id=actor_id,
            position=position,
            current_xp=current_xp,
            level_base_xp=level_base_xp,
            next_level_base_xp=next_level_base_xp,
        ),
        # Inventory must exist before quick-slot references are applied and
        # before PlayerReady makes the local actor interactive.
        encode_inventory_info(actor_id=actor_id),
        encode_quick_slots(quickbar_amount, actor_id=actor_id),
        encode_quickbar_info(quickbar_amount, actor_id=actor_id),
        encode_player_ready(actor_id=actor_id),
    )
