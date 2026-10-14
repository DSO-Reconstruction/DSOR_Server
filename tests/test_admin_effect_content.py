"""Admin content installation, native contracts, and legacy-save migration."""
from dataclasses import replace
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import StatusEffectEntry
from dso_server.services.admin_effects import ADMIN_EFFECTS, restore_admin_effects
from dso_server.tools.install_admin_effects import install_content, TABLE, digest


@pytest.fixture
def bundle(tmp_path):
    server, client = tmp_path / "server.db4", tmp_path / "client.db4"
    with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as source, sqlite3.connect(server) as target:
        for table in (TABLE, "_Template_Player", "_Template_ActorAttributes"):
            target.execute(source.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0])
            if table == TABLE:
                donors = {"pvp_talent_speed"} | {p["icon_template"] for p in ADMIN_EFFECTS.values()}
                rows = [r for r in source.execute(f"SELECT rowid,* FROM {table}") if r[1] in donors]
            else:
                rows = list(source.execute(f"SELECT rowid,* FROM {table}"))
            target.executemany(f"INSERT INTO {table} VALUES (" + ",".join("?" for _ in rows[0][1:]) + ")",
                               [r[1:] for r in rows])
    shutil.copy2(server, client)
    language = tmp_path / "language"
    for lang in ("en", "de"):
        path = language / lang / "db.statuseffect.xml"
        path.parent.mkdir(parents=True)
        path.write_text('<language><category name="db.statuseffect.table">'
                        '<item name="ExistingTitle">Existing text</item></category></language>')
    return server, client, language


def rows(path, table=TABLE):
    with sqlite3.connect(path) as db:
        return dict((r[0], r) for r in db.execute(f"SELECT rowid,* FROM {table}"))


def test_install_preview_backups_wire_ids_and_idempotence(bundle):
    server, client, language = bundle
    old_bytes = server.read_bytes()
    old_rows = rows(server)
    limits = rows(server, "_Template_ActorAttributes")
    old_locale = (language / "en/db.statuseffect.xml").read_bytes()
    preview = install_content(*bundle)
    assert preview["database_changed"] and preview["locales_changed"] == 2
    assert server.read_bytes() == client.read_bytes() == old_bytes
    assert (language / "en/db.statuseffect.xml").read_bytes() == old_locale
    applied = install_content(*bundle, apply=True)
    assert applied["effects"] == preview["effects"]
    assert digest(server) == digest(client)
    assert all(rows(server)[key] == value for key, value in old_rows.items())
    assert rows(server, "_Template_ActorAttributes") == limits
    backups = {str(Path(p).name).split(".admin-effects-")[0]: Path(p) for p in applied["backups"]}
    assert backups["server.db4"].read_bytes() == old_bytes
    assert backups["client.db4"].read_bytes() == old_bytes
    for path in language.glob("*/db.statuseffect.xml"):
        strings = {item.get("name"): item.text for item in ET.parse(path).iter("item")}
        assert strings["ExistingTitle"] == "Existing text"
        assert all(strings[p["resource"] + "Title"] == "Admin: " + p["label"]
                   for p in ADMIN_EFFECTS.values())
    rerun = install_content(*bundle, apply=True)
    assert not rerun["database_changed"] and not rerun["client_sync"]
    assert rerun["locales_changed"] == 0 and not rerun["backups"]
    assert applied["sha256"] == rerun["sha256"]
    assert applied["effects"] == rerun["effects"]


def test_divergent_client_data_is_refused_before_any_writes(bundle):
    server, client, _ = bundle
    with sqlite3.connect(client) as db:
        db.execute(f"UPDATE {TABLE} SET MaxStackSize=42 WHERE Id='pvp_talent_speed'")
    before = (server.read_bytes(), client.read_bytes())
    with pytest.raises(ValueError, match="client content differs"):
        install_content(*bundle, apply=True)
    assert before == (server.read_bytes(), client.read_bytes())
    assert not list(server.parent.rglob("*.bak"))


def test_server_additions_are_preserved_when_syncing_client(bundle):
    server, client, _ = bundle
    with sqlite3.connect(server) as db:
        db.execute("INSERT INTO _Template_ActorAttributes VALUES ('TestAttribute',0,0,100)")
    before = rows(server, "_Template_ActorAttributes")
    install_content(*bundle, apply=True)
    assert rows(client, "_Template_ActorAttributes") == before


def test_new_templates_have_icons_independent_ids_and_no_unrelated_gameplay(bundle):
    install_content(*bundle, apply=True)
    with sqlite3.connect(bundle[0]) as db:
        db.row_factory = sqlite3.Row
        for preset in ADMIN_EFFECTS.values():
            row = db.execute(f"SELECT * FROM {TABLE} WHERE Id=?", (preset["resource"],)).fetchone()
            assert row["StartModifiers"] == preset["modifiers"]
            assert row["IconBrush"] and row["IconConfiguration"] == "statuseffect_buff"
            assert row["Loca"] == 1 and "BuffUI" in row["Groups"].split(";")
            assert row["KeepOnDeath"] == row["OnResumeStartModifiers"] == row["MaxStackSize"] == 1
            assert row["StatusEffectDuration"] == row["StatusEffectDurationPvP"] == 0
            assert all(not row[key] for key in (
                "ExclusiveGroup", "DisableMaps", "DisableRulesets", "CharClass", "AuraEffectId",
                "TickModifiers", "DoneModifiers", "StopModifiers", "StartSequence", "TickSequence"))
        base = {r[0] for r in db.execute("SELECT RunSpeed FROM _Template_Player")}
        cap = db.execute("SELECT UpperLimit FROM _Template_ActorAttributes WHERE Id='MovementSpeed'").fetchone()[0]
    assert base == {ADMIN_EFFECTS["speed"]["values"][0]}
    assert cap == 13  # Ordinary gameplay keeps its native cap.
    assert ADMIN_EFFECTS["speed"]["modifiers"] == "Speed:$0,absolute,MovementBonus;"


@pytest.mark.parametrize("preset", ADMIN_EFFECTS.values(), ids=ADMIN_EFFECTS.keys())
def test_restore_migrates_only_admin_signature_and_deduplicates(preset):
    catalog = SimpleNamespace(effect_ids={preset["resource"]: 99, preset["legacy_resource"]: 55})
    entry = StatusEffectEntry(effect_id=55, field_04=10, field_08=8000, field_14=0,
                              field_0c=0, field_10=0, field_18=0, field_1c=100,
                              field_20=1, field_25=False, field_26=False, field_39=True, has_details=True,
                              values=preset["legacy_values"])
    timed = replace(entry, field_04=11, field_10=125)
    native = replace(entry, field_04=12, values=(0.1, 0, 0, 0, 0))
    talent = replace(entry, field_04=13, field_26=True)
    ending = replace(entry, field_04=14, field_0c=9000)
    entries = {e.field_04: e for e in (entry, timed, native, talent, ending)}
    restored = restore_admin_effects(entries, catalog)
    assert restored[10].effect_id == 99 and restored[10].values == preset["values"]
    assert restored[10].field_08 == 0 and restored[10].field_14 == 25
    assert all(restored[i] is entries[i] for i in (11, 12, 13, 14))
    dedicated = replace(restored[10], field_04=20, field_08=8000)
    entries[20] = dedicated
    restored = restore_admin_effects(entries, catalog)
    assert 10 not in restored and restored[20].field_08 == 0
    assert len(restored) == len(entries) - 1
    assert restore_admin_effects(entries, SimpleNamespace(effect_ids={})) == entries
