"""The whole character-selection flow, driven by the recorded client, offline.

This is the harness that was missing. Every earlier defect in this flow was found
by changing the server, asking the user to run the real client, and reading a log —
one hypothesis per round trip, with a human in the loop each time. The capture
already contains every datagram the real client sent, so the flow can be replayed
into a Service in-process and the answers inspected without the game.

What it proves is that the *server's* side of the exchange is well-formed and
complete. It cannot judge whether a real client is satisfied by the contents; only
the client can. But every fault it does catch is one that would otherwise have cost
a manual test.

The contiguity check below is the reason this file exists. A sequenced message was
consuming two ordering indices, leaving holes at 4 and 5 with the roster at 6 — and
a peer cannot deliver anything past a hole, so the selection screen never appeared.
Nothing in the unit tests could see it, because it only shows up in a full flow.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dsor.messages import parse_message
from raknet.constants import DatagramFlag, Reliability
from raknet.datagram import parse_datagram_header
from raknet.frame import parse_frames
from server import Service

#: Where the reference captures live. From the environment, or ~/dso-capture, so the
#: path names no particular machine or user; the test skips when the file is absent,
#: which is what it does for anyone who has not recorded one.
CAPTURES = Path(os.environ.get("DSOR_CAPTURES", Path.home() / "dso-capture"))
CAPTURE = CAPTURES / "session-20260818-033440.jsonl"
CHARACTER_PORT = 2192
CLIENT = ("172.20.0.2", 51000)


class _Recorder:
    """Stands in for the service's socket, keeping what would have been sent."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendto(self, data: bytes, _to) -> int:
        self.sent.append(data)
        return len(data)

    def close(self) -> None:
        pass


def _client_datagrams(port: int) -> list[bytes]:
    """Every datagram the recorded client sent to *port*, in capture order."""
    out = []
    with CAPTURE.open() as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row["server_port"] == port and not row["from_server"]:
                out.append(bytes.fromhex(row["hex"]))
    return out


@pytest.fixture(scope="module")
def replayed() -> list[bytes]:
    """Drive a character service with the recorded client and keep its output."""
    if not CAPTURE.exists():  # pragma: no cover
        pytest.skip(f"reference capture missing: {CAPTURE}")

    service = Service(
        port=0, name="DrasaCharacterService", role="character", map_name="a0000_char"
    )
    service.socket.close()
    recorder = _Recorder()
    service.socket = recorder
    # In production the event schedule is held back for a few seconds so it cannot
    # reach the client while the character screen is still being built. Neither the
    # hold nor the rate is what this harness is testing, so both are removed.
    service.slow_rate = float("inf")
    service.slow_delay = 0.0

    for raw in _client_datagrams(CHARACTER_PORT):
        service.handle(raw, CLIENT)
        # Drain fully: the queue is paced in production, and a driver that under-
        # drains sees a server that answers the handshake and then goes quiet.
        service.flush(burst=100_000)
        service.flush_slow()
    return recorder.sent


def _connected(datagrams: list[bytes]) -> list[bytes]:
    """Only the connected datagrams.

    The offline handshake replies go out through the same socket, and reading one
    as though it had a datagram header yields a frame claiming a thousand bytes of
    payload — which is how this harness first failed against its own output.
    """
    return [raw for raw in datagrams if raw and raw[0] & DatagramFlag.IS_VALID]


def _ordered_stream(datagrams: list[bytes]) -> dict[int, bytes]:
    """Reassemble the RELIABLE_ORDERED messages, keyed by ordering index."""
    splits: dict[tuple[int, int], dict[int, bytes]] = {}
    stream: dict[int, bytes] = {}
    for raw in _connected(datagrams):
        header, offset = parse_datagram_header(raw)
        if header.is_ack or header.is_nak:
            continue
        for frame in parse_frames(raw, offset):
            if frame.split_count is not None:
                pieces = splits.setdefault((frame.split_id, frame.ordering_index), {})
                pieces[frame.split_index] = frame.payload
                if len(pieces) == frame.split_count:
                    stream[frame.ordering_index] = b"".join(
                        pieces[i] for i in range(frame.split_count)
                    )
            elif frame.reliability == Reliability.RELIABLE_ORDERED:
                stream.setdefault(frame.ordering_index, frame.payload)
    return stream


def _name(payload: bytes) -> str:
    message = parse_message(payload)
    if message.opcode is None:
        return f"{message.message_id:#04x}"
    return f"{message.message_id:#04x}/{message.opcode:#06x}"


# ── the check that would have caught the stall ───────────────────────────────


def test_the_ordered_stream_has_no_holes(replayed):
    """The one invariant a peer cannot recover from.

    RELIABLE_ORDERED delivery is contiguous by definition: a missing index stops
    everything behind it for good. So any gap here is a client frozen on whatever
    screen it had already reached, with no error and no timeout — which is exactly
    how this presented.
    """
    stream = _ordered_stream(replayed)
    assert stream, "the service answered nothing at all"
    indices = sorted(stream)
    assert indices == list(range(len(indices))), (
        f"holes in the ordered stream at "
        f"{sorted(set(range(max(indices) + 1)) - set(indices))}"
    )


def test_the_sequence_matches_the_real_service(replayed):
    """The messages the real character service sent, in its ordering-index order.

    Taken from the reference session, where the indices are 0 to 6. Frame order in
    a capture disagrees with this — several travel in one datagram and a split
    message's fragments interleave — so the ordering index is what this follows.
    """
    stream = _ordered_stream(replayed)
    assert [_name(stream[i]) for i in range(7)] == [
        "0x10",          # connection accepted
        "0x82",          # service identity
        "0x86",          # map assignment, a0000_char
        "0x88",          # bare signal
        "0x84/0x0087",   # the roster
        "0x84/0x001b",
        "0x84/0x00dd",   # the 717 KB account transfer
    ]


def test_the_roster_and_transfer_are_byte_identical_to_the_real_ones(replayed):
    """These are replays, so a mismatch means the wrong file or a mangled split."""
    stream = _ordered_stream(replayed)
    assert stream[4] == Path("dsor/data/character_list.bin").read_bytes()
    assert stream[6] == Path("dsor/data/character_enter_bulk.bin").read_bytes()


def test_the_transfer_reassembles_from_its_fragments(replayed):
    """It splits into hundreds of pieces; one lost index and it never completes."""
    stream = _ordered_stream(replayed)
    assert len(stream[6]) == 733_774


def test_the_clock_is_announced_before_the_roster(replayed):
    """The real service announces its clock between the ready signal and the
    roster, and the client adopts the value rather than being asked for one."""
    clocks = []
    for raw in _connected(replayed):
        header, offset = parse_datagram_header(raw)
        if header.is_ack or header.is_nak:
            continue
        for frame in parse_frames(raw, offset):
            if frame.split_count is None and frame.payload[:1] == b"\x1b":
                clocks.append(frame)
    assert clocks, "no time sync was sent at all"
    assert clocks[0].reliability == Reliability.UNRELIABLE_SEQUENCED


def test_the_account_transfer_is_started_exactly_once(replayed):
    """Answering a repeated ready signal with another 717 KB is self-defeating —
    six repeats inside one second were observed once.

    Identified by fragment count, not by split id and not by how often fragment
    zero appears. The flow legitimately contains three split messages: the transfer
    at 592 fragments and the two 0x010E answers at two fragments each. Two earlier
    versions of this test asserted the wrong invariant in two different ways —
    first reading the resend timer's retransmissions as new transfers, then reading
    the small 0x010E splits as copies of the big one.
    """
    fragments: dict[int, set[int]] = {}
    counts: dict[int, int] = {}
    for raw in _connected(replayed):
        header, offset = parse_datagram_header(raw)
        if header.is_ack or header.is_nak:
            continue
        for frame in parse_frames(raw, offset):
            if frame.split_count is not None:
                fragments.setdefault(frame.split_id, set()).add(frame.split_index)
                counts[frame.split_id] = frame.split_count

    big = [split for split, total in counts.items() if total > 100]
    assert len(big) == 1, f"the 717 KB transfer was started {len(big)} times"
    only = big[0]
    assert len(fragments[only]) == counts[only], (
        f"only {len(fragments[only])} distinct fragments of {counts[only]} were sent"
    )


def test_the_release_fires_on_the_client_s_play_click(replayed):
    """The recorded client clicks Play twice, so the replay exercises the release —
    the part no manual test could reach while this server was answering the wrong
    message.

    Ordering index 7 is the grant and 8 the empty handoff that sends the client back
    to the login server. 9 onward are the answers to its two 0x010B shop queries,
    which are answers to requests and not part of the release.
    """
    stream = _ordered_stream(replayed)
    assert _name(stream[7]) == "0x84/0x0087"
    assert parse_message(stream[7]).body[0] == 5, "operation 5, the grant"
    assert _name(stream[8]) == "0x84/0x0070"
    assert len(stream[8]) == 6, "the empty handoff"


def test_the_empty_handoff_ends_the_flow_and_releases_nothing_else(replayed):
    """The handoff that releases a client carries no address at all: the client
    returns to the login server for a destination rather than being sent straight
    to a map."""
    stream = _ordered_stream(replayed)
    release = parse_message(stream[8])
    assert release.strings in ([], [""]), (
        f"the releasing handoff must be empty, carried {release.strings!r}"
    )
    assert len(stream[8]) == 6, "id, opcode, a zero length and one trailer byte"
