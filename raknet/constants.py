"""RakNet wire constants.

Every value here was taken from RakNet's own ``MessageIdentifiers.h`` (the
BSD-licensed reference implementation) and then confirmed against a real capture
of the Drakensang Online client, so the names match what the client actually
sends rather than what a wiki says.

Two facts are worth reading before anything else in this package:

1. **The datagram layer and the message layer both use values >= 0x80.**  A
   datagram's first byte is a *bitfield* (see ``DatagramFlag``), while a message
   ID is the first byte of a frame's *payload*.  Both are commonly 0x84 in this
   game, which is a well-known source of confusion: 0x84 as a datagram byte
   means "valid, needs bandwidth stats", 0x84 as a message ID means
   ``ID_USER_PACKET_ENUM``.

2. **User message IDs start at 0x84**, so a game's "custom" messages are really
   ``ID_USER_PACKET_ENUM + n``.  Anything below that is RakNet's own, including
   the reserved slots 0x82/0x83 that Drakensang has appropriated.
"""

from enum import IntEnum

#: Sent with every unconnected (pre-handshake) message so a RakNet peer can tell
#: RakNet traffic from anything else that happens to arrive on the port.
OFFLINE_MESSAGE_ID = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")

#: The protocol revision the client announces in OPEN_CONNECTION_REQUEST_1.
#: A mismatch makes the server answer ID_INCOMPATIBLE_PROTOCOL_VERSION and the
#: client gives up, so this is the first thing to check when nothing works.
#: Observed value for Drakensang Online: 5 (i.e. RakNet 4.x).
RAKNET_PROTOCOL_VERSION = 5

#: RakNet always reserves room for this many internal addresses in
#: CONNECTION_REQUEST_ACCEPTED, padding with 0.0.0.0:0.
MAX_INTERNAL_IDS = 10


class MessageID(IntEnum):
    """The subset of RakNet's message IDs this server needs.

    Values are RakNet's, not ours.  ``USER_PACKET_ENUM`` is the boundary: the
    game's own messages live at or above it.
    """

    CONNECTED_PING = 0x00
    UNCONNECTED_PING = 0x01
    CONNECTED_PONG = 0x03
    OPEN_CONNECTION_REQUEST_1 = 0x05
    OPEN_CONNECTION_REPLY_1 = 0x06
    OPEN_CONNECTION_REQUEST_2 = 0x07
    OPEN_CONNECTION_REPLY_2 = 0x08
    CONNECTION_REQUEST = 0x09
    CONNECTION_REQUEST_ACCEPTED = 0x10
    CONNECTION_ATTEMPT_FAILED = 0x11
    ALREADY_CONNECTED = 0x12
    NEW_INCOMING_CONNECTION = 0x13
    NO_FREE_INCOMING_CONNECTIONS = 0x14
    DISCONNECTION_NOTIFICATION = 0x15
    CONNECTION_LOST = 0x16
    INCOMPATIBLE_PROTOCOL_VERSION = 0x19
    UNCONNECTED_PONG = 0x1C
    TIMESTAMP = 0x1B
    #: First ID a game may use for itself.  Everything below is RakNet's.
    USER_PACKET_ENUM = 0x84


class DatagramFlag(IntEnum):
    """Bits of a datagram's first byte.

    ``IS_VALID`` is set on every datagram that belongs to an established
    connection; a first byte without it is an unconnected message, whose value
    is a plain :class:`MessageID` instead of a bitfield.
    """

    IS_VALID = 0x80
    IS_ACK = 0x40
    #: Only meaningful when IS_ACK is set: the datagram carries a bandwidth
    #: figure after the flags.  When IS_ACK is clear this same bit means IS_NAK.
    HAS_B_AND_AS = 0x20
    IS_NAK = 0x20
    IS_PACKET_PAIR = 0x10
    IS_CONTINUOUS_SEND = 0x08
    NEEDS_B_AND_AS = 0x04


class Reliability(IntEnum):
    """RakNet's delivery guarantees, in the top three bits of a frame's flags."""

    UNRELIABLE = 0
    UNRELIABLE_SEQUENCED = 1
    RELIABLE = 2
    RELIABLE_ORDERED = 3
    RELIABLE_SEQUENCED = 4
    UNRELIABLE_WITH_ACK_RECEIPT = 5
    RELIABLE_WITH_ACK_RECEIPT = 6
    RELIABLE_ORDERED_WITH_ACK_RECEIPT = 7

    @property
    def has_reliable_index(self) -> bool:
        """Whether the frame header carries a reliable message number."""
        return self in (
            Reliability.RELIABLE,
            Reliability.RELIABLE_ORDERED,
            Reliability.RELIABLE_SEQUENCED,
            Reliability.RELIABLE_WITH_ACK_RECEIPT,
            Reliability.RELIABLE_ORDERED_WITH_ACK_RECEIPT,
        )

    @property
    def has_sequencing_index(self) -> bool:
        """Whether the frame header carries a sequencing index."""
        return self in (
            Reliability.UNRELIABLE_SEQUENCED,
            Reliability.RELIABLE_SEQUENCED,
        )

    @property
    def has_ordering_index(self) -> bool:
        """Whether the frame header carries an ordering index and channel.

        Sequenced reliabilities are ordered too: sequencing is ordering plus
        "drop anything older than the newest seen".
        """
        return self in (
            Reliability.UNRELIABLE_SEQUENCED,
            Reliability.RELIABLE_ORDERED,
            Reliability.RELIABLE_SEQUENCED,
            Reliability.RELIABLE_ORDERED_WITH_ACK_RECEIPT,
        )


#: Set in a frame's flags byte when the frame is one piece of a split message.
FRAME_HAS_SPLIT_PACKET = 0x10
