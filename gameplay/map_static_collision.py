"""Static line of sight against a map's placed collision meshes.

A map's collision is split across two files:

* ``<map>.col`` (``CPAM`` bundle, version 1) is a flat list of resources,
  each ``u16 length, name ("meshes:<folder>/<mesh>_c_0.nvx2"), u32 size,
  NVX2 blob``.  The blobs are position-only NVX2 meshes (component mask 1):
  header, ``groups`` records, float3 vertices and u16 triangle indices.
* ``<map>.map`` (``MOSD``) places them.  A TMPL template names its collision
  resource (``coll_res_id`` into the STRT string table) and the mesh group it
  uses; every INST instance carries a position, a rotation quaternion, an
  optional scale, a collide flag and an event-mapping index into EMAL.

Instances are already in the absolute map-world frame used by combat,
navigation and :mod:`map_collision_terrain`.  Only steep triangles (walls,
building sides, rocks) are kept: ground, ramps and stairs never stop a shot,
and roofs sit above the bullet.  Instances that exist only under some event
state (EMAL bit strings containing a zero) are left out so that a hidden
event barrier never blocks a shot.
"""

from __future__ import annotations

from array import array
from functools import lru_cache
import math
from pathlib import Path
import struct

from dso_server.gameplay.map_collision_terrain import (
    CollisionTerrainError,
    _transform_vertex,
)


MAP_MAGIC = b"MOSD"
COLLISION_BUNDLE_MAGIC = b"CPAM"
NVX2_MAGIC = b"2XVN"
INVALID_INDEX = 0xFFFF
TEMPLATE_RECORD_SIZE = 46
LINE_OF_SIGHT_CELL_SIZE = 4.0
# Triangles whose normal points more upward than this are floor, ramp or roof.
MAXIMUM_WALL_UPWARD_NORMAL_RATIO = 0.7
# Destructible props change state at runtime; their static mesh would linger.
DYNAMIC_MESH_PREFIXES = ("destroy_",)


class StaticCollisionError(CollisionTerrainError):
    pass


def _nvx2_triangles(blob: bytes, group_index: int):
    """Vertices and triangle index triples of one NVX2 collision mesh."""
    if len(blob) < 28 or blob[:4] != NVX2_MAGIC:
        raise StaticCollisionError("invalid NVX2 collision mesh")
    group_count, vertex_count, width, triangle_count, _edges, _mask = (
        struct.unpack_from("<6I", blob, 4)
    )
    if width < 3:
        raise StaticCollisionError("NVX2 collision vertices have no position")
    groups = [
        struct.unpack_from("<6I", blob, 28 + index * 24)
        for index in range(group_count)
    ]
    vertex_offset = 28 + group_count * 24
    index_offset = vertex_offset + vertex_count * width * 4
    if index_offset + triangle_count * 6 > len(blob):
        raise StaticCollisionError("truncated NVX2 collision mesh")
    stride = width * 4
    vertices = [
        struct.unpack_from("<3f", blob, vertex_offset + index * stride)
        for index in range(vertex_count)
    ]
    indices = struct.unpack_from(f"<{triangle_count * 3}H", blob, index_offset)
    if 0 <= group_index < len(groups):
        first, count = groups[group_index][2], groups[group_index][3]
    else:
        first, count = 0, triangle_count
    first = max(0, min(first, triangle_count))
    count = max(0, min(count, triangle_count - first))
    triangles = []
    for triangle in range(first, first + count):
        a, b, c = indices[triangle * 3:triangle * 3 + 3]
        if a >= vertex_count or b >= vertex_count or c >= vertex_count:
            raise StaticCollisionError("NVX2 triangle references an invalid vertex")
        triangles.append((a, b, c))
    return vertices, triangles


def read_collision_bundle(path: str | Path) -> dict[str, bytes]:
    """Raw NVX2 blobs of a ``CPAM`` bundle, by resource name."""
    data = Path(path).read_bytes()
    if len(data) < 8 or data[:4] != COLLISION_BUNDLE_MAGIC:
        raise StaticCollisionError(f"{path} is not a CPAM collision bundle")
    offset, result = 8, {}
    while offset < len(data):
        if offset + 2 > len(data):
            raise StaticCollisionError("truncated collision resource name")
        size = struct.unpack_from("<H", data, offset)[0]
        offset += 2
        if offset + size + 4 > len(data):
            raise StaticCollisionError("truncated collision resource entry")
        name = data[offset:offset + size].decode("utf-8")
        offset += size
        blob_size = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        if offset + blob_size > len(data):
            raise StaticCollisionError("truncated collision resource blob")
        result[name] = data[offset:offset + blob_size]
        offset += blob_size
    return result


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data, self.offset = data, 0

    def take(self, size: int) -> bytes:
        if self.offset + size > len(self.data):
            raise StaticCollisionError("truncated MOSD map")
        value = self.data[self.offset:self.offset + size]
        self.offset += size
        return value

    def tag(self, expected: bytes) -> None:
        if self.take(4) != expected[::-1]:
            raise StaticCollisionError(f"MOSD map lacks its {expected!r} section")

    def unpack(self, fmt: str):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))


def read_map_collision_instances(path: str | Path):
    """``(resource, group, position, rotation, scale)`` for each colliding
    instance present whatever the map's event state."""
    reader = _Reader(Path(path).read_bytes())
    reader.tag(b"DSOM")
    reader.unpack("<I")
    reader.tag(b"MAPI")
    reader.take(48 + 6)
    reader.tag(b"STRT")
    string_count, string_size = reader.unpack("<II")
    strings = reader.take(string_size).split(b"\0")[:string_count]
    strings = [value.decode("utf-8", "replace") for value in strings]
    reader.tag(b"SETT")
    reader.take(reader.unpack("<I")[0] * 4)
    reader.tag(b"EVET")
    reader.take(reader.unpack("<I")[0] * 4)
    reader.tag(b"EMAL")
    always_present = []
    for _index in range(reader.unpack("<I")[0]):
        size = reader.unpack("<H")[0]
        always_present.append(b"0" not in reader.take(size))
    reader.tag(b"TMPL")
    templates = []
    for _index in range(reader.unpack("<I")[0]):
        values = struct.unpack("<5H8f2H", reader.take(TEMPLATE_RECORD_SIZE))
        resource = (strings[values[3]] if values[3] != INVALID_INDEX
                    and values[3] < len(strings) else "")
        templates.append((resource, values[13]))
    reader.tag(b"INST")
    result = []
    for _index in range(reader.unpack("<I")[0]):
        position = reader.unpack("<4f")[:3]
        rotation = reader.unpack("<4f")
        scale = reader.unpack("<4f")[:3] if reader.take(1)[0] else (1.0, 1.0, 1.0)
        collide = reader.take(1)[0] != 0
        reader.take(1)  # visible for navigation
        template, _group, _name, mapping = reader.unpack("<hhHH")
        if not collide or not 0 <= template < len(templates):
            continue
        resource, mesh_group = templates[template]
        if not resource:
            continue
        if mapping < len(always_present) and not always_present[mapping]:
            continue
        result.append((resource, mesh_group, position, rotation, scale))
    return tuple(result)


class StaticLineOfSight:
    """World-space wall triangles bucketed in a coarse X/Z grid."""

    def __init__(self, triangles) -> None:
        # Flat float64 rows of 11: a, edge1, edge2 and the triangle's Y span
        # (a 100k-triangle dungeon stays near 10 MB).
        self._triangles = array("d")
        count = 0
        cells: dict[tuple[int, int], list[int]] = {}
        size = LINE_OF_SIGHT_CELL_SIZE
        for a, b, c in triangles:
            e1 = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
            e2 = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
            nx = e1[1] * e2[2] - e1[2] * e2[1]
            ny = e1[2] * e2[0] - e1[0] * e2[2]
            nz = e1[0] * e2[1] - e1[1] * e2[0]
            length = math.sqrt(nx * nx + ny * ny + nz * nz)
            if length <= 1e-8 or abs(ny) / length > MAXIMUM_WALL_UPWARD_NORMAL_RATIO:
                continue
            index = count
            count += 1
            self._triangles.extend((
                a[0], a[1], a[2], e1[0], e1[1], e1[2], e2[0], e2[1], e2[2],
                min(a[1], b[1], c[1]), max(a[1], b[1], c[1]),
            ))
            for x in range(math.floor(min(a[0], b[0], c[0]) / size),
                           math.floor(max(a[0], b[0], c[0]) / size) + 1):
                for z in range(math.floor(min(a[2], b[2], c[2]) / size),
                               math.floor(max(a[2], b[2], c[2]) / size) + 1):
                    cells.setdefault((x, z), []).append(index)
        self._count = count
        self._cells = {cell: array("i", ids) for cell, ids in cells.items()}

    @property
    def triangle_count(self) -> int:
        return self._count

    def _cells_along(self, x0, z0, x1, z1):
        size = LINE_OF_SIGHT_CELL_SIZE
        cx, cz = math.floor(x0 / size), math.floor(z0 / size)
        ex, ez = math.floor(x1 / size), math.floor(z1 / size)
        dx, dz = x1 - x0, z1 - z0
        step_x, step_z = (1 if dx > 0 else -1), (1 if dz > 0 else -1)
        t_max_x = (((cx + (dx > 0)) * size - x0) / dx) if dx else math.inf
        t_max_z = (((cz + (dz > 0)) * size - z0) / dz) if dz else math.inf
        t_dx = size / abs(dx) if dx else math.inf
        t_dz = size / abs(dz) if dz else math.inf
        for _step in range(abs(ex - cx) + abs(ez - cz)):
            yield cx, cz, min(t_max_x, t_max_z)
            if t_max_x < t_max_z:
                cx += step_x
                t_max_x += t_dx
            else:
                cz += step_z
                t_max_z += t_dz
        yield cx, cz, math.inf

    def first_hit(self, start, end) -> float | None:
        """Fraction (0..1] of the segment ``start -> end`` before a wall."""
        ox, oy, oz = (float(value) for value in start[:3])
        dx, dy, dz = (float(end[i]) - (ox, oy, oz)[i] for i in range(3))
        low, high = min(oy, oy + dy), max(oy, oy + dy)
        best = None
        seen = set()
        triangles = self._triangles
        for *cell, exit_t in self._cells_along(ox, oz, ox + dx, oz + dz):
            for index in self._cells.get(tuple(cell), ()):
                if index in seen:
                    continue
                seen.add(index)
                row = index * 11
                if triangles[row + 10] < low or triangles[row + 9] > high:
                    continue
                ax, ay, az, e1x, e1y, e1z, e2x, e2y, e2z = triangles[row:row + 9]
                # Moller-Trumbore, two-sided.
                px = dy * e2z - dz * e2y
                py = dz * e2x - dx * e2z
                pz = dx * e2y - dy * e2x
                det = e1x * px + e1y * py + e1z * pz
                if -1e-9 < det < 1e-9:
                    continue
                inverse = 1.0 / det
                tx, ty, tz = ox - ax, oy - ay, oz - az
                u = (tx * px + ty * py + tz * pz) * inverse
                if u < 0.0 or u > 1.0:
                    continue
                qx = ty * e1z - tz * e1y
                qy = tz * e1x - tx * e1z
                qz = tx * e1y - ty * e1x
                v = (dx * qx + dy * qy + dz * qz) * inverse
                if v < 0.0 or u + v > 1.0:
                    continue
                t = (e2x * qx + e2y * qy + e2z * qz) * inverse
                if 0.0 < t <= 1.0 and (best is None or t < best):
                    best = t
            if best is not None and best <= exit_t:
                # Cells are visited in order along the segment: every later
                # cell starts beyond this hit.
                return best
        return best

    def blocked(self, start, end) -> bool:
        return self.first_hit(start, end) is not None

    def clear_distance(self, start, end) -> float:
        """Distance along ``start -> end`` before the first wall (or its length)."""
        length = math.dist(start[:3], end[:3])
        hit = self.first_hit(start, end)
        return length if hit is None else hit * length


def build_static_line_of_sight(map_path: str | Path, collision_path: str | Path) -> StaticLineOfSight:
    blobs = read_collision_bundle(collision_path)
    meshes = {}
    triangles = []
    for resource, group, position, rotation, scale in read_map_collision_instances(map_path):
        if resource.rpartition("/")[2].startswith(DYNAMIC_MESH_PREFIXES):
            continue
        key = (resource, group)
        if key not in meshes:
            blob = blobs.get(resource)
            meshes[key] = None if blob is None else _nvx2_triangles(
                blob, -1 if group == INVALID_INDEX else group)
        mesh = meshes[key]
        if mesh is None:
            continue
        vertices, indices = mesh
        world = [_transform_vertex(vertex, position, rotation, scale) for vertex in vertices]
        triangles.extend((world[a], world[b], world[c]) for a, b, c in indices)
    return StaticLineOfSight(triangles)


@lru_cache(maxsize=6)
def load_static_line_of_sight(map_path: str | Path, collision_path: str | Path) -> StaticLineOfSight:
    """Build once per map, on the first shot that needs it."""
    return build_static_line_of_sight(Path(map_path), Path(collision_path))
