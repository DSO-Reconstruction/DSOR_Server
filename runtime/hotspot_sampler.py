"""Short, triggered sampling of where every thread is running.

A stalled input queue only shows its own stack, but under CPython one
CPU-bound thread anywhere slows every other thread: the 2026-10-29 dungeon
stalls looked like slow casts and late monster ticks while the cause was a
quest scan on another call path. When a stall is noticed, ``trigger()``
samples every thread's stack every ``DSO_HOTSPOT_SAMPLE_MS`` (5) for
``DSO_HOTSPOT_SECONDS`` (2) and prints the server functions that were
running, per thread. It costs nothing until triggered and runs at most once
per ``DSO_HOTSPOT_COOLDOWN_SECONDS`` (10). ``DSO_HOTSPOT_SECONDS=0`` disables it.
"""

from __future__ import annotations

from collections import Counter
import linecache
import os
import sys
import threading
import time


_SECONDS = max(0.0, float(os.getenv("DSO_HOTSPOT_SECONDS", "2")))
_INTERVAL = max(0.001, float(os.getenv("DSO_HOTSPOT_SAMPLE_MS", "5")) / 1000.0)
_COOLDOWN = max(_SECONDS, float(os.getenv("DSO_HOTSPOT_COOLDOWN_SECONDS", "10")))
_TOP = 12
_PACKAGE = os.sep + "dso_server" + os.sep
# A thread parked in one of these calls is waiting, not working.
_WAITING = ("sleep(", ".wait(", "recvfrom(", ".acquire(", ".get(", "select(")

_lock = threading.Lock()
_running = False
_last_started = -_COOLDOWN


def _working_frames(frame):
    """Return (innermost, caller) server frames, or None for a waiting thread."""

    own = []
    while frame is not None and len(own) < 2:
        filename = frame.f_code.co_filename
        if _PACKAGE in filename and not filename.endswith("hotspot_sampler.py"):
            own.append(frame)
        elif not own and filename.endswith(("threading.py", "queue.py", "selectors.py", "socket.py")):
            return None
        frame = frame.f_back
    if not own:
        return None
    line = linecache.getline(own[0].f_code.co_filename, own[0].f_lineno)
    if any(marker in line for marker in _WAITING):
        return None
    return own


def _describe(frame, *, line: bool = True) -> str:
    filename = frame.f_code.co_filename
    short = filename.split(_PACKAGE, 1)[-1].replace(os.sep, "/")
    where = f"{short}:{frame.f_lineno}" if line else short
    return f"{where} {frame.f_code.co_name}"


def _sample(reason: str) -> None:
    global _running
    try:
        names = {thread.ident: thread.name for thread in threading.enumerate()}
        own_ident = threading.get_ident()
        counts = Counter()
        per_thread = Counter()
        samples = 0
        deadline = time.perf_counter() + _SECONDS
        while time.perf_counter() < deadline:
            samples += 1
            for ident, frame in sys._current_frames().items():
                if ident == own_ident:
                    continue
                working = _working_frames(frame)
                if working is None:
                    continue
                name = names.get(ident) or str(ident)
                # Pool workers differ only by their trailing number.
                name = name.rstrip("0123456789").rstrip("-_") or name
                # Group a hot function's lines together; keep the call site.
                site = _describe(working[0], line=False)
                if len(working) > 1:
                    site += " <- " + _describe(working[1])
                counts[(name, site)] += 1
                per_thread[name] += 1
            time.sleep(_INTERVAL)
        if not samples:
            return
        lines = [
            f"[Hotspots] {samples} samples over {_SECONDS:.1f}s after {reason}; "
            "share of samples each thread spent working:"
        ]
        lines.extend(
            f"  {count / samples:6.1%}  {name}" for name, count in per_thread.most_common(6)
        )
        lines.append("  hottest sites:")
        lines.extend(
            f"  {count / samples:6.1%}  {name}  {site}"
            for (name, site), count in counts.most_common(_TOP)
        )
        print("\n".join(lines))
    except Exception as exc:  # diagnostics must never take the server down
        print(f"[Hotspots] sampling failed: {type(exc).__name__}: {exc}")
    finally:
        with _lock:
            _running = False


def trigger(reason: str) -> bool:
    """Start one sampling window unless one ran recently; never blocks."""

    global _running, _last_started
    if _SECONDS <= 0:
        return False
    now = time.perf_counter()
    with _lock:
        if _running or now - _last_started < _COOLDOWN:
            return False
        _running = True
        _last_started = now
    try:
        threading.Thread(
            target=_sample, args=(reason,), name="hotspot-sampler", daemon=True,
        ).start()
    except Exception:
        with _lock:
            _running = False
        return False
    return True
