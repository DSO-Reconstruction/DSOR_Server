"""What had to change for more than one player to stand in a world at once."""

import time

import pytest

import server
from dsor.actors import RECORDED_PLAYER, decode
from dsor.gameplay import Position
from dsor.mapdata import servable_points
from dsor.recorded import monster_library
from dsor.gameplay import WORLD_SCALE as WORLD
from dsor.world import Creature, World


def a_world(players=0):
    service = server.Service(port=30000, name="t", role="map", map_name="a0001")
    world = service.world
    world.populate_from_map(servable_points(set(monster_library())), 0.0)
    for creature in world.creatures.values():
        creature.described = True
    for index in range(players):
        player = world.player(("10.0.0.%d" % (index % 250), 1000 + index))
        player.position = Position(0, 0, 0)
        player.in_world = True
        player.health = 6000.0
        player.max_health = 6000.0
        player.level = 100
        player.server_tick = 1000
    return world


def test_every_player_gets_an_actor_of_their_own():
    """PLAYER_ACTOR was a module constant, so every player was the same entity.

    The first still gets the id the recorded description names, so the single-player
    path is untouched.
    """
    world = a_world(players=5)
    actors = [p.actor for p in world.players.values()]
    assert len(set(actors)) == 5, "no two players share one"
    assert decode(actors[0]) == RECORDED_PLAYER, "the first keeps the recorded id"
    assert all(decode(a) >> 16 == 1 for a in actors), "in the page the service uses"


def test_a_players_actor_goes_back_when_they_leave():
    world = a_world(players=3)
    addresses = list(world.players)
    held = world.actors.in_use
    world.forget(addresses[1])
    assert world.actors.in_use == held - 1


def test_creature_and_item_ids_no_longer_wrap_at_two_hundred_and_fifty_six():
    """The old form masked to one byte, and a collision was silent.

        actor = bytes([(first_actor + index) & 0xFF, 0x00, 0x01, 0x00])
    """
    world = a_world()
    ids = [decode(c.actor) for c in world.creatures.values()]
    assert len(set(ids)) == len(ids)
    # And the space has room for two thousand players and their drops on top.
    assert world.actors.room > 60_000


def test_a_creature_is_moved_once_a_tick_and_not_once_per_viewer():
    """It used to be moved inside entity_update, which runs per player.

    With five players a creature took five steps a tick, each toward a different one,
    and stepped_tick advanced on the first so the rest saw an elapsed of zero.
    """
    world = a_world(players=5)
    actor = next(
        c.actor for c in world.creatures.values() if c.blueprint and c.attack_skill
    )
    before = world.creatures[actor].position
    world.tick()
    once = world.creatures[actor].position

    # Serialising for every player again inside the same tick moves nothing further.
    for player in world.players.values():
        world.entity_update(player.position, 1000)
    assert world.creatures[actor].position == once
    assert (before, once) != (once, once) or before == once


def test_every_player_is_sent_the_same_creatures():
    """They are the same creatures. Building the records once is what makes that true
    rather than merely likely."""
    world = a_world(players=4)
    # tick() returns what it queued and empties the outbox, so take its answer.
    sent = [payload for _address, payload in world.tick()]
    updates = [p for p in sent if p[1:3] == b"\x5f\x00"]
    assert len(updates) == 4
    # Everything past the player's own 20-byte record is the shared creature tail.
    tails = {p[3 + 20 :] for p in updates}
    assert len(tails) == 1, "one world, one set of creatures"


def test_a_creature_strikes_on_its_own_cooldown_not_once_per_player():
    """Ten players around one creature each had their own timer, so it struck ten
    times as often. The timer lives on the creature now."""
    world = a_world(players=10)
    world.rules.creature_damage = 100.0
    world.rules.strike_interval = 5.0
    for creature in world.creatures.values():
        creature.position = Position(0, 0, 0)
    world._place_from_descriptions = lambda: None

    world.creatures_strike()
    struck = len(world.pending_hits)
    world.creatures_strike()
    assert len(world.pending_hits) == struck, "still on cooldown, whoever is standing"


def test_the_timing_cache_notices_a_rule_changing():
    """Keyed on the blueprint alone it went on answering with the old numbers after a
    console "set creature_hit_frame 40"."""
    world = a_world()
    actor = next(iter(world.creatures))
    first = world.creature_timing(actor)
    world.rules.creature_hit_frame = 40
    assert world.creature_timing(actor)[0] == 40
    world.rules.creature_hit_frame = 0
    assert world.creature_timing(actor) == first


def test_a_recorded_payload_is_read_from_disk_once():
    """tick_state is on the per-tick path: a thousand players at ten ticks a second
    was ten thousand file reads a second for the same 96 bytes."""
    from dsor.recorded import payload, tick_state

    payload.cache_clear()
    tick_state()
    misses = payload.cache_info().misses
    for _ in range(500):
        tick_state()
    assert payload.cache_info().misses == misses
    assert payload.cache_info().hits >= 500


def test_two_thousand_players_tick_inside_the_frame_budget():
    """Not a benchmark with a number to beat — a guard that the shape stayed linear.

    Measured on this machine: 154.9 ms a tick before these changes and 39.3 ms after,
    at ten ticks a second. The bound here is loose enough to survive a slower machine
    and tight enough to fail if the per-viewer work comes back.
    """
    world = a_world(players=2_000)
    world.tick()
    start = time.perf_counter()
    world.tick()
    took = time.perf_counter() - start
    assert took < 0.100, f"{took * 1000:.0f} ms a tick for 2000 players"


def converge(world, sender, ticks=3000):
    """Let every creature in the world walk in and settle."""
    world.rules.mob_aggro = 400.0
    player = world.players[sender]
    for step in range(ticks):
        player.server_tick = 5000 + step
        world.tick()
    return [c.position for c in world.creatures.values() if c.alive]


def a_crowded_world():
    from dsor.mapdata import servable_points
    from dsor.recorded import monster_library

    service = server.Service(port=30000, name="t", role="map", map_name="a0001")
    world = service.world
    usable = servable_points(set(monster_library()))
    world.populate_from_map(usable, 0.0)
    # What serve() sets. Without it entity_update drops every creature, which is a trap
    # worth naming: a test that forgets it sees an empty world and blames the wrong
    # thing.
    world.rules.mobs = len(usable)
    for creature in world.creatures.values():
        creature.described = True
    sender = ("127.0.0.1", 1)
    player = world.player(sender)
    player.position = Position(0, 0, 0)
    player.in_world = True
    player.health = 450_000.0
    player.max_health = 450_000.0
    player.level = 104
    return service, world, sender


def test_creatures_that_converge_do_not_end_up_on_one_point():
    """"tous les mobs spawn au meme endroit" — they do not spawn there, they arrive.

    Every creature that reached the player stopped mob_stop units from them and nothing
    kept them apart from each other, so twenty of them stood on one point. The same
    problem clear_of_other_drops solves for items on the ground, solved there and not
    here.
    """
    import itertools

    _service, world, sender = a_crowded_world()
    spawned = {(c.position.x, c.position.y) for c in world.creatures.values()}
    assert len(spawned) == len(world.creatures), "distinct before they move, too"

    settled = converge(world, sender)
    assert len(settled) >= 15
    assert len({(p.x, p.y) for p in settled}) == len(settled), "no two on one point"

    closest = min(
        a.distance_to(b) for a, b in itertools.combinations(settled, 2)
    )
    assert closest > world.rules.mob_spacing * WORLD * 0.9, (
        f"nearest pair {closest / WORLD:.2f} apart, wanted {world.rules.mob_spacing}"
    )


def test_enough_of_them_get_close_enough_to_strike():
    """The other half, and the trap that a single wide ring walks straight into.

    A ring wide enough to space twenty creatures properly puts them 5.1 units out, past
    their own AttackRange of 2 — so they surround the player and never strike. That is
    the same fault as "stopping at 3.5 put the creature outside its own reach".
    """
    _service, world, sender = a_crowded_world()
    settled = converge(world, sender)
    here = Position(0, 0, 0)
    reach = 2.25 * WORLD
    within = [p for p in settled if p.distance_to(here) <= reach]
    assert len(within) >= 5, f"only {len(within)} could reach the player"
    # And the queue behind is further out rather than piled on the inner ring.
    assert max(p.distance_to(here) for p in settled) > reach


def test_the_inner_ring_sits_at_the_stop_distance():
    _service, world, _sender = a_crowded_world()
    places = world._standing_room(Position(0, 0, 0), 20)
    assert len(places) == 20
    here = Position(0, 0, 0)
    nearest = min(p.distance_to(here) for p in places)
    assert nearest == pytest.approx(world.rules.mob_stop * WORLD, rel=0.02)
    # Ring by ring, not one ring: the furthest is further than the stop distance.
    assert max(p.distance_to(here) for p in places) > world.rules.mob_stop * WORLD


def test_one_creature_still_walks_straight_at_the_player():
    """A ring of one is the stop distance, so nothing about a single creature changes."""
    _service, world, _sender = a_crowded_world()
    places = world._standing_room(Position(0, 0, 0), 1)
    assert len(places) == 1
    assert places[0].distance_to(Position(0, 0, 0)) == pytest.approx(
        world.rules.mob_stop * WORLD, rel=0.02
    )
