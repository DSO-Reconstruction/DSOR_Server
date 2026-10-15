"""Server authority over where a player is and where a player's skill lands.

The client predicts its own movement and reports it: one MoveCommand per game
tick (25 a second) carrying where it stands. Until this module the server took
every report as the truth, and every range check measured from it. Now:

* a reported position must be on the walkable navigation mesh (within
  NAVMESH_SNAP), in the connected component the player stood on, and reachable
  in a straight line over walkable ground (no stepping through a wall);
* the distance covered is bounded by a budget refilled at MAX_PLAYER_SPEED
  (the MovementSpeed attribute cap, 13, plus the bonus a mount or a buff adds)
  and capped at BURST_SECONDS of it, so per-record tolerances cannot add up;
* Teleport / Jump / Charge skills move the player by more than that: their
  destination is clamped to the skill's AttackRange and to walkable, reachable
  ground when the cast is accepted, and the moves that follow may then follow
  that path once (an allowance), and only that one;
* a skill command with a non-finite number (NaN, infinity) is dropped: NaN
  defeats every comparison, so such a target used to hit every monster.

A rejected move leaves the server position where it was; the client is put back
there with a ForceTransform (at most every CORRECTION_INTERVAL seconds).
Maps without a PathEngine export keep the speed bound only.
"""

from __future__ import annotations

import dataclasses
import math
import time

from dso_server.gameplay.pathengine_navigation import NavigationError, PathEngineNavigationMesh

# MovementSpeed's attribute upper limit is 13 (static.db4 _Template_ActorAttributes);
# a mount or a speed buff adds MovementBonus on top. Generous on purpose: a
# false correction rubber-bands an honest player.
MAX_PLAYER_SPEED = 13.0 * 1.6
# How much distance the budget holds: one second and a half of running.
BURST_SECONDS = 1.5
# Slack for quantization (1/128) and the client's own snapping.
POSITION_TOLERANCE = 1.0
# How far off the walkable mesh a reported position may be.
NAVMESH_SNAP = 1.5
# Spacing of the straight-line walkability samples between two positions.
SEGMENT_STEP = 0.5
SEGMENT_SNAP = 0.9
CORRECTION_INTERVAL = 0.3
GAME_TICKS_PER_SECOND = 25.0
# Ticks between two records beyond which the gap counts as this long.
MAX_ELAPSED_SECONDS = 2.0

MOVEMENT_SKILL_TYPES = frozenset({"teleport", "jump", "charge", "chargethrough"})
# A movement skill's path stays open this long after its cast.
ALLOWANCE_SECONDS = 3.0
ALLOWANCE_CORRIDOR = 2.0
RANGE_TOLERANCE = 1.0


def non_finite(value) -> bool:
    """Whether a decoded command holds a NaN or an infinity anywhere."""

    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, (tuple, list)):
        return any(non_finite(v) for v in value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return any(non_finite(getattr(value, f.name)) for f in dataclasses.fields(value))
    return False


def _flat_distance(a, b) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[2]) - float(b[2]))


def _navigation(s):
    combat = s.gameplay_combat_state
    if combat is None:
        return None, None
    encounter = combat.encounter
    navigation = getattr(encounter, "navigation", None)
    if not isinstance(navigation, PathEngineNavigationMesh):
        navigation = None
    return encounter.coordinate_frame, navigation


def walkable_path(navigation, start_world, end_world) -> bool:
    """end on the mesh, in start's component, and the straight line between
    them over walkable ground."""

    try:
        navigation.project_position(end_world, max_horizontal_distance=NAVMESH_SNAP)
        if not navigation.is_reachable(start_world, end_world, max_horizontal_snap=NAVMESH_SNAP):
            return False
        length = _flat_distance(start_world, end_world)
        steps = int(length / SEGMENT_STEP)
        for i in range(1, steps + 1):
            f = i * SEGMENT_STEP / length
            point = (
                start_world[0] + (end_world[0] - start_world[0]) * f,
                start_world[1] + (end_world[1] - start_world[1]) * f,
                start_world[2] + (end_world[2] - start_world[2]) * f,
            )
            navigation.project_position(point, max_horizontal_distance=SEGMENT_SNAP)
    except (NavigationError, ValueError):
        return False
    return True


def _on_corridor(point, start, end, width) -> bool:
    """point within `width` of the segment start-end (X/Z)."""

    sx, sz = float(end[0]) - float(start[0]), float(end[2]) - float(start[2])
    px, pz = float(point[0]) - float(start[0]), float(point[2]) - float(start[2])
    length2 = sx * sx + sz * sz
    t = 0.0 if length2 <= 1e-9 else max(0.0, min(1.0, (px * sx + pz * sz) / length2))
    return math.hypot(px - sx * t, pz - sz * t) <= width


def check_move(s, move, now: float | None = None):
    """None when `move` may be accepted, else the reason it may not.

    Updates the session's movement budget and allowance; call with flow_lock."""

    now = time.perf_counter() if now is None else now
    previous = s.gameplay_last_move
    frame, navigation = _navigation(s)
    new_wire = move.relative_position
    if previous is None:
        # The first report of a map: the spawn point, if the server knows it.
        if frame is None or s.gameplay_spawn_position is None:
            s.gameplay_move_budget = MAX_PLAYER_SPEED * BURST_SECONDS
            return None
        previous_wire = frame.world_to_wire(s.gameplay_spawn_position)
        elapsed = BURST_SECONDS
    else:
        previous_wire = previous.relative_position
        delta = (int(move.start_tick) - int(previous.start_tick)) & 0xFFFFFFFF
        # Out of order (older tick): no time has passed.
        elapsed = 0.0 if delta >= 0x80000000 else min(MAX_ELAPSED_SECONDS, delta / GAME_TICKS_PER_SECOND)
    distance = _flat_distance(previous_wire, new_wire)

    allowance = s.gameplay_movement_allowance
    if allowance is not None and now > allowance[2]:
        allowance = s.gameplay_movement_allowance = None
    if allowance is not None:
        origin_wire, destination_wire, _expires = allowance
        if _on_corridor(new_wire, origin_wire, destination_wire, ALLOWANCE_CORRIDOR):
            # Along the skill's own path (it was checked when cast).
            if _flat_distance(new_wire, destination_wire) <= ALLOWANCE_CORRIDOR:
                s.gameplay_movement_allowance = None
            s.gameplay_move_budget = MAX_PLAYER_SPEED * BURST_SECONDS
            return None

    capacity = MAX_PLAYER_SPEED * BURST_SECONDS
    budget = min(capacity, getattr(s, "gameplay_move_budget", capacity) + MAX_PLAYER_SPEED * elapsed)
    if distance > budget + POSITION_TOLERANCE:
        s.gameplay_move_budget = budget
        return f"moved {distance:.2f} m with {budget:.2f} m of running allowed"
    if navigation is not None and distance > 1e-3:
        if not walkable_path(navigation, frame.wire_to_world(previous_wire), frame.wire_to_world(new_wire)):
            s.gameplay_move_budget = budget
            return "off the walkable ground or through a wall"
    s.gameplay_move_budget = max(0.0, budget - distance)
    return None


def clamp_movement_destination(s, geometry, origin_wire, target_world):
    """A movement skill's destination (world), within its AttackRange and on
    walkable ground reachable from the caster; records the allowance."""

    frame, navigation = _navigation(s)
    if frame is None:
        return target_world
    origin_world = frame.wire_to_world(origin_wire)
    reach = max(0.0, float(geometry.attack_range)) + RANGE_TOLERANCE
    dx, dz = float(target_world[0]) - origin_world[0], float(target_world[2]) - origin_world[2]
    length = math.hypot(dx, dz)
    if length > reach > 0.0:
        k = reach / length
        target_world = (origin_world[0] + dx * k, float(target_world[1]), origin_world[2] + dz * k)
        length = reach
    destination = target_world
    if navigation is not None:
        # The furthest point of the line, from the target back to the caster, on
        # walkable ground in the caster's component (a teleport may pass over an
        # obstacle, not into a closed room or the void).
        destination = origin_world
        candidates = [target_world] + [
            (
                origin_world[0] + (target_world[0] - origin_world[0]) * f,
                origin_world[1] + (target_world[1] - origin_world[1]) * f,
                origin_world[2] + (target_world[2] - origin_world[2]) * f,
            )
            for f in (i * SEGMENT_STEP / length for i in range(int(length / SEGMENT_STEP), 0, -1))
        ] if length > 1e-6 else []
        for point in candidates:
            try:
                if navigation.is_reachable(origin_world, point, max_horizontal_snap=NAVMESH_SNAP):
                    destination = navigation.project_position(point, max_horizontal_distance=NAVMESH_SNAP)
                    break
            except (NavigationError, ValueError):
                continue
        # Keep the caster's height reference (PathEngine and actor heights differ
        # by a per-map offset): only X/Z come from the mesh.
        destination = (destination[0], float(target_world[1]), destination[2])
    s.gameplay_movement_allowance = (
        origin_wire, frame.world_to_wire(destination), time.perf_counter() + ALLOWANCE_SECONDS,
    )
    return destination
