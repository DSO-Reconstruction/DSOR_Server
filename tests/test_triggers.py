"""The fields in a skill's effect column that were being dropped.

Three of them, and they are more common than anything this project had already read:
``DP`` 2,741 times, ``TR`` 550, ``On``/``ON`` 693, against ``DE``'s 345. What they mean
was settled by measuring the live service in ``officiel4`` rather than by choosing the
reading that looked best.
"""

import collections
import re

from dsor import database, effects
from dsor.skills import wire_of

COLUMNS = ("UserStatusEffects", "VictimStatusEffects", "LocationStatusEffects")

#: An effect belonging to something this server has no model for: a piece of gear, an
#: item set, ammunition, a rune, food, a skill book, a seasonal set, a talent.
GEAR = re.compile(
    r"^(item|itemset|ammunition|sb|set|chr\d|frozen_plain|jewel|food|costume|rune"
    r"|mercenary|companion|minion|pet|gem|essence)_|^Set[A-Z]|_set_|2025|2026"
    r"|_talent|talent_"
)


def every_entry():
    for row in database.rows("_Template_Skill", "Id", *COLUMNS):
        _wire, ident = row[0], row[1]
        for column in row[2:]:
            for entry in effects.parse_entries(column):
                yield ident, entry


def test_the_trigger_vocabulary_is_small_and_closed():
    """Eleven events, and nothing here invents a twelfth."""
    seen = collections.Counter(
        entry.trigger for _skill, entry in every_entry() if entry.trigger
    )
    assert set(seen) == {
        "skillstart",
        "kill",
        "hit",
        "hitmarked",
        "hitcritical",
        "hitfrost",
        "lock",
        "summonsdead",
        "hitmagecharged",
        "hithostile",
        "hitally",
    }
    assert seen["skillstart"] == 384
    assert seen["kill"] == 141
    assert seen["hit"] == 106
    assert sum(seen.values()) == 693


def test_the_case_of_the_key_and_the_value_both_vary():
    """``On:`` 101 times and ``ON:`` 592, ``SkillStart`` and ``skillstart``.

    A case-sensitive parser reads one in seven of them.
    """
    columns = [
        column
        for row in database.rows("_Template_Skill", "Id", *COLUMNS)
        for column in row[2:]
        if column
    ]
    joined = ";".join(columns)
    assert "ON:SkillStart" in joined and "On:skillstart" in joined
    assert (
        effects.parse_entries("x,On:SkillStart")[0].trigger
        == effects.parse_entries("x,ON:skillstart")[0].trigger
        == "skillstart"
    )


def test_almost_every_trigger_belongs_to_gear_or_a_talent():
    """Which is why none of them is raised.

    638 of the 693 name an effect this server has no model for the source of, and of
    the 55 that remain nearly all are boss and monster skills. Firing them would not
    add what the game does; it would add another character's equipment.
    """
    gated = ungated = 0
    for _skill, entry in every_entry():
        if not entry.triggered:
            continue
        if GEAR.search(entry.effect):
            gated += 1
        else:
            ungated += 1
    assert gated == 638
    assert ungated == 55


def test_the_warrior_gains_nothing_from_them():
    """Zero, for all ten of the warrior's own skills."""
    warrior = (
        "angrystrike mightyswing warshout frenzyshout seismicslam "
        "laceratingstrike enragingleap defiance mightybash earthquake"
    ).split()
    for name in warrior:
        wire = wire_of(name)
        for entries in (
            effects.granted_by(wire, certain_only=False),
            effects.inflicted_by(wire, certain_only=False),
        ):
            for entry in entries:
                if entry.triggered:
                    assert GEAR.search(entry.effect), f"{name}: {entry.effect}"


def test_dp_equals_d_almost_always_and_is_smaller_when_it_does_not():
    """And where it differs the effect is crowd control.

    ``debuff_cc_stun`` is ``D:5.0,DP:1.5`` and ``debuff_cc_petrify`` is
    ``D:5.0,DP:3.0``, which reads either as a short effect inside a long immunity or as
    one shortened in player-versus-player. Either way it is not something to guess at,
    and this only relies on the case where there is no ``D`` at all.
    """
    both = differ = 0
    smaller = 0
    for _skill, entry in every_entry():
        if entry.duration is None or entry.duration_pvp is None:
            continue
        both += 1
        if entry.duration != entry.duration_pvp:
            differ += 1
            if entry.duration_pvp < entry.duration:
                smaller += 1
    assert both == 2697
    # 183, not the 193 a first count gave: that one compared the text of the fields, and
    # ``D:8.00`` against ``DP:8.0`` is ten entries that differ as strings and not as
    # numbers.
    assert differ == 183
    assert smaller > differ * 0.8


def test_an_entry_with_neither_field_falls_back_to_the_effect_s_own_row():
    entry = effects.Entry("skill_warshout_buff_mightybash", 1.0, None, None, (0.0,) * 5)
    assert effects.seconds_of(entry) == 1.0
    assert effects.by_id("skill_warshout_buff_mightybash").duration == 1.0
    # And with DP, which is what its column actually carries: ten seconds, which is
    # what the live service put on the wire fourteen times out of fourteen.
    with_dp = effects.Entry(
        "skill_warshout_buff_mightybash", 1.0, None, None, (0.0,) * 5,
        duration_pvp=10.0,
    )
    assert effects.seconds_of(with_dp) == 10.0


def test_the_off_qualifier_is_read_too():
    """27 entries carry one. angrystrike's is the readable example."""
    ends = collections.Counter(
        entry.until for _skill, entry in every_entry() if entry.until
    )
    assert ends["summonsdead"] == 18
    assert ends["skillstartexceptangrystrike"] == 5
    assert sum(ends.values()) == 27
