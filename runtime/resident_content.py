"""Static game content held in memory for the life of the process.

``static.db4``, the map databases, the map geometry and navigation files and
``quests.xml`` never change while the server runs: they are refreshed from a
client build with the server stopped (``tools/install_admin_effects.py``,
``tools/patch_k_map_return_gates.py``). Read from disk, every lookup a
player triggered went to the filesystem -- a monster template seen for the
first time, a skill's geometry, a map's exits on entry, the ``stat()`` behind
each revision-keyed cache -- on a volume the game client loads from too,
where one ``stat()`` has taken three seconds (runtime/file_revisions.py).

``install()`` reads all of it once, at startup:

* every database becomes a named in-memory SQLite database (``vfs=memdb``)
  that every connection in the process shares, and ``sqlite3.connect`` is
  routed so that each reader opening one of those files -- whatever path or
  URI spelling it uses -- gets a read-only connection to the in-memory copy;
* the other files are kept as bytes, served by :func:`read_bytes`;
* :func:`stat`, :func:`is_file` and :func:`resolved` answer for these files
  from what was observed when they were loaded, so revision-keyed caches
  and existence checks never touch the disk either.

The copies are read-only: a stray write raises instead of silently diverging
from the file. A file edited on disk takes effect at the next start, except
one the server itself rewrites (the CMS's ``events.json``), whose writer
calls :func:`refresh` -- ``file_revisions.invalidate`` does. Anything not
loaded (saves, tests' temporary databases) passes straight through to the
filesystem. ``DSO_RESIDENT_CONTENT=0`` turns all of this off.
"""

from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import unquote

# Captured before install() routes the module attribute.
_sqlite_connect = sqlite3.connect

_lock = threading.Lock()
# Normalised path -> read-only URI of its in-memory database.
_databases: dict[str, str] = {}
# Normalised path -> contents.
_files: dict[str, bytes] = {}
# Normalised path -> os.stat_result taken when the content was loaded.
_stats: dict[str, os.stat_result] = {}
# Normalised path -> the path resolved at load time.
_resolved: dict[str, Path] = {}
# One open read-write connection per in-memory database; a memdb database
# lives only while at least one connection to it is open.
_anchors: list[sqlite3.Connection] = []
_installed = False

# Files of a map, beside its .db4, that gameplay reads.
MAP_FILE_SUFFIXES = (".map", ".col", ".mvr")


def _key(path: str | os.PathLike[str]) -> str:
    # abspath and normcase are string operations; nothing here may stat.
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _database_key(database, uri: bool) -> str | None:
    """The normalised file a ``sqlite3.connect`` target names, if any."""

    if isinstance(database, bytes):
        database = os.fsdecode(database)
    try:
        text = os.fspath(database)
    except TypeError:
        return None
    if not isinstance(text, str) or text in ("", ":memory:"):
        return None
    if uri and text.startswith("file:"):
        text = unquote(text[5:].split("?", 1)[0].split("#", 1)[0])
        if text.startswith("//"):
            authority, _, rest = text[2:].partition("/")
            if authority not in ("", "localhost"):
                return None
            text = "/" + rest
        # file:///D:/x and file:/D:/x name D:/x.
        if len(text) >= 3 and text[0] == "/" and text[2] == ":":
            text = text[1:]
    elif uri and text.startswith(":memory:"):
        return None
    try:
        return _key(text)
    except (OSError, ValueError):
        return None


class ResidentConnection(sqlite3.Connection):
    """A connection to a resident copy, remembering the file it stands for.

    ``PRAGMA database_list`` names no file for an in-memory database, and
    callers key caches by that name (:func:`database_path`).
    """

    resident_path = ""


def connect(database, *args, **kwargs) -> sqlite3.Connection:
    """``sqlite3.connect``, opening resident databases from memory."""

    if _databases and len(args) < 7:  # a positional ``uri`` is the 8th
        key = _database_key(database, bool(kwargs.get("uri", False)))
        target = _databases.get(key) if key is not None else None
        if target is not None:
            kwargs["uri"] = True
            tagged = len(args) < 5 and "factory" not in kwargs
            if tagged:
                kwargs["factory"] = ResidentConnection
            connection = _sqlite_connect(target, *args, **kwargs)
            if tagged:
                connection.resident_path = str(_resolved[key])
            return connection
    return _sqlite_connect(database, *args, **kwargs)


def database_path(connection: sqlite3.Connection) -> str:
    """The file behind ``connection``'s main database, '' for a memory one."""

    path = getattr(connection, "resident_path", "")
    if path:
        return path
    for _seq, name, file_name in connection.execute("PRAGMA database_list"):
        if name == "main":
            return str(file_name or "")
    return ""


def _register(path: Path, stat_result: os.stat_result) -> list[str]:
    resolved = path.resolve()
    keys = list(dict.fromkeys((_key(path), _key(resolved))))
    for key in keys:
        _stats[key] = stat_result
        _resolved[key] = resolved
    return keys


def load_database(path: str | os.PathLike[str]) -> int:
    """Copy one SQLite database into memory; the number of bytes held."""

    path = Path(path)
    stat_result = os.stat(path)
    source = _sqlite_connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    name = f"/dso-resident-{len(_anchors)}"
    anchor = _sqlite_connect(
        f"file:{name}?vfs=memdb", uri=True, check_same_thread=False,
    )
    try:
        source.backup(anchor)
    except BaseException:
        anchor.close()
        raise
    finally:
        source.close()
    with _lock:
        _anchors.append(anchor)
        for key in _register(path, stat_result):
            _databases[key] = f"file:{name}?vfs=memdb&mode=ro"
    return int(stat_result.st_size)


def load_file(path: str | os.PathLike[str]) -> int:
    """Keep one file's bytes in memory; the number of bytes held."""

    path = Path(path)
    stat_result = os.stat(path)
    data = path.read_bytes()
    with _lock:
        for key in _register(path, stat_result):
            _files[key] = data
    return len(data)


def refresh(path: str | os.PathLike[str]) -> None:
    """Re-read a resident file after the server itself rewrote it."""

    key = _key(path)
    with _lock:
        if key not in _files:
            return
        keys = [name for name, known in _resolved.items() if known == _resolved[key]]
    try:
        stat_result = os.stat(path)
        data = Path(path).read_bytes()
    except FileNotFoundError:
        with _lock:
            for name in keys:
                _files.pop(name, None)
                _stats.pop(name, None)
                _resolved.pop(name, None)
        return
    with _lock:
        for name in keys:
            _files[name] = data
            _stats[name] = stat_result


def is_resident(path: str | os.PathLike[str]) -> bool:
    key = _key(path)
    return key in _files or key in _databases


def read_bytes(path: str | os.PathLike[str]) -> bytes:
    """A resident file's contents, or the file read from disk."""

    data = _files.get(_key(path))
    return Path(path).read_bytes() if data is None else data


def read_text(path: str | os.PathLike[str], encoding: str = "utf-8",
              errors: str = "strict") -> str:
    data = _files.get(_key(path))
    if data is None:
        return Path(path).read_text(encoding=encoding, errors=errors)
    return data.decode(encoding, errors)


def stat(path: str | os.PathLike[str]) -> os.stat_result:
    """``os.stat`` of a resident file as it was loaded, else of the file."""

    stat_result = _stats.get(_key(path))
    return os.stat(path) if stat_result is None else stat_result


def revision(path: str | os.PathLike[str]) -> tuple[int, int] | None:
    """``(st_mtime_ns, st_size)`` of a resident file, else ``None``."""

    stat_result = _stats.get(_key(path))
    if stat_result is None:
        return None
    return int(stat_result.st_mtime_ns), int(stat_result.st_size)


def is_file(path: str | os.PathLike[str]) -> bool:
    if _key(path) in _stats:
        return True
    return Path(path).is_file()


def resolved(path: str | os.PathLike[str]) -> Path:
    """``Path(path).resolve()``, without touching the disk once known."""

    known = _resolved.get(_key(path))
    return known if known is not None else _resolve(os.fspath(path))


@lru_cache(maxsize=1024)
def _resolve(path: str) -> Path:
    return Path(path).resolve()


def resident_paths(directory: str | os.PathLike[str], suffix: str) -> tuple[Path, ...]:
    """Resident files directly in ``directory`` ending in ``suffix``, sorted."""

    prefix = _key(directory) + os.sep
    suffix = os.path.normcase(suffix)
    with _lock:
        names = {
            resolved_path for key, resolved_path in _resolved.items()
            if key.startswith(prefix) and key.endswith(suffix)
            and os.sep not in key[len(prefix):]
        }
    return tuple(sorted(names, key=lambda item: item.name))


def install(static_database_path: str | os.PathLike[str],
            extra_files=()) -> None:
    """Load the static content beside ``static_database_path`` into memory.

    ``static_database_path`` is ``<data>/db/static.db4``; the maps and their
    navigation meshes live under ``<data>/maps`` and ``<data>/navigation``.
    """

    global _installed
    if os.getenv("DSO_RESIDENT_CONTENT", "1").strip().lower() in (
        "0", "false", "no", "off",
    ):
        print("[Resident] static content is read from disk (DSO_RESIDENT_CONTENT=0)")
        return
    if _installed:
        return
    started = time.perf_counter()
    static_database_path = Path(static_database_path)
    data_root = static_database_path.parent.parent
    database_bytes = file_bytes = 0
    databases = files = 0
    failures = []

    def attempt(load, path) -> int:
        try:
            return load(path)
        except (OSError, sqlite3.Error) as exc:
            failures.append(f"{path}: {type(exc).__name__}: {exc}")
            return -1

    if static_database_path.is_file():
        loaded = attempt(load_database, static_database_path)
        if loaded >= 0:
            database_bytes += loaded
            databases += 1
    maps = data_root / "maps"
    navigation = data_root / "navigation"
    candidates = []
    if maps.is_dir():
        candidates += [(load_database, path) for path in sorted(maps.glob("*.db4"))]
        candidates += [
            (load_file, path) for path in sorted(maps.iterdir())
            if path.suffix.casefold() in MAP_FILE_SUFFIXES
        ]
    if navigation.is_dir():
        candidates += [(load_file, path) for path in sorted(navigation.glob("*/*.tok"))]
        candidates += [(load_file, path) for path in sorted(navigation.glob("*.bin"))]
    candidates += [(load_file, Path(path)) for path in extra_files]
    for load, path in candidates:
        if not path.is_file():
            continue
        loaded = attempt(load, path)
        if loaded < 0:
            continue
        if load is load_database:
            database_bytes += loaded
            databases += 1
        else:
            file_bytes += loaded
            files += 1
    if databases:
        sqlite3.connect = connect
    _installed = True
    for failure in failures:
        print(f"[Resident] WARNING: read from disk instead: {failure}")
    print(
        f"[Resident] {databases} database(s) ({database_bytes / 2**20:.0f} MB) "
        f"and {files} content file(s) ({file_bytes / 2**20:.0f} MB) held in "
        f"memory, loaded in {time.perf_counter() - started:.1f}s"
    )


def uninstall() -> None:
    """Forget every resident copy (tests)."""

    global _installed
    with _lock:
        sqlite3.connect = _sqlite_connect
        _databases.clear()
        _files.clear()
        _stats.clear()
        _resolved.clear()
        anchors = list(_anchors)
        _anchors.clear()
        _installed = False
    _resolve.cache_clear()
    for anchor in anchors:
        anchor.close()
