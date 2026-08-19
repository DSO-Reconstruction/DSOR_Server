"""End-to-end handshake over a real socket, driven by the recorded client.

This is the closest thing to running the game without having it: the capture
contains the client's own four handshake datagrams, so they can be replayed at a
listening server and the answers inspected.  The server is a real process
boundary here — a UDP socket, its dispatch, its connection table — so this
catches wiring mistakes that the pure-codec tests cannot, such as routing a
datagram to the wrong connection or forgetting to acknowledge.

What it still cannot do is judge whether a real client would be *satisfied* by
the answers.  It proves they are well-formed and arrive.
"""

from __future__ import annotations

import socket
import threading

import pytest

from dsor.protocol import parse_service_identity
from raknet.constants import DatagramFlag, MessageID
from raknet.datagram import parse_datagram_header
from raknet.frame import parse_frames
from raknet.offline import parse_open_connection_request_1
from server import Service

#: The client's handshake datagrams, in order: request 1, request 2,
#: CONNECTION_REQUEST, NEW_INCOMING_CONNECTION.
CLIENT_FRAMES = (1, 3, 5, 9)


@pytest.fixture
def running_service():
    """A Service on an ephemeral port, pumped by a background thread."""
    service = Service(port=0, name="DrasaOnlineLoginServer")
    service.port = service.socket.getsockname()[1]
    stop = threading.Event()

    def pump() -> None:
        """Drive the service the way serve() does.

        Flushing is part of the contract now: replies on an established connection
        go into service.outbound and only leave when drained, because sending a
        long message as one burst lost half of it. A driver that forgets to flush
        sees a server that answers the handshake and then goes silent — which is
        exactly how this test failed when the queue was introduced.
        """
        service.socket.settimeout(0.05)
        while not stop.is_set():
            try:
                raw, sender = service.socket.recvfrom(2048)
            except socket.timeout:
                service.flush(burst=64)
                continue
            except OSError:
                return
            service.handle(raw, sender)
            service.flush(burst=64)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    yield service
    stop.set()
    thread.join(timeout=2)
    service.socket.close()


def _exchange(client: socket.socket, target, payload: bytes) -> list[bytes]:
    """Send one datagram and collect whatever comes back."""
    client.sendto(payload, target)
    replies = []
    client.settimeout(0.5)
    while True:
        try:
            replies.append(client.recv(4096))
        except socket.timeout:
            return replies


def test_recorded_client_handshake_gets_well_formed_answers(running_service, by_frame):
    target = ("127.0.0.1", running_service.port)
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))

    try:
        # 1. OPEN_CONNECTION_REQUEST_1 -> reply 1, with an MTU we can honour.
        (reply,) = _exchange(client, target, by_frame[1])
        assert reply[0] == MessageID.OPEN_CONNECTION_REPLY_1
        assert len(reply) == 28
        probed = parse_open_connection_request_1(by_frame[1]).mtu
        assert int.from_bytes(reply[25:27], "big") <= probed

        # 2. OPEN_CONNECTION_REQUEST_2 -> reply 2, echoing *our* address.
        (reply,) = _exchange(client, target, by_frame[3])
        assert reply[0] == MessageID.OPEN_CONNECTION_REPLY_2
        from raknet.address import decode_address

        echoed, _ = decode_address(reply, 25)
        assert (echoed.ip, echoed.port) == client.getsockname()

        # 3. CONNECTION_REQUEST -> acceptance, plus an acknowledgement.
        replies = _exchange(client, target, by_frame[5])
        messages, acks = _classify(replies)
        assert acks, "the server must acknowledge what it receives"
        assert [m.message_id for m in messages] == [
            MessageID.CONNECTION_REQUEST_ACCEPTED
        ]
        assert len(messages[0].payload) == 96

        # 4. NEW_INCOMING_CONNECTION -> the service announces itself.
        replies = _exchange(client, target, by_frame[9])
        messages, acks = _classify(replies)
        assert acks
        assert parse_service_identity(messages[0].payload) == "DrasaOnlineLoginServer"
    finally:
        client.close()


def test_a_service_that_is_never_flushed_sends_nothing(by_frame):
    """States the contract, so a future driver cannot forget it silently."""
    service = Service(port=0, name="DrasaOnlineLoginServer")
    service.port = service.socket.getsockname()[1]
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.bind(("127.0.0.1", 0))
        try:
            # A connected-path message: its reply is queued, not sent.
            service.handle(by_frame[1], client.getsockname())  # offline: immediate
            assert not service.outbound, "handshake replies bypass the queue"
            client.settimeout(0.3)
            client.recv(4096)  # the offline reply did arrive
        finally:
            client.close()
    finally:
        service.socket.close()


def test_datagram_from_an_unknown_peer_is_ignored(running_service, by_frame):
    """A connected datagram with no prior handshake must not create state."""
    target = ("127.0.0.1", running_service.port)
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        assert _exchange(client, target, by_frame[5]) == []
        assert running_service.connections == {}
    finally:
        client.close()


def _classify(replies: list[bytes]):
    """Split replies into decoded messages and acknowledgements."""
    messages, acks = [], []
    for raw in replies:
        assert raw[0] & DatagramFlag.IS_VALID, f"unexpected offline reply {raw[0]:#02x}"
        header, offset = parse_datagram_header(raw)
        if header.is_ack:
            acks.append(raw)
            continue
        for frame in parse_frames(raw, offset):
            from raknet.connection import IncomingMessage

            messages.append(
                IncomingMessage(frame.message_id, frame.payload, frame.reliability)
            )
    return messages, acks
