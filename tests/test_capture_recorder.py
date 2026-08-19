"""The server's own wire recorder.

It exists so a failing session can be diffed against a reference capture instead
of being diagnosed one hypothesis per restart. That only works if what it writes
is readable by the same tools that read the reference, so that is what these tests
check — the format, not the plumbing.
"""

from __future__ import annotations

import json

from raknet.datagram import parse_datagram_header
from server import Capture

REFERENCE_FIELDS = {"frame", "from_server", "conn", "server_port", "hex"}
CLIENT = ("172.20.0.2", 64811)


def read(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_fields_match_a_reference_capture(tmp_path):
    """A missing or renamed field makes the recording undiffable, which is the
    only reason it is written at all."""
    path = tmp_path / "session.jsonl"
    capture = Capture(str(path))
    capture.record(bytes([0x84, 0, 0, 0]), 2192, CLIENT, from_server=True)
    capture.close()

    (row,) = read(path)
    assert set(row) == REFERENCE_FIELDS
    assert row["from_server"] is True
    assert row["conn"] == "172.20.0.2:64811"
    assert row["server_port"] == 2192
    assert bytes.fromhex(row["hex"]) == bytes([0x84, 0, 0, 0])


def test_frames_are_numbered_across_directions_and_ports(tmp_path):
    """One numbering for the whole session, as in the reference: the three tiers
    share a file so a handoff can be followed from one port to the next."""
    path = tmp_path / "session.jsonl"
    capture = Capture(str(path))
    capture.record(b"\x05", 2190, CLIENT, from_server=False)
    capture.record(b"\x06", 2190, CLIENT, from_server=True)
    capture.record(b"\x09", 2192, CLIENT, from_server=False)
    capture.close()

    rows = read(path)
    assert [r["frame"] for r in rows] == [1, 2, 3]
    assert [r["server_port"] for r in rows] == [2190, 2190, 2192]


def test_a_recorded_datagram_parses_with_the_transport_codec(tmp_path):
    """The recording keeps whole datagrams, headers included — not payloads."""
    path = tmp_path / "session.jsonl"
    capture = Capture(str(path))
    capture.record(bytes([0x80, 7, 0, 0, 0x60, 0, 8, 0x84]), 2192, CLIENT, from_server=True)
    capture.close()

    (row,) = read(path)
    header, offset = parse_datagram_header(bytes.fromhex(row["hex"]))
    assert header.sequence == 7
    assert offset == 4
