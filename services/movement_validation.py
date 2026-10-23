"""Server-side validation for player movement and movement skills."""

from __future__ import annotations

import dataclasses
import math
import time

from dso_server.gameplay.pathengine_navigation import (
    NavigationError,
    PathEngineNavigationMesh,
)


# The client's MovementSpeed cap is 13. Mounts and buffs can add a bonus, so
# leave headroom to avoid correcting legitimate movement.
MAX_PLAYER_SPEED = 13.0 * 1.6
BURST_SECONDS = 1.5
POSITION_TOLERANCE = 1.0
NAVMESH_SNAP = 1.5
SEGMENT_STEP = 0.5
SEGMENT_SNAP = 0.9
CORRECTION_INTERVAL = 0.3
GAME_TICKS_PER_SECOND = 25.0
MAX_ELAPSED_SECONDS = 2.0

MOVEMENT_SKILL_TYPES = frozenset({"teleport", "jump", "charge", "chargethrough"})
ALLOWANCE_SECONDS = 3.0
ALLOWANCE_CORRIDOR = 2.0
RANGE_TOLERANCE = 1.0


def non_finite(value) -> bool:
    """Return whether a decoded command contains NaN or infinity."""

    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, (tuple, list)):
        return any(non_finite(item) for item in value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return any(non_finite(getattr(value, item.name)) for item in dataclasses.fields(value))
    return False


def _flat_distance(a, b) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[2]) - float(b[2]))


def _navigation(session):
    combat = session.gameplay_combat_state
    if combat is None:
        return None, None
    encounter = getattr(combat, "encounter", None)
    if encounter is None:
        return None, None
    navigation = getattr(encounter, "navigation", None)
    if not isinstance(navigation, PathEngineNavigationMesh):
        navigation = None
    return getattr(encounter, "coordinate_frame", None), navigation


def walkable_path(navigation, start_world, end_world) -> bool:
    """Require a walkable destination, connected ground, and a clear segment."""

    try:
        navigation.project_position(end_world, max_horizontal_distance=NAVMESH_SNAP)
        # Every authored connection: the client walks the whole connected
        # ground, not the 1 m-portal components monsters route over.
        if not navigation.is_reachable(
            start_world, end_world, max_horizontal_snap=NAVMESH_SNAP,
            all_connections=True,
        ):
            return False
        length = _flat_distance(start_world, end_world)
        steps = max(1, math.ceil(length / SEGMENT_STEP))
        for index in range(1, steps + 1):
            fraction = index / steps
            point = tuple(
                float(start_world[axis])
                + (float(end_world[axis]) - float(start_world[axis])) * fraction
                for axis in range(3)
            )
            navigation.project_position(
                point, max_horizontal_distance=SEGMENT_SNAP
            )
    except (NavigationError, ValueError, TypeError, IndexError):
        return False
    return True


def _on_corridor(point, start, end, width) -> bool:
    """Return whether point lies within width of the X/Z segment."""

    sx, sz = float(end[0]) - float(start[0]), float(end[2]) - float(start[2])
    px, pz = float(point[0]) - float(start[0]), float(point[2]) - float(start[2])
    length_squared = sx * sx + sz * sz
    fraction = (
        0.0
        if length_squared <= 1e-9
        else max(0.0, min(1.0, (px * sx + pz * sz) / length_squared))
    )
    return math.hypot(px - sx * fraction, pz - sz * fraction) <= width


def check_move(session, move, now: float | None = None):
    """Return None for an allowed move, otherwise a concise refusal reason.

    Call while holding the session's flow lock.
    """

    now = time.perf_counter() if now is None else float(now)
    previous = session.gameplay_last_move
    frame, navigation = _navigation(session)
    new_wire = move.relative_position
    capacity = MAX_PLAYER_SPEED * BURST_SECONDS

    if previous is None:
        if frame is None or session.gameplay_spawn_position is None:
            session.gameplay_move_budget = capacity
            return None
        previous_wire = frame.world_to_wire(session.gameplay_spawn_position)
        elapsed = BURST_SECONDS
    else:
        previous_wire = previous.relative_position
        tick_delta = (int(move.start_tick) - int(previous.start_tick)) & 0xFFFFFFFF
        elapsed = (
            0.0
            if tick_delta >= 0x80000000
            else min(MAX_ELAPSED_SECONDS, tick_delta / GAME_TICKS_PER_SECOND)
        )

    distance = _flat_distance(previous_wire, new_wire)
    allowance = session.gameplay_movement_allowance
    if allowance is not None and now > allowance[2]:
        allowance = session.gameplay_movement_allowance = None
    if allowance is not None:
        origin_wire, destination_wire, _expires = allowance
        if _on_corridor(new_wire, origin_wire, destination_wire, ALLOWANCE_CORRIDOR):
            if _flat_distance(new_wire, destination_wire) <= ALLOWANCE_CORRIDOR:
                session.gameplay_movement_allowance = None
            session.gameplay_move_budget = capacity
            return None

    budget = min(
        capacity,
        float(getattr(session, "gameplay_move_budget", capacity))
        + MAX_PLAYER_SPEED * elapsed,
    )
    if distance > budget + POSITION_TOLERANCE:
        session.gameplay_move_budget = budget
        return f"moved {distance:.2f} m with {budget:.2f} m of running allowed"
    if navigation is not None and distance > 1e-3:
        if frame is None or not walkable_path(
            navigation,
            frame.wire_to_world(previous_wire),
            frame.wire_to_world(new_wire),
        ):
            session.gameplay_move_budget = budget
            return "off the walkable ground or through a wall"

    # Keep tolerance debt so a client cannot spend the free positional slack
    # repeatedly on every tick to exceed the speed cap.
    session.gameplay_move_budget = budget - distance
    return None


def clamp_movement_destination(session, geometry, origin_wire, target_world):
    """Clamp a movement skill to its range and reachable walkable ground."""

    frame, navigation = _navigation(session)
    if frame is None:
        return target_world
    origin_world = frame.wire_to_world(origin_wire)
    reach = max(0.0, float(geometry.attack_range)) + RANGE_TOLERANCE
    dx = float(target_world[0]) - origin_world[0]
    dz = float(target_world[2]) - origin_world[2]
    length = math.hypot(dx, dz)
    if length > reach > 0.0:
        scale = reach / length
        target_world = (
            origin_world[0] + dx * scale,
            float(target_world[1]),
            origin_world[2] + dz * scale,
        )
        length = reach

    destination = target_world
    if navigation is not None:
        destination = origin_world
        candidates = [target_world]
        if length > 1e-6:
            steps = int(length / SEGMENT_STEP)
            candidates.extend(
                tuple(
                    origin_world[axis]
                    + (target_world[axis] - origin_world[axis])
                    * (step * SEGMENT_STEP / length)
                    for axis in range(3)
                )
                for step in range(steps, 0, -1)
            )
        for point in candidates:
            try:
                if navigation.is_reachable(
                    origin_world, point, max_horizontal_snap=NAVMESH_SNAP,
                    all_connections=True,
                ):
                    projected = navigation.project_position(
                        point, max_horizontal_distance=NAVMESH_SNAP
                    )
                    destination = (
                        projected[0], float(target_world[1]), projected[2]
                    )
                    break
            except (NavigationError, ValueError, TypeError, IndexError):
                continue

    return destination


def record_movement_allowance(session, origin_wire, destination_world) -> None:
    """Allow the path of an accepted movement skill once, for a short time."""

    frame, _navigation_mesh = _navigation(session)
    if frame is None:
        return
    session.gameplay_movement_allowance = (
        origin_wire,
        frame.world_to_wire(destination_world),
        time.perf_counter() + ALLOWANCE_SECONDS,
    )
