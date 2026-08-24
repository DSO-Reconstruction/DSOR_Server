"""The item a creature leaves behind.

``NewItemCommand``, 0x002D. Its layout was read out of the batch of a real killing
blow, which is where it travels — the command boundaries in that batch are not
byte-aligned, so finding it at all needed a bit-level walk rather than a search for
``FF 2D 00``:

    actor id            the item's own, four bytes
    six uint32          0, 1, 256, 6753, 0, 0
    string              "none"
    three uint32        1, 15, 0
    two bits            0, 0
    string              the item's template name
    uint16              0
    three float32       where it lies, in the **description** frame
    then                4, 10, 4, 12 … attribute and value, apparently

Only two of those fields are rewritten here — the actor and the position — and
everything else is copied bit for bit from the recording. The tail is not
understood, and inventing it would be the same mistake as writing 26 bytes of a
64-byte skill command: plausible, and silently wrong.

The position is the one that matters. It is in the same frame as a creature's
description, not the frame its movement records use, and the recorded value is
where the creature died **in another session**. Replaying it unchanged is why loot
lay somewhere unreachable.
"""

from __future__ import annotations

import struct
from pathlib import Path

#: Bit offset of the item's actor id: past the 0x85 message id and the command id.
ACTOR_BIT = 24

#: Bit offset of the three floats, past the two strings and the fields between.
#: Fixed only because the template name's length is fixed — rewriting the name
#: would move it, which is why the name is left alone.
POSITION_BIT = 738

_DATA = Path(__file__).with_name("data")


def item_drop() -> bytes:
    """The recorded 0x002D, as a standalone message."""
    return (_DATA / "item_drop.bin").read_bytes()


def _write_bits(buf: bytes, offset: int, payload: bytes) -> bytes:
    """Return *buf* with *payload* written at bit *offset*."""
    out = bytearray(buf)
    for index, byte in enumerate(payload):
        for bit in range(8):
            position = offset + index * 8 + bit
            byte_at, shift = divmod(position, 8)
            mask = 1 << (7 - shift)
            if byte >> (7 - bit) & 1:
                out[byte_at] |= mask
            else:
                out[byte_at] &= 0xFF & ~mask
    return bytes(out)


def _read_bits(buf: bytes, offset: int, count: int) -> bytes:
    value = 0
    for index in range(count * 8):
        position = offset + index
        byte_at, shift = divmod(position, 8)
        value = (value << 1) | (buf[byte_at] >> (7 - shift) & 1)
    return value.to_bytes(count, "big")


def drop_actor(command: bytes) -> bytes:
    """The actor id the command carries."""
    return _read_bits(command, ACTOR_BIT, 4)


def drop_position(command: bytes) -> tuple[float, float, float]:
    """Where the command says the item lies."""
    raw = _read_bits(command, POSITION_BIT, 12)
    return struct.unpack("<3f", raw)


def with_drop(
    command: bytes, actor: bytes, position: tuple[float, float, float]
) -> bytes:
    """Return *command* with a fresh actor and a new resting place."""
    if len(actor) != 4:
        raise ValueError(f"an actor id is four bytes, got {len(actor)}")
    out = _write_bits(command, ACTOR_BIT, actor)
    return _write_bits(out, POSITION_BIT, struct.pack("<3f", *position))
