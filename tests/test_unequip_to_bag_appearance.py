"""Taking an item off into a bag cell (MoveItem 0x07) tells the other players.

FAILURE: "je retire mon arme l'autre me voit toujours avec l'arme": the
client's drop on a bag cell sends 0x07, which only moved the allocation.
"""

from dso_server.protocol.gameplay_protocol import InventoryItemAction, encode_inventory_item_action
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.tests import test_achievements as achievement_fixtures


def test_a_worn_weapon_moved_to_a_bag_cell_updates_the_appearance():
    case = achievement_fixtures.AchievementServiceTests()
    case.setUp()
    try:
        server, session = case.server, case.session
        session.gameplay_inventory = InventoryInfo(
            items=[ItemInfo(item_id=51, template_id="warrior_unique_2h_mace_anniversary_2026", amount=1, item_level=10)],
            equipment_slots={51: (5, 6)}, storage_size=28,
        )
        session.gameplay_next_item_id = 52
        events = []
        session.gameplay_presence_callback = lambda s, event: events.append(event)
        payload, _ = encode_inventory_item_action(InventoryItemAction(51, 0x107, 0, 3, 0))
        server._handle_inventory_item_action(session, payload)
        assert 51 not in session.gameplay_inventory.equipment_slots
        assert session.gameplay_inventory.storage_58.get(51) == 3
        assert "appearance" in events
    finally:
        case.doCleanups()
