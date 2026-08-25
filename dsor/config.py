"""Configuration from a file, because fifty-one flags is not a configuration system.

The server grew one command-line option per decision until there were fifty-one of
them and the line to start it was 428 characters, retyped in full on every restart. It
also could not express the thing a server most needs to: different settings for
different worlds.

So the file is the source of truth and the flags are overrides:

    defaults in the dataclasses  <-  the file  <-  the command line

A key that names no rule is an error rather than a shrug. Silently ignoring a typo in
a config file is the worst behaviour available: the server starts, looks healthy, and
does not do the thing that was asked.

    [service]
    advertise = "172.20.0.1"
    verbose = true

    [rules]
    start_level = 100
    tough_mob = 5_000_000

    [world.a0001_start_tutorial_dun]
    creature_damage = 250      # this map only
"""

from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path
from typing import Any

#: Looked for in the working directory when no path is given.
DEFAULT_NAME = "dsor.toml"


class ConfigError(Exception):
    """A configuration that cannot be applied, with the reason."""


def load(path: str | Path | None = None) -> dict[str, Any]:
    """Read a config file, or return an empty configuration if there is none.

    A path given explicitly must exist; the default name is optional, so a checkout
    with no config still runs.
    """
    if path is None:
        candidate = Path(DEFAULT_NAME)
        if not candidate.exists():
            return {}
    else:
        candidate = Path(path)
        if not candidate.exists():
            raise ConfigError(f"no config file at {candidate}")
    try:
        with candidate.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{candidate}: {error}") from error


def coerce(field: dataclasses.Field, value: Any) -> Any:
    """*value* as the *field* wants it, or an error naming both.

    TOML's types and a dataclass's do not quite line up -- an integer literal for a
    float field is the common one -- and a rule that silently keeps the wrong type
    fails later and somewhere else.
    """
    annotation = field.type if isinstance(field.type, str) else str(field.type)
    wanted = annotation.replace(" ", "")
    if wanted.startswith("bool"):
        if not isinstance(value, bool):
            raise ConfigError(f"{field.name} wants true or false, got {value!r}")
        return value
    if wanted.startswith("int"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{field.name} wants a whole number, got {value!r}")
        return int(value)
    if wanted.startswith("float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{field.name} wants a number, got {value!r}")
        return float(value)
    if wanted.startswith("str"):
        if not isinstance(value, str):
            raise ConfigError(f"{field.name} wants a string, got {value!r}")
        return value
    if wanted.startswith("list"):
        if not isinstance(value, list):
            raise ConfigError(f"{field.name} wants a list, got {value!r}")
        return list(value)
    # Anything else -- a dataclass, a dict -- is not settable from a file.
    raise ConfigError(f"{field.name} cannot be set from a config file")


def apply_to(target: Any, values: dict[str, Any], where: str) -> list[str]:
    """Set *values* on the dataclass *target*, and say which names were used.

    Refuses a name the dataclass does not have, with the nearest ones it does, because
    a mistyped key in a config file is the failure that looks like success.
    """
    fields = {field.name: field for field in dataclasses.fields(target)}
    applied = []
    for name, value in values.items():
        field = fields.get(name)
        if field is None:
            near = sorted(
                other for other in fields if name[:4] and other.startswith(name[:4])
            )
            hint = f"; did you mean {', '.join(near)}?" if near else ""
            raise ConfigError(f"[{where}] has no setting {name!r}{hint}")
        setattr(target, name, coerce(field, value))
        applied.append(name)
    return applied


def rules_for(
    config: dict[str, Any], map_name: str | None, worlds: bool = True
) -> dict[str, Any]:
    """The ``[rules]`` table, with any ``[world.<map>]`` table layered on top.

    Which is the point of having a file at all: one server, several worlds, different
    numbers. A per-world key wins over the global one.
    """
    merged = dict(config.get("rules") or {})
    if not worlds:
        # The login and character services carry rules of the same shape but no world,
        # so a per-world table would land on them too and be wrong the moment there is
        # more than one map.
        return merged
    tables = config.get("world") or {}
    if map_name:
        for name, table in tables.items():
            if name == map_name or map_name.startswith(name):
                merged.update(table or {})
    return merged
