"""Database-authored player projectile trail scheduling."""

from __future__ import annotations

from dso_server.gameplay.combat_entities import projectile_emit_delay
from dso_server.runtime.context import *
from dso_server.runtime.transport import Session


class PlayerTrailService:
    """Project terrain-following world auras behind player projectiles."""

    @staticmethod
    def _schedule_player_bullet_trail(
        s: Session,
        combat,
        encounter,
        skill,
        client_skill,
        position,
        source_actor_id: int,
        trail_specs,
        player_damage_range: tuple[float, float],
        *, attack_speed_multiplier: float = 1.0, modifier_sources=(),
        cast_started_at: float | None = None,
    ) -> None:
        if not trail_specs or not trail_specs[-2]:
            return
        (
            trail_rate,
            trail_start_delay,
            trail_range,
            trail_velocity,
            trail_definitions,
            trail_receiver_effects,
        ) = trail_specs
        geometry = combat._load_player_skill_geometry(skill.skill_index)
        if geometry is None:
            return
        if modifier_sources:
            geometry = combat._modified_player_skill_geometry(geometry, modifier_sources)
        release_delay = projectile_emit_delay(geometry, attack_speed_multiplier)
        timing = max(0.01, geometry.skill_timing_multiplier)
        velocity = float(trail_velocity) / timing
        if velocity <= 1e-6:
            return
        # Trail samples are timed from projectile release, not cast start.
        start_delay = max(0.0, float(trail_start_delay)) * timing
        interval = max(0.02, float(trail_rate)) * timing
        forward_offset = max(0.0, -geometry.start_offset[2])
        cast_started_at = time.perf_counter() if cast_started_at is None else cast_started_at
        origin_world = encounter.coordinate_frame.wire_to_world(position)
        direction_length = math.hypot(
            float(skill.direction[0]), float(skill.direction[2])
        )
        if direction_length <= 1e-6 or trail_range <= 0.0:
            return
        direction_x = float(skill.direction[0]) / direction_length
        direction_z = float(skill.direction[2]) / direction_length
        maximum_distance = float(trail_range)
        if isinstance(client_skill, TargetPointBulletSkill):
            maximum_distance = min(
                maximum_distance,
                math.dist(origin_world, client_skill.target_position),
            )
        flight_duration = max(0.0, maximum_distance - forward_offset) / velocity
        if start_delay > flight_duration:
            return
        count = min(64, int(math.floor((flight_duration - start_delay) / interval)) + 1)
        definitions = tuple(
            replace(effect, delay_ticks=0) for effect in trail_definitions
        )
        for trail_index in range(count):
            flight_time = start_delay + interval * trail_index
            distance = forward_offset + velocity * flight_time
            candidate = (
                origin_world[0] + direction_x * distance,
                origin_world[1],
                origin_world[2] + direction_z * distance,
            )
            trail_position = encounter.navigation.project_position(candidate)
            CharacterService._schedule_player_location_visuals(
                s,
                combat,
                definitions,
                position=trail_position,
                yaw=float(skill.aim_angle),
                server_tick=int(skill.server_tick),
                owner_actor_id=int(source_actor_id),
                cast_id=(
                    (int(skill.cast_id) * 67 + trail_index) & 0xFFFFFFFF
                ),
                delay_seconds=release_delay + flight_time,
                cast_started_at=cast_started_at,
                receiver_status_effects=trail_receiver_effects,
                source_skill_index=int(skill.skill_index),
                player_damage_range=player_damage_range,
            )
