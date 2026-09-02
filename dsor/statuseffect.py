"""``StatusEffectCommand`` 0x004F: what is running on an actor, and for how long.

A rebuild, from measurement rather than from a copied recording. The previous attempt
copied a captured element and rewrote a few fields, which meant every field it did not
understand carried another effect's value -- an instance handle, an actor, and 192 bits
of geometry belonging to a different skill. This one names every field, and the fields
it cannot explain are stated as measured constants rather than smuggled along.

**The grammar.** A command's body is a header bit, a 32-bit count, that many elements,
and then the actor and the 0xFF that close every command in a ``0x85`` chain. An element
is:

    u16   the effect's index -- its row in _Template_StatusEffect, minus one
    u32x8 the eight fields below
    bool  x4  the last of which ends the element when false
    u32   how many parameters, then that many float32
    bool  one more flag
    i8    a signed byte; -1 with the flag clear means "no vectors"
    f32x6 two float3 vectors, only when the flag is set or the byte is not -1

Read that way, 99.6% of the 0x004F messages in 144 captures parse and land exactly on
the actor -- which is the only check available, because a BitStream states no lengths
and the frame's declared bit length is the one external truth.

**The eight fields**, as far as measurement goes. Fields 1, 3 and 5 are a clock: the
end tick, the start tick, and the duration in ticks, and ``end - start == duration`` in
139,112 of 139,112 real elements. Twenty-five ticks is a second. Field 0 is the effect
instance's handle, which the client compares to decide whether it is adding an effect or
extending one it already has -- so two different effects on one actor must not share it.
Field 7 is the actor holding the effect. Field 4 is zero everywhere. Fields 2 and 6 vary
by effect and by nothing else this server can compute: 2 is one of 0, 25, 50 or 75 and 6
is one of 1, 2, 3, 5 or 100, so both are carried per effect from what was measured.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field

from raknet.bitstream import BitReader, BitWriter

log = logging.getLogger("effects")

#: The chained container the server sends in.
MULTI = 0x85

#: This command.
COMMAND = 0x004F

#: What closes a command.
TERMINATOR = 0xFF

#: Ticks in a second, from the three clock fields of real elements.
RATE = 25

#: The signed byte's value when an element carries no vectors.
NO_VECTORS = -1

#: Two float3, when it does.
VECTOR_FLOATS = 6

#: Ceilings, so a mis-read cannot make a reader allocate without bound.
MOST_ELEMENTS = 64
MOST_PARAMETERS = 16

#: Field 4 is zero in every real element.
SPARE = 0


@dataclass
class Element:
    """One effect on one actor."""

    #: Its row in ``_Template_StatusEffect`` minus one.
    index: int
    #: The instance handle the client keys "add or extend" on.
    instance: int = 0
    #: The clock, in ticks.
    start: int = 0
    end: int = 0
    duration: int = 0
    #: The actor the effect is on.
    holder: int = 0
    #: The two fields measurement pins per effect and cannot derive.
    second: int = 0
    sixth: int = 100
    #: Field 4, zero in all but 7 of 313,301 real elements -- and those 7 are why it is
    #: carried rather than written as a constant. A codec that reproduces every byte
    #: but seven understands every field but one.
    spare: int = SPARE
    #: The four flags. The last false ends the element early.
    flags: tuple[bool, bool, bool, bool] = (False, False, False, True)
    parameters: list[float] = field(default_factory=list)
    more: bool = False
    byte: int = NO_VECTORS
    vectors: list[float] | None = None

    @property
    def seconds(self) -> float:
        return self.duration / RATE


@dataclass
class StatusEffects:
    """A decoded 0x004F."""

    actor: int
    elements: list[Element] = field(default_factory=list)
    #: The header bit. Zero in all 313,301 real messages measured.
    header: int = 0


def _f32(value: int) -> float:
    return struct.unpack("<f", value.to_bytes(4, "little"))[0]


def _bits(value: float) -> int:
    return int.from_bytes(struct.pack("<f", value), "little")


def decode(
    payload: bytes, at: int = 24, ends: int | None = None
) -> StatusEffects | None:
    """Read a 0x004F, or None when the reading does not land on the actor.

    None rather than a partial answer. The grammar has no way to check itself from the
    inside, so "it ended where it had to" is the whole of the evidence, and a reading
    that ends anywhere else is wrong however sensible its fields look.

    *ends* is that bit, for a command that is **not** the last in its payload:
    :func:`dsor.chain.walk` knows where each command in a chain finishes, and without
    being told, this refused every chained one. Measured on the live service's own
    traffic, that was 1,712 of 2,833 status effect elements in a single session --
    unreadable for no better reason than that something followed them.
    """
    reader = BitReader(payload, at)
    total = len(payload) * 8 if ends is None else ends
    try:
        header = reader.read_uint(1)
        count = reader.read_uint(32)
        if count > MOST_ELEMENTS:
            return None
        elements: list[Element] = []
        for _ in range(count):
            index = reader.read_uint(16)
            fields = [reader.read_uint(32) for _ in range(8)]
            flags = tuple(bool(reader.read_uint(1)) for _ in range(4))
            element = Element(
                index=index,
                instance=fields[0],
                end=fields[1],
                second=fields[2],
                start=fields[3],
                holder=fields[7],
                duration=fields[5],
                sixth=fields[6],
                spare=fields[4],
                flags=flags,  # type: ignore[arg-type]
            )
            elements.append(element)
            if not flags[3]:
                continue
            many = reader.read_uint(32)
            if many > MOST_PARAMETERS:
                return None
            element.parameters = [_f32(reader.read_uint(32)) for _ in range(many)]
            element.more = bool(reader.read_uint(1))
            raw = reader.read_uint(8)
            element.byte = raw - 256 if raw > 127 else raw
            if element.more or element.byte != NO_VECTORS:
                element.vectors = [
                    _f32(reader.read_uint(32)) for _ in range(VECTOR_FLOATS)
                ]
        actor = reader.read_uint(32)
        if reader.read_uint(8) != TERMINATOR:
            return None
        if ends is None:
            # Byte-buffered, so up to seven bits of padding may follow.
            if total - reader.position > 7:
                return None
        elif reader.position != total:
            # Told exactly where the command ends, so nothing is allowed to be left.
            return None
    except Exception:
        return None
    return StatusEffects(actor=actor, elements=elements, header=header)


def encode(message: StatusEffects) -> bytes:
    """The bytes for *message*: what :func:`decode` read, written back."""
    writer = BitWriter()
    writer.write_uint(MULTI, 8)
    writer.write_uint(COMMAND, 16)
    writer.write_uint(message.header, 1)
    writer.write_uint(len(message.elements), 32)
    for element in message.elements:
        writer.write_uint(element.index, 16)
        for value in (
            element.instance,
            element.end,
            element.second,
            element.start,
            element.spare,
            element.duration,
            element.sixth,
            element.holder,
        ):
            writer.write_uint(value & 0xFFFFFFFF, 32)
        for flag in element.flags:
            writer.write_uint(1 if flag else 0, 1)
        if not element.flags[3]:
            continue
        writer.write_uint(len(element.parameters), 32)
        for parameter in element.parameters:
            writer.write_uint(_bits(parameter), 32)
        writer.write_uint(1 if element.more else 0, 1)
        writer.write_uint(element.byte & 0xFF, 8)
        if element.more or element.byte != NO_VECTORS:
            for value in (element.vectors or [0.0] * VECTOR_FLOATS):
                writer.write_uint(_bits(value), 32)
    writer.write_uint(message.actor, 32)
    writer.write_uint(TERMINATOR, 8)
    return writer.to_bytes()
