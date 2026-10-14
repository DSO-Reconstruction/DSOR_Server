"""Exercise CMS access control, real HTTP boundaries and durable grant delivery."""

import copy
from dataclasses import replace
import http.client
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence.account_registry import AccountRegistry
from dso_server.persistence.account_state import AccountState, AccountStateStore, CharacterProfile
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.player_init_protocol import CurrencyChanged, QuickbarInfo, TalentCategorySelection
from dso_server.services.admin import AdminError, AdminService


def profile(cid=42, admin=False):
    return CharacterProfile(cid, "AdminHero" if admin else "PlayerHero", "a0200_kingscity",
                            1, 0, 0, 0, 0, 0, admin_privileges=admin)


def state(cid=42):
    return AccountState(cid, "a0200_kingscity", None, InventoryInfo(),
                        CurrencyChanged(100, 200, (3, 0, 4), 500, False), 0, 10000,
                        QuickbarInfo(2, 0), {}, (), None,
                        talent_categories=tuple(TalentCategorySelection(0, (), ()) for _ in range(4)))


@pytest.fixture
def cms(tmp_path):
    registry = AccountRegistry(tmp_path / "accounts.sqlite3", password_iterations=10000)
    store = AccountStateStore(tmp_path / "players.sqlite3")
    admin = registry.register("Operator", "secret123", character_ids=[43])
    player = registry.register("Player", "secret123", character_ids=[42])
    store.put_profile(profile(43, True))
    store.put_profile(profile())
    store.put(state(), flush=True)
    service = AdminService(registry=registry, store=store, static_database_path=DEFAULT_STATIC_DB_PATH, port=0)
    service.start()
    try:
        yield SimpleNamespace(service=service, registry=registry, store=store, admin=admin, player=player)
    finally:
        service.stop()
        store.close()
        registry.close()


def request(cms, method, path, payload=None, cookie=None, csrf=None, headers=None):
    values = dict(headers or {})
    if cookie:
        values["Cookie"] = cookie
    if csrf is not None:
        values["X-CSRF-Token"] = csrf
    body = None
    if payload is not None:
        body = json.dumps(payload)
        values.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection("127.0.0.1", cms.service.port, timeout=5)
    try:
        connection.request(method, path, body, values)
        response = connection.getresponse()
        raw = response.read()
        result = json.loads(raw) if "application/json" in response.getheader("Content-Type", "") else raw
        return response.status, result, dict(response.getheaders())
    finally:
        connection.close()


def login(cms, username="Operator"):
    status, data, headers = request(cms, "POST", "/api/login", {"username": username, "password": "secret123"})
    assert status == 200
    return headers["Set-Cookie"].split(";", 1)[0], data["csrf"]


def booking(cms, *, kind="currency", resource="andermant", amount=25, request_id="unique-request-0001"):
    return cms.service.post("/api/bookings", dict(character_id=42, kind=kind, resource=resource,
                            amount=amount, reason="Support ticket 123", request_id=request_id), cms.admin, "")


@pytest.mark.parametrize("path", ["status", "accounts", "characters", "characters/42", "bookings", "items", "sessions", "audit", "currencies"])
def test_every_data_route_requires_admin_session(cms, path):
    assert request(cms, "GET", "/api/" + path)[0] == 401


def test_player_and_game_session_cannot_enter_cms(cms):
    assert request(cms, "POST", "/api/login", {"username": "Player", "password": "secret123"})[0] == 401
    game = cms.registry.login("Operator", "secret123")
    assert request(cms, "GET", "/api/status", cookie=f"{cms.service.cookie_name}={game.session_id}")[0] == 401


def test_cookie_csrf_origin_logout_and_login_do_not_create_game_sessions(cms):
    cookie, csrf = login(cms)
    assert cms.registry.count_active_sessions() == 0
    assert request(cms, "GET", "/api/status", cookie=cookie)[0] == 200
    assert request(cms, "POST", "/api/logout", {}, cookie)[0] == 403
    assert request(cms, "POST", "/api/logout", {}, cookie, csrf, {"Origin": "http://evil.invalid"})[0] == 403
    assert request(cms, "POST", "/api/logout", {}, cookie, csrf)[0] == 200
    assert request(cms, "GET", "/api/status", cookie=cookie)[0] == 401
    status, _, headers = request(cms, "POST", "/api/login", {"username": "Operator", "password": "secret123"})
    assert status == 200
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]


def test_cross_origin_login_and_form_posts_are_rejected(cms):
    data = {"username": "Operator", "password": "secret123"}
    assert request(cms, "POST", "/api/login", data, headers={"Origin": "null"})[0] == 403
    assert request(cms, "POST", "/api/login", data, headers={"Content-Type": "text/plain"})[0] == 415
    assert request(cms, "POST", "/api/login", ["not an object"])[0] == 400
    assert request(cms, "POST", "/api/login", {"password": "x" * 20000})[0] == 413


def test_login_throttle(cms):
    for _ in range(8):
        assert request(cms, "POST", "/api/login", {"username": "Operator", "password": "bad"})[0] == 401
    assert request(cms, "POST", "/api/login", {"username": "Operator", "password": "secret123"})[0] == 429


@pytest.mark.parametrize("change", ["password", "disabled", "admin", "character_role"])
def test_access_rechecked_after_external_account_or_character_change(cms, change):
    cookie, _ = login(cms)
    if change == "character_role":
        cms.store.put_profile(replace(profile(43, True), admin_privileges=False))
    elif change == "password":
        cms.registry.set_password("Operator", "secret123", "changed123")
    else:
        cms.service.repository.update_account("local", cms.admin.account_id,
            {change: change == "disabled"}, "external change")
    assert request(cms, "GET", "/api/status", cookie=cookie)[0] == 401


def test_explicit_role_and_disabled_override(cms):
    cms.service.repository.update_account("local", cms.player.account_id, {"admin": True}, "promotion")
    assert login(cms, "Player")
    cms.service.repository.update_account("local", cms.admin.account_id, {"admin": False}, "demotion")
    assert request(cms, "POST", "/api/login", {"username": "Operator", "password": "secret123"})[0] == 401


def test_account_edits_audited_atomically_without_password_or_hash(cms):
    cookie, csrf = login(cms)
    game = cms.registry.login("Player", "secret123")
    status, _, _ = request(cms, "POST", f"/api/accounts/{cms.player.account_id}",
        {"changes": {"username": "Renamed", "password": "newsecret", "disabled": True}, "reason": "Support reset"}, cookie, csrf)
    assert status == 200
    assert cms.registry.account("Renamed").disabled
    assert cms.registry.validate(game.session_id) is None
    audit = request(cms, "GET", "/api/audit", cookie=cookie)[1]
    encoded = json.dumps(audit)
    assert "Support reset" in encoded and "[reset]" in encoded
    assert "newsecret" not in encoded and "password_hash" not in encoded
    status, _, _ = request(cms, "POST", f"/api/accounts/{cms.admin.account_id}",
        {"changes": {"admin": False}, "reason": "Bad change"}, cookie, csrf)
    assert status == 400


def test_account_rename_conflict_rolls_back_changes(cms):
    cookie, csrf = login(cms)
    status, _, _ = request(cms, "POST", f"/api/accounts/{cms.player.account_id}",
        {"changes": {"username": "Operator", "disabled": True}, "reason": "conflict"}, cookie, csrf)
    assert status == 409
    assert not cms.registry.account("Player").disabled


def test_pages_catalog_and_snapshot_are_available_without_bearer_tokens(cms):
    cms.registry.login("Player", "secret123")
    cookie, _ = login(cms)
    for path in ["status", "accounts", "characters", "bookings", "audit", "currencies", "items?q=potion", "sessions"]:
        status, data, _ = request(cms, "GET", "/api/" + path, cookie=cookie)
        assert status == 200, (path, data)
        assert "session_id" not in json.dumps(data) and "password_hash" not in json.dumps(data)
    status, detail, _ = request(cms, "GET", "/api/characters/42", cookie=cookie)
    assert status == 200 and detail["state"]["currencies"]["andermant"] == 100
    for asset in ["/", "/admin.js", "/admin.css"]:
        status, body, headers = request(cms, "GET", asset)
        assert status == 200 and body
        assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert request(cms, "GET", "/api/accounts?limit=100000", cookie=cookie)[0] == 400


def test_booking_idempotency_and_persistence_after_restart(cms):
    row = booking(cms)
    assert booking(cms)["id"] == row["id"]
    with pytest.raises(ValueError, match="different booking"):
        booking(cms, amount=26)
    assert cms.store.get(42).currency.andermant == 100
    cms.service.delivery.deliver(42)
    cms.service.delivery.deliver(42)
    assert cms.store.get(42).currency.andermant == 125
    loaded = AccountStateStore(cms.store.path)
    try:
        assert loaded.get(42).currency.andermant == 125
        assert loaded.get(42).admin_booking_ids == (row["id"],)
    finally:
        loaded.close()
    assert cms.service.repository.history("admin_bookings")["rows"][0]["status"] == "delivered"


def test_receipt_prevents_duplicate_after_acknowledgement_failure(cms):
    row = booking(cms)
    with patch.object(cms.service.repository, "finish_booking", side_effect=RuntimeError("interrupted")):
        with pytest.raises(RuntimeError):
            cms.service.delivery.deliver(42)
    assert cms.service.repository.pending(42)
    cms.service.delivery.deliver(42)
    assert cms.store.get(42).currency.andermant == 125
    assert cms.store.get(42).admin_booking_ids == (row["id"],)
    assert not cms.service.repository.pending(42)


@pytest.mark.parametrize("amount", [0, -1, True, 1.2, "20", 1000000001])
def test_invalid_booking_amount_rejected(cms, amount):
    with pytest.raises(ValueError):
        booking(cms, amount=amount)


def test_currency_overflow_stays_pending_without_losing_other_rewards(cms):
    saved = state()
    saved.currency = replace(saved.currency, currency_type=0xFFFFFFFF)
    cms.store.put(saved)
    booking(cms)
    booking(cms, resource="gold", request_id="another-request-0001")
    cms.service.delivery.deliver(42)
    assert cms.store.get(42).currency.andermant == 0xFFFFFFFF
    assert cms.store.get(42).currency.gold == 225
    assert "limit" in cms.service.repository.pending(42)[0]["error"]


def test_item_booking_uses_catalog_and_real_inventory_allocation(cms):
    booking(cms, kind="item", resource="potion_combined_health_rarity_05", amount=2)
    cms.service.delivery.deliver(42)
    saved = cms.store.get(42)
    assert sum(i.amount for i in saved.inventory.items) == 2
    assert saved.inventory.storage_58
    assert saved.next_item_id > 10000


def test_full_inventory_does_not_publish_partial_items_and_retries(cms):
    saved = state()
    saved.inventory.storage_size = 0
    cms.store.put(saved)
    booking(cms, kind="item", resource="potion_combined_health_rarity_05", amount=2)
    cms.service.delivery.deliver(42)
    assert not cms.store.get(42).inventory.items
    assert "full" in cms.service.repository.pending(42)[0]["error"]
    saved.inventory.storage_size = 10
    cms.store.put(saved)
    cms.service.delivery.deliver(42)
    assert sum(i.amount for i in cms.store.get(42).inventory.items) == 2


def test_cancel_and_changed_ownership_never_deliver(cms):
    row = booking(cms)
    cms.service.post(f"/api/bookings/{row['id']}/cancel", {"reason": "Duplicate request"}, cms.admin, "")
    cms.service.delivery.deliver(42)
    assert cms.store.get(42).currency.andermant == 100
    booking(cms, request_id="next-request-000001")
    cms.registry.release_character(42)
    cms.registry.claim_character("Operator", 42)
    cms.service.delivery.deliver(42)
    assert cms.store.get(42).currency.andermant == 100
    assert "ownership" in cms.service.repository.pending(42)[0]["error"]


def test_new_character_without_state_has_explicit_booking_error(cms):
    with pytest.raises(ValueError, match="enter the game once"):
        cms.service.post("/api/bookings", {"character_id": 43, "reason": "test case"}, cms.admin, "")


def test_stale_autosave_cannot_overwrite_delivered_grant(cms):
    older_snapshot = cms.store.get(42)
    booking(cms)
    cms.service.delivery.deliver(42)
    cms.store.put(older_snapshot, flush=True)
    assert cms.store.get(42).currency.andermant == 125
    current = cms.store.get(42)
    current.currency = replace(current.currency, currency_type=120)
    cms.store.put(current, flush=True)
    assert cms.store.get(42).currency.andermant == 120


def test_cannot_cancel_grant_committed_before_interrupted_ack(cms):
    row = booking(cms)
    with patch.object(cms.service.repository, "finish_booking", side_effect=RuntimeError("interrupted")):
        with pytest.raises(RuntimeError):
            cms.service.delivery.deliver(42)
    with pytest.raises(ValueError, match="already delivered"):
        cms.service.delivery.cancel("Operator", row["id"], "Cancellation")
    assert cms.service.repository.booking(row["id"])["status"] == "delivered"
    assert cms.store.get(42).currency.andermant == 125


def test_failed_flush_is_retried_before_ack_and_never_duplicates(cms):
    row = booking(cms)
    with patch.object(cms.store, "flush", side_effect=TimeoutError("disk unavailable")):
        with pytest.raises(TimeoutError):
            cms.service.delivery.deliver(42)
    assert cms.service.repository.booking(row["id"])["status"] == "pending"
    cms.service.delivery.deliver(42)
    assert cms.store.get(42).currency.andermant == 125
    assert cms.service.repository.booking(row["id"])["status"] == "delivered"


def test_keyed_currency_delivery_and_uint64_gold(cms):
    initial = cms.store.get(42).game_currencies["gilded_clover"]
    booking(cms, resource="gilded_clover")
    booking(cms, resource="gold", request_id="gold-request-000001")
    saved = cms.store.get(42)
    saved.currency = replace(saved.currency, amount=0xFFFFFFFF)
    cms.store.put(saved)
    cms.service.delivery.deliver(42)
    assert cms.store.get(42).game_currencies["gilded_clover"] == initial + 25
    assert cms.store.get(42).currency.gold == 0xFFFFFFFF + 25


def test_application_composes_and_stops_dedicated_http_service(tmp_path, monkeypatch):
    from dso_server.app import ServerApplication
    from dso_server.runtime.transport import RakNetServer
    # Keep UDP listeners and runtime background diagnostics out of this HTTP test.
    for key in list(__import__("os").environ):
        if key.startswith("DSO_"):
            monkeypatch.delenv(key)
    with patch.object(RakNetServer, "start"), patch.object(RakNetServer, "stop"):
        app = ServerApplication(host="127.0.0.1", autosave_interval=0,
            player_database_file=tmp_path / "players.sqlite3",
            account_registry_file=tmp_path / "accounts.sqlite3",
            social_state_file=tmp_path / "social.json",
            account_service_port=0, admin_service_port=0)
        app.stall_watchdog.start = Mock()
        app.garbage_collector.install = Mock()
        app._print_banner = Mock()
        try:
            app.start()
            assert app.admin.registry is app.account_registry is app.accounts.registry
            assert app.admin.store is app.account_store
            assert app.admin.port != app.accounts.port
            assert app.characters.admin_booking_delivery is app.admin.delivery
            assert app.login.admin_booking_delivery is app.admin.delivery
            assert request(SimpleNamespace(service=app.admin), "GET", "/")[0] == 200
            admin_thread = app.admin._thread
        finally:
            app.stop()
        assert not admin_thread.is_alive()


def test_admin_bind_failure_rolls_back_started_http_service(tmp_path, monkeypatch):
    from dso_server.app import ServerApplication
    from dso_server.runtime.transport import RakNetServer
    monkeypatch.setenv("DSO_ADMIN_SERVICE", "1")
    monkeypatch.setenv("DSO_ACCOUNT_SERVICE", "1")
    with patch.object(RakNetServer, "start"), patch.object(RakNetServer, "stop") as stop:
        app = ServerApplication(autosave_interval=0,
            player_database_file=tmp_path / "players.sqlite3",
            account_registry_file=tmp_path / "accounts.sqlite3",
            social_state_file=tmp_path / "social.json",
            account_service_port=0, admin_service_port=0)
        try:
            with patch.object(app.admin, "start", side_effect=OSError("port unavailable")):
                with pytest.raises(OSError):
                    app.start()
            assert app.accounts._httpd is None
            assert stop.call_count == len(app.services)
            assert not app._started
        finally:
            app.account_store.close()
            app.account_registry.close()


def test_game_initialization_loads_grant_and_persistence_keeps_receipt(cms, tmp_path):
    from dso_server.services.character_server import CharacterServer
    from dso_server.persistence.social_state import SocialStateStore
    from dso_server.runtime.transport import Session
    server = CharacterServer("127.0.0.1", account_store=cms.store,
        social_store=SocialStateStore(tmp_path / "social-init.json"))
    server.admin_booking_delivery = cms.service.delivery
    session = Session(("127.0.0.1", 49000), Mock())
    session.gameplay_character = next(c for c in server.character_selection.characters if c.character_id == 42)
    session.gameplay_map = "a0200_kingscity"
    row = booking(cms)
    resolved = server._resolve_initialization_character(session)
    server._resolve_initialization_inventory(resolved)
    server._encode_initialization_new_player(resolved)
    server._resolve_initialization_talents(resolved)
    server._resolve_initialization_persistent_effects(session, resolved)
    server._resolve_initialization_vitals(resolved)
    server._apply_initialization_state(session, resolved)
    assert session.gameplay_currency_state.andermant == 125
    assert session.gameplay_admin_booking_ids == (row["id"],)
    captured = server._capture_account_state(session)
    assert captured.admin_booking_ids == (row["id"],)
    assert captured.currency.andermant == 125
    cms.store.put(captured, flush=True)
    cms.service.delivery.deliver(42)
    assert cms.store.get(42).currency.andermant == 125


def equipment_booking(cms, options=None, request_id="equipment-request-0001"):
    return cms.service.post("/api/bookings", {
        "account_id": cms.player.account_id, "character_id": 42, "kind": "item",
        "resource": "mage_base_rh_staff_speedAttack_damage", "amount": 1,
        "options": options or {}, "reason": "Equipment support ticket", "request_id": request_id,
    }, cms.admin, "")


def test_account_details_and_lists_scope_characters_sessions_and_bookings(cms):
    cookie, _ = login(cms)
    cms.registry.login("Player", "secret123")
    cms.registry.login("Operator", "secret123")
    row = booking(cms)
    account_id = cms.player.account_id
    status, data, _ = request(cms, "GET", f"/api/accounts/{account_id}", cookie=cookie)
    assert status == 200
    assert [c["character_id"] for c in data["characters"]] == [42]
    assert data["characters"][0]["has_state"]
    for path in [f"characters?account_id={account_id}", f"sessions?account_id={account_id}",
                 f"bookings?account_id={account_id}", "bookings?character_id=42"]:
        code, data, _ = request(cms, "GET", "/api/" + path, cookie=cookie)
        assert code == 200 and data["total"] == 1
    assert request(cms, "GET", f"/api/bookings?account_id={cms.admin.account_id}", cookie=cookie)[1]["total"] == 0
    detail = request(cms, "GET", f"/api/bookings/{row['id']}", cookie=cookie)[1]
    assert detail["account"]["account_id"] == account_id
    assert detail["character"]["character_id"] == 42
    assert request(cms, "GET", "/api/characters/42", cookie=cookie)[1]["account"]["account_id"] == account_id
    for path in [f"/api/accounts/{account_id}", "/api/items/mage_base_helmet_crafted", f"/api/bookings/{row['id']}", "/api/audit/1"]:
        assert request(cms, "GET", path)[0] == 401


def test_item_metadata_distinguishes_equipment_from_slotted_consumables(cms):
    cookie, _ = login(cms)
    equipment = request(cms, "GET", "/api/items/mage_base_helmet_crafted", cookie=cookie)[1]
    consumable = request(cms, "GET", "/api/items/potion_combined_health_rarity_05", cookie=cookie)[1]
    assert equipment["equipment"] and equipment["socket_limits"][100] == 8
    assert not consumable["equipment"]
    assert consumable["slot_types"]  # Having a slot alone must not expose equipment controls.
    for asset in ["admin_ui.js", "admin_pages.js", "admin_character.js", "admin_booking.js"]:
        assert request(cms, "GET", "/" + asset)[0] == 200


def test_equipment_options_are_delivered_saved_and_audited(cms):
    options = {"item_level": 100, "rarity": 4, "socket_count": 8, "enchantment_tier": 3, "roll_quality": 95}
    row = equipment_booking(cms, options)
    assert row["options"] == options
    assert equipment_booking(cms, options)["id"] == row["id"]
    with pytest.raises(ValueError, match="different booking"):
        equipment_booking(cms, {**options, "roll_quality": 96})
    cms.service.delivery.deliver(42)
    item = cms.store.get(42).inventory.items[0]
    assert (item.item_level, item.field_08, item.socket_count) == (100, 4, 8)
    assert item.socketed_templates == [""] * 8
    assert len(item.enchantments) == 4
    assert all(e.value == .95 and e.field_08 == 3 for e in item.enchantments)
    reloaded = AccountStateStore(cms.store.path)
    try:
        saved = reloaded.get(42).inventory.items[0]
        assert saved.socket_count == 8 and saved.enchantments == item.enchantments
    finally:
        reloaded.close()
    audit = cms.service.repository.history("admin_audit")["rows"]
    create = next(a for a in audit if a["action"] == "booking.create")
    assert create["details"]["options"] == options
    cms.service.delivery.deliver(42)
    assert len(cms.store.get(42).inventory.items) == 1


@pytest.mark.parametrize("options", [
    {"item_level": 0}, {"item_level": 201}, {"item_level": True}, {"rarity": 7},
    {"item_level": 1, "socket_count": 8}, {"socket_count": -1}, {"enchantment_tier": 999},
    {"roll_quality": 101}, {"roll_quality": 1.5}, {"roll_quality": False}, {"unknown": 1},
])
def test_invalid_equipment_configuration_is_rejected(cms, options):
    with pytest.raises(ValueError):
        equipment_booking(cms, options)
    assert not cms.service.repository.pending(42)


def test_non_equipment_and_currency_reject_equipment_options(cms):
    for kind, resource in [("item", "potion_combined_health_rarity_05"), ("currency", "gold")]:
        with pytest.raises(ValueError):
            cms.service.post("/api/bookings", {"character_id": 42, "kind": kind, "resource": resource,
                "amount": 1, "options": {"item_level": 100}, "reason": "Bad request",
                "request_id": "invalid-options-request"}, cms.admin, "")


def test_booking_rejects_stale_or_forged_account_character_pair(cms):
    with pytest.raises(ValueError, match="does not belong"):
        cms.service.post("/api/bookings", {"account_id": cms.admin.account_id,
            "character_id": 42, "kind": "currency", "resource": "gold", "amount": 1,
            "reason": "Wrong account", "request_id": "wrong-account-request"}, cms.admin, "")


def test_old_booking_schema_is_migrated_without_losing_pending_rows(tmp_path):
    from dso_server.persistence.admin_registry import AdminRepository
    registry = AccountRegistry(tmp_path / "migration.sqlite3", password_iterations=10000)
    try:
        registry._connection.execute("""CREATE TABLE admin_bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,request_id TEXT NOT NULL UNIQUE,
            character_id INTEGER NOT NULL,account_id INTEGER NOT NULL,actor TEXT NOT NULL,
            kind TEXT NOT NULL,resource TEXT NOT NULL,amount INTEGER NOT NULL,reason TEXT NOT NULL,
            created_at REAL NOT NULL,status TEXT NOT NULL DEFAULT 'pending',delivered_at REAL,
            error TEXT NOT NULL DEFAULT '')""")
        registry._connection.execute("""INSERT INTO admin_bookings
            (request_id,character_id,account_id,actor,kind,resource,amount,reason,created_at)
            VALUES('old-request',42,777778,'Operator','currency','gold',5,'old booking',1)""")
        repository = AdminRepository(registry)
        assert repository.pending(42)[0]["options"] == {}
        assert repository.pending(42)[0]["amount"] == 5
        AdminRepository(registry)  # Reopening is idempotent.
    finally:
        registry.close()


def test_incompatible_equipment_rolls_are_rejected_before_booking(cms):
    with pytest.raises(ValueError, match="no random enchantments"):
        cms.service.post("/api/bookings", {"character_id": 42, "kind": "item",
            "resource": "mage_base_helmet_crafted", "amount": 1,
            "options": {"item_level": 100, "rarity": 4}, "reason": "Invalid combination",
            "request_id": "invalid-equipment-request"}, cms.admin, "")
    assert not cms.service.repository.pending(42)


def test_legacy_pending_equipment_keeps_original_multi_item_delivery(cms):
    cms.service.repository.create_booking("Operator", cms.player.account_id, 42, "item",
        "mage_base_rh_staff_speedAttack_damage", 2, "Legacy booking", "legacy-equipment-request")
    cms.service.delivery.deliver(42)
    items = cms.store.get(42).inventory.items
    assert len(items) == 2 and all(item.amount == 1 for item in items)
    assert all(not item.enchantments for item in items)
    assert not cms.service.repository.pending(42)


def test_multiple_pending_bookings_credit_shared_wallet_once_each(cms):
    cms.registry.claim_character("Player",44)
    cms.store.put(state(44))
    cms.store.account_registry=cms.registry
    booking(cms,request_id="shared-credit-first")
    booking(cms,request_id="shared-credit-second")
    cms.service.delivery.deliver(42)
    assert cms.store.get(42).currency.andermant == 150
    assert cms.store.get(44).currency.andermant == 150
    cms.service.delivery.deliver(42)
    assert cms.store.get(44).currency.andermant == 150
