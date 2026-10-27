"""Leaderboard request/response layouts against the R261_1024 decoders.

Requests are built the way the client's generated encoders write them and
responses are read back field by field in the order of the client decoders
(``ranking_protocol`` module docstring lists the addresses), including the
trailing actor every Ranking/high-score response carries.
"""

import struct

import pytest

from dso_server.protocol.character_selection import BitReader
from dso_server.protocol.command_ids import UNAVAILABLE_COMMAND_ID
from dso_server.protocol import ranking_protocol as rp
from dso_server.protocol.ranking_protocol import (
    HighScoreEntry, ParallelWorldRow, ParallelWorldSummary, RankingRequest,
    RankingRow, decode_ranking_request,
)


def header(command):
    return b"\x8b" + struct.pack("<H", command)


def pw_list(*, start=0xFFFFFFFF, count=10, context=7, flags=0x11, board=3,
            class_id=4, sort=0, board_type=0):
    return (header(rp.PARALLEL_WORLD_USER_RANKING_INFO_COMMAND)
            + struct.pack("<IIIH", start, count, context, flags)
            + bytes((board, class_id, sort, board_type)))


def overall_list(*, start=0, count=10, context=9, flags=1, class_id=4, board_type=1):
    return (header(rp.OVERALL_USER_RANKING_INFO_COMMAND)
            + struct.pack("<IIIH", start, count, context, flags)
            + bytes((class_id, board_type)))


def all_data(class_id=4):
    return header(rp.GET_HIGH_SCORE_RANK_ALL_DATA_COMMAND) + struct.pack("<i", class_id)


def response_reader(packet, command):
    data, bits = packet
    reader = BitReader(data, bits)
    assert reader.read_uint8() == 0x84
    assert int.from_bytes(reader.read_bytes(2), "little") == command
    return reader


def finish(reader):
    assert reader.read_uint32_le() == 1  # Trailing actor (server data).
    assert reader.remaining == 0


def test_every_client_ranking_query_is_routed_and_updates_are_rejected():
    queries = {
        rp.HONOR_RANKING_INFO_COMMAND, rp.HONOR_OVERALL_USER_RANKING_INFO_COMMAND,
        rp.HONOR_LOCAL_USER_RANKING_INFO_COMMAND,
        rp.HONOR_OVERALL_LOCAL_USER_RANKING_INFO_COMMAND,
        rp.PARALLEL_WORLD_USER_RANKING_INFO_COMMAND,
        rp.PARALLEL_WORLD_LOCAL_USER_RANKING_INFO_COMMAND,
        rp.OVERALL_USER_RANKING_INFO_COMMAND, rp.OVERALL_LOCAL_USER_RANKING_INFO_COMMAND,
        rp.GET_HIGH_SCORE_RANK_ALL_DATA_COMMAND,
    }
    if rp.GET_HIGH_SCORE_RANK_GROUP_DATA_COMMAND != UNAVAILABLE_COMMAND_ID:
        queries.add(rp.GET_HIGH_SCORE_RANK_GROUP_DATA_COMMAND)
    assert queries <= rp.RANKING_REQUEST_COMMANDS
    assert UNAVAILABLE_COMMAND_ID not in rp.RANKING_REQUEST_COMMANDS
    for command in (rp.HONOR_RANKING_MATCH_UPDATE_COMMAND, rp.HONOR_RANKING_UPDATE_COMMAND,
                    rp.PARALLEL_WORLD_USER_RANKING_UPDATE_COMMAND):
        assert command in rp.RANKING_REQUEST_COMMANDS
        with pytest.raises(ValueError, match="server-authoritative"):
            decode_ranking_request(header(command) + bytes(60))


def test_parallel_world_list_request_fields_in_client_order():
    request = decode_ranking_request(pw_list(start=20, count=10, context=0x1234,
                                             flags=0x21, board=8, class_id=2, sort=3,
                                             board_type=1))
    assert request == RankingRequest(
        rp.PARALLEL_WORLD_USER_RANKING_INFO_COMMAND, 20, 10, 0x1234, 0x21, 2, 8, 3, 1)


def test_parallel_world_local_overall_and_high_score_requests():
    local = decode_ranking_request(
        header(rp.PARALLEL_WORLD_LOCAL_USER_RANKING_INFO_COMMAND)
        + struct.pack("<I", 77) + bytes((5, 1)))
    assert (local.context, local.board, local.board_type) == (77, 5, 1)
    overall = decode_ranking_request(overall_list(start=10, class_id=1, board_type=1))
    assert (overall.start, overall.class_id, overall.board_type, overall.flags) == (10, 1, 1, 1)
    overall_local = decode_ranking_request(
        header(rp.OVERALL_LOCAL_USER_RANKING_INFO_COMMAND) + struct.pack("<I", 3) + b"\x00")
    assert (overall_local.context, overall_local.board_type) == (3, 0)
    assert decode_ranking_request(all_data(2)).class_id == 2
    if rp.GET_HIGH_SCORE_RANK_GROUP_DATA_COMMAND != UNAVAILABLE_COMMAND_ID:
        group = decode_ranking_request(header(rp.GET_HIGH_SCORE_RANK_GROUP_DATA_COMMAND))
        assert group.command_id == rp.GET_HIGH_SCORE_RANK_GROUP_DATA_COMMAND


@pytest.mark.parametrize("packet", [
    pw_list(board=9),                 # enum8 -1..8
    pw_list(board_type=2),            # enum8 -1..1
    pw_list(board=0xFF),
    pw_list(class_id=5),
    pw_list(sort=4),
    pw_list()[:-1],
    pw_list() + b"\x00",
    overall_list(board_type=2),
    overall_list(class_id=9),
    all_data(-1),
    all_data(5),
    all_data()[:-1],
    header(rp.PARALLEL_WORLD_LOCAL_USER_RANKING_INFO_COMMAND) + struct.pack("<I", 0) + bytes((0, 2)),
])
def test_out_of_range_selectors_and_framing_are_rejected(packet):
    with pytest.raises(ValueError):
        decode_ranking_request(packet)


def test_parallel_world_page_matches_client_decoder():
    request = decode_ranking_request(pw_list(start=0, flags=0x21, board=4, class_id=1,
                                             sort=2, board_type=1))
    packet = rp.encode_parallel_world_page(
        request, (ParallelWorldRow("Alice", 0, (900, 100, 700, 100)),
                  ParallelWorldRow("Bob", 1, (500, 0, 450, 50))),
        start=0, total=2)
    r = response_reader(packet, rp.PARALLEL_WORLD_USER_RANKING_RESPONSE_COMMAND)
    assert (r.read_uint32_le(), r.read_uint32_le(), r.read_uint8()) == (2, 7, 0)
    assert r.read_uint32_le() == 2
    rows = [(r.read_string(), r.read_uint32_le(), tuple(r.read_uint32_le() for _ in range(4)))
            for _ in range(2)]
    assert rows == [("Alice", 0, (900, 100, 700, 100)), ("Bob", 1, (500, 0, 450, 50))]
    assert r.read_uint32_le() == 0                                  # start
    assert int.from_bytes(r.read_bytes(2), "little") == 0x21        # filter
    assert tuple(r.read_uint8() for _ in range(4)) == (4, 1, 2, 1)  # board, class, sort, type
    finish(r)


def test_parallel_world_summary_and_unranked_marker():
    request = decode_ranking_request(
        header(rp.PARALLEL_WORLD_LOCAL_USER_RANKING_INFO_COMMAND)
        + struct.pack("<I", 5) + bytes((2, 0)))
    r = response_reader(rp.encode_parallel_world_local(
        request, ParallelWorldSummary(123456, 754, 180, 1)),
        rp.PARALLEL_WORLD_LOCAL_USER_RANKING_RESPONSE_COMMAND)
    assert tuple(r.read_uint32_le() for _ in range(5)) == (5, 123456, 754, 180, 1)
    assert (r.read_uint8(), r.read_uint8(), r.read_uint8()) == (2, 0, 0)
    finish(r)
    r = response_reader(rp.encode_parallel_world_local(request, None),
                        rp.PARALLEL_WORLD_LOCAL_USER_RANKING_RESPONSE_COMMAND)
    assert tuple(r.read_uint32_le() for _ in range(5)) == (5, 0xFFFFFFFF, 0, 0, 0)


def test_overall_page_and_local_rank_match_client_decoders():
    request = decode_ranking_request(overall_list(start=10, flags=0x41, class_id=3, board_type=1))
    r = response_reader(rp.encode_overall_page(
        request, (RankingRow("Carol", 425, 10),), start=10, total=11),
        rp.OVERALL_USER_RANKING_RESPONSE_COMMAND)
    assert (r.read_uint32_le(), r.read_uint32_le(), r.read_uint8(), r.read_uint32_le()) == (11, 9, 0, 1)
    assert (r.read_string(), r.read_uint32_le(), r.read_uint32_le()) == ("Carol", 425, 10)
    assert r.read_uint32_le() == 10
    assert int.from_bytes(r.read_bytes(2), "little") == 0x41
    assert (r.read_uint8(), r.read_uint8()) == (3, 1)
    finish(r)
    local = decode_ranking_request(
        header(rp.OVERALL_LOCAL_USER_RANKING_INFO_COMMAND) + struct.pack("<I", 4) + b"\x01")
    r = response_reader(rp.encode_overall_local(local, 0),
                        rp.OVERALL_LOCAL_USER_RANKING_RESPONSE_COMMAND)
    assert (r.read_uint32_le(), r.read_uint32_le(), r.read_uint8(), r.read_uint8()) == (4, 1, 1, 0)
    finish(r)
    r = response_reader(rp.encode_overall_local(local, None),
                        rp.OVERALL_LOCAL_USER_RANKING_RESPONSE_COMMAND)
    assert r.read_uint32_le() == 4 and r.read_uint32_le() == 0xFFFFFFFF


def read_high_score_arrays(r):
    floors = [r.read_uint32_le() for _ in range(r.read_uint32_le())]
    times = [r.read_uint32_le() for _ in range(r.read_uint32_le())]
    names = [r.read_string() for _ in range(r.read_uint32_le())]
    return floors, times, names


def test_high_score_results_carry_parallel_arrays_and_echo_the_class():
    entries = (HighScoreEntry("Dora", 12, 95), HighScoreEntry("Emil", 12, 40),
               HighScoreEntry("Finn", 3, 410))
    r = response_reader(rp.encode_high_score_all_data(2, entries),
                        rp.GET_HIGH_SCORE_RANK_ALL_DATA_RESULT_COMMAND)
    assert r.read_uint32_le() == 2
    assert read_high_score_arrays(r) == ([12, 12, 3], [95, 40, 410], ["Dora", "Emil", "Finn"])
    finish(r)
    r = response_reader(rp.encode_high_score_group_data(entries[:1]),
                        rp.GET_HIGH_SCORE_RANK_GROUP_DATA_RESULT_COMMAND)
    assert read_high_score_arrays(r) == ([12], [95], ["Dora"])
    finish(r)
    r = response_reader(rp.encode_high_score_group_data(()),
                        rp.GET_HIGH_SCORE_RANK_GROUP_DATA_RESULT_COMMAND)
    assert read_high_score_arrays(r) == ([], [], [])


def test_encoders_refuse_mismatched_requests():
    honor = RankingRequest(rp.HONOR_OVERALL_USER_RANKING_INFO_COMMAND)
    with pytest.raises(ValueError):
        rp.encode_parallel_world_page(honor, (), start=0, total=0)
    with pytest.raises(ValueError):
        rp.encode_overall_page(honor, (), start=0, total=0)
    with pytest.raises(ValueError):
        rp.encode_parallel_world_local(honor, None)
    request = decode_ranking_request(pw_list())
    with pytest.raises(ValueError):
        rp.encode_parallel_world_page(
            request, tuple(ParallelWorldRow("x", i, (0, 0, 0, 0)) for i in range(11)),
            start=0, total=11)
