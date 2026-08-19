"""Shared fixtures.

The capture fixture is the backbone of this suite: 722 datagrams recorded from a
real Drakensang Online session, which every codec here is checked against.  Unit
tests pin down intent; the capture proves the intent matches reality.
"""

import gzip
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "character_selection.jsonl"
#: Two controlled experiments, reduced to just the gameplay messages they were
#: taken for: stand-still / walk / stand-still, and a second one with four hits
#: taken from an enemy. Small enough to commit, which matters because these are
#: the only evidence for the field layouts in dsor/gameplay.py.
GAMEPLAY_FIXTURE = FIXTURES / "gameplay_experiment.jsonl.gz"

#: A full 15-minute session: 94,875 datagrams over 23 connections and 10 ports,
#: stored gzipped because it is 30 MB as text. This is what exercises the codec
#: at scale — fragments hundreds of pieces long, ACK ranges, and every service.
FULL_FIXTURE = FIXTURES / "full_gameplay.jsonl.gz"

#: Ports the recorded server listened on, used to tell the two directions apart.
SERVER_PORTS = (2190, 2192)


@pytest.fixture(scope="session")
def capture():
    """Every datagram of the recorded session, in order."""
    if not FIXTURE.exists():  # pragma: no cover
        pytest.skip(f"capture fixture missing: {FIXTURE}")
    records = []
    for line in FIXTURE.read_text().splitlines():
        record = json.loads(line)
        record["raw"] = bytes.fromhex(record["hex"])
        records.append(record)
    return records


@pytest.fixture(scope="session")
def by_frame(capture):
    """The same datagrams, addressable by capture frame number."""
    return {record["frame"]: record["raw"] for record in capture}


def _load(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as handle:
        for line in handle:
            record = json.loads(line)
            record["raw"] = bytes.fromhex(record["hex"])
            yield record


@pytest.fixture(scope="session")
def full_capture():
    """Every datagram of the full gameplay session."""
    if not FULL_FIXTURE.exists():  # pragma: no cover
        pytest.skip(f"full capture fixture missing: {FULL_FIXTURE}")
    return list(_load(FULL_FIXTURE))


@pytest.fixture(scope="session")
def gameplay_capture():
    """The controlled-experiment messages: capture tag, frame, direction, body."""
    if not GAMEPLAY_FIXTURE.exists():  # pragma: no cover
        pytest.skip(f"gameplay fixture missing: {GAMEPLAY_FIXTURE}")
    with gzip.open(GAMEPLAY_FIXTURE, "rt") as handle:
        return [json.loads(line) for line in handle]
