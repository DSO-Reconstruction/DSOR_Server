"""Authored quest drops are personal world items, credited only on pickup."""
from contextlib import contextmanager
from dataclasses import replace
import json
import random
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

import pytest

from dso_server.gameplay.quest_content import (
    QUEST_STATE_CLOSED, QuestProgress, apply_quest_event, load_quest_catalog,
)
from dso_server.paths import DEFAULT_QUESTS_PATH, DEFAULT_STATIC_DB_PATH
from dso_server.persistence.account_state import _inventory_from_document, _inventory_to_document
from dso_server.protocol.commerce_protocol import add_purchased_item, load_purchase_item_definition
from dso_server.protocol.inventory_codec import (
    INVENTORY_INFO_COMMAND, InventoryInfo, ItemInfo, owned_inventory_items,
)
from dso_server.protocol.inventory_economy_protocol import InventoryMutationBuilder
from dso_server.protocol.player_init_protocol import decode_server_command_batch
from dso_server.protocol.world_loot_protocol import (
    NEW_ITEM_COMMAND, build_quest_world_drops, decode_new_item_batch,
    decode_world_pickup_response,
)
from dso_server.services.character_server import CharacterServer
from dso_server.tests import test_quests as fixtures

QUEST = fixtures.GRIMMAGSTONE_02
ROOTS = fixtures.ROOTS
WOOD = "quest_a0002_WayToGrimmagstone_02_wood"


@pytest.fixture
def world():
    case = fixtures.QuestServiceTests(methodName="runTest")
    case.setUp()
    session = case._session(fixtures.GARDEN, 50201, class_id=2)
    case.session = session
    case.server.sessions[session.addr] = session
    session.gameplay_inventory = InventoryInfo()
    session.gameplay_quest_log[QUEST] = QuestProgress(QUEST)
    session.gameplay_presence_callback = case.server._handle_presence_event
    with patch.object(CharacterServer, "_achievement_on_item_collected"):
        try:
            yield case
        finally:
            case.doCleanups()


def kill(case, count=1, *, template=ROOTS, position=(0., 0., 0.)):
    case.server._quest_on_monsters_killed(case.session, tuple(
        SimpleNamespace(template_id=template, position=position) for _ in range(count)))
    return tuple(case.session.gameplay_world_loot.values())


def counter(case, quest_id=QUEST):
    quest = case.server._quest_catalog().quest(quest_id)
    return quest.tasks[0].counter(case.session.gameplay_quest_log[quest_id].tokens)


def pickup(case, drop):
    return case.server._pickup_world_item(case.session, drop.item.item_id, automatic=False)


def test_kill_spawns_a_native_item_without_collection_credit(world):
    drops = kill(world)
    assert len(drops) == 1
    drop = drops[0]
    assert (drop.quest_id, drop.item.template_id, drop.item.amount) == (QUEST, WOOD, 1)
    assert counter(world) == 0
    assert world.session.gameplay_inventory.items == []
    packets = [(data, bits) for data, bits, _ in world.session.recorder.messages
               if data[0] == 0x85 and int.from_bytes(data[1:3], "little") == NEW_ITEM_COMMAND]
    assert len(packets) == 1
    item, actor = decode_new_item_batch(*packets[0])[0]
    assert item.template_id == WOOD and item.field_100 == (0., 0., 0.)
    assert pickup(world, drop)
    assert counter(world) == 1
    assert any(i.template_id == WOOD for i in world.session.gameplay_inventory.items)
    response = next((data, bits) for data, bits, label in world.session.recorder.messages
                    if label == "world-loot-pickup-response")
    decode_world_pickup_response(*response)
    assert not pickup(world, drop)
    assert counter(world) == 1
    restored = QuestProgress.from_document(QUEST, world.session.gameplay_quest_log[QUEST].to_document())
    quest = world.server._quest_catalog().quest(QUEST)
    assert quest.tasks[0].counter(restored.tokens) == 1


def test_failed_inventory_pickup_does_not_collect_or_remove_drop(world):
    drop, = kill(world)
    world.session.gameplay_inventory = InventoryInfo(storage_size=0, extra_storage_size=0)
    assert not pickup(world, drop)
    assert counter(world) == 0
    assert drop.item.item_id in world.session.gameplay_world_loot
    world.session.gameplay_inventory = InventoryInfo()
    assert pickup(world, drop)
    assert counter(world) == 1


def test_loot_uses_each_monsters_death_position(world):
    combat = world.session.gameplay_combat_state
    combat.monster_positions[10] = (2., 3., 4.)
    monsters = (
        SimpleNamespace(template_id=ROOTS, actor_id=10, position=(99., 0., 99.)),
        SimpleNamespace(template_id=ROOTS, actor_id=11, position=(5., 6., 7.)),
    )
    world.server._quest_on_monsters_killed(world.session, monsters)
    assert {d.item.field_100 for d in world.session.gameplay_world_loot.values()} == {
        (2., 3., 4.), (5., 6., 7.)}
    assert counter(world) == 0


def test_shared_item_templates_credit_only_the_owning_quest(world, monkeypatch):
    catalog = world.server._quest_catalog()
    other = replace(catalog.quest(QUEST), quest_id="test-other-wood-quest")
    catalog = replace(catalog, quests=MappingProxyType({**catalog.quests, other.key: other}))
    monkeypatch.setattr(world.server, "_quest_catalog", lambda: catalog)
    world.session.gameplay_quest_log[other.quest_id] = QuestProgress(other.quest_id)
    drops = kill(world)
    assert len(drops) == 2
    first = next(d for d in drops if d.quest_id == QUEST)
    second = next(d for d in drops if d.quest_id == other.quest_id)
    assert first.item.template_id == second.item.template_id
    assert pickup(world, first)
    assert (counter(world), counter(world, other.quest_id)) == (1, 0)
    assert pickup(world, second)
    assert (counter(world), counter(world, other.quest_id)) == (1, 1)
    assert world.server._quest_cancel(world.session, QUEST, force=True)
    assert [(i.quest_id, i.template_id) for i in world.session.gameplay_inventory.items] == [
        (other.quest_id, WOOD)]
    assert counter(world, other.quest_id) == 1


def test_wrong_monster_or_inactive_quest_does_not_drop(world):
    assert kill(world, template="unrelated_monster") == ()
    progress = world.session.gameplay_quest_log.pop(QUEST)
    assert kill(world) == ()
    world.session.gameplay_quest_log[QUEST] = progress
    progress.finished_mask = 1
    assert kill(world) == ()


def test_cancellation_discards_old_drops_before_reacceptance(world):
    drop, = kill(world)
    assert world.server._quest_cancel(world.session, QUEST, force=True)
    assert drop.item.item_id not in world.session.gameplay_world_loot
    assert drop.item.item_id not in world.session.gameplay_announced_world_item_ids
    world.session.gameplay_quest_log[QUEST] = QuestProgress(QUEST)
    assert not pickup(world, drop)
    assert counter(world) == 0


def test_personal_drops_have_distinct_ids_across_same_map_players(world):
    first, = kill(world)
    peer = world._session(fixtures.GARDEN, 50202, class_id=2)
    peer.gameplay_inventory = InventoryInfo()
    peer.gameplay_quest_log[QUEST] = QuestProgress(QUEST)
    assert not world.server._pickup_world_item(peer, first.item.item_id, automatic=False)
    world.server._quest_on_monsters_killed(peer, (SimpleNamespace(template_id=ROOTS),))
    second, = peer.gameplay_world_loot.values()
    assert first.item.item_id != second.item.item_id
    assert first.item.item_id not in peer.gameplay_world_loot
    assert second.item.item_id not in world.session.gameplay_world_loot


def test_cancelling_during_item_construction_does_not_publish_stale_drop(world, monkeypatch):
    from dso_server.services import quests
    build = quests.build_quest_world_drops

    def cancel_then_build(*args, **kwargs):
        world.server._quest_cancel(world.session, QUEST, force=True)
        return build(*args, **kwargs)

    monkeypatch.setattr(quests, "build_quest_world_drops", cancel_then_build)
    assert kill(world) == ()


@pytest.mark.parametrize("chance,amount,expected", [(0., 1, 0), (1., 3, 3)])
def test_authored_chance_and_amount_survive_deferred_collection(chance, amount, expected):
    quest = load_quest_catalog(DEFAULT_QUESTS_PATH).quest(QUEST)
    task = quest.tasks[0]
    events = tuple(replace(e, drops=tuple(replace(d, chance=chance, amount=amount) for d in e.drops))
                   for e in task.events)
    quest = replace(quest, tasks=(replace(task, events=events), *quest.tasks[1:]))
    progress = QuestProgress(QUEST)
    result = apply_quest_event(quest, progress, "kill", ROOTS, rng=random.Random(1))
    assert sum(d.amount for d in result.drops) == expected
    assert task.counter(progress.tokens) == 0
    if expected:
        apply_quest_event(quest, progress, "collect", WOOD, count=expected)
        assert task.counter(progress.tokens) == expected


def test_inventory_flag_keeps_direct_collection():
    quest = load_quest_catalog(DEFAULT_QUESTS_PATH).quest(QUEST)
    progress = QuestProgress(QUEST)
    result = apply_quest_event(quest, progress, "interact", fixtures.BAG_SWITCH)
    assert result.drops == ()
    assert quest.tasks[1].counter(progress.tokens) == 1


def test_item_stacks_respect_the_template_limit():
    drops = build_quest_world_drops(DEFAULT_STATIC_DB_PATH, QUEST, WOOD, 10000, (1., 2., 3.))
    assert [d.item.amount for d in drops] == [9999, 1]
    assert all(d.quest_id == QUEST and d.item.field_100 == (1., 2., 3.) for d in drops)


def test_authored_item_alias_keeps_its_collect_id_on_pickup(world):
    quest_id = "k0002-free-the-spirit"
    world.session.gameplay_quest_log.clear()
    progress = QuestProgress(quest_id)
    world.session.gameplay_quest_log[quest_id] = progress
    quest = world.server._quest_catalog().quest(quest_id)
    task = next(t for t in quest.tasks if any(e.value == "k0002_cremationashes" for e in t.events))
    # Open the task's authored prerequisites, as earlier steps would.
    for earlier in quest.tasks:
        if earlier.title in task.pre_tasks:
            progress.finished_mask |= 1 << earlier.index
            for token in earlier.returns:
                progress.tokens[token] = 1
    world.server._quest_feed(world.session, "interact",
                            "k0002-free-the-spirit_k0002-free-the-spirit-collect")
    drop, = world.session.gameplay_world_loot.values()
    assert drop.item.template_id == "quest_k0002_cremationashes"
    assert drop.quest_collect_id == "k0002_cremationashes"
    assert task.counter(progress.tokens) == 0
    assert pickup(world, drop)
    assert task.counter(progress.tokens) == 1


def test_missing_template_is_logged_without_false_collection(world, capsys):
    from dso_server.gameplay.quest_content import Drop
    quest = world.server._quest_catalog().quest(QUEST)
    world.server._quest_spawn_drops(world.session, quest, (
        Drop("unique_questreward_protection", 1, 1., False),
    ))
    assert "cannot materialize drop" in capsys.readouterr().out
    assert not world.session.gameplay_world_loot
    assert counter(world) == 0


@pytest.mark.parametrize("storage", [
    "storage_58", "storage_80", "storage_a8", "storage_d0", "field_180",
    "premium_inventory_overflow", "premium_storage_overflow",
])
def test_cancellation_removes_owned_items_from_every_storage(world, storage):
    drop, = kill(world)
    assert pickup(world, drop)
    inv = world.session.gameplay_inventory
    item, = inv.items
    assert item.quest_id == QUEST
    inv.storage_58.clear()
    if storage.endswith("overflow"):
        inv.items.remove(item)
        getattr(inv, storage).append(item)
    elif storage == "field_180":
        inv.field_180.append(item.item_id)
        inv.field_1a0.append(item.template_id)
    else:
        getattr(inv, storage)[item.item_id] = 0
    ordinary = ItemInfo(item_id=50, template_id="gem_ruby_a")
    inv.items.append(ordinary)
    inv.storage_58[ordinary.item_id] = 1
    with patch.object(CharacterServer, "_request_account_save") as save:
        assert world.server._quest_cancel(world.session, QUEST, force=True)
    save.assert_called_once_with(world.session, "quest cancelled")
    inv = world.session.gameplay_inventory
    assert list(owned_inventory_items(inv)) == [ordinary]
    assert inv.storage_58 == {50: 1}
    assert not inv.storage_80 and not inv.storage_a8 and not inv.storage_d0
    assert not inv.field_180 and not inv.field_1a0
    records = [command.value
               for data, bits, label in world.session.recorder.messages
               if label == "quest-cancel-inventory"
               for command in decode_server_command_batch(data, bits)
               if command.command_id == INVENTORY_INFO_COMMAND]
    assert len(records) == 2
    assert records[0].field_1e0 and not records[1].field_1e0
    if not storage.endswith("overflow"):
        assert records[0].changed_item_ids == [item.item_id]
    assert QUEST not in world.session.gameplay_quest_log


def test_inventory_quest_ownership_survives_save_reload_and_stack_split(world):
    drop, = build_quest_world_drops(DEFAULT_STATIC_DB_PATH, QUEST, WOOD, 4, (0., 0., 0.))
    drop.item.item_id = 900
    world.session.gameplay_world_loot[900] = drop
    assert pickup(world, drop)
    document = json.loads(json.dumps(_inventory_to_document(world.session.gameplay_inventory)))
    inv = _inventory_from_document(document)
    assert inv.items[0].quest_id == QUEST
    # Old account documents still load with unknown ownership.
    document["items"][0].pop("quest_id")
    assert _inventory_from_document(document).items[0].quest_id == ""
    builder = InventoryMutationBuilder(inv, 1000)
    builder.split_stack(900, 2, storage_name="storage_58", storage_slot=1)
    mutation = builder.finish()
    world.session.gameplay_inventory = mutation.inventory
    world.session.gameplay_next_item_id = mutation.next_item_id
    assert [(item.quest_id, item.amount) for item in mutation.inventory.items] == [(QUEST, 2)] * 2
    assert world.server._quest_cancel(world.session, QUEST, force=True)
    assert not world.session.gameplay_inventory.items


def test_ordinary_grants_do_not_merge_into_quest_owned_stacks(world):
    drop, = kill(world)
    assert pickup(world, drop)
    definition = load_purchase_item_definition(DEFAULT_STATIC_DB_PATH, WOOD)
    inventory = world.session.gameplay_inventory
    add_purchased_item(inventory, definition, 2, next_item_id=world.session.gameplay_next_item_id)
    assert {(item.quest_id, item.amount) for item in inventory.items} == {
        (QUEST, 1), ("", 2)}


def test_cancellation_removes_legacy_quest_items_but_preserves_ordinary_requirements(world):
    quest_id = "a0007-monetization-tutorial-02-n"
    world.session.gameplay_quest_log[quest_id] = QuestProgress(quest_id)
    wood = ItemInfo(item_id=50, template_id=WOOD, amount=3)
    ruby = ItemInfo(item_id=51, template_id="gem_ruby_a", amount=2)
    world.session.gameplay_inventory = InventoryInfo(items=[wood, ruby], storage_58={50: 0, 51: 1})
    assert world.server._quest_cancel(world.session, QUEST, force=True)
    assert world.session.gameplay_inventory.items == [ruby]
    assert world.server._quest_cancel(world.session, quest_id, force=True)
    assert world.session.gameplay_inventory.items == [ruby]


def test_legacy_shared_items_are_kept_until_last_quest_is_cancelled(world, monkeypatch):
    catalog = world.server._quest_catalog()
    other = replace(catalog.quest(QUEST), quest_id="test-other-wood-quest")
    catalog = replace(catalog, quests=MappingProxyType({**catalog.quests, other.key: other}))
    monkeypatch.setattr(world.server, "_quest_catalog", lambda: catalog)
    world.session.gameplay_quest_log[other.quest_id] = QuestProgress(other.quest_id)
    wood = ItemInfo(item_id=50, template_id=WOOD, amount=3)
    world.session.gameplay_inventory = InventoryInfo(items=[wood], storage_58={50: 0})
    assert world.server._quest_cancel(world.session, QUEST, force=True)
    assert world.session.gameplay_inventory.items == [wood]
    assert world.server._quest_cancel(world.session, other.quest_id, force=True)
    assert not world.session.gameplay_inventory.items


def test_legacy_aliased_item_is_removed_on_cancellation(world):
    quest_id = "k0002-free-the-spirit"
    world.session.gameplay_quest_log[quest_id] = QuestProgress(quest_id)
    ashes = ItemInfo(item_id=50, template_id="quest_k0002_cremationashes")
    world.session.gameplay_inventory = InventoryInfo(items=[ashes], storage_58={50: 0})
    assert world.server._quest_cancel(world.session, quest_id, force=True)
    assert not world.session.gameplay_inventory.items


@pytest.mark.parametrize("reason", ["closed", "missing", "not-deletable"])
def test_refused_cancellation_preserves_inventory_and_ground_drops(world, monkeypatch, reason):
    first, second = kill(world, count=2)
    assert pickup(world, first)
    if reason == "closed":
        world.session.gameplay_quest_log[QUEST].state = QUEST_STATE_CLOSED
    elif reason == "missing":
        del world.session.gameplay_quest_log[QUEST]
    else:
        catalog = world.server._quest_catalog()
        quest = replace(catalog.quest(QUEST), deletable=False)
        monkeypatch.setattr(world.server, "_quest_catalog", lambda: replace(
            catalog, quests=MappingProxyType({**catalog.quests, quest.key: quest})))
    inventory = world.session.gameplay_inventory
    assert not world.server._quest_cancel(world.session, QUEST, force=reason != "not-deletable")
    assert world.session.gameplay_inventory is inventory
    assert second.item.item_id in world.session.gameplay_world_loot


def test_forced_silent_cancellation_still_removes_inventory_items(world):
    first, second = kill(world, count=2)
    assert pickup(world, first)
    mark = len(world.session.recorder.messages)
    assert world.server._quest_cancel(world.session, QUEST, force=True, notify=False)
    assert not world.session.gameplay_inventory.items
    assert not world.session.gameplay_world_loot
    assert len(world.session.recorder.messages) == mark



def test_player_cancellation_removes_collected_items(world, monkeypatch):
    catalog = world.server._quest_catalog()
    quest = replace(catalog.quest(QUEST), deletable=True)
    monkeypatch.setattr(world.server, "_quest_catalog", lambda: replace(
        catalog, quests=MappingProxyType({**catalog.quests, quest.key: quest})))
    drop, = kill(world)
    assert pickup(world, drop)
    assert world.server._quest_cancel(world.session, QUEST)
    assert not world.session.gameplay_inventory.items
    assert not world.server._quest_cancel(world.session, QUEST)


def test_cancellation_during_pickup_cannot_restore_cancelled_items(world, monkeypatch):
    drop, = kill(world)
    combat = world.session.gameplay_combat_state
    original_lock = combat.lock

    @contextmanager
    def cancel_before_pickup_commit():
        assert world.server._quest_cancel(world.session, QUEST, force=True, notify=False)
        with original_lock:
            yield

    # Pickup has snapshotted the drop before consulting combat for live health.
    monkeypatch.setattr(combat, "lock", cancel_before_pickup_commit())
    assert not pickup(world, drop)
    assert not world.session.gameplay_inventory.items
    assert not world.session.gameplay_world_loot
    assert QUEST not in world.session.gameplay_quest_log
