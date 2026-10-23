"""Logout and authoritative map-transition service.

``LogoutCommand`` (0x0063) is the client's switch-character / logout
request.  In a hub (ruleset 5) it is granted at once.  Anywhere else the
switch happens after a countdown of :data:`LOGOUT_COUNTDOWN_SECONDS` that
any hit on the player cancels (monster attacks and PvP hits both route
through ``_cancel_pending_logout``).  The client is told about the
countdown with ``PlayerLogoutInfoCommand`` (seconds, 0 on cancel); no
capture shows that command, so its value is an inference from the client's
decoder (one u32).
"""

from __future__ import annotations

import time

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.protocol.gameplay_protocol import encode_player_logout_info
from dso_server.gameplay.map_event_state import map_key
from dso_server.gameplay.map_difficulty_state import map_teleport_access_group_override
from dso_server.services.map_events import _PARALLEL_WORLD_EXIT_BOSSES


LOGOUT_COUNTDOWN_SECONDS = 10.0

# This regional route can report the client's current f4102 map as ExitURL
# while the selected MapId or server route points at the hub.
_LEGACY_EXIT_URL_REDIRECTS = {
    ("f4102_dun", "f4102_exit_to_f4100"): ("f4100_hub", "f4102_dun"),
}


class MapTransitionService:
    """Validate map exits/travel and coordinate the local server handoff."""

    @staticmethod
    def _is_fractured_planes_floor_id(map_id: str) -> bool:
        map_id = str(map_id).strip().casefold()
        prefix = "h0009_endlesstower_floor_"
        floor_id = map_id.removeprefix(prefix)
        return (
            map_id.startswith(prefix)
            and floor_id.isdecimal()
            and 1 <= int(floor_id) <= 100
        )

    @staticmethod
    def _event_route_satisfies_map_gate(catalog, map_id: str) -> bool:
        """Allow Fractured Planes to use the Endless Tower floor routes.

        The Endless Tower access rows are authored for ``event_endlesstower``.
        Fractured Planes reuses those floor access IDs, including the zero-padded
        IDs sent by the client when entering a selected floor. Permit valid
        floor IDs only while the Fractured Planes event is active.
        """
        map_id = str(map_id).strip().casefold()
        if not MapTransitionService._is_fractured_planes_floor_id(map_id):
            return False
        return "event_fracturedplanes" in catalog.active_template_ids()

    def _handle_logout(self, s: Session, payload: bytes) -> None:
        try:
            request = decode_logout(payload)
        except ValueError as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected malformed "
                f"LogoutCommand: {exc}"
            )
            return

        gameplay_map = str(s.gameplay_map or "")
        try:
            ruleset = (
                self.static_database.gameplay_map_ruleset(gameplay_map)
                if self.static_database is not None
                else 5
                if gameplay_map in {"a0200_kingscity", "b1200_hub"}
                else 1
            )
        except (OSError, ValueError) as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected switch-character "
                f"request: cannot classify map {gameplay_map!r}: {exc}"
            )
            return
        with s.flow_lock:
            if s.gameplay_exit_transition_started:
                return
            pending = getattr(s, "gameplay_logout_timer", None)
        if pending is not None:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] switch-character already counting "
                f"down on {gameplay_map!r}; repeated request ignored"
            )
            return
        if ruleset == 5:
            self._switch_character(s, request, reason="hub")
            return

        # Outside a hub the switch is granted after a countdown that any hit
        # on the player cancels.
        with s.flow_lock:
            generation = int(getattr(s, "gameplay_logout_generation", 0)) + 1
            s.gameplay_logout_generation = generation
            s.gameplay_client_command_state[0x0063] = request
            timer = s.gameplay_deadline_timer(
                LOGOUT_COUNTDOWN_SECONDS,
                self._complete_delayed_logout,
                args=(s, generation, request),
            )
            timer.daemon = True
            s.gameplay_logout_timer = timer
        timer.start()
        CharacterService._accept_gameplay_state_command(s, 0x0063)
        packet, packet_bits = encode_player_logout_info(
            int(LOGOUT_COUNTDOWN_SECONDS)
        )
        s.send_reliable_ordered(
            (packet, packet_bits), track=True, label="logout-countdown",
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] switch-character on {gameplay_map!r} "
            f"(ruleset {ruleset}) in {LOGOUT_COUNTDOWN_SECONDS:.0f} s unless "
            "the player is attacked"
        )

    def _complete_delayed_logout(
            self, s: Session, generation: int, request) -> None:
        with s.flow_lock:
            if (
                int(getattr(s, "gameplay_logout_generation", 0)) != generation
                or getattr(s, "gameplay_logout_timer", None) is None
            ):
                return
            s.gameplay_logout_timer = None
        if not s.connected:
            return
        try:
            current = self.is_session_current(s)
        except Exception:
            current = True
        if not current:
            return
        self._switch_character(s, request, reason="countdown elapsed")

    @staticmethod
    def _cancel_pending_logout(s: Session, reason: str) -> bool:
        """Stop a running switch-character countdown; returns whether one ran."""

        with s.flow_lock:
            timer = getattr(s, "gameplay_logout_timer", None)
            if timer is None:
                return False
            s.gameplay_logout_timer = None
            s.gameplay_logout_generation = (
                int(getattr(s, "gameplay_logout_generation", 0)) + 1
            )
        timer.cancel()
        packet, packet_bits = encode_player_logout_info(0)
        s.send_reliable_ordered(
            (packet, packet_bits), track=True, label="logout-cancelled",
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] switch-character countdown "
            f"cancelled: {reason}"
        )
        return True

    def _switch_character(self, s: Session, request, *, reason: str) -> None:
        with s.flow_lock:
            if s.gameplay_exit_transition_started:
                return
            s.gameplay_exit_transition_started = True
            s.gameplay_logout_timer = None
            s.gameplay_client_command_state[0x0063] = request
        CharacterService._accept_gameplay_state_command(s, 0x0063)
        CharacterService._quest_leave_map(self, s)
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(s, "disconnect")
        with s.gameplay_inventory_action_lock:
            CharacterService._persist_gameplay_session(
                self,
                s,
                reason="switch-character source",
                flush=True,
            )

        gameplay_router = getattr(self, "gameplay_router", None)
        login_port = getattr(gameplay_router, "port", self.port)
        login_address = f"{self.advertised_host}:{login_port}"
        transition = GameplaySwitchMapPacket(
            login_address,
            enter_character_selection=False,
        ).encode()
        s.send_reliable_ordered(
            transition,
            track=True,
            label="switch-character",
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] accepted LogoutCommand "
            f"({request!r}; {reason}); returning through {login_address} to "
            "character selection"
        )

    def _begin_map_transfer(
            self, s: Session, destination_map_id: str, *,
            spawn_position=None, reason: str,
            gameplay_switch: bool = False,
            scaling_difficulty: int = 0,
            fractured_planes_run: bool = False) -> bool:
        character = s.gameplay_character
        event_catalog = CharacterService._event_catalog(self)
        in_fractured_planes_run = bool(
            getattr(s, "gameplay_fractured_planes_run", False)
            or fractured_planes_run
        ) and CharacterService._is_fractured_planes_floor_id(destination_map_id)
        if (
            reason not in {"admin-chat", "admin-cms"}
            and not event_catalog.map_available(destination_map_id)
            and not (
                (reason == "event-travel"
                 and CharacterService._event_route_satisfies_map_gate(
                     event_catalog, destination_map_id,
                 ))
                or in_fractured_planes_run
            )
        ):
            raise ValueError(
                f"map {destination_map_id!r} requires an active event"
            )
        player_level = max(1, int(getattr(character, "level", 1)))
        map_access = resolve_map_access(
            self.static_database_path,
            destination_map_id,
            player_level=player_level,
            scaling_difficulty=scaling_difficulty,
        )
        physical_map_id = map_access.physical_map_id
        destination_path = map_database_path(
            self.static_database_path, physical_map_id
        )
        if not destination_path.is_file():
            raise ValueError(
                "map-transfer destination has no local map database: "
                f"{physical_map_id}"
            )
        # Every travel request is an entry, including another difficulty of
        # the same physical map. Saved positions are reserved for reconnects.
        if spawn_position is None:
            spawn_position = load_local_spawn_position(self.static_database_path, physical_map_id)
        gameplay_router = getattr(self, 'gameplay_router', None)
        # Both travel paths return to the login service.  Retail's actorless
        # gameplay batch leaves the address empty because its configured
        # primary endpoint is UDP/2190.  Our client is configured with the
        # auth gateway on UDP/80, so leaving it empty incorrectly inserts an
        # auth connection and loses the in-game loading/UI state.  Keep the
        # captured 0x85 transport for an exit, but serialize the logically
        # equivalent local UDP/2190 target explicitly.
        login_port = getattr(gameplay_router, 'port', 2190)
        login_address = f'{self.advertised_host}:{login_port}'
        transition = (
            GameplaySwitchMapPacket(login_address).encode()
            if gameplay_switch
            else switch_map_payload(
                login_address,
                enter_character_selection=False,
            )
        )
        with s.flow_lock:
            if (
                s.gameplay_exit_transition_started
            ):
                return False
            s.gameplay_exit_transition_started = True
            mount_ride = copy.deepcopy(s.gameplay_mount_ride)
            # The old RakNet connection has to remain alive long enough to
            # deliver SwitchMap, but it no longer owns any source-map work.
            # Cancel its timers now instead of waiting for the destination
            # connection or a UDP reset to retire it.
            s.gameplay_chase_generation += 1
            chase_timer = s.gameplay_chase_timer
            s.gameplay_chase_timer = None
            projectile_timers = tuple(
                s.gameplay_projectile_timers.values()
            )
            s.gameplay_projectile_timers.clear()
        CharacterService._quest_leave_map(self, s, physical_map_id)
        if chase_timer is not None:
            chase_timer.cancel()
        for projectile_timer in projectile_timers:
            projectile_timer.cancel()
        if s.gameplay_presence_callback is not None:
            # Retail removes the actor from the source instance before the
            # redirected gameplay connection announces it in the destination.
            # Waiting for the old UDP socket to disconnect leaves a stale
            # same-character session which breaks the later re-entry snapshot.
            s.gameplay_presence_callback(s, 'disconnect')
        with s.gameplay_inventory_action_lock:
            CharacterService._persist_gameplay_session(
                self, s, reason=f'{reason} source', flush=True
            )
        # Build the one-shot route from the state after the durable source
        # snapshot. A QuickSlotsCommand already in flight can be applied after
        # the transition flag is set; taking the route copy before the flush
        # lets that older copy override the newer persisted shortcuts on the
        # destination map.
        with s.flow_lock:
            quick_slots = copy.deepcopy(s.gameplay_quick_slots)
        account_store = getattr(self, 'account_store', None)
        character_id = getattr(character, 'character_id', None)
        if account_store is not None and character_id is not None:
            persisted_state = account_store.get(character_id)
            persisted_quick_slots = getattr(
                persisted_state, 'quick_slots', None
            )
            if persisted_quick_slots is not None:
                if quick_slots is None:
                    quick_slots = copy.deepcopy(persisted_quick_slots)
                else:
                    quick_slots = CharacterService._merge_quick_slots(
                        quick_slots, persisted_quick_slots
                    )
        route_character = copy.deepcopy(character)
        if route_character is not None:
            route_character.map_id = physical_map_id
            # Carry the latest client-assigned shortcuts on the one-shot map
            # route so initialization cannot reuse a stale account snapshot.
            route_character.route_quick_slots = quick_slots
            # This route-only marker distinguishes Fractured Planes from the
            # Endless Tower floors whose physical maps it reuses.
            route_character.fractured_planes_run = bool(fractured_planes_run)
            fractured_deadline = None
            runtime = getattr(s, "gameplay_map_event_runtime", None)
            if fractured_planes_run:
                if reason == "event-travel":
                    fractured_deadline = time.perf_counter() + 8 * 60
                elif runtime is not None:
                    fractured_deadline = runtime.fractured_planes_limit_deadline
            route_character.fractured_planes_deadline = fractured_deadline
            # CharacterRecord intentionally remains a compact wire model.
            # Route-only difficulty metadata is attached to its private copy
            # and consumed by PlayerInitializationService on reconnect.
            route_character.map_access_id = map_access.request_map_id
            route_character.map_difficulty = map_access.map_difficulty
            route_character.scaling_difficulty = (
                map_access.scaling_difficulty
            )
        self.arm_gameplay(
            s.addr[0],
            physical_map_id,
            route_character,
            s.gameplay_template_row_index,
            mount_ride,
            spawn_position,
        )
        if gameplay_router is not None:
            arm_login_handoff = getattr(
                gameplay_router,
                'arm_gameplay_handoff',
                gameplay_router.arm_gameplay,
            )
            arm_login_handoff(
                s.addr[0],
                physical_map_id,
                route_character,
                s.gameplay_template_row_index,
                mount_ride,
                spawn_position,
            )
        s.send_reliable_ordered(
            transition,
            track=True,
            label=f'{reason}-switch-map',
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] sent {reason} SwitchMapCommand "
            f"to {s.addr}: {s.gameplay_map} -> {map_access.request_map_id} "
            f"via {login_address}; message=0x{transition[0][0]:02x}, "
            f"bits={transition[1]}, spawn={spawn_position}; awaiting reconnect"
        )
        return True

    def _activate_map_exit(
            self, s: Session, definition, *, trigger: str,
            destination_access_id: str | None = None) -> bool:
        try:
            character = s.gameplay_character
            selected_access_id = str(
                destination_access_id or definition.destination_map_id
            )
            destination_access = resolve_map_access(
                self.static_database_path,
                selected_access_id,
                player_level=max(
                    1,
                    int(getattr(character, 'level', 1)),
                ),
            )
            destination_physical_map_id = (
                destination_access.physical_map_id
            )
            continuing_fractured_planes = bool(
                getattr(s, "gameplay_fractured_planes_run", False)
                and CharacterService._is_fractured_planes_floor_id(
                    destination_access.request_map_id
                )
            )
            if (
                not continuing_fractured_planes
                and not CharacterService._map_quest_accessible(
                    self, s, destination_access.request_map_id
                )
            ):
                raise ValueError("the destination requires its unlocking quest")
            try:
                destination_position = load_local_entry_position(
                    self.static_database_path,
                    destination_physical_map_id,
                    definition.destination_entry_name,
                )
            except ValueError as exc:
                destination_position = load_local_spawn_position(
                    self.static_database_path,
                    destination_physical_map_id,
                )
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] map exit "
                    f"{definition.name!r} has no matching destination entry; "
                    f"using respawn point ({exc})"
                )
            started = CharacterService._begin_map_transfer(
                self,
                s,
                selected_access_id,
                spawn_position=destination_position,
                reason='map-exit',
                gameplay_switch=True,
                fractured_planes_run=(
                    continuing_fractured_planes
                ),
            )
        except (OSError, ValueError) as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected map exit "
                f"{definition.name!r} ({trigger}): {exc}"
            )
            return False
        if not started:
            return False
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] activated map exit via {trigger}: "
            f"{definition.name!r}: {s.gameplay_map} -> "
            f"{selected_access_id} at entry "
            f"{definition.destination_entry_name!r}; sent actorless typed "
            f"SwitchMapCommand to {self.advertised_host}:"
            f"{getattr(getattr(self, 'gameplay_router', None), 'port', 2190)}"
        )
        return True

    @staticmethod
    def _is_fractured_planes_rift(definition) -> bool:
        return (
            str(getattr(definition, "exit_id", "")).casefold().startswith(
                "fracturedplanes_rift_entrance"
            )
            and int(getattr(definition, "event_set_row", -1)) == 1841
        )

    def _handle_map_exit(self, s: Session, payload: bytes) -> None:
        try:
            request = decode_unlock_map_request(payload)
            gameplay_map = str(s.gameplay_map or "")
            with s.flow_lock:
                s.gameplay_client_command_state[0x0060] = request
                candidates = tuple(
                    definition
                    for definition in s.gameplay_map_exits
                    if (
                        definition.exit_id == request.exit_id
                        or definition.name == request.exit_id
                    )
                )
                last_move = s.gameplay_last_move
                coordinate_frame = s.gameplay_coordinate_frame
                world_position = s.gameplay_spawn_position
            if not candidates:
                authored_exits = load_local_map_exits(
                    self.static_database_path,
                    gameplay_map,
                    include_inactive_event_sets=True,
                )
                matching_authored = tuple(
                    d for d in authored_exits
                    if (
                        d.exit_id == request.exit_id
                        or d.name == request.exit_id
                    )
                )
                if matching_authored:
                    clicked_authored = matching_authored[0]
                    player_level = max(
                        1,
                        int(getattr(s.gameplay_character, 'level', 1)),
                    )
                    try:
                        clicked_dest = resolve_map_access(
                            self.static_database_path,
                            clicked_authored.destination_map_id,
                            player_level=player_level,
                        )
                    except ValueError:
                        clicked_dest = None
                    if clicked_dest is not None:
                        for active_exit in s.gameplay_map_exits:
                            if (
                                math.hypot(
                                    active_exit.position[0] - clicked_authored.position[0],
                                    active_exit.position[2] - clicked_authored.position[2],
                                ) <= 2.0
                                or (
                                    clicked_authored.event_set_row >= 0
                                    and active_exit.event_set_row == clicked_authored.event_set_row
                                )
                            ):
                                try:
                                    active_dest = resolve_map_access(
                                        self.static_database_path,
                                        active_exit.destination_map_id,
                                        player_level=player_level,
                                    )
                                except ValueError:
                                    continue
                                if (
                                    active_dest.physical_map_id.casefold()
                                    == clicked_dest.physical_map_id.casefold()
                                    or (
                                        gameplay_map.casefold() == "f4202_dun"
                                        and clicked_dest.access_group_id.casefold()
                                        == map_teleport_access_group_override(
                                            self.static_database_path,
                                            active_dest.physical_map_id,
                                        ).casefold()
                                    )
                                ):
                                    candidates = (active_exit,)
                                    break
                    # These regional routes are authored as inactive locked
                    # exits, but the client still sends UnlockMap for the
                    # clicked scene entity. The server should accept that
                    # request with its matching access key, then remember the
                    # destination after entry so later trips no longer need it.
                    keyed_routes = {
                        ("f4101_pve", "f4100_hub"): "key_from_f4101_to_f4100",
                        ("f4102_dun", "f4201_pve"): "key_from_f4102_to_f4201",
                        ("f4201_pve", "f4202_dun"): "key_from_f4201_to_f4202",
                    }
                    required_key = keyed_routes.get((
                        gameplay_map.casefold(),
                        clicked_dest.physical_map_id.casefold()
                        if clicked_dest is not None else "",
                    ))
                    if (
                        not candidates
                        and clicked_authored.exit_id.casefold() == "exit_key_lock"
                        and clicked_dest is not None
                        and required_key is not None
                    ):
                        known_maps = {
                            map_key(value)
                            for value in getattr(s, "gameplay_unlocked_maps", ())
                        }
                        already_discovered = bool(known_maps.intersection({
                            map_key(clicked_dest.request_map_id),
                            map_key(clicked_dest.access_group_id),
                            map_key(clicked_dest.physical_map_id),
                        }))
                        with s.flow_lock:
                            inventory = s.gameplay_inventory
                            has_access_key = bool(
                                inventory is not None
                                and any(
                                    str(item.template_id).casefold() == required_key
                                    for item in inventory.items
                                )
                            )
                        if has_access_key or already_discovered:
                            candidates = (clicked_authored,)
            if not candidates:
                raise ValueError(
                    f"unknown or unavailable exit {request.exit_id!r}"
                )
            if last_move is not None and coordinate_frame is not None:
                world_position = coordinate_frame.wire_to_world(
                    last_move.relative_position
                )
            if len(candidates) == 1:
                definition = candidates[0]
            elif world_position is not None:
                definition = min(
                    candidates,
                    key=lambda candidate: math.hypot(
                        world_position[0] - candidate.position[0],
                        world_position[2] - candidate.position[2],
                    ),
                )
            else:
                raise ValueError(
                    f"exit id {request.exit_id!r} is ambiguous without a "
                    "player position"
                )
            # UnlockMap serializes the selected MapAccess config first and the
            # clicked ExitProperty's authored URL second.  For a higher PW
            # tier those values intentionally differ (for example _02/_01).
            # Shared/template exits are the other captured representation and
            # put the active map in both fields; they select the authored
            # destination without an explicit alternate tier.
            authored_exit_url = str(definition.destination_map_id)
            selected_map_id = str(request.map_id)
            request_exit_url = str(request.exit_url)
            redirect = _LEGACY_EXIT_URL_REDIRECTS.get((
                gameplay_map.casefold(), definition.name.casefold(),
            ))
            redirected_destination = (
                redirect[0]
                if (
                    redirect is not None
                    and authored_exit_url.casefold() == redirect[0].casefold()
                    and request_exit_url.casefold() == redirect[1].casefold()
                    and selected_map_id.casefold() in {
                        redirect[0].casefold(), redirect[1].casefold(),
                    }
                )
                else None
            )
            current_map_representation = (
                selected_map_id == gameplay_map
                and request_exit_url == gameplay_map
            )
            if redirected_destination is not None:
                destination_access_id = redirected_destination
            elif (
                self._is_fractured_planes_rift(definition)
                and CharacterService._is_fractured_planes_run(self, s)
            ):
                runtime = getattr(s, "gameplay_map_event_runtime", None)
                if runtime is None or not runtime.fractured_planes_floor_completed:
                    raise ValueError("the Rift Lord has not been defeated")
                # The native portal menu chooses a concrete floor. Its
                # authored ExitURL remains floor one for every floor choice.
                if not CharacterService._is_fractured_planes_floor_id(selected_map_id):
                    raise ValueError("the rift requires a selected Tower floor")
                selected_floor = int(selected_map_id.rsplit("_", 1)[1])
                cleared, _claimed = CharacterService._fractured_planes_progress(s)
                if not 1 <= selected_floor <= min(100, cleared + 1):
                    raise ValueError("the selected Tower floor is still locked")
                with s.flow_lock:
                    s.gameplay_fractured_planes_run = True
                destination_access_id = selected_map_id
            elif current_map_representation:
                destination_access_id = authored_exit_url
            else:
                player_level = max(
                    1,
                    int(getattr(s.gameplay_character, 'level', 1)),
                )
                authored_access = resolve_map_access(
                    self.static_database_path,
                    authored_exit_url,
                    player_level=player_level,
                )
                teleport_access_group_override = (
                    map_teleport_access_group_override(
                        self.static_database_path,
                        authored_access.physical_map_id,
                    ).casefold()
                )
                if request_exit_url != authored_exit_url:
                    try:
                        request_access = resolve_map_access(
                            self.static_database_path,
                            request_exit_url,
                            player_level=player_level,
                        )
                    except ValueError:
                        request_access = None
                    request_access_matches = bool(
                        request_access is not None
                        and (
                            (
                                request_access.physical_map_id.casefold()
                                == authored_access.physical_map_id.casefold()
                                and request_access.access_group_id.casefold()
                                == authored_access.access_group_id.casefold()
                            )
                            or (
                                teleport_access_group_override
                                and request_access.access_group_id.casefold()
                                == teleport_access_group_override
                            )
                        )
                    )
                    if not request_access_matches:
                        raise ValueError(
                            f"request ExitURL {request_exit_url!r} does not match "
                            f"authored exit destination {authored_exit_url!r}"
                        )
                selected_access = resolve_map_access(
                    self.static_database_path,
                    selected_map_id,
                    player_level=player_level,
                )
                if selected_access.request_map_id != selected_map_id:
                    raise ValueError(
                        f"selected MapId {selected_map_id!r} is not a "
                        "concrete map access"
                    )
                selected_access_matches = (
                    selected_access.access_group_id.casefold()
                    == authored_access.access_group_id.casefold()
                    and selected_access.physical_map_id.casefold()
                    == authored_access.physical_map_id.casefold()
                ) or bool(
                    teleport_access_group_override
                    and selected_access.access_group_id.casefold()
                    == teleport_access_group_override
                )
                if not selected_access_matches:
                    raise ValueError(
                        f"selected MapId {selected_map_id!r} is not a "
                        f"tier of exit destination "
                        f"{authored_exit_url!r}"
                    )
                if (
                    request.exit_id != definition.exit_id
                    and map_key(gameplay_map) in _PARALLEL_WORLD_EXIT_BOSSES
                ):
                    destination_access_id = authored_exit_url
                else:
                    destination_access_id = selected_access.request_map_id
            CharacterService._accept_gameplay_state_command(s, 0x0060)
        except ValueError as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected UnlockMap/map-exit "
                f"request: {exc}"
            )
            return
        CharacterService._activate_map_exit(
            self,
            s,
            definition,
            trigger='client cmd 0x0060',
            destination_access_id=destination_access_id,
        )

    def _handle_map_travel(self, s: Session, payload: bytes) -> None:
        try:
            request = decode_map_travel_request(
                payload, getattr(s, "current_message_bit_length", None))
        except ValueError as exc:
            # A body the codec cannot identify must not abort the datagram's
            # remaining messages; log the raw bytes so the form can be added.
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected malformed "
                f"TravelCommand: {exc}"
            )
            return
        if request.travel_point_id:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] travel object "
                f"{request.travel_point_id!r} (mode {request.mode}) "
                f"selected {request.map_id!r} entry {request.entry_name or request.entry_index!r}"
            )
        CharacterService._perform_map_travel(
            self,
            s,
            request,
            command_id=0x0061,
        )

    def _handle_use_travel_item(
            self, s: Session, payload: bytes) -> None:
        request = decode_use_travel_item_request(payload)
        CharacterService._perform_map_travel(
            self,
            s,
            request,
            command_id=USE_TRAVEL_ITEM_COMMAND,
            required_item_id=request.item_id,
        )

    def _resolve_hub_travel_destination(self, s: Session) -> str:
        """Turn the client's symbolic ``Hub`` into a real, local hub map.

        Preference order: the hub the character last stood in (tracked for
        the respawn-to-town flow), then the nearest hub reachable from the
        current map through authored exits, then Kingshill.  A candidate
        must be a ruleset-5 hub with a local map database.
        """

        with s.flow_lock:
            last_town = str(getattr(s, "gameplay_last_visited_town", "") or "")
            current_map = str(s.gameplay_map or "")
        candidates = [last_town]
        if current_map:
            try:
                candidates.append(self._nearest_login_hub(current_map))
            except Exception:
                pass
        candidates.append(DEFAULT_GAMEPLAY_MAP)
        for candidate in candidates:
            if not candidate:
                continue
            try:
                is_hub = self.static_database.gameplay_map_ruleset(candidate) == 5
            except Exception:
                continue
            if is_hub and map_database_path(
                self.static_database_path, candidate
            ).is_file():
                return candidate
        return DEFAULT_GAMEPLAY_MAP

    def _perform_event_travel(self, s: Session, request, *, command_id: int) -> None:
        """Authorize an event preview's map/entry pair against the schedule.

        The command has no event id. Only a currently eligible event's exact
        authored destination can authorize this shortcut past map discovery.
        """
        catalog = CharacterService._event_catalog(self)
        player_level = max(1, int(getattr(s.gameplay_character, "level", 1)))
        requested = (str(request.map_id).casefold(), str(request.entry_name or "").casefold())
        destination = None
        fractured_planes_run = False
        for event in catalog.active_events():
            template = event.template
            if (template.client_only or template.category.casefold() == "silent"
                    or not template.minimum_level <= player_level <= template.maximum_level):
                continue
            # Fractured Planes reuses the Endless Tower floor access IDs. The
            # client asks for a selected floor even though the event template
            # has no TeleportTarget of its own.
            if (
                template.resource_id.casefold() == "event_fracturedplanes"
                and requested[1] == "entrypoint"
                and CharacterService._event_route_satisfies_map_gate(
                    catalog, requested[0],
                )
            ):
                destination = (request.map_id, "Entrypoint")
                fractured_planes_run = True
                break
            target_map, _, target_entry = template.teleport_target.strip().partition(":")
            target_map, target_entry = target_map.strip(), target_entry.strip()
            if not target_map:
                # Empty event targets can arrive empty or with the fallback
                # already filled in. Neither permits an arbitrary destination.
                if requested in {("", ""), (DEFAULT_GAMEPLAY_MAP, ""),
                                 (DEFAULT_GAMEPLAY_MAP, "respawnpoint")}:
                    destination = (DEFAULT_GAMEPLAY_MAP, "")
                    break
            elif requested == (target_map.casefold(), target_entry.casefold()):
                destination = (target_map, target_entry)
                break
        selected_floor = None
        if requested[0].startswith("h0009_endlesstower_floor_"):
            suffix = requested[0].removeprefix("h0009_endlesstower_floor_")
            if suffix.isdecimal():
                selected_floor = int(suffix)
        if (
            destination is None
            and requested[1] == "entrypoint"
            and selected_floor is not None
        ):
            cleared, _claimed = CharacterService._fractured_planes_progress(s)
            if 1 <= selected_floor <= cleared:
                # Completed floors remain replayable from Kingshill after the
                # run marker and the temporary event route have expired.
                destination = (request.map_id, "Entrypoint")
                fractured_planes_run = True
        if destination is None:
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected event travel: no eligible active event "
                  f"targets {request.map_id!r}:{request.entry_name or ''}")
            return
        map_id, entry_name = destination
        try:
            cleared_floor, _claimed_floor = CharacterService._fractured_planes_progress(s)
            replaying_cleared_floor = (
                fractured_planes_run
                and selected_floor is not None
                and selected_floor <= cleared_floor
            )
            if (
                not catalog.map_available(map_id)
                and not CharacterService._event_route_satisfies_map_gate(
                    catalog, map_id,
                )
                and not replaying_cleared_floor
            ):
                raise ValueError("the destination requires an active event")
            if (
                not replaying_cleared_floor
                and not CharacterService._map_quest_accessible(self, s, map_id)
            ):
                raise ValueError("the destination requires its unlocking quest")
            access = resolve_map_access(self.static_database_path, map_id, player_level=player_level)
            try:
                spawn_position = load_local_entry_position(
                    self.static_database_path, access.physical_map_id, entry_name)
            except ValueError:
                # Some older event templates name an entry missing from the
                # installed map. Use that destination's authored default spawn.
                spawn_position = load_local_spawn_position(
                    self.static_database_path, access.physical_map_id)
            with s.flow_lock:
                s.gameplay_client_command_state[command_id] = request
            CharacterService._accept_gameplay_state_command(s, command_id)
            CharacterService._begin_map_transfer(
                self, s, map_id, spawn_position=spawn_position,
                reason="event-travel", scaling_difficulty=0,
                fractured_planes_run=fractured_planes_run)
        except (OSError, ValueError) as exc:
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected event travel: {exc}")

    def _perform_map_travel(
            self, s: Session, request, *, command_id: int,
            required_item_id: int | None = None) -> None:
        """Validate, consume, and transfer either travel command variant."""

        if (getattr(request, "entry_name", None) is not None
                and not getattr(request, "travel_point_id", "")
                and required_item_id is None):
            CharacterService._perform_event_travel(self, s, request, command_id=command_id)
            return

        difficulty = max(0, int(getattr(request, "difficulty", 0)))
        map_id = str(request.map_id)
        spawn_position = None
        if map_id.casefold() == "hub":
            # The "teleport to the last visited city hub" talent names its
            # destination symbolically.  Resolve it to a real hub and arrive
            # at that hub's travel stone.
            map_id = CharacterService._resolve_hub_travel_destination(
                self, s
            )
            spawn_position = load_local_travel_point_position(
                self.static_database_path, map_id
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] resolved travel destination "
                f"'Hub' to {map_id!r}"
            )
        catalog = CharacterService._event_catalog(self)
        with s.flow_lock:
            s.gameplay_client_command_state[command_id] = request
            pending = copy.deepcopy(s.gameplay_pending_travel_item)
            character = copy.deepcopy(s.gameplay_character)
            original_inventory = copy.deepcopy(s.gameplay_inventory)
            original_next_item_id = int(s.gameplay_next_item_id)
        if not catalog.map_available(map_id):
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected event-locked map "
                f"travel to {map_id!r}"
            )
            return

        if not CharacterService._map_quest_accessible(self, s, map_id):
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected quest-locked map travel to {map_id!r}")
            return
        consumed_travel_item = False
        if pending is not None:
            if float(pending.get("expires_at", 0.0)) < time.perf_counter():
                with s.flow_lock:
                    if s.gameplay_pending_travel_item == pending:
                        s.gameplay_pending_travel_item = None
                pending = None
        if required_item_id is not None and pending is None:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected UseTravelItem "
                f"for {map_id!r}: no armed inventory item"
            )
            return
        if pending is not None:
            if (
                required_item_id is not None
                and int(pending["item_id"]) != int(required_item_id)
            ):
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] rejected UseTravelItem "
                    f"item mismatch: armed {pending['item_id']}, got "
                    f"{required_item_id}"
                )
                return
            try:
                player_level = max(
                    1, int(getattr(character, "level", 1))
                )
                expected_access = resolve_map_access(
                    self.static_database_path,
                    str(pending["target_map_id"]),
                    player_level=player_level,
                    scaling_difficulty=difficulty,
                )
                requested_access = resolve_map_access(
                    self.static_database_path,
                    map_id,
                    player_level=player_level,
                    scaling_difficulty=difficulty,
                )
                if (
                    expected_access.access_group_id.casefold()
                    != requested_access.access_group_id.casefold()
                ):
                    print(
                        f"[{GAMEPLAY_SERVICE_NAME}] rejected TravelItem "
                        f"target mismatch: expected "
                        f"{pending['target_map_id']!r}, got "
                        f"{map_id!r}"
                    )
                    return
                with s.flow_lock:
                    inventory = copy.deepcopy(s.gameplay_inventory)
                    if inventory is None:
                        raise ValueError("TravelItem inventory is unavailable")
                    item = next(
                        (
                            value for value in inventory.items
                            if value.item_id == int(pending["item_id"])
                            and value.template_id
                            == str(pending["template_id"])
                        ),
                        None,
                    )
                    if item is None:
                        raise ValueError("armed TravelItem no longer exists")
                    builder = InventoryMutationBuilder(
                        inventory, s.gameplay_next_item_id
                    )
                    builder.consume(
                        item.item_id, 1, preserve_item_id=True
                    )
                    mutation = builder.finish()
                    s.gameplay_inventory = mutation.inventory
                    s.gameplay_next_item_id = mutation.next_item_id
                    s.gameplay_pending_travel_item = None
                    consumed_travel_item = True
            except (KeyError, OSError, ValueError) as exc:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] rejected TravelItem "
                    f"inventory request: {exc}"
                )
                return
        if (not consumed_travel_item and str(request.map_id).casefold() != "hub"
                and not CharacterService._travel_map_unlocked(self, s, map_id)):
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected locked map travel to {map_id!r}")
            return
        CharacterService._accept_gameplay_state_command(s, command_id)
        try:
            if getattr(request, "entry_name", None):
                access = resolve_map_access(self.static_database_path, map_id,
                    player_level=max(1, int(getattr(character, "level", 1))),
                    scaling_difficulty=difficulty)
                spawn_position = load_local_entry_position(
                    self.static_database_path, access.physical_map_id, request.entry_name)
            elif spawn_position is None:
                access = resolve_map_access(self.static_database_path, map_id,
                    player_level=max(1, int(getattr(character, "level", 1))),
                    scaling_difficulty=difficulty)
                spawn_position = load_local_travel_point_position(
                    self.static_database_path, access.physical_map_id)
            started = CharacterService._begin_map_transfer(
                self,
                s,
                map_id,
                spawn_position=spawn_position,
                reason=(
                    'travel-item'
                    if command_id == USE_TRAVEL_ITEM_COMMAND
                    else 'fast-travel'
                ),
                scaling_difficulty=difficulty,
            )
        except (OSError, ValueError) as exc:
            if consumed_travel_item:
                with s.flow_lock:
                    s.gameplay_inventory = original_inventory
                    s.gameplay_next_item_id = original_next_item_id
                    s.gameplay_pending_travel_item = pending
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected map travel: {exc}")
            return
        if not started:
            if consumed_travel_item:
                with s.flow_lock:
                    s.gameplay_inventory = original_inventory
                    s.gameplay_next_item_id = original_next_item_id
                    s.gameplay_pending_travel_item = pending
            return
        if consumed_travel_item:
            self._season_pass_feed(
                s, "UseDungeonKeys", value=str(pending["template_id"]),
            )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] accepted typed travel request for "
            f"{map_id} (scaling difficulty {difficulty}); "
            "armed the next local gameplay connection and sent "
            f"SwitchMapCommand to {self.advertised_host}:2190"
        )
