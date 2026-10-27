"""Player queries, guilds, groups, invitations, and group finder."""

from __future__ import annotations

from dataclasses import replace as _replace_dataclass
from functools import lru_cache

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.persistence.readonly_db import connect as _readonly_connect
from dso_server.persistence.social_state import (
    InvitationMissingError,
    InvitationPendingError,
    SocialContactBlockedError,
    SocialNameError,
    SocialNameTakenError,
    SocialNoChangeError,
    SocialPermissionError,
)
from dso_server.protocol.social_protocol import (
    GROUP_CANDIDATES_COMMAND,
    GROUP_EDIT_COMMAND,
    GROUP_EDIT_INVALID_NAME,
    GROUP_EDIT_OK,
    GROUP_JOIN_ACCEPTED,
    GROUP_JOIN_ALREADY_IN_GROUP,
    GROUP_JOIN_FAILED,
    GROUP_JOIN_LEADER_OFFLINE,
    GROUP_JOIN_NO_GROUP,
    GROUP_JOIN_NOT_LEADER,
    GROUP_JOIN_PENDING,
    GROUP_JOIN_REFUSED,
    GROUP_JOIN_REQUEST_COMMAND,
    GROUP_JOIN_RESPONSE_COMMAND,
    GROUP_KICK_COMMAND,
    GROUP_MULTIPLE_INVITE_COMMAND,
    GROUP_MULTIPLE_INVITE_LIMIT,
    GUILD_KICK_COMMAND,
    GUILD_MOTD_CHANGE_COMMAND,
    GUILD_MOTD_REQUEST_COMMAND,
    GUILD_NAME_EDIT_COMMAND,
    GUILD_RENAME_NAME_INVALID,
    GUILD_RENAME_NAME_TAKEN,
    GUILD_RENAME_NO_CHANGE,
    GUILD_RENAME_NOT_ENOUGH_FUNDS,
    GUILD_RENAME_OK,
    IGNORED_LIST_ADD,
    IGNORED_LIST_COMMAND,
    IGNORED_LIST_REMOVE,
    decode_social_management,
    encode_group_candidates,
    encode_group_edit_result,
    encode_group_join_response,
    encode_guild_motd,
    encode_guild_name_edit_result,
    encode_ignored_status,
    encode_social_kick_notice,
)


# ClientSocialNotificationHandler (0x140211d7c) dispatches notification
# type 1 to the group invitation dialog, 2 to the group join request dialog
# (``confirmJoinRequestGroup``) and 3 to the guild invitation dialog.
_SOCIAL_NOTIFICATION_TYPES = {"group": 1, "group-request": 2, "guild": 3}
# GroupInvitationResponseCommand validates its status as -1..8 and
# GuildInvitationResponseCommand as -1..7 (generated Decode readers).
_INVITATION_RESPONSE_MAX_STATUS = {"group": 8, "guild": 7}
# Shared inviter-side statuses of both invitation responses (handlers
# 0x140278154/0x14027b074): joined, did-not-join, already a member, not
# logged in, unknown player, rights required; 8 is group ``groupRequestPending``.
_INVITE_JOINED = 0
_INVITE_DECLINED = 1
_INVITE_ALREADY_MEMBER = 2
_INVITE_NOT_LOGGED_IN = 3
_INVITE_UNKNOWN_PLAYER = 4
_INVITE_RIGHTS_REQUIRED = 5
_INVITE_GROUP_PENDING = 8
# Local choice: candidate lists answer at most as many rows as PlayerQuery.
_GROUP_CANDIDATE_LIMIT = 50


class _InsufficientSocialFunds(Exception):
    """The wallet cannot pay an authored social fee."""


_GUILD_FEE_COLUMNS = {
    "foundation": ("GuildFoundationRCCost", "GuildFoundationVCCost"),
    "rename": ("GuildEditNameRCCost", "GuildEditNameVCCost"),
}


@lru_cache(maxsize=8)
def _guild_fee_prices(database_path: str, operation: str) -> tuple[int, int]:
    """Return ``(Andermant, gold copper)`` for founding or renaming a guild.

    Read from ``_Globals.GuildFoundationRCCost``/``GuildFoundationVCCost``
    and ``GuildEditNameRCCost``/``GuildEditNameVCCost``.  FoundGuildDialog
    offers exactly these two payment buttons in both modes
    (``gui.window.guildfoundation/detailsLabel`` and ``editDetailsLabel``):
    the silver button sends the command's bool False, the Andermant button
    True after its confirmation (0x14042f370/0x14042f84c).
    """

    andermant_column, gold_column = _GUILD_FEE_COLUMNS[operation]
    with _readonly_connect(database_path) as database:
        row = database.execute(
            f"SELECT {andermant_column}, {gold_column} FROM _Globals LIMIT 1"
        ).fetchone()
    if row is None or any(value is None or int(value) < 0 for value in row):
        raise ValueError(f"invalid guild {operation} prices in _Globals")
    return int(row[0]), int(row[1])


class SocialService:
    """Player queries, guilds, groups, invitations, and group finder."""

    def _handle_social_management(
            self, s: Session, payload: bytes, command_id: int) -> None:
        """Route one group/guild management or ignore-list request."""

        request = decode_social_management(
            payload, command_id, s.current_message_bit_length
        )
        CharacterService._accept_gameplay_state_command(s, command_id)
        if command_id == GROUP_KICK_COMMAND:
            self._handle_social_kick(s, request, "group")
        elif command_id == GUILD_KICK_COMMAND:
            self._handle_social_kick(s, request, "guild")
        elif command_id == GROUP_EDIT_COMMAND:
            self._handle_group_edit(s, request)
        elif command_id == GUILD_NAME_EDIT_COMMAND:
            self._handle_guild_name_edit(s, request)
        elif command_id == GUILD_MOTD_REQUEST_COMMAND:
            self._handle_guild_motd_request(s, request)
        elif command_id == GUILD_MOTD_CHANGE_COMMAND:
            self._handle_guild_motd_change(s, request)
        elif command_id == IGNORED_LIST_COMMAND:
            self._handle_ignored_list(s, request)
        elif command_id == GROUP_JOIN_REQUEST_COMMAND:
            self._handle_group_join_request(s, request)
        elif command_id == GROUP_JOIN_RESPONSE_COMMAND:
            self._handle_group_join_response(s, request)
        elif command_id == GROUP_MULTIPLE_INVITE_COMMAND:
            self._handle_group_multiple_invite(s, request)
        elif command_id == GROUP_CANDIDATES_COMMAND:
            self._handle_group_candidates(s, request)
        else:
            raise ValueError(
                f"unsupported social management command 0x{command_id:04x}"
            )

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
            if kind == "guild":
                self._achievement_feed(
                    member_session, "guildmembers", ("guild",),
                    progress=len(collective.members),
                )
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

    def _ignored_names_for(self, character_id: int) -> tuple[str, ...]:
        """Current display names of the characters one owner ignores.

        The store keeps the name seen when the ignore was added; a saved or
        live profile wins so a CMS rename shows up in the social window.
        """

        store = getattr(self, "account_store", None)
        names = []
        for ignored_id, saved_name in self.social_store.ignored_for(character_id):
            name = saved_name
            session = SocialService._social_session_for_character(self, ignored_id)
            if session is not None:
                name = str(session.gameplay_character.name)
            elif store is not None:
                try:
                    profile = store.get_profile(ignored_id)
                except (OSError, ValueError, KeyError):
                    profile = None
                if profile is not None and getattr(profile, "name", ""):
                    name = str(profile.name)
            names.append(name)
        return tuple(sorted(names, key=str.casefold))

    def _ignored_status_packet(
            self, character_id: int, field_1c: int | None = None):
        """Standalone IgnoredStatus full-list snapshot for one character."""

        return encode_ignored_status(
            SocialService._ignored_names_for(self, character_id),
            int(character_id) if field_1c is None else int(field_1c),
        )

    def _guild_status_for(self, character_id: int) -> SocialGuildStatus:
        # GuildStatus is always paired with IgnoredStatus type 0, which
        # replaces the client's whole ignore list; it must carry the
        # receiver's current list or every guild refresh would clear it.
        ignored_names = SocialService._ignored_names_for(self, character_id)
        guild = self.social_store.guild_for(character_id)
        if guild is None:
            return SocialGuildStatus(
                0, 0, "", int(character_id), False, 0, 0, (), ignored_names
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
            ignored_names,
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
            # Per-receiver leader rights, not the finder listing: a hidden
            # group must not strip its leader of the edit/kick controls.
            int(group.owner_character_id) == int(character_id),
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
            status, error_text = SocialService._found_guild(self, s, request)
            membership_changed = status == 0
            foundation_result = encode_guild_foundation_result(
                status,
                server_time() & 0xFFFFFFFF,
                error_text,
                request.name.strip(),
                False,
            )
            s.send_reliable_ordered(
                foundation_result,
                track=True,
                label=(
                    "create-guild-result" if status == 0
                    else "create-guild-rejected"
                ),
            )
        elif command_id == GUILD_LEAVE_COMMAND:
            decode_uint32_request(payload, command_id)
            remaining = self.social_store.leave_guild(character.character_id)
            membership_changed = True
            if remaining is not None:
                # Remaining members see the departure (and a promoted leader).
                self._broadcast_social_status("guild", remaining.guild_id)
            SocialService._refresh_guild_overheads(
                self, (int(character.character_id),)
            )
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

    def _guild_fee(self, operation: str, andermant: bool) -> tuple[int, int]:
        """Return the ``(Andermant, gold)`` debit for the chosen button."""

        andermant_price, gold_price = _guild_fee_prices(
            str(getattr(self, "static_database_path", DEFAULT_STATIC_DB_PATH)),
            operation,
        )
        return (andermant_price, 0) if andermant else (0, gold_price)

    def _found_guild(self, s: Session, request) -> tuple[int, str]:
        """Charge the authored foundation fee and found the guild.

        Returns the GuildFoundationResponse status (validated -1..4 by
        HandleGuildFoundationResponseCommand 0x14027a880: 0 guildFounded,
        1 isAlreadyInAGuild, 2 notEnoughFundsForFoundingGuild,
        3 guildNameAlreadyAssigned, 4 guildNameInvalid) and an error text.
        The fee is debited and saved before the guild is created and
        refunded if the creation then loses a race.
        """

        def status_for(exc: ValueError) -> int:
            error_text = str(exc)
            if "already in a guild" in error_text:
                return 1
            if "already in use" in error_text:
                return 3
            return 4

        character_id = int(self._character_for_session(s).character_id)
        try:
            self.social_store.validate_guild_foundation(character_id, request.name)
        except ValueError as exc:
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected guild creation: {exc}")
            return status_for(exc), str(exc)
        andermant, gold = SocialService._guild_fee(
            self, "foundation", bool(request.field_a8)
        )
        try:
            with s.flow_lock:
                charged = SocialService._charge_social_fee(
                    self, s, andermant=andermant, gold=gold
                )
        except _InsufficientSocialFunds as exc:
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected guild creation: {exc}")
            return 2, str(exc)
        if charged is not None:
            CharacterService._persist_gameplay_session(
                self, s, reason="guild foundation fee", flush=True
            )
        try:
            self.social_store.create_guild(request.name, "", self._social_member(s))
        except ValueError as exc:
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected guild creation: {exc}")
            if charged is not None:
                refunded = SocialService._refund_social_fee(
                    self, s, andermant=andermant, gold=gold
                )
                CharacterService._persist_gameplay_session(
                    self, s, reason="guild foundation refund", flush=True
                )
                SocialService._send_social_currency(
                    self, s, refunded, label="guild-foundation-refund"
                )
            return status_for(exc), str(exc)
        SocialService._send_social_currency(
            self, s, charged, label="guild-foundation-fee"
        )
        return 0, ""

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
            remaining = self.social_store.leave_group(character.character_id)
            membership_changed = True
            if remaining is not None:
                # The successor learns its leader rights from this refresh.
                self._broadcast_social_status("group", remaining.group_id)
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
            collective_name: str, source_map: str = "",
            invitation_id: uuid.UUID | None = None,
            honor_points: int = 0) -> uuid.UUID:
        """Deliver one group/guild invitation or group join request.

        ``invitation_id`` is the token recorded in the social store's
        invitation ledger.  A join request (``group-request``) goes to the
        group leader; its dialog (0x14021055c) reads the requester profile
        keys below and AcceptGroupJoinRequest echoes ``timeStamp`` back in
        GroupJoinRequestResponseCommand, so the token travels there.
        """

        if kind not in _SOCIAL_NOTIFICATION_TYPES:
            raise ValueError(f"unsupported social invitation kind {kind!r}")
        invitation_id = invitation_id or uuid.uuid4()
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
            "honorPoints": max(0, int(honor_points)),
            "playerGender": int(getattr(source_character, "gender", 0) or 0),
            "titleType": 0,
            "achievementTitle": "",
            "mapName": str(source_map),
            "targetCharacterId": int(target_character.character_id),
            "targetName": str(target_character.name),
            "collectiveId": int(collective_id),
            (
                "guildName" if kind == "guild" else "groupName"
            ): str(collective_name),
        }
        if kind == "group-request":
            payload["timeStamp"] = str(invitation_id)
        notification = make_notification_entry(
            # These ids are the client's NotificationType values:
            # groupinvite=1, group join request=2, guildinvite=3.  The
            # handler reads the second value as the collection id and the
            # third as the sender.
            _SOCIAL_NOTIFICATION_TYPES[kind],
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
        return invitation_id

    def _handle_social_invite(
            self, s: Session, payload: bytes, command_id: int) -> None:
        source = self._character_for_session(s)
        if command_id == GROUP_INVITE_COMMAND:
            request = decode_group_invite(payload)
            kind = "group"
        elif command_id == GUILD_INVITE_COMMAND:
            request = decode_guild_invite(payload)
            kind = "guild"
        else:
            raise ValueError(f"unsupported social invite 0x{command_id:04x}")

        CharacterService._accept_gameplay_state_command(s, command_id)
        SocialService._invite_to_social_collection(
            self, s, source, kind, request.target_name
        )

    def _invite_to_social_collection(
            self, s: Session, source, kind: str, target_name: str) -> bool:
        """Validate, record and deliver one group/guild invitation.

        The invitation is recorded in the social store's ledger before the
        notification leaves, so only an answer from the invited character
        for exactly this inviter and collection can later admit anyone.
        Returns whether a notification was delivered.
        """

        response_id = (
            GROUP_INVITATION_RESPONSE_COMMAND if kind == "group"
            else GUILD_INVITATION_RESPONSE_COMMAND
        )
        source_id = int(source.character_id)
        target_session = CharacterService._gameplay_session_for_character_name(
            self, target_name
        )
        failure_status = None
        collective_id = 0
        collective_name = ""
        if target_session is None:
            # isNotLoggedIn for a known character, unknownPlayer otherwise.
            failure_status = (
                _INVITE_NOT_LOGGED_IN
                if SocialService._resolve_buddy_target(self, target_name)
                is not None
                else _INVITE_UNKNOWN_PLAYER
            )
        elif target_session is s:
            failure_status = _INVITE_ALREADY_MEMBER
        elif self.social_store.social_contact_blocked(
            source_id, int(target_session.gameplay_character.character_id)
        ):
            # Local choice: an ignore in either direction answers like an
            # immediate refusal (the client's own auto-refusals look the
            # same) and never shows the ignored player's invitation.
            failure_status = _INVITE_DECLINED
        elif kind == "group":
            group = self.social_store.group_for(source_id)
            target_group = self.social_store.group_for(
                target_session.gameplay_character.character_id
            )
            # Validate the recipient before implicitly creating a group for
            # the inviter. Previously a rejected invite left a new singleton
            # group behind, causing every later invitation to be rejected.
            # AcceptGroupInvite (0x14026ea94) itself still accepts while the
            # recipient's own group holds only them.
            if target_group is not None and len(target_group.members) > 1:
                failure_status = _INVITE_ALREADY_MEMBER
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
                        target_name,
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
                return False
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
                            self._group_status_for(source_id)
                        ),
                        track=True,
                        label="implicit-group-status",
                    )
                    self._refresh_social_chat_memberships(source_id)
                except ValueError:
                    failure_status = _INVITE_RIGHTS_REQUIRED
            if group is not None:
                collective_id = group.group_id
                collective_name = group.name
        else:
            guild = self.social_store.guild_for(source_id)
            if guild is None:
                failure_status = 6  # noGuildFounded
            else:
                collective_id = guild.guild_id
                collective_name = guild.name
                source_member = next(
                    (
                        member for member in guild.members
                        if member.character_id == source_id
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
                    failure_status = _INVITE_ALREADY_MEMBER

        invitation_id = None
        if failure_status is None:
            invitation_id = uuid.uuid4()
            try:
                self.social_store.issue_invitation(
                    kind,
                    source_id,
                    int(target_session.gameplay_character.character_id),
                    collective_id,
                    str(invitation_id),
                )
            except InvitationPendingError:
                if kind == "guild":
                    # The guild dialog has no pending answer; the original
                    # notification is still on the recipient's screen.
                    print(
                        f"[{GAMEPLAY_SERVICE_NAME}] guild invitation from "
                        f"{source.name!r} to {target_name!r} is already pending"
                    )
                    return False
                failure_status = _INVITE_GROUP_PENDING
            except GroupFullError:
                s.send_reliable_ordered(
                    encode_grouping_failed(
                        GROUPING_FAILED_GROUP_IS_FULL,
                        target_name,
                        server_time() & 0xFFFFFFFF,
                    ),
                    track=True,
                    label="grouping-failed-invite",
                )
                return False
            except SocialContactBlockedError:
                failure_status = _INVITE_DECLINED
            except SocialPermissionError:
                failure_status = _INVITE_RIGHTS_REQUIRED
            except ValueError:
                failure_status = _INVITE_ALREADY_MEMBER

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
                target_name,
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
                f"from {source.name!r} to {target_name!r}; "
                f"status={failure_status}"
            )
            return False

        target_character = target_session.gameplay_character
        CharacterService._send_social_invitation_notification(
            target_session,
            kind=kind,
            source_character=source,
            target_character=target_character,
            collective_id=collective_id,
            collective_name=collective_name,
            source_map=s.gameplay_map or DEFAULT_GAMEPLAY_MAP,
            invitation_id=invitation_id,
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] delivered {kind} invitation "
            f"from {source.name!r} to {target_character.name!r} "
            f"for id {collective_id}"
        )
        return True

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
        elif command_id == GUILD_INVITATION_RESPONSE_COMMAND:
            kind = "guild"
        else:
            raise ValueError(
                f"unsupported invitation response 0x{command_id:04x}"
            )

        CharacterService._accept_gameplay_state_command(s, command_id)
        response_status = int(request.status)
        if not 0 <= response_status <= _INVITATION_RESPONSE_MAX_STATUS[kind]:
            raise ValueError(
                f"client sent invalid {kind} invitation response "
                f"{response_status}"
            )
        SocialService._complete_social_invitation(
            self,
            s,
            kind,
            int(request.inviter_character_id),
            int(request.collective_id),
            response_status,
        )

    def _complete_social_invitation(
            self, s: Session, kind: str, inviter_id: int,
            collective_id: int, response_status: int,
            token: str | None = None) -> None:
        """Answer one ledger invitation for the authenticated character.

        ``response_status`` 0 accepts; any other value is the client's own
        refusal reason and is forwarded to the inviter.  An answer with no
        matching live invitation (forged ids, a replay, an expired request)
        admits nobody and is never forwarded to the claimed inviter.
        """

        target = self._character_for_session(s)
        target_id = int(target.character_id)
        command_id = (
            GROUP_INVITATION_RESPONSE_COMMAND if kind == "group"
            else GUILD_INVITATION_RESPONSE_COMMAND
        )
        lookup = (
            self.social_store.group_for if kind == "group"
            else self.social_store.guild_for
        )
        id_field = "group_id" if kind == "group" else "guild_id"
        current = lookup(target_id)
        if (
            response_status == _INVITE_JOINED
            and current is not None
            and int(getattr(current, id_field)) == int(collective_id)
        ):
            # The client answers both with the response command and the
            # notification echo; the second arrival is an idempotent repeat.
            return
        previous_group_id = (
            int(current.group_id)
            if kind == "group" and current is not None else None
        )

        # An invitation is an asynchronous notification, so the collection can
        # change between sending it and the player pressing accept. The
        # inviter's GroupInvitationResponse does not reach the accepting
        # client, so a group failure is reported to them with
        # GroupingFailedCommand instead.
        grouping_failure = None
        try:
            self.social_store.answer_invitation(
                kind,
                inviter_id,
                target_id,
                collective_id,
                self._social_member(s) if response_status == _INVITE_JOINED else None,
                accept=response_status == _INVITE_JOINED,
                token=token,
            )
        except InvitationMissingError as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] ignored {kind} invitation answer "
                f"from {target.name!r} for inviter {inviter_id} and "
                f"collection {collective_id}: {exc}"
            )
            if kind == "group" and response_status == _INVITE_JOINED:
                s.send_reliable_ordered(
                    encode_grouping_failed(
                        GROUPING_FAILED_REQUEST_EXPIRED,
                        target.name,
                        server_time() & 0xFFFFFFFF,
                    ),
                    track=True,
                    label="grouping-failed",
                )
            return
        except GroupFullError as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected {kind} "
                f"acceptance into a full group: {exc}"
            )
            response_status = _INVITE_ALREADY_MEMBER
            grouping_failure = GROUPING_FAILED_GROUP_IS_FULL
        except SocialContactBlockedError:
            response_status = _INVITE_DECLINED
        except ValueError as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected stale "
                f"{kind} acceptance: {exc}"
            )
            response_status = _INVITE_ALREADY_MEMBER
            current = lookup(target_id)
            grouping_failure = (
                GROUPING_FAILED_ALREADY_IN_A_GROUP
                if current is not None
                and int(getattr(current, id_field)) != int(collective_id)
                else GROUPING_FAILED_REQUEST_EXPIRED
            )

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
                f"{collective_id}"
            )

        if response_status == _INVITE_JOINED:
            self._broadcast_social_status(kind, collective_id)
            if previous_group_id is not None and previous_group_id != int(collective_id):
                self._broadcast_social_status("group", previous_group_id)
            self._refresh_social_chat_memberships(inviter_id, target_id)
            if kind == "guild":
                SocialService._refresh_guild_overheads(self, (target_id,))
        source_session = SocialService._social_session_for_character(
            self, inviter_id
        )
        if source_session is not None:
            source_session.send_reliable_ordered(
                encode_invitation_response(
                    command_id,
                    response_status,
                    target.name,
                    target_id,
                    int(collective_id),
                    LOCAL_PLAYER_ACTOR_ID,
                ),
                track=True,
                label=f"{kind}-invitation-response",
            )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] handled {kind} invitation "
            f"response from {target.name!r}; inviter="
            f"{inviter_id}, collective="
            f"{collective_id}, status={response_status}"
        )

    def _handle_social_notification_action(self, s: Session, action) -> None:
        """Handle a social NotificationCommand echo through the ledger.

        After answering an invitation or join request with its dedicated
        command the client echoes the notification (NotificationCommand
        branch 2, 0x14025aa5c).  The echo carries the client-controlled JSON
        payload, so it may only complete an invitation this server issued to
        the authenticated character; the token is the notification's
        ``invitationId``.  Response code 3 is the historical accept path.
        """

        kind = str(action.payload.get("invitation", ""))
        if kind not in _SOCIAL_NOTIFICATION_TYPES:
            raise ValueError(f"unsupported social notification {kind!r}")
        target = self._character_for_session(s)
        if int(action.payload.get("targetCharacterId", 0)) != int(target.character_id):
            raise ValueError("social invitation belongs to another character")
        if kind in {"group", "guild"} and action.subtype == 3:
            SocialService._complete_social_invitation(
                self,
                s,
                kind,
                int(action.payload.get("sourceCharacterId", 0)),
                int(action.payload.get("collectiveId", 0)),
                _INVITE_JOINED,
                token=str(action.payload.get("invitationId", "")),
            )
        CharacterService._accept_gameplay_state_command(
            s, CLIENT_NOTIFICATION_COMMAND
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

    # -- group/guild management -----------------------------------------------

    def _social_session_for_character(self, character_id: int) -> Session | None:
        character_id = int(character_id)
        return next(
            (
                session
                for session in self._active_gameplay_sessions()
                if int(session.gameplay_character.character_id) == character_id
            ),
            None,
        )

    def _social_member_sessions(self, kind: str, collective_id: int):
        """Online sessions whose character belongs to one collection."""

        lookup = (
            self.social_store.group_for if kind == "group"
            else self.social_store.guild_for
        )
        id_field = "group_id" if kind == "group" else "guild_id"
        return tuple(
            session
            for session in self._active_gameplay_sessions()
            if (
                (collective := lookup(session.gameplay_character.character_id))
                is not None
                and int(getattr(collective, id_field)) == int(collective_id)
            )
        )

    def _send_social_status(self, s: Session, kind: str, *, label: str) -> None:
        character_id = int(self._character_for_session(s).character_id)
        if kind == "group":
            packet = encode_group_status(self._group_status_for(character_id))
        else:
            packet = encode_guild_status(self._guild_status_for(character_id))
        s.send_reliable_ordered(packet, track=True, label=label)

    def _refresh_guild_overheads(self, character_ids) -> None:
        """Re-announce players whose overhead guild name changed.

        RemotePlayerInfo carries the guild name drawn above a player; the
        presence ``appearance`` event resends it to everyone nearby.
        """

        for character_id in character_ids:
            session = SocialService._social_session_for_character(
                self, character_id
            )
            callback = getattr(session, "gameplay_presence_callback", None)
            if session is None or callback is None:
                continue
            try:
                callback(session, "appearance")
            except Exception as exc:  # cosmetic refresh must not undo state
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] guild overhead refresh failed "
                    f"for {character_id}: {type(exc).__name__}: {exc}"
                )

    def _handle_social_kick(self, s: Session, request, kind: str) -> None:
        """GroupKickCommand/GuildKickCommand: remove one named member."""

        actor = self._character_for_session(s)
        try:
            collective_id, removed = self.social_store.kick_member(
                kind, int(actor.character_id), request.target_name
            )
        except ValueError as exc:
            # The client validates rights, self-kicks and membership first;
            # a rejected kick means its roster is stale, so resynchronise.
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected {kind} kick of "
                f"{request.target_name!r} by {actor.name!r}: {exc}"
            )
            SocialService._send_social_status(
                self, s, kind, label=f"{kind}-kick-rejected"
            )
            return
        removed_session = SocialService._social_session_for_character(
            self, removed.character_id
        )
        if removed_session is not None:
            removed_session.send_reliable_ordered(
                encode_social_kick_notice(
                    GROUP_KICK_COMMAND if kind == "group" else GUILD_KICK_COMMAND,
                    removed.name,
                    actor_id=LOCAL_PLAYER_ACTOR_ID,
                ),
                track=True,
                label=f"{kind}-kicked",
            )
            SocialService._send_social_status(
                self, removed_session, kind, label=f"{kind}-status-after-kick"
            )
        self._broadcast_social_status(kind, collective_id)
        self._refresh_social_chat_memberships(removed.character_id)
        if kind == "guild":
            SocialService._refresh_guild_overheads(self, (removed.character_id,))
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] {actor.name!r} removed "
            f"{removed.name!r} from {kind} {collective_id}"
        )

    def _handle_group_edit(self, s: Session, request) -> None:
        """GroupEditCommand: rename the leader's group / toggle "hide group"."""

        actor = self._character_for_session(s)
        try:
            group = self.social_store.edit_group(
                int(actor.character_id), request.name, request.flag
            )
        except SocialNameError:
            s.send_reliable_ordered(
                encode_group_edit_result(GROUP_EDIT_INVALID_NAME, request),
                track=True,
                label="group-edit-rejected",
            )
            return
        except SocialNoChangeError:
            group = self.social_store.group_for(int(actor.character_id))
            s.send_reliable_ordered(
                encode_group_edit_result(
                    GROUP_EDIT_OK,
                    _replace_dataclass(
                        request, name=group.name, flag=group.private
                    ),
                ),
                track=True,
                label="group-edit-unchanged",
            )
            return
        except ValueError as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected group edit by "
                f"{actor.name!r}: {exc}"
            )
            SocialService._send_social_status(
                self, s, "group", label="group-edit-rejected"
            )
            return
        # Every member learns the new name (``groupChangedName``) and hidden
        # flag before the roster refresh, which would otherwise rename the
        # group silently.
        result = encode_group_edit_result(
            GROUP_EDIT_OK,
            _replace_dataclass(request, name=group.name, flag=group.private),
        )
        for member_session in SocialService._social_member_sessions(
            self, "group", group.group_id
        ):
            member_session.send_reliable_ordered(
                result, track=True, label="group-edit-result"
            )
        self._broadcast_social_status("group", group.group_id)
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] {actor.name!r} edited group "
            f"{group.group_id}: name={group.name!r} hidden={group.private}"
        )

    def _charge_social_fee(self, s: Session, *, andermant: int, gold: int):
        """Debit an authored fee from the session wallet (flow_lock held).

        Returns the new CurrencyChanged snapshot, or ``None`` for a free
        operation; raises :class:`_InsufficientSocialFunds`.
        """

        currency = s.gameplay_currency_state
        if andermant <= 0 and gold <= 0:
            return None
        if currency is None:
            raise _InsufficientSocialFunds("the wallet is not initialized")
        if int(currency.andermant) < andermant or int(currency.amount) < gold:
            raise _InsufficientSocialFunds("the wallet cannot pay the fee")
        # The shared wallet commits only the difference.
        s.gameplay_currency_state = replace(
            currency,
            currency_type=int(currency.andermant) - max(0, andermant),
            amount=int(currency.amount) - max(0, gold),
            notify=True,
        )
        return s.gameplay_currency_state

    def _refund_social_fee(self, s: Session, *, andermant: int, gold: int):
        with s.flow_lock:
            currency = s.gameplay_currency_state
            if currency is None:
                return None
            s.gameplay_currency_state = replace(
                currency,
                currency_type=int(currency.andermant) + max(0, andermant),
                amount=int(currency.amount) + max(0, gold),
                notify=True,
            )
            return s.gameplay_currency_state

    def _send_social_currency(self, s: Session, currency, *, label: str) -> None:
        if currency is None:
            return
        packet = encode_server_command_batch(
            (ServerCommand(CURRENCY_CHANGED_COMMAND, currency, LOCAL_PLAYER_ACTOR_ID),)
        )
        s.send_reliable_ordered(packet, track=True, label=label)

    def _handle_guild_name_edit(self, s: Session, request) -> None:
        """GuildNameEditCommand: the leader renames the guild for a fee.

        FoundGuildDialog's edit mode charges ``GuildEditNameVCCost`` gold
        (bool False) or, after its confirmation, ``GuildEditNameRCCost``
        Andermant (bool True).  The fee is debited and saved before the
        rename commits and refunded when the commit loses a race.
        """

        actor = self._character_for_session(s)
        actor_id = int(actor.character_id)

        def reply(status: int, name: str) -> None:
            s.send_reliable_ordered(
                encode_guild_name_edit_result(status, name, flag=request.flag),
                track=True,
                label="guild-rename-rejected",
            )

        failure_statuses = (
            (SocialNameTakenError, GUILD_RENAME_NAME_TAKEN),
            (SocialNameError, GUILD_RENAME_NAME_INVALID),
            (ValueError, GUILD_RENAME_NO_CHANGE),
        )
        try:
            cleaned = self.social_store.validate_guild_rename(actor_id, request.name)
        except ValueError as exc:
            status = next(code for kind, code in failure_statuses if isinstance(exc, kind))
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected guild rename by "
                f"{actor.name!r} to {request.name!r}: {exc}"
            )
            reply(status, request.name)
            return
        andermant, gold = SocialService._guild_fee(self, "rename", request.flag)
        try:
            with s.flow_lock:
                charged = SocialService._charge_social_fee(
                    self, s, andermant=andermant, gold=gold
                )
        except _InsufficientSocialFunds as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected guild rename by "
                f"{actor.name!r}: {exc}"
            )
            reply(GUILD_RENAME_NOT_ENOUGH_FUNDS, cleaned)
            return
        if charged is not None:
            CharacterService._persist_gameplay_session(
                self, s, reason="guild rename fee", flush=True
            )
        try:
            guild = self.social_store.rename_guild(actor_id, cleaned)
        except ValueError as exc:
            refunded = (
                SocialService._refund_social_fee(
                    self, s, andermant=andermant, gold=gold
                )
                if charged is not None else None
            )
            if refunded is not None:
                CharacterService._persist_gameplay_session(
                    self, s, reason="guild rename refund", flush=True
                )
                SocialService._send_social_currency(
                    self, s, refunded, label="guild-rename-refund"
                )
            status = next(code for kind, code in failure_statuses if isinstance(exc, kind))
            reply(status, cleaned)
            return
        SocialService._send_social_currency(
            self, s, charged, label="guild-rename-fee"
        )
        result = encode_guild_name_edit_result(
            GUILD_RENAME_OK, guild.name, source_name=actor.name, flag=request.flag
        )
        members = SocialService._social_member_sessions(self, "guild", guild.guild_id)
        for member_session in members:
            member_session.send_reliable_ordered(
                result, track=True, label="guild-rename-result"
            )
        self._broadcast_social_status("guild", guild.guild_id)
        SocialService._refresh_guild_overheads(
            self, tuple(int(member.character_id) for member in guild.members)
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] {actor.name!r} renamed guild "
            f"{guild.guild_id} to {guild.name!r} for {andermant} Andermant "
            f"and {gold} gold copper"
        )

    def _handle_guild_motd_request(self, s: Session, request) -> None:
        """GuildMessageOfTheDayRequestCommand (``/motd`` and chat login)."""

        guild = self.social_store.guild_for(
            int(self._character_for_session(s).character_id)
        )
        if guild is None or not guild.message_of_the_day:
            return
        s.send_reliable_ordered(
            encode_guild_motd(guild.message_of_the_day, guild.guild_id),
            track=True,
            label="guild-motd",
        )

    def _handle_guild_motd_change(self, s: Session, request) -> None:
        """GuildMessageOfTheDayChangeCommand (``/cmotd``, ``/dmotd``).

        There is no failure reply in the protocol; a refused change is only
        logged.  A new non-empty message is posted to every online member.
        """

        actor = self._character_for_session(s)
        try:
            guild = self.social_store.change_guild_message(
                int(actor.character_id), request.message
            )
        except ValueError as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected guild message change "
                f"by {actor.name!r}: {exc}"
            )
            return
        if not guild.message_of_the_day:
            return
        packet = encode_guild_motd(guild.message_of_the_day, guild.guild_id)
        for member_session in SocialService._social_member_sessions(
            self, "guild", guild.guild_id
        ):
            member_session.send_reliable_ordered(
                packet, track=True, label="guild-motd"
            )

    def _handle_ignored_list(self, s: Session, request) -> None:
        """IgnoredListCommand: refresh, add or remove; answer the full list."""

        owner = self._character_for_session(s)
        owner_id = int(owner.character_id)
        if request.operation in {IGNORED_LIST_ADD, IGNORED_LIST_REMOVE}:
            wanted = str(request.name).strip().casefold()
            target_id = None
            target_name = str(request.name).strip()
            if request.operation == IGNORED_LIST_REMOVE:
                # Prefer the owner's own entries so a renamed or deleted
                # character can still be removed by the shown name.
                listed = {
                    name.casefold(): ignored_id
                    for ignored_id, name in self.social_store.ignored_for(owner_id)
                }
                target_id = listed.get(wanted)
            if target_id is None:
                target = self._resolve_buddy_target(request.name)
                if target is not None:
                    target_id = int(target.character_id)
                    target_name = str(target.name)
            if target_id is None:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] ignored IgnoredList operation "
                    f"{request.operation} for unknown player {request.name!r}"
                )
            else:
                try:
                    changed = self.social_store.set_ignored(
                        owner_id,
                        target_id,
                        target_name,
                        remove=request.operation == IGNORED_LIST_REMOVE,
                    )
                except ValueError as exc:
                    print(
                        f"[{GAMEPLAY_SERVICE_NAME}] rejected IgnoredList "
                        f"operation {request.operation} by {owner.name!r} "
                        f"for {request.name!r}: {exc}"
                    )
                else:
                    if changed:
                        verb = (
                            "removed" if request.operation == IGNORED_LIST_REMOVE
                            else "added"
                        )
                        print(
                            f"[{GAMEPLAY_SERVICE_NAME}] {owner.name!r} {verb} "
                            f"{target_name!r} on the ignore list"
                        )
        s.send_reliable_ordered(
            SocialService._ignored_status_packet(self, owner_id, request.field_24),
            track=True,
            label="ignored-status",
        )

    def _groups_for_join_request(self, s: Session, group_name: str):
        wanted = str(group_name).strip().casefold()
        return tuple(
            group
            for group in self.social_store.groups_in_map(
                s.gameplay_map or DEFAULT_GAMEPLAY_MAP
            )
            if group.name.casefold() == wanted
        )

    def _handle_group_join_request(self, s: Session, request) -> None:
        """GroupJoinRequestCommand: ask a finder group's leader to admit us.

        The request names the group as the finder showed it (GroupInfo
        +0x48); the server resolves it among the listed groups of the
        requester's map.  A valid request is recorded in the invitation
        ledger and shown to the leader as notification type 2.
        """

        requester = self._character_for_session(s)
        requester_id = int(requester.character_id)
        if request.source_name.casefold() != requester.name.casefold():
            raise ValueError(
                "group join request sender does not match the "
                "authenticated character"
            )

        def reply(status: int, group_id: int = 0, name: str = "") -> None:
            s.send_reliable_ordered(
                encode_group_join_response(
                    status, requester_id, group_id, name,
                    actor_id=LOCAL_PLAYER_ACTOR_ID,
                ),
                track=True,
                label="group-join-request-rejected",
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected group join request from "
                f"{requester.name!r} for {request.group_name!r}; status={status}"
            )

        current = self.social_store.group_for(requester_id)
        candidates = SocialService._groups_for_join_request(self, s, request.group_name)
        if current is not None and not request.leave_current:
            reply(GROUP_JOIN_ALREADY_IN_GROUP)
            return
        if len(candidates) != 1:
            # Local choice: an ambiguous name (two listed groups on one map
            # sharing it) is refused rather than guessed.
            reply(GROUP_JOIN_NO_GROUP)
            return
        group = candidates[0]
        if current is not None and current.group_id == group.group_id:
            reply(GROUP_JOIN_ALREADY_IN_GROUP, group.group_id)
            return
        if self.social_store.social_contact_blocked(
            requester_id, int(group.owner_character_id)
        ):
            # Local choice: an ignore in either direction answers like the
            # leader's refusal; the request is never shown to the leader.
            reply(GROUP_JOIN_REFUSED, group.group_id)
            return
        leader = next(
            (m for m in group.members if m.character_id == group.owner_character_id),
            None,
        )
        leader_name = leader.name if leader is not None else ""
        if len(group.members) >= GROUP_MEMBER_LIMIT:
            s.send_reliable_ordered(
                encode_grouping_failed(
                    GROUPING_FAILED_GROUP_IS_FULL, leader_name,
                    server_time() & 0xFFFFFFFF,
                ),
                track=True,
                label="grouping-failed-join-request",
            )
            return
        leader_session = SocialService._social_session_for_character(
            self, group.owner_character_id
        )
        if leader_session is None:
            reply(GROUP_JOIN_LEADER_OFFLINE, group.group_id, leader_name)
            return
        invitation_id = uuid.uuid4()
        try:
            self.social_store.issue_invitation(
                "group-request",
                requester_id,
                int(group.owner_character_id),
                group.group_id,
                str(invitation_id),
                leave_current=bool(request.leave_current),
            )
        except InvitationPendingError:
            reply(GROUP_JOIN_PENDING, group.group_id, leader_name)
            return
        except SocialContactBlockedError:
            # Local choice: an ignore in either direction answers like the
            # leader's refusal and the request is never shown.
            reply(GROUP_JOIN_REFUSED, group.group_id, leader_name)
            return
        except GroupFullError:
            s.send_reliable_ordered(
                encode_grouping_failed(
                    GROUPING_FAILED_GROUP_IS_FULL, leader_name,
                    server_time() & 0xFFFFFFFF,
                ),
                track=True,
                label="grouping-failed-join-request",
            )
            return
        except SocialPermissionError:
            reply(GROUP_JOIN_NOT_LEADER, group.group_id, leader_name)
            return
        except ValueError:
            reply(GROUP_JOIN_FAILED, group.group_id, leader_name)
            return
        CharacterService._send_social_invitation_notification(
            leader_session,
            kind="group-request",
            source_character=requester,
            target_character=leader_session.gameplay_character,
            collective_id=group.group_id,
            collective_name=group.name,
            source_map=s.gameplay_map or DEFAULT_GAMEPLAY_MAP,
            invitation_id=invitation_id,
            honor_points=int(getattr(s, "gameplay_honor_points", 0) or 0),
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] delivered group join request from "
            f"{requester.name!r} to leader {leader_name!r} of group "
            f"{group.group_id}"
        )

    def _handle_group_join_response(self, s: Session, response) -> None:
        """GroupJoinRequestResponseCommand from a leader: admit or refuse.

        Acceptance must present the ledger token the notification carried
        (its ``timeStamp``); a refusal consumes the pending request and is
        forwarded to the requester with the leader's reason code.  Answers
        without a live request are dropped.
        """

        leader = self._character_for_session(s)
        leader_id = int(leader.character_id)
        requester_id = int(response.requester_id)
        group_id = int(response.group_id)
        requester_session = SocialService._social_session_for_character(
            self, requester_id
        )

        def grouping_failed(reason: int, name: str = "") -> None:
            s.send_reliable_ordered(
                encode_grouping_failed(reason, name, server_time() & 0xFFFFFFFF),
                track=True,
                label="grouping-failed-join-response",
            )

        def tell_requester(status: int) -> None:
            if requester_session is None:
                return
            requester_session.send_reliable_ordered(
                encode_group_join_response(
                    status, requester_id, group_id, leader.name,
                    actor_id=LOCAL_PLAYER_ACTOR_ID,
                ),
                track=True,
                label="group-join-response",
            )

        if response.status != GROUP_JOIN_ACCEPTED:
            try:
                self.social_store.answer_invitation(
                    "group-request", requester_id, leader_id, group_id,
                    accept=False,
                )
            except InvitationMissingError:
                return
            tell_requester(int(response.status))
            return
        if requester_session is None:
            try:
                self.social_store.answer_invitation(
                    "group-request", requester_id, leader_id, group_id,
                    accept=False, token=response.token,
                )
            except InvitationMissingError:
                pass
            grouping_failed(GROUPING_FAILED_REQUEST_EXPIRED)
            return
        requester = requester_session.gameplay_character
        previous = self.social_store.group_for(requester_id)
        try:
            self.social_store.answer_invitation(
                "group-request",
                requester_id,
                leader_id,
                group_id,
                self._social_member(requester_session),
                accept=True,
                token=response.token,
            )
        except InvitationMissingError as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] ignored group join acceptance by "
                f"{leader.name!r} for {requester_id}: {exc}"
            )
            grouping_failed(GROUPING_FAILED_REQUEST_EXPIRED)
            return
        except GroupFullError:
            grouping_failed(GROUPING_FAILED_GROUP_IS_FULL)
            tell_requester(GROUP_JOIN_FAILED)
            return
        except SocialContactBlockedError:
            grouping_failed(GROUPING_FAILED_REQUEST_EXPIRED)
            tell_requester(GROUP_JOIN_REFUSED)
            return
        except SocialPermissionError:
            tell_requester(GROUP_JOIN_NOT_LEADER)
            return
        except ValueError as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected group join acceptance "
                f"for {requester.name!r}: {exc}"
            )
            grouping_failed(GROUPING_FAILED_ALREADY_IN_A_GROUP, requester.name)
            tell_requester(GROUP_JOIN_ALREADY_IN_GROUP)
            return
        tell_requester(GROUP_JOIN_ACCEPTED)
        self._broadcast_social_status("group", group_id)
        if previous is not None and previous.group_id != group_id:
            self._broadcast_social_status("group", previous.group_id)
        self._refresh_social_chat_memberships(requester_id)
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] {leader.name!r} admitted "
            f"{requester.name!r} into group {group_id}"
        )

    def _handle_group_multiple_invite(self, s: Session, request) -> None:
        """GroupMultipleInviteCommand: invite up to ten selected players."""

        source = self._character_for_session(s)
        if request.source_name.casefold() != source.name.casefold():
            raise ValueError(
                "group multiple invite sender does not match the "
                "authenticated character"
            )
        names = tuple(dict.fromkeys(
            name.strip() for name in request.target_names if name.strip()
        ))
        if len(names) > GROUP_MULTIPLE_INVITE_LIMIT:
            raise ValueError(
                f"group multiple invite names {len(names)} players; "
                f"the client allows {GROUP_MULTIPLE_INVITE_LIMIT}"
            )
        delivered = 0
        for name in names:
            delivered += bool(
                SocialService._invite_to_social_collection(self, s, source, "group", name)
            )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] {source.name!r} invited "
            f"{delivered}/{len(names)} selected players to their group"
        )

    def _handle_group_candidates(self, s: Session, request) -> None:
        """GroupCandidatesRequestCommand: list online friends or neighbours.

        "Invite friends" (False) lists online buddies; "Invite players
        nearby" (True) lists players in the requester's map instance.  Own
        group members, ignored players and the excluded ids never appear.
        """

        me = self._character_for_session(s)
        me_id = int(me.character_id)
        own_group = self.social_store.group_for(me_id)
        excluded = {me_id, *(int(value) for value in request.excluded_ids)}
        if own_group is not None:
            excluded.update(int(m.character_id) for m in own_group.members)
        if request.nearby:
            sessions = self._active_gameplay_sessions(
                map_id=s.gameplay_map, instance_of=s
            )
        else:
            buddies = set(self.social_store.buddies_for(me_id))
            sessions = tuple(
                session for session in self._active_gameplay_sessions()
                if int(session.gameplay_character.character_id) in buddies
            )
        summaries = []
        for session in sessions:
            character = session.gameplay_character
            character_id = int(character.character_id)
            if (
                character_id in excluded
                or self.social_store.social_contact_blocked(me_id, character_id)
            ):
                continue
            excluded.add(character_id)
            their_group = self.social_store.group_for(character_id)
            summary = self._player_summary(
                character,
                map_or_guild=session.gameplay_map,
                online=True,
                actor_id=self._social_actor_id_for(me_id, character_id),
            )
            summaries.append(_replace_dataclass(
                summary,
                group_invitable=their_group is None or len(their_group.members) <= 1,
            ))
            if len(summaries) >= _GROUP_CANDIDATE_LIMIT:
                break
        s.send_reliable_ordered(
            encode_group_candidates(
                request.field_20, tuple(summaries), actor_id=LOCAL_PLAYER_ACTOR_ID
            ),
            track=True,
            label="group-candidates",
        )
