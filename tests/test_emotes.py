"""Emote inventory unlocks and timed animation playback through real packets."""

import unittest
from unittest.mock import patch

from dso_server.paths import (
    DEFAULT_STATIC_DB_PATH,
)
from dso_server.protocol.commerce_protocol import (
    add_purchased_item,
    load_purchase_item_definition,
)
from dso_server.protocol.gameplay_protocol import (
    AddStickerbookFromInventoryRequest,
    StatusEffectServerCommand,
    decode_monster_spawn_batch,
    encode_add_stickerbook_from_inventory,
)
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.player_init_protocol import (
    StickerbookEntry,
    decode_server_command_batch,
)
from dso_server.gameplay.stickerbook_state import load_sticker_definition, load_sticker_upgrade
from dso_server.services.base import CharacterService
from dso_server.tests import test_pvp_player_effects as fixtures


def use_payload(resource):
    value = resource.encode()
    return b"\x8b\x35\x01" + len(value).to_bytes(2, "little") + value


class EmoteTests(unittest.TestCase):
    move = fixtures.PVPPlayerEffectTests.move

    def setUp(self):
        fixtures.PVPPlayerEffectTests.setUp(self)
        self.source.gameplay_inventory = InventoryInfo()
        self.source.send_reliable_ordered_split = lambda data, **kw: (
            self.source.recorder((data, kw["total_bit_length"]),
                                 label=kw["label_prefix"]))
        for name in ("_persist_gameplay_session", "_refresh_player_combat_vitals"):
            patcher = patch.object(CharacterService, name)
            value = patcher.start()
            self.addCleanup(patcher.stop)
            if name == "_persist_gameplay_session":
                self.persist = value

    def buy(self, resource):
        mutation = add_purchased_item(
            self.source.gameplay_inventory,
            load_purchase_item_definition(DEFAULT_STATIC_DB_PATH, resource),
            1, next_item_id=self.source.gameplay_next_item_id)
        self.source.gameplay_next_item_id = mutation.next_item_id
        return mutation.changed_items[0]

    def unlock(self, item):
        request, _ = encode_add_stickerbook_from_inventory(
            AddStickerbookFromInventoryRequest(item.item_id))
        self.server._handle_add_stickerbook_from_inventory(self.source, request)

    def play(self, resource="emote_handkiss_01"):
        self.server._handle_use_stickerbook_item(self.source, use_payload(resource))

    def test_unlock_new_alias_consumes_item_and_saves_collection(self):
        item = self.buy("emote_handkiss_01")
        self.unlock(item)
        self.assertNotIn(item.item_id, [i.item_id for i in self.source.gameplay_inventory.items])
        self.assertIn("emote_handkiss_01", self.source.gameplay_stickerbook_entries)
        # The unlock batch is followed by the stickerbook achievement record
        # (first owned emote completes stickerbook_first_emotes_01).
        labels = [label for _data, _bits, label in self.source.recorder.messages]
        index = next(i for i, label in enumerate(labels)
                     if label.startswith("stickerbook-unlock"))
        data, bits, _ = self.source.recorder.messages[index]
        commands = decode_server_command_batch(data, bits)
        self.assertEqual(commands[-1].value.entry.resource_id, "emote_handkiss_01")
        achievements = [
            command.value.achievements[0].resource_id
            for data, bits, _label in self.source.recorder.messages[index + 1:]
            for command in decode_server_command_batch(data, bits)
            if hasattr(command.value, "achievements")
        ]
        self.assertIn("stickerbook_first_emotes_01", achievements)
        self.assertTrue(self.persist.call_args.kwargs["flush"])
        self.assertEqual(load_sticker_definition(
            DEFAULT_STATIC_DB_PATH, "emote_handkiss_01").kind, "emote")

    def test_owned_old_alias_cannot_consume_another_unlock_item(self):
        self.source.gameplay_stickerbook_entries["vanity_handkiss_01"] = (
            StickerbookEntry(2, 0, "vanity_handkiss_01"))
        item = self.buy("emote_handkiss_01")
        with self.assertRaisesRegex(ValueError, "is owned"):
            self.unlock(item)
        self.assertIn(item.item_id, [i.item_id for i in self.source.gameplay_inventory.items])
        self.persist.assert_not_called()

    def test_play_emits_authored_effect_for_owner_and_peer(self):
        self.unlock(self.buy("emote_handkiss_01"))
        self.source.recorder.messages.clear()
        self.play()
        entry, expires = next(iter(self.source.gameplay_runtime_status_effects.values()))
        self.assertEqual(entry.effect_id, 762)
        self.assertEqual(entry.field_10, 140)
        self.assertEqual(entry.field_0c - entry.field_08, 140)
        self.assertEqual(entry.values, (-1.0, 0.0, 0.0, 0.0, 0.0))
        self.assertTrue(entry.has_details)
        self.assertNotIn(entry.field_04, self.source.gameplay_persistent_status_effects)
        self.assertFalse(self.source.gameplay_inventory.items)
        for session, actor_id in ((self.source, 1),
                                  (self.target, self.source.gameplay_remote_actor_id)):
            effects = [c for data, bits, _ in session.recorder.messages
                       for c in decode_monster_spawn_batch(data, bits).commands
                       if isinstance(c, StatusEffectServerCommand)]
            self.assertTrue(effects)
            self.assertEqual(effects[-1].actor_id, actor_id)
            self.assertEqual(effects[-1].value.entries[0].effect_id, entry.effect_id)
        expiry = self.timers[-1]
        self.assertAlmostEqual(expiry.delay, 5.7, delta=0.1)
        expiry.function(*expiry.args)
        self.assertFalse(self.source.gameplay_runtime_status_effects)

    def test_owned_legacy_alias_allows_current_client_use(self):
        self.source.gameplay_stickerbook_entries["vanity_handkiss_01"] = (
            StickerbookEntry(2, 0, "vanity_handkiss_01"))
        self.play()
        self.assertTrue(self.source.gameplay_runtime_status_effects)

    def test_repeat_restarts_full_animation_and_stale_timer_is_harmless(self):
        self.unlock(self.buy("emote_handkiss_01"))
        self.play()
        old_timer = self.timers[-1]
        old_instance = next(iter(self.source.gameplay_runtime_status_effects))
        self.play()
        self.assertEqual(len(self.source.gameplay_runtime_status_effects), 1)
        entry, _ = next(iter(self.source.gameplay_runtime_status_effects.values()))
        self.assertNotEqual(entry.field_04, old_instance)
        self.assertEqual(entry.field_10, 140)
        self.assertEqual(entry.field_18, 0)
        old_timer.cancel.assert_called_once()
        old_timer.function(*old_timer.args)
        self.assertIn(entry.field_04, self.source.gameplay_runtime_status_effects)

    def test_unowned_and_consumable_only_emotes_cannot_unlock_or_play(self):
        self.assertIsNone(load_sticker_definition(
            DEFAULT_STATIC_DB_PATH, "emote_handkiss_01_consumable"))
        with self.assertRaisesRegex(ValueError, "not owned"):
            self.play()
        self.assertFalse(self.source.gameplay_runtime_status_effects)

    def test_pet_upgrade_remains_a_separate_tier(self):
        upgrade = load_sticker_upgrade(DEFAULT_STATIC_DB_PATH, "pet_jindo_uncommon")
        self.assertIsNotNone(upgrade)
        base = load_sticker_definition(DEFAULT_STATIC_DB_PATH, upgrade.source_resource_id)
        upgraded = load_sticker_definition(DEFAULT_STATIC_DB_PATH, upgrade.target_resource_id)
        self.assertTrue(set(base.aliases).isdisjoint(upgraded.aliases))
