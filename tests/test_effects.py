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
    assert player.resource == 0.6 * world.rules.player_resource

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
    for _ in range(20):
        world.resolve_attack(sender, wire_of("warshout"))
    assert player.resource == world.rules.player_resource

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


def test_the_buff_stops_when_it_runs_out():
    world, sender = a_player()
    world.resolve_attack(sender, wire_of("warshout"))
    player = world.player(sender)
    assert player.buff is not None

    # Ten seconds is what warshout asks for; put it in the past.
    wire, parameters, _ = player.buff
    player.buff = (wire, parameters, time.monotonic() - 1.0)
    assert world.live_buff(sender) is None
    assert player.buff is None

    world._drain()
    world._tick_pair(sender)
    state = world._drain()[0][1]
    assert status_effect_index(state) == 1350, "back to the recording"


def test_the_switch_turns_the_whole_thing_off():
    world, sender = a_player()
    world.rules.status_effects = False
    world.resolve_attack(sender, wire_of("warshout"))
    assert world.player(sender).buff is None
    world._drain()
    world._tick_pair(sender)
    assert status_effect_index(world._drain()[0][1]) == 1350


def test_a_skill_that_grants_nothing_leaves_the_state_alone():
    world, sender = a_player()
    world.resolve_attack(sender, wire_of("angrystrike"))
    assert world.player(sender).buff is None


def test_only_one_effect_can_be_carried_and_that_is_the_messages_fault():
    """A limit worth stating rather than hiding.

    The recorded 0x004F holds exactly one effect element, and three of the eight
    integers in it are not understood, so a second cannot be built. warshout grants
    four and one is served.
    """
    from dsor.recorded import EFFECT_PARAMETERS

    world, sender = a_player()
    granted = effects.granted_by(wire_of("warshout"))
    assert len(granted) == 4
    world.resolve_attack(sender, wire_of("warshout"))
    assert world.player(sender).buff[0] == effects.wire_of(granted[0].effect)
    assert len(world.player(sender).buff[1]) == EFFECT_PARAMETERS
