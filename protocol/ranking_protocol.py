"""Client leaderboard queries and actor-scoped ranking responses.

Honor layouts are from the release261 client serializers at
0x140a13488..0x140a13c74. The Parallel World, overall PvE and high-score
(Fractured Planes) layouts were read from the R261_1024 generated decoders:

========================================  ========================================
``Ranking::ParallelWorldUserRankingInfo``   0x140a567d4: i32 start, i32 count,
                                            u32 context, u16 filter, enum8 board
                                            -1..8, i8 class, i8 sort, enum8 type
                                            -1..1
``...ParallelWorldUserRankingResponse``     0x140a568c8: i32 total, u32 context,
                                            i8 status, rows (0x140aa7148: string,
                                            i32 rank, 4 x u32), i32 start, u16
                                            filter, enum8 board, i8 class, i8 sort,
                                            enum8 type
``...ParallelWorldLocalUserRankingInfo``    0x140a56660: u32 context, enum8 board,
                                            enum8 type
``...ParallelWorldLocalUserRanking``        0x140a566d4: u32 context, 4 x u32
``Response``                                (score, best time seconds, monster
                                            kills, boss kills), enum8 board,
                                            enum8 type, i8 status
``Ranking::OverallUserRankingInfo``         0x140a564a4: i32 start, i32 count,
                                            u32 context, u16 filter, i8 class,
                                            enum8 type
``...OverallUserRankingResponse``           0x140a56560: i32 total, u32 context,
                                            i8 status, rows (0x140aa71b0: string,
                                            u32 score, i32 rank), i32 start, u16
                                            filter, i8 class, enum8 type
``...OverallLocalUserRankingInfo``          0x140a563b4: u32 context, enum8 type
``...OverallLocalUserRankingInfoResponse``  0x140a56410: u32 context, u32 rank,
                                            enum8 type, i8 status
``Commands::GetHighScoreRankAllData``       0x14097a8f8: i32 class filter
``...GetHighScoreRankAllDataResult``        0x14097a938: i32 class filter, u32
                                            array, u32 array, string array
``Commands::GetHighScoreRankGroupData``     0x14097a9cc: no body
``...GetHighScoreRankGroupDataResult``      0x14097a9e0: u32 array, u32 array,
                                            string array
========================================  ========================================

The PW/overall requests are built by ClientRankingManager (0x14031a8c0,
0x14031aff4, 0x14031acbc, 0x14031a6ac, 0x14031aee0, 0x14031a9f4); the high
score requests by 0x14031a364 (Ladder/"Challenge Ranking": the class
selector) and 0x14031a3f4 (Group Ranking). Their result handlers
(0x1403180d4, 0x140318314) turn element ``i`` of the three arrays into one
row ``{rank i + 1, floor, remaining time, name}``; the table formats the
second value as ``%02u:%02u`` (0x14031fdd8) and pages the whole list
locally ten rows at a time (0x140317b70).

List ranks are zero-based; local-player rank responses use one-based ranks.
Client update commands (``HonorRankingMatchUpdate``, ``HonorRankingUpdate``,
``ParallelWorldUserRankingUpdate``) have no client creator call sites; they
are ranking-server inputs and are never accepted from a game client.
"""

from __future__ import annotations

from dataclasses import dataclass

from dso_server.protocol.character_selection import BitReader, BitWriter
from dso_server.protocol.command_ids import UNAVAILABLE_COMMAND_ID, command_id


HONOR_RANKING_INFO_COMMAND = command_id("Ranking::HonorRankingInfoCommand", 0xEB)
HONOR_RANKING_RESPONSE_COMMAND = command_id("Ranking::HonorRankingResponseCommand", 0xEC)
HONOR_RANKING_MATCH_UPDATE_COMMAND = command_id(
    "Ranking::HonorRankingMatchUpdateCommand", 0xED)
HONOR_RANKING_UPDATE_COMMAND = command_id("Ranking::HonorRankingUpdateCommand", 0xEF)
HONOR_OVERALL_USER_RANKING_INFO_COMMAND = command_id(
    "Ranking::HonorOverallUserRankingInfoCommand", 0xF0)
HONOR_OVERALL_USER_RANKING_RESPONSE_COMMAND = command_id(
    "Ranking::HonorOverallUserRankingResponseCommand", 0xF1)
HONOR_LOCAL_USER_RANKING_INFO_COMMAND = command_id(
    "Ranking::HonorLocalUserRankingInfoCommand", 0xF2)
HONOR_LOCAL_USER_RANKING_RESPONSE_COMMAND = command_id(
    "Ranking::HonorLocalUserRankingInfoResponseCommand", 0xF3)
HONOR_OVERALL_LOCAL_USER_RANKING_INFO_COMMAND = command_id(
    "Ranking::HonorOverallLocalUserRankingInfoCommand", 0xF4)
HONOR_OVERALL_LOCAL_USER_RANKING_RESPONSE_COMMAND = command_id(
    "Ranking::HonorOverallLocalUserRankingInfoResponseCommand", 0xF5)
PARALLEL_WORLD_USER_RANKING_INFO_COMMAND = command_id(
    "Ranking::ParallelWorldUserRankingInfoCommand", 0xF6)
PARALLEL_WORLD_USER_RANKING_RESPONSE_COMMAND = command_id(
    "Ranking::ParallelWorldUserRankingResponseCommand", 0xF7)
PARALLEL_WORLD_LOCAL_USER_RANKING_INFO_COMMAND = command_id(
    "Ranking::ParallelWorldLocalUserRankingInfoCommand", 0xF8)
PARALLEL_WORLD_LOCAL_USER_RANKING_RESPONSE_COMMAND = command_id(
    "Ranking::ParallelWorldLocalUserRankingResponseCommand", 0xF9)
PARALLEL_WORLD_USER_RANKING_UPDATE_COMMAND = command_id(
    "Ranking::ParallelWorldUserRankingUpdateCommand", 0xFA)
OVERALL_USER_RANKING_INFO_COMMAND = command_id(
    "Ranking::OverallUserRankingInfoCommand", 0xFC)
OVERALL_USER_RANKING_RESPONSE_COMMAND = command_id(
    "Ranking::OverallUserRankingResponseCommand", 0xFD)
OVERALL_LOCAL_USER_RANKING_INFO_COMMAND = command_id(
    "Ranking::OverallLocalUserRankingInfoCommand", 0xFE)
OVERALL_LOCAL_USER_RANKING_RESPONSE_COMMAND = command_id(
    "Ranking::OverallLocalUserRankingInfoResponseCommand", 0xFF)
# dro_client64-patch5 numbers these 0x169/0x16a and has no group query;
# release261_1024 uses 0x16d/0x16e and 0x172/0x173.
GET_HIGH_SCORE_RANK_ALL_DATA_COMMAND = command_id(
    "Commands::GetHighScoreRankAllDataCommand", 0x169)
GET_HIGH_SCORE_RANK_ALL_DATA_RESULT_COMMAND = command_id(
    "Commands::GetHighScoreRankAllDataResultCommand", 0x16A)
GET_HIGH_SCORE_RANK_GROUP_DATA_COMMAND = command_id(
    "Commands::GetHighScoreRankGroupDataCommand", UNAVAILABLE_COMMAND_ID)
GET_HIGH_SCORE_RANK_GROUP_DATA_RESULT_COMMAND = command_id(
    "Commands::GetHighScoreRankGroupDataResultCommand", UNAVAILABLE_COMMAND_ID)

HONOR_LIST_COMMANDS = frozenset({
    HONOR_RANKING_INFO_COMMAND, HONOR_OVERALL_USER_RANKING_INFO_COMMAND,
})
HONOR_LOCAL_COMMANDS = frozenset({
    HONOR_LOCAL_USER_RANKING_INFO_COMMAND, HONOR_OVERALL_LOCAL_USER_RANKING_INFO_COMMAND,
})
PVE_LIST_COMMANDS = frozenset({
    PARALLEL_WORLD_USER_RANKING_INFO_COMMAND, OVERALL_USER_RANKING_INFO_COMMAND,
})
PVE_LOCAL_COMMANDS = frozenset({
    PARALLEL_WORLD_LOCAL_USER_RANKING_INFO_COMMAND, OVERALL_LOCAL_USER_RANKING_INFO_COMMAND,
})
HIGH_SCORE_COMMANDS = frozenset({
    GET_HIGH_SCORE_RANK_ALL_DATA_COMMAND, GET_HIGH_SCORE_RANK_GROUP_DATA_COMMAND,
}) - {UNAVAILABLE_COMMAND_ID}
RANKING_UPDATE_COMMANDS = frozenset({
    HONOR_RANKING_MATCH_UPDATE_COMMAND, HONOR_RANKING_UPDATE_COMMAND,
    PARALLEL_WORLD_USER_RANKING_UPDATE_COMMAND,
})
# Every ranking id the client can send. Update commands are routed here only
# so they are rejected explicitly instead of falling through as unknown.
RANKING_REQUEST_COMMANDS = (
    HONOR_LIST_COMMANDS | HONOR_LOCAL_COMMANDS | PVE_LIST_COMMANDS
    | PVE_LOCAL_COMMANDS | HIGH_SCORE_COMMANDS | RANKING_UPDATE_COMMANDS
)
FILTER_ALL = 0x01
FILTER_FRIENDS = 0x02
FILTER_GUILD = 0x04
FILTER_PLAYER_PAGE = 0x10
UNRANKED = 0xFFFFFFFF
MAX_RANKING_PAGE_SIZE = 10  # The client's table allocates exactly ten UI rows.
ALL_CLASSES = 4
PARALLEL_WORLD_BOARDS = range(9)   # enum8 -1..8 at +0x30 (0x140aa6db8).
PVE_BOARD_TYPES = range(2)         # enum8 -1..1: 0 solo, 1 group (0x140aa6df0).
# Sort buttons of the PW table: rank score, time, monster and boss points.
PARALLEL_WORLD_SORT_COLUMNS = range(4)


@dataclass(frozen=True)
class RankingRequest:
    command_id: int
    start: int = UNRANKED
    count: int = MAX_RANKING_PAGE_SIZE
    context: int = 0
    flags: int = FILTER_ALL
    class_id: int = ALL_CLASSES
    board: int = 4
    sort_column: int = 0
    board_type: int = 0


@dataclass(frozen=True)
class RankingRow:
    name: str
    score: int
    rank: int
    wins: int = 0
    losses: int = 0
    draws: int = 0
    kills: int = 0
    deaths: int = 0


@dataclass(frozen=True)
class ParallelWorldRow:
    name: str
    rank: int
    stats: tuple[int, int, int, int]


@dataclass(frozen=True)
class ParallelWorldSummary:
    """The player's best run: score, time (s), monster and boss kills."""

    score: int
    best_time: int
    monster_kills: int
    boss_kills: int


@dataclass(frozen=True)
class HighScoreEntry:
    name: str
    floor: int
    remaining_seconds: int


def _signed32(value: int) -> int:
    return value - (1 << 32) if value & 0x80000000 else value


def decode_ranking_request(payload: bytes, bit_length: int | None = None) -> RankingRequest:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x8B:
        raise ValueError("not a client ranking command")
    command = int.from_bytes(reader.read_bytes(2), "little")
    if command in RANKING_UPDATE_COMMANDS:
        raise ValueError(
            f"ranking update 0x{command:04x} is server-authoritative, not a client query")
    if command not in RANKING_REQUEST_COMMANDS:
        raise ValueError(f"unsupported ranking request 0x{command:04x}")
    if command == HONOR_OVERALL_LOCAL_USER_RANKING_INFO_COMMAND:
        value = RankingRequest(command, context=reader.read_uint32_le())
    elif command == HONOR_LOCAL_USER_RANKING_INFO_COMMAND:
        value = RankingRequest(command, context=reader.read_uint32_le(), board=reader.read_uint8())
    elif command in HONOR_LIST_COMMANDS:
        board = reader.read_uint8() if command == HONOR_RANKING_INFO_COMMAND else 4
        start, count, context = (reader.read_uint32_le() for _ in range(3))
        flags = int.from_bytes(reader.read_bytes(2), "little")
        class_id = reader.read_uint8()
        column = reader.read_uint8() if command == HONOR_RANKING_INFO_COMMAND else 0
        value = RankingRequest(command, start, count, context, flags, class_id, board, column)
        if value.sort_column not in range(6):
            raise ValueError("invalid ranking sort column")
    elif command == PARALLEL_WORLD_USER_RANKING_INFO_COMMAND:
        start, count, context = (reader.read_uint32_le() for _ in range(3))
        flags = int.from_bytes(reader.read_bytes(2), "little")
        board, class_id, column, board_type = (reader.read_uint8() for _ in range(4))
        value = RankingRequest(command, start, count, context, flags, class_id,
                               board, column, board_type)
        if value.sort_column not in PARALLEL_WORLD_SORT_COLUMNS:
            raise ValueError("invalid Parallel World sort column")
    elif command == PARALLEL_WORLD_LOCAL_USER_RANKING_INFO_COMMAND:
        context, board, board_type = (
            reader.read_uint32_le(), reader.read_uint8(), reader.read_uint8())
        value = RankingRequest(command, context=context, board=board, board_type=board_type)
    elif command == OVERALL_USER_RANKING_INFO_COMMAND:
        start, count, context = (reader.read_uint32_le() for _ in range(3))
        flags = int.from_bytes(reader.read_bytes(2), "little")
        class_id, board_type = reader.read_uint8(), reader.read_uint8()
        value = RankingRequest(command, start, count, context, flags, class_id,
                               board=0, board_type=board_type)
    elif command == OVERALL_LOCAL_USER_RANKING_INFO_COMMAND:
        context, board_type = reader.read_uint32_le(), reader.read_uint8()
        value = RankingRequest(command, context=context, board=0, board_type=board_type)
    elif command == GET_HIGH_SCORE_RANK_ALL_DATA_COMMAND:
        class_id = _signed32(reader.read_uint32_le())
        if class_id not in range(ALL_CLASSES + 1):
            raise ValueError("invalid high-score class filter")
        value = RankingRequest(command, class_id=class_id, board=0)
    else:  # GetHighScoreRankGroupDataCommand carries no fields.
        value = RankingRequest(command, board=0)
    if value.class_id not in range(ALL_CLASSES + 1):
        raise ValueError("invalid ranking class filter")
    if command in PVE_LIST_COMMANDS | PVE_LOCAL_COMMANDS:
        if value.board_type not in PVE_BOARD_TYPES:
            raise ValueError("invalid PvE leaderboard type")
        if (command in {PARALLEL_WORLD_USER_RANKING_INFO_COMMAND,
                        PARALLEL_WORLD_LOCAL_USER_RANKING_INFO_COMMAND}
                and value.board not in PARALLEL_WORLD_BOARDS):
            raise ValueError("invalid Parallel World leaderboard")
    elif value.board not in (*range(5), 0xFF):
        raise ValueError("invalid honor ranking board")
    if reader.remaining:
        raise ValueError(f"ranking request has {reader.remaining} unread bits")
    return value


def _response_writer(command: int) -> BitWriter:
    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(command.to_bytes(2, "little"))
    return writer


def _u32(value: int) -> int:
    return max(0, min(0xFFFFFFFF, int(value)))


def encode_ranking_page(
    request: RankingRequest, rows: tuple[RankingRow, ...], *, start: int,
    total: int, actor_id: int = 1,
) -> tuple[bytes, int]:
    detailed = request.command_id == HONOR_RANKING_INFO_COMMAND
    if not detailed and request.command_id != HONOR_OVERALL_USER_RANKING_INFO_COMMAND:
        raise ValueError("ranking page requires a list request")
    if len(rows) > MAX_RANKING_PAGE_SIZE:
        raise ValueError("ranking page exceeds the client's ten row table")
    writer = _response_writer(
        HONOR_RANKING_RESPONSE_COMMAND if detailed else HONOR_OVERALL_USER_RANKING_RESPONSE_COMMAND)
    if detailed:
        writer.write_uint8(request.board)
    writer.write_uint32_le(total)
    writer.write_uint32_le(request.context)
    writer.write_uint8(0)  # Ranking response status: success.
    writer.write_uint32_le(len(rows))
    for row in rows:
        writer.write_string(row.name)
        if detailed:
            writer.write_uint32_le(row.rank)
            for value in (row.score, row.wins, row.losses, row.draws, row.kills, row.deaths):
                writer.write_uint32_le(value)
        else:
            writer.write_uint32_le(row.score)
            writer.write_uint32_le(row.rank)
    writer.write_uint32_le(start)
    if detailed:
        writer.write_uint8(request.class_id)
        writer.write_uint8(request.sort_column)
        writer.write_bytes(request.flags.to_bytes(2, "little"))
    else:
        writer.write_bytes(request.flags.to_bytes(2, "little"))
        writer.write_uint8(request.class_id)
    writer.write_uint32_le(actor_id)
    return writer.finish()


def encode_local_ranking(
    request: RankingRequest, row: RankingRow | None, *, actor_id: int = 1,
) -> tuple[bytes, int]:
    detailed = request.command_id == HONOR_LOCAL_USER_RANKING_INFO_COMMAND
    if not detailed and request.command_id != HONOR_OVERALL_LOCAL_USER_RANKING_INFO_COMMAND:
        raise ValueError("local ranking requires a local player request")
    writer = _response_writer(
        HONOR_LOCAL_USER_RANKING_RESPONSE_COMMAND if detailed
        else HONOR_OVERALL_LOCAL_USER_RANKING_RESPONSE_COMMAND)
    writer.write_uint32_le(request.context)
    writer.write_uint32_le(row.rank + 1 if row is not None else UNRANKED)
    if detailed:
        # +0x28 score, +0x2c kills, +0x30 deaths, +0x34 wins,
        # +0x38 losses, +0x3c draws, +0x40 board, +0x44 active.
        for value in ((row.score, row.kills, row.deaths, row.wins, row.losses, row.draws)
                      if row is not None else (0,) * 6):
            writer.write_uint32_le(value)
    writer.write_uint8(request.board if detailed else 0)
    if detailed:
        writer.write_bool(row is not None)
    writer.write_uint32_le(actor_id)
    return writer.finish()


def encode_parallel_world_page(
    request: RankingRequest, rows: tuple[ParallelWorldRow, ...], *,
    start: int, total: int, actor_id: int = 1,
) -> tuple[bytes, int]:
    """``ParallelWorldUserRankingResponseCommand`` (decoder 0x140a568c8)."""

    if request.command_id != PARALLEL_WORLD_USER_RANKING_INFO_COMMAND:
        raise ValueError("Parallel World page requires a Parallel World list request")
    if len(rows) > MAX_RANKING_PAGE_SIZE:
        raise ValueError("ranking page exceeds the client's ten row table")
    writer = _response_writer(PARALLEL_WORLD_USER_RANKING_RESPONSE_COMMAND)
    writer.write_uint32_le(_u32(total))
    writer.write_uint32_le(request.context)
    writer.write_uint8(0)  # Ranking response status: success.
    writer.write_uint32_le(len(rows))
    for row in rows:
        writer.write_string(row.name)
        writer.write_uint32_le(_u32(row.rank))
        if len(row.stats) != 4:
            raise ValueError("Parallel World rows carry exactly four statistics")
        for value in row.stats:
            writer.write_uint32_le(_u32(value))
    writer.write_uint32_le(_u32(start))
    writer.write_bytes(request.flags.to_bytes(2, "little"))
    writer.write_uint8(request.board)
    writer.write_uint8(request.class_id)
    writer.write_uint8(request.sort_column)
    writer.write_uint8(request.board_type)
    writer.write_uint32_le(actor_id)
    return writer.finish()


def encode_parallel_world_local(
    request: RankingRequest, summary: ParallelWorldSummary | None, *,
    actor_id: int = 1,
) -> tuple[bytes, int]:
    """``ParallelWorldLocalUserRankingResponseCommand`` (0x140a566d4).

    The handler (0x140318ee4) hides the summary when the score is
    0xffffffff, checks the score against the board's league thresholds
    (0x140324120) and formats the next value as ``%02u:%02u:%02u``.
    """

    if request.command_id != PARALLEL_WORLD_LOCAL_USER_RANKING_INFO_COMMAND:
        raise ValueError("Parallel World summary requires a local request")
    writer = _response_writer(PARALLEL_WORLD_LOCAL_USER_RANKING_RESPONSE_COMMAND)
    writer.write_uint32_le(request.context)
    values = ((summary.score, summary.best_time, summary.monster_kills, summary.boss_kills)
              if summary is not None else (UNRANKED, 0, 0, 0))
    for value in values:
        writer.write_uint32_le(_u32(value))
    writer.write_uint8(request.board)
    writer.write_uint8(request.board_type)
    writer.write_uint8(0)  # Status: success.
    writer.write_uint32_le(actor_id)
    return writer.finish()


def encode_overall_page(
    request: RankingRequest, rows: tuple[RankingRow, ...], *, start: int,
    total: int, actor_id: int = 1,
) -> tuple[bytes, int]:
    """``OverallUserRankingResponseCommand`` (decoder 0x140a56560)."""

    if request.command_id != OVERALL_USER_RANKING_INFO_COMMAND:
        raise ValueError("overall page requires an overall list request")
    if len(rows) > MAX_RANKING_PAGE_SIZE:
        raise ValueError("ranking page exceeds the client's ten row table")
    writer = _response_writer(OVERALL_USER_RANKING_RESPONSE_COMMAND)
    writer.write_uint32_le(_u32(total))
    writer.write_uint32_le(request.context)
    writer.write_uint8(0)
    writer.write_uint32_le(len(rows))
    for row in rows:
        writer.write_string(row.name)
        writer.write_uint32_le(_u32(row.score))
        writer.write_uint32_le(_u32(row.rank))
    writer.write_uint32_le(_u32(start))
    writer.write_bytes(request.flags.to_bytes(2, "little"))
    writer.write_uint8(request.class_id)
    writer.write_uint8(request.board_type)
    writer.write_uint32_le(actor_id)
    return writer.finish()


def encode_overall_local(
    request: RankingRequest, rank: int | None, *, actor_id: int = 1,
) -> tuple[bytes, int]:
    """``OverallLocalUserRankingInfoResponseCommand``; ``rank`` is zero-based."""

    if request.command_id != OVERALL_LOCAL_USER_RANKING_INFO_COMMAND:
        raise ValueError("overall local response requires an overall local request")
    writer = _response_writer(OVERALL_LOCAL_USER_RANKING_RESPONSE_COMMAND)
    writer.write_uint32_le(request.context)
    writer.write_uint32_le(UNRANKED if rank is None else _u32(rank + 1))
    writer.write_uint8(request.board_type)
    writer.write_uint8(0)
    writer.write_uint32_le(actor_id)
    return writer.finish()


def _write_high_score_arrays(writer: BitWriter, entries) -> None:
    writer.write_uint32_le(len(entries))
    for entry in entries:
        writer.write_uint32_le(_u32(entry.floor))
    writer.write_uint32_le(len(entries))
    for entry in entries:
        writer.write_uint32_le(_u32(entry.remaining_seconds))
    writer.write_uint32_le(len(entries))
    for entry in entries:
        writer.write_string(entry.name)


def encode_high_score_all_data(
    class_filter: int, entries: tuple[HighScoreEntry, ...], *, actor_id: int = 1,
) -> tuple[bytes, int]:
    """``GetHighScoreRankAllDataResultCommand``, ranked best first.

    The class filter is echoed: the handler stores the list under it
    (ClientRankingManager +0x1e0) and serves later pages from that cache.
    """

    writer = _response_writer(GET_HIGH_SCORE_RANK_ALL_DATA_RESULT_COMMAND)
    writer.write_uint32_le(int(class_filter) & 0xFFFFFFFF)
    _write_high_score_arrays(writer, entries)
    writer.write_uint32_le(actor_id)
    return writer.finish()


def encode_high_score_group_data(
    entries: tuple[HighScoreEntry, ...], *, actor_id: int = 1,
) -> tuple[bytes, int]:
    """``GetHighScoreRankGroupDataResultCommand``, ranked best first."""

    writer = _response_writer(GET_HIGH_SCORE_RANK_GROUP_DATA_RESULT_COMMAND)
    _write_high_score_arrays(writer, entries)
    writer.write_uint32_le(actor_id)
    return writer.finish()
