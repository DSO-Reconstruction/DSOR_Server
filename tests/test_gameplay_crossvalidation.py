"""The gameplay decoders against a session they were not derived from.

`dsor/gameplay.py` was written from two short controlled experiments. This module
runs the same claims against the 15-minute session in `full_gameplay.jsonl.gz` —
a different account, a different day, 19,270 client messages instead of 1,989.

It exists because the first version of that model was wrong in four places, and
every one of them was invisible in the captures it came from:

* bytes 10 and 11 held a single value in both experiments and were modelled as
  part of a fixed trailer; here byte 11 takes 0, 4, 7, 65 and 50;
* "the move flag is 0 implies the position is frozen" had zero exceptions in the
  experiments and has 17 here;
* elevation was effectively constant, because the player never left one patch of
  flat ground;
* the float32 in a property update was read as a 0..1 fraction, because the only
  property observed happened to use that scale.

A held-out session is the only thing that catches that class of error, so it is a
test rather than a note.
"""

from __future__ import annotations

import collections

import pytest

from dsor.gameplay import (
    CLIENT_MOVEMENT_SIZE,
    CLIENT_TRAILER,
    MOVING,
    decode_client_movement,
    decode_entity_update,
    decode_property_update,
    encode_client_movement,
)
from dsor.messages import parse_message
from raknet.constants import DatagramFlag
from raknet.datagram import parse_datagram_header
from raknet.frame import parse_frames


@pytest.fixture(scope="module")
def messages(full_capture):
    """Application messages of the held-out session, reassembled per stream."""
    pending: dict[tuple, dict] = collections.defaultdict(dict)
    out: dict[str, list] = collections.defaultdict(list)
    for record in full_capture:
        raw = record["raw"]
        if not raw[0] & DatagramFlag.IS_VALID:
            continue
        header, offset = parse_datagram_header(raw)
        if header.is_ack or header.is_nak:
            continue
        stream = (record["conn"], record["from_server"])
        for frame in parse_frames(raw, offset):
            if frame.is_split:
                buffer = pending[stream].setdefault(frame.split_id, {})
                buffer[frame.split_index] = frame.payload
                if len(buffer) != frame.split_count:
                    continue
                payload = b"".join(buffer[i] for i in range(frame.split_count))
                del pending[stream][frame.split_id]
            else:
                payload = frame.payload
            if not payload:
                continue
            out[parse_message(payload).name].append(payload[3:])
    return out


@pytest.fixture(scope="module")
def client_movements(messages):
    return [b for b in messages["0x8b/0x005f"] if len(b) == CLIENT_MOVEMENT_SIZE]


def test_the_session_is_the_held_out_one(client_movements):
    assert len(client_movements) == 19270


def test_every_message_round_trips(client_movements):
    """The claim that generalised. If the field boundaries were wrong anywhere,
    19,270 messages from an unrelated session would not re-encode exactly."""
    for body in client_movements:
        assert encode_client_movement(decode_client_movement(body)) == body


def test_move_flag_still_takes_only_two_values(client_movements):
    assert {body[6] for body in client_movements} == {0, MOVING}


def test_constant_trailer_holds(client_movements):
    """Bytes 12..14 are the same three values here as in the experiments."""
    assert {decode_client_movement(b).trailer for b in client_movements} == {CLIENT_TRAILER}


def test_the_bytes_before_the_trailer_do_vary(client_movements):
    """The correction, asserted so it cannot be undone.

    A future reader looking only at a short capture would see bytes 10 and 11
    frozen and be tempted to fold them into the trailer. This session says they
    are fields.
    """
    movements = [decode_client_movement(b) for b in client_movements]
    assert len({m.unknown for m in movements}) > 3, "byte 11 varies"
    assert len({m.counter for m in movements}) > 3, "byte 10 varies"


def test_a_still_character_almost_never_changes_position(client_movements):
    """The invariant, restated as the rate it actually has.

    Zero exceptions in the short experiments, 17 in 7,794 pairs here. Asserting
    perfection would fail on any long session; asserting nothing would lose the
    signal that identifies byte 6.
    """
    pairs = violations = 0
    for before, after in zip(client_movements, client_movements[1:]):
        current = decode_client_movement(after)
        if current.moving:
            continue
        pairs += 1
        if decode_client_movement(before).position != current.position:
            violations += 1
    assert pairs > 5000
    assert violations / pairs < 0.01


def test_tick_still_behaves_like_a_clock(client_movements):
    ticks = [decode_client_movement(b).tick for b in client_movements]
    steps = [(b - a) % 256 for a, b in zip(ticks, ticks[1:])]
    assert sum(1 for s in steps if 1 <= s <= 20) / len(steps) > 0.99


def test_elevation_is_not_constant_over_a_long_session(messages):
    """The over-fitted claim, inverted.

    In the experiments one elevation covered more than 90% of updates. Here the
    player crossed real terrain, so the same field takes many values — which is
    what an elevation should do, and confirms it is not a constant of the format.
    """
    updates = [decode_entity_update(b) for b in messages["0x85/0x005f"] if len(b) >= 20]
    assert len(updates) > 10000
    elevations = collections.Counter(u.position.elevation for u in updates)
    assert len(elevations) > 50
    assert elevations.most_common(1)[0][1] / len(updates) < 0.5


def test_property_updates_use_more_than_one_scale(messages):
    """Why the float needs its identifier.

    This session's dominant property runs on a 0..100 scale in steps of 8; the
    combat experiment's ran 0..1 in steps of 0.2. Reading the float without the
    identifier produces a number with no units.
    """
    updates = [
        decode_property_update(b) for b in messages["0x85/0x007b"] if len(b) >= 12
    ]
    assert len(updates) > 500
    by_property: dict[int, set[float]] = collections.defaultdict(set)
    for update in updates:
        by_property[update.property_id].add(round(update.value, 3))

    # The property seen most often here reaches values far above 1.
    biggest = max(by_property.items(), key=lambda item: len(item[1]))
    assert max(biggest[1]) > 1.0
    assert len(by_property) > 1, "several distinct properties share this message"
