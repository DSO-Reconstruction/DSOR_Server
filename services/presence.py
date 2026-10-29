"""Remote-player presence, equipment, status, and world synchronization."""
from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.gameplay.player_summon_lifecycle import player_summon_preparation_batch
from dso_server.services.presence_movement import relayed_player_move

class PresenceService:
    """Remote-player presence, equipment, status, and world synchronization."""

    @staticmethod
    def _send_remote_player_actor_to(
        viewer: Session, remote: RemotePlayer,
    ) -> None:
        """Register the actor in the client's vicinity before creating it.

        NPCs and props are registered through ActorsEnterVicinity before the
        client receives their entity command. Remote players need the same
        ordering: NewRemotePlayer alone can arrive with a valid actor id that
        the client's vicinity/actor registry has never seen.
        """

        vicinity_packet, _ = encode_actors_enter_vicinity_batch(
            ActorsEnterVicinityBatch(
                ActorsEnterVicinity((int(remote.actor_id),)),
                actor_id=0,
                commands=(),
            )
        )
        viewer.send_reliable_ordered(
            vicinity_packet,
            track=True,
            label=f"remote-player-vicinity-{remote.actor_id}",
        )
        viewer.send_reliable_ordered(
            encode_new_remote_player(remote),
            track=True,
            label=f"remote-player-enter-{remote.actor_id}",
        )
        viewer.send_reliable_ordered(
            encode_remote_player_info(remote),
            track=True,
            label=f"remote-player-info-{remote.actor_id}",
        )

    def _remote_player_for(self, session: Session) -> RemotePlayer:
        character = session.gameplay_character
        if character is None:
            raise ValueError("remote-player session has no character")
        actor_id = CharacterService._session_remote_actor_id(self, session)
        with session.flow_lock:
            session.gameplay_remote_actor_id = actor_id
            last_move = session.gameplay_last_move
            inventory = copy.deepcopy(session.gameplay_inventory)
            position = session.gameplay_spawn_position or (0.0, 0.0, 0.0)
            heading = 0.0
            if last_move is not None:
                heading = float(last_move.yaw)
                # NewRemotePlayer consumes the same map-relative coordinate
                # space carried by MoveCommand. Converting it back to absolute
                # database/world coordinates spawns the remote tens of units
                # away, followed by a visible teleport on its first move.
                position = last_move.relative_position
        guild = self.social_store.guild_for(character.character_id)
        try:
            animation_set = (
                self.static_database.remote_player_animation_set(
                    character.class_id
                )
                if self.static_database is not None
                else ""
            )
        except (OSError, ValueError, CharacterSelectionError):
            animation_set = ""
        if not animation_set:
            # These are the base values in this client export's
            # _Template_RemotePlayer rows. Keep the generated command safe
            # even if the optional static database is unavailable.
            animation_set = (
                "dwarf_1h_weapon"
                if int(character.class_id) == 3
                else "male"
            )
        template_row = int(session.gameplay_template_row_index or 0)
        if self.static_database is not None:
            try:
                template_row = (
                    self.static_database.remote_player_template_row_index(
                        character.class_id
                    )
                )
            except (OSError, ValueError, CharacterSelectionError):
                # Keep the route's template index as a compatibility fallback
                # for older static databases without the remote template set.
                pass
        animation_set = CharacterService._remote_animation_for(
            character,
            inventory,
            animation_set,
        )
        return RemotePlayer(
            actor_id=actor_id,
            name=str(character.name),
            guild_name=guild.name if guild is not None else "",
            class_id=int(character.class_id),
            gender=int(character.gender),
            hair_style=int(character.hair_style),
            beard_style=int(character.beard_style),
            body_style=int(character.body_style),
            body_stature=int(character.body_stature),
            template_row=template_row,
            position=tuple(float(value) for value in position),
            heading=heading,
            character_id=int(character.character_id),
            # NewRemotePlayer carries the account identity as well as the
            # character id. A shared placeholder makes every remote actor
            # appear to belong to the same account and can collide with the
            # client's player/account registries.
            account_id=max(
                1,
                int(getattr(session, "account_id", None)
                    or character.character_id),
            ),
            level=max(1, int(character.level)),
            admin_privileges=bool(character.admin_privileges),
            admin_name_color=bool(character.admin_name_color),
            equipment=self._remote_equipment_visuals(
                inventory,
                getattr(session, "gameplay_hidden_equipment_skins", {}),
            ),
            emblem_id=self._remote_emblem_id(inventory),
            animation_set=animation_set,
            title_type=int(session.gameplay_title_type),
            achievement_title=str(session.gameplay_achievement_title),
            honor_points=int(getattr(session, "gameplay_honor_points", 0) or 0),
            team=(
                0xFFFFFFFF
                if getattr(session, "gameplay_match_team", None) is None
                else int(session.gameplay_match_team)
            ),
        )

    def _announce_remote_presence(self, session: Session) -> None:
        if self.sessions.get(session.addr) is not session:
            return
        character = session.gameplay_character
        if character is None:
            return
        with session.flow_lock:
            session.gameplay_presence_departed = False
        candidate_peers = self._active_gameplay_sessions(
            map_id=session.gameplay_map, excluding=session
        )
        stale_sessions = tuple(
            peer
            for peer in candidate_peers
            if int(peer.gameplay_character.character_id)
            == int(character.character_id)
        )
        peers = tuple(
            peer
            for peer in candidate_peers
            if (
                peer not in stale_sessions
                and CharacterService._gameplay_instance_key(peer)
                == CharacterService._gameplay_instance_key(session)
            )
        )
        # A map-transfer destination may finish initialization before the old
        # UDP socket closes. Both generations share one ActorManager low
        # index, so retire the old generation before NewRemotePlayer. Marking
        # the source departed also makes its later disconnect callback a no-op
        # instead of sending a duplicate DiscardPlayer warning.
        for stale in stale_sessions:
            with stale.flow_lock:
                stale.gameplay_presence_departed = True
            stale_actor_id = (
                stale.gameplay_remote_actor_id
                or CharacterService._remote_actor_id(character.character_id)
            )
            stale_packet = encode_discard_player(stale_actor_id)
            stale_instance_peers = tuple(
                peer
                for peer in candidate_peers
                if (
                    peer not in stale_sessions
                    and CharacterService._gameplay_instance_key(peer)
                    == CharacterService._gameplay_instance_key(stale)
                )
            )
            for peer in stale_instance_peers:
                CharacterService._forget_remote_visual_status(
                    peer, stale_actor_id
                )
                peer.send_reliable_ordered(
                    stale_packet,
                    track=True,
                    label=f"remote-player-replaced-{stale_actor_id}",
                )
        if not peers:
            map_sessions = tuple(
                peer for peer in self._map_session_candidates(
                    session.gameplay_map
                ) if peer is not session
            )
            readiness = tuple(
                (
                    getattr(peer.gameplay_character, "name", "?"),
                    bool(peer.connected),
                    bool(peer.gameplay_mode),
                    bool(peer.gameplay_initialization_sent),
                    bool(peer.gameplay_presence_ready),
                    bool(peer.gameplay_presence_departed),
                    CharacterService._gameplay_instance_key(peer),
                )
                for peer in map_sessions
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] no shared remote-player "
                f"instance for {character.name!r}: map="
                f"{session.gameplay_map!r}, key="
                f"{CharacterService._gameplay_instance_key(session)!r}, "
                f"map_sessions(name,connected,gameplay,initialized,ready,"
                f"departed,key)={readiness!r}"
            )
            CharacterService._broadcast_presence_social_status(
                self, character.character_id
            )
            return
        local_remote = self._remote_player_for(session)
        local_status = CharacterService._remote_status_snapshot(
            session, local_remote.actor_id
        )
        local_pvp = PVPFlag(
            bool(session.gameplay_pvp_enabled),
            int(session.gameplay_chase_server_tick) & 0xFFFFFFFF,
        )
        for peer in peers:
            peer_remote = self._remote_player_for(peer)
            CharacterService._forget_remote_visual_status(
                peer, local_remote.actor_id
            )
            CharacterService._forget_remote_visual_status(
                session, peer_remote.actor_id
            )
            peer_status = CharacterService._remote_status_snapshot(
                peer, peer_remote.actor_id
            )
            peer_pvp = PVPFlag(
                bool(peer.gameplay_pvp_enabled),
                int(peer.gameplay_chase_server_tick) & 0xFFFFFFFF,
            )
            CharacterService._send_remote_player_actor_to(
                peer, local_remote
            )
            if local_status.entries:
                peer.send_reliable_ordered(
                    encode_status_effect_batch(
                        local_status,
                        actor_id=local_remote.actor_id,
                    ),
                    track=True,
                    label=(f"remote-player-status-" f"{local_remote.actor_id}"),
                )
            peer.send_reliable_ordered(
                encode_pvp_flag_response(local_pvp, actor_id=local_remote.actor_id),
                track=True,
                label=f"remote-player-pvp-{local_remote.actor_id}",
            )
            CharacterService._send_player_overhead_icons_to(
                peer, session, local_remote.actor_id
            )
            CharacterService._send_remote_player_actor_to(
                session, peer_remote
            )
            if peer_status.entries:
                session.send_reliable_ordered(
                    encode_status_effect_batch(
                        peer_status,
                        actor_id=peer_remote.actor_id,
                    ),
                    track=True,
                    label=(f"remote-player-status-" f"{peer_remote.actor_id}"),
                )
            session.send_reliable_ordered(
                encode_pvp_flag_response(peer_pvp, actor_id=peer_remote.actor_id),
                track=True,
                label=f"remote-player-pvp-{peer_remote.actor_id}",
            )
            CharacterService._send_player_overhead_icons_to(
                session, peer, peer_remote.actor_id
            )
        # GamePlayer-owned world objects may reference their owner while the
        # client constructs them.  Send them only after every existing remote
        # GamePlayer has been registered for this late entrant.
        #
        # World drops are personal loot and intentionally do not appear here.
        combat = session.gameplay_combat_state
        if combat is not None:
            with combat.lock:
                active_summons = tuple(
                    sorted(
                        combat.player_summons.values(),
                        key=lambda value: value.actor_id,
                    )
                )
                active_location_visuals = (
                    combat.active_player_location_visuals()
                )
                active_traps = tuple(
                    sorted(
                        combat.player_traps.values(),
                        key=lambda value: value.trap_id,
                    )
                )
            if active_summons:
                CharacterService._send_player_summon_spawn_to(
                    session,
                    SimpleNamespace(
                        actors=active_summons,
                        discarded_actor_ids=(),
                    ),
                )
            if active_location_visuals:
                CharacterService._send_player_location_visuals_to(
                    session, active_location_visuals
                )
            for trap in active_traps:
                CharacterService._send_player_trap_spawn_to(session, trap)
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] announced character "
            f"{local_remote.character_id} as remote actor "
            f"{local_remote.actor_id} to {len(peers)} same-map peer(s)"
        )
        CharacterService._broadcast_presence_social_status(
            self, character.character_id
        )

    def _handle_presence_event(self, session: Session, event) -> None:
        character = session.gameplay_character
        if character is None:
            return
        # This pure social query must precede the common source flow lock.
        if isinstance(event, tuple) and event[0] == "pvp_hostile":
            return CharacterService._presence_pvp_hostility(
                self, session, event[1]
            )
        # Map-local scripted events: raised from static combat/movement code
        # that has no server handle.  They need the instance registry, not
        # the peer sets computed below.
        if isinstance(event, tuple) and event[0] == "map_signal":
            return CharacterService._fire_map_signal(
                self, session, event[1], event[2]
            )
        if isinstance(event, tuple) and event[0] == "monster_summons":
            # Monsters' summons landed this tick (services/monster_summons.py).
            return CharacterService._spawn_monster_summons(
                self, session, event[1]
            )
        if isinstance(event, tuple) and event[0] == "monsters_killed":
            return CharacterService._on_monsters_killed(
                self, session, event[1]
            )
        if isinstance(event, tuple) and event[0] == "item_collected":
            CharacterService._quest_on_item_collected(
                self, session, event[4] if len(event) > 4 else event[1],
                event[2] if len(event) > 2 else 1,
                quest_id=event[3] if len(event) > 3 else "",
            )
            return CharacterService._achievement_on_item_collected(
                self, session, event[1], event[2] if len(event) > 2 else 1
            )
        if isinstance(event, tuple) and event[0] == "level_up":
            self._fire_map_signal(session, "PlayerLevelUp", ())
            CharacterService._quest_ensure_parallel_world_introduction(
                self, session,
            )
            return CharacterService._achievement_on_class_level(
                self, session, event[1], event[2]
            )
        if event == "disconnect":
            with session.flow_lock:
                if session.gameplay_presence_departed:
                    return
                session.gameplay_presence_departed = True
            # Free the queue slot and unbind the match roster. A transfer into
            # the arena also lands here for the source socket, which the
            # release path distinguishes from a real departure.
            CharacterService._release_matchmaking_session(self, session)
        actor_id = (
            session.gameplay_remote_actor_id
            or CharacterService._remote_actor_id(character.character_id)
        )
        peers = self._active_gameplay_sessions(
            map_id=session.gameplay_map,
            excluding=session,
            instance_of=session,
        )
        if event == 'disconnect' and not peers and getattr(session, 'gameplay_instance_id', ''):
            runtime = getattr(session, 'gameplay_map_event_runtime', None)
            if runtime is not None:
                with self.gameplay_world_lock:
                    registry = self._map_event_registry()
                    if registry.get(runtime.instance_key) is runtime:
                        registry.pop(runtime.instance_key, None)
                with runtime.lock:
                    timers = tuple(runtime.pending.values())
                    runtime.pending.clear()
                    runtime.pending_start_deadlines.clear()
                for timer in timers:
                    timer.cancel()
        # Scanning for a replacement peer is departure-only work; it used to
        # run for every event, including the two the monster worker raises 25
        # times a second per map.
        if event == "disconnect" and any(
            int(peer.gameplay_character.character_id)
            == int(character.character_id)
            and int(peer.gameplay_remote_actor_id or 0) == int(actor_id)
            for peer in peers
        ):
            # The destination session can be announced before the source UDP
            # socket closes. Both share the stable character actor id, so a
            # stale ActorsLeft would delete the newly-created remote player.
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] suppressed stale departure for "
                f"replacement remote actor {actor_id}"
            )
            return
        with session.flow_lock:
            source_move = session.gameplay_last_move
        vicinity_peers = tuple(
            peer
            for peer in peers
            if (
                source_move is None
                or peer.gameplay_last_move is None
                or math.hypot(
                    source_move.relative_position[0]
                    - peer.gameplay_last_move.relative_position[0],
                    source_move.relative_position[2]
                    - peer.gameplay_last_move.relative_position[2],
                )
                <= PLAYER_ACTIVITY_VICINITY_RADIUS
            )
        )
        if isinstance(event, tuple) and event[0] == "claim_monster_world":
            # A source-map socket remains alive briefly while SwitchMap is
            # delivered reliably.  It has already left presence, however, and
            # must not continue owning the shared monster simulation.
            if (
                not session.connected
                or session.gameplay_presence_departed
                or session.gameplay_combat_state is None
            ):
                return False
            owner = CharacterService._monster_world_owner(
                session.gameplay_combat_state, (session, *peers)
            )
            if owner is None:
                # Distinct from "a peer owns this instance". Nobody can own it
                # right now because no viewer has a spawned monster, which is
                # an idle map rather than a retired one. The caller needs the
                # difference: retiring the simulation worker here is what left
                # a cleared pack with no owner at all.
                return None
            return owner is session
        if self._handle_socket_ally_auras(
            session, event, peers, character
        ):
            return None
        if isinstance(event, tuple) and event[0] == "combat_targets":
            targets = []
            for value in (session, *peers):
                with value.flow_lock:
                    move = value.gameplay_last_move
                    value_character = value.gameplay_character
                if move is None or value_character is None:
                    continue
                targets.append(
                    (
                        (
                            value.gameplay_remote_actor_id
                            or CharacterService._remote_actor_id(
                                value_character.character_id
                            )
                        ),
                        move.relative_position,
                        value,
                    )
                )
            return tuple(targets)
        if isinstance(event, tuple) and event[0] == "player_summon_spawn":
            spawn = event[1]
            combat = session.gameplay_combat_state
            for viewer in (session, *vicinity_peers):
                if viewer.gameplay_combat_state is combat:
                    CharacterService._send_player_summon_spawn_to(
                        viewer, spawn
                    )
            return None
        if isinstance(event, tuple) and event[0] == "player_trap_spawn":
            trap = event[1]
            combat = session.gameplay_combat_state
            for viewer in (session, *peers):
                if viewer.gameplay_combat_state is combat:
                    CharacterService._send_player_trap_spawn_to(viewer, trap)
            return None
        if isinstance(event, tuple) and event[0] == "player_trap_tripped":
            for viewer in (session, *peers):
                if viewer.gameplay_combat_state is session.gameplay_combat_state:
                    CharacterService._send_player_trap_tripped_to(
                        viewer, int(event[1])
                    )
            return None
        if isinstance(event, tuple) and event[0] == "player_trap_discard":
            for viewer in (session, *peers):
                if viewer.gameplay_combat_state is session.gameplay_combat_state:
                    CharacterService._send_player_trap_discard_to(
                        viewer, int(event[1])
                    )
            return None
        if (
            isinstance(event, tuple)
            and event[0] == "player_location_visuals"
        ):
            visuals = event[1]
            combat = session.gameplay_combat_state
            # This command replaces the client's complete map collection.
            # A peer that received an effect while nearby must also receive
            # the later snapshot that omits it, even if the players have
            # moved apart in the meantime.
            for viewer in (session, *peers):
                if viewer.gameplay_combat_state is combat:
                    CharacterService._send_player_location_visuals_to(
                        viewer, visuals
                    )
            return None
        if (
            isinstance(event, tuple)
            and event[0] == "monster_groups_announced"
        ):
            # The client resolves quest indication entries against actors it
            # has already created. A tracker refresh sent on map entry can
            # therefore omit a champion that is still outside the announce
            # radius. Re-publish the living list after each newly announced
            # group so every required commander receives a marker as soon as
            # the client is able to render it.
            self._quest_send_monster_indication(session)
            announced_actor_ids = {int(value) for value in event[1]}
            source_combat = session.gameplay_combat_state
            for viewer in vicinity_peers:
                with viewer.flow_lock:
                    encounter = viewer.gameplay_monster_encounter
                    combat = viewer.gameplay_combat_state
                    move = viewer.gameplay_last_move
                    already_announced = set(
                        viewer.gameplay_announced_monster_actor_ids
                    )
                if (
                    encounter is None
                    or combat is None
                    or combat is not source_combat
                    or move is None
                ):
                    continue
                player_world = encounter.coordinate_frame.wire_to_world(
                    move.relative_position
                )
                with combat.lock:
                    alive_actor_ids = set(combat.alive_actor_ids)
                    current_positions = dict(combat.monster_positions)
                groups = CharacterService._monster_groups_within_radius(
                    encounter,
                    world_position=player_world,
                    actor_ids=(
                        announced_actor_ids
                        .intersection(alive_actor_ids)
                        .difference(already_announced)
                    ),
                    positions=current_positions,
                    radius=MONSTER_VISIBILITY_ENTER_RADIUS,
                    extra_groups=combat.runtime_groups,
                )
                if not groups:
                    continue
                with viewer.flow_lock:
                    for group in groups:
                        viewer.gameplay_announced_monster_actor_ids.update(
                            group.actor_ids
                        )
                    viewer.gameplay_vicinity_announced = True
                for group in groups:
                    CharacterService._send_monster_vicinity(
                        viewer, combat, group,
                        label="monster-vicinity-peer-sync",
                    )
            return None
        if (
            isinstance(event, tuple)
            and event[0] == "player_summon_despawn"
        ):
            actor_ids = tuple(int(value) for value in event[1])
            for viewer in (session, *peers):
                CharacterService._send_player_summon_despawn_to(
                    viewer, actor_ids
                )
            return None
        if (
            isinstance(event, tuple)
            and event[0] == "player_summon_aura_refresh"
        ):
            commands = tuple(event[1])
            combat = session.gameplay_combat_state
            for viewer in (session, *peers):
                if viewer.gameplay_combat_state is not combat:
                    continue
                visible = set(viewer.gameplay_summon_actor_ids)
                viewer_commands = tuple(
                    command
                    for command in commands
                    if int(command.actor_id) in visible
                )
                CharacterService._send_player_summon_aura_refreshes_to(
                    viewer, viewer_commands
                )
            return None
        if isinstance(event, tuple) and event[0] == "monster_activity":
            (
                _kind,
                combat,
                movements,
                attacks,
                transitions,
                resolutions,
            ) = event
            owner_moves = {}
            for value in (session, *peers):
                with value.flow_lock:
                    value_character = value.gameplay_character
                    # Move is immutable; copying it per viewer on every
                    # monster tick only adds fan-out latency.
                    value_move = value.gameplay_last_move
                if value_character is None or value_move is None:
                    continue
                owner_moves[
                    (
                        value.gameplay_remote_actor_id
                        or CharacterService._remote_actor_id(
                            value_character.character_id
                        )
                    )
                ] = value_move
            for viewer in (session, *peers):
                if viewer.gameplay_combat_state is not combat:
                    continue
                visible = (
                    set(viewer.gameplay_spawned_monster_actor_ids)
                    | set(viewer.gameplay_summon_actor_ids)
                )
                # Coalesce unsent movement per viewer, while attacks,
                # transitions and Hit/Kill retain their reliable lifecycle.
                # The movement publisher retains the latest transform while
                # congested; dropping this slice loses terminal stops forever.
                viewer_movements = tuple(value for value in movements if value.actor_id in visible)
                viewer_attacks = tuple(
                    value
                    for value in attacks
                    if value.attacker_actor_id in visible
                )
                viewer_transitions = tuple(
                    value
                    for value in transitions
                    if value.actor_id in visible
                )
                viewer_resolutions = tuple(
                    value
                    for value in resolutions
                    if (
                        value.attacker_actor_id in visible
                        or value.target_actor_id in visible
                    )
                )
                if (
                    viewer_movements
                    or viewer_attacks
                    or viewer_transitions
                    or viewer_resolutions
                    or viewer.gameplay_pending_monster_movements
                    or (
                        viewer.gameplay_monster_stop_repeats
                        and time.perf_counter()
                        >= viewer.gameplay_monster_stop_repeat_next_at
                    )
                ):
                    CharacterService._send_monster_activity_to(
                        viewer,
                        combat,
                        viewer_movements,
                        viewer_attacks,
                        viewer_transitions,
                        viewer_resolutions,
                        owner_moves=owner_moves,
                    )
            # Monster-versus-monster damage (for example mind control) has
            # no player kill callback. It still changes clear-region goals,
            # even for players too far away to receive the combat visuals.
            killed = {}
            for resolution in resolutions:
                if not resolution.killed:
                    continue
                monster = combat.monsters.get(resolution.target_actor_id)
                if monster is not None:
                    template = monster.template_id
                    killed[template] = killed.get(template, 0) + 1
            if killed and session.gameplay_combat_state is combat:
                self._quest_check_shared_cleared_regions(session, killed)
            return None
        if (
            isinstance(event, tuple)
            and event[0] in {"player_bullet", "player_skill"}
        ):
            CharacterService._send_remote_player_skill(
                session, event, vicinity_peers, source_move
            )
            return None
        if (
            isinstance(event, tuple)
            and event[0] in (
                "player_combat_resolution",
                "player_combat_resolutions",
            )
        ):
            if event[0] == "player_combat_resolution":
                dispatches = ((event[1], event[2], event[3]),)
            else:
                dispatches = tuple(event[1])
            # The mapped combat/status payloads are immutable and commonly
            # identical for every remote viewer.  Keep this cache scoped to
            # one authoritative event so it can never outlive the state
            # transition it represents.
            packet_cache = {}
            CharacterService._schedule_monster_death_despawn(
                self, session, session.gameplay_combat_state,
                dispatches, (session, *peers),
            )
            for viewer in (session, *peers):
                visible_dispatches = []
                for resolution, label, loot_generation in dispatches:
                    if (
                        resolution.target_actor_id
                        in viewer.gameplay_spawned_monster_actor_ids
                    ):
                        mapped = replace(
                            resolution,
                            target_actor_id=(
                                CharacterService._viewer_actor_id(
                                    viewer, resolution.target_actor_id
                                )
                            ),
                            attacker_actor_id=(
                                CharacterService._viewer_actor_id(
                                    viewer, resolution.attacker_actor_id
                                )
                            ),
                            source_actor_id=(
                                CharacterService._viewer_actor_id(
                                    viewer, resolution.source_actor_id
                                )
                                if resolution.source_actor_id is not None
                                else None
                            ),
                        )
                        visible_dispatches.append(
                            (resolution, mapped, label)
                        )
                    if (
                        viewer is session
                        and
                        loot_generation is not None
                        and loot_generation.drops
                    ):
                        with viewer.flow_lock:
                            viewer.gameplay_next_item_id = max(
                                viewer.gameplay_next_item_id,
                                loot_generation.next_item_id,
                            )
                            viewer.gameplay_world_loot.update(
                                {
                                    drop.item.item_id: drop
                                    for drop in loot_generation.drops
                                }
                            )
                try:
                    if visible_dispatches:
                        CharacterService._send_combat_resolutions(
                            viewer,
                            tuple(item[1] for item in visible_dispatches),
                            label=(
                                visible_dispatches[0][2]
                                if len(visible_dispatches) == 1
                                else "player-hit-batch"
                            ),
                            packet_cache=packet_cache,
                        )
                finally:
                    for resolution, _mapped, _label in visible_dispatches:
                        if not resolution.killed:
                            continue
                        # Authoritative death must always retire the viewer's
                        # registration, even if a malformed packet fails to
                        # encode after combat state has already committed.
                        with viewer.flow_lock:
                            viewer.gameplay_spawned_monster_actor_ids.discard(
                                resolution.target_actor_id
                            )
                            viewer.gameplay_delivered_monster_positions.pop(
                                resolution.target_actor_id, None
                            )
                            viewer.gameplay_announced_monster_actor_ids.discard(
                                resolution.target_actor_id
                            )
                            viewer.gameplay_requested_actor_ids.discard(
                                resolution.target_actor_id
                            )
                            viewer.gameplay_monsters_spawned = bool(
                                viewer.gameplay_spawned_monster_actor_ids
                            )
                if viewer is session and any(
                    loot_generation is not None and loot_generation.drops
                    for _resolution, _label, loot_generation in dispatches
                ):
                    CharacterService._reconcile_world_loot_visibility(
                        viewer,
                        label="world-loot-spawn",
                    )
            return None
        if (
            isinstance(event, tuple)
            and event[0] == "pvp_combat_resolution"
        ):
            resolution = event[1]
            combat = session.gameplay_combat_state
            with combat.lock:
                summon = getattr(combat, "player_summons", {}).get(resolution.target_actor_id)
                summon_owner = summon.owner_actor_id if summon is not None else None
            if resolution.killed and session.gameplay_match is None:
                victim = next((peer for peer in peers
                               if int(peer.gameplay_remote_actor_id)
                               == int(resolution.target_actor_id)), None)
                if victim is not None and victim.gameplay_match is None:
                    death_key = (id(combat), int(resolution.server_tick),
                                 int(resolution.source_actor_id or resolution.attacker_actor_id))
                    with victim.flow_lock:
                        first_death = (
                            not victim.gameplay_death_pending
                            and getattr(victim, "gameplay_achievement_pvp_death", None) != death_key
                        )
                        if first_death:
                            victim.gameplay_achievement_pvp_death = death_key
                    if first_death:
                        self._achievement_feed(session, "pvp_kill", ("open",))
            # A travelling hit/DoT may land after the victim leaves the
            # caster's vicinity. Always deliver to the victim: its local
            # status cache also drives authoritative debuffs and DoT lifetime.
            viewers = (session, *tuple(
                peer for peer in peers
                if peer in vicinity_peers or int(peer.gameplay_remote_actor_id)
                == int(resolution.target_actor_id)
                or (summon_owner is not None and (
                    int(peer.gameplay_remote_actor_id) == summon_owner
                    or resolution.target_actor_id in peer.gameplay_summon_actor_ids
                ))
            ))
            for viewer in viewers:
                mapped = replace(
                    resolution,
                    target_actor_id=CharacterService._viewer_actor_id(
                        viewer, resolution.target_actor_id
                    ),
                    attacker_actor_id=CharacterService._viewer_actor_id(
                        viewer, resolution.attacker_actor_id
                    ),
                    source_actor_id=(
                        CharacterService._viewer_actor_id(
                            viewer, resolution.source_actor_id
                        )
                        if resolution.source_actor_id is not None
                        else None
                    ),
                )
                CharacterService._send_combat_resolution(
                    viewer, mapped, label="pvp-player-hit",
                    include_item_set_procs=False,
                )
            return None
        if isinstance(event, tuple) and event[0] == "world_loot_picked":
            # Personal loot is removed only from the owner's session by the
            # pickup handler. Other players have independent drop IDs/state.
            return None
        if isinstance(event, tuple) and event[0] == "monster_visibility_changed":
            combat = session.gameplay_combat_state
            if combat is None:
                return None
            CharacterService._reconcile_active_monster_union(
                combat, (session, *peers)
            )
            # The claim's own rule: a re-election that picks someone the claim
            # rejects starts nothing, and the worker asking for it retires.
            owner = CharacterService._monster_world_owner(
                combat, (session, *peers)
            )
            if owner is not None:
                CharacterService._start_monster_chase(owner)
            return None
        if isinstance(event, tuple) and event[0] == "player_revived":
            _, position, heading, health, resource = event
            for peer in peers:
                CharacterService._send_player_revive_to(
                    peer, actor_id=actor_id, position=position, heading=heading,
                    health=health, resource=resource,
                )
            return
        if event == "move":
            if getattr(self, "_presence_movement_publisher_running", False):
                # The 25 Hz movement thread relays it in each viewer's batch.
                return
            with session.flow_lock:
                move = session.gameplay_last_move
            if move is None:
                return
            # Standing players get duration 0, the client's hard stop; moving
            # ones the retail 18-tick dead-reckoning horizon.
            move = relayed_player_move(move)
            packet = encode_move(move, actor_id=actor_id)
            for peer in vicinity_peers:
                peer.send_unreliable_sequenced(packet)
            return
        if isinstance(event, tuple) and event[0] == "move_correction":
            # A refused move is never relayed, so peers would keep walking the
            # player along its last accepted segment and then snap. Give them
            # the same stop at the server position that the owner receives.
            packet = encode_move(event[1], actor_id=actor_id)
            for peer in vicinity_peers:
                peer.send_unreliable_sequenced(packet)
            return
        if event == "appearance":
            remote = self._remote_player_for(session)
            packet = encode_remote_player_info(remote)
            for peer in vicinity_peers:
                peer.send_reliable_ordered(
                    packet,
                    track=True,
                    label=f"remote-player-info-{remote.actor_id}",
                )
            return
        if isinstance(event, tuple) and event[0] in {
            "status_effect",
            "status_effects",
        }:
            actor_id = (
                session.gameplay_remote_actor_id
                or CharacterService._remote_actor_id(
                    character.character_id
                )
            )
            values = (
                (event[1],)
                if event[0] == "status_effect"
                else tuple(event[1])
            )
            remapped = tuple(
                CharacterService._remap_player_status_effect(
                    value, actor_id
                )
                for value in values
            )
            if not remapped:
                return
            if len(remapped) == 1:
                packet = encode_status_effect_batch(
                    remapped[0], actor_id=actor_id
                )
            else:
                packet = encode_monster_spawn_batch(
                    MonsterSpawnBatch(
                        tuple(
                            StatusEffectServerCommand(
                                value,
                                actor_id,
                            )
                            for value in remapped
                        )
                    )
                )
            for peer in vicinity_peers:
                CharacterService._observe_remote_visual_status_removals(
                    peer, actor_id, remapped
                )
                peer.send_reliable_ordered(
                    packet,
                    track=True,
                    label=f"remote-player-status-{actor_id}",
                )
            return
        if isinstance(event, tuple) and event[0] == "overhead_icons":
            for peer in peers:
                CharacterService._send_player_overhead_icons_to(
                    peer, session, actor_id
                )
            return
        if isinstance(event, tuple) and event[0] == "pvp_flag":
            enabled, server_tick = bool(event[1]), int(event[2])
            packet = encode_pvp_flag_response(
                PVPFlag(enabled, server_tick), actor_id=actor_id
            )
            for peer in peers:
                peer.send_reliable_ordered(
                    packet,
                    track=True,
                    label=f"remote-player-pvp-{actor_id}",
                )
                CharacterService._send_player_overhead_icons_to(
                    peer, session, actor_id
                )
            combat = session.gameplay_combat_state
            if combat is not None:
                with combat.lock:
                    summons = tuple(getattr(combat, "player_summons", {}).values())
                for viewer in (session, *peers):
                    for summon in summons:
                        if summon.actor_id not in viewer.gameplay_summon_actor_ids:
                            continue
                        group = CharacterService._summon_group_for_session(viewer, summon, summon.spawn_group)
                        viewer.send_reliable_ordered(encode_monster_update_batch(
                            player_summon_preparation_batch(group, None, owner_actor_id=summon.owner_actor_id)
                        ), track=True, label="player-summon-faction-refresh")
            return
        if isinstance(event, tuple) and event[0] == "actor_request":
            # The client can request an actor while it is still decoding the
            # local initialization stream.  Serving a remote GamePlayer at
            # that point reproduces the retail Ptr<GamePlayer> null assertion.
            if not session.gameplay_presence_ready:
                return None
            requested_actor_id = int(event[1])
            for peer in peers:
                remote = self._remote_player_for(peer)
                if remote.actor_id != requested_actor_id:
                    continue
                CharacterService._forget_remote_visual_status(
                    session, remote.actor_id
                )
                CharacterService._send_remote_player_actor_to(
                    session, remote
                )
                status = CharacterService._remote_status_snapshot(
                    peer, remote.actor_id
                )
                if status.entries:
                    session.send_reliable_ordered(
                        encode_status_effect_batch(
                            status,
                            actor_id=remote.actor_id,
                        ),
                        track=True,
                        label=(f"remote-player-status-" f"{remote.actor_id}"),
                    )
                session.send_reliable_ordered(
                    encode_pvp_flag_response(
                        PVPFlag(
                            bool(peer.gameplay_pvp_enabled),
                            int(peer.gameplay_chase_server_tick) & 0xFFFFFFFF,
                        ),
                        actor_id=remote.actor_id,
                    ),
                    track=True,
                    label=f"remote-player-pvp-{remote.actor_id}",
                )
                CharacterService._send_player_overhead_icons_to(
                    session, peer, remote.actor_id
                )
                return
        if event == "disconnect" and peers:
            # ActorsLeftVicinity leaves PlayerManager/ActorManager entries
            # alive. Retail map departures use DiscardPlayer so a returning
            # actor generation can register at the same low actor index.
            packet = encode_discard_player(actor_id)
            for peer in peers:
                CharacterService._forget_remote_visual_status(
                    peer, actor_id
                )
                peer.send_reliable_ordered(
                    packet,
                    track=True,
                    label=f"remote-player-left-{actor_id}",
                )
        if event == "disconnect":
            combat = session.gameplay_combat_state
            with session.flow_lock:
                pending_despawn_actor_ids = tuple(
                    int(timer_key[1])
                    for timer_key in session.gameplay_projectile_timers
                    if (
                        isinstance(timer_key, tuple)
                        and len(timer_key) == 2
                        and timer_key[0] == "player-summon-discard"
                    )
                )
            if combat is not None:
                with combat.lock:
                    summon_actor_ids = (
                        combat.expire_player_summons_for_owner(actor_id)
                    )
                    location_visuals = (
                        combat.expire_player_location_visuals_for_owner(
                            actor_id
                        )
                    )
                summon_actor_ids = tuple(
                    dict.fromkeys(
                        (
                            *summon_actor_ids,
                            *pending_despawn_actor_ids,
                        )
                    )
                )
                if summon_actor_ids:
                    for peer in peers:
                        CharacterService._send_player_summon_despawn_to(
                            peer, summon_actor_ids
                        )
                for peer in peers:
                    if peer.gameplay_combat_state is combat:
                        CharacterService._send_player_location_visuals_to(
                            peer, location_visuals
                        )
                combat.unregister_player(actor_id)
                CharacterService._reconcile_active_monster_union(
                    combat, peers
                )
            next_owner = CharacterService._monster_world_owner(combat, peers)
            if next_owner is not None:
                CharacterService._start_monster_chase(next_owner)
            CharacterService._broadcast_presence_social_status(
                self, character.character_id
            )
