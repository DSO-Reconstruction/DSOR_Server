"""Editable serializers for the map transition packets used by the server."""

from __future__ import annotations

from dataclasses import dataclass

from dso_server.protocol.character_selection import BitWriter
from dso_server.protocol.command_ids import command_id

# Written into the two SwitchMapCommand batches below. The legacy client
# registers 121 for this class where the current one registers 112, and an id it
# cannot resolve makes it drop the whole batch:
#   "Client command factory could not create command (ID: '112')!"
SWITCH_MAP_COMMAND = command_id("Commands::SwitchMapCommand", 0x0070)


@dataclass(frozen=True)
class MapLoadPacket:
    map_id: str
    ruleset: int
    map_variant: int = 0
    is_instance: bool = False
    access_id: str | None = None

    def encode(self) -> tuple[bytes, int]:
        if not self.map_id:
            raise ValueError("map id cannot be empty")
        access_id = str(self.access_id or self.map_id)
        if not access_id:
            raise ValueError("map access id cannot be empty")
        if not -(1 << 31) <= self.ruleset < (1 << 31):
            raise ValueError("ruleset must fit in a signed 32-bit integer")
        if not 0 <= self.map_variant <= 0xFF:
            raise ValueError("map variant must fit in an unsigned byte")

        writer = BitWriter()
        writer.write_uint8(0x86)
        writer.write_string(self.map_id)
        writer.write_string(access_id)
        writer.write_uint32_le(self.ruleset & 0xFFFFFFFF)
        writer.write_uint8(self.map_variant)
        writer.write_bool(self.is_instance)
        return writer.finish()


@dataclass(frozen=True)
class SwitchMapPacket:
    server_address: str
    enter_character_selection: bool

    def encode(self) -> tuple[bytes, int]:
        try:
            self.server_address.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("server address must be ASCII") from exc

        writer = BitWriter()
        writer.write_uint8(0x84)
        writer.write_bytes(SWITCH_MAP_COMMAND.to_bytes(2, "little"))
        writer.write_string(self.server_address)
        writer.write_bool(self.enter_character_selection)
        return writer.finish()


@dataclass(frozen=True)
class GameplaySwitchMapPacket:
    """Actorless SwitchMapCommand carried by a gameplay ``0x85`` batch.

    Retail map exits use an empty address because the live client's configured
    primary endpoint is already the login service on UDP/2190.  Local clients
    are configured with the auth gateway on UDP/80 instead, so local exit
    transfers must provide the UDP/2190 address explicitly.
    """

    server_address: str = ""
    enter_character_selection: bool = False

    def encode(self) -> tuple[bytes, int]:
        try:
            self.server_address.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("server address must be ASCII") from exc

        writer = BitWriter()
        writer.write_uint8(0x85)
        writer.write_bytes(SWITCH_MAP_COMMAND.to_bytes(2, "little"))
        writer.write_string(self.server_address)
        writer.write_bool(self.enter_character_selection)
        writer.write_uint8(0xFF)
        return writer.finish()
