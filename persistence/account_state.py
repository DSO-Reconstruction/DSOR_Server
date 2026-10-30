"""Durable, character-keyed state for the local gameplay services."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading

from dso_server.persistence.player_database import AsyncPlayerDatabase

from dso_server.protocol.currency_protocol import (
    named_game_currency_balances,
    normalize_game_currency_balances,
)
from dso_server.protocol.gameplay_protocol import StatusEffectEntry
from dso_server.protocol.inventory_codec import (
    InventoryInfo,
    ItemEnchantmentInfo,
    ItemInfo,
    RUNE_SLOT_ACHIEVEMENTS,
    RUNE_TRINKET_EXTRA_SLOTS,
    owned_inventory_items,
    release_locked_rune_trinkets,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.gameplay.local_mount_state import MountDefinition, MountEffectState, find_mount
from dso_server.protocol.membership_protocol import (
    DELUXE_MEMBERSHIP,
    default_deluxe_expiry,
    parse_membership_expiry,
)
from dso_server.gameplay.achievement_content import (
    AchievementProgress,
    TIMESTAMP_FORMAT,
    load_achievement_catalog,
)
from dso_server.protocol.talent_protocol import (
    ACTIVE_SKILL_TALENT_PRESELECTION,
    SKILL_TALENT_UNLOCKABLE_PRESELECTIONS,
)
from dso_server.protocol.player_init_protocol import (
    CurrencyChanged,
    QuickSlotInfo,
    QuickSlotsInfo,
    QuickbarInfo,
    StickerbookEntry,
    TalentCategorySelection,
    TalentSelectionEntry,
)


STARTER_EQUIPMENT_VERSION = 1
# Saved rune entitlements, by the slots they name. Saves used to call the first
# and second Divination Stone slots 18 and 19; the client's are 14 and 13.
_RUNE_ENTITLEMENT_SLOTS = {18: 14, 19: 13, 14: 14, 13: 13}


@dataclass(frozen=True)
class CharacterProfile:
    """Persistent roster metadata for characters created by the local server."""

    character_id: int
    name: str
    map_id: str
    class_id: int
    gender: int
    hair_style: int
    beard_style: int
    body_style: int
    body_stature: int
    level: int = 1
    premium_currency: int = 9_999_999
    # Initial gold is recorded with the character profile at creation time;
    # the first gameplay snapshot later takes over as the durable balance.
    gold: int = 0
    admin_privileges: bool = False
    admin_name_color: bool = False
    unknown_u32_a: int = 0
    progression_u64: int = 0
    progression_u32: int = 0
    transform: tuple[float, ...] = (
        -0.9999995231628418,
        6.894724435824173e-08,
        -4.235164736271502e-22,
        0.0,
        6.894724435824173e-08,
        1.0,
        6.894724435824173e-08,
        0.0,
        4.7537238122947415e-15,
        6.894724435824173e-08,
        -0.9999995231628418,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )
    flag_a: bool = False
    flag_b: bool = False
    quickbar_skill_slots: tuple[str | None, ...] | None = None

    @classmethod
    def from_character_record(
        cls, character, *, gold: int = 0
    ) -> "CharacterProfile":
        """Detach every durable roster field from a CharacterRecord."""

        return cls(
            character_id=character.character_id,
            name=character.name,
            map_id=character.map_id,
            class_id=character.class_id,
            gender=character.gender,
            hair_style=character.hair_style,
            beard_style=character.beard_style,
            body_style=character.body_style,
            body_stature=character.body_stature,
            level=character.level,
            premium_currency=character.premium_currency,
            gold=max(0, int(gold)),
            admin_privileges=character.admin_privileges,
            admin_name_color=character.admin_name_color,
            unknown_u32_a=character.unknown_u32_a,
            progression_u64=character.progression_u64,
            progression_u32=character.progression_u32,
            transform=tuple(character.transform),
            flag_a=character.flag_a,
            flag_b=character.flag_b,
            quickbar_skill_slots=(
                None
                if character.quickbar_skill_slots is None
                else tuple(character.quickbar_skill_slots)
            ),
        )


@dataclass
class AccountState:
    character_id: int
    map_id: str
    position: tuple[float, float, float] | None
    inventory: InventoryInfo
    currency: CurrencyChanged
    currency_ic: int
    next_item_id: int
    quickbar_info: QuickbarInfo
    timed_offer_purchases: dict[str, int]
    status_effects: tuple[StatusEffectEntry, ...]
    mount_ride: MountEffectState | None
    vendor_buyback: tuple[tuple[ItemInfo, int], ...] = ()
    admin_booking_ids: tuple[int, ...] = ()
    talent_categories: tuple[TalentCategorySelection, ...] = ()
    skill_talent_presets: dict[
        int, tuple[TalentSelectionEntry, ...]
    ] = field(default_factory=dict)
    category_talent_presets: dict[
        int, dict[int, tuple[TalentSelectionEntry, ...]]
    ] = field(default_factory=dict)
    event_progress: dict[str, dict[str, object]] = field(
        default_factory=dict
    )
    event_participation: dict[str, bool] = field(default_factory=dict)
    # Quest log: quest id -> QuestProgress document (gameplay/quest_content.py)
    quest_log: dict[str, dict[str, object]] = field(default_factory=dict)
    # Wanted board offers, remaining attempts, and reset/VIP timestamps.
    wanted_thief: dict[str, object] = field(default_factory=dict)
    # Season Pass level, experience, mission progress, and claimed reward IDs.
    season_pass: dict[str, object] = field(default_factory=dict)
    daily_challenges: dict[str, object] = field(default_factory=dict)
    # Achievements: achievement id -> {progress, completed_at}
    # (gameplay/achievement_content.py); completion stamps are written
    # once so a relog replays the same records without new popups.
    achievements: dict[str, dict[str, object]] = field(default_factory=dict)
    # Chapters whose ChapterUnlocked notification was already sent.
    chapters_unlocked: tuple[str, ...] = ()
    pvp_enabled: bool = False
    # Career PvP honor, the running total ``HonorPointsChangedCommand``
    # reports and the ranking commands sort on. Distinct from the badge of
    # honor currency, which is spendable.
    honor_points: int = 0
    pvp_matches_won: int = 0
    pvp_matches_lost: int = 0
    pvp_kills: int = 0
    pvp_deaths: int = 0
    honor_points_today: int = 0
    pvp_kills_today: int = 0
    pvp_solo_matches_won: int = 0
    pvp_solo_matches_played: int = 0
    pvp_team_matches_won: int = 0
    pvp_team_matches_played: int = 0
    pvp_kills_alone_total: int = 0
    pvp_kills_alone_today: int = 0
    pvp_kills_group_total: int = 0
    pvp_kills_group_today: int = 0
    pvp_kills_battle_total: int = 0
    pvp_kills_battle_today: int = 0
    pvp_dishonorable_kills: int = 0
    pvp_stats_date: str = ""
    title_type: int = 0
    achievement_title: str = ""
    saved_at: str = ""
    last_visited_town: str = ""
    unlocked_maps: tuple[str, ...] = ()
    admin_all_maps: bool = False
    admin_invulnerable: bool = False
    map_access_id: str = ""
    map_difficulty: int = 0
    scaling_difficulty: int = 0
    stickerbook_entries: tuple[StickerbookEntry, ...] = ()
    active_costume: str = ""
    # EquipmentSlotType -> skin resource hidden by the client's transmog UI.
    hidden_equipment_skins: dict[int, str] = field(default_factory=dict)
    # Skins collected from held equipment (gameplay/skin_collection.py);
    # only grows, and counts the UnlockSkin achievements.
    unlocked_skins: tuple[str, ...] = ()
    active_pet: str = ""
    active_mercenary: str = ""
    active_decorations: tuple[str, ...] = ()
    quick_slots: QuickSlotsInfo | None = None
    membership_type: int = DELUXE_MEMBERSHIP
    membership_expires_at: str = field(default_factory=default_deluxe_expiry)
    membership_active: bool = True
    game_currencies: dict[str, int] = field(
        default_factory=normalize_game_currency_balances
    )
    recipe_book_resources: tuple[str, ...] = ()
    rune_trinket_unlocked_slots: tuple[int, ...] = ()
    talent_category_points: tuple[int, int, int, int] = ()
    # Skill presets 5..8 bought with Andermant (UnlockSkillTalentPreselection).
    unlocked_skill_presets: int = 0
    # Wall-clock expiry (Unix seconds) for finite effects that survive map
    # handoffs and relogs. Effects absent from this map remain indefinite.
    timed_status_effect_expiries: dict[int, float] = field(
        default_factory=dict
    )
    # Instance id -> _Template_StatusEffect Id of each saved status effect.
    # Wire ids are row numbers that a client-data update can shift; login
    # re-resolves these names (gameplay/status_runtime_catalog.py).
    status_effect_names: dict[int, str] = field(default_factory=dict)
    # Player skill/category cooldowns and casting cadence use wall-clock
    # deadlines in saves, so reconnecting cannot bypass a live cooldown.
    player_skill_timing: dict[str, object] = field(default_factory=dict)
    # Version of the one-time starter-equipment repair. Once the initial
    # loadout has been reconciled, missing starter items are player changes
    # and must not be re-created on later logins.
    starter_equipment_version: int = 0


@dataclass(frozen=True)
class HonorRankingRecord:
    character_id: int
    name: str
    class_id: int
    honor_points: int = 0
    wins: int = 0
    losses: int = 0
    kills: int = 0
    deaths: int = 0


def _integer_map(values: dict) -> dict[int, int]:
    return {int(key): int(value) for key, value in values.items()}


def _item_to_document(item: ItemInfo) -> dict:
    return asdict(item)


def _item_from_document(document: dict) -> ItemInfo:
    values = dict(document)
    values["field_100"] = tuple(float(value) for value in values["field_100"])
    values["created_at"] = tuple(int(value) for value in values["created_at"])
    values["enchantments"] = [
        ItemEnchantmentInfo(**entry)
        for entry in values.get("enchantments", ())
    ]
    values["socketed_templates"] = [
        str(value) for value in values.get("socketed_templates", ())
    ]
    return ItemInfo(**values)


def _inventory_to_document(inventory: InventoryInfo) -> dict:
    return {
        "items": [_item_to_document(item) for item in inventory.items],
        "premium_inventory_overflow": [
            _item_to_document(item) for item in inventory.premium_inventory_overflow
        ],
        "premium_storage_overflow": [
            _item_to_document(item) for item in inventory.premium_storage_overflow
        ],
        "equipment_slots": {
            str(item_id): list(slots)
            for item_id, slots in inventory.equipment_slots.items()
        },
        "storage_58": inventory.storage_58,
        "storage_80": inventory.storage_80,
        "storage_a8": inventory.storage_a8,
        "storage_d0": inventory.storage_d0,
        "field_f8": inventory.field_f8,
        "field_118": inventory.field_118,
        "changed_item_ids": inventory.changed_item_ids,
        "field_158": inventory.field_158,
        "field_180": inventory.field_180,
        "field_1a0": inventory.field_1a0,
        "storage_upgrade": inventory.storage_upgrade,
        "bank_storage_upgrade": inventory.bank_storage_upgrade,
        "storage_size": inventory.storage_size,
        "bank_storage_size": inventory.bank_storage_size,
        "extra_bank_storage_size": inventory.extra_bank_storage_size,
        "extra_storage_size": inventory.extra_storage_size,
        "health_points": inventory.health_points,
        "secondary_resource": inventory.secondary_resource,
        "field_1e0": False,
        "actor_id": inventory.actor_id,
    }


def _inventory_from_document(document: dict) -> InventoryInfo:
    inventory = InventoryInfo(
        premium_inventory_overflow=[
            _item_from_document(item)
            for item in document.get("premium_inventory_overflow", ())
        ],
        premium_storage_overflow=[
            _item_from_document(item)
            for item in document.get("premium_storage_overflow", ())
        ],
        items=[
            _item_from_document(item)
            for item in document.get("items", ())
        ],
        equipment_slots={
            int(item_id): tuple(int(slot) for slot in slots)
            for item_id, slots in document.get(
                "equipment_slots", {}
            ).items()
        },
        storage_58=_integer_map(document.get("storage_58", {})),
        storage_80=_integer_map(document.get("storage_80", {})),
        storage_a8=_integer_map(document.get("storage_a8", {})),
        storage_d0=_integer_map(document.get("storage_d0", {})),
        field_f8=[int(value) for value in document.get("field_f8", ())],
        field_118=[int(value) for value in document.get("field_118", ())],
        changed_item_ids=[],
        field_158=_integer_map(document.get("field_158", {})),
        field_180=[int(value) for value in document.get("field_180", ())],
        field_1a0=[str(value) for value in document.get("field_1a0", ())],
        storage_upgrade=int(document.get("storage_upgrade", 30)),
        bank_storage_upgrade=int(
            document.get("bank_storage_upgrade", 53)
        ),
        storage_size=int(document.get("storage_size", 224)),
        bank_storage_size=int(document.get("bank_storage_size", 270)),
        extra_bank_storage_size=int(
            document.get("extra_bank_storage_size", 0)
        ),
        extra_storage_size=int(document.get("extra_storage_size", 0)),
        health_points=float(document.get("health_points", 1.0)),
        secondary_resource=float(document.get("secondary_resource", 0.0)),
        field_1e0=False,
        actor_id=int(document.get("actor_id", 1)),
    )
    _validate_inventory(inventory)
    return inventory


def _validate_inventory(inventory: InventoryInfo) -> None:
    item_ids = [int(item.item_id) for item in (
        *inventory.items, *inventory.premium_inventory_overflow,
        *inventory.premium_storage_overflow,
    )]
    if any(item_id <= 0 for item_id in item_ids):
        raise ValueError("persisted inventory contains a non-positive item id")
    if len(item_ids) != len(set(item_ids)):
        raise ValueError("persisted inventory contains duplicate item ids")
    known = {int(item.item_id) for item in inventory.items}
    for label, values in (
        ("equipment", inventory.equipment_slots),
        ("storage_58", inventory.storage_58),
        ("storage_80", inventory.storage_80),
        ("storage_a8", inventory.storage_a8),
        ("storage_d0", inventory.storage_d0),
    ):
        unknown = set(values) - known
        if unknown:
            raise ValueError(
                f"persisted {label} references unknown item ids: "
                + ", ".join(str(value) for value in sorted(unknown))
            )


def _status_from_document(document: dict) -> StatusEffectEntry:
    values = dict(document)
    values["values"] = tuple(float(value) for value in values.get("values", ()))
    values["shape_values"] = tuple(
        float(value) for value in values.get("shape_values", ())
    )
    return StatusEffectEntry(**values)


def _talent_categories_to_document(
    categories: tuple[TalentCategorySelection, ...],
) -> list[dict]:
    return [
        {
            "preselection": int(category.preselection),
            "selected_skills": [
                asdict(entry) for entry in category.selected_skills
            ],
            "selected_talents": [
                asdict(entry) for entry in category.selected_talents
            ],
        }
        for category in categories
    ]


def _talent_categories_from_document(
    document: list[dict] | tuple[dict, ...],
) -> tuple[TalentCategorySelection, ...]:
    if not document:
        return (
            TalentCategorySelection(
                ACTIVE_SKILL_TALENT_PRESELECTION, (), ()
            ),
            TalentCategorySelection(0, (), ()),
            TalentCategorySelection(0, (), ()),
            TalentCategorySelection(0, (), ()),
        )
    if len(document) != 4:
        raise ValueError("persisted talent snapshot must have four categories")
    max_presets = (8, 4, 4, 1)
    categories = []
    for category_id, raw_category in enumerate(document):
        preselection = int(raw_category.get("preselection", 0))
        if not 0 <= preselection < max_presets[category_id]:
            raise ValueError(
                f"persisted talent category {category_id} has invalid "
                f"preselection {preselection}"
            )
        selected_skills = tuple(
            TalentSelectionEntry(
                int(entry["field_00"]),
                int(entry.get("field_04", 0)),
                str(entry["resource_id"]),
            )
            for entry in raw_category.get("selected_skills", ())
        )
        selected_talents = tuple(
            TalentSelectionEntry(
                int(entry["field_00"]),
                int(entry.get("field_04", 0)),
                str(entry["resource_id"]),
            )
            for entry in raw_category.get("selected_talents", ())
        )
        if any(
            not entry.resource_id or not 0 < entry.field_00 <= 0xFFFF
            for entry in selected_skills + selected_talents
        ):
            raise ValueError(
                f"persisted talent category {category_id} has invalid entries"
            )
        categories.append(
            TalentCategorySelection(
                preselection, selected_skills, selected_talents
            )
        )
    return tuple(categories)


def _skill_talent_presets_to_document(
    presets: dict[int, tuple[TalentSelectionEntry, ...]],
) -> dict[str, list[dict]]:
    return {
        str(int(preselection)): [asdict(entry) for entry in entries]
        for preselection, entries in sorted(presets.items())
    }


def _skill_talent_presets_from_document(
    document: dict | None,
    categories: tuple[TalentCategorySelection, ...],
) -> dict[int, tuple[TalentSelectionEntry, ...]]:
    if not document:
        active = categories[0]
        entries = active.selected_skills + active.selected_talents
        return {int(active.preselection): entries}
    presets: dict[int, tuple[TalentSelectionEntry, ...]] = {}
    for raw_preselection, raw_entries in document.items():
        preselection = int(raw_preselection)
        if not 0 <= preselection < 8:
            raise ValueError(
                f"persisted skill-talent preset {preselection} is invalid"
            )
        entries = tuple(
            TalentSelectionEntry(
                int(entry["field_00"]),
                int(entry.get("field_04", 0)),
                str(entry["resource_id"]),
            )
            for entry in raw_entries
        )
        if any(
            not entry.resource_id or not 0 < entry.field_00 <= 0xFFFF
            for entry in entries
        ):
            raise ValueError(
                f"persisted skill-talent preset {preselection} "
                "has invalid entries"
            )
        presets[preselection] = entries
    active = categories[0]
    presets.setdefault(
        int(active.preselection),
        active.selected_skills + active.selected_talents,
    )
    return presets


def _category_talent_presets_to_document(
    presets: dict[int, dict[int, tuple[TalentSelectionEntry, ...]]],
) -> dict[str, dict[str, list[dict]]]:
    return {
        str(int(category)): _skill_talent_presets_to_document(layouts)
        for category, layouts in sorted(presets.items())
        if int(category) in (1, 2, 3)
    }


def _category_talent_presets_from_document(
    document: dict | None,
    categories: tuple[TalentCategorySelection, ...],
) -> dict[int, dict[int, tuple[TalentSelectionEntry, ...]]]:
    maximums = (8, 4, 4, 1)
    result: dict[int, dict[int, tuple[TalentSelectionEntry, ...]]] = {}
    for category in (1, 2, 3):
        active = categories[category]
        raw_layouts = (document or {}).get(str(category), {})
        layouts: dict[int, tuple[TalentSelectionEntry, ...]] = {}
        for raw_preselection, raw_entries in raw_layouts.items():
            preselection = int(raw_preselection)
            if not 0 <= preselection < maximums[category]:
                raise ValueError(
                    f"persisted talent category {category} preset "
                    f"{preselection} is invalid"
                )
            entries = tuple(
                TalentSelectionEntry(
                    int(entry["field_00"]),
                    int(entry.get("field_04", 0)),
                    str(entry["resource_id"]),
                )
                for entry in raw_entries
            )
            if any(
                not entry.resource_id or not 0 < entry.field_00 <= 0xFFFF
                for entry in entries
            ):
                raise ValueError(
                    f"persisted talent category {category} preset "
                    f"{preselection} has invalid entries"
                )
            layouts[preselection] = entries
        layouts.setdefault(
            int(active.preselection),
            active.selected_skills,
        )
        result[category] = layouts
    return result


def _state_to_document(state: AccountState) -> dict:
    mount = None
    if state.mount_ride is not None:
        mount = {
            "resource_id": state.mount_ride.definition.stickerbook_resource_id,
            "effect_id": state.mount_ride.effect_id,
            "instance_id": state.mount_ride.instance_id,
            "start_tick": state.mount_ride.start_tick,
            "duration_ticks": state.mount_ride.duration_ticks,
            "definition": asdict(state.mount_ride.definition),
        }
    return {
        "character_id": int(state.character_id),
        "admin_booking_ids": list(state.admin_booking_ids),
        "map_id": str(state.map_id),
        "last_visited_town": str(state.last_visited_town),
        "unlocked_maps": list(state.unlocked_maps),
        "admin_all_maps": bool(state.admin_all_maps),
        "admin_invulnerable": bool(state.admin_invulnerable),
        "map_access_id": str(state.map_access_id or state.map_id),
        "map_difficulty": int(state.map_difficulty),
        "scaling_difficulty": int(state.scaling_difficulty),
        "position": (
            list(state.position) if state.position is not None else None
        ),
        "inventory": _inventory_to_document(state.inventory),
        "starter_equipment_version": int(state.starter_equipment_version),
        "vendor_buyback": [
            {"item": _item_to_document(item), "price": price}
            for item, price in state.vendor_buyback
        ],
        "currencies": {
            "andermant": int(state.currency.andermant),
            "gold": int(state.currency.gold),
            "badge_of_honor": int(state.currency.badge_of_honor),
            "legacy_progression": int(
                state.currency.legacy_progression
            ),
            "draken": int(state.currency.draken),
            "glyph_of_power": int(state.currency.glyph_of_power),
            "ic": int(state.currency_ic),
            "game": named_game_currency_balances(
                normalize_game_currency_balances(state.game_currencies)
            ),
        },
        "next_item_id": int(state.next_item_id),
        "quickbar": {
            "amount": int(state.quickbar_info.quickbar_amount),
            "locked": int(state.quickbar_info.locked_quickbars),
        },
        "quick_slots": (
            [
                [
                    {
                        "kind": int(slot.kind),
                        "resource_id": slot.resource_id,
                    }
                    for slot in quickbar
                ]
                for quickbar in state.quick_slots.quickbars
            ]
            if state.quick_slots is not None
            else None
        ),
        "timed_offer_purchases": {
            str(key): int(value)
            for key, value in state.timed_offer_purchases.items()
        },
        "event_progress": {
            str(key): dict(value)
            for key, value in state.event_progress.items()
        },
        "quest_log": {
            str(key): dict(value)
            for key, value in state.quest_log.items()
        },
        "wanted_thief": copy.deepcopy(state.wanted_thief),
        "season_pass": copy.deepcopy(state.season_pass),
        "daily_challenges": copy.deepcopy(state.daily_challenges),
        "achievements": {
            str(key): dict(value)
            for key, value in state.achievements.items()
        },
        "chapters_unlocked": [
            str(value) for value in state.chapters_unlocked if str(value)
        ],
        "event_participation": {
            str(key): bool(value)
            for key, value in state.event_participation.items()
        },
        "active_status_effects": [
            asdict(effect) for effect in state.status_effects
        ],
        "status_effect_names": {
            str(instance_id): str(name)
            for instance_id, name in state.status_effect_names.items()
        },
        "player_skill_timing": copy.deepcopy(state.player_skill_timing),
        "timed_status_effect_expiries": {
            str(instance_id): float(expires_at)
            for instance_id, expires_at
            in state.timed_status_effect_expiries.items()
        },
        "talent_categories": _talent_categories_to_document(
            state.talent_categories
        ),
        "skill_talent_presets": _skill_talent_presets_to_document(
            state.skill_talent_presets
        ),
        "category_talent_presets": _category_talent_presets_to_document(
            state.category_talent_presets
        ),
        "mount_ride": mount,
        "stickerbook_entries": [
            asdict(entry) for entry in state.stickerbook_entries
        ],
        "active_costume": str(state.active_costume),
        "hidden_equipment_skins": {
            str(int(slot)): str(skin)
            for slot, skin in state.hidden_equipment_skins.items()
            if str(skin)
        },
        "unlocked_skins": sorted({
            str(skin) for skin in state.unlocked_skins if str(skin)
        }),
        "active_pet": str(state.active_pet),
        "active_mercenary": str(state.active_mercenary),
        "active_decorations": list(state.active_decorations),
        "pvp_enabled": bool(state.pvp_enabled),
        "honor_points": int(state.honor_points),
        "pvp_matches_won": int(state.pvp_matches_won),
        "pvp_matches_lost": int(state.pvp_matches_lost),
        "pvp_kills": int(state.pvp_kills),
        "pvp_deaths": int(state.pvp_deaths),
        "honor_points_today": int(state.honor_points_today),
        "pvp_kills_today": int(state.pvp_kills_today),
        "pvp_solo_matches_won": int(state.pvp_solo_matches_won),
        "pvp_solo_matches_played": int(state.pvp_solo_matches_played),
        "pvp_team_matches_won": int(state.pvp_team_matches_won),
        "pvp_team_matches_played": int(state.pvp_team_matches_played),
        "pvp_kills_alone_total": int(state.pvp_kills_alone_total),
        "pvp_kills_alone_today": int(state.pvp_kills_alone_today),
        "pvp_kills_group_total": int(state.pvp_kills_group_total),
        "pvp_kills_group_today": int(state.pvp_kills_group_today),
        "pvp_kills_battle_total": int(state.pvp_kills_battle_total),
        "pvp_kills_battle_today": int(state.pvp_kills_battle_today),
        "pvp_dishonorable_kills": int(state.pvp_dishonorable_kills),
        "pvp_stats_date": str(state.pvp_stats_date),
        "title_type": int(state.title_type),
        "achievement_title": str(state.achievement_title),
        "membership": {
            "type": int(state.membership_type),
            "expires_at": str(state.membership_expires_at),
            "active": bool(state.membership_active),
        },
        "recipe_book_resources": list(state.recipe_book_resources),
        "rune_trinket_unlocked_slots": list(state.rune_trinket_unlocked_slots),
        "talent_category_points": list(state.talent_category_points),
        "unlocked_skill_presets": int(state.unlocked_skill_presets),
        "saved_at": state.saved_at,
    }


def _state_from_document(document: dict) -> AccountState:
    currencies = document["currencies"]
    membership_document = document.get("membership", {})
    mount_document = document.get("mount_ride")
    mount = None
    if mount_document is not None:
        definition = find_mount(str(mount_document["resource_id"]))
        definition_document = mount_document.get("definition")
        if definition is None and definition_document is not None:
            definition = MountDefinition(
                stickerbook_resource_id=str(
                    definition_document["stickerbook_resource_id"]
                ),
                mount_resource_id=str(
                    definition_document["mount_resource_id"]
                ),
                cast_effect_id=int(definition_document["cast_effect_id"]),
                ride_effect_id=int(definition_document["ride_effect_id"]),
                cast_duration_seconds=float(
                    definition_document["cast_duration_seconds"]
                ),
                cast_duration_ticks=int(
                    definition_document["cast_duration_ticks"]
                ),
                ride_duration_ticks=int(
                    definition_document["ride_duration_ticks"]
                ),
                stickerbook_state=int(
                    definition_document["stickerbook_state"]
                ),
                stickerbook_favorite_position=int(
                    definition_document["stickerbook_favorite_position"]
                ),
            )
        if definition is None:
            raise ValueError(
                f"persisted mount {mount_document['resource_id']!r} is unknown"
            )
        mount = MountEffectState(
            definition=definition,
            effect_id=int(mount_document["effect_id"]),
            instance_id=int(mount_document["instance_id"]),
            start_tick=int(mount_document["start_tick"]),
            duration_ticks=int(mount_document["duration_ticks"]),
        )
        if mount.effect_id != definition.ride_effect_id:
            raise ValueError("persisted mount state is not a ride effect")
    position_document = document.get("position")
    position = (
        tuple(float(value) for value in position_document)
        if position_document is not None
        else None
    )
    if position is not None and len(position) != 3:
        raise ValueError("persisted position must contain three floats")
    talent_categories = _talent_categories_from_document(
        document.get("talent_categories", ())
    )
    quick_slots_document = document.get("quick_slots")
    quick_slots = (
        QuickSlotsInfo(
            tuple(
                tuple(
                    QuickSlotInfo(
                        int(slot["kind"]),
                        (
                            None
                            if slot.get("resource_id") is None
                            else str(slot["resource_id"])
                        ),
                    )
                    for slot in quickbar
                )
                for quickbar in quick_slots_document
            )
        )
        if quick_slots_document is not None
        else None
    )
    state = AccountState(
        character_id=int(document["character_id"]),
        admin_booking_ids=tuple(int(value) for value in document.get("admin_booking_ids", ())),
        map_id=str(document["map_id"]),
        last_visited_town=str(document.get("last_visited_town", "")),
        unlocked_maps=tuple(str(value) for value in document.get("unlocked_maps", ())),
        admin_all_maps=document.get("admin_all_maps") is True,
        admin_invulnerable=document.get("admin_invulnerable") is True,
        position=position,
        inventory=_inventory_from_document(document["inventory"]),
        starter_equipment_version=max(
            0, int(document.get("starter_equipment_version", 0))
        ),
        vendor_buyback=tuple(
            (_item_from_document(entry["item"]), max(0, int(entry["price"])))
            for entry in document.get("vendor_buyback", ())
        ),
        currency=CurrencyChanged(
            int(currencies.get("andermant", currencies.get("rc", 0))),
            int(currencies.get("gold", currencies.get("vc", 0))),
            (
                int(
                    currencies.get(
                        "badge_of_honor", currencies.get("gc", 0)
                    )
                ),
                int(
                    currencies.get(
                        "legacy_progression", currencies.get("ec", 0)
                    )
                ),
                int(currencies.get("draken", currencies.get("uc", 0))),
            ),
            int(
                currencies.get(
                    "glyph_of_power", currencies.get("arena", 0)
                )
            ),
            False,
        ),
        currency_ic=int(currencies.get("ic", 0)),
        next_item_id=int(document["next_item_id"]),
        quickbar_info=QuickbarInfo(
            int(document.get("quickbar", {}).get("amount", 1)),
            int(document.get("quickbar", {}).get("locked", 0)),
        ),
        timed_offer_purchases={
            str(key): int(value)
            for key, value in document.get(
                "timed_offer_purchases", {}
            ).items()
        },
        event_progress={
            str(key): dict(value)
            for key, value in document.get("event_progress", {}).items()
            if isinstance(value, dict)
        },
        quest_log={
            str(key): dict(value)
            for key, value in document.get("quest_log", {}).items()
            if isinstance(value, dict)
        },
        wanted_thief=copy.deepcopy(document.get("wanted_thief", {})),
        season_pass=copy.deepcopy(document.get("season_pass", {})),
        daily_challenges=copy.deepcopy(document.get("daily_challenges", {})),
        achievements={
            str(key): dict(value)
            for key, value in document.get("achievements", {}).items()
            if isinstance(value, dict)
        },
        chapters_unlocked=tuple(dict.fromkeys(
            str(value)
            for value in document.get("chapters_unlocked", ())
            if str(value)
        )),
        event_participation={
            str(key): bool(value)
            for key, value in document.get(
                "event_participation", {}
            ).items()
        },
        status_effects=tuple(
            _status_from_document(effect)
            for effect in document.get("active_status_effects", ())
        ),
        status_effect_names={
            int(instance_id): str(name)
            for instance_id, name in (
                document.get("status_effect_names") or {}
            ).items()
        },
        mount_ride=mount,
        talent_categories=talent_categories,
        skill_talent_presets=_skill_talent_presets_from_document(
            document.get("skill_talent_presets"),
            talent_categories,
        ),
        category_talent_presets=_category_talent_presets_from_document(
            document.get("category_talent_presets"),
            talent_categories,
        ),
        pvp_enabled=bool(document.get("pvp_enabled", False)),
        honor_points=max(0, int(document.get("honor_points", 0))),
        pvp_matches_won=max(0, int(document.get("pvp_matches_won", 0))),
        pvp_matches_lost=max(0, int(document.get("pvp_matches_lost", 0))),
        pvp_kills=max(0, int(document.get("pvp_kills", 0))),
        pvp_deaths=max(0, int(document.get("pvp_deaths", 0))),
        honor_points_today=max(0, int(document.get("honor_points_today", 0))),
        pvp_kills_today=max(0, int(document.get("pvp_kills_today", 0))),
        pvp_solo_matches_won=max(0, int(document.get("pvp_solo_matches_won", document.get("pvp_matches_won", 0)))),
        pvp_solo_matches_played=max(0, int(document.get("pvp_solo_matches_played", int(document.get("pvp_matches_won", 0)) + int(document.get("pvp_matches_lost", 0))))),
        pvp_team_matches_won=max(0, int(document.get("pvp_team_matches_won", 0))),
        pvp_team_matches_played=max(0, int(document.get("pvp_team_matches_played", 0))),
        pvp_kills_alone_total=max(0, int(document.get("pvp_kills_alone_total", document.get("pvp_kills", 0)))),
        pvp_kills_alone_today=max(0, int(document.get("pvp_kills_alone_today", document.get("pvp_kills_today", 0)))),
        pvp_kills_group_total=max(0, int(document.get("pvp_kills_group_total", 0))),
        pvp_kills_group_today=max(0, int(document.get("pvp_kills_group_today", 0))),
        pvp_kills_battle_total=max(0, int(document.get("pvp_kills_battle_total", document.get("pvp_kills", 0)))),
        pvp_kills_battle_today=max(0, int(document.get("pvp_kills_battle_today", document.get("pvp_kills_today", 0)))),
        pvp_dishonorable_kills=max(0, int(document.get("pvp_dishonorable_kills", 0))),
        pvp_stats_date=str(document.get("pvp_stats_date", "")),
        title_type=max(0, min(2, int(document.get("title_type", 0)))),
        achievement_title=str(document.get("achievement_title", "")),
        saved_at=str(document.get("saved_at", "")),
        map_access_id=str(
            document.get("map_access_id", document["map_id"])
        ),
        map_difficulty=int(document.get("map_difficulty", 0)),
        scaling_difficulty=int(
            document.get("scaling_difficulty", 0)
        ),
        stickerbook_entries=tuple(
            StickerbookEntry(
                int(entry.get("field_14", 2)),
                int(entry.get("field_18", 0)),
                str(entry["resource_id"]),
            )
            for entry in document.get("stickerbook_entries", ())
            if str(entry.get("resource_id", ""))
        ),
        active_costume=str(document.get("active_costume", "")),
        hidden_equipment_skins={
            int(slot): str(skin)
            for slot, skin in document.get(
                "hidden_equipment_skins", {}
            ).items()
            if str(skin)
        },
        unlocked_skins=tuple(sorted({
            str(skin) for skin in document.get("unlocked_skins", ()) if str(skin)
        })),
        active_pet=str(document.get("active_pet", "")),
        active_mercenary=str(document.get("active_mercenary", "")),
        active_decorations=tuple(
            str(value) for value in document.get("active_decorations", ()) if value
        ),
        quick_slots=quick_slots,
        membership_type=int(
            membership_document.get("type", DELUXE_MEMBERSHIP)
        ),
        membership_expires_at=str(
            membership_document.get(
                "expires_at",
                default_deluxe_expiry(),
            )
        ),
        membership_active=bool(
            membership_document.get("active", True)
        ),
        game_currencies=normalize_game_currency_balances(
            currencies.get("game", {})
        ),
        recipe_book_resources=tuple(dict.fromkeys(
            str(value)
            for value in document.get("recipe_book_resources", ())
            if str(value)
        )),
        rune_trinket_unlocked_slots=tuple(sorted({
            _RUNE_ENTITLEMENT_SLOTS[int(value)]
            for value in document.get("rune_trinket_unlocked_slots", ())
            if int(value) in _RUNE_ENTITLEMENT_SLOTS
        })),
        talent_category_points=tuple(
            max(0, int(value))
            for value in document.get("talent_category_points", ())
        ),
        unlocked_skill_presets=min(
            SKILL_TALENT_UNLOCKABLE_PRESELECTIONS,
            max(0, int(document.get("unlocked_skill_presets", 0))),
        ),
        timed_status_effect_expiries={
            int(instance_id): float(expires_at)
            for instance_id, expires_at in document.get(
                "timed_status_effect_expiries", {}
            ).items()
        },
        player_skill_timing=copy.deepcopy(document.get("player_skill_timing", {})),
    )
    if state.talent_category_points and len(state.talent_category_points) != 4:
        raise ValueError("persisted talent category points require four values")
    if state.character_id <= 0 or not state.map_id:
        raise ValueError("persisted account state has an invalid identity")
    if state.next_item_id <= 0:
        raise ValueError("persisted next item id must be positive")
    if (
        state.map_difficulty < 0
        or state.scaling_difficulty < 0
    ):
        raise ValueError("persisted map difficulty cannot be negative")
    if state.membership_type not in (0, 1, 2):
        raise ValueError("persisted membership type is invalid")
    parse_membership_expiry(state.membership_expires_at)
    release_locked_rune_trinkets(state.inventory, {
        *state.rune_trinket_unlocked_slots,
        *(
            slot for slot, key in zip(RUNE_TRINKET_EXTRA_SLOTS, RUNE_SLOT_ACHIEVEMENTS)
            if isinstance(state.achievements.get(key), dict)
            and AchievementProgress.from_document(state.achievements[key]).completed
        ),
    })
    return state


def _profile_to_document(profile: CharacterProfile) -> dict:
    return asdict(profile)


def _profile_from_document(document: dict) -> CharacterProfile:
    transform = tuple(
        float(value)
        for value in document.get(
            "transform",
            CharacterProfile.__dataclass_fields__["transform"].default,
        )
    )
    raw_quickbar = document.get("quickbar_skill_slots")
    quickbar_skill_slots = (
        None
        if raw_quickbar is None
        else tuple(
            None if value is None else str(value)
            for value in raw_quickbar
        )
    )
    profile = CharacterProfile(
        character_id=int(document["character_id"]),
        name=str(document["name"]),
        map_id=str(document["map_id"]),
        class_id=int(document["class_id"]),
        gender=int(document["gender"]),
        hair_style=int(document["hair_style"]),
        beard_style=int(document["beard_style"]),
        body_style=int(document["body_style"]),
        body_stature=int(document["body_stature"]),
        level=int(document.get("level", 1)),
        premium_currency=int(document.get("premium_currency", 9_999_999)),
        gold=max(0, int(document.get("gold", 0))),
        admin_privileges=bool(document.get("admin_privileges", False)),
        admin_name_color=bool(document.get("admin_name_color", False)),
        unknown_u32_a=int(document.get("unknown_u32_a", 0)),
        progression_u64=int(document.get("progression_u64", 0)),
        progression_u32=int(document.get("progression_u32", 0)),
        transform=transform,
        flag_a=bool(document.get("flag_a", False)),
        flag_b=bool(document.get("flag_b", False)),
        quickbar_skill_slots=quickbar_skill_slots,
    )
    if profile.character_id <= 0:
        raise ValueError("created character id must be positive")
    if not profile.name or not profile.map_id:
        raise ValueError("created character name and map must not be empty")
    if not 0 <= profile.class_id <= 4:
        raise ValueError("created character class id is invalid")
    if profile.gender < 0 or profile.gender > 0xFF:
        raise ValueError("created character gender is invalid")
    for value in (
        profile.hair_style,
        profile.beard_style,
        profile.body_style,
        profile.body_stature,
    ):
        if value < 0 or value > 0xFF:
            raise ValueError("created character appearance value is invalid")
    if profile.level <= 0 or profile.premium_currency < 0:
        raise ValueError("created character progression is invalid")
    if len(profile.transform) != 16:
        raise ValueError("created character transform requires 16 values")
    if profile.quickbar_skill_slots is not None and len(
        profile.quickbar_skill_slots
    ) > 8:
        raise ValueError("created character quickbar exceeds eight slots")
    return profile


class AccountStateStore:
    """Thread-safe in-memory state backed by asynchronous SQLite commits.

    put and put_profile detach and publish snapshots immediately. A single
    writer coalesces repeated updates for each character, which keeps packet
    handlers and the periodic snapshot loop off the disk-I/O path. flush is
    intentionally reserved for startup migrations, tests, and shutdown.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        static_database_path: str | Path = DEFAULT_STATIC_DB_PATH,
    ) -> None:
        self.path = Path(path)
        self.lock = threading.RLock()
        # Serialize read/evaluate/reward/save across characters on an account.
        # Never acquire this from inside flow_lock or the general store lock.
        self.achievement_lock = threading.RLock()
        self.states: dict[int, AccountState] = {}
        self.character_profiles: dict[int, CharacterProfile] = {}
        self.warnings: list[str] = []
        self._database = AsyncPlayerDatabase(
            self.path,
            encode_state=_state_to_document,
            encode_profile=_profile_to_document,
        )
        raw_states, raw_profiles = self._database.load_documents()
        self.warnings.extend(self._database.warnings)
        self._load_database_documents(raw_states, raw_profiles)
        self._complete_rune_slot_achievements(static_database_path)
        self.account_registry = None
        self.account_wallets = self._database.load_wallets()
        self.account_achievements = self._database.load_account_achievements()

    def _complete_rune_slot_achievements(
        self, static_database_path: str | Path
    ) -> None:
        """Complete the achievement behind every saved RuneTrinket unlock.

        The client unlocks these slots from the achievement alone. The slots
        themselves stay as the player left them: nothing is placed in them.
        """

        path = Path(static_database_path)
        if not self.states or not path.is_file():
            return
        try:
            achievement_catalog = load_achievement_catalog(path)
        except (OSError, sqlite3.Error) as exc:
            self.warnings.append(
                f"could not check RuneTrinket slot achievements: {exc}"
            )
            return
        unlock_achievements = tuple(
            (slot, achievement_catalog.achievement(achievement_id))
            for slot, achievement_id
            in zip(RUNE_TRINKET_EXTRA_SLOTS, RUNE_SLOT_ACHIEVEMENTS)
        )
        if any(definition is None for _slot, definition in unlock_achievements):
            self.warnings.append(
                "RuneTrinket slot unlock achievements are missing from the "
                "static database"
            )
        migrated = []
        for character_id, state in self.states.items():
            changed = False
            for slot, definition in unlock_achievements:
                if definition is None or slot not in state.rune_trinket_unlocked_slots:
                    continue
                document = state.achievements.get(definition.key)
                if (
                    isinstance(document, dict)
                    and AchievementProgress.from_document(document).completed
                ):
                    continue
                state.achievements[definition.key] = AchievementProgress(
                    progress=definition.amount,
                    completed_at=datetime.now(timezone.utc).strftime(
                        TIMESTAMP_FORMAT
                    ),
                ).to_document()
                changed = True
            if changed:
                migrated.append((character_id, state))

        for character_id, state in migrated:
            self._database.queue_state(character_id, state)
        if migrated:
            self._database.flush()
            print(
                "[PlayerDatabase] completed RuneTrinket slot achievements "
                f"for {len(migrated)} saved character(s)"
            )

    def _load_database_documents(
        self,
        raw_states: dict[int, dict],
        raw_profiles: dict[int, dict],
    ) -> None:
        for character_id, document in raw_states.items():
            try:
                state = _state_from_document(document)
                if state.character_id != character_id:
                    raise ValueError(
                        "database key and character id differ"
                    )
                self.states[character_id] = state
            except (KeyError, TypeError, ValueError) as exc:
                self.warnings.append(
                    f"ignored SQLite state {character_id}: {exc}"
                )
        for character_id, document in raw_profiles.items():
            try:
                profile = _profile_from_document(document)
                if profile.character_id != character_id:
                    raise ValueError(
                        "database key and character id differ"
                    )
                self.character_profiles[character_id] = profile
            except (KeyError, TypeError, ValueError) as exc:
                self.warnings.append(
                    f"ignored SQLite profile {character_id}: {exc}"
                )

    def _wallet_owner(self, character_id: int, fallback: int = 0):
        registry = self.account_registry
        if registry is None:
            return None
        ownership = registry.wallet_owner(int(character_id))
        if ownership is None:
            return None
        account_id, character_ids = ownership
        if account_id not in self.account_wallets:
            # One-time migration: use each character's durable balance (profile
            # only if it has never entered a map). Do not add starting grants.
            balances = [int(self.states[cid].currency.andermant)
                        if cid in self.states else int(self.character_profiles[cid].premium_currency)
                         for cid in character_ids
                        if cid in self.states or cid in self.character_profiles]
            self.account_wallets[account_id] = max(balances, default=max(0, int(fallback)))
            self._database.queue_wallet(account_id, self.account_wallets[account_id])
        return account_id

    def currency_for_character(self, character_id: int, currency: CurrencyChanged) -> CurrencyChanged:
        with self.lock:
            owner = self._wallet_owner(character_id, currency.andermant)
            if owner is None:
                return currency
            balance = self.account_wallets[owner]
            return replace(currency, currency_type=balance, wallet_base=balance)

    def commit_currency(self, character_id: int, currency: CurrencyChanged) -> CurrencyChanged:
        """Apply a delta to the shared wallet, never a stale absolute balance."""
        with self.lock:
            owner = self._wallet_owner(character_id, currency.andermant)
            if owner is None:
                return currency
            base = currency.wallet_base
            delta = int(currency.andermant) - int(base) if base is not None else 0
            balance = self.account_wallets[owner] + delta
            if not 0 <= balance <= 0xFFFFFFFF:
                raise ValueError("insufficient account Andermant or wallet exceeds wire range")
            self.account_wallets[owner] = balance
            return replace(currency, currency_type=balance, wallet_base=balance)

    def get(self, character_id: int) -> AccountState | None:
        with self.lock:
            state = self.states.get(int(character_id))
            result = copy.deepcopy(state) if state is not None else None
            if result is not None:
                result.currency = self.currency_for_character(character_id, result.currency)
            return result

    def achievement_snapshot(self, character_id: int, catalog) -> dict[str, dict]:
        """Shared progress, migrating old character copies without adding them.

        Copies historically repeated the same accomplishments on relog. Take
        their high-water value once; subsequent events use the shared document.
        The first achievement save persists this migration with its character.
        """
        with self.lock:
            ownership = (self.account_registry.wallet_owner(int(character_id))
                         if self.account_registry is not None else None)
            if ownership is None:
                return {}
            owner, character_ids = ownership
            if owner in self.account_achievements:
                return copy.deepcopy(self.account_achievements[owner])
            merged = {}
            for identity in character_ids:
                state = self.states.get(identity)
                for key, document in (state.achievements.items() if state else ()):
                    definition = catalog.achievement(key)
                    if definition is None or not definition.account_wide:
                        continue
                    candidate = AchievementProgress.from_document(document)
                    previous = merged.get(definition.key)
                    if previous is None or (candidate.completed, candidate.progress) > (
                            previous.completed, previous.progress):
                        merged[definition.key] = candidate
                    elif candidate.reward_status == "granted":
                        previous.reward_status = "granted"
            return {key: progress.to_document() for key, progress in merged.items()}

    def saved_at_for_character(self, character_id: int) -> str:
        """Read only a character's activity timestamp without copying its state."""
        with self.lock:
            state = self.states.get(int(character_id))
            return str(state.saved_at or "") if state is not None else ""

    def get_profile(self, character_id: int) -> CharacterProfile | None:
        """Read current roster metadata without copying a player's inventory."""

        with self.lock:
            profile = self.character_profiles.get(int(character_id))
            if profile is None:
                return None
            owner = self._wallet_owner(character_id, profile.premium_currency)
            return replace(profile, premium_currency=self.account_wallets[owner]) if owner is not None else profile

    def profiles(self) -> tuple[CharacterProfile, ...]:
        with self.lock:
            return tuple(
                self.get_profile(character_id)
                for character_id in sorted(self.character_profiles)
            )

    def honor_ranking_records(self) -> tuple[HonorRankingRecord, ...]:
        """Copy public ranking fields in one lock, including offline players."""
        with self.lock:
            return tuple(
                HonorRankingRecord(
                    profile.character_id, profile.name, profile.class_id,
                    int(getattr(state, "honor_points", 0)),
                    int(getattr(state, "pvp_matches_won", 0)),
                    int(getattr(state, "pvp_matches_lost", 0)),
                    int(getattr(state, "pvp_kills", 0)),
                    int(getattr(state, "pvp_deaths", 0)),
                )
                for profile in self.character_profiles.values()
                for state in (self.states.get(profile.character_id),)
            )

    def put_profile(
        self, profile: CharacterProfile, *, flush: bool = False
    ) -> None:
        saved = _profile_from_document(_profile_to_document(profile))
        with self.lock:
            self.character_profiles[saved.character_id] = saved
            self._database.queue_profile(saved.character_id, saved)
        if flush:
            self.flush()

    def put(
        self, state: AccountState, *, flush: bool = False,
        profile: CharacterProfile | None = None,
        achievement_catalog=None,
    ) -> None:
        saved = copy.deepcopy(state)
        saved_profile = (
            _profile_from_document(_profile_to_document(profile))
            if profile is not None else None
        )
        if saved_profile is not None and saved_profile.character_id != saved.character_id:
            raise ValueError("profile and state must belong to the same character")
        saved.saved_at = datetime.now(timezone.utc).isoformat()
        with self.lock:
            current = self.states.get(int(saved.character_id))
            if current is not None and not set(current.admin_booking_ids).issubset(saved.admin_booking_ids):
                # An autosave captured before booking delivery can arrive after
                # its durable commit. Never replace that reward/receipt with an
                # older session snapshot; initialization installs the new IDs.
                return
            if current is not None and any(
                document.get("reward_status") == "granted"
                and saved.achievements.get(key, {}).get("reward_status") != "granted"
                for key, document in current.achievements.items()
            ):
                # A capture taken before reward delivery cannot erase the item
                # or its receipt after the achievement transaction commits.
                return
            saved.currency = self.commit_currency(saved.character_id, saved.currency)
            self.states[int(saved.character_id)] = saved
            owner = self._wallet_owner(saved.character_id)
            wallet = (owner, self.account_wallets[owner]) if owner is not None else None
            achievements = None
            if owner is not None and achievement_catalog is not None:
                document = {
                    key: copy.deepcopy(value) for key, value in saved.achievements.items()
                    if (definition := achievement_catalog.achievement(key)) is not None
                    and definition.account_wide
                }
                self.account_achievements[owner] = document
                achievements = (owner, document)
            if saved_profile is not None:
                saved_profile = replace(
                    saved_profile, premium_currency=saved.currency.andermant
                )
                self.character_profiles[saved.character_id] = saved_profile
            self._database.queue_state(
                saved.character_id, saved, wallet=wallet, profile=saved_profile,
                achievements=achievements,
            )
        if flush:
            self.flush()

    def delete(self, character_id: int, *, flush: bool = False) -> bool:
        """Forget a character's profile and state; returns whether one existed."""

        character_id = int(character_id)
        with self.lock:
            existed = (
                self.states.pop(character_id, None) is not None
                or self.character_profiles.pop(character_id, None) is not None
            )
            self._database.queue_delete(character_id)
        if flush:
            self.flush()
        return existed

    def request_flush(self) -> bool:
        """Schedule pending snapshots for writing without waiting."""

        return self._database.request_flush()

    def flush(self) -> bool:
        """Wait for all snapshots queued before this call to commit."""

        return self._database.flush()

    def close(self) -> None:
        """Drain the writer and close persistence during orderly shutdown."""

        self._database.close()
