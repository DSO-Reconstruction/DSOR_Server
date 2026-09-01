"""The andermant in the replayed player state, and how thin the evidence for it is.

The level and experience were measured against the live service's own state, on five
characters. This was not. The amount is a single u32 reading 600 in the recording, the
operator reports 600 andermant on screen, it is the only 600 in the first 300,000 bits
whose neighbours are not garbage, and it sits in a run of zeros -- which is what a fresh
character's currency block should look like.

One value, one recording, matching what is displayed. That reasoning was wrong twice this
session, so the writer refuses unless the field still reads exactly 600, and the amount is
a setting that can be turned off in one line.
"""

import pathlib

from dsor import currency, playerstate

STATE = pathlib.Path(__file__).resolve().parent.parent / "dsor/data/zone_content.bin"


def state() -> bytes:
    return STATE.read_bytes()


def test_the_recording_holds_the_amount_the_operator_sees():
    assert currency.andermant_of(state()) == currency.RECORDED_ANDERMANT == 600


def test_writing_changes_three_bytes_and_leaves_the_rest():
    raw = state()
    out = currency.with_andermant(raw, 9999999)
    assert currency.andermant_of(out) == 9999999
    assert len(out) == len(raw)
    assert len([i for i in range(len(raw)) if raw[i] != out[i]]) == 3
    kept = playerstate.progress_of(out)
    assert (kept["level"], kept["experience"]) == (1, 0)
    assert kept["name"] == "balenciagas"
    assert kept["map"] == "a0001_start_tutorial_dun"


def test_it_refuses_to_write_over_anything_but_the_recorded_amount():
    """The guard, and the only thing between a wrong offset and a corrupted 631 KB."""
    raw = state()
    once = currency.with_andermant(raw, 9999999)
    assert currency.with_andermant(once, 12345) == once, "not twice"


def test_the_offset_is_anchored_on_the_experience_and_not_on_the_message_start():
    """Which is what stops it landing in the wrong place on another recording: the
    experience is itself found by walking the name and the map, whose lengths differ
    between characters."""
    found = playerstate.progress_of(state())
    assert currency.andermant_of(state()) == 600
    assert currency.ANDERMANT_REL == 576
    from raknet.bitstream import BitReader

    at = found["experience_at"] + currency.ANDERMANT_REL
    assert BitReader(state(), at).read_uint(32) == 600


def test_a_state_that_cannot_be_walked_is_left_alone():
    for other in (b"", b"\x00" * 4000, state()[::-1]):
        assert currency.andermant_of(other) is None
        assert currency.with_andermant(other, 500) == other


def test_the_amount_is_clamped():
    out = currency.with_andermant(state(), 10**12)
    assert currency.andermant_of(out) == currency.MOST


def test_the_skill_book_and_the_bars_survive_the_write():
    from dsor import actionbar, skillbook

    raw = state()
    out = currency.with_andermant(raw, 9999999)
    assert skillbook.find_book(out) == skillbook.find_book(raw)
    assert len(actionbar.find_bars(out)) == len(actionbar.find_bars(raw))
