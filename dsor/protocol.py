"""The Drakensang layer that sits on top of RakNet.

Everything here was read off a capture of the real client, so each constant
below can be traced back to bytes on a wire rather than to a guess.

Two conventions to know:

*Message IDs are offsets from RakNet's user boundary.*  The game's own messages
begin at :data:`~raknet.constants.MessageID.USER_PACKET_ENUM` (0x84).  Writing
them as bare hex constants invites confusion with the datagram flag byte, which
lands in the same numeric range; expressing them as ``USER_PACKET_ENUM + n``
makes the distinction structural.  Two IDs sit *below* the boundary — 0x82 and
0x83 — because the game appropriated RakNet's unused ``ID_RESERVED_8`` and
``ID_RESERVED_9`` slots, which is legitimate but has to be handled explicitly.

*Strings carry a 16-bit little-endian length prefix*, then raw bytes.  The
service identity message is the clearest example::

    82  16 00  "DrasaOnlineLoginServer"
    ^   ^      ^
    id  22     22 bytes of ASCII
"""

from __future__ import annotations

from enum import IntEnum

from raknet.constants import MessageID

#: UDP ports the client dials, with the service each one announced in 0x82.
#: Taken from a capture; a port not listed here has simply not been observed.
SERVICE_PORTS = {
    2190: "DrasaOnlineLoginServer",
    2192: "DrasaCharacterService",
}

#: How the client identifies itself to the login service.
CLIENT_IDENTITY = "DrasaOnlineClient"


class DsoMessage(IntEnum):
    """Drakensang's own message IDs.

    Names for the messages below the user boundary reflect what they were
    observed doing; the rest are named after their position so nothing is
    claimed that the capture does not show.
    """

    #: RakNet's ID_RESERVED_8, used by the game to announce which service the
    #: client just reached.  Payload: one length-prefixed string.
    SERVICE_IDENTITY = 0x82
    #: RakNet's ID_RESERVED_9. Periodic, server to client.
    TIME_SYNC = 0x83

    USER_0 = MessageID.USER_PACKET_ENUM + 0  # 0x84
    USER_1 = MessageID.USER_PACKET_ENUM + 1  # 0x85
    USER_2 = MessageID.USER_PACKET_ENUM + 2  # 0x86
    USER_3 = MessageID.USER_PACKET_ENUM + 3  # 0x87
    USER_4 = MessageID.USER_PACKET_ENUM + 4  # 0x88
    USER_5 = MessageID.USER_PACKET_ENUM + 5  # 0x89
    USER_6 = MessageID.USER_PACKET_ENUM + 6  # 0x8A
    USER_7 = MessageID.USER_PACKET_ENUM + 7  # 0x8B
    USER_9 = MessageID.USER_PACKET_ENUM + 9  # 0x8D


def encode_string(text: str) -> bytes:
    """Length-prefixed string, as the game writes them."""
    raw = text.encode("utf-8")
    if len(raw) > 0xFFFF:
        raise ValueError("string too long for a 16-bit length prefix")
    return len(raw).to_bytes(2, "little") + raw


def decode_string(buf: bytes, offset: int = 0) -> tuple[str, int]:
    """Read one length-prefixed string, returning it and the offset past it."""
    length = int.from_bytes(buf[offset : offset + 2], "little")
    start = offset + 2
    end = start + length
    if end > len(buf):
        raise ValueError(
            f"string claims {length} bytes but only {len(buf) - start} remain"
        )
    return buf[start:end].decode("utf-8", errors="replace"), end


def build_service_identity(service_name: str) -> bytes:
    """The 0x82 message a service sends as soon as a connection is established."""
    return bytes([DsoMessage.SERVICE_IDENTITY]) + encode_string(service_name)


def parse_service_identity(payload: bytes) -> str:
    """Read a 0x82 message."""
    if payload[0] != DsoMessage.SERVICE_IDENTITY:
        raise ValueError(f"expected 0x82, got {payload[0]:#02x}")
    name, _ = decode_string(payload, 1)
    return name
