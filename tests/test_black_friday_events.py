"""Tests for Black Friday events: black_friday_event, black_friday_early_event,
and their linked silent event event_sell_blackfriday_package_silent.
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


def test_black_friday_aliases_and_flags(tmp_path):
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
    assert "event_sell_blackfriday_package_silent" in active_flags

    # EventUpdate should contain black_friday_event with event_sell_blackfriday_package_silent in tags
    update = catalog.event_update(now)
    event_names = [e.event_name for e in update.events]
    assert "black_friday_event" in event_names
    bf_entry = next(e for e in update.events if e.event_name == "black_friday_event")
    assert "event_sell_blackfriday_package_silent" in bf_entry.tags


def test_black_friday_early_aliases_and_flags(tmp_path):
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
    assert "event_sell_blackfriday_package_silent" in active_flags


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

    # Activating black_friday_event must also activate event_sell_blackfriday_package_silent
    rev = cms.service.events.listing()["revision"]
    cms.service.events.change("admin", "Starting Black Friday", {"revision": rev}, event_id=100, enabled=True)

    rows = {r["template_id"]: r for r in cms.service.events.listing()["rows"]}
    assert rows["black_friday_event"]["state"] == "active"
    assert rows["event_sell_blackfriday_package_silent"]["state"] == "active"

    # Deactivating black_friday_event must also deactivate the silent event
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



