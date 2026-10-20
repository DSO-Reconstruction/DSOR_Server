"""Spawn and publish hostile monsters summoned by another monster."""

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
                alive = (
                    combat.monster_summoned_actor_ids.get(
                        request.caster_actor_id, set()
                    )
                    & set(combat.living_actor_ids)
                )
                amount = max(0, int(request.amount))
                limit = max(0, int(request.limit))
                if limit > 0:
                    amount = min(amount, max(0, limit - len(alive)))
                if amount <= 0:
                    continue
                actor_ids = combat.allocate_runtime_actor_ids(amount)

            monsters = []
            definitions = []
            centre = tuple(float(value) for value in request.position[:3])
            for index, actor_id in enumerate(actor_ids):
                angle = math.tau * index / max(1, len(actor_ids))
                candidate = (
                    centre[0] + math.cos(angle) * SUMMON_RING_RADIUS,
                    centre[1],
                    centre[2] + math.sin(angle) * SUMMON_RING_RADIUS,
                )
                try:
                    position = _grounded_spawn_position(
                        encounter.navigation,
                        encounter.terrain,
                        candidate,
                        reference_position=centre,
                        max_horizontal_distance=SUMMON_RING_RADIUS,
                    )
                except (NavigationError, CollisionTerrainError, ValueError):
                    position = centre

                definition = _load_runtime_definition(
                    database_path, request.template_id, actor_id
                )
                if definition is None:
                    print(
                        f"[{GAMEPLAY_SERVICE_NAME}] summon "
                        f"{request.template_id!r} has no runtime template"
                    )
                    break
                try:
                    monster = _load_monster(
                        database_path,
                        request.template_id,
                        actor_id,
                        position,
                        monster_level=max(1, int(request.level)),
                        scaling_difficulty=difficulty,
                    )
                except ValueError as exc:
                    print(
                        f"[{GAMEPLAY_SERVICE_NAME}] cannot summon "
                        f"{request.template_id!r}: {exc}"
                    )
                    break
                monsters.append(monster)
                definitions.append(definition)

            if not monsters:
                continue
            group = _spawn_group(database_path, tuple(monsters))
            ammo_vulnerabilities = combat.load_ammo_vulnerabilities(
                database_path, group.spawn_batch.commands
            )
            with combat.lock:
                added = combat.add_runtime_monsters(
                    group,
                    definitions,
                    ammo_vulnerabilities=ammo_vulnerabilities,
                )
                if not added:
                    continue
                combat.monster_summoned_actor_ids.setdefault(
                    int(request.caster_actor_id), set()
                ).update(int(actor_id) for actor_id in added)
                combat.activate_monsters(tuple(added))

            viewers = self._active_gameplay_sessions(
                map_id=session.gameplay_map,
                instance_of=session,
            ) or (session,)
            for viewer in viewers:
                if viewer.gameplay_combat_state is combat:
                    self._announce_runtime_group(
                        viewer,
                        combat,
                        group,
                        force_actor_ids=added,
                    )
