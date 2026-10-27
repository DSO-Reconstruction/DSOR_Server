"""Recipe scroll use, persistence, and workbench authorization use real content."""

from dataclasses import replace
import threading
import unittest
from unittest.mock import Mock

import pytest

from dso_server.gameplay.achievement_content import AchievementProgress
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence.account_state import _state_from_document, _state_to_document
from dso_server.protocol.gameplay_protocol import InventoryItemAction, encode_inventory_item_action
from dso_server.protocol.inventory_codec import ItemInfo
from dso_server.protocol.player_init_protocol import (
    INVENTORY_INFO_COMMAND, RECIPE_BOOK_INFO_COMMAND, decode_server_command_batch,
)
from dso_server.runtime.transport import Session
from dso_server.services import recipe_progression
from dso_server.services.character_server import CharacterServer
from dso_server.services.recipe_progression import validate_crafting_recipe_access
from dso_server.tests.fixtures.characters import character_server


FOOD_BOOK = "recipe_cooking_food_carrotjuice"
POTION_BOOK = "recipe_potion_all_rarity_02_unlock_01"
POTION_RECIPE = "potion_combined_health_rarity_02"


def initialize(server, port=47921):
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


@pytest.fixture
def player(monkeypatch):
    case = unittest.TestCase()
    try:
        server = character_server(case)
        session = initialize(server)
        sent, saved = Mock(), Mock()
        monkeypatch.setattr(CharacterServer, "_send_world_loot_state_batch", sent)
        monkeypatch.setattr(CharacterServer, "_request_account_save", saved)
        yield server, session, sent, saved
    finally:
        case.doCleanups()


def add_book(session, template_id=FOOD_BOOK, item_id=910001):
    session.gameplay_inventory.items.append(ItemInfo(
        item_id=item_id, template_id=template_id, amount=1, item_level=1,
    ))
    session.gameplay_inventory.storage_58[item_id] = 10 + item_id - 910001
    session.gameplay_next_item_id = max(session.gameplay_next_item_id, item_id + 1)
    return item_id


def use(server, session, item_id):
    payload, bits = encode_inventory_item_action(InventoryItemAction(item_id, 0x13, 0, 0, 0))
    session.current_message_bit_length = bits
    server._handle_inventory_item_action(session, payload)


def reconnect(server, session, resources=None):
    state = server._capture_account_state(session)
    if resources is not None:
        state = replace(state, recipe_book_resources=resources)
    # Both recipe and inventory state traverse the real persisted JSON codec.
    state = _state_from_document(_state_to_document(state))
    server.account_store.put(state, flush=True)
    return initialize(server, 47922)


def test_new_and_persisted_empty_characters_do_not_receive_recipe_unlocks(player):
    server, session, _, _ = player
    assert session.gameplay_recipe_book_resources == ()
    assert reconnect(server, session).gameplay_recipe_book_resources == ()


def test_existing_unlocks_survive_initialization_including_old_content(player):
    server, session, _, _ = player
    existing = (FOOD_BOOK, POTION_BOOK, "legacy_recipe_unlock")
    assert reconnect(server, session, existing).gameplay_recipe_book_resources == existing


@pytest.mark.parametrize("book", [FOOD_BOOK, POTION_BOOK])
def test_authored_recipe_item_use_consumes_unlocks_publishes_and_survives_relog(player, book):
    server, session, sent, saved = player
    item_id = add_book(session, book)
    use(server, session, item_id)
    assert session.gameplay_recipe_book_resources == (book,)
    assert item_id not in {item.item_id for item in session.gameplay_inventory.items}
    assert item_id not in session.gameplay_inventory.storage_58
    sent.assert_called_once()
    commands = decode_server_command_batch(*sent.call_args.args[1:])
    assert [command.command_id for command in commands] == [
        INVENTORY_INFO_COMMAND, RECIPE_BOOK_INFO_COMMAND, INVENTORY_INFO_COMMAND,
    ]
    assert [(entry.recipe_id, entry.value) for entry in commands[1].value.recipes] == [(book, book)]
    saved.assert_called_once_with(session, "recipe book unlocked", flush=True)
    restored = reconnect(server, session)
    assert restored.gameplay_recipe_book_resources == (book,)
    assert item_id not in {item.item_id for item in restored.gameplay_inventory.items}


def test_two_concurrent_uses_of_same_recipe_consume_only_one_copy(player):
    server, session, sent, saved = player
    item_ids = (add_book(session), add_book(session, item_id=910002))
    barrier = threading.Barrier(2)
    failures = []

    def worker(item_id):
        try:
            barrier.wait(timeout=2)
            use(server, session, item_id)
        except BaseException as exc:
            failures.append(exc)

    workers = [threading.Thread(target=worker, args=(item_id,)) for item_id in item_ids]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert not failures
    assert session.gameplay_recipe_book_resources == (FOOD_BOOK,)
    assert len(set(item_ids) & {item.item_id for item in session.gameplay_inventory.items}) == 1
    sent.assert_called_once()
    saved.assert_called_once()
    # A later duplicate is still harmless after the first response was delivered.
    remaining = next(item_id for item_id in item_ids
                     if any(item.item_id == item_id for item in session.gameplay_inventory.items))
    use(server, session, remaining)
    sent.assert_called_once()
    saved.assert_called_once()


@pytest.mark.parametrize("book,level,class_id", [
    ("recipe_multiple_basic_crafting_intro_unlock", 1, 1),
    ("recipe_multiple_dwarf_gnob_1h_weapon_unlock_01", 100, 1),
    ("recipe_gem_test", 100, 1),
])
def test_ineligible_or_obsolete_scrolls_are_not_consumed(player, book, level, class_id):
    server, session, sent, saved = player
    session.gameplay_character.level = level
    session.gameplay_character.class_id = class_id
    item_id = add_book(session, book)
    use(server, session, item_id)
    assert session.gameplay_recipe_book_resources == ()
    assert item_id in {item.item_id for item in session.gameplay_inventory.items}
    sent.assert_not_called()
    saved.assert_not_called()


def test_encoding_failure_cannot_consume_book_without_granting_unlock(player, monkeypatch):
    server, session, sent, saved = player
    item_id = add_book(session)
    monkeypatch.setattr(recipe_progression, "encode_server_command_batch", Mock(side_effect=ValueError("bad encoding")))
    with pytest.raises(ValueError, match="bad encoding"):
        use(server, session, item_id)
    assert session.gameplay_recipe_book_resources == ()
    assert item_id in {item.item_id for item in session.gameplay_inventory.items}
    sent.assert_not_called()
    saved.assert_not_called()


def test_outbound_failure_happens_after_durable_unlock_and_use_hook(player, monkeypatch):
    server, session, sent, saved = player
    item_id = add_book(session)
    calls = []
    saved.side_effect = lambda *_a, **_k: calls.append("save")
    hook = Mock(side_effect=lambda *_a, **_k: calls.append("use"))
    monkeypatch.setattr(CharacterServer, "_achievement_feed", hook)

    def fail_send(*_args, **_kwargs):
        calls.append("send")
        raise OSError("output queue unavailable")

    sent.side_effect = fail_send
    with pytest.raises(OSError, match="output queue"):
        use(server, session, item_id)
    assert calls == ["save", "use", "send"]
    hook.assert_called_once_with(server, session, "use", (FOOD_BOOK,), count=1, deferred_packets=[])
    assert session.gameplay_recipe_book_resources == (FOOD_BOOK,)
    assert item_id not in {item.item_id for item in session.gameplay_inventory.items}


def test_workbench_requires_both_learned_book_and_completed_achievement(player):
    _, session, _, _ = player

    def validate():
        validate_crafting_recipe_access(session, POTION_RECIPE, DEFAULT_STATIC_DB_PATH)

    with pytest.raises(ValueError, match="recipe book"):
        validate()
    session.gameplay_recipe_book_resources = (POTION_BOOK,)
    with pytest.raises(ValueError, match="achievement"):
        validate()
    session.gameplay_achievements["crafting_potion_rarity_01"] = AchievementProgress(progress=999999)
    with pytest.raises(ValueError, match="achievement"):
        validate()
    session.gameplay_achievements["crafting_potion_rarity_01"].completed_at = "2026-10-26 10:00:00"
    validate()
    session.gameplay_character.level = 44
    with pytest.raises(ValueError, match="level"):
        validate()


def test_learned_recipe_allows_client_decimal_selector_but_rejects_unknown_recipe(player):
    _, session, _, _ = player
    session.gameplay_recipe_book_resources = (FOOD_BOOK,)
    for recipe_id in ("cooking_food_carrotjuice", "cooking_food_carrotjuice0"):
        validate_crafting_recipe_access(session, recipe_id, DEFAULT_STATIC_DB_PATH)
    with pytest.raises(ValueError, match="unknown or locked"):
        validate_crafting_recipe_access(session, "not_a_recipe", DEFAULT_STATIC_DB_PATH)
