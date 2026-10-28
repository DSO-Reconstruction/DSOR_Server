"""CMS event lifecycle, durable schedule edits and native online updates."""

import copy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

import pytest

from dso_server.tests.test_admin_cms import cms, login, request
from dso_server.tests.test_admin_live import online
from dso_server.services import events as game_events
from dso_server.services.admin_events import EventAdministration
from dso_server.gameplay.event_system import load_event_catalog
from dso_server.gameplay.status_modifier_runtime import active_xp_gain_multiplier
from dso_server.protocol.player_init_protocol import EVENT_UPDATE_COMMAND, decode_event_update


@pytest.fixture
def event_cms(cms, tmp_path, monkeypatch):
    schedule = tmp_path / "events.json"
    schedule.write_text(json.dumps({"note": "preserve operator configuration", "events": [
        {"event_id": 41, "template_id": "newmoon_event_02", "state": "active",
         "tags": ["newmoon"], "collectible_maps": ["e_newmoon_blakborg_city_dun"],
         "collectible_drop_chance": 0.75},
        {"event_id": 42, "template_id": "desert_of_essences_02", "state": "expired"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(game_events, "EVENT_SCHEDULE_PATH", schedule)
    cms.service.events = EventAdministration(cms.service, schedule)
    return cms


def create(cms, template="xp_bonus_event_01", **changes):
    payload = {"template_id": template, "revision": cms.service.events.listing()["revision"],
               "reason": "Event operations ticket", **changes}
    return cms.service.post("/api/events", payload, cms.admin, "")


def change(cms, event_id, action, **options):
    return cms.service.post(f"/api/events/{event_id}/{action}", {
        "revision": cms.service.events.listing()["revision"], "reason": "Event operations ticket", **options
    }, cms.admin, "")


def test_event_apis_require_admin_and_csrf(event_cms):
    for path in ("events", "events/41", "event-templates"):
        assert request(event_cms, "GET", "/api/" + path)[0] == 401
    cookie, csrf = login(event_cms)
    payload = {"template_id": "xp_bonus_event_01", "reason": "Enable XP bonus",
               "revision": event_cms.service.events.listing()["revision"]}
    assert request(event_cms, "POST", "/api/events", payload, cookie)[0] == 403
    payload["reason"] = ""
    assert request(event_cms, "POST", "/api/events", payload, cookie, csrf)[0] == 400


def test_event_list_search_filter_catalog_and_detail(event_cms):
    cookie, _ = login(event_cms)
    result = request(event_cms, "GET", "/api/events?state=active&q=newmoon", cookie=cookie)[1]
    assert result["total"] == 1 and result["rows"][0]["event_id"] == 41
    assert result["counts"] == {"active": 1, "upcoming": 0, "expired": 1}
    templates = request(event_cms, "GET", "/api/event-templates?q=xp_bonus_event_01", cookie=cookie)[1]
    assert any(row["template_id"] == "xp_bonus_event_01" for row in templates["rows"])
    detail = request(event_cms, "GET", "/api/events/41", cookie=cookie)[1]
    assert detail["config"]["tags"] == ["newmoon"] and detail["manageable"]
    assert request(event_cms, "GET", "/api/events?state=invalid", cookie=cookie)[0] == 400


def test_add_remove_reactivate_durable_and_preserve_configuration(event_cms):
    cms = event_cms
    result = create(cms, collectible_drop_chance=0.5, collectible_amount=2, participation_default=False)
    new_id = result["event_id"]
    assert new_id > 42
    assert cms.service.events.detail(new_id)["state"] == "active"
    change(cms, 41, "deactivate")
    detail = cms.service.events.detail(41)
    assert detail["state"] == "expired" and detail["config"]["collectible_drop_chance"] == 0.75
    assert detail["config"]["collectible_maps"] == ["e_newmoon_blakborg_city_dun"]
    change(cms, 41, "activate")
    assert cms.service.events.detail(41)["state"] == "active"
    assert cms.service.events.detail(41)["ends_at"] is None
    reloaded = EventAdministration(cms.service, cms.service.events.schedule_path)
    assert reloaded.detail(new_id)["config"]["participation_default"] is False
    assert len(reloaded.listing()["rows"]) == 3
    assert json.loads(cms.service.events.schedule_path.read_text())["note"] == "preserve operator configuration"
    actions = {row["action"] for row in cms.service.repository.history("admin_audit")["rows"]}
    assert {"event.add", "event.activate", "event.deactivate"} <= actions


def test_fresh_run_does_not_reuse_instance_id(event_cms):
    first = create(event_cms)
    change(event_cms, first["event_id"], "deactivate")
    second = create(event_cms)
    assert second["event_id"] > first["event_id"]
    assert event_cms.service.events.detail(first["event_id"])["state"] == "expired"


def test_duplicate_active_template_and_stale_schedule_are_rejected(event_cms):
    cookie, csrf = login(event_cms)
    revision = event_cms.service.events.listing()["revision"]
    create(event_cms)
    original = event_cms.service.events.schedule_path.read_bytes()
    code, result, _ = request(event_cms, "POST", "/api/events/41/deactivate",
        {"revision": revision, "reason": "Stale tab action"}, cookie, csrf)
    assert code == 409 and "refresh" in result["error"]
    with pytest.raises(ValueError, match="already has"):
        create(event_cms)
    assert event_cms.service.events.schedule_path.read_bytes() == original


@pytest.mark.parametrize("changes", [
    {"collectible_drop_chance": -0.1}, {"collectible_drop_chance": float("nan")},
    {"collectible_drop_chance": True}, {"collectible_amount": 0},
    {"collectible_amount": 1.5}, {"participation_default": "yes"},
])
def test_invalid_drop_options_leave_schedule_untouched(event_cms, changes):
    path = event_cms.service.events.schedule_path
    before = path.read_bytes()
    with pytest.raises(ValueError):
        create(event_cms, **changes)
    assert path.read_bytes() == before


def test_reactivation_can_change_run_settings_and_keeps_omitted_ones(event_cms):
    change(event_cms, 42, "activate", duration_days=7, collectible_drop_chance=0.6, collectible_amount=3)
    config = event_cms.service.events.detail(42)["config"]
    assert (config["collectible_drop_chance"], config["collectible_amount"]) == (0.6, 3)
    assert config["participation_default"] is True
    change(event_cms, 42, "deactivate")
    change(event_cms, 42, "activate", participation_default=False)
    config = event_cms.service.events.detail(42)["config"]
    assert (config["collectible_drop_chance"], config["collectible_amount"]) == (0.6, 3)
    assert config["participation_default"] is False
    change(event_cms, 42, "deactivate")
    path = event_cms.service.events.schedule_path
    before = path.read_bytes()
    with pytest.raises(ValueError, match="drop chance"):
        change(event_cms, 42, "activate", collectible_drop_chance=2)
    assert path.read_bytes() == before


def test_unknown_or_map_local_templates_cannot_be_added(event_cms):
    import sqlite3
    with sqlite3.connect(event_cms.service.static_database_path) as db:
        local = db.execute("SELECT Id FROM _Template_Events WHERE MapLocal=1 LIMIT 1").fetchone()[0]
    for template in (local, "missing-template"):
        with pytest.raises(ValueError, match="global game-event"):
            create(event_cms, template)


def test_failed_atomic_replace_keeps_valid_schedule_and_cleans_temp_file(event_cms):
    path = event_cms.service.events.schedule_path
    before = path.read_bytes()
    with patch("dso_server.services.admin_events.os.replace", side_effect=OSError("read only")):
        with pytest.raises(ValueError, match="could not save"):
            create(event_cms)
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".cms-events-*"))
    assert not any(row["action"] == "event.add" for row in event_cms.service.repository.history("admin_audit")["rows"])


def test_live_event_buff_add_remove_keeps_unrelated_effects_and_progress(event_cms, online):
    server, s = online
    s.gameplay_character.level = 50
    server._toggle_admin_speed(s)
    unrelated = copy.deepcopy(s.gameplay_persistent_status_effects)
    s.gameplay_event_progress = {"41": {"amount": 55, "claimed": ["reward-one"]}}
    s.gameplay_event_participation = {"41": False}
    result = create(event_cms)
    assert result["refresh"] == {"updated": 1, "deferred": 0, "errors": []}
    assert "xp_bonus_event_01" in s.gameplay_event_catalog.active_template_ids()
    assert active_xp_gain_multiplier(server.static_database_path, tuple(s.gameplay_persistent_status_effects.values())) == 1.25
    (effect_id,) = s.gameplay_event_status_effect_instance_ids
    snapshot = server._capture_account_state(s)
    assert all(entry.field_04 != effect_id for entry in snapshot.status_effects)
    response = change(event_cms, result["event_id"], "deactivate")
    assert response["refresh"]["updated"] == 1
    assert s.gameplay_persistent_status_effects == unrelated
    assert not s.gameplay_event_status_effect_instance_ids
    assert s.gameplay_event_progress == {"41": {"amount": 55, "claimed": ["reward-one"]}}
    assert s.gameplay_event_participation == {"41": False}
    assert "xp_bonus_event_01" not in s.gameplay_event_catalog.active_template_ids()
    packets = [call for call in s.send_reliable_ordered_split.call_args_list if call.kwargs["label_prefix"] == "admin-events-update"]
    update = decode_event_update(packets[-1].args[0], packets[-1].kwargs["total_bit_length"])
    ended = next(entry for entry in update.events if entry.event_id == result["event_id"])
    assert ended.flags[0] is False and ended.ends_at is not None
    tail = next(item for item in server.post_selection_tail if item[0] == EVENT_UPDATE_COMMAND)
    assert all(entry.event_id != result["event_id"] for entry in decode_event_update(tail[1], tail[2]).events)


def test_live_refresh_respects_level_limits_and_current_session(event_cms, online):
    server, s = online
    first = create(event_cms)
    assert first["refresh"]["updated"] == 1
    assert not s.gameplay_event_status_effect_instance_ids  # XP bonus requires level 20.
    def stale(operation):
        s.gameplay_chase_generation += 1
        return operation()
    server.admin_commands.call = stale
    result = change(event_cms, 41, "deactivate")
    assert result["refresh"]["deferred"] == 1
    assert event_cms.service.events.detail(41)["state"] == "expired"


def test_client_send_failure_is_reported_without_undoing_saved_event(event_cms, online):
    _, s = online
    s.send_reliable_ordered_split.side_effect = OSError("connection closed")
    result = create(event_cms)
    assert event_cms.service.events.detail(result["event_id"])["state"] == "active"
    assert result["refresh"]["deferred"] == 1
    assert result["refresh"]["errors"][0]["character_id"] == 42


def test_event_refresh_runs_through_real_game_service_queue(event_cms, online):
    import threading
    from dso_server.runtime.admin_commands import AdminCommandQueue

    server, session = online
    session.gameplay_character.level = 50
    server.admin_commands = AdminCommandQueue(server)
    server.start()
    try:
        result = create(event_cms)
        assert result["refresh"] == {"updated": 1, "deferred": 0, "errors": []}
        assert server.admin_commands.thread_id != threading.get_ident()
        assert session.gameplay_event_status_effect_instance_ids
        removed = change(event_cms, result["event_id"], "deactivate")
        assert removed["refresh"]["updated"] == 1
        assert not session.gameplay_event_status_effect_instance_ids
    finally:
        server.admin_commands.close()
        server.stop()


@pytest.mark.parametrize("days", [3, 7, 14, 31])
def test_duration_presets_are_exact_24_hour_days(days):
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    assert EventAdministration._end_at({"duration_days": days}, now) == now + timedelta(days=days)


@pytest.mark.parametrize("options", [
    {"duration_days": 0}, {"duration_days": -3}, {"duration_days": 4},
    {"duration_days": True}, {"duration_days": "7"}, {"duration_days": 7.0},
    {"ends_at": "not-a-date"}, {"ends_at": "2027-01-01T12:00:00"},
    {"ends_at": "2026-01-01T00:00:00Z"}, {"ends_at": 123},
    {"duration_days": 7, "ends_at": "2027-01-01T12:00:00Z"},
])
def test_invalid_end_options_are_rejected(options):
    with pytest.raises(ValueError):
        EventAdministration._end_at(options, datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_timed_run_persists_and_expires_at_exact_deadline(event_cms):
    result = create(event_cms, duration_days=7)
    manager = EventAdministration(event_cms.service, event_cms.service.events.schedule_path)
    detail = manager.detail(result["event_id"])
    start, end = datetime.fromisoformat(detail["starts_at"]), datetime.fromisoformat(detail["ends_at"])
    assert detail["mode"] == "scheduled" and end - start == timedelta(days=7)
    catalog = load_event_catalog(event_cms.service.static_database_path, manager.schedule_path)
    event = next(e for e in catalog.events if e.event_id == result["event_id"])
    assert event.state_at(end - timedelta(microseconds=1)) == "active"
    assert event.state_at(end) == "expired"
    assert "xp_bonus_event_01" not in catalog.active_template_ids(end)
    wire = next(e for e in catalog.event_update(start).events if e.event_id == event.event_id)
    assert wire.ends_at == (end.year, end.month, end.day, end.hour, end.minute, end.second)


def test_custom_deadline_reschedule_and_reactivation(event_cms):
    result = create(event_cms)
    manager = event_cms.service.events
    before = manager.detail(result["event_id"])
    custom = (datetime.now(timezone(timedelta(hours=3))) + timedelta(days=2)).replace(microsecond=0)
    change(event_cms, result["event_id"], "schedule", ends_at=custom.isoformat())
    detail = manager.detail(result["event_id"])
    assert datetime.fromisoformat(detail["ends_at"]) == custom.astimezone(timezone.utc)
    assert detail["starts_at"] == before["starts_at"] and detail["mode"] == "scheduled"
    change(event_cms, result["event_id"], "schedule", ends_at=None)
    assert manager.detail(result["event_id"])["ends_at"] is None
    change(event_cms, result["event_id"], "deactivate")
    with pytest.raises(ValueError, match="only active"):
        change(event_cms, result["event_id"], "schedule", duration_days=3)
    change(event_cms, result["event_id"], "activate", duration_days=14)
    detail = manager.detail(result["event_id"])
    assert datetime.fromisoformat(detail["ends_at"]) - datetime.fromisoformat(detail["starts_at"]) == timedelta(days=14)


def test_invalid_end_date_keeps_schedule_unchanged(event_cms):
    cookie, csrf = login(event_cms)
    before = event_cms.service.events.schedule_path.read_bytes()
    code, result, _ = request(event_cms, "POST", "/api/events/41/schedule", {
        "revision": event_cms.service.events.listing()["revision"],
        "reason": "Invalid deadline test", "ends_at": "2000-01-01T00:00:00Z"
    }, cookie, csrf)
    assert code == 400 and "future" in result["error"]
    assert event_cms.service.events.schedule_path.read_bytes() == before


def test_schedule_watcher_expires_online_buffs_after_restart(event_cms, online, monkeypatch):
    import threading
    from dso_server.services import admin_events, admin_event_refresh
    from dso_server.gameplay import event_system

    now = datetime.now(timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)
    for module in (admin_events, admin_event_refresh, event_system):
        monkeypatch.setattr(module, "datetime", Clock)
    server, session = online
    session.gameplay_character.level = 50
    server._toggle_admin_speed(session)
    unrelated = copy.deepcopy(session.gameplay_persistent_status_effects)
    result = create(event_cms, duration_days=3)
    assert session.gameplay_event_status_effect_instance_ids
    # Reconstruct the observer as at startup; a saved deadline needs no CMS tab.
    manager = EventAdministration(event_cms.service, event_cms.service.events.schedule_path)
    event_cms.service.events = manager
    manager.refresh_schedule()
    now += timedelta(days=3)
    complete = threading.Event()
    original = manager.refresh_schedule
    def refresh():
        delay = original()
        complete.set()
        return delay
    monkeypatch.setattr(manager, "refresh_schedule", refresh)
    manager.start()
    try:
        assert complete.wait(10), "scheduled expiry did not run"
        assert manager.detail(result["event_id"])["state"] == "expired"
        assert session.gameplay_persistent_status_effects == unrelated
        assert not session.gameplay_event_status_effect_instance_ids
        packets = [call for call in session.send_reliable_ordered_split.call_args_list
                   if call.kwargs["label_prefix"] == "admin-events-update"]
        update = decode_event_update(packets[-1].args[0], packets[-1].kwargs["total_bit_length"])
        assert any(e.event_id == result["event_id"] and not e.flags[0] for e in update.events)
    finally:
        manager.stop()
    assert manager._thread is None
