"""The launcher's temporary old-style item art must undo itself completely."""

from __future__ import annotations

import sqlite3

from client_settings import (
    apply_old_item_visuals,
    client_data_root,
    remember_client_process,
    restore_old_item_visuals,
)


def _client_tree(tmp_path):
    """A client export_win32 with the icon configs and rarity textures."""
    root = tmp_path / "dlcache" / "export_win32"
    (root / "db").mkdir(parents=True)
    textures = root / "textures" / "icons_toolkit"
    textures.mkdir(parents=True)
    for rarity in ("common", "uncommon", "magic", "rare", "epic", "unique"):
        (textures / f"bg_rarity_{rarity}.dds").write_bytes(b"new " + rarity.encode())
        (textures / f"bg_rarity_{rarity}_old.dds").write_bytes(b"old " + rarity.encode())
    (textures / "bg_rarity_set.dds").write_bytes(b"set")
    db = sqlite3.connect(root / "db" / "static.db4")
    with db:
        db.execute(
            'CREATE TABLE "_Template_IconConfig" '
            "(Id TEXT PRIMARY KEY, RarityControlled INTEGER, FrameTexture TEXT, "
            "InfoTexture TEXT, BackgroundTexture TEXT)"
        )
        db.executemany(
            'INSERT INTO "_Template_IconConfig" VALUES (?,?,?,?,?)',
            [
                ("item_equipment_base", 1, "icons_toolkit/frame_slim_basic", "", ""),
                ("item_equipment_unique", 1, "icons_toolkit/frame_slim_unique", "", ""),
                ("item_equipment_set", 1, "icons_toolkit/frame_slim_set", "", ""),
                ("item_equipment_unique_old", 0, "icons_toolkit/frame_unique", "",
                 "icons_toolkit/bg_rarity_unique_old"),
            ],
        )
    db.close()
    return root


def _frames(root):
    db = sqlite3.connect(root / "db" / "static.db4")
    try:
        return dict(db.execute('SELECT Id, FrameTexture FROM "_Template_IconConfig"'))
    finally:
        db.close()


def test_apply_then_restore_leaves_the_client_untouched(tmp_path):
    root = _client_tree(tmp_path)
    before = _frames(root)
    db_path = root / "db" / "static.db4"
    before_db = db_path.read_bytes()
    textures = root / "textures" / "icons_toolkit"
    before_textures = {
        path.name: path.read_bytes() for path in sorted(textures.iterdir())
    }

    visuals = apply_old_item_visuals(root.parent)

    assert visuals is not None
    assert client_data_root(root.parent) == root
    frames = _frames(root)
    assert frames["item_equipment_base"] == "icons_toolkit/frame_basic"
    assert frames["item_equipment_unique"] == "icons_toolkit/frame_unique"
    assert frames["item_equipment_set"] == "icons_toolkit/frame_slim_set"
    assert frames["item_equipment_unique_old"] == "icons_toolkit/frame_unique"
    assert (textures / "bg_rarity_common.dds").read_bytes() == b"old common"
    assert (textures / "bg_rarity_unique.dds").read_bytes() == b"old unique"
    assert (textures / "bg_rarity_set.dds").read_bytes() == b"set"

    restore_old_item_visuals(visuals)

    assert _frames(root) == before
    assert db_path.read_bytes() == before_db
    assert {
        path.name: path.read_bytes() for path in sorted(textures.iterdir())
    } == before_textures
    assert not visuals.journal_path.exists()


def test_a_live_client_keeps_its_patch(tmp_path):
    root = _client_tree(tmp_path)
    visuals = apply_old_item_visuals(root.parent)
    assert visuals is not None
    remember_client_process(visuals, 0)

    # pid 0 is never a running client, so a second launch may reclaim the patch.
    again = apply_old_item_visuals(root.parent)
    assert again is not None
    restore_old_item_visuals(again)


def test_a_killed_launcher_is_cleaned_up_by_the_next_one(tmp_path):
    root = _client_tree(tmp_path)
    before = _frames(root)
    first = apply_old_item_visuals(root.parent)
    assert first is not None
    # The launcher died before the game closed: the journal still says pid 0.
    assert first.journal_path.exists()

    second = apply_old_item_visuals(root.parent)
    assert second is not None
    restore_old_item_visuals(second)
    assert _frames(root) == before
