"""Board contents for every leaderboard selector, through the dispatcher."""

import inspect
import struct
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from dso_server.gameplay.leaderboards import (
    load_leaderboard_catalog, score_parallel_world_run,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.character_selection import BitReader
from dso_server.protocol.command_ids import UNAVAILABLE_COMMAND_ID
from dso_server.protocol import ranking_protocol as rp
from dso_server.protocol.pvp_match_protocol import (
    MATCH_MODE_BEST_OF_3_1V1, MATCH_MODE_CAPTURE_THE_FLAG_5V5,
)
from dso_server.services import ranking
from dso_server.services.battleground import BattlegroundService
from dso_server.services.character_server import CharacterServer
from dso_server.services.map_events import MapEventService
from dso_server.tests.test_ranking import add, board, read_page, request_bytes, DETAIL


CATALOG = load_leaderboard_catalog(DEFAULT_STATIC_DB_PATH)


class FakeEvent:
    """A scheduled event instance as ``EventCatalog.events`` exposes it."""

    def __init__(self, event_id, template, starts_at, ends_at):
        self.event_id = event_id
        self.template = SimpleNamespace(resource_id=template)
        self.starts_at, self.ends_at = starts_at, ends_at

    def state_at(self, now):
        if now < self.starts_at:
            return "upcoming"
        return "expired" if now >= self.ends_at else "active"


def season(server, *events):
    server._ranking_event_catalog = lambda: SimpleNamespace(events=tuple(events))


@pytest.fixture(autouse=True)
def fresh_snapshots(monkeypatch):
    monkeypatch.setattr(ranking, "RANKING_SNAPSHOT_SECONDS", 0.0)


def header(command):
    return b"\x8b" + struct.pack("<H", command)


def pw_list(board_id=0, board_type=0, *, class_id=4, sort=0, flags=1, start=0):
    return (header(rp.PARALLEL_WORLD_USER_RANKING_INFO_COMMAND)
            + struct.pack("<IIIH", start, 10, 0, flags) + bytes((board_id, class_id, sort, board_type)))


def pw_local(board_id=0, board_type=0):
    return header(rp.PARALLEL_WORLD_LOCAL_USER_RANKING_INFO_COMMAND) + struct.pack("<I", 0) + bytes((board_id, board_type))


def overall_list(board_type=0, class_id=4):
    return (header(rp.OVERALL_USER_RANKING_INFO_COMMAND)
            + struct.pack("<IIIH", 0, 10, 0, 1) + bytes((class_id, board_type)))


def overall_local(board_type=0):
    return header(rp.OVERALL_LOCAL_USER_RANKING_INFO_COMMAND) + struct.pack("<I", 0) + bytes((board_type,))


def read_pw_page(packet):
    data, bits, _ = packet
    r = BitReader(data, bits)
    r.read_bytes(3)
    total, _context, _status, count = r.read_uint32_le(), r.read_uint32_le(), r.read_uint8(), r.read_uint32_le()
    rows = [(r.read_string(), r.read_uint32_le(), tuple(r.read_uint32_le() for _ in range(4)))
            for _ in range(count)]
    return total, rows


def read_pw_local(packet):
    data, bits, _ = packet
    r = BitReader(data, bits)
    r.read_bytes(3)
    return tuple(r.read_uint32_le() for _ in range(5))[1:]


def read_overall_page(packet):
    data, bits, _ = packet
    r = BitReader(data, bits)
    r.read_bytes(3)
    total, _context, _status, count = r.read_uint32_le(), r.read_uint32_le(), r.read_uint8(), r.read_uint32_le()
    return total, [(r.read_string(), r.read_uint32_le(), r.read_uint32_le()) for _ in range(count)]


def read_local_rank(packet):
    data, _bits, _ = packet
    return struct.unpack_from("<I", data, 7)[0]


def read_high_score(packet, *, all_data):
    data, bits, _ = packet
    r = BitReader(data, bits)
    r.read_bytes(3)
    class_id = r.read_uint32_le() if all_data else None
    floors = [r.read_uint32_le() for _ in range(r.read_uint32_le())]
    times = [r.read_uint32_le() for _ in range(r.read_uint32_le())]
    names = [r.read_string() for _ in range(r.read_uint32_le())]
    assert r.read_uint32_le() == 1 and r.remaining == 0
    return class_id, list(zip(names, floors, times))


def honor_rows(packet):
    return [(name, values) for name, values, _rank in read_page(packet).rows]


# ---------------------------------------------------------------------------
# Honor boards
# ---------------------------------------------------------------------------

def test_daily_and_weekly_boards_rank_honor_gained_since_the_period_opened(board):
    server, session, store, dispatch = board
    add(store, 1, 100, pvp_matches_won=4)
    add(store, 2, 50)
    assert read_page(dispatch(request_bytes(DETAIL, board=2))).rows == []
    assert read_page(dispatch(request_bytes(DETAIL, board=3))).rows == []
    add(store, 1, 130, pvp_matches_won=5, pvp_kills=2)   # +30 honor, +1 win, +2 kills
    session.gameplay_honor_points = 95                    # +20 live, unsaved
    for board_id in (2, 3):
        assert honor_rows(dispatch(request_bytes(DETAIL, board=board_id))) == [
            ("Player1", (30, 1, 0, 0, 2, 0)), ("Online", (20, 0, 0, 0, 0, 0))]
    # The all-time board keeps career totals.
    assert [row[1][0] for row in read_page(dispatch(request_bytes(DETAIL, board=4))).rows] == [130, 95, 50]
    local = dispatch(bytes.fromhex("8bf2000000000002"))
    assert struct.unpack_from("<II", local[0], 3) == (0, 2)


def test_daily_board_starts_from_the_new_period_baseline(board, monkeypatch):
    server, session, store, dispatch = board
    day = datetime(2026, 10, 26, 22, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return day

    monkeypatch.setattr(ranking, "datetime", Clock)
    dispatch(request_bytes(DETAIL, board=2))
    session.gameplay_honor_points = 90
    assert honor_rows(dispatch(request_bytes(DETAIL, board=2))) == [("Online", (15, 0, 0, 0, 0, 0))]
    day += timedelta(hours=3)                             # Next UTC day.
    assert read_page(dispatch(request_bytes(DETAIL, board=2))).rows == []
    session.gameplay_honor_points = 100
    assert honor_rows(dispatch(request_bytes(DETAIL, board=2))) == [("Online", (10, 0, 0, 0, 0, 0))]


def test_solo_and_team_boards_accumulate_settled_matches_by_mode(board):
    server, session, store, dispatch = board
    settle = server._ranking_on_pvp_match_settled
    settle(session, match_mode=MATCH_MODE_BEST_OF_3_1V1, honor=30, outcome="won", kills=3, deaths=1)
    settle(session, match_mode=MATCH_MODE_BEST_OF_3_1V1, honor=10, outcome="lost", kills=1, deaths=2)
    settle(session, match_mode=MATCH_MODE_CAPTURE_THE_FLAG_5V5, honor=25, outcome="draw", kills=4, deaths=0)
    assert honor_rows(dispatch(request_bytes(DETAIL, board=0))) == [("Online", (40, 1, 1, 0, 4, 3))]
    assert honor_rows(dispatch(request_bytes(DETAIL, board=1))) == [("Online", (25, 0, 0, 1, 4, 0))]
    local = dispatch(bytes.fromhex("8bf2000000000001"))
    assert struct.unpack_from("<8I", local[0], 3)[1:] == (1, 25, 4, 0, 0, 0, 1)


def test_pvp_season_overall_shows_season_honor_and_career_without_a_season(board):
    server, session, store, dispatch = board
    add(store, 1, 500)
    overall = request_bytes(command=rp.HONOR_OVERALL_USER_RANKING_INFO_COMMAND)
    assert [row[1] for row in read_page(dispatch(overall)).rows] == [500, 75]
    now = datetime.now(timezone.utc)
    season(server, FakeEvent(901, "pvp_seasons_event", now - timedelta(days=1), now + timedelta(days=1)))
    assert read_page(dispatch(overall)).rows == []        # Season opened at current totals.
    session.gameplay_honor_points = 140
    assert read_page(dispatch(overall)).rows == [("Online", 65, 0)]
    local = dispatch(bytes.fromhex("8bf40000000000"))
    assert struct.unpack_from("<I", local[0], 7)[0] == 1


def test_battleground_settlement_feeds_the_mode_boards():
    source = inspect.getsource(BattlegroundService._finish_match)
    assert "_ranking_on_pvp_match_settled" in source


# ---------------------------------------------------------------------------
# Parallel Worlds
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


def enter(server, session, map_id):
    session.gameplay_map_event_runtime = SimpleNamespace(map_id=map_id)
    server._ranking_on_map_entered(session)


def kill(server, session, template, count=1):
    server._ranking_on_monsters_killed(
        session, [SimpleNamespace(template_id=template)] * count)


@pytest.fixture
def pw(board, monkeypatch):
    server, session, store, dispatch = board
    clock = Clock()
    monkeypatch.setattr(ranking.time, "monotonic", clock)
    session.gameplay_map_difficulty = 5
    server._map_event_sessions = lambda runtime: (session,)
    return server, session, store, dispatch, clock


def run_grimmag(server, session, clock, *, seconds=600, trash=(100, 60, 0)):
    enter(server, session, "pw001_grimmagstone_01_dun")
    kill(server, session, "pw001_mob", trash[0])
    clock.value += seconds / 3
    enter(server, session, "pw002_alliance_fortress_01_dun")
    kill(server, session, "pw002_mob", trash[1])
    clock.value += seconds / 3
    enter(server, session, "pw003_grimmag_01_boss")
    clock.value += seconds / 3
    kill(server, session, "boss_grimmag")


GRIMMAG = CATALOG.parallel_world(0)


def grimmag_score(map_seconds, kills, boss_killed):
    return score_parallel_world_run(CATALOG, GRIMMAG, difficulty=5, map_seconds=map_seconds,
                                    kills=kills, boss_killed=boss_killed)


def best(server, board="pw.0.0"):
    return server._ranking_store().scores(board, "all").get(50)


def test_parallel_world_run_is_scored_from_first_map_to_the_authored_boss(pw):
    server, session, _store, dispatch, clock = pw
    run_grimmag(server, session, clock)
    expected = grimmag_score((200, 200, 200), 161, True)
    assert expected.completed and expected.maps_completed == 3
    assert read_pw_page(dispatch(pw_list(0, 0))) == (1, [("Online", 0, expected.row_stats)])
    assert read_pw_local(dispatch(pw_local(0, 0))) == (expected.total, 600, 161, 1)
    assert read_pw_page(dispatch(pw_list(0, 1)))[1] == []          # Solo run only.
    assert read_pw_local(dispatch(pw_local(1, 0)))[0] == 0xFFFFFFFF
    # A slower repeat does not replace the best run; a faster one does.
    run_grimmag(server, session, clock, seconds=3600)
    assert read_pw_local(dispatch(pw_local(0, 0))) == (expected.total, 600, 161, 1)
    run_grimmag(server, session, clock, seconds=300)
    faster = grimmag_score((100, 100, 100), 161, True)
    assert faster.total > expected.total
    assert read_pw_local(dispatch(pw_local(0, 0))) == (faster.total, 300, 161, 1)


def test_partial_run_keeps_completed_maps_until_the_full_run_supersedes_it(pw):
    server, session, _store, dispatch, clock = pw
    enter(server, session, "pw001_grimmagstone_01_dun")
    kill(server, session, "pw001_mob", 50)
    clock.value += 200
    enter(server, session, "pw002_alliance_fortress_01_dun")      # Map 1 completed.
    one_map = grimmag_score((200,), 50, False)
    assert best(server).score == one_map.total
    assert read_pw_local(dispatch(pw_local(0, 0))) == (one_map.total, 200, 50, 0)
    kill(server, session, "pw002_mob", 40)
    clock.value += 300
    enter(server, session, "pw003_grimmag_01_boss")              # Map 2 completed.
    two_maps = grimmag_score((200, 300), 90, False)
    assert two_maps.total > one_map.total and best(server).score == two_maps.total
    clock.value += 100
    enter(server, session, "a0200_kingscity")                    # Left: run over.
    enter(server, session, "pw003_grimmag_01_boss")
    kill(server, session, "boss_grimmag")                        # Not a run any more.
    assert best(server).score == two_maps.total
    assert best(server).stats[8] == 2                            # Maps completed.
    # A finished chain, even a slow one, outranks the partial result.
    run_grimmag(server, session, clock, seconds=6000, trash=(0, 0, 0))
    full = grimmag_score((2000, 2000, 2000), 1, True)
    assert full.total > two_maps.total and best(server).score == full.total


def test_run_end_rules_relog_mid_structure_and_restart(pw):
    server, session, _store, dispatch, clock = pw
    enter(server, session, "pw002_alliance_fortress_01_dun")      # No measured start.
    enter(server, session, "pw003_grimmag_01_boss")
    kill(server, session, "boss_grimmag")
    assert best(server) is None
    # Relog into the current map ends the run; its completed map stands.
    enter(server, session, "pw001_grimmagstone_01_dun")
    clock.value += 100
    enter(server, session, "pw002_alliance_fortress_01_dun")
    enter(server, session, "pw002_alliance_fortress_01_dun")      # Reconnect.
    enter(server, session, "pw003_grimmag_01_boss")
    kill(server, session, "boss_grimmag")
    assert best(server).stats[8] == 1
    # The boss counts only on the final map of the running structure.
    enter(server, session, "pw001_grimmagstone_01_dun")
    kill(server, session, "boss_grimmag")
    assert best(server).stats[8] == 1
    # A restart drops the unfinished map: nothing is recorded for it, and
    # the next arrival cannot resume the lost run.
    enter(server, session, "pw004_hagastove_grotto_01_dun")
    kill(server, session, "pw004_mob", 30)
    server.__dict__.pop("_ranking_pw_runs")
    clock.value += 50
    enter(server, session, "pw005_caverns_loxley_01_dun")
    assert best(server, "pw.1.0") is None


def test_completed_chain_always_outranks_any_partial_run():
    for board in CATALOG.parallel_worlds:
        maps = board.final_order
        for difficulty in range(11):
            score = lambda seconds, kills, boss: score_parallel_world_run(
                CATALOG, board, difficulty=difficulty, map_seconds=seconds,
                kills=kills, boss_killed=boss)
            best_partial = score((1,) * (maps - 1), 10_000, False)
            worst_full = score((100_000,) * maps, 0, True)
            assert worst_full.total > best_partial.total > 0
            # Speed drives the score: halving the run time is worth more
            # than doubling the kills of an equally paced run.
            slow = score((900,) * maps, board.monster_requirement // 2, True)
            assert (score((450,) * maps, board.monster_requirement // 2, True).total - slow.total
                    > score((900,) * maps, board.monster_requirement, True).total - slow.total)
            # The boss alone does not finish a run with maps missing.
            assert not score((60,) * (maps - 1), 0, True).completed


def test_a_shared_instance_records_both_players_on_the_group_board(pw):
    from dso_server.tests.test_pvp_matchmaking import _session
    server, session, store, dispatch, clock = pw
    friend = _session(50991, 51, "Friend")
    friend.gameplay_character.class_id = 2
    friend.gameplay_map_difficulty = 5
    server._active_gameplay_sessions = lambda: (session, friend)
    server._map_event_sessions = lambda runtime: (session, friend)
    for map_id in ("pw001_grimmagstone_01_dun", "pw002_alliance_fortress_01_dun",
                   "pw003_grimmag_01_boss"):
        enter(server, session, map_id)
        enter(server, friend, map_id)
        clock.value += 100
    kill(server, friend, "pw003_mob", 5)
    kill(server, session, "boss_grimmag")
    total, rows = read_pw_page(dispatch(pw_list(0, 1)))
    assert total == 2 and {row[0] for row in rows} == {"Online", "Friend"}
    assert read_pw_page(dispatch(pw_list(0, 1, class_id=2)))[1][0][0] == "Friend"
    assert read_pw_page(dispatch(pw_list(0, 0)))[1] == []


def test_parallel_world_sort_columns_and_player_page(pw):
    server, session, store, dispatch, _clock = pw
    for identity, stats in ((1, (900, 100, 700, 100)), (2, (800, 300, 400, 100)),
                            (3, (700, 0, 650, 50))):
        add(store, identity, 0)
        store_ = server._ranking_store()
        store_.improve_score("pw.4.0", "all", identity, score=stats[0], stats=(*stats, 60, 10, 1, 5, 3))
    assert [row[0] for row in read_pw_page(dispatch(pw_list(4)))[1]] == ["Player1", "Player2", "Player3"]
    assert [row[0] for row in read_pw_page(dispatch(pw_list(4, sort=1)))[1]] == ["Player2", "Player1", "Player3"]
    assert [row[0] for row in read_pw_page(dispatch(pw_list(4, sort=2)))[1]] == ["Player1", "Player3", "Player2"]
    assert [row[1] for row in read_pw_page(dispatch(pw_list(4, start=1)))[1]] == [1, 2]


def test_overall_pve_rank_sums_authored_points_per_board_place(pw):
    server, session, store, dispatch, _clock = pw
    scores = server._ranking_store()
    for identity in (1, 2, 3):
        add(store, identity, 0, class_id=identity % 2)
    # Board 0: 1 > 2 > 3; board 1: only 3; the group board does not count
    # for the solo overall.
    for identity, value in ((1, 900), (2, 800), (3, 700)):
        scores.improve_score("pw.0.0", "all", identity, score=value, stats=(value, 0, 0, 0, 1, 1, 1, 5))
    scores.improve_score("pw.1.0", "all", 3, score=999, stats=(999, 0, 0, 0, 1, 1, 1, 5))
    scores.improve_score("pw.2.1", "all", 1, score=5, stats=(5, 0, 0, 0, 1, 1, 1, 5))
    points = CATALOG.overall_points_for_place
    assert (points(1), points(2), points(3)) == (150, 125, 100)   # Authored table.
    assert read_overall_page(dispatch(overall_list(0))) == (3, [
        ("Player3", points(3) + points(1), 0),
        ("Player1", points(1), 1),
        ("Player2", points(2), 2),
    ])
    assert read_overall_page(dispatch(overall_list(1))) == (1, [("Player1", points(1), 0)])
    assert read_local_rank(dispatch(overall_local(0))) == 0xFFFFFFFF
    assert [row[0] for row in read_overall_page(dispatch(overall_list(0, class_id=1)))[1]] == ["Player3", "Player1"]


def test_pve_season_boards_replace_the_all_time_view_while_active(pw):
    server, session, store, dispatch, clock = pw
    run_grimmag(server, session, clock)
    now = datetime.now(timezone.utc)
    season(server, FakeEvent(41, "pve_season_event", now - timedelta(hours=1), now + timedelta(hours=1)))
    assert read_pw_page(dispatch(pw_list(0)))[1] == []
    run_grimmag(server, session, clock, seconds=900)
    assert read_pw_local(dispatch(pw_local(0)))[1] == 900
    season(server)                                    # Season over: all-time again.
    assert read_pw_local(dispatch(pw_local(0)))[1] == 600


def test_map_event_transitions_call_the_ranking_hooks():
    assert "_ranking_on_map_entered" in inspect.getsource(MapEventService._on_map_entered)
    assert "_ranking_on_monsters_killed" in inspect.getsource(MapEventService._on_monsters_killed)
    assert "_ranking_record_fractured_planes_floor" in inspect.getsource(
        MapEventService._complete_fractured_planes_floor)


# ---------------------------------------------------------------------------
# Fractured Planes high-score lists
# ---------------------------------------------------------------------------

def test_high_score_lists_rank_floor_then_remaining_time(board, monkeypatch):
    server, session, store, dispatch = board
    monkeypatch.setattr(ranking.time, "perf_counter", lambda: 100.0)
    scores = server._ranking_store()
    for identity, class_id, floor, remaining in ((1, 1, 12, 40), (2, 2, 12, 95), (3, 1, 30, 5),
                                                  (4, 1, 0, 0)):
        add(store, identity, 0, class_id=class_id)
        scores.improve_score("fp", "all", identity, score=floor, tiebreak=remaining,
                             stats=(floor, remaining))
    server._ranking_record_fractured_planes_floor(session, 12, 140.4)   # 41 s left.
    server._ranking_record_fractured_planes_floor(session, 11, 400.0)   # Worse floor: ignored.
    command = rp.GET_HIGH_SCORE_RANK_ALL_DATA_COMMAND
    session.current_message_bit_length = 56
    class_id, rows = read_high_score(dispatch(header(command) + struct.pack("<i", 4)), all_data=True)
    assert class_id == 4
    assert rows == [("Player3", 30, 5), ("Player2", 12, 95), ("Online", 12, 41), ("Player1", 12, 40)]
    class_id, rows = read_high_score(dispatch(header(command) + struct.pack("<i", 1)), all_data=True)
    assert (class_id, [row[0] for row in rows]) == (1, ["Player3", "Player1"])
    if rp.GET_HIGH_SCORE_RANK_GROUP_DATA_COMMAND != UNAVAILABLE_COMMAND_ID:
        _none, rows = read_high_score(
            dispatch(header(rp.GET_HIGH_SCORE_RANK_GROUP_DATA_COMMAND)), all_data=False)
        assert [row[0] for row in rows] == ["Player3", "Player2", "Online", "Player1"]


def test_cleared_floor_records_the_remaining_run_time(board, monkeypatch):
    server, session, store, dispatch = board
    monkeypatch.setattr(ranking.time, "perf_counter", lambda: 500.0)
    monkeypatch.setattr(CharacterServer, "_request_account_save", staticmethod(lambda *a, **k: False))
    monkeypatch.setattr(CharacterServer, "_send_world_loot_state_batch",
                        staticmethod(lambda *a, **k: None))
    server._send_fractured_planes_progress = lambda s: None
    server._map_event_sessions = lambda runtime: (session,)
    session.gameplay_map_access_id = "h0009_endlesstower_floor_007"
    runtime = SimpleNamespace(
        lock=threading.Lock(), fractured_planes_floor_completed=False,
        fractured_planes_limit_timer=None, fractured_planes_limit_deadline=590.2)
    MapEventService._complete_fractured_planes_floor(server, runtime)
    record = server._ranking_store().scores("fp", "all")[50]
    assert (record.score, record.tiebreak) == (7, 91)
    assert session.gameplay_timed_offer_purchases["endless-tower-cleared-floor"] == 7
