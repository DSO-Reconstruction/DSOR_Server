"""Statue traps: the Winter Summit's ice-missile lanes and turrets."""

import math

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.gameplay.map_hazards import fires_fixed_direction, is_map_hazard
from dso_server.protocol.gameplay_protocol import NewMonsterServerCommand

WINTER_SUMMIT = "e_b1205_dun_chr_2012_02"


@pytest.fixture(scope="module")
def winter_summit():
    return load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH, WINTER_SUMMIT, monster_level=100,
    )


def statues(encounter):
    return [
        command.value for command in encounter.spawn_batch.commands
        if isinstance(command, NewMonsterServerCommand)
        and is_map_hazard(DEFAULT_STATIC_DB_PATH, command.value.template_id)
    ]


def test_only_the_ancestral_guard_traps_are_hazards():
    assert fires_fixed_direction(DEFAULT_STATIC_DB_PATH, "e_chr_2013_guardian_01_hard")
    assert is_map_hazard(DEFAULT_STATIC_DB_PATH, "e_chr_2013_guardian_05_hard")
    assert not fires_fixed_direction(DEFAULT_STATIC_DB_PATH, "e_chr_2013_guardian_05_hard")
    assert not is_map_hazard(DEFAULT_STATIC_DB_PATH, "e_chr_b1205_normal_troll_01")


def test_every_statue_column_gets_one_statue_with_its_authored_facing(winter_summit):
    placed = statues(winter_summit)
    # 11 + 23 lane columns (four MapVariation variants each share them) and
    # the two turrets standing outside any variation; no density copies.
    assert len(placed) == 36
    positions = {(round(m.position[0], 1), round(m.position[2], 1)) for m in placed}
    assert len(positions) == 36
    by_position = {(round(m.position[0], 1), round(m.position[2], 1)): m for m in placed}
    lane = by_position[(14.0, 24.1)]
    assert lane.template_id.startswith("e_chr_2013_guardian_")
    assert abs(abs(math.degrees(lane.field_a0)) - 180) < 1
    assert lane.position[1] == pytest.approx(6.44, abs=0.01)


def test_a_lane_statue_shoots_down_its_corridor_and_strikes_who_it_passes(winter_summit):
    state = LocalCombatState(winter_summit, player_actor_id=1, player_health=10**9,
                             initially_active=False)
    actor_id = next(
        actor for actor, monster in state.monsters.items()
        if monster.fixed_direction
        and abs(monster.position[0] - 14.0) < 0.2 and abs(monster.position[2] - 24.08) < 0.2
    )
    assert state.monsters[actor_id].invulnerable
    state.activate_monsters((actor_id,))
    state.last_monster_attack_at[actor_id] = 0.0
    state._monster_skill_ready_at[actor_id] = {}
    frame = winter_summit.coordinate_frame
    in_lane = frame.world_to_wire((14.0, 6.0, 30.0))
    beside = frame.world_to_wire((17.0, 6.0, 30.0))
    attack, = state.maybe_start_monster_attacks(
        in_lane, server_tick=500, now=100.0, eligible_actor_ids={actor_id})
    # The wire names the player who woke it; the lane still decides the hits.
    assert attack.target_actor_id == 1
    assert attack.direction[2] == pytest.approx(1.0)  # yaw 180 faces +Z
    assert attack.lane_reach == pytest.approx(15.0)
    hits = {1: [], 2: []}
    for step in range(60):
        now = attack.lane_release_at + step * 0.04
        hits[1] += state.resolve_due_monster_attacks(in_lane, now=now, player_actor_id=1)
        hits[2] += state.resolve_due_monster_attacks(
            beside, now=now, player_actor_id=2, finalize_area_attacks=True)
    # One strike when the missile passes 6 m down the lane, none beside it.
    assert len(hits[1]) == 1 and hits[1][0].damage > 0
    assert hits[2] == []
    assert not state.pending_monster_attack_items()


def test_a_lane_statue_fires_again_while_its_missile_flies(winter_summit):
    state = LocalCombatState(winter_summit, player_actor_id=1, player_health=10**9,
                             initially_active=False)
    actor_id = next(actor for actor, monster in state.monsters.items() if monster.fixed_direction)
    state.activate_monsters((actor_id,))
    state.last_monster_attack_at[actor_id] = 0.0
    state._monster_skill_ready_at[actor_id] = {}
    near = winter_summit.coordinate_frame.world_to_wire(state.monster_positions[actor_id])
    first = state.maybe_start_monster_attacks(near, server_tick=500, now=100.0,
                                              eligible_actor_ids={actor_id})
    second = state.maybe_start_monster_attacks(near, server_tick=532, now=101.3,
                                               eligible_actor_ids={actor_id})
    assert len(first) == len(second) == 1
    assert len(state.pending_monster_attack_items()) == 2


def test_statues_an_event_set_switches_leave_with_their_set():
    # The Big Paws Mom room's turrets stand behind chr2025_01_boss_death=False;
    # map_events retires a disabled set's monsters by template and authored
    # position, which a statue keeps, so they go when she dies.
    from dso_server.gameplay.map_event_state import load_map_event_content

    encounter = load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH, "e_b1205_dun_chr_2025_01", monster_level=100)
    state = LocalCombatState(encounter, player_actor_id=1, player_health=1000)
    turrets = {actor for actor, m in state.monsters.items()
               if m.template_id == "e_chr_2013_guardian_05_hard"}
    assert len(turrets) == 3 and all(state.monsters[a].invulnerable for a in turrets)
    content = load_map_event_content(DEFAULT_STATIC_DB_PATH, "e_b1205_dun_chr_2025_01")
    found = set()
    for row in content.spawn_rows_for_sets({624}):
        found.update(state.find_monsters(row.template_id, row.position, radius=1.5))
    assert found == turrets


def test_a_defeated_boss_silences_only_its_arena_turrets(winter_summit):
    state = LocalCombatState(winter_summit, player_actor_id=1, player_health=1000)
    big_paws = (-45.0, 19.4, -156.8)
    silenced = state.silence_hazards_near(big_paws, 25.0)
    assert sorted(state.monsters[a].template_id for a in silenced) == [
        "e_chr_2013_guardian_05_hard"] * 2
    state.activate_monsters(silenced)
    assert not set(silenced) & state.active_monster_actor_ids
    # Corridor statues keep firing.
    assert any(m.fixed_direction and a in state.active_monster_actor_ids
               for a, m in state.monsters.items())


def test_the_arena_exits_open_only_once_big_paws_is_defeated():
    from dso_server.gameplay.local_map_state import load_local_map_exits

    flags = frozenset({"christmas_2012"})
    arena = {"b1205_exit_to_a0200_02", "b1205_exit_to_chr_2025_01"}  # Kingshill, Bear's Den

    def active(event_flags):
        return {exit.exit_id for exit in load_local_map_exits(
            DEFAULT_STATIC_DB_PATH, WINTER_SUMMIT, active_event_flags=event_flags,
        ) if exit.active}

    assert not arena & active(flags)
    assert arena <= active(flags | {"boss_death"})


def test_entry_tells_the_client_the_boss_events_are_not_running():
    # BOSS_death has no template Map (it is generic). Without an explicit
    # "not running" entry the client left the sets it gates unresolved and
    # showed the arena exits before Big Paws was dead.
    from dso_server.services.events import _declared_map_local_event_entries

    entries = {
        entry.event_name: entry.flags
        for entry in _declared_map_local_event_entries(DEFAULT_STATIC_DB_PATH, WINTER_SUMMIT)
    }
    assert entries["BOSS_death"] == (False, True, False, False)
    assert {"BOSS_spawn", "BOSS_start"} <= set(entries)
    # Generic events no object of this map depends on stay out.
    assert "difficulty_bloodshed_chest_01" not in entries
