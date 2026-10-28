"""Earn recipe-book entries from authored inventory items and enforce gates."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Mapping

from dso_server.runtime import resident_content
from dso_server.protocol.character_selection import CLASS_NAMES
from dso_server.protocol.gameplay_commands import LOCAL_PLAYER_ACTOR_ID
from dso_server.protocol.inventory_economy_protocol import InventoryMutationBuilder
from dso_server.protocol.player_init_protocol import (
    INVENTORY_INFO_COMMAND,
    RECIPE_BOOK_INFO_COMMAND,
    RecipeBookEntry,
    RecipeBookInfo,
    ServerCommand,
    encode_server_command_batch,
)
from dso_server.services.base import CharacterService


@dataclass(frozen=True)
class RecipeAccess:
    resource_id: str
    unlock_resource_id: str
    achievement_id: str
    minimum_level: int


@dataclass(frozen=True)
class RecipeUnlockItem:
    resource_id: str
    minimum_level: int
    class_name: str


@dataclass(frozen=True)
class RecipeProgressionCatalog:
    recipes: Mapping[str, RecipeAccess]
    items: Mapping[str, RecipeUnlockItem]
    live_unlocks: frozenset[str]

    def recipe(self, resource_id: str) -> RecipeAccess:
        # Some direct workbench requests append a decimal result selector.
        # Match the same exact-first lookup as load_crafting_recipe.
        key = str(resource_id)
        definition = self.recipes.get(key)
        if definition is None and key[-1:].isdigit():
            definition = self.recipes.get(key[:-1])
        if definition is None:
            raise ValueError(f"unknown or locked recipe {resource_id!r}")
        return definition


@lru_cache(maxsize=4)
def _load_recipe_progression_catalog(path: str) -> RecipeProgressionCatalog:
    with sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True) as database:
        recipe_rows = database.execute(
            "SELECT Id, RecipeItemReference, AchievementReference, UnlockLevel "
            "FROM _Template_CraftingRecipes WHERE IsLive=1"
        ).fetchall()
        item_rows = database.execute(
            "SELECT Id, RequiredLevel, CharClass FROM _Template_Item "
            "WHERE ItemCategory IN ('RecipeItem', 'RecipeConsumable')"
        ).fetchall()
    recipes = {
        str(row[0]): RecipeAccess(
            str(row[0]), str(row[1] or ""), str(row[2] or ""),
            max(1, int(row[3] or 1)),
        )
        for row in recipe_rows
    }
    return RecipeProgressionCatalog(
        MappingProxyType(recipes),
        MappingProxyType({
            str(row[0]): RecipeUnlockItem(
                str(row[0]), max(1, int(row[1] or 1)),
                str(row[2] or "").casefold(),
            )
            for row in item_rows
        }),
        frozenset(row.unlock_resource_id for row in recipes.values()
                  if row.unlock_resource_id),
    )


def load_recipe_progression_catalog(database_path: str | Path) -> RecipeProgressionCatalog:
    return _load_recipe_progression_catalog(str(resident_content.resolved(database_path)))


def validate_crafting_recipe_access(session, recipe_id: str, database_path: str | Path) -> None:
    """Reject unauthored or unearned recipes before inventory/wallet changes."""
    recipe = load_recipe_progression_catalog(database_path).recipe(recipe_id)
    with session.flow_lock:
        character = getattr(session, "gameplay_character", None)
        if character is None or int(character.level) < recipe.minimum_level:
            raise ValueError("character level is below recipe unlock")
        resources = getattr(session, "gameplay_recipe_book_resources", ())
        if recipe.unlock_resource_id and recipe.unlock_resource_id not in resources:
            raise ValueError("recipe is not present in the character recipe book")
        if recipe.achievement_id:
            progress = getattr(session, "gameplay_achievements", {}).get(
                recipe.achievement_id.casefold()
            )
            if progress is None or not progress.completed:
                raise ValueError(f"recipe requires achievement {recipe.achievement_id!r}")


def handle_recipe_item_use(server, session, item_id: int) -> bool:
    """Consume one new unlock item, committing the item and book together.

    The caller holds the inventory action lock. The current inventory is read
    again under flow_lock, so replaying a use cannot consume a second copy or
    overwrite another unlock. Already learned and obsolete books stay in the bag.
    """
    catalog = load_recipe_progression_catalog(server.static_database_path)
    with session.flow_lock:
        inventory = session.gameplay_inventory
        item = next((candidate for candidate in inventory.items
                     if int(candidate.item_id) == int(item_id)), None) if inventory else None
        if item is None:
            return False
        definition = catalog.items.get(item.template_id)
        if definition is None:
            return False
        character = session.gameplay_character
        resources = tuple(session.gameplay_recipe_book_resources)
        if definition.resource_id in resources:
            return True
        if (
            definition.resource_id not in catalog.live_unlocks
            or character is None
            or int(character.level) < definition.minimum_level
            or (definition.class_name and definition.class_name !=
                CLASS_NAMES.get(int(character.class_id), "").casefold())
        ):
            return True

        builder = InventoryMutationBuilder(inventory, session.gameplay_next_item_id)
        builder.consume(item_id, 1, preserve_item_id=True)
        mutation = builder.finish()
        resources = (*resources, definition.resource_id)
        packet, packet_bits = encode_server_command_batch(
            (
                ServerCommand(INVENTORY_INFO_COMMAND, mutation.delta, LOCAL_PLAYER_ACTOR_ID),
                ServerCommand(
                    RECIPE_BOOK_INFO_COMMAND,
                    RecipeBookInfo(tuple(RecipeBookEntry(key, key) for key in resources)),
                    LOCAL_PLAYER_ACTOR_ID,
                ),
                ServerCommand(INVENTORY_INFO_COMMAND, mutation.final, LOCAL_PLAYER_ACTOR_ID),
            ),
            include_terminator=True,
        )
        session.gameplay_inventory = mutation.inventory
        session.gameplay_next_item_id = mutation.next_item_id
        session.gameplay_recipe_book_resources = resources

    # The durable inventory and book must agree even when the client's output
    # queue rejects the response. Feed Use before that outbound operation too.
    CharacterService._request_account_save(session, "recipe book unlocked", flush=True)
    deferred_packets = []
    CharacterService._achievement_feed(
        server, session, "use", (definition.resource_id,), count=1,
        deferred_packets=deferred_packets,
    )
    CharacterService._send_world_loot_state_batch(
        session, packet, packet_bits, label="recipe-book-unlocked"
    )
    for packet, packet_bits, label in deferred_packets:
        CharacterService._send_world_loot_state_batch(session, packet, packet_bits, label=label)
    return True
