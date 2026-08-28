"""Per-connection state: sequencing, acknowledgement and message assembly.

One :class:`Connection` exists per remote endpoint.  It owns everything that
must not be shared, which is the part most often got wrong when a server keeps
global state:

* the **outgoing datagram sequence**, which starts at 0 and increments per
  datagram sent on this connection;
* the **outgoing reliable message number** and **ordering index**, which are
  independent of the datagram sequence and of the other direction;
* the set of **incoming datagram sequences still to acknowledge**;
* the **fragment reassembly buffers** for split messages.

The Drakensang client makes this concrete: it opens two connections, to the
login service and the character service, *from the same RakNet peer and with the
same client GUID*.  Keying state by GUID, or holding one global "connected"
flag, cannot serve that client.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto

from .address import SystemAddress, decode_address, encode_address, encode_unspecified
from .constants import MAX_INTERNAL_IDS, DatagramFlag, MessageID, Reliability
from .datagram import (
    SEQUENCE_MODULO,
    build_ack,
    build_datagram_header,
    coalesce,
    parse_ack_payload,
    parse_datagram_header,
)
from .frame import Frame, build_frame, parse_frames
from .payload import bits_of
from .offline import UDP_IPV4_OVERHEAD

#: Widest frame header we may emit: flags(1) + bit length(2) + reliable(3)
#: + ordering(3) + channel(1) + split count(4) + split id(2) + split index(4).
MAX_FRAME_HEADER = 20


class State(Enum):
    """Where a connection is in its lifecycle."""

    #: The offline handshake finished; waiting for CONNECTION_REQUEST.
    OPENING = auto()
    #: CONNECTION_REQUEST_ACCEPTED sent; game messages may flow.
    CONNECTED = auto()
    CLOSED = auto()


@dataclass
class _SplitBuffer:
    """Pieces of one split message, keyed by their index."""

    count: int
    pieces: dict[int, bytes] = field(default_factory=dict)

    def add(self, index: int, payload: bytes) -> bytes | None:
        """Store a piece; return the whole message once every piece has arrived."""
        self.pieces[index] = payload
        if len(self.pieces) != self.count:
            return None
        return b"".join(self.pieces[i] for i in range(self.count))


@dataclass
class IncomingMessage:
    """A complete application message handed up from the transport."""

    message_id: int
    payload: bytes
    reliability: Reliability


@dataclass
class _Retained:
    """A sent datagram kept in case it has to go out again."""

    datagram: bytes
    sent_at_ms: int
    #: How many times this has gone on the wire. In the reference session one
    #: fragment was sent 230 times, so there is no cap here on purpose.
    attempts: int = 1


class Connection:
    """The transport state for one remote peer.

    The class deliberately does no I/O.  It turns bytes into messages and
    messages into bytes, and the caller does the sending — which is what makes
    it testable against a recorded capture with no socket involved.
    """

    def __init__(
        self,
        remote: SystemAddress,
        local: SystemAddress,
        server_guid: bytes,
        mtu: int,
        started_at_ms: int | None = None,
    ) -> None:
        self.remote = remote
        self.local = local
        self.server_guid = server_guid
        self.mtu = mtu
        self.state = State.OPENING
        self.client_guid: bytes | None = None
        self._started_at_ms = started_at_ms if started_at_ms is not None else _now_ms()

        self._next_datagram_sequence = 0
        self._next_reliable_index = 0
        self._next_ordering_index: dict[int, int] = defaultdict(int)
        #: Sequenced messages number themselves from here, not from the ordering
        #: counter. Sharing one counter punches holes in the ordered stream.
        self._next_sequencing_index: dict[int, int] = defaultdict(int)
        self._next_split_id = 0
        self._pending_acks: list[int] = []
        self._splits: dict[int, _SplitBuffer] = {}
        #: Datagrams still worth resending, by sequence number. A peer that NAKs
        #: asks for these by number, so they have to be kept after sending.
        #:
        #: This is not an optimisation. Half of a 409-fragment message was measured
        #: lost between a host and a VM guest, and a split message never completes
        #: if one piece is missing — so without retransmission nothing larger than
        #: a single datagram is deliverable at all.
        self._sent: dict[int, _Retained] = {}
        #: How many recent datagrams to keep. 1024 covers a 631 KB message twice
        #: over; keeping everything would grow without bound on a long session.
        self.retain = 1024
        #: How long to wait for an ACK before resending unprompted.
        #:
        #: Waiting for a NAK is not enough, and the reference session says so
        #: plainly: the real service sent 32,176 fragment datagrams to deliver 596
        #: distinct fragments — a fifty-four-fold amplification, sustained for a
        #: hundred seconds. It was not answering NAKs, it was resending everything
        #: unacknowledged on a timer. Answering NAKs only means one lost NAK, or a
        #: client that stops asking, strands a fragment forever; the ordered stream
        #: behind it never advances and the client waits on a screen that never
        #: changes.
        self.resend_after_ms = 300
        #: Sequences the peer reported missing, for logging and diagnosis.
        self.nacked: list[int] = []
        #: Sequences a peer asked for that were no longer retained.
        self.lost: list[int] = []

    # ── time ────────────────────────────────────────────────────────────────

    def elapsed_ms(self) -> int:
        """Milliseconds since this server started, RakNet's timestamp unit."""
        return _now_ms() - self._started_at_ms

    # ── receiving ───────────────────────────────────────────────────────────

    def receive(self, raw: bytes) -> list[IncomingMessage]:
        """Decode one datagram, returning the complete messages it yielded.

        Acknowledgements are recorded for the next :meth:`flush_acks`; split
        frames are buffered and only surface once whole.
        """
        header, offset = parse_datagram_header(raw)
        if not header.is_valid:
            raise ValueError(
                f"{raw[0]:#02x} is not a connected datagram; "
                "unconnected messages belong to raknet.offline"
            )
        if header.is_ack:
            acked = parse_ack_payload(raw, 1, header.has_bandwidth_figure)
            # Acknowledged datagrams can never be asked for again, so drop them.
            for span in acked.ranges:
                for sequence in span:
                    self._sent.pop(sequence, None)
            return []
        if header.is_nak:
            payload = parse_ack_payload(raw, 1, False)
            for span in payload.ranges:
                self.nacked.extend(span)
            return []


        self._pending_acks.append(header.sequence)

        messages: list[IncomingMessage] = []
        for frame in parse_frames(raw, offset):
            complete = self._reassemble(frame)
            if complete is None:
                continue
            if complete:
                messages.append(
                    IncomingMessage(complete[0], complete, frame.reliability)
                )
        return messages

    def _reassemble(self, frame: Frame) -> bytes | None:
        if not frame.is_split:
            return frame.payload
        buffer = self._splits.get(frame.split_id)
        if buffer is None:
            buffer = _SplitBuffer(frame.split_count)
            self._splits[frame.split_id] = buffer
        whole = buffer.add(frame.split_index, frame.payload)
        if whole is not None:
            del self._splits[frame.split_id]
        return whole

    # ── sending ─────────────────────────────────────────────────────────────

    def flush_acks(self) -> bytes | None:
        """Build an ACK for every datagram received since the last call.

        Returning ``None`` when there is nothing to acknowledge keeps callers
        from sending empty datagrams.  Acknowledging is not optional: without it
        the peer retransmits and then declares the connection lost.
        """
        if not self._pending_acks:
            return None
        ranges = coalesce(self._pending_acks)
        self._pending_acks.clear()
        return build_ack(ranges)

    def due_retransmissions(self, limit: int) -> list[bytes]:
        """Unacknowledged datagrams whose resend timer has expired, oldest first.

        Oldest first because a peer reassembling a split message is blocked on its
        earliest gap: resending a later fragment it already holds advances nothing.

        *limit* keeps this from crowding out new sends. The caller decides the
        budget, since the pacing rate is its business, not the connection's.
        """
        now = _now_ms()
        due = sorted(
            (sequence for sequence, held in self._sent.items()
             if now - held.sent_at_ms >= self.resend_after_ms)
        )
        out: list[bytes] = []
        for sequence in due[:limit]:
            held = self._sent[sequence]
            held.sent_at_ms = now
            held.attempts += 1
            out.append(held.datagram)
        return out

    @property
    def unacknowledged(self) -> int:
        """How many sent datagrams are still waiting for an ACK."""
        return len(self._sent)

    def take_retransmissions(self, raw: bytes) -> list[bytes]:
        """Datagrams to resend in answer to a NAK.

        A sequence the peer asks for but that is no longer retained cannot be
        recovered, and is reported rather than passed over: silently skipping it
        leaves the peer waiting for a piece that will never arrive.
        """
        header, _ = parse_datagram_header(raw)
        if not header.is_nak:
            return []
        payload = parse_ack_payload(raw, 1, False)
        out: list[bytes] = []
        for span in payload.ranges:
            for sequence in span:
                held = self._sent.get(sequence)
                if held is not None:
                    held.sent_at_ms = _now_ms()
                    held.attempts += 1
                    out.append(held.datagram)
                else:
                    self.lost.append(sequence)
        return out

    def _retain(self, sequence: int, datagram: bytes) -> None:
        self._sent[sequence] = _Retained(datagram=datagram, sent_at_ms=_now_ms())
        if len(self._sent) > self.retain:
            for old in sorted(self._sent)[: len(self._sent) - self.retain]:
                del self._sent[old]

    def frames_for(
        self,
        payload: bytes,
        reliability: Reliability = Reliability.RELIABLE_ORDERED,
        channel: int = 0,
    ) -> list[Frame]:
        """Split *payload* into frames, without assigning datagram sequences.

        Separating this from :meth:`seal` is what makes a paced or throttled queue
        possible. A datagram's sequence number is a *transmit-order* counter: the
        peer treats a gap in it as loss and asks for everything missing. Numbering
        at build time and sending later manufactures exactly that gap — 592
        fragments numbered at once but released six a second had the client NAK all
        592, repeatedly, which both defeated the throttle and doubled the traffic it
        was meant to reduce.
        """
        budget = self.mtu - UDP_IPV4_OVERHEAD - 4 - MAX_FRAME_HEADER
        if budget <= 0:
            raise ValueError(f"MTU {self.mtu} leaves no room for a payload")

        if len(payload) <= budget:
            return [self._make_frame(payload, reliability, channel)]
        return self._split(payload, reliability, channel, budget)

    def seal(self, frame: Frame) -> bytes:
        """Give *frame* the next datagram sequence number and return the datagram.

        Call this when the datagram actually goes on the wire. Reliable datagrams
        are retained here, since that is also when the resend timer should start.
        """
        sequence = self._take_sequence()
        datagram = build_datagram_header(sequence) + build_frame(frame)
        # Only reliable datagrams are worth keeping. A peer NAKs by reliable
        # index, so there is nothing to ask for otherwise — and resending an
        # unreliable one defeats its purpose: a retransmitted clock reading is
        # a stale timestamp presented as current, which is the opposite of
        # what a time sync is for.
        if frame.reliability.has_reliable_index:
            self._retain(sequence, datagram)
        return datagram

    def send_message(
        self,
        payload: bytes,
        reliability: Reliability = Reliability.RELIABLE_ORDERED,
        channel: int = 0,
    ) -> list[bytes]:
        """Wrap *payload* into datagrams, splitting it if it exceeds the MTU.

        Sequences are assigned immediately, so this suits anything sent straight
        away — the handshake, and tests. Anything that will sit in a queue should
        use :meth:`frames_for` and seal at transmit time instead.
        """
        return [self.seal(frame) for frame in self.frames_for(payload, reliability, channel)]

    def _split(
        self, payload: bytes, reliability: Reliability, channel: int, budget: int
    ) -> list[Frame]:
        total_bits = bits_of(payload)
        chunks = [payload[i : i + budget] for i in range(0, len(payload), budget)]
        split_id = self._next_split_id
        self._next_split_id = (self._next_split_id + 1) % 0x10000
        # Every piece of a split message shares one ordering index: it is one
        # message as far as ordering is concerned.
        ordering_index = self._take_ordering_index(channel)
        frames = []
        for index, chunk in enumerate(chunks):
            # Only the last piece can be short of a byte: the ones before it are
            # whole chunks of the payload, so their bit length is exactly their size.
            before = index * budget
            frame = Frame(
                payload=chunk,
                bit_length=min(len(chunk) * 8, total_bits - before * 8),
                reliability=reliability,
                ordering_index=ordering_index if reliability.has_ordering_index else None,
                ordering_channel=channel,
                split_count=len(chunks),
                split_id=split_id,
                split_index=index,
            )
            if reliability.has_reliable_index:
                frame.reliable_index = self._take_reliable_index()
            frames.append(frame)
        return frames

    def _make_frame(
        self, payload: bytes, reliability: Reliability, channel: int
    ) -> Frame:
        frame = Frame(
            payload=payload,
            reliability=reliability,
            ordering_channel=channel,
            # The payload's own bit length when it knows one. Declaring len * 8 for
            # everything is what left one to seven spare bits at the end of every
            # frame this server sent, which a multi-command reader takes for the
            # start of another command. See raknet.payload.
            bit_length=bits_of(payload),
        )
        if reliability.has_reliable_index:
            frame.reliable_index = self._take_reliable_index()
        if reliability.has_sequencing_index:
            # A sequenced frame carries both fields, and the ordering one must be
            # *reported* rather than consumed: it names the index the next ordered
            # message will use, and the frame numbers itself from a separate
            # counter.
            #
            # Taking one from each — which is what two independent `if`s did here —
            # spends two ordering indices on every sequenced message and leaves a
            # hole in the ordered stream. The peer cannot deliver anything past a
            # hole, so one time-sync announcement was enough to strand the
            # character roster and everything behind it in the client's reorder
            # buffer for ever. The capture agrees with this reading: the real
            # server's 0x83 and the roster that follows it both carry ordering
            # index 4.
            frame.sequencing_index = self._take_sequencing_index(channel)
            frame.ordering_index = self._next_ordering_index[channel]
        elif reliability.has_ordering_index:
            frame.ordering_index = self._take_ordering_index(channel)
            # RakNet restarts sequenced numbering whenever an ordered message goes
            # out on the channel, so the peer discards stale sequenced frames from
            # before it rather than comparing against a counter that never resets.
            self._next_sequencing_index[channel] = 0
        return frame

    def _take_sequence(self) -> int:
        value = self._next_datagram_sequence
        self._next_datagram_sequence = (value + 1) % SEQUENCE_MODULO
        return value

    def _take_reliable_index(self) -> int:
        value = self._next_reliable_index
        self._next_reliable_index = (value + 1) % SEQUENCE_MODULO
        return value

    def _take_ordering_index(self, channel: int) -> int:
        value = self._next_ordering_index[channel]
        self._next_ordering_index[channel] = (value + 1) % SEQUENCE_MODULO
        return value

    def _take_sequencing_index(self, channel: int) -> int:
        """The next sequencing index for *channel*, from its own counter."""
        value = self._next_sequencing_index[channel]
        self._next_sequencing_index[channel] = (value + 1) % SEQUENCE_MODULO
        return value

    # ── RakNet-level messages ───────────────────────────────────────────────

    def handle_connection_request(self, payload: bytes) -> bytes:
        """Accept a CONNECTION_REQUEST and build the acceptance payload.

        Layout of the request, confirmed on the wire (18 bytes without a
        password): id, client GUID, client timestamp, "use security" flag.
        """
        if payload[0] != MessageID.CONNECTION_REQUEST:
            raise ValueError(f"expected 0x09, got {payload[0]:#02x}")
        self.client_guid = payload[1:9]
        client_timestamp = int.from_bytes(payload[9:17], "big")
        self.state = State.CONNECTED
        return self.build_connection_request_accepted(client_timestamp)

    def build_connection_request_accepted(
        self,
        client_timestamp: int,
        system_index: int = 0,
        server_timestamp: int | None = None,
    ) -> bytes:
        """CONNECTION_REQUEST_ACCEPTED: 96 bytes, exactly as the real server sends.

        The client's own timestamp is echoed back before ours, which is how the
        client measures the round trip.  The internal-address list is padded to
        :data:`MAX_INTERNAL_IDS` with 0.0.0.0:0 — the first entry is this
        server's local address, and hardcoding somebody else's LAN address there
        is a mistake worth naming, because it is invisible in testing on one
        machine.

        *server_timestamp* defaults to this connection's clock; it is a parameter
        so a test can reproduce a recorded packet exactly.
        """
        if server_timestamp is None:
            server_timestamp = self.elapsed_ms()
        parts = [
            bytes([MessageID.CONNECTION_REQUEST_ACCEPTED]),
            encode_address(self.remote),
            system_index.to_bytes(2, "big"),
            encode_address(self.local),
        ]
        parts += [encode_unspecified()] * (MAX_INTERNAL_IDS - 1)
        parts += [
            client_timestamp.to_bytes(8, "big"),
            server_timestamp.to_bytes(8, "big"),
        ]
        return b"".join(parts)

    def build_connected_pong(self, client_timestamp: int) -> bytes:
        """Answer a CONNECTED_PING, echoing the client's clock then ours."""
        return b"".join(
            [
                bytes([MessageID.CONNECTED_PONG]),
                client_timestamp.to_bytes(8, "big"),
                self.elapsed_ms().to_bytes(8, "big"),
            ]
        )

    def parse_new_incoming_connection(self, payload: bytes) -> SystemAddress:
        """Read the address the client believes it connected to."""
        if payload[0] != MessageID.NEW_INCOMING_CONNECTION:
            raise ValueError(f"expected 0x13, got {payload[0]:#02x}")
        address, _ = decode_address(payload, 1)
        return address


def _now_ms() -> int:
    return int(time.monotonic() * 1000)
