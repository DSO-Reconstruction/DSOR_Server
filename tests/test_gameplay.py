"""The gameplay message decoders, checked against two controlled experiments.

Capture A: stand still, walk, stand still.
Capture B: walk, wait, walk again in a different direction, take four hits, quit.

The two walks in B were roughly perpendicular, which is what separated the axes.
B's combat is what fixed the hit fraction. Neither could have come from reading
the binary.

Every assertion below is a measurement that was taken before it was written. Two
of them record hypotheses the data **refuted** — those exist so a plausible-
sounding reading is not reintroduced later by someone reasoning from a small
sample, which is exactly how both were arrived at in the first place.
"""

from __future__ import annotations

import collections

import pytest

from dsor.gameplay import (
    CLIENT_MOVEMENT_SIZE,
    CLIENT_TRAILER_START,
    CLIENT_TRAILER,
    MOVING,
    POSITION_TOLERANCE,
    ClientMovement,
    Position,
    decode_client_movement,
    decode_entity_update,
    decode_property_update,
    find_action_events,
    decode_position,
    encode_client_movement,
    encode_position,
)

CLIENT_MESSAGE = "0x8b/0x005f"
SERVER_MESSAGE = "0x85/0x005f"
HIT_MESSAGE = "0x85/0x007b"


# ── codec, on synthetic values ───────────────────────────────────────────────


def test_position_round_trips_including_negatives():
    """Real coordinates in these captures are negative, so signedness matters:
    read as unsigned, -10172 becomes 55364 and every comparison silently fails."""
    for position in (
        Position(-10172, -344, 5888),
        Position(0, 0, 0),
        Position(32767, -32768, 1),
    ):
        assert decode_position(encode_position(position)) == position


def test_position_is_six_bytes():
    assert len(encode_position(Position(1, 2, 3))) == 6


def test_position_read_past_the_end_is_refused():
    with pytest.raises(ValueError, match="position"):
        decode_position(b"\x00\x01\x02")


def test_unknown_move_flag_is_refused():
    """Only 0 and 0x40 were ever seen. Anything else means the layout shifted, and
    quietly treating it as "not moving" would hide that."""
    body = bytearray(encode_position(Position(1, 2, 3)) + bytes(9))
    body[6] = 0x20
    with pytest.raises(ValueError, match="move flag"):
        decode_client_movement(bytes(body))


def test_wrong_body_length_is_refused():
    with pytest.raises(ValueError, match="15 bytes"):
        decode_client_movement(bytes(14))


# ── against the real captures ───────────────────────────────────────────────


@pytest.fixture(scope="module")
def experiment(gameplay_capture):
    """The two captures, grouped by capture tag and message name."""
    grouped: dict[tuple[str, str], list] = collections.defaultdict(list)
    for record in gameplay_capture:
        grouped[(record["capture"], record["name"])].append(
            (record["frame"], bytes.fromhex(record["body"]))
        )
    return grouped


def client_messages(experiment, tag):
    return [
        (frame, body)
        for frame, body in experiment[(tag, CLIENT_MESSAGE)]
        if len(body) == CLIENT_MOVEMENT_SIZE
    ]


def test_capture_has_both_experiments(experiment):
    assert len(client_messages(experiment, "A")) == 1740
    assert len(client_messages(experiment, "B")) == 249
    # Only capture B contains combat.
    assert len(experiment[("B", HIT_MESSAGE)]) == 8
    assert not experiment[("A", HIT_MESSAGE)]


@pytest.mark.parametrize("tag", ["A", "B"])
def test_every_real_message_round_trips(experiment, tag):
    """The strongest available check on the field layout: if the decoder can put
    every byte back, it has not misplaced a boundary."""
    for frame, body in client_messages(experiment, tag):
        assert encode_client_movement(decode_client_movement(body)) == body, frame


@pytest.mark.parametrize("tag", ["A", "B"])
def test_a_still_character_never_changes_position(experiment, tag):
    """The invariant that identifies byte 6, measured at zero violations across
    1,855 consecutive pairs in the two captures.

    It is one-directional on purpose: the flag being set does *not* imply the
    position changed — short bursts were observed with the flag on and no
    movement, presumably a click that resolved to where the character already
    stood.
    """
    messages = client_messages(experiment, tag)
    pairs = violations = 0
    for (_, before), (frame, after) in zip(messages, messages[1:]):
        current = decode_client_movement(after)
        if current.moving:
            continue
        pairs += 1
        if decode_client_movement(before).position != current.position:
            violations += 1
    assert pairs > 100
    assert violations == 0


@pytest.mark.parametrize("tag", ["A", "B"])
def test_move_flag_takes_only_two_values(experiment, tag):
    seen = {body[6] for _, body in client_messages(experiment, tag)}
    assert seen == {0, MOVING}


@pytest.mark.parametrize("tag", ["A", "B"])
def test_trailer_is_constant(experiment, tag):
    """Bytes 12..14 only. Bytes 10 and 11 also hold one value in these two short
    captures, which is exactly why they were wrongly modelled as part of the
    trailer; a longer session shows both varying. See the cross-validation test."""
    movements = [decode_client_movement(body) for _, body in client_messages(experiment, tag)]
    assert {m.trailer for m in movements} == {CLIENT_TRAILER}
    assert CLIENT_TRAILER_START == 12


@pytest.mark.parametrize("tag", ["A", "B"])
def test_tick_advances_even_while_standing_still(experiment, tag):
    """Byte 9 is a clock, which is why it cannot be a movement counter.

    Not asserted as an unbroken rule: capture A has exactly one step outside the
    range, out of 1,739. A lost or reordered message explains one; asserting
    perfection here would make the suite fail on a capture taken over a worse
    connection.
    """
    messages = client_messages(experiment, tag)
    ticks = [decode_client_movement(body).tick for _, body in messages]
    steps = [(b - a) % 256 for a, b in zip(ticks, ticks[1:])]
    in_range = sum(1 for step in steps if 1 <= step <= 20)
    assert in_range / len(steps) > 0.99

    still = [
        decode_client_movement(body)
        for _, body in messages
        if not decode_client_movement(body).moving
    ]
    # The clock keeps moving while the character does not.
    assert len({m.tick for m in still}) > 50


def test_the_four_hits_step_the_fraction_by_a_fifth(experiment):
    """Capture B's combat: four hits, two messages each, stepping by exactly 0.2.

    This is the single clearest result of the whole exercise, and it exists only
    because the hits were counted while they happened.
    """
    updates = [decode_property_update(body) for _, body in experiment[("B", HIT_MESSAGE)]]
    fractions = [u.value for u in updates]
    assert len(fractions) == 8
    # Two properties alternate, one message each per hit.
    assert {u.property_id for u in updates} == {0xEB, 0xEC}
    assert fractions == pytest.approx([0.2, 0.2, 0.4, 0.4, 0.6, 0.6, 0.8, 0.8])

    distinct = sorted(set(round(value, 3) for value in fractions))
    assert distinct == [0.2, 0.4, 0.6, 0.8]
    steps = {round(b - a, 3) for a, b in zip(distinct, distinct[1:])}
    assert steps == {0.2}


@pytest.mark.parametrize("tag", ["A", "B"])
def test_server_updates_decode_to_a_plausible_position(experiment, tag):
    """The server uses the same position layout, which is what lets one codec
    serve both directions."""
    updates = [
        decode_entity_update(body)
        for _, body in experiment[(tag, SERVER_MESSAGE)]
        if len(body) >= 20
    ]
    assert len(updates) > 500
    # These two sessions stayed on one patch of flat ground, so elevation barely
    # moved. That is a property of the experiment, not of the protocol: over a
    # 15-minute session the same field takes many values, so this threshold is
    # deliberately scoped to captures A and B.
    elevations = collections.Counter(u.position.elevation for u in updates)
    assert elevations.most_common(1)[0][1] / len(updates) > 0.9


def test_server_position_tracks_the_client_but_is_not_identical(experiment):
    """Refuted hypothesis, kept as a test.

    Two hand-picked messages matched the client exactly and that was read as the
    server echoing the position verbatim. Over the whole capture only a small
    fraction match exactly, while nearly all sit within a few tens of units —
    ordinary authority lag. Asserting equality would fail on any real session.
    """
    client = client_messages(experiment, "B")
    frames = [frame for frame, _ in client]
    exact = near = total = 0
    for frame, body in experiment[("B", SERVER_MESSAGE)]:
        if len(body) < 20:
            continue
        index = _last_at_or_before(frames, frame)
        if index < 1:
            continue
        current = decode_client_movement(client[index][1])
        previous = decode_client_movement(client[index - 1][1])
        if current.moving or current.position != previous.position:
            continue  # only compare while the character is standing still
        total += 1
        server = decode_entity_update(body).position
        if server == current.position:
            exact += 1
        elif (
            abs(server.x - current.position.x) <= POSITION_TOLERANCE
            and abs(server.y - current.position.y) <= POSITION_TOLERANCE
            and server.elevation == current.position.elevation
        ):
            near += 1

    assert total > 100
    assert exact < total // 2, "exact equality was the refuted reading"
    assert (exact + near) / total > 0.7


def test_direction_bytes_are_not_a_duplicated_heading(experiment):
    """The other refuted hypothesis.

    Bytes 7 and 8 were equal in the first burst examined, which suggested one
    heading written twice. Across both captures they agree only about half the
    time while moving, so they are two related but distinct quantities.
    """
    for tag in ("A", "B"):
        moving = [
            decode_client_movement(body)
            for _, body in client_messages(experiment, tag)
            if decode_client_movement(body).moving
        ]
        assert moving
        equal = sum(1 for m in moving if m.direction[0] == m.direction[1])
        assert equal < len(moving), f"{tag}: they are not always equal"
        assert equal > 0, f"{tag}: nor are they always different"


def test_direction_differs_between_two_different_walks(experiment):
    """What the direction bytes *do* show: two walks, two clearly separated values.

    Capture B's two bursts went in different directions, and the dominant
    direction byte differs between them by far more than its jitter within a
    burst.
    """
    messages = client_messages(experiment, "B")
    bursts: list[list[ClientMovement]] = []
    current: list[ClientMovement] = []
    for _, body in messages:
        decoded = decode_client_movement(body)
        if decoded.moving:
            current.append(decoded)
        elif current:
            bursts.append(current)
            current = []
    if current:
        bursts.append(current)

    long_bursts = [b for b in bursts if len(b) >= 10]
    assert len(long_bursts) >= 2
    dominant = [
        collections.Counter(m.direction[0] for m in burst).most_common(1)[0][0]
        for burst in long_bursts
    ]
    assert max(dominant) - min(dominant) > 30


def _last_at_or_before(frames: list[int], frame: int) -> int:
    """Index of the newest entry at or before *frame*, or -1."""
    import bisect

    return bisect.bisect_right(frames, frame) - 1


# ── events ──────────────────────────────────────────────────────────────────


def _timeline(experiment, tag):
    """Server messages of one capture, in frame order, as find_action_events wants."""
    rows = []
    for (capture, name), entries in experiment.items():
        if capture != tag or not name.startswith("0x85/"):
            continue
        rows += [(frame, name, body) for frame, body in entries]
    rows.sort(key=lambda row: row[0])
    return rows


def test_the_four_hits_each_have_one_event_and_two_property_updates(experiment):
    """Ground truth: four hits were taken, and they were counted while they happened.

    Four of the five events in this capture carry exactly two property updates,
    and their values are the 0.2 steps. The fifth carries none — about a fifth of
    events have no updates, in this capture and in a held-out one alike.
    """
    events = find_action_events(_timeline(experiment, "B"))
    assert len(events) == 5

    with_updates = [event for event in events if event.properties]
    assert len(with_updates) == 4
    assert all(len(event.properties) == 2 for event in with_updates)

    values = sorted({round(u.value, 3) for e in with_updates for u in e.properties})
    assert values == [0.2, 0.4, 0.6, 0.8]
    # Each hit changed the same two properties.
    assert all(e.changed_properties == {0xEB, 0xEC} for e in with_updates)


def test_events_are_ordered_and_carry_their_own_body(experiment):
    events = find_action_events(_timeline(experiment, "B"))
    frames = [event.frame for event in events]
    assert frames == sorted(frames)
    assert all(len(event.body) > 20 for event in events)


def test_an_event_with_no_updates_is_still_reported(experiment):
    """Dropping them would hide that a fifth of events have no visible effect."""
    events = find_action_events(_timeline(experiment, "B"))
    assert any(not event.properties for event in events)
