"""The skills as the player names them, against what the server does with them.

Every test here exists because a report was dismissed by looking at the wrong skill.
The internal ids were matched to the names on screen by guessing, and the guesses were
wrong in the one case that mattered most: "Dragon Hide" is ``frenzyshout``, while
``defiance`` -- which is what was investigated, at length, and declared not to heal --
is called "Banner of War". ``frenzyshout`` leeches life on every hit.

So these are written from the client's own locale file outwards: take the name the
player says, resolve it, and check the mechanic.
"""

import time

import pytest

from dsor import effects
from dsor.skills import by_id, wire_of
from dsor.titles import id_of, title_of

from test_afflictions import a_world_with_a_creature, strike_now, swing_now


def test_the_names_come_from_the_client_not_from_guesswork():
    """The four the operator named, and the one that was confused with another."""
    assert id_of("Dragon Hide") == "frenzyshout"
    assert id_of("Banner of War") == "defiance"
    assert id_of("Ground Breaker") == "seismicslam"
    assert id_of("Fury of the Dragon") == "earthquake"
    assert id_of("Iron Brow") == "laceratingstrike"
    assert id_of("Furious Battle Cry") == "warshout"
    # And the pair that caused it: two different skills, neither one the other.
    assert id_of("Dragon Hide") != id_of("Banner of War")
    assert title_of("frenzyshout") == "Dragon Hide"


def test_dragon_hide_heals_when_you_hit():
    """"le dragon hide devrait me heal" -- and it does, through a life leech.

    frenzyshout grants skill_frenzyshout_buff_lifeleech, whose modifier is
    ``LifeLeech:$0,absolute`` naming eleven skills, with $0 = 0.2 from the skill's own
    entry. So the heal is a fifth of the damage dealt, and only with those eleven.

    The reason this was denied for so long is in the test above: the skill examined
    was defiance, whose chain holds no CurrHealthPoints modifier anywhere. That was
    true, and about the wrong skill.
    """
    world, sender, _actor = a_world_with_a_creature()
    player = world.player(sender)
    player.max_health = 2700.0
    player.health = 100.0

    swing_now(world, sender, id_of("Dragon Hide"))
    held = {effects.effect(b[0]).id for b in player.buffs}
    assert "skill_frenzyshout_buff_lifeleech" in held, held

    leech = effects.by_id("skill_frenzyshout_buff_lifeleech")
    modifier = next(m for m in leech.starts if m.attribute == "LifeLeech")
    assert "angrystrike" in modifier.targets
    assert next(b for b in player.buffs if b[0] == leech.wire)[1][0] == pytest.approx(0.2)

    before = player.health
    swing_now(world, sender, "angrystrike")
    assert player.health > before, "Dragon Hide did not heal"


def test_dragon_hide_heals_only_with_the_skills_it_names():
    world, sender, _actor = a_world_with_a_creature()
    player = world.player(sender)
    player.max_health = 2700.0

    swing_now(world, sender, id_of("Dragon Hide"))
    leech = effects.by_id("skill_frenzyshout_buff_lifeleech")
    named = next(m for m in leech.starts if m.attribute == "LifeLeech").targets
    outsider = next(
        skill.id
        for skill in (by_id("mighty360"), by_id("battlecry"), by_id("spikedShield"))
        if skill.id not in named
    )
    player.health = 100.0
    before = player.health
    swing_now(world, sender, outsider)
    assert player.health == before, f"{outsider} is not in the leech's list"


def test_ground_breaker_stuns_and_breaks_armour():
    """"le ground breaker ne stun pas ni break armor" -- it did neither, and why.

    seismicslam carries ``debuff_cc_stun,C:1.0`` and its tooltip names
    ``skill_seismicslam_debuff_armor``, whose C: reads 0.0. Making the tooltip the sole
    authority served the armour break and *dropped the stun*, because a rule meant to
    add on the tooltip's word was also subtracting on its silence.
    """
    world, sender, actor = a_world_with_a_creature()
    creature = world.creatures[actor]

    inflicted = {e.effect for e in world._entries(by_id(id_of("Ground Breaker")), victim=True)}
    assert "debuff_cc_stun" in inflicted, inflicted
    assert "skill_seismicslam_debuff_armor" in inflicted, inflicted

    world.inflict_effects(actor, by_id(id_of("Ground Breaker")))
    on_it = {effects.effect(e[0]).id for e in creature.effects}
    assert "debuff_cc_stun" in on_it and "skill_seismicslam_debuff_armor" in on_it

    world.advance_creatures(tick=100)
    assert creature.condition.helpless, creature.condition.describe()
    assert creature.condition.damage_taken > 1.0, creature.condition.describe()


def test_iron_brow_breaks_armour():
    """The skill the operator calls "headbutt": Iron Brow, laceratingstrike."""
    world, sender, actor = a_world_with_a_creature()
    creature = world.creatures[actor]
    world.inflict_effects(actor, by_id(id_of("Iron Brow")))
    on_it = {effects.effect(e[0]).id for e in creature.effects}
    assert on_it == {
        "skill_laceratingstrike_debuff_armor",
        "skill_laceratingstrike_debuff_resistance",
    }, on_it
    world.advance_creatures(tick=100)
    assert creature.condition.damage_taken > 1.0, creature.condition.describe()


def test_charge_and_bloody_wild_swing_keep_their_certain_effects():
    """Two more the tooltip is silent about, recovered by the same fix."""
    world, _sender, _actor = a_world_with_a_creature()
    assert {e.effect for e in world._entries(by_id(id_of("Charge")), victim=True)} == {
        "debuff_cc_stun"
    }
    assert "debuff_dot_bleeding" in {
        e.effect
        for e in world._entries(by_id(id_of("Bloody Wild Swing")), victim=True)
    }


def test_fury_of_the_dragon_is_all_ground_and_says_so():
    """"le fury of the dragon n'a pas de vfx" -- correct, and this is the reason.

    earthquake puts everything it does in LocationStatusEffects, its graphics
    included: skill_earthquake_graphics is a location effect like the rest. Location
    effects travel in NewLocationEffectCommand 0x003C, which this server has no
    encoder for, so the crater's *visuals* cannot be sent at all.

    What can be served is the mechanic, and it is: the aura's leaves are put on
    whatever stands inside its radius.
    """
    world, sender, actor = a_world_with_a_creature()
    creature = world.creatures[actor]

    quake = wire_of(id_of("Fury of the Dragon"))
    laid = effects.parse_entries(effects.SKILL_LOCATIONS[quake])
    assert any(e.effect == "skill_earthquake_graphics" for e in laid), "the VFX is here"
    # And nothing on the user or the victim lists, which is why it read as inert.
    assert not world._entries(by_id("earthquake"), victim=False)
    assert not world._entries(by_id("earthquake"), victim=True)

    swing_now(world, sender, "earthquake")
    on_it = {effects.effect(e[0]).id for e in creature.effects}
    assert "debuff_cc_stun" in on_it, on_it
    world.advance_creatures(tick=100)
    assert creature.condition.helpless, creature.condition.describe()


def _instances(payload):
    """(effect id, instance handle) for every element in a 0x004F."""
    from raknet.bitstream import BitReader

    from dsor.recorded import walk_elements

    found, actor = walk_elements(payload)
    out = []
    for index, at, _span in found:
        reader = BitReader(payload[3:], at)
        reader.read_uint(16)
        fields = [reader.read_uint(32) for _ in range(8)]
        out.append((effects.effect(index).id, fields[0]))
    return actor, out


def _sent(world, sender, skill):
    swing_now(world, sender, skill)
    world._drain()
    world._tick_pair(sender)
    return [p for _a, p in world._drain() if p[1:3] == b"\x4f\x00"]


def test_two_effects_in_one_message_never_share_an_instance():
    """The bug behind "les effets sont melanges", and it was mine.

    Field 0 of an element is the handle naming one application of an effect. The
    client's HandleStatusEffectCommand searches the actor's existing effects for one
    whose +0x10c matches it, and on a hit calls UpdateTimingOfEffectAtIndex rather than
    adding anything.

    That field was copied from the capture along with the rest of the element, and the
    captured values collide: debuff_cc_stun and skill_laceratingstrike_debuff_armor both
    carry 66058, warshout's angrystrike and mightybash buffs both carry 66057,
    frenzyshout's life leech shares 66066 with one of angrystrike's, and twenty effects
    share 65546. So Ground Breaker sent a stun and an armour break in one message, the
    client added the first and read the second as "extend the one you already have", and
    one of the two silently vanished. Every skill granting more than one effect lost
    some.
    """
    for shown in ("Ground Breaker", "Furious Battle Cry", "Dragon Hide"):
        world, sender, _actor = a_world_with_a_creature()
        world.player(sender).server_tick = 41230
        for payload in _sent(world, sender, id_of(shown)):
            _actor_id, pairs = _instances(payload)
            assert len(pairs) > 1, f"{shown} sends one effect only: {pairs}"
            handles = [handle for _name, handle in pairs]
            assert len(set(handles)) == len(handles), f"{shown}: {pairs}"


def test_the_captured_handles_really_do_collide():
    """So the fix above is not guarding an imaginary problem."""
    import collections

    from raknet.bitstream import BitReader

    from dsor.elements import ELEMENTS

    seen = collections.defaultdict(list)
    for wire, (_span, bits) in ELEMENTS.items():
        reader = BitReader(bits, 0)
        reader.read_uint(16)
        seen[[reader.read_uint(32) for _ in range(8)][0]].append(wire)
    shared = {k: v for k, v in seen.items() if len(v) > 1}
    assert shared, "the captured elements no longer collide, so this test is stale"

    stun = effects.wire_of("debuff_cc_stun")
    armour = effects.wire_of("skill_laceratingstrike_debuff_armor")
    assert any(stun in v and armour in v for v in shared.values()), (
        "the stun and the armour break used to share a handle"
    )


def test_re_sending_an_effect_keeps_its_handle_and_a_fresh_cast_does_not():
    """Stable while it runs, forgotten when it ends. Both halves matter.

    A changing handle would stack duplicates the client complains about; a handle
    remembered past the end would make a re-cast arrive as "update the timing of an
    effect you no longer have", which does nothing at all.
    """
    world, sender, actor = a_world_with_a_creature()
    world.player(sender).server_tick = 41230
    creature = world.creatures[actor]

    world.inflict_effects(actor, by_id(id_of("Iron Brow")))
    first = world._instance_of(actor, effects.wire_of("skill_laceratingstrike_debuff_armor"))
    assert world._instance_of(actor, effects.wire_of("skill_laceratingstrike_debuff_armor")) == first

    # Expire it, and let live_effects notice.
    creature.effects = [
        (wire, parameters, time.monotonic() - 1.0, seconds)
        for wire, parameters, _expires, seconds in creature.effects
    ]
    assert world.live_effects(creature) == []
    world.inflict_effects(actor, by_id(id_of("Iron Brow")))
    again = world._instance_of(actor, effects.wire_of("skill_laceratingstrike_debuff_armor"))
    assert again != first, "a fresh cast is a fresh application"


def test_two_creatures_get_their_own_handles():
    """One effect on two monsters is two applications, not one shared."""
    from dsor.gameplay import Position
    from dsor.world import Creature

    world, _sender, actor = a_world_with_a_creature()
    other = b"\x87\x00\x01\x00"
    twin = Creature(
        actor=other,
        record=world.creatures[actor].record,
        position=Position(0, 0, 0),
        health=100.0,
        max_health=100.0,
        blueprint="a0001_gen_anderworld_creature",
        described=True,
    )
    world.creatures[other] = twin
    wire = effects.wire_of("debuff_cc_stun")
    assert world._instance_of(actor, wire) != world._instance_of(other, wire)
