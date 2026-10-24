"""Admin speed toggles, native effect packets, and per-character persistence."""

from dataclasses import replace
import sqlite3
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.chat_protocol import decode_chat_text, encode_chat_text
from dso_server.protocol.gameplay_protocol import decode_status_effect_batch
from dso_server.runtime.transport import Session
from dso_server.services.chat import ChatServer
from dso_server.services import customization as customization_module
from dso_server.services.admin_effects import ADMIN_EFFECTS, restore_admin_effects
from dso_server.services.customization import ADMIN_SPEED_EFFECT_RESOURCE, ADMIN_SPEED_EFFECT_VALUES
from dso_server.tests.test_pvp_matchmaking import _quiet_server, _session
from dso_server.tests import test_session_lock_hot_paths as snapshot_fixtures


class AdminSpeedTests(unittest.TestCase):
    def setUp(self):
        self.gameplay_server = _quiet_server(self)
        self.home = _session(52301, 1, "Admin")
        self.away = _session(52302, 2, "Speed Runner")
        for session in (self.home, self.away):
            session.gameplay_initialization_sent = True
            session.gameplay_presence_ready = True
            session.gameplay_presence_callback = Mock()
        self.gameplay_server.sessions = {self.home.addr: self.home, self.away.addr: self.away}
        self.chat = ChatServer.__new__(ChatServer)
        self.chat.gameplay_servers = (self.gameplay_server,)
        self.chat._audit = Mock()
        self.chat._channel_recipients = Mock(return_value=())
        self.admin = _session(52303, 1, "Admin")
        self.admin.chat_user_id = 1
        self.admin.chat_user_name = "Admin"
        self.admin.chat_is_admin = True
        self.admin.chat_joined_channels = {"map"}

    def command(self, text):
        # The claimed administrator tag in the client text grants no privileges.
        packet = encode_chat_text(f"sm#map#en#hb#{text}", from_client=True)
        self.chat._on_char_op(self.admin, packet)
        self.chat._channel_recipients.assert_not_called()
        return decode_chat_text(self.admin.recorder.messages[-1][0], from_client=False)

    def test_self_toggle_sends_add_and_matching_remove_without_stacking(self):
        self.assertIn("+100% enabled for Admin", self.command("!speed"))
        (entry,) = self.home.gameplay_persistent_status_effects.values()
        self.assertEqual(entry.values, ADMIN_SPEED_EFFECT_VALUES)
        self.assertEqual(entry.field_10, 0)
        self.assertFalse(self.home.gameplay_timed_status_effect_expiries)
        payload, bits, label = self.home.recorder.messages[-1]
        self.assertEqual(label, "admin-speed-toggle")
        added, actor_id = decode_status_effect_batch(payload, bits)
        self.assertEqual(actor_id, 1)
        self.assertEqual(added.entries, (entry,))
        self.assertIn("disabled for Admin", self.command("!speed"))
        self.assertFalse(self.home.gameplay_persistent_status_effects)
        payload, bits, _ = self.home.recorder.messages[-1]
        removed = decode_status_effect_batch(payload, bits)[0].entries[0]
        self.assertEqual(removed.field_04, entry.field_04)
        self.assertFalse(removed.has_details)
        self.assertIn("enabled", self.command("!speed"))
        self.assertEqual(len(self.home.gameplay_persistent_status_effects), 1)
        self.home.gameplay_presence_callback.assert_called()

    def test_live_effect_uses_the_characters_movement_clock(self):
        self.home.gameplay_last_move = SimpleNamespace(start_tick=98765)
        self.command("!speed")
        payload, bits, _ = self.home.recorder.messages[-1]
        entry = decode_status_effect_batch(payload, bits)[0].entries[0]
        self.assertEqual(entry.field_08, 98766)
        self.assertEqual(entry.field_14, 25)
        self.assertEqual(entry.field_0c, 0)
        self.assertEqual(entry.values[0], 5.0)

    def test_stationary_character_uses_the_current_server_clock(self):
        self.home.gameplay_last_move = None
        self.home.gameplay_chase_server_tick = 7000
        with patch.object(customization_module, "server_time", return_value=500000):
            self.command("!speed")
        (entry,) = self.home.gameplay_persistent_status_effects.values()
        self.assertEqual(entry.field_08, 12500)
        self.assertEqual(entry.field_14, 25)

    def test_restore_repairs_only_the_durable_admin_speed_instance(self):
        self.command("!speed")
        (entry,) = self.home.gameplay_persistent_status_effects.values()
        old_admin = replace(entry, field_08=90000, field_14=0)
        other_speed = replace(entry, field_04=71, field_08=70000,
                              field_14=25, values=(0.1, 0, 0, 0, 0))
        timed_speed = replace(entry, field_04=72, field_08=80000, field_10=250)
        restored = restore_admin_effects(
            {e.field_04: e for e in (old_admin, other_speed, timed_speed)},
            load_status_runtime_catalog(DEFAULT_STATIC_DB_PATH),
        )
        self.assertEqual(restored[entry.field_04].field_08, 0)
        self.assertEqual(restored[entry.field_04].field_14, 25)
        self.assertEqual(restored[entry.field_04].values, entry.values)
        self.assertIs(restored[71], other_speed)
        self.assertIs(restored[72], timed_speed)

    def test_named_character_is_case_insensitive_and_may_have_spaces(self):
        other_server = _quiet_server(self)
        other_server.sessions = {self.away.addr: self.away}
        del self.gameplay_server.sessions[self.away.addr]
        self.chat.gameplay_servers += (other_server,)
        self.assertIn("enabled for Speed Runner", self.command("!SPEED speed runner"))
        self.assertFalse(self.home.gameplay_persistent_status_effects)
        self.assertEqual(len(self.away.gameplay_persistent_status_effects), 1)
        self.assertIn("disabled for Speed Runner", self.command("!speed SPEED RUNNER"))
        self.assertFalse(self.away.gameplay_persistent_status_effects)

    def test_non_admin_cannot_change_self_or_other_character(self):
        self.admin.chat_is_admin = False
        for command in ("!speed", "!speed Speed Runner"):
            self.assertIn("administrator privileges", self.command(command))
        self.assertFalse(self.home.gameplay_persistent_status_effects)
        self.assertFalse(self.away.gameplay_persistent_status_effects)
        self.assertFalse(self.home.recorder.messages)
        self.assertFalse(self.away.recorder.messages)

    def test_missing_or_departed_target_does_not_toggle_the_admin(self):
        self.assertIn("not online", self.command("!speed Missing"))
        self.away.gameplay_presence_departed = True
        self.assertIn("not online", self.command("!speed Speed Runner"))
        self.assertFalse(self.home.gameplay_persistent_status_effects)
        self.assertFalse(self.away.gameplay_persistent_status_effects)

    def test_initializing_or_transferring_character_cannot_get_a_lost_toggle(self):
        self.home.gameplay_presence_ready = False
        self.assertIn("not ready", self.command("!speed"))
        self.home.gameplay_presence_ready = True
        self.home.gameplay_exit_transition_started = True
        self.assertIn("not ready", self.command("!speed"))
        self.assertFalse(self.home.gameplay_persistent_status_effects)

    def test_ambiguous_character_name_is_rejected(self):
        duplicate = _session(52304, 3, "Speed Runner")
        self.gameplay_server.sessions[duplicate.addr] = duplicate
        self.assertIn("more than one", self.command("!speed Speed Runner"))
        self.assertFalse(self.away.gameplay_persistent_status_effects)
        self.assertFalse(duplicate.gameplay_persistent_status_effects)

    def test_other_bonuses_and_a_different_instance_of_the_same_effect_survive(self):
        self.command("!speed")
        (entry,) = self.home.gameplay_persistent_status_effects.values()
        other = replace(entry, field_04=71, effect_id=2050, values=(0.2, 0, 0, 0, 0))
        same_template = replace(entry, field_04=72, field_10=250, values=(0.1, 0, 0, 0, 0))
        self.home.gameplay_persistent_status_effects.update({71: other, 72: same_template})
        self.command("!speed")
        self.assertEqual(self.home.gameplay_persistent_status_effects, {71: other, 72: same_template})

    def test_help_advertises_speed(self):
        self.assertIn("!speed [character name]", self.command("!help"))

    def test_client_template_adds_base_speed_after_the_normal_movement_cap(self):
        with sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH.as_posix()}?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT StartModifiers, TickModifiers, StopModifiers, DoneModifiers, "
                "ExclusiveGroup, KeepOnDeath, DisableMaps, DisableRulesets "
                "FROM _Template_StatusEffect WHERE Id=?", (ADMIN_SPEED_EFFECT_RESOURCE,),
            ).fetchone()
        self.assertEqual(row[0].rstrip(";"), "Speed:$0,absolute,MovementBonus")
        self.assertTrue(all(not value for value in row[1:5]))
        self.assertEqual(row[5], 1)
        self.assertTrue(all(not value for value in row[6:]))
        self.assertEqual(ADMIN_SPEED_EFFECT_VALUES[0], 5.0)


class AdminSpeedPersistenceTests(unittest.TestCase):
    def test_bonus_survives_persistence_and_reinitialization_and_can_be_removed(self):
        self._check_reinitialization(saved_tick=90000, saved_interval=25)

    def test_old_saved_toggle_is_repaired_on_reinitialization(self):
        self._check_reinitialization(saved_tick=0, saved_interval=0)

    def test_legacy_speed_is_migrated_on_real_player_initialization(self):
        self._check_reinitialization(saved_tick=90000, saved_interval=0, legacy=True)

    def _check_reinitialization(self, *, saved_tick, saved_interval, legacy=False):
        snapshot_fixtures.AccountSnapshotLockTests.setUp(self)
        self.session.connected = True
        self.session.gameplay_presence_ready = True
        effect_id = load_status_runtime_catalog(DEFAULT_STATIC_DB_PATH).effect_ids[ADMIN_SPEED_EFFECT_RESOURCE]
        self.assertTrue(self.server._toggle_admin_speed(self.session))
        character_id = self.session.gameplay_character.character_id
        saved = self.server.account_store.get(character_id)
        self.assertEqual(sum(e.effect_id == effect_id for e in saved.status_effects), 1)
        saved = replace(saved, status_effects=tuple(
            replace(e, field_08=saved_tick, field_14=saved_interval,
                    effect_id=(load_status_runtime_catalog(DEFAULT_STATIC_DB_PATH).effect_ids[
                        ADMIN_EFFECTS["speed"]["legacy_resource"]] if legacy else effect_id),
                    values=(ADMIN_EFFECTS["speed"]["legacy_values"] if legacy else e.values))
            if e.effect_id == effect_id else e for e in saved.status_effects
        ))
        if legacy:
            # A legacy save predates recorded status-effect names.
            saved = replace(saved, status_effect_names={})
        self.server.account_store.put(saved, flush=True)

        restored = Session(("127.0.0.1", 52305), self.sock)
        restored.connected = True
        restored.gameplay_mode = True
        restored.gameplay_map = self.session.gameplay_map
        restored.gameplay_character = self.session.gameplay_character
        restored.gameplay_template_row_index = self.session.gameplay_template_row_index
        self.server._send_local_player_initialization(restored)
        effects = [e for e in restored.gameplay_persistent_status_effects.values() if e.effect_id == effect_id]
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0].values, ADMIN_SPEED_EFFECT_VALUES)
        self.assertEqual(effects[0].field_08, 0)
        self.assertEqual(effects[0].field_14, 25)
        self.assertFalse(self.server._toggle_admin_speed(restored))
        saved = self.server.account_store.get(character_id)
        self.assertFalse(any(e.effect_id == effect_id for e in saved.status_effects))
