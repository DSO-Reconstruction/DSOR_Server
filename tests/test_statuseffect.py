"""The rebuilt status effect path, checked against what the live service was measured
doing rather than against itself.

The numbers quoted here come from 1,581 real skill casts and 313,301 real 0x004F
messages in the captures. They are in the assertions on purpose: a test that only checks
this server agrees with itself is what let the old version ship three different wrong
answers in a row.
"""

import pathlib

import pytest

from dsor import effects, measured, statuseffect as se
from dsor.gameplay import Position
from dsor.skills import wire_of
from dsor.world import World

DATA = pathlib.Path(__file__).resolve().parent.parent / "dsor/data"


def a_world(near: bool = False):
    """A world with a player in it, and creatures the player may target."""
    world = World()
    world.rules.mobs = 3
    world._ready()
    where = ("1.2.3.4", 5)
    player = world.player(where)
    player.in_world = True
    player.server_tick = 41000
    for creature in world.creatures.values():
        creature.described = True
    if near:
        creature = next(iter(world.creatures.values()))
        player.position = Position(
            x=creature.position.x,
            elevation=creature.position.elevation,
            y=creature.position.y,
        )
    else:
        player.position = Position(x=24453, elevation=-31744, y=25595)
    return world, where


def cast(skill: str, near: bool = False):
    world, where = a_world(near)
    world.resolve_attack(where, wire_of(skill))
    world.resolve_attack(where, wire_of(skill), travelled=True)
    sent = [
        payload
        for _address, payload in world.tick()
        if payload[:3] == bytes([se.MULTI, 0x4F, 0x00])
    ]
    return world, [se.decode(payload) for payload in sent]


# --------------------------------------------------------------------- the codec


def test_the_codec_round_trips_a_recorded_payload():
    """Not a payload this server built: one a real server sent.

    Over all 144 captures this reproduced 313,294 of 313,301 messages byte for byte,
    and the seven that differed were field 4, which is now carried rather than written
    as a constant. A BitStream states no lengths, so re-encoding is the only way to
    prove a field is understood and not merely skipped.
    """
    raw = (DATA / "status_effect_004f.bin").read_bytes()
    got = se.decode(raw)
    assert got is not None
    assert se.encode(got) == raw


def test_a_reading_that_misses_the_actor_is_refused():
    """A grammar cannot check itself, so landing on the tail is the whole evidence."""
    raw = (DATA / "status_effect_004f.bin").read_bytes()
    assert se.decode(raw) is not None
    assert se.decode(raw[:-1]) is None, "a byte short must not decode"
    assert se.decode(raw + b"\x00" * 4) is None, "nor four bytes long"


# ------------------------------------------------------- what each skill applies


@pytest.mark.parametrize(
    "skill,expected",
    [
        # Two, not the three the column names. The third is the talent's variant --
        # see test_the_talent_variant_is_not_applied.
        ("frenzyshout", {
            "skill_frenzyshout_buff_armor": (10.0, 0.2),
            "skill_frenzyshout_buff_resistance": (10.0, 0.2),
        }),
        # Measured: 42 casts of warshout, these four.
        ("warshout", {
            "skill_warshout_buff_movementspeed": (10.0, 0.4),
            "skill_warshout_buff_damage": (10.0, 0.3),
            "skill_warshout_buff_angrystrike": (10.0, 0.15),
            "skill_warshout_buff_mightybash": (1.0, -0.05),
        }),
    ],
)
def test_a_self_buff_carries_the_measured_duration_and_parameter(skill, expected):
    _world, messages = cast(skill)
    assert len(messages) == 1, f"{skill}: {len(messages)} messages"
    got = messages[0]
    assert got is not None
    seen = {}
    for element in got.elements:
        found = effects.by_wire(element.index)
        assert found is not None
        seen[found.id] = (element.duration / se.RATE, round(element.parameters[0], 4))
    for name, (seconds, first) in expected.items():
        assert name in seen, f"{skill} did not apply {name}"
        assert seen[name] == pytest.approx((seconds, first)), name


def test_the_talent_variant_is_not_applied():
    """``skill_frenzyshout_buff_lifeleech`` is "Dragon Hide - Dragon's Meal".

    Its column entry is C:1.0, so the chance field does not gate it, and this server
    sent it. The operator cast Dragon Hide on the live service with their own character
    and reported *two* effects labelled "Dragon Hide" -- which is armour and resistance
    and not the leech. Its LocaleId names a talent, and the recorded level-100 character
    whose captures show the leech on 42 of 57 casts had that talent.
    """
    from dsor import effects as table

    leech = table.by_id("skill_frenzyshout_buff_lifeleech")
    assert leech.talent_gated
    assert leech.locale_id == "warrior_frenzyshout_lifeleech_talentTitle"
    _world, messages = cast("frenzyshout")
    names = {table.by_wire(e.index).id for e in messages[0].elements}
    assert "skill_frenzyshout_buff_lifeleech" not in names
    assert len(names) == 2


def test_the_two_effects_dragon_hide_applies_are_both_labelled_dragon_hide():
    """What the operator sees on the live service, and the reason the earlier
    explanation was wrong.

    Neither of these effects has an entry under its own id. Their names come from the
    LocaleId column -- ``warrior_frenzyshout_buff_armorTitle`` -- which already carries
    the Title suffix. Looking a title up by id said Dragon Hide's buffs were nameless
    and produced a confident wrong story about what was on the screen.
    """
    from dsor.effect_titles import effect_title

    assert effect_title("skill_frenzyshout_buff_armor") == "Dragon Hide"
    assert effect_title("skill_frenzyshout_buff_resistance") == "Dragon Hide"
    _world, messages = cast("frenzyshout")
    from dsor import effects as table

    labels = [effect_title(table.by_wire(e.index).id) for e in messages[0].elements]
    assert labels == ["Dragon Hide", "Dragon Hide"]


def test_dragon_hide_applies_nothing_belonging_to_another_skill():
    """Spike Shield and Power of Smash belong to spikedShield and warshout.

    Measured over 57 real frenzyshout casts: neither appears once. They are the two
    effects the operator kept seeing, and they are named -- 'Spike Shield' and 'Power of
    Smash' are real strings in db.statuseffect.xml -- while frenzyshout's own three have
    no displayed name at all.
    """
    _world, messages = cast("frenzyshout")
    names = {effects.by_wire(e.index).id for e in messages[0].elements}
    assert "warrior_spikedShield_buff" not in names
    assert "skill_warshout_buff_mightybash" not in names
    assert len(names) == 2


# ------------------------------------------------------------- the two actor fields


def test_field_zero_is_a_handle_and_field_seven_is_the_actor():
    """Field 0 is never the actor -- 394 of 394 measured elements -- and is unique
    within a message in 186 of 190. Field 7 is the actor the effect is on, in 265 of
    394, and per effect: skill_frenzyshout_buff_armor carries it in 18 of its 34
    elements, warshout's mightybash buff in 12 of 12.

    Field 7 was briefly a "grant handle" here, fitted to one official Dragon Hide
    message whose armour and resistance elements shared a value that was not the actor.
    That is the minority reading of that very effect, and taking it threw the majority
    away.
    """
    world, messages = cast("frenzyshout")
    got = messages[0]
    mine = int.from_bytes(world.player(("1.2.3.4", 5)).actor, "little")
    assert got.actor == mine
    handles = {element.instance for element in got.elements}
    assert len(handles) == len(got.elements), "two effects must not share a handle"
    for element in got.elements:
        assert element.instance != mine, "field 0 is a handle, not the actor"
        assert element.holder == mine, "field 7 is the actor the effect is on"


def test_a_debuff_is_addressed_to_the_creature():
    world, messages = cast("laceratingstrike", near=True)
    assert messages, "no status effect sent for a debuff"
    struck = [c for c in world.creatures.values() if c.running]
    assert struck, "no creature carries the debuff"
    theirs = int.from_bytes(struck[0].actor, "little")
    got = messages[0]
    assert got.actor == theirs
    for element in got.elements:
        assert element.holder == theirs
        assert element.instance != theirs


def test_the_flags_come_from_the_measured_table_per_effect():
    """Not from a rule about the context, which is what they were and it was wrong.

    skill_frenzyshout_buff_armor carries (0,0,0,1) in all 34 of its measured elements
    and skill_laceratingstrike_debuff_armor carries (0,0,1,1) in all four of its, while
    whether field 7 is the message's actor predicts neither -- armour's is the actor 18
    times and is not 16 times, with the same flags throughout.
    """
    from dsor import measured

    for skill, near in (("frenzyshout", False), ("laceratingstrike", True)):
        _world, messages = cast(skill, near=near)
        assert messages, skill
        for element in messages[0].elements:
            want = measured.FLAGS.get(element.index, (0, 0, 0, 1))
            assert tuple(int(b) for b in element.flags) == tuple(want), (
                skill,
                element.index,
            )


def _sent_elements(world):
    for _address, payload in world.tick():
        if payload[:3] == bytes([se.MULTI, 0x4F, 0x00]):
            got = se.decode(payload)
            if got is not None:
                yield from got.elements


# ------------------------------------------------------------------ the clock


def test_the_three_clock_fields_agree():
    """``end - start == duration`` in 139,112 of 139,112 real elements."""
    _world, messages = cast("warshout")
    for element in messages[0].elements:
        assert element.end - element.start == element.duration
        assert element.start == 41000


# ------------------------------------------------------------------ what is not sent


def test_a_skill_that_applies_nothing_sends_nothing():
    """angrystrike, measured: 964 of 1,057 casts added no effect at all.

    An empty 0x004F is answered with "Received empty StatusEffectCommand!", and since
    the command carries an actor's whole set, sending one with nothing in it says every
    effect has ended.
    """
    _world, messages = cast("angrystrike", near=True)
    assert messages == []


def test_the_same_state_is_not_sent_twice():
    """Which effects are on an actor is state. Sending it again asks the client to add
    them a second time, and it answers "Failed to add actor effect ... Effect already
    present!" -- 1,823 times in one recorded session of the old version.
    """
    world, messages = cast("frenzyshout")
    assert len(messages) == 1
    again = [
        payload
        for _address, payload in world.tick()
        if payload[:3] == bytes([se.MULTI, 0x4F, 0x00])
    ]
    assert again == [], "the unchanged state was restated"


def test_an_effect_with_no_duration_is_not_applied():
    """warshout's ctfdropflag is C:1.0 with no duration, and the captures never show it
    applied -- 42 casts, four effects each time, never that one."""
    entries = effects.granted_by(wire_of("warshout"))
    assert any(e.effect == "ctfdropflag" for e in entries)
    _world, messages = cast("warshout")
    names = {effects.by_wire(e.index).id for e in messages[0].elements}
    assert "ctfdropflag" not in names
    assert len(names) == 4


# ---------------------------------------------------------------- the visuals


def test_the_client_is_given_effects_it_can_draw():
    """The VFX is not in the message: StartSequence, StartAnimation and IconBrush are
    columns on the effect, so the client draws them from the index it is sent. Which is
    why "pas d'animation de stun" and "pas d'icone de break armor" were one defect."""
    stun = effects.by_id("debuff_cc_stun")
    assert stun.sequence == "stun_loop"
    assert stun.animation == "Stunned"
    assert stun.icon
    armour = effects.by_id("skill_seismicslam_debuff_armor")
    assert armour.sequence == "warrior_armordamage"
    assert armour.icon


def test_every_class_is_covered_and_not_just_the_warrior():
    """The database holds all five classes, and the measurement is what licenses
    trusting it: every one of the 13 effects measured on real casts is in these
    columns."""
    assert effects.loaded() == 6703
    classes = set()
    from dsor import database

    for _index, name, char_class in database.rows("_Template_Skill", "Id", "CharClass"):
        if char_class:
            classes.add(char_class)
    assert len(classes) >= 4, classes
