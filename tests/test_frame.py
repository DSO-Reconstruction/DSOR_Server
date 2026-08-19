"""Frame headers, whose width depends on the reliability."""

import pytest

from raknet.constants import Reliability
from raknet.frame import Frame, build_frame, parse_frames


def _round_trip(frame: Frame) -> Frame:
    encoded = build_frame(frame)
    (decoded,) = parse_frames(encoded, 0)
    return decoded


def test_payload_length_is_in_bits_not_bytes():
    encoded = build_frame(Frame(payload=b"\x84" * 49, reliable_index=0, ordering_index=0))
    # 49 octets is 392 bits; a byte count would put 0x31 here instead of 0x0188.
    assert encoded[1:3] == (392).to_bytes(2, "big")


def test_reliable_ordered_header_is_ten_bytes():
    frame = Frame(payload=b"\x88", reliable_index=2, ordering_index=3)
    # flags(1) + bit length(2) + reliable(3) + ordering(3) + channel(1)
    assert len(build_frame(frame)) == 10 + len(frame.payload)


@pytest.mark.parametrize("reliability", list(Reliability))
def test_every_reliability_round_trips(reliability):
    frame = Frame(
        payload=b"\x84hello",
        reliability=reliability,
        reliable_index=7 if reliability.has_reliable_index else None,
        sequencing_index=8 if reliability.has_sequencing_index else None,
        ordering_index=9 if reliability.has_ordering_index else None,
        ordering_channel=2,
    )
    decoded = _round_trip(frame)
    assert decoded.payload == frame.payload
    assert decoded.reliability is reliability
    assert decoded.reliable_index == frame.reliable_index
    assert decoded.sequencing_index == frame.sequencing_index
    assert decoded.ordering_index == frame.ordering_index


def test_unreliable_header_carries_no_indices():
    frame = Frame(payload=b"\x84", reliability=Reliability.UNRELIABLE)
    assert len(build_frame(frame)) == 3 + 1
    assert _round_trip(frame).reliable_index is None


def test_split_fields_round_trip():
    frame = Frame(
        payload=b"chunk",
        reliable_index=1,
        ordering_index=1,
        split_count=454,
        split_id=0,
        split_index=453,
    )
    encoded = build_frame(frame)
    assert encoded[0] & 0x10, "the split flag must be set"
    decoded = _round_trip(frame)
    assert (decoded.split_count, decoded.split_id, decoded.split_index) == (454, 0, 453)


def test_several_frames_in_one_datagram():
    body = build_frame(Frame(payload=b"\x88", reliable_index=2, ordering_index=3))
    body += build_frame(Frame(payload=b"\x84" * 49, reliable_index=3, ordering_index=2))
    frames = parse_frames(body, 0)
    assert [f.message_id for f in frames] == [0x88, 0x84]


def test_partial_bit_length_is_preserved():
    # RakNet packs booleans into single bits, so a payload's bit length is not
    # always a multiple of eight. Re-encoding must reproduce the original value,
    # not a recomputed one, or the bytes stop matching the wire.
    body = bytes([0x60]) + (3).to_bytes(2, "big") + b"\x00\x00\x00" + b"\x00\x00\x00\x00" + b"\xe0"
    (frame,) = parse_frames(body, 0)
    assert frame.bit_length == 3 and len(frame.payload) == 1
    assert build_frame(frame) == body


def test_truncated_payload_is_rejected():
    body = bytes([0x60]) + (800).to_bytes(2, "big") + b"\x00" * 7 + b"short"
    with pytest.raises(ValueError, match="payload bytes"):
        parse_frames(body, 0)


def test_missing_index_is_rejected_rather_than_zero_filled():
    with pytest.raises(ValueError, match="reliable_index"):
        build_frame(Frame(payload=b"\x84", ordering_index=0))
