"""Tick-driven player movement relay: one move batch per viewer per tick.

Each accepted MoveCommand used to be forwarded to every nearby player the
moment it arrived, as its own datagram, and echoed to its sender the same
way. With N players on a map every viewer received 25 x N datagrams a second
at whatever spacing the senders' networks produced, and players beyond the
60 m activity radius were never updated, so they froze and then jumped back
into view. Retail instead flushes one move batch per 40 ms tick holding the
viewer's echo and every actor due an update, refreshes far and standing
actors on a 3/6/12-tick schedule, and stamps moving segments with an 18-tick
dead-reckoning horizon and standing ones with 0 (the client's hard stop).

Composed test servers never call ``start()``, so they keep the immediate
relay in ``presence.py``; ``DSO_PRESENCE_MOVEMENT_TICK=0`` restores it in
production.
"""

from __future__ import annotations

import math

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.services.base import CharacterService


PRESENCE_MOVEMENT_TICK_ENABLED = os.getenv(
    "DSO_PRESENCE_MOVEMENT_TICK", "1"
).strip().lower() not in {"0", "false", "no", "off"}
# Players further apart than this are not relayed at all. Retail refreshes
# actors about 95 m away on its slowest (12-tick) schedule.
REMOTE_PLAYER_RELAY_RADIUS = max(
    PLAYER_ACTIVITY_VICINITY_RADIUS,
    float(os.getenv("DSO_REMOTE_PLAYER_RELAY_RADIUS", "150")),
)
# Up to this distance a moving player is relayed on every new MoveCommand.
REMOTE_PLAYER_FULL_RATE_RADIUS = max(
    0.0, float(os.getenv("DSO_REMOTE_PLAYER_FULL_RATE_RADIUS", "30"))
)
REMOTE_PLAYER_MOVE_DURATION_TICKS = 18
# (distance bound, ticks between refreshes while moving, while standing).
# A moving player whose speed, heading or moving state changes is relayed at
# the next tick whatever its tier.
_RELAY_TIERS = (
    (REMOTE_PLAYER_FULL_RATE_RADIUS, 1, 3),
    (PLAYER_ACTIVITY_VICINITY_RADIUS, 3, 6),
    (math.inf, 6, 12),
)


# Every viewer dead-reckons a remote player at (speed byte / 64) x 5.0: the
# base MovementSpeed is _Template_Player.RunSpeed (5.0 for every class) plus
# a MovementBonusSpeed base of 0, whatever the player's buffs and gear.
REMOTE_PLAYER_BASE_SPEED = 5.0
# MoveCommand::Encode writes the speed ratio as (ratio * 64) & 0xFF after
# clamping it to 4.0, so a player at 4x base speed or more (about 20 m/s and
# up) sends speed byte 0 while running, which every viewer drew as a player
# standing still between relays: a teleport on every update. A displacement in
# this band with a zero speed byte is such a wrapped move; anything faster is
# a jump (blink, teleport, knock-back) and stays a hard stop.
_WRAPPED_SPEED_RANGE = (15.0, 40.0)
_MEASURABLE_TICKS = 12


def _direction_byte(dx: float, dz: float) -> int:
    # ComputeVelocity moves along (-sin a, 0, -cos a) for byte b with
    # a = b / 256 * tau - pi.
    return int((math.atan2(-dx, -dz) + math.pi) / math.tau * 256.0) & 0xFF


def relayed_player_move(move: Move, previous: Move | None = None) -> Move:
    """Return the form other clients receive for a player's own Move.

    The client stamps every Move with duration 20, standing or not. Moving
    segments get the retail 18-tick horizon and standing ones 0, the client's
    hard stop. ``previous`` is the player's last distinct earlier Move; with
    it, a wrapped zero speed byte is replaced by the measured speed.
    """

    scalar, direction = move.scalar_byte, move.yaw_byte
    if not scalar and previous is not None:
        ticks = (move.start_tick - previous.start_tick) & 0xFFFFFFFF
        if 0 < ticks <= _MEASURABLE_TICKS:
            dx = (move.quantized_x - previous.quantized_x) / 128.0
            dz = (move.quantized_z - previous.quantized_z) / 128.0
            speed = math.hypot(dx, dz) / (ticks / SERVER_TICKS_PER_SECOND)
            if _WRAPPED_SPEED_RANGE[0] <= speed <= _WRAPPED_SPEED_RANGE[1]:
                scalar = min(255, round(64.0 * speed / REMOTE_PLAYER_BASE_SPEED))
                measured = _direction_byte(dx, dz)
                # Keep the client's heading unless it plainly disagrees with
                # where the player went (more than 45 degrees).
                if abs(((direction - measured + 128) & 0xFF) - 128) > 32:
                    direction = measured
    duration = REMOTE_PLAYER_MOVE_DURATION_TICKS if scalar else 0
    if (scalar, direction, duration) == (
        move.scalar_byte, move.yaw_byte, move.duration_ticks
    ):
        return move
    return replace(
        move, scalar_byte=scalar, yaw_byte=direction, duration_ticks=duration
    )


def _relay_intervals(distance: float) -> tuple[int, int]:
    for bound, moving_every, standing_every in _RELAY_TIERS:
        if distance <= bound:
            return moving_every, standing_every
    return _RELAY_TIERS[-1][1:]


class PresenceMovementService:
    """Publish player movement on the 25 Hz server tick."""

    def _start_presence_movement_publisher(self) -> bool:
        if not PRESENCE_MOVEMENT_TICK_ENABLED:
            return False
        if getattr(self, "_presence_movement_thread", None) is not None:
            return False
        self._presence_movement_publisher_running = True
        self._presence_movement_thread = threading.Thread(
            target=self._presence_movement_loop,
            name=f"{self.service_name}-movement-tick",
            daemon=True,
        )
        self._presence_movement_thread.start()
        print(
            f"[{self.service_name}] player movement relayed on the "
            f"{SERVER_TICKS_PER_SECOND:.0f} Hz tick"
        )
        return True

    def _presence_movement_loop(self) -> None:
        interval = 1.0 / SERVER_TICKS_PER_SECOND
        stop = self._stop_event
        failures = 0
        last_failure_log = 0.0
        deadline = time.perf_counter()
        try:
            while not stop.is_set():
                deadline += interval
                try:
                    self._publish_presence_movement(
                        int(server_time() // 40) & 0xFFFFFFFF
                    )
                    failures = 0
                except Exception as exc:
                    failures += 1
                    now = time.perf_counter()
                    if failures == 1 or now - last_failure_log >= 5.0:
                        last_failure_log = now
                        print(
                            f"[{self.service_name}] movement tick failed "
                            f"({failures} consecutive): "
                            f"{type(exc).__name__}: {exc}"
                        )
                delay = deadline - time.perf_counter()
                if delay < -interval:
                    # Never replay missed ticks; the next one carries the
                    # newest state anyway.
                    deadline = time.perf_counter()
                elif delay > 0:
                    # time.sleep is a high-resolution wait on Windows since
                    # Python 3.11; Event.wait would round up to the system
                    # timer period.
                    time.sleep(delay)
        finally:
            self._presence_movement_publisher_running = False
            for session in tuple(self.sessions.values()):
                session.gameplay_move_echo_deferred = False

    def _publish_presence_movement(self, tick: int) -> int:
        """Send every viewer its due player moves for one tick.

        Returns the number of datagrams sent. Runs on the movement thread
        only, which is the single writer of every viewer's relay state.
        """

        groups = {}
        for session in self._active_gameplay_sessions():
            key = (
                session.gameplay_map,
                CharacterService._gameplay_instance_key(session),
            )
            groups.setdefault(key, []).append(session)
        # actor id -> (latest Move seen, the distinct Move before it), so a
        # wrapped speed byte can be measured from consecutive positions.
        history = getattr(self, "_presence_movement_history", None)
        if history is None:
            history = self._presence_movement_history = {}
        seen = set()
        sent = 0
        for members in groups.values():
            entries = []
            for session in members:
                character = session.gameplay_character
                # One attribute read of an immutable Move needs no flow_lock.
                # Taking it made this one thread, which serves every player,
                # wait out any long handler holding a single session's lock.
                move = session.gameplay_last_move
                if move is None or character is None:
                    continue
                actor_id = int(
                    session.gameplay_remote_actor_id
                    or CharacterService._remote_actor_id(character.character_id)
                )
                latest, previous = history.get(actor_id, (None, None))
                if latest is not None and latest.start_tick != move.start_tick:
                    previous = latest
                history[actor_id] = (move, previous)
                seen.add(actor_id)
                entries.append(
                    (session, actor_id, move, relayed_player_move(move, previous))
                )
            for viewer, _actor_id, viewer_move, _relayed in entries:
                sent += CharacterService._publish_viewer_movement(
                    viewer, viewer_move, entries, tick
                )
        for actor_id in tuple(history):
            if actor_id not in seen:
                del history[actor_id]
        return sent

    @staticmethod
    def _publish_viewer_movement(
            viewer: Session, viewer_move: Move, entries, tick: int) -> int:
        state = viewer.gameplay_remote_move_relay
        if state is None:
            state = viewer.gameplay_remote_move_relay = {}
        # From the first tick this viewer is covered, its own echo travels in
        # the batch as well instead of as a datagram per received move.
        viewer.gameplay_move_echo_deferred = True
        commands = []
        commits = []
        if viewer.gameplay_move_echo_sent is not viewer_move:
            commands.append(MoveServerCommand(viewer_move, LOCAL_PLAYER_ACTOR_ID))
            commits.append(None)
        origin = viewer_move.relative_position
        present = set()
        for source, actor_id, move, relayed in entries:
            if source is viewer:
                continue
            position = move.relative_position
            distance = math.hypot(position[0] - origin[0], position[2] - origin[2])
            if distance > REMOTE_PLAYER_RELAY_RADIUS:
                continue
            present.add(actor_id)
            moving_every, standing_every = _relay_intervals(distance)
            motion = (relayed.scalar_byte, relayed.yaw_byte, relayed.pitch_byte)
            last = state.get(actor_id)
            if last is None:
                due = True
            else:
                last_start, last_tick, last_motion, last_position = last
                elapsed = (tick - last_tick) & 0xFFFFFFFF
                if relayed.scalar_byte:
                    due = relayed.start_tick != last_start and (
                        elapsed >= moving_every or motion != last_motion
                    )
                else:
                    due = (
                        motion != last_motion
                        or position != last_position
                        or elapsed >= standing_every
                    )
            if due:
                commands.append(MoveServerCommand(relayed, actor_id))
                commits.append(
                    (actor_id, (relayed.start_tick, tick, motion, position))
                )
        for actor_id in tuple(state):
            if actor_id not in present:
                # Out of range or gone: start afresh when it is back.
                del state[actor_id]
        sent = 0
        for offset in range(0, len(commands), MONSTER_MOVEMENT_PACKET_BUDGET):
            batch = commands[offset:offset + MONSTER_MOVEMENT_PACKET_BUDGET]
            if viewer.send_unreliable_sequenced(
                encode_move_batch(MoveBatch(tuple(batch))),
                label="player-movement",
                skip_if_busy=True,
            ) is None:
                # Busy outbound: retry these on the next tick.
                break
            sent += 1
            for commit in commits[offset:offset + MONSTER_MOVEMENT_PACKET_BUDGET]:
                if commit is None:
                    viewer.gameplay_move_echo_sent = viewer_move
                else:
                    state[commit[0]] = commit[1]
        return sent
