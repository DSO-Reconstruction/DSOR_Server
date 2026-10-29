"""Essences use the client's slot pair: 35 is active, 36 is the parked spare.

The live client sends slot 36 when switching to the parked essence. Older
server builds incorrectly used slot 34, so those saved allocations migrate
to 36. Only the essence in slot 35 may act, and switching into an occupied
partner slot swaps both items.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from types import SimpleNamespace

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.player_combat_stats import calculate_player_combat_stats
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import (
    MOVE_ITEM_EQUIPMENT_OPERATION,
    InventoryItemAction,
    encode_inventory_item_action,
)
from dso_server.protocol.inventory_codec import (
    ACTIVE_AMMUNITION_SLOT,
    LEGACY_SECONDARY_AMMUNITION_SLOT,
    SECONDARY_AMMUNITION_SLOT,
    InventoryInfo,
    ItemInfo,
    active_equipment_slots,
    decode_inventory_info,
    encode_bootstrap_inventory_info,
    repair_ammunition_slot_allocations,
)
from dso_server.runtime.transport import Session
from dso_server.tests.fixtures.characters import character_server


SNOW = "e_chr_2012_snow_ammunition"  # DamageFactorPvE 1.0 relative, AmmoType02
BGH = "ammunition_damage_bgh"         # DamageFactorPvE 6.0 relative
SNOW_ID = 900_001
BGH_ID = 900_002


class FakeSocket:
    def __init__(self) -> None:
        self.sent = []

    def sendto(self, data, address) -> None:
        self.sent.append((data, address))


class SlotResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = character_server(self).static_database

    def resolve(self, **kwargs):
        return self.database.item_equipment_slots(SNOW, **kwargs)

    def test_lone_essence_takes_the_active_slot(self):
        self.assertEqual(self.resolve(), (ACTIVE_AMMUNITION_SLOT,))
        self.assertEqual(ACTIVE_AMMUNITION_SLOT, 35)

    def test_second_essence_takes_the_secondary_slot(self):
        self.assertEqual(
            self.resolve(occupied_slots=(35,)), (SECONDARY_AMMUNITION_SLOT,)
        )
        self.assertEqual(SECONDARY_AMMUNITION_SLOT, 36)

    def test_explicit_requests_for_either_slot_are_honoured(self):
        self.assertEqual(self.resolve(preferred_slots=(36,)), (36,))
        self.assertEqual(self.resolve(preferred_slots=(35,)), (35,))

    def test_slot_34_is_no_longer_an_essence_slot(self):
        self.assertNotIn(34, self.resolve(preferred_slots=(34,)))
        self.assertNotIn(34, self.resolve(occupied_slots=(35, 36)))


class ActiveAllocationTests(unittest.TestCase):
    def test_parked_essence_is_hidden_from_effects(self):
        inventory = SimpleNamespace(
            equipment_slots={1: (35,), 2: (36,), 3: (0,), 4: (5, 6)}
        )
        self.assertEqual(
            active_equipment_slots(inventory), {1: (35,), 3: (0,), 4: (5, 6)}
        )


def essence(item_id: int, template_id: str) -> SimpleNamespace:
    return SimpleNamespace(item_id=item_id, template_id=template_id, item_level=1)


class CombatStatsTests(unittest.TestCase):
    def stats(self, items, allocations):
        return calculate_player_combat_stats(
            DEFAULT_STATIC_DB_PATH, class_name="mage", level=100,
            inventory=SimpleNamespace(items=items, equipment_slots=allocations),
            modifier_sources=(),
        )

    def test_only_the_active_essence_multiplies_damage(self):
        baseline = self.stats([], {})
        bgh_alone = self.stats([essence(1, BGH)], {1: (35,)})
        self.assertAlmostEqual(
            bgh_alone.maximum_damage, baseline.maximum_damage * 7.0, places=3
        )
        both = self.stats(
            [essence(1, BGH), essence(2, SNOW)], {1: (35,), 2: (36,)}
        )
        self.assertAlmostEqual(
            both.maximum_damage, bgh_alone.maximum_damage, places=3
        )
        snow_active = self.stats(
            [essence(1, BGH), essence(2, SNOW)], {1: (36,), 2: (35,)}
        )
        self.assertAlmostEqual(
            snow_active.maximum_damage, baseline.maximum_damage * 2.0, places=3
        )

    @staticmethod
    def active_ammunition_types(sources):
        # Essences supply AmmunitionType through their item modifiers; only
        # status entries would need a combat state's StartModifiers catalog.
        no_programs = SimpleNamespace(_status_modifier_program=lambda _effect_id: ())
        return LocalCombatState._active_ammunition_types(no_programs, sources)

    def test_only_the_active_essence_supplies_its_ammunition_type(self):
        snow_active = self.stats(
            [essence(1, BGH), essence(2, SNOW)], {1: (36,), 2: (35,)}
        )
        self.assertEqual(
            self.active_ammunition_types(snow_active.skill_modifier_sources),
            frozenset({"ammotype02"}),
        )
        snow_parked = self.stats(
            [essence(1, BGH), essence(2, SNOW)], {1: (35,), 2: (36,)}
        )
        self.assertEqual(
            self.active_ammunition_types(snow_parked.skill_modifier_sources),
            frozenset(),
        )


class LegacySlotRepairTests(unittest.TestCase):
    def test_slot_34_moves_to_the_secondary_slot(self):
        inventory = InventoryInfo(equipment_slots={7: (34,), 8: (35,)})
        repaired, moved = repair_ammunition_slot_allocations(inventory)
        self.assertEqual(moved, (7,))
        self.assertEqual(repaired.equipment_slots[7], (SECONDARY_AMMUNITION_SLOT,))
        self.assertEqual(repaired.equipment_slots[8], (35,))
        # The input is left untouched.
        self.assertEqual(inventory.equipment_slots[7], (34,))

    def test_slot_34_goes_to_the_bag_when_the_pair_is_full(self):
        inventory = InventoryInfo(
            equipment_slots={7: (34,), 8: (35,), 9: (36,)},
            storage_58={100: 0},
        )
        inventory.storage_size = 10
        repaired, moved = repair_ammunition_slot_allocations(inventory)
        self.assertEqual(moved, (7,))
        self.assertNotIn(7, repaired.equipment_slots)
        self.assertEqual(repaired.storage_58[7], 1)

    def test_nothing_to_repair_returns_the_same_object(self):
        inventory = InventoryInfo(equipment_slots={8: (35,)})
        repaired, moved = repair_ammunition_slot_allocations(inventory)
        self.assertIs(repaired, inventory)
        self.assertEqual(moved, ())
        self.assertEqual(LEGACY_SECONDARY_AMMUNITION_SLOT, 34)


class EquipHandlerTests(unittest.TestCase):
    """Drive the real inventory action handler through the essence flows."""

    def setUp(self) -> None:
        self.server = character_server(self)
        self.server.sock = FakeSocket()

    def _session(self, *, snow_at, bgh_at) -> Session:
        packet, bits = encode_bootstrap_inventory_info(
            actor_id=1, class_name="mage", item_level=100
        )
        inventory = decode_inventory_info(packet, bits)
        inventory.items.append(ItemInfo(item_id=SNOW_ID, template_id=SNOW, amount=40, item_level=1))
        inventory.items.append(ItemInfo(item_id=BGH_ID, template_id=BGH, amount=40, item_level=1))
        for item_id, where in ((SNOW_ID, snow_at), (BGH_ID, bgh_at)):
            if isinstance(where, tuple):
                inventory.equipment_slots[item_id] = where
            else:
                inventory.storage_58[item_id] = where
        session = Session(("127.0.0.1", 40800), self.server.sock)
        session.gameplay_mode = True
        session.gameplay_inventory = inventory
        session.gameplay_character = SimpleNamespace(
            class_id=1, character_id=0, level=100, name="Essence"
        )
        session.gameplay_next_item_id = 1_000_000
        return session

    def _act(self, session, item_id, operation, argument) -> str:
        action = InventoryItemAction(
            item_id, int(operation) | (1 << 8), 0, int(argument), 0
        )
        payload, _bits = encode_inventory_item_action(action)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.server._handle_inventory_item_action(session, payload)
        return output.getvalue()

    @staticmethod
    def _where(session, item_id):
        inventory = session.gameplay_inventory
        if item_id in inventory.equipment_slots:
            return inventory.equipment_slots[item_id]
        return ("bag", inventory.storage_58.get(item_id))

    def test_live_sequence_swap_then_move_to_secondary_is_accepted(self):
        session = self._session(snow_at=5, bgh_at=(35,))
        first = self._act(session, SNOW_ID, 0x0B, BGH_ID)
        self.assertNotIn("rejected", first)
        self.assertEqual(self._where(session, SNOW_ID), (35,))
        # The essence that left the active slot takes the mover's bag slot.
        self.assertEqual(self._where(session, BGH_ID), ("bag", 5))
        second = self._act(session, SNOW_ID, 0x01, SECONDARY_AMMUNITION_SLOT)
        self.assertNotIn("rejected", second)
        self.assertEqual(self._where(session, SNOW_ID), (36,))
        self.assertEqual(self._where(session, BGH_ID), ("bag", 5))

    def test_swap_between_the_pair_keeps_both_equipped(self):
        session = self._session(snow_at=(36,), bgh_at=(35,))
        output = self._act(session, SNOW_ID, 0x0B, BGH_ID)
        self.assertNotIn("rejected", output)
        self.assertEqual(self._where(session, SNOW_ID), (35,))
        self.assertEqual(self._where(session, BGH_ID), (36,))
        self.assertIn(f"swapped_into_former_slot=({BGH_ID},)", output)

    def test_moving_the_active_essence_onto_the_spare_swaps_them(self):
        session = self._session(snow_at=(35,), bgh_at=(36,))
        output = self._act(
            session, SNOW_ID, MOVE_ITEM_EQUIPMENT_OPERATION, SECONDARY_AMMUNITION_SLOT
        )
        self.assertNotIn("rejected", output)
        self.assertEqual(self._where(session, SNOW_ID), (36,))
        self.assertEqual(self._where(session, BGH_ID), (35,))

    def test_equipping_from_the_bag_still_sends_the_displaced_essence_to_the_bag(self):
        session = self._session(snow_at=3, bgh_at=(36,))
        output = self._act(session, SNOW_ID, 0x01, SECONDARY_AMMUNITION_SLOT)
        self.assertNotIn("rejected", output)
        self.assertEqual(self._where(session, SNOW_ID), (36,))
        self.assertEqual(self._where(session, BGH_ID), ("bag", 3))


if __name__ == "__main__":
    unittest.main()
