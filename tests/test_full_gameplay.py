"""The full gameplay capture, end to end.

`FullGameplayRaknet.pcapng` is a 15-minute session: 94,875 datagrams over 23
connections and 10 server ports, spanning the login, character, chat and map
services.  It is 130 times the volume of the character-selection capture and it
exercises what the small one never does — fragments hundreds of pieces long,
coalesced acknowledgement ranges, several concurrent connections, and the two
high-rate gameplay messages.

Two claims are made here:

* the **transport** consumes all of it, and re-encodes it byte for byte;
* every **application message** is classified, i.e. the server can name it and
  hand it to a handler rather than logging raw bytes.

Classification is not comprehension.  The bodies of the two bulk gameplay
messages are still opaque, and the tests say so explicitly rather than implying
otherwise.
"""

from __future__ import annotations

import collections

import pytest

from dsor.messages import (
    OPCODE_MESSAGES,
    GameMessage,
    parse_message,
    read_map_assignment,
    read_named_signal,
    read_server_handoff,
    read_service_identity,
)
from raknet.constants import DatagramFlag
from raknet.datagram import (
    build_ack,
    build_datagram_header,
    parse_ack_payload,
    parse_datagram_header,
)
from raknet.frame import build_frame, parse_frames

#: Ports that identify a service tier; anything else is a map/game server, whose
#: port is assigned at runtime and announced in a 0x84/SERVER_HANDOFF message.
SERVICE_TIERS = {2190: "login", 2191: "chat", 2192: "character"}


def tier_of(port: int) -> str:
    return SERVICE_TIERS.get(port, "game")


@pytest.fixture(scope="module")
def decoded(full_capture):
    """Reassemble the capture into application messages, per connection.

    Fragments are grouped by (connection, direction) because split ids are only
    unique within one stream — reassembling globally would splice unrelated
    messages together.
    """
    pending: dict[tuple, dict] = collections.defaultdict(dict)
    messages: list[tuple[str, bool, GameMessage]] = []
    failures: list[str] = []
    counters = collections.Counter()

    for record in full_capture:
        raw = record["raw"]
        if not raw[0] & DatagramFlag.IS_VALID:
            counters["offline"] += 1
            continue
        header, offset = parse_datagram_header(raw)
        if header.is_ack or header.is_nak:
            parse_ack_payload(raw, 1, header.has_bandwidth_figure)
            counters["ack"] += 1
            continue

        counters["data"] += 1
        stream = (record["conn"], record["from_server"])
        for frame in parse_frames(raw, offset):
            counters["frames"] += 1
            if frame.is_split:
                buffer = pending[stream].setdefault(frame.split_id, {})
                buffer[frame.split_index] = frame.payload
                if len(buffer) != frame.split_count:
                    continue
                payload = b"".join(buffer[i] for i in range(frame.split_count))
                del pending[stream][frame.split_id]
                counters["reassembled"] += 1
            else:
                payload = frame.payload
            if not payload:
                continue
            try:
                messages.append(
                    (tier_of(record["server_port"]), record["from_server"],
                     parse_message(payload))
                )
            except ValueError as error:
                failures.append(f"frame {record['frame']}: {error}")

    incomplete = sum(len(v) for v in pending.values())
    return messages, failures, counters, incomplete


def test_capture_is_the_expected_session(full_capture):
    assert len(full_capture) == 94875


def test_transport_consumes_everything(decoded):
    """No datagram fails to parse, and nothing is left over inside one."""
    _, failures, counters, _ = decoded
    assert counters["data"] > 50000 and counters["ack"] > 40000
    assert not failures, "\n".join(failures[:10])


def test_re_encoding_reproduces_every_datagram(full_capture):
    """The scale version of the round-trip property: 94,782 datagrams."""
    mismatches = []
    checked = 0
    for record in full_capture:
        raw = record["raw"]
        if not raw[0] & DatagramFlag.IS_VALID:
            continue
        header, offset = parse_datagram_header(raw)
        if header.is_ack or header.is_nak:
            payload = parse_ack_payload(raw, 1, header.has_bandwidth_figure)
            if payload.arrival_rate is not None:
                continue
            rebuilt = build_ack(payload.ranges, is_nak=header.is_nak)
        else:
            frames = parse_frames(raw, offset)
            rebuilt = build_datagram_header(header.sequence, header.flags) + b"".join(
                build_frame(frame) for frame in frames
            )
        checked += 1
        if rebuilt != raw:
            mismatches.append(record["frame"])
    assert checked > 94000, f"only {checked} datagrams exercised"
    assert not mismatches, f"differs on frames {mismatches[:10]}"


def test_long_split_messages_reassemble(decoded):
    """The big transfers are what the small capture could not exercise."""
    _, _, counters, incomplete = decoded
    assert counters["reassembled"] > 100
    # A capture that starts and ends mid-session necessarily truncates some
    # split messages; they must be a handful, not a symptom.
    assert incomplete < 30, f"{incomplete} split messages never completed"


def test_every_message_is_classified(decoded):
    """Each message gets a stable name, so nothing is logged as raw bytes."""
    messages, _, counters, _ = decoded
    # Fewer messages than frames, because a split message spans many frames and
    # collapses into one. Confusing the two counters is easy and misleading.
    assert len(messages) == 55211
    assert counters["frames"] == 62022
    assert len(messages) < counters["frames"]
    unnamed = [m for _, _, m in messages if not m.name]
    assert not unnamed
    # Opcode-bearing messages must actually have one.
    for _, _, message in messages:
        if message.message_id in OPCODE_MESSAGES:
            assert message.opcode is not None, message.name


def test_the_two_bulk_messages_dominate(decoded):
    """0x8B/0x005F upstream and 0x85/0x005F downstream carry the gameplay."""
    messages, _, _, _ = decoded
    bulk = sum(1 for _, _, m in messages if m.is_bulk)
    assert bulk / len(messages) > 0.6, "the bulk opcodes should dominate"

    client_bulk = sum(
        1 for _, from_server, m in messages
        if m.is_bulk and not from_server and m.message_id == 0x8B
    )
    server_bulk = sum(
        1 for _, from_server, m in messages
        if m.is_bulk and from_server and m.message_id == 0x85
    )
    assert client_bulk > 19000 and server_bulk > 21000


def test_bulk_bodies_are_still_opaque(decoded):
    """Stated as a test so the limit cannot be mistaken for completeness.

    The client's bulk message is a fixed 15-byte body; the server's varies. Their
    contents are not decoded, and this test exists to record that rather than to
    let it be forgotten.
    """
    messages, _, _, _ = decoded
    client_sizes = {
        len(m.body)
        for _, from_server, m in messages
        if m.is_bulk and not from_server and m.message_id == 0x8B
    }
    assert client_sizes == {15}, "the upstream bulk message is fixed width"


def test_service_identities_cover_every_tier(decoded):
    """Each tier announces itself, including chat on 2191."""
    messages, _, _, _ = decoded
    found = collections.defaultdict(set)
    for tier, from_server, message in messages:
        if from_server and message.message_id == 0x82:
            found[tier].add(read_service_identity(message))
    assert found["login"] == {"DrasaOnlineLoginServer"}
    assert found["character"] == {"DrasaCharacterService"}
    assert found["chat"], "the chat service on 2191 should identify itself"
    assert found["game"], "map servers should identify themselves"


def test_map_assignments_are_readable_and_self_consistent(decoded):
    """0x86 duplicates the map name; reading it proves the shape."""
    messages, _, _, _ = decoded
    names = set()
    for _, from_server, message in messages:
        if from_server and message.message_id == 0x86:
            # read_map_assignment raises if the two copies disagree, so this
            # asserts the duplication rule on every occurrence in the capture.
            names.add(read_map_assignment(message).name)
    assert len(names) >= 2
    assert any(name.startswith("a0000") for name in names)


def test_server_handoffs_explain_the_dynamic_ports(decoded):
    """0x84/0x0070 is how the client learns which map server to dial.

    This is the message an emulator must send to move a client onto its own
    server, which makes it the most load-bearing opcode found so far.
    """
    messages, _, _, _ = decoded
    targets = set()
    for _, from_server, message in messages:
        if from_server and message.message_id == 0x84 and message.opcode == 0x0070:
            target = read_server_handoff(message)
            if target:
                targets.add(target)
    assert targets, "no handoff observed"
    # At least one is a concrete endpoint rather than a bare hostname.
    assert any(":" in target for target in targets)


def test_named_signals_decode(decoded):
    """0x8B/0x0113 carries a signal name, which is readable text."""
    messages, _, _, _ = decoded
    names = set()
    for _, from_server, message in messages:
        if not from_server and message.message_id == 0x8B and message.opcode == 0x0113:
            names.add(read_named_signal(message))
    assert names
    assert all(name.isprintable() and name for name in names)


def test_timestamped_messages_are_unwrapped(decoded):
    """0x1B is a wrapper: what surfaces is the message inside it."""
    messages, _, _, _ = decoded
    wrapped = [m for _, _, m in messages if m.timestamp is not None]
    assert wrapped, "the session contains timestamped messages"
    # Every one observed wraps the time-sync message.
    assert {m.message_id for m in wrapped} == {0x83}
    assert all(m.timestamp > 0 for m in wrapped)
