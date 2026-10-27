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
from typing import Iterable, Mapping

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
from dso_server.runtime.file_revisions import observed_revision, resolved_path


UINT32_MAX = 0xFFFFFFFF
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
    maps: frozenset[str]


@dataclass(frozen=True)
class EventProgressBarDefinition:
    resource_id: str
    minimum_level: int
    maximum_level: int
    priority: int
    collectibles: tuple[EventCollectible, ...]
    item_rewards: tuple[EventReward, ...]
    currency_rewards: tuple[EventReward, ...]

    def accepts(self, player_level: int) -> bool:
        return self.minimum_level <= player_level <= self.maximum_level


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
    ) -> tuple[tuple[str, int], ...]:
        current_map = _map_key(map_id)
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
        for event in self.active_events(now):
            template = event.template
            if not bool(participation.get(
                str(event.event_id), event.participation_default
            )):
                continue
            if not template.minimum_level <= player_level <= template.maximum_level:
                continue
            if current_map in template.excluded_maps:
                continue
            if template.map_id and _map_key(template.map_id) != current_map:
                continue
            event_maps = event.collectible_maps
            if event_maps and current_map not in event_maps:
                continue
            if event.collectible_ranks and rank not in event.collectible_ranks:
                continue
            bar = template.progress_bar(player_level)
            if bar is None:
                continue
            for collectible in bar.collectibles:
                # A scheduled map scope, whether explicit or derived from the
                # event TravelItems, is the authoritative ownership boundary.
                # Item Region metadata is only a legacy fallback: some events
                # omit their first-stage map there even though their TravelItem
                # explicitly opens that map for the event.
                if (
                    not event_maps
                    and collectible.maps
                    and current_map not in collectible.maps
                ):
                    continue
                if rng.random() > event.collectible_drop_chance:
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
) -> EventCatalog:
    del database_mtime_ns, database_size, schedule_mtime_ns, schedule_size
    db_path = Path(database_path)
    config = json.loads(Path(schedule_path).read_text(encoding="utf-8"))
    with sqlite3.connect(
        f"file:{db_path.as_posix()}?mode=ro", uri=True
    ) as database:
        database.row_factory = sqlite3.Row
        item_rows = {
            str(row["Id"]): dict(row)
            for row in database.execute("SELECT * FROM _Template_Item")
        }
        map_ids = frozenset(
            _map_key(row["MapTemplate"])
            for row in database.execute(
                compat.map_template_query(database, ("MapTemplate",))
            )
        )
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

    templates: dict[str, EventTemplateDefinition] = {}
    for resource_id, row in event_rows.items():
        bars = []
        for bar_id in _tokens(row.get("EventProgressBars")):
            bar_row = progress_rows.get(bar_id)
            if bar_row is None:
                continue
            collectibles = []
            for encoded in _tokens(bar_row.get("EventCollectibles")):
                encoded_parts = encoded.split("/", 1)
                item_id = encoded_parts[-1].strip()
                try:
                    progress_value = (
                        max(1, int(encoded_parts[0]))
                        if len(encoded_parts) == 2 else 1
                    )
                except ValueError:
                    progress_value = 1
                item = item_rows.get(item_id)
                if item is None:
                    continue
                collectibles.append(EventCollectible(
                    item_id=item_id,
                    item_category=str(item.get("ItemCategory") or "InstantConsumable"),
                    auto_pick_up=str(item.get("AutoPickUp") or "All"),
                    # The prefix is the authored progress represented by one
                    # drop (for example ``2/e_sca_dun_01_progress_item``).
                    base_value=max(
                        progress_value, int(item.get("BaseValue") or 1)
                    ),
                    maps=frozenset(_map_key(value) for value in _tokens(item.get("Region"))),
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
    # Their authoritative map ownership is instead encoded on the event's
    # TravelItems. Without carrying those targets into the loot gate, every
    # active global event can roll its progress item in every map (for
    # example Big Game Hunt progress inside Desert of Essences). An explicit
    # schedule override remains authoritative for development events.
    events = [
        replace(
            event,
            collectible_maps=(
                event.collectible_maps
                or frozenset(event_collectible_maps.get(
                    event.template.resource_id.casefold(),
                    (),
                ))
            ),
        )
        for event in events
    ]

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
    )


def load_event_catalog(
    database_path: str | Path,
    schedule_path: str | Path,
) -> EventCatalog:
    # Looked up many times per map entry: use the last observed revisions
    # instead of stat()ing both files each time (runtime/file_revisions.py).
    # The CMS invalidates the schedule after writing it.
    database = resolved_path(str(database_path))
    schedule = resolved_path(str(schedule_path))
    database_revision = observed_revision(database)
    schedule_revision = observed_revision(schedule)
    if database_revision is None:
        raise FileNotFoundError(database)
    if schedule_revision is None:
        raise FileNotFoundError(schedule)
    return _load_catalog(
        str(database), *database_revision, str(schedule), *schedule_revision,
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
