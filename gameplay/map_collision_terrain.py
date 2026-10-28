"""Sample the rendered map collision surface at server actor coordinates.

PathEngine's ``.tok`` files are suitable for connectivity and A* routing, but
their Y values are not the final world-space terrain heights in every DSO map.
The client renders and collides against the transformed NVX2 meshes embedded in
the map's ``.col`` bundle.  This module reads the same ``MOSD``/``CPAM`` data
and provides a cheap world-space height query for monster and loot placement.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
from pathlib import Path
import struct

from dso_server.runtime import resident_content


MAP_MAGIC = b"MOSD"
COLLISION_BUNDLE_MAGIC = b"CPAM"
NVX2_MAGIC = b"2XVN"
STRING_TABLE_TAG = b"TRTS"  # little-endian on-disk form of STRT
TEMPLATE_TABLE_TAG = b"LPMT"  # little-endian on-disk form of TMPL
INSTANCE_TABLE_TAG = b"TSNI"  # little-endian on-disk form of INST
INVALID_INDEX = 0xFFFF
TEMPLATE_RECORD_SIZE = 46
TERRAIN_SPATIAL_CELL_SIZE = 4.0
MINIMUM_UPWARD_NORMAL_RATIO = 0.25


class CollisionTerrainError(ValueError):
    pass


@dataclass(frozen=True)
class _CollisionMesh:
    vertices: tuple[tuple[float, float, float], ...]
    triangles: tuple[tuple[int, int, int], ...]


@dataclass(frozen=True)
class _SurfaceTriangle:
    a: tuple[float, float, float]
    b: tuple[float, float, float]
    c: tuple[float, float, float]
    minimum_x: float
    minimum_z: float
    maximum_x: float
    maximum_z: float
    denominator: float

    @classmethod
    def create(
        cls,
        a: tuple[float, float, float],
        b: tuple[float, float, float],
        c: tuple[float, float, float],
    ) -> "_SurfaceTriangle | None":
        ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        normal = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        normal_length = math.sqrt(sum(component * component for component in normal))
        if normal_length <= 1e-8:
            return None
        # Vertical walls and fence faces are collision, but never terrain.
        if abs(normal[1]) / normal_length < MINIMUM_UPWARD_NORMAL_RATIO:
            return None
        denominator = (
            (b[2] - c[2]) * (a[0] - c[0])
            + (c[0] - b[0]) * (a[2] - c[2])
        )
        if abs(denominator) <= 1e-8:
            return None
        return cls(
            a,
            b,
            c,
            min(a[0], b[0], c[0]),
            min(a[2], b[2], c[2]),
            max(a[0], b[0], c[0]),
            max(a[2], b[2], c[2]),
            denominator,
        )

    def height_at(self, x: float, z: float) -> float | None:
        tolerance = 1e-5
        if (
            x < self.minimum_x - tolerance
            or x > self.maximum_x + tolerance
            or z < self.minimum_z - tolerance
            or z > self.maximum_z + tolerance
        ):
            return None
        first = (
            (self.b[2] - self.c[2]) * (x - self.c[0])
            + (self.c[0] - self.b[0]) * (z - self.c[2])
        ) / self.denominator
        second = (
            (self.c[2] - self.a[2]) * (x - self.c[0])
            + (self.a[0] - self.c[0]) * (z - self.c[2])
        ) / self.denominator
        third = 1.0 - first - second
        if (
            first < -tolerance
            or second < -tolerance
            or third < -tolerance
        ):
            return None
        if max(self.a[1], self.b[1], self.c[1]) - min(
            self.a[1], self.b[1], self.c[1]
        ) <= 1e-7:
            # Preserve exact flat authored levels (0, 2, 4, ...). Barycentric
            # arithmetic can otherwise turn 2.0 into 2.0000000000000004.
            height = self.a[1]
            integral_height = round(height)
            return (
                float(integral_height)
                if abs(height - integral_height) <= 1e-7
                else height
            )
        return (
            first * self.a[1]
            + second * self.b[1]
            + third * self.c[1]
        )


class MapCollisionTerrain:
    """World-space upward collision triangles indexed by their X/Z cells."""

    def __init__(self, triangles: tuple[_SurfaceTriangle, ...]) -> None:
        if not triangles:
            raise CollisionTerrainError("map collision has no ground triangles")
        self._triangles = triangles
        cells: dict[tuple[int, int], list[int]] = {}
        for triangle_id, triangle in enumerate(triangles):
            minimum_x = math.floor(
                triangle.minimum_x / TERRAIN_SPATIAL_CELL_SIZE
            )
            maximum_x = math.floor(
                triangle.maximum_x / TERRAIN_SPATIAL_CELL_SIZE
            )
            minimum_z = math.floor(
                triangle.minimum_z / TERRAIN_SPATIAL_CELL_SIZE
            )
            maximum_z = math.floor(
                triangle.maximum_z / TERRAIN_SPATIAL_CELL_SIZE
            )
            for cell_x in range(minimum_x, maximum_x + 1):
                for cell_z in range(minimum_z, maximum_z + 1):
                    cells.setdefault((cell_x, cell_z), []).append(triangle_id)
        self._spatial_cells = {
            cell: tuple(triangle_ids)
            for cell, triangle_ids in cells.items()
        }

    @property
    def triangle_count(self) -> int:
        return len(self._triangles)

    def heights_at(
        self,
        point: tuple[float, float, float],
        *,
        minimum_height: float | None = None,
        maximum_height: float | None = None,
    ) -> tuple[float, ...]:
        """Return every collision surface intersected by a vertical ray."""

        x, _reference_y, z = (float(component) for component in point)
        cell = (
            math.floor(x / TERRAIN_SPATIAL_CELL_SIZE),
            math.floor(z / TERRAIN_SPATIAL_CELL_SIZE),
        )
        heights = []
        for triangle_id in self._spatial_cells.get(cell, ()):
            height = self._triangles[triangle_id].height_at(x, z)
            if height is None:
                continue
            if minimum_height is not None and height < minimum_height - 1e-5:
                continue
            if maximum_height is not None and height > maximum_height + 1e-5:
                continue
            if not any(abs(height - existing) <= 1e-4 for existing in heights):
                heights.append(height)
        return tuple(sorted(heights))

    def project_position(
        self,
        point: tuple[float, float, float],
        *,
        reference_height: float | None = None,
        minimum_height: float | None = None,
        maximum_height: float | None = None,
    ) -> tuple[float, float, float]:
        """Replace Y with the nearest actual collision-surface elevation."""

        reference = (
            float(point[1])
            if reference_height is None
            else float(reference_height)
        )
        heights = self.heights_at(
            point,
            minimum_height=minimum_height,
            maximum_height=maximum_height,
        )
        if not heights:
            raise CollisionTerrainError(
                "position does not intersect the map collision surface"
            )
        height = min(heights, key=lambda value: (abs(value - reference), -value))
        return (float(point[0]), height, float(point[2]))


def _read_collision_mesh(blob: bytes) -> _CollisionMesh:
    if len(blob) < 28 or blob[:4] != NVX2_MAGIC:
        raise CollisionTerrainError("invalid NVX2 collision mesh")
    (
        group_count,
        vertex_count,
        vertex_width,
        triangle_count,
        _edge_count,
        _component_mask,
    ) = struct.unpack_from("<6I", blob, 4)
    if vertex_width < 3:
        raise CollisionTerrainError("NVX2 collision vertices have no position")
    vertex_offset = 28 + group_count * 24
    index_offset = vertex_offset + vertex_count * vertex_width * 4
    required_size = index_offset + triangle_count * 3 * 2
    if required_size > len(blob):
        raise CollisionTerrainError("truncated NVX2 collision mesh")
    vertices = tuple(
        tuple(
            float(value)
            for value in struct.unpack_from(
                "<3f", blob, vertex_offset + index * vertex_width * 4
            )
        )
        for index in range(vertex_count)
    )
    raw_indices = struct.unpack_from(
        f"<{triangle_count * 3}H", blob, index_offset
    )
    triangles = tuple(
        tuple(int(raw_indices[offset + component]) for component in range(3))
        for offset in range(0, len(raw_indices), 3)
    )
    if any(index >= vertex_count for triangle in triangles for index in triangle):
        raise CollisionTerrainError("NVX2 triangle references an invalid vertex")
    return _CollisionMesh(vertices, triangles)


def _read_collision_bundle(path: Path) -> dict[str, _CollisionMesh]:
    data = resident_content.read_bytes(path)
    if len(data) < 8 or data[:4] != COLLISION_BUNDLE_MAGIC:
        raise CollisionTerrainError(f"{path} is not a CPAM collision bundle")
    offset = 8
    result = {}
    while offset < len(data):
        if offset + 2 > len(data):
            raise CollisionTerrainError("truncated collision resource name")
        name_size = struct.unpack_from("<H", data, offset)[0]
        offset += 2
        if offset + name_size + 4 > len(data):
            raise CollisionTerrainError("truncated collision resource entry")
        name = data[offset : offset + name_size].decode("utf-8")
        offset += name_size
        blob_size = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        if offset + blob_size > len(data):
            raise CollisionTerrainError("truncated collision resource blob")
        result[name] = _read_collision_mesh(data[offset : offset + blob_size])
        offset += blob_size
    return result


def _rotate_vertex(
    vertex: tuple[float, float, float],
    quaternion: tuple[float, float, float, float],
) -> tuple[float, float, float]:
    x, y, z, w = quaternion
    length = math.sqrt(x * x + y * y + z * z + w * w)
    if length <= 1e-8:
        return vertex
    x, y, z, w = (component / length for component in quaternion)
    vx, vy, vz = vertex
    # q * v * conjugate(q), expanded to avoid temporary quaternion objects.
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


def _transform_vertex(
    vertex: tuple[float, float, float],
    position: tuple[float, float, float],
    quaternion: tuple[float, float, float, float],
    scale: tuple[float, float, float],
) -> tuple[float, float, float]:
    rotated = _rotate_vertex(
        (
            vertex[0] * scale[0],
            vertex[1] * scale[1],
            vertex[2] * scale[2],
        ),
        quaternion,
    )
    return (
        rotated[0] + position[0],
        rotated[1] + position[1],
        rotated[2] + position[2],
    )


def _read_map_surface_triangles(
    path: Path,
    collision_meshes: dict[str, _CollisionMesh],
) -> tuple[_SurfaceTriangle, ...]:
    data = resident_content.read_bytes(path)
    if len(data) < 8 or data[:4] != MAP_MAGIC:
        raise CollisionTerrainError(f"{path} is not a MOSD map")

    string_offset = data.find(STRING_TABLE_TAG, 8)
    if string_offset < 0 or string_offset + 12 > len(data):
        raise CollisionTerrainError("map has no STRT string table")
    string_count, string_size = struct.unpack_from("<II", data, string_offset + 4)
    string_start = string_offset + 12
    string_end = string_start + string_size
    if string_end > len(data):
        raise CollisionTerrainError("truncated map string table")
    strings = data[string_start:string_end].rstrip(b"\0").split(b"\0")
    if len(strings) != string_count:
        raise CollisionTerrainError("map string table count mismatch")
    decoded_strings = tuple(value.decode("utf-8") for value in strings)

    template_offset = data.find(TEMPLATE_TABLE_TAG, string_end)
    if template_offset < 0 or template_offset + 8 > len(data):
        raise CollisionTerrainError("map has no TMPL template table")
    template_count = struct.unpack_from("<I", data, template_offset + 4)[0]
    template_start = template_offset + 8
    template_end = template_start + template_count * TEMPLATE_RECORD_SIZE
    if template_end > len(data):
        raise CollisionTerrainError("truncated map template table")
    collision_names = []
    for template_index in range(template_count):
        values = struct.unpack_from(
            "<5H8f2H",
            data,
            template_start + template_index * TEMPLATE_RECORD_SIZE,
        )
        collision_string_index = values[3]
        collision_names.append(
            decoded_strings[collision_string_index]
            if collision_string_index != INVALID_INDEX
            and collision_string_index < len(decoded_strings)
            else None
        )

    if data[template_end : template_end + 4] != INSTANCE_TABLE_TAG:
        raise CollisionTerrainError("INST table does not follow TMPL records")
    if template_end + 8 > len(data):
        raise CollisionTerrainError("truncated map instance table")
    instance_count = struct.unpack_from("<I", data, template_end + 4)[0]
    offset = template_end + 8
    surfaces = []
    for _instance_index in range(instance_count):
        if offset + 35 > len(data):
            raise CollisionTerrainError("truncated map instance")
        position4 = struct.unpack_from("<4f", data, offset)
        quaternion = struct.unpack_from("<4f", data, offset + 16)
        offset += 32
        has_scale = bool(data[offset])
        offset += 1
        if has_scale:
            if offset + 16 > len(data):
                raise CollisionTerrainError("truncated map instance scale")
            scale4 = struct.unpack_from("<4f", data, offset)
            scale = tuple(float(value) for value in scale4[:3])
            offset += 16
        else:
            scale = (1.0, 1.0, 1.0)
        if offset + 10 > len(data):
            raise CollisionTerrainError("truncated map instance metadata")
        offset += 2  # visible/collision flags
        template_index, _group, _event, _set = struct.unpack_from(
            "<4H", data, offset
        )
        offset += 8
        if template_index >= len(collision_names):
            continue
        collision_name = collision_names[template_index]
        if collision_name is None:
            continue
        mesh = collision_meshes.get(collision_name)
        if mesh is None:
            continue
        position = tuple(float(value) for value in position4[:3])
        transformed = tuple(
            _transform_vertex(vertex, position, quaternion, scale)
            for vertex in mesh.vertices
        )
        for indices in mesh.triangles:
            triangle = _SurfaceTriangle.create(
                transformed[indices[0]],
                transformed[indices[1]],
                transformed[indices[2]],
            )
            if triangle is not None:
                surfaces.append(triangle)
    return tuple(surfaces)


@lru_cache(maxsize=4)
def load_map_collision_terrain(
    map_path: str | Path,
    collision_path: str | Path,
) -> MapCollisionTerrain:
    """Load and cache the terrain used by one active map encounter."""

    map_path = Path(map_path)
    collision_path = Path(collision_path)
    collision_meshes = _read_collision_bundle(collision_path)
    return MapCollisionTerrain(
        _read_map_surface_triangles(map_path, collision_meshes)
    )
