"""CMS action that fills a character's sticker book."""

from dataclasses import replace
import sqlite3

from dso_server.gameplay.stickerbook_state import (
    _rarity_upgrade_stages, _split, collector_item_rewards, load_sticker_definition,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.player_init_protocol import StickerbookEntry
from dso_server.tests.test_admin_cms import cms, login, request
from dso_server.tests.test_admin_live import online

PATH = "/api/characters/42/unlock-all-stickers"
REASON = {"reason": "Test collection"}


def _rows():
    database = sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    return database.execute(
        "SELECT Id, Items, RarityUpgradeItems FROM _Template_StickerbookItem WHERE IsLive<>0"
    ).fetchall()


def _upgradable_row():
    return next(row for row in _rows() if row["RarityUpgradeItems"])


def _unlock(cms):
    cookie, csrf = login(cms)
    return request(cms, "POST", PATH, REASON, cookie, csrf)


def test_every_book_row_is_added_once(cms):
    code, result, _ = _unlock(cms)
    assert code == 200, result
    rows = _rows()
    entries = cms.store.get(42).stickerbook_entries
    assert result == {"ok": True, "newly_unlocked": len(rows), "total_unlocked": len(rows)}
    assert [entry.resource_id for entry in entries] == sorted(entry.resource_id for entry in entries)
    assert all((entry.field_14, entry.field_18) == (2, 0) for entry in entries)
    owned = {entry.resource_id for entry in entries}
    assert len(owned) == len(rows)
    assert all(owned.intersection(_split(row["Items"])) for row in rows)
    assert len(collector_item_rewards(DEFAULT_STATIC_DB_PATH, owned)) == 125
    assert any(row["action"] == "character.stickerbook.unlock_all"
               for row in cms.service.repository.history("admin_audit")["rows"])
    code, result, _ = _unlock(cms)
    assert code == 200 and result == {
        "ok": True, "newly_unlocked": 0, "total_unlocked": len(rows),
    }


def test_owned_rows_keep_their_tier_and_favorite(cms):
    row = _upgradable_row()
    base = _split(row["Items"])[0]
    upgraded = _rarity_upgrade_stages(row["Items"], row["RarityUpgradeItems"])[-1][0]
    favorite = StickerbookEntry(3, 7, upgraded)
    cms.store.put(replace(cms.store.get(42), stickerbook_entries=(favorite,)), flush=True)
    code, result, _ = _unlock(cms)
    assert code == 200, result
    assert result["newly_unlocked"] == result["total_unlocked"] - 1
    entries = {entry.resource_id: entry for entry in cms.store.get(42).stickerbook_entries}
    assert entries[upgraded] == favorite
    assert base not in entries


def test_paid_rewards_stay_paid(cms):
    paid = ("sb_mounts_eggs:1:ammunition_damage_rare",)
    cms.store.put(replace(cms.store.get(42), stickerbook_rewards=paid), flush=True)
    code, result, _ = _unlock(cms)
    assert code == 200, result
    assert cms.store.get(42).stickerbook_rewards == paid


def test_an_online_character_is_rejected(cms, online):
    code, result, _ = _unlock(cms)
    assert code == 400 and "offline" in result["error"]
    assert cms.store.get(42).stickerbook_entries == ()


def test_the_gold_dragon_brood_resolves_through_its_authored_ride():
    definition = load_sticker_definition(DEFAULT_STATIC_DB_PATH, "epicmount_dragonbrood_gold")
    assert definition is not None and definition.kind == "mount"
    assert definition.mount.mount_resource_id == "mount_dragonbrood_gold_01"
