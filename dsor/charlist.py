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

    AmateurDeCombat   882246499   100
    MeufAGrosSeins    882260621   100
    FilleMineur       882269671   100
    BgTimide             128322    18
    balenciagas               0     1     (the recording, and what the screen showed)

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
    return bytes(out)


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
    return bytes(out)
