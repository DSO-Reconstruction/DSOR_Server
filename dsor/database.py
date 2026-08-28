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
    ``rowid - 1``: the client loads the table into a zero-based array while SQLite
    numbers from one. Confirmed against the recording, whose single effect reads 1350
    on the wire and is row 1351, ``a0001_tutorial_heal_on_low_health``; and against the
    tutorial captures, where the monster debuffs come out as laceratingstrike's,
    mightyswing's and seismicslam's own -- while ``rowid`` unshifted turns
    ``debuff_dot_poison`` into ``debuff_movementspeed_relative`` and
    ``skill_mightyswing_debuff_reduce_damage`` into a mage talent.

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

#: A row's wire index is its rowid minus this. One place, so it cannot drift.
ROW_TO_INDEX = 1

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
    picked = ", ".join(f'"{name}"' for name in columns)
    try:
        return [
            (row[0] - ROW_TO_INDEX, *row[1:])
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
