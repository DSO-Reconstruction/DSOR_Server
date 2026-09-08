"""The selection screen, built rather than replayed.

Until this existed, every login showed the recorded character: both clients were
Username because the roster was a captured blob with a level patched into it.
Building one is what puts the account's own characters on the screen.

The trap the whole module is arranged around: **the roster's strings are bit-offset-1
packed**, which is why every offset in dsor/charlist.py is a bit offset counted from the
end of the inline map string, and why the proof below is byte identity against the
recording rather than a field-by-field read.
"""

from __future__ import annotations

import pytest

from dsor import charlist
from dsor.recorded import payload
from raknet.bitstream import BitReader
from raknet.payload import bits_of

#: The recording's own ids, so a rebuild of it can put back what it took out.
RECORDED_CHARACTER = 0x06AB3F8E
RECORDED_ACCOUNT = 0x06AF7532


@pytest.fixture
def recorded():
    return payload("character_list.bin")


def test_the_recording_rebuilds_to_its_own_bytes(recorded):
    """The one test that says the builder writes what it means to and nothing else.

    Anything invented -- a field written where one should be replayed, a string packed a
    bit out -- shows up here as a differing byte.

    *slots* is passed because the template is inconsistent with itself: its header
    declares four characters and one entry walks out of it, because the capture cut the
    message at 316 bytes. Everything else it says about itself, it says correctly.
    """
    was = charlist.entries(recorded)[0]
    same = charlist.build(
        recorded,
        [dict(was, character=RECORDED_CHARACTER)],
        RECORDED_ACCOUNT,
        andermant=was["andermant"],
        slots=4,
    )
    assert bytes(same) == bytes(recorded)
    assert bits_of(same) == bits_of(recorded)


def test_the_count_is_the_number_of_entries(recorded):
    """Measured on the live roster: 4 in the field, four entries that all walk.

    Replaying the template's 4 instead sent a one-character roster declaring four, and
    the client died reading the three that were not there::

        *** NEBULA ASSERTION ***  expression: id.IsValid()
        UI::Element::FindChildElement(const Util::StringAtom &)
    """
    for count in (1, 2, 3):
        built = charlist.build(
            recorded,
            [
                {"name": f"P{n}", "map": "a0200_kingscity", "level": 1, "experience": 0}
                for n in range(count)
            ],
            4242,
        )
        assert BitReader(bytes(built), charlist.COUNT_AT).read_uint(32) == count
        assert len(charlist.entries(built)) == count


def test_each_entry_carries_its_own_character_id(recorded):
    """The id is on the wire twice, and writing only the header was the bug.

    The live four-entry roster holds each entry's own id at IDENTITY_REL and the first
    of them in the header:

        Username 111383506, Username 111396827,
        Username 111423903, Username 111560171
    """
    wanted = [111886223, 111886224]
    built = charlist.build(
        recorded,
        [
            {"name": "Username", "map": "a0200_kingscity", "level": 1,
             "experience": 0, "character": wanted[0]},
            {"name": "Naine", "map": "a0200_kingscity", "level": 7,
             "experience": 42, "character": wanted[1]},
        ],
        4242,
    )
    blob = bytes(built)
    got = [
        BitReader(
            blob, entry["level_at"] - charlist.LEVEL_REL + charlist.IDENTITY_REL
        ).read_uint(32)
        for entry in charlist.entries(built)
    ]
    assert got == wanted
    # And the header holds the first entry's, as the live roster does.
    assert BitReader(blob, charlist.CHARACTER_AT).read_uint(32) == wanted[0]


def test_a_built_roster_reads_back_through_the_decoder(recorded):
    built = charlist.build(
        recorded,
        [
            {
                "name": "Balrog",
                "map": "a0200_kingscity",
                "level": 42,
                "experience": 12345,
                "character": 1000001,
            }
        ],
        4242,
        andermant=9999999,
    )
    (only,) = charlist.entries(built)
    assert only["name"] == "Balrog"
    assert only["map"] == "a0200_kingscity"
    assert only["level"] == 42
    assert only["experience"] == 12345
    assert only["andermant"] == 9999999


def test_two_characters_make_two_entries(recorded):
    built = charlist.build(
        recorded,
        [
            {"name": "Balrog", "map": "a0200_kingscity", "level": 42, "experience": 1},
            {"name": "Naine", "map": "a0001_start_tutorial_dun", "level": 7, "experience": 2},
        ],
        4242,
    )
    read = charlist.entries(built)
    assert [(e["name"], e["map"], e["level"]) for e in read] == [
        ("Balrog", "a0200_kingscity", 42),
        ("Naine", "a0001_start_tutorial_dun", 7),
    ]


def test_a_name_of_a_different_length_moves_everything_behind_it(recorded):
    """Length-prefixed strings, so the message changes size -- and must still walk."""
    short = charlist.build(
        recorded, [{"name": "Yx", "map": "a0200_kingscity", "level": 1, "experience": 0}], 1
    )
    long = charlist.build(
        recorded,
        [{"name": "Y" * 24, "map": "a0200_kingscity", "level": 1, "experience": 0}],
        1,
    )
    assert bits_of(long) - bits_of(short) == (24 - 2) * 8
    assert charlist.entries(short)[0]["name"] == "Yx"
    assert charlist.entries(long)[0]["name"] == "Y" * 24


def test_the_header_carries_the_account_and_the_chosen_character(recorded):
    built = charlist.build(
        recorded,
        [{"name": "Balrog", "map": "a0200_kingscity", "level": 3, "experience": 0,
          "character": 1000001}],
        4242,
    )
    blob = bytes(built)
    assert int.from_bytes(blob[5:9], "little") == 1000001
    assert int.from_bytes(blob[9:13], "little") == 4242
    assert blob[:3] == bytes(recorded)[:3]  # the container and the command id


def test_the_count_can_be_forced_apart_from_the_entries(recorded):
    """Only useful for rebuilding the template, which disagrees with itself."""
    one = {"name": "A", "map": "m", "level": 1, "experience": 0}
    assert bytes(charlist.build(recorded, [one], 1))[13] == 1
    assert bytes(charlist.build(recorded, [one], 1, slots=4))[13] == 4


def test_a_roster_with_nobody_in_it_is_refused(recorded):
    """A client handed an empty roster has nothing to click."""
    with pytest.raises(ValueError, match="at least one character"):
        charlist.build(recorded, [], 1)


def test_the_andermant_is_left_alone_when_none_is_given(recorded):
    was = charlist.entries(recorded)[0]["andermant"]
    built = charlist.build(
        recorded, [{"name": "A", "map": "m", "level": 1, "experience": 0}], 1
    )
    assert charlist.entries(built)[0]["andermant"] == was
