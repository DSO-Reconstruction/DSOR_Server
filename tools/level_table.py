"""Regenerate the level curves in ``dsor/combat.py`` from the client's own database.

    python3 tools/level_table.py ~/dso/db/db_static.sqlite

Rewrites LEVEL_EXPERIENCE, LEVEL_HIT_POINTS and LEVEL_DAMAGE in place from
``_Template_XPLevels``, which holds 110 levels for each of five classes. The tables
were truncated at 30 and a character above that fell off the end of them.
"""

import pathlib
import re
import sqlite3
import sys

CLASS = "warrior"



def connect(path: str):
    """Open the client's database read-only, and refuse a path that is not one.

    ``sqlite3.connect`` *creates* a database when the file is missing, so running one
    of these from the wrong directory left an empty ``db_static.sqlite`` behind and
    then generated a table with nothing in it. A generator that quietly produces an
    empty module is the same failure as a config file that quietly ignores a key.
    """
    found = pathlib.Path(path)
    if not found.exists():
        raise SystemExit(
            f"no database at {found}. Point this at the client's own, usually "
            "~/dso/db/db_static.sqlite"
        )
    return sqlite3.connect(f"file:{found}?mode=ro", uri=True)


def rows(db, column):
    return [
        value
        for (value,) in db.execute(
            f"SELECT {column} FROM _Template_XPLevels WHERE CharClass = ?"
            " ORDER BY Level",
            (CLASS,),
        )
    ]


def literal(values):
    lines = []
    for start in range(0, len(values), 10):
        chunk = ", ".join(str(v) for v in values[start : start + 10])
        lines.append(f"    {chunk},")
    return "\n".join(lines)


def main(path: str) -> None:
    db = connect(path)

    # The claim in combat.py that every class shares the experience curve, checked
    # rather than repeated.
    curves = {}
    for (klass,) in db.execute("SELECT DISTINCT CharClass FROM _Template_XPLevels"):
        curves[klass] = tuple(
            v
            for (v,) in db.execute(
                "SELECT LevelXP FROM _Template_XPLevels WHERE CharClass = ?"
                " ORDER BY Level",
                (klass,),
            )
        )
    shared = len(set(curves.values())) == 1
    print(
        f"experience curve shared by all {len(curves)} classes: {shared}",
        file=sys.stderr,
    )
    if not shared:
        for klass, curve in curves.items():
            print(f"  {klass}: {curve[:4]} ... {curve[-2:]}", file=sys.stderr)

    tables = {
        "LEVEL_EXPERIENCE": rows(db, "LevelXP"),
        "LEVEL_HIT_POINTS": rows(db, "BaseHP"),
        "LEVEL_DAMAGE": rows(db, "BaseDamage"),
        "LEVEL_RESOURCE": rows(db, "BaseMana"),
    }

    target = pathlib.Path("dsor/combat.py")
    source = target.read_text()
    for name, values in tables.items():
        pattern = re.compile(rf"^{name} = \(\n(?:.*\n)*?\)$", re.MULTILINE)
        replacement = f"{name} = (\n{literal(values)}\n)"
        source, count = pattern.subn(lambda _m: replacement, source, count=1)
        if count != 1:
            raise SystemExit(f"could not find {name} in {target}")
        print(f"{name}: {len(values)} levels, 1 to {len(values)}", file=sys.stderr)
    target.write_text(source)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "db_static.sqlite")
