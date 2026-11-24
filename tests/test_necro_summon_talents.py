"""Soulweaver summon skill-node and owner-stat regressions."""

import sqlite3

import pytest

from dso_server.gameplay.local_combat_state import (
    LocalCombatState,
    damage_type_ids,
)
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import StatusEffectEntry


def _row(table, resource_id):
    with sqlite3.connect(
        f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True,
    ) as database:
        return int(database.execute(
            f"SELECT rowid - 1 FROM {table} WHERE Id=?",
            (resource_id,),
        ).fetchone()[0])


def _source(resource_id):
    return StatusEffectEntry(
        effect_id=_row("_Template_StatusEffect", resource_id),
        instance_id=1,
        end_tick=0,
        tick_interval=25,
        start_tick=0,
        stack_index=0,
        total_duration=25,
        source_level=100,
        source_actor_id=1,
        update_timing=False,
        stacked=False,
        permanent=True,
        has_details=False,
    )


@pytest.fixture
def combat():
    encounter = load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH,
        "pw214_mortis_boss",
        include_spawn_areas=False,
    )
    return LocalCombatState(encounter, player_actor_id=1, player_health=1000)


@pytest.mark.parametrize(
    "node,skill,template,limit,extra",
    (
        ("necro_talent_skill_cat_01_01", "NecroSkeletonArcher",
         "minion_necro_skeleton_archer_mana", 1, ()),
        ("necro_talent_skill_cat_01_02", "NecroSkeletonArcher",
         "minion_necro_skeleton_archer", 2, ()),
        ("necro_talent_skill_cat_03_01", "DeathHound",
         "minion_necro_death_hound_extended", 1, ()),
        ("necro_talent_skill_cat_03_01", "NecroSkeletonWarrior",
         "minion_necro_skeleton_warrior_heal", 1, ()),
        ("necro_talent_skill_cat_03_02", "NecroSkeletonWarrior",
         "minion_necro_skeleton_warrior", 2, ()),
        ("necro_talent_skill_cat_03_03", "DeathHound",
         "minion_necro_death_hound_passive", 1, ()),
        ("necro_talent_skill_cat_04_01", "NecroSkeletonMage",
         "minion_necro_skeleton_mage_pierce", 1, ()),
        ("necro_talent_skill_cat_04_02", "NecroSkeletonMage",
         "minion_necro_skeleton_mage", 2, ()),
    ),
)
def test_summon_shaping_skill_nodes_are_applied(
    combat, node, skill, template, limit, extra,
):
    definition = combat.player_summon_definition(
        _row("_Template_Skill", skill),
        modifier_sources=(_source(node),),
    )
    assert definition.monster_template_id == template
    assert definition.limit == limit
    assert tuple(
        summon.monster_template_id
        for summon in definition.additional_summons
    ) == extra


def test_bone_crypt_spawns_second_hound_with_owner_combat_stats(combat):
    definition = combat.player_summon_definition(
        _row("_Template_Skill", "DeathHound"),
        modifier_sources=(_source("necro_talent_skill_cat_03_02"),),
    )
    assert tuple(
        summon.monster_template_id
        for summon in definition.additional_summons
    ) == ("minion_necro_death_hound_delayed",)

    spawned = combat.spawn_player_summon(
        definition,
        owner_actor_id=1,
        position=(40.0, 0.0, 10.0),
        yaw=0.0,
        level=60,
        server_tick=500,
        player_damage_range=(100.0, 200.0),
        critical_hit_chance=0.35,
        critical_damage_multiplier=3.0,
        now=100.0,
    )
    assert {
        summon.definition.monster_template_id for summon in spawned.actors
    } == {
        "minion_necro_death_hound",
        "minion_necro_death_hound_delayed",
    }
    assert all(summon.player_damage_range == (100.0, 200.0)
               for summon in spawned.actors)
    assert all(summon.critical_hit_chance == 0.35
               for summon in spawned.actors)
    assert all(summon.critical_damage_multiplier == 3.0
               for summon in spawned.actors)


def test_summon_damage_uses_active_target_resistance_break(combat):
    actor_id = next(iter(combat.monsters))
    ordinary = combat.monster_incoming_damage_multiplier(
        actor_id, "Physical", now=100.0,
    )
    with sqlite3.connect(
        f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True,
    ) as database:
        debuff = combat._load_victim_status_effects(
            database,
            "ce2026_all_resistance_down",
            default_trigger="skillstart",
        )[0]
    # Keep this focused on the incoming-damage evaluator; selected Mortis
    # actors may refuse the generic test debuff through their authored
    # immunity groups.
    combat.monster_status_effects[actor_id] = {
        debuff.resource_id: (debuff, 200.0),
    }
    broken = combat.monster_incoming_damage_multiplier(
        actor_id, "Physical", now=100.0,
    )
    assert ordinary == pytest.approx(1.0)
    assert broken > ordinary

    mastery_penetration = combat.summon_owner_damage_multiplier(
        (
            _source("necro_talent_bone_master"),
            _source("necro_talent_bone_master_lvl"),
        ),
        "Physical",
    )
    assert mastery_penetration == pytest.approx(1.1)


def test_owner_mastery_penetration_applies_to_casts_and_nether_tag(combat):
    sources = (
        _source("necro_talent_bone_master"),
        _source("necro_talent_bone_master_lvl"),
    )
    skill_index = _row("_Template_Skill", "BoneSpikes")
    geometry = combat._load_player_skill_geometry(skill_index)
    modified = combat._modified_player_skill_geometry(geometry, sources)
    assert modified.damage_modifier > geometry.damage_modifier
    assert damage_type_ids("NetherMagic") == (4,)
