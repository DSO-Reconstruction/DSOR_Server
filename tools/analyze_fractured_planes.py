"""Export the complete tower placement inventory and a browser map atlas.

Run from the repository: python tools/analyze_fractured_planes.py
Only reads client assets; reports go under data/diagnostics/fractured_planes.
"""
from __future__ import annotations

import argparse
from collections import Counter
import html
import importlib.util
import json
import math
from pathlib import Path
import sqlite3
import struct
import sys
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "dso_server", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)

from dso_server.gameplay.fractured_planes_layout import (
    is_tower_mechanism, tower_actor_role, tower_density_templates,
)
from dso_server.gameplay.local_map_state import (
    _enabled_event_set_ids, active_local_map_event_flags,
)
from dso_server.gameplay.local_monster_state import (
    _load_dynamic_monster_encounter, _load_spawn_areas,
    _navigation_for_map, _terrain_for_map, _load_runtime_definition,
    load_local_player_only_encounter,
)
from dso_server.gameplay.map_difficulty_state import resolve_map_access
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.map_event_state import (
    MapEventRuntime, load_map_event_content, load_map_signal_catalog,
)
from dso_server.protocol.gameplay_protocol import NewMonsterServerCommand
from dso_server.services.map_events import (
    MapEventService, _fractured_planes_floor_one_regions,
    _fractured_planes_floor_regions, _fractured_planes_room_event_rows,
)


def transform(blob):
    matrix = struct.unpack("<16f", bytes(blob))
    return tuple(round(v, 5) for v in matrix[12:15]), math.atan2(matrix[8], matrix[0]), matrix


def inventory(path, static):
    result = {"map": path.stem, "layers": {}, "pools": {}}
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        for table in ("SpawnPoint", "SpawnArea", "LocationEffect", "Trap", "StateObject", "Entry", "Exit", "TriggerVolume", "Sequence"):
            rows = []
            for row in db.execute("SELECT rowid, * FROM _Instance_" + table):
                position, yaw, matrix = transform(row["Transform"])
                fields = {k: v for k, v in dict(row).items() if k != "Transform" and not isinstance(v, bytes)}
                fields.update(position=position, yaw=yaw, matrix=matrix)
                if table == "SpawnPoint":
                    fields["role"] = tower_actor_role(static, row["NpcId"])
                    definition = _load_runtime_definition(
                        static, row['NpcId'], 0,
                        stationary=fields['role'] != 'monster',
                    )
                    fields['runtime_skills'] = attack_rows(definition) if definition else []
                rows.append(fields)
            result["layers"][table] = rows
        for row in result["layers"]["SpawnArea"]:
            name = row["SpawnListId"]
            result["pools"][name] = tower_density_templates(static, name)
        result["event_sets"] = [dict(r) for r in db.execute("SELECT * FROM _Instance_EventSets")]
    with sqlite3.connect(static) as db:
        db.row_factory = sqlite3.Row
        for row in result['layers']['SpawnPoint']:
            profile = db.execute(
                'SELECT Graphics, CharacterSet, BehaviorType, PermanentStatusEffect '
                'FROM _Template_Monster WHERE Id=?', (row['NpcId'],)
            ).fetchone()
            row['profile'] = dict(profile) if profile else {}
            row['auras'] = []
            for effect in str(row['profile'].get('PermanentStatusEffect', '')).split(';'):
                aura = db.execute(
                    "SELECT * FROM _Template_StatusEffect WHERE Id=? AND TRIM(AuraEffectId)<>''",
                    (effect,),
                ).fetchone()
                if aura:
                    row['auras'].append(dict(aura))
    nav = _navigation_for_map(static, path.stem)
    # Exact authored walkable triangles provide the map outline in the atlas.
    result["navigation"] = [
        [(nav.vertices[i][0], nav.vertices[i][2]) for i in tri.vertices]
        for tri in getattr(nav, "triangles", ())
    ]
    terrain = _terrain_for_map(static, path.stem)
    result["collision_triangles"] = terrain.triangle_count if terrain else 0
    return result


def attack_rows(runtime):
    return [{
        "id": a.resource_id, "cast_range": a.attack_range,
        "hit_range": a.hit_range, "velocity": a.projectile_velocity,
        "lifetime": a.projectile_lifetime, "motion": a.motion_type,
        "straight_reach": a.projectile_velocity * a.projectile_lifetime,
    } for a in runtime.attacks]


def actor_rows(encounter, static):
    if encounter is None:
        return []
    definitions = {r.actor_id: r for r in encounter.runtime_definitions}
    result = []
    for command in encounter.spawn_batch.commands:
        if not isinstance(command, NewMonsterServerCommand):
            continue
        monster = command.value
        runtime = definitions[monster.actor_id]
        result.append({
            "actor": monster.actor_id, "template": monster.template_id,
            "position": monster.position, "yaw": monster.field_a0,
            "role": tower_actor_role(static, monster.template_id),
            "speed": runtime.run_speed,
            "skills": attack_rows(runtime),
        })
    return result


def audit_event_mechanisms(static, layout):
    """Exercise the actual event-spawn path without sessions or networking."""
    name = layout['map']
    sources = [r for r in layout['layers']['SpawnPoint']
               if r['role'] == 'mechanism' and not r.get('AlliedToPlayer')]
    if not sources:
        return []
    encounter = _load_dynamic_monster_encounter(
        static, name, include_spawn_areas=False, monster_level=145,
    ) or load_local_player_only_encounter(static, name)
    state = LocalCombatState(encounter, player_actor_id=65536, player_health=10000)
    runtime = MapEventRuntime(
        load_map_event_content(static, name), load_map_signal_catalog(static),
        ('offline-layout-audit', name), frozenset(), monster_level=145,
    )
    session = SimpleNamespace(
        gameplay_combat_state=state, flow_lock=threading.RLock(),
        gameplay_presence_ready=False,
    )
    service = MapEventService()
    service.static_database_path = static
    service._spawn_event_monsters(
        runtime, (session,), {r['EventSetRow'] for r in sources if r['EventSetRow'] >= 0},
    )
    errors = []
    for row in sources:
        matches = state.find_monsters(row['NpcId'], row['position'], radius=.001)
        if not matches:
            errors.append(f'{name}: event-spawn lost mechanism row {row["rowid"]}: {row["NpcId"]}')
        for actor in matches:
            if abs(state.monster_yaws[actor]-row['yaw']) > .0001:
                errors.append(f'{name}: event-spawn lost mechanism yaw {row["NpcId"]}')
            if state.monsters[actor].run_speed:
                errors.append(f'{name}: event-spawn mechanism can walk {row["NpcId"]}')
            if row['runtime_skills']:
                state.activate_monsters((actor,))
                state.pending_monster_attacks.pop(actor, None)
                state.monster_skill_unblock_at.pop(actor, None)
                x,y,z = row['position']
                attacks = state.maybe_start_monster_attacks(
                    encounter.coordinate_frame.world_to_wire((x+3,y,z)),
                    server_tick=3000, now=time.perf_counter()+120,
                    eligible_actor_ids={actor},
                )
                if not attacks:
                    errors.append(f'{name}: mounted shooter cannot fire in range: {row["NpcId"]}')
    return errors


def audit_room_clears(static, state, flags):
    """Living mounts/controllers must not block a room whose mobs are dead."""
    name = state.encounter.map_id
    if not name.startswith('h0009_endlesstower_01_sm_'):
        return []
    runtime = MapEventRuntime(
        load_map_event_content(static, name), load_map_signal_catalog(static),
        ('offline-clear-audit', name), flags,
    )
    for actor, monster in state.monsters.items():
        if not is_tower_mechanism(static, name, monster.template_id):
            state.monster_health[actor] = 0
    emitted = set()
    service = MapEventService()
    service.static_database_path = static
    service._emit_map_signal = lambda session, runtime, signal: emitted.add(signal)
    service._map_event_sessions = lambda runtime: ()
    # Exercise a reused physical layout with a different logical floor ID.
    session = SimpleNamespace(
        gameplay_combat_state=state, gameplay_map_event_runtime=runtime,
        gameplay_fractured_planes_run=True,
        gameplay_map_access_id='h0009_endlesstower_floor_011',
    )
    service._update_fractured_planes_clear_state(session, ())
    regions = (
        _fractured_planes_floor_one_regions(str(static)) if name.endswith('_01')
        else _fractured_planes_floor_regions(
            str(static), name, _fractured_planes_room_event_rows(runtime),
            tuple((p.instance_id, tuple(p.position)) for p in runtime.content.props),
        )
    )
    return [
        f'{name}: living mechanism blocked cleared room {signal}'
        for signal, volumes in regions
        if signal not in emitted and any(
            not is_tower_mechanism(static, name, m.template_id)
            and any(v.contains((m.position[0],v.matrix[13],m.position[2])) for v in volumes)
            for m in state.monsters.values()
        )
    ]


def atlas(report):
    parts = ['<!doctype html><meta charset="utf-8"><title>Fractured Planes placement atlas</title>',
        '<style>body{background:#111820;color:#e5edf6;font:15px system-ui;margin:24px}article{margin:28px 0;padding:20px;background:#1b2531;border-radius:12px}svg{background:#0b1017;width:100%;max-height:850px}table{border-collapse:collapse}td,th{padding:6px 12px;border-bottom:1px solid #405065}summary{cursor:pointer}a{color:#90caff}</style>',
        '<h1>Fractured Planes placement atlas</h1><p>World coordinates: X horizontally, Z vertically. Hover over an object for template, event set and XYZ. All 100 floor routes are read from static.db4.</p>',
        '<p>Gray: navigation. Green: normal monsters. Yellow squares: authored mechanisms. Purple: intentionally invisible scene controllers. Red outlines: location effects. Blue diamonds: platforms / props.</p>',
        '<p>Density lists are reconstructed from visible tower-specific templates; the client assets do not ship the server SpawnList registry.</p>']
    for name, layout in report["maps"].items():
        nodes = [r["position"] for layer in layout["layers"].values() for r in layer]
        if not nodes:
            continue
        xs, zs = [p[0] for p in nodes], [p[2] for p in nodes]
        lo_x, lo_z, width, height = min(xs)-10, min(zs)-10, max(xs)-min(xs)+20, max(zs)-min(zs)+20
        parts.append(f'<article id="{name}"><h2>{name}</h2><p>Floors: {layout.get("floors", [])}; collision triangles: {layout["collision_triangles"]}</p>')
        if layout.get('floors'):
            options = ''.join(f'<option value="{i}">Floor {i}</option>' for i in layout['floors'])
            parts.append('<label>Population: <select onchange="this.closest(\'article\').querySelectorAll(\'.population\').forEach(g=>g.style.display=g.dataset.floor===this.value?\'\':\'none\')">'+options+'</select></label>')
        parts.append(f'<svg viewBox="{lo_x} {lo_z} {width} {height}" role="img" aria-label="{name}">')
        path = ' '.join('M'+' L'.join(f'{p[0]:.2f},{p[1]:.2f}' for p in triangle)+' Z' for triangle in layout["navigation"])
        parts.append(f'<path d="{path}" fill="#2c3944" stroke="#374651" stroke-width="0.12"/>')
        for r in layout["layers"]["LocationEffect"]:
            m = r["matrix"]
            corners = [(m[12]+x*m[0]+z*m[8],m[14]+x*m[2]+z*m[10]) for x,z in ((-.5,-.5),(.5,-.5),(.5,.5),(-.5,.5))]
            points = ' '.join(f'{x:.2f},{z:.2f}' for x,z in corners)
            title = html.escape(f'{r.get("StatusEffectId")} XYZ={r["position"]} set={r.get("EventSetRow")}')
            parts.append(f'<polygon points="{points}" fill="#ff504410" stroke="#ff7060" stroke-width=".35"><title>{title}</title></polygon>')
        for floor in report["floors"]:
            if floor["map"] != name:
                continue
            display = '' if floor['floor'] == layout['floors'][0] else 'display:none'
            parts.append(f'<g class="population" data-floor="{floor["floor"]}" style="{display}">')
            for r in floor["actors"]:
                x,y,z = r["position"]
                color = "#c986ff" if r["role"] == "controller" else "#ffcf55" if r["role"] == "mechanism" else "#64e59b"
                title = html.escape(f'{r["template"]} XYZ=({x:.2f},{y:.2f},{z:.2f}) floor={floor["floor"]}')
                parts.append(f'<circle cx="{x}" cy="{z}" r=".7" fill="{color}"><title>{title}</title></circle>')
            parts.append('</g>')
        for layer, color in (("SpawnPoint", "#ffcf55"), ("StateObject", "#70bfff")):
            for r in layout["layers"][layer]:
                if layer == "SpawnPoint" and r["role"] == "monster":
                    continue
                x,y,z = r["position"]
                title = html.escape(f'{r.get("NpcId",r.get("_ID"))} XYZ={r["position"]} yaw={r["yaw"]:.3f} set={r.get("EventSetRow")}')
                parts.append(f'<rect x="{x-.9}" y="{z-.9}" width="1.8" height="1.8" fill="{color}"><title>{title}</title></rect>')
                dx,dz = -math.sin(r["yaw"])*3,-math.cos(r["yaw"])*3
                parts.append(f'<path d="M{x},{z} l{dx},{dz}" stroke="{color}" stroke-width=".4"/>')
                for aura in r.get('auras', ()):
                    radius = float(aura.get('AuraRadius') or 0)
                    if radius:
                        title = html.escape(f'{aura["Id"]}: shape={aura["AuraShape"]}; radius={radius}; rotation={aura["AuraRotationSpeed"]} degrees/sec')
                        parts.append(f'<circle cx="{x}" cy="{z}" r="{radius}" fill="none" stroke="#ffcf55" stroke-dasharray="1,1" stroke-width=".35"><title>{title}</title></circle>')
        parts.append('</svg><details><summary>Authored fixed actor coordinates</summary><table><tr><th>Template</th><th>XYZ</th><th>Yaw</th><th>Event set</th></tr>')
        for r in layout["layers"]["SpawnPoint"]:
            parts.append(f'<tr><td>{html.escape(str(r.get("NpcId")))}</td><td>{r["position"]}</td><td>{r["yaw"]:.3f}</td><td>{r.get("EventSetRow")}</td></tr>')
        parts.append('</table></details></article>')
    return '\n'.join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'data/diagnostics/fractured_planes')
    args = parser.parse_args()
    static = ROOT / 'data/db/static.db4'
    maps = sorted((ROOT / 'data/maps').glob('h0009_endlesstower*.db4'))
    boss = ROOT / 'data/maps/fp0001_boss_01.db4'
    if boss.exists():
        maps.append(boss)
    report = {"maps": {}, "floors": [], "errors": []}
    for path in maps:
        report['maps'][path.stem] = inventory(path, static)
        report['errors'].extend(audit_event_mechanisms(static, report['maps'][path.stem]))
    cache = {}
    for floor in range(1,101):
        route = resolve_map_access(static, f'h0009_endlesstower_floor_{floor:03}', player_level=145)
        name = route.physical_map_id
        flags = route.event_set_flags | frozenset({'event_fracturedplanes'})
        with sqlite3.connect(ROOT / f'data/maps/{name}.db4') as db:
            sets = _enabled_event_set_ids(db, active_local_map_event_flags(name, flags), map_id=name)
            areas = _load_spawn_areas(db, enabled_event_sets=sets, scaling_difficulty=route.monster_scaling_difficulty)
        key = (name, tuple(sorted(sets)), tuple(a.row_id for a in areas))
        if key not in cache:
            encounter = _load_dynamic_monster_encounter(
                static, name, monster_level=route.monster_level or 145,
                scaling_difficulty=route.monster_scaling_difficulty,
                active_event_flags=flags,
            )
            cache[key] = actor_rows(encounter, static)
            if encounter:
                stationary = set()
                for actor in cache[key]:
                    if actor['role'] != 'monster':
                        stationary.add(actor['actor'])
                        if actor['speed'] != 0:
                            report['errors'].append(f'{name}: mobile mechanism {actor["template"]}')
                        authored = [r for r in report['maps'][name]['layers']['SpawnPoint']
                                    if r['NpcId'] == actor['template']
                                    and math.dist(r['position'], actor['position']) < .0001]
                        if not authored or not any(abs(r['yaw']-actor['yaw']) < .0001 for r in authored):
                            report['errors'].append(f'{name}: mechanism lost authored transform {actor}')
                        continue
                    if encounter.terrain:
                        heights = encounter.terrain.heights_at(actor['position'])
                        if not heights or min(abs(actor['position'][1]-y) for y in heights) > .15:
                            report['errors'].append(f'{name}: actor off collision ground: {actor}')
                state = LocalCombatState(encounter, player_actor_id=65536, player_health=10000)
                before = {actor:state.monster_positions[actor] for actor in stationary}
                for tick in range(1,6):
                    state.advance_monsters(
                        encounter.coordinate_frame.world_to_wire((0.,0.,0.)),
                        elapsed_seconds=.04, server_tick=tick,
                        eligible_actor_ids=stationary,
                    )
                if any(state.monster_positions[a] != p for a,p in before.items()):
                    report['errors'].append(f'{name}: movement simulation displaced mounted actor')
                for actor in cache[key]:
                    if abs(state.monster_yaws[actor['actor']]-actor['yaw']) > .0001:
                        report['errors'].append(f'{name}: runtime lost spawn rotation {actor["template"]}')
                for source in report['maps'][name]['layers']['SpawnPoint']:
                    if source['role'] != 'mechanism' or source.get('AlliedToPlayer'):
                        continue
                    if source['EventSetRow'] >= 0 and source['EventSetRow'] not in sets:
                        continue
                    if not any(a['template'] == source['NpcId'] and math.dist(a['position'],source['position']) < .0001 for a in cache[key]):
                        report['errors'].append(f'{name}: missing enabled mechanism row {source["rowid"]}')
                report['errors'].extend(audit_room_clears(static, state, flags))
        actors = cache[key]
        selected = {a.row_id: a for a in areas}
        for area in selected.values():
            pool = tower_density_templates(static, area.spawn_list_id)
            if area.spawn_list_id and not pool:
                report['errors'].append(f'{name}: empty pool {area.spawn_list_id}')
            for template in pool:
                if tower_actor_role(static, template) != 'monster':
                    report['errors'].append(f'{name}: mechanism in density pool {template}')
        row = {'floor':floor, 'map':name, 'event_sets':sorted(sets), 'selected_areas':sorted(selected), 'actors':actors, 'counts':dict(Counter(a['role'] for a in actors))}
        report['floors'].append(row)
        report['maps'][name].setdefault('floors',[]).append(floor)
        print(f'Floor {floor:03}: {name}: {len(actors)} actors; {row["counts"]}', flush=True)
    report['errors'] = sorted(set(report['errors']))
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'layout.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    (args.output/'index.html').write_text(atlas(report),encoding='utf-8')
    print(f'Analyzed 100 floors, {len(report["maps"])} physical maps, {len(cache)} placement variants; errors={len(report["errors"])}')
    for error in report['errors']:
        print(error)
    return bool(report['errors'])


if __name__ == '__main__':
    raise SystemExit(main())
