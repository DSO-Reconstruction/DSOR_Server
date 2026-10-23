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
