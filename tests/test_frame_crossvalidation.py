"""This server's frame encoder against an independent working implementation.

The reference is the ``FrameBuilder`` from another Drakensang emulator, written from the
same captures by someone else and running against the same client. Two implementations
that agree byte for byte are evidence in a way that either one alone is not: a shared
misreading of the protocol would have to be made twice, identically.

It matters here because a long stretch of this project went looking for a bug in the
effect messages, and ruling the transport out cheaply is worth more than assuming it.

The reference, reproduced verbatim in the helpers below:

    reliable ordered  b'\\x60' + pack('>H', bits) + rel[:3] + ord[:3] + b'\\x00' + payload
    unreliable        b'\\x00' + pack('>H', bits) + payload
    sequenced         b'\\x20' + pack('>H', bits) + seq[:3] + ord[:3] + b'\\x00' + payload
    split             b'\\x70' + pack('>H', bits) + rel[:3] + ord[:3] + b'\\x00'
                      + pack('>I', count) + pack('>H', id) + pack('>I', index) + chunk

Note the asymmetry the reference makes explicit and this server also implements: the
frame's bit length and the three-byte indices are little-endian, while the *split*
fields are big-endian.
"""

import struct

from raknet.constants import Reliability
from raknet.frame import Frame, build_frame, parse_frames

PAYLOAD = bytes.fromhex("854f0001020304")


def reference_unreliable(payload, bits=None):
    bits = len(payload) * 8 if bits is None else bits
    return b"\x00" + struct.pack(">H", bits) + payload


def reference_reliable_ordered(payload, reliable, order, bits=None):
    bits = len(payload) * 8 if bits is None else bits
    return (
        b"\x60"
        + struct.pack(">H", bits)
        + struct.pack("<I", reliable)[:3]
        + struct.pack("<I", order)[:3]
        + b"\x00"
        + payload
    )


def reference_sequenced(payload, sequence, order, bits=None):
    bits = len(payload) * 8 if bits is None else bits
    return (
        b"\x20"
        + struct.pack(">H", bits)
        + struct.pack("<I", sequence)[:3]
        + struct.pack("<I", order)[:3]
        + b"\x00"
        + payload
    )


def reference_split(chunk, bits, reliable, order, count, identifier, index):
    return (
        b"\x70"
        + struct.pack(">H", bits)
        + struct.pack("<I", reliable)[:3]
        + struct.pack("<I", order)[:3]
        + b"\x00"
        + struct.pack(">I", count)
        + struct.pack(">H", identifier)
        + struct.pack(">I", index)
        + chunk
    )


def test_unreliable_matches():
    mine = build_frame(Frame(payload=PAYLOAD, reliability=Reliability.UNRELIABLE))
    assert mine == reference_unreliable(PAYLOAD)


def test_reliable_ordered_matches():
    mine = build_frame(
        Frame(
            payload=PAYLOAD,
            reliability=Reliability.RELIABLE_ORDERED,
            reliable_index=7,
            ordering_index=9,
            ordering_channel=0,
        )
    )
    assert mine == reference_reliable_ordered(PAYLOAD, 7, 9)


def test_sequenced_matches():
    mine = build_frame(
        Frame(
            payload=PAYLOAD,
            reliability=Reliability.UNRELIABLE_SEQUENCED,
            sequencing_index=3,
            ordering_index=9,
            ordering_channel=0,
        )
    )
    assert mine == reference_sequenced(PAYLOAD, 3, 9)


def test_a_sub_byte_bit_length_matches():
    """The field most often written as a byte count by mistake, in both directions."""
    mine = build_frame(
        Frame(
            payload=PAYLOAD,
            reliability=Reliability.RELIABLE_ORDERED,
            reliable_index=7,
            ordering_index=9,
            bit_length=53,
        )
    )
    assert mine == reference_reliable_ordered(PAYLOAD, 7, 9, 53)


def test_every_fragment_of_a_split_matches():
    """Including the two rules a split has to get right.

    One ordering index for the whole compound -- it is one message as far as ordering
    is concerned -- and a fresh reliable index per fragment. The last fragment's bit
    length is the remainder, not its byte size.
    """
    payload = bytes(range(200))
    budget = 64
    chunks = [payload[i : i + budget] for i in range(0, len(payload), budget)]
    total_bits = len(payload) * 8
    identifier, order = 5, 9
    assert len(chunks) == 4
    for index, chunk in enumerate(chunks):
        bits = min(len(chunk) * 8, total_bits - index * budget * 8)
        reliable = 100 + index
        mine = build_frame(
            Frame(
                payload=chunk,
                bit_length=bits,
                reliability=Reliability.RELIABLE_ORDERED,
                reliable_index=reliable,
                ordering_index=order,
                ordering_channel=0,
                split_count=len(chunks),
                split_id=identifier,
                split_index=index,
            )
        )
        assert mine == reference_split(
            chunk, bits, reliable, order, len(chunks), identifier, index
        ), index


def test_the_parser_reads_the_reference_back():
    """Both directions, so a shared mistake in one cannot hide in the other."""
    for raw, reliability in (
        (reference_reliable_ordered(PAYLOAD, 7, 9), Reliability.RELIABLE_ORDERED),
        (reference_unreliable(PAYLOAD), Reliability.UNRELIABLE),
        (reference_sequenced(PAYLOAD, 3, 9), Reliability.UNRELIABLE_SEQUENCED),
    ):
        datagram = b"\x84" + struct.pack("<I", 1)[:3] + raw
        frames = parse_frames(datagram, 4)
        assert len(frames) == 1
        assert frames[0].payload == PAYLOAD
        assert frames[0].reliability is reliability
        assert frames[0].bit_length == len(PAYLOAD) * 8
