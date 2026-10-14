"""Wanted mission wire layouts, content, persistence, and service lifecycle."""

from datetime import datetime, timezone
import random
import struct

import pytest

from dso_server.gameplay.quest_content import load_quest_catalog
from dso_server.gameplay.wanted_thief import WantedState, load_wanted_catalog
from dso_server.paths import DEFAULT_QUESTS_PATH, DEFAULT_STATIC_DB_PATH
from dso_server.persistence.account_state import (
    AccountStateStore, _state_from_document, _state_to_document,
)
from dso_server.protocol.wanted_thief_protocol import (
    WantedThiefInfo, WantedThiefRequest, decode_wanted_thief_request,
    encode_wanted_thief_info,
)
from dso_server.tests.test_ranking import state


def test_empty_info_request_and_one_based_slot_requests():
    assert decode_wanted_thief_request(bytes.fromhex("8b6401")) == WantedThiefRequest(0x164)
    assert decode_wanted_thief_request(bytes.fromhex("8b660104000000")) == WantedThiefRequest(0x166, 4)
    assert decode_wanted_thief_request(bytes.fromhex("8b670102000000")) == WantedThiefRequest(0x167, 2)


@pytest.mark.parametrize("raw,bits", [
    ("", None), ("8b", None), ("8b64", None), ("846401", None),
    ("8b6501", None), ("8b640100", None), ("8b6601", None),
    ("8b6701010000", None), ("8b66010100000000", None),
    ("8b6401", 23), ("8b6401", 25), ("8b660101000000", 55),
])
def test_malformed_requests_are_rejected(raw, bits):
    with pytest.raises(ValueError):
        decode_wanted_thief_request(bytes.fromhex(raw), bits)


def test_native_response_layout_including_datetime_and_actor():
    # Independent uint32 sequence from the client decoder, including both
    # array lengths and DateTime's unusual year/day/hour/.../month ordering.
    info = WantedThiefInfo(
        9, 4, (5, 14, 29, 50), (1, 0, 0, 0),
        datetime(2026, 9, 28, 12, 34, 56, 789000, tzinfo=timezone.utc),
    )
    payload, bits = encode_wanted_thief_info(info, actor_id=0x10015)
    expected = bytes.fromhex("846501") + struct.pack(
        "<21I", 9, 4, 4, 5, 14, 29, 50, 4, 1, 0, 0, 0,
        2026, 28, 12, 34, 56, 789, 9, 1, 0x10015,
    )
    assert payload == expected
    assert bits == 696
    empty_expiry, _ = encode_wanted_thief_info(
        WantedThiefInfo(10, 5, (5, 14, 29, 50), (0, 0, 0, 0)))
    assert empty_expiry[-36:-4] == bytes(32)


def test_catalog_uses_database_ids_pools_and_refresh_costs():
    catalog = load_wanted_catalog(DEFAULT_STATIC_DB_PATH)
    quests = load_quest_catalog(DEFAULT_QUESTS_PATH)
    assert len(catalog.missions) == 63
    assert (catalog.base_times, catalog.vip_times) == (10, 5)
    assert (catalog.single_refresh_cost, catalog.all_refresh_cost, catalog.vip_buy_cost) == (30, 100, 500)
    for mission in catalog.missions.values():
        quest = quests.quest(mission.quest_id)
        assert quest is not None and quest.quest_type == "wantedrepeatable"
    first = catalog.roll((0, 0, 0, 0), (0, 1, 2, 3), rng=random.Random(9))
    assert len(set(first)) == 4
    refreshed = catalog.roll(first, (1,), excluded=frozenset({5, 6}), rng=random.Random(1))
    assert refreshed[1] != first[1]
    assert refreshed[1] not in {5, 6}
    assert refreshed[0] == first[0] and refreshed[2:] == first[2:]
    with pytest.raises(ValueError, match="no Wanted mission"):
        catalog.roll(first, (0,), excluded=frozenset(catalog.missions))


def test_board_survives_sqlite_reload_and_older_characters_default(tmp_path):
    path = tmp_path / "players.sqlite3"
    board = WantedState("2026-09-28", 7, 3, (5, 14, 29, 50), (1, 0, 1, 0), 1800000000)
    store = AccountStateStore(path)
    try:
        store.put(state(51, wanted_thief=board.to_document()))
        store.put(state(52))
        store.flush()
    finally:
        store.close()
    store = AccountStateStore(path)
    try:
        assert WantedState.from_document(store.get(51).wanted_thief) == board
        assert WantedState.from_document(store.get(52).wanted_thief) is None
        document = _state_to_document(store.get(51))
        restored = _state_from_document(document)
        document["wanted_thief"]["mission_ids"][0] = 99
        assert restored.wanted_thief["mission_ids"][0] == 5
        document.pop("wanted_thief")
        assert _state_from_document(document).wanted_thief == {}
    finally:
        store.close()


# Retail 12:08:27.769. Three normal slots already accepted, seven attempts
# left; the fourth slot has five attempts. The global manager actor is zero.
RETAIL_WANTED_INFO = bytes.fromhex(
    "856501070000000500000004000000170000003600000034000000070000000400000001"
    "000000010000000100000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000ff"
)


def test_response_matches_retail_capture():
    payload, bits = encode_wanted_thief_info(
        WantedThiefInfo(7, 5, (23, 54, 52, 7), (1, 1, 1, 0)))
    assert bytes([0x85]) + payload[1:] + bytes([0xFF]) == RETAIL_WANTED_INFO
    assert bits + 8 == len(RETAIL_WANTED_INFO) * 8


@pytest.fixture
def wanted_service():
    from dso_server.services.character_server import CharacterServer
    from dso_server.tests.test_pvp_matchmaking import _session
    server = CharacterServer.__new__(CharacterServer)
    server.static_database_path = DEFAULT_STATIC_DB_PATH
    server._wanted_now = lambda: datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    session = _session(50992, 51, "WantedPlayer")
    server._restore_wanted_thief_state(session, None)
    server._restore_quest_state(session, None)
    saves = []
    session.gameplay_persist_callback = lambda s, reason, flush: saves.append(
        server._wanted_thief_document(s))
    def dispatch(raw="8b6401"):
        payload = bytes.fromhex(raw)
        session.current_message_bit_length = len(payload) * 8
        server._on_char_op(session, payload)
    return server, session, saves, dispatch


def test_info_dispatch_creates_and_reuses_the_saved_board(wanted_service):
    server, session, saves, dispatch = wanted_service
    dispatch()
    board = session.gameplay_wanted_thief
    assert len(board.mission_ids) == 4
    assert (board.remaining, board.special_remaining) == (10, 5)
    assert len(saves) == 1
    assert session.recorder.messages[-1][0].startswith(bytes.fromhex("846501"))
    assert session.recorder.messages[-1][0][-4:] == bytes(4)
    first = session.recorder.messages[-1]
    dispatch()
    assert session.gameplay_wanted_thief == board
    assert session.recorder.messages[-1] == first
    assert len(saves) == 1
    from types import SimpleNamespace
    server._restore_wanted_thief_state(session, SimpleNamespace(wanted_thief=saves[0]))
    dispatch()
    assert session.gameplay_wanted_thief == board
    assert len(saves) == 1


def test_daily_counter_reset_keeps_active_missions_and_is_once_only(wanted_service):
    server, session, saves, dispatch = wanted_service
    original = WantedState("2026-09-27", 0, 1, (23, 54, 52, 7), (1, 1, 1, 0))
    session.gameplay_wanted_thief = original
    dispatch()
    board = session.gameplay_wanted_thief
    assert (board.day, board.remaining, board.special_remaining) == ("2026-09-28", 10, 5)
    assert board.mission_ids == original.mission_ids and board.statuses == original.statuses
    assert len(saves) == 1
    dispatch()
    assert len(saves) == 1


def test_malformed_info_and_retired_session_do_not_create_state(wanted_service):
    _, session, saves, dispatch = wanted_service
    dispatch("8b640100")
    assert session.gameplay_wanted_thief is None and not saves
    assert session.recorder.messages == []
    session.retired = True
    dispatch()
    assert session.gameplay_wanted_thief is None and not saves
    assert session.recorder.messages == []



def test_production_account_snapshot_detaches_board(wanted_service):
    from dso_server.tests.fixtures.characters import character_profile
    from dso_server.protocol.inventory_codec import InventoryInfo
    from dso_server.protocol.player_init_protocol import CurrencyChanged
    server, session, _, dispatch = wanted_service
    session.gameplay_character = character_profile(51, "WantedPlayer")
    session.gameplay_inventory = InventoryInfo()
    session.gameplay_currency_state = CurrencyChanged(1000, 0, (0, 0, 0), 0, False)
    dispatch()
    expected = session.gameplay_wanted_thief
    saved = server._capture_account_state(session)
    assert WantedState.from_document(saved.wanted_thief) == expected
    saved.wanted_thief["mission_ids"][0] = 999
    assert session.gameplay_wanted_thief == expected



def test_wanted_event_reaches_initialization_without_an_announcement(wanted_service):
    from dso_server.services.events import event_notification_entries
    server, session, _, _ = wanted_service
    update, _, _ = server._event_initialization_state(session)
    wanted = [e for e in update.events if e.event_name == "event_wanted_thief_silent"]
    assert len(wanted) == 1 and wanted[0].flags[0] is True
    catalog = server._event_catalog()
    wanted_event = next(e for e in catalog.events if e.event_id == wanted[0].event_id)
    assert wanted_event.template.category == "Silent"
    from types import SimpleNamespace
    assert event_notification_entries(
        SimpleNamespace(active_events=lambda now: (wanted_event,)), now=server._wanted_now(),
    ) == ()


def test_retail_three_acceptances_update_quests_and_board_together(wanted_service, monkeypatch):
    from dso_server.gameplay.quest_content import QUEST_STATE_ACCEPTED
    from dso_server.protocol.quest_protocol import (
        decode_quest_batch, QUEST_UPDATE_COMMAND, QuestUpdate,
    )
    server, session, _, dispatch = wanted_service
    # These are the four offers from capture_20260909_000000_sample_11.log.
    session.gameplay_wanted_thief = WantedState(
        "2026-09-28", 10, 5, (23, 54, 52, 7), (0, 0, 0, 0))
    monkeypatch.setattr(server, "_quest_npc_info_commands", lambda s: ())
    monkeypatch.setattr(server, "_quest_fire_signal", lambda *args: None)
    snapshots = []
    def save(s, reason, flush):
        assert not s.flow_lock._is_owned()
        snapshots.append((server._wanted_thief_document(s), server._quest_log_document(s)))
    session.gameplay_persist_callback = save
    quest_ids = ("k0003-wanted-point-23", "pw207-wanted-point-54", "pw206-wanted-point-52")
    for slot, quest_id in enumerate(quest_ids, 1):
        session.recorder.messages.clear()
        dispatch("8b6601" + slot.to_bytes(4, "little").hex())
        board = session.gameplay_wanted_thief
        assert board.remaining == 10 - slot and board.special_remaining == 5
        assert board.mission_ids == (23, 54, 52, 7)
        assert board.statuses == (1,) * slot + (0,) * (4 - slot)
        assert session.gameplay_quest_log[quest_id].state == QUEST_STATE_ACCEPTED
        packet, bits, _ = session.recorder.messages[0]
        commands = decode_quest_batch(packet, bits)
        assert commands[0].command_id == QUEST_UPDATE_COMMAND
        assert commands[0].value == QuestUpdate.quest_state(
            quest_id, QUEST_STATE_ACCEPTED, "0000-00-00 00:00:00")
        assert session.recorder.messages[-1][2] == "wanted-thief-info"
        saved_board, saved_log = snapshots[-1]
        assert saved_board["remaining"] == 10 - slot
        assert set(saved_log) == set(quest_ids[:slot])
    # The third acceptance response has the same body as the later 12:08 capture.
    packet, bits, _ = session.recorder.messages[-1]
    assert bytes([0x85]) + packet[1:] + bytes([0xff]) == RETAIL_WANTED_INFO
    assert len(snapshots) == 3
    session.recorder.messages.clear()
    dispatch("8b660101000000")
    assert session.gameplay_wanted_thief.remaining == 7
    assert len(snapshots) == 3  # Duplicate acceptance never spends another attempt.
    assert len(session.recorder.messages) == 1


@pytest.mark.parametrize("slot,level,remaining", [
    (0, 100, 10), (4, 100, 10), (5, 100, 10), (0xffffffff, 100, 10),
    (1, 9, 10), (1, 100, 0),
])
def test_rejected_acceptance_never_changes_quest_or_counter(wanted_service, slot, level, remaining):
    _, session, saves, dispatch = wanted_service
    board = WantedState("2026-09-28", remaining, 5, (23, 54, 52, 7), (0, 0, 0, 0))
    session.gameplay_wanted_thief = board
    session.gameplay_character.level = level
    dispatch("8b6601" + slot.to_bytes(4, "little").hex())
    assert session.gameplay_wanted_thief == board
    assert not session.gameplay_quest_log and not saves
    assert len(session.recorder.messages) == 1


def test_generic_quest_accept_cannot_bypass_wanted_counter(wanted_service):
    server, session, saves, _ = wanted_service
    assert not server._quest_accept(session, "k0003-wanted-point-23", offered=True)
    assert not session.gameplay_quest_log and not saves


def test_daily_challenge_command_is_not_routed_to_wanted(wanted_service, monkeypatch):
    server, session, saves, dispatch = wanted_service
    def unexpected(*args):
        pytest.fail("Daily Challenge command was routed to Wanted")
    monkeypatch.setattr(server, "_handle_wanted_thief_command", unexpected)
    dispatch("8b940014007076705f77696e6d6174636865735f746965723200")
    assert session.gameplay_wanted_thief is None and not saves


def test_refresh_all_and_one_slot_debit_authored_prices(wanted_service):
    from dso_server.protocol.player_init_protocol import CurrencyChanged
    server, session, saves, dispatch = wanted_service
    session.gameplay_currency_state = CurrencyChanged(500, 0, (0,0,0), 0, False)
    dispatch()
    before = session.gameplay_wanted_thief
    dispatch('8b670100000000')
    after = session.gameplay_wanted_thief
    assert all(a != b for a, b in zip(before.mission_ids, after.mission_ids))
    assert session.gameplay_currency_state.andermant == 400
    before = after
    dispatch('8b670102000000')
    after = session.gameplay_wanted_thief
    assert after.mission_ids[1] != before.mission_ids[1]
    assert after.mission_ids[0] == before.mission_ids[0] and after.mission_ids[2:] == before.mission_ids[2:]
    assert session.gameplay_currency_state.andermant == 370


def test_refresh_cannot_replace_accepted_slots_or_overdraw(wanted_service):
    from dataclasses import replace
    from dso_server.protocol.player_init_protocol import CurrencyChanged
    _, session, _, dispatch = wanted_service
    dispatch()
    original = replace(session.gameplay_wanted_thief, statuses=(1,1,1,1))
    session.gameplay_wanted_thief = original
    session.gameplay_currency_state = CurrencyChanged(200, 0, (0,0,0), 0, False)
    dispatch('8b670100000000')
    assert session.gameplay_wanted_thief == original and session.gameplay_currency_state.andermant == 200
    session.gameplay_wanted_thief = replace(original, statuses=(0,0,0,0))
    original = session.gameplay_wanted_thief
    session.gameplay_currency_state = replace(session.gameplay_currency_state, currency_type=29)
    dispatch('8b670101000000')
    assert session.gameplay_wanted_thief == original and session.gameplay_currency_state.andermant == 29
