"""Achievement definitions (``_Template_Achievements``) and their progress.

Retail reports achievements with ``Achievements::AchievementInfoCommand``:
one record per command, ``(resource id, progress, completion timestamp)``,
where the timestamp is eight ``u32`` fields (year, day, hour, minute,
second, 0, ISO weekday, month) that stay zero until the achievement
completes.  The tutorial capture of 2026-09-09 shows the server sending a
record whenever progress changes (``skin_unlock_amount_*`` counting up)
and a completed record with the timestamp when ``lore_enter_grimford_01``
unlocked on entering Grimford.  The client raises its achievement popup
from those records, so a completed achievement must be sent with the
**same** timestamp on every later login: re-stamping it looks like a new
unlock.

The rule types the server feeds are listed in :data:`SUPPORTED_TYPES`.
Kill rows that retail retired with the ``nomob`` placeholder (Category
``legacy``) stay loaded for persistence but have no evaluator: they never
progress or complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Iterable, Mapping


TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# template Type -> the event kind the service feeds (casefolded)
SUPPORTED_TYPES = MappingProxyType({
    "kill": "kill",
    "enter_map": "enter_map",
    "quest": "quest",
    "interact": "interact",
    "pick_up": "pick_up",
    "class_level": "class_level",
    "cleardungeon": "cleardungeon",
    "points": "points",
    "unlockachievements": "unlockachievements",
    "crafting": "crafting",
    "itemkill": "itemkill",
    "equipitem": "equipitem",
    "equiprarity": "equiprarity",
    "eventprogresscollected": "eventprogresscollected",
    "use": "use",
    "unlockskin": "unlockskin",
    "stickerbook": "stickerbook",
    "pvp_kill": "pvp_kill",
    "pvp_match": "pvp_match",
    "guildmembers": "guildmembers",
    "veteranlevelunlock": "veteranlevelunlock",
    "collectedgamecurrency": "collectedgamecurrency",
    "highscoreranking": "highscoreranking",
})

# Kill rules whose parameter is not a monster template: retail retired
# these rows (Category "legacy"); the local server keeps them unsupported.
_UNSUPPORTED_KILL_PARAMS = frozenset({"nomob"})


def battleground_mode(value: str) -> str:
    """The match mode a ``_Template_BattlegroundSettings`` id belongs to.

    ``Pvp_Match`` rows and ``_Template_MatchAchievements.BattlegroundId``
    name modes (``CaptureTheFlag_5v5``); the settings rows that start a match
    add a running number and ``_default``/``_Predefined`` variants
    (``CaptureTheFlag_5v5_01``, ``BestOf5_5v5_01_default``). A 5v5 settings
    id therefore never names the 3v3 mode.
    """

    mode = str(value or "").strip().casefold()
    while True:
        head, _, tail = mode.rpartition("_")
        if not head or not (tail.isdigit() or tail in {"default", "predefined"}):
            return mode
        mode = head


# Kinds where one event advances a rule by one at most, however many of the
# event's values name it (a map is its id, its access group and access id;
# a kill is its template and its "<template>:<difficulty>" token).
_ONE_HIT_KINDS = frozenset({
    "enter_map", "cleardungeon", "class_level", "crafting", "itemkill",
    "use", "pvp_kill", "pvp_match", "veteranlevelunlock",
    "collectedgamecurrency", "kill", "highscoreranking",
})
_EQUIPMENT_KINDS = frozenset({"equipitem", "equiprarity"})
_ABSOLUTE_KINDS = frozenset({
    "eventprogresscollected", "unlockskin", "stickerbook", "guildmembers",
})
_UNTARGETED_KINDS = frozenset({"unlockskin", "guildmembers"})


def _tokens(value: object) -> tuple[str, ...]:
    return tuple(
        token.strip() for token in str(value or "").split(";") if token.strip()
    )


@dataclass(frozen=True)
class AchievementDefinition:
    achievement_id: str
    kind: str                       # template Type, casefolded
    params: tuple[str, ...]         # casefolded ';' tokens of Param
    amount: int
    points: int
    prerequisites: tuple[str, ...]  # casefolded achievement ids
    category: str
    hidden: bool
    account_wide: bool
    event_id: str
    unlocks_title: bool
    reward_item: str
    reward_amount: int

    @property
    def key(self) -> str:
        return self.achievement_id.casefold()

    @property
    def event_kind(self) -> str | None:
        kind = SUPPORTED_TYPES.get(self.kind)
        if kind == "kill" and (
            not self.params or set(self.params) <= _UNSUPPORTED_KILL_PARAMS
        ):
            return None
        return kind

    @property
    def target(self) -> int:
        return max(1, int(self.amount))

    def matches(self, value: str) -> bool:
        """Whether an event value names this achievement's target."""

        key = str(value or "").casefold()
        if not self.params:
            return False
        if self.kind == "highscoreranking":
            # Param is the worst qualifying place ("3;" = top three); the
            # event value is the final place of one high-score settlement.
            if not key.isdigit() or int(key) < 1:
                return False
            return any(
                param.isdigit() and int(key) <= int(param) for param in self.params
            )
        for param in self.params:
            # ClearDungeon rows carry "<map>:<seconds>"; only the map counts.
            if param == key or (
                self.kind == "cleardungeon" and param.split(":", 1)[0] == key
            ):
                return True
        return False


@dataclass
class AchievementProgress:
    progress: int = 0
    completed_at: str = ""          # TIMESTAMP_FORMAT, "" while open
    reward_status: str = ""

    @property
    def completed(self) -> bool:
        return bool(self.completed_at)

    def to_document(self) -> dict:
        return {"progress": int(self.progress), "completed_at": str(self.completed_at),
                "reward_status": str(self.reward_status)}

    @classmethod
    def from_document(cls, document: Mapping) -> "AchievementProgress":
        completed_at = str(document.get("completed_at", "") or "")
        status = str(document.get("reward_status", "legacy" if completed_at else ""))
        if status not in {"", "pending", "granted", "legacy"}:
            status = "legacy" if completed_at else ""
        return cls(
            progress=int(document.get("progress", 0) or 0),
            completed_at=completed_at,
            reward_status=status,
        )

    def wire_values(self) -> tuple[int, ...]:
        """The nine ``u32`` values of an AchievementInfo record."""

        if not self.completed_at:
            return (int(self.progress), 0, 0, 0, 0, 0, 0, 0, 0)
        moment = datetime.strptime(self.completed_at, TIMESTAMP_FORMAT)
        return (
            int(self.progress), moment.year, moment.day, moment.hour,
            moment.minute, moment.second, 0, moment.isoweekday(), moment.month,
        )


@dataclass(frozen=True)
class AchievementChange:
    definition: AchievementDefinition
    progress: AchievementProgress
    completed_now: bool


@dataclass(frozen=True)
class AchievementCatalog:
    achievements: Mapping[str, AchievementDefinition]      # key -> definition
    by_event_kind: Mapping[str, tuple[AchievementDefinition, ...]]

    def achievement(self, achievement_id: str) -> AchievementDefinition | None:
        return self.achievements.get(str(achievement_id).casefold())

    def for_event(self, kind: str) -> tuple[AchievementDefinition, ...]:
        return self.by_event_kind.get(str(kind).casefold(), ())


def _load_catalog(path: str, mtime_ns: int, size: int) -> AchievementCatalog:
    del mtime_ns, size
    achievements: dict[str, AchievementDefinition] = {}
    by_kind: dict[str, list] = {}
    with sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True) as database:
        database.row_factory = sqlite3.Row
        for row in database.execute(
            "SELECT * FROM _Template_Achievements WHERE COALESCE(EnabledLive, 0)=1"
        ):
            achievement_id = str(row["Id"] or "").strip()
            if not achievement_id:
                continue
            definition = AchievementDefinition(
                achievement_id=achievement_id,
                kind=str(row["Type"] or "").strip().casefold(),
                params=tuple(token.casefold() for token in _tokens(row["Param"])),
                amount=int(row["Amount"] or 1),
                points=int(row["Points"] or 0),
                prerequisites=tuple(
                    token.casefold() for token in _tokens(row["Prerequisites"])
                ),
                category=str(row["Category"] or ""),
                hidden=bool(int(row["IsHidden"] or 0)),
                account_wide=bool(int(row["IsAccountAchievement"] or 0)),
                event_id=str(row["EventId"] or ""),
                unlocks_title=bool(int(row["UnlocksTitle"] or 0)),
                reward_item=str(row["ItemTemplateId"] or ""),
                reward_amount=int(row["ItemAmount"] or 0),
            )
            achievements[definition.key] = definition
            kind = definition.event_kind
            if kind:
                by_kind.setdefault(kind, []).append(definition)
    return AchievementCatalog(
        MappingProxyType(achievements),
        MappingProxyType({k: tuple(v) for k, v in by_kind.items()}),
    )


_load_catalog_cached = lru_cache(maxsize=4)(_load_catalog)


def load_achievement_catalog(static_database_path: str | Path) -> AchievementCatalog:
    path = Path(static_database_path)
    stat = path.stat()
    return _load_catalog_cached(str(path), stat.st_mtime_ns, stat.st_size)


# ---------------------------------------------------------------------------
# Progress engine
# ---------------------------------------------------------------------------

def _now_stamp(now: datetime | None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.strftime(TIMESTAMP_FORMAT)


def _prerequisites_met(
    definition: AchievementDefinition, state: Mapping[str, AchievementProgress],
) -> bool:
    return all(
        state.get(key) is not None and state[key].completed
        for key in definition.prerequisites
    )


def _complete(
    definition: AchievementDefinition,
    progress: AchievementProgress,
    state: Mapping[str, AchievementProgress],
    now: datetime | None,
) -> bool:
    if progress.completed or progress.progress < definition.target:
        return False
    if not _prerequisites_met(definition, state):
        return False
    progress.completed_at = _now_stamp(now)
    return True


def apply_achievement_event(
    catalog: AchievementCatalog,
    state: dict[str, AchievementProgress],
    kind: str,
    values: Iterable[str],
    *,
    count: int = 1,
    progress: int | None = None,
    now: datetime | None = None,
) -> tuple[AchievementChange, ...]:
    """Advance every achievement that listens to ``kind`` for ``values``.

    Counting kinds add ``count`` per matching value. Craft/use/kill aliases
    describe one event, so matching aliases cannot multiply that count.
    Equipment events carry every currently equipped item/rarity; collection,
    guild and event-progress snapshots use the explicit absolute ``progress``.
    ``class_level`` takes the level from values. Completing an achievement re-evaluates the ``points`` and
    ``unlockachievements`` sets, so a chain of completions can come out of
    one event.  Returns the records to send, in order.
    """

    kind = str(kind).casefold()
    wanted = [str(value).casefold() for value in values if str(value)]
    if kind == "pvp_match":
        # A settings id (CaptureTheFlag_5v5_01) also names its mode.
        wanted = list(dict.fromkeys((*wanted, *map(battleground_mode, wanted))))
    absolute_progress = progress
    if kind in _ABSOLUTE_KINDS and absolute_progress is None:
        return ()
    if kind not in _ABSOLUTE_KINDS | _EQUIPMENT_KINDS and int(count) <= 0:
        return ()
    changes: list[AchievementChange] = []
    completed_any = False
    for definition in catalog.for_event(kind):
        hits = (
            1 if kind in _UNTARGETED_KINDS and not definition.params
            else sum(1 for value in wanted if definition.matches(value))
        )
        if not hits:
            continue
        if kind in _ONE_HIT_KINDS:
            hits = 1
        entry = state.setdefault(definition.key, AchievementProgress())
        if entry.completed:
            continue
        before = entry.progress
        if kind == "class_level":
            # values are ("<class>", "<level>")
            level = max((int(v) for v in wanted if v.isdigit()), default=0)
            entry.progress = max(entry.progress, min(level, definition.target))
        elif kind in _EQUIPMENT_KINDS:
            # A set requires simultaneous equipment, never repeated clicks on
            # one item. Each inventory item contributes once to the snapshot.
            entry.progress = max(entry.progress, min(hits, definition.target))
        elif kind in _ABSOLUTE_KINDS:
            entry.progress = max(entry.progress, min(max(0, int(absolute_progress)), definition.target))
        else:
            entry.progress = min(definition.target, entry.progress + hits * int(count))
        completed = _complete(definition, entry, state, now)
        if entry.progress != before or completed:
            changes.append(AchievementChange(definition, entry, completed))
            completed_any = completed_any or completed
    if completed_any:
        changes.extend(refresh_derived_achievements(catalog, state, now=now))
    return tuple(changes)


def refresh_derived_achievements(
    catalog: AchievementCatalog,
    state: dict[str, AchievementProgress],
    *,
    now: datetime | None = None,
) -> tuple[AchievementChange, ...]:
    """Re-evaluate the achievements that count other achievements.

    ``points`` rows track the sum of points of completed achievements;
    ``unlockachievements`` rows count how many of their listed achievements
    are complete.  Runs until nothing changes, since a points milestone can
    itself carry points.
    """

    changes: list[AchievementChange] = []
    for _ in range(8):
        round_changes = []
        # Rules that reached their target while a prerequisite was still
        # open complete as soon as that prerequisite does.
        for key, progress in list(state.items()):
            definition = catalog.achievement(key)
            # Rows without an evaluator (retired "nomob" kills) never
            # complete, whatever an older save recorded for them.
            if definition is None or progress.completed or definition.event_kind is None:
                continue
            if progress.progress >= definition.target and _complete(
                definition, progress, state, now,
            ):
                round_changes.append(AchievementChange(definition, progress, True))
        total_points = sum(
            catalog.achievement(key).points
            for key, progress in state.items()
            if progress.completed and catalog.achievement(key) is not None
        )
        for definition in catalog.for_event("points"):
            progress = state.setdefault(definition.key, AchievementProgress())
            if progress.completed:
                continue
            value = min(definition.target, total_points)
            if value != progress.progress or value >= definition.target:
                before = progress.progress
                progress.progress = value
                completed = _complete(definition, progress, state, now)
                if progress.progress != before or completed:
                    round_changes.append(AchievementChange(definition, progress, completed))
        for definition in catalog.for_event("unlockachievements"):
            progress = state.setdefault(definition.key, AchievementProgress())
            if progress.completed:
                continue
            done = sum(
                1 for key in definition.params
                if state.get(key) is not None and state[key].completed
            )
            value = min(definition.target, done)
            if value != progress.progress or value >= definition.target:
                before = progress.progress
                progress.progress = value
                completed = _complete(definition, progress, state, now)
                if progress.progress != before or completed:
                    round_changes.append(AchievementChange(definition, progress, completed))
        if not round_changes:
            break
        changes.extend(round_changes)
        if not any(change.completed_now for change in round_changes):
            break
    return tuple(changes)


def complete_achievement(
    state: dict[str, AchievementProgress],
    definition: AchievementDefinition,
    *,
    now: datetime | None = None,
) -> bool:
    """Mark ``definition`` complete at its target if it is not already.

    Used for the recipe-gate achievements the local server grants outright;
    the timestamp is written once and then persists.
    """

    progress = state.setdefault(definition.key, AchievementProgress())
    if progress.completed:
        return False
    progress.progress = definition.target
    progress.completed_at = _now_stamp(now)
    return True


__all__ = [
    "SUPPORTED_TYPES",
    "TIMESTAMP_FORMAT",
    "AchievementCatalog",
    "AchievementChange",
    "AchievementDefinition",
    "AchievementProgress",
    "apply_achievement_event",
    "battleground_mode",
    "complete_achievement",
    "load_achievement_catalog",
    "refresh_derived_achievements",
]
