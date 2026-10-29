"""Raazgul's death must publish a usable Alliance Fortress portal."""

from pathlib import Path
from contextlib import closing
import sqlite3
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dso_server.gameplay.map_event_state import (
    GatedSpawnRow, MapEventRuntime, deduplicate_pw002_wave_three_warriors,
    load_map_event_content, load_map_signal_catalog,
)
from dso_server.gameplay.local_map_state import (
    filter_parallel_world_exits_for_access,
    load_local_map_exits,
    load_local_map_initialization,
    native_scene_exit_variation_entries,
)
from dso_server.protocol.player_init_protocol import (
    EXIT_INFO_COMMAND, decode_event_update, decode_server_command_batch,
)
from dso_server.services.character_server import CharacterServer
from dso_server.services.map_events import (
    MapEventService, _PARALLEL_WORLD_EXIT_BOSSES,
)
from dso_server.services.map_transition import MapTransitionService
from dso_server.protocol.gameplay_protocol import UnlockMapRequest, encode_unlock_map_request


class ParallelWorldExitCatalogTests(unittest.TestCase):
    def test_exit_uses_the_active_clients_guid_for_matching_map_row(self):
        path = Path(__file__).resolve().parents[1] / "data/db/static.db4"
        client_guid = bytes.fromhex("00112233445566778899aabbccddeeff")
        with patch(
            "dso_server.gameplay.local_map_state.active_client_instance_guid_overrides",
            return_value={3: client_guid},
        ) as guid_overrides:
            exits = load_local_map_exits(
                path, "pw001_grimmagstone_01_dun",
                include_inactive_event_sets=True,
            )
        gate = next(
            exit_definition for exit_definition in exits
            if exit_definition.exit_id == "pw001_09_exit_to_pw002_09"
        )
        self.assertEqual(gate.instance_guid, client_guid)
        self.assertEqual(
            guid_overrides.call_args.args[2], "_Instance_InteractExit",
        )

    def test_each_parallel_world_exit_has_a_native_scene_creation_entry(self):
        path = Path(__file__).resolve().parents[1] / "data/db/static.db4"
        for map_id, (_boss_template, event_key) in _PARALLEL_WORLD_EXIT_BOSSES.items():
            with self.subTest(map_id=map_id):
                with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
                    row = db.execute(
                        "SELECT Id, Difficulty FROM _Template_MapAccessGroup "
                        "WHERE AccessGroup=? ORDER BY Difficulty DESC LIMIT 1",
                        (map_id,),
                    ).fetchone()
                self.assertIsNotNone(row)
                access_id, difficulty = row
                exits = load_local_map_exits(
                    path, map_id, include_inactive_event_sets=True,
                    active_event_flags=frozenset({event_key}),
                )
                exits = filter_parallel_world_exits_for_access(
                    path, map_id, exits,
                    selected_map_access_id=access_id,
                    selected_map_difficulty=difficulty,
                )
                entries = native_scene_exit_variation_entries(map_id, exits)
                self.assertEqual(
                    len(entries), 1,
                    f"{map_id} did not produce exactly one selected exit scene entry",
                )
                expected_exit = next(
                    exit_definition for exit_definition in exits
                    if exit_definition.instance_guid == entries[0].instance_guid
                )
                self.assertEqual(
                    entries[0].event_set_row,
                    expected_exit.event_set_row,
                    f"{map_id} native scene entry lost its EventSet gate",
                )

    def test_each_configured_gatekeeper_activates_its_exit_event(self):
        path = Path(__file__).resolve().parents[1] / "data/db/static.db4"
        catalog = load_map_signal_catalog(path)
        service = MapEventService()

        for map_id, (boss_template, event_key) in _PARALLEL_WORLD_EXIT_BOSSES.items():
            if not boss_template:
                continue
            with self.subTest(map_id=map_id):
                runtime = MapEventRuntime(
                    load_map_event_content(path, map_id), catalog,
                    instance_key=("parallel-world-gatekeeper", map_id),
                    initial_flags=frozenset(),
                )
                viewer = SimpleNamespace(
                    flow_lock=Lock(), gameplay_exit_transition_started=False,
                )
                with patch.object(
                    service, "_apply_map_event_transition",
                ):
                    service._schedule_map_local_event_start(
                        runtime, catalog.event(event_key),
                    )
                    self.assertNotIn(event_key, runtime.running)
                    service._open_parallel_world_exit_after_boss_kill(
                        viewer, runtime, (boss_template,),
                    )
                self.assertIn(event_key, runtime.running)

    def test_every_boss_gate_matches_its_authored_kill_rule(self):
        path = Path(__file__).resolve().parents[1] / "data/db/static.db4"
        catalog = load_map_signal_catalog(path)
        kill_rules = catalog.rules_by_event_type["playerkilledmonster"]

        for map_id, (boss_template, event_key) in _PARALLEL_WORLD_EXIT_BOSSES.items():
            with self.subTest(map_id=map_id):
                definition = catalog.event(event_key)
                self.assertIsNotNone(definition)
                self.assertTrue(definition.applies_to(map_id))
                runtime = MapEventRuntime(
                    load_map_event_content(path, map_id), catalog,
                    instance_key=("all-parallel-world-exit-catalog", map_id),
                    initial_flags=frozenset(),
                )
                transition = runtime.begin_event(definition)
                self.assertIsNotNone(transition)
                exits = load_local_map_exits(
                    path, map_id, include_inactive_event_sets=True,
                )
                self.assertTrue(
                    any(
                        exit_definition.event_set_row in transition.enabled_sets
                        and exit_definition.destination_map_id
                        for exit_definition in exits
                    ),
                    f"{map_id} event {event_key} does not enable a map exit",
                )
                if not boss_template:
                    continue
                matching_rules = [
                    rule for rule in kill_rules
                    if map_id.casefold() in rule.maps
                    and boss_template.casefold() in {
                        param.casefold() for param in rule.params
                    }
                ]
                self.assertTrue(
                    matching_rules,
                    f"{map_id} exit gate expects {boss_template}, but no "
                    "PlayerKilledMonster rule reports that boss",
                )


class GrimmagstoneExitTests(unittest.TestCase):
    map_id = "pw001_grimmagstone_01_dun"
    boss_template = "pw001_01_leader_undeadmage_heroic"
    event_key = "pw001_spawn_exit"
    destination = "pw002_{tier:02}_alliance_fortress_01_dun"

    def test_initial_population_does_not_bypass_closed_event_sets(self):
        from dso_server.gameplay.local_monster_state import load_local_monster_encounter

        encounter = load_local_monster_encounter(
            self.path, "pw001_grimmagstone_01_dun", include_spawn_areas=False,
            scaling_difficulty=7,
        )
        # Ordinary mobs come from density areas. Closed ritual and seasonal
        # SpawnPoints must not be used as a substitute fixed population.
        self.assertIsNone(encounter)

    def test_ordinary_pool_excludes_seasonal_and_boss_only_mobs(self):
        from dso_server.gameplay.local_monster_state import _load_map_monster_templates

        templates = _load_map_monster_templates(self.path, "pw001_grimmagstone_01_dun")
        self.assertTrue(templates)
        self.assertIn("pw001_01_normal_skeleton_warrior_heroic", templates)
        self.assertFalse(any(
            "anniversary" in template or "_boss_adds" in template
            or "_guardian_" in template or template.endswith("_pw_event")
            for template in templates
        ))

    def test_ritual_boss_has_one_spawn_and_preserves_archer_adds(self):
        runtime = MapEventRuntime(
            load_map_event_content(self.path, "pw001_grimmagstone_01_dun"),
            load_map_signal_catalog(self.path), instance_key=("ritual",),
            initial_flags=frozenset(), scaling_difficulty=7,
        )
        rows = runtime.gated_spawn_rows((3417,))
        self.assertEqual(sum(r.template_id == "pw001_01_leader_undeadmage_heroic" for r in rows), 1)
        self.assertEqual(sum(r.template_id.endswith("_boss_adds") for r in rows), 6)

    def test_client_ritual_exit_report_cannot_open_gate(self):
        runtime = MapEventRuntime(
            load_map_event_content(self.path, "pw001_grimmagstone_01_dun"),
            load_map_signal_catalog(self.path), instance_key=("ritual",),
            initial_flags=frozenset(), scaling_difficulty=7,
        )
        for signal in ("pw001_spawn_exit_signal", "pw001_spawn_exit_signal_B"):
            viewer = SimpleNamespace(
                gameplay_map_event_runtime=runtime,
                gameplay_client_command_state={0x0113: SimpleNamespace(signal_id=signal)},
            )
            with patch("dso_server.services.map_events.CharacterService._handle_simple_gameplay_command"), patch.object(
                self.service, "_emit_map_signal",
            ) as emit:
                self.service._handle_staging_signal(viewer, b"")
            emit.assert_not_called()
            self.assertNotIn("pw001_spawn_exit", runtime.running)
        for signal in ("pw001_spawn_exit_signal", "pw001_spawn_exit_signal_B"):
            self.service._emit_map_signal(viewer, runtime, signal)
        self.assertNotIn("pw001_spawn_exit", runtime.running)

    def setUp(self):
        self.path = Path(__file__).resolve().parents[1] / "data/db/static.db4"
        catalog = load_map_signal_catalog(self.path)
        self.runtime = MapEventRuntime(
            load_map_event_content(self.path, self.map_id),
            catalog, instance_key=("portal-test",), initial_flags=frozenset(),
            scaling_difficulty=7,
        )
        self.viewer = SimpleNamespace(
            flow_lock=Lock(), gameplay_map=self.runtime.map_id,
            gameplay_map_exits=(), gameplay_exit_transition_started=False,
            gameplay_character=SimpleNamespace(level=100),
            gameplay_client_command_state={}, gameplay_last_move=None,
            gameplay_coordinate_frame=None, gameplay_spawn_position=None,
        )
        self.packets = []
        outer = self

        class Service(MapEventService, MapTransitionService):
            static_database_path = outer.path

            def _map_event_sessions(self, runtime):
                return (outer.viewer,)

            def _schedule_map_local_event_start(self, runtime, definition):
                transition = runtime.begin_event(definition)
                if transition is None:
                    return
                with patch.multiple(
                    outer.service,
                    _retire_event_monsters=lambda *args: None,
                    _unpublish_event_props=lambda *args: None,
                    _publish_event_props=lambda *args: None,
                    _spawn_event_monsters=lambda *args: None,
                    _spawn_event_npcs=lambda *args: None,
                ):
                    outer.service._apply_map_event_transition(
                        runtime, transition,
                    )

        self.service = Service()
        self.sender = patch.object(
            CharacterServer, "_send_world_loot_state_batch",
            side_effect=lambda viewer, packet, bits, *, label:
                self.packets.append((label, packet, bits)),
        )
        self.sender.start()
        self.addCleanup(self.sender.stop)

    def kill_gatekeeper(self):
        self.service._open_parallel_world_exit_after_boss_kill(
            self.viewer, self.runtime,
            (self.boss_template,),
        )

    def client_creates_portal(self, packets, initially_active=False):
        # Model the relevant native-client behavior: EntityEventController
        # checks GetExitInfo's GUID on activation, and 0x00dd invokes OnStart
        # only on an inactive -> active edge. Merely repeating active=True
        # after sending ExitInfo does not retry entity creation.
        expected = self.viewer.gameplay_map_exits[0].instance_guid
        cached_guids = set()
        active, created = initially_active, False
        for label, packet, bits in packets:
            if packet[0] == 0x85:
                command, = decode_server_command_batch(packet, bits)
                if label == "map-event-native-exit-variation-info":
                    variation_guids = {
                        entry.instance_guid for entry in command.value.entries
                    }
                    cached_guids.update(variation_guids)
                    if active and expected in variation_guids:
                        created = True
                else:
                    cached_guids.update(e.instance_guid for e in command.value.exits)
            else:
                for event in decode_event_update(packet, bits).events:
                    if event.event_name != self.event_key:
                        continue
                    enabled = event.flags[0]
                    if enabled != active:
                        created = enabled and expected in cached_guids
                    active = enabled
        return created

    def test_only_selected_tier_guid_reaches_the_client_before_event_activation(self):
        # Native ExitInfo handling merges its cache. EntityEventController
        # forces a variation to spawn when its Name/GUID is cached, even if
        # field_10 is zero. A later smaller batch does not remove stale GUIDs.
        with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as db:
            accesses = db.execute(
                "SELECT Id, Difficulty FROM _Template_MapAccessGroup "
                "WHERE AccessGroup=? ORDER BY Difficulty", (self.map_id,),
            ).fetchall()
        all_exits = load_local_map_exits(
            self.path, self.map_id, include_inactive_event_sets=True,
        )
        tier_guids = {
            exit.instance_guid for exit in all_exits
            if exit.destination_map_id in {
                self.destination.format(tier=tier) for tier in (5, 7, 9)
            }
        }
        self.assertEqual(len(tier_guids), 3)
        for access_id, tier in accesses:
            with self.subTest(access=access_id, difficulty=tier):
                target = self.destination.format(tier=tier)
                expected = next(
                    exit for exit in all_exits
                    if exit.destination_map_id == target
                )
                _, initial = load_local_map_initialization(
                    self.path, self.map_id,
                    include_inactive_event_sets=True,
                    selected_map_access_id=access_id,
                )
                cache = {entry.exit_id: entry.instance_guid for entry in initial.exits}
                self.assertEqual(set(cache.values()) & tier_guids, {expected.instance_guid})
                self.assertTrue(all(entry.field_10 == 0 for entry in initial.exits))
                self.runtime = MapEventRuntime(
                    load_map_event_content(self.path, self.map_id),
                    load_map_signal_catalog(self.path),
                    instance_key=("portal-cache-test", tier),
                    initial_flags=frozenset(), scaling_difficulty=9,
                )
                self.viewer.gameplay_map_access_id = access_id
                self.viewer.gameplay_map_difficulty = 9
                self.packets.clear()
                self.kill_gatekeeper()
                saw_activation = False
                for label, packet, bits in self.packets:
                    if packet[0] == 0x85:
                        command, = decode_server_command_batch(packet, bits)
                        if label == "map-event-native-exit-variation-info":
                            self.assertEqual(
                                {entry.instance_guid for entry in command.value.entries}
                                & tier_guids,
                                {expected.instance_guid},
                            )
                        else:
                            cache.update({entry.exit_id: entry.instance_guid for entry in command.value.exits})
                        self.assertEqual(set(cache.values()) & tier_guids, {expected.instance_guid})
                    else:
                        update = decode_event_update(packet, bits)
                        if any(e.event_name == self.event_key and e.flags[0] for e in update.events):
                            saw_activation = True
                            self.assertEqual(set(cache.values()) & tier_guids, {expected.instance_guid})
                self.assertTrue(saw_activation)
                command, = decode_server_command_batch(self.packets[-1][1], self.packets[-1][2])
                self.assertEqual([e.map_id for e in command.value.exits], [target])
                _, late_join = load_local_map_initialization(
                    self.path, self.map_id,
                    active_event_flags=self.runtime.active_flags(),
                    include_inactive_event_sets=True,
                    selected_map_access_id=access_id,
                )
                self.assertEqual(
                    {e.instance_guid for e in late_join.exits} & tier_guids,
                    {expected.instance_guid},
                )

    def test_tier_filter_preserves_unrelated_hub_exit(self):
        all_exits = load_local_map_exits(
            self.path, self.map_id, include_inactive_event_sets=True,
        )
        selected = filter_parallel_world_exits_for_access(
            self.path, self.map_id, all_exits, selected_map_difficulty=5,
        )
        self.assertEqual(
            [e for e in selected if e.destination_map_id == "pw000_hub"],
            [e for e in all_exits if e.destination_map_id == "pw000_hub"],
        )

    def test_boss_death_starts_exit_event_once(self):
        self.kill_gatekeeper()
        self.assertIn(self.event_key, self.runtime.running)
        self.assertEqual([label for label, _, _ in self.packets], [
            "map-event-exit-info", "map-event-update",
            "map-event-native-exit-variation-info",
            "map-event-exit-info",
        ])
        command, = decode_server_command_batch(
            self.packets[0][1], self.packets[0][2],
        )
        self.assertEqual(command.command_id, EXIT_INFO_COMMAND)
        self.assertEqual(command.actor_id, 0)
        self.assertTrue(all(entry.field_10 == 0 for entry in command.value.exits))
        update = decode_event_update(self.packets[1][1], self.packets[1][2])
        exit_event = next(
            entry for entry in update.events
            if entry.event_name == self.event_key
        )
        self.assertEqual(exit_event.event_id, 0)
        self.assertTrue(exit_event.flags[0])
        command, = decode_server_command_batch(
            self.packets[-1][1], self.packets[-1][2],
        )
        active_exits = [e for e in command.value.exits if e.field_10 == 1]
        self.assertEqual(len(active_exits), 1)
        self.assertEqual(active_exits[0].map_id, self.destination.format(tier=7))
        self.assertTrue(self.client_creates_portal(self.packets))

    def test_premature_running_event_is_rebuilt_once_after_gatekeeper_kill(self):
        transition = self.runtime.begin_event(self.runtime.catalog.event(self.event_key))
        with patch.multiple(
            self.service,
            _retire_event_monsters=lambda *args: None,
            _unpublish_event_props=lambda *args: None,
            _publish_event_props=lambda *args: None,
            _spawn_event_monsters=lambda *args: None,
            _spawn_event_npcs=lambda *args: None,
        ):
            self.service._apply_map_event_transition(self.runtime, transition)
        self.packets.clear()
        self.kill_gatekeeper()
        self.assertTrue(self.client_creates_portal(self.packets, initially_active=True))
        self.packets.clear()
        self.kill_gatekeeper()
        self.assertFalse(self.packets)

    def test_boss_death_during_map_transfer_does_not_restart_event(self):
        self.viewer.gameplay_exit_transition_started = True
        self.kill_gatekeeper()
        self.assertFalse(self.packets)
        self.assertNotIn(self.event_key, self.runtime.running)

    def test_map_transition_primes_exit_cache_before_event_activation(self):
        transition = self.runtime.begin_event(self.runtime.catalog.event(self.event_key))
        with patch.multiple(
            self.service,
            _retire_event_monsters=lambda *args: None,
            _unpublish_event_props=lambda *args: None,
            _publish_event_props=lambda *args: None,
            _spawn_event_monsters=lambda *args: None,
            _spawn_event_npcs=lambda *args: None,
        ):
            self.service._apply_map_event_transition(self.runtime, transition)
        self.assertEqual(self.packets[0][0], "map-event-exit-info")
        command, = decode_server_command_batch(
            self.packets[0][1], self.packets[0][2],
        )
        self.assertFalse(command.value.exits)
        event_update = next(
            decode_event_update(packet, bits)
            for label, packet, bits in self.packets
            if label == "map-event-update"
        )
        self.assertTrue(event_update.events)
        self.assertTrue(all(entry.event_id == 0 for entry in event_update.events))
        command, = decode_server_command_batch(
            self.packets[-1][1], self.packets[-1][2],
        )
        active_exits = [e for e in command.value.exits if e.field_10 == 1]
        self.assertEqual(len(active_exits), 1)
        self.assertEqual(active_exits[0].map_id, self.destination.format(tier=7))
        self.assertTrue(self.client_creates_portal(self.packets))

    def test_ordinary_monster_does_not_open_portal(self):
        self.service._open_parallel_world_exit_after_boss_kill(
            self.viewer, self.runtime,
            ("pw001_01_normal_skeleton_warrior_heroic",),
        )
        self.assertFalse(self.packets)

    def test_additional_gatekeeper_kills_do_not_reset_open_portal(self):
        self.kill_gatekeeper()
        self.packets.clear()
        self.kill_gatekeeper()
        self.assertFalse(self.packets)

    def test_click_on_published_portal_routes_to_same_tier(self):
        self.runtime.begin_event(self.runtime.catalog.event(self.event_key))
        self.service._publish_map_exits(self.runtime, (self.viewer,))
        exit_definition, = self.viewer.gameplay_map_exits
        target = self.destination.format(tier=7)
        packet, _bits = encode_unlock_map_request(UnlockMapRequest(
            target, target, exit_definition.exit_id, False,
        ))
        with patch.object(CharacterServer, "_accept_gameplay_state_command"), patch.object(
            CharacterServer, "_activate_map_exit", return_value=True,
        ) as activate:
            self.service._handle_map_exit(self.viewer, packet)
        activate.assert_called_once_with(
            self.service, self.viewer, exit_definition,
            trigger="client cmd 0x0060", destination_access_id=target,
        )

    def test_portal_is_not_rebuilt_during_map_transfer(self):
        self.viewer.gameplay_exit_transition_started = True
        self.kill_gatekeeper()
        self.assertFalse(self.packets)

    def test_click_on_alternate_tier_portal_routes_to_instance_tier(self):
        self.runtime.begin_event(self.runtime.catalog.event(self.event_key))
        self.service._publish_map_exits(self.runtime, (self.viewer,))
        exit_definition, = self.viewer.gameplay_map_exits
        alternate_tier_exit_id = (
            "pw001_09_exit_to_pw002_09"
            if self.map_id.startswith("pw001")
            else "pw002_09_exit_to_pw003_09"
        )
        alternate_target = self.destination.format(tier=9)
        expected_target = self.destination.format(tier=7)
        packet, _bits = encode_unlock_map_request(UnlockMapRequest(
            alternate_target, alternate_target, alternate_tier_exit_id, False,
        ))
        with patch.object(CharacterServer, "_accept_gameplay_state_command"), patch.object(
            CharacterServer, "_activate_map_exit", return_value=True,
        ) as activate:
            self.service._handle_map_exit(self.viewer, packet)
        activate.assert_called_once_with(
            self.service, self.viewer, exit_definition,
            trigger="client cmd 0x0060", destination_access_id=expected_target,
        )


class AllianceFortressExitTests(GrimmagstoneExitTests):
    map_id = "pw002_alliance_fortress_01_dun"
    boss_template = "pw002_01_leader_warrior_heroic"
    event_key = "pw002_spawn_exit"
    destination = "pw003_{tier:02}_grimmag_01_boss"

    def test_wave_three_stacked_dragon_warrior_rows_collapse_to_one(self):
        rows = load_map_event_content(self.path, self.map_id).spawn_rows
        warriors = tuple(
            row for row in rows
            if row.event_set_row == 3438
            and row.template_id == self.boss_template
        )
        self.assertEqual(len(warriors), 1)

        raw_rows = tuple(
            (index, bytes(64), self.boss_template, 3438)
            for index in range(11)
        )
        self.assertEqual(
            len(deduplicate_pw002_wave_three_warriors(self.map_id, raw_rows)),
            1,
        )
        self.assertEqual(
            len(deduplicate_pw002_wave_three_warriors(
                "pw001_grimmagstone_01_dun", raw_rows,
            )),
            11,
        )


if __name__ == "__main__":
    unittest.main()
