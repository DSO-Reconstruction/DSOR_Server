"""Typed codecs for client/server gameplay commands outside player bootstrap."""

from __future__ import annotations

from dso_server.protocol.command_ids import command_id
from dataclasses import dataclass
import math
import struct

from dso_server.protocol.character_selection import (
    BitReader,
    BitWriter,
    CharacterSelectionError,
)


MOVE_COMMAND = command_id("MoveCommand", 0x005F)
LOGOUT_COMMAND = command_id("Commands::LogoutCommand", 0x0063)
SETTINGS_INFO_COMMAND = command_id("Commands::SettingsInfoCommand", 0x0025)
GAUNTLET_NOTIFICATION_INFO_COMMAND = command_id("Commands::GauntletNotificationInfoCommand", 0x012B)
GAUNTLET_NOTIFICATION_RESPONSE_COMMAND = command_id("Commands::GauntletNotificationResponseCommand", 0x012C)
MESSAGE_COMMAND = command_id("Commands::MessageCommand", 0x0089)
SERVER_TO_CLIENT_MSG_COMMAND = command_id("Commands::ServerToClientMsgCommand", 0x0170)
ACTOR_STATS_UPDATE_COMMAND = command_id("Commands::ActorStatsUpdateCommand", 0x007B)
PLAYER_QUERY_COMMAND = command_id("Commands::PlayerQueryCommand", 0x00B4)
PLAYER_QUERY_RESULT_COMMAND = command_id("Commands::PlayerQueryResultCommand", 0x00B5)
ACTOR_REQUEST_COMMAND = command_id("Commands::ActorRequestCommand", 0x001C)
NPC_REQUEST_COMMAND = command_id("Commands::NPCRequestCommand", 0x0029)
ENCOUNTER_COMMAND = command_id("Commands::EncounterCommand", 0x0066)
PROP_REQUEST_COMMAND = command_id("Commands::PropRequestCommand", 0x0038)
STILL_ALIVE_COMMAND = command_id("Commands::StillAliveCommand", 0x006F)
GUILD_MOTD_REQUEST_COMMAND = command_id("Commands::GuildMessageOfTheDayRequestCommand", 0x00BB)
CHAT_CLIENT_LOGGED_IN_COMMAND = command_id("ChatClientLoggedInCommand", 0x00E2)
OCP_OFFER_REQUEST_COMMAND = command_id("Commands::OCPOfferRequestCommand", 0x010B)
STAGING_SIGNAL_COMMAND = command_id("Commands::StagingSignalCommand", 0x0113)
USE_STICKER_BOOK_ITEM_COMMAND = command_id("Commands::UseStickerBookItemCommand", 0x0135)
ADD_STICKER_BOOK_FROM_INVENTORY_COMMAND = command_id("Commands::AddStickerbookFromInventoryCommand", 0x0133)
SET_STICKER_BOOK_FAVORITE_COMMAND = command_id("Commands::UpdateStickerbookFavoriteCommand", 0x0131)
UPGRADE_STICKER_BOOK_ITEM_COMMAND = command_id("Commands::UpgradeStickerbookItemCommand", 0x0134)
QUICK_SLOTS_COMMAND = command_id("Commands::QuickSlotsCommand", 0x0051)
INVENTORY_ITEM_ACTION_COMMAND = command_id("Commands::InventoryCommand", 0x0055)
DISCARD_MONSTER_COMMAND = command_id("Commands::DiscardMonsterCommand", 0x002B)
UNLOCK_MAP_COMMAND = command_id("Commands::UnlockMapCommand", 0x0060)
MAP_TRAVEL_COMMAND = command_id("Commands::TravelCommand", 0x0061)
USE_TRAVEL_ITEM_COMMAND = command_id("Commands::UseTravelItemCommand", 0x0079)
SET_TITLE_COMMAND = command_id("Commands::SetTitleCommand", 0x0115)
CLIENT_LOG_EVENT_COMMAND = command_id("Commands::ClientLogEventCommand", 0x00EA)
CLIENT_AVERAGE_FRAME_INFO_COMMAND = command_id("Commands::ClientAverageFrameInfoTrackingCommand", 0x00E8)
CLIENT_CURRENT_POSITION_FRAME_INFO_COMMAND = command_id(
    "Commands::ClientCurrentPositionFrameInfoTrackingCommand", 0x00E9)
INSTANCE_CONFIG_CLIENT_INFO_COMMAND = command_id("Commands::InstanceConfigClientInfoCommand", 0x001B)
ACTORS_ENTER_VICINITY_COMMAND = command_id("Commands::ActorsEnterVicinityCommand", 0x0074)
CONVERSION_STORY_INFO_COMMAND = command_id("Commands::ConversionStoryInfoCommand", 0x0085)
FORCE_TRANSFORM_COMMAND = command_id("Commands::ForceTransformCommand", 0x0071)
PROP_INFO_COMMAND = command_id("Commands::PropInfoCommand", 0x0036)
MONSTER_UPDATE_COMMAND = command_id("Commands::MonsterUpdateCommand", 0x002C)
LOCATION_EFFECT_INFO_COMMAND = command_id("StatusEffects::LocationEffectInfoCommand", 0x003E)
NEW_LOCATION_EFFECT_COMMAND = command_id("StatusEffects::NewLocationEffectCommand", 0x003C)
DISCARD_LOCATION_EFFECT_COMMAND = command_id("StatusEffects::DiscardLocationEffectCommand", 0x003D)
NEW_TRAP_COMMAND = command_id("StatusEffects::NewTrapCommand", 0x003F)
DISCARD_TRAP_COMMAND = command_id("StatusEffects::DiscardTrapCommand", 0x0040)
TRAP_TRIPPED_COMMAND = command_id("StatusEffects::TrapTrippedCommand", 0x0042)
STATUS_EFFECT_COMMAND = command_id("StatusEffects::StatusEffectCommand", 0x004F)
NEW_NPC_COMMAND = command_id("Commands::NewNPCCommand", 0x0026)
NPC_INFO_COMMAND = command_id("Commands::NPCInfoCommand", 0x0028)
NEW_MONSTER_COMMAND = command_id("Commands::NewMonsterCommand", 0x002A)
GUILD_STATUS_COMMAND = command_id("Commands::GuildStatusCommand", 0x00A7)
IGNORED_STATUS_COMMAND = command_id("Commands::IgnoredStatusCommand", 0x00B3)
OCP_METHODS_COMMAND = command_id("Commands::OCPMethodsCommand", 0x010C)
OCP_OFFER_RESPONSE_COMMAND = command_id("Commands::OCPOfferResponseCommand", 0x010E)
FACTION_INFO_COMMAND = command_id("Commands::FactionInfoCommand", 0x007A)
def _reader(payload: bytes, command_id: int, bit_length: int | None = None) -> BitReader:
    reader = BitReader(payload, len(payload) * 8 if bit_length is None else bit_length)
    if reader.read_uint8() != 0x8B:
        raise ValueError("expected a client application message (0x8b)")
    actual = int.from_bytes(reader.read_bytes(2), "little")
    if actual != command_id:
        raise ValueError(f"expected command 0x{command_id:04x}, got 0x{actual:04x}")
    return reader


@dataclass(frozen=True)
class SettingsInfo:
    enabled: bool


def decode_settings_info(payload: bytes, bit_length: int | None = None) -> SettingsInfo:
    reader = _reader(payload, SETTINGS_INFO_COMMAND, bit_length)
    result = SettingsInfo(reader.read_bool())
    # The final byte is padding when the caller has only the byte-rounded
    # payload. A frame-provided 25-bit length proves there are no other fields.
    if bit_length is not None and reader.remaining:
        raise ValueError("SettingsInfo has unread bits")
    return result


@dataclass(frozen=True)
class Logout:
    field_20: bool
    field_21: bool


def decode_logout(
    payload: bytes, bit_length: int | None = None
) -> Logout:
    # The registered LogoutCommand serializer contains exactly two booleans,
    # hence the retail request's 26 meaningful bits.
    reader = _reader(
        payload,
        LOGOUT_COMMAND,
        26 if bit_length is None else bit_length,
    )
    result = Logout(reader.read_bool(), reader.read_bool())
    if reader.remaining:
        raise ValueError("LogoutCommand has unread bits")
    return result


def encode_logout(value: Logout) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(LOGOUT_COMMAND.to_bytes(2, "little"))
    writer.write_bool(value.field_20)
    writer.write_bool(value.field_21)
    return writer.finish()


# Server → client, actorless.  The client's decoder reads one u32
# (``PlayerLogoutInfoCommand+0x18``); no capture shows it, so the value is
# taken to be the seconds until the pending switch-character / logout
# completes, with 0 for a cancelled countdown.
PLAYER_LOGOUT_INFO_COMMAND = command_id(
    "Commands::PlayerLogoutInfoCommand", 0x0147
)


def encode_player_logout_info(seconds: int) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(PLAYER_LOGOUT_INFO_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(max(0, int(seconds)) & 0xFFFFFFFF)
    return writer.finish()


@dataclass(frozen=True)
class Move:
    quantized_x: int
    quantized_y: int
    quantized_z: int
    scalar_byte: int
    yaw_byte: int
    pitch_byte: int
    start_tick: int
    duration_ticks: int

    @property
    def relative_position(self) -> tuple[float, float, float]:
        """Position relative to the map bounding-box center, in world units."""

        return (
            self.quantized_x / 128.0,
            self.quantized_y / 128.0,
            self.quantized_z / 128.0,
        )

    @property
    def scalar(self) -> float:
        return self.scalar_byte / 64.0

    @property
    def yaw(self) -> float:
        return self.yaw_byte / 256.0 * math.tau - math.pi

    @property
    def pitch(self) -> float:
        return self.pitch_byte / 256.0 * math.tau - math.pi

    @classmethod
    def from_values(
        cls,
        relative_position: tuple[float, float, float],
        scalar: float,
        yaw: float,
        pitch: float,
        start_tick: int,
        duration_ticks: int,
    ) -> "Move":
        quantized = tuple(
            max(-32768, min(32767, round(value * 128.0)))
            for value in relative_position
        )
        return cls(
            *quantized,
            max(0, min(255, int(scalar * 64.0))),
            int((yaw + math.pi) / math.tau * 256.0) & 0xFF,
            int((pitch + math.pi) / math.tau * 256.0) & 0xFF,
            int(start_tick) & 0xFFFFFFFF,
            max(0, min(0xFFFF, int(duration_ticks))),
        )

    def encode_body(self) -> bytes:
        return struct.pack(
            "<hhhBBB I H",
            self.quantized_x,
            self.quantized_y,
            self.quantized_z,
            self.scalar_byte,
            self.yaw_byte,
            self.pitch_byte,
            self.start_tick,
            self.duration_ticks,
        )


def decode_move(payload: bytes) -> Move:
    if len(payload) != 18 or payload[:3] != b"\x8b\x5f\x00":
        raise ValueError("Move request must be an 18-byte 0x8b/0x005f command")
    return Move(*struct.unpack("<hhhBBB I H", payload[3:]))


def encode_move(move: Move, *, actor_id: int) -> tuple[bytes, int]:
    """Encode the server form, including DrasaClientHandler actor data."""

    if not 0 < actor_id <= 0xFFFFFFFF:
        raise ValueError("Move actor_id must be a non-zero uint32")
    payload = b"\x84\x5f\x00" + move.encode_body() + struct.pack("<I", actor_id)
    return payload, len(payload) * 8


@dataclass(frozen=True)
class GauntletNotification:
    category: int
    notification: int


def decode_gauntlet_notification(payload: bytes) -> GauntletNotification:
    reader = _reader(payload, GAUNTLET_NOTIFICATION_INFO_COMMAND)
    result = GauntletNotification(
        category=reader.read_uint32_le(),
        notification=reader.read_uint32_le(),
    )
    if reader.remaining:
        raise ValueError("GauntletNotificationInfo has unread bits")
    return result


def encode_gauntlet_notification_response(
    value: GauntletNotification,
    *,
    actor_id: int = 0,
) -> tuple[bytes, int]:
    """Encode generated ``GauntletNotificationResponseCommand`` fields."""

    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(
        GAUNTLET_NOTIFICATION_RESPONSE_COMMAND.to_bytes(2, "little")
    )
    writer.write_uint32_le(value.category)
    writer.write_uint32_le(value.notification)
    writer.write_uint32_le(actor_id)
    return writer.finish()


@dataclass(frozen=True)
class MessageArgument:
    kind: int
    uint32_value: int = 0
    string_value: str = ""


def _read_message_argument(reader: BitReader) -> MessageArgument:
    """Read one generated ``Game::MessageParam`` union value.

    Retail item-received messages use three alternatives: integer values
    (kind 1), strings (kind 2), and item-instance ids (kind 4). Item ids use
    the same uint32 wire representation as integers but retain a distinct
    discriminator so the client can resolve their icon through ItemManager.
    """

    kind = reader.read_uint8()
    # Kind 3 is an actor id (MessageParam reader 0x140a97a2c): a uint32 like
    # kinds 1 and 4, resolved by the client against its actor/PvP roster.
    if kind in (1, 3, 4):
        return MessageArgument(kind, reader.read_uint32_le())
    if kind == 2:
        return MessageArgument(kind, string_value=reader.read_string())
    raise ValueError(f"unmodeled MessageArgument kind {kind}")


def _write_message_argument(
    writer: BitWriter, argument: MessageArgument,
) -> None:
    kind = int(argument.kind)
    writer.write_uint8(kind)
    if kind in (1, 3, 4):
        writer.write_uint32_le(int(argument.uint32_value))
        return
    if kind == 2:
        writer.write_string(str(argument.string_value))
        return
    raise ValueError(f"unmodeled MessageArgument kind {kind}")


@dataclass(frozen=True)
class Message:
    message_type: int
    arguments: tuple[MessageArgument, ...]


def encode_message(value: Message, *, actor_id: int) -> tuple[bytes, int]:
    """Encode the recovered ``MessageCommand`` union alternatives."""

    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(MESSAGE_COMMAND.to_bytes(2, "little"))
    writer.write_uint8(value.message_type)
    writer.write_uint32_le(len(value.arguments))
    for argument in value.arguments:
        _write_message_argument(writer, argument)
    writer.write_uint32_le(actor_id)
    return writer.finish()


@dataclass(frozen=True)
class ActorStatsUpdate:
    # Names retained for API compatibility. Client reverse engineering and
    # fresh retail captures prove these are live actor vitals: uint64 current
    # health followed by float current class resource.
    stat_id: int
    value: float

    @property
    def health_points(self) -> int:
        return int(self.stat_id)

    @property
    def resource_points(self) -> float:
        return float(self.value)


def encode_actor_stats_update(
    value: ActorStatsUpdate,
    *,
    actor_id: int,
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(ACTOR_STATS_UPDATE_COMMAND.to_bytes(2, "little"))
    writer.write_bytes(int(value.stat_id).to_bytes(8, "little"))
    writer.write_float(value.value)
    writer.write_uint32_le(actor_id)
    return writer.finish()


@dataclass(frozen=True)
class PlayerQuery:
    query_kind: int
    query_id: int
    text: str


def decode_player_query(payload: bytes) -> PlayerQuery:
    reader = _reader(payload, PLAYER_QUERY_COMMAND)
    result = PlayerQuery(
        query_kind=reader.read_uint8(),
        query_id=reader.read_uint32_le(),
        text=reader.read_string(),
    )
    if reader.remaining:
        raise ValueError("PlayerQuery has unread bits")
    return result


def encode_empty_player_query_result(query_id: int) -> tuple[bytes, int]:
    """Encode PlayerQueryResult with a typed empty result collection."""

    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(PLAYER_QUERY_RESULT_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(query_id)
    writer.write_uint32_le(0)
    return writer.finish()


@dataclass(frozen=True)
class UInt32Command:
    command_id: int
    value: int


def decode_uint32_command(payload: bytes, command_id: int) -> UInt32Command:
    reader = _reader(payload, command_id)
    result = UInt32Command(command_id, reader.read_uint32_le())
    if reader.remaining:
        raise ValueError(f"command 0x{command_id:04x} has unread bits")
    return result


def decode_fieldless_command(payload: bytes, command_id: int) -> int:
    reader = _reader(payload, command_id)
    if reader.remaining:
        raise ValueError(f"command 0x{command_id:04x} is not fieldless")
    return command_id


@dataclass(frozen=True)
class ChatClientLoggedIn:
    field_20: int
    field_24: int


def decode_chat_client_logged_in(payload: bytes) -> ChatClientLoggedIn:
    reader = _reader(payload, CHAT_CLIENT_LOGGED_IN_COMMAND)
    result = ChatClientLoggedIn(reader.read_uint32_le(), reader.read_uint32_le())
    if reader.remaining:
        raise ValueError("ChatClientLoggedIn has unread bits")
    return result


@dataclass(frozen=True)
class BooleanCommand:
    command_id: int
    value: bool


def decode_boolean_command(
    payload: bytes,
    command_id: int,
    bit_length: int | None = None,
) -> BooleanCommand:
    reader = _reader(payload, command_id, bit_length)
    result = BooleanCommand(command_id, reader.read_bool())
    if bit_length is not None and reader.remaining:
        raise ValueError(f"command 0x{command_id:04x} has unread bits")
    return result


@dataclass(frozen=True)
class StagingSignal:
    signal_id: str
    signal_type: int
    actor_id: int


def decode_staging_signal(payload: bytes) -> StagingSignal:
    reader = _reader(payload, STAGING_SIGNAL_COMMAND)
    result = StagingSignal(
        signal_id=reader.read_string(),
        signal_type=reader.read_uint8(),
        actor_id=reader.read_uint32_le(),
    )
    if reader.remaining:
        raise ValueError("StagingSignal has unread bits")
    return result


@dataclass(frozen=True)
class StringCommand:
    command_id: int
    value: str


def decode_string_command(payload: bytes, command_id: int) -> StringCommand:
    reader = _reader(payload, command_id)
    result = StringCommand(command_id, reader.read_string())
    if reader.remaining:
        raise ValueError(f"command 0x{command_id:04x} has unread bits")
    return result


@dataclass(frozen=True)
class QuickSlot:
    """One generated QuickSlotsCommand entry.

    ``kind == 0xffffffff`` is the protocol's empty-slot sentinel and omits the
    string entirely.  Other values select the editable resource identifier.
    """

    kind: int
    resource_id: str | None = None


@dataclass(frozen=True)
class QuickSlots:
    slots: tuple[QuickSlot, ...]
    bar_index: int


def decode_quick_slots(payload: bytes) -> QuickSlots:
    """Decode the complete generated client QuickSlotsCommand layout."""

    reader = _reader(payload, QUICK_SLOTS_COMMAND)
    count = reader.read_uint32_le()
    slots = []
    for _ in range(count):
        kind = reader.read_uint32_le()
        slots.append(
            QuickSlot(
                kind=kind,
                resource_id=(
                    None if kind == 0xFFFFFFFF else reader.read_string()
                ),
            )
        )
    result = QuickSlots(tuple(slots), reader.read_uint32_le())
    if reader.remaining:
        raise ValueError("QuickSlots has unread bits")
    return result


def encode_quick_slots(value: QuickSlots) -> tuple[bytes, int]:
    """Serialize an editable client QuickSlotsCommand without capture replay."""

    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(QUICK_SLOTS_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(len(value.slots))
    for slot in value.slots:
        writer.write_uint32_le(slot.kind)
        if slot.kind == 0xFFFFFFFF:
            if slot.resource_id is not None:
                raise ValueError("empty quick slot cannot carry a resource id")
        else:
            if slot.resource_id is None:
                raise ValueError("non-empty quick slot requires a resource id")
            writer.write_string(slot.resource_id)
    writer.write_uint32_le(value.bar_index)
    return writer.finish()


# ``NetworkCommandCreatorProperty::HandleMoveItem`` and ``HandleSplitStack``
# select an operation by which destination slot the UI message carries, in the
# ``Game::Inventory`` container order: equipment, storage, extra storage, bank
# storage, extra bank storage.  Both families take the destination slot as
# their last argument.
MOVE_ITEM_EQUIPMENT_OPERATION = 0x06
MOVE_ITEM_STORAGE_OPERATIONS = {
    0x07: "storage_58",
    0x08: "storage_80",
    0x09: "storage_a8",
    0x0A: "storage_d0",
}
# ``Messaging::SplitStack``'s first argument is the amount that *stays* in the
# source stack, not the amount that moves: ``InventoryProperty::OnSplitStack``
# assigns it directly to the source item's amount.
SPLIT_STACK_OPERATIONS = {
    0x1E: "storage_58",
    0x1F: "storage_80",
    0x20: "storage_a8",
    0x21: "storage_d0",
}


@dataclass(frozen=True)
class InventoryItemAction:
    """Client command emitted when an inventory item is activated.

    The generated ``InventoryCommand`` writes an eight-bit operation followed
    by a uint32 argument count and that many uint32 arguments.  The original
    decoder read the operation together with the low three bytes of the count,
    so the public ``action`` value is the useful composite
    ``operation | (argument_count << 8)``.  For example, ordinary equip is
    operation ``0x01``; a move/swap is ``0x0b`` with zero displaced items or
    ``0x10b`` with one displaced item.

    The anonymous fields retain the wire-compatible representation used by
    the existing gem/item handlers.  Use ``operation``, ``argument_count`` and
    ``arguments`` when dispatching a family of actions.
    """

    item_id: int
    action: int
    field_08: int
    field_0c: int
    field_10: int
    # Move/swap action 0x10b carries one additional item-id/reserved word.
    # Item use action 0x13 (fast travel/payment packages) ends after field_10.
    field_14: int | None = None
    # Gem insertion action 0x211 carries one further reserved word.
    field_18: int | None = None

    @property
    def operation(self) -> int:
        """Return the actual eight-bit ``InventoryAction`` operation."""

        return int(self.action) & 0xFF

    @property
    def argument_count(self) -> int:
        """Return the uint32 argument count split across action/field_08."""

        return (int(self.action) >> 8) | (int(self.field_08) << 24)

    @property
    def arguments(self) -> tuple[int, ...]:
        """Return the action's variable uint32 argument array."""

        count = self.argument_count
        values = (
            self.field_0c,
            self.field_10,
            self.field_14,
            self.field_18,
        )
        if count > len(values):
            raise ValueError(
                "InventoryItemAction argument array exceeds decoded fields"
            )
        if any(value is None for value in values[:count]):
            raise ValueError(
                "InventoryItemAction argument array is truncated"
            )
        return tuple(int(value) for value in values[:count])

    @property
    def removal_socket_index(self) -> int:
        """Return the socket selected by remove-gem action ``0x0114``."""

        if self.operation != 0x14 or self.argument_count != 1:
            raise ValueError(
                "removal_socket_index requires remove-gem operation 0x14 "
                "with one argument"
            )
        return self.arguments[0]

    @property
    def insertion_socket_index(self) -> int:
        """Return the socket selected by insert-gem action ``0x0211``."""

        if self.operation != 0x11 or self.argument_count != 2:
            raise ValueError(
                "insertion_socket_index requires insert-gem operation 0x11 "
                "with two arguments"
            )
        return self.arguments[1]


def decode_inventory_item_action(payload: bytes) -> InventoryItemAction:
    reader = _reader(payload, INVENTORY_ITEM_ACTION_COMMAND)
    item_id = reader.read_uint32_le()
    action = reader.read_uint32_le()
    field_08 = reader.read_uint8()
    field_0c = reader.read_uint32_le()
    field_10 = reader.read_uint32_le()
    optional = []
    while reader.remaining >= 32:
        optional.append(reader.read_uint32_le())
    result = InventoryItemAction(
        item_id,
        action,
        field_08,
        field_0c,
        field_10,
        optional[0] if optional else None,
        optional[1] if len(optional) > 1 else None,
    )
    if len(optional) > 2:
        raise ValueError("InventoryItemAction has too many optional words")
    if reader.remaining:
        raise ValueError("InventoryItemAction has unread bits")
    return result


def encode_inventory_item_action(
    value: InventoryItemAction,
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(INVENTORY_ITEM_ACTION_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(value.item_id)
    writer.write_uint32_le(value.action)
    writer.write_uint8(value.field_08)
    writer.write_uint32_le(value.field_0c)
    writer.write_uint32_le(value.field_10)
    if value.field_14 is not None:
        writer.write_uint32_le(value.field_14)
    if value.field_18 is not None:
        if value.field_14 is None:
            raise ValueError(
                "InventoryItemAction field_18 requires field_14"
            )
        writer.write_uint32_le(value.field_18)
    return writer.finish()


@dataclass(frozen=True)
class UnlockMapRequest:
    """Client interaction with a concrete exit in the current map.

    The generated client names command ``0x0060`` UnlockMapCommand, but the
    live service also uses it as the authoritative request to traverse that
    exit.  The wire fields are ``MapId``, ``ExitURL``, ``ExitId``, and
    the trailing flag, in that order.  Shared/template exits put the active
    map in both strings.  Concrete named exits put the selected MapAccess
    config in ``MapId`` and the clicked ExitProperty's authored URL in
    ``ExitURL``.  The two strings are equal when the authored/base access
    is selected, but a higher PW difficulty deliberately produces a mixed
    pair (for example ``pw001_02...`` followed by ``pw001_01...``).
    """

    map_id: str
    exit_url: str
    exit_id: str
    force_entry: bool

    # Compatibility aliases for diagnostics written before the client field
    # names were recovered.  New validation must use map_id/exit_url because
    # these values are neither a source/current map pair nor interchangeable.
    @property
    def source_map_id(self) -> str:
        return self.map_id

    @property
    def current_map_id(self) -> str:
        return self.exit_url


def decode_unlock_map_request(
    payload: bytes, bit_length: int | None = None
) -> UnlockMapRequest:
    reader = _reader(payload, UNLOCK_MAP_COMMAND, bit_length)
    result = UnlockMapRequest(
        reader.read_string(),
        reader.read_string(),
        reader.read_string(),
        reader.read_bool(),
    )
    if bit_length is not None:
        if reader.remaining:
            raise ValueError("UnlockMapRequest has unread bits")
    elif reader.remaining and reader.read_bits(reader.remaining) != 0:
        raise ValueError("UnlockMapRequest has non-zero padding")
    return result


def encode_unlock_map_request(value: UnlockMapRequest) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(UNLOCK_MAP_COMMAND.to_bytes(2, "little"))
    writer.write_string(value.map_id)
    writer.write_string(value.exit_url)
    writer.write_string(value.exit_id)
    writer.write_bool(value.force_entry)
    return writer.finish()


@dataclass(frozen=True)
class MapTravelRequest:
    """Editable body of the client command which selects a travel map."""

    difficulty: int
    map_id: str
    entry_index: int
    force_entry: bool
    # In-world travel objects (travel stones and waypoints) use a second
    # legitimate TravelCommand body:
    #   [uint8 mode] string travelPointId, string mapId, uint16 entryIndex
    # Fast-travel UI requests leave this empty and retain the body above.
    travel_point_id: str = ""
    # Leading selector of the travel-object body, or ``None`` when the body
    # carries no mode byte.  It is not the scaling slider: those requests
    # carry the chosen tier in ``map_id`` (for example ``a0303_wildcave_08``)
    # and leave ``difficulty`` zero, so the value is kept separate instead of
    # being fed to monster scaling.
    mode: int | None = None
    # Native TravelCommand serializes three strings and a bool: travel
    # source, destination map, destination entry. Event previews leave the
    # source empty and name the entry from TeleportTarget. None preserves
    # the older numeric-entry layouts; "" is an explicit default entry.
    entry_name: str | None = None


@dataclass(frozen=True)
class UseTravelItemRequest:
    """Client selection made after a scaling TravelItem is activated.

    ``InventoryItemAction`` opens the difficulty selector.  The selected
    access-group row is then submitted in command 0x0079 together with the
    runtime inventory item id.  This is a separate path from TravelCommand
    (0x0061), not an alternate encoding of it.
    """

    item_id: int
    map_id: str


@dataclass(frozen=True)
class SetTitleRequest:
    """Character overhead-title display mode selected by the client."""

    title_type: int
    achievement_title: str


def decode_set_title_request(
    payload: bytes, bit_length: int | None = None
) -> SetTitleRequest:
    # Captured bodies are ``00 0000`` (hidden) and ``02 0000`` (PvP rank).
    # The final field is the generated UTF-8 string used by mode 1.
    reader = _reader(payload, SET_TITLE_COMMAND, bit_length)
    result = SetTitleRequest(reader.read_uint8(), reader.read_string())
    if result.title_type not in {0, 1, 2}:
        raise ValueError(f"invalid title display type {result.title_type}")
    if reader.remaining:
        if bit_length is not None or reader.read_bits(reader.remaining) != 0:
            raise ValueError("SetTitleRequest has unread data")
    return result


def _read_travel_tail_flag(reader: BitReader) -> bool | None:
    """Consume the optional trailing flag plus serializer padding.

    The travel-object body ends byte-aligned, so an exact RakNet length leaves
    nothing behind and a byte-rounded dispatch payload leaves one padding byte.
    Either may instead hold a trailing bool, which is read from the first bit.
    Returns the flag, or ``None`` when leftover bits prove the body was
    mis-identified.
    """

    flag = reader.read_bool() if reader.remaining else False
    if reader.remaining and reader.read_bits(reader.remaining) != 0:
        return None
    return flag


def _is_wire_map_id(value: str) -> bool:
    return bool(value) and all(
        character.isprintable() for character in value
    )


def _decode_travel_object_body(
    payload: bytes, bit_length: int | None, *, has_mode: bool
) -> MapTravelRequest | None:
    """Parse the in-world travel-object body, or return ``None``.

    Travel stones and waypoints send two counted strings (the travel point's
    resource id and the destination map) followed by the entry index.  The
    leading mode byte is present on some objects and absent on others, so both
    layouts are attempted and the one that consumes the payload exactly wins.
    """

    try:
        reader = _reader(payload, MAP_TRAVEL_COMMAND, bit_length)
        mode = reader.read_uint8() if has_mode else None
        travel_point_id = reader.read_string()
        map_id = reader.read_string()
        entry_index = int.from_bytes(reader.read_bytes(2), "little")
    except (CharacterSelectionError, struct.error):
        return None
    if not _is_wire_map_id(travel_point_id) or not _is_wire_map_id(map_id):
        return None
    force_entry = _read_travel_tail_flag(reader)
    if force_entry is None:
        return None
    return MapTravelRequest(
        0,
        map_id,
        entry_index,
        force_entry,
        travel_point_id,
        mode,
    )


def _decode_travel_selection_body(
    payload: bytes, bit_length: int | None
) -> MapTravelRequest | None:
    """Parse the fast-travel UI body, or return ``None``."""

    try:
        reader = _reader(payload, MAP_TRAVEL_COMMAND, bit_length)
        difficulty = int.from_bytes(reader.read_bytes(2), "little")
        map_id = reader.read_string()
        entry_index = int.from_bytes(reader.read_bytes(2), "little")
        force_entry = reader.read_bool()
    except (CharacterSelectionError, struct.error):
        return None
    if not _is_wire_map_id(map_id):
        return None
    # RakNet supplies the exact 145-bit length; byte-rounded dispatch payloads
    # carry seven padding bits which must be zero.
    if reader.remaining and reader.read_bits(reader.remaining) != 0:
        return None
    return MapTravelRequest(difficulty, map_id, entry_index, force_entry)


def _decode_travel_named_entry_body(
    payload: bytes, bit_length: int | None
) -> MapTravelRequest | None:
    """Native three-string body, including an event with no TeleportTarget."""

    try:
        reader = _reader(payload, MAP_TRAVEL_COMMAND, bit_length)
        source = reader.read_string()
        map_id = reader.read_string()
        entry_name = reader.read_string()
        force_entry = reader.read_bool()
    except (CharacterSelectionError, struct.error):
        return None
    if any(value and not _is_wire_map_id(value) for value in (source, map_id, entry_name)):
        return None
    # At most seven zero bits may round the final bool up to a byte.
    if reader.remaining > 7 or (reader.remaining and reader.read_bits(reader.remaining) != 0):
        return None
    return MapTravelRequest(0, map_id, 0, force_entry,
                            travel_point_id=source, entry_name=entry_name)


def decode_map_travel_request(
    payload: bytes, bit_length: int | None = None
) -> MapTravelRequest:
    """Decode native named-entry travel and the supported legacy layouts.

    An empty native entry is wire-identical to numeric entry zero. Preserve
    that existing representation; nonempty names and empty event targets use
    the explicit named-entry form.
    """

    for candidate in (
        _decode_travel_selection_body(payload, bit_length),
        _decode_travel_object_body(payload, bit_length, has_mode=True),
        _decode_travel_object_body(payload, bit_length, has_mode=False),
        _decode_travel_named_entry_body(payload, bit_length),
    ):
        if candidate is not None:
            return candidate
    raise ValueError(
        f"unrecognized TravelCommand body: raw={payload.hex()} "
        f"bits={len(payload) * 8 if bit_length is None else bit_length}"
    )


def encode_map_travel_request(value: MapTravelRequest) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(MAP_TRAVEL_COMMAND.to_bytes(2, "little"))
    if value.entry_name is not None:
        writer.write_string(value.travel_point_id)
        writer.write_string(value.map_id)
        writer.write_string(value.entry_name)
        writer.write_bool(value.force_entry)
        return writer.finish()
    if value.travel_point_id:
        if value.mode is not None:
            writer.write_uint8(int(value.mode))
        writer.write_string(value.travel_point_id)
        writer.write_string(value.map_id)
        writer.write_bytes(struct.pack("<H", int(value.entry_index)))
        # The captured bodies stop here; a flag is only re-emitted when the
        # decoder actually saw one in the trailing byte.
        if value.force_entry:
            writer.write_bool(True)
        return writer.finish()
    writer.write_bytes(struct.pack("<H", int(value.difficulty)))
    writer.write_string(value.map_id)
    writer.write_bytes(struct.pack("<H", int(value.entry_index)))
    writer.write_bool(value.force_entry)
    return writer.finish()


def decode_use_travel_item_request(
    payload: bytes, bit_length: int | None = None
) -> UseTravelItemRequest:
    reader = _reader(payload, USE_TRAVEL_ITEM_COMMAND, bit_length)
    result = UseTravelItemRequest(
        reader.read_uint32_le(),
        reader.read_string(),
    )
    if bit_length is not None:
        if reader.remaining:
            raise ValueError("UseTravelItemRequest has unread bits")
    elif reader.remaining and reader.read_bits(reader.remaining) != 0:
        raise ValueError("UseTravelItemRequest has non-zero padding")
    if not result.map_id:
        raise ValueError("UseTravelItemRequest map id cannot be empty")
    return result


def encode_use_travel_item_request(
    value: UseTravelItemRequest,
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(USE_TRAVEL_ITEM_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(value.item_id)
    writer.write_string(value.map_id)
    return writer.finish()


@dataclass(frozen=True)
class AddStickerbookFromInventoryRequest:
    item_id: int


def decode_add_stickerbook_from_inventory(
    payload: bytes, bit_length: int | None = None
) -> AddStickerbookFromInventoryRequest:
    reader = _reader(
        payload, ADD_STICKER_BOOK_FROM_INVENTORY_COMMAND, bit_length
    )
    result = AddStickerbookFromInventoryRequest(reader.read_uint32_le())
    if bit_length is not None:
        if reader.remaining:
            raise ValueError(
                "AddStickerbookFromInventoryRequest has unread bits"
            )
    elif reader.remaining and reader.read_bits(reader.remaining) != 0:
        raise ValueError(
            "AddStickerbookFromInventoryRequest has non-zero padding"
        )
    return result


def encode_add_stickerbook_from_inventory(
    value: AddStickerbookFromInventoryRequest,
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(
        ADD_STICKER_BOOK_FROM_INVENTORY_COMMAND.to_bytes(2, "little")
    )
    writer.write_uint32_le(value.item_id)
    return writer.finish()


@dataclass(frozen=True)
class SetStickerbookFavoriteRequest:
    resource_id: str
    favorite: bool


def decode_set_stickerbook_favorite(
    payload: bytes, bit_length: int | None = None
) -> SetStickerbookFavoriteRequest:
    reader = _reader(payload, SET_STICKER_BOOK_FAVORITE_COMMAND, bit_length)
    result = SetStickerbookFavoriteRequest(
        reader.read_string(), reader.read_bool()
    )
    if bit_length is not None:
        if reader.remaining:
            raise ValueError("SetStickerbookFavoriteRequest has unread bits")
    elif reader.remaining and reader.read_bits(reader.remaining) != 0:
        raise ValueError(
            "SetStickerbookFavoriteRequest has non-zero padding"
        )
    return result


def encode_set_stickerbook_favorite(
    value: SetStickerbookFavoriteRequest,
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(
        SET_STICKER_BOOK_FAVORITE_COMMAND.to_bytes(2, "little")
    )
    writer.write_string(value.resource_id)
    writer.write_bool(value.favorite)
    return writer.finish()


@dataclass(frozen=True)
class UpgradeStickerbookItemRequest:
    resource_id: str


def decode_upgrade_stickerbook_item(
    payload: bytes, bit_length: int | None = None
) -> UpgradeStickerbookItemRequest:
    reader = _reader(payload, UPGRADE_STICKER_BOOK_ITEM_COMMAND, bit_length)
    result = UpgradeStickerbookItemRequest(reader.read_string())
    if bit_length is not None:
        if reader.remaining:
            raise ValueError("UpgradeStickerbookItemRequest has unread bits")
    elif reader.remaining and reader.read_bits(reader.remaining) != 0:
        raise ValueError(
            "UpgradeStickerbookItemRequest has non-zero padding"
        )
    return result


def encode_upgrade_stickerbook_item(
    value: UpgradeStickerbookItemRequest,
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(
        UPGRADE_STICKER_BOOK_ITEM_COMMAND.to_bytes(2, "little")
    )
    writer.write_string(value.resource_id)
    return writer.finish()


@dataclass(frozen=True)
class ClientLogEventEntry:
    """Generated telemetry entry contained by ClientLogEventCommand."""

    event_id: int
    context_id: int
    numeric_properties: tuple[tuple[str, int], ...]
    string_properties: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ClientLogEvent:
    events: tuple[ClientLogEventEntry, ...]
    sequence: int


def decode_client_log_event(payload: bytes) -> ClientLogEvent:
    """Decode every field proven by the generated 0x00ea serializers."""

    reader = _reader(payload, CLIENT_LOG_EVENT_COMMAND)
    events = []
    for _ in range(reader.read_uint32_le()):
        event_id = reader.read_uint32_le()
        context_id = reader.read_uint32_le()
        numeric_properties = tuple(
            (reader.read_string(), reader.read_uint32_le())
            for _ in range(reader.read_uint32_le())
        )
        string_properties = tuple(
            (reader.read_string(), reader.read_string())
            for _ in range(reader.read_uint32_le())
        )
        events.append(
            ClientLogEventEntry(
                event_id,
                context_id,
                numeric_properties,
                string_properties,
            )
        )
    result = ClientLogEvent(tuple(events), reader.read_uint32_le())
    if reader.remaining:
        raise ValueError("ClientLogEvent has unread bits")
    return result


def encode_client_log_event(value: ClientLogEvent) -> tuple[bytes, int]:
    """Serialize an editable ClientLogEventCommand without capture replay."""

    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(CLIENT_LOG_EVENT_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(len(value.events))
    for event in value.events:
        writer.write_uint32_le(event.event_id)
        writer.write_uint32_le(event.context_id)
        writer.write_uint32_le(len(event.numeric_properties))
        for name, number in event.numeric_properties:
            writer.write_string(name)
            writer.write_uint32_le(number)
        writer.write_uint32_le(len(event.string_properties))
        for name, text in event.string_properties:
            writer.write_string(name)
            writer.write_string(text)
    writer.write_uint32_le(value.sequence)
    return writer.finish()


@dataclass(frozen=True)
class InstanceConfigClientInfo:
    config_id: int
    enabled: bool


def encode_instance_config_client_info(
    value: InstanceConfigClientInfo,
    *,
    actor_id: int = 0,
) -> tuple[bytes, int]:
    """Serialize the generated 33-bit body and server actor context."""

    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(INSTANCE_CONFIG_CLIENT_INFO_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(value.config_id)
    writer.write_bool(value.enabled)
    writer.write_uint32_le(actor_id)
    return writer.finish()


def decode_instance_config_client_info(
    payload: bytes,
    bit_length: int,
) -> tuple[InstanceConfigClientInfo, int]:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x84:
        raise ValueError("expected a server application message (0x84)")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != INSTANCE_CONFIG_CLIENT_INFO_COMMAND:
        raise ValueError(f"expected command 0x001b, got 0x{command_id:04x}")
    value = InstanceConfigClientInfo(reader.read_uint32_le(), reader.read_bool())
    actor_id = reader.read_uint32_le()
    if reader.remaining:
        raise ValueError("InstanceConfigClientInfo has unread bits")
    return value, actor_id


@dataclass(frozen=True)
class ClientPositionFrameInfo:
    """Diagnostic 0xe9 fields; measurements retain their original uint32 bits.

    This is client telemetry, not an authoritative movement or score update.
    Layout: generated decoder 0x140970a08, with six-word sample records.
    """

    character_id: int
    frame_seconds: float
    measurements: tuple[int, ...]
    samples: tuple[tuple[int, ...], ...]
    field_60: int
    field_64: int


def decode_client_position_frame_info(payload: bytes) -> ClientPositionFrameInfo:
    reader = _reader(payload, CLIENT_CURRENT_POSITION_FRAME_INFO_COMMAND)
    character_id = reader.read_uint32_le()
    frame_seconds = struct.unpack("<d", reader.read_bytes(8))[0]
    measurements = tuple(reader.read_uint32_le() for _ in range(5))
    count = reader.read_uint32_le()
    if count > 1_000_000 or count * 192 + 64 != reader.remaining:
        raise ValueError("ClientPositionFrameInfo has an invalid sample count or length")
    samples = tuple(tuple(reader.read_uint32_le() for _ in range(6)) for _ in range(count))
    return ClientPositionFrameInfo(
        character_id, frame_seconds, measurements, samples,
        reader.read_uint32_le(), reader.read_uint32_le(),
    )


@dataclass(frozen=True)
class ClientAverageFrameInfo:
    client_tick: int
    average_frame_seconds: float
    resolution_width: int
    resolution_height: int
    enabled: bool


def decode_client_average_frame_info(
    payload: bytes,
    bit_length: int | None = None,
) -> ClientAverageFrameInfo:
    reader = _reader(payload, CLIENT_AVERAGE_FRAME_INFO_COMMAND, bit_length)
    result = ClientAverageFrameInfo(
        client_tick=reader.read_uint32_le(),
        average_frame_seconds=struct.unpack("<d", reader.read_bytes(8))[0],
        resolution_width=reader.read_uint32_le(),
        resolution_height=reader.read_uint32_le(),
        enabled=reader.read_bool(),
    )
    # Runtime dispatch receives the byte-rounded payload rather than RakNet's
    # bit count.  The generated body is exactly 161 bits, leaving seven zero
    # padding bits in that case.
    if bit_length is not None:
        if reader.remaining:
            raise ValueError("ClientAverageFrameInfo has unread bits")
    elif reader.remaining and reader.read_bits(reader.remaining) != 0:
        raise ValueError("ClientAverageFrameInfo has non-zero padding")
    return result


def encode_client_average_frame_info(
    value: ClientAverageFrameInfo,
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(CLIENT_AVERAGE_FRAME_INFO_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(value.client_tick)
    writer.write_bytes(struct.pack("<d", float(value.average_frame_seconds)))
    writer.write_uint32_le(value.resolution_width)
    writer.write_uint32_le(value.resolution_height)
    writer.write_bool(value.enabled)
    return writer.finish()


@dataclass(frozen=True)
class ActorsEnterVicinity:
    actor_ids: tuple[int, ...]


def encode_actors_enter_vicinity(
    value: ActorsEnterVicinity,
    *,
    actor_id: int = 0,
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(ACTORS_ENTER_VICINITY_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(len(value.actor_ids))
    for entry in value.actor_ids:
        writer.write_uint32_le(entry)
    writer.write_uint32_le(actor_id)
    return writer.finish()


def decode_actors_enter_vicinity(
    payload: bytes,
    bit_length: int | None = None,
) -> tuple[ActorsEnterVicinity, int]:
    reader = BitReader(payload, len(payload) * 8 if bit_length is None else bit_length)
    if reader.read_uint8() not in (0x84, 0x85):
        raise ValueError("expected a server application message")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != ACTORS_ENTER_VICINITY_COMMAND:
        raise ValueError(f"expected command 0x0074, got 0x{command_id:04x}")
    value = ActorsEnterVicinity(
        tuple(reader.read_uint32_le() for _ in range(reader.read_uint32_le()))
    )
    actor_id = reader.read_uint32_le()
    if reader.remaining:
        raise ValueError("ActorsEnterVicinity has unread bits")
    return value, actor_id


@dataclass(frozen=True)
class ConversionStoryInfo:
    field_00: int
    server_tick: int
    story_time: str
    field_08: int


def encode_conversion_story_info(
    value: ConversionStoryInfo,
    *,
    actor_id: int = 0,
) -> tuple[bytes, int]:
    """Serialize helper 0x1409f7edc used by command 0x0085."""

    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(CONVERSION_STORY_INFO_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(value.field_00)
    writer.write_uint32_le(value.server_tick)
    writer.write_string(value.story_time)
    writer.write_uint8(value.field_08)
    writer.write_uint32_le(actor_id)
    return writer.finish()


def decode_conversion_story_info(
    payload: bytes,
) -> tuple[ConversionStoryInfo, int]:
    reader = BitReader(payload)
    if reader.read_uint8() not in (0x84, 0x85):
        raise ValueError("expected a server application message")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != CONVERSION_STORY_INFO_COMMAND:
        raise ValueError(f"expected command 0x0085, got 0x{command_id:04x}")
    value = ConversionStoryInfo(
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_string(),
        reader.read_uint8(),
    )
    actor_id = reader.read_uint32_le()
    if reader.remaining:
        raise ValueError("ConversionStoryInfo has unread bits")
    return value, actor_id


@dataclass(frozen=True)
class PropInfoEntry:
    prop_id: int
    template_id: str
    instance_guid: bytes
    field_20: int
    field_24: int
    field_28: int
    position: tuple[float, float, float]
    rotation: float
    field_48: str
    field_50: str
    field_58: int
    field_5c: int
    field_60: int


@dataclass(frozen=True)
class PropInfo:
    entries: tuple[PropInfoEntry, ...]


def encode_prop_info(
    value: PropInfo,
    *,
    actor_id: int = 0,
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(PROP_INFO_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(len(value.entries))
    for entry in value.entries:
        writer.write_uint32_le(entry.prop_id)
        writer.write_string(entry.template_id)
        if len(entry.instance_guid) > 0xFFFF:
            raise ValueError("PropInfo instance_guid is too long")
        writer.write_bytes(len(entry.instance_guid).to_bytes(2, "little"))
        writer.write_bytes(entry.instance_guid)
        writer.write_uint32_le(entry.field_20)
        writer.write_uint8(entry.field_24)
        writer.write_uint32_le(entry.field_28)
        for component in entry.position:
            writer.write_float(component)
        writer.write_float(entry.rotation)
        writer.write_string(entry.field_48)
        writer.write_string(entry.field_50)
        writer.write_uint8(entry.field_58)
        writer.write_uint8(entry.field_5c)
        writer.write_uint32_le(entry.field_60)
    writer.write_uint32_le(actor_id)
    return writer.finish()


def decode_prop_info(payload: bytes) -> tuple[PropInfo, int]:
    reader = BitReader(payload)
    if reader.read_uint8() not in (0x84, 0x85):
        raise ValueError("expected a server application message")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != PROP_INFO_COMMAND:
        raise ValueError(f"expected command 0x0036, got 0x{command_id:04x}")
    entries = []
    for _ in range(reader.read_uint32_le()):
        prop_id = reader.read_uint32_le()
        template_id = reader.read_string()
        guid_size = int.from_bytes(reader.read_bytes(2), "little")
        instance_guid = reader.read_bytes(guid_size)
        field_20 = reader.read_uint32_le()
        field_24 = reader.read_uint8()
        field_28 = reader.read_uint32_le()
        position = tuple(reader.read_float() for _ in range(3))
        rotation = reader.read_float()
        field_48 = reader.read_string()
        field_50 = reader.read_string()
        field_58 = reader.read_uint8()
        field_5c = reader.read_uint8()
        field_60 = reader.read_uint32_le()
        entries.append(
            PropInfoEntry(
                prop_id,
                template_id,
                instance_guid,
                field_20,
                field_24,
                field_28,
                position,
                rotation,
                field_48,
                field_50,
                field_58,
                field_5c,
                field_60,
            )
        )
    actor_id = reader.read_uint32_le()
    if reader.remaining:
        raise ValueError("PropInfo has unread bits")
    return PropInfo(tuple(entries)), actor_id


@dataclass(frozen=True)
class MonsterUpdate:
    field_20: int
    field_28: int
    field_30: int
    field_34: int
    field_38: int


def encode_monster_update(
    value: MonsterUpdate,
    *,
    actor_id: int,
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(MONSTER_UPDATE_COMMAND.to_bytes(2, "little"))
    writer.write_uint64_le(value.field_20)
    writer.write_uint64_le(value.field_28)
    writer.write_uint32_le(value.field_30)
    writer.write_uint32_le(value.field_34)
    writer.write_uint32_le(value.field_38)
    writer.write_uint32_le(actor_id)
    return writer.finish()


def decode_monster_update(payload: bytes) -> tuple[MonsterUpdate, int]:
    reader = BitReader(payload)
    if reader.read_uint8() not in (0x84, 0x85):
        raise ValueError("expected a server application message")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != MONSTER_UPDATE_COMMAND:
        raise ValueError(f"expected command 0x002c, got 0x{command_id:04x}")
    value = MonsterUpdate(
        reader.read_uint64_le(),
        reader.read_uint64_le(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
    )
    actor_id = reader.read_uint32_le()
    if reader.remaining:
        raise ValueError("MonsterUpdate has unread bits")
    return value, actor_id


@dataclass(frozen=True)
class StatusEffectEntry:
    effect_id: int
    field_04: int
    field_0c: int
    field_14: int
    field_08: int
    field_18: int
    field_10: int
    field_1c: int
    field_20: int
    field_25: bool
    field_26: bool
    field_39: bool
    has_details: bool
    values: tuple[float, ...] = ()
    field_38: bool = False
    field_3c: int = 0xFF
    shape_values: tuple[float, ...] = ()

    def __post_init__(self):
        # End tick zero is native permanence. A detailed finite effect needs
        # a positive duration, including instantaneous one-tick effects.
        if self.has_details and self.field_0c and self.field_10 <= 0:
            span = (int(self.field_0c) - int(self.field_08)) & 0xffffffff
            span = span if 0 < span < 0x80000000 else 1
            object.__setattr__(self, 'field_10', span)
            if self.field_0c == self.field_08:
                object.__setattr__(self, 'field_0c', (self.field_08 + span) & 0xffffffff)


@dataclass(frozen=True)
class StatusEffect:
    clear_existing: bool
    entries: tuple[StatusEffectEntry, ...]


@dataclass(frozen=True)
class StatusEffectServerCommand:
    value: StatusEffect
    actor_id: int


@dataclass(frozen=True)
class StatusEffectCommandBatch:
    """One or more actor-scoped StatusEffect commands in a 0x85 batch."""

    commands: tuple[StatusEffectServerCommand, ...]


def encode_status_effect(
    value: StatusEffect,
    *,
    actor_id: int,
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(STATUS_EFFECT_COMMAND.to_bytes(2, "little"))
    _write_status_effect_body(writer, value)
    writer.write_uint32_le(actor_id)
    return writer.finish()


def _write_status_effect_body(writer: BitWriter, value: StatusEffect) -> None:
    writer.write_bool(value.clear_existing)
    writer.write_uint32_le(len(value.entries))
    for entry in value.entries:
        writer.write_bytes(int(entry.effect_id).to_bytes(2, "little"))
        # The client reuses field_26 as the BuffBar counted-icon bit and
        # forwards field_18 directly to UI::Icon::AddCount. That function
        # asserts unless the value is strictly positive. Keep the protocol
        # boundary safe even if a future producer builds an invalid refresh.
        icon_count = (
            max(1, int(entry.field_18))
            if entry.field_26
            else int(entry.field_18)
        )
        for number in (
            entry.field_04,
            entry.field_0c,
            entry.field_14,
            entry.field_08,
            icon_count,
            entry.field_10,
            entry.field_1c,
            entry.field_20,
        ):
            writer.write_uint32_le(number)
        writer.write_bool(entry.field_25)
        writer.write_bool(entry.field_26)
        writer.write_bool(entry.field_39)
        writer.write_bool(entry.has_details)
        if entry.has_details:
            writer.write_uint32_le(len(entry.values))
            for number in entry.values:
                writer.write_float(number)
            writer.write_bool(entry.field_38)
            writer.write_uint8(entry.field_3c)
            has_shape = entry.field_38 or entry.field_3c != 0xFF
            expected = 7 if entry.field_3c == 0 else 6
            if has_shape:
                if len(entry.shape_values) != expected:
                    raise ValueError(
                        f"StatusEffect shape branch requires {expected} floats"
                    )
                for number in entry.shape_values:
                    writer.write_float(number)
            elif entry.shape_values:
                raise ValueError("StatusEffect default branch cannot carry shape values")
        elif entry.values or entry.shape_values:
            raise ValueError("StatusEffect without details cannot carry detail values")


def _read_status_effect_body(reader: BitReader) -> StatusEffect:
    clear_existing = reader.read_bool()
    entries = []
    for _ in range(reader.read_uint32_le()):
        effect_id = int.from_bytes(reader.read_bytes(2), "little")
        numbers = tuple(reader.read_uint32_le() for _ in range(8))
        field_25 = reader.read_bool()
        field_26 = reader.read_bool()
        field_39 = reader.read_bool()
        has_details = reader.read_bool()
        values: tuple[float, ...] = ()
        field_38 = False
        field_3c = 0xFF
        shape_values: tuple[float, ...] = ()
        if has_details:
            values = tuple(
                reader.read_float() for _ in range(reader.read_uint32_le())
            )
            field_38 = reader.read_bool()
            field_3c = reader.read_uint8()
            if field_38 or field_3c != 0xFF:
                shape_count = 7 if field_3c == 0 else 6
                shape_values = tuple(reader.read_float() for _ in range(shape_count))
        entries.append(
            StatusEffectEntry(
                effect_id,
                *numbers,
                field_25,
                field_26,
                field_39,
                has_details,
                values,
                field_38,
                field_3c,
                shape_values,
            )
        )
    return StatusEffect(clear_existing, tuple(entries))


def decode_status_effect(
    payload: bytes,
    bit_length: int,
    *,
    batch_terminator: bool = False,
) -> tuple[StatusEffect, int]:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() not in (0x84, 0x85):
        raise ValueError("expected a server application message")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != STATUS_EFFECT_COMMAND:
        raise ValueError(f"expected command 0x004f, got 0x{command_id:04x}")
    value = _read_status_effect_body(reader)
    actor_id = reader.read_uint32_le()
    if batch_terminator:
        if reader.read_uint8() != 0xFF:
            raise ValueError("StatusEffect batch terminator is missing")
    if reader.remaining:
        raise ValueError("StatusEffect has unread bits")
    return value, actor_id


@dataclass(frozen=True)
class ActorPlacement:
    field_00: int
    actor_id: int
    field_08: int
    field_0c: bool
    field_0d: bool


def _write_actor_placement(writer: BitWriter, value: ActorPlacement) -> None:
    writer.write_uint32_le(value.field_00)
    writer.write_uint32_le(value.actor_id)
    writer.write_uint8(value.field_08)
    writer.write_bool(value.field_0c)
    writer.write_bool(value.field_0d)


def _read_actor_placement(reader: BitReader) -> ActorPlacement:
    return ActorPlacement(
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_uint8(),
        reader.read_bool(),
        reader.read_bool(),
    )


@dataclass(frozen=True)
class LocationEffectInfo:
    """One generated ``StatusEffects::LocationEffectInfo`` value.

    Location effects are world-space visual lifecycles.  In particular,
    dwarf constructions use them to run their authored summon and despawn
    aura instead of appearing/disappearing as bare monster actors.
    """

    instance_id: int
    effect_type: str
    instance_guid: str
    enabled: bool
    transform_mode: int
    position: tuple[float, float, float]
    yaw: float
    scale: tuple[float, ...]
    effect_resource_id: str
    field_78: bool
    field_79: bool
    expires_tick: int
    ticks_per_second: int
    starts_tick: int
    values: tuple[float, ...]
    strength_percent: int
    owner_actor_id: int
    placement: ActorPlacement
    field_b0: int


@dataclass(frozen=True)
class LocationEffectInfoBatch:
    entries: tuple[LocationEffectInfo, ...]
    actor_id: int = 0


def _write_location_effect_info(
    writer: BitWriter, value: LocationEffectInfo
) -> None:
    if value.transform_mode not in (0, 1, 2):
        raise ValueError("LocationEffectInfo transform_mode is invalid")
    expected_scale_count = 3 if value.transform_mode == 0 else 2
    if len(value.scale) != expected_scale_count:
        raise ValueError(
            "LocationEffectInfo scale does not match transform_mode"
        )
    if len(value.position) != 3:
        raise ValueError("LocationEffectInfo position must contain 3 floats")

    writer.write_uint32_le(value.instance_id)
    writer.write_string(value.effect_type)
    writer.write_string(value.instance_guid)
    writer.write_bool(value.enabled)
    writer.write_uint8(value.transform_mode)
    for number in value.position:
        writer.write_float(number)
    writer.write_float(value.yaw)
    for number in value.scale:
        writer.write_float(number)
    writer.write_string(value.effect_resource_id)
    writer.write_bool(value.field_78)
    writer.write_bool(value.field_79)
    writer.write_uint32_le(value.expires_tick)
    writer.write_uint32_le(value.ticks_per_second)
    writer.write_uint32_le(value.starts_tick)
    writer.write_uint32_le(len(value.values))
    for number in value.values:
        writer.write_float(number)
    writer.write_uint32_le(value.strength_percent)
    writer.write_uint32_le(value.owner_actor_id)
    _write_actor_placement(writer, value.placement)
    writer.write_uint32_le(value.field_b0)


def _read_location_effect_info(reader: BitReader) -> LocationEffectInfo:
    instance_id = reader.read_uint32_le()
    effect_type = reader.read_string()
    instance_guid = reader.read_string()
    enabled = reader.read_bool()
    transform_mode = reader.read_uint8()
    if transform_mode not in (0, 1, 2):
        raise ValueError(
            f"unmodeled LocationEffectInfo transform mode {transform_mode}"
        )
    position = tuple(reader.read_float() for _ in range(3))
    yaw = reader.read_float()
    scale = tuple(
        reader.read_float()
        for _ in range(3 if transform_mode == 0 else 2)
    )
    effect_resource_id = reader.read_string()
    field_78 = reader.read_bool()
    field_79 = reader.read_bool()
    expires_tick = reader.read_uint32_le()
    ticks_per_second = reader.read_uint32_le()
    starts_tick = reader.read_uint32_le()
    values = tuple(
        reader.read_float() for _ in range(reader.read_uint32_le())
    )
    strength_percent = reader.read_uint32_le()
    owner_actor_id = reader.read_uint32_le()
    placement = _read_actor_placement(reader)
    field_b0 = reader.read_uint32_le()
    return LocationEffectInfo(
        instance_id,
        effect_type,
        instance_guid,
        enabled,
        transform_mode,
        position,
        yaw,
        scale,
        effect_resource_id,
        field_78,
        field_79,
        expires_tick,
        ticks_per_second,
        starts_tick,
        values,
        strength_percent,
        owner_actor_id,
        placement,
        field_b0,
    )


def encode_location_effect_info_batch(
    value: LocationEffectInfoBatch,
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(LOCATION_EFFECT_INFO_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(len(value.entries))
    for entry in value.entries:
        _write_location_effect_info(writer, entry)
    writer.write_uint32_le(value.actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_location_effect_info_batch(
    payload: bytes,
    bit_length: int,
    *,
    allow_trailing_commands: bool = False,
) -> LocationEffectInfoBatch:
    reader = BitReader(payload, bit_length)
    _start_single_batch(reader, LOCATION_EFFECT_INFO_COMMAND)
    entries = tuple(
        _read_location_effect_info(reader)
        for _ in range(reader.read_uint32_le())
    )
    result = LocationEffectInfoBatch(
        entries, reader.read_uint32_le()
    )
    if reader.read_uint8() != 0xFF:
        raise ValueError("LocationEffectInfo batch terminator is missing")
    if reader.remaining and not allow_trailing_commands:
        raise ValueError("LocationEffectInfo batch has trailing commands")
    return result


def encode_new_location_effect_batch(
    value: LocationEffectInfoBatch,
) -> tuple[bytes, int]:
    """Create native world effects using retail command ``0x003c``.

    Unlike ``LocationEffectInfoCommand``, this command is repeated once per
    entry in a generated server batch.  Each body is the same recovered
    ``LocationEffectInfo`` value followed by the command actor id.
    """

    if not value.entries:
        raise ValueError("NewLocationEffect batch requires at least one entry")
    writer = BitWriter()
    writer.write_uint8(0x85)
    for entry in value.entries:
        writer.write_bytes(NEW_LOCATION_EFFECT_COMMAND.to_bytes(2, "little"))
        _write_location_effect_info(writer, entry)
        writer.write_uint32_le(value.actor_id)
        writer.write_uint8(0xFF)
    return writer.finish()


def decode_new_location_effect_batch(
    payload: bytes,
    bit_length: int,
) -> LocationEffectInfoBatch:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("NewLocationEffect packet is not a server batch")
    entries = []
    actor_id = None
    while reader.remaining:
        command_id = int.from_bytes(reader.read_bytes(2), "little")
        if command_id != NEW_LOCATION_EFFECT_COMMAND:
            raise ValueError(
                f"expected NewLocationEffect, got 0x{command_id:04x}"
            )
        entries.append(_read_location_effect_info(reader))
        current_actor_id = reader.read_uint32_le()
        if actor_id is None:
            actor_id = current_actor_id
        elif actor_id != current_actor_id:
            raise ValueError("NewLocationEffect batch mixes command actors")
        if reader.read_uint8() != 0xFF:
            raise ValueError("NewLocationEffect is not followed by 0xff")
    return LocationEffectInfoBatch(tuple(entries), int(actor_id or 0))


def encode_discard_location_effect_batch(
    instance_ids: tuple[int, ...], *, actor_id: int = 0
) -> tuple[bytes, int]:
    """Terminate native world effects using retail command ``0x003d``."""

    if not instance_ids:
        raise ValueError(
            "DiscardLocationEffect batch requires at least one instance"
        )
    writer = BitWriter()
    writer.write_uint8(0x85)
    for instance_id in instance_ids:
        writer.write_bytes(
            DISCARD_LOCATION_EFFECT_COMMAND.to_bytes(2, "little")
        )
        writer.write_uint32_le(int(instance_id))
        writer.write_uint32_le(int(actor_id))
        writer.write_uint8(0xFF)
    return writer.finish()


def decode_discard_location_effect_batch(
    payload: bytes, bit_length: int
) -> tuple[tuple[int, ...], int]:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("DiscardLocationEffect packet is not a server batch")
    instance_ids = []
    actor_id = None
    while reader.remaining:
        command_id = int.from_bytes(reader.read_bytes(2), "little")
        if command_id != DISCARD_LOCATION_EFFECT_COMMAND:
            raise ValueError(
                f"expected DiscardLocationEffect, got 0x{command_id:04x}"
            )
        instance_ids.append(reader.read_uint32_le())
        current_actor_id = reader.read_uint32_le()
        if actor_id is None:
            actor_id = current_actor_id
        elif actor_id != current_actor_id:
            raise ValueError("DiscardLocationEffect batch mixes command actors")
        if reader.read_uint8() != 0xFF:
            raise ValueError("DiscardLocationEffect is not followed by 0xff")
    return tuple(instance_ids), int(actor_id or 0)


@dataclass(frozen=True)
class TrapInfo:
    """Native ``StatusEffects::TrapInfo`` recovered from this client build."""

    trap_id: int
    resource_id: str
    created_tick: int
    owner_actor_id: int
    transform_mode: int
    position: tuple[float, float, float]
    yaw: float
    scale: tuple[float, ...] = (1.0, 1.0)


def _write_trap_info(writer: BitWriter, value: TrapInfo) -> None:
    if value.transform_mode not in (0, 1, 2):
        raise ValueError("TrapInfo transform_mode is invalid")
    expected_scale_count = 3 if value.transform_mode == 0 else 2
    if len(value.scale) != expected_scale_count:
        raise ValueError("TrapInfo scale does not match transform_mode")
    if len(value.position) != 3:
        raise ValueError("TrapInfo position must contain 3 floats")
    writer.write_uint32_le(value.trap_id)
    writer.write_string(value.resource_id)
    writer.write_uint32_le(value.created_tick)
    writer.write_uint32_le(value.owner_actor_id)
    writer.write_uint8(value.transform_mode)
    for component in value.position:
        writer.write_float(component)
    writer.write_float(value.yaw)
    for component in value.scale:
        writer.write_float(component)


def _read_trap_info(reader: BitReader) -> TrapInfo:
    trap_id = reader.read_uint32_le()
    resource_id = reader.read_string()
    created_tick = reader.read_uint32_le()
    owner_actor_id = reader.read_uint32_le()
    transform_mode = reader.read_uint8()
    if transform_mode not in (0, 1, 2):
        raise ValueError("TrapInfo transform_mode is invalid")
    position = tuple(reader.read_float() for _ in range(3))
    yaw = reader.read_float()
    scale = tuple(
        reader.read_float()
        for _ in range(3 if transform_mode == 0 else 2)
    )
    return TrapInfo(
        trap_id,
        resource_id,
        created_tick,
        owner_actor_id,
        transform_mode,
        position,
        yaw,
        scale,
    )


def encode_new_trap_batch(
    value: TrapInfo,
    *,
    actor_id: int,
    tripped: bool = False,
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(NEW_TRAP_COMMAND.to_bytes(2, "little"))
    _write_trap_info(writer, value)
    writer.write_uint32_le(actor_id)
    writer.write_uint8(0xFF)
    if tripped:
        writer.write_bytes(TRAP_TRIPPED_COMMAND.to_bytes(2, "little"))
        writer.write_uint32_le(value.trap_id)
        writer.write_uint32_le(0)
        writer.write_uint8(0xFF)
    return writer.finish()


def encode_trap_tripped_batch(
    trap_id: int, *, actor_id: int = 0
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(TRAP_TRIPPED_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(trap_id)
    writer.write_uint32_le(actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def encode_discard_trap_batch(
    trap_id: int, *, actor_id: int = 0
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(DISCARD_TRAP_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(trap_id)
    writer.write_uint32_le(actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_new_trap_batch(
    payload: bytes, bit_length: int
) -> tuple[TrapInfo, int, bool]:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("NewTrap packet is not a server batch")
    if int.from_bytes(reader.read_bytes(2), "little") != NEW_TRAP_COMMAND:
        raise ValueError("NewTrap command id is missing")
    value = _read_trap_info(reader)
    actor_id = reader.read_uint32_le()
    if reader.read_uint8() != 0xFF:
        raise ValueError("NewTrap terminator is missing")
    tripped = False
    if reader.remaining:
        if (
            int.from_bytes(reader.read_bytes(2), "little")
            != TRAP_TRIPPED_COMMAND
        ):
            raise ValueError("unexpected command after NewTrap")
        if reader.read_uint32_le() != value.trap_id:
            raise ValueError("TrapTripped references another trap")
        reader.read_uint32_le()
        if reader.read_uint8() != 0xFF:
            raise ValueError("TrapTripped terminator is missing")
        tripped = True
    return value, actor_id, tripped


@dataclass(frozen=True)
class NewNPC:
    template_id: str
    instance_guid: bytes
    placement: ActorPlacement
    rotation: float
    position: tuple[float, float, float]
    enabled: bool
    actor_id: int


@dataclass(frozen=True)
class NPCInteraction:
    source: str
    field_08: int
    target: str
    field_18: int


@dataclass(frozen=True)
class NPCFlag:
    name: str
    enabled: bool


@dataclass(frozen=True)
class NPCStringSet:
    name: str
    values: tuple[str, ...]
    field_28: int
    enabled: bool


@dataclass(frozen=True)
class NPCLink:
    source: str
    target: str
    field_10: int
    enabled: bool


@dataclass(frozen=True)
class NPCInfo:
    template_id: str
    interactions: tuple[NPCInteraction, ...]
    flags: tuple[NPCFlag, ...]
    string_sets: tuple[NPCStringSet, ...]
    links: tuple[NPCLink, ...]
    enabled: bool
    category: int
    actor_id: int


@dataclass(frozen=True)
class NPCBatch:
    commands: tuple[NewNPC | NPCInfo, ...]


def _write_blob16(writer: BitWriter, value: bytes) -> None:
    if len(value) > 0xFFFF:
        raise ValueError("binary field exceeds uint16 length")
    writer.write_bytes(len(value).to_bytes(2, "little"))
    writer.write_bytes(value)


def _read_blob16(reader: BitReader) -> bytes:
    return reader.read_bytes(int.from_bytes(reader.read_bytes(2), "little"))


def _write_new_npc_body(writer: BitWriter, value: NewNPC) -> None:
    writer.write_string(value.template_id)
    _write_blob16(writer, value.instance_guid)
    _write_actor_placement(writer, value.placement)
    writer.write_float(value.rotation)
    for component in value.position:
        writer.write_float(component)
    writer.write_bool(value.enabled)
    writer.write_uint32_le(value.actor_id)


def _read_new_npc_body(reader: BitReader) -> NewNPC:
    template_id = reader.read_string()
    instance_guid = _read_blob16(reader)
    placement = _read_actor_placement(reader)
    rotation = reader.read_float()
    position = tuple(reader.read_float() for _ in range(3))
    enabled = reader.read_bool()
    actor_id = reader.read_uint32_le()
    return NewNPC(
        template_id,
        instance_guid,
        placement,
        rotation,
        position,
        enabled,
        actor_id,
    )


def _write_npc_info_body(writer: BitWriter, value: NPCInfo) -> None:
    writer.write_string(value.template_id)
    writer.write_uint32_le(len(value.interactions))
    for item in value.interactions:
        writer.write_string(item.source)
        writer.write_uint8(item.field_08)
        writer.write_string(item.target)
        writer.write_uint8(item.field_18)
    writer.write_uint32_le(len(value.flags))
    for item in value.flags:
        writer.write_string(item.name)
        writer.write_bool(item.enabled)
    writer.write_uint32_le(len(value.string_sets))
    for item in value.string_sets:
        writer.write_string(item.name)
        if len(item.values) > 0xFFFF:
            raise ValueError("NPCStringSet has too many values")
        writer.write_bytes(len(item.values).to_bytes(2, "little"))
        for entry in item.values:
            writer.write_string(entry)
        writer.write_bytes(int(item.field_28).to_bytes(2, "little"))
        writer.write_bool(item.enabled)
    writer.write_uint32_le(len(value.links))
    for item in value.links:
        writer.write_string(item.source)
        writer.write_string(item.target)
        writer.write_uint32_le(item.field_10)
        writer.write_bool(item.enabled)
    writer.write_bool(value.enabled)
    writer.write_uint8(value.category)
    writer.write_uint32_le(value.actor_id)


def _read_npc_info_body(reader: BitReader) -> NPCInfo:
    template_id = reader.read_string()
    interactions = tuple(
        NPCInteraction(
            reader.read_string(),
            reader.read_uint8(),
            reader.read_string(),
            reader.read_uint8(),
        )
        for _ in range(reader.read_uint32_le())
    )
    flags = tuple(
        NPCFlag(reader.read_string(), reader.read_bool())
        for _ in range(reader.read_uint32_le())
    )
    string_sets = []
    for _ in range(reader.read_uint32_le()):
        name = reader.read_string()
        values = tuple(
            reader.read_string()
            for _ in range(int.from_bytes(reader.read_bytes(2), "little"))
        )
        field_28 = int.from_bytes(reader.read_bytes(2), "little")
        string_sets.append(
            NPCStringSet(name, values, field_28, reader.read_bool())
        )
    links = tuple(
        NPCLink(
            reader.read_string(),
            reader.read_string(),
            reader.read_uint32_le(),
            reader.read_bool(),
        )
        for _ in range(reader.read_uint32_le())
    )
    return NPCInfo(
        template_id,
        interactions,
        flags,
        tuple(string_sets),
        links,
        reader.read_bool(),
        reader.read_uint8(),
        reader.read_uint32_le(),
    )


def encode_npc_batch(
    value: NPCBatch, *, include_actor_vicinity: bool = True,
) -> tuple[bytes, int]:
    if not value.commands:
        raise ValueError("NPC batch must contain at least one command")
    new_npc_ids = tuple(
        int(command.actor_id)
        for command in value.commands
        if isinstance(command, NewNPC)
    )
    writer = BitWriter()
    writer.write_uint8(0x85)
    if new_npc_ids and include_actor_vicinity:
        # The client only picks actors announced in ActorsEnterVicinity.
        # Register every newly-created NPC before its NewNPC/NPCInfo pair.
        writer.write_bytes(ACTORS_ENTER_VICINITY_COMMAND.to_bytes(2, "little"))
        writer.write_uint32_le(len(new_npc_ids))
        for actor_id in new_npc_ids:
            writer.write_uint32_le(actor_id)
        writer.write_uint32_le(0)
        writer.write_uint8(0xFF)
    for index, command in enumerate(value.commands):
        if index > 0:
            writer.write_uint8(0xFF)
        if isinstance(command, NewNPC):
            writer.write_bytes(NEW_NPC_COMMAND.to_bytes(2, "little"))
            _write_new_npc_body(writer, command)
        elif isinstance(command, NPCInfo):
            writer.write_bytes(NPC_INFO_COMMAND.to_bytes(2, "little"))
            _write_npc_info_body(writer, command)
        else:
            raise TypeError(f"unsupported NPC batch command {type(command)!r}")
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_npc_batch(payload: bytes, bit_length: int) -> NPCBatch:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("NewNPC capture is not a server command batch")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id == ACTORS_ENTER_VICINITY_COMMAND:
        actor_count = reader.read_uint32_le()
        if actor_count > 1_000_000:
            raise ValueError("ActorsEnterVicinity actor count is unreasonable")
        for _ in range(actor_count):
            reader.read_uint32_le()
        reader.read_uint32_le()  # actorless command context, zero on retail
        if reader.read_uint8() != 0xFF:
            raise ValueError("ActorsEnterVicinity is not followed by 0xff")
        if not reader.remaining:
            raise ValueError("NPC batch has no commands after actor registration")
        command_id = int.from_bytes(reader.read_bytes(2), "little")
    commands: list[NewNPC | NPCInfo] = []
    while True:
        if command_id == NEW_NPC_COMMAND:
            commands.append(_read_new_npc_body(reader))
        elif command_id == NPC_INFO_COMMAND:
            commands.append(_read_npc_info_body(reader))
        else:
            raise ValueError(f"unmodeled command 0x{command_id:04x} in NPC batch")
        if reader.read_uint8() != 0xFF:
            raise ValueError("NPC batch command is not followed by 0xff")
        if not reader.remaining:
            break
        command_id = int.from_bytes(reader.read_bytes(2), "little")
    return NPCBatch(tuple(commands))


@dataclass(frozen=True)
class MonsterStat:
    kind: int
    value: float


@dataclass(frozen=True)
class NewMonster:
    template_id: str
    stats: tuple[MonsterStat, ...]
    field_88: int
    field_90: int
    field_98: int
    field_9c: int
    field_a0: float
    position: tuple[float, float, float]
    category: int
    field_c4: int
    placement: ActorPlacement
    field_d8: int
    field_dc: bool
    field_e0: int
    field_e4: bool
    rank: str
    field_f0: str
    actor_id: int


@dataclass(frozen=True)
class NewMonsterBatch:
    monster: NewMonster
    status_effect: StatusEffect
    status_actor_id: int


@dataclass(frozen=True)
class NewMonsterServerCommand:
    value: NewMonster


@dataclass(frozen=True)
class MonsterSpawnBatch:
    """An ordered, editable NewMonster/StatusEffect command batch.

    Retail may place multiple monster pairs in one 0x85 application message.
    The legacy :class:`NewMonsterBatch` API remains as a convenience for the
    earlier single-monster capture.
    """

    commands: tuple[NewMonsterServerCommand | StatusEffectServerCommand, ...]


def _write_new_monster_body(writer: BitWriter, value: NewMonster) -> None:
    writer.write_string(value.template_id)
    writer.write_uint32_le(len(value.stats))
    for stat in value.stats:
        writer.write_uint8(stat.kind)
        writer.write_float(stat.value)
    writer.write_uint64_le(value.field_88)
    writer.write_uint64_le(value.field_90)
    writer.write_uint32_le(value.field_98)
    writer.write_uint32_le(value.field_9c)
    writer.write_float(value.field_a0)
    for component in value.position:
        writer.write_float(component)
    writer.write_uint8(value.category)
    writer.write_uint32_le(value.field_c4)
    _write_actor_placement(writer, value.placement)
    writer.write_uint32_le(value.field_d8)
    writer.write_bool(value.field_dc)
    writer.write_uint32_le(value.field_e0)
    writer.write_bool(value.field_e4)
    writer.write_string(value.rank)
    writer.write_string(value.field_f0)
    writer.write_uint32_le(value.actor_id)


def _read_new_monster_body(reader: BitReader) -> NewMonster:
    template_id = reader.read_string()
    stats = tuple(
        MonsterStat(reader.read_uint8(), reader.read_float())
        for _ in range(reader.read_uint32_le())
    )
    return NewMonster(
        template_id,
        stats,
        reader.read_uint64_le(),
        reader.read_uint64_le(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_float(),
        tuple(reader.read_float() for _ in range(3)),
        reader.read_uint8(),
        reader.read_uint32_le(),
        _read_actor_placement(reader),
        reader.read_uint32_le(),
        reader.read_bool(),
        reader.read_uint32_le(),
        reader.read_bool(),
        reader.read_string(),
        reader.read_string(),
        reader.read_uint32_le(),
    )


def encode_monster_spawn_batch(value: MonsterSpawnBatch) -> tuple[bytes, int]:
    if not value.commands:
        raise ValueError("MonsterSpawnBatch requires at least one command")
    writer = BitWriter()
    writer.write_uint8(0x85)
    for index, command in enumerate(value.commands):
        if index:
            writer.write_uint8(0xFF)
        if isinstance(command, NewMonsterServerCommand):
            writer.write_bytes(NEW_MONSTER_COMMAND.to_bytes(2, "little"))
            _write_new_monster_body(writer, command.value)
        elif isinstance(command, StatusEffectServerCommand):
            writer.write_bytes(STATUS_EFFECT_COMMAND.to_bytes(2, "little"))
            _write_status_effect_body(writer, command.value)
            writer.write_uint32_le(command.actor_id)
        else:
            raise TypeError(
                f"unsupported monster spawn command {type(command)!r}"
            )
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_monster_spawn_batch(
    payload: bytes, bit_length: int
) -> MonsterSpawnBatch:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("NewMonster capture is not a server command batch")
    commands = []
    while True:
        command_id = int.from_bytes(reader.read_bytes(2), "little")
        if command_id == NEW_MONSTER_COMMAND:
            command = NewMonsterServerCommand(_read_new_monster_body(reader))
        elif command_id == STATUS_EFFECT_COMMAND:
            command = StatusEffectServerCommand(
                _read_status_effect_body(reader), reader.read_uint32_le()
            )
        else:
            raise ValueError(
                f"unmodeled command 0x{command_id:04x} in monster spawn batch"
            )
        commands.append(command)
        if reader.read_uint8() != 0xFF:
            raise ValueError("monster spawn command is not followed by 0xff")
        if not reader.remaining:
            break
    return MonsterSpawnBatch(tuple(commands))


def encode_discard_monster_batch(
    actor_ids: tuple[int, ...],
) -> tuple[bytes, int]:
    """Remove spawned monster actors that left the player's vicinity."""

    if not actor_ids:
        raise ValueError("DiscardMonster batch requires at least one actor")
    writer = BitWriter()
    writer.write_uint8(0x85)
    for index, actor_id in enumerate(actor_ids):
        if index:
            writer.write_uint8(0xFF)
        writer.write_bytes(DISCARD_MONSTER_COMMAND.to_bytes(2, "little"))
        writer.write_uint32_le(int(actor_id))
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_discard_monster_batch(
    payload: bytes,
    bit_length: int,
) -> tuple[int, ...]:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("DiscardMonster packet is not a server batch")
    actor_ids = []
    while True:
        command_id = int.from_bytes(reader.read_bytes(2), "little")
        if command_id != DISCARD_MONSTER_COMMAND:
            raise ValueError(
                f"expected DiscardMonster, got 0x{command_id:04x}"
            )
        actor_ids.append(reader.read_uint32_le())
        if reader.read_uint8() != 0xFF:
            raise ValueError("DiscardMonster is not followed by 0xff")
        if not reader.remaining:
            break
    return tuple(actor_ids)


def encode_new_monster_batch(value: NewMonsterBatch) -> tuple[bytes, int]:
    return encode_monster_spawn_batch(
        MonsterSpawnBatch(
            (
                NewMonsterServerCommand(value.monster),
                StatusEffectServerCommand(
                    value.status_effect, value.status_actor_id
                ),
            )
        )
    )


def decode_new_monster_batch(
    payload: bytes, bit_length: int
) -> NewMonsterBatch:
    batch = decode_monster_spawn_batch(payload, bit_length)
    if (
        len(batch.commands) != 2
        or not isinstance(batch.commands[0], NewMonsterServerCommand)
        or not isinstance(batch.commands[1], StatusEffectServerCommand)
    ):
        raise ValueError("legacy NewMonsterBatch requires one monster/status pair")
    monster_command, status_command = batch.commands
    return NewMonsterBatch(
        monster_command.value,
        status_command.value,
        status_command.actor_id,
    )


@dataclass(frozen=True)
class GuildMember:
    """One entry in ``GuildStatusCommand``'s generated member array."""

    player_id: int
    field_04: int
    name: str
    # +0x48 is the five-value class enum, +0x54 the validated three-value
    # rank enum; see ``social_protocol.GuildMember``.
    class_id: int
    level: int
    field_50: bool
    field_51: bool
    field_52: bool
    rank: int
    timestamp_parts: tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class GuildStatus:
    status: int
    guild_id: int
    name: str
    server_tick: int
    field_64: bool
    field_68: int
    field_6c: int
    members: tuple[GuildMember, ...]


@dataclass(frozen=True)
class IgnoredStatus:
    status: int
    server_tick: int
    ignored_player_names: tuple[str, ...]


@dataclass(frozen=True)
class GuildStatusBatch:
    guild: GuildStatus
    ignored: IgnoredStatus


def _write_guild_status_body(writer: BitWriter, value: GuildStatus) -> None:
    writer.write_uint8(value.status)
    writer.write_uint32_le(value.guild_id)
    writer.write_string(value.name)
    writer.write_uint32_le(value.server_tick)
    writer.write_bool(value.field_64)
    writer.write_uint32_le(value.field_68)
    writer.write_uint32_le(value.field_6c)
    writer.write_uint32_le(len(value.members))
    for member in value.members:
        if len(member.timestamp_parts) != 6:
            raise ValueError("GuildMember timestamp_parts must contain six uint32s")
        writer.write_uint32_le(member.player_id)
        writer.write_uint32_le(member.field_04)
        writer.write_string(member.name)
        writer.write_uint8(member.class_id)
        writer.write_bytes(int(member.level).to_bytes(2, "little"))
        writer.write_bool(member.field_50)
        writer.write_bool(member.field_51)
        writer.write_bool(member.field_52)
        writer.write_uint8(member.rank)
        for part in member.timestamp_parts:
            writer.write_uint32_le(part)


def _read_guild_status_body(reader: BitReader) -> GuildStatus:
    status = reader.read_uint8()
    guild_id = reader.read_uint32_le()
    name = reader.read_string()
    server_tick = reader.read_uint32_le()
    field_64 = reader.read_bool()
    field_68 = reader.read_uint32_le()
    field_6c = reader.read_uint32_le()
    members = []
    for _ in range(reader.read_uint32_le()):
        members.append(
            GuildMember(
                reader.read_uint32_le(),
                reader.read_uint32_le(),
                reader.read_string(),
                reader.read_uint8(),
                int.from_bytes(reader.read_bytes(2), "little"),
                reader.read_bool(),
                reader.read_bool(),
                reader.read_bool(),
                reader.read_uint8(),
                tuple(reader.read_uint32_le() for _ in range(6)),
            )
        )
    return GuildStatus(
        status,
        guild_id,
        name,
        server_tick,
        field_64,
        field_68,
        field_6c,
        tuple(members),
    )


def _write_ignored_status_body(writer: BitWriter, value: IgnoredStatus) -> None:
    writer.write_uint8(value.status)
    writer.write_uint32_le(value.server_tick)
    writer.write_uint32_le(len(value.ignored_player_names))
    for name in value.ignored_player_names:
        writer.write_string(name)


def _read_ignored_status_body(reader: BitReader) -> IgnoredStatus:
    status = reader.read_uint8()
    server_tick = reader.read_uint32_le()
    names = tuple(reader.read_string() for _ in range(reader.read_uint32_le()))
    return IgnoredStatus(status, server_tick, names)


def encode_guild_status_batch(value: GuildStatusBatch) -> tuple[bytes, int]:
    """Encode the exact retail GuildStatus + IgnoredStatus batch."""

    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(GUILD_STATUS_COMMAND.to_bytes(2, "little"))
    _write_guild_status_body(writer, value.guild)
    writer.write_uint8(0xFF)
    writer.write_bytes(IGNORED_STATUS_COMMAND.to_bytes(2, "little"))
    _write_ignored_status_body(writer, value.ignored)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_guild_status_batch(
    payload: bytes, bit_length: int
) -> GuildStatusBatch:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("GuildStatus capture is not a server command batch")
    if int.from_bytes(reader.read_bytes(2), "little") != GUILD_STATUS_COMMAND:
        raise ValueError("GuildStatus batch has the wrong first command")
    guild = _read_guild_status_body(reader)
    if reader.read_uint8() != 0xFF:
        raise ValueError("GuildStatus is not followed by a command marker")
    if int.from_bytes(reader.read_bytes(2), "little") != IGNORED_STATUS_COMMAND:
        raise ValueError("GuildStatus is not followed by IgnoredStatus")
    ignored = _read_ignored_status_body(reader)
    if reader.read_uint8() != 0xFF or reader.remaining:
        raise ValueError("GuildStatus batch has unaccounted trailing bits")
    return GuildStatusBatch(guild, ignored)


@dataclass(frozen=True)
class OCPOffer:
    offer_id: int
    price: float
    featured: bool
    currency_symbol: str
    product_key: str
    field_b0: str
    expires_at: str


@dataclass(frozen=True)
class OCPMethod:
    name: str
    description: str
    field_80: float
    field_84: float


@dataclass(frozen=True)
class OCPOfferBatch:
    offers: tuple[OCPOffer, ...]
    offer_actor_id: int
    methods: tuple[OCPMethod, ...]
    methods_actor_id: int


def _write_ocp_offer_response_body(
    writer: BitWriter, offers: tuple[OCPOffer, ...]
) -> None:
    writer.write_uint32_le(len(offers))
    for offer in offers:
        writer.write_uint32_le(offer.offer_id)
        writer.write_float(offer.price)
        writer.write_bool(offer.featured)
        writer.write_string(offer.currency_symbol)
        writer.write_string(offer.product_key)
        writer.write_string(offer.field_b0)
        writer.write_string(offer.expires_at)


def _read_ocp_offer_response_body(reader: BitReader) -> tuple[OCPOffer, ...]:
    return tuple(
        OCPOffer(
            reader.read_uint32_le(),
            reader.read_float(),
            reader.read_bool(),
            reader.read_string(),
            reader.read_string(),
            reader.read_string(),
            reader.read_string(),
        )
        for _ in range(reader.read_uint32_le())
    )


def _write_ocp_methods_body(
    writer: BitWriter, methods: tuple[OCPMethod, ...]
) -> None:
    writer.write_uint32_le(len(methods))
    for method in methods:
        writer.write_string(method.name)
        writer.write_string(method.description)
        writer.write_float(method.field_80)
        writer.write_float(method.field_84)


def _read_ocp_methods_body(reader: BitReader) -> tuple[OCPMethod, ...]:
    return tuple(
        OCPMethod(
            reader.read_string(),
            reader.read_string(),
            reader.read_float(),
            reader.read_float(),
        )
        for _ in range(reader.read_uint32_le())
    )


def encode_ocp_offer_response(
    offers: tuple[OCPOffer, ...], *, actor_id: int = 0
) -> tuple[bytes, int]:
    """Serialize a standalone server OCPOfferResponseCommand (0x010e)."""

    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(OCP_OFFER_RESPONSE_COMMAND.to_bytes(2, "little"))
    _write_ocp_offer_response_body(writer, offers)
    writer.write_uint32_le(actor_id)
    return writer.finish()


def decode_ocp_offer_response(
    payload: bytes, bit_length: int
) -> tuple[tuple[OCPOffer, ...], int]:
    """Decode a standalone OCPOfferResponseCommand without retaining bytes."""

    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x84:
        raise ValueError("OCPOfferResponse is not a server application message")
    if int.from_bytes(reader.read_bytes(2), "little") != OCP_OFFER_RESPONSE_COMMAND:
        raise ValueError("OCPOfferResponse has the wrong command ID")
    offers = _read_ocp_offer_response_body(reader)
    actor_id = reader.read_uint32_le()
    if reader.remaining:
        raise ValueError("OCPOfferResponse has unaccounted trailing bits")
    return offers, actor_id


def encode_ocp_methods(
    methods: tuple[OCPMethod, ...], *, actor_id: int = 0
) -> tuple[bytes, int]:
    """Serialize a standalone server OCPMethodsCommand (0x010c)."""

    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(OCP_METHODS_COMMAND.to_bytes(2, "little"))
    _write_ocp_methods_body(writer, methods)
    writer.write_uint32_le(actor_id)
    return writer.finish()


def decode_ocp_methods(
    payload: bytes, bit_length: int
) -> tuple[tuple[OCPMethod, ...], int]:
    """Decode a standalone OCPMethodsCommand without retaining bytes."""

    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x84:
        raise ValueError("OCPMethods is not a server application message")
    if int.from_bytes(reader.read_bytes(2), "little") != OCP_METHODS_COMMAND:
        raise ValueError("OCPMethods has the wrong command ID")
    methods = _read_ocp_methods_body(reader)
    actor_id = reader.read_uint32_le()
    if reader.remaining:
        raise ValueError("OCPMethods has unaccounted trailing bits")
    return methods, actor_id


def encode_ocp_offer_batch(value: OCPOfferBatch) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(OCP_OFFER_RESPONSE_COMMAND.to_bytes(2, "little"))
    _write_ocp_offer_response_body(writer, value.offers)
    writer.write_uint32_le(value.offer_actor_id)
    writer.write_uint8(0xFF)
    writer.write_bytes(OCP_METHODS_COMMAND.to_bytes(2, "little"))
    _write_ocp_methods_body(writer, value.methods)
    writer.write_uint32_le(value.methods_actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_ocp_offer_batch(payload: bytes, bit_length: int) -> OCPOfferBatch:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("OCPOfferResponse capture is not a server command batch")
    if int.from_bytes(reader.read_bytes(2), "little") != OCP_OFFER_RESPONSE_COMMAND:
        raise ValueError("OCPOfferResponse batch has the wrong first command")
    offers = _read_ocp_offer_response_body(reader)
    offer_actor_id = reader.read_uint32_le()
    if reader.read_uint8() != 0xFF:
        raise ValueError("OCPOfferResponse is not followed by a command marker")
    if int.from_bytes(reader.read_bytes(2), "little") != OCP_METHODS_COMMAND:
        raise ValueError("OCPOfferResponse is not followed by OCPMethods")
    methods = _read_ocp_methods_body(reader)
    methods_actor_id = reader.read_uint32_le()
    if reader.read_uint8() != 0xFF or reader.remaining:
        raise ValueError("OCPOfferResponse batch has unaccounted trailing bits")
    return OCPOfferBatch(offers, offer_actor_id, methods, methods_actor_id)


@dataclass(frozen=True)
class ActorStatsServerCommand:
    value: ActorStatsUpdate
    actor_id: int


@dataclass(frozen=True)
class ActorsEnterVicinityBatch:
    value: ActorsEnterVicinity
    actor_id: int
    commands: tuple[StatusEffectServerCommand | ActorStatsServerCommand, ...]


@dataclass(frozen=True)
class MonsterUpdateServerCommand:
    value: MonsterUpdate
    actor_id: int


@dataclass(frozen=True)
class MonsterVicinityBatch:
    """Retail pre-registration state followed by ActorsEnterVicinity."""

    updates: tuple[MonsterUpdateServerCommand, ...]
    vicinity: ActorsEnterVicinity
    vicinity_actor_id: int = 0


def _write_monster_update_body(writer: BitWriter, value: MonsterUpdate) -> None:
    writer.write_uint64_le(value.field_20)
    writer.write_uint64_le(value.field_28)
    writer.write_uint32_le(value.field_30)
    writer.write_uint32_le(value.field_34)
    writer.write_uint32_le(value.field_38)


def _read_monster_update_body(reader: BitReader) -> MonsterUpdate:
    return MonsterUpdate(
        reader.read_uint64_le(),
        reader.read_uint64_le(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
    )


def encode_monster_vicinity_batch(
    value: MonsterVicinityBatch,
) -> tuple[bytes, int]:
    """Encode retail's MonsterUpdate* -> ActorsEnterVicinity transaction."""

    if not value.updates:
        raise ValueError("MonsterVicinityBatch requires at least one update")
    writer = BitWriter()
    writer.write_uint8(0x85)
    for update in value.updates:
        writer.write_bytes(MONSTER_UPDATE_COMMAND.to_bytes(2, "little"))
        _write_monster_update_body(writer, update.value)
        writer.write_uint32_le(update.actor_id)
        writer.write_uint8(0xFF)
    writer.write_bytes(ACTORS_ENTER_VICINITY_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(len(value.vicinity.actor_ids))
    for actor_id in value.vicinity.actor_ids:
        writer.write_uint32_le(actor_id)
    writer.write_uint32_le(value.vicinity_actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_monster_vicinity_batch(
    payload: bytes, bit_length: int
) -> MonsterVicinityBatch:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("MonsterVicinityBatch is not a server batch")
    updates = []
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    while command_id == MONSTER_UPDATE_COMMAND:
        updates.append(
            MonsterUpdateServerCommand(
                _read_monster_update_body(reader),
                reader.read_uint32_le(),
            )
        )
        if reader.read_uint8() != 0xFF:
            raise ValueError("MonsterUpdate is not followed by 0xff")
        command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != ACTORS_ENTER_VICINITY_COMMAND:
        raise ValueError(
            "MonsterUpdate sequence is not followed by ActorsEnterVicinity"
        )
    vicinity = ActorsEnterVicinity(
        tuple(reader.read_uint32_le() for _ in range(reader.read_uint32_le()))
    )
    vicinity_actor_id = reader.read_uint32_le()
    if reader.read_uint8() != 0xFF or reader.remaining:
        raise ValueError("MonsterVicinityBatch has trailing data")
    return MonsterVicinityBatch(
        tuple(updates), vicinity, vicinity_actor_id
    )


def _write_actor_stats_body(writer: BitWriter, value: ActorStatsUpdate) -> None:
    writer.write_uint64_le(value.stat_id)
    writer.write_float(value.value)


def _read_actor_stats_body(reader: BitReader) -> ActorStatsUpdate:
    return ActorStatsUpdate(reader.read_uint64_le(), reader.read_float())


def encode_actors_enter_vicinity_batch(
    value: ActorsEnterVicinityBatch,
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(ACTORS_ENTER_VICINITY_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(len(value.value.actor_ids))
    for actor_id in value.value.actor_ids:
        writer.write_uint32_le(actor_id)
    writer.write_uint32_le(value.actor_id)
    for command in value.commands:
        writer.write_uint8(0xFF)
        if isinstance(command, StatusEffectServerCommand):
            writer.write_bytes(STATUS_EFFECT_COMMAND.to_bytes(2, "little"))
            _write_status_effect_body(writer, command.value)
        elif isinstance(command, ActorStatsServerCommand):
            writer.write_bytes(ACTOR_STATS_UPDATE_COMMAND.to_bytes(2, "little"))
            _write_actor_stats_body(writer, command.value)
        else:
            raise TypeError(f"unsupported vicinity command {type(command)!r}")
        writer.write_uint32_le(command.actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_actors_enter_vicinity_batch(
    payload: bytes, bit_length: int
) -> ActorsEnterVicinityBatch:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("ActorsEnterVicinity capture is not a batch")
    if int.from_bytes(reader.read_bytes(2), "little") != ACTORS_ENTER_VICINITY_COMMAND:
        raise ValueError("ActorsEnterVicinity batch has the wrong first command")
    value = ActorsEnterVicinity(
        tuple(reader.read_uint32_le() for _ in range(reader.read_uint32_le()))
    )
    actor_id = reader.read_uint32_le()
    commands = []
    while True:
        if reader.read_uint8() != 0xFF:
            raise ValueError("vicinity command is not followed by 0xff")
        if not reader.remaining:
            break
        command_id = int.from_bytes(reader.read_bytes(2), "little")
        if command_id == STATUS_EFFECT_COMMAND:
            command = StatusEffectServerCommand(
                _read_status_effect_body(reader), reader.read_uint32_le()
            )
        elif command_id == ACTOR_STATS_UPDATE_COMMAND:
            command = ActorStatsServerCommand(
                _read_actor_stats_body(reader), reader.read_uint32_le()
            )
        else:
            raise ValueError(
                f"unmodeled command 0x{command_id:04x} in vicinity batch"
            )
        commands.append(command)
    return ActorsEnterVicinityBatch(value, actor_id, tuple(commands))


@dataclass(frozen=True)
class FactionInfo:
    placement: ActorPlacement


@dataclass(frozen=True)
class MonsterUpdateBatch:
    monster: MonsterUpdate
    monster_actor_id: int
    faction: FactionInfo
    faction_actor_id: int
    commands: tuple[StatusEffectServerCommand, ...] = ()


def encode_monster_update_batch(value: MonsterUpdateBatch) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(MONSTER_UPDATE_COMMAND.to_bytes(2, "little"))
    writer.write_uint64_le(value.monster.field_20)
    writer.write_uint64_le(value.monster.field_28)
    writer.write_uint32_le(value.monster.field_30)
    writer.write_uint32_le(value.monster.field_34)
    writer.write_uint32_le(value.monster.field_38)
    writer.write_uint32_le(value.monster_actor_id)
    writer.write_uint8(0xFF)
    writer.write_bytes(FACTION_INFO_COMMAND.to_bytes(2, "little"))
    _write_actor_placement(writer, value.faction.placement)
    writer.write_uint32_le(value.faction_actor_id)
    for command in value.commands:
        if not isinstance(command, StatusEffectServerCommand):
            raise TypeError(
                "MonsterUpdateBatch only supports trailing StatusEffect commands"
            )
        writer.write_uint8(0xFF)
        writer.write_bytes(STATUS_EFFECT_COMMAND.to_bytes(2, "little"))
        _write_status_effect_body(writer, command.value)
        writer.write_uint32_le(command.actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_monster_update_batch(
    payload: bytes, bit_length: int
) -> MonsterUpdateBatch:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("MonsterUpdate capture is not a batch")
    if int.from_bytes(reader.read_bytes(2), "little") != MONSTER_UPDATE_COMMAND:
        raise ValueError("MonsterUpdate batch has the wrong first command")
    monster = MonsterUpdate(
        reader.read_uint64_le(),
        reader.read_uint64_le(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
    )
    monster_actor_id = reader.read_uint32_le()
    if reader.read_uint8() != 0xFF:
        raise ValueError("MonsterUpdate is not followed by a marker")
    if int.from_bytes(reader.read_bytes(2), "little") != FACTION_INFO_COMMAND:
        raise ValueError("MonsterUpdate is not followed by FactionInfo")
    faction = FactionInfo(_read_actor_placement(reader))
    faction_actor_id = reader.read_uint32_le()
    commands = []
    if reader.read_uint8() != 0xFF:
        raise ValueError("FactionInfo is not followed by a marker")
    while reader.remaining:
        command_id = int.from_bytes(reader.read_bytes(2), "little")
        if command_id != STATUS_EFFECT_COMMAND:
            raise ValueError(
                f"unmodeled command 0x{command_id:04x} "
                "in MonsterUpdate batch"
            )
        commands.append(
            StatusEffectServerCommand(
                _read_status_effect_body(reader),
                reader.read_uint32_le(),
            )
        )
        if reader.read_uint8() != 0xFF:
            raise ValueError(
                "MonsterUpdate trailing command is not followed by a marker"
            )
    return MonsterUpdateBatch(
        monster,
        monster_actor_id,
        faction,
        faction_actor_id,
        tuple(commands),
    )


@dataclass(frozen=True)
class PropInfoServerCommand:
    value: PropInfo
    actor_id: int


@dataclass(frozen=True)
class GauntletPropBatch:
    notification: GauntletNotification
    notification_actor_id: int
    props: tuple[PropInfoServerCommand, ...]


def _write_prop_info_body(writer: BitWriter, value: PropInfo) -> None:
    writer.write_uint32_le(len(value.entries))
    for entry in value.entries:
        writer.write_uint32_le(entry.prop_id)
        writer.write_string(entry.template_id)
        _write_blob16(writer, entry.instance_guid)
        writer.write_uint32_le(entry.field_20)
        writer.write_uint8(entry.field_24)
        writer.write_uint32_le(entry.field_28)
        for component in entry.position:
            writer.write_float(component)
        writer.write_float(entry.rotation)
        writer.write_string(entry.field_48)
        writer.write_string(entry.field_50)
        writer.write_uint8(entry.field_58)
        writer.write_uint8(entry.field_5c)
        writer.write_uint32_le(entry.field_60)


def _read_prop_info_body(reader: BitReader) -> PropInfo:
    entries = []
    for _ in range(reader.read_uint32_le()):
        entries.append(
            PropInfoEntry(
                reader.read_uint32_le(),
                reader.read_string(),
                _read_blob16(reader),
                reader.read_uint32_le(),
                reader.read_uint8(),
                reader.read_uint32_le(),
                tuple(reader.read_float() for _ in range(3)),
                reader.read_float(),
                reader.read_string(),
                reader.read_string(),
                reader.read_uint8(),
                reader.read_uint8(),
                reader.read_uint32_le(),
            )
        )
    return PropInfo(tuple(entries))


def encode_gauntlet_prop_batch(value: GauntletPropBatch) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(GAUNTLET_NOTIFICATION_RESPONSE_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(value.notification.category)
    writer.write_uint32_le(value.notification.notification)
    writer.write_uint32_le(value.notification_actor_id)
    for command in value.props:
        writer.write_uint8(0xFF)
        writer.write_bytes(PROP_INFO_COMMAND.to_bytes(2, "little"))
        _write_prop_info_body(writer, command.value)
        writer.write_uint32_le(command.actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_gauntlet_prop_batch(
    payload: bytes, bit_length: int
) -> GauntletPropBatch:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("Gauntlet response capture is not a batch")
    if int.from_bytes(reader.read_bytes(2), "little") != GAUNTLET_NOTIFICATION_RESPONSE_COMMAND:
        raise ValueError("Gauntlet response batch has the wrong first command")
    notification = GauntletNotification(
        reader.read_uint32_le(), reader.read_uint32_le()
    )
    notification_actor_id = reader.read_uint32_le()
    props = []
    while True:
        if reader.read_uint8() != 0xFF:
            raise ValueError("Gauntlet batch command is not followed by 0xff")
        if not reader.remaining:
            break
        if int.from_bytes(reader.read_bytes(2), "little") != PROP_INFO_COMMAND:
            raise ValueError("Gauntlet response contains a non-PropInfo command")
        props.append(
            PropInfoServerCommand(_read_prop_info_body(reader), reader.read_uint32_le())
        )
    return GauntletPropBatch(notification, notification_actor_id, tuple(props))


def decode_simple_gameplay_command(payload: bytes):
    """Decode every non-compound client command in the retail map capture."""

    if len(payload) < 3 or payload[0] != 0x8B:
        raise ValueError("not a client command")
    command_id = int.from_bytes(payload[1:3], "little")
    if command_id in (
        ACTOR_REQUEST_COMMAND,
        NPC_REQUEST_COMMAND,
        ENCOUNTER_COMMAND,
        STILL_ALIVE_COMMAND,
        GUILD_MOTD_REQUEST_COMMAND,
    ):
        return decode_uint32_command(payload, command_id)
    if command_id == PROP_REQUEST_COMMAND:
        return decode_fieldless_command(payload, command_id)
    if command_id == CHAT_CLIENT_LOGGED_IN_COMMAND:
        return decode_chat_client_logged_in(payload)
    if command_id == OCP_OFFER_REQUEST_COMMAND:
        return decode_boolean_command(payload, command_id)
    if command_id == STAGING_SIGNAL_COMMAND:
        return decode_staging_signal(payload)
    if command_id == USE_STICKER_BOOK_ITEM_COMMAND:
        return decode_string_command(payload, command_id)
    if command_id == SET_STICKER_BOOK_FAVORITE_COMMAND:
        return decode_set_stickerbook_favorite(payload)
    if command_id == ADD_STICKER_BOOK_FROM_INVENTORY_COMMAND:
        return decode_add_stickerbook_from_inventory(payload)
    if command_id == UPGRADE_STICKER_BOOK_ITEM_COMMAND:
        return decode_upgrade_stickerbook_item(payload)
    if command_id == QUICK_SLOTS_COMMAND:
        return decode_quick_slots(payload)
    if command_id == INVENTORY_ITEM_ACTION_COMMAND:
        return decode_inventory_item_action(payload)
    if command_id == UNLOCK_MAP_COMMAND:
        return decode_unlock_map_request(payload)
    if command_id == MAP_TRAVEL_COMMAND:
        return decode_map_travel_request(payload)
    if command_id == USE_TRAVEL_ITEM_COMMAND:
        return decode_use_travel_item_request(payload)
    if command_id == CLIENT_LOG_EVENT_COMMAND:
        return decode_client_log_event(payload)
    if command_id == CLIENT_AVERAGE_FRAME_INFO_COMMAND:
        return decode_client_average_frame_info(payload)
    if command_id == CLIENT_CURRENT_POSITION_FRAME_INFO_COMMAND:
        return decode_client_position_frame_info(payload)
    raise ValueError(f"command 0x{command_id:04x} is not a simple command")


@dataclass(frozen=True)
class MoveServerCommand:
    value: Move
    actor_id: int


@dataclass(frozen=True)
class MoveBatch:
    commands: tuple[MoveServerCommand, ...]


def encode_move_batch(value: MoveBatch) -> tuple[bytes, int]:
    """Serialize every MoveCommand in a retail 0x85 command batch."""

    if not value.commands:
        raise ValueError("MoveBatch requires at least one command")
    writer = BitWriter()
    writer.write_uint8(0x85)
    for index, command in enumerate(value.commands):
        if index:
            writer.write_uint8(0xFF)
        writer.write_bytes(MOVE_COMMAND.to_bytes(2, "little"))
        writer.write_bytes(command.value.encode_body())
        writer.write_uint32_le(command.actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_move_batch(payload: bytes, bit_length: int) -> MoveBatch:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("MoveBatch is not a server command batch")
    commands = []
    while True:
        if int.from_bytes(reader.read_bytes(2), "little") != MOVE_COMMAND:
            raise ValueError("MoveBatch contains a non-Move command")
        move = Move(*struct.unpack("<hhhBBB I H", reader.read_bytes(15)))
        commands.append(MoveServerCommand(move, reader.read_uint32_le()))
        if reader.read_uint8() != 0xFF:
            raise ValueError("MoveCommand is not followed by 0xff")
        if not reader.remaining:
            break
    return MoveBatch(tuple(commands))


def _start_single_batch(reader: BitReader, command_id: int) -> None:
    if reader.read_uint8() != 0x85:
        raise ValueError("message is not a server command batch")
    actual = int.from_bytes(reader.read_bytes(2), "little")
    if actual != command_id:
        raise ValueError(
            f"expected batch command 0x{command_id:04x}, got 0x{actual:04x}"
        )


def _finish_single_batch(reader: BitReader) -> None:
    if reader.read_uint8() != 0xFF or reader.remaining:
        raise ValueError("single-command batch has trailing data")


@dataclass(frozen=True)
class ForceTransform:
    """``Commands::ForceTransformCommand`` (0x0071): move an actor outright.

    Three position floats and a heading, then the actor id the server-command
    wrapper carries. The battleground uses it the way retail does -- to move
    each side out of its staging entry and into the arena when a round starts.
    The recorded countdown batch carried
    ``(14.0, 0.0, 45.0, -1.643359899520874)`` for actor 65536, which is
    ``BlueTeamRespawn`` on ``b1102_dm`` exactly, heading included.
    """

    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    heading: float = 0.0


def encode_force_transform_batch(
    value: ForceTransform, *, actor_id: int
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(FORCE_TRANSFORM_COMMAND.to_bytes(2, "little"))
    for axis in range(3):
        writer.write_float(float(value.position[axis]))
    writer.write_float(float(value.heading))
    writer.write_uint32_le(int(actor_id))
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_force_transform_batch(
    payload: bytes, bit_length: int
) -> tuple[ForceTransform, int]:
    reader = BitReader(payload, bit_length)
    _start_single_batch(reader, FORCE_TRANSFORM_COMMAND)
    position = tuple(reader.read_float() for _ in range(3))
    value = ForceTransform(position, reader.read_float())
    actor_id = reader.read_uint32_le()
    _finish_single_batch(reader)
    return value, actor_id


def encode_conversion_story_info_batch(
    value: ConversionStoryInfo, *, actor_id: int = 0
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(CONVERSION_STORY_INFO_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(value.field_00)
    writer.write_uint32_le(value.server_tick)
    writer.write_string(value.story_time)
    writer.write_uint8(value.field_08)
    writer.write_uint32_le(actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_conversion_story_info_batch(
    payload: bytes, bit_length: int
) -> tuple[ConversionStoryInfo, int]:
    reader = BitReader(payload, bit_length)
    _start_single_batch(reader, CONVERSION_STORY_INFO_COMMAND)
    value = ConversionStoryInfo(
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_string(),
        reader.read_uint8(),
    )
    actor_id = reader.read_uint32_le()
    _finish_single_batch(reader)
    return value, actor_id


def encode_message_batch(
    value: Message, *, actor_id: int
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(MESSAGE_COMMAND.to_bytes(2, "little"))
    writer.write_uint8(value.message_type)
    writer.write_uint32_le(len(value.arguments))
    for argument in value.arguments:
        _write_message_argument(writer, argument)
    writer.write_uint32_le(actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def encode_maintenance_countdown(seconds: int) -> tuple[bytes, int]:
    """Start the native maintenance timer with a remaining duration in seconds.

    ClientGameWorld::HandleMessageCommand (0x1401c842c) handles message type
    zero by reading integer argument zero and restarting its elapsed timer.
    Its update routine (0x1401d39b0) displays masterServerShutdownMessageMin.
    ACP shutdown commands are not the client-facing countdown notification.
    """

    if type(seconds) is not int or not 0 <= seconds <= 0x7FFFFFFF:
        raise ValueError("maintenance seconds must be a non-negative int32")
    return encode_message_batch(
        Message(0, (MessageArgument(1, seconds),)), actor_id=0,
    )


def encode_screen_message(text: str) -> tuple[bytes, int]:
    """Queue a nonmodal message in the native MessageWidget.

    ServerToClientMsg's decoder (0x14093e6f4) reads an int32 and two
    strings, followed by the inherited actor id. ClientMessageManager
    (0x1401d9978) routes type 0 to MessageWidget; types 1/2 open dialogs.
    """
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(SERVER_TO_CLIENT_MSG_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(0)
    writer.write_string(text)
    writer.write_string("")
    writer.write_uint32_le(0)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_message_batch(
    payload: bytes, bit_length: int
) -> tuple[Message, int]:
    reader = BitReader(payload, bit_length)
    _start_single_batch(reader, MESSAGE_COMMAND)
    message_type = reader.read_uint8()
    arguments = [
        _read_message_argument(reader)
        for _ in range(reader.read_uint32_le())
    ]
    value = Message(message_type, tuple(arguments))
    actor_id = reader.read_uint32_le()
    _finish_single_batch(reader)
    return value, actor_id


def encode_status_effect_batch(
    value: StatusEffect, *, actor_id: int
) -> tuple[bytes, int]:
    return encode_status_effect_command_batch(
        StatusEffectCommandBatch(
            (StatusEffectServerCommand(value, int(actor_id)),)
        )
    )


def encode_status_effect_command_batch(
    value: StatusEffectCommandBatch,
) -> tuple[bytes, int]:
    """Encode actor-scoped effects without one reliable frame per actor.

    Each command retains its own trailing actor id.  The 0xff byte separates
    adjacent commands and also terminates the final command, matching the
    retail multi-command envelope used by monster spawn/update transactions.
    """

    if not value.commands:
        raise ValueError("StatusEffectCommandBatch requires at least one command")
    writer = BitWriter()
    writer.write_uint8(0x85)
    for index, command in enumerate(value.commands):
        if not isinstance(command, StatusEffectServerCommand):
            raise TypeError(
                "StatusEffectCommandBatch only supports "
                "StatusEffectServerCommand values"
            )
        if index:
            writer.write_uint8(0xFF)
        writer.write_bytes(STATUS_EFFECT_COMMAND.to_bytes(2, "little"))
        _write_status_effect_body(writer, command.value)
        writer.write_uint32_le(command.actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_status_effect_batch(
    payload: bytes, bit_length: int
) -> tuple[StatusEffect, int]:
    batch = decode_status_effect_command_batch(payload, bit_length)
    if len(batch.commands) != 1:
        raise ValueError("single StatusEffect batch contains multiple commands")
    command = batch.commands[0]
    return command.value, command.actor_id


def decode_status_effect_command_batch(
    payload: bytes, bit_length: int
) -> StatusEffectCommandBatch:
    """Decode a status-only 0x85 command batch and all actor contexts."""

    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("StatusEffectCommandBatch is not a server batch")
    commands = []
    while True:
        command_id = int.from_bytes(reader.read_bytes(2), "little")
        if command_id != STATUS_EFFECT_COMMAND:
            raise ValueError(
                "StatusEffectCommandBatch contains non-StatusEffect "
                f"command 0x{command_id:04x}"
            )
        commands.append(
            StatusEffectServerCommand(
                _read_status_effect_body(reader),
                reader.read_uint32_le(),
            )
        )
        if reader.read_uint8() != 0xFF:
            raise ValueError("StatusEffect command is not followed by 0xff")
        if not reader.remaining:
            break
    return StatusEffectCommandBatch(tuple(commands))


def encode_prop_info_batch(
    value: PropInfo, *, actor_id: int
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(PROP_INFO_COMMAND.to_bytes(2, "little"))
    _write_prop_info_body(writer, value)
    writer.write_uint32_le(actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_prop_info_batch(
    payload: bytes, bit_length: int
) -> tuple[PropInfo, int]:
    reader = BitReader(payload, bit_length)
    _start_single_batch(reader, PROP_INFO_COMMAND)
    value = _read_prop_info_body(reader)
    actor_id = reader.read_uint32_le()
    _finish_single_batch(reader)
    return value, actor_id


def encode_actor_stats_update_batch(
    value: ActorStatsUpdate, *, actor_id: int
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(ACTOR_STATS_UPDATE_COMMAND.to_bytes(2, "little"))
    _write_actor_stats_body(writer, value)
    writer.write_uint32_le(actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_actor_stats_update_batch(
    payload: bytes, bit_length: int
) -> tuple[ActorStatsUpdate, int]:
    reader = BitReader(payload, bit_length)
    _start_single_batch(reader, ACTOR_STATS_UPDATE_COMMAND)
    value = _read_actor_stats_body(reader)
    actor_id = reader.read_uint32_le()
    _finish_single_batch(reader)
    return value, actor_id


def encode_empty_player_query_result_batch(query_id: int) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(PLAYER_QUERY_RESULT_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(query_id)
    writer.write_uint32_le(0)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_empty_player_query_result_batch(
    payload: bytes, bit_length: int
) -> int:
    reader = BitReader(payload, bit_length)
    _start_single_batch(reader, PLAYER_QUERY_RESULT_COMMAND)
    query_id = reader.read_uint32_le()
    if reader.read_uint32_le() != 0:
        raise ValueError("non-empty PlayerQueryResult branch is not modeled")
    _finish_single_batch(reader)
    return query_id
