"""Reconcile occupied map instances after global event snapshots are delivered."""

from contextlib import contextmanager
import time

from dso_server.services.admin_live import ready
from dso_server.services.base import CharacterService


@contextmanager
def _reserve_viewers(viewers):
    """Reserve map membership without waiting while owning another gate.

    Gameplay and retirement use these transaction gates. A trade can own two,
    so acquire all nonblocking or release all before retrying. Never block on
    a second player's gate while retaining the first player's gate.
    """

    gates = tuple({id(s): s for s, _cid, _context in viewers}.values())
    deadline = time.monotonic() + 2.0
    acquired = []
    while True:
        for session in gates:
            gate = session.gameplay_mutation_lock
            if not gate.acquire(blocking=False):
                break
            acquired.append(gate)
        else:
            break
        for gate in reversed(acquired):
            gate.release()
        acquired.clear()
        if time.monotonic() >= deadline:
            raise ValueError("map gameplay is busy; event world refresh deferred")
        time.sleep(0.01)
    try:
        yield
    finally:
        for gate in reversed(acquired):
            gate.release()


def refresh_map_events(server, runtime, viewers, previous, catalog):
    """Update shared world state once, after every viewer's EventUpdate.

    ``viewers`` contains (session, character id, map/generation) snapshots from
    the first publication phase. Reserve their mutation gates so a map transfer
    cannot replace combat state midway through reconciliation. State locks stay
    short-lived; no flow/combat state lock is held while sending packets.
    """

    with _reserve_viewers(viewers), runtime.transition_lock:
        return _refresh_map_events(server, runtime, viewers, previous, catalog)


def _refresh_map_events(server, runtime, viewers, previous, catalog):
    if not CharacterService._map_event_runtime_is_live(server, runtime):
        return (), ()
    current = []
    for session, cid, context in viewers:
        if not server.is_session_current(session):
            continue
        with session.flow_lock:
            if (not ready(session, cid)
                    or context != (session.gameplay_map, session.gameplay_chase_generation)
                    or session.gameplay_map_event_runtime is not runtime):
                continue
        current.append((session, cid))
    if not current:
        return (), ()

    # Include both catalogs' inactive entries and aliases. Removing a schedule
    # entry must also remove the flag that an existing runtime captured at join.
    owned_flags = {
        value.casefold()
        for source in (previous, catalog)
        for event in source.events
        for value in (event.template.resource_id, *event.tags)
    }
    with runtime.lock:
        enabled, disabled = runtime.replace_global_flags(
            owned_flags, catalog.active_event_flags(),
        )
        # State packets are replayable independently of the shared transition.
        # A failed send must not turn a retry of the same catalog into a no-op.
        affected = {
            event_set.row for flag in owned_flags
            for event_set in runtime.content.event_sets_for(flag)
        }
        visible = tuple(sorted(affected & runtime.enabled_event_sets))
        hidden = tuple(sorted(affected - runtime.enabled_event_sets))
    sessions = tuple(session for session, _cid in current)
    if disabled:
        CharacterService._retire_event_monsters(server, runtime, sessions, disabled)
        CharacterService._unpublish_event_props(server, runtime, sessions, disabled)
    if enabled:
        CharacterService._spawn_event_monsters(server, runtime, sessions, enabled)
    completed, errors = [], []
    for session, cid in current:
        try:
            if hidden:
                CharacterService._hide_event_npcs(server, runtime, (session,), hidden)
            if visible:
                CharacterService._publish_event_props(
                    server, runtime, (session,), visible, refresh_existing=True,
                )
                CharacterService._spawn_event_npcs(server, runtime, (session,), visible)
            if affected:
                CharacterService._publish_map_exits(
                    server, runtime, (session,), disabled_event_sets=hidden,
                )
            completed.append(cid)
        except Exception as exc:
            errors.append({"character_id": cid, "error": str(exc)[:200]})
    return tuple(completed), tuple(errors)
