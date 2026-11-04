"""The Night Walker (niwalk, class 4) is created, enters the world and plays.

This content export authors nothing class-specific for it, so it borrows the
warrior's start equipment, quests and loot, and plays its blood like rage.
"""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dso_server.gameplay.map_event_state import load_map_signal_catalog
from dso_server.gameplay.player_combat_stats import calculate_player_combat_stats
from dso_server.gameplay.quest_content import QuestReward, load_quest_catalog
from dso_server.paths import DEFAULT_QUESTS_PATH, DEFAULT_STATIC_DB_PATH
from dso_server.persistence.static_game_database import StaticGameDatabase
from dso_server.protocol.character_generation import (
    CharacterGeneration, encode_character_generation,
)
from dso_server.protocol.character_selection import CLASS_IDS
from dso_server.protocol.inventory_codec import (
    InventoryInfo, decode_inventory_info, encode_bootstrap_inventory_info,
)
from dso_server.protocol.world_loot_protocol import generate_monster_loot
from dso_server.runtime.transport import Session
from dso_server.services.presence_visuals import PresenceVisualService
from dso_server.tests.fixtures.characters import character_server

NIWALK = CLASS_IDS["niwalk"]
WARRIOR_CORE = {
    "warrior_base_helmet_crafted": (0,),
    "warrior_base_shoulders_crafted": (1,),
    "warrior_base_torso_crafted": (2,),
    "warrior_base_gloves_crafted": (3,),
    "warrior_base_boots_crafted": (4,),
    "warrior_base_rh_sword_speedAttack_damage": (5,),
    "warrior_base_lh_shield_armor_healthpoints_resistanceAll": (6,),
    "warrior_base_backitem_defense_crafted": (7,),
    "warrior_base_weaponmod_crafted": (12,),
}


class FakeSocket:
    def __init__(self) -> None:
        self.sent = []

    def sendto(self, data, address) -> None:
        self.sent.append((data, address))


def _equipped(inventory) -> dict[str, tuple[int, ...]]:
    return {
        item.template_id: tuple(inventory.equipment_slots[item.item_id])
        for item in inventory.items
        if item.item_id in inventory.equipment_slots
    }


class NiwalkCreationTests(unittest.TestCase):
    def _create(self, server, *, gender=0, name="Nightwalker"):
        session = Session(("127.0.0.1", 41234), Mock())
        session.send_reliable_ordered = Mock()
        request = CharacterGeneration(
            operation=3, class_id=NIWALK, gender=gender, name=name,
        )
        payload, bit_length = encode_character_generation(
            request, server_to_client=False,
        )
        session.current_message_bit_length = bit_length
        with patch.object(server, "arm_gameplay") as arm, patch.object(
            server, "_send_character_generation_packet"
        ) as send:
            server._handle_character_generation(session, payload)
        return send, arm

    def test_created_character_enters_the_world_in_warrior_gear(self):
        server = character_server(self, profiles=())
        server.sock = FakeSocket()
        send, arm = self._create(server)
        self.assertEqual(send.call_args.args[1].operation, 5)  # Created.
        character = server.character_selection.active_character
        self.assertEqual(character.class_id, NIWALK)
        self.assertEqual(character.map_id, "a0001_start_tutorial_dun")
        # The niwalk's own player template row (human_male_niwalk).
        self.assertEqual(arm.call_args.args[3], 4)
        # The character list shows the warrior's start equipment.
        warrior_preview = StaticGameDatabase(
            DEFAULT_STATIC_DB_PATH
        ).character_generation_equipment(CLASS_IDS["warrior"])
        self.assertTrue(warrior_preview)
        self.assertEqual(
            [record.item_id for record in server.character_selection.equipment],
            [record.item_id for record in warrior_preview],
        )

        session = Session(("127.0.0.1", 41010), server.sock)
        session.gameplay_mode = True
        session.gameplay_map = character.map_id
        session.gameplay_character = character
        server._send_local_player_initialization(session)
        equipped = _equipped(session.gameplay_inventory)
        for template_id, slots in WARRIOR_CORE.items():
            self.assertEqual(equipped.get(template_id), slots, template_id)
        combat = session.gameplay_combat_state
        actor_id = combat.player_actor_id
        self.assertEqual(combat.player_resource[actor_id][1], 100.0)
        fraction, absolute, interval, delay = (
            combat.player_resource_regeneration[actor_id]
        )
        self.assertAlmostEqual(fraction, -0.05)
        self.assertEqual((absolute, interval, delay), (0.0, 1.0, 5.0))

    def test_bootstrap_inventory_is_the_warriors(self):
        packet, bits = encode_bootstrap_inventory_info(class_name="niwalk")
        warrior, warrior_bits = encode_bootstrap_inventory_info(
            class_name="warrior"
        )
        self.assertEqual(
            _equipped(decode_inventory_info(packet, bits)),
            _equipped(decode_inventory_info(warrior, warrior_bits)),
        )


class NiwalkBloodTests(unittest.TestCase):
    @staticmethod
    def _resource(class_name):
        stats = calculate_player_combat_stats(
            DEFAULT_STATIC_DB_PATH, class_name=class_name, level=1,
            inventory=InventoryInfo(),
        )
        return (
            stats.maximum_resource,
            stats.resource_regeneration_fraction,
            stats.resource_regeneration_absolute,
            stats.resource_regeneration_interval,
            stats.resource_regeneration_delay,
        )

    def test_blood_recovers_like_rage(self):
        niwalk = self._resource("niwalk")
        self.assertEqual(niwalk, self._resource("warrior"))
        # Not the data's mana-like Absolute 3/s: it drains after a delay.
        self.assertEqual(niwalk[2], 0.0)
        self.assertLess(niwalk[1], 0.0)
        self.assertGreater(niwalk[4], 0.0)


class NiwalkAnimationTests(unittest.TestCase):
    def test_base_set_is_niwalk_off_armed_or_not(self):
        armed = SimpleNamespace(equipment_slots={1: (5,), 2: (6,)})
        unarmed = SimpleNamespace(equipment_slots={})
        for gender, expected in ((0, "niwalk_off"), (1, "niwalk_female_off")):
            character = SimpleNamespace(class_id=NIWALK, gender=gender)
            for inventory in (armed, unarmed, None):
                with self.subTest(gender=gender, inventory=inventory):
                    self.assertEqual(
                        PresenceVisualService._remote_animation_for(
                            character, inventory, "male"
                        ),
                        expected,
                    )


class NiwalkContentTests(unittest.TestCase):
    def test_follows_the_warrior_tutorial(self):
        catalog = load_quest_catalog(DEFAULT_QUESTS_PATH)
        self.assertTrue(
            catalog.quest("a0001-start-tutorial-01-w").accepts_class("niwalk")
        )
        self.assertFalse(
            catalog.quest("a0001-start-tutorial-01-m").accepts_class("niwalk")
        )
        rule = load_map_signal_catalog(DEFAULT_STATIC_DB_PATH).rule(
            "a0001_enter_map_quest_signal_warrior"
        )
        self.assertTrue(rule.accepts(
            map_id="a0001_start_tutorial_dun", running_events=frozenset(),
            player_level=1, class_name="niwalk", player_count=1,
        ))

    def test_own_quest_rewards_win_over_the_warriors(self):
        rewards_for = type(load_quest_catalog(DEFAULT_QUESTS_PATH).quest(
            "a0001-start-tutorial-01-w"
        )).rewards_for_class
        warrior = QuestReward("loot", "for_warrior", 1, class_name="warrior")
        niwalk = QuestReward("loot", "for_niwalk", 1, class_name="niwalk")
        shared = QuestReward("xp", "", 10)
        both = SimpleNamespace(rewards=(warrior, niwalk, shared))
        only_warrior = SimpleNamespace(rewards=(warrior, shared))
        self.assertEqual(rewards_for(both, "niwalk"), (niwalk, shared))
        self.assertEqual(rewards_for(both, "warrior"), (warrior, shared))
        self.assertEqual(rewards_for(only_warrior, "niwalk"), (warrior, shared))

    def test_monster_loot_rolls_for_a_niwalk(self):
        loot = generate_monster_loot(
            DEFAULT_STATIC_DB_PATH, "a0001_champion_undead_mage_01",
            class_name="niwalk", player_level=1, position=(0.0, 0.0, 0.0),
            next_item_id=1, force_drops=True,
        )
        self.assertIsNotNone(loot)


if __name__ == "__main__":
    unittest.main()
