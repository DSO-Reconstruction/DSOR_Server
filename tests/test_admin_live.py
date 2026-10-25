"""Live CMS grants, native notifications and stale-session rejection."""

from dataclasses import replace
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dso_server.tests.test_admin_cms import cms, booking, equipment_booking, login, request
from dso_server.persistence.social_state import SocialStateStore
from dso_server.protocol.player_init_protocol import decode_server_command_batch, CURRENCY_CHANGED_COMMAND
from dso_server.protocol.reward_protocol import NOTIFICATION_COMMAND
from dso_server.protocol.currency_protocol import GAME_CURRENCY_BY_NAME, GAME_CURRENCY_CHANGED_COMMAND
from dso_server.runtime.admin_commands import AdminCommandQueue, track_inventory_reward
from dso_server.runtime.transport import RakNetServer, Session
from dso_server.services.admin_live import BOOKING_GUID_PREFIX
from dso_server.services.character_server import CharacterServer


@pytest.fixture
def online(cms, tmp_path):
    server = CharacterServer("127.0.0.1", port=0, account_store=cms.store,
                             social_store=SocialStateStore(tmp_path / "social.json"))
    s = Session(("127.0.0.1", 49001), Mock())
    s.gameplay_character = next(c for c in server.character_selection.characters if c.character_id == 42)
    s.gameplay_map = "a0200_kingscity"
    saved = cms.store.get(42)
    s.gameplay_inventory = saved.inventory
    s.gameplay_currency_state = saved.currency
    s.gameplay_game_currencies = dict(saved.game_currencies)
    s.gameplay_next_item_id = saved.next_item_id
    s.gameplay_quickbar_info = saved.quickbar_info
    s.gameplay_talent_categories = saved.talent_categories
    s.connected = s.gameplay_mode = s.gameplay_initialization_sent = s.gameplay_presence_ready = True
    s.send_reliable_ordered_split = Mock()
    s.send_reliable_ordered = Mock()
    server.sessions[s.addr] = s
    server.admin_commands = SimpleNamespace(call=lambda operation: operation(), close=lambda: None)
    other = SimpleNamespace(session_snapshot=lambda: (), admin_commands=SimpleNamespace(close=lambda: None))
    cms.service.application = SimpleNamespace(characters=server, login=other)
    return server, s


def commands(s):
    args, kwargs = s.send_reliable_ordered_split.call_args
    return decode_server_command_batch(args[0], kwargs["total_bit_length"])


def test_live_currency_uses_current_balance_and_native_booking_notification(cms, online):
    server, s = online
    s.gameplay_currency_state = replace(s.gameplay_currency_state, currency_type=900)
    row = booking(cms)
    assert row["status"] == "delivered", row
    assert s.gameplay_currency_state.andermant == 925
    assert cms.store.get(42).currency.andermant == 925
    assert cms.store.get(42).admin_booking_ids == (row["id"],)
    batch = commands(s)
    assert batch[0].command_id == CURRENCY_CHANGED_COMMAND
    notice = next(c.value for c in batch if c.command_id == NOTIFICATION_COMMAND).entries[0]
    assert notice.instance_guid == BOOKING_GUID_PREFIX + row["id"].to_bytes(8, "big")
    assert json.loads(notice.payload)["addItems"] is True
    assert json.loads(notice.payload)["templates"] == "money_rc"
    assert booking(cms)["id"] == row["id"]
    assert s.gameplay_currency_state.andermant == 925
    assert s.send_reliable_ordered_split.call_count == 1


@pytest.mark.parametrize("resource", ["gold", "glyph_of_power", "badge_of_honor", "draken", "realm_fragment"])
def test_all_wallet_types_deliver_live(cms, online, resource):
    _, s = online
    row = cms.service.post("/api/bookings", {"character_id": 42, "kind": "currency", "resource": resource,
        "amount": 17, "reason": "Support ticket", "request_id": "live-currency-request"}, cms.admin, "")
    assert row["status"] == "delivered", row
    if resource in GAME_CURRENCY_BY_NAME:
        assert commands(s)[0].command_id == GAME_CURRENCY_CHANGED_COMMAND
    else:
        assert getattr(cms.store.get(42).currency, resource) == getattr(s.gameplay_currency_state, resource)


def test_equipment_configuration_is_applied_to_live_inventory(cms, online):
    _, s = online
    row = equipment_booking(cms, {"item_level": 100, "rarity": 4, "socket_count": 4,
                                  "enchantment_tier": 0, "roll_quality": 100})
    assert row["status"] == "delivered", row
    item = s.gameplay_inventory.items[-1]
    assert item.item_level == 100 and item.field_08 == 4 and item.socket_count == 4
    assert len(item.enchantments) >= 4 and all(e.value == 1 for e in item.enchantments)
    assert len(commands(s)) == 3


def test_stale_currency_preparation_retries_without_lost_gameplay_update(cms, online):
    server, s = online
    def call(operation):
        server.admin_commands.call = lambda op: op()
        s.gameplay_currency_state = replace(s.gameplay_currency_state, currency_type=600)
        return operation()
    server.admin_commands.call = call
    row = booking(cms)
    assert row["status"] == "delivered", row
    assert s.gameplay_currency_state.andermant == 625


@pytest.mark.parametrize("change", ["disconnect", "transition", "replacement", "reward_busy"])
def test_unavailable_session_keeps_booking_pending(cms, online, change):
    server, s = online
    def call(operation):
        if change == "disconnect": s.connected = False
        if change == "transition": s.gameplay_exit_transition_started = True
        if change == "replacement": server.sessions.pop(s.addr)
        if change == "reward_busy": s.gameplay_inventory_rewards_in_progress = 1
        return operation()
    server.admin_commands.call = call
    row = booking(cms)
    assert row["status"] == "pending" and row["error"]
    assert s.gameplay_currency_state.andermant == 100
    s.send_reliable_ordered_split.assert_not_called()


def test_failed_flush_retries_receipt_without_granting_twice(cms, online):
    _, s = online
    with patch.object(cms.store, "flush", side_effect=OSError("disk unavailable")):
        row = booking(cms)
    assert row["status"] == "pending" and "disk unavailable" in row["error"]
    assert s.gameplay_currency_state.andermant == 125
    with pytest.raises(ValueError, match="already applied"):
        cms.service.delivery.cancel("Operator", row["id"], "Cancel request")
    assert cms.service.live.deliver(row["id"])["status"] == "delivered"
    assert s.gameplay_currency_state.andermant == 125
    assert s.send_reliable_ordered_split.call_count == 1


@pytest.mark.parametrize("effect", ["speed", "attack_speed", "stun_immunity"])
def test_effects_are_explicit_idempotent_persistent_and_audited(cms, online, effect):
    _, s = online
    cookie, csrf = login(cms)
    payload = {"effect": effect, "enabled": True, "reason": "Test support action"}
    for _ in range(2):
        code, result, _ = request(cms, "POST", "/api/characters/42/effect", payload, cookie, csrf)
        assert code == 200, result
    assert len(s.gameplay_persistent_status_effects) == 1
    assert len(cms.store.get(42).status_effects) == 1
    live = request(cms, "GET", "/api/characters/42/live", cookie=cookie)[1]
    assert next(e for e in live["effects"] if e["id"] == effect)["enabled"]
    payload["enabled"] = False
    assert request(cms, "POST", "/api/characters/42/effect", payload, cookie, csrf)[0] == 200
    assert not s.gameplay_persistent_status_effects
    assert not cms.store.get(42).status_effects
    assert any(row["action"] == "character.effect" for row in cms.service.repository.history("admin_audit")["rows"])


def test_limited_movement_bonus_remains_enabled_in_cms(cms, online):
    server, session = online
    assert server._set_admin_effect(session, "speed", True)
    entry = next(iter(session.gameplay_persistent_status_effects.values()))
    session.gameplay_persistent_status_effects[entry.field_04] = replace(
        entry, values=(1.0, 0.0, 0.0, 0.0, 5.0),
    )
    cookie, csrf = login(cms)
    live = request(cms, "GET", "/api/characters/42/live", cookie=cookie)[1]
    assert next(effect for effect in live["effects"] if effect["id"] == "speed")["enabled"]
    assert server._set_admin_effect(session, "speed", True)
    assert len(session.gameplay_persistent_status_effects) == 1
    payload = {"effect": "speed", "enabled": False, "reason": "Remove mounted speed bonus"}
    assert request(cms, "POST", "/api/characters/42/effect", payload, cookie, csrf)[0] == 200
    assert not session.gameplay_persistent_status_effects


def test_teleport_uses_native_transfer_and_rejects_unknown_maps(cms, online):
    server, s = online
    server._begin_map_transfer = Mock(return_value=True)
    cookie, csrf = login(cms)
    payload = {"map_id": "a0200_kingscity", "reason": "Move stuck character"}
    assert request(cms, "POST", "/api/characters/42/teleport", payload, cookie, csrf)[0] == 200
    server._begin_map_transfer.assert_called_once_with(s, "a0200_kingscity", reason="admin-cms", gameplay_switch=True, scaling_difficulty=0)
    for invalid in ["../../private", "missing-map"]:
        payload["map_id"] = invalid
        assert request(cms, "POST", "/api/characters/42/teleport", payload, cookie, csrf)[0] == 400
    payload["map_id"], payload["scaling_difficulty"] = "a0200_kingscity", 99
    assert request(cms, "POST", "/api/characters/42/teleport", payload, cookie, csrf)[0] == 400
    server._begin_map_transfer.assert_called_once()


def test_live_routes_require_admin_csrf_and_reason(cms, online):
    payload = {"effect": "speed", "enabled": True, "reason": "Support action"}
    assert request(cms, "GET", "/api/maps")[0] == 401
    assert request(cms, "GET", "/api/characters/42/live")[0] == 401
    assert request(cms, "POST", "/api/characters/42/effect", payload)[0] == 401
    cookie, csrf = login(cms)
    assert request(cms, "POST", "/api/characters/42/effect", payload, cookie)[0] == 403
    payload["reason"] = ""
    assert request(cms, "POST", "/api/characters/42/effect", payload, cookie, csrf)[0] == 400


def test_queue_runs_on_dedicated_worker_and_propagates_errors():
    server = RakNetServer("127.0.0.1", 0, "admin-queue-test")
    events = []
    server._dispatch = Mock(side_effect=lambda *args: events.append("datagram"))
    queue = server.admin_commands = AdminCommandQueue(server)
    server.start()
    try:
        thread_id = queue.call(lambda: (events.append("control"), threading.get_ident())[1])
        assert events == ["control"]
        assert thread_id != threading.get_ident()
        assert thread_id not in {thread.ident for thread in server._threads}
        assert queue.call(lambda: queue.call(threading.get_ident)) == thread_id
        with pytest.raises(ValueError, match="rejected"):
            queue.call(lambda: (_ for _ in ()).throw(ValueError("rejected")))
    finally:
        queue.close()
        server.stop()


def test_reward_counter_is_passive_and_resets_after_failure():
    s = Session(("127.0.0.1", 49003), Mock())
    @track_inventory_reward
    def reward(self, s):
        assert s.gameplay_inventory_rewards_in_progress == 1
        raise ValueError("full")
    with pytest.raises(ValueError, match="full"):
        reward(None, s)
    assert s.gameplay_inventory_rewards_in_progress == 0


def test_stack_booking_rekeys_existing_stack_in_live_packet(cms, online):
    _, s = online
    payload = {"character_id": 42, "kind": "item", "resource": "potion_combined_health_rarity_05", "amount": 3,
               "reason": "Replace lost supplies", "request_id": "stack-booking-first"}
    first = cms.service.post("/api/bookings", payload, cms.admin, "")
    assert first["status"] == "delivered", first
    old = s.gameplay_inventory.items[-1]
    payload["request_id"] = "stack-booking-second"
    second = cms.service.post("/api/bookings", payload, cms.admin, "")
    assert second["status"] == "delivered", second
    new = s.gameplay_inventory.items[-1]
    assert new.amount == 6 and new.item_id != old.item_id
    delta = commands(s)[0].value
    assert delta.field_158 == {new.item_id: old.item_id}
    assert delta.changed_item_ids == [old.item_id]
    assert delta.items[0].amount == 6


def test_booking_notification_acknowledgement_never_credits_again(cms, online):
    from dso_server.protocol.reward_protocol import NotificationAction
    from dso_server.services import commerce
    server, s = online
    row = booking(cms)
    entry = commands(s)[-1].value.entries[0]
    action = NotificationAction(2, 1, entry.kind, entry.instance_guid, entry.values,
                                json.loads(entry.payload), entry.timestamp)
    with patch.object(commerce, "decode_notification_action", return_value=action):
        server._handle_notification_action(s, b"")
        server._handle_notification_action(s, b"")
    assert s.gameplay_currency_state.andermant == 125
    unknown = replace(action, instance_guid=BOOKING_GUID_PREFIX + (row["id"] + 1).to_bytes(8, "big"))
    with patch.object(commerce, "decode_notification_action", return_value=unknown):
        with pytest.raises(ValueError, match="unknown CMS"):
            server._handle_notification_action(s, b"")


def test_full_inventory_stays_pending_without_notification(cms, online):
    _, s = online
    s.gameplay_inventory.storage_size = 0
    row = equipment_booking(cms)
    assert row["status"] == "pending" and "full" in row["error"]
    assert not s.gameplay_inventory.items and not s.gameplay_admin_booking_ids
    s.send_reliable_ordered_split.assert_not_called()


def test_map_change_before_queued_action_is_rejected_and_audited(cms, online):
    server, s = online
    def call(operation):
        s.gameplay_chase_generation += 1
        return operation()
    server.admin_commands.call = call
    with pytest.raises(ValueError, match="changed maps"):
        cms.service.live.action(42, "effect", {"effect": "speed", "enabled": True}, "Operator", "Support ticket")
    assert not s.gameplay_persistent_status_effects
    assert cms.service.repository.history("admin_audit")["rows"][0]["action"] == "character.effect.failed"


def test_live_booking_through_real_game_service_queue(cms, online):
    server, s = online
    server.admin_commands = AdminCommandQueue(server)
    server.start()
    try:
        row = booking(cms)
        assert row["status"] == "delivered", row
        assert s.gameplay_currency_state.andermant == 125
        assert cms.store.get(42).admin_booking_ids == (row["id"],)
        assert server.admin_commands.thread_id != threading.get_ident()
        assert commands(s)[-1].command_id == NOTIFICATION_COMMAND
    finally:
        server.admin_commands.close()
        server.stop()


def test_admin_effects_can_coexist_and_remain_independently_removable(cms, online):
    from dso_server.services.admin_effects import ADMIN_EFFECTS
    from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
    server, s = online
    catalog = load_status_runtime_catalog(server.static_database_path)
    for effect in ADMIN_EFFECTS:
        server._set_admin_effect(s, effect, True)
    assert len(s.gameplay_persistent_status_effects) == 3
    assert server._set_admin_effect(s, "speed", True)
    assert len(s.gameplay_persistent_status_effects) == 3
    assert not server._set_admin_effect(s, "speed", False)
    expected = {catalog.effect_ids[ADMIN_EFFECTS[name]["resource"]]
                for name in ("attack_speed", "stun_immunity")}
    assert {e.effect_id for e in s.gameplay_persistent_status_effects.values()} == expected
    assert {e.effect_id for e in cms.store.get(42).status_effects} == expected


def test_unlock_maps_is_per_character_persistent_idempotent_and_audited(cms, online):
    _, s = online
    s.gameplay_unlocked_maps = {"a0200_kingscity"}
    cookie, csrf = login(cms)
    payload = {"reason": "Enable map testing"}
    for _ in range(2):
        code, result, _ = request(cms, "POST", "/api/characters/42/unlock_maps", payload, cookie, csrf)
        assert code == 200, result
        assert result["all_maps_unlocked"]
    assert s.gameplay_admin_all_maps and cms.store.get(42).admin_all_maps
    assert s.gameplay_unlocked_maps == {"a0200_kingscity"}
    assert request(cms, "GET", "/api/characters/42/live", cookie=cookie)[1]["all_maps_unlocked"]
    assert any(row["action"] == "character.unlock_maps"
               for row in cms.service.repository.history("admin_audit")["rows"])


def test_unlock_maps_requires_admin_csrf_reason_and_current_session(cms, online):
    server, s = online
    path, payload = "/api/characters/42/unlock_maps", {"reason": "Enable map testing"}
    assert request(cms, "POST", path, payload)[0] == 401
    cookie, csrf = login(cms)
    assert request(cms, "POST", path, payload, cookie)[0] == 403
    assert request(cms, "POST", path, {"reason": ""}, cookie, csrf)[0] == 400
    def changed_session(operation):
        s.gameplay_exit_transition_started = True
        return operation()
    server.admin_commands.call = changed_session
    assert request(cms, "POST", path, payload, cookie, csrf)[0] == 400
    assert not s.gameplay_admin_all_maps and not cms.store.get(42).admin_all_maps


def test_live_cms_andermant_is_shared_across_owned_characters(cms, online):
    from dso_server.tests.test_admin_cms import state
    server,s=online
    cms.registry.claim_character("Player",44)
    cms.store.put(state(44))
    cms.store.account_registry=cms.registry
    s.gameplay_currency_store=cms.store
    result=booking(cms)
    assert result["status"] == "delivered"
    assert s.gameplay_currency_state.andermant == 125
    assert cms.store.get(44).currency.andermant == 125
    # The same request/receipt cannot credit the account twice.
    booking(cms)
    assert cms.store.get(44).currency.andermant == 125


@pytest.mark.parametrize('operation,changes', [
    ('set_identity', {'name': 'RenamedHero', 'level': 2}),
    ('set_privileges', {'admin_name_color': True}),
])
def test_live_character_fields_are_published_under_mutation_gate(cms, online, operation, changes):
    import builtins
    import copy

    server, session = online
    # Gameplay owns a detached character copy, as it does after initialization.
    session.gameplay_character = copy.copy(session.gameplay_character)
    session.gameplay_talent_category_points = (999, 2, 3, 4)
    original_setattr = builtins.setattr
    written = []

    def checked_setattr(target, name, value):
        if target is session.gameplay_character:
            assert session.gameplay_mutation_lock._is_owned()
            written.append(name)
        original_setattr(target, name, value)

    def refresh(*_args, **_kwargs):
        # Refresh queues its own operation; do not wait on it inside the
        # identity commit while retaining the player's mutation gate.
        assert not session.gameplay_mutation_lock._is_owned()
        return True

    with patch('dso_server.services.admin.setattr', side_effect=checked_setattr, create=True), \
            patch.object(cms.service.live, 'refresh_progress', side_effect=refresh):
        result = getattr(cms.service, operation)(42, changes, cms.admin, 'Update character')
    assert result['gameplay_sessions'] == 1
    assert set(written) == set(changes)
    for name, value in changes.items():
        assert getattr(session.gameplay_character, name) == value
    if 'level' in changes:
        assert session.gameplay_talent_category_points == (
            session.gameplay_character.progression_u32, 2, 3, 4,
        )


def test_identity_publication_rechecks_character_after_queue_wait(cms, online):
    server, session = online
    replacement = SimpleNamespace(character_id=999, name='OtherHero')

    def replaced(operation):
        session.gameplay_character = replacement
        return operation()

    server.admin_commands.call = replaced
    result = cms.service.set_identity(42, {'name': 'RenamedHero'}, cms.admin, 'Update character')
    assert result['gameplay_sessions'] == 0
    assert replacement.name == 'OtherHero'
