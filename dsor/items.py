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

#: An unidentified 32-bit field: 6753 in the recorded ground command, 31633 in the
#: recorded reply. It was taken for the item's id and rewritten to match across the
#: two messages, on the strength of the client's own complaint about an "invalid
#: item id". That was wrong, and the client said so itself: it logs
#:
#:     Factored new item with item id 65602 and game entity id 293
#:
#: where 65602 is 0x00010042 — the **actor** bytes 42 00 01 00. The id the client
#: means is the actor, which is a field this code already rewrote correctly. So
#: this one is left alone, like the rest of what is not understood.
UNKNOWN_FIELD_BIT = 152

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


def with_drop(
    command: bytes,
    actor: bytes,
    position: tuple[float, float, float],
    template: str | None = None,
) -> bytes:
    """Return *command* with a fresh actor, a new resting place, and maybe an item.

    A name the client cannot resolve creates nothing, exactly as an unknown monster
    blueprint does; ``_Template_Item`` is what says whether one is real.
    """
    if len(actor) != 4:
        raise ValueError(f"an actor id is four bytes, got {len(actor)}")
    out = command if template is None else with_template(command, template)
    out = _write_bits(out, ACTOR_BIT, actor)
    return _write_bits(out, _position_bit(out), struct.pack("<3f", *position))


#: Bit offset of the actor id in a pickup reply: past the message id, the command
#: id, and one leading field the ground command does not have.
PICKUP_ACTOR_BIT = 56


def item_info_unprompted() -> bytes:
    """A standalone 0x002F the real server sent with nobody having asked.

    Kept as evidence against the reading it once supported. This one names actor
    07 00 01 00 and arrived *before* any request for that actor — the pickup just
    before it was for 06 00 01 00. So ItemInfoCommand is the server describing an
    item of its own accord, not an answer to a pickup.

    Of the four pickups in that capture, two drew an ItemInfoCommand — only one of
    them naming the actor asked for — and two drew nothing whatsoever. Which leaves
    open the possibility that a successful pickup needs no reply at all.
    """
    return (_DATA / "item_info_unprompted.bin").read_bytes()


def pickup_actor(command: bytes) -> bytes:
    return _read_bits(command, PICKUP_ACTOR_BIT, 4)


def with_pickup(command: bytes, actor: bytes) -> bytes:
    """Return *command* addressed to a different item."""
    if len(actor) != 4:
        raise ValueError(f"an actor id is four bytes, got {len(actor)}")
    return _write_bits(command, PICKUP_ACTOR_BIT, actor)


#: The whole reply the real server sent, not just the command inside it: a 0x004F
#: batch carrying a LocationEffectInfoCommand and then the ItemInfoCommand.
#:
#: Replaying the command alone was byte-for-byte right and did nothing. That is a
#: shape this project has already met once: a skill command identical to a real one
#: also did nothing standalone, and worked the moment it travelled inside the batch
#: the capture put it in. So framing is tried before anything cleverer.
PICKUP_BATCH_ACTOR_BIT = 2379


def item_pickup_batch() -> bytes:
    """The recorded 0x004F batch that answers a pickup."""
    return (_DATA / "item_pickup_batch.bin").read_bytes()


def batch_pickup_actor(batch: bytes) -> bytes:
    return _read_bits(batch, PICKUP_BATCH_ACTOR_BIT, 4)


def with_pickup_batch(batch: bytes, actor: bytes) -> bytes:
    """Return the recorded reply, aimed at a different item.

    One field, the actor. The client's own log shows it calls that the item id.
    """
    if len(actor) != 4:
        raise ValueError(f"an actor id is four bytes, got {len(actor)}")
    return _write_bits(batch, PICKUP_BATCH_ACTOR_BIT, actor)


#: The actor the recorded reply is about. It names that item in **three** places,
#: and all three have to move together:
#:
#:   bit 24    the DiscardItemCommand — take it off the ground
#:   bit 145   the inventory's first item record
#:   bit 1508  the allocation table, which says what occupies which slot
#:
#: The third is the one that was missing, and the client said so by name:
#:
#:   *** NEBULA ASSERTION ***  InvalidIndex != outItemWithLocation.primarySlotIdx
#:   ClientInventoryManager::LocateItem(...)
#:
#: It had found the item and no slot claiming it. The offsets are found by searching
#: for the value and reading each hit back, not by arithmetic: the allocation
#: table's position depends on the rest of the inventory, so there is nothing to
#: compute it from.
RECORDED_TAKEN_ACTOR = bytes([0x04, 0x00, 0x01, 0x00])

def item_taken() -> bytes:
    """The real answer to a pickup: 8.4 KB, fragmented, and easy to miss.

    Every one of the four pickups in the capture is answered by this and nothing
    else. It went unseen for a long time because it arrives in fragments and this
    project's capture analysis skipped fragmented frames — the same blind spot that
    hid the experience message earlier.

    The chain inside is what the client was waiting for, and explains why it kept
    retrying the click:

        DiscardItemCommand   the item's actor — take it off the ground
        InventoryInfoCommand the whole inventory, with the item now in it
        StatusEffectCommand

    Two readings this refutes, both stated confidently here beforehand: that a
    successful pickup might need no reply, and that the real server never sends a
    DiscardItemCommand. It sends one every time.

    Only the discarded actor is rewritten. The inventory is replayed as recorded, so
    the bag shows that session's contents rather than what was actually picked up —
    a real limit, not a fix, and the next thing to do properly.
    """
    return (_DATA / "item_taken.bin").read_bytes()


def taken_actor_bits(reply: bytes, actor: bytes = RECORDED_TAKEN_ACTOR) -> list[int]:
    """Every bit offset at which *actor* appears in *reply*.

    Searched rather than computed. The offsets are not byte-aligned and one of them
    sits in a table whose position depends on the rest of the inventory, so
    arithmetic on the command header is exactly how the wrong one got written.
    """
    size = len(reply)
    value = int.from_bytes(reply, "big")
    found = []
    for shift in range(8):
        window = ((value << shift) & ((1 << (8 * size)) - 1)).to_bytes(size, "big")
        at = window.find(actor)
        while at >= 0:
            # Shifting left by `shift` moves bit p to p - shift, so a hit at bit q
            # in the window was at q + shift in the original. Subtracting instead
            # of adding is what wrote the actor two bits early and made the client
            # assert on a slot it could not find.
            found.append(at * 8 + shift)
            at = window.find(actor, at + 1)
    # Every offset is then confirmed by reading it back. A 32-bit value can be
    # matched at more than one alignment by coincidence, and a spurious write into
    # a bit-packed inventory corrupts whatever field it lands in.
    return sorted(
        bit for bit in set(found) if _read_bits(reply, bit, 4) == actor
    )


def with_taken(
    reply: bytes,
    actor: bytes,
    template: str | None = None,
    slot: int | None = None,
) -> bytes:
    """Return the reply, taking a different item off the ground and into the bag.

    Every mention of the recorded item's actor becomes *actor*: the ground removal,
    the inventory record, and the slot allocation. Everything else is replayed,
    including the rest of the inventory, so the bag also shows the recorded
    session's contents — a known limit, not a fix.
    """
    if len(actor) != 4:
        raise ValueError(f"an actor id is four bytes, got {len(actor)}")
    out = reply if template is None else with_taken_template(reply, template)
    for bit in taken_actor_bits(out):
        out = _write_bits(out, bit, actor)
    if slot is not None:
        out = with_single_allocation(out, actor, slot)
    return out


#: The template the recorded reply's item record carries, at bit 515 — inside the
#: record, length-prefixed, and not byte-aligned, which is why a byte-level search
#: of the message found no item names at all and this was mistaken for an inventory
#: that referenced its items purely by number.
#:
#: It is a sword, which is precisely what appeared in the bag when only the actor
#: was rewritten.
RECORDED_TAKEN_TEMPLATE = "warrior_base_rh_sword_speedAttack_damage"


def _find_string(buf: bytes, text: str) -> int | None:
    """The bit offset of *text*'s length prefix in *buf*, at any alignment."""
    want = len(text).to_bytes(2, "little") + text.encode("ascii")
    size = len(buf)
    value = int.from_bytes(buf, "big")
    for shift in range(8):
        window = ((value << shift) & ((1 << (8 * size)) - 1)).to_bytes(size, "big")
        at = window.find(want)
        while at >= 0:
            bit = at * 8 + shift
            if _read_bits(buf, bit, len(want)) == want:
                return bit
            at = window.find(want, at + 1)
    return None


def taken_template(reply: bytes) -> str | None:
    """The template the reply's item record names."""
    bit = _find_string(reply, RECORDED_TAKEN_TEMPLATE)
    return None if bit is None else RECORDED_TAKEN_TEMPLATE


def with_taken_template(reply: bytes, template: str) -> bytes:
    """Return *reply* whose item record names a different blueprint.

    The name is length-prefixed and everything behind it shifts, so the bit stream
    is spliced rather than overwritten — the message gets longer or shorter. That is
    fine: the reader takes the length from the prefix, and the arrays after it are
    simply concatenated.
    """
    bit = _find_string(reply, RECORDED_TAKEN_TEMPLATE)
    if bit is None:
        raise ValueError("the recorded template is not where it was")
    old = len(RECORDED_TAKEN_TEMPLATE)
    raw = template.encode("ascii")
    stream = _bits(reply)
    head = stream[:bit]
    tail = stream[bit + 16 + 8 * old :]
    return _bytes(head + _bits(len(raw).to_bytes(2, "little")) + _bits(raw) + tail)


#: Where the inventory command's body starts inside the recorded reply: past the
#: 0x85, the DiscardItemCommand's id, its actor, its terminator and the 0x0054.
INVENTORY_BODY_BIT = 113


def _u32(buf: bytes, bit: int) -> int:
    return int.from_bytes(_read_bits(buf, bit, 4), "little")


def inventory_layout(reply: bytes) -> dict[str, int]:
    """Walk the inventory and return where its parts begin.

    The grammar comes from the client's own serialiser — twelve count-prefixed
    arrays then nine scalars — and is validated by this walk landing exactly on the
    command's terminator. Offsets are computed rather than written down because the
    item record contains a length-prefixed template name, so everything behind it
    moves when the name does.
    """
    bit = INVENTORY_BODY_BIT
    records = _u32(reply, bit)
    bit += 32
    record_at = bit
    # One record. Its length is not fixed, so it is measured from the two arrays
    # that follow: walking from the far side is not possible, so the record's size
    # is taken as given for the single-record case this serves.
    if records != 1:
        raise ValueError(f"expected one item record, found {records}")
    bit += _RECORD_BITS + (_template_length(reply) - len(RECORDED_TAKEN_TEMPLATE)) * 8
    storages_at = bit
    count = _u32(reply, bit)
    bit += 32
    for _ in range(count):
        bit += 32
        bytes_in = _u32(reply, bit)
        bit += 32 + 8 * bytes_in
    allocations_at = bit
    return {
        "record": record_at,
        "storages": storages_at,
        "allocations": allocations_at,
    }


#: Bits in the recorded item record, template name included.
_RECORD_BITS = 1091


def _template_length(reply: bytes) -> int:
    """The length of the template name the record carries."""
    for name in (RECORDED_TAKEN_TEMPLATE,):
        if _find_string(reply, name) is not None:
            return len(name)
    # Rewritten already: find any length-prefixed name inside the record.
    for bit in range(INVENTORY_BODY_BIT, INVENTORY_BODY_BIT + 1600):
        length = int.from_bytes(_read_bits(reply, bit, 2), "little")
        if not 8 <= length <= 90:
            continue
        text = _read_bits(reply, bit + 16, length)
        if all(32 <= ch < 127 for ch in text) and b"_" in text:
            return length
    raise ValueError("no template name in the item record")


def allocations(reply: bytes) -> list[tuple[bytes, int]]:
    """The (item, slot) pairs the reply hands the client."""
    at = inventory_layout(reply)["allocations"]
    count = _u32(reply, at)
    out = []
    for index in range(count):
        base = at + 32 + 64 * index
        out.append((_read_bits(reply, base, 4), _u32(reply, base + 32)))
    return out


def with_single_allocation(reply: bytes, actor: bytes, slot: int) -> bytes:
    """Keep one allocation — *actor* in *slot* — and drop the rest.

    Two reasons. The recorded reply allocates a second item, one belonging to the
    session it came from, and applying that moves whatever the player has in the
    slot it claims: an equipped sword ends up in the bag. And the recorded slot is
    always the same one, so a second pickup would stack on the first, which crashes
    the client.
    """
    at = inventory_layout(reply)["allocations"]
    count = _u32(reply, at)
    stream = _bits(reply)
    head = stream[:at]
    tail = stream[at + 32 + 64 * count :]
    entry = _bits(actor) + _bits((slot & 0xFFFFFFFF).to_bytes(4, "little"))
    return _bytes(head + _bits((1).to_bytes(4, "little")) + entry + tail)
