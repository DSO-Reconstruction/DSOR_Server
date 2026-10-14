"""Client settings requests and viewer-specific overhead icon actor ids."""

import pytest

from dso_server.persistence.social_state import SocialStateStore
from dso_server.protocol.character_selection import BitReader
from dso_server.protocol.social_protocol import (
    UPDATE_SHOW_PLAYER_OVERHEAD_ICONS_COMMAND as COMMAND,
    UpdateShowPlayerOverheadIcons,
    decode_update_show_player_overhead_icons,
)
from dso_server.services.character_server import CharacterServer
from dso_server.services.login_game import LoginGameServer
from dso_server.tests.test_pvp_matchmaking import _session


SHOW = bytes.fromhex("8b23008080000000")
HIDE = bytes.fromhex("8b23000080000000")


@pytest.mark.parametrize("bits", [None, 57, 64])
@pytest.mark.parametrize("payload,visible", [(SHOW, True), (HIDE, False)])
def test_captured_settings_request(payload, visible, bits):
    assert decode_update_show_player_overhead_icons(payload, bits) == (
        UpdateShowPlayerOverheadIcons(visible, 1)
    )


@pytest.fixture
def world(tmp_path):
    server = CharacterServer.__new__(CharacterServer)
    server.sessions = {}
    server.social_store = SocialStateStore(tmp_path / "social.json")
    server.static_database = None

    def connect(character_id):
        session = _session(52000 + character_id, character_id, f"Player{character_id}")
        session.gameplay_initialization_sent = True
        session.gameplay_presence_ready = True
        session.gameplay_presence_callback = server._handle_presence_event
        for name in ("gender", "hair_style", "beard_style", "body_style", "body_stature"):
            setattr(session.gameplay_character, name, 0)
        session.gameplay_character.admin_privileges = False
        session.gameplay_character.admin_name_color = False
        server.sessions[session.addr] = session
        return session

    return server, connect(1), connect(2), connect


def dispatch(server, session, payload, *, bits=57, route=CharacterServer):
    session.current_message_bit_length = bits
    route._on_char_op(server, session, payload)


def updates(session):
    values = []
    for data, bits, _ in session.recorder.messages:
        if int.from_bytes(data[1:3], "little") != COMMAND:
            continue
        reader = BitReader(data, bits)
        assert reader.read_uint8() == 0x85
        assert int.from_bytes(reader.read_bytes(2), "little") == COMMAND
        values.append((reader.read_bool(), reader.read_uint32_le()))
        # Server replies need a second actor id for ServerCommand context.
        assert reader.read_uint32_le() == values[-1][1]
        assert reader.read_uint8() == 0xFF
        assert reader.remaining == 0
    return values


@pytest.mark.parametrize("route", [CharacterServer, LoginGameServer])
def test_toggle_echoes_local_actor_and_remaps_for_same_instance_peers(world, route):
    server, source, peer, connect = world
    elsewhere = connect(3)
    elsewhere.gameplay_map = "another_map"
    other_instance = connect(4)
    other_instance.gameplay_instance_id = "another_instance"
    not_ready = connect(5)
    not_ready.gameplay_presence_ready = False
    for payload in (SHOW, HIDE):
        dispatch(server, source, payload, route=route)
    assert updates(source) == [(True, 1), (False, 1)]
    assert updates(peer) == [
        (True, source.gameplay_remote_actor_id),
        (False, source.gameplay_remote_actor_id),
    ]
    assert not updates(elsewhere)
    assert not updates(other_instance)
    assert not updates(not_ready)
    assert source.gameplay_client_command_counts[COMMAND] == 2
    assert source.gameplay_client_command_state[COMMAND].visible is False


@pytest.mark.parametrize("payload", [SHOW, HIDE])
def test_late_join_and_actor_refresh_preserve_visibility(world, payload):
    server, source, peer, _ = world
    dispatch(server, source, payload)
    visible = payload == SHOW
    peer.recorder.messages.clear()
    server._announce_remote_presence(peer)
    assert updates(peer) == [(visible, source.gameplay_remote_actor_id)]
    peer.recorder.messages.clear()
    server._handle_presence_event(peer, ("actor_request", source.gameplay_remote_actor_id))
    assert updates(peer) == [(visible, source.gameplay_remote_actor_id)]


def test_pvp_refresh_does_not_override_explicit_hide(world):
    server, source, peer, _ = world
    dispatch(server, source, HIDE)
    peer.recorder.messages.clear()
    source.gameplay_pvp_enabled = True
    server._handle_presence_event(source, ("pvp_flag", True, 123))
    assert updates(peer) == [(False, source.gameplay_remote_actor_id)]


def test_pvp_default_remains_visible_without_a_settings_request(world):
    server, source, peer, _ = world
    source.gameplay_pvp_enabled = True
    server._handle_presence_event(source, ("pvp_flag", True, 123))
    assert updates(peer) == [(True, source.gameplay_remote_actor_id)]


@pytest.mark.parametrize("payload,bits", [
    (SHOW[:-1], 56),
    (SHOW + b"\x00", 72),
    (SHOW[:-1] + b"\x01", 64),
    (bytes.fromhex("8b23008100000000"), 57),  # Another actor (2).
    (bytes.fromhex("8b23008000000000"), 57),  # No actor.
])
def test_invalid_requests_do_not_change_state_or_send_updates(world, payload, bits):
    server, source, peer, _ = world
    dispatch(server, source, payload, bits=bits)
    assert COMMAND not in source.gameplay_client_command_state
    assert COMMAND not in source.gameplay_client_command_counts
    assert not updates(source)
    assert not updates(peer)
