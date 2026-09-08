"""The character selection screen: the level and experience it shows.

Saving a character server-side does nothing for this screen, because it is drawn from a
replayed recording. The recorded character is level 1 with no experience, so every login
showed level 1 however much had been stored -- "tu sauvegardes rien vu que tu rejoues une
connexion au debut donc ca remet tjrs niveau 1 meme dans l'ecran de perso".

**Measured, on five characters.** A capture of the live service's own roster, taken from
the client's launch so the login was visible, carries four characters; the recording
carries one. The two messages have the same shape, and each entry reads:

    u16 length + the character's name
    u16 length + the map it is in
    256 bits
    u32   the experience
    u32   the level

    Username   882246499   100
    Username    882260621   100
    Username       882269671   100
    Username             128322    18
    Username               0     1     (the recording, and what the screen showed)

Both fields are counted from the *end of the map string*, because that is the last thing
before them whose position can be found: the entries are back to back and their lengths
follow their names, so nothing before the map is at a fixed offset from the start of the
message.

**The level is stored, not derived.** 882,246,499 experience reads as level 104 through
this server's own curve and the roster says 100, so the real ceiling is 100 and the curve
is wrong above it. Which is the other reason to write the level rather than compute it.

An earlier version of this module wrote sixteen bits 145 bits *before* the name, having
found a 1 there in the recording and taken it for the level. It is 1 in the live
service's level-100 character too. Writing 104 into it broke the login. The lesson is in
the guard below: a field is not identified by one value in one recording that happens to
match what is on the screen.
"""

from __future__ import annotations

import logging
import struct

from raknet.payload import Payload, bits_of, respan
from raknet.bitstream import BitReader

log = logging.getLogger("charlist")

#: Where the two fields sit, in bits past the end of the entry's map string.
#: The andermant is the account's, not the character's: all four characters of the live
#: service's roster carry 4,814 and the recording's one carries 600 -- which is exactly
#: what the operator saw on the live service and on this server. Two independent values,
#: each matching what was on screen, and one of them identical across four entries.
ANDERMANT_REL = 160
EXPERIENCE_REL = 256
LEVEL_REL = 288

#: Their width.
FIELD_BITS = 32

#: The levels the live service was measured using. 100 is the ceiling: a character with
#: 882,246,499 experience reads 100 there, while this server's curve says 104.
LOWEST_LEVEL = 1
HIGHEST_LEVEL = 100

#: A name or a map longer than this means the walk has lost the boundary.
LONGEST_STRING = 64

#: The first entry's name length prefix, in bits from the start of the payload. The
#: header before it is fixed: 0x84, the command id, the operation, and two 32-bit ids.
FIRST_NAME_AT = 233


def _string(blob: bytes, at: int) -> tuple[str, int] | None:
    """The length-prefixed string at bit *at*, and where it ends."""
    try:
        reader = BitReader(blob, at)
        length = reader.read_uint(16)
        if not 1 <= length <= LONGEST_STRING:
            return None
        raw = bytes(reader.read_uint(8) for _ in range(length))
        return raw.decode("ascii"), reader.position
    except (IndexError, ValueError, UnicodeDecodeError):
        return None


def entries(blob: bytes) -> list[dict]:
    """Every character in the roster: its name, map, level, experience and offsets.

    Walks forward from the first name, since the entries are back to back. Stops at the
    first thing that does not read as one, and returns what it had -- a roster this
    cannot walk to the end is one to leave alone rather than half-rewrite.
    """
    found: list[dict] = []
    at = FIRST_NAME_AT
    total = len(blob) * 8
    while at < total:
        got = _string(blob, at)
        if got is None:
            break
        name, after_name = got
        got_map = _string(blob, after_name)
        if got_map is None:
            break
        where, after_map = got_map
        if after_map + LEVEL_REL + FIELD_BITS > total:
            break
        try:
            experience = BitReader(blob, after_map + EXPERIENCE_REL).read_uint(FIELD_BITS)
            level = BitReader(blob, after_map + LEVEL_REL).read_uint(FIELD_BITS)
        except (IndexError, ValueError):
            break
        if not LOWEST_LEVEL <= level <= HIGHEST_LEVEL:
            break
        andermant = BitReader(blob, after_map + ANDERMANT_REL).read_uint(FIELD_BITS)
        found.append(
            {
                "name": name,
                "map": where,
                "level": level,
                "experience": experience,
                "andermant": andermant,
                "andermant_at": after_map + ANDERMANT_REL,
                "experience_at": after_map + EXPERIENCE_REL,
                "level_at": after_map + LEVEL_REL,
                "name_at": at,
            }
        )
        # The next entry begins where this one's trailing block ends. Measured constant
        # across the four official entries: 834 bits from the end of the map to the next
        # name's length prefix.
        at = after_map + TRAILER_BITS
    return found


#: From the end of one entry's map string to the next entry's name length prefix.
#: 505 -> 1339, 1603 -> 2437, 2677 -> 3511 in the official roster: 834 every time.
TRAILER_BITS = 834

#: Where an entry keeps its **own** character id, counted from the end of its map string
#: like every other field here. The id is on the wire twice -- once at
#: :data:`CHARACTER_AT` in the header and once per entry -- and writing only the header
#: leaves the entry claiming the recorded character. Measured on the live four-entry
#: roster, where every entry's field holds that entry's id:
#:
#:     Username 111383506, Username 111396827,
#:     Username 111423903, Username 111560171
#:
#: and the header holds the first of them.
IDENTITY_REL = 96


def _write_uint(out: bytearray, at: int, value: int, count: int) -> None:
    """Write *count* bits of *value* at bit *at*, the mirror of BitReader.read_uint.

    Bit by bit, because these fields are not byte aligned: the entries carry their
    strings inline, so where a field lands depends on how long the character's name and
    map are. In the recording the experience sits at bit 801 and in the live service's
    roster at 761 -- neither a multiple of eight. A byte-level splice would have written
    into the neighbours.

    The reader takes multi-byte values low byte first, each byte most significant bit
    first, so this does the same.
    """
    for index in range(count // 8):
        byte = (value >> (8 * index)) & 0xFF
        for bit in range(8):
            position = at + index * 8 + bit
            mask = 1 << (7 - bit)
            if byte & mask:
                out[position // 8] |= 1 << (7 - position % 8)
            else:
                out[position // 8] &= ~(1 << (7 - position % 8)) & 0xFF


def with_progress(blob: bytes, level: int, experience: int) -> bytes:
    """*blob* with the first character's level and experience set.

    Only the first: the recording holds one character, and which of several a saved
    progress belongs to is not something this can know. Sixty-four bits written in
    place, so nothing moves and no length changes.

    Returns the message unchanged, and says so, when it cannot be walked -- the guard
    that the previous version of this module lacked.
    """
    found = entries(blob)
    if not found:
        log.warning("roster does not read as one -- leaving it alone")
        return blob
    first = found[0]
    wanted = max(LOWEST_LEVEL, min(HIGHEST_LEVEL, int(level)))
    if first["level"] == wanted and first["experience"] == experience:
        return blob
    out = bytearray(blob)
    for at, value in (
        (first["experience_at"], max(0, min(0xFFFFFFFF, int(experience)))),
        (first["level_at"], wanted),
    ):
        _write_uint(out, at, value, FIELD_BITS)
    log.info(
        "roster: %s level %d -> %d, experience %d -> %d",
        first["name"],
        first["level"],
        wanted,
        first["experience"],
        experience,
    )
    return respan(blob, bytes(out))


#: A ceiling, so a typo cannot write something the client reads as negative.
MOST_ANDERMANT = 999_999_999


def with_andermant(blob: bytes, amount: int) -> bytes:
    """*blob* with the account's andermant set to *amount*, in every entry.

    Every entry, because it is the account's and not the character's: the live service's
    four characters all carry 4,814. Writing it into one and not the others would make the
    selection screen disagree with itself.
    """
    found = entries(blob)
    if not found:
        log.warning("roster does not read as one -- not touching the andermant")
        return blob
    wanted = max(0, min(MOST_ANDERMANT, int(amount)))
    if all(entry["andermant"] == wanted for entry in found):
        return blob
    out = bytearray(blob)
    for entry in found:
        _write_uint(out, entry["andermant_at"], wanted, FIELD_BITS)
    log.info(
        "roster: andermant %d -> %d in %d entr%s",
        found[0]["andermant"],
        wanted,
        len(found),
        "y" if len(found) == 1 else "ies",
    )
    return respan(blob, bytes(out))


#: ``CharacterSelectionCommand``, and the two operations that matter here.
#:
#: The exchange, read straight off the capture:
#:
#:     server  0x84/0x0087 operation 1   the list, 1,964 bytes
#:     client  0x8B/0x0087 operation 3   29 bytes: the operation, the chosen character
#:                                       id at offset 2, then 23 zeroes
#:     server  0x84/0x0087 operation 5   **the same 29 bytes with byte 0 set to 5**
#:
#: That last line is the whole of the grant. This server replayed a recording instead
#: and read none of the request, so every client that clicked Play was granted the
#: recorded character -- which is why two sessions were both Username.
SELECTION = 0x0087
OPERATION_START_GAME = 3
OPERATION_GRANTED = 5

#: Where the chosen character id sits in the request, and how long the whole body is.
CHOSEN_AT = 2
SELECTION_BODY = 29

#: The container the service answers in: 0x84, not the 0x85 the map server uses.
SINGLE = 0x84


def character_chosen(body: bytes) -> int | None:
    """The character id a start-game request names, or None if it is not one."""
    if len(body) < CHOSEN_AT + 4 or body[0] != OPERATION_START_GAME:
        return None
    chosen = int.from_bytes(body[CHOSEN_AT : CHOSEN_AT + 4], "little")
    return chosen or None


def grant_character(body: bytes) -> bytes | None:
    """The grant for a start-game request: the request, with the operation changed.

    Byte for byte what the live service sends. Nothing else in the 29 bytes differs --
    not the character id, not the 23 zeroes behind it -- which is worth stating because
    it means there is nothing here to get wrong except reading the request at all.
    """
    if character_chosen(body) is None:
        return None
    kept = bytearray(body[:SELECTION_BODY])
    if len(kept) < SELECTION_BODY:
        kept.extend(bytes(SELECTION_BODY - len(kept)))
    kept[0] = OPERATION_GRANTED
    return bytes([SINGLE]) + SELECTION.to_bytes(2, "little") + bytes(kept)


#: The header, and the fields in it that belong to the account rather than the
#: recording. Read off both messages:
#:
#:     84 87 00 | 01 00 | 8e 3f ab 06 | 32 75 af 06 | 04 00 00 00 | ...
#:     ^ 0x84     ^ op 1  ^ character   ^ account     ^ how many
#:
#: The number of entries. Worth writing down how nearly this went wrong: the recorded
#: template holds **4** and only **one** entry walks out of it, which reads exactly like
#: a slot count -- so it was replayed instead of written, and a one-character roster went
#: out declaring four. The client read three entries that were not there and died on an
#: invalid string atom inside ``UI::Element::FindChildElement``.
#:
#: The live roster settles it: 4 here and four entries, each one walking cleanly. So the
#: field is the count, and the template is simply a message the capture cut at 316
#: bytes --- the same trap as the arrival batch, where an exact-looking segmentation of a
#: truncated message looked like proof.
#:
#: 97 bits sit between it and the first name and are not understood, so they are
#: replayed.
OPERATION_LIST = 1
OPERATION_AT = 24
CHARACTER_AT = 40
ACCOUNT_AT = 72
COUNT_AT = 104

#: What the header is, up to the first name.
HEADER_BITS = FIRST_NAME_AT


def _bits(blob: bytes) -> str:
    """*blob* as bits, to its **declared** length and not its byte length.

    The roster is 316 bytes and declares 2,525 bits, so the last three are padding. A
    rebuild that copies them declares 2,528, and the client reads three spare bits as
    the start of another command.
    """
    whole = "".join(f"{byte:08b}" for byte in blob)
    return whole[: bits_of(blob)]


def _packed(bits: str) -> bytes:
    pad = -len(bits) % 8
    return int(bits + "0" * pad, 2).to_bytes((len(bits) + pad) // 8, "big")


def _field(value: int, count: int = FIELD_BITS) -> str:
    """*value* as the wire writes it: low byte first, each byte most significant first."""
    return "".join(f"{byte:08b}" for byte in value.to_bytes(count // 8, "little"))


def _string_bits(text: str) -> str:
    raw = text.encode("ascii")
    if not 1 <= len(raw) <= LONGEST_STRING:
        raise ValueError(f"a name is 1 to {LONGEST_STRING} characters, got {len(raw)}")
    return _field(len(raw), 16) + "".join(f"{byte:08b}" for byte in raw)


def build(
    blob: bytes,
    characters: list[dict],
    account: int,
    andermant: int | None = None,
    slots: int | None = None,
) -> Payload:
    """A roster listing *characters*, built over the recording's own bits.

    Each character is a dict of ``name``, ``map``, ``level``, ``experience``.

    What is written: the header's character id, account id and count, then per entry the
    name, the map, its **own** character id, the level, the experience and the andermant.
    The id goes in twice because the wire carries it twice, and writing only the header
    is what made the selection screen assert -- see :data:`IDENTITY_REL`.

    *slots* overrides the count, which is only useful for rebuilding the template: its
    own header declares four characters and carries one. What is replayed: the 97
    header bits nobody here understands, and the 834-bit trailer behind every entry --
    which holds, among other things, a 4x4 float matrix that is byte-identical in all
    five recorded characters. Inventing 834 bits would be the same mistake as inventing
    a skill command's tail.

    The recording's own entry rebuilds to the recording's own bytes --- given its own
    inconsistent count back through *slots* --- which is the test that this writes what
    it means to and nothing else.
    """
    if not characters:
        raise ValueError("a roster needs at least one character")
    stream = _bits(blob)
    template = entries(blob)
    if not template:
        raise ValueError("the recorded roster does not walk, so there is no template")
    first = template[0]
    trailer = stream[first["name_at"] :]
    # From the end of one entry's map string, TRAILER_BITS to the next name.
    trailer_from = _string(blob, first["name_at"])
    assert trailer_from is not None
    _name, after_name = trailer_from
    got_map = _string(blob, after_name)
    assert got_map is not None
    _map, after_map = got_map
    tail = stream[after_map : after_map + TRAILER_BITS]

    head = stream[:HEADER_BITS]
    head = (
        head[:OPERATION_AT]
        + _field(OPERATION_LIST, 16)
        + _field(int(characters[0].get("character", 0)))
        + _field(account)
        + _field(len(characters) if slots is None else int(slots))
        + head[COUNT_AT + FIELD_BITS :]
    )

    body = ""
    for character in characters:
        kept = tail
        for offset, value in (
            (IDENTITY_REL, int(character.get("character", 0))),
            (ANDERMANT_REL, andermant if andermant is not None else first["andermant"]),
            (EXPERIENCE_REL, int(character["experience"])),
            (LEVEL_REL, max(LOWEST_LEVEL, min(HIGHEST_LEVEL, int(character["level"])))),
        ):
            kept = kept[:offset] + _field(int(value)) + kept[offset + FIELD_BITS :]
        body += _string_bits(str(character["name"]))
        body += _string_bits(str(character["map"]))
        body += kept

    # And what follows the last entry, which is the account's one global equipment
    # list: a 32-bit count, then per item a length-prefixed template name, twenty zero
    # bytes and a set bit. There is exactly one of these however many characters there
    # are, and its entries carry no owner -- so it cannot bind gear to a character
    # except by position, and it is replayed rather than rebuilt.
    whole = head + body + stream[after_map + TRAILER_BITS :]
    return Payload(_packed(whole), len(whole))
