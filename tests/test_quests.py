"""Quest content, codecs, and service flow.

The retail tutorial run of 2026-09-09 (docs/QUEST_PROTOCOL.md) is the
reference: its bytes are compared literally where the capture had them, and
the accept / encounter / turn-in sequence is replayed through the service.
"""

from __future__ import annotations

import json
import random
import struct
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_map_state import (
    load_local_npc_definitions,
    load_map_coordinate_frame,
)
from dso_server.gameplay.local_monster_state import load_local_player_only_encounter
from dso_server.gameplay.map_event_state import (
    PropRuntimeState,
    SignalContext,
    load_map_event_content,
    load_map_signal_catalog,
)
from dso_server.protocol.prop_protocol import (
    DISCARD_PROP_COMMAND,
    PROP_STATE_ACTIVE,
    PropInteraction,
    decode_discard_prop_batch,
    encode_prop_interaction,
)
from dso_server.gameplay.quest_content import (
    QUEST_STATE_ACCEPTED,
    QUEST_STATE_CLOSED,
    QUEST_STATE_FINISHED,
    QuestProgress,
    active_tasks,
    apply_quest_event,
    load_quest_catalog,
    task_counters,
)
from dso_server.paths import DEFAULT_QUESTS_PATH, DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import (
    NPC_INFO_COMMAND,
    PROP_INFO_COMMAND,
    Move,
    decode_npc_batch,
    decode_prop_info_batch,
)
from dso_server.protocol.player_init_protocol import (
    CHAPTER_PROGRESS_COMMAND,
    QUEST_LOG_INFO_COMMAND,
    ServerCommand,
)
from dso_server.protocol.quest_protocol import (
    CHAPTER_UNLOCKED_COMMAND,
    NPC_QUEST_DIALOG,
    NPC_TALK_DIALOG,
    NPC_TASK_TARGET,
    QUEST_COMMAND,
    QUEST_MONSTER_INDICATION_INFO_COMMAND,
    QUEST_OPERATION_ACCEPT,
    QUEST_OPERATION_TURN_IN,
    QUEST_SUBJECT_QUEST_STATE,
    QUEST_SUBJECT_TASK_FINISHED,
    QUEST_SUBJECT_TASK_PROGRESS,
    QUEST_UPDATE_COMMAND,
    QuestCommand,
    Talk,
    QuestMonsterIndication,
    QuestMonsterIndicationInfo,
    QuestUpdate,
    decode_quest_batch,
    decode_quest_command,
    decode_talk,
    encode_quest_batch,
    encode_quest_command,
    encode_talk,
)
from dso_server.runtime.transport import Session
from dso_server.services.character_server import CharacterServer
from dso_server.services.map_events import _prop_info_entry
from dso_server.services.quests import QuestService
from dso_server.tests.test_pvp_matchmaking import _FakeSocket, _Recorder, _quiet_server

TUTORIAL = "a0001_start_tutorial_dun"
GARDEN = "a0003_start_garden_dun"
TUTORIAL_01 = "a0001-start-tutorial-01-r"
GRIMMAGSTONE_02 = "a0002-WayToGrimmagstone-02-n"
ROOTS = "a0003_gen_thing_roots_q_WayToGrimmagstone_02"
BAG_SWITCH = "a0002-WayToGrimmagstone-02-n_a0002-WayToGrimmagstone-02-collect-02"
BOOKSTAND_SWITCH = "a0002-WayToGrimmagstone-02-n_a0002-WayToGrimmagstone-02-collect-03"
TALK_DIALOG = "a0002-WayToGrimmagstone-02-n.s_a0002-WayToGrimmagstone-02-talkDialog"

# Retail bytes from capture_20260909_000000_sample_10.log (actor 65557)
RETAIL_ACCEPT = bytes.fromhex(
    "858b00020100000002000000000200000000190061303030312d73746172742d7475746f72"
    "69616c2d30312d72061300303030302d30302d30302030303a30303a303015000100ff"
)
RETAIL_TASK_DONE = bytes.fromhex(
    "858b0001030000000100000000030100000004010000000100000000190061303030312d73"
    "746172742d7475746f7269616c2d30312d7215000100ff"
)
RETAIL_QUEST_COMMAND = bytes.fromhex(
    "8b8a00190061303030312d73746172742d7475746f7269616c2d30312d720000"
)
RETAIL_TALK = bytes.fromhex(
    "8b670003000100440061303030322d576179546f4772696d6d616773746f6e652d30322d6e"
    "2e735f61303030322d576179546f4772696d6d616773746f6e652d30322d74616c6b4469616c6f67"
)


class QuestCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_quest_catalog(DEFAULT_QUESTS_PATH)

    def test_catalog_shape(self):
        self.assertEqual(len(self.catalog.quests), 1768)
        quest = self.catalog.quest(TUTORIAL_01)
        self.assertEqual(quest.giver, "a0001_start_starter")
        self.assertEqual(quest.goal, "a0001_start_ranger")
        self.assertEqual(quest.class_name, "ranger")
        self.assertEqual([t.task_type for t in quest.tasks], ["CompleteTask"])
        # Retail's ChapterProgress total for act_00_chapter_10 was 7.
        self.assertEqual(len(self.catalog.chapter_quests("act_00_chapter_10", "ranger")), 7)
        self.assertEqual(
            self.catalog.switch_props["a0003_waytogrimmagstone_02_bag_01"], BAG_SWITCH
        )

    def test_availability(self):
        quest = self.catalog.quest(TUTORIAL_01)
        ok = dict(class_name="ranger", level=1, closed_quests=(), known_quests=())
        self.assertTrue(self.catalog.available(quest, **ok))
        self.assertFalse(self.catalog.available(quest, **{**ok, "class_name": "dwarf"}))
        self.assertFalse(self.catalog.available(quest, **{**ok, "level": 40}))
        self.assertFalse(self.catalog.available(quest, **{**ok, "known_quests": (TUTORIAL_01,)}))
        follow = self.catalog.quest("a0001-start-tutorial-02-r")
        self.assertFalse(self.catalog.available(follow, **ok))
        self.assertTrue(self.catalog.available(follow, **{**ok, "closed_quests": (TUTORIAL_01,)}))
        self.assertTrue(self.catalog.quest("bloodshed-challenge-01-n").auto_start)
        self.assertFalse(quest.auto_start)

    def test_engine_replays_the_garden_quest(self):
        quest = self.catalog.quest(GRIMMAGSTONE_02)
        progress = QuestProgress(quest.quest_id)
        # The three collect tasks have no preTasks and ran interleaved in the
        # retail capture; the talk task waits for all of them.
        self.assertEqual([t.index for t in active_tasks(quest, progress)], [0, 1, 2])
        self.assertEqual(quest.tasks[3].pre_tasks, tuple(t.title for t in quest.tasks[:3]))
        result = apply_quest_event(quest, progress, "kill", ROOTS, count=8, rng=random.Random(1))
        self.assertEqual(result.changes, ())
        self.assertEqual(len(result.drops), 8)
        self.assertEqual(progress.finished_mask, 0)
        result = apply_quest_event(quest, progress, "collect", result.drops[0].item_id, count=8)
        self.assertEqual([(c.task.index, c.progress, c.target, c.finished) for c in result.changes], [(0, 8, 8, True)])
        self.assertEqual(progress.finished_mask, 0x1)
        for _ in range(5):
            result = apply_quest_event(quest, progress, "interact", BAG_SWITCH)
        self.assertEqual([(c.task.index, c.progress, c.finished) for c in result.changes], [(1, 5, True)])
        apply_quest_event(quest, progress, "interact", BOOKSTAND_SWITCH)
        self.assertEqual(progress.finished_mask, 0x7)
        # Retail's mid-quest QuestLogInfo: mask 0x7, counters 8 and 5.
        self.assertEqual(task_counters(quest, progress)[:3], (8, 5, 1))
        apply_quest_event(quest, progress, "talk", TALK_DIALOG)
        self.assertEqual(progress.state, QUEST_STATE_ACCEPTED)
        result = apply_quest_event(quest, progress, "encounter", quest.goal)
        self.assertTrue(result.quest_finished)
        self.assertEqual(progress.state, QUEST_STATE_FINISHED)
        self.assertEqual(progress.finished_mask, 0x1f)
        # A finished quest ignores further events.
        self.assertEqual(apply_quest_event(quest, progress, "kill", ROOTS).changes, ())

    def test_arena_quests_count_battles_and_victories(self):
        # Both task events accept the plain and the _default id; a match feeds
        # its one battleground id, so each match counts once.
        quest = self.catalog.quest("pvp-a0200-victory-01-n")
        progress = QuestProgress(quest.quest_id)
        result = apply_quest_event(quest, progress, "victory", "BestOf5_5v5_01_default")
        self.assertEqual(result.unsupported, ())
        self.assertEqual([(c.task.index, c.progress, c.target) for c in result.changes], [(0, 1, 15)])
        self.assertEqual(apply_quest_event(quest, progress, "battle", "BestOf5_5v5_01_default").changes, ())
        self.assertEqual(apply_quest_event(quest, progress, "victory", "BestOf3_1v1_01_default").changes, ())
        result = apply_quest_event(quest, progress, "victory", "BestOf5_5v5_01_default", count=14)
        self.assertEqual([(c.task.index, c.finished) for c in result.changes], [(0, True)])

        quest = self.catalog.quest("pvp-f4100-participation-01-n")
        progress = QuestProgress(quest.quest_id)
        result = apply_quest_event(quest, progress, "battle", "AssaultTheFortress_5v5_01", count=6)
        self.assertEqual(result.unsupported, ())
        self.assertEqual([(c.task.index, c.progress, c.finished) for c in result.changes], [(0, 6, True)])

    def test_progress_document_round_trip(self):
        progress = QuestProgress("q", QUEST_STATE_FINISHED, {"a": 2}, 0x3, "2026-09-26 13:04:32")
        restored = QuestProgress.from_document("q", progress.to_document())
        self.assertEqual(restored, progress)


class QuestCodecTests(unittest.TestCase):
    def test_quest_update_matches_retail_bytes(self):
        accept = ServerCommand(
            QUEST_UPDATE_COMMAND,
            QuestUpdate.quest_state(TUTORIAL_01, QUEST_STATE_ACCEPTED, "0000-00-00 00:00:00"),
            65557,
        )
        self.assertEqual(encode_quest_batch((accept,))[0], RETAIL_ACCEPT)
        done = ServerCommand(
            QUEST_UPDATE_COMMAND,
            QuestUpdate.task_progress(TUTORIAL_01, 0, 1, 1, finished=True),
            65557,
        )
        self.assertEqual(encode_quest_batch((done,))[0], RETAIL_TASK_DONE)
        decoded = decode_quest_batch(RETAIL_ACCEPT, len(RETAIL_ACCEPT) * 8)
        self.assertEqual(decoded[0].value.subject, QUEST_SUBJECT_QUEST_STATE)
        self.assertEqual(decoded[0].actor_id, 65557)

    def test_client_commands_decode_retail_bytes(self):
        self.assertEqual(
            decode_quest_command(RETAIL_QUEST_COMMAND),
            QuestCommand(TUTORIAL_01, QUEST_OPERATION_ACCEPT, 0),
        )
        self.assertEqual(
            encode_quest_command(QuestCommand(TUTORIAL_01, QUEST_OPERATION_ACCEPT, 0))[0],
            RETAIL_QUEST_COMMAND,
        )
        talk = decode_talk(RETAIL_TALK)
        self.assertEqual((talk.npc_actor_id, talk.dialog_id), (65539, TALK_DIALOG))

    def test_indication_round_trip(self):
        info = QuestMonsterIndicationInfo(
            2455, GRIMMAGSTONE_02, 0,
            (QuestMonsterIndication(65545, ROOTS, (-15.25, 9.0, 16.28)),),
        )
        data, bits = encode_quest_batch((
            ServerCommand(QUEST_MONSTER_INDICATION_INFO_COMMAND, info, 65574),
        ))
        decoded = decode_quest_batch(data, bits)[0]
        self.assertEqual(decoded.value.quest_id, GRIMMAGSTONE_02)
        self.assertEqual(decoded.value.monsters[0].actor_id, 65545)
        self.assertAlmostEqual(decoded.value.monsters[0].position[0], -15.25, places=4)


class QuestServiceTests(unittest.TestCase):
    def setUp(self):
        self.server = _quiet_server(self)
        self.server.sessions = {}
        self.server.gameplay_world_lock = threading.RLock()
        for name in ("_accept_gameplay_state_command", "_request_account_save"):
            patcher = patch.object(CharacterServer, name, lambda *a, **k: 0)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.xp_awards = []
        patcher = patch.object(
            CharacterServer, "_award_experience",
            lambda s, amount, **kwargs: self.xp_awards.append(int(amount)) or amount,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session = self._session(TUTORIAL, 50101, class_id=2)
        self.server.sessions[self.session.addr] = self.session
        self.runtime = self.server._attach_map_event_runtime(
            self.session, map_id=TUTORIAL, initial_flags=frozenset(),
            monster_level=None, scaling_difficulty=0, fresh_instance=True,
        )

    def test_wael_client_only_reveal_registers_talk_actor_once(self):
        session = self._session("h0006_valley_dun", 50177, class_id=2)
        session.gameplay_character.level = 55
        quest_id = "h0001-hunt-for-the-lantern-04-n"
        session.gameplay_quest_log[quest_id] = QuestProgress(quest_id)
        session.gameplay_last_move = Move.from_values(
            session.gameplay_coordinate_frame.world_to_wire(
                (-10.0, 0.0, -149.0),
            ),
            0, 1.0, 1.0, 100, 0,
        )
        self.server._attach_map_event_runtime(
            session, map_id=session.gameplay_map, initial_flags=frozenset(),
            monster_level=None, scaling_difficulty=0, fresh_instance=True,
        )
        resolved = self.server._resolve_npc_interaction(session, 0)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.template_id, "h0006_quest_trader_wael_01")
        self.server._quest_spawn_revealed_npcs(session)
        commands = [
            command
            for data, bits, label in session.recorder.messages
            if label.startswith("quest-revealed-npc")
            and "actor-vicinity" not in label
            for command in decode_npc_batch(data, bits).commands
        ]
        wael = next(
            command for command in commands
            if type(command).__name__ == "NewNPC"
            and command.template_id == "h0006_quest_trader_wael_01"
        )
        info = next(
            command for command in commands
            if type(command).__name__ == "NPCInfo"
            and command.actor_id == wael.actor_id
        )
        self.assertTrue(any(
            interaction.field_08 == NPC_TALK_DIALOG
            and interaction.source == quest_id + ".s_h0001-hunt-for-the-lantern-04-talk-01Dialog"
            for interaction in info.interactions
        ))
        mark = len(session.recorder.messages)
        self.server._quest_spawn_revealed_npcs(session)
        self.assertEqual(len(session.recorder.messages), mark)

    def _session(self, map_id, port, *, class_id):
        session = Session(("127.0.0.1", port), _FakeSocket())
        session.connected = True
        session.gameplay_mode = True
        session.gameplay_map = map_id
        session.gameplay_map_access_group_id = map_id
        session.gameplay_initialization_sent = True
        session.gameplay_presence_ready = True
        session.gameplay_character = SimpleNamespace(
            character_id=port, name="P", level=1, class_id=class_id,
        )
        session.gameplay_remote_actor_id = 0x10000 + 5
        frame = load_map_coordinate_frame(DEFAULT_STATIC_DB_PATH, map_id)
        encounter = load_local_player_only_encounter(DEFAULT_STATIC_DB_PATH, map_id)
        session.gameplay_coordinate_frame = frame
        session.gameplay_monster_encounter = encounter
        session.gameplay_combat_state = LocalCombatState(
            encounter, player_actor_id=session.gameplay_remote_actor_id,
            player_health=1000, initially_active=False,
        )
        session.gameplay_last_move = Move.from_values(
            frame.world_to_wire((0.0, 0.0, 0.0)), 0, 1.0, 1.0, 100, 0,
        )
        session.gameplay_quest_log = {}
        session.gameplay_quest_tracking = None
        recorder = _Recorder()
        session.send_reliable_ordered = recorder
        session.send_reliable_ordered_split = (
            lambda payload, **kwargs: recorder(payload, label=kwargs.get("label_prefix", "split"))
        )
        session.recorder = recorder
        return session

    def _quest_commands(self, since=0):
        out = []
        for data, bits, _label in self.session.recorder.messages[since:]:
            if data[0] != 0x85 or int.from_bytes(data[1:3], "little") not in (
                QUEST_UPDATE_COMMAND, CHAPTER_PROGRESS_COMMAND, CHAPTER_UNLOCKED_COMMAND,
                QUEST_LOG_INFO_COMMAND, QUEST_MONSTER_INDICATION_INFO_COMMAND,
            ):
                continue
            out.extend(decode_quest_batch(data, bits))
        return out

    def _updates(self, since=0):
        return [c.value for c in self._quest_commands(since) if c.command_id == QUEST_UPDATE_COMMAND]

    def test_offer_accept_encounter_turn_in(self):
        server, s = self.server, self.session
        # The starter offers the ranger tutorial only (class filter).
        interactions, flags = server._quest_npc_interactions(s, "a0001_start_starter")
        self.assertEqual(
            [(i.source, i.field_08, i.field_18) for i in interactions],
            [(TUTORIAL_01, NPC_QUEST_DIALOG, 6)],
        )
        self.assertEqual(flags, ())
        # Accept through the wire command.
        payload, _bits = encode_quest_command(QuestCommand(TUTORIAL_01, QUEST_OPERATION_ACCEPT))
        server._handle_quest_command(s, payload)
        updates = self._updates()
        self.assertEqual(
            (updates[0].subject, dict(updates[0].ints), dict(updates[0].strs)),
            (QUEST_SUBJECT_QUEST_STATE, {2: QUEST_STATE_ACCEPTED},
             {0: TUTORIAL_01, 6: "0000-00-00 00:00:00"}),
        )
        commands = self._quest_commands()
        chapter = next(c.value for c in commands if c.command_id == CHAPTER_PROGRESS_COMMAND)
        self.assertEqual(
            [(e.chapter_id, e.field_08, e.field_0a) for e in chapter.active],
            [("act_00_chapter_10", 7, 0)],
        )
        self.assertEqual([(e.chapter_id, e.field_08, e.field_0a) for e in chapter.completed], [("act_00", 1, 0)])
        # Retail did not announce the tutorial chapter (its only
        # ChapterUnlocked was act_10_chapter_10), and the client has no
        # act_00 chapter artwork.
        self.assertFalse(any(c.command_id == CHAPTER_UNLOCKED_COMMAND for c in commands))
        # NPCInfo refresh went out for the map's live quest NPCs: retail
        # (16:04:10.562) pushed the three live NPCs and not the spellweaver,
        # whose event set has not opened yet.  Retail's accept reply is one
        # batch: QuestUpdate, NPCInfo per live NPC, ChapterProgress.  Our
        # ids skip 0x10001, the slot the client keeps for the local player.
        accept_batch = self._quest_commands()
        self.assertEqual(
            [c.command_id for c in accept_batch],
            [QUEST_UPDATE_COMMAND] + [NPC_INFO_COMMAND] * 3 + [CHAPTER_PROGRESS_COMMAND],
        )
        pushed = {
            c.value.actor_id: [(i.source, i.field_08) for i in c.value.interactions]
            for c in accept_batch if c.command_id == NPC_INFO_COMMAND
        }
        self.assertEqual(set(pushed), {65536, 65538, 65539})
        self.assertNotIn(0x10001, pushed)
        self.assertEqual(pushed[65538], [(TUTORIAL_01, NPC_QUEST_DIALOG), (TUTORIAL_01, NPC_TASK_TARGET)])
        self.assertEqual(server._quest_npc_interactions(s, "a0001_start_starter"), ((), ()))
        # Retail (16:04:10.562): the ranger now carries the quest dialog (0)
        # and the open encounter task's target marker (1), which is what
        # makes the client meet him when the player walks up.
        interactions, _flags = server._quest_npc_interactions(s, "a0001_start_ranger")
        self.assertEqual(
            [(i.source, i.field_08, i.field_18) for i in interactions],
            [(TUTORIAL_01, NPC_QUEST_DIALOG, 6), (TUTORIAL_01, NPC_TASK_TARGET, 6)],
        )
        # Meeting the goal NPC completes the CompleteTask and finishes the quest.
        mark = len(s.recorder.messages)
        server._quest_on_npc_encounter(s, "a0001_start_ranger")
        updates = self._updates(mark)
        self.assertEqual(updates[0].subject, QUEST_SUBJECT_TASK_FINISHED)
        self.assertEqual(dict(updates[0].ints), {1: 0, 3: 1, 4: 1})
        self.assertEqual((updates[1].subject, dict(updates[1].ints)), (QUEST_SUBJECT_QUEST_STATE, {2: QUEST_STATE_FINISHED}))
        # Retail (16:04:33.381): the marker is gone, the dialog stays for the turn-in.
        interactions, _flags = server._quest_npc_interactions(s, "a0001_start_ranger")
        self.assertEqual([(i.source, i.field_08) for i in interactions], [(TUTORIAL_01, NPC_QUEST_DIALOG)])
        # Turn in: closed with a timestamp, XP paid, follow-up now offered by the ranger.
        mark = len(s.recorder.messages)
        payload, _bits = encode_quest_command(QuestCommand(TUTORIAL_01, QUEST_OPERATION_TURN_IN))
        server._handle_quest_command(s, payload)
        updates = self._updates(mark)
        self.assertEqual(dict(updates[0].ints), {2: QUEST_STATE_CLOSED})
        self.assertRegex(dict(updates[0].strs)[6], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(self.xp_awards, [20])
        interactions, _flags = server._quest_npc_interactions(s, "a0001_start_ranger")
        self.assertEqual([(i.source, i.field_08) for i in interactions], [("a0001-start-tutorial-02-r", NPC_QUEST_DIALOG)])
        # The log snapshot has the closed quest with its completion time.
        log = server._quest_log_info(s)
        self.assertEqual(len(log.quests), 1)
        self.assertEqual((log.quests[0].state, log.quests[0].field_10), (QUEST_STATE_CLOSED, 0x1))
        self.assertNotEqual(log.quests[0].tracking_id, "0000-00-00 00:00:00")

    def test_turn_in_refused_before_tasks_are_done(self):
        server, s = self.server, self.session
        server._quest_accept(s, TUTORIAL_01, offered=True)
        self.assertFalse(server._quest_turn_in(s, TUTORIAL_01))
        self.assertEqual(s.gameplay_quest_log[TUTORIAL_01].state, QUEST_STATE_ACCEPTED)

    def test_collect_and_interact_progress_with_dialog_offer(self):
        server, s = self.server, self.session
        from dso_server.protocol.inventory_codec import InventoryInfo
        s.gameplay_inventory = InventoryInfo()
        s.gameplay_presence_callback = lambda session, event: (
            server._quest_on_item_collected(session, event[1], event[2], quest_id=event[3])
            if isinstance(event, tuple) and event[0] == "item_collected" else None)
        s.gameplay_quest_log[GRIMMAGSTONE_02] = QuestProgress(GRIMMAGSTONE_02)
        server._quest_on_monsters_killed(s, tuple(SimpleNamespace(template_id=ROOTS) for _ in range(3)))
        self.assertEqual(self._updates(), [])
        self.assertEqual(len(s.gameplay_world_loot), 3)
        for item_id in tuple(s.gameplay_world_loot):
            self.assertTrue(server._pickup_world_item(s, item_id, automatic=False))
        updates = self._updates()
        self.assertEqual([(u.subject, dict(u.ints)[3]) for u in updates],
                         [(QUEST_SUBJECT_TASK_PROGRESS, n) for n in (1, 2, 3)])
        server._quest_on_monsters_killed(s, tuple(SimpleNamespace(template_id=ROOTS) for _ in range(5)))
        for item_id in tuple(s.gameplay_world_loot):
            self.assertTrue(server._pickup_world_item(s, item_id, automatic=False))
        self.assertEqual(self._updates()[-1].subject, QUEST_SUBJECT_TASK_FINISHED)
        mark = len(s.recorder.messages)
        for _ in range(5):
            server._quest_on_prop_interacted(s, "a0003_WayToGrimmagstone_02_bag_01", "")
        updates = self._updates(mark)
        self.assertEqual(updates[-2].subject, QUEST_SUBJECT_TASK_FINISHED)
        self.assertEqual(updates[-1].subject, 3)  # the empty interaction marker
        server._quest_on_prop_interacted(s, "", BOOKSTAND_SWITCH)
        interactions, flags = server._quest_npc_interactions(s, "a0002_start_hub_smith_01")
        # The talk target's quest marker, then the dialog that opens the talk.
        self.assertEqual([(i.source, i.field_08) for i in interactions],
                         [(GRIMMAGSTONE_02, NPC_TASK_TARGET), (TALK_DIALOG, NPC_TALK_DIALOG)])
        self.assertEqual([(f.name, f.enabled) for f in flags], [(TALK_DIALOG, False)])
        server._quest_feed(s, "talk", TALK_DIALOG)
        self.assertEqual(s.gameplay_quest_log[GRIMMAGSTONE_02].finished_mask, 0xF)

    def test_quest_log_persists_through_account_state(self):
        server, s = self.server, self.session
        server._quest_accept(s, TUTORIAL_01, offered=True)
        server._quest_on_npc_encounter(s, "a0001_start_ranger")
        document = QuestService._quest_log_document(s)
        # The account document keeps the log verbatim under "quest_log".
        self.assertEqual(set(document), {TUTORIAL_01})
        self.assertEqual(document[TUTORIAL_01]["state"], QUEST_STATE_FINISHED)
        restored = SimpleNamespace(quest_log=document)
        other = self._session(TUTORIAL, 50102, class_id=2)
        QuestService._restore_quest_state(other, restored)
        self.assertEqual(other.gameplay_quest_log[TUTORIAL_01].state, QUEST_STATE_FINISHED)
        self.assertEqual(other.gameplay_quest_log[TUTORIAL_01].finished_mask, 0x1)

    def test_signal_rules_read_quest_state(self):
        server, s = self.server, self.session
        catalog = load_map_signal_catalog(DEFAULT_STATIC_DB_PATH)
        rule = catalog.rule("a0001_enter_map_quest_signal_ranger")
        self.assertEqual(rule.quest_not_accepted, (TUTORIAL_01.casefold(),))
        base = dict(map_id=TUTORIAL, running_events=frozenset(), player_level=1,
                    class_name="ranger", player_count=1)
        self.assertTrue(rule.accepts(**base, **_facts(server, s)))
        server._quest_accept(s, TUTORIAL_01, offered=True)
        self.assertFalse(rule.accepts(**base, **_facts(server, s)))
        # A rule that needs the quest solved fires only after the turn-in.
        solved = next(r for r in catalog.rules_by_signal_id.values() if r.quest_solved)
        self.assertFalse(solved.accepts(**{**base, "map_id": "any"}, **_facts(server, s)) and not solved.maps)

    def test_quest_props_and_triggers_are_published(self):
        content = load_map_event_content(DEFAULT_STATIC_DB_PATH, GARDEN)
        bags = [p for p in content.props if p.instance_id == "a0003_WayToGrimmagstone_02_bag_01"]
        self.assertTrue(bags)
        self.assertTrue(all(p.network_prop and p.quest_switch_id == BAG_SWITCH for p in bags))
        entry = _prop_info_entry(SimpleNamespace(definition=bags[0], state=1))
        self.assertEqual((entry.field_48, entry.field_5c), (BAG_SWITCH, 6))
        tutorial = load_map_event_content(DEFAULT_STATIC_DB_PATH, TUTORIAL)
        self.assertTrue(any(v.triggered_event_id for v in tutorial.trigger_volumes))

    def _prop_infos(self, session, since=0):
        return [
            entry
            for data, bits, _label in session.recorder.messages[since:]
            if int.from_bytes(data[1:3], "little") == PROP_INFO_COMMAND
            for entry in decode_prop_info_batch(data, bits)[0].entries
        ]

    def _discards(self, session, since=0):
        return [
            prop_id
            for data, bits, _label in session.recorder.messages[since:]
            if int.from_bytes(data[1:3], "little") == DISCARD_PROP_COMMAND
            for prop_id, _actor in decode_discard_prop_batch(data, bits)
        ]

    def test_quest_props_follow_the_players_quest_state(self):
        server = self.server
        s = self._session(GARDEN, 50103, class_id=2)
        server.sessions[s.addr] = s
        runtime = server._attach_map_event_runtime(
            s, map_id=GARDEN, initial_flags=frozenset(),
            monster_level=None, scaling_difficulty=0, fresh_instance=True,
        )
        bag_ids = {
            p.prop_id for p in runtime.content.props
            if p.instance_id == "a0003_WayToGrimmagstone_02_bag_01"
        }
        stand_ids = {
            p.prop_id for p in runtime.content.props
            if p.instance_id == "a0003_WayToGrimmagstone_02_bookstand_01"
        }
        self.assertTrue(bag_ids and stand_ids)
        # Without the quest, the map init carries none of its props.
        info = CharacterServer._map_event_prop_info(s)
        self.assertFalse({e.prop_id for e in info.entries} & (bag_ids | stand_ids))
        # Accepting it (log entry, as the hub NPC would create) reveals them
        # with the switch id as operation and quest indication 6.
        s.gameplay_quest_log[GRIMMAGSTONE_02] = QuestProgress(GRIMMAGSTONE_02)
        server._quest_sync_props(s)
        revealed = self._prop_infos(s)
        self.assertEqual({e.prop_id for e in revealed}, bag_ids | stand_ids)
        self.assertEqual(
            {e.field_48 for e in revealed if e.prop_id in bag_ids}, {BAG_SWITCH},
        )
        self.assertEqual({e.field_5c for e in revealed}, {6})
        self.assertEqual(s.gameplay_quest_props_visible, bag_ids | stand_ids)
        # A second sync sends nothing new.
        since = len(s.recorder.messages)
        server._quest_sync_props(s)
        self.assertEqual(len(s.recorder.messages), since)
        # Five bag interactions finish the collect task; its bags go away
        # while the bookstand of the open task stays.
        for _ in range(5):
            server._quest_on_prop_interacted(
                s, "a0003_WayToGrimmagstone_02_bag_01", BAG_SWITCH,
            )
        self.assertEqual(set(self._discards(s, since)), bag_ids)
        self.assertEqual(s.gameplay_quest_props_visible, stand_ids)
        later = CharacterServer._map_event_prop_info(s)
        self.assertEqual({e.prop_id for e in later.entries} & (bag_ids | stand_ids), stand_ids)
        # A click on a bag that is no longer live is ignored.
        self.assertIsNone(QuestService._quest_prop_operation(
            s, runtime.content.prop(next(iter(bag_ids))),
        ))

    def _garden_quest_props(self, port=50180):
        session = self._session(GARDEN, port, class_id=2)
        self.server.sessions[session.addr] = session
        runtime = self.server._attach_map_event_runtime(
            session, map_id=GARDEN, initial_flags=frozenset(),
            monster_level=None, scaling_difficulty=0, fresh_instance=True,
        )
        session.gameplay_quest_log[GRIMMAGSTONE_02] = QuestProgress(GRIMMAGSTONE_02)
        bags = sorted((
            prop for prop in runtime.content.props
            if prop.instance_id == "a0003_WayToGrimmagstone_02_bag_01"
        ), key=lambda prop: prop.prop_id)
        task = next(task for task in self.server._quest_catalog().quest(GRIMMAGSTONE_02).tasks
                    if BAG_SWITCH in task.switches)
        self.server._quest_sync_props(session)
        return session, runtime, bags, task

    def _click_prop(self, session, definition, operation=""):
        payload, bits = encode_prop_interaction(PropInteraction(definition.prop_id, operation))
        session.current_message_bit_length = bits
        self.server._handle_prop_interaction(session, payload)

    def test_multi_prop_task_requires_distinct_placements(self):
        session, runtime, bags, task = self._garden_quest_props()
        progress = session.gameplay_quest_log[GRIMMAGSTONE_02]
        self.assertEqual(len(bags), task.target)
        self._click_prop(session, bags[0], BAG_SWITCH)
        self.assertIsNone(CharacterServer._quest_prop_operation(session, bags[0]))
        self.assertEqual(runtime.props[bags[0].prop_id].state, PROP_STATE_ACTIVE)
        self.assertEqual(task.counter(progress.tokens), 1)
        for _ in range(task.target):
            self.server._quest_sync_props(session)
            self._click_prop(session, bags[0], BAG_SWITCH)
        self.assertEqual(task.counter(progress.tokens), 1)
        self.assertFalse(progress.task_finished(task.index))
        self.assertNotIn(bags[0].prop_id, {
            entry.prop_id for entry in CharacterServer._map_event_prop_info(session).entries
        })
        # All five bags have the same label; distinct authored placements
        # must still count independently.
        for bag in bags[1:]:
            self._click_prop(session, bag, BAG_SWITCH)
        self.assertEqual(task.counter(progress.tokens), task.target)
        self.assertTrue(progress.task_finished(task.index))

    def test_consumed_quest_prop_survives_reload_and_fresh_map_instance(self):
        session, _runtime, bags, task = self._garden_quest_props()
        self._click_prop(session, bags[0], BAG_SWITCH)
        document = json.loads(json.dumps(QuestService._quest_log_document(session)))
        restored, runtime, restored_bags, _task = self._garden_quest_props(50181)
        QuestService._restore_quest_state(restored, SimpleNamespace(quest_log=document))
        self.server._quest_sync_props(restored)
        self._click_prop(restored, restored_bags[0], BAG_SWITCH)
        progress = restored.gameplay_quest_log[GRIMMAGSTONE_02]
        self.assertEqual(task.counter(progress.tokens), 1)
        # Rejected clicks cannot consume a fresh runtime's shared prop.
        self.assertEqual(runtime.props[restored_bags[0].prop_id].state, PROP_STATE_ACTIVE)
        self._click_prop(restored, restored_bags[1], BAG_SWITCH)
        self.assertEqual(task.counter(progress.tokens), 2)
        self.assertEqual(QuestProgress.from_document("legacy", {}).used_props, {})

    def test_shared_quest_props_allow_each_player_one_use_without_resync(self):
        session, runtime, bags, task = self._garden_quest_props()
        peer = self._session(GARDEN, 50182, class_id=2)
        self.server.sessions[peer.addr] = peer
        peer.gameplay_map_event_runtime = runtime
        peer.gameplay_quest_log[GRIMMAGSTONE_02] = QuestProgress(GRIMMAGSTONE_02)
        self.server._quest_sync_props(peer)
        peer_mark = len(peer.recorder.messages)
        self._click_prop(session, bags[0], BAG_SWITCH)
        self.assertFalse(bags[0].individual_usage)
        self.assertEqual(runtime.props[bags[0].prop_id].state, PROP_STATE_ACTIVE)
        self.assertFalse(any(
            label == "prop-update" for _data, _bits, label
            in peer.recorder.messages[peer_mark:]
        ))
        # Both players were already on the quest. The second player can use
        # the same placement immediately, without another quest sync first.
        self._click_prop(peer, bags[0], BAG_SWITCH)
        self.assertEqual(task.counter(peer.gameplay_quest_log[GRIMMAGSTONE_02].tokens), 1)
        self.server._quest_sync_props(peer)
        self._click_prop(session, bags[0], BAG_SWITCH)
        self.assertEqual(task.counter(session.gameplay_quest_log[GRIMMAGSTONE_02].tokens), 1)
        self.assertEqual(runtime.props[bags[0].prop_id].state, PROP_STATE_ACTIVE)
        self._click_prop(peer, bags[0], BAG_SWITCH)
        self.assertEqual(task.counter(peer.gameplay_quest_log[GRIMMAGSTONE_02].tokens), 1)

    def test_fresh_quest_attempt_can_reuse_consumed_placement(self):
        session, _runtime, bags, task = self._garden_quest_props()
        self._click_prop(session, bags[0], BAG_SWITCH)
        session.gameplay_quest_log[GRIMMAGSTONE_02] = QuestProgress(GRIMMAGSTONE_02)
        self.server._quest_sync_props(session)
        self._click_prop(session, bags[0], BAG_SWITCH)
        self.assertEqual(task.counter(session.gameplay_quest_log[GRIMMAGSTONE_02].tokens), 1)

    def test_inactive_quest_click_does_not_consume_shared_prop(self):
        session, runtime, bags, task = self._garden_quest_props()
        session.gameplay_quest_log.clear()
        self._click_prop(session, bags[0], BAG_SWITCH)
        self.assertEqual(runtime.props[bags[0].prop_id].state, PROP_STATE_ACTIVE)
        session.gameplay_quest_log[GRIMMAGSTONE_02] = QuestProgress(GRIMMAGSTONE_02)
        self._click_prop(session, bags[0], BAG_SWITCH)
        self.assertEqual(task.counter(session.gameplay_quest_log[GRIMMAGSTONE_02].tokens), 1)

    def test_individual_quest_prop_can_serve_a_later_switch(self):
        map_id = "h0003_oasis_dun"
        quest_id = "h0001-oasis-trader-02-n"
        session = self._session(map_id, 50183, class_id=2)
        self.server.sessions[session.addr] = session
        runtime = self.server._attach_map_event_runtime(
            session, map_id=map_id, initial_flags=frozenset(),
            monster_level=None, scaling_difficulty=0, fresh_instance=True,
        )
        crate = next(prop for prop in runtime.content.props
                     if prop.instance_id == "h0003_oasis_trader_02_goods_crate")
        runtime.publish_props((crate,))
        progress = QuestProgress(quest_id)
        session.gameplay_quest_log[quest_id] = progress
        first_switch = CharacterServer._quest_prop_operation(session, crate)
        self._click_prop(session, crate, first_switch)
        self.assertTrue(progress.task_finished(1))
        self.assertIsNone(CharacterServer._quest_prop_operation(session, crate))
        # Advance the intervening dialogue/collection tasks to the authored
        # second use of this same individually consumed crate.
        progress.finished_mask = (1 << 8) - 1
        progress.tokens["h0003-oasis-trader-02-talk-02_fin"] = 1
        self.server._quest_sync_props(session)
        second_switch = CharacterServer._quest_prop_operation(session, crate)
        self.assertIsNotNone(second_switch)
        self.assertNotEqual(first_switch, second_switch)
        with patch.object(CharacterServer, "_quest_grant_inventory_drops"):
            self._click_prop(session, crate, second_switch)
        self.assertTrue(progress.task_finished(8))

    def test_event_set_npcs_spawn_when_their_event_starts(self):
        # Retail (16:05:23): the spellweaver, goal of the second tutorial
        # quest, arrived as NewNPC + NPCInfo when the "let npc appear" event
        # (a0001_start_dun_undead_mage_03, event set 1) started.
        server, s = self.server, self.session
        definition = self.runtime.catalog.event("a0001_start_dun_undead_mage_03")
        self.assertIsNotNone(definition)
        mark = len(s.recorder.messages)
        server._start_map_local_event(self.runtime, definition)
        spawned = [
            command
            for data, bits, label in s.recorder.messages[mark:]
            if label.startswith("map-event-new-npc")
            for command in decode_npc_batch(data, bits).commands
        ]
        self.assertEqual([type(c).__name__ for c in spawned], ["NewNPC", "NPCInfo"])
        self.assertEqual(spawned[0].template_id, "a0001_start_spellweaver")
        self.assertEqual(spawned[0].actor_id, spawned[1].actor_id)
        self.assertTrue(spawned[0].enabled)
        # Later quest refreshes include the new NPC.
        self.assertIn(
            spawned[0].actor_id,
            {c.actor_id for c in server._quest_npc_info_commands(s)},
        )

    def _prepare_vithrandir_objective(self, *, reveal=True, prerequisites=True, class_id=0):
        self.session.gameplay_character.class_id = class_id
        quest_id = "a0001-start-tutorial-02-" + ("w", "m", "r", "d")[class_id]
        self.session.gameplay_quest_log.clear()
        progress = QuestProgress(quest_id)
        self.session.gameplay_quest_log[quest_id] = progress
        if prerequisites:
            self.server._quest_feed(self.session, "trigger", "a0001explore01")
            self.server._quest_feed(
                self.session, "kill", "a0001_champion_anderworld_creature_openexit",
            )
        if reveal:
            self.server._start_map_local_event(
                self.runtime,
                self.runtime.catalog.event("a0001_start_dun_undead_mage_03"),
            )
        npc = next(d for d in load_local_npc_definitions(DEFAULT_STATIC_DB_PATH, TUTORIAL)
                   if d.template_id == "a0001_start_spellweaver")
        return quest_id, progress, npc

    def test_vithrandir_npc_request_completes_objective_and_allows_turn_in(self):
        quest_id, progress, npc = self._prepare_vithrandir_objective()
        mark = len(self.session.recorder.messages)
        payload = b"\x8b" + struct.pack("<HI", 0x0029, npc.actor_id)
        self.server._on_char_op(self.session, payload)
        self.assertEqual(progress.state, QUEST_STATE_FINISHED)
        updates = self._updates(mark)
        self.assertEqual(updates[0].subject, QUEST_SUBJECT_TASK_FINISHED)
        self.assertEqual(dict(updates[0].ints), {1: 2, 3: 1, 4: 1})
        replies = [decode_npc_batch(data, bits).commands[0]
                   for data, bits, label in self.session.recorder.messages[mark:]
                   if label == "npc-info-response"]
        self.assertEqual(len(replies), 1)
        self.assertEqual([(i.source, i.field_08) for i in replies[0].interactions],
                         [(quest_id, NPC_QUEST_DIALOG)])
        # The paired encounter must not finish or reward the quest twice.
        self.server._on_char_op(
            self.session, b"\x8b" + struct.pack("<HI", 0x0066, npc.actor_id),
        )
        self.assertEqual(self._updates(mark), updates)
        self.assertTrue(self.server._quest_turn_in(self.session, quest_id))
        self.assertFalse(self.server._quest_turn_in(self.session, quest_id))
        self.assertEqual(self.xp_awards, [175])

    def test_encounter_packet_alone_completes_vithrandir_for_each_class(self):
        for class_id in range(4):
            with self.subTest(class_id=class_id):
                _quest_id, progress, npc = self._prepare_vithrandir_objective(class_id=class_id)
                self.server._on_char_op(
                    self.session, b"\x8b" + struct.pack("<HI", 0x0066, npc.actor_id),
                )
                self.assertEqual(progress.state, QUEST_STATE_FINISHED)

    def test_selecting_dialog_also_records_the_npc_encounter(self):
        quest_id, progress, npc = self._prepare_vithrandir_objective()
        payload, bits = encode_talk(Talk(npc.actor_id, quest_id))
        self.session.current_message_bit_length = bits
        self.server._on_char_op(self.session, payload)
        self.assertEqual(progress.state, QUEST_STATE_FINISHED)

    def test_npc_zero_actor_fallback_can_resolve_revealed_vithrandir(self):
        _quest_id, progress, npc = self._prepare_vithrandir_objective()
        frame = self.session.gameplay_coordinate_frame
        self.session.gameplay_last_move = Move.from_values(
            frame.world_to_wire(npc.position), 0, 1.0, 1.0, 100, 0,
        )
        self.server._handle_npc_request(self.session, b"\x8b\x29\x00" + bytes(4))
        self.assertEqual(progress.state, QUEST_STATE_FINISHED)
        data, bits, label = self.session.recorder.messages[-1]
        self.assertEqual(label, "npc-info-response")
        # The reply names the resolved NPC rather than echoing actor 0: an
        # actor-0 NPCInfo made the client drop its interaction list.
        self.assertEqual(decode_npc_batch(data, bits).commands[0].actor_id, npc.actor_id)

    def test_vithrandir_conversation_requires_live_npc_and_prior_tasks(self):
        for reveal, prerequisites in ((False, True), (True, False)):
            with self.subTest(reveal=reveal, prerequisites=prerequisites):
                _quest_id, progress, npc = self._prepare_vithrandir_objective(
                    reveal=reveal, prerequisites=prerequisites,
                )
                for command_id in (0x0029, 0x0066):
                    self.server._on_char_op(
                        self.session, b"\x8b" + struct.pack("<HI", command_id, npc.actor_id),
                    )
                payload, bits = encode_talk(Talk(npc.actor_id, _quest_id))
                self.session.current_message_bit_length = bits
                self.server._on_char_op(self.session, payload)
                self.assertFalse(progress.task_finished(2))
                self.assertEqual(progress.state, QUEST_STATE_ACCEPTED)

    def _enter_start_hub(self, class_id=0):
        s = self.session = self._session("a0002_start_hub", 50103, class_id=class_id)
        self.server.sessions = {s.addr: s}
        self.runtime = self.server._attach_map_event_runtime(
            s, map_id=s.gameplay_map, initial_flags=frozenset(),
            monster_level=None, scaling_difficulty=0, fresh_instance=True,
        )
        quest_id = "a0001-start-tutorial-03-" + ("w", "m", "r", "d")[class_id]
        s.gameplay_quest_log[quest_id] = QuestProgress(quest_id)
        self.server._on_map_entered(s)
        return s, quest_id

    def test_tutorial_three_map_entry_keeps_the_original_vithrandir_for_every_class(self):
        # Retail 16:05:34.550 travels to the hub, then 16:05:40.213 sends
        # NPCRequest/Encounter for actor 65536; task/state updates follow.
        for class_id in range(4):
            with self.subTest(class_id=class_id):
                s, quest_id = self._enter_start_hub(class_id)
                live = self.server._active_npc_definitions(s)
                self.assertEqual({n.template_id for n in live}, {
                    "a0002_start_hub_spellweaver", "a0002_start_hub_smith",
                })
                self.assertNotIn("a0002_start_hub_04", self.runtime.running_events())
                npc = next(n for n in live if n.template_id.endswith("spellweaver"))
                interactions, _ = self.server._quest_npc_interactions(s, npc.template_id)
                self.assertEqual([(i.source, i.field_08) for i in interactions], [
                    (quest_id, NPC_QUEST_DIALOG), (quest_id, NPC_TASK_TARGET),
                ])
                mark = len(s.recorder.messages)
                for command_id in (0x0029, 0x0066):
                    self.server._on_char_op(s, b"\x8b" + struct.pack("<HI", command_id, npc.actor_id))
                self.assertEqual(s.gameplay_quest_log[quest_id].state, QUEST_STATE_FINISHED)
                self.assertEqual([u.subject for u in self._updates(mark)], [
                    QUEST_SUBJECT_TASK_FINISHED, QUEST_SUBJECT_QUEST_STATE,
                ])
                with patch.object(self.server, "_deliver_rewards"):
                    self.assertTrue(self.server._quest_turn_in(s, quest_id))
                offered, _ = self.server._quest_npc_interactions(s, npc.template_id)
                self.assertIn("a0002-WayToGrimmagstone-01-n", {i.source for i in offered})

    def test_garden_return_requires_both_the_quest_and_map_discovery(self):
        s, _ = self._enter_start_hub()
        s.gameplay_quest_log[GRIMMAGSTONE_02] = QuestProgress(GRIMMAGSTONE_02)
        self.server._on_map_entered(s)
        self.assertNotIn("a0002_start_hub_02", self.runtime.running_events())
        self.assertEqual({n.event_set_row for n in self.server._active_npc_definitions(s)}, {11})
        # Retail first visits the garden at 16:05:49, then returns to the
        # smith/Vithrandir _01 variants at 16:08:19.
        s.gameplay_unlocked_maps.add(GARDEN)
        self.server._on_map_entered(s)
        self.assertIn("a0002_start_hub_02", self.runtime.running_events())
        self.assertNotIn("a0002_start_hub_04", self.runtime.running_events())
        self.assertEqual({n.template_id for n in self.server._active_npc_definitions(s)}, {
            "a0002_start_hub_spellweaver_01", "a0002_start_hub_smith_01",
        })
        self.assertEqual({n.event_set_row for n in self.server._active_npc_definitions(s)}, {13})

    def test_later_hub_phase_requires_actual_fortress_discovery(self):
        s, _ = self._enter_start_hub()
        self.assertNotIn("a0002_start_hub_04", self.runtime.running_events())
        s.gameplay_unlocked_maps.add("a0005_start_fortress_dun")
        self.server._on_map_entered(s)
        self.assertIn("a0002_start_hub_04", self.runtime.running_events())
        live = self.server._active_npc_definitions(s)
        self.assertEqual({n.event_set_row for n in live}, {15})

    def test_event_quest_props_stay_hidden_without_the_quest(self):
        # Kingshill's Halloween clue box is switched by k5001-a-haunted-tale;
        # a player who does not have that quest must not receive it.
        content = load_map_event_content(DEFAULT_STATIC_DB_PATH, "a0200_kingscity")
        clue = next(p for p in content.props if p.instance_id == "quest_halloween_2023_clue_01")
        self.assertTrue(clue.network_prop)
        self.assertEqual(clue.quest_switch_id, "k5001-a-haunted-tale_k5001-a-haunted-tale-interact-01")
        s = self.session
        self.assertIsNone(QuestService._quest_prop_operation(s, clue))
        self.assertEqual(QuestService._quest_prop_entries(s, (PropRuntimeState(clue),)), ())
        # The talk task must be finished before the first clue is live.
        progress = QuestProgress("k5001-a-haunted-tale")
        s.gameplay_quest_log["k5001-a-haunted-tale"] = progress
        self.assertIsNone(QuestService._quest_prop_operation(s, clue))
        progress.tokens["k5001-a-haunted-tale-talk-01_fin"] = 1
        progress.finished_mask |= 1
        self.assertEqual(QuestService._quest_prop_operation(s, clue), clue.quest_switch_id)


def _facts(server, s):
    accepted, solved, tasks = server._quest_signal_facts(s)
    return dict(quests_accepted=accepted, quests_solved=solved, tasks_done=tasks)


if __name__ == "__main__":
    unittest.main()
