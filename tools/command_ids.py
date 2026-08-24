#!/usr/bin/env python3
"""Recover a command's wire id from the client binary, by name.

Nebula3 registers every class with an Rtti object that holds the class name and,
for commands, a numeric id exposed through the vtable. The chain is mechanical:

    "Commands::HitCommand"          the name literal, in .rdata
      -> the Rtti object            an 8-byte pointer to that literal
      -> the GetRtti thunk          48 8D 05 <disp32> C3  (lea rax,[rip+rtti]; ret)
      -> the vtable                 an 8-byte pointer to that thunk
      -> slot 3                     B8 <imm32> C3         (mov eax, id; ret)

Nothing here is guessed: each step is a byte pattern that either matches or does
not, and a name whose chain breaks is reported as unresolved rather than given a
plausible id.
"""

from __future__ import annotations

import re
import struct
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Section:
    name: str
    virtual_address: int
    virtual_size: int
    raw_offset: int
    raw_size: int


class Image:
    def __init__(self, data: bytes) -> None:
        self.data = data
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe : pe + 4] != b"PE\0\0":
            raise ValueError("not a PE image")
        sections = struct.unpack_from("<H", data, pe + 6)[0]
        opt_size = struct.unpack_from("<H", data, pe + 20)[0]
        self.base = struct.unpack_from("<Q", data, pe + 24 + 24)[0]
        table = pe + 24 + opt_size
        self.sections = []
        for i in range(sections):
            raw = data[table + 40 * i : table + 40 * (i + 1)]
            name = raw[:8].rstrip(b"\0").decode("ascii", "replace")
            vsize, va, rsize, roff = struct.unpack_from("<IIII", raw, 8)
            self.sections.append(Section(name, va, vsize, roff, rsize))

    def to_va(self, offset: int) -> int | None:
        for s in self.sections:
            if s.raw_offset <= offset < s.raw_offset + s.raw_size:
                return self.base + s.virtual_address + (offset - s.raw_offset)
        return None

    def to_offset(self, va: int) -> int | None:
        rva = va - self.base
        for s in self.sections:
            if s.virtual_address <= rva < s.virtual_address + s.virtual_size:
                off = s.raw_offset + (rva - s.virtual_address)
                if off < s.raw_offset + s.raw_size:
                    return off
        return None


RIP_LEA = re.compile(rb"\x48\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d](....)", re.DOTALL)
GET_RTTI = re.compile(rb"\x48\x8d\x05(....)\xc3", re.DOTALL)
ID_GETTER_SLOT = 3


class Resolver:
    """The name -> Rtti -> thunk -> vtable -> id chain, indexed once."""

    def __init__(self, image: Image) -> None:
        self.image = image
        self.leas: dict[int, list[int]] = {}
        self.lea_at: dict[int, int] = {}
        for m in RIP_LEA.finditer(image.data):
            va = image.to_va(m.start())
            if va is None:
                continue
            target = va + 7 + struct.unpack("<i", m.group(1))[0]
            self.leas.setdefault(target, []).append(va)
            self.lea_at[va] = target
        self.thunks: dict[int, list[int]] = {}
        for m in GET_RTTI.finditer(image.data):
            va = image.to_va(m.start())
            if va is None:
                continue
            self.thunks.setdefault(
                va + 7 + struct.unpack("<i", m.group(1))[0], []
            ).append(va)
        self._index_pointers()

    def command_id(self, name: str) -> int | None:
        """The wire id of *name*, or None if the chain does not resolve.

        Three spellings are tried, because the Rtti name is not always the C++ one:
        the name as given, the bare class name qualified with ``Commands::``, and
        the bare name alone. The client's skill commands register as plain
        ``SkillCommand`` with no namespace, which is why enumerating ``Commands::``
        missed them entirely.
        """
        bare = name.rsplit("::", 1)[-1]
        for spelling in dict.fromkeys((name, "Commands::" + bare, bare)):
            found = self._resolve(spelling)
            if found is not None:
                return found
        return None

    def _resolve(self, qualified: str) -> int | None:
        literal = qualified.encode() + b"\0"
        for m in re.finditer(re.escape(literal), self.image.data):
            va = self.image.to_va(m.start())
            if va is None:
                continue
            # The Rtti object is the operand of the very next lea: the constructor
            # loads the name, then `this`. Confirmed against HitCommand, whose id
            # this route reproduces as 0x6B.
            for site in self.leas.get(va, []):
                # Usually the very next lea loads `this`; occasionally the
                # constructor emits something between the two, so a short window is
                # searched rather than one fixed offset.
                for delta in range(1, 32):
                    rtti = self.lea_at.get(site + delta)
                    if rtti is None:
                        continue
                    for thunk in self.thunks.get(rtti, []):
                        found = self._from_vtable(thunk)
                        if found is not None:
                            return found
        return None

    def _index_pointers(self) -> None:
        """Every aligned 64-bit word in the data sections, by value.

        Built once. Resolving one name used to rescan the whole 22 MB image for a
        pointer; with 289 names that is the difference between minutes and seconds.
        Only .rdata and .data are scanned, because that is where vtables live.
        """
        self.pointers: dict[int, list[int]] = {}
        for section in self.image.sections:
            if section.name not in (".rdata", ".data"):
                continue
            start = section.raw_offset
            end = min(start + section.raw_size, len(self.image.data))
            end -= (end - start) % 8
            for offset in range(start, end, 8):
                value = int.from_bytes(self.image.data[offset : offset + 8], "little")
                if value >> 32:  # only plausible virtual addresses
                    self.pointers.setdefault(value, []).append(offset)

    def _from_vtable(self, thunk: int) -> int | None:
        for start in self.pointers.get(thunk, ()):
            slot_at = start + 8 * ID_GETTER_SLOT
            if slot_at + 8 > len(self.image.data):
                continue
            slot = struct.unpack_from("<Q", self.image.data, slot_at)[0]
            offset = self.image.to_offset(slot)
            if offset is None:
                continue
            code = self.image.data[offset : offset + 6]
            if code[0] == 0xB8 and code[5] == 0xC3:
                return struct.unpack_from("<I", code, 1)[0]
        return None


def main() -> int:
    resolver = Resolver(Image(open(sys.argv[1], "rb").read()))
    width = max(len(n) for n in sys.argv[2:])
    for name in sys.argv[2:]:
        found = resolver.command_id(name)
        print(
            "  %-*s  %s" % (width, name, "0x%04X" % found if found else "non resolu")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
