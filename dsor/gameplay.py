"""Decoders for the two high-rate gameplay messages.

Most of the field layout came from two captures where the player stood still,
walked, stood still again, and took four hits: a field that freezes while the
player is stationary and drifts while walking identifies itself. Some of it came
from the client's binary and its database instead, and those parts say so.

A field is described here only when the evidence forces it:

* **Position** — the three ``int16`` at the start are frozen bit-for-bit through
  every stationary phase across 1,855 consecutive message pairs, and drift
  smoothly while walking. Two perpendicular walks separated the axes: one moved
  almost purely in the field at offset 0, the other almost purely in the field at
  offset 4, while offset 2 stayed put — which is what elevation does on flat
  ground.
* **Speed and heading** — bytes 6, 7 and 8. See the constants below; the heading
  law was fitted against 114 real movement pairs and the alternatives rejected by
  a wide margin.
* **Tick** — byte 9 advances even while standing still, so it is a clock and not
  a movement counter.
* **Property updates** — 0x85/0x007B carries an identifier and a ``float32``. The
  four hits stepped one property by 0.2 each; a different property in another
  session runs 70..100. The identifier is what gives the value meaning.

Two readings were tried and **refuted**, and are recorded as such so they are not
proposed again:

* bytes 7 and 8 are *not* reliably equal (about half the time during movement),
  so they are not a duplicated heading. The observation was right and the
  conclusion drawn from it was not. Nor are they "the current heading and the one
  being turned toward", which was the second reading and also too loose: byte 7 is
  the heading being *travelled* along and byte 8 is the body's own facing. Over
  twelve live sessions, 128,580 stationary records carry zero in byte 7 99.9% of
  the time — a player standing still travels nowhere — while byte 8 holds a real
  facing across 147 distinct values. Byte 8 is therefore the one to ask which way
  somebody points, in either state;
* the server's position is *not* bit-identical to the client's — it tracks within
  a few tens of units, which is ordinary authority lag. An early sample of two
  messages happened to match exactly and that was over-read.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

#: int16 x, int16 elevation, int16 y
POSITION_SIZE = 6

#: Body length of the client's movement message (0x8B, opcode 0x005F).
CLIENT_MOVEMENT_SIZE = 15

#: Byte 6 is a **speed**, not a flag, and zero means standing still.
#:
#: 0x40 is the walking speed and for a long time it was the only non-zero value in
#: any capture, so this was modelled as a two-valued flag and anything else was
#: refused outright. That refusal took the server down the first time a status effect
#: actually worked: a warrior under warshout's 40% movement buff reported 0x59, and
#: 0x40 x 1.4 is 89.6 -- 0x59 is 89. The client had applied the buff and was saying
#: so, and the decoder called it impossible.
#:
#: So the name is kept for the walking speed it is, and any non-zero value means
#: moving.
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

    ``direction`` is the pair of bytes at offsets 7 and 8: the heading being
    travelled along, and the body's own facing. Both are in 256ths of a turn,
    clockwise from +y, the same scale as :data:`HEADING_UNITS`.

    Byte 7 is zero at rest, because a player standing still is travelling nowhere:
    99.9% of 128,580 stationary records across twelve live sessions. An earlier note
    here read the resting pair as (0, 128) and called the encoding unestablished;
    128 was simply the facing that session, and the same measurement shows 147
    distinct values in byte 8.

    So ``direction[1]`` is what answers "which way is this character pointing",
    whether or not they are moving. ``direction[0]`` answers it only while they
    walk, and using it to aim a skill pointed every blow along +y — because a
    player attacks standing still.
    """

    position: Position
    #: Byte 6, the character's own speed. 0x40 walking, 0 standing still, and higher
    #: under a movement buff -- 0x59 for a warrior under warshout, which is 0x40 times
    #: 1.4. Kept as the raw byte because it is the raw byte that carries the buff.
    speed: int
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
    return ClientMovement(
        position=decode_position(body, 0),
        speed=body[6],
        moving=body[6] != 0,
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
            bytes([movement.speed]),
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


# ── Several entities in one update ──────────────────────────────────────────
#
# A 0x85/0x005F body can carry more than one entity, and the way it does so is
# worth stating because it is not a count: the records are simply **chained with a
# two-byte separator** that is the opcode itself, 0x005F little-endian. The sizes in
# a real session confirm it — 42 bytes is two records, 64 is three, 152 is seven,
# each being 20*n + 2*(n-1).
#
# Inside a record, the first six bytes are the position, in the same three signed
# 16-bit fields as everywhere else. Bytes 9 and 10 are a 16-bit identifier and byte
# 15 an index; the eight bytes after that never varied across 3,395 observed
# records, so they are carried unchanged.

#: The two bytes between consecutive entity records.
ENTITY_SEPARATOR = bytes([0x5F, 0x00])

#: Length of one record. Every one of the 3,395 single-entity updates was this long.
ENTITY_RECORD_SIZE = 20

#: Where the index sits. Distinct per entity: the player's was 0x15 and the
#: creatures around it ran 0x08 to 0x14.
ENTITY_INDEX_OFFSET = 15


def split_entity_group(body: bytes) -> list[bytes]:
    """Split a multi-entity 0x005F body into its records."""
    return body.split(ENTITY_SEPARATOR)


def encode_entity_group(records: list[bytes]) -> bytes:
    """Chain *records* into one 0x005F body."""
    if not records:
        raise ValueError("an entity update needs at least one record")
    return ENTITY_SEPARATOR.join(records)


def reposition_entity(record: bytes, position: Position) -> bytes:
    """Return *record* moved to *position*, everything else untouched.

    Only the leading six bytes change. Rewriting anything else risks detaching the
    record from the entity the zone content defined, and an entity the client does
    not know about is one it will not draw.
    """
    if len(record) != ENTITY_RECORD_SIZE:
        raise ValueError(
            f"an entity record is {ENTITY_RECORD_SIZE} bytes, got {len(record)}"
        )
    return encode_position(position) + record[POSITION_SIZE:]


def entity_index(record: bytes) -> int:
    """The index byte, which distinguishes one entity from another."""
    return record[ENTITY_INDEX_OFFSET]


def encode_entity_group_message(
    records: list[bytes], trailing: list[bytes] | None = None
) -> bytes:
    """Build a 0x85/0x005F carrying several entities, and anything batched behind.

    The single-entity form that :func:`encode_entity_update` produces is this with
    one record, so the two agree by construction.

    ``trailing`` holds whole messages to append as further commands of the same
    batch, each stripped of its own 0x85 header. This is not decoration: all 56
    skill commands the real server sent in the long session ride inside a movement
    batch, immediately behind the records, and not one travelled alone. The
    records end byte-aligned on their terminator, so a command appended here
    starts on a byte boundary exactly as the capture shows.
    """
    body = encode_entity_group(records)
    for message in trailing or []:
        body += message[1:]
    return bytes([0x85]) + (0x005F).to_bytes(2, "little") + body


def ring_positions(centre: Position, count: int, radius: int = 600) -> list[Position]:
    """*count* positions evenly spaced on a circle around *centre*.

    Elevation is copied rather than computed: the ground height is not something
    this server knows, and every creature observed in the tutorial dungeon shared
    the player's elevation of -344.
    """
    import math

    return [
        Position(
            x=centre.x + round(radius * math.cos(2 * math.pi * i / count)),
            elevation=centre.elevation,
            y=centre.y + round(radius * math.sin(2 * math.pi * i / count)),
        )
        for i in range(count)
    ]


# ── the fields inside a movement record ─────────────────────────────────────
#
# Named from the client's own decoder rather than guessed. Earlier passes here read
# bytes 9-10 as an identifier and byte 15 as an index; both were parts of other
# fields, which is why neither ever quite made sense.

#: Where the start tick sits, and its width. The real server advances it by ten to
#: twelve on every update of the same entity, re-announcing the same position.
START_TICK_OFFSET = 9
START_TICK_SIZE = 4

#: Duration of the movement, immediately after. Zero in 620 of 621 observed updates
#: of one creature — a standing entity is announced with duration zero.
DURATION_OFFSET = 13
DURATION_SIZE = 2

#: The 32-bit actor id, which is what the client asks about when it does not
#: recognise an entity.
ACTOR_ID_OFFSET = 15
ACTOR_ID_SIZE = 4

#: One byte closing every command in a batch. Not a separator: the two bytes after
#: it are the next command's id.
COMMAND_TERMINATOR = 0xFF

#: Divisor between the wire's signed 16-bit coordinates and world units.
MOVE_SPEED_OFFSET = 6
"""Byte 6: how fast the entity is travelling, 0 when it stands still.

Read as a flag at first, because 0x40 and 0 are most of its values. The long
session refutes the flag: it also takes 0x1d, 0x20, 0x29, 0x2d, 0x31, 0x33,
0x36, 0x39 and every value from 0x3c to 0x41 — a continuum, not two states. It
is what makes the client dead-reckon, and it is what
``NetworkSmoothMotionProperty::UpdateMovementAnimation`` reads to pick the
animation: a zero here with a position that keeps changing is a standing
creature being teleported, which is exactly what this server used to draw.
"""

WALK_SPEED = 0x40
"""The value a walking entity carries. The player's own records cluster on
0x3c-0x41 for every sustained walk in the long session."""

HEADING_OFFSET = 7
HEADING_GOAL_OFFSET = 8
"""Bytes 7 and 8: the heading being travelled along, and the body's own facing.

Not interchangeable, and which one is read matters. Byte 7 is a *movement* heading
and a player standing still is travelling nowhere: over twelve live sessions, 128580
stationary records carry zero there 99.9% of the time, while byte 8 carries a real
facing across 147 distinct values. While moving the two agree to within three units
in 88% of 5318 records.

So byte 8 is the one to ask which way somebody is pointing, in either state. Byte 7
answers it only while they are walking, and reading it for a skill aimed every blow
north — a player attacks standing still.

An earlier reading — "a duplicated heading" — was refuted because the two are
unequal about half the time while moving. That refutation was right and the
conclusion drawn from it was wrong: they are *two* headings, and they differ
exactly while the entity turns. Byte 7 predicts the direction actually
travelled to within 3.2 degrees over 114 movement pairs, byte 8 does not.
"""

HEADING_UNITS = 256
"""A full turn. Clockwise from +y: ``angle = 90 deg - heading * 360 / 256``.

Measured, not assumed. Records with heading 0 move purely +y and records with
heading 128 purely -y, and the intermediate values follow: median error +0.4
degrees, standard deviation 3.2, 99% of 114 pairs within 15 degrees. Fitting
byte 8 instead, or any of the four other axis conventions, gives 73 degrees or
worse.
"""

WALK_UNITS_PER_TICK = 6
"""Wire units a walking entity covers per game tick.

Every clean axis-aligned sample in the long session moves 24 to 25 units in
four to five ticks. At 40 ms a tick that is about 1.2 world units a second.
Creatures here were stepped 60 units per 100 ms update — four times a player's
run — which is the other half of why they appeared to teleport.
"""


def heading_to(dx: float, dy: float) -> int:
    """The heading byte for travel along *(dx, dy)*, clockwise from +y."""
    return round(math.atan2(dx, dy) * HEADING_UNITS / (2 * math.pi)) % HEADING_UNITS


WORLD_SCALE = 128


def actor_id(record: bytes) -> bytes:
    """The four bytes identifying whose movement this is."""
    return record[ACTOR_ID_OFFSET : ACTOR_ID_OFFSET + ACTOR_ID_SIZE]


def with_motion(
    record: bytes,
    position: Position,
    start_tick: int,
    duration: int = 0,
    speed: int | None = None,
    heading: int | None = None,
) -> bytes:
    """Return *record* at *position*, stamped with a fresh tick and duration.

    A stale start tick is the difference between this server and the real one: it
    re-announced each entity every tick with the counter advanced, while this repeated
    the recorded value for ever. Duration is what the client interpolates over, so a
    non-zero one is what makes an entity glide rather than jump.
    """
    if len(record) != ENTITY_RECORD_SIZE:
        raise ValueError(
            f"an entity record is {ENTITY_RECORD_SIZE} bytes, got {len(record)}"
        )
    out = bytearray(record)
    out[0:POSITION_SIZE] = encode_position(position)
    out[START_TICK_OFFSET : START_TICK_OFFSET + START_TICK_SIZE] = (
        start_tick % 2**32
    ).to_bytes(START_TICK_SIZE, "little")
    out[DURATION_OFFSET : DURATION_OFFSET + DURATION_SIZE] = (
        duration % 2**16
    ).to_bytes(DURATION_SIZE, "little")
    if speed is not None:
        out[MOVE_SPEED_OFFSET] = speed & 0xFF
    if heading is not None:
        out[HEADING_OFFSET] = heading % HEADING_UNITS
        out[HEADING_GOAL_OFFSET] = heading % HEADING_UNITS
    return bytes(out)


# ── actor stats ─────────────────────────────────────────────────────────────
#
# Commands::ActorStatsUpdateCommand, 0x85/0x007B. Seventeen bytes on the wire and
# the layout is plain once the batch framing is accounted for:
#
#     eb 00 00 00 00 00 00 00 | cd cc 4c 3e | 15 00 01 00 | ff
#     stat id, then seven bytes | float value | actor id    | terminator
#
# Three stat ids appear in the reference session, all of them for the player and
# never for a creature:
#
#     0xEC   55 times, 0.00 to 63.60, falling in steps of exactly 5.00 and
#            recovering in steps of 0.2 — the one that depletes
#     0xEB   37 times, 0.20 to 10.00
#     0xEA   15 times, 0.80 to 9.60
#
# Reading 0xEC as health is an inference from that shape, and applying any of them to
# a creature is an extrapolation: the capture contains no example of a monster's
# stats being reported at all.

STATS_OPCODE = 0x007B

#: The stats command carries two numbers, and they are current hit points and the
#: resource a skill spends — not a selector and a value, and not a maximum and a
#: current:
#:
#:     eb 00 00 00 00 00 00 00 | cd cc 4c 3e | 15 00 01 00 | ff
#:     hit points, int64       | resource, f32| actor id    | terminator
#:
#: The client's observer vtable is what settles it: the setter for the first field
#: notifies OnHealthPointsChanged, the one for the second OnSkillResourceChanged. So
#: 234-236 was a nearly full health bar, and the float falling by exactly 5.00 per cast
#: and recovering at 0.2 was mana or rage.
#: Width of the current hit points, and of the skill resource that follows.
HIT_POINTS_SIZE = 8
RESOURCE_SIZE = 4


def encode_actor_vitals(hit_points: int, resource: float, actor: bytes) -> bytes:
    """Build a 0x85/0x007B reporting *actor*'s current health and skill resource.

    The two fields were misread twice here, and each misreading looked plausible. Byte
    0 takes only three values across a whole session — 234, 235, 236 — so it read as a
    stat selector; then the pair read as a maximum and a current value. The client's
    own setters settle it: the first eight bytes go to SetHealthPoints and the four
    after them to SetSkillResource.

    So the float that fell by exactly 5.00 per cast and recovered at 0.2 was never
    health at all — it was the resource a skill spends. Writing a damage value there
    drains the player's mana and leaves their health untouched, which is what happened
    for one round of testing.
    """
    if len(actor) != ACTOR_ID_SIZE:
        raise ValueError(f"an actor id is {ACTOR_ID_SIZE} bytes, got {len(actor)}")
    return (
        bytes([0x85])
        + STATS_OPCODE.to_bytes(2, "little")
        + hit_points.to_bytes(HIT_POINTS_SIZE, "little")
        + struct.pack("<f", resource)
        + actor
        + bytes([COMMAND_TERMINATOR])
    )


# ── where a creature actually stands ────────────────────────────────────────
#
# Not in the movement message. A creature is pinned by the position inside its own
# description, as three 32-bit floats in **world** units — and a position update for
# it is discarded, because the client only applies those to an actor it has bound to
# an entity. That is why hundreds of movement updates moved nothing: the creature was
# never anywhere but where its description put it.
#
# The offset is a rule, like the handle's: 281 bytes from the end in all six recorded
# descriptions, whose sizes are 397, 406 and 411.

#: How far from the end of a description its spawn position sits.
SPAWN_FROM_END = 281


def monster_spawn(description: bytes) -> tuple[float, float, float]:
    """Where *description* places its creature, in world units."""
    offset = len(description) - SPAWN_FROM_END
    if offset < 3:
        raise ValueError(
            f"a description is at least {SPAWN_FROM_END + 3} bytes, "
            f"got {len(description)}"
        )
    return struct.unpack_from("<fff", description, offset)


def with_spawn(
    description: bytes, x: float, elevation: float, y: float
) -> bytes:
    """Return *description* with its creature placed at a different position.

    Twelve bytes change and nothing else, so the creature, its template and its
    handle are the recorded ones — only where it stands is ours.
    """
    offset = len(description) - SPAWN_FROM_END
    if offset < 3:
        raise ValueError(f"description too short: {len(description)} bytes")
    out = bytearray(description)
    struct.pack_into("<fff", out, offset, x, elevation, y)
    return bytes(out)
