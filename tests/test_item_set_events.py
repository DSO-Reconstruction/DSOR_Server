"""Current authored set events fire only after authoritative transitions."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dso_server.gameplay.item_set_procs import collect_trigger_rules, resolve_for_trigger
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_player_only_encounter
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence import readonly_db
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.item_set_protocol import ItemSetDatabase
from dso_server.services.base import CharacterService
from dso_server.services import item_set_events
from dso_server.tests.test_health_globe_pickup import world, place_globe, set_health
from dso_server.tests.test_pvp_matchmaking import _session


@pytest.fixture(scope="module")
def sets():
    return ItemSetDatabase(DEFAULT_STATIC_DB_PATH)


def evaluate(sets, resource, count=None):
    definition = next(x for x in sets.definitions if x.set_id == resource)
    parts = tuple(part.split("|")[0] for part in definition.parts)
    return sets.evaluate(parts[:count], class_name="warrior")


def load_effect(resource):
    return LocalCombatState._load_victim_status_effects(
        readonly_db.connect(DEFAULT_STATIC_DB_PATH), resource,
    )[0]


def resolved(evaluation, event):
    return resolve_for_trigger(
        evaluation, DEFAULT_STATIC_DB_PATH, trigger=event,
        loader=LocalCombatState._load_victim_status_effects,
        rng=SimpleNamespace(random=lambda: 0.0),
    )


@pytest.fixture
def wearer():
    s = _session(52261, 261, "Set wearer")
    s.gameplay_combat_state = LocalCombatState(
        load_local_player_only_encounter(DEFAULT_STATIC_DB_PATH, "a0200_kingscity"),
        player_actor_id=s.gameplay_remote_actor_id, player_health=1000,
    )
    s.gameplay_inventory = InventoryInfo(health_points=1000, secondary_resource=100)
    s.gameplay_chase_server_tick = 200
    s.gameplay_presence_callback = None
    s.gameplay_last_move = None
    s.gameplay_spawn_position = (0.0, 0.0, 0.0)
    s.send_reliable_ordered = Mock()
    s.gameplay_deadline_timer = lambda delay, callback, args=(), kwargs=None: SimpleNamespace(
        start=Mock(), cancel=Mock(), daemon=False, delay=delay,
        callback=callback, args=args, kwargs=kwargs or {},
    )
    yield s
    for timer in s.gameplay_projectile_timers.values():
        timer.cancel()


@pytest.mark.parametrize("set_id,event,effect,duration", [
    ("set_warrior_dragan_01_T3", "hitblocked", "item_e_dragan_block_proc", 125),
    ("set_warrior_ingredient_cave", "pickupingredientitem", "itemset_ingredient_cave_buff", 3000),
    ("set_ce_2025_warrior", "costskillresource", "ce2025_instinct_awakening", 125),
    ("set_cny2026_warrior", "maxstacksizeearthquake", "set_cny2026_warrior_earthquake_rp", 0),
])
def test_current_authored_event_rules_preserve_duration_and_program_marker(
        sets, set_id, event, effect, duration):
    values = resolved(evaluate(sets, set_id), event)
    definition = next(x for x in values if x.resource_id == effect)
    assert definition.duration_ticks == duration
    assert definition.item_set_program


def test_ammon_and_alliance_tiers_upgrade_original_globe_parameters(sets):
    for set_id, resource in (("set_warrior_ammon_T1", "item_ammon_set_invigorate"),
                             ("set_warrior_alliance_set", "item_alliance_set_refresh")):
        first = resolved(evaluate(sets, set_id, 1), "pickuphealthglobe")
        third = resolved(evaluate(sets, set_id, 3), "pickuphealthglobe")
        assert len(first) == len(third) == 1
        assert first[0].resource_id == third[0].resource_id == resource
        assert first[0].values[:3] == (0.15, 0.0, 0.0)
        assert third[0].values[:3] == (0.15, 0.15, 0.3)


def test_location_globe_proc_has_actual_receiver_damage_and_is_not_a_wearer_effect(sets, wearer):
    wearer.gameplay_item_set_evaluation = evaluate(sets, "set_warrior_alliance_set")
    with patch.object(CharacterService, "_send_player_skill_status_effects") as users, \
         patch.object(CharacterService, "_schedule_player_location_visuals") as locations, \
         patch("dso_server.services.endless_tower_buffs.tower_damage_range", return_value=(100, 120)):
        item_set_events.dispatch_item_set_event(wearer, "pickuphealthglobe", server_tick=200)
    assert len(users.call_args.args[1]) == 2
    assert all(x.item_set_target == "user" for x in users.call_args.args[1])
    shell = locations.call_args.args[2][0]
    assert shell.resource_id == "item_alliance_set_explosion_aura"
    assert shell.item_set_target == "location"
    assert locations.call_args.kwargs["receiver_status_effects"]
    assert locations.call_args.kwargs["player_damage_range"] == (100, 120)


def test_live_status_modifiers_enable_authored_zero_chance_rules(sets, wearer):
    evaluation = evaluate(sets, "set_blackdwarf2026_dwarf")
    off = resolved(evaluation, "maxstacksizesimpleshot30")
    assert [x.resource_id for x in off] == ["SetLight2026_dwarf_buff_4_clean"]
    enabling = load_effect("SetLight2026_dwarf_buff_4_30_despawn_aura_trig_effect")
    with patch.object(CharacterService, "_active_skill_modifier_sources", return_value=(enabling,)):
        active = item_set_events.active_item_set_evaluation(wearer, evaluation, DEFAULT_STATIC_DB_PATH)
    on = resolved(active, "maxstacksizesimpleshot30")
    assert len(on) == 3
    assert next(x for x in on if x.resource_id.endswith("explo_aura")).delay_ticks == 12
    assert len(resolved(evaluation, "maxstacksizesimpleshot30")) == 1


def test_block_bridge_works_without_any_socket_rules_and_does_not_duplicate_hit(sets, wearer):
    wearer.gameplay_item_set_evaluation = evaluate(sets, "set_warrior_dragan_01_T3")
    assert wearer.gameplay_socket_effect_evaluation is None
    with patch.object(CharacterService, "_send_player_skill_status_effects") as apply:
        CharacterService._dispatch_socket_event(wearer, "hitblocked", server_tick=200)
        CharacterService._dispatch_socket_event(wearer, "hit", server_tick=200)
    assert apply.call_count == 1
    definition = apply.call_args.args[1][0]
    assert definition.resource_id == "item_e_dragan_block_proc"
    assert definition.values[:2] == (0.01, 0.01)


def test_low_health_enters_once_and_ends_authored_parent_on_recovery(sets, wearer):
    wearer.gameplay_item_set_evaluation = evaluate(sets, "set_warrior_temple_01")
    parent = load_effect("item_temple_set_bonus")
    rule = next(x for x in collect_trigger_rules(wearer.gameplay_item_set_evaluation)
                if x.resource_id == parent.resource_id)
    assert rule.off_trigger == "notlowhealth"
    with patch.object(CharacterService, "_send_player_skill_status_effects") as apply, \
         patch.object(CharacterService, "_end_player_runtime_effects") as end:
        for health in (1000, 300, 200, 500):
            CharacterService._update_low_health_socket_state(
                wearer, current_health=health, maximum_health=1000, server_tick=200,
            )
    assert apply.call_count == 1
    assert apply.call_args.args[1][0].effect_id == parent.effect_id
    assert end.call_count == 1
    assert end.call_args.args[1](parent)
    assert not end.call_args.args[1](load_effect("ce2025_instinct_awakening"))


def test_low_health_parent_enables_only_the_equipped_temple_torso_guardian(sets, wearer):
    evaluation = evaluate(sets, "set_warrior_temple_01")
    assert resolved(evaluation, "hit") == ()
    parent = load_effect("item_temple_set_bonus")
    with patch.object(CharacterService, "_active_skill_modifier_sources", return_value=(parent,)):
        active = item_set_events.active_item_set_evaluation(wearer, evaluation, DEFAULT_STATIC_DB_PATH)
    effects = resolved(active, "hit")
    assert [x.resource_id for x in effects] == ["item_temple_set_fire_guardian"]
    with patch.object(CharacterService, "_active_skill_modifier_sources", return_value=()):
        inactive = item_set_events.active_item_set_evaluation(wearer, evaluation, DEFAULT_STATIC_DB_PATH)
    assert resolved(inactive, "hit") == ()


def test_real_twentieth_cny_stack_starts_hidden_skill_effect_once(sets, wearer):
    wearer.gameplay_item_set_evaluation = evaluate(sets, "set_cny2026_warrior")
    skill_index = readonly_db.connect(DEFAULT_STATIC_DB_PATH).execute(
        "SELECT rowid-1 FROM _Template_Skill WHERE Id='angrystrike'",
    ).fetchone()[0]
    # The set's modUE enables angrystrike's own ``ON:Hit`` entry; the skill
    # column is its single delivery and delivery runs it as a set program.
    stack = next(x for x in wearer.gameplay_combat_state.player_skill_user_effects(
        skill_index, cast_id=1, trigger="hit",
        modifier_effect_ids=tuple(CharacterService._active_skill_modifier_sources(wearer)),
    ) if x.resource_id == "set_cny2026_warrior_earthquake_dmg")
    assert not CharacterService._item_set_procs_for_skill(wearer, skill_index).get("user")
    followup = load_effect("set_cny2026_warrior_earthquake_rp")
    for index in range(21):
        CharacterService._send_player_skill_status_effects(
            wearer, (stack,), server_tick=200 + index, label="test-cny-stack",
        )
        matches = [entry for entry, _ in wearer.gameplay_runtime_status_effects.values()
                   if entry.effect_id == followup.effect_id]
        assert len(matches) == (1 if index >= 19 else 0)
        if index == 19:
            occurrence = matches[0].field_04
        if index == 20:
            assert matches[0].field_04 == occurrence
    assert any(definition.resource_id == stack.resource_id
               for definition in wearer.gameplay_item_set_programs.values())
    # An unrelated equipment refresh must preserve still-equipped skill procs.
    from dso_server.services.item_set_programs import reconcile_item_set_programs
    before = dict(wearer.gameplay_runtime_status_effects)
    reconcile_item_set_programs(wearer)
    assert wearer.gameplay_runtime_status_effects == before
    from dso_server.services.item_set_programs import stop_item_set_effects
    stop_item_set_effects(wearer, (stack.effect_id,), server_tick=230, reason="done")
    assert not any(entry.effect_id in {stack.effect_id, followup.effect_id}
                   for entry, _ in wearer.gameplay_runtime_status_effects.values())


def test_temple_alternative_torsos_count_as_one_part(sets):
    definition = next(x for x in sets.definitions if x.set_id == "set_warrior_temple_01")
    first_four = definition.parts[:4]
    alternatives = definition.parts[4].split("|")
    for torso in alternatives:
        active = sets.evaluate((*first_four, torso), class_name="warrior")
        assert max(x.equipped_parts for x in active.active_tiers
                   if x.set_id == definition.set_id) == 5
        assert any(x.trigger == "lowhealth" for x in collect_trigger_rules(active))
    # Even an invalid synthetic loadout with all variants contributes one part.
    active = sets.evaluate((*first_four, *alternatives), class_name="warrior")
    assert max(x.equipped_parts for x in active.active_tiers
               if x.set_id == definition.set_id) == 5


@pytest.mark.parametrize("change", ["none", "unequip", "transfer"])
def test_delayed_max_stack_event_obeys_authored_delay_and_session_lifetime(sets, wearer, change):
    wearer.gameplay_item_set_evaluation = evaluate(sets, "set_blackdwarf2026_dwarf")
    with patch.object(CharacterService, "_send_player_skill_status_effects") as apply:
        item_set_events.dispatch_item_set_event(wearer, "maxstacksizesimpleshot30", server_tick=200)
        apply.assert_not_called()
        timers = list(wearer.gameplay_projectile_timers.values())
        assert len(timers) == 1
        timer = timers[0]
        assert timer.delay == 12 / 25
        if change == "unequip":
            wearer.gameplay_item_set_evaluation = sets.evaluate(())
        elif change == "transfer":
            wearer.gameplay_chase_generation += 1
        timer.callback(*timer.args, **timer.kwargs)
        timer.callback(*timer.args, **timer.kwargs)
    assert apply.call_count == (1 if change == "none" else 0)
    if change == "none":
        assert apply.call_args.kwargs["server_tick"] == 212


@pytest.mark.parametrize("resource,event,cap", [
    ("set_cny2026_warrior_earthquake_dmg", "maxstacksizeearthquake", 20),
    ("SetLight2026_dwarf_SimpleShot_10", "maxstacksizesimpleshot10", 10),
    ("SetLight2026_dwarf_SimpleShot_20", "maxstacksizesimpleshot20", 20),
    ("SetLight2026_dwarf_SimpleShot_30", "maxstacksizesimpleshot30", 30),
])
def test_authored_stack_alias_fires_once_at_crossing(wearer, resource, event, cap):
    definition = load_effect(resource)
    assert definition.max_stack_size == cap
    with patch.object(item_set_events, "dispatch_item_set_event") as dispatch:
        for previous, current in ((0, 1), (cap - 2, cap - 1), (cap - 1, cap), (cap, cap)):
            item_set_events.dispatch_item_set_max_stack(
                wearer, definition, previous, current, server_tick=200,
            )
    assert dispatch.call_count == 1
    assert dispatch.call_args.args[1] == event


@pytest.mark.parametrize("resource,available,expected", [
    ("frozenorb", 100, 1), ("frozenorb", 0, 0), ("magicmissile", 100, 0),
])
def test_resource_event_requires_real_accepted_cost(wearer, resource, available, expected):
    combat = wearer.gameplay_combat_state
    combat.register_player(wearer.gameplay_remote_actor_id, 1000,
                           maximum_resource=100, current_resource=available)
    index = readonly_db.connect(DEFAULT_STATIC_DB_PATH).execute(
        "SELECT rowid-1 FROM _Template_Skill WHERE Id=?", (resource,),
    ).fetchone()[0]
    with patch.object(item_set_events, "dispatch_item_set_event") as dispatch, \
         patch.object(CharacterService, "_request_account_save"):
        CharacterService._apply_player_skill_resource(
            wearer, combat, skill_index=index, modifier_sources=(), now=100,
        )
    assert dispatch.call_count == expected
    if expected:
        assert combat.player_resource[wearer.gameplay_remote_actor_id][0] < available


def test_paid_resource_event_survives_outbound_failure(wearer):
    combat = wearer.gameplay_combat_state
    combat.register_player(wearer.gameplay_remote_actor_id, 1000,
                           maximum_resource=100, current_resource=100)
    index = readonly_db.connect(DEFAULT_STATIC_DB_PATH).execute(
        "SELECT rowid-1 FROM _Template_Skill WHERE Id='frozenorb'",
    ).fetchone()[0]
    wearer.send_reliable_ordered.side_effect = OSError("send failed")
    with patch.object(item_set_events, "dispatch_item_set_event") as dispatch:
        with pytest.raises(OSError, match="send failed"):
            CharacterService._apply_player_skill_resource(
                wearer, combat, skill_index=index, modifier_sources=(), now=100,
            )
    assert dispatch.call_count == 1


def test_globe_pickup_only_fires_after_consumption_and_cannot_replay(world, sets):
    s = world.session
    s.gameplay_item_set_evaluation = evaluate(sets, "set_warrior_ammon_T1", 3)
    maximum = s.gameplay_combat_state.player_maximum_health
    item_id = place_globe(s)
    with patch.object(CharacterService, "_send_player_skill_status_effects") as apply:
        set_health(s, maximum)
        assert not world.server._pickup_world_item(s, item_id, automatic=True)
        assert apply.call_count == 0
        set_health(s, maximum // 2)
        assert world.server._pickup_world_item(s, item_id, automatic=True)
        assert not world.server._pickup_world_item(s, item_id, automatic=True)
    assert apply.call_count == 1
    assert apply.call_args.args[1][0].values[:3] == (0.15, 0.15, 0.3)


def test_ingredient_pickup_only_fires_after_capacity_check_and_cannot_replay(world, sets):
    from dso_server.services import world as world_service
    s = world.session
    s.gameplay_item_set_evaluation = evaluate(sets, "set_warrior_ingredient_cave")
    item_id = place_globe(s, "activated_stone_essence")
    s.gameplay_inventory = InventoryInfo()
    with patch.object(CharacterService, "_send_player_skill_status_effects") as apply:
        with patch.object(world_service, "add_world_drop_to_inventory", side_effect=ValueError("full")):
            assert not world.server._pickup_world_item(s, item_id, automatic=False)
        assert item_id in s.gameplay_world_loot
        assert apply.call_count == 0
        assert world.server._pickup_world_item(s, item_id, automatic=False)
        assert not world.server._pickup_world_item(s, item_id, automatic=False)
    assert apply.call_count == 1
    assert apply.call_args.args[1][0].resource_id == "itemset_ingredient_cave_buff"
    assert apply.call_args.args[1][0].duration_ticks == 3000
