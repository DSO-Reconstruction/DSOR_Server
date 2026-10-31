"""Mount effect ids and timings come from the active static database, whatever
its build (gameplay/local_mount_state.py)."""

import sqlite3

from dso_server.gameplay.local_mount_state import BLACK_BEAR_MOUNT, find_mount
from dso_server.gameplay.stickerbook_state import load_sticker_definition
from dso_server.paths import DEFAULT_STATIC_DB_PATH


def _wire_id(effect: str) -> int:
    with sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True) as db:
        return db.execute(
            "SELECT rowid FROM _Template_StatusEffect WHERE Id=?", (effect,)
        ).fetchone()[0] - 1


def test_black_bear_ids_and_timing_follow_the_database():
    bear = find_mount(BLACK_BEAR_MOUNT.stickerbook_resource_id)
    assert bear.cast_effect_id == _wire_id("cast_mount_black_bear_01")
    assert bear.ride_effect_id == _wire_id("ride_mount_black_bear_01")
    assert bear.cast_duration_ticks == BLACK_BEAR_MOUNT.cast_duration_ticks
    assert bear.ride_duration_ticks == BLACK_BEAR_MOUNT.ride_duration_ticks
    assert bear.stickerbook_state == BLACK_BEAR_MOUNT.stickerbook_state


def test_sticker_book_bear_carries_the_database_timing():
    sticker = load_sticker_definition(
        DEFAULT_STATIC_DB_PATH, BLACK_BEAR_MOUNT.stickerbook_resource_id
    )
    assert sticker.mount.cast_effect_id == _wire_id("cast_mount_black_bear_01")
    assert sticker.mount.cast_duration_ticks == BLACK_BEAR_MOUNT.cast_duration_ticks


def test_a_database_without_the_mount_falls_back_to_the_local_entry(tmp_path):
    empty = tmp_path / "empty.db4"
    sqlite3.connect(empty).close()
    assert find_mount(BLACK_BEAR_MOUNT.stickerbook_resource_id, empty) == BLACK_BEAR_MOUNT
