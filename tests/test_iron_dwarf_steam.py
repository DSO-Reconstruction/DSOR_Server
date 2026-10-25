"""Iron Dwarf lasts while its steam does, and IronDwarfEnd ends it."""

from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock
import time

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import StatusEffectEntry
from dso_server.services.character_server import CharacterServer
from dso_server.services.player_resources import _resource_bound_effect_ids

TRANSFORM = 5037  # skill_irondwarf_transform (static.db4 row - 1)
OTHER = 4698      # dwarf_tesla_graphics


def _entry(effect_id, instance):
    return StatusEffectEntry(effect_id, instance, 15100, 25, 100, 0, 15000, 100, 1,
                             False, False, False, True, (2.0, 0.25))


def _session():
    later = time.perf_counter() + 600.0
    return SimpleNamespace(
        flow_lock=RLock(), gameplay_runtime_status_effects={
            7: (_entry(TRANSFORM, 7), later), 8: (_entry(OTHER, 8), later)},
        gameplay_projectile_timers={}, gameplay_chase_server_tick=400,
        gameplay_disabled_feature_cache=None, gameplay_combat_state=None,
        gameplay_character=None, gameplay_inventory=None,
        gameplay_presence_callback=None, send_reliable_ordered=Mock())


def test_only_the_transformations_end_with_the_steam():
    assert {TRANSFORM, 6036} <= _resource_bound_effect_ids(str(DEFAULT_STATIC_DB_PATH))
    assert OTHER not in _resource_bound_effect_ids(str(DEFAULT_STATIC_DB_PATH))


def test_spent_steam_removes_the_transformation_from_the_client():
    s = _session()
    assert CharacterServer._end_resource_bound_effects(s)
    assert set(s.gameplay_runtime_status_effects) == {8}
    assert s.send_reliable_ordered.call_args.kwargs["label"] == "player-resource-exhausted"
    assert not CharacterServer._end_resource_bound_effects(s)


def test_iron_dwarf_end_cleanses_the_casters_own_transformation():
    s = _session()
    combat = SimpleNamespace(
        player_skill_cleanse_groups=lambda *_a, **_k: frozenset({"transform"}),
        _status_groups_for_effect=lambda effect: (
            frozenset({"buff", "transform"}) if effect == TRANSFORM else frozenset()),
    )
    s.gameplay_character = SimpleNamespace(character_id=200000001)
    s.gameplay_remote_actor_id = 0x103C1
    s.gameplay_victim_status_effect_instances = {}
    CharacterServer._cleanse_player_skill_debuffs(s, combat, skill_index=1899, modifier_sources=())
    assert set(s.gameplay_runtime_status_effects) == {8}
