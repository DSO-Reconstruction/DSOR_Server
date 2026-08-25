"""The actor id space, which used to be one byte wide."""

import pytest

from dsor.actors import (
    ACTOR_PAGE,
    LAST_LOW,
    RECORDED_PLAYER,
    RESERVED_LOW,
    ActorSpace,
    decode,
    encode,
)


def test_the_wire_form_matches_the_captured_ids():
    """The player is 15 00 01 00 and the dungeon's creatures 80 00 01 00 upward."""
    assert encode(RECORDED_PLAYER) == bytes([0x15, 0x00, 0x01, 0x00])
    assert decode(bytes([0x80, 0x00, 0x01, 0x00])) == ACTOR_PAGE | 0x80
    assert decode(encode(0x0001ABCD)) == 0x0001ABCD


def test_a_world_holds_far_more_than_two_hundred_and_fifty_six():
    """Which is what the old scheme allowed, creatures and items and players together.

        actor = bytes([(first_actor + index) & 0xFF, 0x00, 0x01, 0x00])
    """
    space = ActorSpace()
    assert space.room > 60_000
    handed = {space.take() for _ in range(5_000)}
    assert len(handed) == 5_000, "no repeats"
    assert all(a >> 16 == 1 for a in handed), "all in the page the real service uses"


def test_no_id_is_ever_handed_out_twice():
    """Two entities sharing an actor is not hypothetical: two dropped items once did,
    and it showed as a picked-up item arriving as the wrong thing."""
    space = ActorSpace()
    first = space.take()
    space.give_back(first)
    assert space.take() == first, "reused once it is free"
    assert space.take() != first

    with pytest.raises(ValueError, match="already taken"):
        space.reserve(first)


def test_the_recorded_ids_can_be_claimed_without_clashing():
    space = ActorSpace()
    assert space.reserve(RECORDED_PLAYER) == RECORDED_PLAYER
    for index in range(20):
        space.reserve(ACTOR_PAGE | (0x80 + index))
    # And nothing fresh lands on them, because fresh ones start past the reservations.
    assert RESERVED_LOW > 0x80 + 20
    assert all(space.take() & 0xFFFF >= RESERVED_LOW for _ in range(50))


def test_giving_back_something_never_taken_is_quiet():
    space = ActorSpace()
    space.give_back(ACTOR_PAGE | 0x1234)
    assert space.in_use == 0
    assert not space.free


def test_running_out_is_an_error_and_not_a_wrap():
    """A silent wrap is what the mask did, and a collision is worse than a refusal."""
    space = ActorSpace(next_low=LAST_LOW)
    space.take()
    with pytest.raises(RuntimeError, match="all in use"):
        space.take()


def test_two_thousand_players_and_their_drops_fit():
    space = ActorSpace()
    players = [space.take() for _ in range(2_000)]
    drops = [space.take() for _ in range(20_000)]
    assert len(set(players) | set(drops)) == 22_000
    assert space.room > 40_000
