"""Typed codec for the global PvP flag request/response."""

from __future__ import annotations

from dso_server.protocol.command_ids import command_id
from dataclasses import dataclass

from dso_server.protocol.character_selection import BitReader, BitWriter


PVP_FLAG_COMMAND = command_id("PVP::PVPFlagCommand", 0x00C0)
@dataclass(frozen=True)
class PVPFlag:
    """The two fields serialized by ``PVP::PVPFlagCommand``.

    The bool is the requested global mode and the uint32 is the client game
    tick.  The native serializer writes the bool first, which makes the later
    uint32 appear bit-shifted in a byte-oriented hex dump.
    """

    enabled: bool
    server_tick: int


def decode_pvp_flag(
    payload: bytes, bit_length: int | None = None
) -> PVPFlag:
    bit_length = len(payload) * 8 if bit_length is None else int(bit_length)
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x8B:
        raise ValueError("PVPFlag is not a client command")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != PVP_FLAG_COMMAND:
        raise ValueError(
            f"expected PVPFlag 0x{PVP_FLAG_COMMAND:04x}, "
            f"got 0x{command_id:04x}"
        )
    value = PVPFlag(reader.read_bool(), reader.read_uint32_le())
    if reader.remaining:
        raise ValueError(f"PVPFlag has {reader.remaining} unread bits")
    return value


def encode_pvp_flag_request(value: PVPFlag) -> tuple[bytes, int]:
    """Encode the 57-bit client request used by tests and tooling."""

    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(PVP_FLAG_COMMAND.to_bytes(2, "little"))
    writer.write_bool(bool(value.enabled))
    writer.write_uint32_le(int(value.server_tick))
    return writer.finish()


def encode_pvp_flag_response(
    value: PVPFlag, *, actor_id: int
) -> tuple[bytes, int]:
    """Encode the 97-bit retail response, including its batch terminator."""

    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(PVP_FLAG_COMMAND.to_bytes(2, "little"))
    writer.write_bool(bool(value.enabled))
    writer.write_uint32_le(int(value.server_tick))
    writer.write_uint32_le(int(actor_id))
    writer.write_uint8(0xFF)
    return writer.finish()
