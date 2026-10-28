"""Process-wide immutable status-effect runtime metadata.

Map combat used to lazily open ``static.db4`` the first time each status
effect, status group, or ActorVariable was encountered.  Dense fights can
encounter many new effects in one tick, so the lazy behavior produced I/O
spikes on the battle thread.  This module primes the small runtime subset once
per database revision and shares it between map instances.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
import sqlite3

from dso_server.runtime import resident_content


@dataclass(frozen=True)
class StatusRuntimeCatalog:
    programs: Mapping[int, tuple[float, str, str]]
    groups: Mapping[int, frozenset[str]]
    actor_variables: Mapping[str, float]
    effect_ids: Mapping[str, int]
    zero_duration_effect_ids: frozenset[int]
    # Wire id -> template Id: the stable name saves record next to each id.
    effect_names: Mapping[int, str] = MappingProxyType({})


def _tokens(*values: object) -> frozenset[str]:
    return frozenset(
        token.strip().casefold()
        for value in values
        for token in str(value or "").replace(",", ";").split(";")
        if token.strip()
    )


@lru_cache(maxsize=4)
def _load_status_runtime_catalog(path_text: str) -> StatusRuntimeCatalog:
    path = Path(path_text)
    if not path.is_file():
        raise FileNotFoundError(path)
    with sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True
    ) as database:
        status_rows = database.execute(
            """
            SELECT rowid - 1, Id, COALESCE(StatusEffectTickRate, 1),
                   COALESCE(TickModifiers, ''),
                   COALESCE(StartModifiers, ''), Groups, ExclusiveGroup, StatusEffectDuration
            FROM _Template_StatusEffect
            """
        ).fetchall()
        variable_rows = database.execute(
            "SELECT Id, COALESCE(InitialValue, 0) "
            "FROM _Template_ActorVariables"
        ).fetchall()
    return StatusRuntimeCatalog(
        programs=MappingProxyType({
            int(row[0]): (
                max(0.04, float(row[2])),
                str(row[3]),
                str(row[4]),
            )
            for row in status_rows
        }),
        groups=MappingProxyType({
            int(row[0]): _tokens(row[5], row[6]) for row in status_rows
        }),
        actor_variables=MappingProxyType({
            str(row[0]).casefold(): float(row[1]) for row in variable_rows
        }),
        zero_duration_effect_ids=frozenset(int(r[0]) for r in status_rows if float(r[7] or 0) <= 0),
        effect_ids=MappingProxyType({
            str(row[1]): int(row[0]) for row in status_rows
        }),
        effect_names=MappingProxyType({
            int(row[0]): str(row[1]) for row in status_rows
        }),
    )


def load_status_runtime_catalog(
    database_path: str | Path,
) -> StatusRuntimeCatalog:
    return _load_status_runtime_catalog(
        str(resident_content.resolved(database_path))
    )


def rebind_saved_status_effects(entries, names, catalog: StatusRuntimeCatalog):
    """Resolve saved effects by their recorded name in the active database.

    A status effect's wire id is its ``_Template_StatusEffect`` row number, so
    a client-data update that inserts rows renumbers every later effect: a
    saved sticker-book bonus came back as Sewers2026_Vstate_mage ("Venomous
    Form") after the October 2026 database. Entries whose instance has a
    recorded name take that name's current id and are dropped if the name no
    longer exists; entries from saves without names keep their number.
    """

    rebound = []
    for entry in entries:
        name = names.get(int(entry.field_04))
        if name is None:
            rebound.append(entry)
            continue
        effect_id = catalog.effect_ids.get(name)
        if effect_id is None:
            continue
        rebound.append(
            entry if int(entry.effect_id) == effect_id
            else replace(entry, effect_id=effect_id)
        )
    return tuple(rebound)


__all__ = [
    "StatusRuntimeCatalog",
    "load_status_runtime_catalog",
    "rebind_saved_status_effects",
]
