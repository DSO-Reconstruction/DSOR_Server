"""Event-map amphorae: key cost, server-owned loot lists and drop placement.

Retail reference, 2026-10-28 capture on ``e_dra_03_dun_01`` (Dragan event):
clicking a ``chest_amphora_normal`` spent one ``e_sar_chest_key`` (302 -> 301,
same item id) and spawned seven NewItems at the player's position on a 0.66 m
grid (10 Draken, three gold stacks, the progress key x3 and x50, one
``e_dra_03_hardmode_bosskey``), then PropUpdate state 3. Draken and the key
were auto-picked with loot text kinds 7 and 6.
"""

from __future__ import annotations

import json
from pathlib import Path
import random
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dso_server.gameplay.event_system import load_event_catalog
from dso_server.gameplay.local_map_state import load_map_coordinate_frame
from dso_server.gameplay.local_monster_state import load_local_player_only_encounter
from dso_server.gameplay.map_event_state import load_map_event_content
from dso_server.gameplay.prop_loot import (
    chest_drop_positions,
    load_prop_loot_catalog,
    roll_prop_loot,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.commerce_protocol import _new_item, load_purchase_item_definition
from dso_server.protocol.gameplay_protocol import Move
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.player_init_protocol import decode_server_command_batch
from dso_server.protocol.prop_protocol import (
    PROP_STATE_ACTIVE,
    PROP_STATE_USED,
    PROP_UPDATE_COMMAND,
    PropInteraction,
    decode_prop_update_batch,
    encode_prop_interaction,
)
from dso_server.protocol.world_loot_protocol import (
    INVENTORY_INFO_COMMAND,
    WorldLootDrop,
    add_world_drop_to_inventory,
    decode_world_pickup_response,
    encode_world_pickup_response,
    inventory_loot_text_type,
)
from dso_server.runtime.transport import Session
from dso_server.services.character_server import CharacterServer
from dso_server.tests.test_pvp_matchmaking import _FakeSocket, _Recorder, _quiet_server


DRAGAN_MAP = "e_dra_03_dun_01"
DRAGAN_KEY = "e_dra_02_RiseOfDragan_key_for_maps_01"
CHEST_KEY = "e_sar_chest_key"


class PropLootCatalogTests(unittest.TestCase):
    def test_retail_amphora_spot_is_a_one_key_normal_amphora(self):
        content = load_map_event_content(DEFAULT_STATIC_DB_PATH, DRAGAN_MAP)
        # Client builds regenerate instance GUIDs; the row is the stable key.
        amphora = next(
            prop for prop in content.props
            if prop.template_id == "chest_amphora_normal" and prop.row_id == 26
        )
        self.assertEqual(
            (amphora.template_id, amphora.loot_list_id, amphora.key_id,
             amphora.key_amount, amphora.individual_usage),
            ("chest_amphora_normal", "CHEST_amphora_normal_MAIN", CHEST_KEY, 1, True),
        )
        # Always present in the scene, yet published like retail did.
        self.assertTrue(amphora.always_present)
        self.assertTrue(amphora.network_prop)

    def test_every_list_names_shipped_items_and_aliases_resolve(self):
        catalog = load_prop_loot_catalog()
        import sqlite3
        with sqlite3.connect(f"file:{Path(DEFAULT_STATIC_DB_PATH).as_posix()}?mode=ro", uri=True) as db:
            items = {row[0] for row in db.execute("SELECT Id FROM _Template_Item")}
            lists = {
                str(row[0]).casefold() for row in db.execute(
                    "SELECT LootListId FROM _Template_StateObject WHERE LootListId<>''"
                )
            }
        for entries in catalog.lists.values():
            for entry in entries:
                self.assertTrue(entry.event_progress or entry.item_id in items, entry)
        self.assertTrue(set(catalog.aliases) <= lists)
        self.assertIs(
            catalog.entries("e_dra_03_event_chest_golden"),
            catalog.entries("CHEST_amphora_golden_MAIN"),
        )

    def test_normal_amphora_rolls_the_retail_drop_kinds(self):
        entries = load_prop_loot_catalog().entries("CHEST_amphora_normal_MAIN")
        rolled = roll_prop_loot(
            entries, player_level=70, rng=random.Random(7),
            event_collectibles={"dragan_event_03": (DRAGAN_KEY,)},
        )
        kinds = [item for item, _amount in rolled]
        self.assertEqual(kinds.count("money_vc"), 3)
        self.assertIn(("e_chr_2012_event_currency", 10), rolled)
        self.assertIn(DRAGAN_KEY, kinds)
        for item, amount in rolled:
            if item == "money_vc":
                self.assertTrue(196 <= amount <= 280, amount)

    def test_event_only_entries_need_their_event_on_the_map(self):
        entries = load_prop_loot_catalog().entries("CHEST_amphora_golden_MAIN")
        christmas = roll_prop_loot(
            entries, player_level=70, rng=random.Random(1),
            event_collectibles={"christmas_2012": ("e_chr_progress_item",)},
        )
        kinds = {item for item, _amount in christmas}
        self.assertIn("e_chr_progress_item", kinds)
        self.assertNotIn("e_dra_03_hardmode_bosskey", kinds)
        no_event = roll_prop_loot(
            entries, player_level=70, rng=random.Random(1), event_collectibles={},
        )
        self.assertEqual(
            {item for item, _amount in no_event},
            {"e_chr_2012_event_currency", "money_vc"},
        )

    def test_drops_follow_the_retail_grid_around_the_player(self):
        positions = chest_drop_positions((-58.41, 1.0, -22.89), 7)
        retail = [
            (-58.41, -22.89), (-57.75, -23.55), (-59.07, -22.23),
            (-57.75, -22.23), (-59.07, -23.55), (-59.73, -22.89),
            (-58.41, -24.21),
        ]
        for (x, y, z), (rx, rz) in zip(positions, retail):
            self.assertAlmostEqual(x, rx, places=2)
            self.assertAlmostEqual(z, rz, places=2)
            self.assertEqual(y, 1.0)
        self.assertEqual(len(set(chest_drop_positions((0, 0, 0), 40))), 40)


class AmphoraEventScopeTests(unittest.TestCase):
    def _catalog(self, template):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "events.json"
        path.write_text(json.dumps({"events": [{
            "event_id": 100, "template_id": template, "state": "active",
        }]}), encoding="utf-8")
        return load_event_catalog(DEFAULT_STATIC_DB_PATH, path)

    def test_amphora_progress_is_the_page_item_of_the_maps_event(self):
        dragan = self._catalog("dragan_event_03")
        self.assertEqual(
            dragan.map_event_collectibles(
                map_id=DRAGAN_MAP, player_level=50, participation={},
            ),
            {"dragan_event_03": (DRAGAN_KEY,)},
        )
        self.assertEqual(
            dragan.map_event_collectibles(
                map_id=DRAGAN_MAP, player_level=50, participation={},
                progress={"100": {"amount": 900}},
            ),
            {"dragan_event_03": ("e_dra_progress_item",)},
        )
        self.assertEqual(
            dragan.map_event_collectibles(
                map_id="a0200_kingscity", player_level=50, participation={},
            ),
            {},
        )
        christmas = self._catalog("christmas_2012")
        self.assertEqual(
            christmas.map_event_collectibles(
                map_id="e_chr_challenge_dun_01", player_level=50, participation={},
            ),
            {"christmas_2012": ("e_chr_progress_item",)},
        )


class AmphoraPickupTextTests(unittest.TestCase):
    def test_auto_picked_bag_items_carry_retails_loot_text(self):
        self.assertEqual(inventory_loot_text_type("Key"), 6)
        self.assertEqual(inventory_loot_text_type("NoAutoUseCurrency"), 7)
        self.assertIsNone(inventory_loot_text_type("Gem"))
        definition = load_purchase_item_definition(DEFAULT_STATIC_DB_PATH, DRAGAN_KEY)
        item = _new_item(definition, 66392, 3)
        drop = WorldLootDrop(item, "Key", "All")
        mutation = add_world_drop_to_inventory(
            InventoryInfo(), drop, max_stack_size=definition.max_stack_size,
            next_item_id=66400,
        )
        packet, bits = encode_world_pickup_response(
            drop, actor_id=1, inventory=mutation, loot_text_type=6,
        )
        response = decode_world_pickup_response(packet, bits)
        self.assertEqual(
            (response.loot_received.currency_type, response.loot_received.amount,
             response.loot_received.item_id, response.loot_received.template_id),
            (6, 3, 66392, DRAGAN_KEY),
        )
        silent, silent_bits = encode_world_pickup_response(
            drop, actor_id=1, inventory=mutation,
        )
        self.assertIsNone(decode_world_pickup_response(silent, silent_bits).loot_received)


class AmphoraInteractionTests(unittest.TestCase):
    def setUp(self):
        self.server = _quiet_server(self)
        self.server.sessions = {}
        self.server.gameplay_world_lock = threading.RLock()
        patcher = patch.object(
            CharacterServer, "_accept_gameplay_state_command", lambda *a, **k: 0,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        saver = patch.object(CharacterServer, "_request_account_save", lambda *a, **k: None)
        saver.start()
        self.addCleanup(saver.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        schedule = Path(directory.name) / "events.json"
        schedule.write_text(json.dumps({"events": [{
            "event_id": 35904, "template_id": "dragan_event_03", "state": "active",
        }]}), encoding="utf-8")
        self.events = load_event_catalog(DEFAULT_STATIC_DB_PATH, schedule)
        self.encounter = load_local_player_only_encounter(DEFAULT_STATIC_DB_PATH, DRAGAN_MAP)
        self.frame = load_map_coordinate_frame(DEFAULT_STATIC_DB_PATH, DRAGAN_MAP)
        self.session = self._session(keys=3)
        self.server.sessions[self.session.addr] = self.session
        self.runtime = self.server._attach_map_event_runtime(
            self.session, map_id=DRAGAN_MAP, initial_flags=frozenset(),
            monster_level=None, scaling_difficulty=0, fresh_instance=True,
        )
        self.amphora = next(
            state.definition for state in sorted(
                self.runtime.props.values(), key=lambda state: state.definition.prop_id,
            )
            if state.definition.template_id == "chest_amphora_normal"
        )
        x, _y, z = self.amphora.position
        self.player = (x, 1.0, z + 2.7)
        self.session.gameplay_last_move = Move.from_values(
            self.frame.world_to_wire(self.player), 0, 1.0, 1.0, 100, 0,
        )

    def _session(self, *, keys):
        session = Session(("127.0.0.1", 50011), _FakeSocket())
        session.connected = True
        session.gameplay_mode = True
        session.gameplay_map = DRAGAN_MAP
        session.gameplay_map_access_group_id = DRAGAN_MAP
        session.gameplay_initialization_sent = True
        session.gameplay_presence_ready = True
        session.gameplay_character = SimpleNamespace(
            character_id=1, name="P1", level=70, class_id=1,
        )
        session.gameplay_remote_actor_id = 0x10001
        session.gameplay_coordinate_frame = self.frame
        session.gameplay_monster_encounter = self.encounter
        session.gameplay_event_catalog = self.events
        session.gameplay_next_item_id = 70000
        key = _new_item(
            load_purchase_item_definition(DEFAULT_STATIC_DB_PATH, CHEST_KEY), 66080, keys,
        )
        session.gameplay_inventory = InventoryInfo(
            items=[key] if keys else [], field_180=[66080] if keys else [],
            field_1a0=[CHEST_KEY] if keys else [],
        )
        recorder = _Recorder()
        session.send_reliable_ordered = recorder
        session.send_reliable_ordered_split = (
            lambda payload, **kwargs: recorder(payload, label=kwargs.get("label_prefix", "split"))
        )
        session.recorder = recorder
        return session

    def _click(self):
        payload, bits = encode_prop_interaction(PropInteraction(self.amphora.prop_id, ""))
        self.session.current_message_bit_length = bits
        self.server._handle_prop_interaction(self.session, payload)

    def _prop_update(self):
        update = self.session.recorder.find(PROP_UPDATE_COMMAND)
        self.assertIsNotNone(update)
        (decoded, _actor), = decode_prop_update_batch(*update)
        return decoded

    def test_one_amphora_is_published_per_stacked_spot(self):
        published = [
            state.definition for state in self.runtime.props.values()
            if state.definition.template_id.startswith("chest_amphora_")
        ]
        spots = {(round(d.position[0], 1), round(d.position[2], 1)) for d in published}
        self.assertEqual(len(published), len(spots))
        self.assertEqual(len(spots), 17)

    def test_click_spends_a_key_and_drops_loot_around_the_player(self):
        self._click()
        inventory_update = next(
            (data, bits) for data, bits, label in self.session.recorder.messages
            if label == "prop-key-inventory"
        )
        delta = decode_server_command_batch(*inventory_update)[0]
        self.assertEqual(delta.command_id, INVENTORY_INFO_COMMAND)
        self.assertEqual(
            [(item.item_id, item.template_id, item.amount) for item in delta.value.items],
            [(66080, CHEST_KEY, 2)],
        )
        self.assertEqual(self._prop_update().state, PROP_STATE_USED)
        loot = sorted(self.session.gameplay_world_loot.values(), key=lambda d: d.item.item_id)
        kinds = [drop.item.template_id for drop in loot]
        self.assertEqual(kinds.count("money_vc"), 3)
        self.assertIn("e_chr_2012_event_currency", kinds)
        self.assertIn(DRAGAN_KEY, kinds)
        first = loot[0].item.field_100
        self.assertAlmostEqual(first[0], self.player[0], places=3)
        self.assertAlmostEqual(first[2], self.player[2], places=3)
        auto = {drop.item.template_id: drop.auto_pick_up for drop in loot}
        self.assertEqual(auto["e_chr_2012_event_currency"], "All")
        self.assertEqual(auto[DRAGAN_KEY], "All")
        if "e_dra_03_hardmode_bosskey" in auto:
            self.assertEqual(auto["e_dra_03_hardmode_bosskey"], "Privileged")
        # A second click on the used amphora spends nothing more.
        self.session.recorder.messages.clear()
        count = len(self.session.gameplay_world_loot)
        self._click()
        self.assertEqual(self.session.gameplay_inventory.items[0].amount, 2)
        self.assertEqual(len(self.session.gameplay_world_loot), count)

    def test_without_keys_the_amphora_closes_again(self):
        self.session.gameplay_inventory = InventoryInfo()
        self._click()
        update = self._prop_update()
        self.assertEqual(update.state, PROP_STATE_ACTIVE)
        self.assertEqual(self.session.gameplay_world_loot, {})
        self.assertEqual(self.runtime.individual_prop_usage, set())


if __name__ == "__main__":
    unittest.main()
