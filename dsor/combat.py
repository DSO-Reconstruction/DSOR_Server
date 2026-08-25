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

import math
import struct
from dataclasses import dataclass, field

from dsor.gameplay import HEADING_UNITS
from raknet.bitstream import BitWriter

#: Message id every server-to-client game command travels under.
MESSAGE_ID = 0x85

#: One byte closes each command in a batch; the next two bytes are the following
#: command's id.
TERMINATOR = 0xFF

DISCARD_MONSTER = 0x002B
ACTORS_LEFT_VICINITY = 0x0073
ACTORS_ENTER_VICINITY = 0x0074
TARGET_SKILL = 0x0047
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
    #: **Where the creature dies**, in world units, as three floats. Decoding a real
    #: kill reads (-49.41, 0.00, 53.45) — squarely inside the range the tutorial
    #: dungeon's creatures stand in, so this is a position and not the direction an
    #: earlier reading took it for. Sending a direction here puts the death at the map
    #: origin.
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    tick: int = 0
    #: Zero in the recorded kill, so zero here.
    kill_tick: int = 0
    #: Whether the body is removed rather than left lying. False in the recorded kill.
    despawn: bool = False
    #: One entry, value zero, in the recorded kill.
    damage_types: list[int] = field(default_factory=lambda: [0])
    #: Copied from the recorded kill rather than understood. 75 and 0.
    unknown_48: int = 75
    unknown_4c: int = 0
    #: 0xFFFFFFFF in the recorded kill — the client's "no actor" value.
    unknown_74: int = 0xFFFFFFFF


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
    for component in kill.position:
        writer.write_uint(int.from_bytes(struct.pack("<f", component), "little"), 32)
    writer.write_uint(kill.kill_tick, 32)
    writer.write_uint(kill.unknown_74, 32)
    writer.write_bool(kill.despawn)
    _close(writer, kill.victim)
    return writer.to_bytes()


@dataclass
class Hit:
    """0x006B HitCommand: one blow, and the victim's health after it.

    The field that matters most is not the damage but ``victim_health``: the client
    takes the victim's health straight from this message. Replaying a recorded blow
    therefore replays the health it left behind — and a *killing* blow carries zero,
    so the victim dies on the spot by the unanimated path. Its own log says so
    plainly: "Victim ... is not alive or cannot receive", then "received kill message
    twice" when the real KillCommand arrives after.

    So a creature can only survive a blow, and only die with an animation, if this
    message is generated.
    """

    victim: int
    attacker: int
    damage: int
    victim_health: int
    victim_max_health: int
    #: Whose blow the floating number belongs to. **The attacker**, in all 74 real
    #: hits across two independent sessions — never the victim. An earlier note here
    #: claimed the client draws nothing unless this is the local player's actor; the
    #: samples refute it outright, and this server had been sending the player.
    combat_value_owner: int
    combat_value: float = 1.0
    tick: int = 0
    critical: bool = False
    shield: int = 0
    max_shield: int = 0
    damage_types: list[int] = field(default_factory=lambda: [0])
    #: Three, in every one of the 74 real hits from two sessions and two creature
    #: skills. Zero was this server's invention and nothing observed carries it.
    kind: int = 3
    heavy_until_tick: int = 0


def encode_hit(hit: Hit) -> bytes:
    """Build a 0x006B.

    Five of its eighteen fields are single bits, so everything behind them is shifted
    and packed most-significant-first — a byte writer cannot produce this.
    """
    import struct

    writer = BitWriter()
    writer.write_uint(MESSAGE_ID, 8)
    _open(writer, HIT)
    writer.write_uint(hit.tick, 32)
    writer.write_uint(len(hit.damage_types), 32)
    for kind in hit.damage_types:
        writer.write_uint(kind & 0xFF, 8)
    writer.write_bool(False)
    writer.write_bool(hit.critical)
    writer.write_bool(False)
    writer.write_uint(hit.victim_health & (2**64 - 1), 64)
    writer.write_uint(hit.victim_max_health & (2**64 - 1), 64)
    writer.write_uint(hit.shield & 0xFFFFFFFF, 32)
    writer.write_uint(hit.max_shield & 0xFFFFFFFF, 32)
    writer.write_uint(hit.damage & 0xFFFFFFFF, 32)
    writer.write_uint(hit.kind & 0xFFFFFFFF, 32)
    writer.write_uint(hit.attacker, 32)
    writer.write_uint(hit.combat_value_owner, 32)
    writer.write_uint(hit.heavy_until_tick, 32)
    writer.write_uint(
        int.from_bytes(struct.pack("<f", hit.combat_value), "little"), 32
    )
    writer.write_bool(False)
    writer.write_bool(False)
    writer.write_uint(0, 16)  # a string the inbound path never uses; length zero
    _close(writer, hit.victim)
    return writer.to_bytes()


@dataclass
class TargetSkill:
    """0x0047 TargetSkillCommand: what makes a creature swing.

    There is no "play this animation" command in the protocol — 371 command classes
    and not one names a sequence, an animation or a gesture. A creature swings if and
    only if it is sent a skill, and everything visible follows client-side: the skill's
    type picks a visualizer, the visualizer looks its sequence up in the client's own
    data, and the sequence drives the animation. The hit message asks for no animation
    at all, for anyone.

    Two constraints come from the client and cannot be worked around:

    * the skill must be one the creature's own template grants. The tutorial
      dungeon's creature has ``AnderworldCreatureStrike``, and its actor is asked
      whether it holds that template before anything is visualised.
    * ``start_tick`` is compared against the client's own game tick, which advances as
      the network tick over 32. A stale one makes the visualizer finish the moment it
      starts, and a command that arrives before its creature exists is queued and then
      discarded unless the creature appears within five game ticks.
    """

    attacker: int
    target: int
    skill_id: int
    heading: float = 0.0
    start_tick: int = 0
    #: Zero passes the client's high-water check, which is read but never written.
    high_water: int = 0
    unknown_c8: int = 0
    #: Zero. One skips movement modulation and transform correction.
    flag: bool = False
    #: The skill's own timing, from its row in the client's _Template_Skill. These
    #: eight fields are the whole reason nothing ever animated: this command is 64
    #: bytes on the wire and this server was sending 26. The terminator landed where
    #: the client expects the impact tick, so the visualizer was handed nonsense for
    #: its duration and its position and finished in the same breath it started —
    #: about a hundredth of a second of the swing, which is exactly what was seen.
    #:
    #: Confirmed on two live attacks by two different creatures: impact tick equals
    #: start plus HitFrame (8890+15=8905 and 8905+19=8924, both exact), and the two
    #: duration fields are SkillUnblockFrame and MotionUnblockFrame (30/30 and
    #: 41/41, both exact).
    hit_frame: int = 0
    unblock_frame: int = 0
    #: The attacker's position, in the *description* frame rather than the wire one.
    #: Read off both samples: the offset from each attacker's movement record is
    #: identical for both creatures, +11.11 in x and +42.97 in z, which is the second
    #: coordinate frame already documented for the kill position.
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    #: One in both samples. A rate, most likely; reproduced rather than understood.
    factor: float = 1.0


def encode_target_skill(skill: TargetSkill) -> bytes:
    """Build a 0x0047.

    Its last field before the trailer is a single bit, so the trailer and terminator
    behind it are shifted and this cannot be assembled from whole bytes.
    """
    import struct

    writer = BitWriter()
    writer.write_uint(MESSAGE_ID, 8)
    _open(writer, TARGET_SKILL)
    writer.write_uint(skill.high_water, 16)
    writer.write_uint(skill.skill_id, 16)
    writer.write_uint(
        int.from_bytes(struct.pack("<f", skill.heading), "little"), 32
    )
    writer.write_uint(skill.start_tick % 2**32, 32)
    writer.write_uint(skill.unknown_c8, 32)
    writer.write_bool(skill.flag)
    writer.write_uint(skill.target, 32)
    writer.write_uint(skill.attacker, 32)
    writer.write_uint((skill.start_tick + skill.hit_frame) % 2**32, 32)
    writer.write_uint(skill.hit_frame & 0xFFFFFFFF, 32)
    # Twice: SkillUnblockFrame then MotionUnblockFrame. Equal in every sample, and
    # equal in the table for all three skills looked up, so they are written from one
    # value rather than pretending to know a case where they differ.
    writer.write_uint(skill.unblock_frame & 0xFFFFFFFF, 32)
    writer.write_uint(skill.unblock_frame & 0xFFFFFFFF, 32)
    writer.write_uint(0, 32)
    for value in (*skill.position, skill.factor):
        writer.write_uint(
            int.from_bytes(struct.pack("<f", value), "little"), 32
        )
    # One more bit, false. Found by search rather than by reading: without it the
    # command is 62 of 64 bytes right, and with it all 64 match a real one exactly.
    writer.write_bool(False)
    # No trailing actor: this command names its attacker in the body, so the trailer
    # is the terminator alone.
    writer.write_uint(TERMINATOR, 8)
    return writer.to_bytes()


#: Commands::XPChangedCommand, and its neighbours, named from the binary by walking
#: the Rtti registration back to the id getter at vtable slot 3.
XP_CHANGED = 0x007D
PLAYER_LEVEL_UPDATE = 0x007C
GROUP_XP_CHANGED = 0x007E


#: Experience at which each level begins, for a warrior. Read from
#: ``_Template_XPLevels`` in the client's own database, and confirmed twice on the
#: wire: a real award carried (17, 1, 0, 100) and another (115, 2, 100, 440), which
#: are exactly "level 1 runs 0 to 100" and "level 2 runs 100 to 440".
#:
#: The four classes share this curve; only their hit points differ.
LEVEL_EXPERIENCE = (
    0, 100, 440, 1000, 2000, 3300, 5200, 7700, 10800, 14700,
    19600, 25900, 33800, 43700, 55800, 70700, 89000, 112000, 140000, 172000,
    212000, 261000, 319000, 389000, 475000, 579000, 698000, 830000, 982000, 1150000,
)

#: A warrior's hit points at each level, from the same table. Equipment adds to it:
#: the captured level 1 warrior read 235 of 236 where the table says 225.
LEVEL_HIT_POINTS = (
    225, 333, 442, 550, 658, 767, 875, 983, 1092, 1200,
    1500, 1800, 2100, 2400, 2700, 3000, 3300, 3600, 3900, 4200,
    4770, 5340, 5910, 6480, 7050, 7620, 8190, 8760, 9330, 9900,
)


#: A warrior's own damage at each level, from the same table. Fifteen at level one,
#: not the four this server used to invent.
#:
#: Items add to it, and how much cannot be read from a template: MinDamage,
#: MaxDamage and Armor are all empty there, with ItemLevelScalingType set to
#: ToContextLevel. An item's numbers are rolled per instance and scaled to a level,
#: which is also why a replayed item record shows the rarity and level it was
#: recorded with rather than the ones its blueprint suggests.
LEVEL_DAMAGE = (
    15, 17, 18, 20, 22, 23, 25, 27, 28, 30,
    33, 35, 38, 40, 43, 45, 48, 50, 53, 55,
    71, 87, 103, 119, 135, 151, 167, 183, 199, 215,
)


def damage_at(level: int) -> int:
    """What a character of *level* hits for, before equipment."""
    return LEVEL_DAMAGE[min(max(level, 1), len(LEVEL_DAMAGE)) - 1]


def hit_points_at(level: int) -> int:
    """What a character of *level* has, before equipment."""
    return LEVEL_HIT_POINTS[min(max(level, 1), len(LEVEL_HIT_POINTS)) - 1]


def level_for(experience: int) -> int:
    """The level *experience* points buy, one-based."""
    level = 1
    for index, threshold in enumerate(LEVEL_EXPERIENCE, start=1):
        if experience >= threshold:
            level = index
    return level


def level_bounds(level: int) -> tuple[int, int]:
    """Where *level* starts, and where the next one does."""
    floor = LEVEL_EXPERIENCE[min(level, len(LEVEL_EXPERIENCE)) - 1]
    ceiling = (
        LEVEL_EXPERIENCE[level]
        if level < len(LEVEL_EXPERIENCE)
        else LEVEL_EXPERIENCE[-1]
    )
    return floor, ceiling


def encode_xp_changed(
    total: int, actor: int, level: int = 1, levelled: bool = False
) -> bytes:
    """0x007D: the player's experience, and the bar it fills.

    Four 32-bit fields and a bit, then the actor. What they are is settled by two
    real messages read at the bit level, and then confirmed against a table in the
    client's database that neither of them came from:

        (17,  1, 0,   100)   17 points, level 1, level 1 starts at 0, level 2 at 100
        (115, 2, 100, 440)   115 points, level 2, and level 3 starts at 440

    ``_Template_XPLevels`` gives 0, 100, 440, 1000 for the first four levels, which
    is exactly what the pairs say. So the fields are: total experience, current
    level, the level's own floor, and the next level's floor.

    This was wrong here in a way the client showed plainly. The award was sent in
    the first field, so the total never moved and only the first kill of a session
    displayed anything; and the level and both thresholds were hard-coded to level
    1, so the bar's scale never changed either.

    The trailing bit is 1 in the sample that arrived with a level-up and 0 in the
    other, so it is written as "the level changed" — a reading two samples support
    and neither proves.
    """
    floor, ceiling = level_bounds(level)
    writer = BitWriter()
    writer.write_uint(MESSAGE_ID, 8)
    _open(writer, XP_CHANGED)
    for value in (total, level, floor, ceiling):
        writer.write_uint(value & 0xFFFFFFFF, 32)
    writer.write_bool(levelled)
    _close(writer, actor)
    return writer.to_bytes()


def encode_player_level(level: int, actor: int) -> bytes:
    """0x007C: tell the client its character is now *level*.

    Read off the one real example in a capture, sent when the character went from one
    to two: four 32-bit fields reading 2, 0, 0, 0 and then sixteen bits, addressed to
    the player. The first is taken as the new level; the rest are reproduced as
    observed.
    """
    writer = BitWriter()
    writer.write_uint(MESSAGE_ID, 8)
    _open(writer, PLAYER_LEVEL_UPDATE)
    for value in (level, 0, 0, 0):
        writer.write_uint(value & 0xFFFFFFFF, 32)
    writer.write_uint(0, 16)
    _close(writer, actor)
    return writer.to_bytes()


#: Offsets in the body of every skill command the client sends. The first two bytes
#: are zero, then the skill's wire index, then a float32, then a tick.
SKILL_USE_SKILL_OFFSET = 2
SKILL_USE_HEADING_OFFSET = 4
SKILL_USE_TICK_OFFSET = 8
SKILL_USE_MINIMUM = 12

#: Half a turn, in the 256ths the wire uses for a heading.
HEADING_HALF_TURN = 128


@dataclass(frozen=True)
class SkillUse:
    """A skill command's leading fields, which every one of them shares.

    Six command classes carry a skill the player has just used — SkillCommand,
    TargetSkillCommand, BulletSkillCommand, TargetPointBulletSkillCommand,
    ShiftedSkillCommand and TargetBulletSkillCommand — and all six begin the same
    way. What follows differs and is not decoded here.
    """

    #: The skill's wire index: the row before its row in the client's skill table.
    wire: int
    #: Where the player is aiming, in 256ths of a turn clockwise from +y — the same
    #: units :mod:`dsor.gameplay` uses for a movement record's heading.
    heading: int
    #: The client's own game tick when the skill started.
    tick: int


def decode_skill_use(body: bytes) -> SkillUse | None:
    """Read the fields common to every skill command, or None if *body* is too short.

    The float at offset 4 is the aim, in radians, and it is the client saying exactly
    where the player is pointing — which is better than anything this server can infer.
    A movement record's facing lags: it is the last one the client happened to send,
    and a player who turns and swings in the same breath has moved on. Across 277 real
    skill commands the two agree to within eight units only 77% of the time, and the
    disagreements are the turns.

    Measured, not assumed. All 277 samples fall inside [-pi, +pi] — spanning -3.1315
    to +3.0803, so right to the bounds — across 198 distinct values. Fitting the pair
    (sign, offset) against the facing byte of the movement record immediately
    preceding each command gives sign +1 and offset 128, with a median error of one
    unit out of 256.

    That offset of half a turn is not arbitrary, and it confirms something already
    known from the other direction: this server's own outbound skill command carries
    the vector from the *target* to the attacker, the reverse of the direction the
    blow travels, which was measured to 0.3 degrees against two real samples. The
    client's field is the same quantity, so half a turn converts one to the other.
    """
    if len(body) < SKILL_USE_MINIMUM:
        return None
    radians = struct.unpack_from("<f", body, SKILL_USE_HEADING_OFFSET)[0]
    if not -math.pi - 0.01 <= radians <= math.pi + 0.01:
        # Not a heading. Refuse rather than aim a swing with a number that is
        # something else; the caller falls back to the movement record's facing.
        return None
    units = round(radians / (2.0 * math.pi) * HEADING_UNITS) + HEADING_HALF_TURN
    return SkillUse(
        wire=int.from_bytes(
            body[SKILL_USE_SKILL_OFFSET : SKILL_USE_SKILL_OFFSET + 2], "little"
        ),
        heading=units % HEADING_UNITS,
        tick=int.from_bytes(
            body[SKILL_USE_TICK_OFFSET : SKILL_USE_TICK_OFFSET + 4], "little"
        ),
    )
