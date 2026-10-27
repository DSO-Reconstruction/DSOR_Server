"""Set ``SkillStatusEffect`` rules: one application, one roll, caster's sets only.

``_Template_ItemSet`` ``SkillStatusEffect:add|mod<UE|VE>,<skill>,<effect>``
rules add to, or modify, a skill's own User/Victim status columns.  The
item-set modifiers are part of every caster's skill modifier sources, so
``_modified_player_skill_geometry`` already carries each rule in the skill's
columns: every one of the 2,017 (rule, skill) pairs of the current data, with
the rule's chance (a ``mod`` on a skill without the entry stays a no-op).
The skill is therefore the single application and the single roll.  The set
proc path only supplies what a column cannot: an ``AuraTargets:Creator``
shell's payload for the wearer.  Hit transactions are built from the
caster's resolved effects, never from a viewer's equipment.
"""

from unittest.mock import patch

import pytest

from dso_server.gameplay.item_set_procs import collect_rules, rules_for_skill
from dso_server.gameplay.local_combat_state import DamageResolution
from dso_server.services.base import CharacterService
from dso_server.tests.test_black_dwarf_turrets import (  # noqa: F401 (fixtures)
    clock, equip, owner, populated, sets, skill_index,
)
from dso_server.tests.test_pvp_matchmaking import _session


def cast_user_effects(s, skill, trigger="skillstart"):
    """What player_combat (``skillstart``) or the impact path (``hit``)
    applies to the caster for one cast of ``skill``."""
    combat = s.gameplay_combat_state
    index = skill_index(skill)
    sources = tuple(CharacterService._active_skill_modifier_sources(s))
    return combat.player_skill_user_effects(
        index, cast_id=11, trigger=trigger, modifier_effect_ids=sources,
    ) + CharacterService._item_set_user_proc_effects(s, index, triggers=(trigger,))


def hit(s, skill, effects, *, source):
    combat = s.gameplay_combat_state
    target = combat.encounter.actor_ids[0]
    return DamageResolution(
        target_actor_id=target, attacker_actor_id=source, current_health=100,
        maximum_health=200, damage=10, killed=False,
        position=combat.monster_positions[target], server_tick=600, hit_tags=(3,),
        skill_index=skill_index(skill), victim_status_effects=tuple(effects),
        source_actor_id=source,
    )


def sent(transactions):
    return sorted(entry.effect_id for commands in transactions for command in commands
                  for entry in command.value.entries if entry.has_details)


def test_mod_user_rule_is_applied_once_per_cast_as_a_set_program(owner, sets):
    """CNY warrior: ``modUE,angrystrike,set_cny2026_warrior_earthquake_dmg``.

    Twenty stacks unlock the hidden skill; two applications per cast reached
    it after ten casts.
    """
    equip(owner, sets, 5, "set_cny2026_warrior", "warrior")
    at_cast = cast_user_effects(owner, "angrystrike")
    on_hit = cast_user_effects(owner, "angrystrike", "hit")
    name = "set_cny2026_warrior_earthquake_dmg"
    # angrystrike authors the entry ``ON:Hit``; the mod only enables it.
    assert [x.resource_id for x in at_cast if x.resource_id == name] == []
    stacks = [x for x in on_hit if x.resource_id == name]
    assert len(stacks) == 1
    CharacterService._send_player_skill_status_effects(
        owner, at_cast, server_tick=600, label="test-cast")
    CharacterService._send_player_skill_status_effects(
        owner, on_hit, server_tick=605, label="test-hit")
    (entry,) = [entry for entry, _ in owner.gameplay_runtime_status_effects.values()
                if entry.effect_id == stacks[0].effect_id]
    assert entry.field_18 == 0
    # Delivered by the skill, still run as the set's program (its stack cap,
    # children and unequip cleanup).
    assert entry.field_04 in owner.gameplay_item_set_programs


def test_mod_victim_rule_rolls_only_with_the_skill(owner, sets):
    """Black Dwarf mage: ``modVE,lightningstrike,..._trig_chain_effect,C:0.3``."""
    equip(owner, sets, 4, "set_blackdwarf2026_mage", "mage")
    combat = owner.gameplay_combat_state
    sources = tuple(CharacterService._active_skill_modifier_sources(owner))
    geometry = combat._modified_player_skill_geometry(
        combat._load_player_skill_geometry(skill_index("lightningstrike")), sources)
    chain = next(x for x in geometry.victim_status_effects
                 if x.resource_id == "SetLight2026_mage_trig_chain_effect")
    assert chain.chance == pytest.approx(0.3)
    # The skill's own per-target roll failed for this victim ...
    resolved = tuple(x for x in geometry.victim_status_effects
                     if x.chance >= 1.0 and x.effect_hosts != "Causer")
    resolution = hit(owner, "lightningstrike", resolved, source=owner.gameplay_remote_actor_id)
    # ... so no second, independent roll may add it to the transaction.
    with patch("dso_server.gameplay.item_set_procs.random.random", return_value=0.0):
        transactions = CharacterService._victim_effect_command_transactions(owner, (resolution,))
    assert chain.effect_id not in sent(transactions)
    assert sent(transactions) == sorted({x.effect_id for x in resolved})


def test_viewer_equipment_adds_nothing_to_another_players_hit(owner, sets):
    """Only the caster's own sets shape a hit, for every viewer's copy."""
    combat = owner.gameplay_combat_state
    caster_effects = combat._load_player_skill_geometry(
        skill_index("lightningstrike")).victim_status_effects
    resolved = tuple(x for x in caster_effects if x.chance >= 1.0)
    caster_id = 0x10000 + 999
    viewer = _session(52363, 363, "Viewer with set")
    viewer.gameplay_combat_state = combat
    viewer.gameplay_character.class_id = owner.gameplay_character.class_id
    equip(viewer, sets, 4, "set_blackdwarf2026_mage", "mage")
    resolution = hit(owner, "lightningstrike", resolved, source=caster_id)
    with patch("dso_server.gameplay.item_set_procs.random.random", return_value=0.0):
        seen_by_viewer = sent(CharacterService._victim_effect_command_transactions(
            viewer, (resolution,)))
        seen_by_owner = sent(CharacterService._victim_effect_command_transactions(
            owner, (resolution,)))
    assert seen_by_viewer == seen_by_owner == sorted({x.effect_id for x in resolved})


@pytest.mark.parametrize("set_id,class_name", [
    ("set_blackdwarf2026_dwarf", "dwarf"), ("set_blackdwarf2026_mage", "mage"),
    ("set_blackdwarf2026_ranger", "ranger"), ("set_blackdwarf2026_warrior", "warrior"),
    ("set_cny2026_warrior", "warrior"), ("set_cny2026_mage", "mage"),
    ("set_cny2026_dwarf", "dwarf"), ("chillingly_cold_mage", "mage"),
    ("set_dwarf_bgh_heredur", "dwarf"), ("PRset_dwarf", "dwarf"),
])
def test_the_modified_skill_carries_every_rule_the_procs_used_to_add(owner, sets, set_id, class_name):
    definition = next(x for x in sets.definitions if x.set_id == set_id)
    evaluation = sets.evaluate(tuple(p.split("|")[0] for p in definition.parts),
                               class_name=class_name)
    combat = owner.gameplay_combat_state
    rules = tuple(x for x in collect_rules(evaluation) if x.target in {"user", "victim"})
    assert rules
    checked = 0
    for rule in rules:
        index = skill_index(rule.skill_group) if rule.skill_group.casefold() != "dwarf_skills" else None
        if index is None:
            continue
        base = combat._load_player_skill_geometry(index)
        if base is None:
            continue
        geometry = combat._modified_player_skill_geometry(base, evaluation.modifiers)
        column = (geometry.user_status_effects if rule.target == "user"
                  else geometry.victim_status_effects)
        original = (base.user_status_effects if rule.target == "user"
                    else base.victim_status_effects)
        if rule.operation.casefold().startswith("mod") and not any(
                x.resource_id == rule.resource_id for x in original):
            continue
        assert rules_for_skill((rule,), skill_id=base.resource_id, skill_groups=base.groups)
        (carried,) = [x for x in column if x.resource_id == rule.resource_id]
        assert carried.chance == pytest.approx(min(1.0, rule.chance))
        checked += 1
    assert checked
