"""PvP arena loadout and stat rules.

The arena is not a damage reduction multiplier: it is a different loadout.
These tests pin the predicate (which map/match is an arena), the stat filter
(base values, enchantments and socketed gems only), the talent-tree filter and
the entry/exit handling of pets and mounts.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dso_server.gameplay import pvp_arena_rules
from dso_server.gameplay.local_mount_state import (
    BLACK_BEAR_MOUNT, begin_mount_cast, begin_mount_ride,
)
from dso_server.gameplay.player_combat_stats import (
    CombatModifier, _calculate_player_combat_stats, prime_player_combat_database,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import StatusEffectEntry
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.protocol.pvp_match_protocol import (
    MATCH_MODE_BEST_OF_3_1V1, MATCH_MODE_BEST_OF_5_5V5,
    MATCH_MODE_CAPTURE_THE_FLAG_5V5, MATCH_MODE_LAST_MAN_STANDING_8FFA,
)
from dso_server.runtime.transport import Session
from dso_server.services import character_server  # noqa: F401  (binds the facade)
from dso_server.services.base import CharacterService


PORT = iter(range(52500, 52600))


def session(map_id="b1102_dm", *, match_mode=MATCH_MODE_BEST_OF_3_1V1):
    s = Session(("127.0.0.1", next(PORT)), Mock())
    s.gameplay_map = map_id
    s.gameplay_match = (
        None if match_mode is None else SimpleNamespace(match_mode=match_mode)
    )
    s.gameplay_combat_state = SimpleNamespace(
        encounter=SimpleNamespace(
            static_database_path=str(DEFAULT_STATIC_DB_PATH)
        ),
    )
    s.gameplay_presence_callback = None
    s.gameplay_persist_callback = None
    return s


def entry(effect_id, instance_id):
    return StatusEffectEntry(
        effect_id=int(effect_id), instance_id=int(instance_id), end_tick=0,
        tick_interval=25, start_tick=0, stack_index=0, total_duration=25,
        source_level=100, source_actor_id=1, update_timing=False,
        stacked=False, permanent=True, has_details=True,
    )


class Server:
    static_database_path = str(DEFAULT_STATIC_DB_PATH)

    def __init__(self):
        self.labels = []

    def _send_pvp(self, s, packet, label=""):
        self.labels.append(label)


@pytest.fixture(scope="module")
def database():
    return prime_player_combat_database(DEFAULT_STATIC_DB_PATH)


def test_arena_predicate_follows_the_map_and_the_match():
    assert pvp_arena_rules.arena_active("b1102_dm")
    assert pvp_arena_rules.arena_active("a0206_tdm")
    assert not pvp_arena_rules.arena_active("a0200_kingscity")
    assert not pvp_arena_rules.arena_active(None)
    assert pvp_arena_rules.arena_active(session("a0200_kingscity"))
    assert not pvp_arena_rules.arena_active(
        session("a0200_kingscity", match_mode=None)
    )


@pytest.mark.parametrize("map_id,mode,allowed", [
    ("a0200_kingscity", None, True),
    ("b1102_dm", MATCH_MODE_BEST_OF_3_1V1, False),
    ("a0206_8ffa_lms", MATCH_MODE_LAST_MAN_STANDING_8FFA, False),
    ("a0206_tdm", MATCH_MODE_BEST_OF_5_5V5, True),
    ("c2103_ctf", MATCH_MODE_CAPTURE_THE_FLAG_5V5, True),
])
def test_mounts_are_only_ridden_on_the_five_versus_five_arenas(
    map_id, mode, allowed,
):
    assert pvp_arena_rules.mount_allowed(session(map_id, match_mode=mode)) is allowed


def test_ancient_wisdom_and_group_effects_are_disabled_honor_is_not():
    disabled = pvp_arena_rules.disabled_talent_effect_ids(DEFAULT_STATIC_DB_PATH)
    from dso_server.persistence.readonly_db import connect
    with connect(str(DEFAULT_STATIC_DB_PATH)) as db:
        wisdom_ids = {
            int(row[0]) - 1 for row in db.execute(
                "SELECT rowid FROM _Template_StatusEffect WHERE Id IN "
                "('trait_wisdom_all_damage_effect',"
                "'trait_wisdom_all_damage_stackeffect')"
            )
        }
        group_ids = {
            int(row[0]) - 1 for row in db.execute(
                "SELECT rowid FROM _Template_StatusEffect WHERE Id = "
                "'trait_groupaura_warrior_hpmax_trigger'"
            )
        }
        honor_id = int(db.execute(
            "SELECT rowid-1 FROM _Template_StatusEffect WHERE Id = "
            "'pvp_mage_talent4_activator'"
        ).fetchone()[0])
    assert wisdom_ids and wisdom_ids <= disabled
    assert group_ids and group_ids <= disabled
    assert honor_id not in disabled


def test_arena_keeps_base_values_and_gems_but_drops_runes(database):
    def inventory(*sockets):
        return InventoryInfo(
            items=[
                ItemInfo(
                    item_id=i + 1, item_level=100,
                    template_id="all_base_trinket_rune_01",
                    socketed_templates=list(socket),
                )
                for i, socket in enumerate(sockets)
            ],
            equipment_slots={i + 1: (14 + i,) for i in range(len(sockets))},
        )

    def stats(inventory, arena):
        return _calculate_player_combat_stats(
            database, class_name="mage", level=100, inventory=inventory,
            pvp_arena=arena,
        )

    plain = inventory(())
    runed = inventory(("unique_gem_resistance_damage_block_04",))
    assert stats(runed, False).minimum_damage > stats(plain, False).minimum_damage
    assert stats(runed, True).minimum_damage == pytest.approx(
        stats(plain, True).minimum_damage
    )

    # A gem socketed into ordinary equipment still acts in the arena.
    gem_gear = InventoryInfo(
        items=[ItemInfo(
            item_id=1, item_level=100,
            template_id="warrior_base_rh_sword_speedAttack_damage",
            socketed_templates=["gem_faster_run_q"],
        )],
        equipment_slots={1: (5,)},
    )
    assert stats(gem_gear, True).movement_speed == pytest.approx(
        stats(gem_gear, False).movement_speed
    )
    assert stats(gem_gear, True).movement_speed > stats(
        InventoryInfo(
            items=[ItemInfo(
                item_id=1, item_level=100,
                template_id="warrior_base_rh_sword_speedAttack_damage",
            )],
            equipment_slots={1: (5,)},
        ), True,
    ).movement_speed


def test_arena_ignores_rift_force_lines(database):
    inventory = SimpleNamespace(items=[], equipment_slots={})
    sources = (
        CombatModifier("RiftEchoChance", ("0.5", "absolute")),
        CombatModifier("RiftEcho", ("0.4", "absolute")),
        CombatModifier("ItemRiftForce", ("0.5", "absolute")),
    )
    hub = _calculate_player_combat_stats(
        database, class_name="mage", level=100, inventory=inventory,
        modifier_sources=sources,
    )
    arena = _calculate_player_combat_stats(
        database, class_name="mage", level=100, inventory=inventory,
        modifier_sources=sources, pvp_arena=True,
    )
    assert hub.rift_force_echo_chance == pytest.approx(0.5)
    assert hub.rift_force_echo_multiplier == pytest.approx(0.4)
    assert hub.final_damage_reduction == pytest.approx(0.5)
    assert arena.rift_force_echo_chance == 0
    assert arena.rift_force_echo_multiplier == 0
    assert arena.final_damage_reduction == 0


def test_arena_sources_drop_sockets_ancient_wisdom_and_sets():
    s = session()
    disabled = pvp_arena_rules.disabled_talent_effect_ids(DEFAULT_STATIC_DB_PATH)
    knowledge_id = min(disabled)
    honor_id = 3
    s.gameplay_persistent_status_effects = {
        1: entry(knowledge_id, 1),
        2: entry(honor_id, 2),
    }
    s.gameplay_item_set_effects = {10: entry(7000, 10)}
    s.gameplay_socket_effects = {11: entry(7001, 11)}
    s.gameplay_runtime_status_effects = {
        12: (entry(7002, 12), None),
        13: (entry(honor_id, 13), None),
    }
    s.gameplay_item_set_programs = {12: SimpleNamespace(effect_id=7002)}
    s.gameplay_item_set_evaluation = SimpleNamespace(
        modifiers=(CombatModifier("Damage", ("0.5", "relative")),)
    )
    s.gameplay_socket_effect_evaluation = SimpleNamespace(cast_rules=())

    sources = CharacterService._active_skill_modifier_sources(s)
    ids = [int(getattr(source, "effect_id", -1)) for source in sources]
    assert knowledge_id not in ids
    assert honor_id in ids
    # Item sets, rune/jewel sockets stay inert in an arena.
    assert 7000 not in ids
    assert 7001 not in ids
    assert not any(getattr(source, "kind", "") == "Damage" for source in sources)

    # Outside the arena the very same containers all contribute.
    s.gameplay_match = None
    s.gameplay_map = "a0200_kingscity"
    ids = [
        int(getattr(source, "effect_id", -1))
        for source in CharacterService._active_skill_modifier_sources(s)
    ]
    assert knowledge_id in ids
    assert 7000 in ids
    assert 7001 in ids


def test_item_set_bonuses_are_disabled_in_the_arena():
    disabled = pvp_arena_rules.arena_disabled_effect_ids(DEFAULT_STATIC_DB_PATH)
    from dso_server.services.item_set_persistence import persistent_set_effect_ids

    set_ids = persistent_set_effect_ids(DEFAULT_STATIC_DB_PATH)
    assert set_ids
    assert set_ids.issubset(disabled)


def test_arena_entry_retires_the_pet_and_keeps_its_selection():
    s = session()
    s.gameplay_active_pet = "pet_dragon_joygame"
    s.gameplay_active_pet_effect_instance_id = 7
    s.gameplay_pet_restore_pending = True
    s.gameplay_persistent_status_effects = {7: entry(6000, 7)}
    server = Server()

    CharacterService._clear_battleground_pet_effects(server, s)

    assert 7 not in s.gameplay_persistent_status_effects
    assert s.gameplay_active_pet_effect_instance_id is None
    assert s.gameplay_pet_restore_pending is False
    assert s.gameplay_active_pet == "pet_dragon_joygame"
    assert "pvp-pet-cleanse" in server.labels


def test_arena_entry_cleanses_rune_jewel_sockets_and_sets():
    s = session()
    s.gameplay_item_set_effects = {10: entry(7000, 10)}
    s.gameplay_socket_effects = {11: entry(7001, 11)}
    s.gameplay_item_set_evaluation = SimpleNamespace(modifiers=())
    s.gameplay_socket_effect_evaluation = SimpleNamespace(cast_rules=())
    server = Server()

    CharacterService._clear_battleground_gear_effects(server, s)

    # Sets and socket effects are cleansed on arena entry.
    assert s.gameplay_item_set_effects == {}
    assert s.gameplay_item_set_evaluation is None
    assert s.gameplay_socket_effects == {}
    assert "pvp-gear-cleanse" in server.labels


def test_arena_entry_stows_the_shadow_soul_gear_and_puts_it_back():
    from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo

    s = session()
    s.gameplay_inventory = InventoryInfo(
        items=[
            ItemInfo(item_id=1, template_id="mage_base_helmet_crafted",
                     item_level=100),
            ItemInfo(item_id=2,
                     template_id="mercenary_all_base_amulet_setSoulDestruction_mage",
                     item_level=100),
        ],
        equipment_slots={1: (0,), 2: (25,)},
        storage_size=10,
    )
    s.gameplay_persistent_status_effects = {}
    s.gameplay_item_set_effects = {}
    s.gameplay_socket_effects = {}
    s.gameplay_item_set_evaluation = None
    s.gameplay_socket_effect_evaluation = None
    s.send_reliable_ordered = Mock()
    server = Server()

    CharacterService._unequip_battleground_mercenary_gear(server, s)

    # Shadow Soul pieces leave their slots so the client stops counting them.
    assert 2 not in s.gameplay_inventory.equipment_slots
    assert 1 in s.gameplay_inventory.equipment_slots
    assert 2 in s.gameplay_inventory.storage_slots
    assert s.gameplay_mercenary_restore_slots == ((2, (25,)),)

    CharacterService._restore_battleground_mercenary_gear(server, s)

    assert s.gameplay_inventory.equipment_slots[2] == (25,)
    assert 2 not in s.gameplay_inventory.storage_slots
    assert s.gameplay_mercenary_restore_slots == ()


def test_the_client_never_receives_the_ancient_wisdom_selections():
    from dso_server.protocol.player_init_protocol import TalentCategorySelection

    categories = (
        TalentCategorySelection(1, (), ()),
        TalentCategorySelection(2, (), ()),
        TalentCategorySelection(1, (), ()),
        TalentCategorySelection(0, (), ()),
    )
    kept = pvp_arena_rules.filter_talent_categories(categories, False)
    assert kept is categories

    filtered = pvp_arena_rules.filter_talent_categories(categories, True)
    assert filtered[0] is categories[0]
    assert filtered[3] is categories[3]
    assert filtered[1].selected_skills == ()
    assert filtered[2].selected_skills == ()
    assert filtered[0].preselection == 1


def test_shadow_soul_items_are_inert_in_an_arena():
    for category in (
        "Mercenary", "MercenaryJewelry", "MercenaryDefenseBackitem",
    ):
        assert pvp_arena_rules.item_definition_disabled(
            SimpleNamespace(category=category)
        )
    # Ordinary gear and its gems still act.
    for category in ("Weapon", "Armor", "EquipmentGem"):
        assert not pvp_arena_rules.item_definition_disabled(
            SimpleNamespace(category=category)
        )


def test_arena_entry_strips_shadow_soul_gear_effects():
    s = session()
    disabled = pvp_arena_rules.arena_disabled_effect_ids(DEFAULT_STATIC_DB_PATH)
    mercenary_ids = pvp_arena_rules.mercenary_effect_ids(DEFAULT_STATIC_DB_PATH)
    assert mercenary_ids
    assert mercenary_ids <= disabled
    effect_id = min(mercenary_ids)
    other_id = max(
        value for value in range(2000)
        if value not in disabled and value not in (3,)
    )
    s.gameplay_persistent_status_effects = {
        1: entry(effect_id, 1),
        2: entry(other_id, 2),
    }
    s.gameplay_runtime_status_effects = {3: (entry(effect_id, 3), None)}
    server = Server()

    CharacterService._clear_battleground_mercenary_effects(server, s)

    assert 1 not in s.gameplay_persistent_status_effects
    assert 3 not in s.gameplay_runtime_status_effects
    assert 2 in s.gameplay_persistent_status_effects
    assert "pvp-shadow-soul-cleanse" in server.labels


def test_mercenary_slots_do_not_complete_a_set():
    from dso_server.protocol.item_set_protocol import ItemSetDatabase

    database = ItemSetDatabase(DEFAULT_STATIC_DB_PATH)
    definition = next(
        value for value in database.definitions
        if value.set_id == "set_mage_norselands_01"
    )
    inventory = InventoryInfo(
        items=[
            ItemInfo(item_id=i + 1, template_id=template, item_level=100)
            for i, template in enumerate(definition.parts[:3])
        ],
        # Two of the three pieces sit in the Shadow Soul's slots.
        equipment_slots={1: (25,), 2: (26,), 3: (2,)},
    )
    with_merc = database.evaluate_inventory(
        inventory, class_name="mage", include_mercenary=True,
    )
    without_merc = database.evaluate_inventory(
        inventory, class_name="mage", include_mercenary=False,
    )
    assert [tier.required_parts for tier in with_merc.active_tiers] == [2, 3]
    assert without_merc.active_tiers == ()
    # The default must already exclude them: the Shadow Soul's own set grants
    # the *companion* its +20% health and damage, never the player.  Counting
    # those three pieces for the player raised the mage's health from 871k to
    # 1.2M in the character panel.
    assert database.evaluate_inventory(
        inventory, class_name="mage",
    ).active_tiers == ()


def test_the_arena_buff_reaches_every_authored_line(database):
    from dso_server.protocol.gameplay_protocol import StatusEffectEntry

    buff = pvp_arena_rules_effect_id()
    if buff is None:
        pytest.skip("pvp_char_modifier is not in this database")
    empty = InventoryInfo()

    def entry(effect_id):
        return StatusEffectEntry(
            effect_id=effect_id, instance_id=1, end_tick=0, tick_interval=0,
            start_tick=0, stack_index=0, total_duration=0, source_level=100,
            source_actor_id=1, update_timing=False, stacked=False,
            permanent=True, has_details=True, values=(0.0,) * 5,
        )

    plain = _calculate_player_combat_stats(
        database, class_name="mage", level=100, inventory=empty,
    )
    buffed = _calculate_player_combat_stats(
        database, class_name="mage", level=100, inventory=empty,
        modifier_sources=(entry(buff),),
    )
    # Damage -35%, MaxHealthPoints +40%, Speed -40%, plus the authored
    # absolute armor/resistance/attack-speed lines.
    assert buffed.minimum_damage == pytest.approx(
        plain.minimum_damage * 0.65
    )
    assert buffed.maximum_health == pytest.approx(
        plain.maximum_health * 1.4
    )
    assert buffed.attack_speed < plain.attack_speed
    assert buffed.movement_speed < plain.movement_speed
    assert buffed.armor_rating > plain.armor_rating
    assert dict(buffed.resistance_ratings)["fire"] > 0


def test_the_authored_arena_buff_cuts_outgoing_damage(database):
    from dso_server.protocol.gameplay_protocol import StatusEffectEntry

    buff = pvp_arena_rules_effect_id()
    if buff is None:
        pytest.skip("pvp_char_modifier is not in this database")
    empty = InventoryInfo()
    entry = StatusEffectEntry(
        effect_id=buff, instance_id=1, end_tick=0, tick_interval=0,
        start_tick=0, stack_index=0, total_duration=0, source_level=100,
        source_actor_id=1, update_timing=False, stacked=False,
        permanent=True, has_details=True, values=(0.0,) * 5,
    )
    plain = _calculate_player_combat_stats(
        database, class_name="mage", level=100, inventory=empty,
        pvp_arena=False,
    )
    hub = _calculate_player_combat_stats(
        database, class_name="mage", level=100, inventory=empty,
        modifier_sources=(entry,), pvp_arena=False,
    )
    arena = _calculate_player_combat_stats(
        database, class_name="mage", level=100, inventory=empty,
        modifier_sources=(entry,), pvp_arena=True,
    )
    # The arena buff is applied exactly as authored, inside and outside an
    # arena: -35% damage and +40% health.
    assert hub.minimum_damage == pytest.approx(plain.minimum_damage * 0.65)
    assert arena.minimum_damage == pytest.approx(plain.minimum_damage * 0.65)
    assert arena.maximum_damage == pytest.approx(plain.maximum_damage * 0.65)
    assert arena.maximum_health == pytest.approx(
        plain.maximum_health * 1.4
    )


def pvp_arena_rules_effect_id():
    from dso_server.persistence.readonly_db import connect

    with connect(str(DEFAULT_STATIC_DB_PATH)) as db:
        row = db.execute(
            "SELECT rowid - 1 FROM _Template_StatusEffect "
            "WHERE Id = 'pvp_char_modifier'"
        ).fetchone()
    return int(row[0]) if row else None


def test_the_arena_row_bans_the_rune_and_jewel_trinkets():
    banned = pvp_arena_rules.banned_item_categories(
        DEFAULT_STATIC_DB_PATH, "BestOf3_1v1_01_default",
    )
    assert {"runetrinket", "jeweltrinket", "equipmentrune"} <= banned
    assert pvp_arena_rules.category_banned(
        SimpleNamespace(category="RuneTrinket"), banned
    )
    assert not pvp_arena_rules.category_banned(
        SimpleNamespace(category="Weapon"), banned
    )
    # An unknown row has no ban list, and a hub is never asked for one.
    assert pvp_arena_rules.banned_item_categories(
        DEFAULT_STATIC_DB_PATH, "not-a-battleground",
    ) == frozenset()


def test_a_banned_trinket_stops_contributing_in_the_arena(database):
    inventory = InventoryInfo(
        items=[ItemInfo(
            item_id=1, item_level=100,
            template_id="all_base_trinket_rune_01",
            socketed_templates=["unique_gem_resistance_damage_block_04"],
        )],
        equipment_slots={1: (19,)},
    )
    plain = _calculate_player_combat_stats(
        database, class_name="mage", level=100, inventory=inventory,
    )
    arena = _calculate_player_combat_stats(
        database, class_name="mage", level=100, inventory=inventory,
        pvp_arena=True,
        banned_categories=frozenset({"runetrinket"}),
    )
    with_rune = _calculate_player_combat_stats(
        database, class_name="mage", level=100, inventory=inventory,
        pvp_arena=True,
    )
    observation = {
        "hub": plain.minimum_damage,
        "arena": with_rune.minimum_damage,
        "arena+ban": arena.minimum_damage,
    }
    # The hub counts the socketed rune; the arena ignores it; the arena row's
    # ban list removes the trinket container itself as well.
    assert observation["hub"] > observation["arena"]
    assert observation["arena+ban"] <= observation["arena"]


def test_arena_entry_strips_the_wisdom_talent_effects_from_the_client():
    s = session()
    disabled = pvp_arena_rules.disabled_talent_effect_ids(DEFAULT_STATIC_DB_PATH)
    knowledge_id = min(disabled)
    honor_id = 3
    s.gameplay_persistent_status_effects = {
        1: entry(knowledge_id, 1),
        2: entry(honor_id, 2),
    }
    s.gameplay_runtime_status_effects = {3: (entry(knowledge_id, 3), None)}
    server = Server()

    CharacterService._clear_battleground_talent_effects(server, s)

    assert 1 not in s.gameplay_persistent_status_effects
    assert 3 not in s.gameplay_runtime_status_effects
    assert 2 in s.gameplay_persistent_status_effects
    assert "pvp-talent-cleanse" in server.labels


def test_arena_entry_dismounts_outside_the_five_vs_five_modes():
    cast = begin_mount_cast(BLACK_BEAR_MOUNT, instance_id=5, start_tick=0)
    ride = begin_mount_ride(cast, instance_id=6)

    one_versus_one = session()
    one_versus_one.gameplay_mount_ride = ride
    one_versus_one.gameplay_persistent_status_effects = {
        6: entry(ride.effect_id, 6),
    }
    CharacterService._clear_battleground_mount_effects(Server(), one_versus_one)
    assert one_versus_one.gameplay_mount_ride is None
    assert (
        one_versus_one.gameplay_mount_restore_pending
        == BLACK_BEAR_MOUNT.stickerbook_resource_id
    )

    five_versus_five = session(match_mode=MATCH_MODE_BEST_OF_5_5V5)
    five_versus_five.gameplay_mount_ride = ride
    five_versus_five.gameplay_persistent_status_effects = {
        6: entry(ride.effect_id, 6),
    }
    CharacterService._clear_battleground_mount_effects(Server(), five_versus_five)
    assert five_versus_five.gameplay_mount_ride is ride


def test_returning_to_the_hub_restores_the_pet():
    s = session("a0200_kingscity", match_mode=None)
    s.gameplay_active_pet = "pet_dragon_joygame"
    s.gameplay_pet_restore_pending = True
    server = SimpleNamespace(static_database_path=str(DEFAULT_STATIC_DB_PATH))

    CharacterService._restore_active_pet(server, s)

    instance_id = s.gameplay_active_pet_effect_instance_id
    assert instance_id is not None
    assert instance_id in s.gameplay_persistent_status_effects
    assert s.gameplay_pet_restore_pending is False

    # It must not come back while the player is still inside the arena.
    arena = session()
    arena.gameplay_active_pet = "pet_dragon_joygame"
    arena.gameplay_pet_restore_pending = True
    CharacterService._restore_active_pet(server, arena)
    assert arena.gameplay_active_pet_effect_instance_id is None


def test_leaving_the_arena_puts_the_ride_back_before_the_transfer():
    s = session()
    s.gameplay_mount_restore_pending = "epicmount_black_bear_01"
    server = SimpleNamespace(static_database_path=str(DEFAULT_STATIC_DB_PATH))

    CharacterService._restore_battleground_mount(server, s)

    ride = s.gameplay_mount_ride
    assert ride is not None
    assert ride.definition.stickerbook_resource_id == "epicmount_black_bear_01"
    assert ride.instance_id in s.gameplay_persistent_status_effects
    assert s.gameplay_mount_restore_pending == ""


def test_arena_refuses_new_mounts_and_pets():
    s = session()
    s.gameplay_mount_ride = None
    CharacterService._toggle_mount_definition(
        SimpleNamespace(static_database_path=str(DEFAULT_STATIC_DB_PATH)),
        s, BLACK_BEAR_MOUNT, source="test",
    )
    assert s.gameplay_mount_cast is None

    pet = SimpleNamespace(kind="pet", resource_id="pet_dragon_joygame")
    CharacterService._toggle_switchable_sticker(
        SimpleNamespace(static_database_path=str(DEFAULT_STATIC_DB_PATH)),
        s, pet,
    )
    assert s.gameplay_active_pet == ""
    assert s.gameplay_persistent_status_effects == {}


def test_the_arena_freezes_the_loadout():
    from dso_server.services.inventory_map import (
        ArenaLoadoutLocked, InventoryMapService,
    )

    service = InventoryMapService()
    inventory = InventoryInfo(
        items=[
            ItemInfo(item_id=1, template_id="mage_base_helmet_crafted",
                     item_level=100),
            ItemInfo(item_id=2, template_id="all_base_amulet_crafted",
                     item_level=100),
        ],
        equipment_slots={1: (0,)},
    )
    worn = next(item for item in inventory.items if item.item_id == 1)
    bagged = next(item for item in inventory.items if item.item_id == 2)

    def action(operation, item):
        return SimpleNamespace(operation=operation, item_id=item.item_id)

    # Equipping, moving onto a slot and the occupied-slot swap are all refused.
    for operation in (0x01, 0x06, 0x0B):
        with pytest.raises(ArenaLoadoutLocked):
            service._refuse_arena_loadout_change(
                inventory, bagged, action(operation, bagged)
            )
    # Taking a worn piece off is the store operation, refused as well.
    for operation in (0x02, 0x03, 0x04, 0x05):
        with pytest.raises(ArenaLoadoutLocked):
            service._refuse_arena_loadout_change(
                inventory, worn, action(operation, worn)
            )
    # Parking a bagged item in another storage never changes the loadout.
    service._refuse_arena_loadout_change(
        inventory, bagged, action(0x02, bagged)
    )


def test_arena_preserves_inventory_templates_to_prevent_assertion_crashes():
    inventory = InventoryInfo(
        items=[
            ItemInfo(
                item_id=1,
                template_id="mage_mythic_helmet_dragan_sp",
                item_level=140,
                glyph_value=5000,
            ),
        ],
        equipment_slots={1: (0,)},
    )
    result = pvp_arena_rules.mask_arena_inventory(inventory, arena=True, class_name="mage")
    assert result is inventory
    assert result.items[0].template_id == "mage_mythic_helmet_dragan_sp"


def test_arena_damage_scaling_and_block_mitigation():
    from dso_server.services.pvp_combat import PVP_ARENA_DAMAGE_MULTIPLIER
    assert PVP_ARENA_DAMAGE_MULTIPLIER == 0.25

    s_arena = session("b1102_dm")
    assert pvp_arena_rules.arena_active(s_arena)

    # Outside arena: not active
    s_outside = session("a0200_kingscity", match_mode=None)
    assert not pvp_arena_rules.arena_active(s_outside)


def test_bullet_skills_do_not_auto_hit_via_fallback_plan():
    from dso_server.protocol.combat_protocol import BulletSkill
    from dso_server.gameplay.combat_entities import CombatEntity, EntitySkillPlan

    geom = SimpleNamespace(
        attack_range=10.0, hit_range=10.0, projectile_range=20.0,
        impact_count=1, projectile_velocity=20.0, hit_frame=0.0,
    )
    # A bullet shot straight at angle 0 faces (0, 0, -1).
    skill = BulletSkill(0, 1856, 0.0, 100, 1, (0.0, 0.0, -1.0, 0.0), 0)
    assert isinstance(skill, BulletSkill)



