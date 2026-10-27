"""Account veteran ranks (``_Template_VeteranLevel``).

The client's Veteran Rank tab (gui.window ``VeterantierButton1..3``) lists the
requirements this module evaluates: "Your account is at least 4 / 5 / 7 years
old" and "Have one / two / three level 100 character(s)".  The static rows
carry them as ``MinAmount`` (years) and ``Amount`` (characters), the level is
``_Globals.VeteranCharacterLevel`` (100), and ``Id`` (``Veteran1``..``3``) is
the value ``VeteranLevelUnlock`` achievements list in their Param.

The 2026-07-20 official capture shows a Veteran2 account (NewPlayer +0x200
``"Veteran2"``) whose ``veteran_level_01``/``_02`` completions are stamped
2024-09-29 and 2025-09-29: one rank per account anniversary, so account age
counts whole calendar years.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import sqlite3
from typing import Iterable


@dataclass(frozen=True)
class VeteranLevel:
    level_id: str          # _Template_VeteranLevel.Id, e.g. "Veteran2"
    index: int             # Index (1..3)
    min_account_years: int  # MinAmount
    min_characters: int    # Amount: characters at the veteran level


@dataclass(frozen=True)
class VeteranRules:
    character_level: int   # _Globals.VeteranCharacterLevel
    levels: tuple[VeteranLevel, ...]  # ascending Index


@lru_cache(maxsize=4)
def _load(path: str, mtime_ns: int, size: int) -> VeteranRules:
    del mtime_ns, size
    with sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True) as database:
        rows = database.execute(
            "SELECT Id, CAST(\"Index\" AS INTEGER), CAST(MinAmount AS INTEGER), "
            "CAST(Amount AS INTEGER) FROM _Template_VeteranLevel "
            "ORDER BY CAST(\"Index\" AS INTEGER)"
        ).fetchall()
        level = database.execute(
            "SELECT VeteranCharacterLevel FROM _Globals LIMIT 1"
        ).fetchone()
    if level is None or not level[0]:
        raise ValueError("_Globals.VeteranCharacterLevel is missing")
    return VeteranRules(
        int(level[0]),
        tuple(
            VeteranLevel(str(row[0]), int(row[1]), int(row[2]), int(row[3]))
            for row in rows if str(row[0] or "").strip()
        ),
    )


def load_veteran_rules(static_database_path: str | Path) -> VeteranRules:
    path = Path(static_database_path)
    stat = path.stat()
    return _load(str(path), stat.st_mtime_ns, stat.st_size)


def account_age_years(created_at: float, now: datetime | None = None) -> int:
    """Whole years since ``created_at`` (UTC epoch seconds), by anniversary."""

    created = datetime.fromtimestamp(float(created_at), timezone.utc)
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    years = moment.year - created.year
    if (moment.month, moment.day, moment.time()) < (created.month, created.day, created.time()):
        years -= 1
    return max(0, years)


def veteran_level(
    rules: VeteranRules,
    *,
    account_created_at: float,
    character_levels: Iterable[int],
    now: datetime | None = None,
) -> VeteranLevel | None:
    """The highest rank whose account-age and character requirements hold."""

    if float(account_created_at) <= 0:
        return None
    years = account_age_years(account_created_at, now)
    veterans = sum(1 for level in character_levels if int(level) >= rules.character_level)
    reached = None
    for candidate in rules.levels:
        if years >= candidate.min_account_years and veterans >= candidate.min_characters:
            reached = candidate
    return reached


__all__ = [
    "VeteranLevel",
    "VeteranRules",
    "account_age_years",
    "load_veteran_rules",
    "veteran_level",
]
