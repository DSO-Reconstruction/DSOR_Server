"""A frame's length is in bits, and rounding it up to the byte is not harmless.

The client reads a frame to exactly the boundary its length field gives. RakNet's
BitStream packs booleans into single bits, so a message's true length is usually not a
multiple of eight -- and the bytes are padded up without the length following them.

Every message this server sent declared ``len(payload) * 8``. Measured against the
live service on the same map:

    HitCommand   0x006b   98 of 98 frames declare a sub-byte length
    StatusEffect 0x004f   90 of 91
    ItemUpdate   0x0030   27 of 30
    LocationInfo 0x003e   14 of 14
    this server           0 of 936, across every command it sends

The spare bits sit at the end of the frame, and a multi-command payload is decoded
command after command until its stream runs out -- so the client reads them as the
start of one more command:

    DecodeCommand() invalid command ending in multi command 79!

which is 0x4F. And 40 of the 44 replayed blobs that could be located in the captures
are sub-byte, creature descriptions among them.
"""

import pytest

from raknet.bitstream import BitWriter
from raknet.payload import Payload, bits_of, respan


def test_a_bit_writer_hands_back_its_own_length():
    writer = BitWriter()
    writer.write_uint(0x85, 8)
    writer.write_bool(True)
    writer.write_uint(7, 3)
    out = writer.to_bytes()
    assert len(out) == 2
    assert bits_of(out) == 12, "not 16"


def test_a_payload_is_bytes_everywhere_it_matters():
    """The whole reason this is a bytes subclass rather than a wrapper."""
    p = Payload(b"abcd", 30)
    assert p == b"abcd"
    assert len(p) == 4
    assert p[1:3] == b"bc"
    assert bytes(p) == b"abcd"
    assert bits_of(b"abcd") == 32, "plain bytes fall back to len * 8"
    with pytest.raises(ValueError):
        Payload(b"abcd", 40)


def test_respan_follows_a_splice():
    original = Payload(b"abcdef", 45)
    assert bits_of(respan(original, b"abcdefgh")) == 45 + 16
    assert bits_of(respan(original, b"abcd")) == 45 - 16


def test_a_frame_declares_the_payload_s_own_length():
    from raknet.connection import Connection

    connection = Connection(
        remote=("127.0.0.1", 1),
        local=("127.0.0.1", 30000),
        server_guid=1,
        mtu=1400,
    )
    frames = connection.frames_for(Payload(b"\x85\x4f\x00\x00" * 4, 125))
    assert len(frames) == 1
    assert frames[0].bit_length == 125

    plain = connection.frames_for(b"\x85\x4f\x00\x00")
    assert plain[0].bit_length == 32, "plain bytes still declare len * 8"


def test_the_hit_and_the_effect_declare_sub_byte_lengths():
    """The two commands the live service never sends byte-aligned."""
    from dsor import effects
    from dsor.combat import Hit, encode_hit
    from dsor.recorded import status_effects_message

    hit = encode_hit(
        Hit(
            victim=0x10086,
            attacker=0x10015,
            damage=8400,
            victim_health=4991600,
            victim_max_health=5000000,
            combat_value_owner=0x10015,
            combat_value=0.0,
            tick=41230,
        )
    )
    assert bits_of(hit) % 8, f"{bits_of(hit)} is byte-aligned"
    assert bits_of(hit) < len(hit) * 8

    state = status_effects_message(
        [(effects.wire_of("debuff_cc_stun"), [0.0] * 5, 41230, 5.0, 66049)],
        b"\x86\x00\x01\x00",
        source=b"\x86\x00\x01\x00",
    )
    assert bits_of(state) % 8, f"{bits_of(state)} is byte-aligned"


def test_the_effect_message_declares_exactly_what_its_grammar_uses():
    """No spare bits at all, which is the point."""
    from dsor import effects
    from dsor.recorded import status_effects_message, walk_elements

    state = status_effects_message(
        [
            (effects.wire_of("debuff_cc_stun"), [0.0] * 5, 41230, 5.0, 66049),
            (
                effects.wire_of("skill_seismicslam_debuff_armor"),
                [-0.5, 0.0, 0.0, 0.0, 0.0],
                41230,
                5.0,
                66050,
            ),
        ],
        b"\x86\x00\x01\x00",
        source=b"\x86\x00\x01\x00",
    )
    found, _actor = walk_elements(state)
    last_at, last_span = found[-1][1], found[-1][2]
    #  24 for the message id and command id, then the elements, the actor and 0xFF.
    used = 24 + last_at + last_span + 32 + 8
    assert bits_of(state) == used, f"declares {bits_of(state)}, uses {used}"


def test_a_replayed_blob_keeps_the_length_it_had_on_the_wire():
    from dsor.payload_bits import PAYLOAD_BITS
    from dsor.recorded import payload

    sub = [name for name, bits in PAYLOAD_BITS.items() if bits % 8]
    assert len(sub) >= 40, f"only {len(sub)} sub-byte blobs known"
    for name in sub:
        body = payload(name)
        assert bits_of(body) == PAYLOAD_BITS[name], name
        assert bits_of(body) < len(body) * 8


def test_rewriting_a_creature_description_keeps_its_length():
    """The splices lost it, and creature descriptions are among the sub-byte blobs.

    with_template re-serialises through a BitWriter and copies the reader's
    *remaining* bits -- which is len(bytes) * 8, padding included -- so it declared
    three bits too many for every description. Six of the descriptions this server
    replays are sub-byte.
    """
    from dsor.recorded import (
        ENTITY_DESCRIPTIONS,
        monster_template,
        payload,
        with_actor,
        with_template,
    )

    checked = 0
    for name in ENTITY_DESCRIPTIONS:
        body = payload(name)
        same = with_template(body, monster_template(body))
        assert bytes(same) == bytes(body), name
        assert bits_of(same) == bits_of(body), name
        moved = with_actor(body, b"\x86\x00\x01\x00")
        assert bits_of(moved) == bits_of(body), name
        # And a longer name moves the length by whole bytes.
        longer = with_template(body, monster_template(body) + "xyz")
        assert bits_of(longer) == bits_of(body) + 24, name
        checked += 1
    assert checked == len(ENTITY_DESCRIPTIONS) >= 6


def test_every_element_names_the_actor_it_arrives_on():
    """Field 7 is the actor the effect is on, and copying it named the wrong one.

    Measured across every monster-addressed element in the captures: field 7 equals the
    addressed actor, 568 times out of 568. Copied along with the rest of the element it
    carried whatever actor the capture had, so two of Dragon Hide's three buffs arrived
    at 0x00010015 announcing 0x00010008 -- the player of the session they were lifted
    from. An effect that names an actor other than the one it is delivered to is a
    contradiction, and the client resolves that field to play an animation from it.
    """
    from raknet.bitstream import BitReader

    from dsor import effects
    from dsor.recorded import status_effects_message, walk_elements

    holder = b"\x15\x00\x01\x00"
    names = (
        "skill_frenzyshout_buff_armor",
        "skill_frenzyshout_buff_resistance",
        "skill_frenzyshout_buff_lifeleech",
    )
    state = status_effects_message(
        [
            (effects.wire_of(name), [0.2, 0.0, 0.0, 0.0, 0.0], 41230, 10.0, 66052 + i)
            for i, name in enumerate(names)
        ],
        holder,
        source=holder,
    )
    found, actor = walk_elements(state)
    assert actor == int.from_bytes(holder, "little")
    assert len(found) == len(names)
    for index, at, _span in found:
        reader = BitReader(state[3:], at)
        reader.read_uint(16)
        fields = [reader.read_uint(32) for _ in range(8)]
        assert fields[7] == actor, f"{effects.effect(index).id} names {fields[7]:#x}"

    # And on a creature, the creature -- not the player who cast it.
    monster = b"\x86\x00\x01\x00"
    on_it = status_effects_message(
        [(effects.wire_of("debuff_cc_stun"), [0.0] * 5, 41230, 5.0, 66060)],
        monster,
        source=monster,
    )
    found, actor = walk_elements(on_it)
    reader = BitReader(on_it[3:], found[0][1])
    reader.read_uint(16)
    assert [reader.read_uint(32) for _ in range(8)][7] == actor


def test_the_effect_command_matches_the_client_s_two_readers():
    """The layout, from the client's own Deserialize pair rather than from the bytes.

    DrasaClientHandler::DecodeCommand calls two methods and this server was failing the
    second:

        [vtable+0x28]  slot 5   0x140cc83b0  1 bit    -> this+0x20   the flag
                                0x140a4cd68  an array -> this+0x28   count + elements
        [vtable+0x48]  slot 9   0x140cc8a74  32 bits  -> this+0x18   the actor

    So the actor is the trailing "additional server data" every command carries, and
    the 0xFF behind it is a per-command terminator the frame loop reads. The order this
    server writes is therefore correct, and what the client was rejecting was the
    frame's declared bit length -- rounded up to the byte, which left the loop trying to
    decode one more command out of the padding.
    """
    from raknet.bitstream import BitReader

    from dsor import effects
    from dsor.recorded import status_effects_message

    monster = b"\x86\x00\x01\x00"
    names = ("debuff_cc_stun", "skill_seismicslam_debuff_armor")
    state = status_effects_message(
        [
            (effects.wire_of(n), [-0.5, 0.0, 0.0, 0.0, 0.0], 41230, 5.0, 66200 + i)
            for i, n in enumerate(names)
        ],
        monster,
        source=monster,
    )
    reader = BitReader(state[3:], 0)
    reader.read_bool()                       # this+0x20
    count = reader.read_uint(32)
    assert count == len(names)
    for _ in range(count):                   # this+0x28
        reader.read_uint(16)
        for _ in range(8):
            reader.read_uint(32)
        flags = [reader.read_bool() for _ in range(4)]
        assert flags[3], "these carry parameters"
        for _ in range(reader.read_uint(32)):
            reader.read_uint(32)
        # The tail is whatever the captured element for that effect carries. 90,481 of
        # 92,000 real elements carry vectors and 1,455 do not, and both forms are sent
        # as captured -- the handler copies the vectors into the instance for the
        # visualiser to read, so inventing or dropping them is what went wrong before.
        more = reader.read_bool()
        tail = reader.read_uint(8)
        if more or tail != 0xFF:
            for _ in range(6):
                reader.read_uint(32)
    assert reader.read_uint(32) == int.from_bytes(monster, "little")   # this+0x18
    assert reader.read_uint(8) == 0xFF, "the per-command terminator"
    # And that is the whole message: the declared length ends exactly here.
    assert reader.position == bits_of(state) - 24


def test_a_built_element_reproduces_the_real_ones_field_for_field():
    """The validation the three earlier attempts at building never had.

    Each of those read its constants off whichever handful of elements was in front of
    it, and each was wrong on the next handful -- "field 2 is 25, 50 or 75" and "field 7
    is the addressed actor in 568 of 568" were both artefacts of a sample that a broken
    parser had biased.

    The constants in build_element come from 139,112 real elements instead, parsed with
    the verified grammar and taken only from payloads dsor.chain accounted for whole.
    Rebuilding the corpus's own no-vector elements from them reproduces 1,069 of 1,455
    bit for bit, and field 7 is the only field that ever differs -- for two Dragon Hide
    group buffs delivered to an ally, where it names the caster instead of the holder.

    This pins the rules themselves, so a future edit to any of them fails here.
    """
    import struct

    from dsor import effects
    from dsor.recorded import (
        EFFECT_TICKS_PER_SECOND,
        MEASURED_FLAGS,
        MEASURED_HUNDRED,
        MEASURED_PARAMETERS,
        MEASURED_RATE,
        MEASURED_SPARE,
        NO_VECTORS,
        build_element,
    )
    from raknet.bitstream import BitReader

    holder = b"\x86\x00\x01\x00"
    stun = effects.wire_of("debuff_cc_stun")
    span, built = build_element(stun, 41230, 5.0, [0.25] * 5, 66300, holder)
    assert span == 477, "the no-vector form"

    reader = BitReader(built, 0)
    assert reader.read_uint(16) == stun
    fields = [reader.read_uint(32) for _ in range(8)]
    assert fields[3] == 41230, "the start tick"
    assert fields[5] == 5 * EFFECT_TICKS_PER_SECOND, "the duration in ticks"
    assert fields[1] == fields[3] + fields[5], "end - start == duration, 139,112 of 139,112"
    assert fields[2] == MEASURED_RATE
    assert fields[4] == MEASURED_SPARE
    assert fields[6] == MEASURED_HUNDRED
    assert fields[7] == int.from_bytes(holder, "little")
    assert fields[0] == 66300, "the instance handle"

    assert tuple(reader.read_bool() for _ in range(4)) == MEASURED_FLAGS
    assert reader.read_uint(32) == MEASURED_PARAMETERS == 5
    for _ in range(MEASURED_PARAMETERS):
        got = struct.unpack("<f", reader.read_uint(32).to_bytes(4, "little"))[0]
        assert got == pytest.approx(0.25)
    assert not reader.read_bool(), "the bool at +0x38"
    assert reader.read_uint(8) == NO_VECTORS, "the signed byte at +0x3c: -1"
    assert reader.position == span, "and nothing after it"


def test_field_two_follows_the_tick_rate():
    """The only field a built element reads from the database, and why.

    Field 2 takes exactly two values across 139,112 real elements: 25, and 0 for the
    one effect whose TickRate is 0. Every earlier reading of this field -- a track
    index, a stack size, a constant 25, "25 or 50 or 75" -- was taken off a sample.
    """
    from dsor import effects
    from dsor.recorded import MEASURED_RATE, build_element
    from raknet.bitstream import BitReader

    def field_two(name: str) -> int:
        wire = effects.wire_of(name)
        _span, built = build_element(wire, 1, 1.0, [0.0] * 5, 1, b"\x15\x00\x01\x00")
        reader = BitReader(built, 16 + 32 * 2)
        return reader.read_uint(32)

    ticking = effects.by_id("debuff_cc_stun")
    assert ticking.tick_rate > 0
    assert field_two("debuff_cc_stun") == MEASURED_RATE == 25

    still = effects.by_id("a0001_tutorial_heal_on_low_health")
    assert still.tick_rate == 0.0, "the one effect in the corpus that does not tick"
    assert field_two("a0001_tutorial_heal_on_low_health") == 0
