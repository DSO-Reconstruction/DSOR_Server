"""The state of one map instance: its creatures, and the players in it.

Why this module exists. The server class it was extracted from carried 75
attributes, seventeen of them collections — nine keyed by a player's address and
four by a creature's actor id. Every feature added another one, and the state of a
single player was spread across nine parallel dictionaries that all had to be
kept in step by hand. That is a scaling defect and a correctness one: the level
counter that climbed across reconnections was a per-player value that had been
keyed by nothing at all, and a disconnecting player had to be deleted from nine
places to be really gone.

One object per thing, instead. A player's state lives in :class:`Player`, a
creature's in :class:`Creature`, and the map instance that owns them in
:class:`World`.

The design rule that matters for what comes next: **a world never touches a
socket.** It takes events, advances a tick, and hands back messages for someone
else to send. That keeps the game rules testable without a network, and it is what
makes a thread per map instance possible — the network thread does the syscalls,
each world owns its own state exclusively, and nothing needs a lock because
nothing is shared.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dsor.gameplay import Position, actor_id, decode_position

#: A player is identified by the address its datagrams come from.
Address = tuple[str, int]


@dataclass
class Creature:
    """One creature in a map instance."""

    #: Its actor id, four bytes, as it appears in every command's trailer.
    actor: bytes
    #: The recorded movement record it was built from. Carries its blueprint.
    record: bytes
    #: Where it stands now, in wire units. Starts where it was recorded.
    position: Position
    health: float
    max_health: float
    #: Whether the client has been sent its description. Until it has, the client
    #: has no entity for the actor and nothing addressed to it can be drawn.
    described: bool = False
    #: Ticks the body is still announced for after death. Dropping a creature from
    #: the update the instant it dies leaves the client nothing to play the death
    #: sequence over.
    corpse_ticks: int = 0

    @classmethod
    def from_record(cls, record: bytes, max_health: float) -> "Creature":
        return cls(
            actor=actor_id(record),
            record=record,
            position=decode_position(record),
            health=max_health,
            max_health=max_health,
        )

    @property
    def alive(self) -> bool:
        return self.health > 0.0

    @property
    def announced(self) -> bool:
        """Whether it still belongs in the position update."""
        return self.alive or self.corpse_ticks > 0

    def home(self) -> Position:
        """Where it was first recorded, whatever it has done since."""
        return decode_position(self.record)


@dataclass
class Player:
    """One player in a map instance."""

    address: Address
    position: Position | None = None
    health: float = 0.0
    max_health: float = 0.0
    experience: int = 0
    level: int = 1
    #: The client's own game tick, read from its movement records. A skill's start
    #: tick is compared against it.
    tick: int = 0
    #: When a creature last struck this player, on the server's monotonic clock.
    last_struck: float = 0.0
    #: The creature this player is hitting. Latched: choosing afresh on every blow
    #: makes the choice flip between creatures standing close together, which reads
    #: on screen as damage being shared out.
    target: bytes | None = None
    in_world: bool = False

    @property
    def alive(self) -> bool:
        return self.health > 0.0


@dataclass
class World:
    """One map instance: the creatures in it, and the players who see them.

    Holds no sockets, no connections and no protocol. Everything here is state and
    the rules that move it.
    """

    creatures: dict[bytes, Creature] = field(default_factory=dict)
    players: dict[Address, Player] = field(default_factory=dict)
    #: Blows in flight, as (when it lands, victim, attacker). A creature's blow is
    #: announced when the swing starts and applied at the skill's impact frame.
    pending_hits: list[tuple[float, Address, bytes]] = field(default_factory=list)

    # ── creatures ────────────────────────────────────────────────────────────

    def populate(self, records: list[bytes], max_health: float) -> None:
        """Fill the world with the creatures *records* describes."""
        for record in records:
            creature = Creature.from_record(record, max_health)
            self.creatures[creature.actor] = creature

    def creature(self, actor: bytes) -> Creature | None:
        return self.creatures.get(actor)

    def announced(self) -> list[Creature]:
        """The creatures belonging in a position update, living and lately dead."""
        return [c for c in self.creatures.values() if c.announced]

    def engageable(self) -> list[Creature]:
        """The creatures that can be fought: alive, and known to the client."""
        return [c for c in self.creatures.values() if c.alive and c.described]

    def age_corpses(self) -> None:
        for creature in self.creatures.values():
            if creature.corpse_ticks:
                creature.corpse_ticks -= 1

    # ── players ──────────────────────────────────────────────────────────────

    def player(self, address: Address) -> Player:
        """The player at *address*, created on first sight."""
        player = self.players.get(address)
        if player is None:
            player = Player(address=address)
            self.players[address] = player
        return player

    def forget(self, address: Address) -> None:
        """Remove a player entirely. One place, not nine."""
        self.players.pop(address, None)
        self.pending_hits = [h for h in self.pending_hits if h[1] != address]

    def inhabitants(self) -> list[Player]:
        return [p for p in self.players.values() if p.in_world]
