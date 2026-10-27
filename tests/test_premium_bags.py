"""Premium bag expiry never deletes items or exposes unavailable overflow."""

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dso_server.gameplay.premium_bags import reconcile_premium_bags
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence.account_state import (
    AccountStateStore, _inventory_from_document, _inventory_to_document,
)
from dso_server.protocol.commerce_protocol import PurchaseItemDefinition, add_purchased_item
from dso_server.protocol.gameplay_protocol import InventoryItemAction, encode_inventory_item_action
from dso_server.protocol.inventory_codec import (
    INVENTORY_INFO_COMMAND, InventoryInfo, ItemInfo, ItemEnchantmentInfo,
    clone_inventory_info, consume_equipped_stack, decode_inventory_info,
    encode_inventory_info, equip_inventory_item, owned_inventory_items, rekey_inventory_items,
)
from dso_server.protocol.inventory_economy_protocol import InventoryMutationBuilder
from dso_server.protocol.membership_protocol import MEMBERSHIP_UPDATE_COMMAND, membership_is_active
from dso_server.protocol.player_init_protocol import decode_server_command_batch
from dso_server.protocol.world_loot_protocol import WorldLootDrop, add_world_drop_to_inventory
from dso_server.runtime.transport import RakNetServer
from dso_server.services.character_server import CharacterServer
from dso_server.tests.test_pvp_matchmaking import _session
from dso_server.tests.test_ranking import state as account_state


NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)
PAST = "2000-01-01T00:00:00Z"
FUTURE = "2099-01-01T00:00:00Z"


def full_bags():
    return InventoryInfo(
        items=[ItemInfo(i, template_id=f"item_{i}") for i in range(1, 7)],
        storage_size=1, bank_storage_size=1,
        extra_storage_size=28, extra_bank_storage_size=40,
        storage_58={1: 0}, storage_80={3: 0, 4: 1},
        storage_a8={2: 0}, storage_d0={5: 0, 6: 1},
    )


@pytest.mark.parametrize("tier,enabled,offset,expected", [
    (1, True, 1, True), (2, True, 1, True),
    (0, True, 1, False), (1, False, 1, False), (2, False, 1, False),
    (1, True, 0, False), (2, True, -1, False),
])
def test_membership_requires_paid_active_unexpired_tier(tier, enabled, offset, expected):
    active = membership_is_active(tier, enabled, (NOW + timedelta(seconds=offset)).isoformat(), now=NOW)
    assert active is expected
    inventory = InventoryInfo()
    reconcile_premium_bags(inventory, active=active)
    assert (inventory.extra_storage_size, inventory.extra_bank_storage_size) == (
        (28, 40) if expected else (0, 0)
    )
    assert (inventory.storage_size, inventory.bank_storage_size) == (224, 270)


@pytest.mark.parametrize("storage_name,last_slot", [("storage_80", 27), ("storage_d0", 39)])
def test_premium_bags_accept_exactly_one_standard_bag(storage_name, last_slot):
    inventory = InventoryInfo(items=[ItemInfo(1)], storage_58={1: 0})
    reconcile_premium_bags(inventory, active=True)
    builder = InventoryMutationBuilder(inventory, 100)
    builder.move_item(1, storage_name=storage_name, storage_slot=last_slot)
    assert getattr(builder.inventory, storage_name) == {1: last_slot}
    with pytest.raises(ValueError, match="outside"):
        builder.move_item(1, storage_name=storage_name, storage_slot=last_slot + 1)


def test_expiry_uses_free_slots_then_restores_fifo_in_the_same_container():
    inventory = full_bags()
    inventory.storage_size = 2
    original_items = {i.item_id: copy.deepcopy(i) for i in inventory.items}
    changes = reconcile_premium_bags(inventory, active=False)
    assert inventory.storage_58 == {1: 0, 3: 1}
    assert inventory.storage_a8 == {2: 0}
    assert changes.removed_ids == (4, 5, 6)
    assert not changes.restored_items  # Item 3 was already registered on the client.
    assert [i.item_id for i in inventory.premium_inventory_overflow] == [4]
    assert [i.item_id for i in inventory.premium_storage_overflow] == [5, 6]
    assert not inventory.storage_80 and not inventory.storage_d0

    builder = InventoryMutationBuilder(inventory, 100)
    builder.consume(2)
    inventory = builder.finish().inventory
    changes = reconcile_premium_bags(inventory, active=False)
    assert [i.item_id for i in changes.restored_items] == [5]
    assert inventory.storage_a8 == {5: 0}
    assert [i.item_id for i in inventory.premium_inventory_overflow] == [4]
    assert [i.item_id for i in inventory.premium_storage_overflow] == [6]
    assert all(i == original_items[i.item_id] for i in owned_inventory_items(inventory))
    assert not reconcile_premium_bags(inventory, active=False).restored_items


def test_unavailable_items_cannot_be_used_or_sent_to_the_client():
    inventory = full_bags()
    reconcile_premium_bags(inventory, active=False)
    for action in (
        lambda: InventoryMutationBuilder(inventory, 100).consume(3),
        lambda: InventoryMutationBuilder(inventory, 100).consume_template("item_3", 1),
        lambda: equip_inventory_item(inventory, 3, (0,)),
        lambda: consume_equipped_stack(inventory, 3, 1),
    ):
        with pytest.raises(ValueError):
            action()
    wire = decode_inventory_info(*encode_inventory_info(inventory))
    assert [i.item_id for i in wire.items] == [1, 2]
    assert not wire.storage_80 and not wire.storage_d0
    assert not wire.premium_inventory_overflow and not wire.premium_storage_overflow
    with pytest.raises(ValueError, match="unavailable"):
        InventoryMutationBuilder(inventory, 100).move_item(1, storage_name="storage_80", storage_slot=0)


def test_overflow_survives_database_restart_and_map_rekey_with_full_item_data(tmp_path):
    inventory = full_bags()
    inventory.items[2].amount = 81
    inventory.items[2].socket_count = 1
    inventory.items[2].socketed_templates = ["gem_test"]
    inventory.items[2].enchantments = [ItemEnchantmentInfo(12.5, template_id="enchant_test")]
    reconcile_premium_bags(inventory, active=False)
    saved = account_state(42, membership_expires_at=PAST)
    saved.inventory, saved.next_item_id = inventory, 100
    path = tmp_path / "players.sqlite3"
    store = AccountStateStore(path)
    try:
        store.put(saved, flush=True)
    finally:
        store.close()
    store = AccountStateStore(path)
    try:
        restored = store.get(42).inventory
    finally:
        store.close()
    # Loading does not grant unearned RuneTrinket slots or achievements.
    unlocked = {
        item_id for item_id, slots in restored.equipment_slots.items()
        if tuple(slots) in {(18,), (19,)}
    }
    assert len(unlocked) == 0
    restored.items = [i for i in restored.items if i.item_id not in unlocked]
    for item_id in unlocked:
        del restored.equipment_slots[item_id]
    assert restored == inventory
    assert clone_inventory_info(restored) == copy.deepcopy(restored)
    clone = clone_inventory_info(restored)
    clone.premium_inventory_overflow[0].socketed_templates.append("new_gem")
    assert restored.premium_inventory_overflow[0].socketed_templates == ["gem_test"]

    rekeyed, mapping, next_id = rekey_inventory_items(restored, first_item_id=100)
    assert len(mapping) == 6 and next_id == 106
    assert len({i.item_id for i in owned_inventory_items(rekeyed)}) == 6
    changes = reconcile_premium_bags(rekeyed, active=True)
    assert len(changes.restored_items) == 4
    assert rekeyed.storage_80 == {mapping[3]: 0, mapping[4]: 1}
    assert rekeyed.storage_d0 == {mapping[5]: 0, mapping[6]: 1}
    item = next(i for i in rekeyed.items if i.item_id == mapping[3])
    assert item.amount == 81 and item.socketed_templates == ["gem_test"]
    assert item.enchantments == inventory.premium_inventory_overflow[0].enchantments
    assert not reconcile_premium_bags(rekeyed, active=True).restored_items


def test_legacy_documents_and_duplicate_overflow_ids():
    document = _inventory_to_document(InventoryInfo())
    del document["premium_inventory_overflow"], document["premium_storage_overflow"]
    assert not _inventory_from_document(document).premium_inventory_overflow
    inventory = full_bags()
    inventory.premium_inventory_overflow = [copy.deepcopy(inventory.items[0])]
    with pytest.raises(ValueError, match="duplicate"):
        _inventory_from_document(_inventory_to_document(inventory))


def test_purchases_stack_and_loot_use_the_premium_bag_when_regular_bags_are_full():
    inventory = InventoryInfo(items=[ItemInfo(1)], storage_size=1, storage_58={1: 0})
    reconcile_premium_bags(inventory, active=True)
    definition = PurchaseItemDefinition("test_stack", 99, 0, 1, "Consumable", False)
    add_purchased_item(inventory, definition, 5, next_item_id=10)
    assert inventory.storage_80 == {10: 0}
    add_purchased_item(inventory, definition, 2, next_item_id=11)
    assert inventory.storage_80 == {11: 0}
    assert next(i for i in inventory.items if i.item_id == 11).amount == 7
    drop = WorldLootDrop(ItemInfo(20, template_id="loot"), "Equipment", "")
    mutation = add_world_drop_to_inventory(inventory, drop)
    assert mutation.inventory.storage_80 == {11: 0, 20: 1}
    assert mutation.delta.storage_80 == {20: 1}
    assert not mutation.delta.storage_58


def outgoing(session, command_id):
    return [
        command.value
        for data, bits, _ in session.recorder.messages
        for command in decode_server_command_batch(data, bits)
        if command.command_id == command_id
    ]


@pytest.fixture
def live():
    server = CharacterServer.__new__(CharacterServer)
    server.static_database_path = DEFAULT_STATIC_DB_PATH
    session = _session(50777, 42, "PremiumBags")
    session.gameplay_initialization_sent = session.gameplay_presence_ready = True
    session.gameplay_inventory = full_bags()
    session.gameplay_next_item_id = 100
    session.gameplay_membership_type = 1
    session.gameplay_membership_expires_at = FUTURE
    timers = []
    def timer(delay, callback):
        handle = SimpleNamespace(delay=delay, callback=callback, start=Mock(), cancel=Mock())
        timers.append(handle)
        return handle
    session.gameplay_deadline_timer = timer
    saves = []
    def save(s, reason, flush):
        assert not s.flow_lock._is_owned()
        saves.append(clone_inventory_info(s.gameplay_inventory))
        return True
    session.gameplay_persist_callback = save
    return server, session, timers, saves


def test_idle_expiry_sends_removals_and_persists_overflow(live):
    server, session, timers, saves = live
    server._refresh_premium_bags(session)
    assert len(timers) == 1 and 0 < timers[0].delay <= 86400
    session.gameplay_membership_expires_at = PAST
    timers[0].callback()
    assert not session.gameplay_membership_active
    assert len(saves[-1].premium_inventory_overflow) == 2
    assert len(saves[-1].premium_storage_overflow) == 2
    updates = outgoing(session, INVENTORY_INFO_COMMAND)
    assert updates[0].changed_item_ids == [3, 4, 5, 6]
    assert not updates[0].items
    assert updates[-1].extra_storage_size == updates[-1].extra_bank_storage_size == 0
    assert outgoing(session, MEMBERSHIP_UPDATE_COMMAND)[-1].active is False


def test_moving_an_item_frees_a_slot_and_recovers_overflow_in_the_same_request(live):
    server, session, _, saves = live
    session.gameplay_inventory = InventoryInfo(
        items=[ItemInfo(1), ItemInfo(2)], storage_size=1, bank_storage_size=1,
        storage_58={1: 0}, storage_80={2: 0}, extra_storage_size=28, extra_bank_storage_size=40,
    )
    session.gameplay_membership_expires_at = PAST
    # A stale request naming an expired item cannot move it out of overflow.
    action = InventoryItemAction(2, 0x0109, 0, 0, 0)
    payload, bits = encode_inventory_item_action(action)
    session.current_message_bit_length = bits
    server._on_char_op(session, payload)
    assert [i.item_id for i in session.gameplay_inventory.premium_inventory_overflow] == [2]
    # Moving the ordinary item to storage frees exactly one inventory slot.
    payload, bits = encode_inventory_item_action(InventoryItemAction(1, 0x0109, 0, 0, 0))
    session.current_message_bit_length = bits
    server._on_char_op(session, payload)
    inventory = session.gameplay_inventory
    assert inventory.storage_58 == {2: 0} and inventory.storage_a8 == {1: 0}
    assert not inventory.premium_inventory_overflow
    assert saves[-1] == inventory
    definitions = [i.item_id for update in outgoing(session, INVENTORY_INFO_COMMAND) for i in update.items]
    assert definitions == [2]


def test_renewal_reschedules_and_disconnect_cancels_the_deadline(live):
    server, session, timers, _ = live
    server._refresh_premium_bags(session)
    first = timers[0]
    session.gameplay_membership_type = 2
    server._refresh_premium_bags(session)
    first.cancel.assert_called_once()
    assert len(timers) == 2
    first.callback()
    assert session.gameplay_membership_active
    RakNetServer._cancel_session_activity(session)
    timers[-1].cancel.assert_called_once()
    timers[-1].callback()
    assert not session.gameplay_inventory.premium_inventory_overflow


@pytest.mark.parametrize("tier,expiry,active", [(1, FUTURE, True), (2, FUTURE, True), (1, PAST, False)])
def test_login_reconciles_membership_before_serializing_inventory(tier, expiry, active):
    import unittest
    from dso_server.protocol.inventory_codec import encode_bootstrap_inventory_info
    from dso_server.protocol.player_init_protocol import TalentCategorySelection
    from dso_server.tests.fixtures.characters import character_profile, character_server

    case = unittest.TestCase()
    try:
        server = character_server(case, (character_profile(42, "PremiumLogin"),))
        saved = account_state(42, membership_type=tier, membership_expires_at=expiry)
        saved.talent_categories = tuple(TalentCategorySelection(0, (), ()) for _ in range(4))
        saved.inventory = decode_inventory_info(*encode_bootstrap_inventory_info(class_name="mage"))
        saved.inventory.storage_size = len(saved.inventory.storage_58)
        saved.inventory.bank_storage_size = 0
        saved.inventory.extra_storage_size = 28
        saved.inventory.extra_bank_storage_size = 40
        extra_inventory = copy.deepcopy(saved.inventory.items[-1])
        extra_inventory.item_id = 500
        extra_bank = copy.deepcopy(extra_inventory)
        extra_bank.item_id = 501
        saved.inventory.items.extend((extra_inventory, extra_bank))
        saved.inventory.storage_80 = {500: 0}
        saved.inventory.storage_d0 = {501: 0}
        server.account_store.put(saved, flush=True)
        session = _session(50778, 42, "PremiumLogin")
        session.gameplay_character = server.character_selection.active_character
        state = server._resolve_initialization_character(session)
        server._resolve_initialization_inventory(state)
        server._encode_initialization_new_player(state)
        server._resolve_initialization_talents(state)
        server._resolve_initialization_persistent_effects(session, state)
        wire = decode_inventory_info(*encode_inventory_info(state.inventory))
        assert state.membership_active is active
        assert (wire.extra_storage_size, wire.extra_bank_storage_size) == ((28, 40) if active else (0, 0))
        assert len(state.inventory.premium_inventory_overflow) == (0 if active else 1)
        assert len(state.inventory.premium_storage_overflow) == (0 if active else 1)
        assert state.rekeyed_next_item_id > max(i.item_id for i in owned_inventory_items(state.inventory))
    finally:
        case.doCleanups()


def test_offline_reward_allocator_reserves_hidden_item_ids():
    from dso_server.services.admin_bookings import BookingDelivery

    saved = account_state(42, membership_active=False)
    saved.inventory = InventoryInfo(premium_inventory_overflow=[ItemInfo(500)])
    store = SimpleNamespace(get_profile=lambda identity: SimpleNamespace(level=100, class_id=1))
    delivery = BookingDelivery(None, store, DEFAULT_STATIC_DB_PATH)
    result = delivery._apply(saved, {
        "id": 7, "kind": "item", "resource": "potion_combined_health_rarity_05", "amount": 1,
    })
    assert result.inventory.items[0].item_id == 501
    assert result.next_item_id == 502
    assert result.inventory.premium_inventory_overflow[0].item_id == 500
