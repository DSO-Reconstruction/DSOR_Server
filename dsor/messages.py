"""The Drakensang message layer: what sits inside a RakNet frame.

Three message shapes were identified from a 94,875-datagram capture of a full
gameplay session, and which shape applies is decided by the message ID:

============================  =========================================
``0x84`` ``0x85`` ``0x8B``    id, then a **16-bit little-endian opcode**,
                              then an opaque body
``0x82`` ``0x86`` ``0x8A``    id, then one or more **length-prefixed
                              strings** (16-bit little-endian length)
``0x88`` ``0x8D``             a bare one-byte signal, no body at all
============================  =========================================

There is no single universal envelope, and assuming one is a trap: the two bytes
after ``0x86`` look exactly like an opcode but are the length of the map name
that follows.  The distinction was settled by checking that the value always
equals the length of the string after it.

``0x1B`` is RakNet's ``ID_TIMESTAMP``, not a game message: it prefixes a real
message with an 8-byte clock reading.  :func:`parse_message` unwraps it, so
callers never have to special-case it.

Opcodes are reported as numbers unless a name is justified by evidence.  A guess
recorded as a name becomes indistinguishable from a fact within a week.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from raknet.constants import MessageID

from .protocol import decode_string, encode_string

#: Message IDs whose body begins with a 16-bit little-endian opcode.
OPCODE_MESSAGES = frozenset({0x84, 0x85, 0x8B})

#: Message IDs whose body begins with a length-prefixed string.
STRING_MESSAGES = frozenset({0x82, 0x86, 0x8A})

#: Message IDs that carry nothing at all; their meaning is their arrival.
SIGNAL_MESSAGES = frozenset({0x88, 0x8D})

#: Opcodes named because the capture shows unambiguously what they carry.
#: Keyed by (message id, opcode).
KNOWN_OPCODES = {
    # A host to connect to next, as "hostname" or "ip:port". This is how a
    # service hands the client off to a map server, and it explains why gameplay
    # traffic appears on ports that are not in any fixed list.
    (0x84, 0x0070): "SERVER_HANDOFF",
    # Carries a length-prefixed signal name, e.g. "Premium2DayReminderSignal".
    (0x8B, 0x0113): "NAMED_SIGNAL",
}

#: The two opcodes that carry the game: 84% of all messages in the capture.
#: Named after their role rather than their contents, which are still opaque.
BULK_CLIENT_OPCODE = 0x005F  #: 0x8B/0x005F, 19270 times, always 18 bytes
BULK_SERVER_OPCODE = 0x005F  #: 0x85/0x005F, 21577 times, 23 bytes and up


@dataclass
class GameMessage:
    """One decoded application message.

    ``opcode`` is set only for :data:`OPCODE_MESSAGES`; ``strings`` only for the
    shapes that carry them.  ``body`` is whatever was not interpreted, which for
    most gameplay messages is still everything after the opcode — that is the
    honest state of the reverse engineering, not a parsing failure.
    """

    message_id: int
    opcode: int | None = None
    body: bytes = b""
    strings: list[str] = field(default_factory=list)
    #: Set when the message arrived wrapped in RakNet's ID_TIMESTAMP.
    timestamp: int | None = None

    @property
    def name(self) -> str:
        """A stable identity for logging, so a message is never just bytes."""
        if self.opcode is None:
            return f"{self.message_id:#04x}"
        known = KNOWN_OPCODES.get((self.message_id, self.opcode))
        if known:
            return f"{self.message_id:#04x}/{known}"
        return f"{self.message_id:#04x}/{self.opcode:#06x}"

    @property
    def is_bulk(self) -> bool:
        """Whether this is one of the two high-rate gameplay messages."""
        return self.message_id in (0x85, 0x8B) and self.opcode == 0x005F


def parse_message(payload: bytes) -> GameMessage:
    """Decode one message, unwrapping a timestamp prefix if present.

    Raises :class:`ValueError` on an empty payload; everything else is decoded as
    far as its shape is known and the remainder is kept in ``body``.
    """
    if not payload:
        raise ValueError("empty message payload")

    timestamp = None
    if payload[0] == MessageID.TIMESTAMP:
        # ID_TIMESTAMP(1) + clock(8) + the real message.
        if len(payload) < 10:
            raise ValueError(f"timestamped message is only {len(payload)} bytes")
        timestamp = int.from_bytes(payload[1:9], "big")
        payload = payload[9:]

    message = _parse_body(payload)
    message.timestamp = timestamp
    return message


def _parse_body(payload: bytes) -> GameMessage:
    message_id = payload[0]

    if message_id in SIGNAL_MESSAGES:
        if len(payload) != 1:
            raise ValueError(
                f"{message_id:#04x} is a bare signal but carries "
                f"{len(payload) - 1} extra bytes"
            )
        return GameMessage(message_id)

    if message_id in OPCODE_MESSAGES:
        if len(payload) < 3:
            raise ValueError(f"{message_id:#04x} needs a 2-byte opcode")
        opcode = int.from_bytes(payload[1:3], "little")
        return GameMessage(message_id, opcode=opcode, body=payload[3:])

    if message_id in STRING_MESSAGES:
        return _parse_strings(payload)

    # A RakNet message, or one this table does not know yet. Keeping the bytes
    # is what lets an unknown message be reported rather than dropped.
    return GameMessage(message_id, body=payload[1:])


def _parse_strings(payload: bytes) -> GameMessage:
    """Read the leading length-prefixed strings, keeping the rest as body.

    Reading greedily would misinterpret binary tails as strings, so this stops at
    the first prefix that does not describe printable text.
    """
    message_id = payload[0]
    strings: list[str] = []
    offset = 1
    while offset + 2 <= len(payload):
        length = int.from_bytes(payload[offset : offset + 2], "little")
        start, end = offset + 2, offset + 2 + length
        if length == 0 or end > len(payload):
            break
        candidate = payload[start:end]
        if not _looks_like_text(candidate):
            break
        strings.append(candidate.decode("utf-8", errors="replace"))
        offset = end
    return GameMessage(message_id, body=payload[offset:], strings=strings)


def _looks_like_text(raw: bytes) -> bool:
    """Whether *raw* is plausibly a text field rather than a binary one."""
    return all(32 <= byte < 127 for byte in raw)


# ── Shape-specific readers ──────────────────────────────────────────────────
#
# Each of these takes an already-parsed GameMessage so the caller keeps the
# timestamp and the raw remainder.


def read_service_identity(message: GameMessage) -> str:
    """0x82: which service the client has just reached."""
    _expect(message, 0x82)
    if not message.strings:
        raise ValueError("service identity carries no name")
    return message.strings[0]


@dataclass(frozen=True)
class MapAssignment:
    """0x86: the map the client should load.

    The name appears **twice**.  Sending it once is the kind of difference that a
    client validates and rejects, so both copies are reproduced.
    """

    name: str
    #: The 32-bit little-endian value after the names. Observed as 0xFFFFFFFF on
    #: the character service and small integers on map servers; its meaning is
    #: not established.
    trailer: int


def read_map_assignment(message: GameMessage) -> MapAssignment:
    """0x86: read the map name and confirm it is duplicated as expected."""
    _expect(message, 0x86)
    if len(message.strings) < 2:
        raise ValueError(
            f"0x86 should carry the map name twice, got {len(message.strings)}"
        )
    if message.strings[0] != message.strings[1]:
        raise ValueError(
            f"0x86 map names disagree: {message.strings[0]!r} vs {message.strings[1]!r}"
        )
    trailer = int.from_bytes(message.body[:4], "little") if len(message.body) >= 4 else 0
    return MapAssignment(message.strings[0], trailer)


def read_client_identity(message: GameMessage) -> str:
    """0x8A: the client's own identity string, followed by an opaque credential.

    The 79-byte tail is not decoded.  It is stable across connections except for
    one discriminator byte, which suggests a session credential plus a target
    selector, but nothing in the capture proves that.
    """
    _expect(message, 0x8A)
    if not message.strings:
        raise ValueError("client identity carries no name")
    return message.strings[0]


def read_server_handoff(message: GameMessage) -> str:
    """0x84/0x0070: the address the client should connect to next.

    Observed as a bare hostname and as ``ip:port``.  This is the message a server
    emulator has to send to move a client onto its own map server, so it is the
    single most useful opcode in the table.
    """
    _expect(message, 0x84)
    if message.opcode != 0x0070:
        raise ValueError(f"expected opcode 0x0070, got {message.opcode:#06x}")
    target, _ = decode_string(message.body, 0)
    return target


def read_named_signal(message: GameMessage) -> str:
    """0x8B/0x0113: a signal identified by name."""
    _expect(message, 0x8B)
    if message.opcode != 0x0113:
        raise ValueError(f"expected opcode 0x0113, got {message.opcode:#06x}")
    name, _ = decode_string(message.body, 0)
    return name


def _expect(message: GameMessage, message_id: int) -> None:
    if message.message_id != message_id:
        raise ValueError(
            f"expected message {message_id:#04x}, got {message.message_id:#04x}"
        )


#: Byte appended after the handoff string. It is **not** constant: the three
#: handoffs in the reference session end differently, and the difference tracks
#: where the client is being sent.
#:
#:   to the character service   "…drakensang.com:2192"   0x80
#:   to a map server            "47.245.165.152:30201"   0x00
#:   the empty release handoff  ""                       0x00
#:
#: Reproduced rather than reasoned about, since the client is the only judge of
#: whether it matters — but reproduced *per destination*, not as one value.
HANDOFF_TRAILER_CHARACTER = b"\x80"
HANDOFF_TRAILER_MAP = b"\x00"


def build_server_handoff(
    target: str, trailer: bytes = HANDOFF_TRAILER_CHARACTER
) -> bytes:
    """0x84/0x0070: tell the client which host to connect to next.

    *target* is ``host:port``, exactly as the real server sends it — that form was
    observed carrying both a hostname and a bare address. This is the message that
    moves a client onto another server, so it is what points one at an emulator.

    *trailer* differs by destination; see :data:`HANDOFF_TRAILER_CHARACTER`.
    """
    return b"".join(
        [
            bytes([0x84]),
            (0x0070).to_bytes(2, "little"),
            encode_string(target),
            trailer,
        ]
    )


def build_bare_signal(message_id: int) -> bytes:
    """A one-byte message such as 0x88, whose arrival is its whole content."""
    if message_id not in SIGNAL_MESSAGES:
        raise ValueError(
            f"{message_id:#04x} is not a known bare signal; "
            f"expected one of {sorted(hex(m) for m in SIGNAL_MESSAGES)}"
        )
    return bytes([message_id])


#: Message wrapped by ID_TIMESTAMP for clock synchronisation.
TIME_SYNC = 0x83

#: The client's request carries an 8-byte body, the server's reply a 16-byte one.
TIME_SYNC_REQUEST_BODY = 8
TIME_SYNC_REPLY_BODY = 16


def build_timestamped(raknet_time_ms: int, payload: bytes) -> bytes:
    """Wrap *payload* in RakNet's ID_TIMESTAMP, as the server does for time syncs.

    The 8-byte header clock and whatever the payload carries are on **different
    scales**: the header is RakNet's own millisecond counter, while a time-sync
    body carries a shared game clock some two orders of magnitude larger. Filling
    both from one source produces a reply the client cannot make sense of.
    """
    return bytes([MessageID.TIMESTAMP]) + raknet_time_ms.to_bytes(8, "big") + payload


def read_time_sync_clock(payload: bytes) -> int:
    """Read the game clock out of a client time-sync request.

    *payload* is the unwrapped message, so it starts with 0x83.
    """
    if payload[0] != TIME_SYNC:
        raise ValueError(f"expected 0x83, got {payload[0]:#02x}")
    if len(payload) < 5:
        raise ValueError(f"time sync needs a 4-byte clock, got {len(payload) - 1}")
    return int.from_bytes(payload[1:5], "little")


def build_time_sync_reply(game_clock: int) -> bytes:
    """The 0x83 body a server sends: a game clock, then twelve zero bytes.

    *game_clock* is the **sender's own** clock, not the client's echoed back. That
    was measured, and it is the whole point of the message: on the character
    service the server announced 354,685,782 and the client's next reading was
    354,685,819 — it had adopted the server's clock. On a map server the same
    exchange runs from 5,573, because the value is that service's own uptime rather
    than any shared epoch.

    Echoing the client's clock back, as this once did, tells it nothing. It then
    has no world clock to synchronise to and says so indefinitely — the client's
    own display counted past 2784 without ever settling.
    """
    return (
        bytes([TIME_SYNC])
        + game_clock.to_bytes(4, "little")
        + bytes(TIME_SYNC_REPLY_BODY - 4)
    )


#: Maps observed in 0x86 assignments, with the 32-bit trailer each was sent with.
#: The pairing is not incidental: a0000_char, the character-selection screen, is
#: the only one carrying 0xFFFFFFFF, while every map served by an actual map
#: server carries a small integer. Reading 0xFFFFFFFF as "no instance" fits, but
#: it is an inference from seven samples — what is certain is that a name and its
#: trailer travelled together, so they should not be mixed by hand.
KNOWN_MAPS = {
    "a0000_char": 0xFFFFFFFF,
    "a0001_start_tutorial_dun": 0x0,
    "a0006_grimford_hub": 0x5,
    "a0007_pastures_pve": 0x1,
    "a0008_crypt_dun": 0x0,
    "a0200_kingscity": 0x5,
    "a0301_witchhole": 0x0,
}


def build_map_assignment(name: str, trailer: int | None = None) -> bytes:
    """0x86: the map the client should load.

    The name is written **twice**; the client validates the copies against each
    other, so sending it once is rejected.

    *trailer* defaults to the value this map was observed with (see KNOWN_MAPS).
    An unknown map has no default and must be given one explicitly, rather than
    inheriting a value that belongs to a different map.
    """
    if trailer is None:
        if name not in KNOWN_MAPS:
            raise ValueError(
                f"no trailer known for map {name!r}; pass one explicitly. "
                f"Observed maps: {', '.join(sorted(KNOWN_MAPS))}"
            )
        trailer = KNOWN_MAPS[name]
    return b"".join(
        [
            bytes([0x86]),
            encode_string(name),
            encode_string(name),
            trailer.to_bytes(4, "little"),
            bytes(2),
        ]
    )
