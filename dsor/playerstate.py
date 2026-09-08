"""The level and experience inside the replayed player state.

The selection screen reads them from the roster (see dsor/charlist.py) and the game
reads them from here, which is why the level was right on the screen and back to 1 after
pressing Play: this server replays a recorded ``NewPlayerCommand`` and the recorded
character is level 1.

**Measured against the live service's own.** A capture taken from the client's launch
carries a real 0x001D of 1,203,213 bytes; the recording is 631,240. Both put the
character's name at bit 728 and lay out the same fields after it:

    u16 length + the name          "Username"   /  "Username"
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
from functools import lru_cache

from raknet.payload import Payload, bits_of, respan
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
    return respan(blob, bytes(out))


def with_name(blob: bytes, name: str) -> Payload:
    """*blob* naming *name* instead of the character it was recorded for.

    Which is what stops two clients both being Username. The name is the first
    field the walk finds -- length-prefixed at :data:`NAME_AT` -- and nothing wrote it
    until now, so every player this server served carried the recording's own name over
    their head.

    The field is length-prefixed, so the message changes size and **everything behind
    it moves**: the map string, the level, the experience, and the actor in the
    command's own tail. That is fine and it is why this returns a Payload with the
    length recomputed rather than respanned -- a BitStream is read sequentially and
    states no offsets, so a reader that takes the length from the prefix does not care.
    It also means :func:`with_name` must run **before** anything that addresses a bit
    offset: the level, the experience and the actor are all found by walking to them.
    """
    raw = name.encode("ascii")
    if not 1 <= len(raw) <= LONGEST_STRING:
        raise ValueError(f"a name is 1 to {LONGEST_STRING} characters, got {len(raw)}")
    got = _string(blob, NAME_AT)
    if got is None:
        log.warning("no character name at bit %d -- leaving the state alone", NAME_AT)
        return blob if isinstance(blob, Payload) else Payload(blob, len(blob) * 8)
    was, after = got
    total = bits_of(blob)
    stream = "".join(f"{byte:08b}" for byte in blob)
    head = stream[:NAME_AT]
    tail = stream[after:total]
    body = f"{len(raw):016b}"
    body = "".join(f"{byte:08b}" for byte in len(raw).to_bytes(2, "little"))
    body += "".join(f"{byte:08b}" for byte in raw)
    spliced = head + body + tail
    pad = -len(spliced) % 8
    log.info("player state: %r -> %r", was, name)
    return Payload(
        int(spliced + "0" * pad, 2).to_bytes((len(spliced) + pad) // 8, "big"),
        len(spliced),
    )


#: The container a built command travels in, and what closes it.
MULTI = 0x85
TERMINATOR = 0xFF

#: ``PlayerReadyCommand`` -- the signal that makes the player exist.
#:
#: It is the **last** command of the recorded Kingshill arrival, and trimming that
#: arrival to the leading ``NewPlayerCommand`` cut it off. The operator's report was
#: immediate and exact: "il y'a un bug ou je n'ai pas l'actor qui spawn".
#:
#: There is nothing to it but the signal. Seven bytes chained, eight standalone: the
#: id, **no body at all**, the actor, the terminator --
#: ``1f 00 | 55 21 02 00 | ff`` in the recording, whose actor is the one the state was
#: captured for. So the whole of the initialisation this server was missing is a name
#: and an actor.
PLAYER_READY = 0x001F


def ready(actor: bytes) -> bytes:
    """A ``PlayerReadyCommand`` for *actor*: you exist, and this is who you are.

    Sent last, because that is where the live service sends it -- after the player's
    own command, after everything about the account, after the status effects. An
    arrival without it leaves the client holding a description of a character it never
    instantiates.
    """
    if len(actor) != 4:
        raise ValueError(f"an actor id is four bytes, got {len(actor)}")
    return (
        bytes([MULTI])
        + PLAYER_READY.to_bytes(2, "little")
        + actor
        + bytes([TERMINATOR])
    )


#: The actor the recorded state belongs to. Every client that is handed this message
#: believes it **is** this actor, which is why it has to be rewritten per player.
RECORDED_ACTOR = 0x00010015


@lru_cache(maxsize=4)
def references(state: bytes, actor: int = RECORDED_ACTOR) -> tuple[int, ...]:
    """Every bit offset at which *state* holds *actor* as a 32-bit field.

    Searched at all eight alignments and each hit read back, the same way the pickup
    reply's were. There are 45 in the recorded state, in four clusters: one at bit
    1,240, a table between 4,554 and 34,372, another between 102,307 and 112,122, and
    a handful in the last few thousand bits.

    Every one of them is rewritten, and the argument for that is the message itself: it
    is *this player's* state, so a 32-bit field holding exactly their actor is a
    reference to them. The offsets are computed once, from a blob that never changes,
    and the rewrite is 45 field writes on a copy -- so a login costs one pass over the
    bytes and a tick costs nothing. Cached, because the blob is a constant: the search
    is 37 ms and the rewrite that follows it is 3.
    """
    want = actor.to_bytes(4, "little")
    size = len(state)
    value = int.from_bytes(state, "big")
    mask = (1 << (8 * size)) - 1
    found: set[int] = set()
    for shift in range(8):
        window = ((value << shift) & mask).to_bytes(size, "big")
        at = window.find(want)
        while at >= 0:
            found.add(at * 8 + shift)
            at = window.find(want, at + 1)
    return tuple(
        sorted(bit for bit in found if BitReader(state, bit).read_uint(32) == actor)
    )


def with_actor(state: bytes, actor: int, was: int = RECORDED_ACTOR) -> bytes:
    """*state* belonging to *actor* instead of the one it was recorded for.

    Without this every client is told it is the same actor, and the consequence is
    exactly what the operator saw with two sessions open: "quand je deplace un client ça
    me deplace l'autre". Each client thought the *other* player's entity was itself, so
    one player's position update moved the other player's character.
    """
    if not 0 <= actor <= 0xFFFFFFFF:
        raise ValueError(f"an actor is 32 bits, got {actor}")
    at = references(state, was)
    if not at:
        log.warning("no reference to actor %#x in the player state", was)
        return state
    out = bytearray(state)
    fresh = actor.to_bytes(4, "little")
    for bit in at:
        for index, byte in enumerate(fresh):
            for offset in range(8):
                position = bit + index * 8 + offset
                where, shift = divmod(position, 8)
                mask = 1 << (7 - shift)
                if byte >> (7 - offset) & 1:
                    out[where] |= mask
                else:
                    out[where] &= 0xFF & ~mask
    return respan(state, bytes(out))
