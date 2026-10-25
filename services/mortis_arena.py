"""Authoritative Mortis' Exile phase and incident-effect lifecycle."""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
import math
from pathlib import Path
import sqlite3
import threading
import time

from dso_server.gameplay.mortis_arena import (
    MAP_ID, DRAGON, ADD_DRAGON, MOUNTED, UNBOUND, freeze_effect_ids,
)
from dso_server.gameplay.map_event_state import GatedSpawnRow
from dso_server.gameplay.local_monster_state import load_monster_permanent_status_effects
from dso_server.protocol.gameplay_protocol import (
    MonsterUpdate, StatusEffect, encode_monster_update, encode_status_effect,
)


@lru_cache(maxsize=128)
def _lifetime(static_path, template):
    with sqlite3.connect(Path(static_path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT LifeTime, DespawnDuration FROM _Template_Monster WHERE Id=?", (template,)).fetchone()
    return tuple(max(0.0, float(v or 0.0)) for v in row) if row else (0.0, 0.0)


@lru_cache(maxsize=8)
def _healing_aura(static_path):
    with sqlite3.connect(Path(static_path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT AuraEffectAffectedTemplateId, AuraEffectId FROM _Template_StatusEffect "
            "WHERE Id='boss_mortis_unbound_white_mage_HealBuff_Effect_Aura'",
        ).fetchone()
        if row is None:
            return "", 0.0
        child = db.execute("SELECT StartModifiers FROM _Template_StatusEffect WHERE Id=?", (row[1],)).fetchone()
    for instruction in str(child[0] or "").split(";") if child else ():
        kind, _, args = instruction.partition(":")
        values = args.split(",")
        if kind.casefold() == "currhealthpoints" and len(values) >= 2 and values[1].casefold() == "relative":
            return str(row[0]).casefold(), max(0.0, float(values[0]))
    return "", 0.0


def arena(server, runtime):
    if runtime is None or runtime.map_id.casefold() != MAP_ID:
        return None
    with runtime.lock:
        controller = getattr(runtime, "mortis_arena", None)
        if controller is None:
            controller = MortisArena(server, runtime)
            runtime.mortis_arena = controller
    return controller


class MortisArena:
    def __init__(self, server, runtime):
        self.server, self.runtime = server, runtime
        self.lock = threading.RLock()
        self.phase = "waiting"
        self.started = False
        self.completed = False
        self.incidents = set()
        self.awakening = {}
        self.mounted_position = (48.6, 0.0, 21.0)
        self.serial = 0
        self.timer_pending = False
        self.mounted_death_at = None
        self.unbound_due_at = None
        self.lifetimes = {}
        self.transforms = {}
        self.waking = set()
        self.healing_members = set()

    def _context(self):
        sessions = self.server._map_event_sessions(self.runtime)
        return sessions, self.server._map_event_combat_state(sessions)

    def transition(self, transition):
        if not transition.active or self.completed:
            return
        event = transition.event.key
        if event in {"boss_start", "boss_spawn"}:
            with self.lock:
                if self.started:
                    return
                self.started = True
                self.phase = "dragon"
            self._wake(DRAGON)
            self._schedule()
        elif event == "boss_mortis_phase_02_trigger_spawn":
            self.phase = "wave"
            self._check_wave()
        elif event == "boss_mortis_mounted_spawn":
            self.phase = "mounted"
        elif event == "boss_mortis_unbound_spawn":
            self.phase = "unbound_pending"
            # The map event starts after 4 s, but the mounted actor's authored
            # Die incident delays the actual minion until its death clip ends.
            path = Path(self.server.static_database_path).resolve().as_uri()
            delay = 0.0
            with sqlite3.connect(path + "?mode=ro", uri=True) as db:
                row = db.execute("SELECT IncidentActorStatusEffects FROM _Template_Monster WHERE Id=?", (MOUNTED,)).fetchone()
                for spec in str(row[0] or "").split(";") if row else ():
                    if spec.partition(",")[0] == "Boss_Mortis_Unbound_Spawn":
                        delay = next((float(p[3:]) for p in spec.split(",") if p.startswith("DE:")), 0.0)
            self.unbound_due_at = (self.mounted_death_at or time.perf_counter()) + delay
            if getattr(self.runtime, "scheduler", None) is None:
                self._spawn_unbound()
        elif event == "boss_mortis_unbound_add_dragon_spawn":
            self._wake(ADD_DRAGON)

    def _schedule(self):
        scheduler = getattr(self.runtime, "scheduler", None)
        if scheduler is not None and not self.completed and self.server._map_event_sessions(self.runtime):
            with self.lock:
                if self.timer_pending:
                    return
                self.timer_pending = True
            scheduler.timer(0.25, self._tick).start()

    def resume(self):
        if self.started and not self.completed:
            if self.phase == "dragon":
                self._wake(DRAGON)
            self._schedule()

    def _spawn_unbound(self):
        with self.lock:
            if self.phase != "unbound_pending" or self.completed:
                return
            self.phase = "unbound"
            self.unbound_due_at = None
        x, y, z = self.mounted_position
        self._spawn(((UNBOUND, (x - 1.8, y, z + 2.5)),))

    def _tick(self):
        with self.lock:
            self.timer_pending = False
        if self.completed or not self.server._map_event_runtime_is_live(self.runtime):
            return
        try:
            sessions, combat = self._context()
            if combat is not None:
                now = time.perf_counter()
                self._expire_minions(combat, now)
                if self.unbound_due_at is not None and now >= self.unbound_due_at:
                    self._spawn_unbound()
                with combat.lock:
                    for actor, deadline in tuple(self.awakening.items()):
                        if now >= deadline:
                            if actor in combat.monsters:
                                combat.monsters[actor] = replace(combat.monsters[actor], invulnerable=False)
                            del self.awakening[actor]
                    bosses = tuple(
                        (m, combat.monster_health.get(a, 0))
                        for a, m in combat.monsters.items()
                        if m.template_id.casefold() == UNBOUND
                    )
                for boss, hp in bosses:
                    if 0 < hp < boss.maximum_health:
                        self._incident_once(boss, "damaged")
                    if 0 < hp <= boss.maximum_health / 3:
                        self._incident_once(boss, "lowhealth")
                self._heal_auras(combat, sessions, now)
                self._check_wave()
        except Exception as exc:
            print(f"[mortis-arena] tick failed: {type(exc).__name__}: {exc}")
        finally:
            self._schedule()

    def _wake(self, template):
        sessions, combat = self._context()
        if combat is None:
            return
        with combat.lock:
            actors = tuple(
                a for a, m in combat.monsters.items()
                if m.template_id.casefold() == template
                and combat.monster_health.get(a, 0) > 0
                and a not in combat.mortis_released_actor_ids
            )
        for actor in actors:
            with self.lock:
                if actor in self.waking:
                    continue
                self.waking.add(actor)
            # Publish the frozen actor before removing its lock: ending the
            # authored effect plays Resurrect / boss_05_phase_1_spawn.
            group = combat.group_for_actor(actor)
            for viewer in sessions:
                if group is not None:
                    self.server._announce_runtime_group(viewer, combat, group, force_actor_ids=(actor,))
            effects = tuple(
                effect for effect in load_monster_permanent_status_effects(
                    combat.encounter.static_database_path, template, actor,
                ) if effect.effect_id not in freeze_effect_ids(str(combat.encounter.static_database_path))
            )
            now = time.perf_counter()
            with combat.lock:
                combat.mortis_released_actor_ids.add(actor)
                combat.scripted_dormant_actor_ids.discard(actor)
                combat.monster_spawn_started_at.pop(actor, None)
                combat.begin_monster_spawn((actor,), now=now)
                ready = combat.monster_skill_unblock_at.get(actor, now)
                self.awakening[actor] = ready
                combat.activate_monsters((actor,))
            for viewer in sessions:
                if actor in viewer.gameplay_spawned_monster_actor_ids:
                    viewer.send_reliable_ordered(
                        encode_status_effect(StatusEffect(True, effects), actor_id=actor),
                        track=True, label="mortis-dragon-awakening",
                    )
            print(f"[mortis-arena] awakening {template}, actor={actor}")

    def _spawn(self, entries):
        sessions, combat = self._context()
        if combat is None or self.completed:
            return
        rows = []
        with self.lock:
            for template, position in entries:
                self.serial += 1
                rows.append(GatedSpawnRow(-2_140_000 - self.serial, -214, template, tuple(position)))
        self.server._spawn_event_monsters(self.runtime, sessions, (), extra_rows=tuple(rows))

    def _incident_once(self, monster, trigger):
        key = (int(monster.actor_id), trigger)
        with self.lock:
            if key in self.incidents or self.completed:
                return
            self.incidents.add(key)
        self._spawn_incident_minions(monster, trigger)
        sessions, _combat = self._context()
        if sessions:
            signal = (
                "boss_mortis_unbound_gate_trigger_start_signal" if trigger == "damaged"
                else "boss_mortis_unbound_add_dragon_spawn_start_signal"
            )
            self.server._emit_map_signal(sessions[0], self.runtime, signal)

    def _spawn_incident_minions(self, monster, trigger, *, origin=None):
        sessions, combat = self._context()
        if combat is None:
            return
        path = Path(combat.encounter.static_database_path).resolve().as_uri()
        entries = []
        with sqlite3.connect(path + "?mode=ro", uri=True) as db:
            row = db.execute("SELECT IncidentActorStatusEffects FROM _Template_Monster WHERE Id=?", (monster.template_id,)).fetchone()
            for spec in str(row[0] or "").split(";") if row else ():
                tokens = [p.strip() for p in spec.split(",")]
                if f"on:{trigger}" not in {p.casefold() for p in tokens[1:]}:
                    continue
                effect = db.execute("SELECT StartModifiers FROM _Template_StatusEffect WHERE Id=?", (tokens[0],)).fetchone()
                for instruction in str(effect[0] or "").split(";") if effect else ():
                    kind, _, args = instruction.partition(":")
                    if kind.casefold() != "minion":
                        continue
                    values = args.split(",")
                    if len(values) < 5:
                        continue
                    template, amount = values[0], min(16, max(0, int(values[1])))
                    dx, dy, dz = map(float, values[2:5])
                    with combat.lock:
                        x, y, z = origin if origin is not None else combat.monster_positions.get(monster.actor_id, monster.position)
                    for index in range(amount):
                        angle = math.tau * index / max(1, amount)
                        radius = 1.5 if amount > 1 else 0.0
                        entries.append((template, (x + dx + math.cos(angle) * radius, y + dy, z + dz + math.sin(angle) * radius)))
        if entries:
            self._spawn(entries)

    def spawned(self, combat, monsters):
        now = time.perf_counter()
        for monster in monsters:
            lifetime, despawn = _lifetime(str(combat.encounter.static_database_path), monster.template_id)
            if lifetime > 0:
                self.lifetimes[monster.actor_id] = (now + lifetime, despawn)

    def _expire_minions(self, combat, now):
        # Mortis' gate skeletons transform only if they survive their lifetime;
        # killed ones never advance to a ripper/mage or award another kill.
        for actor, (deadline, despawn) in tuple(self.lifetimes.items()):
            if now < deadline:
                continue
            self.lifetimes.pop(actor, None)
            with combat.lock:
                alive = combat.monster_health.get(actor, 0) > 0
                if alive:
                    monster = combat.monsters[actor]
                    position = combat.monster_positions[actor]
                    combat.retire_monsters((actor,))
            if alive:
                self.transforms[actor] = (deadline + despawn, monster, position)
        for actor, (deadline, monster, position) in tuple(self.transforms.items()):
            if now < deadline:
                continue
            self.transforms.pop(actor, None)
            sessions, _ = self._context()
            slot = -2_140_000 - actor
            with self.runtime.lock:
                self.runtime.event_set_actor_ids[slot] = {actor}
            self.server._retire_event_monsters(self.runtime, sessions, (slot,))
            self._spawn_incident_minions(monster, "despawn", origin=position)

    def _check_wave(self):
        if self.phase != "wave" or self.completed:
            return
        sessions, combat = self._context()
        if combat is None or not sessions:
            return
        with self.runtime.lock:
            actors = tuple(self.runtime.event_set_actor_ids.get(4216, ()))
        with combat.lock:
            cleared = bool(actors) and all(combat.monster_health.get(a, 0) <= 0 for a in actors)
        if cleared:
            with self.lock:
                if self.phase != "wave":
                    return
                self.phase = "mounted_pending"
            self.server._emit_map_signal(sessions[0], self.runtime, "boss_mortis_mounted_spawn_start_signal")

    def _heal_auras(self, combat, sessions, now):
        target, fraction = _healing_aura(str(combat.encounter.static_database_path))
        members, updates = set(), []
        with combat.lock:
            for visual in combat.active_player_location_visuals(now=now):
                if visual.definition.resource_id.casefold() != "boss_mortis_unbound_white_mage_healbuff_effect_aura":
                    continue
                start = visual.expires_at - visual.definition.duration_ticks / 25.0
                if now < start or combat.monster_health.get(visual.owner_actor_id, 0) <= 0:
                    continue
                for actor, monster in combat.monsters.items():
                    hp = combat.monster_health.get(actor, 0)
                    if monster.template_id.casefold() != target or hp <= 0:
                        continue
                    if math.dist(visual.position, combat.monster_positions[actor]) > visual.definition.aura_radius:
                        continue
                    key = (visual.instance_id, actor)
                    members.add(key)
                    if key in self.healing_members:
                        continue
                    healed = min(monster.maximum_health, hp + round(monster.maximum_health * fraction))
                    if healed > hp:
                        combat.monster_health[actor] = healed
                        updates.append((actor, healed, monster.maximum_health))
            self.healing_members = members
        for actor, hp, maximum in updates:
            for viewer in sessions:
                if actor in viewer.gameplay_spawned_monster_actor_ids:
                    viewer.send_reliable_ordered(
                        encode_monster_update(MonsterUpdate(hp, maximum, 0, 0, 100), actor_id=actor),
                        track=True, label="mortis-white-mage-heal",
                    )

    def killed(self, monsters):
        if self.completed:
            return
        sessions, combat = self._context()
        if combat is None:
            return
        for monster in monsters:
            self.lifetimes.pop(monster.actor_id, None)
            template = monster.template_id.casefold()
            if template == MOUNTED:
                self.mounted_death_at = time.perf_counter()
                with combat.lock:
                    self.mounted_position = combat.monster_positions.get(monster.actor_id, monster.position)
            elif template == UNBOUND:
                self.completed = True
                self.phase = "completed"
                with combat.lock:
                    remaining = set(combat.alive_actor_ids)
                with self.runtime.lock:
                    self.runtime.event_set_actor_ids.setdefault(-214, set()).update(remaining)
                self.server._retire_event_monsters(self.runtime, sessions, (-214,))
                return
            # Mounted -> unbound is owned by its delayed map event. Other
            # arena adds retain their authored skeleton -> ripper -> mage chain.
            if template != MOUNTED:
                self._spawn_incident_minions(monster, "die")
        self._check_wave()
