"""Conformance against a recording of the real server — the test that matters.

This suite has no access to the game client, so it uses the next best authority:
722 datagrams captured from a real session.  Those bytes were accepted by the
real client, which makes them a specification rather than a sample.

Three properties are checked, in increasing strength:

1. **Every datagram decodes completely.**  Parsing must consume the body exactly,
   with nothing left over.  In a protocol whose header widths vary by
   reliability, a leftover byte is the earliest and loudest signal that a width
   is wrong — far more sensitive than eyeballing a hex dump.

2. **Re-encoding reproduces the original bytes.**  If this codec can rebuild all
   714 connected datagrams byte for byte, then what it emits is what the real
   server emitted.  This is as close to "the client would accept it" as one can
   get without the client.

3. **The server's replies match, given the client's own packets.**  The capture
   contains both directions, so the recorded client messages can be fed in and
   the answers compared with what the real server actually sent.

What this cannot prove is timing, retransmission, or anything the recorded
session never exercised.  Format conformance is not acceptance — but all three
fatal bugs found in the previous implementation were format bugs.
"""

from __future__ import annotations

import pytest

from dsor.protocol import parse_service_identity
from raknet.constants import DatagramFlag, MessageID
from raknet.datagram import (
    build_ack,
    build_datagram_header,
    parse_ack_payload,
    parse_datagram_header,
)
from raknet.frame import build_frame, parse_frames

SERVER_PORTS = (2190, 2192)


def connected(capture):
    """Datagrams belonging to an established connection."""
    return [r for r in capture if r["raw"][0] & DatagramFlag.IS_VALID]


def test_capture_has_both_directions(capture):
    """A one-directional recording cannot validate a responder."""
    assert sum(1 for r in capture if r["from_server"]) > 0
    assert sum(1 for r in capture if not r["from_server"]) > 0
    assert len(capture) == 722


def test_every_datagram_decodes_with_nothing_left_over(capture):
    """Property 1. Failures name the frame so they can be looked up in Wireshark."""
    failures = []
    for record in connected(capture):
        raw = record["raw"]
        try:
            header, offset = parse_datagram_header(raw)
            if header.is_ack or header.is_nak:
                # parse_ack_payload raises if the record count under-describes
                # the body, which is the same leftover-bytes check.
                parse_ack_payload(raw, 1, header.has_bandwidth_figure)
            else:
                parse_frames(raw, offset)
        except Exception as error:  # noqa: BLE001 - reported, not swallowed
            failures.append(f"frame {record['frame']}: {type(error).__name__}: {error}")
    assert not failures, "\n".join(failures[:10])


def test_re_encoding_reproduces_every_byte(capture):
    """Property 2: the strongest statement available without the client."""
    mismatches = []
    checked = 0
    for record in connected(capture):
        raw = record["raw"]
        header, offset = parse_datagram_header(raw)
        if header.is_ack or header.is_nak:
            payload = parse_ack_payload(raw, 1, header.has_bandwidth_figure)
            if payload.arrival_rate is not None:
                continue  # bandwidth figures are not re-emitted by this server
            rebuilt = build_ack(payload.ranges, is_nak=header.is_nak)
        else:
            frames = parse_frames(raw, offset)
            rebuilt = build_datagram_header(header.sequence, header.flags) + b"".join(
                build_frame(frame) for frame in frames
            )
        checked += 1
        if rebuilt != raw:
            mismatches.append(record["frame"])
    assert checked > 700, f"only {checked} datagrams exercised"
    assert not mismatches, f"re-encoding differs on frames {mismatches[:10]}"


def test_handshake_replies_match_the_recorded_server(by_frame):
    """Property 3, for the four unconnected messages."""
    from raknet.address import SystemAddress
    from raknet.offline import (
        build_open_connection_reply_1,
        build_open_connection_reply_2,
        parse_open_connection_request_1,
        parse_open_connection_request_2,
    )

    guid = bytes.fromhex("00063fb2731c5c79")

    request_1 = parse_open_connection_request_1(by_frame[1])
    assert build_open_connection_reply_1(guid, request_1.mtu) == by_frame[2]

    request_2 = parse_open_connection_request_2(by_frame[3])
    # The observed client endpoint comes from the reply itself, because a capture
    # taken on the client cannot show what the server saw after NAT.
    from raknet.address import decode_address

    observed, _ = decode_address(by_frame[4], 25)
    assert (
        build_open_connection_reply_2(guid, observed, request_2.mtu) == by_frame[4]
    )


def test_acceptance_reply_matches_given_the_recorded_request(by_frame):
    """Property 3, for CONNECTION_REQUEST -> CONNECTION_REQUEST_ACCEPTED."""
    from raknet.address import SystemAddress, decode_address
    from raknet.connection import Connection

    _, offset = parse_datagram_header(by_frame[5])
    request = parse_frames(by_frame[5], offset)[0].payload

    _, offset = parse_datagram_header(by_frame[7])
    recorded_reply = parse_frames(by_frame[7], offset)[0].payload

    # Take the two things the reply reveals about the real server's environment:
    # the client endpoint it observed, and its own LAN address.
    observed_client, after = decode_address(recorded_reply, 1)
    system_index = int.from_bytes(recorded_reply[after : after + 2], "big")
    server_local, _ = decode_address(recorded_reply, after + 2)

    connection = Connection(
        remote=observed_client,
        local=server_local,
        server_guid=bytes.fromhex("00063fb2731c5c79"),
        mtu=1292,
    )
    connection.client_guid = request[1:9]
    built = connection.build_connection_request_accepted(
        client_timestamp=int.from_bytes(request[9:17], "big"),
        system_index=system_index,
        server_timestamp=int.from_bytes(recorded_reply[-8:], "big"),
    )
    assert built == recorded_reply


def test_observed_message_ids_are_all_named(capture):
    """Every message ID in the recording should be one we have a name for.

    An unnamed ID is not an error, but it is a gap in the protocol table, and
    this is where it becomes visible instead of staying in a prose document.
    """
    from dsor.protocol import DsoMessage

    known = {int(m) for m in MessageID} | {int(m) for m in DsoMessage}
    seen = set()
    for record in connected(capture):
        header, offset = parse_datagram_header(record["raw"])
        if header.is_ack or header.is_nak:
            continue
        for frame in parse_frames(record["raw"], offset):
            # For a split message only the first piece starts with the ID.
            if frame.split_index in (None, 0) and frame.message_id is not None:
                seen.add(frame.message_id)
    assert seen, "no message IDs found"
    assert not (seen - known), f"unnamed message IDs: {sorted(hex(i) for i in seen - known)}"


def test_service_identity_is_readable_from_the_capture(capture):
    """The 0x82 payload should decode to the service names, per port."""
    found = {}
    for record in connected(capture):
        header, offset = parse_datagram_header(record["raw"])
        if header.is_ack or header.is_nak or not record["from_server"]:
            continue
        for frame in parse_frames(record["raw"], offset):
            # message_id is only meaningful on a whole frame or the first piece
            # of a split one: a middle fragment starts with arbitrary payload
            # bytes, which here happened to include 0x82.
            if frame.split_index not in (None, 0):
                continue
            if frame.message_id == 0x82:
                found[record["src_port"]] = parse_service_identity(frame.payload)
    assert found == {2190: "DrasaOnlineLoginServer", 2192: "DrasaCharacterService"}


def test_datagram_sequences_start_at_zero_per_connection(capture):
    """Each connection numbers its datagrams from 0, independently."""
    firsts = {}
    for record in connected(capture):
        if not record["from_server"]:
            continue
        header, _ = parse_datagram_header(record["raw"])
        if header.is_ack or header.is_nak:
            continue
        firsts.setdefault(record["src_port"], header.sequence)
    assert set(firsts) == {2190, 2192}
    assert all(sequence == 0 for sequence in firsts.values())
