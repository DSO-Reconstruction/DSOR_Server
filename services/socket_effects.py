"""Database-authored jewel and rune runtime event lifecycle."""

from __future__ import annotations

from dso_server.protocol.gameplay_protocol import (
    StatusEffectCommandBatch,
    encode_status_effect_command_batch,
)

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session


class SocketEffectService:
    """Dispatch cached socket procs and conditional transitions."""

    @staticmethod
    def _stop_socket_event_effects(
            s: Session, event: str, *, server_tick: int) -> None:
        """Stop running effects owned by one conditional socket event."""

        evaluation = getattr(
            s, "gameplay_socket_effect_evaluation", None
        )
        if evaluation is None:
            return
        effect_ids = {
            int(definition.effect_id)
            for rule in evaluation.rules_for_event(event)
            for definition in (
                rule.definition, *rule.nested_user_definitions
            )
        }
        if not effect_ids:
            return
        removed_entries = []
        timers = []
        with s.flow_lock:
            for instance_id, (entry, _expires_at) in tuple(
                s.gameplay_runtime_status_effects.items()
            ):
                if int(entry.effect_id) not in effect_ids:
                    continue
                s.gameplay_runtime_status_effects.pop(instance_id, None)
                timer = s.gameplay_projectile_timers.pop(
                    ("player-status-tick", int(instance_id)), None
                )
                if timer is not None:
                    timers.append(timer)
                removed_entries.append(replace(
                    entry,
                    field_0c=int(server_tick) & 0xFFFFFFFF,
                    field_10=0,
                    field_25=False,
                    field_26=False,
                    field_39=False,
                    has_details=False,
                    values=(),
                    field_38=False,
                    field_3c=0xFF,
                    shape_values=(),
                ))
        for timer in timers:
            timer.cancel()
        if not removed_entries:
            return
        effect = StatusEffect(False, tuple(removed_entries))
        s.send_reliable_ordered(
            encode_status_effect_batch(
                effect, actor_id=LOCAL_PLAYER_ACTOR_ID
            ),
            track=True,
            label=f"socket-{normalize_socket_trigger(event)}-stop",
        )
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(s, ("status_effect", effect))
        CharacterService._refresh_player_combat_vitals(s)

    @staticmethod
    def _update_low_health_socket_state(
            s: Session, *, current_health: int, maximum_health: int,
            server_tick: int, event_seed: int = 0) -> None:
        """Dispatch authored LowHealth / OFF:NotLowHealth transitions."""

        low_health = int(current_health) <= max(
            1, round(max(1, int(maximum_health)) / 3.0)
        )
        with s.flow_lock:
            key = ("threshold", "lowhealth")
            previous = bool(s.gameplay_socket_event_state.get(key, False))
            s.gameplay_socket_event_state[key] = low_health
        if low_health and not previous:
            CharacterService._dispatch_socket_event(
                s,
                "lowhealth",
                server_tick=int(server_tick),
                event_seed=int(event_seed),
            )
        elif previous and not low_health:
            from dso_server.services.item_set_events import stop_item_set_condition
            try:
                CharacterService._stop_socket_event_effects(
                    s, "lowhealth", server_tick=int(server_tick)
                )
            finally:
                stop_item_set_condition(
                    s, "notlowhealth", server_tick=int(server_tick)
                )

    @staticmethod
    def _socket_definition_key(definition) -> tuple:
        """Return the complete wire/runtime identity of one socket effect."""

        return (
            str(definition.resource_id),
            int(definition.effect_id),
            int(definition.duration_ticks),
            tuple(float(value) for value in definition.values),
            normalize_socket_trigger(definition.trigger),
            int(definition.delay_ticks),
            int(definition.max_stack_size),
            float(definition.aura_radius),
            float(definition.aura_height),
            str(definition.aura_shape),
            tuple(int(value) for value in definition.stop_effect_ids),
            tuple(sorted(str(value) for value in definition.modifier_kinds)),
        )

    @staticmethod
    def _unique_socket_definitions(definitions) -> tuple:
        unique = {}
        for definition in definitions:
            normalized = replace(definition, chance=1.0)
            unique.setdefault(
                CharacterService._socket_definition_key(normalized),
                normalized,
            )
        return tuple(unique.values())

    @staticmethod
    def _socket_status_commands_for_actors(
            s: Session, applications, *, server_tick: int):
        """Allocate retail create/remove transitions for socket recipients."""

        commands = []
        applied_at = time.perf_counter()
        with s.flow_lock:
            cache = s.gameplay_victim_status_effect_instances
            consumed_this_batch = set()
            for actor_id, position, definitions in applications:
                removals = []
                for definition in definitions:
                    duration = int(definition.duration_ticks)
                    if duration <= 0:
                        continue
                    effect_id = int(definition.effect_id)
                    key = (int(actor_id), effect_id)
                    if key in consumed_this_batch:
                        continue
                    created, removed, consumed = (
                        CharacterService._advance_status_effect_instance(
                            s,
                            cache,
                            key,
                            definition,
                            actor_id=int(actor_id),
                            source_actor_id=LOCAL_PLAYER_ACTOR_ID,
                            server_tick=int(server_tick),
                            applied_at=applied_at,
                            position=position,
                        )
                    )
                    commands.append(
                        StatusEffectServerCommand(
                            StatusEffect(False, (created,)),
                            int(actor_id),
                        )
                    )
                    if removed is not None:
                        removals.append(removed)
                    if consumed:
                        consumed_this_batch.add(key)
                if removals:
                    commands.append(
                        StatusEffectServerCommand(
                            StatusEffect(False, tuple(removals)),
                            int(actor_id),
                        )
                    )
        return tuple(commands)

    @staticmethod
    def _send_socket_status_command_batches(
            s: Session, commands, *, label: str,
            maximum_payload_bytes: int = FRAG_SIZE) -> int:
        """Send typed multi-actor status batches without RakNet splitting."""

        commands = tuple(commands)
        if not commands:
            return 0
        command_chunks = []
        current = []
        current_bits = 0
        for command in commands:
            single_packet = encode_status_effect_command_batch(
                StatusEffectCommandBatch((command,))
            )
            single_bits = int(single_packet[1])
            # Joining two typed batches removes one trailing/leading marker
            # pair (eight bits). This lets us choose exact MTU chunks without
            # repeatedly re-encoding the growing candidate packet.
            candidate_bits = (
                current_bits + single_bits - 8
                if current else single_bits
            )
            if (
                current
                and (candidate_bits + 7) // 8
                > int(maximum_payload_bytes)
            ):
                command_chunks.append(tuple(current))
                current = [command]
                current_bits = single_bits
            else:
                current.append(command)
                current_bits = candidate_bits
        if current:
            command_chunks.append(tuple(current))
        packets = tuple(
            encode_status_effect_command_batch(
                StatusEffectCommandBatch(chunk)
            )
            for chunk in command_chunks
        )
        for index, packet in enumerate(packets):
            s.send_reliable_ordered(
                packet,
                track=True,
                label=(
                    label
                    if len(packets) == 1
                    else f"{label}-{index + 1}-of-{len(packets)}"
                ),
            )
        return len(packets)

    @staticmethod
    def _send_socket_actor_applications(
            s: Session, applications, *, server_tick: int,
            label: str) -> int:
        commands = CharacterService._socket_status_commands_for_actors(
            s, applications, server_tick=server_tick
        )
        return CharacterService._send_socket_status_command_batches(
            s, commands, label=label
        )

    @staticmethod
    def _dispatch_socket_event_batch(
            s: Session, events, *, _depth: int = 0, _evaluation=None) -> tuple:
        """Resolve several authoritative events as one cast transaction.

        Every occurrence still advances its trigger counter and performs its
        own deterministic chance/cooldown evaluation.  Only the resulting
        applications are coalesced, preventing a fifteen-target cast from
        emitting fifteen copies of the same caster buff or aura packet.
        """

        occurrences = tuple(
            (
                normalize_socket_trigger(event),
                int(server_tick) & 0xFFFFFFFF,
                int(event_seed),
            )
            for event, server_tick, event_seed in events
        )
        if not occurrences:
            return ()
        empty_results = tuple(() for _ in occurrences)
        if _depth > 2:
            return empty_results
        # The same accepted block/pickup/health transition also belongs to
        # item sets, even if the wearer has no event-triggered socket item.
        from dso_server.services.item_set_events import (
            BRIDGED_EVENTS, dispatch_item_set_event,
        )
        for trigger, server_tick, event_seed in occurrences:
            if trigger in BRIDGED_EVENTS:
                dispatch_item_set_event(
                    s, trigger, server_tick=server_tick, event_seed=event_seed,
                )
        evaluation = _evaluation if _evaluation is not None else getattr(
            s, "gameplay_socket_effect_evaluation", None
        )
        if evaluation is None:
            return empty_results

        active_occurrences = []
        results = []
        with s.flow_lock:
            for trigger, server_tick, event_seed in occurrences:
                rules = tuple(evaluation.rules_for_event(trigger))
                if not rules:
                    if VERBOSE_PACKET_LOGGING:
                        print(
                            f"[{GAMEPLAY_SERVICE_NAME}] socket proc "
                            f"event={trigger!r}: no active rules"
                        )
                    results.append(())
                    continue
                state_key = ("event-count", trigger)
                event_count = int(
                    s.gameplay_socket_event_state.get(state_key, 0)
                ) + 1
                s.gameplay_socket_event_state[state_key] = event_count
                cooldown_deadlines = {
                    (
                        rule.socket_template_id,
                        int(rule.definition.effect_id),
                    ): float(
                        s.gameplay_socket_event_state.get(
                            (
                                "cooldown",
                                rule.socket_template_id,
                                int(rule.definition.effect_id),
                            ),
                            0.0,
                        )
                    )
                    for rule in rules
                    if float(rule.cooldown_seconds) > 0.0
                }
                event_now = time.perf_counter()
                passed = []
                for ordinal, rule in enumerate(rules):
                    chance = max(0.0, min(1.0, float(rule.chance)))
                    if (
                        chance <= 0.0
                        or not rule.operation.casefold().startswith("add")
                    ):
                        if VERBOSE_PACKET_LOGGING:
                            print(
                                f"[{GAMEPLAY_SERVICE_NAME}] socket proc "
                                f"event={trigger!r} "
                                f"jewel={rule.socket_template_id!r} "
                                f"effect={rule.definition.resource_id!r}: "
                                "not an add rule"
                            )
                        continue
                    cooldown_key = (
                        rule.socket_template_id,
                        int(rule.definition.effect_id),
                    )
                    if (
                        event_now
                        < cooldown_deadlines.get(cooldown_key, 0.0)
                    ):
                        if VERBOSE_PACKET_LOGGING:
                            remaining = (
                                cooldown_deadlines[cooldown_key] - event_now
                            )
                            print(
                                f"[{GAMEPLAY_SERVICE_NAME}] socket proc "
                                f"event={trigger!r} "
                                f"jewel={rule.socket_template_id!r} "
                                f"effect={rule.definition.resource_id!r}: "
                                f"cooldown remaining={remaining:.2f}s"
                            )
                        continue
                    roll_bits = (
                        int(server_tick) * 1_103_515_245
                        + int(event_seed) * 12_345
                        + int(rule.definition.effect_id) * 97_531
                        + event_count * 257
                        + ordinal * 2_654_435_761
                    ) & 0xFFFFFFFF
                    if (
                        chance < 1.0
                        and roll_bits / 0x1_0000_0000 >= chance
                    ):
                        if VERBOSE_PACKET_LOGGING:
                            print(
                                f"[{GAMEPLAY_SERVICE_NAME}] socket proc "
                                f"event={trigger!r} "
                                f"jewel={rule.socket_template_id!r} "
                                f"effect={rule.definition.resource_id!r}: "
                                f"chance miss ({chance:.3f})"
                            )
                        continue
                    passed.append(rule)
                    if VERBOSE_PACKET_LOGGING:
                        print(
                            f"[{GAMEPLAY_SERVICE_NAME}] socket proc "
                            f"event={trigger!r} "
                            f"jewel={rule.socket_template_id!r} "
                            f"effect={rule.definition.resource_id!r}: "
                            "passed; applying status effect"
                        )
                    if float(rule.cooldown_seconds) > 0.0:
                        s.gameplay_socket_event_state[
                            (
                                "cooldown",
                                rule.socket_template_id,
                                int(rule.definition.effect_id),
                            )
                        ] = event_now + float(rule.cooldown_seconds)
                passed_result = tuple(passed)
                results.append(passed_result)
                if passed_result:
                    active_occurrences.append(
                        (
                            trigger,
                            server_tick,
                            event_seed,
                            event_now,
                            passed_result,
                        )
                    )
        if not active_occurrences:
            return tuple(results)

        user_definitions = {}
        jewel_user_effect_ids = set()
        enemy_auras = {}
        ally_auras = {}
        location_applications = {}
        for (
            _trigger,
            server_tick,
            event_seed,
            _event_now,
            passed,
        ) in active_occurrences:
            for ordinal, rule in enumerate(passed):
                if rule.target == "user":
                    for definition in (
                        rule.definition, *rule.nested_user_definitions
                    ):
                        normalized = replace(definition, chance=1.0)
                        if rule.category.casefold() == "equipmentjewel":
                            jewel_user_effect_ids.add(
                                int(normalized.effect_id)
                            )
                        user_definitions.setdefault(
                            CharacterService._socket_definition_key(
                                normalized
                            ),
                            (normalized, server_tick, event_seed),
                        )
                    if (
                        rule.receiver_definitions
                        and rule.definition.aura_radius > 0.0
                    ):
                        definitions = (
                            CharacterService._unique_socket_definitions(
                                rule.receiver_definitions
                            )
                        )
                        aura_key = (
                            "user",
                            CharacterService._socket_definition_key(
                                replace(rule.definition, chance=1.0)
                            ),
                            tuple(
                                CharacterService._socket_definition_key(
                                    value
                                )
                                for value in definitions
                            ),
                        )
                        enemy_auras.setdefault(
                            aura_key,
                            (
                                float(rule.definition.aura_radius),
                                definitions,
                            ),
                        )
                for aura in rule.event_auras:
                    definitions = (
                        CharacterService._unique_socket_definitions(
                            aura.receiver_definitions
                        )
                    )
                    aura_definition = replace(
                        aura.definition, chance=1.0
                    )
                    aura_key = (
                        str(aura.target).casefold(),
                        CharacterService._socket_definition_key(
                            aura_definition
                        ),
                        tuple(
                            CharacterService._socket_definition_key(value)
                            for value in definitions
                        ),
                    )
                    value = (
                        definitions,
                        float(aura_definition.aura_radius),
                    )
                    if str(aura.target).casefold() == "allies":
                        ally_auras.setdefault(aura_key, value)
                    elif str(aura.target).casefold() in {
                        "enemy", "enemies"
                    }:
                        enemy_auras.setdefault(
                            aura_key,
                            (value[1], value[0]),
                        )
                if rule.target == "location":
                    definition = replace(rule.definition, chance=1.0)
                    receivers = (
                        CharacterService._unique_socket_definitions(
                            rule.receiver_definitions
                        )
                    )
                    location_key = (
                        CharacterService._socket_definition_key(definition),
                        tuple(
                            CharacterService._socket_definition_key(value)
                            for value in receivers
                        ),
                    )
                    location_applications.setdefault(
                        location_key,
                        (
                            definition,
                            receivers,
                            server_tick,
                            event_seed,
                            ordinal,
                        ),
                    )

        if user_definitions:
            definitions = tuple(
                value[0] for value in user_definitions.values()
            )
            CharacterService._send_player_skill_status_effects(
                s,
                definitions,
                server_tick=max(
                    value[1] for value in user_definitions.values()
                ),
                label="socket-cast-status-effects",
                # Socket procs refresh the current buff from its first
                # occurrence instead of accumulating stale stack state.
                force_fresh_effect_ids=jewel_user_effect_ids,
            )
            # _send_player_skill_status_effects already reconciles vitality
            # modifiers; a second refresh here invalidated equipment again.

        combat = s.gameplay_combat_state
        character = s.gameplay_character
        last_move = s.gameplay_last_move
        if combat is not None and character is not None:
            owner_actor_id = (
                s.gameplay_remote_actor_id
                or CharacterService._remote_actor_id(character.character_id)
            )
            position = (
                combat.encounter.coordinate_frame.wire_to_world(last_move.relative_position)
                if last_move is not None else tuple(s.gameplay_spawn_position)
            )
            actor_definitions = {}
            actor_positions = {}
            if enemy_auras:
                with combat.lock:
                    # Snapshot once. Each distinct authored aura walks this
                    # compact candidate tuple once, regardless of hit count.
                    candidates = tuple(
                        (
                            int(actor_id),
                            combat.monster_positions.get(int(actor_id)),
                        )
                        for actor_id in combat.living_actor_ids
                    )
                    for radius, definitions in enemy_auras.values():
                        for target_actor_id, target_position in candidates:
                            if (
                                target_position is None
                                or combat._distance(
                                    position, target_position
                                ) > float(radius)
                            ):
                                continue
                            grouped = actor_definitions.setdefault(
                                target_actor_id, {}
                            )
                            for definition in definitions:
                                grouped.setdefault(
                                    CharacterService._socket_definition_key(
                                        definition
                                    ),
                                    definition,
                                )
                            actor_positions[target_actor_id] = (
                                target_position
                            )
                    event_now = max(
                        value[3] for value in active_occurrences
                    )
                    for target_actor_id, grouped in (
                        actor_definitions.items()
                    ):
                        combat._record_monster_status_effects(
                            int(target_actor_id),
                            tuple(grouped.values()),
                            now=event_now,
                            source_actor_id=int(owner_actor_id),
                        )
            if actor_definitions:
                CharacterService._send_socket_actor_applications(
                    s,
                    tuple(
                        (
                            actor_id,
                            actor_positions[actor_id],
                            tuple(grouped.values()),
                        )
                        for actor_id, grouped
                        in actor_definitions.items()
                    ),
                    server_tick=max(
                        value[1] for value in active_occurrences
                    ),
                    label="socket-cast-aura-receivers",
                )
            if (
                ally_auras
                and last_move is not None
                and s.gameplay_presence_callback is not None
            ):
                s.gameplay_presence_callback(
                    s,
                    (
                        "socket_ally_auras",
                        tuple(ally_auras.values()),
                        tuple(last_move.relative_position),
                        max(value[1] for value in active_occurrences),
                    ),
                )
            for (
                definition,
                receivers,
                server_tick,
                event_seed,
                ordinal,
            ) in location_applications.values():
                location_damage_range = (1.0, 1.0)
                if _evaluation is not None:
                    from dso_server.services.endless_tower_buffs import tower_damage_range
                    location_damage_range = tower_damage_range(
                        combat.encounter.static_database_path, s,
                        CharacterService._active_skill_modifier_sources(s),
                    )
                CharacterService._schedule_player_location_visuals(
                    s,
                    combat,
                    (definition,),
                    position=position,
                    yaw=float(getattr(last_move, "yaw", 0.0)),
                    server_tick=int(server_tick) & 0xFFFFFFFF,
                    owner_actor_id=int(owner_actor_id),
                    cast_id=(
                        (
                            int(event_seed) << 8
                        ) ^ ordinal ^ definition.effect_id
                    ) & 0xFFFFFFFF,
                    delay_seconds=(
                        definition.delay_ticks / SERVER_TICKS_PER_SECOND
                    ),
                    receiver_status_effects=receivers,
                    player_damage_range=location_damage_range,
                )

        chained_events = {}
        with s.flow_lock:
            for definition, server_tick, event_seed in (
                user_definitions.values()
            ):
                for stopped_id in definition.stop_effect_ids:
                    for instance_id, (entry, _expires_at) in tuple(
                        s.gameplay_runtime_status_effects.items()
                    ):
                        if int(entry.effect_id) == int(stopped_id):
                            s.gameplay_runtime_status_effects.pop(
                                instance_id, None
                            )
                if int(definition.max_stack_size) <= 1:
                    continue
                running = CharacterService._running_player_status_effect(
                    s, definition.effect_id, now=time.perf_counter()
                )
                if (
                    running is not None
                    and int(running[1].field_18) + 1
                    >= int(definition.max_stack_size)
                ):
                    chained = (
                        f"maxstacksize{definition.resource_id}"
                    )
                    chained_events.setdefault(
                        chained,
                        (
                            chained,
                            int(server_tick),
                            int(event_seed) ^ 0x5A17,
                        ),
                    )
        if chained_events:
            CharacterService._dispatch_socket_event_batch(
                s,
                tuple(chained_events.values()),
                _depth=_depth + 1,
                _evaluation=_evaluation,
            )
        return tuple(results)

    @staticmethod
    def _dispatch_socket_event(
            s: Session, event: str, *, server_tick: int,
            event_seed: int = 0, _depth: int = 0) -> tuple:
        """Compatibility wrapper for PvP and other single-event callers."""

        return CharacterService._dispatch_socket_event_batch(
            s,
            ((event, server_tick, event_seed),),
            _depth=_depth,
        )[0]



__all__ = ["SocketEffectService"]
