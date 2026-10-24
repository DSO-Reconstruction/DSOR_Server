"""Read PathEngine ground meshes and route local server actors over them.

DSO ships one tokenised-XML ``.tok`` mesh and PathEngine preprocess files for
each map.  The ground mesh already contains the connected walkable triangles;
the ``small`` preprocess describes a roughly 50 cm radius agent.  This module
uses the documented mesh3D vertex/triangle connection data and rejects shared
portals narrower than that agent's one-metre diameter.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
import heapq
import math
from pathlib import Path
import struct


PATHENGINE_UNITS_PER_WORLD_UNIT = 100.0
SMALL_AGENT_DIAMETER = 1.0
NAVIGATION_SPATIAL_CELL_SIZE = 8.0


def _spatial_ring_cells(
    center: tuple[int, int],
    radius: int,
) -> Iterator[tuple[int, int]]:
    """Yield the spatial cells exactly *radius* cells from *center*.

    The outward search visits one ring at a time.  Walking the perimeter is
    ``O(radius)``; enumerating the full ``(2 * radius + 1) ** 2`` square and
    discarding its interior made the accumulated search ``O(radius ** 3)``,
    which dominated monster-encounter generation on dense maps.  The visit
    order matches that square scan (ascending X, then ascending Z) so ties
    between equidistant triangles still resolve the same way.
    """

    center_x, center_z = center
    if radius <= 0:
        yield (center_x, center_z)
        return
    for cell_z in range(center_z - radius, center_z + radius + 1):
        yield (center_x - radius, cell_z)
    for cell_x in range(center_x - radius + 1, center_x + radius):
        yield (cell_x, center_z - radius)
        yield (cell_x, center_z + radius)
    for cell_z in range(center_z - radius, center_z + radius + 1):
        yield (center_x + radius, cell_z)


class NavigationError(ValueError):
    pass


class _MeshComplete(Exception):
    pass


class _TokenReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0

    def byte(self) -> int:
        if self.position >= len(self.data):
            raise NavigationError("unexpected end of tokenised XML")
        value = self.data[self.position]
        self.position += 1
        return value

    def cstring(self) -> str:
        try:
            end = self.data.index(0, self.position)
        except ValueError as exc:
            raise NavigationError("unterminated tokenised XML string") from exc
        try:
            value = self.data[self.position:end].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NavigationError("invalid tokenised XML string") from exc
        self.position = end + 1
        return value

    def attribute_value(self, kind: int):
        if kind == 1:
            return self.cstring()
        formats = {
            2: "<i",
            3: "<h",
            4: "<b",
            5: "<I",
            6: "<H",
            7: "<B",
        }
        try:
            value_format = formats[kind]
        except KeyError as exc:
            raise NavigationError(
                f"unsupported tokenised XML attribute type {kind}"
            ) from exc
        size = struct.calcsize(value_format)
        if self.position + size > len(self.data):
            raise NavigationError("truncated tokenised XML attribute")
        value = struct.unpack_from(value_format, self.data, self.position)[0]
        self.position += size
        return value


@dataclass(frozen=True)
class NavigationTriangle:
    vertices: tuple[int, int, int]
    connections: tuple[int | None, int | None, int | None]


@dataclass(frozen=True)
class NavigationRoute:
    points: tuple[tuple[float, float, float], ...]
    triangle_ids: tuple[int, ...]


class PathEngineNavigationMesh:
    def __init__(
        self,
        vertices: tuple[tuple[float, float, float], ...],
        triangles: tuple[NavigationTriangle, ...],
        *,
        minimum_portal_width: float = SMALL_AGENT_DIAMETER,
    ) -> None:
        if not vertices or not triangles:
            raise NavigationError("navigation mesh has no geometry")
        self.vertices = vertices
        self.triangles = triangles
        self.minimum_portal_width = float(minimum_portal_width)
        self._centroids = tuple(self._centroid(index) for index in range(len(triangles)))
        self._bounds = tuple(self._triangle_bounds(index) for index in range(len(triangles)))
        spatial_cells: dict[tuple[int, int], list[int]] = {}
        for triangle_id, bounds in enumerate(self._bounds):
            minimum_x = math.floor(bounds[0] / NAVIGATION_SPATIAL_CELL_SIZE)
            minimum_z = math.floor(bounds[1] / NAVIGATION_SPATIAL_CELL_SIZE)
            maximum_x = math.floor(bounds[2] / NAVIGATION_SPATIAL_CELL_SIZE)
            maximum_z = math.floor(bounds[3] / NAVIGATION_SPATIAL_CELL_SIZE)
            for cell_x in range(minimum_x, maximum_x + 1):
                for cell_z in range(minimum_z, maximum_z + 1):
                    spatial_cells.setdefault((cell_x, cell_z), []).append(
                        triangle_id
                    )
        self._spatial_cells = {
            cell: tuple(values) for cell, values in spatial_cells.items()
        }
        cell_ids = tuple(self._spatial_cells)
        self._spatial_cell_bounds = (
            min(cell[0] for cell in cell_ids),
            min(cell[1] for cell in cell_ids),
            max(cell[0] for cell in cell_ids),
            max(cell[1] for cell in cell_ids),
        )
        adjacency: list[list[int]] = [[] for _ in triangles]
        portals: dict[tuple[int, int], tuple[int, int]] = {}
        for triangle_id, triangle in enumerate(triangles):
            for edge_index, other in enumerate(triangle.connections):
                if other is None:
                    continue
                if not 0 <= other < len(triangles):
                    raise NavigationError(
                        f"triangle {triangle_id} connects outside the mesh: {other}"
                    )
                start = triangle.vertices[edge_index]
                end = triangle.vertices[(edge_index + 1) % 3]
                left = self.vertices[start]
                right = self.vertices[end]
                width = math.hypot(left[0] - right[0], left[2] - right[2])
                if width + 1e-6 < self.minimum_portal_width:
                    continue
                if other not in adjacency[triangle_id]:
                    adjacency[triangle_id].append(other)
                if triangle_id not in adjacency[other]:
                    adjacency[other].append(triangle_id)
                portals[(triangle_id, other)] = (start, end)
                portals[(other, triangle_id)] = (end, start)
        self._adjacency = tuple(tuple(values) for values in adjacency)
        self._portals = portals
        component_by_triangle = [-1] * len(triangles)
        components = []
        for seed in range(len(triangles)):
            if component_by_triangle[seed] >= 0:
                continue
            component_id = len(components)
            pending = [seed]
            members = []
            component_by_triangle[seed] = component_id
            while pending:
                triangle_id = pending.pop()
                members.append(triangle_id)
                for neighbour in self._adjacency[triangle_id]:
                    if component_by_triangle[neighbour] >= 0:
                        continue
                    component_by_triangle[neighbour] = component_id
                    pending.append(neighbour)
            components.append(tuple(members))
        self._component_by_triangle = tuple(component_by_triangle)
        self._components = tuple(components)
        # The ground as the player walks it: every authored connection,
        # whatever its edge length. The client groups its walkable ground the
        # same way (DSOR_Client client/src/world/nav.rs components: triangles
        # sharing an edge) and lets the player walk across all of it. An edge
        # length is not a corridor width: a finely triangulated room has short
        # shared edges everywhere, and the 1 m cutoff above splits c2303_dun's
        # 10217 triangles into 2342 components (one connected piece here).
        # Union-find: a connection may be recorded on one side only.
        parent = list(range(len(triangles)))

        def root(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        for triangle_id, triangle in enumerate(triangles):
            for other in triangle.connections:
                if other is not None:
                    parent[root(triangle_id)] = root(other)
        ground_by_triangle = [root(index) for index in range(len(triangles))]
        self._ground_component_by_triangle = tuple(ground_by_triangle)

    @classmethod
    def from_tokenised_mesh(
        cls,
        path: str | Path,
        *,
        minimum_portal_width: float = SMALL_AGENT_DIAMETER,
    ) -> "PathEngineNavigationMesh":
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        vertices, triangles = _read_mesh3d(path.read_bytes())
        return cls(
            vertices, triangles,
            minimum_portal_width=minimum_portal_width,
        )

    def _centroid(self, triangle_id: int) -> tuple[float, float, float]:
        points = [self.vertices[index] for index in self.triangles[triangle_id].vertices]
        return tuple(sum(point[axis] for point in points) / 3.0 for axis in range(3))

    def _triangle_bounds(self, triangle_id: int) -> tuple[float, float, float, float]:
        points = [self.vertices[index] for index in self.triangles[triangle_id].vertices]
        return (
            min(point[0] for point in points),
            min(point[2] for point in points),
            max(point[0] for point in points),
            max(point[2] for point in points),
        )

    @staticmethod
    def _horizontal_distance(
        left: tuple[float, float, float], right: tuple[float, float, float]
    ) -> float:
        return math.hypot(left[0] - right[0], left[2] - right[2])

    def _barycentric(
        self, triangle_id: int, point: tuple[float, float, float]
    ) -> tuple[float, float, float] | None:
        a, b, c = (
            self.vertices[index] for index in self.triangles[triangle_id].vertices
        )
        v0 = (b[0] - a[0], b[2] - a[2])
        v1 = (c[0] - a[0], c[2] - a[2])
        v2 = (point[0] - a[0], point[2] - a[2])
        denominator = v0[0] * v1[1] - v1[0] * v0[1]
        if abs(denominator) <= 1e-9:
            return None
        first = (v2[0] * v1[1] - v1[0] * v2[1]) / denominator
        second = (v0[0] * v2[1] - v2[0] * v0[1]) / denominator
        third = 1.0 - first - second
        epsilon = 1e-5
        if first < -epsilon or second < -epsilon or third < -epsilon:
            return None
        return (third, first, second)

    def _height_from_weights(
        self,
        triangle_id: int,
        weights: tuple[float, float, float],
    ) -> float:
        points = [
            self.vertices[index] for index in self.triangles[triangle_id].vertices
        ]
        return sum(
            weight * vertex[1]
            for weight, vertex in zip(weights, points, strict=True)
        )

    def _closest_point_on_triangle(
        self,
        triangle_id: int,
        point: tuple[float, float, float],
    ) -> tuple[tuple[float, float, float], float]:
        """Return the closest walkable X/Z point and its horizontal distance."""

        weights = self._barycentric(triangle_id, point)
        if weights is not None:
            return (
                (
                    float(point[0]),
                    self._height_from_weights(triangle_id, weights),
                    float(point[2]),
                ),
                0.0,
            )

        vertices = [
            self.vertices[index] for index in self.triangles[triangle_id].vertices
        ]
        candidates = []
        for start, end in zip(vertices, vertices[1:] + vertices[:1], strict=True):
            edge_x = end[0] - start[0]
            edge_z = end[2] - start[2]
            edge_length_squared = edge_x * edge_x + edge_z * edge_z
            if edge_length_squared <= 1e-12:
                ratio = 0.0
            else:
                ratio = max(
                    0.0,
                    min(
                        1.0,
                        (
                            (point[0] - start[0]) * edge_x
                            + (point[2] - start[2]) * edge_z
                        )
                        / edge_length_squared,
                    ),
                )
            candidate = tuple(
                start[axis] + (end[axis] - start[axis]) * ratio
                for axis in range(3)
            )
            candidates.append(
                (self._horizontal_distance(point, candidate), candidate)
            )
        distance, candidate = min(candidates, key=lambda value: value[0])
        return candidate, distance

    def _project_position_and_triangle(
        self,
        point: tuple[float, float, float],
        *,
        max_horizontal_distance: float | None = None,
    ) -> tuple[tuple[float, float, float], int]:
        if max_horizontal_distance is not None:
            max_horizontal_distance = float(max_horizontal_distance)
            if max_horizontal_distance < 0.0:
                raise ValueError("max_horizontal_distance cannot be negative")

        cell = (
            math.floor(point[0] / NAVIGATION_SPATIAL_CELL_SIZE),
            math.floor(point[2] / NAVIGATION_SPATIAL_CELL_SIZE),
        )
        local_triangle_ids = self._spatial_cells.get(cell, ())
        containing = []
        for triangle_id in local_triangle_ids:
            bounds = self._bounds[triangle_id]
            if not (
                bounds[0] - 1e-5 <= point[0] <= bounds[2] + 1e-5
                and bounds[1] - 1e-5 <= point[2] <= bounds[3] + 1e-5
            ):
                continue
            weights = self._barycentric(triangle_id, point)
            if weights is None:
                continue
            height = self._height_from_weights(triangle_id, weights)
            containing.append(
                (
                    abs(height - point[1]),
                    triangle_id,
                    (float(point[0]), height, float(point[2])),
                )
            )
        if containing:
            _vertical_distance, triangle_id, projected = min(containing)
            return projected, triangle_id

        # Points generated near an edge (monster separation and item scatter)
        # can lie just outside the walkable polygon. Search spatial rings and
        # clamp to the geometrically closest triangle edge. Once that edge is
        # closer than the unsearched cell boundary, no outside cell can win.
        seen: set[int] = set()
        best: tuple[
            float,
            float,
            int,
            tuple[float, float, float],
        ] | None = None
        minimum_cell_x, minimum_cell_z, maximum_cell_x, maximum_cell_z = (
            self._spatial_cell_bounds
        )
        maximum_radius = max(
            abs(cell[0] - minimum_cell_x),
            abs(cell[0] - maximum_cell_x),
            abs(cell[1] - minimum_cell_z),
            abs(cell[1] - maximum_cell_z),
        )
        # Do not walk thousands of empty rings when an authored point lies
        # well outside the mesh's global cell rectangle.
        minimum_radius = max(
            (
                minimum_cell_x - cell[0]
                if cell[0] < minimum_cell_x
                else cell[0] - maximum_cell_x
                if cell[0] > maximum_cell_x
                else 0
            ),
            (
                minimum_cell_z - cell[1]
                if cell[1] < minimum_cell_z
                else cell[1] - maximum_cell_z
                if cell[1] > maximum_cell_z
                else 0
            ),
        )
        for radius in range(minimum_radius, maximum_radius + 1):
            for cell_key in _spatial_ring_cells(cell, radius):
                for triangle_id in self._spatial_cells.get(cell_key, ()):
                    if triangle_id in seen:
                        continue
                    seen.add(triangle_id)
                    projected, distance = self._closest_point_on_triangle(
                        triangle_id, point
                    )
                    candidate = (
                        distance,
                        abs(projected[1] - point[1]),
                        triangle_id,
                        projected,
                    )
                    if best is None or candidate[:3] < best[:3]:
                        best = candidate

            left = (cell[0] - radius) * NAVIGATION_SPATIAL_CELL_SIZE
            right = (cell[0] + radius + 1) * NAVIGATION_SPATIAL_CELL_SIZE
            bottom = (cell[1] - radius) * NAVIGATION_SPATIAL_CELL_SIZE
            top = (cell[1] + radius + 1) * NAVIGATION_SPATIAL_CELL_SIZE
            unsearched_distance = min(
                point[0] - left,
                right - point[0],
                point[2] - bottom,
                top - point[2],
            )
            if best is not None and best[0] <= unsearched_distance + 1e-6:
                break
            if (
                max_horizontal_distance is not None
                and unsearched_distance > max_horizontal_distance
                and (best is None or best[0] > max_horizontal_distance)
            ):
                raise NavigationError(
                    "position lies outside the allowed navigation snap distance"
                )
        if best is None:
            raise NavigationError("navigation mesh has no projectable triangle")
        if (
            max_horizontal_distance is not None
            and best[0] > max_horizontal_distance + 1e-6
        ):
            raise NavigationError(
                "position lies outside the allowed navigation snap distance"
            )
        return best[3], best[2]

    def project_position(
        self,
        point: tuple[float, float, float],
        *,
        max_horizontal_distance: float | None = None,
    ) -> tuple[float, float, float]:
        """Clamp X/Z to walkable ground and sample that triangle's exact Y."""

        projected, _triangle_id = self._project_position_and_triangle(
            point,
            max_horizontal_distance=max_horizontal_distance,
        )
        return projected

    def project_position_relative(
        self,
        point: tuple[float, float, float],
        reference: tuple[float, float, float],
        *,
        max_horizontal_distance: float | None = None,
    ) -> tuple[float, float, float]:
        """Project world coordinates while preserving a map-layer Y offset.

        PathEngine elevation and network actor elevation differ by a constant
        local offset in several shipped maps. The reference (usually an
        authored spawn point) calibrates that offset; the mesh still supplies
        all subsequent slope and stair height changes.
        """

        reference_ground = self.project_position(
            reference,
            max_horizontal_distance=max_horizontal_distance,
        )
        vertical_offset = float(reference[1]) - reference_ground[1]
        projected = self.project_position(
            (
                float(point[0]),
                float(point[1]) - vertical_offset,
                float(point[2]),
            ),
            max_horizontal_distance=max_horizontal_distance,
        )
        return (
            projected[0],
            projected[1] + vertical_offset,
            projected[2],
        )

    def height_on_triangle(
        self, triangle_id: int, point: tuple[float, float, float]
    ) -> float:
        weights = self._barycentric(triangle_id, point)
        if weights is None:
            projected, _distance = self._closest_point_on_triangle(
                triangle_id, point
            )
            return projected[1]
        return self._height_from_weights(triangle_id, weights)

    def triangle_for_position(self, point: tuple[float, float, float]) -> int:
        _projected, triangle_id = self._project_position_and_triangle(point)
        return triangle_id

    def component_for_position(
        self,
        point: tuple[float, float, float],
        *,
        max_horizontal_distance: float | None = None,
        all_connections: bool = False,
    ) -> int:
        """Return the connected walkable component containing *point*.

        PathEngine exports scenery islands and event-only surfaces alongside
        the component on which the player actually spawned. A point being
        projectable is therefore not sufficient evidence that the player can
        walk to, shoot at, or collect something there.
        """

        _projected, triangle_id = self._project_position_and_triangle(
            point,
            max_horizontal_distance=max_horizontal_distance,
        )
        if all_connections:
            return self._ground_component_by_triangle[triangle_id]
        return self._component_by_triangle[triangle_id]

    def is_reachable(
        self,
        start: tuple[float, float, float],
        goal: tuple[float, float, float],
        *,
        max_horizontal_snap: float | None = None,
        all_connections: bool = False,
    ) -> bool:
        """Return whether both points belong to the same walkable component.

        ``all_connections`` joins components across every authored
        connection, ignoring ``minimum_portal_width``: the player's ground.
        """

        try:
            return self.component_for_position(
                start,
                max_horizontal_distance=max_horizontal_snap,
                all_connections=all_connections,
            ) == self.component_for_position(
                goal,
                max_horizontal_distance=max_horizontal_snap,
                all_connections=all_connections,
            )
        except NavigationError:
            return False

    def component_positions(
        self,
        component_id: int,
        *,
        around: tuple[float, float, float],
        minimum_distance: float = 0.0,
        maximum_distance: float = 40.0,
        limit: int = 12,
    ) -> tuple[tuple[float, float, float], ...]:
        """Sample bounded walkable triangle centers from one component."""

        component_id = int(component_id)
        if not 0 <= component_id < len(self._components):
            raise NavigationError(
                f"unknown navigation component {component_id}"
            )
        minimum_distance = max(0.0, float(minimum_distance))
        maximum_distance = max(
            minimum_distance, float(maximum_distance)
        )
        candidates = sorted(
            (
                (
                    self._horizontal_distance(around, self._centroids[index]),
                    self._centroids[index],
                )
                for index in self._components[component_id]
                if minimum_distance
                <= self._horizontal_distance(
                    around, self._centroids[index]
                )
                <= maximum_distance
            ),
            key=lambda value: value[0],
        )
        if not candidates or limit <= 0:
            return ()
        count = min(int(limit), len(candidates))
        return tuple(
            candidates[
                min(
                    len(candidates) - 1,
                    math.floor((ordinal + 0.5) * len(candidates) / count),
                )
            ][1]
            for ordinal in range(count)
        )

    def find_route(
        self,
        start: tuple[float, float, float],
        goal: tuple[float, float, float],
        *,
        search_center: tuple[float, float, float] | None = None,
        max_search_distance: float | None = None,
    ) -> NavigationRoute:
        if (search_center is None) != (max_search_distance is None):
            raise ValueError(
                "search_center and max_search_distance must be supplied together"
            )
        if max_search_distance is not None:
            max_search_distance = float(max_search_distance)
            if max_search_distance <= 0.0:
                raise ValueError("max_search_distance must be positive")
            if self._horizontal_distance(goal, search_center) > (
                max_search_distance + 1e-6
            ):
                raise NavigationError(
                    "navigation goal lies outside the bounded search territory"
                )
        projected_start, start_triangle = self._project_position_and_triangle(start)
        projected_goal, goal_triangle = self._project_position_and_triangle(goal)
        if start_triangle == goal_triangle:
            return NavigationRoute((projected_start, projected_goal), (start_triangle,))

        frontier = [(0.0, start_triangle)]
        came_from: dict[int, int | None] = {start_triangle: None}
        cost_so_far = {start_triangle: 0.0}
        while frontier:
            _priority, current = heapq.heappop(frontier)
            if current == goal_triangle:
                break
            for neighbour in self._adjacency[current]:
                if (
                    search_center is not None
                    and neighbour != goal_triangle
                    and self._horizontal_distance(
                        self._centroids[neighbour], search_center
                    )
                    > max_search_distance + self.minimum_portal_width
                ):
                    continue
                new_cost = cost_so_far[current] + self._horizontal_distance(
                    self._centroids[current], self._centroids[neighbour]
                )
                if new_cost >= cost_so_far.get(neighbour, math.inf):
                    continue
                cost_so_far[neighbour] = new_cost
                heuristic = self._horizontal_distance(
                    self._centroids[neighbour], projected_goal
                )
                heapq.heappush(frontier, (new_cost + heuristic, neighbour))
                came_from[neighbour] = current
        if goal_triangle not in came_from:
            raise NavigationError(
                f"no connected navigation route from triangle {start_triangle} "
                f"to {goal_triangle}"
            )
        triangle_path = []
        current: int | None = goal_triangle
        while current is not None:
            triangle_path.append(current)
            current = came_from[current]
        triangle_path.reverse()

        portals = []
        for left_triangle, right_triangle in zip(triangle_path, triangle_path[1:]):
            try:
                first, second = self._portals[(left_triangle, right_triangle)]
            except KeyError as exc:
                raise NavigationError(
                    f"missing portal {left_triangle}->{right_triangle}"
                ) from exc
            portals.append(
                self._oriented_portal(left_triangle, first, second)
            )
        points = _string_pull(projected_start, projected_goal, portals)
        return NavigationRoute(tuple(points), tuple(triangle_path))

    def _oriented_portal(
        self, from_triangle: int, first: int, second: int,
    ) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """Orient and inset a shared edge from the incoming triangle."""

        a, b = self.vertices[first], self.vertices[second]
        centre = self._centroids[from_triangle]
        if _area2(centre, a, b) < 0.0:
            a, b = b, a
        width = self._horizontal_distance(a, b)
        inset = min(SMALL_AGENT_DIAMETER / 2.0, width / 2.0)
        if width > 1e-9 and inset > 0.0:
            fraction = inset / width
            a, b = (
                tuple(a[axis] + (b[axis] - a[axis]) * fraction for axis in range(3)),
                tuple(b[axis] + (a[axis] - b[axis]) * fraction for axis in range(3)),
            )
        return a, b

    def advance_toward(
        self,
        start: tuple[float, float, float],
        goal: tuple[float, float, float],
        distance: float,
        *,
        search_center: tuple[float, float, float] | None = None,
        max_search_distance: float | None = None,
    ) -> tuple[float, float, float]:
        remaining = max(0.0, float(distance))
        projected_start = self.project_position(start)
        vertical_offset = float(start[1]) - projected_start[1]
        route = self.find_route(
            (
                float(start[0]),
                float(start[1]) - vertical_offset,
                float(start[2]),
            ),
            (
                float(goal[0]),
                float(goal[1]) - vertical_offset,
                float(goal[2]),
            ),
            search_center=search_center,
            max_search_distance=max_search_distance,
        )
        current = route.points[0]
        for waypoint in route.points[1:]:
            segment = self._horizontal_distance(current, waypoint)
            if segment <= 1e-8:
                current = waypoint
                continue
            if remaining >= segment:
                current = waypoint
                remaining -= segment
                continue
            ratio = remaining / segment
            advanced = tuple(
                current[axis] + (waypoint[axis] - current[axis]) * ratio
                for axis in range(3)
            )
            return (
                advanced[0],
                advanced[1] + vertical_offset,
                advanced[2],
            )
        return (current[0], current[1] + vertical_offset, current[2])


def _area2(a, b, c) -> float:
    """Twice the signed area of a, b, and c in the X/Z plane."""

    return (c[0] - a[0]) * (b[2] - a[2]) - (b[0] - a[0]) * (c[2] - a[2])


def _string_pull(start, goal, portals):
    """Find the shortest route through a sequence of oriented portals."""

    portals = [(start, start)] + list(portals) + [(goal, goal)]
    points = [start]
    apex = left = right = start
    apex_index = left_index = right_index = 0
    index = 1
    while index < len(portals):
        new_left, new_right = portals[index]
        if _area2(apex, right, new_right) <= 0.0:
            if apex == right or _area2(apex, left, new_right) > 0.0:
                right, right_index = new_right, index
            else:
                points.append(left)
                apex, apex_index = left, left_index
                left = right = apex
                left_index = right_index = apex_index
                index = apex_index + 1
                continue
        if _area2(apex, left, new_left) >= 0.0:
            if apex == left or _area2(apex, right, new_left) < 0.0:
                left, left_index = new_left, index
            else:
                points.append(right)
                apex, apex_index = right, right_index
                left = right = apex
                left_index = right_index = apex_index
                index = apex_index + 1
                continue
        index += 1
    if points[-1] != goal:
        points.append(goal)
    return points


def _read_mesh3d(
    data: bytes,
) -> tuple[
    tuple[tuple[float, float, float], ...],
    tuple[NavigationTriangle, ...],
]:
    reader = _TokenReader(data)
    elements = []
    while True:
        value = reader.cstring()
        if not value:
            break
        elements.append(value)
    attributes = []
    while True:
        kind = reader.byte()
        if not kind:
            break
        attributes.append((reader.cstring(), kind))
    if "mesh3D" not in elements or "mesh3DPartitioning" not in elements:
        raise NavigationError("tokenised file is not a PathEngine ground mesh")

    raw_vertices: list[dict[str, object]] = []
    raw_triangles: list[dict[str, object]] = []
    partition_token = elements.index("mesh3DPartitioning") + 1

    def read_element(index: int | None = None) -> None:
        index = reader.byte() if index is None else index
        if index == partition_token:
            raise _MeshComplete
        if not 1 <= index <= len(elements):
            raise NavigationError(f"invalid element token {index}")
        name = elements[index - 1]
        values: dict[str, object] = {}
        while True:
            attribute_index = reader.byte()
            if not attribute_index:
                break
            if not 1 <= attribute_index <= len(attributes):
                raise NavigationError(f"invalid attribute token {attribute_index}")
            attribute_name, kind = attributes[attribute_index - 1]
            values[attribute_name] = reader.attribute_value(kind)
        if name == "vert":
            raw_vertices.append(values)
        elif name == "tri":
            raw_triangles.append(values)
        while True:
            child = reader.byte()
            if not child:
                break
            read_element(child)

    try:
        read_element()
    except _MeshComplete:
        pass
    scale = PATHENGINE_UNITS_PER_WORLD_UNIT
    # PathEngine serialises its horizontal plane as Y/X for these Nebula map
    # exports, with Z remaining the elevation axis.  The previous X/Z/Y
    # conversion mirrored the horizontal plane: cave navigation covered the
    # entrance but missed most database spawn volumes, so rejected density
    # samples collapsed onto the same nearby boundary.  Converting to the
    # client's world X/Y/Z frame below makes the token bounds agree with both
    # the map collision meshes and the authored instance transforms.
    vertices = tuple(
        (
            float(vertex.get("y", 0)) / scale,
            float(vertex.get("z", 0)) / scale,
            float(vertex.get("x", 0)) / scale,
        )
        for vertex in raw_vertices
    )
    triangles = []
    for values in raw_triangles:
        vertex_ids = tuple(
            int(values[f"edge{edge}StartVert"])
            for edge in range(3)
        )
        connections = tuple(
            (
                int(values[f"edge{edge}Connection"])
                if f"edge{edge}Connection" in values
                else None
            )
            for edge in range(3)
        )
        if any(not 0 <= index < len(vertices) for index in vertex_ids):
            raise NavigationError("triangle references an invalid vertex")
        triangles.append(NavigationTriangle(vertex_ids, connections))
    return vertices, tuple(triangles)


# A navigation mesh can contain hundreds of thousands of vertices. Keeping
# sixteen different maps resident consumes several gigabytes during fast
# travel/test sweeps even though a local server normally has only one active
# map (plus a handoff destination).
@lru_cache(maxsize=4)
def load_navigation_mesh(
    path: str | Path,
    *,
    minimum_portal_width: float = SMALL_AGENT_DIAMETER,
) -> PathEngineNavigationMesh:
    return PathEngineNavigationMesh.from_tokenised_mesh(
        Path(path), minimum_portal_width=minimum_portal_width,
    )
