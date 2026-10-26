"""Every accepted cast spends the active essence's ``AmmoCost``.

The essence in slot 35 shrinks by the skill's authored cost, the client gets
an ``InventoryInfo`` delta with the reduced stack, and when the stack is used
up the item and its damage factor are gone. The parked essence in slot 36,
zero-cost skills and battlegrounds spend nothing.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.inventory_codec import (
    ItemInfo,
    consume_equipped_stack,
    decode_inventory_info,
    encode_bootstrap_inventory_info,
)
from dso_server.protocol.player_init_protocol import (
    INVENTORY_INFO_COMMAND,
    decode_server_command_batch,
)
from dso_server.runtime.transport import Session
from dso_server.services import player_resources as resources_module
from dso_server.services.base import CharacterService
from dso_server.services.character_server import CharacterServer
from dso_server.tests.fixtures.characters import character_server


ICE_MISSILE = 1849    # AmmoCost 2
MAGIC_MISSILE = 1847  # AmmoCost 1
FIREBALL = 1848       # AmmoCost 3
ACTIVE_ID = 920_001
PARKED_ID = 920_002
SNOW = "e_chr_2012_snow_ammunition"
BGH = "ammunition_damage_bgh"


def _skill_index(resource_id: str) -> int:
    database = sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True)
    try:
        row = database.execute(
            "SELECT rowid - 1 FROM _Template_Skill "
            "WHERE Id=? AND lower(CharClass)='mage' LIMIT 1",
            (resource_id,),
        ).fetchone()
    finally:
        database.close()
    return int(row[0])


class FakeSocket:
    def __init__(self) -> None:
        self.sent = []

    def sendto(self, data, address) -> None:
        self.sent.append((data, address))


class AmmoCostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        encounter = load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False
        )
        cls.combat = LocalCombatState(encounter, player_actor_id=1, player_health=1000)

    def test_authored_costs_are_read_from_the_skill_table(self):
        self.assertEqual(self.combat.player_skill_ammo_cost(MAGIC_MISSILE), 1)
        self.assertEqual(self.combat.player_skill_ammo_cost(ICE_MISSILE), 2)
        self.assertEqual(self.combat.player_skill_ammo_cost(FIREBALL), 3)
        self.assertEqual(self.combat.player_skill_ammo_cost(_skill_index("teleport")), 0)
        self.assertEqual(self.combat.player_skill_ammo_cost(999_999), 0)


class ConsumeEquippedStackTests(unittest.TestCase):
    def _inventory(self, amount: int):
        packet, bits = encode_bootstrap_inventory_info(actor_id=1, class_name="mage", item_level=100)
        inventory = decode_inventory_info(packet, bits)
        inventory.items.append(ItemInfo(item_id=ACTIVE_ID, template_id=SNOW, amount=amount, item_level=1))
        inventory.equipment_slots[ACTIVE_ID] = (35,)
        return inventory

    def test_partial_spend_keeps_the_item_id_and_publishes_the_stack(self):
        inventory = self._inventory(5)
        updated, delta, depleted = consume_equipped_stack(inventory, ACTIVE_ID, 2)
        self.assertFalse(depleted)
        self.assertEqual(
            next(item.amount for item in updated.items if item.item_id == ACTIVE_ID), 3
        )
        self.assertEqual(updated.equipment_slots[ACTIVE_ID], (35,))
        self.assertEqual([item.item_id for item in delta.items], [ACTIVE_ID])
        self.assertEqual(delta.items[0].amount, 3)
        self.assertEqual(delta.changed_item_ids, [])
        self.assertTrue(delta.field_1e0)
        # The source snapshot is untouched.
        self.assertEqual(
            next(item.amount for item in inventory.items if item.item_id == ACTIVE_ID), 5
        )

    def test_spending_the_last_essence_drops_the_item_and_its_slot(self):
        inventory = self._inventory(2)
        updated, delta, depleted = consume_equipped_stack(inventory, ACTIVE_ID, 2)
        self.assertTrue(depleted)
        self.assertFalse(any(item.item_id == ACTIVE_ID for item in updated.items))
        self.assertNotIn(ACTIVE_ID, updated.equipment_slots)
        self.assertEqual(delta.items, [])
        self.assertEqual(delta.changed_item_ids, [ACTIVE_ID])

    def test_overspending_is_refused(self):
        with self.assertRaises(ValueError):
            consume_equipped_stack(self._inventory(1), ACTIVE_ID, 2)
        with self.assertRaises(ValueError):
            consume_equipped_stack(self._inventory(1), ACTIVE_ID, 0)


class CastConsumptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        encounter = load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False
        )
        cls.combat = LocalCombatState(encounter, player_actor_id=1, player_health=1000)

    def setUp(self) -> None:
        self.server = character_server(self)
        self.server.sock = FakeSocket()
        packet, bits = encode_bootstrap_inventory_info(actor_id=1, class_name="mage", item_level=100)
        inventory = decode_inventory_info(packet, bits)
        inventory.items.append(ItemInfo(item_id=ACTIVE_ID, template_id=SNOW, amount=5, item_level=1))
        inventory.items.append(ItemInfo(item_id=PARKED_ID, template_id=BGH, amount=5, item_level=1))
        inventory.equipment_slots[ACTIVE_ID] = (35,)
        inventory.equipment_slots[PARKED_ID] = (34,)
        self.session = Session(("127.0.0.1", 41100), self.server.sock)
        self.session.gameplay_mode = True
        self.session.gameplay_inventory = inventory
        self.session.gameplay_combat_state = self.combat
        self.session.gameplay_character = SimpleNamespace(
            class_id=1, character_id=0, level=100, name="Caster"
        )
        self.sent = []
        self.saves = []
        self.refreshes = []
        patchers = (
            patch.object(
                CharacterServer, "_send_world_loot_state_batch",
                lambda s, packet, bits, *, label: self.sent.append((packet, bits, label)),
            ),
            patch.object(
                CharacterServer, "_request_account_save",
                lambda s, reason, flush=False: self.saves.append((reason, flush)) or True,
            ),
            patch.object(
                CharacterServer, "_refresh_player_combat_vitals",
                lambda s, **kwargs: self.refreshes.append(s),
            ),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _amount(self, item_id):
        inventory = self.session.gameplay_inventory
        return next((item.amount for item in inventory.items if item.item_id == item_id), None)

    def _consume(self, skill_index=ICE_MISSILE) -> bool:
        with contextlib.redirect_stdout(io.StringIO()):
            return CharacterService._consume_active_essence(
                self.session, self.combat, skill_index=skill_index
            )

    def _last_delta(self):
        packet, bits, label = self.sent[-1]
        self.assertEqual(label, "essence-consumed")
        commands = decode_server_command_batch(packet, bits)
        return next(command.value for command in commands if command.command_id == INVENTORY_INFO_COMMAND)

    def test_an_accepted_cast_spends_the_active_essence_only(self):
        self.assertTrue(self._consume())
        self.assertEqual(self._amount(ACTIVE_ID), 3)
        self.assertEqual(self._amount(PARKED_ID), 5)
        delta = self._last_delta()
        self.assertEqual([(item.item_id, item.amount) for item in delta.items], [(ACTIVE_ID, 3)])
        self.assertEqual(self.saves, [("essence consumed", False)])
        self.assertEqual(self.refreshes, [])

    def test_the_last_essence_is_used_up_and_the_damage_factor_drops(self):
        self._consume()            # 5 -> 3
        self._consume()            # 3 -> 1
        self.assertTrue(self._consume())  # 1 -> gone (cost 2 takes the remainder)
        self.assertIsNone(self._amount(ACTIVE_ID))
        self.assertNotIn(ACTIVE_ID, self.session.gameplay_inventory.equipment_slots)
        self.assertEqual(self.session.gameplay_inventory.equipment_slots[PARKED_ID], (34,))
        self.assertEqual(self._last_delta().changed_item_ids, [ACTIVE_ID])
        self.assertEqual(len(self.refreshes), 1)
        self.assertEqual(self.saves[-1], ("essence depleted", True))
        # Nothing left to spend.
        self.assertFalse(self._consume())

    def test_zero_cost_skills_spend_nothing(self):
        self.assertFalse(self._consume(_skill_index("teleport")))
        self.assertEqual(self._amount(ACTIVE_ID), 5)
        self.assertEqual(self.sent, [])

    def test_no_active_essence_spends_nothing(self):
        self.session.gameplay_inventory.equipment_slots.pop(ACTIVE_ID)
        self.assertFalse(self._consume())
        self.assertEqual(self._amount(PARKED_ID), 5)

    def test_battlegrounds_spend_nothing(self):
        self.session.gameplay_match = object()
        self.assertFalse(self._consume())
        self.assertEqual(self._amount(ACTIVE_ID), 5)

    def test_the_switch_disables_consumption(self):
        with patch.object(resources_module, "ESSENCE_CONSUMPTION_ENABLED", False):
            self.assertFalse(self._consume())
        self.assertEqual(self._amount(ACTIVE_ID), 5)

    def test_a_concurrent_inventory_change_is_not_overwritten(self):
        original = self.session.gameplay_inventory

        def swap_inventory(*_args, **_kwargs):
            # Simulate another handler replacing the inventory mid-spend.
            self.session.gameplay_inventory = replacement
            return original.items[0].item_id if False else None

        replacement = decode_inventory_info(
            *encode_bootstrap_inventory_info(actor_id=1, class_name="mage", item_level=100)
        )
        with patch.object(
            resources_module, "consume_equipped_stack",
            side_effect=lambda inventory, item_id, amount: (
                swap_inventory(), consume_equipped_stack(inventory, item_id, amount)
            )[1],
        ):
            self.assertFalse(self._consume())
        self.assertIs(self.session.gameplay_inventory, replacement)
        self.assertEqual(self.sent, [])


class CastWiringTests(unittest.TestCase):
    """The real bullet handler spends essence once an ingress cast is accepted."""

    def test_bullet_cast_reaches_the_essence_step(self):
        from dso_server.tests import test_combat_entities as entities

        class Fixture(entities.MixedEntityCastTests):
            __test__ = False

            def runTest(self):
                pass

        fixture = Fixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        spent = []
        with patch.object(
            CharacterService, "_consume_active_essence",
            side_effect=lambda s, combat, *, skill_index: spent.append(skill_index) or True,
        ):
            fixture.cast()
        self.assertEqual(spent, [ICE_MISSILE])


if __name__ == "__main__":
    unittest.main()
