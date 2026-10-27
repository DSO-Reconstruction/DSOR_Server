"""Pure skill planning over entity snapshots, independent of health storage.

Adapters decide which actors are hostile/visible and supply world coordinates.
The planner owns geometry, projectile topology and timing for every actor kind.
"""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class CombatEntity:
    actor_id: int
    position: tuple[float, float, float]
    collision_radius: float
    visible_position: tuple[float, float, float] | None = None
    # Optional bounded movement contact supplied by a replication adapter.
    ray_contact: tuple[float, float] | None = None
    impact_count: int = 1
    # Actor-targeted melee uses the navigation footprint, not the bullet capsule.
    target_radius: float | None = None
    # A replication adapter can supply the complete projectile/target contact
    # radius, including the same movement tolerance used at impact time.
    projectile_contact_radius: float | None = None
    # Positions the viewer may still be drawing (lag compensation), newest
    # first. Directional (angle/cone) skills accept any of them.
    recent_positions: tuple = ()

    @property
    def observed_position(self):
        return self.visible_position or self.position


@dataclass(frozen=True)
class EntityImpact:
    target: CombatEntity
    projectile_index: int
    distance: float
    travel_distance: float
    delay: float
    validate_trajectory: bool = False

    def scheduling_tuple(self):
        return (self.target, self.projectile_index, self.distance,
                self.travel_distance, self.delay, self.validate_trajectory)


@dataclass(frozen=True)
class EntitySkillPlan:
    geometry: object
    impacts: tuple[EntityImpact, ...]
    emit_delay: float = 0.0
    projectile_velocity: float = 0.0
    forward_offset: float = 0.0


def distance(a, b):
    return math.dist(a[:3], b[:3])


def _contact(entity, origin, aim, maximum_range):
    if entity.ray_contact is not None:
        return entity.ray_contact
    dx, dz = entity.position[0] - origin[0], entity.position[2] - origin[2]
    forward = min(maximum_range, max(0.0, dx * aim[0] + dz * aim[1]))
    return forward, math.hypot(dx - forward * aim[0], dz - forward * aim[1])


def _directional_width(geometry, forward, reach):
    if geometry.targeting_type.casefold() == "angle":
        return max(0.0, forward) * math.tan(math.radians(max(0.0, geometry.angle_degrees) / 2))
    progress = min(1.0, max(0.0, forward) / max(reach, 1e-6))
    return (geometry.player_width + (geometry.end_width - geometry.player_width) * progress) / 2


def shifted_skill_phases(geometry, attack_speed=1.0):
    """Client frame counts for the cast phase and following ground impact."""
    speed = max(0.1, min(10.0, attack_speed)) if geometry.scale_skill_timing else 1.0
    release = max(1, round(geometry.loop_start_frame * geometry.skill_timing_multiplier / speed))
    # Long falling impacts retain their authored duration, independently of
    # casting speed. These are the two phases serialized by ShiftedSkill.
    impact_speed = speed if geometry.hit_frame < 25 else 1.0
    impact = max(1, round(geometry.hit_frame * geometry.skill_timing_multiplier / impact_speed))
    return release, impact


def projectile_emit_delay(geometry, attack_speed=1.0):
    """Wait for the authored release phase before starting projectile flight.

    Ranged skills commonly leave BulletEmitDelay at zero and put their draw
    phase in LoopStartFrame (Hunting Arrow: 5 frames at 25 Hz). Both fields
    describe release from cast start, so use the later one, as for monsters.
    Attack speed scales this phase; it does not scale world-space velocity.
    """
    speed_scale = max(0.1, min(10.0, attack_speed)) if geometry.scale_skill_timing else 1.0
    return max(geometry.bullet_emit_delay, geometry.loop_start_frame / 25.0) * (
        geometry.skill_timing_multiplier / speed_scale
    )


# Bullets circling their caster or a point never travel along a line of sight.
ORBITING_MOTION_TYPES = frozenset({"orbitingactor", "orbitinglocation"})


def plan_entity_skill(geometry, entities, *, origin, direction,
                     projectile, explicit_targets=(), point=None,
                     attack_speed=1.0, impact_delay=0.0, area_radius=0.0,
                     require_explicit_target=False, range_tolerance=0.6,
                     static_reach=None):
    """Produce one ordered impact plan across all eligible entity types.

    ``static_reach(start, end)`` returns how far a bullet flies from ``start``
    towards ``end`` (both feet positions) before the map's static collision
    stops it. It applies to bullets with ``static_collision`` only.
    """
    entities = tuple({e.actor_id: e for e in entities}.values())
    explicit = tuple(dict.fromkeys(int(v) for v in explicit_targets))
    length = math.hypot(direction[0], direction[2])
    aim = (direction[0] / length, direction[2] / length) if length > 1e-6 else (0.0, 0.0)
    maximum_range = max(geometry.attack_range, geometry.projectile_range) if projectile else geometry.attack_range
    # Flat, as the client clamps the aimed point (skills.rs) and the radius
    # test below measures: a meteor at full range onto ground a few metres
    # higher or lower than the caster came back with an empty plan.
    if point is not None and maximum_range > 0 and math.hypot(
            origin[0] - point[0], origin[2] - point[2]) > maximum_range + range_tolerance:
        return EntitySkillPlan(geometry, ())
    radius = max(0.25, geometry.hit_range, geometry.impact_radius, area_radius)
    targeting = geometry.targeting_type.casefold()
    if not projectile:
        candidates = []
        for entity in entities:
            position = entity.observed_position
            size = entity.collision_radius
            if targeting == "actor" and explicit:
                if entity.actor_id not in explicit:
                    continue
                if geometry.skill_type.casefold() == "meleetarget":
                    if entity.target_radius is not None:
                        size = max(0.0, entity.target_radius)
                    # A submitted walking sample can lag the authoritative
                    # position. Accept either endpoint, as projectile contact
                    # does, without inventing hits along the intervening path.
                    reach = min(distance(origin, position), distance(origin, entity.position))
                else:
                    reach = distance(origin, position)
                if geometry.attack_range > 0 and reach > geometry.attack_range + size:
                    continue
            elif targeting in {"angle", "cone"}:
                if targeting == "cone" and entity.target_radius is not None:
                    # The client's cone test (SkillUpdateTask
                    # HandleConeTargetSkill, 0x140a3dd80) widens by the
                    # ActorShapeId radius, not the 0.3 physics capsule.
                    size = max(size, entity.target_radius)
                # The client swung at the monster where it drew it: the
                # delivered sample, the authoritative position, or the path
                # published since (Scatter Shot visibly hit monsters walking
                # across its cone and the server judged one stale sample).
                reach = None
                for sample in (position, entity.position, *entity.recent_positions):
                    dx, dz = sample[0] - origin[0], sample[2] - origin[2]
                    sample_reach = math.hypot(dx, dz)
                    forward = dx * aim[0] + dz * aim[1]
                    if forward < -size or sample_reach > radius + size:
                        continue
                    width = _directional_width(geometry, forward, radius)
                    if width <= 0 and geometry.angle_degrees > 0:
                        width = max(0.0, forward) * math.tan(math.radians(geometry.angle_degrees / 2))
                    if abs(dx * aim[1] - dz * aim[0]) > max(0.0, width) + size:
                        continue
                    reach = sample_reach if reach is None else min(reach, sample_reach)
                if reach is None:
                    continue
            elif targeting == "actor":
                continue
            else:
                # Flat, as the angle and cone shapes measure: in 3D a monster
                # standing a little higher than the caster fell out of a radius
                # skill that the arc of another still reached ("avec le bloody
                # swing je touche pas le mob alors qu'il est au meme endroit",
                # 2026-10-18; rageful swing, an Angle skill, hit it).
                centre = point or origin
                reach = math.hypot(position[0] - centre[0], position[2] - centre[2])
                if reach > radius + size:
                    continue
            candidates.append((reach, entity.actor_id, entity))
        return EntitySkillPlan(geometry, tuple(
            EntityImpact(entity, 0, reach, 0.0, impact_delay)
            for reach, _actor_id, entity in sorted(candidates)
        ))

    speed_scale = max(0.1, min(10.0, attack_speed)) if geometry.scale_skill_timing else 1.0
    emit = projectile_emit_delay(geometry, attack_speed)
    velocity = geometry.projectile_velocity / max(0.01, geometry.skill_timing_multiplier)
    offset = max(0.0, -geometry.start_offset[2])
    # ImpactRadius splashes only an exploding projectile (the dwarf's C14
    # micro-rocket and grenade: BulletRadius 0, "Multiple Enemies"). A bullet
    # with its own radius and one impact strikes one enemy: the fireball's
    # 2.35 hit every monster around its victim, though its SkillTarget is
    # "Single Enemy".
    impact_splash = (geometry.impact_radius
                     if geometry.impact_count > 1 or geometry.projectile_radius <= 0
                     else 0.0)
    splash_radius = max(impact_splash, max((
        effect.aura_radius for effect in (
            *geometry.location_status_effects, *geometry.location_visual_effects,
            *geometry.bullet_death_effects,
        ) if effect.chance >= 1.0
    ), default=0.0))
    if (static_reach is None or point is not None
            or not getattr(geometry, "static_collision", False)
            or str(getattr(geometry, "motion_type", "Straight")).casefold()
            in ORBITING_MOTION_TYPES):
        static_reach = None

    def in_sight(entity):
        if static_reach is None:
            return True
        position = entity.observed_position
        return static_reach(origin, position) + entity.collision_radius >= distance(origin, position)

    by_id = {e.actor_id: e for e in entities}
    direct = [by_id[v] for v in explicit if v in by_id and (
        maximum_range <= 0 or distance(origin, by_id[v].observed_position)
        <= maximum_range + by_id[v].collision_radius) and in_sight(by_id[v])]
    contacts = {}
    if point is not None:
        direct = sorted((e for e in entities if distance(point, e.observed_position)
                         <= max(radius, splash_radius) + e.collision_radius),
                        key=lambda e: (distance(point, e.observed_position), e.actor_id))
    elif not direct and not require_explicit_target:
        candidates = []
        if length > 1e-6 and maximum_range > 0:
            # The aimed bullet stops at the first wall along its ray.
            wall = (None if static_reach is None or targeting in {"angle", "cone"}
                    else static_reach(origin, (origin[0] + aim[0] * maximum_range, origin[1],
                                               origin[2] + aim[1] * maximum_range)))
            for entity in entities:
                size = entity.collision_radius + max(0.25, geometry.projectile_radius, geometry.player_width / 2)
                if targeting not in {"angle", "cone"} and entity.projectile_contact_radius is not None:
                    size = entity.projectile_contact_radius
                forward, miss = _contact(entity, origin, aim, maximum_range)
                contacts[entity.actor_id] = forward
                if wall is not None and forward - entity.collision_radius > wall:
                    continue
                if targeting in {"angle", "cone"}:
                    dx, dz = entity.position[0] - origin[0], entity.position[2] - origin[2]
                    forward = dx * aim[0] + dz * aim[1]
                    miss = abs(dx * aim[1] - dz * aim[0])
                    if forward < 0 or math.hypot(dx, dz) > maximum_range + size:
                        continue
                    if miss > max(0.0, _directional_width(geometry, forward, maximum_range)) + size:
                        continue
                elif miss > size:
                    continue
                if wall is None and not in_sight(entity):
                    continue
                candidates.append((forward, miss, entity.actor_id, entity))
        direct = [row[3] for row in sorted(candidates)]
    count = max(geometry.impact_count, direct[0].impact_count if direct else 1)
    direct = direct if point is not None else direct[:count]
    impacts = []
    for projectile_index in range(geometry.bullet_count):
        seen = set()
        for target in direct:
            if target.actor_id in seen:
                continue
            target_position = target.observed_position
            reach = distance(origin, target_position)
            # Directional area/charge skills are selected by their authored
            # cone/angle footprint above. Revalidating them as a zero-width
            # projectile ray at impact discards valid off-center targets and
            # produces the visible-hit/no-damage symptom. Their area check is
            # authoritative at cast time; straight projectiles keep the
            # impact-time movement validation below.
            validate = (
                point is None
                and not explicit
                and targeting not in {"angle", "cone"}
            )
            travel = max(0.0, (
                distance(origin, point) if point is not None else
                contacts.get(target.actor_id, reach) if validate else reach
            ) - offset - (target.collision_radius if point is None else 0.0) - max(0.0, geometry.projectile_radius))
            delay = emit + (travel / velocity if velocity > 1e-6 else
                            geometry.hit_frame / 25 * geometry.skill_timing_multiplier / speed_scale)
            impacts.append(EntityImpact(target, projectile_index, reach, travel, delay, validate))
            seen.add(target.actor_id)
            if point is not None or splash_radius <= 0:
                continue
            splash = sorted((e for e in entities if e.actor_id not in seen and
                             distance(target_position, e.position) <= splash_radius + e.collision_radius),
                            key=lambda e: (distance(target_position, e.position), e.actor_id))
            for entity in splash:
                seen.add(entity.actor_id)
                impacts.append(EntityImpact(entity, projectile_index, distance(origin, entity.position), travel, delay))
    if geometry.reproduction_count and impacts:
        used = {e.actor_id for e in direct}
        parent, delay = direct[0], impacts[0].delay
        for index in range(geometry.reproduction_count):
            candidates = [(distance(parent.position, e.position), e.actor_id, e)
                          for e in entities if e.actor_id not in used and (
                              geometry.target_search_radius <= 0 or
                              distance(parent.position, e.position) <= geometry.target_search_radius)]
            if not candidates:
                break
            reach, _actor_id, target = min(candidates)
            used.add(target.actor_id)
            delay += reach / velocity if velocity > 1e-6 else 1 / 25
            impacts.append(EntityImpact(target, geometry.bullet_count + index,
                                        distance(origin, target.position),
                                        max(0.0, (delay - emit) * velocity), delay))
            parent = target
    return EntitySkillPlan(geometry, tuple(impacts), emit, velocity, offset)


def impact_status_effects(geometry, *, projectile, additional=()):
    """Assemble the authored receiver effects before trigger/immunity policy."""
    effects = (*geometry.victim_status_effects, *geometry.location_status_effects)
    if projectile:
        effects += (*geometry.bullet_location_effects, *geometry.bullet_death_effects)
    return tuple({effect.resource_id: effect for effect in (*effects, *additional)}.values())


def scaled_skill_damage(base, coefficient, *, mitigation=1.0, critical=1.0, block=1.0):
    """Shared direct-hit arithmetic; adapters supply target defenses/rolls."""
    if coefficient <= 0.0:
        return 0
    return max(1, math.ceil(base * coefficient * mitigation * critical * block))
