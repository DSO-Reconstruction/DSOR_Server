"""The level curves past 30, and what happens to a body once it has fallen."""

from dsor.combat import (
    LEVEL_DAMAGE,
    LEVEL_EXPERIENCE,
    LEVEL_HIT_POINTS,
    damage_at,
    hit_points_at,
    level_bounds,
    level_for,
)


def test_the_level_curves_run_to_a_hundred_and_ten():
    """They stopped at 30, so a character above that fell off the end.

    level_for returned 30 whatever the experience, and damage_at and hit_points_at
    clamped there: a level 100 warrior hit for a level 30's 215 and carried a level
    30's 9900 health against the table's 16800 and 450000.
    """
    assert len(LEVEL_EXPERIENCE) == 110
    assert len(LEVEL_HIT_POINTS) == 110
    assert len(LEVEL_DAMAGE) == 110

    # Spot values straight out of _Template_XPLevels, warrior rows.
    assert (LEVEL_EXPERIENCE[0], LEVEL_HIT_POINTS[0], LEVEL_DAMAGE[0]) == (0, 225, 15)
    assert (LEVEL_EXPERIENCE[14], LEVEL_HIT_POINTS[14], LEVEL_DAMAGE[14]) == (
        55800, 2700, 43,
    )
    assert (LEVEL_EXPERIENCE[29], LEVEL_HIT_POINTS[29], LEVEL_DAMAGE[29]) == (
        1150000, 9900, 215,
    )
    assert (LEVEL_EXPERIENCE[99], LEVEL_HIT_POINTS[99], LEVEL_DAMAGE[99]) == (
        882229601, 450000, 16800,
    )


def test_the_thresholds_rise_to_104_and_then_stop_rising():
    """A limit of the data, not of the code, and worth stating plainly.

    The warrior's curve plateaus at 100: 882229601, then exactly one more point per
    level, which reads as a sentinel keeping the thresholds apart. It runs out at 105.
    Levels 105 to 110 all share 882229606, so experience cannot tell them apart and
    level_for answers 110 for any of them.
    """
    flat = LEVEL_EXPERIENCE[104:]
    assert len(set(flat)) == 1 and flat[0] == 882229606

    for level, (lower, upper) in enumerate(
        zip(LEVEL_EXPERIENCE, LEVEL_EXPERIENCE[1:]), start=1
    ):
        if level < 105:
            assert upper > lower, level
        else:
            assert upper == lower, level


def test_a_level_survives_a_round_trip_up_to_the_cap():
    from dsor.combat import MAX_LEVEL

    for level in (1, 2, 15, 30, 31, 99, 100, MAX_LEVEL):
        floor, _ = level_bounds(level)
        assert level_for(floor) == level, level


def test_no_level_has_an_experience_band_of_zero_width():
    """The client's character sheet crashes on one, and this server sent one.

    UI::CharacterSheetWidget::SetMaxXpLevel computes the bar's width as ceiling minus
    floor and asserts it is positive:

        mov  r14d, [rax + 0x28]   ; the floor
        mov  r15d, [rax + 0x2c]   ; the ceiling
        sub  ebp, r14d            ; the width
        test ebp, ebp
        jg   ok                   ; else "maxLevel > 0"

    The name is misleading — it is a width, not a level. A level 100 character killing
    anything at all went straight past 882229606 to level 110, where the floor and the
    ceiling are the same number, and the sheet asserted.
    """
    from dsor.combat import MAX_LEVEL

    for level in range(1, 200):
        floor, ceiling = level_bounds(level)
        assert ceiling > floor, f"level {level} has a bar of zero width"

    assert MAX_LEVEL == 104, "the last level with a band of its own"
    # Which is not a choice: 105 upward share a threshold.
    assert LEVEL_EXPERIENCE[104] == LEVEL_EXPERIENCE[109]
    assert LEVEL_EXPERIENCE[103] < LEVEL_EXPERIENCE[104]


def test_experience_past_the_cap_reports_the_cap():
    """Not level 110, whose band has no width."""
    from dsor.combat import MAX_LEVEL

    assert level_for(10**12) == MAX_LEVEL
    assert level_for(882229601 + 17) == MAX_LEVEL
    floor, ceiling = level_bounds(level_for(10**12))
    assert ceiling > floor


def test_the_top_of_the_curve_answers_instead_of_the_old_thirty():
    assert damage_at(100) == 16800
    assert hit_points_at(100) == 450000
    # And past the table's end it clamps to the last row rather than raising.
    assert hit_points_at(200) == LEVEL_HIT_POINTS[-1]
    assert damage_at(200) == LEVEL_DAMAGE[-1]
    assert damage_at(0) == LEVEL_DAMAGE[0]


def test_a_players_health_follows_their_level():
    import server

    service = server.Service(port=30000, name="t", role="map", map_name="a0001")
    world = service.world
    assert service.rules.player_max == 0, "zero means ask the level"
    assert world.player_health(1) == 225.0
    assert world.player_health(15) == 2700.0
    assert world.player_health(100) == 450000.0
    # A forced figure still wins, for the debug console.
    service.rules.player_max = 236
    assert world.player_health(100) == 236.0


def a_dead_creature():
    """A world with one creature, freshly killed."""
    import server

    service = server.Service(port=30000, name="t", role="map", map_name="a0001")
    world = service.world
    sender = ("127.0.0.1", 1)
    player = world.player(sender)
    player.in_world = True
    player.health = 100.0
    player.max_health = 100.0
    from dsor.gameplay import Position
    from dsor.world import Creature

    player.position = Position(0, 0, 0)
    actor = b"\x85\x00\x01\x00"
    world.creatures[actor] = Creature(
        actor=actor,
        record=b"",
        position=Position(0, 0, 0),
        health=0.0,
        max_health=24.0,
        blueprint="a0001_gen_anderworld_creature",
        described=True,
    )
    world.creatures[actor].corpse_ticks = world.rules.corpse_lifetime
    return world, sender, actor


def test_a_corpse_is_deleted_once_its_time_is_up():
    """The countdown was already here and did nothing when it reached zero.

    The corpse left the position update and the entity stayed, so a killed creature
    lay on screen for ever. Leaving the position update is not removal: the client
    keeps whatever it has been told about until something tells it otherwise.
    """
    from dsor.combat import DISCARD_MONSTER

    world, _sender, actor = a_dead_creature()
    creature = world.creatures[actor]

    for _ in range(world.rules.corpse_lifetime - 1):
        world.age_corpses()
    assert not creature.discarded, "still falling"
    assert creature.announced, "and still in the position update"

    world.age_corpses()
    assert creature.discarded
    assert not creature.announced
    sent = world._drain()
    assert sent, "the client was told to delete the entity"
    # 0x85, then the opcode little-endian.
    assert any(
        payload[1:3] == DISCARD_MONSTER.to_bytes(2, "little")
        for _address, payload in sent
    )


def test_a_corpse_is_deleted_exactly_once():
    """age_corpses runs ten times a second, and three counter resets once landed in
    it by mistake. Anything stateful in there needs a guard."""
    world, _sender, actor = a_dead_creature()
    for _ in range(world.rules.corpse_lifetime + 50):
        world.age_corpses()
    sent = world._drain()
    assert len(sent) == 1, f"sent {len(sent)} removals"
    assert world.creatures[actor].corpse_ticks == 0


def test_reset_puts_a_discarded_creature_back():
    """A world that carried its discards into the next session would empty out."""
    world, _sender, actor = a_dead_creature()
    for _ in range(world.rules.corpse_lifetime):
        world.age_corpses()
    assert world.creatures[actor].discarded
    world.reset_creatures()
    creature = world.creatures[actor]
    assert not creature.discarded
    assert creature.health == creature.max_health
    assert creature.alive


def test_the_bar_never_draws_past_its_own_end_at_the_cap():
    """The cap's band is one point wide, and a real total sits far above it.

    The warrior's curve rises by a single point per level from 100, so a character at
    the cap who kills anything has more experience than the band can hold. The bar's
    current value would exceed its maximum.
    """
    from dsor.combat import MAX_LEVEL, encode_xp_changed, level_bounds

    floor, ceiling = level_bounds(MAX_LEVEL)
    assert ceiling - floor == 1, "one point wide, which is what forces this"

    body = encode_xp_changed(total=floor + 5000, actor=0x150001, level=MAX_LEVEL)
    # Four 32-bit fields after the message id and the two-byte opcode.
    fields = [
        int.from_bytes(body[3 + 4 * i : 7 + 4 * i], "little") for i in range(4)
    ]
    reported_total, reported_level, reported_floor, reported_ceiling = fields
    assert reported_level == MAX_LEVEL
    assert (reported_floor, reported_ceiling) == (floor, ceiling)
    assert reported_total == ceiling, "clamped into the band"
    assert reported_total - reported_floor <= reported_ceiling - reported_floor


def test_below_the_cap_the_total_is_reported_untouched():
    from dsor.combat import encode_xp_changed, level_bounds

    floor, _ = level_bounds(15)
    body = encode_xp_changed(total=floor + 400, actor=0x150001, level=15)
    assert int.from_bytes(body[3:7], "little") == floor + 400
