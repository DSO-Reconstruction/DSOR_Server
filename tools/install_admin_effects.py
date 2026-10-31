"""Install dedicated admin buffs; keep server/client wire indexes identical.

Run without --apply to preview. Existing client content must be an exact
subset of the server revision (including rowids); divergent edits are refused.
Every changed file is backed up before writing. Existing rows are updated in
place, never REPLACEd, because status wire IDs are rowid - 1.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
from pathlib import Path
import shutil
import sqlite3
import xml.etree.ElementTree as ET

if __package__ in (None, ""):
    import runpy
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "run.py"))["load_package"]()

from dso_server.services.admin_effects import ADMIN_EFFECTS
from dso_server.paths import DEFAULT_STATIC_DB_PATH

TABLE = "_Template_StatusEffect"
ITEM_TABLE = "_Template_Item"
# Server rules the client has to share. It refuses an item below RequiredLevel
# by itself (GameItem::IsUsableByPlayersLevel), so a server-side change alone
# would never reach the server: new characters start with these stones.
ITEM_PATCHES = {"item_rune_extension_stone": {"RequiredLevel": 1}}


def readonly(path):
    return sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def check_client_content(server, client):
    """Refuse to erase client-only data or relocate its existing wire IDs."""
    with closing(readonly(server)) as source, closing(readonly(client)) as target:
        schemas = [dict(db.execute("SELECT name, sql FROM sqlite_master "
                                  "WHERE type='table'")) for db in (source, target)]
        if schemas[0] != schemas[1]:
            raise ValueError("client/server schemas differ; reconcile content before installing")
        # A stock client still holds the rows this tool patches on the server.
        patched_items = {
            row[0] for row in source.execute(
                f"SELECT rowid FROM {quoted(ITEM_TABLE)} WHERE Id IN ("
                + ",".join("?" for _ in ITEM_PATCHES) + ")", tuple(ITEM_PATCHES),
            )
        } if ITEM_TABLE in schemas[0] else set()
        for table in schemas[0]:
            sql = f"SELECT rowid, * FROM {quoted(table)} ORDER BY rowid"
            rows = {row[0]: row for row in source.execute(sql)}
            for row in target.execute(sql):
                if table == ITEM_TABLE and row[0] in patched_items:
                    continue
                if rows.get(row[0]) != row:
                    raise ValueError(f"client content differs: {table} row {row[0]}; "
                                     "reconcile content before installing")


def desired_effects(db):
    columns = [r[1] for r in db.execute(f"PRAGMA table_info({TABLE})")]
    def template(resource):
        row = db.execute(f"SELECT * FROM {TABLE} WHERE Id=?", (resource,)).fetchone()
        if row is None:
            raise ValueError(f"missing native status template: {resource}")
        return dict(zip(columns, row))
    speeds = {r[0] for r in db.execute("SELECT RunSpeed FROM _Template_Player")}
    if speeds != {5.0}:
        raise ValueError("player base speeds changed; review the +100% admin speed preset")
    rows = []
    for preset in ADMIN_EFFECTS.values():
        # This donor has no auras, animations, class/map restrictions, or
        # unrelated modifiers. Copy only the icon from each visible donor.
        row = template("pvp_talent_speed")
        icon = template(preset["icon_template"])
        row.update(Id=preset["resource"], StatusEffectDuration=0.0,
                   StatusEffectDurationPvP=0.0, StatusEffectTickRate=1.0,
                   MaxStackSize=1, KeepOnDeath=1, OnResumeStartModifiers=1,
                   ResetRunTime=1, Loca=1,
                   StartModifiers=preset["modifiers"],
                   IconBrush=icon["IconBrush"], IconConfiguration=icon["IconConfiguration"],
                   Groups="Special;BuffUI;AdminBuff", ExclusiveGroup="",
                   LocaleId=preset["resource"] + "Title")
        rows.append(row)
    return rows


def patched_rows(db):
    """Everything ``patch_database`` writes, to tell whether it changes anything."""
    rows = list(db.execute(f"SELECT rowid, * FROM {TABLE} ORDER BY rowid"))
    if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (ITEM_TABLE,)).fetchone():
        rows += db.execute(
            f"SELECT Id, RequiredLevel FROM {quoted(ITEM_TABLE)} WHERE Id IN ("
            + ",".join("?" for _ in ITEM_PATCHES) + ") ORDER BY Id", tuple(ITEM_PATCHES),
        ).fetchall()
    return rows


def patch_database(db):
    """Apply to the supplied transaction, preserving all existing rowids."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (ITEM_TABLE,)).fetchone():
        for item_id, values in ITEM_PATCHES.items():
            db.execute(f"UPDATE {quoted(ITEM_TABLE)} SET "
                       + ",".join(f"{quoted(name)}=?" for name in values) + " WHERE Id=?",
                       [*values.values(), item_id])
    installed = {}
    for row in desired_effects(db):
        existing = db.execute(f"SELECT rowid FROM {TABLE} WHERE Id=?", (row["Id"],)).fetchone()
        if existing:
            names = [name for name in row if name != "Id"]
            db.execute(f"UPDATE {TABLE} SET " + ",".join(f"{quoted(n)}=?" for n in names)
                       + " WHERE Id=?", [row[n] for n in names] + [row["Id"]])
        else:
            names = list(row)
            db.execute(f"INSERT INTO {TABLE} (" + ",".join(map(quoted, names))
                       + ") VALUES (" + ",".join("?" for _ in names) + ")", list(row.values()))
        wire_id = db.execute(f"SELECT rowid-1 FROM {TABLE} WHERE Id=?", (row["Id"],)).fetchone()[0]
        if not 0 <= wire_id <= 65535:
            raise ValueError("status effect wire index exceeds uint16")
        installed[row["Id"]] = wire_id
    return installed


def locale_changes(language_root):
    changes = {}
    paths = sorted(Path(language_root).glob("*/db.statuseffect.xml"))
    if not paths:
        raise ValueError(f"no status-effect locales found in {language_root}")
    for path in paths:
        tree = ET.parse(path)
        category = next((c for c in tree.getroot()
                         if c.get("name") == "db.statuseffect.table"), None)
        if category is None:
            raise ValueError(f"missing status-effect locale category in {path}")
        existing = {item.get("name"): item for item in category.iter("item")}
        changed = False
        for preset in ADMIN_EFFECTS.values():
            key = preset["resource"] + "Title"
            title = "Admin: " + preset["label"]
            item = existing.get(key)
            if item is None:
                item = ET.SubElement(category, "item", name=key)
            if item.text != title:
                item.text = title
                changed = True
        if changed:
            changes[path] = ET.tostring(tree.getroot(), encoding="utf-8", xml_declaration=True)
    return changes


def install_content(server, client, language_root, *, apply=False):
    server, client = Path(server).resolve(), Path(client).resolve()
    if server == client:
        raise ValueError("server and client must be separate files")
    check_client_content(server, client)
    original_hashes = (digest(server), digest(client))
    locales = locale_changes(language_root)
    with closing(readonly(server)) as source, closing(sqlite3.connect(":memory:")) as preview:
        source.backup(preview)
        before = patched_rows(preview)
        installed = patch_database(preview)
        changed = before != patched_rows(preview)
    report = {"effects": installed, "database_changed": changed,
              "client_sync": changed or original_hashes[0] != original_hashes[1],
              "locales_changed": len(locales), "applied": apply, "backups": []}
    if not apply:
        return report
    if original_hashes != (digest(server), digest(client)):
        raise ValueError("content changed during preview; run installation again")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    # Back up all targets before the first mutation.
    targets = ([server] if changed else []) + ([client] if report["client_sync"] else [])
    targets += list(locales)
    for path in targets:
        backup = path.with_name(path.name + f".admin-effects-{stamp}.bak")
        shutil.copy2(path, backup)
        report["backups"].append(str(backup))
    if changed:
        with closing(sqlite3.connect(server)) as db, db:
            patch_database(db)
    if report["client_sync"]:
        shutil.copy2(server, client)
    for path, data in locales.items():
        path.write_bytes(data)
    if digest(server) != digest(client):
        raise RuntimeError("client/server hash mismatch after installation")
    report["sha256"] = digest(server)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-db", type=Path, default=DEFAULT_STATIC_DB_PATH)
    parser.add_argument("--client-db", type=Path, required=True)
    parser.add_argument("--language-root", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    import json
    report = install_content(args.server_db, args.client_db,
                             args.language_root or args.client_db.parent.parent / "language",
                             apply=args.apply)
    print(json.dumps(report, indent=2))
    if args.apply:
        print("Restart both the server and client to load the matching content revision.")


if __name__ == "__main__":
    main()
