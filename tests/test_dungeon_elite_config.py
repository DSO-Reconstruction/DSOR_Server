"""Dungeon elite count configuration: defaults, persistence and application."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dso_server.gameplay import dungeon_elites
from dso_server.gameplay.dungeon_elites import (
    DEFAULT_ELITE_TARGETS, TARGET_KEYS, elite_targets, normalize_config,
)
from dso_server.gameplay.local_monster_state import (
    clear_monster_encounter_cache, load_local_monster_encounter,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import NewMonsterServerCommand
from dso_server.services.admin_elites import (
    DungeonEliteAdministration, EliteConfigConflict,
)


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "dungeon_elites.json"
    monkeypatch.setattr(
        dungeon_elites, "DEFAULT_DUNGEON_ELITES_CONFIG_PATH", path,
    )
    # The caps' own tests: the counts held, not the original's composition.
    monkeypatch.setattr(dungeon_elites, "DEFAULT_KEEP_AUTHORED", False)
    clear_monster_encounter_cache()
    yield path
    clear_monster_encounter_cache()


def write_config(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")


def test_defaults_are_low_and_scale_with_difficulty():
    for tier in range(9):
        assert all(
            DEFAULT_ELITE_TARGETS[tier][key] <= DEFAULT_ELITE_TARGETS[tier + 1][key]
            for key in TARGET_KEYS
        )
    assert DEFAULT_ELITE_TARGETS[0] == {
        "leaders": 1, "champions": 2, "guardians": 2,
    }


def test_missing_file_returns_defaults(config_path):
    assert elite_targets("a0406_dun", 0) == DEFAULT_ELITE_TARGETS[0]
    assert elite_targets("a0406_dun", 99) == DEFAULT_ELITE_TARGETS[9]


def test_file_targets_and_map_override(config_path):
    write_config(config_path, {
        "difficulties": {"1": {"leaders": 0, "champions": 1, "guardians": 5}},
        "maps": {"a0406_dun": {"leaders": 7}},
    })
    assert elite_targets("a0406_dun", 1) == {
        "leaders": 7, "champions": 1, "guardians": 5,
    }
    # A partial override inherits the remaining counts from the tier row.
    assert elite_targets("c2301_dun", 1) == {
        "leaders": 0, "champions": 1, "guardians": 5,
    }
    assert elite_targets("a0406_dun", 2) == {
        "leaders": 7, "champions": 4, "guardians": 4,
    }


def test_invalid_values_fall_back_to_defaults(config_path):
    write_config(config_path, {
        "difficulties": {"0": {"leaders": -1, "champions": "two", "guardians": 999}},
        "include_event_maps": "yes",
    })
    snapshot = dungeon_elites.config_snapshot()
    assert snapshot["difficulties"]["0"] == DEFAULT_ELITE_TARGETS[0]
    assert snapshot["include_event_maps"] is False


def ensure_encounter(map_id, difficulty):
    encounter = load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH, map_id, scaling_difficulty=difficulty,
    )
    assert encounter is not None
    return [
        command.value for command in encounter.spawn_batch.commands
        if isinstance(command, NewMonsterServerCommand)
    ]


def test_dungeons_hold_configured_elite_counts(config_path):
    write_config(config_path, {
        "difficulties": {
            "0": {"leaders": 1, "champions": 2, "guardians": 2},
            "3": {"leaders": 2, "champions": 5, "guardians": 5},
        },
    })
    for difficulty in (0, 3):
        targets = elite_targets("a0406_dun", difficulty)
        monsters = ensure_encounter("a0406_dun", difficulty)
        for rank, key in (
            ("Rank_leader", "leaders"),
            ("Rank_champion", "champions"),
            ("Rank_guardian", "guardians"),
        ):
            assert sum(monster.rank == rank for monster in monsters) == targets[key]


def test_authored_elites_are_reduced_to_the_configured_count(config_path):
    # c2301_dun authors dozens of Guardian templates; the cap must demote the
    # extra ones instead of showing every aura.
    write_config(config_path, {
        "difficulties": {"0": {"leaders": 1, "champions": 1, "guardians": 1}},
    })
    monsters = ensure_encounter("c2301_dun", 0)
    assert sum(monster.rank == "Rank_guardian" for monster in monsters) == 1
    assert sum(monster.rank == "Rank_monster" for monster in monsters) == len(monsters) - 3


def test_event_maps_keep_authored_population_by_default(config_path):
    monsters = ensure_encounter("e_ks_01_cave_01", 0)
    assert sum(monster.rank == "Rank_leader" for monster in monsters) > 10


def test_cms_round_trip_writes_and_reads_config(tmp_path, config_path):
    audit = Mock()
    service = SimpleNamespace(
        static_database_path=DEFAULT_STATIC_DB_PATH,
        repository=SimpleNamespace(audit=audit),
    )
    administration = DungeonEliteAdministration(service, config_path=config_path)
    overview = administration.overview()
    assert len(overview["difficulties"]) == 10
    assert overview["difficulties"][0]["name"] == "Normal"

    difficulties = {
        str(entry["tier"]): {
            "leaders": entry["leaders"] + 1,
            "champions": entry["champions"],
            "guardians": entry["guardians"],
        }
        for entry in overview["difficulties"]
    }
    saved = administration.update(
        "Operator", "tune elites", {
            "revision": overview["revision"],
            "difficulties": difficulties,
            "include_event_maps": True,
            "map_id": "a0406_dun",
            "map_targets": {"leaders": 9, "champions": 9, "guardians": 9},
        },
    )
    assert saved["ok"] is True
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert stored["include_event_maps"] is True
    assert stored["maps"]["a0406_dun"] == {"leaders": 9, "champions": 9, "guardians": 9}
    assert stored["difficulties"]["0"]["leaders"] == DEFAULT_ELITE_TARGETS[0]["leaders"] + 1
    audit.assert_called_once()
    assert elite_targets("a0406_dun", 5)["leaders"] == 9
    assert elite_targets("c2301_dun", 5)["leaders"] == difficulties["5"]["leaders"]

    overview = administration.overview(map_id="a0406_dun")
    assert overview["selected_targets"] == {"leaders": 9, "champions": 9, "guardians": 9}
    administration.update(
        "Operator", "clear override", {
            "revision": overview["revision"],
            "difficulties": difficulties,
            "include_event_maps": True,
            "map_id": "a0406_dun",
            "map_targets": None,
        },
    )
    assert elite_targets("a0406_dun", 5)["leaders"] == difficulties["5"]["leaders"]


def test_cms_rejects_stale_revision_and_bad_input(tmp_path, config_path):
    service = SimpleNamespace(
        static_database_path=DEFAULT_STATIC_DB_PATH,
        repository=SimpleNamespace(audit=Mock()),
    )
    administration = DungeonEliteAdministration(service, config_path=config_path)
    overview = administration.overview()
    difficulties = {
        str(entry["tier"]): {key: entry[key] for key in TARGET_KEYS}
        for entry in overview["difficulties"]
    }
    payload = {
        "revision": overview["revision"],
        "difficulties": difficulties,
        "include_event_maps": False,
    }
    with pytest.raises(ValueError):
        administration.update("Operator", "bad", {**payload, "difficulties": {"0": {}}})
    with pytest.raises(ValueError):
        administration.update("Operator", "bad", {
            **payload, "map_id": "not_a_map", "map_targets": difficulties["0"],
        })
    with pytest.raises(ValueError):
        administration.update("Operator", "bad", {
            **payload, "difficulties": {
                **difficulties, "0": {"leaders": 500, "champions": 1, "guardians": 1},
            },
        })
    administration.update("Operator", "first", payload)
    with pytest.raises(EliteConfigConflict):
        administration.update("Operator", "stale", payload)


def test_normalize_config_keeps_partial_overrides():
    snapshot = normalize_config({"maps": {"a0406_dun": {"guardians": 3}}})
    assert snapshot["maps"]["a0406_dun"] == {"guardians": 3}
