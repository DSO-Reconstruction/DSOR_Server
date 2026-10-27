"""Current client wire, authored yields, and the durable food-recycling path."""

import copy
from dataclasses import replace
import struct
import threading
import unittest
from unittest.mock import Mock

import pytest

from dso_server.gameplay.achievement_content import AchievementCatalog
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.currency_protocol import GAME_CURRENCY_CHANGED_COMMAND
from dso_server.protocol.food_recycling_protocol import (
    SALVAGE_COOK_ITEM_COMMAND, FoodRecyclingRequest,
    decode_food_recycling_request, load_food_recycling_catalog,
)
from dso_server.protocol.inventory_codec import ItemInfo
from dso_server.protocol.player_init_protocol import INVENTORY_INFO_COMMAND, decode_server_command_batch
from dso_server.services import food_recycling
from dso_server.services.character_server import CharacterServer
from dso_server.tests.fixtures.characters import character_server
from dso_server.tests.test_recipe_progression import initialize


FISH = "item_cooking_food_tomatosaucefriedfishstrips"
STEAK = "item_cooking_food_bbqsaucesteak"
PLATTER = "item_cooking_food_seaandlandplatter"


def request(*item_ids):
    return b"\x8b" + struct.pack("<HI", SALVAGE_COOK_ITEM_COMMAND, len(item_ids)) + b"".join(
        struct.pack("<I", item_id) for item_id in item_ids
    )


def test_current_client_array_serializer_wire_layout():
    # Recovered at encoder 0x140985564 -> u32-array writer 0x140960df0.
    payload = bytes.fromhex("8b3200020000004433221188776655")
    assert SALVAGE_COOK_ITEM_COMMAND == 0x32
    assert decode_food_recycling_request(payload) == FoodRecyclingRequest((0x11223344, 0x55667788))
    assert request(0x11223344, 0x55667788) == payload


@pytest.mark.parametrize("payload", [
    b"", b"\x85\x32\x00", b"\x8b\x31\x00\x01\x00\x00\x00\x01\x00\x00\x00",
    request(), request(1, 1), request(0), request(*range(1, 11)),
    request(1)[:-1], request(1) + b"\x00", request(1) + b"\x00" * 4,
    b"\x8b\x32\x00\xff\xff\xff\xff",
])
def test_malformed_or_unsupported_arrays_are_rejected(payload):
    with pytest.raises(ValueError):
        decode_food_recycling_request(payload)


def test_authored_food_yields_follow_price_cc_not_consumable_category():
    catalog = load_food_recycling_catalog(DEFAULT_STATIC_DB_PATH)
    assert catalog.currency_id == "money_cc"
    assert {key: catalog.unit_yields[key] for key in (FISH, STEAK, PLATTER)} == {
        FISH: 2, STEAK: 3, PLATTER: 50,
    }
    assert "item_cooking_food_carrotjuice" not in catalog.unit_yields
    assert "potion_health_restore_rarity_01" not in catalog.unit_yields


@pytest.fixture
def player(monkeypatch):
    case = unittest.TestCase()
    try:
        server = character_server(case)
        session = initialize(server, 47961)
        session.gameplay_mode = True
        session.gameplay_persist_callback = lambda s, reason, flush: server._persist_gameplay_session(s, reason=reason, flush=flush)
        npc = next(npc for npc in server._active_npc_definitions(session) if npc.profession == "food_recycling")
        session.gameplay_spawn_position = npc.position
        session.gameplay_last_move = None
        session.gameplay_game_currencies["money_cc"] = 10
        sent = Mock()
        monkeypatch.setattr(CharacterServer, "_send_world_loot_state_batch", sent)
        yield server, session, sent, npc
    finally:
        case.doCleanups()


def add_item(session, template_id=FISH, amount=1):
    item_id = session.gameplay_next_item_id
    session.gameplay_next_item_id += 1
    session.gameplay_inventory.items.append(ItemInfo(
        item_id=item_id, template_id=template_id, amount=amount, item_level=1,
    ))
    session.gameplay_inventory.storage_58[item_id] = max(session.gameplay_inventory.storage_58.values(), default=-1) + 1
    return item_id


def snapshot(session):
    return copy.deepcopy((session.gameplay_inventory, session.gameplay_game_currencies,
                          session.gameplay_next_item_id, session.gameplay_achievements))


def test_dispatch_converts_full_stacks_and_persists_before_response(player):
    server, session, sent, _ = player
    item_ids = (add_item(session, FISH, 4), add_item(session, STEAK, 3), add_item(session, PLATTER, 2))

    def inspect_saved(*_args, **_kwargs):
        saved = server.account_store.get(session.gameplay_character.character_id)
        assert saved.game_currencies["money_cc"] == 127
        assert not set(item_ids).intersection(item.item_id for item in saved.inventory.items)
        assert saved.achievements["collected_cook_currency_01"]["progress"] == 117

    sent.side_effect = inspect_saved
    payload = request(*item_ids)
    server._dispatch_char_op_now(session, payload)
    assert session.gameplay_game_currencies["money_cc"] == 127
    assert session.gameplay_client_command_state[SALVAGE_COOK_ITEM_COMMAND].item_ids == item_ids
    first = sent.call_args_list[0]
    assert first.kwargs["label"] == "food-recycled"
    commands = decode_server_command_batch(*first.args[1:])
    assert [command.command_id for command in commands] == [
        INVENTORY_INFO_COMMAND, GAME_CURRENCY_CHANGED_COMMAND, INVENTORY_INFO_COMMAND,
    ]
    assert commands[0].value.changed_item_ids == list(item_ids)
    assert commands[1].value.resource_id == "money_cc" and commands[1].value.amount == 127
    assert not set(item_ids).intersection(commands[2].value.storage_58)
    saved_stamp = server.account_store.get(session.gameplay_character.character_id).saved_at
    sent.reset_mock()
    server._dispatch_char_op_now(session, payload)
    sent.assert_not_called()
    assert session.gameplay_game_currencies["money_cc"] == 127
    assert server.account_store.get(session.gameplay_character.character_id).saved_at == saved_stamp
    restored = initialize(server, 47962)
    assert restored.gameplay_game_currencies["money_cc"] == 127
    assert not any(item.template_id in (FISH, STEAK, PLATTER) for item in restored.gameplay_inventory.items)


@pytest.mark.parametrize("invalid", ["unknown", "zero-yield", "equipped", "overflow", "zero-amount"])
def test_invalid_mixed_batch_has_no_partial_consumption_or_credit(player, invalid):
    server, session, sent, _ = player
    valid = add_item(session, STEAK, 2)
    bad = add_item(session, FISH, 1)
    if invalid == "unknown":
        bad += 10000
    elif invalid == "zero-yield":
        next(item for item in session.gameplay_inventory.items if item.item_id == bad).template_id = "item_cooking_food_carrotjuice"
    elif invalid == "equipped":
        session.gameplay_inventory.storage_58.pop(bad)
        session.gameplay_inventory.equipment_slots[bad] = (37,)
    elif invalid == "overflow":
        item = next(item for item in session.gameplay_inventory.items if item.item_id == bad)
        session.gameplay_inventory.items.remove(item)
        session.gameplay_inventory.storage_58.pop(bad)
        session.gameplay_inventory.premium_inventory_overflow.append(item)
    else:
        next(item for item in session.gameplay_inventory.items if item.item_id == bad).amount = 0
    before = snapshot(session)
    server._handle_food_recycling(session, request(valid, bad))
    assert snapshot(session) == before
    sent.assert_not_called()


@pytest.mark.parametrize("reason", ["far", "missing", "wrong-profession", "no-position", "transition"])
def test_requires_current_active_recycling_npc_in_range(player, monkeypatch, reason):
    server, session, sent, npc = player
    item_id = add_item(session)
    if reason == "far":
        session.gameplay_spawn_position = (npc.position[0] + 8.1, npc.position[1], npc.position[2])
    elif reason == "missing":
        monkeypatch.setattr(server, "_active_npc_definitions", lambda _s: ())
    elif reason == "wrong-profession":
        monkeypatch.setattr(server, "_active_npc_definitions", lambda _s: (replace(npc, profession="smith"),))
    elif reason == "no-position":
        session.gameplay_spawn_position = None
    else:
        session.gameplay_exit_transition_started = True
    # A previously opened shop must never substitute for current eligibility.
    session.gameplay_interaction_npc_shop_id = "cooking_currency"
    before = snapshot(session)
    server._handle_food_recycling(session, request(item_id))
    assert snapshot(session) == before
    sent.assert_not_called()


def test_currency_overflow_and_encoding_failure_do_not_consume(player, monkeypatch):
    server, session, sent, _ = player
    item_id = add_item(session)
    session.gameplay_game_currencies["money_cc"] = 0xFFFFFFFFFFFFFFFF
    before = snapshot(session)
    server._handle_food_recycling(session, request(item_id))
    assert snapshot(session) == before
    session.gameplay_game_currencies["money_cc"] = 10
    before = snapshot(session)
    monkeypatch.setattr(food_recycling, "encode_server_command_batch", Mock(side_effect=ValueError("bad encoding")))
    server._handle_food_recycling(session, request(item_id))
    assert snapshot(session) == before
    sent.assert_not_called()


def test_failed_publication_still_saves_credit_consumption_and_earned_currency(player):
    server, session, sent, _ = player
    item_id = add_item(session, PLATTER, 2)
    sent.side_effect = OSError("outbound unavailable")
    with pytest.raises(OSError, match="outbound unavailable"):
        server._handle_food_recycling(session, request(item_id))
    saved = server.account_store.get(session.gameplay_character.character_id)
    assert saved.game_currencies["money_cc"] == 110
    assert saved.achievements["collected_cook_currency_01"]["progress"] == 100
    assert item_id not in {item.item_id for item in saved.inventory.items}
    server._handle_food_recycling(session, request(item_id))
    assert session.gameplay_game_currencies["money_cc"] == 110
    assert session.gameplay_achievements["collected_cook_currency_01"].progress == 100


def test_concurrent_duplicate_requests_credit_owned_stacks_only_once(player):
    server, session, _, _ = player
    payload = request(add_item(session, STEAK, 4))
    barrier = threading.Barrier(2)
    failures = []

    def worker():
        try:
            barrier.wait(timeout=2)
            server._handle_food_recycling(session, payload)
        except BaseException as exc:
            failures.append(exc)

    workers = [threading.Thread(target=worker) for _ in range(2)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert not failures
    assert session.gameplay_game_currencies["money_cc"] == 22
    assert session.gameplay_achievements["collected_cook_currency_01"].progress == 12


def test_achievement_reward_inventory_follows_recycling_snapshot(player, monkeypatch):
    server, session, sent, _ = player
    definition = replace(
        server._achievement_catalog().achievement("collected_cook_currency_01"),
        amount=1, reward_item="ingredient_gem_dust", reward_amount=1,
    )
    catalog = AchievementCatalog({definition.key: definition}, {"collectedgamecurrency": (definition,)})
    monkeypatch.setattr(server, "_achievement_catalog", lambda: catalog)
    server._handle_food_recycling(session, request(add_item(session)))
    assert [call.kwargs["label"] for call in sent.call_args_list] == [
        "food-recycled", "achievement-reward", "achievement-collectedgamecurrency",
    ]
    saved = server.account_store.get(session.gameplay_character.character_id)
    assert saved.achievements[definition.key]["reward_status"] == "granted"
    assert any(item.template_id == "ingredient_gem_dust" for item in saved.inventory.items)
