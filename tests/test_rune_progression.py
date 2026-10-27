"""Earned rune extensions preserve old slot entitlements without free new ones."""

import copy
from dataclasses import replace
import threading
import unittest
from unittest.mock import Mock

import pytest

from dso_server.gameplay.achievement_content import AchievementCatalog, AchievementProgress
from dso_server.persistence.account_state import _state_from_document, _state_to_document
from dso_server.protocol.gameplay_protocol import InventoryItemAction, encode_inventory_item_action
from dso_server.protocol.inventory_codec import ItemInfo
from dso_server.protocol.player_init_protocol import decode_server_command_batch
from dso_server.services import rune_progression
from dso_server.services.character_server import CharacterServer
from dso_server.services.rune_progression import (
    EXTRA_RUNE_SLOTS, RUNE_EXTENSION_ITEM, RUNE_SLOT_ACHIEVEMENTS, unlocked_rune_slots,
)
from dso_server.tests.fixtures.characters import character_server
from dso_server.tests.test_recipe_progression import initialize


@pytest.fixture
def player(monkeypatch):
    case = unittest.TestCase()
    try:
        server = character_server(case)
        session = initialize(server, 47941)
        session.gameplay_persist_callback = lambda s, reason, flush: server._persist_gameplay_session(s, reason=reason, flush=flush)
        sent = Mock()
        monkeypatch.setattr(CharacterServer, "_send_world_loot_state_batch", sent)
        yield server, session, sent
    finally:
        case.doCleanups()


def add_item(session, template=RUNE_EXTENSION_ITEM, amount=1, *, equipped=None):
    item_id = session.gameplay_next_item_id
    session.gameplay_next_item_id += 1
    item = ItemInfo(item_id=item_id, template_id=template, amount=amount, item_level=1)
    session.gameplay_inventory.items.append(item)
    if equipped is None:
        session.gameplay_inventory.storage_58[item_id] = max(session.gameplay_inventory.storage_58.values(), default=-1) + 1
    else:
        session.gameplay_inventory.equipment_slots[item_id] = (equipped,)
    return item_id


def act(server, session, item_id, operation=0x13, argument=None):
    action = operation if argument is None else operation | 0x100
    payload, bits = encode_inventory_item_action(InventoryItemAction(item_id, action, 0, argument or 0, 0))
    session.current_message_bit_length = bits
    server._handle_inventory_item_action(session, payload)


def saved_state(server, session):
    return server.account_store.get(session.gameplay_character.character_id)


def test_new_character_has_five_rune_trinkets_and_no_extra_entitlements(player):
    _, session, _ = player
    rune_slots = {slot for slots in session.gameplay_inventory.equipment_slots.values()
                  for slot in slots if 13 <= slot <= 19}
    assert rune_slots == {13, 14, 15, 16, 17}
    assert unlocked_rune_slots(session) == frozenset()
    assert not any(key in session.gameplay_achievements for key in RUNE_SLOT_ACHIEVEMENTS)


def test_two_stones_unlock_in_order_advance_use_and_persist_once_each(player):
    server, session, sent = player
    item_id = add_item(session, amount=3)
    for expected_count in (1, 2):
        act(server, session, item_id)
        assert unlocked_rune_slots(session) == frozenset(EXTRA_RUNE_SLOTS[:expected_count])
        assert next(item for item in session.gameplay_inventory.items if item.item_id == item_id).amount == 3 - expected_count
        assert session.gameplay_achievements[RUNE_SLOT_ACHIEVEMENTS[expected_count - 1]].completed
        saved = saved_state(server, session)
        assert saved.rune_trinket_unlocked_slots == EXTRA_RUNE_SLOTS[:expected_count]
        assert saved.achievements[RUNE_SLOT_ACHIEVEMENTS[expected_count - 1]]["completed_at"]
    before = copy.deepcopy(session.gameplay_inventory)
    sent.reset_mock()
    act(server, session, item_id)
    assert session.gameplay_inventory == before
    sent.assert_not_called()
    assert session.gameplay_achievements[RUNE_SLOT_ACHIEVEMENTS[1]].progress == 2


def test_unlock_packet_creates_equipped_base_trinket_even_with_full_bags(player):
    server, session, sent = player
    item_id = add_item(session, amount=2)
    session.gameplay_inventory.storage_size = len(session.gameplay_inventory.storage_58)
    session.gameplay_inventory.extra_storage_size = 0
    act(server, session, item_id)
    call = next(call for call in sent.call_args_list if call.kwargs.get("label") == "rune-trinket-unlocked")
    commands = decode_server_command_batch(*call.args[1:])
    trinket = next(item for item in commands[0].value.items if item.template_id == "all_base_trinket_rune_01")
    assert trinket.socket_count == 5 and trinket.socketed_templates == [""] * 5
    assert commands[0].value.equipment_slots[trinket.item_id] == (18,)
    assert commands[1].value.equipment_slots[trinket.item_id] == (18,)


def test_stone_requires_authored_level_100(player):
    server, session, sent = player
    session.gameplay_character.level = 99
    item_id = add_item(session)
    before = copy.deepcopy(session.gameplay_inventory)
    act(server, session, item_id)
    assert session.gameplay_inventory == before
    assert unlocked_rune_slots(session) == frozenset()
    sent.assert_not_called()


def test_outbound_failure_keeps_stone_consumption_slot_and_achievement_durable(player):
    server, session, sent = player
    item_id = add_item(session)
    sent.side_effect = OSError("output unavailable")
    with pytest.raises(OSError, match="output unavailable"):
        act(server, session, item_id)
    saved = saved_state(server, session)
    assert saved.rune_trinket_unlocked_slots == (18,)
    assert item_id not in {item.item_id for item in saved.inventory.items}
    assert saved.achievements[RUNE_SLOT_ACHIEVEMENTS[0]]["completed_at"]


def test_use_reward_packets_follow_unlock_inventory_without_reverting_allocations(player, monkeypatch):
    server, session, sent = player
    definition = replace(
        server._achievement_catalog().achievement(RUNE_SLOT_ACHIEVEMENTS[0]),
        reward_item="ingredient_gem_dust", reward_amount=1,
    )
    catalog = AchievementCatalog({definition.key: definition}, {"use": (definition,)})
    monkeypatch.setattr(server, "_achievement_catalog", lambda: catalog)
    item_id = add_item(session)
    act(server, session, item_id)
    assert [call.kwargs["label"] for call in sent.call_args_list] == [
        "rune-trinket-unlocked", "achievement-reward", "achievement-use",
    ]
    rewards = decode_server_command_batch(*sent.call_args_list[1].args[1:])
    reward_id = next(item.item_id for item in rewards[0].value.items
                     if item.template_id == "ingredient_gem_dust")
    assert reward_id in rewards[1].value.field_180
    assert (18,) in rewards[1].value.equipment_slots.values()
    saved = saved_state(server, session)
    assert saved.achievements[definition.key]["reward_status"] == "granted"
    assert reward_id in {item.item_id for item in saved.inventory.items}


def test_encoding_failure_is_atomic(player, monkeypatch):
    server, session, sent = player
    item_id = add_item(session)
    before = copy.deepcopy(session.gameplay_inventory)
    monkeypatch.setattr(rune_progression, "encode_server_command_batch", Mock(side_effect=ValueError("encode failed")))
    with pytest.raises(ValueError, match="encode failed"):
        act(server, session, item_id)
    assert session.gameplay_inventory == before
    assert not unlocked_rune_slots(session)
    sent.assert_not_called()


def test_concurrent_stones_cannot_consume_past_two_slots(player):
    server, session, _ = player
    item_ids = [add_item(session) for _ in range(3)]
    barrier = threading.Barrier(3)
    failures = []

    def worker(item_id):
        try:
            barrier.wait(timeout=2)
            act(server, session, item_id)
        except BaseException as exc:
            failures.append(exc)

    workers = [threading.Thread(target=worker, args=(item_id,)) for item_id in item_ids]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert not failures
    assert unlocked_rune_slots(session) == frozenset(EXTRA_RUNE_SLOTS)
    assert len(set(item_ids) & {item.item_id for item in session.gameplay_inventory.items}) == 1
    assert session.gameplay_achievements[RUNE_SLOT_ACHIEVEMENTS[1]].progress == 2


def test_explicit_equip_cannot_bypass_locked_slot_and_automatic_equip_uses_base_slot(player):
    server, session, _ = player
    item_id = add_item(session, "all_base_trinket_rune_01")
    for slot in EXTRA_RUNE_SLOTS:
        act(server, session, item_id, 0x01, slot)
        assert item_id not in session.gameplay_inventory.equipment_slots
    act(server, session, item_id, 0x01)
    assert session.gameplay_inventory.equipment_slots[item_id] == (13,)
    assert not unlocked_rune_slots(session)


def test_legacy_occupied_slot_stays_authorized_after_unequip_save_and_relog(player):
    server, session, _ = player
    legacy_id = add_item(session, "all_base_trinket_rune_01", equipped=18)
    act(server, session, legacy_id, 0x02)
    assert legacy_id not in session.gameplay_inventory.equipment_slots
    saved = _state_from_document(_state_to_document(saved_state(server, session)))
    assert saved.rune_trinket_unlocked_slots == (18,)
    assert not saved.achievements.get(RUNE_SLOT_ACHIEVEMENTS[0], {}).get("completed_at")
    server.account_store.put(saved, flush=True)
    restored = initialize(server, 47942)
    assert unlocked_rune_slots(restored) == frozenset({18})
    # Map initialization intentionally rekeys every inventory item.
    restored_id = next(item.item_id for item in restored.gameplay_inventory.items
                       if item.template_id == "all_base_trinket_rune_01"
                       and item.item_id in restored.gameplay_inventory.storage_58)
    act(server, restored, restored_id, 0x01, 18)
    assert restored.gameplay_inventory.equipment_slots[restored_id] == (18,)


@pytest.mark.parametrize("slot,key", zip(EXTRA_RUNE_SLOTS, RUNE_SLOT_ACHIEVEMENTS))
def test_saved_completion_authorizes_its_slot_without_consuming_stone(player, slot, key):
    server, session, _ = player
    session.gameplay_achievements[key] = AchievementProgress(progress=slot - 17, completed_at="2026-10-20 12:00:00")
    item_id = add_item(session, "all_base_trinket_rune_01")
    act(server, session, item_id, 0x01, slot)
    assert session.gameplay_inventory.equipment_slots[item_id] == (slot,)
    assert slot in saved_state(server, session).rune_trinket_unlocked_slots
