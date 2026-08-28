#!/usr/bin/env python3
"""Extract real status effect elements from a capture, verbatim.

    python3 tools/element_table.py ~/dso-capture/session-*.jsonl > dsor/elements.py

Give it *every* capture. Taking them from one was a mistake that cost several rounds: the
skills capture holds 34 effects and none on a monster, so the stun, the poison and the
armour break looked as though the live service never sent them. The older tutorial
captures address monsters 11 distinct actors wide and carry all three.

Three attempts at building an element from scratch each got a field wrong -- the
sequencer field read as a track index, then as a stack size, then field 2 written as a
constant 25 when 185 of 365 real elements carry 50 or 75. Every one of those was a
correlation that held over the sample I happened to look at.

So this stops building them. An element is copied bit for bit from one the live service
sent for that same effect, and only the fields whose meaning is *established* are
rewritten: the three ticks and, in the message around it, the actor. Everything else
carries a value a real server sent.

The cost is honest and bounded: only the effects present in a capture can be served.
The three element lengths -- 276 bits with no parameters, 477 with parameters and no
vectors, 669 with vectors -- are exactly the three forms the grammar describes, which
is the strongest confirmation available that the grammar is right.
"""

import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from raknet.datagram import parse_datagram_header  # noqa: E402
from raknet.frame import parse_frames  # noqa: E402

VECTOR_BITS = 192


class Reader:
    __slots__ = ("data", "at")

    def __init__(self, data):
        self.data, self.at = data, 0

    def bits(self, count):
        value = 0
        for _ in range(count):
            value = (value << 1) | ((self.data[self.at >> 3] >> (7 - (self.at & 7))) & 1)
            self.at += 1
        return value

    def uint(self, count):
        return int.from_bytes(bytes(self.bits(8) for _ in range(count // 8)), "little")


def elements(payload):
    """Every element in a 0x004F, as (index, first bit, bit span).

    One line, deliberately. This function used to be a second copy of the grammar and
    the copy was wrong in two ways that fed each other:

    * its end condition read "the signed byte is not -1" instead of "the bool at +0x38
      is false **and** that byte is -1", so an element with the bool set was cut 192
      bits short and everything behind it was read at a wrong offset;
    * it then looped, treating whatever followed as another chained 0x004F whenever the
      next sixteen bits happened to read 0x4F -- which, after a mis-walk, they sometimes
      did.

    Together those invented elements out of neighbouring commands' bytes and filed them
    under whatever sixteen bits sat at the wrong offset. warrior_spikedShield_buff,
    which appears in no capture at all, got a 669-bit element that way; casting Dragon
    Hide drew Spike Shield.

    Measured after the fix: 86,477 of 86,477 real 0x004F messages parse end to end with
    the 0xFF terminator verified, and **none** of them carries a second chained command.
    So there was nothing for the loop to find in the first place.
    """
    from dsor.recorded import walk_elements

    found, _actor = walk_elements(payload, strict=True)
    return found


def slice_bits(body, start, span):
    """*span* bits from *start*, packed into bytes from bit zero."""
    out = bytearray((span + 7) // 8)
    for index in range(span):
        source = start + index
        if (body[source >> 3] >> (7 - (source & 7))) & 1:
            out[index >> 3] |= 1 << (7 - (index & 7))
    return bytes(out)


def main(captures: list[str], database: str) -> None:
    names = {
        row[0] - 1: row[1]
        for row in sqlite3.connect(f"file:{database}?mode=ro", uri=True).execute(
            "SELECT rowid, Id FROM _Template_StatusEffect"
        )
    }
    found = {}
    seen: dict[int, list[tuple[int, bytes]]] = {}
    skipped = 0
    import collections as _c
    refused = _c.Counter()
    for capture in captures:
      for line in pathlib.Path(capture).read_text(errors="ignore").splitlines():
          try:
              record = json.loads(line)
          except Exception:
              continue
          if not record.get("from_server") or "hex" not in record:
              continue
          try:
              raw = bytes.fromhex(record["hex"])
              _header, offset = parse_datagram_header(raw)
              frames = parse_frames(raw, offset)
          except Exception:
              continue
          for frame in frames:
              payload = frame.payload
              if frame.split_count is not None or len(payload) < 4:
                  continue
              if payload[0] != 0x85 or payload[1:3] != (0x004F).to_bytes(2, "little"):
                  continue
              body = payload[3:]

              # The shared grammar, strict: it either walks the whole message with
              # its 0xFF terminator verified, or it refuses. A refusal means the read
              # is off, and a partial walk cannot be told from a complete one -- so
              # nothing from it is harvested.
              #
              # Which replaces two earlier attempts. The first was a majority vote on
              # the span, and that was patching: it assumed most parses were right, and
              # its own output showed spans splitting 2-2 for one effect -- a parser
              # contradicting itself, which a vote cannot see. The second gated on the
              # frame's declared bit length, which is a real proof but only for a frame
              # carrying nothing else. The terminator check is the proof the format
              # itself offers, and it holds for 86,477 of 86,477 real messages.
              try:
                  walked = elements(payload)
              except ValueError as unparsed:
                  refused[str(unparsed)[:60]] += 1
                  skipped += 1
                  continue
              for index, start, span in walked:
                  # Chronological captures, so the newest observation of an effect wins:
                  # it is the one taken on this client build by this account.
                  seen.setdefault(index, []).append(
                      (span, slice_bits(body, start, span))
                  )

    # Every sample came from a message the grammar walked whole, so a disagreement
    # between samples of one effect is information rather than parser noise. Reported,
    # not voted away: if it happens the grammar is still incomplete, and that is worth
    # knowing rather than smoothing over.
    import collections

    disagree = 0
    for index, samples in seen.items():
        spans = collections.Counter(span for span, _bits in samples)
        if len(spans) > 1:
            disagree += 1
            print(f"# {index}: spans differ {dict(spans)}", file=sys.stderr)
        found[index] = samples[-1]
    print(
        f"{len(found)} effects from {sum(len(v) for v in seen.values())} observations; "
        f"{skipped} messages refused; {disagree} effects whose samples disagree",
        file=sys.stderr,
    )
    for why, n in refused.most_common(5):
        print(f"# refused {n}x: {why}", file=sys.stderr)

    print(f'''"""Real status effect elements, copied bit for bit off the wire.

Generated by ``tools/element_table.py``; do not edit by hand.

Three attempts at building an element got a field wrong. The 8-bit field past the
parameters was read as a track index and then as a stack size, and field 2 was written
as a constant 25 when 185 of 365 real elements carry 50 or 75. Each was a correlation
that held over the sample I happened to look at and not over the next one.

So an element is not built. It is copied from one the live service sent for that same
effect, and only the fields whose meaning is established get rewritten: the three ticks,
and the actor in the message around it.

The cost is that only the effects a capture contains can be served -- {len(found)} of
them here. The gain is that every field this server does not understand carries a value
a real server chose.

Element lengths are 276 bits with no parameters, 477 with parameters and no vectors and
669 with vectors, which are exactly the three forms the grammar describes.
"""

from __future__ import annotations

#: Effect wire index -> (bit length, the element's bits packed from bit zero).
ELEMENTS: dict[int, tuple[int, bytes]] = {{''')
    for index in sorted(found):
        span, bits = found[index]
        print(f"    {index}: ({span}, bytes.fromhex({bits.hex()!r})),  # {names.get(index, '?')}")
    print("}")

    # A donor: the element to lend to an effect that has none of its own.
    #
    # Building one from corpus constants was the previous answer and it is weaker than
    # this. The vector-carrying form is the majority -- 93 of 119 real elements -- and
    # its constants differ from the other form's: field 2 is 75 in 58 of them where the
    # 477-bit form is unanimously 25, and fields 1 and 5 are zero in 63 of them where
    # the other form carries real ticks. Choosing between those by counting is how the
    # last three readings of these fields went wrong.
    #
    # So an effect with no element of its own borrows a real one whole. Every field this
    # server does not understand -- the vectors, field 2, the flags -- then carries a
    # value the live service actually sent, and only the index, the ticks and the
    # parameters are written.
    #
    # The one chosen is the commonest vector-carrying element in the capture.
    donors = [
        (index, span, bits)
        for index, (span, bits) in found.items()
        if span == 669
    ]
    if donors:
        index, span, bits = donors[0]
        print()
        print("#: The element lent to an effect that has none of its own. See above for")
        print(f"#: why it is borrowed rather than built. Taken from {names.get(index, '?')}.")
        print(f"DONOR: tuple[int, bytes] = ({span}, bytes.fromhex({bits.hex()!r}))")
    else:
        print()
        print("DONOR: tuple[int, bytes] | None = None")
    print('''

def element(wire: int) -> tuple[int, bytes] | None:
    """The real element for *wire*, or None if no capture contained one."""
    return ELEMENTS.get(wire)


def known() -> frozenset[int]:
    """Every effect a real element exists for."""
    return frozenset(ELEMENTS)''')


if __name__ == "__main__":
    main(
        [a for a in sys.argv[1:] if a.endswith(".jsonl")],
        str(pathlib.Path.home() / "dso/db/db_static.sqlite"),
    )
