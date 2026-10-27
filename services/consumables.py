"""Authoritative inventory consumables: buffs, tonics, and health potions."""

from __future__ import annotations
from dso_server.protocol.skill_book_protocol import experience_commands

from dso_server.protocol.gameplay_protocol import (
    StatusEffectCommandBatch,
    encode_status_effect_command_batch,
)
from dso_server.protocol.player_init_protocol import XP_CHANGED_COMMAND, XPChanged

from dso_server.gameplay.status_modifier_runtime import (
    active_xp_gain_multiplier,
    snapshot_active_status_entries,
)
from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.gameplay.player_skill_timing import (
    apply_skill_cooldown_effect, apply_skill_cooldown_effect_locked,
)
from dso_server.gameplay.status_modifier_runtime import load_status_modifier_catalog
from dso_server.services.player_progression import load_player_progression_catalog


class ConsumableService:
    """Apply database-authored consumables without hardcoded item lists."""

    @staticmethod
    def _active_consumable_entry(
            s: Session, instance_id: int) -> StatusEffectEntry | None:
        """Return an unexpired persistent or map-local consumable entry."""

        instance_id = int(instance_id)
        entry = s.gameplay_persistent_status_effects.get(instance_id)
        expires_at = s.gameplay_timed_status_effect_expiries.get(instance_id)
        if entry is not None and expires_at is not None:
            return entry if float(expires_at) > time.time() else None
        runtime = s.gameplay_runtime_status_effects.get(instance_id)
        if runtime is None:
            return None
        entry, expires_at = runtime
        return (
            entry
            if expires_at is None or float(expires_at) > time.perf_counter()
            else None
        )

    @staticmethod
    def _consumable_actor_id(s: Session) -> int:
        character = s.gameplay_character
        if character is None:
            return LOCAL_PLAYER_ACTOR_ID
        return int(
            s.gameplay_remote_actor_id
            or CharacterService._remote_actor_id(character.character_id)
        )

    @staticmethod
    def _consumable_cleanup_entry(
            entry: StatusEffectEntry, *, server_tick: int) -> StatusEffectEntry:
        """Build the no-details removal paired with a replacement create."""

        return replace(
            entry,
            field_0c=int(server_tick) & 0xFFFFFFFF,
            field_14=SERVER_TICKS_PER_SECOND,
            field_08=int(server_tick) & 0xFFFFFFFF,
            field_10=0,
            field_20=LOCAL_PLAYER_ACTOR_ID,
            field_25=False,
            field_26=False,
            field_39=True,
            has_details=False,
            values=(),
            field_38=False,
            field_3c=0xFF,
            shape_values=(),
        )

    @staticmethod
    def _apply_consumable_health(
            s: Session, modifiers: tuple[HealthModifier, ...],
            values: tuple[float, ...], *, server_tick: int,
            label: str) -> int:
        """Apply one authored potion heal and publish retail ActorStatsUpdate."""

        if not modifiers:
            return 0
        with s.flow_lock:
            combat = s.gameplay_combat_state
            inventory = s.gameplay_inventory
            character = s.gameplay_character
            if combat is None or inventory is None or character is None:
                return 0
            actor_id = CharacterService._consumable_actor_id(s)

        with combat.lock:
            health = combat.player_health.setdefault(
                actor_id,
                [combat.player_maximum_health, combat.player_maximum_health],
            )
            maximum = max(1, int(health[1]))
            requested = 0
            for modifier in modifiers:
                amount = max(0.0, modifier.resolve(values))
                requested += (
                    int(round(maximum * amount))
                    if modifier.relative
                    else int(round(amount))
                )
            current, maximum, recovered = combat.heal_player_amount(
                actor_id, requested
            )
        if recovered <= 0:
            return 0

        with s.flow_lock:
            if s.gameplay_inventory is None:
                return 0
            s.gameplay_inventory.health_points = float(current)
            current_resource = float(
                s.gameplay_inventory.secondary_resource
            )
            update_tick = max(
                int(server_tick),
                int(s.gameplay_last_heal_server_tick) + 1,
            ) & 0xFFFFFFFF
            s.gameplay_last_heal_server_tick = update_tick
        s.send_reliable_ordered(
            encode_actor_stats_update_batch(
                ActorStatsUpdate(current, current_resource),
                actor_id=LOCAL_PLAYER_ACTOR_ID,
            ),
            track=True,
            label=label,
        )
        CharacterService._update_low_health_socket_state(
            s,
            current_health=current,
            maximum_health=maximum,
            server_tick=update_tick,
            event_seed=recovered,
        )
        return recovered

    @staticmethod
    def _expire_consumable_status(
            s: Session, generation: int, timer_key,
            instance_id: int) -> None:
        refresh = False
        with s.flow_lock:
            if generation != s.gameplay_chase_generation:
                s.gameplay_projectile_timers.pop(timer_key, None)
                return
            if s.gameplay_persistent_status_effects.pop(
                int(instance_id), None
            ) is not None:
                refresh = True
            s.gameplay_timed_status_effect_expiries.pop(
                int(instance_id), None
            )
            if s.gameplay_runtime_status_effects.pop(
                int(instance_id), None
            ) is not None:
                refresh = True
            s.gameplay_projectile_timers.pop(timer_key, None)
        if refresh:
            CharacterService._refresh_player_combat_vitals(s)
            CharacterService._request_account_save(
                s, "consumable status expired"
            )

    @staticmethod
    def _tick_consumable_health(
            s: Session, generation: int, timer_key,
            instance_id: int, definition: ConsumableDefinition,
            remaining_ticks: int) -> None:
        with s.flow_lock:
            entry = CharacterService._active_consumable_entry(
                s, instance_id
            )
            if (
                not s.connected
                or generation != s.gameplay_chase_generation
                or entry is None
            ):
                s.gameplay_projectile_timers.pop(timer_key, None)
                return
            server_tick = max(
                int(s.gameplay_chase_server_tick),
                int(s.gameplay_last_heal_server_tick) + 1,
            ) & 0xFFFFFFFF
        CharacterService._apply_consumable_health(
            s,
            definition.periodic_health,
            definition.values,
            server_tick=server_tick,
            label="consumable-health-tick",
        )
        remaining_ticks -= 1
        if remaining_ticks <= 0:
            with s.flow_lock:
                s.gameplay_projectile_timers.pop(timer_key, None)
            return

        with s.flow_lock:
            if (
                not s.connected
                or generation != s.gameplay_chase_generation
                or CharacterService._active_consumable_entry(
                    s, instance_id
                ) is None
            ):
                s.gameplay_projectile_timers.pop(timer_key, None)
                return
            timer = s.gameplay_deadline_timer(
                definition.tick_interval_seconds,
                CharacterService._tick_consumable_health,
                args=(
                    s,
                    generation,
                    timer_key,
                    instance_id,
                    definition,
                    remaining_ticks,
                ),
            )
            timer.daemon = True
            s.gameplay_projectile_timers[timer_key] = timer
        timer.start()

    @staticmethod
    def _consumable_modifier_delta(
            program: str, kind: str, values: tuple[float, ...],
            *, base: float = 0.0) -> float:
        total = 0.0
        for encoded in str(program or '').split(';'):
            raw_kind, separator, raw_arguments = encoded.partition(':')
            if not separator or raw_kind.strip().casefold() != kind.casefold():
                continue
            arguments = tuple(
                token.strip() for token in raw_arguments.split(',')
                if token.strip()
            )
            if not arguments:
                continue
            raw_value = arguments[0].split('<', 1)[0].strip()
            try:
                if raw_value.startswith('$'):
                    index = int(raw_value[1:])
                    value = float(values[index])
                else:
                    value = float(raw_value)
            except (ValueError, IndexError):
                continue
            if any(
                token.casefold() == 'relative'
                for token in arguments[1:]
            ):
                total += float(base) * value
            else:
                total += value
        return total

    @staticmethod
    def _tick_consumable_xp(
            s: Session, generation: int, timer_key,
            instance_id: int, definition, database_path,
            remaining_ticks: int) -> None:
        with s.flow_lock:
            entry = CharacterService._active_consumable_entry(
                s, instance_id
            )
            character = s.gameplay_character
            points = tuple(s.gameplay_talent_category_points)
            if (
                not s.connected
                or generation != s.gameplay_chase_generation
                or entry is None
                or character is None
                or len(points) != 4
            ):
                s.gameplay_projectile_timers.pop(timer_key, None)
                return
            current_xp = max(
                int(character.progression_u32), int(points[0])
            )
            stack_count = min(
                max(1, int(entry.field_18) + 1),
                max(1, int(definition.max_stack_size)),
            )
            active_status_effects = snapshot_active_status_entries(
                s.gameplay_persistent_status_effects,
                s.gameplay_timed_status_effect_expiries,
                s.gameplay_runtime_status_effects,
                s.gameplay_item_set_effects,
                s.gameplay_socket_effects,
            )
        delta = CharacterService._consumable_modifier_delta(
            definition.tick_modifiers,
            'currxp',
            tuple(definition.values),
            base=float(current_xp),
        ) * stack_count
        xp_multiplier = active_xp_gain_multiplier(
            database_path, active_status_effects
        )
        awarded = max(0, int(math.floor(
            delta * xp_multiplier + 0.5
        )))
        if awarded <= 0:
            with s.flow_lock:
                s.gameplay_projectile_timers.pop(timer_key, None)
            return

        transition = load_player_progression_catalog(
            database_path
        ).advance_player(
            class_id=int(character.class_id),
            current_level=max(1, int(character.level)),
            current_xp=current_xp,
            amount=awarded,
        )
        if transition is None:
            with s.flow_lock:
                s.gameplay_projectile_timers.pop(timer_key, None)
            return

        with s.flow_lock:
            if (
                s.gameplay_character is not character
                or generation != s.gameplay_chase_generation
            ):
                s.gameplay_projectile_timers.pop(timer_key, None)
                return
            character.progression_u32 = transition.xp
            character.level = transition.level
            s.gameplay_talent_category_points = (
                transition.xp,
                int(points[1]),
                int(points[2]),
                int(points[3]),
            )
            s.gameplay_projectile_timers.pop(timer_key, None)
        packet, packet_bits = encode_server_command_batch(experience_commands(
            database_path, character.class_id, transition,
            actor_id=LOCAL_PLAYER_ACTOR_ID, quick_slots=s.gameplay_quick_slots,
        ), include_terminator=True)
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label='instant-consumable-xp'
        )
        if transition.level_up:
            CharacterService._refresh_player_combat_vitals(s)
            CharacterService._achievement_on_level_up(
                s, int(character.class_id), int(transition.level),
            )
        CharacterService._request_account_save(
            s, 'instant XP pickup', flush=True
        )

        remaining_ticks -= 1
        if remaining_ticks <= 0:
            return
        with s.flow_lock:
            if CharacterService._active_consumable_entry(
                s, instance_id
            ) is None:
                return
            timer = s.gameplay_deadline_timer(
                definition.tick_interval_seconds,
                CharacterService._tick_consumable_xp,
                args=(
                    s,
                    generation,
                    timer_key,
                    instance_id,
                    definition,
                    database_path,
                    remaining_ticks,
                ),
            )
            timer.daemon = True
            s.gameplay_projectile_timers[timer_key] = timer
        timer.start()

    @staticmethod
    def _schedule_consumable_timers(
            s: Session, instance_id: int,
            definition: ConsumableDefinition, *,
            database_path=DEFAULT_STATIC_DB_PATH) -> None:
        duration_seconds = (
            definition.duration_ticks / float(SERVER_TICKS_PER_SECOND)
        )
        with s.flow_lock:
            if definition.persistent:
                expires_at = s.gameplay_timed_status_effect_expiries.get(
                    int(instance_id)
                )
                clock = time.time()
            else:
                runtime = s.gameplay_runtime_status_effects.get(
                    int(instance_id)
                )
                expires_at = runtime[1] if runtime is not None else None
                clock = time.perf_counter()
            if expires_at is not None:
                duration_seconds = max(
                    0.0, float(expires_at) - clock
                )
            generation = s.gameplay_chase_generation
            expiry_key = ("consumable-expire", int(instance_id))
            expiry = s.gameplay_deadline_timer(
                duration_seconds + 0.10,
                CharacterService._expire_consumable_status,
                args=(s, generation, expiry_key, int(instance_id)),
            )
            expiry.daemon = True
            s.gameplay_projectile_timers[expiry_key] = expiry

            health_timer = None
            health_key = None
            if definition.periodic_health:
                interval = definition.tick_interval_seconds
                count = max(1, int(math.floor(
                    duration_seconds / interval + 1e-6
                )))
                health_key = ("consumable-health-tick", int(instance_id))
                initial_delay = (
                    0.0
                    if "instant" in definition.effect_resource_id.casefold()
                    else interval
                )
                health_timer = s.gameplay_deadline_timer(
                    initial_delay,
                    CharacterService._tick_consumable_health,
                    args=(
                        s,
                        generation,
                        health_key,
                        int(instance_id),
                        definition,
                        count,
                    ),
                )
                health_timer.daemon = True
                s.gameplay_projectile_timers[health_key] = health_timer
            xp_timer = None
            if 'currxp' in definition.modifier_kinds:
                interval = definition.tick_interval_seconds
                count = max(1, int(math.floor(
                    duration_seconds / interval + 1e-6
                )))
                first_delay = min(interval, duration_seconds)
                if (
                    "instant" in definition.effect_resource_id.casefold()
                    or duration_seconds <= interval
                ):
                    # A single-tick effect is due at the same moment it
                    # expires, so the tick finds the status entry already
                    # gone and returns without granting anything. Award it
                    # straight away, as the periodic health path does.
                    first_delay = 0.0
                xp_key = ('consumable-xp-tick', int(instance_id))
                xp_timer = s.gameplay_deadline_timer(
                    first_delay,
                    CharacterService._tick_consumable_xp,
                    args=(
                        s,
                        generation,
                        xp_key,
                        int(instance_id),
                        definition,
                        database_path,
                        count,
                    ),
                )
                xp_timer.daemon = True
                s.gameplay_projectile_timers[xp_key] = xp_timer
        expiry.start()
        if health_timer is not None:
            health_timer.start()
        if xp_timer is not None:
            xp_timer.start()

    def _restore_consumable_timers(self, s: Session) -> None:
        """Resume finite persistent effects after a map handoff or relog."""

        catalog = load_consumable_catalog(self.static_database_path)
        now_epoch = time.time()
        restored = []
        expired = []
        with s.flow_lock:
            for instance_id, expires_at in tuple(
                s.gameplay_timed_status_effect_expiries.items()
            ):
                entry = s.gameplay_persistent_status_effects.get(instance_id)
                if entry is None or float(expires_at) <= now_epoch:
                    s.gameplay_timed_status_effect_expiries.pop(
                        instance_id, None
                    )
                    s.gameplay_persistent_status_effects.pop(
                        instance_id, None
                    )
                    expired.append(instance_id)
                    continue
                definition = catalog.definition_for_effect(
                    entry.effect_id, tuple(entry.values)
                )
                if definition is None:
                    continue
                remaining_ticks = max(
                    1,
                    int(math.ceil(
                        (float(expires_at) - now_epoch)
                        * SERVER_TICKS_PER_SECOND
                    )),
                )
                restored.append((
                    int(instance_id),
                    replace(
                        definition,
                        duration_ticks=remaining_ticks,
                        values=tuple(entry.values),
                    ),
                ))
        if expired:
            CharacterService._refresh_player_combat_vitals(s)
        for instance_id, definition in restored:
            CharacterService._schedule_consumable_timers(
                s, instance_id, definition
            )

    @staticmethod
    def _install_instant_status_entry(
            s: Session, definition, catalog, *, server_tick: int,
            now: float, now_epoch: float):
        '''Install one authored pickup effect while the flow lock is held.'''

        active = []
        retired_timers = []
        for instance_id, entry in tuple(
            s.gameplay_persistent_status_effects.items()
        ):
            expires_at = s.gameplay_timed_status_effect_expiries.get(
                instance_id
            )
            if expires_at is not None and float(expires_at) <= now_epoch:
                s.gameplay_persistent_status_effects.pop(instance_id, None)
                s.gameplay_timed_status_effect_expiries.pop(
                    instance_id, None
                )
                continue
            entry_group = catalog.exclusive_group(entry.effect_id)
            same_effect = int(entry.effect_id) == int(definition.effect_id)
            same_group = bool(
                definition.exclusive_group
                and entry_group.casefold()
                == definition.exclusive_group.casefold()
            )
            if same_effect or same_group:
                active.append((int(instance_id), entry))
        for instance_id, (entry, expires_at) in tuple(
            s.gameplay_runtime_status_effects.items()
        ):
            if expires_at is not None and float(expires_at) <= now:
                s.gameplay_runtime_status_effects.pop(instance_id, None)
                continue
            entry_group = catalog.exclusive_group(entry.effect_id)
            same_effect = int(entry.effect_id) == int(definition.effect_id)
            same_group = bool(
                definition.exclusive_group
                and entry_group.casefold()
                == definition.exclusive_group.casefold()
            )
            if same_effect or same_group:
                active.append((int(instance_id), entry))

        if (
            definition.exclusivity_type.casefold() == 'deny'
            and active
        ):
            return None

        previous_same_record = next(
            (
                (active_instance_id, active_entry)
                for active_instance_id, active_entry in active
                if int(active_entry.effect_id) == int(definition.effect_id)
            ),
            None,
        )
        previous_same = (
            previous_same_record[1]
            if previous_same_record is not None else None
        )
        stack_index = min(
            (
                int(previous_same.field_18) + 1
                if previous_same is not None else 0
            ),
            max(0, int(definition.max_stack_size) - 1),
        )
        duration_ticks = max(1, int(definition.duration_ticks))
        end_tick = (
            int(server_tick) + duration_ticks
        ) & 0xFFFFFFFF
        preserved_persistent_expiry = None
        preserved_runtime_expiry = None
        if previous_same_record is not None and not definition.reset_run_time:
            previous_instance_id, previous_entry = previous_same_record
            previous_persistent_expiry = (
                s.gameplay_timed_status_effect_expiries.get(
                    previous_instance_id
                )
            )
            previous_runtime = s.gameplay_runtime_status_effects.get(
                previous_instance_id
            )
            if previous_persistent_expiry is not None:
                preserved_persistent_expiry = float(
                    previous_persistent_expiry
                )
                duration_ticks = max(
                    1,
                    int(math.ceil(
                        (preserved_persistent_expiry - now_epoch)
                        * SERVER_TICKS_PER_SECOND
                    )),
                )
                end_tick = int(previous_entry.field_0c) & 0xFFFFFFFF
            elif previous_runtime is not None and previous_runtime[1] is not None:
                preserved_runtime_expiry = float(previous_runtime[1])
                duration_ticks = max(
                    1,
                    int(math.ceil(
                        (preserved_runtime_expiry - now)
                        * SERVER_TICKS_PER_SECOND
                    )),
                )
                end_tick = int(previous_entry.field_0c) & 0xFFFFFFFF
        instance_id = int(s.gameplay_next_status_effect_instance_id)
        s.gameplay_next_status_effect_instance_id += 1
        entry = StatusEffectEntry(
            effect_id=int(definition.effect_id),
            field_04=instance_id,
            field_0c=end_tick,
            field_14=SERVER_TICKS_PER_SECOND,
            field_08=int(server_tick) & 0xFFFFFFFF,
            field_18=stack_index,
            field_10=duration_ticks,
            field_1c=100,
            field_20=LOCAL_PLAYER_ACTOR_ID,
            field_25=False,
            field_26=bool(stack_index > 0),
            field_39=False,
            has_details=True,
            values=tuple(definition.values),
        )
        removals = tuple(
            CharacterService._consumable_cleanup_entry(
                old_entry, server_tick=server_tick
            )
            for _old_instance_id, old_entry in active
        )
        for old_instance_id, _old_entry in active:
            s.gameplay_persistent_status_effects.pop(
                old_instance_id, None
            )
            s.gameplay_timed_status_effect_expiries.pop(
                old_instance_id, None
            )
            s.gameplay_runtime_status_effects.pop(old_instance_id, None)
            for prefix in (
                'player-status-tick',
                'consumable-health-tick',
                'consumable-xp-tick',
                'consumable-expire',
            ):
                timer = s.gameplay_projectile_timers.pop(
                    (prefix, old_instance_id), None
                )
                if timer is not None:
                    retired_timers.append(timer)

        duration_seconds = duration_ticks / float(SERVER_TICKS_PER_SECOND)
        if definition.persistent:
            s.gameplay_persistent_status_effects[instance_id] = entry
            s.gameplay_timed_status_effect_expiries[instance_id] = (
                preserved_persistent_expiry
                if preserved_persistent_expiry is not None
                else now_epoch + duration_seconds
            )
        else:
            s.gameplay_runtime_status_effects[instance_id] = (
                entry,
                (
                    preserved_runtime_expiry
                    if preserved_runtime_expiry is not None
                    else now + duration_seconds
                ),
            )
        return entry, removals, tuple(retired_timers), instance_id

    @staticmethod
    def _publish_instant_consumable_ally_receivers(
            s: Session, root_definition, applied, *, server_tick: int
    ) -> None:
        '''Apply authored aura receiver children to same-instance allies.'''

        receivers = {}
        for _entry, current, _instance_id in applied:
            if current is root_definition:
                continue
            if str(current.aura_targets).strip().casefold() != 'allies':
                continue
            hosts = frozenset(
                value.strip().casefold()
                for value in str(current.effect_hosts).replace(',', ';').split(';')
                if value.strip()
            )
            if 'receiver' not in hosts:
                continue
            runtime = current.runtime_definition()
            key = (
                int(runtime.effect_id),
                int(runtime.duration_ticks),
                tuple(float(value) for value in runtime.values),
            )
            receivers.setdefault(key, runtime)
        if not receivers:
            return

        with s.flow_lock:
            callback = s.gameplay_presence_callback
            move = s.gameplay_last_move
            source_position = (
                tuple(move.relative_position)
                if move is not None else (0.0, 0.0, 0.0)
            )
        if callback is None:
            return
        # A zero-radius hosted command aura is map-instance-wide. Presence
        # still limits delivery to grouped allies sharing the exact physical
        # map, logical access group and difficulty instance.
        callback(
            s,
            (
                'socket_ally_auras',
                ((tuple(receivers.values()), float('inf')),),
                source_position,
                int(server_tick) & 0xFFFFFFFF,
            ),
        )

    @staticmethod
    def _apply_instant_consumable_effect(
            s: Session, definition, *,
            database_path=DEFAULT_STATIC_DB_PATH,
            apply_health: bool = True) -> bool:
        '''Apply one auto-picked item effect and its immediate AddAE chain.'''

        if definition is None or STATUS_EFFECTS_DISABLED:
            return False
        catalog = load_consumable_catalog(database_path)
        cooldown_catalog = load_status_modifier_catalog(database_path)
        now = time.perf_counter()
        now_epoch = time.time()
        server_tick = int(s.gameplay_chase_server_tick) & 0xFFFFFFFF
        applied = []
        removals = []
        timers_to_cancel = []
        queue = [definition]
        with s.flow_lock:
            while queue:
                current = queue.pop(0)
                installed = CharacterService._install_instant_status_entry(
                    s,
                    current,
                    catalog,
                    server_tick=server_tick,
                    now=now,
                    now_epoch=now_epoch,
                )
                if installed is None:
                    if current is definition:
                        return False
                    continue
                entry, retired, timers, instance_id = installed
                apply_skill_cooldown_effect_locked(
                    s, entry, database_path, now=now, catalog=cooldown_catalog,
                )
                applied.append((entry, current, instance_id))
                removals.extend(retired)
                timers_to_cancel.extend(timers)
                for child in tuple(current.child_effects):
                    if (
                        child.activation_phase.casefold() != 'start'
                        or child.trigger.casefold() != 'immediate'
                        or int(child.delay_ticks) > 0
                        or random.random() > float(child.chance)
                    ):
                        continue
                    queue.append(child)

        for timer in timers_to_cancel:
            timer.cancel()
        commands = [
            StatusEffectServerCommand(
                StatusEffect(False, (entry,)),
                LOCAL_PLAYER_ACTOR_ID,
            )
            for entry, _definition, _instance_id in applied
        ]
        if removals:
            commands.append(StatusEffectServerCommand(
                StatusEffect(False, tuple(removals)),
                LOCAL_PLAYER_ACTOR_ID,
            ))
        effect_values = tuple(command.value for command in commands)
        if commands:
            s.send_reliable_ordered(
                encode_status_effect_command_batch(
                    StatusEffectCommandBatch(tuple(commands))
                ),
                track=True,
                label='instant-consumable-status-effect',
            )
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(
                s, ('status_effects', effect_values)
            )

        CharacterService._refresh_player_combat_vitals(s)
        CharacterService._apply_player_status_resource(
            s,
            tuple(
                current.runtime_definition()
                for _entry, current, _instance_id in applied
            ),
        )
        for _entry, current, instance_id in applied:
            if apply_health:
                CharacterService._apply_consumable_health(
                    s,
                    current.immediate_health,
                    tuple(current.values),
                    server_tick=server_tick,
                    label='instant-consumable-health',
                )
            CharacterService._schedule_consumable_timers(
                s,
                instance_id,
                current,
                database_path=database_path,
            )
        CharacterService._publish_instant_consumable_ally_receivers(
            s,
            definition,
            tuple(applied),
            server_tick=server_tick,
        )
        if any(current.persistent for _entry, current, _id in applied):
            CharacterService._request_account_save(
                s, 'instant consumable status applied'
            )
        return bool(applied)

    def _handle_consumable_item_use(
            self, s: Session, item_id: int) -> bool:
        """Return whether ``item_id`` was recognized as a consumable."""

        with s.flow_lock:
            inventory = copy.deepcopy(s.gameplay_inventory)
            next_item_id = int(s.gameplay_next_item_id)
        if inventory is None:
            return False
        item = next(
            (
                candidate
                for candidate in inventory.items
                if int(candidate.item_id) == int(item_id)
            ),
            None,
        )
        if item is None:
            return False
        definition = load_consumable_definition(
            self.static_database_path, item.template_id
        )
        if definition is None:
            return False
        if STATUS_EFFECTS_DISABLED:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected consumable "
                f"{item.template_id!r}: status effects are disabled"
            )
            return True

        catalog = load_consumable_catalog(self.static_database_path)
        now = time.perf_counter()
        now_epoch = time.time()
        server_tick = int(s.gameplay_chase_server_tick) & 0xFFFFFFFF
        timers_to_cancel = []
        with s.flow_lock:
            active = []
            for instance_id, entry in tuple(
                s.gameplay_persistent_status_effects.items()
            ):
                expires_at = s.gameplay_timed_status_effect_expiries.get(
                    instance_id
                )
                if expires_at is None:
                    continue
                if float(expires_at) <= now_epoch:
                    s.gameplay_persistent_status_effects.pop(
                        instance_id, None
                    )
                    s.gameplay_timed_status_effect_expiries.pop(
                        instance_id, None
                    )
                    continue
                entry_group = catalog.exclusive_group(entry.effect_id)
                same_effect = int(entry.effect_id) == definition.effect_id
                same_group = bool(
                    definition.exclusive_group
                    and entry_group.casefold()
                    == definition.exclusive_group.casefold()
                )
                if same_effect or same_group:
                    active.append((int(instance_id), entry))
            for instance_id, (entry, expires_at) in tuple(
                s.gameplay_runtime_status_effects.items()
            ):
                if expires_at is not None and float(expires_at) <= now:
                    s.gameplay_runtime_status_effects.pop(
                        instance_id, None
                    )
                    continue
                entry_group = catalog.exclusive_group(entry.effect_id)
                same_effect = int(entry.effect_id) == definition.effect_id
                same_group = bool(
                    definition.exclusive_group
                    and entry_group.casefold()
                    == definition.exclusive_group.casefold()
                )
                if same_effect or same_group:
                    active.append((int(instance_id), entry))

            if (
                definition.exclusivity_type.casefold() == "deny"
                and active
            ):
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] denied consumable "
                    f"{item.template_id!r}: active exclusive group "
                    f"{definition.exclusive_group!r}"
                )
                return True

            builder = InventoryMutationBuilder(inventory, next_item_id)
            builder.consume(item_id, 1, preserve_item_id=True)
            mutation = builder.finish()
            previous_same = next(
                (
                    entry for _instance_id, entry in active
                    if int(entry.effect_id) == definition.effect_id
                ),
                None,
            )
            stack_index = min(
                (
                    int(previous_same.field_18) + 1
                    if previous_same is not None else 0
                ),
                max(0, int(definition.max_stack_size) - 1),
            )
            instance_id = int(s.gameplay_next_status_effect_instance_id)
            s.gameplay_next_status_effect_instance_id += 1
            entry = StatusEffectEntry(
                effect_id=definition.effect_id,
                field_04=instance_id,
                field_0c=(server_tick + definition.duration_ticks)
                & 0xFFFFFFFF,
                field_14=SERVER_TICKS_PER_SECOND,
                field_08=server_tick,
                field_18=stack_index,
                field_10=definition.duration_ticks,
                field_1c=100,
                field_20=LOCAL_PLAYER_ACTOR_ID,
                field_25=False,
                field_26=bool(stack_index > 0),
                field_39=False,
                has_details=True,
                values=definition.values,
            )
            removals = tuple(
                CharacterService._consumable_cleanup_entry(
                    old_entry, server_tick=server_tick
                )
                for _old_instance_id, old_entry in active
            )
            for old_instance_id, _old_entry in active:
                s.gameplay_persistent_status_effects.pop(
                    old_instance_id, None
                )
                s.gameplay_timed_status_effect_expiries.pop(
                    old_instance_id, None
                )
                s.gameplay_runtime_status_effects.pop(
                    old_instance_id, None
                )
                for prefix in (
                    'consumable-xp-tick',
                    "player-status-tick",
                    "consumable-health-tick",
                    "consumable-expire",
                ):
                    timer = s.gameplay_projectile_timers.pop(
                        (prefix, old_instance_id), None
                    )
                    if timer is not None:
                        timers_to_cancel.append(timer)
            if definition.persistent:
                s.gameplay_persistent_status_effects[instance_id] = entry
                s.gameplay_timed_status_effect_expiries[instance_id] = (
                    now_epoch
                    + definition.duration_ticks
                    / float(SERVER_TICKS_PER_SECOND)
                )
            else:
                s.gameplay_runtime_status_effects[instance_id] = (
                    entry,
                    now
                    + definition.duration_ticks
                    / float(SERVER_TICKS_PER_SECOND),
                )
            s.gameplay_inventory = mutation.inventory
            s.gameplay_next_item_id = mutation.next_item_id

        for timer in timers_to_cancel:
            timer.cancel()
        commands = [
            StatusEffectServerCommand(
                StatusEffect(False, (entry,)),
                LOCAL_PLAYER_ACTOR_ID,
            )
        ]
        if removals:
            commands.append(StatusEffectServerCommand(
                StatusEffect(False, removals),
                LOCAL_PLAYER_ACTOR_ID,
            ))
        effect_values = tuple(command.value for command in commands)
        if commands:
            s.send_reliable_ordered(
                encode_status_effect_command_batch(
                    StatusEffectCommandBatch(tuple(commands))
                ),
                track=True,
                label="consumable-status-effect",
            )
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(
                s, ("status_effects", effect_values)
            )

        # Both stores are direct modifier sources. Only database-authored
        # persistent entries survive map/relog initialization.
        apply_skill_cooldown_effect(s, entry, self.static_database_path, now=now)
        CharacterService._refresh_player_combat_vitals(s)
        recovered = CharacterService._apply_consumable_health(
            s,
            definition.immediate_health,
            definition.values,
            server_tick=server_tick,
            label="consumable-health-immediate",
        )
        CharacterService._schedule_consumable_timers(
            s,
            instance_id,
            definition,
            database_path=self.static_database_path,
        )
        CharacterService._request_account_save(
            s, "consumable item used", flush=True
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] consumed {item.template_id!r}; "
            f"effect={definition.effect_resource_id!r}, "
            f"group={definition.exclusive_group!r}, "
            f"values={definition.values}, replaced={len(removals)}, "
            f"healed={recovered}"
        )
        return True


__all__ = ["ConsumableService"]
