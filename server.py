"""The three tiers a Drakensang Online client needs to log in, choose a character
and fight on a map.

Point the client's ``-ip`` at this host and it will complete the whole chain: login,
character selection, the grant that releases it, back to the login server, a map
server, and into the world. Creatures appear around it, can be struck, lose health
and die.

Two shapes are worth noticing before reading further. One process listens on
**several ports**, each an independent service with its own GUID, and connection state
is keyed by ``(ip, port)`` rather than by client GUID — the client opens a connection
to every service from the same peer, reusing one GUID. And the **login server is a
persistent dispatcher**: a client returns to it before every zone change, and where it
sends that client depends on how far it has got.

    python server.py --advertise 192.168.1.10 -v --capture ~/session.jsonl
    python server.py --advertise 192.168.1.10 --mobs 6 --mob-first-command

``--advertise`` has to be an address the **client** can reach, so it cannot be
detected here. ``--capture`` records every datagram in a reference capture's format,
which is what makes a failing session comparable to a working one.
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
    level_bounds,
    level_for,
    Hit,
    TargetSkill,
    encode_target_skill,
    Kill,
    encode_hit,
    encode_player_level,
    encode_xp_changed,
    encode_actors_enter_vicinity,
    encode_discard_monster,
    encode_kill,
)
from dsor.world import Rules, World
from dsor.console import Console
from dsor.combat import decode_skill_use
from dsor.monsters import MONSTERS
from dsor.skills import skill as skill_at
from dsor.mapdata import SPAWN_POINTS, servable_points
from dsor.skillbook import BOOK_SKILLS, skill_index, up_to_level, with_granted
from dsor.protocol import build_service_identity
from dsor.shop import OPCODE as SHOP_OPCODE, keep_first, set_price
from dsor.gameplay import (
    Position,
    SPAWN_POSITIONS,
    START_TICK_OFFSET,
    WALK_SPEED,
    WALK_UNITS_PER_TICK,
    WORLD_SCALE,
    WORLD_SCALE as WORLD,
    actor_id,
    decode_client_movement,
    decode_position,
    encode_entity_group_message,
    encode_entity_update,
    encode_position,
    heading_to,
    monster_spawn,
    reposition_entity,
    ring_positions,
    with_motion,
    with_spawn,
)
from dsor.recorded import (
    CHARACTER_CHOSEN_NAME,
    HANDLE_SIZE,
    entity_descriptions,
    monster_library,
    library_spawn,
    with_actor,
    spawn_bit,
    with_library_spawn,
    with_library_spawn_at,
    with_template,
    first_command,
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

#: Milliseconds per game tick, measured rather than guessed.
#:
#: Both sides stamp their movement commands on one counter, and the game clock a time
#: sync announces is a different scale. Reading the two together in one session settles
#: the ratio: at the moment the client announced a clock of 6217 it stamped a movement
#: at 156, and 6217 / 40 = 155.4. Over the whole session the movement counter runs
#: 106..5200 while the clock runs 4262..10558.
#:
#: This server announced its clock in milliseconds and stamped movement at
#: milliseconds over *ten*, so its ticks ran four times fast against its own clock —
#: and a movement window outside the client's clock moves nothing at all, silently.
GAME_TICK_MS = 40

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

#: Every command the client uses to say "I swung". A targeted skill names its victim
#: and comes as 0x0047; one that does not — a swing, a spin — comes as 0x0046, and a
#: ranged one as a bullet command. Handling only 0x0047 meant angrystrike landed and
#: every skill unlocked by levelling did nothing at all: the client sent SkillCommand
#: seventeen times for mightyswing and mighty360 and this server ignored all of it.
SKILL_OPCODES = {
    0x0046: "SkillCommand",
    0x0047: "TargetSkillCommand",
    0x0048: "BulletSkillCommand",
    # Measured: stuncharge arrives as this one, and leaving it out was the whole of
    # "certains font pas de degats". Eight of them in the real captures too.
    0x0049: "TargetPointBulletSkillCommand",
    0x004A: "ShiftedSkillCommand",
    0x004B: "TargetBulletSkillCommand",
}

#: The two remaining skill commands, deliberately not treated as a blow.
#: SustainedSkillCommand 0x004C is a channelled skill and needs a start-and-stop
#: model rather than one hit per command; SkillStopCommand 0x004D ends one. The
#: warrior has no sustained skill, and neither appears in any capture here.
SKILL_OPCODES_UNHANDLED = {
    0x004C: "SustainedSkillCommand",
    0x004D: "SkillStopCommand",
}
#: Commands::PickupItemCommand. The client sends the item's actor id and nothing
#: else — four bytes, the shortest request in the protocol.
PICKUP_ITEM_OPCODE = 0x0064

#: The client's own god mode. Handled for completeness, but **this client will never
#: send any of it**: the -godmode switch sets one byte at offset 0x1a9d of a config
#: object, and that byte is written by two functions and read by none. Since the
#: displacement needs a 32-bit encoding, a read could not have been missed.
#:
#: So the feature is compiled out, exactly as its own keybinding section says —
#: "Debug (invisible / unavailable in public builds)" — leaving the labels and the
#: flag behind as dead data. The console is what actually works; see dsor/console.py.
GOD_MODE_OPCODES = {
    0x0100: "ActivateGodMode",
    0x0101: "GodModeKill",
    0x0102: "GodModeTeleport",
    0x0103: "GodModeCreateItem",
    0x0104: "GodModeToggleLootDrop",
}

#: The client asking what an entity is, once per entity it does not recognise. Its
#: body is the four-byte handle, and the answer is a 0x85/0x002A description.
DESCRIBE_ENTITY_OPCODE = 0x001C

#: A query the client sends twice, after the release and before it disconnects.
#: Each one is answered with a 0x010E/0x010C pair, and the second answer is the
#: larger of the two.
CLIENT_QUERY_OPCODE = 0x010B


def actor_id_matches_first(handle: bytes, served: int) -> bool:
    """Whether *handle* is the first of the served creatures.

    Compared by actor id rather than by call order, so a client asking about them out
    of sequence still swaps the same one.
    """
    from dsor.gameplay import actor_id
    from dsor.recorded import combat_ready_mobs

    first = combat_ready_mobs()[:served]
    return bool(first) and actor_id(first[0]) == handle


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
        #: What the world does, and what it currently is. Rules are set once from
        #: the command line; state changes every tick.
        self.rules = Rules()
        #: The player state with the skill book patched, built once.
        self._granted_state: bytes | None = None
        self.world = World(rules=self.rules)
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
        #: Datagrams a second accepted from one peer, and what each has sent this
        #: second. A flood from one address should cost that address and nobody else.
        self.peer_rate = 400
        self._peer_seen: dict[tuple[str, int], tuple[int, int]] = {}
        #: How many 0x010B queries each endpoint has asked, since the two recorded
        #: answers differ and are not interchangeable.
        self.queries_seen: dict[tuple[str, int], int] = {}
        #: How many time syncs each endpoint has sent, since how many get answered
        #: depends on the tier. See _answers_time_sync.
        self.time_syncs_answered: dict[tuple[str, int], int] = {}
        #: Clients that have finished entering the world and are being ticked.
        #: Where each of them last was, so a tick can report it.
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
            self.world.forget(sender)
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

    def _world(self) -> World:
        """The map instance, populated on first use.

        Lazily, because the creature count and their health come from the command
        line and are set after construction.
        """
        if not self.world.creatures and self.rules.mobs:
            self.world.populate(
                combat_ready_mobs()[: self.rules.mobs], self.rules.mob_max_health
            )
        return self.world

    def _ship(self, messages) -> None:
        """Send what the world produced.

        The one place the two halves meet: the world decides what to say and never
        touches a socket, and this decides how to put it on the wire.
        """
        for address, payload in messages:
            connection = self.connections.get(address)
            if connection is not None:
                self._queue(connection, payload, address)

    def _set_clock(self, sender) -> None:
        """Tell the world what time this player's connection thinks it is."""
        connection = self.connections.get(sender)
        if connection is not None:
            self.world.player(sender).server_tick = (
                connection.elapsed_ms() // GAME_TICK_MS
            )

    def _with_granted_skills(self, state: bytes) -> bytes:
        """The player state, with the skill book's ownership bits set.

        One bit per skill, in place, so nothing in a 631 KB message moves. Cached
        because patching it on every login would mean walking it again for nothing.
        """
        wanted: set[int] = set()
        for name in self.rules.granted_skills:
            found = skill_index(name)
            if found is None:
                log.warning("%s: no skill named %r in the book", self.name, name)
            else:
                wanted.add(found)
        if self.rules.grant_up_to_level:
            wanted |= up_to_level(self.rules.grant_up_to_level)
        if not wanted:
            return state
        if self._granted_state is None:
            self._granted_state = with_granted(state, wanted)
            log.info(
                "%s: granted %d skill(s) in the book: %s",
                self.name,
                len(wanted),
                ", ".join(sorted(
                    name for name, (index, _u) in BOOK_SKILLS.items()
                    if index in wanted
                )),
            )
        return self._granted_state

    def _announce_vicinity(self, connection: Connection, sender) -> None:
        """Tell the client which actors are near it.

        The leg of the exchange this server skipped. The client's handler for this
        announcement calls RequestActor directly, so an actor announced this way is
        set up by the path the real server used; one the client merely noticed in a
        position update is not.
        """
        if not self.rules.mobs or not self.rules.announce_vicinity:
            return
        world = self.world._ready()
        if any(c.blueprint for c in world.creatures.values()):
            # From the map's own spawn table: twenty-five creatures, three of them
            # champions, at the positions the client's map data gives.
            served = [
                int.from_bytes(c.actor, "little") for c in world.creatures.values()
            ]
        else:
            served = [
                int.from_bytes(actor_id(record), "little")
                for record in combat_ready_mobs()[: self.rules.mobs]
            ]
        if not served:
            return
        # Generated, not replayed: a count then that many 32-bit ids, byte-aligned
        # throughout. One message announces the whole set, where the recorded ones
        # announced a creature each and only existed for the six that happened to be
        # captured.
        self._queue(
            connection,
            encode_actors_enter_vicinity(
                served, int.from_bytes(self.world.player(sender).actor, "little")
            ),
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
        servable = {actor_id(record) for record in combat_ready_mobs()[: self.rules.mobs]}
        mapped = self.world._ready().creature(handle)
        if mapped is not None and mapped.blueprint and mapped.described_at:
            # Built rather than looked up: a library description of any creature,
            # renamed to the blueprint the map wants and placed where the map puts
            # it. The name is the description's first field and the position sits 448
            # bits from its end, so both can be rewritten without understanding what
            # lies between.
            library = monster_library()
            base = library.get(mapped.blueprint)
            if base is None:
                log.warning(
                    "%s: no whole description for %s, refusing to rename one",
                    self.name, mapped.blueprint,
                )
                return
            points = [(x, e, y) for _n, x, e, y in SPAWN_POINTS]
            bit = spawn_bit(base, points)
            if bit is None:
                log.warning(
                    "%s: cannot locate the position in %s, refusing to move it",
                    self.name, mapped.blueprint,
                )
                return
            try:
                built = with_actor(base, handle)
                built = with_library_spawn_at(built, bit, *mapped.described_at)
            except ValueError as error:
                log.warning("%s: %s", self.name, error)
            else:
                mapped.described = True
                self._queue(connection, built, sender)
                log.info(
                    "%s: described %s as %s at (%.2f, %.2f, %.2f) to %s",
                    self.name,
                    handle.hex(" "),
                    mapped.blueprint,
                    *mapped.described_at,
                    sender,
                )
                return
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
        if self.rules.mob_near:
            here = self.world.player(sender).position
            if here is not None:
                index = sum(
                    1 for c in self.world._ready().creatures.values() if c.described
                )
                angle = 2 * math.pi * index / max(1, self.rules.mobs)
                description = with_spawn(
                    description,
                    here.x / WORLD + self.rules.mob_near * math.cos(angle),
                    0.0,
                    here.y / WORLD + self.rules.mob_near * math.sin(angle),
                )
        if self.rules.mob_first_command:
            # Send only the command addressed to this creature. The recorded
            # description is a batch — the NewMonsterCommand and then several more,
            # the last about the player, whose actor id is what the batch's own
            # trailer carries. Replaying commands aimed at other actors is at best
            # noise.
            try:
                description = first_command(description, handle)
            except ValueError as error:
                log.warning("%s: %s", self.name, error)
        if self.rules.mob_swap and actor_id_matches_first(handle, self.rules.mobs):
            # Swap the first served creature for a library blueprint, keeping the slot's
            # own movement record so it stands where that slot stood. What position it
            # is *drawn* at comes from the swapped description, which this server cannot
            # read — so it may well appear somewhere else entirely, or not at all. The
            # kill still announces the death at the slot's own position for the same
            # reason.
            library = monster_library()
            swapped = library.get(self.rules.mob_swap)
            if swapped is None:
                log.warning(
                    "%s: no library creature named %r; have %s",
                    self.name,
                    self.rules.mob_swap,
                    ", ".join(sorted(library)),
                )
            else:
                log.info(
                    "%s: serving %r in place of entity %s",
                    self.name,
                    self.rules.mob_swap,
                    handle.hex(" "),
                )
                # Rewritten to create the actor the client asked about. Without this
                # it creates the actor the description carries — 31 for the swamp
                # creature — and the client goes on asking about the one it wanted
                # every three seconds, which is exactly what happened.
                try:
                    swapped = with_actor(swapped, handle)
                    # And placed where the slot's own creature stood. A library
                    # description carries the position of wherever it was captured —
                    # the swamp creature's is (-27.45, 7.00, -10.48), on the third map
                    # — so served untouched it appears a hundred units away, correctly
                    # and invisibly. Its offset is 448 bits from the end, found by
                    # cross-reference: three creatures appear both as a batch, whose
                    # position offset was known, and as a single command here.
                    slot = entity_descriptions().get(handle)
                    if slot is not None:
                        x, elevation, y = monster_spawn(slot)
                        swapped = with_library_spawn(swapped, x, elevation, y)
                        log.info(
                            "%s: placed it at (%.2f, %.2f, %.2f)",
                            self.name,
                            x,
                            elevation,
                            y,
                        )
                except ValueError as error:
                    log.warning("%s: %s", self.name, error)
                described = self.world._ready().creature(handle)
                if described is not None:
                    described.described = True
                self._queue(connection, swapped, sender)
                return

        if self.rules.mob_template:
            # The blueprint name is the description's first field, so it can be
            # replaced and everything behind it copied bit for bit. What the client
            # accepts is limited by its own data: an unresolvable name logs an invalid
            # template id and creates nothing.
            try:
                description = with_template(description, self.rules.mob_template)
            except ValueError as error:
                log.warning("%s: %s", self.name, error)
        self._queue(connection, description, sender)
        # No health is reported for a creature, because there is no such message. Two
        # captured sessions carry 154 ActorStatsUpdateCommands between them and every
        # single one targets the player — including the session in which six creatures
        # were killed. This sent a baseline anyway for a while; it was noise the real
        # server never emits, and the client took no notice of it either way. The
        # counter below is kept only to decide when to send the removal.
        known = self.world._ready().creature(handle)
        if known is not None:
            known.described = True
        log.info(
            "%s: described entity %s to %s (%d bytes)",
            self.name,
            handle.hex(" "),
            sender,
            len(description),
        )

    def game_tick(self) -> None:
        """Advance the world one tick, and ship what it produced."""
        for player in list(self.world.inhabitants()):
            if player.address not in self.connections:
                self.world.forget(player.address)
                continue
            self._set_clock(player.address)
        self._ship(self.world.tick())

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

    def within_rate(self, sender) -> bool:
        """Whether this peer is inside its datagram budget for the current second.

        A flood from one address should cost that address and nobody else. The client
        sends about twenty-five movement records a second, so the default of four
        hundred passes ordinary play by a wide margin and still bounds what one peer
        can make this server do.

        Counted in whole seconds rather than a sliding window: a window is more
        accurate and this is a bound, not a measurement.
        """
        if not self.peer_rate:
            return True
        second = int(time.monotonic())
        seen, at = self._peer_seen.get(sender, (0, second))
        if at != second:
            seen, at = 0, second
        seen += 1
        self._peer_seen[sender] = (seen, at)
        if seen == self.peer_rate + 1:
            log.warning(
                "%s: %s is over %d datagrams a second; dropping the rest",
                self.name,
                sender,
                self.peer_rate,
            )
        return seen <= self.peer_rate

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
            if self.world.player(sender).in_world:
                log.info("%s: %s repeated its ready signal", self.name, sender)
                return
            # Ordering indices on the real map server: ack 4, the cosmetics table
            # 5-6 (it splits), then 0x00A7 at 7 and 0x0074 at 8, and only then the
            # tick pairs. These last two were dropped in an earlier version because
            # the frame order suggested they came much later; the ordering index
            # says otherwise, and it is the authority.
            mapped = any(c.blueprint for c in self.world._ready().creatures.values())
            for index, piece in enumerate(map_entry_sequence()):
                if index == 2:
                    piece = self._with_granted_skills(piece)
                if mapped and index == 4:
                    # The recorded vicinity announcement names the six creatures of
                    # the session it came from. Sent alongside the map's own spawn
                    # table, the client ends up knowing actors nothing can describe —
                    # it asked about 14 00 01 00 and got nothing — and drawing
                    # creatures at positions no creature stands at. Invisible mobs,
                    # and mobs that cannot be hit because the server has no such
                    # creature. This server announces its own set instead.
                    continue
                self._queue(connection, piece, sender)
            entrant = self.world.player(sender)
            entrant.position = spawn
            entrant.in_world = True
            if self.rules.start_level > 1 and not entrant.experience:
                entrant.experience = level_bounds(self.rules.start_level)[0]
                entrant.level = level_for(entrant.experience)
                if entrant.level != self.rules.start_level:
                    # Levels 105 to 110 all begin at 882229606 in the warrior's
                    # curve, so experience cannot tell them apart and asking for any
                    # of them lands on 110. Say so rather than report a level the
                    # client will not agree with.
                    log.warning(
                        "%s: level %d and level %d share an experience threshold, "
                        "so %s arrives at %d",
                        self.name,
                        self.rules.start_level,
                        entrant.level,
                        sender,
                        entrant.level,
                    )
                log.info(
                    "%s: %s arrives at level %d with %d experience",
                    self.name, sender, entrant.level, entrant.experience,
                )
            # Whatever their level says, so a level 15 warrior does not arrive with
            # a bar one twelfth full.
            entrant.max_health = self.world.player_health(entrant.level)
            entrant.health = entrant.max_health
            self._announce_vicinity(connection, sender)
            self._set_clock(sender)
            self._ship(self.world.enter(sender))
            log.info("%s: %s entered the world at %s", self.name, sender, spawn)
            return

        if (
            self.role == "map"
            and game.message_id == 0x8B
            and game.opcode in SKILL_OPCODES
        ):
            # Every skill command begins the same way: the skill's wire index, and
            # a float saying where the player is aiming. Both come from the command
            # rather than being inferred — a movement record's facing is the last one
            # the client happened to send, and a player who turns and swings in the
            # same breath has moved on.
            use = decode_skill_use(game.body)
            wire = use.wire if use else None
            aim = use.heading if use else None
            known = skill_at(wire)
            log.info(
                "%s: %s used %s (%s) aiming %s via %s",
                self.name,
                sender,
                known.id if known else f"unknown skill {wire}",
                f"{known.targeting} {known.hit_range:g}u x{known.damage_modifier:g}"
                if known
                else "no template",
                f"{aim * 360 // 256}deg" if aim is not None else "unstated",
                SKILL_OPCODES[game.opcode],
            )
            self._set_clock(sender)
            self._ship(self.world.attack(sender, wire, aim))
            return

        if (
            self.role == "map"
            and game.message_id == 0x8B
            and game.opcode in GOD_MODE_OPCODES
        ):
            # Logged in full before anything is done with it. None of these has ever
            # been seen on the wire, so their payloads are unknown, and the first
            # useful thing is to find out whether the client sends them at all.
            log.info(
                "%s: god mode %s from %s, %d bytes: %s",
                self.name,
                GOD_MODE_OPCODES[game.opcode],
                sender,
                len(game.body),
                game.body[:48].hex(" "),
            )
            if game.opcode == 0x0101:
                self._ship(self.world.smite_all(sender))
            return

        if (
            self.role == "map"
            and game.message_id == 0x8B
            and game.opcode == PICKUP_ITEM_OPCODE
            and len(game.body) >= 4
        ):
            self._ship(self.world.pick_up(sender, game.body[:4]))
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
            mover = self.world.player(sender)
            moved = decode_client_movement(game.body)
            # Checked, not taken. See dsor/trust.py: this was
            # "mover.position = moved.position", straight from the datagram.
            self.world.accept_movement(sender, moved.position)
            # Which way the player is facing, for an arc skill to be an arc.
            #
            # The second byte, not the first, and the difference is the whole bug.
            # The pair is (movement heading, body facing), and a player who is
            # standing still is not moving anywhere: across twelve sessions on the
            # live service, 128580 stationary records carry zero in the first byte
            # 99.9% of the time while the second holds a real facing — 147 distinct
            # values. When the player is moving the two agree to within three units
            # in 88% of 5318 records, so the second byte is right in both states and
            # the first is right in only one.
            #
            # Reading the first meant every blow was aimed north, because a player
            # attacks standing still. mightyswing's 170 degree arc pointed the same
            # way whatever the screen showed, which is why some swings hit one
            # creature where they should have hit three.
            mover.heading = moved.direction[1]
            # The client's own game tick, taken from its movement record rather than
            # invented. A skill's start tick is compared against it, and a stale one
            # makes the visualizer finish the instant it is created.
            mover.tick = int.from_bytes(game.body[9:13], "little")
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
    mob_health: float = 0.0,
    mob_damage: float = 0.0,
    mob_near: float = 0.0,
    creature_damage: float = 0.0,
    status_effects: bool = True,
    force_effects: bool = False,
    animated_effects: bool = False,
    player_health: float = 0.0,
    enforce: bool = True,
    pickup_range: float = 12.0,
    peer_rate: int = 400,
    tough_mob: float = 0.0,
    tough_mobs: int = 1,
    creature_skill: int = 440,
    mob_despawn: bool = False,
    mob_first_command: bool = False,
    mob_template: str | None = None,
    mob_swap: str | None = None,
    map_spawns: bool = False,
    granted_skills: list[str] | None = None,
    grant_up_to_level: int = 0,
    start_level: int = 0,
    kill_experience: int = 17,
    mob_chase: bool = True,
    mob_speed: int | None = None,
    skill_lead: int | None = None,
    drop_items: bool = True,
    allow_pickup: bool = True,
    console_port: int = 2199,
    console_host: str = "127.0.0.1",
    first_slot: int | None = None,
    slot_capacity: int | None = None,
    drop_templates: list[str] | None = None,
    mob_aggro: float | None = None,
    mob_stop: float | None = None,
    level_every: int = 34,
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
    console = None

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
        service.rules.mobs = mobs
        service.rules.mob_radius = mob_radius
        service.rules.mob_patrol = mob_patrol
        service.rules.mob_max_health = mob_health
        service.rules.mob_damage = mob_damage
        # The ceiling only matters when one figure is forced on everybody; with
        # per-creature health the tallest template is the ceiling. Fifty, the undead
        # mage champion's.
        service.rules.mob_max_health_ceiling = int(
            mob_health or max(m.hit_points for m in MONSTERS.values())
        )
        service.rules.mob_near = mob_near
        service.rules.creature_damage = creature_damage
        service.rules.creature_skill = creature_skill
        service.rules.status_effects = status_effects
        service.rules.force_effects = force_effects
        service.rules.animated_effects = animated_effects
        service.rules.player_max = int(player_health)
        service.rules.enforce = enforce
        service.rules.pickup_range = pickup_range
        service.peer_rate = peer_rate
        service.rules.mob_despawn = mob_despawn
        service.rules.mob_first_command = mob_first_command
        service.rules.mob_template = mob_template
        service.rules.mob_swap = mob_swap
        service.rules.granted_skills = list(granted_skills or [])
        service.rules.grant_up_to_level = grant_up_to_level
        service.rules.start_level = start_level
        if map_spawns and role == "map":
            usable = servable_points(set(monster_library()))
            service.world.populate_from_map(usable, mob_health)
            service.rules.mobs = len(usable)
            if tough_mob:
                service.world.toughen(tough_mob, max(1, tough_mobs))
            missing = sorted(
                {name for name, *_ in SPAWN_POINTS}
                - {name for name, *_ in usable}
            )
            log.info(
                "map spawns: %d of %d point(s); no whole description for %s",
                len(usable), len(SPAWN_POINTS), ", ".join(missing) or "nothing",
            )
        service.rules.kill_experience = kill_experience
        service.rules.mob_chase = mob_chase
        if mob_speed is not None:
            service.rules.mob_speed = mob_speed
        if skill_lead is not None:
            service.rules.skill_lead = skill_lead
        service.rules.drop_items = drop_items
        service.rules.allow_pickup = allow_pickup
        if first_slot is not None:
            service.world.first_slot = first_slot
        if slot_capacity is not None:
            service.world.slot_capacity = slot_capacity
        service.rules.drop_templates = list(drop_templates or [])
        if mob_aggro is not None:
            service.rules.mob_aggro = mob_aggro
        if mob_stop is not None:
            service.rules.mob_stop = mob_stop
        service.rules.level_every = level_every
        service.rules.announce_vicinity = announce_vicinity
        service.shop_offers = shop_offers
        service.shop_price = shop_price
        services[service.socket] = service
        selector.register(service.socket, selectors.EVENT_READ)
        if role == "map" and console_port:
            # On the map service, because everything worth changing lives in its
            # world. Registered in the same selector as the game sockets, so it
            # neither blocks the tick nor needs a thread.
            console = Console(service, host=console_host, port=console_port)
            selector.register(console.socket, selectors.EVENT_READ)
            log.info("console on %s:%d", console_host, console_port)
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
                if console is not None and key.fileobj is console.socket:
                    console.accept()
                    continue
                service = services[key.fileobj]
                raw, sender = service.socket.recvfrom(2048)
                # Caught, and *loudly*. The original note here said there was no
                # blanket try/except on purpose, because during protocol bring-up a
                # silent exception is indistinguishable from a client that stopped
                # answering, and that costs days. That reasoning stands and this
                # keeps it: the traceback and the offending datagram both go to the
                # log at ERROR, so nothing is hidden.
                #
                # What changed is the cost of the alternative. One unexpected byte
                # took the whole server down mid-session — a movement record reading
                # 0x59 in a field this server insisted could only be 0 or 0x40, which
                # was a client correctly reporting a 40% movement buff. Losing every
                # connected player to one bad message is worse than carrying on with
                # a loud complaint, and a crash during play-testing hides everything
                # that would have come after it.
                if not service.within_rate(sender):
                    continue
                try:
                    service.handle(raw, sender)
                except Exception:
                    log.exception(
                        "%s: %s sent %d bytes this server could not handle: %s",
                        service.name,
                        sender,
                        len(raw),
                        raw.hex(" "),
                    )
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
        "--trust-client",
        dest="enforce",
        action="store_false",
        help=(
            "believe whatever the client says about where it is, which skill it used, "
            "how often and what it picked up. For replaying a capture, not for running "
            "a service — see dsor/trust.py for what is checked and why each tolerance "
            "is as loose as it is"
        ),
    )
    parser.add_argument(
        "--pickup-range",
        type=float,
        default=12.0,
        metavar="U",
        help="how far a player may be from an item and still pick it up, world units",
    )
    parser.add_argument(
        "--peer-rate",
        type=int,
        default=400,
        metavar="N",
        help=(
            "datagrams a second accepted from one peer before the rest are dropped. "
            "The client sends about 25 movement records a second, so 400 is generous; "
            "0 turns the limit off"
        ),
    )
    parser.add_argument(
        "--player-health",
        type=float,
        default=0.0,
        metavar="HP",
        help=(
            "force this much health on the player. The default, 0, uses the level "
            "table: 225 at level 1, 450000 at 100. Lowering it is how a life leech or "
            "a heal becomes visible at all — at 450000 against creatures that hit for "
            "1, the bar never moves and nothing has anything to heal"
        ),
    )
    parser.add_argument(
        "--tough-mob",
        type=float,
        default=0.0,
        metavar="HP",
        help=(
            "give one creature this much health, champions first, so a skill can be "
            "watched. Everything else keeps its own template's number, unlike "
            "--mob-health. See also --tough-mobs"
        ),
    )
    parser.add_argument(
        "--tough-mobs",
        type=int,
        default=1,
        metavar="N",
        help="how many creatures --tough-mob applies to (default 1)",
    )
    parser.add_argument(
        "--animated-effects",
        action="store_true",
        help=(
            "also send the effects the client animates — the stun, the poison, the "
            "movement-speed buff, the armour break. Off: removing the borrowed float3 "
            "vectors was not enough, and the client still asserts in its sequencer on "
            "a FixedArray of TrackSequencer. The prime suspect is the seventh 32-bit "
            "field, which carries 100 where MaxStackSize is 1"
        ),
    )
    parser.add_argument(
        "--force-effects",
        action="store_true",
        help=(
            "apply the effect entries whose chance is zero. Those are gated behind an "
            "item, a talent or a set bonus, and the warrior's stun and poison are "
            "among them: laceratingstrike's debuff_cc_stun and mightybash's "
            "debuff_dot_poison both read C:0.0, so faithfully they never fire without "
            "a talent this server does not model. A testing switch, not fidelity"
        ),
    )
    parser.add_argument(
        "--no-status-effects",
        dest="status_effects",
        action="store_false",
        help=(
            "do not serve a skill's status effects. They travel as a rewritten "
            "recording of the per-tick 0x004F, so this is the switch if that upsets "
            "the client"
        ),
    )
    parser.add_argument(
        "--mob-health",
        type=float,
        default=0.0,
        metavar="H",
        help=(
            "force this health on every creature. The default, 0, gives each creature "
            "what the client's own monster table says it has: 24 for the tutorial "
            "dungeon's creature, 50 for the undead mage champion. Forcing a figure "
            "the client's table disagrees with is what gets drawn as a heal"
        ),
    )
    parser.add_argument(
        "--mob-damage",
        type=float,
        default=0.0,
        metavar="D",
        help=(
            "health one blow takes off a creature, or 0 — the default — to follow "
            "the character's own level curve from _Template_XPLevels: 15 at level "
            "one, 17 at two. A fixed 4 was invented here"
        ),
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
        "--kill-experience",
        type=int,
        default=17,
        metavar="N",
        help="experience awarded per kill, 0 for none. A real award carried 17",
    )
    parser.add_argument(
        "--start-level",
        type=int,
        default=0,
        metavar="N",
        help=(
            "the level a player arrives at, experience set to that level's floor. "
            "Capped at 104: levels 105 to 110 share an experience threshold, so "
            "their bar has zero width and the client's character sheet asserts on it"
        ),
    )
    parser.add_argument(
        "--grant-skill",
        dest="granted_skills",
        metavar="NAME",
        action="append",
        help="mark a skill as owned in the replayed book. Repeat or comma-separate. "
        "mightyswing is what the interface calls Rageful Swing",
    )
    parser.add_argument(
        "--grant-skills-to-level",
        type=int,
        default=0,
        metavar="N",
        help="grant every listed skill a character of level N has unlocked",
    )
    parser.add_argument(
        "--map-spawns",
        action="store_true",
        help="populate the world from the client's own map data — 25 creatures at "
        "the positions _Instance_SpawnPoint gives, three of them champions and one "
        "a health globe — instead of the six a recorded batch held",
    )
    parser.add_argument(
        "--mob-swap",
        metavar="NAME",
        help=(
            "serve this library blueprint in place of the first creature — including "
            "the tutorial's champions. It is placed where that creature stood: the "
            "position sits 448 bits from the end of a single-command description, "
            "found by cross-referencing three creatures that appear both as a batch "
            "and as one command. An earlier version of this help said the position "
            "could not be read, which stopped being true"
        ),
    )
    parser.add_argument(
        "--mob-template",
        metavar="NAME",
        help=(
            "spawn this blueprint instead of the recorded one. The recorded creatures "
            "are a0001_gen_anderworld_creature, ..._1st_encounter and ..._1st_loot; "
            "any name the client's own data resolves under 'Monster' should work"
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
        "--mob-despawn",
        action="store_true",
        help=(
            "clear a killed creature's body at once instead of leaving it lying. On by "
            "mistake once, and it removed the corpse before the death animation could "
            "play"
        ),
    )
    parser.add_argument(
        "--creature-skill",
        type=int,
        default=440,
        metavar="N",
        help=(
            "zero-based index of the skill creatures swing with. 440 is "
            "AnderworldCreatureStrike, row 441 of the client's skill table; the client "
            "refuses one the creature's template does not grant"
        ),
    )
    parser.add_argument(
        "--creature-damage",
        type=float,
        default=0.0,
        metavar="D",
        help=(
            "force this blow on every creature; negative disables retaliation. The "
            "default, 0, gives each creature its own template's damage times its "
            "skill's multiplier: 1 for the tutorial dungeon's creature, 2 to 3 for "
            "the champion that opens the exit, 5 to 9 for the undead mage"
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
        "--skill-lead",
        type=int,
        default=None,
        metavar="TICKS",
        help="how many game ticks ahead a creature's swing is announced",
    )
    parser.add_argument(
        "--drop-item",
        dest="drop_templates",
        metavar="NAME",
        action="append",
        default=None,
        help="blueprint a dying creature leaves, from the client's _Template_Item. "
        "Repeat, or give a comma-separated list, to cycle through several",
    )
    parser.add_argument(
        "--console-port",
        type=int,
        default=2199,
        metavar="PORT",
        help="line-oriented admin console; 0 to switch it off",
    )
    parser.add_argument(
        "--console-host",
        default="127.0.0.1",
        metavar="ADDR",
        help="what the console listens on. Loopback by default: it hands out items "
        "and levels and has no authentication",
    )
    parser.add_argument(
        "--first-slot",
        type=int,
        default=None,
        metavar="N",
        help="first bag cell a pickup may use. Not 0: the character arrives with two "
        "items the client places itself, and claiming an occupied cell asserts",
    )
    parser.add_argument(
        "--slot-capacity",
        type=int,
        default=None,
        metavar="N",
        help="how many bag cells there are. The storage descriptors say 5",
    )
    parser.add_argument(
        "--no-pickup",
        dest="allow_pickup",
        action="store_false",
        help="leave a clicked item on the ground rather than replaying the recorded "
        "inventory, which rearranges the player's own equipment",
    )
    parser.add_argument(
        "--no-drops",
        dest="drop_items",
        action="store_false",
        help="stop a dying creature leaving an item where it fell",
    )
    parser.add_argument(
        "--mob-aggro",
        dest="mob_aggro",
        type=float,
        default=None,
        metavar="UNITS",
        help="world units within which a creature notices the player",
    )
    parser.add_argument(
        "--mob-stop",
        dest="mob_stop",
        type=float,
        default=None,
        metavar="UNITS",
        help="world units a creature stops short of the player",
    )
    parser.add_argument(
        "--mob-speed",
        type=int,
        default=None,
        metavar="UNITS",
        help="wire units a creature covers per game tick, about 6 for a walk",
    )
    parser.add_argument(
        "--no-chase",
        dest="mob_chase",
        action="store_false",
        help="leave creatures where they stand instead of walking them at the player",
    )
    parser.add_argument(
        "--level-every",
        type=int,
        default=0,
        metavar="N",
        help="experience needed per level, 0 to never level. 34 is two kills at 17",
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
        status_effects=args.status_effects,
        force_effects=args.force_effects,
        animated_effects=args.animated_effects,
        player_health=args.player_health,
        enforce=args.enforce,
        pickup_range=args.pickup_range,
        peer_rate=args.peer_rate,
        tough_mob=args.tough_mob,
        tough_mobs=args.tough_mobs,
        creature_skill=args.creature_skill,
        mob_despawn=args.mob_despawn,
        mob_first_command=args.mob_first_command,
        mob_template=args.mob_template,
        mob_swap=args.mob_swap,
        map_spawns=args.map_spawns,
        granted_skills=[
            name for entry in (args.granted_skills or [])
            for name in entry.split(",") if name
        ],
        grant_up_to_level=args.grant_skills_to_level,
        start_level=args.start_level,
        kill_experience=args.kill_experience,
        mob_chase=args.mob_chase,
        mob_speed=args.mob_speed,
        skill_lead=args.skill_lead,
        drop_items=args.drop_items,
        allow_pickup=args.allow_pickup,
        console_port=args.console_port,
        console_host=args.console_host,
        first_slot=args.first_slot,
        slot_capacity=args.slot_capacity,
        drop_templates=[
            name
            for entry in (args.drop_templates or [])
            for name in entry.split(",")
            if name
        ],
        mob_aggro=args.mob_aggro,
        mob_stop=args.mob_stop,
        level_every=args.level_every,
        announce_vicinity=args.announce_vicinity,
    )


if __name__ == "__main__":
    main()
