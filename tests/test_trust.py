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


def test_a_leap_buys_an_allowance_the_size_of_its_own_range():
    """enragingleap has an attack range of 10 world units — 1280 wire units in one
    step — so a plain speed check would refuse every leap in the game."""
    from dsor.gameplay import WORLD_SCALE
    from dsor.skills import by_id

    leap = by_id("enragingleap")
    assert leap.lands_where_it_ends and leap.attack_range == 10.0

    jump = leap.attack_range * WORLD_SCALE
    assert not movement_is_plausible(jump, 0.04), "not without the allowance"

    claims = Claims()
    claims.travel(leap.attack_range, now=100.0)
    assert movement_is_plausible(jump, 0.04, claims.spend_allowance(100.0))


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


def test_a_teleport_is_refused_and_the_last_believed_position_kept():
    """It was "mover.position = moved.position", straight from the datagram."""
    from dsor.gameplay import Position

    _service, world, sender = a_player_in_a_world()
    world.player(sender).seen_at = __import__("time").monotonic()

    assert not world.accept_movement(sender, Position(50_000, 0, 50_000))
    assert world.player(sender).position == Position(0, 0, 0), "kept"
    assert world.player(sender).claims.offences == 1


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
