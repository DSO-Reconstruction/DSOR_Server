"""Complete generated-wire codec for ``Commands::NewPlayerCommand``.

The public bootstrap builder in :mod:`gameplay_commands` intentionally emits
constructor-default state.  This module preserves every generated field so a
retail command can be decoded, edited, and serialized again without retaining
an opaque binary payload.
"""

from __future__ import annotations

from dso_server.protocol.command_ids import command_id
from dataclasses import dataclass

from dso_server.protocol.character_selection import BitReader, BitWriter
from dso_server.protocol.gameplay_protocol import ActorPlacement


NEW_PLAYER_COMMAND = command_id("Commands::NewPlayerCommand", 0x001D)
@dataclass(frozen=True)
class PlayerStateRecord:
    name: str
    description: str
    values: tuple[int, int, int, int, int, int, int]


@dataclass(frozen=True)
class PlayerBulkRecord:
    field_00: int
    field_04: int
    field_08: int


@dataclass(frozen=True)
class NewPlayerWire:
    flags_20: tuple[bool, bool, bool, bool, bool, bool]
    field_28: int
    value_block_2c: tuple[int, ...]
    value_block_4c: tuple[int, ...]
    field_6c: bool
    field_6d: bool
    field_70: int
    position: tuple[float, float, float]
    field_90: int
    name: str
    appearance: tuple[int, int, int, int, int, int]
    template_row: int
    field_e8: str
    field_f0: int
    field_f8: int
    field_fc: int
    field_100: ActorPlacement
    fields_110: tuple[int, ...]
    field_130: tuple[int, ...]
    field_150: tuple[str, ...]
    fields_170: tuple[int, ...]
    field_190: int
    fields_198: tuple[int, int, int]
    field_1a8: int
    field_1b0: tuple[tuple[str, int], ...]
    fields_1d0: tuple[int, int]
    fields_1d8: tuple[int, ...]
    field_1f8: int
    field_200: str
    fields_208: tuple[int, int]
    player_state_scalars: tuple[int, ...]
    player_state_records: tuple[PlayerStateRecord, ...]
    player_state_fixed: tuple[int, ...]
    player_state_tables: tuple[int, ...]
    bulk_records: tuple[PlayerBulkRecord, ...]
    # Team index copied to GamePlayer +0x18; 0xFFFFFFFF means no team.
    field_388: int
    fields_38c: tuple[int, ...]
    field_3ac: int
    field_3b0: str
    fields_3b8: tuple[int, ...]
    actor_id: int


def _read_u32s(reader: BitReader, count: int) -> tuple[int, ...]:
    return tuple(reader.read_uint32_le() for _ in range(count))


def _write_u32s(writer: BitWriter, values, expected: int, label: str) -> None:
    values = tuple(values)
    if len(values) != expected:
        raise ValueError(f"{label} requires {expected} uint32 values")
    for value in values:
        writer.write_uint32_le(value)


def _read_actor_placement(reader: BitReader) -> ActorPlacement:
    return ActorPlacement(
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_uint8(),
        reader.read_bool(),
        reader.read_bool(),
    )


def _write_actor_placement(writer: BitWriter, value: ActorPlacement) -> None:
    writer.write_uint32_le(value.field_00)
    writer.write_uint32_le(value.actor_id)
    writer.write_uint8(value.field_08)
    writer.write_bool(value.field_0c)
    writer.write_bool(value.field_0d)


def read_new_player_body(reader: BitReader) -> NewPlayerWire:
    flags_20 = tuple(reader.read_bool() for _ in range(6))
    field_28 = reader.read_uint32_le()
    value_block_2c = _read_u32s(reader, 8)
    value_block_4c = _read_u32s(reader, 8)
    field_6c = reader.read_bool()
    field_6d = reader.read_bool()
    field_70 = reader.read_uint8()
    position = tuple(reader.read_float() for _ in range(3))
    field_90 = reader.read_uint32_le()
    name = reader.read_string()
    appearance = tuple(reader.read_uint8() for _ in range(6))
    template_row = reader.read_uint32_le()
    field_e8 = reader.read_string()
    field_f0 = reader.read_uint64_le()
    field_f8 = reader.read_uint32_le()
    field_fc = reader.read_uint8()
    field_100 = _read_actor_placement(reader)
    fields_110 = _read_u32s(reader, 8)
    field_130 = _read_u32s(reader, reader.read_uint32_le())
    field_150 = tuple(
        reader.read_string() for _ in range(reader.read_uint32_le())
    )
    fields_170 = _read_u32s(reader, 7)
    field_190 = reader.read_uint64_le()
    fields_198 = _read_u32s(reader, 3)
    field_1a8 = reader.read_uint64_le()
    field_1b0 = tuple(
        (reader.read_string(), reader.read_uint64_le())
        for _ in range(reader.read_uint32_le())
    )
    fields_1d0 = _read_u32s(reader, 2)
    fields_1d8 = _read_u32s(reader, 8)
    field_1f8 = reader.read_uint32_le()
    field_200 = reader.read_string()
    fields_208 = _read_u32s(reader, 2)
    player_state_scalars = _read_u32s(reader, 22)
    player_state_records = tuple(
        PlayerStateRecord(
            reader.read_string(),
            reader.read_string(),
            _read_u32s(reader, 7),
        )
        for _ in range(reader.read_uint32_le())
    )
    player_state_fixed = _read_u32s(reader, 4)
    player_state_tables = _read_u32s(reader, 14)
    bulk_records = tuple(
        PlayerBulkRecord(
            reader.read_uint32_le(),
            reader.read_uint32_le(),
            reader.read_uint8(),
        )
        for _ in range(reader.read_uint32_le())
    )
    field_388 = reader.read_uint32_le()
    fields_38c = _read_u32s(reader, 8)
    field_3ac = reader.read_uint8()
    field_3b0 = reader.read_string()
    fields_3b8 = _read_u32s(reader, 8)
    actor_id = reader.read_uint32_le()
    return NewPlayerWire(
        flags_20,
        field_28,
        value_block_2c,
        value_block_4c,
        field_6c,
        field_6d,
        field_70,
        position,
        field_90,
        name,
        appearance,
        template_row,
        field_e8,
        field_f0,
        field_f8,
        field_fc,
        field_100,
        fields_110,
        field_130,
        field_150,
        fields_170,
        field_190,
        fields_198,
        field_1a8,
        field_1b0,
        fields_1d0,
        fields_1d8,
        field_1f8,
        field_200,
        fields_208,
        player_state_scalars,
        player_state_records,
        player_state_fixed,
        player_state_tables,
        bulk_records,
        field_388,
        fields_38c,
        field_3ac,
        field_3b0,
        fields_3b8,
        actor_id,
    )


def write_new_player_body(writer: BitWriter, value: NewPlayerWire) -> None:
    if len(value.flags_20) != 6 or len(value.appearance) != 6:
        raise ValueError("NewPlayer flag and appearance blocks require six values")
    for flag in value.flags_20:
        writer.write_bool(flag)
    writer.write_uint32_le(value.field_28)
    _write_u32s(writer, value.value_block_2c, 8, "value_block_2c")
    _write_u32s(writer, value.value_block_4c, 8, "value_block_4c")
    writer.write_bool(value.field_6c)
    writer.write_bool(value.field_6d)
    writer.write_uint8(value.field_70)
    for component in value.position:
        writer.write_float(component)
    writer.write_uint32_le(value.field_90)
    writer.write_string(value.name)
    for entry in value.appearance:
        writer.write_uint8(entry)
    writer.write_uint32_le(value.template_row)
    writer.write_string(value.field_e8)
    writer.write_uint64_le(value.field_f0)
    writer.write_uint32_le(value.field_f8)
    writer.write_uint8(value.field_fc)
    _write_actor_placement(writer, value.field_100)
    _write_u32s(writer, value.fields_110, 8, "fields_110")
    writer.write_uint32_le(len(value.field_130))
    for entry in value.field_130:
        writer.write_uint32_le(entry)
    writer.write_uint32_le(len(value.field_150))
    for entry in value.field_150:
        writer.write_string(entry)
    _write_u32s(writer, value.fields_170, 7, "fields_170")
    writer.write_uint64_le(value.field_190)
    _write_u32s(writer, value.fields_198, 3, "fields_198")
    writer.write_uint64_le(value.field_1a8)
    writer.write_uint32_le(len(value.field_1b0))
    for name, number in value.field_1b0:
        writer.write_string(name)
        writer.write_uint64_le(number)
    _write_u32s(writer, value.fields_1d0, 2, "fields_1d0")
    _write_u32s(writer, value.fields_1d8, 8, "fields_1d8")
    writer.write_uint32_le(value.field_1f8)
    writer.write_string(value.field_200)
    _write_u32s(writer, value.fields_208, 2, "fields_208")
    _write_u32s(writer, value.player_state_scalars, 22, "player_state_scalars")
    writer.write_uint32_le(len(value.player_state_records))
    for record in value.player_state_records:
        writer.write_string(record.name)
        writer.write_string(record.description)
        _write_u32s(writer, record.values, 7, "PlayerStateRecord.values")
    _write_u32s(writer, value.player_state_fixed, 4, "player_state_fixed")
    _write_u32s(writer, value.player_state_tables, 14, "player_state_tables")
    writer.write_uint32_le(len(value.bulk_records))
    for record in value.bulk_records:
        writer.write_uint32_le(record.field_00)
        writer.write_uint32_le(record.field_04)
        writer.write_uint8(record.field_08)
    writer.write_uint32_le(value.field_388)
    _write_u32s(writer, value.fields_38c, 8, "fields_38c")
    writer.write_uint8(value.field_3ac)
    writer.write_string(value.field_3b0)
    _write_u32s(writer, value.fields_3b8, 8, "fields_3b8")
    writer.write_uint32_le(value.actor_id)


def decode_new_player_prefix(
    payload: bytes, bit_length: int
) -> tuple[NewPlayerWire, int]:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() not in (0x84, 0x85):
        raise ValueError("NewPlayer payload has the wrong application id")
    if int.from_bytes(reader.read_bytes(2), "little") != NEW_PLAYER_COMMAND:
        raise ValueError("NewPlayer payload has the wrong command id")
    value = read_new_player_body(reader)
    return value, reader.position


def encode_new_player_wire(
    value: NewPlayerWire, *, message_id: int = 0x84
) -> tuple[bytes, int]:
    if message_id not in (0x84, 0x85):
        raise ValueError("NewPlayer message_id must be 0x84 or 0x85")
    writer = BitWriter()
    writer.write_uint8(message_id)
    writer.write_bytes(NEW_PLAYER_COMMAND.to_bytes(2, "little"))
    write_new_player_body(writer, value)
    return writer.finish()
