"""The level and experience inside the replayed player state.

The roster feeds the selection screen and this feeds the game, which is why the level was
right on the screen and back to 1 after pressing Play -- "le niveau est bien sauvegarde
dans l'ecran des persos mais apres le play je reste niveau 1".

Measured against the live service's own 0x001D, taken from a capture made at the client's
launch: 1,203,213 bytes there, 631,240 in the recording, and both lay out

    u16 length + name | 80 bits | u16 length + map | 210 bits | u32 level | u32 experience

    Username  a0200_kingscity           100  882246499
    Username      a0001_start_tutorial_dun    1          0
"""

import pathlib

import pytest

from dsor import playerstate

STATE = pathlib.Path(__file__).resolve().parent.parent / "dsor/data/zone_content.bin"


#: The scrubbed name. Padded to the length of the one it replaced, so nothing in
#: the capture moved -- see tools/anonymise.py.
PLAYER = "Username000"

def state() -> bytes:
    return STATE.read_bytes()


def test_the_recorded_state_is_a_level_one_character():
    got = playerstate.progress_of(state())
    assert got is not None
    assert got["name"] == PLAYER
    assert got["map"] == "a0001_start_tutorial_dun"
    assert got["level"] == 1
    assert got["experience"] == 0


def test_the_fields_are_not_byte_aligned():
    got = playerstate.progress_of(state())
    assert got["level_at"] % 8 != 0 or got["experience_at"] % 8 != 0


@pytest.mark.parametrize(
    "level,experience", [(1, 0), (18, 128322), (100, 882246499), (42, 300000)]
)
def test_what_is_written_is_what_is_read_back(level, experience):
    out = playerstate.with_progress(state(), level, experience)
    got = playerstate.progress_of(out)
    assert (got["level"], got["experience"]) == (level, experience)


def test_writing_moves_nothing_in_631_kb():
    raw = state()
    out = playerstate.with_progress(raw, 100, 882246499)
    assert len(out) == len(raw)
    changed = [i for i in range(len(raw)) if raw[i] != out[i]]
    assert len(changed) == 6, changed
    kept = playerstate.progress_of(out)
    assert kept["name"] == PLAYER
    assert kept["map"] == "a0001_start_tutorial_dun"


def test_the_level_is_clamped_to_a_hundred():
    for asked, want in ((0, 1), (104, 100), (9999, 100)):
        out = playerstate.with_progress(state(), asked, 0)
        assert playerstate.progress_of(out)["level"] == want


def test_writing_what_is_there_changes_nothing():
    raw = state()
    assert playerstate.with_progress(raw, 1, 0) == raw


def test_a_message_of_another_shape_is_left_alone():
    """A truncation of 200 bytes is *not* in this list on purpose: the name, the map,
    the level and the experience all fit inside the first 200 bytes, so walking that
    prefix is correct and refusing it would be the bug."""
    for other in (b"", b"\x00" * 4000, state()[:150], state()[::-1]):
        assert playerstate.progress_of(other) is None, other[:8]
        assert playerstate.with_progress(other, 50, 1000) == other


def test_a_prefix_that_still_holds_the_fields_is_walked():
    got = playerstate.progress_of(state()[:200])
    assert got is not None and got["level"] == 1


def test_the_skill_book_and_the_action_bar_still_read_after_a_write():
    """The three patches share the message, so a write must not disturb the others."""
    from dsor import actionbar, skillbook

    raw = state()
    before_book = skillbook.find_book(raw)
    before_bars = len(actionbar.find_bars(raw))
    out = playerstate.with_progress(raw, 100, 882246499)
    assert skillbook.find_book(out) == before_book
    assert len(actionbar.find_bars(out)) == before_bars
