"""Equipment ``TalentLevelBonus`` raises the talent's own rank.

The dwarf's premium trader rifle (``dwarf_unique_2h_rifle_premium_01``)
authors ``TalentLevelBonus:5,dwarf_HeavyShot_talent`` ("increases the Heavy
Shot talent by 5"). Only a talent's ``StackStatusEffectId`` was read, so a
skill talent, whose ranks are its ``BaseStatusEffectId`` alone, gained
nothing. Every test uses the live ``static.db4`` rows.
"""

import pytest

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.gameplay.player_combat_stats import (
    CombatModifier, calculate_player_combat_stats,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence import readonly_db
from dso_server.protocol.gameplay_protocol import StatusEffectEntry
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo


def db():
    return readonly_db.connect(DEFAULT_STATIC_DB_PATH)


def skill(resource_id):
    return db().execute(
        "SELECT rowid-1 FROM _Template_Skill WHERE Id=?", (resource_id,),
    ).fetchone()[0]


def rank(effect, value):
    """A talent rank as TalentDatabase.status_effect_entries sends it."""
    effect_id = db().execute(
        "SELECT rowid-1 FROM _Template_StatusEffect WHERE Id=?", (effect,),
    ).fetchone()[0]
    return StatusEffectEntry(
        effect_id, 100, 0, 25, 0, value - 1, 25, 100, 1,
        False, value > 1, True, True,
    )


def bonus(levels, talent):
    return CombatModifier("TalentLevelBonus", (
        str(levels), talent, "StringParamLocaOverride", "db.skilltalent2.table"))


@pytest.fixture(scope="module")
def combat():
    return LocalCombatState(
        load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False),
        player_actor_id=1, player_health=1000,
    )


def resolved(combat, resource_id, sources):
    return combat._modified_player_skill_geometry(
        combat._load_player_skill_geometry(skill(resource_id)), tuple(sources))


def explosion(geometry):
    return next(e.values for e in geometry.location_visual_effects
                if e.resource_id == "skill_heavyshot_explosion_aura")


def test_heavy_shot_bonus_runs_the_rank_program_at_base_plus_bonus(combat):
    learned = resolved(combat, "HeavyShot", (rank("skill_HeavyShot_lvl", 10),))
    geared = resolved(combat, "HeavyShot", (
        rank("skill_HeavyShot_lvl", 10), bonus(5, "dwarf_HeavyShot_talent")))
    fifteen = resolved(combat, "HeavyShot", (rank("skill_HeavyShot_lvl", 15),))
    # skill_lvl_heavyshot_dmg: +2.5% per rank, explosion -5% per rank.
    assert geared.damage_modifier == pytest.approx(learned.damage_modifier + 0.125)
    assert geared.damage_modifier == pytest.approx(fifteen.damage_modifier)
    assert explosion(geared) == pytest.approx(explosion(fifteen))
    assert explosion(geared) != pytest.approx(explosion(learned))


def test_an_unlearned_talent_runs_at_its_bonus_ranks(combat):
    geared = resolved(combat, "HeavyShot", (bonus(5, "dwarf_HeavyShot_talent"),))
    five = resolved(combat, "HeavyShot", (rank("skill_HeavyShot_lvl", 5),))
    authored = resolved(combat, "HeavyShot", ())
    assert geared.damage_modifier == pytest.approx(five.damage_modifier)
    assert geared.damage_modifier > authored.damage_modifier


def test_mine_bonus_lays_the_higher_ranked_mine(combat):
    """``TrapOnHit:Mine,dwarf_mine,...`` names the trap by rank; the rows go
    up to ``dwarf_mine20``. A second program at the bonus ranks alone laid
    ``dwarf_mine4`` over a rank 10 talent."""
    learned = resolved(combat, "Mine", (rank("skill_Mine_lvl", 10),))
    geared = resolved(combat, "Mine", (
        rank("skill_Mine_lvl", 10), bonus(4, "dwarf_Mine_talent")))
    assert learned.trap_on_hit == "dwarf_mine10"
    assert geared.trap_on_hit == "dwarf_mine14"


def test_a_stack_talent_adds_its_bonus_to_the_stack_effect(combat):
    """``ranger_talent_skill_cat_03_dmg``: an unlock effect at rank 1, then a
    stack effect from rank 2 (TalentDatabase.status_effect_ids_for_rank)."""
    unlock = rank("ranger_talent_skill_cat_03_dmg_unlock", 1)
    three = (unlock, rank("ranger_talent_skill_cat_03_dmg", 2))
    seven = (unlock, rank("ranger_talent_skill_cat_03_dmg", 6))
    geared = resolved(combat, "mightyshot", (
        *three, bonus(4, "ranger_talent_skill_cat_03_dmg")))
    assert geared.damage_modifier == pytest.approx(
        resolved(combat, "mightyshot", seven).damage_modifier)
    assert geared.damage_modifier > resolved(combat, "mightyshot", three).damage_modifier


def test_a_bonus_never_selects_a_second_elemental_mastery(combat):
    """The client refuses a talent while another of its ``GroupId`` has
    ranks (dwarfTalentElemental): a Frost Master keeps no Fire Master."""
    frost = (rank("dwarf_talent_frozen_master", 1),)
    sources = combat._talent_ranked_sources(
        (*frost, bonus(1, "dwarf_talent_fire_master")))
    assert sources == frost
    fire = combat._talent_ranked_sources((bonus(1, "dwarf_talent_fire_master"),))
    assert [entry.effect_id for entry in fire] == [
        rank("dwarf_talent_fire_master", 1).effect_id]


def test_the_rifle_bonus_reaches_the_cast_sources():
    """The cast handler appends PlayerCombatStats.skill_modifier_sources."""
    rifle = ItemInfo(
        item_id=7, template_id="dwarf_unique_2h_rifle_premium_01",
        item_level=100, amount=1,
    )
    stats = calculate_player_combat_stats(
        DEFAULT_STATIC_DB_PATH, class_name="dwarf", level=100,
        inventory=InventoryInfo(
            health_points=1000, secondary_resource=100,
            items=(rifle,), equipment_slots={7: (5, 6)},
        ),
    )
    assert [(m.arguments[0], m.arguments[1]) for m in stats.skill_modifier_sources
            if m.kind == "TalentLevelBonus"] == [("5", "dwarf_HeavyShot_talent")]


def test_sources_without_a_bonus_are_returned_unchanged(combat):
    sources = (rank("skill_HeavyShot_lvl", 10), CombatModifier("Damage", ("1", "relative")))
    assert combat._talent_ranked_sources(sources) is sources
