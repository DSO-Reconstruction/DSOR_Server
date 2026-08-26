"""The half of an effect the server owns.

Everything here would have failed for the whole life of this project, because none of
it existed: the effects were encoded, sent and logged, and nothing in the simulation
read them. A stun was an icon on a creature that kept walking and kept swinging.

The asymmetry is the reason it went unnoticed for so long. The client owns the
*player's* movement, so a speed buff on the player worked with no server support at
all -- and that one working case was taken as evidence the mechanism was right.
"""

import time

import pytest

from dsor import effects
from dsor.afflictions import UNAFFECTED, Condition, condition_of
from dsor.gameplay import Position
from dsor.skills import by_id, wire_of
from dsor.world import Creature, World


def a_world_with_a_creature(health: float = 5_000_000.0):
    world = World(name="t")
    world.rules.mobs = 0
    sender = ("127.0.0.1", 1)
    player = world.player(sender)
    player.position = Position(0, 0, 0)
    player.in_world = True
    player.health = player.max_health = 2700.0
    player.level = 104
    player.resource = 100.0
    # A real 20-byte entity record, because advance_creatures re-stamps it and
    # gameplay refuses anything that is not one.
    from dsor.recorded import combat_ready_mobs

    record = combat_ready_mobs()[0]
    creature = Creature.from_record(record, health)
    # A monster-range actor. The first combat-ready record carries 0x00010008, which
    # is in the player range, and a creature wearing a player's actor confuses every
    # lookup that decides who is hitting whom.
    creature.actor = b"\x86\x00\x01\x00"
    creature.blueprint = "a0001_gen_anderworld_creature"
    creature.described = True
    creature.attack_skill = 440
    creature.health = health
    creature.position = Position(0, 0, 0)
    world.creatures[creature.actor] = creature
    return world, sender, creature.actor


def strike_now(world):
    """Swing, and let the blow land: a creature's hit waits for its impact frame."""
    world.creatures_strike()
    world.pending_hits = [(0.0, *rest) for _at, *rest in world.pending_hits]
    world.land_hits()


def swing_now(world, sender, skill):
    """Use a skill and let it land: a player's blow waits for its hit frame too."""
    world.resolve_attack(sender, wire_of(skill))
    world.pending_swings = [(0.0, *rest) for _at, *rest in world.pending_swings]
    world.land_swings()
    world.pending_hits = [(0.0, *rest) for _at, *rest in world.pending_hits]
    world.land_hits()


def held(name: str, parameters=(0.0,) * 5):
    effect = effects.by_id(name)
    return [(effect.wire, parameters, effect.duration)]


def test_a_stun_is_read_off_the_template_not_guessed():
    """debuff_cc_stun is ActorFeature:off,Movement,RegularSkills. Both halves."""
    condition = condition_of(held("debuff_cc_stun"))
    assert not condition.moves and not condition.acts
    assert condition.helpless
    assert condition.describe() == "rooted, silenced"


def test_an_armour_break_raises_the_damage_taken():
    """Resistance:$0,relative,Physical with the -0.5 the wire actually carries."""
    condition = condition_of(
        held("skill_laceratingstrike_debuff_armor", (-0.5, 0.0, 0.0, 0.0, 0.0))
    )
    assert condition.damage_taken == pytest.approx(1.5)
    assert condition.moves and condition.acts, "it is a debuff, not a stun"


def test_a_damage_debuff_lowers_the_damage_dealt():
    """Damage:$0,relative,Min,Max. The real value is -0.05, not something rounder."""
    condition = condition_of(
        held("skill_mightyswing_debuff_reduce_damage", (-0.05, 0.0, 0.0, 0.0, 0.0))
    )
    assert condition.damage_dealt == pytest.approx(0.95)


def test_a_value_that_cannot_be_resolved_applies_nothing():
    """debuff_cc_frost names $debuff_cc_frost_movspeed, a constant living elsewhere.

    Its real element carries five zeroes, so the value is not on the wire either.
    Contributing nothing is right; contributing zero would be a 100% slow.
    """
    assert condition_of(held("debuff_cc_frost")) == UNAFFECTED


def test_effects_stack_by_multiplying():
    armour = effects.by_id("skill_laceratingstrike_debuff_armor")
    damage = effects.by_id("skill_mightyswing_debuff_reduce_damage")
    both = condition_of(
        [
            (armour.wire, (-0.5, 0, 0, 0, 0), 5.0),
            (damage.wire, (-0.05, 0, 0, 0, 0), 1.0),
        ]
    )
    assert both.damage_taken == pytest.approx(1.5)
    assert both.damage_dealt == pytest.approx(0.95)


def test_a_stunned_creature_does_not_move():
    world, sender, actor = a_world_with_a_creature()
    creature = world.creatures[actor]
    creature.position = Position(1000, 0, 1000)
    world.rules.mob_aggro = 10_000.0
    world.advance_creatures(tick=100)
    walked = creature.position
    assert walked != Position(1000, 0, 1000), "it chases when it can"

    creature.effects = [
        (effects.by_id("debuff_cc_stun").wire, (0.0,) * 5, time.monotonic() + 5.0, 5.0)
    ]
    world.advance_creatures(tick=200)
    stood = creature.position
    world.advance_creatures(tick=300)
    assert creature.position == stood, "a stunned creature stays where it is"


def test_a_stunned_creature_does_not_swing():
    world, sender, actor = a_world_with_a_creature()
    creature = world.creatures[actor]
    world.rules.creature_damage = 250.0
    before = world.player(sender).health

    world.advance_creatures(tick=100)
    strike_now(world)
    assert world.player(sender).health < before, "it hits when it can"

    creature.last_struck = 0.0
    creature.effects = [
        (effects.by_id("debuff_cc_stun").wire, (0.0,) * 5, time.monotonic() + 5.0, 5.0)
    ]
    world.advance_creatures(tick=200)
    hurt = world.player(sender).health
    for tick in range(300, 900, 100):
        creature.last_struck = 0.0
        world.advance_creatures(tick=tick)
        strike_now(world)
    assert world.player(sender).health == hurt, "a stunned creature does not swing"


def test_an_armour_break_is_worth_something_on_the_next_blow():
    world, sender, actor = a_world_with_a_creature()
    creature = world.creatures[actor]

    swing_now(world, sender, "laceratingstrike")
    plain = creature.max_health - creature.health
    assert plain > 0, "the first blow lands with no debuff on it"
    assert creature.effects, "and puts the armour break on"

    # The condition is recomputed once a tick, so the debuff pays from the next tick
    # on -- a debuff applied by a blow should not amplify that same blow.
    world.advance_creatures(tick=100)
    assert creature.condition.damage_taken > 1.0, creature.condition.describe()

    creature.health = creature.max_health
    swing_now(world, sender, "laceratingstrike")
    broken = creature.max_health - creature.health
    assert broken > plain, f"{broken} should exceed {plain}"
    assert broken == pytest.approx(plain * creature.condition.damage_taken, rel=0.02)


def test_a_damage_debuff_softens_what_the_creature_hits_for():
    world, sender, actor = a_world_with_a_creature()
    creature = world.creatures[actor]
    world.rules.creature_damage = 250.0

    world.advance_creatures(tick=100)
    strike_now(world)
    plain = 2700.0 - world.player(sender).health
    assert plain > 0

    world.player(sender).health = 2700.0
    creature.last_struck = 0.0
    creature.effects = [
        (
            effects.by_id("skill_mightyswing_debuff_reduce_damage").wire,
            (-0.05, 0.0, 0.0, 0.0, 0.0),
            time.monotonic() + 5.0,
            5.0,
        )
    ]
    world.advance_creatures(tick=200)
    strike_now(world)
    softened = 2700.0 - world.player(sender).health
    assert softened < plain, f"{softened} should be under {plain}"
    assert softened == pytest.approx(plain * 0.95, rel=0.02)


def test_the_switch_makes_every_effect_decoration_again():
    """Which is what the server did before this module, at every setting."""
    world, _sender, actor = a_world_with_a_creature()
    creature = world.creatures[actor]
    creature.effects = [
        (effects.by_id("debuff_cc_stun").wire, (0.0,) * 5, time.monotonic() + 5.0, 5.0)
    ]
    assert not world.condition(creature).moves
    world.rules.honour_effects = False
    assert world.condition(creature) == UNAFFECTED


def test_the_crater_stuns_and_burns():
    """earthquake, whose whole effect this server used to log as "not modelled".

    A skill has three effect lists and only two were ever read. earthquake's damage,
    its slow and its stun are all in the third -- LocationStatusEffects -- and each is
    reached through an aura that carries no modifiers of its own, so the chain has to
    be followed twice:

        skill_earthquake_shockwave_aura  radius 3.0, Enemies, $0 = -2.0
          -> debuff_cc_stun                 ActorFeature:off,Movement,RegularSkills
          -> skill_earthquake_shockwave_dmg CurrHealthPointsDmg:$0,relative,Fire
    """
    world, sender, actor = a_world_with_a_creature()
    creature = world.creatures[actor]
    assert not creature.effects

    swing_now(world, sender, "earthquake")
    landed = {effects.effect(e[0]).id for e in creature.effects}
    assert "debuff_cc_stun" in landed, landed
    assert "skill_earthquake_debuff_movementspeed" in landed, landed

    # The stun keeps its own five seconds, not the entry's D:0.0 nor a flat one.
    stun = effects.by_id("debuff_cc_stun").wire
    seconds = next(e[3] for e in creature.effects if e[0] == stun)
    assert seconds == pytest.approx(5.0)

    world.advance_creatures(tick=100)
    assert creature.condition.helpless, creature.condition.describe()

    # And it burns: damage over time, scheduled through the same queue a poison uses.
    assert world.pending_dots, "the crater does no damage"
    before = creature.health
    world.pending_dots = [(0.0, *rest) for _at, *rest in world.pending_dots]
    world.land_dots()
    assert creature.health < before, "the crater burns"


def test_a_creature_out_of_the_crater_is_untouched():
    world, sender, actor = a_world_with_a_creature()
    creature = world.creatures[actor]
    creature.position = Position(100_000, 0, 100_000)
    swing_now(world, sender, "earthquake")
    assert not creature.effects, "an aura has a radius"


def test_dragon_hide_makes_every_skill_free_and_does_not_heal():
    """What defiance actually does, against what it is remembered as doing.

    Its own effect list is entirely C:0.0 -- items, talents and set bonuses. What it
    really does is one location aura on the caster:

        skill_defiance_buff_creators_aura  radius 5.75, Creator, $0 = -1.0
          -> skill_defiance_buff_resource_reg  SkillResourceRegeneration:5.0,absolute
          -> skill_defiance_buff_skill_costs   ResourceCost:$0,relative,player_skills

    So: five resource a second, and every skill free for ten seconds. There is no
    CurrHealthPoints modifier anywhere in the chain, on any of the effects it can
    reach. It does not heal, and three separate readings of the database agree.
    """
    world, sender, actor = a_world_with_a_creature()
    player = world.player(sender)
    pool = world.resource_pool(sender)

    costly = by_id("mighty360")
    assert costly.resource_cost > 0.0
    player.resource = pool
    world.spend_resource(sender, costly)
    assert player.resource < pool, "it costs something normally"

    swing_now(world, sender, "defiance")
    assert player.buffs, "Dragon Hide puts something on the caster"
    laid = {effects.effect(b[0]).id for b in player.buffs}
    assert laid == {
        "skill_defiance_buff_resource_reg",
        "skill_defiance_buff_skill_costs",
    }, laid

    condition = world.condition(player)
    assert condition.resource_cost == pytest.approx(0.0), condition.describe()
    assert condition.resource_regen == pytest.approx(5.0)

    player.resource = pool
    world.spend_resource(sender, costly)
    assert player.resource == pytest.approx(pool), "and free under Dragon Hide"

    # And it regenerates rather than healing.
    player.resource = 0.0
    for _ in range(25):
        world.regenerate(player)
    assert player.resource == pytest.approx(5.0, rel=0.05), "five a second"

    for name in laid:
        reachable = effects.by_id(name)
        assert all(
            "CurrHealthPoints" not in modifier.attribute
            for modifier in reachable.starts + reachable.ticks
        ), f"{name} heals after all"
