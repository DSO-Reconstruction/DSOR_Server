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


def test_the_facing_comes_from_the_body_byte_not_the_movement_byte():
    """The arc bug, pinned. Byte 7 is zero whenever the player stands still.

    Measured over twelve live sessions: 128,580 stationary movement records carry
    zero in byte 7 99.9% of the time, while byte 8 holds a real facing across 147
    distinct values. A player attacks standing still, so reading byte 7 aimed every
    swing along +y and mightyswing's 170 degree arc pointed north whatever the screen
    showed.
    """
    from dsor.gameplay import (
        ClientMovement,
        Position,
        decode_client_movement,
        encode_client_movement,
    )

    at_rest = ClientMovement(
        position=Position(100, 0, 200),
        moving=False,
        direction=(0, 185),  # the shape 128,389 real records take
        tick=7,
        counter=1,
        unknown=0,
        trailer=bytes([0, 20, 0]),
    )
    body = encode_client_movement(at_rest)
    assert decode_client_movement(body).direction == (0, 185)
    assert body[7] == 0, "travelling nowhere"
    assert body[8] == 185, "but facing somewhere"


def test_a_stationary_player_can_still_aim_a_cone():
    """End to end: standing still, facing south, mightyswing must hit what is south.

    With the movement byte read instead, this creature was behind a north-facing
    cone and took nothing.
    """
    world = a_world()
    sender = a_player(world, heading=128)  # facing -y
    south = stand(world, 0x85, 0.0, -2.0)
    north = stand(world, 0x86, 0.0, 2.0)

    world.resolve_attack(sender, skills.wire_of("mightyswing"))
    assert world.creatures[south].health < 1000.0, "in front of a player facing south"
    assert world.creatures[north].health == 1000.0, "behind them"


def test_the_aim_comes_out_of_the_command_itself():
    """Four real commands, decoded. The float at offset 4 is the aim, in radians.

    Measured across 277 real skill commands: every one falls inside [-pi, +pi],
    spanning -3.1315 to +3.0803 across 198 distinct values. Fitting (sign, offset)
    against the facing byte of the movement record immediately before each command
    gives sign +1 and offset 128 — half a turn — with a median error of one unit out
    of 256.
    """
    from dsor.combat import decode_skill_use

    captured = {
        "mightybash": "0000 3b07 93948 3bf 26060000 869057b100".replace(" ", ""),
        "angrystrike": "00002e0753 79aabf 05020000 e11cee8e428000 8000",
        "stuncharge": "0000 3307 ee88babf 52040000 c1",
        "enragingleap": "0000 3907 26d6f13f c3040000 7915",
    }
    expected = {
        "mightybash": (1851, 86, 1574),
        "angrystrike": (1838, 74, 517),
        "stuncharge": (1843, 69, 1106),
        "enragingleap": (1849, 205, 1219),
    }
    for name, hexed in captured.items():
        use = decode_skill_use(bytes.fromhex(hexed.replace(" ", "")))
        assert use is not None, name
        assert (use.wire, use.heading, use.tick) == expected[name], name
        assert skills.skill(use.wire).id == name


def test_a_body_too_short_or_not_a_heading_is_refused():
    """Better to fall back to the movement facing than aim with another field."""
    import struct

    from dsor.combat import decode_skill_use

    assert decode_skill_use(b"\x00" * 11) is None
    # A float well outside [-pi, +pi] is not a heading.
    body = b"\x00\x00" + (1838).to_bytes(2, "little") + struct.pack("<f", 900.0)
    assert decode_skill_use(body + b"\x00" * 4) is None


def test_the_command_aim_beats_a_stale_movement_facing():
    """The player turns, then swings, before the next movement record goes out.

    This is the 23% of real commands where the two disagree, and the command is the
    one that is right.
    """
    world = a_world()
    sender = a_player(world, heading=0)  # the last record said "facing +y"
    south = stand(world, 0x85, 0.0, -2.0)
    north = stand(world, 0x86, 0.0, 2.0)

    # ... but the command says the player is aiming -y.
    world.resolve_attack(sender, skills.wire_of("mightyswing"), aim=128)
    assert world.creatures[south].health < 1000.0, "aimed where the command said"
    assert world.creatures[north].health == 1000.0, "not where the record said"


def test_a_leap_is_resolved_where_it_lands_not_where_it_started():
    """enragingleap crosses ten units and hits within under three of the landing.

    Resolving it on arrival of the command measured from where the player left, which
    for a jump of ten against a reach of three usually meant nobody at all.
    """
    world = a_world()
    sender = a_player(world)
    far = stand(world, 0x85, 0.0, 9.0)

    leap = skills.wire_of("enragingleap")
    world.resolve_attack(sender, leap)
    assert world.creatures[far].health == 1000.0, "nothing yet — it is in the air"
    assert len(world.pending_swings) == 1

    # The client reports where the player came down, as it does continuously.
    world.player(sender).position = Position(0.0, 0.0, 9.0 * 128.0)
    world.pending_swings = [(0.0,) + entry[1:] for entry in world.pending_swings]
    world.land_swings()
    assert world.creatures[far].health < 1000.0, "hit where it landed"
    assert not world.pending_swings


def test_a_leap_that_lands_on_nobody_still_clears():
    world = a_world()
    sender = a_player(world)
    world.resolve_attack(sender, skills.wire_of("enragingleap"))
    world.pending_swings = [(0.0,) + entry[1:] for entry in world.pending_swings]
    world.land_swings()
    assert not world.pending_swings


def test_a_player_who_leaves_takes_their_pending_swing_with_them():
    world = a_world()
    sender = a_player(world)
    world.resolve_attack(sender, skills.wire_of("enragingleap"))
    assert world.pending_swings
    world.forget(sender)
    assert not world.pending_swings


def test_a_charge_travels_too():
    """stuncharge has an attack range of 10 and a 45 degree arc."""
    charge = skills.by_id("stuncharge")
    assert charge.lands_where_it_ends
    assert (charge.attack_range, charge.arc) == (10.0, 45.0)
    assert charge.kind == "Charge"


def test_every_skill_command_the_client_sends_is_dispatched():
    """0x0049 was missing, and stuncharge arrives as it.

    Measured in the captures: 0x0046, 0x0047, 0x0049 and 0x004A from the client, and
    eight of the 0x0049 on the live service too. Leaving it out was the whole of
    "certains font pas de degats".
    """
    import server

    for opcode in (0x0046, 0x0047, 0x0048, 0x0049, 0x004A, 0x004B):
        assert opcode in server.SKILL_OPCODES, hex(opcode)
    assert server.SKILL_OPCODES[0x0049] == "TargetPointBulletSkillCommand"
    # And the two left out on purpose are named, so neither is left out by accident.
    assert set(server.SKILL_OPCODES_UNHANDLED) == {0x004C, 0x004D}
    assert not set(server.SKILL_OPCODES) & set(server.SKILL_OPCODES_UNHANDLED)
