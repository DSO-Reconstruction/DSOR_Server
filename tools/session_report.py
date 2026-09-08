#!/usr/bin/env python3
"""What actually happened in a captured session.

    python3 tools/session_report.py ~/dso-capture/officiel4/session-*.jsonl

Two things this does that no other tool here did.

**It reassembles fragments.** A message larger than the MTU arrives as split frames, and
every earlier pass over a capture skipped them -- which is how the experience message
stayed hidden for weeks, and then the 8.4 KB reply to a pickup after it. Of 14,520
messages in one session 166 are reassembled, and they are the interesting ones: the map
entry at 1.1 MB, the event schedule, both inventory replies.

**It reads the client's side too.** The server chains its commands into a 0x85 with an
actor and a 0xFF after each; the client sends one command per 0x8B with neither. Walking
the second as though it were the first accounted for 0 of 4,000 client payloads, so for a
long time nothing here could say what the player had actually *done*.

What comes out is a session in the terms a person would describe it in: the skills cast,
the effects that landed and on whom, the creatures that appeared, the items picked up.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dsor import effects, statuseffect  # noqa: E402
from dsor.chain import single, walk  # noqa: E402
from dsor.combat import decode_skill_use  # noqa: E402
from dsor.inventory import decode as decode_inventory  # noqa: E402
from dsor.skills import skill as skill_at  # noqa: E402
from raknet.datagram import parse_datagram_header  # noqa: E402
from raknet.frame import parse_frames  # noqa: E402

#: The server's chained container, and the client's single one.
MULTI = 0x85
SINGLE = 0x8B

#: Command ids that carry a skill the player just used. All six begin the same way.
SKILL_COMMANDS = frozenset({0x0046, 0x0047, 0x0048, 0x0049, 0x004A, 0x004B})

#: What the client sends to take an item off the ground.
PICKUP = 0x0064

#: The commands worth naming in a summary, beyond the ones read in full.
NOTABLE = {
    0x0060: "unlocked a map",
    0x0061: "travelled",
    0x0062: "asked to respawn",
    0x0087: "chose a character",
}

def command_names() -> dict[int, str]:
    """``id -> class name`` out of docs/commands.md, so a report reads in English.

    From the document rather than a second table, because the document is generated
    from the client's own Rtti by tools/command_ids.py and two copies would drift.
    """
    found: dict[int, str] = {}
    doc = pathlib.Path(__file__).resolve().parent.parent / "docs/commands.md"
    if not doc.exists():
        return found
    for line in doc.read_text(errors="ignore").splitlines():
        match = re.match(r"\|\s*`0x([0-9A-Fa-f]{4})`\s*\|\s*([A-Za-z0-9_]+)", line)
        if match:
            found[int(match.group(1), 16)] = match.group(2)
    return found


#: The commands a map server sends before it starts ticking, in the order the live
#: service sends them. Printed with ``--arrival``, because "what does the client need to
#: spawn" is a question this project has answered wrong twice and the capture answers it.
ARRIVAL = 0x8D

#: Status effects, creature descriptions, the reply to a pickup.
STATUS_EFFECT = 0x004F
CREATURE = 0x002A
DISCARD = 0x002E

#: Past this, walking a chain costs more than the answer is worth: the map entry is
#: over a megabyte and holds no status effect this is looking for.
BIGGEST = 20_000

#: Creature blueprints look like this in a description, which carries no length this
#: tool knows how to find.
BLUEPRINT = re.compile(rb"(?:pw|a0|g0|h0|m0)[a-z0-9_]{8,60}")


def messages(path: pathlib.Path):
    """``(record number, from the server, payload)`` with fragments reassembled."""
    pending: dict[tuple, dict[int, bytes]] = collections.defaultdict(dict)
    for line in path.read_text(errors="ignore").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
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
        number = record.get("frame", 0)
        for frame in frames:
            if frame.split_count is None:
                if frame.payload:
                    yield number, from_server, frame.payload
                continue
            key = (record.get("conn", ""), from_server, frame.split_id)
            store = pending[key]
            store[frame.split_index] = frame.payload
            if len(store) == frame.split_count:
                whole = b"".join(store[index] for index in sorted(store))
                del pending[key]
                yield number, from_server, whole


def report(path: pathlib.Path) -> dict:
    cast: collections.Counter = collections.Counter()
    landed: dict[int, collections.Counter] = collections.defaultdict(
        collections.Counter
    )
    creatures: collections.Counter = collections.Counter()
    taken: list[tuple[int, str]] = []
    asked: list[int] = []
    did: list[tuple[int, str]] = []
    unreadable = 0
    total = reassembled = 0

    for number, from_server, payload in messages(path):
        total += 1
        if not from_server:
            if payload[0] != SINGLE:
                continue
            found = single(payload)
            if found is None:
                continue
            command, body = found
            if command in SKILL_COMMANDS:
                used = decode_skill_use(body)
                if used is not None:
                    known = skill_at(used.wire)
                    cast[known.id if known else f"wire {used.wire}"] += 1
            elif command == PICKUP and len(body) >= 4:
                asked.append(int.from_bytes(body[:4], "little"))
            elif command in NOTABLE:
                did.append((number, NOTABLE[command]))
            continue
        if payload[0] != MULTI or len(payload) > BIGGEST:
            continue
        if payload[1] == (DISCARD & 0xFF) and payload[2] == 0:
            try:
                got, _ends = decode_inventory(payload)
            except Exception:
                got = None
            if got is not None:
                for record in got.items:
                    if record.id in asked:
                        taken.append((record.id, record.template))
            continue
        if payload[1] == (CREATURE & 0xFF) and payload[2] == 0:
            for match in BLUEPRINT.findall(payload):
                creatures[match.decode()] += 1
            continue
        try:
            chained, _leftover = walk(payload, most=64)
        except Exception:
            continue
        for command in chained:
            if command.id != STATUS_EFFECT:
                continue
            got = statuseffect.decode(
                payload, at=24 + command.start, ends=24 + command.end
            )
            if got is None:
                unreadable += 1
                continue
            for element in got.elements:
                effect = effects.by_wire(element.index)
                name = effect.id if effect else f"wire {element.index}"
                landed[element.holder or got.actor][name] += 1

    return {
        "messages": total,
        "cast": cast,
        "landed": landed,
        "creatures": creatures,
        "taken": taken,
        "did": did,
        "unreadable": unreadable,
    }


def arrival(path: pathlib.Path, most: int = 40) -> list[tuple[str, int, int, str]]:
    """The batch a map server sends on arrival, command by command.

    Every one of those batches is a single message -- Kingshill's is 1.17 MB and 466
    chained commands -- so this walks it forward from its first command, taking each
    end to be the first place a valid ``actor + 0xFF + next id`` tail sits. The walk
    landing exactly on the payload's last bit is what makes the reading believable.

    What it is for: the arrival's **last** command is ``0x001F PlayerReadyCommand``,
    seven bytes naming the player's actor, and an arrival without it leaves the client
    holding a description of a character it never instantiates. That cost a round of
    testing to find and one line here to see.
    """
    from dsor.chain import MOST_ACTOR_PAGES, MOST_COMMANDS

    def window(raw: bytes, at: int, count: int) -> int:
        first, need = at >> 3, (at % 8 + count + 7) >> 3
        chunk = raw[first : first + need]
        if len(chunk) < need:
            raise IndexError(at)
        value = int.from_bytes(chunk, "big")
        return (value >> (need * 8 - (at % 8) - count)) & ((1 << count) - 1)

    def identifier(raw: bytes, at: int) -> int:
        got = window(raw, at, 16)
        return ((got & 0xFF) << 8) | (got >> 8)

    def tail(raw: bytes, at: int, total: int) -> int | None:
        if at + 40 > total or window(raw, at + 32, 8) != 0xFF:
            return None
        plain = window(raw, at, 32)
        swapped = int.from_bytes(plain.to_bytes(4, "big"), "little")
        actor = 0 if not plain else (
            swapped if (swapped >> 16) <= MOST_ACTOR_PAGES else plain
        )
        if actor and (actor >> 16) > MOST_ACTOR_PAGES:
            return None
        if at + 40 == total:
            return actor
        if at + 56 > total:
            return None
        following = identifier(raw, at + 40)
        return None if not 0 < following <= MOST_COMMANDS else actor

    names = command_names()
    biggest = max(
        (payload for _n, from_server, payload in messages(path)
         if from_server and payload[:1] == bytes([MULTI]) and len(payload) > 100_000),
        key=len,
        default=None,
    )
    if biggest is None:
        return []
    total = len(biggest) * 8
    out, at = [], 8
    while at + 16 < total:
        command = identifier(biggest, at)
        if command > MOST_COMMANDS:
            break
        probe, end = at + 16, None
        while probe + 40 <= total:
            actor = tail(biggest, probe, total)
            if actor is not None:
                end = probe + 40
                out.append((names.get(command, "?"), command, (end - at) // 8, hex(actor)))
                break
            probe += 1
        if end is None:
            break
        at = end
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", nargs="+", type=pathlib.Path)
    parser.add_argument(
        "--arrival",
        action="store_true",
        help="take the capture's largest batch apart command by command: what a map "
        "server sends before it starts ticking, in order",
    )
    parser.add_argument(
        "--actor",
        type=lambda value: int(value, 0),
        help="report effects only for this actor, e.g. 0x00010054",
    )
    parser.add_argument("--most", type=int, default=25)
    args = parser.parse_args(argv[1:])

    if args.arrival:
        for capture in args.captures:
            found = arrival(capture)
            print(f"== {capture.name}: {len(found)} commands in the arrival batch")
            for index, (name, command, size, actor) in enumerate(found):
                if index < args.most or index >= len(found) - 4:
                    print(f"   {index:4d} 0x{command:04X} {name:40s} {size:8d} o {actor}")
                elif index == args.most:
                    print(f"        ... {len(found) - args.most - 4} more ...")
        return 0

    for capture in args.captures:
        got = report(capture)
        print(f"== {capture.name}: {got['messages']} messages")
        for _number, what in got["did"]:
            print(f"   the player {what}")
        if got["cast"]:
            print(f"   skills cast ({sum(got['cast'].values())}):")
            for name, count in got["cast"].most_common(args.most):
                print(f"      {name:40s} x{count}")
        if got["creatures"]:
            print("   creatures described:")
            for name, count in got["creatures"].most_common(args.most):
                print(f"      {name:60s} x{count}")
        if got["taken"]:
            print("   items picked up:")
            for actor, template in got["taken"]:
                print(f"      {actor:#010x}  {template}")
        holders = got["landed"]
        if args.actor is not None:
            holders = {args.actor: holders.get(args.actor, collections.Counter())}
        for holder, counter in sorted(
            holders.items(), key=lambda pair: -sum(pair[1].values())
        )[:4]:
            print(f"   effects on {holder:#010x} ({sum(counter.values())} elements):")
            for name, count in counter.most_common(args.most):
                print(f"      {name:60s} x{count}")
        if got["unreadable"]:
            print(f"   {got['unreadable']} status effect commands did not read back")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
