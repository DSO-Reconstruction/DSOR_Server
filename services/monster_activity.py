"""Monster activity packet batching and viewer-specific actor mapping."""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session


class MonsterActivityService:
    @staticmethod
    def _record_delivered_monster_movements(
            s: Session, combat, commands) -> None:
        """Remember transforms whose datagram was submitted for this viewer."""

        delivered = {
            int(command.actor_id): (
                combat.encounter.coordinate_frame.wire_to_world(
                    command.value.relative_position
                )
            )
            for command in commands
            if combat is not None and int(command.actor_id) in combat.monsters
        }
        with s.flow_lock:
            if combat is not None and s.gameplay_combat_state is not combat:
                return
            for command in commands:
                queued = s.gameplay_pending_monster_movements.get(int(command.actor_id))
                if queued is not None and (
                    (int(command.value.start_tick) - int(queued.value.start_tick)) & 0xFFFFFFFF
                ) < 0x80000000:
                    s.gameplay_pending_monster_movements.pop(int(command.actor_id), None)
            spawned = s.gameplay_spawned_monster_actor_ids
            s.gameplay_delivered_monster_positions.update(
                {
                    actor_id: position
                    for actor_id, position in delivered.items()
                    if actor_id in spawned
                }
            )

    @staticmethod
    def _send_monster_movement_batches(
            s: Session, commands, *, combat=None) -> None:
        """Send replaceable walking samples without adding reliable backlog.

        Final stops use ordered reliability because there may be no later
        movement to repair a lost stop. Unsent state is coalesced per actor;
        submitted walking samples never consume attack order slots or retries.
        """
        with s.flow_lock:
            if combat is not None and s.gameplay_combat_state is not combat:
                return
            if s.gameplay_pending_monster_movement_combat is not combat:
                s.gameplay_pending_monster_movements.clear()
                s.gameplay_pending_monster_movement_combat = combat
            pending = s.gameplay_pending_monster_movements
            pending.update((int(command.actor_id), command) for command in commands)
            if combat is not None:
                visible = s.gameplay_spawned_monster_actor_ids | s.gameplay_summon_actor_ids
                now = time.perf_counter()
                for actor_id in tuple(pending):
                    if (
                        actor_id not in visible
                        or now < combat.monster_motion_unblock_at.get(actor_id, 0.0)
                    ):
                        # The reliable skill batch already carries the cast
                        # origin. A retained chase/idle sample must not arrive
                        # afterwards and replace the attack animation.
                        pending.pop(actor_id, None)
            queued = tuple(pending.values())
        # Submit final positions first, so subsequent sequenced movement is
        # stamped after these essential boundaries. Never promote an entire
        # mixed batch to reliable just because one actor stopped.
        for reliable in (True, False):
            selected = tuple(command for command in queued
                             if (command.value.scalar_byte == 0) == reliable)
            for offset in range(0, len(selected), MONSTER_MOVEMENT_PACKET_BUDGET):
                if s.reliable_backlog_size() >= RELIABLE_BACKLOG_SOFT_LIMIT:
                    return
                batch = selected[offset:offset + MONSTER_MOVEMENT_PACKET_BUDGET]
                packet = encode_move_batch(MoveBatch(batch))
                if reliable:
                    sent = s.send_reliable_ordered(
                        packet, track=True, label="monster-movement-stop", skip_if_busy=True)
                else:
                    sent = s.send_unreliable_sequenced(
                        packet, label="monster-movement", skip_if_busy=True)
                if sent is None:
                    return
                CharacterService._record_delivered_monster_movements(s, combat, batch)

    @staticmethod
    def _resync_visible_monster_transforms(
            s: Session, combat, actor_ids, *, limit: int = 16) -> int:
        """Reliably repair a viewer whose replaceable move frames were skipped."""

        with s.flow_lock:
            if s.gameplay_combat_state is not combat:
                return 0
            tick = int(s.gameplay_chase_server_tick) & 0xFFFFFFFF
            spawned = set(s.gameplay_spawned_monster_actor_ids)
        wanted = tuple(
            actor_id
            for actor_id in dict.fromkeys(int(value) for value in actor_ids)
            if actor_id in spawned
        )[:max(1, int(limit))]
        if not wanted:
            return 0
        with combat.lock:
            commands = tuple(
                MoveServerCommand(
                    Move.from_values(
                        combat.encounter.coordinate_frame.world_to_wire(
                            combat.monster_positions[actor_id]
                        ),
                        1.0 if combat.monster_moving.get(actor_id) else 0.0,
                        combat.monster_yaws.get(actor_id, 0.0),
                        combat.monster_yaws.get(actor_id, 0.0),
                        tick,
                        0,
                    ),
                    actor_id,
                )
                for actor_id in wanted
                if actor_id in combat.monster_positions
                and not (
                    time.perf_counter()
                    < combat.monster_motion_unblock_at.get(actor_id, 0.0)
                )
            )
        if not commands:
            return 0
        for offset in range(
            0, len(commands), MONSTER_MOVEMENT_PACKET_BUDGET
        ):
            batch = commands[offset:offset + MONSTER_MOVEMENT_PACKET_BUDGET]
            s.send_reliable_ordered(
                encode_move_batch(MoveBatch(batch)),
                track=True,
                label="monster-transform-resync",
            )
            CharacterService._record_delivered_monster_movements(
                s, combat, batch
            )
        return len(commands)

    @staticmethod
    def _send_monster_activity_to(
        s: Session,
        combat,
        movements,
        attacks,
        return_home_transitions,
        resolutions,
        *,
        owner_moves=None,
    ) -> None:
        character = s.gameplay_character
        if character is not None:
            local_server_actor_id = (
                getattr(s, "gameplay_remote_actor_id", None)
                or CharacterService._remote_actor_id(character.character_id)
            )
            if any(
                int(resolution.target_actor_id) == local_server_actor_id
                and int(resolution.damage) > 0
                for resolution in resolutions
            ):
                CharacterService._cancel_mount_cast(
                    s, "interrupted by monster damage"
                )
                CharacterService._remove_mount_ride(
                    s, reason="interrupted by monster damage"
                )
        for transition in return_home_transitions:
            if transition.active:
                CharacterService._send_monster_return_home_transition(
                    s, transition
                )

        # The declared duration is how long the client animates toward this
        # position, so it has to match how often a replacement arrives. The
        # publisher samples an actor every
        # MONSTER_MOVEMENT_PUBLISH_INTERVAL_TICKS, and the server's own reach
        # model (`_bounded_published_monster_position`) treats exactly that
        # span as one publish worth of travel. The previous fixed 18 had no
        # recorded origin and was 4.5x that span, so the client animated at
        # roughly a fifth of the real speed and fell steadily behind the
        # authoritative position; the stop frame that ends an approach carries
        # duration 0, and closing that accumulated gap in one frame is the
        # jump players see instead of a monster walking the last stretch.
        move_commands = tuple(
            MoveServerCommand(
                Move.from_values(
                    movement.wire_position,
                    1.0 if movement.moving else 0.0,
                    movement.yaw,
                    movement.yaw,
                    movement.server_tick,
                    (
                        MONSTER_MOVEMENT_PUBLISH_INTERVAL_TICKS
                        if movement.moving
                        else 0
                    ),
                ),
                movement.actor_id,
            )
            for movement in movements
        )
        if attacks:
            skill_commands = []
            sustained_skill_commands = []
            for attack in attacks:
                if attack.definition is not None:
                    location_visuals = combat.player_skill_location_visuals(
                        attack.skill_index
                    )
                    if (
                        location_visuals
                        and combat.claim_monster_location_visual_cast(
                            attack.attacker_actor_id,
                            attack.skill_index,
                            attack.server_tick,
                        )
                    ):
                        impact_ticks = (
                            int(attack.impact_tick)
                            - int(attack.server_tick)
                        ) & 0xFFFFFFFF
                        if impact_ticks > 0x7FFFFFFF:
                            impact_ticks = 0
                        location_delay = (
                            impact_ticks / SERVER_TICKS_PER_SECOND
                        )
                        print(
                            f"[{GAMEPLAY_SERVICE_NAME}] scheduled monster "
                            f"location visual(s) actor="
                            f"{attack.attacker_actor_id} "
                            f"skill={attack.skill_index} "
                            f"cast={attack.cast_id} "
                            f"tick={attack.server_tick} "
                            f"count={len(location_visuals)} "
                            f"delay={location_delay:.3f}s"
                        )
                        # As a player's: a Shifted skill's ground effects
                        # where it falls, any other skill's at its caster,
                        # their OX/OZ ahead along its aim (BossSmithEarthqake's
                        # waves 2.5..12.5 m ahead of the smith, Balor's
                        # breath, Boss01_b_Skill03's flood around Khalys).
                        location_origin = (
                            attack.target_position
                            if attack.command_id == SHIFTED_SKILL_COMMAND
                            else attack.position
                        )
                        CharacterService._schedule_player_location_visuals(
                            s,
                            combat,
                            location_visuals,
                            position=tuple(
                                float(value)
                                for value in location_origin[:3]
                            ),
                            yaw=float(attack.aim_angle),
                            server_tick=attack.server_tick,
                            owner_actor_id=int(attack.attacker_actor_id),
                            cast_id=int(attack.server_tick),
                            delay_seconds=location_delay,
                            source_skill_index=int(attack.skill_index),
                            cast_started_at=(
                                float(attack.due_at) - location_delay
                            ),
                        )
                CharacterService._send_entity_status_effects(
                    s,
                    attack.user_status_effects,
                    actor_id=attack.attacker_actor_id,
                    server_tick=attack.server_tick,
                    label="monster-skillstart-status-effects",
                    position=tuple(
                        float(value) for value in attack.position[:3]
                    ),
                    yaw=float(attack.aim_angle),
                )
                target_actor_id = CharacterService._viewer_actor_id(
                    s, attack.target_actor_id
                )
                context = SkillExecutionContext(
                    impact_tick=(
                        attack.execution_context_tick
                        if attack.execution_context_tick is not None
                        else attack.impact_tick
                    ),
                    hit_frame=attack.hit_frame,
                    motion_unblock_frame=attack.motion_unblock_frame,
                    skill_unblock_frame=attack.skill_unblock_frame,
                    reserved=0,
                    position=attack.position,
                    predicted=False,
                )
                if attack.command_id == SKILL_COMMAND:
                    skill_commands.append(
                        SkillServerCommand(
                            SkillCast(
                                skill_level=attack.skill_level,
                                skill_index=attack.skill_index,
                                aim_angle=attack.aim_angle,
                                server_tick=attack.server_tick,
                                cast_id=attack.cast_id,
                            ),
                            attack.attacker_actor_id,
                            context,
                        )
                    )
                elif attack.command_id == SHIFTED_SKILL_COMMAND:
                    definition = attack.definition
                    target_position = tuple(
                        float(value)
                        for value in attack.target_position[:3]
                    )
                    bullet_count = max(
                        1,
                        int(
                            getattr(definition, "bullet_count", 1)
                            if definition is not None else 1
                        ),
                    )
                    points = (
                        target_position,
                        *(
                            tuple(float(value) for value in point[:3])
                            for point in getattr(
                                attack, "extra_target_positions", ()
                            )
                        ),
                    )
                    skill_commands.append(
                        ShiftedSkillServerCommand(
                            ShiftedSkill(
                                skill_level=attack.skill_level,
                                skill_index=attack.skill_index,
                                aim_angle=attack.aim_angle,
                                server_tick=attack.server_tick,
                                cast_id=attack.cast_id,
                                variant=1,
                                target_position=target_position,
                                # Native multi-projectile ground skills read
                                # one point per authored bullet. Keep the point
                                # array aligned with BulletCount so the client
                                # never indexes past the supplied positions:
                                # the cast's own points
                                # (_monster_shifted_points), repeated when
                                # fewer players than bullets are in reach.
                                additional_target_positions=tuple(
                                    points[index % len(points)]
                                    for index in range(1, bullet_count)
                                ),
                            ),
                            attack.attacker_actor_id,
                            context,
                            max(1, int(definition.hit_frame))
                            if definition is not None else 1,
                        )
                    )
                elif attack.command_id == TARGET_SKILL_COMMAND:
                    skill_commands.append(
                        TargetSkillServerCommand(
                            TargetSkill(
                                skill_level=attack.skill_level,
                                skill_index=attack.skill_index,
                                aim_angle=attack.aim_angle,
                                server_tick=attack.server_tick,
                                cast_id=attack.cast_id,
                                target_actor_id=target_actor_id,
                                context=context,
                            ),
                            attack.attacker_actor_id,
                        )
                    )
                elif attack.command_id == BULLET_SKILL_COMMAND:
                    skill_commands.append(
                        BulletSkillServerCommand(
                            BulletSkillServer(
                                skill_level=attack.skill_level,
                                skill_index=attack.skill_index,
                                aim_angle=attack.aim_angle,
                                server_tick=attack.server_tick,
                                cast_id=attack.cast_id,
                                direction=attack.direction,
                                variant=attack.variant,
                            ),
                            attack.attacker_actor_id,
                            context,
                        )
                    )
                elif attack.command_id == TARGET_BULLET_SKILL_COMMAND:
                    bullet_count = max(1, getattr(attack, "bullet_count", 1))
                    if bullet_count == 1 and attack.definition is not None:
                        bullet_count = max(
                            1, getattr(attack.definition, "bullet_count", 1)
                        )
                    additional_targets = ()
                    if bullet_count > 1 and target_actor_id > 0:
                        additional_targets = (target_actor_id,) * (
                            bullet_count - 1
                        )
                    target_position = tuple(
                        float(value)
                        for value in attack.target_position[:3]
                    )
                    skill_commands.append(
                        TargetBulletSkillServerCommand(
                            TargetBulletSkill(
                                skill_level=attack.skill_level,
                                skill_index=attack.skill_index,
                                aim_angle=attack.aim_angle,
                                server_tick=attack.server_tick,
                                cast_id=attack.cast_id,
                                direction=attack.direction,
                                variant=attack.variant,
                                target_actor_id=target_actor_id,
                                additional_target_actor_ids=additional_targets,
                                target_positions=(target_position,)
                                * bullet_count,
                            ),
                            attack.attacker_actor_id,
                            context,
                        )
                    )
                elif attack.command_id == SUSTAINED_SKILL_COMMAND:
                    summon = combat.player_summons.get(
                        int(attack.attacker_actor_id)
                    )
                    if summon is None:
                        continue
                    owner_actor_id = int(summon.owner_actor_id)
                    owner_move = (
                        owner_moves.get(owner_actor_id)
                        if owner_moves is not None
                        else None
                    )
                    if owner_move is None:
                        character = s.gameplay_character
                        if (
                            character is not None
                            and (
                                getattr(s, "gameplay_remote_actor_id", None)
                                or CharacterService._remote_actor_id(
                                    character.character_id
                                )
                            )
                            == owner_actor_id
                        ):
                            owner_move = s.gameplay_last_move
                    if owner_move is None:
                        # Retail frames the sustained command with its owner's
                        # movement snapshot, never with the turret actor.
                        print(
                            f"[{GAMEPLAY_SERVICE_NAME}] suppressed "
                            "SustainedSkill playback for summon "
                            f"{attack.attacker_actor_id}: owner "
                            f"{owner_actor_id} has no movement snapshot"
                        )
                        continue
                    sustained_skill_commands.append(
                        (
                            MoveServerCommand(
                                replace(
                                    owner_move,
                                    start_tick=(
                                        int(attack.server_tick) - 1
                                    )
                                    & 0xFFFFFFFF,
                                    duration_ticks=0,
                                ),
                                CharacterService._viewer_actor_id(
                                    s, owner_actor_id
                                ),
                            ),
                            SustainedSkillServerCommand(
                                SkillCast(
                                    skill_level=attack.skill_level,
                                    skill_index=attack.skill_index,
                                    aim_angle=attack.aim_angle,
                                    server_tick=attack.server_tick,
                                    cast_id=attack.cast_id,
                                    # This client decodes directly into the
                                    # repeated sustained header. A marker here
                                    # shifts every following field by one bit.
                                    derived_flag=SKILL_COMMAND_DERIVED_FLAG,
                                ),
                                attack.attacker_actor_id,
                                context,
                                attack.loop_start_frame,
                                attack.sustained_hit_range,
                            ),
                        )
                    )

            if skill_commands:
                skill_actor_ids = {
                    int(command.actor_id) for command in skill_commands
                }
                stationary_cast_ids = {
                    int(attack.attacker_actor_id)
                    for attack in attacks
                    if (
                        attack.motion_unblock_frame > 0
                        and int(attack.attacker_actor_id) in combat.monsters
                    )
                }
                attack_moves = [
                    command
                    for command in move_commands
                    if (
                        int(command.actor_id) in skill_actor_ids
                        and int(command.actor_id) not in stationary_cast_ids
                    )
                ]
                framed_actor_ids = {
                    int(command.actor_id) for command in attack_moves
                }
                # Pair stationary casts with a stop at the captured cast origin.
                # A chase snapshot from earlier in this tick must not keep the
                # client walking through its animation, nor may delivery read a
                # transform that has since advanced on another simulation tick.
                for attack in attacks:
                    attacker_actor_id = int(attack.attacker_actor_id)
                    if (
                        attacker_actor_id not in skill_actor_ids
                        or attacker_actor_id in framed_actor_ids
                    ):
                        continue
                    if attacker_actor_id not in combat.monsters:
                        continue
                    world_position = tuple(attack.position[:3])
                    attack_moves.append(
                        MoveServerCommand(
                            Move.from_values(
                                combat.encounter.coordinate_frame.world_to_wire(
                                    world_position
                                ),
                                0.0,
                                attack.aim_angle,
                                attack.aim_angle,
                                # The stop precedes the skill on the actor's
                                # timeline as well as in packet order. Giving
                                # both commands the same tick lets the idle
                                # transition supersede the cast animation.
                                (int(attack.server_tick) - 1) & 0xFFFFFFFF,
                                0,
                            ),
                            attacker_actor_id,
                        )
                    )
                    framed_actor_ids.add(attacker_actor_id)
                ambient_moves = tuple(
                    command
                    for command in move_commands
                    if int(command.actor_id) not in skill_actor_ids
                )
                s.send_reliable_ordered(
                    encode_monster_attack_batch(
                        MonsterAttackBatch(tuple(attack_moves), tuple(skill_commands))
                    ),
                    track=True,
                    label="monster-skill",
                )
                CharacterService._record_delivered_monster_movements(
                    s, combat, attack_moves
                )
                # Those stops sit one tick before their cast, so the record
                # above keeps a chase queued on the cast's own tick; resent
                # after the cast, it would walk the caster through it.
                with s.flow_lock:
                    pending = s.gameplay_pending_monster_movements
                    for attack in attacks:
                        actor_id = int(attack.attacker_actor_id)
                        queued = pending.get(actor_id)
                        if (
                            actor_id in framed_actor_ids
                            and queued is not None
                            and (
                                (int(attack.server_tick)
                                 - int(queued.value.start_tick)) & 0xFFFFFFFF
                            ) < 0x80000000
                        ):
                            pending.pop(actor_id, None)
                if ambient_moves:
                    CharacterService._send_monster_movement_batches(
                        s, ambient_moves, combat=combat
                    )
            elif move_commands:
                CharacterService._send_monster_movement_batches(
                    s, move_commands, combat=combat
                )

            # Retail does not mix SustainedSkillCommand with ordinary monster
            # attacks. Each command is the last command in its own batch after
            # the owner player's MoveCommand.
            for sustained_move, sustained_command in sustained_skill_commands:
                encoded = encode_monster_attack_batch(
                    MonsterAttackBatch(
                        (sustained_move,),
                        (sustained_command,),
                    )
                )
                decoded = decode_monster_attack_batch(*encoded)
                decoded_command = decoded.attacks[0]
                if (
                    len(decoded.movements) != 1
                    or len(decoded.attacks) != 1
                    or not isinstance(
                        decoded_command, SustainedSkillServerCommand
                    )
                    or decoded_command.value.skill_index
                    != sustained_command.value.skill_index
                    or decoded_command.actor_id != sustained_command.actor_id
                    or encoded[1] != SUSTAINED_SUMMON_BATCH_BITS
                ):
                    raise ValueError(
                        "invalid retail-framed SustainedSkillCommand"
                    )
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] sending sustained summon "
                    f"batch: bits={encoded[1]}, "
                    f"move_actor={sustained_move.actor_id}, "
                    f"skill_actor={sustained_command.actor_id}, "
                    f"skill_index={sustained_command.value.skill_index}, "
                    f"tick={sustained_command.value.server_tick}"
                )
                s.send_reliable_ordered(
                    encoded,
                    track=True,
                    label="player-summon-sustained-skill",
                )
        elif move_commands:
            # Movement is replaceable state. Use the same bounded, non-blocking
            # path as ambient movement accompanying attacks; otherwise the map
            # owner's 25 Hz worker can wait behind one viewer's outbound lock
            # and delay movement and hit detection for the whole instance.
            CharacterService._send_monster_movement_batches(
                s, move_commands, combat=combat
            )

        # Retry retained final positions even when this tick produced no new
        # movement. Stops must survive congestion without another cast/move.
        if s.gameplay_pending_monster_movements:
            CharacterService._send_monster_movement_batches(s, (), combat=combat)
        for transition in return_home_transitions:
            if not transition.active:
                CharacterService._send_monster_return_home_transition(
                    s, transition
                )
        for resolution in resolutions:
            mapped = replace(
                resolution,
                target_actor_id=CharacterService._viewer_actor_id(
                    s, resolution.target_actor_id
                ),
                attacker_actor_id=CharacterService._viewer_actor_id(
                    s, resolution.attacker_actor_id
                ),
                source_actor_id=(
                    CharacterService._viewer_actor_id(
                        s, resolution.source_actor_id
                    )
                    if resolution.source_actor_id is not None
                    else None
                ),
            )
            CharacterService._send_combat_resolution(
                s, mapped, label="monster-hit-player"
            )
