"""Reusable read-only SQLite connections, one per thread per database.

Opening ``static.db4`` costs ~56us -- small on its own, but the combat path did
it per item-set effect resolution and per skill cast, so a busy server paid it
thousands of times a second for a file that never changes.

Connections are cached **per thread** rather than shared. A single shared
connection would need ``check_same_thread=False`` plus external serialisation,
and this server runs timer threads alongside the receive thread; a per-thread
cache gets the same saving with none of that risk.

The cache is keyed by the path as given. Resolving it first would let
differently-spelled paths share a connection, but ``Path.resolve()`` touches
the filesystem and measured *slower than simply reopening the database* -- it
turned the whole optimisation into a no-op. Callers pass a consistent path, so
a plain string key is both correct here and actually fast.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

_local = threading.local()


def connect(path: str | Path) -> sqlite3.Connection:
    """Return this thread's read-only connection for ``path``."""

    cache = getattr(_local, "connections", None)
    if cache is None:
        cache = _local.connections = {}
    connection = cache.get(path)
    if connection is not None:
        return connection
    connection = sqlite3.connect(
        f"file:{Path(path).as_posix()}?mode=ro", uri=True
    )
    cache[path] = connection
    return connection


def close_all() -> None:
    """Close this thread's cached connections (test/shutdown hygiene)."""

    cache = getattr(_local, "connections", None)
    if not cache:
        return
    for connection in cache.values():
        try:
            connection.close()
        except sqlite3.Error:
            pass
    cache.clear()
