"""Map location effects that create escort actors, and their NPC handoff."""
from dso_server.protocol.combat_protocol import encode_combat_batch
from dso_server.services.base import CharacterService
from dso_server.gameplay.map_companions import load_companion_handoffs, load_map_companion_spawns
from dso_server.gameplay.local_combat_state import DamageResolution


class MapCompanionService:
    @staticmethod
    def _check_map_companions(s, wire_position):
        with s.flow_lock:
            combat = s.gameplay_combat_state
            character = s.gameplay_character
            runtime = s.gameplay_map_event_runtime
            if not s.connected or combat is None or character is None:
                return
            generation = s.gameplay_chase_generation
            owner = s.gameplay_remote_actor_id or CharacterService._remote_actor_id(character.character_id)
            tick = int(s.gameplay_chase_server_tick or getattr(s.gameplay_last_move, "start_tick", 0))
            yaw = float(getattr(s.gameplay_last_move, "yaw", 0.0))
        definitions = load_map_companion_spawns(combat.encounter.static_database_path,
                                               combat.encounter.map_id)
        if not definitions:
            return
        world = combat.encounter.coordinate_frame.wire_to_world(wire_position)
        if runtime is not None:
            with runtime.lock:
                enabled = frozenset(runtime.enabled_event_sets)
        else:
            enabled = frozenset()
        for spec in definitions:
            if spec.volume.event_set_row >= 0 and spec.volume.event_set_row not in enabled:
                continue
            if not spec.volume.contains(world):
                continue
            template = spec.definition.monster_template_id
            key = (int(owner), template)
            with combat.lock:
                if key in combat.map_companion_spawns or template in combat.map_companion_retired:
                    continue
                combat.map_companion_spawns.add(key)
            CharacterService._resolve_player_summon(
                s, generation, ("map-companion", owner, tick), spec.definition,
                owner, world, yaw, spec.level, tick, spec.damage, (),
            )
            with combat.lock:
                if not any(a.owner_actor_id == owner and a.definition.monster_template_id == template
                           for a in combat.player_summons.values()):
                    combat.map_companion_spawns.discard(key)

    def _restore_map_companion_handoffs(self, s):
        """Restore a story handoff after re-entry from persisted task credit.

        Do not feed another kill to the quest or grant rewards again. Only the
        already-earned map event is restored in the new map incarnation.
        """
        path = self.static_database_path
        templates = {spec.definition.monster_template_id
                     for spec in load_map_companion_spawns(path, s.gameplay_map)}
        relevant = tuple(h for h in load_companion_handoffs(path) if h.companion_id in templates)
        if not relevant:
            return
        catalog = self._quest_catalog()
        completed = {
            event.value.casefold()
            for quest_id, progress in (s.gameplay_quest_log or {}).items()
            for quest in (catalog.quest(quest_id),) if quest is not None
            for task in quest.tasks if progress.task_finished(task.index)
            for event in task.events if event.kind == "kill"
        }
        combat = s.gameplay_combat_state
        for handoff in relevant:
            if handoff.defeated_id.casefold() not in completed:
                continue
            if combat is not None:
                with combat.lock:
                    combat.map_companion_retired.add(handoff.companion_id)
            self._fire_map_signal(s, "PlayerKilledMonster", (handoff.defeated_id,))

    def _on_map_companion_boss_defeated(self, s, monsters):
        runtime = s.gameplay_map_event_runtime
        combat = s.gameplay_combat_state
        if runtime is None or combat is None:
            return
        killed = {m.template_id.casefold(): m for m in monsters}
        for handoff in load_companion_handoffs(combat.encounter.static_database_path):
            monster = killed.get(handoff.defeated_id.casefold())
            if monster is None:
                continue
            with combat.lock:
                if handoff.companion_id in combat.map_companion_retired:
                    continue
                combat.map_companion_retired.add(handoff.companion_id)
                position = combat.monster_positions.get(getattr(monster, "actor_id", 0), monster.position)
            # Map-owned scheduling survives an owner disconnect, but never
            # publishes into a replacement map incarnation.
            timer = runtime.scheduler.timer(
                handoff.injury_delay, self._wound_map_companions,
                args=(runtime, combat, handoff, position),
            )
            timer.start()

    def _wound_map_companions(self, runtime, combat, handoff, position):
        if not self._map_event_runtime_is_live(runtime):
            return
        sessions = tuple(s for s in self._map_event_sessions(runtime)
                         if s.gameplay_combat_state is combat)
        if not sessions:
            return
        tick = max(int(s.gameplay_chase_server_tick) for s in sessions)
        resolutions = []
        with combat.lock:
            for summon in tuple(combat.player_summons.values()):
                if summon.definition.monster_template_id != handoff.companion_id:
                    continue
                if (combat._distance(summon.position, position) > handoff.radius
                        or abs(summon.position[1] - position[1]) > handoff.height):
                    continue
                health = combat.player_summon_health[summon.actor_id]
                resolutions.append(DamageResolution(
                    target_actor_id=summon.actor_id, attacker_actor_id=0,
                    current_health=0, maximum_health=health[1], damage=health[0],
                    killed=True, position=summon.position, server_tick=tick,
                    hit_tags=(255,), skill_index=0, corpse_lifetime_ticks=round(handoff.corpse_seconds * 25),
                ))
                combat.expire_player_summon(summon.actor_id)
        for resolution in resolutions:
            packet, bits = encode_combat_batch(CharacterService._combat_batch_for_resolution(resolution))
            for viewer in sessions:
                CharacterService._send_world_loot_state_batch(
                    viewer, packet, bits, label="map-companion-wounded")
        if resolutions:
            timer = runtime.scheduler.timer(
                handoff.corpse_seconds, self._discard_wounded_map_companions,
                args=(runtime, combat, tuple(r.target_actor_id for r in resolutions)),
            )
            timer.start()

    def _discard_wounded_map_companions(self, runtime, combat, actors):
        if not self._map_event_runtime_is_live(runtime):
            return
        for viewer in self._map_event_sessions(runtime):
            if viewer.gameplay_combat_state is combat:
                CharacterService._send_player_summon_despawn_to(viewer, actors)
