"""Admin speed respects the client's four-times-base Move command range."""

from dataclasses import replace
from unittest.mock import patch

import pytest

from dso_server.gameplay.local_mount_state import (
    BLACK_BEAR_MOUNT, begin_mount_cast, begin_mount_ride, mount_status_effect,
)
from dso_server.gameplay.player_combat_stats import calculate_player_combat_stats
from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import (
    decode_monster_spawn_batch, decode_status_effect_batch,
)
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo, ItemEnchantmentInfo
from dso_server.runtime.transport import Session
from dso_server.services.admin_effects import ADMIN_EFFECTS, matching_admin_effects
from dso_server.services.character_server import CharacterServer
from dso_server.tests import test_admin_speed as fixtures
from dso_server.tests import test_session_lock_hot_paths as snapshot_fixtures


@pytest.fixture
def case():
    test = fixtures.AdminSpeedTests(methodName="runTest")
    test.setUp()
    try:
        yield test
    finally:
        test.doCleanups()


def equipment(capped):
    inventory = InventoryInfo()
    if capped:
        # Enough authored movement enchantments to reach the ordinary cap.
        inventory.items.append(ItemInfo(
            item_id=700, template_id="mage_base_boots_crafted", item_level=100,
            enchantments=[ItemEnchantmentInfo(template_id="rune_speed_movement_04") for _ in range(25)],
        ))
        inventory.equipment_slots[700] = (4,)
    return inventory


def stats(session):
    return calculate_player_combat_stats(
        DEFAULT_STATIC_DB_PATH, class_name="mage", level=session.gameplay_character.level,
        inventory=session.gameplay_inventory,
        modifier_sources=tuple(session.gameplay_persistent_status_effects.values()),
    )


def admin_entry(session):
    catalog = load_status_runtime_catalog(DEFAULT_STATIC_DB_PATH)
    preset = ADMIN_EFFECTS["speed"]
    entries = matching_admin_effects(session.gameplay_persistent_status_effects.values(),
                                     catalog.effect_ids[preset["resource"]], preset)
    assert len(entries) == 1
    return entries[0]


def mount(case):
    session = case.home
    session.gameplay_mount_cast = begin_mount_cast(
        BLACK_BEAR_MOUNT, instance_id=session.gameplay_next_status_effect_instance_id,
        start_tick=100,
    )
    session.gameplay_next_status_effect_instance_id += 1
    with patch.object(CharacterServer, "is_session_current", return_value=True):
        case.gameplay_server._complete_mount_cast(session, session.gameplay_mount_generation)


@pytest.mark.parametrize("capped,level,expected", [(False, 1, 5.0), (True, 100, 1.0)])
@pytest.mark.parametrize("mounted_first", [True, False])
def test_mount_and_admin_toggle_preserve_full_bonus_until_limit(case, capped, level, expected, mounted_first):
    session = case.home
    session.gameplay_inventory = equipment(capped)
    session.gameplay_character.level = level
    assert stats(session).movement_speed == (13.0 if capped else 5.0)
    if mounted_first:
        mount(case)
        case.command("!speed")
    else:
        case.command("!speed")
        assert admin_entry(session).values[0] == 5.0
        mount(case)

    entry = admin_entry(session)
    assert entry.values[0] == expected
    current = stats(session)
    assert (current.movement_speed + current.movement_bonus_speed) / current.base_movement_speed <= 4.0
    data, bits, _ = session.recorder.messages[-1]
    updates = decode_monster_spawn_batch(data, bits).commands
    if mounted_first or capped:
        assert any(e == entry for command in updates for e in command.value.entries)
    # Reconciliation is idempotent and keeps a single enabled toggle.
    count = len(session.recorder.messages)
    assert case.gameplay_server._refresh_admin_speed(session) is None
    assert len(session.recorder.messages) == count
    assert admin_entry(session) == entry

    case.gameplay_server._remove_mount_ride(session, reason="combat skill started")
    assert admin_entry(session).values == ADMIN_EFFECTS["speed"]["values"]
    assert session.gameplay_mount_ride is None
    assert "disabled" in case.command("!speed")
    assert not session.gameplay_persistent_status_effects


def test_equipping_and_removing_speed_gear_while_mounted_reconciles_bonus(case):
    session = case.home
    session.gameplay_inventory = equipment(False)
    mount(case)
    case.command("!speed")
    old = admin_entry(session)
    session.gameplay_inventory = equipment(True)
    case.gameplay_server._refresh_player_combat_vitals(session)
    new = admin_entry(session)
    assert new.values == (1.0, 0.0, 0.0, 0.0, 5.0)
    update = next((data, bits) for data, bits, label in reversed(session.recorder.messages)
                  if label == "admin-speed-adjust")
    status, actor = decode_status_effect_batch(*update)
    assert actor == 1
    assert status.entries[0].field_04 == old.field_04 and not status.entries[0].has_details
    assert status.entries[1] == new and new.field_04 != old.field_04
    session.gameplay_inventory = equipment(False)
    case.gameplay_server._refresh_player_combat_vitals(session)
    assert admin_entry(session).values[0] == 5.0


def test_fractional_headroom_survives_wire_rounding_without_repeated_updates(case):
    session = case.home
    session.gameplay_inventory = equipment(True)
    session.gameplay_inventory.items[0].enchantments = session.gameplay_inventory.items[0].enchantments[:17]
    mount(case)
    case.command("!speed")
    entry = admin_entry(session)
    assert 3.474 < entry.values[0] <= 3.475
    payload, bits, _label = session.recorder.messages[-1]
    wire = decode_status_effect_batch(payload, bits)[0].entries[0]
    assert wire.values == entry.values
    current = stats(session)
    assert current.movement_speed + current.movement_bonus_speed <= 4 * current.base_movement_speed
    for _ in range(3):
        assert case.gameplay_server._refresh_admin_speed(session) is None


def test_limited_bonus_can_be_disabled_while_still_mounted(case):
    session = case.home
    session.gameplay_inventory = equipment(True)
    mount(case)
    ride = session.gameplay_mount_ride
    case.command("!speed")
    assert admin_entry(session).values[0] == 1.0
    assert case.gameplay_server._set_admin_effect(session, "speed", True)
    assert "disabled" in case.command("!speed")
    assert session.gameplay_mount_ride == ride
    assert list(session.gameplay_persistent_status_effects) == [ride.instance_id]


def test_mounted_relog_recomputes_limit_before_initial_status_snapshot():
    test = snapshot_fixtures.AccountSnapshotLockTests(methodName="runTest")
    test.setUp()
    try:
        session = test.session
        session.connected = True
        session.gameplay_presence_ready = True
        session.gameplay_inventory = equipment(True)
        cast = begin_mount_cast(BLACK_BEAR_MOUNT,
            instance_id=session.gameplay_next_status_effect_instance_id, start_tick=8000)
        ride = begin_mount_ride(cast, instance_id=cast.instance_id + 1)
        session.gameplay_next_status_effect_instance_id += 2
        session.gameplay_mount_ride = ride
        mount_entry = mount_status_effect(ride, actor_id=1, active=True).entries[0]
        session.gameplay_persistent_status_effects[mount_entry.field_04] = mount_entry
        assert test.server._toggle_admin_speed(session)
        assert admin_entry(session).values[0] == 1.0
        # A native movement buff also reaches the cap after the initializer
        # rebuilds bootstrap equipment from the selected character record.
        native = replace(admin_entry(session),
            field_04=session.gameplay_next_status_effect_instance_id,
            effect_id=load_status_runtime_catalog(DEFAULT_STATIC_DB_PATH).effect_ids["pvp_talent_speed"],
            values=(2.0, 0.0, 0.0, 0.0, 0.0))
        session.gameplay_next_status_effect_instance_id += 1
        session.gameplay_persistent_status_effects[native.field_04] = native
        test.server._persist_gameplay_session(session, reason="mounted speed regression", flush=True)
        # Toggle persistence stores the limited instance and the ride.
        saved = test.server.account_store.get(session.gameplay_character.character_id)
        assert saved.mount_ride == ride
        assert admin_entry(session) in saved.status_effects
        restored = Session(("127.0.0.1", 52319), test.sock)
        restored.connected = True
        restored.gameplay_mode = True
        restored.gameplay_map = session.gameplay_map
        restored.gameplay_character = session.gameplay_character
        restored.gameplay_template_row_index = session.gameplay_template_row_index
        test.server._send_local_player_initialization(restored)
        assert restored.gameplay_mount_ride is not None
        assert admin_entry(restored).values[0] == 1.0
        assert admin_entry(restored).field_08 == 0
        assert not test.server._toggle_admin_speed(restored)
        assert restored.gameplay_mount_ride is not None
    finally:
        test.doCleanups()
