"""Achievement rewards and shared progress traverse the real SQLite boundary."""

from dataclasses import replace
import copy
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import pytest

from dso_server.gameplay.achievement_content import AchievementProgress
from dso_server.persistence.account_state import AccountStateStore
from dso_server.protocol.gameplay_protocol import Message
from dso_server.protocol.inventory_economy_protocol import InventoryMutationBuilder
from dso_server.protocol.player_init_protocol import decode_server_command_batch
from dso_server.runtime.transport import Session
from dso_server.services.character_server import CharacterServer
from dso_server.tests.fixtures.characters import character_profile, character_server
from dso_server.tests.test_achievements import _catalog, _definition


def initialize(server, identity=200_000_001):
    s = Session(("127.0.0.1", identity % 10000 + 46000), Mock())
    s.gameplay_character = next(c for c in server.character_selection.characters
                                if c.character_id == identity)
    s.gameplay_map = "a0200_kingscity"
    state = server._resolve_initialization_character(s)
    server._resolve_initialization_inventory(state)
    server._encode_initialization_new_player(state)
    server._resolve_initialization_talents(state)
    server._resolve_initialization_persistent_effects(s, state)
    server._resolve_initialization_vitals(state)
    server._apply_initialization_state(s, state)
    return s


@pytest.fixture
def player(monkeypatch):
    case = unittest.TestCase()
    try:
        server = character_server(case, [character_profile(), character_profile(
            200_000_002, "Second", class_id=2)])
        sent = Mock()
        monkeypatch.setattr(CharacterServer, "_send_world_loot_state_batch", sent)
        yield server, initialize(server), sent
    finally:
        case.doCleanups()


def amount(session, template):
    return sum(i.amount for i in session.gameplay_inventory.items if i.template_id == template)


def shared(server):
    server.account_store.account_registry = SimpleNamespace(
        wallet_owner=lambda identity: (17, (200_000_001, 200_000_002)))


CRAFT = "crafting_magic_equipment_01"
BOOK = "recipe_rarity_crafting_rare"


def test_authored_crafting_reward_is_durable_before_send_and_never_repeats(player):
    server, s, sent = player
    seen = []
    def check_saved(*args, **kwargs):
        saved = server.account_store.get(s.gameplay_character.character_id)
        seen.append(saved.achievements[CRAFT]["reward_status"])
        assert sum(i.amount for i in saved.inventory.items if i.template_id == BOOK) == 1
    sent.side_effect = check_saved
    server._achievement_feed(s, "crafting", ("rarity_uncommon_to_magic_rule",), count=10)
    assert amount(s, BOOK) == 1
    assert seen and set(seen) == {"granted"}
    first_stamp = s.gameplay_achievements[CRAFT].completed_at
    server._achievement_feed(s, "crafting", ("rarity_uncommon_to_magic_rule",), count=10)
    restored = initialize(server)
    server._achievement_feed(restored, "crafting", ("rarity_uncommon_to_magic_rule",), count=10)
    assert amount(restored, BOOK) == 1
    assert restored.gameplay_achievements[CRAFT].completed_at == first_stamp


def test_a_reward_shows_the_item_received_notification(player):
    server, s, sent = player
    server._achievement_feed(s, "crafting", ("rarity_uncommon_to_magic_rule",), count=10)
    (call,) = [call for call in sent.call_args_list
               if call.kwargs.get("label") == "achievement-reward"]
    (toast,) = [command.value for command in decode_server_command_batch(*call.args[1:3])
                if isinstance(command.value, Message)]
    book = next(item for item in s.gameplay_inventory.items if item.template_id == BOOK)
    assert toast.message_type == 2
    assert toast.arguments[1].uint32_value == 1
    assert toast.arguments[2].uint32_value == book.item_id


def test_full_inventory_keeps_pending_reward_for_relog_retry(player, monkeypatch):
    server, s, _ = player
    with monkeypatch.context() as patch:
        patch.setattr(InventoryMutationBuilder, "add_item", Mock(side_effect=ValueError("no free slot")))
        server._achievement_feed(s, "crafting", ("rarity_uncommon_to_magic_rule",), count=10)
    assert s.gameplay_achievements[CRAFT].reward_status == "pending"
    assert amount(s, BOOK) == 0
    restored = initialize(server)
    server._achievement_feed(restored, "enter_map", ("a0200_kingscity",))
    assert restored.gameplay_achievements[CRAFT].reward_status == "granted"
    assert amount(restored, BOOK) == 1


def test_failed_publication_does_not_repeat_committed_reward(player):
    server, s, sent = player
    sent.side_effect = OSError("socket closed")
    server._achievement_feed(s, "crafting", ("rarity_uncommon_to_magic_rule",), count=10)
    assert amount(initialize(server), BOOK) == 1
    server._achievement_feed(s, "crafting", ("rarity_uncommon_to_magic_rule",), count=10)
    assert amount(s, BOOK) == 1


def test_save_failure_before_store_publication_rolls_back_reward(player, monkeypatch):
    server, s, sent = player
    before = copy.deepcopy(s.gameplay_inventory)
    with monkeypatch.context() as patch:
        patch.setattr(server, "_persist_gameplay_session", Mock(side_effect=OSError("save unavailable")))
        with pytest.raises(OSError, match="save unavailable"):
            server._achievement_feed(s, "crafting", ("rarity_uncommon_to_magic_rule",), count=10)
    assert s.gameplay_inventory == before
    assert CRAFT not in s.gameplay_achievements
    sent.assert_not_called()
    server._achievement_feed(s, "crafting", ("rarity_uncommon_to_magic_rule",), count=10)
    assert amount(s, BOOK) == 1


def test_autosave_cannot_capture_grant_before_shared_receipt(player, monkeypatch):
    server, s, _ = player
    shared(server)
    definition = replace(_definition("shared_reward", "kill", ("mob",)),
                         account_wide=True, reward_item=BOOK, reward_amount=1)
    monkeypatch.setattr(server, "_achievement_catalog", lambda: _catalog(definition))
    original = server._prepare_achievement_rewards
    staged, release, autosaved = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def pause(*args):
        result = original(*args)
        staged.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(server, "_prepare_achievement_rewards", pause)
    def feed():
        try: server._achievement_feed(s, "kill", ("mob",))
        except BaseException as exc: errors.append(exc)
    def autosave():
        try:
            server._persist_gameplay_session(s, reason="interval", flush=True)
            autosaved.set()
        except BaseException as exc: errors.append(exc)
    worker = threading.Thread(target=feed)
    worker.start()
    assert staged.wait(5)
    saver = threading.Thread(target=autosave)
    saver.start()
    try:
        assert not autosaved.wait(0.05)
    finally:
        release.set()
        worker.join(5)
        saver.join(5)
    assert not worker.is_alive() and not saver.is_alive()
    assert errors == [] and autosaved.is_set()
    assert server.account_store.account_achievements[17][definition.key]["reward_status"] == "granted"
    assert amount(initialize(server), BOOK) == 1


def test_achievement_save_does_not_publish_premium_refresh_before_commit(player, monkeypatch):
    server, s, _ = player
    monkeypatch.setattr(CharacterServer, "_refresh_premium_bags", Mock(side_effect=AssertionError("early publish")))
    server._achievement_feed(s, "crafting", ("rarity_uncommon_to_magic_rule",), count=10)
    assert amount(initialize(server), BOOK) == 1


def test_flush_failure_after_queue_retains_receipt_without_duplicate(player, monkeypatch):
    server, s, sent = player
    shared(server)
    definition = replace(_definition("shared_reward", "kill", ("mob",)),
                         account_wide=True, reward_item=BOOK, reward_amount=1)
    monkeypatch.setattr(server, "_achievement_catalog", lambda: _catalog(definition))
    with monkeypatch.context() as patch:
        patch.setattr(server.account_store, "flush", Mock(side_effect=OSError("flush unavailable")))
        with pytest.raises(OSError, match="flush unavailable"):
            server._achievement_feed(s, "kill", ("mob",))
    sent.assert_not_called()
    assert amount(s, BOOK) == 1
    server._achievement_feed(s, "kill", ("mob",))
    assert amount(initialize(server), BOOK) == 1
    assert server.account_store.account_achievements[17][definition.key]["reward_status"] == "granted"


def test_failed_encoding_keeps_item_and_claim_unmodified(player, monkeypatch):
    from dso_server.services import achievements
    server, s, _ = player
    original = achievements.encode_server_command_batch
    def fail_reward(commands, **kwargs):
        if len(commands) == 3:  # inventory delta, notification, final inventory
            raise ValueError("cannot encode reward")
        return original(commands, **kwargs)
    monkeypatch.setattr(achievements, "encode_server_command_batch", fail_reward)
    before = copy.deepcopy(s.gameplay_inventory)
    server._achievement_feed(s, "crafting", ("rarity_uncommon_to_magic_rule",), count=10)
    assert s.gameplay_inventory == before
    assert s.gameplay_achievements[CRAFT].reward_status == "pending"


def test_class_specific_authored_reward_selects_character_class(player):
    server, s, _ = player
    s.gameplay_achievements["general_kill_darkforce_01"] = AchievementProgress(15, "2026-10-01 00:00:00", "legacy")
    server._achievement_feed(s, "kill", ("a0702_dun_boss_sargon_shadow",), count=30)
    assert amount(s, "mage_unique_amulet_ce_teaser") == 1
    assert amount(s, "ranger_unique_amulet_ce_teaser") == 0


def test_old_completed_documents_keep_unlocks_without_retroactive_rewards(player):
    server, s, _ = player
    s.gameplay_achievements[CRAFT] = AchievementProgress.from_document({
        "progress": 10, "completed_at": "2026-10-01 00:00:00"})
    server._achievement_feed(s, "crafting", ("rarity_uncommon_to_magic_rule",), count=10)
    assert amount(s, BOOK) == 0
    assert s.gameplay_achievements[CRAFT].reward_status == "legacy"


def test_shared_account_counter_accumulates_concurrent_characters_once(player, monkeypatch):
    server, first, _ = player
    shared(server)
    definition = replace(_definition("shared_kill", "kill", ("mob",), 100), account_wide=True)
    catalog = _catalog(definition)
    monkeypatch.setattr(server, "_achievement_catalog", lambda: catalog)
    second = initialize(server, 200_000_002)
    barrier, errors = threading.Barrier(2), []
    def work(session):
        try:
            barrier.wait(timeout=3)
            server._achievement_feed(session, "kill", ("mob",), count=7)
        except BaseException as exc:
            errors.append(exc)
    workers = [threading.Thread(target=work, args=(s,)) for s in (first, second)]
    for worker in workers: worker.start()
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()
    assert errors == []
    snapshot = server.account_store.achievement_snapshot(first.gameplay_character.character_id, catalog)
    assert snapshot[definition.key]["progress"] == 14
    server._achievement_init_info(first)
    assert first.gameplay_achievements[definition.key].progress == 14
    with sqlite3.connect(server.account_store.path) as database:
        assert database.execute("SELECT count(*) FROM account_achievements").fetchone()[0] == 1


def test_shared_reward_claim_is_once_per_account_and_survives_character_deletion(player, monkeypatch):
    server, first, _ = player
    shared(server)
    definition = replace(_definition("shared_reward", "kill", ("mob",)),
                         account_wide=True, reward_item=BOOK, reward_amount=1)
    catalog = _catalog(definition)
    monkeypatch.setattr(server, "_achievement_catalog", lambda: catalog)
    second = initialize(server, 200_000_002)
    server._achievement_feed(first, "kill", ("mob",))
    server._achievement_feed(second, "kill", ("mob",))
    assert amount(first, BOOK) == 1 and amount(second, BOOK) == 0
    server.account_store.delete(first.gameplay_character.character_id, flush=True)
    reopened = AccountStateStore(server.account_store.path)
    try:
        reopened.account_registry = server.account_store.account_registry
        snapshot = reopened.achievement_snapshot(200_000_002, catalog)
        assert snapshot[definition.key]["reward_status"] == "granted"
    finally:
        reopened.close()


def test_account_level_unlocks_are_visible_across_classes(player):
    server, first, _ = player
    shared(server)
    second = initialize(server, 200_000_002)
    server._achievement_on_class_level(first, 1, 45)
    server._achievement_on_class_level(second, 2, 45)
    info = server._achievement_init_info(first)
    complete = {row.resource_id for row in info.achievements if row.values[1]}
    assert {"class_create_mage_lvl_45", "class_create_ranger_lvl_45"} <= complete


def test_old_account_copies_migrate_by_high_water_without_summing(player, monkeypatch):
    server, first, _ = player
    shared(server)
    definition = replace(_definition("shared_kill", "kill", ("mob",), 100), account_wide=True)
    catalog = _catalog(definition)
    monkeypatch.setattr(server, "_achievement_catalog", lambda: catalog)
    second = initialize(server, 200_000_002)
    for session, progress in ((first, 17), (second, 12)):
        session.gameplay_achievements[definition.key] = AchievementProgress(progress)
        server.account_store.put(server._capture_account_state(session), flush=True)
    server._achievement_feed(second, "kill", ("mob",))
    assert server.account_store.achievement_snapshot(200_000_001, catalog)[definition.key]["progress"] == 18


def test_stale_capture_cannot_erase_reward_receipt_or_inventory(player):
    server, s, _ = player
    stale = server._capture_account_state(s)
    server._achievement_feed(s, "crafting", ("rarity_uncommon_to_magic_rule",), count=10)
    server.account_store.put(stale, flush=True)
    assert amount(initialize(server), BOOK) == 1


def test_new_character_restart_does_not_grant_rune_achievements(player):
    server, s, _ = player
    server.account_store.put(server._capture_account_state(s), flush=True)
    reopened = AccountStateStore(server.account_store.path)
    try:
        assert not any(key.startswith("item_use_item_rune_extension")
                       for key in reopened.get(s.gameplay_character.character_id).achievements)
    finally:
        reopened.close()
