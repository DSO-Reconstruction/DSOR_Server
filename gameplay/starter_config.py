"""Starter grants configuration for new accounts and new characters.

Provides configurable:
- Membership (None / Premium / Deluxe, duration in days, active status)
- Currencies (Andermant, Gold/Silver/Copper, Badge of Honor, Draken, Glyph of Power, and wallet game-currencies)
- Starter equipment & inventory items (per-class core equipment and shared inventory/ring items)

The CMS edits ``data/starter_config.json``; server initialization and character
creation read the effective values dynamically, so changes take effect immediately.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone, timedelta
from functools import lru_cache
import json
import os
from pathlib import Path
from typing import Any, Mapping

from dso_server.paths import DEFAULT_STARTER_CONFIG_PATH
from dso_server.protocol.membership_protocol import (
    NO_MEMBERSHIP,
    PREMIUM_MEMBERSHIP,
    DELUXE_MEMBERSHIP,
    default_deluxe_expiry,
    add_calendar_years,
)
from dso_server.protocol.currency_protocol import (
    COPPER_PER_GOLD,
    DEFAULT_BADGE_OF_HONOR_BALANCE,
    DEFAULT_DRAKEN_BALANCE,
    DEFAULT_GLYPH_OF_POWER_BALANCE,
    DEFAULT_GAME_CURRENCY_BALANCES,
    GAME_CURRENCY_DEFINITIONS,
    GAME_CURRENCY_BY_NAME,
    GAME_CURRENCY_BY_WIRE_ID,
)
from dso_server.protocol.inventory_codec import (
    _CLASS_CORE_EQUIPMENT,
    BOOTSTRAP_CONSUMABLE_TEMPLATE,
    BOOTSTRAP_TRAVEL_ITEM_TEMPLATE,
    BOOTSTRAP_DIVINATION_STONE_TEMPLATE,
)
from dso_server.runtime import file_revisions


DEFAULT_MEMBERSHIP_TYPE = DELUXE_MEMBERSHIP
DEFAULT_MEMBERSHIP_DAYS = 1825  # ~5 years
DEFAULT_MEMBERSHIP_ACTIVE = True

DEFAULT_ANDERMANT = 100_000_000
DEFAULT_GOLD = 100_000_000
DEFAULT_SILVER = 0
DEFAULT_COPPER = 0

DEFAULT_SHARED_ITEMS: list[dict[str, Any]] = [
    {
        "template_id": "all_mythic_ring_death",
        "name": "Ring of Death",
        "amount": 1,
        "item_level": 1,
        "rarity": 6,
        "slot": "ring1",
    },
    {
        "template_id": "all_mythic_ring_life",
        "name": "Ring of Life",
        "amount": 1,
        "item_level": 1,
        "rarity": 6,
        "slot": "ring2",
    },
    {
        "template_id": "mythical_mount_manticore_01",
        "name": "Manticore Mount",
        "amount": 1,
        "item_level": 1,
        "rarity": 7,
        "slot": "inventory",
    },
    {
        "template_id": "pet_mortis_phase2_epic",
        "name": "Mortis Pet",
        "amount": 1,
        "item_level": 1,
        "rarity": 4,
        "slot": "inventory",
    },
    {
        "template_id": BOOTSTRAP_CONSUMABLE_TEMPLATE,
        "name": "Healing Potion",
        "amount": 1,
        "item_level": 1,
        "rarity": 4,
        "slot": "inventory",
    },
    {
        "template_id": BOOTSTRAP_TRAVEL_ITEM_TEMPLATE,
        "name": "Travel Map",
        "amount": 1,
        "item_level": 10,
        "rarity": 0,
        "slot": "inventory",
    },
    {
        "template_id": "gold_pass",
        "name": "Season Gold Pass",
        "amount": 1,
        "item_level": 1,
        "rarity": 5,
        "slot": "inventory",
    },
    {
        "template_id": "gold_pass_2",
        "name": "Season Gold Pass II",
        "amount": 1,
        "item_level": 1,
        "rarity": 5,
        "slot": "inventory",
    },
    {
        "template_id": "red_pass",
        "name": "Season Red Pass",
        "amount": 1,
        "item_level": 1,
        "rarity": 7,
        "slot": "inventory",
    },
    {
        "template_id": "red_pass_2",
        "name": "Season Red Pass II",
        "amount": 1,
        "item_level": 1,
        "rarity": 7,
        "slot": "inventory",
    },
    {
        "template_id": BOOTSTRAP_DIVINATION_STONE_TEMPLATE,
        "name": "Divination Stones",
        "amount": 2,
        "item_level": 1,
        "rarity": 0,
        "slot": "inventory",
    },
]

EQUIPMENT_SLOT_NAMES: dict[int, str] = {
    0: "Helmet",
    1: "Shoulders",
    2: "Torso",
    3: "Gloves",
    4: "Boots",
    5: "Main Hand Weapon",
    6: "Offhand / Shield",
    7: "Cloak / Back Item",
    8: "Ring 1",
    9: "Ring 2",
    10: "Amulet",
    11: "Belt",
    12: "Weapon Modifier",
}


def _build_default_class_equipment() -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for class_name, entries in _CLASS_CORE_EQUIPMENT.items():
        class_items: list[dict[str, Any]] = []
        for template_id, slots in entries:
            slot = slots[0] if slots else 0
            class_items.append({
                "template_id": template_id,
                "slot": slot,
                "slot_name": EQUIPMENT_SLOT_NAMES.get(slot, f"Slot {slot}"),
                "item_level": 1,
                "rarity": 0,
            })
        result[class_name] = class_items
    return result


DEFAULT_CLASS_EQUIPMENT = _build_default_class_equipment()


def default_starter_config() -> dict[str, Any]:
    """Return a pristine default starter configuration structure."""
    return {
        "membership": {
            "type": DEFAULT_MEMBERSHIP_TYPE,
            "duration_days": DEFAULT_MEMBERSHIP_DAYS,
            "active": DEFAULT_MEMBERSHIP_ACTIVE,
        },
        "currencies": {
            "andermant": DEFAULT_ANDERMANT,
            "gold": DEFAULT_GOLD,
            "silver": DEFAULT_SILVER,
            "copper": DEFAULT_COPPER,
            "badge_of_honor": DEFAULT_BADGE_OF_HONOR_BALANCE,
            "draken": DEFAULT_DRAKEN_BALANCE,
            "glyph_of_power": DEFAULT_GLYPH_OF_POWER_BALANCE,
            "game_currencies": {
                definition.name: definition.default_balance
                for definition in GAME_CURRENCY_DEFINITIONS
            },
        },
        "starter_items": {
            "shared": copy.deepcopy(DEFAULT_SHARED_ITEMS),
            "class_equipment": copy.deepcopy(DEFAULT_CLASS_EQUIPMENT),
        },
    }


def normalize_starter_config(document: Any) -> dict[str, Any]:
    """Validate and normalize a loaded or submitted starter config dictionary."""
    base = default_starter_config()
    if not isinstance(document, dict):
        return base

    # Membership normalization
    raw_m = document.get("membership")
    if isinstance(raw_m, dict):
        m_type = raw_m.get("type", DEFAULT_MEMBERSHIP_TYPE)
        try:
            m_type = int(m_type)
        except (ValueError, TypeError):
            m_type = DEFAULT_MEMBERSHIP_TYPE
        if m_type not in (NO_MEMBERSHIP, PREMIUM_MEMBERSHIP, DELUXE_MEMBERSHIP):
            m_type = DEFAULT_MEMBERSHIP_TYPE

        duration_days = raw_m.get("duration_days", DEFAULT_MEMBERSHIP_DAYS)
        try:
            duration_days = max(1, min(36500, int(duration_days)))
        except (ValueError, TypeError):
            duration_days = DEFAULT_MEMBERSHIP_DAYS

        active = bool(raw_m.get("active", DEFAULT_MEMBERSHIP_ACTIVE))
        base["membership"] = {
            "type": m_type,
            "duration_days": duration_days,
            "active": active,
        }

    # Currencies normalization
    raw_c = document.get("currencies")
    if isinstance(raw_c, dict):
        for key in ("andermant", "gold", "silver", "copper", "badge_of_honor", "draken", "glyph_of_power"):
            if key in raw_c:
                try:
                    val = max(0, min(2_000_000_000, int(raw_c[key])))
                    if key in ("silver", "copper"):
                        val = min(99, val)
                    base["currencies"][key] = val
                except (ValueError, TypeError):
                    pass

        raw_gc = raw_c.get("game_currencies")
        if isinstance(raw_gc, dict):
            for definition in GAME_CURRENCY_DEFINITIONS:
                # Can be referenced by name or wire_id
                val = raw_gc.get(definition.name, raw_gc.get(definition.wire_id))
                if val is not None:
                    try:
                        base["currencies"]["game_currencies"][definition.name] = max(
                            0, min(2_000_000_000, int(val))
                        )
                    except (ValueError, TypeError):
                        pass

    # Starter items normalization
    raw_items = document.get("starter_items")
    if isinstance(raw_items, dict):
        raw_shared = raw_items.get("shared")
        if isinstance(raw_shared, list):
            valid_shared: list[dict[str, Any]] = []
            for entry in raw_shared:
                if not isinstance(entry, dict):
                    continue
                tid = str(entry.get("template_id", "")).strip()
                if not tid:
                    continue
                try:
                    amount = max(1, min(999999, int(entry.get("amount", 1))))
                except (ValueError, TypeError):
                    amount = 1
                try:
                    level = max(1, min(100, int(entry.get("item_level", 1))))
                except (ValueError, TypeError):
                    level = 1
                try:
                    rarity = max(0, min(15, int(entry.get("rarity", 0))))
                except (ValueError, TypeError):
                    rarity = 0
                slot = str(entry.get("slot", "inventory")).strip()
                name = str(entry.get("name", tid)).strip() or tid
                valid_shared.append({
                    "template_id": tid,
                    "name": name,
                    "amount": amount,
                    "item_level": level,
                    "rarity": rarity,
                    "slot": slot,
                })
            base["starter_items"]["shared"] = valid_shared

        raw_class_eq = raw_items.get("class_equipment")
        if isinstance(raw_class_eq, dict):
            for class_name in ("warrior", "mage", "ranger", "dwarf"):
                entries = raw_class_eq.get(class_name)
                if isinstance(entries, list):
                    valid_class_items: list[dict[str, Any]] = []
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        tid = str(entry.get("template_id", "")).strip()
                        if not tid:
                            continue
                        try:
                            slot = max(0, min(30, int(entry.get("slot", 0))))
                        except (ValueError, TypeError):
                            slot = 0
                        try:
                            level = max(1, min(100, int(entry.get("item_level", 1))))
                        except (ValueError, TypeError):
                            level = 1
                        try:
                            rarity = max(0, min(15, int(entry.get("rarity", 0))))
                        except (ValueError, TypeError):
                            rarity = 0
                        slot_name = EQUIPMENT_SLOT_NAMES.get(slot, f"Slot {slot}")
                        valid_class_items.append({
                            "template_id": tid,
                            "slot": slot,
                            "slot_name": slot_name,
                            "item_level": level,
                            "rarity": rarity,
                        })
                    if valid_class_items:
                        base["starter_items"]["class_equipment"][class_name] = valid_class_items

    return base


@lru_cache(maxsize=4)
def _read_document(path: str, modified_ns: int, size: int) -> dict:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return document if isinstance(document, dict) else {}


def _config_signature(path: Path | str | None = None) -> tuple[str, int, int] | None:
    key = str(path or DEFAULT_STARTER_CONFIG_PATH)
    revision = file_revisions.observed_revision(key)
    if revision is None:
        return None
    return (str(file_revisions.resolved_path(key)), *revision)


def load_starter_config(path: Path | str | None = None) -> dict[str, Any]:
    """Read the effective starter grants configuration."""
    target = Path(path or DEFAULT_STARTER_CONFIG_PATH)
    signature = _config_signature(target)
    if signature is None:
        try:
            if target.is_file():
                raw = json.loads(target.read_text(encoding="utf-8"))
                return normalize_starter_config(raw)
        except (OSError, ValueError, TypeError):
            pass
        return default_starter_config()
    raw = _read_document(*signature)
    return normalize_starter_config(raw)



def calculate_starter_gold_units(config: Mapping[str, Any]) -> int:
    """Compute total copper-scale gold units from gold, silver, copper balances."""
    curr = config.get("currencies", {})
    gold = int(curr.get("gold", DEFAULT_GOLD))
    silver = int(curr.get("silver", DEFAULT_SILVER))
    copper = int(curr.get("copper", DEFAULT_COPPER))
    return (gold * COPPER_PER_GOLD) + (silver * 100) + copper


def calculate_membership_expiry_iso(config: Mapping[str, Any], now: datetime | None = None) -> str:
    """Generate ISO-8601 UTC timestamp for configured membership duration."""
    m = config.get("membership", {})
    days = int(m.get("duration_days", DEFAULT_MEMBERSHIP_DAYS))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    expiry = current + timedelta(days=days)
    return expiry.isoformat()
