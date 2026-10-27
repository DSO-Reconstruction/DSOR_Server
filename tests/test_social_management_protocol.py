"""Group/guild management and ignore-list codecs follow the client layouts.

Every expected layout below is the release261_1024 generated Decode of the
command class (vtable slot 5), read independently of the module under test:
the spec readers/writers here only know the primitive order the client uses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dso_server.protocol.character_selection import BitReader, BitWriter
from dso_server.protocol import social_protocol as sp
from dso_server.protocol.social_protocol import (
    GROUP_CANDIDATES_COMMAND,
    GROUP_CANDIDATES_RESULT_COMMAND,
    GROUP_EDIT_COMMAND,
    GROUP_EDIT_RESULT_COMMAND,
    GROUP_JOIN_REQUEST_COMMAND,
    GROUP_JOIN_RESPONSE_COMMAND,
    GROUP_KICK_COMMAND,
    GROUP_MULTIPLE_INVITE_COMMAND,
    GUILD_KICK_COMMAND,
    GUILD_MOTD_CHANGE_COMMAND,
    GUILD_MOTD_COMMAND,
    GUILD_MOTD_REQUEST_COMMAND,
    GUILD_NAME_EDIT_COMMAND,
    GUILD_NAME_EDIT_RESULT_COMMAND,
    GroupStatus,
    GuildStatus,
    IGNORED_LIST_COMMAND,
    IGNORED_STATUS_COMMAND,
    PlayerSummary,
    SocialEditRequest,
    decode_group_status,
    decode_guild_status,
    decode_social_management,
    encode_group_candidates,
    encode_group_edit_result,
    encode_group_join_response,
    encode_group_status,
    encode_guild_motd,
    encode_guild_name_edit_result,
    encode_guild_status,
    encode_ignored_status,
    encode_social_kick_notice,
)


DOCS = Path(__file__).resolve().parents[1] / "docs"


def _client(command: int, spec) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(command.to_bytes(2, "little"))
    for kind, value in spec:
        if kind == "u8":
            writer.write_uint8(value)
        elif kind == "u32":
            writer.write_uint32_le(value)
        elif kind == "str":
            writer.write_string(value)
        elif kind == "bool":
            writer.write_bool(value)
        elif kind == "str[]":
            writer.write_uint32_le(len(value))
            for item in value:
                writer.write_string(item)
        elif kind == "u32[]":
            writer.write_uint32_le(len(value))
            for item in value:
                writer.write_uint32_le(item)
        else:
            raise AssertionError(kind)
    return writer.finish()


_READ = {
    "u8": lambda r: r.read_uint8(),
    "u32": lambda r: r.read_uint32_le(),
    "str": lambda r: r.read_string(),
    "bool": lambda r: r.read_bool(),
    "str[]": lambda r: tuple(r.read_string() for _ in range(r.read_uint32_le())),
    "summary[]": lambda r: tuple(
        sp._read_player_summary(r) for _ in range(r.read_uint32_le())
    ),
}


def _server(packet, command: int, spec, *, actor: bool):
    data, bits = packet
    reader = BitReader(data, bits)
    assert reader.read_uint8() == 0x85
    assert int.from_bytes(reader.read_bytes(2), "little") == command
    values = [_READ[kind](reader) for kind in spec]
    actor_id = reader.read_uint32_le() if actor else None
    assert reader.read_uint8() == 0xFF
    assert reader.remaining == 0
    return values, actor_id


# -- client -> server (Decode order verified by the coordinator) ----------------

@pytest.mark.parametrize("command", [GROUP_KICK_COMMAND, GUILD_KICK_COMMAND])
def test_kick_requests_are_string_uint32_uint32(command):
    payload, bits = _client(command, [("str", "Victim"), ("u32", 7), ("u32", 9)])
    request = decode_social_management(payload, command, bits)
    assert request == sp.SocialKickRequest("Victim", 7, 9)


@pytest.mark.parametrize("command", [GROUP_EDIT_COMMAND, GUILD_NAME_EDIT_COMMAND])
def test_edit_requests_are_uint32_string_string_bool(command):
    payload, bits = _client(
        command, [("u32", 0xFFFFFFFF), ("str", "Leader"), ("str", "New name"), ("bool", True)]
    )
    request = decode_social_management(payload, command, bits)
    assert request == SocialEditRequest(0xFFFFFFFF, "Leader", "New name", True)


def test_ignored_list_operation_is_a_validated_enum():
    for operation in (0, 1, 2):
        payload, bits = _client(
            IGNORED_LIST_COMMAND, [("u8", operation), ("u32", 5), ("str", "Pest")]
        )
        request = decode_social_management(payload, IGNORED_LIST_COMMAND, bits)
        assert (request.operation, request.field_24, request.name) == (operation, 5, "Pest")
    payload, bits = _client(IGNORED_LIST_COMMAND, [("u8", 3), ("u32", 0), ("str", "")])
    with pytest.raises(ValueError):
        decode_social_management(payload, IGNORED_LIST_COMMAND, bits)


def test_guild_message_requests():
    payload, bits = _client(GUILD_MOTD_REQUEST_COMMAND, [("u32", 0xFFFFFFFF)])
    assert decode_social_management(payload, GUILD_MOTD_REQUEST_COMMAND, bits) == (
        sp.GuildMotdRequest(0xFFFFFFFF)
    )
    payload, bits = _client(GUILD_MOTD_CHANGE_COMMAND, [("u32", 1), ("str", "Raid at 8")])
    assert decode_social_management(payload, GUILD_MOTD_CHANGE_COMMAND, bits) == (
        sp.GuildMotdChange(1, "Raid at 8")
    )


def test_group_join_request_and_response():
    payload, bits = _client(
        GROUP_JOIN_REQUEST_COMMAND,
        [("str", "Asker"), ("str", "Party"), ("u32", 11), ("u32", 0), ("bool", True)],
    )
    assert decode_social_management(payload, GROUP_JOIN_REQUEST_COMMAND, bits) == (
        sp.GroupJoinRequest("Asker", "Party", 11, 0, True)
    )
    payload, bits = _client(
        GROUP_JOIN_RESPONSE_COMMAND,
        [("u8", 0), ("u32", 21), ("u32", 60000001), ("str", "Boss"), ("str", "tok")],
    )
    assert decode_social_management(payload, GROUP_JOIN_RESPONSE_COMMAND, bits) == (
        sp.GroupJoinResponse(0, 21, 60000001, "Boss", "tok")
    )
    payload, bits = _client(
        GROUP_JOIN_RESPONSE_COMMAND,
        [("u8", 12), ("u32", 0), ("u32", 0), ("str", ""), ("str", "")],
    )
    with pytest.raises(ValueError):
        decode_social_management(payload, GROUP_JOIN_RESPONSE_COMMAND, bits)


def test_multiple_invite_and_candidate_arrays():
    payload, bits = _client(
        GROUP_MULTIPLE_INVITE_COMMAND,
        [("str", "Leader"), ("str[]", ("A", "B", "C")), ("u32", 0)],
    )
    request = decode_social_management(payload, GROUP_MULTIPLE_INVITE_COMMAND, bits)
    assert request == sp.GroupMultipleInvite("Leader", ("A", "B", "C"), 0)
    payload, bits = _client(
        GROUP_CANDIDATES_COMMAND, [("u32", 33), ("bool", True), ("u32[]", (4, 5))]
    )
    request = decode_social_management(payload, GROUP_CANDIDATES_COMMAND, bits)
    assert request == sp.GroupCandidatesRequest(33, True, (4, 5))
    payload, bits = _client(
        GROUP_CANDIDATES_COMMAND, [("u32", 0), ("bool", False), ("u32[]", tuple(range(1001)))]
    )
    with pytest.raises(ValueError):
        decode_social_management(payload, GROUP_CANDIDATES_COMMAND, bits)


def test_trailing_data_and_wrong_ids_are_rejected():
    payload, bits = _client(GROUP_KICK_COMMAND, [("str", "x"), ("u32", 0), ("u32", 0), ("u8", 1)])
    with pytest.raises(ValueError):
        decode_social_management(payload, GROUP_KICK_COMMAND, bits)
    payload, bits = _client(GROUP_KICK_COMMAND, [("str", "x"), ("u32", 0), ("u32", 0)])
    with pytest.raises(ValueError):
        decode_social_management(payload, GUILD_KICK_COMMAND, bits)


# -- server -> client encoders --------------------------------------------------

def test_guild_message_of_the_day_is_string_uint32_without_actor():
    # Decode 0x140a414a8: string(->atom) +0x18, uint32 +0x20; actor_scoped=False.
    values, _ = _server(
        encode_guild_motd("Welcome", 50000001), GUILD_MOTD_COMMAND,
        ["str", "u32"], actor=False,
    )
    assert values == ["Welcome", 50000001]


def test_group_edit_response_echoes_the_request_without_actor():
    # Decode 0x140a406e8: enum8(-1..1), u32, u32, string, string, bool.
    request = SocialEditRequest(0xFFFFFFFF, "Leader", "Night raid", True)
    values, _ = _server(
        encode_group_edit_result(0, request), GROUP_EDIT_RESULT_COMMAND,
        ["u8", "u32", "u32", "str", "str", "bool"], actor=False,
    )
    assert values == [0, 0, 0xFFFFFFFF, "Leader", "Night raid", True]
    with pytest.raises(ValueError):
        encode_group_edit_result(2, request)


def test_guild_name_edit_response_layout_and_status_range():
    # Decode 0x140a415dc: enum8(-1..4), u32, string, u32, string, bool.
    values, _ = _server(
        encode_guild_name_edit_result(3, "Bad#Name", source_name="Lead", flag=True),
        GUILD_NAME_EDIT_RESULT_COMMAND,
        ["u8", "u32", "str", "u32", "str", "bool"], actor=False,
    )
    assert values == [3, 0, "Lead", 0, "Bad#Name", True]
    with pytest.raises(ValueError):
        encode_guild_name_edit_result(5, "x")


def test_group_candidates_response_has_summaries_and_actor():
    # Decode 0x140a405f4: u32 +0x20, PlayerSummary array (0x140a3da98); actor_scoped.
    member = PlayerSummary(0, 0, 7, 0x10007, "Friend", "a0200_kingscity", 2, 40)
    values, actor_id = _server(
        encode_group_candidates(99, (member,), actor_id=0x10002),
        GROUP_CANDIDATES_RESULT_COMMAND, ["u32", "summary[]"], actor=True,
    )
    assert values == [99, (member,)]
    assert actor_id == 0x10002


def test_group_join_response_layout_with_actor():
    # Decode 0x140a40a84: enum8(-1..11), u32, u32, string, string; actor_scoped.
    values, actor_id = _server(
        encode_group_join_response(11, 21, 60000001, "Boss", actor_id=0x10002),
        GROUP_JOIN_RESPONSE_COMMAND, ["u8", "u32", "u32", "str", "str"], actor=True,
    )
    assert values == [11, 21, 60000001, "Boss", ""]
    assert actor_id == 0x10002
    with pytest.raises(ValueError):
        encode_group_join_response(12, 0, 0, "", actor_id=1)


@pytest.mark.parametrize("command", [GROUP_KICK_COMMAND, GUILD_KICK_COMMAND])
def test_kick_notice_keeps_the_request_body_and_adds_the_actor(command):
    values, actor_id = _server(
        encode_social_kick_notice(command, "Victim", actor_id=0x10002),
        command, ["str", "u32", "u32"], actor=True,
    )
    assert values == ["Victim", 0, 0] and actor_id == 0x10002


def test_standalone_ignored_status_is_a_full_list_without_actor():
    # Decode 0x140a4181c: enum8(-1..3), u32, string array (0x14095ef6c).
    values, _ = _server(
        encode_ignored_status(("Alpha", "Beta"), 77), IGNORED_STATUS_COMMAND,
        ["u8", "u32", "str[]"], actor=False,
    )
    assert values == [0, 77, ("Alpha", "Beta")]


def test_guild_status_compound_carries_the_ignored_names():
    status = GuildStatus(0, 0, "", 5, False, 0, 0, (), ("Pest", "Troll"))
    decoded = decode_guild_status(*encode_guild_status(status))
    assert decoded.ignored_names == ("Pest", "Troll")


def test_group_status_first_flag_is_the_receivers_leader_rights():
    status = GroupStatus(0, 60000001, 1, 2, False, (), "Party", True, "")
    decoded = decode_group_status(*encode_group_status(status))
    assert decoded.leader_rights is False and decoded.listed is False
    assert decoded.private is True


def test_fallback_ids_match_the_current_client_table():
    table = json.loads((DOCS / "command_ids_rel261_1024.json").read_text(encoding="utf-8"))
    flat = {}

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, dict) and "id" in item:
                    flat[key] = item["id"]
                else:
                    walk(item)

    walk(table)
    expected = {
        "Commands::GroupMultipleInviteCommand": GROUP_MULTIPLE_INVITE_COMMAND,
        "Commands::GroupJoinRequestCommand": GROUP_JOIN_REQUEST_COMMAND,
        "Commands::GroupJoinRequestResponseCommand": GROUP_JOIN_RESPONSE_COMMAND,
        "Commands::GroupKickCommand": GROUP_KICK_COMMAND,
        "Commands::GroupEditCommand": GROUP_EDIT_COMMAND,
        "Commands::GroupEditResponseCommand": GROUP_EDIT_RESULT_COMMAND,
        "Commands::GroupCandidatesRequestCommand": GROUP_CANDIDATES_COMMAND,
        "Commands::GroupCandidatesResponseCommand": GROUP_CANDIDATES_RESULT_COMMAND,
        "Commands::GuildKickCommand": GUILD_KICK_COMMAND,
        "Commands::GuildNameEditCommand": GUILD_NAME_EDIT_COMMAND,
        "Commands::GuildNameEditResponseCommand": GUILD_NAME_EDIT_RESULT_COMMAND,
        "Commands::IgnoredListCommand": IGNORED_LIST_COMMAND,
        "Commands::IgnoredStatusCommand": IGNORED_STATUS_COMMAND,
        "Commands::GuildMessageOfTheDayCommand": GUILD_MOTD_COMMAND,
        "Commands::GuildMessageOfTheDayRequestCommand": GUILD_MOTD_REQUEST_COMMAND,
        "Commands::GuildMessageOfTheDayChangeCommand": GUILD_MOTD_CHANGE_COMMAND,
    }
    for name, value in expected.items():
        assert flat.get(name) == value, name
