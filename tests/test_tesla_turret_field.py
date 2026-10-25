"""The Tesla Turret draws its field once an enemy is within its AggroRange."""

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_monster_encounter


def _tesla(offset):
    encounter = load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False)
    state = LocalCombatState(encounter, player_actor_id=1, player_health=1000)
    definition = next(state.player_summon_definition(i) for i in range(4000)
                      if getattr(state.player_summon_definition(i), "skill_resource_id", "") == "TurretTesla")
    target = encounter.actor_ids[0]
    position = state.monster_positions[target]
    spawn = state.spawn_player_summon(
        definition, owner_actor_id=0x103C1, position=(position[0] - offset, *position[1:]),
        yaw=0.0, level=100, server_tick=500, player_damage_range=(30.0, 30.0), now=100.0)
    return state, target, spawn.actors[0].attack_ready_at


def test_an_enemy_beyond_its_reach_wakes_the_field_without_a_strike():
    state, target, now = _tesla(8.0)
    (attack,) = state.maybe_start_player_summon_attacks({}, server_tick=540, now=now)
    assert attack.command_id == 0
    assert [e.resource_id for e in attack.user_status_effects] == ["dwarf_tesla_graphics"]
    assert state.pending_player_summon_attacks == {}
    # Started once: later empty pulses send nothing.
    assert state.maybe_start_player_summon_attacks({}, server_tick=600, now=now + 1.0) == ()


def test_an_enemy_inside_its_reach_is_struck():
    state, target, now = _tesla(2.0)
    (attack,) = state.maybe_start_player_summon_attacks({}, server_tick=540, now=now)
    assert [e.resource_id for e in attack.user_status_effects] == ["dwarf_tesla_graphics"]
    assert target in state.pending_player_summon_attacks[attack.attacker_actor_id].target_actor_ids
