"""Admin entry points through reliable delivery and both reconnect hops."""
from types import SimpleNamespace
import threading
from unittest.mock import Mock

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.gameplay.local_map_state import load_local_spawn_position
from dso_server.protocol.character_selection import BitReader
from dso_server.protocol.map_packets import SWITCH_MAP_COMMAND
from dso_server.runtime.gameplay_routes import GameplayRouteTable
from dso_server.runtime.transport import Session, SessionRegistry
from dso_server.services.admin_live import LiveAdministration
from dso_server.services.character_server import CharacterServer
from dso_server.services.chat import ChatServer
from dso_server.services.login_game import LoginGameServer


@pytest.fixture
def transfer(monkeypatch):
    server = CharacterServer.__new__(CharacterServer)
    server.static_database_path = DEFAULT_STATIC_DB_PATH
    server.static_database = server.account_store = None
    server.advertised_host, server.port = "127.0.0.1", 2192
    server.service_name = "transfer-test"
    server.gameplay_routes = GameplayRouteTable(30)
    server.sessions = SessionRegistry(threading.Lock())
    server.admin_commands = SimpleNamespace(call=lambda operation: operation())
    router = LoginGameServer.__new__(LoginGameServer)
    router.port, router.char_server_addr = 2190, "127.0.0.1:2192"
    router.gameplay_routes = GameplayRouteTable(30)
    server.gameplay_router = router
    source = Session(("127.0.0.1", 52000), Mock())
    source.connected = source.gameplay_mode = True
    source.gameplay_initialization_sent = source.gameplay_presence_ready = True
    source.gameplay_map = "a0200_kingscity"
    source.gameplay_spawn_position = (999, 99, 999)
    source.gameplay_character = SimpleNamespace(character_id=42, name="Traveller", level=100)
    source.gameplay_template_row_index = 0
    server.sessions[source.addr] = source
    monkeypatch.setattr(CharacterServer, "_quest_leave_map", Mock())
    monkeypatch.setattr(CharacterServer, "_persist_gameplay_session", Mock())
    return server, router, source


def read_switch(payload, bits, address, *, batch=False):
    reader = BitReader(payload, bits)
    assert reader.read_uint8() == (0x85 if batch else 0x84)
    assert int.from_bytes(reader.read_bytes(2), "little") == SWITCH_MAP_COMMAND
    assert reader.read_string() == address
    assert not reader.read_bool()  # Keep the in-game loading/UI state.
    if batch:
        assert reader.read_uint8() == 0xFF
    assert reader.remaining == 0


@pytest.mark.parametrize("entry", ["chat", "cms"])
@pytest.mark.parametrize("destination", ["a0201_basicarena", "a0203_kingscrypt02_01"])
def test_admin_transfer_delivery_and_reconnect(transfer, entry, destination, capsys):
    server, router, source = transfer
    if entry == "chat":
        chat = ChatServer.__new__(ChatServer)
        chat.gameplay_servers = (server,)
        session = Session(("127.0.0.1", 52001), Mock())
        session.chat_is_admin, session.chat_user_id, session.chat_user_name = True, 42, "Traveller"
        chat._handle_admin_command(session, "!map " + destination)
    else:
        live = LiveAdministration.__new__(LiveAdministration)
        live.service = SimpleNamespace(
            application=SimpleNamespace(characters=server, login=router),
            static_database_path=DEFAULT_STATIC_DB_PATH, repository=Mock(), store=Mock())
        result = live.action(42, "teleport", {"map_id": destination}, "admin", "Move player")
        assert result["ok"]
    packet = source.sock.sendto.call_args.args[0]
    assert packet[4] == 0x60  # Reliable ordered, channel zero.
    bits = int.from_bytes(packet[5:7], "big")
    read_switch(packet[14:], bits, "127.0.0.1:2190", batch=True)
    assert source.connected and not source.retired and source.gameplay_exit_transition_started
    assert server.gameplay_routes.pending(source.addr[0]) == 1
    assert router.gameplay_routes.pending(source.addr[0]) == 1

    # Losing the first datagram preserves order/reliable indexes on retry.
    sequence = int.from_bytes(packet[1:4], "little")
    retries, dropped = source.retransmit([sequence], interval=0)
    assert not dropped and len(retries) == 1
    retry_sequence, retry, label, _ = retries[0]
    assert retry_sequence != sequence and retry[4:] == packet[4:]
    ack = b"\xc0\x00\x01\x01" + retry_sequence.to_bytes(3, "little")
    server._handle_ack(source, ack)
    assert not source.pending_reliable
    assert "acknowledged admin-" in capsys.readouterr().out

    # The account claims its own route at each hop; no fallback to selection.
    login = Session((source.addr[0], 52002), Mock())
    login.account_id, login.account_character_ids = 7, (42,)
    login.send_wire_first_delivery_second = Mock()
    router._on_hello(login, b"")
    first, redirect = login.send_wire_first_delivery_second.call_args.args
    assert first == b"\x88"
    read_switch(*redirect, "127.0.0.1:2192")
    assert router.gameplay_routes.pending(source.addr[0]) == 0

    arrival = Session((source.addr[0], 52003), Mock())
    arrival.account_id, arrival.account_character_ids = 7, (42,)
    arrival.send_multi_reliable_ordered = Mock()
    server._on_hello(arrival, b"")
    physical = "a0203_kingscrypt02" if destination.endswith("_01") else destination
    assert arrival.gameplay_map == physical
    assert arrival.gameplay_character.character_id == 42
    assert arrival.gameplay_character.map_access_id == destination
    assert arrival.gameplay_route_spawn_position == load_local_spawn_position(DEFAULT_STATIC_DB_PATH, physical)
    assert arrival.gameplay_route_spawn_position != source.gameplay_spawn_position
    assert server.gameplay_routes.pending(source.addr[0]) == 0
    assert arrival.send_multi_reliable_ordered.call_args.args[0][0][0] == 0x86


def test_invalid_switch_address_does_not_retire_source(transfer):
    server, router, source = transfer
    server.advertised_host = "invalid-\u2603"
    with pytest.raises(ValueError, match="ASCII"):
        server._begin_map_transfer(source, "a0201_basicarena", reason="admin-chat")
    assert source.connected and not source.gameplay_exit_transition_started
    assert not source.gameplay_presence_departed
    source.sock.sendto.assert_not_called()
    assert not router.gameplay_routes.pending(source.addr[0])
    assert not server.gameplay_routes.pending(source.addr[0])
