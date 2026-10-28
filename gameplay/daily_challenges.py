"""Daily challenge offers from static content, with detached character state."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
import random
from types import MappingProxyType
from typing import Mapping

from dso_server.runtime import resident_content
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
        return DailyChallengeState(
            current.day, tuple(entries), bonuses, current.spender,
            current.spender_progress, current.spender_selections,
        )


def load_daily_challenge_catalog(database_path: str | Path) -> DailyChallengeCatalog:
    path = Path(database_path)
    stamp = resident_content.stat(path)
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
    spender: tuple[TimedChallenge, ...] = ()
    spender_progress: int = 0
    spender_selections: tuple[str, ...] = ()

    def info(self, *, include_spender: bool = False) -> TimedChallengesInfo:
        # The historical weekly/monthly codec names actually describe bonus
        # objectives and RC spender rewards, not calendar periods.
        return TimedChallengesInfo(
            0, self.daily, self.bonuses,
            self.spender if include_spender else (), (),
        )

    def to_document(self) -> dict:
        return {"day": self.day, "daily": [asdict(e) for e in self.daily],
                "bonuses": [asdict(e) for e in self.bonuses],
                "spender": [asdict(e) for e in self.spender],
                "spender_progress": self.spender_progress,
                "spender_selections": list(self.spender_selections)}

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
        spender_progress = int(document.get("spender_progress", 0))
        spender_selections = tuple(
            str(value) for value in document.get("spender_selections", ())
        )
        if not 0 <= spender_progress <= 0xffffffff:
            raise ValueError("invalid saved tier spender progress")
        if len(set(spender_selections)) != len(spender_selections):
            raise ValueError("duplicate saved tier spender selection")
        return cls(day, entries("daily"), entries("bonuses"), entries("spender"),
                   spender_progress, spender_selections)


@dataclass(frozen=True)
class SpenderChallengeDefinition:
    resource_id: str
    slot: str
    weight: int
    minimum_level: int
    maximum_level: int
    class_names: frozenset[str]
    activation_event: str
    completed_quest: str
    memberships: frozenset[str]


def load_tier_spender_challenges(
    database_path: str | Path,
    facts: ChallengeEligibility,
    *,
    membership: str = "NoPrivilege",
    rng=None,
    slots: tuple[str, ...] = ("copper", "silver", "gold"),
    excluded_ids: frozenset[str] = frozenset(),
) -> tuple[TimedChallenge, ...]:
    """Select four client-resolvable rewards for each requested RC tier."""
    path = Path(database_path)
    stamp = resident_content.stat(path)
    definitions = _load_spender_challenge_definitions(
        str(path), stamp.st_mtime_ns, stamp.st_size
    )
    active_events = facts.active_events
    closed_quests = facts.closed_quests
    membership_name = str(membership or "NoPrivilege").casefold()
    generator = rng or random.SystemRandom()
    selected = []
    requested_slots = tuple(str(slot).casefold() for slot in slots)
    if not requested_slots or any(
        slot not in {"copper", "silver", "gold"} for slot in requested_slots
    ):
        raise ValueError("invalid tier spender slot selection")
    excluded = frozenset(str(value) for value in excluded_ids)
    for slot in requested_slots:
        candidates = [entry for entry in definitions
                      if entry.slot == slot and entry.weight > 0
                      and entry.resource_id not in excluded
                      and entry.minimum_level <= facts.level <= entry.maximum_level
                      and (not entry.class_names or facts.class_name in entry.class_names)
                      and (not entry.activation_event or entry.activation_event in active_events)
                      and (not entry.completed_quest or entry.completed_quest in closed_quests)
                      and membership_name in entry.memberships]
        for _ in range(4):
            if not candidates:
                break
            choice = generator.choices(
                candidates, weights=[entry.weight for entry in candidates], k=1
            )[0]
            selected.append(TimedChallenge(
                choice.resource_id, choice.resource_id, AVAILABLE, 0,
                ZERO_TIMESTAMP,
            ))
            candidates.remove(choice)
    return tuple(selected)


def load_tier_spender_shuffle_prices(
    database_path: str | Path,
) -> dict[str, int]:
    """Read the authored Andermant cost for each spender shuffle tier."""

    names = {
        "copper": "TierSpenderDealsShufflePriceCopper",
        "silver": "TierSpenderDealsShufflePriceSilver",
        "gold": "TierSpenderDealsShufflePriceGold",
    }
    # connect() returns the thread's cached read-only connection. Do not close
    # it here: later gameplay queries on that worker reuse the same handle.
    database = connect(database_path)
    row = database.execute(
        "SELECT " + ", ".join(f'"{column}"' for column in names.values())
        + " FROM _Globals LIMIT 1"
    ).fetchone()
    if row is None:
        raise ValueError("static globals do not define spender shuffle prices")
    prices = {slot: int(row[index]) for index, slot in enumerate(names)}
    if any(price < 0 or price > 0xFFFFFFFF for price in prices.values()):
        raise ValueError("spender shuffle price is outside the currency range")
    return prices


@lru_cache(maxsize=4)
def _load_spender_challenge_definitions(path, mtime_ns, size):
    del mtime_ns, size
    database = connect(path)
    definitions = []
    for row in database.execute(
        'SELECT Id, SpenderSlot, Weighting, MinLevel, MaxLevel, CharClass, '
        'EventId, QuestIdCompleted, Membership '
        'FROM "_Template_RCSpenderRewards" WHERE Obsolete = 0 ORDER BY rowid'
    ):
        definitions.append(SpenderChallengeDefinition(
            str(row[0]), str(row[1] or "").casefold(), int(row[2] or 0),
            int(row[3] or 0), int(row[4] or 0),
            frozenset(part.strip().casefold() for part in str(row[5] or "").split(";")
                      if part.strip()),
            str(row[6] or "").casefold(), str(row[7] or "").casefold(),
            frozenset(part.strip().casefold() for part in str(row[8] or "").split(";")
                      if part.strip()),
        ))
    return tuple(definitions)
