"""Verify event gating and difficulty-scaled Guardian, Champion, and Leader spawns in dungeons."""

from collections import Counter
import pytest

from dso_server.gameplay import dungeon_elites
from dso_server.gameplay.dungeon_elites import elite_targets
from dso_server.gameplay.local_monster_state import (
    clear_monster_encounter_cache, load_local_monster_encounter,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import NewMonsterServerCommand


@pytest.fixture(autouse=True)
def default_elite_config(tmp_path, monkeypatch):
    # Keep the shipped defaults under test even when the operator has a CMS
    # configuration on the machine.
    monkeypatch.setattr(
        dungeon_elites, "DEFAULT_DUNGEON_ELITES_CONFIG_PATH",
        tmp_path / "dungeon_elites.json",
    )
    clear_monster_encounter_cache()
    yield
    clear_monster_encounter_cache()


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
def test_dungeon_elite_ranks_match_configured_counts(diff):
    """Each elite rank must spawn exactly the number the configuration asks for."""
    monsters = _get_encounter_monsters("a0406_dun", scaling_difficulty=diff)
    ranks = Counter(m.rank for m in monsters)
    targets = elite_targets("a0406_dun", diff)

    assert ranks["Rank_leader"] == targets["leaders"]
    assert ranks["Rank_champion"] == targets["champions"]
    assert ranks["Rank_guardian"] == targets["guardians"]

    # Total monsters should remain roughly consistent without overflowing
    assert 140 <= len(monsters) <= 200


def test_dungeon_elite_counts_are_monotonically_increasing():
    """Higher difficulty tiers must have greater or equal counts of elites than lower tiers."""
    prev_total_elites = -1

    for diff in [0, 1, 2, 3, 4, 6, 8]:
        monsters = _get_encounter_monsters("a0406_dun", scaling_difficulty=diff)
        ranks = Counter(m.rank for m in monsters)
        total_elites = (
            ranks["Rank_leader"] + ranks["Rank_champion"] + ranks["Rank_guardian"]
        )
        assert total_elites > prev_total_elites
        prev_total_elites = total_elites
