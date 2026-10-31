"""Login gateway and armed gameplay handoff service."""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.protocol.prop_protocol import (
    PROP_INTERACTION_COMMAND,
    QUEST_TRIGGER_SIGNAL_COMMAND,
)
from dso_server.runtime.transport import LoginServer, Session
from dso_server.services.character_server import CharacterServer
from dso_server.services.dispatch import DispatchService


class LoginGameServer(DispatchService, LoginServer):
    """Share UDP/2190 between login handoffs and a legacy gameplay route.

    The production hosts use different IPs for these two roles but the same
    port.  Normal local transitions preserve the retail topology: the
    character/current-map service arms a handoff on 2190, 2190 redirects the
    client to the already armed map service on 2192, and only 2192 sends the
    map bootstrap.  ``arm_gameplay`` remains as a direct mode for compatibility
    with explicit legacy routes.
    """

    # The normal route uses CharacterServer/2192. Keep the legacy direct-map
    # route feature-complete by sharing the same stateful gameplay helpers.
    _map_session_candidates = CharacterServer._map_session_candidates
    _active_gameplay_sessions = CharacterServer._active_gameplay_sessions
    _social_actor_id_for = CharacterServer._social_actor_id_for
    _remote_player_for = CharacterServer._remote_player_for
    _remote_actor_id = CharacterServer._remote_actor_id
    _player_summary = CharacterServer._player_summary
    _character_for_session = CharacterServer._character_for_session
    _social_member = CharacterServer._social_member
    _guild_status_for = CharacterServer._guild_status_for
    _group_status_for = CharacterServer._group_status_for
    _group_member_summaries = CharacterServer._group_member_summaries
    _broadcast_social_status = CharacterServer._broadcast_social_status
    _refresh_social_chat_memberships = (
        CharacterServer._refresh_social_chat_memberships
    )
    _deliver_rewards = CharacterServer._deliver_rewards
    _handle_activity_signin = CharacterServer._handle_activity_signin
    _handle_simple_gameplay_command = (
        CharacterServer._handle_simple_gameplay_command
    )
    _generate_world_loot_for_session = (
        CharacterServer._generate_world_loot_for_session
    )

    def __init__(
            self, host: str, char_server_addr: str, port: int = 2190,
            static_database=None,
            account_store: AccountStateStore | None = None,
            social_store: SocialStateStore | None = None):
        super().__init__(host, char_server_addr, port)
        self.account_store = account_store
        self.social_store = social_store or SocialStateStore(
            os.getenv('DSO_SOCIAL_STATE_FILE', str(DEFAULT_SOCIAL_STATE_PATH))
        )
        self.social_chat_callback = None
        self.system_chat_callback = None
        self.gameplay_routes = GameplayRouteTable(
            GAMEPLAY_ROUTE_TTL_SECONDS
        )
        self.gameplay_world_lock = threading.RLock()
        self.gameplay_world_next_item_ids: dict[object, int] = {}
        selected_database = Path(
            static_database
            or os.getenv('DSO_STATIC_DB', DEFAULT_STATIC_DB_PATH)
        )
        self.static_database_path = selected_database
        self.static_database = (
            StaticGameDatabase(selected_database)
            if selected_database.is_file()
            else None
        )
        self.talent_database = (
            TalentDatabase(selected_database)
            if selected_database.is_file()
            else None
        )
        self.item_set_database = (
            ItemSetDatabase(selected_database)
            if selected_database.is_file()
            else None
        )
        self.socket_effect_database = (
            prime_socket_effect_database(selected_database)
            if selected_database.is_file()
            else None
        )

    def _gameplay_map_load_spec(
            self, map_name, character=None) -> tuple[bytes, int]:
        requested_access_id = str(
            getattr(character, 'map_access_id', '') or ''
        )
        scaling_difficulty = int(
            getattr(character, 'scaling_difficulty', 0) or 0
        )
        if not requested_access_id and self.account_store is not None:
            persisted = self.account_store.get(
                int(getattr(character, 'character_id', 0) or 0)
            )
            if persisted is not None and persisted.map_id == map_name:
                requested_access_id = str(
                    persisted.map_access_id or persisted.map_id
                )
                scaling_difficulty = int(persisted.scaling_difficulty)
        if self.static_database is not None:
            map_access = resolve_map_access(
                self.static_database_path,
                requested_access_id or map_name,
                player_level=max(
                    1, int(getattr(character, 'level', 1) or 1)
                ),
                scaling_difficulty=scaling_difficulty,
            )
            if map_access.physical_map_id != map_name:
                raise ValueError(
                    f"map load access {map_access.request_map_id!r} "
                    f"resolves to {map_access.physical_map_id!r}, not "
                    f"{map_name!r}"
                )
            access_id = map_access.request_map_id
            ruleset = (
                0
                if access_id != map_name
                else self.static_database.gameplay_map_ruleset(map_name)
            )
        else:
            ruleset = 5 if map_name in {"a0200_kingscity", "b1200_hub"} else 1
            access_id = requested_access_id or map_name
        # A battleground is announced with its own ruleset; the map template
        # calls the arena ClosedPVE, so the override cannot come from there.
        ruleset = battleground_ruleset(map_name) or ruleset
        return MapLoadPacket(
            map_name,
            ruleset=ruleset,
            access_id=access_id,
        ).encode()

    def arm_gameplay(
            self, client_ip: str, map_name: str = DEFAULT_GAMEPLAY_MAP,
            character=None, template_row_index=None, mount_ride=None,
            spawn_position=None, *, login_handoff: bool = False):
        self.gameplay_routes.arm(
            client_ip,
            map_name,
            character=character,
            template_row_index=template_row_index,
            mount_ride=mount_ride,
            spawn_position=spawn_position,
            login_handoff=login_handoff,
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] armed {client_ip} for "
            f"{'login handoff on' if login_handoff else 'gameplay on'} "
            f"UDP/{self.port} "
            f"for {GAMEPLAY_ROUTE_TTL_SECONDS:.0f}s ({map_name}); "
            f"{self.gameplay_routes.pending(client_ip)} route(s) pending"
        )

    def arm_gameplay_handoff(
            self, client_ip: str, map_name: str = DEFAULT_GAMEPLAY_MAP,
            character=None, template_row_index=None, mount_ride=None,
            spawn_position=None):
        self.arm_gameplay(
            client_ip,
            map_name,
            character,
            template_row_index,
            mount_ride,
            spawn_position,
            login_handoff=True,
        )


    # Route inspection by address, kept as the query surface the rest of the
    # server and its tests use. With several routes queued for one address
    # these report the one the next unidentified connection would take.
    def _gameplay_map(self, client_ip: str):
        route = self.gameplay_routes.peek(client_ip)
        return route.map_name if route else None

    def _gameplay_character(self, client_ip: str):
        route = self.gameplay_routes.peek(client_ip)
        return copy.deepcopy(route.character) if route else None

    def _gameplay_template_row_index(self, client_ip: str):
        route = self.gameplay_routes.peek(client_ip)
        return route.template_row_index if route else None

    def _gameplay_mount_ride(self, client_ip: str):
        route = self.gameplay_routes.peek(client_ip)
        return copy.deepcopy(route.mount_ride) if route else None

    def _gameplay_spawn_position(self, client_ip: str):
        route = self.gameplay_routes.peek(client_ip)
        return route.spawn_position if route else None

    def _gameplay_is_login_handoff(self, client_ip: str) -> bool:
        route = self.gameplay_routes.peek(client_ip)
        return bool(route and route.login_handoff)

    def _gameplay_is_armed(self, client_ip: str):
        return self.gameplay_routes.is_armed(client_ip)

    def _consume_gameplay_route(self, s: Session):
        """Claim this session's route once the 0x8a hello identifies it."""

        return self.gameplay_routes.claim(
            s.addr[0],
            character_ids=getattr(s, "account_character_ids", ()),
            identified=getattr(s, "account_id", None) is not None,
        )

    def _on_connected(self, s: Session):
        pending = self.gameplay_routes.peek(s.addr[0])
        if pending is not None and not pending.login_handoff:
            s.gameplay_persist_callback = (
                lambda session, reason, flush=False:
                CharacterServer._persist_gameplay_session(
                    self, session, reason=reason, flush=flush
                )
            )
            s.gameplay_mode = True
            # Provisional until the hello identifies the account; see
            # LifecycleService._on_connected.
            CharacterServer._bind_gameplay_route(s, pending)
            if s.gameplay_mount_ride is not None:
                s.gameplay_next_status_effect_instance_id = max(
                    s.gameplay_next_status_effect_instance_id,
                    s.gameplay_mount_ride.instance_id + 1,
                )
                mount_effect = mount_status_effect(
                    s.gameplay_mount_ride,
                    actor_id=LOCAL_PLAYER_ACTOR_ID,
                    active=True,
                ).entries[0]
                s.gameplay_persistent_status_effects[
                    mount_effect.field_04
                ] = mount_effect
            service = GAMEPLAY_SERVICE_NAME.encode('ascii')
            s.send_reliable_ordered(
                b'\x82' + struct.pack('<H', len(service)) + service
            )
            print(f"[{GAMEPLAY_SERVICE_NAME}] gameplay challenge sent to {s.addr}")
            return
        super()._on_connected(s)

    def _on_hello(self, s: Session, payload: bytes):
        claimed = (
            self._consume_gameplay_route(s)
            if self._gameplay_is_armed(s.addr[0])
            else None
        )
        if (
            claimed is None
            and s.gameplay_mode
            and CharacterServer._refuse_unrouted_gameplay(self, s)
        ):
            return
        if not s.gameplay_mode and claimed is not None and claimed.login_handoff:
            map_name = claimed.map_name or DEFAULT_GAMEPLAY_MAP
            # This is login -> gameplay, not login -> character selection.
            # The SwitchMap flag must stay false so ConnectState retains the
            # game-map/loading state and constructs IngameWidget on arrival.
            redirect = switch_map_payload(
                self.char_server_addr,
                enter_character_selection=False,
            )
            s.send_wire_first_delivery_second(b'\x88', redirect)
            print(
                f"[DrasaOnlineLoginServer] handed {map_name} route to "
                f"{self.char_server_addr}; client remains in ConnectState"
            )
            return
        if s.gameplay_mode or claimed is not None:
            s.gameplay_mode = True
            CharacterServer._bind_gameplay_route(s, claimed, claimed=True)
            map_name = s.gameplay_map or DEFAULT_GAMEPLAY_MAP
            s.gameplay_map = map_name
            map_spec = self._gameplay_map_load_spec(
                map_name, s.gameplay_character
            )
            # The captured gameplay frame pairs this with a bare 0x88.
            s.send_multi_reliable_ordered(map_spec, b'\x88')
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] sent {map_name} bootstrap to {s.addr}"
            )
            return
        super()._on_hello(s, payload)

    def _on_data_request(self, s: Session):
        if s.gameplay_mode:
            CharacterServer._send_gameplay_ready_bundle(self, s)

    def _dispatch_gameplay_skill_command_now(
            self, s: Session, command_id: int, payload: bytes) -> None:
        """Execute queued legacy-route input via CharacterServer logic."""
        self._dispatch_char_op_now(s, payload)

    def _dispatch_char_op_now(self, s: Session, payload: bytes):
        """Handle gameplay readiness when localhost routes the map via 2190."""
        if not s.gameplay_mode:
            return super()._dispatch_char_op_now(s, payload)

        command_id = (
            int.from_bytes(payload[1:3], 'little') if len(payload) >= 3 else None
        )
        if VERBOSE_PACKET_LOGGING or command_id != 0x005F:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] 0x8b raw={payload.hex()} "
                f"length={len(payload)} command="
                f"{f'0x{command_id:04x}' if command_id is not None else 'short'}"
            )
        if command_id == 0x006F:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] accepted gameplay-ready "
                "cmd 0x006f; sending local-player initialization"
            )
            CharacterServer._handle_simple_gameplay_command(
                s, payload, command_id
            )
            CharacterServer._schedule_local_player_initialization(self, s)
        elif command_id == 0x0025:
            CharacterServer._accept_gameplay_state_command(s, command_id)
        elif command_id == UPDATE_SHOW_PLAYER_OVERHEAD_ICONS_COMMAND:
            CharacterServer._handle_update_show_player_overhead_icons(s, payload)
        elif command_id == 0x005F:
            CharacterServer._handle_gameplay_move(s, payload)
        elif command_id in {
            BULLET_SKILL_COMMAND,
            TARGET_POINT_BULLET_SKILL_COMMAND,
            TARGET_BULLET_SKILL_COMMAND,
        }:
            CharacterServer._handle_bullet_skill(s, payload)
        elif command_id in {
            SKILL_COMMAND,
            TARGET_SKILL_COMMAND,
            SHIFTED_SKILL_COMMAND,
        }:
            CharacterServer._handle_spatial_skill(s, payload)
        elif command_id == 0x0055:
            CharacterServer._handle_inventory_item_action(self, s, payload)
        elif command_id == PICKUP_ITEM_COMMAND:
            CharacterServer._handle_pickup_item(s, payload)
        elif command_id == 0x0060:
            CharacterServer._handle_map_exit(self, s, payload)
        elif command_id == 0x0061:
            CharacterServer._handle_map_travel(self, s, payload)
        elif command_id == USE_TRAVEL_ITEM_COMMAND:
            CharacterServer._handle_use_travel_item(self, s, payload)
        elif command_id == 0x0063:
            CharacterServer._handle_logout(self, s, payload)
        elif command_id == 0x001C:
            CharacterServer._handle_actor_request(s, payload)
        elif command_id == 0x012B:
            CharacterServer._handle_gauntlet_notification(s, payload)
        elif command_id == 0x00B4:
            CharacterServer._handle_player_query(self, s, payload)
        elif command_id == 0x0059:
            CharacterServer._handle_request_offer(self, s, payload)
        elif command_id == TRANSACTION_COMMAND:
            CharacterServer._handle_transaction(self, s, payload)
        elif command_id == 0x010B:
            CharacterServer._handle_ocp_offer_request(self, s, payload)
        elif command_id == OCP_BOOKING_COMMAND:
            CharacterServer._handle_ocp_booking(self, s, payload)
        elif command_id == ACCEPTED_TIER_SPENDER_COMMAND:
            CharacterServer._handle_tier_spender_accept(self, s, payload)
        elif command_id == SHUFFLE_TIER_SPENDER_DEALS_COMMAND:
            CharacterServer._handle_tier_spender_shuffle(self, s, payload)
        elif command_id == CLIENT_NOTIFICATION_COMMAND:
            CharacterServer._handle_notification_action(self, s, payload)
        elif command_id == ACTIVITY_SIGNIN_PROTOCOL_COMMAND:
            CharacterServer._handle_activity_signin(self, s, payload)
        elif command_id in {
            CREATE_GUILD_COMMAND,
            GUILD_LIST_REQUEST_COMMAND,
            GUILD_LEAVE_COMMAND,
        }:
            CharacterServer._handle_guild_command(
                self, s, payload, command_id
            )
        elif command_id in {
            CREATE_GROUP_COMMAND,
            GROUP_STATUS_REQUEST_COMMAND,
            GROUP_LEAVE_COMMAND,
        }:
            CharacterServer._handle_group_command(
                self, s, payload, command_id
            )
        elif command_id in {
            GROUP_INVITE_COMMAND,
            GUILD_INVITE_COMMAND,
        }:
            CharacterServer._handle_social_invite(
                self, s, payload, command_id
            )
        elif command_id in {
            GROUP_INVITATION_RESPONSE_COMMAND,
            GUILD_INVITATION_RESPONSE_COMMAND,
        }:
            CharacterServer._handle_invitation_response(
                self, s, payload, command_id
            )
        elif command_id == GROUP_FINDER_QUERY_COMMAND:
            CharacterServer._handle_group_finder(self, s, payload)
        elif command_id == GROUP_MEMBER_QUERY_COMMAND:
            CharacterServer._handle_group_member_query(self, s, payload)
        elif command_id == BUDDY_LIST_COMMAND:
            CharacterServer._handle_buddy_list(self, s, payload)
        elif command_id == 0x0029:
            CharacterServer._handle_npc_request(self, s, payload)
        elif command_id == 0x0066:
            CharacterServer._handle_npc_encounter(self, s, payload)
        elif command_id == 0x0135:
            CharacterServer._handle_use_stickerbook_item(self, s, payload)
        elif command_id == SET_STICKER_BOOK_FAVORITE_COMMAND:
            CharacterServer._handle_stickerbook_favorite(self, s, payload)
        elif command_id == UPGRADE_STICKER_BOOK_ITEM_COMMAND:
            CharacterServer._handle_upgrade_stickerbook_item(
                self, s, payload
            )
        elif command_id == 0x0051:
            CharacterServer._handle_quick_slots(self, s, payload)
        elif command_id == ADD_STICKER_BOOK_FROM_INVENTORY_COMMAND:
            CharacterServer._handle_add_stickerbook_from_inventory(
                self, s, payload
            )
        elif command_id == SELECT_TALENT_COMMAND:
            CharacterServer._handle_select_talent(self, s, payload)
        elif command_id == SELECT_TALENT_PRESELECTION_COMMAND:
            CharacterServer._handle_select_talent_preselection(
                self, s, payload
            )
        elif command_id == DESELECT_TALENT_COMMAND:
            CharacterServer._handle_deselect_talent(self, s, payload)
        elif command_id == UNLOCK_SKILL_TALENT_PRESELECTION_COMMAND:
            CharacterServer._handle_unlock_skill_talent_preselection(
                self, s, payload
            )
        elif command_id == RESET_TALENT_PRESELECTION_COMMAND:
            CharacterServer._handle_reset_talent_preselection(
                self, s, payload
            )
        elif command_id == RESET_TALENT_CATEGORY_COMMAND:
            CharacterServer._handle_reset_talent_category(
                self, s, payload
            )
        elif command_id == 0x0038:
            CharacterServer._handle_prop_request(self, s, payload)
        elif command_id == PROP_INTERACTION_COMMAND:
            CharacterServer._handle_prop_interaction(self, s, payload)
        elif command_id == QUEST_TRIGGER_SIGNAL_COMMAND:
            CharacterServer._handle_quest_trigger_signal(self, s, payload)
        elif command_id == 0x0113:
            CharacterServer._handle_staging_signal(self, s, payload)
        elif command_id == 0x0170:
            CharacterServer._accept_gameplay_state_command(s, command_id)
            CharacterServer._send_fractured_planes_effect_choices(self, s)
        elif command_id == 0x0174:
            CharacterServer._handle_fractured_planes_first_clear_acceptance(
                self, s, payload
            )
        elif command_id in {
            0x00BB, 0x00E2, 0x00E8, 0x00EA,
        }:
            CharacterServer._handle_simple_gameplay_command(
                s, payload, command_id
            )
        else:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] ignored character-service "
                "command on gameplay session"
            )
