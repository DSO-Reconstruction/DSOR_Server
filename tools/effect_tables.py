#!/usr/bin/env python3
"""Generate dsor/measured.py from the captures.

The fields a status effect element carries that this server cannot compute -- the two
constants, the tail byte, the flags -- are written here from what a real server was
measured sending, per effect. Run it again after a new capture:

    python3 tools/effect_tables.py ~/dso-capture/session-*.jsonl

**Only the official server's captures.** This matters more than it looks. The captures
hold both sides of this project's history -- sessions against the live service and
sessions against this emulator -- and taking the commonest value across all of them fed
this server's own mistakes back in as ground truth. Concretely: the old code sent stun
elements with no vectors, so ``TAIL[debuff_cc_stun]`` came out as -1 while every element
the live service sent for it carries 2 and a pair of vectors. A table generated that way
measures nothing.

So each capture is classified first. ``a0001_tutorial_heal_on_low_health`` is the
signature of the old emulator, which replayed it on the player 25 times a second;
``skill_frenzyshout_buff_lifeleech`` and warshout's two named buffs only ever came from
the live service. A capture showing the first and not the second is dropped.

**And the context is separated from the effect.** The four flags, the trailing flag and
the signed byte depend on whether the element names a causer, not on which effect it is:
all 402 measured elements with no causer carry flags (0,0,0,1), and the ones with a
causer vary, 66 of them stopping before their parameters. A single table per effect
conflated the two, so they are kept apart here.

What it reads is stated so the numbers can be checked: 313,301 real 0x004F messages
across 144 captures, of which 304 did not parse (99.9%).
"""
from __future__ import annotations

import collections
import json
import pathlib
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from raknet.bitstream import BitReader  # noqa: E402
from raknet.datagram import parse_datagram_header  # noqa: E402
from raknet.frame import parse_frames  # noqa: E402

SERVER, COMMAND = 0x85, 0x004F

#: The effect the old emulator replayed on the player every tick. Its presence marks a
#: capture as this server's own output rather than the live service's.
EMULATOR_MARK = "a0001_tutorial_heal_on_low_health"

#: Effects only the live service was ever seen sending.
OFFICIAL_MARKS = (
    "skill_frenzyshout_buff_lifeleech",
    "skill_warshout_buff_mightybash",
    "skill_warshout_buff_angrystrike",
)


def walk_actor(payload: bytes, at: int = 24) -> int | None:
    """The actor a 0x004F names, which is the actor its elements are on."""
    from dsor import statuseffect

    got = statuseffect.decode(payload, at)
    return None if got is None else got.actor


def walk(payload: bytes, at: int = 24):
    r = BitReader(payload, at)
    total = len(payload) * 8
    try:
        r.read_uint(1)
        count = r.read_uint(32)
        if count > 64:
            return None
        out = []
        for _ in range(count):
            index = r.read_uint(16)
            fields = [r.read_uint(32) for _ in range(8)]
            flags = tuple(r.read_uint(1) for _ in range(4))
            entry = dict(index=index, fields=fields, flags=flags, params=[],
                         more=None, byte=None, vectors=None)
            out.append(entry)
            if not flags[3]:
                continue
            n = r.read_uint(32)
            if n > 16:
                return None
            entry["params"] = [
                struct.unpack("<f", r.read_uint(32).to_bytes(4, "little"))[0]
                for _ in range(n)
            ]
            entry["more"] = r.read_uint(1)
            raw = r.read_uint(8)
            entry["byte"] = raw - 256 if raw > 127 else raw
            if entry["more"] or entry["byte"] != -1:
                entry["vectors"] = [
                    struct.unpack("<f", r.read_uint(32).to_bytes(4, "little"))[0]
                    for _ in range(6)
                ]
        r.read_uint(32)
        if r.read_uint(8) != 0xFF or total - r.position > 7:
            return None
        return out
    except Exception:
        return None


def main(argv: list[str]) -> int:
    captures = [pathlib.Path(a) for a in argv[1:] if a.endswith(".jsonl")]
    if not captures:
        captures = [
            c for c in sorted(pathlib.Path.home().glob("dso-capture/session-*.jsonl"))
            if c.stat().st_size > 1000
        ]
    from dsor import effects as table

    emulator = table.wire_of(EMULATOR_MARK)
    official = {w for w in (table.wire_of(n) for n in OFFICIAL_MARKS) if w is not None}

    seen: dict[int, list[dict]] = collections.defaultdict(list)
    read = missed = dropped = 0
    for capture in captures:
        try:
            text = capture.read_text(errors="ignore")
        except OSError:
            continue
        # Whose traffic is this? Read once, before anything is kept.
        marks_emulator = marks_official = 0
        harvest: list[list[dict]] = []
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except Exception:
                continue
            if "hex" not in record or not record.get("from_server"):
                continue
            try:
                raw = bytes.fromhex(record["hex"])
                _header, offset = parse_datagram_header(raw)
                frames = parse_frames(raw, offset)
            except Exception:
                continue
            for frame in frames:
                payload = frame.payload
                if (not payload or payload[0] != SERVER or len(payload) < 4
                        or frame.split_count is not None):
                    continue
                if (payload[1] | (payload[2] << 8)) != COMMAND:
                    continue
                found = walk(payload)
                if found is None:
                    missed += 1
                    continue
                read += 1
                actor = walk_actor(payload)
                for entry in found:
                    entry["self"] = entry["fields"][7] == actor
                    if entry["index"] == emulator:
                        marks_emulator += 1
                    elif entry["index"] in official:
                        marks_official += 1
                harvest.append(found)
        # Any trace of this emulator drops the capture, even one that also shows the
        # live service. Of 144 captures: 71 are purely this server's, 7 purely the
        # service's, 7 hold both, and 56 hold no marker either way -- and a capture
        # holding both would mix this server's elements into the measurement of the
        # service's, which is the whole thing being guarded against.
        if marks_emulator:
            dropped += 1
            print(f"  {capture.name}: ecartee (emulateur)", file=sys.stderr, flush=True)
            continue
        for found in harvest:
            for entry in found:
                # Split on whether field 7 is the message's own actor, not on whether
                # it is zero: field 7 *is* an actor and an actor is never zero, so the
                # zero test put every element in one bucket and split nothing.
                key = (entry["index"], not entry.get("self", False))
                keep = seen.setdefault(key, [])
                if len(keep) < 60:
                    keep.append(entry)
        print(f"  {capture.name}", file=sys.stderr, flush=True)

    def pick(key: str, caused: bool, transform=lambda v: v):
        """The commonest value of *key*, per effect, for one context."""
        out = {}
        for (index, has_causer), entries in seen.items():
            if has_causer is not caused:
                continue
            values = [e[key] for e in entries if e[key] is not None]
            if values:
                out[index] = transform(
                    collections.Counter(values).most_common(1)[0][0]
                )
        return dict(sorted(out.items()))

    def field(number: int, caused: bool):
        out = {}
        for (index, has_causer), entries in seen.items():
            if has_causer is not caused:
                continue
            values = [e["fields"][number] for e in entries]
            out[index] = collections.Counter(values).most_common(1)[0][0]
        return dict(sorted(out.items()))

    def flags_per_effect():
        out = {}
        seen_flags = collections.defaultdict(collections.Counter)
        for (index, _has), entries in seen.items():
            for entry in entries:
                seen_flags[index][tuple(entry["flags"])] += 1
        for index in sorted(seen_flags):
            out[index] = seen_flags[index].most_common(1)[0][0]
        return out

    samples: dict[int, int] = collections.Counter()
    for (index, _has), entries in seen.items():
        samples[index] += len(entries)

    here = pathlib.Path(__file__).resolve().parent.parent / "dsor/measured.py"
    kept = len({name for name in seen})
    lines = [
        '"""What the live service put in a status effect element, per effect.',
        "",
        "Generated by tools/effect_tables.py -- do not edit by hand.",
        "",
        f"Read from {len(captures)} captures, of which {dropped} were dropped as this",
        f"emulator's own traffic rather than the live service's. {read} StatusEffectCommand",
        f"messages parsed, {missed} refused "
        f"({100 * read / max(read + missed, 1):.1f}%).",
        "",
        "Each table is split by context, because the fields depend on it: an element that",
        "names a causer -- a debuff someone put on a creature -- carries different values",
        "from a buff an actor put on itself, and one table for both averaged the two into",
        "a value neither of them has.",
        "",
        "The four flags are not here. All 402 measured elements with no causer carry",
        "(0,0,0,1) with no exception, so it is a rule in dsor/world.py rather than a table.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "#: How many elements each effect was measured from, both contexts together.",
        f"SAMPLES: dict[int, int] = {dict(sorted(samples.items()))!r}",
        "",
        "#: Field 2, for an effect an actor put on itself.",
        f"SECOND: dict[int, int] = {field(2, False)!r}",
        "",
        "#: Field 2, for one a causer put on someone.",
        f"SECOND_CAUSED: dict[int, int] = {field(2, True)!r}",
        "",
        "#: Field 6.",
        f"SIXTH: dict[int, int] = {field(6, False)!r}",
        "",
        "#: Field 6, caused.",
        f"SIXTH_CAUSED: dict[int, int] = {field(6, True)!r}",
        "",
        "#: The signed byte behind the parameters.",
        f"TAIL: dict[int, int] = {pick('byte', False)!r}",
        "",
        "#: The same, caused.",
        f"TAIL_CAUSED: dict[int, int] = {pick('byte', True)!r}",
        "",
        "#: The flag in front of that byte.",
        f"MORE: dict[int, int] = {pick('more', False)!r}",
        "",
        "#: The same, caused.",
        f"MORE_CAUSED: dict[int, int] = {pick('more', True)!r}",
        "",
        "#: The four flags, per effect. Measured per effect and not derived from context:",
        "#: skill_frenzyshout_buff_armor carries (0,0,0,1) in all 34 of its measured",
        "#: elements while skill_laceratingstrike_debuff_armor carries (0,0,1,1) in all",
        "#: four of its, and whether field 7 is the message's own actor does not predict",
        "#: either -- armour's field 7 is the actor 18 times and is not 16 times, with the",
        "#: same flags throughout.",
        f"FLAGS: dict[int, tuple] = {flags_per_effect()!r}",
    ]
    everything = collections.Counter()
    for entries in seen.values():
        for entry in entries:
            if entry["vectors"]:
                everything[tuple(entry["vectors"])] += 1
    common = list(everything.most_common(1)[0][0]) if everything else [0.0] * 6
    lines += [
        "",
        "#: The two float3 behind the parameters. Identical to the bit across five",
        "#: different captures, so it is a constant and not the position it was once",
        "#: taken for. The aura effects are the exception, and they are below.",
        f"VECTORS: tuple = {tuple(common)!r}",
        "",
        "#: The effects whose vectors differ: an aura carries its radius and a place.",
        "PER_EFFECT_VECTORS: dict[int, tuple] = {",
    ]
    per_effect = {}
    for (index, _has), entries in seen.items():
        vectors = collections.Counter(
            tuple(e["vectors"]) for e in entries if e["vectors"]
        )
        if not vectors:
            continue
        best = list(vectors.most_common(1)[0][0])
        if best != common:
            per_effect[index] = tuple(best)
    for index in sorted(per_effect):
        lines.append(f"    {index}: {per_effect[index]!r},")
    lines.append("}")
    here.write_text("\n".join(lines) + "\n")
    print(f"{len(samples)} effects, {dropped} captures dropped -> {here}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
