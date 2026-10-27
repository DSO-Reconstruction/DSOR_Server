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

A template with a positive ``_Template_Monster.LifeTime`` (Khalys's bullet
portals 3.9 s, illusions 24 s, Arachna's spiders 45 s) is temporary: its
deadline is pinned at creation and ``MonsterSummonLifecycle`` retires it
without a kill transaction (no XP, loot or quest credit) and discards it for
every viewer. Its caster's death, leash reset or return home retires it
early; ``LifeTime`` 0 summons keep their previous permanent behaviour.
"""

from __future__ import annotations

import math
import time
from contextlib import nullcontext
from dataclasses import replace

from dso_server.gameplay.local_combat_state import _monster_template_lifetime
from dso_server.protocol.gameplay_protocol import encode_discard_monster_batch

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


class MonsterSummonLifecycle:
    """One shared-combat watcher, independent of the current tick owner.

    Model retirement happens under combat.lock. Discard publication has its
    own retry ledger: a socket failure cannot revive an expired actor or make
    another viewer miss the transition. Timer identity guards stale callbacks.
    """

    def __init__(self, server, combat, runtime, scheduler):
        self.server, self.combat = server, combat
        self.runtime, self.scheduler = runtime, scheduler
        self.timer = None
        self.pending = {}
        self.running = False

    def viewers(self):
        return tuple(viewer for viewer in self.server._active_gameplay_sessions()
                     if viewer.gameplay_combat_state is self.combat
                     and getattr(viewer, "connected", True)
                     and not getattr(viewer, "gameplay_presence_departed", False)
                     and (self.runtime is None or getattr(
                         viewer, "gameplay_map_event_runtime", self.runtime) is self.runtime))

    def schedule(self):
        if self.scheduler is None:
            return
        with self.combat.lock:
            if self.timer is not None or not (
                self.combat.temporary_monster_summons
                or self.combat.pending_monster_summon_discards or self.pending
            ):
                return
            deadlines = [r.expires_at for r in self.combat.temporary_monster_summons.values()]
            delay = min(0.25, max(0.001, min(deadlines) - time.perf_counter())) if deadlines else 0.25
            timer = self.scheduler.timer(delay, lambda: self._wake(timer))
            self.timer = timer
        timer.start()

    def _wake(self, timer):
        with self.combat.lock:
            if self.timer is not timer:
                return
            self.timer = None
        self.advance()

    def advance(self, *, now=None):
        current_time = time.perf_counter() if now is None else float(now)
        with self.combat.lock:
            if self.running:
                return
            self.running = True
        try:
            viewers = self.viewers()
            runtime_live = getattr(self.server, "_map_event_runtime_is_live", None)
            live = self.runtime is None or runtime_live is None or runtime_live(self.runtime)
            with self.combat.lock:
                self.combat.expire_monster_summons(now=current_time, retire_all=not live or not viewers)
                retired = set(self.combat.pending_monster_summon_discards)
                self.combat.pending_monster_summon_discards.clear()
                current = {id(viewer): viewer for viewer in viewers}
                self.pending = {key: value for key, value in self.pending.items() if key in current}
                for key, viewer in current.items():
                    if retired:
                        self.pending.setdefault(key, (viewer, set()))[1].update(retired)
                deliveries = tuple((key, viewer, set(actors)) for key, (viewer, actors) in self.pending.items())
            for key, viewer, actors in deliveries:
                try:
                    self._discard(viewer, actors)
                except OSError:
                    # The authoritative retirement already committed. Retry
                    # this viewer on the next map-owned deadline.
                    continue
                with self.combat.lock:
                    pending = self.pending.get(key)
                    if pending is not None:
                        pending[1].difference_update(actors)
                        if not pending[1]:
                            self.pending.pop(key, None)
        finally:
            with self.combat.lock:
                self.running = False
            self.schedule()

    def _discard(self, viewer, actors):
        # Transfers share this gate; never publish an old instance's actor id
        # into the new map. No combat lock is held while entering it.
        gate = getattr(viewer, "gameplay_mutation_lock", None)
        if gate is not None and not gate.acquire(blocking=False):
            raise OSError("viewer mutation in progress")
        try:
            with getattr(viewer, "flow_lock", nullcontext()):
                if viewer.gameplay_combat_state is not self.combat:
                    return
                spawned = getattr(viewer, "gameplay_spawned_monster_actor_ids", set())
                discard = tuple(sorted(actors & spawned))
            if discard:
                viewer.send_reliable_ordered(encode_discard_monster_batch(discard), track=True,
                                             label="monster-summon-expired")
            with getattr(viewer, "flow_lock", nullcontext()):
                for name in ("gameplay_spawned_monster_actor_ids", "gameplay_new_monster_sent_actor_ids",
                             "gameplay_announced_monster_actor_ids", "gameplay_requested_actor_ids"):
                    getattr(viewer, name, set()).difference_update(actors)
                positions = getattr(viewer, "gameplay_delivered_monster_positions", {})
                for actor_id in actors:
                    positions.pop(actor_id, None)
        finally:
            if gate is not None:
                gate.release()


def cleanup_monster_summons(server, session, monsters=()):
    """The normal kill callback retires finite children, without extra kills."""
    combat = getattr(session, "gameplay_combat_state", None)
    # Combat kills carry MonsterCombatant rows. Scripted/quest notifications
    # may carry only a template id; they own no summon state to clean up.
    actor_ids = tuple(
        int(actor_id) for actor_id in (
            getattr(monster, "actor_id", None) for monster in monsters
        ) if actor_id is not None
    )
    if combat is None or not actor_ids:
        return
    with combat.lock:
        for actor_id in actor_ids:
            combat.invalidate_monster_summons(actor_id)
        lifecycle = combat.monster_summon_lifecycle
    if lifecycle is not None:
        lifecycle.advance()


class MonsterSummonService:

    def _spawn_monster_summons(self, session: Session, requests) -> None:
        combat = session.gameplay_combat_state
        if combat is None or not requests:
            return
        encounter = combat.encounter
        database_path = encounter.static_database_path
        difficulty = int(getattr(encounter, "scaling_difficulty", 0))
        for request in requests:
            lifetime = _monster_template_lifetime(str(database_path), request.template_id)
            if lifetime < 0:
                continue
            with combat.lock:
                if not combat.monster_summon_request_is_current(request):
                    continue
                # Legacy callers may omit the incarnation. Pin it before
                # loading static data so reset/death during preparation wins.
                request = replace(request, caster_generation=combat._monster_summon_generations.get(request.caster_actor_id, 0))
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
                if not combat.monster_summon_request_is_current(request):
                    continue
                if request.limit > 0:
                    alive = combat.monster_summoned_actor_ids.get(request.caster_actor_id, set()) & set(combat.living_actor_ids)
                    # Another cast may have committed while templates loaded.
                    room = max(0, request.limit - len(alive))
                    if room < len(monsters):
                        # Rebuilding the packet requires I/O. Reject this
                        # stale reservation rather than overfill the limit.
                        continue
                added = combat.add_runtime_monsters(
                    group, definitions, ammo_vulnerabilities=ammo
                )
                combat.monster_summoned_actor_ids.setdefault(
                    request.caster_actor_id, set()
                ).update(int(actor_id) for actor_id in added)
                # Summoned into a fight: they join it at once.
                combat.activate_monsters(tuple(added))
                combat.register_temporary_monster_summons(
                    request.caster_actor_id, added, lifetime=lifetime, now=time.perf_counter(),
                )
                if lifetime > 0 and combat.monster_summon_lifecycle is None:
                    runtime = getattr(session, "gameplay_map_event_runtime", None)
                    scheduler = getattr(runtime, "scheduler", None) or getattr(session, "gameplay_deadline_scheduler", None)
                    combat.monster_summon_lifecycle = MonsterSummonLifecycle(self, combat, runtime, scheduler)
                lifecycle = combat.monster_summon_lifecycle
            if lifecycle is not None:
                lifecycle.schedule()
            print(f"[{GAMEPLAY_SERVICE_NAME}] monster {request.caster_actor_id} "
                  f"summoned {len(added)} x {request.template_id}")
            viewers = tuple(
                viewer for viewer in self._active_gameplay_sessions()
                if viewer.gameplay_combat_state is combat
            ) or (session,)
            for viewer in viewers:
                # Announced even before the viewer's next move: fresh actors
                # the vicinity has not offered yet (services/map_events.py).
                try:
                    self._announce_runtime_group(viewer, combat, group, force_actor_ids=added)
                except OSError:
                    # Other viewers and expiry must not depend on this socket.
                    continue
