"""The map tier, driven by the recorded client, offline.

The character tier got a harness like this early and it paid immediately: two
comparisons found two transport faults that had each cost a round trip of guess,
restart, ask-the-human. The map tier had none, and the difference showed — a dozen
hypotheses about creatures were tested by changing the server and asking someone to
look at their screen, which is slow and answers only the question you thought to ask.

This drives a real map Service with every datagram the recorded client sent, in
process, and compares what comes back against what the real server sent in the same
session. The reference is a capture in which six creatures approached, were fought,
died and dropped an item — so it contains the whole exchange, not a fragment.

What it can prove is that the server's side matches the real one, message by message
and in ordering-index order. What it cannot prove is that a client would be satisfied
by the contents; only a client can. But every fault it catches is one nobody has to
sit and watch for.
"""

from __future__ import annotations

import collections
import json
from pathlib import Path

import pytest

from dsor.messages import parse_message
from raknet.constants import DatagramFlag, Reliability
from raknet.datagram import parse_datagram_header
from raknet.frame import parse_frames
from server import Service

#: The session six creatures were killed in. Its map port is dynamic, as every map
#: server's is, so it is discovered rather than named.
CAPTURE = Path("/home/lej/dso-capture/session-20260820-012227.jsonl")
MAP_NAME = "a0001_start_tutorial_dun"
CLIENT = ("172.20.0.2", 51000)


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendto(self, data: bytes, _to) -> int:
        self.sent.append(data)
        return len(data)

    def close(self) -> None:
        pass


def _rows() -> list[dict]:
    if not CAPTURE.exists():  # pragma: no cover
        pytest.skip(f"reference capture missing: {CAPTURE}")
    out = []
    with CAPTURE.open() as handle:
        for line in handle:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _map_port(rows: list[dict]) -> int:
    """The busiest high port, which is where the map server was."""
    seen = collections.Counter(
        row["server_port"] for row in rows if row["server_port"] > 3000
    )
    if not seen:  # pragma: no cover
        pytest.skip("no map port in the capture")
    return seen.most_common(1)[0][0]


def _connected(datagrams: list[bytes]) -> list[bytes]:
    """Only connected datagrams: an offline reply read as one yields nonsense."""
    return [raw for raw in datagrams if raw and raw[0] & DatagramFlag.IS_VALID]


def _ordered_stream(datagrams: list[bytes]) -> dict[int, bytes]:
    """Reassemble the RELIABLE_ORDERED messages, keyed by ordering index."""
    splits: dict[tuple[int, int], dict[int, bytes]] = {}
    stream: dict[int, bytes] = {}
    for raw in _connected(datagrams):
        header, offset = parse_datagram_header(raw)
        if header.is_ack or header.is_nak:
            continue
        try:
            frames = parse_frames(raw, offset)
        except ValueError:
            continue
        for frame in frames:
            if frame.split_count is not None:
                pieces = splits.setdefault((frame.split_id, frame.ordering_index), {})
                pieces[frame.split_index] = frame.payload
                if len(pieces) == frame.split_count:
                    stream.setdefault(
                        frame.ordering_index,
                        b"".join(pieces[i] for i in range(frame.split_count)),
                    )
            elif frame.reliability == Reliability.RELIABLE_ORDERED:
                stream.setdefault(frame.ordering_index, frame.payload)
    return stream


def _name(payload: bytes) -> str:
    message = parse_message(payload)
    if message.opcode is None:
        return f"{message.message_id:#04x}"
    return f"{message.message_id:#04x}/{message.opcode:#06x}"


@pytest.fixture(scope="module")
def replayed():
    """Drive a map service with the recorded client, and keep both sides' output."""
    rows = _rows()
    port = _map_port(rows)

    service = Service(port=0, name="DrasaOnlineMapServer", role="map", map_name=MAP_NAME)
    service.socket.close()
    recorder = _Recorder()
    service.socket = recorder
    service.mobs = 5
    service.mob_first_command = True

    real: list[bytes] = []
    for row in rows:
        if row["server_port"] != port:
            continue
        raw = bytes.fromhex(row["hex"])
        if row["from_server"]:
            real.append(raw)
            continue
        service.handle(raw, CLIENT)
        # The tick pairs come from the main loop, not from handling a datagram, so a
        # harness that only handles datagrams sees one tick and the real server's
        # 2455. Driving it here is what makes the tick stream comparable at all.
        service.game_tick()
        service.flush(burst=100_000)
        service.flush_slow()
    return _ordered_stream(recorder.sent), _ordered_stream(real), recorder.sent


# ── the entry sequence, which is deterministic ───────────────────────────────


def test_the_entry_sequence_matches_the_real_map_server(replayed):
    """Ordering indices 0 to 8, the part that does not depend on timing.

    Frame order in the capture disagrees with this — several of these share a
    datagram and a 631 KB message's fragments interleave with everything else — so
    the ordering index is what this follows.
    """
    mine, real, _ = replayed
    expected = [
        "0x10",          # connection accepted
        "0x82",          # service identity
        "0x86",          # the map name, twice
        "0x88",
        "0x84/0x001b",
        "0x85/0x0114",   # the cosmetics table
        "0x85/0x001d",   # the zone content, 631 KB
        "0x85/0x00a7",
        "0x85/0x0074",
    ]
    assert [_name(real[i]) for i in range(9)] == expected, "the reference itself"
    assert [_name(mine[i]) for i in range(9)] == expected


def test_the_map_assignment_is_byte_identical(replayed):
    """The name written twice plus a trailer; a client validates the two copies."""
    mine, real, _ = replayed
    assert mine[2] == real[2]


def test_the_ordered_stream_has_no_holes(replayed):
    """The one invariant a peer cannot recover from: a missing index stops
    everything behind it for good, with no error and no timeout."""
    mine, _, _ = replayed
    indices = sorted(mine)
    assert indices == list(range(len(indices))), (
        f"holes at {sorted(set(range(max(indices) + 1)) - set(indices))}"
    )


# ── what the diff against the real server found ──────────────────────────────


def test_the_creature_health_message_is_gone(replayed):
    """Both captured sessions carry 154 ActorStatsUpdateCommands between them and
    every one targets the player, including the session where six creatures died.

    This server sent a baseline for each creature anyway. It was noise the real one
    never emits, and the client ignored it — a creature's health cannot be reported
    because there is no message for it.
    """
    _, _, mine_raw = replayed
    stats = []
    for raw in _connected(mine_raw):
        header, offset = parse_datagram_header(raw)
        if header.is_ack or header.is_nak:
            continue
        for frame in parse_frames(raw, offset):
            if frame.split_count is not None:
                continue
            message = parse_message(frame.payload)
            if message.message_id == 0x85 and message.opcode == 0x007B:
                stats.append(message.body[12:16])

    assert all(actor == b"\x15\x00\x01\x00" for actor in stats), (
        "only the player's health is ever reported"
    )
