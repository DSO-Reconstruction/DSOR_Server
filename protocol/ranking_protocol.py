"""Client leaderboard queries and actor-scoped honor ranking responses.

Layouts are from the release261 client serializers at 0x140a13488..0x140a13c74.
List ranks are zero-based; local-player rank responses use one-based ranks.
"""

from __future__ import annotations

from dataclasses import dataclass

from dso_server.protocol.character_selection import BitReader, BitWriter
from dso_server.protocol.command_ids import command_id


HONOR_RANKING_INFO_COMMAND = command_id("Ranking::HonorRankingInfoCommand", 0xEB)
HONOR_RANKING_RESPONSE_COMMAND = command_id("Ranking::HonorRankingResponseCommand", 0xEC)
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

RANKING_REQUEST_COMMANDS = frozenset({
    HONOR_RANKING_INFO_COMMAND, HONOR_OVERALL_USER_RANKING_INFO_COMMAND,
    HONOR_LOCAL_USER_RANKING_INFO_COMMAND, HONOR_OVERALL_LOCAL_USER_RANKING_INFO_COMMAND,
})
FILTER_ALL = 0x01
FILTER_FRIENDS = 0x02
FILTER_GUILD = 0x04
FILTER_PLAYER_PAGE = 0x10
UNRANKED = 0xFFFFFFFF
MAX_RANKING_PAGE_SIZE = 10  # The client's table allocates exactly ten UI rows.


@dataclass(frozen=True)
class RankingRequest:
    command_id: int
    start: int = UNRANKED
    count: int = MAX_RANKING_PAGE_SIZE
    context: int = 0
    flags: int = FILTER_ALL
    class_id: int = 4
    board: int = 4
    sort_column: int = 0


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


def decode_ranking_request(payload: bytes, bit_length: int | None = None) -> RankingRequest:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x8B:
        raise ValueError("not a client ranking command")
    command = int.from_bytes(reader.read_bytes(2), "little")
    if command not in RANKING_REQUEST_COMMANDS:
        raise ValueError(f"unsupported ranking request 0x{command:04x}")
    if command == HONOR_OVERALL_LOCAL_USER_RANKING_INFO_COMMAND:
        value = RankingRequest(command, context=reader.read_uint32_le())
    elif command == HONOR_LOCAL_USER_RANKING_INFO_COMMAND:
        value = RankingRequest(command, context=reader.read_uint32_le(), board=reader.read_uint8())
    else:
        board = reader.read_uint8() if command == HONOR_RANKING_INFO_COMMAND else 4
        start, count, context = (reader.read_uint32_le() for _ in range(3))
        flags = int.from_bytes(reader.read_bytes(2), "little")
        class_id = reader.read_uint8()
        column = reader.read_uint8() if command == HONOR_RANKING_INFO_COMMAND else 0
        value = RankingRequest(command, start, count, context, flags, class_id, board, column)
        if value.class_id not in range(5):
            raise ValueError("invalid ranking class filter")
        if value.sort_column not in range(6):
            raise ValueError("invalid ranking sort column")
    if value.board not in (*range(5), 0xFF):
        raise ValueError("invalid honor ranking board")
    if reader.remaining:
        raise ValueError(f"ranking request has {reader.remaining} unread bits")
    return value


def _response_writer(command: int) -> BitWriter:
    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(command.to_bytes(2, "little"))
    return writer


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
