"""Walking a command chain without knowing what any command contains.

A 0x85 payload carries one or more commands back to back. Reading it used to mean
knowing each command's fields, so a chain containing anything unfamiliar was opaque
past its first command -- and that opacity is where this project's worst bug came
from: the element harvester read an index at an offset its own mis-walk had chosen,
and filed foreign bytes under it.

There is a way through that needs no body layout at all, and it comes out of how the
client reads a command rather than out of the bytes. ``DrasaClientHandler::DecodeCommand``
calls two methods on every command:

    call [vtable + 0x28]   slot 5   the command's own fields
    call [vtable + 0x48]   slot 9   0x140cc8a74 reads **32 bits** into this+0x18

Slot 9 is the *same function* for every command -- ``HitCommand`` and
``StatusEffectCommand`` share it -- and its predicate is ``mov al, 1; ret``, so it
always reads. Those 32 bits are the actor. After them comes ``0xFF``, which the chain
reader consumes before looking for the next command's 16-bit id.

So every command, whatever it is, ends with::

    32 bits   the actor, always in the 0x0001xxxx page
     8 bits   0xFF
    16 bits   the next command's id, or the end of the frame

Measured across the three largest captures: that tail is found for 0x005F (90,508 of
92,613), 0x004F, 0x006B, 0x002A, 0x007B, 0x006C, 0x007D, 0x0074, 0x007C and 0x0073 --
every command id it could be looked for in. 0x002D and 0x00A7 do not fit it and are
reported rather than forced.

Which makes a chain walkable by *searching* for that tail instead of measuring a body:
scan forward for a 0xFF whose preceding 32 bits look like an actor and whose following
16 bits are a command id the client would accept. It is a constrained search, not a
guess, and every candidate it takes is one three independent checks agree on.
"""

from __future__ import annotations

from dataclasses import dataclass

from raknet.bitstream import BitReader
from raknet.payload import bits_of

#: The container.
#: The chained container, which the **server** sends. Each command in it ends with a
#: 32-bit actor and a 0xFF, and several follow one another.
MULTI = 0x85

#: The single container, which the **client** sends. It is not the same shape: one
#: command id and its body, with *no* actor and *no* terminator -- ``8b 25 00 00`` is a
#: whole payload in four bytes. Measured over 494,348 client payloads: 0x8B leads every
#: one of them, and this walker read none, because it looks for a tail that does not
#: exist on that side. So there is nothing to walk in a 0x8B: the id is bytes 1 and 2,
#: little-endian, and the body is everything after.
SINGLE = 0x8B

#: The id past which DecodeCommand refuses: ``cmp r9w, 0x177 / jb``.
MOST_COMMANDS = 0x177

#: How high an actor's page may go for a tail to be believed.
#:
#: This was ``== 1`` and that was a local value read as a universal one: the tutorial
#: capture numbers its actors 0x0001xxxx, so every actor this server had ever seen sat
#: in page one. A capture taken against the **official** server -- data this rule had
#: never been fitted to -- numbers them 0x0003xxxx, and the rule fell from 100% to
#: 75.66% on it. That is what a blind test is for.
#:
#: The bound is measured rather than picked. Against the official capture and this
#: server's side by side:
#:
#:     page == 1        75.66%  official   100%  local   no ambiguity
#:     page <= 0xFF     99.86%  official   100%  local   32 of 4,203 payloads ambiguous
#:     no check at all  99.93%  official   100%  local   far worse, up to 21 candidates
#:
#: So the last 0.07% costs a great deal of certainty and is not taken.
MOST_ACTOR_PAGES = 0xFF

#: What closes a command.
TERMINATOR = 0xFF

#: The command's trailing fields, in bits: the actor and the terminator.
TAIL_BITS = 32 + 8

#: How much padding a byte-buffered stream can leave behind the last command.
PADDING_BITS = 7

#: Memo sentinel for "no segmentation from here". None already means something else.
_NONE = object()


@dataclass(frozen=True)
class Command:
    """One command found in a chain."""

    id: int
    #: Where its body begins and ends, in bits from the start of the payload's body
    #: (that is, past the 0x85 and the first command id).
    start: int
    end: int
    actor: int

    @property
    def body_bits(self) -> int:
        return self.end - self.start


class _Bits:
    """The payload as one integer, so a bit window is a shift rather than a read.

    The search walks every bit position, so the cost of extracting a window is the
    whole cost. A BitReader per candidate made a two-capture audit time out; this makes
    it arithmetic.
    """

    __slots__ = ("value", "total")

    def __init__(self, body: bytes, total: int) -> None:
        self.value = int.from_bytes(body, "big")
        self.total = len(body) * 8

    def at(self, position: int, count: int) -> int:
        shift = self.total - position - count
        if shift < 0:
            raise IndexError(position)
        return (self.value >> shift) & ((1 << count) - 1)


def _swapped(value: int) -> int:
    """*value*'s four bytes reversed: the little-endian reading of 32 wire bits."""
    return int.from_bytes(value.to_bytes(4, "big"), "little")


def _identifier(bits: "_Bits", at: int) -> int:
    """The command id sitting at bit *at*: sixteen bits, little-endian.

    Its own function because reading it big-endian is a bug this walker shipped, and
    the symptom was silent. A chained id is two bytes low-first -- ``3e 00`` is
    0x003E -- and reading the window as it lies makes it 0x3E00, which fails the
    ``< 0x177`` bound DecodeCommand applies. So *every* chained command was rejected
    while the first, read from the header as ``payload[1] | payload[2] << 8``, was
    right. That is why this walker reported exactly one command per payload, and why
    the earlier "68 of 91 frames chain another command" reading was withdrawn: the
    withdrawal was the mistake, not the reading.
    """
    window = bits.at(at, 16)
    return ((window & 0xFF) << 8) | (window >> 8)


def actor_of(raw: int) -> int:
    """The actor a tail's 32 bits name, taking whichever byte order lands in the page."""
    if raw == 0:
        return 0
    swapped = _swapped(raw)
    if (swapped >> 16) <= MOST_ACTOR_PAGES:
        return swapped
    return raw


def _tail_at(bits: _Bits, at: int, total: int) -> int | None:
    """The actor, if a command's tail sits at bit *at*. None otherwise.

    Three conditions, all of them the client's own: the 32 bits are an actor in the
    0x0001xxxx page, the byte after them is 0xFF, and what follows is either the end of
    the payload or a command id DecodeCommand would accept.
    """
    if at + TAIL_BITS > total:
        return None
    if bits.at(at + 32, 8) != TERMINATOR:
        return None
    actor = bits.at(at, 32)
    # The actor is little-endian on the wire, so the page is its *high* half once
    # byte-swapped -- which is what the 0x0001xxxx pattern is in memory.
    #
    # Zero is legal and had to be learned: the recorded tutorial heal carries actor 0
    # because the *world* applies it rather than a character. Refusing zero left every
    # 0x002D, every 0x00A7 and 1,943 of the 0x005F payloads unaccounted for.
    # Either byte order. Almost every tail reads as four little-endian bytes -- the
    # wire holds 86 00 01 00 for actor 0x00010086 -- but 1,943 of 44,615 entity
    # updates carry it the other way round, reading 0x0001007e straight off the bits.
    # Which of the two is the writer's intent is *not* settled here, and forcing one
    # was what left those unaccounted for. Both are accepted, the ambiguity is
    # recorded, and :func:`actor_of` says which reading was taken.
    if actor != 0 and min(_swapped(actor) >> 16, actor >> 16) > MOST_ACTOR_PAGES:
        return None
    # And a low half that is not sixteen set bits, which no actor has. This is the
    # condition that was missing, and it cost two rounds of testing: a run of 0xFF
    # bytes inside a body reads as actor 0x0000ffff -- page 0, which is "at most
    # MOST_ACTOR_PAGES" -- with a terminator behind it. That made a NewPlayerCommand
    # appear to end 712 bits before it does, and an exact segmentation of a truncated
    # message is the worst kind of wrong answer: the walk covered the payload, the
    # client said "RakNetStream::ReadBits(): error while reading stream!", and no
    # player was ever created.
    #
    # Measured before changing it: the loose test found 0x0000ffff tails in both
    # recorded arrivals and in nothing else this walker is used on.
    if actor != 0 and (actor & 0xFFFF) == 0xFFFF and (_swapped(actor) & 0xFFFF) == 0:
        return None
    left = total - (at + TAIL_BITS)
    # The end, or the end plus padding. RakNet's BitStream is byte-buffered: whatever
    # the last write leaves, the stream is rounded up to a byte and the frame's length
    # field states that rounded figure -- measured as a multiple of eight in 92,557 of
    # 92,613 real 0x005F frames. So up to seven bits of padding sit behind the last
    # command, and refusing them is what made this walker account for only 47% of the
    # traffic: 43,009 of 43,091 status effect payloads were rejected over two spare
    # bits.
    if left <= PADDING_BITS:
        return actor
    if left < 16:
        return None
    if _identifier(bits, at + TAIL_BITS) >= MOST_COMMANDS:
        return None
    return actor


def walk(payload: bytes, most: int = 64) -> tuple[list[Command], int]:
    """Every command in *payload*, and how many bits are left unaccounted for.

    The leftover is the honest part. Zero means the whole payload was walked; anything
    else is the size of what this cannot read, reported rather than hidden.

    **Why this searches instead of scanning forward.** A BitStream carries no offsets
    and no lengths, so a candidate tail cannot be checked locally -- 32 bits that read
    as an actor, a 0xFF and a small id occur inside a float array often enough. The
    one external truth is the frame's declared bit length, so the test of a
    segmentation is whether it *ends where the payload ends*. Taking the first
    plausible tail is what let a false positive cut a message in half and leave 24
    bits over; requiring exact coverage makes the length do the work it is the only
    thing able to do. The memo keeps that affordable: each start position is solved
    once, so the cost is positions times candidates rather than exponential.
    """
    if len(payload) < 3 or payload[0] != MULTI:
        return [], len(payload) * 8

    total = bits_of(payload) - 24
    body = payload[3:]
    bits = _Bits(body, total)
    first = payload[1] | (payload[2] << 8)

    def complete(at: int, identifier: int, depth: int) -> list[Command] | None:
        """A segmentation of ``at..total``, or None when there is none."""
        if depth > most:
            return None
        seen = memo.get(at)
        if seen is not None:
            return None if seen is _NONE else seen
        for probe in range(at, total - TAIL_BITS + 1):
            actor = _tail_at(bits, probe, total)
            if actor is None:
                continue
            end = probe + TAIL_BITS
            here = Command(identifier, at, end, actor_of(actor))
            if total - end <= PADDING_BITS:
                memo[at] = [here]
                return [here]
            rest = complete(end + 16, _identifier(bits, end), depth + 1)
            if rest is not None:
                memo[at] = [here] + rest
                return memo[at]
        memo[at] = _NONE
        return None

    memo: dict[int, object] = {}
    whole = complete(0, first, 0)
    if whole is not None:
        return whole, 0
    # No segmentation covers the payload. Report the greedy reading and the size of
    # what is left, rather than claiming the payload was understood.
    return _greedy(bits, total, first, most)


def _greedy(bits: "_Bits", total: int, identifier: int, most: int):
    """First-match reading, for a payload no exact segmentation covers."""
    found: list[Command] = []
    at = 0
    while len(found) < most:
        tail = None
        for probe in range(at, total - TAIL_BITS + 1):
            actor = _tail_at(bits, probe, total)
            if actor is not None:
                tail = (probe, actor)
                break
        if tail is None:
            return found, total - at
        probe, actor = tail
        end = probe + TAIL_BITS
        found.append(Command(identifier, at, end, actor_of(actor)))
        if total - end <= PADDING_BITS:
            return found, 0
        identifier = _identifier(bits, end)
        at = end + 16
    return found, total - at


def summary(payload: bytes) -> str:
    """One line saying what a payload holds, for a log or a report."""
    found, left = walk(payload)
    parts = [f"{c.id:#06x}/{c.body_bits}b/{c.actor:#x}" for c in found]
    return f"{len(found)} command(s): {', '.join(parts)}" + (
        f"; {left} bits unaccounted" if left else "; whole payload accounted"
    )


def single(payload: bytes) -> tuple[int, bytes] | None:
    """The id and body of a client's 0x8B payload, or None if it is not one.

    There is no walking to do. The client sends one command per payload with no actor
    and no terminator, which is why :func:`walk` -- built for the server's chained
    0x85 -- accounted for 0 of 4,000 client payloads even when told to accept the
    leading byte. Two containers, two shapes.
    """
    if len(payload) < 3 or payload[0] != SINGLE:
        return None
    return payload[1] | (payload[2] << 8), payload[3:]
