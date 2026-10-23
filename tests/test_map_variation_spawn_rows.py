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
