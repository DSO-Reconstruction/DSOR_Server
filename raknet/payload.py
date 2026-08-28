"""A payload that remembers its own length in bits.

A frame's length field is in *bits*, and the client's reader uses it as the
boundary of the message. RakNet's BitStream packs booleans into single bits, so a
message's true length is usually **not** a multiple of eight -- and the bytes on
the wire are padded up to the next byte without the length following them.

Rounding the declared length up to the byte is therefore not harmless. Measured
against the live service on the same map, its 0x004F frames declare a length
shorter than ``len(payload) * 8`` in 90 of 91 cases -- by one to seven bits -- and
its 0x006b frames in 98 of 98. This server declared ``len * 8`` for 100% of every
command it sent. The spare bits sit at the end of the frame, and a multi-command
payload is decoded command after command until the stream is exhausted, so the
client reads them as the start of one more command and reports

    DecodeCommand() invalid command ending in multi command 79!

which is 0x4F: the status effect command, arriving with a tail the client cannot
account for.

This subclasses ``bytes`` on purpose. Every caller that treats a payload as bytes
-- ``len``, slicing, comparison, writing it out -- keeps working unchanged, and only
the frame builder has to notice the extra attribute.
"""

from __future__ import annotations


class Payload(bytes):
    """*data*, declaring *bits* as its length rather than ``len(data) * 8``."""

    # No __slots__: bytes is a variable-length built-in and CPython refuses a
    # non-empty __slots__ on a subtype of it.
    def __new__(cls, data: bytes, bits: int | None = None) -> "Payload":
        out = super().__new__(cls, data)
        if bits is None:
            bits = len(out) * 8
        if bits < 0 or (bits + 7) // 8 != len(out):
            raise ValueError(
                f"{bits} bits does not describe a {len(out)}-byte payload"
            )
        out.bits = bits
        return out

    def __repr__(self) -> str:
        return f"Payload({bytes(self)!r}, {self.bits})"


def bits_of(payload: bytes) -> int:
    """The declared bit length of *payload*, whatever kind of bytes it is."""
    return getattr(payload, "bits", len(payload) * 8)


def respan(original: bytes, spliced: bytes) -> Payload:
    """*spliced*, keeping *original*'s bit length adjusted for the size change.

    Slicing or concatenating a :class:`Payload` gives plain ``bytes`` back, so every
    function that rewrites a field inside a replayed message loses the length the
    message had on the wire. Adjusting by the byte delta is exact for the splices this
    server does: they rewrite fixed-width fields, or a length-prefixed string, and
    both move the tail by whole bytes.
    """
    return Payload(spliced, bits_of(original) + 8 * (len(spliced) - len(original)))
