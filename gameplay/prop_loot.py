"""Server-owned contents of map chest loot lists (amphorae).

Map StateObjects name a ``LootListId`` such as ``CHEST_amphora_normal_MAIN``;
the retail server owned the rows and the client data only names them, so
their contents are authored in ``data/catalogs/prop_loot.json``.

Retail reference (2026-10-28 capture, ``chest_amphora_normal`` on
``e_dra_03_dun_01`` while the Dragan event ran): the click spent one
``e_sar_chest_key`` and spawned seven NewItems at the clicking player's
position on a 0.66 m grid -- 10 Draken, three gold stacks (276/234/208), the
event's current progress item twice (x3 and x50) and one
``e_dra_03_hardmode_bosskey`` -- then PropUpdate state 3. Draken and the
progress key were auto-picked; the boss key (AutoPickUp Privileged) was not.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
import json
import math
from pathlib import Path
import random
from types import MappingProxyType

from dso_server.paths import DEFAULT_PROP_LOOT_PATH
from dso_server.runtime import resident_content


@dataclass(frozen=True)
class PropLootEntry:
    """One roll of a chest loot list.

    ``item_id`` is empty for an ``event_progress`` entry, which drops the
    current page collectible of every event whose progress applies on the
    map. ``event_id`` limits an item entry to maps where that event's
    progress applies. ``per_level`` sizes each stack as the player level
    times a uniform factor (gold); otherwise ``amount`` is a [min, max]
    range. ``stacks`` is the number of separate ground objects. Entries
    sharing ``roll_group`` form one weighted, one-of roll.
    ``minimum_difficulty`` gates an entry, and
    ``scale_chance_with_difficulty`` applies the selected map's authored drop
    multipliers to its independent ``chance`` roll.
    """

    item_id: str
    event_progress: bool
    event_id: str
    event_progress_item_id: str
    chance: float
    amount: tuple[int, int]
    per_level: tuple[float, float] | None
    stacks: int
    map_id: str = ""
    item_options_by_class: Mapping[str, tuple[str, ...]] = MappingProxyType({})
    roll_group: str = ""
    weight: float = 0.0
    minimum_difficulty: int = 0
    maximum_difficulty: int | None = None
    scale_chance_with_difficulty: bool = False
    required_active_costumes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PropLootCatalog:
    lists: Mapping[str, tuple[PropLootEntry, ...]]
    aliases: Mapping[str, str]

    def entries(self, list_id: str) -> tuple[PropLootEntry, ...] | None:
        key = str(list_id or "").strip().casefold()
        return self.lists.get(self.aliases.get(key, key))

    def has(self, list_id: str) -> bool:
        return self.entries(list_id) is not None


def _range(value: object, name: str, *, minimum: float) -> tuple[float, float]:
    low, high = (
        (float(value[0]), float(value[-1])) if isinstance(value, list)
        else (float(value), float(value))
    )
    if not minimum <= low <= high:
        raise ValueError(f"prop loot {name} must satisfy {minimum} <= min <= max")
    return low, high


def _parse_entry(list_id: str, entry: Mapping[str, object]) -> PropLootEntry:
    event_progress = bool(entry.get("event_progress"))
    item_id = str(entry.get("item") or "").strip()
    raw_class_options = dict(entry.get("item_options_by_class") or {})
    class_options = {}
    for class_name, values in raw_class_options.items():
        options = tuple(str(value) for value in values if str(value))
        if not options:
            raise ValueError(
                f"prop loot class options for {class_name!r} cannot be empty"
            )
        class_options[str(class_name).casefold()] = options
    if not item_id and class_options:
        item_id = next(iter(class_options.values()))[0]
    if event_progress == bool(item_id):
        raise ValueError(
            f"prop loot list {list_id!r}: an entry names exactly one of "
            "'item' or 'event_progress'"
        )
    per_level = None
    if "per_level" in entry:
        per_level = _range(entry["per_level"], "per_level", minimum=0.0)
    low, high = _range(entry.get("amount", 1), "amount", minimum=1)
    roll_group = str(entry.get("roll_group") or "").strip().casefold()
    weight = max(0.0, float(entry.get("weight", 0.0)))
    if bool(roll_group) != (weight > 0.0):
        raise ValueError(
            f"prop loot list {list_id!r}: roll_group and positive weight "
            "must be specified together"
        )
    minimum_difficulty = max(0, int(entry.get("minimum_difficulty", 0)))
    maximum_difficulty = (
        max(0, int(entry["maximum_difficulty"]))
        if entry.get("maximum_difficulty") is not None else None
    )
    if (
        maximum_difficulty is not None
        and maximum_difficulty < minimum_difficulty
    ):
        raise ValueError(
            f"prop loot list {list_id!r}: maximum_difficulty must be "
            "at least minimum_difficulty"
        )
    return PropLootEntry(
        item_id=item_id,
        event_progress=event_progress,
        event_id=str(entry.get("event") or "").strip().casefold(),
        event_progress_item_id=(
            str(entry.get("event_progress_item") or "").strip().casefold()
        ),
        chance=max(0.0, min(1.0, float(entry.get("chance", 1.0)))),
        amount=(int(low), int(high)),
        per_level=per_level,
        stacks=max(1, int(entry.get("stacks", 1))),
        map_id=str(entry.get("map") or "").strip().casefold(),
        item_options_by_class=MappingProxyType(class_options),
        roll_group=roll_group,
        weight=weight,
        minimum_difficulty=minimum_difficulty,
        maximum_difficulty=maximum_difficulty,
        scale_chance_with_difficulty=bool(
            entry.get("scale_chance_with_difficulty")
        ),
        required_active_costumes=frozenset(
            str(value).strip().casefold()
            for value in entry.get("required_active_costumes", ())
            if str(value).strip()
        ),
    )


@lru_cache(maxsize=4)
def _load_catalog(path: str) -> PropLootCatalog:
    document = json.loads(resident_content.read_text(path))
    lists = document.get("loot_lists") if isinstance(document, dict) else None
    if not isinstance(lists, dict):
        raise ValueError("prop loot must contain a loot_lists object")
    parsed = {}
    for list_id, value in lists.items():
        entries = value.get("entries") if isinstance(value, dict) else value
        if not isinstance(entries, list):
            raise ValueError(f"prop loot list {list_id!r} needs an entries array")
        parsed[str(list_id).casefold()] = tuple(
            _parse_entry(list_id, entry) for entry in entries
        )
    aliases = {}
    for alias, target in dict(document.get("aliases") or {}).items():
        target_key = str(target).casefold()
        if target_key not in parsed:
            raise ValueError(f"prop loot alias {alias!r} names unknown list {target!r}")
        aliases[str(alias).casefold()] = target_key
    return PropLootCatalog(MappingProxyType(parsed), MappingProxyType(aliases))


def load_prop_loot_catalog(path: str | Path | None = None) -> PropLootCatalog:
    return _load_catalog(str(path or DEFAULT_PROP_LOOT_PATH))


def has_prop_loot(list_id: str) -> bool:
    """Whether the server can fill this chest; a missing catalog means no."""

    if not list_id:
        return False
    try:
        return load_prop_loot_catalog().has(list_id)
    except (OSError, ValueError):
        return False


def roll_prop_loot(
    entries: Iterable[PropLootEntry],
    *,
    player_level: int,
    rng: random.Random,
    event_collectibles: Mapping[str, tuple[str, ...]],
    class_name: str = "",
    map_id: str = "",
    map_aliases: Iterable[str] = (),
    map_difficulty: int = 0,
    active_costume: str = "",
    privileged_difficulty_multiplier: float = 1.0,
) -> tuple[tuple[str, int], ...]:
    """Roll one chest for one player.

    ``event_collectibles`` maps each event whose progress applies on this map
    (casefolded template id) to the collectibles this player is collecting
    there, from ``EventCatalog.map_event_collectibles``.
    """

    level = max(1, int(player_level))
    current_maps = frozenset(
        str(value).strip().casefold()
        for value in (map_id, *map_aliases)
        if str(value).strip()
    )
    current_costume = str(active_costume or "").strip().casefold()
    result = []
    weighted_groups: dict[str, list[PropLootEntry]] = {}

    def add_entry(entry: PropLootEntry) -> None:
        if entry.event_progress:
            items = tuple(
                item_id
                for event_id, collectibles in event_collectibles.items()
                if not entry.event_id or event_id.casefold() == entry.event_id
                for item_id in collectibles
                if (
                    not entry.event_progress_item_id
                    or item_id.casefold() == entry.event_progress_item_id
                )
            )
        else:
            class_options = entry.item_options_by_class
            if class_options:
                options = class_options.get(str(class_name).casefold(), ())
                if not options:
                    return
                items = (rng.choice(options),)
            else:
                items = (entry.item_id,)
        for item_id in items:
            for _stack in range(entry.stacks):
                if entry.per_level is not None:
                    amount = max(1, round(level * rng.uniform(*entry.per_level)))
                else:
                    amount = rng.randint(*entry.amount)
                result.append((item_id, amount))

    for entry in entries:
        if (
            entry.required_active_costumes
            and current_costume not in entry.required_active_costumes
        ):
            continue
        if entry.map_id and entry.map_id not in current_maps:
            continue
        if entry.event_id and entry.event_id not in event_collectibles:
            continue
        if int(map_difficulty) < entry.minimum_difficulty:
            continue
        if (
            entry.maximum_difficulty is not None
            and int(map_difficulty) > entry.maximum_difficulty
        ):
            continue
        chance = entry.chance
        if entry.scale_chance_with_difficulty:
            chance *= max(0.0, float(privileged_difficulty_multiplier))
        chance = min(1.0, max(0.0, chance))
        if entry.roll_group:
            weighted_groups.setdefault(entry.roll_group, []).append(entry)
            continue
        if chance < 1.0 and rng.random() >= chance:
            continue
        add_entry(entry)

    for group in weighted_groups.values():
        chosen = rng.choices(
            group,
            weights=tuple(entry.weight for entry in group),
            k=1,
        )[0]
        add_entry(chosen)
    return tuple(result)


# Retail placed the seven amphora drops in this order around the clicking
# player, in 0.66 m cells (2026-10-28 capture); further drops fill the
# remaining cells outward.
CHEST_DROP_SPACING = 0.66
_RETAIL_DROP_CELLS = ((0, 0), (1, -1), (-1, 1), (1, 1), (-1, -1), (-2, 0), (0, -2))


def _drop_cells():
    yield from _RETAIL_DROP_CELLS
    seen = set(_RETAIL_DROP_CELLS)
    radius = 1
    while True:
        ring = sorted(
            (
                (dx, dz)
                for dx in range(-radius, radius + 1)
                for dz in range(-radius, radius + 1)
                if (dx, dz) not in seen
            ),
            key=lambda cell: (math.hypot(*cell), cell),
        )
        for cell in ring:
            seen.add(cell)
            yield cell
        radius += 1


def chest_drop_positions(
    origin: tuple[float, float, float], count: int, *,
    spacing: float = CHEST_DROP_SPACING,
) -> tuple[tuple[float, float, float], ...]:
    cells = _drop_cells()
    return tuple(
        (
            float(origin[0]) + dx * max(0.1, float(spacing)),
            float(origin[1]),
            float(origin[2]) + dz * max(0.1, float(spacing)),
        )
        for dx, dz in (next(cells) for _ in range(max(0, int(count))))
    )


__all__ = [
    "CHEST_DROP_SPACING",
    "PropLootCatalog",
    "PropLootEntry",
    "chest_drop_positions",
    "has_prop_loot",
    "load_prop_loot_catalog",
    "roll_prop_loot",
]
