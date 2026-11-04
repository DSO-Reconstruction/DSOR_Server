"""Write the Night Walker's spells (services/niwalk_content.py) into static.db4.

Usage: install_niwalk_content.py [--database <static.db4>] [--apply]

Without --apply it only prints what would change. With it, the database is
backed up beside itself (static.db4.niwalk-<time>.bak) and the niwalk rows
of _Template_Skill are updated in place -- never REPLACEd or inserted: a
skill's wire index is its rowid - 1, shared by the server and the client.
Run it once on the static.db4 both read (data/db/static.db4 is the client's,
symlinked), then export the client's skills again (tools/export_skills.py in
the client repository).
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
from pathlib import Path
import shutil
import sqlite3

if __package__ in (None, ""):
    import runpy
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "run.py"))["load_package"]()

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.services.niwalk_content import NIWALK_SKILLS, authored_row

TABLE = "_Template_Skill"


def row_of(db, skill_id):
    db.row_factory = sqlite3.Row
    row = db.execute(f"SELECT * FROM {TABLE} WHERE Id=? LIMIT 1", (skill_id,)).fetchone()
    return dict(row) if row is not None else None


def planned(db) -> dict[str, dict]:
    """skill id -> the columns to change (empty when already installed)."""

    changes = {}
    for skill_id, spec in NIWALK_SKILLS.items():
        current = row_of(db, skill_id)
        if current is None or str(current.get("CharClass", "")).casefold() != "niwalk":
            raise ValueError(f"{skill_id} is not a niwalk row of {TABLE}")
        donor = row_of(db, spec["donor"])
        if donor is None:
            raise ValueError(f"{skill_id}'s donor {spec['donor']} is missing")
        if str(donor["SkillType"]) != str(current["SkillType"]):
            raise ValueError(f"{skill_id} is {current['SkillType']}, its donor {spec['donor']} {donor['SkillType']}")
        changes[skill_id] = authored_row(skill_id, donor, current)
    return changes


def install(path: Path, *, apply: bool) -> dict[str, dict]:
    with closing(sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)) as db:
        changes = planned(db)
    pending = {skill: columns for skill, columns in changes.items() if columns}
    for skill, columns in pending.items():
        print(f"{skill}: {', '.join(sorted(columns))}")
    if not pending:
        print("niwalk content already installed")
        return changes
    if not apply:
        print("preview only: run again with --apply to write")
        return changes
    target = path.resolve()
    backup = target.with_name(f"{target.name}.niwalk-{datetime.now():%Y%m%d-%H%M%S}.bak")
    shutil.copy2(target, backup)
    with closing(sqlite3.connect(str(target))) as db:
        rowids = {skill: db.execute(f"SELECT rowid FROM {TABLE} WHERE Id=?", (skill,)).fetchone()[0]
                  for skill in pending}
        with db:
            for skill, columns in pending.items():
                assignments = ", ".join(f'"{column}"=?' for column in columns)
                db.execute(f'UPDATE {TABLE} SET {assignments} WHERE rowid=?',
                           (*columns.values(), rowids[skill]))
        moved = [skill for skill, rowid in rowids.items()
                 if db.execute(f"SELECT rowid FROM {TABLE} WHERE Id=?", (skill,)).fetchone()[0] != rowid]
        if moved:
            raise RuntimeError(f"rowids moved for {moved}; restore {backup}")
    print(f"installed {len(pending)} niwalk skills; backup {backup}")
    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_STATIC_DB_PATH)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    install(args.database, apply=args.apply)


if __name__ == "__main__":
    main()
