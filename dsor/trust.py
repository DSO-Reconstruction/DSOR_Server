"""What the server refuses to take the client's word for.

Every one of these was taken on trust, and each is a thing a modified client could
simply assert:

* **where the player is.** ``mover.position = moved.position``, straight from the
  datagram. A client could stand anywhere, at any speed, including inside a boss it
  had not walked to.
* **which skill they used.** The wire index came from the command and was looked up
  with no check that the character has that skill. A level 1 character could send
  ``earthquake``.
* **how often.** No cooldown was enforced, so a skill with a thirty-second cooldown
  could be sent every tick.
* **what it costs.** The note beside the resource said so outright: "A cost that
  cannot be paid is not enforced."
* **what they pick up.** Any item lying anywhere in the world, from anywhere.

The tolerances here are deliberately loose. A server that snaps a player back on the
first disagreement fights the network rather than the cheat, and this one talks over
UDP with no delivery guarantee on the client's own movement records. Each check is
built to pass ordinary play with lag and fail a claim that is not physically
reachable, and each records an offence rather than disconnecting, because the count is
the evidence and one datagram is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dsor.gameplay import WALK_UNITS_PER_TICK, WORLD_SCALE

#: Milliseconds in one game tick, as the movement codec uses.
TICK_MS = 40.0

#: Wire units a *player* covers in a second, measured from their own records.
#:
#: Not WALK_UNITS_PER_TICK. That six is a creature's step, measured on creatures, and
#: using it for the player made this check three to eight times too tight: 2506 real
#: moving records give a median step of 57 wire units, p90 83, p99 142 and a largest of
#: 1146. At twenty-five records a second the median is about 1425 units a second, where
#: the creature figure said 150.
#:
#: Getting this wrong refused 670 legitimate moves in one session.
PLAYER_STEP_MEDIAN = 57.0
PLAYER_STEP_LARGEST = 1146.0
RECORDS_PER_SECOND = 1000.0 / TICK_MS
WALK_UNITS_PER_SECOND = PLAYER_STEP_MEDIAN * RECORDS_PER_SECOND

#: How much faster than a walk a character is allowed to travel and still be believed.
#:
#: Not tight, and on purpose. A movement buff is real -- warshout's is 40%, and the
#: client reports it in the speed byte, 0x59 against a walking 0x40 -- and mounts and
#: future buffs stack on that. Three times a walk passes all of it and still refuses a
#: claim of crossing the map.
SPEED_CEILING = 3.0

#: Wire units always allowed regardless of elapsed time, so that a burst of records
#: arriving together after a stall is not read as a teleport.
#:
#: The largest single step in 2506 real records. Any one record is therefore always
#: allowed and what this bounds is *sustained* speed -- crossing the map -- rather than
#: one odd step, which is the thing worth bounding and the thing that can be bounded
#: without a navmesh.
LAG_SLACK = PLAYER_STEP_LARGEST

#: How long a leap or a charge is allowed to have moved the player, in seconds.
#:
#: Those genuinely do teleport: enragingleap has an attack range of 10 world units,
#: which is 1280 wire units in one step, and a plain speed check would refuse every
#: one of them. So using one buys an allowance the size of its own range, and the
#: allowance expires.
TRAVEL_GRACE = 1.5


@dataclass
class Claims:
    """What one player has claimed lately, and what they are owed.

    Kept per player rather than globally, because every limit here is per character.
    """

    #: Extra wire units the player may cover, from a leap or a charge.
    allowance: float = 0.0
    #: When that allowance was granted, on the monotonic clock.
    granted_at: float = 0.0
    #: When each skill was last used, by wire index.
    used_at: dict[int, float] = field(default_factory=dict)
    #: Claims refused. Evidence, not a trigger: one refusal is a lost datagram.
    offences: int = 0

    def travel(self, world_units: float, now: float) -> None:
        """Grant an allowance for a skill that moves the player *world_units*."""
        self.allowance = max(self.allowance, world_units * WORLD_SCALE)
        self.granted_at = now

    def spend_allowance(self, now: float) -> float:
        """The allowance still standing, expiring it once it is stale."""
        if now - self.granted_at > TRAVEL_GRACE:
            self.allowance = 0.0
        return self.allowance


def reachable(elapsed: float, speed_ceiling: float = SPEED_CEILING) -> float:
    """How many wire units a character can honestly cover in *elapsed* seconds."""
    if elapsed < 0.0:
        elapsed = 0.0
    return WALK_UNITS_PER_SECOND * speed_ceiling * elapsed + LAG_SLACK


def movement_is_plausible(
    travelled: float,
    elapsed: float,
    allowance: float = 0.0,
    speed_ceiling: float = SPEED_CEILING,
) -> bool:
    """Whether a step of *travelled* wire units in *elapsed* seconds can be believed.

    *allowance* is what a leap or a charge bought, which is why this takes it as a
    number rather than looking it up: the caller knows whether one is owed.
    """
    return travelled <= reachable(elapsed, speed_ceiling) + allowance


def off_cooldown(used_at: float | None, cool_down: float, now: float) -> bool:
    """Whether a skill last used at *used_at* may be used again.

    A tolerance of one tick, because the client starts a skill three ticks ahead of
    its own clock -- measured -- and a cooldown enforced to the millisecond would
    refuse the honest early edge of every cast.
    """
    if used_at is None or cool_down <= 0.0:
        return True
    return now - used_at >= cool_down - TICK_MS / 1000.0
