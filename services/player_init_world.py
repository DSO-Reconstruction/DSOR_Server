"""Map load, command construction, and delivery for player initialization.

The phases here run after
:class:`~dso_server.services.player_init_state.PlayerInitializationState` is
committed to the session: the map and its encounter are loaded, the typed 0x85
batch is built and split, and the follow-up snapshots the client does not
request on its own are sent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.services.player_init_state import PlayerInitializationState
from dso_server.protocol.reward_protocol import daily_login_offers
from dso_server.protocol.player_init_protocol import (
    ACTORS_ENTER_VICINITY_COMMAND,
    NEW_NPC_COMMAND,
    OBSTACLE_UPDATE_COMMAND,
    QuickSlotsInfo,
    SeasonPassRewardsInfoResult,
    TIMED_CHALLENGES_INFO_COMMAND,
    VARIATION_INFO_COMMAND,
)
from dso_server.protocol.gameplay_protocol import (
    ActorsEnterVicinity,
    ActorsEnterVicinityBatch,
    encode_actors_enter_vicinity_batch,
)
from dso_server.protocol.skill_book_protocol import skill_book_snapshot
from dso_server.gameplay.skin_unlocks import load_skin_unlock_customizations


@dataclass
class PlayerInitializationWorld:
    """Map content and combat state loaded for one initialization."""

    map_commands: Any
    exit_info: Any
    crafting_props: Any
    monster_encounter: Any
    skill_book_ids: tuple
    combat_state: Any


class PlayerInitializationWorldService:
    """Load the map, build the initialization batch, and deliver it."""

    def _load_initialization_world(
            self, s: Session,
            state: PlayerInitializationState) -> PlayerInitializationWorld:
        """Load map content, join or create the shared encounter state."""

        character = state.character
        gameplay_map = state.gameplay_map
        map_access = state.map_access
        # PropInfo is keyed to the concrete player actor for this map session,
        # even on hub maps that do not create a combat encounter.
        CharacterService._session_remote_actor_id(self, s)
        # Before anything reads the quest log (reveal flags, spawns, the
        # client's QuestLogInfo): event quests out of season are set aside.
        try:
            CharacterService._quest_park_out_of_season(self, s)
        except Exception as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] parking out-of-season quests "
                f"failed: {type(exc).__name__}: {exc}"
            )
        active_event_flags = frozenset({
            *CharacterService._event_catalog(
                self
            ).active_event_flags(),
            *map_access.event_set_flags,
        })
        try:
            active_event_flags |= frozenset(
                CharacterService._quest_reveal_flags(self, s, gameplay_map)
            )
        except Exception:
            pass
        map_commands, exit_info = load_local_map_initialization(
            self.static_database_path,
            gameplay_map,
            active_event_flags=active_event_flags,
            # Register every map NPC before a map-local EventUpdate can
            # activate its scene set. The client owns event visibility; if a
            # gated NPC is first announced after that update, the client has
            # already reserved the row's actor id and rejects our NewNPC.
            include_inactive_event_sets=True,
            selected_map_difficulty=map_access.map_difficulty,
            selected_map_access_id=map_access.request_map_id,
        )
        # Quest state is restored by this phase. Rebuild the authoritative
        # exits from the same reveal flags as the scene snapshot, rather than
        # retaining the earlier snapshot built before quest restoration.
        revealed_exits = tuple(
            definition
            for definition in load_local_map_exits(
                self.static_database_path, gameplay_map,
                active_event_flags=active_event_flags,
            )
            if definition.active and definition.destination_map_id
        )
        with s.flow_lock:
            s.gameplay_map_exits = tuple(filter_parallel_world_exits_for_access(
                self.static_database_path, gameplay_map, revealed_exits,
                selected_map_access_id=map_access.request_map_id,
                selected_map_difficulty=map_access.map_difficulty,
            ))
        native_exit_commands = tuple(
            command for command in map_commands
            if command.command_id == VARIATION_INFO_COMMAND
        )
        if native_exit_commands:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] explicit native exit creation on "
                f"{gameplay_map}: access={map_access.request_map_id!r}, "
                f"entities={sum(len(c.value.entries) for c in native_exit_commands)}"
            )
        monster_encounter = load_local_monster_encounter(
            self.static_database_path,
            gameplay_map,
            monster_level=map_access.monster_level,
            scaling_difficulty=map_access.monster_scaling_difficulty,
            player_position=state.spawn_position,
            active_event_flags=active_event_flags,
        )
        if monster_encounter is None:
            # Some maps spawn every monster from a map-local event (arena
            # waves, boss phases). They still need a combat world to spawn
            # into once the event fires.
            monster_encounter = (
                CharacterService._map_event_encounter_fallback(
                    self.static_database_path, gameplay_map,
                    scaling_difficulty=map_access.monster_scaling_difficulty,
                )
            )
        if monster_encounter is None and CharacterService._entering_battleground(
            self, s, gameplay_map
        ):
            # A battleground spawns no monsters, and "no monsters" otherwise
            # means "no combat state", which leaves the players unregistered
            # and every cast rejected. The arena needs the combat world even
            # though nothing in it is a monster.
            monster_encounter = load_local_player_only_encounter(
                self.static_database_path, gameplay_map
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] {gameplay_map} is a battleground "
                "with no monster population; using a player-only combat "
                "encounter"
            )
        if monster_encounter is None and bool(
            getattr(character, "admin_privileges", False)
        ):
            # Hubs normally have no combat world, so the client rejects
            # skill casts there. Give GM characters an empty player-only
            # combat state while keeping city monster spawns disabled.
            try:
                ruleset = self.static_database.gameplay_map_ruleset(gameplay_map)
            except (AttributeError, KeyError, OSError, ValueError):
                ruleset = 5 if gameplay_map in {"a0200_kingscity", "b1200_hub"} else 1
            if int(ruleset) == 5:
                monster_encounter = load_local_player_only_encounter(
                    self.static_database_path, gameplay_map
                )
        skill_book_ids = tuple(
            self.static_database.player_skill_manager_indices(
                character.class_id
            )
        )
        combat_state = None
        if monster_encounter is not None:
            server_player_actor_id = (
                CharacterService._session_remote_actor_id(self, s)
            )
            match_registry = getattr(self, "_pvp_matchmaking", None)
            match = (
                match_registry.match_for_character(
                    int(character.character_id)
                )
                if match_registry is not None
                and CharacterService._entering_battleground(
                    self, s, gameplay_map
                )
                else None
            )

            if match is not None:
                # Match entrants may both be inside world initialization at
                # once, so neither is discoverable through the ready-peer
                # presence list yet. Publish one combat state on the roster
                # under its lock and have both sessions register in that state.
                with match.lock:
                    combat_state = match.gameplay_combat_state
                if combat_state is None:
                    candidate = LocalCombatState(
                        monster_encounter,
                        player_actor_id=server_player_actor_id,
                        player_health=state.maximum_health,
                        player_current_health=max(
                            1, int(round(state.inventory.health_points))
                        ),
                        player_health_regeneration_fraction=(
                            state.health_regeneration_fraction
                        ),
                        player_health_regeneration_absolute=(
                            state.health_regeneration_absolute
                        ),
                        player_health_regeneration_interval=(
                            state.health_regeneration_interval
                        ),
                        player_maximum_resource=state.maximum_resource,
                        player_current_resource=float(
                            state.inventory.secondary_resource
                        ),
                        player_resource_regeneration_fraction=(
                            state.resource_regeneration_fraction
                        ),
                        player_resource_regeneration_absolute=(
                            state.resource_regeneration_absolute
                        ),
                        player_resource_regeneration_interval=(
                            state.resource_regeneration_interval
                        ),
                        player_resource_regeneration_delay=(
                            state.resource_regeneration_delay
                        ),
                        initially_active=False,
                    )
                    with match.lock:
                        if match.gameplay_combat_state is None:
                            match.gameplay_combat_state = candidate
                        combat_state = match.gameplay_combat_state

            def shared_combat_state():
                return next(
                    (
                        peer.gameplay_combat_state
                        for peer in self._active_gameplay_sessions(
                            map_id=gameplay_map,
                            excluding=s,
                            instance_of=s,
                        )
                        if (
                            peer.gameplay_combat_state is not None
                            and peer.gameplay_monster_encounter is not None
                        )
                    ),
                    None,
                )

            if combat_state is None:
                with self.gameplay_world_lock:
                    combat_state = shared_combat_state()
            if combat_state is None:
                candidate = LocalCombatState(
                    monster_encounter,
                    player_actor_id=server_player_actor_id,
                    player_health=state.maximum_health,
                    player_current_health=max(
                        1, int(round(state.inventory.health_points))
                    ),
                    player_health_regeneration_fraction=(
                        state.health_regeneration_fraction
                    ),
                    player_health_regeneration_absolute=(
                        state.health_regeneration_absolute
                    ),
                    player_health_regeneration_interval=(
                        state.health_regeneration_interval
                    ),
                    player_maximum_resource=state.maximum_resource,
                    player_current_resource=float(
                        state.inventory.secondary_resource
                    ),
                    player_resource_regeneration_fraction=(
                        state.resource_regeneration_fraction
                    ),
                    player_resource_regeneration_absolute=(
                        state.resource_regeneration_absolute
                    ),
                    player_resource_regeneration_interval=(
                        state.resource_regeneration_interval
                    ),
                    player_resource_regeneration_delay=(
                        state.resource_regeneration_delay
                    ),
                    initially_active=False,
                )
                # Construction performs database/cache work outside the world
                # registry lock. A second entrant may have published a shared
                # state meanwhile, so select it under one short recheck.
                with self.gameplay_world_lock:
                    combat_state = shared_combat_state() or candidate
            monster_encounter = combat_state.encounter
            with combat_state.lock:
                combat_state.register_player(
                    server_player_actor_id,
                    state.maximum_health,
                    current_health=max(
                        1, int(round(state.inventory.health_points))
                    ),
                    regeneration_fraction=(
                        state.health_regeneration_fraction
                    ),
                    regeneration_absolute=(
                        state.health_regeneration_absolute
                    ),
                    regeneration_interval=(
                        state.health_regeneration_interval
                    ),
                    maximum_resource=state.maximum_resource,
                    current_resource=float(
                        state.inventory.secondary_resource
                    ),
                    resource_regeneration_fraction=(
                        state.resource_regeneration_fraction
                    ),
                    resource_regeneration_absolute=(
                        state.resource_regeneration_absolute
                    ),
                    resource_regeneration_interval=(
                        state.resource_regeneration_interval
                    ),
                    resource_regeneration_delay=(
                        state.resource_regeneration_delay
                    ),
                )
        with self.gameplay_world_lock:
            peers_present = bool(self._active_gameplay_sessions(
                map_id=gameplay_map, excluding=s, instance_of=s,
            ))
        map_event_runtime = CharacterService._attach_map_event_runtime(
            self, s,
            map_id=gameplay_map,
            initial_flags=active_event_flags,
            monster_level=map_access.monster_level,
            scaling_difficulty=map_access.monster_scaling_difficulty,
            # Match entrants initialize concurrently, before either is a
            # presence-ready peer. Their instance key is the match's own
            # (pvp-match-<id>), so they always join one runtime -- like the
            # match's shared combat state -- instead of each starting one.
            fresh_instance=not peers_present and not (
                CharacterService._entering_battleground(self, s, gameplay_map)
            ),
        )
        CharacterService._quest_prepare_map(self, s)
        crafting_props = CharacterService._map_event_prop_info(s)
        if crafting_props is None:
            crafting_props = load_local_crafting_props(
                self.static_database_path,
                gameplay_map,
            )
        if map_event_runtime is not None and map_event_runtime.running:
            # A late joiner sees the exits, props and NPCs the instance's
            # events already revealed.
            with map_event_runtime.lock:
                runtime_flags = map_event_runtime.active_flags()
            try:
                reveal_flags = frozenset(
                    CharacterService._quest_reveal_flags(self, s, gameplay_map)
                )
            except Exception:
                reveal_flags = frozenset()
            map_commands, exit_info = load_local_map_initialization(
                self.static_database_path,
                gameplay_map,
                active_event_flags=active_event_flags | runtime_flags | reveal_flags,
                include_inactive_event_sets=True,
                selected_map_difficulty=map_access.map_difficulty,
                selected_map_access_id=map_access.request_map_id,
            )
            with s.flow_lock:
                late_join_exits = tuple(
                    definition
                    for definition in load_local_map_exits(
                        self.static_database_path,
                        gameplay_map,
                        active_event_flags=active_event_flags | runtime_flags | reveal_flags,
                    )
                    if definition.active and definition.destination_map_id
                )
                s.gameplay_map_exits = tuple(
                    definition
                    for definition in filter_parallel_world_exits_for_access(
                        self.static_database_path,
                        gameplay_map,
                        late_join_exits,
                        selected_map_access_id=map_access.request_map_id,
                    )
                )
        with s.flow_lock:
            old_chase_timer = s.gameplay_chase_timer
            old_projectile_timers = tuple(
                s.gameplay_projectile_timers.values()
            )
            # Track which event NPCs were sent in this map's initialization.
            try:
                from dso_server.protocol.player_init_protocol import (
                    NEW_NPC_COMMAND as _INIT_NEW_NPC,
                )
                s.gameplay_announced_npc_actor_ids = {
                    int(command.value.actor_id)
                    for command in map_commands
                    if getattr(command, "command_id", None) == _INIT_NEW_NPC
                }
            except Exception:
                s.gameplay_announced_npc_actor_ids = set()
            s.gameplay_projectile_timers.clear()
            s.gameplay_runtime_status_effects.clear()
            s.gameplay_victim_status_effect_instances.clear()
            s.gameplay_effect_cast_applications = {}
            s.gameplay_chase_generation += 1
            s.gameplay_chase_timer = None
            s.gameplay_chase_last_at = None
            s.gameplay_chase_server_tick = 0
            s.gameplay_chase_clock_anchor = None
            s.gameplay_monster_encounter = monster_encounter
            s.gameplay_combat_state = combat_state
            # A session can survive map transfers. Never validate the first
            # move on a new map against its previous map's coordinates.
            s.gameplay_last_move = None
            s.gameplay_move_budget = 0.0
            s.gameplay_movement_allowance = None
            s.gameplay_move_correction_at = 0.0
            s.gameplay_skill_book_ids = frozenset(skill_book_ids)
            s.gameplay_vicinity_announced = False
            s.gameplay_announced_monster_actor_ids.clear()
            s.gameplay_requested_actor_ids.clear()
            s.gameplay_monsters_spawned = False
            s.gameplay_spawned_monster_actor_ids.clear()
            s.gameplay_new_monster_sent_actor_ids.clear()
            s.gameplay_delivered_monster_positions.clear()
            s.gameplay_no_target_cast_streak = 0
            s.gameplay_last_monster_resync_at = 0.0
            s.gameplay_tower_beam_started_at = {}
            s.gameplay_summon_actor_ids.clear()
            s.gameplay_location_effect_ids.clear()
            s.gameplay_remote_visual_status_effects.clear()
        if old_chase_timer is not None:
            old_chase_timer.cancel()
        for projectile_timer in old_projectile_timers:
            projectile_timer.cancel()
        if map_event_runtime is not None and combat_state is not None:
            # Trigger volumes can fire while this first initialization is
            # constructing the combat state. If an event is already running,
            # materialize its gated actors now; the normal post-PlayerReady
            # pass below announces them after the initialization stream.
            with map_event_runtime.lock:
                running_event_sets = frozenset(
                    map_event_runtime.enabled_event_sets
                )
            CharacterService._spawn_event_monsters(
                self, map_event_runtime, (s,), running_event_sets
            )
        return PlayerInitializationWorld(
            map_commands=map_commands,
            exit_info=exit_info,
            crafting_props=crafting_props,
            monster_encounter=monster_encounter,
            skill_book_ids=skill_book_ids,
            combat_state=combat_state,
        )

    def _build_initialization_commands(
            self, s: Session, state: PlayerInitializationState,
            world: PlayerInitializationWorld) -> tuple:
        """Construct the client-supported command list and pin QuickSlots."""

        character = state.character
        persisted_state = state.persisted_state
        now = datetime.now(timezone.utc)
        claims = (
            dict(persisted_state.timed_offer_purchases)
            if persisted_state is not None
            else {}
        )
        daily_day = pending_daily_login_day(claims, today=now.date())
        class_name = CLASS_NAMES[int(character.class_id)].lower()
        pending_booking_notifications = []
        for claim_key in claims:
            parsed_booking = parse_ocp_booking_pending_key(claim_key)
            if parsed_booking is None:
                continue
            booking_guid, booking_offer_id = parsed_booking
            try:
                booking_reward = resolve_ocp_booking_reward(
                    booking_offer_id, 'external'
                )
            except ValueError as exc:
                print(
                    f'[{GAMEPLAY_SERVICE_NAME}] ignoring invalid pending '
                    f'OCP booking {claim_key!r}: {exc}'
                )
                continue
            pending_booking_notifications.append(
                make_notification_entry(
                    OCP_BOOKING_NOTIFICATION_KIND,
                    booking_guid,
                    OCP_BOOKING_NOTIFICATION_VALUES,
                    ocp_booking_notification_payload(
                        booking_reward, booking_guid
                    ),
                )
            )
        notifications = tuple(pending_booking_notifications)
        if DAILY_LOGIN_REWARDS_ENABLED and daily_day is not None:
            notifications += (
                make_notification_entry(
                    6,
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        "dso-local-daily-login:"
                        + str(getattr(character, "character_id", 0))
                        + ":"
                        + now.date().isoformat(),
                    ).bytes,
                    (
                        0x0DB05EC2,
                        server_time() & 0xFFFFFFFF,
                        server_time() & 0xFFFFFFFF,
                    ),
                    {
                        "reward": daily_login_reward_group(
                            class_name, daily_day
                        ),
                        "dailyLoginCount": daily_day,
                        "rewardIndex": daily_day - 1,
                        "premiumReward": premium_daily_login_reward_group(
                            class_name, daily_day
                        ),
                    },
                ),
            )
        # Announce the active events.json entries once per login session.
        # They ride in the same NotificationCommand as the daily login popup;
        # see docs/EVENT_NOTIFICATIONS.md for the client contract.
        notifications += (
            CharacterService._event_notification_entries_for_session(
                self, s, character, now=now
            )
        )
        activity_days = []
        for key, sequence in sorted(claims.items()):
            if not key.startswith("activity-signin:"):
                continue
            try:
                calendar_date = datetime.strptime(
                    key.removeprefix("activity-signin:"), "%Y-%m-%d"
                ).date()
            except ValueError:
                continue
            activity_days.append(
                ActivitySigninDay(
                    (
                        calendar_date.year * 10000
                        + calendar_date.month * 100
                        + calendar_date.day,
                        0,
                        0xFFFFFFFF,
                        int(sequence),
                    )
                )
            )
        (
            event_update,
            event_progress_info,
            event_participation_info,
        ) = CharacterService._event_initialization_state(
            self,
            s,
            character,
            map_id=state.gameplay_map,
            map_access_group_id=state.map_access.access_group_id,
        )
        self._season_pass_daily_login(s)
        season_pass_state = CharacterService._season_pass_document(s)
        route_quick_slots = getattr(character, "route_quick_slots", None)
        if isinstance(s.gameplay_quick_slots, QuickSlotsInfo):
            quick_slots_info = copy.deepcopy(s.gameplay_quick_slots)
        elif isinstance(route_quick_slots, QuickSlotsInfo):
            quick_slots_info = copy.deepcopy(route_quick_slots)
        elif persisted_state is not None:
            quick_slots_info = copy.deepcopy(persisted_state.quick_slots)
        else:
            quick_slots_info = None
        commands = list(build_local_player_init_commands(
            state.new_player,
            state.inventory,
            CharacterService._quest_decorate_map_commands(
                self, s, world.map_commands,
            ),
            exit_info=world.exit_info,
            actor_id=LOCAL_PLAYER_ACTOR_ID,
            quest_log=CharacterService._quest_log_info(self, s),
            chapter_progress=CharacterService._quest_chapter_progress(self, s),
            timed_challenges=CharacterService._daily_challenge_initialization_info(self, s),
            item_customizations=load_skin_unlock_customizations(
                self.static_database_path,
                class_name=class_name,
            ),
            special_offer=(daily_login_offers(self.static_database_path, class_name)
                           if DAILY_LOGIN_REWARDS_ENABLED else None),
            skill_book_ids=world.skill_book_ids,
            skill_book=skill_book_snapshot(self.static_database_path,
                                          character.class_id, character.level),
            quickbar_skill_slots=self.static_database.quickbar_skill_slots(
                character.class_id,
                character.level,
                getattr(character, 'quickbar_skill_slots', None),
            ),
            quickbar_stickerbook_ids=(
                BLACK_BEAR_MOUNT.stickerbook_resource_id,
            )
            if (
                BLACK_BEAR_MOUNT.stickerbook_resource_id
                in s.gameplay_stickerbook_entries
            )
            else (),
            quick_slots_info=quick_slots_info,
            stickerbook_entries=tuple(
                s.gameplay_stickerbook_entries[key]
                for key in sorted(s.gameplay_stickerbook_entries)
            ),
            recipe_book=RecipeBookInfo(tuple(
                RecipeBookEntry(resource_id, resource_id)
                for resource_id in s.gameplay_recipe_book_resources
            )),
            # Replay earned progress and the original completion stamps.
            achievements=CharacterService._achievement_init_info(self, s),
            persistent_status_effects=(
                tuple(
                    s.gameplay_persistent_status_effects[key]
                    for key in sorted(
                        s.gameplay_persistent_status_effects
                    )
                )
                + tuple(
                    s.gameplay_item_set_effects[key]
                    for key in sorted(s.gameplay_item_set_effects)
                )
                + tuple(
                    s.gameplay_socket_effects[key]
                    for key in sorted(s.gameplay_socket_effects)
                )
            ),
            talent_categories=state.talent_categories,
            talent_category_points=state.talent_category_points,
            unlocked_skill_presets=state.unlocked_skill_presets,
            prop_info=world.crafting_props,
            notifications=notifications,
            activity_signin_calendar=ActivitySigninCalendar(
                tuple(activity_days[-32:])
            ),
            membership_update=membership_update_from_expiry(
                state.membership_expires_at,
                membership_type=state.membership_type,
                active=state.membership_active,
            ),
            event_update=event_update,
            event_progress_info=event_progress_info,
            event_participation_info=event_participation_info,
            season_pass_info=SeasonPassRewardsInfoResult(
                int(season_pass_state["level"]),
                int(season_pass_state["experience"]),
                tuple(season_pass_state["pass_types"]),
                tuple(season_pass_state["claimed_awards"]),
            ),
            # Persisted incubations (services/seasonal_activities.py); also
            # records a 2026 recall returner from the previous save stamp.
            hatch_eggs_info=CharacterService._seasonal_initialization(
                self, s, persisted_state,
            ),
        ))
        # Restore obstacle collision before PlayerReady, alongside the quest
        # EventUpdate. Scene visibility alone does not open physical gates.
        obstacles = CharacterService._quest_obstacle_update(
            self, s, state.gameplay_map,
        )
        if obstacles.obstacles:
            commands = [
                ServerCommand(OBSTACLE_UPDATE_COMMAND, obstacles, LOCAL_PLAYER_ACTOR_ID)
                if command.command_id == OBSTACLE_UPDATE_COMMAND else command
                for command in commands
            ]
        with s.flow_lock:
            spender_state = s.gameplay_daily_challenges
            spender_progress = int(
                getattr(spender_state, "spender_progress", 0)
            )
        if spender_progress:
            timed_index = next(
                (index for index, command in enumerate(commands)
                 if command.command_id == TIMED_CHALLENGES_INFO_COMMAND),
                None,
            )
            if timed_index is not None:
                commands.insert(timed_index + 1, ServerCommand(
                    UPDATE_PROGRESS_TIER_SPENDER_CHALLENGES_COMMAND,
                    UpdateProgressTierSpenderChallenges(spender_progress),
                    0,
                ))
        quick_slots_snapshot = next(
            (
                command.value
                for command in commands
                if command.command_id == QUICK_SLOTS_INFO_COMMAND
            ),
            None,
        )
        if not isinstance(quick_slots_snapshot, QuickSlotsInfo):
            raise ValueError(
                "local initialization did not construct QuickSlotsInfo"
            )
        with s.flow_lock:
            s.gameplay_quick_slots = copy.deepcopy(quick_slots_snapshot)
            # Ignore the client's stale empty mount-slot echo while its map
            # UI is still loading. The first real user edit can clear it once
            # this short handoff window expires.
            s.gameplay_quick_slots_init_echo_until = time.monotonic() + 15.0
        return local_client_player_init_commands(commands)

    def _send_initialization_commands(
            self, s: Session, commands: tuple) -> bytes:
        """Encode and split the typed 0x85 initialization batch."""

        # Register map NPC actors in their own reliable-ordered frame before
        # the 0x85 batch creates them. Runtime and quest NPC spawns use this
        # same 0x84 -> NewNPC ordering; embedding ActorsEnterVicinity inside
        # player initialization can leave scene NPCs visible but unselectable.
        npc_actor_ids = tuple(
            int(command.value.actor_id)
            for command in commands
            if command.command_id == NEW_NPC_COMMAND
        )
        if npc_actor_ids:
            packet, _packet_bits = encode_actors_enter_vicinity_batch(
                ActorsEnterVicinityBatch(
                    ActorsEnterVicinity(npc_actor_ids),
                    actor_id=0,
                    commands=(),
                )
            )
            s.send_reliable_ordered(
                packet,
                track=True,
                label="local-initialization-npc-actor-vicinity",
            )
            commands = tuple(
                command for command in commands
                if command.command_id != ACTORS_ENTER_VICINITY_COMMAND
            )
        initialization, initialization_bits = encode_player_init_prefix(
            commands, include_terminator=True
        )
        s.send_reliable_ordered_split(
            initialization,
            total_bit_length=initialization_bits,
            track=True,
            label_prefix='local-player-initialization-split',
        )
        return initialization

    def _send_initialization_followups(
            self, s: Session, state: PlayerInitializationState) -> None:
        """Send the snapshots this client does not request after a handoff."""

        # The initial StickerbookInfo precedes PlayerReady. On a cold login,
        # the native RebuildStickerbookUI handler (0x1401a17ce) ignores that
        # event while the collection widget is absent; its later Load builds
        # the default catalog. Publish the owned entries again after readiness
        # using the same UI refresh operation as a favorite change.
        with s.flow_lock:
            stickerbook_entries = tuple(
                s.gameplay_stickerbook_entries[key]
                for key in sorted(s.gameplay_stickerbook_entries)
            )
            quick_slots = copy.deepcopy(s.gameplay_quick_slots)
        # InventoryInfo arrives inside the split PlayerReady initialization
        # while the client is rebuilding its inventory widgets. Reapply the
        # complete allocation map after readiness so equipment-only entries
        # such as the two essence slots are rendered from the live GameItems.
        # Keep ItemInfo out of this refresh: the client already owns those
        # objects, and re-adding them can rebuild neighboring equipment twice.
        followup_commands = [ServerCommand(
            INVENTORY_INFO_COMMAND,
            equipment_allocation_snapshot(state.inventory),
            LOCAL_PLAYER_ACTOR_ID,
        )]
        if isinstance(quick_slots, QuickSlotsInfo):
            # The client can discard the quickbar snapshot sent inside the
            # initial map batch while it is rebuilding its UI. Reapply the
            # server's persisted bars after PlayerReady so shortcuts (notably
            # the mount bound to O) survive map handoffs.
            followup_commands.append(ServerCommand(
                QUICK_SLOTS_INFO_COMMAND,
                quick_slots,
                LOCAL_PLAYER_ACTOR_ID,
            ))
        followup_commands.append(ServerCommand(
            STICKERBOOK_INFO_COMMAND,
            StickerbookInfo(2, stickerbook_entries),
            LOCAL_PLAYER_ACTOR_ID,
        ))
        CharacterService._send_stickerbook_commands(
            s,
            tuple(followup_commands),
            label="restore-player-ui-state",
        )
        # Persisted egg incubations (services/seasonal_activities.py); the
        # init batch omits UserHatchEggsInfo for this client build.
        CharacterService._seasonal_send_initial_hatch_info(self, s)

        # Global PvP mode belongs to the character, not the socket.  The
        # value was already restored from AccountState, but without this
        # response the freshly loaded client UI silently reset to disabled.
        if s.gameplay_pvp_enabled:
            pvp_packet = encode_pvp_flag_response(
                PVPFlag(
                    True,
                    (server_time() // 40) & 0xFFFFFFFF,
                ),
                actor_id=LOCAL_PLAYER_ACTOR_ID,
            )
            s.send_reliable_ordered(
                pvp_packet,
                track=True,
                label="restore-pvp-flag",
            )

        # Ensure the client's GamePlayer +0x5e4 mirrors the restored career honor points.
        honor = int(getattr(s, "gameplay_honor_points", 0) or 0)
        s.send_reliable_ordered(
            encode_honor_points_changed(
                HonorPointsChanged(honor, LOCAL_PLAYER_ACTOR_ID)
            ),
            track=True,
            label="sync-initial-honor-points",
        )

        # Social membership is durable server state. The client does not
        # reliably request GroupStatus after a relog/map handoff, so without
        # these snapshots it can look groupless while the server correctly
        # rejects invitations as already grouped.
        character_id = getattr(state.character, "character_id", None)
        social_snapshots = []
        if (
            character_id is not None
            and self.social_store.guild_for(character_id) is not None
        ):
            social_snapshots.append(
                (
                    encode_guild_status(
                        self._guild_status_for(character_id)
                    ),
                    "initial-guild-status",
                )
            )
        elif (
            character_id is not None
            and self.social_store.ignored_for(character_id)
        ):
            # The guild snapshot above already carries IgnoredStatus; a
            # guildless character with ignores gets the list on its own.
            social_snapshots.append(
                (
                    CharacterService._ignored_status_packet(self, character_id),
                    "initial-ignored-status",
                )
            )
        if (
            character_id is not None
            and self.social_store.group_for(character_id) is not None
        ):
            social_snapshots.append(
                (
                    encode_group_status(
                        self._group_status_for(character_id)
                    ),
                    "initial-group-status",
                )
            )
        for social_packet, social_label in social_snapshots:
            s.send_reliable_ordered(
                social_packet,
                track=True,
                label=social_label,
            )
        # A complete TalentSelectionInfo snapshot restores every saved tree,
        # but this older client still initializes the skill-layout selector
        # to layout zero. Re-emit the same branch-3 activation used after a
        # live 0x0119 switch so a nonzero last-active layout remains selected
        # after relogging.
        if state.active_skill_preselection:
            activation, activation_bits = encode_server_command_batch(
                (
                    ServerCommand(
                        TALENT_SELECTION_INFO_COMMAND,
                        TalentSelectionInfo(
                            3,
                            preselection=state.active_skill_preselection,
                            selections=state.active_skill_selections,
                        ),
                        0,
                    ),
                ),
                include_terminator=True,
            )
            if activation_bits != len(activation) * 8:
                raise ValueError(
                    "skill-layout activation must be byte aligned"
                )
            s.send_reliable_ordered(
                activation,
                track=True,
                label="restore-active-skill-talent-preset",
            )

        with s.flow_lock:
            active_mercenary = str(s.gameplay_active_mercenary or "")
            restore_mercenary = bool(
                active_mercenary and s.gameplay_mercenary_restore_pending
            )
            if restore_mercenary and s.gameplay_combat_state is not None:
                s.gameplay_mercenary_restore_pending = False
            else:
                restore_mercenary = False
        if restore_mercenary:
            definition = load_sticker_definition(
                self.static_database_path, active_mercenary
            )
            if definition is not None and definition.kind == "mercenary":
                CharacterService._activate_mercenary(
                    self, s, definition, source="map transition restore"
                )

    @staticmethod
    def _log_initialization_summary(
            s: Session, state: PlayerInitializationState,
            world: PlayerInitializationWorld, commands: tuple,
            initialization: bytes) -> None:
        """Report what the finished initialization batch carried."""

        monster_encounter = world.monster_encounter
        monster_population = (
            "0"
            if monster_encounter is None
            else (
                f"{len(monster_encounter.actor_ids)} "
                f"(fixed={monster_encounter.fixed_monster_count}, "
                f"spawn_area={monster_encounter.spawn_area_monster_count}, "
                f"density_target="
                f"{monster_encounter.spawn_area_target_count})"
            )
        )
        map_access = state.map_access
        timed_challenges = next(
            (command.value for command in commands
             if command.command_id == 0x0083),
            None,
        )
        timed_challenge_counts = (
            "missing"
            if timed_challenges is None
            else (
                f"daily={len(timed_challenges.daily)}, "
                f"bonus={len(timed_challenges.weekly)}, "
                f"spender={len(timed_challenges.monthly)}"
            )
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] sent complete typed 0x85 player "
            f"initialization ({len(commands)} client-supported commands, "
            f"{len(initialization)} bytes, timed-challenges: "
            f"{timed_challenge_counts}), including InventoryInfo, "
            f"QuickSlots, QuickbarInfo, map entities and PlayerReady "
            f"for actor {LOCAL_PLAYER_ACTOR_ID} "
            f"(template row {state.template_row_index}, "
            f"spawn={state.spawn_position}, "
            f"unlocked_maps={len(state.unlocked_travel_maps)}, "
            f"stickerbook={len(s.gameplay_stickerbook_entries)}, "
            f"difficulty={map_access.map_difficulty}/"
            f"{map_access.scaling_difficulty}, "
            f"monster_level={map_access.monster_level}, "
            f"monsters={monster_population})"
        )
