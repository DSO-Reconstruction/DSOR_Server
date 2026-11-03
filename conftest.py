"""Allow tests to run directly from the standalone source directory."""

import os
from pathlib import Path
import runpy
import sqlite3
import threading
from urllib.parse import unquote

# The suite's captured packets come from dro_client64-patch5. Pin its command
# ids and skill/Hit layout before any protocol module resolves them at import;
# the server itself defaults to the release261_203 client.
os.environ.setdefault("DSO_COMMAND_IDS", "builtin")
os.environ.setdefault("DSO_SKILL_HIT_MARKER_BITS", "0")

# Gameplay code opens a fresh read-only connection per static lookup, and
# SQLite parses the whole schema (400-760 tables) on each one's first query:
# 3-4 ms, ~53,000 times per suite run, about a third of its time. In shared-
# cache mode connections to one file reuse a parsed schema while any of them
# stays open, so each live data/*.db4 keeps one anchor connection. Only
# read-only URIs to files the tests never rewrite are shared. Scratch test
# trees link data/db and data/maps to the live data, and resolved paths
# name the link targets.
_DATA = Path(os.path.abspath(__file__)).with_name("data")
_LIVE_DATA = tuple({
    os.path.normcase(str(path)) + os.sep
    for path in (_DATA, (_DATA / "db").resolve(), (_DATA / "maps").resolve())
})
_sqlite_connect = sqlite3.connect
_schema_anchors = {}
_schema_anchor_lock = threading.Lock()


def _live_read_only_database(database):
    if not isinstance(database, str) or not database.startswith("file:"):
        return None
    location, _separator, query = database[5:].partition("?")
    params = dict(part.partition("=")[::2] for part in query.split("&") if part)
    if params.get("mode") != "ro" or "cache" in params or "vfs" in params:
        return None
    location = unquote(location)
    if location.startswith("///"):
        location = location[3:]
    elif location[:1] == "/" and location[2:3] == ":":
        location = location[1:]
    path = os.path.normcase(os.path.abspath(location))
    return path if path.startswith(_LIVE_DATA) and path.endswith(".db4") else None


def _connect_sharing_schema(database, *args, **kwargs):
    path = None
    if kwargs.get("uri") and len(args) < 7:  # a positional ``uri`` is the 8th
        path = _live_read_only_database(database)
    if path is None:
        return _sqlite_connect(database, *args, **kwargs)
    database += "&cache=shared"
    with _schema_anchor_lock:
        if path not in _schema_anchors:
            _schema_anchors[path] = _sqlite_connect(
                database, uri=True, check_same_thread=False,
            )
    return _sqlite_connect(database, *args, **kwargs)


sqlite3.connect = _connect_sharing_schema

runpy.run_path(str(Path(__file__).with_name("run.py")))["load_package"]()
