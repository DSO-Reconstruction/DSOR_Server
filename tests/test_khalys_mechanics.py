"""Khalys (boss_khalys, e_bos_khalys_pw_boss): flood, beam and adds from data.

boss_khalys_base_andermight_flood_activate: Allies aura (6 m) wakes the wave
spawner (94, 0, -4, turned -90 deg), whose add_khalys_base_andermight_flood
lays Boss01FloodDamage OZ:2 at the arena's centre (92, 0, -4).
boss_khalys_beam_aura: one 1.1 m star beam, 20.85 m, from -60 deg at 30 deg/s.
boss_khalys_andermight_creature (DE 6): four adds 16 m around Khalys.
"""

from types import SimpleNamespace

import pytest

from dso_server.gameplay.local_combat_state import VictimStatusEffectDefinition
from dso_server.tests.test_monster_location_auras import _state


def _effect(state, name, seconds, delay=0.0):
    return VictimStatusEffectDefinition(
        resource_id=name, effect_id=state._status_effect_ids[name], chance=1.0,
        duration_ticks=round(seconds * 25), values=(), trigger="",
        delay_ticks=round(delay * 25),
    )


def test_flood_activation_wakes_the_wave_spawner_at_the_arena_centre():
    state = _state()
    state.encounter.map_id = "e_bos_khalys_pw_boss"
    attack = SimpleNamespace(position=(92.04, 0.0, -4.01, 1.0), user_status_effects=(
        _effect(state, "boss_khalys_aura_activate_andermight_wave", 10),))
    ((visuals, position, yaw, index, delay),) = (
        state.monster_activated_location_visuals(attack))
    assert [v.resource_id for v in visuals] == ["Boss01FloodDamage"]
    state.start_player_location_visuals(
        visuals, position=position, yaw=yaw, server_tick=0,
        owner_actor_id=100, source_skill_index=index, now=0.0)
    (flood,) = state.player_location_visuals.values()
    assert flood.position == pytest.approx((92.0, 0.0, -4.0), abs=1e-3)
    assert delay == pytest.approx(0.04)


def test_beam_sweeps_from_minus_sixty_degrees_and_strikes_what_it_touches():
    state = _state()
    attack = SimpleNamespace(server_tick=0, skill_index=1, user_status_effects=(
        _effect(state, "boss_khalys_beam_aura", 4),))
    state._start_monster_beam_auras(100, attack, (0.0, 0.0, 0.0), 0.0, 0.0)
    ahead = (0.0, 0.0, -10.0)
    assert state.resolve_monster_location_auras(ahead, player_actor_id=1, now=0.5) == ()
    hits = state.resolve_monster_location_auras(ahead, player_actor_id=1, now=2.0)
    # Its start (60% of 10000) and the 0.25 s tick it entered on (25%).
    assert [h.damage for h in hits] == [6000, 2500]
    assert state.resolve_monster_location_auras(ahead, player_actor_id=1, now=4.5) == ()


def test_summoning_flood_calls_four_adds_after_six_seconds():
    state = _state()
    attack = SimpleNamespace(user_status_effects=(
        _effect(state, "boss_khalys_andermight_creature", 10, delay=6),))
    state._queue_monster_status_minions(100, attack, (0.0, 0.0, 0.0), 0.0, 0.0)
    state.release_due_monster_summons(now=5.9)
    assert state.pending_monster_summons == []
    state.release_due_monster_summons(now=6.1)
    (request,) = state.pending_monster_summons
    assert request.template_id == "pw015_boss_add_anderworld_creature"
    assert sorted(request.positions) == sorted([
        (0.0, 0.0, 16.0), (0.0, 0.0, -16.0), (16.0, 0.0, 0.0), (-16.0, 0.0, 0.0)])
