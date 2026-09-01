"""The level the character selection screen shows.

Saving a character server-side does nothing for this on its own, and that is the whole
point of the module: the screen is drawn from a replayed recording whose character is
level 1, so every login showed level 1 however much had been stored. "tu sauvegardes
rien vu que tu rejoues une connexion au debut donc ca remet tjrs niveau 1 meme dans
l'ecran de perso."
"""

import pathlib

from dsor import charlist

ROSTER = pathlib.Path(__file__).resolve().parent.parent / "dsor/data/character_list.bin"


def roster() -> bytes:
    return ROSTER.read_bytes()


def test_the_recorded_roster_is_level_one():
    assert charlist.level_of(roster()) == 1


def test_writing_the_level_changes_one_byte_and_nothing_else():
    raw = roster()
    out = charlist.with_level(raw, 104)
    assert charlist.level_of(out) == 104
    assert len(out) == len(raw), "sixteen bits in place, so nothing moves"
    assert [i for i in range(len(raw)) if raw[i] != out[i]] == [charlist.LEVEL_AT // 8]


def test_the_level_is_clamped():
    for asked, want in ((0, 1), (-3, 1), (9999, charlist.HIGHEST_LEVEL)):
        assert charlist.level_of(charlist.with_level(roster(), asked)) == want


def test_writing_the_level_it_already_has_changes_nothing():
    raw = roster()
    assert charlist.with_level(raw, 1) == raw


def test_a_message_of_another_shape_is_left_alone():
    """The guard that matters. A patcher writing into the wrong place on a slightly
    different recording would corrupt the roster silently, which is worse than a screen
    showing the wrong number."""
    for other in (b"", b"\x00" * 400, roster()[:40], roster()[::-1]):
        assert charlist.level_of(other) is None
        assert charlist.with_level(other, 50) == other


def test_the_name_and_the_map_are_where_the_layout_says():
    """The check the guard performs, spelled out: the name's length prefix at bit 233
    and the name itself right after. If those move, so has everything else."""
    from raknet.bitstream import BitReader

    reader = BitReader(roster(), charlist.NAME_AT)
    length = reader.read_uint(16)
    assert length == len(charlist.RECORDED_NAME)
    name = bytes(reader.read_uint(8) for _ in range(length)).decode("ascii")
    assert name == charlist.RECORDED_NAME
