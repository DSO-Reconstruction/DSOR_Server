"""The ranger's Wisdom combat talents fire on the events their tooltips name.

Strategic Advantage, Reckless Haste and Second Chance (``trait_wisdom_combat``,
one pick) could be bought and did nothing: their rules name ``HitStunned``,
``KillMarked`` and ``NoHealth``, and no code raised those.
"""

from dataclasses import replace
import math
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pytest

from dso_server.gameplay.local_combat_state import (
    DamageResolution, LocalCombatState, PendingPlayerHit,
)
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.gameplay.player_combat_stats import (
    CombatModifier, calculate_player_combat_stats,
)
from dso_server.gameplay.player_skill_timing import player_skill_timing_state
from dso_server.gameplay.pvp_match_state import (
    MATCH_MODE_BEST_OF_3_1V1, MatchParticipant, MatchPhase, PVPMatch,
)
from dso_server.gameplay.status_modifier_runtime import (
    evaluate_active_status_modifiers, load_status_modifier_catalog,
    snapshot_player_modifier_sources,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence import readonly_db
from dso_server.protocol.character_selection import LOCAL_CHARACTER_ID_BASE
from dso_server.protocol.combat_protocol import BulletSkill, KillServerCommand
from dso_server.protocol.gameplay_commands import LOCAL_PLAYER_ACTOR_ID
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.player_init_protocol import (
    TalentCategorySelection, TalentSelectionEntry,
)
from dso_server.protocol.talent_protocol import TalentDatabase
from dso_server.services import pvp_combat, talent_events
from dso_server.services.base import CharacterService
from dso_server.services.item_set_programs import _apply_terminal_health
from dso_server.services.talent_events import (
    dispatch_incoming_hit, dispatch_kill_talents, dispatch_talent_event,
    route_talent_user_effects, spare_lethal_hits, spare_player,
)
from dso_server.tests.fixtures.characters import character_server
from dso_server.tests.test_black_dwarf_turrets import (  # noqa: F401
    anchor, cast, clock, owner, populated,
)
from dso_server.tests.test_item_set_persistence import initialize, save
from dso_server.tests import test_populated_map_simulation as populated_map
from dso_server.tests import test_pvp_player_effects as pvp_effects
from dso_server.tests.test_pvp_matchmaking import _session
from dso_server.tests.test_ranger_projectile_hits import combat, skill_id  # noqa: F401


STRATEGIC_ADVANTAGE = "trait_wisdom_ranger_hitStunned_Trigger"
RECKLESS_HASTE = "trait_wisdom_ranger_killMarked_Trigger"
SECOND_CHANCE = "trait_wisdom_all_death_Trigger"
HASTE_COOLDOWN = "trait_wisdom_ranger_killMarked_trigger_effect_cooldown"
DEATH_COOLDOWN = "trait_wisdom_all_death_trigger_effect_cooldown"
TALENTS = TalentDatabase(DEFAULT_STATIC_DB_PATH)


def effect(entry):
    return LocalCombatState._load_victim_status_effects(
        readonly_db.connect(DEFAULT_STATIC_DB_PATH), entry,
    )[0]


def talent_entries(talent_id, *, first_instance_id=1):
    return TALENTS.status_effect_entries(
        TALENTS.definition(talent_id), target_rank=1,
        actor_id=LOCAL_PLAYER_ACTOR_ID, first_instance_id=first_instance_id,
        server_tick=0,
    )


def learn(session, talent_id):
    for entry in talent_entries(
        talent_id, first_instance_id=session.gameplay_next_status_effect_instance_id,
    ):
        session.gameplay_persistent_status_effects[entry.instance_id] = entry
        session.gameplay_next_status_effect_instance_id = entry.instance_id + 1


def shoot(state, *, sources=(), resource="markshot", prepare=None):
    """One real arrow at a 100 base damage: returns the resolved hit."""
    actor = state.encounter.actor_ids[0]
    state.monster_positions[actor] = (0.0, 2.0, -5.0)
    state.monster_health[actor] = 1_000_000
    if prepare is not None:
        prepare(actor)
    pending = state.start_bullet_skill_hits(
        BulletSkill(0, skill_id(resource), 0.0, 2000, 321, (0.0, 0.0, -1.0, 0.0), 0),
        state.encounter.coordinate_frame.world_to_wire((0.0, 2.0, 0.0)),
        source_actor_id=1, now=100.0, eligible_actor_ids=(actor,),
        player_damage_range=(100.0, 100.0), modifier_effect_ids=tuple(sources),
    )
    return state.resolve_runtime_player_hit(1, 321, 0, now=pending[0].due_at)


def stunned_by(state, *entries):
    return lambda actor: state._record_monster_status_effects(
        actor, tuple(effect(value) for value in entries), now=100.0,
    )


# Strategic Advantage


def test_strategic_advantage_adds_half_base_damage_to_a_stunned_target(combat):
    plain = shoot(combat, prepare=stunned_by(combat, "debuff_cc_stun,C:1.0,D:5.0"))
    talented = shoot(
        combat, sources=talent_entries(STRATEGIC_ADVANTAGE),
        prepare=stunned_by(combat, "debuff_cc_stun,C:1.0,D:5.0"),
    )
    assert plain.damage == 100
    # The tooltip's 50% of base damage, as extra physical damage.
    assert talented.damage == 150
    assert "trait_wisdom_ranger_hitStunned_trigger_effect" in {
        value.resource_id for value in talented.victim_status_effects
    }


def test_strategic_advantage_needs_a_stunned_target(combat):
    hit = shoot(combat, sources=talent_entries(STRATEGIC_ADVANTAGE))
    assert hit.damage == 100
    assert not hit.victim_status_effects or all(
        "hitStunned" not in value.resource_id for value in hit.victim_status_effects
    )


def test_stun_immunity_shares_the_stun_group_but_is_not_a_stun(combat):
    immunity = effect("debuff_cc_stun_immunity,C:1.0,D:3.0")
    actor = combat.encounter.actor_ids[0]
    assert "stun" in combat._status_groups_for_effect(immunity.effect_id)
    hit = shoot(
        combat, sources=talent_entries(STRATEGIC_ADVANTAGE),
        prepare=lambda target: combat._record_monster_status_effects(
            target, (immunity,), now=100.0,
        ),
    )
    assert actor in combat.monsters and hit.damage == 100


def test_a_stunning_shot_does_not_cash_in_its_own_stun(combat):
    sources = talent_entries(STRATEGIC_ADVANTAGE)
    first = shoot(combat, sources=sources, resource="stunshot")
    assert "debuff_cc_stun" in {value.resource_id for value in first.victim_status_effects}
    assert "trait_wisdom_ranger_hitStunned_trigger_effect" not in {
        value.resource_id for value in first.victim_status_effects
    }
    # The stun it left is what the next arrow pays off.
    follow_up = shoot(combat, sources=sources)
    assert follow_up.damage == 150


def test_strategic_advantage_also_counts_stunned_players():
    fixture = pvp_effects.PVPPlayerEffectTests("test_lethal_impact_uses_current_world_position_on_both_clients")
    fixture.setUp()
    try:
        bonus = fixture.effect(
            "trait_wisdom_ranger_hitStunned_trigger_effect,C:1.0,D:1.0,DP:1.0,"
            "$0:trait_wisdom_ranger_hitStunned_dmg,ON:HitStunned"
        )
        stats = SimpleNamespace(incoming_damage_multiplier=lambda _kind: 0.5)
        with patch.object(
            CharacterService, "_pvp_damage_type_multiplier", return_value=0.5,
        ):
            assert CharacterService._pvp_stunned_hit_damage(
                fixture.combat, (bonus,), (100.0, 100.0), stats,
            ) == 25
            assert CharacterService._pvp_stunned_hit_damage(
                fixture.combat,
                (fixture.effect("debuff_cc_stun,C:1.0,D:5.0"),),
                (100.0, 100.0), stats,
            ) == 0
    finally:
        fixture.doCleanups()


# Reckless Haste and Second Chance run on a real session.


@pytest.fixture
def ranger():
    session = _session(52271, 271, "Ranger")
    session.gameplay_character.class_id = 2
    state = LocalCombatState(
        load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False,
        ),
        player_actor_id=session.gameplay_remote_actor_id, player_health=1000,
    )
    state.register_player(
        session.gameplay_remote_actor_id, 1000,
        maximum_resource=100, current_resource=0.0,
    )
    session.gameplay_combat_state = state
    session.gameplay_inventory = InventoryInfo(health_points=1000, secondary_resource=0)
    session.gameplay_chase_server_tick = 200
    session.gameplay_presence_callback = None
    session.gameplay_last_move = None
    session.gameplay_spawn_position = (0.0, 2.0, 0.0)
    session.gameplay_deadline_timer = lambda delay, callback, args=(), kwargs=None: SimpleNamespace(
        start=Mock(), cancel=Mock(), daemon=False, delay=delay,
        callback=callback, args=args, kwargs=kwargs or {},
    )
    with patch.object(CharacterService, "_start_monster_chase"), \
            patch.object(CharacterService, "_request_account_save"):
        yield session


def marked_monster(session):
    state = session.gameplay_combat_state
    actor = state.encounter.actor_ids[0]
    state._record_monster_status_effects(
        actor, (effect("debuff_cc_mark,C:1.0,D:8.0"),), now=time.perf_counter(),
    )
    return actor


def kill_of(actor, *, marked=False):
    return DamageResolution(
        target_actor_id=actor, attacker_actor_id=1, current_health=0,
        maximum_health=100, damage=100, killed=True, position=(0.0, 2.0, 0.0),
        server_tick=500, hit_tags=(0,), skill_index=0, source_actor_id=1,
        target_marked=marked,
    )


def names(definitions):
    return {value.resource_id for value in definitions}


def running(session):
    catalog = session.gameplay_combat_state
    return {
        entry.effect_id for entry, _expires in session.gameplay_runtime_status_effects.values()
    }, catalog


def cooldown_instance(session, resource):
    wire_id = TALENTS.status_effect_wire_id(resource)
    return next(
        instance_id
        for instance_id, entry in session.gameplay_persistent_status_effects.items()
        if entry.effect_id == wire_id
    )


def five_minutes_pass(session, instance_id):
    """Everything short-lived has ended; only the cooldown's timer ran late.

    The started effect and the buffs share the cooldown's Deny group, and they
    hold the talent back too while they run.
    """

    session.gameplay_runtime_status_effects.clear()
    session.gameplay_timed_status_effect_expiries[instance_id] = time.time() - 1.0


def test_defeating_a_marked_enemy_grants_reckless_haste(ranger):
    learn(ranger, RECKLESS_HASTE)
    started = dispatch_kill_talents(ranger, (kill_of(marked_monster(ranger)),))
    assert names(started) == {
        "trait_wisdom_ranger_killMarked_trigger_effect",
        "trait_wisdom_ranger_killMarked_trigger_buff_movspeed",
        "trait_wisdom_ranger_killMarked_trigger_buff_atkspeed",
        HASTE_COOLDOWN,
    }
    by_name = {value.resource_id: value for value in started}
    # The tooltip's five seconds, and "every 300 seconds".
    assert by_name["trait_wisdom_ranger_killMarked_trigger_buff_movspeed"].duration_ticks == 125
    assert by_name["trait_wisdom_ranger_killMarked_trigger_buff_atkspeed"].duration_ticks == 125
    assert by_name[HASTE_COOLDOWN].duration_ticks == 7500
    assert ranger.recorder.messages


def test_reckless_haste_raises_attack_and_movement_speed(ranger):
    learn(ranger, RECKLESS_HASTE)
    dispatch_kill_talents(ranger, (kill_of(marked_monster(ranger)),))
    live = talent_events._live_sources(ranger)

    def stats(sources):
        return calculate_player_combat_stats(
            DEFAULT_STATIC_DB_PATH, class_name="ranger", level=100,
            inventory=InventoryInfo(), modifier_sources=sources,
        )

    before, after = stats(()), stats(live)
    assert after.attack_speed == pytest.approx(before.attack_speed * 1.2, rel=1e-3)
    assert after.movement_speed == pytest.approx(before.movement_speed * 1.1, rel=1e-3)


@pytest.mark.parametrize("talent_id", (None, STRATEGIC_ADVANTAGE, SECOND_CHANCE))
def test_reckless_haste_belongs_to_its_own_talent(ranger, talent_id):
    if talent_id:
        learn(ranger, talent_id)
    assert dispatch_kill_talents(ranger, (kill_of(marked_monster(ranger)),)) == ()
    assert not ranger.gameplay_runtime_status_effects


def test_an_unmarked_kill_grants_nothing(ranger):
    learn(ranger, RECKLESS_HASTE)
    actor = ranger.gameplay_combat_state.encounter.actor_ids[0]
    assert dispatch_kill_talents(ranger, (kill_of(actor),)) == ()
    assert not ranger.gameplay_runtime_status_effects


def test_the_killing_blows_own_mark_does_not_count(ranger):
    """A lethal hit installs nothing, so a mark shot's kill stays unmarked."""
    learn(ranger, RECKLESS_HASTE)
    state = ranger.gameplay_combat_state
    state._bullet_static_reach = lambda _offset: None
    actor = state.encounter.actor_ids[0]
    state.monster_positions[actor] = (0.0, 2.0, -5.0)
    state.monster_health[actor] = 1
    source = ranger.gameplay_remote_actor_id
    pending = state.start_bullet_skill_hits(
        BulletSkill(0, skill_id("markshot"), 0.0, 2000, 322, (0.0, 0.0, -1.0, 0.0), 0),
        state.encounter.coordinate_frame.world_to_wire((0.0, 2.0, 0.0)),
        source_actor_id=source, now=100.0, eligible_actor_ids=(actor,),
        player_damage_range=(100.0, 100.0),
    )
    hit = state.resolve_runtime_player_hit(source, 322, 0, now=pending[0].due_at)
    assert hit.killed
    assert dispatch_kill_talents(ranger, (hit,)) == ()


def test_a_lethal_dot_tick_on_a_marked_enemy_counts(ranger):
    learn(ranger, RECKLESS_HASTE)
    state = ranger.gameplay_combat_state
    actor = state.encounter.actor_ids[0]
    now = time.perf_counter()
    state.monster_health[actor] = 1
    state._record_monster_status_effects(
        actor,
        (effect("debuff_cc_mark,C:1.0,D:8.0"), effect("debuff_dot_bleeding,C:1.0,D:5.0")),
        now=now, source_actor_id=ranger.gameplay_remote_actor_id,
        player_damage_range=(100.0, 100.0), skill_index=0,
    )
    ticks = state.resolve_due_monster_status_damage(now=now + 2.0, server_tick=600)
    lethal = next(value for value in ticks if value.killed)
    assert lethal.target_marked
    # The tick cleared what it could have been read from.
    assert not state.monster_has_status_group(actor, ("mark",), now=now + 2.0)
    assert HASTE_COOLDOWN in names(dispatch_kill_talents(ranger, (lethal,)))


def test_reckless_haste_waits_out_its_cooldown(ranger):
    learn(ranger, RECKLESS_HASTE)
    actor = marked_monster(ranger)
    assert dispatch_kill_talents(ranger, (kill_of(actor),))
    assert dispatch_kill_talents(ranger, (kill_of(actor),)) == ()
    instance_id = cooldown_instance(ranger, HASTE_COOLDOWN)
    expires_at = ranger.gameplay_timed_status_effect_expiries[instance_id]
    assert expires_at - time.time() == pytest.approx(300.0, abs=2.0)
    # The deadline itself rules; a late timer must not extend the cooldown.
    five_minutes_pass(ranger, instance_id)
    assert dispatch_kill_talents(ranger, (kill_of(actor),))


def test_zoning_does_not_reset_the_cooldown(ranger):
    """A map change clears the runtime store; the cooldown lives elsewhere."""
    learn(ranger, RECKLESS_HASTE)
    actor = marked_monster(ranger)
    dispatch_kill_talents(ranger, (kill_of(actor),))
    instance_id = cooldown_instance(ranger, HASTE_COOLDOWN)
    assert instance_id not in ranger.gameplay_runtime_status_effects

    ranger.gameplay_runtime_status_effects.clear()
    ranger.gameplay_projectile_timers.clear()
    assert dispatch_kill_talents(ranger, (kill_of(actor),)) == ()

    # The expiry follows the player: the new map schedules it again.
    server = SimpleNamespace(static_database_path=DEFAULT_STATIC_DB_PATH)
    CharacterService._restore_consumable_timers(server, ranger)
    timer = ranger.gameplay_projectile_timers[("consumable-expire", instance_id)]
    assert timer.delay == pytest.approx(300.0, abs=2.0)
    with patch.object(CharacterService, "_refresh_player_combat_vitals"):
        timer.callback(*timer.args)
    assert instance_id not in ranger.gameplay_persistent_status_effects
    assert instance_id not in ranger.gameplay_timed_status_effect_expiries


def test_the_cooldown_survives_a_real_save_and_relog():
    case = unittest.TestCase()
    try:
        server = character_server(case)
        session, _ = initialize(server)
        learn(session, RECKLESS_HASTE)
        session.gameplay_combat_state = LocalCombatState(
            load_local_monster_encounter(
                DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False,
            ),
            player_actor_id=session.gameplay_remote_actor_id, player_health=10000,
        )
        with patch.object(CharacterService, "_request_account_save"):
            assert dispatch_kill_talents(session, (kill_of(marked_monster(session)),))
        saved_expiry = session.gameplay_timed_status_effect_expiries[
            cooldown_instance(session, HASTE_COOLDOWN)
        ]
        server.account_store.put(save(server, session), flush=True)

        restored, _ = initialize(server)
        instance_id = cooldown_instance(restored, HASTE_COOLDOWN)
        assert restored.gameplay_timed_status_effect_expiries[instance_id] == pytest.approx(saved_expiry)
        entry = restored.gameplay_persistent_status_effects[instance_id]
        # The client shows it counting down from what is left, on the new clock.
        assert 7000 < entry.total_duration <= 7500 and not entry.permanent
        combat_state = restored.gameplay_combat_state
        assert talent_events._ready_rules(
            combat_state, talent_events._live_sources(restored), "killmarked",
        ) == ()
        # Refused by the cooldown alone: without it the same talent is ready.
        restored.gameplay_persistent_status_effects.pop(instance_id)
        assert talent_events._ready_rules(
            combat_state, talent_events._live_sources(restored), "killmarked",
        )
    finally:
        case.doCleanups()


# Second Chance


def lethal_blow(session, *, damage=1000):
    actor = session.gameplay_remote_actor_id
    state = session.gameplay_combat_state
    with state.lock:
        state.player_health[actor][0] = 0
    return DamageResolution(
        target_actor_id=actor, attacker_actor_id=state.encounter.actor_ids[0],
        current_health=0, maximum_health=1000, damage=damage, killed=True,
        position=(0.0, 2.0, 0.0), server_tick=500, hit_tags=(0,), skill_index=0,
    )


def tick_targets(session):
    return ((session.gameplay_remote_actor_id, (0.0, 2.0, 0.0), session),)


def test_second_chance_turns_the_lethal_hit_into_a_save(ranger):
    learn(ranger, SECOND_CHANCE)
    state = ranger.gameplay_combat_state
    saved, = spare_lethal_hits(state, tick_targets(ranger), (lethal_blow(ranger),))
    assert saved.death_saved and not saved.killed
    assert saved.current_health == 750  # "Regenerates 75% of your Health Points"
    assert state.player_health[ranger.gameplay_remote_actor_id][0] == 750

    started = dispatch_talent_event(ranger, "nohealth", server_tick=saved.server_tick)
    assert names(started) == {
        "trait_wisdom_all_death_trigger_effect",
        "trait_wisdom_all_death_trigger_cd_reduction_effect",
        "trait_wisdom_all_death_trigger_stun_aura",
        DEATH_COOLDOWN,
    }
    # "Fully restores your Resource"
    assert state.player_resource[ranger.gameplay_remote_actor_id][0] == 100.0
    assert ranger.gameplay_inventory.secondary_resource == 100.0
    assert not ranger.gameplay_death_pending


def test_second_chance_halves_skill_cooldowns(ranger):
    learn(ranger, SECOND_CHANCE)
    state = ranger.gameplay_combat_state
    spare_player(ranger, state, lethal_blow(ranger).target_actor_id)
    dispatch_talent_event(ranger, "nohealth", server_tick=500)
    geometry = state._load_player_skill_geometry(skill_id("adrenalin"))
    assert geometry.cooldown_seconds > 0
    modified = state._modified_player_skill_geometry(
        geometry, talent_events._live_sources(ranger),
    )
    assert modified.cooldown_seconds == pytest.approx(geometry.cooldown_seconds * 0.5)


def test_second_chance_stuns_enemies_within_three_metres(ranger):
    learn(ranger, SECOND_CHANCE)
    state = ranger.gameplay_combat_state
    near, far = state.encounter.actor_ids[:2]
    state.monster_positions[near] = (1.0, 2.0, 0.0)
    state.monster_positions[far] = (9.0, 2.0, 0.0)
    state.monster_health[near] = state.monster_health[far] = 10000
    lethal_blow(ranger)
    spare_player(ranger, state, ranger.gameplay_remote_actor_id)
    dispatch_talent_event(ranger, "nohealth", server_tick=500)
    now = time.perf_counter()
    state.apply_player_location_auras(
        now=now, server_tick=501,
        player_positions={ranger.gameplay_remote_actor_id: (0.0, 2.0, 0.0)},
    )
    assert state.monster_has_status_group(near, ("stun",), now=now)
    assert not state.monster_has_status_group(far, ("stun",), now=now)


def test_second_chance_saves_only_once_per_cooldown(ranger):
    learn(ranger, SECOND_CHANCE)
    state = ranger.gameplay_combat_state
    actor = ranger.gameplay_remote_actor_id
    spare_lethal_hits(state, tick_targets(ranger), (lethal_blow(ranger),))
    dispatch_talent_event(ranger, "nohealth", server_tick=500)

    again, = spare_lethal_hits(state, tick_targets(ranger), (lethal_blow(ranger),))
    assert again.killed and not again.death_saved
    assert state.player_health[actor][0] == 0

    five_minutes_pass(ranger, cooldown_instance(ranger, DEATH_COOLDOWN))
    later, = spare_lethal_hits(state, tick_targets(ranger), (lethal_blow(ranger),))
    assert later.death_saved and state.player_health[actor][0] == 750


def test_without_the_talent_a_lethal_hit_stays_lethal(ranger):
    learn(ranger, RECKLESS_HASTE)
    blow = lethal_blow(ranger)
    assert spare_lethal_hits(
        ranger.gameplay_combat_state, tick_targets(ranger), (blow,),
    ) == (blow,)


def test_a_monster_killed_by_a_monster_is_no_players_save(ranger):
    learn(ranger, SECOND_CHANCE)
    state = ranger.gameplay_combat_state
    blow = kill_of(state.encounter.actor_ids[0])
    assert spare_lethal_hits(state, tick_targets(ranger), (blow,)) == (blow,)


def test_the_arena_prevent_death_effect_is_left_to_its_own_rules(ranger):
    """It sets the same variable, but no talent hosts its rule."""
    state = ranger.gameplay_combat_state
    arena = talent_entries(SECOND_CHANCE)[0]
    wire_id = state._status_effect_ids["e_sum_01_defensive_arena_prevent_death"]
    ranger.gameplay_persistent_status_effects[arena.instance_id] = replace(
        arena, effect_id=wire_id,
    )
    lethal_blow(ranger)
    assert not spare_player(ranger, state, ranger.gameplay_remote_actor_id)


def test_the_saved_player_never_waits_for_a_session_lock_under_the_map_lock(ranger):
    """The monster tick holds combat.lock here; one busy player cannot stall it."""
    learn(ranger, SECOND_CHANCE)
    lethal_blow(ranger)
    with ranger.flow_lock:
        assert spare_player(
            ranger, ranger.gameplay_combat_state, ranger.gameplay_remote_actor_id,
        )


class _PvPFixture:
    def __enter__(self):
        self.case = pvp_effects.PVPPlayerEffectTests(
            "test_lethal_impact_uses_current_world_position_on_both_clients"
        )
        self.case.setUp()
        return self.case

    def __exit__(self, *_exc):
        self.case.doCleanups()


def sent_to(session):
    """Effects the PvP fixture's mock delivered to this player."""
    return {
        value.resource_id
        for call in CharacterService._send_player_skill_status_effects.call_args_list
        if call.args[0] is session
        for value in call.args[1]
    }


def test_second_chance_saves_from_a_lethal_pvp_hit():
    with _PvPFixture() as case:
        learn(case.target, SECOND_CHANCE)
        with patch.object(talent_events, "_ready_rules", wraps=talent_events._ready_rules):
            case.impact(damage=5000)
        actor = case.target.gameplay_remote_actor_id
        assert case.combat.player_health[actor][0] == 750
        assert not case.target.gameplay_death_pending
        assert not any(
            isinstance(command, KillServerCommand)
            for session in (case.source, case.target)
            for command in case.commands(session)
        )
        # Both clients are told the health the save left.
        hits = [
            command.value for command in case.commands(case.target)
            if hasattr(command.value, "current_health")
        ]
        assert hits and hits[-1].current_health == 750
        assert DEATH_COOLDOWN in sent_to(case.target)


def test_a_lethal_pvp_hit_without_the_talent_still_kills():
    with _PvPFixture() as case:
        case.impact(damage=5000)
        assert case.target.gameplay_death_pending
        assert any(
            isinstance(command, KillServerCommand)
            for command in case.commands(case.target)
        )


def test_killing_a_marked_player_grants_reckless_haste():
    with _PvPFixture() as case:
        learn(case.source, RECKLESS_HASTE)
        mark = effect("debuff_cc_mark,C:1.0,D:8.0,DP:4.0")
        with patch.object(
            CharacterService, "_active_pvp_victim_modifier_sources", return_value=(mark,),
        ):
            case.impact(damage=5000)
        assert HASTE_COOLDOWN in sent_to(case.source)


@pytest.mark.parametrize("talent_id,event", [
    (RECKLESS_HASTE, "killmarked"), (SECOND_CHANCE, "nohealth"),
])
def test_a_saved_talent_selection_hosts_its_rule_after_login(talent_id, event):
    """Players carry the pick in their talent tree, not as a saved effect."""
    case = unittest.TestCase()
    try:
        server = character_server(case)
        session, _ = initialize(server)
        categories = list(session.gameplay_talent_categories)
        wisdom = categories[1]
        picked = (*wisdom.selected_skills, TalentSelectionEntry(1, 0, talent_id))
        categories[1] = TalentCategorySelection(
            wisdom.preselection, picked,
            CharacterService._talent_category_roots(server.talent_database, picked),
        )
        session.gameplay_talent_categories = tuple(categories)
        session.gameplay_category_talent_presets.setdefault(1, {})[
            wisdom.preselection
        ] = picked
        server.account_store.put(save(server, session), flush=True)

        restored, _ = initialize(server)
        assert talent_events._ready_rules(
            restored.gameplay_combat_state, talent_events._live_sources(restored), event,
        )
    finally:
        case.doCleanups()


def test_second_chance_saves_from_a_lethal_set_program():
    """Item-set Stop/Done damage reaches the same zero by its own path."""
    session = _session(52272, 272, "Set wearer")
    state = LocalCombatState(
        load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False,
        ),
        player_actor_id=session.gameplay_remote_actor_id, player_health=1000,
    )
    state.register_player(session.gameplay_remote_actor_id, 1000)
    session.gameplay_combat_state = state
    session.gameplay_inventory = InventoryInfo(health_points=1000, secondary_resource=0)
    session.gameplay_presence_callback = None
    session.gameplay_last_move = None
    session.gameplay_spawn_position = (0.0, 2.0, 0.0)
    session.gameplay_deadline_timer = lambda delay, callback, args=(), kwargs=None: SimpleNamespace(
        start=Mock(), cancel=Mock(), daemon=False, delay=delay,
        callback=callback, args=args, kwargs=kwargs or {},
    )
    learn(session, SECOND_CHANCE)
    self_damage = SimpleNamespace(arguments=("-2.0", "relative", "Fire"))
    publication = []
    with patch.object(CharacterService, "_start_monster_chase"),             patch.object(CharacterService, "_request_account_save"):
        _apply_terminal_health(
            session, ((self_damage, ()),), server_tick=500,
            publication=publication,
            expected_generation=session.gameplay_chase_generation,
        )
    assert state.player_health[session.gameplay_remote_actor_id][0] == 750
    assert not session.gameplay_death_pending
    assert [kind for kind, _value in publication] == ["health"]
    assert publication[0][1].stat_id == 750  # really the current health


class _MonsterTick:
    """One real monster tick that lands a blow only this test wrote."""

    def __enter__(self):
        self.case = populated_map.PopulatedMapSimulationTests("test_monster_tick_does_not_deepcopy_immutable_moves")
        self.case.setUp()
        self.owner = self.case.owner
        # The fixture's ids sit below the base and would share one actor id.
        self.owner.gameplay_character.character_id = LOCAL_CHARACTER_ID_BASE + 1
        self.case.peer.gameplay_character.character_id = LOCAL_CHARACTER_ID_BASE + 2
        self.actor = CharacterService._remote_actor_id(LOCAL_CHARACTER_ID_BASE + 1)
        self.owner.gameplay_remote_actor_id = self.actor
        self.owner.gameplay_inventory = InventoryInfo(health_points=1000, secondary_resource=0)
        self.case.combat.register_player(
            self.actor, 1000, maximum_resource=100, current_resource=0.0,
        )
        return self

    def __exit__(self, *_exc):
        self.case.tearDown()
        self.case.doCleanups()

    def strike(self):
        combat = self.case.combat
        attacker = self.case.encounter.actor_ids[0]

        def lethal(_position, *, player_actor_id, **_kwargs):
            if player_actor_id != self.actor or combat.player_health[self.actor][0] <= 0:
                return ()
            previous = combat.player_health[self.actor][0]
            combat.player_health[self.actor][0] = 0
            return (DamageResolution(
                target_actor_id=self.actor, attacker_actor_id=attacker,
                current_health=0, maximum_health=1000, damage=previous, killed=True,
                position=(0.0, 0.0, 0.0), server_tick=int(time.time()) & 0xFFFF,
                hit_tags=(0,), skill_index=0,
            ),)

        with patch.object(combat, "advance_monsters", return_value=()),                 patch.object(combat, "maybe_start_monster_attacks", return_value=()),                 patch.object(combat, "resolve_due_monster_attacks", side_effect=lethal),                 patch.object(CharacterService, "_request_account_save"):
            assert self.case._tick()


def test_a_real_monster_tick_saves_the_talented_player():
    with _MonsterTick() as tick:
        learn(tick.owner, SECOND_CHANCE)
        tick.strike()
        assert tick.case.combat.player_health[tick.actor][0] == 750
        assert not tick.owner.gameplay_death_pending
        assert tick.case.combat.player_resource[tick.actor][0] == 100.0
        cooldown_instance(tick.owner, DEATH_COOLDOWN)


def test_a_real_monster_tick_still_kills_the_untalented_player():
    with _MonsterTick() as tick:
        tick.strike()
        assert tick.case.combat.player_health[tick.actor][0] == 0
        assert tick.owner.gameplay_death_pending


def test_the_catalog_reads_second_chance_from_its_own_row():
    rules = talent_events._talent_rules(str(DEFAULT_STATIC_DB_PATH))
    host = TALENTS.status_effect_wire_id("trait_wisdom_all_death_trigger_trigger")
    rule, = rules[host]
    assert (rule.event, rule.deny_group) == ("nohealth", "wisdom_death_trigger")
    assert rule.heal_on_death == pytest.approx(0.75)
    haste = TALENTS.status_effect_wire_id(
        "trait_wisdom_ranger_killMarked_trigger_effect_trigger"
    )
    assert rules[haste][0].deny_group == "wisdom_kilMarked_trigger"
    assert math.isclose(rules[haste][0].heal_on_death, 0.0)


# The other classes' picks, the one-handed and two-handed kill talents, and the
# dwarf's turrets.

BLOCK = "trait_wisdom_warrior_block_trigger"
INUREMENT = "trait_wisdom_warrior_stun_trigger"
ENERGETIC_FORCE = "trait_wisdom_mage_hitCritical_Trigger"
EMERGENCY_RESERVES = "trait_wisdom_mage_hitLowHealth_Trigger"
ONE_HAND_KILL = "trait_wisdom_all_1hand_kill_trigger"
TWO_HAND_KILL = "trait_wisdom_all_2hand_kill_trigger"
CLOSE_TO_TURRET = "trait_wisdom_dwarf_towerInRange_Trigger"
COMPENSATION = "trait_wisdom_dwarf_towerDestroyed_trigger"


def learn_rank(session, talent_id, rank):
    for entry in TALENTS.status_effect_entries(
        TALENTS.definition(talent_id), target_rank=rank,
        actor_id=LOCAL_PLAYER_ACTOR_ID,
        first_instance_id=session.gameplay_next_status_effect_instance_id,
        server_tick=0,
    ):
        session.gameplay_persistent_status_effects[entry.instance_id] = entry
        session.gameplay_next_status_effect_instance_id = entry.instance_id + 1


def taken(session, *, damage=100, current=900, maximum=1000, blocked=False,
          effects=(), periodic=False, killed=False):
    return DamageResolution(
        target_actor_id=session.gameplay_remote_actor_id,
        attacker_actor_id=session.gameplay_combat_state.encounter.actor_ids[0],
        current_health=current, maximum_health=maximum, damage=damage,
        killed=killed, position=(0.0, 2.0, 0.0), server_tick=500, hit_tags=(0,),
        skill_index=0, victim_status_effects=tuple(effects), blocked=blocked,
        periodic_damage=periodic,
    )


def effect_names(session):
    names = {
        wire_id: name
        for name, wire_id in session.gameplay_combat_state._status_effect_ids.items()
    }
    return {names[entry.effect_id] for entry in talent_events._live_sources(session)}


def stats(sources, *base):
    return calculate_player_combat_stats(
        DEFAULT_STATIC_DB_PATH, class_name="warrior", level=100,
        inventory=InventoryInfo(), modifier_sources=(*base, *sources),
    )


def test_a_block_grants_adamant_wall_every_five_minutes(ranger):
    learn(ranger, BLOCK)
    dispatch_incoming_hit(ranger, taken(ranger, blocked=True))
    active = effect_names(ranger)
    assert "trait_wisdom_warrior_block_buff_blockValue" in active
    assert "trait_wisdom_warrior_block_trigger_effect_cooldown" in active
    live = talent_events._live_sources(ranger)
    rating = CombatModifier("BlockValue", ("1000", "absolute"))
    assert stats(live, rating).block_chance > stats((), rating).block_chance

    ranger.recorder.messages.clear()
    dispatch_incoming_hit(ranger, taken(ranger, blocked=True))
    assert not ranger.recorder.messages


def test_a_hit_that_was_not_blocked_grants_no_adamant_wall(ranger):
    learn(ranger, BLOCK)
    dispatch_incoming_hit(ranger, taken(ranger))
    assert "trait_wisdom_warrior_block_buff_blockValue" not in effect_names(ranger)


@pytest.mark.parametrize("entry", (
    "debuff_cc_stun,C:1.0,D:5.0", "debuff_cc_freeze,C:1.0,D:5.0",
    "debuff_cc_petrify,C:1.0,D:5.0", "dwarf_jump_stun,C:1.0,D:2.0",
))
def test_getting_stunned_grants_inurement(ranger, entry):
    learn(ranger, INUREMENT)
    dispatch_incoming_hit(ranger, taken(ranger, effects=(effect(entry),)))
    active = effect_names(ranger)
    assert {
        "trait_wisdom_warrior_stun_buff_resistance",
        "trait_wisdom_warrior_stun_buff_armor",
    } <= active
    live = talent_events._live_sources(ranger)
    rating = CombatModifier("Resistance", ("1000", "absolute", "Fire"))
    assert (stats(live, rating).incoming_damage_multiplier("Fire")
            < stats((), rating).incoming_damage_multiplier("Fire"))


@pytest.mark.parametrize("entry", (
    "debuff_dot_bleeding,C:1.0,D:5.0", "debuff_cc_mark,C:1.0,D:8.0",
    "debuff_cc_stun_immunity,C:1.0,D:3.0",
))
def test_inurement_ignores_what_does_not_stun(ranger, entry):
    learn(ranger, INUREMENT)
    dispatch_incoming_hit(ranger, taken(ranger, effects=(effect(entry),)))
    assert "trait_wisdom_warrior_stun_buff_armor" not in effect_names(ranger)


def energetic_force(session):
    source = talent_entries(ENERGETIC_FORCE)[0]
    state = session.gameplay_combat_state
    geometry = state._modified_player_skill_geometry(
        state._load_player_skill_geometry(skill_id("markshot")), (source,),
    )
    return next(
        value for value in geometry.user_status_effects
        if value.resource_id == "trait_wisdom_mage_hitCritical_trigger_effect"
    )


def test_a_critical_hit_grants_energetic_force_through_its_rule(ranger):
    learn(ranger, ENERGETIC_FORCE)
    carried = energetic_force(ranger)
    assert carried.trigger == "hitcritical"
    other = effect("debuff_cc_mark,C:1.0,D:8.0")
    # The carrier leaves the cast's list; other user effects stay on it.
    assert route_talent_user_effects(
        ranger, (carried, other), server_tick=500,
    ) == (other,)
    active = effect_names(ranger)
    assert "trait_wisdom_mage_hitCritical_trigger_buff_critValue" in active
    assert "trait_wisdom_mage_hitCritical_trigger_effect_cooldown" in active
    live = talent_events._live_sources(ranger)
    rating = CombatModifier("CriticalValue", ("1000", "absolute"))
    assert stats(live, rating).critical_rating == pytest.approx(
        stats((), rating).critical_rating * 1.5
    )

    # "Can be triggered every 300 seconds"
    ranger.recorder.messages.clear()
    assert route_talent_user_effects(ranger, (carried,), server_tick=501) == ()
    assert not ranger.recorder.messages


def test_a_cast_without_the_talent_keeps_its_user_effects(ranger):
    other = effect("debuff_cc_mark,C:1.0,D:8.0")
    assert route_talent_user_effects(ranger, (other,), server_tick=500) == (other,)


def test_a_mage_regains_mana_when_hit_below_a_third_of_their_health(ranger):
    learn(ranger, EMERGENCY_RESERVES)
    state = ranger.gameplay_combat_state
    actor = ranger.gameplay_remote_actor_id
    dispatch_incoming_hit(ranger, taken(ranger, current=400))
    assert state.player_resource[actor][0] == 0.0
    dispatch_incoming_hit(ranger, taken(ranger, current=300))
    assert state.player_resource[actor][0] == 15.0
    # A damage-over-time tick is not a hit, and neither is a miss.
    dispatch_incoming_hit(ranger, taken(ranger, current=300, periodic=True))
    dispatch_incoming_hit(ranger, taken(ranger, current=300, damage=0))
    assert state.player_resource[actor][0] == 15.0


@pytest.mark.parametrize("slots,expected", [
    ({1: (5, 6)}, "kill2hand"),
    ({1: (5,)}, "kill1hand"),
    ({1: (5,), 2: (6,)}, "kill1hand"),  # a weapon and an offhand
    ({2: (6,)}, ""),
    ({}, ""),
])
def test_a_kill_is_named_after_the_weapon_in_hand(ranger, slots, expected):
    ranger.gameplay_inventory.equipment_slots = dict(slots)
    assert talent_events._weapon_kill_event(ranger) == expected


def one_hand_cost(session):
    state = session.gameplay_combat_state
    geometry = state._load_player_skill_geometry(skill_id("mightyshot"))
    assert geometry.resource_cost_fraction > 0
    return (
        geometry.resource_cost_fraction,
        state._modified_player_skill_geometry(
            geometry, talent_events._live_sources(session),
        ).resource_cost_fraction,
    )


@pytest.mark.parametrize("rank,reduction", [(1, 0.0125), (40, 0.11), (60, 0.16)])
def test_a_one_handed_kill_cuts_resource_costs_by_its_rank(ranger, rank, reduction):
    ranger.gameplay_inventory.equipment_slots = {1: (5,)}
    learn_rank(ranger, ONE_HAND_KILL, rank)
    kill = kill_of(ranger.gameplay_combat_state.encounter.actor_ids[0])
    assert "trait_wisdom_all_1hand_kill_trigger_effect" in names(
        dispatch_kill_talents(ranger, (kill,))
    )
    base, reduced = one_hand_cost(ranger)
    assert reduced == pytest.approx(base * (1.0 - reduction))


def test_a_two_handed_weapon_does_not_earn_the_one_handed_talent(ranger):
    ranger.gameplay_inventory.equipment_slots = {1: (5, 6)}
    learn_rank(ranger, ONE_HAND_KILL, 10)
    kill = kill_of(ranger.gameplay_combat_state.encounter.actor_ids[0])
    assert dispatch_kill_talents(ranger, (kill,)) == ()


def test_each_two_handed_final_blow_shortens_running_cooldowns(ranger):
    ranger.gameplay_inventory.equipment_slots = {1: (5, 6)}
    learn_rank(ranger, TWO_HAND_KILL, 40)
    kill = kill_of(ranger.gameplay_combat_state.encounter.actor_ids[0])
    cooldowns = player_skill_timing_state(ranger).cooldowns
    now = time.perf_counter()
    cooldowns["skill03"] = now + 100.0
    dispatch_kill_talents(ranger, (kill,))
    # -1.25% at rank 1 and -0.25% per rank after it: 11% at rank 40.
    assert cooldowns["skill03"] - now == pytest.approx(89.0, abs=0.1)

    ranger.gameplay_runtime_status_effects.clear()
    cooldowns["skill03"] = now + 100.0
    dispatch_kill_talents(ranger, (kill, kill, kill))
    assert cooldowns["skill03"] - now == pytest.approx(100.0 * 0.89 ** 3, abs=0.1)


# Dwarf turrets, through the real cast and summon services.


def pulse(owner, position, clock):
    """One map tick's ground-aura pass and its delivery."""
    combat = owner.gameplay_combat_state
    actor = owner.gameplay_remote_actor_id
    with combat.lock:
        _hits, applications = combat.apply_player_location_auras(
            now=clock(), server_tick=int(owner.gameplay_chase_server_tick),
            player_positions={actor: position},
        )
    CharacterService._dispatch_location_aura_applications({actor: owner}, applications)


TOWER_BUFF = "trait_wisdom_dwarf_towerInRange_trigger_effect"


def test_standing_near_a_turret_grants_close_to_turret(owner, clock):
    learn(owner, CLOSE_TO_TURRET)
    position = anchor(owner)
    turret = cast(owner, "TurretCombat", position, 77)
    near = (position[0] + 4.0, position[1], position[2])
    far = (position[0] + 9.0, position[1], position[2])

    pulse(owner, far, clock)
    assert TOWER_BUFF not in effect_names(owner)

    pulse(owner, near, clock)
    assert TOWER_BUFF in effect_names(owner)

    pulse(owner, far, clock)
    assert TOWER_BUFF not in effect_names(owner)
    assert turret.actor_id in owner.gameplay_combat_state.player_summons


def test_close_to_turret_cuts_cooldowns_each_second(owner, clock):
    learn(owner, CLOSE_TO_TURRET)
    position = anchor(owner)
    cast(owner, "TurretCombat", position, 78)
    cooldowns = player_skill_timing_state(owner).cooldowns
    cooldowns["skill07"] = clock() + 100.0
    pulse(owner, position, clock)
    # Its Start program: "Reduces your active skill cool-down times by 5%".
    assert cooldowns["skill07"] - clock() == pytest.approx(95.0, abs=0.01)

    tick = next(
        timer for key, timer in owner.gameplay_projectile_timers.items()
        if key[0] == "player-status-tick"
    )
    assert tick.delay == pytest.approx(1.0)
    tick.callback(*tick.args, **tick.kwargs)
    assert cooldowns["skill07"] - clock() == pytest.approx(95.0 * 0.95, abs=0.01)


def test_a_turret_without_the_talent_grants_nothing(owner, clock):
    position = anchor(owner)
    cast(owner, "TurretCombat", position, 79)
    pulse(owner, position, clock)
    assert TOWER_BUFF not in effect_names(owner)


def test_the_turrets_ring_ends_with_the_turret(owner, clock):
    learn(owner, CLOSE_TO_TURRET)
    position = anchor(owner)
    turret = cast(owner, "TurretCombat", position, 80)
    combat = owner.gameplay_combat_state
    pulse(owner, position, clock)
    hosted = [
        visual for visual in combat.player_location_visuals.values()
        if visual.source_actor_id == turret.actor_id
    ]
    assert [visual.definition.resource_id for visual in hosted] == [
        "trait_wisdom_dwarf_towerInRange_trigger_aura"
    ]
    # Not the authored 999 s: a client would keep drawing it.
    assert hosted[0].definition.duration_ticks == round(
        turret.definition.lifetime_seconds * 25
    )
    CharacterService._expire_player_summon(
        owner, owner.gameplay_chase_generation,
        ("pvp-summon-kill", turret.actor_id), turret.actor_id,
    )
    assert not any(
        visual.source_actor_id == turret.actor_id
        for visual in combat.player_location_visuals.values()
    )
    pulse(owner, position, clock)
    assert TOWER_BUFF not in effect_names(owner)


def steam(owner):
    return owner.gameplay_combat_state.player_resource[
        owner.gameplay_remote_actor_id
    ][0]


def turret_with_compensation(owner, *, talent=True):
    combat = owner.gameplay_combat_state
    combat.register_player(
        owner.gameplay_remote_actor_id, 1000,
        maximum_resource=100, current_resource=0.0,
    )
    if talent:
        learn(owner, COMPENSATION)
    return cast(owner, "TurretCombat", anchor(owner), 81)


def test_a_destroyed_turret_pays_its_owner_steam(owner, clock):
    turret = turret_with_compensation(owner)
    before = steam(owner)
    CharacterService._expire_player_summon(
        owner, owner.gameplay_chase_generation,
        ("pvp-summon-kill", turret.actor_id), turret.actor_id,
    )
    assert steam(owner) == pytest.approx(before + 20.0)


def test_a_turret_that_simply_runs_out_pays_nothing(owner, clock):
    turret = turret_with_compensation(owner)
    before = steam(owner)
    CharacterService._expire_player_summon(
        owner, owner.gameplay_chase_generation,
        ("player-summon-expire", turret.actor_id), turret.actor_id,
    )
    assert steam(owner) == pytest.approx(before)


def test_a_destroyed_turret_pays_nothing_without_the_talent(owner, clock):
    turret = turret_with_compensation(owner, talent=False)
    before = steam(owner)
    CharacterService._expire_player_summon(
        owner, owner.gameplay_chase_generation,
        ("pvp-summon-kill", turret.actor_id), turret.actor_id,
    )
    assert steam(owner) == pytest.approx(before)


def test_every_turret_a_dwarf_can_build_is_in_the_rules_scope():
    """Compensation's authored list names none of these; the scope does."""
    rules = talent_events._talent_rules(str(DEFAULT_STATIC_DB_PATH))
    host = TALENTS.status_effect_wire_id(
        "trait_wisdom_dwarf_towerDestroyed_trigger_effect_trigger"
    )
    rule, = rules[host]
    assert rule.event == talent_events.MINION_DESTROYED
    assert {
        "minion_combatturret", "minion_teslaturret",
        "minion_machinegunturret", "minion_coveringfireturret",
    } <= rule.templates


def test_every_rule_in_the_catalog_has_something_that_raises_it():
    """A rule nothing raises is how these talents went unnoticed."""
    rules = talent_events._talent_rules(str(DEFAULT_STATIC_DB_PATH))
    raised = {rule.event for values in rules.values() for rule in values}
    assert raised == talent_events.TALENT_EVENTS | {
        talent_events.MINION_SPAWNED, talent_events.MINION_DESTROYED,
    }


def real_impact(session, *, critical=False, health=100000, user_effects=()):
    """One arrow's impact through the real projectile service."""
    state = session.gameplay_combat_state
    actor = state.encounter.actor_ids[0]
    source = session.gameplay_remote_actor_id
    session.gameplay_monster_encounter = state.encounter
    state.monster_positions[actor] = (0.0, 2.0, -2.0)
    state.monster_health[actor] = health
    state.pending_runtime_player_hits[(source, 900, 0)] = PendingPlayerHit(
        cast_id=900, target_actor_id=actor, skill_index=skill_id("markshot"),
        due_at=0.0, impact_tick=600, distance=2.0, travel_distance=0.0,
        emit_delay=0.0, victim_status_effects=(), source_actor_id=source,
        user_status_effects=tuple(user_effects), damage_modifier=1.0,
        player_minimum_damage=100.0, player_maximum_damage=100.0,
        critical_hit_chance=1.0 if critical else 0.0,
        target_hit_count=1,
    )
    with patch.object(CharacterService, "_award_monster_kill_xp"), \
            patch.object(CharacterService, "_notify_monsters_killed"):
        CharacterService._resolve_player_projectile(
            session, session.gameplay_chase_generation, 900, source, 0,
        )
    return actor


def test_a_real_critical_impact_starts_energetic_force(ranger):
    learn(ranger, ENERGETIC_FORCE)
    carried = energetic_force(ranger)
    real_impact(ranger, critical=False, user_effects=(carried,))
    assert "trait_wisdom_mage_hitCritical_trigger_buff_critValue" not in effect_names(ranger)
    real_impact(ranger, critical=True, user_effects=(carried,))
    assert "trait_wisdom_mage_hitCritical_trigger_buff_critValue" in effect_names(ranger)
    assert "trait_wisdom_mage_hitCritical_trigger_effect_cooldown" in effect_names(ranger)


def test_a_real_final_blow_fires_the_weapon_talent(ranger):
    ranger.gameplay_inventory.equipment_slots = {1: (5,)}
    learn_rank(ranger, ONE_HAND_KILL, 10)
    real_impact(ranger)
    assert "trait_wisdom_all_1hand_kill_trigger_effect" not in effect_names(ranger)
    actor = real_impact(ranger, health=1)
    assert ranger.gameplay_combat_state.monster_health[actor] == 0
    assert "trait_wisdom_all_1hand_kill_trigger_effect" in effect_names(ranger)


def test_a_real_monster_tick_runs_a_blocked_hit_through_the_talent():
    with _MonsterTick() as tick:
        learn(tick.owner, BLOCK)
        combat = tick.case.combat
        attacker = tick.case.encounter.actor_ids[0]

        def blocked(_position, *, player_actor_id, **_kwargs):
            if player_actor_id != tick.actor:
                return ()
            return (DamageResolution(
                target_actor_id=tick.actor, attacker_actor_id=attacker,
                current_health=950, maximum_health=1000, damage=50, killed=False,
                position=(0.0, 0.0, 0.0), server_tick=int(time.time()) & 0xFFFF,
                hit_tags=(0,), skill_index=0, blocked=True,
            ),)

        with patch.object(combat, "advance_monsters", return_value=()), \
                patch.object(combat, "maybe_start_monster_attacks", return_value=()), \
                patch.object(combat, "resolve_due_monster_attacks", side_effect=blocked), \
                patch.object(CharacterService, "_request_account_save"):
            assert tick.case._tick()
        wire_id = TALENTS.status_effect_wire_id("trait_wisdom_warrior_block_buff_blockValue")
        assert wire_id in {
            entry.effect_id for entry in talent_events._live_sources(tick.owner)
        }


# Idle talents: an event-only rule must change nothing until its event.


def wire_entry(effect_id, *, stack=0):
    template = talent_entries(SECOND_CHANCE)[0]
    return replace(template, effect_id=int(effect_id), stack_index=stack)


def test_an_idle_talent_changes_no_cast(combat):
    """Second Chance halved cooldowns and Final Supernova added 1000% for good.

    Every host of an event rule, across all trees. Rules that ride the cast
    keep their carrier on it, gated by its trigger, and change nothing else.
    """
    rides_cast = {
        "trait_wisdom_ranger_hitStunned_trigger_effect",
        "trait_wisdom_mage_hitCritical_trigger_effect",
        "trait_wisdom_mage_hitLowHealth_trigger_effect",
    }
    hosts = tuple(talent_events._talent_rules(str(DEFAULT_STATIC_DB_PATH)))
    assert len(hosts) > 20
    for wire_id in hosts:
        source = wire_entry(wire_id)
        for skill in ("adrenalin", "mightyshot", "stab"):
            geometry = combat._load_player_skill_geometry(skill_id(skill))
            modified = combat._modified_player_skill_geometry(geometry, (source,))
            assert modified.cooldown_seconds == geometry.cooldown_seconds, wire_id
            assert modified.resource_cost_fraction == geometry.resource_cost_fraction, wire_id
            assert {
                value.resource_id for value in modified.victim_status_effects
            } - {value.resource_id for value in geometry.victim_status_effects} <= rides_cast
            assert not {
                value.resource_id for value in modified.location_status_effects
                if value.trigger in ("", "hit", "immediate", "immediatemanaged")
            } - {value.resource_id for value in geometry.location_status_effects}, wire_id


@pytest.mark.parametrize("skill,host", [
    ("magicmissile", "pvp_mage_talent4_activator"),
    ("markshot", "pvp_ranger_talent4_activator"),
])
def test_a_death_trait_adds_nothing_to_a_hit(combat, skill, host):
    def hit(sources):
        return shoot(combat, resource=skill, sources=sources)

    plain = hit(())
    combat.monster_status_effects.clear()
    talented = hit((wire_entry(TALENTS.status_effect_wire_id(host)),))
    assert talented.damage == plain.damage
    assert {value.resource_id for value in talented.victim_status_effects} == {
        value.resource_id for value in plain.victim_status_effects
    }


@pytest.mark.parametrize("host,effect_id", [
    ("pvp_dwarf_talent3_activator", "dwarf_stun_OnLowHP_aura"),
    ("warrior_talent_lightning_master_lvl", "all_talent_lightning_master_lightning_field_effect"),
])
def test_a_ground_rule_is_not_part_of_a_cast(combat, host, effect_id):
    """``lowhealth`` on a hit reads the monster's health: this stunned the weak."""
    source = wire_entry(TALENTS.status_effect_wire_id(host))
    geometry = combat._modified_player_skill_geometry(
        combat._load_player_skill_geometry(skill_id("markshot")), (source,),
    )
    assert effect_id not in {
        value.resource_id for value in geometry.location_status_effects
    }


def test_a_dwarfs_trait_does_not_stun_a_weakened_monster(combat):
    source = wire_entry(TALENTS.status_effect_wire_id("pvp_dwarf_talent3_activator"))
    actor = combat.encounter.actor_ids[0]
    maximum = combat.monsters[actor].maximum_health
    combat.monster_positions[actor] = (0.0, 2.0, -5.0)
    combat.monster_health[actor] = max(1000, maximum // 4)
    pending, = combat.start_bullet_skill_hits(
        BulletSkill(0, skill_id("markshot"), 0.0, 2000, 321, (0.0, 0.0, -1.0, 0.0), 0),
        combat.encounter.coordinate_frame.world_to_wire((0.0, 2.0, 0.0)),
        source_actor_id=1, now=100.0, eligible_actor_ids=(actor,),
        player_damage_range=(10.0, 10.0), modifier_effect_ids=(source,),
    )
    combat.monster_health[actor] = max(1, maximum // 4)
    hit = combat.resolve_runtime_player_hit(1, 321, 0, now=pending.due_at)
    assert "dwarf_stun_OnLowHP_aura" not in {
        value.resource_id for value in hit.victim_status_effects
    }


def test_will_to_survive_rides_both_of_its_skills(combat):
    """``addUE,warshout,frenzyshout,<effect>``: the second skill is no effect."""
    source = wire_entry(TALENTS.status_effect_wire_id("pvp_warrior_talent1_activator"))
    for skill, expected in (("warshout", True), ("frenzyshout", True), ("stab", False)):
        geometry = combat._modified_player_skill_geometry(
            combat._load_player_skill_geometry(skill_id(skill)), (source,),
        )
        carried = next(
            (value for value in geometry.user_status_effects
             if value.resource_id == "warrior_healing_talent"),
            None,
        )
        assert (carried is not None) == expected, skill
        if carried is not None:
            assert carried.duration_ticks == 125
            assert carried.values[0] == pytest.approx(0.02)


# Honor traits on real sessions.


def trait(session, trait_id):
    learn(session, trait_id)


def low(session, *, current, maximum=1000):
    CharacterService._update_low_health_socket_state(
        session, current_health=current, maximum_health=maximum, server_tick=700,
    )


@pytest.mark.parametrize("trait_id,expected", [
    ("mage_fame_trait_02_hpreg_onkill", {"mage_cyanitskin"}),
    ("ranger_fame_trait_02_hpreg_onkill", {"RangerAdrenalin", "ranger_pvp_stunimmunity"}),
    ("warrior_fame_trait_02_hpreg_onkill", {"warrior_regeneration_OnLowHP"}),
])
def test_falling_below_a_third_starts_the_trait_once(ranger, trait_id, expected):
    trait(ranger, trait_id)
    low(ranger, current=500)
    assert not expected & effect_names(ranger)
    low(ranger, current=300)
    assert expected <= effect_names(ranger)
    ranger.recorder.messages.clear()
    low(ranger, current=250)    # Already low: only the crossing counts.
    assert not ranger.recorder.messages


def test_supernatural_regeneration_ends_above_a_third(ranger):
    trait(ranger, "warrior_fame_trait_02_hpreg_onkill")
    low(ranger, current=300)
    assert "warrior_regeneration_OnLowHP" in effect_names(ranger)
    low(ranger, current=400)
    assert "warrior_regeneration_OnLowHP" not in effect_names(ranger)


def test_a_killing_blow_does_not_count_as_falling_low(ranger):
    trait(ranger, "mage_fame_trait_02_hpreg_onkill")
    low(ranger, current=0)
    assert "mage_cyanitskin" not in effect_names(ranger)


def test_emergency_measures_refills_steam_and_cools_rocket_pack(ranger):
    trait(ranger, "dwarf_fame_trait_02_hpreg_onkill")
    state = ranger.gameplay_combat_state
    cooldowns = player_skill_timing_state(ranger).cooldowns
    cooldowns["skill15"] = time.perf_counter() + 60.0
    near, far = state.encounter.actor_ids[:2]
    state.monster_positions[near] = (1.0, 2.0, 0.0)
    state.monster_positions[far] = (20.0, 2.0, 0.0)
    state.monster_health[near] = state.monster_health[far] = 10000
    low(ranger, current=300)
    assert state.player_resource[ranger.gameplay_remote_actor_id][0] == 100.0
    assert "skill15" not in cooldowns
    now = time.perf_counter()
    state.apply_player_location_auras(
        now=now, server_tick=701,
        player_positions={ranger.gameplay_remote_actor_id: (0.0, 2.0, 0.0)},
    )
    assert "dwarf_stun_OnLowHP_aura" in {
        value.resource_id for value in state._active_monster_modifier_sources(near, now=now)
    }
    assert not state._active_monster_modifier_sources(far, now=now)


def test_a_mastery_low_health_rule_runs_too(ranger):
    learn_rank(ranger, "warrior_talent_tank", 2)
    low(ranger, current=300)
    assert "warrior_talent_tank_buff_armor" in effect_names(ranger)


def test_master_of_lightning_leaves_a_field_when_stunned(ranger):
    ranger.gameplay_persistent_status_effects[900] = replace(
        wire_entry(TALENTS.status_effect_wire_id("warrior_talent_lightning_master_lvl")),
        instance_id=900,
    )
    state = ranger.gameplay_combat_state
    dispatch_incoming_hit(ranger, taken(ranger, effects=(effect("debuff_cc_stun,C:1.0,D:5.0"),)))
    assert "all_talent_lightning_master_lightning_field_aura" in {
        visual.definition.resource_id for visual in state.player_location_visuals.values()
    }


def test_master_of_lightning_stays_off_in_a_match(ranger):
    ranger.gameplay_persistent_status_effects[900] = replace(
        wire_entry(TALENTS.status_effect_wire_id("warrior_talent_lightning_master_lvl")),
        instance_id=900,
    )
    ranger.gameplay_match = SimpleNamespace(phase=MatchPhase.RUNNING, round_index=0)
    state = ranger.gameplay_combat_state
    dispatch_incoming_hit(ranger, taken(ranger, effects=(effect("debuff_cc_stun,C:1.0,D:5.0"),)))
    assert not state.player_location_visuals


def test_emergency_reserves_no_longer_pays_for_the_mages_own_hits(ranger):
    learn(ranger, EMERGENCY_RESERVES)
    state = ranger.gameplay_combat_state
    geometry = state._modified_player_skill_geometry(
        state._load_player_skill_geometry(skill_id("markshot")),
        (talent_entries(EMERGENCY_RESERVES)[0],),
    )
    carried = next(
        value for value in geometry.user_status_effects
        if value.resource_id == "trait_wisdom_mage_hitLowHealth_trigger_effect"
    )
    assert route_talent_user_effects(ranger, (carried,), server_tick=500) == ()
    assert state.player_resource[ranger.gameplay_remote_actor_id][0] == 0.0


class _Arena:
    """The PvP fixture inside a running match, with its real delivery."""

    MOCKED = (
        "_dispatch_socket_event", "_update_low_health_socket_state",
        "_send_player_skill_status_effects",
    )

    def __enter__(self):
        real = {
            name: getattr(CharacterService, name) for name in self.MOCKED
        }
        self.case = pvp_effects.PVPPlayerEffectTests(
            "test_lethal_impact_uses_current_world_position_on_both_clients"
        )
        self.case.setUp()
        self.live = [
            *(patch.object(CharacterService, name, real[name]) for name in self.MOCKED),
            patch.object(
                pvp_combat, "calculate_player_combat_stats",
                calculate_player_combat_stats,
            ),
        ]
        for patcher in self.live:
            patcher.start()
        case = self.case
        self.frame = case.combat.encounter.coordinate_frame
        match = PVPMatch(MATCH_MODE_BEST_OF_3_1V1, tuple(
            MatchParticipant(
                int(session.gameplay_character.character_id), None,
                session.gameplay_character.name, 100, 1, team, session=session,
            )
            for team, session in enumerate((case.source, case.target))
        ))
        match.phase = MatchPhase.RUNNING
        for team, session in enumerate((case.source, case.target)):
            session.gameplay_match = match
            session.gameplay_match_team = team
            session.gameplay_match_callback = None
            session.gameplay_inventory = InventoryInfo(
                health_points=1000, secondary_resource=0,
            )
            session.gameplay_monster_encounter = case.combat.encounter
            session.gameplay_character.level = 100
        self.source_id = case.source.gameplay_remote_actor_id
        self.target_id = case.target.gameplay_remote_actor_id
        return self

    def __exit__(self, *_exc):
        for patcher in reversed(self.live):
            patcher.stop()
        self.case.doCleanups()

    @property
    def target_health(self):
        return self.case.combat.player_health[self.target_id][0]

    def run_timers(self, name):
        for timer in tuple(self.case.timers):
            if getattr(timer.function, "__name__", "") == name:
                timer.function(*timer.args, **timer.kwargs)


def test_final_supernova_strikes_nearby_players_after_its_fuse():
    with _Arena() as arena:
        case = arena.case
        learn(case.source, "mage_fame_trait_01_nova")
        case.move(case.target, (30.0, 0.0, 53.57))   # 4.5 m away
        with patch.object(talent_events, "_owner_damage_range", return_value=(10.0, 10.0)):
            definitions = talent_events.dispatch_player_death(case.source)
            assert "mage_passive_ondeath_nova" in names(definitions)
            assert arena.target_health == 1000   # The fuse is three seconds.
            fuse = next(
                timer for timer in case.timers
                if getattr(timer.function, "__name__", "") == "_strike_pvp_ground_aura"
            )
            assert fuse.delay == pytest.approx(3.0)
            arena.run_timers("_strike_pvp_ground_aura")
        # 1000% of 10 base damage, before the target's armor.
        assert 0 < 1000 - arena.target_health <= 100


@pytest.mark.parametrize("trait_id,receiver,fuse,slowed", [
    ("warrior_fame_trait_01_nova", "warrior_deathcry_aura_effect_damage", 1.0, True),
    ("ranger_fame_trait_01_nova", "ranger_fireflies_aura_effect", 3.0, True),
])
def test_a_death_blast_lands_its_lasting_effects_on_players(trait_id, receiver, fuse, slowed):
    with _Arena() as arena, patch.object(
        talent_events, "_owner_damage_range", return_value=(10.0, 10.0),
    ):
        case = arena.case
        learn(case.source, trait_id)
        case.move(case.target, (28.0, 0.0, 53.57))
        talent_events.dispatch_player_death(case.source)
        timer = next(
            value for value in case.timers
            if getattr(value.function, "__name__", "") == "_strike_pvp_ground_aura"
        )
        assert timer.delay == pytest.approx(fuse)
        arena.run_timers("_strike_pvp_ground_aura")
        wire_id = TALENTS.status_effect_wire_id(receiver)
        running = case.target.gameplay_victim_status_effect_instances
        assert (LOCAL_PLAYER_ACTOR_ID, wire_id) in running
        entry, _expires = running[(LOCAL_PLAYER_ACTOR_ID, wire_id)]
        assert entry.total_duration > 0


def test_a_death_trait_needs_an_official_arena(ranger):
    trait(ranger, "mage_fame_trait_01_nova")
    assert talent_events.dispatch_player_death(ranger) == ()


def test_a_ground_strike_ignores_players_out_of_reach():
    with _Arena() as arena:
        case = arena.case
        aura = effect("mage_supernova_aura,C:1.0,D:0.1,DP:0.1,$0:-10.0")
        receivers = (effect("mage_supernova_aura_effect,C:1.0,$0:-10.0"),)
        case.move(case.target, (45.0, 0.0, 53.57))   # 19.5 m away
        CharacterService._schedule_pvp_ground_strike(
            case.source, case.combat, aura, receivers,
            position=(25.49, 0.0, 53.57), damage_range=(10.0, 10.0), server_tick=300,
        )
        assert arena.target_health == 1000


def test_second_chance_stuns_enemy_players_too():
    with _Arena() as arena, patch.object(
        talent_events, "_owner_damage_range", return_value=(10.0, 10.0),
    ):
        case = arena.case
        learn(case.source, SECOND_CHANCE)
        case.move(case.target, (26.5, 0.0, 53.57))
        talent_events.dispatch_talent_event(case.source, "nohealth", server_tick=300)
        stun = TALENTS.status_effect_wire_id("debuff_cc_stun")
        assert (1, stun) in case.target.gameplay_victim_status_effect_instances


def test_defeating_a_player_restores_a_share_of_health():
    with _Arena() as arena:
        case = arena.case
        learn(case.source, "warrior_fame_trait_02_hpreg_onkill")
        health = case.combat.player_health[arena.source_id]
        health[0] = 500
        with patch.object(CharacterService, "_pvp_round_token", return_value=None):
            case.impact(damage=5000)
        assert arena.target_health == 0
        arena.run_timers("_tick_player_status_effect")
        assert health[0] == 650    # 15% of 1000


def test_nimble_raises_attack_speed_in_a_match_only(ranger):
    trait(ranger, "mage_fame_trait_04_supportaura")
    dispatch_talent_event(ranger, "enterpvp", server_tick=500)
    live = talent_events._live_sources(ranger)
    assert stats(live).attack_speed == pytest.approx(stats(()).attack_speed * 1.2, rel=1e-3)


@pytest.mark.parametrize("rank,share", [(1, 0.99), (20, 0.8)])
def test_steadfastness_turns_away_player_damage(rank, share):
    definition = TALENTS.definition("fame_focus_talent_03_playerdmgresistence")
    entries = TALENTS.status_effect_entries(
        definition, target_rank=rank, actor_id=1, first_instance_id=1, server_tick=0,
    )
    assert stats(entries).pvp_damage_permeability == pytest.approx(share)
    assert CharacterService._pvp_damage_type_multiplier(
        stats(entries), "Physical",
    ) == pytest.approx(share)


def test_cyanite_shield_halves_player_damage_again():
    shield = wire_entry(TALENTS.status_effect_wire_id("mage_cyanitskin"))
    assert stats((shield,)).pvp_damage_permeability == pytest.approx(0.5)


def test_crowd_favorite_raises_honor_after_a_battle(ranger):
    definition = TALENTS.definition("fame_focus_talent_01_honorbonus")
    for entry in TALENTS.status_effect_entries(
        definition, target_rank=10, actor_id=1, first_instance_id=50, server_tick=0,
    ):
        ranger.gameplay_persistent_status_effects[entry.instance_id] = entry
    modifiers = evaluate_active_status_modifiers(
        load_status_modifier_catalog(DEFAULT_STATIC_DB_PATH),
        snapshot_player_modifier_sources(ranger),
    )
    assert modifiers.honor_gain.apply(1000) == 1100


def defiance_session(ranger, rank):
    definition = TALENTS.definition("fame_focus_talent_04_playerstundefense")
    for entry in TALENTS.status_effect_entries(
        definition, target_rank=rank, actor_id=1, first_instance_id=60, server_tick=0,
    ):
        ranger.gameplay_persistent_status_effects[entry.instance_id] = entry
    ranger.gameplay_character.class_id = 1
    return ranger


def test_defiance_shortens_stuns_in_an_official_arena(ranger):
    defiance_session(ranger, 20)
    stun = effect("debuff_cc_stun,C:1.0,D:4.0")
    bleed = effect("debuff_dot_bleeding,C:1.0,D:4.0")
    ranger.gameplay_match = SimpleNamespace(phase=MatchPhase.RUNNING, round_index=0)
    shortened, untouched = CharacterService._defiance_durations(
        ranger, ranger.gameplay_combat_state, (stun, bleed),
    )
    assert shortened.duration_ticks == round(stun.duration_ticks * 0.7)
    assert untouched.duration_ticks == bleed.duration_ticks


def test_defiance_stays_out_of_open_play(ranger):
    defiance_session(ranger, 20)
    stun = effect("debuff_cc_stun,C:1.0,D:4.0")
    assert CharacterService._defiance_durations(
        ranger, ranger.gameplay_combat_state, (stun,),
    ) == (stun,)


def bomb_arena(arena):
    case = arena.case
    case.source.gameplay_character.class_id = 4
    learn(case.source, "dwarf_fame_trait_01_nova")
    with patch.object(CharacterService, "_start_monster_chase"):
        talent_events.dispatch_player_death(case.source)
        arena.run_timers("_resolve_player_summon")
    return tuple(case.combat.player_summons.values())


def test_all_four_bombs_appear_when_the_dwarf_dies():
    with _Arena() as arena:
        bombs = bomb_arena(arena)
        assert [bomb.definition.monster_template_id for bomb in bombs] == ["dwarf_bomb"] * 4


def test_a_bomb_hunts_the_enemy_player_and_strikes_once():
    with _Arena() as arena, patch.object(CharacterService, "_start_monster_chase"):
        case = arena.case
        bombs = bomb_arena(arena)
        combat = case.combat
        start = max(float(bomb.spawn_ready_at) for bomb in bombs) + 0.05
        victim = (34.0, 0.0, 53.57)
        case.move(case.target, victim)
        origin = case.source.gameplay_last_move.relative_position
        positions = {
            arena.source_id: arena.frame.wire_to_world(origin),
            arena.target_id: victim,
        }
        targets = (
            (arena.source_id, origin, case.source),
            (arena.target_id, case.target.gameplay_last_move.relative_position, case.target),
        )
        hostile = {arena.source_id: (arena.target_id,)}
        sessions = {arena.source_id: case.source, arena.target_id: case.target}
        before = min(
            combat._distance(bomb.position, victim) for bomb in combat.player_summons.values()
        )
        now, struck = start, ()
        for tick in range(240):
            now += 0.04
            with combat.lock:
                combat.advance_player_summons(
                    elapsed_seconds=0.04, server_tick=300 + tick, now=now,
                    player_positions=positions,
                    hostile_player_actor_ids_by_owner=hostile,
                )
                _gone, _attacks, _monsters, struck, _support = (
                    CharacterService._resolve_player_summon_cycle(
                        combat, targets, positions, server_tick=300 + tick, now=now,
                        hostile_player_actor_ids_by_owner=hostile,
                        player_damage_multipliers={arena.target_id: 1.0},
                        lethal_player_actor_ids_by_owner=hostile,
                    )
                )
            if struck:
                break
        assert struck, "no bomb reached its target"
        hit = struck[0]
        assert hit.damage > 0
        CharacterService._dispatch_pvp_summon_resolutions(combat, sessions, struck)
        assert hit.attacker_actor_id not in combat.player_summons
        assert before > min(
            combat._distance(bomb.position, victim) for bomb in combat.player_summons.values()
        )
