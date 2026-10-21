"""Typed battle command codecs recovered from the retail client.

The local server only imports editable command values from this module.  The
capture fixtures in the tests are evidence for field order and bit widths;
captured packet bodies are never used by the runtime.
"""

from __future__ import annotations

from dso_server.protocol.command_ids import command_id
from dataclasses import dataclass
import os
import struct

from dso_server.protocol.character_selection import BitReader, BitWriter
from dso_server.protocol.gameplay_protocol import (
    MOVE_COMMAND,
    STATUS_EFFECT_COMMAND,
    Move,
    MoveServerCommand,
    StatusEffectServerCommand,
    _read_status_effect_body,
    _write_status_effect_body,
)


SKILL_COMMAND = command_id("SkillCommand", 0x0046)
TARGET_SKILL_COMMAND = command_id("TargetSkillCommand", 0x0047)
BULLET_SKILL_COMMAND = command_id("BulletSkillCommand", 0x0048)
TARGET_POINT_BULLET_SKILL_COMMAND = command_id("TargetPointBulletSkillCommand", 0x0049)
SHIFTED_SKILL_COMMAND = command_id("ShiftedSkillCommand", 0x004A)
TARGET_BULLET_SKILL_COMMAND = command_id("TargetBulletSkillCommand", 0x004B)
SUSTAINED_SKILL_COMMAND = command_id("SustainedSkillCommand", 0x004C)
HIT_COMMAND = command_id("Commands::HitCommand", 0x006B)
KILL_COMMAND = command_id("Commands::KillCommand", 0x006C)
# ``SkillCommand``-derived commands can carry a generated inheritance marker
# between the base cast fields and the derived body.  The recovered retail
# Machine Gun sample has it; this client does not.  Neither
# ``SkillCommand::Decode`` nor ``::Encode`` touches such a bit, and every skill
# command the client sends decodes with the marker absent.  Emitting it anyway
# shifts the repeated ``SustainedSkill`` header - including the skill index the
# client hands to ``SkillManager::GetSkillInfo`` - one bit to the right, which
# trips that manager's range assertion.  Synthesized commands therefore omit
# it; captured payloads that do carry it still round-trip.
SKILL_COMMAND_DERIVED_FLAG: bool | None = None

# On by default: the release261_192 client this server targets (the xm1rth
# fork's) always reads the skill inheritance marker and the Hit tag-boundary
# bit; without them it logs "invalid command ending in multi command 71/107" and
# the misread actor ids and positions end in a matrix44 assertion.
# DSO_SKILL_HIT_MARKER_BITS=0 writes both only when a value carries them, the
# layout of dro_client64-patch5 and the DSOR client (crates/proto combat.rs).
SKILL_HIT_MARKER_BITS = os.getenv(
    "DSO_SKILL_HIT_MARKER_BITS", "1"
).strip().lower() in {"1", "true", "yes"}

# One owner ``MoveCommand`` followed by one summon ``SustainedSkillCommand``.
# The retail sample measures 1,026 bits with the inheritance marker.
SUSTAINED_SUMMON_BATCH_BITS = (
    1026 if SKILL_HIT_MARKER_BITS else 1025
)


@dataclass(frozen=True)
class SkillCast:
    """Client request for a user-centred melee/radius/cone skill."""

    skill_level: int
    skill_index: int
    aim_angle: float
    server_tick: int
    cast_id: int
    # Current retail sessions insert one generated inheritance marker between
    # the SkillCommand base and the derived command body.  Local/legacy
    # sessions omit it. None means absent; a boolean preserves the wire bit.
    derived_flag: bool | None = None


@dataclass(frozen=True)
class TargetSkillCast:
    """Client request for an actor-targeted non-projectile skill."""

    skill_level: int
    skill_index: int
    aim_angle: float
    server_tick: int
    cast_id: int
    target_actor_id: int
    derived_flag: bool | None = None


@dataclass(frozen=True)
class BulletSkill:
    """Client request serialized by ``BulletSkillCommand::Serialize``.

    ``skill_index`` is the zero-based SkillManager index, not the database
    rowid and not a quickbar slot.  ``direction`` is the four-float aiming
    vector used by the client skill implementation.
    """

    skill_level: int
    skill_index: int
    aim_angle: float
    server_tick: int
    cast_id: int
    direction: tuple[float, float, float, float]
    variant: int
    derived_flag: bool | None = None


@dataclass(frozen=True)
class BulletSkillServer:
    """Authoritative remote bullet playback fields.

    Native ``BulletSkillCommand::Serialize`` writes the base command's
    ``+0xc8`` cast value after ``server_tick``.  Its target field at ``+0x2c``
    is *not* part of command 0x0048 and therefore remains ``InvalidActorId``.
    Actor-targeted projectiles use :class:`TargetBulletSkill`/command 0x004b.
    """

    skill_level: int
    skill_index: int
    aim_angle: float
    server_tick: int
    cast_id: int
    direction: tuple[float, float, float, float]
    variant: int
    derived_flag: bool | None = None


@dataclass(frozen=True)
class TargetBulletSkill:
    """Actor-targeted bullet command recovered from the native serializer.

    ``TargetBulletSkillCommand`` derives from ``BulletSkillCommand`` and adds
    the target actor id plus arrays of additional actor ids and target
    positions.  The native decoder permits an empty additional-actor array,
    but requires at least one target position.
    """

    skill_level: int
    skill_index: int
    aim_angle: float
    server_tick: int
    cast_id: int
    direction: tuple[float, float, float, float]
    variant: int
    target_actor_id: int
    additional_target_actor_ids: tuple[int, ...] = ()
    target_positions: tuple[tuple[float, float, float], ...] = ()
    derived_flag: bool | None = None


@dataclass(frozen=True)
class TargetPointBulletSkill:
    """Projectile whose endpoint is a world-space point rather than an actor."""

    skill_level: int
    skill_index: int
    aim_angle: float
    server_tick: int
    cast_id: int
    direction: tuple[float, float, float, float]
    variant: int
    target_position: tuple[float, float, float]
    additional_target_positions: tuple[
        tuple[float, float, float], ...
    ] = ()
    derived_flag: bool | None = None


@dataclass(frozen=True)
class ShiftedSkill:
    """Ground-targeted cast serialized by command ``0x004a``."""

    skill_level: int
    skill_index: int
    aim_angle: float
    server_tick: int
    cast_id: int
    variant: int
    target_position: tuple[float, float, float]
    additional_target_positions: tuple[
        tuple[float, float, float], ...
    ] = ()
    derived_flag: bool | None = None


@dataclass(frozen=True)
class SkillExecutionContext:
    """Server timing/placement trailer consumed by skill playback."""

    impact_tick: int
    hit_frame: int
    motion_unblock_frame: int
    skill_unblock_frame: int
    reserved: int
    position: tuple[float, float, float, float]
    predicted: bool


@dataclass(frozen=True)
class TargetSkill:
    """Authoritative targeted cast which starts a monster animation."""

    skill_level: int
    skill_index: int
    aim_angle: float
    server_tick: int
    cast_id: int
    target_actor_id: int
    context: SkillExecutionContext
    derived_flag: bool | None = None


@dataclass(frozen=True)
class TargetSkillServerCommand:
    value: TargetSkill
    actor_id: int


@dataclass(frozen=True)
class BulletSkillServerCommand:
    value: BulletSkillServer
    actor_id: int
    context: SkillExecutionContext


@dataclass(frozen=True)
class TargetBulletSkillServerCommand:
    value: TargetBulletSkill
    actor_id: int
    context: SkillExecutionContext


@dataclass(frozen=True)
class SkillServerCommand:
    value: SkillCast
    actor_id: int
    context: SkillExecutionContext


@dataclass(frozen=True)
class TargetPointBulletSkillServerCommand:
    value: TargetPointBulletSkill
    actor_id: int
    context: SkillExecutionContext
    # Native TargetPointBulletSkillCommand::EncodeAdditionalServerData writes
    # one extra uint32 after BulletSkillCommand's actor/context block.
    hit_range: int = 0


@dataclass(frozen=True)
class ShiftedSkillServerCommand:
    value: ShiftedSkill
    actor_id: int
    context: SkillExecutionContext
    pre_impact_delay: int = 0


@dataclass(frozen=True)
class SustainedSkillServerCommand:
    """Authoritative sustained cone/beam command recovered from retail.

    The native command contains the ordinary cast header followed by a
    repeated loop header and two timing/placement blocks. The second block
    intentionally has no trailing predicted bit.
    """

    value: SkillCast
    actor_id: int
    context: SkillExecutionContext
    loop_start_frame: int
    hit_range: int


@dataclass(frozen=True)
class MonsterAttackBatch:
    """Retail movement snapshots followed by one or more monster casts."""

    movements: tuple[MoveServerCommand, ...]
    attacks: tuple[
        TargetSkillServerCommand
        | BulletSkillServerCommand
        | TargetPointBulletSkillServerCommand
        | ShiftedSkillServerCommand
        | SkillServerCommand
        | TargetBulletSkillServerCommand
        | SustainedSkillServerCommand,
        ...,
    ]


def _read_client_header(
    payload: bytes, expected_command: int, bit_length: int | None
) -> BitReader:
    reader = BitReader(
        payload, len(payload) * 8 if bit_length is None else bit_length
    )
    if reader.read_uint8() != 0x8B:
        raise ValueError("skill payload is not a client command")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != expected_command:
        raise ValueError(
            f"expected skill command 0x{expected_command:04x}, "
            f"got 0x{command_id:04x}"
        )
    return reader


def _read_skill_cast_body(reader: BitReader) -> tuple[int, int, float, int, int]:
    return (
        int.from_bytes(reader.read_bytes(2), "little"),
        int.from_bytes(reader.read_bytes(2), "little"),
        reader.read_float(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
    )


def _read_skill_base(reader: BitReader) -> tuple[int, int, float, int, int, bool]:
    """Read the shared skill header plus the inheritance marker emitted by
    :func:`_write_skill_base`."""

    return (
        *_read_skill_cast_body(reader),
        reader.read_bool() if SKILL_HIT_MARKER_BITS else None,
    )


def _read_optional_derived_flag(reader: BitReader) -> bool | None:
    """Consume the current retail inheritance marker when it is present.

    Every concrete derived body is otherwise byte-sized.  The generated
    retail serializer therefore leaves one bit modulo eight after the common
    SkillCommand body, while the local/legacy layout remains byte-aligned.
    """

    return reader.read_bool() if reader.remaining % 8 == 1 else None


def decode_skill(
    payload: bytes, bit_length: int | None = None
) -> SkillCast:
    reader = _read_client_header(payload, SKILL_COMMAND, bit_length)
    base = _read_skill_cast_body(reader)
    value = SkillCast(*base, _read_optional_derived_flag(reader))
    if reader.remaining:
        raise ValueError("SkillCommand has unread bits")
    return value


def decode_target_skill(
    payload: bytes, bit_length: int | None = None
) -> TargetSkillCast:
    reader = _read_client_header(payload, TARGET_SKILL_COMMAND, bit_length)
    base = _read_skill_cast_body(reader)
    derived_flag = _read_optional_derived_flag(reader)
    value = TargetSkillCast(
        *base,
        reader.read_uint32_le(),
        derived_flag,
    )
    # Zero is the client's no-target sentinel. Preserve it so gameplay can
    # ignore the cast without classifying a complete packet as malformed.
    if reader.remaining:
        raise ValueError("TargetSkillCommand has unread bits")
    return value


def decode_shifted_skill(
    payload: bytes, bit_length: int | None = None
) -> ShiftedSkill:
    reader = _read_client_header(payload, SHIFTED_SKILL_COMMAND, bit_length)
    base = _read_skill_cast_body(reader)
    derived_flag = _read_optional_derived_flag(reader)
    position_count = reader.read_uint8()
    if position_count == 0:
        raise ValueError("ShiftedSkill requires at least one target position")
    positions = tuple(
        tuple(reader.read_float() for _ in range(3))
        for _ in range(position_count)
    )
    value = ShiftedSkill(
        *base,
        position_count,
        positions[0],
        positions[1:],
        derived_flag,
    )
    if reader.remaining:
        raise ValueError("ShiftedSkillCommand has unread bits")
    return value


def decode_target_point_bullet_skill(
    payload: bytes, bit_length: int | None = None
) -> TargetPointBulletSkill:
    reader = _read_client_header(
        payload, TARGET_POINT_BULLET_SKILL_COMMAND, bit_length
    )
    base = _read_skill_cast_body(reader)
    derived_flag = _read_optional_derived_flag(reader)
    direction = tuple(reader.read_float() for _ in range(4))
    variant = reader.read_uint8()
    # An early reconstructed fixture omitted the generated array count.
    # Accept it read-only, while every native current command uses the count.
    if reader.remaining == 96:
        positions = (tuple(reader.read_float() for _ in range(3)),)
    else:
        position_count = reader.read_uint8()
        if position_count == 0:
            raise ValueError(
                "TargetPointBulletSkill requires a target position"
            )
        positions = tuple(
            tuple(reader.read_float() for _ in range(3))
            for _ in range(position_count)
        )
    value = TargetPointBulletSkill(
        *base,
        direction,
        variant,
        positions[0],
        positions[1:],
        derived_flag,
    )
    if reader.remaining:
        raise ValueError("TargetPointBulletSkillCommand has unread bits")
    return value


def decode_bullet_skill(
    payload: bytes, bit_length: int | None = None
) -> BulletSkill:
    reader = BitReader(
        payload, len(payload) * 8 if bit_length is None else bit_length
    )
    if reader.read_uint8() != 0x8B:
        raise ValueError("BulletSkill is not a client command")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != BULLET_SKILL_COMMAND:
        raise ValueError(
            f"expected BulletSkill 0x{BULLET_SKILL_COMMAND:04x}, "
            f"got 0x{command_id:04x}"
        )
    base = _read_skill_cast_body(reader)
    derived_flag = _read_optional_derived_flag(reader)
    value = BulletSkill(
        *base,
        direction=tuple(reader.read_float() for _ in range(4)),
        variant=reader.read_uint8(),
        derived_flag=derived_flag,
    )
    if reader.remaining:
        raise ValueError("BulletSkill has unread bits")
    return value


def decode_target_bullet_skill(
    payload: bytes, bit_length: int | None = None
) -> TargetBulletSkill:
    """Decode the client-side 0x004b actor-targeted projectile request."""

    reader = BitReader(
        payload, len(payload) * 8 if bit_length is None else bit_length
    )
    if reader.read_uint8() != 0x8B:
        raise ValueError("TargetBulletSkill is not a client command")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != TARGET_BULLET_SKILL_COMMAND:
        raise ValueError(
            f"expected TargetBulletSkill "
            f"0x{TARGET_BULLET_SKILL_COMMAND:04x}, got 0x{command_id:04x}"
        )
    (
        skill_level,
        skill_index,
        aim_angle,
        server_tick,
        cast_id,
    ) = _read_skill_cast_body(reader)
    derived_flag = _read_optional_derived_flag(reader)
    direction = tuple(reader.read_float() for _ in range(4))
    variant = reader.read_uint8()
    target_actor_id = reader.read_uint32_le()
    additional = tuple(
        reader.read_uint32_le()
        for _ in range(reader.read_uint8())
    )
    positions = tuple(
        tuple(reader.read_float() for _ in range(3))
        for _ in range(reader.read_uint8())
    )
    if target_actor_id == 0:
        raise ValueError(
            "TargetBulletSkill has an invalid primary target actor"
        )
    if not positions:
        raise ValueError(
            "TargetBulletSkill requires at least one target position"
        )
    if reader.remaining:
        raise ValueError("TargetBulletSkill has unread bits")
    return TargetBulletSkill(
        skill_level,
        skill_index,
        aim_angle,
        server_tick,
        cast_id,
        direction,
        variant,
        target_actor_id,
        additional,
        positions,
        derived_flag,
    )


def _write_skill_base(writer: BitWriter, value) -> None:
    """Write the five shared skill fields plus retail's inheritance marker.

    ``SkillCommand`` and every derived server command start with the same
    16/16/32/32/32-bit header.  Release261's decoders then consume one
    inheritance marker before the derived fields, so omitting it shifts the
    target, actor and execution context by a bit and the client rejects the
    command (``DecodeCommand() invalid command ending in multi command 71!``,
    followed by ``No game actor found for attacker id '<id> * 2>'``).
    """

    # Historical API name: skill_level is the native uint16 SkillCounter.
    # ClientSkillManager::HandleSkillCommand compares command +0x20 against
    # actor +0x72 and ignores nonzero values that do not increase. Zero opts
    # autonomous server casts out of client prediction reconciliation.
    # Never populate this field from a monster's authored Skills entry level.
    _write_uint16_le(writer, value.skill_level)
    _write_uint16_le(writer, value.skill_index)
    writer.write_float(value.aim_angle)
    writer.write_uint32_le(value.server_tick)
    writer.write_uint32_le(value.cast_id)
    if value.derived_flag is not None:
        writer.write_bool(value.derived_flag)
    elif SKILL_HIT_MARKER_BITS:
        writer.write_bool(False)


def _write_target_skill_body(writer: BitWriter, value: TargetSkill) -> None:
    _write_skill_base(writer, value)
    writer.write_uint32_le(value.target_actor_id)


def _write_skill_context(
    writer: BitWriter, value: SkillExecutionContext
) -> None:
    writer.write_uint32_le(value.impact_tick)
    writer.write_uint32_le(value.hit_frame)
    writer.write_uint32_le(value.motion_unblock_frame)
    writer.write_uint32_le(value.skill_unblock_frame)
    writer.write_uint32_le(value.reserved)
    for component in value.position:
        writer.write_float(component)
    writer.write_bool(value.predicted)


def _read_target_skill_body(reader: BitReader) -> tuple[
    int, int, float, int, int, bool, int
]:
    return (
        *_read_skill_base(reader),
        reader.read_uint32_le(),
    )


def _read_skill_context(reader: BitReader) -> SkillExecutionContext:
    integers = tuple(reader.read_uint32_le() for _ in range(5))
    position = tuple(reader.read_float() for _ in range(4))
    return SkillExecutionContext(*integers, position, reader.read_bool())


def encode_monster_attack_batch(
    value: MonsterAttackBatch,
) -> tuple[bytes, int]:
    """Encode a live Move -> TargetSkill/BulletSkill attack-start sequence."""

    if not value.attacks:
        raise ValueError("MonsterAttackBatch requires at least one attack")
    writer = BitWriter()
    writer.write_uint8(0x85)
    command_count = 0
    for movement in value.movements:
        if command_count:
            writer.write_uint8(0xFF)
        writer.write_bytes(MOVE_COMMAND.to_bytes(2, "little"))
        writer.write_bytes(movement.value.encode_body())
        writer.write_uint32_le(movement.actor_id)
        command_count += 1
    for attack in value.attacks:
        if command_count:
            writer.write_uint8(0xFF)
        if isinstance(attack, SkillServerCommand):
            writer.write_bytes(SKILL_COMMAND.to_bytes(2, "little"))
            value = attack.value
            _write_skill_base(writer, value)
            writer.write_uint32_le(attack.actor_id)
            _write_skill_context(writer, attack.context)
        elif isinstance(attack, TargetSkillServerCommand):
            writer.write_bytes(TARGET_SKILL_COMMAND.to_bytes(2, "little"))
            _write_target_skill_body(writer, attack.value)
            writer.write_uint32_le(attack.actor_id)
            _write_skill_context(writer, attack.value.context)
        elif isinstance(attack, BulletSkillServerCommand):
            writer.write_bytes(BULLET_SKILL_COMMAND.to_bytes(2, "little"))
            value = attack.value
            _write_skill_base(writer, value)
            for component in value.direction:
                writer.write_float(component)
            writer.write_uint8(value.variant)
            writer.write_uint32_le(attack.actor_id)
            _write_skill_context(writer, attack.context)
        elif isinstance(attack, TargetPointBulletSkillServerCommand):
            writer.write_bytes(
                TARGET_POINT_BULLET_SKILL_COMMAND.to_bytes(2, "little")
            )
            value = attack.value
            _write_skill_base(writer, value)
            for component in value.direction:
                writer.write_float(component)
            writer.write_uint8(value.variant)
            positions = (
                value.target_position,
                *value.additional_target_positions,
            )
            if len(positions) > 0xFF:
                raise ValueError(
                    "TargetPointBulletSkill has more than 255 positions"
                )
            writer.write_uint8(len(positions))
            for position in positions:
                for component in position:
                    writer.write_float(component)
            writer.write_uint32_le(attack.actor_id)
            _write_skill_context(writer, attack.context)
            writer.write_uint32_le(max(0, int(attack.hit_range)))
        elif isinstance(attack, ShiftedSkillServerCommand):
            writer.write_bytes(SHIFTED_SKILL_COMMAND.to_bytes(2, "little"))
            value = attack.value
            _write_skill_base(writer, value)
            positions = (
                value.target_position,
                *value.additional_target_positions,
            )
            if not 1 <= len(positions) <= 0xFF:
                raise ValueError(
                    "ShiftedSkill requires 1 to 255 target positions"
                )
            writer.write_uint8(len(positions))
            for position in positions:
                for component in position:
                    writer.write_float(component)
            writer.write_uint32_le(attack.actor_id)
            _write_skill_context(writer, attack.context)
            # ShiftedSkillCommand owns one server-only delay after the common
            # execution context. Retail satisfies:
            #   impact_tick - server_tick == hit_frame + pre_impact_delay
            # Omitting this uint32 makes the client consume the 0xff batch
            # terminator as additional data and reject command 0x004a.
            writer.write_uint32_le(max(0, int(attack.pre_impact_delay)))
        elif isinstance(attack, SustainedSkillServerCommand):
            writer.write_bytes(SUSTAINED_SKILL_COMMAND.to_bytes(2, "little"))
            value = attack.value
            _write_skill_base(writer, value)
            # Repeated loop header owned by SustainedSkillCommand.
            _write_uint16_le(writer, value.skill_index)
            writer.write_float(value.aim_angle)
            writer.write_uint32_le(value.server_tick)
            writer.write_uint32_le(attack.actor_id)
            context = attack.context
            writer.write_uint32_le(context.impact_tick)
            writer.write_uint32_le(attack.loop_start_frame)
            writer.write_uint32_le(context.motion_unblock_frame)
            writer.write_uint32_le(context.skill_unblock_frame)
            writer.write_uint32_le(context.reserved)
            for component in context.position:
                writer.write_float(component)
            writer.write_bool(context.predicted)
            writer.write_uint32_le(context.impact_tick)
            writer.write_uint32_le(attack.hit_range)
            writer.write_uint32_le(context.hit_frame)
            writer.write_uint32_le(context.motion_unblock_frame)
            writer.write_uint32_le(context.skill_unblock_frame)
            for component in context.position:
                writer.write_float(component)
        elif isinstance(attack, TargetBulletSkillServerCommand):
            writer.write_bytes(
                TARGET_BULLET_SKILL_COMMAND.to_bytes(2, "little")
            )
            value = attack.value
            if not 0 < int(value.target_actor_id) <= 0xFFFFFFFF:
                raise ValueError(
                    "TargetBulletSkill requires a valid primary target actor"
                )
            if len(value.additional_target_actor_ids) > 0xFF:
                raise ValueError(
                    "TargetBulletSkill has more than 255 additional targets"
                )
            if not 1 <= len(value.target_positions) <= 0xFF:
                raise ValueError(
                    "TargetBulletSkill requires 1 to 255 target positions"
                )
            _write_skill_base(writer, value)
            for component in value.direction:
                writer.write_float(component)
            writer.write_uint8(value.variant)
            writer.write_uint32_le(value.target_actor_id)
            writer.write_uint8(len(value.additional_target_actor_ids))
            for actor_id in value.additional_target_actor_ids:
                writer.write_uint32_le(actor_id)
            writer.write_uint8(len(value.target_positions))
            for position in value.target_positions:
                if len(position) != 3:
                    raise ValueError(
                        "TargetBulletSkill target positions require 3 floats"
                    )
                for component in position:
                    writer.write_float(component)
            writer.write_uint32_le(attack.actor_id)
            _write_skill_context(writer, attack.context)
        else:
            raise TypeError(
                f"unsupported monster attack type {type(attack).__name__}"
            )
        command_count += 1
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_monster_attack_batch(
    payload: bytes, bit_length: int
) -> MonsterAttackBatch:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("MonsterAttackBatch is not a server command batch")
    movements = []
    attacks = []
    while True:
        command_id = int.from_bytes(reader.read_bytes(2), "little")
        if command_id == MOVE_COMMAND:
            move = Move(*struct.unpack("<hhhBBB I H", reader.read_bytes(15)))
            movements.append(MoveServerCommand(move, reader.read_uint32_le()))
        elif command_id == SKILL_COMMAND:
            value = SkillCast(*_read_skill_base(reader))
            actor_id = reader.read_uint32_le()
            attacks.append(
                SkillServerCommand(
                    value, actor_id, _read_skill_context(reader)
                )
            )
        elif command_id == TARGET_SKILL_COMMAND:
            level, index, angle, tick, cast_id, flag, target = (
                _read_target_skill_body(reader)
            )
            actor_id = reader.read_uint32_le()
            context = _read_skill_context(reader)
            attacks.append(
                TargetSkillServerCommand(
                    TargetSkill(
                        level, index, angle, tick, cast_id, target,
                        context, flag,
                    ),
                    actor_id,
                )
            )
        elif command_id == BULLET_SKILL_COMMAND:
            level, index, angle, tick, cast_id, flag = _read_skill_base(reader)
            value = BulletSkillServer(
                skill_level=level,
                skill_index=index,
                aim_angle=angle,
                server_tick=tick,
                cast_id=cast_id,
                direction=tuple(reader.read_float() for _ in range(4)),
                variant=reader.read_uint8(),
                derived_flag=flag,
            )
            actor_id = reader.read_uint32_le()
            attacks.append(
                BulletSkillServerCommand(
                    value, actor_id, _read_skill_context(reader)
                )
            )
        elif command_id == TARGET_POINT_BULLET_SKILL_COMMAND:
            level, index, angle, tick, cast_id, flag = _read_skill_base(reader)
            direction = tuple(reader.read_float() for _ in range(4))
            variant = reader.read_uint8()
            target_position_count = reader.read_uint8()
            if target_position_count == 0:
                raise ValueError(
                    "TargetPointBulletSkill requires a target position"
                )
            target_positions = tuple(
                tuple(reader.read_float() for _ in range(3))
                for _ in range(target_position_count)
            )
            value = TargetPointBulletSkill(
                level,
                index,
                angle,
                tick,
                cast_id,
                direction,
                variant,
                target_positions[0],
                target_positions[1:],
                flag,
            )
            actor_id = reader.read_uint32_le()
            context = _read_skill_context(reader)
            attacks.append(
                TargetPointBulletSkillServerCommand(
                    value,
                    actor_id,
                    context,
                    reader.read_uint32_le(),
                )
            )
        elif command_id == SHIFTED_SKILL_COMMAND:
            level, index, angle, tick, cast_id, flag = _read_skill_base(reader)
            position_count = reader.read_uint8()
            if position_count == 0:
                raise ValueError(
                    "ShiftedSkill requires at least one target position"
                )
            target_positions = tuple(
                tuple(reader.read_float() for _ in range(3))
                for _ in range(position_count)
            )
            value = ShiftedSkill(
                level,
                index,
                angle,
                tick,
                cast_id,
                position_count,
                target_positions[0],
                target_positions[1:],
                flag,
            )
            actor_id = reader.read_uint32_le()
            context = _read_skill_context(reader)
            attacks.append(
                ShiftedSkillServerCommand(
                    value,
                    actor_id,
                    context,
                    reader.read_uint32_le(),
                )
            )
        elif command_id == SUSTAINED_SKILL_COMMAND:
            (
                skill_level, skill_index, aim_angle, server_tick, cast_id,
                derived_flag,
            ) = _read_skill_base(reader)
            # Repeated loop header owned by SustainedSkillCommand.
            loop_skill_index = int.from_bytes(
                reader.read_bytes(2), "little"
            )
            loop_aim_angle = reader.read_float()
            loop_server_tick = reader.read_uint32_le()
            actor_id = reader.read_uint32_le()
            first_integers = tuple(
                reader.read_uint32_le() for _ in range(5)
            )
            position = tuple(reader.read_float() for _ in range(4))
            predicted = reader.read_bool()
            second_integers = tuple(
                reader.read_uint32_le() for _ in range(5)
            )
            second_position = tuple(
                reader.read_float() for _ in range(4)
            )
            if (
                loop_skill_index != skill_index
                or loop_server_tick != server_tick
                or abs(loop_aim_angle - aim_angle) > 1e-5
                or second_integers[0] != first_integers[0]
                or second_position != position
            ):
                raise ValueError(
                    "SustainedSkill repeated loop fields disagree"
                )
            context = SkillExecutionContext(
                impact_tick=first_integers[0],
                hit_frame=second_integers[2],
                motion_unblock_frame=first_integers[2],
                skill_unblock_frame=first_integers[3],
                reserved=first_integers[4],
                position=position,
                predicted=predicted,
            )
            attacks.append(
                SustainedSkillServerCommand(
                    SkillCast(
                        skill_level,
                        skill_index,
                        aim_angle,
                        server_tick,
                        cast_id,
                        derived_flag,
                    ),
                    actor_id,
                    context,
                    first_integers[1],
                    second_integers[1],
                )
            )
        elif command_id == TARGET_BULLET_SKILL_COMMAND:
            (
                skill_level, skill_index, aim_angle, server_tick, cast_id,
                derived_flag,
            ) = _read_skill_base(reader)
            direction = tuple(reader.read_float() for _ in range(4))
            variant = reader.read_uint8()
            target_actor_id = reader.read_uint32_le()
            if target_actor_id == 0:
                raise ValueError(
                    "TargetBulletSkill has an invalid primary target actor"
                )
            additional_target_actor_ids = tuple(
                reader.read_uint32_le()
                for _ in range(reader.read_uint8())
            )
            target_position_count = reader.read_uint8()
            if target_position_count == 0:
                raise ValueError(
                    "TargetBulletSkill requires at least one target position"
                )
            target_positions = tuple(
                tuple(reader.read_float() for _ in range(3))
                for _ in range(target_position_count)
            )
            actor_id = reader.read_uint32_le()
            value = TargetBulletSkill(
                skill_level,
                skill_index,
                aim_angle,
                server_tick,
                cast_id,
                direction,
                variant,
                target_actor_id,
                additional_target_actor_ids,
                target_positions,
                derived_flag,
            )
            attacks.append(
                TargetBulletSkillServerCommand(
                    value, actor_id, _read_skill_context(reader)
                )
            )
        else:
            raise ValueError(
                f"unsupported monster attack command 0x{command_id:04x}"
            )
        if reader.read_uint8() != 0xFF:
            raise ValueError("monster attack command is not followed by 0xff")
        if not reader.remaining:
            break
    if not attacks:
        raise ValueError("MonsterAttackBatch does not contain a skill command")
    return MonsterAttackBatch(tuple(movements), tuple(attacks))


@dataclass(frozen=True)
class Hit:
    """Authoritative damage transition consumed by ``HandleHitCommand``.

    Retail always sends a reserved bit between ``hit_tags`` and the two native
    flags at object offsets ``+0x48`` and ``+0x49``. Omitting it shifts every
    following bit field, including health, damage, actor ids and combat-text
    data, and the client then drops the whole 0x85 batch: release261 reports
    ``DecodeCommand() invalid command ending in multi command 107!`` followed
    by ``Received HitCommand with unknown actor id (...)`` for the
    one-bit-shifted actor id it then reads.
    """

    server_tick: int
    hit_tags: tuple[int, ...]
    field_48: bool
    field_49: bool
    current_health: int
    maximum_health: int
    absorbed_damage: int
    shield_damage: int
    damage: int
    damage_type: int
    attacker_actor_id: int
    source_actor_id: int
    skill_id: int
    impact_strength: float
    field_80: bool
    field_81: bool
    # Client object +0x88 is a Util::String combat-text override. Retail
    # sends values such as ``DamageDonePoison`` here; treating its uint16
    # string length as a count of one-bit reactions happened to preserve an
    # empty value but corrupted every elemental hit.
    combat_text_type: str = ""
    # The reserved retail bit described above.  Retail has always been observed
    # clear; it is kept so captured packets round-trip.
    tag_boundary_bit: bool = False


@dataclass(frozen=True)
class Kill:
    """Monster/player death transition consumed by ``HandleKillCommand``."""

    server_tick: int
    hit_tags: tuple[int, ...]
    animation_time: int
    field_4c: int
    killer_actor_id: int
    position: tuple[float, float, float]
    skill_id: int
    projectile_id: int
    field_78: bool


@dataclass(frozen=True)
class HitServerCommand:
    value: Hit
    actor_id: int


@dataclass(frozen=True)
class KillServerCommand:
    value: Kill
    actor_id: int


@dataclass(frozen=True)
class CombatBatch:
    """One ordered retail combat transaction envelope.

    Retail interleaves authoritative hit results and status-effect lifecycle
    corrections in the same 0x85 application batch. Keeping StatusEffect in
    this typed union preserves the captured Hit -> StatusEffect -> Kill order
    and prevents an independent reliable packet from head-of-line blocking
    the damage result it belongs to.
    """

    commands: tuple[
        HitServerCommand | StatusEffectServerCommand | KillServerCommand, ...
    ]


def _write_uint16_le(writer: BitWriter, value: int) -> None:
    value = int(value)
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"uint16 value out of range: {value}")
    writer.write_bytes(value.to_bytes(2, "little"))


def _write_uint64_le(writer: BitWriter, value: int) -> None:
    value = int(value)
    if not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"uint64 value out of range: {value}")
    writer.write_uint64_le(value)


def _write_hit_body(
        writer: BitWriter, value: Hit, *,
        legacy_hit_tag_boundary_bit: bool | None = None) -> None:
    writer.write_uint32_le(value.server_tick)
    writer.write_uint32_le(len(value.hit_tags))
    for tag in value.hit_tags:
        writer.write_uint8(tag)
    if (SKILL_HIT_MARKER_BITS if legacy_hit_tag_boundary_bit is None
            else legacy_hit_tag_boundary_bit):
        writer.write_bool(value.tag_boundary_bit)
    writer.write_bool(value.field_48)
    writer.write_bool(value.field_49)
    _write_uint64_le(writer, value.current_health)
    _write_uint64_le(writer, value.maximum_health)
    writer.write_uint32_le(value.absorbed_damage)
    writer.write_uint32_le(value.shield_damage)
    writer.write_uint32_le(value.damage)
    writer.write_uint32_le(value.damage_type)
    writer.write_uint32_le(value.attacker_actor_id)
    writer.write_uint32_le(value.source_actor_id)
    writer.write_uint32_le(value.skill_id)
    writer.write_float(value.impact_strength)
    writer.write_bool(value.field_80)
    writer.write_bool(value.field_81)
    writer.write_string(value.combat_text_type)


def _read_hit_body(
        reader: BitReader, *,
        legacy_hit_tag_boundary_bit: bool | None = None) -> Hit:
    server_tick = reader.read_uint32_le()
    tags = tuple(reader.read_uint8() for _ in range(reader.read_uint32_le()))
    tag_boundary_bit = (
        reader.read_bool()
        if (SKILL_HIT_MARKER_BITS if legacy_hit_tag_boundary_bit is None
            else legacy_hit_tag_boundary_bit)
        else False
    )
    field_48 = reader.read_bool()
    field_49 = reader.read_bool()
    current_health = reader.read_uint64_le()
    maximum_health = reader.read_uint64_le()
    integers = tuple(reader.read_uint32_le() for _ in range(7))
    impact_strength = reader.read_float()
    field_80 = reader.read_bool()
    field_81 = reader.read_bool()
    combat_text_type = reader.read_string()
    return Hit(
        server_tick,
        tags,
        field_48,
        field_49,
        current_health,
        maximum_health,
        *integers,
        impact_strength,
        field_80,
        field_81,
        combat_text_type,
        tag_boundary_bit,
    )


def _write_kill_body(writer: BitWriter, value: Kill) -> None:
    writer.write_uint32_le(value.server_tick)
    writer.write_uint32_le(len(value.hit_tags))
    for tag in value.hit_tags:
        writer.write_uint8(tag)
    writer.write_uint32_le(value.animation_time)
    writer.write_uint32_le(value.field_4c)
    writer.write_uint32_le(value.killer_actor_id)
    for component in value.position:
        writer.write_float(component)
    writer.write_uint32_le(value.skill_id)
    writer.write_uint32_le(value.projectile_id)
    writer.write_bool(value.field_78)


def _read_kill_body(reader: BitReader) -> Kill:
    server_tick = reader.read_uint32_le()
    tags = tuple(reader.read_uint8() for _ in range(reader.read_uint32_le()))
    return Kill(
        server_tick,
        tags,
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        tuple(reader.read_float() for _ in range(3)),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_bool(),
    )


def encode_combat_batch(
        value: CombatBatch, *,
        legacy_hit_tag_boundary_bit: bool | None = None) -> tuple[bytes, int]:
    if not value.commands:
        raise ValueError("CombatBatch requires at least one command")
    writer = BitWriter()
    writer.write_uint8(0x85)
    for index, command in enumerate(value.commands):
        if index:
            writer.write_uint8(0xFF)
        if isinstance(command, HitServerCommand):
            _write_uint16_le(writer, HIT_COMMAND)
            _write_hit_body(
                writer,
                command.value,
                legacy_hit_tag_boundary_bit=legacy_hit_tag_boundary_bit,
            )
        elif isinstance(command, StatusEffectServerCommand):
            _write_uint16_le(writer, STATUS_EFFECT_COMMAND)
            _write_status_effect_body(writer, command.value)
        elif isinstance(command, KillServerCommand):
            _write_uint16_le(writer, KILL_COMMAND)
            _write_kill_body(writer, command.value)
        else:
            raise TypeError(f"unsupported combat command {type(command)!r}")
        writer.write_uint32_le(command.actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_combat_batch(
        payload: bytes, bit_length: int, *,
        legacy_hit_tag_boundary_bit: bool | None = None) -> CombatBatch:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("CombatBatch is not a server batch")
    commands: list[
        HitServerCommand | StatusEffectServerCommand | KillServerCommand
    ] = []
    while True:
        command_id = int.from_bytes(reader.read_bytes(2), "little")
        if command_id == HIT_COMMAND:
            command = HitServerCommand(
                _read_hit_body(
                    reader,
                    legacy_hit_tag_boundary_bit=legacy_hit_tag_boundary_bit,
                ), reader.read_uint32_le()
            )
        elif command_id == STATUS_EFFECT_COMMAND:
            command = StatusEffectServerCommand(
                _read_status_effect_body(reader), reader.read_uint32_le()
            )
        elif command_id == KILL_COMMAND:
            command = KillServerCommand(
                _read_kill_body(reader), reader.read_uint32_le()
            )
        else:
            raise ValueError(
                f"unsupported combat command 0x{command_id:04x}"
            )
        commands.append(command)
        if reader.read_uint8() != 0xFF:
            raise ValueError("combat command is not followed by 0xff")
        if not reader.remaining:
            break
    return CombatBatch(tuple(commands))
