"""Item metadata and validated, template-aware equipment booking options."""

from contextlib import closing
from functools import lru_cache
import os
from pathlib import Path
import sqlite3
import xml.etree.ElementTree as ElementTree

from dataclasses import replace
from dso_server.paths import DEFAULT_LANGUAGE_ROOT, PACKAGE_ROOT
from dso_server.protocol.commerce_protocol import load_purchase_item_definition, add_purchased_item
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.character_selection import CLASS_NAMES
from dso_server.protocol.world_loot_protocol import _is_equipment_row, EQUIPMENT_RARITIES, roll_new_equipment_enchantments


# Locales are only used to label item templates, so the first readable one wins.
LANGUAGE_PREFERENCE = ("tr", "en", "de", "ru", "cs", "bg", "ar")

# Fallback language roots when DSO_LANGUAGE_DIR is not set and data/language/<locale> is empty.
_FALLBACK_LANGUAGE_ROOTS = [
    PACKAGE_ROOT / "cdndata" / "export_win32" / "language",
]


def _has_locale_files(directory):
    """True if a directory contains at least one db.*.xml file."""
    return any(Path(directory).glob("db.*.xml"))


def item_language_directory():
    """The client locale folder that supplies item titles, or None for template names."""
    wanted = str(os.getenv("DSO_ITEM_LANGUAGE", "")).strip().casefold()
    if wanted:
        candidate = DEFAULT_LANGUAGE_ROOT / wanted
        if candidate.is_dir() and _has_locale_files(candidate):
            return candidate
        # Also check fallback roots (e.g., cdndata/export_win32/language/tr).
        for root in _FALLBACK_LANGUAGE_ROOTS:
            candidate = root / wanted
            if candidate.is_dir() and _has_locale_files(candidate):
                return candidate
        return None
    # Keep locale preference ahead of root preference so a German fallback
    # cannot override Turkish or English files shipped with the client.
    for name in LANGUAGE_PREFERENCE:
        for root in (DEFAULT_LANGUAGE_ROOT, *_FALLBACK_LANGUAGE_ROOTS):
            candidate = root / name
            if candidate.is_dir() and _has_locale_files(candidate):
                return candidate
    return None


@lru_cache(maxsize=64)
def locale_titles(language_directory):
    """Index every ``db.*.xml`` locale key the client would display."""
    titles = {}
    if language_directory is None:
        return titles
    for path in sorted(Path(language_directory).glob("db.*.xml")):
        for entry in ElementTree.parse(path).getroot().iter("item"):
            key = str(entry.get("name") or "")
            text = str(entry.text or "").strip()
            if key and text:
                titles[key] = text
    return titles


def item_language_directories():
    """Return every available locale directory, in a stable search order."""
    roots = (DEFAULT_LANGUAGE_ROOT, *_FALLBACK_LANGUAGE_ROOTS)
    available = set()
    for root in roots:
        if root.is_dir():
            available.update(
                child.name for child in root.iterdir()
                if child.is_dir() and _has_locale_files(child)
            )

    locale_order = (*LANGUAGE_PREFERENCE,
                    *sorted(available - set(LANGUAGE_PREFERENCE), key=str.casefold))
    directories = []
    for locale in locale_order:
        for root in roots:
            candidate = root / locale
            if locale in available and candidate.is_dir() and _has_locale_files(candidate):
                directories.append(candidate)
    return tuple(directories)


def _display_name(titles, template_id, name, locale_key):
    localized = titles.get(str(locale_key or ""))
    if not localized or localized.startswith("Localize:"):
        # Cosmetic templates often leave LocaleIdTitle empty even though the
        # client resolves their display name from db.switchable.xml using
        # ``<template id>Title`` (for example epicmount_doomhound_01Title).
        localized = titles.get(f"{template_id}Title")
    if localized and not localized.startswith("Localize:"):
        return localized
    return str(name or "").strip() or str(template_id)


@lru_cache(maxsize=4)
def _catalog(static_database_path, language_directory, search_language_directories):
    titles = locale_titles(language_directory)
    search_titles = tuple(locale_titles(directory) for directory in search_language_directories)
    with closing(sqlite3.connect(
            Path(static_database_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT Id,Name,LocaleIdTitle,ItemCategory,DefaultRarity,RequiredLevel,MaxStackSize,SlotType "
            "FROM _Template_Item").fetchall()
    catalog = []
    for row in rows:
        template_id = str(row[0])
        template_name = str(row[1] or "").strip()
        locale_key = str(row[2] or "")
        name = _display_name(titles, template_id, template_name, locale_key)
        search_names = {name, template_name}
        localized_names = []
        for directory, localized_titles in zip(search_language_directories, search_titles):
            for key in (locale_key, f"{template_id}Title"):
                localized = str(localized_titles.get(key) or "").strip()
                if localized and not localized.startswith("Localize:"):
                    search_names.add(localized)
                    localized_names.append((directory.name, localized))
        catalog.append({
            "template_id": template_id, "category": str(row[3] or ""),
            "name": name, "template_name": template_name,
            "search_names": tuple(sorted(search_names, key=str.casefold)),
            "localized_names": tuple(dict.fromkeys(localized_names)),
            "rarity": row[4], "required_level": row[5], "max_stack_size": row[6], "slot_types": row[7],
        })
    return tuple(catalog)


def item_catalog(static_database_path):
    """All item templates with display names and searchable names in every locale."""
    return _catalog(Path(static_database_path).resolve(), item_language_directory(),
                    item_language_directories())


def socket_limit(schedule, level):
    """Resolve either a constant or the game's level:value socket progression."""
    text = str(schedule or "0")
    if ":" not in text:
        return max(0, min(255, int(text)))
    result = 0
    for entry in text.split(","):
        if entry.strip():
            threshold, count = map(int, entry.split(":"))
            if level >= threshold:
                result = count
    return max(0, min(255, result))


def item_metadata(path, template_id):
    definition = load_purchase_item_definition(path, template_id)
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute(
            "SELECT Id,ItemCategory,SlotType,IconConfiguration,CharClass,MaxSocketCount,Name,LocaleIdTitle "
            "FROM _Template_Item WHERE Id=?", (template_id,),
        ).fetchone())
        tiers = sorted({0, *(int(r[0]) for r in db.execute("SELECT Tier FROM _Template_EnchantmentTiers"))})
    equipment = _is_equipment_row(row)
    rarities = list(range(5)) if definition.rarity < 5 else [definition.rarity]
    return {
        "template_id": template_id, "category": definition.item_category,
        "name": _display_name(locale_titles(item_language_directory()), template_id,
                              row.get("Name"), row.get("LocaleIdTitle")),
        "template_name": str(row.get("Name") or "").strip(),
        "equipment": equipment, "rarity": definition.rarity,
        "rarity_name": EQUIPMENT_RARITIES[definition.rarity],
        "max_stack_size": definition.max_stack_size, "required_level": definition.required_level,
        "min_level": definition.minimum_item_level, "max_level": definition.maximum_item_level,
        "slot_types": list(definition.slot_types),
        "classes": [v for v in str(row["CharClass"] or "").replace(";", ",").split(",") if v],
        "socket_limits": [socket_limit(row["MaxSocketCount"], level)
                          for level in range(definition.maximum_item_level + 1)],
        "rarities": [{"value": value, "name": EQUIPMENT_RARITIES[value]} for value in rarities],
        "enchantment_tiers": tiers,
        "bookable": definition.item_category not in {"Currency", "InstantConsumable"},
        "destination": "Ingredients" if definition.ingredient_bag else "Inventory",
    }


def equipment_options(metadata, options, *, level=1):
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ValueError("item options must be an object")
    if not metadata["equipment"]:
        if options:
            raise ValueError("non-equipment items support stack size only")
        return {}
    allowed = {"item_level", "rarity", "socket_count", "enchantment_tier", "roll_quality"}
    if options.keys() - allowed:
        raise ValueError("unknown equipment option")
    result = {"item_level": min(metadata["max_level"], max(metadata["min_level"], level)),
              "rarity": metadata["rarity"], "socket_count": 0,
              "enchantment_tier": 0, "roll_quality": None, **options}
    for field in allowed - {"roll_quality"}:
        if type(result[field]) is not int:
            raise ValueError(f"{field} must be a whole number")
    if not metadata["min_level"] <= result["item_level"] <= metadata["max_level"]:
        raise ValueError("item level is outside the template's allowed range")
    if result["rarity"] not in [r["value"] for r in metadata["rarities"]]:
        raise ValueError("rarity is not supported by this template")
    if not 0 <= result["socket_count"] <= metadata["socket_limits"][result["item_level"]]:
        raise ValueError("socket count exceeds this template's limit at the selected level")
    if result["enchantment_tier"] not in metadata["enchantment_tiers"]:
        raise ValueError("unknown enchantment tier")
    quality = result["roll_quality"]
    if quality is not None and (type(quality) is not int or not 0 <= quality <= 100):
        raise ValueError("roll quality must be a whole percentage from 0 to 100, or null for random")
    return result


def validate_equipment_rolls(path, metadata, options, class_id):
    """Check enchantment compatibility before accepting an undeliverable booking.

    Some crafting bases have no enchantment affinities; their higher-rarity
    combinations must report the game's normal validation error at creation.
    """
    definition = replace(load_purchase_item_definition(path, metadata["template_id"]),
                         rarity=options["rarity"], max_stack_size=1)
    inventory = InventoryInfo()
    add_purchased_item(inventory, definition, 1, next_item_id=1, item_level=options["item_level"])
    class_name = CLASS_NAMES.get(class_id, "mage")
    if metadata["classes"] and class_name not in metadata["classes"]:
        class_name = metadata["classes"][0]
    roll_new_equipment_enchantments(path, inventory, (1,), class_name=class_name,
                                   map_difficulty=options["enchantment_tier"])
