"""Typed codecs for the local social and multiplayer-presence commands.

The layouts in this module come from the generated serializers in the 2026-09-09 client and are checked against the corresponding retail captures.  The
server constructs every response from local state; captured payloads are never
replayed.
"""

from __future__ import annotations

from dso_server.protocol.command_ids import command_id
from dataclasses import dataclass

from dso_server.protocol.character_selection import BitReader, BitWriter
from dso_server.protocol.pvp_match_protocol import honor_level_title_for


DISCARD_PLAYER_COMMAND = command_id("Commands::DiscardPlayerCommand", 0x001E)
NEW_REMOTE_PLAYER_COMMAND = command_id("Commands::NewRemotePlayerCommand", 0x0021)
REMOTE_PLAYER_INFO_COMMAND = command_id("Commands::RemotePlayerInfoCommand", 0x0022)
UPDATE_SHOW_PLAYER_OVERHEAD_ICONS_COMMAND = command_id("Commands::UpdateShowPlayerOverheadIconsCommand", 0x0023)
ACTORS_LEFT_COMMAND = command_id("Commands::ActorsLeftVicinityCommand", 0x0073)
CREATE_GROUP_COMMAND = command_id("Commands::GroupFoundationCommand", 0x0098)
CREATE_GROUP_RESULT_COMMAND = command_id("Commands::GroupFoundationResponseCommand", 0x0099)
GROUP_INVITE_COMMAND = command_id("Commands::GroupInviteCommand", 0x009A)
GROUP_INVITATION_RESPONSE_COMMAND = command_id("Commands::GroupInvitationResponseCommand", 0x009C)
GROUP_LEAVE_COMMAND = command_id("Commands::GroupLeaveCommand", 0x00A0)
GROUPING_FAILED_COMMAND = command_id("Commands::GroupingFailedCommand", 0x00A3)
GROUP_STATUS_COMMAND = command_id("Commands::GroupStatusCommand", 0x0097)
GROUP_STATUS_REQUEST_COMMAND = command_id("Commands::GroupListCommand", 0x0096)
# ``GroupingFailedCommand``'s reason is a validated enum: the generated reader
# rejects anything outside -1..3 and aborts the whole command, and
# ``ClientSocialManager::HandleGroupingFailedCommand`` switches the four valid
# values onto ``gui.window.grouping`` locale keys.  Reasons 1 and 2 render the
# affected player name; 0 and 3 are name-less.
GROUPING_FAILED_GROUP_IS_FULL = 0
GROUPING_FAILED_ALREADY_IN_A_GROUP = 1
GROUPING_FAILED_REQUEST_EXPIRED = 2
GROUPING_FAILED_NO_GROUP = 3
GUILD_LIST_REQUEST_COMMAND = command_id("Commands::GuildListCommand", 0x00A6)
GUILD_STATUS_COMMAND = command_id("Commands::GuildStatusCommand", 0x00A7)
# ``GuildStatusCommand``'s member rank is a validated three-value enum: the
# generated reader rejects anything outside -1..2 and aborts the whole
# command, and the client's ToString maps 0/1/2 to Member/Officer/Leader.
GUILD_RANK_MEMBER = 0
GUILD_RANK_OFFICER = 1
GUILD_RANK_LEADER = 2
BUDDY_LIST_COMMAND = command_id("Commands::BuddyListCommand", 0x00B0)
BUDDY_STATUS_COMMAND = command_id("Commands::BuddyStatusCommand", 0x00B1)
IGNORED_STATUS_COMMAND = command_id("Commands::IgnoredStatusCommand", 0x00B3)
CREATE_GUILD_COMMAND = command_id("Commands::GuildFoundationCommand", 0x00A8)
CREATE_GUILD_RESULT_COMMAND = command_id("Commands::GuildFoundationResponseCommand", 0x00A9)
GUILD_INVITE_COMMAND = command_id("Commands::GuildInviteCommand", 0x00AA)
GUILD_INVITATION_RESPONSE_COMMAND = command_id("Commands::GuildInvitationResponseCommand", 0x00AB)
GUILD_LEAVE_COMMAND = command_id("Commands::GuildLeaveCommand", 0x00AD)
PLAYER_QUERY_COMMAND = command_id("Commands::PlayerQueryCommand", 0x00B4)
PLAYER_QUERY_RESULT_COMMAND = command_id("Commands::PlayerQueryResultCommand", 0x00B5)
GROUP_FINDER_QUERY_COMMAND = command_id("Commands::GroupQueryCommand", 0x00B6)
GROUP_FINDER_RESULT_COMMAND = command_id("Commands::GroupQueryResultCommand", 0x00B7)
GROUP_MEMBER_QUERY_COMMAND = command_id("Commands::GroupMemberQueryCommand", 0x00B8)
GROUP_MEMBER_QUERY_RESULT_COMMAND = command_id("Commands::GroupMemberQueryResultCommand", 0x00B9)
# ``GroupMemberQueryResultCommand``'s status is validated against -1..1.
GROUP_MEMBER_QUERY_OK = 0
GROUP_MEMBER_QUERY_FAILED = 1

# Group/guild management and ignored players.  Every layout below was read
# from the release261_1024 client's generated Decode (vtable slot 5); the
# operation enums come from the ClientSocialManager senders and handlers
# cited at each constant.  Client requests carry no trailing actor context;
# server replies append one only when the class is ``actor_scoped``.
GROUP_MULTIPLE_INVITE_COMMAND = command_id("Commands::GroupMultipleInviteCommand", 155)
GROUP_JOIN_REQUEST_COMMAND = command_id("Commands::GroupJoinRequestCommand", 157)
GROUP_JOIN_RESPONSE_COMMAND = command_id("Commands::GroupJoinRequestResponseCommand", 158)
GROUP_KICK_COMMAND = command_id("Commands::GroupKickCommand", 159)
GROUP_EDIT_COMMAND = command_id("Commands::GroupEditCommand", 161)
GROUP_EDIT_RESULT_COMMAND = command_id("Commands::GroupEditResponseCommand", 162)
GROUP_CANDIDATES_COMMAND = command_id("Commands::GroupCandidatesRequestCommand", 164)
GROUP_CANDIDATES_RESULT_COMMAND = command_id("Commands::GroupCandidatesResponseCommand", 165)
GUILD_KICK_COMMAND = command_id("Commands::GuildKickCommand", 172)
GUILD_NAME_EDIT_COMMAND = command_id("Commands::GuildNameEditCommand", 174)
GUILD_NAME_EDIT_RESULT_COMMAND = command_id("Commands::GuildNameEditResponseCommand", 175)
IGNORED_LIST_COMMAND = command_id("Commands::IgnoredListCommand", 178)
GUILD_MOTD_COMMAND = command_id("Commands::GuildMessageOfTheDayCommand", 186)
GUILD_MOTD_REQUEST_COMMAND = command_id("Commands::GuildMessageOfTheDayRequestCommand", 187)
GUILD_MOTD_CHANGE_COMMAND = command_id("Commands::GuildMessageOfTheDayChangeCommand", 188)
SOCIAL_MANAGEMENT_COMMANDS = frozenset({
    GROUP_MULTIPLE_INVITE_COMMAND, GROUP_JOIN_REQUEST_COMMAND, GROUP_JOIN_RESPONSE_COMMAND,
    GROUP_KICK_COMMAND, GROUP_EDIT_COMMAND, GROUP_CANDIDATES_COMMAND, GUILD_KICK_COMMAND,
    GUILD_NAME_EDIT_COMMAND, IGNORED_LIST_COMMAND, GUILD_MOTD_REQUEST_COMMAND,
    GUILD_MOTD_CHANGE_COMMAND,
})

# IgnoredListCommand's operation (validated -1..2).  ClientSocialManager
# sends 1 from AddPlayerToIgnored (0x14026fa4c), 2 from
# RemovePlayerFromIgnored (0x1402808f8) and 0 from the social window's
# refresh (0x1402810e4).
IGNORED_LIST_REFRESH = 0
IGNORED_LIST_ADD = 1
IGNORED_LIST_REMOVE = 2
# IgnoredStatusCommand's type (validated -1..3).  HandleIgnoredStatusCommand
# (0x14027c974) replaces its list only for 0; 1..3 are accepted and ignored.
IGNORED_STATUS_FULL_LIST = 0

# GroupEditResponseCommand's status (validated -1..1).  The handler
# (0x140277298) renames the group (``groupChangedName``) for 0; for 1 it
# shows ``groupNameEmpty`` or, with a name, ``rankedTeamNameInvalid``.
GROUP_EDIT_OK = 0
GROUP_EDIT_INVALID_NAME = 1

# GuildNameEditResponseCommand's status (validated -1..4), from
# HandleGuildNameEditResponseCommand (0x14027bd58).
GUILD_RENAME_OK = 0  # guildRenamed; the client then re-sends GuildListCommand
GUILD_RENAME_NOT_ENOUGH_FUNDS = 1  # notEnoughFundsForEditingGuild
GUILD_RENAME_NAME_TAKEN = 2  # guildNameAlreadyAssigned
GUILD_RENAME_NAME_INVALID = 3  # guildNameInvalid
GUILD_RENAME_NO_CHANGE = 4  # noNameChange

# Game::GroupJoinRequestResponseType (validated -1..11).  The leader's
# client sends 0 from AcceptGroupJoinRequest (0x14026ecd8) and refusals from
# RefuseGroupJoinRequest[ByReason] (0x140280180/0x1402801ac).  The
# requester's handler (0x1402789e8) shows the matching gui.window.grouping
# text.
GROUP_JOIN_ACCEPTED = 0  # groupJoinRequestAccepted
GROUP_JOIN_REFUSED = 1  # groupJoinRequestRefused
GROUP_JOIN_ALREADY_IN_GROUP = 2  # youAreAlreadyInAGroup
GROUP_JOIN_LEADER_NOT_IN_GROUP = 3  # playerNotInAGroup
GROUP_JOIN_LEADER_OFFLINE = 4  # isNotLoggedIn (name)
GROUP_JOIN_NOT_LEADER = 5  # playerNotLeader
GROUP_JOIN_UNKNOWN_PLAYER = 6  # unknownPlayer (name)
GROUP_JOIN_RIGHTS_REQUIRED = 7  # leaderRightsRequired
GROUP_JOIN_NO_GROUP = 8  # groupRequestErrorNoGroup
GROUP_JOIN_REQUESTS_DISABLED = 9  # groupRequestDisabledNotifications
GROUP_JOIN_FAILED = 10  # ocpRequestFailed (generic)
GROUP_JOIN_PENDING = 11  # groupRequestPending
GROUP_JOIN_MAX_STATUS = 11

# ``gui.window.grouping/inviteNumberLimit``: "Invite up to ten players
# simultaneously".
GROUP_MULTIPLE_INVITE_LIMIT = 10
# The generated array readers (0x14095ef6c/0x14095d24c) accept up to one
# million entries; the server bounds what it will even parse.
_MAX_SOCIAL_ARRAY = 1000


@dataclass(frozen=True)
class SocialKickRequest:
    """GroupKickCommand/GuildKickCommand: ``string, uint32, uint32``.

    GroupKickPlayer (0x140274894) and GuildKickPlayer (0x1402762a8) set only
    the target name; both uint32 fields keep their constructor defaults.
    """

    target_name: str
    field_60: int = 0
    field_64: int = 0


@dataclass(frozen=True)
class SocialEditRequest:
    """GroupEditCommand/GuildNameEditCommand: ``uint32, string, string, bool``.

    ClientSocialManager::EditGroup (0x140272664) fills the sender's name,
    the new group name and the "hide group" flag.  GuildNameEdit
    (0x140272c14) fills the new name and the payment flag: FoundGuildDialog
    sends ``False`` from its silver button and ``True`` only after the
    Andermant confirmation (0x14042f370/0x14042f84c).
    """

    field_20: int
    source_name: str
    name: str
    flag: bool


@dataclass(frozen=True)
class IgnoredListRequest:
    operation: int
    field_24: int
    name: str


@dataclass(frozen=True)
class GuildMotdRequest:
    """GuildMessageOfTheDayRequestCommand (``/motd`` and chat login)."""

    field_20: int


@dataclass(frozen=True)
class GuildMotdChange:
    """GuildMessageOfTheDayChangeCommand (``/cmotd text``; ``/dmotd`` = "")."""

    field_20: int
    message: str


@dataclass(frozen=True)
class GroupJoinRequest:
    """GroupJoinRequestCommand: ``string, string, uint32, uint32, bool``.

    ClientSocialManager::GroupJoinRequest (0x1402742a8) writes the sender's
    name, the selected finder entry's group name (GroupInfo +0x48), a local
    player id (+0x410, unused here), leaves +0xa4 at zero and sets the bool
    after ``confirmGroupLeaveForJoinRequest``.
    """

    source_name: str
    group_name: str
    field_a0: int
    field_a4: int
    leave_current: bool


@dataclass(frozen=True)
class GroupJoinResponse:
    """GroupJoinRequestResponseCommand ``enum8, uint32, uint32, string, string``.

    Sent by the leader: requester id (notification value +0x1c), the second
    notification value (the group id), the leader's name and, for
    acceptance only, the notification payload's ``timeStamp`` token.
    """

    status: int
    requester_id: int
    group_id: int
    leader_name: str
    token: str


@dataclass(frozen=True)
class GroupMultipleInvite:
    """GroupMultipleInviteCommand: ``string, string[], uint32``."""

    source_name: str
    target_names: tuple[str, ...]
    field_80: int


@dataclass(frozen=True)
class GroupCandidatesRequest:
    """GroupCandidatesRequestCommand: ``uint32, bool, uint32[]``.

    RequestGroupInviteCandidates (0x140280e14) sets +0x20 to a local player
    id, +0x24 True for "Invite players nearby" and False for "Invite
    friends"; it leaves the uint32 array empty.
    """

    field_20: int
    nearby: bool
    excluded_ids: tuple[int, ...]


def _read_social_array_count(reader: BitReader) -> int:
    count = reader.read_uint32_le()
    if count > _MAX_SOCIAL_ARRAY:
        raise ValueError(f"social request array is too long ({count})")
    return count


def decode_social_management(
    payload: bytes, command: int, bit_length: int | None = None
):
    """Decode one client group/guild management or ignore-list request."""

    reader = _client_reader(payload, command, bit_length)
    u32, string = reader.read_uint32_le, reader.read_string
    if command in {GROUP_KICK_COMMAND, GUILD_KICK_COMMAND}:
        result = SocialKickRequest(string(), u32(), u32())
    elif command in {GROUP_EDIT_COMMAND, GUILD_NAME_EDIT_COMMAND}:
        result = SocialEditRequest(u32(), string(), string(), reader.read_bool())
    elif command == IGNORED_LIST_COMMAND:
        operation = reader.read_uint8()
        if operation not in {IGNORED_LIST_REFRESH, IGNORED_LIST_ADD, IGNORED_LIST_REMOVE}:
            raise ValueError(f"invalid IgnoredList operation {operation}")
        result = IgnoredListRequest(operation, u32(), string())
    elif command == GUILD_MOTD_REQUEST_COMMAND:
        result = GuildMotdRequest(u32())
    elif command == GUILD_MOTD_CHANGE_COMMAND:
        result = GuildMotdChange(u32(), string())
    elif command == GROUP_JOIN_REQUEST_COMMAND:
        result = GroupJoinRequest(string(), string(), u32(), u32(), reader.read_bool())
    elif command == GROUP_JOIN_RESPONSE_COMMAND:
        status = reader.read_uint8()
        if status > GROUP_JOIN_MAX_STATUS:
            raise ValueError(f"invalid GroupJoinRequestResponse status {status}")
        result = GroupJoinResponse(status, u32(), u32(), string(), string())
    elif command == GROUP_MULTIPLE_INVITE_COMMAND:
        source = string()
        names = tuple(string() for _ in range(_read_social_array_count(reader)))
        result = GroupMultipleInvite(source, names, u32())
    elif command == GROUP_CANDIDATES_COMMAND:
        field_20, nearby = u32(), reader.read_bool()
        excluded = tuple(u32() for _ in range(_read_social_array_count(reader)))
        result = GroupCandidatesRequest(field_20, nearby, excluded)
    else:
        raise ValueError(f"unknown social management command 0x{command:04x}")
    _finish_byte_rounded(reader, "social management request")
    return result


def _server_writer(command: int) -> BitWriter:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(int(command).to_bytes(2, "little"))
    return writer


def _server_reader(payload: bytes, bit_length: int, command: int) -> BitReader:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("expected a server command batch")
    actual = int.from_bytes(reader.read_bytes(2), "little")
    if actual != command:
        raise ValueError(f"expected command 0x{command:04x}, got 0x{actual:04x}")
    return reader


def _finish_server(reader: BitReader, label: str) -> None:
    if reader.read_uint8() != 0xFF or reader.remaining:
        raise ValueError(f"{label} has trailing data")


def encode_guild_motd(message: str, field_20: int = 0) -> tuple[bytes, int]:
    """Encode GuildMessageOfTheDayCommand: ``string, uint32``; no actor.

    HandleGuildMessageOfTheDayCommand (0x14027bb74) posts the string to the
    guild chat channel under ``gui.window.chat/GuildMessage`` ("Guild News")
    and ignores the uint32, which this server fills with the guild id.
    """

    writer = _server_writer(GUILD_MOTD_COMMAND)
    writer.write_string(message)
    writer.write_uint32_le(int(field_20) & 0xFFFFFFFF)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_guild_motd(payload: bytes, bit_length: int) -> tuple[str, int]:
    reader = _server_reader(payload, bit_length, GUILD_MOTD_COMMAND)
    value = (reader.read_string(), reader.read_uint32_le())
    _finish_server(reader, "GuildMessageOfTheDay")
    return value


def encode_group_edit_result(
    status: int, request: SocialEditRequest, *, field_1c: int = 0,
) -> tuple[bytes, int]:
    """Encode GroupEditResponseCommand; no actor context.

    Layout ``enum8(-1..1), uint32, uint32, string, string, bool``: the status
    and a uint32 precede an echo of the GroupEditCommand body.  The handler
    reads only the status, the name (+0x68) and the hidden flag (+0xa8),
    which it stores as the group's hidden state.
    """

    if status not in {GROUP_EDIT_OK, GROUP_EDIT_INVALID_NAME}:
        raise ValueError(f"invalid GroupEditResponse status {status}")
    writer = _server_writer(GROUP_EDIT_RESULT_COMMAND)
    writer.write_uint8(status)
    writer.write_uint32_le(int(field_1c) & 0xFFFFFFFF)
    writer.write_uint32_le(int(request.field_20) & 0xFFFFFFFF)
    writer.write_string(request.source_name)
    writer.write_string(request.name)
    writer.write_bool(bool(request.flag))
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_group_edit_result(payload: bytes, bit_length: int):
    """Decode into ``(status, field_1c, SocialEditRequest)``."""

    reader = _server_reader(payload, bit_length, GROUP_EDIT_RESULT_COMMAND)
    status, field_1c = reader.read_uint8(), reader.read_uint32_le()
    request = SocialEditRequest(
        reader.read_uint32_le(), reader.read_string(), reader.read_string(),
        reader.read_bool(),
    )
    _finish_server(reader, "GroupEditResponse")
    return status, field_1c, request


def encode_guild_name_edit_result(
    status: int, name: str, *, source_name: str = "", field_1c: int = 0,
    field_60: int = 0, flag: bool = False,
) -> tuple[bytes, int]:
    """Encode GuildNameEditResponseCommand; no actor context.

    Layout ``enum8(-1..4), uint32, string, uint32, string, bool`` (+0x18,
    +0x1c, +0x20, +0x60, +0x68, +0xa8).  The handler formats its message
    with the name at +0x68 and reads nothing else besides the status.
    """

    if not GUILD_RENAME_OK <= int(status) <= GUILD_RENAME_NO_CHANGE:
        raise ValueError(f"invalid GuildNameEditResponse status {status}")
    writer = _server_writer(GUILD_NAME_EDIT_RESULT_COMMAND)
    writer.write_uint8(int(status))
    writer.write_uint32_le(int(field_1c) & 0xFFFFFFFF)
    writer.write_string(source_name)
    writer.write_uint32_le(int(field_60) & 0xFFFFFFFF)
    writer.write_string(name)
    writer.write_bool(bool(flag))
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_guild_name_edit_result(payload: bytes, bit_length: int):
    """Decode into ``(status, field_1c, source_name, field_60, name, flag)``."""

    reader = _server_reader(payload, bit_length, GUILD_NAME_EDIT_RESULT_COMMAND)
    value = (
        reader.read_uint8(), reader.read_uint32_le(), reader.read_string(),
        reader.read_uint32_le(), reader.read_string(), reader.read_bool(),
    )
    _finish_server(reader, "GuildNameEditResponse")
    return value


def encode_group_candidates(
    field_20: int, members, *, actor_id: int,
) -> tuple[bytes, int]:
    """Encode GroupCandidatesResponseCommand (actor-scoped).

    Layout ``uint32`` then the 230-byte PlayerSummary array reader shared
    with GroupStatus (0x140a3da98), then the outer actor context.  The
    handler (0x1402771a8) passes only the array to
    SocialWidget::UpdateMultipleInvite.
    """

    writer = _server_writer(GROUP_CANDIDATES_RESULT_COMMAND)
    writer.write_uint32_le(int(field_20) & 0xFFFFFFFF)
    writer.write_uint32_le(len(members))
    for member in members:
        _write_player_summary(writer, member)
    writer.write_uint32_le(int(actor_id) & 0xFFFFFFFF)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_group_candidates(payload: bytes, bit_length: int):
    """Decode into ``(field_20, members, actor_id)``."""

    reader = _server_reader(payload, bit_length, GROUP_CANDIDATES_RESULT_COMMAND)
    field_20 = reader.read_uint32_le()
    members = tuple(
        _read_player_summary(reader) for _ in range(reader.read_uint32_le())
    )
    actor_id = reader.read_uint32_le()
    _finish_server(reader, "GroupCandidatesResponse")
    return field_20, members, actor_id


def encode_group_join_response(
    status: int, requester_id: int, group_id: int, leader_name: str,
    token: str = "", *, actor_id: int,
) -> tuple[bytes, int]:
    """Encode GroupJoinRequestResponseCommand to the requester (actor-scoped).

    Same body as the leader's request: ``enum8(-1..11), uint32, uint32,
    string, string`` followed by the outer actor context.  Statuses 4 and 6
    format the name at +0x30; 0 additionally makes the client re-request
    its GroupStatus.
    """

    if not 0 <= int(status) <= GROUP_JOIN_MAX_STATUS:
        raise ValueError(f"invalid GroupJoinRequestResponse status {status}")
    writer = _server_writer(GROUP_JOIN_RESPONSE_COMMAND)
    writer.write_uint8(int(status))
    writer.write_uint32_le(int(requester_id) & 0xFFFFFFFF)
    writer.write_uint32_le(int(group_id) & 0xFFFFFFFF)
    writer.write_string(leader_name)
    writer.write_string(token)
    writer.write_uint32_le(int(actor_id) & 0xFFFFFFFF)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_group_join_response(payload: bytes, bit_length: int):
    """Decode into ``(GroupJoinResponse, actor_id)``."""

    reader = _server_reader(payload, bit_length, GROUP_JOIN_RESPONSE_COMMAND)
    value = GroupJoinResponse(
        reader.read_uint8(), reader.read_uint32_le(), reader.read_uint32_le(),
        reader.read_string(), reader.read_string(),
    )
    actor_id = reader.read_uint32_le()
    _finish_server(reader, "GroupJoinRequestResponse")
    return value, actor_id


def encode_social_kick_notice(
    command: int, target_name: str, *, actor_id: int,
) -> tuple[bytes, int]:
    """Tell a removed member they were kicked (actor-scoped).

    The client registers handlers for both kick commands: GroupKickCommand
    (0x14027922c) shows ``youWereKicked`` and GuildKickCommand (0x14027b8fc)
    ``youWereKickedFromGuild``; neither reads the body, which keeps the
    request layout ``string, uint32, uint32``.
    """

    if command not in {GROUP_KICK_COMMAND, GUILD_KICK_COMMAND}:
        raise ValueError(f"invalid kick command 0x{command:04x}")
    writer = _server_writer(command)
    writer.write_string(target_name)
    writer.write_uint32_le(0)
    writer.write_uint32_le(0)
    writer.write_uint32_le(int(actor_id) & 0xFFFFFFFF)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_social_kick_notice(payload: bytes, bit_length: int, command: int):
    """Decode into ``(SocialKickRequest, actor_id)``."""

    reader = _server_reader(payload, bit_length, command)
    value = SocialKickRequest(
        reader.read_string(), reader.read_uint32_le(), reader.read_uint32_le(),
    )
    actor_id = reader.read_uint32_le()
    _finish_server(reader, "kick notice")
    return value, actor_id


def encode_ignored_status(
    names: tuple[str, ...], field_1c: int = 0,
) -> tuple[bytes, int]:
    """Encode a standalone IgnoredStatusCommand full list; no actor.

    Layout ``enum8(-1..3), uint32, string[]`` (array reader 0x14095ef6c).
    Type 0 replaces ClientSocialManager's ignore list (+0x120) and refreshes
    the social widget.  The uint32 is unread by the handler.
    """

    writer = _server_writer(IGNORED_STATUS_COMMAND)
    writer.write_uint8(IGNORED_STATUS_FULL_LIST)
    writer.write_uint32_le(int(field_1c) & 0xFFFFFFFF)
    writer.write_uint32_le(len(names))
    for name in names:
        writer.write_string(name)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_ignored_status(payload: bytes, bit_length: int):
    """Decode into ``(status, field_1c, names)``."""

    reader = _server_reader(payload, bit_length, IGNORED_STATUS_COMMAND)
    status, field_1c = reader.read_uint8(), reader.read_uint32_le()
    names = tuple(reader.read_string() for _ in range(reader.read_uint32_le()))
    _finish_server(reader, "IgnoredStatus")
    return status, field_1c, names


def _client_reader(
    payload: bytes, command_id: int, bit_length: int | None = None
) -> BitReader:
    reader = BitReader(payload, len(payload) * 8 if bit_length is None else bit_length)
    if reader.read_uint8() != 0x8B:
        raise ValueError("expected a client application command")
    actual = int.from_bytes(reader.read_bytes(2), "little")
    if actual != command_id:
        raise ValueError(f"expected command 0x{command_id:04x}, got 0x{actual:04x}")
    return reader


def _finish_byte_rounded(reader: BitReader, label: str) -> None:
    """Accept only the zero padding introduced by RakNet byte rounding."""

    if reader.remaining >= 8:
        raise ValueError(f"{label} has {reader.remaining} unread bits")
    if reader.remaining and reader.read_bits(reader.remaining):
        raise ValueError(f"{label} has non-zero padding")


@dataclass(frozen=True)
class FoundationRequest:
    field_20: int
    source_name: str
    name: str
    field_a8: bool

    @property
    def description(self) -> str:
        """Compatibility alias for callers from before the field was named."""

        return self.source_name


def decode_foundation_request(
    payload: bytes, command_id: int, bit_length: int | None = None
) -> FoundationRequest:
    reader = _client_reader(payload, command_id, bit_length)
    result = FoundationRequest(
        reader.read_uint32_le(),
        reader.read_string(),
        reader.read_string(),
        reader.read_bool(),
    )
    _finish_byte_rounded(reader, "foundation request")
    return result


@dataclass(frozen=True)
class GroupInviteRequest:
    """Client request to invite one named character into a group.

    The first string is the sending character name and the second is the
    target selected in the player-query/chat context UI. The server still
    treats the authenticated session as authoritative.
    """

    target_name: str
    source_name: str
    field_a0: int


def decode_group_invite(
    payload: bytes, bit_length: int | None = None
) -> GroupInviteRequest:
    reader = _client_reader(payload, GROUP_INVITE_COMMAND, bit_length)
    source_name = reader.read_string()
    target_name = reader.read_string()
    result = GroupInviteRequest(
        target_name,
        source_name,
        reader.read_uint32_le(),
    )
    _finish_byte_rounded(reader, "GroupInvite")
    return result


@dataclass(frozen=True)
class GuildInviteRequest:
    """Typed layout generated by ``GuildInviteCommand`` (0x00aa)."""

    target_name: str
    source_name: str
    field_a0: int
    field_a4: int
    message: str


def decode_guild_invite(
    payload: bytes, bit_length: int | None = None
) -> GuildInviteRequest:
    reader = _client_reader(payload, GUILD_INVITE_COMMAND, bit_length)
    # Like GroupInviteCommand, the authenticated sender is serialized first
    # and the selected recipient second.  Treating the first string as the
    # target turns every real guild invitation into a self-invite.
    source_name = reader.read_string()
    target_name = reader.read_string()
    result = GuildInviteRequest(
        target_name,
        source_name,
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_string(),
    )
    _finish_byte_rounded(reader, "GuildInvite")
    return result


@dataclass(frozen=True)
class InvitationResponseRequest:
    """Client accept/decline response for a group or guild invitation."""

    status: int
    inviter_character_id: int
    collective_id: int
    player_name: str


def decode_invitation_response(
    payload: bytes,
    command_id: int,
    bit_length: int | None = None,
) -> InvitationResponseRequest:
    if command_id not in {
        GROUP_INVITATION_RESPONSE_COMMAND,
        GUILD_INVITATION_RESPONSE_COMMAND,
    }:
        raise ValueError(f"invalid invitation response id 0x{command_id:04x}")
    reader = _client_reader(payload, command_id, bit_length)
    result = InvitationResponseRequest(
        reader.read_uint8(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_string(),
    )
    _finish_byte_rounded(reader, "invitation response")
    return result


def decode_uint32_request(
    payload: bytes, command_id: int, bit_length: int | None = None
) -> int:
    reader = _client_reader(payload, command_id, bit_length)
    result = reader.read_uint32_le()
    _finish_byte_rounded(reader, f"command 0x{command_id:04x}")
    return result


@dataclass(frozen=True)
class PlayerSummary:
    field_00: int
    field_04: int
    character_id: int
    # This wire slot is a map-local actor id, not the durable account id.
    # Zero tells the social UI that the member is outside the viewer's map.
    account_id: int
    name: str
    map_or_guild: str
    class_id: int
    level: int
    region_or_power: int = 200
    field_54: int = 0
    field_58: int = 0
    online: bool = True
    available: bool = True
    group_invitable: bool = True
    role: int = 0xFF
    timestamp: str = "0000-00-00 00:00:00"
    relation: int = 0xFF
    note: str = ""
    field_88: int = 0xFFFFFFFF


def _write_player_summary(writer: BitWriter, value: PlayerSummary) -> None:
    for number in (
        value.field_00,
        value.field_04,
        value.character_id,
        value.account_id,
    ):
        writer.write_uint32_le(number)
    writer.write_string(value.name)
    writer.write_string(value.map_or_guild)
    writer.write_uint8(value.class_id)
    writer.write_bytes(int(value.level).to_bytes(2, "little"))
    writer.write_bytes(int(value.region_or_power).to_bytes(2, "little"))
    writer.write_uint32_le(value.field_54)
    writer.write_bytes(int(value.field_58).to_bytes(2, "little"))
    writer.write_bool(value.online)
    writer.write_bool(value.available)
    writer.write_bool(value.group_invitable)
    writer.write_uint8(value.role)
    writer.write_string(value.timestamp)
    writer.write_uint8(value.relation)
    writer.write_string(value.note)
    writer.write_uint32_le(value.field_88)


def _read_player_summary(reader: BitReader) -> PlayerSummary:
    return PlayerSummary(
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_string(),
        reader.read_string(),
        reader.read_uint8(),
        int.from_bytes(reader.read_bytes(2), "little"),
        int.from_bytes(reader.read_bytes(2), "little"),
        reader.read_uint32_le(),
        int.from_bytes(reader.read_bytes(2), "little"),
        reader.read_bool(),
        reader.read_bool(),
        reader.read_bool(),
        reader.read_uint8(),
        reader.read_string(),
        reader.read_uint8(),
        reader.read_string(),
        reader.read_uint32_le(),
    )


@dataclass(frozen=True)
class BuddyListRequest:
    action: int
    field_24: int
    name: str
    field_68: bool


def decode_buddy_list_request(
    payload: bytes, bit_length: int | None = None,
) -> BuddyListRequest:
    reader = _client_reader(payload, BUDDY_LIST_COMMAND, bit_length)
    value = BuddyListRequest(
        reader.read_uint8(), reader.read_uint32_le(), reader.read_string(),
        reader.read_bool(),
    )
    _finish_byte_rounded(reader, "BuddyList")
    return value


def encode_buddy_status(
    members: tuple[PlayerSummary, ...] = (), *, field_1c: int = 0,
) -> tuple[bytes, int]:
    """Full friends-list refresh; BuddyStatus is actorless and permits empty lists."""

    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(BUDDY_STATUS_COMMAND.to_bytes(2, "little"))
    writer.write_uint8(0)
    writer.write_uint32_le(field_1c)
    writer.write_uint32_le(len(members))
    for member in members:
        _write_player_summary(writer, member)
    writer.write_uint8(0xFF)
    return writer.finish()


@dataclass(frozen=True)
class PlayerQuery:
    query_kind: int
    query_id: int
    text: str


def decode_player_query(
    payload: bytes, bit_length: int | None = None
) -> PlayerQuery:
    reader = _client_reader(payload, PLAYER_QUERY_COMMAND, bit_length)
    result = PlayerQuery(
        reader.read_uint8(), reader.read_uint32_le(), reader.read_string()
    )
    _finish_byte_rounded(reader, "PlayerQuery")
    return result


def encode_player_query_result(
    query_id: int, results: tuple[PlayerSummary, ...]
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(PLAYER_QUERY_RESULT_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(query_id)
    writer.write_uint32_le(len(results))
    for result in results:
        _write_player_summary(writer, result)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_player_query_result(
    payload: bytes, bit_length: int
) -> tuple[int, tuple[PlayerSummary, ...]]:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("PlayerQueryResult is not a server batch")
    if int.from_bytes(reader.read_bytes(2), "little") != PLAYER_QUERY_RESULT_COMMAND:
        raise ValueError("wrong PlayerQueryResult command")
    query_id = reader.read_uint32_le()
    results = tuple(
        _read_player_summary(reader) for _ in range(reader.read_uint32_le())
    )
    if reader.read_uint8() != 0xFF or reader.remaining:
        raise ValueError("PlayerQueryResult has trailing data")
    return query_id, results


@dataclass(frozen=True)
class GroupFinderQuery:
    category: int
    field_24: int
    category_mask: bytes


def decode_group_finder_query(
    payload: bytes, bit_length: int | None = None
) -> GroupFinderQuery:
    reader = _client_reader(payload, GROUP_FINDER_QUERY_COMMAND, bit_length)
    category = reader.read_uint8()
    field_24 = reader.read_uint32_le()
    mask_size = int.from_bytes(reader.read_bytes(2), "little")
    if mask_size != 16:
        raise ValueError(
            f"GroupFinderQuery category mask must be 16 bytes, got {mask_size}"
        )
    result = GroupFinderQuery(
        category, field_24, reader.read_bytes(mask_size)
    )
    _finish_byte_rounded(reader, "GroupFinderQuery")
    return result


@dataclass(frozen=True)
class GroupFinderEntry:
    group_id: int
    category: int
    name: str
    map_id: str
    member_count: int
    field_68: int


def encode_group_finder_result(
    server_tick: int, entries: tuple[GroupFinderEntry, ...]
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(GROUP_FINDER_RESULT_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(server_tick)
    writer.write_uint32_le(len(entries))
    for entry in entries:
        writer.write_uint32_le(entry.group_id)
        writer.write_bytes(int(entry.category).to_bytes(2, "little"))
        writer.write_string(entry.name)
        writer.write_string(entry.map_id)
        writer.write_uint32_le(entry.member_count)
        writer.write_uint32_le(entry.field_68)
    writer.write_uint8(0xFF)
    return writer.finish()


@dataclass(frozen=True)
class GroupStatus:
    status: int
    group_id: int
    owner_character_id: int
    # Retail fills this slot with the receiving character id.
    server_tick: int
    # GroupStatusCommand +0x28.  The handler (0x140279828) copies it to
    # ClientSocialManager +0x168, the receiver's leader-rights flag that
    # EditGroup, GroupKickPlayer and AcceptGroupJoinRequest check before
    # answering ``leaderRightsRequired``/``playerNotLeader`` locally.
    leader_rights: bool
    members: tuple[PlayerSummary, ...]
    name: str
    # +0x58 is copied to the manager's hidden-group flag (+0x1fc), the same
    # field GroupEditResponse's bool updates.
    private: bool
    description: str

    @property
    def listed(self) -> bool:
        """Compatibility alias for the leader-rights slot's earlier name."""

        return self.leader_rights


def encode_group_status(value: GroupStatus) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(GROUP_STATUS_COMMAND.to_bytes(2, "little"))
    writer.write_uint8(value.status)
    writer.write_uint32_le(value.group_id)
    writer.write_uint32_le(value.owner_character_id)
    writer.write_uint32_le(value.server_tick)
    writer.write_bool(value.leader_rights)
    writer.write_uint32_le(len(value.members))
    for member in value.members:
        _write_player_summary(writer, member)
    writer.write_string(value.name)
    writer.write_bool(value.private)
    writer.write_string(value.description)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_group_status(payload: bytes, bit_length: int) -> GroupStatus:
    """Decode a retail/server ``GroupStatusCommand`` snapshot."""

    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("GroupStatus is not a server batch")
    if int.from_bytes(reader.read_bytes(2), "little") != GROUP_STATUS_COMMAND:
        raise ValueError("wrong GroupStatus command")
    value = GroupStatus(
        reader.read_uint8(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_bool(),
        tuple(
            _read_player_summary(reader)
            for _ in range(reader.read_uint32_le())
        ),
        reader.read_string(),
        reader.read_bool(),
        reader.read_string(),
    )
    if reader.read_uint8() != 0xFF or reader.remaining:
        raise ValueError("GroupStatus has trailing data")
    return value


def encode_create_group_result(
    status: int, server_tick: int, message: str = ""
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(CREATE_GROUP_RESULT_COMMAND.to_bytes(2, "little"))
    writer.write_uint8(status)
    writer.write_uint32_le(server_tick)
    writer.write_string(message)
    writer.write_uint8(0xFF)
    return writer.finish()


def encode_invitation_response(
    command_id: int,
    status: int,
    player_name: str,
    field_24: int = 0,
    field_28: int = 0,
    actor_id: int = 0,
) -> tuple[bytes, int]:
    """Encode the common group/guild invitation-result wire layout.

    The generated serializers write an 8-bit response enum, two uint32
    command fields, then the affected player's name. Unlike the surrounding
    actorless social-status commands, this generated type also requires the
    outer server-command actor context before the batch terminator.
    """

    if command_id not in {
        GROUP_INVITATION_RESPONSE_COMMAND,
        GUILD_INVITATION_RESPONSE_COMMAND,
    }:
        raise ValueError(f"invalid invitation response id 0x{command_id:04x}")
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(command_id.to_bytes(2, "little"))
    writer.write_uint8(status)
    writer.write_uint32_le(field_24)
    writer.write_uint32_le(field_28)
    writer.write_string(player_name)
    writer.write_uint32_le(actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def encode_grouping_failed(
    reason: int,
    player_name: str = "",
    server_tick: int = 0,
) -> tuple[bytes, int]:
    """Encode ``GroupingFailedCommand`` (0x00a3).

    The generated serializer writes a uint32, the affected player's name, and
    an eight-bit reason enum, in that order.  Unlike the invitation-response
    pair this type does not override the outer server-command actor accessors,
    so no actor context follows the body -- adding one shifts the batch
    terminator and the client discards the command.

    ``GroupInvitationResponseCommand`` is not a substitute: its failure codes
    are reported to the *inviter*, while this command is what tells the player
    who accepted why the join did not happen.
    """

    if reason not in {
        GROUPING_FAILED_GROUP_IS_FULL,
        GROUPING_FAILED_ALREADY_IN_A_GROUP,
        GROUPING_FAILED_REQUEST_EXPIRED,
        GROUPING_FAILED_NO_GROUP,
    }:
        raise ValueError(f"invalid GroupingFailed reason {reason}")
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(GROUPING_FAILED_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(int(server_tick) & 0xFFFFFFFF)
    writer.write_string(player_name)
    writer.write_uint8(reason)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_grouping_failed(
    payload: bytes, bit_length: int
) -> tuple[int, str, int]:
    """Decode ``GroupingFailedCommand`` into ``(reason, name, tick)``."""

    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("GroupingFailed is not a server batch")
    if (
        int.from_bytes(reader.read_bytes(2), "little")
        != GROUPING_FAILED_COMMAND
    ):
        raise ValueError("wrong GroupingFailed command")
    server_tick = reader.read_uint32_le()
    player_name = reader.read_string()
    reason = reader.read_uint8()
    if reader.read_uint8() != 0xFF or reader.remaining:
        raise ValueError("GroupingFailed has trailing data")
    return reason, player_name, server_tick


@dataclass(frozen=True)
class GroupMemberQuery:
    """Client request for one group's live member roster (0x00b8).

    Both fields are uint32. Native query builder 0x14027245c sets +0x24
    to the group selected in the finder popup, independently of membership.
    +0x20 is the common request context and is echoed in the response.
    """

    field_20: int
    field_24: int

    @property
    def group_id(self) -> int:
        return self.field_24


def decode_group_member_query(
    payload: bytes, bit_length: int | None = None
) -> GroupMemberQuery:
    reader = _client_reader(payload, GROUP_MEMBER_QUERY_COMMAND, bit_length)
    result = GroupMemberQuery(
        reader.read_uint32_le(), reader.read_uint32_le()
    )
    _finish_byte_rounded(reader, "GroupMemberQuery")
    return result


def encode_group_member_query_result(
    group_id: int,
    members: tuple[PlayerSummary, ...],
    *,
    status: int = GROUP_MEMBER_QUERY_OK,
    field_1c: int = 0,
) -> tuple[bytes, int]:
    """Encode ``GroupMemberQueryResultCommand`` (0x00b9).

    A successful response must match the popup's SELECTED group, which need
    not be the receiver's own group, and contain members. Failure status 1
    permits an empty roster and makes the native handler close the popup.
    The elements share the PlayerSummary layout used by GroupStatus.
    """

    if status not in {GROUP_MEMBER_QUERY_OK, GROUP_MEMBER_QUERY_FAILED}:
        raise ValueError(f"invalid GroupMemberQueryResult status {status}")
    if not 0 <= int(group_id) <= 0xFFFFFFFF:
        raise ValueError("GroupMemberQueryResult group_id must be a uint32")
    if status == GROUP_MEMBER_QUERY_OK and (not group_id or not members):
        raise ValueError("successful GroupMemberQueryResult requires a group and members")
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(
        GROUP_MEMBER_QUERY_RESULT_COMMAND.to_bytes(2, "little")
    )
    writer.write_uint8(status)
    writer.write_uint32_le(int(field_1c) & 0xFFFFFFFF)
    writer.write_uint32_le(int(group_id))
    writer.write_uint32_le(len(members))
    for member in members:
        _write_player_summary(writer, member)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_group_member_query_result(
    payload: bytes, bit_length: int
) -> tuple[int, int, int, tuple[PlayerSummary, ...]]:
    """Decode into ``(status, field_1c, group_id, members)``."""

    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("GroupMemberQueryResult is not a server batch")
    if (
        int.from_bytes(reader.read_bytes(2), "little")
        != GROUP_MEMBER_QUERY_RESULT_COMMAND
    ):
        raise ValueError("wrong GroupMemberQueryResult command")
    status = reader.read_uint8()
    field_1c = reader.read_uint32_le()
    group_id = reader.read_uint32_le()
    members = tuple(
        _read_player_summary(reader) for _ in range(reader.read_uint32_le())
    )
    if reader.read_uint8() != 0xFF or reader.remaining:
        raise ValueError("GroupMemberQueryResult has trailing data")
    return status, field_1c, group_id, members


def encode_guild_foundation_result(
    status: int,
    server_tick: int,
    message: str = "",
    guild_name: str = "",
    field_a0: bool = False,
) -> tuple[bytes, int]:
    """Encode ``GuildFoundationResponseCommand`` (0x00a9).

    The client closes the guild-foundation progress form only after receiving
    this command.  ``GuildStatusCommand`` updates the background guild panel
    but is not a substitute for the operation response.
    """

    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(CREATE_GUILD_RESULT_COMMAND.to_bytes(2, "little"))
    writer.write_uint8(status)
    writer.write_uint32_le(server_tick)
    writer.write_string(message)
    writer.write_string(guild_name)
    writer.write_bool(field_a0)
    writer.write_uint8(0xFF)
    return writer.finish()


@dataclass(frozen=True)
class GuildMember:
    player_id: int
    # Map-local actor id as seen by the receiving client; zero when absent.
    account_id: int
    name: str
    # The class byte precedes the level; the rank byte follows the three
    # flags.  The generated decoder stores them at +0x48 and +0x54, and
    # ``SocialWidget::CreateGuildListElement()`` switches +0x48 over the five
    # class icons (warrior/wizard/ranger/dwarf/niwalk) while rendering +0x54
    # through the three-value rank enum.  Only the rank slot is validated: a
    # value outside -1..2 aborts ``GuildStatusCommand::Decode``.
    class_id: int
    level: int
    online: bool  # Native +0x50 controls member-name styling.
    field_51: bool  # Native +0x51 controls the online/offline activity icon.
    field_52: bool
    rank: int
    timestamp_parts: tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class GuildStatus:
    status: int
    guild_id: int
    name: str
    # Retail fills this slot with the receiving character id.
    server_tick: int
    field_64: bool
    field_68: int
    field_6c: int
    members: tuple[GuildMember, ...]
    ignored_names: tuple[str, ...] = ()


def encode_guild_status(value: GuildStatus) -> tuple[bytes, int]:
    """Encode the retail GuildStatus + IgnoredStatus compound response."""

    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(GUILD_STATUS_COMMAND.to_bytes(2, "little"))
    writer.write_uint8(value.status)
    writer.write_uint32_le(value.guild_id)
    writer.write_string(value.name)
    writer.write_uint32_le(value.server_tick)
    writer.write_bool(value.field_64)
    writer.write_uint32_le(value.field_68)
    writer.write_uint32_le(value.field_6c)
    writer.write_uint32_le(len(value.members))
    for member in value.members:
        if member.rank not in {GUILD_RANK_MEMBER, GUILD_RANK_OFFICER,
                               GUILD_RANK_LEADER}:
            raise ValueError(
                f"guild rank must be 0, 1, or 2; got {member.rank}"
            )
        writer.write_uint32_le(member.player_id)
        writer.write_uint32_le(member.account_id)
        writer.write_string(member.name)
        writer.write_uint8(member.class_id)
        writer.write_bytes(int(member.level).to_bytes(2, "little"))
        writer.write_bool(member.online)
        writer.write_bool(member.field_51)
        writer.write_bool(member.field_52)
        writer.write_uint8(member.rank)
        if len(member.timestamp_parts) != 6:
            raise ValueError("guild timestamp must contain six parts")
        for part in member.timestamp_parts:
            writer.write_uint32_le(part)
    # Retail never sends A7 alone. This client continues decoding at the
    # command marker and requires the paired B3 IgnoredStatus snapshot.
    writer.write_uint8(0xFF)
    writer.write_bytes(IGNORED_STATUS_COMMAND.to_bytes(2, "little"))
    writer.write_uint8(0)
    writer.write_uint32_le(value.server_tick)
    writer.write_uint32_le(len(value.ignored_names))
    for name in value.ignored_names:
        writer.write_string(name)
    writer.write_uint8(0xFF)
    return writer.finish()


def decode_guild_status(payload: bytes, bit_length: int) -> GuildStatus:
    """Decode a retail/server ``GuildStatusCommand`` snapshot."""

    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("GuildStatus is not a server batch")
    if int.from_bytes(reader.read_bytes(2), "little") != GUILD_STATUS_COMMAND:
        raise ValueError("wrong GuildStatus command")
    status = reader.read_uint8()
    guild_id = reader.read_uint32_le()
    name = reader.read_string()
    server_tick = reader.read_uint32_le()
    field_64 = reader.read_bool()
    field_68 = reader.read_uint32_le()
    field_6c = reader.read_uint32_le()
    members = []
    for _ in range(reader.read_uint32_le()):
        # Positional order matches the encoder: id, actor, name, class,
        # level, three flags, rank, timestamp.
        members.append(
            GuildMember(
                reader.read_uint32_le(),
                reader.read_uint32_le(),
                reader.read_string(),
                reader.read_uint8(),
                int.from_bytes(reader.read_bytes(2), "little"),
                reader.read_bool(),
                reader.read_bool(),
                reader.read_bool(),
                reader.read_uint8(),
                tuple(reader.read_uint32_le() for _ in range(6)),
            )
        )
    if reader.read_uint8() != 0xFF:
        raise ValueError("GuildStatus is not followed by a command marker")
    if (
        int.from_bytes(reader.read_bytes(2), "little")
        != IGNORED_STATUS_COMMAND
    ):
        raise ValueError("GuildStatus is not followed by IgnoredStatus")
    if reader.read_uint8() != 0:
        raise ValueError("local IgnoredStatus has an invalid status")
    if reader.read_uint32_le() != server_tick:
        raise ValueError("GuildStatus/IgnoredStatus ticks do not match")
    ignored_names = tuple(reader.read_string() for _ in range(reader.read_uint32_le()))
    if reader.read_uint8() != 0xFF or reader.remaining:
        raise ValueError("GuildStatus compound response has trailing data")
    return GuildStatus(
        status,
        guild_id,
        name,
        server_tick,
        field_64,
        field_68,
        field_6c,
        tuple(members),
        ignored_names,
    )


@dataclass(frozen=True)
class RemotePlayer:
    actor_id: int
    name: str
    guild_name: str
    class_id: int
    gender: int
    hair_style: int
    beard_style: int
    body_style: int
    body_stature: int
    template_row: int
    position: tuple[float, float, float]
    heading: float
    character_id: int
    account_id: int
    level: int
    admin_privileges: bool = False
    admin_name_color: bool = False
    equipment: tuple["RemoteEquipmentVisual", ...] = ()
    emblem_id: str = ""
    permanent_statuses: tuple[str, ...] = ()
    animation_set: str = ""
    additional_sequences: tuple[str, ...] = ()
    fighting_power: int = 0
    title_type: int = 0
    achievement_title: str = ""
    pvp_rank_title: str = ""
    honor_points: int = 0
    team: int = 0xFFFFFFFF

    @property
    def faction_id(self) -> int:
        # Retail assigns every remote player a separate non-owned faction
        # identity.  In the observed city capture it is consistently 0xae
        # below the actor id (0x1030d/0x103bb and 0x10312/0x103c0).
        faction_id = int(self.actor_id) - 0xAE
        if faction_id <= 0:
            raise ValueError("remote actor id is too small for a faction id")
        return faction_id


@dataclass(frozen=True)
class RemoteEquipmentVisual:
    slot: int
    skins: tuple[str, ...]
    color: tuple[float, float, float, float] | None = None


def _write_remote_equipment(
    writer: BitWriter, equipment: tuple[RemoteEquipmentVisual, ...]
) -> None:
    writer.write_uint32_le(len(equipment))
    for visual in equipment:
        writer.write_uint8(visual.slot)
        writer.write_uint32_le(len(visual.skins))
        for skin in visual.skins:
            writer.write_string(skin)
        writer.write_bool(visual.color is not None)
        if visual.color is not None:
            if len(visual.color) != 4:
                raise ValueError("remote equipment color requires four floats")
            for component in visual.color:
                writer.write_float(component)


def _remote_title_fields(value: RemotePlayer) -> tuple[int, str, str]:
    """Return a localization-safe remote-player title selection.

    The client resolves the two strings only for their matching display mode.
    Advertising achievement mode with an empty achievement id produces the
    literal ``LF:db.achievementtitles.table_Title`` placeholder.  PvP mode,
    meanwhile, still needs a valid honor-level id even for an unranked player.
    """

    title_type = max(0, min(2, int(value.title_type)))
    achievement_title = str(value.achievement_title or "").strip()
    pvp_rank_title = str(value.pvp_rank_title or "").strip()
    if title_type == 1 and not achievement_title:
        title_type = 0
    if title_type == 2 and not pvp_rank_title:
        pvp_rank_title = honor_level_title_for(value.honor_points)
    return title_type, achievement_title, pvp_rank_title


def encode_new_remote_player(value: RemotePlayer) -> tuple[bytes, int]:
    """Encode the fully typed, equipment-empty NewRemotePlayer branch.

    Equipment and status arrays are deliberately valid empty arrays. They are
    not omitted and can be extended with RemotePlayerInfo as multiplayer
    equipment synchronization grows.
    """

    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(NEW_REMOTE_PLAYER_COMMAND.to_bytes(2, "little"))
    for flag in (
        True,
        value.admin_privileges,
        value.admin_name_color,
        False,
        True,
    ):
        writer.write_bool(flag)
    # +0x28 is not the player's class. The class is appearance[0] below.
    # NewRemotePlayer's handler forwards +0x2c directly to
    # SetGameEntityVisible::SetVisible(), so locally announced peers must set
    # it true or the successfully registered actor remains hidden.
    writer.write_uint32_le(0)
    writer.write_bool(True)
    for component in value.position:
        writer.write_float(component)
    writer.write_float(value.heading)
    writer.write_string(value.name)
    writer.write_string(value.guild_name)
    for number in (
        value.class_id,
        value.gender,
        value.hair_style,
        value.beard_style,
        value.body_style,
        value.body_stature,
    ):
        writer.write_uint8(number)
    writer.write_uint32_le(value.template_row)
    _write_remote_equipment(writer, value.equipment)
    writer.write_uint32_le(len(value.permanent_statuses))
    for status_id in value.permanent_statuses:
        writer.write_string(status_id)
    writer.write_uint64_le(value.character_id)
    writer.write_uint64_le(value.account_id)
    writer.write_uint32_le(value.level)
    writer.write_uint32_le(value.level)
    # The client decodes this as a validated int8 enum with accepted values
    # -1..4. Value 5 makes NewRemotePlayerCommand::Decode fail, so use the
    # valid remote-player kind used by this client build.
    writer.write_uint8(4)
    # A remote player must have a distinct, non-owned ActorPlacement.
    # Reusing the local player's 0x10002 faction and setting owned=True makes
    # the client treat movement as locally controlled player state.
    writer.write_uint32_le(value.faction_id)
    writer.write_uint32_le(value.actor_id)
    # ActorPlacement serializes faction id, actor id, a validated int8 enum,
    # then two bit-packed flags.
    writer.write_uint8(0)
    writer.write_bool(False)
    writer.write_bool(False)
    writer.write_uint32_le(0)
    # +0x150 is copied into GamePlayer::level before SetupPlayerAttributes.
    # Zero makes setup fail even when the visible level field is non-zero.
    writer.write_uint32_le(max(1, value.level))
    writer.write_uint32_le(value.team & 0xFFFFFFFF)
    writer.write_uint32_le(max(0, int(value.honor_points)))
    title_type, achievement_title, pvp_rank_title = _remote_title_fields(value)
    writer.write_uint8(title_type)
    writer.write_string(achievement_title)
    writer.write_string(pvp_rank_title)
    # The client's final identity string sets the remote player's emblem
    # icon (Messaging::SetEmblemIcon::SetEmblemId).
    writer.write_string(str(value.emblem_id or ""))
    writer.write_uint32_le(value.actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def encode_remote_player_info(value: RemotePlayer) -> tuple[bytes, int]:
    """Encode the valid minimal RemotePlayerInfo generated branch.

    The two collection fields are present as typed arrays. The remaining
    fields follow this client's generated serializer, including three
    trailing strings and the observer actor context.
    """

    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(REMOTE_PLAYER_INFO_COMMAND.to_bytes(2, "little"))
    # +0x20 updates GamePlayer::guildName, not the character name.
    writer.write_string(value.guild_name)
    _write_remote_equipment(writer, value.equipment)
    writer.write_string(value.animation_set)
    writer.write_uint32_le(len(value.additional_sequences))
    for sequence in value.additional_sequences:
        writer.write_string(sequence)
    writer.write_uint32_le(0)  # attribute type 2
    writer.write_uint32_le(max(1, value.level))  # attribute type 5
    # Match NewRemotePlayer's validated player-kind enum (-1..4).
    writer.write_uint8(4)
    writer.write_uint32_le(value.faction_id)
    writer.write_uint32_le(value.actor_id)
    writer.write_uint8(0)
    writer.write_bool(False)
    writer.write_bool(False)
    writer.write_uint32_le(max(1, value.level))
    writer.write_uint32_le(value.team & 0xFFFFFFFF)
    title_type, achievement_title, pvp_rank_title = _remote_title_fields(value)
    writer.write_uint8(title_type)
    writer.write_string(achievement_title)
    writer.write_string(pvp_rank_title)
    # The client dispatches this string to SetEmblemIcon::SetEmblemId.
    writer.write_string(str(value.emblem_id or ""))
    writer.write_uint32_le(value.actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def encode_discard_player(actor_id: int) -> tuple[bytes, int]:
    """Remove one remote player and its actor from client registries.

    ``ActorsLeftVicinity`` only changes visibility. Retail map departures use
    command 0x001e so a later ``NewRemotePlayer`` can safely reuse the actor's
    low ActorManager index with a new generation.
    """

    if not 0 < int(actor_id) <= 0xFFFFFFFF:
        raise ValueError("DiscardPlayer actor_id must be a non-zero uint32")
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(DISCARD_PLAYER_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(int(actor_id))
    writer.write_uint8(0xFF)
    return writer.finish()


@dataclass(frozen=True)
class UpdateShowPlayerOverheadIcons:
    visible: bool
    actor_id: int


def decode_update_show_player_overhead_icons(
    payload: bytes, bit_length: int | None = None,
) -> UpdateShowPlayerOverheadIcons:
    """Decode the settings request: one bool and the local player's actor id.

    Native decoder 0x14093edb0 reads +0x20/+0x24. The settings sender at
    0x1401b81f0 fills +0x24 from GamePlayer's actor id. Client requests omit
    the additional ServerCommand actor context present in server replies.
    """

    reader = _client_reader(
        payload, UPDATE_SHOW_PLAYER_OVERHEAD_ICONS_COMMAND, bit_length
    )
    value = UpdateShowPlayerOverheadIcons(
        reader.read_bool(), reader.read_uint32_le()
    )
    _finish_byte_rounded(reader, "UpdateShowPlayerOverheadIcons")
    return value


def encode_update_show_player_overhead_icons(
    visible: bool, *, actor_id: int
) -> tuple[bytes, int]:
    """Refresh a player's overhead UI with both native actor fields."""

    if not 0 < int(actor_id) <= 0xFFFFFFFF:
        raise ValueError("overhead-icons actor_id must be a non-zero uint32")
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(
        UPDATE_SHOW_PLAYER_OVERHEAD_ICONS_COMMAND.to_bytes(2, "little")
    )
    writer.write_bool(bool(visible))
    writer.write_uint32_le(int(actor_id))
    # Generic ServerCommand additional data follows the native command's own
    # actor field. Omitting this second actor context makes the client reject
    # command 35 after enabling PvP overhead UI.
    writer.write_uint32_le(int(actor_id))
    writer.write_uint8(0xFF)
    return writer.finish()


def encode_actors_left(actor_ids: tuple[int, ...]) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(ACTORS_LEFT_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(len(actor_ids))
    for actor_id in actor_ids:
        writer.write_uint32_le(actor_id)
    writer.write_uint32_le(0)  # DrasaClientHandler actor context
    writer.write_uint8(0xFF)
    return writer.finish()
