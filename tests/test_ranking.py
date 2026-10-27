"""Ranking wire fixtures and requests through the composed gameplay dispatcher."""

import struct
from types import SimpleNamespace

import pytest

from dso_server.persistence.account_state import (
    AccountState, AccountStateStore, HonorRankingRecord,
    _state_from_document, _state_to_document,
)
from dso_server.protocol.character_selection import BitReader
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.player_init_protocol import CurrencyChanged, QuickbarInfo
from dso_server.protocol.ranking_protocol import (
    RankingRequest, RankingRow, decode_ranking_request,
    encode_local_ranking, encode_ranking_page,
    HONOR_OVERALL_USER_RANKING_INFO_COMMAND as LIST,
    HONOR_OVERALL_LOCAL_USER_RANKING_INFO_COMMAND as LOCAL,
    HONOR_RANKING_INFO_COMMAND as DETAIL,
    HONOR_LOCAL_USER_RANKING_INFO_COMMAND as LOCAL_DETAIL,
)
from dso_server.services.character_server import CharacterServer
from dso_server.tests.fixtures.characters import character_profile
from dso_server.tests.test_pvp_matchmaking import _session


RECORDED_LIST = bytes.fromhex("8bf000ffffffff0a00000000000000110000")
RECORDED_LOCAL = bytes.fromhex("8bf40000000000")


def request_bytes(command=LIST, *, start=0, count=10, flags=1, class_id=4, board=4, column=0):
    data = b"\x8b" + struct.pack("<H", command)
    if command == DETAIL:
        data += bytes([board])
    data += struct.pack("<IIIHB", start, count, 0, flags, class_id)
    if command == DETAIL:
        data += bytes([column])
    return data


def read_page(packet):
    """Independent reading of the native 0xf1 / 0xec layout."""
    data, bits, _ = packet
    r = BitReader(data, bits)
    assert r.read_uint8() == 0x84
    command = int.from_bytes(r.read_bytes(2), "little")
    detailed = command == 0xEC
    if detailed:
        board = r.read_uint8()
    total, context, status, count = r.read_uint32_le(), r.read_uint32_le(), r.read_uint8(), r.read_uint32_le()
    rows = []
    for _ in range(count):
        name = r.read_string()
        if detailed:
            rank = r.read_uint32_le()
            values = tuple(r.read_uint32_le() for _ in range(6))
            rows.append((name, values, rank))
        else:
            rows.append((name, r.read_uint32_le(), r.read_uint32_le()))
    start = r.read_uint32_le()
    if detailed:
        class_id, column = r.read_uint8(), r.read_uint8()
        flags = int.from_bytes(r.read_bytes(2), "little")
    else:
        flags = int.from_bytes(r.read_bytes(2), "little")
        class_id = r.read_uint8()
    assert r.read_uint32_le() == 1
    assert r.remaining == 0
    assert context == status == 0
    return SimpleNamespace(total=total, start=start, rows=rows, class_id=class_id, flags=flags)


@pytest.fixture
def board(tmp_path):
    store = AccountStateStore(tmp_path / "players.sqlite3")
    server = CharacterServer.__new__(CharacterServer)
    server.account_store = store
    server.social_store = SimpleNamespace(guild_for=lambda _: None, buddies_for=lambda _: ())
    session = _session(50990, 50, "Online")
    session.gameplay_character.class_id = 0
    session.gameplay_honor_points = 75
    server._active_gameplay_sessions = lambda: (session,)
    def dispatch(payload):
        session.current_message_bit_length = len(payload) * 8
        server._on_char_op(session, payload)
        return session.recorder.messages[-1]
    yield server, session, store, dispatch
    store.close()


def state(identity, **values):
    return AccountState(
        character_id=identity, map_id="a0200_kingscity", position=None,
        inventory=InventoryInfo(), currency=CurrencyChanged(0, 0, (0, 0, 0), 0, False), currency_ic=0,
        next_item_id=1, quickbar_info=QuickbarInfo(0, 0), timed_offer_purchases={},
        status_effects=(), mount_ride=None, **values,
    )


def add(store, identity, score, *, name=None, class_id=0, **values):
    store.put_profile(character_profile(identity, name or f"Player{identity}", class_id=class_id))
    store.put(state(identity, honor_points=score, **values))


def test_recorded_client_requests():
    assert decode_ranking_request(RECORDED_LIST) == RankingRequest(
        LIST, start=0xFFFFFFFF, count=10, flags=0x11, class_id=0)
    assert decode_ranking_request(RECORDED_LOCAL).command_id == LOCAL


@pytest.mark.parametrize("packet", [RECORDED_LIST[:-1], RECORDED_LOCAL + b"\0", b"\x84" + RECORDED_LIST[1:], b"\x8b\xed\x00"])
def test_rejects_truncated_trailing_and_nonquery_commands(packet):
    with pytest.raises(ValueError):
        decode_ranking_request(packet)


@pytest.mark.parametrize("class_id", range(5))
def test_all_native_class_selectors_are_accepted(class_id):
    assert decode_ranking_request(request_bytes(class_id=class_id)).class_id == class_id


def test_native_overall_response_field_order():
    query = RankingRequest(LIST, class_id=0)
    row = RankingRow("Alice", 1234, 9)
    packet, bits = encode_ranking_page(query, (row,), start=9, total=20)
    # Native decoder reads total/context/status, array count, name/score/rank,
    # then start/filter/class and the additional server actor id.
    expected = (bytes.fromhex("84f100") + struct.pack("<IIBIH", 20, 0, 0, 1, 5)
                + b"Alice" + struct.pack("<IIIHBI", 1234, 9, 9, 1, 0, 1))
    assert packet == expected
    assert bits == len(expected) * 8


def test_local_rank_is_one_based_and_missing_rank_is_minus_one():
    query = RankingRequest(LOCAL)
    packet, bits = encode_local_ranking(query, RankingRow("A", 123, 0))
    assert packet == bytes.fromhex("84f50000000000010000000001000000")
    assert bits == 128
    packet, _ = encode_local_ranking(query, None)
    assert packet[7:11] == b"\xff" * 4


def test_local_detail_has_one_bit_flag_and_unaligned_actor():
    query = RankingRequest(LOCAL_DETAIL, context=7)
    row = RankingRow("A", 99, 2, wins=3, losses=4, draws=5, kills=6, deaths=7)
    packet, bits = encode_local_ranking(query, row)
    assert bits == 321
    r = BitReader(packet, bits)
    assert r.read_bytes(3) == bytes.fromhex("84f300")
    assert tuple(r.read_uint32_le() for _ in range(8)) == (7, 3, 99, 6, 7, 3, 4, 5)
    assert r.read_uint8() == 4
    assert r.read_bool()
    assert r.read_uint32_le() == 1
    assert r.remaining == 0


def test_recorded_dispatch_includes_offline_scores_and_live_overlay(board):
    server, session, store, dispatch = board
    add(store, 50, 1, name="OldName")
    add(store, 2, 100, name="Offline")
    add(store, 3, 999, name="OtherClass", class_id=1)
    result = read_page(dispatch(RECORDED_LIST))
    assert result.rows == [("Offline", 100, 0), ("Online", 75, 1)]
    assert (result.total, result.start, result.class_id) == (2, 0, 0)
    assert store.get(50).honor_points == 1  # A read never updates player state.
    local, bits, _ = dispatch(RECORDED_LOCAL)
    assert struct.unpack_from("<I", local, 7)[0] == 2


def test_ties_are_stable_and_all_classes_filter_is_supported(board):
    _, session, store, dispatch = board
    add(store, 1, 75, name="First", class_id=3)
    add(store, 2, 75, name="Second", class_id=1)
    result = read_page(dispatch(request_bytes()))
    assert [row[0] for row in result.rows] == ["First", "Second", "Online"]


def test_player_centered_paging_and_bounded_page_size(board):
    _, session, store, dispatch = board
    for i in range(30):
        add(store, 100 + i, 200 - i)
    session.gameplay_honor_points = 181
    result = read_page(dispatch(RECORDED_LIST))
    assert len(result.rows) == 10
    assert any(row[0] == "Online" for row in result.rows)
    assert result.start > 0
    result = read_page(dispatch(request_bytes(count=0xFFFFFFFF)))
    assert len(result.rows) == 10
    assert result.start == 0
    result = read_page(dispatch(request_bytes(start=10)))
    assert result.start == 10
    assert [row[2] for row in result.rows] == list(range(10, 20))
    result = read_page(dispatch(request_bytes(start=1000)))
    assert result.rows == []
    assert result.total == 31


def test_guild_and_friend_filters_do_not_return_unrelated_players(board):
    server, _, store, dispatch = board
    add(store, 1, 100)
    add(store, 2, 90)
    server.social_store.guild_for = lambda _: SimpleNamespace(
        members=[SimpleNamespace(character_id=i) for i in (1, 50)])
    assert [row[0] for row in read_page(dispatch(request_bytes(flags=4))).rows] == ["Player1", "Online"]
    assert [row[0] for row in read_page(dispatch(request_bytes(flags=2))).rows] == ["Online"]
    server.social_store.guild_for = lambda _: None
    assert read_page(dispatch(request_bytes(flags=4))).rows == []


def test_alltime_detail_includes_saved_results_and_sorts_columns(board):
    _, session, store, dispatch = board
    add(store, 1, 30, pvp_matches_won=7, pvp_matches_lost=2, pvp_kills=13, pvp_deaths=4)
    result = read_page(dispatch(request_bytes(DETAIL, column=1)))
    assert result.rows[0] == ("Player1", (30, 7, 2, 0, 13, 4), 0)
    assert result.rows[1][0] == "Online"


def test_periods_without_activity_return_empty_boards(board):
    # Daily honor starts from the totals at the period start (see
    # test_ranking_boards.py); nobody has gained honor yet.
    _, _, _, dispatch = board
    assert read_page(dispatch(request_bytes(DETAIL, board=2))).rows == []
    packet, bits, _ = dispatch(bytes.fromhex("8bf2000000000002"))
    r = BitReader(packet, bits)
    r.read_bytes(3)
    assert r.read_uint32_le() == 0
    assert r.read_uint32_le() == 0xFFFFFFFF


def test_malformed_query_sends_nothing(board):
    server, session, _, _ = board
    session.current_message_bit_length = (len(RECORDED_LIST) - 1) * 8
    server._on_char_op(session, RECORDED_LIST[:-1])
    assert session.recorder.messages == []


def test_pages_share_snapshot_and_allocate_only_requested_rows(board, monkeypatch):
    from dso_server.services import ranking
    server, _, store, dispatch = board
    for identity in range(100, 200):
        add(store, identity, identity)
    scans, encoded = [], []
    load = server._honor_ranking_records
    encode = ranking._wire_row
    monkeypatch.setattr(server, "_honor_ranking_records", lambda s: (scans.append(1), load(s))[1])
    monkeypatch.setattr(ranking, "_wire_row", lambda row, rank: (encoded.append(rank), encode(row, rank))[1])
    read_page(dispatch(request_bytes(count=10)))
    read_page(dispatch(request_bytes(start=10, count=10)))
    assert len(scans) == 1
    assert encoded == list(range(20))


def test_snapshot_refreshes_live_scores_after_expiry(board, monkeypatch):
    from dso_server.services import ranking
    _, session, _, dispatch = board
    now = [100.0]
    monkeypatch.setattr(ranking.time, "monotonic", lambda: now[0])
    assert read_page(dispatch(request_bytes())).rows[0][1] == 75
    session.gameplay_honor_points = 123
    now[0] += ranking.RANKING_SNAPSHOT_SECONDS + 0.01
    assert read_page(dispatch(request_bytes())).rows[0][1] == 123


def test_unknown_board_does_not_scan_characters(board, monkeypatch):
    server, _, _, dispatch = board
    def unexpected(_session):
        raise AssertionError("an unknown board must not read scores")
    monkeypatch.setattr(server, "_honor_ranking_records", unexpected)
    assert read_page(dispatch(request_bytes(DETAIL, board=0xFF))).rows == []


def test_pvp_scores_survive_database_reload_and_missing_fields_default(tmp_path):
    path = tmp_path / "players.sqlite3"
    store = AccountStateStore(path)
    try:
        add(store, 1, 700, pvp_matches_won=6, pvp_matches_lost=2, pvp_kills=14, pvp_deaths=5)
        add(store, 2, 300)
        store.flush()
    finally:
        store.close()
    reloaded = AccountStateStore(path)
    try:
        assert reloaded.honor_ranking_records() == (
            HonorRankingRecord(1, "Player1", 0, 700, 6, 2, 14, 5),
            HonorRankingRecord(2, "Player2", 0, 300, 0, 0, 0, 0),
        )
        session = _session(50991, 1, "Player1")
        CharacterServer._restore_pvp_career(session, reloaded.get(1))
        assert (session.gameplay_pvp_kills, session.gameplay_pvp_deaths) == (14, 5)
        old = _state_to_document(reloaded.get(1))
        old.pop("pvp_kills")
        old.pop("pvp_deaths")
        legacy = _state_from_document(old)
        assert legacy.honor_points == 700
        assert legacy.pvp_kills == legacy.pvp_deaths == 0
    finally:
        reloaded.close()


POSITION_TELEMETRY = bytes.fromhex(
    "8be90005c2eb0b00806f7610e0793fdaf8933b542c483b7fdfca3d21006542837041c2"
    "000000005c5ba63300000000")


def test_recorded_position_telemetry_is_diagnostic_only(board):
    from dso_server.protocol.gameplay_protocol import ClientPositionFrameInfo
    server, session, _, _ = board
    before = session.gameplay_honor_points
    position = session.gameplay_last_move
    session.current_message_bit_length = len(POSITION_TELEMETRY) * 8
    server._on_char_op(session, POSITION_TELEMETRY)
    value = session.gameplay_client_command_state[0xE9]
    assert isinstance(value, ClientPositionFrameInfo)
    assert value.character_id == 200000005
    assert value.samples == ()
    assert value.field_60 == 866540380
    assert session.gameplay_last_move is position
    assert session.gameplay_honor_points == before
    assert session.recorder.messages == []


def test_position_telemetry_validates_sample_counts_and_lengths():
    from dso_server.protocol.gameplay_protocol import decode_client_position_frame_info
    for payload in (POSITION_TELEMETRY[:-1], POSITION_TELEMETRY + b"\0",
                    POSITION_TELEMETRY[:35] + struct.pack("<I", 0xFFFFFFFF) + POSITION_TELEMETRY[39:]):
        with pytest.raises(ValueError):
            decode_client_position_frame_info(payload)
    packet = POSITION_TELEMETRY[:35] + struct.pack("<I6I", 1, 1, 2, 3, 4, 5, 6) + POSITION_TELEMETRY[39:]
    assert decode_client_position_frame_info(packet).samples == ((1, 2, 3, 4, 5, 6),)
