"""Social activity markers use the flag actually read by the native UI."""

import threading

import pytest

from dso_server.persistence.social_state import SocialStateStore, make_social_member
from dso_server.protocol.social_protocol import (
    GUILD_LIST_REQUEST_COMMAND, GUILD_STATUS_COMMAND,
    decode_guild_status, decode_group_status,
)
from dso_server.runtime.transport import SessionRegistry
from dso_server.services.character_server import CharacterServer
from dso_server.tests.test_pvp_matchmaking import _session


@pytest.fixture
def social(tmp_path):
    store = SocialStateStore(tmp_path / "social.json")
    guild = store.create_guild("Test guild", "", make_social_member(1, "Viewer", 1, 100))
    store.add_guild_member(guild.guild_id, 1, make_social_member(2, "Member", 1, 100))
    group = store.create_group("Test group", "", "a0200_kingscity", make_social_member(1, "Viewer", 1, 100))
    store.add_group_member(group.group_id, 1, make_social_member(2, "Member", 1, 100))
    server = CharacterServer.__new__(CharacterServer)
    server.service_name = "social-test"
    server.social_store = SocialStateStore(store.path)
    server.sessions = SessionRegistry(threading.Lock())

    def connect(character_id, port):
        session = _session(port, character_id, "Viewer" if character_id == 1 else "Member")
        session.gameplay_initialization_sent = True
        session.gameplay_presence_ready = True
        session.gameplay_presence_callback = server._handle_presence_event
        server.sessions[session.addr] = session
        return session

    viewer = connect(1, 51001)
    return server, viewer, connect


def guild_members(viewer):
    # The native CreateGuildListElement (0x14052b928) selects its activity
    # icon using the SECOND bool (+0x51), not the first field named online.
    packet = next((data, bits) for data, bits, _ in reversed(viewer.recorder.messages)
                  if int.from_bytes(data[1:3], "little") == GUILD_STATUS_COMMAND)
    status = decode_guild_status(*packet)
    return {member.player_id: member for member in status.members}


def query_guild(server, viewer):
    payload = b"\x8b" + GUILD_LIST_REQUEST_COMMAND.to_bytes(2, "little") + bytes(4)
    viewer.current_message_bit_length = len(payload) * 8
    server._on_char_op(viewer, payload)
    return guild_members(viewer)


def test_saved_members_without_a_session_are_offline(social):
    server, viewer, _ = social
    members = query_guild(server, viewer)
    assert members[1].field_51 is True
    assert members[2].field_51 is False
    assert members[2].account_id == 0
    assert len(server.social_store.guild_for(1).members) == 2


@pytest.mark.parametrize("map_id", ["a0200_kingscity", "b1200_hub"])
def test_logged_in_member_is_online_even_on_another_map(social, map_id):
    server, viewer, connect = social
    member = connect(2, 51002)
    member.gameplay_map = map_id
    members = query_guild(server, viewer)
    assert members[2].field_51 is True
    assert bool(members[2].account_id) == (map_id == viewer.gameplay_map)


@pytest.mark.parametrize("attribute,value", [
    ("connected", False), ("gameplay_mode", False),
    ("gameplay_initialization_sent", False), ("gameplay_presence_ready", False),
    ("gameplay_presence_departed", True),
])
def test_inactive_character_sessions_do_not_count_as_online(social, attribute, value):
    server, viewer, connect = social
    member = connect(2, 51002)
    setattr(member, attribute, value)
    assert query_guild(server, viewer)[2].field_51 is False


def test_disconnect_automatically_updates_guild_and_group_packets(social):
    server, viewer, connect = social
    member = connect(2, 51002)
    assert query_guild(server, viewer)[2].field_51 is True
    viewer.recorder.messages.clear()
    assert server._retire_session(member, reason="client disconnect", remove=True)
    assert guild_members(viewer)[2].field_51 is False
    packet = next((data, bits) for data, bits, label in viewer.recorder.messages
                  if label.startswith("group-status"))
    row = next(row for row in decode_group_status(*packet).members if row.character_id == 2)
    assert row.available is False  # PlayerSocialInfo's second bool also drives its icon.
    assert member.addr not in server.sessions
    assert len(server.social_store.guild_for(1).members) == 2


def test_departing_character_is_offline_while_logout_socket_still_exists(social):
    server, viewer, connect = social
    member = connect(2, 51002)
    server._handle_presence_event(member, "disconnect")
    assert member.connected  # Character switch/map handoff can retain the socket.
    assert guild_members(viewer)[2].field_51 is False
    assert query_guild(server, viewer)[2].field_51 is False


def test_new_login_refreshes_the_marker_without_changing_membership(social):
    server, viewer, connect = social
    assert query_guild(server, viewer)[2].field_51 is False
    connect(2, 51002)
    server._broadcast_presence_social_status(2)
    assert guild_members(viewer)[2].field_51 is True


def test_old_socket_retirement_does_not_mark_replacement_offline(social):
    server, viewer, connect = social
    old = connect(2, 51002)
    new = connect(2, 51003)
    server._retire_session(old, reason="reconnected", remove=True)
    assert query_guild(server, viewer)[2].field_51 is True
    assert server.sessions[new.addr] is new
