"""Every authored quest defense is runnable or a documented content gap.

The wave syntax follows the client's own loader, a defense runs on the map
whose exported scene places its actor, and the world-story fallback only
replaces setups that cannot be built from the current export.
pw208-hide-and-seek-03-n runs end to end on pw208_lost_woods_dun_04, the only
map whose scene holds its trigger, ClearRegion spawn and defense.
"""

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import random
import re
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pytest

from dso_server.gameplay.holdground import (
    CONTENT_CORRECTIONS, MAP_MONSTER_POOL, UnsupportedHoldGround, holdground_actor_ids,
    load_holdground_plan, map_monster_pool, parse_wave,
)
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_map_state import load_local_npc_definitions, local_npc_actor_id
from dso_server.gameplay.local_monster_state import load_local_player_only_encounter
from dso_server.gameplay.quest_content import (
    QUEST_STATE_ACCEPTED, QUEST_STATE_CLOSED, QuestProgress, load_quest_catalog,
)
from dso_server.paths import DEFAULT_QUESTS_PATH, DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import Move
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.quest_protocol import NPC_DEFEND_DIALOG, Defend, encode_defend
from dso_server.services import quest_holdgrounds
from dso_server.services.base import CharacterService
from dso_server.services.character_server import CharacterServer
from dso_server.tests import test_quests as fixtures
from dso_server.tests.test_pvp_matchmaking import _quiet_server, _session as pvp_session


CATALOG = load_quest_catalog(DEFAULT_QUESTS_PATH)
STATIC = SimpleNamespace(static_database_path=DEFAULT_STATIC_DB_PATH)
PW208 = ("pw208-hide-and-seek-03-n", 2)

# Exported maps absent from the server and the current client
# (export_win32/maps and its manifests). Only the retired legacy client
# still ships most of them; the release-261 client cannot load them.
ABSENT_MAPS = {
    ("e-a0401-tow-01-hold-ground-01-n", 0): "default_event",  # placeholder map id
    ("e-a0401-tow-01-hold-ground-01re-n", 0): "default_event",
    ("e-d3101-ess-01-holdground-jullov-01-n", 0): "e_d3101_dun_ess_01",
    ("e-hal-01-vault-challenge-02-n", 0): "e_hal_01_vault_dun",
    ("e-hal-01-vault-challenge-02-n-re", 0): "e_hal_01_vault_dun",
    ("e-time-01-goblin-attack-02-n", 0): "e_time_01_goblin_land_dun",
    ("e-time-01-hagastove-grotto-02-n", 0): "e_time_01_hagastove_grotto_01_dun",
    ("e-tow-02-hold-ground-01-n", 0): "e_a0200_dun_tow_02",
    ("e-tow-02-hold-ground-02-n", 0): "e_a0200_dun_tow_02",
    ("e-tow-02-hold-ground-03-n", 0): "e_a0200_dun_tow_02",
    ("e-tow-02-hold-ground-04-n", 0): "e_a0200_dun_tow_02",
    ("e-tow-02-hold-ground-05re-n", 0): "e_a0200_dun_tow_02",
    ("e-tow-02-hold-ground-05re-n", 1): "e_a0200_dun_tow_02",
    ("e-tow-02-hold-ground-05re-n", 2): "e_a0200_dun_tow_02",
    ("e-tow-02-hold-ground-05re-n", 3): "e_a0200_dun_tow_02",
    ("e-tow-03-resistance-command-center-01-n", 0): "e_tow_princecamp_01",
    ("e-tow-03-resistance-command-center-01-start-n", 0): "e_tow_princecamp_01",
    ("e-tow-03-resistance-command-center-02-n", 0): "e_tow_princecamp_01",
    ("e-tow-03-resistance-command-center-02-start-n", 0): "e_tow_princecamp_01",
    ("e-tow-04-jarlshofn-in-danger-01-n", 0): "e_tow_jarlshofn_01",
    ("e-tow-04-jarlshofn-in-danger-02-n", 1): "e_tow_jarlshofn_01",
    ("e-z0101-new-01-defend-virgin-01-n", 0): "e_z0101_dun",
    ("e-z0101-new-01-defend-virgin-01re-n", 0): "e_z0101_dun",
    ("e-zah-01-challenge-01-n", 0): "e_zah_01_dun_desert_01",
    ("e-zah-01-desert-03-n", 0): "e_zah_01_dun_desert_01",
}
# Authored typos/absent rows on present maps. No marker, template or
# defender of these names exists in any static table or exported map.
CONTENT_GAPS = {
    ("f4201-NotSafeAnymore-03-n", 0): r"'hg_marker07:f4201_notmal_s_gen_mechgunner_HoldGround'.*missing template",
    ("g0005-TempleOfIce-08-n", 0): r"'hg_marker06:g0005_holdground_mummy_melee_01(_scale)?'.*missing marker",
    ("k0001-power-from-within", 0): r"missing defender template 'k0001_dwarf'",
    ("k0002-stand-tall", 0): r"missing defender template 'k0001_dwarf'",
    ("k0003-bring-balance", 0): r"missing defender template 'k0001_prince'",
    ("Test-HoldGround", 0): r"missing defender template 'test_holdground_defender'",
}


def _cell(config_id, wave):
    path = Path(DEFAULT_STATIC_DB_PATH).as_posix()
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as database:
        return database.execute(
            f"SELECT Wave{int(wave)} FROM _Template_HoldGround WHERE HoldGroundId=?", (config_id,),
        ).fetchone()[0]


def _task(quest_id, index):
    quest = CATALOG.quest(quest_id)
    task = next(task for task in quest.tasks if task.index == index)
    return quest, task, task.holdgrounds[0]


def _session(map_id, difficulty=0):
    return SimpleNamespace(gameplay_map=map_id,
                           gameplay_map_event_runtime=SimpleNamespace(scaling_difficulty=difficulty))


def _outcomes(map_id, binding):
    """Resolution of every authored tier/placement of a binding on a map."""
    outcomes = []
    for tier, _config in binding.configurations:
        for actor_id in holdground_actor_ids(DEFAULT_STATIC_DB_PATH, map_id, binding.actor) or (None,):
            try:
                quest_holdgrounds.resolve_holdground(STATIC, _session(map_id, tier or 0), binding,
                                                     npc_actor_id=actor_id)
                outcomes.append((tier, actor_id, None))
            except UnsupportedHoldGround as error:
                outcomes.append((tier, actor_id, str(error)))
    return outcomes


@pytest.mark.parametrize(("config_id", "wave", "pairs", "blank", "detail"), [
    # A token without ':' stays a blank WaveInfo entry.
    ("h0000_history_of_hiraja_07_holdground_01", 5, 5, 1, None),
    # strtok never trims: the leading space is part of the client's atom.
    ("f4201_holdground_NotSafeAnymore-03", 9, 3, 0,
     (-1, (" hg_marker08", "f4201_s_champion_gen_dragon_berserker_HoldGround"))),
    ("e_tow_04_holdground_resistance_gunnar", 4, 7, 0,
     (0, ("hg_marker04", " e_tow_04_jarlshofn_holdground_norsegnome_shaman_champion_01"))),
    # Three ':'-parts (two tokens missing their ';') are blank as well.
    ("e_a0200_tow_02_holdground_arena", 7, 4, 1, None),
    # Runs of separators collapse; they are not entries.
    ("a0008_DefendTheGravedigger_01", 1, 4, 0, None),
    # A whitespace-only token has one part and is blank.
    ("e_ful_02_defendthevillage_01", 1, 6, 1, None),
    # A template without its marker is blank.
    ("e_a0200_tow_02_holdground_captain", 9, 11, 1, None),
])
def test_wave_cells_split_like_the_client_template_loader(config_id, wave, pairs, blank, detail):
    parsed, blanks = parse_wave(_cell(config_id, wave))
    assert (len(parsed), blanks) == (pairs, blank)
    assert all(len(pair) == 2 and all(pair) for pair in parsed)
    if detail is not None:
        index, pair = detail
        assert parsed[index] == pair


def test_malformed_token_spawns_nothing_instead_of_rejecting_the_defense():
    _quest, _value, binding = _task("h0000-history-of-hiraja-07-n", 0)
    placements = holdground_actor_ids(DEFAULT_STATIC_DB_PATH, "h0007_treasure_cave_dun", binding.actor)
    assert len(placements) == 3
    for actor_id in placements:
        plan = load_holdground_plan(DEFAULT_STATIC_DB_PATH, "h0007_treasure_cave_dun",
                                    "h0000_history_of_hiraja_07_holdground_01", binding.actor,
                                    npc_actor_id=actor_id)
        assert [(len(w.spawns), w.blank_entries, w.duration_seconds) for w in plan.waves] == [
            (6, 0, 15.0), (4, 0, 15.0), (5, 0, 12.0), (6, 0, 10.0), (5, 1, 5.0)]
        assert plan.defender_template_id == "h0000_guard_dwarf_ortolax_01"
        assert plan.npc_actor_id == actor_id
        assert {spawn.marker_id for wave in plan.waves for spawn in wave.spawns} == {
            f"hg_marker0{number}" for number in range(1, 7)}


def test_a_duplicate_marker_named_by_a_wave_is_ambiguous():
    with pytest.raises(UnsupportedHoldGround, match="ambiguous defense marker 'hg_marker02'"):
        load_holdground_plan(DEFAULT_STATIC_DB_PATH, "a0008_crypt_dun",
                             "a0008_DefendTheGravedigger_01", "a0008_gravedigger")


def test_pw208_defense_resolves_where_sibyl_and_its_markers_are():
    _quest, task, binding = _task(*PW208)
    assert task.maps == {"pw208_rangervillage_dun"}
    assert holdground_actor_ids(DEFAULT_STATIC_DB_PATH, "pw208_rangervillage_dun", binding.actor) == ()
    with pytest.raises(UnsupportedHoldGround, match="0 matching placements"):
        load_holdground_plan(DEFAULT_STATIC_DB_PATH, "pw208_rangervillage_dun",
                             "pw208_hide_and_seek_03", binding.actor)
    plan = quest_holdgrounds.resolve_holdground(STATIC, _session("pw208_lost_woods_dun_04"), binding)
    assert plan.defender_template_id == "pw208_normal_defender_sibyl_01"
    assert [len(wave.spawns) for wave in plan.waves] == [9] * 9
    assert [wave.duration_seconds for wave in plan.waves] == [15, 15, 15, 10, 10, 10, 5, 5, 5]
    assert {spawn.marker_id for wave in plan.waves for spawn in wave.spawns} == {
        "hg_marker01", "hg_marker02", "hg_marker03"}


@pytest.mark.parametrize(("quest_task", "map_id", "hosted"), [
    (PW208, "pw208_lost_woods_dun_04", True),    # actor + every marker
    (PW208, "pw208_lost_woods_dun_01", False),   # Sibyl, but no markers
    (PW208, "pw208_rangervillage_dun", True),    # declared map
    (PW208, "a0200_kingscity", False),
    # Its chancellor also stands in the city; the declared map is absent.
    (("e-tow-02-hold-ground-03-n", 0), "a0200_kingscity", False),
    # The declared map places the emissary; the hub copy is not a host.
    (("h0000-history-of-hiraja-02-n", 0), "h0001_hub", False),
    (("h0000-history-of-hiraja-02-n", 0), "h0004_temple_dun", True),
])
def test_declared_map_hosts_the_defense_unless_it_lacks_the_actor(quest_task, map_id, hosted):
    _quest, task, binding = _task(*quest_task)
    assert quest_holdgrounds.holdground_hosted_here(STATIC, _session(map_id), task, binding) is hosted


@pytest.mark.parametrize("quest_task", sorted(ABSENT_MAPS))
def test_absent_maps_remain_unrunnable(quest_task):
    _quest, task, binding = _task(*quest_task)
    map_id = ABSENT_MAPS[quest_task]
    assert task.maps == {map_id}
    outcomes = _outcomes(map_id, binding)
    assert outcomes and all(error == f"missing defense content: {map_id}.db4"
                            for _tier, _actor, error in outcomes)


@pytest.mark.parametrize("quest_task", sorted(CONTENT_GAPS))
def test_authored_content_gaps_remain_unrunnable(quest_task):
    _quest, task, binding = _task(*quest_task)
    (map_id,) = task.maps
    outcomes = _outcomes(map_id, binding)
    assert outcomes
    for _tier, _actor, error in outcomes:
        assert error is not None
        assert re.search(CONTENT_GAPS[quest_task], error), error


OASIS_POOL = ("h0003_normal_clawstrider_01", "h0003_normal_cyclops_01",
              "h0003_normal_skeleton_archer_01", "h0003_normal_skeleton_warrior_01")


def test_user_corrections_are_exactly_three_keyed_tokens():
    # User decision 2026-10-27: no general typo tolerance exists.
    assert CONTENT_CORRECTIONS == {
        ("ce2025_holdground_02", "ce202504", "marker", "hg_marker010"): "hg_marker10",
        ("h0000_history_of_hiraja_04_holdground_01", "h0004_temple_dun", "marker",
         "hg_merker10"): "hg_marker10",
        ("h0003_deep_fall_01_holdground_01", "h0003_oasis_dun", "template",
         "Guardian"): MAP_MONSTER_POOL,
    }


def test_corrected_markers_use_the_maps_hg_marker10():
    afterglow = load_holdground_plan(DEFAULT_STATIC_DB_PATH, "ce202504", "ce2025_holdground_02",
                                     "ce_2025_zador_benniss_hold_quest_02")
    truth = load_holdground_plan(DEFAULT_STATIC_DB_PATH, "h0004_temple_dun",
                                 "h0000_history_of_hiraja_04_holdground_01",
                                 "h0000_quest_dwarf_emissary", npc_actor_id=65536)
    count = lambda plan: [sum(s.marker_id == "hg_marker10" for s in w.spawns) for w in plan.waves]
    # Afterglow wave 3: its own hg_marker10 entry plus the corrected one.
    assert count(afterglow) == [1, 2, 2, 2, 1]
    # Search for the Truth: exactly the authored hg_merker10 entries.
    assert count(truth) == [1, 0, 1, 0, 2, 1, 0, 3, 0]
    for plan in (afterglow, truth):
        assert not any(spawn.template_pool for wave in plan.waves for spawn in wave.spawns)


def test_crash_landing_guardian_entries_draw_from_the_oasis_pool():
    assert map_monster_pool(DEFAULT_STATIC_DB_PATH, "h0003_oasis_dun") == OASIS_POOL
    plan = load_holdground_plan(DEFAULT_STATIC_DB_PATH, "h0003_oasis_dun",
                                "h0003_deep_fall_01_holdground_01", "h0003_quest_dwarf_01")
    pooled = [(wave.number, spawn.marker_id, spawn.template_id, spawn.template_pool)
              for wave in plan.waves for spawn in wave.spawns if spawn.template_pool]
    assert pooled == [(number, "hg_marker05", "Guardian", OASIS_POOL) for number in (2, 4, 6, 8, 9)]
    assert all(spawn.template_id.startswith("h0003_normal_") for wave in plan.waves
               for spawn in wave.spawns if not spawn.template_pool)


def test_destroy_worlds_heart_only_lacks_its_normal_difficulty():
    _quest, _task_value, binding = _task("f4100-DestroyWorldsHeart-06-n", 0)
    outcomes = _outcomes("f4202_dun", binding)
    assert [(tier, error is None) for tier, _actor, error in outcomes] == [
        (0, False), (1, True), (2, True), (3, True), (4, True), (5, True), (6, True)]
    assert "'hg_marker02:4202_normal_g_buzzsaw_DestoryWorldsHeart_06'" in outcomes[0][2]


def test_every_other_authored_defense_resolves_on_its_host_map():
    unrunnable = set(ABSENT_MAPS) | set(CONTENT_GAPS)
    tasks = [(quest, task, binding) for quest in CATALOG.quests.values()
             for task in quest.tasks for binding in task.holdgrounds]
    assert len(tasks) == 85 and len(unrunnable) == 31
    runnable = 0
    for quest, task, binding in tasks:
        key = (quest.quest_id, task.index)
        if key in unrunnable:
            continue
        runnable += 1
        hosts = ("pw208_lost_woods_dun_04",) if key == PW208 else tuple(task.maps)
        for map_id in hosts:
            outcomes = _outcomes(map_id, binding)
            tiers = {tier for tier, _actor, error in outcomes if error is None}
            expected = {tier for tier, _config in binding.configurations}
            if key == ("f4100-DestroyWorldsHeart-06-n", 0):
                expected.discard(0)
            assert tiers == expected, (key, map_id, outcomes)
    assert runnable == 54


class DefenseHostAndFallbackTests(unittest.TestCase):
    _session = fixtures.QuestServiceTests._session

    def setUp(self):
        self.server = _quiet_server(self)
        self.server.sessions = {}
        self.server.gameplay_world_lock = threading.RLock()
        self.saved = []

        def save(session, reason, **kwargs):
            self.saved.append((reason, deepcopy(self.server._quest_log_document(session))))

        for name, replacement in (
            ("_accept_gameplay_state_command", lambda *a, **k: 0),
            ("_request_account_save", save),
            ("_start_monster_chase", lambda *a, **k: None),
        ):
            patcher = patch.object(CharacterServer, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _enter(self, map_id, quest_id, *, difficulty=0, finished=(), flags=frozenset(), port=58401):
        session = self._session(map_id, port, class_id=2)
        session.gameplay_character.level = 60
        self.server.sessions[session.addr] = session
        self.server._attach_map_event_runtime(
            session, map_id=map_id, initial_flags=frozenset(flags), monster_level=None,
            scaling_difficulty=difficulty, fresh_instance=True,
        )
        quest = self.server._quest_catalog().quest(quest_id)
        progress = QuestProgress(quest.quest_id, QUEST_STATE_ACCEPTED)
        for task in quest.tasks:
            if task.index in finished:
                progress.finished_mask |= 1 << task.index
                for token in task.returns:
                    progress.tokens[token] = 1
        session.gameplay_quest_log[quest.quest_id] = progress
        for required in quest.required:
            session.gameplay_quest_log[required] = QuestProgress(required, QUEST_STATE_CLOSED)
        return session, quest, progress

    def _defend_offers(self, session, actor):
        interactions, _flags = self.server._quest_npc_interactions(session, actor)
        return [entry.source for entry in interactions if entry.field_08 == NPC_DEFEND_DIALOG]

    def test_pw208_offer_and_request_follow_sibyl_to_lost_woods_04(self):
        _quest, _task_value, binding = _task(*PW208)
        session, quest, progress = self._enter(
            "pw208_lost_woods_dun_04", PW208[0], finished=(0, 1),
            flags={"pw208_hide_and_seek_03_move_npc"},
        )
        self.assertEqual(self._defend_offers(session, binding.actor), [binding.holdground_id])
        npc = next(npc for npc in load_local_npc_definitions(DEFAULT_STATIC_DB_PATH, session.gameplay_map)
                   if npc.template_id == binding.actor)
        x, y, z = npc.position
        session.gameplay_last_move = Move.from_values(
            session.gameplay_coordinate_frame.world_to_wire((x, y, z)), 0, 1.0, 1.0, 100, 0)
        packet, bits = encode_defend(Defend(local_npc_actor_id(npc), binding.holdground_id))
        session.current_message_bit_length = bits
        with patch.object(quest_holdgrounds, "start_holdground") as start:
            self.server._handle_defend_command(session, packet)
        start.assert_called_once_with(self.server, session, quest.quest_id, binding,
                                      npc_actor_id=local_npc_actor_id(npc))
        self.assertEqual((progress.finished_mask, progress.state), (0b11, QUEST_STATE_ACCEPTED))

    def test_pw208_hide_and_seek_03_runs_end_to_end_on_lost_woods_04(self):
        quest = CATALOG.quest(PW208[0])
        explore, clear, defend, complete = quest.tasks
        binding = defend.holdgrounds[0]
        region = {value.casefold() for monsters in clear.clear_regions.values()
                  for value in monsters.split(";")}
        session, _quest_value, _progress = self._enter("pw208_lost_woods_dun_04", PW208[0])
        progress = lambda: session.gameplay_quest_log[quest.quest_id]
        combat = session.gameplay_combat_state

        # Entering runs the quest's own signals: Sibyl moves here and event
        # set 4088 spawns its ClearRegion monsters (never the ordinary pool).
        self.server._on_map_entered(session)
        with combat.lock:
            spawned = {actor_id: monster for actor_id, monster in combat.monsters.items()
                       if monster.template_id.casefold() in region}
        self.assertTrue(spawned)
        sibyl = next(npc for npc in self.server._active_npc_definitions(session)
                     if npc.template_id == binding.actor)

        # pw208expl08 is only in this scene; the trigger feeds the quest here.
        self.server._fire_map_signal(session, "PlayerHitTrigger", ("pw208expl08",))
        self.assertTrue(progress().task_finished(explore.index))

        # The tracked ClearRegion stays current here and lists its monsters.
        session.gameplay_quest_tracking = (quest.quest_id, clear.index)
        with patch.object(CharacterServer, "_quest_send_monster_indication") as indication:
            self.server._quest_send_tracked_clear_region_indication(session)
        indication.assert_called_once_with(session)
        self.server._quest_on_map_entered(session)
        self.assertEqual(session.gameplay_quest_tracking, (quest.quest_id, clear.index))
        self.assertEqual(self._defend_offers(session, binding.actor), [])

        with combat.lock:
            for actor_id in spawned:
                combat.monster_health[actor_id] = 0
        self.server._quest_on_monsters_killed(session, tuple(spawned.values()))
        self.assertTrue(progress().task_finished(clear.index))
        self.assertEqual(self._defend_offers(session, binding.actor), [binding.holdground_id])

        # The native Defend request starts the real encounter beside Sibyl.
        x, y, z = sibyl.position
        session.gameplay_last_move = Move.from_values(
            session.gameplay_coordinate_frame.world_to_wire((x, y, z)), 0, 1.0, 1.0, 100, 0)
        packet, bits = encode_defend(Defend(local_npc_actor_id(sibyl), binding.holdground_id))
        session.current_message_bit_length = bits
        clock = [1000.0]
        with patch.object(quest_holdgrounds.time, "perf_counter", lambda: clock[0]), \
                patch.object(quest_holdgrounds, "_schedule_poll"):
            self.server._handle_defend_command(session, packet)
            run = next(iter(session.gameplay_map_event_runtime.holdgrounds.values()))
            self.assertEqual(run.plan.map_id, "pw208_lost_woods_dun_04")
            while not run.stopped:
                clock[0] = max(clock[0], run.wave_deadline)
                with session.gameplay_mutation_lock, run.lock:
                    quest_holdgrounds._advance(self.server, run)
                if run.wave_index + 1 == len(run.plan.waves):
                    with combat.lock:
                        for actor_id in run.owned_actor_ids:
                            combat.monster_health[actor_id] = 0
                    with session.gameplay_mutation_lock, run.lock:
                        quest_holdgrounds._advance(self.server, run)
        self.assertEqual(run.outcome, "holdgroundsucceeded")
        self.assertEqual(len(run.owned_actor_ids), 81)
        self.assertTrue(progress().task_finished(defend.index))

        self.server._quest_on_npc_encounter(session, binding.actor)
        self.assertTrue(progress().task_finished(complete.index))
        self.assertEqual(progress().finished_mask, 0b1111)

    def test_hide_and_seek_03_region_is_not_credited_on_its_declared_or_other_maps(self):
        quest = CATALOG.quest(PW208[0])
        clear = quest.tasks[1]
        for port, map_id in enumerate(("pw208_rangervillage_dun", "pw208_lost_woods_dun_01",
                                       "pw208_lost_woods_dun_05"), 58430):
            with self.subTest(map_id=map_id):
                session, _quest_value, _progress = self._enter(map_id, PW208[0], finished=(0,),
                                                               port=port)
                self.assertEqual(self.server._quest_clear_region_relocated(session, quest, clear),
                                 False)

    def test_pw208_lookalike_map_without_markers_offers_nothing(self):
        _quest, _task_value, binding = _task(*PW208)
        session, _quest_value, _progress = self._enter(
            "pw208_lost_woods_dun_01", PW208[0], finished=(0, 1))
        self.assertEqual(self._defend_offers(session, binding.actor), [])

    def test_story_fallback_only_replaces_unbuildable_world_defenses(self):
        cases = (
            # (map, quest, difficulty, auto-completed)
            ("g0005_temple_ice_02_dun", "g0005-TempleOfIce-08-n", 0, True),
            ("f4201_pve", "f4201-NotSafeAnymore-03-n", 0, True),
            ("f4202_dun", "f4100-DestroyWorldsHeart-06-n", 0, True),
            ("f4202_dun", "f4100-DestroyWorldsHeart-06-n", 1, False),
            ("c2303_dun", "c2200-RewriteHistory-01-n", 0, False),
            # Local quests never had the story shortcut.
            ("h0003_oasis_dun", "h0003-deep-fall-01-n", 0, False),
        )
        for port, (map_id, quest_id, difficulty, completed) in enumerate(cases, 58410):
            with self.subTest(quest=quest_id, difficulty=difficulty):
                session, quest, progress = self._enter(map_id, quest_id, difficulty=difficulty, port=port)
                task = next(task for task in quest.tasks if task.holdgrounds)
                self.server._quest_start_active_holdgrounds(session)
                progress = session.gameplay_quest_log[quest.quest_id]
                self.assertEqual(progress.task_finished(task.index), completed)
                offers = self._defend_offers(session, task.holdgrounds[0].actor)
                if completed:
                    self.assertEqual(offers, [])
                elif quest.quest_type.casefold() == "world":
                    self.assertEqual(offers, [task.holdgrounds[0].holdground_id])


class _Timer:
    def __init__(self, delay, callback, args=(), kwargs=None):
        self.delay, self.callback, self.args, self.kwargs = delay, callback, args, kwargs or {}

    def start(self):
        pass

    def cancel(self):
        pass


def _arena(monkeypatch, map_id, port):
    s = pvp_session(port, port % 1000, "Defender")
    combat = LocalCombatState(load_local_player_only_encounter(DEFAULT_STATIC_DB_PATH, map_id),
                              player_actor_id=s.gameplay_remote_actor_id, player_health=1000)
    runtime = SimpleNamespace(lock=threading.Lock(), map_id=map_id, scaling_difficulty=0,
                              monster_level=None, scheduler=SimpleNamespace(timer=_Timer),
                              active_flags=lambda: frozenset(),
                              catalog=SimpleNamespace(event=lambda value: None))
    s.gameplay_map = map_id
    s.gameplay_combat_state = combat
    s.gameplay_map_event_runtime = runtime
    s.gameplay_inventory = InventoryInfo()
    s.gameplay_deadline_timer = _Timer
    s.gameplay_chase_server_tick = 200
    s.gameplay_presence_callback = None
    s.send_reliable_ordered = Mock()
    s.send_reliable_ordered_split = Mock()
    s.gameplay_announced_npc_actor_ids = set()
    server = SimpleNamespace(static_database_path=DEFAULT_STATIC_DB_PATH,
                             _map_event_sessions=lambda r: (s,),
                             _announce_runtime_group=Mock(), _quest_feed=Mock(),
                             _quest_refresh_npc_infos=Mock())
    clock = [100.0]
    monkeypatch.setattr(quest_holdgrounds.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(CharacterService, "_start_monster_chase", Mock())
    return SimpleNamespace(server=server, s=s, combat=combat, runtime=runtime, clock=clock)


def test_recovered_defenses_run_and_blank_entries_spawn_nothing(monkeypatch):
    arena = _arena(monkeypatch, "h0007_treasure_cave_dun", 52620)
    quest, _value, binding = _task("h0000-history-of-hiraja-07-n", 0)
    actor_id = holdground_actor_ids(DEFAULT_STATIC_DB_PATH, "h0007_treasure_cave_dun", binding.actor)[0]
    try:
        assert quest_holdgrounds.start_holdground(arena.server, arena.s, quest.quest_id, binding,
                                                  npc_actor_id=actor_id)
        run = next(iter(arena.runtime.holdgrounds.values()))
        counts = [len(run.owned_actor_ids)]
        while run.wave_index + 1 < len(run.plan.waves):
            arena.clock[0] = run.wave_deadline
            quest_holdgrounds.advance_holdgrounds(arena.server, arena.s)
            counts.append(len(run.owned_actor_ids))
        assert counts == [6, 10, 15, 21, 26]  # wave 5: five monsters, one blank entry
        assert len(arena.combat.monsters) == 26
        arena.server._quest_feed.assert_not_called()
    finally:
        quest_holdgrounds.cancel_holdgrounds(arena.server, arena.s)


def test_pw208_defense_starts_on_lost_woods_04(monkeypatch):
    arena = _arena(monkeypatch, "pw208_lost_woods_dun_04", 52621)
    quest, _value, binding = _task(*PW208)
    try:
        assert quest_holdgrounds.start_holdground(arena.server, arena.s, quest.quest_id, binding)
        run = next(iter(arena.runtime.holdgrounds.values()))
        assert run.plan.map_id == "pw208_lost_woods_dun_04"
        assert len(run.owned_actor_ids) == 9
        assert set(arena.combat.holdground_targets.values()) == {run.defender_actor_id}
        assert run.defender_actor_id in arena.combat.player_summons
    finally:
        quest_holdgrounds.cancel_holdgrounds(arena.server, arena.s)
    assert not arena.runtime.holdgrounds


def test_clear_region_relocation_admits_only_hide_and_seek_03_in_the_whole_corpus():
    from dso_server.gameplay.quest_task_hosts import clear_region_relocated_to
    maps = sorted(path.stem for path in
                  (Path(DEFAULT_STATIC_DB_PATH).parent.parent / "maps").glob("*.db4"))
    admitted = [(quest.quest_id, task.index, map_id)
                for quest in CATALOG.quests.values() for task in quest.tasks
                if task.clear_regions and task.maps
                for map_id in maps
                if clear_region_relocated_to(DEFAULT_STATIC_DB_PATH, quest, task, map_id)]
    assert admitted == [(PW208[0], 1, "pw208_lost_woods_dun_04")]


def test_guardian_entries_are_drawn_independently_at_each_wave_start(monkeypatch):
    arena = _arena(monkeypatch, "h0003_oasis_dun", 52622)
    quest, _value, binding = _task("h0003-deep-fall-01-n", 0)
    rng = random.Random(20261027)
    draws = []

    def choice(pool):
        draws.append((None if not hasattr(arena, "run") else arena.run.wave_index, pool))
        return rng.choice(pool)

    monkeypatch.setattr(quest_holdgrounds.random, "choice", choice)
    try:
        assert quest_holdgrounds.start_holdground(arena.server, arena.s, quest.quest_id, binding)
        arena.run = run = next(iter(arena.runtime.holdgrounds.values()))
        while run.wave_index + 1 < len(run.plan.waves):
            arena.clock[0] = run.wave_deadline
            quest_holdgrounds.advance_holdgrounds(arena.server, arena.s)
        # One draw per Guardian entry, when waves 2, 4, 6, 8 and 9 start.
        assert [(index, pool) for index, pool in draws] == [
            (wave - 1, OASIS_POOL) for wave in (2, 4, 6, 8, 9)]
        with arena.combat.lock:
            spawned = [arena.combat.monsters[actor_id].template_id for actor_id in run.owned_actor_ids]
        assert "Guardian" not in spawned
        assert set(spawned) <= set(OASIS_POOL) | {
            spawn.template_id for wave in run.plan.waves for spawn in wave.spawns}
    finally:
        quest_holdgrounds.cancel_holdgrounds(arena.server, arena.s)
