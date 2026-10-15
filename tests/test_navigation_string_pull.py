"""Routes are string-pulled (gameplay/pathengine_navigation.py _string_pull):
straight wherever the ground allows, on the walkable mesh, never longer than
the old portal-midpoint lanes."""

from __future__ import annotations

import math
import random
from pathlib import Path

import pytest

from dso_server.gameplay.pathengine_navigation import NavigationError, load_navigation_mesh

MAPS = Path(__file__).resolve().parents[1] / "data" / "navigation"


def _centre(mesh, i):
    v = [mesh.vertices[k] for k in mesh.triangles[i].vertices]
    return tuple(sum(p[j] for p in v) / 3 for j in range(3))


def _length(points):
    return sum(math.hypot(b[0] - a[0], b[2] - a[2]) for a, b in zip(points, points[1:]))


def _on_mesh(mesh, a, b):
    n = max(1, int(math.hypot(b[0] - a[0], b[2] - a[2]) / 0.25))
    for i in range(n + 1):
        p = tuple(a[k] + (b[k] - a[k]) * i / n for k in range(3))
        try:
            mesh.project_position(p, max_horizontal_distance=0.15)
        except NavigationError:
            return False
    return True


@pytest.mark.parametrize("name", ["a0200_kingscity", "a0007_pastures_pve"])
def test_routes_are_pulled_tight_and_stay_on_the_ground(name):
    path = MAPS / name / f"{name}.tok"
    if not path.exists():
        pytest.skip("client navigation content not installed")
    mesh = load_navigation_mesh(path)
    rng = random.Random(7)
    checked = 0
    for _ in range(3000):
        a = _centre(mesh, rng.randrange(len(mesh.triangles)))
        b = _centre(mesh, rng.randrange(len(mesh.triangles)))
        if math.hypot(a[0] - b[0], a[2] - b[2]) > 60:
            continue
        try:
            route = mesh.find_route(a, b)
        except NavigationError:
            continue
        lanes = [a] + [
            tuple((mesh.vertices[x][k] + mesh.vertices[y][k]) / 2 for k in range(3))
            for x, y in (mesh._portals[(l, r)] for l, r in zip(route.triangle_ids, route.triangle_ids[1:]))
        ] + [b]
        assert all(_on_mesh(mesh, p, q) for p, q in zip(route.points, route.points[1:]))
        assert _length(route.points) <= _length(lanes) + 1e-6
        checked += 1
        if checked >= 150:
            break
    assert checked >= 50
