"""Add return portals between Greyling Forest, Tidal Wildwood, and Kingshill.

The extracted map databases are local assets and are not committed. Run this
after refreshing them with ``setup_client_assets.py``. Pass the client's
``dlcache/export_win32/maps`` directory as an additional ``--map-dir`` so the
same scene entities are present in the running client. Pass its
``dlcache/export_win32/db/static.db4`` with ``--static-db`` to supply a readable
Greyling Forest map name to the client.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import struct
import uuid
from pathlib import Path


ROUTES = {
    "k0002_greylingforest": {
        "entry_name": "Entrypoint",
        "destination": "k0001_maincity",
        "destination_entry": "Entrypoint",
        "source_prefix": "k0002_greylingforest_diff_",
        "source_destination": "k0003_tidalwildwood_diff_",
        "exit_prefix": "k0002_greylingforest_to_k0001_maincity",
        "single_root_gate": True,
    },
    "k0003_tidalwildwood": {
        "entry_name": "Entrypoint",
        "destination": "k0002_greylingforest_diff_{difficulty}",
        "destination_entry": "Entrypoint",
        "source_prefix": "k0003_tidalwildwood_diff_",
        "source_destination": "k0004_hallofparadoxes_diff_",
        "exit_prefix": "k0003_tidalwildwood_to_k0002_greylingforest",
    },
}


def _transform_at_entry(template: bytes, entry: bytes, *, offset_x: float = 0.0) -> bytes:
    if len(template) != 64 or len(entry) != 64:
        raise ValueError("Map transforms must be 64-byte matrices.")
    result = bytearray(template)
    x, y, z = struct.unpack_from("<3f", entry, 48)
    struct.pack_into("<3f", result, 48, x + offset_x, y, z)
    return bytes(result)


def patch_map_database(path: Path) -> int:
    map_id = path.stem
    route = ROUTES.get(map_id)
    if route is None:
        return 0
    if not path.is_file():
        raise FileNotFoundError(path)

    backup = path.with_name(path.name + ".before-k-map-return-gates")
    if not backup.exists():
        shutil.copy2(path, backup)

    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        columns = [
            row[1]
            for row in connection.execute(
                'PRAGMA table_info("_Instance_InteractExit")'
            )
        ]
        if not columns:
            raise RuntimeError(f"Missing _Instance_InteractExit in {path}")

        entry_row = connection.execute(
            "SELECT Transform FROM _Instance_Entry WHERE Name=? ORDER BY rowid LIMIT 1",
            (route["entry_name"],),
        ).fetchone()
        if entry_row is None:
            raise RuntimeError(f"Missing {route['entry_name']} in {path}")

        existing = {
            str(row[0])
            for row in connection.execute("SELECT Id FROM _Instance_InteractExit")
        }
        if route.get("single_root_gate"):
            keep_id = f"{route['exit_prefix']}_diff_00"
            connection.execute(
                "DELETE FROM _Instance_InteractExit WHERE Id LIKE ? AND Id<>?",
                (route["exit_prefix"] + "_diff_%", keep_id),
            )
            connection.execute(
                "UPDATE _Instance_InteractExit "
                "SET ParentNodeIndex=0, EventSetRow=-1, State='active', "
                "ExitURL=?, EntryName=?, Name=?, _ID=? WHERE Id=?",
                (
                    route["destination"].format(difficulty="00"),
                    route["destination_entry"],
                    keep_id, keep_id, keep_id,
                ),
            )
            existing = {
                str(row[0])
                for row in connection.execute("SELECT Id FROM _Instance_InteractExit")
            }
        template_rows = connection.execute(
            "SELECT * FROM _Instance_InteractExit "
            "WHERE Id LIKE ? ORDER BY Id",
            (route["source_prefix"] + "%",),
        ).fetchall()
        if not template_rows:
            raise RuntimeError(f"Missing tiered gate templates in {path}")

        by_difficulty = {}
        for row in template_rows:
            values = dict(zip(columns, row))
            if not str(values["ExitURL"]).startswith(route["source_destination"]):
                continue
            suffix = str(values["Id"]).split("_diff_", 1)[-1][:2]
            by_difficulty[suffix] = values

        added = 0
        source_variations = sorted(by_difficulty.items())
        if route.get("single_root_gate"):
            source_variations = source_variations[:1]
        for difficulty, source in source_variations:
            exit_id = f"{route['exit_prefix']}_diff_{difficulty}"
            if exit_id in existing:
                if route.get("single_root_gate"):
                    connection.execute(
                        "UPDATE _Instance_InteractExit "
                        "SET ParentNodeIndex=0, EventSetRow=-1, State='active', "
                        "ExitURL=?, EntryName=?, Name=?, _ID=? WHERE Id=?",
                        (
                            route["destination"].format(difficulty=difficulty),
                            route["destination_entry"],
                            exit_id, exit_id, exit_id,
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE _Instance_InteractExit "
                        "SET ParentNodeIndex=?, EventSetRow=-1, State='active', "
                        "ExitURL=?, EntryName=?, Name=?, _ID=? WHERE Id=?",
                        (
                            int(source["ParentNodeIndex"] or 0),
                            route["destination"].format(difficulty=difficulty),
                            route["destination_entry"], exit_id, exit_id, exit_id,
                        ),
                    )
                continue
            row = dict(source)
            row.update(
                {
                    "Guid": uuid.uuid5(
                        uuid.NAMESPACE_URL, f"drasa-server:{map_id}:{exit_id}"
                    ).bytes,
                    "_ID": exit_id,
                    "_Level": map_id,
                    "Transform": _transform_at_entry(
                        bytes(source["Transform"]), bytes(entry_row[0]), offset_x=6.0
                    ),
                    "EventSetRow": -1,
                    "Id": exit_id,
                    "ExitURL": route["destination"].format(
                        difficulty=difficulty
                    ),
                    "EntryName": route["destination_entry"],
                    "State": "active",
                    "Name": exit_id,
                }
            )
            if route.get("single_root_gate"):
                # Kingshill is a single, non-tiered destination. Keep one
                # shared scene entity instead of cloning it into every tier.
                row["ParentNodeIndex"] = 0
            quoted_columns = ",".join(f'"{column}"' for column in columns)
            placeholders = ",".join("?" for _ in columns)
            connection.execute(
                f"INSERT INTO _Instance_InteractExit ({quoted_columns}) "
                f"VALUES ({placeholders})",
                tuple(row[column] for column in columns),
            )
            added += 1

        connection.commit()
        return added
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--map-dir",
        action="append",
        type=Path,
        help="Map database directory; may be specified more than once.",
    )
    parser.add_argument(
        "--static-db",
        action="append",
        type=Path,
        help="Static database to give Greyling Forest a display name; repeatable.",
    )
    args = parser.parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    map_dirs = args.map_dir or [repository_root / "data" / "maps"]
    total = 0
    for map_dir in map_dirs:
        for map_id in ROUTES:
            count = patch_map_database(map_dir / f"{map_id}.db4")
            print(f"{map_dir / f'{map_id}.db4'}: added {count} return gate(s)")
            total += count
    static_databases = args.static_db or [
        repository_root / "data" / "db" / "static.db4"
    ]
    for static_db in static_databases:
        if not static_db.is_file():
            raise FileNotFoundError(static_db)
        backup = static_db.with_name(
            static_db.name + ".before-k-map-return-gates"
        )
        if not backup.exists():
            shutil.copy2(static_db, backup)
        connection = sqlite3.connect(static_db)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE _Template_MapTemplate SET MapName=? "
                "WHERE MapTemplate='k0002_greylingforest'",
                ("Greyling Forest",),
            )
            if connection.total_changes == 0:
                raise RuntimeError(
                    f"Greyling Forest map template is missing in {static_db}"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        print(f"{static_db}: set Greyling Forest display name")
    print(f"Total added: {total}; original databases are backed up beside each file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
