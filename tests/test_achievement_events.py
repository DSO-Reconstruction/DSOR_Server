"""Authored achievement event kinds and their authoritative gameplay hooks."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dso_server.gameplay.achievement_content import (
    AchievementProgress, apply_achievement_event, load_achievement_catalog,
)
from dso_server.gameplay.event_system import load_event_catalog
from dso_server.gameplay.season_pass import SeasonPassCatalog, normalize_season_pass_state
from dso_server.gameplay.local_combat_state import DamageResolution
from dso_server.gameplay.stickerbook_state import load_cosmetic_definition
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import (
    AddStickerbookFromInventoryRequest, encode_add_stickerbook_from_inventory,
    InventoryItemAction, encode_inventory_item_action,
)
from dso_server.protocol.inventory_economy_protocol import CurrencyChanged
from dso_server.protocol.chest_loot_protocol import ChestContents, ChestReward
from dso_server.protocol.commerce_protocol import load_purchase_item_definition
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.protocol.pvp_match_protocol import MATCH_MODE_BEST_OF_3_1V1
from dso_server.services.base import CharacterService
from dso_server.services.character_server import CharacterServer
from dso_server.tests import test_achievements as achievement_fixtures
from dso_server.tests import test_pvp_elimination as arena_fixtures
from dso_server.tests import test_pvp_player_effects as combat_fixtures


@pytest.fixture(scope="module")
def catalog():
    return load_achievement_catalog(DEFAULT_STATIC_DB_PATH)


@pytest.fixture
def player():
    case = achievement_fixtures.AchievementServiceTests()
    case.setUp()
    try:
        yield case.server, case.session
    finally:
        case.doCleanups()


def test_crafting_counts_recipe_executions_once_per_matching_alias(catalog):
    state = {}
    recipe = "rarity_uncommon_to_magic_rule"
    apply_achievement_event(catalog, state, "crafting", (recipe, recipe), count=4)
    assert state["crafting_magic_equipment_01"].progress == 4
    apply_achievement_event(catalog, state, "crafting", (recipe,), count=6)
    assert state["crafting_magic_equipment_01"].completed
    assert apply_achievement_event(catalog, state, "crafting", (recipe,), count=50) == ()
    assert "crafting_rare_equipment_01" not in state


def test_itemkill_requires_the_class_qualified_equipped_template(catalog):
    definition = catalog.achievement("skin_2h_gorga_FX_01")
    mage = next(value for value in definition.params if value.startswith("mage:"))
    state = {}
    assert apply_achievement_event(catalog, state, "itemkill", ("mage", mage.split(":")[1])) == ()
    apply_achievement_event(catalog, state, "itemkill", (mage, mage), count=7)
    assert state[definition.key].progress == 7


def test_equipment_counts_simultaneous_matching_items_not_repeated_clicks(catalog):
    definition = catalog.achievement("challenge_equip_ammon_set_01")
    state = {}
    for _ in range(10):
        apply_achievement_event(catalog, state, "equipitem", definition.params[:1])
    assert state[definition.key].progress == 1
    apply_achievement_event(catalog, state, "equipitem", definition.params[:definition.target])
    assert state[definition.key].completed


@pytest.mark.parametrize("kind,values,amount,key", (
    ("unlockskin", (), 10, "skin_unlock_amount_01"),
    ("stickerbook", ("Pets",), 25, "stickerbook_25_pets_01"),
    ("guildmembers", (), 10, "social_guild_members_10"),
    ("eventprogresscollected", ("level_rallye_event_01_mage",), 24, "level_rallye_step_25_mage"),
))
def test_absolute_progress_is_idempotent_and_requires_a_snapshot(catalog, kind, values, amount, key):
    state = {}
    assert apply_achievement_event(catalog, state, kind, values) == ()
    apply_achievement_event(catalog, state, kind, values, progress=amount - 1)
    assert state[key].progress == amount - 1
    assert not state[key].completed
    assert apply_achievement_event(catalog, state, kind, values, progress=amount - 1) == ()
    apply_achievement_event(catalog, state, kind, values, progress=amount)
    assert state[key].completed


def test_currency_and_use_events_respect_committed_amount_and_zero_deltas(catalog):
    state = {}
    apply_achievement_event(catalog, state, "collectedgamecurrency", ("money_cc",), count=30)
    apply_achievement_event(catalog, state, "collectedgamecurrency", ("money_cc",), count=0)
    assert state["collected_cook_currency_01"].progress == 30
    apply_achievement_event(catalog, state, "use", ("item_rune_extension_stone",) * 2)
    assert state["item_use_item_rune_extension_stone"].completed
    assert state["item_use_item_rune_extension_stone_02"].progress == 1


@pytest.mark.parametrize("fail_output", (False, True))
def test_currency_item_use_counts_once_even_when_primary_output_fails(player, fail_output):
    server, session = player
    session.gameplay_inventory = InventoryInfo(
        items=[ItemInfo(item_id=51, template_id="money_cc", amount=30)],
        storage_58={51: 0},
    )
    session.gameplay_next_item_id = 52
    session.gameplay_currency_state = CurrencyChanged(0, 0, (0, 0, 0), 0, False)
    session.gameplay_game_currencies = {"money_cc": 100}
    payload, _ = encode_inventory_item_action(InventoryItemAction(51, 0x13, 0, 0, 0))
    original_send = CharacterServer._send_world_loot_state_batch
    labels = []

    def send(s, packet, bits, *, label):
        labels.append(label)
        if fail_output and label == "currency-item-consumed":
            raise OSError("output queue unavailable")
        return original_send(s, packet, bits, label=label)

    with patch.object(CharacterServer, "_send_world_loot_state_batch", side_effect=send):
        if fail_output:
            with pytest.raises(OSError, match="output queue unavailable"):
                server._handle_inventory_item_action(session, payload)
        else:
            server._handle_inventory_item_action(session, payload)
    assert session.gameplay_game_currencies["money_cc"] == 130
    assert session.gameplay_achievements["collected_cook_currency_01"].progress == 30
    assert not any(item.item_id == 51 for item in session.gameplay_inventory.items)
    assert labels[0] == "currency-item-consumed"
    if not fail_output:
        assert "achievement-collectedgamecurrency" in labels[1:]
    server._handle_inventory_item_action(session, payload)
    assert session.gameplay_game_currencies["money_cc"] == 130
    assert session.gameplay_achievements["collected_cook_currency_01"].progress == 30


def test_mapped_chest_direct_currency_award_counts_once_without_inventory_use(player):
    server, session = player
    chest = "levelchest_lvl_60"
    session.gameplay_character.level = 100
    session.gameplay_inventory = InventoryInfo(
        items=[ItemInfo(item_id=51, template_id=chest, amount=1)], storage_58={51: 0},
    )
    session.gameplay_next_item_id = 52
    session.gameplay_currency_state = CurrencyChanged(0, 0, (0, 0, 0), 0, False)
    session.gameplay_game_currencies = {"money_cc": 100}
    contents = ChestContents(chest, "deterministic-currency-roll", (
        ChestReward(load_purchase_item_definition(DEFAULT_STATIC_DB_PATH, "money_cc"), 30),
    ))
    payload, _ = encode_inventory_item_action(InventoryItemAction(51, 0x13, 0, 0, 0))
    with patch("dso_server.services.inventory_map.roll_chest_contents", return_value=contents), \
            patch.object(CharacterServer, "_persist_gameplay_session", return_value=False):
        server._handle_inventory_item_action(session, payload)
        server._handle_inventory_item_action(session, payload)
    assert session.gameplay_game_currencies["money_cc"] == 130
    assert session.gameplay_achievements["collected_cook_currency_01"].progress == 30
    assert not any(item.template_id == "money_cc" for item in session.gameplay_inventory.items)
    labels = [label for _, _, label in session.recorder.messages]
    assert labels.index("mapped-chest-open-mapped-chest-open") < labels.index("achievement-collectedgamecurrency")


def test_season_pass_direct_currency_award_counts_once_on_claim_replay(player):
    server, session = player
    session.gameplay_inventory = InventoryInfo()
    session.gameplay_next_item_id = 52
    session.gameplay_currency_state = CurrencyChanged(0, 0, (0, 0, 0), 0, False)
    session.gameplay_game_currencies = {"money_cc": 100}
    session.gameplay_season_pass = normalize_season_pass_state({"level": 1})
    catalog = SeasonPassCatalog({}, ((1, 0),), {1: ("Loot:money_cc,30", "", "")})
    request = SimpleNamespace(award_ids=(1,))
    for _ in range(2):
        server._handle_season_pass_awards(session, request, session.gameplay_season_pass, catalog)
    assert session.gameplay_game_currencies["money_cc"] == 130
    assert session.gameplay_achievements["collected_cook_currency_01"].progress == 30
    labels = [label for _, _, label in session.recorder.messages]
    assert labels.index("season-pass-awards") < labels.index("achievement-collectedgamecurrency")


def test_retired_placeholder_kills_are_not_guessed(catalog):
    # Retail retired the "nomob" Kill rows; they stay without an evaluator.
    assert catalog.achievement("pw_kill_boss_mortis_02").event_kind is None
    assert catalog.achievement("general_kill_mortis_01").event_kind is None
    assert catalog.achievement("fracturedplanes_rank3").event_kind == "highscoreranking"
    assert apply_achievement_event(catalog, {}, "kill", ("nomob",), count=1000) == ()


def test_reward_receipts_survive_roundtrip_and_old_completions_are_legacy():
    for status in ("", "pending", "granted", "legacy"):
        value = AchievementProgress(10, "2026-10-26 10:00:00", status)
        assert AchievementProgress.from_document(value.to_document()) == value
    assert AchievementProgress.from_document({"progress": 10, "completed_at": "2026-10-26 10:00:00"}).reward_status == "legacy"
    assert AchievementProgress.from_document({"progress": 3}).reward_status == ""


def test_equipment_hook_counts_each_item_once_even_if_it_occupies_two_slots(player):
    server, session = player
    session.gameplay_inventory = InventoryInfo(
        items=[ItemInfo(item_id=1, template_id="test_equipment", field_08=4)],
        equipment_slots={1: [5, 6]},
    )
    for _ in range(8):
        server._achievement_on_equipment_changed(session)
    assert session.gameplay_achievements["general_equip_item_legendary_01"].completed
    assert session.gameplay_achievements["general_equip_item_legendary_06"].progress == 1


def test_monster_kill_hook_uses_only_current_equipment(player, catalog):
    server, session = player
    definition = catalog.achievement("skin_2h_gorga_FX_01")
    mage = next(value.split(":", 1)[1] for value in definition.params if value.startswith("mage:"))
    session.gameplay_character.class_id = 1
    session.gameplay_inventory = InventoryInfo(
        items=[ItemInfo(item_id=1, template_id=mage)], equipment_slots={1: [5, 6]},
    )
    monsters = (SimpleNamespace(template_id="test_monster"),) * 3
    with patch.object(CharacterServer, "_daily_challenge_monsters_killed"):
        server._achievement_on_monsters_killed(session, monsters)
        session.gameplay_inventory.equipment_slots.clear()
        server._achievement_on_monsters_killed(session, monsters)
    assert session.gameplay_achievements[definition.key].progress == 3


def test_real_sticker_unlock_and_replayed_snapshot_count_one_owned_pet(player):
    server, session = player
    session.gameplay_inventory = InventoryInfo(
        items=[ItemInfo(item_id=51, template_id="pet_goat", amount=1)],
        storage_58={51: 0},
    )
    session.gameplay_next_item_id = 52
    payload, bits = encode_add_stickerbook_from_inventory(AddStickerbookFromInventoryRequest(51))
    session.current_message_bit_length = bits
    with patch.object(CharacterServer, "_persist_gameplay_session", return_value=False):
        server._handle_add_stickerbook_from_inventory(session, payload)
    assert session.gameplay_achievements["stickerbook_first_pet_01"].completed
    assert session.gameplay_achievements["stickerbook_25_pets_01"].progress == 1
    with pytest.raises(ValueError):
        server._handle_add_stickerbook_from_inventory(session, payload)
    server._achievement_on_stickerbook_changed(session)
    assert session.gameplay_achievements["stickerbook_25_pets_01"].progress == 1


def test_pet_activation_feeds_use_but_deactivation_does_not(player):
    server, session = player
    definition = load_cosmetic_definition(DEFAULT_STATIC_DB_PATH, "pet_goat")
    original_feed = CharacterServer._achievement_feed
    with patch.object(CharacterServer, "_persist_gameplay_session", return_value=False), \
            patch.object(CharacterServer, "_refresh_admin_speed"), \
            patch.object(CharacterServer, "_achievement_feed", side_effect=original_feed) as feed:
        server._toggle_switchable_sticker(session, definition)
        server._toggle_switchable_sticker(session, definition)
    assert feed.call_count == 1
    assert session.gameplay_achievements["pets_use_pet_goat_01_lor"].completed


def test_real_event_progress_uses_the_total_without_double_counting(player, tmp_path):
    server, session = player
    schedule = tmp_path / "events.json"
    schedule.write_text(json.dumps({"events": [{"event_id": 65001,
        "template_id": "level_rallye_event_01_mage", "state": "active"}]}), encoding="utf-8")
    events = load_event_catalog(DEFAULT_STATIC_DB_PATH, schedule)
    session.gameplay_character.class_id = 1
    with patch.object(CharacterServer, "_event_catalog", return_value=events), \
            patch.object(CharacterServer, "_deliver_rewards"):
        for _ in range(2):
            assert server._advance_event_progress(
                session, collectible_id="level_rallye_progress_mage", amount=12,
            )
    assert session.gameplay_event_progress["65001"]["amount"] == 24
    assert session.gameplay_achievements["level_rallye_step_25_mage"].completed
    assert session.gameplay_achievements["level_rallye_step_50_mage"].progress == 24


def test_open_pvp_kill_is_credited_once_and_arena_kills_are_excluded():
    case = combat_fixtures.PVPPlayerEffectTests()
    case.setUp()
    try:
        resolution = DamageResolution(
            target_actor_id=case.target.gameplay_remote_actor_id,
            attacker_actor_id=case.source.gameplay_remote_actor_id,
            source_actor_id=case.source.gameplay_remote_actor_id,
            current_health=0, maximum_health=1000, damage=1000, killed=True,
            position=(25.49, 0.0, 53.57), server_tick=200,
            hit_tags=(0,), skill_index=0,
        )
        case.combat.player_health[case.target.gameplay_remote_actor_id][0] = 0
        for _ in range(2):
            case.server._handle_presence_event(case.source, ("pvp_combat_resolution", resolution))
        assert case.source.gameplay_achievements["pvp_kill_players_01"].progress == 1
        case.target.gameplay_death_pending = False
        case.source.gameplay_match = object()
        case.server._handle_presence_event(case.source, ("pvp_combat_resolution", replace(resolution, server_tick=201)))
        assert case.source.gameplay_achievements["pvp_kill_players_01"].progress == 1
    finally:
        case.doCleanups()


def test_duel_win_settlement_completes_authored_achievement_once():
    case = arena_fixtures.MultiplayerEliminationTests()
    case.setUp()
    try:
        match = case.arena(MATCH_MODE_BEST_OF_3_1V1)
        for _ in range(2):
            CharacterService._finish_match_round(case.server, match, 1)
            if match.phase != "over":
                CharacterService._begin_match_round(case.server, match)
                CharacterService._start_match_round(case.server, match, match.info_generation)
        CharacterService._finish_match(case.server, match, match.info_generation)
        winner = case.sessions[1]
        value = winner.gameplay_achievements["pvp_win_dm_01"].to_document()
        CharacterService._finish_match(case.server, match, match.info_generation)
        assert winner.gameplay_achievements["pvp_win_dm_01"].to_document() == value
        assert value["completed_at"]
        assert "pvp_win_dm_01" not in getattr(case.sessions[0], "gameplay_achievements", {})
    finally:
        case.doCleanups()
