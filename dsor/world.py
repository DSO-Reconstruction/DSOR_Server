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

from dsor.gameplay import WALK_UNITS_PER_TICK, Position, actor_id, decode_position

#: A player is identified by the address its datagrams come from.
Address = tuple[str, int]


@dataclass
class Rules:
    """What the world does, as opposed to what it currently is.

    Separated from :class:`World` because the two change for different reasons: a
    rule is set once from the command line, state changes every tick. They were
    mixed on one service object with 75 attributes, where a reader could not tell
    a measurement from a knob.

    Where a value is measured, the comment says where from. Where it is ours, it
    says that too.
    """

    #: How many creatures to place around the player, from the recorded set.
    mobs: int = 0
    #: Distance to place them at, or 0 to leave them where they were recorded.
    mob_radius: int = 0
    #: Radius of a slow patrol around their own position, or 0 to stand still as
    #: the recorded ones did.
    mob_patrol: int = 0
    #: Walk creatures toward the player instead, and how far per tick in wire units.
    mob_chase: bool = True
    mob_speed: int = WALK_UNITS_PER_TICK
    #: How close a creature comes before it stands and faces the player. The first
    #: attempt stopped at 1.5 world units, which is inside the player: it read as
    #: walking through them. This is the skill's own AttackRange, so a creature
    #: halts exactly where it could strike from.
    mob_stop: float = 2.0
    #: How near the player must be before a creature takes an interest. Without
    #: this every creature on the map converged from any distance. Nothing in the
    #: capture fixes the real value, so this is a choice, not a finding.
    mob_aggro: float = 14.0
    #: Duration stamped on a moving entity, for the client to interpolate over.
    tick_duration: int = 18
    #: How many more ticks a dead creature stays in the entity update, so its death
    #: sequence has something to play over. Twenty at ten a second is two seconds.
    corpse_lifetime: int = 20
    #: Experience awarded per kill. Seventeen is what a real award carried.
    kill_experience: int = 17
    #: Experience per level, 0 to never level.
    level_every: int = 0
    #: Zero-based index of the skill a creature swings with. 440 is
    #: AnderworldCreatureStrike, which the tutorial dungeon's creature template
    #: grants; the client refuses a skill its actor does not hold.
    creature_skill: int = 440
    #: How many ticks ahead a skill is announced. Three is what the client's own
    #: skill commands carry, in both samples the capture holds.
    skill_lead: int = 3
    #: The rest of the creature's blow, read off row 441 of the client's own
    #: _Template_Skill rather than guessed. Every one of these was wrong once, and
    #: each wrong one produced a symptom:
    #:
    #:   HitFrame 12    the impact lands twelve ticks into the swing. Sending the
    #:                  hit in the same breath as the skill is why the animation
    #:                  showed for about a hundredth of a second.
    #:   AttackRange 2  how close it must be to strike. Stopping at 3.5 put the
    #:                  creature outside its own reach: it halted far away and kept
    #:                  edging closer for ever.
    #:   HitRange 2.25  how far the blow itself carries.
    #:   CoolDown 2.75  seconds between swings. Confirmed on the wire: 70 ticks
    #:                  between one creature's attacks, and a tick is 40 ms.
    #:   DamageType     DarkMagic and Physical, so two entries. The player's own
    #:                  angrystrike has one, which is what tells the capture's two
    #:                  hit shapes apart: 52 with two types are creatures striking
    #:                  the player, 2 with one type are the player striking back.
    creature_hit_frame: int = 12
    #: SkillUnblockFrame, the swing's total length. Confirmed on the wire for two
    #: other skills: ThingRootsStrike's 30 and ThingSwampStrike's 41 are exactly
    #: what their commands carry.
    creature_unblock_frame: int = 27
    creature_hit_range: float = 2.25
    creature_damage_types: list[int] = field(default_factory=lambda: [0, 4])
    #: The player's health. 236 is measured — a nearly full bar in the capture.
    player_max: int = 236
    #: The resource a skill spends, reported alongside health and left alone.
    player_resource: float = 10.0
    #: Whether a killed creature's body is removed at once.
    mob_despawn: bool = False
    #: What one creature blow takes off, and how often one lands. Both ours.
    creature_damage: float = 3.0
    strike_interval: float = 2.75
    #: How far the player's own blow reaches, in world units.
    #:
    #: Looser than the client's own 1.75 on purpose, because this measures from a
    #: creature's movement record while the client measures from what it draws.
    #: Replaying a real session's twelve attacks against wire positions gives 0.9 to
    #: 3.5; six was too loose and made the player take damage across the room.
    reach: float = 3.5
    #: A creature's health, and what one blow takes off it.
    #:
    #: Twelve is measured and since confirmed in play: decoding a recorded blow
    #: reads "victim health 0, max 12, damage 11" — a creature holding twelve
    #: points, killed by a blow of eleven. The scale matters more than the exact
    #: number: serving 60 against a client that knows a creature has twelve is read
    #: as a *heal* and drawn as a floating +400, which is how this was found.
    #:
    #: Four rather than eleven only so a fight lasts three blows instead of one.
    #:
    #: A caution for whoever revisits this: hunting a plausible number through a
    #: bit-packed message finds mirages. Reading this message's 11 two bits late
    #: gives 44, and its 12 one or two bits late gives 24 and 48 — every one of them
    #: looks like a health value, and one of them briefly convinced me.
    mob_max_health: float = 12.0
    mob_damage: float = 4.0
    #: The maximum reported alongside the current value. The capture's players
    #: carried 234 to 236; a creature's is not observed at all.
    mob_max_health_ceiling: int = 60
    #: World units from the player to place a creature at, or 0 — the default — for
    #: the position its own description carries.
    #:
    #: Rewriting it is off by default because it broke what worked: the creatures
    #: stopped appearing at all. The description is bit-packed, with strings and
    #: single-bit fields, so twelve bytes written at a fixed byte offset shift
    #: everything behind them and the client's decoder gives up — silently, with
    #: nothing in its log. Reading that offset yields plausible coordinates in all
    #: six recorded descriptions, so the field is there; writing it needs the
    #: bit-level layout, not a byte offset.
    mob_near: float = 0.0
    #: Send only the creature's own command instead of the whole recorded batch.
    mob_first_command: bool = False
    #: Spawn this blueprint instead of the recorded one, or None to keep it.
    mob_template: str | None = None
    #: Library blueprint to serve in place of the first creature, or None.
    mob_swap: str | None = None
    #: Send the recorded vicinity announcement before a creature is asked about.
    announce_vicinity: bool = True


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

    rules: Rules = field(default_factory=Rules)
    creatures: dict[bytes, Creature] = field(default_factory=dict)
    players: dict[Address, Player] = field(default_factory=dict)
    #: Blows in flight, as (when it lands, victim, attacker). A creature's blow is
    #: announced when the swing starts and applied at the skill's impact frame.
    pending_hits: list[tuple[float, Address, bytes]] = field(default_factory=list)
    #: The last tick the creatures were stepped on, so a step can be scaled by how
    #: much game time actually passed rather than by how often this is called.
    tick: int = 0
    #: Cached offset between the wire frame and the description frame.
    frame_offset: tuple[float, float, float] | None = None

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
