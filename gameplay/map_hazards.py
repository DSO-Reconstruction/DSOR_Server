"""Fixed map hazards: the ancestral-guard statues that shoot ice missiles.

The christmas maps (Winter Summit, Kranparus' lairs) and Eternal Watch stand
``characters/ancestral_guard`` statues in wall columns. They are authored as
ordinary SpawnPoint monsters, but they are scenery traps rather than
opponents: a static strategy, the ``e_mark_no_damage`` permanent effect
(``ActorFeature:off,Damage``) and an icetrap skill.

* ``NoTurnStaticStrategy`` statues face across a corridor and fire along
  their authored yaw whenever a player is near, so their missiles form the
  moving blue "laser" fences players have to time.
* ``StaticStrategy`` statues turn to aim at the player like a turret.

Each statue stands on its own little navigation island inside its column, so
the ordinary "reachable from the entry" spawn check would drop every one.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
import sqlite3


HAZARD_GRAPHICS = "characters/ancestral_guard"
# The client refuses to target this faction (local_monster_state._load_monster).
UNTARGETABLE_FACTION = 0x00010004
HAZARD_STATUS_EFFECT = "e_mark_no_damage"
FIXED_DIRECTION_STRATEGY = "noturnstaticstrategy"
AIMED_STRATEGY = "staticstrategy"


@lru_cache(maxsize=8)
def _hazard_strategies(database_path: str) -> dict[str, str]:
    with sqlite3.connect(
        Path(database_path).resolve().as_uri() + "?mode=ro", uri=True
    ) as database:
        rows = database.execute(
            "SELECT Id, BehaviorType, PermanentStatusEffect, Graphics "
            "FROM _Template_Monster"
        ).fetchall()
    result = {}
    for template_id, behavior, permanent, graphics in rows:
        strategy = str(behavior or "").casefold()
        effects = {
            value.split(":", 1)[0].strip().casefold()
            for value in re.split(r"[;,]", str(permanent or ""))
        }
        if (
            str(graphics or "").casefold() == HAZARD_GRAPHICS
            and strategy in {FIXED_DIRECTION_STRATEGY, AIMED_STRATEGY}
            and HAZARD_STATUS_EFFECT in effects
        ):
            result[str(template_id).casefold()] = strategy
    return result


def is_map_hazard(database_path: str | Path, template_id: str) -> bool:
    """Whether this monster template is a fixed, invulnerable statue trap."""

    return str(template_id).casefold() in _hazard_strategies(str(database_path))


def fires_fixed_direction(database_path: str | Path, template_id: str) -> bool:
    """Whether the hazard shoots along its authored facing, not at a player."""

    return (
        _hazard_strategies(str(database_path)).get(str(template_id).casefold())
        == FIXED_DIRECTION_STRATEGY
    )


def spawned_as_hazard(database_path: str | Path, monster) -> bool:
    """Whether this NewMonster was placed as a statue trap.

    Only statues on rows no EventSet switches are (local_monster_state);
    switched ones keep the ordinary monster behaviour.
    """

    placement = getattr(monster, "placement", None)
    return (
        getattr(placement, "field_00", None) == UNTARGETABLE_FACTION
        and is_map_hazard(database_path, monster.template_id)
    )
