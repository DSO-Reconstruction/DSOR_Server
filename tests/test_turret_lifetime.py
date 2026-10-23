from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_monster_encounter


def _definition(state, skill_resource_id):
    for index in range(4000):
        definition = state.player_summon_definition(index)
        if definition is not None and definition.skill_resource_id == skill_resource_id:
            return definition
    raise AssertionError(skill_resource_id)


def test_summon_visualizer_bounds_turret_lifetime():
    encounter = load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False,
    )
    state = LocalCombatState(encounter, player_actor_id=1, player_health=1000)
    # Machine Gun: visualizer and despawn aura 20 s, template LifeTime 60.
    assert _definition(state, "TurretMachineGun").lifetime_seconds == 20.0
    assert _definition(state, "TurretCombat").lifetime_seconds == 10.0
    # No visualizer: the template LifeTime still bounds it (45 s > 30 s aura).
    assert _definition(state, "pw_arachna_base_summonmeteor").lifetime_seconds == 45.0


def test_combat_turret_bullet_in_flight_lands_after_expiry():
    encounter = load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False,
    )
    state = LocalCombatState(encounter, player_actor_id=1, player_health=1000)
    target = encounter.actor_ids[0]
    position = state.monster_positions[target]
    spawn = state.spawn_player_summon(
        _definition(state, "TurretCombat"), owner_actor_id=0x103C1,
        position=(position[0] - 9.0, *position[1:]), yaw=0.0, level=100,
        server_tick=500, player_damage_range=(30.0, 30.0), now=100.0,
    )
    turret = spawn.actors[0].actor_id
    now = spawn.actors[0].attack_ready_at
    (attack,) = state.maybe_start_player_summon_attacks({}, server_tick=530, now=now)
    health = state.monster_health[target]

    state.expire_player_summon(turret, land_fired_bullets=True)  # lifetime ends mid-flight
    damage, _ = state.resolve_due_player_summon_attacks(now=attack.due_at)

    assert [hit.target_actor_id for hit in damage] == [target]
    assert state.monster_health[target] == health - damage[0].damage
    assert state.orphaned_player_summon_attacks == {}
