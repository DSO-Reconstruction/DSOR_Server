"""An EventSet's spawn rows keep only the instance's MapVariation tier.

The Herald's lair boss_start set (row 279) authors its broodlings once per
difficulty tier (a0604_heraldsslair.mvr family 2125: 4 on tier 0, 6 on tiers
1-3, 8 on tiers 4-10); all 78 walked in at once.
"""

from collections import Counter

from dso_server.gameplay.map_event_state import (
    load_map_event_content, select_variation_spawn_rows,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH


def _broodlings(difficulty):
    content = load_map_event_content(DEFAULT_STATIC_DB_PATH, "a0604_heraldsslair")
    rows = select_variation_spawn_rows(content.spawn_rows_for_sets((279,)), difficulty)
    return Counter(row.template_id for row in rows)


def test_herald_broodlings_one_tier_per_difficulty():
    assert _broodlings(0) == {"a0604_normal_add_dragon_broodling": 4}
    assert _broodlings(2) == {"a0604_normal_add_dragon_broodling_scale": 6}
    assert _broodlings(10) == {"a0604_normal_add_dragon_broodling_scale": 8}


def _mercyless_areas(difficulty):
    import sqlite3
    from dso_server.gameplay.local_map_state import map_database_path
    from dso_server.gameplay.local_monster_state import _load_spawn_areas

    path = map_database_path(DEFAULT_STATIC_DB_PATH, "c2303_dun")
    with sqlite3.connect(path) as db:
        areas = _load_spawn_areas(
            db, enabled_event_sets=frozenset({43}),
            scaling_difficulty=difficulty,
        )
    return [a.parent_node_index for a in areas if a.event_set_row == 43]


def test_mercyless_extra_spawn_areas_stay_off_lower_tiers():
    # difficulty_extra_spawn (EventSet 43) opens on every map entry; its
    # areas sit under c2303_dun.mvr tiers 6-9 (nodes 23526..23532).
    assert _mercyless_areas(0) == []
    assert _mercyless_areas(5) == []
    assert _mercyless_areas(6) == [23526]
    assert _mercyless_areas(10) == [23532]


def test_medusa_scaling_gorgons_stay_off_the_normal_instance():
    # c2304_boss BOSS_spawn (EventSet 159) holds only tiers 1-10; tier 0 is
    # the always-present normal family.
    content = load_map_event_content(DEFAULT_STATIC_DB_PATH, "c2304_boss")
    assert select_variation_spawn_rows(content.spawn_rows_for_sets((159,)), 0) == ()
    rows = select_variation_spawn_rows(content.spawn_rows_for_sets((159,)), 3)
    assert Counter(r.template_id for r in rows) == {
        "c2303_normal_g_gen_gorgon_scale": 4,
        "c2303_miniboss_medusa_05_scale": 1,
    }
