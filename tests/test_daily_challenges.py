"""Daily board initialization, retail selection protocol, and durable billing."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
import threading
from types import SimpleNamespace
import unittest

import pytest

from dso_server.gameplay.daily_challenges import (
    ACCEPTED, AVAILABLE, CATEGORIES, ChallengeEligibility, DailyChallengeState,
    load_daily_challenge_catalog,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence.account_state import AccountStateStore
from dso_server.protocol.character_selection import BitWriter
from dso_server.protocol.daily_challenge_protocol import decode_daily_challenge_selection
from dso_server.protocol.player_init_protocol import (
    CURRENCY_CHANGED_COMMAND, TIMED_CHALLENGES_INFO_COMMAND,
    CurrencyChanged, ExitInfo, decode_server_command_batch, encode_server_command_batch,
)
from dso_server.protocol.talent_protocol import TalentDatabase
from dso_server.services.character_server import CharacterServer
from dso_server.tests.fixtures.characters import character_profile, character_server
from dso_server.tests.test_pvp_matchmaking import _session
from dso_server.tests.test_ranking import state as account_state


NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


def selection(identity, refresh=False):
    writer = BitWriter()
    writer.write_bytes(bytes.fromhex("8b9400"))
    writer.write_string(identity)
    writer.write_bool(refresh)
    return writer.finish()


@pytest.fixture
def daily_service():
    server = CharacterServer.__new__(CharacterServer)
    server.static_database_path = DEFAULT_STATIC_DB_PATH
    server.talent_database = TalentDatabase(DEFAULT_STATIC_DB_PATH)
    server._daily_challenge_now = lambda: NOW
    session = _session(50993, 52, "DailyPlayer")
    session.gameplay_currency_state = CurrencyChanged(100, 50, (0, 0, 0), 0, False)
    session.gameplay_membership_type = 0
    session.gameplay_membership_active = False
    server._restore_daily_challenge_state(session, None)
    server._restore_quest_state(session, None)
    saves = []
    def save(s, reason, flush):
        assert not s.flow_lock._is_owned()
        saves.append((server._daily_challenge_document(s), s.gameplay_currency_state))
    session.gameplay_persist_callback = save
    def dispatch(identity, refresh=False):
        payload, bits = selection(identity, refresh)
        session.current_message_bit_length = bits
        server._on_char_op(session, payload)
    return server, session, saves, dispatch


def pvp_entry(server, session):
    catalog = load_daily_challenge_catalog(DEFAULT_STATIC_DB_PATH)
    server._daily_challenge_initialization_info(session)
    return next(e for e in session.gameplay_daily_challenges.daily
                if catalog.definitions[e.challenge_id].category == "PvP")


def test_codec_matches_recorded_refresh_and_accept():
    refresh = bytes.fromhex("8b940014007076705f77696e6d6174636865735f746965723280")
    accept = bytes.fromhex("8b940014007076705f77696e6d6174636865735f746965723200")
    assert selection("pvp_winmatches_tier2", True) == (refresh, 201)
    assert selection("pvp_winmatches_tier2") == (accept, 201)
    assert decode_daily_challenge_selection(refresh, 201).refresh
    assert not decode_daily_challenge_selection(accept, 201).refresh
    assert decode_daily_challenge_selection(refresh).challenge_id == "pvp_winmatches_tier2"


@pytest.mark.parametrize("raw,bits", [
    ("", None), ("8b9400", None), ("8b9400000000", 41),
    ("8b9400020061", None), ("8b6400", None),
    ("8b940001006180", 48), ("8b94000100618000", None),
    ("8b940001006181", None),
])
def test_invalid_selection_is_rejected(raw, bits):
    with pytest.raises(ValueError):
        decode_daily_challenge_selection(bytes.fromhex(raw), bits)


def test_info_codec_reproduces_retail_and_identifies_the_three_collections():
    fixture = json.loads((Path(__file__).parent / "fixtures/daily_challenges_retail.json").read_text())
    raw = bytes.fromhex(fixture["payload"])
    commands = decode_server_command_batch(raw, fixture["bit_length"])
    assert len(commands) == 1 and commands[0].command_id == TIMED_CHALLENGES_INFO_COMMAND
    info = commands[0].value
    assert [e.state for e in info.daily] == [2, 2, 0, 2]
    assert info.daily[2].challenge_id == "pvp_playmatches_tier2"
    assert all(e.challenge_id.startswith("bonus_solved_challenge_") for e in info.weekly)
    assert len(info.weekly) == 7 and len(info.monthly) == 12
    assert encode_server_command_batch(commands, include_terminator=True) == (raw, fixture["bit_length"])


def test_catalog_generates_four_valid_categories_and_bonus_objectives():
    catalog = load_daily_challenge_catalog(DEFAULT_STATIC_DB_PATH)
    assert len(catalog.definitions) == 1480
    assert catalog.refresh_cost == 40 and catalog.free_refresh_memberships == {1, 2}
    for level in (10, 25, 40, 100):
        facts = ChallengeEligibility(level, "mage")
        board = catalog.board(None, NOW.date(), facts, rng=random.Random(4))
        definitions = [catalog.definitions[e.challenge_id] for e in board.daily]
        assert tuple(d.category for d in definitions) == CATEGORIES
        assert all(d.eligible(facts) and d.tier == 1 for d in definitions)
        assert all(e.state == AVAILABLE for e in board.daily)
        assert len(board.bonuses) == 7 and all(e.state == ACCEPTED for e in board.bonuses)
        assert catalog.board(board, NOW.date(), facts) == board
    assert catalog.board(None, NOW.date(), ChallengeEligibility(9, "mage")) is None
    definition = catalog.definitions["pvp_winmatches_tier1"]
    restricted = replace(definition, class_name="ranger", activation_quest="q", activation_event="event")
    assert not restricted.eligible(ChallengeEligibility(100, "mage"))
    assert not restricted.eligible(ChallengeEligibility(100, "ranger", closed_quests=frozenset({"q"})))
    assert restricted.eligible(ChallengeEligibility(
        100, "ranger", closed_quests=frozenset({"q"}), active_events=frozenset({"event"})))


def test_accept_persists_and_duplicate_or_accepted_refresh_spends_nothing(daily_service):
    server, session, saves, dispatch = daily_service
    old = pvp_entry(server, session)
    dispatch(old.challenge_id)
    board = session.gameplay_daily_challenges
    assert next(e for e in board.daily if e.challenge_id == old.challenge_id).state == ACCEPTED
    assert len(saves) == 1 and saves[0][0] == board.to_document()
    dispatch(old.challenge_id)
    dispatch(old.challenge_id, True)
    assert session.gameplay_daily_challenges == board and len(saves) == 1
    assert session.gameplay_currency_state.andermant == 100
    commands = decode_server_command_batch(*session.recorder.messages[-1][:2])
    assert commands[-1].value.daily == board.daily
    server._restore_daily_challenge_state(session, SimpleNamespace(daily_challenges=saves[0][0]))
    assert server._daily_challenge_initialization_info(session).daily == board.daily


@pytest.mark.parametrize("membership,active,expired,cost", [
    (0, False, False, 40), (1, True, False, 0), (2, True, False, 0),
    (1, False, False, 40), (2, True, True, 40),
])
def test_refresh_replaces_only_one_category_and_commits_cost_with_it(
    daily_service, membership, active, expired, cost,
):
    server, session, saves, dispatch = daily_service
    old = pvp_entry(server, session)
    before = session.gameplay_daily_challenges
    session.gameplay_membership_type = membership
    session.gameplay_membership_active = active
    session.gameplay_membership_expires_at = (NOW + timedelta(days=-1 if expired else 1)).isoformat()
    dispatch(old.challenge_id, True)
    board = session.gameplay_daily_challenges
    changes = [(a, b) for a, b in zip(before.daily, board.daily) if a != b]
    assert len(changes) == 1 and changes[0][0] == old
    catalog = load_daily_challenge_catalog(DEFAULT_STATIC_DB_PATH)
    changed = catalog.definitions[changes[0][1].challenge_id]
    assert changed.category == "PvP" and changed.tier == 1
    assert session.gameplay_currency_state.andermant == 100 - cost
    assert saves == [(board.to_document(), session.gameplay_currency_state)]
    commands = decode_server_command_batch(*session.recorder.messages[-1][:2])
    assert [c.command_id for c in commands] == (
        [CURRENCY_CHANGED_COMMAND, TIMED_CHALLENGES_INFO_COMMAND] if cost
        else [TIMED_CHALLENGES_INFO_COMMAND])
    dispatch(old.challenge_id, True)
    assert session.gameplay_currency_state.andermant == 100 - cost and len(saves) == 1


def test_insufficient_funds_and_unknown_selection_only_resend_current_board(daily_service):
    server, session, saves, dispatch = daily_service
    old = pvp_entry(server, session)
    board = session.gameplay_daily_challenges
    session.gameplay_currency_state = replace(session.gameplay_currency_state, currency_type=39)
    dispatch(old.challenge_id, True)
    dispatch("not_an_offer", True)
    assert session.gameplay_daily_challenges == board and not saves
    assert session.gameplay_currency_state.andermant == 39
    assert len(session.recorder.messages) == 2


def test_midnight_rollover_rejects_old_selection_without_charging(daily_service):
    server, session, saves, dispatch = daily_service
    old = pvp_entry(server, session)
    dispatch(old.challenge_id)
    server._daily_challenge_now = lambda: NOW + timedelta(days=1)
    dispatch(old.challenge_id, True)
    assert session.gameplay_daily_challenges.day == "2026-09-29"
    assert all(e.state == AVAILABLE for e in session.gameplay_daily_challenges.daily)
    assert session.gameplay_currency_state.andermant == 100


def test_concurrent_repeated_refresh_charges_once(daily_service, monkeypatch):
    server, session, saves, dispatch = daily_service
    old = pvp_entry(server, session)
    context = server._daily_challenge_context
    barrier = threading.Barrier(2)
    def synchronized(s):
        result = context(s)
        barrier.wait(timeout=5)
        return result
    monkeypatch.setattr(server, "_daily_challenge_context", synchronized)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(dispatch, old.challenge_id, True) for _ in range(2)]
        for future in pending:
            future.result(timeout=10)
    assert session.gameplay_currency_state.andermant == 60 and len(saves) == 1


def test_board_round_trips_through_sqlite_and_old_accounts(daily_service, tmp_path):
    server, session, _, _ = daily_service
    pvp_entry(server, session)
    board = session.gameplay_daily_challenges
    path = tmp_path / "daily.sqlite3"
    store = AccountStateStore(path)
    try:
        store.put(account_state(52, daily_challenges=board.to_document()))
        store.put(account_state(53))
        store.flush()
    finally:
        store.close()
    store = AccountStateStore(path)
    try:
        assert DailyChallengeState.from_document(store.get(52).daily_challenges) == board
        assert store.get(53).daily_challenges == {}
        doc = board.to_document()
        restored = DailyChallengeState.from_document(doc)
        doc["daily"][0]["state"] = 3
        assert restored == board
    finally:
        store.close()


def test_real_initialization_emits_populated_info_and_snapshot_keeps_it():
    from dso_server.services.player_init_world import PlayerInitializationWorld
    case = unittest.TestCase()
    try:
        server = character_server(case, (character_profile(52, "DailyPlayer"),))
        session = _session(50994, 52, "DailyPlayer")
        session.gameplay_character = server.character_selection.active_character
        state = server._resolve_initialization_character(session)
        server._resolve_initialization_inventory(state)
        server._encode_initialization_new_player(state)
        server._resolve_initialization_talents(state)
        server._resolve_initialization_persistent_effects(session, state)
        server._resolve_initialization_vitals(state)
        server._apply_initialization_state(session, state)
        world = PlayerInitializationWorld((), ExitInfo(()), None, None, (), None)
        commands = server._build_initialization_commands(session, state, world)
        daily = [c for c in commands if c.command_id == TIMED_CHALLENGES_INFO_COMMAND]
        assert len(daily) == 1 and len(daily[0].value.daily) == 4
        assert len(daily[0].value.weekly) == 7
        packet = encode_server_command_batch(tuple(daily))
        assert decode_server_command_batch(*packet)[0] == daily[0]
        snapshot = server._capture_account_state(session)
        assert DailyChallengeState.from_document(snapshot.daily_challenges) == session.gameplay_daily_challenges
    finally:
        case.doCleanups()


def test_context_uses_saved_wisdom_progress_outside_lock(daily_service, monkeypatch):
    server, session, _, _ = daily_service
    session.gameplay_talent_category_points = (0, 12345, 0, 0)
    def wisdom_level(category_id, xp):
        assert not session.flow_lock._is_owned()
        assert (category_id, xp) == (1, 12345)
        return 42
    monkeypatch.setattr(server.talent_database, "progression_level", wisdom_level)
    assert server._daily_challenge_context(session)[2].wisdom_level == 42


def test_expired_event_rerolls_unaccepted_offer_but_keeps_accepted_progress():
    catalog = load_daily_challenge_catalog(DEFAULT_STATIC_DB_PATH)
    facts = ChallengeEligibility(100, "mage")
    board = catalog.board(None, NOW.date(), facts, rng=random.Random(3))
    old = board.daily[0]
    definitions = dict(catalog.definitions)
    definitions[old.challenge_id] = replace(definitions[old.challenge_id], activation_event="expired")
    catalog = replace(catalog, definitions=definitions)
    updated = catalog.board(board, NOW.date(), facts)
    assert updated.daily[0].challenge_id != old.challenge_id
    assert updated.daily[1:] == board.daily[1:]
    accepted = replace(board, daily=(replace(old, state=ACCEPTED), *board.daily[1:]))
    assert catalog.board(accepted, NOW.date(), facts) == accepted


def set_task(server, session, *, objective='MonstersKilled', target='2', reward='VC:100', tier=1,
             category='Generic', difficulty='Any', reward_effect=''):
    from dso_server.gameplay.daily_challenges import DailyChallengeCatalog
    catalog, now, facts = server._daily_challenge_context(session)
    base = next(d for d in catalog.definitions.values() if d.category == category and d.tier == tier)
    task = replace(base, resource_id='test_daily', objective=objective, objective_value=target,
                   reward=reward, reward_effect=reward_effect, difficulty=difficulty)
    definitions = dict(catalog.definitions); definitions[task.resource_id] = task
    catalog = replace(catalog, definitions=definitions)
    server._daily_challenge_context = lambda s: (catalog, now, facts)
    session.gameplay_daily_challenges = DailyChallengeState(now.date().isoformat(), (task.entry(),), ())
    return task, catalog


def test_progress_requires_acceptance_and_rewards_only_once(daily_service):
    server, session, saves, dispatch = daily_service
    task, catalog = set_task(server, session)
    server._daily_challenge_feed(session, 'MonstersKilled', count=99)
    assert session.gameplay_currency_state.amount == 50
    dispatch(task.resource_id)
    server._daily_challenge_feed(session, 'MonstersKilled')
    assert next(e for e in session.gameplay_daily_challenges.daily if e.challenge_id == 'test_daily').progress == 1
    server._daily_challenge_feed(session, 'MonstersKilled')
    state = session.gameplay_daily_challenges
    assert state.daily[0].state == 3 and state.daily[0].progress == 2
    assert session.gameplay_currency_state.amount == 150
    assert any(e.state == AVAILABLE and catalog.definitions[e.challenge_id].tier == 2
               for e in state.daily if catalog.definitions[e.challenge_id].category == 'Generic')
    server._daily_challenge_feed(session, 'MonstersKilled', count=500)
    assert session.gameplay_currency_state.amount == 150
    server._restore_daily_challenge_state(session, SimpleNamespace(daily_challenges=state.to_document()))
    server._daily_challenge_feed(session, 'MonstersKilled', count=500)
    assert session.gameplay_currency_state.amount == 150


def test_final_tier_keeps_completed_row_without_new_offer(daily_service):
    server, session, _, dispatch = daily_service
    task, catalog = set_task(server, session, tier=3)
    dispatch(task.resource_id)
    server._daily_challenge_feed(session, 'MonstersKilled', count=100)
    generic = [e for e in session.gameplay_daily_challenges.daily
               if catalog.definitions[e.challenge_id].category == 'Generic']
    assert len(generic) == 1 and generic[0].state == 3


def test_item_task_counts_quantity_and_filters_template_and_difficulty(daily_service):
    server, session, _, dispatch = daily_service
    task, _ = set_task(server, session, objective='ItemCollected',
                       target='gem_ruby_a|gem_ruby_b:5', difficulty='1')
    dispatch(task.resource_id)
    server._daily_challenge_feed(session, 'ItemCollected', value='gem_ruby_a', count=5)
    assert next(e for e in session.gameplay_daily_challenges.daily if e.challenge_id == 'test_daily').progress == 0
    session.gameplay_map_difficulty = 1
    server._daily_challenge_feed(session, 'ItemCollected', value='gem_sapphire_a', count=5)
    assert next(e for e in session.gameplay_daily_challenges.daily if e.challenge_id == 'test_daily').progress == 0
    server._daily_challenge_feed(session, 'ItemCollected', value='gem_ruby_b', count=5)
    assert next(e for e in session.gameplay_daily_challenges.daily if e.challenge_id == 'test_daily').state == 3


def test_full_inventory_keeps_reward_pending_then_retries_once(daily_service):
    from dso_server.protocol.inventory_codec import InventoryInfo
    server, session, _, dispatch = daily_service
    task, _ = set_task(server, session, reward='Loot:gem_ruby_a,2')
    session.gameplay_inventory = InventoryInfo(storage_size=0)
    dispatch(task.resource_id)
    server._daily_challenge_feed(session, 'MonstersKilled', count=2)
    assert next(e for e in session.gameplay_daily_challenges.daily if e.challenge_id == 'test_daily').state == ACCEPTED
    assert next(e for e in session.gameplay_daily_challenges.daily if e.challenge_id == 'test_daily').progress == 2
    session.gameplay_inventory.storage_size = 21
    server._daily_challenge_feed(session, 'MonstersKilled')
    assert next(e for e in session.gameplay_daily_challenges.daily if e.challenge_id == 'test_daily').state == 3
    assert sum(i.amount for i in session.gameplay_inventory.items if i.template_id == 'gem_ruby_a') == 2
    server._daily_challenge_feed(session, 'MonstersKilled')
    assert sum(i.amount for i in session.gameplay_inventory.items if i.template_id == 'gem_ruby_a') == 2


def test_an_item_reward_shows_the_item_received_notification(daily_service):
    from dso_server.protocol.gameplay_protocol import Message
    from dso_server.protocol.inventory_codec import InventoryInfo
    server, session, _, _ = daily_service
    task, _ = set_task(server, session, reward='Loot:gem_ruby_a,2;VC:100')
    session.gameplay_inventory = InventoryInfo(storage_size=21)
    commands, _ = server._daily_challenge_reward(session, task, None)
    (toast,) = [command.value for command in commands if isinstance(command.value, Message)]
    gem = next(item for item in session.gameplay_inventory.items if item.template_id == 'gem_ruby_a')
    assert toast.message_type == 2
    assert toast.arguments[1].uint32_value == 2
    assert toast.arguments[2].uint32_value == gem.item_id


def test_a_currency_reward_shows_no_item_notification(daily_service):
    from dso_server.protocol.gameplay_protocol import Message
    server, session, _, _ = daily_service
    task, _ = set_task(server, session, reward='VC:100')
    commands, _ = server._daily_challenge_reward(session, task, None)
    assert not any(isinstance(command.value, Message) for command in commands)


def test_honor_and_authored_buff_are_awarded(daily_service, monkeypatch):
    server, session, _, dispatch = daily_service
    catalog, _, _ = server._daily_challenge_context(session)
    real = catalog.definitions['pvp_winmatches_tier1']
    task, _ = set_task(server, session, objective='MatchesWon', target='1',
                       reward='HC:50,CharacterLevel', reward_effect=real.reward_effect)
    monkeypatch.setattr(CharacterServer, '_schedule_consumable_timers', lambda *a, **k: None)
    monkeypatch.setattr(CharacterServer, '_refresh_player_combat_vitals', lambda *a: None)
    dispatch(task.resource_id)
    server._daily_challenge_feed(session, 'MatchesWon')
    assert session.gameplay_honor_points == 5000
    assert next(e for e in session.gameplay_daily_challenges.daily if e.challenge_id == 'test_daily').state == 3
    effects = [e for e, expiry in session.gameplay_runtime_status_effects.values()]
    effects += list(session.gameplay_persistent_status_effects.values())
    assert any(e.field_10 == 900 * 25 for e in effects)
    server._daily_challenge_feed(session, 'MatchesWon')
    assert session.gameplay_honor_points == 5000


def test_ranked_monster_events_use_combatant_rank_id(daily_service, monkeypatch):
    server, session, _, dispatch = daily_service
    task, _ = set_task(server, session, objective='ChampionsAndLeadersKilled', target='2')
    dispatch(task.resource_id)
    server._daily_challenge_monsters_killed(session, (
        SimpleNamespace(template_id='a', rank_id='Rank_champion'),
        SimpleNamespace(template_id='a', rank_id='Rank_monster'),
        SimpleNamespace(template_id='a', rank_id='Rank_leader')))
    assert next(e for e in session.gameplay_daily_challenges.daily if e.challenge_id == 'test_daily').state == 3


def test_native_progress_packet_contains_single_id_state_count_and_six_date_words():
    import struct
    from dso_server.protocol.player_init_protocol import (
        DAILY_CHALLENGE_PROGRESS_COMMAND, ServerCommand, TimedChallenge)
    entry = TimedChallenge('abc','abc',3,12,(2026,9,29,1,2,3))
    packet,bits = encode_server_command_batch((ServerCommand(DAILY_CHALLENGE_PROGRESS_COMMAND, entry, 1),),
                                             include_terminator=True)
    expected = b'\x85\x95\x00\x03\x00abc\x03' + struct.pack('<8I',12,2026,9,29,1,2,3,1) + b'\xff'
    assert packet == expected and bits == len(expected)*8
    assert decode_server_command_batch(packet,bits)[0].value == entry


def test_bonus_reward_is_atomic_and_not_repaid_after_restore(daily_service):
    from dso_server.protocol.inventory_codec import InventoryInfo
    server, session, _, dispatch = daily_service
    task, catalog = set_task(server, session, target='1')
    other = replace(task.entry(), challenge_id='earlier_daily', schedule_id='earlier_daily', state=3, progress=1)
    definitions = dict(catalog.definitions)
    definitions['earlier_daily'] = replace(task, resource_id='earlier_daily', category='Cooking')
    catalog = replace(catalog, definitions=definitions)
    _, now, facts = server._daily_challenge_context(session)
    server._daily_challenge_context = lambda s: (catalog, now, facts)
    session.gameplay_inventory = InventoryInfo(storage_size=21)
    session.gameplay_daily_challenges = replace(session.gameplay_daily_challenges,
                                               daily=(other, task.entry()))
    dispatch(task.resource_id)
    server._daily_challenge_feed(session, 'MonstersKilled')
    board = session.gameplay_daily_challenges
    bonus = next(e for e in board.bonuses if e.challenge_id == 'bonus_solved_challenge_2')
    assert bonus.state == 3 and bonus.progress == 2
    assert sum(i.amount for i in session.gameplay_inventory.items
               if i.template_id == 'crafting_stateobject_key_broken') == 2
    server._restore_daily_challenge_state(session, SimpleNamespace(daily_challenges=board.to_document()))
    server._daily_challenge_feed(session, 'RetryRewards', count=0)
    assert sum(i.amount for i in session.gameplay_inventory.items
               if i.template_id == 'crafting_stateobject_key_broken') == 2
