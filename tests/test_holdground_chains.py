"""The three corrected defense chains complete through the real server paths.

User decision 2026-10-27 corrected three authored HoldGround rows
(gameplay/holdground.py CONTENT_CORRECTIONS). Each chain is driven from the
giver's offer through every task -- trigger volumes, talk/deliver/prop/kill
requests, world-loot pickups and the native Defend encounter with all its
waves -- to the turn-in and the follow-up offer, map by map.
"""

from contextlib import closing
from pathlib import Path
import random
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dso_server.gameplay.local_map_state import local_npc_actor_id, map_database_path
from dso_server.gameplay.local_monster_state import _load_map_monster_templates
from dso_server.gameplay.premium_bags import reconcile_premium_bags
from dso_server.gameplay.quest_content import (
    QUEST_STATE_ACCEPTED, QUEST_STATE_CLOSED, QUEST_STATE_FINISHED, QuestProgress,
    active_tasks,
)
from dso_server.protocol.gameplay_protocol import Move
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.player_init_protocol import CurrencyChanged, QuickbarInfo
from dso_server.protocol.quest_protocol import (
    NPC_QUEST_DIALOG, QUEST_OPERATION_ACCEPT, QUEST_OPERATION_TURN_IN, Defend, Deliver,
    QuestCommand, Talk, encode_defend, encode_deliver, encode_quest_command, encode_talk,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.services import quest_holdgrounds
from dso_server.services.character_server import CharacterServer
from dso_server.tests import test_quests as fixtures


def _spawnable(map_id):
    """Monster templates a map places or populates its density areas with."""
    path = Path(DEFAULT_STATIC_DB_PATH)
    with closing(sqlite3.connect(
            f"file:{map_database_path(path, map_id).as_posix()}?mode=ro", uri=True)) as database:
        placed = {str(row[0]).casefold() for row in database.execute(
            "SELECT NpcId FROM _Instance_SpawnPoint") if row[0]}
    return placed | {value.casefold() for value in _load_map_monster_templates(path, map_id)}


class Chain:
    """One character walking a quest chain across map instances."""

    def __init__(self, case, *, level, port):
        self.case, self.server, self.level, self.port = case, case.server, level, port
        self.log, self.s = {}, None
        self.inventory = InventoryInfo()
        reconcile_premium_bags(self.inventory, active=True)
        self.next_item_id = 900
        self.defenses = []

    # -- maps -----------------------------------------------------------
    def enter(self, map_id):
        if self.s is not None and self.s.gameplay_map == map_id:
            return self.s
        old = self.s
        if old is not None:
            self.inventory, self.next_item_id = old.gameplay_inventory, old.gameplay_next_item_id
            self.server.sessions.pop(old.addr, None)
        self.port += 1
        s = self.case._session(map_id, self.port, class_id=2)
        s.gameplay_character.level = self.level
        s.gameplay_quest_log = self.log
        s.gameplay_inventory = self.inventory
        s.gameplay_next_item_id = self.next_item_id
        s.gameplay_currency_state = CurrencyChanged(1000, 1000, (0,) * 3, 0, True)
        s.gameplay_quickbar_info = QuickbarInfo(0, 0)
        s.gameplay_presence_callback = self.server._handle_presence_event
        self.server.sessions[s.addr] = s
        self.server._attach_map_event_runtime(
            s, map_id=map_id, initial_flags=frozenset(self.server._quest_reveal_flags(s, map_id)),
            monster_level=None, scaling_difficulty=0, fresh_instance=True,
        )
        self.server._quest_prepare_map(s)
        self.server._quest_start_active_holdgrounds(s)
        self.server._on_map_entered(s)
        self.s = s
        return s

    def npc(self, template):
        found = [npc for npc in self.server._active_npc_definitions(self.s)
                 if npc.template_id.casefold() == template.casefold()]
        assert found, f"{template} is not active on {self.s.gameplay_map}"
        return found[0]

    def stand_at(self, npc):
        self.s.gameplay_last_move = Move.from_values(
            self.s.gameplay_coordinate_frame.world_to_wire(tuple(npc.position)), 0, 1.0, 1.0, 100, 0)

    # -- quest lifecycle ------------------------------------------------
    def accept(self, quest, map_id):
        self.enter(map_id)
        giver = self.npc(quest.giver)
        interactions, _flags = self.server._quest_npc_interactions(self.s, giver.template_id)
        assert (quest.quest_id, NPC_QUEST_DIALOG) in {(i.source, i.field_08) for i in interactions}
        payload, bits = encode_quest_command(QuestCommand(quest.quest_id, QUEST_OPERATION_ACCEPT))
        self.s.current_message_bit_length = bits
        self.server._handle_quest_command(self.s, payload)
        assert self.log[quest.quest_id].state == QUEST_STATE_ACCEPTED
        # Entering a map again runs the quest's PlayerEnteredMap signals.
        self.s = None
        self.enter(map_id)

    def turn_in(self, quest):
        assert self.log[quest.quest_id].state == QUEST_STATE_FINISHED
        payload, bits = encode_quest_command(QuestCommand(quest.quest_id, QUEST_OPERATION_TURN_IN))
        self.s.current_message_bit_length = bits
        self.server._handle_quest_command(self.s, payload)
        assert self.log[quest.quest_id].state == QUEST_STATE_CLOSED

    def run(self, quest):
        """Perform every task in its authored order until the quest finishes."""
        for _ in range(4 * len(quest.tasks)):
            progress = self.log[quest.quest_id]
            if progress.state != QUEST_STATE_ACCEPTED:
                break
            task = active_tasks(quest, progress)[0]
            (map_id,) = sorted(task.maps)
            self.enter(map_id)
            before = progress.finished_mask
            self.perform(quest, task)
            assert self.log[quest.quest_id].finished_mask != before, (quest.quest_id, task.title)
        assert self.log[quest.quest_id].state == QUEST_STATE_FINISHED

    def perform(self, quest, task):
        kinds = [event.kind for event in task.events]
        if "holdgroundsucceeded" in kinds:
            return self.defend(quest, task)
        if "talk" in kinds:
            ((dialog, actor),) = task.dialogs.items()
            npc = self.npc(actor)
            self.stand_at(npc)
            payload, bits = encode_talk(Talk(local_npc_actor_id(npc), dialog))
            self.s.current_message_bit_length = bits
            return self.server._handle_talk_command(self.s, payload)
        if "trigger" in kinds:
            trigger = next(event.value for event in task.events if event.kind == "trigger")
            return self.server._fire_map_signal(self.s, "PlayerHitTrigger", (trigger,))
        if "deliver" in kinds:
            (barter,) = task.barters
            npc = self.npc(barter.actor)
            self.stand_at(npc)
            payload, bits = encode_deliver(Deliver(local_npc_actor_id(npc), barter.barter_id))
            self.s.current_message_bit_length = bits
            return self.server._on_char_op(self.s, payload)
        if "interact" in kinds:
            ((switch, prop_id),) = task.switches.items()
            runtime = self.s.gameplay_map_event_runtime
            # A Switch names a scene _ID or a spawn point's StateObjectId.
            props = [prop for prop in runtime.content.props
                     if prop_id.casefold() in {prop.instance_id.casefold(),
                                               prop.template_id.casefold()}]
            assert props, f"{prop_id} is not placed on {self.s.gameplay_map}"
            for prop in props:
                self.server._quest_sync_props(self.s)
                self.case._click_prop(self.s, prop, switch)
                if self.log[quest.quest_id].task_finished(task.index):
                    break
            return None
        if "kill" in kinds:
            templates = [event.value for event in task.events if event.kind == "kill"]
            # The fixture's combat state has no ambient population, so check
            # that the authored targets spawn on this map at all.
            assert _spawnable(self.s.gameplay_map) & {value.casefold() for value in templates}, (
                f"{templates} never spawn on {self.s.gameplay_map}")
            for count in range(1, 200):
                self.server._quest_on_monsters_killed(self.s, (SimpleNamespace(
                    template_id=templates[count % len(templates)], position=(0.0, 0.0, 0.0),
                    actor_id=0),))
                for drop in tuple(self.s.gameplay_world_loot.values()):
                    if drop.quest_id == quest.quest_id:
                        assert self.server._pickup_world_item(self.s, drop.item.item_id,
                                                              automatic=False)
                if self.log[quest.quest_id].task_finished(task.index):
                    return None
            raise AssertionError(f"{task.title} never finished")
        if "encounter" in kinds:
            npc = self.npc(next(event.value for event in task.events if event.kind == "encounter"))
            return self.server._quest_on_npc_encounter(self.s, npc.template_id)
        raise AssertionError(f"unsupported task {task.title}: {kinds}")

    def defend(self, quest, task):
        binding = task.holdgrounds[0]
        npc = self.npc(binding.actor)
        interactions, _ = self.server._quest_npc_interactions(self.s, npc.template_id)
        assert binding.holdground_id in {i.source for i in interactions}
        self.stand_at(npc)
        payload, bits = encode_defend(Defend(local_npc_actor_id(npc), binding.holdground_id))
        self.s.current_message_bit_length = bits
        clock = [1000.0]
        combat = self.s.gameplay_combat_state
        with patch.object(quest_holdgrounds.time, "perf_counter", lambda: clock[0]), \
                patch.object(quest_holdgrounds, "_schedule_poll"):
            self.server._handle_defend_command(self.s, payload)
            run = next(iter(self.s.gameplay_map_event_runtime.holdgrounds.values()))
            spawned = []
            while not run.stopped:
                clock[0] = max(clock[0], run.wave_deadline)
                with self.s.gameplay_mutation_lock, run.lock:
                    quest_holdgrounds._advance(self.server, run)
                if run.wave_index + 1 == len(run.plan.waves):
                    with combat.lock:
                        for actor_id in run.owned_actor_ids:
                            combat.monster_health[actor_id] = 0
                    with self.s.gameplay_mutation_lock, run.lock:
                        quest_holdgrounds._advance(self.server, run)
            with combat.lock:
                spawned = [combat.monsters[actor_id].template_id for actor_id in sorted(run.owned_actor_ids)
                           if actor_id in combat.monsters]
        assert run.outcome == "holdgroundsucceeded"
        self.defenses.append((run.plan, spawned))

    def offered(self, quest, map_id):
        self.enter(map_id)
        giver = self.npc(quest.giver)
        interactions, _flags = self.server._quest_npc_interactions(self.s, giver.template_id)
        return (quest.quest_id, NPC_QUEST_DIALOG) in {(i.source, i.field_08) for i in interactions}


@pytest.fixture
def case():
    value = fixtures.QuestServiceTests(methodName="runTest")
    value.setUp()
    for name in ("_start_monster_chase", "_achievement_on_item_collected"):
        patcher = patch.object(CharacterServer, name, lambda *a, **k: None)
        patcher.start()
        value.addCleanup(patcher.stop)
    try:
        yield value
    finally:
        value.doCleanups()


def _walk(chain, steps):
    catalog = chain.server._quest_catalog()
    for index, (quest_id, giver_map) in enumerate(steps):
        quest = catalog.quest(quest_id)
        for required in quest.required:
            chain.log.setdefault(required, QuestProgress(required, QUEST_STATE_CLOSED))
        chain.accept(quest, giver_map)
        chain.run(quest)
        chain.turn_in(quest)
        if index + 1 < len(steps):
            following, following_map = steps[index + 1]
            assert following in quest.followups
            assert chain.offered(catalog.quest(following), following_map)


def test_afterglow_iv_and_v_complete(case):
    chain = Chain(case, level=100, port=59100)
    _walk(chain, (("ce2025-side-quest-15", "ce202504"), ("ce2025-side-quest-16", "ce202504")))
    ((plan, spawned),) = chain.defenses
    corrected = [spawn for spawn in plan.waves[2].spawns if spawn.marker_id == "hg_marker10"]
    assert len(corrected) == 2  # the authored hg_marker10 entry and the corrected one
    assert len(spawned) == sum(len(wave.spawns) for wave in plan.waves)


def test_search_for_the_truth_04_to_08_complete(case):
    chain = Chain(case, level=60, port=59200)
    _walk(chain, (
        ("h0000-history-of-hiraja-04-n", "h0004_temple_dun"),
        ("h0000-history-of-hiraja-05-n", "h0004_temple_dun"),
        ("h0000-history-of-hiraja-06-n", "h0006_valley_dun"),
        ("h0000-history-of-hiraja-07-n", "h0007_treasure_cave_dun"),
        ("h0000-history-of-hiraja-08-n", "h0007_treasure_cave_dun"),
    ))
    assert [plan.config_id for plan, _spawned in chain.defenses] == [
        "h0000_history_of_hiraja_04_holdground_01", "h0000_history_of_hiraja_07_holdground_01"]


def test_crash_landing_01_and_02_complete_with_seeded_guardian_draws(case):
    random.seed(20261027)
    chain = Chain(case, level=60, port=59300)
    _walk(chain, (("h0003-deep-fall-01-n", "h0003_oasis_dun"),
                  ("h0003-deep-fall-02-n", "h0003_oasis_dun")))
    ((plan, spawned),) = chain.defenses
    pool = {template for wave in plan.waves for spawn in wave.spawns for template in spawn.template_pool}
    assert pool and "Guardian" not in spawned
    assert len(spawned) == sum(len(wave.spawns) for wave in plan.waves)
