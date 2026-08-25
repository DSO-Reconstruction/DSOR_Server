"""Configuration from a file, because fifty-one flags is not a configuration system."""

import dataclasses

import pytest

from dsor import config
from dsor.world import Rules


def write(tmp_path, text):
    path = tmp_path / "dsor.toml"
    path.write_text(text)
    return path


def test_a_missing_default_file_is_fine_and_a_named_one_is_not():
    """A checkout with no config still runs; a path given explicitly must exist."""
    assert config.load(None) == {} or isinstance(config.load(None), dict)
    with pytest.raises(config.ConfigError, match="no config file"):
        config.load("/nowhere/dsor.toml")


def test_a_mistyped_key_is_refused_with_the_nearest_name():
    """The worst available behaviour is to start, look healthy, and not do the thing
    that was asked."""
    rules = Rules()
    with pytest.raises(config.ConfigError, match="start_level"):
        config.apply_to(rules, {"start_levl": 1}, "rules")


def test_a_wrong_type_is_refused_rather_than_kept():
    """TOML's types and a dataclass's do not quite line up, and a rule that silently
    keeps the wrong one fails later and somewhere else."""
    rules = Rules()
    with pytest.raises(config.ConfigError, match="whole number"):
        config.apply_to(rules, {"start_level": "cent"}, "rules")
    with pytest.raises(config.ConfigError, match="true or false"):
        config.apply_to(rules, {"enforce": 1}, "rules")
    with pytest.raises(config.ConfigError, match="a list"):
        config.apply_to(rules, {"drop_templates": "a_sword"}, "rules")

    # An integer for a float field is the common one, and it is accepted and converted.
    config.apply_to(rules, {"creature_damage": 250}, "rules")
    assert rules.creature_damage == 250.0
    assert isinstance(rules.creature_damage, float)


def test_a_boolean_is_not_a_number():
    """True is an int in Python, and a rule that takes it as one is a silent bug."""
    rules = Rules()
    with pytest.raises(config.ConfigError):
        config.apply_to(rules, {"start_level": True}, "rules")


def test_a_per_world_table_wins_over_the_global_one():
    """Which is the point of having a file: one server, several worlds, different
    numbers."""
    settings = {
        "rules": {"creature_damage": 1.0, "start_level": 15},
        "world": {"a0001": {"creature_damage": 250.0}},
    }
    merged = config.rules_for(settings, "a0001_start_tutorial_dun")
    assert merged == {"creature_damage": 250.0, "start_level": 15}


def test_a_per_world_table_does_not_reach_another_world():
    settings = {"rules": {}, "world": {"a0001": {"creature_damage": 250.0}}}
    assert config.rules_for(settings, "a0002_start_hub") == {}


def test_a_per_world_table_does_not_reach_a_service_with_no_world():
    """The login and character services carry rules of the same shape and no world, so
    a per-world table would land on them too and be wrong the moment there are two
    maps."""
    settings = {"rules": {"start_level": 15}, "world": {"a0001": {"start_level": 100}}}
    assert config.rules_for(settings, "a0001", worlds=False) == {"start_level": 15}


def test_every_key_in_the_shipped_file_names_a_real_setting():
    """The file in the repository has to load, or it is documentation that lies."""
    from pathlib import Path

    path = Path("dsor.toml")
    if not path.exists():
        pytest.skip("no dsor.toml in this checkout")
    settings = config.load(path)
    names = {field.name for field in dataclasses.fields(Rules)}
    for table in ["rules"] + [f"world.{w}" for w in settings.get("world", {})]:
        values = (
            settings.get("rules")
            if table == "rules"
            else settings["world"][table.split(".", 1)[1]]
        )
        for key in values or {}:
            assert key in names, f"[{table}] {key} is not a rule"
    # And it applies cleanly.
    config.apply_to(Rules(), config.rules_for(settings, "a0001_start_tutorial_dun"), "rules")


def test_the_file_can_express_a_rule_no_flag_can():
    """Twenty-five of the forty-six rules have no command-line option at all, which is
    why [rules] does not go through argparse."""
    rules = Rules()
    config.apply_to(rules, {"dot_share": 0.5, "corpse_lifetime": 40}, "rules")
    assert rules.dot_share == 0.5
    assert rules.corpse_lifetime == 40
