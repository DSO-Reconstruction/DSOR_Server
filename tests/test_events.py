"""The global event schedule, 0x84/0x00DD — the largest message in the protocol.

733,774 bytes, 596 fragments on the wire, and for a long time entirely opaque. Its
size and its position right behind the character roster made it look like account
state: inventory, quests, progression. It is none of those. It is a flat list of
8,233 dated event-schedule entries, identical for every player.

Two consequences worth stating, because both redirect work elsewhere. The client's
missing character appearance is not in here, so that search moves on. And nothing in
here is private — every string is a promotion or event key, not account data.

The round-trip test is the one that matters. For a format this large, any misread
field shows up as a shifted bit somewhere in 5.87 million, so reproducing the file
exactly is a far stronger statement than any field-by-field assertion.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from dsor.events import (
    MESSAGE_ID,
    OPCODE,
    decode_schedule,
    encode_schedule,
)

RECORDED = Path(__file__).parent.parent / "dsor" / "data" / "character_enter_bulk.bin"


@pytest.fixture(scope="module")
def raw() -> bytes:
    if not RECORDED.exists():  # pragma: no cover
        pytest.skip(f"recorded schedule missing: {RECORDED}")
    return RECORDED.read_bytes()


@pytest.fixture(scope="module")
def entries(raw: bytes):
    return decode_schedule(raw)


# ── the test the layout stands on ────────────────────────────────────────────


def test_the_recorded_message_round_trips_byte_for_byte(raw, entries):
    """Decode then re-encode reproduces all 733,774 bytes.

    This is what makes the layout a finding rather than a reading. The model
    accounts for 5,870,188 of the file's 5,870,192 bits; the remaining four are
    zero padding to the byte, which is why frames carry a length in bits and not
    in bytes.
    """
    assert encode_schedule(entries) == raw


def test_the_header_count_agrees_with_what_the_body_holds(entries):
    """A count that disagreed with the body would mean the record stride is wrong
    and the decode merely survived it."""
    assert len(entries) == 8233


# ── what the entries are ─────────────────────────────────────────────────────


def test_every_date_is_a_real_calendar_date(entries):
    """Six 32-bit integers rather than a packed timestamp, and they are genuine
    dates: every dated entry parses, with no 31 February among them."""
    dated = [entry for entry in entries if entry.has_date]
    assert len(dated) == 8212, "21 entries carry a year of zero"
    for entry in dated:
        year, month, day, hour, minute, second = entry.date
        datetime.datetime(year, month, day, hour, minute, second)


def test_entries_are_ordered_by_identifier_and_not_by_date(entries):
    """Refutes the natural assumption. The ids increase across every consecutive
    pair; the dates go backwards 501 times, so nothing may rely on time order.
    """
    ids = [entry.identifier for entry in entries]
    assert all(a < b for a, b in zip(ids, ids[1:])), "ids strictly increase"

    dates = [entry.date for entry in entries if entry.has_date]
    descents = sum(1 for a, b in zip(dates, dates[1:]) if b < a)
    assert descents == 501, "the dates are not sorted, and by a wide margin"


def test_the_two_entries_with_parameters_are_the_two_longer_records(entries):
    """The 32-bit field after the date is a parameter count, not more date.

    It reads 1 in exactly two entries, and those two are exactly the two records
    that are 32 bits longer than every other. A perfect correlation on 2 of 2 is
    thin on its own, but the alternative readings — milliseconds, day of week — do
    not explain a change in record length.
    """
    with_parameters = [entry for entry in entries if entry.parameters]
    assert len(with_parameters) == 2
    assert all(len(entry.parameters) == 1 for entry in with_parameters)
    assert {entry.key for entry in with_parameters} == {
        "pw_difficulty_unlock",
        "scaling_difficulty_unlock",
    }


def test_the_message_carries_no_character_or_appearance_data(entries):
    """Why this message is no longer a suspect for the missing hair and gear.

    Compare the character roster, which does contain these shapes: a map identifier
    'a0001_start_tutorial_dun' and an item 'warrior_base_rh_sword_...'. Nothing of
    that kind is here.
    """
    keys = {entry.key for entry in entries}
    for word in (
        "hair", "face", "head", "body", "gender", "beard", "eye",
        "wardrobe", "avatar", "armor", "weapon", "helm",
    ):
        assert not any(word in key for key in keys), f"unexpected {word!r} key"

    # No map identifiers either: those look like a letter, four digits, a name.
    assert not any(
        key[0].isalpha() and key[1:5].isdigit() and "_" in key for key in keys
    ), "a map identifier would mean this is not purely a schedule"


def test_the_keys_that_do_mention_cosmetics_are_shop_categories(entries):
    """There *are* cosmetic-sounding keys, and they are promotions rather than
    anything the player is wearing — which is exactly the trap this test exists to
    mark, since a keyword search for 'costume' finds them.
    """
    keys = {entry.key for entry in entries}
    cosmetic = {key for key in keys if key.startswith("highlight_")}
    assert cosmetic, "the shop highlight keys are expected to be present"
    # Most are dated, but not all: 21 entries in the message carry a year of zero,
    # and some of those are highlights. Asserting every one had a date was wrong.
    assert any(entry.has_date for entry in entries if entry.key in cosmetic)


# ── refusing what it should refuse ───────────────────────────────────────────


def test_another_message_is_refused(entries):
    with pytest.raises(ValueError, match="expected 0x84/0x00dd"):
        decode_schedule(bytes([0x84, 0x87, 0x00]) + bytes(64))


def test_a_truncated_transfer_is_refused_rather_than_half_served(raw):
    """596 fragments arrive over seconds; serving half a schedule silently would
    be worse than failing."""
    with pytest.raises(ValueError, match="could not be read"):
        decode_schedule(raw[: len(raw) // 2])
