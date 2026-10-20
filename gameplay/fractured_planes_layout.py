"""Placement roles and density pools for the shipped Endless Tower layouts.

The client ships SpawnListId names but not their server-side list registry.
Use its concrete ``*_endlesstower`` monster variants to reconstruct those
families. Invisible scene controllers and fixed trap shooters must never be
selected as ordinary, kill-required density monsters.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
import sqlite3


# Fixed tower hardware is a hazard, not a duel, so the server shortens its
# cycles: a laser that burns once a second and a shooter that fires every
# 0.6 s leave the player too much room to walk through untouched.
#
# Keep the visible hazard and damage area aligned. The rotating laser uses its
# blue beam geometry, while fire shooters get longer projectile travel and a
# wider target acquisition radius without increasing the impact splash:
#
# * ``e_dra_fortress_normal_powernode`` has an authored 8-unit
#   ``dragan_rotation_aura_small_01`` status aura. Keep its native effect active
#   for the laser, while the server's separate damage check uses a tighter
#   contact radius so players are not hit well outside the visible hazard.
# * ``h0009_endlesstower_01_room_fire_shooter`` has a 1000-unit
#   ``AttackRange`` but only a 14-unit ``HitRange`` and a 40-unit projectile
#   travel limit. Increase acquisition and travel, leaving the impact radius
#   untouched so shots do not splash around the player.
#
# Keep the multipliers here: the aura tick (``map_events``) and the autonomous
# attack runtime (``local_monster_state``) must scale together or a laser and a
# trap shooter on the same floor would drift apart.
TOWER_ATTACK_RATE_MULTIPLIER = 5.0
# Applies to ``AttackRange`` (target acquisition) only, never to ``HitRange``.
TOWER_ATTACK_RANGE_MULTIPLIER = 3.0
TOWER_AURA_RATE_MULTIPLIER = 3.0
# A tower may never fire faster than this regardless of authored frames.
TOWER_MIN_ATTACK_INTERVAL = 0.10
TOWER_FIRE_BURST_SHOTS = 10
TOWER_FIRE_BURST_INTERVAL = 0.30
TOWER_FIRE_BURST_COOLDOWN = 2.0
TOWER_FIRE_PROJECTILE_SPEED_MULTIPLIER = 5.0
TOWER_FIRE_ACQUISITION_RANGE = 60.0


def is_tower_layout(map_id: str) -> bool:
    return str(map_id).casefold().startswith("h0009_endlesstower_")


@lru_cache(maxsize=16)
def _template_roles(database_path: str | Path) -> dict[str, str]:
    with sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT Id, CharacterSet, Graphics, BehaviorType FROM _Template_Monster"
        ).fetchall()
    return {
        str(template).casefold(): (
            "controller" if str(character_set).casefold() == "invisible"
            or str(graphics).casefold() == "characters/multi"
            else "mechanism" if "staticstrategy" in str(behavior).casefold()
            else "monster"
        )
        for template, character_set, graphics, behavior in rows
    }


def tower_actor_role(database_path: str | Path, template_id: str) -> str:
    return _template_roles(database_path).get(str(template_id).casefold(), "monster")


def is_tower_mechanism(database_path: str | Path, map_id: str, template_id: str) -> bool:
    return is_tower_layout(map_id) and tower_actor_role(database_path, template_id) != "monster"


def is_tower_controller(database_path: str | Path, map_id: str, template_id: str) -> bool:
    """Whether this row is an invisible scene controller, not a game actor."""

    return is_tower_layout(map_id) and tower_actor_role(database_path, template_id) == "controller"


def is_tower_hardware(database_path: str | Path, map_id: str, template_id: str) -> bool:
    """Visible fixed hardware: power nodes, trap shooters, sentinels.

    Unlike :func:`is_tower_mechanism` this excludes the invisible scene
    controllers, which exist for the script but have no model to publish.
    """

    return (
        is_tower_layout(map_id)
        and tower_actor_role(database_path, template_id) == "mechanism"
    )


@lru_cache(maxsize=64)
def tower_density_templates(
    database_path: str | Path,
    spawn_list_id: str,
    map_id: str = "",
) -> tuple[str, ...]:
    """Resolve an authored area to the combatants for its tower segment.

    The Endless Tower map is split into ten physical ``sm_NN`` segments.
    Their matching ``h0009_arena_NN`` templates are level-one coefficients
    which the server scales to the run's level.  The ``_endlesstower`` pool
    contains level-100 snapshots for the boss showcase; using those snapshots
    in every segment leaves their small authored HP values unscaled.
    """

    family = str(spawn_list_id).casefold().split("_", 1)[0]
    with sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        segment = re.search(r"_sm_(\d+)$", str(map_id).casefold())
        if segment:
            arena_id = f"h0009_arena_{int(segment.group(1)):02d}_room_{family}_%"
            rows = db.execute(
                "SELECT Id FROM _Template_Monster WHERE Id LIKE ? "
                "AND Rank='Rank_monster' AND COALESCE(Obsolete, 0)=0 "
                "AND CharLevel<=1 AND TRIM(COALESCE(Skills, ''))<>'' "
                "AND TRIM(COALESCE(Graphics, ''))<>'' "
                "AND lower(COALESCE(CharacterSet, ''))<>'invisible' "
                "ORDER BY Id",
                (arena_id,),
            ).fetchall()
            if rows:
                return tuple(str(row[0]) for row in rows)
        rows = db.execute(
            "SELECT Id, Graphics, CharacterSet, BehaviorType FROM _Template_Monster "
            "WHERE Id LIKE '%_endlesstower' AND Rank='Rank_monster' "
            "AND COALESCE(Obsolete, 0)=0 AND TRIM(COALESCE(Skills, ''))<>'' "
            "ORDER BY Id"
        ).fetchall()
    def matches(template: str) -> bool:
        key = template.casefold()
        if family in ("fire", "ice", "poison", "andermagic"):
            return f"_room_{family}_" in key
        return any(token in key for token in {
            "sliver": ("thing_gold", "thing_silver"),
            "ocean": ("c2203_", "c2302_", "c2303_"),
            "machine": ("f4102_", "f4202_"),
            "light": ("monster_wisp_", "pw019_"),
            "dragon": ("pw017_",),
            "shadow": ("pw199_", "pw203_"),
        }.get(family, ()))
    return tuple(
        str(template) for template, graphics, character_set, behavior in rows
        if matches(str(template)) and str(graphics).strip()
        and str(character_set).casefold() != "invisible"
        and "staticstrategy" not in str(behavior).casefold()
    )
