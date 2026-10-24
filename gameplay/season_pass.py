"""Static Season Pass mission catalog and detached character progress."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from dso_server.persistence.readonly_db import connect


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


def load_season_pass_catalog(
    database_path: str | Path, *, alternate_rewards: bool = False,
) -> SeasonPassCatalog:
    path = Path(database_path)
    stat = path.stat()
    return _load_catalog(
        str(path), stat.st_mtime_ns, stat.st_size, bool(alternate_rewards),
    )


@lru_cache(maxsize=4)
def _load_catalog(
    path: str, mtime_ns: int, size: int, alternate_rewards: bool,
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
    thresholds = tuple(
        (int(level), int(experience))
        for level, experience in database.execute(
            "SELECT SeasonPassLevel, SeasonPassExp "
            "FROM _Template_SeasonPassRewards "
            "GROUP BY SeasonPassLevel, SeasonPassExp "
            "ORDER BY SeasonPassLevel"
        )
    )
    if not thresholds:
        raise ValueError("static database has no Season Pass reward levels")
    columns = {str(row[1]) for row in database.execute(
        "PRAGMA table_info(_Template_SeasonPassRewards)"
    )}
    award_columns = (
        ("SeasonPassAward4", "SeasonPassAward5", "SeasonPassAward6")
        if alternate_rewards and all(
            f"SeasonPassAward{index}" in columns for index in (4, 5, 6)
        )
        else ("SeasonPassAward1", "SeasonPassAward2", "SeasonPassAward3")
    )
    reward_levels = {
        int(level): tuple(str(value or "") for value in awards)
        for level, *awards in database.execute(
            "SELECT SeasonPassLevel, " + ", ".join(award_columns)
            + " FROM _Template_SeasonPassRewards "
            "ORDER BY SeasonPassLevel"
        )
    }
    if not reward_levels:
        raise ValueError("static database has no Season Pass rewards")
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
    }
