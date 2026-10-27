"""Current quest effect rewards commit once with the full hand-in reward."""

import copy
from dataclasses import replace
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

import pytest

from dso_server.gameplay.quest_content import (
    QUEST_STATE_CLOSED, QUEST_STATE_FINISHED, QuestProgress, load_quest_catalog,
)
from dso_server.gameplay.achievement_content import AchievementCatalog, AchievementDefinition
from dso_server.gameplay.status_modifier_runtime import active_xp_gain_multiplier
from dso_server.paths import DEFAULT_QUESTS_PATH, DEFAULT_STATIC_DB_PATH
from dso_server.persistence.account_state import _state_from_document, _state_to_document
from dso_server.protocol.consumable_protocol import load_consumable_catalog
from dso_server.protocol.gameplay_protocol import STATUS_EFFECT_COMMAND
from dso_server.protocol.inventory_codec import ItemInfo
from dso_server.protocol.player_init_protocol import decode_server_command_batch
from dso_server.protocol.quest_protocol import (
    QUEST_OPERATION_TURN_IN, QuestCommand, encode_quest_command,
)
from dso_server.services import quest_status_rewards
from dso_server.services.character_server import CharacterServer
from dso_server.tests.fixtures.characters import character_server
from dso_server.tests.test_recipe_progression import initialize


CATALOG = load_quest_catalog(DEFAULT_QUESTS_PATH)
STATUS_QUESTS = tuple(quest.quest_id for quest in CATALOG.quests.values()
                      if any(reward.kind == "statuseffect" for reward in quest.rewards))
REACT = "reactivation-03-n"
EASTER = "e-easter-01-repeatable-01-n-re"
ZAH = "e-zah-01-oasis-01-n-re"


def install_timers(session):
    session.gameplay_deadline_timer = lambda delay, callback, args=(), kwargs=None: SimpleNamespace(
        delay=delay, callback=callback, args=args, start=Mock(), cancel=Mock(), daemon=False,
    )
    session.send_reliable_ordered = Mock()
    session.send_reliable_ordered_split = Mock()
    session.connected = True


@pytest.fixture
def player(monkeypatch):
    case = unittest.TestCase()
    try:
        server = character_server(case)
        session = initialize(server, 47831)
        install_timers(session)
        session.gameplay_persist_callback = lambda s, reason, flush: server._persist_gameplay_session(s, reason=reason, flush=flush)
        sent = Mock()
        monkeypatch.setattr(CharacterServer, "_send_world_loot_state_batch", sent)
        # Map/NPC phase work is independent of the reward transaction.
        for name in ("_quest_spawn_revealed_npcs", "_quest_refresh_map_phases", "_quest_sync_props", "_quest_fire_signal"):
            monkeypatch.setattr(server, name, Mock())
        yield server, session, sent
    finally:
        case.doCleanups()


def finished(session, quest_id):
    quest = CATALOG.quest(quest_id)
    session.gameplay_quest_log[quest.quest_id] = QuestProgress(
        quest.quest_id, state=QUEST_STATE_FINISHED,
        finished_mask=(1 << len(quest.tasks)) - 1,
    )


def effect(session, quest_id):
    resource = next(reward.value for reward in CATALOG.quest(quest_id).rewards if reward.kind == "statuseffect")
    catalog = load_consumable_catalog(DEFAULT_STATIC_DB_PATH)
    return next(entry for entry in session.gameplay_persistent_status_effects.values()
                if (definition := catalog.definition_for_effect(entry.effect_id)) is not None
                and definition.effect_resource_id == resource)


def quantities(inventory):
    result = {}
    for item in inventory.items:
        result[item.template_id] = result.get(item.template_id, 0) + item.amount
    return result


@pytest.mark.parametrize("quest_id", STATUS_QUESTS)
def test_every_current_authored_status_reward_reaches_the_client_and_saves(player, quest_id):
    server, session, sent = player
    finished(session, quest_id)
    assert server._quest_turn_in(session, quest_id)
    entry = effect(session, quest_id)
    definition = load_consumable_catalog(DEFAULT_STATIC_DB_PATH).definition_for_effect(entry.effect_id)
    assert entry.field_10 == definition.duration_ticks
    assert entry.field_18 == 0 and not entry.field_39
    assert session.gameplay_timed_status_effect_expiries[entry.field_04] > 0
    assert ("consumable-expire", entry.field_04) in session.gameplay_projectile_timers
    call = next(call for call in sent.call_args_list if call.kwargs.get("label") == "quest-status-rewards")
    commands = decode_server_command_batch(*call.args[1:])
    assert any(command.command_id == STATUS_EFFECT_COMMAND and entry in command.value.entries for command in commands)
    saved = server.account_store.get(session.gameplay_character.character_id)
    assert saved.quest_log[quest_id]["state"] == QUEST_STATE_CLOSED
    assert entry.field_04 in saved.status_effect_names
    sent.reset_mock()
    assert not server._quest_turn_in(session, quest_id)
    sent.assert_not_called()
    assert effect(session, quest_id) == entry


def test_request_commits_all_rewards_and_consumption_before_failed_first_delivery(player):
    server, session, sent = player
    finished(session, REACT)
    before = quantities(session.gameplay_inventory)
    original_rc = session.gameplay_currency_state.andermant
    item_id = session.gameplay_next_item_id
    session.gameplay_next_item_id += 1
    session.gameplay_inventory.items.append(ItemInfo(item_id, template_id="potion_health_restore_rarity_01", quest_id=REACT))
    session.gameplay_inventory.storage_58[item_id] = 7

    def outbound(*args, **kwargs):
        saved = server.account_store.get(session.gameplay_character.character_id)
        assert saved.quest_log[REACT]["state"] == QUEST_STATE_CLOSED
        assert saved.currency.andermant == original_rc + 500
        saved_items = quantities(saved.inventory)
        for template in ("react_bundle_gems_min_lvl_1", "react_bundle_consumables_min_lvl_1"):
            assert saved_items[template] == before.get(template, 0) + 1
        assert item_id not in {item.item_id for item in saved.inventory.items}
        assert "buff_reactivation_campaign_instant_01" in saved.status_effect_names.values()
        raise OSError("outbound unavailable")

    sent.side_effect = outbound
    payload, bits = encode_quest_command(QuestCommand(REACT, QUEST_OPERATION_TURN_IN))
    session.current_message_bit_length = bits
    with pytest.raises(OSError, match="outbound unavailable"):
        server._handle_quest_command(session, payload)
    assert not server._quest_turn_in(session, REACT)
    restored = initialize(server, 47832)
    assert effect(restored, REACT).field_18 == 0
    assert not server._quest_turn_in(restored, REACT)
    assert quantities(restored.gameplay_inventory)["react_bundle_gems_min_lvl_1"] == before.get("react_bundle_gems_min_lvl_1", 0) + 1


@pytest.mark.parametrize("quest_id,refresh", [(EASTER, False), (ZAH, False), (REACT, True)])
def test_repeated_completions_honor_deny_and_reset_runtime(player, monkeypatch, quest_id, refresh):
    server, session, _ = player
    monkeypatch.setattr(quest_status_rewards.time, "time", lambda: 1900000000.0)
    finished(session, quest_id)
    assert server._quest_turn_in(session, quest_id)
    first = effect(session, quest_id)
    expiry = session.gameplay_timed_status_effect_expiries[first.field_04]
    monkeypatch.setattr(quest_status_rewards.time, "time", lambda: 1900000100.0)
    finished(session, quest_id)  # Another legitimately finished repeat/run.
    assert server._quest_turn_in(session, quest_id)
    second = effect(session, quest_id)
    assert session.gameplay_timed_status_effect_expiries[second.field_04] == expiry + (100 if refresh else 0)
    assert second.field_18 == 0
    assert (second.field_04 == first.field_04) == (quest_id == EASTER)


@pytest.mark.parametrize("quest_id,offline,remaining", [(ZAH, 100, 1700), ("TestTriggeredQuest", 10000, 600)])
def test_real_time_and_paused_clocks_survive_codec_and_rejoin(player, monkeypatch, quest_id, offline, remaining):
    server, session, _ = player
    epoch = 1900000000.0
    monkeypatch.setattr(quest_status_rewards.time, "time", lambda: epoch)
    finished(session, quest_id)
    server._quest_turn_in(session, quest_id)
    original = effect(session, quest_id)
    captured = _state_from_document(_state_to_document(server._capture_account_state(session)))
    if quest_id == "TestTriggeredQuest":
        assert captured.timed_status_effect_expiries[original.field_04] == -600
    server.account_store.put(captured, flush=True)
    monkeypatch.setattr(quest_status_rewards.time, "time", lambda: epoch + offline)
    restored = initialize(server, 47833)
    install_timers(restored)
    entry = effect(restored, quest_id)
    assert entry.field_10 == remaining * 25
    assert restored.gameplay_timed_status_effect_expiries[entry.field_04] == epoch + offline + remaining
    server._restore_consumable_timers(restored)
    timer = restored.gameplay_projectile_timers[("consumable-expire", entry.field_04)]
    assert timer.delay == pytest.approx(remaining + .1)
    timer.callback(*timer.args)
    assert entry.field_04 not in restored.gameplay_persistent_status_effects


def test_expired_real_time_reward_does_not_restart_from_closed_quest(player, monkeypatch):
    server, session, _ = player
    monkeypatch.setattr(quest_status_rewards.time, "time", lambda: 1900000000.0)
    finished(session, ZAH)
    server._quest_turn_in(session, ZAH)
    entry = effect(session, ZAH)
    monkeypatch.setattr(quest_status_rewards.time, "time", lambda: 1900002000.0)
    restored = initialize(server, 47834)
    assert entry.field_04 not in restored.gameplay_persistent_status_effects
    assert not server._quest_turn_in(restored, ZAH)


@pytest.mark.parametrize("failure", ["encoding", "full-bag", "unknown-effect"])
def test_preflight_failure_leaves_finished_quest_and_all_state_unchanged(player, monkeypatch, failure):
    server, session, sent = player
    finished(session, REACT)
    if failure == "encoding":
        monkeypatch.setattr(quest_status_rewards, "encode_server_command_batch", Mock(side_effect=ValueError("encoding failed")))
    elif failure == "full-bag":
        monkeypatch.setattr(quest_status_rewards.InventoryMutationBuilder, "add_item", Mock(side_effect=ValueError("ordinary inventory storage is full")))
    else:
        monkeypatch.setattr(quest_status_rewards, "load_status_runtime_catalog", lambda _path: SimpleNamespace(effect_ids={}))
    before = copy.deepcopy((session.gameplay_inventory, session.gameplay_currency_state,
                            session.gameplay_persistent_status_effects, session.gameplay_timed_status_effect_expiries,
                            session.gameplay_next_status_effect_instance_id))
    assert not server._quest_turn_in(session, REACT)
    assert session.gameplay_quest_log[REACT].state == QUEST_STATE_FINISHED
    assert (session.gameplay_inventory, session.gameplay_currency_state,
            session.gameplay_persistent_status_effects, session.gameplay_timed_status_effect_expiries,
            session.gameplay_next_status_effect_instance_id) == before
    sent.assert_not_called()


def test_simultaneous_turn_ins_credit_status_currency_and_loot_once(player):
    server, session, _ = player
    finished(session, REACT)
    before = session.gameplay_currency_state.andermant
    barrier = threading.Barrier(2)
    results, errors = [], []
    def worker():
        try:
            barrier.wait(timeout=2)
            results.append(server._quest_turn_in(session, REACT))
        except BaseException as exc:
            errors.append(exc)
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert not errors
    assert sorted(results) == [False, True]
    assert session.gameplay_currency_state.andermant == before + 500
    assert effect(session, REACT).field_18 == 0


def test_reactivation_reward_contributes_its_authored_xp_multiplier(player):
    server, session, _ = player
    finished(session, REACT)
    before = active_xp_gain_multiplier(DEFAULT_STATIC_DB_PATH, tuple(session.gameplay_persistent_status_effects.values()))
    server._quest_turn_in(session, REACT)
    after = active_xp_gain_multiplier(DEFAULT_STATIC_DB_PATH, tuple(session.gameplay_persistent_status_effects.values()))
    assert after == pytest.approx(before + 1.0)


def test_authored_keep_on_death_preserves_live_rewards_and_removes_test_fire_buff(player):
    server, session, _ = player
    representatives = ("e-a0200-hel-02-generic-battleground-participation-01re-n",
                       "e-a0200-hel-02-generic-battleground-win-01re-n", EASTER,
                       ZAH, "e-zah-01-temple-01-n-re", REACT, "TestTriggeredQuest")
    for quest_id in representatives:
        finished(session, quest_id)
        assert server._quest_turn_in(session, quest_id)
    keep = {effect(session, quest_id).field_04 for quest_id in representatives[:-1]}
    fire = effect(session, "TestTriggeredQuest")
    timer = session.gameplay_projectile_timers[("consumable-expire", fire.field_04)]
    server._notify_player_death(session)
    assert keep <= set(session.gameplay_persistent_status_effects)
    assert fire.field_04 not in session.gameplay_persistent_status_effects
    assert fire.field_04 not in session.gameplay_timed_status_effect_expiries
    timer.cancel.assert_called_once()
    saved = server.account_store.get(session.gameplay_character.character_id)
    assert keep <= {entry.field_04 for entry in saved.status_effects}
    assert fire.field_04 not in saved.status_effect_names


def test_status_reward_quest_xp_and_currency_are_saved_even_if_publication_fails(player, monkeypatch):
    server, session, sent = player
    session.gameplay_character.level = 1
    session.gameplay_character.progression_u32 = 0
    session.gameplay_talent_category_points = (0, *session.gameplay_talent_category_points[1:])
    finished(session, "TestTriggeredQuest")
    refresh = Mock(wraps=CharacterServer._refresh_player_combat_vitals)
    level_hook = Mock()
    monkeypatch.setattr(CharacterServer, "_refresh_player_combat_vitals", refresh)
    monkeypatch.setattr(CharacterServer, "_achievement_on_level_up", level_hook)
    before_rc = session.gameplay_currency_state.andermant
    sent.side_effect = OSError("outbound unavailable")
    with pytest.raises(OSError, match="outbound unavailable"):
        server._quest_turn_in(session, "TestTriggeredQuest")
    saved = server.account_store.get(session.gameplay_character.character_id)
    assert saved.currency.andermant == before_rc + 100
    assert saved.talent_category_points[0] == 1000
    assert "questbuff_resistance_fire" in saved.status_effect_names.values()
    refresh.assert_called()
    level_hook.assert_called_once_with(session, session.gameplay_character.class_id, session.gameplay_character.level)


def test_failed_delivery_finishes_quest_achievement_despite_daily_and_season_send_errors(player, monkeypatch):
    server, session, sent = player
    definition = AchievementDefinition("test-status-reward-quest", "quest", (REACT,), 1, 0, (),
                                       "quests", False, False, "", False, "", 0)
    catalog = AchievementCatalog({definition.key: definition}, {"quest": (definition,)})
    monkeypatch.setattr(server, "_achievement_catalog", lambda: catalog)
    daily = Mock(side_effect=OSError("daily publication failed"))
    season = Mock(side_effect=OSError("season publication failed"))
    monkeypatch.setattr(server, "_daily_challenge_feed", daily)
    monkeypatch.setattr(server, "_season_pass_feed", season)
    monkeypatch.setattr(server, "_wanted_catalog", lambda: SimpleNamespace(missions={1: SimpleNamespace(quest_id=REACT)}))
    first_failure = OSError("first reward publication failed")
    sent.side_effect = first_failure
    finished(session, REACT)
    with pytest.raises(OSError) as caught:
        server._quest_turn_in(session, REACT)
    assert caught.value is first_failure
    daily.assert_called_once_with(session, "QuestsClosed")
    season.assert_called_once_with(session, "WantedThief")
    assert session.gameplay_achievements[definition.key].completed
    saved = server.account_store.get(session.gameplay_character.character_id)
    assert saved.achievements[definition.key]["completed_at"]
    assert not server._quest_turn_in(session, REACT)


def test_completion_hook_does_not_hide_unexpected_errors(player, monkeypatch):
    server, session, _ = player
    monkeypatch.setattr(server, "_daily_challenge_feed", Mock(side_effect=RuntimeError("unexpected daily bug")))
    with pytest.raises(RuntimeError, match="unexpected daily bug"):
        server._achievement_on_quest_closed(session, REACT)
