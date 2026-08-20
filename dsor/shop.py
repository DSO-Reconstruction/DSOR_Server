"""The purchase-offer list: message 0x84/0x010E, and its map-server twin 0x85/0x010E.

The client asks for this with 0x8B/0x010B and gets back a list of offers — price
labels, payment methods, validity dates — followed by a small 0x010C. It has nothing
to do with entering the world, which is worth stating because its ordering indices
sit right behind the release and it was briefly mistaken for part of it.

The layout is verified by round-trip on both recorded samples. Every entry begins
with a fixed 105-bit block and then three length-prefixed strings; the middle one is
usually empty and names a payment method when it is not. Nothing here is
byte-aligned: 105 is not a multiple of eight, so the strings drift by a nibble from
one entry to the next, and a byte-aligned search finds roughly half of them.

What is *not* understood is a short trailer after the last entry — 27 bits or so,
plus padding to the byte. It is carried verbatim rather than guessed, which is
enough to re-emit a list with a different number of offers while keeping every other
byte the real service sent.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from raknet.bitstream import BitReader, BitWriter

#: Fixed bits at the start of every entry, before its three strings.
ENTRY_HEAD_BITS = 105

#: Where the price sits inside that head, as a 32-bit little-endian float. Read off
#: the recorded list, which prices its andermant bundles 1.99, 4.99, 9.99, 24.99 and
#: 49.99 in exactly this field — and the client displays those figures.
PRICE_BIT = 32
PRICE_BITS = 32

#: Bits of the head before and after the price, carried verbatim.
HEAD_BEFORE_BITS = PRICE_BIT
HEAD_AFTER_BITS = ENTRY_HEAD_BITS - PRICE_BIT - PRICE_BITS

#: Width of a string's length prefix, as everywhere else in this protocol.
STRING_LENGTH_BITS = 16

#: The opcode, on both the character service (0x84) and a map server (0x85).
OPCODE = 0x010E


@dataclass
class Offer:
    """One purchase offer."""

    #: Undecoded bits of the head before the price.
    head_before: int
    #: What the client charges, in the account's currency.
    price: float
    #: Undecoded bits of the head after the price.
    head_after: int
    #: A product label. The number in it is the *quantity* — 1500 andermants — and
    #: not the price, which is the separate float above.
    product: str
    #: A payment method, or empty. Only one recorded entry carries one.
    method: str
    #: Validity, as text. Every recorded entry carries the same null date.
    valid_from: str


@dataclass
class OfferList:
    """A complete 0x010E message."""

    message_id: int
    offers: list[Offer]
    #: Bits after the last entry, undecoded, with their exact width. Re-emitted as
    #: they arrived; a shorter list keeps the same trailer.
    trailer: int
    trailer_bits: int


def _read_float(reader: BitReader) -> float:
    """A 32-bit float, byte-swapped the same way every other integer here is."""
    raw = reader.read_uint(PRICE_BITS)
    return struct.unpack("<f", raw.to_bytes(4, "little"))[0]


def _write_float(writer: BitWriter, value: float) -> None:
    writer.write_uint(
        int.from_bytes(struct.pack("<f", value), "little"), PRICE_BITS
    )


def decode_offers(payload: bytes) -> OfferList:
    """Decode a 0x010E message, header included."""
    reader = BitReader(payload)
    message_id = reader.read_uint(8)
    opcode = reader.read_uint(16)
    if opcode != OPCODE:
        raise ValueError(f"expected opcode {OPCODE:#06x}, got {opcode:#06x}")

    count = reader.read_uint(32)
    offers: list[Offer] = []
    for index in range(count):
        try:
            offers.append(
                Offer(
                    head_before=reader.read_bits(HEAD_BEFORE_BITS),
                    price=_read_float(reader),
                    head_after=reader.read_bits(HEAD_AFTER_BITS),
                    product=reader.read_string(STRING_LENGTH_BITS),
                    method=reader.read_string(STRING_LENGTH_BITS),
                    valid_from=reader.read_string(STRING_LENGTH_BITS),
                )
            )
        except ValueError as error:
            raise ValueError(
                f"offer list claims {count} entries but entry {index} "
                f"could not be read: {error}"
            ) from error

    remaining = reader.remaining
    return OfferList(
        message_id=message_id,
        offers=offers,
        trailer=reader.read_bits(remaining) if remaining else 0,
        trailer_bits=remaining,
    )


def encode_offers(offers: OfferList) -> bytes:
    """Rebuild a 0x010E message.

    Re-encoding what :func:`decode_offers` produced reproduces the recorded bytes
    exactly. Dropping entries from the list produces a shorter message the client
    accepts, which is how the count field was confirmed to be the count.
    """
    writer = BitWriter()
    writer.write_uint(offers.message_id, 8)
    writer.write_uint(OPCODE, 16)
    writer.write_uint(len(offers.offers), 32)
    for offer in offers.offers:
        writer.write_bits(offer.head_before, HEAD_BEFORE_BITS)
        _write_float(writer, offer.price)
        writer.write_bits(offer.head_after, HEAD_AFTER_BITS)
        writer.write_string(offer.product, STRING_LENGTH_BITS)
        writer.write_string(offer.method, STRING_LENGTH_BITS)
        writer.write_string(offer.valid_from, STRING_LENGTH_BITS)
    writer.write_bits(offers.trailer, offers.trailer_bits)
    return writer.to_bytes()


def keep_first(payload: bytes, count: int) -> bytes:
    """Re-emit *payload* with only its first *count* offers."""
    parsed = decode_offers(payload)
    parsed.offers = parsed.offers[:count]
    return encode_offers(parsed)


def set_price(payload: bytes, price: float) -> bytes:
    """Re-emit *payload* with every offer priced at *price*.

    A second, sharper test of the same kind as :func:`keep_first`: the client either
    displays the figure it was sent or it does not, and the figure it was sent is one
    no real server ever offered.
    """
    parsed = decode_offers(payload)
    for offer in parsed.offers:
        offer.price = price
    return encode_offers(parsed)
