"""Current opal loot-table recipes commit exact outputs or change nothing."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import replace
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dso_server.gameplay.crafting_outputs import resolve_crafting_outputs
from dso_server.gameplay.achievement_content import AchievementProgress
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.character_selection import BitWriter
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.protocol.inventory_economy_protocol import (
    CRAFTING_COMMAND, crafting_price_for_payment, load_crafting_recipe,
)
from dso_server.protocol.player_init_protocol import (
    CRAFTING_RESULT_COMMAND, CurrencyChanged, decode_server_command_batch,
)
from dso_server.runtime.transport import Session
from dso_server.services.base import CharacterService
from dso_server.services.character_server import CharacterServer  # registers composed services
from dso_server.services.inventory_interactions import InventoryInteractionService
from dso_server.services import inventory_interactions
from dso_server.tests.fixtures.characters import character_server
from dso_server.tests.test_achievement_rewards import initialize


OPAL = "gem_healthpoints_armor_resistAll_i"
DIVIDER = "gem_combined_dismantle_tool"
RECIPE = OPAL + "_dismantle"
GEMS = ("gem_amethyst_i", "gem_cyanit_i", "gem_diamond_i")


def packet(recipe_id=RECIPE, item_ids=(11, 12), *, amount=1, payment=0):
    writer = BitWriter()
    writer.write_bytes(b"\x8b" + CRAFTING_COMMAND.to_bytes(2, "little"))
    writer.write_uint8(0)
    writer.write_uint32_le(len(item_ids))
    for item_id in item_ids:
        writer.write_uint32_le(item_id)
    writer.write_uint8(payment)
    writer.write_uint8(0xFF)
    writer.write_uint32_le(amount)
    writer.write_string(recipe_id)
    for value in (2, 0, 0):
        writer.write_uint32_le(value)
    return writer.finish()[0]


def add_item(s, resource, item_id, amount=1):
    s.gameplay_inventory.items.append(ItemInfo(
        item_id=item_id, template_id=resource, amount=amount, item_level=1,
    ))
    s.gameplay_inventory.storage_58[item_id] = len(s.gameplay_inventory.storage_58)


@pytest.fixture
def player(monkeypatch):
    server = InventoryInteractionService()
    server.static_database_path = DEFAULT_STATIC_DB_PATH
    server._daily_challenge_feed = Mock()
    server._handle_enchantment_transfer = Mock()
    s = Session(("127.0.0.1", 47619), Mock())
    s.gameplay_inventory = InventoryInfo(storage_size=8, extra_storage_size=0)
    s.gameplay_character = SimpleNamespace(character_id=123, level=100, class_id=1)
    s.gameplay_achievements = {}
    s.gameplay_currency_state = CurrencyChanged(1_000_000_000, 1_000_000_000, (0, 0, 0), 0, False)
    s.gameplay_next_item_id = 100
    add_item(s, OPAL, 11)
    add_item(s, DIVIDER, 12)
    sent, saved, achievements, quests = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(CharacterService, "_send_world_loot_state_batch", sent)
    monkeypatch.setattr(CharacterService, "_request_account_save", saved)
    monkeypatch.setattr(CharacterService, "_achievement_feed", achievements)
    monkeypatch.setattr(CharacterService, "_quest_feed", quests)
    return SimpleNamespace(server=server, s=s, sent=sent, saved=saved,
                           achievements=achievements, quests=quests)


def items(s):
    return Counter({item.template_id: item.amount for item in s.gameplay_inventory.items})


def snapshot(s):
    return copy.deepcopy((s.gameplay_inventory, s.gameplay_currency_state,
                          s.gameplay_currency_ic, s.gameplay_next_item_id))


def grant_access(s, recipe_id):
    with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as database:
        book, achievement = database.execute(
            "SELECT RecipeItemReference, AchievementReference FROM _Template_CraftingRecipes WHERE Id=?",
            (recipe_id,),
        ).fetchone()
    if book:
        s.gameplay_recipe_book_resources = (*s.gameplay_recipe_book_resources, book)
    if achievement:
        s.gameplay_achievements[achievement.casefold()] = SimpleNamespace(completed=True)


def test_every_current_loot_table_recipe_has_exact_authored_three_gem_outputs():
    with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as database:
        rows = database.execute(
            "SELECT r.Id,o.IngredientItemId1,o.IngredientItemId2,o.IngredientItemId3 "
            "FROM _Template_CraftingRecipes r LEFT JOIN _Template_OpalCrafting o "
            "ON r.CraftedItemId='DISMANTLE_'||o.CraftedItemId "
            "WHERE r.IsLive=1 AND r.ResultIsLootTable=1"
        ).fetchall()
    assert len(rows) == 2574
    for recipe_id, *components in rows:
        recipe = load_crafting_recipe(DEFAULT_STATIC_DB_PATH, recipe_id)
        output = resolve_crafting_outputs(DEFAULT_STATIC_DB_PATH, recipe)
        assert tuple((entry.template_id, entry.amount) for entry in output) == tuple(
            (value.partition(":")[0], int(value.partition(":")[2])) for value in components
        )
        assert sum(entry.amount for entry in output) == 3


def test_unknown_or_malformed_result_table_never_guesses_rewards():
    recipe = load_crafting_recipe(DEFAULT_STATIC_DB_PATH, RECIPE)
    for altered in (
        replace(recipe, result_template_id="UNRECOVERED_random_rewards"),
        replace(recipe, result_template_id="DISMANTLE_missing_opal"),
        replace(recipe, ingredients=((OPAL, 1),)),
        replace(recipe, ingredients=((OPAL, 1), (OPAL, 1), (DIVIDER, 1))),
    ):
        with pytest.raises(ValueError):
            resolve_crafting_outputs(DEFAULT_STATIC_DB_PATH, altered)


def test_dismantle_commits_three_gems_price_and_one_execution_event(player):
    p = player
    original_gold = p.s.gameplay_currency_state.gold
    p.server._handle_crafting(p.s, packet(RECIPE + "0"))
    assert items(p.s) == Counter(dict.fromkeys(GEMS, 1))
    recipe = load_crafting_recipe(DEFAULT_STATIC_DB_PATH, RECIPE)
    price = crafting_price_for_payment(recipe.price, 0, 1, player_level=100, scale_vc_to_player_level=True)
    assert p.s.gameplay_currency_state.gold == original_gold - price.vc
    p.achievements.assert_called_once_with(p.server, p.s, "crafting", (RECIPE,), count=1, deferred_packets=[])
    p.server._daily_challenge_feed.assert_called_once_with(p.s, "ItemsCrafted", count=3)
    commands = decode_server_command_batch(p.sent.call_args.args[1], p.sent.call_args.args[2])
    result = next(command.value for command in commands if command.command_id == CRAFTING_RESULT_COMMAND)
    assert Counter({item.template_id: item.amount for item in result.items}) == Counter(dict.fromkeys(GEMS, 1))
    assert p.s.gameplay_inventory.equipment_slots == {}
    p.saved.assert_called_once()


def test_batch_consumes_exact_counts_preserves_partial_ids_and_counts_executions(player):
    p = player
    for item in p.s.gameplay_inventory.items:
        item.amount = 3
    p.server._handle_crafting(p.s, packet(amount=2))
    assert items(p.s) == Counter({OPAL: 1, DIVIDER: 1, **dict.fromkeys(GEMS, 2)})
    assert {item.item_id for item in p.s.gameplay_inventory.items if item.template_id in {OPAL, DIVIDER}} == {11, 12}
    p.achievements.assert_called_once_with(p.server, p.s, "crafting", (RECIPE,), count=2, deferred_packets=[])


def test_outputs_merge_existing_gem_stack_and_use_freed_ingredient_slots(player):
    p = player
    p.s.gameplay_inventory.storage_size = 3
    add_item(p.s, GEMS[0], 13, 5)
    p.server._handle_crafting(p.s, packet())
    assert items(p.s) == Counter({GEMS[0]: 6, GEMS[1]: 1, GEMS[2]: 1})
    assert len(p.s.gameplay_inventory.storage_58) == 3
    assert 13 not in p.s.gameplay_inventory.storage_58
    p.achievements.assert_called_once()


@pytest.mark.parametrize("failure", ["capacity", "currency", "ingredient", "duplicate", "count"])
def test_invalid_craft_changes_no_inventory_wallet_ids_or_progress(player, failure):
    p = player
    request = packet()
    if failure == "capacity":
        p.s.gameplay_inventory.storage_size = 2
    elif failure == "currency":
        p.s.gameplay_currency_state = replace(p.s.gameplay_currency_state, amount=0)
    elif failure == "ingredient":
        request = packet(amount=2)
    elif failure == "duplicate":
        request = packet(item_ids=(11, 11, 12))
    else:
        request = packet(amount=1_000_001)
    before = snapshot(p.s)
    p.server._handle_crafting(p.s, request)
    assert snapshot(p.s) == before
    for callback in (p.sent, p.saved, p.achievements, p.quests):
        callback.assert_not_called()


def test_replay_cannot_duplicate_outputs_or_progress(player):
    p = player
    request = packet()
    p.server._handle_crafting(p.s, request)
    committed = snapshot(p.s)
    p.server._handle_crafting(p.s, request)
    assert snapshot(p.s) == committed
    p.sent.assert_called_once()
    p.achievements.assert_called_once()


def test_concurrent_duplicate_requests_commit_only_one_craft(player):
    p = player
    ready = threading.Barrier(2)

    def craft():
        ready.wait(timeout=5)
        p.server._handle_crafting(p.s, packet())

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: craft(), range(2)))
    assert items(p.s) == Counter(dict.fromkeys(GEMS, 1))
    p.sent.assert_called_once()
    p.achievements.assert_called_once()


def test_packet_encoding_failure_rolls_back_before_commit(player, monkeypatch):
    p = player
    before = snapshot(p.s)
    monkeypatch.setattr(inventory_interactions, "encode_server_command_batch", Mock(side_effect=ValueError("encode failure")))
    p.server._handle_crafting(p.s, packet())
    assert snapshot(p.s) == before
    p.achievements.assert_not_called()
    p.sent.assert_not_called()


def test_concurrent_wallet_change_is_not_overwritten_at_commit(player, monkeypatch):
    p = player
    original_inventory = copy.deepcopy(p.s.gameplay_inventory)
    encode = inventory_interactions.encode_server_command_batch

    def encode_and_change_wallet(*args, **kwargs):
        result = encode(*args, **kwargs)
        p.s.gameplay_currency_state = replace(p.s.gameplay_currency_state, amount=123)
        return result

    monkeypatch.setattr(inventory_interactions, "encode_server_command_batch", encode_and_change_wallet)
    p.server._handle_crafting(p.s, packet())
    assert p.s.gameplay_currency_state.gold == 123
    assert p.s.gameplay_inventory == original_inventory
    assert p.s.gameplay_next_item_id == 100
    p.achievements.assert_not_called()
    p.sent.assert_not_called()


def test_send_failure_after_commit_does_not_skip_persistence_or_progress(player):
    p = player
    p.sent.side_effect = OSError("network unavailable")
    with pytest.raises(OSError, match="network unavailable"):
        p.server._handle_crafting(p.s, packet())
    assert items(p.s) == Counter(dict.fromkeys(GEMS, 1))
    p.achievements.assert_called_once()
    p.saved.assert_called_once()
    p.quests.assert_called_once()
    p.server._handle_crafting(p.s, packet())
    p.achievements.assert_called_once()


def test_direct_recipe_and_opal_creation_preserve_their_authored_outputs(player):
    p = player
    p.s.gameplay_inventory = InventoryInfo(storage_size=8)
    add_item(p.s, "gem_amethyst_a", 21)
    add_item(p.s, "ingredient_gem_dust", 22, 2)
    grant_access(p.s, "gem_amethyst_a_to_b")
    p.server._handle_crafting(p.s, packet("gem_amethyst_a_to_b0", (21, 22)))
    assert items(p.s) == Counter({"gem_amethyst_b": 1})
    p.s.gameplay_inventory = InventoryInfo(storage_size=8)
    for item_id, resource in enumerate(GEMS, 31):
        add_item(p.s, resource, item_id)
    add_item(p.s, "ingredient_gem_dust", 34, 4500)
    grant_access(p.s, "opal_creation_crafting_i")
    p.server._handle_crafting(p.s, packet("opal_creation_crafting_i", (31, 32, 33, 34)))
    assert items(p.s) == Counter({OPAL: 1})


def test_enchantment_transfer_cannot_bypass_earned_recipe_gate(player):
    p = player
    recipe_id = "enchantment_transfer_all_rule"
    request = packet(recipe_id)
    p.server._handle_crafting(p.s, request)
    p.server._handle_enchantment_transfer.assert_not_called()
    grant_access(p.s, recipe_id)
    p.server._handle_crafting(p.s, request)
    p.server._handle_enchantment_transfer.assert_called_once()


@pytest.mark.parametrize("fail_craft_send", [False, True])
def test_real_tenth_craft_persists_reward_then_publishes_allocations_in_order(monkeypatch, fail_craft_send):
    case = unittest.TestCase()
    try:
        server = character_server(case)
        s = initialize(server)
        s.gameplay_inventory = InventoryInfo(storage_size=8)
        s.gameplay_currency_state = CurrencyChanged(1_000_000, 1_000_000_000, (0, 0, 0), 0, False)
        s.gameplay_persist_callback = lambda session, reason, flush: server._persist_gameplay_session(
            session, reason=reason, flush=flush,
        )
        s.gameplay_next_item_id = 200
        recipe_id = "potion_health_restore_rarity_01"
        achievement_id = "crafting_potion_rarity_01"
        reward = "recipe_potion_all_rarity_02_unlock_01"
        grant_access(s, recipe_id)
        recipe = load_crafting_recipe(DEFAULT_STATIC_DB_PATH, recipe_id)
        for item_id, (template, quantity) in enumerate(recipe.ingredients, 101):
            add_item(s, template, item_id, quantity)
        s.gameplay_achievements[achievement_id] = AchievementProgress(9)
        messages = []

        def send(session, payload, bits, *, label):
            persisted = server.account_store.get(session.gameplay_character.character_id)
            assert persisted.achievements[achievement_id]["reward_status"] == "granted"
            assert sum(i.amount for i in persisted.inventory.items if i.template_id == reward) == 1
            messages.append((label, decode_server_command_batch(payload, bits)))
            if fail_craft_send and label == "workbench-crafting":
                raise OSError("craft publication failed")

        monkeypatch.setattr(CharacterServer, "_send_world_loot_state_batch", send)
        quests, daily = Mock(), Mock()
        monkeypatch.setattr(CharacterServer, "_quest_feed", quests)
        monkeypatch.setattr(CharacterServer, "_daily_challenge_feed", daily)
        request = packet(recipe_id, tuple(range(101, 101 + len(recipe.ingredients))))
        if fail_craft_send:
            with pytest.raises(OSError, match="craft publication failed"):
                server._handle_crafting(s, request)
        else:
            server._handle_crafting(s, request)
        labels = [label for label, _ in messages]
        assert labels[0] == "workbench-crafting"
        assert labels.index("achievement-reward") > labels.index("workbench-crafting")
        assert items(s) == Counter({recipe_id: 1, reward: 1})
        reward_views = next(commands for label, commands in messages if label == "achievement-reward")
        final_allocation = reward_views[-1].value.storage_58
        assert final_allocation == s.gameplay_inventory.storage_58
        quests.assert_called_once()
        assert daily.call_count == 2
        server._handle_crafting(s, request)
        assert items(s) == Counter({recipe_id: 1, reward: 1})
        quests.assert_called_once()
    finally:
        case.doCleanups()
