"""Reading the character record the selection screen is built from.

The record arrives as a 0x84/0x0087 message and is written with RakNet's
``BitStream``, so its fields do not sit on byte boundaries. That is the whole
difficulty: 437 bytes of it contained two character names in plain text and no
byte-aligned search found either, because the first begins at bit 225 and the
second at bit 1323.

Strings use the same convention as everywhere else in this protocol — a 16-bit
little-endian length, then that many bytes — just written at an arbitrary bit
offset. Confirmed against the record: the 16 bits before ``jeangustavo`` read as
11, its exact length.

What this module does *not* do is claim a full layout. It locates the strings and
pairs each character with the map name that follows it, which is enough to serve a
world consistent with the character a client picked. The numeric fields between
them are not decoded, so a record still cannot be built from nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from raknet.bitstream import BitReader

#: Width of a string's length prefix, in bits.
STRING_LENGTH_BITS = 16

#: Longest string worth considering. Names and map identifiers are far shorter;
#: a larger value would let a random length prefix match a run of printable bytes.
MAX_STRING_LENGTH = 64

#: A map identifier looks like "a0001_start_tutorial_dun": a letter, four digits,
#: then a name. Used to tell a character's map from its name.
MAP_PREFIX_LENGTH = 5


@dataclass(frozen=True)
class RecordString:
    """A string found in the record, with where it was found."""

    bit_position: int
    value: str

    @property
    def looks_like_a_map(self) -> bool:
        """Whether this is a map identifier rather than a name or an item."""
        head = self.value[:MAP_PREFIX_LENGTH]
        return (
            len(self.value) > MAP_PREFIX_LENGTH
            and head[0].isalpha()
            and head[1:].isdigit()
            and "_" in self.value
        )


@dataclass(frozen=True)
class CharacterEntry:
    """One character offered on the selection screen."""

    name: str
    #: The map this character was last on. The client places it there, which is why
    #: serving a different map's world state puts the player somewhere unrelated.
    last_map: str | None
    name_bit: int


def find_strings(record: bytes) -> list[RecordString]:
    """Locate every length-prefixed string in *record*, at any bit offset.

    Scans one bit at a time and keeps a position only when the length prefix
    describes a run of printable bytes. A prefix is 16 bits, so a false positive
    needs a plausible length *and* printable text behind it — rare enough that the
    strings found here match those a brute-force bit-shift search finds, which is
    how this was checked.
    """
    reader = BitReader(record)
    found: list[RecordString] = []
    occupied_until = -1

    for position in range(reader.length - STRING_LENGTH_BITS):
        if position < occupied_until:
            continue
        reader.seek(position)
        length = reader.read_uint(STRING_LENGTH_BITS)
        if not 3 <= length <= MAX_STRING_LENGTH:
            continue
        if reader.remaining < length * 8:
            continue
        raw = reader.read_bytes(length)
        if not all(32 <= byte < 127 for byte in raw):
            continue
        text = raw.decode("ascii")
        # Require something word-shaped, so a run of spaces or punctuation does
        # not register as a field.
        if not text[0].isalnum() or sum(c.isalnum() for c in text) < 3:
            continue
        found.append(RecordString(position + STRING_LENGTH_BITS, text))
        # Skip past what was just consumed: overlapping matches inside a string's
        # own bytes are not separate fields.
        occupied_until = position + STRING_LENGTH_BITS + length * 8

    return found


def decode_character_list(record: bytes) -> list[CharacterEntry]:
    """Pair each character name with the map that follows it.

    The record lists characters in the order the selection screen shows them, each
    followed by its last map. Item and skill identifiers appear later and are
    ignored here: they are map-shaped in neither prefix nor position.
    """
    strings = find_strings(record)
    entries: list[CharacterEntry] = []

    for index, candidate in enumerate(strings):
        if candidate.looks_like_a_map:
            continue
        if "_" in candidate.value:
            # Item and skill identifiers are underscore-heavy; a character name is
            # a single word. This is a heuristic, and it is the reason a name
            # containing an underscore would be missed.
            continue
        following = strings[index + 1] if index + 1 < len(strings) else None
        last_map = (
            following.value
            if following is not None and following.looks_like_a_map
            else None
        )
        entries.append(
            CharacterEntry(
                name=candidate.value, last_map=last_map, name_bit=candidate.bit_position
            )
        )
    return entries
