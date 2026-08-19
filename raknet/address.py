"""Encoding and decoding of RakNet's ``SystemAddress``.

RakNet does not put an IPv4 address on the wire as-is.  It writes the **bitwise
complement** of the four address bytes, historically so that NAT devices
scanning for embedded addresses would not rewrite them.  The port is *not*
complemented: it goes out big-endian (network order) unchanged.

Getting this wrong is quiet and fatal.  The client complements the bytes back,
believes its external address is nonsense, and abandons the connection without
an error message.  Byte-reversing instead of complementing is the specific
mistake to watch for, because reversal happens to look plausible in a hex dump.

The rule is self-evident in a capture: the address the client writes into
OPEN_CONNECTION_REQUEST_2 is the address it dialled, so complementing it must
give back the server's own IP.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

#: RakNet tags each address with its family. Only IPv4 is implemented here,
#: because it is all the Drakensang client uses.
IP_VERSION_4 = 4

#: version byte + 4 address bytes + 2 port bytes
SYSTEM_ADDRESS_SIZE = 7

#: A "no address" placeholder, used to pad the internal-address list.
UNSPECIFIED = ("0.0.0.0", 0)


@dataclass(frozen=True)
class SystemAddress:
    """An IPv4 endpoint as RakNet represents it."""

    ip: str
    port: int

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.ip}:{self.port}"


def _complement(raw: bytes) -> bytes:
    """Flip every bit of *raw*. Its own inverse, which is why encode == decode."""
    return bytes(b ^ 0xFF for b in raw)


def encode_address(address: SystemAddress) -> bytes:
    """Serialise *address* into RakNet's 7-byte form."""
    if not 0 <= address.port <= 0xFFFF:
        raise ValueError(f"port out of range: {address.port}")
    packed = socket.inet_aton(address.ip)
    return bytes([IP_VERSION_4]) + _complement(packed) + address.port.to_bytes(2, "big")


def decode_address(buf: bytes, offset: int = 0) -> tuple[SystemAddress, int]:
    """Read one address from *buf* at *offset*.

    Returns the address and the offset just past it, so callers can chain reads
    without tracking widths themselves.
    """
    end = offset + SYSTEM_ADDRESS_SIZE
    if len(buf) < end:
        raise ValueError(
            f"need {SYSTEM_ADDRESS_SIZE} bytes for a SystemAddress, "
            f"only {len(buf) - offset} left"
        )
    version = buf[offset]
    if version != IP_VERSION_4:
        raise ValueError(f"unsupported IP version {version}; only IPv4 is implemented")
    ip = socket.inet_ntoa(_complement(buf[offset + 1 : offset + 5]))
    port = int.from_bytes(buf[offset + 5 : end], "big")
    return SystemAddress(ip, port), end


def encode_unspecified() -> bytes:
    """The padding entry RakNet uses for unused internal-address slots."""
    return encode_address(SystemAddress(*UNSPECIFIED))
