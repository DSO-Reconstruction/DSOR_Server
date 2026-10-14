"""Wanted mission commands from the release261 client serializers.

The response decoder is 0x14093b26c. Its two arrays contain uint32 values;
the final composite is a DateTime (eight uint32s), not another array.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from dso_server.protocol.character_selection import BitReader, BitWriter
from dso_server.protocol.command_ids import command_id


GET_WANTED_THIEF_INFO_COMMAND = command_id("Commands::GetWantedThiefInfoCommand", 0x0164)
GET_WANTED_THIEF_INFO_RESULT_COMMAND = command_id(
    "Commands::GetWantedThiefInfoResultCommand", 0x0165)
ACCEPT_WANTED_THIEF_MISSION_COMMAND = command_id(
    "Commands::AcceptWantedThiefMissionCommand", 0x0166)
REFRESH_WANTED_THIEF_MISSION_COMMAND = command_id(
    "Commands::RefreshWantedThiefMissionCommand", 0x0167)
WANTED_THIEF_REQUEST_COMMANDS = frozenset({
    GET_WANTED_THIEF_INFO_COMMAND, ACCEPT_WANTED_THIEF_MISSION_COMMAND,
    REFRESH_WANTED_THIEF_MISSION_COMMAND,
})
WANTED_SLOT_COUNT = 4


@dataclass(frozen=True)
class WantedThiefRequest:
    command_id: int
    slot: int | None = None


@dataclass(frozen=True)
class WantedThiefInfo:
    remaining: int
    special_remaining: int
    mission_ids: tuple[int, ...]
    statuses: tuple[int, ...]
    vip_expires_at: datetime | None = None


def decode_wanted_thief_request(
    payload: bytes, bit_length: int | None = None,
) -> WantedThiefRequest:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x8B:
        raise ValueError("not a client Wanted Thief command")
    command = int.from_bytes(reader.read_bytes(2), "little")
    if command not in WANTED_THIEF_REQUEST_COMMANDS:
        raise ValueError(f"unsupported Wanted Thief command 0x{command:04x}")
    slot = None if command == GET_WANTED_THIEF_INFO_COMMAND else reader.read_uint32_le()
    if reader.remaining:
        raise ValueError(f"Wanted Thief request has {reader.remaining} unread bits")
    return WantedThiefRequest(command, slot)


def encode_wanted_thief_info(
    info: WantedThiefInfo, *, actor_id: int = 0,
) -> tuple[bytes, int]:
    if len(info.mission_ids) != WANTED_SLOT_COUNT or len(info.statuses) != WANTED_SLOT_COUNT:
        raise ValueError("Wanted Thief info requires four mission IDs and four statuses")
    values = (info.remaining, info.special_remaining, *info.mission_ids, *info.statuses, actor_id)
    if any(not 0 <= value <= 0xFFFFFFFF for value in values):
        raise ValueError("Wanted Thief fields must fit uint32")
    stamp = info.vip_expires_at
    if stamp is None:
        date_fields = (0,) * 8
    else:
        if stamp.tzinfo is None:
            raise ValueError("Wanted VIP expiry must have a timezone")
        stamp = stamp.astimezone(timezone.utc)
        # DateTime::Encode (0x140a55630), not SYSTEMTIME's memory order.
        date_fields = (
            stamp.year, stamp.day, stamp.hour, stamp.minute, stamp.second,
            stamp.microsecond // 1000, stamp.month, (stamp.weekday() + 1) % 7,
        )
    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(GET_WANTED_THIEF_INFO_RESULT_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(info.remaining)
    writer.write_uint32_le(info.special_remaining)
    for array in (info.mission_ids, info.statuses):
        writer.write_uint32_le(len(array))
        for value in array:
            writer.write_uint32_le(value)
    for value in date_fields:
        writer.write_uint32_le(value)
    # Additional server data is present, but the retail manager reply uses
    # actor 0 (capture_20260909_000000_sample_12.log, 12:08:27.769).
    writer.write_uint32_le(actor_id)
    return writer.finish()
