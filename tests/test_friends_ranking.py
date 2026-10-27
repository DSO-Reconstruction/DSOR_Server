"""Friends boards read the requester's persisted relationships on every query."""
from types import SimpleNamespace

import pytest

from dso_server.persistence.social_state import SocialStateStore
from dso_server.tests.test_ranking import board, add, read_page, request_bytes, DETAIL


@pytest.fixture
def friends(board, tmp_path):
    server, session, account_store, dispatch = board
    store = SocialStateStore(tmp_path / "friends.json")
    server.social_store = store
    yield server, session, account_store, dispatch, store
    server.social_store.close()


def test_offline_friends_survive_store_reload_and_exclude_unrelated_players(friends):
    server, session, accounts, dispatch, social = friends
    add(accounts, 1, 100, name="Offline friend")
    add(accounts, 2, 1000, name="Stranger")
    social.add_buddy(session.gameplay_character.character_id, 1)
    social.add_buddy(2, session.gameplay_character.character_id)  # Direction matters.
    social.close()
    server.social_store = SocialStateStore(social.path)
    result = read_page(dispatch(request_bytes(flags=2)))
    assert result.rows == [("Offline friend", 100, 0), ("Online", 75, 1)]
    assert result.total == 2
    assert accounts.get(1).honor_points == 100


def test_add_remove_and_deleted_friend_apply_even_while_score_snapshot_is_cached(friends, monkeypatch):
    server, session, accounts, dispatch, social = friends
    from dso_server.services import ranking
    monkeypatch.setattr(ranking, "RANKING_SNAPSHOT_SECONDS", 3600)
    add(accounts, 1, 100)
    add(accounts, 2, 50)
    load = server._honor_ranking_records
    scans = []
    monkeypatch.setattr(server, "_honor_ranking_records", lambda s: (scans.append(1), load(s))[1])
    assert read_page(dispatch(request_bytes(flags=2))).rows == [("Online", 75, 0)]
    social.add_buddy(50, 1)
    social.add_buddy(50, 999999)  # A persisted reference without a character record.
    assert [row[0] for row in read_page(dispatch(request_bytes(flags=2))).rows] == ["Player1", "Online"]
    social.remove_buddy(50, 1)
    social.add_buddy(50, 2)
    assert [row[0] for row in read_page(dispatch(request_bytes(flags=2))).rows] == ["Online", "Player2"]
    assert len(scans) == 1


def test_friend_and_guild_union_deduplicates_and_all_filter_keeps_precedence(friends):
    server, session, accounts, dispatch, social = friends
    for identity in (1, 2, 3):
        add(accounts, identity, 110 - identity)
    social.add_buddy(50, 1)
    social.guild_for = lambda _: SimpleNamespace(members=[SimpleNamespace(character_id=i) for i in (1, 2, 50)])
    assert [row[0] for row in read_page(dispatch(request_bytes(flags=6))).rows] == ["Player1", "Player2", "Online"]
    assert read_page(dispatch(request_bytes(flags=7))).total == 4


def test_friend_board_keeps_class_filter_sorting_and_filtered_pagination(friends):
    server, session, accounts, dispatch, social = friends
    for identity in range(1, 13):
        add(accounts, identity, identity, class_id=1, pvp_kills=20-identity)
        social.add_buddy(50, identity)
    add(accounts, 99, 9999, class_id=1, pvp_kills=9999)
    result = read_page(dispatch(request_bytes(DETAIL, flags=2, class_id=1, column=4, start=10)))
    assert result.total == 12
    assert [row[0] for row in result.rows] == ["Player11", "Player12"]
    assert [row[2] for row in result.rows] == [10, 11]
