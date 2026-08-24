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

import pytest

import collections

import struct

import pytest

from dsor.gameplay import (
    CLIENT_MOVEMENT_SIZE,
    CLIENT_TRAILER,
    CLIENT_TRAILER_START,
    COMMAND_TERMINATOR,
    ClientMovement,
    ENTITY_SEPARATOR,
    MOVING,
    POSITION_TOLERANCE,
    Position,
    SPAWN_FROM_END,
    WORLD_SCALE,
    actor_id,
    decode_client_movement,
    decode_entity_update,
    decode_position,
    decode_property_update,
    encode_actor_vitals,
    encode_client_movement,
    encode_entity_group,
    encode_entity_group_message,
    encode_entity_update,
    encode_position,
    entity_index,
    find_action_events,
    monster_spawn,
    reposition_entity,
    ring_positions,
    split_entity_group,
    with_motion,
    with_spawn,
)
from dsor.combat import (
    ACTORS_LEFT_VICINITY,
    HIT,
    Hit,
    encode_hit,
    KILL,
    Kill,
    encode_actors_enter_vicinity,
    encode_actors_left_vicinity,
    encode_discard_monster,
    encode_kill,
)
from raknet.bitstream import BitReader
from dsor.recorded import first_command, hit_commands, combat_ready_mobs, entity_descriptions, mob_templates

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


# ── several entities in one update ───────────────────────────────────────────


def test_entities_are_chained_by_a_separator_and_not_counted():
    """The multi-entity form of 0x85/0x005F has no count field.

    Records are joined by two bytes that are the opcode itself, 0x005F
    little-endian. The sizes seen in a real session are what settle it: 42 bytes is
    two records, 64 is three and 152 is seven, each 20*n + 2*(n-1).
    """
    records = [bytes(range(20)), bytes(range(20, 40))]
    body = encode_entity_group(records)
    assert len(body) == 42
    assert body[20:22] == ENTITY_SEPARATOR
    assert split_entity_group(body) == records

    for count, size in ((2, 42), (3, 64), (7, 152)):
        assert len(encode_entity_group([bytes(20)] * count)) == size


def test_repositioning_an_entity_touches_only_its_position():
    """Rewriting anything else risks detaching the record from the entity the zone
    content declared, and an entity the client does not know is one it will not
    draw."""
    original = bytes.fromhex("d6e9a8feec0a008080c3160a000000000a0001 00".replace(" ", ""))
    moved = reposition_entity(original, Position(-10000, -344, 5800))
    assert decode_position(moved) == Position(-10000, -344, 5800)
    assert moved[6:] == original[6:], "everything after the position is untouched"
    assert entity_index(moved) == entity_index(original)


def test_a_record_of_the_wrong_length_is_refused():
    with pytest.raises(ValueError, match="20 bytes"):
        reposition_entity(bytes(19), Position(0, 0, 0))


def test_the_single_entity_form_is_the_group_form_with_one_record():
    """So the two encoders cannot drift apart."""
    template = bytes(range(20))
    position = Position(1, 2, 3)
    grouped = encode_entity_group_message([encode_position(position) + template[6:]])
    assert grouped == encode_entity_update(position, template)


def test_creatures_are_placed_around_the_player_at_its_own_elevation():
    """Ground height is not something this server knows, and every creature
    observed in the tutorial dungeon shared the player's elevation."""
    centre = Position(-10172, -344, 5888)
    ring = ring_positions(centre, 4, radius=600)
    assert len(ring) == 4
    assert all(p.elevation == centre.elevation for p in ring)
    assert all(round(centre.distance_to(p)) == 600 for p in ring)


def test_the_recorded_creatures_are_real_records_with_distinct_indices():
    """Eight of them, one per index the real session showed."""
    templates = mob_templates()
    assert len(templates) == 8
    assert all(len(t) == 20 for t in templates)
    indices = [entity_index(t) for t in templates]
    assert len(set(indices)) == len(indices), "each is a different entity"
    assert 0x15 not in indices, "none of them is the player"


def test_a_fresh_tick_is_stamped_without_disturbing_the_actor():
    """The real server re-announces every entity each tick with its counter
    advanced by ten to twelve; this repeated the recorded value for ever.

    What must not change is the actor id, since that is the only thing binding the
    record to an entity the client has agreed to draw.
    """
    template = mob_templates()[0]
    moved = with_motion(template, Position(-5000, -344, 2800), 12345, duration=18)

    assert actor_id(moved) == actor_id(template)
    assert decode_position(moved) == Position(-5000, -344, 2800)
    assert int.from_bytes(moved[9:13], "little") == 12345
    assert int.from_bytes(moved[13:15], "little") == 18
    assert moved[19] == COMMAND_TERMINATOR


def test_a_standing_entity_carries_duration_zero():
    """Which is what 620 of 621 observed updates of one creature did."""
    template = mob_templates()[0]
    still = with_motion(template, decode_position(template), 999)
    assert int.from_bytes(still[13:15], "little") == 0
    assert still[:6] == template[:6], "it did not move"


def test_the_tick_and_duration_wrap_rather_than_overflow():
    """A session long enough to exceed either field should not raise."""
    template = mob_templates()[0]
    wrapped = with_motion(template, Position(0, 0, 0), 2**32 + 5, duration=2**16 + 3)
    assert int.from_bytes(wrapped[9:13], "little") == 5
    assert int.from_bytes(wrapped[13:15], "little") == 3


def test_the_vitals_command_reproduces_the_real_bytes():
    """Seventeen bytes: current hit points as int64, the skill resource as a float,
    the actor id, the terminator."""
    real = bytes.fromhex("857b00eb00000000000000cdcc4c3e15000100ff")
    assert encode_actor_vitals(0xEB, 0.2, bytes.fromhex("15000100")) == real


def test_the_two_fields_are_health_and_a_spendable_resource():
    """Misread twice here, and each misreading looked reasonable.

    Byte 0 takes only three values across a session — 234, 235, 236 — so it read as a
    stat selector; then the pair read as a maximum and a current value. The client's
    observer vtable settles it: the first field's setter notifies
    OnHealthPointsChanged and the second's OnSkillResourceChanged.

    So the float falling by exactly 5.00 per cast and recovering at 0.2 was never
    health — it was mana or rage. Writing damage there drains the resource and leaves
    the health bar untouched, which is precisely what one round of testing showed.
    """
    built = encode_actor_vitals(200, 7.5, bytes.fromhex("15000100"))
    assert int.from_bytes(built[3:11], "little") == 200, "health, first"
    assert struct.unpack("<f", built[11:15])[0] == 7.5, "resource, second"
    assert built[15:19] == bytes.fromhex("15000100")
    assert built[19] == COMMAND_TERMINATOR


def test_a_vitals_update_needs_a_four_byte_actor():
    with pytest.raises(ValueError, match="actor id is 4 bytes"):
        encode_actor_vitals(60, 1.0, b"\x01\x02")


# ── where a creature stands ─────────────────────────────────────────────────


def test_a_creature_is_pinned_by_its_description_not_by_a_movement_update():
    """The finding that explains hundreds of ignored updates.

    A position update for a creature is discarded — the client applies those only to
    an actor it has bound to an entity — so the only position that takes effect is the
    one inside its own description, three floats in world units. All six recorded
    descriptions carry it 281 bytes from the end, at three different message sizes.
    """
    for description in entity_descriptions().values():
        x, elevation, y = monster_spawn(description)
        assert -100.0 < x < 0.0, "the tutorial dungeon's creatures sit here"
        assert elevation == 0.0
        assert 40.0 < y < 60.0


def test_placing_a_creature_changes_twelve_bytes_and_nothing_else():
    """The creature, its template and its handle stay the recorded ones."""
    description = entity_descriptions()[b"\x08\x00\x01\x00"]
    moved = with_spawn(description, -79.0, 0.0, 47.5)

    assert monster_spawn(moved) == (-79.0, 0.0, 47.5)
    assert len(moved) == len(description)
    offset = len(description) - SPAWN_FROM_END
    assert moved[:offset] == description[:offset]
    assert moved[offset + 12 :] == description[offset + 12 :]


def test_a_description_too_short_to_hold_a_position_is_refused():
    with pytest.raises(ValueError, match="at least"):
        monster_spawn(bytes(10))


def test_a_creature_has_two_positions_in_two_different_frames():
    """The distinction that made every distance bound fail, in both directions.

    A creature's description carries three floats, and its movement records carry
    three signed 16-bit fields — and they are **not** the same frame. Measured over a
    real session: the player's decoded trajectory runs z=46 down to z=21, while the
    descriptions put the creatures at z=52 to 55, an interval the player never enters.
    Compared against the descriptions, the player was never nearer than 18 units and
    was 30 away at the moment of every single attack, so a bound of 1.75 refused every
    blow and so did a bound of 6. Compared on the wire, the same player came within
    0.9 units.

    Compare wire against wire.
    """
    for record in combat_ready_mobs():
        wire = decode_position(record)
        described = monster_spawn(entity_descriptions()[actor_id(record)])

        # Same creature, and the two disagree by far more than rounding.
        assert abs(wire.x / WORLD_SCALE - described[0]) < 6.0, "x roughly agrees"
        assert abs(wire.y / WORLD_SCALE - described[2]) > 25.0, "the depth does not"


# ── the lifecycle commands, generated ────────────────────────────────────────


def test_discarding_a_creature_needs_no_recording():
    """Its body is empty — the actor id in the trailer is the whole message — which
    is what makes it the way to retire a creature no capture happens to contain a
    removal for."""
    built = encode_discard_monster(0x00010008)
    assert built == bytes.fromhex("852b000800010 0ff".replace(" ", ""))
    assert len(built) == 8, "id, command, actor, terminator"


def test_a_vicinity_announcement_is_a_count_then_that_many_actors():
    """Byte-aligned throughout, so no bit writer is needed and any set of actors can
    be announced in one message."""
    built = encode_actors_enter_vicinity([0x00010008, 0x0001000A], 0x00010015)
    assert len(built) == 3 + 4 + 8 + 5
    assert int.from_bytes(built[3:7], "little") == 2
    assert int.from_bytes(built[7:11], "little") == 0x00010008
    assert int.from_bytes(built[11:15], "little") == 0x0001000A
    assert built[-1] == 0xFF


def test_hiding_and_killing_are_different_commands():
    """The mistake that left creatures standing at zero health: 0x0073 sets an entity
    invisible, it does not kill. What kills is 0x006C, which carries the impulse the
    corpse is thrown with and whether the body is removed."""
    hide = encode_actors_left_vicinity([0x00010008], 0x00010015)
    assert int.from_bytes(hide[1:3], "little") == ACTORS_LEFT_VICINITY

    killed = encode_kill(Kill(victim=0x00010008, killer=0x00010015, despawn=True))
    assert int.from_bytes(killed[1:3], "little") == KILL
    assert killed != hide


def test_a_kill_ends_one_bit_short_of_its_last_byte():
    """Its despawn flag is a single bit, so the trailer and the terminator behind it
    are shifted — which is why this cannot be assembled from whole bytes."""
    killed = encode_kill(Kill(victim=0x00010008, killer=0x00010015, despawn=True))
    assert killed[-1] == 0x80, "the terminator's last bit, then seven of padding"


def test_a_generated_hit_is_the_same_size_as_the_real_one():
    """Independent confirmation of an eighteen-field layout with five booleans:
    generating one produces exactly the 68 bytes the leading command of a recorded
    hit batch occupies."""
    built = encode_hit(
        Hit(
            victim=0x00010008,
            attacker=0x00010015,
            damage=12,
            victim_health=48,
            victim_max_health=60,
            combat_value_owner=0x00010015,
            combat_value=12.0,
            tick=1234,
        )
    )
    assert len(built) == 68
    assert built[0] == 0x85
    assert int.from_bytes(built[1:3], "little") == HIT


def test_the_victims_health_travels_in_the_blow_which_is_why_replay_failed():
    """The client takes a creature's health from the hit and nowhere else.

    A recorded blow is the one that killed its creature, so it carries zero, and the
    creature dies on the spot by the unanimated path — the client logs "Victim ... is
    not alive or cannot receive" and then "received kill message twice" when the real
    death arrives behind it. Only a generated blow can leave a creature standing.
    """
    survives = encode_hit(
        Hit(
            victim=0x00010008,
            attacker=0x00010015,
            damage=12,
            victim_health=48,
            victim_max_health=60,
            combat_value_owner=0x00010015,
        )
    )
    dies = encode_hit(
        Hit(
            victim=0x00010008,
            attacker=0x00010015,
            damage=12,
            victim_health=0,
            victim_max_health=60,
            combat_value_owner=0x00010015,
        )
    )
    assert survives != dies, "the health left is what differs"
    assert len(survives) == len(dies)


def test_a_real_hit_decodes_sensibly_with_this_layout():
    """The check that should have come before the encoder, not after it.

    A correct total length proves nothing on its own — two adjacent fields swapped
    give the same 68 bytes. Reading a recorded blow back with this layout is what
    settles it, and every field lands on a plausible value: one damage type, three
    false booleans, the victim left at 0 of a maximum of 12, damage 11, and both actor
    ids the player's.

    It also caught a scale error, and settled the scale itself: a creature holds
    **twelve** points, confirmed in play. Serving 60 against a client that knows it has
    twelve is read as a heal and drawn as a floating +400, which is how this was found.

    A caution for whoever revisits this: hunting a plausible value through a
    bit-packed message finds mirages. This same message yields 44 by reading its 11 two
    bits late, and 24 and 48 by reading its 12 one and two bits late — every one of them
    looks like a health value, and one of them was briefly believed over the aligned
    read.
    """
    recorded = first_command(hit_commands()[b"\x08\x00\x01\x00"], b"\x08\x00\x01\x00")
    assert len(recorded) == 68

    reader = BitReader(recorded)
    assert reader.read_uint(8) == 0x85
    assert reader.read_uint(16) == HIT
    reader.read_uint(32)                     # tick
    assert reader.read_uint(32) == 1         # one damage type
    reader.read_uint(8)
    assert [reader.read_bool() for _ in range(3)] == [False, False, False]
    assert reader.read_uint(64) == 0, "the victim was left at zero"
    assert reader.read_uint(64) == 12, "out of twelve"
    assert reader.read_uint(32) == 0 and reader.read_uint(32) == 0   # shields
    assert reader.read_uint(32) == 11, "the blow did eleven"
    reader.read_uint(32)                     # kind
    player = int.from_bytes(b"\x15\x00\x01\x00", "little")
    assert reader.read_uint(32) == player, "the attacker"
    assert reader.read_uint(32) == player, "and whose floating number it is"


def test_heading_law_matches_the_measured_axes():
    """Heading is clockwise from +y, the only convention the capture supports.

    Fitted against 114 real movement pairs: this one lands within 3.2 degrees,
    every other axis convention is 73 degrees or worse. The two axis-aligned
    families in the capture are what pin it — heading 0 travels +y, heading 128
    travels -y.
    """
    from dsor.gameplay import HEADING_UNITS, heading_to

    assert heading_to(0, 1) == 0
    assert heading_to(0, -1) == HEADING_UNITS // 2
    assert heading_to(1, 0) == HEADING_UNITS // 4
    assert heading_to(-1, 0) == 3 * HEADING_UNITS // 4


def test_motion_stamps_speed_and_both_headings():
    """A walking record must carry a non-zero speed, or the client snaps.

    This is the bug the user saw as "ils se tp sur moi": position changing while
    byte 6 said the creature was standing. The same byte is what
    UpdateMovementAnimation reads, so a zero also meant no walk animation.
    """
    from dsor.gameplay import (
        HEADING_GOAL_OFFSET,
        HEADING_OFFSET,
        MOVE_SPEED_OFFSET,
        WALK_SPEED,
        Position,
        with_motion,
    )
    from dsor.recorded import combat_ready_mobs

    record = combat_ready_mobs()[0]
    out = with_motion(
        record, Position(10, 20, 30), 99, duration=18, speed=WALK_SPEED, heading=200
    )
    assert out[MOVE_SPEED_OFFSET] == WALK_SPEED
    assert out[HEADING_OFFSET] == out[HEADING_GOAL_OFFSET] == 200
    assert len(out) == len(record)
    # And a standing record keeps speed zero.
    still = with_motion(record, Position(10, 20, 30), 99, speed=0, heading=200)
    assert still[MOVE_SPEED_OFFSET] == 0
    assert still[13:15] == b"\x00\x00"


def test_chase_speed_is_a_walk_not_a_leap():
    """Wire units per game tick, not per update.

    The first chase stepped 60 units per 100 ms update. A real walk is about six
    units per 40 ms tick, so that was four times a player's run and read as a
    teleport. Guard the unit, because getting it wrong looks like a different bug.
    """
    from dsor.gameplay import WALK_UNITS_PER_TICK

    per_second = WALK_UNITS_PER_TICK * 1000 / 40
    assert 100 <= per_second <= 200


def test_a_movement_batch_can_carry_a_trailing_command():
    """The form every real skill command took.

    All 56 skill commands the real server sent in the long session ride behind the
    movement records of a 0x005F batch; none travelled alone. The records end
    byte-aligned on their terminator, so the appended command must start on a byte
    boundary with its own two-byte id, exactly as `ff 47 00` shows in the capture.
    """
    from dsor.combat import TargetSkill, encode_target_skill
    from dsor.gameplay import encode_entity_group_message
    from dsor.recorded import combat_ready_mobs

    skill = encode_target_skill(
        TargetSkill(attacker=0x00010008, target=0x00010015, skill_id=440)
    )
    batch = encode_entity_group_message([combat_ready_mobs()[0]], [skill])
    assert batch[:3] == b"\x85\x5f\x00"
    assert b"\xff\x47\x00" in batch
    # And the appended command is the real one, minus only its own message header.
    assert batch.endswith(skill[1:])


def test_a_drop_is_rewritten_where_the_creature_fell():
    """Only the actor and the position, and nothing else in the command.

    The recorded 0x002D carries the place a creature died in another session, which
    is why replayed loot lay somewhere unreachable. Its tail is not understood, so
    it is copied bit for bit rather than rebuilt — the lesson from a skill command
    that was 26 bytes of a 64-byte message.
    """
    from dsor.items import drop_actor, drop_position, item_drop, with_drop

    recorded = item_drop()
    assert drop_actor(recorded) == bytes([0x03, 0x00, 0x01, 0x00])
    assert drop_position(recorded) == pytest.approx((-30.33, 0.0, 54.03), abs=1e-4)

    moved = with_drop(recorded, bytes([0x41, 0x00, 0x01, 0x00]), (-1.5, 0.0, 2.25))
    assert len(moved) == len(recorded)
    assert drop_actor(moved) == bytes([0x41, 0x00, 0x01, 0x00])
    assert drop_position(moved) == pytest.approx((-1.5, 0.0, 2.25))
    # Everything outside those two fields is untouched.
    changed = {i for i, (a, b) in enumerate(zip(recorded, moved)) if a != b}
    assert changed <= {3} | set(range(92, 105)), sorted(changed)
