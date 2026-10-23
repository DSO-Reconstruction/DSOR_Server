"""Map access follows visits/quest turn-ins; admin grants are per character."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dso_server.gameplay.local_mount_state import BLACK_BEAR_MOUNT
from dso_server.gameplay.map_unlocks import saved_quest_map_unlocks, unlocked_travel_map_ids
from dso_server.gameplay.quest_content import QuestProgress, QUEST_STATE_FINISHED, QUEST_STATE_CLOSED
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence.account_state import _state_from_document, _state_to_document
from dso_server.protocol.gameplay_protocol import MapTravelRequest, NEW_NPC_COMMAND
from dso_server.protocol.player_init_protocol import (
    decode_player_init_prefix, STICKERBOOK_INFO_COMMAND, QUICK_SLOTS_INFO_COMMAND, StickerbookEntry,
    PLAYER_READY_COMMAND, decode_server_command_batch,
)
from dso_server.runtime.transport import Session
from dso_server.services.character_server import CharacterServer
from dso_server.tests.fixtures.characters import character_profile, character_server
from dso_server.tests import test_admin_speed as chat_fixtures, test_quests as quest_fixtures


class MapUnlockTests(unittest.TestCase):
    def setUp(self):
        self.server = character_server(self, [character_profile(
            map_id="a0001_start_tutorial_dun", level=1)])
        self.server.sock = Mock()

    def enter(self, map_id):
        s = Session(("127.0.0.1", 51002), self.server.sock)
        s.connected = s.gameplay_mode = True
        s.gameplay_map = map_id
        s.gameplay_character = self.server.character_selection.active_character
        send_order = []
        s.send_reliable_ordered_split = Mock(
            side_effect=lambda *args, **kwargs: send_order.append(
                kwargs.get("label_prefix")
            )
        )
        s.send_reliable_ordered = Mock(
            side_effect=lambda *args, **kwargs: send_order.append(
                kwargs.get("label")
            )
        )
        s._test_send_order = send_order
        self.server._send_local_player_initialization(s)
        call = next(c for c in s.send_reliable_ordered_split.call_args_list
                    if c.kwargs.get("label_prefix") == "local-player-initialization-split")
        batch, _ = decode_player_init_prefix(call.args[0], call.kwargs["total_bit_length"])
        return s, batch

    def test_kingshill_registers_npc_actors_before_player_init(self):
        session, batch = self.enter("a0200_kingscity")
        vicinity = next(
            call for call in session.send_reliable_ordered.call_args_list
            if call.kwargs.get("label")
            == "local-initialization-npc-actor-vicinity"
        )
        send_order = session._test_send_order
        self.assertLess(
            send_order.index("local-initialization-npc-actor-vicinity"),
            send_order.index("local-player-initialization-split"),
        )
        decoded = decode_server_command_batch(
            vicinity.args[0], len(vicinity.args[0]) * 8,
        )
        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].command_id, 0x0074)
        registered = set(decoded[0].value.actor_ids)
        templates = {
            "e_event_merchant_grima": 0x10076,
            "e_event_merchant_01": 0x1002B,
            "e_travel_change_quest_npc": 0x10071,
            "a0200_gambler_trader": 0x10073,
            "a0200_premium_trader": 0x10007,
            "e_anniversary_merchant_phestos": 0x1004F,
        }
        new_npcs = {
            command.value.template_id: command.value.actor_id
            for command in batch
            if command.command_id == NEW_NPC_COMMAND
        }
        for template, actor_id in templates.items():
            with self.subTest(template=template):
                self.assertEqual(new_npcs[template], actor_id)
                self.assertIn(actor_id, registered)
        self.assertEqual(
            len(registered),
            sum(command.command_id == NEW_NPC_COMMAND for command in batch),
        )

    def test_new_character_has_no_mount_shortcut_collection_or_travel_unlocks(self):
        for _ in range(2):  # Include relog with a deliberately empty saved collection.
            s, batch = self.enter("a0001_start_tutorial_dun")
            self.assertEqual(batch[0].value.field_130, (0, 0, 0, 0))
            self.assertFalse(s.gameplay_admin_all_maps)
            self.assertFalse(s.gameplay_stickerbook_entries)
            self.assertIsNone(s.gameplay_mount_ride)
            book = next(c.value for c in batch if c.command_id == STICKERBOOK_INFO_COMMAND)
            self.assertFalse(book.entries)
            slots = next(c.value for c in batch if c.command_id == QUICK_SLOTS_INFO_COMMAND)
            self.assertFalse(any(slot.kind == 2 for bar in slots.quickbars for slot in bar))
            saved = self.server.account_store.get(s.gameplay_character.character_id)
            self.assertEqual(saved.stickerbook_entries, ())

    def test_visits_unlock_only_discovered_destinations_and_survive_relogin(self):
        known = set()
        for map_id in ("a0200_kingscity", "a0006_grimford_hub", "a0200_kingscity"):
            s, batch = self.enter(map_id)
            known.add(map_id)
            self.assertEqual(batch[0].value.field_130,
                             self.server.static_database.travel_map_unlock_words(tuple(known)))
            self.assertEqual(s.gameplay_unlocked_maps, known)
            self.assertFalse(self.server._travel_map_unlocked(s, "b1200_hub"))

    def test_admin_grant_persists_and_does_not_fabricate_visits_or_quests(self):
        s, _ = self.enter("a0001_start_tutorial_dun")
        discoveries, quests = set(s.gameplay_unlocked_maps), dict(s.gameplay_quest_log)
        self.server._unlock_all_maps(s)
        self.server._unlock_all_maps(s)
        saved = self.server.account_store.get(s.gameplay_character.character_id)
        self.assertTrue(saved.admin_all_maps)
        self.assertTrue(_state_from_document(_state_to_document(saved)).admin_all_maps)
        restored, batch = self.enter(s.gameplay_map)
        all_maps = self.server.static_database.fast_travel_map_ids()
        self.assertEqual(batch[0].value.field_130,
                         self.server.static_database.travel_map_unlock_words(all_maps))
        self.assertEqual(restored.gameplay_unlocked_maps, discoveries)
        self.assertEqual(restored.gameplay_quest_log, quests)
        self.assertTrue(self.server._map_quest_accessible(restored, "pw001_01_grimmagstone_01_dun"))
        self.assertFalse(restored.gameplay_stickerbook_entries)
        legacy = _state_to_document(saved)
        del legacy["admin_all_maps"]
        self.assertFalse(_state_from_document(legacy).admin_all_maps)

    def test_existing_mount_is_preserved(self):
        s, _ = self.enter("a0001_start_tutorial_dun")
        saved = self.server.account_store.get(s.gameplay_character.character_id)
        bear = StickerbookEntry(3, 0, BLACK_BEAR_MOUNT.stickerbook_resource_id)
        self.server.account_store.put(replace(saved, stickerbook_entries=(bear,)), flush=True)
        restored, _ = self.enter(s.gameplay_map)
        self.assertEqual(restored.gameplay_stickerbook_entries[bear.resource_id], bear)

    def test_owned_pets_and_mounts_are_refreshed_after_player_ready_once(self):
        session, _ = self.enter("a0001_start_tutorial_dun")
        saved = self.server.account_store.get(session.gameplay_character.character_id)
        entries = (
            StickerbookEntry(3, 0, BLACK_BEAR_MOUNT.stickerbook_resource_id),
            StickerbookEntry(2, 0, "pet_rabbit"),
        )
        self.server.account_store.put(replace(saved, stickerbook_entries=entries), flush=True)
        restored, initial = self.enter(session.gameplay_map)
        self.assertEqual(initial[-1].command_id, PLAYER_READY_COMMAND)
        sends = restored.send_reliable_ordered_split.call_args_list
        initial_index = next(i for i, c in enumerate(sends)
                             if c.kwargs.get("label_prefix") == "local-player-initialization-split")
        refreshes = [(i, c) for i, c in enumerate(sends)
                     if c.kwargs.get("label_prefix") == "restore-stickerbook-restore-stickerbook"]
        self.assertEqual(len(refreshes), 1)
        refresh_index, call = refreshes[0]
        self.assertGreater(refresh_index, initial_index)
        packet = decode_server_command_batch(call.args[0], call.kwargs["total_bit_length"])
        self.assertEqual(len(packet), 1)
        self.assertEqual(packet[0].command_id, STICKERBOOK_INFO_COMMAND)
        self.assertEqual(packet[0].actor_id, 1)
        self.assertEqual(packet[0].value.field_20, 2)
        self.assertCountEqual(packet[0].value.entries, entries)
        self.assertCountEqual(self.server.account_store.get(saved.character_id).stickerbook_entries, entries)
        count = len(sends)
        self.server._send_local_player_initialization(restored)
        self.assertEqual(restored.send_reliable_ordered_split.call_count, count)

    def test_fast_travel_refuses_unvisited_maps_and_allows_admin_grant(self):
        s, _ = self.enter("a0001_start_tutorial_dun")
        request = MapTravelRequest(0, "a0200_kingscity", 0, False)
        with patch.object(CharacterServer, "_begin_map_transfer", return_value=True) as transfer:
            self.server._perform_map_travel(s, request, command_id=0x61)
            transfer.assert_not_called()
            self.server._unlock_all_maps(s)
            self.server._perform_map_travel(s, request, command_id=0x61)
            transfer.assert_called_once()

    def test_admin_unlock_does_not_override_event_availability(self):
        s, _ = self.enter("a0001_start_tutorial_dun")
        self.server._unlock_all_maps(s)
        request = MapTravelRequest(0, "ce202501", 0, False)
        with patch.object(CharacterServer, "_event_catalog", return_value=Mock(map_available=lambda _: False)), \
                patch.object(CharacterServer, "_begin_map_transfer", return_value=True) as transfer:
            self.server._perform_map_travel(s, request, command_id=0x61)
            transfer.assert_not_called()

    def test_quest_lock_is_checked_before_consuming_a_travel_item(self):
        s, _ = self.enter("a0001_start_tutorial_dun")
        request = MapTravelRequest(0, "pw001_01_grimmagstone_01_dun", 0, False)
        s.gameplay_pending_travel_item = {"target_map_id": request.map_id}
        inventory = s.gameplay_inventory
        with patch.object(CharacterServer, "_begin_map_transfer", return_value=True) as transfer:
            self.server._perform_map_travel(s, request, command_id=0x79, required_item_id=1)
            transfer.assert_not_called()
        self.assertIs(s.gameplay_inventory, inventory)
        self.assertIsNotNone(s.gameplay_pending_travel_item)

    def test_access_variant_and_signal_suffix_unlock_the_base_travel_point(self):
        for discovered in ("a0203_kingscrypt02_03", "a0203_kingscrypt02_diff_2"):
            self.assertEqual(unlocked_travel_map_ids(DEFAULT_STATIC_DB_PATH, (discovered,)),
                             ("a0203_kingscrypt02",))


class QuestMapUnlockTests(unittest.TestCase):
    def test_authored_unlock_requires_turn_in_and_is_restored_from_older_saves(self):
        fixture = quest_fixtures.QuestServiceTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        server, s = fixture.server, fixture.session
        quest_id, access = "pw000-unlock-pw001-n", "pw001_01_grimmagstone_01_dun"
        progress = QuestProgress(quest_id, state=QUEST_STATE_FINISHED)
        s.gameplay_quest_log[quest_id] = progress
        self.assertFalse(server._map_quest_accessible(s, access))
        self.assertFalse(saved_quest_map_unlocks(DEFAULT_STATIC_DB_PATH,
                         SimpleNamespace(quest_log={quest_id: progress.to_document()})))
        self.assertTrue(server._quest_turn_in(s, quest_id))
        self.assertEqual(progress.state, QUEST_STATE_CLOSED)
        self.assertTrue(server._map_quest_accessible(s, access))
        self.assertIn("pw001_grimmagstone_01_dun", s.gameplay_unlocked_maps)
        grants = saved_quest_map_unlocks(DEFAULT_STATIC_DB_PATH,
                     SimpleNamespace(quest_log={quest_id: progress.to_document()}))
        self.assertIn(access, grants)
        self.assertFalse(server._map_quest_accessible(s, "pw004_01_hagastove_grotto_01_dun"))


class UnlockMapsChatTests(unittest.TestCase):
    def setUp(self):
        self.fixture = chat_fixtures.AdminSpeedTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_self_named_target_and_help(self):
        f = self.fixture
        self.assertIn("!unlockmaps [character name]", f.command("!help"))
        self.assertIn("All maps unlocked for Speed Runner", f.command("!UNLOCKMAPS speed runner"))
        self.assertTrue(f.away.gameplay_admin_all_maps)
        self.assertFalse(f.home.gameplay_admin_all_maps)
        self.assertIn("Change maps or relog", f.command("!unlockmaps"))
        self.assertTrue(f.home.gameplay_admin_all_maps)

    def test_non_admin_and_unavailable_targets_cannot_grant_access(self):
        f = self.fixture
        f.admin.chat_is_admin = False
        self.assertIn("administrator privileges", f.command("!unlockmaps Speed Runner"))
        self.assertFalse(f.away.gameplay_admin_all_maps)
        f.admin.chat_is_admin = True
        self.assertIn("not online", f.command("!unlockmaps Missing"))
        f.home.gameplay_presence_ready = False
        self.assertIn("not ready", f.command("!unlockmaps"))
        self.assertFalse(f.home.gameplay_admin_all_maps)
