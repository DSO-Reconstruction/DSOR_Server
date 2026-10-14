"""Observe map workers without acquiring session, combat, or outbound locks."""
from __future__ import annotations

from collections import deque
import json
import threading
import time
import weakref

_workers = weakref.WeakSet()
_worker_exits = deque(maxlen=128)
_registry_lock = threading.Lock()


def register_worker(worker):
    with _registry_lock:
        _workers.add(worker)


def worker_snapshot():
    with _registry_lock:
        return tuple(_workers)


def record_worker_exit(worker):
    # Keep only a bounded value snapshot. The live Session/Thread must remain
    # collectable even when monitoring is disabled or the app is stopping.
    session = worker.session
    event = {
        "event": "worker-exited",
        "map": getattr(session, "gameplay_map", None),
        "endpoint": session.addr,
        "generation": worker.generation,
        "session_generation": getattr(session, "gameplay_chase_generation", None),
        "ticks_completed": worker.diagnostic_ticks_completed,
        "last_result": worker.diagnostic_last_result,
        "cancelled": worker._stopped.is_set(),
        "still_installed": getattr(session, "gameplay_chase_timer", None) is worker,
    }
    with _registry_lock:
        _worker_exits.append(event)


def drain_worker_exits():
    with _registry_lock:
        result = tuple(_worker_exits)
        _worker_exits.clear()
    return result


def mark_phase(session, phase):
    worker = getattr(session, "gameplay_chase_timer", None)
    if (
        worker is not None
        and hasattr(worker, "diagnostic_phase")
        and worker._thread.ident == threading.get_ident()
    ):
        # A retiring thread must not overwrite its replacement's progress.
        worker._set_phase(str(phase))


def describe_worker(worker, now):
    """Best-effort diagnostic snapshot; never authoritative gameplay state."""
    session = worker.session
    combat = getattr(session, "gameplay_combat_state", None)
    pending = dict(getattr(session, "pending_reliable", {}))
    oldest = min(
        pending.values(),
        key=lambda entry: entry.get("queued_at", now),
        default={},
    )
    move = getattr(session, "gameplay_last_move", None)
    clock_tick = int(getattr(session, "gameplay_chase_server_tick", 0))
    client_tick = int(move.start_tick) if move is not None else clock_tick
    lead = ((clock_tick - client_tick + 0x80000000) & 0xFFFFFFFF) - 0x80000000
    outbound = getattr(session, "outbound_lock", None)
    return json.dumps({
        "map": getattr(session, "gameplay_map", None),
        "endpoint": session.addr,
        "worker": worker._thread.name,
        "alive": worker._thread.is_alive(),
        "generation": worker.generation,
        "session_generation": getattr(session, "gameplay_chase_generation", None),
        "phase": worker.diagnostic_phase,
        "phase_seconds": round(max(0, now - worker.diagnostic_phase_at), 3),
        "progress_age_seconds": round(max(0, now - worker.diagnostic_progress_at), 3),
        "ticks_completed": worker.diagnostic_ticks_completed,
        "last_result": worker.diagnostic_last_result,
        "visible": len(getattr(session, "gameplay_spawned_monster_actor_ids", ())),
        "active": len(getattr(combat, "active_monster_actor_ids", ())),
        "returning": len(getattr(combat, "returning_home_actor_ids", ())),
        "targets": getattr(combat, "_last_tick_inputs", {}),
        "reliable_pending": len(pending),
        "oldest_reliable_label": oldest.get("label"),
        "oldest_reliable_seconds": round(max(0, now - oldest.get("queued_at", now)), 3),
        "seconds_since_receive": round(max(0, now - getattr(session, "last_received_at", now)), 3),
        "outbound_holder": getattr(outbound, "_holder", None),
        "ingress_depth": len(getattr(session, "gameplay_skill_dispatch_queue", ())),
        "ingress_command": getattr(session, "gameplay_skill_dispatch_current_command", None),
        "server_minus_last_client_tick": lead,
    }, sort_keys=True)
