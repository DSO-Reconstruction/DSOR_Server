"""The unconnected handshake: four messages before a connection exists.

These are the only RakNet messages that are *not* datagrams.  Their first byte
is a plain message ID, they carry :data:`OFFLINE_MESSAGE_ID` so a peer can
recognise them, and they have no sequence number or reliability.

The exchange, all four steps confirmed against a Drakensang Online capture::

    C -> S  0x05  OPEN_CONNECTION_REQUEST_1   magic, protocol version, MTU padding
    S -> C  0x06  OPEN_CONNECTION_REPLY_1     server GUID, security flag, MTU
    C -> S  0x07  OPEN_CONNECTION_REQUEST_2   server address, MTU, client GUID
    S -> C  0x08  OPEN_CONNECTION_REPLY_2     server GUID, client address, MTU, security

Two details decide whether a real client will proceed:

*MTU discovery.*  Request 1 is padded with zeros so the whole IP packet is
exactly the size being probed; the server infers the MTU from how much arrived
and must never answer with more.  The reply's ``security`` byte and 16-bit MTU
are three bytes total — writing four is a common and fatal slip.

*The address in reply 2 is the client's address as the server sees it*, which
behind NAT is its public one, not the address the client knows itself by.  It is
encoded with the complement rule in :mod:`raknet.address`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .address import SystemAddress, decode_address, encode_address
from .constants import OFFLINE_MESSAGE_ID, RAKNET_PROTOCOL_VERSION, MessageID

#: Bytes an IPv4/UDP header adds to a payload, used to turn a received datagram
#: size back into the MTU the client was probing.
UDP_IPV4_OVERHEAD = 28

#: RakNet refuses to negotiate below this; a smaller answer is a bug, not a
#: conservative choice.
MINIMUM_MTU = 400


class HandshakeError(Exception):
    """Raised when an unconnected message cannot be honoured."""


def _expect_magic(buf: bytes, offset: int) -> int:
    end = offset + len(OFFLINE_MESSAGE_ID)
    if buf[offset:end] != OFFLINE_MESSAGE_ID:
        raise HandshakeError("offline message id missing; not RakNet traffic")
    return end


@dataclass(frozen=True)
class OpenConnectionRequest1:
    protocol_version: int
    #: MTU the client is probing, derived from the datagram size.
    mtu: int


def parse_open_connection_request_1(buf: bytes) -> OpenConnectionRequest1:
    if buf[0] != MessageID.OPEN_CONNECTION_REQUEST_1:
        raise HandshakeError(f"expected 0x05, got {buf[0]:#02x}")
    offset = _expect_magic(buf, 1)
    return OpenConnectionRequest1(
        protocol_version=buf[offset], mtu=len(buf) + UDP_IPV4_OVERHEAD
    )


def build_open_connection_reply_1(
    server_guid: bytes, mtu: int, use_security: bool = False
) -> bytes:
    """Answer request 1.

    *mtu* must not exceed what the client probed, or the connection will carry
    datagrams the path cannot deliver.
    """
    _check_guid(server_guid)
    if mtu < MINIMUM_MTU:
        raise HandshakeError(f"MTU {mtu} is below RakNet's minimum {MINIMUM_MTU}")
    return b"".join(
        [
            bytes([MessageID.OPEN_CONNECTION_REPLY_1]),
            OFFLINE_MESSAGE_ID,
            server_guid,
            bytes([1 if use_security else 0]),
            mtu.to_bytes(2, "big"),
        ]
    )


@dataclass(frozen=True)
class OpenConnectionRequest2:
    #: The address the client dialled. Complementing it must give the server's
    #: own IP, which makes this field a free sanity check on the address codec.
    server_address: SystemAddress
    mtu: int
    client_guid: bytes


def parse_open_connection_request_2(buf: bytes) -> OpenConnectionRequest2:
    if buf[0] != MessageID.OPEN_CONNECTION_REQUEST_2:
        raise HandshakeError(f"expected 0x07, got {buf[0]:#02x}")
    offset = _expect_magic(buf, 1)
    server_address, offset = decode_address(buf, offset)
    mtu = int.from_bytes(buf[offset : offset + 2], "big")
    offset += 2
    client_guid = buf[offset : offset + 8]
    if len(client_guid) != 8:
        raise HandshakeError("truncated client GUID")
    return OpenConnectionRequest2(server_address, mtu, client_guid)


def build_open_connection_reply_2(
    server_guid: bytes,
    client_address: SystemAddress,
    mtu: int,
    use_security: bool = False,
) -> bytes:
    """Answer request 2, echoing the client's address as observed."""
    _check_guid(server_guid)
    return b"".join(
        [
            bytes([MessageID.OPEN_CONNECTION_REPLY_2]),
            OFFLINE_MESSAGE_ID,
            server_guid,
            encode_address(client_address),
            mtu.to_bytes(2, "big"),
            bytes([1 if use_security else 0]),
        ]
    )


def build_incompatible_protocol_version(server_guid: bytes) -> bytes:
    """Tell a client its RakNet revision does not match ours.

    Worth sending rather than dropping the packet: it is the difference between
    a client that reports a version error and one that silently retries.
    """
    _check_guid(server_guid)
    return b"".join(
        [
            bytes([MessageID.INCOMPATIBLE_PROTOCOL_VERSION]),
            bytes([RAKNET_PROTOCOL_VERSION]),
            OFFLINE_MESSAGE_ID,
            server_guid,
        ]
    )


def _check_guid(guid: bytes) -> None:
    if len(guid) != 8:
        raise HandshakeError(f"a RakNet GUID is 8 bytes, got {len(guid)}")
