"""Spawns stand on the walkable layer, not on collision geometry beneath it."""

from dso_server.gameplay.local_monster_state import (
    SUNKEN_SPAWN_TOLERANCE, load_local_monster_encounter,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import NewMonsterServerCommand


def test_grimmagstone_difficulty_population_spawns_on_the_floor():
    # Its tall spawn volumes centre near sub-floor geometry; grounding against
    # the volume centre left 76 of the 120 difficulty-10 monsters 4-24 m under
    # the playable level.
    encounter = load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH, "pw001_grimmagstone_01_dun", scaling_difficulty=10,
    )
    positions = [
        tuple(command.value.position)
        for group in encounter.groups
        for command in group.spawn_batch.commands
        if isinstance(command, NewMonsterServerCommand)
    ]
    assert len(positions) == 120
    sunken = [
        position for position in positions
        if position[1] < encounter.navigation.project_position(position)[1]
        - SUNKEN_SPAWN_TOLERANCE
    ]
    assert sunken == []
    assert all(encounter.terrain.heights_at(position) for position in positions)
