"""Retail dye/skin exchanges, atomic payment, and durable identification."""
import copy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence.account_state import _inventory_from_document, _inventory_to_document
from dso_server.persistence.static_game_database import StaticGameDatabase
from dso_server.protocol.character_selection import BitWriter
from dso_server.protocol.gem_protocol import encode_item_info_response
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.protocol.item_customization_protocol import (
    APPLY_ITEM_CUSTOMIZATION_COMMAND, ItemCustomizationChange,
    apply_item_customizations, decode_item_customization_changes, item_tint,
)
from dso_server.protocol.player_init_protocol import CurrencyChanged
from dso_server.runtime.transport import Session
from dso_server.services.character_server import CharacterServer

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/item_customization_retail.json').read_text())
WALLET = CurrencyChanged(100_000, 100_000_000, (0, 0, 0), 0, False)


def request(changes):
    w = BitWriter()
    w.write_uint8(0x8b)
    w.write_bytes(APPLY_ITEM_CUSTOMIZATION_COMMAND.to_bytes(2, 'little'))
    w.write_uint32_le(len(changes))
    for c in changes:
        w.write_uint32_le(c.slot_type)
    w.write_uint32_le(len(changes))
    for c in changes:
        w.write_uint32_le(6)
        for value in (c.apply_skin, c.remove_skin, c.skin, c.effect_skin, c.apply_color, c.remove_color):
            w.write_string(value)
    return w.finish()[0]


def paint(slot, color='fk_Black_008', old=''):
    return ItemCustomizationChange(slot, '', '', '', '', color, old)


def apply(inv, changes, currency=WALLET):
    return apply_item_customizations(inv, currency, changes, database_path=DEFAULT_STATIC_DB_PATH,
                                     class_id=1, level=100, next_item_id=1_000_000)


def equipped():
    item = ItemInfo(100, template_id='mage_base_helmet_crafted', item_level=100,
                    field_30=10, field_38=0)
    return InventoryInfo(items=[item], equipment_slots={100: (0,)})


@pytest.mark.parametrize('exchange', FIXTURE['exchanges'])
def test_retail_customization_requests_decode_exactly(exchange):
    raw = bytes.fromhex(exchange['request'])
    changes = decode_item_customization_changes(raw)
    assert request(changes) == raw
    assert len(changes) in (1, 3)


@pytest.mark.parametrize('exchange', FIXTURE['exchanges'][:2])
def test_dye_updates_match_retail_compact_item_packets(exchange):
    changes = decode_item_customization_changes(bytes.fromhex(exchange['request']))
    templates = ('mage_unique_helmet_essence_black_set', 'unique_mage_2h_staff2h_watergrave',
                 'mage_unique_backitem_medusa')
    inv = InventoryInfo()
    for data, template, slot in zip(exchange['updates'], templates, (0, 5, 7)):
        item = ItemInfo(data['item_id'], template_id=template, amount=data['amount'],
                        socket_count=data['socket_count'], field_34=data['field_34'],
                        item_level=data['item_level'], field_3c=data['flags'][0],
                        field_3d=data['flags'][1], field_f8=data['skin'],
                        socketed_templates=data['sockets'], field_3e=data['enabled'],
                        field_28=changes[0].remove_color)
        inv.items.append(item)
        inv.equipment_slots[item.item_id] = (slot,)
    mutation, wallet, updated = apply(inv, changes)
    assert wallet.andermant == WALLET.andermant - 3 * 500
    assert wallet.gold == WALLET.gold - 3 * 12500
    for item, data in zip(updated, exchange['updates']):
        packet, bits = encode_item_info_response(item, actor_id=data['actor_id'])
        assert (packet.hex(), bits) == (data['payload'], data['bit_length'])
    restored = _inventory_from_document(_inventory_to_document(mutation.inventory))
    assert [(i.field_20, i.field_1c, i.field_28) for i in restored.items] == [
        (i.field_20, i.field_1c, i.field_28) for i in updated]


def test_black_for_all_eight_equipment_types_maps_cloak_to_inventory_seven():
    inv = equipped()
    for index in range(1, 8):
        inv.items.append(replace(inv.items[0], item_id=100+index))
        inv.equipment_slots[100+index] = (index,)
    changes = tuple(paint(slot) for slot in (0, 1, 2, 3, 4, 5, 6, 12))
    mutation, wallet, updated = apply(inv, decode_item_customization_changes(request(changes)))
    assert len(updated) == 8
    assert wallet.andermant == WALLET.andermant - 4000
    assert all((i.field_20, i.field_1c, i.field_28) == (0xd1050505, 107, 'fk_Black_008')
               for i in updated)
    assert inv.items[0].field_20 == 0
    again, unchanged_wallet, updated = apply(mutation.inventory, changes, wallet)
    assert unchanged_wallet == wallet and updated == ()


def test_dye_stacks_are_used_before_currency_and_two_hand_color_costs_once():
    inv = equipped()
    inv.equipment_slots = {100: (5, 6)}
    inv.items.append(ItemInfo(200, template_id='misc_colorize_fk_Black_008', amount=2))
    inv.field_180 = [200]
    mutation, wallet, updated = apply(inv, (paint(5), paint(6)))
    assert wallet == WALLET and len(updated) == 1
    assert next(i for i in mutation.inventory.items if i.item_id == 200).amount == 1


def test_last_dye_is_removed_and_insufficient_money_does_not_consume_it():
    inv = equipped()
    inv.items.extend([replace(inv.items[0], item_id=101),
                      ItemInfo(200, template_id='misc_colorize_fk_Black_008', amount=1)])
    inv.equipment_slots[101] = (1,)
    inv.field_180 = [200]
    before = copy.deepcopy(inv)
    with pytest.raises(ValueError, match='insufficient'):
        apply(inv, (paint(0), paint(1)), replace(WALLET, currency_type=0))
    assert inv == before
    mutation, _, _ = apply(inv, (paint(0),))
    assert 200 not in mutation.inventory.field_180
    assert all(i.item_id != 200 for i in mutation.inventory.items)


@pytest.mark.parametrize('amount', [3, 4])
def test_one_dye_stack_for_several_slots_publishes_only_its_final_state(amount):
    inv = equipped()
    for slot in (1, 2):
        inv.items.append(replace(inv.items[0], item_id=100+slot))
        inv.equipment_slots[100+slot] = (slot,)
    inv.items.append(ItemInfo(200, template_id='misc_colorize_fk_Black_008', amount=amount))
    inv.field_180 = [200]
    mutation, wallet, updated = apply(inv, tuple(paint(slot) for slot in (0,1,2)))
    assert wallet == WALLET and len(updated) == 3
    assert [(item.item_id, item.amount) for item in mutation.delta.items] == (
        [] if amount == 3 else [(200,1)])
    assert mutation.delta.changed_item_ids == ([200] if amount == 3 else [])


def test_retail_skin_change_is_persisted_without_changing_stats_or_rolls():
    data = FIXTURE['exchanges'][2]
    changes = decode_item_customization_changes(bytes.fromhex(data['request']))
    inv = equipped()
    inv.items[0].template_id = 'unique_mage_2h_staff2h_watergrave'
    inv.items[0].field_f8 = 'mage_unique_2h_staff_watergrave'
    inv.equipment_slots = {100: (5, 6)}
    mutation, wallet, updated = apply(inv, changes)
    assert updated[0].field_f8 == 'mage_unique_2h_staff_corruption_set_FX'
    assert updated[0].template_id == inv.items[0].template_id
    assert updated[0].item_level == inv.items[0].item_level
    assert updated[0].field_18 == inv.items[0].field_18
    assert wallet.andermant == WALLET.andermant - 799
    restored = _inventory_from_document(_inventory_to_document(mutation.inventory))
    assert restored.items[0].field_f8 == updated[0].field_f8
    assert CharacterServer._equipment_record_for(restored.items[0]).appearance_id == updated[0].field_f8
    server = object.__new__(CharacterServer)
    server.static_database = StaticGameDatabase(DEFAULT_STATIC_DB_PATH)
    visuals = server._remote_equipment_visuals(restored)
    expected_skin = server.static_database.remote_equipment_skin(
        restored.items[0].template_id, updated[0].field_f8)
    assert len(visuals) == 2
    assert all(visual.skins == (expected_skin,) for visual in visuals)


@pytest.mark.parametrize('changes', [
    (paint(0, 'invented-color'),), (paint(1),), (paint(0, old='fk_Lime_003'),),
    (ItemCustomizationChange(0, 'mage_unique_2h_staff_watergrave', '', '', '', '', ''),),
])
def test_invalid_customizations_do_not_mutate_inventory(changes):
    inv = equipped()
    before = copy.deepcopy(inv)
    with pytest.raises(ValueError):
        apply(inv, changes)
    assert inv == before


def test_truncated_duplicate_and_oversized_requests_are_rejected():
    raw = request((paint(0),))
    for malformed in (raw[:-1], raw+b'\0', request((paint(0),paint(0))), raw[:3]+b'\xff'*4):
        with pytest.raises(ValueError):
            decode_item_customization_changes(malformed)


def test_transmog_dispatch_sends_updates_saves_and_broadcasts_appearance():
    server = object.__new__(CharacterServer)
    server.static_database_path = DEFAULT_STATIC_DB_PATH
    session = Session(('127.0.0.1', 40991), Mock())
    session.gameplay_mode = True
    session.gameplay_inventory = equipped()
    session.gameplay_currency_state = WALLET
    session.gameplay_character = SimpleNamespace(class_id=1, level=100)
    session.gameplay_presence_callback = Mock()
    with patch.object(CharacterServer, '_request_account_save') as save, \
         patch.object(CharacterServer, '_send_world_loot_state_batch') as send:
        server._on_char_op(session, request((paint(0),)))
    assert send.call_count == 2
    save.assert_called_once()
    session.gameplay_presence_callback.assert_called_once_with(session, 'appearance')
    assert item_tint(session.gameplay_inventory.items[0]) == pytest.approx((5/255,5/255,5/255,209/255))


@pytest.mark.parametrize('raw', [
    '8b55004403010012000000000000000000000000',
    '8b55004603010012000000000000000000000000',
    '8b55004203010012000000000000000000000000',
])
def test_retail_right_click_identification_survives_reload(raw):
    item_id = int.from_bytes(bytes.fromhex(raw)[3:7], 'little')
    server = object.__new__(CharacterServer)
    session = Session(('127.0.0.1', 40992), Mock())
    item = ItemInfo(item_id, template_id='mage_base_helmet_crafted', field_08=2, field_3c=True)
    session.gameplay_inventory = InventoryInfo(items=[item], storage_58={item_id:0})
    with patch.object(CharacterServer, '_request_account_save') as save, \
         patch.object(CharacterServer, '_send_world_loot_state_batch') as send:
        server._handle_inventory_item_action(session, bytes.fromhex(raw))
        server._handle_inventory_item_action(session, bytes.fromhex(raw))
    save.assert_called_once()
    send.assert_called_once()
    restored = _inventory_from_document(_inventory_to_document(session.gameplay_inventory))
    assert not restored.items[0].field_3c
    assert restored.items[0].field_18 == item.field_18
