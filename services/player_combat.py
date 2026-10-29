"""Player skill validation, targeting, projectiles, and impacts."""

from __future__ import annotations

import math
import os

from dso_server.runtime.context import *
from dso_server.runtime import latency_stats
from dso_server.runtime.transport import Session
from dso_server.services.player_cast_timing import PlayerCastTimingService
from dso_server.services import movement_validation
from dso_server.gameplay.player_skill_timing import (
    reserve_player_skill_timing, rollback_player_skill_timing,
)


SKILL_TRACE = os.getenv("DSO_SKILL_TRACE", "0").strip().lower() in {
    "1", "true", "yes",
}


class PlayerCombatService(PlayerCastTimingService):
    """Player skill validation, targeting, projectiles, and impacts."""

    @staticmethod
    def _accept_player_skill_timing(
        s, combat, *, skill_index, modifier_sources, speed, now,
        pay_resources=True,
    ) -> bool:
        """Gate one client request before any resource or combat side effects.

        Geometry resolution never holds flow_lock while waiting on map state.
        The reservation also serializes duplicate requests; failed resource
        payment rolls it back so a rejected cast does not start a cooldown.
        """
        geometry = combat._load_player_skill_geometry(skill_index)
        if geometry is None:
            return False
        geometry = combat._modified_player_skill_geometry(
            geometry, tuple(modifier_sources)
        )
        # Judge cadence by when the cast arrived: time spent queued behind
        # other ingress work is not the client casting early.
        received_at = getattr(s, "current_message_received_at", None)
        timing_now = (
            min(float(now), float(received_at))
            if isinstance(received_at, (int, float)) else now
        )
        reservation = reserve_player_skill_timing(
            s, geometry, speed, now=timing_now
        )
        if reservation is None:
            if SKILL_TRACE:
                print(f"[{GAMEPLAY_SERVICE_NAME}] skill {skill_index} rejected: cooldown or casting cadence")
            return False
        if pay_resources:
            committed = False

            def resource_committed():
                # Called under combat.lock: deliberately no locks or I/O.
                nonlocal committed
                committed = True

            try:
                accepted = CharacterService._apply_player_skill_resource(
                    s, combat, skill_index=skill_index,
                    modifier_sources=tuple(modifier_sources), now=now,
                    on_commit=resource_committed,
                )
            except Exception:
                if not committed:
                    rollback_player_skill_timing(s, reservation)
                raise
            if not accepted:
                if not committed:
                    rollback_player_skill_timing(s, reservation)
                return False
        return True

    @staticmethod
    def _trace_spatial_skill(
        combat,
        skill_index,
        position,
        plan,
        eligible_actor_ids,
        visible_actor_positions,
    ) -> None:
        try:
            geometry = combat._load_player_skill_geometry(skill_index)
            origin = combat.encounter.coordinate_frame.wire_to_world(position)
            hit_ids = {
                impact.target.actor_id
                for impact in (plan.impacts if plan is not None else ())
            }
            nearby = []
            for actor_id in combat.living_actor_ids:
                at = combat.monster_positions[actor_id]
                flat_distance = math.hypot(
                    at[0] - origin[0], at[2] - origin[2]
                )
                if flat_distance > 8.0:
                    continue
                seen = visible_actor_positions.get(actor_id)
                entry = (
                    f"{actor_id}:{flat_distance:.1f}m "
                    f"dy{at[1] - origin[1]:+.1f}"
                    f"{' HIT' if actor_id in hit_ids else ''}"
                    f"{' not-sent' if eligible_actor_ids is not None and actor_id not in eligible_actor_ids else ''}"
                    f"{' home' if actor_id in combat.returning_home_actor_ids else ''}"
                )
                if seen is not None:
                    entry += (
                        " seen@"
                        f"{math.hypot(seen[0] - origin[0], seen[2] - origin[2]):.1f}m"
                    )
                nearby.append(entry)
            resource_id = geometry.resource_id if geometry else str(skill_index)
            targeting = geometry.targeting_type if geometry else "?"
            hit_range = geometry.hit_range if geometry else 0.0
            print(
                f"[skill-trace] {resource_id} ({targeting}, hit range "
                f"{hit_range:.2f}) at {tuple(round(v, 1) for v in origin)}: "
                f"{len(hit_ids)} hit; " + ", ".join(nearby)
            )
        except Exception as exc:
            # Diagnostics must not interrupt an accepted player cast.
            print(f"[skill-trace] failed: {exc!r}")

    @staticmethod
    def _record_monster_targeting_result(
            s: Session, combat, wire_position, *, hit: bool,
            skill_index: int, modifier_sources=()) -> None:
        """Bound recovery from a viewer/server transform disagreement.

        A normal miss remains a miss. Repeated misses while living registered
        actors are close trigger one reliable transform keyframe, ensuring a
        skipped unreliable movement frame can never strand the viewer forever.
        """

        now = time.perf_counter()
        with s.flow_lock:
            if s.gameplay_combat_state is not combat:
                return
            if hit:
                s.gameplay_no_target_cast_streak = 0
                return
            s.gameplay_no_target_cast_streak += 1
            streak = int(s.gameplay_no_target_cast_streak)
            if (
                streak < 5
                or now - float(s.gameplay_last_monster_resync_at) < 1.0
            ):
                return
            s.gameplay_last_monster_resync_at = now
            spawned = set(s.gameplay_spawned_monster_actor_ids)
            delivered = dict(s.gameplay_delivered_monster_positions)
        player_world = combat.encounter.coordinate_frame.wire_to_world(
            wire_position
        )
        with combat.lock:
            living = set(combat.living_actor_ids)
            returning = set(combat.returning_home_actor_ids)
            ammunition_types = combat._active_ammunition_types(
                tuple(modifier_sources)
            )
        candidates = sorted(
            (
                math.hypot(
                    position[0] - player_world[0],
                    position[2] - player_world[2],
                ),
                actor_id,
            )
            for actor_id, position in delivered.items()
            if (
                actor_id in spawned
                and actor_id in living
                and actor_id not in returning
            )
        )
        nearby_living = tuple(
            actor_id
            for distance, actor_id in candidates
            if distance <= MONSTER_VISIBILITY_EXIT_RADIUS + 1e-6
        )
        if not nearby_living:
            return
        nearby = tuple(
            actor_id
            for actor_id in nearby_living
            if (
                actor_id not in returning
                and combat._ammunition_can_hit(actor_id, ammunition_types)
            )
        )
        ammo_blocked = sum(
            actor_id not in returning
            and not combat._ammunition_can_hit(actor_id, ammunition_types)
            for actor_id in nearby_living
        )
        repaired = CharacterService._resync_visible_monster_transforms(
            s, combat, nearby, limit=16
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] targetless-cast recovery: "
            f"skill={int(skill_index)}, streak={streak}, "
            f"nearby_living={len(nearby_living)}, "
            f"returning={sum(value in returning for value in nearby_living)}, "
            f"ammo_blocked={ammo_blocked}, repaired={repaired}, "
            f"nearest={candidates[0][0]:.2f}"
        )

    @staticmethod
    def _handle_bullet_skill(s: Session, payload: bytes) -> None:
        if CharacterService._player_victim_feature_locked(
            s, "skills", "regularskills"
        ):
            return
        command_id = int.from_bytes(payload[1:3], "little")
        if command_id == TARGET_BULLET_SKILL_COMMAND:
            client_skill = decode_target_bullet_skill(
                payload, s.current_message_bit_length
            )
            skill = BulletSkill(
                client_skill.skill_level,
                client_skill.skill_index,
                client_skill.aim_angle,
                client_skill.server_tick,
                client_skill.cast_id,
                client_skill.direction,
                client_skill.variant,
                client_skill.derived_flag,
            )
        elif command_id == TARGET_POINT_BULLET_SKILL_COMMAND:
            client_skill = decode_target_point_bullet_skill(
                payload, s.current_message_bit_length
            )
            skill = BulletSkill(
                client_skill.skill_level,
                client_skill.skill_index,
                client_skill.aim_angle,
                client_skill.server_tick,
                client_skill.cast_id,
                client_skill.direction,
                client_skill.variant,
                client_skill.derived_flag,
            )
        else:
            client_skill = decode_bullet_skill(
                payload, s.current_message_bit_length
            )
            skill = client_skill
        if movement_validation.non_finite(client_skill):
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] dropped bullet skill "
                f"0x{command_id:04x} with a non-finite value"
            )
            return
        CharacterService._accept_gameplay_state_command(s, command_id)
        if CharacterService._player_victim_feature_locked(
            s, "skills", "regularskills"
        ):
            return
        now = time.perf_counter()
        phase = latency_stats.PhaseTimer("bullet-skill")
        timers = []
        old_timers = []
        position = None
        source_actor_id = None
        pending_hits = ()
        player_damage_range = (0.0, 0.0)
        player_critical_hit_chance = 0.0
        player_critical_damage_multiplier = 2.0
        player_attack_speed = 0.0
        pvp_timers = ()
        entity_plan = None
        pvp_snapshot = ((), ())
        location_owner_effects = ()
        trail_specs = ()
        combat = None
        encounter = None
        pending = None
        rejection = None
        skill_speed_multiplier = 1.0
        modifier_effect_ids = ()
        generation = 0
        eligible_actor_ids = ()
        visible_actor_positions = {}
        target_position_wire = None
        runtime_cast = False
        with s.flow_lock:
            s.gameplay_client_command_state[command_id] = client_skill
            combat = s.gameplay_combat_state
            allowed_skills = s.gameplay_skill_book_ids
            last_move = s.gameplay_last_move
            encounter = s.gameplay_monster_encounter
            runtime_cast = (
                s.gameplay_presence_callback is not None
                and s.gameplay_character is not None
            )
            if skill.skill_index not in allowed_skills:
                pending = None
                rejection = (
                    f"skill index {skill.skill_index} is not in the "
                    "character skill book"
                )
            elif combat is None or encounter is None:
                pending = None
                rejection = 'the current map has no local combat encounter'
            else:
                position = (
                    last_move.relative_position
                    if last_move is not None
                    else encounter.coordinate_frame.world_to_wire(
                        s.gameplay_spawn_position
                        if s.gameplay_spawn_position is not None
                        else encounter.trigger_center
                    )
                )
                source_actor_id = (
                    s.gameplay_remote_actor_id
                    or CharacterService._remote_actor_id(
                        s.gameplay_character.character_id
                    )
                    if runtime_cast
                    else combat.player_actor_id
                )
                generation = s.gameplay_chase_generation
                eligible_actor_ids = tuple(
                    s.gameplay_spawned_monster_actor_ids
                )
                visible_actor_positions = dict(
                    s.gameplay_delivered_monster_positions
                )
                character = s.gameplay_character
                inventory = s.gameplay_inventory

        modifier_effect_ids = CharacterService._active_skill_modifier_sources(s)
        phase.mark("lock and sources")
        if rejection is None:
            if character is not None:
                player_stats = calculate_player_combat_stats(
                    encounter.static_database_path,
                    cache_owner=s,
                    class_name=CLASS_NAMES.get(
                        int(character.class_id), ""
                    ),
                    level=max(
                        1, int(character.level)
                    ),
                    inventory=inventory,
                    modifier_sources=modifier_effect_ids,
                )
                player_damage_range = (
                    player_stats.minimum_damage,
                    player_stats.maximum_damage,
                )
                player_attack_speed = float(player_stats.attack_speed)
                player_critical_hit_chance = (
                    player_stats.critical_hit_chance
                )
                player_critical_damage_multiplier = (
                    player_stats.critical_damage_multiplier
                )
                modifier_effect_ids = (
                    tuple(modifier_effect_ids)
                    + player_stats.skill_modifier_sources
                )
                modifier_effect_ids = (
                    CharacterService._conditional_skill_modifier_sources(
                        s, modifier_effect_ids
                    )
                )
                skill_speed_multiplier = (
                    combat.player_skill_speed_multiplier(
                        class_name=CLASS_NAMES.get(
                            int(character.class_id), ""
                        ),
                        # ``player_stats.skill_modifier_sources`` already
                        # contains every equipped item, resolved with its
                        # concrete level/tier/roll. Scanning inventory
                        # again doubled attack speed and collapsed
                        # distinct skill hit frames against the cap.
                        inventory=None,
                        modifier_sources=modifier_effect_ids,
                        player_stats=player_stats,
                    )
                )
            phase.mark("stats")
            trail_specs = combat.player_skill_bullet_location_trail(
                skill.skill_index,
                modifier_sources=tuple(modifier_effect_ids),
            )
            phase.mark("geometry")
            if isinstance(client_skill, TargetPointBulletSkill):
                target_position_wire = (
                    encounter.coordinate_frame.world_to_wire(
                        client_skill.target_position
                    )
                )

            with s.flow_lock:
                if (
                    s.gameplay_combat_state is not combat
                    or s.gameplay_chase_generation != generation
                    or s.gameplay_character is not character
                    or s.gameplay_inventory is not inventory
                ):
                    return

        if (
            rejection is None
            and combat is not None
            and not CharacterService._accept_player_skill_timing(
                s, combat,
                skill_index=skill.skill_index,
                modifier_sources=tuple(modifier_effect_ids),
                speed=skill_speed_multiplier, now=now,
                pay_resources=runtime_cast,
            )
        ):
            return
        if rejection is None:
            CharacterService._cancel_mount_cast(s, "combat skill started")
            CharacterService._remove_mount_ride(s, reason="combat skill started")
        if rejection is None and runtime_cast and combat is not None:
            # The cast is accepted: it burns essence. The damage range above
            # was calculated with the essence still equipped, which is the
            # bonus this cast paid for.
            CharacterService._consume_active_essence(
                s, combat, skill_index=skill.skill_index
            )

        phase.mark("timing and essence")
        # Never wait for the shared map-combat lock while owning the session
        # lock. Autosave, presence, and map retirement need flow_lock and can
        # independently be waiting for a combat snapshot.
        if rejection is None and combat is not None and position is not None:
            if runtime_cast:
                pvp_snapshot = CharacterService._snapshot_pvp_skill_targets(
                    s, combat, skill_index=skill.skill_index, modifier_sources=modifier_effect_ids)
            with combat.lock:
                if runtime_cast:
                    entity_plan = combat.plan_entity_skill_hits(
                        skill_index=skill.skill_index, player_position=position, projectile=True,
                        direction=skill.direction, target_position=target_position_wire,
                        explicit_target_actor_ids=(
                            (client_skill.target_actor_id, *client_skill.additional_target_actor_ids)
                            if isinstance(client_skill, TargetBulletSkill) else ()
                        ),
                        require_explicit_target=isinstance(client_skill, TargetBulletSkill),
                        modifier_sources=modifier_effect_ids, eligible_actor_ids=eligible_actor_ids,
                        visible_actor_positions=visible_actor_positions, additional_entities=pvp_snapshot[1],
                        attack_speed_multiplier=skill_speed_multiplier, now=now,
                    )
                    pending_hits = combat.start_bullet_skill_hits(
                        skill,
                        position,
                        hit_plan=entity_plan,
                        source_actor_id=source_actor_id,
                        now=now,
                        attack_speed_multiplier=skill_speed_multiplier,
                        explicit_target_actor_ids=(
                            (
                                client_skill.target_actor_id,
                                *client_skill.additional_target_actor_ids,
                            )
                            if isinstance(client_skill, TargetBulletSkill)
                            else ()
                        ),
                        modifier_effect_ids=modifier_effect_ids,
                        eligible_actor_ids=eligible_actor_ids,
                        visible_actor_positions=visible_actor_positions,
                        target_position=target_position_wire,
                        player_damage_range=player_damage_range,
                        player_critical_hit_chance=(
                            player_critical_hit_chance
                        ),
                        player_critical_damage_multiplier=(
                            player_critical_damage_multiplier
                        ),
                    )
                    pending = pending_hits[0] if pending_hits else None
                else:
                    pending = combat.start_bullet_skill(
                        skill,
                        position,
                        now=now,
                        attack_speed_multiplier=skill_speed_multiplier,
                    )
                    pending_hits = (
                        (pending,) if pending is not None else ()
                    )
                has_living_targets = bool(combat.living_actor_ids)
            rejection = (
                (
                    'cast ray did not intersect a living target'
                    if has_living_targets
                    else 'no living target remains'
                )
                if pending is None else None
            )

            # Revalidate before installing deadline handles. A map transition
            # may have replaced the combat state while the cast was resolved.
            with s.flow_lock:
                session_is_current = (
                    s.gameplay_combat_state is combat
                    and s.gameplay_chase_generation == generation
                )
                if session_is_current and runtime_cast:
                    new_timers, replaced_timers = (
                        CharacterService.
                        _register_runtime_projectile_deadlines(
                            s,
                            pending_hits,
                            now=now,
                            generation=generation,
                        )
                    )
                    timers.extend(new_timers)
                    old_timers.extend(replaced_timers)
                elif session_is_current:
                    for value in pending_hits:
                        timer_key = value.cast_id
                        old_timer = s.gameplay_projectile_timers.pop(
                            timer_key, None
                        )
                        if old_timer is not None:
                            old_timers.append(old_timer)
                        timer = s.gameplay_deadline_timer(
                            max(0.0, value.due_at - now),
                            CharacterService._resolve_player_projectile,
                            args=(
                                s,
                                generation,
                                value.cast_id,
                                None,
                                value.impact_index,
                            ),
                        )
                        timer.absolute_deadline = float(value.due_at)
                        timer.daemon = True
                        s.gameplay_projectile_timers[timer_key] = timer
                        timers.append(timer)
            if not session_is_current:
                pending = None
                pending_hits = ()
                rejection = 'the gameplay combat state changed during cast'
        phase.mark("plan hits")
        # Presence discovery re-enters the originating session while taking
        # its own snapshot.  Never invoke it under ``flow_lock``: doing so
        # deadlocked every PvP-enabled spatial summon (most visibly Tesla).
        if (
            runtime_cast
            and combat is not None
            and encounter is not None
            and position is not None
            and source_actor_id is not None
            and entity_plan is not None
        ):
            pvp_timers = CharacterService._prepare_pvp_skill_hits(
                s,
                combat,
                client_skill,
                hit_plan=entity_plan,
                target_snapshot=pvp_snapshot,
                source_position=position,
                source_actor_id=source_actor_id,
                player_damage_range=player_damage_range,
                player_critical_hit_chance=player_critical_hit_chance,
                player_critical_damage_multiplier=(
                    player_critical_damage_multiplier
                ),
                attack_speed_multiplier=skill_speed_multiplier,
                modifier_sources=modifier_effect_ids,
                explicit_target_actor_ids=(
                    (
                        client_skill.target_actor_id,
                        *client_skill.additional_target_actor_ids,
                    )
                    if isinstance(client_skill, TargetBulletSkill)
                    else ()
                ),
                target_position=(
                    encounter.coordinate_frame.world_to_wire(
                        client_skill.target_position
                    )
                    if isinstance(client_skill, TargetPointBulletSkill)
                    else None
                ),
                now=now,
            )
            timers.extend(pvp_timers)
        for old_timer in old_timers:
            old_timer.cancel()
        if (
            runtime_cast
            and position is not None
            and source_actor_id is not None
            and s.gameplay_presence_callback is not None
        ):
            s.gameplay_presence_callback(
                s,
                (
                    "player_bullet",
                    client_skill,
                    pending,
                    position,
                    source_actor_id,
                    tuple(modifier_effect_ids),
                    float(skill_speed_multiplier),
                ),
            )
        phase.mark("pvp and presence")
        if runtime_cast and combat is not None and position is not None and source_actor_id is not None:
            CharacterService._send_player_skill_status_effects(
                s,
                combat.player_skill_user_effects(
                    skill.skill_index,
                    cast_id=skill.cast_id,
                    trigger="skillstart",
                    modifier_effect_ids=modifier_effect_ids,
                )
                + CharacterService._item_set_user_proc_effects(
                    s, skill.skill_index, triggers=("skillstart",)
                ),
                server_tick=skill.server_tick,
                label="player-skillstart-status-effects",
            )
            if pending_hits or pvp_timers:
                CharacterService._send_attack_speed_bonus_stack(
                    s,
                    attack_speed=player_attack_speed,
                    server_tick=skill.server_tick,
                    label="player-attack-speed-bonus-stack",
                )
        phase.mark("skill-start effects")
        if all((runtime_cast, combat, encounter, position, source_actor_id)):
            CharacterService._schedule_player_bullet_trail(
                s, combat, encounter, skill, client_skill, position,
                int(source_actor_id), trail_specs, player_damage_range,
                attack_speed_multiplier=skill_speed_multiplier,
                modifier_sources=modifier_effect_ids, cast_started_at=now,
            )
        phase.mark("trail")
        for timer in timers:
            timer.start()
        # A cast is authoritative map activity, so re-arm the simulation owner
        # from the combat path itself. Previously nothing here or on the impact
        # path ever restarted the worker: recovery depended entirely on the
        # client requesting a newly announced actor. No locks are held at this
        # point, and this is a cheap no-op while a worker is installed.
        if runtime_cast and combat is not None:
            CharacterService._start_monster_chase(s)
            if position is not None:
                CharacterService._record_monster_targeting_result(
                    s,
                    combat,
                    position,
                    hit=bool(pending_hits or pvp_timers),
                    skill_index=skill.skill_index,
                    modifier_sources=modifier_effect_ids,
                )
        phase.mark("timers and chase")
        if pending is None:
            if VERBOSE_PACKET_LOGGING:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] accepted BulletSkill "
                    f"index={skill.skill_index}; monster_hit=none, "
                    f"scheduled_hits={len(timers)}, reason={rejection}"
                )
            return
        if VERBOSE_PACKET_LOGGING:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] scheduled BulletSkill "
                f"index={skill.skill_index}, cast={skill.cast_id}, "
                f"impacts={len(pending_hits)}, first_target="
                f"{pending.target_actor_id}, center_distance="
                f"{pending.distance:.2f}, flight_distance="
                f"{pending.travel_distance:.2f}, emit={pending.emit_delay:.3f}s, "
                f"skill_speed={skill_speed_multiplier:.3f}, "
                f"player_damage="
                f"{player_damage_range[0]:.1f}-"
                f"{player_damage_range[1]:.1f}, "
                f"impact_in={max(0.0, pending.due_at - now):.3f}s"
            )

    @staticmethod
    def _handle_spatial_skill(s: Session, payload: bytes) -> None:
        # A RegularSkills lock (stun, freeze) depends on the skill: the
        # debuff-cleansing escapes still cast. Judge it once decoded.
        if CharacterService._player_victim_feature_locked(s, "skills"):
            return
        command_id = int.from_bytes(payload[1:3], "little")
        if command_id == SKILL_COMMAND:
            client_skill = decode_skill(
                payload, s.current_message_bit_length
            )
            target_position = None
            explicit_target_actor_ids = ()
        elif command_id == TARGET_SKILL_COMMAND:
            client_skill = decode_target_skill(
                payload, s.current_message_bit_length
            )
            target_position = None
            explicit_target_actor_ids = (
                client_skill.target_actor_id,
            )
        elif command_id == SHIFTED_SKILL_COMMAND:
            client_skill = decode_shifted_skill(
                payload, s.current_message_bit_length
            )
            # ShiftedSkill serializes an absolute map position.  Combat-state
            # APIs intentionally accept the same origin-relative coordinate
            # space as MoveCommand, so normalize it once at the protocol
            # boundary instead of adding the map origin a second time.
            target_position = client_skill.target_position
            explicit_target_actor_ids = ()
        else:
            raise ValueError(
                f"unsupported spatial skill command 0x{command_id:04x}"
            )
        if movement_validation.non_finite(client_skill):
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] dropped spatial skill "
                f"0x{command_id:04x} with a non-finite value"
            )
            return
        CharacterService._accept_gameplay_state_command(s, command_id)
        if command_id == TARGET_SKILL_COMMAND and client_skill.target_actor_id == 0:
            # Target loss is a no-op before mount, resource, cooldown, or hit
            # side effects. Never turn it into an untargeted area attack.
            with s.flow_lock:
                s.gameplay_client_command_state[command_id] = client_skill
            return
        if CharacterService._player_skill_cast_locked(
            s, client_skill.skill_index
        ):
            return
        now = time.perf_counter()
        timers = []
        old_timers = []
        pending_hits = ()
        position = None
        source_actor_id = None
        rejection = None
        modifier_effect_ids = ()
        player_damage_range = (0.0, 0.0)
        player_critical_hit_chance = 0.0
        player_critical_damage_multiplier = 2.0
        player_attack_speed = 0.0
        skill_speed_multiplier = 1.0
        summon_definition = None
        trap_definition = None
        movement_skill_destination = None
        summon_timer = None
        summon_position_world = None
        location_position_world = None
        location_visual_definitions = ()
        location_visual_delay = 0.0
        location_owner_effects = ()
        pvp_timers = ()
        entity_plan = None
        pvp_snapshot = ((), ())
        combat = None
        encounter = None
        generation = 0
        eligible_actor_ids = ()
        visible_actor_positions = {}
        character_level = 1
        with s.flow_lock:
            s.gameplay_client_command_state[command_id] = client_skill
            combat = s.gameplay_combat_state
            encounter = s.gameplay_monster_encounter
            last_move = s.gameplay_last_move
            if client_skill.skill_index not in s.gameplay_skill_book_ids:
                rejection = (
                    f"skill index {client_skill.skill_index} is not in the "
                    "character skill book"
                )
            elif combat is None or encounter is None:
                rejection = "the current map has no local combat encounter"
            elif s.gameplay_character is None:
                rejection = "the gameplay character is unavailable"
            else:
                position = (
                    last_move.relative_position
                    if last_move is not None
                    else encounter.coordinate_frame.world_to_wire(
                        s.gameplay_spawn_position
                        if s.gameplay_spawn_position is not None
                        else encounter.trigger_center
                    )
                )
                source_actor_id = (
                    s.gameplay_remote_actor_id
                    or CharacterService._remote_actor_id(
                        s.gameplay_character.character_id
                    )
                )
                generation = s.gameplay_chase_generation
                eligible_actor_ids = tuple(
                    s.gameplay_spawned_monster_actor_ids
                )
                visible_actor_positions = dict(
                    s.gameplay_delivered_monster_positions
                )
                character_level = max(
                    1, int(s.gameplay_character.level)
                )

                character = s.gameplay_character
                inventory = s.gameplay_inventory

        if rejection is None:
            modifier_effect_ids = (
                CharacterService._active_skill_modifier_sources(s)
            )
            player_stats = calculate_player_combat_stats(
                encounter.static_database_path,
                cache_owner=s,
                class_name=CLASS_NAMES.get(
                    int(character.class_id), ""
                ),
                level=max(1, int(character.level)),
                inventory=inventory,
                modifier_sources=modifier_effect_ids,
            )
            player_damage_range = (
                player_stats.minimum_damage,
                player_stats.maximum_damage,
            )
            player_attack_speed = float(player_stats.attack_speed)
            player_critical_hit_chance = (
                player_stats.critical_hit_chance
            )
            player_critical_damage_multiplier = (
                player_stats.critical_damage_multiplier
            )
            modifier_effect_ids = (
                tuple(modifier_effect_ids)
                + player_stats.skill_modifier_sources
            )
            modifier_effect_ids = (
                CharacterService._conditional_skill_modifier_sources(
                    s, modifier_effect_ids
                )
            )
            summon_definition = combat.player_summon_definition(
                client_skill.skill_index,
                modifier_sources=modifier_effect_ids,
            )
            trap_definition = combat.player_trap_definition(
                client_skill.skill_index,
                modifier_sources=modifier_effect_ids,
            )
            skill_speed_multiplier = (
                combat.player_skill_speed_multiplier(
                    class_name=CLASS_NAMES.get(
                        int(character.class_id), ""
                    ),
                    inventory=None,
                    modifier_sources=modifier_effect_ids,
                    player_stats=player_stats,
                )
            )
            geometry = combat._load_player_skill_geometry(
                client_skill.skill_index
            )
            if (
                target_position is not None
                and geometry is not None
                and geometry.skill_type.casefold()
                in movement_validation.MOVEMENT_SKILL_TYPES
            ):
                with s.flow_lock:
                    clamped = movement_validation.clamp_movement_destination(
                        s, geometry, position, target_position
                    )
                if movement_validation._flat_distance(
                    clamped, target_position
                ) > 0.05:
                    print(
                        f"[{GAMEPLAY_SERVICE_NAME}] {geometry.skill_type} "
                        f"destination {tuple(round(v, 2) for v in target_position)} "
                        f"clamped to {tuple(round(v, 2) for v in clamped)}"
                    )
                target_position = clamped
                movement_skill_destination = clamped
                if hasattr(client_skill, "target_position"):
                    client_skill = replace(
                        client_skill, target_position=clamped
                    )
                    with s.flow_lock:
                        s.gameplay_client_command_state[command_id] = client_skill
            if target_position is not None:
                summon_position_world = tuple(
                    float(value) for value in target_position
                )
                location_position_world = summon_position_world
                target_position = (
                    encounter.coordinate_frame.world_to_wire(
                        target_position
                    )
                )
            else:
                location_position_world = (
                    encounter.coordinate_frame.wire_to_world(
                        position
                    )
                )

            with s.flow_lock:
                if (
                    s.gameplay_combat_state is not combat
                    or s.gameplay_chase_generation != generation
                    or s.gameplay_character is not character
                    or s.gameplay_inventory is not inventory
                ):
                    return

        if (
            rejection is None
            and combat is not None
            and source_actor_id is not None
            and not CharacterService._accept_player_skill_timing(
                s, combat,
                skill_index=client_skill.skill_index,
                modifier_sources=tuple(modifier_effect_ids),
                speed=skill_speed_multiplier, now=now,
            )
        ):
            return
        if rejection is None:
            CharacterService._cancel_mount_cast(s, "combat skill started")
            CharacterService._remove_mount_ride(s, reason="combat skill started")
        if rejection is None and combat is not None:
            # Cleanse at cast start, before the destination allowance and
            # any MoveCommand that follows the escape.
            CharacterService._cleanse_player_skill_debuffs(
                s,
                combat,
                skill_index=client_skill.skill_index,
                modifier_sources=modifier_effect_ids,
            )
        if rejection is None and movement_skill_destination is not None:
            with s.flow_lock:
                if s.gameplay_combat_state is combat:
                    movement_validation.record_movement_allowance(
                        s, position, movement_skill_destination
                    )
        if (
            rejection is None
            and combat is not None
            and source_actor_id is not None
        ):
            CharacterService._consume_active_essence(
                s, combat, skill_index=client_skill.skill_index
            )

        # Resolve shared map state without flow_lock. Holding both locks made
        # this cast path part of the session/combat lock cycle seen under
        # autosave and populated-map activity.
        if rejection is None and combat is not None and position is not None:
            pvp_snapshot = CharacterService._snapshot_pvp_skill_targets(
                s, combat, skill_index=client_skill.skill_index, modifier_sources=modifier_effect_ids)
            with combat.lock:
                if trap_definition is None:
                    entity_plan = combat.plan_entity_skill_hits(
                        skill_index=client_skill.skill_index, player_position=position, projectile=False,
                        aim_angle=client_skill.aim_angle, target_position=target_position,
                        explicit_target_actor_ids=explicit_target_actor_ids,
                        modifier_sources=modifier_effect_ids, eligible_actor_ids=eligible_actor_ids,
                        visible_actor_positions=visible_actor_positions, additional_entities=pvp_snapshot[1],
                        attack_speed_multiplier=skill_speed_multiplier, now=now,
                    )
                    if SKILL_TRACE:
                        CharacterService._trace_spatial_skill(
                            combat,
                            client_skill.skill_index,
                            position,
                            entity_plan,
                            eligible_actor_ids,
                            visible_actor_positions,
                        )
                    pending_hits = combat.start_spatial_skill_hits(
                        hit_plan=entity_plan,
                        skill_index=client_skill.skill_index,
                        cast_id=client_skill.cast_id,
                        server_tick=client_skill.server_tick,
                        player_position=position,
                        target_position=target_position,
                        aim_angle=client_skill.aim_angle,
                        source_actor_id=source_actor_id,
                        now=now,
                        attack_speed_multiplier=skill_speed_multiplier,
                        modifier_effect_ids=modifier_effect_ids,
                        eligible_actor_ids=eligible_actor_ids,
                        visible_actor_positions=visible_actor_positions,
                        explicit_target_actor_ids=explicit_target_actor_ids,
                        player_damage_range=player_damage_range,
                        player_critical_hit_chance=(
                            player_critical_hit_chance
                        ),
                        player_critical_damage_multiplier=(
                            player_critical_damage_multiplier
                        ),
                    )
                location_owner_effects = (
                    combat.player_skill_location_owner_effects(
                        client_skill.skill_index,
                        cast_id=client_skill.cast_id,
                        target_count=len({
                            impact.target.actor_id for impact in entity_plan.impacts
                        } if entity_plan is not None else {
                            hit.target_actor_id for hit in pending_hits
                        }),
                        modifier_effect_ids=modifier_effect_ids,
                    )
                )
                if summon_definition is not None:
                    location_visual_definitions = (
                        summon_definition.location_visual_effects
                    )
                else:
                    location_visual_definitions = (
                        combat.player_skill_location_visuals(
                            client_skill.skill_index,
                            modifier_sources=modifier_effect_ids,
                        )
                    )
                    location_visual_delay = (
                        combat.player_skill_impact_delay_seconds(
                            client_skill.skill_index,
                            attack_speed_multiplier=skill_speed_multiplier,
                            modifier_sources=modifier_effect_ids,
                        )
                    )

            timer_key = None
            if summon_definition is not None:
                if summon_position_world is None:
                    summon_position_world = (
                        encounter.coordinate_frame.wire_to_world(position)
                    )
                timer_key = (
                    "player-summon",
                    int(source_actor_id),
                    int(client_skill.cast_id),
                )
                # Construct the dormant handle before taking flow_lock; start
                # and cancellation also remain outside every domain lock.
                summon_timer = s.gameplay_deadline_timer(
                    summon_definition.spawn_delay_seconds,
                    CharacterService._resolve_player_summon,
                    args=(
                        s,
                        generation,
                        timer_key,
                        summon_definition,
                        int(source_actor_id),
                        summon_position_world,
                        float(client_skill.aim_angle),
                        character_level,
                        int(client_skill.server_tick),
                        player_damage_range,
                        tuple(modifier_effect_ids),
                    ),
                    kwargs={"round_token": CharacterService._pvp_round_token(s)},
                )
                summon_timer.daemon = True

            # Revalidate after the combat phase, then mutate only the timer
            # registry during this short session-lock critical section.
            with s.flow_lock:
                session_is_current = (
                    s.gameplay_combat_state is combat
                    and s.gameplay_chase_generation == generation
                )
                if session_is_current:
                    new_timers, replaced_timers = (
                        CharacterService.
                        _register_runtime_projectile_deadlines(
                            s,
                            pending_hits,
                            now=now,
                            generation=generation,
                        )
                    )
                    timers.extend(new_timers)
                    old_timers.extend(replaced_timers)
                    if summon_timer is not None:
                        old_timer = s.gameplay_projectile_timers.pop(
                            timer_key, None
                        )
                        if old_timer is not None:
                            old_timers.append(old_timer)
                        s.gameplay_projectile_timers[
                            timer_key
                        ] = summon_timer
            if not session_is_current:
                if summon_timer is not None:
                    summon_timer.cancel()
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] discarded spatial cast "
                    "because the gameplay combat state changed"
                )
                return
            if not pending_hits:
                rejection = (
                    "no living monster is inside the authored hit area"
                )
        if (
            combat is not None
            and position is not None
            and source_actor_id is not None
            and entity_plan is not None
        ):
            pvp_timers = CharacterService._prepare_pvp_skill_hits(
                s,
                combat,
                client_skill,
                hit_plan=entity_plan,
                target_snapshot=pvp_snapshot,
                source_position=position,
                source_actor_id=source_actor_id,
                player_damage_range=player_damage_range,
                player_critical_hit_chance=player_critical_hit_chance,
                player_critical_damage_multiplier=(
                    player_critical_damage_multiplier
                ),
                attack_speed_multiplier=skill_speed_multiplier,
                modifier_sources=modifier_effect_ids,
                explicit_target_actor_ids=explicit_target_actor_ids,
                target_position=target_position,
                now=now,
            )
        for timer in old_timers:
            timer.cancel()
        if (
            position is not None
            and source_actor_id is not None
            and s.gameplay_presence_callback is not None
        ):
            s.gameplay_presence_callback(
                s,
                (
                    "player_skill",
                    client_skill,
                    pending_hits[0] if pending_hits else None,
                    position,
                    source_actor_id,
                    tuple(modifier_effect_ids),
                    float(skill_speed_multiplier),
                ),
            )
        if combat is not None and position is not None and source_actor_id is not None:
            CharacterService._send_player_skill_status_effects(
                s,
                combat.player_skill_user_effects(
                    client_skill.skill_index,
                    cast_id=client_skill.cast_id,
                    trigger="skillstart",
                    modifier_effect_ids=modifier_effect_ids,
                )
                + CharacterService._item_set_user_proc_effects(
                    s, client_skill.skill_index, triggers=("skillstart",)
                ),
                server_tick=client_skill.server_tick,
                label="player-skillstart-status-effects",
                effect_position=(location_position_world if command_id == SHIFTED_SKILL_COMMAND else None),
            )
            if pending_hits or pvp_timers:
                CharacterService._send_attack_speed_bonus_stack(
                    s,
                    attack_speed=player_attack_speed,
                    server_tick=client_skill.server_tick,
                    label="player-attack-speed-bonus-stack",
                )
            CharacterService._send_player_skill_status_effects(
                s,
                location_owner_effects,
                server_tick=client_skill.server_tick,
                label="player-location-owner-status-effects",
            )
        if (
            combat is not None
            and trap_definition is not None
            and location_position_world is not None
            and source_actor_id is not None
        ):
            CharacterService._schedule_player_trap(
                s,
                combat,
                trap_definition,
                delay_seconds=location_visual_delay,
                cast_started_at=now,
                owner_actor_id=int(source_actor_id),
                skill_index=int(client_skill.skill_index),
                cast_id=int(client_skill.cast_id),
                server_tick=int(client_skill.server_tick),
                position=location_position_world,
                yaw=float(client_skill.aim_angle),
                player_damage_range=player_damage_range,
                critical_hit_chance=player_critical_hit_chance,
                critical_damage_multiplier=(
                    player_critical_damage_multiplier
                ),
                modifier_sources=tuple(modifier_effect_ids),
            )
        if (
            combat is not None
            and location_position_world is not None
            and source_actor_id is not None
            and location_visual_definitions
        ):
            CharacterService._schedule_player_location_visuals(
                s,
                combat,
                location_visual_definitions,
                position=location_position_world,
                yaw=float(client_skill.aim_angle),
                server_tick=int(client_skill.server_tick),
                owner_actor_id=int(source_actor_id),
                cast_id=int(client_skill.cast_id),
                delay_seconds=location_visual_delay,
                source_skill_index=int(client_skill.skill_index),
            )
        for timer in timers:
            timer.start()
        for timer in pvp_timers:
            timer.start()
        if summon_timer is not None:
            summon_timer.start()
        # Same re-arm as the bullet path: spatial/location casts are map
        # activity and must not depend on an ActorRequest to bring the
        # simulation owner back.
        if combat is not None:
            CharacterService._start_monster_chase(s)
        if (
            not pending_hits
            and not pvp_timers
            and summon_definition is None
            and trap_definition is None
        ):
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] accepted "
                f"{type(client_skill).__name__} index="
                f"{client_skill.skill_index}, but produced no hit: "
                f"{rejection}"
            )
            return
        if trap_definition is not None:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] scheduled native trap "
                f"{trap_definition.resource_id} from skill index="
                f"{client_skill.skill_index}, cast={client_skill.cast_id}"
            )
        elif summon_definition is not None:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] scheduled "
                f"{summon_definition.monster_template_id} from "
                f"{type(client_skill).__name__} index="
                f"{client_skill.skill_index}, cast={client_skill.cast_id}, "
                f"spawn_in={summon_definition.spawn_delay_seconds:.3f}s"
            )
        else:
            # A cast that found nothing to hit is ordinary -- a swing at empty
            # ground, or at an actor this player may not harm -- so the
            # summary has to survive an empty target list.
            impact_in = (
                f"{max(0.0, pending_hits[0].due_at - now):.3f}s"
                if pending_hits
                else "n/a"
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] scheduled "
                f"{type(client_skill).__name__} index="
                f"{client_skill.skill_index}, cast={client_skill.cast_id}, "
                f"targets={len(pending_hits)}, skill_speed="
                f"{skill_speed_multiplier:.3f}, player_damage="
                f"{player_damage_range[0]:.1f}-"
                f"{player_damage_range[1]:.1f}, impact_in={impact_in}"
            )
