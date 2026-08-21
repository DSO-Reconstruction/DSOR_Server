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

import pytest

from dsor.messages import (
    HANDOFF_TRAILER_MAP,
    build_server_handoff,
    build_time_sync_reply,
    parse_message,
)
from dsor.recorded import (
    CHARACTER_RELEASE_SEQUENCE,
    HANDLE_FROM_END,
    describable_mobs,
    entity_descriptions,
    entity_handle,
    first_command,
    combat_ready_mobs,
    commands_for,
    departure_commands,
    describable_mobs,
    hit_commands,
    vicinity_announcements,
    mob_templates,
    CHARACTER_SELECTION_SEQUENCE,
    MAP_ENTRY_SEQUENCE,
    client_query_reply,
    payload,
)
from dsor.gameplay import ENTITY_SEPARATOR, Position, actor_id
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


def test_the_event_schedule_is_held_back_and_kept_off_the_fast_queue():
    """Two mistakes in sequence, and the second was mine correcting the first badly.

    Delivering the 717 KB schedule immediately crashed the client: it processed the
    schedule before it had finished building the character screen, and died in the
    audio thread the screen build starts next.

    Throttling the whole transfer to the real service's six fragments a second fixed
    the crash and replaced it with a worse symptom. The schedule is ordering index 6
    and the grant, the handoff and the shop are 7, 8 and 9 — none of which a peer can
    deliver until 6 completes. So the client sat on "loading data" for the ninety-
    eight seconds that took. The real service did the same and its player waited
    forty-nine seconds after clicking; that was an accident of a lossy link, not a
    property worth reproducing.

    What the client actually needs is for the schedule to arrive *after* the screen,
    not slowly. So: hold the start, then send at the normal rate.
    """
    service = Service(port=42196, name="test-slow", role="character")
    sent: list[bytes] = []
    service.socket.close()
    service.socket = type(
        "Stub", (), {"sendto": lambda _s, data, _to: sent.append(data)}
    )()
    for index in range(20):
        service._send_slow_sealed(bytes([index]), ("172.20.0.2", 1))
    assert not service.outbound, "the schedule must not use the fast queue"

    # Inside the hold, nothing leaves however much credit has accrued.
    service._slow_since = time.monotonic()
    service._slow_last = time.monotonic() - 10.0
    assert service.flush_slow() == 0
    assert not sent

    # Past it, the queue drains at the ordinary rate rather than a trickle.
    service._slow_since = time.monotonic() - service.slow_delay - 1.0
    service._slow_last = time.monotonic() - 1.0
    assert service.flush_slow() == 20
    assert len(sent) == 20


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
        service._send_slow_sealed(bytes([index]), ("172.20.0.2", 1))
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


def test_every_recorded_creature_can_be_described_by_its_own_handle():
    """Was: the client's 0x8B/0x001C left unanswered.

    That request is how the client asks what an entity is, and its four-byte body
    begins with the entity's index. A position update for an entity it has never
    heard of is ignored, so without an answer the creature never exists: the map
    loads, the player walks, and nothing appears. The client asks for as long as it
    runs — 136 times against this server, against 8 in the whole reference session.

    The pairing is not guesswork. Each recorded description carries the same
    four-byte handle the client asks with, 260 bytes from its end.
    """
    descriptions = entity_descriptions()
    assert len(descriptions) == 6

    for handle, description in descriptions.items():
        assert entity_handle(description) == handle, "self-consistent by construction"
        assert parse_message(description).opcode == 0x002A
        assert handle[1:] == b"\x00\x01\x00", "the handle's shape, index first"

    # The indices are exactly the creatures the recorded position updates carry.
    assert sorted(handle[0] for handle in descriptions) == [
        0x08, 0x0A, 0x0B, 0x0F, 0x10, 0x12
    ]


def test_the_handle_offset_is_a_rule_and_not_a_table():
    """It sits 260 bytes from the end in every description — offset 151 in the
    411-byte ones, 137 in the 397-byte one, 146 in the 406-byte one. Derivable
    means a description can be recognised without being listed.
    """
    for description in entity_descriptions().values():
        offset = len(description) - HANDLE_FROM_END
        assert description[offset : offset + 4] == entity_handle(description)


def test_a_description_too_short_to_hold_a_handle_is_refused():
    with pytest.raises(ValueError, match="at least"):
        entity_handle(bytes(10))


def test_only_entities_that_can_be_described_are_served():
    """Was: eight creature records served with only six descriptions to go with them.

    The client discards a position for an actor it does not know, asks what the actor
    is, and waits. Measured in one session: the six records with a description were
    asked about exactly **once** each, and the two without were asked **25 times**
    each. Answering ends the question; serving an entity you cannot describe is worse
    than not serving it.

    The two excluded ones are not monsters. The client has a separate command per
    kind — NewMonsterCommand for creatures, NewNPCCommand and NewDestroyableCommand
    for the others — and the reference session answered their requests with neither of
    the descriptions collected here.
    """
    served = describable_mobs()
    descriptions = entity_descriptions()
    assert len(served) == 6
    assert len(mob_templates()) == 8, "two are deliberately held back"
    for record in served:
        assert record[15:19] in descriptions

    excluded = {r[15:19] for r in mob_templates()} - {r[15:19] for r in served}
    assert excluded == {b"\x13\x00\x01\x00", b"\x14\x00\x01\x00"}


def test_an_entity_record_ends_with_a_terminator_not_a_separator():
    """A correction worth keeping, because the wrong model produced right bytes.

    This treated 0x5F 0x00 as a separator between chained records. It is not: 0xFF at
    the end of each record is a per-command terminator, and 0x5F 0x00 is simply the
    *next command's id* — the client reads a batch of commands, each one ending in
    0xFF. The two models emit identical bytes, so the mistake was invisible until the
    client's own decoder was read.

    A record is therefore 15 bytes of payload, a 4-byte actor id, and the terminator.
    """
    for record in mob_templates():
        assert len(record) == 20
        assert record[19] == 0xFF, "the terminator, not part of a separator"

    # What earlier passes called an index and an id were parts of other fields: byte
    # 15 is the low byte of the 32-bit actor id, and bytes 9-12 are a start tick.
    record = mob_templates()[0]
    assert record[15] == record[15:19][0]
    assert int.from_bytes(record[9:13], "little") == 2492


def test_a_description_is_a_batch_and_its_trailer_names_the_player():
    """What looked like one message per creature is several.

    The client reads commands in sequence, each ending in 0xFF, until fewer than
    sixteen bits remain. So a 411-byte "description" is the creature's
    NewMonsterCommand followed by more commands — and the actor id in the *batch's*
    trailer is the player's, 15 00 01 00, in all six. Matching on that would have
    addressed every creature as the player.

    Nothing here is byte-aligned either: every batch ends three bits short of its last
    byte, which is why a twelve-byte write at a fixed byte offset corrupted one.
    """
    for handle, batch in entity_descriptions().items():
        assert batch[-1] == 0xF8, "0xFF shifted three bits, then zero padding"

        only = first_command(batch, handle)
        assert len(only) < len(batch), "the batch holds more than this creature"
        assert only[0] == 0x85
        assert int.from_bytes(only[1:3], "little") == 0x002A
        assert batch.startswith(only[:-1]), "a prefix, not a rebuild"


def test_every_served_creature_has_a_vicinity_announcement():
    """Was: the announcement leg of the exchange skipped entirely.

    The real exchange has three legs — the server announces that an actor is near,
    the client asks what it is, the server describes it — and only the last two were
    implemented. The client's handler for the announcement calls RequestActor
    directly, so an actor announced this way is set up by the path the real server
    used; one the client merely noticed in a position update is not.

    Each recorded announcement is 111 bytes and names exactly one creature, and in
    the reference session each arrived at the very frame that creature first appeared.
    """
    announcements = vicinity_announcements()
    descriptions = entity_descriptions()
    assert set(announcements) == set(descriptions), "one per creature we can describe"

    for actor, announcement in announcements.items():
        message = parse_message(announcement)
        assert (message.message_id, message.opcode) == (0x85, 0x0074)
        assert len(announcement) == 111
        assert actor in message.body, "the announcement names its actor"


def test_a_creature_has_no_health_message_only_a_hit():
    """The correction that needed a second capture to establish.

    This server reported a creature's health with 0x007B ActorStatsUpdateCommand,
    over several attempts, and nothing ever happened. Two captured sessions say why:
    154 stats updates between them, **every one** targeting the player — including in
    the session where six creatures were killed. A creature has no health message.

    What the killing session does show is an exact pair per death: a large
    0x85/0x006B HitCommand naming both the creature and the player, then a
    0x85/0x0073 ActorsLeftVicinityCommand naming the creature. Frames 5219 then 5410
    for the first, 5525 then 5733 for the second, and so on for all six.
    """
    hits = hit_commands()
    assert len(hits) == 6, "one per creature killed in the reference session"
    for actor, hit in hits.items():
        message = parse_message(hit)
        assert (message.message_id, message.opcode) == (0x85, 0x006B)
        assert actor in message.body, "the hit names its creature"
        assert len(hit) > 200, "the small 160-byte hits name no creature at all"

    # Five of the six also name the player who landed the blow. The sixth, a
    # 545-byte one, does not — asserting that all of them did was a guess this test
    # caught, and the exception is kept rather than smoothed over because it means
    # the player is not a required field.
    naming_player = [
        actor for actor, hit in hits.items()
        if b"\x15\x00\x01\x00" in parse_message(hit).body
    ]
    assert len(naming_player) == 5

    departures = departure_commands()
    for actor, departure in departures.items():
        message = parse_message(departure)
        assert (message.message_id, message.opcode) == (0x85, 0x0073)
        assert actor in message.body


def test_only_creatures_that_can_be_removed_are_served():
    """Was: a creature served with no removal message to go with it.

    It reached zero health, the client drew it dead, and it never went away — so the
    player could keep striking a corpse. Its removal arrived in the capture grouped
    with another actor's, so it could not be isolated; one of the six is dropped for
    that reason rather than served half-working.

    The same rule already applies to descriptions: serve nothing you cannot also
    explain and undo.
    """
    ready = combat_ready_mobs()
    hits = hit_commands()
    for record in ready:
        assert record[15:19] in hits, "its blow is still a replay"

    # A recorded *removal* is no longer needed: DiscardMonsterCommand is generated and
    # its body is empty. The creature that used to be held back for want of one — its
    # removal arrived grouped with another actor's — is servable again.
    assert len(ready) == 6
    assert {r[15:19] for r in ready} == {r[15:19] for r in describable_mobs()}


def test_a_hit_keeps_the_commands_addressed_to_its_creature():
    """Two wrong answers before this one, and each broke something the other fixed.

    A recorded hit is a batch of ten commands or more. Sending all of it teleported
    the creature and dropped its loot where the other session's player stood; sending
    only its first command lost whatever made it disappear, so it died at zero health
    and stayed on screen. What belongs to the creature is the commands addressed to
    it — one per 32-bit actor id before each 0xFF terminator.
    """
    hits = hit_commands()
    kept = {}
    for actor, batch in hits.items():
        try:
            kept[actor] = commands_for(batch, actor)
        except ValueError:
            continue

    assert len(kept) == 5, "one hit has no detectable command boundary"
    for actor, filtered in kept.items():
        batch = hits[actor]
        assert len(filtered) < len(batch), "the cascade is dropped"
        assert len(filtered) > 100, "and more than just the blow survives"
        assert filtered[0] == 0x85
        assert int.from_bytes(filtered[1:3], "little") == 0x006B


def test_a_dead_creature_is_not_re_announced():
    """Was: the tick reported every served creature's position regardless of health.

    Symptom: a creature was removed, redeclared a tick later, removed again — it
    flickered and stayed on screen. Telling a client to forget something and then
    immediately reminding it of the same thing is not a protocol problem.
    """
    service = Service(port=42198, name="test-tick", role="map", map_name="a0001_start_tutorial_dun")
    try:
        service.mobs = 5
        living = combat_ready_mobs()[:5]
        assert living, "the fixture set is not empty"

        # Measured by length, not by counting the separator: that two-byte pattern
        # also occurs inside the records as data, which is the same naivety that once
        # made this project read it as a separator in the first place.
        header, record, joiner = 3, 20, len(ENTITY_SEPARATOR)

        before = service._entity_update(Position(-10172, -344, 5888))
        assert len(before) == header + record + len(living) * (joiner + record)

        # Kill them all and the update carries the player alone.
        for entry in living:
            service.mob_health[actor_id(entry)] = 0.0
        after = service._entity_update(Position(-10172, -344, 5888))
        assert len(after) == header + record
    finally:
        service.socket.close()


def test_a_corpse_stays_in_the_tick_long_enough_to_fall():
    """Was: a creature dropped from the entity update the instant it died.

    The wire showed it plainly — the update went from 155 bytes to 133 in the same
    breath as the kill, one record and its separator — so the client was told to play
    a death sequence over something this server had stopped mentioning. It vanished
    instead, with no animation and nothing in its log.

    Bounded on the other side too: reporting a creature after the client has finished
    removing it flickers it back into existence, which is a mistake this file already
    records once.
    """
    service = Service(
        port=42200, name="test-corpse", role="map", map_name="a0001_start_tutorial_dun"
    )
    try:
        service.mobs = 1
        (creature,) = combat_ready_mobs()[:1]
        actor = actor_id(creature)
        alive = len(service._entity_update(Position(-10172, -344, 5888)))

        # Dead, but freshly so: still reported.
        service.mob_health[actor] = 0.0
        service.corpse_ticks[actor] = service.corpse_lifetime
        assert len(service._entity_update(Position(-10172, -344, 5888))) == alive

        # The grace period runs out over that many ticks, and then it is gone.
        for _ in range(service.corpse_lifetime + 1):
            service.game_tick()
        assert actor not in service.corpse_ticks
        gone = len(service._entity_update(Position(-10172, -344, 5888)))
        assert gone == alive - 22, "one twenty-byte record and its two-byte joiner"
    finally:
        service.socket.close()
