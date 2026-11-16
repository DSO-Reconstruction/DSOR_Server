"""Keep equipment effects on the inventory model in the supported x64 client."""

import hashlib
import struct
import tempfile
from pathlib import Path


# Both release261_1024 images use the same call site, object layout, and guard padding.
_SUPPORTED_CLIENT_SHA256 = frozenset({
    "0b593691da671f5cb525a566b83e7fde3b9e9e009512928bf3ede036fe718e06",
    "dba92163fd8d5535fd7bb69158d10b26269b8510fe2197576a307ef210647454",
})
_CALL_RVA = 0x1513CD
_CALL_OFFSET = 0x1507CD
_UPDATE_SEQUENCES_RVA = 0x151C14
_OFFSCREEN_VTABLE_RVA = 0xEC0680
_GUARD_RVA = 0xE45BC0
_GUARD_OFFSET = 0xE44FC0
_TEXT_VIRTUAL_SIZE_OFFSET = 0x250


def _relative32(target: int, next_instruction: int) -> bytes:
    return struct.pack("<i", target - next_instruction)


def _guard() -> bytes:
    # UpdateAdditionalSequencer can replay an empty list on the inventory
    # actor, wiping the sequences VisualizeLocalPlayer just copied onto it.
    # Skip that call only for an empty list, the offscreen actor class, and
    # inventory mode. Tail jump for the other cases to preserve the x64 ABI.
    code = bytearray(b"\x83\x7a\x08\x00\x75\x00")  # cmp [rdx+8], 0; jne original
    code += b"\x48\x8d\x05"
    code += _relative32(_OFFSCREEN_VTABLE_RVA, _GUARD_RVA + len(code) + 4)
    code += b"\x48\x39\x01\x75\x00"  # cmp [rcx], rax; jne original
    code += b"\x83\xb9\x18\x02\x00\x00\x01\x75\x00\xc3"
    original = len(code)
    for displacement_offset in (5, 17, 26):
        code[displacement_offset] = original - displacement_offset - 1
    code += b"\xe9" + _relative32(_UPDATE_SEQUENCES_RVA, _GUARD_RVA + original + 5)
    return bytes(code)


def _patched_image(source: bytes) -> bytes | None:
    if hashlib.sha256(source).hexdigest() not in _SUPPORTED_CLIENT_SHA256:
        return None
    guard = _guard()
    image = bytearray(source)
    image[_CALL_OFFSET:_CALL_OFFSET + 5] = b"\xe8" + _relative32(_GUARD_RVA, _CALL_RVA + 5)
    image[_GUARD_OFFSET:_GUARD_OFFSET + len(guard)] = guard
    # The guard occupies zero padding at the end of the executable .text
    # section. Extend VirtualSize to cover it without moving any sections.
    struct.pack_into("<I", image, _TEXT_VIRTUAL_SIZE_OFFSET, _GUARD_RVA - 0x1000 + len(guard))
    return bytes(image)


def create_inventory_vfx_client(executable: Path) -> Path | None:
    """Create a temporary launch image for the supported client build."""
    image = _patched_image(executable.read_bytes())
    if image is None:
        return None

    handle = tempfile.NamedTemporaryFile(
        dir=executable.parent,
        prefix=f"{executable.stem}.inventory-vfx-",
        suffix=executable.suffix,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(image)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return temporary
