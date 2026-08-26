#!/usr/bin/env python3
"""Read and edit the client's UI layouts -- the ``.bxml`` files under ``ui/``.

A layout is binary XML, and simpler than the name suggests::

    "LMXB"
    u32 x3            a header whose first two fields are not identified
    u32 ...           the tree: a flat run of (key, value) string indices,
                      0x7FFFFFFF as a sentinel
    the string pool   NUL-separated, at the end of the file

The tree refers to strings by **ordinal index**, not by byte offset. That is the fact
the whole of this rests on: a string's content and its length can both change without
disturbing anything, as long as the count and the order stay put. So an attribute is
edited by rewriting its value in the pool -- no re-serialising, no offset arithmetic,
and nothing touched that was not meant to be.

A widget begins at an ``id`` key and owns the pairs that follow it until the next
``id``. Button states -- ``normal``, ``pressed``, ``mouseover``, ``mouseoverpressed``,
``disabled`` and the ``glow*`` overlays -- are widgets in their own right that belong
to the button above them, so hiding a button means hiding those too. Their rects are
absolute, in the same 0..1 screen space as everything else, so a child keeps drawing
when its parent is collapsed; that is why this hides each one rather than the group.

Hiding is done by setting ``rect`` to zeros, and only for rect strings used *nowhere
else in the file*. A shared string is reported and left alone rather than being
rewritten on the chance that the other user did not matter.
"""

from __future__ import annotations

import pathlib
import struct
import sys

SENTINEL = 0x7FFFFFFF
HEADER_FIELDS = 3
ZERO_RECT = "0.000000,0.000000,0.000000,0.000000"

#: Widgets that belong to the button they follow rather than standing on their own.
STATES = frozenset(
    {"normal", "pressed", "mouseover", "mouseoverpressed", "disabled"}
)


def _pool_start(raw: bytes, ints: int) -> int:
    return 4 + ints * 4


class Layout:
    """One parsed ``.bxml``."""

    def __init__(self, raw: bytes) -> None:
        if raw[:4] != b"LMXB":
            raise ValueError("not a bxml layout")
        self.raw = raw
        # The pool begins where the integer stream stops. The string count is the
        # third header field, so the pool can be found from the end: take the last
        # `count` NUL-terminated runs. Simpler and exact: scan for the offset at which
        # splitting on NUL yields that many strings.
        count = struct.unpack_from("<I", raw, 4 + 8)[0]
        at = len(raw)
        found = None
        # Walk back over NUL-separated strings until there are `count` of them.
        end = len(raw)
        if raw.endswith(b"\x00"):
            end -= 1
        seen = 0
        cursor = end
        while cursor > 0 and seen <= count:
            cursor = raw.rfind(b"\x00", 0, cursor)
            if cursor < 0:
                break
            seen += 1
            if seen == count:
                found = cursor + 1
                break
        if found is None:
            raise ValueError("cannot locate the string pool")
        self.pool_at = found
        ints = (self.pool_at - 4) // 4
        self.nums = list(struct.unpack_from(f"<{ints}I", raw, 4))
        body = raw[self.pool_at :]
        if body.endswith(b"\x00"):
            body = body[:-1]
        self.strings = [s.decode("ascii", "replace") for s in body.split(b"\x00")]
        if len(self.strings) != count:
            raise ValueError(
                f"header says {count} strings, pool holds {len(self.strings)}"
            )

    def name(self, value: int) -> str | None:
        if value == SENTINEL or value >= len(self.strings):
            return None
        return self.strings[value]

    def widgets(self) -> list[dict]:
        """Every widget, in file order, with each attribute's string index."""
        out: list[dict] = []
        current = None
        at = HEADER_FIELDS
        while at + 1 < len(self.nums):
            key, value = self.name(self.nums[at]), self.nums[at + 1]
            if key == "id":
                current = {"id": self.name(value), "attrs": {}}
                out.append(current)
            elif current is not None and key is not None:
                current["attrs"].setdefault(key, value)
            at += 2
        return out

    def uses(self, index: int) -> int:
        """How many times the string at *index* appears anywhere in the tree."""
        return self.nums.count(index)

    def rewrite(self, index: int, text: str) -> None:
        """Give the string at *index* new content, in place."""
        if index >= len(self.strings):
            raise IndexError(index)
        self.strings[index] = text

    def to_bytes(self) -> bytes:
        pool = b"\x00".join(s.encode("ascii") for s in self.strings) + b"\x00"
        return self.raw[: self.pool_at] + pool


def group(widgets: list[dict]) -> list[tuple[str, list[dict]]]:
    """Widgets grouped under the named one they belong to."""
    out: list[tuple[str, list[dict]]] = []
    for widget in widgets:
        name = widget["id"] or ""
        if out and (name in STATES or name.startswith("glow")):
            out[-1][1].append(widget)
        else:
            out.append((name, [widget]))
    return out


def hide(layout: Layout, wanted: set[str]) -> tuple[list[str], list[str]]:
    """Collapse every widget in *wanted*, and the states belonging to it.

    Returns the rect strings rewritten and the ones refused for being shared.
    """
    targets: set[int] = set()
    for name, members in group(layout.widgets()):
        if name not in wanted:
            continue
        for widget in members:
            if "rect" in widget["attrs"]:
                targets.add(widget["attrs"]["rect"])

    others: set[int] = set()
    for name, members in group(layout.widgets()):
        if name in wanted:
            continue
        for widget in members:
            if "rect" in widget["attrs"]:
                others.add(widget["attrs"]["rect"])

    done, refused = [], []
    for index in sorted(targets):
        if index in others:
            refused.append(layout.strings[index])
            continue
        done.append(layout.strings[index])
        layout.rewrite(index, ZERO_RECT)
    return done, refused


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        print("usage: uilayout.py list <file.bxml>")
        print("       uilayout.py hide <file.bxml> <widget> [widget...]")
        return 2
    what, path = argv[1], pathlib.Path(argv[2])
    layout = Layout(path.read_bytes())
    if what == "list":
        for name, members in group(layout.widgets()):
            rect = members[0]["attrs"].get("rect")
            print(f"{name:34s} {layout.strings[rect] if rect is not None else '-':40s}"
                  f" +{len(members)-1} state(s)" if len(members) > 1 else
                  f"{name:34s} {layout.strings[rect] if rect is not None else '-'}")
        return 0
    if what == "hide":
        done, refused = hide(layout, set(argv[3:]))
        path.write_bytes(layout.to_bytes())
        print(f"{len(done)} rect(s) collapsed in {path.name}")
        for text in refused:
            print(f"  refused, shared elsewhere: {text}")
        return 0
    print(f"unknown command {what}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
