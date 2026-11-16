"""Tests for Worldboss guaranteed drops and custom mob loot scaling."""

import random
from collections import Counter
import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.world_loot_protocol import (
    ALL_MOBS_REALM_FRAGMENT_CHANCE,
    ALL_MOBS_WISDOM_CHANCE,
    REALM_FRAGMENT_ITEM_TEMPLATE,
    SPECIAL_MOB_DROP_CHANCE,
    SPECIAL_MOB_RANKS,
    SPECIAL_MOB_REALM_FRAGMENTS_BY_DIFFICULTY,
    SPECIAL_MOB_WISDOM_BY_DIFFICULTY,
    WISDOM_ITEM_TEMPLATE,
    WORLDBOSS_BONUS_WISDOM_BY_DIFFICULTY,
    WORLDBOSS_BONUS_WISDOM_CHANCE,
    WORLDBOSS_GUARANTEED_WISDOM_BY_DIFFICULTY,
    WORLDBOSS_REALM_FRAGMENT_RANGE_BY_DIFFICULTY,
    generate_monster_loot,
    prime_world_loot_cache,
)
from dso_server.services.world import AUTO_PICKUP_CATEGORIES


@pytest.fixture(scope="module")
def cache():
    return prime_world_loot_cache(DEFAULT_STATIC_DB_PATH)


@pytest.fixture(scope="module")
def worldboss_template(cache):
    return next(k for k, v in cache.monster_profiles.items() if v.rank_kind == "worldboss")


@pytest.fixture(scope="module")
def normal_monster_template(cache):
    return next(k for k, v in cache.monster_profiles.items() if v.rank_kind == "monster")


def test_worldboss_bloodshed_guaranteed_drops(worldboss_template):
    """Worldboss on Bloodshed (difficulty 8) drops guaranteed 75x Wisdom and 1x-3x Realm Fragments,
    with auto_pick_up='All'."""
    bloodshed_tier = 8
    wisdom_stacks_seen = []
    rf_amounts_seen = set()

    for seed in range(200):
        rng = random.Random(seed)
        gen = generate_monster_loot(
            DEFAULT_STATIC_DB_PATH,
            worldboss_template,
            class_name="mage",
            player_level=100,
            monster_level=140,
            position=(0.0, 0.0, 0.0),
            next_item_id=1,
            rng=rng,
            map_difficulty=bloodshed_tier,
        )

        wisdom_drops = [d for d in gen.drops if d.item.template_id == WISDOM_ITEM_TEMPLATE]
        rf_drops = [d for d in gen.drops if d.item.template_id == REALM_FRAGMENT_ITEM_TEMPLATE]

        # There must always be at least one guaranteed wisdom stack of 75
        assert any(d.item.amount == 75 for d in wisdom_drops)
        for d in wisdom_drops:
            assert d.auto_pick_up == "All"
            assert d.item_category in AUTO_PICKUP_CATEGORIES

        # There must always be a guaranteed Realm Fragment drop of 1x to 3x
        assert len(rf_drops) >= 1
        for d in rf_drops:
            assert d.auto_pick_up == "All"
            assert d.item_category in AUTO_PICKUP_CATEGORIES
            rf_amounts_seen.add(d.item.amount)

        # Track bonus wisdom (50x)
        bonus_drops = [d for d in wisdom_drops if d.item.amount == 50]
        if bonus_drops:
            wisdom_stacks_seen.append(True)
        else:
            wisdom_stacks_seen.append(False)

    # 1x, 2x, 3x should all be observed across seeds
    assert {1, 2, 3}.issubset(rf_amounts_seen)

    # Bonus 50x wisdom has 25% chance
    bonus_rate = sum(wisdom_stacks_seen) / len(wisdom_stacks_seen)
    assert 0.15 <= bonus_rate <= 0.35


def test_worldboss_difficulty_scaling(worldboss_template):
    """Worldboss drops scale down proportionally across difficulty tiers."""
    for diff in range(10):
        expected_wisdom = WORLDBOSS_GUARANTEED_WISDOM_BY_DIFFICULTY[diff]
        expected_bonus = WORLDBOSS_BONUS_WISDOM_BY_DIFFICULTY[diff]
        low_rf, high_rf = WORLDBOSS_REALM_FRAGMENT_RANGE_BY_DIFFICULTY[diff]

        gen = generate_monster_loot(
            DEFAULT_STATIC_DB_PATH,
            worldboss_template,
            class_name="mage",
            player_level=100,
            monster_level=140,
            position=(0.0, 0.0, 0.0),
            next_item_id=1,
            rng=random.Random(42),
            map_difficulty=diff,
            force_drops=True,
        )

        wisdom_amounts = [d.item.amount for d in gen.drops if d.item.template_id == WISDOM_ITEM_TEMPLATE]
        rf_amounts = [d.item.amount for d in gen.drops if d.item.template_id == REALM_FRAGMENT_ITEM_TEMPLATE]

        assert expected_wisdom in wisdom_amounts
        assert expected_bonus in wisdom_amounts
        assert any(low_rf <= amt <= high_rf for amt in rf_amounts)


@pytest.mark.parametrize("rank", ["guardian", "leader", "champion", "sentinel"])
def test_special_mobs_bloodshed_drops(cache, rank):
    """Guardian, leader, champion, sentinel mobs on Bloodshed have 15% chance for 20x Wisdom & 3x Realm Fragments."""
    matching_template = next(k for k, v in cache.monster_profiles.items() if v.rank_kind == rank)

    gen = generate_monster_loot(
        DEFAULT_STATIC_DB_PATH,
        matching_template,
        class_name="mage",
        player_level=100,
        monster_level=140,
        position=(0.0, 0.0, 0.0),
        next_item_id=1,
        rng=random.Random(1),
        map_difficulty=8,  # Bloodshed
        force_drops=True,
    )

    wisdom_drops = [d for d in gen.drops if d.item.template_id == WISDOM_ITEM_TEMPLATE and d.item.amount == 20]
    rf_drops = [d for d in gen.drops if d.item.template_id == REALM_FRAGMENT_ITEM_TEMPLATE and d.item.amount == 3]

    assert len(wisdom_drops) >= 1
    assert len(rf_drops) >= 1
    assert wisdom_drops[0].auto_pick_up == "All"
    assert rf_drops[0].auto_pick_up == "All"


def test_special_mobs_difficulty_scaling(cache):
    """Guardian/leader/champion/sentinel mob loot scales by difficulty."""
    template = next(k for k, v in cache.monster_profiles.items() if v.rank_kind in SPECIAL_MOB_RANKS)

    for diff in range(10):
        expected_wisdom = SPECIAL_MOB_WISDOM_BY_DIFFICULTY[diff]
        expected_rf = SPECIAL_MOB_REALM_FRAGMENTS_BY_DIFFICULTY[diff]

        gen = generate_monster_loot(
            DEFAULT_STATIC_DB_PATH,
            template,
            class_name="mage",
            player_level=100,
            monster_level=140,
            position=(0.0, 0.0, 0.0),
            next_item_id=1,
            rng=random.Random(1),
            map_difficulty=diff,
            force_drops=True,
        )

        wisdom_amounts = [d.item.amount for d in gen.drops if d.item.template_id == WISDOM_ITEM_TEMPLATE]
        rf_amounts = [d.item.amount for d in gen.drops if d.item.template_id == REALM_FRAGMENT_ITEM_TEMPLATE]

        assert expected_wisdom in wisdom_amounts
        assert expected_rf in rf_amounts


def test_all_mobs_realm_fragment_and_wisdom_drops(normal_monster_template):
    """All ordinary mobs have a 0.05% chance for 1x Realm Fragment and 0.015% chance for 1x or 3x Wisdom."""
    class ControlledRNG(random.Random):
        def __init__(self, random_val, choice_val):
            super().__init__()
            self.val = random_val
            self.choice_val = choice_val

        def random(self):
            return self.val

        def choice(self, seq):
            return self.choice_val

    # Test Realm Fragment drop trigger (val <= 0.0005)
    gen_rf = generate_monster_loot(
        DEFAULT_STATIC_DB_PATH,
        normal_monster_template,
        class_name="mage",
        player_level=100,
        monster_level=140,
        position=(0.0, 0.0, 0.0),
        next_item_id=1,
        rng=ControlledRNG(random_val=0.0004, choice_val=1),
        map_difficulty=0,
    )
    rf_drops = [d for d in gen_rf.drops if d.item.template_id == REALM_FRAGMENT_ITEM_TEMPLATE]
    assert len(rf_drops) == 1
    assert rf_drops[0].item.amount == 1
    assert rf_drops[0].auto_pick_up == "All"

    # Test Ancient Wisdom drop trigger (val <= 0.00015) with 3x
    gen_wis = generate_monster_loot(
        DEFAULT_STATIC_DB_PATH,
        normal_monster_template,
        class_name="mage",
        player_level=100,
        monster_level=140,
        position=(0.0, 0.0, 0.0),
        next_item_id=1,
        rng=ControlledRNG(random_val=0.0001, choice_val=3),
        map_difficulty=0,
    )
    wis_drops = [d for d in gen_wis.drops if d.item.template_id == WISDOM_ITEM_TEMPLATE and d.item.amount == 3]
    assert len(wis_drops) == 1
    assert wis_drops[0].auto_pick_up == "All"
