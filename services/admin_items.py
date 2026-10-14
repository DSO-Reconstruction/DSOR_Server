"""Item metadata and validated, template-aware equipment booking options."""

from contextlib import closing
from pathlib import Path
import sqlite3

from dataclasses import replace
from dso_server.protocol.commerce_protocol import load_purchase_item_definition, add_purchased_item
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.character_selection import CLASS_NAMES
from dso_server.protocol.world_loot_protocol import _is_equipment_row, EQUIPMENT_RARITIES, roll_new_equipment_enchantments


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
            "SELECT Id,ItemCategory,SlotType,IconConfiguration,CharClass,MaxSocketCount "
            "FROM _Template_Item WHERE Id=?", (template_id,),
        ).fetchone())
        tiers = sorted({0, *(int(r[0]) for r in db.execute("SELECT Tier FROM _Template_EnchantmentTiers"))})
    equipment = _is_equipment_row(row)
    rarities = list(range(5)) if definition.rarity < 5 else [definition.rarity]
    return {
        "template_id": template_id, "category": definition.item_category,
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
