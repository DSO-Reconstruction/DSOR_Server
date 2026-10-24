"""Monster pursuit, attacks, return-home state, and vicinity delivery."""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.runtime.monster_runtime_diagnostics import mark_phase
from dso_server.services.monster_chase_loop import (
    _FixedRateChaseLoop,
    MONSTER_TICK_IDLE,
    MONSTER_WORLD_ORPHAN_GRACE_SECONDS,
)
from dso_server.services.monster_tick_context import MonsterTickContextService
from dso_server.services.player_owned_resolution_dispatch import (
    PlayerOwnedResolutionDispatchService,
)


class MonsterCombatService(
        PlayerOwnedResolutionDispatchService,
        MonsterTickContextService):
    """Monster pursuit, attacks, return-home state, and vicinity delivery."""

    def _admin_kill_all_monsters(self, s: Session) -> int:
        """Resolve every living monster in this shared map instance as a GM kill."""
        combat = s.gameplay_combat_state
        character = s.gameplay_character
        if combat is None or character is None:
            raise ValueError("the current map has no combat encounter")
        attacker_actor_id = (
            s.gameplay_remote_actor_id
            or CharacterService._remote_actor_id(character.character_id)
        )
        server_tick = int(server_time() // 40) & 0xFFFFFFFF
        resolutions = []
        killed_monsters = []
        with combat.lock:
            for actor_id in combat.alive_actor_ids:
                monster = combat.monsters[actor_id]
                if str(monster.template_id).casefold().startswith("damage_doll"):
                    continue
                previous = int(combat.monster_health.get(actor_id, 0))
                if previous <= 0:
                    continue
                combat.monster_health[actor_id] = 0
                combat.pending_monster_attacks.pop(actor_id, None)
                combat.monster_status_effects.pop(actor_id, None)
                for key in tuple(combat.monster_status_damage):
                    if int(key[0]) == int(actor_id):
                        combat.monster_status_damage.pop(key, None)
                combat.monster_target_actor_ids.pop(actor_id, None)
                resolutions.append(DamageResolution(
                    target_actor_id=int(actor_id),
                    attacker_actor_id=int(attacker_actor_id),
                    current_health=0,
                    maximum_health=int(monster.maximum_health),
                    damage=previous,
                    killed=True,
                    position=combat.monster_positions[actor_id],
                    server_tick=server_tick,
                    hit_tags=(0,),
                    skill_index=0,
                    source_actor_id=int(attacker_actor_id),
                ))
                killed_monsters.append(monster)
        if not resolutions:
            return 0
        target_sessions = {int(attacker_actor_id): s}
        for peer in self._active_gameplay_sessions(
                map_id=s.gameplay_map, excluding=s, instance_of=s):
            peer_character = peer.gameplay_character
            if peer_character is None or peer.gameplay_combat_state is not combat:
                continue
            peer_actor_id = (
                peer.gameplay_remote_actor_id
                or CharacterService._remote_actor_id(peer_character.character_id)
            )
            target_sessions[int(peer_actor_id)] = peer
        self._dispatch_player_owned_resolutions(
            combat, target_sessions, tuple(resolutions)
        )
        return len(killed_monsters)

    @staticmethod
    def _apply_base_health_regeneration(
            target_sessions, health_regeneration) -> None:
        """Publish passive regeneration through the non-combat vital update.

        Retail never models base regeneration as a Hit. ActorStatsUpdate is a
        uint64 current-health plus float current-resource snapshot and updates
        the HUD without entering ApplyDamage or creating combat text. Explicit
        spell, item, set and life-steal heals continue to use HealingSelf.
        """

        latest_by_actor = {}
        for (
            actor_id,
            _previous_health,
            current_health,
            maximum_health,
            recovered,
        ) in health_regeneration:
            actor_id = int(actor_id)
            previous = latest_by_actor.get(actor_id)
            latest_by_actor[actor_id] = (
                int(current_health),
                int(maximum_health),
                int(recovered) + (0 if previous is None else previous[2]),
            )

        for actor_id, (
            current_health,
            maximum_health,
            recovered,
        ) in latest_by_actor.items():
            target_session = target_sessions.get(int(actor_id))
            if target_session is None:
                continue
            with target_session.flow_lock:
                inventory = target_session.gameplay_inventory
                if inventory is None or target_session.gameplay_death_pending:
                    continue
                inventory.health_points = float(current_health)
                current_resource = float(inventory.secondary_resource)
                server_tick = max(
                    int(target_session.gameplay_chase_server_tick),
                    int(target_session.gameplay_last_heal_server_tick) + 1,
                ) & 0xFFFFFFFF
                target_session.gameplay_last_heal_server_tick = server_tick
            packet = encode_actor_stats_update_batch(
                ActorStatsUpdate(current_health, current_resource),
                actor_id=LOCAL_PLAYER_ACTOR_ID,
            )
            target_session.send_reliable_ordered(
                packet,
                track=True,
                label="passive-health-regeneration",
            )
            CharacterService._update_low_health_socket_state(
                target_session,
                current_health=current_health,
                maximum_health=maximum_health,
                server_tick=server_tick,
                event_seed=recovered,
            )

    @staticmethod
    def _monster_world_has_background_work(combat) -> bool:
        """Whether ``combat`` needs simulating even with no monster in view.

        Player summons and wounded, regenerating players are advanced by the
        map's simulation thread whether or not any viewer has a monster
        spawned, so either makes every session on the instance a possible
        owner of that thread.
        """

        if combat is None:
            return False
        lock = getattr(combat, "lock", None)
        if lock is None:
            return bool(getattr(combat, "player_summons", ())) or bool(
                getattr(combat, "has_regenerating_players", False)
            )
        # ``has_regenerating_players`` iterates the map's health table, which
        # joining and departing players resize under this lock.
        with lock:
            return bool(getattr(combat, "player_summons", ())) or bool(
                getattr(combat, "has_regenerating_players", False)
            )

    @staticmethod
    def _monster_world_owner(combat, sessions):
        """Elect the session that runs ``combat``'s simulation thread.

        Claiming the world and both re-elections (on a visibility change and
        on departure) must apply this one rule. They used to disagree about
        player summons: the claim counted every session on the instance once a
        summon existed, the re-elections only sessions with spawned monsters.
        The worker that lost its claim to an idle lower-id player then
        re-elected its own session, was refused, and retired -- leaving the
        map with no simulation at all until that idle player acted.
        """

        if combat is None:
            return None
        background = CharacterService._monster_world_has_background_work(
            combat
        )
        candidates = tuple(
            value
            for value in sessions
            if (
                value.connected
                and not value.gameplay_presence_departed
                and value.gameplay_character is not None
                and value.gameplay_combat_state is combat
                and (background or value.gameplay_monsters_spawned)
            )
        )
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda value: int(value.gameplay_character.character_id),
        )

    @staticmethod
    def _live_monster_worker(s: Session) -> bool:
        installed = s.gameplay_chase_timer
        return installed is not None and not (
            isinstance(installed, _FixedRateChaseLoop)
            and installed.has_exited()
        )

    @staticmethod
    def _start_monster_chase(s: Session) -> bool:
        if CharacterService._live_monster_worker(s):
            with s.flow_lock:
                installed = s.gameplay_chase_timer
                if CharacterService._live_monster_worker(s):
                    # That worker may be deciding to retire at this moment;
                    # let it see the request instead of dropping it.
                    if isinstance(installed, _FixedRateChaseLoop):
                        installed.request_restart()
                    return False
            # It released the slot between the two reads; replace it below.
        if (
            s.gameplay_presence_callback is not None
            and not s.gameplay_presence_callback(
                s, ("claim_monster_world",)
            )
        ):
            return False
        claimed_combat = s.gameplay_combat_state
        # Taken before flow_lock: the map lock may not nest inside it.
        background = CharacterService._monster_world_has_background_work(
            claimed_combat
        )
        with s.flow_lock:
            combat = s.gameplay_combat_state
            installed = s.gameplay_chase_timer
            if CharacterService._live_monster_worker(s):
                if isinstance(installed, _FixedRateChaseLoop):
                    installed.request_restart()
                return False
            if (
                not s.connected
                or combat is None
                or combat is not claimed_combat
                or (not s.gameplay_monsters_spawned and not background)
            ):
                return False
            # Generation identifies a map incarnation. Worker restarts must
            # not invalidate in-flight casts; only lifecycle paths bump it.
            generation = s.gameplay_chase_generation
            s.gameplay_chase_last_at = time.perf_counter()
            if s.gameplay_last_move is not None:
                client_tick = int(s.gameplay_last_move.start_tick)
                if (
                    (client_tick - int(s.gameplay_chase_server_tick))
                    & 0xFFFFFFFF
                ) < 0x80000000:
                    # A stationary player's last Move may predate many map
                    # ticks. Restarting must not rewind movement/attack time.
                    s.gameplay_chase_server_tick = client_tick
            timer = _FixedRateChaseLoop(s, generation)
            s.gameplay_chase_timer = timer
        try:
            timer.start()
        except Exception:
            with s.flow_lock:
                if s.gameplay_chase_timer is timer:
                    s.gameplay_chase_timer = None
            raise
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] started navmesh monster chase "
            f"at {1.0 / MONSTER_CHASE_INTERVAL_SECONDS:.0f} Hz"
        )
        return True

    @staticmethod
    def _supervise_monster_worlds(server, *, now: float | None = None):
        """Restart the simulation of any map instance left without a worker.

        A map whose only simulation thread is gone freezes silently: the
        monster watchdog can only sample workers that exist, and nothing on
        the casting or movement path re-elects an owner by itself. This runs
        about once a second from the watchdog thread, holds no lock across
        sessions, and acts only on an instance that has stayed orphaned for a
        grace period, so it never races an ordinary hand-off. Returns one
        diagnostic event per recovery or failed recovery.
        """

        now = time.perf_counter() if now is None else float(now)
        registry = getattr(server, "sessions", None)
        if registry is None:
            return ()
        instances = {}
        for session in tuple(registry.values()):
            combat = getattr(session, "gameplay_combat_state", None)
            if combat is None:
                continue
            _combat, members, viewers = instances.setdefault(
                id(combat), (combat, [], [])
            )
            # Any worker counts, even on a session that cannot be elected
            # right now; starting a second one would double the simulation.
            members.append(session)
            if (
                session.connected
                and session.gameplay_mode
                and session.gameplay_initialization_sent
                and session.gameplay_presence_ready
                and not session.gameplay_presence_departed
                and session.gameplay_character is not None
            ):
                viewers.append(session)
        orphans = getattr(server, "_monster_world_orphans", None)
        if orphans is None:
            orphans = server._monster_world_orphans = {}
        for key in tuple(orphans):
            if key not in instances:
                orphans.pop(key, None)
        events = []
        for key, (combat, members, viewers) in instances.items():
            if any(
                CharacterService._live_monster_worker(member)
                for member in members
            ):
                orphans.pop(key, None)
                continue
            owner = CharacterService._monster_world_owner(combat, viewers)
            if owner is None:
                orphans.pop(key, None)
                continue
            # [orphaned since, last failed recovery reported at]
            state = orphans.setdefault(key, [now, None])
            orphaned_for = now - state[0]
            if orphaned_for < MONSTER_WORLD_ORPHAN_GRACE_SECONDS:
                continue
            started = CharacterService._start_monster_chase(owner)
            event = {
                "event": (
                    "orphaned-map-restarted"
                    if started
                    else "orphaned-map-unrecovered"
                ),
                "map": owner.gameplay_map,
                "owner_endpoint": owner.addr,
                "owner_character_id": int(
                    owner.gameplay_character.character_id
                ),
                "players": len(viewers),
                "orphaned_seconds": round(orphaned_for, 3),
            }
            if started:
                orphans.pop(key, None)
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] restarted the orphaned "
                    f"monster simulation of {owner.gameplay_map}: no worker "
                    f"for {orphaned_for:.1f}s; owner character "
                    f"{event['owner_character_id']}, "
                    f"{len(viewers)} player(s) on the instance"
                )
                events.append(event)
            elif state[1] is None or now - state[1] >= 30.0:
                state[1] = now
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] monster simulation of "
                    f"{owner.gameplay_map} has had no worker for "
                    f"{orphaned_for:.1f}s and its elected owner (character "
                    f"{event['owner_character_id']}) refused to start one"
                )
                events.append(event)
        return tuple(events)

    @staticmethod
    def _send_monster_return_home_transition(
            s: Session, transition) -> None:
        start_tick = int(transition.start_tick) & 0xFFFFFFFF
        duration = int(transition.duration_ticks)
        entry = StatusEffectEntry(
            effect_id=int(transition.effect_id),
            field_04=int(transition.instance_id),
            field_0c=(start_tick + duration) & 0xFFFFFFFF,
            field_14=25,
            field_08=start_tick,
            field_18=0,
            field_10=duration,
            field_1c=100,
            field_20=int(transition.actor_id),
            # Detailed constructs; no-details + field_25=false removes.
            field_25=False,
            field_26=False,
            field_39=False,
            has_details=bool(transition.active),
            values=(
                (0.0, 0.0, 0.0, 0.0, 0.0)
                if transition.active
                else ()
            ),
        )
        packet = encode_status_effect_batch(
            StatusEffect(False, (entry,)),
            actor_id=transition.actor_id,
        )
        s.send_reliable_ordered(
            packet,
            track=True,
            label=(
                "monster-return-home-add"
                if transition.active
                else "monster-return-home-remove"
            ),
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] MonsterReturnHome "
            f"{'applied to' if transition.active else 'removed from'} "
            f"actor {transition.actor_id}"
        )

    @staticmethod
    def _viewer_actor_id(s: Session, actor_id: int) -> int:
        character = s.gameplay_character
        if character is None:
            return int(actor_id)
        local_server_actor_id = (
            getattr(s, "gameplay_remote_actor_id", None)
            or CharacterService._remote_actor_id(character.character_id)
        )
        return (
            LOCAL_PLAYER_ACTOR_ID
            if int(actor_id) == local_server_actor_id
            else int(actor_id)
        )

    @staticmethod
    def _monster_chase_tick(s: Session, generation: int):
        now = time.perf_counter()
        mark_phase(s, "claim-world")
        callback = s.gameplay_presence_callback
        if callback is not None:
            claim = callback(s, ("claim_monster_world",))
            if not claim:
                # Reconcile the active set either way; only the reason for the
                # failure decides whether this thread is finished.
                callback(s, ("monster_visibility_changed",))
                if claim is None:
                    # No viewer has a spawned monster, so there is nothing to
                    # own yet -- typically the instant an AoE clears the last
                    # visible pack. Retiring on that emptied the map of its
                    # only simulation worker, and nothing on the casting or
                    # damage path ever started another one. Stay idle instead.
                    with s.flow_lock:
                        if generation == s.gameplay_chase_generation:
                            s.gameplay_chase_last_at = now
                    return MONSTER_TICK_IDLE
                # A peer owns the instance now; the event above elected it.
                return False
        mark_phase(s, "target-snapshots")
        targets = (
            callback(s, ("combat_targets",))
            if callback is not None
            else ()
        )
        combat_candidate = s.gameplay_combat_state
        if combat_candidate is None:
            return False
        # Visibility is client-owned while combat state is shared per map.
        # Snapshot the union before taking the simulation lock so the 25 Hz
        # worker never expands back to every monster in the map merely because
        # one actor was active earlier in the session.
        #
        # One pass, one ``flow_lock`` acquisition per viewer. Every gameplay
        # thread contends for those locks, and this worker touches all of them
        # on behalf of the whole map, so each extra pass multiplies the map's
        # exposure to one player's lock hold by the number of players on it.
        candidate_target_sessions = {id(s): s}
        candidate_target_sessions.update(
            (id(target_session), target_session)
            for _actor_id, _position, target_session in targets
            if target_session.gameplay_combat_state is combat_candidate
        )
        (
            spawned_by_viewer,
            visible_monster_actor_ids,
            has_periodic_socket_auras,
        ) = CharacterService._snapshot_monster_tick_viewers(
            candidate_target_sessions, combat_candidate
        )
        if (
            callback is None
            and not visible_monster_actor_ids
            and s.gameplay_monsters_spawned
        ):
            # Historical single-session callers used only the aggregate flag.
            # Preserve that standalone contract without weakening multiplayer,
            # where the per-viewer spawned sets are authoritative.
            with combat_candidate.lock:
                visible_monster_actor_ids.update(
                    combat_candidate.active_monster_actor_ids
                )
            spawned_by_viewer[id(s)] = set(visible_monster_actor_ids)
        # Reliable backpressure is per client, so it must not stop the map.
        # This worker owns the whole instance's simulation: pausing it because
        # the *owner* is behind froze every other player's monsters, which is
        # what a populated map's "monsters stop updating" lag spike was. When
        # a presence callback exists, `monster_activity` suppresses only the
        # movement submission for each backlogged viewer while retaining its latest
        # transforms; attack starts,
        # transitions and Hit/Kill resolutions remain reliable. Without a
        # callback the owner is the only viewer and the two are equivalent.
        if callback is None:
            backlog = s.reliable_backlog_size()
            if backlog >= RELIABLE_BACKLOG_SOFT_LIMIT:
                with s.flow_lock:
                    if (
                        not s.connected
                        or generation != s.gameplay_chase_generation
                    ):
                        return False
                    # Discard elapsed wall time so recovery does not execute a
                    # burst of overdue movement or attacks.
                    s.gameplay_chase_last_at = now
                    if (
                        now - s.last_reliable_backpressure_log_at
                        >= 2.0
                    ):
                        s.last_reliable_backpressure_log_at = now
                        print(
                            f"[{GAMEPLAY_SERVICE_NAME}] paused monster packet "
                            f"production at reliable backlog {backlog}; "
                            f"soft limit={RELIABLE_BACKLOG_SOFT_LIMIT}"
                        )
                return True
        (
            damage_locked_session_ids,
            untargetable_session_ids,
        ) = CharacterService._prepare_victim_feature_gates(
            candidate_target_sessions
        )
        summon_context = CharacterService._prepare_player_summon_cycle_context(
            combat_candidate, targets, now=now
        )
        owner_snapshot = CharacterService._snapshot_monster_tick_owner(
            s, generation, combat_candidate, now=now
        )
        if owner_snapshot is None:
            return False
        combat = combat_candidate
        last_move = owner_snapshot.last_move
        combat_targets = [
            (actor_id, position, target_session)
            for actor_id, position, target_session in targets
            if target_session.gameplay_combat_state is combat
        ]
        if not combat_targets and last_move is not None:
            character = owner_snapshot.character
            combat_targets = [
                (
                    (
                        (
                            owner_snapshot.remote_actor_id
                            or CharacterService._remote_actor_id(
                                character.character_id
                            )
                        )
                        if character is not None
                        else combat.player_actor_id
                    ),
                    last_move.relative_position,
                    s,
                )
            ]

        # All lazy content lookups and player-stat calculation happen before
        # the authoritative map transaction. The short preflight snapshot is
        # map-owned only and never retains a session lock.
        mark_phase(s, "catalog-preflight")
        CharacterService._prime_monster_tick_catalogs(combat)
        with combat.lock:
            due_attack_actor_ids = frozenset(
                int(attack.target_actor_id)
                for _caster_id, attack in combat.pending_monster_attack_items()
                if attack.due_at <= now
            )
        mark_phase(s, "target-stats")
        (
            combat_targets,
            due_periodic_auras_by_actor,
            prepared_target_stats,
        ) = CharacterService._prepare_monster_target_inputs(
            combat,
            combat_targets,
            due_attack_actor_ids=due_attack_actor_ids,
            now=now,
        )

        with combat.lock:
            if (
                not visible_monster_actor_ids
                and not combat.player_summons
                and not combat.has_regenerating_players
                and not has_periodic_socket_auras
                # Authored impacts and monster swings outlive the visible set.
                # Reporting idleness while either is pending would let the
                # worker retire with authoritative work still queued.
                and not combat.pending_runtime_player_hits
                and not combat.pending_monster_attacks
                and not combat.pending_monster_projectile_attacks
            ):
                idle = True
            else:
                idle = False
        if idle:
            # Advance the owner clock even with nothing to simulate. Otherwise
            # the elapsed-time cap in the next active tick turns a quiet
            # stretch into one oversized movement step.
            CharacterService._commit_monster_tick_owner(
                s,
                generation,
                combat,
                owner_snapshot,
                now=now,
            )
            return MONSTER_TICK_IDLE

        mark_phase(s, "simulation")
        with combat.lock:
            elapsed = owner_snapshot.elapsed
            server_tick = owner_snapshot.server_tick
            wanted_actor_ids = {
                actor_id
                for actor_id in visible_monster_actor_ids
                if (
                    actor_id in combat.monsters
                    and combat.monster_health.get(actor_id, 0) > 0
                )
            }
            inactive_actor_ids = (
                set(combat.active_monster_actor_ids) - wanted_actor_ids
            )
            if inactive_actor_ids:
                combat.deactivate_monsters(
                    tuple(sorted(inactive_actor_ids)), reset=False
                )
            newly_active_actor_ids = (
                wanted_actor_ids - set(combat.active_monster_actor_ids)
            )
            if newly_active_actor_ids:
                combat.activate_monsters(
                    tuple(sorted(newly_active_actor_ids))
                )
            out_of_range_summons = ()
            tick_inputs = {"combat_targets": len(combat_targets)}
            if not combat_targets:
                movements = ()
                attacks = ()
                resolutions = ()
                player_owned_resolutions = ()
                pvp_summon_resolutions = ()
                support_applications = ()
                location_aura_applications = ()
                location_aura_resolutions = ()
            else:
                living_actor_ids = combat.living_actor_ids
                ordinary_actor_ids = set(living_actor_ids)
                movements = []
                attacks = []
                for taunting_summon, taunted_actor_ids in (
                    combat.player_summon_taunt_assignments(now=now)
                ):
                    ordinary_actor_ids.difference_update(
                        taunted_actor_ids
                    )
                    taunt_position = (
                        combat.encounter.coordinate_frame.world_to_wire(
                            taunting_summon.position
                        )
                    )
                    movements.extend(combat.advance_monsters(
                        taunt_position,
                        elapsed_seconds=elapsed,
                        server_tick=server_tick,
                        eligible_actor_ids=taunted_actor_ids,
                        now=now,
                    ))
                    attacks.extend(combat.maybe_start_monster_attacks(
                        taunt_position,
                        server_tick=server_tick,
                        now=now,
                        target_actor_id=taunting_summon.actor_id,
                        eligible_actor_ids=taunted_actor_ids,
                    ))
                aggro_targets = tuple(
                    target
                    for target in combat_targets
                    if (
                        id(target[2]) not in untargetable_session_ids
                        and bool(
                            spawned_by_viewer.get(id(target[2]), set())
                            .intersection(ordinary_actor_ids)
                        )
                    )
                )
                tick_inputs.update(
                    aggro_targets=len(aggro_targets),
                    living=len(living_actor_ids),
                    ordinary=len(ordinary_actor_ids),
                    untargetable=len(untargetable_session_ids),
                    spawned=max(
                        (
                            len(spawned_by_viewer.get(id(target[2]), ()))
                            for target in combat_targets
                        ),
                        default=0,
                    ),
                )
                assignments = combat.assign_monster_targets(
                    ordinary_actor_ids,
                    tuple((actor_id, combat.encounter.coordinate_frame.wire_to_world(position),
                           spawned_by_viewer.get(id(viewer), set()))
                          for actor_id, position, viewer in aggro_targets),
                )
                for actor_id, scripted_target in tuple(
                    combat.scripted_monster_targets.items()
                ):
                    if actor_id in ordinary_actor_ids:
                        assignments[actor_id] = (
                            LOCAL_PLAYER_ACTOR_ID, scripted_target,
                        )
                # One movement pass preserves the map's A* budget and collision
                # separation while each pack pursues its own nearby player.
                fallback = combat_targets[0][1]
                movements.extend(combat.advance_monsters(
                    fallback, elapsed_seconds=elapsed, server_tick=server_tick,
                    eligible_actor_ids=ordinary_actor_ids, now=now,
                    targets_by_actor=assignments,
                ))
                attacks.extend(combat.maybe_start_monster_attacks(
                    fallback, server_tick=server_tick, now=now,
                    eligible_actor_ids=ordinary_actor_ids,
                    targets_by_actor=assignments,
                ))
                movements.extend(combat.advance_player_summons(
                    elapsed_seconds=elapsed,
                    server_tick=server_tick,
                    now=now,
                    player_positions={
                        actor_id: combat.encounter.coordinate_frame.wire_to_world(position)
                        for actor_id, position, _viewer in combat_targets
                    },
                ))
                movements = tuple(movements)
                attacks = tuple(attacks)
                hostile_resolutions = []
                periodic_monster_resolutions = []
                periodic_owner_resolutions = []
                target_stats_by_actor = prepared_target_stats
                for target_index, (actor_id, position, target_session) in enumerate(
                    combat_targets
                ):
                    incoming_multipliers = 1.0
                    block_chance = 0.0
                    block_damage_reduction = 0.0
                    due_periodic_auras = (
                        due_periodic_auras_by_actor.get(int(actor_id), ())
                    )
                    target_stats = target_stats_by_actor.get(int(actor_id))
                    if target_stats is not None:
                        incoming_multipliers = (
                            target_stats.incoming_damage_multipliers
                        )
                        if id(target_session) in damage_locked_session_ids:
                            incoming_multipliers = 0.0
                        block_chance = target_stats.block_chance
                        block_damage_reduction = (
                            target_stats.block_damage_reduction
                        )
                    invulnerable = bool(getattr(
                        target_session, "gameplay_gm_invulnerable", False
                    ))
                    if invulnerable:
                        incoming_multipliers = 0.0
                    hostile_resolutions.extend(
                        combat.resolve_due_monster_attacks(
                            position,
                            now=now,
                            player_actor_id=actor_id,
                            incoming_damage_multiplier=incoming_multipliers,
                            block_chance=block_chance,
                            block_damage_reduction=(
                                block_damage_reduction
                            ),
                            finalize_area_attacks=(
                                target_index == len(combat_targets) - 1
                            ),
                        )
                    )
                    if due_periodic_auras:
                        world_position = (
                            combat.encounter.coordinate_frame.wire_to_world(
                                position
                            )
                        )
                        monster_ticks, owner_ticks = (
                            combat.resolve_socket_periodic_auras(
                                actor_id,
                                world_position,
                                tuple(due_periodic_auras),
                                player_damage_range=(
                                    target_stats.minimum_damage,
                                    target_stats.maximum_damage,
                                ),
                                player_incoming_damage_multipliers=(
                                    target_stats.incoming_damage_multipliers
                                ),
                                player_invulnerable=invulnerable,
                                now=now,
                                server_tick=server_tick,
                            )
                        )
                        periodic_monster_resolutions.extend(monster_ticks)
                        periodic_owner_resolutions.extend(owner_ticks)
                hostile_resolutions.extend(periodic_owner_resolutions)
                hostile_resolutions = tuple(hostile_resolutions)
                reflected_resolutions = tuple(
                    reflected
                    for hostile in hostile_resolutions
                    for target_stats in (
                        target_stats_by_actor.get(
                            int(hostile.target_actor_id)
                        ),
                    )
                    if target_stats is not None
                    for reflected in (
                        combat.reflect_player_damage(
                            hostile,
                            player_actor_id=int(hostile.target_actor_id),
                            fraction=(
                                target_stats.damage_reflection_fraction
                            ),
                        ),
                    )
                    if reflected is not None
                )
                player_world_positions = {
                    actor_id: combat.encounter.coordinate_frame.wire_to_world(
                        position
                    )
                    for actor_id, position, _session in combat_targets
                }
                (
                    out_of_range_summons,
                    summon_attacks,
                    summon_resolutions,
                    pvp_summon_resolutions,
                    support_applications,
                ) = CharacterService._resolve_player_summon_cycle(
                    combat,
                    combat_targets,
                    player_world_positions,
                    server_tick=server_tick,
                    now=now,
                    hostile_player_actor_ids_by_owner=summon_context[0],
                    player_damage_multipliers=summon_context[1],
                    lethal_player_actor_ids_by_owner=summon_context[2],
                )
                (
                    location_aura_resolutions,
                    location_aura_applications,
                ) = (
                    combat.apply_player_location_auras(
                        now=now, server_tick=server_tick,
                        player_positions=player_world_positions,
                    )
                )
                status_resolutions = (
                    combat.resolve_due_monster_status_damage(
                        now=now, server_tick=server_tick
                    )
                )
                attacks = tuple(attacks) + tuple(summon_attacks)
                # Autonomous summons and status ticks are still owned by a
                # player.  They must use the player-combat dispatch path so
                # elemental combat text, lethal actor cleanup, and loot all
                # behave exactly like a direct spell impact.  Sending them
                # as monster activity mislabeled the source and left dead
                # actors registered in each viewer's vicinity set.
                resolutions = tuple(hostile_resolutions)
                player_owned_resolutions = (
                    tuple(summon_resolutions)
                    + tuple(location_aura_resolutions)
                    + tuple(status_resolutions)
                    + tuple(periodic_monster_resolutions)
                    + reflected_resolutions
                )
            out_of_range_actor_ids = {
                int(summon.actor_id)
                for summon in out_of_range_summons
            }
            summon_aura_refreshes = tuple(
                command
                for command in combat.due_player_summon_aura_refreshes(
                    server_tick=server_tick
                )
                if int(command.actor_id) not in out_of_range_actor_ids
            )
            if out_of_range_actor_ids:
                attacks = tuple(
                    attack
                    for attack in attacks
                    if int(attack.attacker_actor_id)
                    not in out_of_range_actor_ids
                )
            return_home_transitions = (
                combat.drain_return_home_transitions()
            )
            monster_summons = tuple(combat.pending_monster_summons)
            combat.pending_monster_summons.clear()
            health_regeneration = combat.regenerate_players(now=now)
            resource_regeneration = (
                combat.regenerate_player_resources(now=now)
            )

        mark_phase(s, "commit-owner")
        if not CharacterService._commit_monster_tick_owner(
            s,
            generation,
            combat,
            owner_snapshot,
            now=now,
        ):
            return False

        if monster_summons and callback is not None:
            callback(s, ("monster_summons", monster_summons))

        mark_phase(s, "console-heartbeat")
        CharacterService._report_monster_control(
            combat, movements, attacks, tick_inputs
        )

        mark_phase(s, "resolution-fanout")
        target_sessions = {
            actor_id: target_session
            for actor_id, _position, target_session in combat_targets
        }
        resolutions = tuple(
            replace(
                resolution,
                victim_status_effects=(
                    CharacterService._filter_player_status_effects(
                        target_sessions[int(resolution.target_actor_id)],
                        resolution.victim_status_effects,
                    )
                ),
            )
            if int(resolution.target_actor_id) in target_sessions
            else resolution
            for resolution in resolutions
        )
        # A hit cancels the victim's switch-character countdown.
        for resolution in resolutions:
            if int(getattr(resolution, "damage", 0)) > 0:
                victim = target_sessions.get(int(resolution.target_actor_id))
                if victim is not None:
                    CharacterService._cancel_pending_logout(victim, "attacked")
        CharacterService._apply_base_health_regeneration(
            target_sessions, health_regeneration
        )
        CharacterService._apply_base_resource_regeneration(
            target_sessions, resource_regeneration
        )
        CharacterService._dispatch_pvp_summon_resolutions(
            combat, target_sessions, pvp_summon_resolutions
        )
        CharacterService._dispatch_location_aura_applications(
            target_sessions, location_aura_applications
        )
        CharacterService._dispatch_player_owned_resolutions(
            combat, target_sessions, player_owned_resolutions
        )
        for summon in out_of_range_summons:
            owner_session = target_sessions.get(summon.owner_actor_id)
            if owner_session is None:
                continue
            CharacterService._expire_player_summon(
                owner_session,
                owner_session.gameplay_chase_generation,
                (
                    "player-summon-owner-range",
                    int(summon.actor_id),
                ),
                int(summon.actor_id),
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] despawning "
                f"{summon.definition.monster_template_id} "
                f"{summon.actor_id}: owner crossed "
                f"{summon.definition.owner_despawn_range:.1f}-unit range"
            )
        for application in support_applications:
            target_session = target_sessions.get(
                application.target_actor_id
            )
            if target_session is None:
                if application.target_actor_id in combat.player_summons:
                    for viewer in {id(v): v for v in target_sessions.values()}.values():
                        CharacterService._send_entity_status_effects(
                            viewer, application.status_effects,
                            actor_id=application.target_actor_id,
                            server_tick=application.server_tick,
                            label="player-summon-causer-status-effects",
                        )
                continue
            CharacterService._send_player_skill_status_effects(
                target_session,
                application.status_effects,
                server_tick=application.server_tick,
                label="player-summon-support-status-effects",
            )

        if summon_aura_refreshes:
            if callback is not None:
                callback(
                    s,
                    (
                        "player_summon_aura_refresh",
                        summon_aura_refreshes,
                    ),
                )
            else:
                CharacterService._send_player_summon_aura_refreshes_to(
                    s, summon_aura_refreshes
                )

        mark_phase(s, "monster-activity-fanout")
        if callback is not None:
            callback(
                s,
                (
                    "monster_activity",
                    combat,
                    movements,
                    attacks,
                    return_home_transitions,
                    resolutions,
                ),
            )
        else:
            CharacterService._send_monster_activity_to(
                s,
                combat,
                movements,
                attacks,
                return_home_transitions,
                resolutions,
            )
        mark_phase(s, "socket-status-updates")
        for resolution in resolutions:
            target_session = target_sessions.get(
                int(resolution.target_actor_id)
            )
            if target_session is None:
                continue
            if bool(getattr(resolution, "blocked", False)):
                CharacterService._dispatch_socket_event(
                    target_session,
                    "hitblocked",
                    server_tick=int(resolution.server_tick),
                    event_seed=(
                        int(resolution.attacker_actor_id)
                        ^ int(resolution.skill_index)
                    ),
                )
            CharacterService._update_low_health_socket_state(
                target_session,
                current_health=int(resolution.current_health),
                maximum_health=int(resolution.maximum_health),
                server_tick=int(resolution.server_tick),
                event_seed=int(resolution.attacker_actor_id),
            )
        mark_phase(s, "console-attacks")
        if attacks and (
            VERBOSE_PACKET_LOGGING
            or now - s.gameplay_last_monster_attack_log_at >= 1.0
        ):
            s.gameplay_last_monster_attack_log_at = now
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] started autonomous attack(s) "
                + ", ".join(
                    f"{(
                        value.definition.resource_id
                        if value.definition is not None
                        else (
                            combat.monsters[
                                value.attacker_actor_id
                            ].attack.resource_id
                            if value.attacker_actor_id in combat.monsters
                            else "expired-player-summon"
                        )
                    )}"
                    f"/0x{value.command_id:02x}"
                    f"@{value.attacker_actor_id}"
                    f"#skill={value.skill_index}"
                    f"#cast={value.cast_id}"
                    f"#tick={value.server_tick}"
                    for value in attacks
                )
            )
        if VERBOSE_PACKET_LOGGING:
            for resolution in resolutions:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] monster actor "
                    f"{resolution.attacker_actor_id} hit "
                    f"player for {resolution.damage}; health="
                    f"{resolution.current_health}/{resolution.maximum_health}"
                )
        return True

    @staticmethod
    def _expire_runtime_status_effects(s: Session) -> None:
        """Drop lapsed runtime buffs and reconcile the vitals they changed.

        Shared by the full modifier-source build and by the cheap
        effect-id-only walk that answers the ActorFeature gates, so both see
        the same expiry instant and neither can observe a stale buff.
        """

        now = time.perf_counter()
        candidates = tuple(
            instance_id
            for instance_id, (_entry, expires_at)
            in tuple(s.gameplay_runtime_status_effects.items())
            if expires_at is not None and expires_at <= now
        )
        if not candidates:
            return
        with s.flow_lock:
            # Another cast can refresh an occurrence between the read-only
            # snapshot and this mutation. Never remove its renewed lease.
            expired = False
            for instance_id in candidates:
                current = s.gameplay_runtime_status_effects.get(instance_id)
                if (
                    current is not None
                    and current[1] is not None
                    and current[1] <= now
                ):
                    s.gameplay_runtime_status_effects.pop(instance_id)
                    expired = True
            combat = s.gameplay_combat_state
        # Vitals reconciliation can acquire the map lock and send packets.
        # Never run it under flow_lock or invalidate unchanged equipment.
        if expired and combat is not None:
            CharacterService._refresh_player_combat_vitals(
                s, inventory_changed=False,
            )

    @staticmethod
    def _active_status_effect_ids(s: Session) -> tuple[int, ...]:
        """Return the sorted effect ids of every live status source.

        The ActorFeature gates only ever need effect ids, but they used to get
        them by building the complete modifier-source tuple: every persistent,
        item-set, socket and runtime entry, plus the item-set modifier list and
        a ``parse_socket_modifier`` call per socket cast rule -- none of which
        carry an ``effect_id`` at all. The map's simulation thread does this
        once per viewer per 25 Hz tick, so on a populated map that discarded
        work scaled with the player count.
        """

        CharacterService._expire_runtime_status_effects(s)
        return tuple(sorted(
            int(source.effect_id)
            for group in (
                tuple(s.gameplay_persistent_status_effects.values()),
                tuple(s.gameplay_item_set_effects.values()),
                tuple(s.gameplay_socket_effects.values()),
                (
                    entry
                    for entry, _expires_at
                    in tuple(s.gameplay_runtime_status_effects.values())
                ),
            )
            for source in group
            if hasattr(source, "effect_id")
        ))

    @staticmethod
    def _active_skill_modifier_sources(s: Session) -> tuple[object, ...]:
        """Return unexpired player effects plus active set/socket rules."""

        CharacterService._expire_runtime_status_effects(s)
        victim_sources = tuple(
            CharacterService._active_pvp_victim_modifier_sources(s)
        )
        # Entries are immutable; copy each container before iterating. Pure
        # modifier reads must not hold gameplay locks during evaluation.
        sources = (
            tuple(s.gameplay_persistent_status_effects.values())
            + tuple(s.gameplay_item_set_effects.values())
            + tuple(s.gameplay_socket_effects.values())
            + tuple(
                entry for entry, _expires_at
                in tuple(s.gameplay_runtime_status_effects.values())
            )
            + tuple(getattr(s.gameplay_item_set_evaluation, "modifiers", ()))
        )
        cast_rules = tuple(
            getattr(s.gameplay_socket_effect_evaluation, "cast_rules", ())
        )
        return victim_sources + sources + tuple(
            parse_socket_modifier(rule.raw) for rule in cast_rules
        )

    @staticmethod
    def _player_is_low_health(s: Session) -> bool:
        """Read the live authoritative one-third-health threshold."""

        combat = s.gameplay_combat_state
        character = s.gameplay_character
        if combat is None or character is None:
            return False
        actor_id = (
            s.gameplay_remote_actor_id
            or CharacterService._remote_actor_id(character.character_id)
        )
        health = combat.player_health.get(int(actor_id))
        return bool(
            health is not None
            and int(health[0]) <= max(1, round(int(health[1]) / 3.0))
        )

    @staticmethod
    def _conditional_skill_modifier_sources(
            s: Session, sources) -> tuple[object, ...]:
        """Filter wearer-state conditions before materializing skill data.

        ``ON:HitLowHealth`` on ammunition jewels means the wearer is below
        one third health. It is not a test of the monster's remaining HP.
        Filtering the compiled tuple once per cast prevents conditional
        location/bullet modifiers from entering the generic geometry resolver.
        """

        low_health = CharacterService._player_is_low_health(s)
        result = []
        for source in tuple(sources):
            arguments = tuple(
                str(value) for value in getattr(source, "arguments", ())
            )
            trigger = next(
                (
                    normalize_socket_trigger(value.partition(":")[2])
                    for value in arguments
                    if value.partition(":")[0].strip().casefold() == "on"
                ),
                "",
            )
            if trigger in DISABLED_TRIGGERS:
                continue
            if trigger == "hitlowhealth" and not low_health:
                continue
            result.append(source)
        return tuple(result)
