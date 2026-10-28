"""Authoritative skill unlock snapshots and one-time level-up notifications."""
from functools import lru_cache
from contextlib import closing
from pathlib import Path
import sqlite3

from dso_server.runtime import resident_content
from dso_server.protocol.character_selection import CLASS_NAMES, CLIENT_SKILL_MANAGER_ROW_OFFSET
from dso_server.protocol.player_init_protocol import (
    SKILL_BOOK_INFO_COMMAND, XP_CHANGED_COMMAND, ServerCommand, SkillBookEntry,
    SkillBookInfo, XPChanged,
)


@lru_cache(maxsize=16)
def _skills(path: str, mtime: int, class_id: int):
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as db:
        return tuple(db.execute(
            'SELECT rowid, Id, UnlockLevel, Hidden, IsLive FROM _Template_Skill '
            'WHERE CharClass=? ORDER BY rowid', (CLASS_NAMES[int(class_id)],)
        ))


def skill_book_snapshot(database_path, class_id, level, *, new_level=None, quick_slots=None):
    path = resident_content.resolved(database_path)
    assigned = {slot.resource_id for bar in getattr(quick_slots, 'quickbars', ())
                for slot in bar if slot.kind == 0}
    seen = set()
    entries = []
    for rowid, resource, unlock, hidden, live in _skills(str(path), resident_content.stat(path).st_mtime_ns, class_id):
        unlock = max(1, int(unlock or 1))
        unlocked = bool(live and unlock <= level)
        fresh = bool(unlocked and not hidden and unlock == new_level
                     and resource not in assigned and resource not in seen)
        entries.append(SkillBookEntry(rowid + CLIENT_SKILL_MANAGER_ROW_OFFSET,
                                      (unlocked, bool(hidden or not live), fresh)))
        seen.add(resource)
    return SkillBookInfo(tuple(entries))


def experience_commands(database_path, class_id, transition, *, actor_id, quick_slots=None):
    xp = ServerCommand(XP_CHANGED_COMMAND, XPChanged(
        transition.xp, transition.level, transition.level_base_xp,
        transition.next_level_base_xp, transition.level_up,
    ), actor_id)
    if not transition.level_up:
        return (xp,)
    # XPChanged makes the client process SkillBook's `new` bits. Set them for
    # only this level, then clear them after the event so later refreshes cannot
    # re-add an earlier skill (even if the player removed it from the bar).
    fresh = skill_book_snapshot(database_path, class_id, transition.level,
                               new_level=transition.level, quick_slots=quick_slots)
    read = SkillBookInfo(tuple(SkillBookEntry(e.skill_id, (*e.flags[:2], False))
                              for e in fresh.skills))
    return (ServerCommand(SKILL_BOOK_INFO_COMMAND, fresh, actor_id), xp,
            ServerCommand(SKILL_BOOK_INFO_COMMAND, read, actor_id))
