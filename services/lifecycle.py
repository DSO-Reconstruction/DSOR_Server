"""Connection lifecycle, route arming, and map bootstrap."""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.runtime import progression_queue
from dso_server.runtime.gameplay_scheduler import (
    DEFAULT_GAMEPLAY_DEADLINE_WORKERS,
    DEFAULT_GAMEPLAY_INGRESS_WORKERS,
    DEFAULT_GAMEPLAY_INITIALIZATION_WORKERS,
)
from dso_server.runtime.transport import Session
from dso_server.persistence.character_roster import load_character_roster


class LifecycleService:
    """Connection lifecycle, route arming, and map bootstrap."""

    def start(self):
        super().start()
        threading.Thread(
            target=self._warm_static_catalogs,
            name=f"{self.service_name}-catalog-warmup", daemon=True,
        ).start()
        self._start_presence_movement_publisher()
        self._kill_progress_batching = KILL_PROGRESS_BATCH_SECONDS > 0
        if progression_queue.ENABLED and progression_queue.active() is None:
            # Quests, achievements, kill XP and loose saves run here, off the
            # real-time movement/cast/hit path (runtime/progression_queue.py).
            queue = progression_queue.ProgressionQueue(
                name=f"{self.service_name}-progression"
            )
            queue.start()
            progression_queue.install(queue)
            self._progression_queue = queue
            print(
                f"[{self.service_name}] progression side effects run off the "
                f"real-time loop ({progression_queue.BUDGET_SECONDS * 1000:.0f} ms "
                f"per {progression_queue.WINDOW_SECONDS * 1000:.0f} ms budget)"
            )

    def stop(self):
        queue = getattr(self, "_progression_queue", None)
        if queue is not None:
            # Finish queued credit and saves before sessions are retired.
            queue.stop(timeout=3.0)
            if progression_queue.active() is queue:
                progression_queue.install(None)
        super().stop()

    def _warm_static_catalogs(self) -> None:
        """Build the large lazily-loaded catalogs before the first map entry.

        Map initialization holds the session's mutation gate. On the first
        entry after a start it also parsed quests.xml and the signal and
        consumable tables (~1.9 s cold), and the client's queued commands
        meanwhile tripped the ingress stall warning. Composed test servers
        never call start(), so they keep loading lazily.
        """

        from dso_server.gameplay.map_event_state import load_map_signal_catalog
        from dso_server.gameplay.map_unlocks import map_unlock_catalog
        from dso_server.gameplay.skin_collection import load_skin_catalog
        from dso_server.gameplay.stickerbook_state import load_sticker_definition
        from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
        from dso_server.protocol.consumable_protocol import load_consumable_catalog

        started = time.perf_counter()
        warmed = []
        path = self.static_database_path
        for label, load in (
            ("quests", self._quest_catalog),
            ("map signals", lambda: load_map_signal_catalog(path)),
            ("consumables", lambda: load_consumable_catalog(path)),
            ("map unlocks", lambda: map_unlock_catalog(str(path))),
            ("events", self._event_catalog),
            ("skins", lambda: load_skin_catalog(path)),
            ("status runtime", lambda: load_status_runtime_catalog(path)),
            ("sticker book", lambda: load_sticker_definition(path, "")),
        ):
            try:
                load()
            except Exception as exc:
                print(f"[{self.service_name}] catalog warm-up skipped {label}: "
                      f"{type(exc).__name__}: {exc}")
            else:
                warmed.append(label)
        print(f"[{self.service_name}] warmed {', '.join(warmed)} catalogs in "
              f"{time.perf_counter() - started:.2f}s")

    def __init__(
            self, host: str, port: int = 2192, gameplay_router=None,
            static_database=None,
            account_store: AccountStateStore | None = None,
            social_store: SocialStateStore | None = None):
        super().__init__(
            host,
            port,
            'DrasaCharacterService',
            gameplay_deadline_workers=DEFAULT_GAMEPLAY_DEADLINE_WORKERS,
            gameplay_ingress_workers=DEFAULT_GAMEPLAY_INGRESS_WORKERS,
            gameplay_initialization_workers=(
                DEFAULT_GAMEPLAY_INITIALIZATION_WORKERS
            ),
        )
        self.gameplay_router = gameplay_router
        self.account_store = account_store
        self.social_store = social_store or SocialStateStore(
            os.getenv('DSO_SOCIAL_STATE_FILE', str(DEFAULT_SOCIAL_STATE_PATH))
        )
        self.social_chat_callback = None
        self.system_chat_callback = None
        # Production resolves the same game hostname to a different backend
        # after character selection.  On localhost both backends have to share
        # UDP/2192, so the next connection from the selected client is routed
        # to DrasaOnlineMapServer here.
        self.gameplay_routes = GameplayRouteTable(
            GAMEPLAY_ROUTE_TTL_SECONDS
        )
        # Every map has one authoritative monster state. Sessions retain
        # their own visibility sets, but never their own divergent copy of
        # monster health, transforms, cooldowns, or pending projectiles.
        self.gameplay_world_lock = threading.RLock()
        self.gameplay_world_next_item_ids: dict[object, int] = {}
        self.gameplay_remote_actor_generations: dict[int, int] = {}
        self.character_roster_lock = threading.Lock()
        self.character_roster_revision = 0
        self.character_selection = load_character_roster(self.account_store)
        database_warnings = []
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
        self.world_loot_cache = (
            prime_world_loot_cache(selected_database)
            if selected_database.is_file()
            else None
        )
        self.player_combat_database = (
            prime_player_combat_database(selected_database)
            if selected_database.is_file()
            else None
        )
        if self.world_loot_cache is not None:
            print(
                "  Cached static loot data: "
                f"{len(self.world_loot_cache.item_rows)} items, "
                f"{len(self.world_loot_cache.monster_profiles)} monsters"
            )
        if self.player_combat_database is not None:
            print(
                "  Cached player combat data: "
                f"{len(self.player_combat_database.items)} items, "
                f"{len(self.player_combat_database.enchantments)} "
                "enchantments, "
                f"{len(self.player_combat_database.status_effect_programs)} "
                "status effects"
            )
        if self.socket_effect_database is not None:
            print(
                "  Cached socket effect data: "
                f"{len(self.socket_effect_database.programs)} jewels/runes"
            )
        self.player_template_rows = {}
        if self.static_database is not None:
            for character in self.character_selection.characters:
                try:
                    self.player_template_rows[character.class_id] = (
                        self.static_database.player_template_row_index(
                            character.class_id
                        )
                    )
                except (OSError, ValueError) as exc:
                    database_warnings.append(
                        f"{character.name}: cannot resolve gameplay template: {exc}"
                    )
        self.character_equipment = {}
        if selected_database.is_file():
            database = StaticGameDatabase(selected_database)
            for character in self.character_selection.characters:
                try:
                    self.character_equipment[character.character_id] = (
                        database.character_generation_equipment(
                            character.class_id
                        )
                    )
                except (OSError, ValueError) as exc:
                    database_warnings.append(
                        f"{character.name}: cannot load selection equipment: {exc}"
                    )
                    self.character_equipment[character.character_id] = []
        self.character_selection.equipment = CharacterService._selection_equipment(
            self, self.character_selection.active_character_id,
        )
        (
            self.character_selection_payload,
            self.character_selection_bit_length,
        ) = encode_character_selection(
            self._within_slot_limit(copy.deepcopy(self.character_selection))
        )
        print(
            "  Serialized character selection: "
            f"{len(self.character_selection.characters)} characters, "
            f"{len(self.character_selection.equipment)} equipment records, "
            f"{len(self.character_selection_payload)} bytes/"
            f"{self.character_selection_bit_length} bits"
        )
        for warning in database_warnings:
            print(f"  WARNING: {warning}")
        # Reproduce every command in the retail post-selection sequence from
        # typed local state.  0x001b is delivered with the first 0x0087 above;
        # the remaining entries retain their original order and boundaries.
        event_update_packet, event_update_bits = encode_event_update(
            CharacterService._event_catalog(
                self
            ).event_update()
        )
        self.post_selection_tail = (
            (0x001B, CMD_001B, CMD_001B_BITS),
            (0x00DD, event_update_packet, event_update_bits),
            (
                0x0087,
                self.character_selection_payload,
                self.character_selection_bit_length,
            ),
            (0x010E, EMPTY_OCP_OFFERS, EMPTY_OCP_OFFERS_BITS),
            (0x010C, EMPTY_OCP_METHODS, EMPTY_OCP_METHODS_BITS),
        )
        print(
            "  Built typed post-selection sequence: "
            f"{sum(len(packet) for _, packet, _ in self.post_selection_tail)} "
            "bytes (no captured payload replay)"
        )

    def _on_connected(self, s: Session):
        if self._gameplay_is_armed(s.addr[0]):
            s.gameplay_persist_callback = (
                lambda session, reason, flush=False:
                CharacterService._persist_gameplay_session(
                    self, session, reason=reason, flush=flush
                )
            )
            s.gameplay_mode = True
            # Provisional: the connection has not identified itself yet, so
            # with several routes queued for this address the oldest is used
            # here and corrected from the claimed route at the hello.
            CharacterService._bind_gameplay_route(
                s, self.gameplay_routes.peek(s.addr[0])
            )
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
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] gameplay challenge sent on "
                f"UDP/{self.port} to {s.addr}"
            )
            return

        # Announce the character service first. The client answers with 0x8a;
        # the map and character bootstrap follows that response.
        super()._on_connected(s)

    def arm_gameplay(
            self, client_ip: str, map_name: str = DEFAULT_GAMEPLAY_MAP,
            character=None, template_row_index=None, mount_ride=None,
            spawn_position=None):
        if template_row_index is None and character is not None:
            template_row_index = self.player_template_rows.get(
                character.class_id
            )
        self.gameplay_routes.arm(
            client_ip,
            map_name,
            character=character,
            template_row_index=template_row_index,
            mount_ride=mount_ride,
            spawn_position=spawn_position,
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] armed {client_ip} for UDP/{self.port} "
            f"for {GAMEPLAY_ROUTE_TTL_SECONDS:.0f}s ({map_name}); "
            f"{self.gameplay_routes.pending(client_ip)} route(s) pending"
        )
        # The client still has to load the map before it reconnects and asks
        # to be initialized.  Generate the destination population during that
        # window so the receive thread does not stall on it afterwards.
        CharacterService._prewarm_map_encounter(
            self, map_name, character, spawn_position
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

    def _gameplay_is_armed(self, client_ip: str):
        return self.gameplay_routes.is_armed(client_ip)

    def _consume_gameplay_route(self, s: Session):
        """Claim this session's route at the 0x8a hello.

        Takes a session rather than an address because the hello is the first
        point at which the account is known, and therefore the first point at
        which one of several routes queued for a shared address can be
        attributed to the right client.
        """

        return self.gameplay_routes.claim(
            s.addr[0],
            character_ids=getattr(s, "account_character_ids", ()),
            identified=getattr(s, "account_id", None) is not None,
        )

    def _refuse_unrouted_gameplay(self, s: Session) -> bool:
        """Drop a gameplay reconnect that found no route of its own.

        Only under login enforcement. The connection was flagged for gameplay
        at ``ID_NEW_INCOMING_CONNECTION`` because *some* route was pending for
        its address, and provisionally bound to it. Now that the hello has
        identified the account, that route turned out to belong to another
        account sharing the address, so keeping the provisional binding would
        put this client into someone else's character. Without enforcement
        the historical behaviour stands: the binding is kept.
        """

        if not getattr(self, "require_account_identity", False):
            return False
        self.drop_session(
            s,
            reason=(
                f"account {s.account_id} ({s.account_username!r}) reconnected "
                f"for gameplay, but every route pending for {s.addr[0]} "
                "belongs to another account"
            ),
        )
        return True

    @staticmethod
    def _map_load_payload(map_name='a0000_char') -> bytes:
        return MapLoadPacket(map_name, ruleset=-1).encode()[0]

    def _gameplay_map_load_spec(
            self, map_name, character=None) -> tuple[bytes, int]:
        """Build the editable gameplay 0x86 map load without packet replay."""
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
            # Keep the two known protocol values available to a deliberately
            # database-less test/development server.
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

    def _refresh_character_selection_from_store(self) -> None:
        if self.account_store is None:
            return
        with self.character_roster_lock:
            roster = tuple(
                (character.character_id, character.map_id,
                 character.premium_currency)
                for character in self.character_selection.characters
            )
        updates = {}
        for character_id, map_id, premium_currency in roster:
            state = self.account_store.get(character_id)
            if state is None:
                profile = self.account_store.get_profile(character_id)
                if profile is None:
                    continue
                saved_map, saved_premium = profile.map_id, profile.premium_currency
            else:
                saved_map, saved_premium = state.map_id, state.currency.andermant
            if map_id != saved_map or premium_currency != saved_premium:
                updates[character_id] = (saved_map, saved_premium)
        if not updates:
            return
        changed = False
        with self.character_roster_lock:
            for character in self.character_selection.characters:
                update = updates.get(character.character_id)
                if update is None:
                    continue
                if (
                    character.map_id != update[0]
                    or character.premium_currency != update[1]
                ):
                    character.map_id, character.premium_currency = update
                    changed = True
            if changed:
                self.character_roster_revision += 1
        if not changed:
            return
        self._reencode_character_selection()

    def _owned_character_ids(self, s: Session) -> tuple[int, ...] | None:
        """Character IDs this connection's account owns, or ``None``.

        ``None`` means the connection was never identified -- no registry, or
        a client that connected without logging in -- and keeps the historical
        behavior of showing the whole configured roster.
        """

        registry = getattr(self, "account_registry", None)
        if registry is None:
            return None
        if s.account_id is None:
            # Under login enforcement an unidentified connection owns
            # nothing. The hello already dropped it; this is the backstop for
            # any request that arrives ahead of identification.
            if getattr(self, "require_account_identity", False):
                return ()
            return None
        try:
            account = registry.account_by_id(s.account_id)
        except Exception as exc:
            print(
                "[DrasaCharacterService] cannot resolve characters for "
                f"account {s.account_id}: {type(exc).__name__}: {exc}"
            )
            return tuple(s.account_character_ids or ())
        owned = tuple(account.character_ids)
        s.account_character_ids = owned
        return owned

    def _visible_character_ids(self, s: Session) -> tuple[int, ...] | None:
        """The characters this connection would actually be shown.

        Ownership alone is not enough: a claimed character that is missing
        from the roster shows up as an empty list, and an empty list is what
        sends the client into its placeholder state. The decision to withhold
        the list has to be made on what would be sent, not on what the
        registry says the account owns.
        """

        owned = self._owned_character_ids(s)
        if owned is None:
            return None
        owned_set = set(owned)
        with self.character_roster_lock:
            return tuple(
                int(character.character_id)
                for character in self.character_selection.characters
                if int(character.character_id) in owned_set
            )

    def _account_owns_character(self, s: Session, character_id: int) -> bool:
        """Whether this connection may act on ``character_id``."""

        owned = self._owned_character_ids(s)
        if owned is None:
            return True
        return int(character_id) in set(owned)

    def _require_character_ownership(
        self, s: Session, character_id: int, *, action: str, drop: bool = True
    ) -> bool:
        """Refuse, and usually drop, a request for an unowned character.

        Dropping is for tampering: the client can only offer characters the
        server listed for it, so naming another one means the list was not
        where the id came from. Two cases are *not* tampering and only get a
        refusal, because dropping them strands legitimate players:

        * An account that owns no characters was never shown a list at all.
          It is on the creation screen, and the client still probes with
          whatever character it has cached locally from a previous account on
          the same machine. Killing that connection leaves a new player
          unable to finish creating their first character.
        * Informational requests (``drop=False``), which the client makes
          from that same cached state before the player has chosen anything.
        """

        if self._account_owns_character(s, character_id):
            return True
        owned = self._owned_character_ids(s)
        tampering = drop and bool(owned)
        message = (
            f"account {s.account_id} ({s.account_username!r}) requested "
            f"{action} for character {character_id}, which it does not own"
        )
        if tampering:
            self.drop_session(s, reason=message)
        else:
            print(f"[DrasaCharacterService] refused: {message}")
        return False

    def _claim_character_for_account(self, s: Session, character_id: int) -> None:
        """Attach a newly created character to the connected account."""

        registry = getattr(self, "account_registry", None)
        if registry is None or s.account_id is None:
            return
        try:
            account = registry.account_by_id(s.account_id)
            registry.claim_character(account.username, int(character_id))
        except Exception as exc:
            print(
                "[DrasaCharacterService] could not attach character "
                f"{character_id} to account {s.account_id}: "
                f"{type(exc).__name__}: {exc}"
            )
            return
        s.account_character_ids = tuple(
            sorted({*(s.account_character_ids or ()), int(character_id)})
        )
        with self.character_roster_lock:
            if hasattr(self, "_account_selection_cache"):
                self._account_selection_cache.pop(s.account_id, None)
        print(
            f"[DrasaCharacterService] character {character_id} now belongs to "
            f"{s.account_username!r} (account {s.account_id})"
        )

    def _release_character_from_account(self, s: Session, character_id: int) -> None:
        """Detach a deleted character from whichever account claimed it."""

        registry = getattr(self, "account_registry", None)
        if registry is not None:
            try:
                registry.release_character(int(character_id))
            except Exception as exc:
                print(
                    "[DrasaCharacterService] could not detach character "
                    f"{character_id}: {type(exc).__name__}: {exc}"
                )
        if s.account_character_ids:
            s.account_character_ids = tuple(
                value for value in s.account_character_ids
                if int(value) != int(character_id)
            )
        with self.character_roster_lock:
            if hasattr(self, "_account_selection_cache"):
                self._account_selection_cache.pop(s.account_id, None)

    def _account_selection_payload(self, s: Session):
        """The 0x0087 payload holding only this account's characters."""

        owned = self._owned_character_ids(s)
        if owned is None:
            return self.character_selection_payload, self.character_selection_bit_length

        owned_set = set(owned)
        with self.character_roster_lock:
            revision = self.character_roster_revision
            cache = getattr(self, "_account_selection_cache", None)
            if cache is None:
                cache = self._account_selection_cache = {}
            selection = copy.deepcopy(self.character_selection)

        selection.characters = [
            character
            for character in selection.characters
            if int(character.character_id) in owned_set
        ]
        active = (
            selection.active_character_id
            if selection.active_character_id in owned_set
            else (
                int(selection.characters[0].character_id)
                if selection.characters
                else 0
            )
        )
        selection.active_character_id = active
        # The payload carries one equipment list: the active character's.
        # Build it from the account store on every selection request. Gameplay
        # customization changes the inventory without changing the character
        # roster revision, so a revision-only cache can leave the character
        # preview showing the old skin and dye after returning to this screen.
        selection.equipment = copy.deepcopy(
            CharacterService._selection_equipment(self, active)
        )
        payload, bit_length = encode_character_selection(selection)
        with self.character_roster_lock:
            self._account_selection_cache[s.account_id] = (
                (revision, frozenset(owned_set)),
                payload,
                bit_length,
            )
        return payload, bit_length

    @staticmethod
    def _within_slot_limit(selection):
        """Trim a roster copy to what one client screen can hold.

        Only the unscoped payload needs this. Per-account lists are already
        bounded, because creation enforces the slot limit per account, and
        the roster itself must keep every character so the account that owns
        one is never sent an empty list.
        """

        if len(selection.characters) > selection.character_slots:
            selection.characters = selection.characters[
                :selection.character_slots
            ]
            visible = {
                int(character.character_id)
                for character in selection.characters
            }
            if selection.active_character_id not in visible:
                selection.active_character_id = (
                    int(selection.characters[0].character_id)
                    if selection.characters
                    else 0
                )
        return selection

    def _reencode_character_selection(self) -> None:
        # Encoding can be sizeable. Snapshot under the roster lock, encode
        # without it, then publish only if no concurrent creation/refresh
        # changed the roster in the meantime.
        while True:
            with self.character_roster_lock:
                revision = self.character_roster_revision
                selection = copy.deepcopy(self.character_selection)
            payload, bit_length = encode_character_selection(
                self._within_slot_limit(selection)
            )
            with self.character_roster_lock:
                if revision != self.character_roster_revision:
                    continue
                self.character_selection_payload = payload
                self.character_selection_bit_length = bit_length
                # Per-account payloads are keyed by this revision, so they
                # rebuild on the next send rather than going stale.
                if hasattr(self, "_account_selection_cache"):
                    self._account_selection_cache.clear()
                if hasattr(self, 'post_selection_tail'):
                    self.post_selection_tail = tuple(
                        (
                            command_id,
                            payload,
                            bit_length,
                        )
                        if command_id == 0x0087
                        else (command_id, packet, bits)
                        for command_id, packet, bits
                        in self.post_selection_tail
                    )
                return

    def _send_character_selection(
            self, s: Session, *, force: bool = False, trailing_payloads=()):
        self._refresh_character_selection_from_store()
        with s.flow_lock:
            if s.character_data_sent and not force:
                return False
            s.character_data_sent = True

        if self._visible_character_ids(s) == ():
            # An account with nothing to show must never receive a selection
            # list, not even an empty one. The client answers that command by
            # building its screen around a client-side placeholder character
            # and then entering the world with it. Retail sends the creation
            # editor instead, which is the only thing there is to show.
            self._open_character_generation_editor(
                s, reason="account has no characters"
            )
            if trailing_payloads:
                s.send_multi_reliable_ordered(
                    *trailing_payloads,
                    track=True,
                    label='character-selection-trailing',
                )
            print(
                "[DrasaCharacterService] no character list sent to account "
                f"{s.account_id} ({s.account_username!r}); opened the "
                "creation editor instead"
            )
            return True

        packet, bit_length = self._account_selection_payload(s)
        fragments = tuple(
            packet[offset:offset + TAIL_FRAG_SIZE]
            for offset in range(0, len(packet), TAIL_FRAG_SIZE)
        )
        if len(fragments) > 1:
            s.send_reliable_ordered_split(
                fragments,
                trailing_payloads=trailing_payloads,
                track=True,
                label_prefix='character-selection',
                total_bit_length=bit_length,
            )
        else:
            s.send_multi_reliable_ordered(
                (packet, bit_length), *trailing_payloads,
                track=True,
                label='character-selection',
            )
        owned = self._owned_character_ids(s)
        scope = (
            f"account {s.account_id} ({s.account_username!r}): "
            + (
                ", ".join(str(character_id) for character_id in owned)
                if owned
                else "no characters"
            )
            if owned is not None
            else "unscoped roster"
        )
        print(
            "[DrasaCharacterService] sent serialized character selection "
            f"({len(packet)} bytes in {len(fragments)} fragments) -- {scope}"
        )
        return True

    def _send_bootstrap(self, s: Session):
        with s.flow_lock:
            if s.map_bootstrap_sent:
                return
            s.map_bootstrap_sent = True

        # Exact session 57602 post-hello datagram: map announcement first
        # (reliable/order index 2), then bare auth-complete (index 3).
        # The real character service does not send 0x87 here and waits for the
        # client's 0x8d before replaying the character-selection command.
        map_load = self._map_load_payload()
        s.send_multi_reliable_ordered(
            (map_load, len(map_load) * 8 - 7), b'\x88'
        )

        print("[DrasaCharacterService] map/auth bootstrap complete; waiting for 0x8d")

    @staticmethod
    def _bind_gameplay_route(s: Session, route, *, claimed: bool = False) -> None:
        """Bind a route; only an identified, claimed route starts a new map."""

        if route is None:
            return
        timer_to_cancel = None
        with s.flow_lock:
            s.gameplay_map = route.map_name
            s.gameplay_character = copy.deepcopy(route.character)
            s.gameplay_template_row_index = route.template_row_index
            s.gameplay_mount_ride = copy.deepcopy(route.mount_ride)
            s.gameplay_route_spawn_position = route.spawn_position
            if claimed:
                # _on_connected peeks provisionally; _on_hello claims the
                # identified route. Do not reset on the provisional bind.
                timer_to_cancel = s.gameplay_initialization_timer
                s.gameplay_initialization_timer = None
                s.gameplay_initialization_sent = False
                s.gameplay_ready_sent = False
                s.gameplay_presence_ready = False
                s.gameplay_map_access_group_id = None
                s.gameplay_map_event_runtime = None
        if timer_to_cancel is not None:
            timer_to_cancel.cancel()
        if NPC_TRACE:
            print(
                f"[NPC-TRACE] route bound map={route.map_name!r} "
                f"claimed={claimed}"
            )

    def _on_hello(self, s: Session, payload: bytes):
        if s.gameplay_mode or self._gameplay_is_armed(s.addr[0]):
            # The account is known by now, so this replaces whatever the
            # connection provisionally picked up with the route that actually
            # belongs to this client.
            route = self._consume_gameplay_route(s)
            if route is None and self._refuse_unrouted_gameplay(s):
                return
            s.gameplay_mode = True
            CharacterService._bind_gameplay_route(s, route, claimed=True)
            map_name = s.gameplay_map or DEFAULT_GAMEPLAY_MAP
            s.gameplay_map = map_name

            map_spec = self._gameplay_map_load_spec(
                map_name, s.gameplay_character
            )
            # Frame 73 in the production trace is exactly 108 bytes on wire:
            # a 15-byte a0200_kingscity announcement plus a bare 0x88.  An
            # extra trigger payload would make that captured size impossible.
            s.send_multi_reliable_ordered(
                map_spec, b'\x88',
                track=True,
                label='gameplay-map-bootstrap',
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] sent {map_name} map bootstrap "
                f"to {s.addr}; waiting for 0x8d"
            )
            return
        self._send_bootstrap(s)

    def _on_data_request(self, s: Session):
        if s.gameplay_mode:
            self._send_gameplay_ready_bundle(s)
            return

        if not s.map_bootstrap_sent:
            self._send_bootstrap(s)

        with s.flow_lock:
            send_sync = not s.pre_character_sync_sent
            if send_sync:
                s.pre_character_sync_sent = True
        if send_sync:
            # Exact application shape and RakNet reliability/order semantics
            # observed immediately before the first 0x0087 in session 57602.
            now = server_time() & 0xffffffff
            timestamped_custom = (
                b'\x1b'
                + struct.pack('>Q', now)
                + b'\x83'
                + struct.pack('<I', now)
                + bytes(12)
            )
            s.send_unreliable_sequenced(timestamped_custom)
            print(
                "[DrasaCharacterService] sent pre-selection timestamped "
                "custom message 0x1b/0x83"
            )
            if PRE_CHARACTER_SYNC_DELAY_SECONDS > 0:
                time.sleep(PRE_CHARACTER_SYNC_DELAY_SECONDS)

        if self._send_character_selection(
                s, trailing_payloads=((CMD_001B, CMD_001B_BITS),)):
            # Session 57602 sends cmd 0x001b immediately after the second split
            # fragment, in the same outgoing datagram sequence.
            print("[DrasaCharacterService] sent post-selection cmd 0x001b")
            self._schedule_post_selection_tail(s)

    @staticmethod
    def _gameplay_timestamp_payload() -> bytes:
        now = server_time() & 0xffffffff
        return (
            b'\x1b'
            + struct.pack('>Q', now)
            + b'\x83'
            + struct.pack('<I', now)
            + bytes(12)
        )

    def _send_gameplay_ready_bundle(self, s: Session):
        with s.flow_lock:
            if s.gameplay_ready_sent:
                return
            s.gameplay_ready_sent = True

        # Production frame 78 is 104 bytes on wire and contains 0x1b + 0x84.
        # Two reliable/order headers around these 26-byte and 12-byte payloads
        # reproduce that size exactly.  CMD_001B is the captured, valid 89-bit
        # command used by this client build immediately after map bootstrap.
        timestamped_custom = CharacterService._gameplay_timestamp_payload()
        s.send_multi_reliable_ordered(
            timestamped_custom, (CMD_001B, CMD_001B_BITS),
            track=True,
            label='gameplay-ready-bundle',
        )

        # The retail destination trace sends NewPlayer 981 ms after this
        # bundle. Keeping the same cadence matters: NewPlayer and the final
        # PlayerReady advance the active loading screen, while delaying them
        # until after the scene is visible leaves a frozen world with no valid
        # IngameWidget. The login-to-map handoff keeps ConnectState active
        # during this interval.
        if GAMEPLAY_ENTITY_DELAY_SECONDS > 0:
            timer = threading.Timer(
                GAMEPLAY_ENTITY_DELAY_SECONDS,
                CharacterService._send_local_player_initialization,
                args=(self, s),
                kwargs={'scheduled': True},
            )
            timer.daemon = True
            with s.flow_lock:
                s.gameplay_initialization_timer = timer
            timer.start()
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] received client 0x8d and sent "
                "typed 0x1b/0x84 ready bundle; local-player entity "
                f"initialization scheduled in +{GAMEPLAY_ENTITY_DELAY_SECONDS:.3f}s"
            )
        else:
            CharacterService._send_local_player_initialization(self, s)
