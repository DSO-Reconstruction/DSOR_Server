"""The two commands that tell the client an actor's level and stats.

These are the answer to a long search. Every status effect this server sent was
correct -- the codec reproduces all 313,301 real 0x004F messages byte for byte, the
database is the client's own file down to its md5, the wire index mapping is uniquely
best at ``rowid - 1``, the message is field-for-field the live service's, and the
messages provably arrived -- and nothing appeared on screen.

The client's own code says why. ``Game::ClientStatusEffectManager``'s handler for a
StatusEffectCommand looks the actor up in ``ClientActorManager``, then calls into
``statuseffectmanager.cc`` to create each effect instance, and that function asserts:

    causer.isvalid()
    effectTemplate->IsValid()
    0 < causerLevel
    MaxActorLevel >= causerLevel

**``0 < causerLevel``.** When the instance is not created the handler jumps to a block
that advances to the next element and logs nothing at all -- a silent skip, per element,
for the whole message. So an actor whose level the client never learned can receive no
status effect, and no amount of correcting the effect message changes that.

And the level is exactly what this server never sent. Of everything the live service
sends that this one did not, on captures that are certainly the service's and nothing
else, there were two commands left: ``0x007C PlayerLevelUpdateCommand`` and
``0x007B ActorStatsUpdateCommand``.

The layouts, from the service's own bytes::

    0x007C   857c00 68000000 00000000000000000000000000 15000100 ff
             u32 level (0x68 = 104), fourteen zero bytes, actor, terminator

    0x007B   857b00 d0dd0600 00000000 00009642 15000100 ff
             u32, u32 zero, float32 value, actor, terminator

The 0x007B samples carry 75.0, 80.0, 85.0, 90.0 and 95.0 with a varying first field, so
it is a value changing over time rather than a table of attributes. Which is exactly what
it is, and :func:`dsor.gameplay.encode_actor_vitals` already had it from the client's own
setters -- the first eight bytes go to ``SetHealthPoints`` and the four after them to
``SetSkillResource``. The live service says the same thing out loud: over one session
its 248 messages for the player's actor hold a first field that plateaus at 2,757,733
and falls under fire, and a float that starts at 115.6348, drops as skills are cast,
touches 0.0000 once, and climbs back to 115.6348 every time. A current health and a
current resource, nothing else.

Only the level is sent from here, because the level is what the assert reads and the
vitals have their own encoder. This module is kept for the level and for the record of
what the stats command is.
"""

from __future__ import annotations

import struct

#: The container the server sends in.
MULTI = 0x85

#: What closes a command.
TERMINATOR = 0xFF

#: ``PlayerLevelUpdateCommand``.
PLAYER_LEVEL = 0x007C

#: ``ActorStatsUpdateCommand``.
ACTOR_STATS = 0x007B

#: The zero bytes between the level and the actor, counted off the recorded message.
LEVEL_PADDING = 14

#: The ceiling the client asserts against, from ``MaxActorLevel >= causerLevel``. The
#: level table runs to 110, and a value past whatever the client's constant is would
#: trip the assert rather than be ignored.
HIGHEST_LEVEL = 110


def player_level(level: int, actor: bytes) -> bytes:
    """A ``0x007C`` telling the client *actor* is at *level*.

    Byte-for-byte the shape of the recorded one. The level is clamped rather than
    trusted: the client asserts ``MaxActorLevel >= causerLevel``, and an assert in a
    release build of Nebula3 is a message box and a dead client, not a warning.
    """
    kept = max(1, min(HIGHEST_LEVEL, int(level)))
    return (
        bytes([MULTI])
        + struct.pack("<H", PLAYER_LEVEL)
        + struct.pack("<I", kept)
        + bytes(LEVEL_PADDING)
        + actor
        + bytes([TERMINATOR])
    )


def actor_stats(first: int, value: float, actor: bytes) -> bytes:
    """A ``0x007B``. Kept for symmetry with the recording; nothing sends it yet.

    *first* is the leading 32 bits, which the samples vary (450000, 449488, 447762) and
    whose meaning is not established.
    """
    return (
        bytes([MULTI])
        + struct.pack("<H", ACTOR_STATS)
        + struct.pack("<I", first)
        + struct.pack("<I", 0)
        + struct.pack("<f", value)
        + actor
        + bytes([TERMINATOR])
    )
