"""Delayed authoritative player projectile impact resolution."""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session

class PlayerProjectileService:
    @staticmethod
    def _player_impact_socket_events(
            pending, resolution, *, source_actor_id: int,
            low_health: bool, low_health_casts: set) -> tuple:
        """Describe proc events for one impact without sending packets.

        Hit/critical/kill describe the attacker's impact on a target.
        HitFrost is wearer-side (the wearer was hit while frozen), so it is
        dispatched from the incoming-damage path instead. HitLowHealth is a
        cast event and is emitted only for the first resolved target of a cast.
        """

        event_seed = (
            int(pending.cast_id)
            + int(pending.projectile_index) * 257
            + int(pending.impact_index)
        )
        skill_index = int(pending.skill_index)
        hit_position = tuple(float(value) for value in resolution.position)
        damage_range = (
            max(0.0, float(pending.player_minimum_damage)),
            max(0.0, float(pending.player_maximum_damage)),
        )
        if not any(damage_range):
            damage_range = None
        events = [(
            "hit", resolution.server_tick, event_seed, skill_index,
            hit_position, damage_range,
        )]
        cast_key = (int(source_actor_id), int(pending.cast_id))
        if low_health and cast_key not in low_health_casts:
            low_health_casts.add(cast_key)
            events.append(
                (
                    "hitlowhealth", resolution.server_tick, event_seed,
                    skill_index, hit_position, damage_range,
                )
            )
        if resolution.critical_hit:
            events.append(
                (
                    "hitcritical", resolution.server_tick, event_seed,
                    skill_index, hit_position, damage_range,
                )
            )
        if resolution.killed:
            events.append((
                "kill", resolution.server_tick, event_seed, skill_index,
                hit_position, damage_range,
            ))
        return tuple(events)

    @staticmethod
    def _resolve_player_projectile_group(
            s: Session, generation: int, timer_keys,
            due_at: float) -> None:
        """Resolve one equal-deadline impact group through one callback.

        The logical keys remain individually addressable in the session, but
        a multi-target cast wakes the bounded scheduler only once.  Resolve
        through the existing single-impact entry point so packet ordering,
        loot, status effects, and exact-deadline batching stay centralized.
        """

        ordered_keys = tuple(timer_keys)
        replacement_timer = None
        selected_key = None
        with s.flow_lock:
            combat_candidate = s.gameplay_combat_state
            if combat_candidate is None:
                for timer_key in ordered_keys:
                    s.gameplay_projectile_timers.pop(timer_key, None)
                return
            if (
                not s.connected
                or generation != s.gameplay_chase_generation
            ):
                return
        now = time.perf_counter()
        with combat_candidate.lock:
            matching_keys = tuple(
                timer_key
                for timer_key in ordered_keys
                if (
                    (
                        pending := combat_candidate.
                        pending_runtime_player_hits.get(timer_key)
                    ) is not None
                    and abs(float(pending.due_at) - float(due_at)) <= 1e-9
                )
            )
            missing_keys = tuple(
                timer_key
                for timer_key in ordered_keys
                if timer_key not in combat_candidate.pending_runtime_player_hits
            )
            if not matching_keys:
                remaining = None
            else:
                remaining = float(due_at) - now

        if not matching_keys:
            with s.flow_lock:
                if (
                    s.connected
                    and generation == s.gameplay_chase_generation
                    and s.gameplay_combat_state is combat_candidate
                ):
                    for timer_key in missing_keys:
                        s.gameplay_projectile_timers.pop(timer_key, None)
            return

        if remaining is not None and remaining > 0.001:
            replacement_timer = s.gameplay_deadline_timer(
                remaining,
                CharacterService._resolve_player_projectile_group,
                args=(
                    s,
                    generation,
                    matching_keys,
                    float(due_at),
                ),
            )
            replacement_timer.daemon = True
            installed = False
            with s.flow_lock:
                if (
                    s.connected
                    and generation == s.gameplay_chase_generation
                    and s.gameplay_combat_state is combat_candidate
                ):
                    for timer_key in matching_keys:
                        s.gameplay_projectile_timers[timer_key] = (
                            replacement_timer
                        )
                    installed = True
            if not installed:
                replacement_timer.cancel()
                return
        else:
            with s.flow_lock:
                if (
                    not s.connected
                    or generation != s.gameplay_chase_generation
                    or s.gameplay_combat_state is not combat_candidate
                ):
                    return
            selected_key = matching_keys[0]
        if replacement_timer is not None:
            replacement_timer.start()
            return
        if selected_key is None:
            return
        CharacterService._resolve_player_projectile(
            s,
            generation,
            selected_key[1],
            selected_key[0],
            selected_key[2],
        )

    @staticmethod
    def _resolve_player_projectile(
            s: Session, generation: int, cast_id: int,
            source_actor_id: int | None = None,
            impact_index: int = 0) -> None:
        """Apply one queued player hit when its database travel time elapses."""

        now = time.perf_counter()
        replacement_timer = None
        timer_key = (
            (int(source_actor_id), int(cast_id), int(impact_index))
            if source_actor_id is not None
            else int(cast_id)
        )
        with s.flow_lock:
            combat_candidate = s.gameplay_combat_state
            if combat_candidate is None:
                s.gameplay_projectile_timers.pop(timer_key, None)
                return
            visible_actor_positions = dict(
                getattr(s, "gameplay_delivered_monster_positions", {})
            )
            if (
                not s.connected
                or generation != s.gameplay_chase_generation
            ):
                s.gameplay_projectile_timers.pop(timer_key, None)
                return
        combat = combat_candidate
        resolution_state = 'missing'
        resolved_timer_keys = ()
        with combat.lock:
            pending = (
                combat.pending_runtime_player_hits.get(timer_key)
                if source_actor_id is not None
                else combat.pending_player_hits.get(cast_id)
            )
            if pending is None:
                remaining = None
            else:
                remaining = (
                    combat.runtime_player_hit_remaining(
                        source_actor_id,
                        cast_id,
                        impact_index,
                        now=now,
                        visible_actor_positions=visible_actor_positions,
                    )
                    if source_actor_id is not None
                    else pending.due_at - now
                )
            if pending is not None and remaining is not None:
                if remaining > 0.001:
                    resolution_state = 'reschedule'
                else:
                    due_candidates = [(timer_key, pending)]
                    if source_actor_id is not None:
                        # Only targets with the exact same authored arrival
                        # form one command batch. A late worker must not pull
                        # an independently-authored impact forward.
                        due_candidates.extend(
                            (candidate_key, candidate)
                            for candidate_key, candidate in tuple(
                                combat.pending_runtime_player_hits.items()
                            )
                            if (
                                candidate_key != timer_key
                                and candidate_key[0] == int(source_actor_id)
                                and candidate_key[1] == int(cast_id)
                                and abs(
                                    candidate.due_at - pending.due_at
                                ) <= 1e-9
                            )
                        )
                        due_candidates.sort(key=lambda item: item[0][2])
                    resolved = []
                    for candidate_key, candidate in due_candidates:
                        candidate_resolution = (
                            combat.resolve_runtime_player_hit(
                                source_actor_id,
                                cast_id,
                                candidate_key[2],
                                now=max(now, candidate.due_at),
                            )
                            if source_actor_id is not None
                            else combat.resolve_player_hit(
                                cast_id, now=max(now, candidate.due_at)
                            )
                        )
                        if candidate_resolution is not None:
                            resolved.append((candidate, candidate_resolution))
                    resolved_pairs = tuple(resolved)
                    resolved_timer_keys = tuple(
                        candidate_key for candidate_key, _ in due_candidates
                    )
                    resolution_state = 'resolved'

        if resolution_state == 'reschedule':
            replacement_timer = s.gameplay_deadline_timer(
                remaining,
                CharacterService._resolve_player_projectile,
                args=(
                    s,
                    generation,
                    cast_id,
                    source_actor_id,
                    impact_index,
                ),
            )
            replacement_timer.daemon = True
            installed = False
            with s.flow_lock:
                if (
                    s.connected
                    and generation == s.gameplay_chase_generation
                    and s.gameplay_combat_state is combat
                ):
                    s.gameplay_projectile_timers[timer_key] = replacement_timer
                    installed = True
            if not installed:
                replacement_timer.cancel()
                return
        elif resolution_state == 'missing':
            with s.flow_lock:
                if (
                    generation == s.gameplay_chase_generation
                    and s.gameplay_combat_state is combat
                ):
                    s.gameplay_projectile_timers.pop(timer_key, None)
            return
        else:
            cancelled_timers = []
            with s.flow_lock:
                if (
                    not s.connected
                    or generation != s.gameplay_chase_generation
                    or s.gameplay_combat_state is not combat
                ):
                    return
                for resolved_key in resolved_timer_keys:
                    candidate_timer = s.gameplay_projectile_timers.pop(
                        resolved_key, None
                    )
                    if candidate_timer is not None:
                        cancelled_timers.append(candidate_timer)
                encounter = s.gameplay_monster_encounter
                character = s.gameplay_character
            for candidate_timer in cancelled_timers:
                candidate_timer.cancel()
        if replacement_timer is not None:
            replacement_timer.start()
            return
        if not resolved_pairs or encounter is None:
            return

        dispatches = []
        socket_events = []
        low_health_socket_casts = set()
        socket_low_health = bool(
            source_actor_id is not None
            and CharacterService._player_is_low_health(s)
        )
        # Caster-side effects are cast transactions, not target
        # transactions.  A multi-projectile/AOE cast may resolve dozens of
        # targets at one authored deadline; sending the same user effect once
        # per target flooded RakNet's ordered stream and made Hit delivery
        # visibly stall behind duplicate status commands.
        batched_user_effects = {}
        per_target_speed_effects = []
        batched_user_effect_tick = 0
        killed_monsters = []
        effective_pairs = tuple(
            (pending, resolution)
            for pending, resolution in resolved_pairs
            if not bool(getattr(resolution, "damage_immune", False))
        )
        if source_actor_id is not None and effective_pairs:
            # Caster-targeted set rules carry their own authored trigger.
            # `ON:SkillStart` ones were already sent when the cast went out;
            # only the target events this batch actually satisfied belong here.
            impact_triggers = ["hit"]
            if any(
                resolution.critical_hit
                for _pending, resolution in effective_pairs
            ):
                impact_triggers.append("hitcritical")
            if any(
                resolution.killed for _pending, resolution in effective_pairs
            ):
                impact_triggers.append("kill")
            for skill_index in dict.fromkeys(
                int(pending.skill_index)
                for pending, _resolution in effective_pairs
            ):
                for definition in CharacterService._item_set_user_proc_effects(
                    s, skill_index, triggers=tuple(impact_triggers)
                ):
                    batched_user_effects.setdefault(
                        int(definition.effect_id), definition
                    )
            for definition in CharacterService._item_set_trigger_effects(
                s, "hit"
            ):
                batched_user_effects.setdefault(
                    int(definition.effect_id), definition
                )
            if any(resolution.killed for _pending, resolution in effective_pairs):
                for definition in CharacterService._item_set_trigger_effects(
                    s, "kill"
                ):
                    batched_user_effects.setdefault(
                        int(definition.effect_id), definition
                    )
        triggered_hit_auras = set()
        for pending, resolution in resolved_pairs:
            loot_generation = None
            if resolution.killed:
                monster = combat.monsters[resolution.target_actor_id]
                killed_monsters.append(monster)
                class_name = CLASS_NAMES.get(
                    int(getattr(character, 'class_id', 1)), 'mage'
                )
            # Several AOE impacts resolve on independent timer threads, and
            # multiplayer sessions share the same client-side item-manager
            # index space. Prefer the map-global allocator installed by the
            # runtime. Standalone protocol tests retain the local locked
            # fallback.
                allocator = s.gameplay_world_loot_allocator
                try:
                    if allocator is not None:
                        loot_generation = allocator(
                            s,
                            encounter=encounter,
                            monster=monster,
                            class_name=class_name,
                            player_level=max(
                                1, int(getattr(character, 'level', 1))
                            ),
                            position=resolution.position,
                            position_projector=lambda candidate: (
                                combat.ground_loot_position(
                                    resolution.target_actor_id,
                                    candidate,
                                )
                            ),
                        )
                    else:
                        with s.flow_lock:
                            next_item_id = s.gameplay_next_item_id
                            map_difficulty = s.gameplay_map_difficulty
                            scaling_difficulty = (
                                s.gameplay_scaling_difficulty
                            )
                            socket_evaluation = (
                                s.gameplay_socket_effect_evaluation
                            )
                        loot_generation = generate_monster_loot(
                            encounter.static_database_path,
                            monster.template_id,
                            class_name=class_name,
                            player_level=max(
                                1, int(getattr(character, 'level', 1))
                            ),
                            monster_level=max(1, int(monster.level)),
                            position=resolution.position,
                            next_item_id=next_item_id,
                            force_drops=FORCE_LOOT_DROPS_FOR_TESTING,
                            map_difficulty=max(
                                int(map_difficulty),
                                int(scaling_difficulty),
                            ),
                            extra_boss_gems=int(
                                getattr(
                                    socket_evaluation,
                                    "boss_bonus_gems",
                                    0,
                                )
                            ),
                            position_projector=lambda candidate: (
                                combat.ground_loot_position(
                                    resolution.target_actor_id,
                                    candidate,
                                )
                            ),
                        )
                        loot_generation = apply_socket_drop_amounts(
                            socket_evaluation,
                            loot_generation,
                        )
            # Loot is a consequence of a lethal hit, not part of committing
            # that hit. A malformed loot row or map projection must never
            # suppress the authoritative Hit/Kill response to the client.
                except Exception as exc:
                    print(
                        f"[{GAMEPLAY_SERVICE_NAME}] loot generation "
                        f"failed for {monster.template_id!r}; combat "
                        f"resolution will still be sent: "
                        f"{type(exc).__name__}: {exc}"
                    )
                if loot_generation is not None:
                    with s.flow_lock:
                        if generation != s.gameplay_chase_generation:
                            return
                        s.gameplay_next_item_id = max(
                            s.gameplay_next_item_id,
                            loot_generation.next_item_id,
                        )
                        if s.gameplay_presence_callback is None:
                        # Multiplayer presence installs the authoritative
                        # generation into every viewer, including the source.
                        # Pre-inserting it here allowed an auto-pickup to
                        # remove an item before presence inserted it a second
                        # time, respawning the same runtime id.
                            s.gameplay_world_loot.update(
                                {
                                    drop.item.item_id: drop
                                    for drop in loot_generation.drops
                                }
                            )

            resolution_label = (
                "player-lethal-hit"
                if resolution.killed
                else "player-hit-monster"
            )
            if source_actor_id is not None:
                if bool(getattr(resolution, "damage_immune", False)):
                    dispatches.append(
                        (resolution, "player-hit-immune", loot_generation)
                    )
                    continue
                wearer_low_health = socket_low_health
                frost_hit = combat.monster_has_status_group(
                    resolution.target_actor_id,
                    ("frost", "freeze"),
                    now=time.perf_counter(),
                )
                socket_events.extend(
                    CharacterService._player_impact_socket_events(
                        pending,
                        resolution,
                        source_actor_id=int(source_actor_id),
                        low_health=socket_low_health,
                        low_health_casts=low_health_socket_casts,
                    )
                )
                for definition in pending.user_status_effects:
                    if (
                        normalize_socket_trigger(definition.trigger)
                        in (
                            {
                                "", "hit", "kill", "hitcritical",
                                "hitlowhealth", "hitfrost",
                            }
                            if resolution.killed
                            else {
                                "", "hit", "hitcritical",
                                "hitlowhealth", "hitfrost",
                            }
                        )
                        and combat._effect_applies(
                            definition,
                            cast_id=(
                                pending.cast_id
                                + pending.projectile_index * 257
                                + pending.impact_index
                            ),
                            killed=resolution.killed,
                            critical_hit=bool(resolution.critical_hit),
                            low_health=wearer_low_health,
                            frost_hit=frost_hit,
                        )
                    ):
                        if definition.resource_id == "skill_mightystab_buff_attackspeed":
                            per_target_speed_effects.append((definition, resolution.server_tick))
                        else:
                            batched_user_effects.setdefault(
                                int(definition.effect_id), definition
                            )
                batched_user_effect_tick = max(
                    batched_user_effect_tick,
                    int(resolution.server_tick),
                )
                if pending.skill_index >= 0:
                    skill_geometry = combat._load_player_skill_geometry(pending.skill_index)
                    if skill_geometry is not None:
                        active_sources = CharacterService._active_skill_modifier_sources(s)
                        if active_sources:
                            skill_geometry = combat._modified_player_skill_geometry(
                                skill_geometry, active_sources
                            )
                        unmodified_geometry = combat._load_player_skill_geometry(pending.skill_index)
                        unmod_chances = {
                            v.resource_id: v.chance
                            for v in unmodified_geometry.location_visual_effects
                        } if unmodified_geometry is not None else {}
                        hit_location_visuals = tuple(
                            v for v in skill_geometry.location_visual_effects
                            if v.chance >= 1.0
                            and normalize_socket_trigger(v.trigger) in ("", "hit")
                            and (pending.travel_distance <= 0.0 or v.chance > unmod_chances.get(v.resource_id, 0.0))
                        )
                        if hit_location_visuals:
                            hit_location_receivers = tuple(
                                e for e in skill_geometry.location_status_effects
                                if e.chance >= 1.0 and normalize_socket_trigger(e.trigger) in ("", "hit")
                            )
                            for visual_def in hit_location_visuals:
                                aura_key = (pending.cast_id, visual_def.resource_id)
                                if aura_key in triggered_hit_auras:
                                    continue
                                triggered_hit_auras.add(aura_key)
                                CharacterService._schedule_player_location_visuals(
                                    s,
                                    combat,
                                    (visual_def,),
                                    position=resolution.position,
                                    yaw=0.0,
                                    server_tick=int(resolution.server_tick),
                                    owner_actor_id=int(source_actor_id),
                                    cast_id=int(pending.cast_id ^ pending.impact_index),
                                    delay_seconds=0.0,
                                    receiver_status_effects=hit_location_receivers,
                                    source_skill_index=int(pending.skill_index),
                                    player_damage_range=(
                                        pending.player_minimum_damage,
                                        pending.player_maximum_damage,
                                    ),
                                )
                                loc_resolutions, _ = combat.apply_player_location_auras(
                                    now=time.perf_counter(),
                                    server_tick=int(resolution.server_tick),
                                )
                                if loc_resolutions:
                                    CharacterService._dispatch_player_owned_resolutions(
                                        combat,
                                        {int(source_actor_id): s},
                                        loc_resolutions,
                                    )
            dispatches.append(
                (resolution, resolution_label, loot_generation)
            )
        if socket_events:
            CharacterService._dispatch_socket_event_batch(
                s, tuple(socket_events)
            )
        for definition, tick in per_target_speed_effects:
            CharacterService._send_player_skill_status_effects(
                s, (definition,), server_tick=tick,
                label="mightystab-hit-speed-stack",
            )
        if batched_user_effects:
            CharacterService._send_player_skill_status_effects(
                s,
                tuple(batched_user_effects.values()),
                server_tick=batched_user_effect_tick,
                label="player-skill-impact-status-effects",
            )
        CharacterService._apply_player_damage_healing(
            s,
            tuple(item[0] for item in dispatches),
            label="player-life-leech",
        )
        CharacterService._apply_player_kill_healing(
            s,
            tuple(item[0] for item in dispatches),
            label="player-kill-heal",
        )
        if (
            source_actor_id is not None
            and s.gameplay_presence_callback is not None
        ):
            if len(dispatches) == 1:
                resolution, resolution_label, loot_generation = dispatches[0]
                event = (
                    "player_combat_resolution",
                    resolution,
                    resolution_label,
                    loot_generation,
                )
            else:
                event = (
                    "player_combat_resolutions",
                    tuple(dispatches),
                )
            s.gameplay_presence_callback(s, event)
        else:
            CharacterService._send_combat_resolutions(
                s,
                tuple(item[0] for item in dispatches),
                label="player-hit-batch",
            )
            if any(
                loot_generation is not None and loot_generation.drops
                for _resolution, _label, loot_generation in dispatches
            ):
                CharacterService._reconcile_world_loot_visibility(
                    s,
                    label="world-loot-spawn",
                )
        if (getattr(s, "gameplay_match", None) is not None
                and getattr(s, "gameplay_match_callback", None) is not None):
            # Battleground objectives score damage dealt to team structures.
            s.gameplay_match_callback(s, ("match_monster_damage", tuple(
                (int(item[0].target_actor_id), int(item[0].damage))
                for item in dispatches
            )))
        if killed_monsters:
            CharacterService._award_monster_kill_xp(
                s,
                tuple(killed_monsters),
                database_path=encounter.static_database_path,
            )
            CharacterService._notify_monsters_killed(
                s, tuple(killed_monsters)
            )
        for (pending, resolution), dispatch in zip(
            resolved_pairs, dispatches
        ):
            loot_generation = dispatch[2]
            if (
                loot_generation is not None
                and loot_generation.drops
                and VERBOSE_PACKET_LOGGING
            ):
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] spawned "
                    f"{len(loot_generation.drops)} database-resolved world "
                    f"drop(s) from "
                    f"{loot_generation.profile.loot_list_id!r}: "
                    f"{[(drop.item.item_id, drop.item.template_id, drop.item.amount, drop.item.field_08) for drop in loot_generation.drops]}"
                )
            if VERBOSE_PACKET_LOGGING:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] applied BulletSkill "
                    f"index={pending.skill_index}, cast={pending.cast_id}, "
                    f"target={resolution.target_actor_id}, "
                    f"damage={resolution.damage}, "
                    f"health={resolution.current_health}/"
                    f"{resolution.maximum_health}, killed={resolution.killed}"
                )
