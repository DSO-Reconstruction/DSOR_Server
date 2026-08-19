"""Regression tests for the bugs that stopped the previous implementation.

Each test below fails if the corresponding mistake is reintroduced.  They are
kept together and named after the symptom because all four were found by reading
a capture rather than by reading code, and all four look harmless in a diff.
"""

from raknet.address import SystemAddress, encode_address
from raknet.connection import Connection
from raknet.datagram import parse_datagram_header
import time
from pathlib import Path

from dsor.messages import (
    HANDOFF_TRAILER_MAP,
    build_server_handoff,
    build_time_sync_reply,
    parse_message,
)
from dsor.recorded import (
    CHARACTER_RELEASE_SEQUENCE,
    CHARACTER_SELECTION_SEQUENCE,
    MAP_ENTRY_SEQUENCE,
    client_query_reply,
    payload,
)
from raknet.constants import Reliability
from raknet.frame import parse_frames
from raknet.offline import build_open_connection_reply_1
from server import SCHEDULE_FRAGMENTS_PER_SECOND, Service

GUID = bytes.fromhex("00063fb2731c5c79")
CLIENT = SystemAddress("78.112.59.92", 52758)
SERVER = SystemAddress("10.208.115.155", 2190)


def test_reply_1_security_and_mtu_are_exactly_three_bytes(by_frame):
    """Was: a four-byte literal, announcing MTU 128 and a stray byte.

    Symptom: the client stops after the second packet of the session.
    """
    reply = build_open_connection_reply_1(GUID, 1292)
    tail = reply[1 + 16 + 8 :]
    assert len(tail) == 3, "security flag (1) + MTU (2)"
    assert tail == b"\x00\x05\x0c"
    assert reply == by_frame[2]


def test_address_bytes_are_complemented_not_reversed():
    """Was: the IPv4 bytes reversed instead of complemented.

    Symptom: the client computes a nonsense external address for itself and
    abandons the connection with no error.
    """
    encoded = encode_address(SystemAddress("192.168.1.47", 1234))
    assert encoded[1:5] == bytes(b ^ 0xFF for b in bytes([192, 168, 1, 47]))
    assert encoded[1:5] != bytes([47, 1, 168, 192])


def test_datagram_sequence_is_not_constant():
    """Was: three zero bytes hardcoded, mislabelled 'length in bit'.

    Symptom: the client accepts the first datagram and silently drops the rest
    as duplicates, which is indistinguishable from the client ignoring us.
    """
    connection = Connection(
        remote=SystemAddress("10.0.0.2", 5000),
        local=SystemAddress("10.0.0.1", 2190),
        server_guid=GUID,
        mtu=1292,
    )
    sequences = [
        parse_datagram_header(connection.send_message(b"\x84")[0])[0].sequence
        for _ in range(3)
    ]
    assert sequences == [0, 1, 2], "a constant sequence number breaks delivery"


def test_ack_datagrams_are_recognised_as_connected():
    """Was: a dispatch range of 0x80..0x8d, which excludes ACKs at 0xC0.

    Symptom: every acknowledgement the client sends is dropped, so the server
    can never know what arrived.
    """
    ack_first_byte = 0xC0
    assert ack_first_byte & 0x80, "the test of validity is the top bit, not a range"
    assert not 0x80 <= ack_first_byte <= 0x8D, "the old range really did exclude it"
    header, _ = parse_datagram_header(bytes([ack_first_byte, 0, 0, 0]))
    assert header.is_valid and header.is_ack


def test_the_account_transfer_is_sent_with_the_roster_not_on_release():
    """Was: the 717 KB 0x84/0x00DD held back until the client asked to enter the
    world.

    Symptom: the roster appears, the client time-syncs indefinitely, and the Play
    button never arms — so the request that would have released the transfer never
    arrives. A deadlock in which the server waits for a click that waits for the
    server.

    The reference session settles it: the real service sends the transfer at
    ordering index 6, immediately behind the roster (4) and the acknowledgement
    (5), starting at frame 103 — while the client's request arrives at frame 19147.
    """
    assert "character_enter_bulk.bin" in CHARACTER_SELECTION_SEQUENCE
    assert "character_enter_bulk.bin" not in CHARACTER_RELEASE_SEQUENCE
    assert CHARACTER_SELECTION_SEQUENCE.index("character_list.bin") == 0, (
        "the roster is ordering index 4 and goes first"
    )


def test_the_release_answers_the_selection_command_and_nothing_else():
    """Was: the release keyed to 0x8B/0x0086 and built out of three messages, one of
    them a character-creation reply.

    The two opcodes were the wrong way round, and it was the most expensive mistake
    in this project. 0x0087 is CharacterSelectionCommand and 0x0086 is
    CharacterGenerationCommand. The Play button sends 0x0087 with operation 3 and
    waits for operation 5; the 0x0086 in the reference session at frame 19147 was
    the player *creating* a character.

    So the two 0x0087 dismissed as "ignored by the real service" were the Play
    clicks themselves, and this server answered neither — which is exactly why the
    client appeared never to offer the button. It offered it, the user clicked it,
    and nothing came back.
    """
    assert CHARACTER_RELEASE_SEQUENCE == (
        "character_chosen_reply.bin",   # 0x84/0x0087 operation 5, the grant
        "character_release_extra.bin",  # 0x84/0x0070, the empty handoff
    )
    # The creation reply must not be sent to a client that selected an existing
    # character: it never asked to create one.
    assert "character_enter_ack.bin" not in CHARACTER_RELEASE_SEQUENCE


def test_the_grant_carries_operation_five_and_a_non_zero_character():
    """Both are required. The client checks the operation to leave its
    start-game-sent state, and refuses a grant whose character id is zero.
    """
    grant = parse_message(payload("character_chosen_reply.bin"))
    assert (grant.message_id, grant.opcode) == (0x84, 0x0087)
    assert grant.body[0] == 5, "operation 5 is the grant"
    assert grant.body[1] == 0, "no deny reason"
    assert int.from_bytes(grant.body[2:6], "little") != 0, "a real character id"


def test_every_replayed_release_payload_exists_and_parses():
    """The sequence names files; a typo would only surface as a client that hangs."""
    for name in CHARACTER_SELECTION_SEQUENCE + CHARACTER_RELEASE_SEQUENCE:
        message = parse_message(payload(name))
        assert message.message_id == 0x84
        assert message.opcode is not None


def test_the_010b_answers_are_not_sent_as_part_of_the_release():
    """Was: the two 0x010E/0x010C pairs sent unprompted with the release, because
    their ordering indices (10-13) follow it.

    Ordering index says what order messages are delivered in, not what prompted
    them. The client asks twice with 0x8B/0x010B and the pairs answer those
    requests, so sending them unasked is two messages the client never wanted.
    """
    assert not any("010e" in name for name in CHARACTER_RELEASE_SEQUENCE)
    assert not any("010c" in name for name in CHARACTER_RELEASE_SEQUENCE)
    assert len(CHARACTER_RELEASE_SEQUENCE) == 2


def test_the_two_010b_answers_are_not_interchangeable():
    """The second query gets the larger 0x010E — 2008 bytes against 1573."""
    first, second = client_query_reply(0), client_query_reply(1)
    assert first != second
    assert len(second[0]) > len(first[0])


def test_an_unreliable_message_spends_no_ordering_index():
    """Was: keepalives sent RELIABLE_ORDERED, so each one consumed an ordering
    index in the same stream as the messages that matter.

    Measured effect on the character service: the roster arrived at ordering index
    8 where the real service puts it at 4, with twenty-nine time-sync replies and
    pongs interleaved ahead of it — and the resend timer kept every one of them
    alive for ever, because a reliable message is retained until acknowledged.
    """
    connection = Connection(remote=CLIENT, local=SERVER, server_guid=GUID, mtu=1292)
    connection.send_message(b"\x03" + bytes(8), reliability=Reliability.UNRELIABLE)
    (datagram,) = connection.send_message(b"\x84\x87\x00")

    (frame,) = parse_frames(datagram, parse_datagram_header(datagram)[1])
    assert frame.ordering_index == 0, "the unreliable pong must not have used index 0"


def test_a_clock_reading_is_never_retransmitted():
    """A retransmitted timestamp is a stale reading presented as current.

    Sequencing exists so a late one is dropped instead of delivered; the real
    service was measured sending its time sync UNRELIABLE_SEQUENCED.
    """
    connection = Connection(remote=CLIENT, local=SERVER, server_guid=GUID, mtu=1292)
    connection.send_message(
        b"\x83" + bytes(16), reliability=Reliability.UNRELIABLE_SEQUENCED
    )
    connection.resend_after_ms = 0
    assert connection.due_retransmissions(limit=8) == []
    assert connection.unacknowledged == 0


def test_each_tier_answers_time_syncs_as_often_as_the_real_one_did():
    """Counted per connection in the reference session: login 0 answers out of 1
    request, character 1 out of 24, map 17 out of 20.

    Answering all of them on the character service left the client
    re-synchronising instead of finishing — its own counter climbed past 2784.
    """
    def service(role: str, port: int) -> Service:
        return Service(port=port, name=f"test-{role}", role=role)

    login = service("login", 42190)
    character = service("character", 42192)
    map_server = service("map", 42193)
    try:
        assert login._answers_time_sync(0) is False
        assert character._answers_time_sync(0) is True
        assert character._answers_time_sync(1) is False
        assert map_server._answers_time_sync(0) is True
        assert map_server._answers_time_sync(19) is True
    finally:
        for s in (login, character, map_server):
            s.socket.close()


def test_an_acknowledgement_does_not_wait_behind_the_paced_queue():
    """Was: acknowledgements queued with everything else.

    Measured cost: the client's ready signal arrived in datagram 6 and its
    acknowledgement left 594 datagrams later — 2.4 seconds at the paced rate,
    because a 717 KB transfer was ahead of it. The client resends an
    unacknowledged reliable message roughly every 100 ms, so it sent that one
    signal twenty-three times, all carrying reliable index 4, and never got past
    the selection screen.

    Pacing protects the peer's receive buffer from a burst. A seven-byte
    acknowledgement is not a burst, and holding it back generates traffic.
    """
    service = Service(port=42194, name="test-ack", role="character")
    sent: list[tuple[bytes, tuple[str, int]]] = []
    service.socket.close()
    service.socket = type("Stub", (), {"sendto": lambda _self, data, to: sent.append((data, to))})()
    try:
        service._send_ack(b"\xc0\x00\x01\x01\x06\x00\x00", ("172.20.0.2", 64811))
        assert sent, "the acknowledgement never reached the socket"
        assert not service.outbound, "it must not sit in the paced queue"
    finally:
        pass


def test_the_time_sync_body_reproduces_the_real_announcement_bytes():
    """Was: the client's own clock echoed back to it.

    The real character service announced 354,685,782 and the client's next reading
    was 354,685,819 — it adopts whatever it is told. Echoing its own value back
    gives it no world clock, and it reports that it is still synchronising for as
    long as it is left running; the user's client counted past 2784.

    354,685,782 is 0x15241356, so little-endian it is the exact four bytes the real
    service put on the wire.
    """
    announced = 354_685_782
    body = build_time_sync_reply(announced)

    assert body[0] == 0x83
    assert body[1:5] == bytes.fromhex("56132415"), "the real service's four bytes"
    assert len(body) == 17, "a clock and twelve zero bytes, as measured"
    assert body[5:] == bytes(12)


def test_the_clock_is_announced_rather_than_only_answered():
    """The real service sent its clock at frame 99 and the client first asked at
    frame 110, so it is an announcement, not a reply — and the character service
    sent exactly one all session.
    """
    service = Service(port=42195, name="test-clock", role="character")
    try:
        assert hasattr(service, "_announce_game_clock")
        assert service._answers_time_sync(0) is True
        assert service._answers_time_sync(1) is False
    finally:
        service.socket.close()


def test_the_handoff_reproduces_the_real_bytes_for_each_destination():
    """Was: one trailer byte, 0x80, for every handoff.

    The three in the reference session do not agree. The one pointing at the
    character service ends 0x80; the one pointing at a map server and the empty one
    that releases a client both end 0x00. Reproduced per destination rather than
    assumed constant.
    """
    to_character = build_server_handoff("game-481-01-public.prod.drakensang.com:2192")
    assert to_character.hex(" ").startswith("84 70 00 2b 00 67 61 6d 65")
    assert to_character.endswith(b"\x80")
    assert len(to_character) == 49, "the real one was 49 bytes"

    to_map = build_server_handoff("47.245.165.152:30201", HANDOFF_TRAILER_MAP)
    assert to_map == bytes.fromhex(
        "84 70 00 14 00 34 37 2e 32 34 35 2e 31 36 35 2e 31 35 32 "
        "3a 33 30 32 30 31 00".replace(" ", "")
    )

    release = build_server_handoff("", HANDOFF_TRAILER_MAP)
    assert release == bytes.fromhex("847000000000"), "six bytes, ending 0x00"


def test_the_login_order_depends_on_where_the_client_is_sent():
    """Was: one order for both destinations — first the map order for both, then
    the character order for both.

    The real login server uses both, and the ordering indices on its two
    connections say which is which:

        to the character service   handoff 2, signal 3
        to a map server            signal 2, handoff 3

    Measuring this needs the two connections kept apart. Ordering indices restart
    with each connection, so a survey that merges them reads one connection's
    handoff against the other's signal — which is how the wrong order got in.
    """
    source = Path("server.py").read_text()
    dispatch = source[source.index("chosen = self.sessions.character_chosen") :]
    dispatch = dispatch[: dispatch.index("log.info")]
    assert "(signal, handoff) if chosen else (handoff, signal)" in dispatch


def test_a_sequenced_message_leaves_no_hole_in_the_ordered_stream():
    """Was: a sequenced frame took one index from the ordering counter for its
    sequencing field and another for its ordering field, spending two per message.

    Symptom, exactly as observed: the selection screen never appeared and the
    client sat on the map named in the 0x86 it had already received. One time-sync
    announcement before the roster consumed ordering indices 4 and 5, the roster
    landed at 6, and a peer cannot deliver past a missing index — so the roster and
    everything behind it stayed in the client's reorder buffer for ever.

    The capture is unambiguous: the real server's 0x83 carries ordering index 4 and
    so does the roster that follows it. A sequenced frame reports the index the next
    ordered message will use; it does not claim one.
    """
    connection = Connection(remote=CLIENT, local=SERVER, server_guid=GUID, mtu=1292)
    ordered = []
    for payload in (b"\x82\x00\x00", b"\x86\x00\x00", b"\x88"):
        (datagram,) = connection.send_message(payload)
        (frame,) = parse_frames(datagram, parse_datagram_header(datagram)[1])
        ordered.append(frame.ordering_index)
    assert ordered == [0, 1, 2], "three ordered messages take three indices"

    (clock,) = connection.send_message(
        b"\x83" + bytes(16), reliability=Reliability.UNRELIABLE_SEQUENCED
    )
    (sequenced,) = parse_frames(clock, parse_datagram_header(clock)[1])
    assert sequenced.ordering_index == 3, "it reports the next ordered index"
    assert sequenced.sequencing_index == 0, "and numbers itself from its own counter"

    (datagram,) = connection.send_message(b"\x84\x87\x00")
    (roster,) = parse_frames(datagram, parse_datagram_header(datagram)[1])
    assert roster.ordering_index == 3, (
        "the roster must take the index the sequenced frame only reported, "
        "as the real server's roster does after its 0x83"
    )


def test_sequenced_numbering_restarts_after_an_ordered_message():
    """RakNet resets the sequenced counter when an ordered message goes out on the
    channel, so a peer discards stale sequenced frames from before it."""
    connection = Connection(remote=CLIENT, local=SERVER, server_guid=GUID, mtu=1292)
    for _ in range(3):
        connection.send_message(
            b"\x83" + bytes(16), reliability=Reliability.UNRELIABLE_SEQUENCED
        )
    connection.send_message(b"\x84\x87\x00")
    (again,) = connection.send_message(
        b"\x83" + bytes(16), reliability=Reliability.UNRELIABLE_SEQUENCED
    )
    (frame,) = parse_frames(again, parse_datagram_header(again)[1])
    assert frame.sequencing_index == 0


def test_the_event_schedule_is_released_slowly_and_separately():
    """Was: all 592 fragments of the 717 KB schedule queued with everything else,
    landing in 2.4 seconds.

    Symptom: the client crashed with an access violation while building its
    character screen. Its own log gives the order away. Against the real server it
    logs the screen being built and switched *before* "handle event updates";
    against this one, receiving the schedule forty times faster, it logs "handle
    event updates" first and dies in the audio thread the screen build starts next.

    The real service delivered those fragments over 101.6 seconds — not by design,
    but because 54 of every 55 datagrams it sent were retransmissions. The client
    depends on the result regardless, so the rate is reproduced deliberately.
    """
    service = Service(port=42196, name="test-slow", role="character")
    sent: list[bytes] = []
    service.socket.close()
    service.socket = type(
        "Stub", (), {"sendto": lambda _s, data, _to: sent.append(data)}
    )()
    try:
        for index in range(20):
            service._send_slow(bytes([index]), ("172.20.0.2", 1))
        assert not service.outbound, "the schedule must not use the fast queue"

        # No time has passed, so no credit has accrued.
        service._slow_credit = 0.0
        service._slow_last = time.monotonic()
        assert service.flush_slow() == 0

        # A second's worth of credit releases the measured rate, and no more.
        service._slow_last -= 1.0
        assert service.flush_slow() == int(SCHEDULE_FRAGMENTS_PER_SECOND)
        assert len(service.slow) == 20 - int(SCHEDULE_FRAGMENTS_PER_SECOND)
    finally:
        pass


def test_an_infinite_rate_drains_the_slow_queue():
    """What the offline replay harness uses, so its tests do not take two minutes
    to deliver a message that takes a hundred seconds in production."""
    service = Service(port=42197, name="test-drain", role="character")
    sent: list[bytes] = []
    service.socket.close()
    service.socket = type(
        "Stub", (), {"sendto": lambda _s, data, _to: sent.append(data)}
    )()
    service.slow_rate = float("inf")
    for index in range(50):
        service._send_slow(bytes([index]), ("172.20.0.2", 1))
    assert service.flush_slow() == 50
    assert not service.slow


def test_the_map_entry_includes_the_zone_content():
    """Was: ordering index 6 skipped, so the entry went 4, 5, 7, 8.

    Symptom, exactly as observed: the client completes all three tiers, arrives on
    the map, and dies on `localPlayerActor is valid()`. The 631 KB 0x85/0x001D is
    what populates the world — the player's own actor included — so without it the
    client is standing in an empty zone and asserts on the first thing that needs
    the player to exist.

    The opcodes are asserted against the real map server's ordering indices 4 to 8,
    since a list of file names cannot show its own order is right.
    """
    opcodes = [parse_message(payload(name)).opcode for name in MAP_ENTRY_SEQUENCE]
    assert opcodes == [
        0x001B,   # index 4, the acknowledgement
        0x0114,   # 5, the cosmetics table
        0x001D,   # 6, the zone content — 631 KB in 513 fragments
        0x00A7,   # 7
        0x0074,   # 8
    ]
    assert len(payload("zone_content.bin")) == 631_240
