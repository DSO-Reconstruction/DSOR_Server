"""Player-created summon and location-effect lifecycle service."""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.gameplay.pvp_match_state import MatchPhase
from dso_server.runtime.transport import Session


class PlayerSummonService:
    """Materialize, synchronize, expire, and despawn player summons."""

    LOCATION_VISUAL_REFRESH_SECONDS = 1.0

    @staticmethod
    def _activate_player_user_effect_children(
            s: Session, applied, *, server_tick: int, effect_position=None) -> None:
        """Materialize immediate AE/LE children after the user effect is accepted."""
        with s.flow_lock:
            combat = s.gameplay_combat_state
            character = s.gameplay_character
            move = s.gameplay_last_move
            if combat is None or character is None:
                return
            owner_actor_id = s.gameplay_remote_actor_id or CharacterService._remote_actor_id(character.character_id)
            position = effect_position
            if position is None:
                position = (
                    combat.encounter.coordinate_frame.wire_to_world(move.relative_position)
                    if move is not None else s.gameplay_spawn_position
                )
            yaw = float(getattr(move, "yaw", 0.0))
            inventory = s.gameplay_inventory
        if position is None:
            return
        children = tuple(
            (instance_id, aura, receivers, delay)
            for instance_id, definition in applied
            for aura, receivers, delay in combat.player_user_effect_location_auras((definition,))
        )
        if not children:
            return
        stats = calculate_player_combat_stats(
            combat.encounter.static_database_path, cache_owner=s,
            class_name=CLASS_NAMES.get(int(character.class_id), "mage"),
            level=max(1, int(character.level)), inventory=inventory,
            modifier_sources=CharacterService._active_skill_modifier_sources(s),
        )
        for instance_id, aura, receivers, delay in children:
            CharacterService._schedule_player_location_visuals(
                s, combat, (aura,), position=tuple(position[:3]), yaw=yaw,
                server_tick=server_tick, owner_actor_id=int(owner_actor_id),
                cast_id=int(instance_id), delay_seconds=delay,
                receiver_status_effects=receivers,
                player_damage_range=(stats.minimum_damage, stats.maximum_damage),
            )
        CharacterService._start_monster_chase(s)

    @staticmethod
    def _resolve_player_summon(
            s: Session, generation: int, timer_key,
            definition, owner_actor_id: int,
            position: tuple[float, float, float], yaw: float,
            level: int, server_tick: int,
            player_damage_range: tuple[float, float],
            modifier_sources: tuple[object, ...], *, round_token=None) -> None:
        """Materialize a delayed friendly construction from a ShiftedSkill."""

        now = time.perf_counter()
        expiry_timers = []
        cancelled_timers = []
        with s.flow_lock:
            s.gameplay_projectile_timers.pop(timer_key, None)
            combat = s.gameplay_combat_state
            if (
                not s.connected
                or generation != s.gameplay_chase_generation
                or combat is None
                or (round_token is not None and
                    round_token != CharacterService._pvp_round_token(s))
                or (s.gameplay_match is not None and s.gameplay_match.phase != MatchPhase.RUNNING)
            ):
                return
            # The summon is materialized after its authored construction
            # delay.  NewMonster.field_c4 is the spawn tick in retail, not
            # the earlier player-cast tick.  Stamping the actor with the
            # stale cast tick leaves sustained-skill animation playback
            # behind the session game clock.
            spawn_server_tick = (
                int(s.gameplay_chase_server_tick) & 0xFFFFFFFF
            )
            if spawn_server_tick == 0:
                spawn_server_tick = (
                    int(server_tick)
                    + round(
                        max(0.0, definition.spawn_delay_seconds) * 25.0
                    )
                ) & 0xFFFFFFFF
        with combat.lock:
            spawn = combat.spawn_player_summon(
                definition,
                owner_actor_id=owner_actor_id,
                position=position,
                yaw=yaw,
                level=level,
                server_tick=spawn_server_tick,
                cast_id=int(timer_key[2] if len(timer_key) > 2 else timer_key[-1]),
                player_damage_range=player_damage_range,
                modifier_sources=modifier_sources,
                now=now,
            )

        published = False
        with s.flow_lock:
            if (
                s.connected
                and generation == s.gameplay_chase_generation
                and s.gameplay_combat_state is combat
                and (round_token is None or round_token == CharacterService._pvp_round_token(s))
                and (s.gameplay_match is None or s.gameplay_match.phase == MatchPhase.RUNNING)
            ):
                for actor_id in spawn.discarded_actor_ids:
                    old_key = ('player-summon-expire', int(actor_id))
                    old_timer = s.gameplay_projectile_timers.pop(
                        old_key, None
                    )
                    if old_timer is not None:
                        cancelled_timers.append(old_timer)
                for summon in spawn.actors:
                    if summon.expires_at is None:
                        continue
                    expiry_key = (
                        'player-summon-expire',
                        int(summon.actor_id),
                    )
                    expiry_timer = s.gameplay_deadline_timer(
                        max(0.0, summon.expires_at - now),
                        CharacterService._expire_player_summon,
                        args=(
                            s,
                            generation,
                            expiry_key,
                            int(summon.actor_id),
                        ),
                    )
                    expiry_timer.daemon = True
                    s.gameplay_projectile_timers[expiry_key] = expiry_timer
                    expiry_timers.append(expiry_timer)
                published = True
        if not published:
            # A map/generation transition won the optimistic commit. The
            # actors belong to the detached combat state and must not linger.
            with combat.lock:
                for summon in spawn.actors:
                    combat.expire_player_summon(summon.actor_id)
            return
        for timer in cancelled_timers:
            timer.cancel()
        callback = s.gameplay_presence_callback
        if callback is not None:
            callback(s, ("player_summon_spawn", spawn))
        else:
            CharacterService._send_player_summon_spawn_to(s, spawn)
        for summon in spawn.actors:
            for aura, receivers in combat.player_status_minion_auras(summon.definition.monster_template_id):
                CharacterService._publish_player_location_visuals(
                    s, combat, (aura,), position=summon.position,
                    yaw=summon.yaw, server_tick=spawn_server_tick,
                    owner_actor_id=owner_actor_id, cast_id=summon.cast_id,
                    receiver_status_effects=receivers,
                    player_damage_range=player_damage_range,
                    managed_aura=True, source_actor_id=summon.actor_id,
                )
        for timer in expiry_timers:
            timer.start()
        # The owner's Minion auras (Black Dwarf) reach the new construction
        # and a live turret-duration bonus applies to its lifetime.
        from dso_server.services.minion_auras import request_minion_aura_sync
        request_minion_aura_sync(s)
        CharacterService._start_monster_chase(s)

    @staticmethod
    def _spawn_socket_status_minions(
            s: Session, status_definition, *, server_tick: int,
            position_override=None) -> None:
        """Materialize minions declared by a jewel/rune status program."""

        with s.flow_lock:
            combat = s.gameplay_combat_state
            character = s.gameplay_character
            last_move = s.gameplay_last_move
            if (
                combat is None
                or character is None
                or not s.connected
            ):
                return
            owner_actor_id = (
                s.gameplay_remote_actor_id
                or CharacterService._remote_actor_id(character.character_id)
            )
            position = (
                combat.encounter.coordinate_frame.wire_to_world(last_move.relative_position)
                if last_move is not None else tuple(s.gameplay_spawn_position)
            )
            if position_override is not None:
                position = tuple(float(v) for v in position_override[:3])
            generation = s.gameplay_chase_generation
            character_level = max(1, int(character.level))
            class_name = CLASS_NAMES.get(int(character.class_id), "mage")
            inventory = s.gameplay_inventory
            yaw = float(getattr(last_move, "yaw", 0.0))

        # Status-driven summons used to hold the session flow lock while
        # waiting for the shared combat lock and while calculating equipment
        # stats.  Keep the two lock domains disjoint: snapshot session state,
        # inspect combat, do immutable database work, and only then briefly
        # reacquire the flow lock to publish the prepared timers.
        with combat.lock:
            specs = combat.player_status_minion_specs(status_definition)
        if not specs:
            return
        modifier_sources = tuple(
            CharacterService._active_skill_modifier_sources(s)
        )
        stats = calculate_player_combat_stats(
            combat.encounter.static_database_path,
            cache_owner=s,
            class_name=class_name,
            level=character_level,
            inventory=inventory,
            modifier_sources=modifier_sources,
        )
        prepared_definitions = tuple(
            (ordinal, definition)
            for ordinal, (monster_id, amount, offset) in enumerate(specs)
            for definition in (
                load_socket_minion_definition(
                    combat.encounter.static_database_path,
                    monster_id,
                    amount,
                    offset,
                ),
            )
            if definition is not None
        )

        with s.flow_lock:
            if (
                not s.connected
                or s.gameplay_combat_state is not combat
                or s.gameplay_character is not character
                or s.gameplay_chase_generation != generation
            ):
                return
            timers = []
            replaced = []
            for ordinal, definition in prepared_definitions:
                timer_key = (
                    "socket-status-minion",
                    int(status_definition.effect_id),
                    int(server_tick) & 0xFFFFFFFF,
                    ordinal,
                )
                old = s.gameplay_projectile_timers.pop(timer_key, None)
                if old is not None:
                    replaced.append(old)
                timer = s.gameplay_deadline_timer(
                    0.0,
                    CharacterService._resolve_player_summon,
                    args=(
                        s,
                        generation,
                        timer_key,
                        definition,
                        int(owner_actor_id),
                        position,
                        yaw,
                        character_level,
                        int(server_tick) & 0xFFFFFFFF,
                        (stats.minimum_damage, stats.maximum_damage),
                        modifier_sources,
                    ),
                    kwargs={"round_token": CharacterService._pvp_round_token(s)},
                )
                timer.daemon = True
                s.gameplay_projectile_timers[timer_key] = timer
                timers.append(timer)
        for timer in replaced:
            timer.cancel()
        for timer in timers:
            timer.start()

    @staticmethod
    def _resolve_player_location_visuals(
            s: Session, generation: int, timer_key, combat, definitions, *,
            position: tuple[float, float, float], yaw: float,
            server_tick: int, owner_actor_id: int, cast_id: int,
            receiver_status_effects=(), source_skill_index: int = 0,
            player_damage_range: tuple[float, float] = (1.0, 1.0),
            round_token=None) -> None:
        """Publish a ground effect only when its cast reaches HitFrame."""

        with s.flow_lock:
            s.gameplay_projectile_timers.pop(timer_key, None)
            if (
                not s.connected
                or generation != s.gameplay_chase_generation
                or s.gameplay_combat_state is not combat
                or (round_token is not None and
                    round_token != CharacterService._pvp_round_token(s))
                or (s.gameplay_match is not None and s.gameplay_match.phase != MatchPhase.RUNNING)
            ):
                return
        CharacterService._publish_player_location_visuals(
            s,
            combat,
            definitions,
            position=position,
            yaw=yaw,
            server_tick=server_tick,
            owner_actor_id=owner_actor_id,
            cast_id=cast_id,
            receiver_status_effects=receiver_status_effects,
            source_skill_index=source_skill_index,
            player_damage_range=player_damage_range,
            round_token=round_token,
        )

    @staticmethod
    def _schedule_player_location_visuals(
            s: Session, combat, definitions, *,
            position: tuple[float, float, float], yaw: float,
            server_tick: int, owner_actor_id: int, cast_id: int,
            delay_seconds: float, receiver_status_effects=(),
            source_skill_index: int = 0,
            player_damage_range: tuple[float, float] = (1.0, 1.0),
            cast_started_at: float | None = None) -> None:
        """Align generated world VFX with the database-authored impact."""

        delay_seconds = max(0.0, float(delay_seconds))
        if delay_seconds <= 1e-6:
            CharacterService._publish_player_location_visuals(
                s,
                combat,
                definitions,
                position=position,
                yaw=yaw,
                server_tick=server_tick,
                owner_actor_id=owner_actor_id,
                cast_id=cast_id,
                receiver_status_effects=receiver_status_effects,
                source_skill_index=source_skill_index,
                player_damage_range=player_damage_range,
            )
            return
        timer_key = (
            "player-location-start",
            int(owner_actor_id),
            int(cast_id),
            tuple(int(effect.effect_id) for effect in definitions),
        )
        with s.flow_lock:
            generation = s.gameplay_chase_generation
            old_timer = s.gameplay_projectile_timers.pop(timer_key, None)
            timer = s.gameplay_deadline_timer(
                delay_seconds,
                CharacterService._resolve_player_location_visuals,
                args=(s, generation, timer_key, combat, definitions),
                kwargs={
                    "position": position,
                    "yaw": yaw,
                    "server_tick": (
                        int(server_tick)
                        + round(delay_seconds * SERVER_TICKS_PER_SECOND)
                    ) & 0xFFFFFFFF,
                    "owner_actor_id": int(owner_actor_id),
                    "cast_id": int(cast_id),
                    "receiver_status_effects": tuple(receiver_status_effects),
                    "source_skill_index": int(source_skill_index),
                    "player_damage_range": tuple(player_damage_range),
                    "round_token": CharacterService._pvp_round_token(s),
                },
            )
            if cast_started_at is not None:
                timer.absolute_deadline = float(cast_started_at) + delay_seconds
            timer.daemon = True
            s.gameplay_projectile_timers[timer_key] = timer
        if old_timer is not None:
            old_timer.cancel()
        timer.start()

    @staticmethod
    def _publish_player_location_visuals(
            s: Session, combat, definitions, *,
            position: tuple[float, float, float],
            yaw: float, server_tick: int,
            owner_actor_id: int, cast_id: int = 0,
            receiver_status_effects=(), source_skill_index: int = 0,
            player_damage_range: tuple[float, float] = (1.0, 1.0),
            round_token=None, managed_aura: bool = False,
            source_actor_id: int | None = None) -> None:
        """Create and synchronize generated command 0x003e visuals."""

        with combat.lock:
            if (round_token is not None and round_token != CharacterService._pvp_round_token(s)):
                return
            if s.gameplay_match is not None and s.gameplay_match.phase != MatchPhase.RUNNING:
                return
            visuals = combat.start_player_location_visuals(
                tuple(definitions),
                position=position,
                yaw=yaw,
                server_tick=server_tick,
                owner_actor_id=owner_actor_id,
                cast_id=cast_id,
                receiver_status_effects=tuple(receiver_status_effects),
                source_skill_index=source_skill_index,
                player_damage_range=player_damage_range,
                managed_aura=managed_aura, source_actor_id=source_actor_id,
            )
        if not visuals:
            return
        now = time.perf_counter()
        owned_visuals = tuple(
            visual for visual in visuals
            if int(visual.owner_actor_id) == int(owner_actor_id)
        )
        expiry_timer = None
        if owned_visuals:
            timer_key = (
                "player-location-expire",
                int(owner_actor_id),
            )
            with s.flow_lock:
                generation = s.gameplay_chase_generation
                old_timer = s.gameplay_projectile_timers.pop(
                    timer_key, None
                )
                expiry_timer = s.gameplay_deadline_timer(
                    max(
                        0.0,
                        min(
                            CharacterService.LOCATION_VISUAL_REFRESH_SECONDS,
                            min(
                                visual.expires_at
                                for visual in owned_visuals
                            ) - now,
                        ),
                    ),
                    CharacterService._expire_player_location_visuals,
                    args=(
                        s, generation, timer_key, combat,
                        int(owner_actor_id),
                    ),
                )
                expiry_timer.daemon = True
                s.gameplay_projectile_timers[timer_key] = expiry_timer
            if old_timer is not None:
                old_timer.cancel()
        callback = s.gameplay_presence_callback
        if callback is not None:
            callback(s, ("player_location_visuals", visuals))
        else:
            CharacterService._send_player_location_visuals_to(
                s, visuals
            )
        if expiry_timer is not None:
            expiry_timer.start()

    @staticmethod
    def _expire_player_location_visuals(
            s: Session, generation: int, timer_key, combat,
            owner_actor_id: int) -> None:
        """Sweep expired location effects and publish the new snapshot."""

        replacement = None
        with s.flow_lock:
            s.gameplay_projectile_timers.pop(timer_key, None)
            if (
                not s.connected
                or generation != s.gameplay_chase_generation
                or s.gameplay_combat_state is not combat
            ):
                return
        now = time.perf_counter()
        with combat.lock:
            visuals = combat.active_player_location_visuals(now=now)
        owned = tuple(
            visual for visual in visuals
            if int(visual.owner_actor_id) == int(owner_actor_id)
        )
        with s.flow_lock:
            if (
                not s.connected
                or generation != s.gameplay_chase_generation
                or s.gameplay_combat_state is not combat
            ):
                return
            if owned:
                replacement = s.gameplay_deadline_timer(
                    max(
                        0.0,
                        min(
                            CharacterService.LOCATION_VISUAL_REFRESH_SECONDS,
                            min(visual.expires_at for visual in owned) - now,
                        ),
                    ),
                    CharacterService._expire_player_location_visuals,
                    args=(
                        s, generation, timer_key, combat,
                        int(owner_actor_id),
                    ),
                )
                replacement.daemon = True
                s.gameplay_projectile_timers[timer_key] = replacement
        callback = s.gameplay_presence_callback
        if callback is not None:
            callback(s, ("player_location_visuals", visuals))
        else:
            CharacterService._send_player_location_visuals_to(s, visuals)
        if replacement is not None:
            replacement.start()

    @staticmethod
    def _expire_player_summon(
            s: Session, generation: int, timer_key,
            actor_id: int) -> None:
        """Begin a construction's authored despawn before unregistering it."""

        discard_timer = None
        client_actor_ids = ()
        location_visuals = None
        cancelled_timers = []
        with s.flow_lock:
            s.gameplay_projectile_timers.pop(timer_key, None)
            lifetime_key = ('player-summon-expire', int(actor_id))
            if timer_key != lifetime_key:
                lifetime_timer = s.gameplay_projectile_timers.pop(
                    lifetime_key,
                    None,
                )
                if lifetime_timer is not None:
                    cancelled_timers.append(lifetime_timer)
            combat = s.gameplay_combat_state
            valid = (
                s.connected
                and combat is not None
            )
        for timer in cancelled_timers:
            timer.cancel()
        if not valid:
            return

        with combat.lock:
            summon = combat.player_summons.get(int(actor_id))
            if summon is None:
                return
            client_actor_ids = summon.client_actor_ids
            despawn_duration = max(
                0.0,
                float(summon.definition.despawn_duration_seconds),
            )
            location_visuals = (
                combat.expire_player_location_visuals_matching(
                    summon.owner_actor_id,
                    tuple(
                        effect.resource_id
                        for effect in (
                            summon.definition.location_visual_effects
                        )
                    ),
                    cast_id=summon.cast_id,
                )
            )
            if not combat.expire_player_summon(
                actor_id, land_fired_bullets=timer_key == lifetime_key,
            ):
                return

        committed = False
        old_discard_timer = None
        if despawn_duration > 0.0:
            discard_key = (
                'player-summon-discard',
                int(actor_id),
            )
            discard_timer = s.gameplay_deadline_timer(
                despawn_duration,
                CharacterService._finish_player_summon_despawn,
                args=(
                    s,
                    generation,
                    discard_key,
                    client_actor_ids,
                ),
            )
            discard_timer.daemon = True
        with s.flow_lock:
            if (
                s.connected
                and s.gameplay_combat_state is combat
            ):
                if discard_timer is not None:
                    old_discard_timer = s.gameplay_projectile_timers.pop(
                        discard_key, None
                    )
                    s.gameplay_projectile_timers[discard_key] = discard_timer
                committed = True
        if old_discard_timer is not None:
            old_discard_timer.cancel()
        if not committed:
            if discard_timer is not None:
                discard_timer.cancel()
            return
        if location_visuals is not None:
            callback = s.gameplay_presence_callback
            if callback is not None:
                callback(s, ("player_location_visuals", location_visuals))
            else:
                CharacterService._send_player_location_visuals_to(
                    s, location_visuals
                )
        # Owner feedback of a Minion aura ends with its last admitted minion.
        from dso_server.services.minion_auras import request_minion_aura_sync
        request_minion_aura_sync(s)
        if discard_timer is not None:
            # The already-published LocationEffectInfo expires now and runs
            # the actor's authored teardown.  DiscardMonster follows only
            # after Monster.DespawnDuration, as it does in retail.
            discard_timer.start()
            return
        CharacterService._publish_player_summon_despawn(
            s, client_actor_ids
        )

    @staticmethod
    def _finish_player_summon_despawn(
            s: Session, generation: int, timer_key,
            client_actor_ids: tuple[int, ...]) -> None:
        """Unregister a summon after its visual despawn duration elapsed."""

        with s.flow_lock:
            s.gameplay_projectile_timers.pop(timer_key, None)
            if not s.connected:
                return
        CharacterService._publish_player_summon_despawn(
            s, client_actor_ids
        )

    @staticmethod
    def _publish_player_summon_despawn(
            s: Session, client_actor_ids: tuple[int, ...]) -> None:
        callback = s.gameplay_presence_callback
        if callback is not None:
            callback(
                s,
                (
                    "player_summon_despawn",
                    client_actor_ids,
                ),
            )
        else:
            CharacterService._send_player_summon_despawn_to(
                s, client_actor_ids
            )
