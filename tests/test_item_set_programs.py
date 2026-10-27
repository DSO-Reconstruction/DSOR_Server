"""Authored persistent set programs change combat stats and stop cleanly."""

from types import SimpleNamespace
from threading import Event, Thread
from unittest.mock import Mock, patch

import pytest

from dso_server.gameplay.item_set_procs import resolve_for_trigger
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_player_only_encounter
from dso_server.gameplay.player_combat_stats import calculate_player_combat_stats
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import decode_status_effect_command_batch
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.item_set_protocol import ItemSetDatabase
from dso_server.services.base import CharacterService
from dso_server.tests.test_pvp_matchmaking import _session


MORTIS_DAMAGE = "item_pw_mortis_increase_variable_damage"
MORTIS_HEALTH = "item_pw_mortis_set_health_debuff"
MORTIS_SHELLS = {
    "item_pw_mortis_set_variable_damage_trigger": "item_pw_mortis_set_damage_buff",
    "item_pw_mortis_set_variable_max_hp_trigger": "item_pw_mortis_set_variable_max_hp",
}


@pytest.fixture(scope="module")
def sets():
    return ItemSetDatabase(DEFAULT_STATIC_DB_PATH)


def evaluation(sets, set_id):
    definition = next(value for value in sets.definitions if value.set_id == set_id)
    return sets.evaluate(definition.parts, class_name="mage")


def effects(sets, set_id="set_mage_pw_mortis", *, event="kill", roll=1.0):
    return resolve_for_trigger(
        evaluation(sets, set_id), DEFAULT_STATIC_DB_PATH, trigger=event,
        loader=LocalCombatState._load_victim_status_effects,
        rng=SimpleNamespace(random=lambda: roll),
    )


@pytest.fixture
def players(sets):
    first = _session(52101, 101, "Mortis wearer")
    second = _session(52102, 102, "Other player")
    combat = LocalCombatState(
        load_local_player_only_encounter(DEFAULT_STATIC_DB_PATH, "a0200_kingscity"),
        player_actor_id=first.gameplay_remote_actor_id, player_health=1000,
    )
    combat.register_player(second.gameplay_remote_actor_id, 1000)
    timers = []

    def timer(delay, function, args=(), kwargs=None):
        value = SimpleNamespace(
            delay=delay, function=function, args=args, kwargs=kwargs or {},
            cancel=Mock(), start=Mock(), daemon=False,
        )
        timers.append(value)
        return value

    for session in (first, second):
        session.gameplay_inventory = InventoryInfo()
        session.gameplay_combat_state = combat
        session.gameplay_deadline_timer = timer
        session.gameplay_presence_callback = None
        session.gameplay_chase_server_tick = 200
    first.gameplay_item_set_evaluation = evaluation(sets, "set_mage_pw_mortis")
    return first, second, timers


def stats(session):
    return calculate_player_combat_stats(
        DEFAULT_STATIC_DB_PATH, class_name="mage", level=100,
        inventory=session.gameplay_inventory,
        modifier_sources=CharacterService._active_skill_modifier_sources(session),
    )


def running(session):
    names = session.gameplay_combat_state._status_effect_ids
    by_id = {value: key for key, value in names.items()}
    return {by_id[entry.effect_id]: (entry, expiry)
            for entry, expiry in session.gameplay_runtime_status_effects.values()}


def kill(session, definitions, count=1):
    for index in range(count):
        CharacterService._send_player_skill_status_effects(
            session, definitions, server_tick=200 + index, label="test-item-set-program",
        )


def test_one_mortis_kill_applies_persistent_variables_and_managed_children_once(sets, players):
    wearer, other, timers = players
    before, other_before = stats(wearer), stats(other)
    definitions = effects(sets)
    assert {definition.resource_id for definition in definitions} == {
        *MORTIS_SHELLS, MORTIS_DAMAGE, MORTIS_HEALTH,
    }
    kill(wearer, definitions)
    after = stats(wearer)
    assert after.minimum_damage == pytest.approx(before.minimum_damage * 1.1)
    assert after.maximum_damage == pytest.approx(before.maximum_damage * 1.1)
    assert after.maximum_health == pytest.approx(before.maximum_health * 0.95)
    assert stats(other) == other_before  # Both players share the same combat map.
    active = running(wearer)
    assert set(MORTIS_SHELLS.values()) <= set(active)
    assert all(expiry is None for _entry, expiry in active.values())
    assert not timers
    wire_entries = [entry for packet, bits, label in wearer.recorder.messages
                    if label == "test-item-set-program"
                    for command in decode_status_effect_command_batch(packet, bits).commands
                    for entry in command.value.entries if entry.has_details]
    assert wire_entries
    assert all(entry.field_39 and entry.field_10 == 0 and entry.field_0c == 0
               for entry in wire_entries)


def test_mortis_persistent_variable_stacks_stop_at_authored_fifty(sets, players):
    wearer, _other, timers = players
    before = stats(wearer)
    definitions = effects(sets)
    kill(wearer, definitions, 50)
    capped = stats(wearer)
    assert capped.minimum_damage == pytest.approx(before.minimum_damage * 6.0)
    for name in (MORTIS_DAMAGE, MORTIS_HEALTH):
        entry, expiry = running(wearer)[name]
        assert entry.field_18 + 1 == 50
        assert expiry is None
    kill(wearer, definitions, 5)
    assert stats(wearer) == capped
    assert all(running(wearer)[name][0].field_18 + 1 == 50
               for name in (MORTIS_DAMAGE, MORTIS_HEALTH))
    assert not timers


@pytest.mark.parametrize("rare_trigger", (False, True))
def test_failed_child_publication_keeps_all_earned_programs_and_vitals(sets, players, rare_trigger):
    from dso_server.services.item_set_persistence import persistent_item_set_entries

    wearer, _other, _timers = players
    before = stats(wearer)
    with patch.object(wearer, "send_reliable_ordered", side_effect=OSError("output unavailable")):
        with pytest.raises(OSError, match="output unavailable"):
            kill(wearer, effects(sets, roll=0.0 if rare_trigger else 1.0))
    expected = {*MORTIS_SHELLS, *MORTIS_SHELLS.values(), MORTIS_DAMAGE, MORTIS_HEALTH}
    persistent_count = len(expected)
    if rare_trigger:
        expected.update(("item_pw_mortis_set_kill_trigger", "item_pw_mortis_set_kill_trigger_stun"))
        entry, _ = running(wearer)["item_pw_mortis_set_kill_trigger"]
        assert ("item-set-program-expire", entry.field_04) in wearer.gameplay_projectile_timers
    assert set(running(wearer)) == expected
    assert {definition.resource_id for definition in wearer.gameplay_item_set_programs.values()} == expected
    with wearer.flow_lock:
        saved = persistent_item_set_entries(wearer)
    assert len(saved) == persistent_count
    after = stats(wearer)
    assert after.minimum_damage == pytest.approx(before.minimum_damage * 1.1)
    assert after.maximum_health == pytest.approx(before.maximum_health * 0.95)
    assert wearer.gameplay_combat_state.player_health[wearer.gameplay_remote_actor_id][1] == round(after.maximum_health)
    from dso_server.services.item_set_programs import reconcile_item_set_programs
    wearer.gameplay_item_set_evaluation = None
    reconcile_item_set_programs(wearer)
    assert not expected.intersection(running(wearer))
    assert not wearer.gameplay_item_set_programs


def test_stopping_mortis_shells_removes_managed_children_and_their_stats(sets, players):
    from dso_server.services.item_set_programs import stop_item_set_effects

    wearer, _other, _timers = players
    before = stats(wearer)
    kill(wearer, effects(sets), 3)
    names = wearer.gameplay_combat_state._status_effect_ids
    stop_item_set_effects(
        wearer, tuple(names[name] for name in MORTIS_SHELLS),
        server_tick=300, reason="stop",
    )
    active = running(wearer)
    assert not set(MORTIS_SHELLS).intersection(active)
    assert not set(MORTIS_SHELLS.values()).intersection(active)
    after = stats(wearer)
    assert after.minimum_damage == pytest.approx(before.minimum_damage)
    assert after.maximum_health == pytest.approx(before.maximum_health)


def test_mortis_terminal_program_runs_death_cleanup_once(sets, players):
    from dso_server.services.player_respawn import _DEATH_DIALOG_TIMER_KEY

    wearer, _other, _timers = players
    wearer.gameplay_respawn_allowed = True
    baseline = stats(wearer)
    kill(wearer, effects(sets), 3)
    terminal = next(value for value in effects(sets, roll=0.0)
                    if value.resource_id == "item_pw_mortis_set_kill_trigger")
    kill(wearer, (terminal,))
    entry, _ = running(wearer)[terminal.resource_id]
    timer = wearer.gameplay_projectile_timers[("item-set-program-expire", entry.field_04)]
    notifier = CharacterService._notify_player_death
    with patch.object(CharacterService, "_notify_player_death", wraps=notifier) as notify:
        timer.function(*timer.args)
        timer.function(*timer.args)
    assert notify.call_count == 1
    assert wearer.gameplay_combat_state.player_health[wearer.gameplay_remote_actor_id][0] == 0
    assert wearer.gameplay_inventory.health_points == 0
    assert wearer.gameplay_death_pending
    assert _DEATH_DIALOG_TIMER_KEY in wearer.gameplay_projectile_timers
    assert not ({*MORTIS_SHELLS, *MORTIS_SHELLS.values(), MORTIS_DAMAGE, MORTIS_HEALTH}
                & set(running(wearer)))
    assert stats(wearer).minimum_damage == pytest.approx(baseline.minimum_damage)


def test_terminal_state_finishes_when_nested_cleanup_packet_cannot_be_sent(sets, players):
    wearer, _other, _timers = players
    kill(wearer, effects(sets), 3)
    terminal = next(value for value in effects(sets, roll=0.0)
                    if value.resource_id == "item_pw_mortis_set_kill_trigger")
    kill(wearer, (terminal,))
    entry, _ = running(wearer)[terminal.resource_id]
    timer = wearer.gameplay_projectile_timers[("item-set-program-expire", entry.field_04)]
    with patch.object(wearer, "send_reliable_ordered", side_effect=OSError("output unavailable")):
        try:
            timer.function(*timer.args)
        except OSError:
            pass
    assert wearer.gameplay_combat_state.player_health[wearer.gameplay_remote_actor_id][0] == 0
    assert wearer.gameplay_inventory.health_points == 0
    assert wearer.gameplay_death_pending
    assert terminal.resource_id not in running(wearer)


def test_replaced_timed_set_effect_ignores_its_canceled_expiry_callback(sets, players):
    wearer, _other, _timers = players
    definition, = effects(sets, "set_mage_01_worlddrop")
    kill(wearer, (definition,))
    old_entry, _ = running(wearer)[definition.resource_id]
    old_timer = wearer.gameplay_projectile_timers[("item-set-program-expire", old_entry.field_04)]
    kill(wearer, (definition,))
    new_entry, _ = running(wearer)[definition.resource_id]
    assert new_entry.field_04 != old_entry.field_04
    old_timer.cancel.assert_called_once()
    old_timer.function(*old_timer.args)
    assert running(wearer)[definition.resource_id][0] == new_entry
    new_timer = wearer.gameplay_projectile_timers[("item-set-program-expire", new_entry.field_04)]
    new_timer.function(*new_timer.args)
    assert definition.resource_id not in running(wearer)


def test_terminal_death_does_not_report_a_kill_into_a_replacement_match(sets, players):
    wearer, _other, _timers = players
    wearer.gameplay_match, wearer.gameplay_match_callback = object(), Mock()
    original_callback = wearer.gameplay_match_callback
    terminal = next(value for value in effects(sets, roll=0.0)
                    if value.resource_id == "item_pw_mortis_set_kill_trigger")
    kill(wearer, (terminal,))
    entry, _ = running(wearer)[terminal.resource_id]
    timer = wearer.gameplay_projectile_timers[("item-set-program-expire", entry.field_04)]

    def handoff(*_args, **_kwargs):
        with wearer.flow_lock:
            wearer.gameplay_chase_generation += 1
            wearer.gameplay_match, wearer.gameplay_match_callback = object(), Mock()

    with patch.object(CharacterService, "_notify_player_death", side_effect=handoff):
        timer.function(*timer.args)
    original_callback.assert_not_called()
    wearer.gameplay_match_callback.assert_not_called()


def test_old_map_expiry_cannot_remove_new_map_timer_reusing_instance_id(sets, players):
    wearer, _other, _timers = players
    definition, = effects(sets, "set_mage_01_worlddrop")
    kill(wearer, (definition,))
    old_entry, _ = running(wearer)[definition.resource_id]
    key = ("item-set-program-expire", old_entry.field_04)
    old_timer = wearer.gameplay_projectile_timers[key]
    # Map initialization replaces runtime effects and allocates a fresh local
    # status namespace. A canceled timer may already be queued by the worker.
    with wearer.flow_lock:
        wearer.gameplay_chase_generation += 1
        wearer.gameplay_runtime_status_effects.clear()
        wearer.gameplay_item_set_programs.clear()
        wearer.gameplay_projectile_timers.clear()
        wearer.gameplay_next_status_effect_instance_id = old_entry.field_04
    kill(wearer, (definition,))
    new_timer = wearer.gameplay_projectile_timers[key]
    old_timer.function(*old_timer.args)
    assert wearer.gameplay_projectile_timers.get(key) is new_timer
    assert definition.resource_id in running(wearer)


def test_map_handoff_between_expiry_check_and_cleanup_preserves_new_effect(sets, players):
    from dso_server.services import item_set_programs

    wearer, _other, _timers = players
    definition, = effects(sets, "set_mage_01_worlddrop")
    kill(wearer, (definition,))
    old_entry, _ = running(wearer)[definition.resource_id]
    key = ("item-set-program-expire", old_entry.field_04)
    old_timer = wearer.gameplay_projectile_timers[key]
    reached_cleanup, release_cleanup = Event(), Event()
    original_stop = item_set_programs.stop_item_set_effects

    def delayed_stop(*args, **kwargs):
        reached_cleanup.set()
        assert release_cleanup.wait(3), "test did not release the old expiry"
        return original_stop(*args, **kwargs)

    with patch.object(item_set_programs, "stop_item_set_effects", side_effect=delayed_stop):
        worker = Thread(target=lambda: old_timer.function(*old_timer.args), daemon=True)
        worker.start()
        try:
            assert reached_cleanup.wait(3), "expiry did not reach cleanup"
            with wearer.flow_lock:
                wearer.gameplay_chase_generation += 1
                wearer.gameplay_runtime_status_effects.clear()
                wearer.gameplay_item_set_programs.clear()
                wearer.gameplay_projectile_timers.clear()
                wearer.gameplay_next_status_effect_instance_id = old_entry.field_04
            kill(wearer, (definition,))
            current_entry, _ = running(wearer)[definition.resource_id]
            current_timer = wearer.gameplay_projectile_timers[key]
        finally:
            release_cleanup.set()
            worker.join(3)
    assert not worker.is_alive()
    assert running(wearer).get(definition.resource_id, (None,))[0] == current_entry
    assert wearer.gameplay_projectile_timers.get(key) is current_timer


def test_old_terminal_program_cannot_kill_a_character_after_map_handoff(sets, players):
    from dso_server.services import item_set_programs

    wearer, _other, _timers = players
    terminal = next(value for value in effects(sets, roll=0.0)
                    if value.resource_id == "item_pw_mortis_set_kill_trigger")
    kill(wearer, (terminal,))
    entry, _ = running(wearer)[terminal.resource_id]
    timer = wearer.gameplay_projectile_timers[("item-set-program-expire", entry.field_04)]
    reached_program, release_program = Event(), Event()
    original_program = item_set_programs._terminal_programs

    def delayed_program(*args, **kwargs):
        reached_program.set()
        assert release_program.wait(3), "test did not release terminal execution"
        return original_program(*args, **kwargs)

    new_combat = LocalCombatState(
        wearer.gameplay_combat_state.encounter,
        player_actor_id=wearer.gameplay_remote_actor_id, player_health=1000,
    )
    with patch.object(item_set_programs, "_terminal_programs", side_effect=delayed_program):
        worker = Thread(target=lambda: timer.function(*timer.args), daemon=True)
        worker.start()
        try:
            assert reached_program.wait(3), "expiry did not reach its terminal program"
            with wearer.flow_lock:
                wearer.gameplay_chase_generation += 1
                wearer.gameplay_combat_state = new_combat
                wearer.gameplay_runtime_status_effects.clear()
                wearer.gameplay_item_set_programs.clear()
                wearer.gameplay_projectile_timers.clear()
                wearer.gameplay_inventory.health_points = 1000
                wearer.gameplay_death_pending = False
        finally:
            release_program.set()
            worker.join(3)
    assert not worker.is_alive()
    assert new_combat.player_health[wearer.gameplay_remote_actor_id][0] > 0
    assert wearer.gameplay_inventory.health_points > 0
    assert not wearer.gameplay_death_pending


@pytest.mark.parametrize("set_id,event,resource,seconds,values", (
    ("set_mage_pw_mortis", "kill", "item_pw_mortis_set_kill_trigger", 1.36, None),
    ("set_mage_01_worlddrop", "kill", "item_speedboost", 5.0, None),
    ("set_mh_rings", "hit", "mh_rings_set_aura", 5.0, (-0.5,)),
))
def test_zero_template_duration_preserves_authored_timed_rule_override(
        sets, set_id, event, resource, seconds, values):
    definition = next(value for value in effects(sets, set_id, event=event, roll=0.0)
                      if value.resource_id == resource)
    assert definition.duration_ticks == round(seconds * 25)
    if values:
        assert definition.values[:len(values)] == values
