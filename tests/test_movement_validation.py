"""Server authority over player positions (services/movement_validation.py).

Uses Kingshill's real PathEngine mesh (data/navigation, client content)."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from dso_server.gameplay.local_map_state import MapCoordinateFrame
from dso_server.gameplay.pathengine_navigation import NavigationError, load_navigation_mesh
from dso_server.protocol.gameplay_protocol import Move
from dso_server.services import movement_validation as mv

MESH = Path(__file__).resolve().parents[1] / "data" / "navigation" / "a0200_kingscity" / "a0200_kingscity.tok"
ORIGIN = (10.0, 0.0, -20.0)

pytestmark = pytest.mark.skipif(not MESH.exists(), reason="client navigation content not installed")


@pytest.fixture(scope="module")
def mesh():
    return load_navigation_mesh(MESH)


def session(mesh, spawn=None):
    frame = MapCoordinateFrame(ORIGIN)
    combat = SimpleNamespace(encounter=SimpleNamespace(coordinate_frame=frame, navigation=mesh))
    return SimpleNamespace(
        gameplay_last_move=None, gameplay_combat_state=combat, gameplay_spawn_position=spawn,
        gameplay_move_budget=0.0, gameplay_movement_allowance=None, gameplay_move_correction_at=0.0,
    )


def move(frame, world, tick):
    return Move.from_values(frame.world_to_wire(world), 0.0, 0.0, 0.0, tick, 20)


def centre(mesh, triangle_id):
    v = [mesh.vertices[i] for i in mesh.triangles[triangle_id].vertices]
    return tuple(sum(p[k] for p in v) / 3 for k in range(3))


def walk_pair(mesh, rng, length):
    """Two points `length` apart with walkable straight ground between."""
    for _ in range(20000):
        a = centre(mesh, rng.randrange(len(mesh.triangles)))
        angle = rng.random() * math.tau
        b = (a[0] + math.cos(angle) * length, a[1], a[2] + math.sin(angle) * length)
        try:
            b = mesh.project_position(b, max_horizontal_distance=0.05)
        except NavigationError:
            continue
        if mv.walkable_path(mesh, a, b):
            return a, b
    pytest.skip("no straight walkable segment found")


def accept(s, frame, world, tick):
    m = move(frame, world, tick)
    reason = mv.check_move(s, m)
    if reason is None:
        s.gameplay_last_move = m
    return reason


def test_walking_at_running_speed_is_accepted(mesh):
    rng = random.Random(1)
    a, b = walk_pair(mesh, rng, 12.0)
    s = session(mesh, spawn=a)
    frame = s.gameplay_combat_state.encounter.coordinate_frame
    steps = int(12.0 / (5.0 / 25.0))
    for i in range(steps + 1):
        f = i / steps
        p = tuple(a[k] + (b[k] - a[k]) * f for k in range(3))
        assert accept(s, frame, p, 1000 + i) is None


def test_teleporting_by_move_is_refused(mesh):
    rng = random.Random(2)
    a, b = walk_pair(mesh, rng, 40.0)
    s = session(mesh, spawn=a)
    frame = s.gameplay_combat_state.encounter.coordinate_frame
    assert accept(s, frame, a, 1000) is None
    assert accept(s, frame, b, 1001) is not None
    # The server position stays where it was.
    assert math.dist(frame.wire_to_world(s.gameplay_last_move.relative_position), a) < 0.05


def test_off_mesh_position_is_refused(mesh):
    rng = random.Random(3)
    s = None
    for _ in range(5000):
        a = centre(mesh, rng.randrange(len(mesh.triangles)))
        for d in (3.0, 4.0, 5.0):
            for angle in (0, 1.5, 3.1, 4.7):
                b = (a[0] + math.cos(angle) * d, a[1], a[2] + math.sin(angle) * d)
                try:
                    mesh.project_position(b, max_horizontal_distance=mv.NAVMESH_SNAP)
                except NavigationError:
                    s = session(mesh, spawn=a)
                    frame = s.gameplay_combat_state.encounter.coordinate_frame
                    assert accept(s, frame, a, 1000) is None
                    assert accept(s, frame, b, 1000 + 25) is not None
                    return
    pytest.skip("no off-mesh point found near the mesh")


def test_stepping_through_a_wall_is_refused(mesh):
    """Same component, close enough to run to, but not in a straight line."""
    rng = random.Random(4)
    for _ in range(20000):
        a = centre(mesh, rng.randrange(len(mesh.triangles)))
        angle = rng.random() * math.tau
        b = (a[0] + math.cos(angle) * 4.0, a[1], a[2] + math.sin(angle) * 4.0)
        try:
            b = mesh.project_position(b, max_horizontal_distance=0.05)
        except NavigationError:
            continue
        if mesh.is_reachable(a, b) and not mv.walkable_path(mesh, a, b):
            s = session(mesh, spawn=a)
            frame = s.gameplay_combat_state.encounter.coordinate_frame
            assert accept(s, frame, a, 1000) is None
            assert accept(s, frame, b, 1000 + 25) is not None
            return
    pytest.skip("no wall between two close points found")


@dataclass(frozen=True)
class Geometry:
    attack_range: float
    skill_type: str = "Teleport"


def test_teleport_destination_is_clamped_to_range_and_ground(mesh):
    rng = random.Random(5)
    a, b = walk_pair(mesh, rng, 30.0)
    s = session(mesh, spawn=a)
    frame = s.gameplay_combat_state.encounter.coordinate_frame
    origin_wire = frame.world_to_wire(a)
    landed = mv.clamp_movement_destination(s, Geometry(10.0), origin_wire, b)
    assert mv._flat_distance(a, landed) <= 10.0 + mv.RANGE_TOLERANCE + 1e-6
    assert mesh.is_reachable(a, landed)
    # Its moves may follow the jump, once, the cast having been accepted
    # (services/player_combat.py records the allowance only then).
    mv.record_movement_allowance(s, origin_wire, landed)
    s.gameplay_last_move = move(frame, a, 1000)
    assert accept(s, frame, landed, 1001) is None
    # A destination in the void stays on the ground the caster can reach.
    void = (a[0] + 9.0, a[1] - 500.0, a[2])
    landed = mv.clamp_movement_destination(s, Geometry(10.0), origin_wire, (a[0] + 1e5, a[1], a[2]))
    assert mesh.is_reachable(a, landed)
    del void


def test_non_finite_values_are_detected():
    @dataclass(frozen=True)
    class Cast:
        aim_angle: float
        target_position: tuple[float, float, float]

    assert mv.non_finite(Cast(float("nan"), (0.0, 0.0, 0.0)))
    assert mv.non_finite(Cast(0.0, (0.0, float("inf"), 0.0)))
    assert not mv.non_finite(Cast(1.0, (2.0, 3.0, 4.0)))


SEWERS = MESH.parents[1] / "c2303_dun" / "c2303_dun.tok"


@pytest.mark.skipif(not SEWERS.exists(), reason="client navigation content not installed")
def test_a_step_across_a_short_authored_edge_is_walkable():
    """c2303_dun: the log's corrected position (wire -46.8, -1.59, -92.69)
    and a point 1.5 m away lie in different 1 m-portal components, split by
    a 0.94 m shared edge, yet on one connected ground the client walks."""
    sewers = load_navigation_mesh(SEWERS)
    a = (-101.029, -7.02, -119.1875)
    b = (-101.2, -7.0, -120.8)
    assert not sewers.is_reachable(a, b)
    assert sewers.is_reachable(a, b, all_connections=True)
    assert mv.walkable_path(sewers, a, b)
