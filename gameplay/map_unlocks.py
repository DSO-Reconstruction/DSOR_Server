"""Character map discovery and authored quest requirements for map access."""

from contextlib import closing
from functools import lru_cache
import sqlite3

from dso_server.gameplay.map_event_state import map_key
from dso_server.gameplay.quest_content import QUEST_STATE_CLOSED


@lru_cache(maxsize=4)
def map_unlock_catalog(path):
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        travel = tuple(str(row[0]) for row in db.execute(
            'SELECT Id FROM _Template_TravelMap WHERE IsLive<>0 AND HasTravelPoint<>0 '
            'ORDER BY MapLookupIndex, rowid'))
        accesses = tuple((str(a), str(g), str(q or "")) for a, g, q in db.execute(
            'SELECT Id, AccessGroup, UnlockingQuest FROM _Template_MapAccessGroup'))
    return travel, accesses


def restored_map_unlocks(saved, current_maps=()):
    # Legacy saves establish only their last location, never the old global
    # testing travel list. Keep real discoveries separate from admin access.
    return {map_key(value) for value in (
        *getattr(saved, "unlocked_maps", ()),
        getattr(saved, "map_id", ""), getattr(saved, "map_access_id", ""),
        *current_maps,
    ) if value}


def quest_map_unlocks(path, closed_quests):
    closed = {str(q).casefold() for q in closed_quests}
    return {map_key(value) for access, group, quest in map_unlock_catalog(str(path))[1]
            if quest and quest.casefold() in closed for value in (access, group) if value}


def saved_quest_map_unlocks(path, saved):
    return quest_map_unlocks(path, (
        quest for quest, progress in getattr(saved, "quest_log", {}).items()
        if int(progress.get("state", -1)) == QUEST_STATE_CLOSED
    ))


def unlocked_travel_map_ids(path, unlocked_maps, *, admin_all_maps=False):
    travel, accesses = map_unlock_catalog(str(path))
    if admin_all_maps:
        return travel
    known = {map_key(value) for value in unlocked_maps}
    known.update(map_key(group) for access, group, _ in accesses if map_key(access) in known)
    return tuple(map_id for map_id in travel if map_key(map_id) in known)


def map_unlocking_quests(path, map_id):
    key = map_key(map_id)
    rows = map_unlock_catalog(str(path))[1]
    # Prefer the exact access configuration; a group can contain several tiers.
    exact = tuple(quest for access, _, quest in rows if map_key(access) == key)
    return tuple(q for q in (exact or tuple(
        quest for _, group, quest in rows if map_key(group) == key
    )) if q)
