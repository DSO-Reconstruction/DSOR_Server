"""Static Season Pass mission catalog and detached character progress."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Mapping

from dso_server.paths import DEFAULT_SEASON_PASS_CONFIG_PATH
from dso_server.persistence.readonly_db import connect


# The client runs its Season Pass UI while the event named by
# _Globals.SeasonPassEventId is active; each run of it is one season.
DEFAULT_SEASON_PASS_EVENT_ID = "event_season_ticket"
# Reward columns per track: free (no ticket), gold (pass types 1/3) and red
# (pass types 2/4). The 2026 client added a second set for its temp event.
SEASON_PASS_TRACKS = ("free", "gold", "red")
SEASON_PASS_REWARD_COLUMNS = {
    "standard": ("SeasonPassAward1", "SeasonPassAward2", "SeasonPassAward3"),
    "alternate": ("SeasonPassAward4", "SeasonPassAward5", "SeasonPassAward6"),
}
MAX_SEASON_PASS_REWARD_AMOUNT = 1_000_000


@dataclass(frozen=True)
class SeasonPassMission:
    mission_id: int
    target: int
    objective: str
    experience: int
    special: str
    difficulty: str


@dataclass(frozen=True)
class SeasonPassCatalog:
    missions: Mapping[int, SeasonPassMission]
    level_thresholds: tuple[tuple[int, int], ...]
    reward_levels: Mapping[int, tuple[str, str, str]]

    def level_for_experience(self, experience: int) -> int:
        return sum(1 for _level, threshold in self.level_thresholds
                   if int(experience) >= threshold)


def parse_season_pass_reward(value: str) -> tuple[str, int]:
    """Split a ``Loot:<item>,<amount>`` reward cell."""
    if not isinstance(value, str) or not value.startswith("Loot:"):
        raise ValueError(f"Season Pass reward {value!r} has no static loot entry")
    item_id, separator, raw_amount = value[5:].rpartition(",")
    try:
        amount = int(raw_amount)
    except ValueError:
        amount = 0
    if not separator or not item_id or amount <= 0:
        raise ValueError(f"Season Pass reward {value!r} has invalid static loot")
    return item_id, amount


def season_pass_event_id(database_path: str | Path) -> str:
    path = Path(database_path)
    stat = path.stat()
    return _season_pass_event_id(str(path), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=4)
def _season_pass_event_id(path: str, mtime_ns: int, size: int) -> str:
    del mtime_ns, size
    try:
        row = connect(path).execute("SELECT SeasonPassEventId FROM _Globals").fetchone()
    except sqlite3.Error:
        row = None
    value = str(row[0] or "").strip() if row is not None else ""
    return value or DEFAULT_SEASON_PASS_EVENT_ID


def season_pass_reward_set(database, alternate_rewards: bool) -> str:
    """The reward column set in use: the alternate one only if the table has it."""
    if not alternate_rewards:
        return "standard"
    columns = {str(row[1]) for row in database.execute(
        "PRAGMA table_info(_Template_SeasonPassRewards)"
    )}
    return ("alternate" if set(SEASON_PASS_REWARD_COLUMNS["alternate"]) <= columns
            else "standard")


def read_season_pass_rewards(
    database, reward_set: str,
) -> dict[int, tuple[int, str, str, str]]:
    """Level -> (experience threshold, free, gold, red) from one database."""
    columns = SEASON_PASS_REWARD_COLUMNS[reward_set]
    return {
        int(level): (int(experience), *(str(value or "") for value in awards))
        for level, experience, *awards in database.execute(
            "SELECT SeasonPassLevel, SeasonPassExp, " + ", ".join(columns)
            + " FROM _Template_SeasonPassRewards ORDER BY SeasonPassLevel"
        )
    }


def write_season_pass_rewards(
    database, reward_set: str, rows: Mapping[int, tuple[int, str, str, str]],
) -> int:
    """Update reward rows in place (rowids never move); returns changed rows."""
    columns = SEASON_PASS_REWARD_COLUMNS[reward_set]
    current = read_season_pass_rewards(database, reward_set)
    changed = 0
    for level, values in sorted(rows.items()):
        if int(level) not in current:
            raise ValueError(f"Season Pass level {level} is not in this database")
        if current[int(level)] == tuple(values):
            continue
        database.execute(
            "UPDATE _Template_SeasonPassRewards SET SeasonPassExp=?, "
            + ", ".join(f"{column}=?" for column in columns)
            + " WHERE SeasonPassLevel=?",
            (int(values[0]), *values[1:], int(level)),
        )
        changed += 1
    return changed


def parse_season_pass_overrides(document) -> dict[str, dict[int, dict[str, object]]]:
    """Validate the CMS overrides document: rewards[set][level] = fields."""
    if not isinstance(document, dict):
        raise ValueError("Season Pass configuration must be an object")
    rewards = document.get("rewards", {})
    if not isinstance(rewards, dict) or not set(rewards) <= set(SEASON_PASS_REWARD_COLUMNS):
        raise ValueError("Season Pass rewards must be keyed by reward set")
    result = {}
    for reward_set, levels in rewards.items():
        if not isinstance(levels, dict):
            raise ValueError(f"Season Pass {reward_set} rewards must be an object")
        parsed = {}
        for raw_level, fields in levels.items():
            level = int(raw_level)
            if level <= 0 or not isinstance(fields, dict):
                raise ValueError(f"invalid Season Pass level {raw_level!r}")
            if not set(fields) <= {"exp", *SEASON_PASS_TRACKS}:
                raise ValueError(f"unknown fields for Season Pass level {level}")
            entry = {}
            if "exp" in fields:
                if type(fields["exp"]) is not int or fields["exp"] < 0:
                    raise ValueError(f"invalid experience for Season Pass level {level}")
                entry["exp"] = fields["exp"]
            for track in SEASON_PASS_TRACKS:
                if track in fields:
                    _, amount = parse_season_pass_reward(fields[track])
                    if amount > MAX_SEASON_PASS_REWARD_AMOUNT:
                        raise ValueError(f"Season Pass level {level} {track} amount is too large")
                    entry[track] = fields[track]
            parsed[level] = entry
        result[reward_set] = parsed
    return result


def apply_season_pass_overrides(
    rows: Mapping[int, tuple[int, str, str, str]],
    overrides: Mapping[int, Mapping[str, object]],
) -> dict[int, tuple[int, str, str, str]]:
    result = {level: tuple(values) for level, values in rows.items()}
    for level, fields in overrides.items():
        if level not in result:
            continue
        experience, *awards = result[level]
        result[level] = (
            int(fields.get("exp", experience)),
            *(str(fields.get(track, award))
              for track, award in zip(SEASON_PASS_TRACKS, awards)),
        )
    return result


def effective_season_pass_rewards(
    database, overrides: Mapping[str, Mapping[int, Mapping[str, object]]],
) -> dict[str, dict[int, tuple[int, str, str, str]]]:
    """Every reward set this database has, with the CMS overrides applied."""
    return {
        reward_set: apply_season_pass_overrides(
            read_season_pass_rewards(database, reward_set),
            overrides.get(reward_set, {}),
        )
        for reward_set in SEASON_PASS_REWARD_COLUMNS
        if season_pass_reward_set(database, reward_set == "alternate") == reward_set
    }


def load_season_pass_overrides(
    path: str | Path | None = None,
) -> Mapping[str, Mapping[int, Mapping[str, object]]]:
    path = Path(path or DEFAULT_SEASON_PASS_CONFIG_PATH)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return MappingProxyType({})
    return _load_overrides(str(path), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=4)
def _load_overrides(path: str, mtime_ns: int, size: int):
    del mtime_ns, size
    try:
        parsed = parse_season_pass_overrides(
            json.loads(Path(path).read_text(encoding="utf-8"))
        )
    except (OSError, ValueError) as exc:
        # Rewards are granted from gameplay handlers; a hand-edited file must
        # not break them. The CMS validates before it writes the file.
        print(f"[SeasonPass] ignoring invalid configuration {path}: {exc}")
        return MappingProxyType({})
    return MappingProxyType({
        reward_set: MappingProxyType({
            level: MappingProxyType(fields) for level, fields in levels.items()
        })
        for reward_set, levels in parsed.items()
    })


def load_season_pass_catalog(
    database_path: str | Path, *, alternate_rewards: bool = False,
    overrides_path: str | Path | None = None,
) -> SeasonPassCatalog:
    path = Path(database_path)
    stat = path.stat()
    overrides = load_season_pass_overrides(overrides_path)
    frozen = tuple(
        (reward_set, tuple(sorted(
            (level, tuple(sorted(fields.items())))
            for level, fields in levels.items()
        )))
        for reward_set, levels in sorted(overrides.items())
    )
    return _load_catalog(
        str(path), stat.st_mtime_ns, stat.st_size, bool(alternate_rewards), frozen,
    )


@lru_cache(maxsize=4)
def _load_catalog(
    path: str, mtime_ns: int, size: int, alternate_rewards: bool,
    overrides: tuple = (),
) -> SeasonPassCatalog:
    del mtime_ns, size
    database = connect(path)
    missions = {}
    for identity, target, objective, experience, _special, _difficulty in database.execute(
        "SELECT Id, SeasonPassTargetValue, Objective, SeasonPassExp, "
        "SeasonPassSpecial, ChallengeDifficulty "
        "FROM _Template_SeasonPassMissions ORDER BY Priority, Id"
    ):
        mission_id = int(identity)
        if mission_id in missions or int(target) < 0 or int(experience) < 0:
            raise ValueError(f"invalid Season Pass mission {identity!r}")
        missions[mission_id] = SeasonPassMission(
            mission_id, int(target), str(objective or ""), int(experience),
            str(_special or ""), str(_difficulty or "Any"),
        )
    if not missions:
        raise ValueError("static database has no Season Pass missions")
    reward_set = season_pass_reward_set(database, alternate_rewards)
    rows = apply_season_pass_overrides(
        read_season_pass_rewards(database, reward_set),
        {level: dict(fields) for level, fields in dict(overrides).get(reward_set, ())},
    )
    if not rows:
        raise ValueError("static database has no Season Pass reward levels")
    thresholds = tuple((level, values[0]) for level, values in sorted(rows.items()))
    reward_levels = {level: tuple(values[1:]) for level, values in sorted(rows.items())}
    return SeasonPassCatalog(
        MappingProxyType(missions), thresholds,
        MappingProxyType(reward_levels),
    )


def normalize_season_pass_state(document) -> dict[str, object]:
    document = document if isinstance(document, dict) else {}
    pass_types = tuple(sorted({
        int(value) for value in document.get(
            "pass_types", document.get("claimed_free", ())
        ) if 1 <= int(value) <= 4
    }))
    claimed_awards = tuple(sorted({
        int(value) for value in document.get(
            "claimed_awards", document.get("claimed_premium", ())
        ) if int(value) >= 0
    }))
    awarded_awards = tuple(sorted({
        int(value) for value in document.get("awarded_awards", ())
        if int(value) >= 0
    }))
    # Version 1 means the server's delivery ledger was written by the handler
    # that actually granted the corresponding rewards. Older ledgers recorded
    # client claim markers as deliveries, even when no reward was issued.
    delivery_ledger_version = max(0, int(document.get("delivery_ledger_version", 0)))
    progress = {}
    for identity, value in document.get("mission_progress", {}).items():
        identity, value = int(identity), int(value)
        if identity >= 0 and value >= 0:
            progress[identity] = value
    level = max(0, int(document.get("level", 0)))
    experience = max(0, int(document.get("experience", 0)))
    login_day = str(document.get("login_day", ""))
    return {
        "level": level,
        "experience": experience,
        "pass_types": pass_types,
        "claimed_awards": claimed_awards,
        "awarded_awards": awarded_awards,
        "delivery_ledger_version": delivery_ledger_version,
        "mission_progress": progress,
        "login_day": login_day,
        # Event instance of the season this progress belongs to; 0 = not yet
        # bound (saves from before seasons were tracked).
        "season_id": max(0, int(document.get("season_id", 0))),
    }


def season_pass_state_for_season(document, season_id: int | None) -> dict[str, object]:
    """Bind progress to the running season; a different season starts it over.

    Unbound progress is adopted by the first season it meets, so existing saves
    keep their levels. Activated tickets (pass types) carry over.
    """
    state = normalize_season_pass_state(document)
    if season_id is None or state["season_id"] == int(season_id):
        return state
    if state["season_id"]:
        state.update(
            level=0, experience=0, claimed_awards=(), awarded_awards=(),
            mission_progress={}, login_day="",
        )
    state["season_id"] = int(season_id)
    return state
