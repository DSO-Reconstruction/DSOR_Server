"""Glowing Cave leader, resource nodes, and Possible Rewards behavior."""

import random
import threading
from types import SimpleNamespace
from unittest.mock import patch

from dso_server.gameplay.local_map_state import (
    CRAFTING_DUNGEON_LEADER_PRESENT_FLAG,
    CRAFTING_DUNGEON_MAP_ID,
)
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.gameplay.map_event_state import (
    MapEventRuntime,
    _dungeon_resource_category,
    load_map_event_content,
    load_map_signal_catalog,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import NewMonsterServerCommand
from dso_server.protocol.commerce_protocol import _new_item, load_purchase_item_definition
from dso_server.protocol.gem_protocol import insert_gem
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.socket_effect_protocol import SocketEffectDatabase
from dso_server.protocol.world_loot_protocol import (
    CRAFTING_DUNGEON_JEWEL_DROP_CHANCE,
    CRAFTING_DUNGEON_JEWEL_TEMPLATE,
    CRAFTING_DUNGEON_LEADER_TEMPLATE,
    CRAFTING_DUNGEON_MOUNT_DROP_CHANCE,
    CRAFTING_DUNGEON_MOUNT_TEMPLATES,
    _roll_crafting_dungeon_special_rewards,
    WorldLootDrop,
    add_world_drop_to_inventory,
    generate_monster_loot,
)
from dso_server.services.map_events import (
    _gatherable_ingredient_templates,
    _multitool_resource_category,
)
from dso_server.services.character_server import CharacterServer
from dso_server.services.player_init_world import (
    _crafting_dungeon_leader_present_from_roll,
)
from dso_server.tests import test_quests as quest_fixtures


LEADER = "b1205_ingredient_leader_cerberus_01"
RESOURCE_NODES = {
    "ingredient_tree_01": "herb",
    "ingredient_geode_01": "ore",
}


def _leader_templates(encounter):
    return tuple(
        command.value.template_id
        for command in encounter.spawn_batch.commands
        if isinstance(command, NewMonsterServerCommand)
        and command.value.template_id == LEADER
    )


def _loot_templates(generation):
    return tuple(drop.item.template_id for drop in generation.drops)


def test_leader_and_gatherables_are_mutually_exclusive_per_map_run():
    content = load_map_event_content(DEFAULT_STATIC_DB_PATH, CRAFTING_DUNGEON_MAP_ID)
    catalog = load_map_signal_catalog(DEFAULT_STATIC_DB_PATH)

    no_leader_runtime = MapEventRuntime(
        content, catalog, instance_key=("crafting", "resources"),
        initial_flags=frozenset(), scaling_difficulty=0,
    )
    visible_nodes = {
        state.definition.instance_id: _dungeon_resource_category(state.definition)
        for state in no_leader_runtime.published_props()
        if state.definition.instance_id in RESOURCE_NODES
    }
    assert visible_nodes == RESOURCE_NODES

    leader_runtime = MapEventRuntime(
        content, catalog, instance_key=("crafting", "leader"),
        initial_flags=frozenset({CRAFTING_DUNGEON_LEADER_PRESENT_FLAG}),
        scaling_difficulty=0,
    )
    assert not {
        state.definition.instance_id
        for state in leader_runtime.published_props()
        if state.definition.instance_id in RESOURCE_NODES
    }

    no_leader_encounter = load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH, CRAFTING_DUNGEON_MAP_ID,
        include_spawn_areas=False, monster_level=80, scaling_difficulty=0,
        active_event_flags=frozenset(),
    )
    leader_encounter = load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH, CRAFTING_DUNGEON_MAP_ID,
        include_spawn_areas=False, monster_level=80, scaling_difficulty=0,
        active_event_flags=frozenset({CRAFTING_DUNGEON_LEADER_PRESENT_FLAG}),
    )
    assert _leader_templates(no_leader_encounter) == ()
    assert _leader_templates(leader_encounter) == (LEADER,)


def test_leader_roll_splits_all_hash_bytes_exactly_fifty_fifty():
    outcomes = tuple(
        _crafting_dungeon_leader_present_from_roll(roll)
        for roll in range(256)
    )
    assert sum(outcomes) == 128


def test_no_leader_gatherables_are_explicitly_instantiated_in_prop_info():
    content = load_map_event_content(DEFAULT_STATIC_DB_PATH, CRAFTING_DUNGEON_MAP_ID)
    catalog = load_map_signal_catalog(DEFAULT_STATIC_DB_PATH)
    runtime = MapEventRuntime(
        content, catalog, instance_key=("crafting", "visible-nodes"),
        initial_flags=frozenset(), scaling_difficulty=0,
    )
    session = SimpleNamespace(
        gameplay_map_event_runtime=runtime,
        gameplay_quest_props_visible=set(),
        gameplay_map_difficulty=0,
        gameplay_monster_level=80,
        gameplay_character=SimpleNamespace(level=80),
        gameplay_chase_server_tick=0,
    )

    info = CharacterServer._map_event_prop_info(session)

    nodes = {
        entry.template_id: entry
        for entry in info.entries
        if entry.template_id in {"ingredient_tree_01", "ingredient_geode_01"}
    }
    assert set(nodes) == {"ingredient_tree_01", "ingredient_geode_01"}
    assert all(entry.instance_guid == bytes(16) for entry in nodes.values())


def test_fallback_nodes_use_live_difficulty_matched_ingredient_bag_items():
    content = load_map_event_content(DEFAULT_STATIC_DB_PATH, CRAFTING_DUNGEON_MAP_ID)
    nodes = {
        prop.instance_id: prop for prop in content.props
        if prop.instance_id in RESOURCE_NODES
    }
    assert {
        instance_id: _multitool_resource_category(prop)
        for instance_id, prop in nodes.items()
    } == RESOURCE_NODES

    for difficulty in range(10):
        rarity = min(5, difficulty // 2 + 1)
        herbs = _gatherable_ingredient_templates(
            str(DEFAULT_STATIC_DB_PATH), "herb", rarity,
        )
        yarn = _gatherable_ingredient_templates(
            str(DEFAULT_STATIC_DB_PATH), "yarn", rarity,
        )
        ore = _gatherable_ingredient_templates(
            str(DEFAULT_STATIC_DB_PATH), "ore", rarity,
        )
        assert herbs and yarn and ore
        assert all(f"rarity_0{rarity}" in item for item in (*herbs, *yarn, *ore))


def test_gatherable_ground_drop_is_capped_at_ten():
    content = load_map_event_content(DEFAULT_STATIC_DB_PATH, CRAFTING_DUNGEON_MAP_ID)
    tree = next(prop for prop in content.props if prop.instance_id == "ingredient_tree_01")
    template = _gatherable_ingredient_templates(
        str(DEFAULT_STATIC_DB_PATH), "herb", 1,
    )[0]
    server = CharacterServer.__new__(CharacterServer)
    server.static_database_path = DEFAULT_STATIC_DB_PATH
    server.gameplay_world_lock = threading.RLock()
    session = SimpleNamespace(
        flow_lock=threading.RLock(),
        gameplay_character=SimpleNamespace(level=80),
        gameplay_monster_encounter=None,
        gameplay_last_move=None,
        gameplay_inventory=None,
        gameplay_map=CRAFTING_DUNGEON_MAP_ID,
        gameplay_map_access_group_id=CRAFTING_DUNGEON_MAP_ID,
        gameplay_map_difficulty=0,
        gameplay_scaling_difficulty=0,
        gameplay_instance_id="gatherable-test-instance",
        gameplay_world_loot={},
        gameplay_next_item_id=70010,
        gameplay_chase_generation=1,
        send_reliable_ordered=lambda *args, **kwargs: None,
    )

    with patch.object(CharacterServer, "_reconcile_world_loot_visibility"):
        assert CharacterServer._spawn_gatherable_drop(
            server, session, tree, template, amount=50,
        )

    assert len(session.gameplay_world_loot) == 1
    drop = next(iter(session.gameplay_world_loot.values()))
    assert drop.item.template_id == template
    assert drop.item.amount == 10
    assert drop.auto_pick_up == "All"


def test_gathered_ingredients_enter_the_ingredient_bag_when_picked_up():
    template = _gatherable_ingredient_templates(
        str(DEFAULT_STATIC_DB_PATH), "herb", 1,
    )[0]
    definition = load_purchase_item_definition(DEFAULT_STATIC_DB_PATH, template)
    item = _new_item(definition, 70001, 10, 80)
    drop = WorldLootDrop(item, definition.item_category, "All")

    mutation = add_world_drop_to_inventory(
        InventoryInfo(), drop,
        max_stack_size=definition.max_stack_size,
        next_item_id=70002,
        ingredient_bag=definition.ingredient_bag,
    )

    assert definition.ingredient_bag
    assert template in mutation.inventory.ingredient_bag_templates
    assert [stored.item_id for stored in mutation.inventory.items] == [70001]
    assert mutation.inventory.ingredient_bag_item_ids == [70001]
    assert 70001 not in mutation.inventory.storage_slots


def test_delayed_gatherable_pickup_moves_drop_into_bag_without_another_move():
    case = quest_fixtures.QuestServiceTests(methodName="runTest")
    case.setUp()
    try:
        session = case._session(quest_fixtures.GARDEN, 50231, class_id=2)
        session.gameplay_inventory = InventoryInfo()
        session.gameplay_next_item_id = 70004
        session.gameplay_chase_generation = 12
        session.gameplay_world_loot = {}
        session.gameplay_announced_world_item_ids = set()
        session.gameplay_presence_callback = None
        template = _gatherable_ingredient_templates(
            str(DEFAULT_STATIC_DB_PATH), "herb", 1,
        )[0]
        definition = load_purchase_item_definition(DEFAULT_STATIC_DB_PATH, template)
        item = _new_item(definition, 70003, 4, 80)
        session.gameplay_world_loot[70003] = WorldLootDrop(
            item, definition.item_category, "All",
        )

        with patch.object(CharacterServer, "_dispatch_socket_event"):
            CharacterServer._auto_pickup_gatherable_drop(session, 70003, 12)

        assert 70003 not in session.gameplay_world_loot
        assert [stored.template_id for stored in session.gameplay_inventory.items] == [template]
        assert session.gameplay_inventory.ingredient_bag_item_ids == [70003]
        assert template in session.gameplay_inventory.ingredient_bag_templates
    finally:
        case.doCleanups()


def test_cerberus_drops_the_authored_jewel_and_both_mounts_when_forced():
    result = generate_monster_loot(
        DEFAULT_STATIC_DB_PATH, LEADER, class_name="mage", player_level=80,
        monster_level=80, position=(0.0, 0.0, 0.0), next_item_id=1,
        rng=random.Random(7), map_difficulty=0,
        map_access_id="b1205_crafting_dun", force_drops=True,
    )
    templates = _loot_templates(result)
    assert CRAFTING_DUNGEON_JEWEL_TEMPLATE in templates
    assert set(CRAFTING_DUNGEON_MOUNT_TEMPLATES) <= set(templates)


def test_ingredient_hunter_jewel_sockets_into_ordinary_equipment():
    jewel = _new_item(
        load_purchase_item_definition(
            DEFAULT_STATIC_DB_PATH, CRAFTING_DUNGEON_JEWEL_TEMPLATE,
        ),
        71001, 1, 100,
    )
    equipment = _new_item(
        load_purchase_item_definition(
            DEFAULT_STATIC_DB_PATH, "all_base_amulet_armor_block_critical",
        ),
        71002, 1, 100,
    )
    equipment.socket_count = 1
    equipment.socketed_templates = [""]
    inventory = InventoryInfo(
        items=[jewel, equipment],
        equipment_slots={equipment.item_id: (1,)},
    )

    mutation = insert_gem(
        inventory, jewel.item_id, equipment.item_id,
        static_database_path=DEFAULT_STATIC_DB_PATH,
    )

    assert mutation.equipment_item.socketed_templates == [jewel.template_id]
    assert all(item.item_id != jewel.item_id for item in mutation.inventory.items)
    evaluation = SocketEffectDatabase(DEFAULT_STATIC_DB_PATH).evaluate_inventory(
        mutation.inventory,
    )
    assert evaluation.drop_amount_bonus(
        "herb_ingredient_offensive_rarity_01",
    ) == (1.0, 0.0)


def test_cerberus_special_reward_rates_and_mount_rolls_are_independent():
    assert CRAFTING_DUNGEON_JEWEL_DROP_CHANCE == 0.05
    assert CRAFTING_DUNGEON_MOUNT_DROP_CHANCE == 0.15

    class ScriptedRng:
        def __init__(self, values):
            self.values = iter(values)

        def random(self):
            return next(self.values)

    listed = (CRAFTING_DUNGEON_JEWEL_TEMPLATE, *CRAFTING_DUNGEON_MOUNT_TEMPLATES)
    # Jewel succeeds below 5%; first mount succeeds below 15%; second mount
    # fails at its independent 15% boundary.
    rewards = _roll_crafting_dungeon_special_rewards(
        "b1205_crafting_dun_ingredients_01", LEADER, listed,
        ScriptedRng((0.049999, 0.149999, 0.15)),
    )
    assert rewards == (
        CRAFTING_DUNGEON_JEWEL_TEMPLATE,
        CRAFTING_DUNGEON_MOUNT_TEMPLATES[0],
    )


def test_crafting_mounts_and_ingredient_jewel_are_leader_only():
    for seed in range(20):
        result = generate_monster_loot(
            DEFAULT_STATIC_DB_PATH, "b1205_normal_add_minispider",
            class_name="mage", player_level=80, monster_level=80,
            position=(0.0, 0.0, 0.0), next_item_id=1,
            rng=random.Random(seed), map_difficulty=0,
            map_access_id="b1205_crafting_dun",
        )
        assert not set((
            CRAFTING_DUNGEON_JEWEL_TEMPLATE,
            *CRAFTING_DUNGEON_MOUNT_TEMPLATES,
        )) & set(_loot_templates(result))
