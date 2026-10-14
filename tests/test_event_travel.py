"""Event preview Start: native named entries, schedule gates and map handoff."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from dso_server.gameplay.event_system import load_event_catalog
from dso_server.gameplay.local_map_state import load_local_entry_position, load_local_spawn_position
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import (
    MapTravelRequest, decode_map_travel_request, encode_map_travel_request,
)
from dso_server.runtime.transport import Session
from dso_server.services.character_server import CharacterServer
from dso_server.tests.fixtures.characters import character_server


PACKET = bytes.fromhex(
    "8b610000000f0061303230305f6b696e677363697479"
    "100061303230305f6661697267726f756e6400")
HUB = "a0200_kingscity"
ENTRY = "a0200_fairground"


class EventTravelCodecTests(unittest.TestCase):
    def test_reported_packet_with_exact_and_rounded_bit_lengths(self):
        for bits in (None, 321, 328):
            with self.subTest(bits=bits):
                request = decode_map_travel_request(PACKET, bits)
                self.assertEqual(request, MapTravelRequest(0, HUB, 0, False, entry_name=ENTRY))
                self.assertEqual(encode_map_travel_request(request), (PACKET, 321))

    def test_named_entry_with_source_and_force_flag(self):
        request = MapTravelRequest(0, HUB, 0, True,
            travel_point_id="general_travel_point", entry_name=ENTRY)
        packet, bits = encode_map_travel_request(request)
        self.assertEqual(decode_map_travel_request(packet, bits), request)

    def test_empty_event_target_is_not_a_malformed_command(self):
        request = MapTravelRequest(0, "", 0, False, entry_name="")
        packet, bits = encode_map_travel_request(request)
        self.assertEqual(decode_map_travel_request(packet, bits), request)

    def test_existing_fast_travel_and_waypoint_formats_are_preserved(self):
        requests = [MapTravelRequest(0, "b1201_pve", 0, False),
                    MapTravelRequest(3, HUB, 2, True)]
        requests.extend(MapTravelRequest(0, "a0303_wildcave_08", 2, False,
            travel_point_id="a0300_travel_point_01", mode=mode)
            for mode in (None, 0, 1, 2, 255))
        for request in requests:
            with self.subTest(request=request):
                packet, bits = encode_map_travel_request(request)
                self.assertEqual(decode_map_travel_request(packet, bits), request)
                self.assertEqual(decode_map_travel_request(packet), request)

    def test_truncated_named_entry_and_extra_data_are_rejected(self):
        for packet in (PACKET[:-1], PACKET + b"\x01", PACKET + b"\x00",
                       PACKET[:-2] + b"\x00\x00"):
            with self.subTest(packet=packet):
                with self.assertRaises(ValueError):
                    decode_map_travel_request(packet)


class EventTravelTests(unittest.TestCase):
    def setUp(self):
        self.server = character_server(self)
        self.server.sock = Mock()
        self.s = Session(("127.0.0.1", 51900), self.server.sock)
        self.s.connected = self.s.gameplay_mode = True
        self.s.gameplay_map = "a0303_wildcave"
        self.s.gameplay_character = self.server.character_selection.active_character
        self.s.send_reliable_ordered = Mock()
        self.s.current_message_bit_length = 328
        self.directory = tempfile.TemporaryDirectory(prefix="dso-event-travel-")
        self.addCleanup(self.directory.cleanup)
        self.catalog = self.load_catalog()
        p = patch.object(CharacterServer, "_event_catalog", lambda _self: self.catalog)
        p.start()
        self.addCleanup(p.stop)

    def load_catalog(self, template="e_chr_2015_challenge_04", state="active"):
        path = Path(self.directory.name) / (template + state + ".json")
        path.write_text(json.dumps({"events": [
            {"event_id": 17, "template_id": template, "state": state}]}), encoding="utf-8")
        return load_event_catalog(DEFAULT_STATIC_DB_PATH, path)

    def handle(self, packet=PACKET, bits=None):
        self.s.current_message_bit_length = len(packet)*8 if bits is None else bits
        with patch.object(CharacterServer, "_begin_map_transfer", return_value=True) as transfer:
            self.server._handle_map_travel(self.s, packet)
        return transfer

    def test_active_event_uses_its_named_entry_without_requiring_a_previous_visit(self):
        self.assertFalse(self.s.gameplay_unlocked_maps)
        call = self.handle()
        call.assert_called_once()
        self.assertEqual(call.call_args.args[2], HUB)
        self.assertEqual(call.call_args.kwargs["spawn_position"],
                         load_local_entry_position(DEFAULT_STATIC_DB_PATH, HUB, ENTRY))
        self.assertEqual(call.call_args.kwargs["reason"], "event-travel")
        self.assertEqual(call.call_args.kwargs["scaling_difficulty"], 0)
        self.assertFalse(self.s.gameplay_unlocked_maps)  # discovery happens on arrival

    def test_a_different_authored_hub_is_preserved(self):
        self.catalog = self.load_catalog("e_ks_blitz_hailstone_mountains")
        packet, bits = encode_map_travel_request(
            MapTravelRequest(0, "b1200_hub", 0, False, entry_name="Respawnpoint"))
        call = self.handle(packet, bits)
        call.assert_called_once()
        self.assertEqual(call.call_args.args[2], "b1200_hub")
        self.assertEqual(call.call_args.kwargs["spawn_position"],
                         load_local_spawn_position(DEFAULT_STATIC_DB_PATH, "b1200_hub"))

    def test_event_without_target_defaults_to_kingscity_spawn(self):
        self.catalog = self.load_catalog("scaling_dungeon_kill_event_01")
        packet, bits = encode_map_travel_request(MapTravelRequest(0, "", 0, False, entry_name=""))
        call = self.handle(packet, bits)
        call.assert_called_once()
        self.assertEqual(call.call_args.args[2], HUB)
        self.assertEqual(call.call_args.kwargs["spawn_position"],
                         load_local_spawn_position(DEFAULT_STATIC_DB_PATH, HUB))

    def test_missing_authored_entry_uses_that_maps_spawn(self):
        event = self.catalog.events[0]
        self.catalog = replace(self.catalog, events=(replace(event,
            template=replace(event.template, teleport_target=HUB + ":missing_entry")),))
        packet, bits = encode_map_travel_request(MapTravelRequest(0, HUB, 0, False, entry_name="missing_entry"))
        call = self.handle(packet, bits)
        call.assert_called_once()
        self.assertEqual(call.call_args.kwargs["spawn_position"],
                         load_local_spawn_position(DEFAULT_STATIC_DB_PATH, HUB))

    def test_expired_upcoming_ineligible_and_unknown_destinations_cannot_teleport(self):
        for state in ("expired", "upcoming"):
            self.catalog = self.load_catalog(state=state)
            self.handle().assert_not_called()
        self.catalog = self.load_catalog()
        for level in (19, 201):
            self.s.gameplay_character.level = level
            self.handle().assert_not_called()
        self.s.gameplay_character.level = 100
        for map_id, entry in (("b1200_hub", "Respawnpoint"), (HUB, "entry_dragan_event"), ("", "")):
            packet, bits = encode_map_travel_request(MapTravelRequest(0, map_id, 0, True, entry_name=entry))
            self.handle(packet, bits).assert_not_called()

    def test_silent_event_does_not_authorize_the_fallback(self):
        self.catalog = self.load_catalog("event_wanted_thief_silent")
        packet, bits = encode_map_travel_request(MapTravelRequest(0, "", 0, False, entry_name=""))
        self.handle(packet, bits).assert_not_called()

    def test_event_travel_preserves_unrelated_armed_inventory_item(self):
        pending = {"item_id": 123, "target_map_id": "b1201_pve", "expires_at": 1e30}
        self.s.gameplay_pending_travel_item = pending
        inventory = self.s.gameplay_inventory
        self.handle().assert_called_once()
        self.assertIs(self.s.gameplay_pending_travel_item, pending)
        self.assertIs(self.s.gameplay_inventory, inventory)

    def test_event_start_retains_quest_and_event_map_gates(self):
        with patch.object(CharacterServer, "_map_quest_accessible", return_value=False):
            self.handle().assert_not_called()
        with patch.object(type(self.catalog), "map_available", return_value=False):
            self.handle().assert_not_called()

    def test_named_travel_object_retains_map_discovery_check(self):
        packet, bits = encode_map_travel_request(MapTravelRequest(0, HUB, 0, False,
            travel_point_id="general_travel_point", entry_name=ENTRY))
        self.handle(packet, bits).assert_not_called()
        self.s.gameplay_unlocked_maps.add(HUB)
        call = self.handle(packet, bits)
        call.assert_called_once()
        self.assertEqual(call.call_args.kwargs["reason"], "fast-travel")

    def test_event_entry_survives_real_handoff_and_duplicate_click(self):
        with patch.object(CharacterServer, "_persist_gameplay_session") as persist, \
             patch.object(self.server, "arm_gameplay") as arm:
            self.server._handle_map_travel(self.s, PACKET)
            self.server._handle_map_travel(self.s, PACKET)
        persist.assert_called_once()
        arm.assert_called_once()
        self.assertEqual(arm.call_args.args[1], HUB)
        self.assertEqual(arm.call_args.args[2].map_id, HUB)
        self.assertEqual(arm.call_args.args[5],
                         load_local_entry_position(DEFAULT_STATIC_DB_PATH, HUB, ENTRY))
        self.assertTrue(self.s.gameplay_exit_transition_started)
        self.s.send_reliable_ordered.assert_called_once()
        self.assertEqual(self.s.send_reliable_ordered.call_args.kwargs["label"], "event-travel-switch-map")
