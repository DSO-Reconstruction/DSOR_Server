"""The static database the dsor client must hold, published by the account service.

The dsor client (the Rust client in DSOR_Client) downloads the original
client's files from Bigpoint's CDN, including its ``static.db4``. This server
may run with another one: its own rows (the admin buffs of
``tools/install_admin_effects.py``) or another build's. Status effects travel
by row (wire id = rowid - 1), so the client's copy must be this server's,
byte for byte. The account service offers it beside the login::

    GET  /api/content             {"version", "static_db": {"sha256", "size", "packed_size"} or null}
    POST /api/content/static-db   {session_id} -> the database packed as the CDN
                                  packs files: b"__ZN", the size (u32 LE), zlib

Nothing the official client or its launcher use changes: they never ask for
these. Only a signed-in player may download, a few times per address in a
quarter of an hour. The file is read whole between two identical ``stat``
calls, so a download never mixes two revisions while a tool rewrites it, and
it is hashed and packed once per revision.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import struct
import threading
import time
import zlib


CONTENT_VERSION = 1
# A larger file is not a static database; refuse to read it into memory.
MAX_STATIC_DB_BYTES = 512 * 1024 * 1024
DOWNLOADS_PER_WINDOW = 6
DOWNLOAD_WINDOW_SECONDS = 900.0


class StaticDatabasePublisher:
    """Hash and packed copy of one static database, rebuilt when it changes."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._revision: tuple[int, int] | None = None
        self._sha256 = ""
        self._size = 0
        self._packed = b""

    def _snapshot(self) -> tuple[tuple[int, int], bytes]:
        for _ in range(5):
            before = os.stat(self.path)
            if before.st_size > MAX_STATIC_DB_BYTES:
                raise ValueError(f"{self.path} is {before.st_size} bytes, more than a static database")
            data = self.path.read_bytes()
            after = os.stat(self.path)
            revision = (after.st_size, after.st_mtime_ns)
            if (before.st_size, before.st_mtime_ns) == revision and len(data) == after.st_size:
                return revision, data
            time.sleep(0.2)
        raise RuntimeError(f"{self.path} keeps changing; try again")

    def _current(self) -> bool:
        """Bring the cache to the file on disk; False when there is no file."""
        try:
            stat = os.stat(self.path)
        except FileNotFoundError:
            self._revision = None
            return False
        if self._revision != (stat.st_size, stat.st_mtime_ns):
            revision, data = self._snapshot()
            self._sha256 = hashlib.sha256(data).hexdigest()
            self._size = len(data)
            self._packed = b"__ZN" + struct.pack("<I", len(data)) + zlib.compress(data, 6)
            self._revision = revision
        return True

    def describe(self) -> dict | None:
        """The manifest entry, or None when the server has no database file."""
        with self._lock:
            if not self._current():
                return None
            return {"sha256": self._sha256, "size": self._size, "packed_size": len(self._packed)}

    def packed(self) -> tuple[str, bytes] | None:
        """(sha256 of the database, its packed bytes), or None without a file."""
        with self._lock:
            if not self._current():
                return None
            return self._sha256, self._packed
