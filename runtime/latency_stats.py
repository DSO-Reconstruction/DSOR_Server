"""Rolling latency budgets for the real-time loop, printed once a minute.

What a player does -- a move, a cast, a pickup -- must be handled within
milliseconds. The slow-command warnings only show outliers above 50 ms; this
keeps the distribution. ``record(kind, seconds)`` is cheap (one lock, a
bounded sample ring per kind) and every ``DSO_LATENCY_REPORT_SECONDS`` (30)
of activity one line per kind reports count, p50, p95, p99 and max.
``DSO_LATENCY_REPORT_SECONDS=0`` disables reporting.
"""

from __future__ import annotations

from collections import deque
import os
import threading
import time


# A dungeon fight is often over within a minute; a shorter window keeps it
# visible on its own instead of averaged into map entry.
REPORT_SECONDS = max(0.0, float(os.getenv("DSO_LATENCY_REPORT_SECONDS", "30")))
_SAMPLES = 2048

# Short names for the gameplay commands that dominate the input queue.
# (rel261 ids, docs/command_ids_rel261_1024.json)
COMMAND_NAMES = {
    0x001C: "actor-request",
    0x0029: "npc-request",
    0x0046: "skill",
    0x0047: "target-skill",
    0x0048: "bullet-skill",
    0x0049: "target-point-bullet",
    0x004A: "shifted-skill",
    0x0055: "inventory",
    0x005F: "move",
    0x0061: "travel",
    0x0062: "respawn",
    0x0064: "pickup-item",
}

_lock = threading.Lock()
_samples: dict[str, deque] = {}
_counts: dict[str, int] = {}
_maxima: dict[str, float] = {}
_window_started = time.perf_counter()


class PhaseTimer:
    """Record successive stages of one handler as ``"<name> > <stage>"``.

    Each ``mark`` records the time since the previous mark (or creation), so
    the ``[Latency]`` table splits a slow command into its stages.
    """

    __slots__ = ("name", "last")

    def __init__(self, name: str) -> None:
        self.name = name
        self.last = time.perf_counter()

    def mark(self, stage: str) -> None:
        now = time.perf_counter()
        record(f"{self.name} > {stage}", now - self.last)
        self.last = now


def command_kind(command_id: int) -> str:
    name = COMMAND_NAMES.get(int(command_id))
    return f"input {name}" if name else f"input 0x{int(command_id):04x}"


def record(kind: str, seconds: float) -> None:
    if REPORT_SECONDS <= 0:
        return
    seconds = max(0.0, float(seconds))
    report = None
    now = time.perf_counter()
    global _window_started
    with _lock:
        ring = _samples.get(kind)
        if ring is None:
            ring = _samples[kind] = deque(maxlen=_SAMPLES)
        ring.append(seconds)
        _counts[kind] = _counts.get(kind, 0) + 1
        if seconds > _maxima.get(kind, 0.0):
            _maxima[kind] = seconds
        if now - _window_started >= REPORT_SECONDS:
            report = _snapshot_locked(now - _window_started)
            _samples.clear()
            _counts.clear()
            _maxima.clear()
            _window_started = now
    if report:
        print(report)


def _percentile(ordered, fraction):
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _snapshot_locked(elapsed: float) -> str:
    lines = [f"[Latency] last {elapsed:.0f}s (ms: p50 / p95 / p99 / max, count):"]
    for kind in sorted(_samples, key=lambda key: -_counts.get(key, 0)):
        ordered = sorted(_samples[kind])
        lines.append(
            f"  {kind:<28} {_percentile(ordered, 0.5) * 1000:7.2f} "
            f"{_percentile(ordered, 0.95) * 1000:7.2f} "
            f"{_percentile(ordered, 0.99) * 1000:7.2f} "
            f"{_maxima.get(kind, 0.0) * 1000:8.2f}  n={_counts.get(kind, 0)}"
        )
    return "\n".join(lines)
