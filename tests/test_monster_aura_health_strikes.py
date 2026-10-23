"""A monster aura strike can take a share of health or a fixed amount.

e_ks_boss_eye_360: e_ks_boss_eye_360_aura (26 m, D 1.6), child start
CurrHealthPoints:-0.8,relative: 80% of maximum health, once.
Boss02HeraldSkill04: Boss02Skill04Aura (10.5 m, D 0), child start
CurrHealthPoints:-1000.0,absolute,Physical.
boss_khalys_base_beam's aura ticks for 4 s: not a one-shot strike.
"""

import sqlite3

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from tests.test_monster_location_auras import _state


def _strikes(state, skill_id):
    with sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True) as db:
        (value,) = db.execute(
            "SELECT UserStatusEffects FROM _Template_Skill WHERE Id=?", (skill_id,)
        ).fetchone()
        return state._load_monster_aura_strikes(db, value)


def test_eye_360_takes_eighty_percent_of_health():
    state = _state()
    strike = _strikes(state, "e_ks_boss_eye_360")["e_ks_boss_eye_360_aura"]
    assert strike.radius == pytest.approx(26.0)
    assert strike.damage_programs == (("health", pytest.approx(0.8), "Physical"),)
    assert state._monster_aura_program_damage(
        100, strike.damage_programs, 10_000, 1.0, 0.0,
    ) == 8000


def test_herald_meteor_aura_deals_a_fixed_thousand_mitigated():
    state = _state()
    strike = _strikes(state, "Boss02HeraldSkill04")["Boss02Skill04Aura"]
    assert strike.damage_programs == (("absolute", pytest.approx(1000.0), "Physical"),)
    assert state._monster_aura_program_damage(
        100, strike.damage_programs, 10_000, {"physical": 0.25}, 0.0,
    ) == 250


def test_ticking_beam_aura_is_not_a_one_shot_strike():
    assert _strikes(_state(), "boss_khalys_base_beam") == {}
