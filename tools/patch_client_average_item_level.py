"""Patch dro_client64.exe to calculate Character Sheet Average Item Level strictly from gear equipment slots 0..12.

Slots 0..12:
  0: Helmet
  1: Shoulders
  2: Torso
  3: Gloves
  4: Boots
  5: Main Hand Weapon
  6: Offhand / Shield (or secondary hand of 2H weapon)
  7: Cloak (backitem)
  8: Ring 1
  9: Ring 2
  10: Amulet
  11: Belt
  12: Adornment (weapon modifier)

Excludes rune/trinket slots (13..19) and essence slots (34..36).
"""

from __future__ import annotations

import os
from pathlib import Path

# Original bytes at 0x140956730 (raw 0x955b30)
EXPECTED_ORIGINAL_PREFIX = bytes([
    0x8b, 0x90, 0xc0, 0x00, 0x00, 0x00,  # mov edx, dword ptr [rax + 0xc0]
    0x8d, 0x4a, 0xf7,                    # lea ecx, [rdx - 9]
    0x83, 0xf9, 0x3e,                    # cmp ecx, 0x3e
    0x77, 0x10,                          # ja 0x14095674e
])

PATCH_OFFSET = 0x955b30
PATCH_TOTAL_LEN = 208

# New logic:
# cmp dword ptr [rbp + 0x20], 12
# ja 0x140956813 (offset 0xd9 from 0x14095673a)
# jmp 0x140956800 (offset 0xc1 from 0x14095673f)
# followed by NOPs
REPLACEMENT_BYTES = (
    bytes([0x83, 0x7d, 0x20, 0x0c])
    + bytes([0x0f, 0x87, 0xd9, 0x00, 0x00, 0x00])
    + bytes([0xe9, 0xc1, 0x00, 0x00, 0x00])
    + (b"\x90" * (PATCH_TOTAL_LEN - 15))
)


def patch_client_average_item_level(executable_path: str | Path) -> bool:
    path = Path(executable_path)
    if not path.is_file():
        print(f"File not found: {path}")
        return False

    data = bytearray(path.read_bytes())
    if len(data) < PATCH_OFFSET + PATCH_TOTAL_LEN:
        print(f"File too small: {path}")
        return False

    current_chunk = data[PATCH_OFFSET : PATCH_OFFSET + len(EXPECTED_ORIGINAL_PREFIX)]
    if current_chunk == REPLACEMENT_BYTES[: len(EXPECTED_ORIGINAL_PREFIX)]:
        print(f"Already patched: {path}")
        return True

    if current_chunk != EXPECTED_ORIGINAL_PREFIX:
        print(f"Byte mismatch at {hex(PATCH_OFFSET)} in {path}")
        return False

    data[PATCH_OFFSET : PATCH_OFFSET + PATCH_TOTAL_LEN] = REPLACEMENT_BYTES
    path.write_bytes(bytes(data))
    print(f"Successfully patched Average Item Level calculation in: {path}")
    return True


if __name__ == "__main__":
    candidates = [
        Path(os.path.expandvars(r"%LOCALAPPDATA%\Temp\DSOClient\dlcache\dro_client64.exe")),
        Path(r"C:\Program Files (x86)\Drakensang Online\dro_client64.exe"),
        Path(r"C:\Users\tolga\Desktop\dsor-client-installer-main\launcher\dro_client64.exe"),
    ]
    for cand in candidates:
        if cand.is_file():
            patch_client_average_item_level(cand)
