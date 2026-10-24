"""Quest classifications and authoritative NPC inventory hand-ins."""
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
import copy
import struct
from unittest.mock import patch

import pytest

from dso_server.gameplay.premium_bags import reconcile_premium_bags
from dso_server.gameplay.quest_content import (
    QUEST_TYPE_IDS, QUEST_STATE_ACCEPTED, QUEST_STATE_FINISHED, QUEST_STATE_CLOSED,
    Guard, QuestProgress, load_quest_catalog,
)
from dso_server.gameplay.local_map_state import load_local_npc_definitions, local_npc_info
from dso_server.paths import DEFAULT_QUESTS_PATH, DEFAULT_STATIC_DB_PATH
from dso_server.protocol.character_selection import BitWriter
from dso_server.protocol.commerce_protocol import TRANSACTION_COMMAND, price_for_offer
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo, INVENTORY_INFO_COMMAND
from dso_server.protocol.player_init_protocol import CurrencyChanged, QuickbarInfo, decode_server_command_batch
from dso_server.protocol.gameplay_protocol import NPCBatch, encode_npc_batch, decode_npc_batch
from dso_server.protocol.quest_protocol import (
    DELIVER_COMMAND, NPC_DELIVERY_DIALOG, Deliver, encode_deliver, decode_deliver,
    QUEST_SUBJECT_TASK_FINISHED, QUEST_SUBJECT_QUEST_STATE,
)
from dso_server.protocol.shop_protocol import load_shop_offers
from dso_server.services.character_server import CharacterServer
from dso_server.services.map_events import _prop_info_entry
from dso_server.tests import test_quests as fixtures

RUBY = "a0007-monetization-tutorial-02-n"
JIMMY = "a0007_quest_mt_grimford_villager_01"
MAP = "a0007_pastures_pve"


@pytest.fixture
def quest_case():
    case = fixtures.QuestServiceTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


@pytest.fixture
def ruby_case(quest_case):
    case = quest_case
    s = case._session(MAP, 50109, class_id=2)
    case.session = s
    case.server.sessions[s.addr] = s
    case.server._attach_map_event_runtime(s, map_id=MAP, initial_flags=frozenset(),
        monster_level=None, scaling_difficulty=0, fresh_instance=True)
    s.gameplay_inventory = InventoryInfo()
    reconcile_premium_bags(s.gameplay_inventory, active=True)
    s.gameplay_next_item_id = 900
    s.gameplay_currency_state = CurrencyChanged(1000, 1000, (0,) * 3, 0, True)
    s.gameplay_quickbar_info = QuickbarInfo(0, 0)
    s.gameplay_quest_log["a0007-monetization-tutorial-01-n"] = QuestProgress(
        "a0007-monetization-tutorial-01-n", state=QUEST_STATE_CLOSED)
    case.jimmy = next(d for d in load_local_npc_definitions(DEFAULT_STATIC_DB_PATH, MAP)
                      if d.template_id == JIMMY)
    s.gameplay_spawn_position = case.jimmy.position
    return case


def accept(case):
    assert case.server._quest_accept(case.session, RUBY, offered=True)
    return case.server._quest_catalog().quest(RUBY).tasks[0].barters[0]


def gem(case, amount=1, template="gem_ruby_a", item_id=800, storage="storage_58"):
    item = ItemInfo(item_id=item_id, template_id=template, amount=amount)
    inv = case.session.gameplay_inventory
    inv.items.append(item)
    values = getattr(inv, storage)
    if isinstance(values, dict):
        values[item_id] = len(values)
    else:
        values.append(item_id)
    return item


def deliver(case, barter=None, actor=None):
    if barter is None:
        barter = case.server._quest_catalog().quest(RUBY).tasks[0].barters[0]
    payload, _ = encode_deliver(Deliver(actor or case.jimmy.actor_id, barter.barter_id))
    case.server._on_char_op(case.session, payload)


def test_deliver_codec_matches_native_u32_actor_then_string():
    value = Deliver(65539, "test-barter")
    encoded, bits = encode_deliver(value)
    assert encoded == b"\x8b\x68\x00" + struct.pack("<I", 65539) + b"\x0b\x00test-barter"
    assert decode_deliver(encoded, bits) == value
    with pytest.raises(ValueError):
        decode_deliver(encoded + b"\xff", bits + 8)


@pytest.mark.parametrize("quest_type,expected", list(QUEST_TYPE_IDS.items()))
def test_offer_target_and_turn_in_indications_follow_authored_quest_type(quest_case, quest_type, expected):
    case = quest_case
    catalog = case.server._quest_catalog()
    quest = replace(catalog.quest(fixtures.TUTORIAL_01), quest_type=quest_type)
    quests = dict(catalog.quests)
    quests[quest.key] = quest
    catalog = replace(catalog, quests=MappingProxyType(quests),
                      by_giver=MappingProxyType({quest.giver.casefold(): (quest,)}))
    with patch.object(case.server, "_quest_catalog", return_value=catalog):
        interactions, _ = case.server._quest_npc_interactions(case.session, quest.giver)
        assert [i.field_18 for i in interactions] == [expected]
        case.session.gameplay_quest_log[quest.quest_id] = QuestProgress(quest.quest_id)
        interactions, _ = case.server._quest_npc_interactions(case.session, quest.goal)
        assert [i.field_18 for i in interactions] == [expected, expected]
        case.session.gameplay_quest_log[quest.quest_id].state = QUEST_STATE_FINISHED
        interactions, _ = case.server._quest_npc_interactions(case.session, quest.goal)
        assert [i.field_18 for i in interactions] == [expected]


def test_every_authored_type_and_delivery_is_parsed():
    catalog = load_quest_catalog(DEFAULT_QUESTS_PATH)
    assert {q.quest_type for q in catalog.quests.values()} == set(QUEST_TYPE_IDS)
    assert all(q.quest_type_id in range(14) for q in catalog.quests.values())
    barters = [b for q in catalog.quests.values() for t in q.tasks for b in t.barters]
    # Pay tasks (currency hand-ins, e.g. Cardhun's) are parsed beside the
    # item deliveries.
    assert sum(b.event_kind == "deliver" for b in barters) == 379
    assert sum(b.event_kind == "pay" for b in barters) == 19
    assert sum(b.only_show for b in barters) == 29


def test_side_quest_prop_uses_local_indication():
    catalog = load_quest_catalog(DEFAULT_QUESTS_PATH)
    quest = next(q for q in catalog.quests.values() if q.quest_type == "local"
                 and any(t.switch_definitions for t in q.tasks))
    switch = next(b for t in quest.tasks for b in t.switch_definitions if b.prop)
    definition = SimpleNamespace(prop_id=9, template_id="test", instance_guid=b"\0" * 16,
        instance_id=switch.prop, quest_switch_id=switch.switch_id,
        position=(0., 0., 0.), rotation=0.)
    entry = _prop_info_entry(SimpleNamespace(definition=definition, state=1))
    assert entry.field_5c == 5


def test_npc_has_delivery_descriptor_even_before_buying_and_uses_side_quest_marker(ruby_case):
    case = ruby_case
    barter = accept(case)
    info = case.server._quest_decorate_npc_info(case.session, local_npc_info(case.jimmy), JIMMY)
    wire = encode_npc_batch(NPCBatch((info,)))
    decoded = decode_npc_batch(*wire).commands[0]
    choice = next(i for i in decoded.interactions if i.source == barter.barter_id)
    assert (choice.field_08, choice.target, choice.field_18) == (NPC_DELIVERY_DIALOG, RUBY, 5)
    row = next(b for b in decoded.string_sets if b.name == barter.barter_id)
    assert row.values == ("gem_ruby_a",) and row.field_28 == 1 and row.enabled


def test_owned_gem_before_accept_is_usable_and_consumed_once(ruby_case):
    case = ruby_case
    gem(case, amount=2)
    accept(case)
    mark = len(case.session.recorder.messages)
    deliver(case)
    progress = case.session.gameplay_quest_log[RUBY]
    assert progress.state == QUEST_STATE_FINISHED and progress.finished_mask == 1
    assert case.session.gameplay_inventory.items[0].amount == 1
    assert {u.subject for u in case._updates(mark)} == {QUEST_SUBJECT_TASK_FINISHED, QUEST_SUBJECT_QUEST_STATE}
    snapshot = copy.deepcopy(case.session.gameplay_inventory)
    deliver(case)
    assert case.session.gameplay_inventory == snapshot
    assert case.session.gameplay_quest_log[RUBY].tokens == progress.tokens
    with patch.object(case.server, "_quest_grant_rewards") as reward:
        assert case.server._quest_turn_in(case.session, RUBY)
        assert not case.server._quest_turn_in(case.session, RUBY)
        reward.assert_called_once()


@pytest.mark.parametrize("reason", ["missing", "wrong_gem", "bank", "equipped", "wrong_npc", "guard", "future_task"])
def test_invalid_handins_do_not_consume_items_or_progress(ruby_case, reason):
    case = ruby_case
    barter = accept(case)
    if reason != "missing":
        gem(case, template="gem_onyx_a" if reason == "wrong_gem" else "gem_ruby_a",
            storage="storage_a8" if reason == "bank" else "storage_58")
    if reason == "equipped":
        case.session.gameplay_inventory.equipment_slots[800] = (5,)
    catalog = case.server._quest_catalog()
    quest = catalog.quest(RUBY)
    if reason in {"guard", "future_task"}:
        task = quest.tasks[0]
        if reason == "guard":
            task = replace(task, barters=(replace(barter, guards=(Guard("has", "missing"),)),))
        else:
            task = replace(task, pre_tasks=("unfinished",))
        quest = replace(quest, tasks=(task,))
        catalog = replace(catalog, quests={**catalog.quests, quest.key: quest})
    before = copy.deepcopy(case.session.gameplay_inventory)
    with patch.object(case.server, "_quest_catalog", return_value=catalog):
        deliver(case, actor=12345 if reason == "wrong_npc" else None)
    assert case.session.gameplay_inventory == before
    assert case.session.gameplay_quest_log[RUBY].state == QUEST_STATE_ACCEPTED
    assert case.session.gameplay_quest_log[RUBY].tokens == {}


def test_alternative_items_across_stacks_and_show_only_retention(ruby_case):
    case = ruby_case
    barter = accept(case)
    gem(case, amount=2)
    gem(case, amount=3, template="gem_onyx_a", item_id=801, storage="storage_80")
    catalog = case.server._quest_catalog()
    quest = catalog.quest(RUBY)
    barter = replace(barter, items=("gem_ruby_a", "gem_onyx_a"), amount=4, only_show=True)
    task = replace(quest.tasks[0], barters=(barter,))
    quest = replace(quest, tasks=(task,))
    catalog = replace(catalog, quests={**catalog.quests, quest.key: quest})
    before = copy.deepcopy(case.session.gameplay_inventory)
    with patch.object(case.server, "_quest_catalog", return_value=catalog):
        deliver(case, barter)
    assert case.session.gameplay_inventory == before
    assert case.session.gameplay_quest_log[RUBY].state == QUEST_STATE_FINISHED
    # Reset only the isolated test state to check the consuming variant.
    case.session.gameplay_quest_log[RUBY] = QuestProgress(RUBY)
    quest = replace(quest, tasks=(replace(task, barters=(replace(barter, only_show=False),)),))
    catalog = replace(catalog, quests={**catalog.quests, quest.key: quest})
    with patch.object(case.server, "_quest_catalog", return_value=catalog):
        deliver(case)
    assert [(i.template_id, i.amount) for i in case.session.gameplay_inventory.items] == [("gem_onyx_a", 1)]
    assert 800 not in case.session.gameplay_inventory.storage_58
    assert case.session.gameplay_inventory.storage_80 == {801: 0}


def test_real_shop_purchase_then_deliver_and_restore_progress(ruby_case):
    case = ruby_case
    accept(case)
    s = case.session
    offers = load_shop_offers(DEFAULT_STATIC_DB_PATH, "global_rc_trader", player_level=1, class_id=2)
    offer = next(o for o in offers.offers if o.item_id == "gem_ruby_a")
    s.gameplay_open_shop_id, s.gameplay_open_shop_offers = offers.shop_id, offers
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(TRANSACTION_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(1)
    writer.write_bool(True)
    writer.write_string(offer.resource_id)
    for value in (0, 0, 1):
        writer.write_uint32_le(value)
    writer.write_string(offers.shop_id)
    writer.write_bool(False)
    payload, bits = writer.finish()
    s.current_message_bit_length = bits
    with patch.object(CharacterServer, "_persist_gameplay_session"):
        case.server._handle_transaction(s, payload)
    s.current_message_bit_length = None
    assert any(i.template_id == "gem_ruby_a" for i in s.gameplay_inventory.items)
    assert s.gameplay_currency_state.currency_type == 1000 - price_for_offer(offer, 1).rc
    deliver(case)
    assert not any(i.template_id == "gem_ruby_a" for i in s.gameplay_inventory.items)
    saved = case.server._quest_log_document(s)
    case.server._restore_quest_state(s, SimpleNamespace(quest_log=saved))
    assert s.gameplay_quest_log[RUBY].state == QUEST_STATE_FINISHED
    assert not case.server._quest_npc_barters(s, JIMMY)


def test_authored_multi_item_return_credits_the_full_delivery_amount():
    from dso_server.gameplay.quest_content import apply_quest_event
    catalog = load_quest_catalog(DEFAULT_QUESTS_PATH)
    quest = catalog.quest("a0200-Crafting-WorkbenchTutorial-02-n")
    task = next(t for t in quest.tasks if t.barters and t.barters[0].amount == 20)
    task = replace(task, index=0, pre_tasks=())
    quest = replace(quest, tasks=(task,))
    barter = task.barters[0]
    progress = QuestProgress(quest.quest_id,
        tokens={g.token: g.amount for g in barter.guards if g.kind == "has"})
    result = apply_quest_event(quest, progress, "deliver", barter.barter_id)
    assert result.quest_finished
    assert result.changes[0].progress == result.changes[0].target == 20
