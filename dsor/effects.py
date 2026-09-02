"""The client's own status effect and skill tables, for all five classes.

A rebuild. The version deleted before this one was a generated snapshot taken through a
filter -- 458 of 6,703 effects, the warrior's eighteen skills and what their auras
reached -- so anything outside that set was simply unknown, and a snapshot regenerated
against a different copy of the database shifted every wire index by however many rows
had been inserted. This reads the database directly: 6,703 effects, 1,983 skills, every
class.

**Why the database can be trusted here.** It was checked against the traffic rather than
assumed. 1,581 real skill casts were pulled out of 80 captures, and for each one the
effects the real server added were measured by taking an actor's effect list just after
the cast minus the list just before -- a 0x004F carries the whole list, so the difference
is what the cast caused. Every one of the 13 effects measured that way appears in these
three columns, and the ``C:1.0`` entries account for 11 of the 13; for ``frenzyshout``
the ``C:1.0`` set is exactly the three effects measured. The two left over are
``C:0.0`` entries that the recorded character's talents unlocked.

That is why ``C:1.0`` is the rule below, and why it is a measurement rather than a
guess: a talentless character gets what a talentless character got.

**The visuals are not in the message.** ``StartSequence``, ``StartAnimation`` and
``IconBrush`` are columns on the effect, so the client draws the effect's VFX, its
animation and its icon itself from the index it is sent. 2,196 of the 6,703 effects have
a sequence and 2,829 an icon. So "pas d'animation de stun" and "pas d'icone de break
armor" were never two problems -- both follow from sending the right index or not.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import cache

from dsor import database

log = logging.getLogger("effects")

#: The tables read.
EFFECT_TABLE = "_Template_StatusEffect"
SKILL_TABLE = "_Template_Skill"

#: The three columns naming what a skill does. Measured against real casts: every effect
#: a real server was seen applying is in one of them.
ON_USER = "UserStatusEffects"
ON_VICTIM = "VictimStatusEffects"
ON_GROUND = "LocationStatusEffects"

#: The chance an entry has to be certain. Entries below it are talent, item or set
#: gated: the recorded level-100 character triggered two of them, a fresh one triggers
#: none.
CERTAIN = 1.0


#: A LocaleId naming a talent marks the effect as that talent's variant. Fourteen of
#: the 6,703 effects carry one, and the one that matters here is
#: ``skill_frenzyshout_buff_lifeleech``, whose displayed name is "Dragon Hide -
#: Dragon's Meal". Its column entry is C:1.0, so chance does not gate it -- the talent
#: does, and a fresh character has no talents. Which is why the live service put it on
#: the recorded level-100 character 42 casts out of 57 and why the operator, casting the
#: same skill on their own character, sees two effects labelled "Dragon Hide" and not
#: three.
TALENT_MARK = "_talent"


@dataclass(frozen=True)
class Effect:
    """One row of ``_Template_StatusEffect``."""

    #: Its index on the wire: the row's rowid minus one. Confirmed by name against
    #: eight different skills' own debuffs in the captures.
    wire: int
    id: str
    #: Seconds, when the skill's own entry does not override it with ``D:``.
    duration: float
    #: The key its displayed name is looked up under. Already ends in ``Title``, and it
    #: is *not* the effect's own id: ``skill_frenzyshout_buff_armor`` is named through
    #: ``warrior_frenzyshout_buff_armorTitle``, which reads "Dragon Hide".
    locale_id: str
    #: What the client draws. Not sent -- it reads these itself.
    sequence: str
    animation: str
    icon: str
    #: The aura this effect projects, if it is an aura.
    aura_effect: str
    aura_radius: float
    aura_targets: str

    @property
    def visible(self) -> bool:
        """Whether the client has anything to draw for it."""
        return bool(self.sequence or self.icon or self.animation)

    @property
    def talent_gated(self) -> bool:
        """Whether a talent unlocks it. See :data:`TALENT_MARK`."""
        return TALENT_MARK in self.locale_id.lower()


@dataclass(frozen=True)
class Entry:
    """One effect named in a skill's column, with the fields that column carries."""

    effect: str
    #: ``C:`` -- see :data:`CERTAIN`.
    chance: float
    #: ``D:`` seconds, overriding the effect's own duration.
    duration: float | None
    #: ``DE:`` seconds before it starts.
    delay: float | None
    #: ``$0``..``$4``, which become the element's parameters.
    substitutions: tuple[float, ...]
    #: ``DP:`` seconds. The most common field in these columns after ``C`` and the
    #: second most common overall -- 2,741 uses against ``On:``'s 693 -- and it was
    #: being dropped.
    #:
    #: What it *means* is not settled. It equals ``D`` in 2,504 of the 2,697 entries
    #: that carry both, and where they differ it is smaller and the effect is crowd
    #: control: ``debuff_cc_stun`` is ``D:5.0,DP:1.5`` and ``debuff_cc_petrify`` is
    #: ``D:5.0,DP:3.0``, which reads either as "1.5 seconds of stun inside a 5-second
    #: immunity" or as "shortened in player-versus-player".
    #:
    #: What *is* settled is what to do when there is no ``D`` at all, which is 24
    #: entries. warshout's ``ctfdropflag`` is one, at ``DP:10.0``, and the live service
    #: puts it on the player for **250 ticks** -- ten seconds exactly, in the same
    #: message as the buffs beside it. So DP is a duration here, and dropping it is why
    #: this server never applied that effect.
    duration_pvp: float | None = None
    #: ``TR:`` seconds between ticks. 377 entries say 1.0, 105 say 0.0.
    tick_rate: float | None = None
    #: ``On:`` -- the event that applies it instead of the cast, lowercased. See
    #: :func:`triggered`.
    trigger: str | None = None
    #: ``OFF:`` -- the event that ends it, lowercased. 27 entries carry one.
    until: str | None = None

    @property
    def certain(self) -> bool:
        return self.chance >= CERTAIN

    @property
    def triggered(self) -> bool:
        """Whether an event applies it rather than the cast.

        693 entries across the client's skill table carry one, and the vocabulary is
        small: ``skillstart`` (384), ``kill`` (141), ``hit`` (106), ``hitmarked`` (23),
        ``hitcritical`` (13), ``hitfrost`` (8), ``lock`` (5), ``summonsdead`` (5),
        ``hitmagecharged`` (4), ``hithostile`` (2), ``hitally`` (2).

        Nothing raises these yet, and the reason is a measurement rather than laziness:
        **638 of the 693 name an effect belonging to an item, an item set, ammunition, a
        rune, food, a skill book or a talent**, none of which this server models. Of the
        55 that remain, all but a handful are boss and monster skills. For the warrior's
        ten skills the count of trigger entries that a character with no gear and no
        talents should receive is **zero** -- so firing them would not add what the game
        does, it would add another character's equipment.
        """
        return self.trigger is not None


def _number(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def parse_entries(column: str | None) -> tuple[Entry, ...]:
    """The entries in one of the three columns.

    The format is ``name,C:1.0,D:8.0,DP:8.0,DE:1.0,$0:-0.4;name,...``. A field this does
    not recognise is skipped rather than guessed at, and a ``$n`` naming a variable
    instead of a number -- ``$0:charge_resis2026`` is real -- contributes zero, which is
    what a character with no talent to set it has.
    """
    if not column:
        return ()
    found: list[Entry] = []
    for chunk in column.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(",")
        name = parts[0].strip()
        if not name:
            continue
        chance, duration, delay = 1.0, None, None
        persistent = rate = None
        trigger = until = None
        substitutions = [0.0] * 5
        for field in parts[1:]:
            key, _, value = field.partition(":")
            key, value = key.strip(), value.strip()
            # Case-insensitively: the same column writes both ``On:`` and ``ON:``, and
            # ``skillstart`` and ``SkillStart``, 101 times against 592.
            upper = key.upper()
            if upper == "C":
                chance = _number(value) if _number(value) is not None else 1.0
            elif upper == "D":
                duration = _number(value)
            elif upper == "DP":
                persistent = _number(value)
            elif upper == "DE":
                delay = _number(value)
            elif upper == "TR":
                rate = _number(value)
            elif upper == "ON":
                trigger = value.lower() or None
            elif upper == "OFF":
                until = value.lower() or None
            elif re.fullmatch(r"\$\d", key):
                index = int(key[1])
                if index < len(substitutions):
                    substitutions[index] = _number(value) or 0.0
        found.append(
            Entry(
                name,
                chance,
                duration,
                delay,
                tuple(substitutions),
                duration_pvp=persistent,
                tick_rate=rate,
                trigger=trigger,
                until=until,
            )
        )
    return tuple(found)


@cache
def _effects() -> tuple[dict[int, Effect], dict[str, Effect]]:
    rows = database.rows(
        EFFECT_TABLE,
        "Id",
        "StatusEffectDuration",
        "LocaleId",
        "StartSequence",
        "StartAnimation",
        "IconBrush",
        "AuraEffectId",
        "AuraRadius",
        "AuraTargets",
    )
    by_wire: dict[int, Effect] = {}
    by_name: dict[str, Effect] = {}
    for (index, name, duration, locale, sequence, animation, icon, aura, radius,
         targets) in rows:
        effect = Effect(
            wire=index,
            id=name or "",
            duration=_number(duration) or 0.0,
            locale_id=locale or "",
            sequence=sequence or "",
            animation=animation or "",
            icon=icon or "",
            aura_effect=aura or "",
            aura_radius=_number(radius) or 0.0,
            aura_targets=targets or "",
        )
        by_wire[index] = effect
        if effect.id:
            by_name[effect.id] = effect
    return by_wire, by_name


@cache
def _skills() -> dict[int, tuple[str, str, str, str]]:
    """``wire -> (id, user column, victim column, ground column)``."""
    rows = database.rows(SKILL_TABLE, "Id", ON_USER, ON_VICTIM, ON_GROUND)
    return {
        index: (name or "", user or "", victim or "", ground or "")
        for index, name, user, victim, ground in rows
    }


def by_wire(wire: int) -> Effect | None:
    return _effects()[0].get(wire)


def by_id(name: str) -> Effect | None:
    return _effects()[1].get(name)


def wire_of(name: str) -> int | None:
    found = by_id(name)
    return None if found is None else found.wire


def loaded() -> int:
    """How many effects are known. Zero without the database."""
    return len(_effects()[0])


def _column(skill_wire: int, which: int) -> tuple[Entry, ...]:
    held = _skills().get(skill_wire)
    return () if held is None else parse_entries(held[which])


def granted_by(skill_wire: int, certain_only: bool = True) -> tuple[Entry, ...]:
    """What the skill puts on whoever casts it."""
    found = _column(skill_wire, 1)
    return tuple(e for e in found if e.certain) if certain_only else found


def inflicted_by(skill_wire: int, certain_only: bool = True) -> tuple[Entry, ...]:
    """What it puts on whoever it hits."""
    found = _column(skill_wire, 2)
    return tuple(e for e in found if e.certain) if certain_only else found


def placed_by(skill_wire: int, certain_only: bool = True) -> tuple[Entry, ...]:
    """What it puts on the ground. earthquake and defiance, for the warrior."""
    found = _column(skill_wire, 3)
    return tuple(e for e in found if e.certain) if certain_only else found


def seconds_of(entry: Entry) -> float:
    """How long *entry* lasts: ``D:``, else ``DP:``, else the effect's own.

    The ``DP:`` step is measured, not a guess at precedence. 24 entries carry ``DP``
    and no ``D``, warshout's ``ctfdropflag`` among them at ``DP:10.0``, and the live
    service puts that effect on the player for 250 ticks -- ten seconds, in the same
    message as the buffs beside it. Without this step it lasted zero seconds and was
    never sent, which this project had written down as the service not applying it.
    """
    if entry.duration is not None:
        return entry.duration
    if entry.duration_pvp is not None:
        return entry.duration_pvp
    found = by_id(entry.effect)
    return found.duration if found is not None else 0.0
