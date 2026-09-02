"""``InventoryInfoCommand``, 0x0054, and the item records inside it.

Read out of the client's own decoder rather than guessed at. ``Commands::InventoryInfoCommand``
registers its Rtti at ``+0xabefc`` with the fourcc ``'IvIC'``, whose creator leads to a
constructor that plants the vtable at ``0x14115fcd8``; slot 5 of that vtable -- the slot
``DrasaClientHandler::DecodeCommand`` calls for a command's own fields -- is
``+0x9671e8``. That function is a flat run of reader calls, one per member, and it is
the grammar:

    array<ItemInfo>            +0x20    the records being merged
    dict<u32, array<int8>>     +0x30    per storage, its shape
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

#: Where the 0x0054 body begins in that reply: the 0x002E's id, its item, its actor and
#: its terminator, then the next command's id.
BODY_AT = 112

#: The client's own ceiling on every count it reads (``cmp ecx, 0xf4240``).
MOST = 1_000_000

#: A count of statistics or names on one item is a byte, so this is that ceiling.
MOST_ON_AN_ITEM = 255

#: Trailing every command: the actor, then this.
TERMINATOR = 0xFF


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
    shapes: list[tuple[int, list[int]]] = field(default_factory=list)
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
    header: int = 0


def decode(payload: bytes, at: int = BODY_AT) -> tuple[Inventory, int]:
    """The command's body starting at bit *at*, and the bit it ends on.

    The returned position is where the command's actor begins, so a caller can check
    the ``0xFF`` behind it -- which is the whole proof that the walk is right.
    """
    reader = BitReader(payload, at)
    got = Inventory(header=reader.read_bits(1))

    def count() -> int:
        found = reader.read_uint(32)
        if found > MOST:
            raise ValueError(f"a count of {found} is past the client's own ceiling")
        return found

    got.items = [_read_item(reader) for _ in range(count())]
    for _ in range(count()):
        key = reader.read_uint(32)
        got.shapes.append((key, [_read_signed(reader) for _ in range(count())]))
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
    writer.write_bits(got.header, 1)

    def count(items) -> None:
        if len(items) > MOST:
            raise ValueError(
                f"{len(items)} is past the client's own ceiling of {MOST}"
            )
        writer.write_uint(len(items), 32)

    count(got.items)
    for item in got.items:
        _write_item(writer, item)
    count(got.shapes)
    for key, shape in got.shapes:
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


def rebuild(payload: bytes, got: Inventory, at: int = BODY_AT) -> Payload:
    """*payload* with the inventory at bit *at* replaced by *got*.

    Everything before and after is carried across bit for bit, which is the point:
    the reply this server sends is a real one from the live service, and the parts of
    it that are not understood -- the storage shapes, the equipment map, the strings,
    the trailing status effect -- stay exactly as they were recorded. Only what is
    understood is rewritten, and it is rewritten by re-encoding the whole collection
    rather than by splicing bytes into it. Splicing is what shifted the equipment map
    and took the character's health with it.
    """
    _, ends = decode(payload, at)
    stream = _bits_of(payload)
    fresh = "".join(str(bit) for bit in encode(got)._bits)  # noqa: SLF001
    spliced = stream[:at] + fresh + stream[ends:]
    return Payload(_to_bytes(spliced), bits_of(payload) + len(fresh) - (ends - at))


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


def picked_up(
    payload: bytes,
    actor: int,
    template: str | None = None,
    where: tuple[float, float, float] | None = None,
    stamped: tuple[int, int, int, int, int, int] | None = None,
    slot: int | None = None,
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
        got.placements = [
            (actor if item == was else item, cell) for item, cell in got.placements
        ]
        if not any(item == actor for item, _cell in got.placements):
            got.placements.append((actor, slot))
        else:
            got.placements = [
                (item, slot if item == actor else cell)
                for item, cell in got.placements
            ]
    out = rebuild(payload, got, at)
    return with_discarded(out, actor)
