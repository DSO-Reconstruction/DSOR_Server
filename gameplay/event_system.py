"""Database-driven runtime game-event scheduling and progression.

The client owns the presentation of an event, but the server owns the event
instance: its runtime id, schedule, participation, progress, rewards, loot and
map availability.  Static definitions remain in ``static.db4``; the small JSON
schedule selects which definitions are active/upcoming without modifying the
shared game database.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
import json
import math
from pathlib import Path
import random
import re
import sqlite3
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping

from dso_server.runtime import resident_content
from dso_server.persistence import static_schema_compat as compat
from dso_server.protocol.gameplay_protocol import StatusEffectEntry
from dso_server.protocol.player_init_protocol import (
    EventParticipation,
    EventParticipationInfo,
    EventProgressInfo,
    EventProgressInfoEntry,
    EventUpdate,
    EventUpdateEntry,
)
from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
from dso_server.paths import DEFAULT_EVENT_ITEM_REGIONS_PATH, DEFAULT_EVENT_LOOT_PATH
from dso_server.runtime.file_revisions import observed_revision, resolved_path


UINT32_MAX = 0xFFFFFFFF
_CURRENCY_COLLECTIBLE_CATEGORIES = frozenset({"Currency", "NoAutoUseCurrency"})
_DIFFICULTY_SUFFIX = re.compile(r"_diff_\d+$", re.IGNORECASE)
_DROP_AMOUNT = re.compile(
    r"^DropAmount:([^,]+),(absolute|relative),([^#]+)", re.IGNORECASE
)


def _tokens(value: object) -> tuple[str, ...]:
    return tuple(
        token.strip()
        for token in str(value or "").replace(",", ";").split(";")
        if token.strip()
    )


def _map_key(value: object) -> str:
    return _DIFFICULTY_SUFFIX.sub("", str(value or "").strip()).casefold()


def _timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _wire_time(value: datetime | None) -> tuple[int, int, int, int, int, int]:
    if value is None:
        return (0, 0, 0, 0, 0, 0)
    value = value.astimezone(timezone.utc)
    return (
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
    )


@dataclass(frozen=True)
class EventReward:
    key: str
    tier: int
    threshold: int
    item_options: Mapping[str, str]
    amount: int
    currency: bool = False

    def item_for_class(self, class_name: str) -> str | None:
        if self.currency:
            return None
        folded = str(class_name).casefold()
        return self.item_options.get(folded) or self.item_options.get("")


@dataclass(frozen=True)
class EventCollectible:
    item_id: str
    item_category: str
    auto_pick_up: str
    base_value: int
    # _Template_Item.Region: loot regions, matched against the map's
    # _Template_MapTemplate.Region and the monster's _Template_Monster.Region
    # (plus map ids for a few items whose region names a map).
    maps: frozenset[str]
    # _Template_Item.EventId: the event family the item belongs to.
    owner_events: frozenset[str] = frozenset()
    # Paged bars (FixedBarCollectible, or one ``0/``, ``1/``... entry per
    # page): the page this item is collected on; None on ordinary bars.
    tier: int | None = None


@dataclass(frozen=True)
class EventProgressBarDefinition:
    resource_id: str
    minimum_level: int
    maximum_level: int
    priority: int
    collectibles: tuple[EventCollectible, ...]
    item_rewards: tuple[EventReward, ...]
    currency_rewards: tuple[EventReward, ...]
    # Bars that advance through leaderboard placement or level-ups list a
    # collectible only as their progress unit; monsters never drop it.
    monster_drops: bool = True

    def accepts(self, player_level: int) -> bool:
        return self.minimum_level <= player_level <= self.maximum_level

    def current_tier(self, amount: int) -> int:
        """The bar page a player with ``amount`` progress is collecting on.

        Reward entries are ``page/threshold``; the page of the next reward
        still ahead is the current one, the last page once all are reached.
        """

        rewards = (*self.item_rewards, *self.currency_rewards)
        ahead = [reward.tier for reward in rewards if reward.threshold > amount]
        if ahead:
            return min(ahead)
        return max((reward.tier for reward in rewards), default=0)

    def droppable_collectibles(
        self, progress: Mapping[str, object] | None,
    ) -> tuple[EventCollectible, ...]:
        """The collectibles a kill can drop for a player with ``progress``.

        A FixedBarCollectible bar collects one item per page: the retail
        Dragan event drops the Rise of Dragan key until its first page is
        done, then progress items, then boss progress (2026-10-28 capture).
        """

        # Happy-hour bars count Andermant (money_rc): progress from spending
        # it, never premium currency raining from monsters.
        droppable = tuple(
            value for value in self.collectibles
            if value.item_category not in _CURRENCY_COLLECTIBLE_CATEGORIES
        )
        tiered = [value for value in droppable if value.tier is not None]
        if not tiered:
            return droppable
        tier = self.current_tier(max(0, int((progress or {}).get("amount", 0))))
        reached = [value for value in tiered if value.tier <= tier]
        if not reached:
            return ()
        page = max(value.tier for value in reached)
        return tuple(value for value in reached if value.tier == page)


@dataclass(frozen=True)
class EventTemplateDefinition:
    resource_id: str
    client_name: str
    client_only: bool
    map_local: bool
    minimum_level: int
    maximum_level: int
    map_id: str
    excluded_maps: frozenset[str]
    teleport_target: str
    status_effects: tuple[str, ...]
    progress_bars: tuple[EventProgressBarDefinition, ...]
    category: str = ""
    # _Template_Events.Loot: the list every worthy opponent rolls while the
    # event runs (christmas sugar beets). Contents: data/catalogs/event_loot.json.
    loot_list_id: str = ""

    def progress_bar(self, player_level: int) -> EventProgressBarDefinition | None:
        eligible = tuple(
            bar for bar in self.progress_bars if bar.accepts(player_level)
        )
        if not eligible:
            return None
        return max(eligible, key=lambda bar: (bar.priority, bar.minimum_level))


@dataclass(frozen=True)
class ScheduledEvent:
    event_id: int
    template: EventTemplateDefinition
    starts_at: datetime | None
    ends_at: datetime | None
    forced_state: str
    tags: tuple[str, ...]
    participation_default: bool
    collectible_drop_chance: float
    collectible_amount: int
    collectible_maps: frozenset[str]
    collectible_ranks: frozenset[str]
    # Maps the event owns, derived from static data (see _event_owned_maps);
    # ``collectible_maps`` is the explicit schedule override.
    progress_maps: frozenset[str] = frozenset()

    def state_at(self, now: datetime) -> str:
        if self.forced_state in {"active", "upcoming", "expired"}:
            return self.forced_state
        if self.starts_at is not None and now < self.starts_at:
            return "upcoming"
        if self.ends_at is not None and now >= self.ends_at:
            return "expired"
        return "active"

    def active_at(self, now: datetime) -> bool:
        return self.state_at(now) == "active"


@dataclass(frozen=True)
class TravelItemDefinition:
    template_id: str
    target_map_id: str
    required_level: int
    event_ids: frozenset[str]
    only_during_event: bool


@dataclass(frozen=True)
class EventProgressAdvance:
    event: ScheduledEvent
    progress: dict[str, object]
    item_rewards: tuple[EventReward, ...]
    currency_rewards: tuple[EventReward, ...]


@dataclass(frozen=True)
class EventLootEntry:
    """One item of an event loot list, by monster rank class.

    Rank classes are ``monster``, ``elite`` (guardian, champion, leader) and
    ``boss`` (boss, worldboss); a class without its own values uses
    ``monster``'s. The chance is further scaled by the monster's
    ``_Template_Monster.EventDropChance``, unless the entry names
    ``regions``: then it drops from every monster on maps of those regions
    (or monsters of them), the event-map loot like christmas presents.
    """

    item_id: str
    chances: Mapping[str, float]
    amounts: Mapping[str, tuple[int, int]]
    # _Template_Item.MinDropLevel/MaxDropLevel, against the monster's level.
    minimum_monster_level: int = 1
    maximum_monster_level: int = 0
    regions: frozenset[str] = frozenset()
    # Per map difficulty (``..._Diff_{difficulty:02d}``); the last one serves
    # higher difficulties. Empty: always item_id.
    difficulty_items: tuple[str, ...] = ()

    def item_for_difficulty(self, difficulty: int) -> str:
        if not self.difficulty_items:
            return self.item_id
        return self.difficulty_items[
            max(0, min(int(difficulty), len(self.difficulty_items) - 1))
        ]


def _rank_class(rank_kind: str) -> str:
    rank = str(rank_kind).casefold()
    if rank in {"boss", "worldboss"}:
        return "boss"
    if rank in {"guardian", "champion", "leader"}:
        return "elite"
    return "monster"


@dataclass(frozen=True)
class DropAmountModifier:
    effect_id: int
    target_item_id: str
    value: str
    mode: str
    max_stack_size: int


@dataclass(frozen=True)
class EventCatalog:
    events: tuple[ScheduledEvent, ...]
    travel_items: Mapping[str, TravelItemDefinition]
    event_map_requirements: Mapping[str, frozenset[str]]
    drop_modifiers: Mapping[int, tuple[DropAmountModifier, ...]]
    # Casefolded template id -> maps it owns (also for unscheduled templates,
    # so a challenge event can fall back to its parent event's maps).
    event_maps: Mapping[str, frozenset[str]] = MappingProxyType({})
    # Map key (map template, access group or access id) -> its Region tokens.
    map_regions: Mapping[str, frozenset[str]] = MappingProxyType({})
    # Casefolded monster template id -> its Region tokens.
    monster_regions: Mapping[str, frozenset[str]] = MappingProxyType({})
    # Loot list id -> entries; casefolded monster id -> EventDropChance.
    event_loot: Mapping[str, tuple[EventLootEntry, ...]] = MappingProxyType({})
    monster_event_drop_chances: Mapping[str, float] = MappingProxyType({})

    def active_events(self, now: datetime | None = None) -> tuple[ScheduledEvent, ...]:
        moment = now or datetime.now(timezone.utc)
        return tuple(event for event in self.events if event.active_at(moment))

    def active_template_ids(self, now: datetime | None = None) -> frozenset[str]:
        return frozenset(
            event.template.resource_id.casefold()
            for event in self.active_events(now)
        )

    def active_event_flags(self, now: datetime | None = None) -> frozenset[str]:
        """Names understood by map EventSets and other authored gates."""

        return frozenset(
            value.casefold()
            for event in self.active_events(now)
            for value in (event.template.resource_id, *event.tags)
        )

    def event_update(self, now: datetime | None = None) -> EventUpdate:
        moment = now or datetime.now(timezone.utc)
        entries = []
        for event in self.events:
            state = event.state_at(moment)
            if state == "expired":
                continue
            entries.append(EventUpdateEntry(
                event_id=event.event_id,
                event_name=event.template.resource_id,
                flags=(state == "active", event.template.map_local, False, False),
                starts_at=_wire_time(event.starts_at),
                ends_at=_wire_time(event.ends_at),
                tags=event.tags,
            ))
        return EventUpdate(tuple(entries))

    def progress_info(
        self,
        progress: Mapping[str, Mapping[str, object]],
        *,
        player_level: int,
        now: datetime | None = None,
    ) -> EventProgressInfo:
        result = []
        for event in self.active_events(now):
            if event.template.progress_bar(player_level) is None:
                continue
            state = progress.get(str(event.event_id), {})
            result.append(EventProgressInfoEntry(
                event.event_id,
                (
                    max(0, int(state.get("amount", 0))),
                    int(state.get("level", UINT32_MAX)) & UINT32_MAX,
                    int(state.get("currency_level", UINT32_MAX)) & UINT32_MAX,
                    max(0, int(state.get("group", 0))),
                ),
            ))
        return EventProgressInfo(tuple(result))

    def participation_info(
        self,
        participation: Mapping[str, bool],
        *,
        now: datetime | None = None,
    ) -> EventParticipationInfo:
        values = tuple(
            EventParticipation(
                event.event_id,
                bool(participation.get(
                    str(event.event_id), event.participation_default
                )),
            )
            for event in self.active_events(now)
            if not event.participation_default
            or str(event.event_id) in participation
        )
        return EventParticipationInfo(values)

    def travel_item(self, template_id: str) -> TravelItemDefinition | None:
        return self.travel_items.get(str(template_id))

    def travel_item_available(
        self,
        definition: TravelItemDefinition,
        *,
        player_level: int,
        now: datetime | None = None,
    ) -> bool:
        if player_level < definition.required_level:
            return False
        if not definition.event_ids:
            return True
        active = self.active_template_ids(now)
        return bool(definition.event_ids & active)

    def map_available(self, map_id: str, now: datetime | None = None) -> bool:
        requirements = self.event_map_requirements.get(_map_key(map_id))
        if not requirements:
            return True
        return bool(requirements & self.active_template_ids(now))

    def event_for_collectible(
        self, item_id: str, *, now: datetime | None = None
    ) -> tuple[ScheduledEvent, EventProgressBarDefinition] | None:
        for event in self.active_events(now):
            for bar in event.template.progress_bars:
                if any(value.item_id == item_id for value in bar.collectibles):
                    return event, bar
        return None

    def is_event_collectible(self, item_id: str) -> bool:
        """Return true even when the owning event is not currently active."""

        return any(
            value.item_id == str(item_id)
            for event in self.events
            for bar in event.template.progress_bars
            for value in bar.collectibles
        )

    def collectible_drops(
        self,
        *,
        map_id: str,
        player_level: int,
        monster_rank: str,
        participation: Mapping[str, bool],
        status_effects: Iterable[StatusEffectEntry],
        rng: random.Random,
        now: datetime | None = None,
        map_aliases: Iterable[str] = (),
        monster_template_id: str = "",
        progress: Mapping[str, Mapping[str, object]] = MappingProxyType({}),
    ) -> tuple[tuple[str, int], ...]:
        rank = str(monster_rank).casefold()
        # The public catalog is also used by tooling and protocol tests. Be
        # defensive about the runtime store's ``(entry, expires_at)`` shape so
        # malformed optional modifier state can never suppress base event
        # progress drops or the rest of a monster's loot transaction.
        active_effects = tuple(
            entry
            for value in status_effects
            for entry in (
                value
                if hasattr(value, "effect_id")
                else (
                    value[0]
                    if isinstance(value, tuple)
                    and value
                    and hasattr(value[0], "effect_id")
                    else None
                ),
            )
            if entry is not None
        )
        result = []
        for event, collectible in self._collectibles_here(
            map_id=map_id,
            map_aliases=map_aliases,
            player_level=player_level,
            participation=participation,
            progress=progress,
            now=now,
            monster_template_id=monster_template_id,
        ):
            if event.collectible_ranks and rank not in event.collectible_ranks:
                continue
            # An event map's final boss (Big Paws, Dragan, Wendigo...) always
            # pays out; generate_monster_loot sizes its stack.
            if rank != "worldboss" and rng.random() > event.collectible_drop_chance:
                continue
            base = max(
                1,
                int(event.collectible_amount),
                int(collectible.base_value),
            )
            amount = self._modified_drop_amount(
                collectible.item_id, base, active_effects
            )
            result.append((collectible.item_id, amount))
        return tuple(result)

    def map_event_collectibles(
        self,
        *,
        map_id: str,
        player_level: int,
        participation: Mapping[str, bool],
        progress: Mapping[str, Mapping[str, object]] = MappingProxyType({}),
        now: datetime | None = None,
        map_aliases: Iterable[str] = (),
    ) -> dict[str, tuple[str, ...]]:
        """Events whose progress applies on this map, with what a chest drops.

        Keys are casefolded event template ids, values the collectibles this
        player is collecting there (a paged bar's current page). Map chests
        (amphorae) drop these outright: unlike kills, no per-event drop
        chance or monster rank applies.
        """

        result: dict[str, list[str]] = {}
        for event, collectible in self._collectibles_here(
            map_id=map_id,
            map_aliases=map_aliases,
            player_level=player_level,
            participation=participation,
            progress=progress,
            now=now,
        ):
            items = result.setdefault(event.template.resource_id.casefold(), [])
            if collectible.item_id not in items:
                items.append(collectible.item_id)
        return {key: tuple(value) for key, value in result.items()}

    def _collectibles_here(
        self,
        *,
        map_id: str,
        map_aliases: Iterable[str],
        player_level: int,
        participation: Mapping[str, bool],
        progress: Mapping[str, Mapping[str, object]],
        now: datetime | None,
        monster_template_id: str = "",
    ) -> Iterator[tuple[ScheduledEvent, EventCollectible]]:
        """Progress collectibles a drop on this map advances, per event."""

        # An instance is known by its physical map, access group and access
        # id; any of them can be the name static data uses for it.
        current_maps = frozenset(
            key
            for key in (_map_key(map_id), *(_map_key(v) for v in map_aliases))
            if key
        )
        loot_scopes = current_maps.union(
            *(self.map_regions.get(key, ()) for key in current_maps),
            self.monster_regions.get(
                str(monster_template_id).casefold(), ()
            ),
            (_map_key(monster_template_id),),
        )
        for event in self.active_events(now):
            template = event.template
            if not bool(participation.get(
                str(event.event_id), event.participation_default
            )):
                continue
            if not template.minimum_level <= player_level <= template.maximum_level:
                continue
            if current_maps & template.excluded_maps:
                continue
            if template.map_id and _map_key(template.map_id) not in current_maps:
                continue
            bar = template.progress_bar(player_level)
            if bar is None or not bar.monster_drops:
                continue
            for collectible in bar.droppable_collectibles(
                progress.get(str(event.event_id))
            ):
                if self._collectible_in_scope(
                    event, collectible, current_maps, loot_scopes
                ):
                    yield event, collectible

    def _collectible_in_scope(
        self,
        event: ScheduledEvent,
        collectible: EventCollectible,
        current_maps: frozenset[str],
        loot_scopes: frozenset[str],
    ) -> bool:
        """Whether a kill here may drop this event's progress collectible.

        Progress belongs to the event's own content (the Winter Summit for
        christmas honey cookies), while the "worthy opponents everywhere"
        drops are the event's Loot list. In order of authority:
        an explicit schedule ``collectible_maps``; the item's Region, or the
        one data/catalogs/event_item_regions.json authors for it, matched
        against the map, its regions, the monster and the monster's regions
        (or the event's own maps, which covers first-stage maps an item
        Region omits); the maps the event owns; the maps of the event family
        named by the item's EventId (challenge events reuse their parent's
        collectible). With none of those, only an event naming a Loot list
        hunts worthy opponents everywhere (Monster Hunt); any other event's
        progress does not come from kills (Live Stream's bonus codes).
        """

        if event.collectible_maps:
            return bool(event.collectible_maps & current_maps)
        own = event.progress_maps
        if collectible.maps:
            return bool(collectible.maps & loot_scopes or own & current_maps)
        if own:
            return bool(own & current_maps)
        family = frozenset().union(*(
            self.event_maps.get(owner, ())
            for owner in collectible.owner_events
        ))
        if family:
            return bool(family & current_maps)
        return bool(event.template.loot_list_id)

    def event_loot_drops(
        self,
        *,
        map_id: str,
        player_level: int,
        monster_template_id: str,
        monster_rank: str,
        monster_level: int,
        participation: Mapping[str, bool],
        rng: random.Random,
        now: datetime | None = None,
        map_aliases: Iterable[str] = (),
        map_difficulty: int = 0,
    ) -> tuple[tuple[str, int], ...]:
        """Roll the Loot lists of the active events for one kill.

        These are the "collect X from worthy opponents" drops (christmas
        sugar beets): any map the event does not exclude, from monsters whose
        authored EventDropChance is above zero, so event-dungeon monsters,
        adds and summons drop none. Entries with ``regions`` are the event
        maps' own loot instead (christmas presents on the e_chr maps).
        """

        worthiness = self.monster_event_drop_chances.get(
            str(monster_template_id).casefold(), 0.0
        )
        current_maps = frozenset(
            key
            for key in (_map_key(map_id), *(_map_key(v) for v in map_aliases))
            if key
        )
        loot_scopes = current_maps.union(
            *(self.map_regions.get(key, ()) for key in current_maps),
            self.monster_regions.get(str(monster_template_id).casefold(), ()),
        )
        rank = _rank_class(monster_rank)
        result = []
        for event in self.active_events(now):
            template = event.template
            entries = self.event_loot.get(template.loot_list_id.casefold(), ())
            if not entries:
                continue
            if not bool(participation.get(
                str(event.event_id), event.participation_default
            )):
                continue
            if not template.minimum_level <= player_level <= template.maximum_level:
                continue
            if current_maps & template.excluded_maps:
                continue
            if template.map_id and _map_key(template.map_id) not in current_maps:
                continue
            for entry in entries:
                if monster_level < entry.minimum_monster_level or (
                    entry.maximum_monster_level
                    and monster_level > entry.maximum_monster_level
                ):
                    continue
                if entry.regions:
                    if not entry.regions & loot_scopes:
                        continue
                    scale = 1.0
                elif worthiness <= 0.0:
                    continue
                else:
                    scale = worthiness
                chance = entry.chances.get(rank, entry.chances.get("monster", 0.0))
                if rng.random() >= chance * scale:
                    continue
                low, high = entry.amounts.get(
                    rank, entry.amounts.get("monster", (1, 1))
                )
                result.append((
                    entry.item_for_difficulty(map_difficulty),
                    rng.randint(low, high),
                ))
        return tuple(result)

    def _modified_drop_amount(
        self,
        item_id: str,
        base: int,
        status_effects: tuple[StatusEffectEntry, ...],
    ) -> int:
        absolute = 0.0
        relative = 0.0
        for effect in status_effects:
            for modifier in self.drop_modifiers.get(int(effect.effect_id), ()):
                if modifier.target_item_id != item_id:
                    continue
                stack_count = min(
                    max(1, int(getattr(effect, 'field_18', 0)) + 1),
                    max(1, int(modifier.max_stack_size)),
                )
                raw = modifier.value.strip()
                if raw.startswith("$") and raw[1:].isdigit():
                    index = int(raw[1:])
                    value = (
                        float(effect.values[index])
                        if index < len(effect.values) else 0.0
                    )
                else:
                    try:
                        value = float(raw)
                    except ValueError:
                        continue
                if modifier.mode == "relative":
                    relative += value * stack_count
                else:
                    absolute += value * stack_count
        return max(1, int(math.floor(base * (1.0 + relative) + absolute + 0.5)))

    def advance(
        self,
        progress: Mapping[str, Mapping[str, object]],
        *,
        collectible_id: str,
        amount: int,
        player_level: int,
        class_name: str,
        now: datetime | None = None,
    ) -> EventProgressAdvance | None:
        found = self.event_for_collectible(collectible_id, now=now)
        if found is None:
            return None
        event, _any_bar = found
        bar = event.template.progress_bar(player_level)
        if bar is None or not any(
            value.item_id == collectible_id for value in bar.collectibles
        ):
            return None
        key = str(event.event_id)
        old = dict(progress.get(key, {}))
        previous = max(0, int(old.get("amount", 0)))
        current = min(UINT32_MAX, previous + max(0, int(amount)))
        claimed = {str(value) for value in old.get("claimed", ())}

        def crossed(reward: EventReward) -> bool:
            return previous < reward.threshold <= current and reward.key not in claimed

        item_rewards = tuple(
            reward for reward in bar.item_rewards
            if crossed(reward) and reward.item_for_class(class_name)
        )
        currency_rewards = tuple(
            reward for reward in bar.currency_rewards if crossed(reward)
        )
        claimed.update(reward.key for reward in item_rewards)
        claimed.update(reward.key for reward in currency_rewards)

        reached_item_tiers = [
            reward.tier for reward in bar.item_rewards
            if reward.threshold <= current
        ]
        reached_currency_tiers = [
            reward.tier for reward in bar.currency_rewards
            if reward.threshold <= current
        ]
        state = {
            "template_id": event.template.resource_id,
            "amount": current,
            # Retail uses UINT_MAX for no progress and one-based item pages.
            "level": (
                max(reached_item_tiers) + 1
                if reached_item_tiers else UINT32_MAX
            ),
            "currency_level": (
                max(reached_currency_tiers)
                if reached_currency_tiers else UINT32_MAX
            ),
            "group": 0,
            "claimed": sorted(claimed),
        }
        return EventProgressAdvance(event, state, item_rewards, currency_rewards)


def _parse_reward_entries(value: object, *, currency: bool) -> tuple[EventReward, ...]:
    rewards = []
    # Item rewards use commas inside a single class-conditioned choice
    # (``Warrior|foo,Ranger|bar``), while semicolons separate reward tiers.
    # Currency rewards have no class choices and occur with either delimiter
    # in different generations of the static data.
    raw_entries = (
        re.split(r"[;,]", str(value or ""))
        if currency else str(value or "").split(";")
    )
    for index, raw in enumerate(entry.strip() for entry in raw_entries if entry.strip()):
        parts = raw.split(":")
        try:
            if currency:
                if len(parts) != 2:
                    continue
                tier_threshold, amount_text = parts
                item_text = ""
            else:
                if len(parts) < 3:
                    continue
                tier_threshold, item_text, amount_text = parts[0], parts[1], parts[2]
            if "/" in tier_threshold:
                tier_text, threshold_text = tier_threshold.split("/", 1)
                tier = int(tier_text)
            else:
                tier = 0
                threshold_text = tier_threshold
            threshold = max(0, int(threshold_text))
            amount = max(1, int(amount_text.rstrip(",")))
        except ValueError:
            continue
        options: dict[str, str] = {}
        if not currency:
            for option in item_text.split(","):
                option = option.strip()
                if not option:
                    continue
                if "|" in option:
                    class_name, item_id = option.split("|", 1)
                    options[class_name.casefold()] = item_id
                else:
                    options[""] = option
        rewards.append(EventReward(
            key=f"{'currency' if currency else 'item'}:{index}:{threshold}",
            tier=tier,
            threshold=threshold,
            item_options=MappingProxyType(options),
            amount=amount,
            currency=currency,
        ))
    return tuple(sorted(rewards, key=lambda reward: reward.threshold))


def _event_owned_maps(
    templates: Mapping[str, EventTemplateDefinition],
    item_rows: Mapping[str, Mapping[str, object]],
    map_rows: Iterable[Mapping[str, object]],
    access_rows: Iterable[Mapping[str, object]],
    travel_maps: Mapping[str, set[str]],
    scripted_maps: Mapping[str, set[str]] | None = None,
    exit_maps: Mapping[str, frozenset[str]] | None = None,
) -> dict[str, frozenset[str]]:
    """Map keys each event template owns, from static data alone.

    Seeds are the template's Map, its TravelItem targets, access groups that
    require the event, and access groups entered with a key item of the
    event (the Winter Summit takes christmas sugar beets). Event-type maps
    sharing a Region with a seed join it: the christmas maps are all region
    ``e_chr``, so its calendar and Kranparus dungeons count too.

    An event with none of those falls back to ``scripted_maps``, the maps
    whose ``<map>_<event>.eve`` script runs for it: Helios trademarks belong
    to its PvP arenas. Only then, as most seasonal events script every hub.
    """

    known = {resource_id.casefold() for resource_id in templates}
    seeds: dict[str, set[str]] = {
        event_id: set(maps) for event_id, maps in travel_maps.items()
    }
    for resource_id, template in templates.items():
        if template.map_id:
            seeds.setdefault(resource_id.casefold(), set()).add(
                _map_key(template.map_id)
            )

    group_maps: dict[str, set[str]] = {}
    event_maps_by_region: dict[str, set[str]] = {}
    map_info: dict[str, list[tuple[str, frozenset[str]]]] = {}
    for row in map_rows:
        key = _map_key(row.get("MapTemplate"))
        group = _map_key(row.get("AccessGroup"))
        map_type = str(row.get("MapType") or "")
        regions = frozenset(value.casefold() for value in _tokens(row.get("Region")))
        if group:
            group_maps.setdefault(group, set()).update((key, group))
        map_info.setdefault(key, []).append((map_type, regions))
    for key, variants in map_info.items():
        # Regional variants give a shared arena (the anniversary arena has
        # one row per themed region) a row in many families; only a map of
        # a single region belongs to that region's family.
        regions = frozenset().union(*(
            regions for map_type, regions in variants if map_type == "Event"
        ))
        if len(regions) == 1:
            event_maps_by_region.setdefault(next(iter(regions)), set()).add(key)
    for row in access_rows:
        group = _map_key(row.get("AccessGroup"))
        if group:
            group_maps.setdefault(group, {group}).add(_map_key(row.get("Id")))

    for row in access_rows:
        group = _map_key(row.get("AccessGroup"))
        owners = {
            value.casefold()
            for value in (
                *_tokens(row.get("RequiredActiveEvent")),
                *_tokens(row.get("EventId")),
            )
        }
        for key_entry in (
            *_tokens(row.get("KeyId")), *_tokens(row.get("TeleportKeyId")),
        ):
            item = item_rows.get(key_entry.split(":", 1)[0].strip())
            if item is not None:
                owners.update(
                    value.casefold() for value in _tokens(item.get("EventId"))
                )
        for owner in owners & known:
            seeds.setdefault(owner, set()).update(
                group_maps.get(group, ()), (_map_key(row.get("Id")),)
            )
    # Region-scoped collectibles (the sewer blitz worm's progress) already
    # say where they drop; a script map would widen them to every monster.
    unscoped = {
        resource_id.casefold()
        for resource_id, template in templates.items()
        if any(
            not collectible.maps
            for bar in template.progress_bars
            for collectible in bar.collectibles
        )
    }
    exits = {
        event_id: maps for event_id, maps in (exit_maps or {}).items()
        if event_id in known
    }
    for event_id, maps in (scripted_maps or {}).items():
        if (
            event_id in unscoped
            and not seeds.get(event_id)
            and not exits.get(event_id)
        ):
            seeds[event_id] = set(maps)

    owned: dict[str, frozenset[str]] = {}
    for event_id, maps in seeds.items():
        expanded = set(maps)
        for key in maps:
            expanded.update(group_maps.get(key, ()))
            for map_type, regions in map_info.get(key, ()):
                if map_type != "Event":
                    continue
                for region in regions:
                    expanded.update(event_maps_by_region.get(region, ()))
        expanded.discard("")
        if expanded:
            owned[event_id] = frozenset(expanded)
    # The maps an event's own exits lead to count exactly: no region
    # widening, or the Frozen Field (region e_chr) would take in the Winter
    # Summit that hosts its entrance.
    for event_id, maps in exits.items():
        exact = set(maps)
        for key in maps:
            exact.update(group_maps.get(key, ()))
        exact.discard("")
        if exact:
            owned[event_id] = owned.get(event_id, frozenset()) | frozenset(exact)
    return owned


@lru_cache(maxsize=4)
def _event_exit_destinations(
    database_path: str, database_mtime_ns: int, database_size: int,
) -> dict[str, frozenset[str]]:
    """Event id -> maps behind exits that only an ``<event>=True`` EventSet opens.

    A sub-event names its own map this way: the Winter Summit's exits to the
    Frozen Field stand behind ``e_chr_2015_challenge_04=True``, and those to
    Kranparus' lairs behind ``e_chr_2015_challenge_01..03=True``. The static
    exit list carries each exit's map and EventSet row; the flag text lives in
    that map's own EventSet table. Cached per static.db4 revision because it
    opens every map with gated exits.
    """

    del database_mtime_ns, database_size
    db_path = Path(database_path)
    maps_directory = db_path.parent.parent / "maps"
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as database:
        known = {
            str(row[0]).casefold()
            for row in database.execute("SELECT Id FROM _Template_Events")
        }
        if not compat.has_columns(
            database, "_Template_ExitList", "_Level", "EventSetRow", "ExitURL",
        ):
            return {}
        gated: dict[str, list[tuple[int, str]]] = {}
        for level, event_set, url in database.execute(
            "SELECT _Level, EventSetRow, ExitURL FROM _Template_ExitList "
            "WHERE EventSetRow IS NOT NULL AND EventSetRow != -1 "
            "AND COALESCE(ExitURL, '') != ''"
        ):
            gated.setdefault(str(level), []).append((int(event_set), str(url)))
    result: dict[str, set[str]] = {}
    for level, exits in gated.items():
        path = maps_directory / f"{level}.db4"
        if not path.is_file():
            continue
        wanted = sorted({str(event_set) for event_set, _url in exits})
        try:
            with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as map_database:
                flags = dict(map_database.execute(
                    "SELECT Id, EventFlags FROM _Instance_EventSets WHERE Id IN ({})".format(
                        ",".join("?" * len(wanted))
                    ),
                    wanted,
                ))
        except sqlite3.Error:
            continue
        for event_set, url in exits:
            for token in re.split(r"[;,]", str(flags.get(str(event_set)) or "")):
                name, _separator, value = token.partition("=")
                name = name.strip().casefold()
                if name in known and value.strip().casefold() == "true":
                    result.setdefault(name, set()).add(_map_key(url))
    return {event_id: frozenset(maps) for event_id, maps in result.items()}


def _event_scripted_maps(
    maps_directory: Path, templates: Mapping[str, EventTemplateDefinition],
) -> dict[str, set[str]]:
    """Event id -> maps shipping a ``<map>_<event>.eve`` script for it."""

    known = {resource_id.casefold() for resource_id in templates}
    result: dict[str, set[str]] = {}
    try:
        names = [path.stem for path in maps_directory.glob("*.eve")]
    except OSError:
        return result
    for stem in names:
        folded = stem.casefold()
        for index, character in enumerate(folded):
            if character == "_" and folded[index + 1:] in known:
                result.setdefault(folded[index + 1:], set()).add(
                    _map_key(folded[:index])
                )
                break
    return result


def _event_loot_lists(
    path: str, item_rows: Mapping[str, Mapping[str, object]],
) -> dict[str, tuple[EventLootEntry, ...]]:
    """Parse data/catalogs/event_loot.json (see docs there).

    The retail server owned these loot lists and the client data only names
    them, so their contents are authored here. A malformed file fails the
    catalog load like a malformed schedule does.
    """

    if not path:
        return {}
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    lists = document.get("loot_lists") if isinstance(document, dict) else None
    if not isinstance(lists, dict):
        raise ValueError("event loot must contain a loot_lists object")
    result: dict[str, tuple[EventLootEntry, ...]] = {}
    for list_id, entries in lists.items():
        if not isinstance(entries, list):
            raise ValueError(f"event loot list {list_id!r} must be an array")
        parsed = []
        for entry in entries:
            item_id = str(entry.get("item") or "")
            # ``{difficulty:02d}`` picks the map difficulty's variant
            # (e_chr_progress_box_rare_Diff_03); the variants run from 0 up
            # to the last one the database has.
            difficulty_items = []
            if "{difficulty" in item_id:
                for difficulty in range(0, 100):
                    variant = item_id.format(difficulty=difficulty)
                    if variant not in item_rows:
                        break
                    difficulty_items.append(variant)
                if not difficulty_items:
                    raise ValueError(f"event loot list {list_id!r} names unknown item {item_id!r}")
                item_id = difficulty_items[0]
            item = item_rows.get(item_id)
            if item is None:
                raise ValueError(f"event loot list {list_id!r} names unknown item {item_id!r}")
            chances = {
                str(rank): max(0.0, min(1.0, float(value)))
                for rank, value in dict(entry.get("chance") or {}).items()
            }
            amounts = {}
            for rank, value in dict(entry.get("amount") or {}).items():
                low, high = (int(value[0]), int(value[-1])) if isinstance(
                    value, list) else (int(value), int(value))
                if not 1 <= low <= high:
                    raise ValueError(f"event loot amount for {item_id!r} must be 1 <= min <= max")
                amounts[str(rank)] = (low, high)
            if not chances:
                raise ValueError(f"event loot entry {item_id!r} needs a chance")
            parsed.append(EventLootEntry(
                item_id=item_id,
                chances=MappingProxyType(chances),
                amounts=MappingProxyType(amounts),
                minimum_monster_level=max(1, int(item.get("MinDropLevel") or 1)),
                maximum_monster_level=max(0, int(item.get("MaxDropLevel") or 0)),
                regions=frozenset(
                    str(value).casefold() for value in entry.get("regions") or ()
                ),
                difficulty_items=tuple(difficulty_items),
            ))
        result[str(list_id).casefold()] = tuple(parsed)
    return result


def _authored_item_regions() -> dict[str, frozenset[str]]:
    """Parse data/catalogs/event_item_regions.json (see the note there).

    The retail server knew where these items drop; the client leaves their
    Region empty and no table ties their event to a place. Optional, like
    the loot table.
    """

    try:
        document = json.loads(
            DEFAULT_EVENT_ITEM_REGIONS_PATH.read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return {}
    return {
        str(item_id): frozenset(_map_key(value) for value in regions)
        for item_id, regions in document["item_regions"].items()
    }


def _schedule_revision(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


@lru_cache(maxsize=4)
def _load_catalog(
    database_path: str,
    database_mtime_ns: int,
    database_size: int,
    schedule_path: str,
    schedule_mtime_ns: int,
    schedule_size: int,
    loot_path: str = "",
    loot_mtime_ns: int = 0,
    loot_size: int = 0,
) -> EventCatalog:
    del database_mtime_ns, database_size, schedule_mtime_ns, schedule_size
    del loot_mtime_ns, loot_size
    db_path = Path(database_path)
    config = json.loads(resident_content.read_text(schedule_path, encoding="utf-8"))
    with sqlite3.connect(
        f"file:{db_path.as_posix()}?mode=ro", uri=True
    ) as database:
        database.row_factory = sqlite3.Row
        item_rows = {
            str(row["Id"]): dict(row)
            for row in database.execute("SELECT * FROM _Template_Item")
        }
        map_rows = tuple(
            dict(row)
            for row in database.execute(compat.map_template_query(
                database, ("MapTemplate", "AccessGroup", "MapType", "Region"),
            ))
        )
        access_rows = ()
        if compat.has_columns(
            database, "_Template_MapAccessGroup", "Id", "AccessGroup"
        ):
            access_columns = compat.columns(
                database, "_Template_MapAccessGroup"
            )
            access_rows = tuple(
                dict(row)
                for row in database.execute(
                    "SELECT {} FROM _Template_MapAccessGroup".format(", ".join(
                        name if name in access_columns else f"NULL AS {name}"
                        for name in (
                            "Id", "AccessGroup", "RequiredActiveEvent",
                            "EventId", "KeyId", "TeleportKeyId",
                        )
                    ))
                )
            )
        monster_region_rows = ()
        if compat.has_columns(database, "_Template_Monster", "Region"):
            monster_region_rows = tuple(database.execute(
                "SELECT Id, Region FROM _Template_Monster "
                "WHERE Region IS NOT NULL AND Region != ''"
            ))
        monster_event_drop_rows = ()
        if compat.has_columns(database, "_Template_Monster", "EventDropChance"):
            monster_event_drop_rows = tuple(database.execute(
                "SELECT Id, EventDropChance FROM _Template_Monster "
                "WHERE EventDropChance > 0"
            ))
        # Event progress bars postdate the legacy client, which has no table for
        # them; an empty catalogue reads as "this build shows no progress bars".
        progress_rows = {}
        if compat.table_exists(database, "_Template_EventProgressBar"):
            progress_rows = {
                str(row["Id"]): dict(row)
                for row in database.execute(
                    "SELECT * FROM _Template_EventProgressBar"
                )
            }
        event_rows = {
            str(row["Id"]): dict(row)
            for row in database.execute("SELECT * FROM _Template_Events")
        }
        status_rows = tuple(database.execute(
            "SELECT rowid - 1, Id, StartModifiers FROM _Template_StatusEffect"
        ))
        status_stack_limits = {
            int(row[0]): max(1, int(row[1] or 1))
            for row in database.execute(
                'SELECT rowid - 1, COALESCE(MaxStackSize, 1) '
                'FROM _Template_StatusEffect'
            )
        }

    item_regions = _authored_item_regions()
    templates: dict[str, EventTemplateDefinition] = {}
    for resource_id, row in event_rows.items():
        bars = []
        for bar_id in _tokens(row.get("EventProgressBars")):
            bar_row = progress_rows.get(bar_id)
            if bar_row is None:
                continue
            collectibles = []
            entries = []
            for encoded in _tokens(bar_row.get("EventCollectibles")):
                encoded_parts = encoded.split("/", 1)
                try:
                    prefix = (
                        int(encoded_parts[0]) if len(encoded_parts) == 2 else None
                    )
                except ValueError:
                    prefix = None
                entries.append((prefix, encoded_parts[-1].strip()))
            # Several bars list one entry per page (0/, 1/, 2/...) without the
            # FixedBarCollectible flag: Full Moon and Stellar Gold repeat the
            # same item per page, Lost Viscanium changes it. Read those as
            # pages too; as progress values they rolled one item three times.
            fixed_tiers = bool(int(bar_row.get("FixedBarCollectible") or 0)) or (
                len(entries) > 1
                and sorted(prefix for prefix, _ in entries if prefix is not None)
                == list(range(len(entries)))
            )
            seen_items = set()
            for prefix, item_id in entries:
                item = item_rows.get(item_id)
                if item is None:
                    continue
                if not fixed_tiers:
                    if item_id in seen_items:
                        continue
                    seen_items.add(item_id)
                if fixed_tiers:
                    # The prefix is the bar page, and one drop is worth the
                    # bar's ProgressbarBase: retail dropped the Dragan key in
                    # stacks of 2 on its 2.0-base bars (2026-10-28 capture).
                    tier = prefix or 0
                    base_value = max(1, round(float(
                        bar_row.get("ProgressbarBase") or 1
                    )))
                else:
                    # The prefix is the authored progress represented by one
                    # drop (for example ``2/e_sca_dun_01_progress_item``).
                    tier = None
                    base_value = max(
                        max(1, prefix or 1), int(item.get("BaseValue") or 1)
                    )
                collectibles.append(EventCollectible(
                    item_id=item_id,
                    item_category=str(item.get("ItemCategory") or "InstantConsumable"),
                    auto_pick_up=str(item.get("AutoPickUp") or "All"),
                    tier=tier,
                    base_value=base_value,
                    maps=frozenset(
                        _map_key(value) for value in _tokens(item.get("Region"))
                    ) | item_regions.get(item_id, frozenset()),
                    owner_events=frozenset(
                        value.casefold()
                        for value in _tokens(item.get("EventId"))
                    ),
                ))
            bars.append(EventProgressBarDefinition(
                resource_id=bar_id,
                minimum_level=max(1, int(bar_row.get("MinPlayerLevel") or 1)),
                maximum_level=max(1, int(bar_row.get("MaxPlayerLevel") or 200)),
                priority=int(bar_row.get("Priority") or 0),
                collectibles=tuple(collectibles),
                item_rewards=_parse_reward_entries(
                    bar_row.get("EventRewardItems"), currency=False
                ),
                currency_rewards=_parse_reward_entries(
                    bar_row.get("EventRewardCurrency"), currency=True
                ),
                monster_drops=not any(
                    int(bar_row.get(column) or 0)
                    for column in (
                        "ProgressByPVELeaderboardProgress",
                        "ProgressByPVPLeaderboardProgress",
                        "ProgressByLevelUp",
                    )
                ),
            ))
        templates[resource_id] = EventTemplateDefinition(
            resource_id=resource_id,
            client_name=str(row.get("Name") or resource_id),
            client_only=bool(int(row.get("ClientOnly") or 0)),
            map_local=bool(int(row.get("MapLocal") or 0)),
            minimum_level=max(1, int(row.get("MinPlayerLevel") or 1)),
            maximum_level=max(1, int(row.get("MaxPlayerLevel") or 200)),
            map_id=str(row.get("Map") or ""),
            excluded_maps=frozenset(
                _map_key(value) for value in _tokens(row.get("ExcludeMaps"))
            ),
            teleport_target=str(row.get("TeleportTarget") or ""),
            status_effects=_tokens(row.get("EventStatusEffects")),
            progress_bars=tuple(bars),
            category=str(row.get("Category") or ""),
            loot_list_id=str(row.get("Loot") or "").strip(),
        )

    events = []
    seen_ids = set()
    for entry in config.get("events", ()):
        resource_id = str(entry.get("template_id") or "").strip()
        template = templates.get(resource_id)
        if template is None:
            raise ValueError(f"event schedule references unknown template {resource_id!r}")
        event_id = int(entry["event_id"])
        if event_id <= 0 or event_id > UINT32_MAX or event_id in seen_ids:
            raise ValueError(f"invalid or duplicate runtime event id {event_id}")
        seen_ids.add(event_id)
        forced_state = str(entry.get("state") or "scheduled").casefold()
        ranks = frozenset(str(value).casefold() for value in entry.get("collectible_ranks", ()))
        if not ranks and resource_id == "scaling_dungeon_kill_event_01":
            ranks = frozenset(("leader", "champion", "guardian", "boss", "worldboss"))
        events.append(ScheduledEvent(
            event_id=event_id,
            template=template,
            starts_at=_timestamp(entry.get("starts_at")),
            ends_at=_timestamp(entry.get("ends_at")),
            forced_state=forced_state,
            tags=tuple(str(value) for value in entry.get("tags", ())),
            participation_default=bool(entry.get("participation_default", True)),
            collectible_drop_chance=max(0.0, min(1.0, float(entry.get("collectible_drop_chance", 0.35)))),
            collectible_amount=max(1, int(entry.get("collectible_amount", 1))),
            collectible_maps=frozenset(_map_key(value) for value in entry.get("collectible_maps", ())),
            collectible_ranks=ranks,
        ))

    travel_items = {}
    map_requirements: dict[str, set[str]] = {}
    event_collectible_maps: dict[str, set[str]] = {}
    known_game_events = frozenset(
        resource_id.casefold() for resource_id in templates
    )
    for item_id, row in item_rows.items():
        if str(row.get("ItemCategory") or "") != "TravelItem":
            continue
        target = str(row.get("TravelItemTarget") or "").strip()
        if not target:
            continue
        authored_event_ids = frozenset(
            value.casefold()
            for value in (
                *_tokens(row.get("EventId")),
                *_tokens(row.get("ActiveEvents")),
            )
        )
        # ActiveEvents also stores quest/map signal names. Only actual
        # _Template_Events IDs are game-event schedule gates.
        event_ids = authored_event_ids & known_game_events
        definition = TravelItemDefinition(
            template_id=item_id,
            target_map_id=target,
            required_level=max(1, int(row.get("RequiredLevel") or 1)),
            event_ids=event_ids,
            only_during_event=bool(int(row.get("OnlyActiveDuringEvent") or 0)),
        )
        travel_items[item_id] = definition
        if event_ids:
            target_key = _map_key(target)
            map_requirements.setdefault(target_key, set()).update(event_ids)
            for event_id in event_ids:
                event_collectible_maps.setdefault(event_id, set()).add(
                    target_key
                )

    # The Azar boss ration is authored as a LootGenItem with an empty
    # TravelItemTarget, while the client still sends the ordinary inventory
    # use action for it. Its intended destination is the first Azar access
    # group; that group's normal MapAccess selection supplies the difficulty
    # choices. Keep this compatibility mapping explicit instead of treating
    # arbitrary LootGenItems as travel items.
    azar_ration_id = "boss_j0003_lava_dun_boss_travelitem"
    azar_destination_id = "boss_j0003_lava_dun_boss_01_travelitem"
    azar_destination = travel_items.get(azar_destination_id)
    if azar_destination is not None and azar_ration_id not in travel_items:
        travel_items[azar_ration_id] = replace(
            azar_destination, template_id=azar_ration_id
        )

    # Several global event definitions leave _Template_Events.Map empty.
    # Their map ownership is instead encoded on the event's TravelItems,
    # access groups and keys. Without carrying it into the loot gate, every
    # active global event rolls its progress item in every map (for example
    # Big Game Hunt progress inside Desert of Essences, or Winter Solstice
    # progress in Kingshill). An explicit schedule override stays
    # authoritative and is kept apart, so the CMS carries only that forward.
    database_revision = db_path.stat()
    event_maps = _event_owned_maps(
        templates, item_rows, map_rows, access_rows, event_collectible_maps,
        _event_scripted_maps(db_path.parent.parent / "maps", templates),
        _event_exit_destinations(
            str(db_path), database_revision.st_mtime_ns, database_revision.st_size,
        ),
    )
    events = [
        replace(
            event,
            progress_maps=event_maps.get(
                event.template.resource_id.casefold(), frozenset()
            ),
        )
        for event in events
    ]
    map_regions: dict[str, set[str]] = {}
    group_regions: dict[str, set[str]] = {}
    for row in map_rows:
        regions = {value.casefold() for value in _tokens(row.get("Region"))}
        map_regions.setdefault(_map_key(row.get("MapTemplate")), set()).update(
            regions
        )
        group = _map_key(row.get("AccessGroup"))
        if group:
            group_regions.setdefault(group, set()).update(regions)
    for group, regions in group_regions.items():
        map_regions.setdefault(group, set()).update(regions)
    for row in access_rows:
        regions = group_regions.get(_map_key(row.get("AccessGroup")))
        if regions:
            map_regions.setdefault(_map_key(row.get("Id")), set()).update(
                regions
            )
    monster_regions = {
        str(monster_id).casefold(): frozenset(
            value.casefold() for value in _tokens(region)
        )
        for monster_id, region in monster_region_rows
    }

    modifiers: dict[int, list[DropAmountModifier]] = {}
    for effect_id, _resource_id, program in status_rows:
        for instruction in str(program or "").split(";"):
            match = _DROP_AMOUNT.match(instruction.strip())
            if match is None:
                continue
            modifiers.setdefault(int(effect_id), []).append(DropAmountModifier(
                int(effect_id), match.group(3).strip(), match.group(1).strip(),
                match.group(2).casefold(),
                status_stack_limits.get(int(effect_id), 1),
            ))

    return EventCatalog(
        events=tuple(sorted(events, key=lambda event: event.event_id)),
        travel_items=MappingProxyType(travel_items),
        event_map_requirements=MappingProxyType({
            key: frozenset(values) for key, values in map_requirements.items()
        }),
        drop_modifiers=MappingProxyType({
            key: tuple(values) for key, values in modifiers.items()
        }),
        event_maps=MappingProxyType(event_maps),
        map_regions=MappingProxyType({
            key: frozenset(values)
            for key, values in map_regions.items() if values
        }),
        monster_regions=MappingProxyType(monster_regions),
        event_loot=MappingProxyType(_event_loot_lists(loot_path, item_rows)),
        monster_event_drop_chances=MappingProxyType({
            str(monster_id).casefold(): min(1.0, float(chance))
            for monster_id, chance in monster_event_drop_rows
        }),
    )


def load_event_catalog(
    database_path: str | Path,
    schedule_path: str | Path,
    loot_path: str | Path | None = None,
) -> EventCatalog:
    # Looked up many times per map entry: use the last observed revisions
    # instead of stat()ing both files each time (runtime/file_revisions.py).
    # The CMS invalidates the schedule after writing it.
    database = resolved_path(str(database_path))
    schedule = resolved_path(str(schedule_path))
    loot = resolved_path(str(loot_path or DEFAULT_EVENT_LOOT_PATH))
    database_revision = observed_revision(database)
    schedule_revision = observed_revision(schedule)
    if database_revision is None:
        raise FileNotFoundError(database)
    if schedule_revision is None:
        raise FileNotFoundError(schedule)
    # The event loot table is optional: without it events drop no Loot list.
    loot_revision = observed_revision(loot)
    return _load_catalog(
        str(database), *database_revision, str(schedule), *schedule_revision,
        *((str(loot), *loot_revision) if loot_revision is not None else ()),
    )


@dataclass(frozen=True)
class EventStatusEffectSpec:
    """One durable buff an active event grants, resolved to its wire id."""

    resource_id: str
    effect_id: int
    values: tuple[float, ...]


def parse_event_status_effects(
    tokens, *, value_slots: int = 5,
) -> tuple[tuple[str, tuple[float, ...]], ...]:
    """Read ``_Template_Events.EventStatusEffects`` into (effect, values).

    The column mixes effect ids with ``$N:value`` parameter assignments for
    the effect named just before them: ``xpgain,$0:0.25`` is the XP bonus
    effect with its first program parameter set to 0.25, which the effect's
    ``XPGain:$0,relative`` modifier then reads. Effects without assignments
    keep zero parameters and rely on literal program values.
    """

    result: list[tuple[str, list[float]]] = []
    for token in tokens:
        text = str(token or "").strip()
        if not text:
            continue
        if text.startswith("$"):
            if not result:
                continue
            reference, _separator, raw_value = text[1:].partition(":")
            if not reference.strip().isdigit():
                continue
            try:
                value = float(raw_value.strip())
            except ValueError:
                continue
            index = int(reference)
            values = result[-1][1]
            while len(values) <= index:
                values.append(0.0)
            values[index] = value
            continue
        result.append((text, [0.0] * value_slots))
    return tuple(
        (resource_id, tuple(values)) for resource_id, values in result
    )


def event_status_effect_specs(
    catalog: EventCatalog,
    database_path: str | Path,
    *,
    now: datetime | None = None,
    player_level: int | None = None,
    include_inactive: bool = False,
) -> tuple[EventStatusEffectSpec, ...]:
    """Resolve the buffs the scheduled events grant a player.

    Only active events count unless ``include_inactive`` asks for every
    scheduled template, which the initialization phase uses to strip stale
    entries. A template's level window is honoured when ``player_level`` is
    given; the shipped XP events start at level 20.
    """

    runtime = load_status_runtime_catalog(database_path)
    events = (
        catalog.events if include_inactive else catalog.active_events(now)
    )
    specs: list[EventStatusEffectSpec] = []
    seen: set[str] = set()
    for event in events:
        template = event.template
        if player_level is not None and not (
            template.minimum_level <= int(player_level) <= template.maximum_level
        ):
            continue
        for resource_id, values in parse_event_status_effects(
            template.status_effects
        ):
            effect_id = runtime.effect_ids.get(resource_id)
            if effect_id is None or resource_id in seen:
                continue
            seen.add(resource_id)
            specs.append(
                EventStatusEffectSpec(resource_id, int(effect_id), values)
            )
    return tuple(specs)


def event_status_entry(
    spec: EventStatusEffectSpec, *, instance_id: int, actor_id: int,
) -> StatusEffectEntry:
    """Build the permanent, detailed entry that carries an event buff.

    Shaped like the costume and pet entries the initialization already
    sends: no duration, no start offset, the local player as causer, and the
    parameter values the effect program reads through ``$N``.
    """

    return StatusEffectEntry(
        effect_id=int(spec.effect_id),
        field_04=int(instance_id),
        field_0c=0,
        field_14=0,
        field_08=0,
        field_18=0,
        field_10=0,
        field_1c=100,
        field_20=int(actor_id),
        field_25=False,
        field_26=False,
        field_39=True,
        has_details=True,
        values=tuple(float(value) for value in spec.values),
    )


def event_status_effect_ids(
    catalog: EventCatalog,
    database_path: str | Path,
    *,
    now: datetime | None = None,
) -> tuple[int, ...]:
    return tuple(
        spec.effect_id
        for spec in event_status_effect_specs(catalog, database_path, now=now)
    )


__all__ = [
    "EventCatalog",
    "EventProgressAdvance",
    "EventReward",
    "ScheduledEvent",
    "TravelItemDefinition",
    "UINT32_MAX",
    "EventStatusEffectSpec",
    "event_status_effect_ids",
    "event_status_effect_specs",
    "event_status_entry",
    "load_event_catalog",
    "parse_event_status_effects",
]
