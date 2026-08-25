"""Handing out actor ids that do not collide.

Every entity the client can draw has a four-byte actor id, and this server used to
build them by hand:

    actor = bytes([(first_actor + index) & 0xFF, 0x00, 0x01, 0x00])

One byte. Two hundred and fifty-six ids for every creature, every dropped item and
every player in a world, and the mask made a collision silent rather than an error.
That is not hypothetical: two dropped items once claimed the same actor, and the way it
showed was a picked-up item arriving as the wrong thing.

The real service's own ids say how much room there is. The captured player is
``15 00 01 00`` and the tutorial dungeon's creatures run from ``80 00 01 00``, so the
id is little-endian with a high word of 1 -- 0x00010015 and 0x00010080. That leaves the
low sixteen bits as the index, which is 65,535 actors in a world rather than 256.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The high word every real actor id carries. Measured: the player is 0x00010015 and
#: the dungeon's creatures 0x00010080 upward.
ACTOR_PAGE = 0x00010000

#: Where the captured player sits. The recorded player description names this id, so
#: the first player in a world still gets it and the recording still matches.
RECORDED_PLAYER = 0x00010015

#: Reserved so the ids this server hands out do not land on the recordings it replays:
#: the player at 0x15, and the creature and item ranges the old hand-rolled scheme
#: used, 0x40 upward for items and 0x80 upward for creatures.
RESERVED_LOW = 0x0100

#: The last index in a page.
LAST_LOW = 0xFFFF


def encode(actor: int) -> bytes:
    """An actor id as it appears on the wire: four bytes, little endian."""
    return actor.to_bytes(4, "little")


def decode(actor: bytes) -> int:
    return int.from_bytes(actor, "little")


@dataclass
class ActorSpace:
    """The actor ids of one world.

    Ids are handed out from a rising counter and returned to a free list, so an id is
    reused only after whatever held it is gone. Reuse matters over a long session: a
    world that drops items for a day would otherwise walk off the end of the page.
    """

    #: Where to hand out the next fresh id from.
    next_low: int = RESERVED_LOW
    #: Ids handed back, reused before fresh ones.
    free: list[int] = field(default_factory=list)
    #: Everything currently held, so a double release is an error rather than a
    #: silent second entry in the free list.
    taken: set[int] = field(default_factory=set)

    def reserve(self, actor: int) -> int:
        """Claim a specific id -- for the recorded player and the recorded creatures.

        Refuses one already held, because two entities sharing an actor is the exact
        failure this class exists to prevent.
        """
        if actor in self.taken:
            raise ValueError(f"actor {actor:#010x} is already taken")
        self.taken.add(actor)
        if actor in self.free:
            self.free.remove(actor)
        return actor

    def take(self) -> int:
        """A fresh actor id."""
        while self.free:
            candidate = self.free.pop(0)
            if candidate not in self.taken:
                self.taken.add(candidate)
                return candidate
        while True:
            if self.next_low > LAST_LOW:
                raise RuntimeError(
                    f"a world holds at most {LAST_LOW - RESERVED_LOW} actors and they "
                    "are all in use"
                )
            candidate = ACTOR_PAGE | self.next_low
            self.next_low += 1
            if candidate not in self.taken:
                self.taken.add(candidate)
                return candidate

    def give_back(self, actor: int) -> None:
        """Return an id. Quiet about one that was never taken."""
        if actor in self.taken:
            self.taken.discard(actor)
            self.free.append(actor)

    def take_bytes(self) -> bytes:
        return encode(self.take())

    @property
    def in_use(self) -> int:
        return len(self.taken)

    @property
    def room(self) -> int:
        """How many more this world can hand out."""
        return len(self.free) + (LAST_LOW - self.next_low + 1)
