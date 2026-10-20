"""Picking up Ancient Wisdom shows its loot-received text, as coins do."""

import unittest

from dso_server.protocol.character_selection import BitReader
from dso_server.protocol.inventory_codec import ItemInfo
from dso_server.protocol.world_loot_protocol import (
    DISCARD_ITEM_COMMAND,
    LOOT_RECEIVED_MESSAGE,
    MESSAGE_COMMAND,
    WISDOM_ITEM_TEMPLATE,
    WorldLootDrop,
    encode_wisdom_pickup_response,
)


class WisdomPickupTests(unittest.TestCase):
    def test_wisdom_pickup_sends_the_loot_text_then_the_discard(self):
        # It sent the DiscardItem alone: the client showed no violet text.
        drop = WorldLootDrop(
            item=ItemInfo(item_id=1234, amount=40, template_id=WISDOM_ITEM_TEMPLATE),
            item_category="Currency",
            auto_pick_up="All",
        )
        packet, bits = encode_wisdom_pickup_response(drop, actor_id=1)
        r = BitReader(packet, bits)
        self.assertEqual(r.read_uint8(), 0x85)
        self.assertEqual(int.from_bytes(r.read_bytes(2), "little"), MESSAGE_COMMAND)
        self.assertEqual(r.read_uint8(), LOOT_RECEIVED_MESSAGE)
        self.assertEqual(r.read_uint32_le(), 4)
        self.assertEqual((r.read_uint8(), r.read_uint32_le()), (1, 3), "GrindCurrency: Ancient Wisdom")
        self.assertEqual((r.read_uint8(), r.read_uint32_le()), (1, 40))
        self.assertEqual((r.read_uint8(), r.read_uint32_le()), (4, 1234))
        self.assertEqual((r.read_uint8(), r.read_string()), (2, WISDOM_ITEM_TEMPLATE))
        self.assertEqual(r.read_uint32_le(), 1)
        self.assertEqual(r.read_uint8(), 0xFF)
        self.assertEqual(int.from_bytes(r.read_bytes(2), "little"), DISCARD_ITEM_COMMAND)
        self.assertEqual(r.read_uint32_le(), 1234)

    def test_only_the_wisdom_item(self):
        drop = WorldLootDrop(item=ItemInfo(item_id=1, template_id="money_vc"), item_category="Currency", auto_pick_up="All")
        with self.assertRaises(ValueError):
            encode_wisdom_pickup_response(drop, actor_id=1)


if __name__ == "__main__":
    unittest.main()
