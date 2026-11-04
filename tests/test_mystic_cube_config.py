"""Mystic Cube CMS configuration: defaults, persistence and application."""

from __future__ import annotations

import json
import random
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dso_server.gameplay import mystic_cube_config
from dso_server.gameplay.mystic_cube_config import (
    CONTENT_KEYS,
    DEFAULT_CONTENT_WEIGHTS,
    DEFAULT_DROP_CHANCE_PERCENT,
    cube_content_weights,
    cube_drop_chance,
    normalize_config,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.chest_loot_protocol import roll_chest_contents
from dso_server.protocol.world_loot_protocol import (
    generate_monster_loot,
    prime_world_loot_cache,
)
from dso_server.services.admin_mystic_cube import (
    MysticCubeAdministration, MysticCubeConfigConflict,
)


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "mystic_cube.json"
    monkeypatch.setattr(
        mystic_cube_config, "DEFAULT_MYSTIC_CUBE_CONFIG_PATH", path,
    )
    mystic_cube_config._read_document.cache_clear()
    yield path
    mystic_cube_config._read_document.cache_clear()


def write_config(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")


class _FixedRng:
    """One percentage roll, then scripted choices."""

    def __init__(self, roll, *choices):
        self._roll = int(roll)
        self._choices = list(choices)

    def randrange(self, stop):
        assert int(stop) == 100
        return self._roll

    def choice(self, sequence):
        value = self._choices.pop(0) if self._choices else sequence[0]
        assert value in sequence
        return value


def _contents(box, *, roll, class_name="mage", choices=()):
    return roll_chest_contents(
        DEFAULT_STATIC_DB_PATH,
        box,
        player_level=1,
        class_name=class_name,
        rng=_FixedRng(roll, *choices),
    )


def test_defaults_match_the_authored_table(config_path):
    assert DEFAULT_DROP_CHANCE_PERCENT == 3.0
    assert DEFAULT_CONTENT_WEIGHTS == {
        "unique": 15, "set": 22, "gold": 15, "wisdom": 15,
        "glyph": 15, "gem": 15, "andermant": 3,
    }
    assert sum(DEFAULT_CONTENT_WEIGHTS.values()) == 100
    assert cube_drop_chance(0) == 0.03
    assert cube_content_weights("mystic_cube_epic") == DEFAULT_CONTENT_WEIGHTS


def test_missing_file_returns_defaults(config_path):
    assert cube_drop_chance(5) == 0.03
    snapshot = mystic_cube_config.config_snapshot()
    assert snapshot["drop_chance"]["default"] == 3.0
    assert snapshot["contents"]["tiers"] == {}


def test_default_and_difficulty_drop_chances(config_path):
    write_config(config_path, {
        "drop_chance": {"default": 1.5, "difficulties": {"8": 10}},
    })
    assert cube_drop_chance(8) == 0.10
    assert cube_drop_chance(0) == 0.015
    # Out-of-range difficulties clamp at the ladder's ends, where tiers
    # without their own value still inherit the default.
    assert cube_drop_chance(99) == 0.015
    assert cube_drop_chance(-3) == 0.015


def test_invalid_values_fall_back_to_defaults(config_path):
    write_config(config_path, {
        "drop_chance": {"default": -1, "difficulties": {"0": "high"}},
        "contents": {
            "default": {key: 50 for key in CONTENT_KEYS},
            "tiers": {
                "bogus_cube": {"andermant": 100},
                "mystic_cube_epic": {"gold": 100},
            },
        },
    })
    snapshot = mystic_cube_config.config_snapshot()
    assert snapshot["drop_chance"]["difficulties"]["0"] == 3.0
    assert snapshot["contents"]["default"] == DEFAULT_CONTENT_WEIGHTS
    # Unknown tiers are dropped; a tier whose weights do not total 100 is too.
    assert snapshot["contents"]["tiers"] == {}


def test_a_tier_override_only_changes_that_cube(config_path):
    write_config(config_path, {
        "contents": {
            "default": dict(DEFAULT_CONTENT_WEIGHTS),
            "tiers": {"mystic_cube_epic": {
                "unique": 0, "set": 0, "gold": 100, "wisdom": 0,
                "glyph": 0, "gem": 0, "andermant": 0,
            }},
        },
    })
    assert cube_content_weights("mystic_cube_epic")["gold"] == 100
    assert cube_content_weights("mystic_cube_common") == DEFAULT_CONTENT_WEIGHTS


def test_contents_roll_uses_the_configured_weights(config_path):
    write_config(config_path, {
        "contents": {"default": {
            "unique": 0, "set": 0, "gold": 100, "wisdom": 0,
            "glyph": 0, "gem": 0, "andermant": 0,
        }},
    })
    contents = _contents("mystic_cube_common", roll=0)
    [reward] = contents.rewards
    assert contents.source == "mystic_cube"
    assert (reward.definition.template_id, reward.amount) == ("money_vc_gold", 19)


def test_a_tier_override_changes_only_its_own_roll(config_path):
    write_config(config_path, {
        "contents": {
            "default": {
                "unique": 0, "set": 0, "gold": 100, "wisdom": 0,
                "glyph": 0, "gem": 0, "andermant": 0,
            },
            "tiers": {"mystic_cube_epic": {
                "unique": 0, "set": 0, "gold": 0, "wisdom": 0,
                "glyph": 0, "gem": 0, "andermant": 100,
            }},
        },
    })
    [common] = _contents("mystic_cube_common", roll=99).rewards
    assert common.definition.template_id == "money_vc_gold"
    [epic] = _contents("mystic_cube_epic", roll=99).rewards
    assert (epic.definition.template_id, epic.amount) == ("money_rc", 999)


@pytest.fixture(scope="module")
def cache():
    return prime_world_loot_cache(DEFAULT_STATIC_DB_PATH)


@pytest.fixture(scope="module")
def monster(cache):
    return next(
        key for key, profile in cache.monster_profiles.items()
        if profile.rank_kind == "monster"
    )


def _cube_drops(monster, *, difficulty):
    generation = generate_monster_loot(
        DEFAULT_STATIC_DB_PATH,
        monster,
        class_name="ranger",
        player_level=60,
        monster_level=60,
        position=(0.0, 0.0, 0.0),
        next_item_id=1,
        rng=random.Random(7),
        force_drops=False,
        member_mystic_cube_eligible=True,
        map_difficulty=difficulty,
    )
    return [
        drop.item.template_id for drop in generation.drops
        if drop.item.template_id.startswith("mystic_cube")
    ]


def test_a_zero_chance_disables_the_drop(config_path, monster):
    write_config(config_path, {"drop_chance": {"default": 0}})
    assert _cube_drops(monster, difficulty=8) == []


def test_a_full_chance_always_drops(config_path, monster):
    write_config(config_path, {"drop_chance": {"default": 100}})
    assert _cube_drops(monster, difficulty=8) == ["mystic_cube_epic"]
    assert _cube_drops(monster, difficulty=0) == ["mystic_cube_common"]


def test_cms_round_trip_writes_and_reads_config(config_path):
    audit = Mock()
    service = SimpleNamespace(
        static_database_path=DEFAULT_STATIC_DB_PATH,
        repository=SimpleNamespace(audit=audit),
    )
    administration = MysticCubeAdministration(service, config_path=config_path)
    overview = administration.overview()
    assert overview["default"] == 3.0
    assert len(overview["difficulties"]) == 10
    assert overview["difficulties"][0]["name"] == "Normal"
    assert [entry["key"] for entry in overview["contents"]["keys"]] == list(CONTENT_KEYS)
    assert len(overview["contents"]["tiers"]) == 5
    assert not any(tier["overridden"] for tier in overview["contents"]["tiers"])

    difficulties = {
        str(entry["tier"]): (
            10 if entry["tier"] == 8 else 2 if entry["tier"] == 0 else entry["drop_chance"]
        )
        for entry in overview["difficulties"]
    }
    epic_weights = {
        "unique": 0, "set": 0, "gold": 0, "wisdom": 0,
        "glyph": 0, "gem": 0, "andermant": 100,
    }
    saved = administration.update(
        "Operator", "tune cubes", {
            "revision": overview["revision"],
            "drop_chance": {"default": 2, "difficulties": difficulties},
            "contents": {
                "default": dict(DEFAULT_CONTENT_WEIGHTS),
                "tiers": {"mystic_cube_epic": epic_weights},
            },
        },
    )
    assert saved["ok"] is True
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert stored["drop_chance"]["default"] == 2
    assert stored["drop_chance"]["difficulties"]["8"] == 10
    assert stored["contents"]["tiers"]["mystic_cube_epic"] == epic_weights
    audit.assert_called_once()
    assert cube_drop_chance(8) == 0.10
    assert cube_drop_chance(0) == 0.02
    assert cube_content_weights("mystic_cube_epic")["andermant"] == 100

    refreshed = administration.overview()
    overridden = [tier for tier in refreshed["contents"]["tiers"] if tier["overridden"]]
    assert [tier["template_id"] for tier in overridden] == ["mystic_cube_epic"]


def test_cms_rejects_bad_input_and_stale_revision(config_path):
    service = SimpleNamespace(
        static_database_path=DEFAULT_STATIC_DB_PATH,
        repository=SimpleNamespace(audit=Mock()),
    )
    administration = MysticCubeAdministration(service, config_path=config_path)
    overview = administration.overview()
    difficulties = {str(tier): 3.0 for tier in range(10)}
    payload = {
        "revision": overview["revision"],
        "drop_chance": {"default": 3, "difficulties": difficulties},
        "contents": {"default": dict(DEFAULT_CONTENT_WEIGHTS), "tiers": {}},
    }
    with pytest.raises(ValueError):
        administration.update("Operator", "bad", {
            **payload,
            "contents": {
                "default": {**DEFAULT_CONTENT_WEIGHTS, "andermant": 2},
                "tiers": {},
            },
        })
    with pytest.raises(ValueError):
        administration.update("Operator", "bad", {
            **payload,
            "drop_chance": {"default": 101, "difficulties": difficulties},
        })
    with pytest.raises(ValueError):
        administration.update("Operator", "bad", {
            **payload,
            "drop_chance": {
                "default": 3,
                "difficulties": {str(tier): 3 for tier in range(9)},
            },
        })
    with pytest.raises(ValueError):
        administration.update("Operator", "bad", {
            **payload,
            "contents": {
                "default": dict(DEFAULT_CONTENT_WEIGHTS),
                "tiers": {"not_a_cube": dict(DEFAULT_CONTENT_WEIGHTS)},
            },
        })
    administration.update("Operator", "first", payload)
    with pytest.raises(MysticCubeConfigConflict):
        administration.update("Operator", "stale", payload)


def test_normalize_config_keeps_only_valid_tiers():
    good = {"unique": 0, "set": 0, "gold": 0, "wisdom": 0,
            "glyph": 0, "gem": 100, "andermant": 0}
    snapshot = normalize_config({
        "contents": {"tiers": {"mystic_cube_rare": good}},
    })
    assert snapshot["contents"]["tiers"] == {"mystic_cube_rare": good}
