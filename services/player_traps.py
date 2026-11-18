"""Native player trap creation, trip detection, damage, and teardown."""

from __future__ import annotations

from dso_server.gameplay.pvp_match_state import MatchPhase
from dso_server.runtime.context import *
from dso_server.runtime.transport import Session


TRAP_POLL_SECONDS = 0.10
TRAP_TRIGGER_VISUAL_SECONDS = 0.50
_LAST_TRAP_PROBE: dict[int, float] = {}


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
        rift_force_echo_chance: float = 0.0,
        rift_force_echo_multiplier: float = 0.0,
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
                rift_force_echo_chance=rift_force_echo_chance,
                rift_force_echo_multiplier=rift_force_echo_multiplier,
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
        with combat.lock:
            trap = combat.player_traps.get(int(trap_id))
            if trap is None:
                return
            trap_position = trap.position
            trip_radius = float(trap.definition.trip_radius)
            trap_expires_at = trap.expires_at
        expired = not still_current or now >= trap_expires_at
        # Hostile players are not part of the monster population, so they are
        # tested separately; the presence callback owns their sessions and
        # wire positions, which must be read outside ``combat.lock``.
        pvp_targets = (
            () if expired
            else CharacterService._player_trap_victims(
                s, combat, trap_position, trip_radius,
            )
        )
        pending_hits = ()
        pvp_timers = ()
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
                if not tripped and pvp_targets:
                    tripped = True
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
                        player_rift_force_echo_chance=(
                            trap.rift_force_echo_chance
                        ),
                        player_rift_force_echo_multiplier=(
                            trap.rift_force_echo_multiplier
                        ),
                        area_radius_override=trap.definition.trip_radius,
                        impact_delay_override=0.0,
                        additional_victim_effects=(
                            trap.definition.victim_status_effects
                        ),
                    )
        # The PvP pipeline reads the presence layer (session locks), which may
        # not nest inside the map/combat lock this poll ran under.
        if trap is not None and tripped and pvp_targets:
            pvp_timers = CharacterService._player_trap_pvp_hits(
                s, combat, trap, pvp_targets, now=now,
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] trap tripped with "
                f"pvp targets {pvp_targets}: "
                f"{len(pvp_timers)} pvp hit(s), "
                f"{len(pending_hits)} monster hit(s)"
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
            for timer in pvp_timers:
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
    def _player_trap_victims(s: Session, combat, position, radius) -> tuple[int, ...]:
        """Hostile players standing inside a trap's trip radius.

        The presence callback owns peer sessions and their wire positions, so
        this has to run outside ``combat.lock``.  Monster trips are resolved
        separately by the poll.
        """

        callback = s.gameplay_presence_callback
        if not s.gameplay_pvp_enabled or callback is None:
            return ()
        frame = combat.encounter.coordinate_frame
        victims = []
        targets = tuple(callback(s, ("combat_targets",)) or ())
        seen = []
        for actor_id, wire_position, owner in targets:
            world = frame.wire_to_world(tuple(wire_position))
            distance = math.dist(position, world)
            hostile = CharacterService._pvp_target_is_hostile(s, owner)
            same_world = getattr(owner, "gameplay_combat_state", None) is combat
            accepted = hostile and same_world and distance <= float(radius) + 0.75
            seen.append(
                f"actor={actor_id} d={distance:.2f} hostile={hostile} "
                f"same_world={same_world} hit={accepted}"
            )
            if accepted:
                victims.append(int(actor_id))
        if targets and not victims:
            # Throttled so a trap that never springs cannot flood the log.
            now = time.perf_counter()
            if now - _LAST_TRAP_PROBE.get(id(s), 0.0) > 1.0:
                _LAST_TRAP_PROBE[id(s)] = now
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] trap probe "
                    f"r={float(radius):.2f} targets={len(targets)}: "
                    + "; ".join(seen)
                )
        return tuple(victims)

    @staticmethod
    def _player_trap_pvp_hits(s: Session, combat, trap, actor_ids, *, now):
        """Queue PvP damage and effects for the players a trap caught."""

        from types import SimpleNamespace

        frame = combat.encounter.coordinate_frame
        wire_position = frame.world_to_wire(trap.position)
        trigger = SimpleNamespace(
            skill_index=int(trap.skill_index),
            cast_id=int(trap.cast_id),
            aim_angle=float(trap.yaw),
            server_tick=int(trap.server_tick),
        )
        return CharacterService._prepare_pvp_skill_hits(
            s, combat, trigger,
            source_position=wire_position,
            source_actor_id=int(trap.owner_actor_id),
            player_damage_range=trap.player_damage_range,
            player_critical_hit_chance=trap.critical_hit_chance,
            player_critical_damage_multiplier=trap.critical_damage_multiplier,
            attack_speed_multiplier=1.0,
            modifier_sources=tuple(trap.modifier_sources),
            player_rift_force_echo_chance=trap.rift_force_echo_chance,
            player_rift_force_echo_multiplier=(
                trap.rift_force_echo_multiplier
            ),
            explicit_target_actor_ids=tuple(int(value) for value in actor_ids),
            target_position=wire_position,
            now=now,
            area_radius_override=float(trap.definition.trip_radius),
            impact_delay_override=0.0,
            additional_victim_effects=tuple(
                trap.definition.victim_status_effects
            ),
        )

    @staticmethod
    def _publish_player_trap_discard(s: Session, trap_id: int) -> None:
        callback = s.gameplay_presence_callback
        if callback is not None:
            callback(s, ("player_trap_discard", int(trap_id)))
        elif s.connected:
            CharacterService._send_player_trap_discard_to(s, trap_id)
