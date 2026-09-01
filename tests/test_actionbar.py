"""The action bar: reading it, and why filling it is left off.

The bar is not the skill book. The book says what the character owns; the bar is what it
can press. This server granted all eighteen warrior skills in the book and the bar stayed
as recorded -- one slot, ``angrystrike``, sixteen empty -- which the client confirmed
itself in all 180 QuickSlotsCommand messages of one session.

Filling it crashed the client:

    *** NEBULA ASSERTION ***
    expression: this->elements && (index >= 0) && (index < this->size)
    func: Util::FixedArray<Core::Ptr<UI::Slot>>::operator[](int)

An index past the end of the UI's slot array. The record *declares* seventeen slots and
the client's array is not seventeen long, so writing all of them walks off it. Nothing in
the data says so, which is why the reading below is kept and the writing is off by
default: the module is worth having, and turning it on costs a crash per attempt.
"""

import pathlib

from dsor import actionbar

ZONE = pathlib.Path(__file__).resolve().parent.parent / "dsor/data/zone_content.bin"


def zone() -> bytes:
    return ZONE.read_bytes()


def test_the_recorded_state_holds_ten_bars_of_one_skill():
    bars = actionbar.find_bars(zone())
    assert len(bars) == actionbar.BARS
    for start, end, slots in bars:
        assert end - start == actionbar.RECORDED_BITS
        assert len(slots) == actionbar.SLOTS
        assert slots[0] == "angrystrike"
        assert all(slot is None for slot in slots[1:]), "sixteen empty"


def test_the_bars_are_back_to_back():
    bars = actionbar.find_bars(zone())
    for before, after in zip(bars, bars[1:]):
        assert before[1] == after[0]


def test_a_wrong_offset_finds_nothing_rather_than_writing_into_it():
    """The failure that matters. A bar written where there is no bar corrupts whatever
    is there, and a corrupt player state is worse than an empty bar."""
    raw = zone()
    assert actionbar.find_bars(raw, start=actionbar.FIRST_BAR + 1) == []
    assert actionbar.with_skills(raw, ["angrystrike"], start=12345) == raw


def test_writing_the_bar_round_trips():
    raw = zone()
    wanted = ["angrystrike", "mightybash", "warshout"]
    out = actionbar.with_skills(raw, wanted)
    bars = actionbar.find_bars(out)
    assert len(bars) == actionbar.BARS
    for _start, _end, slots in bars:
        assert slots[: len(wanted)] == wanted
        assert all(slot is None for slot in slots[len(wanted):])


def test_filling_the_bar_makes_the_message_longer():
    """Unlike the book's ownership bits, which change in place.

    Worth asserting because it was the risk being watched -- and it was the wrong one.
    The client asserted on the slot *count*, not on the length.
    """
    raw = zone()
    out = actionbar.with_skills(raw, ["angrystrike", "mightybash"])
    assert len(out) > len(raw)
    unchanged = actionbar.with_skills(raw, ["angrystrike"])
    assert unchanged == raw, "writing the bar it already has changes nothing"


def test_one_skill_per_cooldown_category():
    """The rule that picks a class's real skills out of its table.

    A variant shares its base's cooldown category -- bloody360 and bloody360_Chimera are
    both Skill03, earthquake and true_earthquake both Skill15 -- and the base names
    itself as its second group while the variant names the base. Requiring that also
    drops the skills an item set grants rather than a level.
    """
    warrior = actionbar.bar_skills("warrior", 100)
    assert "bloody360" in warrior
    assert "bloody360_Chimera" not in warrior
    assert "earthquake" in warrior
    assert "true_earthquake" not in warrior
    assert "chainlightning_SetLight2026_warrior" not in warrior, "a set's skill"
    # The five the live service was seen putting on a real bar.
    for name in ("angrystrike", "mightybash", "warshout", "frenzyshout", "bloody360"):
        assert name in warrior, name


def test_the_bar_grows_with_the_level():
    assert actionbar.bar_skills("warrior", 1) == ["angrystrike"]
    assert len(actionbar.bar_skills("warrior", 20)) > 1
    assert len(actionbar.bar_skills("warrior", 100)) >= len(
        actionbar.bar_skills("warrior", 20)
    )


def test_an_unreleased_class_yields_nothing():
    """niwalk is in the database and never shipped. Its skills carry only
    ``niwalk_skills`` and unlock at level 0, so the rule yields nothing -- which is the
    right answer for unfinished data, and the reason not to bend the rule for it."""
    from dsor import database

    if not database.available():
        return
    assert actionbar.bar_skills("niwalk", 100) == []
