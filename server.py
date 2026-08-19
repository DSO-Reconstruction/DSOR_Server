"""A RakNet server that a Drakensang Online client can complete a handshake with.

Run it, point the client at this host, and it will take a connection as far as
the service-identity message.  Everything past that is the game protocol, which
is not implemented here.

The shape to notice is that one process listens on **several ports**, each an
independent service with its own GUID, and that connection state is keyed by
``(ip, port)`` rather than by client GUID — because the client opens a
connection to every service from the same peer, reusing one GUID.

    python server.py                  # listen on every known service port
    python server.py --port 2190      # just the login service
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import selectors
import socket
import time

from dsor.messages import (
    KNOWN_OPCODES,
    TIME_SYNC,
    build_bare_signal,
    build_map_assignment,
    HANDOFF_TRAILER_CHARACTER,
    HANDOFF_TRAILER_MAP,
    build_server_handoff,
    build_time_sync_reply,
    build_timestamped,
    parse_message,
    read_client_identity,
    read_map_assignment,
    read_server_handoff,
)
from dsor.protocol import SERVICE_PORTS, build_service_identity
from dsor.gameplay import (
    SPAWN_POSITIONS,
    decode_client_movement,
    encode_entity_update,
)
from dsor.recorded import (
    CHARACTER_CHOSEN_NAME,
    character_release,
    client_query_reply,
    map_entry_sequence,
    tick_state,
    character_selection,
    entity_update_template,
)
from raknet.address import SystemAddress
from raknet.connection import Connection, State
from raknet.constants import DatagramFlag, MessageID, Reliability
from raknet.offline import (
    RAKNET_PROTOCOL_VERSION,
    HandshakeError,
    build_incompatible_protocol_version,
    build_open_connection_reply_1,
    build_open_connection_reply_2,
    parse_open_connection_request_1,
    parse_open_connection_request_2,
)

log = logging.getLogger("dsor")

#: Deliberately fixed rather than random, so packet captures of two runs can be
#: compared. A real deployment should randomise it per service.
DEFAULT_GUID = bytes.fromhex("00063fb2731c5c79")


#: Commands::CharacterSelectionCommand. Its body begins with an 8-bit operation,
#: and that operation is the whole state machine of the selection screen.
SELECTION_COMMAND = 0x0087

#: Commands::CharacterGenerationCommand — character *creation*, not entering the
#: world. Naming these two the other way round was the most expensive mistake in
#: this project: it made the Play click look like a message the real service
#: ignored, so this server ignored it too and the client waited for a grant that
#: never came. The 0x0086 in the reference session was the player creating a
#: character. Creation is not implemented here.
GENERATION_COMMAND = 0x0086

#: Operations carried by SELECTION_COMMAND, read off the wire and confirmed against
#: the client's own dispatch table:
#:
#:   1  server -> client   here is the character list
#:   2  client -> server   send me the character list
#:   3  client -> server   start the game with the selected character
#:   4  server -> client   refused, with a reason
#:   5  server -> client   granted; the client enters its GameState
#:   7  server -> client   refresh the list
#:   9  server -> client   update one character
#:
#: The client will not send 2 on a fresh login — it expects operation 1 unsolicited
#: — and after sending 3 it waits for 5 with no timeout and no retry beyond a
#: second attempt.
OPERATION_PUSH_LIST = 1
OPERATION_REQUEST_LIST = 2
OPERATION_START_GAME = 3
OPERATION_DENY = 4
OPERATION_GRANT = 5

#: Datagrams a second for the event schedule, the one message that must arrive
#: slowly. Measured on the real service: 596 fragments delivered in 101.6 seconds.
#: It was not throttling on purpose — 54 of every 55 datagrams it sent were
#: retransmissions — but the client depends on the result either way.
SCHEDULE_FRAGMENTS_PER_SECOND = 6.0

#: A query the client sends twice, after the release and before it disconnects.
#: Each one is answered with a 0x010E/0x010C pair, and the second answer is the
#: larger of the two.
CLIENT_QUERY_OPCODE = 0x010B


class Capture:
    """Records every datagram to a file in the same format as a reference capture.

    The point is comparability. Diagnosing a client that reaches a screen and stops
    has cost several round trips of guess, restart, retest — each one testing a
    single hypothesis. A recording in the reference format can be diffed against
    the real session at message level, which tests every hypothesis at once.

    Fields match the reference: frame number, direction, endpoint, port, hex.
    """

    def __init__(self, path: str) -> None:
        self._file = open(path, "w", buffering=1)
        self._frame = 0

    def record(self, raw: bytes, port: int, sender: tuple[str, int], *, from_server: bool) -> None:
        self._frame += 1
        json.dump(
            {
                "frame": self._frame,
                "from_server": from_server,
                "conn": f"{sender[0]}:{sender[1]}",
                "server_port": port,
                "hex": raw.hex(),
            },
            self._file,
        )
        self._file.write("\n")

    def close(self) -> None:
        self._file.close()


class Sessions:
    """What each client has already done, keyed by its RakNet GUID.

    Shared by all three services because the real topology needs it: the login
    server is a *dispatcher* that a client comes back to before every zone change,
    and where it sends that client depends on whether character selection has
    happened yet. The GUID is the only identifier stable across those reconnects —
    the address and port change every time.
    """

    def __init__(self) -> None:
        self._chosen: set[bytes] = set()

    def character_chosen(self, guid: bytes | None) -> bool:
        return guid is not None and guid in self._chosen

    def mark_chosen(self, guid: bytes | None) -> None:
        if guid is not None:
            self._chosen.add(guid)


class Service:
    """One listening port and the connections on it."""

    def __init__(
        self,
        port: int,
        name: str,
        guid: bytes = DEFAULT_GUID,
        role: str = "login",
        character_target: str | None = None,
        map_target: str | None = None,
        map_name: str | None = None,
        sessions: "Sessions | None" = None,
        capture: "Capture | None" = None,
    ) -> None:
        self.port = port
        self.name = name
        self.guid = guid
        #: Wire recorder, or None. Shared between services so one file holds the
        #: whole session across all three tiers, in one frame numbering.
        self.capture = capture
        #: "login", "character" or "map". The three tiers behave differently on
        #: the same messages, so the role is explicit rather than inferred from the
        #: port number.
        self.role = role
        self.sessions = sessions if sessions is not None else Sessions()
        #: Login role only. Where to send a client that has not chosen a character
        #: yet, and where to send one that has. Both are "host:port" as the client
        #: must be able to reach them.
        self.character_target = character_target
        self.map_target = map_target
        #: Map to assign once a client authenticates here. The character service
        #: answers a 0x8A with the map name written twice and then a bare 0x88; the
        #: client validates the two copies against each other.
        self.map_name = map_name
        self.connections: dict[tuple[str, int], Connection] = {}
        #: Endpoints already sent the zone. The client repeats its ready signal
        #: when something is still missing, and answering each repeat with another
        #: 499 KB is self-defeating: the burst costs fragments, the missing
        #: fragments stop the reassembly, and the client retries again. Six repeats
        #: were observed inside one second.
        #: Clients already sent the character roster, so a repeated ready signal
        #: does not send it twice.
        self.record_sent: set[tuple[str, int]] = set()
        #: How many 0x010B queries each endpoint has asked, since the two recorded
        #: answers differ and are not interchangeable.
        self.queries_seen: dict[tuple[str, int], int] = {}
        #: How many time syncs each endpoint has sent, since how many get answered
        #: depends on the tier. See _answers_time_sync.
        self.time_syncs_answered: dict[tuple[str, int], int] = {}
        #: Clients that have finished entering the world and are being ticked.
        self.in_world: set[tuple[str, int]] = set()
        #: Where each of them last was, so a tick can report it.
        self.positions: dict[tuple[str, int], object] = {}
        #: Datagrams waiting to go out, paced by the main loop. Sending 409
        #: fragments in a tight loop lost half of them; the peer's buffer cannot
        #: absorb a burst that size and nothing retransmits what it drops.
        self.outbound: collections.deque[tuple[bytes, tuple[str, int]]] = (
            collections.deque()
        )
        #: Datagrams released at a deliberately slow rate, for the one message the
        #: client must NOT receive quickly. See _send_slow.
        self.slow: collections.deque[tuple[bytes, tuple[str, int]]] = (
            collections.deque()
        )
        #: Datagrams a second out of that queue. The real transfer delivered 596
        #: fragments in 101.6 seconds — six a second — because 54 out of every 55
        #: of its datagrams were retransmissions of fragments already sent.
        self.slow_rate = SCHEDULE_FRAGMENTS_PER_SECOND
        self._slow_credit = 0.0
        self._slow_last = time.monotonic()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("0.0.0.0", port))

    def _answers_time_sync(self, already_answered: int) -> bool:
        """Whether to answer a time sync, given how many this endpoint has sent.

        The counts come from the reference session; see the call site.
        """
        if self.role == "login":
            return False
        if self.role == "character":
            return already_answered == 0
        return True

    def local_address(self) -> SystemAddress:
        """This server's own address, as reported in CONNECTION_REQUEST_ACCEPTED.

        Resolved per call rather than cached so it stays right on a machine whose
        address changes, and so nobody is tempted to write a literal here.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            try:
                probe.connect(("10.255.255.255", 1))
                ip = probe.getsockname()[0]
            except OSError:
                ip = "127.0.0.1"
        return SystemAddress(ip, self.port)

    # ── dispatch ────────────────────────────────────────────────────────────

    def handle(self, raw: bytes, sender: tuple[str, int]) -> None:
        if self.capture is not None:
            self.capture.record(raw, self.port, sender, from_server=False)
        if raw[0] & DatagramFlag.IS_VALID:
            self._handle_connected(raw, sender)
        else:
            self._handle_offline(raw, sender)

    def _send(self, payload: bytes, sender: tuple[str, int]) -> None:
        """Queue a datagram. The main loop decides when it actually leaves."""
        self.outbound.append((payload, sender))

    def _send_slow(self, payload: bytes, sender: tuple[str, int]) -> None:
        """Queue a datagram for slow release.

        Exactly one message goes through here: the 717 KB event schedule. The
        client crashes if it arrives while the character screen is still being
        built, and the reason the real server never triggered that is an accident
        of its link quality — its transfer took 101.6 seconds to complete because
        it was resending fragments 54 times over, so the schedule landed long
        after the player had left the screen.

        The client's own log shows the ordering plainly. Against the real server it
        logs the screen being built and switched, and only then "handle event
        updates". Against this server, delivering the schedule in 2.4 seconds, it
        logs "handle event updates" first and dies in the audio thread that the
        screen build starts immediately afterwards.

        Slowing this down is therefore not a workaround for a bug of ours: it is
        reproducing a property of the real service that the client depends on.
        """
        self.slow.append((payload, sender))

    def _send_now(self, payload: bytes, sender: tuple[str, int]) -> None:
        """Send immediately, for the handshake replies a client waits on."""
        if self.capture is not None:
            self.capture.record(payload, self.port, sender, from_server=True)
        self.socket.sendto(payload, sender)

    def _announce_game_clock(self, connection: Connection, sender) -> None:
        """Tell the client this service's game clock, unprompted.

        The real service does this once, immediately after the ready signal and
        before the roster — frame 99 against the client's first request at frame
        110, so it is an announcement and not an answer. The client adopts the
        value: its next reading was 37 ms behind the announced one.

        Waiting to be asked and echoing the client's own clock back, as this did,
        never gives it a world clock at all. It reports that it is still
        synchronising for as long as it is left running.
        """
        reply = build_timestamped(
            connection.elapsed_ms(), build_time_sync_reply(connection.elapsed_ms())
        )
        for datagram in connection.send_message(
            reply, reliability=Reliability.UNRELIABLE_SEQUENCED
        ):
            self._send(datagram, sender)
        log.debug("%s: announced game clock %d", self.name, connection.elapsed_ms())

    def _send_ack(self, ack: bytes, sender: tuple[str, int]) -> None:
        """Send an acknowledgement immediately, ahead of the paced queue.

        Queueing these was measured costing 2.4 seconds. The client's ready signal
        arrived in datagram 6 and the acknowledgement covering it left 594
        datagrams later, because it had been put behind a 717 KB transfer at 250
        datagrams a second. The client resends an unacknowledged reliable message
        about every 100 ms, so it sent that one signal twenty-three times — all
        with reliable index 4, one message, not twenty-three requests — and spent
        the whole transfer waiting instead of advancing.

        Pacing exists to stop a burst overwhelming the peer's receive buffer. A
        seven-byte acknowledgement is not that burst, and delaying it *causes*
        traffic rather than sparing any.
        """
        self._send_now(ack, sender)

    def resend_due(self, limit: int) -> int:
        """Queue unacknowledged datagrams whose resend timer expired.

        Called when the outbound queue has room, so retransmissions use bandwidth
        new sends are not asking for. The real service kept a steady 250-300
        datagrams a second this way for a hundred seconds to land one 717 KB
        message.
        """
        queued = 0
        for sender, connection in self.connections.items():
            if queued >= limit:
                break
            for datagram in connection.due_retransmissions(limit - queued):
                self._send(datagram, sender)
                queued += 1
        return queued

    def flush_slow(self) -> int:
        """Release whatever the slow rate allows since the last call.

        Credit accumulates in fractions of a datagram so a rate below one per loop
        iteration still works; a rate of infinity drains the queue, which is what
        the offline replay harness uses so its tests do not take two minutes.
        """
        if not self.slow:
            return 0
        now = time.monotonic()
        if self.slow_rate == float("inf"):
            self._slow_credit = len(self.slow)
        else:
            self._slow_credit += (now - self._slow_last) * self.slow_rate
        self._slow_last = now

        sent = 0
        while self.slow and self._slow_credit >= 1.0:
            payload, sender = self.slow.popleft()
            self.socket.sendto(payload, sender)
            self._slow_credit -= 1.0
            sent += 1
        return sent

    def flush(self, burst: int) -> int:
        """Send at most *burst* queued datagrams. Returns how many went out."""
        sent = 0
        while self.outbound and sent < burst:
            payload, sender = self.outbound.popleft()
            if self.capture is not None:
                self.capture.record(payload, self.port, sender, from_server=True)
            self.socket.sendto(payload, sender)
            sent += 1
        return sent

    def _handle_offline(self, raw: bytes, sender: tuple[str, int]) -> None:
        # Offline replies go out immediately rather than through the queue: the
        # client retries the handshake on its own timer, so pacing these would
        # only slow the connection down.
        message_id = raw[0]
        try:
            if message_id == MessageID.OPEN_CONNECTION_REQUEST_1:
                request = parse_open_connection_request_1(raw)
                if request.protocol_version != RAKNET_PROTOCOL_VERSION:
                    log.warning(
                        "%s: client speaks RakNet protocol %d, we speak %d",
                        self.name,
                        request.protocol_version,
                        RAKNET_PROTOCOL_VERSION,
                    )
                    self._send_now(build_incompatible_protocol_version(self.guid), sender)
                    return
                # Never answer with more than the client proved the path carries.
                self._send_now(
                    build_open_connection_reply_1(self.guid, request.mtu), sender
                )
                log.info("%s: handshake 1/2 with %s, MTU %d", self.name, sender, request.mtu)

            elif message_id == MessageID.OPEN_CONNECTION_REQUEST_2:
                request = parse_open_connection_request_2(raw)
                remote = SystemAddress(*sender)
                self.connections[sender] = Connection(
                    remote=remote,
                    local=self.local_address(),
                    server_guid=self.guid,
                    mtu=request.mtu,
                )
                # The address echoed back is the client as *we* see it, which
                # behind NAT is not the address the client knows itself by.
                self._send_now(
                    build_open_connection_reply_2(self.guid, remote, request.mtu),
                    sender,
                )
                log.info("%s: handshake 2/2 with %s", self.name, sender)

            else:
                log.debug("%s: ignoring unconnected %#02x", self.name, message_id)
        except HandshakeError as error:
            log.warning("%s: handshake from %s refused: %s", self.name, sender, error)

    def _handle_connected(self, raw: bytes, sender: tuple[str, int]) -> None:
        connection = self.connections.get(sender)
        if connection is None:
            log.debug("%s: datagram from unknown peer %s", self.name, sender)
            return

        resend = connection.take_retransmissions(raw)
        if resend:
            for datagram in resend:
                self._send(datagram, sender)
            log.info(
                "%s: %s asked for %d datagram(s) again", self.name, sender, len(resend)
            )
        if connection.lost:
            log.warning(
                "%s: %d datagram(s) asked for were no longer retained",
                self.name,
                len(connection.lost),
            )
            connection.lost.clear()

        for message in connection.receive(raw):
            self._on_message(connection, message, sender)

        ack = connection.flush_acks()
        if ack is not None:
            self._send_ack(ack, sender)

    def _on_message(self, connection: Connection, message, sender) -> None:
        message_id = message.message_id
        game = parse_message(message.payload)
        log.debug("%s: message %#02x (%d bytes)", self.name, message_id, len(message.payload))

        if message_id == MessageID.CONNECTION_REQUEST:
            accepted = connection.handle_connection_request(message.payload)
            for datagram in connection.send_message(accepted):
                self._send(datagram, sender)
            log.info("%s: accepted %s", self.name, sender)

        elif message_id == MessageID.NEW_INCOMING_CONNECTION:
            # The client is now fully connected; a real service announces itself
            # here, which is the last thing this server knows how to do.
            for datagram in connection.send_message(build_service_identity(self.name)):
                self._send(datagram, sender)
            log.info("%s: announced service identity to %s", self.name, sender)

        elif message_id == MessageID.CONNECTED_PING:
            # UNRELIABLE, which is what the real service was measured using — four
            # pongs, none of them reliable. Sending these RELIABLE_ORDERED, as this
            # did, spends an ordering index on every one: on the character service
            # the roster arrived at ordering index 8 instead of 4, and twenty-nine
            # keepalives sat in the ordered stream ahead of the messages that
            # matter, each one retransmitted for ever by the resend timer.
            client_time = int.from_bytes(message.payload[1:9], "big")
            for datagram in connection.send_message(
                connection.build_connected_pong(client_time),
                reliability=Reliability.UNRELIABLE,
            ):
                self._send(datagram, sender)

        elif message_id == 0x8A:  # noqa: PLR2004 - documented in dsor.messages
            # The client has announced itself and is waiting. Observed reply from
            # the real login service, in this order: a bare 0x88, then the handoff.
            # Without them the client pings for 30 s and reconnects for ever, which
            # is exactly what it did before this was implemented.
            identity = game.strings[0] if game.strings else "?"
            log.info("%s: %s authenticating", self.name, identity)
            self._authenticate(connection, sender)

        elif message_id == MessageID.DISCONNECTION_NOTIFICATION:
            connection.state = State.CLOSED
            self.connections.pop(sender, None)
            self.in_world.discard(sender)
            self.positions.pop(sender, None)
            self.record_sent.discard(sender)
            log.info("%s: %s disconnected", self.name, sender)

        else:
            self._on_game_message(connection, message, sender)

    def _authenticate(self, connection: Connection, sender) -> None:
        """Answer a 0x8A the way this tier's real counterpart does.

        Login dispatches, the character service serves the selection screen, and a
        map server serves a world map. All three send a bare 0x88; what precedes or
        follows it is what differs.
        """
        if self.role == "login":
            # The login server is where a client comes back before every zone
            # change, so the destination depends on how far it has got.
            chosen = self.sessions.character_chosen(connection.client_guid)
            target = self.map_target if chosen else self.character_target
            if not target:
                log.warning("%s: no target for this client", self.name)
                return
            # Both the order and the trailer depend on where the client is going.
            # The real login server does this differently on its two connections,
            # and the ordering indices are what say so:
            #
            #   to the character service   handoff 2, signal 3, trailer 0x80
            #   to a map server            signal 2, handoff 3, trailer 0x00
            #
            # Neither order is "the" order. This used the map order for both, then
            # the character order for both; both were half wrong. Measuring it
            # needed the two connections kept apart, since ordering indices restart
            # with each one — merging them pairs one connection's handoff with the
            # other's signal.
            handoff = build_server_handoff(
                target,
                HANDOFF_TRAILER_MAP if chosen else HANDOFF_TRAILER_CHARACTER,
            )
            signal = build_bare_signal(0x88)
            for payload in ((signal, handoff) if chosen else (handoff, signal)):
                for datagram in connection.send_message(payload):
                    self._send(datagram, sender)
            log.info(
                "%s: dispatching %s to %s (%s)",
                self.name,
                sender,
                target,
                "map" if chosen else "character selection",
            )
            return

        # Character and map services both answer with a map assignment.
        if not self.map_name:
            log.warning("%s: no map to assign", self.name)
            return
        for datagram in connection.send_message(build_map_assignment(self.map_name)):
            self._send(datagram, sender)
        for datagram in connection.send_message(build_bare_signal(0x88)):
            self._send(datagram, sender)
        log.info("%s: assigned map %r", self.name, self.map_name)

    def _tick_pair(self, connection: Connection, sender) -> None:
        """One tick: the 0x85/0x004F state, then the generated position.

        The order matters — the real server always sends 0x004F first — and the
        pair is what the client expects continuously, not once.
        """
        position = self.positions.get(sender)
        if position is None:
            return
        for datagram in connection.send_message(tick_state()):
            self._send(datagram, sender)
        message = encode_entity_update(position, entity_update_template())
        for datagram in connection.send_message(message):
            self._send(datagram, sender)

    def game_tick(self) -> None:
        """Send a tick pair to everyone in the world."""
        for sender in list(self.in_world):
            connection = self.connections.get(sender)
            if connection is None:
                self.in_world.discard(sender)
                self.positions.pop(sender, None)
                continue
            self._tick_pair(connection, sender)

    def _send_character_list(
        self, connection: Connection, sender, repeat: bool = False
    ) -> None:
        """Push the character list, unprompted, and start the event schedule.

        Operation 1 of the selection command. The client does not ask for this on a
        fresh login — it waits for the push — and it refuses a second one, so a
        repeated ready signal must not produce another.

        Ordering indices 4, 5 and 6: the list, its acknowledgement, and the 717 KB
        event schedule. The first two go out at once; the schedule goes out slowly,
        because the client crashes if it arrives before the selection screen has
        finished building. See _send_slow.
        """
        if sender in self.record_sent and not repeat:
            log.info("%s: %s asked for the roster again", self.name, sender)
            return
        try:
            record = character_selection()
        except FileNotFoundError as error:
            log.error("%s: %s", self.name, error)
            return
        # The clock first, as the real service does: it announced its own between
        # the client's ready signal and the list.
        self._announce_game_clock(connection, sender)
        total = 0
        for index, piece in enumerate(record):
            datagrams = connection.send_message(piece)
            queue = self._send if index < 2 else self._send_slow
            for datagram in datagrams:
                queue(datagram, sender)
            total += len(datagrams)
        self.record_sent.add(sender)
        log.info(
            "%s: pushed the character list to %s (%d datagrams)",
            self.name,
            sender,
            total,
        )

    def _release_character(self, connection: Connection, game, sender) -> None:
        """Grant the client's request to start the game.

        Answers 0x8B/0x0087 operation 3 with operation 5, which is the only thing
        that moves the client out of its selection screen: it has set its internal
        state to "start-game sent" and waits there indefinitely otherwise. The grant
        must carry a non-zero character id, or the client refuses it and stays.

        Two messages, ordering indices 7 and 8 on the real service: the grant, then
        an **empty** handoff. The client disconnects and returns to the login server
        for a destination.

        The 112-byte 0x84/0x0086 the real service also sent is not here. It answers
        a character-creation command, which the reference session's player happened
        to send; a client selecting an existing character never does.
        """
        total = 0
        for piece in character_release():
            datagrams = connection.send_message(piece)
            for datagram in datagrams:
                self._send(datagram, sender)
            total += len(datagrams)
        self.sessions.mark_chosen(connection.client_guid)
        log.info(
            "%s: released %s as %r (%d datagrams)",
            self.name,
            sender,
            CHARACTER_CHOSEN_NAME,
            total,
        )

    def _on_game_message(self, connection: Connection, message, sender) -> None:
        """Log a Drakensang message by name rather than as raw bytes.

        Nothing here answers yet — the game protocol is not implemented — but
        every message is identified, which is the difference between a log that
        can be worked from and a hex dump.  See dsor/messages.py for the three
        message shapes and what is known about each.
        """
        try:
            game = parse_message(message.payload)
        except ValueError as error:
            log.warning("%s: undecodable message from %s: %s", self.name, sender, error)
            return

        if game.message_id == TIME_SYNC:
            # The client sends one of these every two seconds. How often to answer
            # is not a matter of taste: it was counted per tier in the reference
            # session, and the three tiers differ.
            #
            #   login      1 request,   0 answers — never
            #   character  24 requests, 1 answer  — the first, then silence
            #   map        20 requests, 17 answers — nearly all of them
            #
            # That shape makes sense: the selection screen has no world whose clock
            # needs following, and the map does. Answering every one here kept the
            # client re-synchronising instead of finishing — its own display
            # counted past 2784 without ever settling.
            #
            # parse_message already stripped the ID_TIMESTAMP wrapper and the 0x83
            # byte, so the body starts with the client's game clock.
            if len(game.body) < 4:
                log.warning("%s: time sync with no clock in it", self.name)
                return
            clock = int.from_bytes(game.body[:4], "little")
            answered = self.time_syncs_answered.get(sender, 0)
            if not self._answers_time_sync(answered):
                log.debug("%s: time sync %d left unanswered", self.name, answered + 1)
                self.time_syncs_answered[sender] = answered + 1
                return
            # Our own clock, not *clock* echoed back: the client adopts what it is
            # told, so echoing tells it nothing. See _announce_game_clock.
            reply = build_timestamped(
                connection.elapsed_ms(), build_time_sync_reply(connection.elapsed_ms())
            )
            # UNRELIABLE_SEQUENCED, as measured. A clock reading must never be
            # retransmitted: what arrives is then a stale timestamp presented as
            # current, and sequencing exists precisely so a late one is dropped
            # rather than delivered.
            for datagram in connection.send_message(
                reply, reliability=Reliability.UNRELIABLE_SEQUENCED
            ):
                self._send(datagram, sender)
            self.time_syncs_answered[sender] = answered + 1
            log.debug("%s: time sync, clock %d", self.name, clock)
            return

        if self.role == "map" and game.message_id == 0x8D:
            # The entry sequence, in the order the real map server uses: the
            # acknowledgement, the cosmetics table, then alternating tick pairs.
            #
            # Everything else it eventually sends — the 631 KB transfer, the
            # multi-entity snapshot — arrives much later, and sending it here
            # crashed the client on an assertion inside its own UI manager.
            spawn = SPAWN_POSITIONS.get(self.map_name or "")
            if spawn is None:
                log.error(
                    "%s: no arrival position known for %r", self.name, self.map_name
                )
                return
            if sender in self.in_world:
                log.info("%s: %s repeated its ready signal", self.name, sender)
                return
            # Ordering indices on the real map server: ack 4, the cosmetics table
            # 5-6 (it splits), then 0x00A7 at 7 and 0x0074 at 8, and only then the
            # tick pairs. These last two were dropped in an earlier version because
            # the frame order suggested they came much later; the ordering index
            # says otherwise, and it is the authority.
            for piece in map_entry_sequence():
                for datagram in connection.send_message(piece):
                    self._send(datagram, sender)
            self.in_world.add(sender)
            self.positions[sender] = spawn
            self._tick_pair(connection, sender)
            log.info("%s: %s entered the world at %s", self.name, sender, spawn)
            return

        if (
            self.role == "map"
            and game.message_id == 0x8B
            and game.opcode == 0x005F
            and len(game.body) == 15
        ):
            # The client reports where it walked to; remember it so the next tick
            # reports the same place back.
            self.positions[sender] = decode_client_movement(game.body).position
            return

        if self.role == "character" and game.message_id == 0x8D:
            # The selection screen asks to be filled. Real order: the 0x84/0x001B
            # acknowledgement, then the character record. Without the record the
            # screen has nobody to offer and the client cannot proceed.
            self._send_character_list(connection, sender)
            return

        if (
            self.role == "character"
            and game.message_id == 0x8B
            and game.opcode == SELECTION_COMMAND
        ):
            operation = game.body[0] if game.body else None
            if operation == OPERATION_START_GAME:
                self._release_character(connection, game, sender)
            elif operation == OPERATION_REQUEST_LIST:
                # Not expected on a fresh login, but answerable: push the list.
                self._send_character_list(connection, sender, repeat=True)
            else:
                log.info(
                    "%s: selection command operation %s from %s, ignored",
                    self.name,
                    operation,
                    sender,
                )
            return

        if (
            self.role == "character"
            and game.message_id == 0x8B
            and game.opcode == GENERATION_COMMAND
        ):
            log.warning(
                "%s: %s asked to create a character; not implemented",
                self.name,
                sender,
            )
            return

        if (
            self.role == "character"
            and game.message_id == 0x8B
            and game.opcode == CLIENT_QUERY_OPCODE
        ):
            seen = self.queries_seen.get(sender, 0)
            for piece in client_query_reply(seen):
                for datagram in connection.send_message(piece):
                    self._send(datagram, sender)
            self.queries_seen[sender] = seen + 1
            log.info("%s: answered query %d from %s", self.name, seen + 1, sender)
            return

        if game.is_bulk:
            # 84% of a session is these two opcodes and their bodies are still
            # opaque; logging each one at INFO would drown everything else.
            log.debug("%s: %s (%d bytes)", self.name, game.name, len(game.body))
            return

        detail = ""
        if game.strings:
            detail = " " + " ".join(repr(s) for s in game.strings[:2])
        elif (game.message_id, game.opcode) in KNOWN_OPCODES:
            try:
                detail = f" -> {read_server_handoff(game)!r}"
            except ValueError:
                detail = ""
        log.info(
            "%s: %s from %s (%d bytes)%s",
            self.name,
            game.name,
            sender,
            len(game.body),
            detail,
        )


def serve(
    login_port: int = 2190,
    character_port: int = 2192,
    map_port: int = 30000,
    advertise: str = "127.0.0.1",
    map_name: str = "a0001_start_tutorial_dun",
    capture_path: str | None = None,
    schedule_rate: float = SCHEDULE_FRAGMENTS_PER_SECOND,
) -> None:
    """Run the three tiers the real service is built from.

    The topology is taken from a capture rather than guessed, and it is not the
    obvious one: the **login server is a persistent dispatcher**. A client returns
    to it before every zone change — eleven separate connections in a fifteen
    minute session — and it issues every handoff, first to the character service
    and afterwards to a map server. The character service serves only the
    selection screen and then releases the client with an *empty* handoff.

    *advertise* is the address written into those handoffs. It has to be reachable
    by the **client**, which behind a VM is not an address this process can see on
    itself, so it cannot be detected and must be given.
    """
    sessions = Sessions()
    capture = Capture(capture_path) if capture_path else None
    if capture is not None:
        log.info("recording every datagram to %s", capture_path)
    selector = selectors.DefaultSelector()
    services = {}

    tiers = [
        (login_port, "DrasaOnlineLoginServer", "login"),
        (character_port, "DrasaCharacterService", "character"),
        (map_port, "DrasaOnlineMapServer", "map"),
    ]
    for port, name, role in tiers:
        service = Service(
            port,
            name,
            role=role,
            sessions=sessions,
            capture=capture,
            character_target=f"{advertise}:{character_port}" if role == "login" else None,
            map_target=f"{advertise}:{map_port}" if role == "login" else None,
            # The selection screen is its own map, and the only one the real
            # character service ever serves.
            map_name=(
                "a0000_char" if role == "character"
                else map_name if role == "map"
                else None
            ),
        )
        service.slow_rate = schedule_rate
        services[service.socket] = service
        selector.register(service.socket, selectors.EVENT_READ)
        log.info("listening on udp/%d as %s (%s)", port, name, role)
    log.info("handoffs will advertise %s", advertise)

    #: Two hundred and fifty datagrams a second, which is the rate the real
    #: service was measured at: 32,176 fragment datagrams over 101.6 seconds,
    #: never far from 250-300 in any five-second window.
    #:
    #: A thousand a second was tried first, on the reasoning that faster is better
    #: for a 717 KB transfer. It is not — the client dropped 133 datagrams out of
    #: 594 immediately. The peer's receive buffer is the constraint, and the real
    #: service evidently knew it.
    burst, tick = 1, 0.004
    #: Game ticks per second. The real server alternates a state message and a
    #: position at roughly this cadence; the exact rate is not established.
    tick_hz = 10.0
    next_tick = time.monotonic() + 1.0 / tick_hz

    try:
        while True:
            busy = any(
                service.outbound or service.slow for service in services.values()
            )
            for key, _ in selector.select(timeout=tick if busy else 1.0):
                service = services[key.fileobj]
                raw, sender = service.socket.recvfrom(2048)
                # No blanket try/except here on purpose: during protocol
                # bring-up a silent exception is indistinguishable from a client
                # that stopped answering, and that costs days.
                service.handle(raw, sender)
            now = time.monotonic()
            if now >= next_tick:
                next_tick = now + 1.0 / tick_hz
                for service in services.values():
                    if service.role == "map":
                        service.game_tick()
            for service in services.values():
                # Top up before flushing, so a quiet queue spends its budget on
                # whatever is still unacknowledged rather than idling.
                if len(service.outbound) < burst:
                    service.resend_due(burst)
                service.flush(burst)
                service.flush_slow()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        for service in services.values():
            service.socket.close()
        if capture is not None:
            capture.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--advertise",
        default="127.0.0.1",
        help="address written into handoffs. Must be reachable from the client: "
        "behind a VM that is the host address as the guest sees it, e.g. 172.20.0.1",
    )
    parser.add_argument("--login-port", type=int, default=2190)
    parser.add_argument("--character-port", type=int, default=2192)
    parser.add_argument("--map-port", type=int, default=30000)
    parser.add_argument(
        "--map",
        default="a0001_start_tutorial_dun",
        help="world map the map server assigns. Must be one with a known arrival "
        "position (see gameplay.SPAWN_POSITIONS). Its 32-bit trailer is looked up "
        "from the value it was observed with, so name and trailer stay paired",
    )
    parser.add_argument(
        "--capture",
        metavar="PATH",
        help=(
            "record every datagram to PATH, in the same JSONL format as a "
            "reference capture, so the two can be diffed at message level"
        ),
    )
    parser.add_argument(
        "--schedule-rate",
        type=float,
        default=SCHEDULE_FRAGMENTS_PER_SECOND,
        metavar="N",
        help=(
            "datagrams a second for the 717 KB event schedule (default: "
            f"{SCHEDULE_FRAGMENTS_PER_SECOND:g}, the measured rate). Raise it to "
            "test whether the client still crashes when it arrives early."
        ),
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    serve(
        login_port=args.login_port,
        character_port=args.character_port,
        map_port=args.map_port,
        advertise=args.advertise,
        map_name=args.map,
        capture_path=args.capture,
        schedule_rate=args.schedule_rate,
    )


if __name__ == "__main__":
    main()
