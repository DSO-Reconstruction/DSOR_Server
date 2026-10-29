"""Crypt exits need native scene creation, icons and normal difficulty routing."""

from pathlib import Path
import sqlite3
import struct
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dso_server.gameplay.local_map_state import (
    load_local_map_exits,
    load_local_map_initialization,
    _active_client_map_database_path,
)
from dso_server.protocol.gameplay_protocol import UnlockMapRequest, encode_unlock_map_request
from dso_server.services.character_server import CharacterServer
from dso_server.services.map_transition import MapTransitionService
from dso_server.protocol.player_init_protocol import (
    EVENT_UPDATE_COMMAND,
    EXIT_INFO_COMMAND,
    VARIATION_INFO_COMMAND,
    ServerCommand,
    VariationInfo,
    VariationInfoEntry,
    build_local_player_init_commands,
    decode_server_command_batch,
    encode_server_command_batch,
)


class CryptExitVisibilityTests(unittest.TestCase):
    path = Path(__file__).resolve().parents[1] / "data/db/static.db4"

    def test_heroic_difficulty_zero_access_routes_to_supported_tier(self):
        service = MapTransitionService()
        service.static_database_path = self.path
        definition = SimpleNamespace(
            exit_id="kingshill_to_cardhun", name="kingshill_to_cardhun",
            destination_map_id="pw199_cardhun_dun", position=(0.0, 0.0, 0.0),
        )
        viewer = SimpleNamespace(
            flow_lock=Lock(), gameplay_map="a0200_kingscity",
            gameplay_client_command_state={}, gameplay_map_exits=(definition,),
            gameplay_last_move=None, gameplay_coordinate_frame=None,
            gameplay_spawn_position=definition.position,
            gameplay_character=SimpleNamespace(level=55),
        )
        packet, _ = encode_unlock_map_request(UnlockMapRequest(
            "pw199_cardhun_dun_00", "pw199_cardhun_dun",
            definition.exit_id, False,
        ))
        with patch.object(CharacterServer, "_accept_gameplay_state_command"), patch.object(
            CharacterServer, "_activate_map_exit", return_value=True,
        ) as activate:
            service._handle_map_exit(viewer, packet)
        activate.assert_called_once_with(
            service, viewer, definition, trigger="client cmd 0x0060",
            destination_access_id="pw199_cardhun_dun_01",
        )

    def test_map_template_name_is_not_accepted_as_concrete_access(self):
        service = MapTransitionService()
        service.static_database_path = self.path
        definition = SimpleNamespace(
            exit_id="kingshill_to_cardhun", name="kingshill_to_cardhun",
            destination_map_id="pw199_cardhun_dun", position=(0.0, 0.0, 0.0),
        )
        viewer = SimpleNamespace(
            flow_lock=Lock(), gameplay_map="a0200_kingscity",
            gameplay_client_command_state={}, gameplay_map_exits=(definition,),
            gameplay_last_move=None, gameplay_coordinate_frame=None,
            gameplay_spawn_position=definition.position,
            gameplay_character=SimpleNamespace(level=55),
        )
        packet, _ = encode_unlock_map_request(UnlockMapRequest(
            "pw199_cardhun_dun", "pw199_cardhun_dun", definition.exit_id, False,
        ))
        with patch.object(CharacterServer, "_accept_gameplay_state_command"), patch.object(
            CharacterServer, "_activate_map_exit", return_value=True,
        ) as activate:
            service._handle_map_exit(viewer, packet)
        activate.assert_not_called()

    def test_variation_wire_matches_native_guid_and_event_set_reader(self):
        guid = bytes.fromhex("7f86d707c21141ce8b9600310d8c88ca")
        command = ServerCommand(
            VARIATION_INFO_COMMAND, VariationInfo((VariationInfoEntry(guid),)), 0,
        )
        packet, bits = encode_server_command_batch((command,), include_terminator=True)
        # Native record decoder reads a u16-length GUID and one int32, with
        # no transform or parent-node field between this and the actor trailer.
        expected = (
            b"\x85" + struct.pack("<HIH", VARIATION_INFO_COMMAND, 1, 16)
            + guid + struct.pack("<iI", -1, 0) + b"\xff"
        )
        self.assertEqual(packet, expected)
        self.assertEqual(bits, len(expected) * 8)
        self.assertEqual(decode_server_command_batch(packet, bits), (command,))

    def test_all_crypt_tiers_create_both_destination_families(self):
        for map_id, destinations in (
            ("a0202_kingscrypt01", ("a0203_kingscrypt02",)),
            ("a0203_kingscrypt02", ("a0202_kingscrypt01", "a0204_boss")),
        ):
            authored = load_local_map_exits(self.path, map_id)
            for tier in (0, 1, 2, 3, 4, 6, 8):
                suffix = f"_{tier:02}" if tier else ""
                with self.subTest(map=map_id, tier=tier):
                    commands, exits = load_local_map_initialization(
                        self.path, map_id, selected_map_access_id=map_id + suffix,
                        include_inactive_event_sets=True,
                    )
                    create, = (c for c in commands if c.command_id == VARIATION_INFO_COMMAND)
                    expected = {
                        e.instance_guid for e in authored
                        if e.destination_map_id in {d + suffix for d in destinations}
                    }
                    self.assertEqual(len(expected), len(destinations))
                    self.assertEqual({e.instance_guid for e in create.value.entries}, expected)
                    self.assertTrue(all(e.event_set_row == -1 for e in create.value.entries))
                    self.assertTrue(expected <= {
                        e.instance_guid for e in exits.exits if e.field_10
                    })

    def test_created_native_exits_retain_their_minimap_metadata(self):
        for map_id in ("a0202_kingscrypt01", "a0203_kingscrypt02"):
            with self.subTest(map=map_id):
                commands, _ = load_local_map_initialization(self.path, map_id)
                create, = (c for c in commands if c.command_id == VARIATION_INFO_COMMAND)
                map_path = _active_client_map_database_path(map_id) or (
                    self.path.parent.parent / "maps" / (map_id + ".db4")
                )
                with sqlite3.connect(f"file:{map_path.as_posix()}?mode=ro", uri=True) as db:
                    for entry in create.value.entries:
                        row = db.execute(
                            "SELECT LocationMapIcon, ExitURL, Graphics "
                            "FROM _Instance_InteractExit WHERE Guid=?",
                            (entry.instance_guid,),
                        ).fetchone()
                        self.assertIsNotNone(row)
                        self.assertEqual(row[0], "ui/icon_map_exit")
                        self.assertTrue(row[1].startswith(("a0202", "a0203", "a0204")))
                        self.assertTrue(row[2])

    def test_native_difficulty_choice_routes_to_the_selected_access(self):
        service = MapTransitionService()
        service.static_database_path = self.path
        for map_id, destination in (
            ("a0202_kingscrypt01", "a0203_kingscrypt02"),
            ("a0203_kingscrypt02", "a0202_kingscrypt01"),
            ("a0203_kingscrypt02", "a0204_boss"),
        ):
            with self.subTest(map=map_id, destination=destination):
                definition = next(
                    e for e in load_local_map_exits(self.path, map_id)
                    if e.destination_map_id == destination
                )
                viewer = SimpleNamespace(
                    flow_lock=Lock(), gameplay_map=map_id,
                    gameplay_client_command_state={}, gameplay_map_exits=(definition,),
                    gameplay_last_move=None, gameplay_coordinate_frame=None,
                    gameplay_spawn_position=definition.position,
                    gameplay_character=SimpleNamespace(level=100),
                )
                selected_access = destination + "_02"
                packet, _ = encode_unlock_map_request(UnlockMapRequest(
                    selected_access, destination, definition.exit_id, False,
                ))
                with patch.object(CharacterServer, "_accept_gameplay_state_command"), patch.object(
                    CharacterServer, "_activate_map_exit", return_value=True,
                ) as activate:
                    service._handle_map_exit(viewer, packet)
                activate.assert_called_once_with(
                    service, viewer, definition, trigger="client cmd 0x0060",
                    destination_access_id=selected_access,
                )

    def test_initialization_primes_cache_creates_exits_then_refreshes_state(self):
        map_commands, exit_info = load_local_map_initialization(
            self.path, "a0203_kingscrypt02", selected_map_difficulty=0,
        )
        new_player = SimpleNamespace(
            actor_id=1, fields_110=(0,) * 8, player_state_scalars=(0,) * 18,
            fields_170=(0,) * 7, field_190=0, fields_198=(), field_1a8=0,
        )
        commands = build_local_player_init_commands(
            new_player, None, map_commands, exit_info=exit_info, actor_id=1,
        )
        ids = [c.command_id for c in commands]
        create = ids.index(VARIATION_INFO_COMMAND)
        event = ids.index(EVENT_UPDATE_COMMAND)
        snapshots = [i for i, c in enumerate(ids) if c == EXIT_INFO_COMMAND]
        self.assertEqual(len(snapshots), 2)
        self.assertLess(snapshots[0], create)
        self.assertLess(create, event)
        self.assertLess(event, snapshots[1])


if __name__ == "__main__":
    unittest.main()
