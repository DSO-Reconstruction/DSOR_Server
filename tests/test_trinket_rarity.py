import random

from dso_server.paths import DEFAULT_STATIC_DB_PATH as DB
from dso_server.protocol.world_loot_protocol import (
    ENCHANTMENT_FREE_TRINKET_CATEGORIES, generate_monster_loot,
    roll_equipment_reward,
)


def test_trinkets_drop_common_on_every_difficulty():
    # They have no random enchantments: a rolled Magic trinket showed the
    # rarity with nothing on it. The original drops them Common.
    trinkets = []
    for difficulty in range(6):
        for seed in range(300):
            result = generate_monster_loot(DB, 'a0604_normal_g_Herold', class_name='warrior',
                player_level=55, monster_level=55, position=(1, 0, 1), next_item_id=1000,
                rng=random.Random(seed), map_difficulty=difficulty)
            trinkets.extend(drop for drop in result.drops
                            if drop.item_category in ENCHANTMENT_FREE_TRINKET_CATEGORIES)
    assert trinkets
    assert {drop.item.field_08 for drop in trinkets} == {0}


def test_rarity_boxes_above_common_hold_no_trinket():
    for seed in range(300):
        roll = roll_equipment_reward(DB, class_name='mage', item_level=55, rarity='Magic',
                                     rng=random.Random(seed))
        assert 'trinket' not in roll.template_id
