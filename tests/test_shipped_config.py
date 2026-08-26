"""What the shipped configuration actually produces, before anyone is asked to test.

Every one of these would have caught a setting I shipped and then had reported back to
me as a regression:

* ``mob_damage = 8`` made a level 104 character hit for 10 where the curve says 21000
  -- "je tape rien".
* ``player_max = 6000`` pinned health at 6k where the curve says 450000 -- "je suis
  bloque a 6k".
* dropping ``tough_mob`` left every creature dying to one blow -- "les boss qui avaient
  5M ont genre 100HP".

None of them needed a client to find. They needed somebody to run the numbers the
config produces and compare them against the client's own tables, which is what this
does.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

import server
from dsor import config
from dsor.combat import damage_at, hit_points_at, resource_at
from dsor.gameplay import Position
from dsor.mapdata import servable_points
from dsor.recorded import monster_library
from dsor.skillbook import up_to_level
from dsor.skills import of_class, wire_of
from dsor.world import Creature, Rules

SHIPPED = Path("dsor.toml")


@pytest.fixture(scope="module")
def settings():
    if not SHIPPED.exists():
        pytest.skip("no dsor.toml in this checkout")
    return config.load(SHIPPED)


@pytest.fixture
def rules(settings):
    """The rules the shipped file produces for the map it configures."""
    made = Rules()
    config.apply_to(made, config.rules_for(settings, "a0001_start_tutorial_dun"), "rules")
    return made


def test_the_file_loads_and_every_key_is_real(rules):
    """Already covered elsewhere, kept here because everything below depends on it."""
    assert isinstance(rules, Rules)


def test_the_player_hits_for_what_their_level_says(rules):
    """mob_damage of 8 was the whole of "je tape rien": blows of 10 and 4 at level 104,
    against a curve that says 21000.

    A forced figure is allowed -- the debug console sets one -- but not in the file that
    ships, because the file is what somebody is asked to play on.
    """
    assert rules.mob_damage == 0.0, (
        f"mob_damage is {rules.mob_damage}, which overrides the level curve's "
        f"{damage_at(rules.start_level or 1)} at level {rules.start_level}"
    )


def test_the_player_has_the_health_their_level_says(rules):
    """player_max of 6000 read as being stuck at 6k, at a level worth 450000."""
    assert rules.player_max == 0, (
        f"player_max is {rules.player_max}, where level {rules.start_level} is worth "
        f"{hit_points_at(rules.start_level or 1)}"
    )


def test_something_survives_a_blow(settings, rules):
    """Every creature in this dungeon holds 24 and a blow at the configured level is
    thousands, so without a punching bag nothing lives long enough to watch an effect."""
    tough = (settings.get("service") or {}).get("tough_mob", 0.0)
    blow = damage_at(rules.start_level or 1)
    assert tough > blow * 3, (
        f"tough_mob is {tough} against a blow of {blow}; nothing would survive to be "
        "watched"
    )


def test_the_skills_the_config_grants_are_the_skills_it_starts_at(rules):
    """Granting to a lower level than the character starts at silently withholds
    skills, and the ownership check would then refuse them."""
    assert rules.grant_up_to_level >= rules.start_level, (
        f"granted to {rules.grant_up_to_level} but starting at {rules.start_level}"
    )


def test_every_granted_skill_is_usable_at_that_level(rules):
    """The trust check refuses a skill the character does not own, and it is built from
    these same two rules -- so a mismatch here refuses skills in play."""
    service = server.Service(port=30000, name="t", role="map", map_name="a0001")
    service.rules = rules
    sender = ("127.0.0.1", 1)
    player = service.world.player(sender)
    player.level = rules.start_level or 1
    player.resource = resource_at(player.level)

    owned = service.world.player_skills(sender)
    assert owned == up_to_level(rules.grant_up_to_level)
    for skill in of_class("warrior"):
        if skill.unlock_level > (rules.start_level or 1):
            continue
        refusal = service.world.may_use(sender, skill.wire)
        assert refusal is None, f"{skill.id}: {refusal}"


def a_world_with_a_bag(rules):
    """A service with the shipped rules, a player at the configured level, and one
    creature tough enough to be hit repeatedly."""
    service = server.Service(port=30000, name="t", role="map", map_name="a0001")
    service.rules = rules
    world = service.world
    world.populate_from_map(servable_points(set(monster_library())), 0.0)
    for creature in world.creatures.values():
        creature.described = True
    actor = world.toughen(5_000_000.0)[0]
    world.creatures[actor].position = Position(0, 0, 0)

    sender = ("127.0.0.1", 1)
    player = world.player(sender)
    player.position = Position(0, 0, 0)
    player.in_world = True
    player.level = rules.start_level or 1
    player.max_health = world.player_health(player.level)
    player.health = player.max_health
    player.resource = world.resource_pool(sender)
    return service, world, sender, actor


def test_every_skill_lands_a_blow_worth_the_level(rules):
    """The end-to-end check that was missing: use each skill the character has, on a
    creature in range, and see damage arrive."""
    service, world, sender, actor = a_world_with_a_bag(rules)
    expected = damage_at(rules.start_level or 1)

    for skill in of_class("warrior"):
        if skill.unlock_level > (rules.start_level or 1) or skill.harmless:
            continue
        before = world.creatures[actor].health
        world.player(sender).resource = world.resource_pool(sender)
        world.player(sender).claims.used_at.clear()
        world.attack(sender, skill.wire)
        world.pending_swings = [(0.0,) + e[1:] for e in world.pending_swings]
        world.land_swings()
        dealt = before - world.creatures[actor].health
        assert dealt > 0, f"{skill.id} dealt nothing"
        assert dealt == pytest.approx(expected * skill.damage_modifier), skill.id


def test_a_realistic_walk_is_never_counted_as_a_cheat(rules):
    """670 legitimate moves were refused in one session. A median step, repeated, must
    produce no offence at all."""
    import time

    from dsor.trust import PLAYER_STEP_MEDIAN

    service, world, sender, _actor = a_world_with_a_bag(rules)
    player = world.player(sender)
    now = time.monotonic()
    x = 0
    for step in range(200):
        x += int(PLAYER_STEP_MEDIAN)
        player.seen_at = now - 0.04
        world.accept_movement(sender, Position(x, 0, 0))
    assert player.claims.offences == 0, f"{player.claims.offences} of 200 refused"
    assert player.position == Position(x, 0, 0)


def test_a_killed_creature_is_taken_off_the_screen(rules):
    """The corpse has to leave, and it leaves on a countdown that has to run."""
    service, world, sender, actor = a_world_with_a_bag(rules)
    creature = world.creatures[actor]
    creature.health = 0.0
    world.creature_died(sender, actor)
    world._drain()

    for _ in range(rules.corpse_lifetime + 1):
        world.tick()
    assert creature.discarded, "still on the client's screen"
    assert not creature.announced


def test_a_creature_can_reach_the_player_and_strike(rules):
    """The chase and the blow, which the rollback loop broke by freezing the player's
    position on the server."""
    service, world, sender, actor = a_world_with_a_bag(rules)
    world.creatures[actor].position = Position(0, 0, 0)
    world.creatures[actor].last_struck = 0.0
    before = world.player(sender).health
    world.creatures_strike()
    world.pending_hits = [(0.0,) + e[1:] for e in world.pending_hits]
    world.land_hits()
    assert world.player(sender).health < before, "nothing reached the player"
