"""A monster skill's one-shot damaging user aura lands a second blow.

heredur_base_doublestrike hits at its HitFrame (26) and its UserStatusEffects
boss_heredur_aura_360smash (AuraRadius 5, AuraTargets Enemies, DE:0.76, $0
-0.5 -> boss_heredur_effect_360smash CurrHealthPointsDmg:$0,relative,Physical)
strikes every enemy around Heredur 19 frames later, half the skill's damage
basis: the double strike's second camera shake (frame 44).
"""

from dataclasses import replace

import pytest

from dso_server.gameplay.local_combat_state import (
    SERVER_TICKS_PER_SECOND, LocalCombatState,
)
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.paths import DEFAULT_STATIC_DB_PATH


def _heredur():
    encounter = load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH, "a0204_boss", include_spawn_areas=False,
        active_event_flags=frozenset({"BOSS_spawn"}),
    )
    state = LocalCombatState(encounter, player_actor_id=1, player_health=1_000_000)
    caster = next(
        actor_id for actor_id in encounter.actor_ids
        if any(a.resource_id == "heredur_base_doublestrike"
               for a in state.monsters[actor_id].attacks)
    )
    monster = state.monsters[caster]
    double = next(a for a in monster.attacks if a.resource_id == "heredur_base_doublestrike")
    state.monsters[caster] = replace(monster, attack=double, attacks=(double,))
    return encounter, state, caster


def test_heredur_double_strike_aura_is_loaded_as_a_strike():
    _encounter, state, caster = _heredur()
    strikes = state._monster_aura_strikes_for(state.monsters[caster].attack)
    strike = strikes["boss_heredur_aura_360smash"]
    assert strike.radius == pytest.approx(5.0)
    assert strike.delay_ticks == 19
    assert strike.damage_programs == ((pytest.approx(0.5), "Physical"),)


def test_heredur_double_strike_hits_twice_and_the_second_blow_is_an_area():
    encounter, state, caster = _heredur()
    position = state.monster_positions[caster]
    near = encounter.coordinate_frame.world_to_wire(
        (position[0] + 1.0, position[1], position[2])
    )
    now = 1000.0
    state.last_monster_attack_at[caster] = 0.0
    attacks = state.maybe_start_monster_attacks(
        near, server_tick=5000, now=now, target_actor_id=1,
        eligible_actor_ids={caster},
    )
    assert [a.definition.resource_id for a in attacks] == ["heredur_base_doublestrike"]
    first = attacks[0]
    (strike,) = state.pending_monster_projectile_attacks[caster]
    assert strike.aura_strike is not None
    assert strike.due_at == pytest.approx(now + (26 + 19) / SERVER_TICKS_PER_SECOND)
    assert strike.impact_tick == 5000 + 26 + 19

    hits = state.resolve_due_monster_attacks(
        near, now=first.due_at + 0.001, player_actor_id=1, finalize_area_attacks=True,
    )
    assert len(hits) == 1
    # The second blow is not due yet.
    assert state.pending_monster_projectile_attacks[caster] == [strike]

    # A second player standing 10 m away is outside the 5 m aura.
    far = encounter.coordinate_frame.world_to_wire(
        (position[0] + 10.0, position[1], position[2])
    )
    assert state.resolve_due_monster_attacks(
        far, now=strike.due_at + 0.001, player_actor_id=2,
    ) == ()
    second = state.resolve_due_monster_attacks(
        near, now=strike.due_at + 0.001, player_actor_id=1, finalize_area_attacks=True,
    )
    assert len(second) == 1
    assert second[0].skill_index == first.skill_index
    assert second[0].server_tick == strike.impact_tick
    assert second[0].victim_status_effects == ()
    monster = state.monsters[caster]
    basis = (monster.minimum_damage + monster.maximum_damage) / 2
    assert second[0].damage == max(1, round(basis * 0.5))
    assert not state.pending_monster_projectile_attacks
    # Landed once: nothing more on a later tick.
    assert state.resolve_due_monster_attacks(
        near, now=strike.due_at + 1.0, player_actor_id=1,
    ) == ()


def test_dead_caster_drops_its_pending_aura_strike():
    encounter, state, caster = _heredur()
    position = state.monster_positions[caster]
    near = encounter.coordinate_frame.world_to_wire(
        (position[0] + 1.0, position[1], position[2])
    )
    state.last_monster_attack_at[caster] = 0.0
    state.maybe_start_monster_attacks(
        near, server_tick=5000, now=1000.0, target_actor_id=1,
        eligible_actor_ids={caster},
    )
    (strike,) = state.pending_monster_projectile_attacks[caster]
    state.monster_health[caster] = 0
    assert state.resolve_due_monster_attacks(
        near, now=strike.due_at + 0.001, player_actor_id=1,
    ) == ()
