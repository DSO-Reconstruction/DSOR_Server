"""Period history, idempotent settlement and reward/achievement delivery."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

import pytest

from dso_server.persistence import ranking_state
from dso_server.persistence.ranking_state import RankingStateStore
from dso_server.services import commerce, ranking
from dso_server.services.character_server import CharacterServer
from dso_server.tests.fixtures.characters import character_profile, character_server
from dso_server.tests.test_achievement_rewards import amount, initialize
from dso_server.tests.test_ranking_boards import FakeEvent


FIRST, SECOND, THIRD = 200_000_001, 200_000_002, 200_000_003
NOW = datetime.now(timezone.utc).replace(microsecond=0)
OPEN, ENDED = NOW, NOW + timedelta(hours=2)


@pytest.fixture
def world(monkeypatch):
    case = unittest.TestCase()
    try:
        server = character_server(case, [
            character_profile(FIRST, "First", class_id=1),
            character_profile(SECOND, "Second", class_id=2),
            character_profile(THIRD, "Third", class_id=0),
        ])
        monkeypatch.setattr(CharacterServer, "_send_world_loot_state_batch",
                            staticmethod(lambda *args, **kwargs: None))
        monkeypatch.setattr(ranking, "RANKING_SNAPSHOT_SECONDS", 0.0)
        online = []
        server._active_gameplay_sessions = lambda: tuple(online)
        feeds = []
        original = server._achievement_feed

        def feed(s, kind, values, **kwargs):
            feeds.append((int(s.gameplay_character.character_id), kind, tuple(values)))
            return original(s, kind, values, **kwargs)

        server._achievement_feed = feed
        yield SimpleNamespace(server=server, online=online, feeds=feeds)
    finally:
        case.doCleanups()


def schedule(server, *events):
    server._ranking_event_catalog = lambda: SimpleNamespace(events=tuple(events))


def fp_event(event_id=77):
    return FakeEvent(event_id, "event_fracturedplanes_rank_start",
                     NOW - timedelta(hours=1), NOW + timedelta(hours=1))


def login(world, identity):
    session = initialize(world.server, identity)
    world.online.append(session)
    return session


def floors(world, period, values):
    store = world.server._ranking_store()
    for identity, floor, remaining in values:
        store.improve_score("fp", period, identity, score=floor, tiebreak=remaining,
                            stats=(floor, remaining))


def claims(world, identity):
    return world.server.account_store.get(identity).timed_offer_purchases


# ---------------------------------------------------------------------------
# Fractured Planes ranking period
# ---------------------------------------------------------------------------

def test_high_score_settlement_rewards_places_and_emits_rank_once(world):
    server = world.server
    schedule(server, fp_event())
    first = login(world, FIRST)
    server._ranking_tick(OPEN)
    server._ranking_record_fractured_planes_floor(first, 20, None)
    floors(world, "event:77", ((SECOND, 12, 30), (THIRD, 0, 0)))
    server._ranking_tick(ENDED)

    store = server._ranking_store()
    history = store.history("fp.group", "event:77")
    assert [(row.rank, row.character_id, row.stats) for row in history] == [
        (1, FIRST, (20, 0)), (2, SECOND, (12, 30))]        # Floor 0 is not ranked.
    entitlements = {value.character_id: value for value in store.entitlements()}
    assert set(entitlements) == {FIRST, SECOND}
    assert entitlements[FIRST].items == (("fp_box_01", 1),)
    assert entitlements[SECOND].items == (("fp_box_02", 1),)
    # Online winner: granted, saved with its claim, rank fed to achievements.
    assert amount(first, "fp_box_01") == 1
    assert entitlements[FIRST].reward_status == entitlements[FIRST].achievement_status == "delivered"
    assert claims(world, FIRST)["ranking-reward:fp.group:event:77"] == 1
    assert claims(world, FIRST)["ranking-achievement:fp.group:event:77"] == 1
    assert world.feeds == [(FIRST, "highscoreranking", ("1",))]
    assert first.gameplay_achievements["fracturedplanes_rank3"].progress == 1
    # Offline runner-up keeps both pending.
    assert entitlements[SECOND].pending
    # Replayed ticks change nothing.
    receipt = store.settlement("fp.season", "event:77")
    server._ranking_tick(ENDED)
    server._ranking_tick(ENDED + timedelta(minutes=5))
    assert store.settlement("fp.season", "event:77") == receipt
    assert amount(first, "fp_box_01") == 1
    assert world.feeds == [(FIRST, "highscoreranking", ("1",))]


def test_offline_place_is_delivered_at_next_login_exactly_once(world):
    server = world.server
    schedule(server, fp_event())
    server._ranking_tick(OPEN)
    floors(world, "event:77", ((SECOND, 9, 10),))
    server._ranking_tick(ENDED)
    second = initialize(server, SECOND)
    server._ranking_on_map_entered(second)
    assert amount(second, "fp_box_01") == 1
    assert world.feeds == [(SECOND, "highscoreranking", ("1",))]
    assert server._ranking_store().entitlements(pending_only=True) == ()
    # A relog replays nothing: the claims travelled with the inventory.
    again = initialize(server, SECOND)
    server._ranking_on_map_entered(again)
    assert amount(again, "fp_box_01") == 1
    assert len(world.feeds) == 1


def test_full_inventory_keeps_the_reward_pending_until_space(world, monkeypatch):
    server = world.server
    schedule(server, fp_event())
    server._ranking_tick(OPEN)
    floors(world, "event:77", ((SECOND, 9, 10), (FIRST, 5, 10)))
    server._ranking_tick(ENDED)
    second = initialize(server, SECOND)
    with monkeypatch.context() as patch:
        def full(*args, **kwargs):
            raise ValueError("ordinary inventory storage is full")
        patch.setattr(commerce, "add_purchased_item", full)
        server._ranking_on_map_entered(second)
    pending = server._ranking_store().entitlements(SECOND)[0]
    assert pending.reward_status == "pending" and "full" in pending.last_error
    assert pending.achievement_status == "delivered"     # The rank event is independent.
    assert amount(second, "fp_box_01") == 0
    server._ranking_on_map_entered(second)
    assert amount(second, "fp_box_01") == 1
    assert server._ranking_store().entitlements(SECOND, pending_only=True) == ()
    assert world.feeds == [(SECOND, "highscoreranking", ("1",))]


def test_settlement_interrupted_mid_transaction_leaves_nothing_and_reruns(world, monkeypatch):
    server = world.server
    schedule(server, fp_event())
    server._ranking_tick(OPEN)
    floors(world, "event:77", ((FIRST, 3, 1), (SECOND, 2, 1)))
    original = ranking_state._encode
    calls = []

    def crash(values):
        calls.append(1)
        if len(calls) == 2:                              # Second history row.
            raise RuntimeError("power cut")
        return original(values)

    with monkeypatch.context() as patch:
        patch.setattr(ranking_state, "_encode", crash)
        with pytest.raises(RuntimeError):
            server._ranking_settle("fp.season", "event:77", ENDED)
    store = server._ranking_store()
    assert store.settlement("fp.season", "event:77") is None
    assert store.history("fp.group", "event:77") == ()
    assert store.entitlements() == ()
    assert store.period("fp.season", "event:77").state == "open"
    # A restart (fresh store on the same file) settles it completely, once.
    server.ranking_store = RankingStateStore(store.path)
    server._ranking_tick(ENDED)
    assert server.ranking_store.settlement("fp.season", "event:77")[1] == 2
    assert server._ranking_settle("fp.season", "event:77", ENDED) is False
    assert len(server.ranking_store.entitlements()) == 2


def test_removed_schedule_entry_does_not_settle(world):
    server = world.server
    schedule(server, fp_event())
    server._ranking_tick(OPEN)
    floors(world, "event:77", ((FIRST, 3, 1),))
    schedule(server)
    server._ranking_tick(ENDED)
    assert server._ranking_store().settlement("fp.season", "event:77") is None


# ---------------------------------------------------------------------------
# PvP and PvE seasons, honor periods
# ---------------------------------------------------------------------------

def test_pvp_season_rewards_season_honor_places(world):
    server = world.server
    schedule(server, FakeEvent(88, "pvp_seasons_event", NOW - timedelta(days=1), NOW + timedelta(hours=1)))
    first = login(world, FIRST)
    first.gameplay_honor_points = 500                    # Career honor before the season.
    server._ranking_tick(OPEN)
    first.gameplay_honor_points = 540
    second = initialize(server, SECOND)                  # Plays, then logs out.
    second.gameplay_honor_points = 10
    server._persist_gameplay_session(second, reason="client disconnect", flush=True)
    server._ranking_on_pvp_match_settled(first, match_mode=0, honor=40, outcome="won", kills=2, deaths=0)
    server._ranking_tick(ENDED)
    store = server._ranking_store()
    assert [(row.character_id, row.score) for row in store.history("pvp.overall", "event:88")] == [
        (FIRST, 40), (SECOND, 10)]
    assert [(row.character_id, row.stats) for row in store.history("pvp.solo", "event:88")] == [
        (FIRST, (40, 1, 0, 0, 2, 0))]
    assert amount(first, "box_pvp_leaderboard_overall_1st") == 1
    second = {value.character_id: value for value in store.entitlements()}[SECOND]
    assert second.items == (("box_pvp_leaderboard_overall_2nd", 1),)
    assert second.achievement_status == "none" and world.feeds == []


def test_pve_season_settles_solo_and_group_overall_tables(world):
    server = world.server
    schedule(server, FakeEvent(99, "pve_season_event", NOW - timedelta(days=1), NOW + timedelta(hours=1)))
    server._ranking_tick(OPEN)
    store = server._ranking_store()
    for board, identity, value in (("pw.0.0", FIRST, 900), ("pw.0.0", SECOND, 800),
                                   ("pw.3.1", THIRD, 50)):
        store.improve_score(board, "event:99", identity, score=value,
                            stats=(value, 1, 2, 3, 600, 100, 1, 5, 3))
    store.improve_score("pw.0.0", "all", THIRD, score=99999, stats=(99999, 0, 0, 0, 1, 1, 1, 5, 3))
    server._ranking_tick(ENDED)
    rewards = {(value.board, value.character_id): value.items for value in store.entitlements()}
    assert rewards == {
        ("pve.overall.0", FIRST): (("box_leaderboard_overall_1st_2026", 1),),
        ("pve.overall.0", SECOND): (("box_leaderboard_overall_2nd_2026", 1),),
        ("pve.overall.1", THIRD): (("box_leaderboard_overall_group_1st_2026", 1),),
    }
    assert [(row.character_id, row.stats) for row in store.history("pw.0.0", "event:99")] == [
        (FIRST, (900, 1, 2, 3, 600, 100, 1, 5, 3)), (SECOND, (800, 1, 2, 3, 600, 100, 1, 5, 3))]
    assert [(row.character_id, row.score) for row in store.history("pve.overall.0", "event:99")] == [
        (FIRST, 150), (SECOND, 125)]


def test_daily_period_closes_into_history_and_reopens_from_new_totals(world):
    server = world.server
    schedule(server)
    day = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
    first = login(world, FIRST)
    first.gameplay_honor_points = 100
    server._ranking_tick(day)
    first.gameplay_honor_points = 130
    first.gameplay_pvp_matches_won = 1
    server._ranking_tick(day + timedelta(hours=14, minutes=1))   # 00:01 next day.
    store = server._ranking_store()
    assert [(row.character_id, row.stats) for row in store.history("honor.daily", "day:2026-10-26")] == [
        (FIRST, (30, 1, 0, 0, 0, 0))]
    assert store.settlement("honor.daily", "day:2026-10-26") is not None
    assert store.period("honor.daily", "day:2026-10-27").state == "open"
    assert store.baselines("day:2026-10-27")[FIRST][0] == 130
    assert store.period("honor.weekly", "week:2026-W44").state == "open"
    assert store.entitlements() == ()
