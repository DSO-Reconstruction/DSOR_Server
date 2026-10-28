"""Static Wanted mission pools and detached per-character board state."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import random
from types import MappingProxyType
from typing import Mapping

from dso_server.runtime import resident_content
from dso_server.persistence.readonly_db import connect


@dataclass(frozen=True)
class WantedMission:
    mission_id: int
    quest_id: str
    weight: int


@dataclass(frozen=True)
class WantedCatalog:
    missions: Mapping[int, WantedMission]
    pools: tuple[tuple[int, ...], ...]
    base_times: int
    vip_times: int
    single_refresh_cost: int
    all_refresh_cost: int
    vip_buy_cost: int

    def roll(
        self, current: tuple[int, ...], slots: tuple[int, ...], *,
        excluded: frozenset[int] = frozenset(), rng=None,
    ) -> tuple[int, ...]:
        """Choose distinct offers from each requested zero-based slot's pool.

        Retain other slots. A refresh never silently returns the old offer or
        one whose quest the character already has in progress.
        """
        chooser = rng or random.SystemRandom()
        result = list(current)
        for slot in slots:
            candidates = tuple(
                identity for identity in self.pools[slot]
                if identity not in result and identity not in excluded
            )
            if not candidates:
                raise ValueError(f"no Wanted mission available for slot {slot + 1}")
            result[slot] = chooser.choices(
                candidates, weights=[self.missions[i].weight for i in candidates], k=1,
            )[0]
        return tuple(result)


def load_wanted_catalog(database_path: str | Path) -> WantedCatalog:
    path = Path(database_path)
    stat = resident_content.stat(path)
    return _load_catalog(str(path), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=4)
def _load_catalog(path: str, mtime_ns: int, size: int) -> WantedCatalog:
    del mtime_ns, size
    database = connect(path)
    missions = {
        int(identity): WantedMission(int(identity), str(quest), int(weight))
        for identity, quest, weight in database.execute(
            "SELECT Id, QuestId, WantedMissionWeight FROM _Template_WantedMissionAll"
        )
        if quest and int(weight) > 0
    }
    pools = {}
    for slot, identities in database.execute(
        'SELECT "Index", QuestIds FROM _Template_WantedMissionClassify ORDER BY "Index"'
    ):
        slot = int(slot)
        if slot in pools or slot not in range(1, 5):
            raise ValueError(f"invalid or duplicate Wanted pool slot {slot}")
        pools[slot] = tuple(dict.fromkeys(
            int(value) for value in str(identities).split(";")
            if value and int(value) in missions
        ))
    if set(pools) != {1, 2, 3, 4} or any(not values for values in pools.values()):
        raise ValueError("Wanted content requires four nonempty mission pools")
    settings = database.execute(
        "SELECT WantedBaseTimes, WantedVIPTimes, WantedSingleRefreshRC, "
        "WantedAllRefreshRC, WantedVIPBuyRC FROM _Globals LIMIT 1"
    ).fetchone()
    if settings is None or any(int(value) < 0 for value in settings):
        raise ValueError("invalid Wanted mission settings")
    return WantedCatalog(
        MappingProxyType(missions), tuple(pools[i] for i in range(1, 5)),
        *(int(value) for value in settings),
    )


@dataclass(frozen=True)
class WantedState:
    day: str
    remaining: int
    special_remaining: int
    mission_ids: tuple[int, ...]
    statuses: tuple[int, ...]
    vip_expires_at: float = 0.0

    def to_document(self) -> dict[str, object]:
        return {
            "day": self.day, "remaining": self.remaining,
            "special_remaining": self.special_remaining,
            "mission_ids": list(self.mission_ids), "statuses": list(self.statuses),
            "vip_expires_at": self.vip_expires_at,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> WantedState | None:
        if not document:
            return None
        missions = tuple(int(value) for value in document["mission_ids"])
        statuses = tuple(int(value) for value in document["statuses"])
        remaining = int(document["remaining"])
        special_remaining = int(document["special_remaining"])
        if (len(missions) != 4 or len(statuses) != 4
                or any(not 0 <= value <= 0xFFFFFFFF
                       for value in (*missions, *statuses, remaining, special_remaining))):
            raise ValueError("invalid saved Wanted mission state")
        return cls(
            str(document["day"]), remaining, special_remaining, missions, statuses,
            float(document.get("vip_expires_at", 0)),
        )
