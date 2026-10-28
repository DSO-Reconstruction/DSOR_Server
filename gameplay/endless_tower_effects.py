"""Resolve Endless Tower stage buffs from the static game database."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import sqlite3
import hashlib

from dso_server.runtime import resident_content
from dso_server.gameplay.event_system import (
    EventStatusEffectSpec,
    parse_event_status_effects,
)
from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog


@dataclass(frozen=True)
class EndlessTowerPlayerBuffs:
    act_number: int
    specs: tuple[EventStatusEffectSpec, ...]
    all_effect_ids: frozenset[int]
    choices: tuple[str, ...]
    monster_specs: tuple[EventStatusEffectSpec, ...]


@lru_cache(maxsize=8)
def _load_endless_tower_player_buffs(
        database_path: str, access_id: str,
        character_id: int) -> EndlessTowerPlayerBuffs:
    path = Path(database_path)
    uri = f"file:{path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as database:
        floor = database.execute(
            "SELECT ActNumber FROM _Template_EndlessTower "
            "WHERE lower(MapAccess)=lower(?) LIMIT 1",
            (access_id,),
        ).fetchone()
        act_number = max(0, int(floor[0] or 0)) if floor else 0
        buff_rows = tuple(database.execute(
            "SELECT Id, COALESCE(AddBuffToPlayer, ''), "
            "COALESCE(TempAddBuffToPlayer, ''), "
            "COALESCE(AddBuffToMonster, '') "
            "FROM _Template_EndlessTowerBuff ORDER BY CAST(Id AS INTEGER)"
        ))

    status_catalog = load_status_runtime_catalog(path)
    all_resources = {
        resource.strip()
        for _act, permanent, temporary, _monster in buff_rows
        for value in (permanent, temporary)
        for resource in str(value or "").split(";")
        if resource.strip()
    }
    all_effect_ids = frozenset(
        status_catalog.effect_ids[resource]
        for resource in all_resources
        if resource in status_catalog.effect_ids
    )

    # Each row is a choice pool, not a list of simultaneous buffs. The native
    # portal consumes five chosen resource IDs and displays the first
    # ActNumber entries. Derive stable per-character choices so reconnects,
    # previews and floor entry all use the same set without rerolling.
    active_resources = []
    active_monsters = []
    choices = []
    for row_act, permanent, _temporary, monster in buff_rows:
        pool = [
            (value.strip(), target)
            for target, encoded in (("player", permanent), ("monster", monster))
            for value in str(encoded or "").split(";")
            if value.strip()
        ]
        if not pool:
            raise ValueError(f"empty Endless Tower buff pool {row_act}")
        digest = hashlib.sha256(
            f"fracturedplanes:{int(character_id)}:{row_act}".encode("ascii")
        ).digest()
        chosen, target = pool[int.from_bytes(digest[:8], "little") % len(pool)]
        choices.append(chosen)
        if 0 < int(row_act) <= act_number:
            (active_resources if target == "player" else active_monsters).append(chosen)
    specs = []
    seen = set()
    for resource, values in parse_event_status_effects(active_resources):
        key = resource.casefold()
        effect_id = status_catalog.effect_ids.get(resource)
        if effect_id is None or key in seen:
            continue
        seen.add(key)
        specs.append(EventStatusEffectSpec(resource, int(effect_id), values))
    return EndlessTowerPlayerBuffs(
        act_number, tuple(specs), all_effect_ids, tuple(choices),
        tuple(
            EventStatusEffectSpec(resource, int(status_catalog.effect_ids[resource]), values)
            for resource, values in parse_event_status_effects(active_monsters)
            if resource in status_catalog.effect_ids
        ),
    )


def endless_tower_player_buffs(
        database_path: str | Path, access_id: str, *,
        character_id: int) -> EndlessTowerPlayerBuffs:
    """Resolve one choice per act and grant only the reached player effects."""

    return _load_endless_tower_player_buffs(
        str(resident_content.resolved(database_path)), str(access_id).casefold(),
        int(character_id),
    )


__all__ = ["EndlessTowerPlayerBuffs", "endless_tower_player_buffs"]
