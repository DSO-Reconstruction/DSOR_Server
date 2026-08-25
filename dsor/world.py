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

import logging
import math
import time
from dataclasses import dataclass, field

from dsor.combat import (
    Hit,
    Kill,
    TargetSkill,
    encode_actors_enter_vicinity,
    encode_discard_monster,
    encode_hit,
    encode_kill,
    encode_player_level,
    encode_target_skill,
    encode_xp_changed,
    damage_at,
    hit_points_at,
    level_for,
)
from dsor.gameplay import (
    ACTOR_ID_OFFSET,
    WALK_SPEED,
    WALK_UNITS_PER_TICK,
    WORLD_SCALE,
    Position,
    actor_id,
    decode_position,
    encode_entity_group_message,
    encode_position,
    heading_to,
    monster_spawn,
    reposition_entity,
    ring_positions,
    with_motion,
)
from dsor.items import item_drop, item_taken, with_drop, with_taken
from dsor.mapdata import attack_skill
from dsor.recorded import (
    combat_ready_mobs,
    entity_descriptions,
    entity_update_template,
    tick_state,
)

log = logging.getLogger("dsor.world")

#: Milliseconds in one game tick.
GAME_TICK_MS = 40
#: The player's own actor, as every command's trailer carries it.
PLAYER_ACTOR = bytes([0x15, 0x00, 0x01, 0x00])
WORLD = WORLD_SCALE

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
    #: Fourteen was too small for this map: the creatures sit sixteen or more units
    #: apart, so only one within arm's reach of the path ever reacted.
    mob_aggro: float = 20.0
    #: Duration stamped on a moving entity, for the client to interpolate over.
    tick_duration: int = 18
    #: How many more ticks a dead creature stays in the entity update, so its death
    #: sequence has something to play over. Twenty at ten a second is two seconds.
    corpse_lifetime: int = 20
    #: Experience awarded per kill. Seventeen is what a real award carried.
    kill_experience: int = 17
    #: Experience per level, or 0 — the default — to follow the client's own curve
    #: from _Template_XPLevels. Kept only for forcing a level quickly in testing.
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
    #: A creature's health, and what one blow takes off it. Damage of 0 — the
    #: default — means "follow the character's level curve" instead.
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
    mob_damage: float = 0.0
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
    #: Skills to mark as owned in the replayed skill book, by name, or a level to
    #: grant everything unlocked at. The book lists all eighteen warrior skills and a
    #: single bit per entry says which the character has — a skill the client shows as
    #: unlocked but refuses to place is one whose bit is clear.
    granted_skills: list[str] = field(default_factory=list)
    grant_up_to_level: int = 0
    #: The level a player arrives at, or 0 to start from nothing. Sets the experience
    #: to that level's floor, so the bar and the level agree — the client is told a
    #: level and both of its thresholds together, and moving one without the others
    #: leaves it describing a different character.
    start_level: int = 0
    #: Whether a dying creature leaves an item where it fell.
    drop_items: bool = True
    #: Whether to answer a pickup at all.
    #:
    #: Answering replays the recorded inventory, which carries the layout of every
    #: storage — so the player's own equipment gets rearranged by another session's
    #: arrangement. Refusing leaves the item on the ground and the client retrying,
    #: which is untidy but touches nothing. Neither is right; building the inventory
    #: is what would be.
    allow_pickup: bool = True
    #: Blueprints to drop, cycled through one per kill, or empty to keep the
    #: recorded one. Anything in the client's _Template_Item; a name it cannot
    #: resolve creates nothing at all, exactly as an unknown monster blueprint does.
    drop_templates: list[str] = field(default_factory=list)


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
    #: The blueprint this creature is, when it comes from the map's own spawn table
    #: rather than from a recorded batch. None means "whatever the recording held".
    blueprint: str | None = None
    #: Where the map says it stands, in the description frame. Kept because that is
    #: the frame a description carries, and converting loses precision.
    described_at: tuple[float, float, float] | None = None
    #: The wire index of the skill it strikes with, or None if its blueprint gives it
    #: none. The tutorial's movement target has only monster_selfkill, and the undead
    #: mage champion has nothing at all — neither should ever hit anybody.
    attack_skill: int | None = None
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
        """Where it started, whatever it has done since."""
        if not self.record:
            return self.position
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
    #: The server's own game tick for this player's connection. Written by whoever
    #: owns the connection, so the world never has to reach for a socket to know
    #: what time it is.
    server_tick: int = 0

    @property
    def alive(self) -> bool:
        return self.health > 0.0


@dataclass
class World:
    """One map instance: the creatures in it, and the players who see them.

    Holds no sockets, no connections and no protocol. Everything here is state and
    the rules that move it.
    """

    #: For log lines. A world does not know which port it is served on.
    name: str = "world"
    rules: Rules = field(default_factory=Rules)
    creatures: dict[bytes, Creature] = field(default_factory=dict)
    players: dict[Address, Player] = field(default_factory=dict)
    #: Blows in flight, as (when it lands, victim, attacker). A creature's blow is
    #: announced when the swing starts and applied at the skill's impact frame.
    pending_hits: list[tuple[float, Address, bytes]] = field(default_factory=list)
    #: The last tick the creatures were stepped on, so a step can be scaled by how
    #: much game time actually passed rather than by how often this is called.
    stepped_tick: int = 0
    #: Cached offset between the wire frame and the description frame.
    frame_offset: tuple[float, float, float] | None = None
    #: Actor ids handed to dropped items. Starts above the recorded creatures so a
    #: drop cannot collide with one of them, or with the player's 0x15.
    next_item: int = 0x40
    #: How far apart two items on the ground must lie, in world units. Ours: the
    #: client stacks items that share a place and a stack crashes it.
    drop_spacing: float = 1.2
    #: How many bag cells there are to hand out. Read off the storage descriptors in
    #: both messages, which carry 5 and 6. Beyond this a pickup is refused rather
    #: than allowed to land outside the grid.
    slot_capacity: int = 5
    #: The first cell to hand out.
    #:
    #: Not zero. The character's login inventory carries two items and **no**
    #: allocations, so those two are placed by the client itself — at the first free
    #: cells, evidently — and claiming cell 0 draws its own assertion by name:
    #:
    #:   *** NEBULA ASSERTION ***  programmer says: Storage slot is occupied
    #:   Game::Inventory::AddItemAtStorageSlot(...)
    #:
    #: Two items at login, so two cells gone. Inferred, not read: there is nothing in
    #: the messages that says where the client put them.
    first_slot: int = 2
    #: How many items have been dropped, which also picks the next blueprint.
    drops: int = 0
    #: The inventory slot the next pickup is put in.
    #:
    #: The bag is a grid, and a slot outside it crashes the client just as surely as
    #: two items in the same cell do. A first attempt started at ten, which is past
    #: the end: the storages the messages describe carry a size byte of 5 and 6, and
    #: the character's own login inventory allocates **no** bag slots at all — its
    #: sword and shield are equipped, and equipment is not in this table. So the
    #: cells are free, and they are few.
    next_slot: int = -1
    #: Items lying on the ground, by actor: where each one lies. Positions matter
    #: beyond bookkeeping — two items in the same place stack, and a stack crashes
    #: the client, so a new drop is nudged clear of the ones already down.
    dropped: dict[bytes, tuple[float, float, float]] = field(default_factory=dict)
    #: What each item lying there is, so the pickup names the same thing the drop
    #: did. Cycling blueprints without this put a different item in the bag from the
    #: one on the ground.
    templates: dict[bytes, str | None] = field(default_factory=dict)
    #: Item ids handed out. Both real ones observed are below 65536 — 6753 on the
    #: ground and 31633 in an inventory — so these stay in that range too. A first
    #: attempt started at 90000, which is past a 16-bit field: if the client narrows
    #: this id anywhere, 90001 becomes 24465 and the lookup cannot succeed.
    next_item_id: int = 40000
    #: Messages produced by the current call, waiting to be handed back.
    _outbox: list[tuple[Address, bytes]] = field(default_factory=list)

    # ── what comes out ───────────────────────────────────────────────────────

    def _emit(self, payload: bytes, address: Address) -> None:
        """Queue a message for *address*.

        The world never sends. It appends here, and whoever called it ships the
        result. That is the whole reason a world could run in its own thread: it
        touches no socket and shares nothing.
        """
        self._outbox.append((address, payload))

    def _drain(self) -> list[tuple[Address, bytes]]:
        out, self._outbox = self._outbox, []
        return out

    def _ready(self) -> "World":
        """Populate on first use, since the creature count comes from the rules."""
        if not self.creatures and self.rules.mobs:
            self.populate(
                combat_ready_mobs()[: self.rules.mobs], self.rules.mob_max_health
            )
        return self

    # ── creatures ────────────────────────────────────────────────────────────

    def populate_from_map(
        self,
        points: list[tuple[str, float, float, float]],
        max_health: float,
        first_actor: int = 0x80,
    ) -> None:
        """Fill the world from the map's own spawn table.

        Twenty-five creatures at the positions the client's map data gives, against
        the six a recorded batch held — and three of them are champions. Actors start
        at 0x80 so they cannot collide with the player's 0x15 or with the items,
        which take 0x40 upward.

        The positions are in the description frame, so they are stored as given and
        converted only where a movement record needs them.
        """
        # A movement record, borrowed and re-addressed. Everything downstream — the
        # chase, the hit, the corpse — works off a record, and a mapped creature has
        # none of its own; only the actor and the position in it matter, and both are
        # rewritten.
        base = combat_ready_mobs()[0]
        for index, (blueprint, x, elevation, y) in enumerate(points):
            actor = bytes([(first_actor + index) & 0xFF, 0x00, 0x01, 0x00])
            record = bytearray(base)
            record[ACTOR_ID_OFFSET : ACTOR_ID_OFFSET + 4] = actor
            self.creatures[actor] = Creature(
                actor=actor,
                record=bytes(record),
                position=Position(0, 0, 0),
                health=max_health,
                max_health=max_health,
                blueprint=blueprint,
                described_at=(x, elevation, y),
                attack_skill=attack_skill(blueprint),
            )
        self._place_from_descriptions()

    def _place_from_descriptions(self) -> None:
        """Give every mapped creature a wire position from its described one."""
        offset = self.frame_offset
        if offset is None:
            # Derived the same way the kill position is: the constant between the two
            # frames, read off a creature that exists in both.
            for record in combat_ready_mobs():
                try:
                    described = monster_spawn(entity_descriptions()[actor_id(record)])
                except (KeyError, ValueError):
                    continue
                home = decode_position(record)
                offset = (
                    described[0] - home.x / WORLD,
                    described[1] - home.elevation / WORLD,
                    described[2] - home.y / WORLD,
                )
                self.frame_offset = offset
                break
        if offset is None:
            return
        for creature in self.creatures.values():
            if creature.described_at is None:
                continue
            x, elevation, y = creature.described_at
            creature.position = Position(
                x=round((x - offset[0]) * WORLD),
                elevation=round((elevation - offset[1]) * WORLD),
                y=round((y - offset[2]) * WORLD),
            )

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
        """Remove a player entirely. One place, not nine.

        And when the last one goes, put the creatures back. Not respawning — the
        dead stay dead while anyone is here — but a world that carries one session's
        corpses into the next gets emptier every time somebody reconnects. That is
        what left a player standing on the spot where a creature had died an hour
        earlier, swinging at nothing, with the survivors too far away to notice.
        """
        self.players.pop(address, None)
        self.pending_hits = [h for h in self.pending_hits if h[1] != address]
        if not self.players:
            self.reset_creatures()

    def reset_creatures(self) -> None:
        """Every creature alive again, back where it was recorded.

        And the counters with them. These three lines once landed in
        ``age_corpses``, which runs ten times a second: the slot counter restarted
        every tick so two pickups both took cell 2, the blueprint table was wiped so
        a picked-up item arrived as whatever the recording held, and — worst — actor
        ids repeated, so two different items could claim the same one.
        """
        for creature in self.creatures.values():
            creature.health = creature.max_health
            creature.position = creature.home()
            creature.corpse_ticks = 0
            creature.described = False
        self.dropped.clear()
        self.templates.clear()
        self.next_item = 0x40
        self.next_slot = -1

    def inhabitants(self) -> list[Player]:
        return [p for p in self.players.values() if p.in_world]

    # ── the rules ────────────────────────────────────────────────────────────

    def wire_position(self, actor: bytes):
        """Where a creature stands, in the same frame as the player's own position.

        This is the distinction that made every distance bound fail, in both
        directions. A creature has two positions: the three floats inside its
        description, and the three signed 16-bit fields in its movement records —
        and **they are not the same frame**. The player's trajectory decoded from the
        wire runs z=46 down to z=21; the descriptions put the creatures at z=52 to 55,
        an interval it never enters. Measured against the descriptions, the player was
        never closer than 18 units and 30 at the moment of every attack, so a bound of
        1.75 refused everything and a bound of 6 refused everything. Measured on the
        wire, the same player came within 3 units of a creature.

        So: compare wire against wire.
        """
        creature = self._ready().creature(actor)
        if creature is not None:
            return creature.position
        for record in combat_ready_mobs():
            if actor_id(record) == actor:
                return decode_position(record)
        return None


    def described_position(self, where) -> tuple[float, float, float]:
        """A wire position in the frame the descriptions use.

        The two frames differ by a constant per map, and the constant is derived
        here rather than written down: every described creature sits at the same
        offset from its own movement record — (-3.50, +2.69, +31.18) for the tutorial
        dungeon, and identical for all three creatures checked. The live service
        shows the same thing on its own map with a different constant, which is why
        this reads it off the data instead of hard-coding it.
        """
        if where is None:
            return (0.0, 0.0, 0.0)
        if self.frame_offset is None:
            self.frame_offset = (0.0, 0.0, 0.0)
            for record in combat_ready_mobs():
                try:
                    described = monster_spawn(entity_descriptions()[actor_id(record)])
                except (KeyError, ValueError):
                    continue
                home = decode_position(record)
                self.frame_offset = (
                    described[0] - home.x / WORLD,
                    described[1] - home.elevation / WORLD,
                    described[2] - home.y / WORLD,
                )
                break
        dx, dy, dz = self.frame_offset
        return (
            where.x / WORLD + dx,
            where.elevation / WORLD + dy,
            where.y / WORLD + dz,
        )


    def entity_update(
        self, position, tick: int = 0, trailing: list[bytes] | None = None
    ) -> bytes:
        """The player's position, and any creatures placed around it.

        Several entities travel in one 0x85/0x005F, chained by a two-byte separator
        rather than counted. The creature records are real ones lifted from the
        tutorial dungeon with only their position rewritten: the zone content
        message is what tells the client the map contains them, so an invented
        identifier would be an entity it cannot draw.
        """
        player = with_motion(
            encode_position(position) + entity_update_template()[6:], position, tick
        )
        if not self.rules.mobs:
            return encode_entity_group_message([player], trailing)

        # The living, and the recently dead. Dropping a creature from the tick the
        # instant it dies is what made it vanish with no animation: the client was
        # told to play a death sequence over something this server had stopped
        # mentioning, so there was nothing to play it on. The update went from 155
        # bytes to 133 in the same breath as the kill — one record and its separator.
        #
        # Still bounded, because re-announcing a creature *after* the client has
        # finished removing it makes it flicker back into existence.
        announced = self._ready().announced()
        if not announced:
            return encode_entity_group_message([player], trailing)
        if self.rules.mob_chase:
            # Toward the player, and at a player's pace. Two things made the first
            # attempt look like a teleport rather than a walk, and they were the same
            # thing twice: the record was stamped with speed zero, so the client had
            # no velocity to extrapolate and simply snapped to each new position and
            # played the idle animation over it; and the step was 60 wire units per
            # 100 ms update, four times a real walk. A speed byte, a heading, and
            # WALK_UNITS_PER_TICK fix both. The animation follows for free, because
            # it is NetworkSmoothMotionProperty that both interpolates and animates.
            placed = []
            elapsed = max(1, tick - self.stepped_tick)
            self.stepped_tick = tick
            reach = self.rules.mob_stop * WORLD_SCALE
            aggro = self.rules.mob_aggro * WORLD_SCALE
            for creature in announced:
                template, here = creature.record, creature.position
                span = here.distance_to(position)
                heading = heading_to(position.x - here.x, position.y - here.y)
                if span > aggro:
                    # Out of range: leave it exactly as it stands, facing as it was.
                    # A heading here would turn every creature on the map toward the
                    # player from across the zone.
                    placed.append(with_motion(template, here, tick, speed=0))
                    continue
                if span <= reach + self.rules.mob_speed * elapsed:
                    # Snapped to the stop distance instead of approaching it
                    # asymptotically. Clamping the step to the distance remaining left
                    # the creature always a fraction outside, announcing a walk it
                    # never finished: "elles s'arretent loin et continuent de vouloir
                    # venir".
                    # Arrived: stand and face the player. Standing creatures in the
                    # capture carry speed zero and duration zero, so anything else
                    # here would claim a movement the client would extrapolate into
                    # the player.
                    placed.append(
                        with_motion(template, here, tick, speed=0, heading=heading)
                    )
                    continue
                step = min(self.rules.mob_speed * elapsed, span - reach)
                moved = Position(
                    x=here.x + round((position.x - here.x) * step / span),
                    elevation=position.elevation,
                    y=here.y + round((position.y - here.y) * step / span),
                )
                creature.position = moved
                placed.append(
                    with_motion(
                        template,
                        moved,
                        tick,
                        duration=self.rules.tick_duration,
                        speed=WALK_SPEED,
                        heading=heading,
                    )
                )
            return encode_entity_group_message([player, *placed], trailing)

        if self.rules.mob_patrol:
            # Beyond what the capture shows: its creatures stood still, 620 of 621
            # updates at one position with duration zero. This walks them in a slow
            # circle around that position instead, with a duration for the client to
            # interpolate over, because a standing monster is hard to tell from a
            # broken one.
            placed = []
            for offset, template in enumerate(templates):
                home = decode_position(template)
                angle = (tick / 100.0) + offset
                placed.append(
                    with_motion(
                        template,
                        Position(
                            x=home.x + round(self.rules.mob_patrol * math.cos(angle)),
                            elevation=home.elevation,
                            y=home.y + round(self.rules.mob_patrol * math.sin(angle)),
                        ),
                        tick,
                        duration=self.rules.tick_duration,
                    )
                )
            return encode_entity_group_message([player, *placed])

        if not self.rules.mob_radius:
            # Where the capture put them. Their own positions are valid by
            # construction — they stand on ground the map actually has — whereas a
            # ring around the player is a guess, and a creature inside a wall is one
            # the client has every reason to refuse to draw.
            return encode_entity_group_message(
                [player, *(with_motion(t, decode_position(t), tick) for t in templates)]
            )

        placed = [
            reposition_entity(template, where)
            for template, where in zip(
                templates, ring_positions(position, len(templates), self.rules.mob_radius)
            )
        ]
        return encode_entity_group_message([player, *placed])


    def resolve_attack(self, sender: Address) -> None:
        """Work out what the player just hit, and take health off it.

        The client's TargetSkillCommand names no target: twenty-one bytes holding a
        skill id, a float, a tick and two fields whose meaning is not established, and
        not one actor id among them. Deciding who was hit is therefore the server's
        job, which is why hitting a creature forever did nothing — nobody was
        deciding.

        What this does is the simplest defensible rule — nearest creature to the
        player — and it is ours rather than measured. The capture never reports a
        creature's stats at all, so both the health scale and the damage are invented;
        only the message they travel in is real.
        """
        position = self.player(sender).position
        if position is None:
            return
        alive = [creature.actor for creature in self._ready().engageable()]
        if not alive:
            return

        here = (position.x / WORLD, position.y / WORLD)

        def distance(actor: bytes) -> float:
            where = self.wire_position(actor)
            return float("inf") if where is None else position.distance_to(where)

        # Keep hitting whatever is already being hit. The client names no target —
        # measured: across a session with six kills, not one message from it carries a
        # creature's id except the question "what is this entity" — so the server
        # chooses, and choosing the nearest afresh on every blow makes the choice flip
        # between creatures standing close together. That is what looked like damage
        # being shared between them.
        latched = self.player(sender).target
        if latched is not None and latched in alive:
            target = latched
        else:
            # Generous rather than exact. Filtering at the client's own 1.75 units
            # stopped damage entirely — eleven attacks refused in one session —
            # because the distance here is measured against the coordinates in a
            # creature's description while the client measures against what it draws,
            # and the two differed by 2.3 against 1.75. But no bound at all meant a
            # blow could land on a creature thirty units away once the near one died.
            nearby = [a for a in alive if distance(a) <= self.rules.reach * WORLD]
            if not nearby:
                log.info(
                    "%s: %s attacked with nothing within %.0f units",
                    self.name,
                    sender,
                    self.rules.reach,
                )
                return
            target = min(nearby, key=distance)
            self.player(sender).target = target

        struck = self.creatures[target]
        # What the character hits for, from the client's own class curve rather than
        # a number invented here: fifteen at level one, not four. Equipment adds to
        # it and this does not model that — an item's damage is rolled per instance
        # and scaled to a level, and is not in its template at all.
        blow = self.rules.mob_damage or damage_at(self.player(sender).level)
        left = max(0.0, struck.health - blow)
        struck.health = left

        # The blow, generated. A creature has no health message of its own — every
        # stats update in both captured sessions targets the player — so the client
        # takes the victim's health from this message and nowhere else.
        #
        # Which is precisely why replaying one could never work: a recorded blow is a
        # *killing* blow, so it carries zero, and the creature died on the spot by the
        # unanimated path. The client said so: "Victim ... is not alive or cannot
        # receive", then "received kill message twice" when the real death arrived
        # after. Generating it is what lets a creature survive a hit at all.
        victim = int.from_bytes(target, "little")
        player = int.from_bytes(PLAYER_ACTOR, "little")
        self._emit(encode_hit(
                Hit(
                    victim=victim,
                    attacker=player,
                    damage=int(blow),
                    victim_health=int(left),
                    victim_max_health=int(self.rules.mob_max_health),
                    # The attacker, which for the player's own blow is the player.
                    # Seventy-four real hits carry the attacker here, never the
                    # victim, and an earlier note claiming otherwise is refuted.
                    combat_value_owner=player,
                    # Zero, as the real blow carries. The floating number comes from
                    # the damage field; putting it here as well draws a second one.
                    combat_value=0.0,
                    tick=self.player(sender).server_tick,
                )
            ),
            sender,
        )
        log.info(
            "%s: %s hit entity %s for %.0f, %.1f left",
            self.name,
            sender,
            target.hex(" "),
            blow,
            left,
        )

        if left > 0.0:
            return
        self.creature_died(sender, target)

    def creature_died(self, sender: Address, target: bytes) -> None:
        """Everything a creature's death sends.

        Split out of the blow that causes it so a death can also be commanded — by
        the console, or by the client's own god mode — without pretending an attack
        happened.
        """
        # Dead. Two generated commands, in this order, and neither is what this
        # server used to send.
        #
        # ActorsLeftVicinityCommand — which is what was sent before — does not kill
        # anything: it sets the entity invisible. That is why creatures stayed
        # standing at zero health. What kills is KillCommand, which carries the
        # impulse the corpse is thrown with and a flag saying whether the body is
        # removed; the client's death sequence hangs off it. DiscardMonsterCommand
        # then deletes the entity and the actor outright, and its body is empty, so
        # it needs no recording — which is how a creature with no captured removal
        # can be retired at all.
        victim = int.from_bytes(target, "little")
        tick = self.player(sender).server_tick
        # Where it stands *now*, in the description frame rather than the wire one.
        # Two mistakes on this one field. The frame first: a real kill for this
        # creature carries (-45.68, 54.63) where its movement records put it at
        # (-42.18, 23.45), and a death announced thirty units from the body has
        # nothing to play over. Then the position itself: this read the creature's
        # recorded *spawn*, which was invisible while creatures stood still and
        # obvious the moment they chased — they died back where they started.
        described = None
        here = self.wire_position(target)
        if here is not None:
            described = self.described_position(here)
        if described is None:
            try:
                described = monster_spawn(entity_descriptions()[target])
            except (KeyError, ValueError):
                described = None
        self._emit(encode_kill(
                Kill(
                    victim=victim,
                    killer=int.from_bytes(PLAYER_ACTOR, "little"),
                    # Where it dies, in world units — not a direction. A real kill
                    # carries the creature's own position here.
                    position=described if described else (0.0, 0.0, 0.0),
                    tick=tick,
                    # False, so the body is left lying. True means "remove it",
                    # which is what made creatures vanish the instant they died with
                    # no animation — nothing can play over a corpse already cleared.
                    despawn=self.rules.mob_despawn,
                )
            ),
            sender,
        )
        # No DiscardMonsterCommand here. It deletes the entity outright, and sending
        # it in the same breath as the kill destroys the death sequence before it can
        # play — the creature simply vanished. The kill's own despawn flag already
        # tells the client to clear the body; discarding is for retiring a creature
        # that is not dying, and for one no capture contains a removal for.
        self.creatures[target].corpse_ticks = self.rules.corpse_lifetime
        # And what it leaves behind, at the place it fell rather than the place the
        # recording's creature fell. Only the actor and the position are rewritten;
        # the rest of the command is copied bit for bit, because its tail is not
        # understood and inventing a tail is how the skill command came to be 26
        # bytes of a 64-byte message.
        if self.rules.drop_items and described is not None:
            # One item per blueprint named, all from the same body. They are spread
            # out rather than piled up: two items in the same place stack, and a
            # stack crashes the client.
            for blueprint in self.rules.drop_templates or [None]:
                self.next_item += 1
                lying = bytes([self.next_item & 0xFF, 0x00, 0x01, 0x00])
                where = self.clear_of_other_drops(described)
                self.dropped[lying] = where
                self.templates[lying] = blueprint
                self.drops += 1
                self._emit(
                    with_drop(item_drop(), lying, where, template=blueprint), sender
                )
            log.info(
                "%s: %s dropped %d item(s) around (%.2f, %.2f, %.2f)",
                self.name,
                target.hex(" "),
                len(self.rules.drop_templates or [None]),
                *described,
            )
        if self.rules.kill_experience:
            # Addressed to the player, not the creature — which is why this server's
            # batch filter dropped it for hours: it keeps what belongs to the creature
            # or to nobody, and experience belongs to whoever landed the blow.
            # Banked first, because the field is a total. Sending the award itself
            # lit up the first creature and nothing after it — a total that never
            # moves is a gain of zero.
            earner = self.player(sender)
            earner.experience += self.rules.kill_experience
            # The level is not a knob: it follows from the experience through the
            # client's own curve, 0 / 100 / 440 / 1000 for the first four. The bar
            # only fills correctly if the message carries the level and both of its
            # thresholds, which is what was hard-coded to level 1 before.
            level = level_for(earner.experience)
            levelled = level > earner.level
            self._emit(
                encode_xp_changed(
                    earner.experience,
                    int.from_bytes(PLAYER_ACTOR, "little"),
                    level=level,
                    levelled=levelled,
                ),
                sender,
            )
            if levelled:
                earner.level = level
                self._emit(
                    encode_player_level(
                        level, int.from_bytes(PLAYER_ACTOR, "little")
                    ),
                    sender,
                )
                log.info("%s: %s reached level %d", self.name, sender, level)
        log.info(
            "%s: entity %s killed%s",
            self.name,
            target.hex(" "),
            " (+%d xp)" % self.rules.kill_experience if self.rules.kill_experience else "",
        )
        self.player(sender).target = None


    def creature_swing(self, sender: Address) -> None:
        """Let a live creature hit the player, every so often.

        Two messages, both replayed from a session where creatures fought back: the
        blow, and the player's health afterwards. The blow is one of the sixteen
        163-byte hits that name no creature — the mark of a blow taken rather than
        landed.

        This direction works where the other cannot: the player's actor is bound to an
        entity, so its health updates take effect. A creature's never do, which is why
        no creature's health is reported at all.

        The rate and the damage are this server's, not measured. The real session's
        sixteen blows were spread over a fight this server has no model of.
        """
        if not self.rules.creature_damage:
            return
        position = self.player(sender).position
        if position is None:
            return
        def near(actor: bytes) -> bool:
            creature = self.creature(actor)
            if creature is not None and creature.blueprint and not creature.attack_skill:
                # No attack in its own blueprint, so it never strikes. The movement
                # target's only skill is monster_selfkill and it stands eight units
                # from where a player arrives: it walked over and hit, invisibly.
                return False
            where = self.wire_position(actor)
            return where is not None and (
                position.distance_to(where) <= self.rules.creature_hit_range * WORLD
            )

        if not any(
            near(creature.actor) for creature in self._ready().engageable()
        ):
            # Nothing alive within reach. Losing health with no creature beside you
            # was this condition being "any creature anywhere", which every described
            # creature satisfied for the whole session.
            return
        now = time.monotonic()
        striker = self.player(sender)
        if not striker.alive:
            return
        if now - striker.last_struck < self.rules.strike_interval:
            return
        striker.last_struck = now

        # Whoever is nearest does the striking, so the blow names a real attacker.
        attacker = min(
            (
                creature.actor
                for creature in self._ready().engageable()
                if near(creature.actor)
            ),
            key=lambda actor: position.distance_to(self.wire_position(actor)),
        )

        # Floored above zero. Driving it to zero leaves the player standing at an
        # empty bar and not dying, because nothing here kills a player — that is a
        # separate command this server does not send yet, and an empty bar with no
        # death is worse than a scratch.
        # The swing first. There is no "play this animation" command in the protocol —
        # 371 command classes and not one names a sequence or a gesture — so a creature
        # animates only when sent a skill. The hit asks for no animation at all, for
        # anyone, which is why a creature that damages the player still stands there.
        #
        # The skill has to be one the creature's own template grants: the client asks
        # its actor whether it holds that template before visualising anything. The
        # tutorial dungeon's creature has AnderworldCreatureStrike, which is row 441 of
        # the client's skill table, and the wire wants a zero-based index — 440. That
        # mapping is confirmed the other way round too: the player's angrystrike is row
        # 1839 and the client sends 1838.
        #
        # And it travels *inside* a movement batch, because that is the only form the
        # capture contains: all 56 skill commands the real server sent ride behind
        # the movement records of a 0x005F, and not one travelled alone. Sending it
        # standalone reproduced the real bytes exactly — 26 of 26 — and still drew
        # nothing, which left the framing as the last difference.
        where = self.wire_position(attacker)
        heading = 0.0
        if where is not None:
            # From the *target* to the attacker, which is the reverse of the direction
            # the blow travels. Measured, not reasoned: both real samples fit this to
            # within 0.3 degrees, and the forward vector misses by 180. Sending the
            # forward one made creatures swing away from the player.
            heading = math.atan2(where.x - position.x, where.y - position.y)
        # Three ticks ahead, on the live clock rather than the last tick the client
        # happened to report. Both skill commands the client itself sent announce a
        # start three ticks past its own latest movement tick — 2735 against 2732,
        # 2788 against 2785 — so a skill starts in the near future, not now. Feeding
        # it a tick that had already passed is what showed a hundredth of a second of
        # the swing: the visualizer was created and finished in the same breath.
        now = self.player(sender).server_tick
        skill = encode_target_skill(
            TargetSkill(
                attacker=int.from_bytes(attacker, "little"),
                target=int.from_bytes(PLAYER_ACTOR, "little"),
                skill_id=(
                    (self.creature(attacker).attack_skill if self.creature(attacker) else None)
                    or self.rules.creature_skill
                ),
                heading=heading,
                start_tick=now + self.rules.skill_lead,
                hit_frame=self.rules.creature_hit_frame,
                unblock_frame=self.rules.creature_unblock_frame,
                position=self.described_position(where),
            )
        )
        self._emit(self.entity_update(position, now, trailing=[skill]), sender
        )

        # And the blow lands twelve ticks later, not now. The skill's own HitFrame
        # says 12, and the wire agrees: the median gap from a real announcement to
        # the next hit is twelve ticks. Landing it immediately is what cut the swing
        # short — the client had resolved the blow before the animation could play.
        self.pending_hits.append(
            (
                time.monotonic() + self.rules.creature_hit_frame * GAME_TICK_MS / 1000.0,
                sender,
                attacker,
            )
        )


    def land_hits(self) -> None:
        """Apply the blows whose impact frame has arrived."""
        now = time.monotonic()
        due = [entry for entry in self.pending_hits if entry[0] <= now]
        if not due:
            return
        self.pending_hits = [
            entry for entry in self.pending_hits if entry[0] > now
        ]
        for _, sender, attacker in due:
            victim = self.player(sender)
            position = victim.position
            if position is None or not victim.in_world:
                continue
            if not victim.alive:
                continue
            left = max(0.0, victim.health - self.rules.creature_damage)
            victim.health = left
            # One message, not two. This used to replay a recorded blow *and* send a
            # separate vitals update, so the number the player saw came from another
            # session's fight while the health actually applied came from here — a "-1"
            # floating up while the bar dropped by fifteen, and a "+400" whenever the
            # vitals message happened to raise the bar rather than lower it. A hit
            # carries the victim's health, so it is the only thing that needs sending.
            self._emit(encode_hit(
                    Hit(
                        victim=int.from_bytes(PLAYER_ACTOR, "little"),
                        attacker=int.from_bytes(attacker, "little"),
                        damage=int(self.rules.creature_damage),
                        victim_health=int(left),
                        victim_max_health=int(self.rules.player_max),
                        combat_value_owner=int.from_bytes(attacker, "little"),
                        combat_value=0.0,
                        damage_types=list(self.rules.creature_damage_types),
                        tick=self.player(sender).server_tick,
                    )
                ),
                sender,
            )
            if left <= 0.0:
                # A player at zero health with nothing killing them stands at an
                # empty bar for ever. The same command that kills a creature kills a
                # player — and the death stands: refilling here is what let a corpse
                # be killed again and again.
                self._emit(encode_kill(
                        Kill(
                            victim=int.from_bytes(PLAYER_ACTOR, "little"),
                            killer=int.from_bytes(attacker, "little"),
                            position=(position.x / WORLD, 0.0, position.y / WORLD),
                            tick=self.player(sender).server_tick,
                            despawn=False,
                        )
                    ),
                    sender,
                )

                log.info("%s: %s was killed by %s", self.name, sender, attacker.hex(" "))
            log.info(
                "%s: entity %s struck %s, %.0f health left",
                self.name,
                attacker.hex(" "),
                sender,
                left,
            )

    # ── the tick ─────────────────────────────────────────────────────────────

    def _tick_pair(self, sender: Address) -> None:
        """One tick for one player: the 0x004F state, then its position update.

        The order matters — the real server always sends 0x004F first — and the
        pair is what the client expects continuously, not once.
        """
        player = self.player(sender)
        if player.position is None:
            return
        self._emit(tick_state(), sender)
        self._emit(self.entity_update(player.position, player.server_tick), sender)

    def tick(self) -> list[tuple[Address, bytes]]:
        """Advance the world one tick and return everything it wants sent.

        Corpses age first, so one killed this tick is still reported once.
        """
        self.age_corpses()
        for player in self.inhabitants():
            self._tick_pair(player.address)
            self.creature_swing(player.address)
        self.land_hits()
        return self._drain()

    def enter(self, sender: Address) -> list[tuple[Address, bytes]]:
        """The first tick pair a player gets, on arrival."""
        self._tick_pair(sender)
        return self._drain()

    def attack(self, sender: Address) -> list[tuple[Address, bytes]]:
        """A player has swung: resolve it and return what to send."""
        self.resolve_attack(sender)
        return self._drain()

    def blueprint_for(self, index: int) -> str | None:
        """The blueprint the *index*-th drop leaves, cycling through the rules."""
        names = self.rules.drop_templates
        return names[index % len(names)] if names else None

    def clear_of_other_drops(
        self, where: tuple[float, float, float]
    ) -> tuple[float, float, float]:
        """*where*, moved until no other item lies within ``drop_spacing``.

        Two items sharing a place stack, and a stack crashes the client. Creatures
        that die together — a group killed in one fight, or one killed where another
        already fell — would otherwise drop on top of each other.

        The nudge spirals outward so the item stays near the body, and gives up after
        a bounded number of tries rather than looping.
        """
        import math

        def clashes(spot: tuple[float, float, float]) -> bool:
            return any(
                math.hypot(spot[0] - other[0], spot[2] - other[2])
                < self.drop_spacing
                for other in self.dropped.values()
            )

        if not clashes(where):
            return where
        for step in range(1, 25):
            angle = step * 2.399963  # the golden angle, so the ring fills evenly
            radius = self.drop_spacing * (1 + step * 0.35)
            spot = (
                where[0] + radius * math.cos(angle),
                where[1],
                where[2] + radius * math.sin(angle),
            )
            if not clashes(spot):
                return spot
        return where

    def pick_up(self, sender: Address, actor: bytes) -> list[tuple[Address, bytes]]:
        """A player has asked for the item lying at *actor*.

        The request is four bytes and nothing else — the item's actor id — which is
        the whole of PickupItemCommand as the client sends it.

        The answer replays a real ItemInfoCommand with the actor rewritten. Nothing
        else in it is touched: it carries an item id and fields whose meaning is not
        established, and the client does not need a name here because the 0x002D that
        put the item on the ground already told it what the actor is.
        """
        if not self.rules.allow_pickup:
            log.info("%s: %s asked for item %s, and pickup is off",
                     self.name, sender, actor.hex(" "))
            return []
        if actor not in self.dropped:
            log.info("%s: %s asked for item %s, which is not lying here",
                     self.name, sender, actor.hex(" "))
            return []
        if self.next_slot < 0:
            self.next_slot = self.first_slot
        if self.next_slot >= self.slot_capacity:
            log.info(
                "%s: %s asked for item %s and the bag is full at %d cells",
                self.name, sender, actor.hex(" "), self.slot_capacity,
            )
            return []
        del self.dropped[actor]
        blueprint = self.templates.pop(actor, None)
        slot = self.next_slot
        self.next_slot += 1
        self._emit(
            with_taken(
                item_taken(),
                actor,
                template=blueprint,
                slot=slot,
            ),
            sender,
        )
        log.info("%s: %s picked up item %s into cell %d of %d",
                 self.name, sender, actor.hex(" "), slot, self.slot_capacity)
        return self._drain()

    def smite_all(self, sender: Address) -> list[tuple[Address, bytes]]:
        """Kill every live creature, as god mode asks.

        Through the same path a blow takes, so the death animation, the experience
        and the drops all happen exactly as they would in a fight. Anything else
        would be a second implementation of dying, and this project already learned
        what two sources of the same truth cost.
        """
        for creature in list(self.creatures.values()):
            if not creature.alive:
                continue
            creature.health = 0.0
            self.creature_died(sender, creature.actor)
        return self._drain()
