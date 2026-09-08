"""``InventoryInfoCommand``, 0x0054, and the item records inside it.

Read out of the client's own decoder rather than guessed at. ``Commands::InventoryInfoCommand``
registers its Rtti at ``+0xabefc`` with the fourcc ``'IvIC'``, whose creator leads to a
constructor that plants the vtable at ``0x14115fcd8``; slot 5 of that vtable -- the slot
``DrasaClientHandler::DecodeCommand`` calls for a command's own fields -- is
``+0x9671e8``. That function is a flat run of reader calls, one per member, and it is
the grammar:

    array<ItemInfo>            +0x20    the records being merged
    dict<u32, array<int8>>     +0x30    item -> the cells it occupies
    dict<u32, u32>             +0x58    item -> slot in the bag
    dict<u32, u32>             +0x80
    dict<u32, u32>             +0xa8    item -> equipment slot
    dict<u32, u32>             +0xd0
    array<u32>                 +0xf8
    array<u32>                 +0x118
    array<u32>                 +0x138
    array<u32, u32>            +0x158
    array<u32>                 +0x180
    array<string>              +0x1a0
    6 x u32, 2 x u32, 1 bit    +0x1c0 .. +0x1e0

Every count is 32 bits and refused above 1,000,000 (``cmp ecx, 0xf4240``), so a count
this server writes has that as its ceiling too.

The proof that the transcription is right is that it lands, to the bit, on the 32-bit
actor and the ``0xFF`` that end every command: the live service's two pickup replies in
``officiel4`` are 126,808 and 127,768 bits long and the walk ends at 126,768 and
127,728. Nothing about a BitStream makes that a coincidence -- a layout one bit out
drifts and dies inside the first string.

**What it cost to not know this.** The pickup reply used to be replayed with its fields
found by searching for the recorded item's actor and by arithmetic on the command
header. Both were wrong in the same place. The real allocation array is at bit 1,764 of
that message; the code wrote its own at bit **1,412**, which is where ``+0x58`` --
item to bag slot -- begins, and spliced, shifting every field behind it. Two bad
consequences, both of which the operator reported and neither of which had an
explanation until now:

* the bag showed the recording's sword instead of what was picked up, because the item
  record was replayed whole and only its name was ever rewritten;
* health fell to 200, because ``+0xa8`` -- **fourteen entries, one per equipment slot**
  -- moved 64 bits and stopped naming anything, so the client recomputed the character
  from a naked body.

The dictionary at ``+0xa8`` having exactly fourteen entries in the live capture, against
the fourteen slots a character wears, is what named it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field, replace as _replace

from raknet.bitstream import BitReader, BitWriter
from raknet.payload import Payload, bits_of

#: The chained container the reply travels in.
MULTI = 0x85

#: ``DiscardItemCommand`` -- take the item off the ground. It leads the reply.
DISCARD_ITEM = 0x002E

#: ``InventoryInfoCommand``.
INVENTORY_INFO = 0x0054

#: Where the 0x002E's item id sits: past the 0x85 and the command id.
DISCARDED_AT = 24

#: Where the 0x0054 body begins in the live service's recorded reply: the 0x002E's id,
#: its item, its actor and its terminator, then the next command's id.
#:
#: A starting point for the search in :func:`body_at` and **not** a constant, which is
#: the second half of the same lesson: this is 113 in the recorded reply and 112 in the
#: chain :func:`taken` builds, because the chain is bit-packed and where a command
#: begins depends on the length of the one in front of it. A number measured on one
#: message is a number about that message.
BODY_AT = 113
#: 113 and not 112 in the recording, which cost a live session to find. The chain is
#: bit-packed, so a command boundary need not be byte aligned, and the ``0x0054`` id in
#: the recorded reply occupies bits **97 to 112**::
#:
#:     16 bits from 96:  1010101000000000  = 0x00aa
#:     16 bits from 97:  0101010000000000  = 0x0054   <-- the id
#:
#: Measured one bit early, the reader then needed a leading one-bit field to swallow the
#: difference --- and it had one, ``Inventory.header``, which decoded as 0 in both
#: recorded replies and re-encoded faithfully, so every round-trip test passed. It was a
#: fiction. **A bit-for-bit round-trip proves the copy is faithful, not that the grammar
#: is aligned**: decode and encode agreed with each other about a field that was not
#: there.
#:
#: It only showed when this module started *building* a command instead of editing one.
#: The client reads 641 body bits for an empty bag --- twelve 32-bit counts, six 32-bit
#: scalars, two floats and one bit, all transcribed from ``Deserialize`` at
#: ``0x1409671e8`` --- and said so out loud::
#:
#:     Received InventoryInfoCommand with unknown or invalid actor id (2155872384)!
#:
#: 2155872384 is 0x80800080, which is this server's actor 0x00010100 read **one bit
#: early** with a set bit in front of it.

#: The client's own ceiling on every count it reads (``cmp ecx, 0xf4240``).
MOST = 1_000_000

#: A count of statistics or names on one item is a byte, so this is that ceiling.
MOST_ON_AN_ITEM = 255

#: Trailing every command: the actor, then this.
TERMINATOR = 0xFF

#: The actor and the terminator behind a command's body.
ACTOR_BITS = 32
TAIL_BITS = ACTOR_BITS + 8

#: The high halves an actor id is ever seen with. Page 1 is the map server's own
#: numbering and page 2 the live service's; anything else in that half means the four
#: bytes read as an actor are not one. The same test as dsor.recorded.ACTOR_PAGES, and
#: it is here for the same reason -- to keep a run of 0xFF from reading as a boundary.
ACTOR_PAGES = (0, 1, 2)


def _read_string(reader: BitReader) -> str:
    """A 16-bit length and that many bytes.

    Latin-1 rather than UTF-8 so that any byte survives the round trip; the names in
    this protocol are ASCII, and a decoder that raises on the one that is not would
    lose a message rather than replay it.
    """
    count = reader.read_uint(16)
    return bytes(reader.read_bits(8) for _ in range(count)).decode("latin-1")


def _write_string(writer: BitWriter, text: str) -> None:
    raw = text.encode("latin-1")
    writer.write_uint(len(raw), 16)
    writer.write_bytes(raw)


def _read_float(reader: BitReader) -> float:
    return struct.unpack("<f", reader.read_uint(32).to_bytes(4, "little"))[0]


def _write_float(writer: BitWriter, value: float) -> None:
    writer.write_uint(int.from_bytes(struct.pack("<f", value), "little"), 32)


def _read_signed(reader: BitReader) -> int:
    value = reader.read_bits(8)
    return value - 256 if value & 0x80 else value


def _write_signed(writer: BitWriter, value: int) -> None:
    writer.write_bits(value & 0xFF, 8)


@dataclass
class Statistic:
    """One line of an item's own numbers.

    The name is the attribute's, and the value is a float -- which is how a percentage
    and a flat bonus share a field.
    """

    name: str
    value: float
    kind: int
    third: int
    fourth: int


@dataclass
class Item:
    """``Game::ItemInfo``, in the order ``+0x92422c`` reads it.

    Most fields keep the offset they have in the client's structure for a name, because
    naming them from a single capture is how this project has been wrong before. Four
    are established: ``id`` is the item's actor, ``template`` its blueprint,
    ``stamped`` the year, month, day, hour, minute and second it was acquired -- read
    straight out of the live capture as 2026, 9, 1, 22, 34, 8 for a gem picked up at
    22:34 that day -- and ``position`` where it lay on the ground.
    """

    id: int
    template: str
    kind: int = 0
    second: int = 0
    third: int = 0
    tenth: int = 0
    eleventh: int = 0
    twelfth: int = 0
    eighteenth: int = 0
    twentieth: int = 0
    twenty_eighth: int = 0
    named: str = "none"
    #: The item's level. 125 on the torso the operator picked up, and the same 125
    #: appears in every one of that record's statistics -- which is what named it.
    level: int = 1
    thirtieth: int = 0
    fifty_second: int = 0
    flags: tuple[int, int, int] = (0, 0, 1)
    second_name: str = ""
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    third_name: str = ""
    #: A tier. 5 on that torso, and again the same 5 in each of its statistics.
    tier: int = 0
    fourth_name: str = ""
    statistics: list[Statistic] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    stamped: tuple[int, int, int, int, int, int] = (0, 0, 0, 0, 0, 0)


def _read_item(reader: BitReader) -> Item:
    id_ = reader.read_uint(32)
    second = reader.read_uint(32)
    third = reader.read_uint(32)
    kind = reader.read_bits(8)
    tenth = reader.read_bits(8)
    eleventh = reader.read_bits(8)
    twelfth = reader.read_bits(8)
    eighteenth = reader.read_uint(32)
    twentieth = reader.read_uint(32)
    twenty_eighth = reader.read_uint(32)
    named = _read_string(reader)
    level = reader.read_uint(32)
    thirtieth = reader.read_uint(32)
    fifty_second = reader.read_uint(32)
    first_flag = reader.read_bits(1)
    second_flag = reader.read_bits(1)
    template = _read_string(reader)
    second_name = _read_string(reader)
    position = (_read_float(reader), _read_float(reader), _read_float(reader))
    third_name = _read_string(reader)
    tier = reader.read_uint(32)
    fourth_name = _read_string(reader)
    third_flag = reader.read_bits(1)
    statistics = []
    for _ in range(reader.read_bits(8)):
        value = _read_float(reader)
        line_kind = _read_signed(reader)
        line_third = reader.read_uint(32)
        line_fourth = reader.read_uint(32)
        statistics.append(
            Statistic(_read_string(reader), value, line_kind, line_third, line_fourth)
        )
    names = [_read_string(reader) for _ in range(reader.read_bits(8))]
    stamped = tuple(reader.read_uint(32) for _ in range(6))
    return Item(
        id=id_,
        template=template,
        kind=kind,
        second=second,
        third=third,
        tenth=tenth,
        eleventh=eleventh,
        twelfth=twelfth,
        eighteenth=eighteenth,
        twentieth=twentieth,
        twenty_eighth=twenty_eighth,
        named=named,
        level=level,
        thirtieth=thirtieth,
        fifty_second=fifty_second,
        flags=(first_flag, second_flag, third_flag),
        second_name=second_name,
        position=position,
        third_name=third_name,
        tier=tier,
        fourth_name=fourth_name,
        statistics=statistics,
        names=names,
        stamped=stamped,  # type: ignore[arg-type]
    )


def _write_item(writer: BitWriter, item: Item) -> None:
    if len(item.statistics) > MOST_ON_AN_ITEM:
        raise ValueError(
            f"an item carries at most {MOST_ON_AN_ITEM} statistics, "
            f"got {len(item.statistics)}"
        )
    if len(item.names) > MOST_ON_AN_ITEM:
        raise ValueError(
            f"an item carries at most {MOST_ON_AN_ITEM} names, got {len(item.names)}"
        )
    writer.write_uint(item.id, 32)
    writer.write_uint(item.second, 32)
    writer.write_uint(item.third, 32)
    writer.write_bits(item.kind, 8)
    writer.write_bits(item.tenth, 8)
    writer.write_bits(item.eleventh, 8)
    writer.write_bits(item.twelfth, 8)
    writer.write_uint(item.eighteenth, 32)
    writer.write_uint(item.twentieth, 32)
    writer.write_uint(item.twenty_eighth, 32)
    _write_string(writer, item.named)
    writer.write_uint(item.level, 32)
    writer.write_uint(item.thirtieth, 32)
    writer.write_uint(item.fifty_second, 32)
    writer.write_bits(item.flags[0], 1)
    writer.write_bits(item.flags[1], 1)
    _write_string(writer, item.template)
    _write_string(writer, item.second_name)
    for value in item.position:
        _write_float(writer, value)
    _write_string(writer, item.third_name)
    writer.write_uint(item.tier, 32)
    _write_string(writer, item.fourth_name)
    writer.write_bits(item.flags[2], 1)
    writer.write_bits(len(item.statistics), 8)
    for line in item.statistics:
        _write_float(writer, line.value)
        _write_signed(writer, line.kind)
        writer.write_uint(line.third, 32)
        writer.write_uint(line.fourth, 32)
        _write_string(writer, line.name)
    writer.write_bits(len(item.names), 8)
    for name in item.names:
        _write_string(writer, name)
    for value in item.stamped:
        writer.write_uint(value, 32)


@dataclass
class Inventory:
    """The command's twelve collections and nine scalars.

    ``placements`` and ``equipment`` are the two that have been read: the first maps an
    item to its cell in the bag, the second an item to the slot it is worn in, and the
    live capture's fourteen entries in the second against a character's fourteen
    equipment slots is what named it. The rest keep the offset they have in the client.
    """

    items: list[Item] = field(default_factory=list)
    slots: list[tuple[int, list[int]]] = field(default_factory=list)
    placements: list[tuple[int, int]] = field(default_factory=list)
    eightieth: list[tuple[int, int]] = field(default_factory=list)
    equipment: list[tuple[int, int]] = field(default_factory=list)
    two_hundred_eighth: list[tuple[int, int]] = field(default_factory=list)
    two_hundred_forty_eighth: list[int] = field(default_factory=list)
    two_hundred_eightieth: list[int] = field(default_factory=list)
    three_hundred_twelfth: list[int] = field(default_factory=list)
    allocations: list[tuple[int, int]] = field(default_factory=list)
    three_hundred_eighty_fourth: list[int] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    scalars: tuple[int, ...] = (0, 0, 0, 0, 0, 0, 0, 0)
    flag: int = 1


#: Where the body starts in a message that **is** an inventory command rather than one
#: carrying it behind a DiscardItemCommand: past the 0x85 and the id.
BODY_AT_ALONE = 24


#: How far into a message :func:`body_at` will look for the command id. Both known
#: shapes put it inside the first sixteen bytes; a bound keeps the search from turning
#: a malformed message into a scan of the whole thing.
SEARCH_BITS = 128


def body_at(payload: bytes) -> int:
    """Where *payload*'s inventory body begins, whichever shape it is.

    Searched and **verified**, not looked up. There were two constants here, 24 for a
    standalone command and 112 for the live service's ``0x002E`` + ``0x0054`` reply, and
    the second was one bit wrong -- the recorded reply's id sits at bits 97 to 112, so
    its body starts at 113, while the chain :func:`taken` builds is byte aligned and
    starts at 112. Neither number is *the* number: the chain is bit-packed, so where the
    inventory command begins depends on how long the command in front of it was.

    So: find the id, decode from behind it, and accept the offset only if the body ends
    on a command boundary -- an actor with the ``0xFF`` behind it. That last clause is
    what makes it a measurement instead of a guess, and it is the same test
    :func:`dsor.recorded._real_tail` needs for the same reason: a plausible offset in a
    bit-packed stream is easy to come by and means nothing on its own.

    Two passes, because the strongest evidence is not always available. An offset whose
    body closes on the **message's own end** is taken first and without argument. Only
    if none does is a mid-message boundary accepted, and one recording needs that: the
    live service batches a ``StatusEffectCommand`` behind the inventory, so there the
    command ends in the middle. A mid-message ``0xFF`` is the weaker claim -- it is
    exactly the shape of the false tail that put the arrival boundary 584 bits early --
    so it also demands a plausible actor rather than any four bytes.
    """
    # The declared length and not the byte length: a message this server built can
    # carry padding bits, and 705 bits in 89 bytes puts the actor seven bits from where
    # the bytes would.
    total = bits_of(payload)
    boundary = None
    for at in range(min(SEARCH_BITS, max(0, total - 16)) + 1):
        if BitReader(payload, at).read_uint(16) != INVENTORY_INFO:
            continue
        try:
            _got, ends = decode(payload, at + 16)
        except (ValueError, IndexError):
            continue
        if ends + TAIL_BITS > total:
            continue
        if BitReader(payload, ends + ACTOR_BITS).read_uint(8) != TERMINATOR:
            continue
        if ends + TAIL_BITS == total:
            return at + 16
        actor = BitReader(payload, ends).read_uint(ACTOR_BITS)
        if boundary is None and actor >> 16 in ACTOR_PAGES:
            boundary = at + 16
    if boundary is not None:
        return boundary
    raise ValueError("no inventory command in this message that ends on a boundary")


def decode(payload: bytes, at: int | None = None) -> tuple[Inventory, int]:
    """The command's body starting at bit *at*, and the bit it ends on.

    The returned position is where the command's actor begins, so a caller can check
    the ``0xFF`` behind it -- which is the whole proof that the walk is right.
    """
    reader = BitReader(payload, body_at(payload) if at is None else at)
    got = Inventory()

    def count() -> int:
        found = reader.read_uint(32)
        if found > MOST:
            raise ValueError(f"a count of {found} is past the client's own ceiling")
        return found

    got.items = [_read_item(reader) for _ in range(count())]
    for _ in range(count()):
        key = reader.read_uint(32)
        got.slots.append((key, [_read_signed(reader) for _ in range(count())]))
    for name in ("placements", "eightieth", "equipment", "two_hundred_eighth"):
        setattr(
            got,
            name,
            [(reader.read_uint(32), reader.read_uint(32)) for _ in range(count())],
        )
    for name in (
        "two_hundred_forty_eighth",
        "two_hundred_eightieth",
        "three_hundred_twelfth",
    ):
        setattr(got, name, [reader.read_uint(32) for _ in range(count())])
    got.allocations = [
        (reader.read_uint(32), reader.read_uint(32)) for _ in range(count())
    ]
    got.three_hundred_eighty_fourth = [reader.read_uint(32) for _ in range(count())]
    got.names = [_read_string(reader) for _ in range(count())]
    got.scalars = tuple(reader.read_uint(32) for _ in range(8))
    got.flag = reader.read_bits(1)
    return got, reader.position


def encode(got: Inventory) -> BitWriter:
    """The body, as bits. No id, no actor, no terminator -- see :func:`rebuild`."""
    writer = BitWriter()

    def count(items) -> None:
        if len(items) > MOST:
            raise ValueError(
                f"{len(items)} is past the client's own ceiling of {MOST}"
            )
        writer.write_uint(len(items), 32)

    count(got.items)
    for item in got.items:
        _write_item(writer, item)
    count(got.slots)
    for key, shape in got.slots:
        writer.write_uint(key, 32)
        count(shape)
        for value in shape:
            _write_signed(writer, value)
    for name in ("placements", "eightieth", "equipment", "two_hundred_eighth"):
        pairs = getattr(got, name)
        count(pairs)
        for first, second in pairs:
            writer.write_uint(first, 32)
            writer.write_uint(second, 32)
    for name in (
        "two_hundred_forty_eighth",
        "two_hundred_eightieth",
        "three_hundred_twelfth",
    ):
        values = getattr(got, name)
        count(values)
        for value in values:
            writer.write_uint(value, 32)
    count(got.allocations)
    for first, second in got.allocations:
        writer.write_uint(first, 32)
        writer.write_uint(second, 32)
    count(got.three_hundred_eighty_fourth)
    for value in got.three_hundred_eighty_fourth:
        writer.write_uint(value, 32)
    count(got.names)
    for name in got.names:
        _write_string(writer, name)
    if len(got.scalars) != 8:
        raise ValueError(f"the command ends with eight scalars, got {len(got.scalars)}")
    for value in got.scalars:
        writer.write_uint(value, 32)
    writer.write_bits(got.flag, 1)
    return writer


def _bits_of(payload: bytes) -> str:
    return "".join(f"{byte:08b}" for byte in payload)


def _to_bytes(bits: str) -> bytes:
    pad = -len(bits) % 8
    return int(bits + "0" * pad, 2).to_bytes((len(bits) + pad) // 8, "big")


def rebuild(payload: bytes, got: Inventory, at: int | None = None) -> Payload:
    """*payload* with the inventory at bit *at* replaced by *got*.

    Everything before and after is carried across bit for bit, which is the point:
    the reply this server sends is a real one from the live service, and the parts of
    it that are not understood -- the storage shapes, the equipment map, the strings,
    the trailing status effect -- stay exactly as they were recorded. Only what is
    understood is rewritten, and it is rewritten by re-encoding the whole collection
    rather than by splicing bytes into it. Splicing is what shifted the equipment map
    and took the character's health with it.
    """
    at = body_at(payload) if at is None else at
    _, ends = decode(payload, at)
    stream = _bits_of(payload)
    fresh = "".join(str(bit) for bit in encode(got)._bits)  # noqa: SLF001
    spliced = stream[:at] + fresh + stream[ends:]
    return Payload(_to_bytes(spliced), bits_of(payload) + len(fresh) - (ends - at))


#: ``InventoryCommand``, 0x0055, which the client sends when an item leaves the cell it
#: was in. Equipping one is the case that matters, and it appears in no capture -- it was
#: found by logging the body and then reading the client's decoder for it.
MOVE_ITEM = 0x0055

#: Its grammar, from ``InventoryCommand::Decode`` at ``+0x96713c``:
#:
#:     +0x20   u32           the item
#:     +0x24   int8          an operation, checked against 34 in the client
#:     +0x28   array         a count and that many elements -- empty in every sample
#:     +0x48   u32           zero in every sample
#:     +0x4c   u32           zero in every sample
#:
#: Which comes to 17 bytes with an empty array, and 17 bytes is exactly what the client
#: sent twice while the operator equipped two items:
#:
#:     00 01 01 00  01  00 00 00 00  00 00 00 00  00 00 00 00
#:     01 01 01 00  01  00 00 00 00  00 00 00 00  00 00 00 00
#:
#: Both name an item this server had just handed out, and both carry operation 1.
MOVE_ITEM_SIZE = 17

#: What the client sent for an equip. One of at most 34 the enum allows; the rest have
#: not been seen, so nothing here treats this value as special.
EQUIP = 1


def moved(body: bytes) -> tuple[int, int] | None:
    """The item and the operation in a 0x0055, or None if it is not one.

    Only the first two fields, because they are the two that are established and the
    two that matter: which item left, and by what means.
    """
    if len(body) < 5:
        return None
    return int.from_bytes(body[:4], "little"), body[4]


def discarded(payload: bytes) -> int:
    """The item the leading ``DiscardItemCommand`` takes off the ground."""
    return BitReader(payload, DISCARDED_AT).read_uint(32)


def with_discarded(payload: bytes, actor: int) -> Payload:
    """*payload* whose ``DiscardItemCommand`` names *actor*.

    One fixed-width field at a known offset, so this is a write and not a search. The
    search it replaces rewrote **every** 32-bit window in 8.4 KB that happened to
    match the recorded item, the trailing status effect included.
    """
    stream = _bits_of(payload)
    fresh = "".join(f"{byte:08b}" for byte in actor.to_bytes(4, "little"))
    spliced = stream[:DISCARDED_AT] + fresh + stream[DISCARDED_AT + 32 :]
    return Payload(_to_bytes(spliced), bits_of(payload))


#: Where an item's cell can be stated, and the storage each one means.
#:
#: Read out of ``ClientInventoryManager::LocateItem`` at ``+0x1fea1c``, which is the
#: function whose assertion started all of this. It looks the item up in five
#: collections in a fixed order and writes a number into the location it fills, and that
#: number is the storage:
#:
#:     +0x30    storage 1     dict of item -> the cells it occupies
#:     +0x80    storage 2     dict of item -> one cell
#:     +0xa8    storage 3     dict of item -> one cell
#:     +0xd0    storage 4     dict of item -> one cell
#:     +0x58    storage 0     dict of item -> one cell
#:     +0x180   storage 7     an array, searched by value
#:
#: So ``+0x58`` -- the one this server wrote into for weeks -- is **storage zero**, and
#: the operator's measurement says what storage zero is: "si je drop 10 items j'en
#: equippe 9 le 10eme sera au slot 14". It is the character.
#:
#: ``+0x30`` is the one the client looks in **first**, and it is the only one whose
#: value is a list: ``slots[0]`` becomes the primary cell and ``slots[1]`` the secondary,
#: which is how an item that occupies two cells is stated. The live service's pickup
#: replies carry 23 entries there.
#:
#: Which of them is the backpack is still a rule rather than a constant, because the
#: tag numbers are the client's own and nothing read so far says which number the
#: backpack has. But the order is no longer a guess, and neither is the shape.
LAYOUTS = ("slots", "eightieth", "equipment", "two_hundred_eighth", "placements")

#: Where a picked-up item's cell goes by default: the collection the client searches
#: first, and the only one that can state more than one cell.
DEFAULT_LAYOUT = "slots"

#: Index of the current health among the trailing scalars, and of the value that
#: travels beside it.
#:
#: Read off the live service by bracketing. The second pickup in ``officiel4`` carries
#: 2,634,612.5 here, and the two ``ActorStatsUpdateCommand`` messages either side of it
#: carry 2,616,196 and 2,639,085 for the same actor -- the inventory's figure sits
#: between them. The first pickup agrees: 2,721,042.75 against 2,698,962 just before.
#:
#: Which is why picking anything up used to collapse the health bar. The reply this
#: server replays was recorded from a **level 1** character and carries 236.13 here, so
#: every pickup told the client the player had 236 health. The operator reported it as
#: "ça me baisse ma vie a 200".
HEALTH = 6
BESIDE_HEALTH = 7


#: What a from-scratch item record leaves at its dataclass default, because nothing
#: read so far says what any of them mean.
#:
#: Written down because they are the first suspects if the client refuses a built
#: record, and because each one is a single field-copy away from the recording:
#: ``kind``, ``second``, ``third``, ``tenth``, ``eleventh``, ``twelfth``,
#: ``eighteenth``, ``twentieth``, ``twenty_eighth``, ``named``, ``thirtieth``,
#: ``fifty_second``, the three ``flags`` bits, and ``second_name`` / ``third_name`` /
#: ``fourth_name``. Observed values for two real items, for comparison: the recorded
#: sword has ``kind=0, third=1, eighteenth=31633, thirtieth=14, flags=(0,0,1)`` and the
#: live torso ``kind=2, tenth=6, eighteenth=29803, thirtieth=14, fifty_second=9477,
#: flags=(1,0,1), third_name='warrior_torso_pw'``.
OPAQUE = (
    "kind", "second", "third", "tenth", "eleventh", "twelfth", "eighteenth",
    "twentieth", "twenty_eighth", "named", "thirtieth", "fifty_second", "flags",
    "second_name", "third_name", "fourth_name",
)

#: The six leading scalars. Five of them are **sizes the client resizes its storages
#: to**, which is not a guess: ``ClientInventoryManager::ResizeStorages`` at
#: ``+0x20050c`` reads ``+0x1c0`` through ``+0x1d0`` in turn and hands each to a
#: different setter on the player's inventory.
#:
#: Which size is which is read off the two recordings rather than named:
#:
#:     level 1     (1, 1,  21, 10, 0, 0)   two items in the bag
#:     level 100   (26, 2, 196, 15, 0, 0)  152 items, cells running to 161
#:
#: So the third tracks the bag — 21 against 196 — and the first a count of storages.
#: An inventory that declares zeroes gives the interface no cells at all, and this
#: project has already met the crash that follows:
#: ``Util::FixedArray<Core::Ptr<UI::Slot>>::operator[]``.
LEADING_SCALARS = (1, 1, 21, 10, 0, 0)

#: Which of them the bag's capacity is.
CAPACITY = 2


def empty(
    actor: int,
    capacity: int,
    health: float | None = None,
    resource: float | None = None,
) -> Payload:
    """An ``InventoryInfoCommand`` for a character who owns nothing.

    Sent on arrival, because the trimmed arrival carries no inventory command at all and
    a client whose bag was never described crashes the moment it is opened. What this
    states is the sizes and nothing else: no items, no placements, no equipment, none of
    the 247 template strings the recorded reply carries.

    *capacity* is written into :data:`CAPACITY` so the bag the client draws and the cells
    this server hands out are the same number -- ``Rules.slot_capacity`` on both sides,
    rather than the recording's 21 against a server willing to use 46.
    """
    scalars = list(LEADING_SCALARS) + [0, 0]
    scalars[CAPACITY] = max(int(capacity), LEADING_SCALARS[CAPACITY])
    if health is not None:
        scalars[HEALTH] = int.from_bytes(struct.pack("<f", health), "little")
    if resource is not None:
        scalars[BESIDE_HEALTH] = int.from_bytes(
            struct.pack("<f", resource), "little"
        )
    return build(Inventory(scalars=tuple(scalars)), actor)


def _framed(command: int, body: BitWriter, actor: int) -> BitWriter:
    """*body* as a whole command: the id, the body, the actor, the terminator.

    Written here for the first time. Everything this module did until now edited a
    recorded message, so nothing needed to state the framing -- which is why ``MULTI``,
    ``INVENTORY_INFO`` and ``TERMINATOR`` sat declared and unused. :mod:`dsor.location`
    and :mod:`dsor.remote` both write it the same way.
    """
    out = BitWriter()
    out.write_uint(command, 16)
    for bit in body._bits:  # noqa: SLF001
        out.write_bits(bit, 1)
    out.write_uint(actor, 32)
    out.write_uint(TERMINATOR, 8)
    return out


def build(got: Inventory, actor: int) -> Payload:
    """*got* as a standalone ``InventoryInfoCommand``.

    The body has been provably right for a while -- :func:`encode` reproduces both of
    the live service's replies bit for bit -- and this is the framing it never had.
    """
    writer = BitWriter()
    writer.write_uint(MULTI, 8)
    body = _framed(INVENTORY_INFO, encode(got), actor)
    for bit in body._bits:  # noqa: SLF001
        writer.write_bits(bit, 1)
    return writer.to_bytes()


def taken(
    item: Item,
    cell: int,
    actor: int,
    health: float | None = None,
    resource: float | None = None,
    into: str = DEFAULT_LAYOUT,
) -> Payload:
    """The whole answer to a pickup, built rather than replayed.

    The chain the live service sends, minus the trailing status effect it happens to
    batch: a ``DiscardItemCommand`` taking the item off the ground, then an
    ``InventoryInfoCommand`` putting it in the bag.

    What this replaces was 8.4 KB of a recorded session -- another character's sword,
    his two cell placements and 247 template strings -- with six fields rewritten
    inside it. What it states instead is only what is known: the item, its cell, the
    player's health and resource. Everything else is left at :data:`OPAQUE`.
    """
    writer = BitWriter()
    writer.write_uint(MULTI, 8)
    discard = BitWriter()
    discard.write_uint(item.id, 32)
    for bit in _framed(DISCARD_ITEM, discard, item.id)._bits:  # noqa: SLF001
        writer.write_bits(bit, 1)

    scalars = list(LEADING_SCALARS) + [0, 0]
    if health is not None:
        scalars[HEALTH] = int.from_bytes(struct.pack("<f", health), "little")
    if resource is not None:
        scalars[BESIDE_HEALTH] = int.from_bytes(
            struct.pack("<f", resource), "little"
        )
    bag = Inventory(items=[item], scalars=tuple(scalars))
    setattr(
        bag,
        into,
        [(item.id, [cell])] if into == "slots" else [(item.id, cell)],
    )
    body = _framed(INVENTORY_INFO, encode(bag), actor)
    for bit in body._bits:  # noqa: SLF001
        writer.write_bits(bit, 1)
    return writer.to_bytes()


def picked_up(
    payload: bytes,
    actor: int,
    template: str | None = None,
    where: tuple[float, float, float] | None = None,
    stamped: tuple[int, int, int, int, int, int] | None = None,
    slot: int | None = None,
    into: str | None = None,
    health: float | None = None,
    beside: float | None = None,
    statistics: list[Statistic] | None = None,
    level: int | None = None,
    tier: int | None = None,
    at: int = BODY_AT,
) -> Payload:
    """The recorded reply, made to be about *actor* instead of what it recorded.

    The item record is rewritten rather than replayed: its actor, its blueprint, where
    it lay and when it was taken. Everything else in the message is left alone --
    including the equipment map, which is the field this used to destroy.

    *slot* places the item in the bag through ``+0x58``. The live service does not
    always send one: of its two pickups in ``officiel4`` the first names a cell and the
    second names none, and both put the item in the bag. So it is optional here too,
    and passing ``None`` leaves the recording's own placement untouched.
    """
    got, _ends = decode(payload, at)
    if not got.items:
        raise ValueError("the recorded reply carries no item record to rewrite")
    first = got.items[0]
    was = first.id
    got.items[0] = _replace(
        first,
        id=actor,
        template=template if template is not None else first.template,
        position=where if where is not None else first.position,
        stamped=stamped if stamped is not None else first.stamped,
        statistics=first.statistics if statistics is None else list(statistics),
        level=first.level if level is None else level,
        tier=first.tier if tier is None else tier,
    )
    if health is not None or beside is not None:
        scalars = list(got.scalars)
        if health is not None:
            scalars[HEALTH] = int.from_bytes(struct.pack("<f", health), "little")
        if beside is not None:
            scalars[BESIDE_HEALTH] = int.from_bytes(
                struct.pack("<f", beside), "little"
            )
        got.scalars = tuple(scalars)
    if slot is not None:
        # **Only** the item that was picked up. The recorded reply also places an item
        # of its own session -- 0x00010001 in cell 0 -- and carrying that across tells
        # the client to move whatever the player has there, which took the equipped
        # sword off the character. The operator reported it as "l'epee se desequippe".
        #
        # An earlier version of this file replaced the whole dictionary for exactly
        # that reason and was right about it; what it got wrong was the offset, writing
        # at bit 1,412 where the array it meant begins 352 bits later. Rebuilding the
        # message from a decode keeps the intent and loses the bug.
        for name in LAYOUTS:
            setattr(got, name, [])
        wanted = into or DEFAULT_LAYOUT
        # ``+0x30`` states a *list* of cells per item, the first of which the client
        # takes as the primary. The other four state one cell.
        setattr(
            got,
            wanted,
            [(actor, [slot])] if wanted == "slots" else [(actor, slot)],
        )
    out = rebuild(payload, got, at)
    return with_discarded(out, actor)
