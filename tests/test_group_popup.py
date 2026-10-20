"""Group finder inspection uses the selected party, not viewer membership."""

from dataclasses import FrozenInstanceError
import threading
from types import SimpleNamespace

import pytest

from dso_server.persistence.account_state import AccountStateStore, CharacterProfile
from dso_server.persistence.social_state import SocialStateStore, make_social_member
from dso_server.protocol.character_selection import BitReader, BitWriter
from dso_server.protocol.social_protocol import (
    BUDDY_LIST_COMMAND, BUDDY_STATUS_COMMAND,
    GROUP_MEMBER_QUERY_COMMAND, GROUP_MEMBER_QUERY_FAILED,
    GROUP_MEMBER_QUERY_RESULT_COMMAND, GROUP_STATUS_COMMAND,
    decode_buddy_list_request, decode_group_member_query,
    decode_group_member_query_result, encode_group_member_query_result,
)
from dso_server.runtime.transport import SessionRegistry
from dso_server.services.character_server import CharacterServer
from dso_server.services.login_game import LoginGameServer
from dso_server.tests.test_pvp_matchmaking import _session


RECORDED_BUDDY_REFRESH = bytes.fromhex("8bb0000000000000000000")


@pytest.fixture
def popup(tmp_path):
    social = SocialStateStore(tmp_path / "social.json")
    accounts = AccountStateStore(tmp_path / "players.sqlite3")
    group = social.create_group("Public party", "", "a0200_kingscity", make_social_member(10, "Leader", 0, 30))
    for character_id, name, class_id, level in (
        (11, "Old name", 1, 15), (12, "Remote", 2, 20),
        (13, "Loading", 3, 20), (14, "Offline fallback", 4, 18),
    ):
        social.add_group_member(group.group_id, 10, make_social_member(character_id, name, class_id, level))
    accounts.put_profile(CharacterProfile(
        11, "Saved name", "b1200_hub", 1, 0, 0, 0, 0, 0, level=72,
    ))
    server = CharacterServer.__new__(CharacterServer)
    server.service_name = "group-popup-test"
    server.social_store = social
    server.account_store = accounts
    server.sessions = SessionRegistry(threading.Lock())

    def connect(character_id, name, class_id, level):
        session = _session(53000 + character_id, character_id, name)
        session.gameplay_character.class_id = class_id
        session.gameplay_character.level = level
        session.gameplay_initialization_sent = True
        session.gameplay_presence_ready = True
        server.sessions[session.addr] = session
        return session

    viewer = connect(99, "Viewer", 1, 100)
    leader = connect(10, "Leader live", 0, 87)
    remote = connect(12, "Remote", 2, 61)
    remote.gameplay_map = "b1200_hub"
    loading = connect(13, "Loading", 3, 90)
    loading.gameplay_presence_ready = False
    yield SimpleNamespace(
        server=server, social=social, accounts=accounts, group_id=group.group_id,
        viewer=viewer, leader=leader, remote=remote, loading=loading,
    )
    accounts.close()


def request_bytes(group_id, context=0):
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(GROUP_MEMBER_QUERY_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(context)
    writer.write_uint32_le(group_id)
    return writer.finish()


def query(popup, group_id=None, context=0, server=None):
    p = popup
    payload, bits = request_bytes(p.group_id if group_id is None else group_id, context)
    p.viewer.current_message_bit_length = bits
    (server or p.server)._on_char_op(p.viewer, payload)
    data, bits, label = p.viewer.recorder.messages[-1]
    assert label == "group-member-query-result"
    return decode_group_member_query_result(data, bits)


def test_native_query_target_is_second_uint32():
    payload, bits = request_bytes(60000005, context=123)
    value = decode_group_member_query(payload, bits)
    assert value.field_20 == 123 and value.group_id == 60000005


def test_nonmember_can_inspect_full_selected_group_without_joining(popup):
    p = popup
    before = p.social.path.read_bytes()
    status, context, group_id, members = query(p, context=12345)
    assert (status, context, group_id) == (0, 12345, p.group_id)
    assert len(members) == 5
    rows = {m.character_id: m for m in members}
    assert (rows[10].name, rows[10].class_id, rows[10].level, rows[10].role) == ("Leader live", 0, 87, 0)
    assert rows[10].online and rows[10].available
    assert rows[10].account_id == p.leader.gameplay_remote_actor_id
    assert (rows[11].name, rows[11].level, rows[11].map_or_guild) == ("Saved name", 72, "b1200_hub")
    assert not rows[11].online and not rows[11].available and rows[11].account_id == 0
    assert rows[12].online and rows[12].available and rows[12].level == 61
    assert rows[12].account_id == 0 and rows[12].map_or_guild == "b1200_hub"
    assert not rows[13].available and rows[13].level == 20
    assert not rows[14].available and rows[14].name == "Offline fallback"
    assert all(m.role == 1 for m in members if m.character_id != 10)
    assert p.social.group_for(99) is None
    assert p.social.path.read_bytes() == before
    assert GROUP_STATUS_COMMAND not in p.viewer.recorder.ids()


def test_selection_wins_when_viewer_belongs_to_a_different_group(popup):
    p = popup
    own = p.social.create_group("Other party", "", "a0200_kingscity", make_social_member(99, "Viewer", 1, 100))
    status, context, group_id, members = query(p, context=own.group_id)
    assert status == 0 and group_id == p.group_id and context == own.group_id
    assert {m.character_id for m in members} == {10, 11, 12, 13, 14}
    assert p.social.group_for(99).group_id == own.group_id


def test_members_can_inspect_their_unlisted_group_on_another_map(popup):
    p = popup
    own = p.social.create_group("Own party", "", "b1200_hub", make_social_member(99, "Viewer", 1, 100))
    p.social.groups[own.group_id].listed = False
    result = query(p, group_id=own.group_id)
    assert result[0] == 0 and result[2] == own.group_id
    assert [m.character_id for m in result[3]] == [99]


@pytest.mark.parametrize("case", ["zero", "unknown", "deleted", "empty", "unlisted", "different-map"])
def test_unavailable_group_returns_native_failure_to_close_popup(popup, case):
    p = popup
    target = p.group_id
    if case == "zero": target = 0
    elif case == "unknown": target = 0xFFFFFFFF
    elif case == "deleted": del p.social.groups[target]
    elif case == "empty": p.social.groups[target].members.clear()
    elif case == "unlisted": p.social.groups[target].listed = False
    elif case == "different-map": p.social.groups[target].map_id = "b1200_hub"
    assert query(p, group_id=target, context=57) == (GROUP_MEMBER_QUERY_FAILED, 57, target, ())


def test_logout_and_relogin_refresh_the_popup_activity_marker(popup):
    p = popup
    assert next(m for m in query(p)[3] if m.character_id == 10).available
    p.leader.gameplay_presence_departed = True
    row = next(m for m in query(p)[3] if m.character_id == 10)
    assert not row.available and row.account_id == 0
    p.leader.gameplay_presence_departed = False
    p.leader.gameplay_character.level = 88
    row = next(m for m in query(p)[3] if m.character_id == 10)
    assert row.available and row.level == 88


def test_same_map_different_instance_is_online_without_a_local_actor(popup):
    p = popup
    p.leader.gameplay_map_access_group_id = "another-instance"
    row = next(m for m in query(p)[3] if m.character_id == 10)
    assert row.available and row.account_id == 0


def test_legacy_gameplay_endpoint_serves_the_same_popup(popup):
    p = popup
    legacy = LoginGameServer.__new__(LoginGameServer)
    legacy.sessions = p.server.sessions
    legacy.social_store = p.social
    legacy.account_store = p.accounts
    result = query(p, server=legacy)
    assert result[0] == 0 and result[2] == p.group_id and len(result[3]) == 5


def test_group_lookup_returns_detached_data_and_profile_is_immutable(popup):
    p = popup
    group = p.social.group_by_id(p.group_id)
    group.members.clear()
    assert len(p.social.group_by_id(p.group_id).members) == 5
    profile = p.accounts.get_profile(11)
    with pytest.raises(FrozenInstanceError):
        profile.level = 1
    assert p.accounts.get_profile(11).level == 72
    assert p.accounts.get_profile(123456) is None


def test_failure_codec_allows_empty_but_success_requires_members():
    packet, bits = encode_group_member_query_result(123, (), status=GROUP_MEMBER_QUERY_FAILED)
    expected = b"\x85" + GROUP_MEMBER_QUERY_RESULT_COMMAND.to_bytes(2, "little") + bytes.fromhex("01000000007b00000000000000ff")
    assert packet == expected and bits == len(expected) * 8
    assert decode_group_member_query_result(packet, bits) == (1, 0, 123, ())
    with pytest.raises(ValueError):
        encode_group_member_query_result(123, ())


def test_recorded_buddy_refresh_decodes_and_receives_empty_status(popup):
    request = decode_buddy_list_request(RECORDED_BUDDY_REFRESH)
    assert (request.action, request.field_24, request.name, request.field_68) == (0, 0, "", False)
    p = popup
    p.viewer.current_message_bit_length = 81
    p.server._on_char_op(p.viewer, RECORDED_BUDDY_REFRESH)
    data, bits, label = p.viewer.recorder.messages[-1]
    assert label == "buddy-list-status"
    reader = BitReader(data, bits)
    assert reader.read_uint8() == 0x85
    assert int.from_bytes(reader.read_bytes(2), "little") == BUDDY_STATUS_COMMAND
    assert reader.read_uint8() == 0
    assert reader.read_uint32_le() == 0
    assert reader.read_uint32_le() == 0
    assert reader.read_uint8() == 0xFF and reader.remaining == 0
    assert p.viewer.gameplay_client_command_counts[BUDDY_LIST_COMMAND] == 1


@pytest.mark.parametrize("packet", [
    RECORDED_BUDDY_REFRESH[:-1], RECORDED_BUDDY_REFRESH + b"\0",
    RECORDED_BUDDY_REFRESH[:-1] + b"\x01",
])
def test_malformed_buddy_refresh_is_rejected(packet):
    with pytest.raises(ValueError):
        decode_buddy_list_request(packet)


def buddy_request(action, name=""):
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(BUDDY_LIST_COMMAND.to_bytes(2, "little"))
    writer.write_uint8(action)
    writer.write_uint32_le(0)
    writer.write_string(name)
    writer.write_bool(False)
    return writer.finish()


def send_buddy_request(popup, action, name=""):
    packet, bits = buddy_request(action, name)
    popup.viewer.current_message_bit_length = bits
    popup.server._on_char_op(popup.viewer, packet)


def test_buddy_add_and_remove_by_name_answer_with_the_full_list(popup):
    p = popup
    send_buddy_request(p, 1, "remote")
    assert p.social.buddies_for(99) == (12,)
    assert p.viewer.recorder.messages[-1][2] == "buddy-list-status"
    send_buddy_request(p, 2, "Remote")
    assert p.social.buddies_for(99) == ()
    assert p.viewer.recorder.messages[-1][2] == "buddy-list-status"


@pytest.mark.parametrize("name", ["", "Nobody", "Viewer"])
def test_buddy_add_of_an_unknown_name_or_self_changes_nothing(popup, name):
    p = popup
    send_buddy_request(p, 1, name)
    assert p.social.buddies_for(99) == ()
    assert p.viewer.recorder.messages[-1][2] == "buddy-list-status"


def test_unknown_buddy_operation_is_not_acknowledged_as_a_refresh(popup):
    p = popup
    with pytest.raises(ValueError, match="unsupported buddy-list operation"):
        send_buddy_request(p, 3)
    assert not p.viewer.recorder.messages
