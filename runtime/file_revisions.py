"""Last observed revisions of content files, kept off the gameplay hot path.

Content loaders cache their parse by ``(path, st_mtime_ns, st_size)`` so an
edited file is picked up without a restart. Calling ``stat()`` for that key
on every lookup made one map entry stat quests.xml, events.json and
static.db4 about 2,000 times while holding the session's mutation gate. On a
volume spanning two hard disks that the client loads the same map from, a
single ``stat()`` has taken three seconds (2026-10-24, see
``gameplay/player_combat_stats.prime_player_combat_database``).

The first observation of a path stats it on the caller's thread. Later calls
return the last observed revision at once, and at most once per
``RECHECK_SECONDS`` a background thread re-checks the file, so an edited file
is still picked up within about a second. A writer that needs its change
visible to the very next lookup calls ``invalidate(path)``.
"""

from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import threading
import time

RECHECK_SECONDS = 1.0

# ``None`` records a missing file, so lookups do not keep probing for it.
_revisions: dict[str, tuple[int, int] | None] = {}
_checked_at: dict[str, float] = {}
_rechecks: set[str] = set()
# Bumped by invalidate(): a re-check that started before a write must not
# store the revision it read before that write.
_generations: dict[str, int] = {}
_lock = threading.Lock()


@lru_cache(maxsize=256)
def resolved_path(path: str) -> Path:
    """Resolve a content path once; resolve() also touches the filesystem."""

    return Path(path).resolve()


def _stat_revision(key: str) -> tuple[int, int] | None:
    try:
        stat = os.stat(key)
    except FileNotFoundError:
        return None
    return int(stat.st_mtime_ns), int(stat.st_size)


def observed_revision(path: str | Path) -> tuple[int, int] | None:
    """``(st_mtime_ns, st_size)`` of ``path``, or ``None`` if it is missing."""

    key = str(path)
    now = time.perf_counter()
    with _lock:
        known = key in _revisions
        revision = _revisions.get(key)
        recheck = (
            known
            and key not in _rechecks
            and now - _checked_at.get(key, now) >= RECHECK_SECONDS
        )
        if recheck:
            _rechecks.add(key)
        generation = _generations.get(key, 0)
    if not known:
        revision = _stat_revision(key)
        with _lock:
            _revisions[key] = revision
            _checked_at[key] = time.perf_counter()
    elif recheck:
        threading.Thread(
            target=_recheck, args=(key, generation),
            name="content-revision-recheck", daemon=True,
        ).start()
    return revision


def _recheck(key: str, generation: int) -> None:
    try:
        revision = _stat_revision(key)
    except OSError as exc:
        # Keep serving the revision already observed; the next check retries.
        print(f"[ContentRevision] could not re-check {key}: {type(exc).__name__}: {exc}")
        revision = generation = None
    with _lock:
        if generation is not None and generation == _generations.get(key, 0):
            _revisions[key] = revision
        _checked_at[key] = time.perf_counter()
        _rechecks.discard(key)


def invalidate(path: str | Path | None = None) -> None:
    """Make the next lookup of ``path`` (or of every path) stat again."""

    named = () if path is None else (str(path), str(resolved_path(str(path))))
    with _lock:
        for key in named or tuple(_revisions):
            _revisions.pop(key, None)
            _checked_at.pop(key, None)
            _generations[key] = _generations.get(key, 0) + 1
