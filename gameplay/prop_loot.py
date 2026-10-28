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
    range. ``stacks`` is the number of separate ground objects.
    """

    item_id: str
    event_progress: bool
    event_id: str
    chance: float
    amount: tuple[int, int]
    per_level: tuple[float, float] | None
    stacks: int


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
    if event_progress == bool(item_id):
        raise ValueError(
            f"prop loot list {list_id!r}: an entry names exactly one of "
            "'item' or 'event_progress'"
        )
    per_level = None
    if "per_level" in entry:
        per_level = _range(entry["per_level"], "per_level", minimum=0.0)
    low, high = _range(entry.get("amount", 1), "amount", minimum=1)
    return PropLootEntry(
        item_id=item_id,
        event_progress=event_progress,
        event_id=str(entry.get("event") or "").strip().casefold(),
        chance=max(0.0, min(1.0, float(entry.get("chance", 1.0)))),
        amount=(int(low), int(high)),
        per_level=per_level,
        stacks=max(1, int(entry.get("stacks", 1))),
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
) -> tuple[tuple[str, int], ...]:
    """Roll one chest for one player.

    ``event_collectibles`` maps each event whose progress applies on this map
    (casefolded template id) to the collectibles this player is collecting
    there, from ``EventCatalog.map_event_collectibles``.
    """

    level = max(1, int(player_level))
    result = []
    for entry in entries:
        if entry.event_id and entry.event_id not in event_collectibles:
            continue
        if entry.chance < 1.0 and rng.random() >= entry.chance:
            continue
        if entry.event_progress:
            items = tuple(
                item_id
                for collectibles in event_collectibles.values()
                for item_id in collectibles
            )
        else:
            items = (entry.item_id,)
        for item_id in items:
            for _stack in range(entry.stacks):
                if entry.per_level is not None:
                    amount = max(1, round(level * rng.uniform(*entry.per_level)))
                else:
                    amount = rng.randint(*entry.amount)
                result.append((item_id, amount))
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
    origin: tuple[float, float, float], count: int,
) -> tuple[tuple[float, float, float], ...]:
    cells = _drop_cells()
    return tuple(
        (
            float(origin[0]) + dx * CHEST_DROP_SPACING,
            float(origin[1]),
            float(origin[2]) + dz * CHEST_DROP_SPACING,
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
