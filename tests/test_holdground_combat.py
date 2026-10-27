"""Defense wave targeting and real hostile damage use isolated defender HP."""
from dataclasses import replace
from types import SimpleNamespace
import time

import pytest

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.paths import DEFAULT_STATIC_DB_PATH


@pytest.fixture
def combat():
    encounter = load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False)
    state = LocalCombatState(encounter, player_actor_id=1, player_health=1000)
    attacker = next(actor for actor in state.monsters if state.monsters[actor].attack.damage_modifier > 0)
    position = state.monster_positions[attacker]
    target = (position[0] + 0.5, position[1], position[2])
    state.player_summons[900001] = SimpleNamespace(actor_id=900001, position=target)
    state.player_summon_health[900001] = [50, 50]
    state.holdground_targets[attacker] = 900001
    return state, attacker, target


def test_wave_prefers_own_defender_over_a_nearer_player(combat):
    state, attacker, target = combat
    position = state.monster_positions[attacker]
    assignments = state.assign_monster_targets((attacker,), ((1, position, {attacker}),))
    assert assignments == {attacker: (900001, target)}
    assert state.monster_target_actor_ids[attacker] == 900001
    state.player_summon_health[900001][0] = 0
    assert state.assign_monster_targets((attacker,), ((1, position, {attacker}),))[attacker][0] == 1


def test_attacks_damage_defender_once_without_creating_a_player_health_record(combat):
    state, attacker, target = combat
    wire = state.encounter.coordinate_frame.world_to_wire(target)
    now = time.perf_counter()
    attacks = ()
    for delay in (0., 5., 10.):
        attacks = state.maybe_start_monster_attacks(
            wire, server_tick=100, now=now + delay, eligible_actor_ids={attacker},
            target_actor_id=900001,
        )
        if attacks:
            break
    assert attacks
    # One real authored hit with deterministic lethal raw damage.
    attack = attacks[0]
    definition = replace(attack.definition or state.monsters[attacker].attack, damage_modifier=100000.)
    attack = replace(attack, definition=definition)
    state.pending_monster_attacks[attacker] = attack
    state.pending_monster_projectile_attacks.clear()
    result = state.resolve_due_holdground_attacks(now=attack.due_at + 1.)
    assert len(result) == 1 and result[0].target_actor_id == 900001
    assert result[0].killed and state.player_summon_health[900001][0] == 0
    assert state.player_health[1] == [1000, 1000]
    assert 900001 not in state.player_health
    assert not state.resolve_due_holdground_attacks(now=attack.due_at + 2.)


def test_two_wave_groups_keep_their_defenders_separate(combat):
    state, attacker, target = combat
    other = next(actor for actor in state.monsters if actor != attacker)
    other_position = state.monster_positions[other]
    state.player_summons[900002] = SimpleNamespace(actor_id=900002, position=other_position)
    state.player_summon_health[900002] = [70, 70]
    state.holdground_targets[other] = 900002
    assignments = state.assign_monster_targets((attacker, other), ())
    assert assignments == {attacker: (900001, target), other: (900002, other_position)}
