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
    # And nothing is sent. It used to fall back on the recorded 0x004F, which puts the
    # tutorial dungeon's heal on the player 25 times a second -- 1823 of the 1836
    # status-effect commands one session sent, against 91 for the whole of a live
    # session. Each is an add for an effect already present, and since a 0x004F is an
    # actor's whole effect list, the replay also wiped the buff applied a tick earlier.
    assert not [p for _a, p in world._drain() if p[1:3] == b"\x4f\x00"]


def test_the_switch_turns_the_whole_thing_off():
    world, sender = a_player()
    world.rules.status_effects = False
    world.resolve_attack(sender, wire_of("warshout"))
    assert world.player(sender).buffs == []
    world._drain()
    world._tick_pair(sender)
    assert not [p for _a, p in world._drain() if p[1:3] == b"\x4f\x00"]


def test_a_skill_whose_tooltip_promises_nothing_grants_nothing():
    """mightybash is the example, and the answer to "je mets le headbut, le mob n'est
    pas stun": its description names a damage range and no effect at all.

    The C: field that made this server send a stun anyway is not the chance it looks
    like -- seismicslam's armour break, which its description does name, is C:0.0 while
    the stun it does not name is C:1.0.
    """
    assert effects.promised_by(wire_of("mightybash"), "VictimEffect") == ()
    assert effects.promised_by(wire_of("mightybash"), "UserEffect") == ()
    world, sender = a_player()
    world.resolve_attack(sender, wire_of("mightybash"))
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
    # granted_by is the union of the tooltip's list and the C:1.0 entries, so it also
    # names ctfdropflag -- certain, and PvP capture-the-flag machinery. The union is
    # what recovers Ground Breaker's stun and Bloody Wild Swing's bleed, which the
    # tooltip is silent about; the noise it lets in is dropped by servable() one step
    # later, which is why the server's own list below is still exactly four.
    assert len(effects.granted_by(wire_of("warshout"))) == 5
    assert "ctfdropflag" in {e.effect for e in effects.granted_by(wire_of("warshout"))}
    assert not effects.by_id("ctfdropflag").servable(world.class_skills)
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
    # All four have a real element now. Reading the element table from every capture
    # rather than one is what did it: 76 effects instead of 34.
    assert len(have) == 4 and not built, "all four are captured now"
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


def test_what_a_warrior_inflicts_can_be_drawn_now():
    """The count that says how much of the warrior is actually served.

    It used to be one -- warshout's movement speed, the single effect the operator ever
    saw land -- and the reason was not the mechanic but the element table: it was read
    from one capture, which held 34 effects and none of the warrior's debuffs. Read
    from all of them it holds 76, and the stun, the poison and both armour breaks are
    among the new ones.

    What is still missing is a clean set, and knowing which is the point of this test:
    five LocationEffects (defiance's three, earthquake's two) need a message this server
    has no encoder for, and the rest are taunt auras with no real element anywhere.
    """
    from dsor.elements import element
    from dsor.skills import of_class, wire_of as skill_wire

    have, missing = [], []
    for skill in of_class("warrior"):
        for kind, name in effects.PROMISED.get(skill_wire(skill.id), ()):
            try:
                where = element(effects.wire_of(name))
            except Exception:
                where = None
            (have if where else missing).append((kind, name))

    assert len(have) == 14, sorted(name for _k, name in have)
    for name in (
        "skill_laceratingstrike_debuff_armor",
        "skill_seismicslam_debuff_armor",
        "skill_mightyswing_debuff_reduce_damage",
        "skill_warshout_buff_movementspeed",
        "skill_frenzyshout_buff_armor",
    ):
        assert any(n == name for _k, n in have), name

    # Every one still missing is a location effect or an aura, not a debuff.
    assert all(
        kind == "LocationEffect" or "aura" in name or "taunt" in name
        or name == "buff_resourceonhit"
        or name.startswith("skill_battlecry_debuff")
        or name == "skill_enragingleap_buff_attackspeed"
        for kind, name in missing
    ), sorted(missing)
    assert sum(1 for kind, _n in missing if kind == "LocationEffect") == 5

    # And the two the game itself puts on a monster, which this server also inflicts.
    assert element(effects.wire_of("debuff_cc_stun")) is not None
    assert element(effects.wire_of("debuff_dot_poison")) is not None


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

    # And what the aura *does* used to be invisible here: the generator collected what
    # the class skills name and nothing collected what an effect names in turn, so a
    # nested effect was simply absent -- 458 of the database's 6703. The client's own
    # database is loaded into memory now and the gap is closed.
    from dsor import database

    nested = effects.by_id("skill_defiance_buff_resource")
    if database.available():
        assert nested is not None, "the database carries every effect"
        assert effects.LOADED == 6703
        assert nested.start_modifiers, "and its modifiers with it"
    else:
        assert nested is None, "without the file, the generated snapshot stands"
        assert effects.LOADED == 0


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

def test_an_effect_with_no_captured_element_is_left_out():
    """The opposite of what this test used to say, and the opposite is what works.

    It used to assert that an effect with no captured element is *built* from corpus
    constants rather than dropped, on the reasoning that 34 captured effects against
    the game's 6703 made "only what is captured" mean almost nothing. The client
    disagreed in the quietest way there is: HandleStatusEffect has three bail-outs
    that return without a word, a forged or borrowed element takes one of them, and
    the effect neither drew nor complained.

    Two things changed. The table is read from every capture now, so the four effects
    below are all real -- the stun and the poison among them. And anything still
    missing is left out of the message instead of being faked.
    """
    from dsor.elements import element
    from dsor.recorded import (
        servable_effects,
        status_effect_count,
        status_effect_indices,
        status_effects_message,
    )

    wires = [
        effects.wire_of(name)
        for name in (
            "skill_warshout_buff_movementspeed",
            "debuff_cc_stun",
            "skill_frenzyshout_buff_armor",
            "debuff_dot_poison",
        )
    ]
    captured, built = servable_effects(wires)
    assert captured == wires and not built, "all four are real now"

    sent = status_effects_message(
        [(w, [0.4, 0.0, 0.0, 0.0, 0.0], 41230, 5.0) for w in wires],
        b"\x86\x00\x01\x00",
        source=b"\x86\x00\x01\x00",
        instance=0x00010200,
    )
    assert status_effect_count(sent) == 4
    assert status_effect_indices(sent) == wires

    # And one with nothing captured is **built**, not dropped and not borrowed.
    #
    # Dropping it meant 72 of the game's 6,703 effects could ever be served; borrowing
    # meant a spell drew another spell's aura. Building is the third option, and it
    # became available only once the constants were measured over the whole corpus
    # rather than off whichever handful was in front of me: 1,069 of the corpus's 1,455
    # no-vector elements are reproduced bit for bit from them.
    absent = effects.wire_of("skill_battlecry_debuff_resistance")
    assert element(absent) is None, "nothing captured for this one"
    built = status_effects_message(
        [(absent, [-0.1, 0.0, 0.0, 0.0, 0.0], 41230, 5.0, 66400)],
        b"\x86\x00\x01\x00",
        source=b"\x86\x00\x01\x00",
    )
    assert status_effect_count(built) == 1
    from dsor.recorded import walk_elements

    found, actor = walk_elements(built, strict=True)
    assert [i for i, _a, _s in found] == [absent]
    assert found[0][2] == 477, "the no-vector form"
    assert actor == int.from_bytes(b"\x86\x00\x01\x00", "little")


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

def test_the_stun_and_the_poison_are_real_now():
    """The two effects the operator kept asking for, and why they never showed.

    They were served by lending another effect's element, because the element table was
    read from a single capture -- the one taken with every warrior skill cast, which
    holds 34 effects and neither of these. Both are in the older tutorial sessions,
    addressed to actors 0x10085..0x1008c, so the live service does put them on monsters
    and the examples were on disk the whole time.
    """
    from dsor.elements import ELEMENTS, element
    from dsor.recorded import (
        status_effect_count,
        status_effect_fields,
        status_effect_indices,
        status_effects_message,
    )

    # 72, down from 76: four entries were fabrications the corrected grammar no longer
    # produces, and two more (warrior_spikedShield_buff, frenzyshout's life leech) came
    # back as the 477-bit elements the captures actually hold instead of invented 669s.
    # Fewer and true beats more and wrong.
    assert len(ELEMENTS) >= 72, "read from every capture, not one"
    wires = [effects.wire_of("debuff_cc_stun"), effects.wire_of("debuff_dot_poison")]
    assert all(element(w) is not None for w in wires), "real, not lent"

    monster = b"\x86\x00\x01\x00"
    sent = status_effects_message(
        [(w, [0.4, 0.0, 0.0, 0.0, 0.0], 41230, 5.0) for w in wires],
        monster,
        source=monster,
        instance=0x00010200,
    )
    assert status_effect_count(sent) == 2
    assert status_effect_indices(sent) == wires
    # Field 7 is the actor the effect is on, not the caster: it equals the addressed
    # actor in all 568 monster-addressed elements the captures hold.
    assert status_effect_fields(sent)[7] == int.from_bytes(monster, "little")


def test_an_effect_command_is_sent_on_change_and_not_every_tick():
    """"Failed to add actor effect ... Effect already present!", ten times a second.

    Which effects are on an actor is state, and sending it again does not restate it --
    the client tries to *add* each one and refuses the duplicate, loudly, for every
    chained effect a skill pulls in.

    The live service sent 91 status effect commands for 37 casts in one session, two or
    three per cast. This server sent one per actor per tick.
    """
    from dsor.recorded import status_effect_count, tick_state
    from dsor.skills import wire_of

    world, sender = a_player()
    world.player(sender).server_tick = 41230
    world.resolve_attack(sender, wire_of("warshout"))
    world._drain()

    recorded = tick_state()
    sent = 0
    for step in range(100):
        world.player(sender).server_tick = 41230 + step
        world._tick_pair(sender)
        for _address, payload in world._drain():
            if payload[1:3] != b"\x4f\x00" or payload == recorded:
                continue
            if status_effect_count(payload) > 0:
                sent += 1
    assert sent == 1, f"{sent} commands for one unchanging set of effects"


def test_a_change_is_sent_and_an_expiry_clears_the_memory():
    import time

    from dsor.recorded import status_effect_count, tick_state
    from dsor.skills import wire_of

    world, sender = a_player()
    player = world.player(sender)
    player.server_tick = 41230
    recorded = tick_state()

    def effect_commands():
        world._tick_pair(sender)
        return [
            payload
            for _a, payload in world._drain()
            if payload[1:3] == b"\x4f\x00"
            and payload != recorded
            and status_effect_count(payload) > 0
        ]

    world.resolve_attack(sender, wire_of("warshout"))
    world._drain()
    assert len(effect_commands()) == 1, "the new set goes out"
    assert effect_commands() == [], "and is not repeated"

    # A second skill changes the set, so it goes out again.
    world.resolve_attack(sender, wire_of("frenzyshout"))
    world._drain()
    assert len(effect_commands()) == 1

    # And when everything expires the memory is cleared, so the next cast is a change.
    player.buffs = [
        (wire, parameters, time.monotonic() - 1.0, seconds)
        for wire, parameters, _expires, seconds in player.buffs
    ]
    assert effect_commands() == [], "nothing running, nothing sent"
    world.resolve_attack(sender, wire_of("warshout"))
    world._drain()
    assert len(effect_commands()) == 1, "a fresh cast is a change again"

def test_every_promised_effect_of_every_warrior_skill_goes_out():
    """The end of it: what the descriptions name is what is sent."""
    from dsor.recorded import status_effect_count, status_effect_indices
    from dsor.skills import of_class

    world, sender = a_player()
    world.player(sender).level = 104
    world.player(sender).server_tick = 41230

    for skill in of_class("warrior"):
        promised = effects.promised_by(skill.wire, "UserEffect")
        if not promised:
            continue
        world.player(sender).buffs = []
        world._last_sent.clear()
        world.player(sender).resource = world.resource_pool(sender)
        world.resolve_attack(sender, skill.wire)
        world._drain()
        world._tick_pair(sender)
        state = next(
            (p for _a, p in world._drain() if p[1:3] == b"\x4f\x00"), None
        )
        if state is None:
            # Nothing servable, so nothing is sent -- battlecry, whose one promised
            # user effect is a taunt aura with no real element anywhere. Silence is the
            # right answer; the recorded 0x004F this used to fall back on put the
            # tutorial heal on the player instead.
            from dsor.elements import element

            for name in promised:
                assert element(effects.wire_of(name)) is None, f"{skill.id}: {name}"
            continue
        sent = {effects.effect(w).id for w in status_effect_indices(state)}
        # Everything promised that changes something. The two that do not are taunt
        # auras carrying no modifier at all -- like defiance's, their work is in the
        # aura mechanism and cannot be put on an actor.
        wanted = {
            name
            for name in promised
            if (found := effects.by_id(name)) is not None and found.changes_anything
        }
        assert wanted <= sent, f"{skill.id}: {wanted - sent} missing"
        for name in set(promised) - wanted:
            assert name.endswith("_aura"), f"{skill.id}: {name} dropped and not an aura"


def test_a_debuff_reaches_the_creature_it_is_put_on():
    """The end-to-end check that was missing while nothing worked.

    Every earlier test of the victim side stopped at ``creature.effects`` -- the
    bookkeeping -- or at "the count is not zero". None of them asked whether the effect
    that reached the wire was the one the skill promises, addressed to the creature,
    carrying an element the live service actually sent. That gap is how three warrior
    debuffs could be inflicted, logged, encoded and still draw nothing for weeks: they
    had no real element, so they were served a borrowed one, and the client discards a
    borrowed element without a word.
    """
    from dsor.elements import element
    from dsor.gameplay import Position
    from dsor.recorded import status_effect_indices, walk_elements
    from dsor.skills import by_id, of_class
    from dsor.world import Creature, World

    empty = World(name="t")
    empty.rules.mobs = 0

    checked = 0
    for skill in of_class("warrior"):
        # Driven by the path the server actually takes, not by PROMISED directly.
        # stuncharge is the reason: it has no tooltip row at all, so PROMISED is empty
        # for it, and its stun comes from the C:1.0 fallback instead. Reading the
        # tooltip table here would have tested everything except the stun.
        promised = [
            entry.effect
            for entry in empty._entries(skill, victim=True)
            if element(effects.wire_of(entry.effect)) is not None
        ]
        if not promised:
            continue

        world, sender = a_player()
        world.player(sender).level = 104
        world.player(sender).server_tick = 41230
        actor = b"\x86\x00\x01\x00"
        world.creatures[actor] = Creature(
            actor=actor,
            record=b"",
            position=Position(0, 0, 0),
            health=5_000_000.0,
            max_health=5_000_000.0,
            blueprint="a0001_gen_anderworld_creature",
            described=True,
        )
        world.inflict_effects(actor, by_id(skill.id))
        world._drain()
        world._tick_pair(sender)

        addressed = [
            payload for _a, payload in world._drain() if payload[1:3] == b"\x4f\x00"
        ]
        mine = [p for p in addressed if walk_elements(p)[1] == int.from_bytes(actor, "little")]
        assert mine, f"{skill.id}: nothing addressed to the creature"
        sent = {effects.effect(w).id for w in status_effect_indices(mine[0])}
        assert set(promised) <= sent, f"{skill.id}: {set(promised) - sent} missing"

        # And every element that travelled is **built**: 477 bits, no vectors, no
        # aura geometry from anybody else's capture. That last part is the fix. Five
        # effects in the captured table carried the same six floats, and 2.15 of them
        # is skill_earthquake_aura's own radius -- which is how casting Iron Brow drew
        # Fury of the Dragon's crater. Copying is gone.
        found, _actor = walk_elements(mine[0], strict=True)
        for index, _at, span in found:
            assert span == 477, f"{skill.id}: effect {index} is {span} bits, not built"
        checked += 1

    assert checked >= 6, f"only {checked} warrior skills had a servable victim effect"
    # And the stun among them, which is the one that was asked for by name.
    assert element(effects.wire_of("debuff_cc_stun")) is not None
    assert any(
        entry.effect == "debuff_cc_stun"
        for entry in empty._entries(by_id("stuncharge"), victim=True)
    ), "stuncharge stuns"


def test_the_captured_corpus_is_now_a_yardstick_and_not_a_source():
    """dsor/elements.py stays, but nothing is sent from it any more.

    It held 72 elements against the game's 6,703 effects, so copying could serve almost
    nothing -- and what it did serve carried the capture's own instance handle, actor and
    192 bits of aura geometry into a message about a different effect. Five of the 72
    shared the same six floats, and 2.15 of them is skill_earthquake_aura's radius,
    which is how casting Iron Brow drew Fury of the Dragon's crater.

    Rewriting those fields one at a time never fixed it: the rest of the element stayed
    foreign. So the copy path is gone, and the table's job is to *check* the builder.
    """
    import struct

    from dsor.elements import ELEMENTS
    from dsor.recorded import EFFECT_TICKS_PER_SECOND, build_element
    from raknet.bitstream import BitReader

    assert ELEMENTS, "the corpus is still committed"
    assert not hasattr(__import__("dsor.recorded", fromlist=["x"]), "real_element")
    assert not hasattr(__import__("dsor.recorded", fromlist=["x"]), "borrowed_element")

    checked = matched = 0
    for wire, (span, bits) in ELEMENTS.items():
        if span != 477:
            continue                      # only the no-vector form is built
        reader = BitReader(bits, 0)
        reader.read_uint(16)
        fields = [reader.read_uint(32) for _ in range(8)]
        flags = tuple(reader.read_bool() for _ in range(4))
        if not flags[3]:
            continue
        count = reader.read_uint(32)
        params = [
            struct.unpack("<f", reader.read_uint(32).to_bytes(4, "little"))[0]
            for _ in range(count)
        ]
        holder = fields[7].to_bytes(4, "little")
        seconds = fields[5] / EFFECT_TICKS_PER_SECOND
        _n, built = build_element(
            wire, fields[3], seconds, params, fields[0], holder
        )
        checked += 1
        if bytes(built) == bytes(bits):
            matched += 1
    assert checked >= 10, f"only {checked} no-vector elements to check against"
    # Two thirds rebuilt byte for byte. Every one that does not is a known minority
    # variant, and each is named rather than chased:
    #
    #   the third flag set   403 of 139,112 real elements, 0.29%
    #   field 4 == 15        1 of 76
    #   field 1 == 0         an effect with no end tick
    #
    # The builder writes the majority in each case, which is a stated choice.
    assert matched >= checked * 2 // 3, f"{matched} of {checked} rebuilt exactly"


def test_the_parameters_come_from_the_template_by_construction():
    """Not by rewriting a copy, which is how a +0.4 speed buff became a 40% slow.

    A captured element for skill_warshout_buff_movementspeed carries $0 = -0.4 where
    the skill's own template says +0.4. Copying it whole applied the effect perfectly
    and in the wrong direction. Building takes the parameters as given and there is
    nothing else in the element to disagree with them.
    """
    import struct

    from dsor import effects
    from dsor.recorded import build_element
    from raknet.bitstream import BitReader

    wire = effects.wire_of("skill_warshout_buff_movementspeed")
    _span, built = build_element(wire, 41230, 10.0, [0.4, 0.0, 0.0, 0.0, 0.0], 1,
                                 b"\x15\x00\x01\x00")
    reader = BitReader(built, 16 + 32 * 8 + 4 + 32)
    got = struct.unpack("<f", reader.read_uint(32).to_bytes(4, "little"))[0]
    assert got == pytest.approx(0.4), "the template's sign, not the capture's"


def test_the_switch_silences_every_skill_the_operator_tests():
    """The discriminating experiment for "je lance dragon hide j'ai spike shield".

    With the switch off this server puts nothing on any actor and sends no 0x004F at
    all -- not for the player, not for a creature, not for an aura. So if those two
    effects still appear on screen, they do not come from this server's effect code,
    and the search moves elsewhere. That is the whole point of asserting it here
    rather than reasoning about it: the four flag checks sit on the *application*
    paths, and it is the emptiness of the wire that matters, not their placement.
    """
    for name in (
        "frenzyshout",       # Dragon Hide
        "spikedShield",      # Spike Shield
        "warshout",          # Furious Battle Cry -- grants Power of Smash
        "seismicslam",       # Ground Breaker
        "laceratingstrike",  # Iron Brow
        "earthquake",        # Fury of the Dragon
        "battlecry",         # Outburst
        "defiance",          # Banner of War
    ):
        world, sender = a_player()
        world.rules.status_effects = False
        creature = next(iter(world._ready().creatures.values()), None)
        world.resolve_attack(sender, wire_of(name))
        assert world.player(sender).buffs == [], name
        if creature is not None:
            assert not creature.effects, name
        world._drain()
        world._tick_pair(sender)
        sent = [p for _a, p in world._drain() if p[1:3] == b"\x4f\x00"]
        assert not sent, f"{name} sent {len(sent)} status-effect command(s)"
