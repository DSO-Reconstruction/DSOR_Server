"""Each skill is its own shape, and the differences are the client's own numbers."""

from dsor import skills
from dsor.gameplay import Position, heading_to
from dsor.world import Creature, World


def a_world() -> World:
    world = World(name="t")
    world.rules.mobs = True
    world.rules.mob_max_health = 1000.0
    world.rules.mob_damage = 10.0  # a flat blow, so a multiplier is visible
    return world


def stand(world: World, tag: int, x: float, y: float) -> bytes:
    """Put a creature at *(x, y)* in world units and return its actor."""
    actor = bytes([tag, 0, 1, 0])
    world.creatures[actor] = Creature(
        actor=actor,
        record=b"",
        position=Position(x * 128.0, 0.0, y * 128.0),
        health=1000.0,
        max_health=1000.0,
        blueprint="a0001_gen_anderworld_creature",
        described=True,
    )
    return actor


def a_player(world: World, heading: int = 0) -> tuple[str, int]:
    sender = ("127.0.0.1", 1)
    player = world.player(sender)
    player.position = Position(0.0, 0.0, 0.0)
    player.health = 236.0
    player.max_health = 236.0
    player.level = 15
    player.heading = heading
    player.in_world = True
    return sender


def test_the_three_warrior_skills_are_three_different_shapes():
    """This is the bug: they were one shape, and two of them did nothing.

    The client sends SkillCommand for a skill with no target and TargetSkillCommand
    for one with a target, and a server that only handled the second ignored 17 of
    18 swings in a session. Handling both is half the fix; the other half is that
    they are not the same blow.
    """
    strike = skills.by_id("angrystrike")
    cleave = skills.by_id("mightyswing")
    spin = skills.by_id("mighty360")

    assert (strike.targeting, cleave.targeting, spin.targeting) == (
        "Actor", "Angle", "Radius",
    )
    assert (strike.arc, cleave.arc, spin.arc) == (360.0, 170.0, 360.0)
    assert not strike.area and cleave.area and spin.area
    assert (strike.damage_modifier, cleave.damage_modifier, spin.damage_modifier) == (
        1.25, 1.0, 1.5,
    )
    # And their blows land on different frames, so one hit delay cannot serve all.
    assert (strike.hit_frame, cleave.hit_frame, spin.hit_frame) == (5, 5, 7)


def test_a_single_target_skill_hits_one_creature_of_three():
    world = a_world()
    sender = a_player(world)
    for tag, y in ((0x85, 1.0), (0x86, 1.2), (0x87, 1.4)):
        stand(world, tag, 0.0, y)

    world.resolve_attack(sender, skills.wire_of("angrystrike"))
    hurt = [c for c in world.creatures.values() if c.health < 1000.0]
    assert len(hurt) == 1, "Actor targeting strikes one creature"
    # 1.25 times the blow, which is the skill's own multiplier.
    assert hurt[0].health == 1000.0 - 12.5


def test_a_radius_skill_hits_everything_around_including_behind():
    world = a_world()
    sender = a_player(world, heading=0)  # facing +y
    stand(world, 0x85, 0.0, 3.0)   # in front
    stand(world, 0x86, 0.0, -3.0)  # behind
    stand(world, 0x87, 3.0, 0.0)   # to the side

    world.resolve_attack(sender, skills.wire_of("mighty360"))
    hurt = [c for c in world.creatures.values() if c.health < 1000.0]
    assert len(hurt) == 3, "Radius targeting spares nobody in range"
    assert all(c.health == 1000.0 - 15.0 for c in hurt), "1.5 times the blow"


def test_an_arc_skill_spares_what_is_behind_the_player():
    """mightyswing cuts 170 degrees, so 175 degrees away is outside it."""
    world = a_world()
    sender = a_player(world, heading=0)  # facing +y
    front = stand(world, 0x85, 0.0, 2.0)
    back = stand(world, 0x86, 0.0, -2.0)

    world.resolve_attack(sender, skills.wire_of("mightyswing"))
    assert world.creatures[front].health == 1000.0 - 10.0, "in the arc"
    assert world.creatures[back].health == 1000.0, "behind the swing"


def test_the_arc_follows_where_the_player_looks():
    """Turn around and the same skill hits the other creature.

    Without a facing there is no arc: the cone pointed one fixed way whatever the
    player did.
    """
    for heading, hit, spared in ((0, 0x85, 0x86), (128, 0x86, 0x85)):
        world = a_world()
        sender = a_player(world, heading=heading)
        stand(world, 0x85, 0.0, 2.0)
        stand(world, 0x86, 0.0, -2.0)
        world.resolve_attack(sender, skills.wire_of("mightyswing"))
        assert world.creatures[bytes([hit, 0, 1, 0])].health < 1000.0
        assert world.creatures[bytes([spared, 0, 1, 0])].health == 1000.0


def test_the_arc_is_measured_in_the_wires_own_heading_units():
    """The comparison is in 256ths of a turn, not degrees, to avoid a conversion.

    Facing +y is heading 0, and a creature straight ahead must read as no offset at
    all — otherwise every cone is aimed slightly wrong.
    """
    assert heading_to(0.0, 1.0) == 0
    assert heading_to(0.0, -1.0) == 128
    assert 170.0 / 360.0 * 256 / 2 == 60.444444444444443


def test_a_longer_skill_reaches_further_than_a_shorter_one():
    """mighty360 reaches 6.3 units and angrystrike 1.75. Both plus the same slack."""
    world = a_world()
    sender = a_player(world)
    far = stand(world, 0x85, 0.0, 6.0)

    world.resolve_attack(sender, skills.wire_of("angrystrike"))
    assert world.creatures[far].health == 1000.0, "beyond 1.75 + 1.75"

    world.resolve_attack(sender, skills.wire_of("mighty360"))
    assert world.creatures[far].health < 1000.0, "within 6.3 + 1.75"


def test_angrystrike_still_reaches_exactly_as_far_as_before():
    """The slack is chosen so the one skill that worked keeps working.

    3.5 was tuned by hand against real wire positions and confirmed in play. If the
    per-skill range moved it, this change would have fixed two skills and broken one.
    """
    world = a_world()
    assert (
        skills.by_id("angrystrike").hit_range + world.rules.reach_slack
        == world.rules.reach
    )


def test_a_buff_is_not_an_attack():
    """warshout, frenzyshout, defiance and spikedShield damage nobody.

    Treating one as a blow gave the player a free hit every time they steadied
    themselves.
    """
    world = a_world()
    sender = a_player(world)
    near = stand(world, 0x85, 0.0, 1.0)

    for name in ("warshout", "frenzyshout", "defiance", "spikedShield"):
        assert skills.by_id(name).harmless, name
        world.resolve_attack(sender, skills.wire_of(name))
    assert world.creatures[near].health == 1000.0


def test_a_skill_whose_effect_is_a_status_effect_deals_no_direct_blow():
    """battlecry and earthquake carry a zero damage modifier on purpose.

    battlecry debuffs movement speed, resistance and attack speed for five seconds;
    earthquake lays a ground aura that does its damage over eight. Neither is
    modelled here, and inventing a blow for them would be inventing damage.
    """
    for name in ("battlecry", "earthquake", "true_earthquake"):
        used = skills.by_id(name)
        assert used.damage_modifier == 0.0, name
        assert used.harmless, name
        assert used.targeting != "User", f"{name} is aimed outward, but does no blow"


def test_an_area_skill_that_covers_nothing_hits_nothing():
    world = a_world()
    sender = a_player(world)
    stand(world, 0x85, 0.0, 40.0)
    world.resolve_attack(sender, skills.wire_of("mighty360"))
    assert all(c.health == 1000.0 for c in world.creatures.values())


def test_an_unknown_skill_falls_back_to_one_blow_at_the_old_reach():
    """A skill index this server cannot look up must still land a hit."""
    world = a_world()
    sender = a_player(world)
    near = stand(world, 0x85, 0.0, 1.0)
    assert skills.skill(999999) is None
    world.resolve_attack(sender, 999999)
    assert world.creatures[near].health == 1000.0 - 10.0, "no multiplier applied"


def test_an_area_skill_can_kill_several_creatures_at_once():
    world = a_world()
    world.rules.mob_damage = 2000.0
    sender = a_player(world)
    for tag, y in ((0x85, 1.0), (0x86, 2.0), (0x87, 3.0)):
        stand(world, tag, 0.0, y)

    world.resolve_attack(sender, skills.wire_of("mighty360"))
    assert all(c.health == 0.0 for c in world.creatures.values())


def test_every_warrior_skill_has_a_shape_this_server_understands():
    """No warrior skill may fall into a case nobody handles."""
    known = {"Actor", "Angle", "Cone", "Radius", "User"}
    warrior = skills.of_class("warrior")
    assert len(warrior) == 18
    for used in warrior:
        assert used.targeting in known, f"{used.id} targets by {used.targeting}"


def test_the_travelling_skills_are_flagged_rather_than_quietly_wrong():
    """enragingleap and stuncharge strike where they land, not where they start.

    This server measures from where the caster stands, so it is generous for those
    two. Flagged rather than papered over: the fix needs the destination, and no
    message this server receives carries it.
    """
    leap = skills.by_id("enragingleap")
    charge = skills.by_id("stuncharge")
    assert leap.lands_where_it_ends and charge.lands_where_it_ends
    assert leap.attack_range == 10.0 and leap.hit_range < 3.0
    assert not skills.by_id("angrystrike").lands_where_it_ends


def test_the_wire_index_is_the_row_before():
    """1838 is angrystrike, which is row 1839. Measured from what the client sends."""
    assert skills.wire_of("angrystrike") == 1838
    assert skills.wire_of("mightyswing") == 1839
    assert skills.wire_of("mighty360") == 1842
    assert skills.wire_of("AnderworldCreatureStrike") == 440
