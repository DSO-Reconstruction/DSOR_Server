"""Exact outputs for crafting recipes whose result names a server loot list.

The current client has 2,574 such recipes, all ``DISMANTLE_<opal>``. Its
Opal Divider description (language/en/db.upgradeitem.xml) specifies three
single gems; ``_Template_OpalCrafting`` names those three gems and quantities.
The fourth ingredient is creation dust, not a dismantling reward. No random
choice or guessed loot weight is involved. Unknown table families fail closed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import sqlite3

from dso_server.runtime import resident_content
from dso_server.protocol.inventory_economy_protocol import CraftingRecipe


@dataclass(frozen=True)
class CraftingOutput:
    template_id: str
    amount: int


@lru_cache(maxsize=4096)
def _opal_outputs(path: str, modified_ns: int, size: int, table_id: str):
    del modified_ns, size
    prefix = "DISMANTLE_"
    if not table_id.startswith(prefix) or not table_id[len(prefix):]:
        raise ValueError(f"crafting loot table {table_id!r} has no authored output resolver")
    opal_id = table_id[len(prefix):]
    with sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True) as database:
        rows = database.execute(
            "SELECT IngredientItemId1, IngredientItemId2, IngredientItemId3 "
            "FROM _Template_OpalCrafting WHERE CraftedItemId=?", (opal_id,),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError(f"crafting loot table {table_id!r} has no unique opal mapping")
        outputs = Counter()
        for encoded in rows[0]:
            template, separator, quantity = str(encoded or "").partition(":")
            if not template or (separator and not quantity.isdigit()):
                raise ValueError(f"invalid opal component {encoded!r}")
            amount = int(quantity) if separator else 1
            category = database.execute(
                "SELECT ItemCategory FROM _Template_Item WHERE Id=?", (template,),
            ).fetchone()
            if amount != 1 or category is None or category[0] != "EquipmentGem":
                raise ValueError(f"opal component {encoded!r} is not one authored gem")
            outputs[template] += amount
    if len(outputs) != 3:
        raise ValueError(f"crafting loot table {table_id!r} does not resolve to three distinct gems")
    return tuple(CraftingOutput(template, amount) for template, amount in outputs.items())


def resolve_crafting_outputs(database_path, recipe: CraftingRecipe) -> tuple[CraftingOutput, ...]:
    if not recipe.result_is_loot_table:
        return (CraftingOutput(recipe.result_template_id, recipe.result_amount),)
    if recipe.crafting_recipe_type != "ItemUpgrade":
        raise ValueError(f"unsupported crafting loot-table recipe type {recipe.crafting_recipe_type!r}")
    opal_id = recipe.result_template_id.removeprefix("DISMANTLE_")
    ingredients = Counter()
    for template_id, amount in recipe.ingredients:
        ingredients[template_id] += amount
    if ingredients != Counter({opal_id: 1, "gem_combined_dismantle_tool": 1}):
        raise ValueError("opal dismantling recipe does not consume one opal and one divider")
    path = resident_content.resolved(database_path)
    stat = resident_content.stat(path)
    return _opal_outputs(str(path), stat.st_mtime_ns, stat.st_size, recipe.result_template_id)
