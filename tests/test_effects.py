"""Status effects: what a skill grants, and how it reaches the client."""

import time

import pytest

from dsor import effects
from dsor.gameplay import Position
from dsor.recorded import (
    status_effect_index,
    status_effect_parameters,
    tick_state,
    with_status_effect,
)
from dsor.skills import wire_of
from dsor.world import World


def a_player():
    world = World(name="t")
    world.rules.mobs = 0
    # Animated effects are on by default now; two tests turn them off to check the
    # switch still holds them back.
    world.rules.animated_effects = True
    sender = ("127.0.0.1", 1)
    player = world.player(sender)
    player.position = Position(0, 0, 0)
    player.in_world = True
    player.health = 2700.0
    player.max_health = 2700.0
    player.level = 15
    return world, sender


def test_the_recorded_tick_state_decodes_as_one_named_effect():
    """The 0x004F replayed every tick is not opaque.

    StatusEffectCommand::Serialize was recovered through the vtable: a flag, a 32-bit
    count, then per effect a 16-bit index, eight 32-bit fields, four flags and a
    float32 parameter array. Decoding the recording with that grammar reads index
    1350, which is row 1351 of _Template_StatusEffect --
    a0001_tutorial_heal_on_low_health, exactly what a tutorial dungeon carries. A
    layout that produces the right name from the right table is not a coincidence.
    """
    index = status_effect_index()
    assert index == 1350
    assert effects.effect(index).id == "a0001_tutorial_heal_on_low_health"
    assert status_effect_parameters() == [0.0] * 5


def test_rewriting_the_effect_keeps_the_message_intact():
    """Only the index and the parameters move.

    Three of the eight integers are not understood and neither is the tail, so an
    element cannot be built from nothing -- it is rewritten, the way the item drop and
    the creature description already are.
    """
    wire = effects.wire_of("skill_warshout_buff_movementspeed")
    original = tick_state()
    out = with_status_effect(original, wire, [0.4, 0.0, 0.0, 0.0, 0.0])

    assert len(out) == len(original), "same length, so nothing shifted"
    assert out[:3] == original[:3], "the message id and opcode are untouched"
    assert status_effect_index(out) == wire
    assert abs(status_effect_parameters(out)[0] - 0.4) < 1e-6

    # A small, contiguous rewrite: the index, and the first float.
    changed = [i for i in range(len(out)) if out[i] != original[i]]
    assert changed == [7, 8, 9, 45, 46, 47, 48, 49]


def test_a_rewrite_that_does_not_fit_is_refused_rather_than_sent():
    """The read-back guard, and what it can and cannot catch.

    It catches a value that does not survive the field: an index wider than sixteen
    bits truncates, reads back as something else, and is refused instead of shipped.

    What it cannot catch is a wrong offset, because the reader uses the same constant
    as the writer and a symmetric round-trip agrees with itself. That is not a
    hypothetical -- it is how the creature description's position went wrong here
    once, sitting 448 bits from the end in some records and 480 or 520 in others, with
    a read/write pair that round-tripped perfectly the whole time. The evidence for
    these offsets is that the recording decodes to the right effect name from the
    right table, not that they round-trip.
    """
    import pytest

    from dsor import recorded

    with pytest.raises(ValueError):
        recorded.with_status_effect(tick_state(), 0x1_0000 + 5149, [0.4])

    # And the offsets themselves are checked the only way they can be: against the
    # recording's own content.
    assert effects.effect(status_effect_index()) is not None


def test_warshout_is_furious_battlecry_and_grants_four_things():
    """It names sixteen effects and grants four; the rest need items or talents.

    An entry's chance of 0.0 does not mean "never" -- it marks the ones that depend on
    something the character does not have.
    """
    granted = effects.granted_by(wire_of("warshout"))
    found = {
        m.attribute: m.amount(entry.substitutions)
        for entry in granted
        for m in effects.by_id(entry.effect).starts
    }
    assert found["Speed"] == 0.4, "+40% movement, which is the one that shows"
    assert found["Damage"] == 0.3
    assert found["SkillDamage"] == 0.15
    assert found["ResourceCost"] == -0.05
    assert all(entry.certain for entry in granted)


def test_battlecry_debuffs_its_victims_rather_than_buffing_the_caster():
    granted = effects.granted_by(wire_of("battlecry"))
    inflicted = effects.inflicted_by(wire_of("battlecry"))
    assert not granted
    found = {
        (m.attribute, m.targets): m.amount(entry.substitutions)
        for entry in inflicted
        for m in effects.by_id(entry.effect).starts
    }
    assert found[("Speed", ("Movement",))] == -0.1
    assert found[("Resistance", ("Fire", "Ice", "Lightning", "Poison"))] == -0.2


def test_a_shout_gives_rage_and_says_so():
    """warshout's ResourceGain is 0.6, and none of it was ever served.

    encode_actor_vitals was written, tested and never called, so the resource stayed
    where it started for the whole of every session.
    """
    world, sender = a_player()
    player = world.player(sender)
    assert player.resource == 0.0

    world.resolve_attack(sender, wire_of("warshout"))
    # 0.6 of a pool of a hundred, which the level table gives. Sixty rage, not six:
    # the pool was written here as ten and that is why nothing appeared to happen.
    assert world.resource_pool(sender) == 100.0
    assert player.resource == 60.0

    sent = world._drain()
    assert sent, "the client was told"
    # 0x85 then the stats opcode.
    from dsor.gameplay import STATS_OPCODE

    assert any(
        payload[:3] == bytes([0x85]) + STATS_OPCODE.to_bytes(2, "little")
        for _address, payload in sent
    )


def test_the_resource_never_leaves_its_pool():
    world, sender = a_player()
    player = world.player(sender)
    pool = world.resource_pool(sender)
    for _ in range(20):
        world.resolve_attack(sender, wire_of("warshout"))
    assert player.resource == pool

    for _ in range(40):
        world.resolve_attack(sender, wire_of("mighty360"))
    assert player.resource == 0.0


def test_the_buff_rides_the_tick_state_while_it_lasts():
    world, sender = a_player()
    world.resolve_attack(sender, wire_of("warshout"))
    world._drain()

    world._tick_pair(sender)
    state = world._drain()[0][1]
    # warshout's movement-speed buff has a real element in the capture, so it is the
    # one that can be served. Its parameters are the capture's own, not ours: rewriting
    # them is safe but the values a real server paired with the rest of the element are
    # better evidence than a number from the template.
    assert effects.effect(status_effect_index(state)).id in {
        "skill_warshout_buff_movementspeed",
        "skill_warshout_buff_damage",
    }


def test_a_buff_stops_when_it_runs_out():
    world, sender = a_player()
    world.resolve_attack(sender, wire_of("warshout"))
    player = world.player(sender)
    assert player.buffs

    # Ten seconds is what warshout asks for; put every expiry in the past.
    player.buffs = [
        (wire, parameters, time.monotonic() - 1.0, seconds)
        for wire, parameters, _expires, seconds in player.buffs
    ]
    assert world.live_effects(player) == []
    assert player.buffs == []

    world._drain()
    world._tick_pair(sender)
    state = world._drain()[0][1]
    assert status_effect_index(state) == 1350, "back to the recording"


def test_the_switch_turns_the_whole_thing_off():
    world, sender = a_player()
    world.rules.status_effects = False
    world.resolve_attack(sender, wire_of("warshout"))
    assert world.player(sender).buffs == []
    world._drain()
    world._tick_pair(sender)
    assert status_effect_index(world._drain()[0][1]) == 1350


def test_a_skill_that_grants_nothing_leaves_the_state_alone():
    world, sender = a_player()
    world.resolve_attack(sender, wire_of("angrystrike"))
    assert world.player(sender).buffs == []


def test_all_of_a_skills_effects_travel_now():
    """It used to be one, and that is why most of them looked broken.

    The recorded 0x004F carries a single element and the element's tail branches over
    vectors this server does not decode. But a *copy* of a real element needs only its
    length, and the length is known: the player's actor sits at bit 702 and the first
    element begins at 33, so an element is 669 bits. The arithmetic closes exactly --
    1 flag + 32 count + 669 + 32 actor + 8 terminator, plus two bits of padding, is the
    744 the message is.
    """
    from dsor.recorded import (
        EFFECT_ACTOR_BIT,
        EFFECT_ELEMENT_BIT,
        EFFECT_ELEMENT_BITS,
        status_effect_count,
        status_effect_indices,
        status_effects_message,
    )

    assert EFFECT_ELEMENT_BIT + EFFECT_ELEMENT_BITS == EFFECT_ACTOR_BIT == 702
    assert 1 + 32 + EFFECT_ELEMENT_BITS + 32 + 8 + 2 == len(tick_state()[3:]) * 8

    from dsor.skills import by_id

    world, sender = a_player()
    # granted_by is the skill's own list, before this server decides what it can
    # serve: it still holds ctfdropflag, a capture-the-flag flag with no place in a
    # dungeon. The world's own filter is what settles the four.
    assert len(effects.granted_by(wire_of("warshout"))) == 5
    granted = world._entries(by_id("warshout"), victim=False)
    assert [entry.effect for entry in granted] == [
        "skill_warshout_buff_movementspeed",
        "skill_warshout_buff_damage",
        "skill_warshout_buff_angrystrike",
        "skill_warshout_buff_mightybash",
    ]

    world.resolve_attack(sender, wire_of("warshout"))
    assert len(world.player(sender).buffs) == 4

    world._drain()
    world._tick_pair(sender)
    state = world._drain()[0][1]
    # All four go out: two spliced from the capture and two built from the corpus
    # constants. Sending only what the capture held is what made three of warshout's
    # four vanish.
    from dsor.recorded import servable_effects

    have, built = servable_effects(
        effects.wire_of(entry.effect) for entry in granted
    )
    assert len(have) == 2 and len(built) == 2, "two of each, in this capture"
    assert status_effect_count(state) == 4
    assert set(status_effect_indices(state)) == {
        effects.wire_of(entry.effect) for entry in granted
    }


def test_the_grammar_reads_the_recording_field_for_field():
    """The element layout, from StatusEffectCommand's Deserialize, against the wire.

    The element reader at 0x140a4c6bc reads: a 16-bit index, eight 32-bit fields, four
    bools, and -- only if the fourth bool is set -- a float32 parameter array, a bool,
    and a *signed* byte. The recording agrees with every part of that.

    Copying the element used to reproduce the recording byte for byte, which was the
    check that validated the copy. Elements are built now, and deliberately shorter, so
    the check moved to the grammar itself.
    """
    from dsor.recorded import element_grammar

    grammar = element_grammar()
    assert grammar["index"] == 1350
    assert grammar["integers"] == [65546, 176, 0, 151, 0, 25, 100, 0]
    assert grammar["flags"] == [False, False, False, True], "the fourth is the gate"
    assert len(grammar["parameters"]) == 5
    assert grammar["tail_bool"] is True
    assert grammar["tail_byte"] == 2, "so the recording does carry vectors"


def test_frenzyshout_no_longer_drops_its_life_leech():
    """Three effects, and the visible one used to be the one thrown away.

    Carrying only the first meant a resistance buff nobody can see, which is exactly
    what "ne fait rien" looks like.
    """
    granted = effects.granted_by(wire_of("frenzyshout"))
    assert [entry.effect for entry in granted] == [
        "skill_frenzyshout_buff_armor",
        "skill_frenzyshout_buff_resistance",
        "skill_frenzyshout_buff_lifeleech",
    ]
    world, sender = a_player()
    world.resolve_attack(sender, wire_of("frenzyshout"))
    carried = {
        effects.effect(wire).id for wire, *_ in world.player(sender).buffs
    }
    assert "skill_frenzyshout_buff_lifeleech" in carried


def test_a_stun_and_a_poison_are_gated_behind_talents_and_can_be_forced():
    """The honest state of both.

    debuff_cc_stun is ActorFeature:off,Movement,RegularSkills for five seconds, and
    debuff_dot_poison is CurrHealthPointsDmg every 1.5 seconds. Both are real, both are
    the client's own arithmetic -- and both read C:0.0 on the warrior's skills, meaning
    a talent has to raise the chance. This server has no talents, so faithfully they
    never fire.
    """
    stun = effects.by_id("debuff_cc_stun")
    poison = effects.by_id("debuff_dot_poison")
    assert "ActorFeature" in stun.start_modifiers
    assert stun.duration == 5.0
    assert "CurrHealthPointsDmg" in poison.tick_modifiers
    assert poison.tick_rate == 1.5

    lacerating = wire_of("laceratingstrike")
    assert "debuff_cc_stun" not in [e.effect for e in effects.inflicted_by(lacerating)]
    assert "debuff_cc_stun" in [
        e.effect for e in effects.anything_by(lacerating, victim=True)
    ]

    bash = wire_of("mightybash")
    assert effects.inflicted_by(bash) == ()
    assert "debuff_dot_poison" in [
        e.effect for e in effects.anything_by(bash, victim=True)
    ]


def test_no_empty_status_effect_command_is_ever_sent():
    """"Received empty StatusEffectCommand!" over and over, once per creature per tick.

    Filtering to the effects a real element exists for happens inside the encoder, and
    the callers sent whatever it returned without asking whether it held anything. A
    command with a count of zero is a message that says nothing, and the client says so
    every time.
    """
    from dsor.gameplay import Position
    from dsor.recorded import status_effect_count
    from dsor.skills import by_id
    from dsor.world import Creature

    world, sender = a_player()
    actor = b"\x86\x00\x01\x00"
    world.creatures[actor] = Creature(
        actor=actor,
        record=b"",
        position=Position(0, 0, 0),
        health=100.0,
        max_health=100.0,
        blueprint="a0001_gen_anderworld_creature",
        described=True,
    )
    world.rules.force_effects = True
    world.inflict_effects(actor, by_id("mightybash"))
    assert world.creatures[actor].effects, "something is on the creature"

    world._drain()
    for _ in range(5):
        world._tick_pair(sender)
    for _address, payload in world._drain():
        if payload[1:3] == b"\x4f\x00":
            assert status_effect_count(payload) > 0, "an empty command went out"


def test_nothing_a_warrior_inflicts_can_be_drawn_yet():
    """Two separate reasons that between them cover every warrior debuff.

    debuff_dot_poison and debuff_cc_stun have no captured element: a capture of the live
    service with every warrior skill cast does not contain them, because they are
    talent-gated there too. And debuff_cc_charge, which *is* in the capture, is refused
    by the skill-reference check -- its modifiers name chainlightning, lightningstrike
    and balllightning, which a warrior does not have.

    So the mechanic is served and the drawing is not, and the log says which once.
    """
    from dsor.elements import element
    from dsor.skills import by_id

    world, _sender = a_player()
    world.rules.force_effects = True
    inflicted = world._entries(by_id("mightybash"), victim=True)
    assert inflicted, "the skill does inflict something"
    assert all(element(effects.wire_of(e.effect)) is None for e in inflicted)

    assert element(effects.wire_of("debuff_cc_charge")) is not None, "captured"
    from dsor.skills import of_class

    warrior = frozenset(sk.id for sk in of_class("warrior"))
    assert not effects.by_id("debuff_cc_charge").servable(warrior), "names mage skills"


def test_the_pool_is_a_hundred_and_the_arithmetic_comes_out_exact():
    """Measured twice on the wire, in one real session.

    Out of combat the resource falls by exactly 5.00 a step, from 63.6 to 0. Then two
    angrystrikes raise it by exactly 5.00 each -- 0.2 to 5.2, then 5.4 to 10.4 -- and
    angrystrike's ResourceGain is 0.05. So the fractions in the templates are
    fractions of a hundred, which is what BaseMana says at every one of the 110
    levels.

    An earlier note here read those falling steps of 5.00 as the effect of the skill.
    They are the decay between casts; the skill is the rise.
    """
    from dsor.combat import resource_at
    from dsor.skills import by_id

    assert resource_at(1) == resource_at(100) == 100.0
    assert by_id("angrystrike").resource_gain * resource_at(1) == 5.0
    assert by_id("warshout").resource_gain * resource_at(1) == 60.0
    assert by_id("mighty360").resource_cost * resource_at(1) == 40.0


def test_a_skill_both_costs_and_gains_from_the_same_pool():
    world, sender = a_player()
    world.player(sender).level = 100
    for name, expected in (
        ("warshout", 60.0),
        ("angrystrike", 65.0),
        ("mighty360", 25.0),
    ):
        world.resolve_attack(sender, wire_of(name))
        assert world.player(sender).resource == expected, name


def test_the_buff_is_stamped_with_the_clock_the_server_announces():
    """Replaying the recorded ticks is why nothing happened.

    They say the effect began at 151 and ended at 176. A client whose clock is in the
    tens of thousands reads that as something long finished, so the right index and
    the right parameter arrived attached to an expired window.
    """
    from dsor.recorded import (
        EFFECT_TICKS_PER_SECOND,
        status_effect_fields,
    )

    assert status_effect_fields()[1] == 176, "the recording ends here"

    world, sender = a_player()
    world.player(sender).server_tick = 41230
    world.resolve_attack(sender, wire_of("warshout"))
    world._drain()
    world._tick_pair(sender)
    fields = status_effect_fields(world._drain()[0][1])

    span = 10 * EFFECT_TICKS_PER_SECOND  # warshout asks for ten seconds
    assert fields[3] == 41230, "starts now"
    assert fields[1] == 41230 + span, "ends ten seconds from now"
    assert fields[5] == span, "and says how long it runs"
    # Field 2 is 25 now, and field 7 the caster: a capture of the live service
    # carrying fourteen animated effects settled both. What is still copied from the
    # recording is 4 and 6 -- and 6 is 100 in every element anywhere, real or recorded.
    from dsor.recorded import EFFECT_RATE

    assert fields[2] == EFFECT_RATE == 25
    recorded = status_effect_fields()
    for index in (4, 6):
        assert fields[index] == recorded[index], index
    assert recorded[6] == 100


def test_twenty_five_ticks_is_one_second():
    """The duration field read 25 for an effect whose template says 1.0 seconds.

    Which is the tick rate the rest of this server already uses: 40 ms.
    """
    from dsor.recorded import EFFECT_TICKS_PER_SECOND
    from dsor.world import GAME_TICK_MS

    assert EFFECT_TICKS_PER_SECOND * GAME_TICK_MS == 1000
    assert effects.effect(1350).duration == 1.0
    assert status_effect_fields_duration() == EFFECT_TICKS_PER_SECOND


def status_effect_fields_duration():
    from dsor.recorded import status_effect_fields

    return status_effect_fields()[5]


def test_no_effect_that_reaches_into_a_skill_is_ever_served():
    """The client asserts on one: skillTemplateId.IsValid().

    Two produced it, and both are identifiable from their modifiers:

        set_cny2026_warrior_earthquake_dmg
            SkillStatusEffect:modLE,earthquake,skill_earthquake_aura,$1:-0.05
        warrior_talent_damage_dealer_cooldown_reduction
            ActiveCoolDown:$talent_warrior_dd_cd,absolute,Skill01..Skill20

    One rewrites another skill's status effects, the other names skill slots. Neither
    belongs on a character with no set and no talents, and a blanket "apply everything"
    sent both.
    """
    from dsor.skills import of_class

    warrior = frozenset(s.id for s in of_class("warrior"))
    for name in (
        "set_cny2026_warrior_earthquake_dmg",
        "warrior_talent_damage_dealer_cooldown_reduction",
    ):
        found = effects.by_id(name)
        assert found is not None, name
        assert not found.servable(warrior), name


def test_the_filter_keeps_the_effects_worth_having():
    """A stricter rule threw out both of the ones that matter.

    "Plain attribute changes only" excluded debuff_cc_stun for carrying a
    StopStatusEffect and debuff_dot_poison for chaining an explosion trigger. Chaining
    another effect and stopping one are not the problem; reaching into a skill is.
    """
    from dsor.skills import of_class

    warrior = frozenset(s.id for s in of_class("warrior"))
    assert effects.by_id("debuff_cc_stun").servable(warrior)
    assert effects.by_id("debuff_dot_poison").servable(warrior)
    # And the ones naming warrior skills, which a warrior has.
    assert effects.by_id("skill_frenzyshout_buff_lifeleech").servable(warrior)
    assert effects.by_id("skill_warshout_buff_angrystrike").servable(warrior)
    # The hash suffix is stripped before the name is checked.
    modifier = effects.by_id("skill_warshout_buff_angrystrike").starts[0]
    assert modifier.skills_named == ("angrystrike",)


def test_forcing_only_reaches_a_skills_own_effects_and_the_debuffs():
    """Not the item, set and talent entries: nonsense, and the road to the assertion."""
    assert effects.forceable("debuff_cc_stun")
    assert effects.forceable("skill_warshout_buff_damage")
    for name in (
        "item_materi_belt_buff",
        "itemset_sewers_damage_buff",
        "talent_warrior_dd_warshout_buff_crit",
        "set_cny2026_warrior_earthquake_dmg",
        "chr2025_set_coldchill",
        "ammunition_damage_fire",
    ):
        assert not effects.forceable(name), name


def test_forced_gives_exactly_the_stun_and_the_poison():
    world, sender = a_player()
    world.rules.force_effects = True
    from dsor.skills import by_id

    inflicted = {
        entry.effect
        for entry in world._entries(by_id("laceratingstrike"), victim=True)
    }
    assert "debuff_cc_stun" in inflicted
    assert "skill_laceratingstrike_debuff_armor" in inflicted
    assert not any(name.startswith("ammunition_") for name in inflicted)

    poisoned = {
        entry.effect for entry in world._entries(by_id("mightybash"), victim=True)
    }
    assert "debuff_dot_poison" in poisoned


def test_dragon_hide_puts_its_effects_on_a_place_not_on_an_actor():
    """Which is why defiance does nothing here, and it is not a dropped effect.

    Its UserStatusEffects are all item, set and talent entries. Everything that is
    actually the skill -- the resource refill, the regeneration, the auras that slow
    and weaken enemies -- sits in LocationStatusEffects, a third list this server does
    not serve at all: an aura placed on the ground rather than on a character.
    """
    from dsor.skills import wire_of

    assert effects.granted_by(wire_of("defiance")) == ()
    assert effects.inflicted_by(wire_of("defiance")) == ()

    # Its aura is in the table, and it is an aura: no modifiers of its own, and
    # Groups says so.
    aura = effects.by_id("skill_defiance_enemies_movement_aura")
    assert aura is not None
    assert aura.groups == "Aura"
    assert not aura.starts and not aura.ticks, "an aura carries no modifier itself"

    # And what the aura *does* is in an effect this table does not carry, because the
    # generator collects what the class skills name and nothing collects what an
    # effect names in turn. A second gap, recorded rather than papered over: nested
    # effects are invisible here.
    assert effects.by_id("skill_defiance_buff_resource") is None


def test_the_tooltips_own_tokens_agree_with_what_is_served():
    """The check the descriptions make possible without their words.

    _Template_LocaleToken says which effect each piece of a skill's description reads
    from. A token of type SkillEffect whose attribute is UserEffect or VictimEffect
    names an effect that goes on an actor; LocationEffect names an aura on the ground.

    So warshout's tooltip names exactly the four this server sends, and defiance's
    names six auras and nothing else -- which is the description itself saying Dragon
    Hide is a place, not a buff.
    """
    import sqlite3
    from pathlib import Path

    from dsor.skills import by_id, wire_of

    database = Path.home() / "dso/db/db_static.sqlite"
    if not database.exists():
        import pytest

        pytest.skip("the client database is not here")

    db = sqlite3.connect(str(database))
    rows = db.execute(
        "SELECT Param1Attr, Param1Id, Param2Id FROM _Template_LocaleToken"
        " WHERE TokenType = 'SkillEffect' AND Param1Id IN ('warshout', 'defiance')"
    ).fetchall()

    promised = {}
    for attribute, skill, effect_id in rows:
        promised.setdefault(skill, set()).add((attribute, effect_id))

    world = World(name="t")
    world.rules.animated_effects = True
    served = {
        entry.effect for entry in world._entries(by_id("warshout"), victim=False)
    }
    assert {e for a, e in promised["warshout"] if a == "UserEffect"} == served

    # Every one of defiance's is a LocationEffect, so there is nothing to put on the
    # character at all.
    assert all(a == "LocationEffect" for a, _ in promised["defiance"])
    assert not world._entries(by_id("defiance"), victim=False)


def test_elements_are_copied_off_the_wire_and_not_built():
    """Three attempts at building one each got a field wrong.

    The 8-bit field past the parameters was read as a track index, then as a stack
    size; field 2 was written as a constant 25 when 185 of 365 real elements carry 50 or
    75. Each was a correlation that held over the sample I looked at and not the next.

    So an element is copied from one the live service sent for the same effect, and only
    the three tick fields are rewritten. Every other field carries a value a real server
    chose.
    """
    from dsor.elements import ELEMENTS, element
    from dsor.recorded import real_element, servable_effects

    # The three lengths are exactly the three forms the grammar describes: 276 bits
    # with no parameters, 477 with parameters and no vectors, 669 with vectors.
    assert {span for span, _bits in ELEMENTS.values()} == {276, 477, 669}

    wire = effects.wire_of("skill_warshout_buff_movementspeed")
    span, original = element(wire)
    assert span == 669, "this one carries vectors"

    copy = real_element(wire, start_tick=41230, seconds=10.0)
    assert copy is not None
    # Only the tick fields differ from what came off the wire.
    changed = [i for i in range(len(original)) if original[i] != copy[i]]
    assert changed, "the ticks were written"
    assert len(changed) <= 12, f"{len(changed)} bytes changed, expected the ticks only"


def test_an_effect_with_no_captured_element_is_built_rather_than_dropped():
    """It used to be dropped, and that is why nothing arrived.

    Thirty-four effects were captured and the game has 6703, so "only what the capture
    holds" meant almost nothing. One without a captured element is built from the corpus
    constants instead -- and a captured one is still preferred, because it also carries
    the vectors and whatever field 4's few non-zero values mean.
    """
    from dsor.elements import element
    from dsor.recorded import (
        servable_effects,
        status_effect_count,
        status_effect_indices,
        status_effects_message,
    )

    captured, built = servable_effects(
        effects.wire_of(name)
        for name in (
            "skill_warshout_buff_movementspeed",
            "debuff_cc_stun",
            "skill_frenzyshout_buff_armor",
            "debuff_dot_poison",
        )
    )
    assert [effects.effect(w).id for w in captured] == [
        "skill_warshout_buff_movementspeed",
        "skill_frenzyshout_buff_armor",
    ]
    assert [effects.effect(w).id for w in built] == [
        "debuff_cc_stun",
        "debuff_dot_poison",
    ]

    # All four go out, two spliced and two built.
    wires = captured + built
    sent = status_effects_message(
        [(w, [0.4, 0.0, 0.0, 0.0, 0.0], 41230, 5.0) for w in wires],
        b"\x86\x00\x01\x00",
        source=b"\x08\x00\x01\x00",
        instance=0x00010200,
    )
    assert status_effect_count(sent) == 4
    assert status_effect_indices(sent) == wires


def test_an_animated_effect_can_be_held_back():
    """The switch, and the reason it existed.

    While an element was a copy of the tutorial heal's it carried that effect's float3
    vectors, and handing them to an animated effect sent the client's sequencer into a
    FixedArray it could not index. Elements are built now and end before the vectors,
    so animated effects are sent again — but the switch stays, because the sequencer is
    the part of this with the worst track record.
    """
    from dsor.skills import by_id

    assert not effects.by_id("skill_frenzyshout_buff_lifeleech").animated
    for name in (
        "debuff_cc_stun",
        "debuff_dot_poison",
        "skill_warshout_buff_movementspeed",
        "skill_laceratingstrike_debuff_armor",
    ):
        assert effects.by_id(name).animated, name

    world, sender = a_player()
    world.rules.force_effects = True
    world.rules.animated_effects = False
    served = {
        entry.effect for entry in world._entries(by_id("warshout"), victim=False)
    }
    assert "skill_warshout_buff_damage" in served
    assert "skill_warshout_buff_movementspeed" not in served, "animated, held back"

    # And the switch lets them through for whoever wants to retest the sequencer.
    world.rules.animated_effects = True
    served = {
        entry.effect for entry in world._entries(by_id("warshout"), victim=False)
    }
    assert "skill_warshout_buff_movementspeed" in served


def test_what_survives_the_filters_is_still_worth_having():
    """The life leech, warshout's damage buffs and battlecry's three debuffs."""
    from dsor.skills import by_id

    world, sender = a_player()
    world.rules.force_effects = True
    world.rules.animated_effects = False

    assert {
        e.effect for e in world._entries(by_id("frenzyshout"), victim=False)
    } == {"skill_frenzyshout_buff_lifeleech"}
    assert {
        e.effect for e in world._entries(by_id("warshout"), victim=False)
    } == {
        "skill_warshout_buff_damage",
        "skill_warshout_buff_angrystrike",
        "skill_warshout_buff_mightybash",
    }
    assert {
        e.effect for e in world._entries(by_id("battlecry"), victim=True)
    } == {
        "skill_battlecry_debuff_movementspeed",
        "skill_battlecry_debuff_resistance",
        "skill_battlecry_debuff_attackspeed",
    }


def a_creature(world, actor=b"\x86\x00\x01\x00", health=5_000_000.0):
    from dsor.gameplay import Position
    from dsor.world import Creature

    world.creatures[actor] = Creature(
        actor=actor,
        record=b"",
        position=Position(0, 0, 0),
        health=health,
        max_health=health,
        blueprint="a0001_gen_anderworld_creature",
        described=True,
    )
    return actor


def swing(world, sender, name):
    world.resolve_attack(sender, wire_of(name))
    world.pending_swings = [(0.0,) + e[1:] for e in world.pending_swings]
    world.land_swings()


def test_a_new_cast_no_longer_wipes_the_buffs_already_on_you():
    """A real bug, and the whole of "le sort qui doit heal ne fait rien".

    Applying a skill's effects *replaced* the list, so frenzyshout put its life leech
    on and the very next angrystrike — which grants a frenzy buff of its own — wiped it
    before it could ever pay out.
    """
    world, sender = a_player()
    world.rules.mob_damage = 1000.0
    # angrystrike's own frenzy buffs read C:0.0, so without this it grants nothing and
    # there is no second cast to be wiped by.
    world.rules.force_effects = True
    a_creature(world)

    swing(world, sender, "frenzyshout")
    assert {effects.effect(b[0]).id for b in world.player(sender).buffs} == {
        "skill_frenzyshout_buff_lifeleech",
        "skill_frenzyshout_buff_armor",
        "skill_frenzyshout_buff_resistance",
    }

    swing(world, sender, "angrystrike")
    carried = {effects.effect(b[0]).id for b in world.player(sender).buffs}
    assert "skill_frenzyshout_buff_lifeleech" in carried, "survived the next cast"
    assert "skill_angrystrike_buff_frenzy_movementspeed" in carried


def test_a_life_leech_heals_a_share_of_what_was_dealt():
    """frenzyshout's LifeLeech:$0,absolute over eleven named skills, $0 of 0.2.

    Served here rather than by the client, because every effect that carries a
    mechanic like this is animated and an animated effect cannot be sent yet — there is
    not one in any capture to check against.
    """
    world, sender = a_player()
    world.rules.animated_effects = False
    world.rules.mob_damage = 1000.0
    world.player(sender).level = 100
    world.player(sender).max_health = 450_000.0
    world.player(sender).health = 400_000.0
    a_creature(world)

    swing(world, sender, "frenzyshout")
    before = world.player(sender).health
    swing(world, sender, "angrystrike")
    gained = world.player(sender).health - before
    # angrystrike is 1.25 of the blow, and the leech is a fifth of that.
    assert gained == 1000.0 * 1.25 * 0.2


def test_a_leech_does_not_heal_past_full_or_on_a_skill_it_does_not_cover():
    world, sender = a_player()
    world.rules.animated_effects = False
    world.rules.mob_damage = 1000.0
    player = world.player(sender)
    player.max_health = 2700.0
    player.health = 2700.0
    a_creature(world)

    swing(world, sender, "frenzyshout")
    swing(world, sender, "angrystrike")
    assert player.health == 2700.0, "already full"

    # battlecry is not in the leech's list of skills.
    player.health = 100.0
    modifier = next(
        m
        for m in effects.by_id("skill_frenzyshout_buff_lifeleech").starts
        if m.attribute == "LifeLeech"
    )
    assert "battlecry" not in modifier.targets
    assert "angrystrike" in modifier.targets


def test_a_poison_ticks_on_its_own_rate_for_its_own_duration():
    """debuff_dot_poison every 1.5 seconds for 2. Its damage is ours.

    The template reads $debuff_dot_poison_dmg, a named variable a talent sets, and
    there is no talent here — nor is the value in _Template_StatusEffectLevelModifier,
    which has nine columns and none about poison.
    """
    world, sender = a_player()
    world.rules.force_effects = True
    world.rules.mob_damage = 1000.0
    actor = a_creature(world)

    assert effects.by_id("debuff_dot_poison").over_time
    assert effects.by_id("debuff_dot_poison").tick_rate == 1.5
    assert not effects.by_id("skill_frenzyshout_buff_lifeleech").over_time

    swing(world, sender, "mightybash")
    assert len(world.pending_dots) == 1
    after_blow = world.creatures[actor].health

    for _ in range(3):
        world.pending_dots = [(0.0,) + e[1:] for e in world.pending_dots]
        world.land_dots()
    assert world.creatures[actor].health < after_blow, "the poison bit"
    # Bounded: it does not tick for ever.
    for _ in range(20):
        world.pending_dots = [(0.0,) + e[1:] for e in world.pending_dots]
        world.land_dots()
    assert not world.pending_dots


def test_a_poison_ticks_once_here_because_that_is_what_the_template_says():
    """Two seconds of duration at a rate of 1.5 is one tick, not two.

    Worth pinning: it looks like a rounding choice and it is the template's own
    arithmetic. debuff_dot_burn is 3 seconds at 1.0 and gets three.
    """
    world, sender = a_player()
    world.rules.force_effects = True
    world.rules.mob_damage = 10.0
    actor = a_creature(world, health=30.0)

    swing(world, sender, "mightybash")
    assert world.pending_dots[0][4] == 1, "one tick"

    world.pending_dots = [(0.0,) + e[1:] for e in world.pending_dots]
    world.land_dots()
    assert not world.pending_dots
    assert 0.0 < world.creatures[actor].health < 5.0, "bitten, not killed"

    assert effects.by_id("debuff_dot_burn").duration == 3.0
    assert effects.by_id("debuff_dot_burn").tick_rate == 1.0


def test_a_poison_that_kills_ends_the_creature_properly():
    world, sender = a_player()
    world.rules.force_effects = True
    world.rules.mob_damage = 10.0
    actor = a_creature(world, health=26.0)
    swing(world, sender, "mightybash")
    world.pending_dots = [(0.0,) + e[1:] for e in world.pending_dots]
    world.land_dots()
    assert world.creatures[actor].health == 0.0
    assert not world.pending_dots


def test_a_player_who_leaves_takes_their_poison_with_them():
    world, sender = a_player()
    world.rules.force_effects = True
    world.rules.mob_damage = 1000.0
    a_creature(world)
    swing(world, sender, "mightybash")
    assert world.pending_dots
    world.forget(sender)
    assert not world.pending_dots


def test_the_location_effect_family_is_kept():
    """The third list, which two earlier conclusions said had nothing to send.

    LocationStatusEffects is where earthquake and defiance keep everything they do, and
    a capture of the live service with every warrior skill cast contains the commands:
    NewLocationEffectCommand 0x003C twice, LocationEffectInfoCommand 0x003E fourteen
    times and DiscardLocationEffectCommand 0x003D four times.

    The string they carry is read as a string and nothing more. Calling it a shape was
    an over-read: the same string appears in ItemUpdateCommand and StatusEffectCommand
    in the same capture, and the client binary has SphereEffect once with no BoxEffect,
    CylinderEffect or ConeEffect anywhere -- a shape selector would have siblings.
    """
    from dsor.recorded import (
        LOCATION_EFFECT_DISCARD,
        LOCATION_EFFECT_INFO,
        LOCATION_EFFECT_NEW,
        LOCATION_EFFECT_STRING,
        location_effect,
        location_effect_string,
    )

    for name, opcode in (
        (LOCATION_EFFECT_NEW, 0x003C),
        (LOCATION_EFFECT_INFO, 0x003E),
        (LOCATION_EFFECT_DISCARD, 0x003D),
    ):
        message = location_effect(name)
        assert message[0] == 0x85
        assert int.from_bytes(message[1:3], "little") == opcode, name
        assert len(message) > 300, name

    assert location_effect_string(location_effect(LOCATION_EFFECT_NEW)) == (
        LOCATION_EFFECT_STRING
    )
    assert location_effect_string(location_effect(LOCATION_EFFECT_INFO)) == (
        LOCATION_EFFECT_STRING
    )
    # And something that is not one of these says so rather than guessing.
    assert location_effect_string(bytes([0x85, 0x5F, 0x00]) + bytes(20)) is None


def test_a_copied_element_gets_the_template_s_parameters_not_the_capture_s():
    """The difference between a buff and a debuff, and it showed as one.

    The captured element for skill_warshout_buff_movementspeed carries $0 = -0.4 where
    the skill's own template says +0.4, so copying it whole gave a forty percent *slow*.
    The effect applied perfectly and in the wrong direction.

    Parameters are safe to write, unlike the rest of the element: their layout is a
    32-bit count and that many float32, which is established. So the rule is copy what
    is not understood and write what is.
    """
    import struct

    from dsor.elements import element
    from dsor.recorded import element_parameters, real_element
    from raknet.bitstream import BitReader

    wire = effects.wire_of("skill_warshout_buff_movementspeed")
    _span, captured = element(wire)
    at, count = element_parameters(captured)
    assert count == 5
    reader = BitReader(captured, at)
    original = [
        struct.unpack("<f", reader.read_uint(32).to_bytes(4, "little"))[0]
        for _ in range(count)
    ]
    assert original[0] == pytest.approx(-0.4), "what the wire carried"

    written = real_element(wire, 41230, 10.0, [0.4, 0.0, 0.0, 0.0, 0.0])
    reader = BitReader(written, at)
    now = [
        struct.unpack("<f", reader.read_uint(32).to_bytes(4, "little"))[0]
        for _ in range(count)
    ]
    assert now[0] == pytest.approx(0.4), "what the template says"


def test_the_whole_path_sends_the_buff_with_the_sign_the_template_gives():
    from dsor.recorded import status_effect_indices, walk_elements
    from dsor.skills import wire_of as skill_wire
    import struct
    from raknet.bitstream import BitReader

    world, sender = a_player()
    world.player(sender).server_tick = 41230
    world.resolve_attack(sender, skill_wire("warshout"))
    world._drain()
    world._tick_pair(sender)
    state = next(
        payload for _a, payload in world._drain() if payload[1:3] == b"\x4f\x00"
    )
    found, _actor = walk_elements(state)
    body = state[3:]
    wanted = effects.wire_of("skill_warshout_buff_movementspeed")
    speed = next((f for f in found if f[0] == wanted), None)
    assert speed is not None, "the movement buff was not sent"

    reader = BitReader(body, speed[1])
    reader.read_uint(16)
    for _ in range(8):
        reader.read_uint(32)
    flags = [reader.read_bool() for _ in range(4)]
    assert flags[3]
    count = reader.read_uint(32)
    first = struct.unpack("<f", reader.read_uint(32).to_bytes(4, "little"))[0]
    assert first == pytest.approx(0.4), f"sent {first}, the template says 0.4"


def test_dragon_hide_is_six_auras_and_none_of_them_heal():
    """Asked three times whether defiance heals, and the answer is no, with evidence.

    Following every effect reachable from defiance -- including the ones named by other
    effects rather than by the skill, which an earlier check missed -- reaches 57 of them
    and not one contains CurrHealthPoints. Its own six are all Groups=Aura and carry no
    modifiers at all, so what they do lives in the aura mechanism and the numbers come
    from the skill's own $0, $1 and $2:

        skill_defiance_enemies_movement_aura        $0 = -0.4
        skill_defiance_enemies_damage_buff_aura     $1 = 0.02, $2 = 0.05
        skill_defiance_buff_creators_aura           $0 = -1.0

    Which is why serving it through the actor's effect list does nothing: an aura is not
    on an actor. It needs NewLocationEffectCommand 0x003C, and there is no encoder for
    that.

    The heal is a talent. Every healing effect in the warrior's tree says so in its own
    name -- warrior_healing_talent, warrior_battlecry_healing_talent,
    warrior_talent_ta_mighty360_heal, skill_spikedshield_heal -- and the one that is not
    talent-named, warrior_block_heal, heals on a block that defiance does not grant: it
    modifies no Block, no Armor and no Resistance anywhere.
    """
    from dsor.skills import wire_of

    # Nothing lands on the actor, which is what this server can send.
    assert effects.granted_by(wire_of("defiance")) == ()
    assert effects.inflicted_by(wire_of("defiance")) == ()

    for name in (
        "skill_defiance_enemies_movement_aura",
        "skill_defiance_enemies_damage_buff_aura",
        "skill_defiance_buff_creators_aura",
    ):
        aura = effects.by_id(name)
        assert aura is not None, name
        assert aura.groups == "Aura", name
        assert not aura.starts and not aura.ticks, f"{name} carries no modifier"
        assert not aura.changes_anything, name

    # And the heals are elsewhere, named for what gates them.
    for name in (
        "warrior_healing_talent",
        "warrior_battlecry_healing_talent",
        "warrior_talent_ta_mighty360_heal",
    ):
        healer = effects.by_id(name)
        if healer is None:
            continue
        assert "CurrHealthPoints" in (
            healer.start_modifiers + healer.tick_modifiers
        ), name
        assert "talent" in name


def test_a_built_element_reproduces_the_real_ones_it_can_be_checked_against():
    """The test that makes building elements defensible again.

    Three earlier readings of these fields each came from the one sample in front of me
    and each was wrong. The constants a built element uses now come from the 26 real
    elements of the 477-bit form, and this rebuilds every one of them from its own ticks,
    parameters, caster and instance handle, and compares field by field.

    Twenty-two come back identical. The four that do not differ in field 4 alone, which
    is zero in 22 of the 26 and small and unexplained in the rest -- so the remaining
    unknown is one field, bounded, rather than a shrug.
    """
    import struct

    from dsor.elements import ELEMENTS
    from dsor.recorded import built_element
    from raknet.bitstream import BitReader

    def read(bits):
        reader = BitReader(bits, 0)
        index = reader.read_uint(16)
        integers = [reader.read_uint(32) for _ in range(8)]
        flags = [reader.read_bool() for _ in range(4)]
        if not flags[3]:
            return index, integers, flags, None, None
        count = reader.read_uint(32)
        parameters = [
            struct.unpack("<f", reader.read_uint(32).to_bytes(4, "little"))[0]
            for _ in range(count)
        ]
        reader.read_bool()
        tail = reader.read_uint(8)
        return index, integers, flags, parameters, tail

    same = differ = 0
    fields_that_differ = set()
    for wire, (span, bits) in ELEMENTS.items():
        if span != 477:
            continue
        index, integers, flags, parameters, tail = read(bits)
        assert index == wire and tail == 0xFF
        made = built_element(
            wire,
            start_tick=integers[3],
            seconds=(integers[1] - integers[3]) / 25 if integers[1] else 0.04,
            parameters=parameters,
            source=integers[7].to_bytes(4, "little"),
            instance=integers[0],
        )
        _i, mine, myflags, _p, _t = read(made)
        if mine == integers and myflags == flags:
            same += 1
        else:
            differ += 1
            fields_that_differ |= {
                i for i, (a, b) in enumerate(zip(mine, integers)) if a != b
            }

    assert same + differ >= 13, "the committed corpus holds the 477-bit form"
    assert same >= differ * 3, f"{same} identical against {differ} not"
    assert fields_that_differ <= {4}, (
        f"fields {sorted(fields_that_differ)} differ, expected only 4"
    )


def test_every_effect_can_be_served_now_even_without_a_capture():
    """34 effects were captured and the game has 6703.

    An effect with no captured element is built rather than dropped, so the answer to
    "no effect at all" is no longer "the capture does not have it".
    """
    from dsor.recorded import (
        status_effect_count,
        status_effect_indices,
        status_effects_message,
    )

    # debuff_cc_stun and debuff_dot_poison have no captured element.
    from dsor.elements import element

    wires = [effects.wire_of("debuff_cc_stun"), effects.wire_of("debuff_dot_poison")]
    assert all(element(w) is None for w in wires)

    sent = status_effects_message(
        [(w, [0.4, 0.0, 0.0, 0.0, 0.0], 41230, 5.0) for w in wires],
        b"\x86\x00\x01\x00",
        source=b"\x08\x00\x01\x00",
        instance=0x00010200,
    )
    assert status_effect_count(sent) == 2
    assert status_effect_indices(sent) == wires
