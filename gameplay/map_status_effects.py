"""Status effects a map applies to every player who enters it.

``_Template_MapAccessGroup.MapStatusEffect`` names the effects attached to a
map route.  The PvP arenas author ``pvp_char_modifier`` there: the permanent
buff that scales player damage, health, speed and mitigation down to the PvP
ruleset.  These effects are map-scoped, not durable character state, so they
are installed on entry and removed on the way out rather than saved.
"""

from __future__ import annotations

from functools import lru_cache

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.persistence.readonly_db import connect
from dso_server.protocol.gameplay_protocol import StatusEffectEntry


@lru_cache(maxsize=64)
def map_status_effect_definitions(
    database_path: str, map_access_id: str,
) -> tuple:
    """Resolve the effects authored on one map-access row.

    ``map_access_id`` is the row the client travelled to.  Rows that share an
    ``AccessGroup`` may carry different effects, so the exact ``Id`` match is
    tried first and the group is only used as a fallback.
    """

    map_access_id = str(map_access_id or "").strip()
    if not map_access_id:
        return ()
    with connect(database_path) as database:
        row = database.execute(
            "SELECT MapStatusEffect FROM _Template_MapAccessGroup "
            "WHERE Id=? ORDER BY rowid LIMIT 1",
            (map_access_id,),
        ).fetchone()
        if row is None:
            row = database.execute(
                "SELECT MapStatusEffect FROM _Template_MapAccessGroup "
                "WHERE AccessGroup=? ORDER BY rowid LIMIT 1",
                (map_access_id,),
            ).fetchone()
        if row is None:
            return ()
        return LocalCombatState._load_victim_status_effects(
            database, str(row[0] or ""),
        )


def map_status_effect_entry(
    definition, *, instance_id: int, actor_id: int,
) -> StatusEffectEntry:
    """Build the permanent, detailed entry that carries a map effect.

    Same shape as the event and membership buffs: no duration, no start
    offset, and the parameter values the effect program reads through ``$N``.
    """

    return StatusEffectEntry(
        effect_id=int(definition.effect_id),
        field_04=int(instance_id),
        field_0c=0,
        field_14=0,
        field_08=0,
        field_18=0,
        field_10=0,
        field_1c=100,
        field_20=int(actor_id),
        field_25=False,
        field_26=False,
        field_39=True,
        has_details=True,
        values=tuple(float(value) for value in definition.values),
    )


__all__ = ["map_status_effect_definitions", "map_status_effect_entry"]
