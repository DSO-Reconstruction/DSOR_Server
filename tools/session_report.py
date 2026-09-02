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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", nargs="+", type=pathlib.Path)
    parser.add_argument(
        "--actor",
        type=lambda value: int(value, 0),
        help="report effects only for this actor, e.g. 0x00010054",
    )
    parser.add_argument("--most", type=int, default=25)
    args = parser.parse_args(argv[1:])

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
