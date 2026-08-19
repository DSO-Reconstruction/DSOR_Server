"""The frame layer: individual messages packed inside a datagram.

A datagram carries one or more frames back to back.  Each frame has a header
whose *size depends on the reliability*, which is why a frame cannot be located
by a fixed offset:

===============================  ==========================================
1 byte                           reliability (top 3 bits) + split flag (0x10)
2 bytes, big-endian              payload length **in bits**
3 bytes, little-endian           reliable message number   *(if reliable)*
3 bytes, little-endian           sequencing index          *(if sequenced)*
3 bytes little-endian + 1 byte   ordering index + channel  *(if ordered)*
4 + 2 + 4 bytes, big-endian      split count, id, index    *(if split)*
ceil(bits / 8) bytes             payload; its first byte is the message ID
===============================  ==========================================

The length being in *bits* matters twice.  It is the field most often written as
a byte count by mistake, and because RakNet's BitStream packs booleans into
single bits, a payload's bit length is not always a multiple of eight — so a
parser that reads whole bytes must round up while a validator must compare bits.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import FRAME_HAS_SPLIT_PACKET, Reliability


@dataclass
class Frame:
    """One message, with the delivery metadata RakNet wraps around it."""

    payload: bytes
    reliability: Reliability = Reliability.RELIABLE_ORDERED
    reliable_index: int | None = None
    sequencing_index: int | None = None
    ordering_index: int | None = None
    ordering_channel: int = 0
    #: Split fields are set together or not at all.
    split_count: int | None = None
    split_id: int | None = None
    split_index: int | None = None
    #: Bit length as it appeared on the wire.  Only differs from
    #: ``len(payload) * 8`` when the sender packed a partial byte, which is why
    #: it is preserved rather than recomputed: re-encoding must reproduce it.
    bit_length: int | None = None

    @property
    def is_split(self) -> bool:
        return self.split_count is not None

    @property
    def message_id(self) -> int | None:
        """The first payload byte, which is how RakNet identifies a message.

        ``None`` for an empty payload.  For a split frame this is only
        meaningful on the piece with ``split_index == 0``.
        """
        return self.payload[0] if self.payload else None


def parse_frames(buf: bytes, offset: int) -> list[Frame]:
    """Read every frame in a datagram body.

    Raises if the body does not end exactly on a frame boundary.  That strictness
    is deliberate: leftover bytes are the earliest and clearest signal that a
    header width is wrong.
    """
    frames: list[Frame] = []
    while offset < len(buf):
        frame, offset = _parse_one(buf, offset)
        frames.append(frame)
    if offset != len(buf):  # pragma: no cover - loop condition makes this unreachable
        raise ValueError("frame parsing overran the datagram")
    return frames


def _parse_one(buf: bytes, offset: int) -> tuple[Frame, int]:
    if len(buf) - offset < 3:
        raise ValueError(
            f"{len(buf) - offset} bytes left, too few for a frame header"
        )
    flags = buf[offset]
    offset += 1
    reliability = Reliability((flags >> 5) & 0x07)
    is_split = bool(flags & FRAME_HAS_SPLIT_PACKET)

    bit_length = int.from_bytes(buf[offset : offset + 2], "big")
    offset += 2
    byte_length = (bit_length + 7) // 8

    frame = Frame(payload=b"", reliability=reliability, bit_length=bit_length)

    if reliability.has_reliable_index:
        frame.reliable_index = int.from_bytes(buf[offset : offset + 3], "little")
        offset += 3
    if reliability.has_sequencing_index:
        frame.sequencing_index = int.from_bytes(buf[offset : offset + 3], "little")
        offset += 3
    if reliability.has_ordering_index:
        frame.ordering_index = int.from_bytes(buf[offset : offset + 3], "little")
        offset += 3
        frame.ordering_channel = buf[offset]
        offset += 1
    if is_split:
        frame.split_count = int.from_bytes(buf[offset : offset + 4], "big")
        offset += 4
        frame.split_id = int.from_bytes(buf[offset : offset + 2], "big")
        offset += 2
        frame.split_index = int.from_bytes(buf[offset : offset + 4], "big")
        offset += 4

    end = offset + byte_length
    if end > len(buf):
        raise ValueError(
            f"frame claims {byte_length} payload bytes but only "
            f"{len(buf) - offset} remain"
        )
    frame.payload = buf[offset:end]
    return frame, end


def build_frame(frame: Frame) -> bytes:
    """Serialise *frame*, reproducing the wire bit length when it was preserved."""
    flags = (int(frame.reliability) & 0x07) << 5
    if frame.is_split:
        flags |= FRAME_HAS_SPLIT_PACKET

    bit_length = (
        frame.bit_length if frame.bit_length is not None else len(frame.payload) * 8
    )
    if (bit_length + 7) // 8 != len(frame.payload):
        raise ValueError(
            f"bit_length {bit_length} does not describe a {len(frame.payload)}-byte "
            "payload"
        )

    out = bytearray([flags])
    out += bit_length.to_bytes(2, "big")

    if frame.reliability.has_reliable_index:
        out += _require(frame.reliable_index, "reliable_index").to_bytes(3, "little")
    if frame.reliability.has_sequencing_index:
        out += _require(frame.sequencing_index, "sequencing_index").to_bytes(3, "little")
    if frame.reliability.has_ordering_index:
        out += _require(frame.ordering_index, "ordering_index").to_bytes(3, "little")
        out.append(frame.ordering_channel)
    if frame.is_split:
        out += frame.split_count.to_bytes(4, "big")
        out += frame.split_id.to_bytes(2, "big")
        out += frame.split_index.to_bytes(4, "big")

    return bytes(out) + frame.payload


def _require(value: int | None, name: str) -> int:
    if value is None:
        raise ValueError(f"{name} is required for this reliability but was not set")
    return value
