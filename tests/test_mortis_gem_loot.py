"""The final Mortis awards one exact Royal gem from every normal family."""

from collections import Counter
import random
import threading
from types import SimpleNamespace

import pytest

from dso_server.gameplay.mortis_arena import MAP_ID, UNBOUND, FINAL_GEM_REWARDS
from dso_server.paths import DEFAULT_STATIC_DB_PATH as STATIC
from dso_server.protocol.world_loot_protocol import (
    generate_monster_loot, add_world_drop_to_inventory,
)
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.runtime.transport import Session
from dso_server.services.character_server import CharacterServer


@pytest.mark.parametrize("difficulty", [0, 7, 9])
def test_exact_royal_rewards_include_all_elemental_resistances(difficulty):
    generation = generate_monster_loot(
        STATIC, UNBOUND, class_name="mage", player_level=100, monster_level=100,
        position=(42.0, 0.0, 14.0), next_item_id=1000,
        rng=random.Random(1), map_difficulty=difficulty,
        guaranteed_gem_templates=FINAL_GEM_REWARDS,
    )
    gems = [d for d in generation.drops if d.item_category == "EquipmentGem"]
    assert len(FINAL_GEM_REWARDS) == 13
    assert Counter(d.item.template_id for d in gems) == Counter(FINAL_GEM_REWARDS)
    assert all(d.item.amount == 1 for d in gems)
    assert {f"gem_diamond_{element}_i" for element in (
        "fire", "ice", "lightning", "poison", "andermagic",
    )} <= {d.item.template_id for d in gems}
    assert len({d.item.item_id for d in generation.drops}) == len(generation.drops)
    inventory = InventoryInfo()
    for drop in gems:
        inventory = add_world_drop_to_inventory(inventory, drop).inventory
    assert Counter(i.template_id for i in inventory.items) == Counter(FINAL_GEM_REWARDS)


@pytest.mark.parametrize("map_id,template,expected", [
    (MAP_ID, UNBOUND, 13),
    (MAP_ID, "boss_mortis_mounted", 0),
    (MAP_ID, "boss_mortis_bone_dragon", 0),
    ("pw102_mortis_01_boss", UNBOUND, 0),
])
def test_only_the_final_main_boss_in_mortis_exile_gets_the_bonus(map_id, template, expected):
    server = CharacterServer.__new__(CharacterServer)
    server.static_database_path = STATIC
    server.gameplay_world_lock = threading.RLock()
    server.gameplay_world_next_item_ids = {}
    session = Session(("127.0.0.1", 50114), None)
    session.gameplay_map = map_id
    session.gameplay_event_catalog = SimpleNamespace(
        collectible_drops=lambda **kwargs: (), event_loot_drops=lambda **kwargs: ())
    generation = server._generate_world_loot_for_session(
        session, encounter=SimpleNamespace(static_database_path=STATIC, map_id=map_id),
        monster=SimpleNamespace(template_id=template, level=100),
        class_name="mage", player_level=100, position=(42.0, 0.0, 14.0),
        position_projector=None,
    )
    gems = [d for d in generation.drops if d.item_category == "EquipmentGem"]
    assert len(gems) == expected
    assert all(d.item.amount == 1 for d in gems)
