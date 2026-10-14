"""Player death/respawn commands recovered from the retail serializers.

Respawn Encode 0x1409452f8 writes two bools (+0x20, +0x21), not an
option number. The death UI sets one flag for each paid button and neither
for town (0x1403dbd97, 0x1403dbe80, 0x1403dbf56).
"""

from dso_server.protocol.command_ids import command_id
from dataclasses import dataclass

from dso_server.protocol.character_selection import BitReader, BitWriter

RESPAWN_COMMAND = command_id("Commands::RespawnCommand", 0x0062)
REVIVE_COMMAND = command_id("Commands::ReviveCommand", 0x006D)
SHOW_DEATH_DIALOG_COMMAND = command_id("Commands::ShowDeathDialogCommand", 0x0081)
@dataclass(frozen=True)
class RespawnRequest:
    nearest_exit: bool = False
    same_spot: bool = False


def decode_respawn(payload: bytes, bit_length: int | None = None) -> RespawnRequest:
    # RakNet supplies 26 bits; direct callers may only have the padded bytes.
    if len(payload) != 4 or bit_length not in (None, 26, 32):
        raise ValueError("RespawnCommand requires exactly two flags")
    if payload[:3] != b"\x8b\x62\x00" or payload[3] & 0x3F:
        raise ValueError("invalid RespawnCommand header or padding")
    reader = BitReader(payload, 26)
    reader.read_bytes(3)
    request = RespawnRequest(reader.read_bool(), reader.read_bool())
    if request.nearest_exit and request.same_spot:
        raise ValueError("respawn destinations are mutually exclusive")
    return request


def encode_revive(*, actor_id: int, duration_ticks: int = 0) -> tuple[bytes, int]:
    """Revive Encode 0x14094541c: uint32 duration, then the actor wrapper."""
    writer = BitWriter()
    writer.write_bytes(b"\x85\x6d\x00")
    writer.write_uint32_le(duration_ticks)
    writer.write_uint32_le(actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def encode_death_dialog() -> tuple[bytes, int]:
    # Fieldless command with actor wrapper zero, exactly as captured.
    return b"\x85\x81\x00\x00\x00\x00\x00\xff", 64
