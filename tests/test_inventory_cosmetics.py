"""Costumes and pets can be worn straight from the bag.

Using a ``Switchable`` item used to fall through to the package/chest path
and be rejected as "not an inventory container"; only sticker-book
collectibles could be toggled. The toggle now resolves the cosmetic from the
sticker book first and from the item's own status effect otherwise.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dso_server.gameplay.stickerbook_state import load_cosmetic_definition
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import (
    InventoryItemAction,
    encode_inventory_item_action,
)
from dso_server.protocol.inventory_codec import (
    ItemInfo,
    decode_inventory_info,
    encode_bootstrap_inventory_info,
)
from dso_server.runtime.transport import Session
from dso_server.services.character_server import CharacterServer
from dso_server.tests.fixtures.characters import character_server


COLLECTIBLE_COSTUME = "e_dra_03_costume_cm_01"   # in the sticker book
PLAIN_COSTUME = "costume_dwarf_01"               # never a collectible
COSTUME_ITEM_ID = 910_001


class FakeSocket:
    def __init__(self) -> None:
        self.sent = []

    def sendto(self, data, address) -> None:
        self.sent.append((data, address))


class CosmeticDefinitionTests(unittest.TestCase):
    def test_collectible_costume_keeps_its_sticker_book_identity(self):
        definition = load_cosmetic_definition(DEFAULT_STATIC_DB_PATH, COLLECTIBLE_COSTUME)
        self.assertIsNotNone(definition)
        self.assertEqual(definition.kind, "costume")
        self.assertEqual(definition.status_effect_resource_id, "costume_cm_01")
        self.assertIsNotNone(definition.stickerbook_category_id)

    def test_plain_costume_resolves_from_its_item_row(self):
        definition = load_cosmetic_definition(DEFAULT_STATIC_DB_PATH, PLAIN_COSTUME)
        self.assertIsNotNone(definition)
        self.assertEqual(definition.kind, "costume")
        self.assertEqual(definition.status_effect_resource_id, PLAIN_COSTUME)
        self.assertIsNone(definition.stickerbook_category_id)
        self.assertEqual(definition.aliases, (PLAIN_COSTUME,))
        self.assertGreater(definition.duration_ticks, 0)

    def test_non_cosmetic_items_do_not_resolve(self):
        self.assertIsNone(
            load_cosmetic_definition(DEFAULT_STATIC_DB_PATH, "ammunition_damage_bgh")
        )


class UseCostumeFromInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = character_server(self)
        self.server.sock = FakeSocket()
        packet, bits = encode_bootstrap_inventory_info(
            actor_id=1, class_name="mage", item_level=100
        )
        inventory = decode_inventory_info(packet, bits)
        inventory.items.append(
            ItemInfo(item_id=COSTUME_ITEM_ID, template_id=COLLECTIBLE_COSTUME, amount=1, item_level=1)
        )
        inventory.storage_58[COSTUME_ITEM_ID] = 7
        self.session = Session(("127.0.0.1", 40900), self.server.sock)
        self.session.gameplay_mode = True
        self.session.gameplay_inventory = inventory
        self.session.gameplay_character = SimpleNamespace(
            class_id=1, character_id=0, level=100, name="Dresser"
        )

    def _use(self) -> str:
        action = InventoryItemAction(COSTUME_ITEM_ID, 0x13, 0, 0, 0)
        payload, _bits = encode_inventory_item_action(action)
        output = io.StringIO()
        with patch.object(CharacterServer, "_persist_gameplay_session") as persist, \
                contextlib.redirect_stdout(output):
            self.server._handle_inventory_item_action(self.session, payload)
        self.assertTrue(persist.called)
        return output.getvalue()

    def _costume_effects(self):
        definition = load_cosmetic_definition(DEFAULT_STATIC_DB_PATH, COLLECTIBLE_COSTUME)
        return [
            effect for effect in self.session.gameplay_persistent_status_effects.values()
            if effect.effect_id == definition.status_effect_wire_id
        ]

    def test_using_the_costume_wears_it_without_consuming_it(self):
        output = self._use()
        self.assertNotIn("not an inventory container", output)
        self.assertIn("toggled costume", output)
        self.assertEqual(self.session.gameplay_active_costume, COLLECTIBLE_COSTUME)
        self.assertEqual(len(self._costume_effects()), 1)
        self.assertTrue(self.server.sock.sent, "the status effect must reach the client")
        inventory = self.session.gameplay_inventory
        self.assertEqual(inventory.storage_58.get(COSTUME_ITEM_ID), 7)
        self.assertEqual(
            next(item for item in inventory.items if item.item_id == COSTUME_ITEM_ID).amount,
            1,
        )

    def test_using_it_again_takes_it_off(self):
        self._use()
        self._use()
        self.assertEqual(self.session.gameplay_active_costume, "")
        self.assertEqual(self._costume_effects(), [])


if __name__ == "__main__":
    unittest.main()
