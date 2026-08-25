"""Status effects: what a skill grants, and how it reaches the client."""

import time

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
    carried = status_effect_index(state)
    assert effects.effect(carried).id == "skill_warshout_buff_movementspeed"
    assert abs(status_effect_parameters(state)[0] - 0.4) < 1e-6


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
    assert status_effect_count(state) == 4
    assert [effects.effect(w).id for w in status_effect_indices(state)] == [
        entry.effect for entry in granted
    ]


def test_one_effect_at_the_recorded_values_reproduces_the_recording():
    """The strongest check available on the whole layout.

    If the flag, the count, the element length, the actor and the terminator are all
    right, then building a message from the recorded element with the recorded values
    must give back the recording byte for byte. It does.
    """
    from dsor.recorded import status_effect_fields, status_effects_message

    fields = status_effect_fields()
    built = status_effects_message(
        [(1350, [0.0] * 5, fields[3], 1.0)], b"\x15\x00\x01\x00"
    )
    assert built == tick_state()


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


def test_an_inflicted_effect_is_addressed_to_the_creature():
    """Which actor the message names is the only thing that decides who it lands on."""
    from dsor.gameplay import Position
    from dsor.recorded import status_effect_actor, status_effect_indices
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
    world.inflict_effects(actor, __import__("dsor.skills", fromlist=["by_id"]).by_id("mightybash"))
    assert world.creatures[actor].effects, "something landed on it"

    world._drain()
    world._tick_pair(sender)
    sent = [payload for _address, payload in world._drain()]
    addressed = [p for p in sent if len(p) > 3 and p[1:3] == b"\x4f\x00"]
    assert any(status_effect_actor(p) == actor for p in addressed), "on the creature"
    on_creature = next(p for p in addressed if status_effect_actor(p) == actor)
    names = [effects.effect(w).id for w in status_effect_indices(on_creature)]
    assert "debuff_dot_poison" in names


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
    # The five this server does not understand are left exactly as recorded.
    recorded = status_effect_fields()
    for index in (0, 2, 4, 6, 7):
        assert fields[index] == recorded[index], index


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
    served = {
        entry.effect for entry in world._entries(by_id("warshout"), victim=False)
    }
    assert {e for a, e in promised["warshout"] if a == "UserEffect"} == served

    # Every one of defiance's is a LocationEffect, so there is nothing to put on the
    # character at all.
    assert all(a == "LocationEffect" for a, _ in promised["defiance"])
    assert not world._entries(by_id("defiance"), victim=False)
