"""The purchase-offer list, 0x84/0x010E and its map-server twin 0x85/0x010E.

The client asks with 0x8B/0x010B and gets a list of price labels, payment methods
and validity dates. Its ordering indices sit right behind the release, which is why
it was briefly mistaken for part of it; it is an answer to a request, and nothing to
do with entering the world.

The round-trip test is what makes the layout a finding. The trimming test is what
makes it useful: re-emitting a message with a different number of entries is the
cheapest proof that a format is understood rather than replayed, because the client
then has to accept bytes no real server ever sent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dsor.shop import (
    ENTRY_HEAD_BITS,
    OPCODE,
    decode_offers,
    encode_offers,
    keep_first,
    set_price,
)

DATA = Path(__file__).parent.parent / "dsor" / "data"
SAMPLES = ("release_010e_a.bin", "release_010e_b.bin")


@pytest.fixture(params=SAMPLES)
def sample(request) -> bytes:
    path = DATA / request.param
    if not path.exists():  # pragma: no cover
        pytest.skip(f"recorded offer list missing: {path}")
    return path.read_bytes()


def test_both_samples_round_trip_byte_for_byte(sample):
    """Any misread field would show up as a shifted bit, so this is the whole
    argument for the layout."""
    assert encode_offers(decode_offers(sample)) == sample


def test_the_two_samples_hold_different_numbers_of_offers():
    """Which is what identifies the 32-bit field after the opcode as a count."""
    counts = [len(decode_offers((DATA / name).read_bytes()).offers) for name in SAMPLES]
    assert counts == [23, 30]


def test_the_middle_string_is_a_payment_method_and_usually_empty(sample):
    """Three strings per entry, and the middle one is empty in all but one."""
    offers = decode_offers(sample).offers
    named = [offer.method for offer in offers if offer.method]
    assert named == ["sms"], "exactly one recorded entry names a method"


def test_the_entry_head_is_not_a_whole_number_of_bytes():
    """Why a byte-aligned search finds only about half the strings: 105 bits of
    head per entry means every second one starts on a nibble boundary."""
    assert ENTRY_HEAD_BITS % 8 != 0


def test_a_trailer_is_carried_rather_than_guessed(sample):
    """The bits after the last entry are not decoded. Preserving them verbatim is
    what lets a shorter list keep every other byte the real service sent."""
    parsed = decode_offers(sample)
    assert 0 < parsed.trailer_bits < 40, "short, and not byte-aligned"


def test_trimming_produces_a_shorter_message_that_still_parses(sample):
    """The point of the exercise: a list the real service never sent."""
    trimmed = keep_first(sample, 1)
    assert len(trimmed) < len(sample) // 10

    reparsed = decode_offers(trimmed)
    assert len(reparsed.offers) == 1
    assert reparsed.offers[0] == decode_offers(sample).offers[0]
    assert int.from_bytes(trimmed[1:3], "little") == OPCODE


def test_trimming_to_more_than_there_are_keeps_them_all(sample):
    assert keep_first(sample, 1000) == sample


def test_another_opcode_is_refused():
    with pytest.raises(ValueError, match="expected opcode"):
        decode_offers(bytes([0x84, 0x87, 0x00]) + bytes(64))


def test_the_price_is_a_float_in_the_entry_head_not_the_product_label(sample):
    """The number in the label is the quantity, not the price.

    '1_realCurrency_1500.0000_0_NONE' is 1500 andermants, and what the client
    charges for it sits in a separate 32-bit float in the entry's leading block.
    Reading the label as a price would have been the obvious mistake.
    """
    prices = [round(offer.price, 2) for offer in decode_offers(sample).offers[:6]]
    assert prices == [1.99, 2.0, 4.99, 9.99, 24.99, 49.99]


def test_the_price_can_be_rewritten(sample):
    """The sharpest available proof that this format is generated rather than
    replayed: the client is sent a figure no real server ever offered."""
    free = set_price(sample, 0.0)
    assert all(offer.price == 0.0 for offer in decode_offers(free).offers)
    assert len(free) == len(sample), "only a field changed, not the layout"

    # Everything else survives untouched.
    original = decode_offers(sample)
    rewritten = decode_offers(free)
    assert [o.product for o in rewritten.offers] == [o.product for o in original.offers]
    assert rewritten.trailer == original.trailer
