"""The datagram layer: RakNet's envelope around one or more frames.

Every UDP payload on an established connection starts with:

===========  =====================================================
1 byte       flags (see :class:`~raknet.constants.DatagramFlag`)
3 bytes      datagram sequence number, **little-endian**
===========  =====================================================

and then either acknowledgement ranges (when ``IS_ACK`` or ``IS_NAK`` is set) or
a run of frames.

The sequence number is per connection and per direction, starting at 0 and
incrementing for every datagram sent.  Sending a constant value is a classic
bug: the peer accepts the first datagram and discards every later one as a
duplicate, which looks exactly like the peer ignoring you.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import DatagramFlag

#: flags byte + 24-bit sequence number
DATAGRAM_HEADER_SIZE = 4

#: The sequence number is 24 bits, so it wraps.
SEQUENCE_MODULO = 1 << 24


@dataclass(frozen=True)
class DatagramHeader:
    """A parsed datagram envelope."""

    flags: int
    sequence: int

    @property
    def is_valid(self) -> bool:
        """False for unconnected messages, which are not datagrams at all."""
        return bool(self.flags & DatagramFlag.IS_VALID)

    @property
    def is_ack(self) -> bool:
        return bool(self.flags & DatagramFlag.IS_ACK)

    @property
    def is_nak(self) -> bool:
        # The 0x20 bit means NAK only when it is not an ACK, where it means
        # "carries bandwidth stats" instead.
        return not self.is_ack and bool(self.flags & DatagramFlag.IS_NAK)

    @property
    def has_bandwidth_figure(self) -> bool:
        return self.is_ack and bool(self.flags & DatagramFlag.HAS_B_AND_AS)


def parse_datagram_header(buf: bytes) -> tuple[DatagramHeader, int]:
    """Read the envelope from *buf*, returning it and the payload offset."""
    if len(buf) < DATAGRAM_HEADER_SIZE:
        raise ValueError(f"datagram too short: {len(buf)} bytes")
    flags = buf[0]
    sequence = int.from_bytes(buf[1:4], "little")
    return DatagramHeader(flags, sequence), DATAGRAM_HEADER_SIZE


def build_datagram_header(sequence: int, flags: int = DatagramFlag.IS_VALID) -> bytes:
    """Serialise an envelope. *sequence* wraps at 24 bits, as RakNet's does."""
    if flags & ~0xFF:
        raise ValueError(f"flags must fit in one byte: {flags:#x}")
    return bytes([flags]) + (sequence % SEQUENCE_MODULO).to_bytes(3, "little")


@dataclass(frozen=True)
class AckRange:
    """One acknowledged span of datagram sequence numbers, inclusive."""

    minimum: int
    maximum: int

    def __post_init__(self) -> None:
        if self.maximum < self.minimum:
            raise ValueError(f"inverted range {self.minimum}..{self.maximum}")

    def __iter__(self):
        return iter(range(self.minimum, self.maximum + 1))


@dataclass
class AckPayload:
    """The body of an ACK or NAK datagram."""

    ranges: list[AckRange] = field(default_factory=list)
    #: Present only when the sender included a bandwidth figure.
    arrival_rate: float | None = None


def parse_ack_payload(buf: bytes, offset: int, has_bandwidth: bool) -> AckPayload:
    """Read acknowledgement ranges.

    Layout: a big-endian 16-bit record count, then per record a flag saying
    whether the range is a single number, followed by one or two 24-bit
    little-endian sequence numbers.
    """
    import struct

    arrival_rate = None
    if has_bandwidth:
        (arrival_rate,) = struct.unpack_from(">f", buf, offset)
        offset += 4
    count = int.from_bytes(buf[offset : offset + 2], "big")
    offset += 2
    ranges: list[AckRange] = []
    for _ in range(count):
        single = buf[offset]
        offset += 1
        minimum = int.from_bytes(buf[offset : offset + 3], "little")
        offset += 3
        if single:
            maximum = minimum
        else:
            maximum = int.from_bytes(buf[offset : offset + 3], "little")
            offset += 3
        ranges.append(AckRange(minimum, maximum))
    if offset != len(buf):
        raise ValueError(
            f"{len(buf) - offset} unread bytes after {count} ACK records — "
            "the layout does not match"
        )
    return AckPayload(ranges, arrival_rate)


def build_ack(ranges: list[AckRange], is_nak: bool = False) -> bytes:
    """Serialise an ACK (or NAK) datagram.

    ACKs carry no datagram sequence number of their own: the flags byte is
    followed directly by the record count.
    """
    flags = DatagramFlag.IS_VALID | (
        DatagramFlag.IS_NAK if is_nak else DatagramFlag.IS_ACK
    )
    out = bytearray([flags])
    out += len(ranges).to_bytes(2, "big")
    for span in ranges:
        single = span.minimum == span.maximum
        out.append(1 if single else 0)
        out += span.minimum.to_bytes(3, "little")
        if not single:
            out += span.maximum.to_bytes(3, "little")
    return bytes(out)


def coalesce(sequences: list[int]) -> list[AckRange]:
    """Group sequence numbers into contiguous ranges, as RakNet does.

    Acknowledging 200 datagrams individually would need 800 bytes; as ranges it
    is usually one record.
    """
    if not sequences:
        return []
    ordered = sorted(set(sequences))
    ranges = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(AckRange(start, previous))
        start = previous = value
    ranges.append(AckRange(start, previous))
    return ranges
