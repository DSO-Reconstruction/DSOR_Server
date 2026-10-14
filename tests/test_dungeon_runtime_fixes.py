"""Difficulty entry, level scaling, elite population, and status delivery."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.gameplay.local_monster_state import _load_monster, load_local_monster_encounter
from dso_server.gameplay.map_difficulty_state import resolve_map_access
from dso_server.gameplay.local_map_state import load_local_spawn_position
from dso_server.gameplay.local_combat_state import DamageResolution, VictimStatusEffectDefinition
from dso_server.protocol.gameplay_protocol import NewMonsterServerCommand, StatusEffectEntry
from dso_server.services.character_server import CharacterServer
from dso_server.tests.test_pvp_matchmaking import _session


def stat(monster, kind):
    return next(s.value for s in monster.stats if s.kind == kind)


def test_levelled_template_does_not_multiply_its_base_level_growth_twice():
    old = _load_monster(DEFAULT_STATIC_DB_PATH, 'a0202_normal_g_gen_skeleton_warrior', 99,
                        (0,0,0), monster_level=105, scaling_difficulty=1)
    coefficient = _load_monster(DEFAULT_STATIC_DB_PATH, 'a0202_normal_g_gen_skeleton_warrior_scale', 99,
                                (0,0,0), monster_level=105, scaling_difficulty=1)
    assert .85 < stat(old, 11) / stat(coefficient, 11) < 1.15
    normal = _load_monster(DEFAULT_STATIC_DB_PATH, 'a0202_normal_g_gen_skeleton_warrior', 99,
                          (0,0,0), monster_level=100, scaling_difficulty=0)
    assert 1 < stat(old, 11) / stat(normal, 11) < 1.5
    authored = _load_monster(DEFAULT_STATIC_DB_PATH, 'a0202_normal_g_gen_skeleton_warrior', 99, (0,0,0))
    assert stat(authored, 11) == 20  # Non-scaling tutorial/world stats are retained.


@pytest.mark.parametrize('difficulty', [0,1,3,8])
def test_scaling_dungeon_has_at_least_ten_champions_at_each_tier(difficulty):
    encounter = load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, 'a0203_kingscrypt02',
                 monster_level=100 + 5*difficulty, scaling_difficulty=difficulty)
    monsters = [c.value for c in encounter.spawn_batch.commands if isinstance(c, NewMonsterServerCommand)]
    assert sum(m.rank in {'Rank_champion','Rank_leader','Rank_leader_no_entchantments'} for m in monsters) >= 10
    assert len({m.actor_id for m in monsters}) == len(monsters)


def test_private_dungeon_group_joins_match_difficulty_and_entry():
    server = CharacterServer.__new__(CharacterServer)
    server.static_database_path = DEFAULT_STATIC_DB_PATH
    first, friend, stranger = [_session(52000+i, i+1, str(i)) for i in range(3)]
    group = SimpleNamespace(group_id=15)
    server.social_store = SimpleNamespace(group_for=lambda cid: group if cid in (1,2) else None)
    server.sessions = {s.addr:s for s in (first,friend,stranger)}
    for s in (first,friend,stranger):
        s.gameplay_map = 'a0203_kingscrypt02'
        s.gameplay_map_access_group_id = s.gameplay_map
        s.gameplay_map_difficulty = 1
    access = resolve_map_access(DEFAULT_STATIC_DB_PATH, 'a0203_kingscrypt02_01', player_level=100)
    server._select_dungeon_instance(first, access)
    first.gameplay_initialization_sent = first.gameplay_presence_ready = True
    server._select_dungeon_instance(friend, access)
    assert server._gameplay_instance_key(first) == server._gameplay_instance_key(friend)
    server._select_dungeon_instance(stranger, access)
    assert server._gameplay_instance_key(first) != server._gameplay_instance_key(stranger)
    previous = friend.gameplay_instance_id
    friend.gameplay_map_difficulty, friend.gameplay_scaling_difficulty = 0, 1
    server._select_dungeon_instance(friend, access)
    assert friend.gameplay_instance_id == previous
    friend.gameplay_scaling_difficulty = 0
    friend.gameplay_map_difficulty = 2
    server._select_dungeon_instance(friend, access)
    assert friend.gameplay_instance_id != previous


def test_transfer_to_same_map_sets_explicit_entry(monkeypatch):
    server = CharacterServer.__new__(CharacterServer)
    server.static_database_path = DEFAULT_STATIC_DB_PATH
    server.advertised_host = '127.0.0.1'
    s = _session(52011, 11, 'Traveller')
    s.gameplay_map = 'a0203_kingscrypt02'
    s.gameplay_spawn_position = (999,99,999)
    arms = []
    server.arm_gameplay = lambda *args: arms.append(args)
    monkeypatch.setattr(CharacterServer, '_persist_gameplay_session', lambda *a, **k: None)
    monkeypatch.setattr(CharacterServer, '_quest_leave_map', lambda *a, **k: None)
    assert server._begin_map_transfer(s, 'a0203_kingscrypt02_01', reason='fast-travel')
    assert arms[0][-1] == load_local_spawn_position(DEFAULT_STATIC_DB_PATH, s.gameplay_map)
    assert arms[0][2].map_difficulty == 1


def test_delayed_impacts_do_not_reapply_frost_from_one_cast():
    s = _session(52012, 12, 'Mage')
    effect = VictimStatusEffectDefinition('debuff_cc_frost', 10, 1, 125, (), 'hit', max_stack_size=50,
                                        consume_on_max_stack=True)
    hit = DamageResolution(200,1,100,200,10,False,(0,0,0),100,(),123,
                           victim_status_effects=(effect,), cast_id=42)
    first = CharacterServer._victim_effect_command_transactions(s, (hit,), include_item_set_procs=False)
    second = CharacterServer._victim_effect_command_transactions(s, (replace(hit, server_tick=103),),
                                                               include_item_set_procs=False)
    assert len(first[0]) == 1 and second == ((),)
    third = CharacterServer._victim_effect_command_transactions(s, (replace(hit, cast_id=43),),
                                                              include_item_set_procs=False)
    assert third[0][0].value.entries[0].field_18 == 1


def test_finite_zero_duration_is_repaired_without_changing_permanent_or_removal():
    base = StatusEffectEntry(1,2,100,25,100,0,0,100,1,False,False,False,True)
    assert base.field_10 == 1 and base.field_0c == 101
    assert replace(base, field_0c=0, field_10=0).field_10 == 0
    assert replace(base, has_details=False, field_10=0).field_10 == 0


def test_counted_zero_template_has_explicit_client_expiry(monkeypatch):
    from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
    catalog = load_status_runtime_catalog(DEFAULT_STATIC_DB_PATH)
    identity = next(iter(catalog.zero_duration_effect_ids))
    resource = next(name for name, value in catalog.effect_ids.items() if value == identity)
    definition = VictimStatusEffectDefinition(resource, identity, 1, 125, (0,0,0,0,0), 'hit')
    s = _session(52014, 14, 'Buffed')
    timers = []
    class Timer:
        def __init__(self, delay, callback, args):
            self.delay, self.callback, self.args = delay, callback, args
            self.started = False
        def start(self): self.started = True
        def cancel(self): pass
    def timer(delay, callback, args):
        result = Timer(delay, callback, args); timers.append(result); return result
    s.gameplay_deadline_timer = timer
    monkeypatch.setattr(CharacterServer, '_refresh_player_combat_vitals', lambda *a, **k: None)
    for tick in (100,105):
        CharacterServer._send_player_skill_status_effects(s, (definition,), server_tick=tick, label='test')
    entry = next(e for e, expiry in s.gameplay_runtime_status_effects.values())
    assert entry.field_26 and entry.field_39 and entry.field_10 == 125
    assert len(timers) == 1 and timers[0].started and timers[0].delay == 5
    timers[0].callback(*timers[0].args)
    assert not s.gameplay_runtime_status_effects
    assert s.recorder.messages[-1][2] == 'runtime-counted-status-expired'
