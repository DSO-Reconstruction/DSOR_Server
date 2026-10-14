"""Prop lifecycle and trigger-report commands.

Layouts follow ``docs/PROP_AND_TRIGGER_COMMANDS.md``: ``NewProp``,
``DiscardProp`` and ``PropUpdate`` are server-to-client, actor-scoped and
batched as ``0x85`` messages; ``PropInteraction`` and ``QuestTriggerSignal``
are client-to-server ``0x8B`` commands that carry exactly their decoder's
fields and no trailing actor.
"""

from __future__ import annotations

from dataclasses import dataclass

from dso_server.protocol.character_selection import BitReader, BitWriter
from dso_server.protocol.command_ids import command_id


NEW_PROP_COMMAND = command_id("Commands::NewPropCommand", 0x0034)
DISCARD_PROP_COMMAND = command_id("Commands::DiscardPropCommand", 0x0035)
PROP_UPDATE_COMMAND = command_id("Commands::PropUpdateCommand", 0x0037)
PROP_INTERACTION_COMMAND = command_id(
    "Commands::PropInteractionCommand", 0x0039
)
QUEST_TRIGGER_SIGNAL_COMMAND = command_id(
    "Commands::QuestTriggerSignalCommand", 0x0125
)

# ``Game::PropState::Id`` as the client's range-checked enum8 reader accepts
# it: anything outside -1..3 makes the client reject the whole command and
# desynchronizes every command batched behind it.
PROP_STATE_INACTIVE = 0
PROP_STATE_ACTIVE = 1
PROP_STATE_COOLDOWN = 2
# The second inactive alias; retail's PropUpdate for a used one-shot prop
# carried this value.
PROP_STATE_USED = 3
PROP_STATE_NONE = -1
ZERO_GUID = bytes(16)


def _write_int32(writer: BitWriter, value: int) -> None:
    writer.write_uint32_le(int(value) & 0xFFFFFFFF)


def _read_int32(reader: BitReader) -> int:
    value = reader.read_uint32_le()
    return value - 0x100000000 if value & 0x80000000 else value


def _write_enum8(writer: BitWriter, value: int, *, low: int, high: int) -> None:
    value = int(value)
    if not low <= value <= high:
        raise ValueError(f"enum value {value} outside {low}..{high}")
    writer.write_uint8(value & 0xFF)


def _read_enum8(reader: BitReader) -> int:
    value = reader.read_uint8()
    return value - 0x100 if value & 0x80 else value


def _write_guid(writer: BitWriter, value: bytes) -> None:
    if len(value) != 16:
        raise ValueError("prop instance guid must be 16 bytes")
    writer.write_bytes((16).to_bytes(2, "little"))
    writer.write_bytes(value)


def _read_guid(reader: BitReader) -> bytes:
    size = int.from_bytes(reader.read_bytes(2), "little")
    return reader.read_bytes(size)


@dataclass(frozen=True)
class NewProp:
    prop_id: int
    template_id: str
    instance_guid: bytes
    position: tuple[float, float, float]
    rotation: float
    state: int = PROP_STATE_ACTIVE
    variant: int = -1
    counter: int = 0
    operation: str = ""
    quest_id: str = ""
    task_index: int = -1
    quest_indication: int = -1


@dataclass(frozen=True)
class PropUpdate:
    prop_id: int
    state: int
    actor_id: int = 0
    variant: int = -1
    counter: int = 0
    state_changed: bool = True
    operation: str = ""
    quest_id: str = ""
    task_index: int = -1
    quest_indication: int = -1
    unknown_c0: int = 0
    unknown_c4: float = 0.0
    operation_changed: bool = False
    unknown_cc: int = 0
    unknown_d0: int = 0
    keep_visual_state: bool = False


@dataclass(frozen=True)
class PropInteraction:
    prop_id: int
    operation: str


@dataclass(frozen=True)
class QuestTriggerSignal:
    trigger_id: str


def _write_new_prop_body(writer: BitWriter, value: NewProp) -> None:
    writer.write_uint32_le(int(value.prop_id))
    writer.write_string(value.template_id)
    _write_guid(writer, bytes(value.instance_guid))
    _write_int32(writer, value.variant)
    _write_enum8(writer, value.state, low=-1, high=3)
    writer.write_uint32_le(int(value.counter))
    writer.write_string(value.operation)
    writer.write_string(value.quest_id)
    _write_int32(writer, value.task_index)
    _write_enum8(writer, value.quest_indication, low=-1, high=13)
    for component in value.position:
        writer.write_float(float(component))
    writer.write_float(float(value.rotation))


def _read_new_prop_body(reader: BitReader) -> NewProp:
    prop_id = reader.read_uint32_le()
    template_id = reader.read_string()
    instance_guid = _read_guid(reader)
    variant = _read_int32(reader)
    state = _read_enum8(reader)
    counter = reader.read_uint32_le()
    operation = reader.read_string()
    quest_id = reader.read_string()
    task_index = _read_int32(reader)
    quest_indication = _read_enum8(reader)
    position = tuple(reader.read_float() for _ in range(3))
    rotation = reader.read_float()
    return NewProp(
        prop_id=prop_id,
        template_id=template_id,
        instance_guid=instance_guid,
        position=position,
        rotation=rotation,
        state=state,
        variant=variant,
        counter=counter,
        operation=operation,
        quest_id=quest_id,
        task_index=task_index,
        quest_indication=quest_indication,
    )


def _write_prop_update_body(writer: BitWriter, value: PropUpdate) -> None:
    writer.write_uint32_le(int(value.prop_id))
    _write_int32(writer, value.variant)
    writer.write_uint32_le(int(value.actor_id))
    _write_enum8(writer, value.state, low=-1, high=3)
    writer.write_uint32_le(int(value.counter))
    writer.write_bool(bool(value.state_changed))
    writer.write_string(value.operation)
    writer.write_string(value.quest_id)
    _write_int32(writer, value.task_index)
    _write_enum8(writer, value.quest_indication, low=-1, high=13)
    writer.write_uint32_le(int(value.unknown_c0))
    writer.write_float(float(value.unknown_c4))
    writer.write_bool(bool(value.operation_changed))
    _write_int32(writer, value.unknown_cc)
    writer.write_uint32_le(int(value.unknown_d0))
    writer.write_bool(bool(value.keep_visual_state))


def _read_prop_update_body(reader: BitReader) -> PropUpdate:
    prop_id = reader.read_uint32_le()
    variant = _read_int32(reader)
    actor_id = reader.read_uint32_le()
    state = _read_enum8(reader)
    counter = reader.read_uint32_le()
    state_changed = reader.read_bool()
    operation = reader.read_string()
    quest_id = reader.read_string()
    task_index = _read_int32(reader)
    quest_indication = _read_enum8(reader)
    unknown_c0 = reader.read_uint32_le()
    unknown_c4 = reader.read_float()
    operation_changed = reader.read_bool()
    unknown_cc = _read_int32(reader)
    unknown_d0 = reader.read_uint32_le()
    keep_visual_state = reader.read_bool()
    return PropUpdate(
        prop_id=prop_id,
        state=state,
        actor_id=actor_id,
        variant=variant,
        counter=counter,
        state_changed=state_changed,
        operation=operation,
        quest_id=quest_id,
        task_index=task_index,
        quest_indication=quest_indication,
        unknown_c0=unknown_c0,
        unknown_c4=unknown_c4,
        operation_changed=operation_changed,
        unknown_cc=unknown_cc,
        unknown_d0=unknown_d0,
        keep_visual_state=keep_visual_state,
    )


def _encode_actor_batch(entries, writer_for_body, command: int, actor_id: int):
    entries = tuple(entries)
    if not entries:
        raise ValueError("prop batch requires at least one command")
    writer = BitWriter()
    writer.write_uint8(0x85)
    for index, entry in enumerate(entries):
        if index:
            writer.write_uint8(0xFF)
        writer.write_bytes(int(command).to_bytes(2, "little"))
        writer_for_body(writer, entry)
        writer.write_uint32_le(int(actor_id))
    writer.write_uint8(0xFF)
    return writer.finish()


def _decode_actor_batch(payload: bytes, bit_length: int, reader_for_body, command: int):
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("prop packet is not a server command batch")
    entries = []
    while True:
        found = int.from_bytes(reader.read_bytes(2), "little")
        if found != command:
            raise ValueError(
                f"expected command 0x{command:04x}, got 0x{found:04x}"
            )
        value = reader_for_body(reader)
        actor_id = reader.read_uint32_le()
        entries.append((value, actor_id))
        if reader.read_uint8() != 0xFF:
            raise ValueError("prop command is not followed by 0xff")
        if not reader.remaining:
            break
    return tuple(entries)


def encode_new_prop_batch(
    props: tuple[NewProp, ...], *, actor_id: int,
) -> tuple[bytes, int]:
    """Create props mid-map; the client ignores ids it already knows."""

    return _encode_actor_batch(props, _write_new_prop_body, NEW_PROP_COMMAND, actor_id)


def decode_new_prop_batch(
    payload: bytes, bit_length: int,
) -> tuple[tuple[NewProp, int], ...]:
    return _decode_actor_batch(payload, bit_length, _read_new_prop_body, NEW_PROP_COMMAND)


def encode_discard_prop_batch(
    prop_ids: tuple[int, ...], *, actor_id: int,
) -> tuple[bytes, int]:
    return _encode_actor_batch(
        tuple(int(value) for value in prop_ids),
        lambda writer, prop_id: writer.write_uint32_le(prop_id),
        DISCARD_PROP_COMMAND,
        actor_id,
    )


def decode_discard_prop_batch(
    payload: bytes, bit_length: int,
) -> tuple[tuple[int, int], ...]:
    return _decode_actor_batch(
        payload, bit_length,
        lambda reader: reader.read_uint32_le(),
        DISCARD_PROP_COMMAND,
    )


def encode_prop_update_batch(
    updates: tuple[PropUpdate, ...], *, actor_id: int,
) -> tuple[bytes, int]:
    return _encode_actor_batch(
        updates, _write_prop_update_body, PROP_UPDATE_COMMAND, actor_id
    )


def decode_prop_update_batch(
    payload: bytes, bit_length: int,
) -> tuple[tuple[PropUpdate, int], ...]:
    return _decode_actor_batch(
        payload, bit_length, _read_prop_update_body, PROP_UPDATE_COMMAND
    )


def _client_reader(payload: bytes, command: int, bit_length: int | None) -> BitReader:
    reader = BitReader(
        payload, len(payload) * 8 if bit_length is None else int(bit_length)
    )
    if reader.read_uint8() != 0x8B:
        raise ValueError("expected a client application message (0x8b)")
    found = int.from_bytes(reader.read_bytes(2), "little")
    if found != command:
        raise ValueError(
            f"expected command 0x{command:04x}, got 0x{found:04x}"
        )
    return reader


def _check_padding(reader: BitReader, name: str) -> None:
    # Datagrams are byte aligned; the bits after the last field must be the
    # zero padding of the final byte, never another field.
    if reader.remaining >= 8 or (reader.remaining and reader.read_bits(reader.remaining)):
        raise ValueError(f"{name} has trailing data")


def decode_prop_interaction(
    payload: bytes, bit_length: int | None = None,
) -> PropInteraction:
    reader = _client_reader(payload, PROP_INTERACTION_COMMAND, bit_length)
    result = PropInteraction(reader.read_uint32_le(), reader.read_string())
    _check_padding(reader, "PropInteraction")
    return result


def encode_prop_interaction(value: PropInteraction) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(PROP_INTERACTION_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(int(value.prop_id))
    writer.write_string(value.operation)
    return writer.finish()


def decode_quest_trigger_signal(
    payload: bytes, bit_length: int | None = None,
) -> QuestTriggerSignal:
    reader = _client_reader(payload, QUEST_TRIGGER_SIGNAL_COMMAND, bit_length)
    result = QuestTriggerSignal(reader.read_string())
    _check_padding(reader, "QuestTriggerSignal")
    return result


def encode_quest_trigger_signal(value: QuestTriggerSignal) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(QUEST_TRIGGER_SIGNAL_COMMAND.to_bytes(2, "little"))
    writer.write_string(value.trigger_id)
    return writer.finish()


__all__ = [
    "DISCARD_PROP_COMMAND",
    "NEW_PROP_COMMAND",
    "PROP_INTERACTION_COMMAND",
    "PROP_STATE_ACTIVE",
    "PROP_STATE_COOLDOWN",
    "PROP_STATE_INACTIVE",
    "PROP_STATE_NONE",
    "PROP_STATE_USED",
    "PROP_UPDATE_COMMAND",
    "QUEST_TRIGGER_SIGNAL_COMMAND",
    "NewProp",
    "PropInteraction",
    "PropUpdate",
    "QuestTriggerSignal",
    "ZERO_GUID",
    "decode_discard_prop_batch",
    "decode_new_prop_batch",
    "decode_prop_interaction",
    "decode_prop_update_batch",
    "decode_quest_trigger_signal",
    "encode_discard_prop_batch",
    "encode_new_prop_batch",
    "encode_prop_interaction",
    "encode_prop_update_batch",
    "encode_quest_trigger_signal",
]
