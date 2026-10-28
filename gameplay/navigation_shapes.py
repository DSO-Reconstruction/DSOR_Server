"""Native navigation footprints used by actor-targeted melee validation.

The client subtracts the target's ActorShapeId radius from center distance,
not its physics CapsuleRadius. See docs/MELEE_TARGETING.md for native evidence.
"""

from contextlib import closing
from functools import lru_cache
import math
from pathlib import Path
import sqlite3
import struct
from types import MappingProxyType

from dso_server.runtime import resident_content
from dso_server.paths import DATA_ROOT


@lru_cache(maxsize=4)
def navigation_shape_radii(path=DATA_ROOT / "navigation" / "navshapes.bin"):
    data = resident_content.read_bytes(path)
    if len(data) < 12 or data[:4] != b"HSAN":
        raise ValueError("invalid navigation shape table")
    version, count = struct.unpack_from("<II", data, 4)
    if version != 1:
        raise ValueError(f"unsupported navigation shape version {version}")
    offset, result = 12, {}
    for _ in range(count):
        length, = struct.unpack_from("<H", data, offset)
        offset += 2
        name = data[offset:offset + length].decode("utf-8")
        offset += length
        width, depth, _height = struct.unpack_from("<fff", data, offset)
        offset += 12
        if not all(math.isfinite(v) and v > 0 for v in (width, depth)):
            raise ValueError(f"invalid navigation shape dimensions for {name!r}")
        # CreateShape truncates to centimeters, builds an octagon, and the
        # loader measures vertex zero (width / 3, depth) from its center.
        x, z = int(width * 100), int(depth * 100)
        result[name] = math.hypot(x // 3, z) / 100.0
    if offset != len(data):
        raise ValueError("unexpected trailing navigation shape data")
    result["default"] = result["small"]
    return MappingProxyType(result)


@lru_cache(maxsize=4)
def monster_target_radii(static_database_path):
    shapes = navigation_shape_radii()
    with closing(sqlite3.connect(f"file:{static_database_path}?mode=ro", uri=True)) as db:
        return MappingProxyType({str(template): shapes[str(shape)]
            for template, shape in db.execute('SELECT Id, ActorShapeId FROM _Template_Monster')
            if str(shape) in shapes})
