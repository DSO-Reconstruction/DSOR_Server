"""Server-side Banner of War aura and debuff behavior."""

import sqlite3

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import StatusEffectEntry


def _effect_id(database, resource_id):
    return database.execute(
        "SELECT rowid - 1 FROM _Template_StatusEffect WHERE Id=?",
        (resource_id,),
    ).fetchone()[0]


def test_battlecry_talent_enables_banner_movement_debuff_for_nearby_monsters():
    state = LocalCombatState(
        load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False,
        ),
        player_actor_id=1,
        player_health=1000,
    )
    with sqlite3.connect(state.encounter.static_database_path) as database:
        index = database.execute(
            "SELECT rowid - 1 FROM _Template_Skill WHERE Id='defiance'"
        ).fetchone()[0]
        talent_sources = tuple(
            StatusEffectEntry(
                effect_id=_effect_id(database, resource_id),
                field_04=index + ordinal,
                field_0c=0,
                field_14=25,
                field_08=0,
                field_18=0,
                field_10=0,
                field_1c=100,
                field_20=1,
                field_25=False,
                field_26=False,
                field_39=True,
                has_details=True,
                values=values,
            )
            for ordinal, (resource_id, values) in enumerate((
                ("skill_defiance_lvl", ()),
                ("skill_battlecry_lvl", (-0.05,)),
            ), start=1)
        )

    visuals = state.player_skill_location_visuals(
        index, modifier_sources=talent_sources,
    )
    movement_aura = next(
        effect for effect in visuals
        if effect.resource_id == "skill_defiance_enemies_movement_aura"
    )
    assert movement_aura.chance == 1.0
    assert abs(movement_aura.values[0] + 0.45) < 1e-8
    receivers = state.player_skill_location_receiver_effects(
        index, modifier_sources=talent_sources,
    )
    movement_debuff = next(
        effect for effect in receivers
        if effect.resource_id == "skill_defiance_debuff_movementspeed"
    )
    assert movement_debuff.chance == 1.0

    actor_id = state.encounter.actor_ids[0]
    state.monster_positions[actor_id] = (1.0, 0.0, 0.0)
    state.start_player_location_visuals(
        (movement_aura,), position=(0.0, 0.0, 0.0), yaw=0.0,
        server_tick=100, owner_actor_id=7,
        receiver_status_effects=receivers, source_skill_index=index,
        now=50.0,
    )
    _resolutions, applications = state.apply_player_location_auras(
        now=50.1, server_tick=102,
        player_positions={7: (0.0, 0.0, 0.0)},
    )

    assert "skill_defiance_debuff_movementspeed" in state.monster_status_effects[
        actor_id
    ]
    assert actor_id in {
        application.target_actor_id for application in applications
    }
    assert abs(state._monster_status_scale(
        actor_id, "Speed", "Movement", now=50.1,
    ) - 0.55) < 1e-8
