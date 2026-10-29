"""Persistent sticker-book definitions backed by the shared static database."""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
import sqlite3

from dso_server.protocol.gameplay_protocol import StatusEffect, StatusEffectEntry
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.gameplay.local_mount_state import (
    MountDefinition,
    with_captured_timing,
)


SERVER_TICKS_PER_SECOND = 25


@dataclass(frozen=True)
class StickerDefinition:
    resource_id: str
    source_item_ids: tuple[str, ...]
    item_category: str
    status_effect_resource_id: str
    status_effect_wire_id: int
    duration_ticks: int
    values: tuple[float, ...]
    exclusive_group: str
    start_modifiers: str
    mount: MountDefinition | None = None
    stickerbook_category_id: int | None = None
    aliases: tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        if self.stickerbook_category_id == 2:
            return "emote"
        if self.stickerbook_category_id == 6:
            return "decoration"
        if self.mount is not None or self.item_category == "Mount":
            return "mount"
        if self.item_category == "Pet":
            return "pet"
        if self.item_category in {"Switchable", "Costume", "TempCostume"}:
            return "costume"
        return self.item_category.casefold()


@dataclass(frozen=True)
class StickerInventoryMutation:
    inventory: InventoryInfo
    delta: InventoryInfo
    final: InventoryInfo
    next_item_id: int


@dataclass(frozen=True)
class CollectorEffectDefinition:
    resource_id: str
    wire_id: int
    stack_count: int
    duration_ticks: int


@dataclass(frozen=True)
class StickerUpgradeDefinition:
    source_resource_id: str
    target_resource_id: str
    cost: int
    tier: int


@dataclass(frozen=True)
class CollectorEffectTransition:
    effects: dict[int, StatusEffectEntry]
    next_instance_id: int
    status_effect: StatusEffect | None


def _split(value: object) -> tuple[str, ...]:
    return tuple(
        part.strip() for part in str(value or "").split(";") if part.strip()
    )


def _rarity_upgrade_stages(
    items: object, upgrades: object
) -> tuple[tuple[str, int], ...]:
    """Return base and generated rarity tiers in authored order."""

    base = _split(items)
    if not base:
        return ()
    stages = [(base[0], 0)]
    for encoded in _split(upgrades):
        resource_id, separator, raw_cost = encoded.partition(":")
        if not separator or not resource_id.strip():
            continue
        try:
            cost = max(0, int(raw_cost.strip()))
        except ValueError:
            continue
        stages.append((resource_id.strip(), cost))
    return tuple(stages)


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _mount_definition_from_item(
    database: sqlite3.Connection,
    item: sqlite3.Row,
    *,
    resource_id: str,
    stickerbook_state: int,
    stickerbook_favorite_position: int,
) -> MountDefinition | None:
    """Resolve an item's cast effect into the paired persistent ride effect."""

    if str(item["ItemCategory"] or "") != "Mount":
        return None
    cast_resource = str(item["StatusEffectId"] or "")
    if not cast_resource:
        return None
    cast = database.execute(
        """
        SELECT rowid, StatusEffectDuration, DoneModifiers
        FROM _Template_StatusEffect WHERE Id=? LIMIT 1
        """,
        (cast_resource,),
    ).fetchone()
    if cast is None:
        return None

    # Most mount casts use cast_* / ride_* names. Prefer the explicit
    # DoneModifiers link because event and legacy mounts need not follow that
    # convention.
    ride_resource = ""
    marker = "ActorStatusEffect:addae,"
    done_modifiers = str(cast["DoneModifiers"] or "")
    if marker in done_modifiers:
        ride_resource = done_modifiers.split(marker, 1)[1].split(
            ",", 1
        )[0].strip()
    if not ride_resource:
        ride_resource = (
            "ride_" + cast_resource.removeprefix("cast_")
            if cast_resource.startswith("cast_")
            else cast_resource.replace("cast", "ride", 1)
        )
    ride = database.execute(
        """
        SELECT rowid, StatusEffectDuration, StartModifiers
        FROM _Template_StatusEffect WHERE Id=? LIMIT 1
        """,
        (ride_resource,),
    ).fetchone()
    if ride is None:
        return None

    mount_resource = str(resource_id)
    mounted_marker = "Mounted:Mount,"
    modifiers = str(ride["StartModifiers"] or "")
    if mounted_marker in modifiers:
        mount_resource = modifiers.split(mounted_marker, 1)[1].split(
            "#", 1
        )[0].split(";", 1)[0]
    item_duration = float(item["StatusEffectDuration"] or 0.0)
    cast_duration = float(cast["StatusEffectDuration"] or 0.0)
    cast_seconds = max(0.1, item_duration or cast_duration)
    return with_captured_timing(MountDefinition(
        stickerbook_resource_id=str(resource_id),
        mount_resource_id=mount_resource,
        cast_effect_id=int(cast["rowid"]) - 1,
        ride_effect_id=int(ride["rowid"]) - 1,
        cast_duration_seconds=cast_seconds,
        cast_duration_ticks=max(
            1, round(cast_seconds * SERVER_TICKS_PER_SECOND)
        ),
        ride_duration_ticks=max(
            1,
            round(
                float(ride["StatusEffectDuration"] or 36000.0)
                * SERVER_TICKS_PER_SECOND
            ),
        ),
        stickerbook_state=int(stickerbook_state),
        stickerbook_favorite_position=int(stickerbook_favorite_position),
    ))


@lru_cache(maxsize=1024)
def _load_inventory_mount_definition_cached(
    static_database_path: Path,
    item_template_id: str,
) -> MountDefinition | None:
    with _open_read_only(static_database_path) as database:
        item = database.execute(
            """
            SELECT Id, ItemCategory, StatusEffectId, StatusEffectDuration
            FROM _Template_Item WHERE Id=? LIMIT 1
            """,
            (item_template_id,),
        ).fetchone()
        if item is None:
            return None
        return _mount_definition_from_item(
            database,
            item,
            resource_id=str(item["Id"]),
            stickerbook_state=0,
            stickerbook_favorite_position=0,
        )


def load_inventory_mount_definition(
    static_database_path: str | Path,
    item_template_id: str,
) -> MountDefinition | None:
    """Load a reusable mount that is activated directly from inventory."""

    return _load_inventory_mount_definition_cached(
        Path(static_database_path), str(item_template_id)
    )


@lru_cache(maxsize=8)
def _sticker_rows_by_identifier(static_database_path: Path) -> dict:
    """Map every base item, rarity tier and sticker id to its book row.

    Built once per database: scanning all 807 live rows for each lookup cost
    ~3 ms, so resolving a large collection on map entry took over half a
    second. The first row naming an identifier wins, as the scan did.
    """

    index: dict[str, sqlite3.Row] = {}
    with _open_read_only(static_database_path) as database:
        for row in database.execute(
            "SELECT s.Id, s.Items, s.RarityUpgradeItems, b.StickerbookCategoryId "
            "FROM _Template_StickerbookItem AS s "
            "LEFT JOIN _Template_Stickerbook AS b ON b.Id=s.StickerbookId "
            "WHERE s.IsLive<>0"
        ):
            for identifier in (
                *_split(row["Items"]),
                *(
                    resource_id
                    for resource_id, _cost in _rarity_upgrade_stages(
                        row["Items"], row["RarityUpgradeItems"]
                    )
                ),
                str(row["Id"]),
            ):
                index.setdefault(identifier, row)
    return index


@lru_cache(maxsize=1024)
def _load_sticker_definition_cached(
    static_database_path: Path,
    resource_or_item_id: str,
) -> StickerDefinition | None:
    sticker_row = _sticker_rows_by_identifier(static_database_path).get(
        resource_or_item_id
    )
    if sticker_row is None:
        return None
    with _open_read_only(static_database_path) as database:
        stages = _rarity_upgrade_stages(
            sticker_row["Items"], sticker_row["RarityUpgradeItems"]
        )
        base_aliases = _split(sticker_row["Items"])
        source_item_ids = tuple(dict.fromkeys((
            *base_aliases, *(resource_id for resource_id, _cost in stages),
        )))
        if not source_item_ids:
            return None
        resource_id = (
            resource_or_item_id
            if resource_or_item_id in source_item_ids
            else source_item_ids[0]
        )
        item = database.execute(
            """
            SELECT Id, ItemCategory, StatusEffectId, StatusEffectDuration,
                   StatusEffectValue0, StatusEffectValue1,
                   StatusEffectValue2, StatusEffectValue3,
                   StatusEffectValue4
            FROM _Template_Item
            WHERE Id=? LIMIT 1
            """,
            (resource_id,),
        ).fetchone()
        if item is None:
            item = database.execute(
                """
                SELECT Id, ItemCategory, StatusEffectId,
                       StatusEffectDuration, StatusEffectValue0,
                       StatusEffectValue1, StatusEffectValue2,
                       StatusEffectValue3, StatusEffectValue4
                FROM _Template_Item
                WHERE Id=? LIMIT 1
                """,
                (source_item_ids[0],),
            ).fetchone()
        if item is None or not str(item["StatusEffectId"] or ""):
            return None

        status_resource = str(item["StatusEffectId"])
        # The dwarf Guardian Angel item row incorrectly points at the Plague
        # Doctor status effect in this client export. Resolve its own authored
        # skin effect so the two switchable costumes remain distinct.
        if (
            resource_id.casefold() == "costume_dwarf_guardian_angel"
            and status_resource.casefold() == "costume_dwarf_plaguedoctor"
        ):
            status_resource = resource_id
        status = database.execute(
            """
            SELECT rowid, StatusEffectDuration, ExclusiveGroup, StartModifiers
            FROM _Template_StatusEffect WHERE Id=? LIMIT 1
            """,
            (status_resource,),
        ).fetchone()
        if status is None:
            return None
        item_duration = float(item["StatusEffectDuration"] or 0.0)
        status_duration = float(status["StatusEffectDuration"] or 0.0)
        duration_seconds = item_duration or status_duration
        values = tuple(
            float(item[f"StatusEffectValue{index}"] or 0.0)
            for index in range(5)
        )

        mount = None
        if str(item["ItemCategory"]) == "Mount":
            ride_resource = (
                "ride_" + status_resource.removeprefix("cast_")
                if status_resource.startswith("cast_")
                else status_resource.replace("cast", "ride", 1)
            )
            ride = database.execute(
                """
                SELECT rowid, StatusEffectDuration, StartModifiers
                FROM _Template_StatusEffect WHERE Id=? LIMIT 1
                """,
                (ride_resource,),
            ).fetchone()
            if ride is None:
                return None
            mount_resource = resource_id
            marker = "Mounted:Mount,"
            modifiers = str(ride["StartModifiers"] or "")
            if marker in modifiers:
                mount_resource = modifiers.split(marker, 1)[1].split(
                    "#", 1
                )[0].split(";", 1)[0]
            cast_seconds = max(0.1, duration_seconds)
            mount = with_captured_timing(MountDefinition(
                stickerbook_resource_id=resource_id,
                mount_resource_id=mount_resource,
                cast_effect_id=int(status["rowid"]) - 1,
                ride_effect_id=int(ride["rowid"]) - 1,
                cast_duration_seconds=cast_seconds,
                cast_duration_ticks=max(
                    1, round(cast_seconds * SERVER_TICKS_PER_SECOND)
                ),
                ride_duration_ticks=max(
                    1,
                    round(
                        float(ride["StatusEffectDuration"] or 36000.0)
                        * SERVER_TICKS_PER_SECOND
                    ),
                ),
                stickerbook_state=2,
                stickerbook_favorite_position=0,
            ))

        return StickerDefinition(
            resource_id=resource_id,
            source_item_ids=source_item_ids,
            item_category=str(item["ItemCategory"] or ""),
            status_effect_resource_id=status_resource,
            status_effect_wire_id=int(status["rowid"]) - 1,
            duration_ticks=max(
                1, round(duration_seconds * SERVER_TICKS_PER_SECOND)
            ),
            values=values,
            exclusive_group=str(status["ExclusiveGroup"] or ""),
            start_modifiers=str(status["StartModifiers"] or ""),
            mount=mount,
            stickerbook_category_id=sticker_row["StickerbookCategoryId"],
            # Base item variants unlock the same collectible; rarity upgrades
            # remain distinct tiers and must not be treated as aliases.
            aliases=(
                base_aliases if resource_id in base_aliases else (resource_id,)
            ),
        )


@lru_cache(maxsize=1024)
def _load_sticker_upgrade_cached(
    static_database_path: Path,
    resource_id: str,
) -> StickerUpgradeDefinition | None:
    with _open_read_only(static_database_path) as database:
        for row in database.execute(
            "SELECT Items, RarityUpgradeItems "
            "FROM _Template_StickerbookItem WHERE IsLive<>0"
        ):
            stages = _rarity_upgrade_stages(
                row["Items"], row["RarityUpgradeItems"]
            )
            for index, (current, _current_cost) in enumerate(stages[:-1]):
                if current != str(resource_id):
                    continue
                target, cost = stages[index + 1]
                return StickerUpgradeDefinition(
                    source_resource_id=current,
                    target_resource_id=target,
                    cost=max(0, int(cost)),
                    tier=index + 1,
                )
    return None


def load_sticker_upgrade(
    static_database_path: str | Path,
    resource_id: str,
) -> StickerUpgradeDefinition | None:
    """Return the next authored altered-crescerite tier, if one exists."""

    return _load_sticker_upgrade_cached(
        Path(static_database_path), str(resource_id)
    )


def load_sticker_definition(
    static_database_path: str | Path,
    resource_or_item_id: str,
) -> StickerDefinition | None:
    return _load_sticker_definition_cached(
        Path(static_database_path), str(resource_or_item_id)
    )


# Inventory items a player can wear or summon by using them directly, without
# first adding them to the sticker book.  263 of the 584 ``Switchable``
# costumes are not sticker-book collectibles at all, so the book cannot be the
# only route to their status effect.
_INVENTORY_COSMETIC_CATEGORIES = frozenset(
    {"Switchable", "Costume", "TempCostume", "Pet"}
)


@lru_cache(maxsize=1024)
def _load_item_cosmetic_definition_cached(
    static_database_path: Path,
    template_id: str,
) -> StickerDefinition | None:
    with _open_read_only(static_database_path) as database:
        item = database.execute(
            """
            SELECT Id, ItemCategory, StatusEffectId, StatusEffectDuration,
                   StatusEffectValue0, StatusEffectValue1,
                   StatusEffectValue2, StatusEffectValue3,
                   StatusEffectValue4
            FROM _Template_Item
            WHERE Id=? LIMIT 1
            """,
            (template_id,),
        ).fetchone()
        if item is None:
            return None
        category = str(item["ItemCategory"] or "")
        status_resource = str(item["StatusEffectId"] or "")
        if category not in _INVENTORY_COSMETIC_CATEGORIES or not status_resource:
            return None
        status = database.execute(
            """
            SELECT rowid, StatusEffectDuration, ExclusiveGroup, StartModifiers
            FROM _Template_StatusEffect WHERE Id=? LIMIT 1
            """,
            (status_resource,),
        ).fetchone()
        if status is None:
            return None
        duration_seconds = (
            float(item["StatusEffectDuration"] or 0.0)
            or float(status["StatusEffectDuration"] or 0.0)
        )
        return StickerDefinition(
            resource_id=str(item["Id"]),
            source_item_ids=(str(item["Id"]),),
            item_category=category,
            status_effect_resource_id=status_resource,
            status_effect_wire_id=int(status["rowid"]) - 1,
            duration_ticks=max(
                1, round(duration_seconds * SERVER_TICKS_PER_SECOND)
            ),
            values=tuple(
                float(item[f"StatusEffectValue{index}"] or 0.0)
                for index in range(5)
            ),
            exclusive_group=str(status["ExclusiveGroup"] or ""),
            start_modifiers=str(status["StartModifiers"] or ""),
            mount=None,
            stickerbook_category_id=None,
            aliases=(str(item["Id"]),),
        )


def load_cosmetic_definition(
    static_database_path: str | Path,
    resource_or_item_id: str,
) -> StickerDefinition | None:
    """Resolve a costume/pet by sticker-book id or by inventory item template.

    Sticker-book collectibles come first so an owned costume keeps its book
    identity; anything else falls back to the item's own status effect.
    """

    path = Path(static_database_path)
    identifier = str(resource_or_item_id)
    return (
        _load_sticker_definition_cached(path, identifier)
        or _load_item_cosmetic_definition_cached(path, identifier)
    )


def owned_source_item_ids(
    static_database_path: str | Path,
    owned_resource_ids: tuple[str, ...] | set[str],
) -> frozenset[str]:
    result: set[str] = set()
    for resource_id in owned_resource_ids:
        definition = load_sticker_definition(
            static_database_path, resource_id
        )
        if definition is not None:
            result.update(definition.source_item_ids)
    return frozenset(result)


@lru_cache(maxsize=256)
def _collector_effect_definitions_cached(
    static_database_path: Path,
    owned_resource_ids: tuple[str, ...],
) -> tuple[CollectorEffectDefinition, ...]:
    """Resolve all earned collector-bag status effects by subgroup count."""

    owned = set(owned_resource_ids)
    with _open_read_only(static_database_path) as database:
        subgroup_counts: dict[str, int] = {}
        for row in database.execute(
            "SELECT Id, Items, StickerbookId "
            "FROM _Template_StickerbookItem WHERE IsLive<>0"
        ):
            resources = {str(row["Id"]), *_split(row["Items"])}
            if not resources.intersection(owned):
                continue
            subgroup = str(row["StickerbookId"] or "")
            if subgroup:
                subgroup_counts[subgroup] = subgroup_counts.get(subgroup, 0) + 1

        earned: list[str] = []
        for subgroup, count in subgroup_counts.items():
            row = database.execute(
                "SELECT StickerbookRewardItems FROM _Template_Stickerbook "
                "WHERE Id=? LIMIT 1",
                (subgroup,),
            ).fetchone()
            if row is None:
                continue
            for encoded in _split(row["StickerbookRewardItems"]):
                parts = tuple(part.strip() for part in encoded.split(":"))
                if len(parts) < 2:
                    continue
                try:
                    threshold = int(parts[0])
                except ValueError:
                    continue
                reward_id = parts[1]
                if count < threshold:
                    continue
                status = database.execute(
                    "SELECT rowid, StatusEffectDuration, MaxStackSize "
                    "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
                    (reward_id,),
                ).fetchone()
                if status is None:
                    continue
                # The client interprets StatusEffectEntry.field_18 as the
                # effect's authored rank, not a generic stack counter.  Two
                # sticker subgroups earning the same +3% HP reward therefore
                # need two rank-zero instances; encoding one rank-one effect
                # makes it display as +30%.
                earned.append(reward_id)

        definitions = []
        for reward_id in sorted(earned):
            status = database.execute(
                "SELECT rowid, StatusEffectDuration, MaxStackSize "
                "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
                (reward_id,),
            ).fetchone()
            if status is None:
                continue
            definitions.append(
                CollectorEffectDefinition(
                    reward_id,
                    int(status["rowid"]) - 1,
                    1,
                    # Stickerbook rewards are derived from permanent
                    # collection progress. The database's one-second
                    # duration is only a template default. These entries use
                    # field_0c=0, which the client reserves for permanent
                    # effects, so they must also carry a zero duration rather
                    # than an inconsistent one-second timer.
                    0,
                )
            )
        return tuple(definitions)


def collector_effect_definitions(
    static_database_path: str | Path,
    owned_resource_ids: tuple[str, ...] | set[str],
) -> tuple[CollectorEffectDefinition, ...]:
    return _collector_effect_definitions_cached(
        Path(static_database_path),
        tuple(sorted(str(value) for value in owned_resource_ids)),
    )


@lru_cache(maxsize=8)
def _collector_effect_wire_ids_cached(
    static_database_path: Path,
) -> frozenset[int]:
    with _open_read_only(static_database_path) as database:
        reward_ids = set()
        for row in database.execute(
            "SELECT StickerbookRewardItems FROM _Template_Stickerbook "
            "WHERE StickerbookRewardItems IS NOT NULL"
        ):
            for encoded in _split(row[0]):
                parts = encoded.split(":")
                if len(parts) >= 2:
                    reward_ids.add(parts[1].strip())
        if not reward_ids:
            return frozenset()
        result = set()
        for reward_id in reward_ids:
            row = database.execute(
                "SELECT rowid FROM _Template_StatusEffect "
                "WHERE Id=? LIMIT 1",
                (reward_id,),
            ).fetchone()
            if row is not None:
                result.add(int(row["rowid"]) - 1)
        return frozenset(result)


def collector_effect_wire_ids(
    static_database_path: str | Path,
) -> frozenset[int]:
    return _collector_effect_wire_ids_cached(Path(static_database_path))


def reconcile_collector_effects(
    static_database_path: str | Path,
    owned_resource_ids: tuple[str, ...] | set[str],
    current_effects: dict[int, StatusEffectEntry],
    *,
    actor_id: int,
    next_instance_id: int,
    server_tick: int,
) -> CollectorEffectTransition:
    """Rebuild derived collector effects without touching unrelated buffs."""

    known_wire_ids = collector_effect_wire_ids(static_database_path)
    desired_by_wire: dict[int, list[CollectorEffectDefinition]] = {}
    for definition in collector_effect_definitions(
        static_database_path, owned_resource_ids
    ):
        desired_by_wire.setdefault(int(definition.wire_id), []).append(
            definition
        )
    effects = dict(current_effects)
    existing_by_wire: dict[int, list[StatusEffectEntry]] = {}
    for effect in effects.values():
        if int(effect.effect_id) in known_wire_ids:
            existing_by_wire.setdefault(int(effect.effect_id), []).append(effect)
    transitions: list[StatusEffectEntry] = []

    for wire_id in sorted(known_wire_ids | desired_by_wire.keys()):
        existing = sorted(
            existing_by_wire.get(wire_id, ()),
            key=lambda value: int(value.field_04),
        )
        desired = desired_by_wire.get(wire_id, ())
        keep_count = min(len(existing), len(desired))
        for previous in existing[keep_count:]:
            effects.pop(int(previous.field_04), None)
            transitions.append(replace(
                previous,
                field_10=0,
                field_18=0,
                field_25=False,
                field_39=False,
                has_details=False,
                values=(),
                field_38=False,
                field_3c=0xFF,
                shape_values=(),
            ))
        for previous, definition in zip(
            existing[:keep_count], desired[:keep_count]
        ):
            if (
                int(previous.field_10) == int(definition.duration_ticks)
                and int(previous.field_20) == int(actor_id)
            ):
                # Retained collector buffs are already live. Advancing only
                # their start tick and resending the same detailed iid is a
                # duplicate client-side ADD, not a refresh.
                continue
            effects.pop(int(previous.field_04), None)
            transitions.append(replace(
                previous,
                field_10=0,
                field_18=0,
                field_25=False,
                field_39=False,
                has_details=False,
                values=(),
                field_38=False,
                field_3c=0xFF,
                shape_values=(),
            ))
            next_instance_id = max(1, int(next_instance_id))
            replacement = StatusEffectEntry(
                effect_id=int(definition.wire_id),
                field_04=next_instance_id,
                field_0c=0,
                field_14=25,
                field_08=int(server_tick) & 0xFFFFFFFF,
                field_18=0,
                field_10=int(definition.duration_ticks),
                field_1c=100,
                field_20=int(actor_id),
                field_25=False,
                field_26=False,
                field_39=True,
                has_details=True,
                values=(),
            )
            effects[next_instance_id] = replacement
            transitions.append(replacement)
            next_instance_id += 1

        for definition in desired[keep_count:]:
            next_instance_id = max(1, int(next_instance_id))
            entry = StatusEffectEntry(
                effect_id=int(definition.wire_id),
                field_04=next_instance_id,
                field_0c=0,
                field_14=25,
                field_08=int(server_tick) & 0xFFFFFFFF,
                field_18=0,
                field_10=int(definition.duration_ticks),
                field_1c=100,
                field_20=int(actor_id),
                field_25=False,
                field_26=False,
                field_39=True,
                has_details=True,
                values=(),
            )
            effects[next_instance_id] = entry
            transitions.append(entry)
            next_instance_id += 1

    return CollectorEffectTransition(
        effects,
        next_instance_id,
        StatusEffect(False, tuple(transitions)) if transitions else None,
    )


def sticker_status_entry(
    definition: StickerDefinition,
    *,
    instance_id: int,
    start_tick: int,
    actor_id: int,
    active: bool,
) -> StatusEffectEntry:
    return StatusEffectEntry(
        effect_id=int(definition.status_effect_wire_id),
        field_04=int(instance_id),
        field_0c=0,
        field_14=0,
        field_08=int(start_tick) & 0xFFFFFFFF,
        field_18=0,
        field_10=int(definition.duration_ticks),
        field_1c=100,
        field_20=int(actor_id),
        field_25=False,
        field_26=False,
        field_39=True,
        has_details=bool(active),
        values=definition.values if active else (),
    )


def _remove_item_id(inventory: InventoryInfo, item_id: int) -> None:
    for mapping in (
        inventory.equipment_slots,
        inventory.storage_58,
        inventory.storage_80,
        inventory.storage_a8,
        inventory.storage_d0,
    ):
        mapping.pop(item_id, None)
    for values in (
        inventory.field_f8,
        inventory.field_118,
        inventory.field_180,
    ):
        while item_id in values:
            values.remove(item_id)


def _replace_item_id(
    inventory: InventoryInfo, old_item_id: int, new_item_id: int
) -> None:
    for mapping in (
        inventory.equipment_slots,
        inventory.storage_58,
        inventory.storage_80,
        inventory.storage_a8,
        inventory.storage_d0,
    ):
        if old_item_id in mapping:
            mapping[new_item_id] = mapping.pop(old_item_id)
    for values in (
        inventory.field_f8,
        inventory.field_118,
        inventory.field_180,
    ):
        for index, value in enumerate(values):
            if value == old_item_id:
                values[index] = new_item_id


def consume_sticker_inventory_item(
    source: InventoryInfo,
    item_id: int,
    *,
    next_item_id: int,
) -> StickerInventoryMutation:
    inventory = copy.deepcopy(source)
    item = next(
        (entry for entry in inventory.items if entry.item_id == int(item_id)),
        None,
    )
    if item is None:
        raise ValueError(f"sticker-book inventory item {item_id} is missing")
    changed_items: list[ItemInfo] = []
    replaced_ids = [int(item_id)]
    replacements: dict[int, int] = {}
    if item.amount <= 1:
        inventory.items.remove(item)
        _remove_item_id(inventory, int(item_id))
    else:
        replacement_id = int(next_item_id)
        next_item_id += 1
        replacement = replace(
            item,
            item_id=replacement_id,
            amount=item.amount - 1,
            field_18=replacement_id & 0xFFFF,
        )
        inventory.items[inventory.items.index(item)] = replacement
        _replace_item_id(inventory, int(item_id), replacement_id)
        changed_items.append(replacement)
        replacements[replacement_id] = int(item_id)

    inventory.changed_item_ids = []
    inventory.field_158 = {}
    inventory.field_1e0 = False
    delta = copy.deepcopy(inventory)
    delta.items = copy.deepcopy(changed_items)
    delta.changed_item_ids = replaced_ids
    delta.field_158 = replacements
    delta.field_1e0 = True
    final = copy.deepcopy(inventory)
    final.items = []
    return StickerInventoryMutation(
        inventory, delta, final, int(next_item_id)
    )
