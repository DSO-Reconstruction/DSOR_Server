"""The skill book, and which of its skills the character actually owns.

``SkillBookInfoCommand`` 0x0058 travels inside the 631 KB player state. Its shape
comes from the client's own serialiser, read through the command's vtable:

    count            8 bits, and the writer asserts count - 1 <= 0xFD
    per entry        the skill index as uint32, then three single bits
    trailer          the actor, then the terminator

Validated by decoding the replayed book with it: eighteen entries, every warrior
skill in order from angrystrike 1838 to spikedShield 1854 plus one event skill, and
the body ending exactly thirty-two bits before the next command — the trailer, to the
bit.

What the bits mean follows from the data rather than from reading. Of the eighteen
skills, only ``angrystrike`` has any bit set, and it is the first of the three; it is
also the only skill the character can use. So the first bit is ownership, and a skill
the client shows as unlocked by level but refuses to place is one the book lists with
that bit clear.

Which is what "I unlocked rageful swing but cannot equip it" was: ``mightyswing``,
index 1839, UnlockLevel 3 — the internal name of what the interface calls Rageful
Swing — listed in the book with its bit at zero.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The command this module rewrites.
SKILL_BOOK = 0x0058

#: Bits per entry: a uint32 and three flags.
ENTRY_BITS = 35


@dataclass(frozen=True)
class Entry:
    """One skill in the book."""

    index: int
    skill: int
    owned: bool
    #: Bit offset of the ownership flag, for rewriting it.
    owned_bit: int


def _bit(buf: bytes, at: int) -> int:
    byte, shift = divmod(at, 8)
    return buf[byte] >> (7 - shift) & 1


def _uint(buf: bytes, at: int, count: int) -> int:
    value = 0
    for step in range(count):
        value = (value << 1) | _bit(buf, at + step)
    return value


def _le32(buf: bytes, at: int) -> int:
    return int.from_bytes(
        bytes(_uint(buf, at + 8 * i, 8) for i in range(4)), "little"
    )


def find_book(message: bytes) -> int | None:
    """The bit offset of the book's own command id, or None.

    Found by searching for a terminator followed by the command id at any alignment,
    then confirming — the message is bit-packed, so the boundary is not byte-aligned
    and a byte-level search misses it entirely.
    """
    want = bytes([0xFF, SKILL_BOOK & 0xFF, SKILL_BOOK >> 8])
    size = len(message)
    value = int.from_bytes(message, "big")
    for shift in range(8):
        window = ((value << shift) & ((1 << (8 * size)) - 1)).to_bytes(size, "big")
        at = window.find(want)
        while at >= 0:
            bit = at * 8 + shift
            if bytes(_uint(message, bit + 8 * i, 8) for i in range(3)) == want:
                return bit
            at = window.find(want, at + 1)
    return None


def entries(message: bytes, book: int | None = None) -> list[Entry]:
    """Every skill the book lists."""
    at = find_book(message) if book is None else book
    if at is None:
        return []
    cursor = at + 24
    count = _uint(message, cursor, 8)
    cursor += 8
    out = []
    for index in range(count):
        skill = _le32(message, cursor)
        owned_bit = cursor + 32
        out.append(Entry(index, skill, bool(_bit(message, owned_bit)), owned_bit))
        cursor += ENTRY_BITS
    return out


def with_granted(message: bytes, skills: set[int]) -> bytes:
    """Return *message* with the ownership flag set for *skills*.

    One bit each, in place: nothing moves, so nothing else in a 631 KB message can be
    disturbed. A skill the book does not list cannot be granted this way, and is
    reported by the caller rather than silently ignored.
    """
    out = bytearray(message)
    for entry in entries(message):
        if entry.skill in skills:
            byte, shift = divmod(entry.owned_bit, 8)
            out[byte] |= 1 << (7 - shift)
    return bytes(out)


def granted(message: bytes) -> set[int]:
    """The skills the book says the character owns."""
    return {entry.skill for entry in entries(message) if entry.owned}


#: The warrior's skills, kept as the fallback for a machine without the database. It
#: matches _Template_Skill exactly -- all 18 entries, same indices, same unlock levels --
#: which is how the database read below was checked. It covers one class of five, and
#: that is the reason for the read: "if you make 1 class work without hardcoding spells,
#: all of the classes will be done". The database has 108 skills across warrior, mage,
#: ranger, dwarf and niwalk.
BOOK_SKILLS: dict[str, tuple[int, int]] = {
    'angrystrike': (1838, 1),
    'mightyswing': (1839, 3),
    'bloody360': (1840, 13),
    'bloody360_Chimera': (1841, 13),
    'mighty360': (1842, 15),
    'stuncharge': (1843, 9),
    'warshout': (1844, 19),
    'battlecry': (1845, 17),
    'frenzyshout': (1846, 21),
    'seismicslam': (1847, 25),
    'laceratingstrike': (1848, 23),
    'enragingleap': (1849, 7),
    'defiance': (1850, 29),
    'mightybash': (1851, 11),
    'earthquake': (1852, 31),
    'true_earthquake': (1853, 31),
    'spikedShield': (1854, 33),
    'chainlightning_SetLight2026_warrior': (1868, 0),
}


def skill_index(name: str) -> int | None:
    """The wire index of *name*, if the book lists it."""
    found = BOOK_SKILLS.get(name)
    return None if found is None else found[0]


def of_class(character_class: str = "") -> dict[str, tuple[int, int]]:
    """``name -> (wire index, unlock level)`` for one class, from the database.

    Falls back on :data:`BOOK_SKILLS` -- the warrior's, hard-coded -- when the database
    is absent, which is how the tests run. The two were compared: for the warrior they
    agree on all eighteen entries, indices and unlock levels alike.

    A skill whose UnlockLevel is missing or unparseable is treated as level zero, which
    is what the database itself uses for the event skills that are always listed.
    """
    from dsor import database

    rows = database.rows("_Template_Skill", "Id", "CharClass", "UnlockLevel")
    if not rows:
        return dict(BOOK_SKILLS)
    found: dict[str, tuple[int, int]] = {}
    for index, name, owner, unlock in rows:
        if not name or not owner:
            continue
        if character_class and owner != character_class:
            continue
        try:
            level = int(unlock) if unlock not in (None, "") else 0
        except (TypeError, ValueError):
            level = 0
        found[name] = (index, level)
    return found or dict(BOOK_SKILLS)


def up_to_level(level: int, character_class: str = "warrior") -> set[int]:
    """Every skill of *character_class* a character of *level* has unlocked.

    This is the whole of "you just send the available ones to the player based on the
    character level": the book lists a class's skills and one bit per entry says whether
    the character owns it, so unlocking a skill is setting that bit for every entry the
    level reaches.
    """
    return {
        index
        for _name, (index, unlock) in of_class(character_class).items()
        if unlock <= level
    }
