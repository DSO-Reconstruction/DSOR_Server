"""The andermant in the replayed player state.

**The evidence is thinner than for the level, and that is the point of this docstring.**

The level and experience were measured against the live service's own player state, five
characters, four of them not this project's. This is not: the amount is a single u32
reading 600 in the recording, and the operator reports 600 andermant on screen. It is the
only ``600`` in the first 300,000 bits whose neighbours are not garbage, it sits in a run
of zeros -- which is what a fresh character's currency block should look like, 600
andermant and nothing else -- and the field after it reads 2.

That is one value in one recording matching what is displayed. Twice already this session
that reasoning has been wrong: a 1 at bit 168 of the roster looked like the level and is 1
in the service's level-100 characters too, and writing into it broke the login. So this is
behind a rule that is easy to turn off, anchored on the experience field rather than on
the start of the message, and it refuses to write unless the amount it is about to
replace is the one it expects.

The official state cannot confirm it: at the same place that message holds strings --
achievements and equipment -- because a level-100 character's state is 1.2 MB against the
recording's 631 KB and the two diverge well before this point.

    experience field + 576 bits   u32, 600 in the recording
"""

from __future__ import annotations

import logging

from raknet.bitstream import BitReader

from dsor import playerstate

log = logging.getLogger("currency")

#: Past the experience field, in bits. The experience is itself found by walking the
#: name and the map, so nothing here depends on a fixed offset from the message start.
ANDERMANT_REL = 576

#: Its width.
FIELD_BITS = 32

#: What the recording holds. The write refuses unless it finds this, which is the guard:
#: a different recording, or a wrong offset, and nothing is touched.
RECORDED_ANDERMANT = 600

#: A ceiling, so a typo cannot write something the client reads as negative.
MOST = 999_999_999


def andermant_of(blob: bytes) -> int | None:
    """The amount the state holds, or None when the state cannot be walked."""
    found = playerstate.progress_of(blob)
    if found is None:
        return None
    at = found["experience_at"] + ANDERMANT_REL
    if at + FIELD_BITS > len(blob) * 8:
        return None
    try:
        return BitReader(blob, at).read_uint(FIELD_BITS)
    except (IndexError, ValueError):
        return None


def with_andermant(blob: bytes, amount: int) -> bytes:
    """*blob* with the andermant set to *amount*, or unchanged.

    Unchanged, and said out loud, unless the field currently holds exactly what the
    recording holds. The offset is one measurement against one recording, so the guard is
    the only thing standing between a wrong reading and 631 KB of corrupted player state.
    """
    found = playerstate.progress_of(blob)
    if found is None:
        log.warning("player state does not read as one -- not touching the andermant")
        return blob
    at = found["experience_at"] + ANDERMANT_REL
    current = andermant_of(blob)
    if current is None:
        return blob
    wanted = max(0, min(MOST, int(amount)))
    if current == wanted:
        return blob
    if current != RECORDED_ANDERMANT:
        log.warning(
            "the andermant field reads %d, not the recorded %d -- not writing, because "
            "the offset is a single measurement and this is what says it is wrong",
            current,
            RECORDED_ANDERMANT,
        )
        return blob
    out = bytearray(blob)
    playerstate._write_uint(out, at, wanted, FIELD_BITS)
    log.info("player state: andermant %d -> %d", current, wanted)
    return bytes(out)
