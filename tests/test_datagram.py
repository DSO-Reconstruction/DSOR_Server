"""The datagram envelope and acknowledgements."""

import pytest

from raknet.constants import DatagramFlag
from raknet.datagram import (
    AckRange,
    build_ack,
    build_datagram_header,
    coalesce,
    parse_ack_payload,
    parse_datagram_header,
)


def test_sequence_is_24_bit_little_endian():
    # 1 as little-endian over three bytes is 01 00 00; big-endian would be 00 00 01.
    assert build_datagram_header(1) == bytes([DatagramFlag.IS_VALID, 0x01, 0x00, 0x00])
    header, offset = parse_datagram_header(bytes([0x84, 0x05, 0x00, 0x00]))
    assert (header.sequence, offset) == (5, 4)


def test_sequence_wraps_at_24_bits():
    assert build_datagram_header((1 << 24) + 7)[1:] == build_datagram_header(7)[1:]


def test_flags_are_decoded_as_a_bitfield():
    header, _ = parse_datagram_header(bytes([0x8C, 0, 0, 0]))
    assert header.is_valid and not header.is_ack and not header.is_nak

    ack, _ = parse_datagram_header(bytes([0xC0, 0, 0, 0]))
    assert ack.is_ack and not ack.is_nak

    # The 0x20 bit is NAK only when IS_ACK is clear; with it set the same bit
    # announces a bandwidth figure instead.
    nak, _ = parse_datagram_header(bytes([0xA0, 0, 0, 0]))
    assert nak.is_nak and not nak.has_bandwidth_figure
    both, _ = parse_datagram_header(bytes([0xE0, 0, 0, 0]))
    assert both.is_ack and both.has_bandwidth_figure and not both.is_nak


def test_unconnected_first_byte_is_not_a_datagram():
    header, _ = parse_datagram_header(bytes([0x05, 0, 0, 0]))
    assert not header.is_valid


def test_real_ack_round_trips():
    raw = bytes.fromhex("c0000101000000")  # frame 6 of the capture
    header, _ = parse_datagram_header(raw)
    payload = parse_ack_payload(raw, 1, header.has_bandwidth_figure)
    assert payload.ranges == [AckRange(0, 0)]
    assert build_ack(payload.ranges) == raw


def test_ack_records_use_ranges_when_contiguous():
    encoded = build_ack(coalesce([4, 5, 6]))
    # count(2) + single-flag(1) + min(3) + max(3)
    assert len(encoded) == 1 + 2 + 1 + 3 + 3
    assert encoded[3] == 0, "a multi-value range is not flagged as single"


def test_coalesce_groups_runs_and_drops_duplicates():
    assert coalesce([3, 1, 2, 7, 3]) == [AckRange(1, 3), AckRange(7, 7)]
    assert coalesce([]) == []


def test_ack_payload_rejects_trailing_bytes():
    # A record count that under-describes the body means the layout is wrong;
    # silently ignoring the remainder would hide exactly that.
    with pytest.raises(ValueError, match="unread bytes"):
        parse_ack_payload(bytes.fromhex("c0000101000000ffff"), 1, False)


def test_inverted_range_is_rejected():
    with pytest.raises(ValueError, match="inverted"):
        AckRange(9, 2)
