"""Monsters summoning monsters (bosses' adds).

A monster's summon skill (gameplay/local_monster_state.py
MonsterAttackDefinition.summon_template) lands in the combat tick, which only
queues it (LocalCombatState.pending_monster_summons) and raises the
``monster_summons`` presence event. Here, with the server at hand, the
summoned monsters are built like an event's spawns (services/map_events.py
_spawn_event_monsters): placed in a ring around their caster on walkable
ground, at the caster's level and the map's difficulty, registered with the
combat state, woken at once, counted against the caster's SummonMonsterLimit,
and announced to every player in that combat.
"""

from __future__ import annotations

import math

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.gameplay.local_monster_state import (
    _grounded_spawn_position,
    _load_monster,
    _load_runtime_definition,
    _spawn_group,
)
from dso_server.gameplay.map_collision_terrain import CollisionTerrainError
from dso_server.gameplay.pathengine_navigation import NavigationError

# The summoned stand around their caster, this far away (metres).
SUMMON_RING_RADIUS = 2.5


class MonsterSummonService:

    def _spawn_monster_summons(self, session: Session, requests) -> None:
        combat = session.gameplay_combat_state
        if combat is None or not requests:
            return
        encounter = combat.encounter
        database_path = encounter.static_database_path
        difficulty = int(getattr(encounter, "scaling_difficulty", 0))
        for request in requests:
            with combat.lock:
                if request.caster_actor_id not in combat.living_actor_ids:
                    continue
                alive = combat.monster_summoned_actor_ids.get(
                    request.caster_actor_id, set()
                ) & set(combat.living_actor_ids)
                room = request.amount
                if request.limit > 0:
                    room = min(room, request.limit - len(alive))
                if room <= 0:
                    continue
                actor_ids = combat.allocate_runtime_actor_ids(room)
            monsters = []
            definitions = []
            for index, actor_id in enumerate(actor_ids):
                angle = math.tau * index / max(1, len(actor_ids))
                centre = request.position
                position = (
                    centre[0] + math.cos(angle) * SUMMON_RING_RADIUS,
                    centre[1],
                    centre[2] + math.sin(angle) * SUMMON_RING_RADIUS,
                )
                if index < len(request.positions):
                    # Placed where its Minion instruction puts it.
                    position = centre = request.positions[index]
                try:
                    position = _grounded_spawn_position(
                        encounter.navigation,
                        encounter.terrain,
                        position,
                        reference_position=centre,
                        max_horizontal_distance=SUMMON_RING_RADIUS,
                    )
                except (NavigationError, CollisionTerrainError, ValueError):
                    position = tuple(float(value) for value in centre)
                definition = _load_runtime_definition(
                    database_path, request.template_id, actor_id
                )
                if definition is None:
                    print(f"[{GAMEPLAY_SERVICE_NAME}] summon {request.template_id!r} "
                          "has no runtime template; skipped")
                    break
                try:
                    monster = _load_monster(
                        database_path,
                        request.template_id,
                        actor_id,
                        position,
                        monster_level=request.level,
                        scaling_difficulty=difficulty,
                    )
                except ValueError as exc:
                    print(f"[{GAMEPLAY_SERVICE_NAME}] cannot summon "
                          f"{request.template_id!r}: {exc}")
                    break
                monsters.append(monster)
                definitions.append(definition)
            if not monsters:
                continue
            group = _spawn_group(database_path, tuple(monsters))
            ammo = combat.load_ammo_vulnerabilities(
                database_path, group.spawn_batch.commands
            )
            with combat.lock:
                added = combat.add_runtime_monsters(
                    group, definitions, ammo_vulnerabilities=ammo
                )
                combat.monster_summoned_actor_ids.setdefault(
                    request.caster_actor_id, set()
                ).update(int(actor_id) for actor_id in added)
                # Summoned into a fight: they join it at once.
                combat.activate_monsters(tuple(added))
            print(f"[{GAMEPLAY_SERVICE_NAME}] monster {request.caster_actor_id} "
                  f"summoned {len(added)} x {request.template_id}")
            viewers = tuple(
                viewer for viewer in self._active_gameplay_sessions()
                if viewer.gameplay_combat_state is combat
            ) or (session,)
            for viewer in viewers:
                # Announced even before the viewer's next move: fresh actors
                # the vicinity has not offered yet (services/map_events.py).
                self._announce_runtime_group(
                    viewer, combat, group, force_actor_ids=added
                )
