"""An item above the wearer's level + EquipItemLevelOffset does not act.

FAILURE: a level 7 in level 145 gear struck a level 100 for 196328 and took
5% of its blows.
"""

from dso_server.gameplay.player_combat_stats import calculate_player_combat_stats, equip_item_level_offset
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo

MACE = "warrior_unique_2h_mace_anniversary_2026"


def damage(level: int, item_level: int) -> float:
    inventory = InventoryInfo(items=[ItemInfo(item_id=1, template_id=MACE, amount=1, item_level=item_level)],
                              equipment_slots={1: (0,)})
    return calculate_player_combat_stats(DEFAULT_STATIC_DB_PATH, class_name="warrior", level=level,
                                         inventory=inventory).maximum_damage


def test_offset_is_the_databases():
    assert equip_item_level_offset(str(DEFAULT_STATIC_DB_PATH)) == 45


def test_a_weapon_acts_up_to_level_plus_45_only():
    bare = damage(7, 1000)
    assert damage(100, 145) > damage(7, 52) > bare
    assert damage(7, 53) == bare
