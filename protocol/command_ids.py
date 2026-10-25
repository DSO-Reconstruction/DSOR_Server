"""Resolve network command ids against whichever client build is being served.

Command IDs can change between client builds. The server defaults to the
R261_1024 table. The R261_203 and R261_192 tables remain available for older
clients through ``DSO_COMMAND_IDS``.
Protocol modules declare IDs by class name rather than by number::

    SWITCH_MAP_COMMAND = command_id("Commands::SwitchMapCommand", 0x70)

The literal stays as the dro_client64-patch5 value and as the fallback.
By default the server resolves against ``docs/command_ids_rel261_1024.json``.
``DSO_COMMAND_IDS`` selects a table dumped from another client -- see
``dump_command_ids.py`` for x64 and ``dump_command_ids_x86.py`` for x86 -- and
``DSO_COMMAND_IDS=builtin`` keeps the patch5 literals.

Resolution happens once at import, so this costs nothing per packet.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

__all__ = [
    "command_id",
    "active_table_path",
    "unresolved",
    "UNAVAILABLE_COMMAND_ID",
]

ENVIRONMENT_VARIABLE = "DSO_COMMAND_IDS"
DEFAULT_TABLE = (
    Path(__file__).resolve().parents[1] / "docs" / "command_ids_rel261_1024.json"
)
BUILTIN_VALUES = frozenset({"builtin", "patch5", "none"})

# Stands in for a command the selected build does not define. Keeping the
# current-client number instead is actively dangerous: of the seven classes the
# legacy client has no string for, three of their current ids belong to real
# legacy commands, so id 308 would arrive as SendQuickbarsCommand rather than
# UpgradeStickerbookItemCommand. The client would accept it and read a payload
# that means something else entirely. An id no build registers is rejected
# outright, which turns a silent wire-level corruption into the same visible
# "could not create command" the client already logs.
UNAVAILABLE_COMMAND_ID = 0xFFFF

_unresolved: dict[str, int] = {}


def active_table_path() -> Path | None:
    """The command-id table in use, or None when built-in values apply."""

    configured = (os.getenv(ENVIRONMENT_VARIABLE) or "").strip()
    if not configured:
        return DEFAULT_TABLE if DEFAULT_TABLE.is_file() else None
    if configured.casefold() in BUILTIN_VALUES:
        return None
    return Path(configured)


@lru_cache(maxsize=4)
def _table(path: str) -> dict[str, int]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    table = {}
    for name, info in raw.items():
        # Accept both the dumper's {"id": n, ...} records and a plain name -> id
        # mapping, so a hand-written table is usable too.
        value = info["id"] if isinstance(info, dict) else info
        table[str(name)] = int(value)
    return table


def command_id(class_name: str, default: int) -> int:
    """The id this build uses for ``class_name``.

    ``default`` is the current client's id and is returned unchanged when no
    table is selected, so the current server keeps every number it had.

    When a table *is* selected and does not name the class, the result is
    :data:`UNAVAILABLE_COMMAND_ID` rather than ``default`` -- see the note there
    for why carrying the current id over is unsafe. The class is recorded in
    :func:`unresolved` so startup can name it.
    """

    path = active_table_path()
    if path is None:
        return default
    table = _table(str(path))
    resolved = table.get(class_name)
    if resolved is None:
        # Builds disagree about whether a class carries its namespace: the
        # current client registers Commands::TransactionCommand while the legacy
        # one registers a bare TransactionCommand. Matching the short name
        # second keeps that from reading as a missing command.
        resolved = _by_short_name(str(path)).get(class_name.split("::")[-1])
    if resolved is None:
        _unresolved[class_name] = default
        return UNAVAILABLE_COMMAND_ID
    return resolved


@lru_cache(maxsize=4)
def _by_short_name(path: str) -> dict[str, int]:
    """Namespace-stripped lookup, skipping names that become ambiguous."""

    collected: dict[str, list[int]] = {}
    for name, value in _table(path).items():
        collected.setdefault(name.split("::")[-1], []).append(value)
    return {
        short: values[0]
        for short, values in collected.items()
        if len(set(values)) == 1
    }


def unresolved() -> dict[str, int]:
    """Classes the selected build does not define, with their current-client id.

    Each is a command this server can no longer send. Worth naming at startup:
    if one is reachable, the feature behind it needs gating for this build
    rather than a different number.
    """

    return dict(_unresolved)
