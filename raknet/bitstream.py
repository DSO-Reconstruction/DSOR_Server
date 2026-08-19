"""Bit-level reading and writing, because this protocol is not byte-aligned.

RakNet's ``BitStream`` packs a boolean into one bit and has compressed forms for
integers, so a field can start anywhere. The consequence is concrete: a character
record 437 bytes long contained a plain-text name and no byte-aligned search would
find it. Shifting the whole buffer one bit to the left made ``jeangustavo`` appear
immediately, and shifting three bits made a second character appear — each
variable-length field displacing everything after it.

That is why this module exists. Without it a field can only be recognised when it
happens to land on a byte boundary, which is how an earlier pass through the
movement message found three coordinates and missed everything else.

Bit order is **most significant first** within each byte, matching what the
shifting experiment established: text found after a one-bit left shift means it
begins at absolute bit 223 counting MSB-first from byte zero.
"""

from __future__ import annotations


class BitReader:
    """Reads fields of arbitrary bit width from a buffer.

    Positions are absolute bit offsets, so a field discovered by searching can be
    read directly without recomputing byte boundaries.
    """

    def __init__(self, data: bytes, position: int = 0) -> None:
        self._data = data
        self.position = position

    @property
    def length(self) -> int:
        """Total size in bits."""
        return len(self._data) * 8

    @property
    def remaining(self) -> int:
        return self.length - self.position

    def seek(self, position: int) -> None:
        if not 0 <= position <= self.length:
            raise ValueError(f"bit {position} is outside a {self.length}-bit buffer")
        self.position = position

    def read_bits(self, count: int) -> int:
        """Read *count* bits as an unsigned integer, most significant first."""
        if count < 0:
            raise ValueError(f"cannot read {count} bits")
        if count > self.remaining:
            raise ValueError(
                f"need {count} bits at position {self.position}, "
                f"only {self.remaining} left"
            )
        value = 0
        for _ in range(count):
            byte = self._data[self.position >> 3]
            bit = (byte >> (7 - (self.position & 7))) & 1
            value = (value << 1) | bit
            self.position += 1
        return value

    def read_bool(self) -> bool:
        """One bit. The reason nothing in this protocol stays byte-aligned."""
        return self.read_bits(1) == 1

    def read_uint(self, bits: int, little_endian: bool = True) -> int:
        """Read an integer of *bits* bits, byte-swapped if it is little-endian.

        The wire is little-endian for multi-byte integers, but the *bits* within
        each byte still come most significant first, so the bytes have to be read
        individually and reordered rather than read as one long bit run.
        """
        if bits % 8:
            # A width that is not a whole number of bytes has no byte order.
            return self.read_bits(bits)
        raw = [self.read_bits(8) for _ in range(bits // 8)]
        if little_endian:
            raw.reverse()
        value = 0
        for byte in raw:
            value = (value << 8) | byte
        return value

    def read_bytes(self, count: int) -> bytes:
        """Read *count* whole bytes from the current bit position."""
        return bytes(self.read_bits(8) for _ in range(count))

    def read_string(self, length_bits: int = 16) -> str:
        """Read a length-prefixed string: a length, then that many bytes.

        The prefix width is a parameter because it is not yet established for every
        message; 16 bits matches the byte-aligned strings elsewhere in the game.
        """
        length = self.read_uint(length_bits)
        raw = self.read_bytes(length)
        return raw.decode("utf-8", errors="replace")

    def peek_bits(self, count: int) -> int:
        """Read without advancing, for probing a layout."""
        saved = self.position
        try:
            return self.read_bits(count)
        finally:
            self.position = saved


class BitWriter:
    """Builds a buffer field by field, mirroring :class:`BitReader`.

    Needed for the same reason: a message this server has to *produce* cannot be
    assembled from bytes if its fields are not byte-sized.
    """

    def __init__(self) -> None:
        self._bits: list[int] = []

    def __len__(self) -> int:
        return len(self._bits)

    def write_bits(self, value: int, count: int) -> None:
        if count < 0:
            raise ValueError(f"cannot write {count} bits")
        if value < 0:
            raise ValueError("only unsigned values; convert before writing")
        if count and value >> count:
            raise ValueError(f"{value} does not fit in {count} bits")
        for shift in range(count - 1, -1, -1):
            self._bits.append((value >> shift) & 1)

    def write_bool(self, value: bool) -> None:
        self.write_bits(1 if value else 0, 1)

    def write_uint(self, value: int, bits: int, little_endian: bool = True) -> None:
        if bits % 8:
            self.write_bits(value, bits)
            return
        raw = value.to_bytes(bits // 8, "little" if little_endian else "big")
        for byte in raw:
            self.write_bits(byte, 8)

    def write_bytes(self, data: bytes) -> None:
        for byte in data:
            self.write_bits(byte, 8)

    def write_string(self, text: str, length_bits: int = 16) -> None:
        raw = text.encode("utf-8")
        self.write_uint(len(raw), length_bits)
        self.write_bytes(raw)

    def to_bytes(self) -> bytes:
        """Pad the final byte with zeros and return the buffer.

        Padding is unavoidable — a buffer is bytes — so a message whose bit length
        is not a multiple of eight cannot be reproduced from bytes alone. That is
        why frames carry a length in *bits*.
        """
        padded = self._bits + [0] * (-len(self._bits) % 8)
        out = bytearray(len(padded) // 8)
        for index, bit in enumerate(padded):
            if bit:
                out[index >> 3] |= 1 << (7 - (index & 7))
        return bytes(out)
