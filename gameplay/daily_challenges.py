"""Daily challenge offers from static content, with detached character state."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
import random
from types import MappingProxyType
from typing import Mapping

from dso_server.persistence.readonly_db import connect
from dso_server.protocol.player_init_protocol import TimedChallenge, TimedChallengesInfo


CATEGORIES = ("Cooking", "Generic", "PvP", "Boss")
AVAILABLE = 0
ACCEPTED = 2
COMPLETED = 3
ZERO_TIMESTAMP = (0, 0, 0, 0, 0, 0)


@dataclass(frozen=True)
class ChallengeEligibility:
    level: int
    class_name: str
    wisdom_level: int = 0
    closed_quests: frozenset[str] = frozenset()
    active_events: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DailyChallengeDefinition:
    resource_id: str
    category: str
    tier: int
    weight: int
    minimum_level: int
    maximum_level: int
    class_name: str
    minimum_wisdom: int
    maximum_wisdom: int
    activation_quest: str
    deactivation_quest: str
    activation_event: str

    objective: str = ''
    objective_value: str = ''
    difficulty: str = 'Any'
    reward: str = ''
    alternative_reward: str = ''
    reward_effect: str = ''

    @property
    def target(self):
        if self.objective == 'SpecificMonsterKilled':
            return 1
        value = self.objective_value.rsplit(':', 1)[-1]
        return max(1, int(value))

    def increment(self, kind, *, value='', count=1, difficulty=0):
        if self.objective != kind:
            return 0
        if self.difficulty != 'Any' and difficulty < int(self.difficulty):
            return 0
        if kind in {'SpecificMonsterKilled', 'ItemCollected'}:
            targets = self.objective_value.rsplit(':', 1)[0].casefold().split('|')
            if value.casefold() not in targets:
                return 0
        return max(0, int(count))

    def eligible(self, facts: ChallengeEligibility) -> bool:
        return (
            facts.level >= 10
            and self.minimum_level <= facts.level <= self.maximum_level
            and (not self.class_name or self.class_name == facts.class_name)
            and self.minimum_wisdom <= facts.wisdom_level
            and (not self.maximum_wisdom or facts.wisdom_level <= self.maximum_wisdom)
            and (not self.activation_quest or self.activation_quest in facts.closed_quests)
            and (not self.deactivation_quest or self.deactivation_quest not in facts.closed_quests)
            and (not self.activation_event or self.activation_event in facts.active_events)
        )

    def entry(self, state: int = AVAILABLE) -> TimedChallenge:
        # The wire map key and the embedded challenge template ID match retail.
        return TimedChallenge(self.resource_id, self.resource_id, state, 0, ZERO_TIMESTAMP)


@dataclass(frozen=True)
class DailyChallengeCatalog:
    definitions: Mapping[str, DailyChallengeDefinition]
    refresh_cost: int
    free_refresh_memberships: frozenset[int]

    def choose(self, category, tier, facts, *, excluding="", rng=None):
        candidates = tuple(d for d in self.definitions.values()
                           if d.category == category and d.tier == tier
                           and d.weight > 0 and d.resource_id != excluding
                           and d.eligible(facts))
        if not candidates:
            return None
        return (rng or random.SystemRandom()).choices(
            candidates, weights=[d.weight for d in candidates], k=1,
        )[0]

    def board(self, current, day: date, facts: ChallengeEligibility, *, rng=None):
        if facts.level < 10:
            return None
        if current is None or current.day < day.isoformat():
            current = DailyChallengeState(day.isoformat(), (), ())
        existing = {self.definitions[e.challenge_id].category: e
                    for e in current.daily if e.challenge_id in self.definitions and e.state != COMPLETED}
        entries = [e for e in current.daily if e.state == COMPLETED
                   and e.challenge_id in self.definitions]
        for category in CATEGORIES:
            entry = existing.get(category)
            completed_tier = max((self.definitions[e.challenge_id].tier for e in entries
                                  if self.definitions[e.challenge_id].category == category), default=0)
            tier = self.definitions[entry.challenge_id].tier if entry else completed_tier + 1
            if (entry is not None and entry.state == AVAILABLE
                    and not self.definitions[entry.challenge_id].eligible(facts)):
                entry = None
            if entry is None:
                definition = self.choose(category, tier, facts, rng=rng)
                if definition is not None:
                    entry = definition.entry()
            if entry is not None:
                entries.append(entry)
        bonus = {e.challenge_id: e for e in current.bonuses}
        bonuses = tuple(bonus.get(d.resource_id, d.entry(ACCEPTED))
                        for d in self.definitions.values()
                        if d.category == "BonusReward" and d.eligible(facts))
        return DailyChallengeState(current.day, tuple(entries), bonuses)


def load_daily_challenge_catalog(database_path: str | Path) -> DailyChallengeCatalog:
    path = Path(database_path)
    stamp = path.stat()
    return _load_catalog(str(path), stamp.st_mtime_ns, stamp.st_size)


@lru_cache(maxsize=4)
def _load_catalog(path, mtime_ns, size):
    del mtime_ns, size
    database = connect(path)
    definitions = {}
    for row in database.execute(
        "SELECT Id, Category, Tier, ProbabilityWeighting, MinPlayerLevel, "
        "MaxPlayerLevel, CharClass, MinWisdomLevel, MaxWisdomLevel, "
        "ActivationQuest, DeactivationQuest, ActivationEvent, Objective, ObjectiveValue, "
        "ChallengeDifficulty, Reward, AlternativeReward, RewardStatusEffect "
        "FROM _Template_DailyChallenges ORDER BY rowid"
    ):
        definition = DailyChallengeDefinition(
            str(row[0]), str(row[1]), *(int(v) for v in row[2:6]),
            str(row[6] or "").casefold(), int(row[7]), int(row[8]),
            *(str(v or "").casefold() for v in row[9:12]),
            *(str(v or "") for v in row[12:18]),
        )
        definitions[definition.resource_id] = definition
    cost = int(database.execute(
        "SELECT DailyAchievementReshuffleCosts FROM _Globals LIMIT 1"
    ).fetchone()[0])
    sources = {"Premium": 1, "Deluxe": 2}
    free = frozenset(sources[source] for source, enabled in database.execute(
        "SELECT PrivilegeSource, DailyChallengeFreeReshuffle FROM _Template_Privileges"
    ) if source in sources and enabled)
    if cost < 0:
        raise ValueError("negative daily challenge refresh cost")
    return DailyChallengeCatalog(MappingProxyType(definitions), cost, free)


@dataclass(frozen=True)
class DailyChallengeState:
    day: str
    daily: tuple[TimedChallenge, ...]
    bonuses: tuple[TimedChallenge, ...]

    def info(self) -> TimedChallengesInfo:
        # The historical weekly/monthly codec names actually describe bonus
        # objectives and RC spender rewards. Spender rewards are independent.
        return TimedChallengesInfo(0, self.daily, self.bonuses, ())

    def to_document(self) -> dict:
        return {"day": self.day, "daily": [asdict(e) for e in self.daily],
                "bonuses": [asdict(e) for e in self.bonuses]}

    @classmethod
    def from_document(cls, document):
        if not document:
            return None
        day = date.fromisoformat(document["day"]).isoformat()
        def entries(key):
            result = []
            for value in document.get(key, ()):
                entry = TimedChallenge(
                    str(value["challenge_id"]), str(value["schedule_id"]),
                    int(value["state"]), int(value["progress"]), tuple(value["timestamp"]),
                )
                if (entry.state not in (0, 1, 2, 3) or not 0 <= entry.progress <= 0xffffffff
                        or len(entry.timestamp) != 6):
                    raise ValueError("invalid saved daily challenge")
                result.append(entry)
            return tuple(result)
        return cls(day, entries("daily"), entries("bonuses"))
