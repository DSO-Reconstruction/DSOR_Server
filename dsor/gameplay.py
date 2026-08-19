"""Decoders for the two high-rate gameplay messages.

Everything here was recovered by controlled experiment rather than by reading
code: two captures where the player stood still, walked, stood still again, and
in the second one took four hits from an enemy. Static analysis could not have
produced any of it — the fields are only visible in what changes and what does
not.

The method matters because it decides what may be claimed. A field is described
here only when the capture forces it:

* **Position** — the three ``int16`` at the start are frozen bit-for-bit through
  every stationary phase across 1,855 consecutive message pairs, and drift
  smoothly while walking. Two perpendicular walks separated the axes: one moved
  almost purely in the field at offset 0, the other almost purely in the field at
  offset 4, while offset 2 stayed at the same value throughout both — which is
  what elevation does on flat ground.
* **Move flag** — byte 6 is 0x40 or 0 and nothing else, across 21,259 messages.
  When it is 0 the position essentially never changes: zero exceptions in the two
  short experiments, 17 in 7,794 pairs over a long session. Treated as a strong
  tendency, not an invariant, because the long session says so.
* **Tick** — byte 9 advances even while standing still, so it is a clock and not
  a movement counter.
* **Property updates** — 0x85/0x007B carries an identifier and a ``float32``. The
  four hits stepped one property by 0.2 each; a different property in another
  session runs 70..100. The identifier is what gives the value meaning.

Two readings were tried and **refuted**, and are recorded as such so they are not
proposed again:

* bytes 7 and 8 are *not* reliably equal (about half the time during movement),
  so they are not a duplicated heading;
* the server's position is *not* bit-identical to the client's — it tracks within
  a few tens of units, which is ordinary authority lag. An early sample of two
  messages happened to match exactly and that was over-read.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

#: int16 x, int16 elevation, int16 y
POSITION_SIZE = 6

#: Body length of the client's movement message (0x8B, opcode 0x005F).
CLIENT_MOVEMENT_SIZE = 15

#: Value of byte 6 while the character is moving. The only other value observed
#: is 0, and byte 6 is never anything else.
MOVING = 0x40

#: Bytes 12..14. Constant across 21,259 messages from three independent sessions,
#: which is why these three and not more: bytes 10 and 11 look constant in a short
#: capture and are not — see CLIENT_TRAILER_START.
CLIENT_TRAILER = bytes([0, 20, 0])

#: Offset where the constant trailer begins. Bytes 10 and 11 sit before it and do
#: vary; they were briefly modelled as part of a fixed trailer because both held
#: one value throughout two short experiments. A 15-minute session showed byte 11
#: taking 0, 4, 7, 65, 50 and byte 10 ranging widely. Constant in one session is
#: not constant.
CLIENT_TRAILER_START = 12

#: Offset of the float32 in a 0x85/0x007B property update.
PROPERTY_VALUE_OFFSET = 8

#: How far the server's idea of a position may sit from the client's own while
#: the client is stationary. Measured maximum over both captures was well inside
#: this; it exists to describe authority lag, not to hide a decoding error.
POSITION_TOLERANCE = 32


@dataclass(frozen=True)
class Position:
    """A world position as both directions encode it.

    The same three fields, in the same order and at the same offset, appear in the
    client's movement message and in the server's entity update — so one codec
    serves both, which is the practical payoff of the whole exercise.

    Units are unknown. Walking for roughly three seconds moved the player about
    2,800 of them, so they are far finer than a tile.
    """

    x: int
    elevation: int
    y: int

    def distance_to(self, other: "Position") -> float:
        """Horizontal distance, ignoring elevation."""
        return ((self.x - other.x) ** 2 + (self.y - other.y) ** 2) ** 0.5


def decode_position(buf: bytes, offset: int = 0) -> Position:
    """Read a position triple at *offset*."""
    if len(buf) < offset + POSITION_SIZE:
        raise ValueError(
            f"need {POSITION_SIZE} bytes for a position, "
            f"only {len(buf) - offset} left"
        )
    x, elevation, y = struct.unpack_from("<hhh", buf, offset)
    return Position(x, elevation, y)


def encode_position(position: Position) -> bytes:
    """Serialise a position triple."""
    return struct.pack("<hhh", position.x, position.elevation, position.y)


@dataclass(frozen=True)
class ClientMovement:
    """The client's 0x8B/0x005F message, sent continuously while in the world.

    ``direction`` is the pair of bytes at offsets 7 and 8. They clearly track the
    direction of travel — they take distinctly different values for two different
    walks, and sit at (0, 128) at rest — but what they encode is not established,
    so they are exposed as raw bytes rather than converted into an angle that
    would look more authoritative than the evidence.
    """

    position: Position
    moving: bool
    direction: tuple[int, int]
    #: Byte 9. Advances by a few units per message *even while stationary*, so it
    #: is a clock rather than anything to do with movement. Wraps at 256.
    tick: int
    #: Byte 10. Increments once per burst of movement in short captures, but takes
    #: many values over a long session, so "burst counter" is a guess and the raw
    #: value is what is exposed.
    counter: int
    #: Byte 11. Varies over a long session; meaning unknown.
    unknown: int
    #: Bytes 12..14, constant everywhere measured.
    trailer: bytes


def decode_client_movement(body: bytes) -> ClientMovement:
    """Decode the 15-byte body of a client movement message."""
    if len(body) != CLIENT_MOVEMENT_SIZE:
        raise ValueError(
            f"client movement body is {CLIENT_MOVEMENT_SIZE} bytes, got {len(body)}"
        )
    flag = body[6]
    if flag not in (0, MOVING):
        raise ValueError(f"unexpected move flag {flag:#02x}; only 0 and 0x40 seen")
    return ClientMovement(
        position=decode_position(body, 0),
        moving=flag == MOVING,
        direction=(body[7], body[8]),
        tick=body[9],
        counter=body[10],
        unknown=body[11],
        trailer=body[CLIENT_TRAILER_START:15],
    )


def encode_client_movement(movement: ClientMovement) -> bytes:
    """Serialise a client movement message body, reproducing the original bytes."""
    return b"".join(
        [
            encode_position(movement.position),
            bytes([MOVING if movement.moving else 0]),
            bytes(movement.direction),
            bytes([movement.tick, movement.counter, movement.unknown]),
            movement.trailer,
        ]
    )


@dataclass(frozen=True)
class EntityUpdate:
    """The server's 0x85/0x005F message: where an entity is.

    Only the leading position is decoded. The message carries more than one
    entity when it is longer than 20 bytes, but the layout past the first block is
    **not** established: sizes 20, 42, 64, 86 and 108 fit a fixed 22-byte block,
    while 80, 84 and 106 do not, so it is not a plain array. ``rest`` is handed
    back untouched rather than split on a rule that is wrong a third of the time.
    """

    position: Position
    rest: bytes

    @property
    def may_hold_more_entities(self) -> bool:
        """Whether this update is longer than a single-entity one."""
        return len(self.rest) > 14


def decode_entity_update(body: bytes) -> EntityUpdate:
    """Decode the leading position of a server entity update."""
    if len(body) < 20:
        raise ValueError(f"entity update is at least 20 bytes, got {len(body)}")
    return EntityUpdate(position=decode_position(body, 0), rest=body[POSITION_SIZE:])


@dataclass(frozen=True)
class PropertyUpdate:
    """A 0x85/0x007B message: one named quantity of one entity, as a float32.

    The scale depends on the property, which is the correction that matters here.
    Read as a single field it looks contradictory: during the four-hit experiment
    properties 0xEB and 0xEC stepped 0.2, 0.4, 0.6, 0.8, while a longer session
    shows property 0x02 running 70, 78, 86, 94, 100 in steps of 8. Same offset,
    same type, different units — so the value cannot be interpreted without its
    identifier, and naming this "hit fraction" was over-reading one experiment.
    """

    #: Byte 0. Selects what the value means.
    property_id: int
    #: Byte 1. A sub-selector or entity kind; 0x03 alongside property 0x02, 0x00
    #: alongside 0xEB/0xEC.
    sub_id: int
    value: float


def decode_property_update(body: bytes) -> PropertyUpdate:
    """Decode a 0x85/0x007B property update."""
    if len(body) < PROPERTY_VALUE_OFFSET + 4:
        raise ValueError(
            f"need {PROPERTY_VALUE_OFFSET + 4} bytes for a property update, "
            f"got {len(body)}"
        )
    return PropertyUpdate(
        property_id=body[0],
        sub_id=body[1],
        value=struct.unpack_from("<f", body, PROPERTY_VALUE_OFFSET)[0],
    )


# ── Combat and other events ─────────────────────────────────────────────────

#: Message that precedes a burst of property changes. Named for what it does
#: rather than for what it might be: in the four-hit experiment exactly one of
#: these arrived before each hit's property updates, so "attack" is a reasonable
#: reading, but 552 of them in a fifteen-minute session is more than that session
#: plausibly contained attacks on the player, so it is likely the general
#: "something happened to an entity" event.
EVENT_OPCODE = 0x006B

#: The property update that follows an event.
PROPERTY_OPCODE = 0x007B

#: How long after an event its property updates may arrive, in capture frames.
#: 80 was measured: at that window 81% of events in a held-out session are
#: followed by at least one update, and widening it mostly picks up the *next*
#: event's updates instead.
EVENT_WINDOW = 80


@dataclass(frozen=True)
class ActionEvent:
    """An event message together with the property changes it caused.

    This pairing is the closest thing to a decoded combat interaction available so
    far, and it was found by counting: four hits taken, four events, eight
    property updates, and a float that stepped by a fifth each time.

    Note what is *not* claimed. The event's own 65-to-161-byte body is not
    decoded, so which entity attacked which is unknown. And the event is not
    exclusive to combat — it fires far too often for that.
    """

    frame: int
    body: bytes
    properties: tuple[PropertyUpdate, ...]

    @property
    def changed_properties(self) -> set[int]:
        return {update.property_id for update in self.properties}


def find_action_events(
    timeline: list[tuple[int, str, bytes]], window: int = EVENT_WINDOW
) -> list[ActionEvent]:
    """Group event messages with the property updates that follow them.

    *timeline* is ``(frame, message name, body)`` in frame order, as produced by
    walking a capture. Only server messages should be passed in; an event and its
    consequences both travel server to client.

    An event with no updates is still returned, with an empty tuple: about a fifth
    of them have none, and silently dropping those would hide that.
    """
    event_name = f"0x85/{EVENT_OPCODE:#06x}"
    property_name = f"0x85/{PROPERTY_OPCODE:#06x}"

    updates = [
        (frame, decode_property_update(body))
        for frame, name, body in timeline
        if name == property_name and len(body) >= PROPERTY_VALUE_OFFSET + 4
    ]

    events = []
    for frame, name, body in timeline:
        if name != event_name:
            continue
        following = tuple(
            update
            for update_frame, update in updates
            if frame < update_frame <= frame + window
        )
        events.append(ActionEvent(frame=frame, body=body, properties=following))
    return events


# ── producing entity updates ────────────────────────────────────────────────

#: Entry positions observed the first time a map server reported a player on each
#: map. These are where the real server placed the character on arrival, so they
#: are spawn points rather than arbitrary samples.
#:
#: Taken from the first 20-byte position update each map server sent, which is
#: where the player actually arrives. An earlier value for the tutorial came from a
#: multi-entity snapshot recorded later in the session and was 3 km off.
SPAWN_POSITIONS = {
    "a0001_start_tutorial_dun": Position(-10172, -344, 5888),
    "a0006_grimford_hub": Position(-2070, 405, -1531),
    "a0007_pastures_pve": Position(-13417, 2, -238),
    "a0008_crypt_dun": Position(-8752, 526, 556),
    "a0301_witchhole": Position(-15135, -1428, 13851),
    # Three map servers reported three different positions for this one, which is
    # what a city with several entrances looks like; the first is kept.
    "a0200_kingscity": Position(-3040, -71, 2645),
}


def encode_entity_update(position: Position, template: bytes) -> bytes:
    """Build a 0x85/0x005F carrying *position*, over a recorded template.

    Only the position is generated. The fourteen bytes after it are copied from a
    real message, because their meaning is not established — an earlier pass
    identified some as constant across one session and a longer session showed
    several of them varying. Substituting the three coordinates into known-good
    bytes is honest; inventing fourteen is not.

    The template must be a 20-byte body, which is the single-entity form.
    """
    if len(template) != 20:
        raise ValueError(
            f"the single-entity body is 20 bytes, template is {len(template)}"
        )
    return (
        bytes([0x85])
        + (0x005F).to_bytes(2, "little")
        + encode_position(position)
        + template[POSITION_SIZE:]
    )
