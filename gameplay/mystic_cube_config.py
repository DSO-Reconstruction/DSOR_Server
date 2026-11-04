"""Mystic Cube drop chance and contents weights.

The CMS edits ``data/mystic_cube.json``; world loot generation and chest
opening read the effective values on every roll, so a saved change applies
without a restart. A missing or invalid document falls back to the authored
defaults.
"""

from __future__ import annotations

from functools import lru_cache
import json
import math
import os
from pathlib import Path

from dso_server.paths import DEFAULT_MYSTIC_CUBE_CONFIG_PATH
from dso_server.runtime import file_revisions


MAX_DIFFICULTY = 9
CONTENT_KEYS = ("unique", "set", "gold", "wisdom", "glyph", "gem", "andermant")
CONTENT_LABELS = {
    "unique": "Unique item",
    "set": "Class set piece",
    "gold": "Gold",
    "wisdom": "Ancient Wisdom",
    "glyph": "Glyph of Power",
    "gem": "Gem",
    "andermant": "999 Andermant",
}
# Cube template IDs in tier order, with the label the client shows.
CUBE_TIERS = (
    ("mystic_cube_common", "Common Mystic Cube"),
    ("mystic_cube_uncommon", "Uncommon Mystic Cube"),
    ("mystic_cube_magic", "Magic Mystic Cube"),
    ("mystic_cube_rare", "Rare Mystic Cube"),
    ("mystic_cube_epic", "Epic Mystic Cube"),
)
CUBE_TIER_IDS = frozenset(template_id for template_id, _ in CUBE_TIERS)

# One flat chance per kill; the environment variable keeps its probability
# form (0.03 = 3%) so existing deployments keep working.
_ENV_DROP_PROBABILITY = 0.03


def _env_default_percent() -> float:
    try:
        probability = float(
            os.getenv("DSO_MYSTIC_CUBE_DROP_CHANCE", str(_ENV_DROP_PROBABILITY))
        )
    except ValueError:
        probability = _ENV_DROP_PROBABILITY
    return max(0.0, min(1.0, probability)) * 100.0


DEFAULT_DROP_CHANCE_PERCENT = _env_default_percent()
DEFAULT_CONTENT_WEIGHTS = {
    "unique": 15,
    "set": 22,
    "gold": 15,
    "wisdom": 15,
    "glyph": 15,
    "gem": 15,
    "andermant": 3,
}


@lru_cache(maxsize=8)
def _read_document(path: str, modified_ns: int, size: int) -> dict:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return document if isinstance(document, dict) else {}


def _config_signature() -> tuple[str, int, int] | None:
    # Every member kill rolls the cube chance. A stat() and resolve() per
    # roll released the GIL and waited for it again on the gameplay threads;
    # the observed revision is re-checked in the background instead, and the
    # CMS writer invalidates it so a save applies at once.
    key = str(DEFAULT_MYSTIC_CUBE_CONFIG_PATH)
    revision = file_revisions.observed_revision(key)
    if revision is None:
        return None
    return (str(file_revisions.resolved_path(key)), *revision)


def _config_document() -> dict:
    signature = _config_signature()
    if signature is None:
        return {}
    return _read_document(*signature)


def valid_drop_chance(value) -> float | None:
    """Return a clean percentage, or ``None`` when the value is unusable."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(numeric) or not 0.0 <= numeric <= 100.0:
        return None
    return round(numeric, 3)


def valid_weights(value) -> dict | None:
    """Return whole-number weights that total 100, or ``None``."""

    if not isinstance(value, dict):
        return None
    result = {}
    for key in CONTENT_KEYS:
        weight = value.get(key)
        if isinstance(weight, bool) or not isinstance(weight, int):
            return None
        if not 0 <= weight <= 100:
            return None
        result[key] = weight
    if sum(result.values()) != 100:
        return None
    return result


def normalize_config(document) -> dict:
    """Fill every default into a raw configuration document."""

    if not isinstance(document, dict):
        document = {}

    stored_chance = document.get("drop_chance", {})
    if not isinstance(stored_chance, dict):
        stored_chance = {}
    default_chance = valid_drop_chance(stored_chance.get("default"))
    if default_chance is None:
        default_chance = DEFAULT_DROP_CHANCE_PERCENT
    stored_difficulties = stored_chance.get("difficulties", {})
    if not isinstance(stored_difficulties, dict):
        stored_difficulties = {}
    difficulties = {}
    for tier in range(MAX_DIFFICULTY + 1):
        chance = valid_drop_chance(stored_difficulties.get(str(tier)))
        difficulties[str(tier)] = default_chance if chance is None else chance

    stored_contents = document.get("contents", {})
    if not isinstance(stored_contents, dict):
        stored_contents = {}
    default_weights = valid_weights(stored_contents.get("default"))
    if default_weights is None:
        default_weights = dict(DEFAULT_CONTENT_WEIGHTS)
    tiers = {}
    stored_tiers = stored_contents.get("tiers", {})
    if isinstance(stored_tiers, dict):
        for template_id, weights in stored_tiers.items():
            if template_id not in CUBE_TIER_IDS:
                continue
            cleaned = valid_weights(weights)
            if cleaned is not None:
                tiers[template_id] = cleaned

    return {
        "drop_chance": {
            "default": default_chance,
            "difficulties": difficulties,
        },
        "contents": {"default": default_weights, "tiers": tiers},
    }


def config_snapshot() -> dict:
    """Return the effective configuration with defaults applied."""

    return normalize_config(_config_document())


def cube_drop_chance(difficulty) -> float:
    """Return the drop probability for one map difficulty."""

    tier = max(0, min(MAX_DIFFICULTY, int(difficulty or 0)))
    return config_snapshot()["drop_chance"]["difficulties"][str(tier)] / 100.0


def cube_content_weights(template_id: str) -> dict:
    """Return the category weights used by one cube template."""

    contents = config_snapshot()["contents"]
    return dict(contents["tiers"].get(str(template_id), contents["default"]))
