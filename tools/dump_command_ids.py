#!/usr/bin/env python3
"""Recover the network command ID table from the DSO x64 client binary.

Inspects dro_client64.exe, traces Nebula3 RTTI registration to creators,
constructors, vtables, and reads GetCommandId() from vtable slot 3 and
actor-scoping flags from vtable slots 13 and 14.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
from pathlib import Path
import struct
import sys
import capstone
import pefile

DEFAULT_CLIENT_DIR = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Temp" / "DSOClient" / "dlcache"
)


def recover_command_ids(exe_path: Path) -> dict[str, dict]:
    pe = pefile.PE(str(exe_path))
    data = exe_path.read_bytes()
    image_base = pe.OPTIONAL_HEADER.ImageBase

    pdata_dir = pe.OPTIONAL_HEADER.DATA_DIRECTORY[
        pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXCEPTION"]
    ]
    pdata_size = pdata_dir.Size
    pdata_offset = pe.get_offset_from_rva(pdata_dir.VirtualAddress)
    pdata_bytes = data[pdata_offset : pdata_offset + pdata_size]

    entries = []
    for i in range(0, len(pdata_bytes), 12):
        b, e, u = struct.unpack("<III", pdata_bytes[i : i + 12])
        entries.append((b, e, u))

    begin_rvas = [e[0] for e in entries]

    def find_fn(va: int) -> tuple[int | None, int | None]:
        rva = va - image_base
        idx = bisect.bisect_right(begin_rvas, rva) - 1
        if idx >= 0:
            b, e, u = entries[idx]
            if b <= rva < e:
                return image_base + b, image_base + e
        return None, None

    def read_str(va: int) -> str | None:
        offset = pe.get_offset_from_rva(va - image_base)
        null_pos = data.find(b"\x00", offset)
        if null_pos != -1:
            try:
                return data[offset:null_pos].decode("latin1")
            except Exception:
                return None
        return None

    def read_qword(va: int) -> int:
        offset = pe.get_offset_from_rva(va - image_base)
        return struct.unpack("<Q", data[offset : offset + 8])[0]

    text_sec = [s for s in pe.sections if s.Name.startswith(b".text")][0]
    text_start_va = image_base + text_sec.VirtualAddress
    text_data = data[
        text_sec.PointerToRawData : text_sec.PointerToRawData + text_sec.SizeOfRawData
    ]

    candidate_targets = [
        # (registration function, allocator thunk, inherited actor stubs)
        (0x140CF19FC, 0x140CF1A34, (0x140A8B3FC, 0x140A8B438)),  # release261_222
        (0x140CF114C, 0x140CF1184, (0x140A8AA04, 0x140A8AA40)),  # release261_220/221
        (0x140CF100C, 0x140CF1044, (0x140A8AA04, 0x140A8AA40)),  # release261_219
    ]

    target_va = None
    allocator_va = None
    callers = []

    stub_pair = None
    for t_va, a_va, candidate_stub_pair in candidate_targets:
        test_callers = []
        for offset in range(len(text_data) - 5):
            if text_data[offset] == 0xE8:
                disp32 = struct.unpack("<i", text_data[offset + 1 : offset + 5])[0]
                instr_va = image_base + pe.get_rva_from_offset(text_sec.PointerToRawData + offset)
                if instr_va + 5 + disp32 == t_va:
                    if 0x140080000 <= instr_va <= 0x140150000:
                        test_callers.append(instr_va)
        if len(test_callers) > 200:
            target_va = t_va
            allocator_va = a_va
            stub_pair = candidate_stub_pair
            callers = test_callers
            break

    if not target_va:
        return {}

    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)

    recovered = {}

    for c_va in callers:
        f_start, f_end = find_fn(c_va)
        if not f_start:
            continue
        f_offset = pe.get_offset_from_rva(f_start - image_base)
        f_code = data[f_offset : f_offset + (f_end - f_start)]

        insns = list(md.disasm(f_code, f_start))

        name_va = None
        rtti_va = None
        creator_va = None

        for insn in insns:
            if insn.address > c_va:
                break
            if insn.mnemonic == "lea" and insn.op_str.startswith("rdx, [rip"):
                disp = (
                    int(insn.op_str.split("rip + ")[1].rstrip("]"), 16)
                    if "rip + " in insn.op_str
                    else -int(insn.op_str.split("rip - ")[1].rstrip("]"), 16)
                )
                name_va = insn.address + insn.size + disp
            elif insn.mnemonic == "lea" and insn.op_str.startswith("rcx, [rip"):
                disp = (
                    int(insn.op_str.split("rip + ")[1].rstrip("]"), 16)
                    if "rip + " in insn.op_str
                    else -int(insn.op_str.split("rip - ")[1].rstrip("]"), 16)
                )
                rtti_va = insn.address + insn.size + disp
            elif insn.mnemonic == "lea" and insn.op_str.startswith("r9, [rip"):
                disp = (
                    int(insn.op_str.split("rip + ")[1].rstrip("]"), 16)
                    if "rip + " in insn.op_str
                    else -int(insn.op_str.split("rip - ")[1].rstrip("]"), 16)
                )
                creator_va = insn.address + insn.size + disp
            elif insn.mnemonic == "xor" and insn.op_str == "r9d, r9d":
                creator_va = 0

        if not name_va:
            continue
        name = read_str(name_va)
        if not name or not name.endswith("Command"):
            continue
        if name in ("Db::Sqlite3Command", "Chat::BuiltinChatCommand"):
            continue

        if not creator_va:
            continue

        cr_offset = pe.get_offset_from_rva(creator_va - image_base)
        cr_code = data[cr_offset : cr_offset + 30]
        cr_insns = list(md.disasm(cr_code, creator_va))

        thunk_va = creator_va
        if cr_insns and cr_insns[0].mnemonic == "jmp":
            try:
                thunk_va = int(cr_insns[0].op_str, 16)
            except ValueError:
                pass

        th_offset = pe.get_offset_from_rva(thunk_va - image_base)
        th_code = data[th_offset : th_offset + 80]
        th_insns = list(md.disasm(th_code, thunk_va))

        ctor_va = None
        for insn in th_insns:
            if insn.mnemonic == "call":
                try:
                    target = int(insn.op_str, 16)
                    if target != allocator_va:
                        ctor_va = target
                except ValueError:
                    pass
            elif insn.mnemonic == "ret":
                break

        if not ctor_va:
            continue

        ct_offset = pe.get_offset_from_rva(ctor_va - image_base)
        ct_code = data[ct_offset : ct_offset + 120]
        ct_insns = list(md.disasm(ct_code, ctor_va))

        vtable_va = None
        for i_idx, insn in enumerate(ct_insns):
            if insn.mnemonic == "ret":
                break
            if insn.mnemonic == "lea" and insn.op_str.startswith("rax, [rip"):
                disp = (
                    int(insn.op_str.split("rip + ")[1].rstrip("]"), 16)
                    if "rip + " in insn.op_str
                    else -int(insn.op_str.split("rip - ")[1].rstrip("]"), 16)
                )
                candidate_vtable = insn.address + insn.size + disp
                for next_i in ct_insns[i_idx + 1 : i_idx + 10]:
                    if next_i.mnemonic == "ret":
                        break
                    if (
                        next_i.mnemonic == "mov"
                        and "rax" in next_i.op_str
                        and "[" in next_i.op_str
                    ):
                        vtable_va = candidate_vtable
                        break
            if vtable_va:
                break

        if not vtable_va:
            continue

        try:
            slot3_va = read_qword(vtable_va + 3 * 8)
            s3_offset = pe.get_offset_from_rva(slot3_va - image_base)
            s3_code = data[s3_offset : s3_offset + 20]
            s3_insns = list(md.disasm(s3_code, slot3_va))

            cmd_id = None
            if s3_insns:
                first_i = s3_insns[0]
                if first_i.mnemonic == "mov" and first_i.op_str.startswith("eax, "):
                    cmd_id = int(first_i.op_str.split("eax, ")[1], 16)
                elif first_i.mnemonic == "mov" and first_i.op_str.startswith("ax, "):
                    cmd_id = int(first_i.op_str.split("ax, ")[1], 16)
                elif first_i.mnemonic == "xor" and first_i.op_str == "eax, eax":
                    cmd_id = 0

            slot13_va = read_qword(vtable_va + 13 * 8)
            slot14_va = read_qword(vtable_va + 14 * 8)
            is_scoped = (slot13_va, slot14_va) != stub_pair

            recovered[name] = {
                "id": cmd_id,
                "vtable": hex(vtable_va),
                "creator": hex(creator_va),
                "via": "creator",
                "actor_scoped": is_scoped,
            }
        except Exception:
            pass

    return recovered


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dump DSO client network command IDs"
    )
    parser.add_argument(
        "--exe",
        type=Path,
        default=DEFAULT_CLIENT_DIR / "dro_client64.exe",
        help="Path to dro_client64.exe",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=Path("docs") / "command_ids_rel261_222.json",
        help="Output JSON file path",
    )
    parser.add_argument(
        "--grep",
        type=str,
        default=None,
        help="Filter command names by substring",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate against active protocol mapping",
    )
    args = parser.parse_args()

    if not args.exe.is_file():
        print(f"Error: Executable not found at {args.exe}")
        return 1

    print(f"Analyzing {args.exe}...")
    table = recover_command_ids(args.exe)
    print(f"Recovered {len(table)} command classes.")

    if args.grep:
        for name, info in sorted(table.items()):
            if args.grep.casefold() in name.casefold():
                print(f"  {name}: {info}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(table, indent=1), encoding="utf-8")
        print(f"Saved to {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
