"""A boss skill's ground auras (LocationStatusEffects) strike players in them.

BossSmithEarthqake: five waves, BossSmithEarthqake_aura_0N (2.15 m, D 3.95 s,
DE N-1 s, OZ -2.5 x N), child BossSmithEarthqake_dmg Start and Tick
CurrHealthPointsDmg:$2 (-1.5),relative,Fire every 1 s.
Boss01_b_Skill03: Boss01_b_FloodDamage (20.84 m, D 10 s), child
CurrHealthPoints:-0.1,relative,DarkMagic every 0.25 s.
"""

import sqlite3
from types import SimpleNamespace

import pytest

from dso_server.gameplay.local_combat_state import LocalCombatState, MonsterCombatant
from dso_server.paths import DEFAULT_STATIC_DB_PATH


def _state():
    monster = MonsterCombatant(
        actor_id=100, template_id="boss", level=30, rank_id="Rank_boss",
        position=(0.0, 0.0, 0.0), maximum_health=100000,
        minimum_damage=100.0, maximum_damage=100.0, run_speed=0.0,
        aggro_range=50.0, escape_range=150.0, travel_distance=150.0,
        attack_interval=1.0, collision_radius=1.0,
        attack=None, attacks=(),
    )
    encounter = SimpleNamespace(
        static_database_path=DEFAULT_STATIC_DB_PATH, map_id="aura-test",
        coordinate_frame=SimpleNamespace(wire_to_world=lambda p: p),
        navigation=SimpleNamespace(project_position=lambda p, **kw: p),
        spawn_batch=SimpleNamespace(commands=()), actor_ids=(100,),
    )

    class Combat(LocalCombatState):
        def _combatants(self, encounter):
            return {100: monster}

    return Combat(encounter, player_actor_id=1, player_health=10_000)


def _cast(state, skill_id, now):
    with sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True) as db:
        (index,) = db.execute(
            "SELECT rowid - 1 FROM _Template_Skill WHERE Id=?", (skill_id,)
        ).fetchone()
    visuals = state.player_skill_location_visuals(index)
    state.start_player_location_visuals(
        visuals, position=(0.0, 0.0, 0.0), yaw=0.0, server_tick=1000,
        owner_actor_id=100, source_skill_index=index, now=now,
    )
    return index


def test_earthquake_wave_strikes_on_entry_and_every_second():
    state = _state()
    index = _cast(state, "BossSmithEarthqake", 0.0)
    first = next(
        v for v in state.player_location_visuals.values()
        if v.definition.resource_id == "BossSmithEarthqake_aura_01"
    )
    spot = first.position
    # The first wave rises after its DE (1 s), 2.5 m ahead of the smith.
    assert first.position == pytest.approx((0.0, 0.0, -2.5))
    assert state.resolve_monster_location_auras(spot, player_actor_id=1, now=0.5) == ()
    hits = state.resolve_monster_location_auras(spot, player_actor_id=1, now=1.01)
    assert [h.damage for h in hits] == [150]  # 1.5 x the 100 damage basis
    assert hits[0].skill_index == index and hits[0].attacker_actor_id == 100
    assert state.resolve_monster_location_auras(spot, player_actor_id=1, now=1.5) == ()
    # Its tick, and the second wave rising 2.5 m further on (2.15 m + the
    # player's own radius still reach back to the first).
    assert [h.damage for h in state.resolve_monster_location_auras(
        spot, player_actor_id=1, now=2.01,
    )] == [150, 150]
    # Far from every wave: nothing.
    assert state.resolve_monster_location_auras(
        (50.0, 0.0, 50.0), player_actor_id=2, now=1.5,
    ) == ()


def test_flood_takes_a_tenth_of_health_every_quarter_second_until_khalys_dies():
    state = _state()
    _cast(state, "Boss01_b_Skill03", 0.0)
    assert state.resolve_monster_location_auras(
        (5.0, 0.0, 0.0), player_actor_id=1, now=0.01,
    ) == ()
    hits = state.resolve_monster_location_auras(
        (5.0, 0.0, 0.0), player_actor_id=1, now=0.26,
        incoming_damage_multiplier={"darkmagic": 0.5},
    )
    assert [h.damage for h in hits] == [500]  # 10% of 10000, halved
    state.monster_health[100] = 0
    assert state.resolve_monster_location_auras(
        (5.0, 0.0, 0.0), player_actor_id=1, now=0.51,
    ) == ()


def test_monster_ground_aura_ignores_its_late_entry_backlog():
    state = _state()
    _cast(state, "Boss01_b_Skill03", 0.0)
    hits = state.resolve_monster_location_auras(
        (5.0, 0.0, 0.0), player_actor_id=1, now=5.0,
    )
    assert len(hits) == 1
    assert hits[0].damage == pytest.approx(1000)
