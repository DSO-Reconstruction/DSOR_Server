"""Connect Happy Hour travel-item descriptions to their localized text.

The locale XML already contains the Andermant reward descriptions. The client
database left ``LocaleIdDesc`` blank, so the client could not find those keys.
Run this against both the server and client ``static.db4`` files; the project
requires both sides to use the same static database revision.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path


DEFAULT_STATIC_DB = Path(__file__).resolve().parents[1] / "data" / "db" / "static.db4"
DESCRIPTIONS = {
    f"e_hh_travelitem_t{tier}": f"e_hh_travelitem_t{tier}Desc"
    for tier in range(7)
}


def patch_static_database(path: Path) -> int:
    if not path.is_file():
        raise FileNotFoundError(path)

    connection = sqlite3.connect(path)
    try:
        rows = dict(connection.execute(
            "SELECT Id, LocaleIdDesc FROM _Template_Item "
            "WHERE Id LIKE 'e_hh_travelitem_t%'"
        ))
        if set(rows) != set(DESCRIPTIONS):
            raise RuntimeError(
                f"Expected all seven Happy Hour travel items in {path}; "
                f"found {sorted(rows)}"
            )
        updates = [
            (locale_id, item_id)
            for item_id, locale_id in DESCRIPTIONS.items()
            if rows[item_id] != locale_id
        ]
        if not updates:
            return 0

        backup = path.with_name(path.name + ".before-hh-travelitem-descriptions")
        if not backup.exists():
            shutil.copy2(path, backup)

        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            "UPDATE _Template_Item SET LocaleIdDesc=? WHERE Id=?",
            updates,
        )
        connection.commit()
        return len(updates)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--static-db", type=Path, action="append",
        help=(
            "static.db4 to patch; repeat for the server and client copies "
            f"(default: {DEFAULT_STATIC_DB})"
        ),
    )
    args = parser.parse_args()
    for path in args.static_db or [DEFAULT_STATIC_DB]:
        count = patch_static_database(path)
        status = f"set {count} description key(s)" if count else "already correct"
        print(f"{path}: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
