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

#: Bit offset of the item's own id in the ground command. The client validates it
#: on pickup — "Received ItemInfoCommand with invalid item id (%u)!" is its own
#: complaint — so the id a drop declares and the id its pickup reply carries have
#: to be the same one. Replaying two recordings gave 6753 on the ground and 31633
#: in the reply, from different sessions, and the pickup silently did nothing.
ITEM_ID_BIT = 152

#: The same field in the pickup reply, which has one extra leading field.
PICKUP_ITEM_ID_BIT = 184

#: Bit offset of the template name's length prefix. Everything up to here is
#: fixed-width: the actor, six uint32, the string "none", three uint32 and two
#: bits.
NAME_LENGTH_BIT = 394

#: Bits between the end of the name and the three floats: one uint16.
GAP_AFTER_NAME = 16

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


def _bits(buf: bytes) -> str:
    return "".join(f"{byte:08b}" for byte in buf)


def _bytes(bits: str) -> bytes:
    pad = -len(bits) % 8
    return int(bits + "0" * pad, 2).to_bytes((len(bits) + pad) // 8, "big")


def drop_template(command: bytes) -> str:
    """The item's blueprint name."""
    length = int.from_bytes(_read_bits(command, NAME_LENGTH_BIT, 2), "little")
    return _read_bits(command, NAME_LENGTH_BIT + 16, length).decode("ascii")


def _position_bit(command: bytes) -> int:
    """Where the three floats start, which depends on the name's length."""
    length = int.from_bytes(_read_bits(command, NAME_LENGTH_BIT, 2), "little")
    return NAME_LENGTH_BIT + 16 + 8 * length + GAP_AFTER_NAME


def drop_position(command: bytes) -> tuple[float, float, float]:
    """Where the command says the item lies."""
    raw = _read_bits(command, _position_bit(command), 12)
    return struct.unpack("<3f", raw)


def with_template(command: bytes, name: str) -> bytes:
    """Return *command* carrying a different item.

    The name is length-prefixed and everything behind it shifts, so this splices the
    bit stream rather than overwriting bytes — a longer name would otherwise run
    over the position and the fields after it. Both names seen so far differ in
    length: the recorded mace is 39 characters, a helmet 54.
    """
    raw = name.encode("ascii")
    bits = _bits(command)
    old = int.from_bytes(_read_bits(command, NAME_LENGTH_BIT, 2), "little")
    tail = NAME_LENGTH_BIT + 16 + 8 * old
    head = bits[:NAME_LENGTH_BIT]
    length = _bits(len(raw).to_bytes(2, "little"))
    return _bytes(head + length + _bits(raw) + bits[tail:])


def drop_item_id(command: bytes) -> int:
    return int.from_bytes(_read_bits(command, ITEM_ID_BIT, 4), "little")


def with_drop(
    command: bytes,
    actor: bytes,
    position: tuple[float, float, float],
    template: str | None = None,
    item_id: int | None = None,
) -> bytes:
    """Return *command* with a fresh actor, a new resting place, and maybe an item.

    A name the client cannot resolve creates nothing, exactly as an unknown monster
    blueprint does; ``_Template_Item`` is what says whether one is real.
    """
    if len(actor) != 4:
        raise ValueError(f"an actor id is four bytes, got {len(actor)}")
    out = command if template is None else with_template(command, template)
    out = _write_bits(out, ACTOR_BIT, actor)
    if item_id is not None:
        out = _write_bits(out, ITEM_ID_BIT, item_id.to_bytes(4, "little"))
    return _write_bits(out, _position_bit(out), struct.pack("<3f", *position))


#: Bit offset of the actor id in a pickup reply: past the message id, the command
#: id, and one leading field the ground command does not have.
PICKUP_ACTOR_BIT = 56


def item_pickup() -> bytes:
    """The recorded 0x002F, as a standalone message.

    What the real server answers a pickup with. Its batch was 0x004F carrying a
    LocationEffectInfoCommand and then this, and it contains **no readable string**:
    the client already knows what the actor is, from the 0x002D that put it on the
    ground, so this only moves it. Which is what makes replaying it viable at all.
    """
    return (_DATA / "item_pickup.bin").read_bytes()


def pickup_actor(command: bytes) -> bytes:
    return _read_bits(command, PICKUP_ACTOR_BIT, 4)


def pickup_item_id(command: bytes) -> int:
    return int.from_bytes(_read_bits(command, PICKUP_ITEM_ID_BIT, 4), "little")


def with_pickup(command: bytes, actor: bytes, item_id: int | None = None) -> bytes:
    """Return *command* addressed to a different item.

    The id matters as much as the actor: the client looks the item up by id and
    refuses one it has never been told about.
    """
    if len(actor) != 4:
        raise ValueError(f"an actor id is four bytes, got {len(actor)}")
    out = _write_bits(command, PICKUP_ACTOR_BIT, actor)
    if item_id is not None:
        out = _write_bits(out, PICKUP_ITEM_ID_BIT, item_id.to_bytes(4, "little"))
    return out


#: The whole reply the real server sent, not just the command inside it: a 0x004F
#: batch carrying a LocationEffectInfoCommand and then the ItemInfoCommand.
#:
#: Replaying the command alone was byte-for-byte right and did nothing. That is a
#: shape this project has already met once: a skill command identical to a real one
#: also did nothing standalone, and worked the moment it travelled inside the batch
#: the capture put it in. So framing is tried before anything cleverer.
PICKUP_BATCH_ACTOR_BIT = 2379
PICKUP_BATCH_ITEM_ID_BIT = 2507


def item_pickup_batch() -> bytes:
    """The recorded 0x004F batch that answers a pickup."""
    return (_DATA / "item_pickup_batch.bin").read_bytes()


def batch_pickup_actor(batch: bytes) -> bytes:
    return _read_bits(batch, PICKUP_BATCH_ACTOR_BIT, 4)


def batch_pickup_item_id(batch: bytes) -> int:
    return int.from_bytes(_read_bits(batch, PICKUP_BATCH_ITEM_ID_BIT, 4), "little")


def with_pickup_batch(batch: bytes, actor: bytes, item_id: int) -> bytes:
    """Return the recorded reply, aimed at a different item."""
    if len(actor) != 4:
        raise ValueError(f"an actor id is four bytes, got {len(actor)}")
    out = _write_bits(batch, PICKUP_BATCH_ACTOR_BIT, actor)
    return _write_bits(out, PICKUP_BATCH_ITEM_ID_BIT, item_id.to_bytes(4, "little"))
