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

    def command_id(self, name: str) -> int | None:
        # A fully qualified name is taken as given; a bare one is a Commands:: class.
        # The client's skill commands live in Skills::, which is why enumerating only
        # Commands:: missed them.
        qualified = name if "::" in name else "Commands::" + name
        literal = qualified.encode() + b"\0"
        for m in re.finditer(re.escape(literal), self.image.data):
            va = self.image.to_va(m.start())
            if va is None:
                continue
            # The Rtti object is the operand of the very next lea: the constructor
            # loads the name, then `this`. Confirmed against HitCommand, whose id
            # this route reproduces as 0x6B.
            for site in self.leas.get(va, []):
                rtti = self.lea_at.get(site + 7)
                if rtti is None:
                    continue
                for thunk in self.thunks.get(rtti, []):
                    found = self._from_vtable(thunk)
                    if found is not None:
                        return found
        return None

    def _from_vtable(self, thunk: int) -> int | None:
        needle = struct.pack("<Q", thunk)
        for p in re.finditer(re.escape(needle), self.image.data):
            slot = struct.unpack_from(
                "<Q", self.image.data, p.start() + 8 * ID_GETTER_SLOT
            )[0]
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
