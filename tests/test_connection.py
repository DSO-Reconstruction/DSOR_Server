"""Per-connection state: sequencing, acknowledgement, splitting, acceptance."""

import pytest

from raknet.address import SystemAddress
from raknet.connection import MAX_FRAME_HEADER, Connection, State
from raknet.constants import MessageID, Reliability
from raknet.datagram import parse_datagram_header
from raknet.frame import parse_frames
from raknet.offline import UDP_IPV4_OVERHEAD

CLIENT = SystemAddress("78.112.59.92", 52758)
SERVER = SystemAddress("10.208.115.155", 2190)
GUID = bytes.fromhex("00063fb2731c5c79")
MTU = 1292


def make_connection(mtu: int = MTU) -> Connection:
    return Connection(remote=CLIENT, local=SERVER, server_guid=GUID, mtu=mtu)


def sequences_of(datagrams: list[bytes]) -> list[int]:
    return [parse_datagram_header(d)[0].sequence for d in datagrams]


def test_datagram_sequence_starts_at_zero_and_increments():
    # A constant sequence number is the bug that makes a peer accept the first
    # datagram and discard every later one as a duplicate.
    connection = make_connection()
    sent = [connection.send_message(b"\x84a")[0] for _ in range(4)]
    assert sequences_of(sent) == [0, 1, 2, 3]


def test_reliable_and_ordering_indices_advance_independently():
    connection = make_connection()
    first = parse_frames(connection.send_message(b"\x84a")[0], 4)[0]
    second = parse_frames(connection.send_message(b"\x84b")[0], 4)[0]
    assert (first.reliable_index, second.reliable_index) == (0, 1)
    assert (first.ordering_index, second.ordering_index) == (0, 1)


def test_ordering_indices_are_per_channel():
    connection = make_connection()
    channel_0 = parse_frames(connection.send_message(b"\x84a", channel=0)[0], 4)[0]
    channel_3 = parse_frames(connection.send_message(b"\x84b", channel=3)[0], 4)[0]
    assert channel_0.ordering_index == channel_3.ordering_index == 0
    assert channel_3.ordering_channel == 3


def test_receiving_queues_an_ack_and_flushing_clears_it():
    connection = make_connection()
    for sequence in (0, 1, 2):
        connection.receive(_datagram(sequence, b"\x84ping"))
    ack = connection.flush_acks()
    assert ack is not None and ack[0] == 0xC0
    # Coalesced into a single 0..2 range rather than three separate records:
    # flags(1) + count(2) + range flag(1) + min(3) + max(3).
    assert len(ack) == 10
    assert ack[3] == 0, "a span of three is not a single-value record"
    assert connection.flush_acks() is None, "nothing left to acknowledge"


def test_flush_acks_returns_none_when_idle():
    assert make_connection().flush_acks() is None


def test_large_payload_is_split_and_reassembled():
    connection = make_connection()
    budget = MTU - UDP_IPV4_OVERHEAD - 4 - MAX_FRAME_HEADER
    payload = bytes([0x84]) + bytes(range(256)) * 12  # comfortably over one MTU
    assert len(payload) > budget

    datagrams = connection.send_message(payload)
    assert len(datagrams) > 1

    frames = [parse_frames(d, 4)[0] for d in datagrams]
    assert all(f.is_split for f in frames)
    assert {f.split_id for f in frames} == {0}
    assert [f.split_index for f in frames] == list(range(len(frames)))
    # Every piece of one message shares an ordering index: it is one message.
    assert len({f.ordering_index for f in frames}) == 1

    # Feed the pieces back into a fresh connection and expect the whole message.
    receiver = make_connection()
    delivered = []
    for sequence, datagram in enumerate(datagrams):
        body = datagram[4:]
        delivered += receiver.receive(bytes([0x84]) + sequence.to_bytes(3, "little") + body)
    assert len(delivered) == 1
    assert delivered[0].payload == payload


def test_split_pieces_reassemble_out_of_order():
    connection = make_connection()
    payload = bytes([0x84]) + b"x" * 4000
    datagrams = connection.send_message(payload)
    receiver = make_connection()
    delivered = []
    for sequence, datagram in enumerate(reversed(datagrams)):
        delivered += receiver.receive(
            bytes([0x84]) + sequence.to_bytes(3, "little") + datagram[4:]
        )
    assert len(delivered) == 1 and delivered[0].payload == payload


def test_connection_request_is_accepted_and_state_advances():
    connection = make_connection()
    request = (
        bytes([MessageID.CONNECTION_REQUEST])
        + bytes.fromhex("077000013213ce81")
        + (5136012).to_bytes(8, "big")
        + b"\x00"
    )
    accepted = connection.handle_connection_request(request)
    assert connection.state is State.CONNECTED
    assert connection.client_guid == bytes.fromhex("077000013213ce81")
    assert accepted[0] == MessageID.CONNECTION_REQUEST_ACCEPTED
    assert len(accepted) == 96
    # The client's own clock is echoed back before ours; that is how it measures
    # the round trip.
    assert accepted[80:88] == (5136012).to_bytes(8, "big")


def test_acceptance_matches_the_recorded_server(by_frame):
    _, offset = parse_datagram_header(by_frame[7])
    recorded = parse_frames(by_frame[7], offset)[0].payload
    connection = make_connection()
    built = connection.build_connection_request_accepted(
        client_timestamp=5136012, system_index=28, server_timestamp=788962489
    )
    assert built == recorded


def test_acceptance_reports_our_own_local_address():
    # A literal LAN address copied from someone else's capture is invisible when
    # testing on one machine and wrong everywhere else.
    connection = make_connection()
    accepted = connection.build_connection_request_accepted(0, server_timestamp=0)
    from raknet.address import decode_address

    _, offset = decode_address(accepted, 1)
    internal, _ = decode_address(accepted, offset + 2)
    assert (internal.ip, internal.port) == (SERVER.ip, SERVER.port)


def test_unconnected_datagram_is_refused_by_the_connection_layer():
    with pytest.raises(ValueError, match="not a connected datagram"):
        make_connection().receive(bytes([0x05, 0, 0, 0]))


def test_nak_is_recorded_rather_than_ignored():
    connection = make_connection()
    # single-value range: flag 0x01, then one 24-bit sequence number
    nak = bytes([0xA0]) + (1).to_bytes(2, "big") + b"\x01" + (4).to_bytes(3, "little")
    assert connection.receive(nak) == []
    assert connection.nacked == [4]


def test_pong_echoes_the_client_clock():
    connection = make_connection()
    pong = connection.build_connected_pong(1234)
    assert pong[0] == MessageID.CONNECTED_PONG
    assert pong[1:9] == (1234).to_bytes(8, "big")


def test_mtu_too_small_to_carry_anything_is_refused():
    with pytest.raises(ValueError, match="no room"):
        make_connection(mtu=UDP_IPV4_OVERHEAD + 4 + MAX_FRAME_HEADER).send_message(b"\x84")


def _datagram(sequence: int, payload: bytes) -> bytes:
    """A minimal reliable-ordered datagram, for feeding into receive()."""
    from raknet.frame import Frame, build_frame

    frame = Frame(payload=payload, reliable_index=0, ordering_index=0)
    return bytes([0x84]) + sequence.to_bytes(3, "little") + build_frame(frame)


def test_unacknowledged_datagrams_are_resent_without_being_asked():
    """A NAK is not the only thing that triggers a resend.

    The reference session sent 32,176 fragment datagrams to deliver 596 distinct
    fragments over 101.6 seconds. Nothing NAKs at that rate: the service was
    resending everything unacknowledged on a timer, and it had to be — one lost NAK
    strands a fragment, and a split message with a gap in it is never delivered.
    """
    connection = make_connection()
    datagrams = connection.send_message(b"\x84" + bytes(32))
    assert connection.unacknowledged == len(datagrams)

    connection.resend_after_ms = 0
    assert connection.due_retransmissions(limit=8) == datagrams


def test_the_resend_timer_waits_before_firing():
    """Otherwise every datagram goes out twice, doubling the load it was added to
    reduce."""
    connection = make_connection()
    connection.send_message(b"\x84" + bytes(32))
    connection.resend_after_ms = 60_000
    assert connection.due_retransmissions(limit=8) == []


def test_an_acknowledged_datagram_is_not_resent():
    connection = make_connection()
    (datagram,) = connection.send_message(b"\x84" + bytes(32))
    sequence = int.from_bytes(datagram[1:4], "little")
    ack = bytes([0xC0]) + (1).to_bytes(2, "big") + b"\x01" + sequence.to_bytes(3, "little")
    connection.receive(ack)
    connection.resend_after_ms = 0
    assert connection.due_retransmissions(limit=8) == []
    assert connection.unacknowledged == 0


def test_retransmissions_go_oldest_first():
    """A peer reassembling a split message is blocked on its earliest gap, so
    resending a later piece it already holds advances nothing."""
    connection = make_connection()
    datagrams = connection.send_message(b"\x84" + bytes(4000))
    assert len(datagrams) > 2
    connection.resend_after_ms = 0
    assert connection.due_retransmissions(limit=2) == datagrams[:2]


def test_a_datagram_sequence_is_assigned_when_it_is_sent_not_when_it_is_built():
    """Was: sequences assigned at build time, so anything queued left a gap.

    A datagram's sequence number is a transmit-order counter, and a peer reads a gap
    in it as loss. Numbering 592 fragments at once and then releasing six a second
    had the client NAK all 592 — repeatedly — which defeated the throttle and
    doubled the traffic it was meant to reduce. Its screen sat on "loading data".
    """
    connection = make_connection()
    frames = connection.frames_for(b"\x84" + bytes(4000))
    assert len(frames) > 2, "large enough to split"

    # Building took no sequence numbers at all.
    (first,) = connection.send_message(b"\x88")
    assert int.from_bytes(first[1:4], "little") == 0, (
        "the frames built above must not have consumed sequence 0"
    )

    # Sealing them now continues from there, in the order they go out.
    sealed = [connection.seal(frame) for frame in frames]
    assert sequences_of(sealed) == list(range(1, len(frames) + 1))
