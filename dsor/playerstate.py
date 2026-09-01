"""The level and experience inside the replayed player state.

The selection screen reads them from the roster (see dsor/charlist.py) and the game
reads them from here, which is why the level was right on the screen and back to 1 after
pressing Play: this server replays a recorded ``NewPlayerCommand`` and the recorded
character is level 1.

**Measured against the live service's own.** A capture taken from the client's launch
carries a real 0x001D of 1,203,213 bytes; the recording is 631,240. Both put the
character's name at bit 728 and lay out the same fields after it:

    u16 length + the name          "AmateurDeCombat"   /  "balenciagas"
    80 bits
    u16 length + the map           "a0200_kingscity"   /  "a0001_start_tutorial_dun"
    210 bits
    u32  the level                 100                 /  1
    u32  the experience            882246499           /  0

Counted from the end of the map string because both strings are inline and their lengths
differ between characters -- the same reason the roster's fields are counted from its
map. Aligning on a fixed offset from the start of the message does not work: the
official level sits at bit 1274 and the recording's at 1314, exactly the 40 bits by
which the two maps' names differ.

Only those two fields are written, in place, bit by bit. Nothing else in 631 KB is
touched, and the walk is checked before anything is: the level read has to be a real
level and the two strings have to be where the layout says.
"""

from __future__ import annotations

import logging

from raknet.bitstream import BitReader

log = logging.getLogger("playerstate")

#: Where the character's name's length prefix sits, in bits. The same in both messages.
NAME_AT = 728 - 16

#: Between the end of the name and the map's length prefix.
GAP_BITS = 80

#: Past the end of the map string: the level, then the experience.
LEVEL_REL = 210
EXPERIENCE_REL = 242

#: Their width.
FIELD_BITS = 32

#: What the live service uses. 100 is the ceiling, measured on four characters.
LOWEST_LEVEL = 1
HIGHEST_LEVEL = 100

#: A string longer than this means the walk has lost the boundary.
LONGEST_STRING = 64


def _string(blob: bytes, at: int) -> tuple[str, int] | None:
    try:
        reader = BitReader(blob, at)
        length = reader.read_uint(16)
        if not 1 <= length <= LONGEST_STRING:
            return None
        raw = bytes(reader.read_uint(8) for _ in range(length))
        return raw.decode("ascii"), reader.position
    except (IndexError, ValueError, UnicodeDecodeError):
        return None


def progress_of(blob: bytes) -> dict | None:
    """The name, map, level, experience and where the last two are. None if not walkable."""
    got = _string(blob, NAME_AT)
    if got is None:
        return None
    name, after_name = got
    got_map = _string(blob, after_name + GAP_BITS)
    if got_map is None:
        return None
    where, after_map = got_map
    if after_map + EXPERIENCE_REL + FIELD_BITS > len(blob) * 8:
        return None
    try:
        level = BitReader(blob, after_map + LEVEL_REL).read_uint(FIELD_BITS)
        experience = BitReader(blob, after_map + EXPERIENCE_REL).read_uint(FIELD_BITS)
    except (IndexError, ValueError):
        return None
    if not LOWEST_LEVEL <= level <= HIGHEST_LEVEL:
        return None
    return {
        "name": name,
        "map": where,
        "level": level,
        "experience": experience,
        "level_at": after_map + LEVEL_REL,
        "experience_at": after_map + EXPERIENCE_REL,
    }


def _write_uint(out: bytearray, at: int, value: int, count: int) -> None:
    """The mirror of BitReader.read_uint: low byte first, each byte high bit first.

    Bit by bit because these fields are not byte aligned -- the strings before them are
    inline, so where they land depends on how long a character's name and map are.
    """
    for index in range(count // 8):
        byte = (value >> (8 * index)) & 0xFF
        for bit in range(8):
            position = at + index * 8 + bit
            if byte & (1 << (7 - bit)):
                out[position // 8] |= 1 << (7 - position % 8)
            else:
                out[position // 8] &= ~(1 << (7 - position % 8)) & 0xFF


def with_progress(blob: bytes, level: int, experience: int) -> bytes:
    """*blob* with the character's level and experience set. Unchanged if not walkable."""
    found = progress_of(blob)
    if found is None:
        log.warning("player state does not read as one -- leaving it alone")
        return blob
    wanted = max(LOWEST_LEVEL, min(HIGHEST_LEVEL, int(level)))
    kept = max(0, min(0xFFFFFFFF, int(experience)))
    if found["level"] == wanted and found["experience"] == kept:
        return blob
    out = bytearray(blob)
    _write_uint(out, found["level_at"], wanted, FIELD_BITS)
    _write_uint(out, found["experience_at"], kept, FIELD_BITS)
    log.info(
        "player state: %s level %d -> %d, experience %d -> %d",
        found["name"],
        found["level"],
        wanted,
        found["experience"],
        kept,
    )
    return bytes(out)
