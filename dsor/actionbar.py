"""The action bar, and putting skills on it.

The bar is not the skill book. Granting a skill in the book tells the client the
character *owns* it; the bar is what the character can press, and the two are stored
separately. This server granted all eighteen warrior skills in the book and the bar
stayed as it was recorded -- one slot, ``angrystrike``, sixteen empty -- so pressing
anything else did nothing. The client confirms it itself: all 180 ``QuickSlotsCommand``
messages of one session report that same single entry.

**Where it lives.** Not in the 733 KB PlayerInitialization: neither the string nor the
0xFF runs appear anywhere in it. It is in the 631 KB zone content, right behind the
skill book, as ten consecutive records of exactly 680 bits:

    u32   17          how many slots
    u32   0
    slot  x17         either 0xFFFFFFFF for empty, or u16 length + the skill's id

Ten of them, back to back at bit 103610, 104290, 104970 and on, each identical. Which
is the client's several bars.

**Why filling it grows the blob.** An empty slot is 32 bits and a filled one is 16 plus
eight per character, so every skill added is 64 to 80 bits more. A BitStream is read
sequentially and states no offsets, so growth is safe as far as the reader is concerned
-- and the frame layer computes the declared bit length from the payload, so the length
follows. That is the assumption this makes, and it is the one to suspect first if the
client refuses the state.
"""

from __future__ import annotations

import logging

from raknet.bitstream import BitReader, BitWriter

log = logging.getLogger("actionbar")

#: An empty slot, as 32 bits.
EMPTY = 0xFFFFFFFF

#: How many slots a record holds. Seventeen in every recorded bar.
SLOTS = 17

#: A record's size when only the first slot is filled with an eleven-character id.
RECORDED_BITS = 680

#: Where the first record starts in the recorded zone content. Derived rather than
#: trusted: :func:`find_bars` locates them by shape.
FIRST_BAR = 103610

#: A skill id longer than this means the walk has lost the boundary.
LONGEST_ID = 48

#: How many bars the recorded state holds.
BARS = 10


def _read_record(blob: bytes, at: int) -> tuple[list[str | None], int] | None:
    """The slots of the record at bit *at*, and where it ends. None if it is not one."""
    try:
        reader = BitReader(blob, at)
        count = reader.read_uint(32)
        if count != SLOTS:
            return None
        if reader.read_uint(32) != 0:
            return None
        slots: list[str | None] = []
        for _ in range(count):
            here = reader.position
            length = reader.read_uint(16)
            if length == 0xFFFF:
                reader.position = here
                if reader.read_uint(32) != EMPTY:
                    return None
                slots.append(None)
                continue
            if not 1 <= length <= LONGEST_ID:
                return None
            raw = bytes(reader.read_uint(8) for _ in range(length))
            try:
                name = raw.decode("ascii")
            except UnicodeDecodeError:
                return None
            if not name.replace("_", "").isalnum():
                return None
            slots.append(name)
        return slots, reader.position
    except (IndexError, ValueError):
        return None


def find_bars(blob: bytes, start: int = FIRST_BAR) -> list[tuple[int, int, list]]:
    """Every action bar record in *blob*: ``(start bit, end bit, slots)``.

    Walks forward from the first one, since they are back to back. Returns an empty
    list when *start* does not hold a record, so a caller can leave the state alone
    rather than write into the middle of something else.
    """
    found = []
    at = start
    while True:
        got = _read_record(blob, at)
        if got is None:
            break
        slots, end = got
        found.append((at, end, slots))
        at = end
    return found


def _write_record(writer: BitWriter, slots: list[str | None]) -> None:
    writer.write_uint(SLOTS, 32)
    writer.write_uint(0, 32)
    for index in range(SLOTS):
        name = slots[index] if index < len(slots) else None
        if not name:
            writer.write_uint(EMPTY, 32)
            continue
        raw = name.encode()
        writer.write_uint(len(raw), 16)
        for byte in raw:
            writer.write_uint(byte, 8)


def with_skills(blob: bytes, skills: list[str], start: int = FIRST_BAR) -> bytes:
    """*blob* with every action bar carrying *skills*, in order from slot zero.

    Returns the blob unchanged when no record is found, and says so in the log: a bar
    written at the wrong offset would corrupt whatever is there, and silently serving a
    corrupt player state is worse than serving one with an empty bar.
    """
    bars = find_bars(blob, start)
    if not bars:
        log.warning("no action bar at bit %d -- leaving the state alone", start)
        return blob
    wanted = [name for name in skills][:SLOTS]
    if all(bar[2][: len(wanted)] == wanted for bar in bars):
        return blob

    first = bars[0][0]
    last_end = bars[-1][1]
    writer = BitWriter()
    reader = BitReader(blob, 0)
    for _ in range(first):
        writer.write_uint(reader.read_uint(1), 1)
    for _ in bars:
        _write_record(writer, wanted)
    reader = BitReader(blob, last_end)
    total = len(blob) * 8
    for _ in range(total - last_end):
        writer.write_uint(reader.read_uint(1), 1)
    out = writer.to_bytes()
    log.info(
        "action bar: %d bar(s) filled with %d skill(s), state %d -> %d bytes",
        len(bars),
        len(wanted),
        len(blob),
        len(out),
    )
    return out


def bar_skills(character_class: str, level: int) -> list[str]:
    """The skills to put on the bar: one per cooldown category, in unlock order.

    The bar is a *quick slot* bar, not a skill bar: it also holds mounts, pets and
    consumables, which is why seventeen slots are mostly empty on a real character. So
    this fills it with skills only and leaves the rest to the player.

    **One per cooldown category** is the rule, and it is the database's own. A class's
    table lists variants beside their base -- ``bloody360`` and ``bloody360_Chimera``
    are both ``Skill03``, ``earthquake`` and ``true_earthquake`` both ``Skill15``, and
    ``chainlightning_SetLight2026_warrior`` is a set's version of a skill the warrior
    does not otherwise have. Every one of those shares its category with its base, and
    no two canonical skills share one: grouping by category and keeping one leaves
    exactly the fifteen the warrior can place, including all five the live service was
    seen putting on a real bar.

    Which of a category's skills is the canonical one comes from ``Groups``: the base
    lists its own id as the second group and a variant lists the base's. Requiring that
    also drops the skills an *item set* grants rather than a level --
    ``chainlightning_SetLight2026_warrior`` is the warrior's only entry in its cooldown
    category and would otherwise win it, while being a set's skill that a character
    without the set cannot use.

    The database also holds ``niwalk``, which never shipped as a class. Its skills carry
    only ``niwalk_skills`` and unlock at level 0, so the rule yields nothing for it --
    which is the right answer for unreleased content, and the reason not to bend the
    rule to accommodate it. The four playable classes are warrior, mage, ranger and
    dwarf.
    """
    from dsor import database

    rows = database.rows(
        "_Template_Skill", "Id", "CharClass", "UnlockLevel", "Groups", "CoolDownCategory"
    )
    if not rows:
        from dsor.skillbook import of_class

        held = of_class(character_class)
        return [
            name
            for name, (_index, unlock) in sorted(held.items(), key=lambda kv: kv[1][1])
            if unlock <= level
        ][:SLOTS]

    by_category: dict[str, list[tuple[int, bool, int, str]]] = {}
    for index, name, owner, unlock, groups, category in rows:
        if not name or owner != character_class or not category:
            continue
        try:
            at = int(unlock) if unlock not in (None, "") else 0
        except (TypeError, ValueError):
            at = 0
        if at > level:
            continue
        parts = [group for group in (groups or "").split(";") if group]
        if not (len(parts) >= 2 and parts[1] == name):
            continue
        by_category.setdefault(category, []).append((at, index, name))

    chosen = []
    for category, candidates in by_category.items():
        candidates.sort()
        at, _index, name = candidates[0]
        chosen.append((at, category, name))
    chosen.sort()
    return [name for _at, _category, name in chosen][:SLOTS]
