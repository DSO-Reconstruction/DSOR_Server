"""Accepted skill deadlines survive the actual character save/load path."""

import unittest
from unittest.mock import Mock

import pytest

from dso_server.gameplay.player_skill_timing import (
    restore_player_skill_timing, snapshot_player_skill_timing,
)
from dso_server.persistence.account_state import _state_from_document, _state_to_document
from dso_server.runtime.transport import Session
from dso_server.services import player_init_state, player_persistence
from dso_server.tests.fixtures.characters import character_server


@pytest.fixture
def player_server():
    case = unittest.TestCase()
    try:
        yield character_server(case)
    finally:
        case.doCleanups()


def initialize(server, port):
    session = Session(("127.0.0.1", port), Mock())
    session.gameplay_character = server.character_selection.active_character
    session.gameplay_map = "a0200_kingscity"
    state = server._resolve_initialization_character(session)
    server._resolve_initialization_inventory(state)
    server._encode_initialization_new_player(state)
    server._resolve_initialization_talents(state)
    server._resolve_initialization_persistent_effects(session, state)
    server._resolve_initialization_vitals(state)
    server._apply_initialization_state(session, state)
    return session


@pytest.mark.parametrize("elapsed", [20.0, 61.0])
def test_save_and_reconnect_keep_only_unexpired_player_skill_deadlines(
    player_server, monkeypatch, elapsed,
):
    original = initialize(player_server, 47901)
    epoch = 1_800_000_000.0
    document = {"cooldowns": {"teleport": epoch + 60}, "cast_ready_at": epoch + 0.75}
    original.gameplay_player_skill_timing = restore_player_skill_timing(
        document, now=1000.0, now_epoch=epoch,
    )
    monkeypatch.setattr(
        player_persistence, "snapshot_player_skill_timing",
        lambda state: snapshot_player_skill_timing(state, now=1000.0, now_epoch=epoch),
    )
    saved = player_server._capture_account_state(original)
    assert saved is not None
    assert saved.player_skill_timing["cooldowns"] == document["cooldowns"]
    assert saved.player_skill_timing["cast_ready_at"] == document["cast_ready_at"]
    # Exercise the JSON codec, not just the in-memory AccountState object.
    restored = _state_from_document(_state_to_document(saved))
    player_server.account_store.put(restored, flush=True)
    monkeypatch.setattr(
        player_init_state, "restore_player_skill_timing",
        lambda value: restore_player_skill_timing(value, now=2000.0, now_epoch=epoch + elapsed),
    )
    reconnected = initialize(player_server, 47902)
    current = snapshot_player_skill_timing(
        reconnected.gameplay_player_skill_timing, now=2000.0, now_epoch=epoch + elapsed,
    )
    assert current.get("cooldowns", {}) == (
        {"teleport": epoch + 60} if elapsed < 60 else {}
    )
    assert current.get("cast_ready_at", 0) <= epoch + elapsed
    assert reconnected.gameplay_player_skill_timing is not original.gameplay_player_skill_timing


def test_legacy_character_save_has_no_invented_skill_cooldowns(player_server):
    session = initialize(player_server, 47903)
    saved = player_server._capture_account_state(session)
    document = _state_to_document(saved)
    document.pop("player_skill_timing")
    restored = _state_from_document(document)
    assert restored.player_skill_timing == {}
    player_server.account_store.put(restored, flush=True)
    reconnected = initialize(player_server, 47904)
    current = snapshot_player_skill_timing(reconnected.gameplay_player_skill_timing)
    assert current.get("cooldowns", {}) == {}
