"""Boxes, Lucky Spheres and Deluxe teasers the player could not use.

FAILURE: "y'a des items que je peux pas utiliser genre les lucky sphere le
deluxe le daily limited chest".
"""

from datetime import datetime, timedelta, timezone
import random
import sqlite3
from unittest.mock import Mock, patch

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol import chest_loot_protocol
from dso_server.protocol import seasonal_activity_protocol as seasonal_proto
from dso_server.protocol.chest_loot_protocol import (
    UNRESOLVED_CHEST_MESSAGE, UnresolvedChestError, _AUTHORED_CHEST_CONTENTS,
    _AUTHORED_ROLL_CHESTS, _AUTHORED_WEIGHTED_CHESTS, _authored_reward_template, roll_chest_contents,
)
from dso_server.protocol.seasonal_activity_protocol import load_wishing_well_definitions
from dso_server.protocol.commerce_protocol import load_purchase_item_definition
from dso_server.protocol.gameplay_protocol import InventoryItemAction, encode_inventory_item_action
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.protocol.inventory_economy_protocol import CurrencyChanged
from dso_server.gameplay.membership_effects import membership_status_effect
from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
from dso_server.protocol.gameplay_protocol import STATUS_EFFECT_COMMAND
from dso_server.protocol.membership_protocol import (
    DELUXE_MEMBERSHIP, MEMBERSHIP_UPDATE_COMMAND, PREMIUM_MEMBERSHIP, extend_membership, parse_membership_expiry,
    premium_handover,
)
from dso_server.protocol.player_init_protocol import decode_server_command_batch
from dso_server.services.character_server import CharacterServer
from dso_server.services.membership_items import teaser_days, teaser_membership
from dso_server.tests import test_achievements as achievement_fixtures
from dso_server.tests.test_seasonal_protocol import seasonal  # noqa: F401


def _selective_items():
    database = sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        return [row[0] for row in database.execute(
            "SELECT Id FROM _Template_Item WHERE ItemCategory='Selective'")]
    finally:
        database.close()


@pytest.mark.parametrize("box,expected", [
    # "Open to receive one of the following: 1x Winter Solstice Breeze ..."
    # (description only: chest_contents_descriptions.json).
    ("emilia_daily_andermant_cost", None),
    # A listed Lucky Sphere without odds: one of its items, evenly.
    ("ls_mount_warthog_c", None),
    # A category Lucky Sphere: a pet of the Free Spirits sticker book.
    ("ls_pets_stickerbook_free_spirits_p", "pet_"),
    # The daily challenges' chests: an equipment item of their rarity.
    ("lootbox_daily_challenge_rare", None),
])
def test_boxes_without_a_server_loot_list_open(box, expected):
    contents = roll_chest_contents(DEFAULT_STATIC_DB_PATH, box, player_level=60, class_name="warrior",
                                   rng=random.Random(7))
    assert contents.rewards
    if expected:
        assert all(r.definition.template_id.startswith(expected) for r in contents.rewards)


def test_deluxe_teaser_durations_and_extension():
    assert teaser_days("deluxe_teaser_one_day") == 1
    assert teaser_days("deluxe_teaser_three_days_consumable_7d") == 3
    assert teaser_days("deluxe_teaser_one_year") == 365
    assert teaser_days("ls_dyes_c") is None
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later = (now + timedelta(days=10)).isoformat()
    # Extended from the current Deluxe expiry ...
    plan = extend_membership(DELUXE_MEMBERSHIP, later, True, "", DELUXE_MEMBERSHIP, 1, now=now)
    assert parse_membership_expiry(plan[1]) == now + timedelta(days=11) and plan[3] == ""
    # ... or from now when it has lapsed.
    past = (now - timedelta(days=3)).isoformat()
    plan = extend_membership(DELUXE_MEMBERSHIP, past, True, "", DELUXE_MEMBERSHIP, 7, now=now)
    assert parse_membership_expiry(plan[1]) == now + timedelta(days=7)


def _days(plan, now):
    return tuple(
        (parse_membership_expiry(value) - now) / timedelta(days=1) if value else None
        for value in (plan[1], plan[3])
    )


def test_premium_time_is_paused_while_deluxe_runs():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    at = lambda days: (now + timedelta(days=days)).isoformat()
    # Premium under a 10-day Deluxe starts when Deluxe ends.
    plan = extend_membership(DELUXE_MEMBERSHIP, at(10), True, "", PREMIUM_MEMBERSHIP, 14, now=now)
    assert plan[0] == DELUXE_MEMBERSHIP and _days(plan, now) == (10, 24)
    # More Deluxe pushes the paused Premium back by as much.
    plan = extend_membership(*plan[:4], DELUXE_MEMBERSHIP, 3, now=now)
    assert plan[0] == DELUXE_MEMBERSHIP and _days(plan, now) == (13, 27)
    # More Premium adds to the paused time.
    plan = extend_membership(*plan[:4], PREMIUM_MEMBERSHIP, 1, now=now)
    assert _days(plan, now) == (13, 28)
    # Deluxe over a running Premium pauses the 5 days it has left.
    plan = extend_membership(PREMIUM_MEMBERSHIP, at(5), True, at(5), DELUXE_MEMBERSHIP, 3, now=now)
    assert plan[0] == DELUXE_MEMBERSHIP and _days(plan, now) == (3, 8)
    # Premium on a running Premium, and on nothing.
    plan = extend_membership(PREMIUM_MEMBERSHIP, at(5), True, at(5), PREMIUM_MEMBERSHIP, 7, now=now)
    assert plan[0] == PREMIUM_MEMBERSHIP and _days(plan, now) == (12, 12)
    plan = extend_membership(DELUXE_MEMBERSHIP, at(-1), True, "", PREMIUM_MEMBERSHIP, 7, now=now)
    assert plan[0] == PREMIUM_MEMBERSHIP and _days(plan, now) == (7, 7)
    # Deluxe that ended with Premium left: Premium has been running since.
    assert premium_handover(DELUXE_MEMBERSHIP, at(-1), True, at(4), now=now) == (PREMIUM_MEMBERSHIP, at(4), True)
    assert premium_handover(DELUXE_MEMBERSHIP, at(1), True, at(4), now=now) == (DELUXE_MEMBERSHIP, at(1), True)
    assert premium_handover(DELUXE_MEMBERSHIP, at(-2), True, at(-1), now=now) == (DELUXE_MEMBERSHIP, at(-2), True)
    plan = extend_membership(DELUXE_MEMBERSHIP, at(-1), True, at(4), DELUXE_MEMBERSHIP, 2, now=now)
    assert _days(plan, now) == (2, 6)


def test_using_a_deluxe_teaser_consumes_it_and_extends_deluxe():
    case = achievement_fixtures.AchievementServiceTests()
    case.setUp()
    try:
        server, session = case.server, case.session
        session.gameplay_inventory = InventoryInfo(
            items=[ItemInfo(item_id=51, template_id="deluxe_teaser_one_week", amount=1)], storage_slots={51: 0},
        )
        session.gameplay_next_item_id = 52
        before = parse_membership_expiry(session.gameplay_membership_expires_at)
        payload, _ = encode_inventory_item_action(InventoryItemAction(51, 0x13, 0, 0, 0))
        server._handle_inventory_item_action(session, payload)
        assert not any(item.item_id == 51 for item in session.gameplay_inventory.items)
        assert session.gameplay_membership_type == DELUXE_MEMBERSHIP
        assert parse_membership_expiry(session.gameplay_membership_expires_at) - before == timedelta(days=7)
    finally:
        case.doCleanups()


def _use_teaser(template_id, *, membership_type, expires_at, running_effect=None):
    """Use one teaser; return the session and the StatusEffect entries sent."""
    case = achievement_fixtures.AchievementServiceTests()
    case.setUp()
    try:
        return _run_teaser(case, template_id, membership_type, expires_at, running_effect)
    finally:
        case.doCleanups()


def _run_teaser(case, template_id, membership_type, expires_at, running_effect):
    server, session = case.server, case.session
    session.gameplay_inventory = InventoryInfo(
        items=[ItemInfo(item_id=51, template_id=template_id, amount=1)], storage_slots={51: 0},
    )
    session.gameplay_next_item_id = 52
    session.gameplay_presence_ready = True
    session.gameplay_membership_type = membership_type
    session.gameplay_membership_expires_at = expires_at
    session.gameplay_membership_active = True
    if running_effect is not None:
        effect = membership_status_effect(
            DEFAULT_STATIC_DB_PATH, running_effect, instance_id=7, actor_id=1, expires_at=expires_at,
        )
        session.gameplay_persistent_status_effects[7] = effect
    payload, _ = encode_inventory_item_action(InventoryItemAction(51, 0x13, 0, 0, 0))
    server._handle_inventory_item_action(session, payload)
    commands = [command for data, bits, _ in session.recorder.messages if data[0] == 0x85
                for command in decode_server_command_batch(data, bits)]
    entries = [entry for command in commands if command.command_id == STATUS_EFFECT_COMMAND
               for entry in command.value.entries]
    return server, session, commands, entries


def _effect_id(name):
    return load_status_runtime_catalog(DEFAULT_STATIC_DB_PATH).effect_ids[name]


def test_teaser_tiers_and_durations():
    assert teaser_membership("premium_teaser_two_weeks_consumable_7d") == (PREMIUM_MEMBERSHIP, 14)
    assert teaser_membership("deluxe_teaser_three_days_consumable_7d") == (DELUXE_MEMBERSHIP, 3)
    assert teaser_membership("premium_teaser_one_year") == (PREMIUM_MEMBERSHIP, 365)
    assert teaser_membership("dailylogin_lootbox_premium") is None


# FAILURE: the Deluxe buff and its time only changed after re-entering a map.
def test_a_deluxe_teaser_renews_the_running_deluxe_buff_at_once():
    expires_at = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    _server, session, commands, entries = _use_teaser(
        "deluxe_teaser_three_days_consumable_7d", membership_type=DELUXE_MEMBERSHIP,
        expires_at=expires_at, running_effect=DELUXE_MEMBERSHIP,
    )
    assert MEMBERSHIP_UPDATE_COMMAND in [command.command_id for command in commands]
    # Renewed in place: the client refuses a second one-stack Deluxe.
    [entry] = entries
    assert (entry.effect_id, entry.instance_id) == (_effect_id("Deluxe"), 7)
    assert entry.update_timing and not entry.has_details
    assert entry.total_duration == pytest.approx(13 * 86400 * 25, abs=25 * 60)
    assert session.gameplay_persistent_status_effects[7].total_duration == entry.total_duration


def test_a_deluxe_teaser_replaces_a_premium_buff_with_deluxe():
    expires_at = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    _server, session, _commands, entries = _use_teaser(
        "deluxe_teaser_one_day", membership_type=PREMIUM_MEMBERSHIP,
        expires_at=expires_at, running_effect=PREMIUM_MEMBERSHIP,
    )
    added, removed = entries
    assert added.effect_id == _effect_id("Deluxe") and added.has_details and added.instance_id != 7
    assert (removed.effect_id, removed.instance_id) == (_effect_id("Premium"), 7)
    assert not removed.has_details and not removed.update_timing
    assert [effect.effect_id for effect in session.gameplay_persistent_status_effects.values()] == [
        _effect_id("Deluxe")]


def test_a_premium_teaser_grants_premium_to_a_lapsed_account():
    lapsed = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    _server, session, _commands, entries = _use_teaser(
        "premium_teaser_two_weeks_consumable_7d", membership_type=DELUXE_MEMBERSHIP, expires_at=lapsed,
    )
    assert not session.gameplay_inventory.items
    assert session.gameplay_membership_type == PREMIUM_MEMBERSHIP
    remaining = parse_membership_expiry(session.gameplay_membership_expires_at) - datetime.now(timezone.utc)
    assert timedelta(days=13, hours=23) < remaining <= timedelta(days=14)
    [added] = entries
    assert added.effect_id == _effect_id("Premium") and added.has_details


# A Premium plan is paused while Deluxe runs and resumes when Deluxe ends.
def test_a_premium_teaser_under_deluxe_adds_premium_time_after_deluxe():
    deluxe = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    _server, session, commands, _entries = _use_teaser(
        "premium_teaser_two_weeks_consumable_7d", membership_type=DELUXE_MEMBERSHIP,
        expires_at=deluxe, running_effect=DELUXE_MEMBERSHIP,
    )
    assert not session.gameplay_inventory.items
    assert (session.gameplay_membership_type, session.gameplay_membership_expires_at) == (DELUXE_MEMBERSHIP, deluxe)
    premium = parse_membership_expiry(session.gameplay_membership_premium_expires_at)
    assert premium == parse_membership_expiry(deluxe) + timedelta(days=14)
    # Premium first: the client takes the last update as the current plan.
    updates = [command.value for command in commands if command.command_id == MEMBERSHIP_UPDATE_COMMAND]
    assert [(update.membership_type, update.active) for update in updates] == [
        (PREMIUM_MEMBERSHIP, True), (DELUXE_MEMBERSHIP, True)]
    assert (updates[0].expires_at.year, updates[0].expires_at.month, updates[0].expires_at.day) == (
        premium.year, premium.month, premium.day)


# FAILURE: Lydia's Grace ("It can be used at level N") opened to nothing at
# any level: its loot lists are server-side only and missing.
# Each opening levels the box by 10 and pays Polished Dragon Scales; the
# level 100 opening also pays the Statue of Abundance.
@pytest.mark.parametrize("level", range(10, 101, 10))
def test_lydias_grace_pays_scales_and_the_next_box_from_its_level(level):
    box = f"ancestral_books_lv{level}"
    assert load_purchase_item_definition(DEFAULT_STATIC_DB_PATH, box).required_level == level
    with pytest.raises(ValueError, match=f"requires level {level}"):
        roll_chest_contents(DEFAULT_STATIC_DB_PATH, box, player_level=level - 1, class_name="mage")
    contents = roll_chest_contents(DEFAULT_STATIC_DB_PATH, box, player_level=level, class_name="mage")
    following = (
        f"ancestral_books_lv{level + 10}" if level < 100 else "wealth_stone_carving"
    )
    assert [(r.definition.template_id, r.amount) for r in contents.rewards] == [
        ("polished_squama", 1), (following, 1)]


@pytest.mark.parametrize("box,player_level,next_box", [
    ("ancestral_books_lv10", 10, "ancestral_books_lv20"),
    ("ancestral_books_lv90", 95, "ancestral_books_lv100"),
    ("ancestral_books_lv100", 100, "wealth_stone_carving"),
])
def test_using_lydias_grace_grants_its_scales_and_the_next_box(box, player_level, next_box):
    case = achievement_fixtures.AchievementServiceTests()
    case.setUp()
    try:
        server, session = case.server, case.session
        session.gameplay_character.level = player_level
        session.gameplay_inventory = InventoryInfo(
            items=[ItemInfo(item_id=51, template_id=box, amount=1)], storage_slots={51: 0},
        )
        session.gameplay_next_item_id = 52
        session.gameplay_currency_state = CurrencyChanged(500, 0, (0, 0, 0), 0, False)
        payload, _ = encode_inventory_item_action(InventoryItemAction(51, 0x13, 0, 0, 0))
        with patch.object(CharacterServer, "_persist_gameplay_session", return_value=False):
            server._handle_inventory_item_action(session, payload)
        assert session.gameplay_currency_state.andermant == 500
        templates = [item.template_id for item in session.gameplay_inventory.items]
        assert templates == ["polished_squama", next_box]
    finally:
        case.doCleanups()


def test_lydias_grace_below_its_level_is_refused_and_kept():
    case = achievement_fixtures.AchievementServiceTests()
    case.setUp()
    try:
        server, session = case.server, case.session
        session.gameplay_character.level = 19
        session.gameplay_inventory = InventoryInfo(
            items=[ItemInfo(item_id=51, template_id="ancestral_books_lv20", amount=1)], storage_slots={51: 0},
        )
        session.gameplay_next_item_id = 52
        session.gameplay_currency_state = CurrencyChanged(500, 0, (0, 0, 0), 0, False)
        server.system_chat_callback = Mock()
        payload, _ = encode_inventory_item_action(InventoryItemAction(51, 0x13, 0, 0, 0))
        with patch.object(CharacterServer, "_persist_gameplay_session", return_value=False) as persist:
            server._handle_inventory_item_action(session, payload)
        assert session.gameplay_currency_state.andermant == 500
        assert [(i.item_id, i.template_id, i.amount) for i in session.gameplay_inventory.items] == [
            (51, "ancestral_books_lv20", 1)]
        assert not persist.called
        # The client may already have hidden the box: the inventory is resent.
        labels = [label for _, _, label in session.recorder.messages]
        assert any(label.startswith("rejected-chest-inventory-resync") for label in labels)
        # A level refusal is not an unimplemented chest.
        server.system_chat_callback.assert_not_called()
    finally:
        case.doCleanups()


# The user asked that a chest without a loot table tell the player so,
# instead of doing nothing.
@pytest.mark.parametrize("box", [
    "DEBUG_box_mount",  # no mapping at all
    "e_hel_01_setbox_01",  # a tooltip whose items match no template
    # Only its offer bundle is mapped; it "contains lots of test items".
    "misc_surprisepackage_em",
])
def test_a_chest_without_a_loot_table_tells_the_player_and_stays(box):
    case = achievement_fixtures.AchievementServiceTests()
    case.setUp()
    try:
        server, session = case.server, case.session
        session.gameplay_character.level = 100
        session.gameplay_inventory = InventoryInfo(
            items=[ItemInfo(item_id=51, template_id=box, amount=1)], storage_slots={51: 0},
        )
        session.gameplay_next_item_id = 52
        session.gameplay_currency_state = CurrencyChanged(0, 0, (0, 0, 0), 0, False)
        server.system_chat_callback = Mock()
        payload, _ = encode_inventory_item_action(InventoryItemAction(51, 0x13, 0, 0, 0))
        server._handle_inventory_item_action(session, payload)
        server.system_chat_callback.assert_called_once_with(
            session.gameplay_character.character_id, UNRESOLVED_CHEST_MESSAGE,
        )
        assert [(i.item_id, i.template_id, i.amount) for i in session.gameplay_inventory.items] == [
            (51, box, 1)]
    finally:
        case.doCleanups()


def test_only_chests_count_as_unimplemented_chests():
    with pytest.raises(UnresolvedChestError):
        roll_chest_contents(DEFAULT_STATIC_DB_PATH, "DEBUG_box_mount", player_level=1, class_name="warrior")
    # A consumable without contents is refused, but it is no chest.
    with pytest.raises(ValueError) as refused:
        roll_chest_contents(DEFAULT_STATIC_DB_PATH, "buff_crafting_ingredient_doe", player_level=1,
                            class_name="warrior")
    assert not isinstance(refused.value, UnresolvedChestError)


def _rewards(box, *, level=100, class_name="mage", seed=1):
    contents = roll_chest_contents(DEFAULT_STATIC_DB_PATH, box, player_level=level, class_name=class_name,
                                   rng=random.Random(seed))
    return [(r.definition.template_id, r.amount) for r in contents.rewards]


# FAILURE: these boxes had a mapping that could not be used (a lost tooltip
# line, an offer bundle's text, a box named like its skin) and did nothing.
@pytest.mark.parametrize("box,class_name,expected", [
    # "5x Spirit stone | 1x Bag Extension"
    ("thanksgiving_level_10_rewards", "mage",
     [("misc_resurrect_partymember_01", 5), ("misc_bag_extension_no_droplevel_restriction", 1)]),
    # "1x Cloak of Thanks | 100x Lockpick"
    ("thanksgiving_level_40_rewards", "dwarf", [("thanksgiving_cloak_white_2023_dwarf", 1), ("lockpick", 100)]),
    # "1x Cloak of Giving | 100x Key of Prowess"
    ("thanksgiving_level_95_rewards", "ranger",
     [("thanksgiving_cloak_pruple_2023_ranger", 1), ("scaling_dungeon_boss_key_01", 100)]),
    # "5000x Andermant | 1x Jewel chest | 5x Marks of Morti"
    ("thanksgiving_level_100_rewards", "mage",
     [("money_rc", 5000), ("lootbox_level_rallye_jewels_random", 1), ("mortis_key", 5)]),
    # "20x Realm Path to Circus Monstrorum, 5x Key of Defeat, 999x Essences
    # of Destruction, 1x Nefertari Doll's Crown, 5x Spirit Stone"
    ("e_bos_02_starterbox", "warrior",
     [("e_bos_02_circus_teleport_01", 20), ("e_bos_02_chest_key", 5), ("rc_ammunition_epic", 999),
      ("pet_nefertari_part_01", 1), ("misc_resurrect_partymember_01", 5)]),
    # FAILURE: opened with common Essence of Vigor and no realm paths.
    # "20x Realm Path to Circus Monstrorum, 5x Key of Defeat, 999x
    # <RarityEpicStart>Essence of Vigor</>, 5x Spirit Stone, 20x Feather of a
    # Gryphon, 20x Newborn Heart"
    ("e_bos_starterbox", "dwarf",
     [("e_bos_02_circus_teleport_01", 20), ("e_bos_02_chest_key", 5), ("ammunition_damage_epic", 999),
      ("misc_resurrect_partymember_01", 5), ("pet_ingredient_feather_02", 20), ("pet_ingredient_crystal_03", 20)]),
    # "- 40x Pure Viscanium"
    ("e_ide_02_progress_package", "mage", [("event_identify_drop_crystal_piece_02", 40)]),
    # "1x Dragan Event Attire Skin": the back item skin, a banner for dwarves.
    ("dragan_event_attire_skin", "mage", [("skin_all_unique_backitem_attire_dragan_01", 1)]),
    ("dragan_event_attire_skin", "dwarf", [("skin_dwarf_unique_backitem_attire_dragan_01", 1)]),
])
def test_boxes_with_a_broken_mapping_give_their_described_contents(box, class_name, expected):
    assert _rewards(box, class_name=class_name) == expected


@pytest.mark.parametrize("box", sorted(_AUTHORED_CHEST_CONTENTS))
def test_every_written_box_opens_for_every_class_from_its_level(box):
    level = load_purchase_item_definition(DEFAULT_STATIC_DB_PATH, box).required_level
    for class_name in ("warrior", "mage", "ranger", "dwarf"):
        assert _rewards(box, level=level, class_name=class_name)
    if level > 1:
        with pytest.raises(ValueError, match=f"requires level {level}"):
            _rewards(box, level=level - 1)


# "Contains one of the following: 999x Essence of Vigor (Extraordinary),
# 999x Essence of Vigor (Legendary), 5/20/50x Lockpicks,
# 50/100/200/500/1000x Andermant, Jewel Chest, Eternal Amethyst, Veil of
# Dreams, Holy Star Shard"
ANNIVERSARY_CAKE = {
    ("ammunition_damage_rare", 999), ("ammunition_damage_epic", 999), ("lockpick", 5), ("money_rc", 50),
    ("lootbox_level_rallye_jewels_random", 1), ("item_ingredient_eternal_amethyst", 1),
    ("item_ingredient_dream_silk", 1), ("item_ingredient_blessing_star_crystal", 1),
}


@pytest.mark.parametrize("box,listed", [
    ("e_anniversary_cake", ANNIVERSARY_CAKE),
    ("e_chr_cookie", ANNIVERSARY_CAKE),  # the cake's loot list and text
    # The Greed Fiend's six, plus its bonus items (no odds given: one draw).
    ("the_greed_fiends_gratitude", {
        ("ammunition_damage_epic", 999), ("cooking_key", 10), ("lootbox_gems_f", 1), ("e_sar_chest_key", 3),
        ("dragon_blood_box", 1), ("epicmount_bigbang", 1), ("item_ingredient_mercenary_soul_cluster", 10),
        ("item_ingredient_mercenary_heart_of_destruction", 5), ("divine_crusaders_material_selection_chest", 1),
    }),
])
def test_alternative_amounts_give_the_first_and_every_listed_reward_can_drop(box, listed):
    drawn = set()
    for seed in range(400):
        rewards = _rewards(box, level=60, seed=seed)
        assert len(rewards) == 1
        drawn.add(rewards[0])
    assert drawn == listed


# FAILURE: "lucky bag 2024, ancestor's treasure map, crafting chest
# içerikleri yok". Their retail contents sit in the descriptions, which
# chest_contents_descriptions.json could not parse (a corrupted bullet,
# percentages the generator never reads).
LUCKY_BAG_2024 = {
    ("item_lootbox_glitter_clover", 1), ("ingredient_rune_resistance_ice_fire_light", 1),
    ("jewel_buff_lords_magic", 1), ("jewel_boss_gem_drop_common", 1),
    ("jewel_ingredient_find_common", 1), ("premium_teaser_one_month", 6),
    ("lootbox_level_rallye_jewels_random", 1), ("money_rc", 5000),
    ("community_coins", 100), ("community_coins", 50), ("community_coins", 40), ("community_coins", 20),
    ("mortis_key", 2), ("ce_darkforce_key", 2), ("pw_enter_locked_difficulty_key", 40),
}
LUCKY_BAG_2024_COMMON = {
    ("lootbox_level_rallye_jewels_random", 1), ("money_rc", 5000),
    ("community_coins", 100), ("community_coins", 50), ("community_coins", 40), ("community_coins", 20),
    ("mortis_key", 2), ("ce_darkforce_key", 2), ("pw_enter_locked_difficulty_key", 40),
}
LUCKY_BAG_2024_COIN_AMOUNTS = {20, 40, 50, 100}


def test_lucky_bag_2024_draws_one_reward_at_its_retail_odds():
    table = _AUTHORED_WEIGHTED_CHESTS["item_lootbox_lucky_bag_2024"]
    assert sum(chance for _template_id, _amount, chance in table) == pytest.approx(100.0)
    drawn: dict[tuple[str, int], int] = {}
    coins = 0
    coin_amounts = set()
    for seed in range(1000):
        [reward] = _rewards("item_lootbox_lucky_bag_2024", level=60, seed=seed)
        drawn[reward] = drawn.get(reward, 0) + 1
        if reward[0] == "community_coins":
            coins += 1
            coin_amounts.add(reward[1])
    assert set(drawn) <= LUCKY_BAG_2024
    # Community Coin is 66.5% of the draw, a random 100/50/40/20.
    assert 0.60 < coins / 1000 < 0.73
    assert coin_amounts == LUCKY_BAG_2024_COIN_AMOUNTS
    assert LUCKY_BAG_2024_COMMON <= set(drawn)


# "Consumed when used" for the stated currency.
@pytest.mark.parametrize("box,expected", [
    ("perforated_dragon_scale", [("money_vc_gold", 5)]),
    ("perforated_dragon_bone", [("money_vc_gold", 10)]),
    ("perforated_dragon_tooth", [("money_rc", 100)]),
    ("motley_glass_plate", [("community_coins", 10)]),
    ("motley_glass_bowl", [("community_coins", 50)]),
    ("three_hole_bone_flute", [("e_chr_2012_event_currency", 20)]),
    ("five_hole_bone_flute", [("e_chr_2012_event_currency", 100)]),
    ("seven_hole_bone_flute", [("e_chr_2012_event_currency", 500)]),
])
def test_treasure_map_relics_exchange_for_their_described_currency(box, expected):
    assert _rewards(box, level=1) == expected


# "Once open, it gives you a 100% chance for a complete Dragon Stone."
def test_dragon_crystal_box_gives_one_ancestors_soul():
    assert _rewards("dragon_crystal_box", level=1) == [("e_bgh_ancestors_soul", 1)]


# "Use it to get 5 to 15 random Andermants."
def test_polished_dragon_scale_exchanges_for_five_to_fifteen_andermant():
    amounts = set()
    for seed in range(200):
        [reward] = _rewards("polished_squama", level=1, seed=seed)
        assert reward[0] == "money_rc"
        amounts.add(reward[1])
    assert amounts == set(range(5, 16))


# "You have a chance to receive one of the following items" (eight mounts,
# the odds are not stated): one of the eight per opening.
FOOLS_LUCKY_GIFT_MOUNTS = {
    ("mythical_mount_skeleton_dragon_01_3d", 1),
    ("epicmount_ghost_02_1d", 1),
    ("epicmount_ghost_01_1d", 1),
    ("epicmount_wealth_ston_doll_1d", 1),
    ("epicmount_ghost_bone_horse_1d", 1),
    ("epicmount_ghost_bone_monoceros_1d", 1),
    ("epicmount_valentines_day_chair_1d", 1),
    ("epicmount_chaos_dragon_01_1d", 1),
}


def test_fools_lucky_gift_draws_one_of_its_eight_mounts():
    table = _AUTHORED_WEIGHTED_CHESTS["event_fools_lucky_gift"]
    assert sum(chance for _template_id, _amount, chance in table) == pytest.approx(100.0)
    drawn = set()
    for seed in range(400):
        [reward] = _rewards("event_fools_lucky_gift", level=1, seed=seed)
        drawn.add(reward)
    assert drawn == FOOLS_LUCKY_GIFT_MOUNTS


def test_fools_lucky_gift_is_consumed_for_one_mount():
    opened = _open_chest("event_fools_lucky_gift", level=1, seed=11)
    assert "event_fools_lucky_gift" not in opened["templates"]
    assert len(opened["templates"]) == 1
    assert (opened["templates"][0], 1) in FOOLS_LUCKY_GIFT_MOUNTS


IMMORTAL_RELIC_BONUS = {
    (("ancestors_molten_chest", 1),),
    (("season_pass_chest", 1),),
    (("e_cny_boss_essence_04", 1999),),
    (("ce2024_black_dragon_mount", 1),),
    # The package's own offer: 10 Challenge Pass + 10 Blue Dragon Package.
    (("endlesstower_key", 10), ("ce2024_blue_dragon_mount", 10)),
}


def test_immortal_relic_chest_pays_its_fixed_rewards_and_one_bonus():
    drawn = set()
    for seed in range(400):
        rewards = _rewards("immortal_relic_chest", level=1, seed=seed)
        assert rewards[:2] == [("event_fools_lucky_gift", 1), ("item_season_pass_experience", 5)]
        drawn.add(tuple(rewards[2:]))
    assert drawn == IMMORTAL_RELIC_BONUS


ANCESTOR_TREASURE_MAP = {
    ("perforated_dragon_scale", 1), ("perforated_dragon_bone", 1), ("perforated_dragon_tooth", 1),
    ("motley_glass_plate", 1), ("motley_glass_bowl", 1), ("motley_glass_cup", 1),
    ("three_hole_bone_flute", 1), ("five_hole_bone_flute", 1), ("seven_hole_bone_flute", 1),
    ("e_bgh_ancestors_soul", 1), ("ancestral_books_lv10", 1),
    ("horn_of_harvest_lv60", 1), ("golden_goose_Lv60", 1), ("magic_lamp_1_filthy_1", 1),
    ("magic_dragon_egg_fossil", 1), ("dragon_blood_box", 1), ("dragon_crystal_box", 1),
    ("money_rc", 1000), ("money_rc", 2000), ("money_rc", 4000), ("money_rc", 6000),
    ("money_rc", 8000), ("money_rc", 10000),
}


def test_ancestor_treasure_map_rolls_every_listed_reward_independently():
    assert [
        entry for entry in _AUTHORED_ROLL_CHESTS["ancestor_treasure_map"] if entry[2] == 100.0
    ] == [("perforated_dragon_scale", 1, 100.0), ("money_rc", 2000, 100.0)]
    drawn = set()
    for seed in range(600):
        rewards = _rewards("ancestor_treasure_map", level=60, seed=seed)
        assert ("perforated_dragon_scale", 1) in rewards
        assert ("money_rc", 2000) in rewards
        drawn.update(rewards)
    assert drawn <= ANCESTOR_TREASURE_MAP
    assert {
        ("perforated_dragon_scale", 1), ("perforated_dragon_bone", 1), ("perforated_dragon_tooth", 1),
        ("motley_glass_plate", 1), ("three_hole_bone_flute", 1), ("five_hole_bone_flute", 1),
        ("ancestral_books_lv10", 1), ("golden_goose_Lv60", 1), ("dragon_blood_box", 1),
        ("money_rc", 1000), ("money_rc", 4000),
    } <= drawn


@pytest.mark.parametrize("placeholder,player_level,expected", [
    ("@harvest_horn", 5, "horn_of_harvest_lv20"),
    ("@harvest_horn", 59, "horn_of_harvest_lv40"),
    ("@harvest_horn", 100, "horn_of_harvest_lv100"),
    ("@golden_goose", 1, "golden_goose_Lv50"),
    ("@golden_goose", 73, "golden_goose_Lv70"),
    ("@golden_goose", 100, "golden_goose_Lv95"),
])
def test_ancestor_level_scaled_rewards_follow_the_players_level(placeholder, player_level, expected):
    assert _authored_reward_template(
        placeholder, player_level=player_level, rng=random.Random(0),
    ) == expected


def test_ancestor_treasure_map_opens_and_credits_its_guaranteed_andermant():
    case = achievement_fixtures.AchievementServiceTests()
    case.setUp()
    try:
        server, session = case.server, case.session
        session.gameplay_character.level = 60
        session.gameplay_inventory = InventoryInfo(
            items=[ItemInfo(item_id=51, template_id="ancestor_treasure_map", amount=1)],
            storage_slots={51: 0},
        )
        session.gameplay_next_item_id = 52
        session.gameplay_currency_state = CurrencyChanged(0, 0, (0, 0, 0), 0, False)
        payload, _ = encode_inventory_item_action(InventoryItemAction(51, 0x13, 0, 0, 0))
        with patch.object(CharacterServer, "_persist_gameplay_session", return_value=False):
            server._handle_inventory_item_action(session, payload)
        assert session.gameplay_currency_state.andermant >= 2000
        templates = [item.template_id for item in session.gameplay_inventory.items]
        assert "ancestor_treasure_map" not in templates
        assert "perforated_dragon_scale" in templates
    finally:
        case.doCleanups()


def _open_chest(box, *, level=60, seed=None):
    """Open one chest on a fresh account; return the resulting inventory facts."""
    case = achievement_fixtures.AchievementServiceTests()
    case.setUp()
    try:
        server, session = case.server, case.session
        session.gameplay_character.level = level
        session.gameplay_inventory = InventoryInfo(
            items=[ItemInfo(item_id=51, template_id=box, amount=1)], storage_slots={51: 0},
        )
        session.gameplay_next_item_id = 52
        session.gameplay_currency_state = CurrencyChanged(0, 0, (0, 0, 0), 0, False)
        payload, _ = encode_inventory_item_action(InventoryItemAction(51, 0x13, 0, 0, 0))
        patches = [patch.object(CharacterServer, "_persist_gameplay_session", return_value=False)]
        if seed is not None:
            patches.append(patch.object(
                chest_loot_protocol.random, "SystemRandom", lambda: random.Random(seed),
            ))
        for active in patches:
            active.start()
        try:
            server._handle_inventory_item_action(session, payload)
        finally:
            for active in reversed(patches):
                active.stop()
        inventory = session.gameplay_inventory
        by_id = {item.item_id: item.template_id for item in inventory.items}
        return {
            "gold": session.gameplay_currency_state.gold,
            "andermant": session.gameplay_currency_state.andermant,
            "templates": [item.template_id for item in inventory.items],
            "bagged": {by_id[item_id] for item_id in inventory.ingredient_bag_item_ids},
            "stored": [by_id[item_id] for item_id in inventory.storage_slots],
        }
    finally:
        case.doCleanups()


def test_a_dragon_scale_exchange_credits_gold_and_consumes_the_scale():
    opened = _open_chest("perforated_dragon_scale")
    assert opened["gold"] == 5
    assert opened["templates"] == []


def test_a_polished_dragon_scale_is_consumed_for_andermant():
    opened = _open_chest("polished_squama", seed=3)
    assert 5 <= opened["andermant"] <= 15
    assert opened["templates"] == []


def test_dragon_crystal_box_is_consumed_and_its_soul_is_bagged():
    opened = _open_chest("dragon_crystal_box", level=1)
    assert opened["templates"] == ["e_bgh_ancestors_soul"]
    assert opened["bagged"] == {"e_bgh_ancestors_soul"}
    assert not opened["stored"]


def test_chest_rewards_registered_in_the_ingredient_bag_are_stored_there():
    opened = _open_chest("crafting_quest_reward_box", level=10)
    assert opened["bagged"] == set(opened["templates"])
    assert "crafting_stateobject_key" in opened["bagged"]
    assert any(template.startswith("basic_ingredient_") for template in opened["bagged"])
    assert not opened["stored"]


@pytest.mark.parametrize("bagged_seed,template_id", [
    # The 2024 Lucky Bag rewards the client keeps in the ingredient bag.
    (0, "ce_darkforce_key"),
    (6, "mortis_key"),
    (139, "ingredient_rune_resistance_ice_fire_light"),
])
def test_lucky_bag_bag_items_are_delivered_to_the_ingredient_bag(
    bagged_seed, template_id,
):
    assert _rewards(
        "item_lootbox_lucky_bag_2024", level=60, seed=bagged_seed,
    ) == [(template_id, 2 if template_id in {"ce_darkforce_key", "mortis_key"} else 1)]
    opened = _open_chest("item_lootbox_lucky_bag_2024", seed=bagged_seed)
    assert opened["templates"] == [template_id]
    assert opened["bagged"] == {template_id}
    assert not opened["stored"]


def test_the_crafting_chest_holds_a_basic_ingredient_and_a_multitool():
    ingredients = set()
    for seed in range(50):
        rewards = _rewards("crafting_quest_reward_box", level=10, seed=seed)
        assert [amount for _template_id, amount in rewards] == [1, 1]
        assert rewards[1] == ("crafting_stateobject_key", 1)
        assert rewards[0][0].startswith("basic_ingredient_")
        ingredients.add(rewards[0][0])
    assert len(ingredients) > 1


@pytest.mark.parametrize("box", sorted(_selective_items()))
def test_selection_chests_never_roll_and_open_with_the_players_pick(seasonal, box):
    with pytest.raises(ValueError, match="requires a client reward selection") as refused:
        _rewards(box)
    assert not isinstance(refused.value, UnresolvedChestError)
    world = seasonal
    world.session.gameplay_character.level = 100
    option = load_wishing_well_definitions(DEFAULT_STATIC_DB_PATH)[box].options[0]
    before = world.count(option.resource_id)
    world.dispatch(seasonal_proto.encode_select_wishing_well(world.add_item(box), (0,), 1))
    assert world.count(box) == 0
    assert world.count(option.resource_id) == before + option.amount
