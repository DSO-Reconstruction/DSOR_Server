"""Mind Control, Singularity, Frost Wind and the Guardians, from live data."""

import math
import sqlite3
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.combat_protocol import SHIFTED_SKILL_COMMAND, ShiftedSkill
from dso_server.protocol.gameplay_protocol import StatusEffectEntry
from dso_server.services import player_combat
from dso_server.services.base import CharacterService
from dso_server.services.player_projectiles import (
    _location_effect_follows_hit, location_effect_waits_for_hit,
)
from dso_server.tests.test_combat_entities import MixedEntityCastTests
from dso_server.tests.test_pvp_matchmaking import _session


OWNER = 98305


def database():
    return sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True)


def skill(resource_id):
    return database().execute(
        "SELECT rowid-1 FROM _Template_Skill WHERE Id=?", (resource_id,),
    ).fetchone()[0]


def effect(resource_id):
    row = database().execute(
        "SELECT rowid-1 FROM _Template_StatusEffect WHERE Id=?", (resource_id,),
    ).fetchone()
    return StatusEffectEntry(
        effect_id=row[0], field_04=1, field_0c=0, field_14=25, field_08=0, field_18=0,
        field_10=0, field_1c=100, field_20=1, field_25=False, field_26=False,
        field_39=True, has_details=True, values=(0.5,),
    )


@pytest.fixture(scope="module")
def encounter():
    return load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=True)


@pytest.fixture
def combat(encounter):
    return LocalCombatState(encounter, player_actor_id=OWNER, player_health=1000)


@pytest.mark.parametrize("resource_id,bonus", [
    ("mindcontrol", "fp_buff_s5_player_physical_dmg"),
    ("arcanevortex", "fp_buff_s5_player_physical_dmg"),
    ("frostwind", "fp_buff_s4_player_ice_dmg"),
])
def test_a_flat_element_bonus_gives_a_status_only_skill_no_damage(combat, resource_id, bonus):
    geometry = combat._load_player_skill_geometry(skill(resource_id))
    assert geometry.damage_modifier == 0.0
    assert combat._modified_player_skill_geometry(geometry, (effect(bonus),)).damage_modifier == 0.0


def test_a_flat_element_bonus_still_raises_a_damaging_skill(combat):
    geometry = combat._load_player_skill_geometry(skill("magicmissile"))
    modified = combat._modified_player_skill_geometry(
        geometry, (effect("fp_buff_s5_player_physical_dmg"),))
    assert modified.damage_modifier > geometry.damage_modifier


def _location_visual(combat, skill_id, resource_id, modifiers=()):
    geometry = combat._load_player_skill_geometry(skill(skill_id))
    geometry = combat._modified_player_skill_geometry(geometry, modifiers)
    return next(
        item for item in geometry.location_visual_effects if item.resource_id == resource_id
    ), geometry


def test_singularity_stays_with_the_cast_instead_of_following_a_hit(combat):
    visual, geometry = _location_visual(combat, "arcanevortex", "skill_arcanevortex_aura")
    assert visual.trigger == "hit"
    assert not location_effect_waits_for_hit(visual, geometry)
    assert not _location_effect_follows_hit(visual, geometry, 0.0, 1.0)


def test_an_earthquake_opens_around_its_caster(combat):
    visual, geometry = _location_visual(combat, "earthquake", "skill_earthquake_aura")
    assert not location_effect_waits_for_hit(visual, geometry)
    assert not _location_effect_follows_hit(visual, geometry, 0.0, 1.0)


def test_deadly_blow_explodes_on_the_monster_it_struck(combat):
    visual, geometry = _location_visual(
        combat, "mightystab", "ranger_talent_mightystab_explosion_aura",
        (effect("ranger_talent_mightystab_explosion"),),
    )
    assert location_effect_waits_for_hit(visual, geometry)
    assert _location_effect_follows_hit(visual, geometry, 0.0, 0.0)


def test_a_bullets_talent_location_effect_lands_on_impact(combat):
    visual, geometry = _location_visual(combat, "explosiveshot", "skill_explosiveshot_explosion_aura")
    assert _location_effect_follows_hit(visual, geometry, 12.0, 0.0)
    assert not _location_effect_follows_hit(visual, geometry, 12.0, 1.0)


class SingularityCastTests(unittest.TestCase):
    AIMED = (25.0, 0.0, 58.0)

    def setUp(self):
        fixture = MixedEntityCastTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.source, self.combat, self.monster = fixture.source, fixture.combat, fixture.monster
        self.source.gameplay_skill_book_ids = {skill("arcanevortex")}

    def place_monster(self, position):
        self.combat.monster_positions[self.monster] = position
        self.source.gameplay_delivered_monster_positions[self.monster] = position

    def cast_singularity(self):
        client_skill = ShiftedSkill(0, skill("arcanevortex"), 0.0, 200, 88, 1, self.AIMED)
        stats = SimpleNamespace(
            minimum_damage=10.0, maximum_damage=10.0, critical_hit_chance=0.0,
            critical_damage_multiplier=2.0, attack_speed=1.0, skill_modifier_sources=(),
        )
        with (
            patch.object(player_combat, "decode_shifted_skill", return_value=client_skill),
            patch.object(player_combat, "calculate_player_combat_stats", return_value=stats),
            patch.object(CharacterService, "_apply_player_skill_resource", return_value=True),
            patch.object(CharacterService, "_start_monster_chase"),
            patch.object(CharacterService, "_record_monster_targeting_result"),
            patch.object(CharacterService, "_send_remote_player_skill"),
            patch.object(CharacterService, "_schedule_player_location_visuals") as placed,
        ):
            CharacterService._handle_spatial_skill(
                self.source, b"\x8b" + SHIFTED_SKILL_COMMAND.to_bytes(2, "little"))
        return placed

    def assert_placed_where_aimed(self, placed):
        placed.assert_called_once()
        arguments = placed.call_args
        self.assertEqual(
            [item.resource_id for item in arguments.args[2]], ["skill_arcanevortex_aura"])
        self.assertEqual(arguments.kwargs["position"], self.AIMED)
        self.assertEqual(
            {item.resource_id for item in arguments.kwargs["receiver_status_effects"]},
            {"skill_arcanevortext_debuff_movementspeed", "skill_arcanevortext_debuff_attackspeed",
             "skill_arcanevortext_debuff_resistance_physical",
             "skill_arcanevortext_debuff_resist_elemantal"},
        )

    def test_it_opens_where_aimed_beside_a_monster(self):
        self.place_monster((27.5, 0.0, 58.0))
        self.assert_placed_where_aimed(self.cast_singularity())
        self.assertTrue(self.combat.pending_runtime_player_hits)

    def test_it_opens_where_aimed_with_nothing_in_reach(self):
        self.place_monster((100.0, 0.0, 100.0))
        self.assert_placed_where_aimed(self.cast_singularity())
        self.assertFalse(self.combat.pending_runtime_player_hits)

    def test_a_partial_chance_location_effect_rolls_at_the_cast(self):
        self.place_monster((100.0, 0.0, 100.0))
        visuals = self.combat.player_skill_location_visuals(skill("arcanevortex"))
        unlikely = tuple(replace(visual, chance=1e-9) for visual in visuals)
        with patch.object(self.combat, "player_skill_location_visuals", return_value=unlikely):
            self.cast_singularity().assert_not_called()


def _frost_trigger(combat):
    geometry = combat._modified_player_skill_geometry(
        combat._load_player_skill_geometry(skill("frostwind")),
        (effect("mage_talent_frozen_master"),),
    )
    return next(item for item in geometry.victim_status_effects
                if item.resource_id == "skill_frostwind_frost_explosion_trigger")


def test_frost_wind_arms_its_explosion_for_two_seconds(combat):
    trigger = _frost_trigger(combat)
    assert trigger.chance == 1.0
    assert trigger.delay_ticks == 50
    (aura, _vfx) = combat.victim_effect_location_children(trigger)
    assert aura[0].resource_id == "skill_frostwind_frost_explosion_aura"
    assert aura[0].aura_radius == 3.0
    assert {item.resource_id for item in aura[1]} >= {"skill_frostwind_frost_explosion_effect"}


def test_the_frost_explosion_deals_ice_damage(combat):
    (aura, _vfx) = combat.victim_effect_location_children(_frost_trigger(combat))
    actor = next(iter(combat.living_actor_ids))
    receivers = tuple(item for item in aura[1]
                      if item.resource_id == "skill_frostwind_frost_explosion_effect")
    damage, _tags = combat.status_effect_immediate_damage(actor, receivers, (1000.0, 1000.0), now=100.0)
    assert damage > 0
    (coefficient, damage_type, *_rest) = combat._status_damage_programs(receivers[0], ticks=False)[0]
    assert coefficient == pytest.approx(0.5)
    assert damage_type.casefold() == "ice"


def _caster(combat):
    s = _session(52471, 471, "Frost")
    s.gameplay_combat_state = combat
    s.gameplay_remote_actor_id = OWNER
    s.gameplay_presence_callback = None
    s.timers = []

    def timer(delay, function, args=(), kwargs=None):
        value = SimpleNamespace(delay=delay, function=function, args=args, kwargs=kwargs or {},
                                start=Mock(), cancel=Mock(), daemon=False)
        s.timers.append(value)
        return value
    s.gameplay_deadline_timer = timer
    return s


def _resolution(combat, effects, *, killed=False):
    actor = next(iter(combat.living_actor_ids))
    return SimpleNamespace(
        source_actor_id=OWNER, attacker_actor_id=OWNER, target_actor_id=actor,
        victim_status_effects=effects, killed=killed, damage_immune=False,
        server_tick=2000, skill_index=skill("frostwind"),
        position=combat.monster_positions[actor],
    )


def test_the_explosion_goes_off_where_the_victim_stands_after_its_delay(combat):
    s = _caster(combat)
    trigger = _frost_trigger(combat)
    hit = _resolution(combat, (trigger,))
    CharacterService._apply_victim_location_effects(s, (hit,))
    (timer,) = s.timers
    assert timer.delay == pytest.approx(2.0)
    actor = hit.target_actor_id
    moved = (10.0, combat.monster_positions[actor][1], 20.0)
    combat.monster_positions[actor] = moved
    with patch.object(CharacterService, "_schedule_player_location_visuals") as place:
        timer.function(*timer.args, **timer.kwargs)
    assert place.call_count == 2
    first = place.call_args_list[0]
    assert first.kwargs["position"] == moved
    assert first.kwargs["delay_seconds"] == 0.0
    assert first.args[2][0].resource_id == "skill_frostwind_frost_explosion_aura"


def test_the_explosion_follows_keep_on_death(combat):
    s = _caster(combat)
    trigger = _frost_trigger(combat)
    keeps = database().execute(
        "SELECT KeepOnDeath FROM _Template_StatusEffect WHERE Id=?", (trigger.resource_id,),
    ).fetchone()[0]
    CharacterService._apply_victim_location_effects(
        s, (_resolution(combat, (trigger,), killed=True),))
    assert bool(s.timers) == bool(keeps)


def _summon(combat, resource_id):
    definition = combat.player_summon_definition(skill(resource_id))
    actor = next(iter(combat.living_actor_ids))
    target = combat.monster_positions[actor]
    return combat.spawn_player_summon(
        definition, owner_actor_id=OWNER, position=(target[0] - 8.0, target[1], target[2]),
        yaw=0.0, level=100, server_tick=550, player_damage_range=(100.0, 100.0), now=100.0,
    )


@pytest.mark.parametrize("resource_id", ["arcanemanifestation", "firemanifestation", "packofwolves"])
def test_a_living_minion_ends_with_its_owner(combat, resource_id):
    s = _caster(combat)
    spawn = _summon(combat, resource_id)
    with patch.object(CharacterService, "_expire_player_summon") as expire:
        CharacterService._expire_summons_with_owner(s)
    assert {call.args[3] for call in expire.call_args_list} == {
        actor.actor_id for actor in spawn.actors}


def test_a_turret_keeps_its_own_leash(combat):
    s = _caster(combat)
    _summon(combat, "TurretCombat")
    with patch.object(CharacterService, "_expire_player_summon") as expire:
        CharacterService._expire_summons_with_owner(s)
    expire.assert_not_called()


def test_another_players_guardian_stays(combat):
    s = _caster(combat)
    _summon(combat, "arcanemanifestation")
    s.gameplay_remote_actor_id = OWNER + 1
    with patch.object(CharacterService, "_expire_player_summon") as expire:
        CharacterService._expire_summons_with_owner(s)
    expire.assert_not_called()


@pytest.mark.parametrize("resource_id", ["arcanemanifestation", "firemanifestation"])
def test_guardians_shoot_on_their_cooldown(combat, resource_id):
    assert combat.player_summon_definition(skill(resource_id)).attack_interval_seconds == 1.0


def _first_shot(combat, resource_id):
    spawn = _summon(combat, resource_id)
    actor = spawn.actors[0].actor_id
    summon = combat.player_summons[actor]
    attacks = combat.maybe_start_player_summon_attacks(
        {}, server_tick=600, now=summon.attack_ready_at + 0.01,
    )
    return summon, next(attack for attack in attacks if attack.attacker_actor_id == actor)


def test_the_guardian_aims_its_bullet_from_the_muzzle(combat):
    summon, attack = _first_shot(combat, "arcanemanifestation")
    geometry = combat._load_player_skill_geometry(attack.skill_index)
    side, _height, ahead = geometry.start_offset[:3]
    sine, cosine = math.sin(attack.aim_angle), math.cos(attack.aim_angle)
    muzzle = (summon.position[0] + side * cosine + ahead * sine,
              summon.position[2] + ahead * cosine - side * sine)
    target = attack.target_position
    to_target = (target[0] - muzzle[0], target[2] - muzzle[1])
    across = to_target[0] * attack.direction[2] - to_target[1] * attack.direction[0]
    assert abs(across) < 1e-4
