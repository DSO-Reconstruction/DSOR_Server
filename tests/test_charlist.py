"""The level and experience the character selection screen shows.

Saving a character server-side does nothing for this screen: it is drawn from a replayed
recording whose character is level 1 with no experience, so every login showed level 1
however much had been stored -- "tu sauvegardes rien vu que tu rejoues une connexion au
debut donc ca remet tjrs niveau 1 meme dans l'ecran de perso".

The offsets are measured on five characters. A capture of the live service taken from
the client's launch, so the login was visible, carries four:

    AmateurDeCombat   882246499  100
    MeufAGrosSeins    882260621  100
    FilleMineur       882269671  100
    BgTimide             128322   18
    balenciagas               0    1   (the recording)

Experience 256 bits and level 288 bits past the end of the entry's map string, both
u32. Counted from the map because the entries carry their strings inline and are back to
back, so nothing before it is at a fixed offset from the start of the message.

The first attempt at this wrote sixteen bits 145 bits *before* the name, having found a 1
there in the recording and taken it for the level. It is 1 in the service's level-100
characters too, and writing 104 into it broke the login.
"""

import pathlib

import pytest

from dsor import charlist

ROSTER = pathlib.Path(__file__).resolve().parent.parent / "dsor/data/character_list.bin"


def roster() -> bytes:
    return ROSTER.read_bytes()


def test_the_recorded_roster_reads_as_one_level_one_character():
    found = charlist.entries(roster())
    assert len(found) == 1
    assert found[0]["name"] == "balenciagas"
    assert found[0]["map"] == "a0001_start_tutorial_dun"
    assert found[0]["level"] == 1
    assert found[0]["experience"] == 0


@pytest.mark.parametrize(
    "level,experience", [(1, 0), (18, 128322), (100, 882246499), (57, 400000), (100, 0)]
)
def test_what_is_written_is_what_is_read_back(level, experience):
    out = charlist.with_progress(roster(), level, experience)
    found = charlist.entries(out)[0]
    assert found["level"] == level
    assert found["experience"] == experience


def test_writing_moves_nothing_and_touches_only_the_two_fields():
    raw = roster()
    out = charlist.with_progress(raw, 100, 882246499)
    assert len(out) == len(raw)
    changed = [i for i in range(len(raw)) if raw[i] != out[i]]
    # Two 32-bit fields, four bits apart from a byte boundary, so six bytes.
    assert changed == [100, 101, 102, 103, 104, 105]
    kept = charlist.entries(out)[0]
    assert kept["name"] == "balenciagas"
    assert kept["map"] == "a0001_start_tutorial_dun"


def test_the_fields_are_not_byte_aligned():
    """Which is why the write is bit by bit. A byte splice would hit the neighbours."""
    found = charlist.entries(roster())[0]
    assert found["experience_at"] % 8 != 0
    assert found["level_at"] % 8 != 0


def test_the_level_is_clamped_to_what_the_service_uses():
    """100 is the ceiling, measured: 882,246,499 experience reads 100 in the live
    service's roster while this server's own curve says 104."""
    assert charlist.HIGHEST_LEVEL == 100
    for asked, want in ((0, 1), (-3, 1), (104, 100), (9999, 100)):
        out = charlist.with_progress(roster(), asked, 0)
        assert charlist.entries(out)[0]["level"] == want


def test_writing_what_is_already_there_changes_nothing():
    raw = roster()
    assert charlist.with_progress(raw, 1, 0) == raw


def test_a_message_of_another_shape_is_left_alone():
    """The guard the first version lacked. A roster this cannot walk is one to leave
    alone rather than half-rewrite."""
    for other in (b"", b"\x00" * 400, roster()[:40], roster()[::-1]):
        assert charlist.entries(other) == []
        assert charlist.with_progress(other, 50, 1000) == other


# ------------------------------------------------------------------ the andermant


def test_the_andermant_is_the_account_s_and_not_the_character_s():
    """Measured on two screens and four entries.

    The live service's roster carries 4,814 for all four of its characters and the
    recording carries 600 for its one -- which is exactly what the operator reported
    seeing on the live service and on this server. Two independent values, each matching
    a screen, and one of them identical across four entries.

    A first attempt put it in the player state, on a single 600 found there. Writing that
    changed nothing on screen, which is how the wrong field was ruled out.
    """
    found = charlist.entries(roster())
    assert len(found) == 1
    assert found[0]["andermant"] == 600
    assert charlist.ANDERMANT_REL == 160


def test_writing_the_andermant_leaves_the_level_and_experience_alone():
    raw = roster()
    out = charlist.with_andermant(raw, 9999999)
    got = charlist.entries(out)[0]
    assert got["andermant"] == 9999999
    assert (got["level"], got["experience"]) == (1, 0)
    assert got["name"] == "balenciagas"
    assert len(out) == len(raw)
    assert len([i for i in range(len(raw)) if raw[i] != out[i]]) == 3


def test_the_andermant_goes_into_every_entry():
    """It is the account's, so writing it into one entry and not the others would make
    the selection screen disagree with itself."""
    raw = roster()
    out = charlist.with_andermant(raw, 4242)
    assert [e["andermant"] for e in charlist.entries(out)] == [4242]


def test_the_andermant_is_clamped_and_a_repeat_is_a_no_op():
    assert charlist.entries(
        charlist.with_andermant(roster(), 10**12)
    )[0]["andermant"] == charlist.MOST_ANDERMANT
    once = charlist.with_andermant(roster(), 500)
    assert charlist.with_andermant(once, 500) == once


def test_a_roster_that_cannot_be_walked_keeps_its_andermant():
    for other in (b"", b"\x00" * 400, roster()[::-1]):
        assert charlist.with_andermant(other, 5000) == other
