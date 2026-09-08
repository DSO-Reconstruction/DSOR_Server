#!/usr/bin/env python3
"""Take the operator's own character name out of the replayed captures.

The captures under ``dsor/data/`` are recordings of a real session, so they carry the
name of the character they were recorded for. That is the one piece of the operator's
own data in this repository, and it does not need to be here for anything to work.

**The placeholder is the same length as the name it replaces**, and that is the whole
design. These names are length-prefixed, so a shorter one moves every byte behind it:
replacing an eleven-byte name with the eight-byte root shortened the tutorial batch by three
bytes and shifted every bit offset inside it by 24 -- ``content_bits`` 4,594 to 4,570,
``actionbar.FIRST_BAR`` 103,610 to 103,586, and every measured offset written in a
comment anywhere in the project silently off by the same amount. Seven tests caught it;
the ones that would not have been caught are the reason this pads instead.

So: same length, nothing moves, no constant changes, and the diff is the bytes of a
name and nothing else.

Run it from the repository root. It is idempotent -- a file already scrubbed is left
alone -- and it refuses rather than guesses if a file does not read as expected.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dsor import charlist, playerstate  # noqa: E402
from dsor.recorded import payload  # noqa: E402
from raknet.bitstream import BitReader  # noqa: E402
from raknet.payload import bits_of  # noqa: E402

#: The placeholder's root. The replacement is this padded with zeros to the exact
#: length of the name it replaces, which is computed rather than written down -- so this
#: file names no real character, which would rather defeat the point of it.
ROOT = "Username"


def scrubbed(was: str) -> str:
    """What *was* becomes: the root, zero-padded to the same number of bytes.

    Same length, and that is the whole design. These names are length-prefixed, so a
    shorter one moves every byte behind it: replacing an eleven-byte name with the
    eight-byte root shortened the tutorial batch by three bytes and shifted every bit
    offset inside it by 24 -- ``content_bits`` 4,594 to 4,570, ``actionbar.FIRST_BAR``
    103,610 to 103,586, and every measured offset written in a comment anywhere in this
    project silently off by the same amount. Seven tests caught that; the offsets that
    live only in prose would not have been caught, which is why this pads.
    """
    if len(was) < len(ROOT):
        raise SystemExit(
            f"{was!r} is shorter than {ROOT!r}, so it cannot be padded to fit"
        )
    return ROOT + "0" * (len(was) - len(ROOT))


#: The batch files, whose name sits in the leading NewPlayerCommand.
BATCHES = ("zone_content.bin", "zone_content_kingscity.bin")

#: The roster, whose names are bit-offset-1 packed and so need its own writer.
ROSTER = "character_list.bin"

DATA = pathlib.Path(__file__).resolve().parent.parent / "dsor/data"


def _refuse_unscrubbed(name: str, blob: bytes, was: str) -> None:
    """Refuse to write a file whose name field did not actually change."""
    if was != scrubbed(was) and was.encode() in blob:
        raise SystemExit(f"{name}: the recorded name is still in the bytes")


def batch(name: str) -> bool:
    """Rename the character in one arrival batch. True if it changed."""
    path = DATA / name
    blob = path.read_bytes()
    before = playerstate.progress_of(blob)
    if before is None:
        raise SystemExit(f"{name}: no player command reads out of it")
    was = before["name"]
    out = scrubbed(was)
    if was == out:
        print(f"  {name}: already {was!r}")
        return False
    fresh = bytes(playerstate.with_name(blob, out))
    if len(fresh) != len(blob):
        raise SystemExit(f"{name}: {len(blob)} -> {len(fresh)} bytes, so something moved")
    kept = playerstate.progress_of(fresh)
    if kept is None or kept["name"] != out:
        raise SystemExit(f"{name}: the rename does not read back")
    _refuse_unscrubbed(name, fresh, was)
    path.write_bytes(fresh)
    print(f"  {name}: {len(was)}-byte name -> {out!r}, {len(fresh)} bytes "
          f"unchanged, level {kept['level']}")
    return True


def roster() -> bool:
    """Rename the character in the selection screen's roster. True if it changed."""
    path = DATA / ROSTER
    blob = payload(ROSTER)
    entries = charlist.entries(blob)
    if not entries:
        raise SystemExit(f"{ROSTER}: no entry reads out of it")
    was = entries[0]
    out = scrubbed(was["name"])
    if was["name"] == out:
        print(f"  {ROSTER}: already {out!r}")
        return False
    raw = bytes(blob)
    character = BitReader(raw, charlist.CHARACTER_AT).read_uint(32)
    account = BitReader(raw, charlist.ACCOUNT_AT).read_uint(32)
    slots = BitReader(raw, charlist.COUNT_AT).read_uint(32)
    # Given its own values back, the builder reproduces the recording byte for byte --
    # so a rename is the only difference between what goes out and what came in.
    same = charlist.build(blob, [dict(was, character=character)], account,
                          andermant=was["andermant"], slots=slots)
    if bytes(same) != raw:
        raise SystemExit(f"{ROSTER}: the builder does not reproduce it, refusing to write")
    fresh = charlist.build(blob, [dict(was, name=out, character=character)], account,
                           andermant=was["andermant"], slots=slots)
    if len(bytes(fresh)) != len(raw) or bits_of(fresh) != bits_of(blob):
        raise SystemExit(f"{ROSTER}: the length changed, so something moved")
    _refuse_unscrubbed(ROSTER, bytes(fresh), was["name"])
    path.write_bytes(bytes(fresh))
    print(f"  {ROSTER}: {len(was['name'])}-byte name -> {out!r}, "
          f"{len(bytes(fresh))} bytes and {bits_of(fresh)} bits unchanged")
    return True


def main() -> int:
    changed = False
    for name in BATCHES:
        if (DATA / name).exists():
            changed |= batch(name)
        else:
            print(f"  {name}: absent")
    changed |= roster()
    print("changed" if changed else "nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
