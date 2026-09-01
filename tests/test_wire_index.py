"""Which number on the wire means which effect, and which means which skill.

This is the defect the whole effects effort was chasing. It is one constant, and it
produced exactly the symptom reported over and over: casting Dragon Hide showed "Power
of Smash" and "Spike Shield".

**The two tables do not share a convention.** Each becomes an array on the client, and
the array the wire indexes need not start at the table's first row.

``_Template_Skill`` is ``rowid - 1``, and the evidence is the *client's* own traffic:
across 1,581 real casts the skill command carries 1838 for angrystrike, 1846 for
frenzyshout, 1854 for spikedShield.

``_Template_StatusEffect`` is ``rowid + 15``, and the evidence is a capture of the live
service in which the operator cast Dragon Hide three times, then Spike Shield, then
Furious Battle Cry, then Ground Breaker, each isolated by thirty seconds. Eleven
index-to-effect correspondences, every one of them the same offset.

**Why the wrong value survived so long.** It was checked -- repeatedly -- against
captures that are for the most part this emulator's own traffic. Half of the 144
captures are. This server had written those indices itself with ``rowid - 1``, so
reading them back the same way returned the names it had put in, and every check
passed. The error was only ever visible against traffic this server did not write.
"""

from dsor import database, effects
from dsor.skills import wire_of as skill_wire


def test_the_two_tables_have_different_conventions():
    assert database.offset_of("_Template_Skill") == 1
    assert database.offset_of("_Template_StatusEffect") == -15


def test_the_skill_indices_are_the_ones_the_client_sends():
    """Measured on the client's own skill commands, not on anything served here."""
    for name, wire in (
        ("angrystrike", 1838),
        ("mightyswing", 1839),
        ("stuncharge", 1843),
        ("warshout", 1844),
        ("battlecry", 1845),
        ("frenzyshout", 1846),
        ("seismicslam", 1847),
        ("laceratingstrike", 1848),
        ("earthquake", 1852),
        ("spikedShield", 1854),
    ):
        assert skill_wire(name) == wire, name


def test_the_effect_indices_are_the_ones_the_live_service_sends():
    """The eleven correspondences from the isolated-cast capture."""
    for name, wire in (
        # Dragon Hide, three casts thirty seconds apart
        ("skill_frenzyshout_buff_armor", 5184),
        ("skill_frenzyshout_buff_resistance", 5185),
        ("skill_frenzyshout_buff_lifeleech", 5194),
        # Spike Shield
        ("warrior_spikedShield_buff", 5169),
        ("skill_spikedshield_buff_armor_trigger", 5542),
        # Furious Battle Cry
        ("skill_warshout_buff_movementspeed", 5165),
        ("skill_warshout_buff_angrystrike", 5166),
        ("skill_warshout_buff_mightybash", 5168),
        ("skill_warshout_buff_damage", 5518),
        ("talent_warrior_dd_warshout_buff_crit", 5519),
        ("ctfdropflag", 6212),
        # Ground Breaker
        ("skill_seismicslam_debuff_armor", 5158),
    ):
        assert effects.wire_of(name) == wire, name


def test_the_old_offset_is_what_the_operator_was_seeing():
    """Sixteen rows earlier, and the two names are the two that were reported.

    Not a curiosity: it is the whole of the evidence that this was the defect. With
    ``rowid - 1`` this server sent 5168 and 5169 for Dragon Hide, and those are the wire
    indices of warshout's mightybash buff and of the Spike Shield buff.
    """
    from dsor.effect_titles import effect_title

    assert effects.wire_of("skill_warshout_buff_mightybash") == 5168
    assert effect_title("skill_warshout_buff_mightybash") == "Power of Smash"
    assert effects.wire_of("warrior_spikedShield_buff") == 5169
    assert effect_title("warrior_spikedShield_buff") == "Spike Shield"
    # And what Dragon Hide should carry instead.
    assert effects.wire_of("skill_frenzyshout_buff_armor") == 5184
    assert effect_title("skill_frenzyshout_buff_armor") == "Dragon Hide"


def test_a_cast_now_puts_the_measured_indices_on_the_wire():
    from dsor import statuseffect as se
    from dsor.gameplay import Position
    from dsor.world import World

    world = World()
    world.rules.mobs = 3
    world._ready()
    where = ("1.2.3.4", 5)
    player = world.player(where)
    player.in_world = True
    player.position = Position(x=24453, elevation=-31744, y=25595)
    world.accept_clock(where, 2231)
    world.resolve_attack(where, skill_wire("frenzyshout"))
    world.resolve_attack(where, skill_wire("frenzyshout"), travelled=True)
    sent = [
        payload
        for _address, payload in world.tick()
        if payload[:3] == bytes([se.MULTI, 0x4F, 0x00])
    ]
    assert len(sent) == 1
    got = se.decode(sent[0])
    assert {element.index for element in got.elements} == {5184, 5185}
    for element in got.elements:
        # The same fields the live service carried for the same skill at the same tick.
        assert element.second == 25
        assert element.start == 2231
        assert element.end == 2481
        assert element.duration == 250
        assert element.sixth == 100
        assert tuple(int(bit) for bit in element.flags) == (0, 0, 0, 1)
