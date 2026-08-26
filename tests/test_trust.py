"""The limits on what a client may claim."""

from dsor.trust import (
    LAG_SLACK,
    SPEED_CEILING,
    TRAVEL_GRACE,
    WALK_UNITS_PER_SECOND,
    Claims,
    movement_is_plausible,
    off_cooldown,
    reachable,
)


def test_a_walk_is_believed_and_crossing_the_map_is_not():
    # A second of walking, which must pass with room to spare.
    assert movement_is_plausible(WALK_UNITS_PER_SECOND, 1.0)
    # Three times a walk is the ceiling, and it passes.
    assert movement_is_plausible(WALK_UNITS_PER_SECOND * SPEED_CEILING, 1.0)
    # Ten times does not.
    assert not movement_is_plausible(WALK_UNITS_PER_SECOND * 10, 1.0)
    # The tutorial dungeon is a few thousand wire units across; one tick of it is not.
    assert not movement_is_plausible(20_000.0, 0.04)


def test_a_movement_buff_still_passes():
    """warshout's is 40%, and the client reports it: 0x59 against a walking 0x40.

    A tight ceiling would have refused every buffed step, which is the failure mode
    worth avoiding — the check exists to catch a claim that is not reachable, not to
    second-guess the client's own arithmetic.
    """
    buffed = WALK_UNITS_PER_SECOND * (0x59 / 0x40)
    assert movement_is_plausible(buffed, 1.0)


def test_a_burst_after_a_stall_is_not_read_as_a_teleport():
    """UDP, and no guarantee on the client's own movement records."""
    assert movement_is_plausible(LAG_SLACK, 0.0)
    assert reachable(0.0) == LAG_SLACK


def test_a_leap_buys_an_allowance_for_sustained_travel():
    """enragingleap crosses 10 world units, which is 1280 wire units in one step.

    A single step of that size is allowed outright now, because the slack is the
    largest step in 2506 real records — what this bounds is *sustained* speed, which is
    the only thing boundable without a navigation mesh. The allowance still matters for
    a leap followed immediately by running.
    """
    from dsor.gameplay import WORLD_SCALE
    from dsor.skills import by_id

    leap = by_id("enragingleap")
    assert leap.lands_where_it_ends and leap.attack_range == 10.0

    jump = leap.attack_range * WORLD_SCALE
    assert movement_is_plausible(jump, 0.04), "one step of any size is allowed"

    # A leap and then half a second of running is not, without the allowance.
    both = jump + WALK_UNITS_PER_SECOND * 0.5
    assert not movement_is_plausible(both, 0.04)
    claims = Claims()
    claims.travel(leap.attack_range, now=100.0)
    assert movement_is_plausible(both, 0.04, claims.spend_allowance(100.0))


def test_an_allowance_expires():
    claims = Claims()
    claims.travel(10.0, now=100.0)
    assert claims.spend_allowance(100.0) > 0
    assert claims.spend_allowance(100.0 + TRAVEL_GRACE + 0.1) == 0.0


def test_a_cooldown_is_enforced_with_a_tick_of_slack():
    """The client starts a skill three ticks ahead of its own clock — measured — so a
    cooldown enforced to the millisecond refuses the honest early edge of a cast."""
    assert off_cooldown(None, 30.0, now=0.0), "never used"
    assert off_cooldown(100.0, 0.0, now=100.0), "no cooldown at all"
    assert not off_cooldown(100.0, 30.0, now=110.0)
    assert off_cooldown(100.0, 30.0, now=130.0)
    # A hair early passes; a second early does not.
    assert off_cooldown(100.0, 30.0, now=129.97)
    assert not off_cooldown(100.0, 30.0, now=129.0)


def test_offences_are_counted_rather_than_acted_on():
    """One refusal is a lost datagram. The count is the evidence."""
    claims = Claims()
    assert claims.offences == 0
    claims.offences += 1
    assert claims.offences == 1


def a_player_in_a_world():
    import server
    from dsor.gameplay import Position

    service = server.Service(port=30000, name="t", role="map", map_name="a0001")
    world = service.world
    world.rules.mobs = 0
    world.rules.grant_up_to_level = 15
    sender = ("127.0.0.1", 1)
    player = world.player(sender)
    player.position = Position(0, 0, 0)
    player.in_world = True
    player.health = 2700.0
    player.max_health = 2700.0
    player.level = 15
    player.resource = 100.0
    return service, world, sender


def test_a_teleport_is_counted_and_then_taken_anyway():
    """Refusing was worse than not checking, and this is why.

    The position the server keeps is the one it echoes back in the next tick, so
    keeping a stale one drags the client to it. And once one record is refused the
    anchor is stale, so the next honest record measures as a huge jump and is refused
    too: 670 refusals in one session, the claimed distance shrinking each time as the
    client was pulled back. A rollback loop caused entirely by the correction.

    There is no honest correction available: it needs a model of where the player
    could be, and this server has no collision data and no navigation mesh.
    """
    from dsor.gameplay import Position

    _service, world, sender = a_player_in_a_world()
    world.player(sender).seen_at = __import__("time").monotonic()

    far = Position(50_000, 0, 50_000)
    assert world.accept_movement(sender, far), "taken"
    assert world.player(sender).position == far, "so nothing drags the client back"
    assert world.player(sender).claims.offences == 1, "and counted"


def test_refusing_can_be_switched_on_for_whoever_has_a_model():
    from dsor.gameplay import Position

    _service, world, sender = a_player_in_a_world()
    world.rules.refuse_movement = True
    world.player(sender).seen_at = __import__("time").monotonic()
    assert not world.accept_movement(sender, Position(50_000, 0, 50_000))
    assert world.player(sender).position == Position(0, 0, 0)


def test_the_speed_bound_is_the_players_own_and_not_a_creatures():
    """WALK_UNITS_PER_TICK is six, measured on creatures. Using it for the player made
    this three to eight times too tight and refused 670 legitimate moves.

    2506 real moving records: median step 57 wire units, p90 83, p99 142, largest 1146.
    """
    from dsor.trust import PLAYER_STEP_LARGEST, PLAYER_STEP_MEDIAN

    assert PLAYER_STEP_MEDIAN == 57.0
    assert WALK_UNITS_PER_SECOND == 57.0 * 25
    # Every one of the refusals that produced the rollback loop now passes.
    for travelled, ms in ((1200, 62), (532, 79), (258, 63), (146, 150), (142, 40)):
        assert movement_is_plausible(travelled, ms / 1000.0), (travelled, ms)
    # And the slack is the largest step seen, so any single record is allowed.
    assert movement_is_plausible(PLAYER_STEP_LARGEST, 0.0)


def test_an_ordinary_step_is_believed():
    import time

    from dsor.gameplay import Position

    _service, world, sender = a_player_in_a_world()
    world.player(sender).seen_at = time.monotonic() - 0.2
    assert world.accept_movement(sender, Position(30, 0, 30))
    assert world.player(sender).position == Position(30, 0, 30)
    assert world.player(sender).claims.offences == 0


def test_a_skill_the_character_has_not_unlocked_is_refused():
    """A client could send earthquake at level one: the wire index came from the
    command and was looked up with no check at all."""
    from dsor.skills import by_id, wire_of

    _service, world, sender = a_player_in_a_world()
    assert by_id("earthquake").unlock_level == 31
    assert world.may_use(sender, wire_of("earthquake")) is not None
    assert world.may_use(sender, wire_of("angrystrike")) is None
    assert world.may_use(sender, 999_999) is not None


def test_the_ownership_check_agrees_with_what_the_book_grants():
    """Out of step, it would refuse every skill in the game."""
    from dsor.skillbook import up_to_level

    _service, world, sender = a_player_in_a_world()
    assert world.player_skills(sender) == up_to_level(15)


def test_a_cooldown_is_enforced_on_the_attack_path():
    from dsor.skills import by_id, wire_of

    _service, world, sender = a_player_in_a_world()
    world.rules.grant_up_to_level = 33
    wire = wire_of("battlecry")
    assert by_id("battlecry").cool_down == 30.0

    assert world.may_use(sender, wire) is None
    world.note_use(sender, wire, by_id("battlecry"))
    assert "cooldown" in world.may_use(sender, wire)


def test_a_skill_that_cannot_be_paid_for_is_refused():
    """The note beside the resource said so outright: a cost that cannot be paid is
    not enforced."""
    from dsor.skills import by_id, wire_of

    _service, world, sender = a_player_in_a_world()
    assert by_id("mighty360").resource_cost == 0.4
    world.player(sender).resource = 100.0
    assert world.may_use(sender, wire_of("mighty360")) is None
    world.player(sender).resource = 10.0
    assert "costs" in world.may_use(sender, wire_of("mighty360"))


def test_using_a_leap_buys_the_movement_it_needs():
    from dsor.skills import by_id, wire_of

    _service, world, sender = a_player_in_a_world()
    world.note_use(sender, wire_of("enragingleap"), by_id("enragingleap"))
    import time

    assert world.player(sender).claims.spend_allowance(time.monotonic()) > 1000


def test_an_item_out_of_reach_is_refused():
    _service, world, sender = a_player_in_a_world()
    actor = world.actors.take_bytes()
    world.dropped[actor] = (400.0, 0.0, 400.0)
    assert world.pick_up(sender, actor) == []
    assert world.player(sender).claims.offences == 1


def test_turning_the_checks_off_is_a_deliberate_switch():
    from dsor.gameplay import Position
    from dsor.skills import wire_of

    _service, world, sender = a_player_in_a_world()
    world.rules.enforce = False
    world.player(sender).seen_at = __import__("time").monotonic()
    assert world.accept_movement(sender, Position(50_000, 0, 50_000))
    assert world.may_use(sender, wire_of("earthquake")) is None


def test_one_peer_flooding_costs_only_that_peer():
    import server

    service = server.Service(port=30000, name="t", role="map", map_name="a0001")
    service.peer_rate = 10
    noisy, quiet = ("10.0.0.1", 1), ("10.0.0.2", 1)
    accepted = sum(1 for _ in range(50) if service.within_rate(noisy))
    assert accepted == 10
    assert service.within_rate(quiet), "untouched"

    service.peer_rate = 0
    assert all(service.within_rate(noisy) for _ in range(100)), "0 turns it off"
