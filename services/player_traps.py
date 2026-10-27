"""Native player trap creation, trip detection, damage, and teardown."""

from __future__ import annotations

from dso_server.gameplay.pvp_match_state import MatchPhase
from dso_server.runtime.context import *
from dso_server.runtime.transport import Session


TRAP_POLL_SECONDS = 0.10
TRAP_TRIGGER_VISUAL_SECONDS = 0.50


class PlayerTrapService:
    """Run authored ``TrapOnHit`` skills as shared map-world objects."""

    @staticmethod
    def _send_player_trap_spawn_to(viewer: Session, trap) -> None:
        owner_actor_id = CharacterService._viewer_actor_id(
            viewer, int(trap.owner_actor_id)
        )
        viewer.send_reliable_ordered(
            encode_new_trap_batch(
                TrapInfo(
                    trap_id=int(trap.trap_id),
                    resource_id=trap.definition.resource_id,
                    created_tick=int(trap.server_tick),
                    owner_actor_id=owner_actor_id,
                    transform_mode=2,
                    position=trap.position,
                    yaw=float(trap.yaw),
                ),
                actor_id=owner_actor_id,
            ),
            track=True,
            label=f"player-trap-new-{int(trap.trap_id)}",
        )

    @staticmethod
    def _send_player_trap_tripped_to(viewer: Session, trap_id: int) -> None:
        viewer.send_reliable_ordered(
            encode_trap_tripped_batch(int(trap_id)),
            track=True,
            label=f"player-trap-tripped-{int(trap_id)}",
        )

    @staticmethod
    def _send_player_trap_discard_to(viewer: Session, trap_id: int) -> None:
        viewer.send_reliable_ordered(
            encode_discard_trap_batch(int(trap_id)),
            track=True,
            label=f"player-trap-discard-{int(trap_id)}",
        )

    @staticmethod
    def _schedule_player_trap(s: Session, combat, definition, *,
                              delay_seconds: float, cast_started_at: float,
                              **spawn_kwargs) -> None:
        """Create and arm the trap only after its placement animation."""
        delay = max(0.0, float(delay_seconds))
        key = ("player-trap-create", int(spawn_kwargs["owner_actor_id"]),
               int(spawn_kwargs["cast_id"]))
        spawn_kwargs["server_tick"] = (
            int(spawn_kwargs["server_tick"]) + math.ceil(delay * SERVER_TICKS_PER_SECOND - 1e-9)
        ) & 0xFFFFFFFF
        with s.flow_lock:
            generation = s.gameplay_chase_generation
            round_token = CharacterService._pvp_round_token(s)
            timer = s.gameplay_deadline_timer(
                delay, CharacterService._resolve_player_trap_creation,
                args=(s, generation, key, combat, definition, round_token, spawn_kwargs),
            )
            timer.absolute_deadline = float(cast_started_at) + delay
            timer.daemon = True
            old_timer = s.gameplay_projectile_timers.pop(key, None)
            s.gameplay_projectile_timers[key] = timer
        if old_timer is not None:
            old_timer.cancel()
        timer.start()

    @staticmethod
    def _resolve_player_trap_creation(s, generation, key, combat, definition,
                                      round_token, spawn_kwargs) -> None:
        with s.flow_lock:
            s.gameplay_projectile_timers.pop(key, None)
            if (not s.connected or s.gameplay_chase_generation != generation
                    or s.gameplay_combat_state is not combat
                    or round_token != CharacterService._pvp_round_token(s)
                    or (s.gameplay_match is not None and s.gameplay_match.phase != MatchPhase.RUNNING)):
                return
        CharacterService._spawn_player_trap(s, combat, definition, **spawn_kwargs)

    @staticmethod
    def _spawn_player_trap(
        s: Session,
        combat: LocalCombatState,
        definition,
        *,
        owner_actor_id: int,
        skill_index: int,
        cast_id: int,
        server_tick: int,
        position: tuple[float, float, float],
        yaw: float,
        player_damage_range: tuple[float, float],
        critical_hit_chance: float,
        critical_damage_multiplier: float,
        modifier_sources: tuple[object, ...],
    ) -> None:
        now = time.perf_counter()
        with combat.lock:
            trap = combat.spawn_player_trap(
                definition,
                owner_actor_id=owner_actor_id,
                skill_index=skill_index,
                cast_id=cast_id,
                server_tick=server_tick,
                position=position,
                yaw=yaw,
                player_damage_range=player_damage_range,
                critical_hit_chance=critical_hit_chance,
                critical_damage_multiplier=critical_damage_multiplier,
                modifier_sources=modifier_sources,
                now=now,
            )
        callback = s.gameplay_presence_callback
        if callback is not None:
            callback(s, ("player_trap_spawn", trap))
        else:
            CharacterService._send_player_trap_spawn_to(s, trap)
        CharacterService._schedule_player_trap_poll(s, combat, trap.trap_id)

    @staticmethod
    def _schedule_player_trap_poll(
        s: Session, combat: LocalCombatState, trap_id: int
    ) -> None:
        timer_key = ("player-trap", int(trap_id))
        timer = s.gameplay_deadline_timer(
            TRAP_POLL_SECONDS,
            CharacterService._poll_player_trap,
            args=(
                s,
                combat,
                int(trap_id),
                int(s.gameplay_chase_generation),
            ),
        )
        timer.daemon = True
        with s.flow_lock:
            old_timer = s.gameplay_projectile_timers.pop(timer_key, None)
            s.gameplay_projectile_timers[timer_key] = timer
        if old_timer is not None:
            old_timer.cancel()
        timer.start()

    @staticmethod
    def _poll_player_trap(
        s: Session,
        combat: LocalCombatState,
        trap_id: int,
        generation: int,
    ) -> None:
        timer_key = ("player-trap", int(trap_id))
        with s.flow_lock:
            s.gameplay_projectile_timers.pop(timer_key, None)
            still_current = (
                s.connected
                and s.gameplay_combat_state is combat
                and int(s.gameplay_chase_generation) == int(generation)
            )
        now = time.perf_counter()
        pending_hits = ()
        tripped = False
        trap = None
        with combat.lock:
            trap = combat.player_traps.get(int(trap_id))
            if trap is None:
                return
            expired = not still_current or now >= trap.expires_at
            if not expired:
                tripped = any(
                    actor_id not in combat.returning_home_actor_ids
                    # An arena team's own structures do not spring its traps.
                    and combat.monster_hostile_to_player(
                        actor_id, trap.owner_actor_id)
                    and math.dist(
                        trap.position, combat.monster_positions[actor_id]
                    )
                    <= (
                        trap.definition.trip_radius
                        + combat.monsters[actor_id].collision_radius
                    )
                    for actor_id in combat.living_actor_ids
                )
            if not expired and not tripped:
                trap = None
            else:
                trap = combat.remove_player_trap(int(trap_id))
                if tripped and trap is not None:
                    pending_hits = combat.start_spatial_skill_hits(
                        skill_index=trap.skill_index,
                        cast_id=trap.cast_id,
                        server_tick=trap.server_tick,
                        player_position=(
                            combat.encounter.coordinate_frame.world_to_wire(
                                trap.position
                            )
                        ),
                        source_actor_id=trap.owner_actor_id,
                        now=now,
                        modifier_effect_ids=trap.modifier_sources,
                        player_damage_range=trap.player_damage_range,
                        player_critical_hit_chance=(
                            trap.critical_hit_chance
                        ),
                        player_critical_damage_multiplier=(
                            trap.critical_damage_multiplier
                        ),
                        area_radius_override=trap.definition.trip_radius,
                        impact_delay_override=0.0,
                        additional_victim_effects=(
                            trap.definition.victim_status_effects
                        ),
                    )
        if trap is None:
            CharacterService._schedule_player_trap_poll(
                s, combat, int(trap_id)
            )
            return

        callback = s.gameplay_presence_callback
        if tripped:
            if callback is not None:
                callback(s, ("player_trap_tripped", int(trap_id)))
            else:
                CharacterService._send_player_trap_tripped_to(s, trap_id)
            for pending in pending_hits:
                impact_key = (
                    pending.source_actor_id,
                    pending.cast_id,
                    pending.impact_index,
                )
                timer = s.gameplay_deadline_timer(
                    max(0.0, pending.due_at - now),
                    CharacterService._resolve_player_projectile,
                    args=(
                        s,
                        generation,
                        pending.cast_id,
                        pending.source_actor_id,
                        pending.impact_index,
                    ),
                )
                timer.daemon = True
                with s.flow_lock:
                    s.gameplay_projectile_timers[impact_key] = timer
                timer.start()

        discard_delay = TRAP_TRIGGER_VISUAL_SECONDS if tripped else 0.0
        timer = s.gameplay_deadline_timer(
            discard_delay,
            CharacterService._publish_player_trap_discard,
            args=(s, int(trap_id)),
        )
        timer.daemon = True
        timer.start()

    @staticmethod
    def _publish_player_trap_discard(s: Session, trap_id: int) -> None:
        callback = s.gameplay_presence_callback
        if callback is not None:
            callback(s, ("player_trap_discard", int(trap_id)))
        elif s.connected:
            CharacterService._send_player_trap_discard_to(s, trap_id)
