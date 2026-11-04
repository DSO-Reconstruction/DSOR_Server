"""Guardian, Champion, and Leader population targets for dungeon instances.

The encounter builder keeps each of the three elite ranks at the configured
count: extra authored elites are demoted back to ordinary monsters and missing
ones are promoted from the normal pool. The CMS edits the JSON document read
here, so a saved change applies to the next generated encounter.
"""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

from dso_server.paths import DEFAULT_DUNGEON_ELITES_CONFIG_PATH


TARGET_KEYS = ("leaders", "champions", "guardians")
MAX_DIFFICULTY = 9
MAX_TARGET = 200

# Difficulty tiers follow _Template_MapDifficulty (0 Normal, 1 Painful, ...,
# 9 Parallel World Bloodshed). Defaults stay low so a dungeon is not covered in
# rank auras; the CMS raises them per tier or per map.
DEFAULT_ELITE_TARGETS = {
    0: {"leaders": 1, "champions": 2, "guardians": 2},
    1: {"leaders": 1, "champions": 3, "guardians": 3},
    2: {"leaders": 1, "champions": 4, "guardians": 4},
    3: {"leaders": 2, "champions": 5, "guardians": 5},
    4: {"leaders": 2, "champions": 6, "guardians": 6},
    5: {"leaders": 2, "champions": 7, "guardians": 7},
    6: {"leaders": 3, "champions": 7, "guardians": 8},
    7: {"leaders": 3, "champions": 8, "guardians": 8},
    8: {"leaders": 3, "champions": 9, "guardians": 9},
    9: {"leaders": 4, "champions": 10, "guardians": 10},
}
# Event maps author their encounters as waves of ranked actors, so the cap is
# off unless an administrator enables it for them.
DEFAULT_INCLUDE_EVENT_MAPS = False


@lru_cache(maxsize=8)
def _read_document(path: str, modified_ns: int, size: int) -> dict:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return document if isinstance(document, dict) else {}


def _config_signature() -> tuple[str, int, int] | None:
    path = Path(DEFAULT_DUNGEON_ELITES_CONFIG_PATH)
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path.resolve()), int(stat.st_mtime_ns), int(stat.st_size))


def elite_config_revision() -> tuple[str, int, int]:
    """Identify the loaded configuration for encounter cache keys."""

    return _config_signature() or ("", 0, 0)


def _clamped_target(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 <= value <= MAX_TARGET:
        return None
    return value


def _targets(value, fallback: dict) -> dict:
    result = dict(fallback)
    if not isinstance(value, dict):
        return result
    for key in TARGET_KEYS:
        cleaned = _clamped_target(value.get(key))
        if cleaned is not None:
            result[key] = cleaned
    return result


def _config_document() -> dict:
    signature = _config_signature()
    if signature is None:
        return {}
    return _read_document(*signature)


def config_snapshot() -> dict:
    """Return the effective configuration with defaults applied."""

    return normalize_config(_config_document())


def normalize_config(document) -> dict:
    """Fill every default into a raw configuration document."""

    if not isinstance(document, dict):
        document = {}
    stored_difficulties = document.get("difficulties", {})
    if not isinstance(stored_difficulties, dict):
        stored_difficulties = {}
    difficulties = {
        str(tier): _targets(
            stored_difficulties.get(str(tier)), DEFAULT_ELITE_TARGETS[tier]
        )
        for tier in range(MAX_DIFFICULTY + 1)
    }
    maps = {}
    stored_maps = document.get("maps", {})
    if isinstance(stored_maps, dict):
        for map_id, entry in stored_maps.items():
            if isinstance(map_id, str) and isinstance(entry, dict):
                maps[map_id] = _targets(entry, {})
    include_event_maps = document.get("include_event_maps")
    return {
        "difficulties": difficulties,
        "maps": maps,
        "include_event_maps": (
            include_event_maps
            if isinstance(include_event_maps, bool)
            else DEFAULT_INCLUDE_EVENT_MAPS
        ),
    }


def elite_targets(map_id: str, scaling_difficulty: int | None) -> dict:
    """Return the leader/champion/guardian counts for one instance."""

    snapshot = config_snapshot()
    tier = max(0, min(MAX_DIFFICULTY, int(scaling_difficulty or 0)))
    targets = dict(snapshot["difficulties"][str(tier)])
    override = snapshot["maps"].get(str(map_id))
    if override:
        targets.update(override)
    return targets


def include_event_maps() -> bool:
    return bool(config_snapshot()["include_event_maps"])
