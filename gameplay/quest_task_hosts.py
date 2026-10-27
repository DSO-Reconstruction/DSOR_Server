"""Map evidence for ClearRegion tasks whose declared map lacks the region.

A task's ``map`` attribute is normally where its objective lives, and the
quest filters keep using it. pw208-hide-and-seek-03-n declares
pw208_rangervillage_dun for every task, but the spawn area of its
ClearRegion only exists in pw208_lost_woods_dun_04: it is gated by event set
4088 (``pw208_hide_and_seek_03_spawn_enemies=True``), and that event is
started by the quest's own server signal while
``pw208-hide-and-seek-03-n:pw208-hide-and-seek-03-clear-01`` is not done.
The ranger village exports the same EventSets rows but no spawn source gated
by them.

A ClearRegion "lives" on a map that places one of its monster templates
(``_Instance_SpawnPoint.NpcId``/``_Instance_Monster.Id``) or whose spawn
source is gated by an event that a signal conditioned on this exact task
starts (``_Template_Signal.QuestTaskDone/QuestTaskNotDone`` ->
``_Template_Events.StartSignalId``). A task is relocated to the current map
only when every declared map is exported, none of them contains any of the
task's regions, and the current map contains all of them.

Explore triggers need no relocation: PlayerHitTrigger feeds the quest event
by trigger id on any map.
"""

from contextlib import closing
from functools import lru_cache
from pathlib import Path
import sqlite3

from dso_server.gameplay.local_map_state import map_database_path
from dso_server.gameplay.map_event_state import map_key


def _signature(path):
    path = Path(path).resolve()
    try:
        stat = path.stat()
    except OSError:
        return None
    return str(path), stat.st_mtime_ns, stat.st_size


def _connect(signature):
    return closing(sqlite3.connect(f"file:{Path(signature[0]).as_posix()}?mode=ro", uri=True))


def _tokens(value):
    return tuple(part.strip() for part in str(value or "").split(";") if part.strip())


def _rows(database, sql):
    try:
        return database.execute(sql).fetchall()
    except sqlite3.Error:  # a table/column this export does not have
        return []


@lru_cache(maxsize=512)
def _map_spawn_content(map_signature):
    """(placed monster templates, events whose EventSet gates a spawn row)."""
    with _connect(map_signature) as database:
        placed = frozenset(str(row[0]).casefold() for row in (
            _rows(database, "SELECT NpcId FROM _Instance_SpawnPoint")
            + _rows(database, "SELECT Id FROM _Instance_Monster")) if row[0])
        gating = {int(row[0]) for table in ("_Instance_SpawnArea", "_Instance_SpawnPoint",
                                            "_Instance_Monster")
                  for row in _rows(database, f"SELECT EventSetRow FROM {table}")
                  if row[0] is not None and int(row[0]) >= 0}
        events = set()
        for row_id, flags in _rows(database, "SELECT Id, EventFlags FROM _Instance_EventSets"):
            try:
                if int(row_id) not in gating:
                    continue
            except (TypeError, ValueError):
                continue
            for flag in _tokens(flags):
                name, _, value = flag.partition("=")
                if value.strip().casefold() == "true":
                    events.add(name.strip().casefold())
    return placed, frozenset(events)


@lru_cache(maxsize=4)
def _task_started_events(static_signature):
    """``quest:task`` -> events started by signals conditioned on that task."""
    with _connect(static_signature) as database:
        signals = {}
        for signal_id, done, not_done in _rows(database, (
                "SELECT SignalId, QuestTaskDone, QuestTaskNotDone FROM _Template_Signal "
                "WHERE COALESCE(Enabled, 1) <> 0")):
            for task in _tokens(done) + _tokens(not_done):
                signals.setdefault(str(signal_id).casefold(), set()).add(task.casefold())
        result = {}
        for event_id, starts in _rows(database, "SELECT Id, StartSignalId FROM _Template_Events"):
            for signal in _tokens(starts):
                for task in signals.get(signal.casefold(), ()):
                    result.setdefault(task, set()).add(str(event_id).casefold())
    return {task: frozenset(events) for task, events in result.items()}


def _regions_on_map(static_database_path, map_id, quest, task):
    """Per-region containment on ``map_id``; None when it is not exported."""
    signature = _signature(map_database_path(static_database_path, map_id))
    static = _signature(static_database_path)
    if signature is None or static is None:
        return None
    placed, gated_events = _map_spawn_content(signature)
    own = _task_started_events(static).get(f"{quest.quest_id}:{task.title}".casefold(), frozenset())
    gated_here = bool(own & gated_events)
    return tuple(
        gated_here or bool({value.strip().casefold() for value in monsters.split(";")
                            if value.strip()} & placed)
        for monsters in task.clear_regions.values()
    )


def clear_region_relocated_to(static_database_path, quest, task, map_id):
    """Whether ``map_id`` hosts ClearRegions their declared map cannot contain."""
    if not task.clear_regions or not task.maps or not map_id:
        return False
    if map_key(map_id) in {map_key(value) for value in task.maps}:
        return False
    for declared in task.maps:
        found = _regions_on_map(static_database_path, declared, quest, task)
        if found is None or any(found):  # absent map = missing content
            return False
    found = _regions_on_map(static_database_path, map_id, quest, task)
    return bool(found) and all(found)


__all__ = ["clear_region_relocated_to"]
