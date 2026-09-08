"""Another player, seen from this one's client.

A creature and a player arrive by the same route, which is the useful part: this server
already walks it for creatures.

    server   0x0074   ActorsEnterVicinityCommand -- an actor came into range
    client   0x001C   ActorRequestCommand -- who is that?
    server   0x0021   NewRemotePlayerCommand -- this is who
    server   0x005F   MoveCommand, from then on
    server   0x001E   DiscardPlayerCommand -- and they are gone

Read straight off the live service in ``officiel4``, which had other players standing
around the hub: 18 ``NewRemotePlayerCommand`` and, four messages before one of them, the
vicinity announcement and the client's own question.

The description is replayed rather than built, because it is 559 bytes of appearance --
``ranger_helmet_09``, ``ranger_torso_02``, ``cape_rc_02`` -- plus titles and achievements,
and none of that is understood. Two fields are rewritten and the rest is carried across
bit for bit:

* the **name**, length-prefixed at bit 190, which the client draws over their head;
* the **actor**, which the description writes **twice** --- once inside the body and
  once in the chained command's tail with the ``0xFF``. Both are rewritten; rewriting
  only the tail leaves the description announcing the recording's actor, and the client
  draws nothing.

So a second player looks like the recorded one and answers to their own name. That is a
real limit and it is the same one the creatures had before their appearance was read: a
replayed description shows the recording's body.
"""

from __future__ import annotations

from pathlib import Path

from raknet.bitstream import BitReader
from raknet.payload import Payload, bits_of, respan

#: The container.
MULTI = 0x85

#: ``NewRemotePlayerCommand``, the answer to "who is that".
NEW_REMOTE_PLAYER = 0x0021

#: ``RemotePlayerInfoCommand``, which has to follow the description.
#:
#: This was the missing half. The live service sends **62** of these across the
#: captures against 27 ``NewRemotePlayerCommand``, and this server sent none -- so the
#: client was handed a player and never told what it looks like. It carries the worn
#: appearance as template names: ``event_merchant_all_helmet_01_set``,
#: ``mage_female_2h_weapon``, ``wfx_unique_mage_2h_staff2h_mortis_01`` in the one
#: recorded here, and lengths from 259 to 744 bytes across the others because the list
#: is per character.
#:
#: The pairing is the same shape as everywhere else in this protocol: ``NewNPCCommand``
#: 0x0026 has ``NPCInfoCommand`` 0x0028 behind it, and ``NewRemotePlayerCommand`` 0x0021
#: has this.
REMOTE_PLAYER_INFO = 0x0022

#: Its declared length. **Not** a multiple of eight, which is the whole reason it is
#: written down: the command ends at bit 4,074 and a 509-byte extraction truncated it by
#: two bits. The actor sits at 4,034 where a byte-aligned tail would put it at 4,032, and
#: that two-bit gap is the only thing that says the file is short.
INFO_BITS = 4074

#: ``DiscardPlayerCommand``, eight bytes, sent when one leaves.
DISCARD_PLAYER = 0x001E

#: Where the name's 16-bit length prefix sits in the recorded description. The name
#: follows it at bit 206.
NAME_LENGTH_AT = 190
NAME_AT = NAME_LENGTH_AT + 16

#: What every command ends with: the actor, then this.
ACTOR_BITS = 32
TERMINATOR = 0xFF
TAIL_BITS = ACTOR_BITS + 8

#: The name the recording carries, and the actor it belonged to.
RECORDED_NAME = "MiyavSu"
RECORDED_ACTOR = 0x0002215A

#: A name the client will draw. Bounded because the field is 16 bits and because a name
#: this server invents should not be the thing that overflows something.
LONGEST_NAME = 64

#: Where the command's own fields start, past the container, the id and 32 bits that are
#: not identified. Not guessed: walking the transcribed field widths (below) from bit 56
#: lands the first string's length prefix on bit 190, which is where the name already
#: known to be, and no other start does.
BODY_AT = 56

#: ``NewRemotePlayerCommand::Decode``, vtable slot 5 at ``0x1409689b4``, transcribed:
#: thirty-five reads, each one a call to a width-specific reader that passes its bit
#: count to the stream's ``ReadBits``. The first eleven, which is as far as the name and
#: the template row need:
#:
#:   ==========  =========  ==================================================
#:   struct      width      reader
#:   ==========  =========  ==================================================
#:   +0x20..24   1 bit x5   ``0x140cc83b0``, ReadBits(1)
#:   +0x28       32         ``0x1409f889c``, a u32 enum refused above 2
#:   +0x2c       1
#:   +0x30       64         ``0x140cc8774``, two floats
#:   +0x40       32         ``0x140cc8520``, a float
#:   +0x48       string     ``0x140cc888c``, 16-bit length then bytes -- the **name**
#:   +0x88       string     the guild: 'The\xd1\x84rder' in the recording
#:   +0xc8..cd   8 bits x6  ``0x140cc8a14``
#:   +0xd0       32         ``0x140cc86ac`` -- the **template row**
#:   ==========  =========  ==================================================
#:
#: and behind it two counted collections (``0x14094bc58`` and ``0x14094b4f4``, each a
#: u32 count refused above a million and then that many elements), the first of which
#: holds 8 in the recording, then twenty more fields. Those are not transcribed: the
#: description is replayed, so only the fields this server rewrites need naming.

#: How many bits sit between the end of the guild string and the template row: the six
#: single-byte fields at +0xc8.
ROW_BACK_BITS = 48

_DATA = Path(__file__).with_name("data")


def description() -> bytes:
    """The recorded ``NewRemotePlayerCommand``, as a standalone message."""
    return (_DATA / "remote_player.bin").read_bytes()


def info() -> Payload:
    """The recorded ``RemotePlayerInfoCommand``, as a standalone message.

    A :class:`Payload` and not bytes, because its declared length is 4,074 bits inside
    510 bytes and the six padding bits are not part of the command.
    """
    return Payload((_DATA / "remote_player_info.bin").read_bytes(), INFO_BITS)


def _bits(payload: bytes) -> str:
    return "".join(f"{byte:08b}" for byte in payload)


def _to_bytes(bits: str) -> bytes:
    pad = -len(bits) % 8
    return int(bits + "0" * pad, 2).to_bytes((len(bits) + pad) // 8, "big")


def named(payload: bytes) -> str:
    """The name the description carries."""
    length = BitReader(payload, NAME_LENGTH_AT).read_uint(16)
    return bytes(
        BitReader(payload, NAME_AT + 8 * index).read_bits(8) for index in range(length)
    ).decode("latin-1")


def _string_at(payload: bytes, at: int) -> tuple[str, int]:
    """The length-prefixed string at *at*, and the bit it ends on."""
    length = BitReader(payload, at).read_uint(16)
    raw = bytes(
        BitReader(payload, at + 16 + 8 * index).read_bits(8) for index in range(length)
    )
    return raw.decode("latin-1"), at + 16 + 8 * length


def guild_of(payload: bytes) -> str:
    """The guild name, the string straight after the player's own."""
    _name, after = _string_at(payload, NAME_LENGTH_AT)
    return _string_at(payload, after)[0]


def template_row_at(payload: bytes) -> int:
    """The bit the template row sits on, found by walking rather than written down.

    It moves with both strings, so there is no offset to write down --- which is the
    same reason the actor is found from the end.
    """
    _name, after_name = _string_at(payload, NAME_LENGTH_AT)
    _guild, after_guild = _string_at(payload, after_name)
    return after_guild + ROW_BACK_BITS


def template_row(payload: bytes) -> int:
    """Which row of ``Managers::TemplateManager`` draws this player.

    The one field of the description whose failure the client names out loud::

        ClientPlayerManager::HandleNewRemotePlayerCommand():
            no template found at row index %d!

    Read at ``0x140218499`` as ``[command + 0xd0]``, handed to the template manager at
    ``0x1402184d9``, and on a false return the handler logs that and gives up --- so a
    row this client cannot resolve is a player it will not draw. The recording holds
    **1**, which is a plausible row and not obviously the fault; if the client ever
    prints that line, this is the field to move.
    """
    return BitReader(payload, template_row_at(payload)).read_uint(32)


def with_template_row(payload: bytes, row: int) -> Payload:
    """*payload* drawn from a different template row."""
    at = template_row_at(payload)
    total = bits_of(payload)
    stream = _bits(payload)
    spliced = stream[:at] + _bits(row.to_bytes(4, "little")) + stream[at + 32 : total]
    return Payload(_to_bytes(spliced), total)


def actor_of(payload: bytes) -> int:
    """The actor the description is about: the last 40 bits, before the terminator."""
    at = bits_of(payload) - TAIL_BITS
    return BitReader(payload, at).read_uint(32)


def with_name(payload: bytes, name: str) -> Payload:
    """*payload* naming *name*.

    The field is length-prefixed, so everything behind it moves and the message changes
    size -- which is fine, and is why the actor is found from the end rather than from a
    written-down offset.
    """
    raw = name.encode("latin-1")
    if not raw or len(raw) > LONGEST_NAME:
        raise ValueError(f"a name is 1 to {LONGEST_NAME} bytes, got {len(raw)}")
    was = BitReader(payload, NAME_LENGTH_AT).read_uint(16)
    stream = _bits(payload)
    head = stream[:NAME_LENGTH_AT]
    tail = stream[NAME_AT + 8 * was :]
    length = _bits(len(raw).to_bytes(2, "little"))
    spliced = head + length + _bits(raw) + tail
    return Payload(_to_bytes(spliced), len(spliced))


def occurrences(payload: bytes, actor: int) -> list[int]:
    """Every bit offset at which *actor* is written in *payload*.

    A 32-bit window matching by accident has a chance of about one in a million per
    message, so a value that turns up twice turns up twice because it was written
    twice.
    """
    total = bits_of(payload)
    return [
        at
        for at in range(total - ACTOR_BITS + 1)
        if BitReader(payload, at).read_uint(ACTOR_BITS) == actor
    ]


def with_actor(payload: bytes, actor: int) -> Payload:
    """*payload* describing *actor* instead of the one it recorded.

    **Every** occurrence, not just the trailing one. The recorded description writes its
    actor twice --- at bit 3918 inside the body and at 4432 in the chained command's
    tail --- and rewriting only the tail is what made a second player invisible: the
    client was handed a description that announced itself as the recording's actor while
    everything around it, the vicinity announcement and every MoveCommand, spoke about
    the new one. Nothing bound, so nothing was drawn.

    Rewriting all of them rather than the one at a measured offset, because the offset
    moves: it sits behind two length-prefixed strings and two counted collections, and
    three of the last fields are strings as well. The value is its own address.

    This is the second time the same shape of bug has cost a day. The roster wrote the
    character id in the header and left the entry claiming the recorded character; this
    wrote the actor in the tail and left the body claiming the recorded actor. **When a
    replayed message is rebound to a new id, look for every place that id is written.**
    """
    total = bits_of(payload)
    tail_at = total - TAIL_BITS
    was = BitReader(payload, tail_at).read_uint(ACTOR_BITS)
    if was == actor:
        return respan(payload, bytes(payload))
    at_all = occurrences(payload, was)
    if tail_at not in at_all:  # pragma: no cover - the tail is read from the tail
        raise ValueError("the tail actor is not where the tail is")
    stream = _bits(payload)
    fresh = _bits(actor.to_bytes(4, "little"))
    for at in at_all:
        stream = stream[:at] + fresh + stream[at + ACTOR_BITS :]
    return Payload(_to_bytes(stream[:total]), total)


def player(actor: bytes, name: str) -> Payload:
    """The description of *actor*, drawn as the recording and named *name*."""
    if len(actor) != 4:
        raise ValueError(f"an actor id is four bytes, got {len(actor)}")
    out = with_name(description(), name)
    return with_actor(out, int.from_bytes(actor, "little"))


def appearance(actor: bytes) -> Payload:
    """The ``RemotePlayerInfoCommand`` for *actor*: what the player looks like.

    Sent behind :func:`player`, because the description on its own is not enough. The
    order is the one the protocol uses for every other pair of this shape -- the "new"
    command then the "info" command -- and it is what a client needs before it will draw
    anything.
    """
    if len(actor) != 4:
        raise ValueError(f"an actor id is four bytes, got {len(actor)}")
    return with_actor(info(), int.from_bytes(actor, "little"))


def described(actor: bytes, name: str) -> list[Payload]:
    """Everything the client needs to draw *actor*: the description, then the look."""
    return [player(actor, name), appearance(actor)]


def left(actor: bytes) -> bytes:
    """A ``DiscardPlayerCommand`` for *actor*.

    Eight bytes, and the recorded pair differ in exactly the four that are the actor --
    ``85 1e 00 | 0f 21 02 00 | ff`` and ``85 1e 00 | 4d 0b 02 00 | ff`` -- so there is
    nothing else in it to get wrong.
    """
    if len(actor) != 4:
        raise ValueError(f"an actor id is four bytes, got {len(actor)}")
    return bytes([MULTI]) + DISCARD_PLAYER.to_bytes(2, "little") + actor + bytes([TERMINATOR])
