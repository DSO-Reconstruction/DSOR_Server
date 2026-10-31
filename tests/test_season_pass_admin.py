"""Season Pass seasons, CMS reward edits and the client copies of the table."""

from contextlib import closing
import json
import sqlite3
import struct
from types import SimpleNamespace
import zlib

import pytest

from dso_server.tests.test_admin_cms import cms, login, request
from dso_server.services import events as game_events
from dso_server.services.admin import AdminError
from dso_server.services.admin_events import EventAdministration
from dso_server.services.admin_season_pass import SeasonPassAdministration
from dso_server.services.content import StaticDatabasePublisher
from dso_server.services.season_pass import SeasonPassService
from dso_server.gameplay.season_pass import (
    load_season_pass_catalog, read_season_pass_rewards, season_pass_event_id,
    season_pass_state_for_season,
)


def reward_table(source, target):
    """A small database holding only the static Season Pass reward table."""
    with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as db:
        schema = db.execute("SELECT sql FROM sqlite_master WHERE name='_Template_SeasonPassRewards'").fetchone()[0]
        rows = db.execute("SELECT * FROM _Template_SeasonPassRewards ORDER BY rowid").fetchall()
    with closing(sqlite3.connect(target)) as db:
        db.execute(schema)
        db.executemany(f"INSERT INTO _Template_SeasonPassRewards VALUES ({','.join('?' * len(rows[0]))})", rows)
        db.commit()
    return target


@pytest.fixture
def season_cms(cms, tmp_path, monkeypatch):
    schedule = tmp_path / "events.json"
    schedule.write_text(json.dumps({"events": []}), encoding="utf-8")
    monkeypatch.setattr(game_events, "EVENT_SCHEDULE_PATH", schedule)
    cms.service.events = EventAdministration(cms.service, schedule)
    cms.client_db = reward_table(cms.service.static_database_path, tmp_path / "client.db4")
    cms.config = tmp_path / "season_pass.json"
    cms.service.season_pass = SeasonPassAdministration(cms.service, cms.config, [cms.client_db])
    return cms


def post(cms, path, **payload):
    return cms.service.post(path, {"reason": "Season operations ticket", **payload}, cms.admin, "")


def level_payload(cms, level, **changes):
    row = next(r for r in cms.service.season_pass.rewards()["rows"] if r["level"] == level)
    payload = {"revision": cms.service.season_pass.rewards()["revision"], "exp": row["exp"],
               **{track: {"item_id": row["tracks"][track]["item_id"], "amount": row["tracks"][track]["amount"]}
                  for track in ("free", "gold", "red")}}
    payload.update(changes)
    return payload


def test_progress_is_bound_to_one_season():
    saved = {"level": 12, "experience": 2400, "pass_types": [1, 4], "claimed_awards": [1, 101],
             "awarded_awards": [1, 101], "mission_progress": {"2": 1}, "login_day": "2026-10-24"}
    adopted = season_pass_state_for_season(saved, 70000)
    assert adopted["season_id"] == 70000 and adopted["level"] == 12  # unbound saves keep their progress
    assert season_pass_state_for_season(adopted, None) == adopted     # between seasons nothing moves
    fresh = season_pass_state_for_season(adopted, 70001)
    assert fresh["season_id"] == 70001 and fresh["pass_types"] == (1, 4)
    assert (fresh["level"], fresh["experience"], fresh["claimed_awards"], fresh["awarded_awards"],
            fresh["mission_progress"], fresh["login_day"]) == (1, 0, (), (), {}, "")


def test_missions_and_level_purchases_wait_for_a_season(cms):
    server = SimpleNamespace(_season_pass_season=lambda: None)
    session = SimpleNamespace(gameplay_character=object(), connected=True, retired=False,
                              gameplay_exit_transition_started=False)
    assert SeasonPassService._season_pass_feed(server, session, "DailyLogin") is False
    assert season_pass_event_id(cms.service.static_database_path) == "event_season_ticket"


def test_reward_edit_is_granted_shown_and_audited(season_cms):
    cms = season_cms
    result = post(cms, "/api/season-pass/rewards/2", **level_payload(
        cms, 2, exp=450, gold={"item_id": "season_coin", "amount": 777}))
    assert result["databases"][0]["error"] is None and result["databases"][0]["changed"] == 1
    saved = json.loads(cms.config.read_text(encoding="utf-8"))
    assert saved == {"rewards": {"standard": {"2": {"exp": 450, "gold": "Loot:season_coin,777"}}}}
    catalog = load_season_pass_catalog(cms.service.static_database_path, overrides_path=cms.config)
    assert catalog.reward_levels[2][1] == "Loot:season_coin,777"
    assert dict(catalog.level_thresholds)[2] == 450 and catalog.level_for_experience(449) == 1
    with closing(sqlite3.connect(cms.client_db)) as db:
        assert read_season_pass_rewards(db, "standard")[2][:3] == (450, catalog.reward_levels[2][0], "Loot:season_coin,777")
    row = cms.service.season_pass.rewards()["rows"][1]
    assert row["edited"] and row["tracks"]["gold"]["edited"] and not row["tracks"]["free"]["edited"]
    assert cms.service.season_pass.database_status()[0]["in_sync"]
    actions = [row["action"] for row in cms.service.repository.history("admin_audit")["rows"]]
    assert {"season_pass.reward", "season_pass.reward.clients"} <= set(actions)
    # Resetting drops the override and restores the client copy.
    post(cms, "/api/season-pass/rewards/2", reset=True, revision=cms.service.season_pass.rewards()["revision"])
    assert json.loads(cms.config.read_text(encoding="utf-8")) == {"rewards": {"standard": {}}}
    with closing(sqlite3.connect(cms.client_db)) as db, \
            closing(sqlite3.connect(f"file:{cms.service.static_database_path.as_posix()}?mode=ro", uri=True)) as static:
        assert read_season_pass_rewards(db, "standard") == read_season_pass_rewards(static, "standard")


def test_reward_edits_are_validated(season_cms):
    cms = season_cms
    with pytest.raises(ValueError, match="experience must be from"):
        post(cms, "/api/season-pass/rewards/2", **level_payload(cms, 2, exp=100))
    with pytest.raises(ValueError, match="not in the static database"):
        post(cms, "/api/season-pass/rewards/2", **level_payload(cms, 2, free={"item_id": "no_such_item", "amount": 1}))
    with pytest.raises(ValueError, match="amount must be"):
        post(cms, "/api/season-pass/rewards/2", **level_payload(cms, 2, red={"item_id": "season_coin", "amount": 0}))
    with pytest.raises(ValueError, match="no changes"):
        post(cms, "/api/season-pass/rewards/2", **level_payload(cms, 2))
    with pytest.raises(AdminError) as conflict:
        post(cms, "/api/season-pass/rewards/2", **level_payload(cms, 2, exp=450, revision="stale"))
    assert conflict.value.status == 409
    with pytest.raises(ValueError, match="reason"):
        cms.service.post("/api/season-pass/sync", {"reason": ""}, cms.admin, "")
    assert not cms.config.exists()


def test_season_lifespan_uses_the_season_pass_event(season_cms):
    cms = season_cms
    overview = cms.service.season_pass.overview()
    assert overview["manageable"] and overview["current"] is None and overview["levels"] == 60
    assert overview["databases"][0]["in_sync"]  # the client copy starts identical
    started = cms.service.post("/api/events", {"revision": overview["revision"], "template_id": overview["template_id"],
                                               "duration_days": 31, "reason": "Season 1"}, cms.admin, "")
    overview = cms.service.season_pass.overview()
    assert overview["current"]["event_id"] == started["event_id"] and overview["current"]["state"] == "active"
    cms.service.post(f"/api/events/{started['event_id']}/deactivate",
                     {"revision": overview["revision"], "reason": "Season over"}, cms.admin, "")
    overview = cms.service.season_pass.overview()
    assert overview["current"] is None and overview["runs"][0]["state"] == "expired"


def test_season_pass_pages_require_a_signed_in_administrator(season_cms):
    for path in ("season-pass", "season-pass/rewards", "season-pass/missions"):
        assert request(season_cms, "GET", "/api/" + path)[0] == 401
    cookie, csrf = login(season_cms)
    assert request(season_cms, "GET", "/api/season-pass/missions", cookie=cookie)[1]["rows"]
    assert request(season_cms, "POST", "/api/season-pass/sync", {"reason": "Sync clients"}, cookie)[0] == 403
    assert request(season_cms, "POST", "/api/season-pass/sync", {"reason": "Sync clients"}, cookie, csrf)[0] == 200
    assert request(season_cms, "GET", "/admin_season_pass.js")[0] == 200


def test_dsor_clients_download_the_edited_reward_table(season_cms, tmp_path):
    cms = season_cms
    published = reward_table(cms.service.static_database_path, tmp_path / "server.db4")
    publisher = StaticDatabasePublisher(published, cms.config)
    plain = publisher.describe()["sha256"]
    post(cms, "/api/season-pass/rewards/3", **level_payload(cms, 3, free={"item_id": "season_coin", "amount": 5}))
    sha, packed = publisher.packed()
    assert sha != plain
    copy = tmp_path / "downloaded.db4"
    copy.write_bytes(zlib.decompress(packed[8:]))
    assert struct.unpack("<I", packed[4:8])[0] == copy.stat().st_size
    with closing(sqlite3.connect(copy)) as db:
        assert read_season_pass_rewards(db, "standard")[3][1] == "Loot:season_coin,5"
    with closing(sqlite3.connect(published)) as db:  # the server's own file is untouched
        assert read_season_pass_rewards(db, "standard")[3][1] != "Loot:season_coin,5"
