"""Typed codecs for the client's ``PVP::`` matchmaking command block.

Command ids 0xC0..0xDA and 0x13C belong to the ``PVP::`` namespace rather than
``Commands::``.  They were missing from ``dso_server/docs/command_ids.json``
until ``dump_command_ids.py`` was widened to every namespace; see
``docs/PVP_MATCHMAKING.md``.

Every layout here was read out of the client's own serializer -- vtable slot 4
(``Encode``) of each command class -- and then checked against the recorded
retail sessions.  A layout is only accepted when decoding a captured message
consumes its bit length exactly, ending on the batch terminator.  The client
function address that defines each layout is named in the dataclass docstring
so the derivation can be re-checked against a future patch.

Wire framing
------------
A server->client application message is ``0x85`` followed by one or more
commands, each ``uint16`` command id (little endian) + body + a ``0xFF``
terminator byte.  Everything is bit-packed: a command whose body ends on a
bool leaves the following id and terminator byte-misaligned, which is normal
and why these codecs are written against ``BitReader``/``BitWriter`` rather
than ``struct``.  A client->server message is ``0x8B`` + id + body with no
terminator.

The command base class contributes nothing: ``Network::Command::Encode`` is a
bare ``ret``.  Fields that look like a shared header (a server tick, an actor
id) are ordinary members that most commands happen to declare.
"""

from __future__ import annotations

from dso_server.protocol.command_ids import command_id
from dataclasses import dataclass, field
from datetime import datetime, timezone
import struct

from dso_server.protocol.character_selection import BitReader, BitWriter


# ---------------------------------------------------------------------------
# Command ids
# ---------------------------------------------------------------------------

PVP_MATCH_OVER_COMMAND = command_id("PVP::PVPMatchOverCommand", 0x00C1)
PVP_MATCH_START_COMMAND = command_id("PVP::PVPMatchStartCommand", 0x00C2)
PVP_MATCH_COUNTDOWN_COMMAND = command_id("PVP::PVPMatchCountdownCommand", 0x00C3)
PVP_MATCH_INFO_COMMAND = command_id("PVP::PVPMatchInfoCommand", 0x00C4)
LEAVE_PVP_COMMAND = command_id("PVP::LeavePVPCommand", 0x00C7)
SET_BATTLEGROUND_ID_COMMAND = command_id("PVP::SetBattlegroundIdCommand", 0x00CB)
PVP_MATCH_DESCRIPTION_COMMAND = command_id("PVP::PVPMatchDescriptionCommand", 0x00CA)
SHOW_PVP_DEATH_DIALOG_COMMAND = command_id("PVP::ShowPVPDeathDialogCommand", 0x00CC)
JOIN_MATCH_QUEUE_COMMAND = command_id("PVP::JoinMatchQueueCommand", 0x00CE)
JOIN_MATCH_QUEUE_RESPONSE_COMMAND = command_id("PVP::JoinMatchQueueResponseCommand", 0x00CF)
JOIN_MATCH_QUEUE_INFO_COMMAND = command_id("PVP::JoinMatchQueueInfoCommand", 0x00D0)
ABORT_MATCH_QUEUE_COMMAND = command_id("PVP::AbortMatchQueueCommand", 0x00D1)
JOIN_BATTLEGROUND_COMMAND = command_id("PVP::JoinBattleGroundCommand", 0x00D2)
JOIN_BATTLEGROUND_RESPONSE_COMMAND = command_id("PVP::JoinBattlegroundResponseCommand", 0x00D3)
MATCHMAKING_INFO_COMMAND = command_id("PVP::MatchMakingInfoCommand", 0x00D7)
HONOR_POINTS_CHANGED_COMMAND = command_id("Commands::HonorPointsChangedCommand", 0x007F)
BATCH_TERMINATOR = 0xFF
SERVER_BATCH_MESSAGE = 0x85
CLIENT_COMMAND_MESSAGE = 0x8B

# Server commands are wrapped by an outer object that owns one trailing
# ``uint32`` actor id, written after the body and before the terminator. Not
# every class has it: vtable slots 13 and 14 are its encode/decode accessors,
# and 270 of the 341 classes point them at the real implementation
# (``0x1409e1f5c`` / ``0x1409e1fe0``, which read and write a ``uint32`` at
# object offset +0x18) while 71 inherit stubs and send no actor at all.
#
# The split is visible in the layouts themselves: a wrapped class declares its
# own first member at +0x20, because +0x18 belongs to the wrapper, while an
# unwrapped one starts at +0x18.
#
# Getting this wrong is not a silent truncation. The client logs
# ``Could not decode command's additional server data`` and drops the whole
# command, leaving its bit reader mid-stream -- which corrupts every command
# batched behind it. Encoding therefore goes through
# ``_finish_server_command``, which refuses to write a command whose actor
# scoping disagrees with this table.
ACTOR_SCOPED_COMMANDS = frozenset({
    JOIN_MATCH_QUEUE_RESPONSE_COMMAND,
    SHOW_PVP_DEATH_DIALOG_COMMAND,
    HONOR_POINTS_CHANGED_COMMAND,
})
UNSCOPED_SERVER_COMMANDS = frozenset({
    PVP_MATCH_DESCRIPTION_COMMAND,
    PVP_MATCH_OVER_COMMAND,
    PVP_MATCH_START_COMMAND,
    PVP_MATCH_COUNTDOWN_COMMAND,
    PVP_MATCH_INFO_COMMAND,
    SET_BATTLEGROUND_ID_COMMAND,
    JOIN_MATCH_QUEUE_INFO_COMMAND,
    JOIN_BATTLEGROUND_COMMAND,
    MATCHMAKING_INFO_COMMAND,
})


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

# The client validates these on read as ``(value + 1) <= limit``, which fixes
# both the width (one signed byte) and the legal range.  ``-1`` is the shared
# "unset" member every one of them declares.
MATCH_MODE_INVALID = -1
MATCH_MODE_BEST_OF_3_1V1 = 0
MATCH_MODE_BEST_OF_5_5V5 = 1
MATCH_MODE_CAPTURE_THE_FLAG_5V5 = 2
MATCH_MODE_ASSAULT_THE_FORTRESS_5V5 = 3
MATCH_MODE_LAST_MAN_STANDING_8FFA = 4
MATCH_MODE_RANGE = (-1, 4)

# ``MatchMakingInfo.state`` is an event code, not an active/inactive flag.
# Values 0..6 are handled explicitly by the retail client.
MATCHMAKING_EVENT_INVALID = -1
MATCHMAKING_EVENT_JOINED_BY_USER = 0
MATCHMAKING_EVENT_JOINED_BY_GROUP_LEADER = 1
MATCHMAKING_EVENT_ABORTED_BY_USER = 2
MATCHMAKING_EVENT_ABORTED_BY_GROUP_LEADER = 3
MATCHMAKING_EVENT_ABORTED_BY_SERVER = 4
MATCHMAKING_EVENT_GROUP_TOO_BIG = 5
MATCHMAKING_EVENT_GROUP_LEVEL_RANGE = 6
MATCHMAKING_EVENT_RANGE = (-1, 6)

QUEUE_KIND_INVALID = -1
QUEUE_KIND_SINGLE = 0
QUEUE_KIND_GROUP = 1
QUEUE_KIND_RANGE = (-1, 1)

# ``ClientBattlegroundManager`` names these battleground ids as string
# literals; ``SetBattlegroundIdCommand`` carries the selected one verbatim.
BATTLEGROUND_IDS = {
    MATCH_MODE_BEST_OF_3_1V1: "BestOf3_1v1_01_default",
    MATCH_MODE_BEST_OF_5_5V5: "BestOf5_5v5_01_default",
    MATCH_MODE_CAPTURE_THE_FLAG_5V5: "CaptureTheFlag_5v5_01",
    MATCH_MODE_ASSAULT_THE_FORTRESS_5V5: "AssaultTheFortress_5v5_01",
    MATCH_MODE_LAST_MAN_STANDING_8FFA: "LastManStanding_8ffa_01",
}

# Arena map each mode is played on.  ``b1102_dm`` is the map retail sent this
# account to for a 1v1; the rest are the local maps whose names match the
# corresponding battleground.
BATTLEGROUND_MAPS = {
    MATCH_MODE_BEST_OF_3_1V1: "b1102_dm",
    MATCH_MODE_BEST_OF_5_5V5: "pvp_tdm_01",
    MATCH_MODE_CAPTURE_THE_FLAG_5V5: "pvp_ctf_01",
    MATCH_MODE_ASSAULT_THE_FORTRESS_5V5: "pvp_con_01",
    MATCH_MODE_LAST_MAN_STANDING_8FFA: "pvp_br_01",
}

# The 0x86 map load announces a ruleset, and a battleground needs its own.
# Retail loaded ``b1102_dm`` with **14** where it loads a hub with 5 and an
# ordinary PvE map with 1; ``_Template_MapTemplate`` calls the arena
# ``ClosedPVE`` and so cannot be used to derive this. Without it the client
# never brings its PvP subsystem up on the map, and the match countdown, its
# movement lock and the end-of-match overview are all silently inert -- the
# commands arrive and are dropped because nothing is listening for them.
BATTLEGROUND_RULESET = 14


def battleground_ruleset(map_id: str) -> int | None:
    """The map-load ruleset for an arena, or ``None`` for an ordinary map."""

    return (
        BATTLEGROUND_RULESET
        if str(map_id) in set(BATTLEGROUND_MAPS.values())
        else None
    )


# Team sizes per mode, used by the queue to decide when a match is full.
MATCH_TEAM_SIZES = {
    MATCH_MODE_BEST_OF_3_1V1: (1, 1),
    MATCH_MODE_BEST_OF_5_5V5: (5, 5),
    MATCH_MODE_CAPTURE_THE_FLAG_5V5: (5, 5),
    MATCH_MODE_ASSAULT_THE_FORTRESS_5V5: (5, 5),
    MATCH_MODE_LAST_MAN_STANDING_8FFA: (8,),
}

# Rounds needed to take the match, per mode name ("BestOf3" -> 2).
MATCH_ROUNDS_TO_WIN = {
    MATCH_MODE_BEST_OF_3_1V1: 2,
    MATCH_MODE_BEST_OF_5_5V5: 3,
    MATCH_MODE_CAPTURE_THE_FLAG_5V5: 3,
    MATCH_MODE_ASSAULT_THE_FORTRESS_5V5: 3,
    MATCH_MODE_LAST_MAN_STANDING_8FFA: 1,
}

# Match clocks are expressed in 25 Hz server ticks.  Retail's three recorded
# rounds started 401 and 354 ticks apart for wall-clock gaps of 16.04 s and
# 14.16 s, which fixes the rate; every capture carries 3000 as the round
# duration, i.e. two minutes.
MATCH_TICK_HZ = 25
ROUND_DURATION_TICKS = 3000


class PVPMatchProtocolError(ValueError):
    """A PvP command could not be decoded from the bytes supplied."""


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------

def _write_double(writer: BitWriter, value: float) -> None:
    """``BitWriter`` has no 64-bit float; the queue estimate is the only one."""

    writer.write_bytes(struct.pack("<d", float(value)))


def _read_double(reader: BitReader) -> float:
    return struct.unpack("<d", reader.read_bytes(8))[0]


def _write_enum8(writer: BitWriter, value: int, limits: tuple[int, int],
                 name: str) -> None:
    low, high = limits
    value = int(value)
    if not low <= value <= high:
        raise PVPMatchProtocolError(
            f"{name} {value} outside client range {low}..{high}"
        )
    writer.write_bits(value & 0xFF, 8)


def _read_enum8(reader: BitReader, limits: tuple[int, int],
                name: str) -> int:
    raw = reader.read_bits(8)
    value = raw - 256 if raw >= 0x80 else raw
    low, high = limits
    if not low <= value <= high:
        raise PVPMatchProtocolError(
            f"{name} {value} outside client range {low}..{high}"
        )
    return value


@dataclass(frozen=True)
class CalendarTime:
    """The eight ``uint32`` fields ``JoinMatchQueueInfo`` embeds at +0x30.

    Field order is fixed by the two recorded queue joins: 17:03 local on
    2026-09-09 serialized as ``(2026, 30, 14, 3, 0, 0, 4, 7)`` and 02:05:51
    local on 2026-09-09 as ``(2026, 3, 23, 5, 51, 0, 4, 9)``.  Both are the
    UTC instant, and both carry ISO weekday 4 -- a Thursday in each case,
    which is what pins the sixth and seventh slots apart.
    """

    year: int = 0
    day: int = 0
    hour: int = 0
    minute: int = 0
    second: int = 0
    millisecond: int = 0
    weekday: int = 0
    month: int = 0

    @classmethod
    def from_datetime(cls, moment: datetime) -> "CalendarTime":
        moment = moment.astimezone(timezone.utc)
        return cls(
            moment.year,
            moment.day,
            moment.hour,
            moment.minute,
            moment.second,
            moment.microsecond // 1000,
            moment.isoweekday(),
            moment.month,
        )

    def write(self, writer: BitWriter) -> None:
        for value in (self.year, self.day, self.hour, self.minute,
                      self.second, self.millisecond, self.weekday,
                      self.month):
            writer.write_uint32_le(int(value))

    @classmethod
    def read(cls, reader: BitReader) -> "CalendarTime":
        return cls(*(reader.read_uint32_le() for _ in range(8)))


def _begin_server_command(writer: BitWriter, command_id: int) -> None:
    writer.write_uint8(SERVER_BATCH_MESSAGE)
    writer.write_bytes(command_id.to_bytes(2, "little"))


def _finish_server_command(
    writer: BitWriter, command_id: int, actor_id: int | None = None
) -> tuple[bytes, int]:
    """Close one server command, writing its actor id when it has one."""

    scoped = command_id in ACTOR_SCOPED_COMMANDS
    if scoped:
        writer.write_uint32_le(int(actor_id or 0))
    elif actor_id is not None:
        raise PVPMatchProtocolError(
            f"command 0x{command_id:04x} carries no actor id"
        )
    writer.write_uint8(BATCH_TERMINATOR)
    return writer.finish()


def _start_server_command(reader: BitReader, command_id: int) -> None:
    if reader.read_uint8() != SERVER_BATCH_MESSAGE:
        raise PVPMatchProtocolError("message is not a server command batch")
    actual = int.from_bytes(reader.read_bytes(2), "little")
    if actual != command_id:
        raise PVPMatchProtocolError(
            f"expected command 0x{command_id:04x}, got 0x{actual:04x}"
        )


def _end_server_command(reader: BitReader, command_id: int) -> int | None:
    """Read the trailing actor id, when the command has one, and the 0xff."""

    actor_id = (
        reader.read_uint32_le()
        if command_id in ACTOR_SCOPED_COMMANDS
        else None
    )
    if reader.read_uint8() != BATCH_TERMINATOR:
        raise PVPMatchProtocolError("command is not followed by 0xff")
    if reader.remaining:
        raise PVPMatchProtocolError(
            f"command has {reader.remaining} unread bits"
        )
    return actor_id


def _start_client_command(reader: BitReader, command_id: int) -> None:
    if reader.read_uint8() != CLIENT_COMMAND_MESSAGE:
        raise PVPMatchProtocolError("message is not a client command")
    actual = int.from_bytes(reader.read_bytes(2), "little")
    if actual != command_id:
        raise PVPMatchProtocolError(
            f"expected command 0x{command_id:04x}, got 0x{actual:04x}"
        )


def _end_client_command(reader: BitReader, name: str) -> None:
    if reader.remaining:
        raise PVPMatchProtocolError(
            f"{name} has {reader.remaining} unread bits"
        )


# ---------------------------------------------------------------------------
# Queue entry -- client to server
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JoinMatchQueue:
    """``PVP::JoinMatchQueueCommand`` (0x00CE), Encode at 0x14099b374.

    ``uint32`` + two signed-byte enums = 48 bits, which is exactly the 72-bit
    request the client sent when queueing for a 1v1 (``00000000 00 00``).
    """

    server_tick: int = 0
    match_mode: int = MATCH_MODE_BEST_OF_3_1V1
    queue_kind: int = QUEUE_KIND_SINGLE


def decode_join_match_queue(
        payload: bytes, bit_length: int | None = None) -> JoinMatchQueue:
    reader = BitReader(payload, bit_length)
    _start_client_command(reader, JOIN_MATCH_QUEUE_COMMAND)
    value = JoinMatchQueue(
        reader.read_uint32_le(),
        _read_enum8(reader, MATCH_MODE_RANGE, "match mode"),
        _read_enum8(reader, QUEUE_KIND_RANGE, "queue kind"),
    )
    _end_client_command(reader, "JoinMatchQueue")
    return value


def encode_join_match_queue(value: JoinMatchQueue) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(CLIENT_COMMAND_MESSAGE)
    writer.write_bytes(JOIN_MATCH_QUEUE_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(int(value.server_tick))
    _write_enum8(writer, value.match_mode, MATCH_MODE_RANGE, "match mode")
    _write_enum8(writer, value.queue_kind, QUEUE_KIND_RANGE, "queue kind")
    return writer.finish()


@dataclass(frozen=True)
class AbortMatchQueue:
    """``PVP::AbortMatchQueueCommand`` (0x00D1), Encode at 0x14099ad74.

    ``uint32``, bool, an array of match modes, then a trailing bool.
    """

    server_tick: int = 0
    flag: bool = False
    match_modes: tuple[int, ...] = ()
    all_queues: bool = False


def decode_abort_match_queue(
        payload: bytes, bit_length: int | None = None) -> AbortMatchQueue:
    reader = BitReader(payload, bit_length)
    _start_client_command(reader, ABORT_MATCH_QUEUE_COMMAND)
    server_tick = reader.read_uint32_le()
    flag = reader.read_bool()
    count = reader.read_uint32_le()
    modes = tuple(
        _read_enum8(reader, MATCH_MODE_RANGE, "match mode")
        for _ in range(count)
    )
    value = AbortMatchQueue(server_tick, flag, modes, reader.read_bool())
    _end_client_command(reader, "AbortMatchQueue")
    return value


def encode_abort_match_queue(value: AbortMatchQueue) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(CLIENT_COMMAND_MESSAGE)
    writer.write_bytes(ABORT_MATCH_QUEUE_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(int(value.server_tick))
    writer.write_bool(bool(value.flag))
    writer.write_uint32_le(len(value.match_modes))
    for mode in value.match_modes:
        _write_enum8(writer, mode, MATCH_MODE_RANGE, "match mode")
    writer.write_bool(bool(value.all_queues))
    return writer.finish()


@dataclass(frozen=True)
class LeavePVP:
    """``PVP::LeavePVPCommand`` (0x00C7), Encode at 0x14099b50c.

    A single bool; the recorded request is 25 bits (``8bc700 80``).
    """

    leave: bool = True


def decode_leave_pvp(
        payload: bytes, bit_length: int | None = None) -> LeavePVP:
    reader = BitReader(payload, bit_length)
    _start_client_command(reader, LEAVE_PVP_COMMAND)
    value = LeavePVP(reader.read_bool())
    _end_client_command(reader, "LeavePVP")
    return value


def encode_leave_pvp(value: LeavePVP) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(CLIENT_COMMAND_MESSAGE)
    writer.write_bytes(LEAVE_PVP_COMMAND.to_bytes(2, "little"))
    writer.write_bool(bool(value.leave))
    return writer.finish()


@dataclass(frozen=True)
class JoinBattlegroundResponse:
    """``PVP::JoinBattlegroundResponseCommand`` (0x00D3), Encode 0x14099b290.

    ``uint32``, ``uint32``, bool, ``uint32``, enum = 105 bits, matching the
    129-bit accept the client sent one and a half seconds after the match
    dialog opened.  The answer is the bool: it was set in that accept, while
    both surrounding ``uint32`` fields were zero.
    """

    server_tick: int = 0
    battleground_id: int = 0
    accepted: bool = True
    field_2c: int = 0
    match_mode: int = MATCH_MODE_BEST_OF_3_1V1


def decode_join_battleground_response(
        payload: bytes,
        bit_length: int | None = None) -> JoinBattlegroundResponse:
    reader = BitReader(payload, bit_length)
    _start_client_command(reader, JOIN_BATTLEGROUND_RESPONSE_COMMAND)
    value = JoinBattlegroundResponse(
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_bool(),
        reader.read_uint32_le(),
        _read_enum8(reader, MATCH_MODE_RANGE, "match mode"),
    )
    _end_client_command(reader, "JoinBattlegroundResponse")
    return value


def encode_join_battleground_response(
        value: JoinBattlegroundResponse) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(CLIENT_COMMAND_MESSAGE)
    writer.write_bytes(
        JOIN_BATTLEGROUND_RESPONSE_COMMAND.to_bytes(2, "little")
    )
    writer.write_uint32_le(int(value.server_tick))
    writer.write_uint32_le(int(value.battleground_id))
    writer.write_bool(bool(value.accepted))
    writer.write_uint32_le(int(value.field_2c))
    _write_enum8(writer, value.match_mode, MATCH_MODE_RANGE, "match mode")
    return writer.finish()


# ---------------------------------------------------------------------------
# Queue state -- server to client
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MatchMakingInfo:
    """``PVP::MatchMakingInfoCommand`` (0x00D7), Encode at 0x14099b55c.

    Matchmaking event, ``uint32``, match mode, string. The client distinguishes
    queue joined/aborted events using the first enum; it is not a boolean state.
    Retail sends an empty string, so the whole command including its
    terminator is twelve bytes.
    """

    state: int = 0
    server_tick: int = 0
    match_mode: int = MATCH_MODE_INVALID
    message: str = ""


def encode_matchmaking_info(value: MatchMakingInfo) -> tuple[bytes, int]:
    writer = BitWriter()
    _begin_server_command(writer, MATCHMAKING_INFO_COMMAND)
    _write_enum8(writer, value.state, MATCHMAKING_EVENT_RANGE,
                 "matchmaking event")
    writer.write_uint32_le(int(value.server_tick))
    _write_enum8(writer, value.match_mode, MATCH_MODE_RANGE, "match mode")
    writer.write_string(value.message)
    return _finish_server_command(writer, MATCHMAKING_INFO_COMMAND)


def decode_matchmaking_info(
        payload: bytes, bit_length: int | None = None) -> MatchMakingInfo:
    reader = BitReader(payload, bit_length)
    _start_server_command(reader, MATCHMAKING_INFO_COMMAND)
    value = MatchMakingInfo(
        _read_enum8(reader, MATCHMAKING_EVENT_RANGE, "matchmaking event"),
        reader.read_uint32_le(),
        _read_enum8(reader, MATCH_MODE_RANGE, "match mode"),
        reader.read_string(),
    )
    _end_server_command(reader, MATCHMAKING_INFO_COMMAND)
    return value


@dataclass(frozen=True)
class JoinMatchQueueInfo:
    """``PVP::JoinMatchQueueInfoCommand`` (0x00D0), Encode at 0x14099b3e4.

    ``uint32``, a 64-bit double, enum, ``uint16``, then the eight-field
    calendar block.  ``queued_players`` is the ``uint16``; it read 1 in the
    first queue update and 2 in the one 0.7 s later.
    """

    server_tick: int = 0
    estimated_wait: float = 0.0
    state: int = 0
    queued_players: int = 0
    queued_at: CalendarTime = field(default_factory=CalendarTime)


def encode_join_match_queue_info(
        value: JoinMatchQueueInfo) -> tuple[bytes, int]:
    writer = BitWriter()
    _begin_server_command(writer, JOIN_MATCH_QUEUE_INFO_COMMAND)
    writer.write_uint32_le(int(value.server_tick))
    _write_double(writer, value.estimated_wait)
    _write_enum8(writer, value.state, (-1, 4), "queue state")
    writer.write_bytes((int(value.queued_players) & 0xFFFF).to_bytes(2, "little"))
    value.queued_at.write(writer)
    return _finish_server_command(writer, JOIN_MATCH_QUEUE_INFO_COMMAND)


def decode_join_match_queue_info(
        payload: bytes, bit_length: int | None = None) -> JoinMatchQueueInfo:
    reader = BitReader(payload, bit_length)
    _start_server_command(reader, JOIN_MATCH_QUEUE_INFO_COMMAND)
    server_tick = reader.read_uint32_le()
    estimated_wait = _read_double(reader)
    state = _read_enum8(reader, (-1, 4), "queue state")
    queued_players = int.from_bytes(reader.read_bytes(2), "little")
    value = JoinMatchQueueInfo(
        server_tick,
        estimated_wait,
        state,
        queued_players,
        CalendarTime.read(reader),
    )
    _end_server_command(reader, JOIN_MATCH_QUEUE_INFO_COMMAND)
    return value


@dataclass(frozen=True)
class JoinMatchQueueResponse:
    """``PVP::JoinMatchQueueResponseCommand`` (0x00CF), Encode 0x14099b4a8.

    enum + bool = 9 bits of body, plus the wrapper's trailing actor id -- this
    is one of the three commands here that carries one.
    """

    result: int = 0
    accepted: bool = True
    actor_id: int = 0


def encode_join_match_queue_response(
        value: JoinMatchQueueResponse) -> tuple[bytes, int]:
    writer = BitWriter()
    _begin_server_command(writer, JOIN_MATCH_QUEUE_RESPONSE_COMMAND)
    _write_enum8(writer, value.result, (-1, 4), "queue result")
    writer.write_bool(bool(value.accepted))
    return _finish_server_command(
        writer, JOIN_MATCH_QUEUE_RESPONSE_COMMAND, value.actor_id
    )


def decode_join_match_queue_response(
        payload: bytes,
        bit_length: int | None = None) -> JoinMatchQueueResponse:
    reader = BitReader(payload, bit_length)
    _start_server_command(reader, JOIN_MATCH_QUEUE_RESPONSE_COMMAND)
    result = _read_enum8(reader, (-1, 4), "queue result")
    accepted = reader.read_bool()
    actor_id = _end_server_command(reader, JOIN_MATCH_QUEUE_RESPONSE_COMMAND)
    return JoinMatchQueueResponse(result, accepted, actor_id or 0)


@dataclass(frozen=True)
class JoinBattleground:
    """``PVP::JoinBattleGroundCommand`` (0x00D2), Encode at 0x14099b1d0.

    ``uint32``, ``uint32``, ``uint32``, string, enum.  The recorded match
    invitation is 216 bits, which this accounts for exactly with the map name
    ``b1102_dm``.
    """

    server_tick: int = 0
    dialog_id: int = 0
    battleground_id: int = 0
    map_id: str = ""
    match_mode: int = MATCH_MODE_BEST_OF_3_1V1


def encode_join_battleground(value: JoinBattleground) -> tuple[bytes, int]:
    writer = BitWriter()
    _begin_server_command(writer, JOIN_BATTLEGROUND_COMMAND)
    writer.write_uint32_le(int(value.server_tick))
    writer.write_uint32_le(int(value.dialog_id))
    writer.write_uint32_le(int(value.battleground_id))
    writer.write_string(value.map_id)
    _write_enum8(writer, value.match_mode, MATCH_MODE_RANGE, "match mode")
    return _finish_server_command(writer, JOIN_BATTLEGROUND_COMMAND)


def decode_join_battleground(
        payload: bytes, bit_length: int | None = None) -> JoinBattleground:
    reader = BitReader(payload, bit_length)
    _start_server_command(reader, JOIN_BATTLEGROUND_COMMAND)
    value = JoinBattleground(
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_string(),
        _read_enum8(reader, MATCH_MODE_RANGE, "match mode"),
    )
    _end_server_command(reader, JOIN_BATTLEGROUND_COMMAND)
    return value


# ---------------------------------------------------------------------------
# Match lifecycle -- server to client
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SetBattlegroundId:
    """``PVP::SetBattlegroundIdCommand`` (0x00CB), Encode at 0x14099ba90.

    One string.  It is the first PvP command the arena map server sends and
    carried ``BestOf3_1v1_01_default`` for the recorded 1v1.
    """

    battleground_id: str = ""


def encode_set_battleground_id(
        value: SetBattlegroundId) -> tuple[bytes, int]:
    writer = BitWriter()
    _begin_server_command(writer, SET_BATTLEGROUND_ID_COMMAND)
    writer.write_string(value.battleground_id)
    return _finish_server_command(writer, SET_BATTLEGROUND_ID_COMMAND)


def decode_set_battleground_id(
        payload: bytes, bit_length: int | None = None) -> SetBattlegroundId:
    reader = BitReader(payload, bit_length)
    _start_server_command(reader, SET_BATTLEGROUND_ID_COMMAND)
    value = SetBattlegroundId(reader.read_string())
    _end_server_command(reader, SET_BATTLEGROUND_ID_COMMAND)
    return value


@dataclass(frozen=True)
class MatchDescription:
    """``PVP::PVPMatchDescriptionCommand`` (0x00CA), Encode at 0x14099b72c.

    A bool and a ``uint32``. Retail sends it **in the same batch as**
    ``SetBattlegroundId``, as the arena's very first message, carrying
    ``(True, 0)``:

    ``85 cb00 1600 "BestOf3_1v1_01_default" ff ca00 80 000000 7f ...``

    It is what tells the client's battleground manager that a match exists on
    this map. Without it the manager holds a battleground id and nothing else,
    so the countdown, its movement lock and the result panel are all delivered
    to something that is not running a match.
    """

    active: bool = True
    value: int = 0


def encode_match_description(value: MatchDescription) -> tuple[bytes, int]:
    writer = BitWriter()
    _begin_server_command(writer, PVP_MATCH_DESCRIPTION_COMMAND)
    writer.write_bool(bool(value.active))
    writer.write_uint32_le(int(value.value))
    return _finish_server_command(writer, PVP_MATCH_DESCRIPTION_COMMAND)


def decode_match_description(
        payload: bytes, bit_length: int | None = None) -> MatchDescription:
    reader = BitReader(payload, bit_length)
    _start_server_command(reader, PVP_MATCH_DESCRIPTION_COMMAND)
    value = MatchDescription(reader.read_bool(), reader.read_uint32_le())
    _end_server_command(reader, PVP_MATCH_DESCRIPTION_COMMAND)
    return value


@dataclass(frozen=True)
class MatchCountdown:
    """``PVP::PVPMatchCountdownCommand`` (0x00C3).

    The class declares no members: its Encode slot tail-jumps straight to the
    empty base, so the whole command on the wire is ``85 c300 ff``.  Retail
    sent it four seconds ahead of each ``PVPMatchStart``.
    """


def encode_match_countdown() -> tuple[bytes, int]:
    writer = BitWriter()
    _begin_server_command(writer, PVP_MATCH_COUNTDOWN_COMMAND)
    return _finish_server_command(writer, PVP_MATCH_COUNTDOWN_COMMAND)


def decode_match_countdown(
        payload: bytes, bit_length: int | None = None) -> MatchCountdown:
    reader = BitReader(payload, bit_length)
    _start_server_command(reader, PVP_MATCH_COUNTDOWN_COMMAND)
    _end_server_command(reader, PVP_MATCH_COUNTDOWN_COMMAND)
    return MatchCountdown()


@dataclass(frozen=True)
class MatchStart:
    """``PVP::PVPMatchStartCommand`` (0x00C2), Encode at 0x14099b91c.

    ``uint32`` round-start tick, ``uint32`` round duration, bool = 65 bits.
    The three recorded rounds started at ticks 329, 730 and 1084, all with a
    3000-tick duration.
    """

    round_start_tick: int = 0
    round_duration_ticks: int = ROUND_DURATION_TICKS
    flag: bool = False


def encode_match_start(value: MatchStart) -> tuple[bytes, int]:
    writer = BitWriter()
    _begin_server_command(writer, PVP_MATCH_START_COMMAND)
    writer.write_uint32_le(int(value.round_start_tick))
    writer.write_uint32_le(int(value.round_duration_ticks))
    writer.write_bool(bool(value.flag))
    return _finish_server_command(writer, PVP_MATCH_START_COMMAND)


def decode_match_start(
        payload: bytes, bit_length: int | None = None) -> MatchStart:
    reader = BitReader(payload, bit_length)
    _start_server_command(reader, PVP_MATCH_START_COMMAND)
    value = MatchStart(
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_bool(),
    )
    _end_server_command(reader, PVP_MATCH_START_COMMAND)
    return value


@dataclass(frozen=True)
class MatchOver:
    """``PVP::PVPMatchOverCommand`` (0x00C1), Encode at 0x14099b87c.

    ``uint32``, ``uint32`` winning team, bool = 65 bits, so the whole message
    is 97 bits.  The recorded loss ended with winning team 1; a session that
    ended with the local player's team winning carried 0.
    """

    server_tick: int = 0
    winning_team: int = 0
    flag: bool = False


def encode_match_over(value: MatchOver) -> tuple[bytes, int]:
    writer = BitWriter()
    _begin_server_command(writer, PVP_MATCH_OVER_COMMAND)
    writer.write_uint32_le(int(value.server_tick))
    writer.write_uint32_le(int(value.winning_team))
    writer.write_bool(bool(value.flag))
    return _finish_server_command(writer, PVP_MATCH_OVER_COMMAND)


def decode_match_over(
        payload: bytes, bit_length: int | None = None) -> MatchOver:
    reader = BitReader(payload, bit_length)
    _start_server_command(reader, PVP_MATCH_OVER_COMMAND)
    value = MatchOver(
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_bool(),
    )
    _end_server_command(reader, PVP_MATCH_OVER_COMMAND)
    return value


@dataclass(frozen=True)
class ShowPVPDeathDialog:
    """``PVP::ShowPVPDeathDialogCommand`` (0x00CC), Encode at 0x14099bac4."""

    respawn_delay_ticks: int = 0
    actor_id: int = 0


def encode_show_pvp_death_dialog(
        value: ShowPVPDeathDialog) -> tuple[bytes, int]:
    writer = BitWriter()
    _begin_server_command(writer, SHOW_PVP_DEATH_DIALOG_COMMAND)
    writer.write_uint32_le(int(value.respawn_delay_ticks))
    return _finish_server_command(
        writer, SHOW_PVP_DEATH_DIALOG_COMMAND, value.actor_id
    )


def decode_show_pvp_death_dialog(
        payload: bytes, bit_length: int | None = None) -> ShowPVPDeathDialog:
    reader = BitReader(payload, bit_length)
    _start_server_command(reader, SHOW_PVP_DEATH_DIALOG_COMMAND)
    delay = reader.read_uint32_le()
    actor_id = _end_server_command(reader, SHOW_PVP_DEATH_DIALOG_COMMAND)
    return ShowPVPDeathDialog(delay, actor_id or 0)


@dataclass(frozen=True)
class HonorPointsChanged:
    """``Commands::HonorPointsChangedCommand`` (0x007F), Encode 0x140941c74."""

    honor_points: int = 0
    actor_id: int = 0


def encode_honor_points_changed(
        value: HonorPointsChanged) -> tuple[bytes, int]:
    writer = BitWriter()
    _begin_server_command(writer, HONOR_POINTS_CHANGED_COMMAND)
    writer.write_uint32_le(int(value.honor_points))
    return _finish_server_command(
        writer, HONOR_POINTS_CHANGED_COMMAND, value.actor_id
    )


def decode_honor_points_changed(
        payload: bytes, bit_length: int | None = None) -> HonorPointsChanged:
    reader = BitReader(payload, bit_length)
    _start_server_command(reader, HONOR_POINTS_CHANGED_COMMAND)
    honor = reader.read_uint32_le()
    actor_id = _end_server_command(reader, HONOR_POINTS_CHANGED_COMMAND)
    return HonorPointsChanged(honor, actor_id or 0)


HONOR_LEVEL_THRESHOLDS: tuple[tuple[int, str], ...] = (
    (75_000_000, "pvptitle20"),
    (32_500_000, "pvptitle19"),
    (15_000_000, "pvptitle18"),
    (6_750_000, "pvptitle17"),
    (3_600_000, "pvptitle16"),
    (2_250_000, "pvptitle15"),
    (1_450_000, "pvptitle14"),
    (1_000_000, "pvptitle13"),
    (750_000, "pvptitle12"),
    (550_000, "pvptitle11"),
    (410_000, "pvptitle10"),
    (300_000, "pvptitle09"),
    (210_000, "pvptitle08"),
    (140_000, "pvptitle07"),
    (90_000, "pvptitle06"),
    (50_000, "pvptitle05"),
    (25_000, "pvptitle04"),
    (11_000, "pvptitle03"),
    (3_200, "pvptitle02"),
    (400, "pvptitle01"),
    (0, "pvptitle00"),
)


def honor_level_title_for(honor_points: int) -> str:
    points = max(0, int(honor_points))
    for threshold, title in HONOR_LEVEL_THRESHOLDS:
        if points >= threshold:
            return title
    return "pvptitle00"


# ---------------------------------------------------------------------------
# Scoreboard
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MatchPlayer:
    """One scoreboard row, serialized by 0x140aa11d4 / decoded by 0x140aa0e94.

    Ten ``uint32`` fields, a float, a ``uint32``, a ``uint16``, a bool, the
    player name, a ``uint32`` (class_id), an array of ``uint32`` (stats), and
    finally the member at +0x28 (rounds_lost). That ordering consumes the
    exact bit stream expected by the client.

    RE of ``ScoreboardWidget::FillTable`` and
    ``ClientBattlegroundManager::HandlePVPMatchInfoCommand`` shows that
    field 16 (written after player name) is copied to ``PvPMember + 0x0c``,
    which the scoreboard uses for the class emblem (Warrior=0, Mage=1,
    Ranger=2, Dwarf=3).
    """

    actor_id: int = 0
    honor_total: int = 0
    honor_rounds_won: int = 0
    honor_participation: int = 0
    honor_bonus: int = 0
    rounds_won: int = 0
    kills: int = 0
    deaths: int = 0
    damage_dealt: int = 0
    score: int = 0
    support: float = 0.0
    rating: int = 0
    level: int = 0
    enemy: bool = False
    name: str = ""
    class_id: int = 0
    stats: tuple[int, ...] = ()
    rounds_lost: int = 0

    def write(self, writer: BitWriter) -> None:
        for value in (self.actor_id, self.honor_total,
                      self.honor_rounds_won, self.honor_participation,
                      self.honor_bonus, self.rounds_won,
                      self.kills, self.deaths,
                      self.damage_dealt, self.score):
            writer.write_uint32_le(int(value))
        writer.write_float(float(self.support))
        writer.write_uint32_le(int(self.rating))
        writer.write_bytes((int(self.level) & 0xFFFF).to_bytes(2, "little"))
        writer.write_bool(bool(self.enemy))
        writer.write_string(self.name)
        writer.write_uint32_le(int(self.class_id))
        writer.write_uint32_le(len(self.stats))
        for entry in self.stats:
            writer.write_uint32_le(int(entry))
        writer.write_uint32_le(int(self.rounds_lost))

    @classmethod
    def read(cls, reader: BitReader) -> "MatchPlayer":
        head = [reader.read_uint32_le() for _ in range(10)]
        support = reader.read_float()
        rating = reader.read_uint32_le()
        level = int.from_bytes(reader.read_bytes(2), "little")
        enemy = reader.read_bool()
        name = reader.read_string()
        class_id = reader.read_uint32_le()
        count = reader.read_uint32_le()
        stats = tuple(reader.read_uint32_le() for _ in range(count))
        rounds_lost = reader.read_uint32_le()
        return cls(*head, support, rating, level, enemy, name, class_id,
                   stats, rounds_lost)


@dataclass(frozen=True)
class MatchTeam:
    """One team entry, serialized by 0x140a5efe4.

    ``uint32`` team id, ``uint32`` rounds won, ``uint32``, a float, then the
    player array.  The float held 0.371 and 0.629 for the two recorded teams
    -- a win probability, since the pair sums to one.
    """

    team_id: int = 0
    rounds_won: int = 0
    score: int = 0
    win_chance: float = 0.0
    players: tuple[MatchPlayer, ...] = ()

    def write(self, writer: BitWriter) -> None:
        writer.write_uint32_le(int(self.team_id))
        writer.write_uint32_le(int(self.rounds_won))
        writer.write_uint32_le(int(self.score))
        writer.write_float(float(self.win_chance))
        writer.write_uint32_le(len(self.players))
        for player in self.players:
            player.write(writer)

    @classmethod
    def read(cls, reader: BitReader) -> "MatchTeam":
        team_id = reader.read_uint32_le()
        rounds_won = reader.read_uint32_le()
        score = reader.read_uint32_le()
        win_chance = reader.read_float()
        count = reader.read_uint32_le()
        return cls(team_id, rounds_won, score, win_chance,
                   tuple(MatchPlayer.read(reader) for _ in range(count)))


@dataclass(frozen=True)
class MatchInfo:
    """``PVP::PVPMatchInfoCommand`` (0x00C4), Encode at 0x14099b7b0.

    ``uint32`` round-start tick, ``uint32`` round duration, the team array,
    the battleground id, and a trailing ``uint32``.  Retail repeats it about
    every two seconds and on every scoreboard change; it is what drives both
    the in-match HUD and the end-of-match overview.
    """

    round_start_tick: int = 0
    round_duration_ticks: int = ROUND_DURATION_TICKS
    teams: tuple[MatchTeam, ...] = ()
    battleground_id: str = ""
    stage: int = 0


def encode_match_info(value: MatchInfo) -> tuple[bytes, int]:
    writer = BitWriter()
    _begin_server_command(writer, PVP_MATCH_INFO_COMMAND)
    writer.write_uint32_le(int(value.round_start_tick))
    writer.write_uint32_le(int(value.round_duration_ticks))
    writer.write_uint32_le(len(value.teams))
    for team in value.teams:
        team.write(writer)
    writer.write_string(value.battleground_id)
    writer.write_uint32_le(int(value.stage))
    return _finish_server_command(writer, PVP_MATCH_INFO_COMMAND)


def decode_match_info(
        payload: bytes, bit_length: int | None = None) -> MatchInfo:
    reader = BitReader(payload, bit_length)
    _start_server_command(reader, PVP_MATCH_INFO_COMMAND)
    round_start_tick = reader.read_uint32_le()
    round_duration_ticks = reader.read_uint32_le()
    count = reader.read_uint32_le()
    teams = tuple(MatchTeam.read(reader) for _ in range(count))
    value = MatchInfo(
        round_start_tick,
        round_duration_ticks,
        teams,
        reader.read_string(),
        reader.read_uint32_le(),
    )
    _end_server_command(reader, PVP_MATCH_INFO_COMMAND)
    return value
