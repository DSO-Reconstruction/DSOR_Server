"""What a client is handed when it enters a map, and what it is no longer handed.

The blob this server called "the player state" was not one. It was the whole batch the
live map server sent -- 466 chained commands in Kingshill's 1,170,448 bytes -- and the
player's own ``NewPlayerCommand`` is the first **1,270** of them. The other 99.9% was
one recorded account: its inventory, its equipment, its 57 achievements, its quest log,
its currencies, and 38 remote players who were standing in that city when the capture
was taken.

So the arrival is cut to that first command, and the three things this server has any
business stating -- who the character is, what they own, what is on their bar -- are
written rather than patched into somebody else's message.
"""

import pytest

from dsor import actionbar, chain, playerstate, recorded, skillbook
from raknet.payload import bits_of

#: Each servable map, the bit its player command ends on, and the character the
#: recording was taken from.
#:
#: 4,594 and 10,882, and the 712 bits between those and the 4,010 / 10,170 this file
#: asserted first are the whole of a bug the operator paid for twice. See
#: test_the_boundary_is_where_a_real_actor_is.
ARRIVALS = (
    ("a0001_start_tutorial_dun", 4594, "Username000", 0x00010015),
    ("a0200_kingscity", 10882, "Username0000000", 0x00022155),
)


@pytest.mark.parametrize("map_name,bits,who,actor", ARRIVALS)
def test_the_trimmed_content_is_one_new_player_command(map_name, bits, who, actor):
    """Ending on the player's own actor and a terminator, with nothing over.

    Checked with :func:`dsor.recorded._real_tail` rather than ``chain.walk``, and that
    is not a preference: ``walk`` accepts an actor in any page up to 0xFF, page 0
    included, so it sees a false tail 712 bits early and reads the rest of the command
    as a second one. An exact segmentation of a truncated message is the worst kind of
    wrong answer -- it looks like proof.
    """
    from raknet.bitstream import BitReader

    content = dict(recorded.map_arrival(map_name))["content"]
    assert bits_of(content) == bits
    assert len(content) == (bits + 7) // 8, "the cut lands mid-byte"
    assert recorded._sixteen(bytes(content), 8) == recorded.NEW_PLAYER
    assert BitReader(content, bits - 8).read_bits(8) == 0xFF
    assert BitReader(content, bits - 40).read_uint(32) == actor, "the player's own"
    assert recorded._real_tail(bytes(content), bits - 40, bits) == actor


def test_the_boundary_is_where_a_real_actor_is():
    """The bug this file first shipped, in one assertion.

    A run of 0xFF bytes inside the player command reads as actor ``0x0000ffff`` -- page
    zero, which is "at most 0xFF" -- with a terminator behind it and a plausible
    command id behind that. So the loose tail test put the command's end 712 bits
    early -- 584 bits early in one recording and 712 in the other -- the arrival went
    out truncated, and the client said so:

        RakNetStream::ReadBits(): error while reading stream!
        DecodeCommand(): Could not decode command (ID: '29', 'NewPlayerCommand')!
        Received PlayerReadyCommand with unknown actor id (65557)!

    No player was created, so every command after it was refused for naming an actor
    the client had never been given. The fix is one condition: an actor is zero, or it
    is in a page this game uses with a low half that is not sixteen set bits.
    """
    for name, early, real, missed in (
        ("zone_content.bin", 4010, 4594, 584),
        ("zone_content_kingscity.bin", 10170, 10882, 712),
    ):
        raw = bytes(recorded.payload(name))
        total = len(raw) * 8
        # The false tail is still there, and still reads as a tail to the loose test.
        assert recorded._thirty_two(raw, early - 40) == 0x0000FFFF
        assert recorded._real_tail(raw, early - 40, total) is None, "refused now"
        assert recorded.first_command_end(raw) == real
        assert real - early == missed


@pytest.mark.parametrize("map_name,bits,who,actor", ARRIVALS)
def test_the_character_still_reads_back_out_of_it(map_name, bits, who, actor):
    """Name, map, level and experience all live inside the player's own command."""
    content = dict(recorded.map_arrival(map_name))["content"]
    got = playerstate.progress_of(content)
    assert got is not None
    assert got["name"] == who
    assert got["map"] == map_name
    assert got["level_at"] < bits and got["experience_at"] < bits


@pytest.mark.parametrize("map_name,bits,who,actor", ARRIVALS)
def test_two_actor_references_instead_of_a_hundred(map_name, bits, who, actor):
    """Which is why the trim comes before the patch and not after.

    Two, and knowing that they are two matters: one field inside the body and one in
    the command's own tail. The first cut kept only the body's, which is another way of
    saying it threw the tail away. The untrimmed Kingshill state holds 124 of them and
    the tutorial's 45, and rewriting all of those was a 37 ms search per login.
    """
    content = dict(recorded.map_arrival(map_name))["content"]
    inside = playerstate.references(bytes(content), actor)
    assert len(inside) == 2, inside
    assert inside[-1] == bits - 40, "the second is the command's tail"
    whole = recorded.payload(str(recorded.ZONES[map_name]["content"]))
    assert len(playerstate.references(bytes(whole), actor)) > 40


@pytest.mark.parametrize("map_name,bits,who,actor", ARRIVALS)
def test_none_of_the_recorded_session_survives(map_name, bits, who, actor):
    """No inventory, no achievements, no quest log, no remote players."""
    content = bytes(dict(recorded.map_arrival(map_name))["content"])
    for command in (0x0054, 0x0021, 0x0114, 0x008C, 0x0080):
        assert bytes([0xFF, command & 0xFF, command >> 8]) not in content
    # And it is small enough that it cannot be hiding any of them.
    assert len(content) < 1500


def test_the_whole_arrival_is_a_kilobyte_and_a_half():
    """Against 1.17 MB, which is the size of the complaint."""
    pieces = dict(recorded.map_arrival("a0200_kingscity"))
    assert set(pieces) == {"ack", "cosmetics", "content"}
    assert sum(len(piece) for piece in pieces.values()) < 3000, pieces
    whole = recorded.payload("zone_content_kingscity.bin")
    assert len(whole) == 1_170_448
    assert len(pieces["content"]) * 800 < len(whole), "three orders of magnitude"


def test_a_wrong_cut_fails_loudly():
    """Rather than shipping a truncated message.

    The constant is an expectation and the forward scan is the answer, so they are
    compared. Checking only that the result walks as one command is not enough: the
    tail search has a byte of slack around an actor, so a cut eight bits either side of
    the right one still admits an exact segmentation and would have passed.
    """
    assert recorded.first_command_end(recorded.payload("zone_content.bin")) == 4594
    assert (
        recorded.first_command_end(recorded.payload("zone_content_kingscity.bin"))
        == 10882
    )
    zone = recorded.ZONES["a0200_kingscity"]
    was = zone["content_bits"]
    for delta in (-8, +8, -200, +2000):
        recorded._checked.cache_clear()
        zone["content_bits"] = was + delta
        try:
            with pytest.raises(ValueError):
                recorded.map_arrival("a0200_kingscity")
        finally:
            zone["content_bits"] = was
            recorded._checked.cache_clear()
    # And the right one still works.
    assert bits_of(dict(recorded.map_arrival("a0200_kingscity"))["content"]) == 10882


# ------------------------------------------------------------ what is written instead


def test_a_built_skill_book_reads_back():
    """The grammar the recording agrees with, field for field.

    An 8-bit count, then per entry a little-endian uint32 and three single bits of
    which the first is ownership. The other two are zero in all eighteen recorded
    entries, so they are written zero.
    """
    wanted = [1838, 1839, 1844, 1846, 1852]
    book = skillbook.encode(wanted, bytes([0x15, 0x00, 0x01, 0x00]))
    assert bits_of(book) == 72 + 35 * len(wanted)
    found, leftover = chain.walk(book, most=4)
    assert leftover == 0
    assert [command.id for command in found] == [skillbook.SKILL_BOOK]
    assert found[0].actor == 0x00010015
    assert [entry.skill for entry in skillbook.entries(book)] == wanted
    assert skillbook.granted(book) == set(wanted)
    # And a book can say a skill is listed but not owned.
    half = skillbook.encode(wanted, bytes([0x15, 0, 1, 0]), owned={1838, 1844})
    assert skillbook.granted(half) == {1838, 1844}


@pytest.mark.parametrize("actor", [b"", bytes(3), bytes(5)])
def test_a_book_refuses_an_actor_that_is_not_four_bytes(actor):
    with pytest.raises(ValueError):
        skillbook.encode([1838], actor)


def test_a_book_refuses_more_entries_than_the_client_counts():
    with pytest.raises(ValueError):
        skillbook.encode([], bytes(4))
    with pytest.raises(ValueError):
        skillbook.encode(list(range(skillbook.MOST_ENTRIES + 1)), bytes(4))


def test_a_built_action_bar_reads_back():
    """Ten records inside one command, which is where they always were.

    ``with_skills`` has been rewriting these records in place for months without
    knowing they sit inside a ``0x0050``: in the recorded batch the skill book's
    terminator is at bit 103,554, the next id at 103,562 is 0x0050, and its body opens
    at 103,578 with a uint32 of 10 -- the number of bars -- immediately followed by the
    records at ``FIRST_BAR``.
    """
    skills = ["angrystrike", "mightybash", "warshout"]
    message = actionbar.encode(skills, bytes([0x15, 0x00, 0x01, 0x00]))
    found, leftover = chain.walk(message, most=4)
    assert leftover == 0
    assert [command.id for command in found] == [actionbar.QUICK_SLOTS_INFO]
    assert found[0].actor == 0x00010015
    bars = actionbar.find_bars(message, start=24 + 32)
    assert len(bars) == actionbar.BARS
    for _start, _end, slots in bars:
        assert slots[: len(skills)] == skills
        assert slots[len(skills):] == [None] * (actionbar.SLOTS - len(skills))


def test_the_bar_refuses_a_bad_actor_or_a_bad_count():
    with pytest.raises(ValueError):
        actionbar.encode(["angrystrike"], bytes(3))
    with pytest.raises(ValueError):
        actionbar.encode(["angrystrike"], bytes(4), bars=0)
    with pytest.raises(ValueError):
        actionbar.encode(["angrystrike"], bytes(4), bars=actionbar.BARS + 1)


def test_the_bar_cannot_fill_all_seventeen_slots():
    """Filling them killed the client inside its own FixedArray."""
    message = actionbar.encode([f"skill{i}" for i in range(40)], bytes(4))
    _start, _end, slots = actionbar.find_bars(message, start=24 + 32)[0]
    assert len([name for name in slots if name]) == actionbar.SLOTS
    # The server never asks for that many: the rule bounds it.
    from dsor.world import Rules

    assert Rules().arrival_bar_slots < actionbar.SLOTS


# ------------------------------------------------------ the patchers keep their length


def test_every_patcher_keeps_the_declared_bit_length():
    """The reason the trim can come first.

    All of them write in place, so none should change the length -- and all of them
    used to return plain ``bytes``, which rounds a sub-byte message up and leaves the
    client reading the spare bits as another command. The roster is the case that was
    already wrong in the tree: 316 bytes declaring 2,525 bits went out as 2,528.
    """
    from dsor import charlist

    roster = recorded.payload("character_list.bin")
    assert bits_of(roster) == 2525
    assert bits_of(charlist.with_progress(roster, 100, 882246499)) == 2525
    assert bits_of(charlist.with_andermant(roster, 9_999_999)) == 2525

    content = dict(recorded.map_arrival("a0200_kingscity"))["content"]
    assert bits_of(playerstate.with_progress(content, 100, 882246499)) == 10882
    assert bits_of(playerstate.with_actor(content, 0x00010042, was=0x00022155)) == 10882

    state = recorded.payload("zone_content.bin")
    assert bits_of(skillbook.with_granted(state, {1838})) == bits_of(state)


def test_the_arrival_ends_with_the_signal_that_makes_the_player_exist():
    """The command the trim cut off, and the whole of the bug it caused.

    ``PlayerReadyCommand`` is the **last** command of the recorded Kingshill batch --
    command 466 of 466 -- and there is nothing to it but the signal: the id, no body at
    all, the actor, the terminator. Trimming the arrival to the leading
    ``NewPlayerCommand`` removed it, and the client was left holding a description of a
    character it never instantiated: "je n'ai pas l'actor qui spawn".
    """
    message = playerstate.ready(bytes([0x55, 0x21, 0x02, 0x00]))
    assert len(message) == 8
    assert message == bytes.fromhex("851f005521020 0ff".replace(" ", ""))
    found, leftover = chain.walk(message, most=4)
    assert leftover == 0
    assert [command.id for command in found] == [playerstate.PLAYER_READY]
    assert found[0].actor == 0x00022155
    # No body: the id, the actor and the terminator account for every bit.
    assert bits_of(message) == 8 + 16 + 32 + 8


@pytest.mark.parametrize("actor", [b"", bytes(3), bytes(5)])
def test_the_ready_signal_refuses_an_actor_that_is_not_four_bytes(actor):
    with pytest.raises(ValueError):
        playerstate.ready(actor)


def test_the_recorded_kingshill_batch_really_does_end_with_it():
    """So this is a measurement and not a plausible-sounding fix.

    Read off the blob: the last 56 bits are the id 0x001F, the actor the state was
    captured for, and 0xFF.
    """
    from raknet.bitstream import BitReader

    whole = recorded.payload("zone_content_kingscity.bin")
    total = bits_of(whole)
    assert BitReader(whole, total - 56).read_uint(16) == playerstate.PLAYER_READY
    assert BitReader(whole, total - 40).read_uint(32) == 0x00022155
    assert BitReader(whole, total - 8).read_bits(8) == 0xFF
