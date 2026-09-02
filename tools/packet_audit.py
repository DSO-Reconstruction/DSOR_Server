#!/usr/bin/env python3
"""How much of the captured traffic this server actually understands.

Run as::

    python3 tools/packet_audit.py ~/dso-capture/session-*.jsonl

The point is transparency about the gaps rather than a guess at their size. For a long
time "the packets are mostly decoded" was an impression; this counts.

Every datagram in a capture is taken apart down to its frames, and every frame's
payload is classified:

* **message id** -- the first byte. 0x85 is the multi-command container this game uses
  for almost everything; the rest are RakNet's own or one of the game's single
  messages.
* **command id** -- for a 0x85, the 16 bits after it. That is the command the client
  will hand to a decoder, and it is the unit this server either understands or does
  not.
* **accounted bits** -- for a command with a decoder here, how far the decoder gets.
  The frame states its own length in bits, so "the decoder ends exactly where the
  frame does" is a *proof* for a frame carrying one command, and "the decoder ends
  early" is either more commands behind it or a wrong read.

What comes out is a table of command ids by volume, with a name where the client's own
Rtti gives one, and a column saying whether this server can read it. That is the list
of work, in the order that matters.
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dsor.chain import walk  # noqa: E402
from raknet.datagram import parse_datagram_header  # noqa: E402
from raknet.frame import parse_frames  # noqa: E402

#: The container almost every game message travels in.
MULTI = 0x85

#: The other leading byte the game uses for a command, seen from the client.
MULTI_ALT = 0x8B

#: Commands this server can read, and the reader that proves it.
KNOWN: dict[int, str] = {
    0x004F: "dsor.recorded.walk_elements",
    0x005F: "dsor.gameplay.decode_entity_update",
    0x006B: "dsor.combat.encode_hit (built, so the layout is known)",
    0x0046: "dsor.combat.decode_skill_use",
    0x0047: "dsor.combat.decode_skill_use",
    0x0048: "dsor.combat.decode_skill_use",
    0x0049: "dsor.combat.decode_skill_use",
    0x004A: "dsor.combat.decode_skill_use",
    0x004B: "dsor.combat.decode_skill_use",
    0x0051: "dsor.quickslots.decode",
    0x0058: "dsor.skillbook",
    0x002A: "dsor.recorded.monster_template",
    0x002D: "dsor.items.with_drop",
    0x002E: "dsor.inventory.discarded",
    0x0054: "dsor.inventory.decode (both live replies re-encode byte for byte)",
    0x0064: "dsor.world.pick_up",
    0x0030: "dsor.items (partial)",
    0x003C: "dsor.location.decode",
    0x003D: "dsor.location.decode",
    0x003E: "dsor.location.encode (round-trips to the byte)",
}


def names_from(path: pathlib.Path) -> dict[int, str]:
    """``id -> class name`` from tools/command_ids.py's output, when it is there."""
    found: dict[int, str] = {}
    if not path.exists():
        return found
    for line in path.read_text(errors="ignore").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].startswith("0x"):
            try:
                found[int(parts[-1], 16)] = parts[0]
            except ValueError:
                continue
    return found


def audit(captures: list[pathlib.Path]) -> dict:
    counts: dict[tuple[bool, int], int] = collections.Counter()
    volume: dict[tuple[bool, int], int] = collections.Counter()
    leading: dict[int, int] = collections.Counter()
    frames_seen = fragments = accounted = unaccounted = 0

    for capture in captures:
        try:
            lines = capture.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except Exception:
                continue
            if "hex" not in record:
                continue
            try:
                raw = bytes.fromhex(record["hex"])
                _header, offset = parse_datagram_header(raw)
                frames = parse_frames(raw, offset)
            except Exception:
                continue
            from_server = bool(record.get("from_server"))
            for frame in frames:
                payload = frame.payload
                frames_seen += 1
                if frame.split_count is not None:
                    fragments += 1
                    continue
                if not payload:
                    continue
                leading[payload[0]] += 1
                if payload[0] not in (MULTI, MULTI_ALT) or len(payload) < 3:
                    continue
                # Every command in the container, not just the first. Reading only the
                # leading id undercounted the traffic by a fifth: 1,656 of 8,000 real
                # payloads carry more than one command, and the batched entity updates
                # behind the first came to 8,845 in that sample alone.
                chained, leftover = walk(payload, most=64)
                unaccounted += leftover
                if not chained:
                    command = payload[1] | (payload[2] << 8)
                    counts[(from_server, command)] += 1
                    volume[(from_server, command)] += len(payload)
                    continue
                for command in chained:
                    counts[(from_server, command.id)] += 1
                    volume[(from_server, command.id)] += command.body_bits // 8
                if leftover == 0:
                    accounted += len(chained)
    return {
        "counts": counts,
        "volume": volume,
        "leading": leading,
        "frames": frames_seen,
        "fragments": fragments,
        "accounted": accounted,
        "unaccounted": unaccounted,
    }


def main(argv: list[str]) -> int:
    captures = [pathlib.Path(a) for a in argv[1:] if a.endswith(".jsonl")]
    if not captures:
        captures = sorted(pathlib.Path.home().glob("dso-capture/session-*.jsonl"))
    if not captures:
        print("no captures given and none in ~/dso-capture")
        return 1

    here = pathlib.Path(__file__).resolve().parent.parent
    names = names_from(here / "dsor/command_names.txt")
    if not names:
        names = names_from(pathlib.Path("/tmp/ids.txt"))

    found = audit(captures)
    counts, volume = found["counts"], found["volume"]
    print(f"{len(captures)} captures, {found['frames']} frames, "
          f"{found['fragments']} of them fragments")
    print()
    print("leading message ids:")
    for lead, n in found["leading"].most_common(8):
        print(f"   {lead:#04x}  {n}")
    print()

    total = sum(counts.values())
    known = sum(n for (_d, c), n in counts.items() if c in KNOWN)
    print(f"{total} commands in a 0x85 container; this server reads "
          f"{known} of them ({100 * known / max(total, 1):.1f}%)")
    print(f"{found['accounted']} of them ({100 * found['accounted'] / max(total, 1):.1f}%) "
          f"sit in a payload the chain walker accounts for to the bit; "
          f"{found['unaccounted']} bits unread")
    print()
    header = f'{"dir":4s} {"id":>7s} {"count":>8s} {"bytes":>10s}  {"read":4s} name'
    print(header)
    print("-" * max(len(header), 78))
    for (from_server, command), n in counts.most_common(45):
        mark = "yes" if command in KNOWN else "NO"
        print(f'{"S->C" if from_server else "C->S":4s} {command:#07x} {n:8d} '
              f'{volume[(from_server, command)]:10d}  {mark:4s} '
              f'{names.get(command, "?")}')
    unread = sorted(
        {c for (_d, c) in counts if c not in KNOWN},
        key=lambda c: -sum(v for (d, x), v in volume.items() if x == c),
    )
    print()
    print(f"{len(unread)} distinct commands with no reader here, by volume:")
    for command in unread[:20]:
        vol = sum(v for (_d, x), v in volume.items() if x == command)
        print(f"   {command:#07x}  {vol:10d} bytes  {names.get(command, '?')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
