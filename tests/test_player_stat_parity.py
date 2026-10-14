"""Screenshot regression and native item/actor stat boundaries."""
import json
import math
from dataclasses import replace
from types import SimpleNamespace

import pytest

from dso_server.gameplay.player_combat_stats import (
    CombatModifier, _calculate_player_combat_stats, prime_player_combat_database,
)
from dso_server.gameplay.status_modifier_runtime import (
    StatusModifierCatalog, StatusModifierDefinition, evaluate_active_status_modifiers,
    parse_modifier_program,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.tools.audit_character_stats import DEFAULT_FIXTURE, evaluate_character


@pytest.fixture(scope="module")
def database():
    return prime_player_combat_database(DEFAULT_STATIC_DB_PATH)


@pytest.fixture(scope="module")
def sheet():
    document = json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"))
    return document, evaluate_character(document)


@pytest.mark.parametrize("key,field,places", [
    ("critical", "critical_rating", 0), ("attack_speed", "attack_speed", 3),
    ("regeneration", "displayed_health_regeneration_per_second", 2),
    ("block", "block_value", 0),
])
def test_example_player_matches_screenshot(sheet, key, field, places):
    document, results = sheet
    assert round(results["pet_off"][field], places) == document["screenshot"][key]


@pytest.mark.parametrize("element", ["fire", "ice", "lightning", "darkmagic", "poison"])
def test_example_player_elemental_defenses_match(sheet, element):
    document, results = sheet
    assert round(dict(results["pet_off"]["resistance_ratings"])[element]) == document["screenshot"][element]


@pytest.mark.parametrize("key,field", [("damage","minimum_damage"), ("health","maximum_health"), ("armor","armor_rating")])
@pytest.mark.xfail(strict=True, reason="Known exact-display residuals: damage -1, health +1, armor -4; no compensating constants")
def test_remaining_exact_display_residuals(sheet, key, field):
    document, results = sheet
    assert math.floor(results["pet_off"][field] + .5) == document["screenshot"][key]


def test_example_player_pet_collection_is_conditional(sheet):
    _, results = sheet
    off, on = results["pet_off"], results["pet_on"]
    assert round(on["critical_rating"]) == 358213
    assert on["maximum_health"] == 1055781
    assert on["critical_rating"] > off["critical_rating"]
    assert on["maximum_health"] > off["maximum_health"]
    assert on["attack_speed"] == off["attack_speed"]
    assert on["minimum_damage"] == off["minimum_damage"]


def test_example_player_drop_awards_use_current_wisdom_rank(sheet):
    _, results = sheet
    for result in results.values():
        assert result["drop_award_from_10000"] == {
            "money_rc":12500, "money_vc":20600, "money_gc":10000, "materi_fragment_01":10000,
        }


def modified_inventory(database, *programs, scale=False):
    template = database.items["all_base_trinket_rune_01"]
    definitions = [replace(template, template_id=f"stat_test_{i}", scale_with_level=scale,
        base_enchantment_ids=(), template_enchantment_ids=(), modifiers=tuple(program))
        for i, program in enumerate(programs)]
    db = replace(database, items={**database.items, **{d.template_id:d for d in definitions}})
    inv = InventoryInfo(items=[ItemInfo(item_id=i+1, item_level=100, template_id=d.template_id)
                               for i,d in enumerate(definitions)],
                        equipment_slots={i+1:(14+i,) for i in range(len(definitions))})
    return db, inv


def calculate(database, inventory, *sources):
    return _calculate_player_combat_stats(database, class_name="mage", level=100,
                                         inventory=inventory, modifier_sources=sources)


@pytest.mark.parametrize("item_kind,actor_kind,scope,field", [
    ("ItemCriticalValue","CriticalValue",(),"critical_rating"),
    ("ItemArmor","Armor",(),"armor_rating"),
    ("ItemResistance","Resistance",("Fire",),"fire"),
])
def test_item_rating_multiplier_cannot_scale_other_items_or_actor_talents(database, item_kind, actor_kind, scope, field):
    db, inv = modified_inventory(database,
        (CombatModifier(item_kind,("100","absolute",*scope)), CombatModifier(item_kind,("0.5","relative",*scope))),
        (CombatModifier(item_kind,("200","absolute",*scope)),))
    stats = calculate(db,inv,CombatModifier(actor_kind,("1000","absolute",*scope)),
                            CombatModifier(actor_kind,("0.2","relative",*scope)))
    actual = dict(stats.resistance_ratings)[field] if field == "fire" else getattr(stats,field)
    assert actual == pytest.approx((100*1.5 + 200 + 1000)*1.2)


def test_item_all_resistance_and_element_enchantments_have_separate_attributes(database):
    db, inv = modified_inventory(database, (
        CombatModifier("ItemResistance",("100","absolute","All")),
        CombatModifier("ItemResistance",("0.5","relative","All")),
        CombatModifier("ItemResistance",("200","absolute","Ice")),
        CombatModifier("ItemResistance",("0.25","relative","Ice")),
    ))
    resistances = dict(calculate(db, inv).resistance_ratings)
    assert resistances["ice"] == 100*1.5 + 200*1.25
    assert resistances["fire"] == 100*1.5


def test_unique_item_attributes_scale_but_actor_flat_bonus_does_not(database):
    db, inv = modified_inventory(database, (
        CombatModifier("ItemDamage",("1","absolute")),
        CombatModifier("Damage",("5000","absolute")),
    ), scale=True)
    stats = calculate(db,inv)
    factor = database.progression_factor(100,"DamageFactorAbsolute") / database.progression_factor(1,"DamageFactorAbsolute")
    assert stats.minimum_damage == pytest.approx(stats.class_base_damage + factor + 5000)


def test_attack_speed_keeps_item_boundaries_and_weapon_scope(database):
    db, inv = modified_inventory(database,
        (CombatModifier("ItemSpeed",("0.2","absolute","Skills")), CombatModifier("ItemSpeed",("0.5","relative","Skills"))),
        (CombatModifier("ItemSpeed",("0.4","absolute","Skills")),))
    inv.equipment_slots[1] = (5,6)
    sources = (CombatModifier("Speed",("0.1","absolute","Skills")),
               CombatModifier("Speed",("0.2","relative","Skills")),
               CombatModifier("WeaponSpeedBonus",("0.3","absolute","LargeWeapon")),
               CombatModifier("WeaponSpeedBonus",("20","absolute","SmallWeapon")))
    assert calculate(db,inv,*sources).attack_speed == pytest.approx((1 + .2*1.5 + .3 + .4 + .1)*1.2)
    inv.equipment_slots[1] = (14,)
    assert calculate(db,inv,*sources).attack_speed == pytest.approx((1 + .2*1.5 + .4 + .1)*1.2)


@pytest.mark.parametrize("trigger", ["Hit", "LowHealth", "EnterPvP", "Disabled"])
def test_conditional_speed_buff_waits_for_active_instance(database, trigger):
    effect_id = 900001
    db = replace(database, status_effect_ids={**database.status_effect_ids,"test_speed_child":effect_id},
        status_effect_programs={**database.status_effect_programs,effect_id:(CombatModifier("Speed",("0.5","relative","Skills")),)})
    source = CombatModifier("ActorStatusEffect",("addAE","test_speed_child","C:1","ON:"+trigger))
    inv = InventoryInfo()
    assert calculate(db,inv,source).attack_speed == 1
    assert calculate(db,inv,source,SimpleNamespace(effect_id=effect_id,field_18=0,values=())).attack_speed == 1.5


def test_increase_relative_regeneration_scales_base_fraction(database):
    stats = calculate(database,InventoryInfo(),
        CombatModifier("HealthPointsRegeneration",("0.02","relative")),
        CombatModifier("HealthPointsRegeneration",("0.5","relative","IncreaseRelative")),
        CombatModifier("HealthPointsRegeneration",("12","absolute")))
    base, absolute, _ = database.player_recovery("mage")
    assert stats.health_regeneration_fraction == pytest.approx((base+.02)*1.5-base)
    assert stats.health_regeneration_absolute == 12


def test_status_drop_variables_resolve_before_reads_even_from_generator():
    catalog = StatusModifierCatalog(definitions={
        1:StatusModifierDefinition(1,1,parse_modifier_program("VirtualCurrencyDropAmount:$rank_bonus,relative"),()),
        2:StatusModifierDefinition(2,3,parse_modifier_program("ActorVariable:$0,absolute,rank_bonus"),()),
    }, actor_variables={"rank_bonus":.01})
    # Reader precedes the rank writer; an oversized occurrence is clamped to 3.
    entries = (SimpleNamespace(effect_id=1,values=(),field_18=0),
               SimpleNamespace(effect_id=2,values=(.1,),field_18=99))
    bonus = evaluate_active_status_modifiers(catalog,(entry for entry in entries))
    assert bonus.drop_amount_bonus("money_vc").apply(10000) == 13100
    assert catalog.actor_variables["rank_bonus"] == .01
