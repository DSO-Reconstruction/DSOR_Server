"""The top difficulties pay Extraordinary or better; monster gems stop short of the ladder's end."""

import random
from collections import Counter

import pytest

from dso_server.gameplay.mortis_arena import FINAL_GEM_REWARDS
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.world_loot_protocol import (
    EQUIPMENT_RARITIES, _monster_gem_rank, _roll_equipment_rarity, generate_monster_loot,
    prime_world_loot_cache,
)


RARE = EQUIPMENT_RARITIES.index("Rare")  # the client calls it Extraordinary
EPIC = EQUIPMENT_RARITIES.index("Epic")
BLOODSHED = 8
PARALLEL_WORLD_BLOODSHED = 9
ROLLS = 20_000


@pytest.fixture(scope="module")
def cache():
    return prime_world_loot_cache(DEFAULT_STATIC_DB_PATH)


@pytest.fixture(scope="module")
def boss(cache):
    return next(key for key, value in cache.monster_profiles.items()
                if value.rank_kind == "worldboss")


def _ranks(cache, *, level, difficulty):
    return Counter(
        _monster_gem_rank(cache, level, difficulty, random.Random(seed))
        for seed in range(ROLLS)
    )


def _best_monster_rank(cache):
    return chr(ord(max(cache.standard_gems_by_rank)) - 5)


@pytest.mark.parametrize("difficulty", (BLOODSHED, PARALLEL_WORLD_BLOODSHED))
def test_the_top_difficulties_drop_extraordinary_or_better(cache, difficulty):
    rolled = Counter(
        _roll_equipment_rarity(cache, difficulty, random.Random(seed)) for seed in range(2000)
    )
    assert set(rolled) == {RARE, EPIC}
    assert rolled[RARE] > rolled[EPIC]


def test_other_difficulties_keep_their_authored_odds(cache):
    assert cache.rarity_weights_by_difficulty[6] == (5.0, 15.0, 60.0, 18.0, 2.0)
    assert cache.rarity_weights_by_difficulty[0] == (39.0, 38.0, 19.0, 4.0, 0.0)


def test_a_bloodshed_kill_drops_no_equipment_below_extraordinary(boss):
    rarities = set()
    for seed in range(60):
        generation = generate_monster_loot(
            DEFAULT_STATIC_DB_PATH, boss, class_name="mage", player_level=100, monster_level=140,
            position=(0.0, 0.0, 0.0), next_item_id=1, rng=random.Random(seed),
            map_difficulty=BLOODSHED,
        )
        rarities.update(
            drop.item.field_08 for drop in generation.drops if drop.item.field_38 or drop.item.socket_count
            or drop.item_category not in {"Currency", "InstantConsumable", "EquipmentGem"}
        )
    assert rarities and min(rarities) >= RARE


def test_the_ladder_still_ends_where_this_was_measured(cache):
    assert max(cache.standard_gems_by_rank) == "q"
    assert _best_monster_rank(cache) == "l"


@pytest.mark.parametrize("difficulty", (BLOODSHED, PARALLEL_WORLD_BLOODSHED))
def test_the_best_monster_gem_is_a_five_percent_roll_on_the_top_difficulties(cache, difficulty):
    ranks = _ranks(cache, level=200, difficulty=difficulty)
    best = _best_monster_rank(cache)
    assert max(ranks) == best
    assert ranks[best] / ROLLS == pytest.approx(0.05, abs=0.01)


@pytest.mark.parametrize("difficulty", range(8))
def test_the_best_monster_gem_drops_nowhere_else(cache, difficulty):
    assert max(_ranks(cache, level=200, difficulty=difficulty)) < _best_monster_rank(cache)


@pytest.mark.parametrize("level,rank", [(1, "a"), (60, "a"), (100, "e"), (115, "g"), (140, "i")])
def test_gems_sit_five_ranks_below_their_level_band(cache, level, rank):
    # Below the top difficulties the level band is all there is.
    assert set(_ranks(cache, level=level, difficulty=4)) == {rank}


def test_a_boss_kill_rolls_every_gem_on_its_own(boss):
    seen = Counter()
    for seed in range(400):
        generation = generate_monster_loot(
            DEFAULT_STATIC_DB_PATH, boss, class_name="mage", player_level=100, monster_level=140,
            position=(0.0, 0.0, 0.0), next_item_id=1, rng=random.Random(seed),
            map_difficulty=BLOODSHED, extra_boss_gems=5,
        )
        gems = [drop.item.template_id for drop in generation.drops
                if drop.item_category == "EquipmentGem"]
        assert len(gems) == 5
        seen.update(gem.rsplit("_", 1)[1] for gem in gems)
    assert set(seen) == {"i", "l"}
    assert seen["l"] / sum(seen.values()) == pytest.approx(0.05, abs=0.02)


def test_mortis_stays_below_the_monster_ceiling(cache):
    ceiling = _best_monster_rank(cache)
    assert all(gem.rsplit("_", 1)[1] <= ceiling for gem in FINAL_GEM_REWARDS)
