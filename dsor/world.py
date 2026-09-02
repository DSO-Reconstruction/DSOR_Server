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
from datetime import datetime
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
    resource_at,
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
    encode_actor_vitals,
    encode_entity_group_message,
    encode_position,
    HEADING_UNITS,
    heading_to,
    monster_spawn,
    reposition_entity,
    ring_positions,
    with_motion,
)
from dsor.items import drop_template, item_drop, item_taken, with_drop
from dsor import (
    effect_titles,
    effects,
    inventory,
    location,
    measured,
    statuseffect,
    usable,
    vitals,
)

#: How long after arriving the level is stated a second time, in seconds.
#: The replayed player state is a level-1 character's, so the interface starts
#: there, and one message at map entry may land before it is listening.
LEVEL_AGAIN = 6.0

#: Where effect instance handles start. The live service was measured
#: handing out 66,052 to 66,066 in one session, in the same 0x1xxxx space
#: actors live in.
FIRST_HANDLE = 0x10200
from dsor.mapdata import attack_skill
from dsor.actors import ActorSpace, RECORDED_PLAYER, encode as encode_actor
from dsor.monsters import Monster, monster
from dsor.skills import Skill, skill as skill_at
from dsor.titles import title_of
from dsor.trust import Claims, movement_is_plausible, off_cooldown
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
#: The actor the recorded player description names. The first player in a world gets
#: it so that recording still matches; everybody after gets one of their own from the
#: world's :class:`~dsor.actors.ActorSpace`.
#:
#: It used to be *the* player actor, a module constant, which meant every player in a
#: world was the same entity. That is the first thing that had to go for two people to
#: stand in one map at once.
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
    #: Zero — the default — means "take it from the creature's own skill", the way
    #: mob_damage takes zero to mean "follow the character's level curve". A number
    #: here forces that value on every creature, which is what the debug console does
    #: when a swing needs to be watched in slow motion.
    creature_hit_frame: int = 0
    #: SkillUnblockFrame, the swing's total length. Confirmed on the wire for two
    #: other skills: ThingRootsStrike's 30 and ThingSwampStrike's 41 are exactly
    #: what their commands carry.
    creature_unblock_frame: int = 0
    creature_hit_range: float = 0.0
    creature_damage_types: list[int] = field(default_factory=lambda: [0, 4])
    #: The player's health. 236 is measured — a nearly full bar in the capture.
    #: Zero — the default — means "what the character's level says", from
    #: _Template_XPLevels: 225 at level 1, 2700 at 15, 450000 at 100. A number forces
    #: one figure regardless of level.
    #:
    #: 236 was the old default and it is a measured number — a nearly full bar on a
    #: captured level 1 warrior, against the table's 225, the difference being
    #: equipment. It was right for that one character, and a level 15 warrior with 236
    #: of a stated 2700 shows a bar one twelfth full.
    player_max: int = 0
    #: The resource a skill spends, reported alongside health and left alone.
    #: Zero — the default — means "what the level table's BaseMana says", which is a
    #: hundred at every level. Ten was written here and it is why nothing appeared to
    #: happen: warshout's ResourceGain of 0.6 put six points on a bar of a hundred.
    player_resource: float = 0.0
    #: A diagnostic: an extra effect added to whatever a skill grants.
    #:
    #: It exists because refining the message stopped paying. The database is
    #: byte-identical to the client's own, md5 included; the wire index mapping is
    #: uniquely best at rowid-1 (11 name agreements against 0 contradictions, every
    #: other shift worse); the message is field-for-field the same as the live
    #: service's for this skill; and neither the traffic nor the replayed blobs carry
    #: the effect the operator sees. So the question is no longer which field is wrong.
    #:
    #: Set it to an unmistakable effect -- debuff_cc_stun is "Stunned", with its own
    #: animation and icon, and no warrior self-buff produces it -- and one cast tells
    #: three things apart. Seeing it alongside the skill's own effects means these
    #: messages are rendered and the stray icon comes from elsewhere. Seeing it and not
    #: the skill's own means the effects are rendered but the skill's are wrong. Not
    #: seeing it at all means the client is not drawing this message.
    probe_effect: str = ""

    #: How much andermant to write into the replayed player state. Zero leaves it as
    #: recorded, which is 600.
    #:
    #: It lives in the *roster*, not in the player state, and it is the account's rather
    #: than the character's: all four characters of the live service's roster carry 4,814
    #: while the recording's one carries 600 -- exactly what the operator saw on the live
    #: service and on this server. Two independent values, each matching a screen, one of
    #: them identical across four entries. See dsor/charlist.py.
    andermant: int = 0

    #: Whether to write the saved level and experience into the replayed roster.
    #:
    #: On, now that the fields are measured rather than guessed: five characters, four
    #: of them the live service's own, put the experience 256 bits and the level 288
    #: bits past the end of the entry's map string. A first attempt wrote sixteen bits
    #: 145 bits before the name and broke the login; that field is 1 in the service's
    #: level-100 characters as well as in the level-1 recording.
    roster_level: bool = True

    #: Whether to draw what a skill puts on the ground. Nine skills of the four playable
    #: classes place only ground effects and do nothing at all without it.
    location_effects: bool = True

    #: Whether to fill the action bar with the class's skills.
    #:
    #: The bar is not the book. Granting a skill in the book tells the client the
    #: character owns it; the bar is what it can press. The recorded state has one slot
    #: filled -- angrystrike -- and sixteen empty, the client reported that same single
    #: entry in all 180 QuickSlotsCommand messages of one session, and the live service
    #: answers none of them. So the bar comes from the served state or from nowhere.
    fill_action_bar: bool = True
    #: What to put in the effect element's seventh 32-bit field, or -1 to keep the 100
    #: a real server sends. A dial, not a setting: it is the prime suspect for the
    #: sequencer assertion that holds animated effects back, and it has been read wrong
    #: twice already, so it is something to try rather than something known.
    effect_stack: int = -1
    #: What share of the blow a damage-over-time effect deals each tick. Ours: the
    #: templates read a named variable a talent sets, and there is no talent here.
    dot_share: float = 0.15
    #: Which class's skills a modifier may name. One class per world for now, because
    #: this server serves one character.
    character_class: str = "warrior"
    #: Whether to check what the client claims: where it is, which skill it used, how
    #: often, what it cost, and what it may pick up. Off is for debugging a capture,
    #: not for running a service.
    enforce: bool = True
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
    #: How far apart two creatures stand when they have both arrived, in world units.
    #: They used to have no separation at all and twenty of them stood on one point --
    #: the same problem clear_of_other_drops solves for items on the ground, which was
    #: solved there and not here.
    mob_spacing: float = 1.6
    #: Whether an implausible move is *refused* as well as counted. Off, and the
    #: reason is in accept_movement: the position this server keeps is the one it
    #: echoes back, so refusing drags the client to a stale one and every honest record
    #: after it measures as a jump. Correcting a player needs a model of where they
    #: could be, and there is no collision data here.
    refuse_movement: bool = False
    #: How far a player may be from an item and still pick it up, in world units.
    #: Ours: the client has an AutoPickupRange somewhere but not in _Globals, so this
    #: is a bound rather than the game's own.
    pickup_range: float = 12.0
    #: Whether a killed creature's body is removed at once.
    mob_despawn: bool = False
    #: What one creature blow takes off, and how often one lands. Both ours.
    #: Zero — the default — means "each creature hits for what its own template
    #: says": one for the tutorial creature, five to nine for the undead mage. A
    #: number forces one blow on everybody; a negative number turns creature attacks
    #: off. Eight was being served for every creature alike.
    creature_damage: float = 0.0
    strike_interval: float = 0.0
    #: How far the player's own blow reaches, in world units.
    #:
    #: Looser than the client's own 1.75 on purpose, because this measures from a
    #: creature's movement record while the client measures from what it draws.
    #: Replaying a real session's twelve attacks against wire positions gives 0.9 to
    #: 3.5; six was too loose and made the player take damage across the room.
    reach: float = 3.5
    #: How much looser than its own stated range every skill reaches, for the same
    #: reason: wire coordinates against drawn ones. 1.75 puts angrystrike's 1.75 at
    #: exactly the 3.5 above, which is the figure tuned by hand and confirmed in play.
    reach_slack: float = 1.75
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
    #: Zero — the default — means "each creature has what its own template says":
    #: 24 for the tutorial creature, 50 for the undead mage champion. Twelve was
    #: served for all of them, and twelve was a misreading of a recorded blow.
    mob_max_health: float = 0.0
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

    #: Whether using an item applies what its template says it applies. A mount is
    #: the case that matters: the client sends the item's name and the live service
    #: answers with a summon effect and a ride effect, both of which are in the
    #: client's own tables.
    usable_items: bool = True
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


#: What a creature has when nothing else says. The tutorial dungeon's own creature,
#: from the client's monster table — and the value this server should have been
#: serving all along. The twelve it did serve came from decoding a recorded blow, and
#: the note beside that reading warned that misreading the field by a bit or two gives
#: 24. It does, and 24 is the answer.
DEFAULT_CREATURE_HEALTH = 24.0


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
    #: When this creature last struck anybody, on the monotonic clock. Its own, not
    #: the player's: the cooldown used to live on the player being hit, so ten players
    #: around one creature each had their own timer and the creature struck ten times
    #: as often.
    last_struck: float = 0.0
    #: Whether the client has been sent its description. Until it has, the client
    #: has no entity for the actor and nothing addressed to it can be drawn.
    described: bool = False
    #: Ticks the body is still announced for after death. Dropping a creature from
    #: the update the instant it dies leaves the client nothing to play the death
    #: sequence over.
    corpse_ticks: int = 0
    #: Whether the client has been told to delete this entity. A corpse that has had
    #: its time is discarded once, not once per tick.
    discarded: bool = False
    #: What its effects do to it, recomputed once a tick in advance_creatures rather
    #: than at each of the three places that need it. See dsor.afflictions.

    #: What is running on it: (effect wire, parameters, when it started, seconds,
    #: causer actor). One list, because a 0x004F carries an actor's whole set and
    #: sending a subset is a message that says "only these", wiping the rest.
    running: list = field(default_factory=list)
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
    #: This player's own actor id, four bytes as the wire wants it. Assigned on
    #: arrival from the world's actor space.
    actor: bytes = PLAYER_ACTOR
    #: What this player has claimed lately, and what the server owes them. See
    #: :mod:`dsor.trust` for why each limit is as loose as it is.
    claims: Claims = field(default_factory=Claims)
    #: When their last movement record was accepted, on the monotonic clock.
    seen_at: float = 0.0
    position: Position | None = None
    health: float = 0.0
    max_health: float = 0.0
    experience: int = 0
    level: int = 1
    #: The client's own game tick, read from its movement records. A skill's start
    #: tick is compared against it.
    tick: int = 0
    #: The resource a skill spends and a shout gives back — rage for a warrior. Was
    #: never reported at all: encode_actor_vitals existed and nothing called it, so
    #: the only health the client ever heard came from a hit and the resource never
    #: moved. warshout's ResourceGain of 0.6 is the whole of "furious battlecry gives
    #: rage".
    resource: float = 0.0
    #: Which way the player is facing, in 256ths of a turn clockwise from +y, read
    #: from the heading byte of their own movement records. An arc skill needs it:
    #: mightyswing cuts 170 degrees of *something*, and without a facing there is no
    #: arc to cut.
    heading: int = 0
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

    #: What is running on it: (effect wire, parameters, when it started, seconds,
    #: causer actor). One list, because a 0x004F carries an actor's whole set and
    #: sending a subset is a message that says "only these", wiping the rest.
    running: list = field(default_factory=list)
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
    #: Swings whose victims are chosen later, not now: a leap and a charge strike
    #: where they land, and where they land is not known until the player's own
    #: movement records say so. (due, address, skill wire, aim)
    pending_swings: list[tuple[float, Address, int | None, int | None]] = field(
        default_factory=list
    )
    #: Damage still to come from an effect that deals it over time — a poison, a burn,
    #: a bleed. (due, address, creature, damage per tick, ticks left, seconds between)
    #:
    #: Applied here rather than by the client. The client *can* do it —
    #: debuff_dot_poison's TickModifiers are CurrHealthPointsDmg every 1.5 seconds —
    #: but every effect that carries one also carries an animation, and an animated
    #: effect cannot be sent: it sends the client's sequencer into a FixedArray it
    #: cannot index, and there is nothing in any capture to check one against. Twenty
    #: thousand real 0x004F messages hold exactly four distinct effects and not one of
    #: them is animated.
    #:
    #: So the mechanic is served and the visualisation is not. A number floats off the
    #: creature every tick and its health drops, which is the part that matters.
    #: Every actor id in this world, handed out so that none collide. One space per
    #: world, which is also what makes a world shardable: two worlds can hand out the
    #: same ids because no client ever sees both.
    actors: ActorSpace = field(default_factory=ActorSpace)
    #: The creature movement records for the tick in progress, built once by
    #: advance_creatures and shared by every viewer. They do not depend on who is
    #: looking, which is what makes sharing them correct as well as cheap.
    placed: list[bytes] = field(default_factory=list)
    #: Which tick :attr:`placed` was built for, so a caller that asks for an update
    #: without having advanced the world first still gets one -- built once, however
    #: many viewers ask.
    placed_tick: int | None = None
    #: A creature's (hit frame, unblock frame, reach, cooldown) by blueprint. It
    #: depends on the blueprint and the rules and nothing else, and it was being
    #: recomputed once per creature per player per tick -- three dictionary lookups
    #: and a skill-table hit each time.
    _timings: dict[tuple, tuple[int, int, float, float]] = field(
        default_factory=dict
    )
    #: How many effect applications this world has handed out. See
    #: :meth:`_effect_instance`.
    _next_effect: int = 0
    #: (actor, effect wire) -> the instance handle naming that application. See
    #: _instance_of; copying the captured handle instead is what mixed effects up.
    _instances: dict[tuple[bytes, int], int] = field(default_factory=dict)
    #: Effect sets already reported as unservable, so the log says it once.
    _said: set = field(default_factory=set)
    #: The effect list last sent for each actor. A status effect command is an *event*,
    #: not a state to repeat: the live service sent 91 of them for 37 skills in one
    #: session, two or three per cast, where this server sent one per actor per tick.
    #:
    #: Ten a second is not merely wasteful, it is wrong -- the client tries to *add*
    #: each one and says so: "Failed to add actor effect
    #: (debuff_dot_poison_triggerExplosion). Effect already present!", once per tick,
    #: for every chained effect.
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
    _sent: dict = field(default_factory=dict)
    #: Which actors have been told their level, so it is sent once.
    _levelled: dict = field(default_factory=dict)
    _entered_at: dict = field(default_factory=dict)
    _handles: dict = field(default_factory=dict)
    _next_handle: int = 0
    #: The creatures' effect messages for this tick, built once.
    _creature_effects: list = field(default_factory=list)
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
            # Reserved rather than masked. The old form was
            # ``bytes([(first_actor + index) & 0xFF, ...])``, which wraps at 256 and
            # made a collision silent.
            actor = encode_actor(self.actors.reserve(0x00010000 | (first_actor + index)))
            record = bytearray(base)
            record[ACTOR_ID_OFFSET : ACTOR_ID_OFFSET + 4] = actor
            self.creatures[actor] = Creature(
                actor=actor,
                record=bytes(record),
                position=Position(0, 0, 0),
                # Its own health, from the client's monster table. Twenty-four for
                # the tutorial creature and fifty for the undead mage champion, not
                # one figure for all — and *not* the twelve this server used to
                # serve, which came from misreading a recorded blow by a bit or two.
                # The client knows each creature's health from that same table, so a
                # figure that disagrees is read as a change to it: that is the real
                # reason 60 was drawn as a floating +400.
                health=self.creature_health(blueprint, max_health),
                max_health=self.creature_health(blueprint, max_health),
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
        """Fill the world with the creatures *records* describes.

        A recorded record carries no blueprint, so there is no template to ask and
        *max_health* is all there is. Zero — which is what the rule now holds by
        default, meaning "ask the template" — would spawn them dead, so it falls back
        to the tutorial creature's own 24: these records are that creature.
        """
        health = max_health or DEFAULT_CREATURE_HEALTH
        for record in records:
            creature = Creature.from_record(record, health)
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
        """Count the corpses down, and delete the body when it has had its time.

        The counting down was already here and did nothing when it reached zero: the
        corpse left the position update and the entity stayed, so a killed creature
        lay on the screen for ever. Leaving the position update is not removal — the
        client keeps whatever it has been told about until something tells it
        otherwise.

        That something is DiscardMonsterCommand, whose body is empty: the actor id in
        the trailer is the whole message. Sending it in the same breath as the kill
        destroys the death sequence before it can play, which is why it is sent
        *here*, two seconds later, once the body has finished falling.

        Once per corpse. This method runs ten times a second, and three counter
        resets once landed in it by mistake — the damage that did is written up in
        reset_creatures. Anything stateful in here needs a guard, and ``discarded``
        is that guard.
        """
        for creature in self.creatures.values():
            if not creature.corpse_ticks:
                continue
            creature.corpse_ticks -= 1
            if creature.corpse_ticks or creature.discarded:
                continue
            creature.discarded = True
            for player in self.inhabitants():
                self._emit(
                    encode_discard_monster(
                        int.from_bytes(creature.actor, "little")
                    ),
                    player.address,
                )

    # ── players ──────────────────────────────────────────────────────────────

    def player(self, address: Address) -> Player:
        """The player at *address*, created on first sight."""
        player = self.players.get(address)
        if player is None:
            player = Player(address=address, actor=self._player_actor())
            self.players[address] = player
        return player

    def _player_actor(self) -> bytes:
        """An actor for a player arriving now.

        The first gets the one the recorded description names, so the single-player
        path is untouched. Everybody after gets a fresh one -- which is necessary but
        not yet sufficient for them to be visible: their description is still a replay
        of the recorded character and names the recorded actor, so serving a second
        player properly needs that rewritten too.
        """
        if RECORDED_PLAYER not in self.actors.taken:
            return encode_actor(self.actors.reserve(RECORDED_PLAYER))
        return self.actors.take_bytes()

    def forget(self, address: Address) -> None:
        """Remove a player entirely. One place, not nine.

        And when the last one goes, put the creatures back. Not respawning — the
        dead stay dead while anyone is here — but a world that carries one session's
        corpses into the next gets emptier every time somebody reconnects. That is
        what left a player standing on the spot where a creature had died an hour
        earlier, swinging at nothing, with the survivors too far away to notice.
        """
        leaving = self.players.pop(address, None)
        if leaving is not None:
            self.actors.give_back(int.from_bytes(leaving.actor, "little"))
        self.pending_hits = [h for h in self.pending_hits if h[1] != address]
        self.pending_swings = [s for s in self.pending_swings if s[1] != address]
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
            creature.last_struck = 0.0
            creature.discarded = False
            creature.described = False
        for lying in self.dropped:
            self.actors.give_back(int.from_bytes(lying, "little"))
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

    def wire_of_described(self, where: tuple[float, float, float]) -> Position:
        """The inverse of :meth:`described_position`, frame offset and all.

        Needed because a dropped item is remembered in the descriptions' frame — the
        drop message wants it that way — while every distance is measured on the wire.
        Multiplying by the scale and stopping there loses the constant offset, which
        is 31.18 units of z on the tutorial map: about 3,991 wire units, against a
        pickup bound of 1,536. That is how every pickup came to be refused as "too
        far away" while the item lay at the player's feet, and it is the same
        two-frames mistake :meth:`wire_position` was written to end.
        """
        # Reading it back through described_position derives the offset if it has not
        # been derived yet, so the two directions can never disagree.
        self.described_position(Position(0, 0, 0))
        dx, dy, dz = self.frame_offset or (0.0, 0.0, 0.0)
        return Position(
            int((where[0] - dx) * WORLD),
            int((where[1] - dy) * WORLD),
            int((where[2] - dz) * WORLD),
        )


    def _standing_room(self, centre: Position, count: int) -> list[Position]:
        """*count* places around *centre*, in rings, nearest ring first.

        One ring is not enough, and getting that wrong twice is instructive. With no
        ring at all, twenty creatures stopped mob_stop units from the player and stood
        on one point. With a single ring wide enough to space twenty of them properly,
        they stood 5.1 units out -- past their own AttackRange of 2 and hit range of
        2.25 -- so they surrounded the player and never struck. That is the same trap as
        "stopping at 3.5 put the creature outside its own reach".
        
        So the inner ring is at mob_stop, where a creature can actually reach, and holds
        as many as fit at mob_spacing apart -- eight of them at these numbers, which is
        about what fits round a person. The rest queue on further rings and wait for a
        gap, which is what a crowd does.
        """
        places: list[Position] = []
        ring = 0
        while len(places) < count:
            radius = self.rules.mob_stop + ring * self.rules.mob_spacing
            circumference = 2 * math.pi * radius
            fits = max(1, int(circumference / self.rules.mob_spacing))
            wanted = min(fits, count - len(places))
            places.extend(
                ring_positions(
                    centre, wanted, max(1, round(radius * WORLD_SCALE))
                )
            )
            ring += 1
        return places[:count]

    def advance_creatures(self, tick: int) -> None:
        """Move every creature one step, and remember how to say so.

        Once a tick for the whole world, not once per viewer. A creature chases the
        nearest player in the world rather than "the player", because with two people
        in a map there is no such thing -- and the records it produces are the same
        for everybody, so they are built here and shared.

        Everything about *how* it moves is unchanged and was hard-won: a record
        stamped with speed zero gives the client no velocity to extrapolate, so it
        snaps and plays the idle animation; a step of 60 wire units per update is four
        times a walk. The speed byte, the heading and WALK_UNITS_PER_TICK are what make
        it a walk, and NetworkSmoothMotionProperty animates it for free.
        """
        self.placed_tick = tick
        announced = self._ready().announced()
        if not announced or not self.rules.mob_chase:
            self.placed = []
            return

        standing = [
            player.position
            for player in self.players.values()
            if player.in_world and player.position is not None
        ]
        # Where each creature is heading, which is *not* the player's own feet.
        #
        # Every creature that arrived stopped mob_stop units from the player, and
        # nothing kept them apart from each other, so twenty of them stood on one
        # point -- "tous les mobs spawn au meme endroit". They spawn twenty units
        # apart; they converge. Each gets its own place on a ring instead, evenly
        # spaced, its slot fixed by its position in the creature order so it does not
        # swap places from tick to tick.
        #
        # The same problem was already solved for items on the ground and not for
        # creatures: clear_of_other_drops exists because two items sharing a place
        # stack and a stack crashes the client.
        chasing: dict[int, list[Creature]] = {}
        for creature in announced:
            if not standing:
                continue
            nearest = min(
                range(len(standing)),
                key=lambda i: creature.position.distance_squared_to(standing[i]),
            )
            chasing.setdefault(nearest, []).append(creature)
        slot: dict[bytes, Position] = {}
        for index, group in chasing.items():
            for creature, place in zip(group, self._standing_room(standing[index], len(group))):
                slot[creature.actor] = place

        elapsed = max(1, tick - self.stepped_tick)
        self.stepped_tick = tick
        reach = self.rules.mob_stop * WORLD_SCALE
        aggro = self.rules.mob_aggro * WORLD_SCALE

        placed = []
        for creature in announced:
            template, here = creature.record, creature.position
            if not standing:
                placed.append(with_motion(template, here, tick, speed=0))
                continue
            # The nearest player in the world. With one player this is the player, so
            # nothing about the single-player behaviour changes.
            position = min(standing, key=here.distance_squared_to)
            # Faced toward the player, but walking to its own place on the ring around
            # them. Facing the ring slot instead would have creatures looking past the
            # player once they arrived.
            heading = heading_to(position.x - here.x, position.y - here.y)
            target = slot.get(creature.actor, position)
            span = here.distance_to(target)
            if here.distance_to(position) > aggro:
                # Out of range: leave it exactly as it stands, facing as it was. A
                # heading here would turn every creature on the map toward the player
                # from across the zone.
                placed.append(with_motion(template, here, tick, speed=0))
                continue
            pace = self.rules.mob_speed
            if span <= pace * elapsed:
                # Arrived: stand and face the player. Clamping the step to the
                # distance remaining left the creature always a fraction outside,
                # announcing a walk it never finished — "elles s'arretent loin et
                # continuent de vouloir venir". Standing creatures in the capture
                # carry speed zero and duration zero, so anything else here claims a
                # movement the client extrapolates into the player.
                placed.append(
                    with_motion(template, here, tick, speed=0, heading=heading)
                )
                continue
            step = min(pace * elapsed, span)
            moved = Position(
                x=here.x + round((target.x - here.x) * step / span),
                elevation=position.elevation,
                y=here.y + round((target.y - here.y) * step / span),
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
        self.placed = placed

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
            # Serialised, not moved. Moving happens once a tick in advance_creatures;
            # this used to do it here, which meant a creature was moved once per
            # viewer and each time toward a different player, and stepped_tick
            # advanced on the first viewer so everybody after saw an elapsed of zero.
            # One creature has one position.
            if self.placed_tick != tick:
                self.advance_creatures(tick)
            return encode_entity_group_message([player, *self.placed], trailing)

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


    def victims_of(
        self, sender: Address, used: Skill | None, aim: int | None = None
    ) -> list[bytes]:
        """Which creatures *used* strikes, cast by the player at *sender*.

        A skill is not one shape, and serving every skill as "the one creature in
        front" is what made the skills unlocked by levelling do nothing at all. The
        client's own table says how each one picks its victims:

          Actor         the single named target. ``angrystrike``.
          Angle, Cone   an arc in front of the caster, ``arc`` degrees wide.
                        ``mightyswing`` cuts 170 degrees at 3.5 units.
          Radius        everything around, out to ``hit_range``. ``mighty360``
                        reaches 6.3.
          User          the caster alone. A buff: it damages nobody.

        The range is the skill's own ``hit_range`` plus ``reach_slack``, and the slack
        is not decoration. Filtering at the client's exact figure stopped damage
        entirely — eleven attacks refused in one session — because the distance here
        is measured against the coordinates in a creature's description while the
        client measures against what it draws, and replaying a real session's twelve
        attacks gives 0.9 to 3.5 against a stated 1.75. A slack of 1.75 puts
        ``angrystrike`` at exactly the 3.5 that was tuned by hand and confirmed in
        play, and scales the others by their own numbers rather than flattening them.
        """
        position = self.player(sender).position
        if position is None:
            return []
        alive = [creature.actor for creature in self._ready().engageable()]
        if not alive:
            return []

        def distance(actor: bytes) -> float:
            where = self.wire_position(actor)
            return float("inf") if where is None else position.distance_to(where)

        reach = (
            used.hit_range + self.rules.reach_slack if used else self.rules.reach
        )
        within = [a for a in alive if distance(a) <= reach * WORLD]

        if used is None or not used.area:
            # One victim, and the same one as last time. The client names no target —
            # measured: across a session with six kills, not one message from it
            # carries a creature's id except the question "what is this entity" — so
            # the server chooses, and choosing the nearest afresh on every blow makes
            # the choice flip between creatures standing close together. That is what
            # looked like damage being shared between them.
            latched = self.player(sender).target
            # Still latched only while it is still in reach. Holding it whatever the
            # distance is what "ca reste focus sur l'ancien meme s'il est loin" was:
            # the first creature struck stayed the target for the rest of the session,
            # so a swing at something standing in front kept landing on whatever had
            # walked off behind. The latch is there to stop the choice flipping between
            # two creatures standing together, and that reason expires the moment the
            # one held is out of range.
            if latched is not None and latched in within:
                return [latched]
            if not within:
                self.player(sender).target = None
                return []
            target = min(within, key=distance)
            self.player(sender).target = target
            return [target]

        # An area skill. No latching: it hits what it covers, and it may cover
        # nothing.
        if used.arc >= 360.0:
            return sorted(within, key=distance)

        # An arc, in 256ths of a turn rather than degrees, because that is the unit
        # the wire uses for a heading and comparing in it avoids a conversion in the
        # middle of a wrap.
        half = used.arc / 360.0 * HEADING_UNITS / 2.0
        # Where the command says the player is aiming, and only failing that the
        # last facing a movement record reported. The command is the better source:
        # across 277 real skill commands the two agree to within eight units 77% of
        # the time, and the disagreements are exactly the turns — the player pointed
        # somewhere and swung before the next movement record went out.
        facing = self.player(sender).heading if aim is None else aim
        struck = []
        for actor in within:
            where = self.wire_position(actor)
            if where is None:
                continue
            bearing = heading_to(where.x - position.x, where.y - position.y)
            offset = (bearing - facing + HEADING_UNITS // 2) % HEADING_UNITS - (
                HEADING_UNITS // 2
            )
            if abs(offset) <= half:
                struck.append(actor)
        return sorted(struck, key=distance)

    def resolve_attack(
        self,
        sender: Address,
        wire: int | None = None,
        aim: int | None = None,
        travelled: bool = False,
    ) -> None:
        """Work out what the player just hit with skill *wire*, and take health off it.

        Neither skill command names a victim: ``TargetSkillCommand`` carries a skill
        id, a float, a tick and two fields whose meaning is not established, and not
        one actor id among them. Deciding who was hit is therefore the server's job,
        which is why hitting a creature forever did nothing — nobody was deciding.

        What the skill *does* say, in the client's own table, is how many victims and
        how far and how hard. That is :meth:`victims_of`. The health scale is still
        ours — the capture never reports a creature's stats at all — but the shape of
        the blow is the client's.
        """
        used = skill_at(wire)
        if used is not None and not travelled:
            self.grant_effects(sender, used)
            self.place_ground_effects(sender, used)
            if self.spend_resource(sender, used):
                self.report_vitals(sender)
        if used is not None and not travelled and used.hit_frame > 0:
            # Not yet. A blow lands on the skill's own HitFrame, which is 5 ticks for
            # angrystrike, 7 for mighty360, 9 for mightybash and 11 for enragingleap —
            # 200 to 440 ms after the swing begins. Resolving on arrival of the
            # command put the damage at the start of the animation instead of at the
            # moment the weapon connects, so the number floated up before the swing
            # had visibly happened.
            #
            # This is the same correction already made for creatures, where sending
            # the hit in the same breath as the swing cut the animation to a
            # hundredth of a second. It was never applied to the player's own blow.
            #
            # For a leap or a charge it does a second job: those strike at the far end
            # of the travel, and waiting means the player's own movement records have
            # arrived and say where they came down.
            self.pending_swings.append(
                (
                    time.monotonic() + used.hit_frame * GAME_TICK_MS / 1000.0,
                    sender,
                    wire,
                    aim,
                )
            )
            log.debug(
                "%s: %s began %s, landing in %d ticks%s",
                self.name,
                sender,
                used.id,
                used.hit_frame,
                " where it lands" if used.lands_where_it_ends else "",
            )
            return
        if used is not None and used.harmless:
            # Not a blow. Either the skill is cast on the caster — warshout,
            # frenzyshout, defiance, spikedShield — or its damage modifier is zero
            # because the whole effect lives in status effects: battlecry debuffs
            # movement speed, resistance and attack speed for five seconds, and
            # earthquake lays an aura on the ground that does its damage over eight.
            #
            log.info(
                "%s: %s used %s (%s, x%.2f)",
                self.name,
                sender,
                f"{title_of(used.id)} ({used.id})",
                used.targeting,
                used.damage_modifier,
            )
            return

        targets = self.victims_of(sender, used, aim)
        if not targets:
            log.info(
                "%s: %s attacked with %s and nothing was within %.2f units",
                self.name,
                sender,
                used.id if used else "an unknown skill",
                used.hit_range + self.rules.reach_slack if used else self.rules.reach,
            )
            return

        # What the character hits for, from the client's own class curve rather than
        # a number invented here: fifteen at level one, not four. Equipment adds to
        # it and this does not model that — an item's damage is rolled per instance
        # and scaled to a level, and is not in its template at all.
        #
        # Times the skill's own multiplier: 1.25 for angrystrike, 1.5 for mighty360,
        # 1.0 for mightyswing, which is paid for by its hitting several creatures.
        base = self.rules.mob_damage or damage_at(self.player(sender).level)
        blow = base * (used.damage_modifier if used else 1.0)

        player = int.from_bytes(self.player(sender).actor, "little")
        killed = []
        dealt = 0.0
        for target in targets:
            struck = self.creatures[target]
            # An armour break is worth what it changes. Each creature's own condition,
            # so two standing side by side take different damage from one swing --
            # which is the whole point of having broken one's armour.
            hit_for = blow
            left = max(0.0, struck.health - hit_for)
            struck.health = left

            # The blow, generated. A creature has no health message of its own —
            # every stats update in both captured sessions targets the player — so
            # the client takes the victim's health from this message and nowhere else.
            #
            # Which is precisely why replaying one could never work: a recorded blow
            # is a *killing* blow, so it carries zero, and the creature died on the
            # spot by the unanimated path. The client said so: "Victim ... is not
            # alive or cannot receive", then "received kill message twice" when the
            # real death arrived after. Generating it is what lets a creature survive
            # a hit at all.
            self._emit(encode_hit(
                    Hit(
                        victim=int.from_bytes(target, "little"),
                        attacker=player,
                        damage=int(hit_for),
                        victim_health=int(left),
                        victim_max_health=int(struck.max_health),
                        # The attacker, which for the player's own blow is the
                        # player. Seventy-four real hits carry the attacker here,
                        # never the victim, and an earlier note claiming otherwise is
                        # refuted.
                        combat_value_owner=player,
                        # Zero, as the real blow carries. The floating number comes
                        # from the damage field; putting it here as well draws a
                        # second one.
                        combat_value=0.0,
                        tick=self.player(sender).server_tick,
                    )
                ),
                sender,
            )

            self.inflict_effects(target, used, self.player(sender).actor)
            dealt += hit_for
            log.info(
                "%s: %s hit entity %s with %s for %.0f, %.1f left",
                self.name,
                sender,
                target.hex(" "),
                used.id if used else "an unknown skill",
                hit_for,
                left,
            )
            if left <= 0.0:
                killed.append(target)

        # And what a life leech gives back, on the total rather than per victim.

        # Deaths after every blow, so an area skill reports all its damage before the
        # first corpse rearranges the creature list.
        for target in killed:
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
                    killer=int.from_bytes(self.player(sender).actor, "little"),
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
                # From the world's space, not a counter masked to one byte. Items
                # are the ones that grow without bound, and two of them once claimed
                # the same actor -- which showed as a picked-up item arriving as the
                # wrong thing.
                lying = self.actors.take_bytes()
                where = self.clear_of_other_drops(described)
                self.dropped[lying] = where
                # What the 0x002D actually names, not what the rule asked for. With
                # no blueprint configured the drop keeps the recording's own -- a
                # mace -- and the pickup reply used to keep *its* recording's, a
                # sword. The two recordings are of different items, so the ground
                # showed one thing and the bag another, which is what the operator
                # reported as "j'ai pas le bon item".
                self.templates[lying] = blueprint or drop_template(item_drop())
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
                    int.from_bytes(self.player(sender).actor, "little"),
                    level=level,
                    levelled=levelled,
                ),
                sender,
            )
            if levelled:
                earner.level = level
                if earner.address is not None:
                    self.announce_level(earner.address)
                self._emit(
                    encode_player_level(
                        level, int.from_bytes(self.player(sender).actor, "little")
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


    def spend_resource(self, sender: Address, used: Skill | None) -> bool:
        """Move the player's resource for *used*, and say whether it changed.

        Both directions come from the skill's own template, as fractions of the pool:
        angrystrike gives 0.05 back, mighty360 costs 0.4, and warshout gives 0.6 —
        which is the rage "furious battlecry" is supposed to grant.

        A cost that cannot be paid is not enforced. The client has already played the
        animation by the time this runs, and refusing a blow the player has seen land
        is worse than letting the pool go to nothing.
        """
        if used is None:
            return False
        pool = self.resource_pool(sender)
        # Dragon Hide's ResourceCost:-1.0,relative,player_skills makes it zero.
        cost = used.resource_cost
        change = (used.resource_gain - cost) * pool
        if not change:
            return False
        player = self.player(sender)
        before = player.resource
        player.resource = max(0.0, min(pool, before + change))
        return player.resource != before

    def accept_clock(self, sender: Address, tick: int | None) -> None:
        """Adopt the client's own game tick as this player's clock, as it stands.

        The client's status effect handler compares an element against its *own* clock,
        so that is the clock to write. Its movement commands carry it.

        **Not forward-only.** That was tried and it is wrong: the client's clock starts
        again at nearly zero on a new session, and a rule that refuses to go back kept
        the previous session's value -- 35,278 while the client was at 10,000, which
        schedules every effect a thousand seconds into the future and shows nothing at
        all. Taking the tick as it comes cannot drift, because it is not this server's
        clock in the first place.

        The reason for the forward-only rule was a measurement error worth naming: one
        capture held two sessions, and comparing the first session's low tick with the
        second's high one made this server look 5,561 ticks behind. Element by element
        against the client's clock at the same moment, it was never more than about
        thirty out.
        """
        if tick is None or tick <= 0 or tick > 0x7FFFFFFF:
            return
        self.player(sender).server_tick = tick

    def accept_movement(self, sender: Address, position: Position) -> bool:
        """Take the client's word for where it is, or refuse this record.

        Refusing means keeping the last position the server did believe, which is a
        snap-back on the server side only -- nothing is sent to correct the client,
        because a correction on a lost datagram would fight the network rather than a
        cheat.
        """
        player = self.player(sender)
        now = time.monotonic()
        before, seen = player.position, player.seen_at
        player.seen_at = now
        if before is None or not self.rules.enforce:
            player.position = position
            return True
        travelled = before.distance_to(position)
        allowance = player.claims.spend_allowance(now)
        if movement_is_plausible(travelled, now - seen, allowance):
            player.position = position
            return True

        player.claims.offences += 1
        # Counted, and then **taken anyway** unless something is set that says
        # otherwise. Refusing was worse than not checking at all.
        #
        # The position this server keeps is the one it echoes back in the next tick's
        # entity update, so keeping a stale one drags the client to it. And once one
        # record is refused the anchor is stale, so the next honest record measures as
        # a huge jump and is refused too: 670 refusals in one session, the claimed
        # distance shrinking each time as the client was pulled back. A rollback loop,
        # caused entirely by the correction.
        #
        # There is no honest correction available here. Correcting a player needs a
        # model of where they *could* be, and this server has no collision data and no
        # navigation mesh -- it does not know a wall from a corridor. So the offence
        # count is the evidence and the client keeps its position.
        if player.claims.offences in (1, 10, 100) or player.claims.offences % 1000 == 0:
            log.warning(
                "%s: %s claimed %.0f wire units in %.0f ms (offence %d)",
                self.name,
                sender,
                travelled,
                (now - seen) * 1000.0,
                player.claims.offences,
            )
        if self.rules.refuse_movement:
            return False
        player.position = position
        return True

    def player_skills(self, sender: Address) -> set[int]:
        """Every skill this player actually has, by wire index.

        Built from the same two rules the skill book's ownership bits are set from, so
        what the server grants and what it accepts agree by construction. Getting
        those out of step would refuse every skill in the game.
        """
        from dsor.skillbook import skill_index, up_to_level

        wanted: set[int] = set()
        for name in self.rules.granted_skills:
            found = skill_index(name)
            if found is not None:
                wanted.add(found)
        if self.rules.grant_up_to_level:
            wanted |= up_to_level(
                self.rules.grant_up_to_level, self.rules.character_class
            )
        return wanted

    def may_use(self, sender: Address, wire: int | None) -> str | None:
        """Why this player may not use skill *wire*, or None if they may.

        Three things the server used to take on trust: that the character has the
        skill, that it is off cooldown, and that the resource is there. A client could
        send earthquake at level one, every tick, for free.
        """
        if not self.rules.enforce or wire is None:
            return None
        used = skill_at(wire)
        if used is None:
            return f"there is no skill {wire}"
        owned = self.player_skills(sender)
        if owned and wire not in owned:
            return f"{used.id} is not unlocked"
        player = self.player(sender)
        now = time.monotonic()
        if not off_cooldown(player.claims.used_at.get(wire), used.cool_down, now):
            return f"{used.id} is on cooldown for another {used.cool_down:.1f}s"
        cost = used.resource_cost * self.resource_pool(sender)
        if cost > player.resource + 1e-6:
            return f"{used.id} costs {cost:.0f} and only {player.resource:.0f} is left"
        return None

    def note_use(self, sender: Address, wire: int | None, used: Skill | None) -> None:
        """Remember that a skill was used, for the cooldown and for a leap's budget."""
        if wire is None:
            return
        player = self.player(sender)
        now = time.monotonic()
        player.claims.used_at[wire] = now
        if used is not None and used.lands_where_it_ends:
            # A leap or a charge genuinely moves the player, and by more than a speed
            # check would ever allow. Using one buys exactly its own range.
            player.claims.travel(used.attack_range, now)

    def resource_pool(self, sender: Address) -> float:
        """How much rage this player can hold, from the client's own level table.

        A hundred, at every level: BaseMana in _Template_XPLevels. A skill's
        ResourceCost and ResourceGain are fractions of it, so warshout's 0.6 is sixty
        rage. Ten was written here once, and six points on a bar of a hundred looks
        exactly like nothing happening.
        """
        return self.rules.player_resource or resource_at(self.player(sender).level)

    def report_vitals(self, sender: Address) -> None:
        """Tell the client the player's health and resource.

        Nothing did this. ``encode_actor_vitals`` was written, tested and never
        called, so the only health the client ever heard was whatever a hit carried
        and the resource stayed wherever it started.
        """
        player = self.player(sender)
        self._emit(
            encode_actor_vitals(int(player.health), player.resource, player.actor),
            sender,
        )
        log.info(
            "%s: %s has %.0f health and %.1f of %.0f rage",
            self.name,
            sender,
            player.health,
            player.resource,
            self.resource_pool(sender),
        )

    def player_health(self, level: int) -> float:
        """What a character of *level* has, unless a rule forces a figure.

        From the client's own class curve, the same table the damage comes from.
        """
        return float(self.rules.player_max or hit_points_at(level))

    def toughen(self, health: float, count: int = 1) -> list[bytes]:
        """Give *count* creatures *health*, and return which ones got it.

        For watching a skill work. Every creature in the tutorial dungeon holds 24
        points and a level 100 warrior hits for 16800, so nothing survives long enough
        to see an animation play, let alone an area skill hit three things.

        Champions first, because they are the ones worth staring at, then whatever
        else is standing. The rest of the world keeps its own numbers -- forcing a
        figure on every creature through ``mob_max_health`` would, and this does not.

        A caution that belongs with it: the client knows each creature's health from
        its own monster table, so a figure that disagrees may be drawn oddly. Serving
        60 where the table said 24 was once read as a heal. The blow now carries this
        creature's max as well as its current health, which the earlier attempt did
        not, so it has a better chance -- but it is still a number the client can
        contradict, and that is the trade for being able to see anything at all.
        """
        def rank(creature: "Creature") -> tuple[int, bytes]:
            champion = "champion" in (creature.blueprint or "")
            return (0 if champion else 1, creature.actor)

        chosen = sorted(
            (c for c in self._ready().creatures.values() if c.alive),
            key=rank,
        )[:count]
        for creature in chosen:
            creature.health = health
            creature.max_health = health
            log.info(
                "%s: entity %s (%s) now holds %.0f health",
                self.name,
                creature.actor.hex(" "),
                creature.blueprint or "recorded",
                health,
            )
        return [creature.actor for creature in chosen]

    def creature_health(self, blueprint: str | None, fallback: float) -> float:
        """The health *blueprint* starts with, from the client's monster table.

        A rule set explicitly overrides it, so the debug console can still make a
        creature take twenty blows. Otherwise the template wins, and *fallback* only
        applies to a blueprint the table does not carry —
        ``a0001_normal_anderworld_minion_01`` is one such.
        """
        if self.rules.mob_max_health:
            return self.rules.mob_max_health
        known = monster(blueprint)
        if known is not None:
            return float(known.hit_points)
        return fallback or DEFAULT_CREATURE_HEALTH

    def creature_blow(self, actor: bytes) -> float:
        """What the creature at *actor* takes off a player, from its own template.

        One to one for the tutorial dungeon's creature, two to three for the champion
        that opens the exit, five to nine for the undead mage. This server served
        eight for every creature alike, which is what "les monstres ne tapent pas
        comme sur le vrai serveur" was: eight times too hard for the creature the
        dungeon is full of, and not hard enough for its champion.

        Midpoint rather than a roll, because nothing else here is random and a
        reproducible blow is worth more than a realistic one while this is being
        debugged.
        """
        if self.rules.creature_damage:
            return self.rules.creature_damage
        creature = self.creature(actor)
        known = monster(creature.blueprint) if creature else None
        if known is None:
            return 3.0
        return (known.min_damage + known.max_damage) / 2.0

    def creature_skill(self, actor: bytes) -> Skill | None:
        """The skill the creature at *actor* strikes with, from the client's table.

        The tutorial dungeon's creature has ``AnderworldCreatureStrike``, and that
        row is where the three numbers this server used to carry as constants
        actually come from: HitFrame 12, SkillUnblockFrame 27, AttackRange 2 and
        CoolDown 2.75. They were right — for that one creature. Its ranged sibling
        ``AnderworldCreatureShot`` reaches 16 units, waits 3 seconds and lands its
        blow on frame 0, and none of those constants describes it at all.
        """
        creature = self.creature(actor)
        if creature is None or creature.attack_skill is None:
            return None
        return skill_at(creature.attack_skill)

    def creature_timing(self, actor: bytes) -> tuple[int, int, float, float]:
        """(hit frame, unblock frame, hit range, cooldown) for the creature at *actor*.

        Each creature's own, unless a rule forces one on everybody. The fallbacks are
        AnderworldCreatureStrike's, which is what this server assumed for every
        creature before it read the table.
        """
        creature = self.creature(actor)
        # The rules are part of the key, not just the blueprint. They are settable at
        # runtime from the debug console, and a cache keyed on the blueprint alone
        # went on answering with the old numbers after a "set creature_hit_frame 40" —
        # which is exactly what the test for that override caught.
        key = (
            creature.blueprint if creature else None,
            self.rules.creature_hit_frame,
            self.rules.creature_unblock_frame,
            self.rules.creature_hit_range,
            self.rules.strike_interval,
        )
        cached = self._timings.get(key)
        if cached is not None:
            return cached
        used = self.creature_skill(actor)
        found = (
            self.rules.creature_hit_frame or (used.hit_frame if used else 12),
            self.rules.creature_unblock_frame or (used.unblock_frame if used else 27),
            self.rules.creature_hit_range or (used.hit_range if used else 2.25),
            self.rules.strike_interval or (used.cool_down if used else 2.75),
        )
        self._timings[key] = found
        return found

    def creatures_strike(self) -> None:
        """Let every creature that can reach somebody hit them, once a tick.

        One pass over the creatures rather than one pass per player. It used to be the
        other way round -- ``for each player, which creatures are near me`` -- and that
        was 79% of the whole tick at two thousand players, because it recomputed each
        creature's reach, skill and timing once per viewer.

        Inverting it also puts the cooldown check first, which is the cheap one: a
        creature that swung recently is skipped before any distance is measured, and
        most of them have. What survives is O(creatures off cooldown x players), and
        the reach a creature has is looked up once per creature instead of once per
        pair.
        """
        if self.rules.creature_damage < 0.0:
            # Negative turns creature attacks off entirely. Zero used to mean that,
            # and now means "each creature hits for what its own template says".
            return
        standing = [
            player
            for player in self.players.values()
            if player.in_world and player.alive and player.position is not None
        ]
        if not standing:
            return
        now = time.monotonic()
        for creature in self._ready().engageable():
            if creature.blueprint and not creature.attack_skill:
                # No attack in its own blueprint, so it never strikes. The movement
                # target's only skill is monster_selfkill and it stands eight units
                # from where a player arrives: it walked over and hit, invisibly.
                continue
            hit_frame, unblock_frame, reach, cooldown = self.creature_timing(
                creature.actor
            )
            # The cheap test first, and the one that skips the most.
            if now - creature.last_struck < cooldown:
                continue
            where = self.wire_position(creature.actor)
            if where is None:
                continue
            limit = (reach * WORLD) ** 2
            victim = min(standing, key=lambda p: p.position.distance_squared_to(where))
            if victim.position.distance_squared_to(where) > limit:
                # Nothing alive within reach. Losing health with no creature beside
                # you was this condition being "any creature anywhere", which every
                # described creature satisfied for the whole session.
                continue
            creature.last_struck = now
            victim.last_struck = now
            self._creature_blow(victim.address, creature.actor, hit_frame,
                                unblock_frame)

    def creature_swing(self, sender: Address) -> None:
        """The old per-player entry point, kept for callers and tests that use it."""
        self.creatures_strike()

    def _creature_blow(
        self, sender: Address, attacker: bytes, hit_frame: int, unblock_frame: int
    ) -> None:
        """One creature's swing at one player.

        Two messages, both replayed from a session where creatures fought back: the
        blow, and the player's health afterwards. The blow is one of the sixteen
        163-byte hits that name no creature -- the mark of a blow taken rather than
        landed.

        This direction works where the other cannot: the player's actor is bound to an
        entity, so its health updates take effect. A creature's never do, which is why
        no creature's health is reported at all.
        """
        striker = self.player(sender)
        position = striker.position
        if position is None:
            return

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
                target=int.from_bytes(self.player(sender).actor, "little"),
                skill_id=(
                    (self.creature(attacker).attack_skill if self.creature(attacker) else None)
                    or self.rules.creature_skill
                ),
                heading=heading,
                start_tick=now + self.rules.skill_lead,
                hit_frame=hit_frame,
                unblock_frame=unblock_frame,
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
                time.monotonic() + hit_frame * GAME_TICK_MS / 1000.0,
                sender,
                attacker,
            )
        )


    def land_swings(self) -> None:
        """Resolve the swings whose impact frame has now arrived.

        Every skill waits its own HitFrame, the way a creature's blow already did.
        Two things depend on it. The number the player sees should appear when the
        weapon connects, not when the swing starts — 5 ticks for angrystrike, 11 for
        enragingleap. And a leap or a charge strikes at the far end of the travel, so
        waiting also means the player's own movement records have arrived and say
        where they came down, without decoding a destination field whose layout is not
        established.
        """
        now = time.monotonic()
        due = [entry for entry in self.pending_swings if entry[0] <= now]
        if not due:
            return
        self.pending_swings = [e for e in self.pending_swings if e[0] > now]
        for _, sender, wire, aim in due:
            if not self.player(sender).in_world:
                continue
            self.resolve_attack(sender, wire, aim, travelled=True)

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
            blow = self.creature_blow(attacker)
            used = self.creature_skill(attacker)
            if used is not None:
                blow *= used.damage_modifier
            # And what is on the creature. mightyswing's debuff is Damage:-0.05,
            # relative, Min and Max -- five percent off everything it hits for, which
            # is small and is the real value the wire carries.
            left = max(0.0, victim.health - blow)
            victim.health = left
            # One message, not two. This used to replay a recorded blow *and* send a
            # separate vitals update, so the number the player saw came from another
            # session's fight while the health actually applied came from here — a "-1"
            # floating up while the bar dropped by fifteen, and a "+400" whenever the
            # vitals message happened to raise the bar rather than lower it. A hit
            # carries the victim's health, so it is the only thing that needs sending.
            self._emit(encode_hit(
                    Hit(
                        victim=int.from_bytes(victim.actor, "little"),
                        attacker=int.from_bytes(attacker, "little"),
                        damage=int(blow),
                        victim_health=int(left),
                        victim_max_health=int(victim.max_health),
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
                            victim=int.from_bytes(victim.actor, "little"),
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

    # ---------------------------------------------------------------- effects

    def _source_handle(self) -> int:
        """A handle for one grant: every effect from a single cast shares it.

        Read off the live service's own Dragon Hide message. Its three elements carry
        field 7 as 65544, 65544 and 65557: armour and resistance, which come from the
        skill, share a value, and the life leech, which comes from the talent, has its
        own. So field 7 is not the actor -- it names *what granted the effect*, and two
        effects from one cast name the same thing.

        This server had the actor there, which is the one field that differed from the
        official message once everything else matched.
        """
        self._next_handle += 1
        return FIRST_HANDLE + self._next_handle

    def grant_effects(self, sender: Address, used: "Skill | None") -> None:
        """Put on the caster whatever the skill's UserStatusEffects column names.

        The C:1.0 entries, which is a measurement and not a policy: 1,581 real casts
        were pulled out of the captures and the effects the real server added were read
        off the wire, and for frenzyshout the C:1.0 set is exactly the three effects it
        was measured applying. The C:0.0 entries fired only on the recorded character,
        who had the talents that unlock them.
        """
        if used is None:
            return
        player = self.player(sender)
        source = self._source_handle()
        for entry in effects.granted_by(used.wire):
            self._start(player, entry, causer=source)
        if self.rules.probe_effect:
            # The diagnostic. See Rules.probe_effect.
            found = effects.by_id(self.rules.probe_effect)
            if found is None:
                log.warning(
                    "%s: no effect named %r to probe with",
                    self.name,
                    self.rules.probe_effect,
                )
            else:
                self._start(
                    player,
                    effects.Entry(found.id, 1.0, 5.0, None, (0.0,) * 5),
                    causer=source,
                )

    def use_item(self, sender: Address, template: str) -> list[tuple[Address, bytes]]:
        """The player used an item by name, which is how a mount is summoned.

        Nothing is guessed from the name: ``_Template_Item.StatusEffectId`` says which
        effect the item applies, and for a mount the ride is that name with ``cast``
        swapped for ``ride``. See :mod:`dsor.usable` for the capture this comes from.
        """
        if not self.rules.usable_items:
            log.info("%s: %s used %s, and items are off", self.name, sender, template)
            return []
        entries = usable.entries_for(template)
        if not entries:
            log.info(
                "%s: %s used %s, which applies nothing this server can find",
                self.name, sender, template,
            )
            return []
        player = self.player(sender)
        source = self._source_handle()
        for entry in entries:
            self._start(player, entry, causer=source)
        log.info(
            "%s: %s used %s -> %s",
            self.name, sender, template, ", ".join(e.effect for e in entries),
        )
        return self._drain()

    def place_ground_effects(self, sender: Address, used: "Skill | None") -> None:
        """Draw whatever the skill puts on the ground.

        Nine skills of the four playable classes place *only* ground effects and so did
        nothing at all without this: the warrior's Fury of the Dragon and Banner of War,
        the mage's arcanevortex, and five of the dwarf's. Fourteen use one at all.

        The command carries no position -- neither the recording caster's wire
        coordinates nor their described form appears anywhere in the payload, in any byte
        order or bit alignment, and the actor is 0 -- so the client places these from the
        skill use it sent itself. Which is why this has no position to get wrong.

        The entries come from the same ``LocationStatusEffects`` column the mechanics
        read, so the two cannot disagree about what a skill places.
        """
        if used is None or not self.rules.location_effects:
            return
        message = location.for_skill(used.wire)
        if message is None:
            return
        self._emit(message, sender)
        log.info(
            "%s: %s draws %d ground effect(s) for %s",
            self.name,
            sender,
            len(location.placed_by(used.wire)),
            used.id,
        )

    def inflict_effects(self, target: bytes, used: "Skill | None", causer: bytes) -> None:
        """Put on the victim whatever VictimStatusEffects names."""
        if used is None:
            return
        creature = self.creature(target)
        if creature is None:
            return
        source = self._source_handle()
        for entry in effects.inflicted_by(used.wire):
            self._start(creature, entry, causer=source)

    def _start(self, holder, entry, causer: int) -> None:
        """Begin one effect on *holder*, replacing it if it is already running."""
        found = effects.by_id(entry.effect)
        if found is None:
            return
        if found.talent_gated and found.id != self.rules.probe_effect:
            # A talent's variant of the effect, and this character has no talents. Its
            # column entry says C:1.0, so the chance field does not gate it -- see
            # dsor.effects.TALENT_MARK for why the displayed name does.
            return
        seconds = effects.seconds_of(entry)
        if seconds <= 0.0:
            # Nothing to run and nothing to expire. warshout's ctfdropflag used to be
            # the example here, and it was wrong: its duration is in the column's DP
            # field, which the parser dropped, and the live service applies it for ten
            # seconds. See dsor.effects.seconds_of.
            return
        holder.running = [r for r in holder.running if r[0] != found.wire]
        holder.running.append(
            (found.wire, entry.substitutions, time.monotonic(), seconds, causer)
        )
        log.info(
            "%s: %s on %s for %.1fs%s",
            self.name,
            entry.effect,
            holder.actor.hex(),
            seconds,
            " (vfx %s)" % found.sequence if found.sequence else "",
        )

    def _handle(self, actor: bytes, wire: int) -> int:
        """A stable handle for one effect instance on one actor.

        The client keys "add this effect" against "extend the one already running" on
        this number, so two different effects on one actor must not share it and the
        same effect must keep it while it runs. Handed out from a counter rather than
        derived, because a derivation that collides is a silent bug: two effects with
        one handle made the client read the second as an extension of the first.

        The measured handles sit in the same 0x1xxxx space as actors -- 66,052 to
        66,066 across one session -- which is why this counts from there.
        """
        key = (actor, wire)
        found = self._handles.get(key)
        if found is None:
            self._next_handle += 1
            found = FIRST_HANDLE + self._next_handle
            self._handles[key] = found
        return found

    def running_on(self, holder) -> list:
        """What is still running on *holder*, the expired dropped."""
        now = time.monotonic()
        holder.running = [r for r in holder.running if r[2] + r[3] > now]
        return holder.running

    def effect_message(self, holder, tick: int) -> bytes | None:
        """A 0x004F for *holder*, or None when nothing is running on it.

        None matters: an empty command is answered with "Received empty
        StatusEffectCommand!", and a 0x004F carries the actor's *whole* set, so sending
        one with nothing in it says every effect has ended.
        """
        # The cheap test first. Almost every actor almost always has nothing running,
        # and this is called once per player and once per creature per tick -- 2,000
        # players put the tick 1 ms over its 100 ms budget without it.
        if not holder.running:
            if self._sent:
                self._sent.pop(holder.actor, None)
            return None
        live = self.running_on(holder)
        if not live:
            self._sent.pop(holder.actor, None)
            return None
        # Only when it changes. Which effects are on an actor is state; sending it
        # again does not restate it, it asks the client to add them a second time --
        # and the client answers "Failed to add actor effect ... Effect already
        # present!".
        signature = tuple(sorted((wire, round(seconds, 2)) for wire, _p, _s, seconds, _c in live))
        if self._sent.get(holder.actor) == signature:
            return None
        self._sent[holder.actor] = signature
        mine = int.from_bytes(holder.actor, "little")
        elements = []
        for wire, parameters, started, seconds, causer in live:
            duration = int(round(seconds * statuseffect.RATE))
            elements.append(
                statuseffect.Element(
                    index=wire,
                    # Field 0 is a handle for this effect *instance*, never the actor
                    # -- 394 of 394 measured elements -- and unique within a message in
                    # 186 of 190. Field 7 is the actor the effect is on: the message's
                    # own actor in 265 of 394.
                    #
                    # Field 7 was briefly a "grant handle" here, read off a single
                    # official Dragon Hide message whose armour and resistance elements
                    # shared a value that was not the actor. Per effect the majority is
                    # the other way: that same effect carries the actor in 18 of its 34
                    # measured elements and warshout's mightybash buff in 12 of 12. One
                    # sample was fitted and the majority thrown away.
                    instance=self._handle(holder.actor, wire),
                    holder=mine,
                    start=tick,
                    end=tick + duration,
                    duration=duration,
                    second=measured.second_of(wire),
                    sixth=measured.sixth_of(wire),
                    # Per effect, from the table, and not from a rule about the
                    # context: skill_frenzyshout_buff_armor carries (0,0,0,1) in all 34
                    # of its measured elements and
                    # skill_laceratingstrike_debuff_armor carries (0,0,1,1) in all four
                    # of its, while whether field 7 is the actor predicts neither.
                    # The first three from the table, the fourth always set. The
                    # fourth is the one the client tests at [element+0x24]: clear, it
                    # jumps past the parameters and skips the element. An element built
                    # here always carries its parameters, so it has to be set --
                    # whatever a truncated sample in the table happens to say.
                    flags=(
                        *(bool(bit) for bit in
                          measured.FLAGS.get(wire, (0, 0, 0, 1))[:3]),
                        True,
                    ),
                    parameters=list(parameters),
                    more=bool(measured.more_of(wire)),
                    byte=measured.tail_of(wire),
                    vectors=list(measured.vectors_of(wire)),
                )
            )
        # Every message, with the names the client displays. Without this there was no
        # way to compare what went out against what was on the screen, and the answer
        # to "je lance dragon hide j'ai power of smash" turned out to be that a 0x004F
        # carries an actor's *whole* set: warshout's buffs last ten seconds, so a
        # frenzyshout cast seven seconds later travels with them still in the list --
        # and warshout's are the only two of the seven with a displayed name, so they
        # are the only two the buff bar can label.
        log.info(
            "%s: 0x004F to %s carries %d: %s",
            self.name,
            holder.actor.hex(),
            len(elements),
            ", ".join(
                "%s%s %.1fs"
                % (
                    found.id if found else "?",
                    " [%s]" % effect_titles.effect_title(found.id) if found and effect_titles.effect_title(found.id) else "",
                    element.duration / statuseffect.RATE,
                )
                for element in elements
                if (found := effects.by_wire(element.index)) or True
            ),
        )
        return statuseffect.encode(
            statuseffect.StatusEffects(actor=mine, elements=elements)
        )

    def _tick_pair(self, sender: Address) -> None:
        """One tick for one player: the 0x004F state, then its position update.

        The order matters — the real server always sends 0x004F first — and the
        pair is what the client expects continuously, not once.
        """
        player = self.player(sender)
        if player.position is None:
            return
        # The order matters: the real server sends the 0x004F before the position.
        message = self.effect_message(player, player.server_tick)
        if message is not None:
            self._emit(message, sender)
        for message in self._creature_effects:
            self._emit(message, sender)
        self._emit(self.entity_update(player.position, player.server_tick), sender)

    def tick(self) -> list[tuple[Address, bytes]]:
        """Advance the world one tick and return everything it wants sent.

        Corpses age first, so one killed this tick is still reported once.
        """
        self.age_corpses()
        # Creatures first, once, so every player is sent the same world.
        clock = next(
            (p.server_tick for p in self.inhabitants() if p.server_tick), 0
        )
        self.advance_creatures(clock)
        # Once for the world, not once per player. Which effects are on a creature does
        # not depend on who is looking, and rebuilding them per player made a tick
        # O(players x creatures) -- 101 ms for two thousand players against a 100 ms
        # budget, which the scale test caught.
        self._creature_effects = [
            message
            for creature in self._ready().creatures.values()
            if creature.running
            and (message := self.effect_message(creature, clock)) is not None
        ]
        for player in self.inhabitants():
            # The level, once more, a few seconds after arriving. See announce_level.
            entered = self._entered_at.get(player.address)
            if entered is not None and time.monotonic() - entered >= LEVEL_AGAIN:
                del self._entered_at[player.address]
                self.announce_level(player.address, again=True)
            self._tick_pair(player.address)
        self.creatures_strike()
        self.land_hits()
        self.land_swings()
        return self._drain()

    def enter(self, sender: Address) -> list[tuple[Address, bytes]]:
        """The first tick pair a player gets, on arrival, and its level.

        The level first, and it is not decoration. The client's status effect handler
        creates each effect through a function that asserts ``0 < causerLevel``, and
        when the instance is not created it advances to the next element and logs
        nothing. So an actor whose level the client never learned can receive no status
        effect at all -- which is what "rien n'apparait" was, with the message itself
        correct the whole time. See dsor/vitals.py.
        """
        self.announce_level(sender)
        self._entered_at[sender] = time.monotonic()
        self._tick_pair(sender)
        return self._drain()

    def announce_level(self, sender: Address, again: bool = False) -> None:
        """Tell the client what level this player's actor is.

        *again* resends a level already sent. The replayed player state is a level-1
        character's, so the interface starts from that and the level it shows is
        whatever last told it otherwise. Sending once at map entry may land before the
        interface is up, and there is nothing to acknowledge it, so it is repeated a
        few seconds in -- see LEVEL_AGAIN_TICKS.
        """
        player = self.player(sender)
        level = player.level or self.rules.start_level or 1
        if not again and self._levelled.get(player.actor) == level:
            return
        self._levelled[player.actor] = level
        self._emit(vitals.player_level(level, player.actor), sender)
        log.info(
            "%s: %s is level %d%s",
            self.name,
            player.actor.hex(),
            level,
            " (again)" if again else "",
        )

    def attack(
        self,
        sender: Address,
        wire: int | None = None,
        aim: int | None = None,
    ) -> list[tuple[Address, bytes]]:
        """A player has swung skill *wire* toward *aim*: resolve it and send."""
        refusal = self.may_use(sender, wire)
        if refusal is not None:
            log.warning("%s: %s refused: %s", self.name, sender, refusal)
            return []
        self.note_use(sender, wire, skill_at(wire))
        self.resolve_attack(sender, wire, aim)
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
        standing = self.player(sender).position
        lying = self.dropped.get(actor)
        if (
            self.rules.enforce
            and standing is not None
            and lying is not None
            and self.wire_of_described(lying).distance_to(standing)
            > self.rules.pickup_range * WORLD_SCALE
        ):
            self.player(sender).claims.offences += 1
            log.warning(
                "%s: %s asked for item %s from too far away",
                self.name,
                sender,
                actor.hex(" "),
            )
            return []
        if actor not in self.dropped:
            log.info("%s: %s asked for item %s, which is not lying here",
                     self.name, sender, actor.hex(" "))
            return []
        if self.next_slot < 0:
            self.next_slot = self.rules.first_slot
        if self.next_slot >= self.rules.slot_capacity:
            log.info(
                "%s: %s asked for item %s and the bag is full at %d cells",
                self.name, sender, actor.hex(" "), self.rules.slot_capacity,
            )
            return []
        where = self.dropped.pop(actor)
        blueprint = self.templates.pop(actor, None)
        slot = self.next_slot
        self.next_slot += 1
        player = self.player(sender)
        now = datetime.now()
        self._emit(
            inventory.picked_up(
                item_taken(),
                int.from_bytes(actor, "little"),
                template=blueprint,
                where=where,
                stamped=(now.year, now.month, now.day, now.hour, now.minute, now.second),
                slot=slot,
                # The two that used to be replayed, and the reason a pickup collapsed
                # the health bar: the recorded reply was taken from a level 1
                # character and its scalars carry that character's 236 health.
                health=player.health or self.player_health(player.level),
                beside=player.resource or None,
            ),
            sender,
        )
        log.info("%s: %s picked up %s as item %s into cell %d of %d, health %.0f",
                 self.name, sender, blueprint or "the recorded blueprint",
                 actor.hex(" "), slot, self.rules.slot_capacity,
                 player.health or self.player_health(player.level))
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
