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
import math
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
from dsor.combat import (
    Kill,
    encode_actors_enter_vicinity,
    encode_discard_monster,
    encode_kill,
)
from dsor.protocol import SERVICE_PORTS, build_service_identity
from dsor.shop import OPCODE as SHOP_OPCODE, keep_first, set_price
from dsor.gameplay import (
    Position,
    SPAWN_POSITIONS,
    decode_client_movement,
    START_TICK_OFFSET,
    WORLD_SCALE,
    actor_id,
    decode_position,
    WORLD_SCALE as WORLD,
    encode_actor_vitals,
    monster_spawn,
    with_spawn,
    encode_entity_group_message,
    encode_position,
    with_motion,
    encode_entity_update,
    reposition_entity,
    ring_positions,
)
from dsor.recorded import (
    CHARACTER_CHOSEN_NAME,
    HANDLE_SIZE,
    entity_descriptions,
    commands_for,
    first_command,
    departure_commands,
    hit_commands,
    incoming_hit,
    vicinity_announcements,
    combat_ready_mobs,
    describable_mobs,
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

#: Seconds to hold the event schedule back before starting it.
#:
#: The client crashes if the schedule reaches it while the character screen is
#: still being built, so it has to arrive later than that. What it must *not* do is
#: arrive slowly: everything behind it in the ordered stream — the grant, the
#: handoff, the shop — cannot be delivered until it completes, so throttling the
#: whole transfer to the real service's measured six fragments a second left the
#: client on "loading data" for the ninety-eight seconds that took.
#:
#: The real service did exactly that, and its player paid for it: they clicked at
#: fifty-two seconds and were let into the world at a hundred and one, because the
#: grant was queued behind the same transfer. That was an accident of a lossy link,
#: not a design, and it is the one property here worth *not* reproducing.
#:
#: So: delay the start, then send at the normal rate.
SCHEDULE_DELAY_SECONDS = 8.0

#: Datagrams a second once it does start. The same rate as everything else.
SCHEDULE_FRAGMENTS_PER_SECOND = 250.0

#: The player's own actor id, as every recorded stats update carries it.
PLAYER_ACTOR = bytes([0x15, 0x00, 0x01, 0x00])

#: How close the client insists on being before it will send a skill at all, read
#: from its own refusal: "target for 'angrystrike' out of range!! 4.17 <-> 1.75".
#:
#: Not used to filter anything, and the attempt is worth recording. Rejecting attacks
#: whose nearest creature was further than this stopped damage entirely — eleven in
#: one session — because the distance here is measured against the coordinates in a
#: creature's description while the client measures against what it draws, and the two
#: disagree by enough to matter: 2.3 against 1.75 in one observed case. The client is
#: the authority on its own range, and it has already applied it before sending.
SKILL_RANGE = 1.75

#: Commands::TargetSkillCommand — the client using a skill. It names no target, so
#: the server decides what was hit.
TARGET_SKILL_OPCODE = 0x0047

#: The client asking what an entity is, once per entity it does not recognise. Its
#: body is the four-byte handle, and the answer is a 0x85/0x002A description.
DESCRIBE_ENTITY_OPCODE = 0x001C

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
        #: How many purchase offers to serve, or None for all of them.
        self.shop_offers: int | None = None
        #: Price to put on every offer, or None to leave the recorded ones.
        self.shop_price: float | None = None
        #: How many creatures to place around the player, from the recorded set.
        self.mobs = 0
        #: Distance to place them at, or 0 to leave them where they were recorded.
        self.mob_radius = 0
        #: Radius of a slow patrol around their own position, or 0 to stand still as
        #: the recorded ones did.
        self.mob_patrol = 0
        #: Duration stamped on a moving entity, for the client to interpolate over.
        self.tick_duration = 18
        #: Health each served creature has left, keyed by its actor id.
        self.mob_health: dict[bytes, float] = {}
        #: Which creature each client is currently fighting, so consecutive blows
        #: land on the same one.
        self.mob_target: dict[tuple[str, int], bytes] = {}
        #: The player's health, and when a creature last struck them.
        self.player_health: dict[tuple[str, int], float] = {}
        self._last_strike: dict[tuple[str, int], float] = {}
        #: Where the player starts, and the ceiling reported alongside it. The
        #: killing session's readings ran 0.0 to 31.6 against a maximum of 234 to 236.
        self.player_max = 236
        #: The resource a skill spends, reported alongside health and left alone.
        self.player_resource = 10.0
        #: What one creature blow takes off, and how often one lands. Both ours.
        self.creature_damage = 3.0
        self.strike_interval = 1.5
        #: How far a blow reaches, in world units, in either direction.
        #:
        #: Four, from measurement: replaying a real session's twelve attacks against
        #: wire positions gives distances of 0.9 to 3.5. Looser than the client's own
        #: 1.75 on purpose, because this measures from a creature's movement record
        #: while the client measures from what it draws; six was too loose and made
        #: the player take damage from creatures across the room.
        self.reach = 4.0
        #: What a creature starts with, and what one hit takes off it.
        self.mob_max_health = 60.0
        self.mob_damage = 12.0
        #: The maximum reported alongside the current value. The capture's players
        #: carried 234 to 236; a creature's is not observed at all.
        self.mob_max_health_ceiling = 60
        #: World units from the player to place a creature at, or 0 — the default —
        #: for the position its own description carries.
        #:
        #: Rewriting it is off by default because it broke what worked: the creatures
        #: stopped appearing at all. The description is bit-packed, with strings and
        #: single-bit fields, so twelve bytes written at a fixed byte offset shift
        #: everything behind them and the client's decoder gives up — silently, with
        #: nothing in its log. Reading that offset yields plausible coordinates in all
        #: six recorded descriptions, so the field is there; writing it needs the
        #: bit-level layout, not a byte offset.
        self.mob_near = 0.0
        #: Send only the creature's own command instead of the whole recorded batch.
        self.mob_first_command = False
        #: Send the recorded vicinity announcement before a creature is asked about.
        self.announce_vicinity = True
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
        #: client must NOT receive quickly. See _send_slow_sealed.
        self.slow: collections.deque[tuple[bytes, tuple[str, int]]] = (
            collections.deque()
        )
        #: Datagrams a second out of that queue. The real transfer delivered 596
        #: fragments in 101.6 seconds — six a second — because 54 out of every 55
        #: of its datagrams were retransmissions of fragments already sent.
        self.slow_rate = SCHEDULE_FRAGMENTS_PER_SECOND
        #: How long to hold the queue before releasing anything from it.
        self.slow_delay = SCHEDULE_DELAY_SECONDS
        self._slow_credit = 0.0
        self._slow_last = time.monotonic()
        #: When the queue last became non-empty, so the delay is measured from
        #: there rather than from the start of the process.
        self._slow_since: float | None = None
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

    def _queue(
        self,
        connection: Connection,
        payload: bytes,
        sender: tuple[str, int],
        reliability: Reliability = Reliability.RELIABLE_ORDERED,
        channel: int = 0,
        slow: bool = False,
    ) -> int:
        """Queue a message as frames, to be sealed when they actually go out.

        Sealing late is what makes pacing safe. A datagram's sequence number is a
        transmit-order counter, so numbering at build time and sending later leaves
        the peer staring at a gap it reads as loss.
        """
        frames = connection.frames_for(payload, reliability, channel)
        queue = self.slow if slow else self.outbound
        if slow and self._slow_since is None:
            self._slow_since = time.monotonic()
        for frame in frames:
            queue.append((connection, frame, sender))
        return len(frames)

    def _send(self, payload: bytes, sender: tuple[str, int]) -> None:
        """Queue an already-sealed datagram, for retransmissions."""
        self.outbound.append((None, payload, sender))

    def _send_slow_sealed(self, payload: bytes, sender: tuple[str, int]) -> None:
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
        self.slow.append((None, payload, sender))

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
        self._queue(connection, reply, sender, reliability=Reliability.UNRELIABLE_SEQUENCED)
        log.debug("%s: announced game clock %d", self.name, connection.elapsed_ms())

    def _trim_offers(self, payload: bytes) -> bytes:
        """Cut the purchase-offer list down to --shop-offers entries.

        Nothing in the protocol needs this. It exists because re-emitting a message
        with a different number of entries is the cheapest proof that a format is
        actually understood rather than merely replayed: the client either shows the
        shorter list or it does not.
        """
        if (self.shop_offers is None and self.shop_price is None) or len(payload) < 3:
            return payload
        if int.from_bytes(payload[1:3], "little") != SHOP_OPCODE:
            return payload
        try:
            trimmed = payload
            if self.shop_offers is not None:
                trimmed = keep_first(trimmed, self.shop_offers)
            if self.shop_price is not None:
                trimmed = set_price(trimmed, self.shop_price)
        except ValueError as error:
            log.warning("%s: could not trim the offer list: %s", self.name, error)
            return payload
        log.info(
            "%s: offer list rewritten (%d bytes instead of %d, %s offers, price %s)",
            self.name,
            len(trimmed),
            len(payload),
            self.shop_offers if self.shop_offers is not None else "all",
            self.shop_price if self.shop_price is not None else "unchanged",
        )
        return trimmed

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
            self._slow_since = None
            return 0
        now = time.monotonic()
        if self._slow_since is not None and now - self._slow_since < self.slow_delay:
            # Still inside the hold: let the client finish building its screen.
            self._slow_last = now
            return 0
        if self.slow_rate == float("inf"):
            self._slow_credit = len(self.slow)
        else:
            self._slow_credit += (now - self._slow_last) * self.slow_rate
        self._slow_last = now

        sent = 0
        while self.slow and self._slow_credit >= 1.0:
            sent += self._transmit(self.slow.popleft())
            self._slow_credit -= 1.0
        return sent

    def flush(self, burst: int) -> int:
        """Send at most *burst* queued datagrams. Returns how many went out."""
        sent = 0
        while self.outbound and sent < burst:
            sent += self._transmit(self.outbound.popleft())
        return sent

    def _transmit(self, entry) -> int:
        """Send one queued entry, sealing it if it is still a frame."""
        connection, item, sender = entry
        payload = connection.seal(item) if connection is not None else item
        if self.capture is not None:
            self.capture.record(payload, self.port, sender, from_server=True)
        self.socket.sendto(payload, sender)
        return 1

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
            self._queue(connection, accepted, sender)
            log.info("%s: accepted %s", self.name, sender)

        elif message_id == MessageID.NEW_INCOMING_CONNECTION:
            # The client is now fully connected; a real service announces itself
            # here, which is the last thing this server knows how to do.
            self._queue(connection, build_service_identity(self.name), sender)
            log.info("%s: announced service identity to %s", self.name, sender)

        elif message_id == MessageID.CONNECTED_PING:
            # UNRELIABLE, which is what the real service was measured using — four
            # pongs, none of them reliable. Sending these RELIABLE_ORDERED, as this
            # did, spends an ordering index on every one: on the character service
            # the roster arrived at ordering index 8 instead of 4, and twenty-nine
            # keepalives sat in the ordered stream ahead of the messages that
            # matter, each one retransmitted for ever by the resend timer.
            client_time = int.from_bytes(message.payload[1:9], "big")
            self._queue(connection, connection.build_connected_pong(client_time), sender, reliability=Reliability.UNRELIABLE, )

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
                self._queue(connection, payload, sender)
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
        self._queue(connection, build_map_assignment(self.map_name), sender)
        self._queue(connection, build_bare_signal(0x88), sender)
        log.info("%s: assigned map %r", self.name, self.map_name)

    def _tick_pair(self, connection: Connection, sender) -> None:
        """One tick: the 0x85/0x004F state, then the generated position.

        The order matters — the real server always sends 0x004F first — and the
        pair is what the client expects continuously, not once.
        """
        position = self.positions.get(sender)
        if position is None:
            return
        self._queue(connection, tick_state(), sender)
        # The real server advances a tick counter on every update and re-announces
        # each entity; it observed ten to twelve per update. Milliseconds over ten
        # reproduces that rate without inventing a clock of our own.
        tick = connection.elapsed_ms() // 10
        self._queue(connection, self._entity_update(position, tick), sender)

    def _creature_wire_position(self, actor: bytes):
        """Where a creature stands, in the same frame as the player's own position.

        This is the distinction that made every distance bound fail, in both
        directions. A creature has two positions: the three floats inside its
        description, and the three signed 16-bit fields in its movement records —
        and **they are not the same frame**. The player's trajectory decoded from the
        wire runs z=46 down to z=21; the descriptions put the creatures at z=52 to 55,
        an interval it never enters. Measured against the descriptions, the player was
        never closer than 18 units and 30 at the moment of every attack, so a bound of
        1.75 refused everything and a bound of 6 refused everything. Measured on the
        wire, the same player came within 3 units of a creature.

        So: compare wire against wire.
        """
        for record in combat_ready_mobs():
            if actor_id(record) == actor:
                return decode_position(record)
        return None

    def _resolve_attack(self, connection: Connection, sender) -> None:
        """Work out what the player just hit, and take health off it.

        The client's TargetSkillCommand names no target: twenty-one bytes holding a
        skill id, a float, a tick and two fields whose meaning is not established, and
        not one actor id among them. Deciding who was hit is therefore the server's
        job, which is why hitting a creature forever did nothing — nobody was
        deciding.

        What this does is the simplest defensible rule — nearest creature to the
        player — and it is ours rather than measured. The capture never reports a
        creature's stats at all, so both the health scale and the damage are invented;
        only the message they travel in is real.
        """
        position = self.positions.get(sender)
        if position is None or not self.mob_health:
            return

        alive = [
            actor for actor, left in self.mob_health.items() if left > 0.0
        ]
        if not alive:
            return

        here = (position.x / WORLD, position.y / WORLD)

        def distance(actor: bytes) -> float:
            where = self._creature_wire_position(actor)
            return float("inf") if where is None else position.distance_to(where)

        # Keep hitting whatever is already being hit. The client names no target —
        # measured: across a session with six kills, not one message from it carries a
        # creature's id except the question "what is this entity" — so the server
        # chooses, and choosing the nearest afresh on every blow makes the choice flip
        # between creatures standing close together. That is what looked like damage
        # being shared between them.
        latched = self.mob_target.get(sender)
        if latched is not None and latched in alive:
            target = latched
        else:
            # Generous rather than exact. Filtering at the client's own 1.75 units
            # stopped damage entirely — eleven attacks refused in one session —
            # because the distance here is measured against the coordinates in a
            # creature's description while the client measures against what it draws,
            # and the two differed by 2.3 against 1.75. But no bound at all meant a
            # blow could land on a creature thirty units away once the near one died.
            nearby = [a for a in alive if distance(a) <= self.reach * WORLD]
            if not nearby:
                log.info(
                    "%s: %s attacked with nothing within %.0f units",
                    self.name,
                    sender,
                    self.reach,
                )
                return
            target = min(nearby, key=distance)
            self.mob_target[sender] = target

        # One blow, one kill — because the blow being replayed is the one that
        # killed this creature in the capture, and the client reads its health out of
        # it. It shows zero after the first hit however much health this server
        # thinks is left, so counting further hits only means striking a corpse and
        # then, when the count finally runs out, moving on to the next creature. That
        # is what looked like hitting one creature killing the others.
        left = 0.0
        self.mob_health[target] = left

        # The hit itself, replayed. A creature has no health message — every one of
        # the 107 stats updates in both captured sessions targets the player, even the
        # session where six creatures died — so the client works its bar out from
        # this. Reporting health to a creature was an extrapolation, and it never had
        # any effect.
        hit = hit_commands().get(target)
        if hit is not None:
            if self.mob_first_command:
                # Everything in the batch addressed to this creature, and nothing
                # else. Sending the whole batch teleported it and dropped its loot
                # where the other session's player stood; sending only its first
                # command lost whatever made it disappear, so it died at zero health
                # and stayed on screen. The commands aimed at the creature are the
                # ones that belong to it.
                try:
                    hit = commands_for(hit, target)
                except ValueError as error:
                    log.info("%s: %s, sending it whole", self.name, error)
            self._queue(connection, hit, sender)
        log.info(
            "%s: %s hit entity %s, %.1f health left%s",
            self.name,
            sender,
            target.hex(" "),
            left,
            "" if hit is not None else " (no recorded hit for it)",
        )

        if left > 0.0:
            return
        # Dead. Two generated commands, in this order, and neither is what this
        # server used to send.
        #
        # ActorsLeftVicinityCommand — which is what was sent before — does not kill
        # anything: it sets the entity invisible. That is why creatures stayed
        # standing at zero health. What kills is KillCommand, which carries the
        # impulse the corpse is thrown with and a flag saying whether the body is
        # removed; the client's death sequence hangs off it. DiscardMonsterCommand
        # then deletes the entity and the actor outright, and its body is empty, so
        # it needs no recording — which is how a creature with no captured removal
        # can be retired at all.
        victim = int.from_bytes(target, "little")
        tick = connection.elapsed_ms() // 10
        self._queue(
            connection,
            encode_kill(
                Kill(
                    victim=victim,
                    killer=int.from_bytes(PLAYER_ACTOR, "little"),
                    impulse=(0.0, 1.0, 0.0),
                    tick=tick,
                    kill_tick=tick,
                    despawn=True,
                )
            ),
            sender,
        )
        # No DiscardMonsterCommand here. It deletes the entity outright, and sending
        # it in the same breath as the kill destroys the death sequence before it can
        # play — the creature simply vanished. The kill's own despawn flag already
        # tells the client to clear the body; discarding is for retiring a creature
        # that is not dying, and for one no capture contains a removal for.
        log.info("%s: entity %s killed", self.name, target.hex(" "))
        self.mob_target.pop(sender, None)

    def _announce_vicinity(self, connection: Connection, sender) -> None:
        """Tell the client which actors are near it.

        The leg of the exchange this server skipped. The client's handler for this
        announcement calls RequestActor directly, so an actor announced this way is
        set up by the path the real server used; one the client merely noticed in a
        position update is not.
        """
        if not self.mobs or not self.announce_vicinity:
            return
        served = [
            int.from_bytes(actor_id(record), "little")
            for record in combat_ready_mobs()[: self.mobs]
        ]
        if not served:
            return
        # Generated, not replayed: a count then that many 32-bit ids, byte-aligned
        # throughout. One message announces the whole set, where the recorded ones
        # announced a creature each and only existed for the six that happened to be
        # captured.
        self._queue(
            connection,
            encode_actors_enter_vicinity(served, int.from_bytes(PLAYER_ACTOR, "little")),
            sender,
        )
        log.info("%s: announced %d nearby actors to %s", self.name, len(served), sender)

    def _describe_entity(self, connection: Connection, game, sender) -> None:
        """Answer "what is entity N" with the entity's description.

        This is what makes a creature exist. A position update for an entity the
        client has never heard of is ignored, so the sequence is: the entity appears
        in a 0x005F, the client asks about it with 0x8B/0x001C, and only once it has
        an answer does it instantiate and draw the thing.

        Leaving the question unanswered is invisible in a log and fatal on screen:
        the map loads, the player walks around, and no creature ever appears. The
        client keeps asking for as long as it is running — 136 times against this
        server before this existed, against 8 in the whole reference session.

        The request body is the four-byte handle, and the recorded descriptions carry
        that same handle back, which is how each was paired with its creature.
        """
        handle = game.body[:HANDLE_SIZE]
        # Only creatures this server can see through to the end. Describing one it
        # cannot place, hit or remove produced exactly what it sounds like: a creature
        # standing at full health that no blow could ever reach.
        servable = {actor_id(record) for record in combat_ready_mobs()[: self.mobs]}
        description = (
            entity_descriptions().get(handle) if handle in servable else None
        )
        if description is None:
            log.info(
                "%s: %s asked about entity %s, which is not in the recorded set",
                self.name,
                sender,
                handle.hex(" "),
            )
            return
        # Where the creature stands is decided here and nowhere else. A position
        # update for it is discarded — the client only applies those to an actor it
        # has bound to an entity — so the description's own position is the only one
        # that takes effect. Hundreds of movement updates moved nothing for exactly
        # this reason.
        if self.mob_near:
            here = self.positions.get(sender)
            if here is not None:
                index = len(self.mob_health)
                angle = 2 * math.pi * index / max(1, self.mobs)
                description = with_spawn(
                    description,
                    here.x / WORLD + self.mob_near * math.cos(angle),
                    0.0,
                    here.y / WORLD + self.mob_near * math.sin(angle),
                )
        if self.mob_first_command:
            # Send only the command addressed to this creature. The recorded
            # description is a batch — the NewMonsterCommand and then several more,
            # the last about the player, whose actor id is what the batch's own
            # trailer carries. Replaying commands aimed at other actors is at best
            # noise.
            try:
                description = first_command(description, handle)
            except ValueError as error:
                log.warning("%s: %s", self.name, error)
        self._queue(connection, description, sender)
        # No health is reported for a creature, because there is no such message. Two
        # captured sessions carry 154 ActorStatsUpdateCommands between them and every
        # single one targets the player — including the session in which six creatures
        # were killed. This sent a baseline anyway for a while; it was noise the real
        # server never emits, and the client took no notice of it either way. The
        # counter below is kept only to decide when to send the removal.
        self.mob_health[handle] = self.mob_max_health
        log.info(
            "%s: described entity %s to %s (%d bytes)",
            self.name,
            handle.hex(" "),
            sender,
            len(description),
        )

    def _entity_update(self, position, tick: int = 0) -> bytes:
        """The player's position, and any creatures placed around it.

        Several entities travel in one 0x85/0x005F, chained by a two-byte separator
        rather than counted. The creature records are real ones lifted from the
        tutorial dungeon with only their position rewritten: the zone content
        message is what tells the client the map contains them, so an invented
        identifier would be an entity it cannot draw.
        """
        player = with_motion(
            encode_position(position) + entity_update_template()[6:], position, tick
        )
        if not self.mobs:
            return encode_entity_group_message([player])

        # Only the living. Re-announcing a creature the client has been told to
        # remove makes it flicker: removed, redeclared a tick later, removed again.
        templates = [
            record
            for record in combat_ready_mobs()[: self.mobs]
            if self.mob_health.get(actor_id(record), self.mob_max_health) > 0.0
        ]
        if not templates:
            return encode_entity_group_message([player])
        if self.mob_patrol:
            # Beyond what the capture shows: its creatures stood still, 620 of 621
            # updates at one position with duration zero. This walks them in a slow
            # circle around that position instead, with a duration for the client to
            # interpolate over, because a standing monster is hard to tell from a
            # broken one.
            placed = []
            for offset, template in enumerate(templates):
                home = decode_position(template)
                angle = (tick / 100.0) + offset
                placed.append(
                    with_motion(
                        template,
                        Position(
                            x=home.x + round(self.mob_patrol * math.cos(angle)),
                            elevation=home.elevation,
                            y=home.y + round(self.mob_patrol * math.sin(angle)),
                        ),
                        tick,
                        duration=self.tick_duration,
                    )
                )
            return encode_entity_group_message([player, *placed])

        if not self.mob_radius:
            # Where the capture put them. Their own positions are valid by
            # construction — they stand on ground the map actually has — whereas a
            # ring around the player is a guess, and a creature inside a wall is one
            # the client has every reason to refuse to draw.
            return encode_entity_group_message(
                [player, *(with_motion(t, decode_position(t), tick) for t in templates)]
            )

        placed = [
            reposition_entity(template, where)
            for template, where in zip(
                templates, ring_positions(position, len(templates), self.mob_radius)
            )
        ]
        return encode_entity_group_message([player, *placed])

    def game_tick(self) -> None:
        """Send a tick pair to everyone in the world, and let creatures strike back."""
        for sender in list(self.in_world):
            connection = self.connections.get(sender)
            if connection is None:
                self.in_world.discard(sender)
                self.positions.pop(sender, None)
                continue
            self._tick_pair(connection, sender)
            self._creatures_strike(connection, sender)

    def _creatures_strike(self, connection: Connection, sender) -> None:
        """Let a live creature hit the player, every so often.

        Two messages, both replayed from a session where creatures fought back: the
        blow, and the player's health afterwards. The blow is one of the sixteen
        163-byte hits that name no creature — the mark of a blow taken rather than
        landed.

        This direction works where the other cannot: the player's actor is bound to an
        entity, so its health updates take effect. A creature's never do, which is why
        no creature's health is reported at all.

        The rate and the damage are this server's, not measured. The real session's
        sixteen blows were spread over a fight this server has no model of.
        """
        if not self.creature_damage:
            return
        position = self.positions.get(sender)
        if position is None:
            return
        def near(actor: bytes) -> bool:
            where = self._creature_wire_position(actor)
            return where is not None and (
                position.distance_to(where) <= self.reach * WORLD
            )

        if not any(
            left > 0.0 and near(actor) for actor, left in self.mob_health.items()
        ):
            # Nothing alive within reach. Losing health with no creature beside you
            # was this condition being "any creature anywhere", which every described
            # creature satisfied for the whole session.
            return
        now = time.monotonic()
        if now - self._last_strike.get(sender, 0.0) < self.strike_interval:
            return
        self._last_strike[sender] = now

        try:
            blow = incoming_hit()
        except FileNotFoundError as error:
            log.error("%s: %s", self.name, error)
            self.creature_damage = 0.0
            return

        left = max(
            0.0, self.player_health.get(sender, float(self.player_max)) - self.creature_damage
        )
        self.player_health[sender] = left
        self._queue(connection, blow, sender)
        # Health in the first field, the skill resource in the second. Writing the
        # damage into the second drained the player's mana and left their health
        # untouched, which is exactly what a round of testing showed.
        self._queue(
            connection,
            encode_actor_vitals(int(left), self.player_resource, PLAYER_ACTOR),
            sender,
        )
        log.info("%s: a creature struck %s, %.1f health left", self.name, sender, left)

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
        finished building. See _send_slow_sealed.
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
        # On a repeat, the list and its acknowledgement only: the schedule behind
        # them is global and already on its way.
        if repeat:
            record = record[:2]
        total = 0
        for index, piece in enumerate(record):
            total += self._queue(connection, piece, sender, slow=index >= 2)
        self.record_sent.add(sender)
        log.info(
            "%s: pushed the character list%s to %s (%d datagrams)",
            self.name,
            " again" if repeat else "",
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
            self._queue(connection, reply, sender, reliability=Reliability.UNRELIABLE_SEQUENCED)
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
                self._queue(connection, piece, sender)
            self.in_world.add(sender)
            self.positions[sender] = spawn
            self._announce_vicinity(connection, sender)
            self._tick_pair(connection, sender)
            log.info("%s: %s entered the world at %s", self.name, sender, spawn)
            return

        if (
            self.role == "map"
            and game.message_id == 0x8B
            and game.opcode == TARGET_SKILL_OPCODE
        ):
            self._resolve_attack(connection, sender)
            return

        if (
            self.role == "map"
            and game.message_id == 0x8B
            and game.opcode == DESCRIBE_ENTITY_OPCODE
        ):
            self._describe_entity(connection, game, sender)
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
                # Answer it. A static reading of the client said a second list is
                # refused unless its state is at most 1, and this refused to send one
                # on the strength of that — which left the client waiting forever for
                # something it had explicitly asked for. If it is asking, its state
                # permits it; the inference was sound and the conclusion was not.
                #
                # The list and its acknowledgement go again, the event schedule does
                # not: it is global data, it is already in flight or delivered, and
                # 717 KB of it a second time only delays what the client wants.
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
                piece = self._trim_offers(piece)
                self._queue(connection, piece, sender)
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
    shop_offers: int | None = None,
    shop_price: float | None = None,
    schedule_delay: float = SCHEDULE_DELAY_SECONDS,
    mobs: int = 0,
    mob_radius: int = 0,
    mob_patrol: int = 0,
    mob_health: float = 60.0,
    mob_damage: float = 12.0,
    mob_near: float = 0.0,
    creature_damage: float = 3.0,
    mob_first_command: bool = False,
    announce_vicinity: bool = True,
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
        service.slow_delay = schedule_delay
        service.mobs = mobs
        service.mob_radius = mob_radius
        service.mob_patrol = mob_patrol
        service.mob_max_health = mob_health
        service.mob_damage = mob_damage
        service.mob_max_health_ceiling = int(mob_health)
        service.mob_near = mob_near
        service.creature_damage = creature_damage
        service.mob_first_command = mob_first_command
        service.announce_vicinity = announce_vicinity
        service.shop_offers = shop_offers
        service.shop_price = shop_price
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
            "datagrams a second for the 717 KB event schedule once it starts "
            f"(default: {SCHEDULE_FRAGMENTS_PER_SECOND:g})"
        ),
    )
    parser.add_argument(
        "--shop-offers",
        type=int,
        default=None,
        metavar="N",
        help=(
            "serve only the first N purchase offers instead of all 23. Proves the "
            "offer list is generated rather than replayed, since the client has to "
            "accept a message it has never seen"
        ),
    )
    parser.add_argument(
        "--schedule-delay",
        type=float,
        default=SCHEDULE_DELAY_SECONDS,
        metavar="S",
        help=(
            "seconds to hold the event schedule back so it arrives after the "
            f"character screen is built (default: {SCHEDULE_DELAY_SECONDS:g}). "
            "Zero reproduces the crash it was added to avoid"
        ),
    )
    parser.add_argument(
        "--mobs",
        type=int,
        default=0,
        metavar="N",
        help=(
            "place N recorded creatures in a ring around the player. They are real "
            "entity records from the tutorial dungeon with only their position "
            "rewritten, since the client can only draw what the zone content "
            "declared (at most 8)"
        ),
    )
    parser.add_argument(
        "--mob-health",
        type=float,
        default=60.0,
        metavar="H",
        help=(
            "health each creature starts with. Invented: the capture never reports a "
            "creature's stats, only the player's, whose depleting stat ran to 63.60"
        ),
    )
    parser.add_argument(
        "--mob-damage",
        type=float,
        default=12.0,
        metavar="D",
        help="health one hit takes off a creature (also invented)",
    )
    parser.add_argument(
        "--no-vicinity",
        dest="announce_vicinity",
        action="store_false",
        help=(
            "skip the recorded ActorsEnterVicinityCommand announcements. They are "
            "the leg of the exchange that makes the client request an actor the way "
            "the real server caused it to"
        ),
    )
    parser.add_argument(
        "--mob-first-command",
        action="store_true",
        help=(
            "answer an entity request with only the creature's own command instead of "
            "the whole recorded batch, which also holds commands about other actors"
        ),
    )
    parser.add_argument(
        "--creature-damage",
        type=float,
        default=3.0,
        metavar="D",
        help=(
            "health a creature's blow takes off the player, 0 to disable retaliation. "
            "The blow itself is replayed from a real session; the damage and the rate "
            "are this server's"
        ),
    )
    parser.add_argument(
        "--mob-near",
        type=float,
        default=0.0,
        metavar="D",
        help=(
            "place each creature D world units from the player. Default 0 keeps the "
            "position its own description carries. Rewriting it currently stops the "
            "creatures appearing at all — the field is bit-packed and a byte-aligned "
            "write corrupts what follows"
        ),
    )
    parser.add_argument(
        "--mob-patrol",
        type=int,
        default=0,
        metavar="N",
        help=(
            "walk the creatures in a circle of N wire units around their own "
            "position. Default 0 stands still, as the recorded ones did — this goes "
            f"beyond the capture. Divide by {WORLD_SCALE} for world units"
        ),
    )
    parser.add_argument(
        "--mob-radius",
        type=int,
        default=0,
        metavar="N",
        help=(
            "place the creatures N units from the player instead of where they were "
            "recorded. Default 0 keeps their own positions, which are the only ones "
            "known to be on ground the map has"
        ),
    )
    parser.add_argument(
        "--shop-price",
        type=float,
        default=None,
        metavar="P",
        help=(
            "put price P on every purchase offer. The price is a 32-bit float in "
            "each entry's leading block, and the recorded list prices its bundles "
            "1.99 to 49.99 there"
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
        shop_offers=args.shop_offers,
        shop_price=args.shop_price,
        schedule_delay=args.schedule_delay,
        mobs=args.mobs,
        mob_radius=args.mob_radius,
        mob_patrol=args.mob_patrol,
        mob_health=args.mob_health,
        mob_damage=args.mob_damage,
        mob_near=args.mob_near,
        creature_damage=args.creature_damage,
        mob_first_command=args.mob_first_command,
        announce_vicinity=args.announce_vicinity,
    )


if __name__ == "__main__":
    main()
