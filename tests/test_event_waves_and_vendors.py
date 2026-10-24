"""Full scripted arena waves and clickable event vendors/crafting stations."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dso_server.gameplay.local_monster_state import load_local_player_only_encounter
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_map_state import load_local_npc_definitions, load_map_coordinate_frame
from dso_server.gameplay.quest_content import QUEST_STATE_CLOSED, QUEST_STATE_FINISHED, QuestProgress
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import (
    PROP_INFO_COMMAND, decode_prop_info_batch, encode_screen_message,
)
from dso_server.services.character_server import CharacterServer
from dso_server.tests import test_map_events as fixtures
from dso_server.tests.test_map_companions import Scheduler


@pytest.fixture
def world():
    case=fixtures.MapEventServiceTests(methodName="runTest"); case.setUp()
    try: yield case
    finally: case.doCleanups()


def enter(case, map_id, flags=frozenset(), level=100, difficulty=0):
    s=case.session
    s.gameplay_map=map_id; s.gameplay_map_access_group_id=map_id
    s.gameplay_character.level=level
    s.gameplay_monster_encounter=load_local_player_only_encounter(DEFAULT_STATIC_DB_PATH,map_id)
    s.gameplay_coordinate_frame=load_map_coordinate_frame(DEFAULT_STATIC_DB_PATH,map_id)
    s.gameplay_combat_state=LocalCombatState(s.gameplay_monster_encounter,
        player_actor_id=s.gameplay_remote_actor_id, player_health=1000, initially_active=False)
    s.gameplay_last_move=None; s.gameplay_spawn_position=(0,0,0)
    r=case.server._attach_map_event_runtime(s,map_id=map_id,initial_flags=flags,
        monster_level=level,scaling_difficulty=difficulty,fresh_instance=True)
    r.scheduler=Scheduler()
    return s,r


@pytest.mark.parametrize("difficulty", range(10))
def test_all_five_random_anniversary_waves_spawn_then_unlock_reward(world, difficulty):
    case=world; s,r=enter(case,"e_anniversary_arena_01", difficulty=difficulty)
    combat=s.gameplay_combat_state
    server=case.server
    with patch.object(CharacterServer,"_start_monster_chase",return_value=False):
        server._fire_map_signal(s,"PlayerInteractedProp",("e_anniversary_monsterwave_random",))
        assert "e_anniversary_arena_random_wave_01" in r.pending
        seen=set()
        for number in range(1,6):
            name=f"e_anniversary_arena_random_wave_{number:02}"
            # Fire the actual scheduled start callback without sleeping.
            if name in r.pending:
                r.pending[name].run()
            assert name in r.running
            assert [
                (data, bits) for data, bits, label in s.recorder.messages
                if label == "map-wave-start"
            ] == [encode_screen_message(f"Wave {i}") for i in range(1, number + 1)]
            alive=tuple(combat.monsters[i] for i in combat.alive_actor_ids)
            assert len(alive) >= 10
            assert not (set(combat.alive_actor_ids) & seen)
            seen.update(combat.alive_actor_ids)
            sets=tuple(r.event_set_actor_ids)
            count=len(combat.monsters)
            server._spawn_event_monsters(r,(s,),sets)
            assert len(combat.monsters) == count
            # One kill is not enough to advance the wave.
            combat.monster_health[alive[0].actor_id]=0
            server._on_monsters_killed(s,(alive[0],))
            target=(f"e_anniversary_arena_random_wave_{number+1:02}" if number<5
                    else "e_anniversary_arena_random_reward")
            assert target not in r.pending and target not in r.running
            for m in alive[1:]: combat.monster_health[m.actor_id]=0
            server._on_monsters_killed(s,alive[1:])
            assert target in r.pending or target in r.running
            if number < 5:
                assert target in r.pending
                assert r.pending[target].seconds >= 5
        if "e_anniversary_arena_random_reward" in r.pending:
            r.pending["e_anniversary_arena_random_reward"].run()
        assert "e_anniversary_arena_random_reward" in r.running
        expected = sorted([
            "e_anniversary_monsterwave_random_reward",
            "e_anniversary_monsterwave_chest_guardian",
            "e_anniversary_monsterwave_chest_champion",
            "e_anniversary_monsterwave_chest_champion",
            "e_anniversary_monsterwave_chest_leader",
        ])
        # Check the exact published chest records, not just a running event
        # or any pre-existing prop whose name happens to contain "reward".
        entries = [entry for data, bits, label in s.recorder.messages
                   if label == "map-event-prop-info"
                   and int.from_bytes(data[1:3], "little") == PROP_INFO_COMMAND
                   for entry in decode_prop_info_batch(data, bits)[0].entries
                   if "_reward" in entry.template_id or "_chest_" in entry.template_id]
        assert sorted(entry.template_id for entry in entries) == expected
        assert len({entry.prop_id & 0xFFFF for entry in entries}) == 5
        assert all(entry.field_24 == 1 for entry in entries)
        assert all(r.props[entry.prop_id].definition.one_shot for entry in entries)
        assert all(r.props[entry.prop_id].definition.loot_list_id for entry in entries)


@pytest.mark.parametrize("level,matrona",[(1,False),(11,False),(12,True),(100,True)])
def test_kingscity_vendor_and_station_registration(world,level,matrona):
    case=world; s,r=enter(case,"a0200_kingscity",frozenset({"anniversary_event"}),level)
    case.server._fire_map_signal(s,"PlayerEnteredMap",("a0200_kingscity",))
    definitions=case.server._active_npc_definitions(s)
    grima=next(d for d in definitions if d.template_id=="e_event_merchant_grima")
    assert case.server._resolve_npc_interaction(s,grima.actor_id).shop_id=="monger_event_grima"
    assert any(d.template_id=="crafting_npc_event" for d in definitions)==matrona
    props=case.server._map_event_prop_info(s)
    cooking=next(p for p in props.entries if p.template_id=="cooking_tool_halloween_2023")
    for _ in range(2):
        station, changed=r.interact(cooking.prop_id)
        assert not changed
        assert station.state == 1
    if level>=12:
        assert any(p.template_id=="workbench_smith_01" for p in props.entries)


def test_grima_remains_event_gated(world):
    case = world
    # The local schedule can have a Grima event active on the day this runs.
    # This case specifically exercises a realm with no scheduled events.
    with patch.object(type(case.server._event_catalog()), "active_events", return_value=()):
        s, _ = enter(case, "a0200_kingscity")
        assert not any(
            d.template_id == "e_event_merchant_grima"
            for d in case.server._active_npc_definitions(s)
        )


def test_an_anniversary_quest_does_not_start_the_season(world):
    # Phestos, the quest's turn-in NPC, stands in the anniversary_event set
    # with the piñatas; the quest must not reveal that set out of season.
    case=world; s,r=enter(case,"a0200_kingscity")
    quest="e-anniversary-introduction-02-n"
    s.gameplay_quest_log={quest: QuestProgress(quest, state=QUEST_STATE_FINISHED)}
    assert "anniversary_event" not in case.server._quest_reveal_flags(s,"a0200_kingscity")
    assert not any(d.template_id=="e_anniversary_merchant_phestos" for d in case.server._active_npc_definitions(s))


def test_event_quests_sleep_out_of_season(world):
    case=world; s,r=enter(case,"a0200_kingscity"); srv=case.server
    open_quest,closed_quest,plain="e-anniversary-introduction-02-n","anni-2024-quest-01","pvp-a0200-participation-01-n"
    # A Bloody Reward: given by its map's signal, tied to no scheduled event.
    map_quest="bloodshed-challenge-01-n"
    s.gameplay_quest_log={open_quest: QuestProgress(open_quest),
        closed_quest: QuestProgress(closed_quest, state=QUEST_STATE_CLOSED), plain: QuestProgress(plain),
        map_quest: QuestProgress(map_quest)}
    s.gameplay_quest_tracking=(open_quest,0)
    srv._quest_park_out_of_season(s)
    # Not tracked: gone from the live log; a closed one stays for prerequisites.
    assert set(s.gameplay_quest_log)=={closed_quest,plain,map_quest} and s.gameplay_quest_tracking is None
    # Not shown, yet saved.
    assert {e.quest_id for e in srv._quest_log_info(s).quests}=={plain,map_quest}
    assert set(srv._quest_log_document(s))=={open_quest,closed_quest,plain,map_quest}
    # The anniversary starts again: back on the next map entry.
    season=SimpleNamespace(active_event_flags=lambda now=None: frozenset({"anniversary_event"}))
    with patch.object(type(srv),"_event_catalog",lambda self: season):
        srv._quest_park_out_of_season(s)
        assert set(s.gameplay_quest_log)=={open_quest,closed_quest,plain,map_quest}
        assert {e.quest_id for e in srv._quest_log_info(s).quests}=={open_quest,closed_quest,plain,map_quest}
