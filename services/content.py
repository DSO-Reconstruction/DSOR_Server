"""The static database the dsor client must hold, published by the account service.

The dsor client (the Rust client in DSOR_Client) downloads the original
client's files from Bigpoint's CDN, including its ``static.db4``. This server
may run with another one: its own rows (the admin buffs of
``tools/install_admin_effects.py``) or another build's. Status effects travel
by row (wire id = rowid - 1), so the client's copy must be this server's,
byte for byte. The account service offers it beside the login::

    GET  /api/content             {"version", "static_db": {"sha256", "size", "packed_size"} or null,
                                   "maps": {"sha256", "files", "size", "packed_size"} or null}
    POST /api/content/static-db   {session_id} -> the database packed as the CDN
                                  packs files: b"__ZN", the size (u32 LE), zlib
    POST /api/content/maps        {session_id} -> every data/maps/*.db4 in one
                                  bundle (MapDatabasesPublisher)

The map databases travel the same way: NewNPC, PropInfo and ExitInfo name
level instances by their Guid, and Bigpoint's builds give every row a new
one (the October 22 build's maps share no Guid with the October 20 build's
this server was set up with), so the client converts its maps from these.

Nothing the official client or its launcher use changes: they never ask for
these. Only a signed-in player may download, a few times per address in a
quarter of an hour. The file is read whole between two identical ``stat``
calls, so a download never mixes two revisions while a tool rewrites it, and
it is hashed and packed once per revision.

Season Pass rewards edited in the CMS live beside the database
(``DSO_SEASON_PASS_CONFIG``); the published copy carries them in its reward
table, updated in place, so the client shows what the server grants.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import threading
import time
import zlib

from dso_server.gameplay.season_pass import (
    effective_season_pass_rewards, load_season_pass_overrides, write_season_pass_rewards,
)
from dso_server.paths import DEFAULT_SEASON_PASS_CONFIG_PATH


CONTENT_VERSION = 2
# A larger file is not a static database; refuse to read it into memory.
MAX_STATIC_DB_BYTES = 512 * 1024 * 1024
DOWNLOADS_PER_WINDOW = 6
DOWNLOAD_WINDOW_SECONDS = 900.0
# The map bundle is ~77 MB packed (656 MB of databases): fewer per address.
MAP_DOWNLOADS_PER_WINDOW = 3
MAP_BUNDLE_MAGIC = b"DSORMAP1"


class StaticDatabasePublisher:
    """Hash and packed copy of one static database, rebuilt when it changes."""

    def __init__(self, path: str | os.PathLike[str],
                 season_pass_config: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path)
        self.season_pass_config = Path(season_pass_config or DEFAULT_SEASON_PASS_CONFIG_PATH)
        self._lock = threading.Lock()
        self._revision: tuple | None = None
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

    def _config_revision(self) -> tuple[int, int] | None:
        try:
            stat = os.stat(self.season_pass_config)
        except FileNotFoundError:
            return None
        return stat.st_size, stat.st_mtime_ns

    def _with_season_pass(self, data: bytes) -> bytes:
        overrides = load_season_pass_overrides(self.season_pass_config)
        if not any(overrides.values()):
            return data
        with tempfile.TemporaryDirectory(prefix="dso-static-db-") as directory:
            copy = Path(directory) / "static.db4"
            copy.write_bytes(data)
            try:
                with closing(sqlite3.connect(copy)) as database:
                    with database:
                        for reward_set, rows in effective_season_pass_rewards(database, overrides).items():
                            write_season_pass_rewards(database, reward_set, rows)
            except (sqlite3.Error, ValueError) as exc:
                print(f"[Content] publishing static.db4 without Season Pass edits: {exc}")
                return data
            return copy.read_bytes()

    def _current(self) -> bool:
        """Bring the cache to the file on disk; False when there is no file."""
        try:
            stat = os.stat(self.path)
        except FileNotFoundError:
            self._revision = None
            return False
        if self._revision != ((stat.st_size, stat.st_mtime_ns), self._config_revision()):
            config = self._config_revision()
            revision, data = self._snapshot()
            data = self._with_season_pass(data)
            self._sha256 = hashlib.sha256(data).hexdigest()
            self._size = len(data)
            self._packed = b"__ZN" + struct.pack("<I", len(data)) + zlib.compress(data, 6)
            self._revision = (revision, config)
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


def map_bundle_sha256(entries: list[tuple[str, str]]) -> str:
    """The bundle's identity: sha256 of its sorted "name<TAB>sha256<LF>" lines."""
    lines = "".join(f"{name}\t{sha}\n" for name, sha in sorted(entries))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


class MapDatabasesPublisher:
    """Every ``<maps>/*.db4`` in one bundle, rebuilt when one of them changes.

    The bundle: ``MAP_BUNDLE_MAGIC``, the file count (u32 LE), then per file
    in name order: the name's length (u16 LE) and UTF-8 name, the database's
    sha256 (32 bytes), its size (u32 LE), the packed length (u32 LE) and the
    database packed as the CDN packs files (b"__ZN", size, zlib), so the
    client unpacks and checks one file at a time. Its identity is
    :func:`map_bundle_sha256` over the files' names and sha256s.
    """

    def __init__(self, maps_dir: str | os.PathLike[str]) -> None:
        self.maps_dir = Path(maps_dir)
        self._lock = threading.Lock()
        self._revision: tuple | None = None
        self._sha256 = ""
        self._size = 0
        self._files = 0
        self._bundle = b""

    def _listing(self) -> tuple[tuple[str, int, int], ...]:
        try:
            paths = sorted(self.maps_dir.glob("*.db4"))
        except OSError:
            return ()
        listing = []
        for path in paths:
            stat = path.stat()
            listing.append((path.name, stat.st_size, stat.st_mtime_ns))
        return tuple(listing)

    def _read(self, name: str) -> bytes:
        path = self.maps_dir / name
        for _ in range(5):
            before = os.stat(path)
            if before.st_size > MAX_STATIC_DB_BYTES:
                raise ValueError(f"{path} is {before.st_size} bytes, more than a map database")
            data = path.read_bytes()
            after = os.stat(path)
            if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns) and len(data) == after.st_size:
                return data
            time.sleep(0.2)
        raise RuntimeError(f"{path} keeps changing; try again")

    def _current(self) -> bool:
        """Bring the cache to the files on disk; False when there are none."""
        listing = self._listing()
        if not listing:
            self._revision = None
            return False
        if self._revision != listing:
            parts = [MAP_BUNDLE_MAGIC, struct.pack("<I", len(listing))]
            entries, size = [], 0
            for name, _, _ in listing:
                data = self._read(name)
                digest = hashlib.sha256(data)
                packed = b"__ZN" + struct.pack("<I", len(data)) + zlib.compress(data, 6)
                encoded = name.encode("utf-8")
                parts += [struct.pack("<H", len(encoded)), encoded, digest.digest(),
                          struct.pack("<II", len(data), len(packed)), packed]
                entries.append((name, digest.hexdigest()))
                size += len(data)
            self._bundle = b"".join(parts)
            self._sha256 = map_bundle_sha256(entries)
            self._size = size
            self._files = len(entries)
            # A file rewritten while it was read waits for the next request.
            self._revision = listing if self._listing() == listing else None
        return True

    def describe(self) -> dict | None:
        """The manifest entry, or None when the server has no map database."""
        with self._lock:
            if not self._current():
                return None
            return {"sha256": self._sha256, "files": self._files, "size": self._size,
                    "packed_size": len(self._bundle)}

    def packed(self) -> tuple[str, bytes] | None:
        """(the bundle's sha256, the bundle), or None without map databases."""
        with self._lock:
            if not self._current():
                return None
            return self._sha256, self._bundle
