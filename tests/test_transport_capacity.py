"""Network work stays bounded when peers stall or send excessive records."""

import threading
import time
import socket
from unittest.mock import patch

import pytest

from dso_server.runtime import transport
from dso_server.runtime.outbound import BoundedOutboundSender, OutboundCapacityError
from dso_server.runtime.transport import RakNetServer, Session
from dso_server.runtime.gameplay_scheduler import GameplayDeadlineScheduler


class Sink:
    def __init__(self, expected=1):
        self.sent = []
        self.expected = expected
        self.done = threading.Event()

    def sendto(self, data, addr):
        self.sent.append((data, addr))
        if len(self.sent) >= self.expected:
            self.done.set()


def test_ack_never_waits_for_ordered_output_lock():
    sock = Sink()
    session = Session(("127.0.0.1", 40100), sock)
    completed = threading.Event()
    session.outbound_lock.acquire()
    thread = threading.Thread(target=lambda: (session.ack(3), completed.set()))
    try:
        thread.start()
        assert completed.wait(1), "ACK waited behind another producer's output"
    finally:
        session.outbound_lock.release()
        thread.join(1)
    assert sock.sent[0][0][0] == 0xC0


def test_paced_fragments_enqueue_without_sleep_and_keep_following_order():
    sock = Sink(expected=4)
    sender = BoundedOutboundSender(name="test-outbound", worker_count=1)
    session = Session(("127.0.0.1", 40101), sock, outbound_sender=sender)
    sender.start()
    try:
        # Sleep in FrameBuilder would block the caller and used to stop UDP
        # receive. The production queue expresses spacing as deadlines.
        with patch.object(transport.time, "sleep", side_effect=AssertionError("paced caller slept")):
            session.send_reliable_ordered_split(b"x" * (transport.FRAG_SIZE * 3), pace_seconds=0.02)
            session.send_reliable_ordered(b"after")
        assert sock.done.wait(2)
        frames = [f for packet, _ in sock.sent for f in RakNetServer._parse_frames(packet)]
        assert [f["order_idx"] for f in frames] == [0, 0, 0, 1]
        assert [f["split"]["idx"] for f in frames[:3]] == [0, 1, 2]
        assert frames[-1]["payload"] == b"after"
    finally:
        sender.close()


def test_unsent_reliable_frames_are_not_retransmitted():
    class DeferredSender:
        def enqueue(self, *args, **kwargs):
            pass

    session = Session(("127.0.0.1", 40102), Sink(), outbound_sender=DeferredSender())
    packet = session.send_reliable_ordered(b"queued")
    assert session.due_retransmissions(interval=0)[0] == []
    session._mark_send_attempt(packet)
    assert len(session.due_retransmissions(interval=0)[0]) == 1
    assert session.due_retransmissions(interval=0)[0] == []


def test_output_queue_rejects_an_oversized_peer_packet_before_allocating_mailbox():
    sender = BoundedOutboundSender(name="test-capacity", peer_bytes=8)
    session = Session(("127.0.0.1", 40103), Sink(), outbound_sender=sender)
    try:
        with pytest.raises(OutboundCapacityError):
            sender.enqueue(session, b"x" * 9)
        assert sender._mailboxes == {}
        assert sender._bytes == 0
    finally:
        sender.close()


def test_output_queue_has_peer_and_global_packet_limits():
    sender = BoundedOutboundSender(name="test-capacity", peer_packets=2, max_packets=2)
    session = Session(("127.0.0.1", 40104), Sink(), outbound_sender=sender)
    try:
        with patch.object(sender, "_schedule"):
            sender.enqueue(session, b"one")
            sender.enqueue(session, b"two")
            with pytest.raises(OutboundCapacityError):
                sender.enqueue(session, b"three")
        assert sender._packets == 2
        sender.discard(session)
        assert sender._packets == sender._bytes == 0
    finally:
        sender.close()


def test_reliable_count_budget_fails_peer_without_growing_backlog():
    session = Session(("127.0.0.1", 40105), Sink())
    with patch.object(transport, "RELIABLE_PENDING_LIMIT", 2):
        session.send_reliable_ordered(b"one")
        session.send_reliable_ordered(b"two")
        with pytest.raises(OutboundCapacityError):
            session.send_reliable_ordered(b"three")
    assert session.reliable_backlog_size() == 2
    assert session.transport_failure_reason


def test_reliable_byte_budget_and_ack_accounting():
    session = Session(("127.0.0.1", 40106), Sink())
    packet = session.send_reliable_ordered(b"one")
    assert session.pending_reliable_bytes == len(packet)
    session.acknowledge([int.from_bytes(packet[1:4], "little")])
    assert session.pending_reliable_bytes == 0
    with patch.object(transport, "RELIABLE_PENDING_BYTES", 16):
        with pytest.raises(OutboundCapacityError):
            session.send_reliable_ordered(b"too large")
    assert session.reliable_backlog_size() == 0


def test_recent_input_does_not_keep_a_peer_without_ack_progress_alive():
    session = Session(("127.0.0.1", 40107), Sink())
    session.connected = True
    session.send_reliable_ordered(b"pending")
    later = session.last_reliable_ack_progress_at + transport.RELIABLE_ACK_TIMEOUT_SECONDS + 1
    session.last_received_at = later
    assert RakNetServer._session_reliably_stale(session, later)
    session.acknowledge(list(session.pending_reliable))
    assert not RakNetServer._session_reliably_stale(session, later)


def test_newer_acks_do_not_hide_a_permanently_missing_oldest_frame():
    session = Session(("127.0.0.1", 40111), Sink())
    session.connected = True
    session.send_reliable_ordered(b"missing oldest")
    second = session.send_reliable_ordered(b"acknowledged newer")
    oldest = next(iter(session._pending_reliable_order.values()))
    now = oldest['first_sent_at'] + transport.RELIABLE_ACK_TIMEOUT_SECONDS + 1
    session.acknowledge([int.from_bytes(second[1:4], "little")])
    session.last_received_at = session.last_reliable_ack_progress_at = now
    assert RakNetServer._session_reliably_stale(session, now)
    assert len(session._pending_reliable_order) == 1


def test_idle_sessions_expire_without_pending_output_and_before_connect():
    session = Session(("127.0.0.1", 40108), Sink())
    assert RakNetServer._session_reliably_stale(
        session, session.last_received_at + transport.HANDSHAKE_TIMEOUT_SECONDS + 1,
    )
    session.connected = True
    assert RakNetServer._session_reliably_stale(
        session, session.last_received_at + transport.RELIABLE_SESSION_IDLE_TIMEOUT_SECONDS + 1,
    )


def test_offline_pings_do_not_create_gameplay_sessions():
    server = RakNetServer("127.0.0.1", 0, "test")
    server.sock = Sink()
    for port in range(41000, 42000):
        server._dispatch(b"\x01", ("127.0.0.1", port))
    assert len(server.sessions) == 0
    assert len(server.sock.sent) == 1000


def test_rejected_handshakes_do_not_allocate_sessions():
    server = RakNetServer("127.0.0.1", 0, "test")
    server.sock = Sink()
    server.connection_filter = lambda _ip: False
    server._dispatch(b"\x05", ("127.0.0.1", 42000))
    server._dispatch(b"\x07", ("127.0.0.1", 42001))
    assert len(server.sessions) == 0


def test_handshake_admission_obeys_global_session_cap():
    server = RakNetServer("127.0.0.1", 0, "test")
    server.sock = Sink()
    with patch.object(transport, "MAX_SESSIONS", 2):
        for port in range(42000, 42003):
            server._dispatch(b"\x05", ("127.0.0.1", port))
    assert len(server.sessions) == 2


def test_ack_range_expansion_has_an_aggregate_budget():
    record = b"\x00" + (0).to_bytes(3, "little") + (4096).to_bytes(3, "little")
    packet = b"\xc0" + (584).to_bytes(2, "big") + record * 584
    assert len(packet) == 4091
    assert RakNetServer._parse_ack_sequences(packet) == []


def test_ack_ranges_are_deduplicated():
    record = b"\x00" + (1).to_bytes(3, "little") + (3).to_bytes(3, "little")
    assert RakNetServer._parse_ack_sequences(b"\xc0\x00\x02" + record * 2) == [1, 2, 3]


def test_nak_retries_are_burst_limited_and_keep_byte_accounting():
    session = Session(("127.0.0.1", 40109), Sink())
    for _ in range(5):
        session.send_reliable_ordered(b"pending")
    before = session.pending_reliable_bytes
    retries, _ = session.retransmit(list(session.pending_reliable), interval=0, burst_limit=2)
    assert len(retries) == 2
    assert session.pending_reliable_bytes == before


def test_retirement_request_does_not_run_cleanup_inline():
    server = RakNetServer("127.0.0.1", 0, "test")
    server.sock = Sink()
    session = server._session(("127.0.0.1", 40110))
    server.request_session_retirement(session, "overloaded")
    assert not session.retired
    with patch.object(server, "_retire_session") as retire:
        server._process_retransmit_session(session, time.perf_counter())
    retire.assert_called_once_with(session, reason="overloaded", remove=True)


def test_running_server_survives_receive_timeout_and_sends_queued_handshake():
    server = RakNetServer("127.0.0.1", 0, "test")
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(2)
    try:
        server.start()
        destination = server.sock.getsockname()
        assert server.gameplay_command_offload
        time.sleep(0.08)
        client.sendto(b"\x01", destination)
        assert client.recvfrom(4096)[0][0] == 0x1C
        assert not server.sessions
        client.sendto(b"\x05", destination)
        assert client.recvfrom(4096)[0][0] == 0x06
        assert len(server.sessions) == 1
        assert server.session_snapshot()[0].outbound_sender is server._outbound_sender
    finally:
        client.close()
        server.stop()
    assert not server.gameplay_command_offload


def test_retirement_waits_for_mutation_then_runs_cleanup_before_snapshot():
    server = RakNetServer("127.0.0.1", 0, "test")
    server.sock = Sink()
    session = server._session(("127.0.0.1", 40112))
    session.connected = True
    calls = []
    started = threading.Event()
    completed = threading.Event()
    session.gameplay_retire_callback = lambda s: calls.append("trade-cleanup")
    session.gameplay_persist_callback = lambda s, reason, flush: calls.append(s.audit_mutation)

    def retire():
        started.set()
        server._retire_session(session, reason="test", remove=True)
        completed.set()

    thread = threading.Thread(target=retire)
    with session.gameplay_mutation_lock:
        thread.start()
        assert started.wait(1)
        assert not completed.wait(0.05)
        assert calls == []
        session.audit_mutation = "committed"
    thread.join(2)
    assert completed.is_set()
    assert calls == ["trade-cleanup", "committed"]


def test_blocked_production_retirement_keeps_receive_and_other_peer_ack_free():
    server = RakNetServer("127.0.0.1", 0, "test")
    sock = server.sock = Sink()
    cleanup = server._lifecycle_scheduler = GameplayDeadlineScheduler(
        name="test-lifecycle", worker_count=1, max_pending=4,
    )
    cleanup.start()
    old = server._session(("127.0.0.1", 42100))
    other = server._session(("127.0.0.1", 42101))
    old.connected = other.connected = True
    persisting = threading.Event()
    release = threading.Event()
    persisted = threading.Event()

    def persist(s, reason, flush):
        persisting.set()
        assert release.wait(2)
        persisted.set()

    old.gameplay_persist_callback = persist
    try:
        server._dispatch(b"\x05", old.addr)
        assert persisting.wait(1)
        assert old.retiring
        assert server.sessions.get(old.addr) is old
        # A retry cannot replace this endpoint until final persistence ends.
        server._dispatch(b"\x05", old.addr)
        assert server.sessions.get(old.addr) is old
        server._dispatch(b"\x84\x00\x00\x00", other.addr)
        assert any(packet[0] == 0xC0 and addr == other.addr for packet, addr in sock.sent)
        release.set()
        cleanup.shutdown(wait=True, cancel_pending=False, timeout=2)
        server._lifecycle_scheduler = None
        assert persisted.is_set()
        assert server.sessions.get(old.addr) is None
        server._dispatch(b"\x05", old.addr)
        assert server.sessions.get(old.addr) is not old
    finally:
        release.set()
        cleanup.shutdown(wait=True, cancel_pending=False, timeout=2)
        server._lifecycle_scheduler = None


def test_terminal_disconnect_bypasses_the_outbox_discarded_by_retirement():
    server = RakNetServer("127.0.0.1", 0, "test")
    sock = server.sock = Sink()
    sender = BoundedOutboundSender(name="test-terminal")
    session = Session(("127.0.0.1", 42102), sock, outbound_sender=sender)
    server.sessions[session.addr] = session
    session.connected = True
    try:
        # Keep ordinary output queued until retirement discards it. The
        # terminal message must already have been submitted to the socket.
        with patch.object(sender, "_schedule"):
            session.send_reliable_ordered(b"obsolete payload")
            server.drop_session(session, reason="test")
        assert session.retired
        frames = [f for packet, _ in sock.sent for f in RakNetServer._parse_frames(packet)]
        assert [f["payload"] for f in frames] == [b"\x15"]
        assert sender._packets == 0
    finally:
        sender.close()


def test_retirement_waits_for_initialization_and_observes_its_late_callbacks():
    from dso_server.services.character_server import CharacterServer
    from dso_server.services.player_initialization import PlayerInitializationService

    server = RakNetServer("127.0.0.1", 0, "test")
    server.sock = Sink()
    cleanup = server._lifecycle_scheduler = GameplayDeadlineScheduler(
        name="test-init-retirement", worker_count=1,
    )
    cleanup.start()
    session = server._session(("127.0.0.1", 42103))
    session.connected = True
    started = threading.Event()
    release = threading.Event()
    saved = []
    initializations = []

    def initialize(_server, s, *, scheduled=False):
        initializations.append(s)
        started.set()
        assert release.wait(2)
        s.audit_initialized = True
        s.gameplay_persist_callback = lambda value, reason, flush: saved.append(value.audit_initialized)

    with patch.object(CharacterServer, "_send_local_player_initialization_locked", initialize):
        thread = threading.Thread(
            target=PlayerInitializationService._send_local_player_initialization,
            args=(server, session),
        )
        try:
            thread.start()
            assert started.wait(1)
            server._retire_session(session, reason="client disconnect", remove=True)
            assert session.retiring
            assert saved == []
            assert server.sessions.get(session.addr) is session
            release.set()
            thread.join(2)
            cleanup.shutdown(wait=True, cancel_pending=False, timeout=2)
            assert saved == [True]
            assert server.sessions.get(session.addr) is None
            PlayerInitializationService._send_local_player_initialization(server, session)
            assert initializations == [session]
        finally:
            release.set()
            thread.join(2)
            cleanup.shutdown(wait=True, cancel_pending=False, timeout=2)
            server._lifecycle_scheduler = None
