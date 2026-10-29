"""Materialize newly enabled density areas using map-script monster selectors."""
from __future__ import annotations

import random
import sqlite3

from dso_server.gameplay.local_map_state import (
    load_local_npc_definitions,
    map_database_path,
)
from dso_server.gameplay.boss_encounter_policy import density_spawn_templates
from dso_server.gameplay.fractured_planes_layout import is_tower_layout
from dso_server.gameplay.local_monster_state import (
    _load_spawn_areas, _grounded_spawn_area_candidates,
    _grounded_spawn_position, _area_template_pool,
    _load_runtime_definition,
)
from dso_server.gameplay.map_event_state import GatedSpawnRow, map_key


# Stonekeep's mill script uses five overlapping SpawnAreas for the sequence.
# The final champion area is the authored Demonic Miller encounter, but its
# SpawnListId is only ``special_spawn`` (the spawn-list contents are not
# present in static.db4), so the generic fallback would select a normal mob.
_ORSHA_MACHINE_BABY_TEMPLATES = (
    "pw212_normal_mechgunner_01_q_orsha",
    "pw212_normal_tank_01_q_orsha",
    "pw212_normal_junk_minion_01_q_orsha",
    "pw212_normal_irondwarf_01_q_orsha",
    "pw212_normal_buzzsaw_01_q_orsha",
)

_EVENT_AREA_TEMPLATE_OVERRIDES = {
    # Only area 21 is the final Demonic Miller encounter. Event set 3460 also
    # contains the five overlapping mill-wave areas; applying this override
    # to the event set as a whole spawned one boss in every wave location.
    ("pw007_stonekeep_01_dun", 3460, 21): (
        "pw007_01_leader_undead_mage_heroic",
    ),
    # The PW017 seal's main wave is authored as ``group_seal_01_wave_main``.
    # Without the retail spawn-list registry, the generic name matcher picks
    # the invisible ``pw017_seal_01`` controller (it is also a Rank_monster
    # template), leaving it alive after every visible wave mob is killed and
    # blocking the quest's ClearRegion task. Use that task's actual wave mobs.
    ("pw017_dragonslair_01_dun", 3506): (
        "pw017_01_normal_dragon_berserker_02",
        "pw017_01_normal_dragon_brood_warrior_02",
        "pw017_01_normal_dragon_broodling_02",
        "pw017_01_normal_minion_fire_02",
    ),
    ("pw017_dragonslair_01_dun", 3509): (
        "pw017_01_normal_dragon_berserker_02",
        "pw017_01_normal_dragon_brood_warrior_02",
        "pw017_01_normal_dragon_broodling_02",
        "pw017_01_normal_minion_fire_02",
    ),
    # Orsha's event has five broad, map-wide list_orsha_01 areas, but the
    # retail list membership is absent from the client export. The active
    # quest's ClearRegion names these five Machine Babies; the ordinary map
    # pool can otherwise select unrelated actors such as Target Crystals.
    ("pw212_elysium_dun", 4182): _ORSHA_MACHINE_BABY_TEMPLATES,
    # Hide and Seek (3/3): the only spawn source of event set 4088
    # (pw208_hide_and_seek_03_spawn_enemies, started by the quest's server
    # signal while its clear-01 task is not done) is a list_q_hide_and_seek
    # area whose membership is not exported. The quest's ClearRegion names
    # exactly these three templates; the map pool would spawn none of them.
    ("pw208_lost_woods_dun_04", 4088): (
        "pw208_normal_shadow_corpser_01_q_hide_and_seek_03",
        "pw208_normal_shadow_critter_01_q_hide_and_seek_03",
        "pw208_normal_shadow_caster_01_q_hide_and_seek_03",
    ),
}

_ORSHA_MACHINE_BABY_OFFSETS = (
    (-4.0, -2.0),
    (-2.0, 2.0),
    (2.0, -2.0),
    (4.0, 2.0),
    (0.0, 4.0),
)


def _orsha_machine_baby_position(runtime, encounter, area):
    """Use a grounded point around Orsha instead of the map-wide areas."""

    definitions = load_local_npc_definitions(
        encounter.static_database_path, runtime.map_id,
        include_inactive_event_sets=True,
    )
    orsha = next((
        definition
        for definition in definitions
        if str(definition.template_id).casefold()
        == "pw212_quest_elysium_hero_02"
    ), None)
    if orsha is None:
        return ()

    offset_index = (int(area.row_id) - 1) % len(_ORSHA_MACHINE_BABY_OFFSETS)
    dx, dz = _ORSHA_MACHINE_BABY_OFFSETS[offset_index]
    center = tuple(float(value) for value in orsha.position)
    candidate = (center[0] + dx, center[1], center[2] + dz)
    try:
        position = _grounded_spawn_position(
            encounter.navigation,
            encounter.terrain,
            candidate,
            reference_position=center,
            max_horizontal_distance=6.0,
        )
    except (ValueError, RuntimeError):
        return ()
    return (position,)


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
    overrides = {
        area.row_id: (
            _EVENT_AREA_TEMPLATE_OVERRIDES.get(
                (map_key(runtime.map_id), area.event_set_row, area.row_id),
            )
            or _EVENT_AREA_TEMPLATE_OVERRIDES.get(
                (map_key(runtime.map_id), area.event_set_row),
            )
        )
        for area in areas
    }
    # Reserve the shared mill arena for its boss before placing the regular
    # waves, which use overlapping authored volumes and the same candidate
    # positions.
    areas = tuple(sorted(
        areas,
        key=lambda area: (
            not bool(overrides.get(area.row_id)), area.row_id,
        ),
    ))
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
        override = overrides.get(area.row_id)
        if override:
            if (
                map_key(runtime.map_id) == "pw212_elysium_dun"
                and int(area.event_set_row) == 4182
            ):
                template = _ORSHA_MACHINE_BABY_TEMPLATES[
                    (int(area.row_id) - 1)
                    % len(_ORSHA_MACHINE_BABY_TEMPLATES)
                ]
                candidates = (template,)
            else:
                candidates = override
            pool = tuple(
                template for template in candidates
                if _load_runtime_definition(
                    encounter.static_database_path, template, 0,
                ) is not None
            )
        else:
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
        if not override:
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
        if (
            map_key(runtime.map_id) == "pw212_elysium_dun"
            and int(area.event_set_row) == 4182
        ):
            positions = _orsha_machine_baby_position(
                runtime, encounter, area,
            )
        else:
            positions = _grounded_spawn_area_candidates(
                encounter.navigation, encounter.terrain, area,
                area.candidate_positions(),
            )
        if override:
            positions = positions[:1]
        for position in positions:
            if any((position[0]-p[0])**2 + (position[2]-p[2])**2 < 1.0 for p in occupied):
                continue
            occupied.append(position)
            result.append(GatedSpawnRow(-area.row_id, area.event_set_row,
                                       random.choice(pool), position))
    return tuple(result)
