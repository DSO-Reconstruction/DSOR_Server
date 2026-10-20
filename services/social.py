"""Player queries, guilds, groups, invitations, and group finder."""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session


class SocialService:
    """Player queries, guilds, groups, invitations, and group finder."""

    def _character_for_session(self, s: Session):
        character = s.gameplay_character
        if character is None and hasattr(self, "character_selection"):
            character = self.character_selection.active_character
        if character is None:
            raise ValueError("session has no selected character")
        return character

    def _social_actor_id_for(
            self, viewer_character_id: int,
            member_character_id: int) -> int:
        """Return the member's actor id in one viewer's local scene.

        The second id in GuildMember and fourth id in PlayerSummary are
        runtime actor ids, despite their earlier ``account_id`` label. A
        zero value means the member is not present in this viewer's map.
        """

        viewer_character_id = int(viewer_character_id)
        member_character_id = int(member_character_id)
        if viewer_character_id == member_character_id:
            return LOCAL_PLAYER_ACTOR_ID
        viewer = next(
            (
                session
                for session in tuple(self.sessions.values())
                if (
                    session.connected
                    and session.gameplay_mode
                    and not session.gameplay_presence_departed
                    and session.gameplay_character is not None
                    and int(session.gameplay_character.character_id)
                    == viewer_character_id
                )
            ),
            None,
        )
        member = next(
            (
                session
                for session in self._active_gameplay_sessions()
                if int(session.gameplay_character.character_id)
                == member_character_id
            ),
            None,
        )
        if (
            viewer is None
            or member is None
            or CharacterService._gameplay_instance_key(viewer)
            != CharacterService._gameplay_instance_key(member)
        ):
            return 0
        return int(
            member.gameplay_remote_actor_id
            or CharacterService._remote_actor_id(member_character_id)
        )

    def _player_summary(
            self, character, *, map_or_guild: str,
            online: bool = True, actor_id: int = 0,
            role: int = 0xFF,
            timestamp: str = "0000-00-00 00:00:00") -> PlayerSummary:
        return PlayerSummary(
            0,
            0,
            int(character.character_id),
            int(actor_id),
            str(character.name),
            str(map_or_guild),
            int(character.class_id),
            max(1, int(character.level)),
            200,
            0,
            0,
            bool(online),
            bool(online),
            bool(online),
            int(role),
            timestamp=str(timestamp),
        )

    def _social_member(self, s: Session):
        character = self._character_for_session(s)
        return make_social_member(
            character.character_id,
            character.name,
            character.class_id,
            character.level,
        )

    def _refresh_social_chat_memberships(
            self, *character_ids: int) -> None:
        callback = getattr(self, "social_chat_callback", None)
        if callback is not None:
            callback(tuple(int(value) for value in character_ids))

    def _broadcast_social_status(
            self, kind: str, collective_id: int) -> None:
        if kind == "group":
            lookup = self.social_store.group_for
            status_getter = self._group_status_for
            status_encoder = encode_group_status
            id_field = "group_id"
        elif kind == "guild":
            lookup = self.social_store.guild_for
            status_getter = self._guild_status_for
            status_encoder = encode_guild_status
            id_field = "guild_id"
        else:
            raise ValueError(f"unsupported social collection {kind!r}")
        for member_session in self._active_gameplay_sessions():
            member_character = member_session.gameplay_character
            if member_character is None:
                continue
            collective = lookup(member_character.character_id)
            if (
                collective is None
                or int(getattr(collective, id_field)) != int(collective_id)
            ):
                continue
            member_session.send_reliable_ordered(
                status_encoder(
                    status_getter(member_character.character_id)
                ),
                track=True,
                label=f"{kind}-status-after-invitation",
            )

    def _broadcast_presence_social_status(
            self, character_id: int) -> None:
        """Refresh social rosters after a player enters or leaves a map."""

        group = self.social_store.group_for(character_id)
        if group is not None:
            self._broadcast_social_status("group", group.group_id)
        guild = self.social_store.guild_for(character_id)
        if guild is not None:
            self._broadcast_social_status("guild", guild.guild_id)
        self._broadcast_buddy_presence(character_id)

    def _broadcast_buddy_presence(self, character_id: int) -> None:
        """Send updated friend lists to online characters who have this buddy."""
        character_id = int(character_id)
        for viewer in self._active_gameplay_sessions():
            viewer_character = viewer.gameplay_character
            if viewer_character is None:
                continue
            viewer_id = int(viewer_character.character_id)
            if character_id not in self.social_store.buddies_for(viewer_id):
                continue
            viewer.send_reliable_ordered(
                encode_buddy_status(self._buddy_summaries(viewer_id)),
                track=True,
                label="buddy-presence-status",
            )

    def _buddy_last_online(self, character_id: int) -> str:
        """Format the best saved per-character activity time for the client."""
        store = getattr(self, "account_store", None)
        if store is not None:
            saved_at_reader = getattr(store, "saved_at_for_character", None)
            saved_at = (
                saved_at_reader(character_id) if saved_at_reader is not None
                else getattr(store.get(character_id), "saved_at", "")
            )
            if saved_at:
                try:
                    saved_at = datetime.fromisoformat(str(saved_at))
                    if saved_at.tzinfo is None:
                        saved_at = saved_at.replace(tzinfo=timezone.utc)
                    return saved_at.astimezone(timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                except (TypeError, ValueError, OverflowError):
                    pass
        registry = getattr(self, "account_registry", None)
        if registry is not None:
            owner = registry.character_owner(character_id)
            if owner is not None and owner.last_login_at > 0:
                try:
                    return datetime.fromtimestamp(
                        owner.last_login_at, timezone.utc
                    ).strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, OverflowError, OSError):
                    pass
        return "0000-00-00 00:00:00"

    def _guild_status_for(self, character_id: int) -> SocialGuildStatus:
        guild = self.social_store.guild_for(character_id)
        if guild is None:
            return SocialGuildStatus(
                0, 0, "", int(character_id), False, 0, 0, ()
            )
        online_ids = {
            int(session.gameplay_character.character_id)
            for session in self._active_gameplay_sessions()
        }
        def joined_parts(value: str) -> tuple[int, int, int, int, int, int]:
            try:
                parsed = datetime.fromisoformat(str(value))
            except (TypeError, ValueError):
                parsed = datetime.now(timezone.utc)
            return (
                parsed.year,
                parsed.month,
                parsed.day,
                parsed.hour,
                parsed.minute,
                parsed.second,
            )

        members = tuple(
            SocialGuildMember(
                member.character_id,
                self._social_actor_id_for(
                    character_id, member.character_id
                ),
                member.name,
                member.class_id,
                member.level,
                member.character_id in online_ids,
                # The client reads the second flag (+0x51) for the activity icon.
                member.character_id in online_ids,
                False,
                member.rank,
                joined_parts(member.joined_at),
            )
            for member in guild.members
        )
        leader = next(
            (
                member
                for member in guild.members
                if member.rank == GUILD_RANK_LEADER
            ),
            guild.members[0] if guild.members else None,
        )
        return SocialGuildStatus(
            0,
            guild.guild_id,
            guild.name,
            int(character_id),
            True,
            leader.character_id if leader is not None else 0,
            len(guild.members),
            members,
        )

    def _group_member_summaries(
            self, group: Group, viewer_character_id: int) -> tuple[PlayerSummary, ...]:
        sessions_by_character = {
            int(session.gameplay_character.character_id): session
            for session in self._active_gameplay_sessions()
        }
        store = getattr(self, "account_store", None)
        members = []
        for member in group.members:
            session = sessions_by_character.get(member.character_id)
            profile = (
                store.get_profile(member.character_id)
                if session is None and store is not None else None
            )
            character = (
                session.gameplay_character if session is not None
                else profile if profile is not None else member
            )
            members.append(self._player_summary(
                character,
                map_or_guild=(
                    session.gameplay_map if session is not None
                    else profile.map_id if profile is not None else group.map_id
                ),
                online=session is not None,
                actor_id=self._social_actor_id_for(
                    viewer_character_id, member.character_id,
                ),
                role=0 if member.character_id == group.owner_character_id else 1,
            ))
        return tuple(members)

    def _group_status_for(self, character_id: int) -> GroupStatus:
        group = self.social_store.group_for(character_id)
        if group is None:
            return GroupStatus(
                0, 0, 0, int(character_id), False, (), "", False, ""
            )
        return GroupStatus(
            0,
            group.group_id,
            group.owner_character_id,
            int(character_id),
            group.listed,
            self._group_member_summaries(group, character_id),
            group.name,
            group.private,
            group.description,
        )

    def _handle_buddy_list(self, s: Session, payload: bytes) -> None:
        request = decode_buddy_list_request(payload, s.current_message_bit_length)
        CharacterService._accept_gameplay_state_command(s, BUDDY_LIST_COMMAND)
        owner = self._character_for_session(s)
        owner_id = int(owner.character_id)
        if request.action == 1:
            target = self._resolve_buddy_target(request.name)
            if target is None:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] ignored buddy add for "
                    f"unknown player {request.name!r} from {owner.name!r}"
                )
            elif int(target.character_id) == owner_id:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] ignored self-buddy add "
                    f"from {owner.name!r}"
                )
            else:
                try:
                    self.social_store.add_buddy(
                        owner_id, int(target.character_id)
                    )
                except ValueError as exc:
                    print(
                        f"[{GAMEPLAY_SERVICE_NAME}] ignored buddy add "
                        f"from {owner.name!r} to {target.name!r}: {exc}"
                    )
        elif request.action == 2:
            target = self._resolve_buddy_target(request.name)
            if target is not None:
                self.social_store.remove_buddy(
                    owner_id, int(target.character_id)
                )
        elif request.action != 0:
            raise ValueError(
                f"unsupported buddy-list operation {request.action}"
            )

        members = self._buddy_summaries(owner_id)
        s.send_reliable_ordered(
            encode_buddy_status(members, field_1c=request.field_24),
            track=True, label="buddy-list-status",
        )

    def _resolve_buddy_target(self, name: str):
        wanted = str(name).strip().casefold()
        if not wanted:
            return None
        online = [
            session.gameplay_character
            for session in self._active_gameplay_sessions()
            if session.gameplay_character.name.casefold() == wanted
        ]
        if len(online) == 1:
            return online[0]
        if len(online) > 1:
            return None

        profiles = []
        store = getattr(self, "account_store", None)
        if store is not None:
            profiles.extend(store.profiles())
        selection = getattr(self, "character_selection", None)
        if selection is not None:
            profiles.extend(selection.characters)
        matches = {
            int(character.character_id): character
            for character in profiles
            if str(character.name).casefold() == wanted
        }
        return next(iter(matches.values())) if len(matches) == 1 else None

    def _buddy_summaries(
        self, owner_character_id: int
    ) -> tuple[PlayerSummary, ...]:
        online_by_id = {
            int(session.gameplay_character.character_id): session
            for session in self._active_gameplay_sessions()
        }
        store = getattr(self, "account_store", None)
        selection = getattr(self, "character_selection", None)
        roster = (
            {
                int(character.character_id): character
                for character in selection.characters
            }
            if selection is not None
            else {}
        )
        summaries = []
        for buddy_id in self.social_store.buddies_for(owner_character_id):
            session = online_by_id.get(int(buddy_id))
            character = (
                session.gameplay_character if session is not None
                else store.get_profile(buddy_id) if store is not None else None
            )
            if character is None:
                character = roster.get(int(buddy_id))
            if character is None:
                continue
            summaries.append(self._player_summary(
                character,
                map_or_guild=(session.gameplay_map if session is not None else ""),
                online=session is not None,
                actor_id=self._social_actor_id_for(
                    owner_character_id, buddy_id
                ),
                timestamp=self._buddy_last_online(buddy_id),
            ))
        return tuple(summaries)

    def _handle_player_query(self, s: Session, payload: bytes) -> None:
        request = decode_player_query(payload)
        CharacterService._accept_gameplay_state_command(s, 0x00B4)
        requester_id = int(self._character_for_session(s).character_id)
        results = []
        if request.query_kind == 1:
            # Local-player refresh only includes initialized peers on this map.
            candidates = [
                session.gameplay_character
                for session in self._active_gameplay_sessions(
                    map_id=s.gameplay_map,
                    instance_of=s,
                )
            ]
        else:
            candidates = list(self.character_selection.characters)
        query = request.text.strip().casefold()
        seen = set()
        for character in candidates:
            if (
                character is None
                or int(character.character_id) == requester_id
                or character.character_id in seen
            ):
                continue
            if query and query not in character.name.casefold():
                continue
            seen.add(character.character_id)
            guild = self.social_store.guild_for(character.character_id)
            results.append(
                self._player_summary(
                    character,
                    map_or_guild=guild.name if guild is not None else "",
                    online=any(
                        active.gameplay_character is not None
                        and active.gameplay_character.character_id
                        == character.character_id
                        for active in self._active_gameplay_sessions()
                    ),
                    actor_id=self._social_actor_id_for(
                        requester_id, character.character_id
                    ),
                )
            )
        response = encode_player_query_result(
            request.query_id, tuple(results[:50])
        )
        s.send_reliable_ordered(
            response,
            track=True,
            label='player-query-result',
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] handled PlayerQuery "
            f"kind={request.query_kind} id={request.query_id} "
            f"text={request.text!r}; sent {len(results[:50])} typed result(s)"
        )

    def _handle_guild_command(
            self, s: Session, payload: bytes, command_id: int) -> None:
        character = self._character_for_session(s)
        membership_changed = False
        if command_id == CREATE_GUILD_COMMAND:
            request = decode_foundation_request(payload, command_id)
            try:
                self.social_store.create_guild(
                    request.name, "", self._social_member(s)
                )
                membership_changed = True
                foundation_result = encode_guild_foundation_result(
                    0,
                    server_time() & 0xFFFFFFFF,
                    "",
                    request.name.strip(),
                    False,
                )
                result_label = "create-guild-result"
            except ValueError as exc:
                print(f"[{GAMEPLAY_SERVICE_NAME}] rejected guild creation: {exc}")
                error_text = str(exc)
                if "already in a guild" in error_text:
                    status = 1
                elif "already in use" in error_text:
                    status = 3
                elif "3..32" in error_text:
                    status = 4
                else:
                    status = 4
                foundation_result = encode_guild_foundation_result(
                    status,
                    server_time() & 0xFFFFFFFF,
                    error_text,
                    request.name.strip(),
                    False,
                )
                result_label = "create-guild-rejected"
            s.send_reliable_ordered(
                foundation_result,
                track=True,
                label=result_label,
            )
        elif command_id == GUILD_LEAVE_COMMAND:
            decode_uint32_request(payload, command_id)
            self.social_store.leave_guild(character.character_id)
            membership_changed = True
        elif command_id != GUILD_LIST_REQUEST_COMMAND:
            raise ValueError(f"unsupported guild command 0x{command_id:04x}")
        CharacterService._accept_gameplay_state_command(s, command_id)
        packet = encode_guild_status(
            self._guild_status_for(character.character_id)
        )
        s.send_reliable_ordered(
            packet,
            track=True,
            label="guild-status",
        )
        if membership_changed:
            self._refresh_social_chat_memberships(character.character_id)

    def _handle_group_command(
            self, s: Session, payload: bytes, command_id: int) -> None:
        character = self._character_for_session(s)
        membership_changed = False
        if command_id == CREATE_GROUP_COMMAND:
            request = decode_foundation_request(payload, command_id)
            try:
                self.social_store.create_group(
                    request.name,
                    "",
                    s.gameplay_map or DEFAULT_GAMEPLAY_MAP,
                    self._social_member(s),
                )
                membership_changed = True
                result = encode_create_group_result(
                    0, server_time() & 0xFFFFFFFF
                )
                s.send_reliable_ordered(
                    result,
                    track=True,
                    label="create-group-result",
                )
            except ValueError as exc:
                result = encode_create_group_result(
                    1, server_time() & 0xFFFFFFFF, str(exc)
                )
                s.send_reliable_ordered(
                    result,
                    track=True,
                    label="create-group-rejected",
                )
        elif command_id == GROUP_LEAVE_COMMAND:
            decode_uint32_request(payload, command_id)
            self.social_store.leave_group(character.character_id)
            membership_changed = True
        elif command_id != GROUP_STATUS_REQUEST_COMMAND:
            raise ValueError(f"unsupported group command 0x{command_id:04x}")
        CharacterService._accept_gameplay_state_command(s, command_id)
        status = encode_group_status(
            self._group_status_for(character.character_id)
        )
        s.send_reliable_ordered(
            status,
            track=True,
            label="group-status",
        )
        if membership_changed:
            self._refresh_social_chat_memberships(character.character_id)

    def _gameplay_session_for_character_name(
            self, name: str) -> Session | None:
        wanted = str(name).strip().casefold()
        if not wanted:
            return None
        return next(
            (
                session
                for session in self._active_gameplay_sessions()
                if (
                    session.gameplay_character is not None
                    and session.gameplay_character.name.casefold() == wanted
                )
            ),
            None,
        )

    @staticmethod
    def _send_social_invitation_notification(
            target: Session, *, kind: str, source_character,
            target_character, collective_id: int,
            collective_name: str, source_map: str = "") -> None:
        if kind not in {"group", "guild"}:
            raise ValueError(f"unsupported social invitation kind {kind!r}")
        invitation_id = uuid.uuid4()
        payload = {
            "invitation": kind,
            "invitationId": str(invitation_id),
            "sourceCharacterId": int(source_character.character_id),
            "sourceName": str(source_character.name),
            # ClientSocialNotificationHandler requires playerName for guild
            # invitations. Keep sourceName too for our action path.
            "playerName": str(source_character.name),
            # The group invitation click handler refuses to construct its
            # accept/decline dialog unless this full profile schema is present.
            "playerLevel": max(1, int(source_character.level)),
            "playerClass": int(source_character.class_id),
            "honorPoints": 0,
            "playerGender": int(source_character.gender),
            "titleType": 0,
            "achievementTitle": "",
            "mapName": str(source_map),
            "targetCharacterId": int(target_character.character_id),
            "targetName": str(target_character.name),
            "collectiveId": int(collective_id),
            (
                "groupName" if kind == "group" else "guildName"
            ): str(collective_name),
        }
        notification = make_notification_entry(
            # These ids are the client's NotificationType values and match
            # _Template_Notifications: groupinvite=1, guildinvite=3.
            1 if kind == "group" else 3,
            invitation_id.bytes,
            (
                int(target_character.character_id),
                int(collective_id),
                int(source_character.character_id),
            ),
            payload,
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        )
        packet, packet_bits = encode_server_command_batch(
            (
                ServerCommand(
                    CLIENT_NOTIFICATION_COMMAND,
                    Notification(1, (notification,)),
                    0,
                ),
            )
        )
        if packet_bits > len(packet) * 8:
            raise AssertionError("social notification bit length overflow")
        target.send_reliable_ordered(
            (packet, packet_bits),
            track=True,
            label=f"{kind}-invitation-notification",
        )

    def _handle_social_invite(
            self, s: Session, payload: bytes, command_id: int) -> None:
        source = self._character_for_session(s)
        if command_id == GROUP_INVITE_COMMAND:
            request = decode_group_invite(payload)
            kind = "group"
            response_id = GROUP_INVITATION_RESPONSE_COMMAND
        elif command_id == GUILD_INVITE_COMMAND:
            request = decode_guild_invite(payload)
            kind = "guild"
            response_id = GUILD_INVITATION_RESPONSE_COMMAND
        else:
            raise ValueError(f"unsupported social invite 0x{command_id:04x}")

        CharacterService._accept_gameplay_state_command(s, command_id)
        target_session = CharacterService._gameplay_session_for_character_name(
            self, request.target_name
        )
        failure_status = None
        collective_id = 0
        collective_name = ""
        if target_session is None:
            failure_status = 4  # unknownPlayer
        elif target_session is s:
            failure_status = 2  # already in the requested social collection
        elif kind == "group":
            group = self.social_store.group_for(source.character_id)
            target_group = self.social_store.group_for(
                target_session.gameplay_character.character_id
            )
            # Validate the recipient before implicitly creating a group for
            # the inviter. Previously a rejected invite left a new singleton
            # group behind, causing every later invitation to be rejected.
            if target_group is not None:
                failure_status = 2
            elif (
                group is not None
                and len(group.members) >= GROUP_MEMBER_LIMIT
            ):
                # Refuse before the notification is delivered. Without this a
                # full group keeps issuing invitations that can only ever fail
                # on acceptance.
                s.send_reliable_ordered(
                    encode_grouping_failed(
                        GROUPING_FAILED_GROUP_IS_FULL,
                        request.target_name,
                        server_time() & 0xFFFFFFFF,
                    ),
                    track=True,
                    label="grouping-failed-invite",
                )
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] refused group invitation "
                    f"from {source.name!r}: group {group.group_id} already "
                    f"holds {GROUP_MEMBER_LIMIT} members"
                )
                return
            elif group is None:
                try:
                    group = self.social_store.create_group(
                        f"{source.name}'s group",
                        "",
                        s.gameplay_map or DEFAULT_GAMEPLAY_MAP,
                        self._social_member(s),
                    )
                    s.send_reliable_ordered(
                        encode_group_status(
                            self._group_status_for(source.character_id)
                        ),
                        track=True,
                        label="implicit-group-status",
                    )
                    self._refresh_social_chat_memberships(
                        source.character_id
                    )
                except ValueError:
                    failure_status = 5  # leaderRightsRequired
            if group is not None:
                collective_id = group.group_id
                collective_name = group.name
        else:
            guild = self.social_store.guild_for(source.character_id)
            if guild is None:
                failure_status = 6  # noGuildFounded
            else:
                collective_id = guild.guild_id
                collective_name = guild.name
                source_member = next(
                    (
                        member for member in guild.members
                        if member.character_id == source.character_id
                    ),
                    None,
                )
                if source_member is None or not (
                    source_member.can_invite
                    or source_member.rank in {
                        GUILD_RANK_LEADER,
                        GUILD_RANK_OFFICER,
                    }
                ):
                    failure_status = 5  # guildOfficerRankRequired
                elif self.social_store.guild_for(
                    target_session.gameplay_character.character_id
                ) is not None:
                    failure_status = 2

        if failure_status is not None:
            target_character_id = (
                int(target_session.gameplay_character.character_id)
                if (
                    target_session is not None
                    and target_session.gameplay_character is not None
                )
                else 0
            )
            packet = encode_invitation_response(
                response_id,
                failure_status,
                request.target_name,
                target_character_id,
                collective_id,
                LOCAL_PLAYER_ACTOR_ID,
            )
            s.send_reliable_ordered(
                packet,
                track=True,
                label=f"{kind}-invitation-rejected",
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected {kind} invitation "
                f"from {source.name!r} to {request.target_name!r}; "
                f"status={failure_status}"
            )
            return

        target_character = target_session.gameplay_character
        CharacterService._send_social_invitation_notification(
            target_session,
            kind=kind,
            source_character=source,
            target_character=target_character,
            collective_id=collective_id,
            collective_name=collective_name,
            source_map=s.gameplay_map or DEFAULT_GAMEPLAY_MAP,
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] delivered {kind} invitation "
            f"from {source.name!r} to {target_character.name!r} "
            f"for id {collective_id}"
        )

    def _handle_invitation_response(
            self, s: Session, payload: bytes, command_id: int) -> None:
        request = decode_invitation_response(payload, command_id)
        target = self._character_for_session(s)
        if request.player_name.casefold() != target.name.casefold():
            raise ValueError(
                "invitation response player does not match the "
                "authenticated character"
            )
        if command_id == GROUP_INVITATION_RESPONSE_COMMAND:
            kind = "group"
            lookup = self.social_store.group_for
            add_member = self.social_store.add_group_member
            id_field = "group_id"
        elif command_id == GUILD_INVITATION_RESPONSE_COMMAND:
            kind = "guild"
            lookup = self.social_store.guild_for
            add_member = self.social_store.add_guild_member
            id_field = "guild_id"
        else:
            raise ValueError(
                f"unsupported invitation response 0x{command_id:04x}"
            )

        CharacterService._accept_gameplay_state_command(s, command_id)
        response_status = int(request.status)
        if response_status not in {0, 1}:
            raise ValueError(
                f"client sent invalid {kind} invitation response "
                f"{response_status}"
            )

        # An invitation is an asynchronous notification, so the collection can
        # change between sending it and the player pressing accept. The
        # inviter's GroupInvitationResponse does not reach the accepting
        # client, so a group failure is reported to them with
        # GroupingFailedCommand instead.
        grouping_failure = None
        if response_status == 0:
            source_collective = lookup(request.inviter_character_id)
            if (
                source_collective is None
                or int(getattr(source_collective, id_field))
                != int(request.collective_id)
            ):
                response_status = 4
                grouping_failure = GROUPING_FAILED_NO_GROUP
            else:
                target_collective = lookup(target.character_id)
                if target_collective is None:
                    try:
                        add_member(
                            request.collective_id,
                            request.inviter_character_id,
                            self._social_member(s),
                        )
                    except GroupFullError as exc:
                        print(
                            f"[{GAMEPLAY_SERVICE_NAME}] rejected {kind} "
                            f"acceptance into a full group: {exc}"
                        )
                        response_status = 2
                        grouping_failure = GROUPING_FAILED_GROUP_IS_FULL
                    except ValueError as exc:
                        print(
                            f"[{GAMEPLAY_SERVICE_NAME}] rejected stale "
                            f"{kind} acceptance: {exc}"
                        )
                        response_status = 2
                        grouping_failure = GROUPING_FAILED_REQUEST_EXPIRED
                elif (
                    int(getattr(target_collective, id_field))
                    != int(request.collective_id)
                ):
                    response_status = 2
                    grouping_failure = GROUPING_FAILED_ALREADY_IN_A_GROUP

        if kind == "group" and grouping_failure is not None:
            s.send_reliable_ordered(
                encode_grouping_failed(
                    grouping_failure,
                    target.name,
                    server_time() & 0xFFFFFFFF,
                ),
                track=True,
                label="grouping-failed",
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] sent GroupingFailed reason "
                f"{grouping_failure} to {target.name!r} for group "
                f"{request.collective_id}"
            )

        source_session = next(
            (
                session
                for session in self._active_gameplay_sessions()
                if (
                    session.gameplay_character is not None
                    and int(session.gameplay_character.character_id)
                    == int(request.inviter_character_id)
                )
            ),
            None,
        )
        if response_status == 0:
            self._broadcast_social_status(kind, request.collective_id)
            self._refresh_social_chat_memberships(
                request.inviter_character_id,
                target.character_id,
            )
        if source_session is not None:
            source_session.send_reliable_ordered(
                encode_invitation_response(
                    command_id,
                    response_status,
                    target.name,
                    int(target.character_id),
                    int(request.collective_id),
                    LOCAL_PLAYER_ACTOR_ID,
                ),
                track=True,
                label=f"{kind}-invitation-response",
            )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] handled {kind} invitation "
            f"response from {target.name!r}; inviter="
            f"{request.inviter_character_id}, collective="
            f"{request.collective_id}, status={response_status}"
        )

    def _handle_group_finder(self, s: Session, payload: bytes) -> None:
        decode_group_finder_query(payload)
        groups = self.social_store.groups_in_map(
            s.gameplay_map or DEFAULT_GAMEPLAY_MAP
        )
        entries = tuple(
            GroupFinderEntry(
                group.group_id,
                0,
                group.name,
                group.map_id,
                len(group.members),
                0,
            )
            for group in groups
        )
        packet = encode_group_finder_result(
            server_time() & 0xFFFFFFFF, entries
        )
        s.send_reliable_ordered(
            packet,
            track=True,
            label="group-finder-result",
        )
        CharacterService._accept_gameplay_state_command(
            s, GROUP_FINDER_QUERY_COMMAND
        )

    def _handle_group_member_query(self, s: Session, payload: bytes) -> None:
        """Inspect the group selected in the finder without changing membership."""

        request = decode_group_member_query(payload, s.current_message_bit_length)
        CharacterService._accept_gameplay_state_command(s, GROUP_MEMBER_QUERY_COMMAND)
        character_id = int(self._character_for_session(s).character_id)
        group = self.social_store.group_by_id(request.group_id)
        if group is not None:
            belongs = any(member.character_id == character_id for member in group.members)
            # Members can inspect their own unlisted/off-map party. Other
            # players can inspect the same groups exposed by the local finder.
            if not belongs and (
                not group.listed
                or group.map_id != (s.gameplay_map or DEFAULT_GAMEPLAY_MAP)
            ):
                group = None
        members = self._group_member_summaries(group, character_id) if group else ()
        status = GROUP_MEMBER_QUERY_OK if members else GROUP_MEMBER_QUERY_FAILED
        s.send_reliable_ordered(
            encode_group_member_query_result(
                request.group_id, members, status=status, field_1c=request.field_20,
            ),
            track=True, label="group-member-query-result",
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] handled GroupMemberQuery for selected group "
            f"{request.group_id}: status={status}, sent {len(members)} member(s)"
        )
