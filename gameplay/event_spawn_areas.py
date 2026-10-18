"""Materialize newly enabled density areas using map-script monster selectors."""
from __future__ import annotations

import random
import sqlite3

from dso_server.gameplay.local_map_state import map_database_path
from dso_server.gameplay.boss_encounter_policy import density_spawn_templates
from dso_server.gameplay.fractured_planes_layout import is_tower_layout
from dso_server.gameplay.local_monster_state import (
    _load_spawn_areas, _grounded_spawn_area_candidates, _area_template_pool,
    _load_runtime_definition,
)
from dso_server.gameplay.map_event_state import GatedSpawnRow, map_key


def event_area_spawn_rows(runtime, encounter, event_sets):
    wanted = frozenset(event_set for event_set in event_sets
                       if not runtime.event_set_actor_ids.get(event_set))
    already_populated = frozenset(
        getattr(encounter, "prematerialized_spawn_area_rows", frozenset())
    )
    with sqlite3.connect(map_database_path(encounter.static_database_path, runtime.map_id)) as db:
        areas = tuple(
            area for area in _load_spawn_areas(
                db, enabled_event_sets=wanted,
                scaling_difficulty=runtime.scaling_difficulty,
            )
            if area.event_set_row in wanted
            and area.row_id not in already_populated
        )
    if not areas:
        return ()
    # The shipped client has SpawnListId references but no server spawn-list
    # table. Scripted clear conditions supply authoritative eligible templates
    # for the running wave; only fall back to the ordinary map pool if absent.
    running = runtime.running_events()
    selectors = tuple(dict.fromkeys(
        template for rule in runtime.catalog.rules_by_event_type.get("monsterseliminated", ())
        if rule.server_evaluated and rule.events_running
        and set(rule.events_running) <= running
        and (not rule.maps or map_key(runtime.map_id) in rule.maps)
        for template in rule.params
    ))
    metadata = {}
    with sqlite3.connect(encounter.static_database_path) as db:
        for template in density_spawn_templates(encounter.static_database_path, selectors):
            row = db.execute("SELECT Rank FROM _Template_Monster WHERE Id=?", (template,)).fetchone()
            if row and _load_runtime_definition(encounter.static_database_path, template, 0):
                metadata[template] = str(row[0]).casefold()
    result = []
    occupied = []
    for area in areas:
        if not area.spawn_list_id.strip():
            continue
        pool = (
            _area_template_pool(encounter.static_database_path, runtime.map_id, area, ())
            if is_tower_layout(runtime.map_id)
            else tuple(metadata) or _area_template_pool(
                encounter.static_database_path, runtime.map_id, area, ())
        )
        name = area.spawn_list_id.casefold()
        # Adds/main populations exclude the bosses/guardians placed as fixed
        # points in the same script. Explicit leader/sentinel areas keep them.
        rank = ("sentinel" if "sentinel" in name and "add" not in name else
                "leader" if "leader" in name and "add" not in name else "monster")
        ranked = tuple(t for t in pool if rank in metadata.get(t, "rank_monster"))
        pool = ranked or pool
        tokens = set(name.split("_")) - {
            "random", "main", "add", "adds", "group", "wave", "normal",
            "duria", "norselands", "atlantis", "myrdosch", "lortac", "qaizah",
        }
        matched = tuple(t for t in pool if tokens <= set(t.casefold().split("_")))
        pool = matched or pool
        if not pool:
            continue
        positions = _grounded_spawn_area_candidates(
            encounter.navigation, encounter.terrain, area, area.candidate_positions())
        for position in positions:
            if any((position[0]-p[0])**2 + (position[2]-p[2])**2 < 1.0 for p in occupied):
                continue
            occupied.append(position)
            result.append(GatedSpawnRow(-area.row_id, area.event_set_row,
                                       random.choice(pool), position))
    return tuple(result)
