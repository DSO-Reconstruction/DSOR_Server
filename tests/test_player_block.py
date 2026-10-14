"""Block rates from the captured character sheet through real monster impacts."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence.account_state import _inventory_from_document
from dso_server.protocol.gameplay_protocol import StatusEffectEntry
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.talent_protocol import TalentDatabase
from dso_server.gameplay.player_combat_stats import (
    CombatModifier, calculate_player_combat_stats, prime_player_combat_database,
)
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_monster_encounter


def effect(resource_id, *, values=()):
    database = prime_player_combat_database(DEFAULT_STATIC_DB_PATH)
    return StatusEffectEntry(
        effect_id=database.status_effect_ids[resource_id], field_04=100,
        field_0c=0, field_14=25, field_08=0, field_18=0, field_10=25,
        field_1c=100, field_20=1, field_25=False, field_26=False,
        field_39=True, has_details=True, values=values,
    )


def stats(inventory=None, sources=()):
    return calculate_player_combat_stats(
        DEFAULT_STATIC_DB_PATH, class_name="mage", level=100,
        inventory=inventory if inventory is not None else InventoryInfo(),
        modifier_sources=tuple(sources),
    )


@pytest.fixture
def saved_block_loadout():
    data = json.loads((Path(__file__).parent / "fixtures/player_block_example_player.json").read_text(encoding="utf-8"))
    talents = TalentDatabase(DEFAULT_STATIC_DB_PATH)
    sources = []
    for selection in data["talents"]:
        sources.extend(talents.status_effect_entries(
            talents.definition(selection["resource_id"]),
            target_rank=selection["rank"], actor_id=1,
            first_instance_id=1000 + len(sources), server_tick=0,
        ))
    sources.append(effect(data["pet"]["resource_id"], values=tuple(data["pet"]["values"])))
    return data, _inventory_from_document(data["inventory"]), tuple(sources)


def test_example_player_matches_displayed_block_value_and_percentage(saved_block_loadout):
    data, inventory, sources = saved_block_loadout
    actual = stats(inventory, sources)
    coefficient = prime_player_combat_database(DEFAULT_STATIC_DB_PATH).progression_factor(100, "BlockChanceFactor")
    assert round(actual.block_chance / coefficient) == data["displayed_block_value"]
    assert round(actual.block_chance * 100, 2) == data["displayed_block_percent"]


def test_item_block_enchantment_cannot_multiply_wisdom_or_other_items(saved_block_loadout):
    _, inventory, sources = saved_block_loadout
    # These shoulders own a relative ItemBlockValue enchantment but no base
    # block. They must not increase the torso's block or the Wisdom bonus.
    sources = (CombatModifier("BlockValue", ("41405", "absolute")),)
    with_shoulders = stats(inventory, sources).block_chance
    inventory.items = [i for i in inventory.items if i.template_id != "mage_mythic_shoulders_01"]
    assert stats(inventory, sources).block_chance == pytest.approx(with_shoulders)


@pytest.mark.parametrize("trigger", ["LowHealth", "EnterPvP", "Hit", "Die", "Disabled"])
def test_conditional_actor_effect_is_not_a_permanent_bonus(trigger):
    source = CombatModifier("ActorStatusEffect", ("addAE", "mage_cyanitskin", "C:1", "ON:" + trigger))
    assert stats(sources=(source,)).block_chance == 0


def test_triggered_child_counts_only_when_an_active_instance_is_present():
    activator = effect("pvp_mage_talent3_activator")
    active = effect("mage_cyanitskin")
    assert stats(sources=(activator,)).block_chance == 0
    assert stats(sources=(active,)).block_chance > 0
    assert stats(sources=(activator, active)).block_chance == stats(sources=(active,)).block_chance


@pytest.mark.parametrize("trigger", ["", "Immediate", "ImmediateManaged"])
def test_unconditional_child_effect_still_contributes(trigger):
    args = ("addAE", "mage_cyanitskin", "C:1") + (("ON:" + trigger,) if trigger else ())
    assert stats(sources=(CombatModifier("ActorStatusEffect", args),)).block_chance == stats(sources=(effect("mage_cyanitskin"),)).block_chance


def test_chance_based_immediate_child_requires_an_active_instance():
    source = CombatModifier("ActorStatusEffect", ("addAE", "mage_cyanitskin", "C:0.5", "ON:Immediate"))
    assert stats(sources=(source,)).block_chance == 0


def test_pet_critical_parameter_does_not_replace_zero_collection_variables():
    base = (CombatModifier("CriticalValue", ("1000", "absolute")),
            CombatModifier("BlockValue", ("1000", "absolute")))
    plain = stats(sources=base)
    pet = effect("pet_jabbax", values=(0.25, 0, 0, 0, 0))
    with_pet = stats(sources=base + (pet,))
    assert with_pet.critical_hit_chance == pytest.approx(plain.critical_hit_chance * 1.25)
    assert with_pet.block_chance == plain.block_chance
    assert with_pet.maximum_health == plain.maximum_health
    assert with_pet.minimum_damage == plain.minimum_damage
    collection = CombatModifier("ActorVariable", ("0.10", "absolute", "pet_block_01"))
    assert stats(sources=base + (pet, collection)).block_chance == pytest.approx(plain.block_chance * 1.10)


def test_saved_loadout_blocks_expected_fraction_of_real_monster_hits(saved_block_loadout):
    _, inventory, sources = saved_block_loadout
    player_stats = stats(inventory, sources)
    encounter = load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False)
    combat = LocalCombatState(encounter, player_actor_id=1, player_health=100_000_000)
    monster_id = next(a for a in encounter.actor_ids if combat.monsters[a].attack is not None)
    position = combat.monster_positions[monster_id]
    wire = encounter.coordinate_frame.world_to_wire(position)
    attacks = combat.maybe_start_monster_attacks(
        wire, server_tick=100, now=max(combat.last_monster_attack_at.values()) + 10,
        target_actor_id=1, eligible_actor_ids={monster_id},
    )
    assert len(attacks) == 1
    attack = replace(attacks[0], motion_type="Target", target_position=position, projectile_origin=position)
    blocked = 0
    samples = 20_000
    for index in range(samples):
        combat.player_health[1][0] = 100_000_000
        pending = replace(attack, server_tick=100 + index * 25, impact_tick=attack.impact_tick + index * 25)
        combat.pending_monster_attacks[monster_id] = pending
        resolved = combat.resolve_due_monster_attacks(
            wire, now=pending.due_at, player_actor_id=1,
            block_chance=player_stats.block_chance,
            block_damage_reduction=player_stats.block_damage_reduction,
        )
        assert len(resolved) == 1
        blocked += int(resolved[0].blocked)
    observed = blocked / samples
    print(f"Block audit: {blocked}/{samples} hits blocked ({observed:.3%}); calculated {player_stats.block_chance:.5%}")
    assert abs(observed - 0.1414) < 0.015
