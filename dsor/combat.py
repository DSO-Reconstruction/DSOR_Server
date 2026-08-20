"""Combat and actor-lifecycle commands, generated rather than replayed.

Replaying recorded combat got the emulator as far as creatures that died in one
blow, stood at zero health and flickered — because a recorded batch carries another
session's damage numbers, positions and loot. These encoders replace that.

Every layout here is read from the client's own decoders, and the widths matter more
than usual. The stream is a RakNet ``BitStream``: when a read's offset and width are
both whole bytes it degenerates to a memcpy of little-endian data, which is why a
byte-oriented reader worked on the movement and stats messages. The moment a 1-bit
field appears, everything behind it shifts and is packed most-significant-first. So
:class:`~raknet.bitstream.BitWriter` is required for anything carrying a boolean, and
a plain byte writer is enough for anything that does not.

The four commands here split neatly along that line:

* ``0x002B`` and the two vicinity commands are byte-aligned throughout.
* ``0x006B`` and ``0x006C`` carry booleans, so they are assembled bit by bit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from raknet.bitstream import BitWriter

#: Message id every server-to-client game command travels under.
MESSAGE_ID = 0x85

#: One byte closes each command in a batch; the next two bytes are the following
#: command's id.
TERMINATOR = 0xFF

DISCARD_MONSTER = 0x002B
ACTORS_LEFT_VICINITY = 0x0073
ACTORS_ENTER_VICINITY = 0x0074
HIT = 0x006B
KILL = 0x006C

#: What the client uses for "no actor". A hit or a kill carrying it in the trailer is
#: refused outright.
INVALID_ACTOR = 0xFFFFFFFF


def _open(writer: BitWriter, command: int) -> None:
    writer.write_uint(command, 16)


def _close(writer: BitWriter, actor: int) -> None:
    """The trailer every actor command ends with: whose it is, then the terminator."""
    writer.write_uint(actor, 32)
    writer.write_uint(TERMINATOR, 8)


def encode_discard_monster(actor: int) -> bytes:
    """0x002B: delete a creature's entity and actor outright.

    Its body is **empty** — the actor id in the trailer is the whole message — so it
    needs no recording at all, which is what makes it the way to retire a creature
    this server has no captured removal for.

    Note what it is not: a death. There is no animation and no corpse. For a creature
    to die visibly, send :func:`encode_kill` first.
    """
    writer = BitWriter()
    writer.write_uint(MESSAGE_ID, 8)
    _open(writer, DISCARD_MONSTER)
    _close(writer, actor)
    return writer.to_bytes()


def _encode_actor_list(command: int, actors: list[int], sender: int) -> bytes:
    writer = BitWriter()
    writer.write_uint(MESSAGE_ID, 8)
    _open(writer, command)
    writer.write_uint(len(actors), 32)
    for actor in actors:
        writer.write_uint(actor, 32)
    _close(writer, sender)
    return writer.to_bytes()


def encode_actors_enter_vicinity(actors: list[int], sender: int) -> bytes:
    """0x0074: these actors are near you.

    A count then that many 32-bit ids, all byte-aligned. For any id the client does
    not know, its handler calls RequestActor directly — which is the leg that makes a
    creature ask to be described, rather than merely being noticed in a position
    update.
    """
    return _encode_actor_list(ACTORS_ENTER_VICINITY, actors, sender)


def encode_actors_left_vicinity(actors: list[int], sender: int) -> bytes:
    """0x0073: these actors are no longer near you.

    Identical in shape to its counterpart, and worth being precise about what it
    does: it sets each entity **invisible**. It does not kill, it does not remove, and
    a creature hidden this way is still standing there as far as everything else is
    concerned. Mistaking this for death is why creatures stayed on screen at zero
    health.
    """
    return _encode_actor_list(ACTORS_LEFT_VICINITY, actors, sender)


@dataclass
class Kill:
    """0x006C KillCommand: what actually makes a creature die."""

    victim: int
    killer: int
    #: Where the corpse is thrown, as three floats.
    impulse: tuple[float, float, float] = (0.0, 0.0, 0.0)
    tick: int = 0
    kill_tick: int = 0
    #: Whether the body is removed rather than left lying.
    despawn: bool = True
    damage_types: list[int] = field(default_factory=list)
    unknown_48: int = 0
    unknown_4c: int = 0
    unknown_74: int = 0


def encode_kill(kill: Kill) -> bytes:
    """Build a 0x006C.

    Byte-aligned up to the despawn flag; that single bit shifts the trailer and the
    terminator, which is the whole reason this cannot be assembled from bytes.
    """
    import struct

    writer = BitWriter()
    writer.write_uint(MESSAGE_ID, 8)
    _open(writer, KILL)
    writer.write_uint(kill.tick, 32)
    writer.write_uint(len(kill.damage_types), 32)
    for kind in kill.damage_types:
        writer.write_uint(kind & 0xFF, 8)
    writer.write_uint(kill.unknown_48, 32)
    writer.write_uint(kill.unknown_4c, 32)
    writer.write_uint(kill.killer, 32)
    for component in kill.impulse:
        writer.write_uint(int.from_bytes(struct.pack("<f", component), "little"), 32)
    writer.write_uint(kill.kill_tick, 32)
    writer.write_uint(kill.unknown_74, 32)
    writer.write_bool(kill.despawn)
    _close(writer, kill.victim)
    return writer.to_bytes()
