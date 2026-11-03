"""Verify event gating and difficulty-scaled Guardian, Champion, and Leader spawns in dungeons."""

from collections import Counter
import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.protocol.gameplay_protocol import NewMonsterServerCommand


def _get_encounter_monsters(map_id: str, scaling_difficulty: int = 0, active_event_flags: frozenset[str] = frozenset()):
    encounter = load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH,
        map_id,
        scaling_difficulty=scaling_difficulty,
        active_event_flags=active_event_flags,
    )
    assert encounter is not None, f"Encounter for {map_id} at diff {scaling_difficulty} should not be None"
    return [c.value for c in encounter.spawn_batch.commands if isinstance(c, NewMonsterServerCommand)]


def test_inactive_event_monsters_do_not_spawn_in_dungeons():
    """Event-gated monsters must never spawn in dungeons when their event is inactive."""
    monsters = _get_encounter_monsters("a0406_dun", scaling_difficulty=0)
    templates = {m.template_id for m in monsters}

    # Seasonal event mobs that must not be present
    assert "e_stego_normal_farm_meanie_gold_01" not in templates
    assert "e_anniversary_dds_normal_buzzsaw_duria" not in templates
    assert "e_ks_01_blitz_normal_rat_01" not in templates
    assert "e_ks_01_add_normal_rat_diff_normal" not in templates

    # Authored dungeon monsters should be present
    native_mobs = [t for t in templates if t.startswith("a0406_normal_g_gen_")]
    assert len(native_mobs) > 0


def test_active_event_monsters_spawn_when_event_flag_is_set():
    """Event-gated monsters should spawn when the corresponding event flag is active."""
    # When stellar gold event is active
    monsters = _get_encounter_monsters(
        "a0406_dun",
        scaling_difficulty=0,
        active_event_flags=frozenset({"stellar_gold_event_01"}),
    )
    templates = {m.template_id for m in monsters}
    assert any("gold" in t for t in templates)


@pytest.mark.parametrize("diff", [0, 1, 2, 3, 4, 6, 8])
def test_dungeon_elite_ranks_scale_with_difficulty(diff):
    """Guardians, Champions, and Leaders must increase in numbers as map difficulty increases."""
    monsters = _get_encounter_monsters("a0406_dun", scaling_difficulty=diff)
    ranks = Counter(m.rank for m in monsters)

    if diff == 0:
        # On Normal difficulty, no extra difficulty-promoted leaders or guardians
        assert ranks["Rank_leader"] == 0
        assert ranks["Rank_guardian"] == 0
    else:
        # On difficulty > 0, Guardians, Champions, and Leaders must all be present
        assert ranks["Rank_leader"] >= 1
        assert ranks["Rank_champion"] >= 2
        assert ranks["Rank_guardian"] >= 3

    # Total monsters should remain roughly consistent without overflowing
    assert 140 <= len(monsters) <= 200


def test_dungeon_elite_counts_are_monotonically_increasing():
    """Higher difficulty tiers must have greater or equal counts of elites than lower tiers."""
    prev_total_elites = -1
    prev_guardians = -1
    prev_champions = -1
    prev_leaders = -1

    for diff in [1, 2, 3, 4, 6, 8]:
        monsters = _get_encounter_monsters("a0406_dun", scaling_difficulty=diff)
        ranks = Counter(m.rank for m in monsters)

        leaders = ranks["Rank_leader"]
        champions = ranks["Rank_champion"]
        guardians = ranks["Rank_guardian"]
        total_elites = leaders + champions + guardians

        assert leaders >= prev_leaders, f"Leaders at diff {diff} ({leaders}) < diff {diff-1} ({prev_leaders})"
        assert champions >= prev_champions, f"Champions at diff {diff} ({champions}) < diff {diff-1} ({prev_champions})"
        assert guardians >= prev_guardians, f"Guardians at diff {diff} ({guardians}) < diff {diff-1} ({prev_guardians})"
        assert total_elites > prev_total_elites, f"Total elites at diff {diff} should strictly increase"

        prev_leaders = leaders
        prev_champions = champions
        prev_guardians = guardians
        prev_total_elites = total_elites
