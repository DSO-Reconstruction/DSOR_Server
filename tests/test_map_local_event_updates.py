from __future__ import annotations

from pathlib import Path
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dso_server.gameplay.local_map_state import load_local_map_initialization
from dso_server.gameplay.map_event_state import (
    MapEventRuntime, load_map_event_content, load_map_signal_catalog,
)
from dso_server.protocol.player_init_protocol import (
    ACTORS_ENTER_VICINITY_COMMAND, EVENT_UPDATE_COMMAND, EXIT_INFO_COMMAND,
    NEW_NPC_COMMAND, NPC_INFO_COMMAND, VARIATION_INFO_COMMAND,
    EventUpdate, EventUpdateEntry, build_local_player_init_commands,
    decode_event_update, decode_server_command_batch, encode_server_command_batch,
)
from dso_server.services.character_server import CharacterServer
from dso_server.services.events import _baseline_map_local_event_entries
from dso_server.services.map_events import MapEventService


STATIC_DATABASE = (
    Path(__file__).resolve().parents[1] / 'data' / 'db' / 'static.db4'
)


class MapLocalEventUpdateTests(unittest.TestCase):
    def test_kingshill_creates_npc_actors_after_event_snapshot(self):
        map_commands, exits = load_local_map_initialization(
            STATIC_DATABASE, 'a0200_kingscity',
            include_inactive_event_sets=True,
        )
        new_player = SimpleNamespace(
            actor_id=1, fields_110=(0,) * 8, player_state_scalars=(0,) * 18,
            fields_170=(0,) * 7, field_190=0, fields_198=(), field_1a8=0,
        )
        commands = build_local_player_init_commands(
            new_player, None, map_commands, exit_info=exits, actor_id=1,
        )
        ids = [command.command_id for command in commands]
        event_update = ids.index(EVENT_UPDATE_COMMAND)
        first_npc = ids.index(NEW_NPC_COMMAND)
        self.assertLess(event_update, first_npc)
        self.assertNotIn(ACTORS_ENTER_VICINITY_COMMAND, ids)
        self.assertEqual(
            commands[first_npc].command_id, NEW_NPC_COMMAND,
        )
        expected_actors = tuple(
            command.value.actor_id for command in map_commands
            if command.command_id == NEW_NPC_COMMAND
        )
        self.assertEqual(len(set(expected_actors)), len(expected_actors))

        # NPC creation stays in the same ordered init stream, after the
        # EventUpdate that materializes its authored scene entity.
        packet, bits = encode_server_command_batch(
            commands[event_update:],
        )
        decoded = decode_server_command_batch(packet, bits)
        self.assertEqual(decoded[0], commands[event_update])
        vendors = {
            command.value.template_id: command.value.actor_id
            for command in decoded if command.command_id == NEW_NPC_COMMAND
        }
        infos = {
            command.value.actor_id: command.value
            for command in decoded if command.command_id == NPC_INFO_COMMAND
        }
        for template in (
            'e_event_merchant_grima', 'e_event_merchant_01',
            'e_travel_change_quest_npc', 'e_anniversary_merchant_phestos',
            'a0200_gambler_trader', 'a0200_premium_trader',
        ):
            with self.subTest(template=template):
                actor_id = vendors[template]
                self.assertTrue(infos[actor_id].enabled)
                self.assertTrue(infos[actor_id].template_id)
                self.assertNotEqual(infos[actor_id].category, 0xFF)

    def test_initial_hiraja_gate_creation_follows_event_snapshot(self):
        gate = 'h0001_hunt_for_a_legend_7_spawn_exit_to_h0006_01'
        map_commands, exits = load_local_map_initialization(
            STATIC_DATABASE, 'h0001_hub',
            active_event_flags=frozenset({gate}),
            include_inactive_event_sets=True,
        )
        new_player = SimpleNamespace(
            actor_id=1, fields_110=(0,) * 8, player_state_scalars=(0,) * 18,
            fields_170=(0,) * 7, field_190=0, fields_198=(), field_1a8=0,
        )
        commands = build_local_player_init_commands(
            new_player, None, map_commands, exit_info=exits, actor_id=1,
            event_update=EventUpdate((EventUpdateEntry(
                0, gate, (True, True, False, False), None, None, (),
            ),)),
        )
        ids = [command.command_id for command in commands]
        creation = ids.index(VARIATION_INFO_COMMAND)
        event = ids.index(EVENT_UPDATE_COMMAND)
        snapshots = [i for i, value in enumerate(ids) if value == EXIT_INFO_COMMAND]
        self.assertLess(snapshots[0], event)
        self.assertLess(event, creation)
        self.assertLess(creation, snapshots[-1])
        self.assertEqual(commands[creation].value.entries[0].event_set_row, 2094)

    def test_hiraja_gate_is_created_only_after_its_event_is_active(self):
        gate = 'h0001_hunt_for_a_legend_7_spawn_exit_to_h0006_01'
        catalog = load_map_signal_catalog(STATIC_DATABASE)
        runtime = MapEventRuntime(
            load_map_event_content(STATIC_DATABASE, 'h0001_hub'), catalog,
            instance_key=('hiraja-gate',), initial_flags=frozenset(),
        )
        transition = runtime.begin_event(catalog.event(gate))
        viewer = SimpleNamespace(
            flow_lock=RLock(), gameplay_map='h0001_hub',
            gameplay_map_access_id='h0001_hub', gameplay_map_difficulty=0,
        )
        service = MapEventService()
        service.static_database_path = STATIC_DATABASE
        with patch.object(service, '_map_event_sessions', return_value=(viewer,)), \
                patch.object(service, '_publish_event_props'), \
                patch.object(service, '_spawn_event_monsters'), \
                patch.object(service, '_spawn_event_npcs'), \
                patch.object(CharacterServer, '_send_world_loot_state_batch') as send:
            service._apply_map_event_transition(runtime, transition)

        commands = []
        for call in send.call_args_list:
            packet, bits = call.args[1:]
            if packet[0] == 0x84:
                commands.append(SimpleNamespace(
                    command_id=EVENT_UPDATE_COMMAND,
                    value=decode_event_update(packet, bits),
                ))
            else:
                commands.extend(decode_server_command_batch(packet, bits))
        self.assertEqual([command.command_id for command in commands], [
            EXIT_INFO_COMMAND, EVENT_UPDATE_COMMAND,
            VARIATION_INFO_COMMAND, EXIT_INFO_COMMAND,
        ])
        self.assertTrue(next(
            event.flags[0] for event in commands[1].value.events
            if event.event_name == gate
        ))
        creation, = commands[2].value.entries
        self.assertEqual(creation.event_set_row, 2094)
        closed, = commands[0].value.exits
        opened = next(
            exit for exit in commands[3].value.exits
            if exit.exit_id == 'h0001_exit_to_h0006'
        )
        self.assertEqual(closed.field_10, 0)
        self.assertEqual(opened.field_10, 1)
        self.assertEqual(creation.instance_guid, opened.instance_guid)

    def test_hiraja_update_excludes_foreign_references_and_keeps_gate_active(self):
        gate = 'h0001_hunt_for_a_legend_7_spawn_exit_to_h0006_01'
        events = {
            name: SimpleNamespace(key=name, event_id=name, maps=frozenset(maps))
            for name, maps in (
                (gate, ('h0001_hub',)),
                ('a0001_start_dun_exit_01', ('a0001_start_dun',)),
                ('stopped_local', ('h0001_hub',)),
                ('authored_without_map', ()),
            )
        }
        runtime = SimpleNamespace(
            lock=RLock(), map_id='h0001_hub', running={gate: object()},
            active_flags=lambda: frozenset({gate}),
            content=SimpleNamespace(referenced_events=frozenset(events)),
            catalog=SimpleNamespace(event=events.get),
        )
        transition = SimpleNamespace(event=events[gate], active=True)
        with patch.object(CharacterServer, '_send_world_loot_state_batch') as send:
            CharacterServer._broadcast_map_event_update(
                SimpleNamespace(), runtime, (object(),), transition,
            )
        packet, bits = send.call_args.args[1:]
        entries = {
            entry.event_name: entry.flags[0]
            for entry in decode_event_update(packet, bits).events
        }
        self.assertEqual(entries, {
            gate: True,
            'stopped_local': False,
            'authored_without_map': False,
        })

    def test_kingshill_activates_thabo_before_npc_initialization(self):
        entries = _baseline_map_local_event_entries(
            STATIC_DATABASE, 'a0200_kingscity'
        )
        thabo = next(
            entry for entry in entries
            if entry.event_name == 'a0200_unlock_thabo'
        )
        self.assertEqual(thabo.flags, (True, True, False, False))
        self.assertIsNone(thabo.starts_at)
        self.assertIsNone(thabo.ends_at)

    def test_unconditional_hub_does_not_receive_kingshill_event(self):
        self.assertEqual(
            _baseline_map_local_event_entries(
                STATIC_DATABASE, 'b1100_hub'
            ),
            (),
        )

    def test_shared_desert_map_activates_only_selected_color(self):
        entries = _baseline_map_local_event_entries(
            STATIC_DATABASE,
            'e_doe_map_02',
            ('e_doe_map_02_white',),
        )
        self.assertEqual(
            tuple(entry.event_name for entry in entries),
            ('e_doe_map_02_white',),
        )


if __name__ == '__main__':
    unittest.main()
