#!/usr/bin/env python3
"""Recover a command's wire layout by disassembling its Serialize method.

    python3 tools/command_layout.py ~/dso/DSOClient/dlcache/dro_client64.exe HitCommand

Extends the chain ``tools/command_ids.py`` already walks. That one stops at vtable
slot 3, the id getter; the layout is in slot 4, Serialize, which writes each field
through a bit writer in wire order. Disassembling it and listing the calls in order
gives the field widths without guessing at a hex dump.

Validate on HitCommand first, whose layout is known from the wire, before trusting
what it says about a command no capture decodes.
"""

from __future__ import annotations

import pathlib
import struct
import sys

import capstone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from command_ids import ID_GETTER_SLOT, Image, Resolver  # noqa: E402

SERIALIZE_SLOT = 4
DESERIALIZE_SLOT = 5


class Layout(Resolver):
    """A resolver that hands back a vtable rather than an id."""

    def vtable_of(self, name: str) -> int | None:
        import re

        bare = name.rsplit("::", 1)[-1]
        for spelling in dict.fromkeys((name, "Commands::" + bare, bare)):
            literal = spelling.encode() + b"\0"
            for m in re.finditer(re.escape(literal), self.image.data):
                va = self.image.to_va(m.start())
                if va is None:
                    continue
                for site in self.leas.get(va, []):
                    for delta in range(1, 32):
                        rtti = self.lea_at.get(site + delta)
                        if rtti is None:
                            continue
                        for thunk in self.thunks.get(rtti, []):
                            for start in self.pointers.get(thunk, ()):
                                if self._is_command_vtable(start):
                                    return start
        return None

    def _is_command_vtable(self, start: int) -> bool:
        slot_at = start + 8 * ID_GETTER_SLOT
        slot = struct.unpack_from("<Q", self.image.data, slot_at)[0]
        offset = self.image.to_offset(slot)
        if offset is None:
            return False
        code = self.image.data[offset : offset + 6]
        return code[0] == 0xB8 and code[5] == 0xC3

    def slot(self, vtable_offset: int, index: int) -> int:
        return struct.unpack_from(
            "<Q", self.image.data, vtable_offset + 8 * index
        )[0]


def disassemble(image: Image, va: int, limit: int = 0x400):
    offset = image.to_offset(va)
    if offset is None:
        return []
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    out = []
    for ins in md.disasm(image.data[offset : offset + limit], va):
        out.append(ins)
        if ins.mnemonic == "ret":
            break
    return out


def fields(instructions):
    """The (object offset, bit width) pairs a Serialize writes, in wire order.

    The generated code is uniform: the field is loaded from ``[rsi + offset]``, the
    width reaches ``r8d``, and a virtual call through the writer commits it. The
    width is often not an immediate — the compiler hoists a constant into r14d and
    materialises small ones with ``lea r12d, [r14 - 0x1f]`` — so the integer
    registers are tracked rather than only immediates. Anything that does not fit the
    shape is reported as a bare call, so a composite field (a string, a float3) shows
    up rather than being dropped silently.
    """
    out = []
    regs: dict[str, int] = {}
    offset = None
    for ins in instructions:
        text = ins.op_str
        parts = [p.strip() for p in text.split(",")]
        dest = parts[0] if parts else ""

        if ins.mnemonic in ("mov", "lea") and "[rsi + 0x" in text:
            try:
                offset = int(text.split("[rsi + ")[1].split("]")[0], 16)
            except (IndexError, ValueError):
                pass
        if ins.mnemonic == "mov" and len(parts) == 2:
            if parts[1].startswith("0x") and dest in ("r8d", "r12d", "r14d", "ecx"):
                regs[dest] = int(parts[1], 16)
            elif parts[1] in regs and dest in ("r8d", "r12d", "r14d"):
                regs[dest] = regs[parts[1]]
        if ins.mnemonic == "lea" and len(parts) == 2 and dest.endswith("d"):
            # lea r12d, [r14 - 0x1f] -> r14 - 0x1f
            body = parts[1]
            for reg in ("r14", "r12", "r8"):
                if f"[{reg} " in body or f"[{reg}]" in body:
                    base = regs.get(reg + "d")
                    if base is None:
                        continue
                    if " - " in body:
                        regs[dest] = base - int(body.split(" - ")[1].rstrip("]"), 16)
                    elif " + " in body:
                        regs[dest] = base + int(body.split(" + ")[1].rstrip("]"), 16)
                    else:
                        regs[dest] = base
                    break
        if ins.mnemonic == "call":
            if "qword ptr [rax + 0xa8]" in text:
                out.append((offset, regs.get("r8d")))
                offset = None
            elif text.startswith("0x") and not text.endswith("364a4"):
                out.append((offset, f"helper {text}"))
                offset = None
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    image = Image(pathlib.Path(sys.argv[1]).read_bytes())
    resolver = Layout(image)
    for name in sys.argv[2:]:
        vtable = resolver.vtable_of(name)
        if vtable is None:
            print(f"{name}: no vtable found")
            continue
        ident = resolver.slot(vtable, ID_GETTER_SLOT)
        id_offset = image.to_offset(ident)
        wire = struct.unpack_from("<I", image.data, id_offset + 1)[0]
        print(f"=== {name}  id {wire:#06x}  vtable at file {vtable:#x} ===")
        for label, index in (
            ("Serialize", SERIALIZE_SLOT),
            ("Deserialize", DESERIALIZE_SLOT),
        ):
            va = resolver.slot(vtable, index)
            if label == "Deserialize":
                continue
            code = disassemble(image, va)
            print(f"--- {label} at {va:#x}, {len(code)} instructions ---")
            for offset, width in fields(code):
                where = "?" if offset is None else f"+{offset:#04x}"
                if isinstance(width, str):
                    print(f"  {where:>6s}  helper {width}")
                else:
                    print(f"  {where:>6s}  {width} bits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
