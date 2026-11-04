"""The Night Walker's authored spells (services/niwalk_content.py)."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from dso_server.protocol.character_selection import CLASS_IDS, NEW_CHARACTER_QUICKBAR_SKILL_SLOTS
from dso_server.services.niwalk_content import NIWALK_SKILLS, OWN_COLUMNS, authored_row
from dso_server.tools import install_niwalk_content as installer

COLUMNS = ("Id", "Name", "CharClass", "IconBrush", "Groups", "CoolDownCategory", "CoolDown",
           "SkillType", "AttackRange", "ExecuteSequenceMapping", "HitFrame", "UnlockLevel",
           "IsLive", "Hidden", "ResourceCost", "ResourceGain", "DamageModifier",
           "VictimStatusEffects", "UserStatusEffects", "LocationStatusEffects",
           "MinDifficulty", "MaxDifficulty", "SummonCategory", "AmmoCost",
           "ResourceCostToDamage", "InitialCoolDown")


def database(path):
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE _Template_Skill ({', '.join(COLUMNS)})")
    donors = {spec["donor"] for spec in NIWALK_SKILLS.values()}
    rows = []
    for donor in sorted(donors):
        rows.append(dict(Id=donor, CharClass="warrior", SkillType=f"type_{donor}", AttackRange=7.5,
                         ExecuteSequenceMapping=f"{donor}_execute", HitFrame=5, IsLive=1,
                         VictimStatusEffects="item_effect,C:0.0;", IconBrush="icons/donor"))
    for skill, spec in NIWALK_SKILLS.items():
        rows.append(dict(Id=skill, CharClass="niwalk", SkillType=f"type_{spec['donor']}",
                         ExecuteSequenceMapping="work\\sequences\\skill\\skill_dummy_01_green",
                         IconBrush="icons/niwalk", IsLive=0, InitialCoolDown="0.0"))
    for row in rows:
        db.execute(f"INSERT INTO _Template_Skill ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                   tuple(row.values()))
    db.commit()
    db.close()


class NiwalkContentTests(unittest.TestCase):
    def test_a_spell_takes_its_donors_geometry_and_keeps_its_own_identity(self):
        donor = dict(Id="angrystrike", Name="warrior_angrystrike", CharClass="warrior", IconBrush="icons/w",
                     AttackRange=1.75, ExecuteSequenceMapping="warrior_angrystrike_fast_execute",
                     VictimStatusEffects="ammunition_damage_fire,C:0.0;", SkillType="MeleeTarget")
        row = authored_row("zero_slicedice", donor, dict(Id="zero_slicedice", IconBrush="icons/n"))
        self.assertEqual(row["ExecuteSequenceMapping"], "warrior_angrystrike_fast_execute")
        self.assertEqual(row["AttackRange"], 1.75)
        self.assertEqual(row["IsLive"], 1)
        self.assertGreater(row["ResourceGain"], 0.0)
        self.assertEqual(row["VictimStatusEffects"], "")
        self.assertEqual(row["CoolDownCategory"], "niwalk_zero_slicedice")
        for column in ("Id", "Name", "CharClass", "IconBrush"):
            self.assertNotIn(column, row)

    def test_quick_cuts_fill_blood_and_the_others_spend_it(self):
        for skill, spec in NIWALK_SKILLS.items():
            if skill.startswith("zero_"):
                self.assertGreater(spec["gain"], 0.0, skill)
                self.assertEqual(spec["cost"], 0.0, skill)
            else:
                self.assertGreater(spec["cost"], 0.0, skill)
        for skill in ("sacrifice_cut", "sacrifice_vial"):
            self.assertTrue(any(e.startswith("talent_bloodmage_self_damage") for e in NIWALK_SKILLS[skill]["user"]))

    def test_the_new_characters_bar_holds_installed_spells(self):
        bar = NEW_CHARACTER_QUICKBAR_SKILL_SLOTS[CLASS_IDS["niwalk"]]
        self.assertEqual(bar[0], "zero_slicedice")
        self.assertTrue(set(bar) <= set(NIWALK_SKILLS))

    def test_install_updates_rows_in_place_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "static.db4"
            database(path)
            with sqlite3.connect(path) as db:
                before = dict(db.execute("SELECT Id, rowid FROM _Template_Skill"))
            self.assertTrue(any(installer.install(path, apply=False).values()))
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT IsLive FROM _Template_Skill WHERE Id='zero_slicedice'").fetchone()[0], 0)
            installer.install(path, apply=True)
            with sqlite3.connect(path) as db:
                after = dict(db.execute("SELECT Id, rowid FROM _Template_Skill"))
                live = dict(db.execute("SELECT Id, IsLive FROM _Template_Skill WHERE CharClass='niwalk'"))
                icon = db.execute("SELECT IconBrush FROM _Template_Skill WHERE Id='zero_slicedice'").fetchone()[0]
            self.assertEqual(before, after)
            self.assertTrue(all(live[skill] == 1 for skill in NIWALK_SKILLS))
            self.assertEqual(icon, "icons/niwalk")
            self.assertFalse(any(installer.install(path, apply=False).values()))
            self.assertEqual(len(list(Path(folder).glob("static.db4.niwalk-*.bak"))), 1)

    def test_own_columns_are_never_copied(self):
        self.assertTrue({"Id", "CharClass", "IconBrush", "IsLive", "UnlockLevel"} <= OWN_COLUMNS)


if __name__ == "__main__":
    unittest.main()
