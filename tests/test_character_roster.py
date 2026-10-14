"""The player database owns the roster, including fresh and existing servers."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dso_server.persistence.account_state import AccountStateStore
from dso_server.persistence.character_roster import load_character_roster
from dso_server.protocol.character_generation import (
    CharacterGeneration, encode_character_generation,
)
from dso_server.protocol.character_selection import (
    LOCAL_CHARACTER_ID_BASE, decode_character_selection, encode_character_selection,
)
from dso_server.runtime.transport import Session
from dso_server.tests.fixtures.characters import character_profile, character_server
from dso_server.tests.fixtures.captured_character_selection import (
    CHARACTER_SELECTION_BIT_LENGTH, CHARACTER_SELECTION_PAYLOAD,
)


class CharacterRosterTests(unittest.TestCase):
    def test_empty_database_has_no_seed_characters_or_equipment(self):
        server = character_server(self, profiles=())
        self.assertEqual(server.account_store.profiles(), ())
        self.assertEqual(server.character_selection.characters, [])
        self.assertEqual(server.character_selection.equipment, [])
        self.assertEqual(server.character_equipment, {})
        self.assertEqual(server.character_selection.active_character_id, 0)
        self.assertEqual(server._next_character_id(), LOCAL_CHARACTER_ID_BASE)
        decoded = decode_character_selection(
            server.character_selection_payload, server.character_selection_bit_length,
        )
        self.assertEqual(decoded.characters, [])

    def test_existing_database_ids_and_profiles_are_not_reseeded_or_renumbered(self):
        profiles = tuple(
            character_profile(42 + index, f"Persisted{index}", premium_currency=123 + index)
            for index in range(7)
        )
        server = character_server(self, profiles)
        self.assertEqual(server.account_store.profiles(), profiles)
        self.assertEqual(
            [character.character_id for character in server.character_selection.characters],
            [profile.character_id for profile in profiles],
        )
        self.assertEqual(
            server.character_selection.equipment,
            server._selection_equipment(42),
        )
        self.assertEqual(server.character_selection.active_character_id, 42)
        self.assertEqual(len(server.character_equipment), 7)

    def test_current_state_overlays_roster_without_rewriting_profiles(self):
        profile = character_profile(premium_currency=10)
        store = Mock(spec=AccountStateStore)
        store.profiles.return_value = (profile,)
        store.get.return_value = SimpleNamespace(
            map_id="b1201_pve", currency=SimpleNamespace(currency_type=25),
        )
        selection = load_character_roster(store)
        self.assertEqual(selection.active_character.map_id, "b1201_pve")
        self.assertEqual(selection.active_character.premium_currency, 25)
        self.assertEqual(profile.premium_currency, 10)
        store.put_profile.assert_not_called()

    def test_saved_empty_equipment_does_not_restore_the_starting_loadout(self):
        server = character_server(self)
        character_id = server.character_selection.active_character_id
        self.assertTrue(server.character_equipment[character_id])
        saved = SimpleNamespace(inventory=SimpleNamespace(items=[], equipment_slots={}))
        with patch.object(server.account_store, "get", return_value=saved):
            self.assertEqual(server._selection_equipment(character_id), [])

    def test_character_created_on_empty_server_survives_reload(self):
        server = character_server(self, profiles=())
        session = Session(("127.0.0.1", 41234), Mock())
        session.send_reliable_ordered = Mock()
        request = CharacterGeneration(operation=3, class_id=1, name="NewHero")
        payload, bit_length = encode_character_generation(request, server_to_client=False)
        session.current_message_bit_length = bit_length
        with patch.object(server, "arm_gameplay"), patch.object(
            server, "_send_character_generation_packet"
        ) as send:
            server._handle_character_generation(session, payload)
        self.assertEqual(send.call_args.args[1].operation, 5)
        server.account_store.flush()
        reopened = AccountStateStore(server.account_store.path)
        self.addCleanup(reopened.close)
        selection = load_character_roster(reopened)
        self.assertEqual([character.name for character in selection.characters], ["NewHero"])
        self.assertEqual(selection.active_character_id, LOCAL_CHARACTER_ID_BASE)
        self.assertEqual(selection.active_character.level, 1)
        # New characters start in the retail tutorial dungeon.
        self.assertEqual(selection.active_character.map_id, "a0001_start_tutorial_dun")

    def _delete_request(self, character_id):
        # Captured client bytes for the delete button: operation 6, a zero
        # byte, the character id, zero padding to 32 bytes.
        body = bytes((0x06, 0x00)) + int(character_id).to_bytes(4, "little")
        return b"\x8b\x87\x00" + body.ljust(29, b"\x00")

    def test_back_from_the_creation_editor_resends_the_selection_tail(self):
        server = character_server(self)
        session = Session(("127.0.0.1", 41236), Mock())
        sent = []  # (bytes, bit length)
        record = lambda *payloads, **kwargs: sent.extend(
            p if isinstance(p, tuple) else (p, len(p) * 8) for p in payloads
        )
        session.send_reliable_ordered = record
        session.send_multi_reliable_ordered = record
        session.send_reliable_ordered_split = lambda fragments, **kw: sent.append(
            (b"".join(fragments), kw.get("total_bit_length"))
        )
        # The login flow already delivered the tail once, then the player
        # opened the creation editor.
        session.post_selection_client_ready = True
        session.post_selection_tail_sent = True
        session.character_generation_active = True
        # The editor's back button: the client asks for the selection screen
        # again with operation 2 and expects the list and offers back.
        request = b"\x8b\x87\x00\x02".ljust(32, b"\x00")
        server._on_char_op(session, request)
        ids = [int.from_bytes(p[1:3], "little") for p, _ in sent if len(p) >= 3 and p[0] == 0x84]
        self.assertIn(0x0087, ids)
        self.assertIn(0x010E, ids)
        self.assertIn(0x010C, ids)
        listed = next(
            decode_character_selection(p, bits) for p, bits in sent
            if p[:3] == b"\x84\x87\x00"
        )
        self.assertEqual([c.name for c in listed.characters], ["TestPlayer"])
        self.assertFalse(session.character_generation_active)

    def test_delete_request_removes_the_character_everywhere(self):
        profiles = (character_profile(42, "Keep"), character_profile(43, "d12tradg4"))
        server = character_server(self, profiles)
        session = Session(("127.0.0.1", 41235), Mock())
        session.send_reliable_ordered = Mock()
        session.send_multi_reliable_ordered = Mock()
        session.send_reliable_ordered_split = Mock()
        server._on_char_op(session, self._delete_request(43))
        self.assertEqual(
            [c.character_id for c in server.character_selection.characters], [42],
        )
        self.assertNotIn(43, server.character_equipment)
        # PlayerDeletedCommand (0x0010, actorless) names the deleted id.
        (deleted, bits), = (
            call.args[0] for call in session.send_reliable_ordered.call_args_list
            if call.kwargs.get("label") == "player-deleted"
        )
        self.assertEqual(deleted, bytes.fromhex("841000") + (43).to_bytes(4, "little"))
        self.assertEqual(bits, len(deleted) * 8)
        # The remaining list follows.
        (packet, bit_length), = session.send_multi_reliable_ordered.call_args.args[:1]
        listed = decode_character_selection(packet, bit_length)
        self.assertEqual([c.name for c in listed.characters], ["Keep"])
        self.assertEqual(listed.active_character_id, 42)
        # And the database no longer knows the character after a reload.
        server.account_store.flush()
        reopened = AccountStateStore(server.account_store.path)
        self.addCleanup(reopened.close)
        self.assertEqual([p.character_id for p in reopened.profiles()], [42])
        # Deleting the last character opens the creation editor instead of
        # sending an empty list.
        with patch.object(server, "_open_character_generation_editor") as editor:
            server._on_char_op(session, self._delete_request(42))
        self.assertEqual(server.character_selection.characters, [])
        self.assertEqual(server.character_selection.active_character_id, 0)
        editor.assert_called_once()
        # An unknown id is refused without side effects.
        server._on_char_op(session, self._delete_request(99))
        self.assertEqual(server.character_selection.characters, [])

    def test_character_selection_wire_layout_still_matches_capture(self):
        model = decode_character_selection(
            CHARACTER_SELECTION_PAYLOAD, CHARACTER_SELECTION_BIT_LENGTH,
        )
        packet, bits = encode_character_selection(model)
        self.assertEqual((packet, bits), (
            CHARACTER_SELECTION_PAYLOAD, CHARACTER_SELECTION_BIT_LENGTH,
        ))
