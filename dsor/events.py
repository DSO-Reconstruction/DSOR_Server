"""The global event schedule: message 0x84/0x00DD.

This is the largest message in the protocol — 733,774 bytes, 596 fragments on the
wire — and it was opaque for a long time. It is not what its size and position
suggest. It carries no inventory, no quests, no character data of any kind: it is a
flat list of 8,233 dated event-schedule entries, the same for every player, sent to
the client immediately behind the character roster.

That matters twice over. It tells us where the client's character appearance does
*not* come from, and it means this message holds nothing private — every string in
it is a promotion or event key such as a PvP match cycle or a seasonal event, not
anything belonging to an account.

The layout below is verified by round-trip rather than argued: decoding the
recorded message and re-encoding the result reproduces all 733,774 bytes exactly,
and the model accounts for 5,870,188 of the file's 5,870,192 bits. The remaining
four are zero padding to the byte, which is why frames carry a length in bits.

Nothing here is byte-aligned. The gap between one record's text and the next
record's length prefix is a constant 452 bits, and 452 is not a multiple of eight,
so every second string starts on a nibble boundary. That is why a byte-aligned
search finds only half of them — 4,117 against 4,116.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from raknet.bitstream import BitReader, BitWriter

#: Message id and opcode this module reads and writes.
MESSAGE_ID = 0x84
OPCODE = 0x00DD

#: Width of the record count, and of every integer field in a record.
COUNT_BITS = 32

#: Width of a string's length prefix, as everywhere else in this protocol.
STRING_LENGTH_BITS = 16

#: One bit, set in 16 of the 8,233 recorded entries. Every one of those 16 has an
#: end date in the future, but only 16 of the 101 future-dated entries carry it, so
#: "currently running" is consistent with the evidence without being proven by it.
#: Named for what it is until something forces a better name.
FLAG_BITS = 1

#: Three bits that are zero in all 8,233 records. Whether they are three fields or
#: padding cannot be told from a capture where they never vary.
RESERVED_BITS = 3

#: Six 32-bit values, zero in all 8,233 records. Their width is exactly that of the
#: date structure that follows, which makes an unset second date the natural
#: reading — but it is untested, because they never once differ.
BLANK_FIELDS = 6

#: The date, as six separate 32-bit little-endian integers rather than a packed
#: timestamp. All 8,212 non-null dates in the recorded message are calendar-valid.
DATE_FIELDS = 6


@dataclass
class ScheduleEntry:
    """One dated entry in the schedule."""

    #: Strictly increasing across the message — 8,232 of 8,232 consecutive pairs —
    #: so the list is ordered by id and not by date. The dates descend 501 times.
    identifier: int
    #: An event or promotion key, e.g. a PvP match cycle or a seasonal event.
    key: str
    #: Year, month, day, hour, minute, second. A year of zero means no date: 21
    #: entries are like that.
    date: tuple[int, int, int, int, int, int]
    flag: bool = False
    reserved: int = 0
    blanks: tuple[int, ...] = (0,) * BLANK_FIELDS
    #: Length-prefixed strings after the date. Only two entries in the recorded
    #: message carry any, one each, so a list longer than one is untested.
    parameters: list[str] = field(default_factory=list)

    @property
    def has_date(self) -> bool:
        return self.date[0] != 0


def decode_schedule(payload: bytes) -> list[ScheduleEntry]:
    """Decode a complete 0x84/0x00DD message, header included.

    Raises :class:`ValueError` if the message is not this one, or if the record
    count disagrees with what the body actually holds — a truncated transfer is
    worth refusing loudly rather than serving half a schedule.
    """
    reader = BitReader(payload)
    message_id = reader.read_uint(8)
    opcode = reader.read_uint(16)
    if (message_id, opcode) != (MESSAGE_ID, OPCODE):
        raise ValueError(
            f"expected {MESSAGE_ID:#04x}/{OPCODE:#06x}, "
            f"got {message_id:#04x}/{opcode:#06x}"
        )

    count = reader.read_uint(COUNT_BITS)
    entries: list[ScheduleEntry] = []
    for index in range(count):
        try:
            entries.append(_read_entry(reader))
        except ValueError as error:
            raise ValueError(
                f"schedule claims {count} entries but entry {index} "
                f"could not be read: {error}"
            ) from error

    # Only padding may remain. Anything else means the layout is wrong somewhere
    # earlier and the decode happened to survive it.
    if reader.remaining >= 8:
        raise ValueError(
            f"{reader.remaining} bits left after {count} entries, "
            "which is more than byte padding"
        )
    if reader.remaining and reader.read_bits(reader.remaining):
        raise ValueError("trailing bits are not zero padding")
    return entries


def _read_entry(reader: BitReader) -> ScheduleEntry:
    identifier = reader.read_uint(COUNT_BITS)
    key = reader.read_string(STRING_LENGTH_BITS)
    flag = reader.read_bool()
    reserved = reader.read_bits(RESERVED_BITS)
    blanks = tuple(reader.read_uint(COUNT_BITS) for _ in range(BLANK_FIELDS))
    date = tuple(reader.read_uint(COUNT_BITS) for _ in range(DATE_FIELDS))
    parameters = [
        reader.read_string(STRING_LENGTH_BITS)
        for _ in range(reader.read_uint(COUNT_BITS))
    ]
    return ScheduleEntry(
        identifier=identifier,
        key=key,
        date=date,  # type: ignore[arg-type]
        flag=flag,
        reserved=reserved,
        blanks=blanks,
        parameters=parameters,
    )


def encode_schedule(entries: list[ScheduleEntry]) -> bytes:
    """Build a complete 0x84/0x00DD message from *entries*.

    Round-trips the recorded message byte for byte, which is the only test that
    means anything for a format this large: any misread field would show up as a
    shifted bit somewhere in 5.87 million.
    """
    writer = BitWriter()
    writer.write_uint(MESSAGE_ID, 8)
    writer.write_uint(OPCODE, 16)
    writer.write_uint(len(entries), COUNT_BITS)
    for entry in entries:
        writer.write_uint(entry.identifier, COUNT_BITS)
        writer.write_string(entry.key, STRING_LENGTH_BITS)
        writer.write_bool(entry.flag)
        writer.write_bits(entry.reserved, RESERVED_BITS)
        for blank in entry.blanks:
            writer.write_uint(blank, COUNT_BITS)
        for part in entry.date:
            writer.write_uint(part, COUNT_BITS)
        writer.write_uint(len(entry.parameters), COUNT_BITS)
        for parameter in entry.parameters:
            writer.write_string(parameter, STRING_LENGTH_BITS)
    return writer.to_bytes()
