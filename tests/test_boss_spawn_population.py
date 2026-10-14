"""Bosses keep authored locations while density areas populate their support."""
from collections import Counter
from dataclasses import replace
import sqlite3
import struct
from types import SimpleNamespace

import pytest

from dso_server.gameplay import event_spawn_areas, local_monster_state as population
from dso_server.gameplay.boss_encounter_policy import select_boss_spawn_rows
from dso_server.gameplay.local_map_state import map_database_path
from dso_server.gameplay.pathengine_navigation import NavigationError
from dso_server.paths import DEFAULT_STATIC_DB_PATH as DATABASE
from dso_server.protocol.gameplay_protocol import NewMonsterServerCommand


KINGS = "a0702_kingscity_past_dun"
SARGON = "a0702_dun_boss_sargon_shadow"
SUPPORT = "reactivation_normal_minispider_01"
STAGING = "a0005_champion_grimmag_01_spawner"
EVENT_CASES = (
    ("e_chr_challenge_dun_01", "e_chr_2015_boss_krampus_challenge_01"),
    ("e_chr_challenge_dun_02", "e_chr_2015_boss_krampus_challenge_02"),
    ("e_chr_challenge_dun_03", "e_chr_2015_boss_krampus_challenge_03"),
    ("e_b1205_dun_chr_2025_01", "e_chr_boss_big_paws_mom"),
    ("e_dra_03_2025_dun_01", "dra_03_boss_dragan_2025"),
)


def actors(encounter):
    assert encounter is not None
    return tuple(c.value for c in encounter.spawn_batch.commands
                 if isinstance(c, NewMonsterServerCommand))


def authored_boss_position(map_id, template):
    with sqlite3.connect(map_database_path(DATABASE, map_id)) as db:
        rows = db.execute("SELECT Transform FROM _Instance_SpawnPoint WHERE NpcId=?",
                          (template,)).fetchall()
    assert len(rows) == 1
    return struct.unpack("<16f", rows[0][0])[12:15]


@pytest.mark.parametrize("difficulty", [0, 3, 8])
def test_kingscity_has_one_anchored_sargon_and_the_authored_support(difficulty):
    encounter = population.load_local_monster_encounter(
        DATABASE, KINGS, scaling_difficulty=difficulty,
        monster_level=100 if difficulty else None,
    )
    monsters = actors(encounter)
    counts = Counter(m.template_id for m in monsters)
    assert counts[SARGON] == 1
    assert counts["reactivation_guardian_minispider_01"] == 9
    boss = next(m for m in monsters if m.template_id == SARGON)
    assert boss.position == pytest.approx(authored_boss_position(KINGS, SARGON))
    for template in (SUPPORT, "reactivation_normal_beast_hound_01",
                     "reactivation_normal_skeleton_warrior_01",
                     "reactivation_normal_skeleton_archer_01"):
        assert counts[template] > 0
    with sqlite3.connect(map_database_path(DATABASE, KINGS)) as db:
        exclusions = [a for a in population._load_spawn_areas(db)
                      if not a.spawn_list_id]
    assert len(exclusions) == 2
    supporting = [m for m in monsters if m.template_id.startswith("reactivation_normal")]
    assert not any(a.contains(m.position) for a in exclusions for m in supporting)


@pytest.mark.parametrize("map_id,template", EVENT_CASES)
@pytest.mark.parametrize("difficulty", [0, 3])
def test_event_dungeons_do_not_repeat_their_fixed_boss(map_id, template, difficulty):
    monsters = actors(population.load_local_monster_encounter(
        DATABASE, map_id, scaling_difficulty=difficulty,
    ))
    bosses = [m for m in monsters if m.rank.casefold().startswith("rank_boss")]
    assert [m.template_id for m in bosses] == [template]
    position = authored_boss_position(map_id, template)
    # Grounding can correct the authored height; the boss stays at the marker.
    assert bosses[0].position[0] == pytest.approx(position[0], abs=2.0)
    assert bosses[0].position[2] == pytest.approx(position[2], abs=2.0)
    assert len(monsters) > 1


@pytest.mark.parametrize("source", ["fallback", "map", "override"])
def test_density_pools_cannot_repeat_bosses_or_boss_staging_actors(monkeypatch, source):
    with sqlite3.connect(map_database_path(DATABASE, KINGS)) as db:
        area = next(a for a in population._load_spawn_areas(db) if a.spawn_list_id)
    candidates = (SARGON, STAGING, SUPPORT)
    monkeypatch.setattr(population, "_SPAWN_LIST_TEMPLATE_OVERRIDES",
                        {KINGS: {area.spawn_list_id: candidates}} if source == "override" else {})
    monkeypatch.setattr(population, "_load_map_monster_templates",
                        lambda *args: candidates if source == "map" else ())
    fallback = candidates if source == "fallback" else ()
    assert population._area_template_pool(DATABASE, KINGS, area, fallback) == (SUPPORT,)
    assert population._area_template_pool(
        DATABASE, KINGS, replace(area, spawn_list_id=""), candidates,
    ) == ()


def test_unreachable_fixed_points_do_not_clone_the_boss_in_replacement_population(monkeypatch):
    with sqlite3.connect(map_database_path(DATABASE, KINGS)) as db:
        positions = {struct.unpack("<16f", row[0])[12:15] for row in db.execute(
            "SELECT Transform FROM _Instance_SpawnPoint")}
    ground = population._grounded_spawn_position

    def reject_authored(navigation, terrain, position, **kwargs):
        if position in positions:
            raise NavigationError("authored point on a disconnected component")
        return ground(navigation, terrain, position, **kwargs)

    monkeypatch.setattr(population, "_grounded_spawn_position", reject_authored)
    encounter = population._load_dynamic_monster_encounter(
        DATABASE, KINGS, include_spawn_areas=False,
    )
    monsters = actors(encounter)
    assert encounter.fixed_monster_count == 0
    assert sum(m.template_id == SARGON for m in monsters) == 1
    assert len(monsters) > 1


@pytest.mark.parametrize("with_support", [False, True])
@pytest.mark.parametrize("spawn_list", ["normal_spawn", ""])
def test_scripted_density_selectors_exclude_bosses_and_spawners(monkeypatch, with_support, spawn_list):
    map_id, boss = EVENT_CASES[0]
    with sqlite3.connect(map_database_path(DATABASE, map_id)) as db:
        area = population._load_spawn_areas(db)[0]
    area = replace(area, event_set_row=1234, spawn_list_id=spawn_list)
    monkeypatch.setattr(event_spawn_areas, "_load_spawn_areas", lambda *a, **kw: (area,))
    selectors = (boss, STAGING) + ((SUPPORT,) if with_support else ())
    rule = SimpleNamespace(server_evaluated=True, events_running={"wave"},
                           maps=(), params=selectors)
    runtime = SimpleNamespace(
        map_id=map_id, event_set_actor_ids={}, scaling_difficulty=0,
        running_events=lambda: {"wave"},
        catalog=SimpleNamespace(rules_by_event_type={"monsterseliminated": (rule,)}),
    )
    encounter = population.load_local_player_only_encounter(DATABASE, map_id)
    rows = event_spawn_areas.event_area_spawn_rows(runtime, encounter, {1234})
    if with_support and spawn_list:
        assert len(rows) > 1
        assert {r.template_id for r in rows} == {SUPPORT}
    else:
        assert rows == ()


def test_story_boss_phase_markers_and_support_are_preserved():
    rows = ((1, b"first", SARGON, 10), (2, b"later", SARGON, 20),
            (3, b"support", SUPPORT, 10))
    assert select_boss_spawn_rows(DATABASE, KINGS, rows) == rows
