"""Tests for christmas_2012 starting and controlling its linked silent events."""

import json
from pathlib import Path
import pytest

from datetime import datetime, timezone
import random
from dso_server.gameplay.event_system import (
    load_event_catalog,
    SCHEDULED_EVENT_FLAG_ALIASES,
    scheduled_event_flag_names,
    christmas_calendar_day_index,
    _christmas_progress_amount,
    christmas_boss_extra_loot,
)
from dso_server.gameplay.ocp_store import (
    WINTER_SOLSTICE_ATTIRE_PRODUCT_KEY,
    TEARS_OF_UNIVERSE_PACKAGE_PRODUCT_KEY,
    WINTER_EVENT_SMALL_BUNDLE_PRODUCT_KEY,
    WINTER_EVENT_BIG_BUNDLE_PRODUCT_KEY,
    MASCOT_PACKAGE_PRODUCT_KEY,
    WINTER_SOLSTICE_CHEST_PRODUCT_KEY,
    WINTER_SOLSTICE_MATERIALS_CHEST_PRODUCT_KEY,
    EVENT_OCP_PRODUCT_EVENT_IDS,
    ocp_product_event_is_active,
)
from dso_server.protocol.reward_protocol import (
    LOCAL_OCP_OFFERS,
    resolve_ocp_booking_reward,
)
from dso_server.gameplay.prop_loot import load_prop_loot_catalog
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.services.admin_events import EventAdministration, LINKED_SILENT_EVENTS
from dso_server.services import events as game_events
from dso_server.tests.test_admin_cms import cms


@pytest.fixture
def christmas_cms(cms, tmp_path, monkeypatch):
    schedule = tmp_path / "events.json"
    schedule.write_text(json.dumps({"events": [
        {
            "event_id": 100,
            "template_id": "christmas_2012",
            "state": "expired",
            "starts_at": "2026-11-16T00:00:00+00:00",
            "ends_at": "2026-11-23T00:00:00+00:00",
        },
        {
            "event_id": 101,
            "template_id": "christmas_2012_silent",
            "state": "expired",
            "starts_at": "2026-11-16T00:00:00+00:00",
            "ends_at": "2026-11-23T00:00:00+00:00",
        },
    ]}), encoding="utf-8")
    monkeypatch.setattr(game_events, "EVENT_SCHEDULE_PATH", schedule)
    cms.service.events = EventAdministration(cms.service, schedule)
    return cms


def test_christmas_aliases_and_daily_present(tmp_path):
    schedule_path = tmp_path / "events.json"
    schedule_path.write_text(json.dumps({"events": [
        {
            "event_id": 1,
            "template_id": "christmas_2012",
            "state": "active",
            "starts_at": "2026-12-01T00:00:00+00:00",
            "ends_at": "2026-12-26T00:00:00+00:00",
        }
    ]}), encoding="utf-8")

    catalog = load_event_catalog(DEFAULT_STATIC_DB_PATH, schedule_path)

    # Test on December 5th
    dec_5 = datetime(2026, 12, 5, 12, 0, 0, tzinfo=timezone.utc)
    active_flags = catalog.active_event_flags(dec_5)

    # Core silent flags must be active
    assert "christmas_2012" in active_flags
    assert "christmas_2012_silent" in active_flags
    assert "e_chr_calendar_map_silent" in active_flags
    assert "event_chr_event_2023_silent" in active_flags

    # On December 5th, exactly e_chr_present_05 should be active, NOT all 25
    assert "e_chr_present_05" in active_flags
    assert "e_chr_present_01" not in active_flags
    assert "e_chr_present_04" not in active_flags
    assert "e_chr_present_06" not in active_flags
    assert "e_chr_present_25" not in active_flags

    # EventUpdate on Dec 5 must contain dedicated entry for e_chr_present_05 so client resolves scene sets
    update = catalog.event_update(dec_5)
    event_names = [e.event_name for e in update.events]
    assert "christmas_2012" in event_names
    assert "e_chr_present_05" in event_names

    chr_entry = next(e for e in update.events if e.event_name == "christmas_2012")
    assert "christmas_2012_silent" in chr_entry.tags
    assert "e_chr_present_05" in chr_entry.tags

    present_entry = next(e for e in update.events if e.event_name == "e_chr_present_05")
    assert present_entry.flags[0] is True  # active


def test_christmas_presents_prop_loot():
    catalog = load_prop_loot_catalog()
    for day in range(1, 26):
        key = f"CHEST_e_chr_calendar_present_{day:02d}"
        assert catalog.has(key), f"Missing prop loot for {key}"
        entries = catalog.entries(key)
        assert entries is not None and len(entries) >= 5
        items = {entry.item_id for entry in entries}
        assert "e_chr_2012_event_currency" in items  # Draken
        assert "e_chr_2012_snow_ammunition" in items  # Snow ammo


def test_admin_activate_christmas_starts_silent_events(christmas_cms):
    cms = christmas_cms
    # Activate existing christmas_2012
    rev = cms.service.events.listing()["revision"]
    cms.service.events.change("admin", "Starting Christmas", {"revision": rev}, event_id=100, enabled=True)

    rows = {r["template_id"]: r for r in cms.service.events.listing()["rows"]}
    assert rows["christmas_2012"]["state"] == "active"
    # christmas_2012_silent should now also be active
    assert rows["christmas_2012_silent"]["state"] == "active"
    # missing linked templates should have been automatically added and active
    assert "e_chr_calendar_map_silent" in rows
    assert rows["e_chr_calendar_map_silent"]["state"] == "active"
    assert "event_chr_event_2023_silent" in rows
    assert rows["event_chr_event_2023_silent"]["state"] == "active"


def test_admin_deactivate_christmas_expires_silent_events(christmas_cms):
    cms = christmas_cms
    # First activate
    rev = cms.service.events.listing()["revision"]
    cms.service.events.change("admin", "Starting Christmas", {"revision": rev}, event_id=100, enabled=True)

    # Now deactivate
    rev = cms.service.events.listing()["revision"]
    cms.service.events.change("admin", "Stopping Christmas", {"revision": rev}, event_id=100, enabled=False)

    rows = {r["template_id"]: r for r in cms.service.events.listing()["rows"]}
    assert rows["christmas_2012"]["state"] == "expired"
    assert rows["christmas_2012_silent"]["state"] == "expired"
    assert rows["e_chr_calendar_map_silent"]["state"] == "expired"
    assert rows["event_chr_event_2023_silent"]["state"] == "expired"


def test_admin_add_christmas_adds_silent_events(cms, tmp_path, monkeypatch):
    schedule = tmp_path / "events.json"
    schedule.write_text(json.dumps({"events": []}), encoding="utf-8")
    monkeypatch.setattr(game_events, "EVENT_SCHEDULE_PATH", schedule)
    cms.service.events = EventAdministration(cms.service, schedule)

    rev = cms.service.events.listing()["revision"]
    cms.service.events.change("admin", "Adding Christmas", {
        "revision": rev,
        "template_id": "christmas_2012",
        "duration_days": 14,
    }, event_id=None, enabled=True)

    rows = {r["template_id"]: r for r in cms.service.events.listing()["rows"]}
    assert "christmas_2012" in rows
    assert rows["christmas_2012"]["state"] == "active"
    assert "christmas_2012_silent" in rows
    assert rows["christmas_2012_silent"]["state"] == "active"
    assert "e_chr_calendar_map_silent" in rows
    assert rows["e_chr_calendar_map_silent"]["state"] == "active"
    assert "event_chr_event_2023_silent" in rows
    assert rows["event_chr_event_2023_silent"]["state"] == "active"

    # Also check the stored document entries
    raw = json.loads(schedule.read_text(encoding="utf-8"))
    raw_events = {e["template_id"]: e for e in raw["events"]}
    assert raw_events["christmas_2012"]["state"] == "scheduled"
    assert raw_events["christmas_2012_silent"]["state"] == "scheduled"
    assert raw_events["e_chr_calendar_map_silent"]["state"] == "scheduled"
    assert raw_events["event_chr_event_2023_silent"]["state"] == "scheduled"
    assert raw_events["christmas_2012_silent"]["ends_at"] == raw_events["christmas_2012"]["ends_at"]


def test_christmas_ocp_offers_configuration():
    christmas_keys = {
        WINTER_SOLSTICE_ATTIRE_PRODUCT_KEY,
        TEARS_OF_UNIVERSE_PACKAGE_PRODUCT_KEY,
        WINTER_EVENT_SMALL_BUNDLE_PRODUCT_KEY,
        WINTER_EVENT_BIG_BUNDLE_PRODUCT_KEY,
        MASCOT_PACKAGE_PRODUCT_KEY,
        WINTER_SOLSTICE_CHEST_PRODUCT_KEY,
        WINTER_SOLSTICE_MATERIALS_CHEST_PRODUCT_KEY,
    }

    # Verify event product routing
    for key in christmas_keys:
        assert EVENT_OCP_PRODUCT_EVENT_IDS[key] == "christmas_2012"
        assert ocp_product_event_is_active(key, ["christmas_2012"])
        assert not ocp_product_event_is_active(key, ["other_event"])
        assert not ocp_product_event_is_active(key, [])

    # Verify local OCP offers list
    offers_by_key = {offer.product_key: offer for offer in LOCAL_OCP_OFFERS}
    for key in christmas_keys:
        assert key in offers_by_key
        offer = offers_by_key[key]
        assert offer.price == 9.90
        assert offer.currency_symbol == "€"


def test_christmas_ocp_booking_rewards():
    # 1. Attire by class (includes matching back skin)
    warrior_reward = resolve_ocp_booking_reward(WINTER_SOLSTICE_ATTIRE_PRODUCT_KEY, "external", class_id=0)
    assert warrior_reward.item_id == "costume_event_pass_xmas_warrior"
    assert warrior_reward.bundle_items == (
        ("costume_event_pass_xmas_warrior", 1),
        ("skin_all_unique_backitem_attire_xmas_01", 1),
    )

    mage_reward = resolve_ocp_booking_reward(WINTER_SOLSTICE_ATTIRE_PRODUCT_KEY, "external", class_id=1)
    assert mage_reward.item_id == "costume_event_pass_xmas_mage"
    assert mage_reward.bundle_items == (
        ("costume_event_pass_xmas_mage", 1),
        ("skin_all_unique_backitem_attire_xmas_01", 1),
    )

    ranger_reward = resolve_ocp_booking_reward(WINTER_SOLSTICE_ATTIRE_PRODUCT_KEY, "external", class_id=2)
    assert ranger_reward.item_id == "costume_event_pass_xmas_ranger"
    assert ranger_reward.bundle_items == (
        ("costume_event_pass_xmas_ranger", 1),
        ("skin_all_unique_backitem_attire_xmas_01", 1),
    )

    dwarf_reward = resolve_ocp_booking_reward(WINTER_SOLSTICE_ATTIRE_PRODUCT_KEY, "external", class_id=3)
    assert dwarf_reward.item_id == "costume_event_pass_xmas_dwarf"
    assert dwarf_reward.bundle_items == (
        ("costume_event_pass_xmas_dwarf", 1),
        ("skin_dwarf_unique_backitem_attire_xmas_01", 1),
    )

    # 2. Tears of the Universe Package
    tears_reward = resolve_ocp_booking_reward(TEARS_OF_UNIVERSE_PACKAGE_PRODUCT_KEY, "external")
    assert tears_reward.bundle_items == (
        ("tears_of_the_star", 2),
        ("tears_of_the_moon", 2),
        ("tears_of_the_sun", 2),
        ("e_chr_2012_snow_ammunition", 1999),
        ("money_rc", 2000),
    )

    # 3. Small Bundle
    small_reward = resolve_ocp_booking_reward(WINTER_EVENT_SMALL_BUNDLE_PRODUCT_KEY, "external")
    assert small_reward.bundle_items == (
        ("event_chr_craftingcake_honeymilk", 100),
        ("event_chr_craftingcake_gingerpowder", 100),
        ("event_chr_craftingcake_granulatedsugar", 100),
        ("event_chr_craftingcake_sweetcream", 20),
    )

    # 4. Big Bundle
    big_reward = resolve_ocp_booking_reward(WINTER_EVENT_BIG_BUNDLE_PRODUCT_KEY, "external")
    assert big_reward.bundle_items == (
        ("event_chr_craftingcake_honeymilk", 1000),
        ("event_chr_craftingcake_gingerpowder", 1000),
        ("event_chr_craftingcake_granulatedsugar", 1000),
        ("event_chr_craftingcake_sweetcream", 200),
    )

    # 5. Mascot Package
    mascot_reward = resolve_ocp_booking_reward(MASCOT_PACKAGE_PRODUCT_KEY, "external")
    assert mascot_reward.bundle_items == (
        ("pet_bobr_05", 1),
        ("premium_equipment_drop_buff", 5),
        ("e_sar_chest_key", 20),
        ("difficulty_key_common", 30),
        ("e_chr_frost_coins", 100),
        ("e_chr_2012_snow_ammunition", 9999),
    )

    # 6. Winter Solstice Chest
    chest_reward = resolve_ocp_booking_reward(WINTER_SOLSTICE_CHEST_PRODUCT_KEY, "external")
    assert chest_reward.bundle_items == (
        ("frost_age_material_selection_chest", 5),
        ("e_chr_2012_snow_ammunition", 999),
        ("e_chr_frost_coins", 10),
        ("e_b1205_dun_chr_2025_01_travelitem", 1),
    )

    # 7. Winter Solstice Materials Chest
    mat_reward = resolve_ocp_booking_reward(WINTER_SOLSTICE_MATERIALS_CHEST_PRODUCT_KEY, "external")
    assert mat_reward.bundle_items == (
        ("frost_age_material_selection_chest", 50),
        ("item_heart_of_winter", 650),
        ("item_zero_degree_ice_crystal", 2000),
    )


def test_christmas_progress_drop_amounts():
    expected = [
        # (diff_key, boss, elite, piglet)
        (0, 100, 5, 5),   # Normal
        (1, 125, 6, 6),   # Painful
        (2, 150, 8, 8),   # Excruciating
        (3, 175, 9, 9),   # Fatal
        (4, 300, 15, 15), # Infernal
        (5, 300, 15, 15), # Infernal alias
        (6, 400, 20, 20), # Merciless
        (7, 400, 20, 20), # Merciless alias
        (8, 550, 28, 28), # Bloodshed
        (9, 550, 28, 28), # Bloodshed alias
    ]

    for diff, boss_amt, elite_amt, piglet_amt in expected:
        # Boss
        assert _christmas_progress_amount(
            item_id="e_chr_progress_item",
            monster_rank="boss",
            map_difficulty=diff,
        ) == boss_amt

        assert _christmas_progress_amount(
            item_id="e_chr_progress_item",
            monster_rank="worldboss",
            map_difficulty=diff,
        ) == boss_amt

        # Elite
        for rank in ("sentinel", "champion", "leader", "elite"):
            assert _christmas_progress_amount(
                item_id="e_chr_progress_item",
                monster_rank=rank,
                map_difficulty=diff,
            ) == elite_amt

        # Piglets (monster template containing pig)
        assert _christmas_progress_amount(
            item_id="e_chr_progress_item",
            monster_rank="normal",
            map_difficulty=diff,
            monster_template_id="e_chr_event_piglet",
        ) == piglet_amt


def test_christmas_progress_collectible_drops(tmp_path):
    schedule_path = tmp_path / "events.json"
    schedule_path.write_text(json.dumps({"events": [
        {
            "event_id": 1,
            "template_id": "christmas_2012",
            "state": "active",
            "collectible_drop_chance": 1.0,
        }
    ]}), encoding="utf-8")

    catalog = load_event_catalog(DEFAULT_STATIC_DB_PATH, schedule_path)
    map_id = "e_b1205_dun_chr_2012_02"

    # Bloodshed (diff 8) Boss without attire
    drops = catalog.collectible_drops(
        map_id=map_id,
        player_level=100,
        monster_rank="boss",
        participation={},
        status_effects=(),
        rng=random.Random(1),
        map_difficulty=8,
    )
    assert drops == (("e_chr_progress_item", 550),)

    # Bloodshed (diff 8) Boss WITH attire (+50% -> 550 * 1.5 = 825)
    drops_attire = catalog.collectible_drops(
        map_id=map_id,
        player_level=100,
        monster_rank="boss",
        participation={},
        status_effects=(),
        rng=random.Random(1),
        map_difficulty=8,
        active_costume="costume_event_pass_xmas_mage",
    )
    assert drops_attire == (("e_chr_progress_item", 825),)

    # Bloodshed (diff 8) Elite WITH attire (+50% -> 28 * 1.5 = 42)
    drops_elite_attire = catalog.collectible_drops(
        map_id=map_id,
        player_level=100,
        monster_rank="leader",
        participation={},
        status_effects=(),
        rng=random.Random(1),
        map_difficulty=8,
        active_costume="costume_event_pass_xmas_mage",
    )
    assert drops_elite_attire == (("e_chr_progress_item", 42),)


def test_christmas_big_paws_extra_drops():
    # Big Paws on Winter Summit
    map_id = "e_b1205_dun_chr_2012_02"
    template = "e_chr_boss_big_paws"

    expected_shards = {
        0: (1, 2),   # Normal
        1: (1, 2),   # Painful
        2: (2, 4),   # Excruciating
        3: (3, 5),   # Fatal
        4: (4, 6),   # Infernal
        6: (8, 10),  # Merciless
        8: (9, 11),  # Bloodshed
    }

    for diff, (shard_min, shard_max) in expected_shards.items():
        loot = christmas_boss_extra_loot(
            map_id=map_id,
            monster_template_id=template,
            map_difficulty=diff,
            rng=random.Random(42),
        )
        loot_dict = dict(loot)
        assert "item_zero_degree_ice_crystal" in loot_dict
        assert shard_min <= loot_dict["item_zero_degree_ice_crystal"] <= shard_max

        # Heart of Winter check
        if diff in (6, 8):
            assert loot_dict.get("item_heart_of_winter") == 1
        else:
            assert "item_heart_of_winter" not in loot_dict


def test_christmas_kranparus_extra_drops():
    challenges = [
        ("e_chr_challenge_dun_01", "e_chr_2015_boss_krampus_challenge_01", "item_ingredient_frost_age_torso"),
        ("e_chr_challenge_dun_02", "e_chr_2015_boss_krampus_challenge_02", "item_ingredient_frost_age_gloves"),
        ("e_chr_challenge_dun_03", "e_chr_2015_boss_krampus_challenge_03", "item_ingredient_frost_age_boots"),
        ("e_b1205_dun_chr_2024", "e_chr_2024_boss_krampus_challenge_01", None),
    ]

    expected_shards = {
        0: (2, 6),    # Normal
        1: (5, 10),   # Painful
        2: (10, 15),  # Excruciating
        3: (15, 20),  # Fatal
        4: (15, 25),  # Infernal
        6: (35, 45),  # Merciless
        8: (45, 55),  # Bloodshed
    }

    for map_id, template, specific_mat in challenges:
        for diff, (shard_min, shard_max) in expected_shards.items():
            loot = christmas_boss_extra_loot(
                map_id=map_id,
                monster_template_id=template,
                map_difficulty=diff,
                rng=random.Random(42),
            )
            loot_dict = dict(loot)
            assert "item_zero_degree_ice_crystal" in loot_dict
            assert shard_min <= loot_dict["item_zero_degree_ice_crystal"] <= shard_max

            # Heart of Winter
            if diff == 6:
                assert 3 <= loot_dict.get("item_heart_of_winter", 0) <= 4
            elif diff == 8:
                assert 4 <= loot_dict.get("item_heart_of_winter", 0) <= 5
            else:
                assert "item_heart_of_winter" not in loot_dict

        # Frost Age Material drop on Merciless/Bloodshed when rng roll succeeds
        class FixedRNG:
            def randint(self, a, b): return a
            def random(self): return 0.01  # < 0.03 -> trigger drop
            def choice(self, seq): return seq[0]

        loot = christmas_boss_extra_loot(
            map_id=map_id,
            monster_template_id=template,
            map_difficulty=8,
            rng=FixedRNG(),
        )
        loot_dict = dict(loot)
        if specific_mat:
            assert loot_dict.get(specific_mat) == 1
        else:
            # Frozen field drops one of the 3 materials
            assert any(mat in loot_dict for mat in (
                "item_ingredient_frost_age_torso",
                "item_ingredient_frost_age_gloves",
                "item_ingredient_frost_age_boots",
            ))


def test_christmas_big_momma_extra_drops():
    map_id = "e_b1205_dun_chr_2025_01"
    template = "e_chr_boss_big_paws_mom"

    expected_hearts = {
        0: (1, 1),
        1: (1, 1),
        2: (1, 2),
        3: (1, 2),
        4: (2, 3),
        6: (5, 7),
        8: (7, 9),
    }

    for diff, (h_min, h_max) in expected_hearts.items():
        loot = christmas_boss_extra_loot(
            map_id=map_id,
            monster_template_id=template,
            map_difficulty=diff,
            rng=random.Random(42),
        )
        loot_dict = dict(loot)
        assert "item_heart_of_winter" in loot_dict
        assert h_min <= loot_dict["item_heart_of_winter"] <= h_max

    # Big Momma on Normal/Merciless with drop chance roll succeeding (3%)
    class TriggerRNG:
        def randint(self, a, b): return a
        def random(self): return 0.01  # < 0.03
        def choice(self, seq): return seq[0]

    loot_norm = christmas_boss_extra_loot(
        map_id=map_id,
        monster_template_id=template,
        map_difficulty=0,
        rng=TriggerRNG(),
    )
    loot_norm_dict = dict(loot_norm)
    assert any(m in loot_norm_dict for m in (
        "item_ingredient_frost_age_torso",
        "item_ingredient_frost_age_gloves",
        "item_ingredient_frost_age_boots",
    ))

    # Big Momma on Bloodshed with high chance rolling all 3 ingredients + Winter's Guardian Amulet
    class BloodshedLuckyRNG:
        def randint(self, a, b): return a
        def random(self): return 0.01  # < 0.20 (all 3 mats) and < 0.02 (amulet)
        def choice(self, seq): return seq[0]

    for class_name, expected_amulet in [
        ("warrior", "set_winters_guardian_amulet_warrior"),
        ("mage", "set_winters_guardian_amulet_mage"),
        ("ranger", "set_winters_guardian_amulet_ranger"),
        ("dwarf", "set_winters_guardian_amulet_dwarf"),
    ]:
        loot_blood = christmas_boss_extra_loot(
            map_id=map_id,
            monster_template_id=template,
            map_difficulty=8,
            class_name=class_name,
            rng=BloodshedLuckyRNG(),
        )
        blood_dict = dict(loot_blood)
        # All 3 materials dropped
        assert blood_dict.get("item_ingredient_frost_age_torso") == 1
        assert blood_dict.get("item_ingredient_frost_age_gloves") == 1
        assert blood_dict.get("item_ingredient_frost_age_boots") == 1
        # Class amulet dropped
        assert blood_dict.get(expected_amulet) == 1


def test_christmas_calendar_present_claim_key_and_viewer_state():
    from unittest.mock import MagicMock
    from dso_server.services.map_events import (
        _christmas_calendar_present_claim_key,
        _prop_state_for_viewer,
        PropRuntimeState,
        PROP_STATE_ACTIVE,
        PROP_STATE_USED,
    )
    from dso_server.services.admin_events import _clear_christmas_calendar_present_claims

    # Mock session and runtime on e_chr_calendar_dun_01
    session = MagicMock()
    runtime = MagicMock()
    runtime.map_id = "e_chr_calendar_dun_01"
    session.gameplay_map_event_runtime = runtime
    session.gameplay_map = "e_chr_calendar_dun_01"

    # Event catalog with active christmas_2012 event id 100
    mock_event = MagicMock()
    mock_event.event_id = 100
    mock_event.template.resource_id = "christmas_2012"
    catalog = MagicMock()
    catalog.active_events.return_value = [mock_event]
    session.gameplay_event_catalog = catalog

    # Mock prop definition
    definition = MagicMock()
    definition.instance_id = "e_chr_calendar_present_05"
    definition.template_id = "e_chr_calendar_present_05"
    definition.prop_id = 555
    definition.individual_usage = True

    claim_key = _christmas_calendar_present_claim_key(session, definition)
    assert claim_key == "christmas-calendar-present:100:e_chr_calendar_present_05"

    # Not claimed yet
    session.gameplay_timed_offer_purchases = {}
    active_state = PropRuntimeState(definition, PROP_STATE_ACTIVE)
    assert _prop_state_for_viewer(session, active_state).state == PROP_STATE_ACTIVE

    # Claimed in timed_offer_purchases
    session.gameplay_timed_offer_purchases = {claim_key: 1}
    assert _prop_state_for_viewer(session, active_state).state == PROP_STATE_USED

    # Clearing claims on event reset
    claims = {
        claim_key: 1,
        "christmas-calendar-present:100:e_chr_calendar_present_01": 1,
        "other_unrelated_key": 42,
    }
    changed = _clear_christmas_calendar_present_claims(claims, 100)
    assert changed is True
    assert claim_key not in claims
    assert "christmas-calendar-present:100:e_chr_calendar_present_01" not in claims
    assert claims["other_unrelated_key"] == 42


def test_settle_ocp_booking_with_bundle_containing_money_rc():
    """Verify that OCP bundles containing currency like money_rc (Andermant) settle without ValueError."""
    from dso_server.protocol.reward_protocol import resolve_ocp_booking_reward
    from dso_server.services.commerce import CommerceService
    from dso_server.protocol.player_init_protocol import CurrencyChanged
    from unittest.mock import MagicMock
    import copy

    from dso_server.gameplay.ocp_store import TEARS_OF_UNIVERSE_PACKAGE_PRODUCT_KEY
    reward = resolve_ocp_booking_reward(TEARS_OF_UNIVERSE_PACKAGE_PRODUCT_KEY, "external")  # Tears of the Universe Package
    assert reward is not None
    assert any(item == "money_rc" for item, _ in reward.bundle_items)

    service = CommerceService()
    service.static_database_path = DEFAULT_STATIC_DB_PATH

    session = MagicMock()
    session.account_id = 1
    session.gameplay_currency_state = CurrencyChanged(
        currency_type=1000,
        amount=500,
        values=(0, 0, 0),
        delta=0,
        notify=False,
    )
    session.gameplay_next_item_id = 100
    mock_char = MagicMock()
    mock_char.character_id = 123
    mock_char.level = 50
    session.gameplay_character = mock_char
    from unittest.mock import MagicMock, patch
    from dso_server.services.base import CharacterService
    from dso_server.protocol.reward_protocol import ocp_booking_pending_key
    pending_key = ocp_booking_pending_key(b"\x01" * 16, TEARS_OF_UNIVERSE_PACKAGE_PRODUCT_KEY)
    session.gameplay_timed_offer_purchases = {pending_key: 1}
    session.gameplay_event_progress_callback = MagicMock()

    mock_inventory = MagicMock()
    mock_inventory.sections = {}
    session.gameplay_inventory = mock_inventory

    # Bind a dummy implementation to CharacterService for test
    CharacterService._implementation = MagicMock()

    # Should not raise ValueError: unsupported wallet currency 'money_rc'
    with session.flow_lock:
        service._settle_ocp_booking(session, pending_key, reward)

    assert session.gameplay_currency_state.andermant == 1000 + 2000
    session.gameplay_event_progress_callback.assert_called_with(session, "money_rc", 2000)

    # 1000 initial + 2000 from package = 3000
    assert session.gameplay_currency_state.currency_type == 3000
    # Callback called with 2000 Andermant
    session.gameplay_event_progress_callback.assert_called_with(session, "money_rc", 2000)


def test_winter_summit_mob_drops_and_boss_drops():
    """Verify that normal mobs on Winter Summit do not drop boss uniques, and Big Paws drops them."""
    from dso_server.protocol.world_loot_protocol import generate_monster_loot

    # Normal mob on Winter Summit e_b1205_dun_chr_2012_02
    mob_gen = generate_monster_loot(
        DEFAULT_STATIC_DB_PATH,
        template_id="e_chr_b1205_normal_troll_01",
        class_name="mage",
        player_level=100,
        position=(0.0, 0.0, 0.0),
        next_item_id=1,
        map_difficulty=8,
        map_access_id="e_b1205_dun_chr_2012_02",
        force_drops=True,
    )
    dropped_templates = [drop.item.item_id for drop in mob_gen.drops]

    # Normal mob must NOT drop Frosty Scepter, Greed's End, Moosov mount, or Winter's Guardian Amulet
    assert "mage_unique_2h_staff_christmas_bear_set" not in dropped_templates
    assert "mage_unique_backitem_christmas_bear_set" not in dropped_templates
    assert "epicmount_moose_jullov_01" not in dropped_templates
    assert "set_winters_guardian_amulet_mage" not in dropped_templates

    # Boss Big Paws drops
    class LuckyBossRNG:
        def randint(self, a, b): return a
        def random(self): return 0.05  # < 0.25 (weapon), < 0.25 (backitem), < 0.10 (recipe)
        def choice(self, seq): return seq[0]

    boss_loot = christmas_boss_extra_loot(
        map_id="e_b1205_dun_chr_2012_02",
        monster_template_id="e_chr_boss_big_paws",
        map_difficulty=8,
        class_name="mage",
        rng=LuckyBossRNG(),
    )
    boss_loot_dict = dict(boss_loot)
    # Boss drops Frosty Scepter (Big Paws' Frosty Scepter), Greed's End, and Frostpocalypse Recipe
    assert boss_loot_dict.get("mage_unique_2h_staff_christmas_bear_set") == 1
    assert boss_loot_dict.get("mage_unique_backitem_christmas_bear_set") == 1
    assert boss_loot_dict.get("recipe_cataclysmic_winter") == 1


def test_winter_summit_density_normal_monsters_and_ranks():
    """Verify Winter Summit density areas spawn normal quest monsters, not 100% leaders/champions."""
    from dso_server.gameplay.local_monster_state import (
        _load_map_monster_templates,
        load_local_monster_encounter,
    )
    from collections import Counter

    templates = _load_map_monster_templates(
        DEFAULT_STATIC_DB_PATH, "e_b1205_dun_chr_2012_02"
    )
    # Must include the 6 core normal monsters, specifically Cookie Nibblers & Gremlin Cookie Noshers
    assert "e_chr_b1205_normal_beast_hound_01" in templates  # Cookie Nibbler
    assert "e_chr_b1205_normal_meanie_rascal_01" in templates  # Gremlin Cookie Nosher
    assert "e_chr_b1205_normal_norsegnome_01" in templates
    assert "e_chr_b1205_normal_troll_01" in templates

    encounter = load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH, "e_b1205_dun_chr_2012_02"
    )
    assert encounter is not None

    spawned_templates = Counter()
    spawned_ranks = Counter()
    for cmd in encounter.spawn_batch.commands:
        v = cmd.value
        if hasattr(v, "template_id") and hasattr(v, "rank"):
            spawned_templates[v.template_id] += 1
            spawned_ranks[v.rank] += 1

    # Normal rank should be the vast majority (100+), NOT dominated by leaders/champions
    assert spawned_ranks["Rank_monster"] >= 100
    assert spawned_ranks["Rank_leader"] <= 10
    assert spawned_ranks["Rank_champion"] <= 10

    # Cookie Nibblers and Gremlin Cookie Noshers must exist on the map
    assert spawned_templates["e_chr_b1205_normal_beast_hound_01"] > 10
    assert spawned_templates["e_chr_b1205_normal_meanie_rascal_01"] > 10


def test_winter_summit_yellow_snow_defense_and_kill_quests():
    from types import SimpleNamespace
    from dso_server.paths import DEFAULT_QUESTS_PATH, DEFAULT_STATIC_DB_PATH
    from dso_server.gameplay.quest_content import (
        load_quest_catalog, QuestProgress, QUEST_STATE_ACCEPTED, apply_quest_event,
    )
    from dso_server.services.quest_holdgrounds import holdground_hosted_here, resolve_holdground

    catalog = load_quest_catalog(DEFAULT_QUESTS_PATH)

    # 1. YellowSnowIsNoGo defense quest: holdground_hosted_here must be True on e_b1205_dun_chr_2012_02
    q_snow = catalog.quest("e-b2105-YellowSnowIsNoGo-01-n")
    assert q_snow is not None
    task_snow = q_snow.tasks[0]
    binding_snow = task_snow.holdgrounds[0]

    server = SimpleNamespace(static_database_path=DEFAULT_STATIC_DB_PATH)
    session = SimpleNamespace(gameplay_map="e_b1205_dun_chr_2012_02", gameplay_scaling_difficulty=0)

    assert holdground_hosted_here(server, session, task_snow, binding_snow) is True
    plan = resolve_holdground(server, session, binding_snow)
    assert plan is not None
    assert plan.defender_template_id == "e_chr_2013_guard_jullov"
    assert len(plan.waves) == 9

    # 2. Ice Princess quest: Cookie Nibbler kill produces Flawless Teeth drop
    q_ice = catalog.quest("e-b2105-chr-JullovIceSkate-01-n")
    assert q_ice is not None
    p_ice = QuestProgress("e-b2105-chr-JullovIceSkate-01-n", state=QUEST_STATE_ACCEPTED)
    res_ice = apply_quest_event(q_ice, p_ice, "kill", "e_chr_b1205_normal_beast_hound_01")
    assert any(d.item_id == "e_chr_2013_julloviceskate_01_teeth" for d in res_ice.drops)

    # 3. Kill quest: Cookie Nibbler and Gremlin Cookie Nosher advance kills
    q_kill = catalog.quest("e-a0200-chr-2012-monster-kill-01")
    assert q_kill is not None
    p_kill = QuestProgress("e-a0200-chr-2012-monster-kill-01", state=QUEST_STATE_ACCEPTED)
    apply_quest_event(q_kill, p_kill, "kill", "e_chr_b1205_normal_beast_hound_01")
    assert p_kill.tokens.get("chr-2012-monster-kill-01-KillHoundKill") == 1
    apply_quest_event(q_kill, p_kill, "kill", "e_chr_b1205_normal_meanie_rascal_01")
    assert p_kill.tokens.get("chr-2012-monster-kill-01-KillMeanieKill") == 1


def test_christmas_challenge_dungeons():
    from collections import Counter
    from gameplay.local_monster_state import load_local_monster_encounter
    from gameplay.map_event_state import load_map_event_content
    from gameplay.prop_loot import has_prop_loot, load_prop_loot_catalog, roll_prop_loot

    db_path = DEFAULT_STATIC_DB_PATH

    for m in ["e_chr_challenge_dun_01", "e_chr_challenge_dun_02", "e_chr_challenge_dun_03"]:
        enc = load_local_monster_encounter(db_path, m)
        assert enc is not None
        assert len(enc.actor_ids) == 162

        # Check monster distributions: must have >130 normal monsters, 1 leader, 1 boss
        monsters = []
        for g in enc.spawn_groups:
            for cmd in g.spawn_batch.commands:
                if hasattr(cmd, "value") and hasattr(cmd.value, "template_id"):
                    monsters.append((cmd.value.template_id, cmd.value.rank))

        ranks = Counter(rank for _, rank in monsters)
        assert ranks["Rank_monster"] >= 130
        assert ranks["Rank_leader"] == 1  # Only the single cursed amphora minion
        assert ranks["Rank_boss"] == 1    # Krampus boss

        # Props check
        content = load_map_event_content(db_path, m)
        cages = [p for p in content.props if p.template_id == "e_chr_child_cage"]
        save_spots = [p for p in content.props if p.template_id == "e_chr_save_spot"]
        assert len(cages) > 0
        assert len(save_spots) > 0
        assert all(sp.spends_keys for sp in save_spots)

    # Child cage loot
    assert has_prop_loot("CHEST_e_chr_child_cage") is True
    cat = load_prop_loot_catalog()
    entries = cat.entries("CHEST_e_chr_child_cage")
    drops = dict(roll_prop_loot(entries, player_level=100, rng=random.Random(42), event_collectibles={}))
    assert "e_chr_challenge_progress_01" in drops
    assert drops["e_chr_challenge_progress_01"] >= 60


def test_triangle_of_forces_ocp_package():
    from gameplay.ocp_store import (
        TRIANGLE_OF_FORCES_PACKAGE_PRODUCT_KEY,
        EVENT_OCP_PRODUCT_EVENT_IDS,
    )
    from protocol.reward_protocol import LOCAL_OCP_OFFERS, resolve_ocp_booking_reward

    assert EVENT_OCP_PRODUCT_EVENT_IDS[TRIANGLE_OF_FORCES_PACKAGE_PRODUCT_KEY] == "christmas_2012"

    offer = next(
        (o for o in LOCAL_OCP_OFFERS if o.product_key == TRIANGLE_OF_FORCES_PACKAGE_PRODUCT_KEY),
        None,
    )
    assert offer is not None
    assert offer.price == 9.90
    assert offer.currency_symbol == "€"

    reward = resolve_ocp_booking_reward(TRIANGLE_OF_FORCES_PACKAGE_PRODUCT_KEY, "external")
    assert reward is not None
    items = dict(reward.bundle_items)
    assert items["triangles_of_strength"] == 3
    assert items["triangles_of_wisdom"] == 3
    assert items["triangles_of_courage"] == 3
    assert items["e_sar_chest_key"] == 5


def test_winter_event_chest_collect_loot_and_respawn():
    from gameplay.prop_loot import load_prop_loot_catalog, roll_prop_loot
    from gameplay.map_event_state import MapPropDefinition, PropRuntimeState
    from protocol.prop_protocol import PROP_STATE_ACTIVE, PROP_STATE_USED

    cat = load_prop_loot_catalog()
    entries = cat.entries("e_chr_2023_event_chest_collect")
    assert entries is not None
    assert len(entries) == 4

    # 100 random rolls: each must roll exactly one of the 4 ingredients
    allowed = {
        ("event_chr_craftingcake_honeymilk", 5),
        ("event_chr_craftingcake_gingerpowder", 5),
        ("event_chr_craftingcake_granulatedsugar", 5),
        ("event_chr_craftingcake_sweetcream", 1),
    }
    seen = set()
    for seed in range(100):
        drops = roll_prop_loot(
            entries,
            player_level=100,
            event_collectibles=(),
            rng=random.Random(seed),
        )
        assert len(drops) == 1
        item, amount = drops[0]
        assert (item, amount) in allowed
        seen.add((item, amount))

    # Over 100 rolls, all 4 must have been seen
    assert seen == allowed


def test_festive_winter_solstice_present_loot_and_single_claim():
    from gameplay.prop_loot import load_prop_loot_catalog, roll_prop_loot
    from gameplay.map_event_state import MapPropDefinition, PropRuntimeState
    from protocol.prop_protocol import PROP_STATE_ACTIVE, PROP_STATE_USED
    from services.map_events import (
        _festive_winter_solstice_present_claim_key,
        _prop_state_for_viewer,
    )
    import threading

    cat = load_prop_loot_catalog()
    entries = cat.entries("e_chr_big_present_01")
    assert entries is not None
    assert len(entries) == 20

    # Also verify alias CHEST_e_chr_big_present_01 resolves to the same entries
    alias_entries = cat.entries("CHEST_e_chr_big_present_01")
    assert alias_entries == entries

    # Roll loot and verify all 13 requested rewards are present
    drops = roll_prop_loot(
        entries,
        player_level=100,
        event_collectibles=(),
        rng=random.Random(42),
    )
    drop_dict = {item: amount for item, amount in drops}

    # 1. 1 Rune of Enhancement
    assert drop_dict["unique_gem_crafting"] == 1
    # 2. 1 Gem of Enhancement
    assert drop_dict["e_ide_02_crafting_gem"] == 1
    # 3. 10k Drakens
    assert drop_dict["e_chr_2012_event_currency"] == 10000
    # 4. 1 Jewel Chest
    assert drop_dict["lootbox_level_rallye_jewels_random"] == 1
    # 5. 1 Dragon Blood Box
    assert drop_dict["dragon_blood_box"] == 1
    # 6. 50k Anders
    assert drop_dict["money_rc"] == 50000
    # 7. 100k Gold
    assert drop_dict["money_gc_no_auto_use"] == 100000
    # 8. 1 Golden Goose (level scaled)
    assert any(item.startswith("golden_goose_Lv") for item in drop_dict)
    # 9. 5 Trapezoid and 3 Imperial gems
    trapezoid_gems = ["gem_ruby_j", "gem_onyx_j", "gem_cyanit_j", "gem_diamond_j", "gem_amethyst_j"]
    for gem in trapezoid_gems:
        assert drop_dict[gem] == 1
    imperial_gems = ["gem_ruby_n", "gem_onyx_n", "gem_diamond_n"]
    for gem in imperial_gems:
        assert drop_dict[gem] == 1
    # 10. 1 Reindeer mount
    assert drop_dict["e_chr_2012_unique_mount"] == 1
    # 11. Scroll of Ice Resistance
    assert drop_dict["e_chr_ingredient_present"] == 1
    # 12. 1 Rune of Fortitude (magic)
    assert drop_dict["unique_gem_armor_01"] == 1
    # 13. 1 Rune of Vigor (magic)
    assert drop_dict["unique_gem_damage_01"] == 1

    # Verify claim key and single-claim enforcement
    import types
    prop_def = types.SimpleNamespace(
        prop_id=1234,
        instance_id="e_chr_big_present_01",
        template_id="e_chr_big_present_01",
        loot_list_id="e_chr_big_present_01",
        key_id="e_chr_big_present_coin_key_01",
        key_amount=2500,
        individual_usage=True,
    )

    class DummySession:
        def __init__(self):
            self.flow_lock = threading.Lock()
            self.gameplay_timed_offer_purchases = {}
            self.gameplay_event_catalog = None
            self.gameplay_map_event_runtime = None

    session = DummySession()
    claim_key = _festive_winter_solstice_present_claim_key(session, prop_def)
    assert claim_key == "festive-winter-solstice-present:0"

    # Before claiming: prop state is active
    state = PropRuntimeState(prop_def, PROP_STATE_ACTIVE)
    view_state = _prop_state_for_viewer(session, state)
    assert view_state.state == PROP_STATE_ACTIVE

    # After claiming: prop state becomes PROP_STATE_USED
    session.gameplay_timed_offer_purchases[claim_key] = 1
    view_state_claimed = _prop_state_for_viewer(session, state)
    assert view_state_claimed.state == PROP_STATE_USED
