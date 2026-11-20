"""Tests for Black Friday events: black_friday_event, black_friday_early_event,
and the legacy package sales event event_sell_blackfriday_package_silent.

The legacy Black Friday packages are no longer advertised by the current
rotation; they stay gated behind the separate silent sales event instead of
following the parent events.
"""

import json
from datetime import datetime, timezone
import pytest

from dso_server.gameplay.event_system import (
    load_event_catalog,
    SCHEDULED_EVENT_FLAG_ALIASES,
    scheduled_event_flag_names,
)
from dso_server.gameplay.ocp_store import (
    BLACK_FRIDAY_REALM_PATH_PACKAGE_PRODUCT_KEY,
    BLACK_FRIDAY_ANDERMANT_PACKAGE_PRODUCT_KEY,
    BLACK_FRIDAY_JEWEL_PACKAGE_1_PRODUCT_KEY,
    BLACK_FRIDAY_DELUXE_PACKAGE_1_PRODUCT_KEY,
    BLACK_FRIDAY_GEM_PACKAGE_PRODUCT_KEY,
    BLACK_FRIDAY_JEWEL_PACKAGE_2_PRODUCT_KEY,
    BLACK_FRIDAY_RUNE_PACKAGE_PRODUCT_KEY,
    BLACK_FRIDAY_DELUXE_PACKAGE_2_PRODUCT_KEY,
    ocp_product_event_is_active,
)
from dso_server.protocol.reward_protocol import (
    LOCAL_OCP_OFFERS,
    resolve_ocp_booking_reward,
)
from dso_server.protocol.chest_loot_protocol import roll_chest_contents
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.services.admin_events import EventAdministration, LINKED_SILENT_EVENTS
from dso_server.services import events as game_events
from dso_server.tests.test_admin_cms import cms


def test_black_friday_legacy_packages_gated_off_parent_events(tmp_path):
    schedule_path = tmp_path / "events.json"
    schedule_path.write_text(json.dumps({"events": [
        {
            "event_id": 1,
            "template_id": "black_friday_event",
            "state": "active",
            "starts_at": "2026-11-19T00:00:00+00:00",
            "ends_at": "2026-11-26T00:00:00+00:00",
        }
    ]}), encoding="utf-8")

    catalog = load_event_catalog(DEFAULT_STATIC_DB_PATH, schedule_path)
    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
    active_flags = catalog.active_event_flags(now)

    assert "black_friday_event" in active_flags
    assert "event_sell_blackfriday_package_silent" not in active_flags
    assert not ocp_product_event_is_active(
        BLACK_FRIDAY_REALM_PATH_PACKAGE_PRODUCT_KEY, active_flags
    )

    update = catalog.event_update(now)
    bf_entry = next(
        e for e in update.events if e.event_name == "black_friday_event"
    )
    assert "event_sell_blackfriday_package_silent" not in bf_entry.tags


def test_black_friday_early_legacy_packages_gated_off(tmp_path):
    schedule_path = tmp_path / "events.json"
    schedule_path.write_text(json.dumps({"events": [
        {
            "event_id": 2,
            "template_id": "black_friday_early_event",
            "state": "active",
            "starts_at": "2026-11-19T00:00:00+00:00",
            "ends_at": "2026-11-26T00:00:00+00:00",
        }
    ]}), encoding="utf-8")

    catalog = load_event_catalog(DEFAULT_STATIC_DB_PATH, schedule_path)
    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
    active_flags = catalog.active_event_flags(now)

    assert "black_friday_early_event" in active_flags
    assert "event_sell_blackfriday_package_silent" not in active_flags
    assert not ocp_product_event_is_active(
        BLACK_FRIDAY_ANDERMANT_PACKAGE_PRODUCT_KEY, active_flags
    )


def test_black_friday_ocp_packages_active():
    active_events = ["event_sell_blackfriday_package_silent"]
    packages = [
        BLACK_FRIDAY_REALM_PATH_PACKAGE_PRODUCT_KEY,
        BLACK_FRIDAY_ANDERMANT_PACKAGE_PRODUCT_KEY,
        BLACK_FRIDAY_JEWEL_PACKAGE_1_PRODUCT_KEY,
        BLACK_FRIDAY_DELUXE_PACKAGE_1_PRODUCT_KEY,
        BLACK_FRIDAY_GEM_PACKAGE_PRODUCT_KEY,
        BLACK_FRIDAY_JEWEL_PACKAGE_2_PRODUCT_KEY,
        BLACK_FRIDAY_RUNE_PACKAGE_PRODUCT_KEY,
        BLACK_FRIDAY_DELUXE_PACKAGE_2_PRODUCT_KEY,
    ]
    for pkg in packages:
        assert ocp_product_event_is_active(pkg, active_events)
        reward = resolve_ocp_booking_reward(pkg, "external")
        assert reward.amount >= 1
        assert reward.item_id.startswith("black_friday_")


def test_black_friday_chest_packages_contents():
    packages = [
        "black_friday_realm_path_package",
        "black_friday_andermant_package",
        "black_friday_jewel_package_1",
        "black_friday_deluxe_package_1",
        "black_friday_gem_package",
        "black_friday_jewel_package_2",
        "black_friday_rune_package",
        "black_friday_deluxe_package_2",
    ]
    for pkg in packages:
        res = roll_chest_contents(DEFAULT_STATIC_DB_PATH, pkg, player_level=100)
        assert len(res.rewards) > 0


def test_black_friday_linked_silent_admin_flow(cms, tmp_path, monkeypatch):
    schedule = tmp_path / "events.json"
    schedule.write_text(json.dumps({"events": [
        {
            "event_id": 100,
            "template_id": "black_friday_event",
            "state": "expired",
            "starts_at": "2026-11-16T00:00:00+00:00",
            "ends_at": "2026-11-23T00:00:00+00:00",
        },
        {
            "event_id": 101,
            "template_id": "event_sell_blackfriday_package_silent",
            "state": "expired",
            "starts_at": "2026-11-16T00:00:00+00:00",
            "ends_at": "2026-11-23T00:00:00+00:00",
        },
    ]}), encoding="utf-8")
    monkeypatch.setattr(game_events, "EVENT_SCHEDULE_PATH", schedule)
    cms.service.events = EventAdministration(cms.service, schedule)

    # The legacy packages no longer follow the parent Black Friday events:
    # activating black_friday_event must leave the silent sales event alone.
    rev = cms.service.events.listing()["revision"]
    cms.service.events.change("admin", "Starting Black Friday", {"revision": rev}, event_id=100, enabled=True)

    rows = {r["template_id"]: r for r in cms.service.events.listing()["rows"]}
    assert rows["black_friday_event"]["state"] == "active"
    assert rows["event_sell_blackfriday_package_silent"]["state"] == "expired"

    # Stopping the parent event also leaves the silent event untouched.
    rev = cms.service.events.listing()["revision"]
    cms.service.events.change("admin", "Stopping Black Friday", {"revision": rev}, event_id=100, enabled=False)

    rows_after = {r["template_id"]: r for r in cms.service.events.listing()["rows"]}
    assert rows_after["black_friday_event"]["state"] == "expired"
    assert rows_after["event_sell_blackfriday_package_silent"]["state"] == "expired"


def test_black_friday_transactions_info_codec():
    from dso_server.protocol.reward_protocol import (
        GET_BLACK_FRIDAY_TRANSACTIONS_INFO_COMMAND,
        GET_BLACK_FRIDAY_TRANSACTIONS_INFO_RESULT_COMMAND,
        encode_get_black_friday_transactions_info_result,
        decode_get_black_friday_transactions_info_result,
    )

    assert GET_BLACK_FRIDAY_TRANSACTIONS_INFO_COMMAND == 375
    assert GET_BLACK_FRIDAY_TRANSACTIONS_INFO_RESULT_COMMAND == 376

    txs = ("101_blackfriday_package_1", "101_blackfriday_package_2")
    payload, bits = encode_get_black_friday_transactions_info_result(txs, actor_id=1001)
    decoded, actor_id = decode_get_black_friday_transactions_info_result(payload, bits)
    assert decoded == txs
    assert actor_id == 1001


def test_bullet_skill_retail_decode():
    from dso_server.protocol.character_selection import BitWriter
    from dso_server.protocol.combat_protocol import (
        decode_bullet_skill,
        BULLET_SKILL_COMMAND,
    )

    w = BitWriter()
    w.write_uint8(0x8B)
    w.write_bytes(BULLET_SKILL_COMMAND.to_bytes(2, "little"))
    w.write_bytes((1).to_bytes(2, "little"))
    w.write_bytes((0).to_bytes(2, "little"))
    w.write_float(0.5)
    w.write_uint32_le(1234)
    w.write_uint32_le(5678)
    w.write_bool(False)
    for v in [1.0, 0.0, 0.0, 0.0]:
        w.write_float(v)
    w.write_uint8(0)
    w.write_string("")  # Retail client encodes string after variant
    payload, bits = w.finish()

    skill = decode_bullet_skill(payload, bits)
    assert skill.skill_level == 1
    assert skill.skill_index == 0
    assert skill.aim_angle == pytest.approx(0.5)
    assert skill.server_tick == 1234
    assert skill.cast_id == 5678
    assert skill.direction == pytest.approx((1.0, 0.0, 0.0, 0.0))
    assert skill.variant == 0


def test_early_bird_offers_and_pascal_case_event_update(tmp_path):
    from dso_server.protocol.reward_protocol import (
        EARLY_BIRD_CHEST_1_PRODUCT_KEY,
        EARLY_BIRD_CHEST_2_PRODUCT_KEY,
        EARLY_BIRD_CHEST_3_PRODUCT_KEY,
        EARLY_BIRD_CHEST_4_PRODUCT_KEY,
        EARLY_BIRD_CHEST_5_PRODUCT_KEY,
    )

    schedule_path = tmp_path / "events.json"
    schedule_path.write_text(json.dumps({"events": [
        {
            "event_id": 10,
            "template_id": "black_friday_early_event",
            "state": "active",
            "starts_at": "2026-11-19T00:00:00+00:00",
            "ends_at": "2026-11-26T00:00:00+00:00",
        },
        {
            "event_id": 11,
            "template_id": "black_friday_event",
            "state": "active",
            "starts_at": "2026-11-19T00:00:00+00:00",
            "ends_at": "2026-11-26T00:00:00+00:00",
        }
    ]}), encoding="utf-8")

    catalog = load_event_catalog(DEFAULT_STATIC_DB_PATH, schedule_path)
    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
    active_flags = catalog.active_event_flags(now)
    all_active = set(catalog.active_template_ids(now)) | set(active_flags)

    # EventUpdate uses valid database template resource_id (Id)
    update = catalog.event_update(now)
    event_names = {e.event_name for e in update.events}
    assert "black_friday_early_event" in event_names
    assert "black_friday_event" in event_names

    # Client StringAtom checks: template Name in database is PascalCase
    events_by_id = {e.template.resource_id: e for e in catalog.events}
    assert events_by_id["black_friday_early_event"].template.client_name == "BlackFridayEarlyEvent"
    assert events_by_id["black_friday_event"].template.client_name == "BlackFridayEvent"

    # Check Early Bird offers registered and active
    eb_keys = [
        EARLY_BIRD_CHEST_1_PRODUCT_KEY,
        EARLY_BIRD_CHEST_2_PRODUCT_KEY,
        EARLY_BIRD_CHEST_3_PRODUCT_KEY,
        EARLY_BIRD_CHEST_4_PRODUCT_KEY,
        EARLY_BIRD_CHEST_5_PRODUCT_KEY,
    ]
    offers_by_key = {o.product_key: o for o in LOCAL_OCP_OFFERS}
    for k in eb_keys:
        assert k in offers_by_key
        assert offers_by_key[k].price > 0
        assert ocp_product_event_is_active(k, all_active)
        reward = resolve_ocp_booking_reward(k, "external", class_id=1)
        assert reward.bundle_items

    # Check Queen Chests (Happy Time) registered and active
    from dso_server.gameplay.ocp_store import (
        QUEEN_CHEST_1_PRODUCT_KEY,
        QUEEN_CHEST_14_PRODUCT_KEY,
    )
    for idx in range(1, 15):
        q_key = f"101_queen_chest_{idx}_1.0000_0_NONE"
        assert q_key in offers_by_key
        assert offers_by_key[q_key].price > 0
        assert ocp_product_event_is_active(q_key, all_active)
        reward = resolve_ocp_booking_reward(q_key, "external", class_id=1)
        assert reward.bundle_items or reward.item_id


def test_offer_id_normalization():
    from dso_server.protocol.reward_protocol import normalize_ocp_offer_id
    assert normalize_ocp_offer_id("101_blackfriday_package_1") == "101_blackfriday_package_1_1.0000_0_NONE"
    assert normalize_ocp_offer_id("blackfriday_package_1") == "101_blackfriday_package_1_1.0000_0_NONE"
    assert normalize_ocp_offer_id("101_early_bird_chest_2") == "101_early_bird_chest_2_1.0000_0_NONE"
    assert normalize_ocp_offer_id("early_bird_chest_2") == "101_early_bird_chest_2_1.0000_0_NONE"
    assert normalize_ocp_offer_id("101_queen_chest_3") == "101_queen_chest_3_1.0000_0_NONE"
    assert normalize_ocp_offer_id("queen_chest_3") == "101_queen_chest_3_1.0000_0_NONE"


def test_black_friday_ocp_booking_immediate_settlement():
    from unittest.mock import MagicMock
    from types import SimpleNamespace
    from dso_server.services.commerce import CommerceService
    from dso_server.protocol.reward_protocol import encode_ocp_booking_request
    from dso_server.protocol.inventory_codec import InventoryInfo
    from dso_server.protocol.player_init_protocol import CurrencyChanged
    from dso_server.services.base import CharacterService

    service = CommerceService()
    service.static_database_path = DEFAULT_STATIC_DB_PATH

    session = MagicMock()
    session.account_id = 1
    session.gameplay_currency_state = CurrencyChanged(
        currency_type=1000,
        amount=500,
        values=(0, 0, 0),
        delta=0,
        notify=False,
    )
    session.gameplay_next_item_id = 100
    mock_char = SimpleNamespace(character_id=123, level=100, class_id=1)
    session.gameplay_character = mock_char
    session.gameplay_timed_offer_purchases = {}
    session.gameplay_inventory = InventoryInfo(items=[], storage_size=50, storage_slots={})
    session.gameplay_remote_actor_id = 1001

    catalog_mock = MagicMock()
    catalog_mock.active_template_ids.return_value = ["black_friday_event"]
    catalog_mock.active_event_flags.return_value = ["event_sell_blackfriday_package_silent"]
    CharacterService._event_catalog = MagicMock(return_value=catalog_mock)
    CharacterService._implementation = MagicMock()

    payload = encode_ocp_booking_request("101_blackfriday_package_1", "in_game")
    service._handle_ocp_booking(session, payload)

    # Verify item was delivered immediately into inventory!
    delivered = [item.template_id for item in session.gameplay_inventory.items]
    assert "black_friday_realm_path_package" in delivered

    # Verify transactions broadcast was sent
    assert session.send_reliable_ordered_split.called


def test_ocp_booking_rejects_package_limit():
    from unittest.mock import MagicMock
    from types import SimpleNamespace
    from dso_server.services.commerce import CommerceService
    from dso_server.protocol.reward_protocol import encode_ocp_booking_request
    from dso_server.protocol.inventory_codec import InventoryInfo
    from dso_server.protocol.player_init_protocol import CurrencyChanged
    from dso_server.services.base import CharacterService

    service = CommerceService()
    service.static_database_path = DEFAULT_STATIC_DB_PATH
    assert service._payment_package_limit(
        "101_early_bird_chest_1_1.0000_0_NONE"
    ) == 1
    assert service._payment_package_limit(
        "101_queen_chest_12_1.0000_0_NONE"
    ) == 10
    assert service._payment_package_limit(
        "101_blackfriday_package_1_1.0000_0_NONE"
    ) == 0

    session = MagicMock()
    session.account_id = 1
    session.gameplay_currency_state = CurrencyChanged(
        currency_type=1000, amount=500, values=(0, 0, 0),
        delta=0, notify=False,
    )
    session.gameplay_next_item_id = 100
    session.gameplay_character = SimpleNamespace(
        character_id=123, level=100, class_id=1
    )
    session.gameplay_timed_offer_purchases = {
        "101_early_bird_chest_1_1.0000_0_NONE": 1,
    }
    session.gameplay_inventory = InventoryInfo(
        items=[], storage_size=50, storage_slots={}
    )
    session.gameplay_remote_actor_id = 1001

    catalog_mock = MagicMock()
    catalog_mock.active_template_ids.return_value = ["black_friday_early_event"]
    catalog_mock.active_event_flags.return_value = []
    CharacterService._event_catalog = MagicMock(return_value=catalog_mock)
    CharacterService._implementation = MagicMock()

    payload = encode_ocp_booking_request("101_early_bird_chest_1", "in_game")
    service._handle_ocp_booking(session, payload)

    # The limit is enforced before any claim or delivery is written.
    assert session.gameplay_inventory.items == []
    assert session.gameplay_timed_offer_purchases == {
        "101_early_bird_chest_1_1.0000_0_NONE": 1,
    }
    assert session.send_reliable_ordered_split.called


def test_trigger_in_game_purchase_delivery():
    from unittest.mock import MagicMock
    from types import SimpleNamespace
    from dso_server.services.commerce import CommerceService
    from dso_server.protocol.inventory_codec import InventoryInfo
    from dso_server.protocol.player_init_protocol import CurrencyChanged
    from dso_server.services.base import CharacterService

    service = CommerceService()
    service.static_database_path = DEFAULT_STATIC_DB_PATH

    session = MagicMock()
    session.account_id = 1
    session.gameplay_currency_state = CurrencyChanged(
        currency_type=1000,
        amount=500,
        values=(0, 0, 0),
        delta=0,
        notify=False,
    )
    session.gameplay_next_item_id = 100
    session.gameplay_character = SimpleNamespace(character_id=123, level=100, class_id=1)
    session.gameplay_timed_offer_purchases = {}
    session.gameplay_inventory = InventoryInfo(items=[], storage_size=50, storage_slots={})
    session.gameplay_remote_actor_id = 1001
    CharacterService._implementation = MagicMock()

    service._trigger_in_game_purchase(session, "101_early_bird_chest_1")

    # Early Bird Chest 1 delivers mount, Thanksgiving white cloak, purple cloak, difficulty keys
    delivered = [item.template_id for item in session.gameplay_inventory.items]
    assert "mount_common_horse_01" in delivered
    assert "thanksgiving_cloak_white_2023_mage" in delivered
    assert "thanksgiving_cloak_pruple_2023_mage" in delivered
    assert "difficulty_key_common" in delivered
    assert session.send_reliable_ordered_split.called


def test_presale_discount_notification_action():
    from unittest.mock import MagicMock
    from types import SimpleNamespace
    import uuid
    from dso_server.services.commerce import CommerceService
    from dso_server.protocol.reward_protocol import (
        NotificationAction,
        encode_notification_action,
    )
    from dso_server.protocol.inventory_codec import InventoryInfo
    from dso_server.protocol.player_init_protocol import CurrencyChanged
    from dso_server.services.base import CharacterService

    service = CommerceService()
    service.static_database_path = DEFAULT_STATIC_DB_PATH

    session = MagicMock()
    session.account_id = 1
    session.gameplay_currency_state = CurrencyChanged(
        currency_type=1000,
        amount=500,
        values=(0, 0, 0),
        delta=0,
        notify=False,
    )
    session.gameplay_next_item_id = 100
    session.gameplay_character = SimpleNamespace(character_id=123, level=100, class_id=1)
    session.gameplay_timed_offer_purchases = {}
    session.gameplay_inventory = InventoryInfo(items=[], storage_size=50, storage_slots={})
    session.gameplay_remote_actor_id = 1001
    CharacterService._implementation = MagicMock()

    action = NotificationAction(
        notification_type=2,
        subtype=3,
        kind=33,
        instance_guid=uuid.uuid4().bytes,
        values=(0, 1001, 1001),
        payload={"productKey": "101_queen_chest_1"},
        timestamp="2026-11-19 22:00:00",
    )
    payload = encode_notification_action(action)
    service._handle_notification_action(session, payload)

    # Queen chest 1 delivers surprise_lottery_bag
    delivered = [item.template_id for item in session.gameplay_inventory.items]
    assert "surprise_lottery_bag" in delivered
    assert session.send_reliable_ordered_split.called


def test_presale_discount_empty_payload_does_not_purchase():
    import uuid
    from unittest.mock import MagicMock
    from types import SimpleNamespace
    from dso_server.protocol.reward_protocol import (
        NotificationAction,
        encode_notification_action,
    )
    from dso_server.protocol.inventory_codec import InventoryInfo
    from dso_server.protocol.player_init_protocol import CurrencyChanged
    from dso_server.services.commerce import CommerceService
    from dso_server.services.base import CharacterService

    service = CommerceService()
    service.static_database_path = DEFAULT_STATIC_DB_PATH

    session = MagicMock()
    session.account_id = 1
    session.gameplay_currency_state = CurrencyChanged(
        currency_type=1000,
        amount=500,
        values=(0, 0, 0),
        delta=0,
        notify=False,
    )
    session.gameplay_next_item_id = 100
    session.gameplay_character = SimpleNamespace(character_id=123, level=100, class_id=1)
    session.gameplay_timed_offer_purchases = {}
    session.gameplay_inventory = InventoryInfo(items=[], storage_size=50, storage_slots={})
    session.gameplay_remote_actor_id = 1001
    CharacterService._implementation = MagicMock()

    # Opening the treasury or clicking an offer tile names no product, so it
    # must never deliver a package.
    action = NotificationAction(
        notification_type=2,
        subtype=3,
        kind=33,
        instance_guid=uuid.uuid4().bytes,
        values=(0, 0, 0),
        payload={},
        timestamp="2026-11-19 22:00:00",
    )
    payload = encode_notification_action(action)
    service._handle_notification_action(session, payload)

    assert session.gameplay_inventory.items == []
    assert session.gameplay_timed_offer_purchases == {}
    assert not session.send_reliable_ordered_split.called


def test_accounts_in_game_purchase_endpoint():
    from unittest.mock import MagicMock
    from types import SimpleNamespace
    from dso_server.services.accounts import AccountRequestHandler
    from dso_server.services.commerce import CommerceService
    from dso_server.protocol.inventory_codec import InventoryInfo
    from dso_server.protocol.player_init_protocol import CurrencyChanged
    from dso_server.services.base import CharacterService

    # Set up mock gameplay server with a session
    mock_gameplay = CommerceService()
    mock_gameplay.static_database_path = DEFAULT_STATIC_DB_PATH
    CharacterService._implementation = MagicMock()

    session = MagicMock()
    session.account_id = 777
    session.login_session_id = "test-session-guid"
    session.gameplay_character = SimpleNamespace(character_id=123, level=100, class_id=1)
    session.gameplay_inventory = InventoryInfo(items=[], storage_size=50, storage_slots={})
    session.gameplay_currency_state = CurrencyChanged(
        currency_type=1000,
        amount=500,
        values=(0, 0, 0),
        delta=0,
        notify=False,
    )
    session.gameplay_next_item_id = 1
    session.gameplay_timed_offer_purchases = {}
    session.gameplay_remote_actor_id = 1001

    mock_gameplay.sessions = {("127.0.0.1", 1234): session}

    # Set up handler
    handler = MagicMock()
    handler.service = SimpleNamespace(gameplay_server=mock_gameplay)
    handler._fail = MagicMock()
    handler._respond = MagicMock()

    # Call _in_game_purchase
    AccountRequestHandler._in_game_purchase(handler, {
        "account_id": 777,
        "session_id": "test-session-guid",
        "product_key": "101_blackfriday_package_1",
    })

    assert handler._respond.called
    assert not handler._fail.called
    # Delivered items into session inventory
    assert len(session.gameplay_inventory.items) > 0


def test_chat_buy_commands():
    from unittest.mock import MagicMock
    from types import SimpleNamespace
    from dso_server.services.chat import ChatServer

    chat = MagicMock(spec=ChatServer)
    chat._send_system_message = MagicMock()

    mock_gameplay_server = MagicMock()
    mock_gameplay_session = SimpleNamespace(gameplay_map="test_map")

    chat._gameplay_server_and_session_for_user_id.return_value = (
        mock_gameplay_server,
        mock_gameplay_session,
    )

    s = SimpleNamespace(chat_is_admin=True, chat_user_id=123)

    # Test !buy
    ChatServer._handle_admin_command(chat, s, "!buy 101_blackfriday_package_1")
    mock_gameplay_server._trigger_in_game_purchase.assert_called_with(
        mock_gameplay_session, "101_blackfriday_package_1"
    )

    # Test !bf 2
    ChatServer._handle_admin_command(chat, s, "!bf 2")
    mock_gameplay_server._trigger_in_game_purchase.assert_called_with(
        mock_gameplay_session, "101_blackfriday_package_2"
    )

    # Test !chest 5
    ChatServer._handle_admin_command(chat, s, "!chest 5")
    mock_gameplay_server._trigger_in_game_purchase.assert_called_with(
        mock_gameplay_session, "101_queen_chest_5"
    )






