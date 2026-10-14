"""Map replies must not block the reliable stream ahead of SwitchMap."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dso_server.gameplay.local_map_state import active_local_map_event_flags
from dso_server.gameplay.map_event_state import (
    MapEventRuntime, load_map_event_content, load_map_signal_catalog,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import decode_prop_info_batch, encode_prop_info_batch
from dso_server.protocol.map_packets import GameplaySwitchMapPacket, SwitchMapPacket
from dso_server.runtime.context import FRAG_SIZE
from dso_server.runtime.transport import RakNetServer, Session
from dso_server.services.character_server import CharacterServer


@pytest.fixture
def map_reply():
    server = CharacterServer.__new__(CharacterServer)
    server.static_database_path = DEFAULT_STATIC_DB_PATH
    sender = Session(("127.0.0.1", 52000), Mock())
    sender.gameplay_mode = sender.connected = True
    sender.gameplay_map = "a0200_kingscity"
    sender.gameplay_character = SimpleNamespace(level=100, class_id=4, character_id=42)
    receiver = Session(("127.0.0.1", 2192), Mock())
    client = RakNetServer.__new__(RakNetServer)
    delivered = []
    client._on_message = lambda s, message, payload: delivered.append((payload, s.current_message_bit_length))
    return server, sender, receiver, client, delivered


@pytest.mark.parametrize("events", [False, True])
@pytest.mark.parametrize("batch", [False, True])
def test_prop_reply_then_switch_survives_reordering_and_loss(map_reply, events, batch):
    server, sender, receiver, client, delivered = map_reply
    # Anniversary adds the nine fairground props that made the live response
    # exceed the client's datagram budget (15 entries, 1290 payload bytes).
    flags = active_local_map_event_flags(sender.gameplay_map, frozenset({
        "anniversary_event", "e_anniversary_challenge_silent_duria",
        "e_chr_2015_challenge_04", "event_wanted_thief_silent",
        "scaling_dungeon_kill_event_01",
    }) if events else frozenset())
    sender.gameplay_map_event_runtime = MapEventRuntime(
        load_map_event_content(DEFAULT_STATIC_DB_PATH, sender.gameplay_map),
        load_map_signal_catalog(DEFAULT_STATIC_DB_PATH),
        instance_key=(sender.gameplay_map,), initial_flags=flags,
    )
    expected = server._map_event_prop_info(sender)
    server._handle_prop_request(sender, bytes.fromhex("8b3800"))
    switch = (GameplaySwitchMapPacket("127.0.0.1:2190") if batch else
              SwitchMapPacket("127.0.0.1:2190", False)).encode()
    sender.send_reliable_ordered(switch, label="test-switch-map")
    packets = [call.args[0] for call in sender.sock.sendto.call_args_list]
    assert all(len(packet) <= FRAG_SIZE + 24 for packet in packets)
    assert len(packets) == (3 if events else 2)

    # Switch arrives and is ACKed first, just like the live stall. Receiving
    # that datagram does not deliver it before the missing prop reply.
    for packet in reversed(packets[1:]):
        client._handle_frameset(receiver, packet)
        sender.acknowledge([int.from_bytes(packet[1:4], "little")])
    assert not delivered
    missing = int.from_bytes(packets[0][1:4], "little")
    retries, dropped = sender.retransmit([missing], interval=0)
    assert len(retries) == 1 and not dropped
    retry_sequence, retry, _label, _attempt = retries[0]
    assert len(retry) <= FRAG_SIZE + 24
    client._handle_frameset(receiver, retry)
    sender.acknowledge([retry_sequence])
    assert not sender.pending_reliable
    assert len(delivered) == 2
    props, actor_id = decode_prop_info_batch(*delivered[0])
    assert actor_id == 1 and len(props.entries) == len(expected.entries)
    assert delivered[0] == encode_prop_info_batch(expected, actor_id=1)
    assert delivered[1] == switch
    assert not receiver.split_packets and not receiver.inbound_order_pending
