"""Built-in chat service and social-channel membership routing."""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.runtime.transport import RakNetServer, Session
from dso_server.paths import DEFAULT_CHAT_AUDIT_PATH
from dso_server.runtime.maintenance import (
    DEFAULT_SHUTDOWN_MINUTES, MAX_SHUTDOWN_MINUTES,
)


class ChatServer(RakNetServer):
    def __init__(
            self, host: str, *, instance_id: int | None = None,
            region_id: int | None = None, user_name: str | None = None,
            map_room_id: str | None = None,
            character_selection=None, gameplay_servers=(),
            social_store: SocialStateStore | None = None,
            static_database: StaticGameDatabase | None = None,
            account_registry=None,
            audit_path: str | Path | None = 'chat-service.log',
            shutdown_callback=None):
        super().__init__(host, 2191, 'ChatService')
        if instance_id is None:
            # The retail captures all use chat instance 481.  The same value
            # is supplied to ClientChatManager through -instanceid.
            instance_id = int(os.getenv('DSO_CHAT_INSTANCE_ID', '481'))
        if not 0 < instance_id <= 0xffffffff:
            raise ValueError('chat instance id must be a non-zero uint32')
        configured_region = os.getenv('DSO_CHAT_REGION_ID')
        self.region_id_is_override = (
            region_id is not None or configured_region is not None
        )
        if region_id is None:
            region_id = int(configured_region or '5')
        if not 0 < region_id <= 0xffffffff:
            raise ValueError('chat region id must be a non-zero uint32')
        if map_room_id is None:
            map_room_id = os.getenv('DSO_CHAT_MAP_ROOM_ID') or str(uuid.uuid4())
        try:
            map_room_id = str(uuid.UUID(map_room_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError('chat map room id must be a UUID') from exc
        self.instance_id = instance_id
        self.region_id = region_id
        self.map_room_id = map_room_id
        self.user_name = user_name or os.getenv(
            'DSO_CHAT_USERNAME', 'Test Acc 1'
        )
        self.character_selection = character_selection
        self.gameplay_servers = tuple(gameplay_servers)
        self.shutdown_callback = shutdown_callback
        self.social_store = social_store
        # The chat hello is the only client packet carrying the session GUID,
        # so this is where a login can actually be proven. ``None`` keeps the
        # historical behavior of trusting whatever the client sends.
        self.account_registry = account_registry
        self.static_database = static_database or StaticGameDatabase(
            os.getenv('DSO_STATIC_DB', DEFAULT_STATIC_DB_PATH)
        )
        if audit_path == 'chat-service.log':
            audit_path = os.getenv('DSO_CHAT_AUDIT_LOG', str(DEFAULT_CHAT_AUDIT_PATH))
        self.audit_path = Path(audit_path) if audit_path else None
        self.audit_lock = threading.Lock()

    def _character_for_user_id(self, user_id: int):
        roster = getattr(self.character_selection, 'characters', ())
        return next(
            (
                character
                for character in roster
                if int(character.character_id) == int(user_id)
            ),
            None,
        )

    @staticmethod
    def _admin_flags(character):
        """Return the overhead colour and the command gate for a roster character."""
        privileges = bool(
            character is not None
            and getattr(character, 'admin_privileges', False)
        )
        return bool(
            character is not None
            and (getattr(character, 'admin_name_color', False) or privileges)
        ), privileges

    def refresh_identity(self, user_id: int) -> int:
        """Re-sync live chat sessions after a CMS privilege or name change.

        Chat reads the roster, so the durable change must already be applied.
        Returns how many sessions were refreshed.
        """
        character = self._character_for_user_id(user_id)
        if character is None:
            return 0
        flags = self._admin_flags(character)
        name = str(character.name)
        updated = 0
        for session in tuple(self.sessions.values()):
            if (
                session.chat_user_id is not None
                and int(session.chat_user_id) == int(user_id)
            ):
                session.chat_user_name = name
                session.chat_admin_name_color, session.chat_is_admin = flags
                updated += 1
        return updated

    def _gameplay_server_and_session_for_user_id(self, user_id: int):
        for gameplay_server in self.gameplay_servers:
            for session in tuple(gameplay_server.sessions.values()):
                character = session.gameplay_character
                if (
                    session.connected
                    and session.gameplay_mode
                    and not session.gameplay_presence_departed
                    and character is not None
                    and int(character.character_id) == int(user_id)
                ):
                    return gameplay_server, session
        return None, None

    def _gameplay_server_and_session_for_character_name(self, name: str):
        matches = {}
        for server in self.gameplay_servers:
            for session in tuple(server.sessions.values()):
                character = session.gameplay_character
                if (
                    session.connected
                    and session.gameplay_mode
                    and not session.gameplay_presence_departed
                    and not session.gameplay_exit_transition_started
                    and character is not None
                    and character.name.casefold() == name.casefold()
                ):
                    matches[int(character.character_id)] = (server, session)
        if not matches:
            raise ValueError(f"character {name!r} is not online")
        if len(matches) > 1:
            raise ValueError(f"more than one online character is named {name!r}")
        return next(iter(matches.values()))

    def _gameplay_session_for_user_id(self, user_id: int):
        _server, session = self._gameplay_server_and_session_for_user_id(
            user_id
        )
        return session

    @staticmethod
    def _send_system_message(s: Session, text: str) -> None:
        response = encode_chat_text(
            encode_received_whisper(
                WhisperChatMessage(
                    str(s.chat_user_name or ""),
                    "hb",
                    str(text),
                ),
                "System",
            )
        )
        s.send_reliable_ordered(
            response,
            track=True,
            label="chat-admin-system",
        )

    def _handle_admin_command(self, s: Session, text: str) -> None:
        if not s.chat_is_admin:
            self._send_system_message(
                s, "This command requires administrator privileges."
            )
            return
        arguments = text.strip().split()
        command = arguments[0].casefold() if arguments else "!help"
        gameplay_server, gameplay = (
            self._gameplay_server_and_session_for_user_id(s.chat_user_id)
        )
        if command == "!help":
            self._send_system_message(
                s,
                "Admin commands: !help | !item <templateId> [amount] | "
                "!map <mapId> [difficulty] | !where | !speed [character name] | "
                "!unlockmaps [character name] | !immortal [character name] | "
                "!killmobs | !stress <amount> [templateId] | "
                "!shutdown [minutes]",
            )
            return
        if gameplay is None or gameplay_server is None:
            raise ValueError("your gameplay character is not online")
        if command == "!shutdown":
            if len(arguments) not in {1, 2}:
                raise ValueError("usage: !shutdown [minutes] (default: 5)")
            try:
                minutes = (
                    int(arguments[1]) if len(arguments) == 2
                    else DEFAULT_SHUTDOWN_MINUTES
                )
            except ValueError:
                raise ValueError(
                    "usage: !shutdown [minutes]; minutes must be a whole number"
                ) from None
            if not 0 <= minutes <= MAX_SHUTDOWN_MINUTES:
                raise ValueError(f"minutes must be from 0 to {MAX_SHUTDOWN_MINUTES}")
            if self.shutdown_callback is None:
                raise ValueError("maintenance shutdown is unavailable on this server")
            self.shutdown_callback(minutes)
            self._send_system_message(
                s, f"Server maintenance scheduled in {minutes} minute(s)."
            )
            return
        if command == "!speed":
            if len(arguments) > 1:
                gameplay_server, gameplay = (
                    self._gameplay_server_and_session_for_character_name(
                        " ".join(arguments[1:])
                    )
                )
            enabled = CharacterService._toggle_admin_speed(gameplay_server, gameplay)
            self._send_system_message(
                s, f"Movement speed +100% {'enabled' if enabled else 'disabled'} "
                f"for {gameplay.gameplay_character.name}.",
            )
            return
        if command == "!unlockmaps":
            if len(arguments) > 1:
                gameplay_server, gameplay = self._gameplay_server_and_session_for_character_name(
                    " ".join(arguments[1:]))
            CharacterService._unlock_all_maps(gameplay_server, gameplay)
            self._send_system_message(
                s, f"All maps unlocked for {gameplay.gameplay_character.name}. "
                "Change maps or relog to refresh the travel menu.")
            return
        if command == "!immortal":
            if len(arguments) > 1:
                gameplay_server, gameplay = (
                    self._gameplay_server_and_session_for_character_name(
                        " ".join(arguments[1:])
                    )
                )
            with gameplay.flow_lock:
                gameplay.gameplay_gm_invulnerable = not bool(
                    getattr(gameplay, "gameplay_gm_invulnerable", False)
                )
                enabled = gameplay.gameplay_gm_invulnerable
            CharacterService._request_account_save(
                gameplay, "admin invulnerability changed", flush=True
            )
            self._send_system_message(
                s,
                f"GM invulnerability {'enabled' if enabled else 'disabled'} "
                f"for {gameplay.gameplay_character.name}.",
            )
            return
        if command == "!killmobs":
            if len(arguments) != 1:
                raise ValueError("usage: !killmobs")
            killed = gameplay_server._admin_kill_all_monsters(gameplay)
            self._send_system_message(
                s, f"Killed {killed} monster(s) on the current map instance."
            )
            return
        if command == "!stress":
            if len(arguments) not in {2, 3}:
                raise ValueError("usage: !stress <amount> [templateId]")
            try:
                amount = int(arguments[1])
            except ValueError:
                raise ValueError(
                    "usage: !stress <amount> [templateId]; amount must be a "
                    "whole number"
                ) from None
            spawned, template_id = gameplay_server._admin_spawn_stress_monsters(
                gameplay, amount, arguments[2] if len(arguments) == 3 else None,
            )
            self._send_system_message(
                s, f"Spawned {spawned}x {template_id} around you. "
                "!killmobs clears them.",
            )
            return
        if command == "!where":
            move = gameplay.gameplay_last_move
            position = (
                move.relative_position
                if move is not None
                else gameplay.gameplay_spawn_position
            )
            self._send_system_message(
                s,
                f"Map={gameplay.gameplay_map} position={position}",
            )
            return
        if command == "!item":
            if len(arguments) not in {2, 3}:
                raise ValueError("usage: !item <templateId> [amount]")
            amount = int(arguments[2]) if len(arguments) == 3 else 1
            gameplay_server._grant_inventory_item(
                gameplay, arguments[1], amount
            )
            self._send_system_message(
                s, f"Granted {amount}x {arguments[1]}."
            )
            return
        if command == "!map":
            if len(arguments) not in {2, 3}:
                raise ValueError("usage: !map <mapId> [difficulty]")
            difficulty = (
                int(arguments[2]) if len(arguments) == 3 else 0
            )
            if not gameplay_server._begin_map_transfer(
                gameplay,
                arguments[1],
                reason="admin-chat",
                gameplay_switch=True,
                scaling_difficulty=difficulty,
            ):
                raise ValueError("a map transfer is already in progress")
            self._send_system_message(
                s, f"Teleporting to {arguments[1]}."
            )
            return
        raise ValueError(
            f"unknown admin command {command!r}; use !help"
        )

    def _chat_region_id_for_user(self, user_id: int) -> int:
        if self.region_id_is_override:
            return self.region_id
        gameplay = self._gameplay_session_for_user_id(user_id)
        if gameplay is None or not gameplay.gameplay_map:
            return self.region_id
        try:
            region_name = self.static_database.gameplay_map_chat_region(
                gameplay.gameplay_map
            )
            return CHAT_REGION_IDS[region_name]
        except (
            FileNotFoundError,
            KeyError,
            OSError,
            ValueError,
            CharacterSelectionError,
        ) as exc:
            print(
                f"[ChatService] cannot resolve ChatRegion for "
                f"{gameplay.gameplay_map!r}: {exc}; using fallback "
                f"{self.region_id}"
            )
            return self.region_id

    def _chat_session_for_user_id(
            self, user_id: int, *, excluding: Session | None = None):
        return next(
            (
                session
                for session in tuple(self.sessions.values())
                if (
                    session is not excluding
                    and session.chat_user_id is not None
                    and int(session.chat_user_id) == int(user_id)
                )
            ),
            None,
        )

    def _desired_social_chat_rooms(self, user_id: int) -> dict[str, str]:
        if self.social_store is None:
            return {}
        rooms = {}
        group = self.social_store.group_for(user_id)
        if group is not None:
            rooms["group"] = (
                f"Group__{self.instance_id}_{group.group_id}"
            )
        guild = self.social_store.guild_for(user_id)
        if guild is not None:
            rooms["guild"] = (
                f"Guild__{self.instance_id}_{guild.guild_id}"
            )
        return rooms

    def _refresh_social_membership_for_session(self, s: Session) -> None:
        if (
            self.sessions.get(s.addr) is not s
            or s.chat_user_id is None
        ):
            return
        desired = self._desired_social_chat_rooms(s.chat_user_id)
        with s.flow_lock:
            previous = dict(s.chat_social_rooms)
        packets = []
        audit_events = []
        for channel, previous_room in previous.items():
            if desired.get(channel) == previous_room:
                continue
            packets.append(encode_chat_text(f"l#{channel}"))
            audit_events.append(f"leave {channel}")
        for channel, room in desired.items():
            if previous.get(channel) == room:
                continue
            packets.append(encode_chat_text(f"e#{room}"))
            audit_events.append(f"join {room}")
        if packets:
            s.send_multi_reliable_ordered(
                *packets,
                track=True,
                label="chat-social-memberships",
            )
        with s.flow_lock:
            s.chat_social_rooms = desired
            s.chat_joined_channels.difference_update(
                {"group", "guild"}
            )
            s.chat_joined_channels.update(desired)
        for event in audit_events:
            operation, detail = event.split(" ", 1)
            self._audit(s, operation, detail)

    def refresh_social_memberships(
            self, character_ids: tuple[int, ...] = ()) -> None:
        selected = {int(value) for value in character_ids}
        for session in tuple(self.sessions.values()):
            if session.chat_user_id is None:
                continue
            if selected and int(session.chat_user_id) not in selected:
                continue
            self._refresh_social_membership_for_session(session)

    def _channel_recipients(
            self, sender: Session, channel: str) -> tuple[Session, ...]:
        channel = channel.casefold()
        candidates = tuple(
            session
            for session in tuple(self.sessions.values())
            if (
                session.chat_user_id is not None
                and channel in session.chat_joined_channels
            )
        )
        if channel == 'region':
            sender_region = self._chat_region_id_for_user(
                sender.chat_user_id
            )
            return tuple(
                session
                for session in candidates
                if self._chat_region_id_for_user(
                    session.chat_user_id
                ) == sender_region
            )
        if channel == 'map':
            sender_gameplay = self._gameplay_session_for_user_id(
                sender.chat_user_id
            )
            sender_map = (
                sender_gameplay.gameplay_map
                if sender_gameplay is not None
                else None
            )
            return tuple(
                session
                for session in candidates
                if (
                    (
                        self._gameplay_session_for_user_id(
                            session.chat_user_id
                        ).gameplay_map
                        if self._gameplay_session_for_user_id(
                            session.chat_user_id
                        ) is not None
                        else None
                    )
                    == sender_map
                )
            )
        if channel in {'group', 'guild'} and self.social_store is not None:
            lookup = (
                self.social_store.group_for
                if channel == 'group'
                else self.social_store.guild_for
            )
            source_collective = lookup(sender.chat_user_id)
            if source_collective is None:
                return (sender,)
            source_id = getattr(
                source_collective, f'{channel}_id'
            )
            return tuple(
                session
                for session in candidates
                if (
                    (collective := lookup(session.chat_user_id)) is not None
                    and getattr(collective, f'{channel}_id') == source_id
                )
            )
        # Unknown/private channels must not leak globally.
        return (sender,)

    def _resolve_whisper_target(
            self, sender: Session, requested_name: str, text: str):
        """Resolve the chat UI's space-split whisper target.

        The UI sends ``sw#Test#...#Acc 2 message`` for ``Example Player 2``.
        Recombine the longest online character name before routing.
        """

        requested = requested_name.strip()
        online = tuple(
            session
            for session in tuple(self.sessions.values())
            if (
                session is not sender
                and session.chat_user_id is not None
                and session.chat_user_name
            )
        )
        exact = next(
            (
                session for session in online
                if session.chat_user_name.casefold() == requested.casefold()
            ),
            None,
        )
        if exact is not None:
            return exact, text

        prefix = requested + ' '
        matches = []
        for session in online:
            full_name = session.chat_user_name
            if not full_name.casefold().startswith(prefix.casefold()):
                continue
            suffix = full_name[len(prefix):]
            if text.casefold() == suffix.casefold():
                matches.append((len(full_name), session, ''))
            elif text[:len(suffix)].casefold() == suffix.casefold() and (
                len(text) > len(suffix) and text[len(suffix)] == ' '
            ):
                matches.append(
                    (len(full_name), session, text[len(suffix) + 1:])
                )
        if not matches:
            return None, text
        _length, target, message = max(matches, key=lambda item: item[0])
        return target, message

    def _audit(self, s: Session, event: str, detail: str):
        """Persist the small 2191 application trace for live diagnosis."""

        line = (
            f'{time.strftime("%Y-%m-%d %H:%M:%S")} '
            f'{s.addr[0]}:{s.addr[1]} {event} {detail}\n'
        )
        if self.audit_path is not None:
            with self.audit_lock:
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                with self.audit_path.open('a', encoding='utf-8') as stream:
                    stream.write(line)

    def _authenticate_hello(self, s: Session, hello) -> bool | None:
        """Resolve the hello's session GUID against the account registry.

        Returns ``None`` when no registry is configured (the session is
        accepted unverified, as it always was), ``True`` when the session and
        its character ownership check out, and ``False`` when the client must
        be refused.
        """

        registry = self.account_registry
        if registry is None:
            return None
        try:
            record = registry.validate(hello.session_id)
        except Exception as exc:
            # Losing the registry must not disconnect players mid-session.
            print(
                f"[ChatService] session lookup failed for "
                f"{hello.session_id.hex()}: {type(exc).__name__}: {exc}; "
                "accepting"
            )
            return None
        if record is None:
            self._audit(
                s, 'rejected',
                f'user_id={hello.user_id} session={hello.session_id.hex()} '
                'unknown-or-expired',
            )
            self.drop_session(
                s,
                reason=(
                    f"chat login for user {hello.user_id} presented a session "
                    "that is unknown, expired, or revoked"
                ),
            )
            return False
        # An account with no character owns none: it must not chat as any
        # character id it names (an admin's included). The list is read live
        # from account_characters, so a character made after login counts.
        if hello.user_id not in record.character_ids:
            self._audit(
                s, 'rejected',
                f'user_id={hello.user_id} session={hello.session_id.hex()} '
                f'account={record.account_id} character-not-owned',
            )
            self.drop_session(
                s,
                reason=(
                    f"account {record.account_id} ({record.username!r}) "
                    f"claimed character {hello.user_id}, which it does not own"
                ),
            )
            return False
        s.chat_account_id = record.account_id
        return True

    def _on_hello(self, s: Session, payload: bytes):
        try:
            hello = decode_chat_client_hello(payload)
            if hello.service_name != CHAT_CLIENT_SERVICE:
                raise ValueError(
                    f"expected service {CHAT_CLIENT_SERVICE!r}, "
                    f"got {hello.service_name!r}"
                )
            if hello.user_id == 0:
                raise ValueError('chat hello contains user id zero')
            if len(hello.session_id) != 16:
                raise ValueError('chat hello session id is not 16 bytes')
        except ValueError as exc:
            print(f"[ChatService] rejected malformed ChatClient hello: {exc}")
            return

        authenticated = self._authenticate_hello(s, hello)
        if authenticated is False:
            return

        s.chat_user_id = hello.user_id
        s.chat_session_id = hello.session_id
        character = self._character_for_user_id(hello.user_id)
        s.chat_user_name = (
            str(character.name) if character is not None else self.user_name
        )
        s.chat_admin_name_color, s.chat_is_admin = self._admin_flags(character)
        self._audit(
            s, 'login',
            f'user_id={hello.user_id} session={hello.session_id.hex()}'
            + (
                f' account={s.chat_account_id}'
                if s.chat_account_id is not None
                else ' account=unverified'
            ),
        )
        s.send_reliable_ordered(
            b'\x88',
            track=True,
            label='chat-login-accepted',
        )
        timer = threading.Timer(
            max(0, CHAT_CHANNEL_JOIN_DELAY_SECONDS),
            self._send_channel_memberships,
            args=(s,),
        )
        timer.daemon = True
        s.chat_membership_timer = timer
        timer.start()
        print(
            f"[ChatService] accepted ChatClient user={hello.user_id} "
            f"session={hello.session_id.hex()}"
        )

    def _send_channel_memberships(self, s: Session):
        active = self.is_session_current(s)
        with s.flow_lock:
            s.chat_membership_timer = None
            if not active or s.chat_user_id is None:
                return
        map_room = f'Map__{self.instance_id}_{self.map_room_id}'
        region_id = self._chat_region_id_for_user(s.chat_user_id)
        region_room = f'Region__{self.instance_id}_{region_id}'
        social_rooms = self._desired_social_chat_rooms(s.chat_user_id)
        memberships = (
            encode_chat_text(f'e#{map_room}'),
            encode_chat_text(f'e#{region_room}'),
            *(
                encode_chat_text(f'e#{room}')
                for room in social_rooms.values()
            ),
        )
        s.send_multi_reliable_ordered(
            *memberships,
            track=True,
            label='chat-channel-memberships',
        )
        with s.flow_lock:
            s.chat_joined_channels.update(('map', 'region'))
            s.chat_joined_channels.update(social_rooms)
            s.chat_social_rooms = social_rooms
        self._audit(
            s,
            'join',
            " ".join((map_room, region_room, *social_rooms.values())),
        )

    def _on_char_op(self, s: Session, payload: bytes):
        if s.chat_user_id is None:
            print('[ChatService] ignored chat command before login')
            self._audit(s, 'rejected', f'before-login raw={payload.hex()}')
            return
        try:
            command = decode_chat_text(payload, from_client=True)
            if command.startswith('sw#'):
                whisper = decode_whisper_chat_message(command)
                target, whisper_text = self._resolve_whisper_target(
                    s, whisper.target_name, whisper.text
                )
                if target is None:
                    raise ValueError(
                        f'whisper target {whisper.target_name!r} is not online'
                    )
                whisper = replace(
                    whisper,
                    user_type=(
                        'hb' if s.chat_admin_name_color else 'ha'
                    ),
                    text=whisper_text,
                )
                response = encode_chat_text(
                    encode_received_whisper(whisper, s.chat_user_name)
                )
                self._audit(s, 'received', command)
                target.send_reliable_ordered(
                    response,
                    track=True,
                    label='chat-whisper',
                )
                self._audit(
                    target, 'sent',
                    decode_chat_text(response, from_client=False),
                )
                print(
                    f"[ChatService] whisper {s.chat_user_name} -> "
                    f"{target.chat_user_name}: {whisper.text}"
                )
                return

            message = decode_map_chat_message(command)
            with s.flow_lock:
                joined = message.channel.lower() in s.chat_joined_channels
            if not joined:
                raise ValueError(
                    f'client sent to channel {message.channel!r} before join'
                )
            if message.text.lstrip().startswith("!"):
                try:
                    self._handle_admin_command(
                        s, message.text.lstrip()
                    )
                    self._audit(s, "admin-command", message.text)
                except (OSError, TypeError, ValueError) as exc:
                    self._send_system_message(s, f"Command failed: {exc}")
                    self._audit(
                        s, "admin-command-failed", str(exc)
                    )
                return
            message = replace(
                message,
                user_type='hb' if s.chat_admin_name_color else 'ha',
            )
            response = encode_chat_text(
                encode_received_map_chat(message, s.chat_user_name)
            )
        except ValueError as exc:
            print(f"[ChatService] ignored unsupported chat command: {exc}")
            self._audit(s, 'rejected', f'{exc}; raw={payload.hex()}')
            return
        self._audit(s, 'received', command)
        recipients = self._channel_recipients(s, message.channel)
        for recipient in recipients:
            recipient.send_reliable_ordered(
                response,
                track=True,
                label='chat-message',
            )
            self._audit(
                recipient, 'sent',
                decode_chat_text(response, from_client=False),
            )
        print(
            f"[ChatService] {s.chat_user_name} -> "
            f"{message.channel}: {message.text}"
        )
