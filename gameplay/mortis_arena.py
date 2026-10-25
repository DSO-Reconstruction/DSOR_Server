"""Mortis' Exile content selectors, independent of optional .mvr files."""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
from pathlib import Path
import random
import sqlite3

from dso_server.gameplay.local_map_state import map_database_path

MAP_ID = "pw214_mortis_boss"
DRAGON = "boss_mortis_bone_dragon"
ADD_DRAGON = "boss_mortis_add_bone_dragon"
MOUNTED = "boss_mortis_mounted"
UNBOUND = "boss_mortis_unbound"
FINAL_GEM_REWARDS = tuple(
    f"gem_{family}_n" for family in (
        "ruby", "onyx", "amethyst", "cyanit", "diamond",
        "attackspeed", "block", "faster_run",
        "diamond_fire", "diamond_ice", "diamond_lightning",
        "diamond_poison", "diamond_andermagic",
    )
)
CONTROLLERS = frozenset({
    "mortis_bone_dragon_freeze_removal", "mortis_add_dragon_freeze_removal",
    "mortis_add_gate_trigger_monster",
})
FREEZES = {
    DRAGON: "Boss_Mortis_Bone_Dragon_Initial_Freeze",
    ADD_DRAGON: "Boss_Mortis_Add_Initial_Freeze",
}


@lru_cache(maxsize=8)
def _parent_groups(static_path: str, event_set: int):
    path = map_database_path(static_path, MAP_ID)
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        groups = {}
        for row, parent in db.execute(
            "SELECT rowid, ParentNodeIndex FROM _Instance_SpawnPoint "
            "WHERE EventSetRow=? ORDER BY rowid", (event_set,),
        ):
            groups.setdefault(int(parent), []).append(int(row))
    return tuple(tuple(rows) for rows in groups.values())


def select_spawn_rows(static_path, runtime, rows):
    """Choose one authored random pack at the instance's difficulty.

    The map contains ten tier blocks, each with eight alternative intermission
    packs. Gate packs have six alternatives on tiers 0-5, seven on tiers 6-9.
    Each parent is a *choice*, not another simultaneous wave. This structure
    is present in both supported map DBs, even when their .mvr was not exported.
    """
    if runtime.map_id.casefold() != MAP_ID:
        return tuple(rows)
    tier = min(9, max(0, int(runtime.scaling_difficulty)))
    allowed = {}
    for event_set in {row.event_set_row for row in rows} & {4214, 4215, 4216}:
        groups = _parent_groups(str(Path(static_path).resolve()), event_set)
        if event_set == 4216:
            choices = groups[tier * 8:(tier + 1) * 8]
        elif event_set == 4214:
            start = min(tier, 6) * 6 + max(0, tier - 6) * 7
            choices = groups[start:start + (6 if tier < 6 else 7)]
        else:
            choices = groups
        if not choices:
            raise ValueError(f"Mortis tier {tier} has no pack for set {event_set}")
        rng = random.Random(f"{runtime.instance_key!r}:{event_set}")
        weights = (
            (20, 20, 20, 15, 15, 10) if event_set == 4214 and tier < 6
            else (15, 15, 15, 15, 5, 15, 15) if event_set == 4214 and tier < 8
            else (1,) * len(choices)
        )
        allowed[event_set] = frozenset(rng.choices(choices, weights=weights, k=1)[0])
    result = []
    mounted_seen = False
    for row in rows:
        if row.template_id.casefold() in CONTROLLERS:
            continue
        if row.event_set_row in allowed and row.row_id not in allowed[row.event_set_row]:
            continue
        if row.template_id.casefold() == MOUNTED:
            if mounted_seen:
                continue
            mounted_seen = True
        if row.event_set_row == 4214:
            # Gate packs are authored in the off-arena staging pen (z=-8).
            # Their client script walks them through the gate at z=4. Spawn
            # there directly; the generic AI cannot traverse that staging pen.
            x, y, z = row.position
            row = replace(row, position=(x, y, z + 12.0))
        result.append(row)
    return tuple(result)


@lru_cache(maxsize=8)
def freeze_effect_ids(static_path):
    with sqlite3.connect(Path(static_path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        return frozenset(
            int(row[0]) for resource in FREEZES.values()
            for row in db.execute(
                "SELECT rowid-1 FROM _Template_StatusEffect WHERE Id=?", (resource,),
            )
        )


def released_spawn_batch(combat, batch):
    """Late viewers must not reapply a statue's already removed freeze."""
    if combat.encounter.map_id.casefold() != MAP_ID:
        return batch
    from dso_server.protocol.gameplay_protocol import StatusEffectServerCommand

    released = getattr(combat, "mortis_released_actor_ids", set())
    if not released:
        return batch
    frozen = freeze_effect_ids(str(combat.encounter.static_database_path))
    commands = tuple(
        replace(command, value=replace(
            command.value,
            entries=tuple(e for e in command.value.entries if e.effect_id not in frozen),
        ))
        if isinstance(command, StatusEffectServerCommand) and command.actor_id in released
        else command
        for command in batch.commands
    )
    return replace(batch, commands=commands)
