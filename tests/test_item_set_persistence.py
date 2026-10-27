"""Persistent earned set occurrences survive the real account/init pipeline."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import pytest

from dso_server.gameplay.item_set_procs import resolve_for_trigger
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_player_only_encounter
from dso_server.gameplay.player_combat_stats import calculate_player_combat_stats
from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence.account_state import _state_from_document, _state_to_document
from dso_server.protocol.inventory_codec import ItemInfo
from dso_server.runtime.transport import Session
from dso_server.services.base import CharacterService
from dso_server.services.item_set_persistence import restore_persistent_item_set_effects
from dso_server.tests.fixtures.characters import character_server


SET = "set_mage_pw_mortis"
DAMAGE = "item_pw_mortis_increase_variable_damage"
PARENT = "item_pw_mortis_set_variable_damage_trigger"
CHILD = "item_pw_mortis_set_damage_buff"


def initialize(server, session=None):
    session = session or Session(("127.0.0.1", 47891), Mock())
    session.gameplay_character = server.character_selection.active_character
    session.gameplay_map = "a0200_kingscity"
    state = server._resolve_initialization_character(session)
    server._resolve_initialization_inventory(state)
    server._encode_initialization_new_player(state)
    server._resolve_initialization_talents(state)
    server._resolve_initialization_persistent_effects(session, state)
    server._resolve_initialization_vitals(state)
    server._apply_initialization_state(session, state)
    session.connected = True
    session.send_reliable_ordered = Mock()
    session.gameplay_deadline_timer = lambda delay, callback, args=(), kwargs=None: SimpleNamespace(
        delay=delay, callback=callback, args=args, start=Mock(), cancel=Mock(),
    )
    session.gameplay_remote_actor_id = CharacterService._remote_actor_id(session.gameplay_character.character_id)
    session.gameplay_combat_state = LocalCombatState(
        load_local_player_only_encounter(DEFAULT_STATIC_DB_PATH, "a0200_kingscity"),
        player_actor_id=session.gameplay_remote_actor_id, player_health=10000,
    )
    return session, state


@pytest.fixture
def wearer():
    case = unittest.TestCase()
    try:
        server = character_server(case)
        session, _ = initialize(server)
        definition = next(value for value in server.item_set_database.definitions if value.set_id == SET)
        for template in definition.parts:
            slots = server.static_database.item_equipment_slots(template)
            inventory = session.gameplay_inventory
            for old_id, old_slots in tuple(inventory.equipment_slots.items()):
                if set(slots).intersection(old_slots):
                    inventory.equipment_slots.pop(old_id)
                    inventory.storage_58[old_id] = max(inventory.storage_58.values(), default=-1) + 1
            item_id = session.gameplay_next_item_id
            session.gameplay_next_item_id += 1
            inventory.items.append(ItemInfo(item_id, template_id=template, item_level=100))
            inventory.equipment_slots[item_id] = slots
        session.gameplay_item_set_evaluation = server.item_set_database.evaluate_inventory(
            session.gameplay_inventory, class_name="mage",
        )
        yield server, session
    finally:
        case.doCleanups()


def trigger(session, count=1, *, terminal=False):
    definitions = resolve_for_trigger(
        session.gameplay_item_set_evaluation, DEFAULT_STATIC_DB_PATH, trigger="kill",
        loader=LocalCombatState._load_victim_status_effects,
        rng=SimpleNamespace(random=lambda: 0.0 if terminal else 1.0),
    )
    if terminal:
        definitions = tuple(value for value in definitions if value.resource_id == "item_pw_mortis_set_kill_trigger")
    for _ in range(count):
        CharacterService._send_player_skill_status_effects(session, definitions, server_tick=200, label="test-set-persistence")


def running(session):
    names = load_status_runtime_catalog(DEFAULT_STATIC_DB_PATH).effect_names
    return {names[entry.effect_id]: entry for entry, expiry in session.gameplay_runtime_status_effects.values()
            if expiry is None}


def save(server, session):
    state = server._capture_account_state(session)
    return _state_from_document(_state_to_document(state))


def stats(session):
    return calculate_player_combat_stats(
        DEFAULT_STATIC_DB_PATH, class_name="mage", level=100,
        inventory=session.gameplay_inventory,
        modifier_sources=CharacterService._active_skill_modifier_sources(session),
    )


@pytest.mark.parametrize("same_session", [False, True])
def test_saved_occurrences_keep_counts_and_managed_children_on_relog_or_map_handoff(wearer, same_session):
    server, session = wearer
    trigger(session, 4)
    before = running(session)
    saved = save(server, session)
    assert set(saved.status_effect_names.values()) >= set(before)
    assert {entry.effect_id for entry in saved.status_effects} >= {entry.effect_id for entry in before.values()}
    server.account_store.put(saved, flush=True)
    restored, state = initialize(server, session if same_session else None)
    after = running(restored)
    assert set(after) == set(before)
    assert {name: (entry.field_18, entry.values) for name, entry in after.items()} == {
        name: (entry.field_18, entry.values) for name, entry in before.items()
    }
    assert len(restored.gameplay_item_set_programs) == 6
    assert all(entry.field_08 == entry.field_0c == entry.field_10 == 0 for entry in after.values())
    assert set(entry.field_04 for entry in after.values()) <= set(state.persistent_status_effects)
    assert not set(restored.gameplay_item_set_programs).intersection(restored.gameplay_persistent_status_effects)
    restore_persistent_item_set_effects(restored, state, DEFAULT_STATIC_DB_PATH)
    assert running(restored) == after
    sources = CharacterService._active_skill_modifier_sources(restored)
    for entry in after.values():
        assert sum(getattr(source, "effect_id", None) == entry.effect_id for source in sources) == 1
    trigger(restored)
    assert running(restored)[DAMAGE].field_18 == before[DAMAGE].field_18 + 1


def test_capture_only_persists_tracked_native_effects_with_no_expiry(wearer):
    server, session = wearer
    trigger(session)
    entries = running(session)
    tracked_id = entries[DAMAGE].field_04
    original = session.gameplay_runtime_status_effects[tracked_id]
    session.gameplay_runtime_status_effects[tracked_id] = (original[0], 9999999999.0)
    session.gameplay_runtime_status_effects[987654] = (replace(original[0], field_04=987654), None)
    saved = save(server, session)
    ids = {entry.field_04 for entry in saved.status_effects}
    assert tracked_id not in ids
    assert 987654 not in ids
    assert entries[PARENT].field_04 in ids


def test_saved_template_names_rebind_renumbered_effect_ids(wearer):
    server, session = wearer
    trigger(session, 3)
    expected = running(session)
    saved = save(server, session)
    set_ids = {entry.field_04 for entry in expected.values()}
    saved = replace(saved, status_effects=tuple(
        replace(entry, effect_id=60000) if entry.field_04 in set_ids else entry
        for entry in saved.status_effects
    ))
    server.account_store.put(saved, flush=True)
    restored, _ = initialize(server)
    assert {name: entry.effect_id for name, entry in running(restored).items()} == {
        name: entry.effect_id for name, entry in expected.items()
    }


def test_unequipped_saved_sources_are_removed_before_vitals_and_wire(wearer):
    server, session = wearer
    trigger(session, 4)
    old_effect_ids = {entry.effect_id for entry in running(session).values()}
    saved = save(server, session)
    templates = next(value.parts for value in server.item_set_database.definitions if value.set_id == SET)
    for item in saved.inventory.items:
        if item.template_id in templates:
            saved.inventory.equipment_slots.pop(item.item_id, None)
            saved.inventory.storage_58[item.item_id] = max(saved.inventory.storage_58.values(), default=-1) + 1
    server.account_store.put(saved, flush=True)
    restored, state = initialize(server)
    assert not running(restored)
    assert not restored.gameplay_item_set_programs
    assert not any(entry.effect_id in old_effect_ids for entry in state.persistent_status_effects.values())
    assert state.maximum_health == stats(restored).maximum_health
    assert not any(entry.effect_id in old_effect_ids for entry in save(server, restored).status_effects)


@pytest.mark.parametrize("missing", [PARENT, CHILD])
def test_restore_does_not_create_missing_managed_effects_or_restore_orphans(wearer, missing):
    server, session = wearer
    trigger(session)
    saved = save(server, session)
    removed_id = running(session)[missing].field_04
    saved = replace(saved, status_effects=tuple(entry for entry in saved.status_effects if entry.field_04 != removed_id))
    server.account_store.put(saved, flush=True)
    restored, _ = initialize(server)
    assert CHILD not in running(restored)
    assert (PARENT in running(restored)) == (missing != PARENT)


def test_equipped_set_without_earned_occurrences_grants_nothing_on_login(wearer):
    server, session = wearer
    server.account_store.put(save(server, session), flush=True)
    restored, _ = initialize(server)
    assert SET in restored.gameplay_item_set_evaluation.active_set_ids
    assert not running(restored)
    assert not restored.gameplay_item_set_programs


def test_terminal_death_after_restore_cannot_resurrect_saved_occurrences(wearer):
    server, session = wearer
    trigger(session, 3)
    server.account_store.put(save(server, session), flush=True)
    restored, _ = initialize(server)
    trigger(restored, terminal=True)
    timer = next(timer for key, timer in restored.gameplay_projectile_timers.items()
                 if key[0] == "item-set-program-expire")
    timer.callback(*timer.args)
    assert restored.gameplay_death_pending
    assert not running(restored)
    saved = save(server, restored)
    assert not any(name.startswith("item_pw_mortis_") for name in saved.status_effect_names.values())
    server.account_store.put(saved, flush=True)
    rejoined, _ = initialize(server)
    assert not running(rejoined)
