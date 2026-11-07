"""Fanaticism ends when any skill but Rage Attack starts (OFF:SkillStartExceptAngryStrike)."""

from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock
import sqlite3
import time

from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import StatusEffectEntry
from dso_server.services.character_server import CharacterServer
from dso_server.services.player_resources import _skill_start_released_effects

_EFFECT_IDS = load_status_runtime_catalog(DEFAULT_STATIC_DB_PATH).effect_ids
ATTACK_SPEED = _EFFECT_IDS['skill_angrystrike_buff_frenzy_attackspeed']
MOVE_SPEED = _EFFECT_IDS['skill_angrystrike_buff_frenzy_movementspeed']
OTHER = _EFFECT_IDS['dwarf_tesla_graphics']


def _skill_index(skill_id):
    with sqlite3.connect(
        f'file:{DEFAULT_STATIC_DB_PATH.as_posix()}?mode=ro', uri=True
    ) as database:
        return database.execute(
            'SELECT rowid - 1 FROM _Template_Skill WHERE Id = ?', (skill_id,)
        ).fetchone()[0]


def _entry(effect_id, instance):
    return StatusEffectEntry(effect_id, instance, 15100, 25, 100, 0, 15000, 100, 1,
                             False, False, False, True, (0.1, 0.0))


def _session():
    later = time.perf_counter() + 5.0
    return SimpleNamespace(
        flow_lock=RLock(), gameplay_runtime_status_effects={
            7: (_entry(ATTACK_SPEED, 7), later), 8: (_entry(MOVE_SPEED, 8), later),
            9: (_entry(OTHER, 9), later)},
        gameplay_projectile_timers={}, gameplay_chase_server_tick=400,
        gameplay_disabled_feature_cache=None, gameplay_combat_state=None,
        gameplay_character=None, gameplay_inventory=None,
        gameplay_presence_callback=None, send_reliable_ordered=Mock())


def test_fanaticism_is_kept_only_by_rage_attack():
    released, _skills = _skill_start_released_effects(str(DEFAULT_STATIC_DB_PATH))
    assert released[ATTACK_SPEED] == released[MOVE_SPEED] == frozenset({'angrystrike'})
    assert OTHER not in released


def test_rage_attack_keeps_fanaticism():
    s = _session()
    assert not CharacterServer._end_skill_start_released_effects(s, _skill_index('angrystrike'))
    assert set(s.gameplay_runtime_status_effects) == {7, 8, 9}
    s.send_reliable_ordered.assert_not_called()


def test_smash_ends_fanaticism_on_the_client():
    s = _session()
    assert CharacterServer._end_skill_start_released_effects(s, _skill_index('mightybash'))
    assert set(s.gameplay_runtime_status_effects) == {9}
    assert s.send_reliable_ordered.call_args.kwargs['label'] == 'player-skill-start-off'
    assert not CharacterServer._end_skill_start_released_effects(s, _skill_index('mightybash'))
