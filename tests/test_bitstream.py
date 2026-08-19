"""The bit-level codec, and the character record that made it necessary.

The record is 437 bytes containing two plain-text character names, and no
byte-aligned search finds either: the first begins at bit 225, the second at bit
1323. Everything here exists because of that.

The bit numbering is worth stating precisely, because getting its sign wrong is
what delayed finding the names. A buffer shifted left by *b* bits puts byte *i* of
the shifted copy at absolute bit ``8*i + b`` of the original — not ``8*i - b``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dsor.character import (
    STRING_LENGTH_BITS,
    decode_character_list,
    find_strings,
)
from raknet.bitstream import BitReader, BitWriter

#: A two-character record, kept here as immutable evidence rather than read from
#: dsor/data/. That directory holds operational scaffolding which is re-extracted
#: whenever a better capture turns up, and these tests assert measurements — bit
#: 225, bit 1323 — that belong to this specific record. Pointing them at a moving
#: file made four of them fail the moment the scaffolding was replaced.
RECORD = Path(__file__).parent / "fixtures" / "character_record_two.bin"


# ── the codec ───────────────────────────────────────────────────────────────


def test_bits_are_read_most_significant_first():
    reader = BitReader(bytes([0b1010_0000]))
    assert reader.read_bits(3) == 0b101
    assert reader.position == 3


def test_a_single_bit_shifts_everything_after_it():
    """The whole reason this module exists: one boolean displaces every field
    behind it, so nothing downstream stays byte-aligned."""
    writer = BitWriter()
    writer.write_bool(True)
    writer.write_bytes(b"abc")
    packed = writer.to_bytes()
    assert packed[:3] != b"abc", "the text must no longer be byte-aligned"

    reader = BitReader(packed)
    assert reader.read_bool() is True
    assert reader.read_bytes(3) == b"abc"


def test_multi_byte_integers_are_little_endian_by_default():
    """Bytes are ordered little-endian while the bits inside each stay MSB-first,
    so the two conventions coexist and cannot be read as one long bit run."""
    reader = BitReader(bytes([0x0B, 0x00]))
    assert reader.read_uint(16) == 11
    reader.seek(0)
    assert reader.read_uint(16, little_endian=False) == 0x0B00


def test_widths_that_are_not_whole_bytes_have_no_byte_order():
    reader = BitReader(bytes([0b1111_0000]))
    assert reader.read_uint(4) == 0b1111


def test_writer_round_trips_every_field_kind():
    writer = BitWriter()
    writer.write_bool(False)
    writer.write_bits(0b101, 3)
    writer.write_uint(4242, 16)
    writer.write_string("jeangustavo")
    writer.write_bool(True)

    reader = BitReader(writer.to_bytes())
    assert reader.read_bool() is False
    assert reader.read_bits(3) == 0b101
    assert reader.read_uint(16) == 4242
    assert reader.read_string() == "jeangustavo"
    assert reader.read_bool() is True


def test_reading_past_the_end_is_refused():
    with pytest.raises(ValueError, match="only .* left"):
        BitReader(bytes(1)).read_bits(9)


def test_writing_a_value_too_wide_is_refused():
    with pytest.raises(ValueError, match="does not fit"):
        BitWriter().write_bits(256, 8)


def test_peek_does_not_advance():
    reader = BitReader(bytes([0xFF]))
    assert reader.peek_bits(4) == 0b1111
    assert reader.position == 0


def test_padding_means_a_bit_length_cannot_be_recovered_from_bytes():
    """Why frames carry a length in bits: three bits pad out to a whole byte, and
    nothing in the bytes says how many were real."""
    writer = BitWriter()
    writer.write_bits(0b101, 3)
    assert len(writer) == 3
    assert len(writer.to_bytes()) == 1


# ── the record it was written for ────────────────────────────────────────────


@pytest.fixture(scope="module")
def record() -> bytes:
    if not RECORD.exists():  # pragma: no cover
        pytest.skip(f"character record fixture missing: {RECORD}")
    # Skip the message id and its 16-bit opcode.
    return RECORD.read_bytes()[3:]


def test_the_names_sit_at_known_bit_positions(record):
    """The measurement that anchors everything else. Both are read back directly,
    so a change in bit numbering breaks this test rather than going unnoticed."""
    reader = BitReader(record)
    reader.seek(225)
    assert reader.read_bytes(11) == b"jeangustavo"
    reader.seek(1323)
    assert reader.read_bytes(7) == b"dqsdqsd"


def test_a_name_is_preceded_by_its_length_as_a_16_bit_prefix(record):
    """Strings use the same convention as the byte-aligned ones elsewhere in the
    protocol; only the alignment differs."""
    reader = BitReader(record)
    reader.seek(225 - STRING_LENGTH_BITS)
    assert reader.read_uint(STRING_LENGTH_BITS) == len("jeangustavo")


def test_byte_aligned_search_finds_nothing(record):
    """States the problem, so the reason for this module cannot be forgotten."""
    assert b"jeangustavo" not in record
    assert b"dqsdqsd" not in record


def test_the_structured_scan_finds_exactly_the_expected_strings(record):
    """Cross-check: a principled scan agrees with the brute-force bit-shift search
    that first revealed the names."""
    found = [s.value for s in find_strings(record)]
    assert found == [
        "jeangustavo",
        "a0006_grimford_hub",
        "dqsdqsd",
        "a0001_start_tutorial_dun",
        "mage_base_rh_staff_speedAttack_damage",
        "mage_base_lh_orb_armor_healthpoints_resistanceAll",
    ]


def test_each_character_is_paired_with_its_last_map(record):
    """The pairing that explains the symptom: serving one character's map while
    the client selected the other places the player somewhere unrelated."""
    entries = decode_character_list(record)
    assert [(e.name, e.last_map) for e in entries] == [
        ("jeangustavo", "a0006_grimford_hub"),
        ("dqsdqsd", "a0001_start_tutorial_dun"),
    ]


def test_map_identifiers_are_told_apart_from_names(record):
    strings = {s.value: s.looks_like_a_map for s in find_strings(record)}
    assert strings["a0006_grimford_hub"] is True
    assert strings["a0001_start_tutorial_dun"] is True
    assert strings["jeangustavo"] is False
    assert strings["dqsdqsd"] is False
