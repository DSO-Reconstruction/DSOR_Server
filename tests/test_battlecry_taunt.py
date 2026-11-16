"""Outburst applies its authored fear debuffs and 15 m / 10 s taunt."""

import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import (
    _DirectNavigationMesh,
    load_local_monster_encounter,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.combat_protocol import SKILL_COMMAND, SkillCast
from dso_server.protocol.gameplay_protocol import StatusEffectEntry
from dso_server.services.character_server import CharacterServer
from dso_server.services import player_combat
from dso_server.services.base import CharacterService
from dso_server.tests.test_combat_entities import MixedEntityCastTests


OWNER = 98305
OTHER_PLAYER = 98306


def _row_id(table, resource_id):
    with sqlite3.connect(
        f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True,
    ) as database:
        return database.execute(
            f"SELECT rowid - 1 FROM {table} WHERE Id=?", (resource_id,),
        ).fetchone()[0]


def _combat():
    encounter = load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=True,
    )
    return LocalCombatState(encounter, player_actor_id=OWNER, player_health=1000)


def _outburst_talent_source():
    talent = _row_id("_Template_StatusEffect", "skill_battlecry_lvl")
    return StatusEffectEntry(
        effect_id=talent, field_04=1, field_0c=0, field_14=25,
        field_08=0, field_18=0, field_10=0, field_1c=100,
        field_20=1, field_25=False, field_26=False, field_39=True,
        has_details=True, values=(0.1,),
    )


def test_outburst_cast_handler_publishes_its_managed_server_taunt_aura():
    fixture = MixedEntityCastTests()
    fixture.setUp()
    try:
        session, combat = fixture.source, fixture.combat
        outburst = _row_id("_Template_Skill", "battlecry")
        session.gameplay_skill_book_ids = {outburst}
        session.gameplay_character.class_id = 1
        session.gameplay_presence_callback = None
        session.current_message_bit_length = 1
        talent_source = _outburst_talent_source()
        session.gameplay_persistent_status_effects = {
            talent_source.field_04: talent_source,
        }
        fixture.move(session, (25.0, 0.0, 50.0))
        near_actor, outer_actor = combat.encounter.actor_ids[:2]
        combat.monster_positions[near_actor] = (25.0, 0.0, 53.0)
        combat.monster_positions[outer_actor] = (25.0, 0.0, 67.0)
        session.gameplay_delivered_monster_positions = dict(
            combat.monster_positions
        )
        stats = SimpleNamespace(
            minimum_damage=10.0, maximum_damage=10.0,
            critical_hit_chance=0.0, critical_damage_multiplier=2.0,
            attack_speed=1.0, skill_modifier_sources=(),
        )
        cast = SkillCast(0, outburst, 0.0, 100, 700)
        published = []
        with (
            patch.object(player_combat, "decode_skill", return_value=cast),
            patch.object(
                player_combat, "calculate_player_combat_stats",
                return_value=stats,
            ),
            patch.object(player_combat.time, "perf_counter", return_value=100.0),
            patch.object(
                CharacterService, "_accept_player_skill_timing",
                return_value=True,
            ),
            patch.object(
                CharacterService, "_apply_player_skill_resource",
                return_value=True,
            ),
            patch.object(CharacterService, "_consume_active_essence"),
            patch.object(CharacterService, "_cancel_mount_cast"),
            patch.object(CharacterService, "_remove_mount_ride"),
            patch.object(CharacterService, "_cleanse_player_skill_debuffs"),
            patch.object(
                CharacterService, "_snapshot_pvp_skill_targets",
                return_value=((), ()),
            ),
            patch.object(CharacterService, "_send_player_skill_status_effects"),
            patch.object(CharacterService, "_send_attack_speed_bonus_stack"),
            patch.object(
                CharacterService, "_publish_player_location_visuals",
                side_effect=lambda *args, **kwargs: published.append(
                    (args, kwargs)
                ),
            ),
            patch.object(CharacterService, "_start_monster_chase"),
            patch.object(CharacterService, "_record_monster_targeting_result"),
            patch.object(
                CharacterService, "_register_runtime_projectile_deadlines",
                return_value=((), ()),
            ),
        ):
            CharacterServer._handle_spatial_skill(
                session, b"\x8b" + SKILL_COMMAND.to_bytes(2, "little"),
            )

        assert len(published) == 1
        args, kwargs = published[0]
        (aura,) = args[2]
        assert aura.resource_id == "skill_battlecry_taunt_aura"
        assert aura.aura_radius == 15.0
        assert aura.duration_ticks == 250
        assert kwargs["managed_aura"] is True
        assert kwargs["follows_owner"] is True
        taunt = next(
            effect for effect in kwargs["receiver_status_effects"]
            if effect.resource_id == "skill_battlecry_taunt_effect"
        )
        assert taunt.duration_ticks == 250
        fear_effect_ids = {
            "skill_battlecry_debuff_movementspeed",
            "skill_battlecry_debuff_resistance",
            "skill_battlecry_debuff_attackspeed",
        }
        pending_hits = tuple(combat.pending_runtime_player_hits.values())
        assert {hit.target_actor_id for hit in pending_hits} == {
            near_actor, outer_actor,
        }
        assert all(
            fear_effect_ids <= {
                effect.resource_id for effect in hit.victim_status_effects
            }
            for hit in pending_hits
        )
    finally:
        fixture.doCleanups()


def test_outburst_taunts_enemies_within_fifteen_metres_for_ten_seconds():
    combat = _combat()
    outburst = _row_id("_Template_Skill", "battlecry")
    talent_source = _outburst_talent_source()
    user_effects = combat.player_skill_user_effects(
        outburst, cast_id=700, trigger="skillstart",
        modifier_effect_ids=(talent_source,),
    )
    aura, receivers, _delay = combat.player_user_effect_location_auras(
        user_effects,
    )[0]
    taunt = next(
        effect for effect in receivers
        if effect.resource_id == "skill_battlecry_taunt_effect"
    )
    assert aura.duration_ticks == 250
    assert taunt.duration_ticks == 250

    inside_actor, outside_actor = combat.living_actor_ids[:2]
    player_position = (0.0, 0.0, 0.0)
    combat.monster_positions[inside_actor] = (14.9, 0.0, 0.0)
    combat.monster_positions[outside_actor] = (17.0, 0.0, 0.0)
    combat.player_health[OTHER_PLAYER] = [1000, 1000]
    combat.start_player_location_visuals(
        (aura,), position=player_position, yaw=0.0, server_tick=100,
        owner_actor_id=OWNER, cast_id=700,
        receiver_status_effects=receivers, follows_owner=True,
        managed_aura=True, now=100.0,
    )
    _resolutions, applications = combat.apply_player_location_auras(
        now=100.0, server_tick=100,
        player_positions={OWNER: player_position},
    )

    applied_taunt = next(
        effect for application in applications
        for effect in application.status_effects
        if effect.resource_id == "skill_battlecry_taunt_effect"
    )
    assert applied_taunt.duration_ticks == 30
    assert inside_actor in {
        application.target_actor_id for application in applications
    }
    assert outside_actor not in {
        application.target_actor_id for application in applications
    }
    with patch.object(
        CharacterService, "_send_socket_actor_applications",
    ) as send_actor_applications:
        CharacterService._dispatch_location_aura_applications(
            {OWNER: SimpleNamespace()}, applications,
        )
    actor_batches = send_actor_applications.call_args.args[1]
    mob_batch = next(
        batch for batch in actor_batches if batch[0] == inside_actor
    )
    assert any(
        effect.resource_id == "skill_battlecry_taunt_effect"
        for effect in mob_batch[2]
    )
    assert combat.monster_status_effects[inside_actor][
        "skill_battlecry_taunt_effect"
    ][1] == 101.2
    # Taunt overrides a nearer player's normal aggro selection.
    assignments = combat.assign_monster_targets(
        (inside_actor,),
        (
            (OWNER, player_position, {inside_actor}),
            (OTHER_PLAYER, (14.6, 0.0, 0.0), {inside_actor}),
        ),
        now=100.1,
    )
    assert assignments[inside_actor][0] == OWNER

    # Managed refreshes keep the taunt at the mob through the full authored
    # ten-second aura, then normal nearest-player aggro resumes.
    combat.apply_player_location_auras(
        now=109.5, server_tick=337,
        player_positions={OWNER: player_position},
    )
    assert combat.monster_status_effects[inside_actor][
        "skill_battlecry_taunt_effect"
    ][1] == 110.02
    assignments = combat.assign_monster_targets(
        (inside_actor,),
        (
            (OWNER, player_position, {inside_actor}),
            (OTHER_PLAYER, (14.6, 0.0, 0.0), {inside_actor}),
        ),
        now=109.99,
    )
    assert assignments[inside_actor][0] == OWNER
    assignments = combat.assign_monster_targets(
        (inside_actor,),
        (
            (OWNER, player_position, {inside_actor}),
            (OTHER_PLAYER, (14.6, 0.0, 0.0), {inside_actor}),
        ),
        now=110.03,
    )
    assert assignments[inside_actor][0] == OTHER_PLAYER


def test_outburst_applies_fear_debuffs_to_all_targets_inside_eighteen_metres():
    combat = _combat()
    outburst = _row_id("_Template_Skill", "battlecry")
    talent_source = _outburst_talent_source()
    geometry = combat._modified_player_skill_geometry(
        combat._load_player_skill_geometry(outburst), (talent_source,),
    )
    origin = combat.encounter.coordinate_frame.wire_to_world((0, 0, 0))
    inside = combat.living_actor_ids[:3]
    outside = combat.living_actor_ids[3]
    for actor_id, distance in zip(inside, (5.0, 10.0, 17.0)):
        combat.monster_positions[actor_id] = (
            origin[0] + distance, origin[1], origin[2],
        )
    combat.monster_positions[outside] = (
        origin[0] + 22.0, origin[1], origin[2],
    )

    plan = combat.plan_entity_skill_hits(
        skill_index=outburst,
        player_position=(0, 0, 0),
        projectile=False,
        eligible_actor_ids=(*inside, outside),
        geometry=geometry,
        now=100.0,
    )
    assert {impact.target.actor_id for impact in plan.impacts} == set(inside)

    pending = combat.start_spatial_skill_hits(
        skill_index=outburst,
        cast_id=701,
        server_tick=200,
        player_position=(0, 0, 0),
        source_actor_id=OWNER,
        eligible_actor_ids=(*inside, outside),
        hit_plan=plan,
        now=100.0,
        player_damage_range=(1.0, 1.0),
    )
    expected = {
        "skill_battlecry_debuff_movementspeed",
        "skill_battlecry_debuff_resistance",
        "skill_battlecry_debuff_attackspeed",
    }
    assert {hit.target_actor_id for hit in pending} == set(inside)
    resolved_effects_by_actor = {}
    for hit_index, hit in enumerate(pending):
        assert expected <= {effect.resource_id for effect in hit.victim_status_effects}
        assert hit.area_radius == 18.0
        resolution = combat.resolve_runtime_player_hit(
            OWNER, 701, hit.impact_index, now=hit.due_at + 0.01,
        )
        assert resolution is not None
        resolved_effects_by_actor[hit.target_actor_id] = (
            resolution.victim_status_effects
        )
        assert expected <= {
            effect.resource_id for effect in resolution.victim_status_effects
        }
        if hit_index == 0:
            session = SimpleNamespace(
                flow_lock=threading.RLock(),
                gameplay_combat_state=combat,
                gameplay_victim_status_effect_instances={},
                gameplay_effect_cast_applications={},
                gameplay_next_status_effect_instance_id=1,
            )
            transactions = CharacterServer._victim_effect_command_transactions(
                session, (resolution,),
            )
            sent_effect_ids = {
                entry.effect_id
                for commands in transactions
                for command in commands
                for entry in command.value.entries
                if entry.has_details
            }
            expected_ids = {
                effect.effect_id
                for effect in resolution.victim_status_effects
                if effect.resource_id in expected
            }
            assert expected_ids <= sent_effect_ids

    # The authoritative monster simulation consumes all three debuffs too.
    actor_id = inside[0]
    assert abs(combat._monster_status_scale(
        actor_id, "Speed", "Movement", now=104.0,
    ) - 0.89) < 1e-6
    assert abs(combat._monster_status_scale(
        actor_id, "Speed", "Skills", now=104.0,
    ) - 0.9) < 1e-6
    assert abs(combat.monster_incoming_damage_multiplier(
        actor_id, "Fire", now=104.0,
    ) - 1.2) < 1e-6

    # Verify the debuffs change authoritative movement and attack timing,
    # rather than only appearing in the emitted status packet.
    direct_encounter = replace(
        combat.encounter, navigation=_DirectNavigationMesh(), terrain=None,
    )
    origin = direct_encounter.coordinate_frame.wire_to_world((0, 0, 0))
    movement_distances = {}
    for feared in (False, True):
        state = LocalCombatState(
            direct_encounter, player_actor_id=OWNER, player_health=1000,
        )
        mob = state.living_actor_ids[0]
        state.monsters[mob] = replace(
            state.monsters[mob], run_speed=1.0, aggro_range=100.0,
            escape_range=100.0, travel_distance=100.0,
            position=origin,
        )
        state.monster_positions[mob] = origin
        if feared:
            effects = state.accepted_monster_status_effects(
                mob, resolved_effects_by_actor[actor_id], now=104.0,
            )
            state._record_monster_status_effects(
                mob, effects, now=104.0, source_actor_id=OWNER,
                player_damage_range=(1.0, 1.0), skill_index=outburst,
            )
        target = direct_encounter.coordinate_frame.world_to_wire(
            (origin[0] + 10.0, origin[1], origin[2]),
        )
        state.advance_monsters(
            target, elapsed_seconds=0.25, server_tick=2600, now=104.0,
            eligible_actor_ids={mob},
        )
        movement_distances[feared] = state._distance(
            origin, state.monster_positions[mob],
        )
    assert movement_distances[True] < movement_distances[False]

    attack_intervals = {}
    for feared in (False, True):
        state = _combat()
        mob = state.living_actor_ids[0]
        home = state.monster_positions[mob]
        state.monsters[mob] = replace(
            state.monsters[mob], attack_interval=1.0,
            aggro_range=100.0, escape_range=100.0,
        )
        state.monster_positions[mob] = (home[0] + 1.0, home[1], home[2])
        state.last_monster_attack_at[mob] = 99.0
        if feared:
            effects = state.accepted_monster_status_effects(
                mob, resolved_effects_by_actor[actor_id], now=100.05,
            )
            state._record_monster_status_effects(
                mob, effects, now=100.05, source_actor_id=OWNER,
                player_damage_range=(1.0, 1.0), skill_index=outburst,
            )
        target = state.encounter.coordinate_frame.world_to_wire(
            (home[0] + 2.0, home[1], home[2]),
        )
        attacks = state.maybe_start_monster_attacks(
            target, server_tick=2501, now=100.05,
            eligible_actor_ids={mob},
        )
        attack_intervals[feared] = len(attacks)
    assert attack_intervals[False] == 1
    assert attack_intervals[True] == 0
