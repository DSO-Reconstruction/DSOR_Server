"""The client's own database, loaded into memory at start.

``data/static.db4`` is the client's ``export_win32/db/static.db4``, lifted out of its
asset bundles. It is a plain SQLite file despite the extension: 197 tables and 148,265
rows. Put it in ``data/`` and this module loads it into an in-memory copy once, at
startup, so every lookup afterwards is a dict hit.

Why this exists rather than more generated tables. The generated modules are snapshots
taken through a filter, and the filter was the problem: ``dsor/effects.py`` holds 458
of the database's 6,703 status effects -- those a class skill names, plus what their
auras reach -- and ``dsor/monsters.py`` holds 23 of 7,985. Everything outside those
sets was simply unknown to the server, which is no way to serve five classes. A
snapshot also drifts: the row a name sits on is the number that goes on the wire, so a
table regenerated against one database and elements extracted against another disagree
by however many rows were inserted between them, and the symptom is a skill showing
another skill's effect.

    **The index convention lives here and nowhere else.** A row's wire index is
    ``rowid + 15``. The client's ``effectInfos`` array is sized to the table's own row
    count and filled in order -- ``StatusEffectManager::Load()`` does exactly that, and
    ``StatusEffectTableRowToId`` indexes it directly -- but the array the wire indexes
    has sixteen entries in front of the table's first row.

    Measured, and only measurable against traffic this server did not write. A capture
    of the live service in which the operator cast Dragon Hide three times, Spike Shield
    once, Furious Battle Cry once and Ground Breaker once gives eleven index-to-effect
    correspondences, and every one of them is ``rowid + 15``:

        wire 5184, 5185, 5194  ->  skill_frenzyshout_buff_armor, _resistance, _lifeleech
        wire 5169, 5542        ->  warrior_spikedShield_buff, _armor_trigger
        wire 5165, 5518, 5166, 5168, 5519, 6212 -> warshout's six
        wire 5158              ->  skill_seismicslam_debuff_armor

    It was ``rowid - 1`` here for a long time, and the checks that confirmed it were
    circular: they ran over captures that are for the most part *this emulator's own
    traffic*, where these very indices had been written with ``rowid - 1``, so reading
    them back the same way returned the names that had been put in. Half of the 144
    captures are this server's. The error is worth stating plainly because it produced,
    for weeks, exactly the symptom the operator kept reporting: sending 5168 and 5169
    for Dragon Hide made the client show "Power of Smash" and "Spike Shield", which are
    the effects sixteen rows earlier.

The generated modules stay as the fallback for a machine without the file -- the tests
run that way -- and as the record of how each table was read. When the database is
present it wins, because it is the same data without the filter.
"""

from __future__ import annotations

import logging
import pathlib
import sqlite3
import threading

log = logging.getLogger("database")

#: Where the file belongs, relative to the repository root.
DATA = pathlib.Path(__file__).resolve().parent.parent / "data"

#: The name the client gives it. ``.db4`` is SQLite; the extension is Bigpoint's.
STATIC = "static.db4"

#: Also accepted, so an operator who already extracted it need not copy it twice.
ELSEWHERE = (
    pathlib.Path.home() / "dso/db/static.db4",
    pathlib.Path.home() / "dso/db/db_static.sqlite",
)

#: A row's wire index is its rowid minus this. One place per table, so it cannot drift.
#:
#: **It is not the same for every table.** Each one becomes an array on the client and
#: the array the wire indexes need not begin at the table's first row.
#:
#: ``_Template_Skill`` is ``rowid - 1``, and that one is confirmed by the *client's* own
#: traffic rather than by anything this server wrote: over 1,581 real casts the skill
#: command carries 1838 for angrystrike, 1846 for frenzyshout, 1854 for spikedShield,
#: which are those rows' rowids minus one.
#:
#: ``_Template_StatusEffect`` is ``rowid + 15``. See the module docstring.
DEFAULT_ROW_TO_INDEX = 1

ROW_TO_INDEX: dict[str, int] = {
    "_Template_StatusEffect": -15,
}


def offset_of(table: str) -> int:
    """How much to subtract from a rowid to get *table*'s wire index."""
    return ROW_TO_INDEX.get(table, DEFAULT_ROW_TO_INDEX)

_lock = threading.Lock()
_memory: sqlite3.Connection | None = None
_looked = False


def path() -> pathlib.Path | None:
    """Where the database is, or None when it is nowhere to be found."""
    here = DATA / STATIC
    if here.exists():
        return here
    for other in ELSEWHERE:
        if other.exists():
            return other
    return None


def connection() -> sqlite3.Connection | None:
    """The in-memory copy, loaded on first use, or None without the file.

    Loaded rather than opened. A query against the file goes to disk; the whole thing
    is 69 MB and the server reads it on the per-tick path, so it is copied into memory
    once with SQLite's own backup and never touched on disk again.

    ``check_same_thread=False`` because the transport, the tick and the console all
    read it. It is read-only after the load, and SQLite serialises reads on one
    connection, so there is nothing to guard beyond the load itself.
    """
    global _memory, _looked
    with _lock:
        if _memory is not None or _looked:
            return _memory
        _looked = True
        found = path()
        if found is None:
            log.info(
                "no %s in %s -- falling back on the generated tables, which cover "
                "458 of 6703 status effects and 23 of 7985 monsters",
                STATIC,
                DATA,
            )
            return None
        try:
            on_disk = sqlite3.connect(f"file:{found}?mode=ro", uri=True)
            memory = sqlite3.connect(":memory:", check_same_thread=False)
            on_disk.backup(memory)
            on_disk.close()
        except sqlite3.Error as problem:
            log.warning("cannot load %s: %s", found, problem)
            return None
        tables = memory.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        log.info("loaded %s into memory from %s: %d tables", STATIC, found, tables)
        _memory = memory
        return _memory


def rows(table: str, *columns: str) -> list[tuple]:
    """``(index, *columns)`` for every row of *table*, index-first.

    Empty when the database is absent, so a caller can fall back without asking
    whether it is there.
    """
    held = connection()
    if held is None:
        return []
    # Checked against the table, because SQLite will not complain. A double-quoted
    # name it does not recognise as a column is taken as a *string literal* -- so
    # asking for "Duration" on a table whose column is StatusEffectDuration returned
    # the word "Duration" for all 6,703 rows instead of an error. Silent wrong data is
    # worse than a stack trace.
    known = set(columns_of(table))
    unknown = [name for name in columns if name not in known]
    if unknown:
        raise KeyError(
            f"{table} has no column {', '.join(unknown)}"
            + (f" -- did you mean {sorted(n for n in known if unknown[0].lower() in n.lower())}?"
               if any(unknown[0].lower() in n.lower() for n in known) else "")
        )
    picked = ", ".join(f'"{name}"' for name in columns)
    try:
        shift = offset_of(table)
        return [
            (row[0] - shift, *row[1:])
            for row in held.execute(f'SELECT rowid, {picked} FROM "{table}"')
        ]
    except sqlite3.Error as problem:
        log.warning("cannot read %s: %s", table, problem)
        return []


def columns_of(table: str) -> list[str]:
    """The column names of *table*, or an empty list without the database."""
    held = connection()
    if held is None:
        return []
    try:
        return [row[1] for row in held.execute(f'PRAGMA table_info("{table}")')]
    except sqlite3.Error:
        return []


def available() -> bool:
    """Whether the database was found and loaded."""
    return connection() is not None


def forget() -> None:
    """Drop the in-memory copy. For tests that need the fallback path."""
    global _memory, _looked
    with _lock:
        if _memory is not None:
            _memory.close()
        _memory = None
        _looked = False
