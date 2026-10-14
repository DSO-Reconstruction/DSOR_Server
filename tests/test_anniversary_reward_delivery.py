"""The arena's last wave must not block the ordered stream before its chests."""
from unittest.mock import Mock, patch

import pytest

from dso_server.protocol.gameplay_protocol import (
    Move, MONSTER_UPDATE_COMMAND, PROP_INFO_COMMAND, decode_monster_vicinity_batch,
    decode_prop_info_batch, encode_screen_message,
)
from dso_server.runtime.context import FRAG_SIZE
from dso_server.runtime.transport import RakNetServer, Session
from dso_server.services.character_server import CharacterServer
from dso_server.tests.test_event_waves_and_vendors import enter, world


EXPECTED_CHESTS = sorted([
    "e_anniversary_monsterwave_random_reward",
    "e_anniversary_monsterwave_chest_guardian",
    "e_anniversary_monsterwave_chest_champion",
    "e_anniversary_monsterwave_chest_champion",
    "e_anniversary_monsterwave_chest_leader",
])


@pytest.mark.parametrize("route", ["event", "movement", "peer"])
def test_final_wave_and_chests_survive_packet_loss_without_stalling(world, route):
    case = world
    source, runtime = enter(case, "e_anniversary_arena_01")
    runtime.scaling_difficulty = 1  # Painful, as in the live report.
    wave = runtime.catalog.event("e_anniversary_arena_random_wave_05")
    with patch.object(CharacterServer, "_start_monster_chase", return_value=False):
        case.server._start_map_local_event(runtime, wave)
    combat = source.gameplay_combat_state
    group = combat.runtime_groups[-1]
    # The sentinel cluster is outside the old test's origin. Approaching it
    # reveals enough actors to exceed the native client's datagram limit.
    position = (50.0584, 0, -16.70755)
    move = Move.from_values(source.gameplay_coordinate_frame.world_to_wire(position),
                            0, 0, 0, 500, 0)
    viewer = source
    if route == "peer":
        viewer = case._session(50002, 2)
        viewer.gameplay_map = source.gameplay_map
        viewer.gameplay_map_access_group_id = source.gameplay_map_access_group_id
        viewer.gameplay_map_event_runtime = runtime
        viewer.gameplay_combat_state = combat
        viewer.gameplay_monster_encounter = source.gameplay_monster_encounter
        viewer.gameplay_coordinate_frame = source.gameplay_coordinate_frame
        case.server.sessions[viewer.addr] = viewer
    viewer.gameplay_last_move = move
    viewer.gameplay_announced_monster_actor_ids.clear()
    # Use real RakNet framing and splitting; the old recorder masked this bug.
    del viewer.send_reliable_ordered
    del viewer.send_reliable_ordered_split
    viewer.sock.sent.clear()
    if route == "event":
        case.server._announce_runtime_group(viewer, combat, group)
    elif route == "movement":
        case.server._handle_gameplay_move(viewer, b"\x8b\x5f\x00" + move.encode_body())
    else:
        with patch.object(CharacterServer, "_active_gameplay_sessions", return_value=(viewer,)):
            case.server._handle_presence_event(source, ("monster_groups_announced", group.actor_ids))

    # Complete the real final-wave signal and publish its reward props.
    alive = tuple(combat.monsters[i] for i in combat.alive_actor_ids)
    for monster in alive:
        combat.monster_health[monster.actor_id] = 0
    with patch.object(CharacterServer, "_achievement_on_monsters_killed"):
        case.server._on_monsters_killed(source, alive)
    assert "e_anniversary_arena_random_reward" in runtime.running
    marker = encode_screen_message("arena-responsive")
    viewer.send_reliable_ordered(marker, label="post-arena-check")
    packets = [packet for packet, _addr in viewer.sock.sent]
    assert all(len(packet) <= FRAG_SIZE + 24 for packet in packets)

    receiver = Session(("127.0.0.1", 2192), Mock())
    client = RakNetServer.__new__(RakNetServer)
    delivered = []
    client._on_message = lambda s, message, payload: delivered.append((payload, s.current_message_bit_length))
    # Movement emits an unrelated unreliable echo first. Withhold the first
    # tracked datagram, ACK later packets, then retransmit the missing fragment.
    missing = min(viewer.pending_reliable)
    for packet in reversed(packets):
        sequence = int.from_bytes(packet[1:4], "little")
        if sequence == missing:
            continue
        client._handle_frameset(receiver, packet)
        viewer.acknowledge([sequence])
    assert marker not in delivered
    assert not any(p[:3] == b"\x85" + PROP_INFO_COMMAND.to_bytes(2, "little") for p, _ in delivered)
    retries, dropped = viewer.retransmit([missing], interval=0)
    assert len(retries) == 1 and not dropped
    sequence, retry, _label, _attempt = retries[0]
    client._handle_frameset(receiver, retry)
    viewer.acknowledge([sequence])
    assert delivered[-1] == marker
    assert not viewer.pending_reliable
    assert not receiver.split_packets and not receiver.inbound_order_pending
    visibility = next(value for value in delivered
                      if value[0][:3] == b"\x85" + MONSTER_UPDATE_COMMAND.to_bytes(2, "little"))
    assert len(decode_monster_vicinity_batch(*visibility).updates) > 32
    chests = [entry for payload, bits in delivered
              if payload[:3] == b"\x85" + PROP_INFO_COMMAND.to_bytes(2, "little")
              for entry in decode_prop_info_batch(payload, bits)[0].entries]
    assert sorted(entry.template_id for entry in chests) == EXPECTED_CHESTS
    assert len({entry.prop_id & 0xFFFF for entry in chests}) == 5
    assert all(entry.field_24 == 1 for entry in chests)
