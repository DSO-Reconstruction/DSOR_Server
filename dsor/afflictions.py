"""What an effect does to the simulation, as opposed to what it draws.

An effect has two halves and this project spent a long time serving only one. The
message half tells the client an effect is on an actor, and the client applies it
*itself* -- ``Speed:$0,relative,Movement`` on the player is the client's own
arithmetic, which is why warshout's speed buff worked without this module existing.

The other half is the server's, and the asymmetry is the whole lesson: the client
owns the player's movement, and the **server** owns a creature's. So a stun sent to
the client changed nothing at all -- the server kept walking the creature and kept
letting it swing, 25 times a second, straight through the stun. Armour break was the
same story from the other end: the icon is the client's, the damage is mine.

Nothing here is invented. Every rule below reads a modifier the client's own
``_Template_StatusEffect.StartModifiers`` column spells out:

    debuff_cc_stun                       ActorFeature:off,Movement,RegularSkills
    debuff_cc_frost                      Speed:$...,relative,Movement + ...,Skills
    skill_laceratingstrike_debuff_armor  Resistance:$0,relative,Physical
    skill_mightyswing_debuff_reduce_damage  Damage:$0,relative,Min,Max

What is *not* recovered is how resistance becomes damage. The real formula involves
the attacker's level and is not in this database, so the model here is stated rather
than measured: a relative resistance change of v scales the damage taken by (1 - v),
so the -0.5 laceratingstrike carries means half again as much damage. It is a model,
it is named as one, and it is in one place so it can be replaced by a measurement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from dsor import effects

log = logging.getLogger("effects")

#: The modifier that switches a capability off outright.
FEATURE = "ActorFeature"

#: Its targets, as the templates name them.
MOVEMENT = "Movement"
SKILLS = "RegularSkills"

#: How much a relative resistance change moves the damage taken. One means a -50%
#: resistance is +50% damage; it is the stated model, not a measurement.
RESISTANCE_TO_DAMAGE = 1.0

#: Nothing may be slowed below this, or a stunned-but-not-stunned creature stops
#: dead and never recovers because its own arrival is what clears the effect.
SLOWEST = 0.05

#: Nor amplified past this, whatever a stack of debuffs multiplies out to.
STRONGEST = 10.0


@dataclass(frozen=True)
class Condition:
    """What the effects on one actor do to it, all of them collapsed together."""

    #: Whether it may move at all. False under a stun.
    moves: bool = True
    #: Whether it may use a skill -- for a creature, whether it may swing.
    acts: bool = True
    #: Multiplier on how far it moves in a tick.
    speed: float = 1.0
    #: Multiplier on how fast it may swing again.
    haste: float = 1.0
    #: Multiplier on the damage it deals.
    damage_dealt: float = 1.0
    #: Multiplier on the damage it takes.
    damage_taken: float = 1.0
    #: Multiplier on what a skill costs. Dragon Hide carries
    #: ``ResourceCost:$0,relative,player_skills`` with $0 = -1.0, so for ten seconds
    #: every skill is free -- which is the whole of what that skill does, and it does
    #: not heal.
    resource_cost: float = 1.0
    #: Added to the resource each second, absolute. Dragon Hide's other half is
    #: ``SkillResourceRegeneration:5.0,absolute``.
    resource_regen: float = 0.0

    @property
    def helpless(self) -> bool:
        """Stunned: neither moving nor acting."""
        return not self.moves and not self.acts

    def describe(self) -> str:
        """For the log, so a creature's state can be read without a debugger."""
        parts = []
        if not self.moves:
            parts.append("rooted")
        if not self.acts:
            parts.append("silenced")
        for name, value in (
            ("speed", self.speed),
            ("haste", self.haste),
            ("damage", self.damage_dealt),
            ("taken", self.damage_taken),
            ("cost", self.resource_cost),
        ):
            if abs(value - 1.0) > 1e-6:
                parts.append(f"{name} x{value:.2f}")
        if self.resource_regen:
            parts.append(f"regen +{self.resource_regen:.1f}/s")
        return ", ".join(parts) or "unaffected"


def _resolve(modifier, parameters: tuple[float, ...]) -> float | None:
    """A modifier's value, with ``$0``..``$4`` filled in from *parameters*.

    A reference this server cannot resolve -- ``$debuff_cc_frost_movspeed`` names a
    constant that lives elsewhere in the client -- returns None rather than zero, so
    the caller can leave the modifier out instead of silently applying nothing.
    """
    text = modifier.value
    if not text:
        return None
    if text.startswith("$"):
        name = text[1:]
        if name.isdigit():
            index = int(name)
            if 0 <= index < len(parameters):
                return float(parameters[index])
            return None
        return None
    try:
        return float(text)
    except ValueError:
        return None


def condition_of(held) -> Condition:
    """Collapse everything running on an actor into one :class:`Condition`.

    *held* is what ``World.live_effects`` returns: ``(wire, parameters, seconds)``
    apiece. Multipliers multiply, switches latch off, and an effect whose value
    cannot be resolved contributes nothing rather than contributing zero.
    """
    moves = acts = True
    speed = haste = dealt = taken = cost = 1.0
    regen = 0.0

    for entry in held:
        wire, parameters = entry[0], entry[1]
        effect = effects.effect(wire)
        if effect is None:
            continue
        for modifier in effect.starts:
            attribute = modifier.attribute
            if attribute == FEATURE:
                if modifier.value.lower() != "off":
                    continue
                if MOVEMENT in modifier.targets:
                    moves = False
                if SKILLS in modifier.targets:
                    acts = False
                continue

            value = _resolve(modifier, tuple(parameters))
            if value is None:
                continue
            if attribute == "Speed":
                if MOVEMENT in modifier.targets:
                    speed *= 1.0 + value
                if "Skills" in modifier.targets:
                    haste *= 1.0 + value
            elif attribute == "Damage":
                dealt *= 1.0 + value
            elif attribute == "ResourceCost":
                if modifier.mode == "relative":
                    cost *= max(0.0, 1.0 + value)
            elif attribute == "SkillResourceRegeneration":
                regen += value
            elif attribute == "Resistance":
                # Less resistance, more damage. See RESISTANCE_TO_DAMAGE.
                taken *= 1.0 - value * RESISTANCE_TO_DAMAGE

    clamp = lambda x: min(STRONGEST, max(SLOWEST, x))  # noqa: E731
    return Condition(
        moves=moves,
        acts=acts,
        speed=clamp(speed),
        haste=clamp(haste),
        damage_dealt=clamp(dealt),
        damage_taken=clamp(taken),
        # Not clamped: zero is the point. A cost multiplier of zero is a free skill,
        # and clamping it to the slowest-anything floor would leave Dragon Hide
        # charging five percent for ever.
        resource_cost=max(0.0, min(STRONGEST, cost)),
        resource_regen=regen,
    )


UNAFFECTED = Condition()
