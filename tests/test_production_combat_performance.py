"""Behavior and I/O regressions for combat and appearance hot paths."""

from contextlib import closing
import math
import random
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dso_server.gameplay.local_combat_state import LocalCombatState, _MonsterOccupancy
from dso_server.persistence.static_game_database import StaticGameDatabase
from dso_server.services.player_resources import PlayerResourceService
from dso_server.services.presence_visuals import PresenceVisualService


def bare_combat():
    combat = LocalCombatState.__new__(LocalCombatState)
    combat._status_effect_modifier_programs = {}
    combat._status_effect_runtime_programs = {}
    combat._actor_variable_values = {}
    combat._player_trap_definitions = {}
    return combat


@pytest.mark.parametrize('seed', range(8))
def test_spatial_separation_preserves_ordered_corrections(seed):
    rng = random.Random(seed)
    combat = bare_combat()
    combat.monsters = {1: SimpleNamespace(collision_radius=0.3 + seed * 0.15)}
    positions = [(rng.uniform(-8, 8), 0.0, rng.uniform(-8, 8)) for _ in range(150)]
    # Repeated positions exercise the actor-id tangent fallback; varying cells
    # and clearances exercise corrections that enter a new candidate cell.
    positions.extend([(0.0, 0.0, 0.0)] * 8)
    occupancy = _MonsterOccupancy()
    for position in positions:
        occupancy.append(position)
    for _ in range(50):
        destination = (rng.uniform(-8, 8), 0.0, rng.uniform(-8, 8))
        expected = combat._separate_destination(1, destination, (1.0, 0.0, 1.0), positions)
        actual = combat._separate_destination(1, destination, (1.0, 0.0, 1.0), occupancy)
        assert actual == expected


def test_separation_does_not_compare_distant_packs():
    combat = bare_combat()
    combat.monsters = {1: SimpleNamespace(collision_radius=0.5)}
    occupancy = _MonsterOccupancy()
    original_hypot = math.hypot
    with patch('dso_server.gameplay.local_combat_state.math.hypot', wraps=original_hypot) as distance:
        for index in range(1000):
            point = (index * 4.0, 0.0, 0.0)
            assert combat._separate_destination(1, point, (0.0, 0.0, 0.0), occupancy) == point
            occupancy.append(point)
    assert distance.call_count < 1000


def test_resource_modifiers_use_primed_programs_and_variables_without_sql():
    combat = bare_combat()
    combat._actor_variable_values = {'bonus': 7.0}
    combat._status_effect_runtime_programs = {
        1: (1.0, '', 'CurrSkillResource:$bonus,absolute;CurrSkillResource:$0,relative'),
    }
    definition = SimpleNamespace(effect_id=1, values=(0.25,), modifier_kinds={'currskillresource'})
    with patch('sqlite3.connect', side_effect=AssertionError('combat must not open SQLite')):
        assert combat.player_status_resource_delta((definition,), maximum_resource=100) == 32
        assert combat.player_status_resource_delta((definition,), maximum_resource=200) == 57


def test_unrelated_status_effects_do_not_acquire_player_or_map_locks():
    # A session without any locks suffices: the resource path must exit first.
    PlayerResourceService._apply_player_status_resource(
        SimpleNamespace(), (SimpleNamespace(modifier_kinds={'damage'}),),
    )


def test_visual_modifier_selection_uses_primed_programs_without_sql():
    combat = bare_combat()
    combat._load_player_skill_geometry = lambda _index: object()
    combat._skill_scope_matches = lambda _geometry, scopes: scopes == ('wanted',)
    combat._status_effect_runtime_programs = {
        1: (1.0, '', 'SkillSequence:execute,wanted,alternate'),
        2: (1.0, '', 'SkillSequence:execute,other,alternate'),
    }
    chosen = SimpleNamespace(effect_id=1)
    other = SimpleNamespace(effect_id=2)
    with patch('sqlite3.connect', side_effect=AssertionError('visual lookup must not open SQLite')):
        for _ in range(2):
            assert combat.player_skill_visual_modifier_effects(1, (chosen, other)) == (chosen,)


def test_trap_definitions_cache_by_resolved_resource_and_close_connection(tmp_path):
    path = tmp_path / 'static.sqlite'
    with closing(sqlite3.connect(path)) as database:
        database.execute('CREATE TABLE _Template_Trap (Id TEXT, Graphics TEXT, StatusEffectId TEXT, LifeTime REAL, TripperStatusEffect TEXT, TripRadius REAL)')
        database.execute("INSERT INTO _Template_Trap VALUES ('trap', 'visual', '', 4, '', 2)")
        database.commit()
    combat = bare_combat()
    combat.encounter = SimpleNamespace(static_database_path=path)
    combat._load_player_skill_geometry = lambda index: SimpleNamespace(trap_on_hit='trap' if index < 3 else 'missing')
    combat._load_location_aura_effects = lambda *_args: ()
    combat._load_victim_status_effects = lambda *_args: ()
    connections = []
    original_connect = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    with patch('sqlite3.connect', side_effect=connect):
        first = combat.player_trap_definition(1)
        assert combat.player_trap_definition(2) is first
        assert combat.player_trap_definition(3) is None
        assert combat.player_trap_definition(3) is None
    assert first.lifetime_seconds == 4
    assert len(connections) == 2
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')


def test_static_appearance_cache_reuses_results_and_explicitly_invalidates(tmp_path):
    path = tmp_path / 'static.sqlite'
    with closing(sqlite3.connect(path)) as database:
        database.executescript('''
            CREATE TABLE _Template_Item (Id TEXT, Skin TEXT, EquipmentSkinId TEXT, IconBrush TEXT);
            INSERT INTO _Template_Item VALUES ('item', 'skin', '', 'icon');
            CREATE TABLE _Template_Player (Id TEXT);
            INSERT INTO _Template_Player VALUES ('human_male_mage');
            CREATE TABLE _Template_RemotePlayer (Id TEXT, AnimSet TEXT);
            INSERT INTO _Template_RemotePlayer VALUES ('remote_human_male_mage', 'anim');
        ''')
    content = StaticGameDatabase(path)
    assert content.remote_equipment_skin('item') == 'skin'
    assert content.remote_equipment_icon('item') == 'icon'
    assert content.player_template_row_index(1) == 0
    assert content.remote_player_template_row_index(1) == 0
    assert content.remote_player_animation_set(1) == 'anim'
    with patch.object(content, '_connect', side_effect=AssertionError('warm cache must not open SQLite')):
        assert content.remote_equipment_skin('item') == 'skin'
        assert content.remote_equipment_icon('item') == 'icon'
        assert content.player_template_row_index(1) == 0
        assert content.remote_player_template_row_index(1) == 0
        assert content.remote_player_animation_set(1) == 'anim'
    with closing(sqlite3.connect(path)) as database:
        database.execute("UPDATE _Template_Item SET Skin='new'")
        database.commit()
    content.invalidate_content_cache()
    assert content.remote_equipment_skin('item') == 'new'


def test_equipment_visibility_is_filtered_before_content_queries():
    content = SimpleNamespace(remote_equipment_skin=Mock(side_effect=AssertionError('invisible item queried')))
    server = SimpleNamespace(static_database=content)
    item = SimpleNamespace(item_id=1, template_id='accessory', field_f8='', field_80='')
    inventory = SimpleNamespace(items=[item], equipment_slots={1: (8, 23)})
    assert PresenceVisualService._remote_equipment_visuals(server, inventory) == ()
    inventory.equipment_slots = {1: (7,)}
    assert PresenceVisualService._remote_equipment_visuals(server, inventory, {12: 'hidden'}) == ()
