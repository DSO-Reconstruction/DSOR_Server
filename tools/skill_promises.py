#!/usr/bin/env python3
"""Check what a skill's own tooltip promises against what this server serves.

    python3 tools/skill_promises.py ~/dso/db/db_static.sqlite warrior

The localised text is not in the downloaded client, but the *structure* of every
tooltip is: ``_Template_LocaleToken`` says which attribute each piece of a description
reads and which effect it reads it from. So a description can be checked without its
words.

    skill_warshout_buff_movement   UserEffect  warshout  skill_warshout_buff_movementspeed
    skill_defiance_debuff_movspeed LocationEffect defiance skill_defiance_enemies_movement_aura

A token whose TokenType is ``SkillEffect`` and whose Param1Attr is ``UserEffect`` or
``VictimEffect`` names an effect this server can put on an actor. ``LocationEffect``
names an aura placed on the ground, which it cannot.
"""

import pathlib
import sqlite3
import sys


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


def main(path: str, character_class: str) -> None:
    # The repository root from this file, not the working directory. Inserting "."
    # meant the tool only ran from the root, and the failure it gave there was a
    # missing dsor module rather than anything about what it was asked to do.
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    db = connect(path)

    from dsor import effects
    from dsor.skills import of_class
    tokens = db.execute(
        "SELECT Id, Param1Attr, Param1Id, Param2Id, TokenType"
        " FROM _Template_LocaleToken WHERE TokenType = 'SkillEffect'"
    ).fetchall()

    by_skill: dict[str, list[tuple]] = {}
    for token, attribute, skill, effect_id, _kind in tokens:
        by_skill.setdefault(skill, []).append((token, attribute, effect_id))

    known = frozenset(s.id for s in of_class(character_class))
    print(f"{character_class}: what each tooltip names, and whether it is served\n")
    for skill in of_class(character_class):
        promised = by_skill.get(skill.id)
        if not promised:
            continue
        print(f"  {skill.id}  (level {skill.unlock_level})")
        for token, attribute, effect_id in sorted(set(promised)):
            found = effects.by_id(effect_id)
            if attribute == "LocationEffect":
                verdict = "NO — an aura on the ground, not on an actor"
            elif found is None:
                verdict = "NO — not in the generated table"
            elif not (found.starts or found.ticks):
                verdict = "nothing to serve — it carries no modifier"
            elif not found.servable(known):
                verdict = "NO — a modifier reaches into a skill"
            else:
                served = {
                    entry.effect
                    for entry in effects.anything_by(skill.wire)
                    + effects.anything_by(skill.wire, victim=True)
                    if effects.forceable(entry.effect)
                    and effects.by_id(entry.effect).servable(known)
                }
                verdict = "yes" if effect_id in served else "NO — filtered out"
            print(f"      {attribute:15s} {effect_id:44s} {verdict}")
        print()


if __name__ == "__main__":
    main(
        sys.argv[1] if len(sys.argv) > 1 else "db_static.sqlite",
        sys.argv[2] if len(sys.argv) > 2 else "warrior",
    )
