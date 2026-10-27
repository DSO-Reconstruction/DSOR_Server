"""Hit transactions: Causer-hosted effects and authored extra occurrences.

* ``EffectHosts:Causer`` rows in a skill's victim column are the hitter's own
  on-hit buffs (``skill_mightystab_buff_attackspeed``, Quick Shot's Black
  Dwarf Overload ``SetLight2026_dwarf_SimpleShot_trig``).  They are hosted by
  the caster, never sent to the victim.
* Tesla's overcharged ``tesla_charge_add_SetLight2026`` doubles Electrified by
  authoring ``ActorStatusEffect:addAE,debuff_cc_charge,...,ON:Immediate`` in
  its own StartModifiers.  The client's ActorStatusEffect modifier creates
  such immediate children itself when the effect starts (apply routine at
  0x1408ffef8: trigger 0x38/0x39 -> chance roll -> 0x1409008b8
  ``CreateImmediateActorEffect``), the same routine that derives max-stack
  children for monsters.  The server therefore delivers the parent alongside
  the direct stack in the same transaction, and keeps one advance per effect
  per cast for every other impact of that cast.
"""

from unittest.mock import patch

import pytest

from dso_server.gameplay.local_combat_state import DamageResolution
from dso_server.services.base import CharacterService
from dso_server.tests.test_black_dwarf_turrets import (  # noqa: F401 (fixtures)
    anchor, attack_geometry, cast, clock, equip, owner, populated, sets, skill_index, sync,
)


def resolution(s, target, effects, *, source=None, tick=600, cast_id=0):
    combat = s.gameplay_combat_state
    return DamageResolution(
        target_actor_id=target, attacker_actor_id=source or s.gameplay_remote_actor_id,
        current_health=100, maximum_health=200, damage=10, killed=False,
        position=combat.monster_positions[target], server_tick=tick, hit_tags=(0,),
        skill_index=0, victim_status_effects=tuple(effects),
        source_actor_id=source or s.gameplay_remote_actor_id, cast_id=cast_id,
    )


def sent_effect_ids(transactions):
    return [entry.effect_id for commands in transactions for command in commands
            for entry in command.value.entries if entry.has_details]


def test_causer_hosted_hit_buff_goes_to_the_caster_once_and_never_to_the_victim(owner):
    combat = owner.gameplay_combat_state
    geometry = combat._load_player_skill_geometry(skill_index("mightystab"))
    buff = next(x for x in geometry.victim_status_effects
                if x.resource_id == "skill_mightystab_buff_attackspeed")
    assert buff.effect_hosts == "Causer" and buff.chance == 1.0
    targets = combat.encounter.actor_ids[:2]
    hits = tuple(resolution(owner, target, geometry.victim_status_effects)
                 for target in targets)
    transactions = CharacterService._victim_effect_command_transactions(owner, hits)
    assert buff.effect_id not in sent_effect_ids(transactions)
    with patch.object(CharacterService, "_send_player_skill_status_effects") as caster:
        CharacterService._send_combat_resolutions(owner, hits, label="test-hit")
    (call,) = caster.call_args_list
    assert [x.resource_id for x in call.args[1]] == ["skill_mightystab_buff_attackspeed"]

    # Another player watching the same hit hosts nothing.
    from dso_server.tests.test_pvp_matchmaking import _session
    viewer = _session(52362, 362, "Viewer")
    viewer.gameplay_combat_state = combat
    viewer.send_reliable_ordered = owner.send_reliable_ordered
    with patch.object(CharacterService, "_send_player_skill_status_effects") as caster:
        CharacterService._send_combat_resolutions(viewer, hits, label="test-hit")
    caster.assert_not_called()


def test_quick_shot_overload_is_hosted_once_on_the_shooter_as_a_set_program(owner, sets):
    equip(owner, sets, 4)
    combat = owner.gameplay_combat_state
    sources = tuple(CharacterService._active_skill_modifier_sources(owner))
    geometry = combat._modified_player_skill_geometry(
        combat._load_player_skill_geometry(skill_index("SimpleShot")), sources)
    overload = next(x for x in geometry.victim_status_effects
                    if x.resource_id == "SetLight2026_dwarf_SimpleShot_trig")
    assert overload.chance == pytest.approx(0.4)
    hit = resolution(owner, combat.encounter.actor_ids[0], (overload,))
    assert overload.effect_id not in sent_effect_ids(
        CharacterService._victim_effect_command_transactions(owner, (hit,)))
    # Quick Shot's own column rolled the authored 40% for this hit; the
    # shooter hosts it once, as the set's program, so its Overload stack
    # children (10/20/30) follow on the server.
    CharacterService._send_combat_resolutions(owner, (hit,), label="test-hit")
    running = {entry.effect_id: instance_id
               for instance_id, (entry, _) in owner.gameplay_runtime_status_effects.items()}
    assert running[overload.effect_id] in owner.gameplay_item_set_programs
    counters = [x for x in owner.gameplay_item_set_programs.values()
                if x.resource_id.startswith("SetLight2026_dwarf_SimpleShot_")
                and x.resource_id != "SetLight2026_dwarf_SimpleShot_trig"]
    assert sorted(x.resource_id for x in counters) == [
        "SetLight2026_dwarf_SimpleShot_10", "SetLight2026_dwarf_SimpleShot_20",
        "SetLight2026_dwarf_SimpleShot_30"]
    # A second hit transaction adds one stack, not two.
    CharacterService._send_combat_resolutions(owner, (hit,), label="test-hit")
    (entry,) = [entry for entry, _ in owner.gameplay_runtime_status_effects.values()
                if entry.effect_id == overload.effect_id]
    assert entry.field_18 == 1


def test_overcharged_tesla_delivers_the_authored_second_electrified_occurrence(owner, sets, clock):
    equip(owner, sets, 3)
    start = clock.value
    tesla = cast(owner, "TurretTesla", anchor(owner), 5)
    sync(owner, clock, start + 8.0)
    geometry = attack_geometry(owner, tesla, start + 8.5)
    effects = tuple(x for x in geometry.victim_status_effects if x.chance >= 1.0)
    names = {x.resource_id for x in effects}
    assert {"debuff_cc_charge", "debuff_cc_charge_resis2026",
            "tesla_charge_add_SetLight2026"} <= names
    target = owner.gameplay_combat_state.encounter.actor_ids[0]
    ids = {x.resource_id: x.effect_id for x in effects}
    # Two impacts of one cast on the same monster: the first carries the
    # direct stack and the parent whose immediate children are the second
    # stack; the second impact advances nothing again (once per cast).
    hits = (resolution(owner, target, effects, cast_id=77),
            resolution(owner, target, effects, cast_id=77))
    first, second = CharacterService._victim_effect_command_transactions(owner, hits)
    sent = sent_effect_ids((first,))
    for name in ("debuff_cc_charge", "debuff_cc_charge_resis2026", "tesla_charge_add_SetLight2026"):
        assert sent.count(ids[name]) == 1
    assert sent_effect_ids((second,)) == []
