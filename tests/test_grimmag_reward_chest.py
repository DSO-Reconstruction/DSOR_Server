"""Arena chest visibility must survive the quest-prop filter and boss lock."""

from dataclasses import replace
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dso_server.gameplay.local_map_state import (
    active_local_map_event_flags, local_prop_actor_base,
)
from dso_server.gameplay.map_event_state import (
    MapEventRuntime, load_map_event_content, load_map_signal_catalog,
)
from dso_server.gameplay.quest_content import (
    QuestProgress, QUEST_STATE_CLOSED, load_quest_catalog,
)
from dso_server.paths import DEFAULT_QUESTS_PATH
from dso_server.protocol.player_init_protocol import decode_server_command_batch
from dso_server.protocol.prop_protocol import (
    PROP_STATE_ACTIVE, PROP_STATE_USED, PropInteraction, encode_prop_interaction,
)
from dso_server.services.character_server import CharacterServer
from dso_server.services.events import (
    _baseline_map_local_event_entries, _declared_map_local_event_entries,
)
from dso_server.services.map_events import (
    MapEventService, _PARALLEL_WORLD_BOSS_REWARD_CHESTS,
)
from dso_server.services.quests import QuestService


class GrimmagRewardChestTests(unittest.TestCase):
    map_id = "pw003_grimmag_01_boss"
    quest_id = "pw001-pw002-pw003-n-re"

    def setUp(self):
        self.path = Path(__file__).resolve().parents[1] / "data/db/static.db4"
        self.runtime = MapEventRuntime(
            load_map_event_content(self.path, self.map_id),
            load_map_signal_catalog(self.path),
            instance_key=("chest-test",),
            initial_flags=active_local_map_event_flags(self.map_id),
        )
        self.viewer = SimpleNamespace(
            flow_lock=Lock(), gameplay_map=self.map_id,
            gameplay_map_difficulty=5,
            gameplay_map_event_runtime=self.runtime,
            gameplay_quest_log={}, gameplay_quest_props_visible=set(),
            gameplay_client_command_state={}, selected_character_id=123,
            send_reliable_ordered=Mock(),
        )
        self.service = MapEventService()
        self.service._spawn_map_chest_loot = Mock(return_value=1)
        self.service._fire_map_signal = Mock()
        self.chest = next(
            state.definition for state in self.runtime.published_props()
            if state.definition.instance_id == "pw_run_lootchest"
        )

    def test_initial_prop_snapshot_includes_chests_without_quest(self):
        info = MapEventService._map_event_prop_info(self.viewer)
        self.assertTrue(info.entries)
        # Tier 5 (Infernal) has its own MapVariation chest; the tier 7 and 9
        # copies share the same boss-side transform and must not be sent.
        self.assertEqual(
            {entry.template_id for entry in info.entries},
            {"t000_state_chest_loot_04"},
        )
        self.assertEqual(len(info.entries), 1)
        self.assertTrue(all(entry.field_24 == PROP_STATE_ACTIVE for entry in info.entries))

    def test_reward_chest_selection_follows_map_difficulty(self):
        expected = {
            5: "t000_state_chest_loot_04",
            7: "t000_state_chest_loot_04",
            9: "t000_state_chest_loot_05",
        }
        for difficulty, template_id in expected.items():
            with self.subTest(difficulty=difficulty):
                self.viewer.gameplay_map_difficulty = difficulty
                info = MapEventService._map_event_prop_info(self.viewer)
                self.assertEqual(len(info.entries), 1)
                self.assertEqual(info.entries[0].template_id, template_id)

    def test_quest_prop_sync_reveals_only_selected_chest(self):
        packets = []
        with patch.object(
            CharacterServer, "_send_world_loot_state_batch",
            side_effect=lambda viewer, packet, bits, **kwargs: packets.append(
                (packet, bits)
            ),
        ):
            QuestService()._quest_sync_props(self.viewer)
        entries = [
            entry
            for packet, bits in packets
            for command in decode_server_command_batch(packet, bits)
            if getattr(command.value, "entries", None) is not None
            for entry in command.value.entries
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].template_id, "t000_state_chest_loot_04")
        self.assertEqual(entries[0].prop_id, self.chest.prop_id)

    def test_reward_chests_use_fresh_zero_guid_prop_ids(self):
        # The client only creates a PropInfo prop when its GUID is empty;
        # otherwise it looks the entity up and drops the entry if the
        # quest-controlled authored chest entity is absent.
        chests = [
            state.definition for state in self.runtime.published_props()
            if state.definition.instance_id.casefold()
            in {"pw_run_lootchest", "pw_run_lootchest_gold"}
        ]
        self.assertEqual(len(chests), 3)
        self.assertEqual(
            {chest.variation_difficulty for chest in chests}, {5, 7, 9},
        )
        self.assertTrue(all(chest.instance_guid == bytes(16) for chest in chests))
        self.assertEqual(len({chest.prop_id for chest in chests}), len(chests))
        authored_ids = {
            local_prop_actor_base(self.path, self.map_id) + row_id - 1
            for row_id in range(1, 4)
        }
        # The published chests must use ids beyond the authored scene rows so
        # no hidden authored prop captures the PropInfo update.
        self.assertTrue(all(chest.prop_id not in authored_ids for chest in chests))
        self.assertTrue(all(chest.prop_id > max(authored_ids) for chest in chests))
        # They stay at the authored boss-side transform instead of being moved
        # to the arena entry.
        self.assertEqual(
            {chest.position for chest in chests},
            {(-10.044989585876465, 0.2845880091190338, 44.881248474121094)},
        )

    def test_arena_event_update_uses_client_map_local_event_id(self):
        baseline = next(
            entry for entry in _baseline_map_local_event_entries(
                self.path, self.map_id,
            ) if entry.event_name == "pw003_spawn_chest"
        )
        declared = next(
            entry for entry in _declared_map_local_event_entries(
                self.path, self.map_id,
            ) if entry.event_name == "pw003_spawn_chest"
        )
        self.assertEqual(baseline.event_id, 0)
        self.assertTrue(baseline.flags[0])
        self.assertEqual(declared.event_id, 0)

    def test_incomplete_and_closed_quest_do_not_hide_arena_chest(self):
        reward_task = load_quest_catalog(
            DEFAULT_QUESTS_PATH
        ).quest(self.quest_id).tasks[-1]
        reward_switch = reward_task.switch_definitions[0].switch_id
        for progress in (
            QuestProgress(self.quest_id),
            QuestProgress(self.quest_id, state=QUEST_STATE_CLOSED),
        ):
            with self.subTest(state=progress.state):
                self.viewer.gameplay_quest_log = {self.quest_id: progress}
                self.assertEqual(
                    QuestService._quest_prop_operation(self.viewer, self.chest),
                    "",
                )
                info = MapEventService._map_event_prop_info(self.viewer)
                self.assertTrue(info.entries)
                self.assertTrue(all(
                    entry.field_48 == "" for entry in info.entries
                ))

    def test_other_quest_props_and_other_arenas_keep_their_quest_filter(self):
        self.runtime.content = replace(self.runtime.content, map_id="pw006_heredur_01_boss")
        self.assertIsNone(QuestService._quest_prop_operation(self.viewer, self.chest))
        self.runtime.content = replace(self.runtime.content, map_id=self.map_id)
        unrelated = replace(self.chest, instance_id="unrelated_quest_prop")
        self.assertIsNone(QuestService._quest_prop_operation(self.viewer, unrelated))

    def test_active_reward_task_keeps_its_quest_interaction_operation(self):
        quest = load_quest_catalog(DEFAULT_QUESTS_PATH).quest(self.quest_id)
        reward_task = quest.tasks[-1]
        self.viewer.gameplay_quest_log[self.quest_id] = QuestProgress(
            self.quest_id,
            tokens={task.title + "_fin": 1 for task in quest.tasks[:-1]},
            finished_mask=sum(1 << task.index for task in quest.tasks[:-1]),
        )
        self.assertEqual(
            QuestService._quest_prop_operation(self.viewer, self.chest),
            reward_task.switch_definitions[0].switch_id,
        )

    def click_chest(self):
        payload, bits = encode_prop_interaction(PropInteraction(self.chest.prop_id, ""))
        self.viewer.current_message_bit_length = bits
        self.service._handle_prop_interaction(self.viewer, payload)

    def test_map_entry_replays_chest_props_after_scene_load(self):
        packets = []
        with patch.object(
            CharacterServer, "_send_world_loot_state_batch",
            side_effect=lambda viewer, packet, bits, **kwargs: packets.append((packet, bits)),
        ):
            self.service._publish_parallel_world_boss_reward_chest_props(
                self.viewer, self.runtime,
            )
        decoded = [
            command
            for packet, bits in packets
            for command in decode_server_command_batch(packet, bits)
            if getattr(command.value, "entries", None) is not None
        ]
        self.assertEqual(len(decoded), 1)
        self.assertEqual(
            {entry.template_id for entry in decoded[0].value.entries},
            {"t000_state_chest_loot_04"},
        )
        self.assertTrue(all(
            entry.field_24 == PROP_STATE_ACTIVE
            for entry in decoded[0].value.entries
        ))
        self.assertTrue(all(
            entry.instance_guid == bytes(16)
            for entry in decoded[0].value.entries
        ))

    def test_boss_spawn_replays_chests_after_boss_actor_publish(self):
        calls = []
        transition = SimpleNamespace(
            active=True,
            event=SimpleNamespace(key="boss_spawn"),
            enabled_sets=(159,),
            disabled_sets=(),
            announce=True,
        )
        with patch.object(
            self.service, "_map_event_sessions", return_value=(self.viewer,),
        ), patch.object(
            self.service, "_publish_map_exits",
            side_effect=lambda *args, **kwargs: calls.append("exits"),
        ), patch.object(
            self.service, "_broadcast_map_event_update",
            side_effect=lambda *args, **kwargs: calls.append("event-update"),
        ), patch.object(
            self.service, "_publish_event_props",
            side_effect=lambda *args, **kwargs: calls.append("event-props"),
        ), patch.object(
            self.service, "_spawn_event_monsters",
            side_effect=lambda *args, **kwargs: calls.append("boss-actors"),
        ), patch.object(
            self.service, "_spawn_event_npcs",
            side_effect=lambda *args, **kwargs: calls.append("npcs"),
        ), patch.object(
            self.service, "_publish_entrance_bosses",
            side_effect=lambda *args, **kwargs: calls.append("entrance-bosses"),
        ), patch.object(
            self.service, "_publish_parallel_world_boss_reward_chest_props",
            side_effect=lambda *args, **kwargs: calls.append("reward-chests"),
        ):
            self.service._apply_map_event_transition(self.runtime, transition)

        self.assertLess(calls.index("boss-actors"), calls.index("reward-chests"))
        self.assertEqual(calls.count("reward-chests"), 1)

    def test_early_click_preserves_chest_then_boss_death_allows_one_open(self):
        packets = []
        with patch.object(CharacterServer, "_accept_gameplay_state_command"), patch.object(
            CharacterServer, "_send_world_loot_state_batch",
            side_effect=lambda viewer, packet, bits, **kwargs: packets.append((packet, bits)),
        ), patch.object(CharacterServer, "_quest_on_prop_interacted"), patch.object(
            CharacterServer, "_achievement_on_prop_interacted",
        ):
            self.click_chest()
            self.assertFalse(self.runtime.individual_prop_usage)
            self.service._spawn_map_chest_loot.assert_not_called()
            self.service._fire_map_signal.assert_not_called()

            # The locked reply must include the chest after the same quest
            # filter that previously removed it from every network snapshot.
            command, = decode_server_command_batch(*packets[-1])
            self.assertEqual(command.value.entries[0].prop_id, self.chest.prop_id)
            self.assertEqual(command.value.entries[0].field_24, PROP_STATE_ACTIVE)

            packets_before_boss_death = len(packets)
            self.service._unlock_parallel_world_boss_reward_chest(
                self.runtime, ("boss_grimmag",),
            )
            self.assertIn(
                "pw003_spawn_chest", self.runtime.boss_reward_chests_unlocked,
            )
            # The chest is already visible from the entry snapshot; boss
            # death only unlocks its one-shot interaction.
            self.assertEqual(len(packets), packets_before_boss_death)
            self.click_chest()
            self.assertIn((123, self.chest.prop_id), self.runtime.individual_prop_usage)
            self.service._spawn_map_chest_loot.assert_called_once_with(self.viewer, self.chest)
            self.assertEqual(
                MapEventService._map_event_prop_info(self.viewer).entries[0].field_24,
                PROP_STATE_USED,
            )
            self.click_chest()
            self.service._spawn_map_chest_loot.assert_called_once()


class ParallelWorldRewardChestArenaTests(unittest.TestCase):
    """Every parallel-world boss arena uses the same reward-chest contract."""

    path = Path(__file__).resolve().parents[1] / "data/db/static.db4"

    def _runtime(self, map_id):
        return MapEventRuntime(
            load_map_event_content(self.path, map_id),
            load_map_signal_catalog(self.path),
            instance_key=(map_id, "chest-test"),
            initial_flags=active_local_map_event_flags(map_id),
        )

    def _viewer(self, runtime, difficulty):
        return SimpleNamespace(
            flow_lock=Lock(), gameplay_map=runtime.map_id,
            gameplay_map_difficulty=difficulty,
            gameplay_map_event_runtime=runtime,
            gameplay_quest_log={}, gameplay_quest_props_visible=set(),
            gameplay_client_command_state={}, selected_character_id=123,
            send_reliable_ordered=Mock(),
        )

    def test_every_arena_publishes_one_chest_for_the_selected_tier(self):
        for map_id, (boss_template, event_set) in (
            _PARALLEL_WORLD_BOSS_REWARD_CHESTS.items()
        ):
            with self.subTest(map_id=map_id):
                runtime = self._runtime(map_id)
                self.assertIn(event_set, active_local_map_event_flags(map_id))
                self.assertIn(
                    boss_template,
                    {row.template_id for row in runtime.content.spawn_rows},
                )
                chests = [
                    state.definition
                    for state in runtime.published_props()
                    if state.definition.instance_id.casefold()
                    in {"pw_run_lootchest", "pw_run_lootchest_gold"}
                ]
                self.assertTrue(chests)
                # The authored, quest-controlled entities are replaced by
                # zero-GUID template props at one arena transform.
                self.assertTrue(all(
                    chest.instance_guid == bytes(16) for chest in chests
                ))
                self.assertTrue(
                    {5, 7, 9}.issubset({
                        chest.variation_difficulty for chest in chests
                    })
                )
                self.assertEqual(len({chest.position for chest in chests}), 1)
                authored_position = chests[0].position

                for difficulty in (5, 7, 9):
                    viewer = self._viewer(runtime, difficulty)
                    entries = [
                        entry
                        for entry in MapEventService._map_event_prop_info(
                            viewer
                        ).entries
                        if entry.template_id in {
                            "t000_state_chest_loot_04",
                            "t000_state_chest_loot_05",
                        }
                    ]
                    self.assertEqual(len(entries), 1)
                    expected_template = (
                        "t000_state_chest_loot_05"
                        if difficulty == 9
                        else "t000_state_chest_loot_04"
                    )
                    self.assertEqual(entries[0].template_id, expected_template)
                    self.assertEqual(entries[0].position, authored_position)
                    self.assertEqual(entries[0].field_48, "")

    def test_every_arena_boss_unlocks_its_chest_event(self):
        service = MapEventService()
        for map_id, (boss_template, event_set) in (
            _PARALLEL_WORLD_BOSS_REWARD_CHESTS.items()
        ):
            with self.subTest(map_id=map_id):
                runtime = self._runtime(map_id)
                service._unlock_parallel_world_boss_reward_chest(
                    runtime, (boss_template,),
                )
                self.assertIn(event_set, runtime.boss_reward_chests_unlocked)


if __name__ == "__main__":
    unittest.main()
