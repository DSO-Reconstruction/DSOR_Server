"""Authoritative shared-map combat state for local PvE encounters."""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
import math
import os
import random
from pathlib import Path
import sqlite3
import struct
import threading
import time

from dso_server.runtime.concurrency import InstrumentedRLock, MAP_COMBAT_DOMAIN
from dso_server.gameplay.navigation_shapes import monster_target_radii
from dso_server.gameplay.combat_entities import (
    CombatEntity,
    ORBITING_MOTION_TYPES,
    plan_entity_skill,
    projectile_emit_delay,
    shifted_skill_phases,
    impact_status_effects,
    scaled_skill_damage,
)

from dso_server.protocol.combat_protocol import (
    BULLET_SKILL_COMMAND,
    SHIFTED_SKILL_COMMAND,
    SKILL_COMMAND,
    SUSTAINED_SKILL_COMMAND,
    TARGET_BULLET_SKILL_COMMAND,
    TARGET_SKILL_COMMAND,
    BulletSkill,
)
from dso_server.protocol.gameplay_protocol import (
    NewMonsterServerCommand,
    StatusEffectServerCommand,
)
from dso_server.gameplay.local_monster_state import (
    LOCAL_MONSTER_ACTOR_BASE,
    map_monster_actor_base,
    LocalMonsterEncounter,
    MonsterAttackDefinition,
    MonsterRuntimeDefinition,
    MonsterSpawnGroup,
    load_player_summon_spawn,
)
from dso_server.gameplay.player_summon_lifecycle import (
    PlayerSummonAuraDefinition,
    SUMMON_AURA_RENEW_INTERVAL_TICKS,
    add_player_summon_aura_to_spawn,
    load_player_summon_aura,
    player_summon_aura_refresh_command,
    summon_startup_seconds,
)
from dso_server.gameplay.map_collision_terrain import CollisionTerrainError
from dso_server.gameplay.map_static_collision import (
    StaticLineOfSight,
    load_static_line_of_sight,
)
from dso_server.gameplay.fractured_planes_layout import (
    TOWER_FIRE_BURST_COOLDOWN,
    TOWER_FIRE_BURST_SHOTS,
    TOWER_FIRE_ACQUISITION_RANGE,
    is_tower_mechanism,
)
from dso_server.gameplay.pathengine_navigation import NavigationError
from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog


def _status_effects_disabled(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() not in {
        "0", "false", "no", "off", ""
    }


# Bisection switches, re-exported by ``dso_server.runtime.context``. Status effects
# are the one gameplay subsystem that can disable an actor client-side without
# the server recording anything: the client derives authored max-stack
# children locally from the stack occurrence it receives, and
# ``debuff_cc_freeze``/``debuff_cc_stun`` end in
# ``ActorFeature:off,Movement,RegularSkills``. Turning them off answers
# whether they are involved at all, which server-side logging cannot.
#
# Diagnostic, not balance settings. Damage, deaths and loot are unaffected;
# only the effect commands are suppressed.
VICTIM_STATUS_EFFECTS_DISABLED = _status_effects_disabled(
    "DSO_DISABLE_VICTIM_STATUS_EFFECTS"
)
STATUS_EFFECTS_DISABLED = _status_effects_disabled(
    "DSO_DISABLE_STATUS_EFFECTS"
)

@lru_cache(maxsize=4096)
def parse_disabled_actor_features(encoded: str) -> frozenset[str]:
    """Return the ActorFeature names an authored program switches off.

    ``StartModifiers`` text is immutable content, so this is a pure function of
    the program string and is safe to memoize process-wide. The player victim
    gates asked this question by re-splitting and re-casefolding that text for
    every active source, at the head of every MoveCommand (twice), every cast
    (twice), and once per candidate viewer on every 25 Hz monster tick. On a
    level-100 loadout that measured 0.043 ms per query -- paid by the map's
    single simulation thread once per viewer, so it scaled with player count.
    """

    disabled: set[str] = set()
    for raw in str(encoded or "").split(";"):
        kind, separator, arguments = raw.partition(":")
        if not separator or kind.strip().casefold() != "actorfeature":
            continue
        values = tuple(
            value.strip().casefold()
            for value in arguments.split(",")
            if value.strip()
        )
        if values and values[0] == "off":
            disabled.update(values[1:])
    return frozenset(disabled)


MONSTER_STOP_DISTANCE = 1.75
# DSO_BOSS_BASIC_ATTACK_ROTATION=1: a boss with a close basic hit keeps it in
# rotation and casts a ready special only every third choice (the xm1rth
# fork's tuning). Off, a ready special goes first, as for every monster.
BOSS_BASIC_ATTACK_ROTATION = os.getenv(
    "DSO_BOSS_BASIC_ATTACK_ROTATION", "0"
).strip().lower() in {"1", "true", "yes"}
# A melee blow reaches a player no more than this far above or below the
# monster's feet (metres). Range checks are flat (x/z): without it a monster on
# a ledge struck a player standing under it ("a monster on high ground attacked
# me ... but I still took the damage", 2026-10-18). 1.5 m keeps slopes and
# steps, where both feet sit on the same ground, unaffected.
MONSTER_MELEE_VERTICAL_REACH = 1.5
# A projectile's lane (from the monster's centre to its cast-time target) is
# also a height: the player is hit where the lane passes through their capsule,
# within this much above their head or below their feet.
PROJECTILE_VERTICAL_SLACK = 1.0
PLAYER_CAPSULE_HEIGHT = 2.0


def _cooldown_key(definition) -> str:
    """Skills of one CoolDownCategory share a timer; others have their own."""

    return definition.cooldown_category or definition.resource_id
RANGED_STANDOFF_RATIO = 0.80
MONSTER_HOME_ARRIVAL_DISTANCE = 0.20
# Player-owned mercenaries must not autonomously engage players in the city
# hubs, even when both players have opted into PvP. Other summon behavior is
# unchanged; this only gates player actors as targets for Shadow Souls.
MERCENARY_PVP_SAFE_MAP_IDS = frozenset({"a0200_kingscity", "b1200_hub"})
# ``_Template_StatusEffect`` row ``MonsterReturnHome`` (15 s, group "Special").
# The wire id is that table's zero-based row order, so it shifts whenever the
# table is edited. 701 is the fallback only; ``_return_home_effect_id``
# resolves the live value from the encounter's own database.
MONSTER_RETURN_HOME_EFFECT_RESOURCE_ID = "MonsterReturnHome"
MONSTER_RETURN_HOME_EFFECT_ID = 701
MONSTER_RETURN_HOME_DURATION_TICKS = 15 * 25
# A route can become temporarily unavailable after dynamic separation or a
# navigation projection failure. Returning actors are intentionally immune,
# so leaving one in that state forever creates a client-visible monster that
# can never be damaged. Bound that state independently of the authored VFX
# duration and hard-reset the actor if walking home cannot make progress.
MONSTER_RETURN_HOME_TIMEOUT_SECONDS = max(
    1.0, float(os.getenv("DSO_MONSTER_RETURN_HOME_TIMEOUT", "4.0"))
)
# Some stationary event emitters carry map-wide TravelDistance values (up to
# thousands of units). They must not turn a local monster route into a
# full-map A* search. Ordinary templates retain their smaller database value.
MONSTER_NAVIGATION_RADIUS_CAP = max(
    5.0, float(os.getenv("DSO_MONSTER_NAVIGATION_RADIUS", "40"))
)
MONSTER_SIMULATION_RADIUS = max(
    10.0, float(os.getenv("DSO_MONSTER_ACTIVE_RADIUS", "40"))
)
# Client actor registration is deliberately tighter than the server-side
# navigation/simulation envelope. Retail announces ordinary monsters just
# outside the visible camera area, then retains them for a little longer as
# the player leaves. Separate thresholds prevent register/discard thrashing.
MONSTER_VISIBILITY_ENTER_RADIUS = max(
    5.0,
    float(os.getenv("DSO_MONSTER_VISIBILITY_ENTER_RADIUS", "18.5")),
)
MONSTER_VISIBILITY_EXIT_RADIUS = max(
    MONSTER_VISIBILITY_ENTER_RADIUS,
    float(os.getenv("DSO_MONSTER_VISIBILITY_EXIT_RADIUS", "23")),
)
MONSTER_ACQUISITION_RADIUS = max(
    5.0,
    min(
        MONSTER_SIMULATION_RADIUS,
        float(os.getenv("DSO_MONSTER_ACQUISITION_RADIUS", "24")),
    ),
)
# How much further than the authored TravelDistance a monster may pursue.
# The pursuit radius is no longer a wall the goal is clamped to -- monsters
# used to walk to that sphere and stand on it, which is how packs collected on
# an invisible boundary doing nothing. It is now the distance at which a
# monster gives up and walks home.
MONSTER_CHASE_GRACE_DISTANCE = max(
    0.0, float(os.getenv("DSO_MONSTER_CHASE_GRACE_DISTANCE", "18"))
)
# Past this much beyond the pursuit radius a monster no longer walks back: it
# resets, snapping to its spawn with full health and no carried state. Bounding
# pursuit with a snap rather than a clamp is what lets monsters follow a player
# properly while still guaranteeing they cannot be dragged across the map.
MONSTER_RESET_GRACE_DISTANCE = max(
    1.0, float(os.getenv("DSO_MONSTER_RESET_GRACE_DISTANCE", "25"))
)
PLAYER_SUMMON_OWNER_RANGE = max(
    1.0,
    float(
        os.getenv(
            "DSO_PLAYER_SUMMON_OWNER_RANGE",
            str(MONSTER_SIMULATION_RADIUS),
        )
    ),
)
PLAYER_PROJECTILE_COLLISION_RADIUS = max(
    0.1, float(os.getenv("DSO_PLAYER_PROJECTILE_RADIUS", "0.6"))
)
PLAYER_PROJECTILE_HITBOX_TOLERANCE = max(
    0.0, float(os.getenv("DSO_PLAYER_HITBOX_TOLERANCE", "0.20"))
)


def _parse_skill_radius_bonuses(raw: str) -> dict[str, float]:
    """Parse ``skill=bonus;skill=bonus`` into a case-folded mapping."""

    result: dict[str, float] = {}
    for entry in str(raw or "").replace(",", ";").split(";"):
        name, separator, value = entry.partition("=")
        if not separator or not name.strip():
            continue
        try:
            result[name.strip().casefold()] = max(0.0, float(value))
        except ValueError:
            continue
    return result


# Extra collider radius, in world units, added to a player skill's authored
# bullet radius when its geometry is loaded.  Ice Missile's authored 0.25
# missed targets the client visibly clipped; a small bonus widens the server's
# sweep and impact tests for that skill alone.
PLAYER_SKILL_PROJECTILE_RADIUS_BONUS = _parse_skill_radius_bonuses(
    os.getenv("DSO_SKILL_PROJECTILE_RADIUS_BONUS", "iceball=0.15")
)
# Monster simulation follows the client's 25 Hz game clock, but retail does
# not publish a fresh transform on every one of those ticks. The 2026-09-09
# combat capture spaces ordinary moving-monster snapshots by 4-8 ticks while
# giving each MoveCommand an 18-tick interpolation window. Publishing every
# tick continually restarts that interpolation and looks like transform
# correction/teleporting rather than locomotion.
MONSTER_CHASE_INTERVAL_SECONDS = 1.0 / 25.0
MONSTER_MOVEMENT_PUBLISH_INTERVAL_TICKS = max(
    # Retail captures space ordinary snapshots by 4-8 ticks. Use the captured
    # lower bound so one replaceable frame lost to an occupied outbound queue
    # cannot create nearly half a second between client-visible transforms.
    1, int(os.getenv('DSO_MONSTER_MOVEMENT_PUBLISH_TICKS', '4'))
)
# How much further than its walking speed a published step may carry an actor
# before the client has to draw it as a jump. Slightly above 1 so ordinary
# rounding, ground snapping and route corners do not register.
MONSTER_TELEPORT_TOLERANCE = max(
    1.0, float(os.getenv('DSO_MONSTER_TELEPORT_TOLERANCE', '1.5'))
)
SERVER_TICKS_PER_SECOND = 25.0

# Khalys's flood leaves four safe circles inside its aura, (+-6, +-6) m from
# its centre in the effect's own frame, 2.16 m across their flames' ring.
# CONTRACT: a player inside one is spared the flood's strikes; the circles
# turn with the effect's yaw, as the client draws them.
# EVIDENCE (data): Boss01_b_FloodDamage and Boss01FloodDamage StartSequence
# boss_01_andermacht plays effects/boss_01_skill_03_state, whose nodes
# emitter_fire_safe1..4 sit at (-6,6) (-6,-6) (6,-6) (6,6), each an arc of
# radius 2.16 (pe2_0_g1..g4 x -2.161..2.151); the flood's lines reach 20.91
# (AuraRadius 20.84). a0404_culttower03.map: s07_deco_platform_01 (4.26 m
# square) at (86|98, 0, 2|-10) around Khalys's spawn (92.04, 0, -4.01).
# UNVERIFIED: retail's own test (no LocationEffectClearing row exists for it).
LOCATION_AURA_SAFE_CIRCLES = {
    effect: (((-6.0, 6.0), (-6.0, -6.0), (6.0, -6.0), (6.0, 6.0)), 2.16)
    for effect in ("boss01_b_flooddamage", "boss01flooddamage")
}


@lru_cache(maxsize=256)
def _ally_aura_skill_unlocks(
    static_database_path: str, map_id: str, effect_id: str,
) -> tuple[tuple[float, int, int, tuple[tuple[tuple[float, float, float], float], ...]], ...]:
    """What a caster's ally aura makes a placed helper cast at once.

    CONTRACT: (AuraRadius, skill row index, HitFrame, ((spawn, yaw), ...))
    per child UnlockHiddenSkills of an AuraTargets:Allies aura limited to an
    AuraEffectAffectedTemplateId, with that template's authored spawn points
    on the map (yaw atan2(m8, m0), as local_monster_state places monsters).
    EVIDENCE (data): boss_khalys_base_andermight_flood_activate puts
    boss_khalys_aura_activate_andermight_wave (Allies, 6 m, D 10, affects
    boss_khalys_spawner_andermight_wave) on Khalys; its child
    boss_khalys_effect_activate_andermight_wave unlocks the spawner's hidden
    add_khalys_base_andermight_flood (ForceExecution, HitFrame 1), whose
    Boss01FloodDamage OZ:2 lands at (92, 0, -4) from the spawner at (94, 0,
    -4) turned -90 degrees (e_bos_khalys_pw_boss, pw015_khalys_boss_01).
    UNVERIFIED: the spawner itself is not simulated; the flood is credited
    to the caster of the aura.
    """

    try:
        with sqlite3.connect(
            f"file:{Path(static_database_path).as_posix()}?mode=ro", uri=True
        ) as database:
            row = database.execute(
                "SELECT COALESCE(AuraTargets, ''), COALESCE(AuraRadius, 0), "
                "COALESCE(AuraEffectAffectedTemplateId, ''), "
                "COALESCE(AuraEffectId, '') FROM _Template_StatusEffect "
                "WHERE Id=?", (str(effect_id),),
            ).fetchone()
            if (
                row is None or str(row[0]).strip().casefold() != "allies"
                or float(row[1]) <= 0.0 or not str(row[2]).strip()
            ):
                return ()
            skills = []
            for child in str(row[3]).split(";"):
                program = database.execute(
                    "SELECT COALESCE(StartModifiers, '') "
                    "FROM _Template_StatusEffect WHERE Id=?",
                    (child.strip(),),
                ).fetchone()
                for encoded in str(program[0] if program else "").split(";"):
                    kind, separator, value = encoded.partition(":")
                    if separator and kind.strip().casefold() == "unlockhiddenskills":
                        skill = database.execute(
                            "SELECT rowid - 1, COALESCE(HitFrame, 0) "
                            "FROM _Template_Skill WHERE Id=?",
                            (value.strip(),),
                        ).fetchone()
                        if skill is not None:
                            skills.append((int(skill[0]), int(skill[1])))
        if not skills:
            return ()
        path = Path(static_database_path).parent.parent / "maps" / f"{map_id}.db4"
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as database:
            spawns = []
            for (transform,) in database.execute(
                "SELECT Transform FROM _Instance_SpawnPoint WHERE NpcId=?",
                (str(row[2]).strip(),),
            ):
                if not isinstance(transform, bytes) or len(transform) != 64:
                    continue
                matrix = struct.unpack("<16f", transform)
                spawns.append((
                    (float(matrix[12]), float(matrix[13]), float(matrix[14])),
                    math.atan2(matrix[8], matrix[0]),
                ))
    except (OSError, sqlite3.Error):
        return ()
    return tuple(
        (float(row[1]), index, hit_frame, tuple(spawns))
        for index, hit_frame in skills
    )


@lru_cache(maxsize=256)
def _star_beam_aura(
    static_database_path: str, effect_id: str,
) -> tuple[float, float, float, float, float] | None:
    """(radius, width, start angle, degrees per second, beam spacing).

    EVIDENCE (data): boss_khalys_beam_aura (boss_khalys_base_beam's
    UserStatusEffects, D 4): AuraTargets Enemies, AuraRadius 20.85,
    AuraStarBeamWidth 1.1, AuraStarBeamAngle 360 (one beam), AuraStartAngle
    -60, AuraRotationSpeed 30; its visual boss_01_beam is one straight beam.
    """

    try:
        with sqlite3.connect(
            f"file:{Path(static_database_path).as_posix()}?mode=ro", uri=True
        ) as database:
            row = database.execute(
                "SELECT COALESCE(AuraTargets, ''), COALESCE(AuraRadius, 0), "
                "COALESCE(AuraStarBeamWidth, 0), COALESCE(AuraStartAngle, 0), "
                "COALESCE(AuraRotationSpeed, 0), COALESCE(AuraStarBeamAngle, 0) "
                "FROM _Template_StatusEffect WHERE Id=?", (str(effect_id),),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    if (
        row is None or str(row[0]).strip().casefold() != "enemies"
        or float(row[1]) <= 0.0 or float(row[2]) <= 0.0
    ):
        return None
    return (
        float(row[1]), float(row[2]), float(row[3]), float(row[4]),
        float(row[5]) if 0.0 < float(row[5]) < 360.0 else 360.0,
    )


@dataclass(frozen=True)
class MonsterBeamAura:
    """A rotating star beam a monster carries (SEE: _star_beam_aura)."""

    instance_id: int
    owner_actor_id: int
    definition: "VictimStatusEffectDefinition"
    centre: tuple[float, float, float]
    yaw: float
    starts_at: float
    expires_at: float
    starts_tick: int
    skill_index: int
    radius: float
    width: float
    start_angle: float
    rotation_speed: float
    spacing: float

    def touches(self, point, now: float, reach: float) -> bool:
        """Whether a point is within ``reach`` of one of its beams now."""

        dx = float(point[0]) - self.centre[0]
        dz = float(point[2]) - self.centre[2]
        turned = self.start_angle + self.rotation_speed * max(
            0.0, now - self.starts_at
        )
        for index in range(max(1, round(360.0 / self.spacing))):
            # Forward is -Z at yaw 0, turning as the server turns OX/OZ.
            yaw = self.yaw + math.radians(turned + index * self.spacing)
            ux, uz = -math.sin(yaw), -math.cos(yaw)
            along = min(self.radius, max(0.0, dx * ux + dz * uz))
            if math.hypot(dx - ux * along, dz - uz * along) <= (
                self.width * 0.5 + reach
            ):
                return True
        return False

# ``Messaging::Hit::SetDamageTypes`` consumes this enum in the exact order
# recovered from the client: Physical, Fire, Ice, Lightning, DarkMagic,
# Poison.  The client selects elemental combat text (including
# DamageDonePoison) from this array.
DAMAGE_TYPE_IDS = {
    "physical": 0,
    "fire": 1,
    "ice": 2,
    "lightning": 3,
    "darkmagic": 4,
    "andermagic": 4,
    "poison": 5,
}


def damage_type_ids(encoded: str) -> tuple[int, ...]:
    """Return the client DamageType ids with a positive authored weight."""

    result: list[int] = []
    for entry in str(encoded or "Physical").replace(",", ";").split(";"):
        token, separator, raw_weight = entry.strip().partition(":")
        damage_type = DAMAGE_TYPE_IDS.get(token.strip().casefold())
        if damage_type is None:
            continue
        try:
            weight = float(raw_weight) if separator else 1.0
        except ValueError:
            weight = 1.0
        if weight > 0.0 and damage_type not in result:
            result.append(damage_type)
    return tuple(result or (DAMAGE_TYPE_IDS["physical"],))


_COMBAT_ROLL_MASK = 0xFFFFFFFF
_COMBAT_ROLL_SCALE = float(0x1_0000_0000)


# A monster's Shifted skill falls on ground points with these TargetingTypes
# (RandomPosition: points around the caster, _monster_shifted_points).
MONSTER_GROUND_TARGETING_TYPES = frozenset({
    "radius", "point", "location", "randomposition",
})


def deterministic_combat_roll(*components: int) -> float:
    """Mix one combat event into an unbiased unit-interval roll.

    Combat outcomes remain reproducible without sharing the strong linear
    correlation of the old ``tick * A + actor * B`` expression. Adjacent
    actors attacking on one simulation frame must receive independent rolls;
    otherwise a 0.2% block window can be shared by a whole nearby pack.
    """

    value = 0x9E3779B9
    for ordinal, component in enumerate(components, 1):
        mixed_component = (
            (int(component) & _COMBAT_ROLL_MASK)
            + ordinal * 0x85EBCA6B
        ) & _COMBAT_ROLL_MASK
        value ^= mixed_component
        value ^= value >> 16
        value = (value * 0x7FEB352D) & _COMBAT_ROLL_MASK
        value ^= value >> 15
        value = (value * 0x846CA68B) & _COMBAT_ROLL_MASK
        value ^= value >> 16
    return (value & _COMBAT_ROLL_MASK) / _COMBAT_ROLL_SCALE

# Retail animation sequences advance in whole frames. The character sheet's
# raw attack speed therefore changes the effective animation rate only when a
# breakpoint is crossed. Keep the authored values (ascending for lookup), not
# a smoothed interpolation between them. The supplied rows describe the
# 25-frame through 6-frame portion of the curve; the same whole-frame sequence
# continues through the one-frame floor rather than capping at 4.1667 APS.
ATTACK_SPEED_BREAKPOINTS = (
    (1.0256, 1.2500),
    (1.0811, 1.3158),
    (1.1429, 1.3889),
    (1.2121, 1.4706),
    (1.2903, 1.5625),
    (1.3793, 1.6667),
    (1.4815, 1.7857),
    (1.6000, 1.9231),
    (1.7391, 2.0833),
    (1.9048, 2.2727),
    (2.1053, 2.5000),
    (2.3529, 2.7778),
    (2.6667, 3.1250),
    (3.0769, 3.5714),
    (3.6364, 4.1667),
    (4.4444, 5.0000),
    (5.7143, 6.2500),
    (8.0000, 8.3333),
    (13.3333, 12.5000),
    (40.0000, 25.0000),
)


def effective_attack_speed(raw_attack_speed: float) -> float:
    """Quantize a character-sheet speed to retail sequence breakpoints."""

    raw = max(0.01, float(raw_attack_speed))
    if raw < 1.0:
        # Slows below baseline remain meaningful even though the supplied
        # positive-speed table begins above 1.0.
        return raw
    effective = 1.0
    for threshold, attacks_per_second in ATTACK_SPEED_BREAKPOINTS:
        if raw + 1e-9 < threshold:
            break
        effective = attacks_per_second
    return effective


# Height above the feet at which a bullet flies when its StartOffset gives
# none, and the band the authored heights are kept in (low enough that a
# shot clears no wall, high enough that kerbs and benches never stop it).
DEFAULT_BULLET_FLIGHT_HEIGHT = 1.2
BULLET_FLIGHT_HEIGHT_RANGE = (0.8, 1.8)


def _bullet_flight_height(start_offset) -> float:
    height = float(start_offset[1]) if start_offset else 0.0
    if height <= 0.0:
        return DEFAULT_BULLET_FLIGHT_HEIGHT
    return min(BULLET_FLIGHT_HEIGHT_RANGE[1], max(BULLET_FLIGHT_HEIGHT_RANGE[0], height))


def _player_impact_tick(server_tick: int, delay_seconds: float) -> int:
    """Map an exact monotonic deadline onto the client's 25 Hz clock.

    The wall-clock timer remains sub-frame precise.  The integer command tick
    must never describe an impact before that timer expires, so positive
    fractional delays are rounded up instead of being snapped to the nearest
    shared frame.
    """

    delay = max(0.0, float(delay_seconds))
    offset = (
        0
        if delay <= 0.0
        else max(
            1,
            int(math.ceil(delay * SERVER_TICKS_PER_SECOND - 1e-9)),
        )
    )
    return (int(server_tick) + offset) & 0xFFFFFFFF
# Dense maps can place dozens of attack-ready actors in one simulation
# bubble. Starting every cast on the same tick creates an oversized reliable
# command and synchronized hit burst. Retail distributes AI decisions over
# its simulation frames, so cap starts per map tick and rotate fairly.
MONSTER_ATTACK_START_BUDGET = max(
    1, int(os.getenv('DSO_MONSTER_ATTACKS_PER_TICK', '4'))
)
PLAYER_SUMMON_ACTOR_BASE = 0x00010600
PLAYER_SUMMON_ACTOR_CAPACITY = 0x0200
# SustainedSkillCommand owns a second client execution clock.  Both Machine
# Gun casts in the supplied retail session use ``context tick - cast tick =
# 2023`` (640 -> 2663 and 765 -> 2788).  The ordinary hit-frame deadline is a
# different value and remains responsible for authoritative damage timing.
SUSTAINED_SKILL_CONTEXT_OFFSET_TICKS = 2023
# Retail waits eleven game ticks between Machine Gun's NewMonster snapshot and
# its first SustainedSkillCommand (629 -> 640 in the supplied capture).  Its
# eight-frame HitFrame is the later damage cadence, not the actor-ready delay.
SUSTAINED_SKILL_INITIAL_DELAY_SECONDS = 11.0 / SERVER_TICKS_PER_SECOND
# The latest retail trace repeats the native Machine Gun sustained command at
# ticks 14840, 14965, 15090, and 15215: exactly 125 ticks (five seconds).
# SkillUnblockFrame is 120, so deriving this lease from that field cuts the
# client animation graph short before retail would refresh it.
SUSTAINED_SKILL_PLAYBACK_INTERVAL_SECONDS = (
    125.0 / SERVER_TICKS_PER_SECOND
)
@dataclass(frozen=True)
class MonsterCombatant:
    actor_id: int
    template_id: str
    level: int
    rank_id: str
    position: tuple[float, float, float]
    maximum_health: int
    minimum_damage: float
    maximum_damage: float
    run_speed: float
    aggro_range: float
    escape_range: float
    travel_distance: float
    attack_interval: float
    collision_radius: float
    attack: MonsterAttackDefinition
    attacks: tuple[MonsterAttackDefinition, ...] = ()
    target_radius: float | None = None
    # Fixed tower hardware (power nodes, trap shooters) is scenery that shoots.
    # Retail marks it with ``e_mark_no_damage``/invulnerability, so no player
    # skill, damage-over-time, or summon may take its health down.
    invulnerable: bool = False


def _monster_health_after_damage(
    monster: MonsterCombatant,
    previous_health: int,
    damage: int,
) -> tuple[int, bool]:
    """Apply damage while keeping arena training dummies available forever."""

    if monster.invulnerable:
        return int(previous_health), False
    current = max(0, int(previous_health) - max(0, int(damage)))
    if current == 0 and str(monster.template_id).casefold().startswith(
        "damage_doll"
    ):
        return max(1, int(monster.maximum_health)), False
    return current, current == 0


@dataclass(frozen=True)
class PlayerSkillGeometry:
    resource_id: str
    skill_type: str
    targeting_type: str
    char_class: str
    groups: tuple[str, ...]
    attack_range: float
    hit_range: float
    angle_degrees: float
    end_width: float
    player_width: float
    projectile_radius: float
    impact_radius: float
    projectile_range: float
    bullet_emit_delay: float
    projectile_velocity: float
    hit_frame: int
    victim_status_effects: tuple["VictimStatusEffectDefinition", ...]
    # Animation frame at which the cast enters its executing/projectile
    # phase.  This is distinct from HitFrame: retail ShiftedSkill packets use
    # LoopStartFrame in SkillExecutionContext and serialize HitFrame as the
    # following impact phase (Meteor is the clearest example: 5 + 60).
    loop_start_frame: int = 0
    motion_unblock_frame: int = 0
    skill_unblock_frame: int = 0
    execute_sequence_mapping: str = ""
    damage_type: str = "Physical"
    bullet_count: int = 1
    reproduction_count: int = 0
    scale_skill_timing: bool = True
    damage_modifier: float = 1.0
    projectile_lifetime: float = 0.0
    impact_count: int = 1
    impact_child_spawn_count: int = 0
    target_search_radius: float = 0.0
    motion_type: str = "Straight"
    start_offset: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    user_status_effects: tuple["VictimStatusEffectDefinition", ...] = ()
    location_status_effects: tuple["VictimStatusEffectDefinition", ...] = ()
    location_owner_status_effects: tuple[
        "VictimStatusEffectDefinition", ...
    ] = ()
    location_visual_effects: tuple[
        "VictimStatusEffectDefinition", ...
    ] = ()
    skill_timing_multiplier: float = 1.0
    bullet_location_effect_rate: float = 0.0
    bullet_location_effects: tuple[
        "VictimStatusEffectDefinition", ...
    ] = ()
    bullet_location_visual_effects: tuple[
        "VictimStatusEffectDefinition", ...
    ] = ()
    bullet_death_effects: tuple[
        "VictimStatusEffectDefinition", ...
    ] = ()
    bullet_location_effect_start_delay: float = 0.0
    trap_on_hit: str = ""
    # Fractions of the actor's current maximum skill-resource pool. Database
    # values use 0.5 for 50%, and scoped ResourceCost/ResourceGain modifiers
    # operate on these values before they are converted to absolute points.
    resource_cost_fraction: float = 0.0
    resource_gain_fraction: float = 0.0
    resource_cost_to_damage: float = 0.0
    # ``_Template_Skill.AmmoCost``: essences spent from the active ammunition
    # slot per accepted cast. Zero for utility skills such as teleport.
    ammo_cost: int = 0
    # Casefolded status groups the cast removes from its caster: authored
    # ``StatusEffectGroupCleanse`` ("Frees you from any debuff" on Teleport,
    # Dive, Rocket Pack and Furious Battle Cry) plus any active
    # ``SkillStatusEffectCleanse`` talent program.
    cleanse_groups: frozenset[str] = frozenset()
    # ``_Template_SkillBullet.StaticCollision``: the bullet stops at walls.
    static_collision: bool = False


def _metadata_float(metadata: dict[str, str], key: str) -> float:
    """A status-effect column entry's numeric field, 0 when absent or not a number."""
    try:
        return float(metadata.get(key, 0) or 0)
    except ValueError:
        return 0.0


@dataclass(frozen=True)
class VictimStatusEffectDefinition:
    resource_id: str
    effect_id: int
    chance: float
    duration_ticks: int
    values: tuple[float, ...]
    trigger: str
    delay_ticks: int = 0
    # ``_Template_StatusEffect.MaxStackSize``. Retail replaces a running
    # instance with a fresh id on every application and carries this count as
    # a zero-based index; the previous id is removed in a no-details command.
    max_stack_size: int = 1
    # Some authored max-stack triggers immediately stop the stack owner (for
    # example frost -> applyFreeze -> StopStatusEffect(debuff_cc_frost)).  The
    # client consumes that instance at the cap, so retaining and refreshing it
    # server-side repeatedly re-fires the child effect on an actor that already
    # owns it.  This flag is derived from the linked database programs rather
    # than from effect-name heuristics.
    consume_on_max_stack: bool = False
    # Aura-hosted effects need their authored shape on the wire.  Omitting
    # this branch still applies gameplay modifiers, but the client cannot
    # instantiate looped visuals such as Tesla's electrical field.
    aura_radius: float = 0.0
    aura_height: float = 0.0
    aura_shape: str = ""
    modifier_kinds: frozenset[str] = frozenset()
    # Preserve the authored DP override separately from PvE's D duration.
    pvp_duration_ticks: int | None = None
    effect_hosts: str = "Receiver"
    aura_targets: str = ""
    exclusivity_type: str = "Replace"
    stack_count: int = 1
    # The location effect's offset from where it is cast (the skill column's
    # OX/OY/OZ), in the caster's frame: -Z ahead along the aim.
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    # The AuraTargets:Creator shell this child came from, when that shell has
    # a radius: the creator holds the child only while it stands inside
    # (Banner of War's cheaper skills and resource regeneration, 5.75 m).
    creator_aura: str = ""


@dataclass(frozen=True)
class PlayerSummonDefinition:
    """Database-owned construction created by a player skill."""

    skill_index: int
    skill_resource_id: str
    monster_template_id: str
    client_monster_template_id: str
    amount: int
    limit: int
    category: str
    spawn_delay_seconds: float
    lifetime_seconds: float
    despawn_duration_seconds: float
    owner_despawn_range: float
    spawn_offset: tuple[float, float, float, float]
    # Both authoritative geometry and client playback resolve the current
    # template's resource id against this client's bundled database.
    attack_skill_database_index: int | None
    attack_skill_index: int | None
    attack_skill_type: str
    attack_interval_seconds: float
    attack_playback_interval_seconds: float
    attack_playback_enabled: bool
    attack_initial_delay_seconds: float
    attack_loop_start_frame: int
    attack_motion_unblock_frame: int
    attack_skill_unblock_frame: int
    aura: PlayerSummonAuraDefinition | None
    location_visual_effects: tuple[
        VictimStatusEffectDefinition, ...
    ] = ()
    move_speed: float = 0.0
    aggro_range: float = 0.0
    heal_fraction: float = 0.0
    heal_radius: float = 0.0
    heal_interval_seconds: float = 1.0
    taunt_radius: float = 0.0
    support_status_effects: tuple[
        VictimStatusEffectDefinition, ...
    ] = ()
    stationary: bool = False
    # A permanent effect switches its Movement feature off (the mage
    # Guardians' ArcanemanifestationMinionIdle / guardian_idle:
    # ``ActorFeature:off,Movement``): it never walks, though its template has a
    # RunSpeed. Unlike ``stationary`` it keeps the ordinary summon spawn.
    movement_locked: bool = False
    startup_duration_seconds: float = 0.0
    overheat_attack_skill_database_index: int | None = None
    overheat_attack_skill_type: str = ""
    overheat_attack_interval_seconds: float = 0.0
    overheat_attack_unblock_frame: int = 0
    overheat_attack_motion_unblock_frame: int = 0
    overheat_attack_loop_start_frame: int = 0
    overheat_attack_skill_effect_id: int | None = None


@dataclass(frozen=True)
class PlayerTrapDefinition:
    resource_id: str
    graphics: str
    lifetime_seconds: float
    trip_radius: float
    victim_status_effects: tuple["VictimStatusEffectDefinition", ...]


@dataclass(frozen=True)
class ActivePlayerTrap:
    trap_id: int
    owner_actor_id: int
    skill_index: int
    cast_id: int
    server_tick: int
    position: tuple[float, float, float]
    yaw: float
    definition: PlayerTrapDefinition
    player_damage_range: tuple[float, float]
    critical_hit_chance: float
    critical_damage_multiplier: float
    modifier_sources: tuple[object, ...]
    expires_at: float


@dataclass(frozen=True)
class PlayerSummonActor:
    actor_id: int
    owner_actor_id: int
    definition: PlayerSummonDefinition
    spawn_group: MonsterSpawnGroup
    position: tuple[float, float, float]
    yaw: float
    created_at: float
    expires_at: float | None
    player_damage_range: tuple[float, float]
    modifier_sources: tuple[object, ...]
    initial_aura_instance_id: int | None
    cast_id: int = 0
    patrol_index: int = 0
    moving: bool = False

    @property
    def spawn_ready_at(self) -> float:
        return self.created_at + self.definition.startup_duration_seconds

    @property
    def attack_ready_at(self) -> float:
        definition = self.definition
        delay = max(
            definition.startup_duration_seconds,
            definition.attack_initial_delay_seconds,
        )
        if definition.attack_skill_type == "Sustained":
            delay = max(delay, SUSTAINED_SKILL_INITIAL_DELAY_SECONDS,
                        definition.attack_interval_seconds)
        return self.created_at + delay

    @property
    def client_actor_ids(self) -> tuple[int, ...]:
        return (self.actor_id,)

    @property
    def spawn_groups(self) -> tuple[MonsterSpawnGroup, ...]:
        return (self.spawn_group,)


@dataclass(frozen=True)
class PlayerSummonSpawn:
    actors: tuple[PlayerSummonActor, ...]
    discarded_actor_ids: tuple[int, ...]


@dataclass(frozen=True)
class PlayerLocationVisual:
    instance_id: int
    definition: VictimStatusEffectDefinition
    position: tuple[float, float, float]
    yaw: float
    starts_tick: int
    expires_tick: int
    owner_actor_id: int
    expires_at: float
    cast_id: int = 0
    receiver_status_effects: tuple[VictimStatusEffectDefinition, ...] = ()
    source_skill_index: int = 0
    player_damage_range: tuple[float, float] = (1.0, 1.0)
    managed_aura: bool = False
    source_actor_id: int | None = None


@dataclass(frozen=True)
class PlayerSupportApplication:
    source_actor_id: int
    target_actor_id: int
    server_tick: int
    status_effects: tuple[VictimStatusEffectDefinition, ...]
    remove: bool = False


@dataclass(frozen=True)
class _PendingPlayerSummonAttack:
    attack: "MonsterAttack"
    geometry: PlayerSkillGeometry
    player_damage_range: tuple[float, float]
    target_actor_ids: tuple[int, ...]
    target_due_times: tuple[tuple[int, float, int], ...] = ()


@dataclass(frozen=True)
class PendingPlayerHit:
    cast_id: int
    target_actor_id: int
    skill_index: int
    due_at: float
    impact_tick: int
    distance: float
    travel_distance: float
    emit_delay: float
    victim_status_effects: tuple[VictimStatusEffectDefinition, ...]
    source_actor_id: int = 1
    projectile_index: int = 0
    impact_index: int = 0
    target_hit_count: int = 1
    user_status_effects: tuple[VictimStatusEffectDefinition, ...] = ()
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    direction: tuple[float, float] = (0.0, 0.0)
    emitted_at: float = 0.0
    spawn_sweep_distance: float = 0.0
    projectile_velocity: float = 0.0
    projectile_radius: float = 0.0
    maximum_range: float = 0.0
    motion_type: str = "Straight"
    damage_modifier: float = 1.0
    player_minimum_damage: float = 0.0
    player_maximum_damage: float = 0.0
    damage_type: str = "Physical"
    critical_hit_chance: float = 0.0
    critical_damage_multiplier: float = 2.0
    damage_immune: bool = False
    # An area impact's radius around ``origin`` (the aimed point), checked
    # again when it lands: a meteor falls 2.6 s after the cast.
    area_radius: float = 0.0


@dataclass
class _MonsterStatusDamage:
    """One authored damage-over-time program running on a monster."""

    definition: "VictimStatusEffectDefinition"
    source_actor_id: int
    player_damage_range: tuple[float, float]
    skill_index: int
    interval_seconds: float
    coefficient: float
    damage_type: str
    relative: bool
    next_at: float
    expires_at: float
    stack_count: int = 1


@dataclass
class _MonsterRouteState:
    goal: tuple[float, float, float]
    waypoints: tuple[tuple[float, float, float], ...]
    next_waypoint: int
    refreshed_tick: int


@dataclass(frozen=True)
class MonsterMovement:
    actor_id: int
    world_position: tuple[float, float, float]
    wire_position: tuple[float, float, float]
    yaw: float
    moving: bool
    server_tick: int


@dataclass(frozen=True)
class MonsterReturnHomeTransition:
    actor_id: int
    active: bool
    effect_id: int
    instance_id: int
    start_tick: int
    duration_ticks: int


@dataclass(frozen=True)
class MonsterAuraStrike:
    """A one-shot damaging aura a monster skill puts on its caster.

    CONTRACT: a UserStatusEffects entry whose status has AuraRadius > 0,
    AuraTargets Enemies and no duration, and whose AuraEffectId children
    carry ``CurrHealthPointsDmg:$N,relative,<type>``: every enemy within
    AuraRadius of the caster takes coefficient x the monster's damage, once,
    DE seconds after the skill's hit frame.
    EVIDENCE: heredur_base_doublestrike (HitFrame 26) carries
    boss_heredur_aura_360smash (radius 5, DE:0.76 = 19 frames, $0 -0.5);
    its sequence miniboss_01_heredur_skill_02 shakes the camera at frames 26
    and 44, the two blows of the double strike.
    """

    resource_id: str
    radius: float
    delay_ticks: int
    # (kind, amount, damage type) per health-taking start instruction of its
    # children, SEE: LocalCombatState._status_health_programs.
    damage_programs: tuple[tuple[str, float, str], ...]


@dataclass(frozen=True)
class MonsterAttack:
    attacker_actor_id: int
    target_actor_id: int
    skill_level: int
    skill_index: int
    command_id: int
    aim_angle: float
    server_tick: int
    cast_id: int
    impact_tick: int
    hit_frame: int
    motion_unblock_frame: int
    skill_unblock_frame: int
    position: tuple[float, float, float, float]
    target_position: tuple[float, float, float]
    direction: tuple[float, float, float, float]
    projectile_radius: float
    variant: int
    due_at: float
    projectile_origin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    maximum_projectile_range: float = 0.0
    motion_type: str = "Straight"
    user_status_effects: tuple[VictimStatusEffectDefinition, ...] = ()
    loop_start_frame: int = 0
    sustained_hit_range: int = 0
    definition: MonsterAttackDefinition | None = None
    execution_context_tick: int | None = None
    bullet_count: int = 1
    resolved_target_actor_ids: tuple[int, ...] = ()
    # Set on the extra blow a skill's user aura lands after its hit frame
    # (MonsterAuraStrike); None for the skill's own hit.
    aura_strike: MonsterAuraStrike | None = None
    # A multi-bullet Shifted cast's other ground points (world frame, like
    # target_position), sent after it in its ShiftedSkill. SEE:
    # LocalCombatState._monster_shifted_points.
    extra_target_positions: tuple[tuple[float, float, float], ...] = ()
    # A homing bullet's flight (MotionType Homing / LockOnHoming), flown by
    # _advance_homing_bullet: when the monster cast it, when its LifeTime
    # ends (0.0: not flown, it lands at due_at), and where it was at
    # homing_at (None: still at projectile_origin). due_at is its release.
    cast_at: float = 0.0
    homing_expires_at: float = 0.0
    homing_position: tuple[float, float, float] | None = None
    homing_at: float = 0.0


HOMING_MOTION_TYPES = frozenset({"homing", "lockonhoming"})
# A homing bullet whose target no longer resolves is dropped this long after
# its LifeTime.
HOMING_ORPHAN_GRACE_SECONDS = 1.0


@dataclass(frozen=True)
class MonsterSummonRequest:
    """A monster's summon that landed: the service spawns the monsters."""

    caster_actor_id: int
    template_id: str
    amount: int
    limit: int
    position: tuple[float, float, float]
    level: int
    # A status effect's Minion instructions place each one where authored
    # (around the caster, turned by its aim) instead of on a ring.
    positions: tuple[tuple[float, float, float], ...] = ()


@lru_cache(maxsize=256)
def _monster_template_lifetime(static_database_path: str, template_id: str) -> float:
    try:
        with sqlite3.connect(
            f"file:{Path(static_database_path).as_posix()}?mode=ro", uri=True
        ) as database:
            row = database.execute(
                "SELECT COALESCE(LifeTime, 0) FROM _Template_Monster WHERE Id=?",
                (str(template_id),),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return -1.0
    return -1.0 if row is None else float(row[0] or 0.0)


@dataclass(frozen=True)
class DamageResolution:
    target_actor_id: int
    attacker_actor_id: int
    current_health: int
    maximum_health: int
    damage: int
    killed: bool
    position: tuple[float, float, float]
    server_tick: int
    hit_tags: tuple[int, ...]
    skill_index: int
    victim_status_effects: tuple[VictimStatusEffectDefinition, ...] = ()
    source_actor_id: int | None = None
    critical_hit: bool = False
    # Only periodic status-effect ticks use elemental floating combat text.
    # Direct skills retain their authored damage type for mitigation while
    # displaying the ordinary/critical direct-hit indicator.
    periodic_damage: bool = False
    blocked: bool = False
    damage_immune: bool = False
    # Kill.animation_time on the wire; player delivery uses retail's long life.
    corpse_lifetime_ticks: int = 300
    cast_id: int = 0


class LocalCombatState:
    """Mutable map-world health, transforms, cooldowns, and projectile state."""

    def __init__(
        self,
        encounter: LocalMonsterEncounter,
        *,
        player_actor_id: int,
        player_health: int,
        player_current_health: int | None = None,
        player_health_regeneration_fraction: float = 0.0,
        player_health_regeneration_absolute: float = 0.0,
        player_health_regeneration_interval: float = 1.0,
        player_maximum_resource: int = 100,
        player_current_resource: float | None = None,
        player_resource_regeneration_fraction: float = 0.0,
        player_resource_regeneration_absolute: float = 0.0,
        player_resource_regeneration_interval: float = 1.0,
        player_resource_regeneration_delay: float = 0.0,
        initially_active: bool = True,
    ) -> None:
        self.encounter = encounter
        self._cast_status_applications = {}
        self.lock = InstrumentedRLock(
            MAP_COMBAT_DOMAIN,
            f"combat[{getattr(encounter, 'map_id', '<unknown>')}@{id(self):x}]",
        )
        self.player_actor_id = int(player_actor_id)
        self.player_maximum_health = max(1, int(player_health))
        self.player_current_health = min(
            self.player_maximum_health,
            max(
                1,
                int(
                    self.player_maximum_health
                    if player_current_health is None
                    else player_current_health
                ),
            ),
        )
        self.player_health: dict[int, list[int]] = {
            self.player_actor_id: [
                self.player_current_health,
                self.player_maximum_health,
            ]
        }
        self.player_health_regeneration: dict[
            int, tuple[float, float, float]
        ] = {
            self.player_actor_id: (
                max(0.0, float(player_health_regeneration_fraction)),
                max(0.0, float(player_health_regeneration_absolute)),
                max(0.05, float(player_health_regeneration_interval)),
            )
        }
        self.last_player_health_regeneration_at = {
            self.player_actor_id: time.perf_counter()
        }
        maximum_resource = max(1.0, float(player_maximum_resource))
        current_resource = (
            maximum_resource
            if player_current_resource is None
            else min(
                maximum_resource,
                max(0.0, float(player_current_resource)),
            )
        )
        self.player_resource: dict[int, list[float]] = {
            self.player_actor_id: [current_resource, maximum_resource]
        }
        self.player_resource_regeneration: dict[
            int, tuple[float, float, float, float]
        ] = {
            self.player_actor_id: (
                float(player_resource_regeneration_fraction),
                float(player_resource_regeneration_absolute),
                max(0.05, float(player_resource_regeneration_interval)),
                max(0.0, float(player_resource_regeneration_delay)),
            )
        }
        resource_now = time.perf_counter()
        self.last_player_resource_change_at = {
            self.player_actor_id: resource_now
        }
        self.last_player_resource_regeneration_at = {
            self.player_actor_id: resource_now
        }
        self.monsters = self._combatants(encounter)
        # Actors that map-local events spawn after the encounter was built.
        # The encounter itself is immutable and shared through a cache, so
        self._runtime_spawn_groups: list[MonsterSpawnGroup] = []
        self._runtime_definitions: dict[int, MonsterRuntimeDefinition] = {}
        self._next_runtime_actor_id = map_monster_actor_base(
            encounter.map_id,
            static_database_path=encounter.static_database_path,
        )
        self.monster_health = {
            actor_id: monster.maximum_health
            for actor_id, monster in self.monsters.items()
        }
        self.monster_status_effects: dict[
            int,
            dict[
                str,
                tuple[VictimStatusEffectDefinition, float | None],
            ],
        ] = {actor_id: {} for actor_id in self.monsters}
        # Authored crowd-control diminishing returns, keyed by actor:
        # ``{group: (active_from, active_until)}``. The window opens when the
        # effect that granted it expires, which is what stops a victim being
        # re-frozen the instant a previous freeze lapses.
        self.monster_status_immunities: dict[
            int, dict[str, tuple[float, float]]
        ] = {}
        self._status_effect_content_cache: dict[
            str, tuple[frozenset[str], tuple[frozenset[str], float] | None]
        ] = {}
        # Authoritative answer to "why is this monster not responding".
        self._monster_control_reason: dict[int, str] = {}
        self._control_transitions: list[tuple[int, str | None]] = []
        # Movement and attack acquisition ask the same control question during
        # one authoritative tick. Re-parsing every active status program twice
        # per actor becomes visible on densely populated event maps.
        self._monster_action_lock_cache_at: float | None = None
        self._monster_action_lock_cache: dict[int, str | None] = {}
        # Simulation heartbeat. A client-visible freeze has to be attributed
        # to either "the simulation stopped producing" or "it produced and the
        # client never showed it", and only a running count separates them.
        self.simulation_ticks = 0
        self.published_movements = 0
        self.published_attacks = 0
        self._heartbeat_started_at = 0.0
        self._last_tick_inputs: dict | None = None
        # Published positions the client cannot have walked to in the elapsed
        # time. Those are what a player sees as a monster teleporting.
        self._teleport_reports: list[tuple[int, float, float, int]] = []
        self.monster_status_damage: dict[
            tuple[int, str], _MonsterStatusDamage
        ] = {}
        self.monster_ammo_vulnerabilities: dict[int, frozenset[str]] = {}
        permanent_effect_ids: dict[int, set[int]] = {}
        for command in encounter.spawn_batch.commands:
            if not isinstance(command, StatusEffectServerCommand):
                continue
            permanent_effect_ids.setdefault(int(command.actor_id), set()).update(
                int(entry.effect_id) for entry in command.value.entries
            )
        if permanent_effect_ids:
            with sqlite3.connect(
                f"file:{encounter.static_database_path.as_posix()}?mode=ro",
                uri=True,
            ) as database:
                for actor_id, effect_ids in permanent_effect_ids.items():
                    ammo_types = set()
                    for effect_id in effect_ids:
                        row = database.execute(
                            "SELECT StartModifiers FROM _Template_StatusEffect "
                            "WHERE rowid=? LIMIT 1",
                            (effect_id + 1,),
                        ).fetchone()
                        for raw in str(row[0] if row else "").split(";"):
                            kind, separator, arguments = raw.partition(":")
                            if separator and kind.casefold() == "ammovulnerability":
                                ammo_types.update(
                                    value.strip().casefold()
                                    for value in arguments.split(",")
                                    if value.strip()
                                )
                    if ammo_types:
                        self.monster_ammo_vulnerabilities[actor_id] = (
                            frozenset(ammo_types)
                        )
        self.monster_positions = {
            actor_id: monster.position
            for actor_id, monster in self.monsters.items()
        }
        self.monster_navigation_offsets = {}
        for actor_id, monster in self.monsters.items():
            navigation_ground = encounter.navigation.project_position(
                monster.position
            )
            self.monster_navigation_offsets[actor_id] = (
                monster.position[1] - navigation_ground[1]
            )
        initialized_at = time.perf_counter()
        self._monster_spawn_yaws = {
            int(command.value.actor_id): float(command.value.field_a0)
            for command in encounter.spawn_batch.commands
            if isinstance(command, NewMonsterServerCommand)
        }
        self.monster_yaws = dict(self._monster_spawn_yaws)
        self.tower_beam_started_at: dict[int, float] = {
            # Permanent aura effects are sent with StartTick=0, so the client
            # rotates these beams from the shared server clock origin.
            actor_id: 0.0
            for actor_id, monster in self.monsters.items()
            if is_tower_mechanism(
                encounter.static_database_path, encounter.map_id,
                monster.template_id,
            )
            and "powernode" in monster.template_id.casefold()
        }
        self.monster_moving = {
            actor_id: False for actor_id in self.monsters
        }
        # This gates only network snapshots. Positions and A* routes still
        # advance at 25 Hz so collision and attack acquisition remain exact.
        self.monster_last_movement_publish_ticks: dict[int, int] = {}
        self.monster_last_published_positions = dict(self.monster_positions)
        self.monster_routes: dict[int, _MonsterRouteState] = {}
        self._route_refresh_budget = 0
        self._route_actor_cursor = 0
        self.monster_target_actor_ids = {}
        # Scripted map actors can follow an authoritative fixed target instead
        # of acquiring a player (for example, the Crypt of Kings ram).
        self.scripted_monster_targets: dict[
            int, tuple[float, float, float]
        ] = {}
        self.returning_home_actor_ids: set[int] = set()
        self.returning_home_started_at: dict[int, float] = {}
        self.returning_home_progress: dict[int, float] = {}
        self._return_home_transitions: list[
            MonsterReturnHomeTransition
        ] = []
        self.active_monster_actor_ids = (
            set(self.monsters) if initially_active else set()
        )
        self.last_monster_attack_at = {
            actor_id: initialized_at for actor_id in self.monsters
        }
        self._monster_attack_cursor = 0
        self._monster_skill_cursors: dict[int, int] = {
            actor_id: 0 for actor_id in self.monsters
        }
        # When each monster's skill (or cooldown category) can be used again,
        # set once it starts fighting: its InitialCoolDown, then its CoolDown
        # after each use (_monster_skill_choice).
        self._monster_skill_ready_at: dict[int, dict[str, float]] = {}
        # The monsters each monster summoned (still counted while alive), and
        # the summons landed this tick, for the service to spawn
        # (services/monster_summons.py).
        self.monster_summoned_actor_ids: dict[int, set[int]] = {}
        self.pending_monster_summons: list[MonsterSummonRequest] = []
        # (due time, request): Minion instructions with a DE wait for it.
        self._delayed_monster_summons: list[tuple[float, MonsterSummonRequest]] = []
        self._monster_cooldown_random = random.Random(0x0B055)
        self.pending_monster_attacks: dict[int, MonsterAttack] = {}
        self._static_line_of_sight: StaticLineOfSight | None = None
        self._static_line_of_sight_loaded = False
        # The map's wall collision is built in the background as the map
        # loads: built on the first shot, its pure-Python mesh stalled that
        # tick under the combat lock on big maps. A shot before it is ready
        # waits for it (_map_static_line_of_sight).
        self._static_line_of_sight_thread = threading.Thread(
            target=self._load_static_line_of_sight,
            name=f"walls-{getattr(encounter, 'map_id', '')}",
            daemon=True,
        )
        self._static_line_of_sight_thread.start()
        # Released projectiles may still be flying when a boss recovers and
        # starts its next cast. Keep their impacts independently of that cast.
        self.pending_monster_projectile_attacks: dict[
            int, list[MonsterAttack]
        ] = {}
        self.tower_fire_burst_remaining: dict[int, int] = {}
        self.tower_fire_burst_cooldown_until: dict[int, float] = {}
        # Animation locks outlive early impacts and can end before a projectile
        # lands. Keep them separate from the pending damage transaction.
        self.monster_motion_unblock_at: dict[int, float] = {}
        self.monster_skill_unblock_at: dict[int, float] = {}
        self.monster_spawn_started_at: dict[int, float] = {}
        # Block rolls are per resolved impact, not per actor or render tick.
        # The sequence also protects against repeated authored attacks that
        # legitimately reuse a zero cast id and the same skill index.
        self._player_block_roll_sequence = 0
        self.pending_player_hits: dict[int, PendingPlayerHit] = {}
        # Runtime casts use a server actor id as part of their key. Different
        # clients legitimately reuse cast ids, and one cast may produce
        # several projectile/chain impacts.
        self.pending_runtime_player_hits: dict[
            tuple[int, int, int], PendingPlayerHit
        ] = {}
        self._player_skill_geometry: dict[int, PlayerSkillGeometry | None] = {}
        self._player_summon_definitions: dict[
            int, PlayerSummonDefinition | None
        ] = {}
        self._player_trap_definitions: dict[
            tuple[int, tuple[int, ...]], PlayerTrapDefinition | None
        ] = {}
        self.player_summons: dict[int, PlayerSummonActor] = {}
        self.player_summon_health: dict[int, list[int]] = {}
        self.player_summon_heat_hits: dict[int, int] = {}
        self.player_summon_overheated_until: dict[int, float] = {}
        self.map_companion_spawns: set[tuple[int, str]] = set()
        self.map_companion_retired: set[str] = set()
        self.player_traps: dict[int, ActivePlayerTrap] = {}
        self._next_player_trap_id = 0x00010000
        self.player_location_visuals: dict[int, PlayerLocationVisual] = {}
        # (visual instance, player actor, child index) -> next tick time of
        # a monster ground aura on that player (resolve_monster_location_auras).
        self._monster_location_aura_ticks: dict[
            tuple[int, int, int], float
        ] = {}
        self._monster_location_aura_program_cache: dict[tuple, tuple] = {}
        # Rotating star beams monsters carry (Khalys's beam), keyed apart
        # from the location visuals' ids (resolve_monster_location_auras).
        self.monster_beam_auras: dict[int, MonsterBeamAura] = {}
        self._next_monster_beam_id = 0x40000000
        self.player_location_aura_members: set[tuple[int, int]] = set()
        self.player_location_aura_refresh_at: dict[tuple[int, int], float] = {}
        self.player_location_owner_auras: dict[tuple[int, int], tuple[VictimStatusEffectDefinition, float]] = {}
        self._next_player_location_visual_id = 0x00010000
        self._monster_location_visual_casts: set[
            tuple[int, int, int]
        ] = set()
        self.pending_player_summon_attacks: dict[
            int, _PendingPlayerSummonAttack
        ] = {}
        # Bullets already fired by a construction that has since expired:
        # (the expired summon, its pending attack), resolved like any other.
        self.orphaned_player_summon_attacks: dict[
            int, tuple[PlayerSummonActor, _PendingPlayerSummonAttack]
        ] = {}
        self.last_player_summon_attack_at: dict[int, float] = {}
        self.last_player_summon_playback_at: dict[int, float] = {}
        self.last_player_summon_support_at: dict[int, float] = {}
        self.started_player_summon_effect_ids: dict[int, set[int]] = {}
        self.last_player_summon_aura_tick: dict[int, int] = {}
        self._next_player_summon_ordinal = 0
        self._next_player_summon_aura_instance_id = 0x00030000
        self._monster_attack_effects: dict[
            str, tuple[VictimStatusEffectDefinition, ...]
        ] = {}
        self._monster_user_effects: dict[
            str, tuple[VictimStatusEffectDefinition, ...]
        ] = {}
        # Skill resource id -> {user effect resource id: MonsterAuraStrike},
        # filled with _monster_user_effects.
        self._monster_aura_strikes: dict[
            str, dict[str, MonsterAuraStrike]
        ] = {}
        self._status_effect_modifier_programs: dict[
            int, tuple[tuple[str, tuple[str, ...]], ...]
        ] = {}
        # ``ActorFeature:off,...`` names per effect id. Content is immutable,
        # and the player victim gates ask this question several times per
        # packet, so the answer is memoized rather than re-parsed.
        self._status_effect_disabled_features: dict[int, frozenset[str]] = {}
        # Geometry compilation resolves nested status-effect and talent
        # programs from static.db4.  A cast may ask for the same combination
        # once per target/projectile, so keep a bounded immutable result cache
        # instead of reopening SQLite for every impact.  The key includes the
        # complete source stack/value payload; equipment, talent, and temporary
        # status changes therefore select a different entry automatically.
        self._modified_player_geometry_cache: dict[
            tuple[PlayerSkillGeometry, tuple[tuple[object, ...], ...]],
            PlayerSkillGeometry,
        ] = {}
        self._user_effect_location_auras_cache = {}
        runtime_catalog = load_status_runtime_catalog(
            encounter.static_database_path
        )
        self._actor_variable_values = runtime_catalog.actor_variables
        self._status_effect_runtime_programs = runtime_catalog.programs
        self._status_effect_groups = runtime_catalog.groups
        self._status_effect_ids = runtime_catalog.effect_ids

    @staticmethod
    def _combatants(
        encounter: LocalMonsterEncounter,
    ) -> dict[int, MonsterCombatant]:
        result = {}
        for command in encounter.spawn_batch.commands:
            if not isinstance(command, NewMonsterServerCommand):
                continue
            monster = command.value
            stats = {entry.kind: entry.value for entry in monster.stats}
            maximum_health = max(
                1, int(round(stats.get(2, monster.field_90)))
            )
            runtime = encounter.runtime_for_actor(monster.actor_id)
            result[monster.actor_id] = MonsterCombatant(
                actor_id=monster.actor_id,
                template_id=monster.template_id,
                level=max(1, int(monster.field_9c)),
                rank_id=monster.rank,
                position=monster.position,
                maximum_health=maximum_health,
                minimum_damage=float(stats.get(11, 1.0)),
                maximum_damage=float(stats.get(12, stats.get(11, 1.0))),
                run_speed=float(runtime.run_speed),
                aggro_range=float(runtime.aggro_range),
                escape_range=float(runtime.escape_range),
                travel_distance=(
                    float(runtime.travel_distance)
                    if (
                        str(monster.rank or "").casefold().startswith("rank_boss")
                        or (
                            encounter.map_id.casefold() == "f4203_boss"
                            and monster.template_id.casefold() == "f4203_boss_06_ram"
                        )
                    )
                    else min(
                        float(runtime.travel_distance),
                        MONSTER_NAVIGATION_RADIUS_CAP,
                    )
                ),
                attack_interval=float(runtime.attack_interval),
                collision_radius=float(runtime.capsule_radius),
                target_radius=monster_target_radii(encounter.static_database_path).get(monster.template_id),
                attack=runtime.attack,
                attacks=runtime.attacks or (runtime.attack,),
                invulnerable=(
                    is_tower_mechanism(
                        encounter.static_database_path, encounter.map_id,
                        monster.template_id,
                    )
                    or (
                        encounter.map_id.casefold() == "f4203_boss"
                        and monster.template_id.casefold() == "f4203_boss_06_ram"
                    )
                ),
            )
        if set(result) != set(encounter.actor_ids):
            raise ValueError("monster spawn batch and encounter actor ids differ")
        return result

    @property
    def living_actor_ids(self) -> tuple[int, ...]:
        """Return alive monsters currently active in the simulation bubble."""

        return tuple(
            actor_id
            for actor_id in sorted(self.active_monster_actor_ids)
            if self.monster_health.get(actor_id, 0) > 0
        )

    @property
    def alive_actor_ids(self) -> tuple[int, ...]:
        """Return all unkilled map-world monsters, including inactive ones.

        Vicinity discovery must be able to announce an inactive group before
        the client requests and activates it. Using ``living_actor_ids`` for
        that lookup creates a circular dependency: inactive monsters are
        filtered out, so they can never enter vicinity and become active.
        """

        return tuple(
            actor_id
            for actor_id in self.monsters
            if self.monster_health.get(actor_id, 0) > 0
        )

    @property
    def runtime_groups(self) -> tuple[MonsterSpawnGroup, ...]:
        """Spawn groups added after the encounter was built (map events)."""

        return tuple(self._runtime_spawn_groups)

    def runtime_group_for_actor(self, actor_id: int) -> MonsterSpawnGroup | None:
        actor_id = int(actor_id)
        for group in self._runtime_spawn_groups:
            if actor_id in group.actor_id_set:
                return group
        return None

    def group_for_actor(self, actor_id: int) -> MonsterSpawnGroup | None:
        """Authored or runtime spawn group that publishes ``actor_id``."""

        group = self.encounter.group_for_actor(actor_id)
        if group is None:
            group = self.runtime_group_for_actor(actor_id)
        return group

    def allocate_runtime_actor_ids(self, count: int) -> tuple[int, ...]:
        """Reserve map-monster actor ids above every id already in use."""

        highest = max(
            (int(actor_id) for actor_id in self.monsters),
            default=LOCAL_MONSTER_ACTOR_BASE - 1,
        )
        highest = max(highest, self._next_runtime_actor_id - 1)
        result = tuple(range(highest + 1, highest + 1 + max(0, int(count))))
        if result:
            self._next_runtime_actor_id = result[-1] + 1
        return result

    def monster_template_counts(self) -> tuple[dict[str, int], dict[str, int]]:
        """Return ``(present, alive)`` counts per hostile template id."""

        present: dict[str, int] = {}
        alive: dict[str, int] = {}
        for actor_id, monster in self.monsters.items():
            key = str(monster.template_id).casefold()
            present[key] = present.get(key, 0) + 1
            if self.monster_health.get(actor_id, 0) > 0:
                alive[key] = alive.get(key, 0) + 1
        return present, alive

    def find_monsters(
        self,
        template_id: str,
        position: tuple[float, float, float],
        *,
        radius: float = 1.0,
    ) -> tuple[int, ...]:
        """Actors of ``template_id`` whose authored home is near ``position``."""

        key = str(template_id).casefold()
        radius_squared = float(radius) ** 2
        return tuple(
            actor_id
            for actor_id, monster in self.monsters.items()
            if str(monster.template_id).casefold() == key
            and (
                (float(monster.position[0]) - float(position[0])) ** 2
                + (float(monster.position[2]) - float(position[2])) ** 2
            ) <= radius_squared
        )

    @staticmethod
    def load_ammo_vulnerabilities(
        static_database_path,
        commands,
    ) -> dict[int, frozenset[str]]:
        """Resolve ``AmmoVulnerability`` groups from permanent effects.

        Runs SQLite, so callers resolve it before taking the combat lock.
        """

        permanent_effect_ids: dict[int, set[int]] = {}
        for command in commands:
            if not isinstance(command, StatusEffectServerCommand):
                continue
            permanent_effect_ids.setdefault(int(command.actor_id), set()).update(
                int(entry.effect_id) for entry in command.value.entries
            )
        result: dict[int, frozenset[str]] = {}
        if not permanent_effect_ids:
            return result
        with sqlite3.connect(
            f"file:{Path(static_database_path).as_posix()}?mode=ro",
            uri=True,
        ) as database:
            for actor_id, effect_ids in permanent_effect_ids.items():
                ammo_types = set()
                for effect_id in effect_ids:
                    row = database.execute(
                        "SELECT StartModifiers FROM _Template_StatusEffect "
                        "WHERE rowid=? LIMIT 1",
                        (effect_id + 1,),
                    ).fetchone()
                    for raw in str(row[0] if row else "").split(";"):
                        kind, separator, arguments = raw.partition(":")
                        if separator and kind.casefold() == "ammovulnerability":
                            ammo_types.update(
                                value.strip().casefold()
                                for value in arguments.split(",")
                                if value.strip()
                            )
                if ammo_types:
                    result[actor_id] = frozenset(ammo_types)
        return result

    def begin_monster_spawn(
        self, actor_ids, *, now: float | None = None, startup_overrides=None,
    ) -> None:
        """Let each actor finish its authored entrance before AI can act.

        Anchor this to the first published NewMonster, since an encounter can
        exist long before a client enters its vicinity. Shared-map viewers and
        culling must not restart the server's entrance lock.
        """

        current_time = time.perf_counter() if now is None else float(now)
        for raw_actor_id in actor_ids:
            actor_id = int(raw_actor_id)
            if actor_id in self.monster_spawn_started_at or actor_id not in self.monsters:
                continue
            runtime = self._runtime_definitions.get(actor_id)
            if runtime is None:
                runtime = self.encounter.runtime_for_actor(actor_id)
            self.monster_spawn_started_at[actor_id] = current_time
            duration = max(0.0, float((startup_overrides or {}).get(
                actor_id, runtime.startup_duration
            )))
            if duration <= 0.0:
                continue
            ready_at = current_time + duration
            self.monster_motion_unblock_at[actor_id] = max(
                ready_at, self.monster_motion_unblock_at.get(actor_id, 0.0)
            )
            self.monster_skill_unblock_at[actor_id] = max(
                ready_at, self.monster_skill_unblock_at.get(actor_id, 0.0)
            )

    def add_runtime_monsters(
        self,
        group: MonsterSpawnGroup,
        runtime_definitions,
        *,
        ammo_vulnerabilities=None,
    ) -> tuple[int, ...]:
        """Register hostile actors a map event spawned after the map loaded.

        Mirrors the constructor's per-actor bookkeeping.  The new actors stay
        inactive until a viewer requests them, exactly like the authored
        population at map entry.
        """

        definitions = {
            int(definition.actor_id): definition
            for definition in runtime_definitions
        }
        added = []
        for command in group.spawn_batch.commands:
            if not isinstance(command, NewMonsterServerCommand):
                continue
            monster = command.value
            actor_id = int(monster.actor_id)
            if actor_id in self.monsters:
                continue
            runtime = definitions.get(actor_id)
            if runtime is None:
                runtime = self.encounter.runtime_for_actor(actor_id)
            stats = {entry.kind: entry.value for entry in monster.stats}
            maximum_health = max(
                1, int(round(stats.get(2, monster.field_90)))
            )
            combatant = MonsterCombatant(
                actor_id=actor_id,
                template_id=monster.template_id,
                level=max(1, int(monster.field_9c)),
                rank_id=monster.rank,
                position=monster.position,
                maximum_health=maximum_health,
                minimum_damage=float(stats.get(11, 1.0)),
                maximum_damage=float(stats.get(12, stats.get(11, 1.0))),
                run_speed=float(runtime.run_speed),
                aggro_range=float(runtime.aggro_range),
                escape_range=float(runtime.escape_range),
                travel_distance=(
                    float(runtime.travel_distance)
                    if (
                        self.encounter.map_id.casefold() == "f4203_boss"
                        and monster.template_id.casefold() == "f4203_boss_06_ram"
                    )
                    else min(
                        float(runtime.travel_distance),
                        MONSTER_NAVIGATION_RADIUS_CAP,
                    )
                ),
                attack_interval=float(runtime.attack_interval),
                collision_radius=float(runtime.capsule_radius),
                target_radius=monster_target_radii(self.encounter.static_database_path).get(monster.template_id),
                attack=runtime.attack,
                attacks=runtime.attacks or (runtime.attack,),
                invulnerable=(
                    is_tower_mechanism(
                        self.encounter.static_database_path,
                        self.encounter.map_id,
                        monster.template_id,
                    )
                    or (
                        self.encounter.map_id.casefold() == "f4203_boss"
                        and monster.template_id.casefold() == "f4203_boss_06_ram"
                    )
                ),
            )
            self.monsters[actor_id] = combatant
            self.monster_health[actor_id] = combatant.maximum_health
            self.monster_status_effects[actor_id] = {}
            self.monster_positions[actor_id] = combatant.position
            try:
                navigation_ground = self.encounter.navigation.project_position(
                    combatant.position
                )
                self.monster_navigation_offsets[actor_id] = (
                    combatant.position[1] - navigation_ground[1]
                )
            except (NavigationError, CollisionTerrainError, ValueError):
                self.monster_navigation_offsets[actor_id] = 0.0
            self._monster_spawn_yaws[actor_id] = float(monster.field_a0)
            self.monster_yaws[actor_id] = float(monster.field_a0)
            if (
                is_tower_mechanism(
                    self.encounter.static_database_path,
                    self.encounter.map_id,
                    monster.template_id,
                )
                and "powernode" in monster.template_id.casefold()
            ):
                self.tower_beam_started_at[actor_id] = 0.0
            self.monster_moving[actor_id] = False
            self.monster_last_published_positions[actor_id] = combatant.position
            self.last_monster_attack_at[actor_id] = time.perf_counter()
            self._monster_skill_cursors[actor_id] = 0
            self._monster_skill_ready_at.pop(actor_id, None)
            if ammo_vulnerabilities and actor_id in ammo_vulnerabilities:
                self.monster_ammo_vulnerabilities[actor_id] = frozenset(
                    ammo_vulnerabilities[actor_id]
                )
            self._runtime_definitions[actor_id] = runtime
            added.append(actor_id)
        if added:
            self._runtime_spawn_groups.append(group)
        return tuple(added)

    def retire_monsters(self, actor_ids) -> tuple[int, ...]:
        """Remove map-event actors from play without a kill transaction."""

        retired = []
        for actor_id in (int(value) for value in actor_ids):
            if actor_id not in self.monsters:
                continue
            self.monster_health[actor_id] = 0
            retired.append(actor_id)
        if retired:
            self.deactivate_monsters(tuple(retired), reset=True)
        return tuple(retired)

    @property
    def has_regenerating_players(self) -> bool:
        """Return whether a wounded player still needs server-side regen.

        Health regeneration must not be coupled to the presence of an active
        monster.  In particular, culling the last monster in a player's
        vicinity used to stop the fixed-rate world loop and strand the live
        health state below maximum indefinitely.
        """

        for actor_id, (current, maximum) in self.player_health.items():
            fraction, absolute, _interval = (
                self.player_health_regeneration.get(
                    actor_id, (0.0, 0.0, 1.0)
                )
            )
            if (
                0 < int(current) < int(maximum)
                and (float(fraction) > 0.0 or float(absolute) > 0.0)
            ):
                return True
        for actor_id, (current, maximum) in self.player_resource.items():
            fraction, absolute, _interval, _delay = (
                self.player_resource_regeneration.get(
                    actor_id, (0.0, 0.0, 1.0, 0.0)
                )
            )
            amount = float(maximum) * float(fraction) + float(absolute)
            if (
                (amount > 0.0 and float(current) < float(maximum))
                or (amount < 0.0 and float(current) > 0.0)
            ):
                return True
        return False

    def activate_monsters(self, actor_ids: tuple[int, ...]) -> None:
        unknown = set(actor_ids) - set(self.monsters)
        if unknown:
            raise ValueError(
                "cannot activate unknown monster actors: "
                + ", ".join(str(actor_id) for actor_id in sorted(unknown))
            )
        normalized = tuple(int(value) for value in actor_ids
                           if int(value) not in self.active_monster_actor_ids)
        self.active_monster_actor_ids.update(normalized)
        for actor_id in normalized:
            # The vicinity spawn packet already carries the current transform.
            # A later movement start must nevertheless publish immediately.
            self.monster_last_movement_publish_ticks.pop(actor_id, None)
            self.monster_last_published_positions[actor_id] = (
                self.monster_positions[actor_id]
            )

    def deactivate_monsters(
        self,
        actor_ids: tuple[int, ...],
        *,
        reset: bool = True,
    ) -> None:
        """Stop simulating discarded vicinity actors."""

        for actor_id in (int(value) for value in actor_ids):
            self.active_monster_actor_ids.discard(actor_id)
            if reset:
                self.returning_home_actor_ids.discard(actor_id)
            # Pause the recovery clock while this actor is not simulated.
            self.returning_home_started_at.pop(actor_id, None)
            self.returning_home_progress.pop(actor_id, None)
            self.monster_target_actor_ids.pop(actor_id, None)
            self.monster_moving[actor_id] = False
            self.pending_monster_attacks.pop(actor_id, None)
            self.pending_monster_projectile_attacks.pop(actor_id, None)
            self.tower_fire_burst_remaining.pop(actor_id, None)
            self.tower_fire_burst_cooldown_until.pop(actor_id, None)
            self.monster_motion_unblock_at.pop(actor_id, None)
            self.monster_skill_unblock_at.pop(actor_id, None)
            self.monster_routes.pop(actor_id, None)
            self.monster_last_movement_publish_ticks.pop(actor_id, None)
            self.monster_last_published_positions.pop(actor_id, None)
            for cast_id, pending in tuple(self.pending_player_hits.items()):
                if pending.target_actor_id == actor_id:
                    del self.pending_player_hits[cast_id]
            for key, pending in tuple(
                self.pending_runtime_player_hits.items()
            ):
                if pending.target_actor_id == actor_id:
                    del self.pending_runtime_player_hits[key]
            if reset and actor_id in self.monsters:
                monster = self.monsters[actor_id]
                self.monster_health[actor_id] = monster.maximum_health
                self.monster_status_effects[actor_id].clear()
                self.monster_status_immunities.pop(actor_id, None)
                self.monster_positions[actor_id] = monster.position
                self.monster_yaws[actor_id] = self._monster_spawn_yaws.get(actor_id, 0.0)
                self.monster_moving[actor_id] = False
        discarded = set(int(value) for value in actor_ids)
        self._return_home_transitions = [
            transition
            for transition in self._return_home_transitions
            if transition.actor_id not in discarded
        ]

    def heal_player(
        self, *, full: bool = False, actor_id: int | None = None
    ) -> tuple[int, int]:
        """Apply the local health-globe result and return current/maximum HP."""

        actor_id = (
            self.player_actor_id if actor_id is None else int(actor_id)
        )
        health = self.player_health.setdefault(
            actor_id,
            [self.player_maximum_health, self.player_maximum_health],
        )
        current, maximum = health
        if current <= 0:
            return 0, maximum
        if full:
            current = maximum
        else:
            recovered = max(1, round(maximum * 0.25))
            current = min(maximum, current + recovered)
        health[0] = current
        if actor_id == self.player_actor_id:
            self.player_current_health = current
        return current, maximum

    def register_player(
        self,
        actor_id: int,
        maximum_health: int,
        *,
        current_health: int | None = None,
        regeneration_fraction: float = 0.0,
        regeneration_absolute: float = 0.0,
        regeneration_interval: float = 1.0,
        maximum_resource: int | None = None,
        current_resource: float | None = None,
        resource_regeneration_fraction: float = 0.0,
        resource_regeneration_absolute: float = 0.0,
        resource_regeneration_interval: float = 1.0,
        resource_regeneration_delay: float = 0.0,
    ) -> None:
        actor_id = int(actor_id)
        maximum_health = max(1, int(maximum_health))
        current = (
            self.player_health.get(actor_id, [maximum_health])[0]
            if current_health is None
            else int(current_health)
        )
        self.player_health[actor_id] = [
            min(maximum_health, max(0, int(current))),
            maximum_health,
        ]
        self.player_health_regeneration[actor_id] = (
            max(0.0, float(regeneration_fraction)),
            max(0.0, float(regeneration_absolute)),
            max(0.05, float(regeneration_interval)),
        )
        self.last_player_health_regeneration_at[actor_id] = time.perf_counter()
        previous_resource = self.player_resource.get(actor_id)
        resolved_maximum_resource = max(
            1.0,
            float(
                maximum_resource
                if maximum_resource is not None
                else (
                    previous_resource[1]
                    if previous_resource is not None else 100.0
                )
            ),
        )
        resolved_current_resource = (
            previous_resource[0]
            if current_resource is None and previous_resource is not None
            else (
                resolved_maximum_resource
                if current_resource is None
                else float(current_resource)
            )
        )
        self.player_resource[actor_id] = [
            min(
                resolved_maximum_resource,
                max(0.0, resolved_current_resource),
            ),
            resolved_maximum_resource,
        ]
        self.player_resource_regeneration[actor_id] = (
            float(resource_regeneration_fraction),
            float(resource_regeneration_absolute),
            max(0.05, float(resource_regeneration_interval)),
            max(0.0, float(resource_regeneration_delay)),
        )
        resource_now = time.perf_counter()
        self.last_player_resource_change_at.setdefault(actor_id, resource_now)
        # A refresh (every status expiry re-registers the player) must not
        # restart the regeneration clock.
        self.last_player_resource_regeneration_at.setdefault(
            actor_id, resource_now
        )

    def unregister_player(self, actor_id: int) -> None:
        actor_id = int(actor_id)
        if actor_id != self.player_actor_id:
            self.player_health.pop(actor_id, None)
            self.player_health_regeneration.pop(actor_id, None)
            self.last_player_health_regeneration_at.pop(actor_id, None)
            self.player_resource.pop(actor_id, None)
            self.player_resource_regeneration.pop(actor_id, None)
            self.last_player_resource_change_at.pop(actor_id, None)
            self.last_player_resource_regeneration_at.pop(actor_id, None)

    def regenerate_players(
        self, *, now: float
    ) -> tuple[tuple[int, int, int, int, int], ...]:
        """Apply each due base/item regeneration tick independently."""

        transitions = []
        now = float(now)
        for actor_id, health in self.player_health.items():
            fraction, absolute, interval = (
                self.player_health_regeneration.get(
                    actor_id, (0.0, 0.0, 1.0)
                )
            )
            previous_at = self.last_player_health_regeneration_at.get(
                actor_id, now
            )
            due_count = min(
                8,
                max(0, int((now - previous_at + 1e-6) / interval)),
            )
            if due_count <= 0:
                continue
            self.last_player_health_regeneration_at[actor_id] = (
                previous_at + due_count * interval
            )
            current, maximum = health
            amount = max(
                0,
                int(round(maximum * fraction + absolute)),
            )
            if amount <= 0 or current <= 0 or current >= maximum:
                continue
            for _ in range(due_count):
                previous = current
                current = min(maximum, current + amount)
                recovered = current - previous
                if recovered <= 0:
                    break
                transitions.append(
                    (actor_id, previous, current, maximum, recovered)
                )
            health[0] = current
            if actor_id == self.player_actor_id:
                self.player_current_health = current
        return tuple(transitions)

    def apply_player_skill_resource(
        self,
        actor_id: int,
        *,
        cost_fraction: float,
        gain_fraction: float,
        now: float,
    ) -> tuple[bool, float, float, float, float]:
        """Atomically pay/gain one skill's authored resource fractions."""

        actor_id = int(actor_id)
        resource = self.player_resource.setdefault(
            actor_id, [100.0, 100.0]
        )
        current, maximum = resource
        cost = max(0.0, float(maximum) * max(0.0, cost_fraction))
        gain = max(0.0, float(maximum) * max(0.0, gain_fraction))
        # Integer-valued HUD resources still originate from fractional
        # database costs. Round only at the final pool boundary.
        cost = float(round(cost))
        gain = float(round(gain))
        if current + 1e-6 < cost:
            return False, current, maximum, cost, gain
        current = min(maximum, max(0.0, current - cost + gain))
        resource[0] = current
        # Only rage's out-of-combat delay counts from the last spend; mana keeps
        # ticking on its own clock. Restarting it here meant a mage casting more
        # often than once a second never regenerated at all.
        self.last_player_resource_change_at[actor_id] = float(now)
        return True, current, maximum, cost, gain

    def note_player_attacked(self, actor_id: int, *, now: float) -> None:
        """Restart rage's out-of-combat delay without delaying mana recovery."""
        actor_id = int(actor_id)
        fraction, absolute, _interval, _delay = self.player_resource_regeneration.get(
            actor_id, (0.0, 0.0, 1.0, 0.0)
        )
        maximum = self.player_resource.get(actor_id, (0.0, 0.0))[1]
        if maximum * fraction + absolute < 0:
            self.last_player_resource_change_at[actor_id] = float(now)
            self.last_player_resource_regeneration_at[actor_id] = float(now)

    def regenerate_player_resources(
        self, *, now: float
    ) -> tuple[tuple[int, float, float, float], ...]:
        """Apply due class/status resource regeneration or rage decay."""

        transitions = []
        now = float(now)
        for actor_id, resource in self.player_resource.items():
            fraction, absolute, interval, delay = (
                self.player_resource_regeneration.get(
                    actor_id, (0.0, 0.0, 1.0, 0.0)
                )
            )
            amount = float(resource[1]) * float(fraction) + float(absolute)
            if abs(amount) <= 1e-9:
                continue
            cursor = self.last_player_resource_regeneration_at.get(
                actor_id, now
            )
            if delay > 0:
                cursor = max(
                    cursor,
                    self.last_player_resource_change_at.get(actor_id, now)
                    + float(delay),
                )
            due_count = min(
                8,
                max(0, int((now - cursor + 1e-6) / float(interval))),
            )
            if due_count <= 0:
                continue
            self.last_player_resource_regeneration_at[actor_id] = (
                cursor + due_count * float(interval)
            )
            previous = float(resource[0])
            resource[0] = min(
                float(resource[1]),
                max(0.0, previous + amount * due_count),
            )
            if abs(resource[0] - previous) > 1e-6:
                transitions.append(
                    (actor_id, resource[0], resource[1], resource[0] - previous)
                )
        return tuple(transitions)

    @staticmethod
    def _distance(
        left: tuple[float, float, float],
        right: tuple[float, float, float],
    ) -> float:
        return math.hypot(left[0] - right[0], left[2] - right[2])

    def _near_protected_waypoint(
        self, position: tuple[float, float, float]
    ) -> bool:
        return any(
            self._distance(position, waypoint) <= radius
            for waypoint, radius in self.encounter.protected_waypoints
        )

    def _segment_intersects_protected_waypoint(
        self,
        start: tuple[float, float, float],
        end: tuple[float, float, float],
        *,
        padding: float = 0.2,
    ) -> bool:
        """Return whether a horizontal movement segment crosses a safe zone."""

        dx = float(end[0]) - float(start[0])
        dz = float(end[2]) - float(start[2])
        length_squared = dx * dx + dz * dz
        for waypoint, radius in self.encounter.protected_waypoints:
            if length_squared <= 1e-9:
                closest_x, closest_z = float(start[0]), float(start[2])
            else:
                parameter = (
                    (float(waypoint[0]) - float(start[0])) * dx
                    + (float(waypoint[2]) - float(start[2])) * dz
                ) / length_squared
                parameter = max(0.0, min(1.0, parameter))
                closest_x = float(start[0]) + dx * parameter
                closest_z = float(start[2]) + dz * parameter
            if math.hypot(
                closest_x - float(waypoint[0]),
                closest_z - float(waypoint[2]),
            ) <= float(radius) + max(0.0, float(padding)):
                return True
        return False

    @staticmethod
    def _clamp_to_territory(
        point: tuple[float, float, float],
        home: tuple[float, float, float],
        radius: float,
    ) -> tuple[float, float, float]:
        dx = point[0] - home[0]
        dz = point[2] - home[2]
        distance = math.hypot(dx, dz)
        if distance <= radius or distance <= 1e-8:
            return point
        scale = radius / distance
        return (
            home[0] + dx * scale,
            point[1],
            home[2] + dz * scale,
        )

    @staticmethod
    def _return_home_instance_id(actor_id: int) -> int:
        # Stable for add/remove pairing, while remaining disjoint from the
        # small sequential ids used for player skill status effects.
        return (0x4D520000 | (int(actor_id) & 0xFFFF)) & 0xFFFFFFFF

    def _return_home_effect(self) -> tuple[int, int]:
        """Resolve ``MonsterReturnHome``'s wire id and duration from the DB.

        The client keys status effects by the zero-based row order of
        ``_Template_StatusEffect``, so a hardcoded id silently starts pointing
        at an unrelated effect as soon as that table is edited. Read the live
        value once per encounter and fall back to the audited constants.
        """

        cached = getattr(self, "_return_home_effect_cache", None)
        if cached is not None:
            return cached
        effect_id = MONSTER_RETURN_HOME_EFFECT_ID
        duration_ticks = MONSTER_RETURN_HOME_DURATION_TICKS
        try:
            path = self.encounter.static_database_path
            with sqlite3.connect(
                f"file:{path.as_posix()}?mode=ro", uri=True
            ) as database:
                row = database.execute(
                    "SELECT rowid - 1, COALESCE(StatusEffectDuration, 0) "
                    "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
                    (MONSTER_RETURN_HOME_EFFECT_RESOURCE_ID,),
                ).fetchone()
            if row is not None:
                effect_id = int(row[0])
                duration_ticks = max(
                    0, round(max(0.0, float(row[1])) * SERVER_TICKS_PER_SECOND)
                ) or MONSTER_RETURN_HOME_DURATION_TICKS
        except Exception:
            # Content lookup only. A damaged or fixture database must never
            # stop a monster from returning home.
            pass
        self._return_home_effect_cache = (effect_id, duration_ticks)
        return self._return_home_effect_cache

    def _effects_kept_on_way_home(
        self, resource_ids: tuple[str, ...]
    ) -> frozenset[str]:
        """Select the effects ``KeepOnWayHome`` says survive a reset."""

        if not resource_ids:
            return frozenset()
        cache = getattr(self, "_keep_on_way_home_cache", None)
        if cache is None:
            cache = {}
            self._keep_on_way_home_cache = cache
        unknown = tuple(
            resource_id
            for resource_id in set(resource_ids)
            if resource_id not in cache
        )
        if unknown:
            for resource_id in unknown:
                cache[resource_id] = False
            try:
                path = self.encounter.static_database_path
                placeholders = ",".join("?" * len(unknown))
                with sqlite3.connect(
                    f"file:{path.as_posix()}?mode=ro", uri=True
                ) as database:
                    for row in database.execute(
                        "SELECT Id, COALESCE(KeepOnWayHome, 0) "
                        "FROM _Template_StatusEffect "
                        f"WHERE Id IN ({placeholders})",
                        unknown,
                    ):
                        cache[str(row[0])] = bool(row[1])
            except Exception:
                # Treat an unreadable row as "cleared", which matches the
                # previous unconditional behaviour.
                pass
        return frozenset(
            resource_id
            for resource_id in resource_ids
            if cache.get(resource_id, False)
        )

    def _reset_monster_to_spawn(
        self,
        actor_id: int,
        server_tick: int,
    ) -> None:
        """Snap one monster back to its spawn and drop everything it carried.

        This is the hard half of the leash. A monster that has been pulled
        past its reset radius does not walk home across the map: it resets in
        place, exactly like a fresh spawn. ``MonsterReturnHome`` is still
        raised and cleared around the snap so the client sees the same
        return-home transition it gets from an ordinary walk back.
        """

        actor_id = int(actor_id)
        monster = self.monsters[actor_id]
        # Raises MonsterReturnHome and drops this actor's queued swings and
        # every player impact already in flight toward it.
        self._set_returning_home(
            actor_id, True, server_tick,
            reason="hard leash: pulled past its reset radius, reset at spawn",
        )
        self.monster_positions[actor_id] = monster.position
        self.monster_health[actor_id] = monster.maximum_health
        self.monster_yaws[actor_id] = self._monster_spawn_yaws.get(actor_id, 0.0)
        self.monster_moving[actor_id] = False
        self.monster_routes.pop(actor_id, None)
        active = self.monster_status_effects.get(actor_id)
        if active:
            kept = self._effects_kept_on_way_home(tuple(active))
            for resource_id in tuple(active):
                if resource_id in kept:
                    continue
                active.pop(resource_id, None)
                self.monster_status_damage.pop((actor_id, resource_id), None)
        # A reset actor is a fresh spawn; it carries no diminishing returns.
        self.monster_status_immunities.pop(actor_id, None)
        # Its skill cooldowns start over with its next fight.
        self._monster_skill_ready_at.pop(actor_id, None)
        # Clears MonsterReturnHome: the actor is already home.
        self._set_returning_home(actor_id, False, server_tick)

    def _set_returning_home(
        self,
        actor_id: int,
        active: bool,
        server_tick: int,
        *, now: float | None = None, reason: str = "",
    ) -> bool:
        actor_id = int(actor_id)
        active = bool(active)
        if active == (actor_id in self.returning_home_actor_ids):
            return False
        if active:
            if reason:
                cancelled_hits = sum(
                    pending.target_actor_id == actor_id
                    for pending in self.pending_player_hits.values()
                ) + sum(
                    pending.target_actor_id == actor_id
                    for pending in self.pending_runtime_player_hits.values()
                )
                print(
                    f"[{self.__class__.__name__}] monster {actor_id} gives up: "
                    f"{reason}"
                    + (
                        f"; {cancelled_hits} player hit(s) in flight cancelled"
                        if cancelled_hits else ""
                    )
                )
            self.returning_home_actor_ids.add(actor_id)
            self.returning_home_started_at[actor_id] = time.perf_counter() if now is None else now
            self.returning_home_progress[actor_id] = self._distance(
                self.monster_positions[actor_id], self.monsters[actor_id].position)
            self.monster_target_actor_ids.pop(actor_id, None)
            self.pending_monster_attacks.pop(actor_id, None)
            self.pending_monster_projectile_attacks.pop(actor_id, None)
            self.tower_fire_burst_remaining.pop(actor_id, None)
            self.tower_fire_burst_cooldown_until.pop(actor_id, None)
            self.monster_motion_unblock_at.pop(actor_id, None)
            self.monster_skill_unblock_at.pop(actor_id, None)
            for cast_id, pending in tuple(self.pending_player_hits.items()):
                if pending.target_actor_id == actor_id:
                    del self.pending_player_hits[cast_id]
            for key, pending in tuple(
                self.pending_runtime_player_hits.items()
            ):
                if pending.target_actor_id == actor_id:
                    del self.pending_runtime_player_hits[key]
        else:
            self.returning_home_actor_ids.discard(actor_id)
            self.returning_home_started_at.pop(actor_id, None)
            self.returning_home_progress.pop(actor_id, None)
        self.monster_routes.pop(actor_id, None)
        effect_id, duration_ticks = self._return_home_effect()
        self._return_home_transitions.append(
            MonsterReturnHomeTransition(
                actor_id=actor_id,
                active=active,
                effect_id=effect_id,
                instance_id=self._return_home_instance_id(actor_id),
                start_tick=int(server_tick) & 0xFFFFFFFF,
                duration_ticks=duration_ticks,
            )
        )
        return True

    def drain_return_home_transitions(
        self,
    ) -> tuple[MonsterReturnHomeTransition, ...]:
        transitions = tuple(self._return_home_transitions)
        self._return_home_transitions.clear()
        return transitions

    def _monster_movement_publish_due(
        self,
        actor_id: int,
        server_tick: int,
        *,
        moving: bool,
        was_moving: bool,
        position: tuple[float, float, float],
        force: bool = False,
    ) -> bool:
        '''Keep 25 Hz simulation while publishing retail-like snapshots.

        Start and stop transitions are never delayed. Ongoing movement is
        sampled on the server tick clock; unsigned subtraction keeps the
        cadence correct when the 32-bit tick counter wraps.
        '''

        actor_id = int(actor_id)
        tick = int(server_tick) & 0xFFFFFFFF
        previous_tick = self.monster_last_movement_publish_ticks.get(actor_id)
        due = (
            previous_tick is None
            or ((tick - previous_tick) & 0xFFFFFFFF)
            >= MONSTER_MOVEMENT_PUBLISH_INTERVAL_TICKS
        )
        if not (force or bool(moving) != bool(was_moving) or due):
            return False
        self._note_unwalkable_publish(actor_id, tick, previous_tick, position)
        self.monster_last_movement_publish_ticks[actor_id] = tick
        self.monster_last_published_positions[actor_id] = position
        return True

    def _note_unwalkable_publish(
        self,
        actor_id: int,
        tick: int,
        previous_tick: int | None,
        position: tuple[float, float, float],
    ) -> None:
        """Record a published step the client cannot reproduce by walking.

        The client animates an actor from where it last saw it toward each
        published position. A step longer than the actor could have walked in
        the elapsed ticks cannot be animated, so it is drawn as a jump -- what
        a player reports as a monster teleporting. Recorded rather than
        printed: this runs under the map lock.
        """

        previous_position = self.monster_last_published_positions.get(actor_id)
        monster = self.monsters.get(actor_id)
        if previous_position is None or previous_tick is None or monster is None:
            return
        elapsed_ticks = (int(tick) - int(previous_tick)) & 0xFFFFFFFF
        if not elapsed_ticks or elapsed_ticks > 0xFFFF:
            return
        walkable = (
            max(0.0, float(monster.run_speed))
            * elapsed_ticks
            / SERVER_TICKS_PER_SECOND
        )
        stepped = math.hypot(
            position[0] - previous_position[0],
            position[2] - previous_position[2],
        )
        if stepped <= walkable * MONSTER_TELEPORT_TOLERANCE + 0.05:
            return
        self._teleport_reports.append(
            (actor_id, stepped, walkable, elapsed_ticks)
        )

    def drain_teleport_reports(
        self,
    ) -> tuple[tuple[int, float, float, int], ...]:
        reports = tuple(self._teleport_reports)
        self._teleport_reports.clear()
        return reports

    @staticmethod
    def _preferred_stop_distance(monster: MonsterCombatant) -> float:
        has_melee = any(
            attack.command_id in {SKILL_COMMAND, TARGET_SKILL_COMMAND}
            and attack.attack_range <= 8.0
            for attack in (monster.attacks or (monster.attack,))
        )
        if not has_melee and monster.attack.command_id in {
            BULLET_SKILL_COMMAND,
            TARGET_BULLET_SKILL_COMMAND,
        }:
            return max(
                MONSTER_STOP_DISTANCE,
                monster.attack.attack_range * RANGED_STANDOFF_RATIO,
            )
        return MONSTER_STOP_DISTANCE

    def _load_player_skill_geometry(
        self, skill_index: int
    ) -> PlayerSkillGeometry | None:
        skill_index = int(skill_index)
        if skill_index in self._player_skill_geometry:
            return self._player_skill_geometry[skill_index]
        path = self.encounter.static_database_path
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as db:
            row = db.execute(
                """
                SELECT s.Id, s.SkillType, s.TargetingType, s.AttackRange,
                       s.HitRange, s.Angle, s.EndWidth, s.PlayerWidth,
                       COALESCE(b.BulletRadius, 0),
                       COALESCE(b.ImpactRadius, 0),
                       COALESCE(b.Velocity, 0) * COALESCE(b.LifeTime, 0),
                       COALESCE(s.BulletEmitDelay, 0),
                       COALESCE(b.Velocity, 0),
                       COALESCE(s.HitFrame, 0),
                       COALESCE(s.VictimStatusEffects, ''),
                       COALESCE(s.BulletCount, 1),
                       COALESCE(s.ReproductionCount, 0),
                       COALESCE(s.ScaleSkillTiming, 1),
                       COALESCE(s.DamageModifier, 1),
                       COALESCE(s.UserStatusEffects, ''),
                       COALESCE(b.LifeTime, 0),
                       COALESCE(b.ImpactCount, 1),
                       COALESCE(b.ImpactChildSpawnCount, 0),
                       COALESCE(b.TargetSearchRadius, 0),
                        COALESCE(b.MotionType, 'Straight'),
                        b.StartOffset,
                        COALESCE(s.LocationStatusEffects, ''),
                        COALESCE(s.CharClass, ''),
                        COALESCE(s.Groups, ''),
                        COALESCE(s.DamageType, 'Physical'),
                        COALESCE(b.BulletLocationEffectRate, 0),
                        COALESCE(b.BulletLocationEffect, ''),
                        COALESCE(b.BulletDeathLocationEffect, ''),
                        COALESCE(b.BulletLocationEffectStartDelay, 0),
                        COALESCE(s.MotionUnblockFrame, 0),
                        COALESCE(s.SkillUnblockFrame, 0),
                        COALESCE(s.ExecuteSequenceMapping, ''),
                        COALESCE(s.LoopStartFrame, 0),
                        COALESCE(s.TrapOnHit, ''),
                        COALESCE(s.ResourceCost, 0),
                        COALESCE(s.ResourceGain, 0),
                        COALESCE(s.ResourceCostToDamage, 0),
                        COALESCE(s.AmmoCost, 0),
                        COALESCE(s.StatusEffectGroupCleanse, ''),
                        COALESCE(b.StaticCollision, 0)
                FROM _Template_Skill AS s
                LEFT JOIN _Template_SkillBullet AS b
                  ON b.Id = s.SkillBulletId
                WHERE s.rowid=? LIMIT 1
                """,
                (skill_index + 1,),
            ).fetchone()
            victim_status_effects = (
                self._load_victim_status_effects(db, row[14])
                if row is not None
                else ()
            )
            user_status_effects = (
                self._load_victim_status_effects(
                    db,
                    row[19],
                    default_trigger="skillstart",
                )
                if row is not None
                else ()
            )
            location_status_effects = (
                self._load_location_aura_effects(db, row[26])
                if row is not None
                else ()
            )
            location_owner_status_effects = (
                self._load_aura_owner_effects(db, row[26])
                if row is not None
                else ()
            )
            location_visual_effects = (
                self._load_victim_status_effects(db, row[26])
                if row is not None
                else ()
            )
            # Normal Arachna's legacy summon omits the ground-pool entry
            # present in the newer encounter. Use the existing pool resource
            # for its visual at the caster's summon position.
            if row is not None and row[0] == "arachna_base_summon" and not location_visual_effects:
                location_visual_effects = self._load_victim_status_effects(
                    db, "poisonous_puddle_aura,C:1.0,D:30.0,DP:30.0,$0:-0.10"
                )
            bullet_location_effects = (
                self._load_location_aura_effects(db, row[31])
                if row is not None
                else ()
            )
            bullet_location_visual_effects = (
                self._load_victim_status_effects(db, row[31])
                if row is not None
                else ()
            )
            bullet_death_effects = (
                self._load_location_aura_effects(db, row[32])
                if row is not None
                else ()
            )
        start_offset = (0.0, 0.0, 0.0, 0.0)
        if row is not None and isinstance(row[25], bytes):
            try:
                if len(row[25]) == 16:
                    start_offset = tuple(struct.unpack("<4f", row[25]))
            except struct.error:
                pass
        geometry = (
            PlayerSkillGeometry(
                resource_id=str(row[0]),
                skill_type=str(row[1]),
                targeting_type=str(row[2]),
                char_class=str(row[27]),
                groups=tuple(
                    part.strip()
                    for part in str(row[28] or "")
                    .replace(",", ";")
                    .split(";")
                    if part.strip()
                ),
                attack_range=max(0.0, float(row[3])),
                hit_range=max(0.0, float(row[4])),
                angle_degrees=max(0.0, float(row[5])),
                end_width=max(0.0, float(row[6])),
                player_width=max(0.0, float(row[7])),
                projectile_radius=(
                    max(0.0, float(row[8]))
                    + PLAYER_SKILL_PROJECTILE_RADIUS_BONUS.get(
                        str(row[0]).casefold(), 0.0
                    )
                ),
                impact_radius=max(0.0, float(row[9])),
                projectile_range=max(0.0, float(row[10])),
                bullet_emit_delay=max(0.0, float(row[11])),
                projectile_velocity=max(0.0, float(row[12])),
                hit_frame=max(0, int(row[13])),
                victim_status_effects=victim_status_effects,
                loop_start_frame=max(0, int(row[37] or 0)),
                motion_unblock_frame=max(0, int(row[34] or 0)),
                skill_unblock_frame=max(0, int(row[35] or 0)),
                execute_sequence_mapping=str(row[36] or ""),
                bullet_count=max(1, int(row[15])),
                reproduction_count=max(0, int(row[16])),
                scale_skill_timing=bool(row[17]),
                damage_modifier=max(0.0, float(row[18])),
                projectile_lifetime=max(0.0, float(row[20])),
                impact_count=max(1, int(row[21])),
                impact_child_spawn_count=max(0, int(row[22])),
                target_search_radius=max(0.0, float(row[23])),
                motion_type=str(row[24] or "Straight"),
                start_offset=start_offset,
                user_status_effects=user_status_effects,
                location_status_effects=location_status_effects,
                location_owner_status_effects=location_owner_status_effects,
                location_visual_effects=location_visual_effects,
                damage_type=str(row[29] or "Physical"),
                bullet_location_effect_rate=max(0.0, float(row[30] or 0.0)),
                bullet_location_effects=bullet_location_effects,
                bullet_location_visual_effects=(
                    bullet_location_visual_effects
                ),
                bullet_death_effects=bullet_death_effects,
                bullet_location_effect_start_delay=max(
                    0.0, float(row[33] or 0.0)
                ),
                trap_on_hit=str(row[38] or ""),
                resource_cost_fraction=max(0.0, float(row[39] or 0.0)),
                resource_gain_fraction=max(0.0, float(row[40] or 0.0)),
                resource_cost_to_damage=max(0.0, float(row[41] or 0.0)),
                ammo_cost=max(0, int(row[42] or 0)),
                cleanse_groups=frozenset(
                    part.strip().casefold()
                    for part in str(row[43] or "")
                    .replace(";", ",")
                    .split(",")
                    if part.strip()
                ),
                static_collision=bool(row[44] or 0),
            )
            if row is not None
            else None
        )
        self._player_skill_geometry[skill_index] = geometry
        return geometry

    @classmethod
    def _load_player_summon_support(
        cls,
        database: sqlite3.Connection,
        permanent_status_effects: str,
    ) -> tuple[
        float, float, float, float,
        tuple[VictimStatusEffectDefinition, ...],
    ]:
        '''Resolve summon heal and taunt auras from generated effects.'''

        heal_fraction = 0.0
        heal_radius = 0.0
        heal_interval = 1.0
        taunt_radius = 0.0
        support_effects = []
        for permanent_effect in str(
            permanent_status_effects or ''
        ).split(';'):
            permanent_effect = permanent_effect.strip()
            if not permanent_effect:
                continue
            aura_row = database.execute(
                'SELECT AuraEffectId, AuraRadius, '
                'StatusEffectTickRate FROM _Template_StatusEffect '
                'WHERE Id=? LIMIT 1',
                (permanent_effect,),
            ).fetchone()
            if aura_row is None:
                continue
            for child_id in str(aura_row[0] or '').split(';'):
                child_id = child_id.strip()
                if not child_id:
                    continue
                child_row = database.execute(
                    'SELECT StartModifiers FROM _Template_StatusEffect '
                    'WHERE Id=? LIMIT 1',
                    (child_id,),
                ).fetchone()
                modifiers = str(child_row[0] or '') if child_row else ''
                if 'Taunt' in modifiers:
                    taunt_radius = max(
                        taunt_radius, float(aura_row[1] or 0.0)
                    )
                if 'CurrHealthPoints:' not in modifiers:
                    continue
                raw_amount = modifiers.partition(
                    'CurrHealthPoints:'
                )[2].partition(',')[0].strip()
                if raw_amount.startswith('$'):
                    variable = database.execute(
                        'SELECT InitialValue FROM _Template_ActorVariables '
                        'WHERE Id=? LIMIT 1',
                        (raw_amount[1:],),
                    ).fetchone()
                    amount = float(variable[0]) if variable else 0.0
                else:
                    try:
                        amount = float(raw_amount)
                    except ValueError:
                        amount = 0.0
                heal_fraction = max(heal_fraction, amount)
                heal_radius = max(
                    heal_radius, float(aura_row[1] or 0.0)
                )
                heal_interval = max(
                    0.1, float(aura_row[2] or 1.0)
                )
                support_effects.extend(
                    replace(
                        effect,
                        duration_ticks=max(1, effect.duration_ticks),
                    )
                    for effect in cls._load_victim_status_effects(
                        database,
                        child_id,
                        default_trigger='immediate',
                    )
                )
        return (
            heal_fraction, heal_radius, heal_interval, taunt_radius,
            tuple(support_effects),
        )

    def player_summon_definition(
        self,
        skill_index: int,
        *,
        modifier_sources: tuple[object, ...] = (),
    ) -> PlayerSummonDefinition | None:
        """Resolve the summon metadata owned by one SkillManager entry."""

        skill_index = int(skill_index)
        if skill_index not in self._player_summon_definitions:
            path = self.encounter.static_database_path
            with sqlite3.connect(
                f"file:{path.as_posix()}?mode=ro",
                uri=True,
            ) as database:
                row = database.execute(
                    """
                    SELECT s.Id, COALESCE(s.Summons, 0),
                           COALESCE(s.SummonMonsterId, ''),
                           COALESCE(s.SummonMonsterAmount, 0),
                           COALESCE(s.SummonMonsterLimit, 0),
                           COALESCE(s.SummonCategory, ''),
                           COALESCE(s.SummonSpawnDelay, 0),
                           s.SummonSpawnOffset,
                           COALESCE(m.LifeTime, 0),
                           COALESCE(m.Skills, ''),
                           COALESCE(m.DespawnDuration, 0),
                           COALESCE(m.PermanentStatusEffect, ''),
                           COALESCE(m.RunSpeed, 0),
                           COALESCE(m.AggroRange, 0),
                           COALESCE(m.StartupDuration, 0),
                           COALESCE(m.Graphics, ''),
                           COALESCE(m.StartAnimationState, ''),
                           COALESCE(m.SummonSequence, '')
                    FROM _Template_Skill AS s
                    LEFT JOIN _Template_Monster AS m
                      ON m.Id=s.SummonMonsterId
                    WHERE s.rowid=? LIMIT 1
                    """,
                    (skill_index + 1,),
                ).fetchone()
                definition = None
                if (
                    row is not None
                    and bool(row[1])
                    and str(row[2]).strip()
                    and int(row[3]) > 0
                ):
                    monster_template_id = str(row[2])
                    client_monster_template_id = monster_template_id
                    attack_skill_database_index = None
                    attack_skill_index = None
                    attack_skill_type = ""
                    attack_interval = 0.4
                    attack_playback_interval = 0.4
                    attack_playback_enabled = False
                    startup_duration = summon_startup_seconds(*row[14:18])
                    attack_initial_delay = 0.0
                    attack_loop_start_frame = 0
                    attack_motion_unblock_frame = 0
                    attack_skill_unblock_frame = 0
                    overheat_attack_skill_database_index = None
                    overheat_attack_skill_type = ""
                    overheat_attack_interval_seconds = 0.0
                    overheat_attack_unblock_frame = 0
                    overheat_attack_motion_unblock_frame = 0
                    overheat_attack_loop_start_frame = 0
                    overheat_attack_skill_effect_id = None
                    for raw_skill in str(row[9] or "").replace(
                        ";", ","
                    ).split(","):
                        resource_id = raw_skill.partition(":")[0].strip()
                        if not resource_id:
                            continue
                        attack_row = database.execute(
                            """
                            SELECT rowid - 1,
                                   COALESCE(SkillType, ''),
                                   COALESCE(SkillUnblockFrame, 0),
                                   COALESCE(HitFrame, 0),
                                   COALESCE(LoopStartFrame, 0),
                                   COALESCE(MotionUnblockFrame, 0),
                                   COALESCE(ExecuteSequenceMapping, '')
                            FROM _Template_Skill AS skill
                            WHERE skill.Id=? LIMIT 1
                            """,
                            (resource_id,),
                        ).fetchone()
                        if attack_row is None:
                            continue
                        attack_skill_database_index = int(attack_row[0])
                        attack_skill_index = int(attack_row[0])
                        attack_skill_type = str(attack_row[1])
                        execute_sequence = str(attack_row[6]).strip()
                        attack_playback_enabled = (
                            bool(execute_sequence)
                            and execute_sequence.casefold()
                            != "empty_sequence"
                        )
                        attack_skill_unblock_frame = max(
                            0, int(attack_row[2])
                        )
                        attack_loop_start_frame = max(
                            0, int(attack_row[4])
                        )
                        attack_motion_unblock_frame = max(
                            0, int(attack_row[5])
                        )
                        if attack_skill_type == "Sustained":
                            attack_interval = max(
                                0.2,
                                int(attack_row[3])
                                / SERVER_TICKS_PER_SECOND,
                            )
                            attack_playback_interval = max(
                                attack_interval,
                                int(attack_row[2])
                                / SERVER_TICKS_PER_SECOND,
                                SUSTAINED_SKILL_PLAYBACK_INTERVAL_SECONDS,
                            )
                        elif resource_id == "minionskill_coveringfireturret":
                            # Retail refreshes the five-second support effects
                            # shortly before their expiry.
                            attack_interval = 4.0
                            attack_playback_interval = attack_interval
                        else:
                            attack_interval = max(
                                0.2,
                                int(attack_row[2])
                                / SERVER_TICKS_PER_SECOND,
                            )
                            attack_playback_interval = attack_interval
                        break
                    if monster_template_id.casefold() == (
                        "mercenary_ember_knight_kain"
                    ):
                        overheat_row = database.execute(
                            "SELECT rowid - 1, COALESCE(SkillType, ''), "
                            "COALESCE(SkillUnblockFrame, 0), "
                            "COALESCE(LoopStartFrame, 0), "
                            "COALESCE(MotionUnblockFrame, 0) "
                            "FROM _Template_Skill "
                            "WHERE Id='mercenaryskill_lava_01_attack_02' "
                            "AND COALESCE(Hidden, 0)<>0 LIMIT 1"
                        ).fetchone()
                        effect_row = database.execute(
                            "SELECT rowid - 1 FROM _Template_StatusEffect "
                            "WHERE Id='mercenary_ember_knight_overheated' "
                            "LIMIT 1"
                        ).fetchone()
                        if overheat_row is not None and effect_row is not None:
                            overheat_attack_skill_database_index = int(
                                overheat_row[0]
                            )
                            overheat_attack_skill_type = str(
                                overheat_row[1]
                            )
                            overheat_attack_unblock_frame = max(
                                0, int(overheat_row[2])
                            )
                            overheat_attack_loop_start_frame = max(
                                0, int(overheat_row[3])
                            )
                            overheat_attack_motion_unblock_frame = max(
                                0, int(overheat_row[4])
                            )
                            overheat_attack_interval_seconds = max(
                                0.2,
                                overheat_attack_unblock_frame
                                / SERVER_TICKS_PER_SECOND,
                            )
                            overheat_attack_skill_effect_id = int(
                                effect_row[0]
                            )
                    spawn_offset = (0.0, 0.0, 0.0, 0.0)
                    if isinstance(row[7], bytes) and len(row[7]) == 16:
                        try:
                            spawn_offset = tuple(
                                float(value)
                                for value in struct.unpack("<4f", row[7])
                            )
                        except struct.error:
                            pass
                    geometry = self._load_player_skill_geometry(skill_index)
                    aura = load_player_summon_aura(
                        database,
                        monster_template_id,
                    )
                    location_visual_effects = (
                        geometry.location_visual_effects
                        if geometry is not None
                        else ()
                    )
                    # Machine Gun (and a few legacy constructions) has no
                    # LocationStatusEffects row. Retail still allocates a
                    # location lifecycle from its summon visualizer and
                    # discards that instance when SummonsDead fires. Without
                    # this fallback the actor can be removed, but its native
                    # deploy/teardown VFX is never created or terminated.
                    if not location_visual_effects and geometry is not None:
                        location_visual_effects = tuple(
                            effect
                            for effect in geometry.user_status_effects
                            if "summon_visualizer"
                            in effect.resource_id.casefold()
                        )
                    visual_lifetime = max(
                        (
                            effect.duration_ticks
                            / SERVER_TICKS_PER_SECOND
                            for effect in (
                                geometry.user_status_effects
                                if geometry is not None
                                else ()
                            )
                            if "summon_visualizer"
                            in effect.resource_id.casefold()
                        ),
                        default=0.0,
                    )
                    visualizer_lifetime = visual_lifetime
                    visual_lifetime = max(
                        visual_lifetime,
                        max(
                            (
                                effect.duration_ticks
                                / SERVER_TICKS_PER_SECOND
                                for effect in location_visual_effects
                            ),
                            default=0.0,
                        ),
                    )
                    movement_locked = False
                    for permanent_effect in str(row[11] or "").split(";"):
                        permanent_effect = permanent_effect.strip()
                        if not permanent_effect:
                            continue
                        delay_row = database.execute(
                            """
                            SELECT COALESCE(StatusEffectDuration, 0),
                                   COALESCE(StartModifiers, '')
                            FROM _Template_StatusEffect
                            WHERE Id=? LIMIT 1
                            """,
                            (permanent_effect,),
                        ).fetchone()
                        if delay_row is not None and "movement" in (
                            parse_disabled_actor_features(str(delay_row[1]))
                        ):
                            movement_locked = True
                        if (
                            delay_row is not None
                            and "delay" in permanent_effect.casefold()
                        ):
                            attack_initial_delay = max(
                                attack_initial_delay,
                                max(0.0, float(delay_row[0] or 0.0)),
                            )
                    summon_support = (
                        self._load_player_summon_support(
                            database, str(row[11] or '')
                        )
                    )
                    definition = PlayerSummonDefinition(
                        skill_index=skill_index,
                        skill_resource_id=str(row[0]),
                        monster_template_id=monster_template_id,
                        client_monster_template_id=(
                            client_monster_template_id
                        ),
                        amount=max(1, int(row[3])),
                        limit=max(1, int(row[4])),
                        category=str(row[5]),
                        spawn_delay_seconds=max(
                            0.0,
                            float(row[6]) / SERVER_TICKS_PER_SECOND,
                        ),
                        # The skill's own summon visualizer (and its despawn
                        # aura) is the construction's authored duration; the
                        # template LifeTime only bounds summons without one.
                        # Machine Gun authors 20 s on both but LifeTime 60:
                        # max() kept it firing and standing 40 s after its
                        # timer and ground marker had ended (2026-10-23).
                        lifetime_seconds=(
                            visual_lifetime
                            if visualizer_lifetime > 0.0
                            else max(
                                0.0, float(row[8] or 0.0), visual_lifetime,
                            )
                        ),
                        despawn_duration_seconds=max(
                            0.0, float(row[10] or 0.0)
                        ),
                        owner_despawn_range=(
                            PLAYER_SUMMON_OWNER_RANGE
                        ),
                        spawn_offset=spawn_offset,
                        attack_skill_database_index=(
                            attack_skill_database_index
                        ),
                        attack_skill_index=attack_skill_index,
                        attack_skill_type=attack_skill_type,
                        attack_interval_seconds=attack_interval,
                        attack_playback_interval_seconds=(
                            attack_playback_interval
                        ),
                        attack_playback_enabled=(
                            attack_playback_enabled
                        ),
                        startup_duration_seconds=startup_duration,
                        overheat_attack_skill_database_index=(
                            overheat_attack_skill_database_index
                        ),
                        overheat_attack_skill_type=(
                            overheat_attack_skill_type
                        ),
                        overheat_attack_interval_seconds=(
                            overheat_attack_interval_seconds
                        ),
                        overheat_attack_unblock_frame=(
                            overheat_attack_unblock_frame
                        ),
                        overheat_attack_motion_unblock_frame=(
                            overheat_attack_motion_unblock_frame
                        ),
                        overheat_attack_loop_start_frame=(
                            overheat_attack_loop_start_frame
                        ),
                        overheat_attack_skill_effect_id=(
                            overheat_attack_skill_effect_id
                        ),
                        attack_initial_delay_seconds=(
                            attack_initial_delay
                        ),
                        attack_loop_start_frame=attack_loop_start_frame,
                        attack_motion_unblock_frame=(
                            attack_motion_unblock_frame
                        ),
                        attack_skill_unblock_frame=(
                            attack_skill_unblock_frame
                        ),
                        aura=aura,
                        location_visual_effects=(
                            location_visual_effects
                        ),
                        move_speed=max(0.0, float(row[12] or 0.0)),
                        aggro_range=max(0.0, float(row[13] or 0.0)),
                        heal_fraction=summon_support[0],
                        heal_radius=summon_support[1],
                        heal_interval_seconds=summon_support[2],
                        taunt_radius=summon_support[3],
                        support_status_effects=summon_support[4],
                        stationary=(
                            str(row[0]).casefold().startswith("turret")
                            or "turret" in monster_template_id.casefold()
                        ),
                        movement_locked=movement_locked,
                    )
            self._player_summon_definitions[skill_index] = definition

        definition = self._player_summon_definitions[skill_index]
        if definition is None or not modifier_sources:
            return definition
        geometry = self._load_player_skill_geometry(skill_index)
        if geometry is None:
            return definition
        geometry = self._modified_player_skill_geometry(
            geometry, modifier_sources
        )
        visual_lifetime = max(
            (
                effect.duration_ticks / SERVER_TICKS_PER_SECOND
                for effect in geometry.user_status_effects
                if "summon_visualizer" in effect.resource_id.casefold()
            ),
            default=definition.lifetime_seconds,
        )
        location_visual_effects = (
            geometry.location_visual_effects
            or definition.location_visual_effects
        )
        visual_lifetime = max(
            visual_lifetime,
            max(
                (
                    effect.duration_ticks
                    / SERVER_TICKS_PER_SECOND
                    for effect in location_visual_effects
                ),
                default=0.0,
            ),
        )
        return replace(
            definition,
            lifetime_seconds=max(
                definition.lifetime_seconds,
                visual_lifetime,
            ),
            location_visual_effects=location_visual_effects,
        )

    def player_skill_location_visuals(
        self,
        skill_index: int,
        *,
        modifier_sources: tuple[object, ...] = (),
    ) -> tuple[VictimStatusEffectDefinition, ...]:
        """Return world-hosted aura visuals for one player skill cast."""

        geometry = self._load_player_skill_geometry(int(skill_index))
        if geometry is None:
            return ()
        if modifier_sources:
            geometry = self._modified_player_skill_geometry(
                geometry, modifier_sources
            )
        return tuple(
            effect
            for effect in geometry.location_visual_effects
            if effect.chance > 0.0
        )

    def player_skill_bullet_location_trail(
        self,
        skill_index: int,
        *,
        modifier_sources: tuple[object, ...] = (),
    ) -> tuple[
        float,
        float,
        float,
        float,
        tuple[VictimStatusEffectDefinition, ...],
        tuple[VictimStatusEffectDefinition, ...],
    ]:
        """Return authored rate/start/range/visuals for projectile trails."""

        geometry = self._load_player_skill_geometry(int(skill_index))
        if geometry is None:
            return 0.0, 0.0, 0.0, 0.0, (), ()
        if modifier_sources:
            geometry = self._modified_player_skill_geometry(
                geometry, modifier_sources
            )
        visuals = tuple(
            effect
            for effect in geometry.bullet_location_visual_effects
            if effect.chance > 0.0 and effect.duration_ticks > 0
        )
        return (
            max(0.0, float(geometry.bullet_location_effect_rate)),
            max(
                0.0,
                float(geometry.bullet_location_effect_start_delay),
                min(
                    (
                        effect.delay_ticks / SERVER_TICKS_PER_SECOND
                        for effect in visuals
                    ),
                    default=0.0,
                ),
            ),
            max(
                0.0,
                float(geometry.projectile_range),
                float(geometry.attack_range),
            ),
            max(0.0, float(geometry.projectile_velocity)),
            visuals,
            tuple(
                effect
                for effect in geometry.bullet_location_effects
                if effect.chance > 0.0
            ),
        )

    def player_trap_definition(
        self,
        skill_index: int,
        *,
        modifier_sources: tuple[object, ...] = (),
    ) -> PlayerTrapDefinition | None:
        """Resolve a skill's talent/item-modified native trap resource."""

        geometry = self._load_player_skill_geometry(int(skill_index))
        if geometry is None:
            return None
        if modifier_sources:
            geometry = self._modified_player_skill_geometry(
                geometry, modifier_sources
            )
        resource_id = str(geometry.trap_on_hit or "").strip()
        if not resource_id:
            return None
        with sqlite3.connect(
            f"file:{self.encounter.static_database_path.as_posix()}?mode=ro",
            uri=True,
        ) as database:
            row = database.execute(
                "SELECT Graphics, StatusEffectId, COALESCE(LifeTime, 0), "
                "COALESCE(TripperStatusEffect, ''), "
                "COALESCE(TripRadius, 0) FROM _Template_Trap "
                "WHERE Id=? LIMIT 1",
                (resource_id,),
            ).fetchone()
            if row is None:
                return None
            effects = list(
                self._load_location_aura_effects(database, str(row[1] or ""))
            )
            if not effects:
                effects.extend(
                    self._load_victim_status_effects(
                        database, str(row[1] or "")
                    )
                )
            effects.extend(
                self._load_victim_status_effects(
                    database, str(row[3] or "")
                )
            )
        return PlayerTrapDefinition(
            resource_id=resource_id,
            graphics=str(row[0] or ""),
            lifetime_seconds=max(0.05, float(row[2] or 0.0)),
            trip_radius=max(0.25, float(row[4] or 0.0)),
            victim_status_effects=tuple({
                effect.resource_id: effect for effect in effects
            }.values()),
        )

    def spawn_player_trap(
        self,
        definition: PlayerTrapDefinition,
        *,
        owner_actor_id: int,
        skill_index: int,
        cast_id: int,
        server_tick: int,
        position: tuple[float, float, float],
        yaw: float,
        player_damage_range: tuple[float, float],
        critical_hit_chance: float,
        critical_damage_multiplier: float,
        modifier_sources: tuple[object, ...] = (),
        now: float | None = None,
    ) -> ActivePlayerTrap:
        current_time = time.perf_counter() if now is None else float(now)
        trap_id = int(self._next_player_trap_id)
        self._next_player_trap_id = (
            (trap_id + 1) & 0xFFFFFFFF or 0x00010000
        )
        trap = ActivePlayerTrap(
            trap_id=trap_id,
            owner_actor_id=int(owner_actor_id),
            skill_index=int(skill_index),
            cast_id=int(cast_id),
            server_tick=int(server_tick) & 0xFFFFFFFF,
            position=tuple(float(value) for value in position),
            yaw=float(yaw),
            definition=definition,
            player_damage_range=tuple(float(value) for value in player_damage_range),
            critical_hit_chance=float(critical_hit_chance),
            critical_damage_multiplier=float(critical_damage_multiplier),
            modifier_sources=tuple(modifier_sources),
            expires_at=current_time + definition.lifetime_seconds,
        )
        self.player_traps[trap_id] = trap
        return trap

    def remove_player_trap(self, trap_id: int) -> ActivePlayerTrap | None:
        return self.player_traps.pop(int(trap_id), None)

    @staticmethod
    def _player_skill_impact_delay(
        geometry: PlayerSkillGeometry,
        attack_speed_multiplier: float,
    ) -> float:
        """Return the authored cast-to-impact delay for a player skill.

        ``HitFrame`` is expressed in the same 25 Hz timeline used by the
        client animations.  Ordinary attacks scale with the effective skill
        speed, while long shifted impacts (Meteor/Lightning Strike) retain
        their authored landing frame.  Keeping this in one helper prevents
        damage and generated world effects from drifting apart.
        """

        timing_scale = (
            max(0.1, min(10.0, float(attack_speed_multiplier)))
            if geometry.scale_skill_timing
            else 1.0
        )
        if geometry.skill_type.casefold() == "shifted":
            return sum(shifted_skill_phases(geometry, attack_speed_multiplier)) / SERVER_TICKS_PER_SECOND
        return max(
            0.0,
            geometry.hit_frame
            / SERVER_TICKS_PER_SECOND
            * geometry.skill_timing_multiplier
            / timing_scale,
        )

    def player_skill_impact_delay_seconds(
        self,
        skill_index: int,
        *,
        attack_speed_multiplier: float = 1.0,
        modifier_sources: tuple[object, ...] = (),
    ) -> float:
        """Resolve the same cast deadline used by spatial hit scheduling."""

        geometry = self._load_player_skill_geometry(int(skill_index))
        if geometry is None:
            return 0.0
        if modifier_sources:
            geometry = self._modified_player_skill_geometry(
                geometry, modifier_sources
            )
        return self._player_skill_impact_delay(
            geometry, attack_speed_multiplier
        )

    def start_player_location_visuals(
        self,
        definitions: tuple[VictimStatusEffectDefinition, ...],
        *,
        position: tuple[float, float, float],
        yaw: float,
        server_tick: int,
        owner_actor_id: int,
        cast_id: int = 0,
        receiver_status_effects: tuple[
            VictimStatusEffectDefinition, ...
        ] = (),
        source_skill_index: int = 0,
        player_damage_range: tuple[float, float] = (1.0, 1.0),
        managed_aura: bool = False,
        source_actor_id: int | None = None,
        now: float | None = None,
    ) -> tuple[PlayerLocationVisual, ...]:
        """Create location visuals and return the live snapshot.

        Command 0x003e is snapshot-shaped: retail repeats every still-live
        location effect whenever the collection changes.  Keeping this state
        in the shared map combat object also lets late entrants receive the
        same authored visual lifecycle.
        """

        current_time = time.perf_counter() if now is None else float(now)
        self.active_player_location_visuals(now=current_time)
        for definition in definitions:
            if (
                definition.chance <= 0.0
                or definition.duration_ticks <= 0
            ):
                continue
            instance_id = self._next_player_location_visual_id
            self._next_player_location_visual_id = (
                instance_id + 1
            ) & 0xFFFFFFFF
            starts_tick = (
                int(server_tick) + int(definition.delay_ticks)
            ) & 0xFFFFFFFF
            expires_tick = (
                starts_tick + int(definition.duration_ticks)
            ) & 0xFFFFFFFF
            # Its OX/OY/OZ turned with the aim (forward is -Z: (-sin, 0, -cos)):
            # Fury of the Dragon's crack opens 2.5 m ahead (OZ:-2.5), not under
            # the warrior ("spawn toujours sous moi", 2026-10-21).
            ox, oy, oz = definition.offset
            cos_yaw, sin_yaw = math.cos(float(yaw)), math.sin(float(yaw))
            self.player_location_visuals[instance_id] = (
                PlayerLocationVisual(
                    instance_id=instance_id,
                    definition=definition,
                    position=(
                        float(position[0]) + ox * cos_yaw + oz * sin_yaw,
                        float(position[1]) + oy,
                        float(position[2]) - ox * sin_yaw + oz * cos_yaw,
                    ),
                    yaw=float(yaw),
                    starts_tick=starts_tick,
                    expires_tick=expires_tick,
                    owner_actor_id=int(owner_actor_id),
                    expires_at=(
                        current_time
                        + (
                            definition.delay_ticks
                            + definition.duration_ticks
                        )
                        / SERVER_TICKS_PER_SECOND
                    ),
                    cast_id=int(cast_id),
                    managed_aura=bool(managed_aura),
                    source_actor_id=source_actor_id,
                    receiver_status_effects=tuple(receiver_status_effects),
                    source_skill_index=int(source_skill_index),
                    player_damage_range=(
                        max(0.0, float(player_damage_range[0])),
                        max(0.0, float(player_damage_range[1])),
                    ),
                )
            )
        return self.active_player_location_visuals(now=current_time)

    def claim_monster_location_visual_cast(
        self, actor_id: int, skill_index: int, server_tick: int,
    ) -> bool:
        """Deduplicate one monster cast's location VFX across map viewers."""

        key = (
            int(actor_id),
            int(skill_index),
            int(server_tick) & 0xFFFFFFFF,
        )
        with self.lock:
            if key in self._monster_location_visual_casts:
                return False
            if len(self._monster_location_visual_casts) >= 4096:
                self._monster_location_visual_casts.clear()
            self._monster_location_visual_casts.add(key)
        return True

    def active_player_location_visuals(
            self, *, now: float | None = None
    ) -> tuple[PlayerLocationVisual, ...]:
        current_time = time.perf_counter() if now is None else float(now)
        for instance_id, visual in tuple(
            self.player_location_visuals.items()
        ):
            if visual.expires_at <= current_time:
                del self.player_location_visuals[instance_id]
        return tuple(
            self.player_location_visuals[instance_id]
            for instance_id in sorted(self.player_location_visuals)
        )

    def apply_player_location_auras(
        self,
        *,
        now: float | None = None,
        server_tick: int = 0,
        player_positions: dict[int, tuple[float, float, float]] | None = None,
    ) -> tuple[
        tuple[DamageResolution, ...],
        tuple[PlayerSupportApplication, ...],
    ]:
        """Apply authored aura children when a monster enters a ground VFX."""

        current_time = time.perf_counter() if now is None else float(now)
        visuals = self.active_player_location_visuals(now=current_time)
        live_visual_ids = {int(visual.instance_id) for visual in visuals}
        living = set(self.living_actor_ids)
        self.player_location_aura_members = {
            key
            for key in self.player_location_aura_members
            if key[0] in live_visual_ids and key[1] in living
        }
        inside_now: set[tuple[int, int]] = set()
        applications = []
        resolutions = []
        owner_auras = {}
        for visual in visuals:
            radius = max(0.0, float(visual.definition.aura_radius))
            # An AuraTargets:Creator ground aura (Banner of War's arena) gives
            # its children to its creator while the creator stands inside it,
            # and takes them back on leaving; they were applied once at the
            # cast for the whole 10 s wherever the warrior went.
            if (
                radius > 0.0
                and visual.definition.aura_targets.casefold() == "creator"
            ):
                position = (player_positions or {}).get(visual.owner_actor_id)
                if position is not None and self._distance(visual.position, position) <= radius:
                    for effect in self._creator_aura_children(visual):
                        key = (visual.owner_actor_id, effect.effect_id)
                        old = owner_auras.get(key)
                        if old is None or visual.expires_at > old[1]:
                            owner_auras[key] = (effect, visual.expires_at)
                continue
            if not visual.receiver_status_effects or radius <= 0.0:
                continue
            if visual.managed_aura and visual.definition.aura_targets.casefold() == "summoner":
                position = (player_positions or {}).get(visual.owner_actor_id)
                if position is not None and self._distance(visual.position, position) <= radius:
                    for effect in visual.receiver_status_effects:
                        key = (visual.owner_actor_id, effect.effect_id)
                        old = owner_auras.get(key)
                        if old is None or visual.expires_at > old[1]:
                            owner_auras[key] = (effect, visual.expires_at)
                continue
            for actor_id in self.living_actor_ids:
                monster = self.monsters[actor_id]
                if self._distance(
                    visual.position, self.monster_positions[actor_id]
                ) > radius + max(0.0, monster.collision_radius):
                    continue
                key = (int(visual.instance_id), int(actor_id))
                inside_now.add(key)
                if key in self.player_location_aura_members:
                    if not visual.managed_aura or self.player_location_aura_refresh_at.get(key, 0.0) > current_time:
                        continue
                self.player_location_aura_refresh_at[key] = current_time + 0.5
                definitions = self.accepted_monster_status_effects(
                    actor_id,
                    tuple(
                        replace(effect, duration_ticks=30)
                        if visual.managed_aura else effect
                        for effect in visual.receiver_status_effects
                    ),
                    now=current_time,
                )
                if not definitions:
                    continue
                self._record_monster_status_effects(
                    actor_id,
                    definitions,
                    now=current_time,
                    source_actor_id=int(visual.owner_actor_id),
                    player_damage_range=visual.player_damage_range,
                    skill_index=int(visual.source_skill_index),
                )
                applications.append(
                    PlayerSupportApplication(
                        source_actor_id=int(visual.owner_actor_id),
                        target_actor_id=int(actor_id),
                        server_tick=int(server_tick) & 0xFFFFFFFF,
                        status_effects=definitions,
                    )
                )
                damage, hit_tags = self.status_effect_immediate_damage(
                    actor_id,
                    definitions,
                    visual.player_damage_range,
                    now=current_time,
                )
                previous = self.monster_health.get(actor_id, 0)
                if damage > 0 and previous > 0:
                    current, killed = _monster_health_after_damage(
                        monster, previous, damage
                    )
                    self.monster_health[actor_id] = current
                    resolutions.append(
                        DamageResolution(
                            target_actor_id=actor_id,
                            attacker_actor_id=int(visual.owner_actor_id),
                            current_health=current,
                            maximum_health=monster.maximum_health,
                            damage=previous - current,
                            killed=killed,
                            position=self.monster_positions[actor_id],
                            server_tick=int(server_tick) & 0xFFFFFFFF,
                            hit_tags=hit_tags,
                            skill_index=int(visual.source_skill_index),
                            victim_status_effects=definitions,
                            source_actor_id=int(visual.owner_actor_id),
                        )
                    )
        for key, (effect, expires_at) in owner_auras.items():
            previous = self.player_location_owner_auras.get(key)
            if previous is None or expires_at > previous[1] + 0.04:
                applications.append(PlayerSupportApplication(
                    key[0], key[0], int(server_tick),
                    (replace(effect, duration_ticks=max(1, round((expires_at-current_time) * SERVER_TICKS_PER_SECOND))),),
                ))
        for key, (effect, _expires_at) in self.player_location_owner_auras.items():
            if key not in owner_auras:
                applications.append(PlayerSupportApplication(key[0], key[0], int(server_tick), (effect,), remove=True))
        self.player_location_owner_auras = owner_auras
        self.player_location_aura_refresh_at = {
            key: stamp for key, stamp in self.player_location_aura_refresh_at.items()
            if key in inside_now
        }
        # Leaving a segment permits a later re-entry to acquire its aura.
        self.player_location_aura_members.intersection_update(inside_now)
        self.player_location_aura_members.update(inside_now)
        return tuple(resolutions), tuple(applications)

    def _monster_location_aura_programs(
        self, definition: VictimStatusEffectDefinition,
    ) -> tuple[tuple[float, tuple, tuple], ...]:
        """A monster ground aura's children: (interval, start, tick) each.

        start/tick are SEE: _status_health_programs entries. The children are
        the aura's AuraEffectId list, read with the aura's own $N values
        (BossSmithEarthqake_aura_01 $2 -1.5 -> BossSmithEarthqake_dmg
        CurrHealthPointsDmg:$2,relative,Fire).
        """

        key = (definition.resource_id, tuple(definition.values))
        cached = self._monster_location_aura_program_cache.get(key)
        if cached is not None:
            return cached
        path = self.encounter.static_database_path
        with sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT COALESCE(AuraEffectId, '') FROM _Template_StatusEffect "
                "WHERE Id=? LIMIT 1",
                (definition.resource_id,),
            ).fetchone()
        programs = []
        for child_id in str(row[0] if row else "").split(";"):
            effect_id = self._status_effect_ids.get(child_id.strip())
            if effect_id is None:
                continue
            child = replace(
                definition, resource_id=child_id.strip(), effect_id=effect_id,
            )
            interval, _tick, _start = self._status_effect_runtime_program(
                effect_id
            )
            start = self._status_health_programs(child, ticks=False)
            tick = self._status_health_programs(child, ticks=True)
            if start or tick:
                programs.append((
                    max(1.0 / SERVER_TICKS_PER_SECOND, float(interval)),
                    start,
                    tick,
                ))
        self._monster_location_aura_program_cache[key] = tuple(programs)
        return tuple(programs)

    def _status_health_programs(
        self,
        definition: VictimStatusEffectDefinition,
        *,
        ticks: bool,
    ) -> tuple[tuple[str, float, str], ...]:
        """Health-taking instructions of a status effect's program.

        ("damage", coefficient, type): CurrHealthPointsDmg relative, x the
        source's damage basis; ("absolute", amount, type): CurrHealthPointsDmg
        without relative, or a negative absolute CurrHealthPoints
        (Boss02Skill04Damage -1000); ("health", fraction, type): a negative
        relative CurrHealthPoints, that fraction of the victim's maximum
        health (Boss01_b_FloodDamageEffect CurrHealthPoints:-0.1,relative).
        """

        programs = [
            ("damage" if relative else "absolute", coefficient, damage_type)
            for coefficient, damage_type, relative
            in self._status_damage_programs(definition, ticks=ticks)
        ]
        _interval, tick_modifiers, start_modifiers = (
            self._status_effect_runtime_program(definition.effect_id)
        )
        for encoded in str(
            tick_modifiers if ticks else start_modifiers
        ).split(";"):
            kind, separator, arguments_raw = encoded.partition(":")
            if not separator or kind.strip().casefold() != "currhealthpoints":
                continue
            arguments = tuple(v.strip() for v in arguments_raw.split(","))
            amount = self._modifier_amount(
                None, arguments[0], definition.values
            )
            if amount is None or amount >= 0.0:
                continue
            damage_type = next(
                (v for v in arguments[1:] if v.casefold() in DAMAGE_TYPE_IDS),
                "Physical",
            )
            if any(v.casefold() == "relative" for v in arguments[1:]):
                programs.append(
                    ("health", min(1.0, -float(amount)), damage_type)
                )
            else:
                programs.append(("absolute", -float(amount), damage_type))
        return tuple(programs)

    def resolve_monster_location_auras(
        self,
        player_position: tuple[float, float, float],
        *,
        player_actor_id: int,
        now: float | None = None,
        incoming_damage_multiplier: (
            float | dict[str, float] | tuple[tuple[str, float], ...]
        ) = 1.0,
    ) -> tuple[DamageResolution, ...]:
        """Damage one player standing in a monster skill's ground auras.

        CONTRACT: a location effect a monster's skill left (LocationStatus
        Effects, created as a PlayerLocationVisual owned by the monster) with
        AuraTargets Enemies and an AuraRadius strikes a player within that
        radius of it: its children's StartModifiers once when the player is
        first inside, their TickModifiers every StatusEffectTickRate seconds
        while inside, from the effect's start (after its DE) to its end.
        Each strike is mitigated by the player's defense for its damage type
        and sent as a Hit with the skill's index. Only while the monster
        lives. A player in one of its safe circles is spared
        (LOCATION_AURA_SAFE_CIRCLES: Khalys's flood).
        EVIDENCE (data): Boss01_b_Skill03 Boss01_b_FloodDamage (20.84 m, D 10,
        child CurrHealthPoints -0.1 relative every 0.25 s);
        BossSmithEarthqake's five _aura_0N, DE 1..5 s along OZ -2.5..-12.5;
        Boss07BalorFirebreath (18 m, $0 -1.5 every 0.5 s).
        UNVERIFIED: whether retail mitigates the percent-of-health floods.
        """

        current_time = time.perf_counter() if now is None else float(now)
        player_actor_id = int(player_actor_id)
        player_world = self.encounter.coordinate_frame.wire_to_world(
            player_position
        )
        live = set()
        resolutions = []
        health = self.player_health.setdefault(
            player_actor_id,
            [self.player_maximum_health, self.player_maximum_health],
        )
        inside = []
        for visual in self.active_player_location_visuals(now=current_time):
            owner = int(visual.owner_actor_id)
            monster = self.monsters.get(owner)
            definition = visual.definition
            radius = float(definition.aura_radius)
            if (
                monster is None
                or radius <= 0.0
                or definition.aura_targets.strip().casefold() != "enemies"
            ):
                continue
            starts_at = (
                visual.expires_at
                - definition.duration_ticks / SERVER_TICKS_PER_SECOND
            )
            if (
                self.monster_health.get(owner, 0) <= 0
                or current_time + 1e-9 < starts_at
                or self._distance(visual.position, player_world) > radius
                + PLAYER_PROJECTILE_COLLISION_RADIUS
                or self._in_location_aura_safe_circle(visual, player_world)
            ):
                continue
            inside.append((
                int(visual.instance_id), owner, definition, starts_at,
                visual.expires_at, int(visual.starts_tick),
                int(visual.source_skill_index),
            ))
        # A monster's rotating star beam strikes a player its beam touches.
        for beam in tuple(self.monster_beam_auras.values()):
            if beam.expires_at <= current_time:
                del self.monster_beam_auras[beam.instance_id]
                continue
            if (
                self.monster_health.get(beam.owner_actor_id, 0) <= 0
                or current_time + 1e-9 < beam.starts_at
                or not beam.touches(
                    player_world, current_time,
                    PLAYER_PROJECTILE_COLLISION_RADIUS,
                )
            ):
                continue
            inside.append((
                beam.instance_id, beam.owner_actor_id, beam.definition,
                beam.starts_at, beam.expires_at, beam.starts_tick,
                beam.skill_index,
            ))
        for (
            instance_id, owner, definition, starts_at, expires_at,
            starts_tick, skill_index,
        ) in inside:
            for index, (interval, start, tick) in enumerate(
                self._monster_location_aura_programs(definition)
            ):
                key = (instance_id, player_actor_id, index)
                live.add(key)
                strikes = []
                next_at = self._monster_location_aura_ticks.get(key)
                if next_at is None:
                    strikes.append(start)
                    next_at = starts_at + interval
                    while next_at + interval <= current_time + 1e-9:
                        next_at += interval  # entered late: no back-ticks
                while (
                    tick
                    and next_at <= current_time + 1e-9
                    and next_at <= expires_at + 1e-9
                    and len(strikes) < 4
                ):
                    strikes.append(tick)
                    next_at += interval
                self._monster_location_aura_ticks[key] = next_at
                for programs in strikes:
                    if not programs or health[0] <= 0:
                        continue
                    damage = self._monster_aura_program_damage(
                        owner, programs, health[1],
                        incoming_damage_multiplier, current_time,
                    )
                    previous = health[0]
                    current = max(0, previous - damage)
                    if current == previous:
                        continue
                    self.note_player_attacked(player_actor_id, now=current_time)
                    health[0] = current
                    if player_actor_id == self.player_actor_id:
                        self.player_current_health = current
                    resolutions.append(DamageResolution(
                        target_actor_id=player_actor_id,
                        attacker_actor_id=owner,
                        current_health=current,
                        maximum_health=health[1],
                        damage=previous - current,
                        killed=current == 0,
                        position=player_world,
                        server_tick=(
                            starts_tick + round(
                                (current_time - starts_at)
                                * SERVER_TICKS_PER_SECOND
                            )
                        ) & 0xFFFFFFFF,
                        hit_tags=(0,),
                        skill_index=skill_index,
                        victim_status_effects=(),
                    ))
        for key in tuple(self._monster_location_aura_ticks):
            if key[1] == player_actor_id and key not in live:
                del self._monster_location_aura_ticks[key]
        return tuple(resolutions)

    def _start_monster_beam_auras(
        self, actor_id: int, attack, centre, yaw: float, now: float,
    ) -> None:
        """Arm the rotating star beams among a monster cast's user effects.

        CONTRACT: from the cast's start (+ the effect's DE) for its D, at the
        caster's place and aim (the beam skill keeps it there: SkillUnblock
        180 frames > D 4 s). SEE: MonsterBeamAura, _star_beam_aura.
        """

        for effect in attack.user_status_effects:
            if effect.chance <= 0.0 or effect.duration_ticks <= 0:
                continue
            beam = _star_beam_aura(
                str(self.encounter.static_database_path), effect.resource_id,
            )
            if beam is None:
                continue
            radius, width, start_angle, speed, spacing = beam
            starts_at = now + effect.delay_ticks / SERVER_TICKS_PER_SECOND
            instance_id = self._next_monster_beam_id
            self._next_monster_beam_id = (instance_id + 1) & 0x7FFFFFFF or 0x40000000
            self.monster_beam_auras[instance_id] = MonsterBeamAura(
                instance_id=instance_id,
                owner_actor_id=int(actor_id),
                definition=effect,
                centre=tuple(float(v) for v in centre[:3]),
                yaw=float(yaw),
                starts_at=starts_at,
                expires_at=starts_at
                + effect.duration_ticks / SERVER_TICKS_PER_SECOND,
                starts_tick=(
                    int(attack.server_tick) + int(effect.delay_ticks)
                ) & 0xFFFFFFFF,
                skill_index=int(attack.skill_index),
                radius=radius,
                width=width,
                start_angle=start_angle,
                rotation_speed=speed,
                spacing=spacing,
            )

    def _queue_monster_status_minions(
        self, actor_id: int, attack, centre, yaw: float, now: float,
    ) -> None:
        """Summon the adds a monster cast's user effects name (Minion:).

        CONTRACT: one monster per Minion instruction at its authored (x, z)
        around the caster, turned by its aim as OX/OZ are, after the effect's
        DE. Templates with a LifeTime are left out: the server cannot yet end
        them (Khalys's bullet portals, 3.9 s; her illusions, 24 s).
        EVIDENCE (data): boss_khalys_base_andermight_flood_activate_summon
        (difficulty 5-10) boss_khalys_andermight_creature,DE:6.0: Minion:
        pw015_boss_add_anderworld_creature,2,(0,0,16) (0,0,-16) (16,0,0)
        (-16,0,0).
        UNVERIFIED: the Minion's second argument (1 or 2 here, 0 on other
        spawners) is read as no count; one add per instruction.
        """

        monster = self.monsters.get(int(actor_id))
        if monster is None:
            return
        cos_yaw, sin_yaw = math.cos(float(yaw)), math.sin(float(yaw))
        static_path = str(self.encounter.static_database_path)
        for effect in attack.user_status_effects:
            if effect.chance <= 0.0:
                continue
            by_template: dict[str, list[tuple[float, float, float]]] = {}
            for template_id, _amount, (ox, oy, oz, _turn) in (
                self.player_status_minion_specs(effect)
            ):
                if _monster_template_lifetime(static_path, template_id) != 0.0:
                    continue
                by_template.setdefault(template_id, []).append((
                    float(centre[0]) + ox * cos_yaw + oz * sin_yaw,
                    float(centre[1]) + oy,
                    float(centre[2]) - ox * sin_yaw + oz * cos_yaw,
                ))
            for template_id, positions in by_template.items():
                self._delayed_monster_summons.append((
                    now + effect.delay_ticks / SERVER_TICKS_PER_SECOND,
                    MonsterSummonRequest(
                        caster_actor_id=int(actor_id),
                        template_id=template_id,
                        amount=len(positions),
                        limit=0,
                        position=tuple(float(v) for v in centre[:3]),
                        level=int(monster.level),
                        positions=tuple(positions),
                    ),
                ))

    def release_due_monster_summons(self, now: float | None = None) -> None:
        """Move the delayed status-effect summons that are due to the queue."""

        current_time = time.perf_counter() if now is None else float(now)
        due = [r for at, r in self._delayed_monster_summons if at <= current_time]
        if due:
            self._delayed_monster_summons = [
                (at, r) for at, r in self._delayed_monster_summons
                if at > current_time
            ]
            self.pending_monster_summons.extend(
                r for r in due if r.caster_actor_id in self.living_actor_ids
            )

    def monster_activated_location_visuals(self, attack) -> tuple[
        tuple[tuple[VictimStatusEffectDefinition, ...],
              tuple[float, float, float], float, int, float], ...
    ]:
        """Ground effects a helper casts when a monster's ally aura wakes it.

        Returns (visuals, position, yaw, skill index, delay seconds) per
        authored helper within the aura's radius of the caster.
        SEE: _ally_aura_skill_unlocks (Khalys's andermight flood).
        """

        result = []
        centre = attack.position
        for effect in attack.user_status_effects:
            if effect.chance <= 0.0:
                continue
            for radius, skill_index, hit_frame, spawns in _ally_aura_skill_unlocks(
                str(self.encounter.static_database_path),
                str(self.encounter.map_id), effect.resource_id,
            ):
                visuals = self.player_skill_location_visuals(skill_index)
                if not visuals:
                    continue
                for position, yaw in spawns:
                    if math.hypot(
                        position[0] - float(centre[0]),
                        position[2] - float(centre[2]),
                    ) > radius:
                        continue
                    result.append((
                        visuals, position, yaw, skill_index,
                        (effect.delay_ticks + max(0, hit_frame))
                        / SERVER_TICKS_PER_SECOND,
                    ))
        return tuple(result)

    @staticmethod
    def _in_location_aura_safe_circle(
        visual: PlayerLocationVisual,
        world: tuple[float, float, float],
    ) -> bool:
        """Whether a point stands in one of a ground aura's safe circles.

        SEE: LOCATION_AURA_SAFE_CIRCLES. The circles' local (x, z) turn with
        the visual's yaw as start_player_location_visuals turns OX/OZ.
        """

        circles = LOCATION_AURA_SAFE_CIRCLES.get(
            visual.definition.resource_id.strip().casefold()
        )
        if circles is None:
            return False
        centres, radius = circles
        cos_yaw, sin_yaw = math.cos(visual.yaw), math.sin(visual.yaw)
        dx = float(world[0]) - float(visual.position[0])
        dz = float(world[2]) - float(visual.position[2])
        return any(
            math.hypot(
                dx - (x * cos_yaw + z * sin_yaw),
                dz - (-x * sin_yaw + z * cos_yaw),
            ) <= radius
            for x, z in centres
        )

    def _monster_aura_program_damage(
        self,
        actor_id: int,
        programs: tuple[tuple[str, float, str], ...],
        maximum_health: int,
        incoming_damage_multiplier,
        now: float,
    ) -> int:
        """One strike of SEE: _status_health_programs from a monster."""

        monster = self.monsters[actor_id]
        basis = (
            (monster.minimum_damage + monster.maximum_damage) / 2
            * self._monster_skill_damage_multiplier(actor_id, now=now)
        )
        if isinstance(incoming_damage_multiplier, (int, float)):
            defense = lambda _type: max(0.0, float(incoming_damage_multiplier))
        else:
            by_type = {
                str(name).casefold(): max(0.0, float(value))
                for name, value in dict(incoming_damage_multiplier).items()
            }
            defense = lambda damage_type: by_type.get(
                "darkmagic" if damage_type.casefold() == "andermagic"
                else damage_type.casefold(),
                1.0,
            )
        total = 0.0
        for kind, amount, damage_type in programs:
            raw = (
                basis * amount if kind == "damage"
                else maximum_health * amount if kind == "health"
                else amount
            )
            total += raw * defense(damage_type)
        return max(1, int(round(total))) if total > 0.0 else 0

    def _creator_aura_children(
        self, visual
    ) -> tuple[VictimStatusEffectDefinition, ...]:
        geometry = self._load_player_skill_geometry(int(visual.source_skill_index))
        if geometry is None:
            return ()
        return tuple(
            effect for effect in geometry.location_owner_status_effects
            if effect.creator_aura == visual.definition.resource_id
            and effect.chance > 0.0
        )

    def expire_player_location_visuals_for_owner(
            self, owner_actor_id: int) -> tuple[PlayerLocationVisual, ...]:
        """Remove one departed caster's effects and return the live snapshot."""

        owner_actor_id = int(owner_actor_id)
        self.player_location_visuals = {
            instance_id: visual
            for instance_id, visual in self.player_location_visuals.items()
            if int(visual.owner_actor_id) != owner_actor_id
        }
        return self.active_player_location_visuals()

    def expire_player_location_visuals_matching(
        self,
        owner_actor_id: int,
        resource_ids: tuple[str, ...] | set[str] | frozenset[str],
        *,
        cast_id: int | None = None,
    ) -> tuple[PlayerLocationVisual, ...] | None:
        """Remove one summon/category visual without clearing other casts.

        Command 0x003e is a complete location-effect snapshot.  A turret
        crossing its owner leash triggers ``SummonsDead`` immediately, so its
        summon visualizer must disappear from that snapshot before the actor's
        authored ``DespawnDuration`` finishes.  Matching by owner and resource
        keeps simultaneous Tesla/Combat/Machine-Gun visuals independent.
        """

        owner_actor_id = int(owner_actor_id)
        wanted = {
            str(value).strip().casefold()
            for value in resource_ids
            if str(value).strip()
        }
        if not wanted:
            return None
        removed = False
        remaining = {}
        for instance_id, visual in self.player_location_visuals.items():
            if (
                int(visual.owner_actor_id) == owner_actor_id
                and visual.definition.resource_id.casefold() in wanted
                and (
                    cast_id is None
                    or int(visual.cast_id) == int(cast_id)
                )
            ):
                removed = True
                continue
            remaining[instance_id] = visual
        if not removed:
            return None
        self.player_location_visuals = remaining
        return self.active_player_location_visuals()

    def _allocate_player_summon_actor_id(self) -> int:
        occupied = set(self.monsters) | set(self.player_summons)
        for _ in range(PLAYER_SUMMON_ACTOR_CAPACITY):
            ordinal = self._next_player_summon_ordinal
            self._next_player_summon_ordinal = (
                ordinal + 1
            ) % PLAYER_SUMMON_ACTOR_CAPACITY
            actor_id = PLAYER_SUMMON_ACTOR_BASE + ordinal
            if actor_id not in occupied:
                return actor_id
        raise RuntimeError("player summon actor-id capacity exhausted")

    def _allocate_player_summon_aura_instance_id(self) -> int:
        instance_id = self._next_player_summon_aura_instance_id
        self._next_player_summon_aura_instance_id = (
            instance_id + 1
        ) & 0xFFFFFFFF
        if self._next_player_summon_aura_instance_id == 0:
            self._next_player_summon_aura_instance_id = 0x00030000
        return instance_id

    def spawn_player_summon(
        self,
        definition: PlayerSummonDefinition,
        *,
        owner_actor_id: int,
        position: tuple[float, float, float],
        yaw: float,
        level: int,
        server_tick: int,
        cast_id: int = 0,
        player_damage_range: tuple[float, float],
        modifier_sources: tuple[object, ...] = (),
        now: float | None = None,
    ) -> PlayerSummonSpawn:
        """Create friendly summon actors and enforce the authored category cap."""

        current_time = time.perf_counter() if now is None else float(now)
        owner_actor_id = int(owner_actor_id)
        existing = sorted(
            (
                summon
                for summon in self.player_summons.values()
                if (
                    summon.owner_actor_id == owner_actor_id
                    and summon.definition.category == definition.category
                )
            ),
            key=lambda summon: (summon.created_at, summon.actor_id),
        )
        remove_count = max(
            0,
            len(existing) + definition.amount - definition.limit,
        )
        discarded_summons = existing[:remove_count]
        discarded = tuple(
            actor_id
            for summon in discarded_summons
            for actor_id in summon.client_actor_ids
        )
        for summon in discarded_summons:
            self.expire_player_summon(summon.actor_id)

        grounded = tuple(float(value) for value in position)
        try:
            grounded = self.encounter.navigation.project_position(
                grounded,
                max_horizontal_distance=2.0,
            )
        except (NavigationError, CollisionTerrainError, ValueError):
            pass
        offset = definition.spawn_offset
        spawn_yaw = float(yaw)
        if getattr(definition, 'fixed_direction', False):
            spawn_yaw += math.radians(offset[3])
        grounded = (
            grounded[0] + offset[0],
            grounded[1] + offset[1],
            grounded[2] + offset[2],
        )
        created = []
        for ordinal in range(definition.amount):
            actor_id = self._allocate_player_summon_actor_id()
            initial_aura_instance_id = (
                self._allocate_player_summon_aura_instance_id()
                if definition.aura is not None
                else None
            )
            # Multi-summon skills (notably Wild Pack) must materialize
            # distinct actors. The old 0.15-unit offset overlapped all three
            # models/capsules and made three actors look like one wolf.
            if definition.amount > 1:
                angle = (
                    float(yaw)
                    + 2.0 * math.pi * ordinal / definition.amount
                )
                summon_position = (
                    grounded[0] + math.cos(angle),
                    grounded[1],
                    grounded[2] + math.sin(angle),
                )
                try:
                    summon_position = (
                        self.encounter.navigation.project_position(
                            summon_position,
                            max_horizontal_distance=2.0,
                        )
                    )
                except (
                    NavigationError,
                    CollisionTerrainError,
                    ValueError,
                ):
                    pass
            else:
                summon_position = grounded
            spawn_group = load_player_summon_spawn(
                self.encounter.static_database_path,
                definition.client_monster_template_id,
                actor_id,
                owner_actor_id,
                summon_position,
                level=max(1, int(level)),
                server_tick=server_tick,
                yaw=spawn_yaw,
                # The summon scales with its owner. Map-difficulty modifiers
                # apply to hostile actors, not player-owned constructions.
                scaling_difficulty=0,
                map_companion=bool(getattr(definition, "map_companion", False)),
            )
            spawn_group = add_player_summon_aura_to_spawn(
                spawn_group,
                definition.aura,
                owner_actor_id=owner_actor_id,
                instance_id=initial_aura_instance_id,
            )
            expires_at = (
                current_time + definition.lifetime_seconds
                if definition.lifetime_seconds > 0.0
                else None
            )
            if getattr(definition, "map_companion", False):
                monster = next(c.value for c in spawn_group.spawn_batch.commands
                               if isinstance(c, NewMonsterServerCommand))
                stats = {stat.kind: stat.value for stat in monster.stats}
                player_damage_range = (stats[11], stats[12])
            summon = PlayerSummonActor(
                actor_id=actor_id,
                owner_actor_id=owner_actor_id,
                definition=definition,
                spawn_group=spawn_group,
                position=summon_position,
                yaw=spawn_yaw,
                created_at=current_time,
                expires_at=expires_at,
                player_damage_range=(
                    max(0.0, float(player_damage_range[0])),
                    max(0.0, float(player_damage_range[1])),
                ),
                modifier_sources=tuple(modifier_sources),
                initial_aura_instance_id=initial_aura_instance_id,
                cast_id=int(cast_id),
            )
            self.player_summons[actor_id] = summon
            spawned_monster = next(
                command.value for command in spawn_group.spawn_batch.commands
                if isinstance(command, NewMonsterServerCommand)
            )
            self.player_summon_health[actor_id] = [
                int(spawned_monster.field_88), int(spawned_monster.field_90),
            ]
            if definition.aura is not None:
                # NewMonster carries the same initial aura instance as the
                # preparation packet, shifted to spawn_tick + 1. Retail then
                # publishes a newly allocated rolling lease every game tick.
                self.last_player_summon_aura_tick[actor_id] = (
                    int(server_tick) + 1
                ) & 0xFFFFFFFF
            # First attacks use an absolute readiness deadline, independent
            # of haste/slow. The cooldown starts only after an actual attack.
            self.last_player_summon_playback_at[actor_id] = (
                current_time
                - definition.attack_playback_interval_seconds
                - 1e-6
            )
            self.last_player_summon_support_at[actor_id] = (
                current_time
                - definition.heal_interval_seconds
                - 1e-6
            )
            created.append(summon)
        return PlayerSummonSpawn(tuple(created), discarded)

    def expire_player_summon(
        self, actor_id: int, *, land_fired_bullets: bool = False,
    ) -> bool:
        actor_id = int(actor_id)
        removed = self.player_summons.pop(actor_id, None)
        self.player_location_visuals = {
            key: visual for key, visual in self.player_location_visuals.items()
            # Older location visuals (and test stand-ins) have no source.
            if getattr(visual, "source_actor_id", None) != actor_id
        }
        existed = removed is not None
        if removed is not None and getattr(removed.definition, "map_companion", False):
            self.map_companion_spawns.discard((removed.owner_actor_id, removed.definition.monster_template_id))
        self.player_summon_health.pop(actor_id, None)
        self.player_summon_heat_hits.pop(actor_id, None)
        self.player_summon_overheated_until.pop(actor_id, None)
        self.monster_status_effects.pop(actor_id, None)
        self.monster_status_immunities.pop(actor_id, None)
        self._monster_action_lock_cache.pop(actor_id, None)
        for key in tuple(self.monster_status_damage):
            if key[0] == actor_id:
                self.monster_status_damage.pop(key, None)
        for monster_actor_id, attack in self.pending_monster_attack_items():
            if int(attack.target_actor_id) == actor_id:
                self._remove_pending_monster_attack(monster_actor_id, attack)
        pending = self.pending_player_summon_attacks.pop(actor_id, None)
        # A bullet the turret already fired flies on and lands after the
        # turret's lifetime ends. Dropping it with the turret left the last
        # shot of every Combat Turret visibly striking without damage
        # ("touche parfois l'ennemi mais l'attaque n'est pas detectee").
        # Only the lifetime end keeps it: a round reset, a kill or a
        # replacement still discards everything the summon owned.
        if (
            land_fired_bullets
            and removed is not None
            and pending is not None
            and pending.attack.command_id
            in {BULLET_SKILL_COMMAND, TARGET_BULLET_SKILL_COMMAND}
        ):
            self.orphaned_player_summon_attacks[actor_id] = (removed, pending)
        self.last_player_summon_attack_at.pop(actor_id, None)
        self.last_player_summon_playback_at.pop(actor_id, None)
        self.last_player_summon_support_at.pop(actor_id, None)
        self.started_player_summon_effect_ids.pop(actor_id, None)
        self.last_player_summon_aura_tick.pop(actor_id, None)
        return existed

    def due_player_summon_aura_refreshes(
        self,
        *,
        server_tick: int,
    ) -> tuple[StatusEffectServerCommand, ...]:
        """Refresh active construction auras on the retail game clock."""

        current_tick = int(server_tick) & 0xFFFFFFFF
        commands = []
        for actor_id, summon in tuple(self.player_summons.items()):
            aura = summon.definition.aura
            if aura is None:
                continue
            previous_tick = self.last_player_summon_aura_tick.get(
                actor_id,
                (current_tick - 1) & 0xFFFFFFFF,
            )
            elapsed = (current_tick - previous_tick) & 0xFFFFFFFF
            # A value in the upper half of uint32 means the current chase
            # clock is still before the spawn snapshot's tick, not that four
            # billion ticks elapsed across a wrap.
            if (
                elapsed < SUMMON_AURA_RENEW_INTERVAL_TICKS
                or elapsed >= 0x80000000
            ):
                continue
            command = player_summon_aura_refresh_command(
                aura,
                actor_id=actor_id,
                owner_actor_id=summon.owner_actor_id,
                position=summon.position,
                yaw=summon.yaw,
                server_tick=current_tick,
                instance_id=(
                    self._allocate_player_summon_aura_instance_id()
                ),
            )
            self.last_player_summon_aura_tick[actor_id] = current_tick
            commands.append(command)
        return tuple(commands)

    def expire_player_summons_for_owner(
        self, owner_actor_id: int
    ) -> tuple[int, ...]:
        summons = tuple(
            summon
            for summon in self.player_summons.values()
            if summon.owner_actor_id == int(owner_actor_id)
        )
        actor_ids = tuple(
            actor_id
            for summon in summons
            for actor_id in summon.client_actor_ids
        )
        for summon in summons:
            self.expire_player_summon(summon.actor_id)
        return actor_ids

    def player_summons_outside_owner_range(
        self,
        player_positions: dict[int, tuple[float, float, float]],
    ) -> tuple[PlayerSummonActor, ...]:
        """Return constructions whose owner crossed the retail leash.

        The supplied retail Tesla trace unregisters the construction when its
        owner reaches the same approximately 40-unit horizontal boundary used
        by local monster simulation. Terrain elevation is deliberately
        excluded so stairs and cliffs do not shorten that gameplay radius.
        Missing owner transforms are ignored; disconnect and map-transfer
        cleanup own those cases.
        """

        outside = []
        for summon in self.player_summons.values():
            owner_position = player_positions.get(summon.owner_actor_id)
            if owner_position is None:
                continue
            distance = math.hypot(
                float(owner_position[0]) - float(summon.position[0]),
                float(owner_position[2]) - float(summon.position[2]),
            )
            if distance > summon.definition.owner_despawn_range:
                outside.append(summon)
        return tuple(
            sorted(outside, key=lambda summon: summon.actor_id)
        )

    def advance_player_summons(
        self,
        *,
        elapsed_seconds: float,
        server_tick: int,
        now: float | None = None,
        player_positions: dict[int, tuple[float, float, float]] | None = None,
    ) -> tuple[MonsterMovement, ...]:
        '''Move mobile player minions toward nearby hostile actors.'''

        current_time = time.perf_counter() if now is None else float(now)
        elapsed = max(0.0, min(0.5, float(elapsed_seconds)))
        movements = []
        for actor_id, summon in tuple(self.player_summons.items()):
            def stop_if_moving(value):
                if not value.moving:
                    return value
                stopped = replace(value, moving=False)
                self.player_summons[actor_id] = stopped
                movements.append(
                    MonsterMovement(
                        actor_id,
                        stopped.position,
                        self.encounter.coordinate_frame.world_to_wire(
                            stopped.position
                        ),
                        stopped.yaw,
                        False,
                        int(server_tick) & 0xFFFFFFFF,
                    )
                )
                return stopped

            definition = summon.definition
            if getattr(definition, "map_companion", False):
                from dso_server.gameplay.map_companions import companion_combat_phase
                definition = companion_combat_phase(self.encounter.static_database_path,
                                                     self.encounter.map_id, definition, summon.position)
                if definition != summon.definition:
                    summon = replace(summon, definition=definition)
                    self.player_summons[actor_id] = summon
            if (
                definition.stationary
                or getattr(definition, "movement_locked", False)
                or current_time < summon.spawn_ready_at
                or self.player_summon_health.get(actor_id, [0])[0] <= 0
                or self._monster_actions_disabled(actor_id, now=current_time)
                or definition.move_speed <= 0.0
                or definition.attack_skill_database_index is None
            ):
                stop_if_moving(summon)
                continue
            owner = (player_positions or {}).get(summon.owner_actor_id)
            follows = bool(getattr(definition, "follows_owner", False))
            candidates = tuple(
                (
                    self._distance(
                        summon.position, self.monster_positions[target_id]
                    ),
                    target_id,
                )
                for target_id in self.living_actor_ids
                if target_id not in self.returning_home_actor_ids
                and (not follows or owner is None or
                     self._distance(owner, self.monster_positions[target_id]) <= definition.aggro_range)
            )
            nearest = min(candidates) if candidates else None
            geometry = self._load_player_skill_geometry(definition.attack_skill_database_index)
            if nearest is not None and (definition.aggro_range <= 0.0 or nearest[0] <= definition.aggro_range):
                distance, target_actor_id = nearest
                target = self.monster_positions[target_actor_id]
                # Within its attack's reach it stops, follower or not: a
                # ranged summon shoots from where it stands instead of
                # walking up to 2 m of its target.
                stop_distance = max(0.75, min(
                    geometry.attack_range * 0.8 if geometry is not None else 2.0,
                    geometry.attack_range if geometry is not None else 1.5))
            elif follows and owner is not None:
                target = owner
                distance = self._distance(summon.position, target)
                stop_distance = (
                    4.0
                    if str(getattr(definition, "category", "")).casefold()
                    == "mercenary"
                    else 2.5
                )
            else:
                stop_if_moving(summon)
                continue
            patrol = getattr(definition, "patrol_points", ())
            if (follows and patrol and nearest is not None
                    and self.monsters[nearest[1]].rank_id.casefold().startswith("rank_boss")
                    and nearest[0] <= definition.aggro_range):
                # Authored CompanionStrategy paths place allies inside the
                # arena's behavior-change aura before their boss-facing cast.
                index = summon.patrol_index
                if self._distance(summon.position, patrol[index]) <= 0.80 and index + 1 < len(patrol):
                    index += 1
                    summon = replace(summon, patrol_index=index)
                    self.player_summons[actor_id] = summon
                target = patrol[index]
                distance = self._distance(summon.position, target)
                stop_distance = 0.75
            if distance <= stop_distance + 1e-6:
                stop_if_moving(summon)
                continue
            dx = target[0] - summon.position[0]
            dz = target[2] - summon.position[2]
            horizontal = max(1e-6, math.hypot(dx, dz))
            travel = min(
                distance - stop_distance,
                definition.move_speed * elapsed
                * self._summon_status_scale(actor_id, "Speed", "Movement", now=current_time),
            )
            candidate = (
                summon.position[0] + dx / horizontal * travel,
                summon.position[1],
                summon.position[2] + dz / horizontal * travel,
            )
            try:
                if follows:
                    candidate = self.encounter.navigation.advance_toward(
                        summon.position, target, travel)
                else:
                    candidate = self.encounter.navigation.project_position(
                        candidate, max_horizontal_distance=2.0
                    )
                if self.encounter.terrain is not None:
                    candidate = self.encounter.terrain.project_position(
                        candidate, reference_height=summon.position[1]
                    )
            except (
                NavigationError, CollisionTerrainError, ValueError
            ):
                stop_if_moving(summon)
                continue
            if self._distance(summon.position, candidate) <= 1e-5:
                stop_if_moving(summon)
                continue
            yaw = math.atan2(-dx, -dz)
            spawn_group = summon.spawn_group.runtime_subset(
                {actor_id}, positions={actor_id: candidate}
            )
            updated = replace(
                summon,
                position=candidate,
                yaw=yaw,
                spawn_group=spawn_group or summon.spawn_group,
                moving=True,
            )
            self.player_summons[actor_id] = updated
            movements.append(
                MonsterMovement(
                    actor_id,
                    candidate,
                    self.encounter.coordinate_frame.world_to_wire(candidate),
                    yaw,
                    True,
                    int(server_tick) & 0xFFFFFFFF,
                )
            )
        return tuple(movements)

    def active_player_summon_taunt(
        self, *, now: float | None = None
    ) -> PlayerSummonActor | None:
        '''Return an active tree whose database aura covers a monster.'''

        taunts = self.active_player_summon_taunts(now=now)
        return taunts[0] if taunts else None

    def active_player_summon_taunts(
        self, *, now: float | None = None
    ) -> tuple[PlayerSummonActor, ...]:
        """Return every live guard/decoy with at least one nearby enemy."""

        current_time = time.perf_counter() if now is None else float(now)
        result = []
        for summon in sorted(
            self.player_summons.values(), key=lambda value: value.actor_id
        ):
            if (
                summon.definition.taunt_radius <= 0.0
                or (
                    summon.expires_at is not None
                    and summon.expires_at <= current_time
                )
            ):
                continue
            if self.player_summon_taunted_actor_ids(summon):
                result.append(summon)
        return tuple(result)

    def player_summon_taunt_assignments(
        self, *, now: float | None = None
    ) -> tuple[tuple[PlayerSummonActor, frozenset[int]], ...]:
        """Assign each nearby hostile to its nearest guard exactly once."""

        summons = self.active_player_summon_taunts(now=now)
        assignments: dict[int, set[int]] = {
            summon.actor_id: set() for summon in summons
        }
        for actor_id in self.living_actor_ids:
            candidates = [
                (
                    self._distance(
                        summon.position, self.monster_positions[actor_id]
                    ),
                    summon.actor_id,
                    summon,
                )
                for summon in summons
                if self._distance(
                    summon.position, self.monster_positions[actor_id]
                ) <= summon.definition.taunt_radius
            ]
            if candidates:
                _distance, summon_id, _summon = min(candidates)
                assignments[summon_id].add(actor_id)
        return tuple(
            (summon, frozenset(assignments[summon.actor_id]))
            for summon in summons
            if assignments[summon.actor_id]
        )

    def player_summon_taunted_actor_ids(
        self, summon: PlayerSummonActor
    ) -> frozenset[int]:
        '''Return only monsters currently inside a summon taunt aura.'''

        radius = max(0.0, float(summon.definition.taunt_radius))
        return frozenset(
            actor_id
            for actor_id in self.living_actor_ids
            if self._distance(
                summon.position, self.monster_positions[actor_id]
            ) <= radius
        )

    def resolve_due_player_summon_taunts(
        self, *, now: float | None = None
    ) -> None:
        '''Consume due hostile attacks aimed at a taunting summon.'''

        current_time = time.perf_counter() if now is None else float(now)
        for actor_id, attack in self.pending_monster_attack_items():
            if current_time < attack.due_at:
                continue
            summon = self.player_summons.get(attack.target_actor_id)
            if summon is not None and summon.definition.taunt_radius > 0.0:
                self._remove_pending_monster_attack(actor_id, attack)

    def active_player_summon_groups(self) -> tuple[MonsterSpawnGroup, ...]:
        return tuple(
            group
            for summon in sorted(
                self.player_summons.values(),
                key=lambda value: value.actor_id,
            )
            for group in summon.spawn_groups
        )

    @staticmethod
    def _consumes_on_max_stack(
        database: sqlite3.Connection,
        resource_id: str,
        start_modifiers: str,
    ) -> bool:
        """Return whether an authored max-stack child stops its parent."""

        program = str(start_modifiers or "")
        if "on:maxstack" not in program.casefold():
            return False
        parent = str(resource_id).strip().casefold()
        for command in program.split(";"):
            if "on:maxstack" not in command.casefold():
                continue
            fields = [part.strip() for part in command.split(",")]
            if len(fields) < 2:
                continue
            operation = fields[0].partition(":")[0].strip().casefold()
            if operation != "actorstatuseffect":
                continue
            child_id = fields[1]
            child_row = database.execute(
                "SELECT COALESCE(StartModifiers, '') "
                "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
                (child_id,),
            ).fetchone()
            if child_row is None:
                continue
            for child_command in str(child_row[0] or "").split(";"):
                operation, separator, arguments = child_command.partition(":")
                if not separator or operation.strip().casefold() != (
                    "stopstatuseffect"
                ):
                    continue
                stopped_id = (
                    arguments.split(",", 1)[0].split("|", 1)[0].strip()
                )
                if stopped_id.casefold() == parent:
                    return True
        return False

    def _status_effect_content(
        self, resource_id: str
    ) -> tuple[frozenset[str], tuple[frozenset[str], float] | None]:
        """Return one effect's groups and the immunity its expiry grants.

        Retail's diminishing returns live in ``StopModifiers``: when
        ``debuff_cc_freeze`` ends it applies ``debuff_cc_stun_immunity``,
        whose ``StatusEffectImmunityGroup`` entries block the groups that
        would re-apply it. Without that step a victim can be re-frozen the
        instant the previous freeze lapses, which is a permanent
        ``ActorFeature:off,Movement,RegularSkills`` for as long as a caster
        keeps stacking.
        """

        cache = self._status_effect_content_cache
        cached = cache.get(resource_id)
        if cached is not None:
            return cached
        groups: frozenset[str] = frozenset()
        immunity: tuple[frozenset[str], float] | None = None
        try:
            path = self.encounter.static_database_path
            with sqlite3.connect(
                f"file:{path.as_posix()}?mode=ro", uri=True
            ) as database:
                row = database.execute(
                    "SELECT COALESCE(Groups, ''), COALESCE(StopModifiers, '') "
                    "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
                    (resource_id,),
                ).fetchone()
                if row is not None:
                    groups = frozenset(
                        token.strip().casefold()
                        for token in str(row[0]).split(";")
                        if token.strip()
                    )
                    immunity = self._parse_stop_immunity(database, str(row[1]))
        except Exception:
            # Content lookup only; a damaged row must not break combat.
            pass
        cache[resource_id] = (groups, immunity)
        return cache[resource_id]

    @staticmethod
    def _parse_stop_immunity(
        database: sqlite3.Connection, stop_modifiers: str
    ) -> tuple[frozenset[str], float] | None:
        """Resolve ``StopModifiers`` into the immunity groups it grants."""

        for command in str(stop_modifiers or "").split(";"):
            fields = [part.strip() for part in command.split(",") if part.strip()]
            if len(fields) < 2:
                continue
            if fields[0].partition(":")[0].strip().casefold() != (
                "actorstatuseffect"
            ):
                continue
            granted_id = fields[1]
            # ``D`` is the PvE duration override; the template value is the
            # fallback. PvP (``DP``) is a separate ruleset and not applied to
            # map monsters.
            duration = None
            for field in fields[2:]:
                name, separator, raw = field.partition(":")
                if separator and name.strip().casefold() == "d":
                    try:
                        duration = float(raw.split("<", 1)[0].strip())
                    except ValueError:
                        duration = None
                    break
            row = database.execute(
                "SELECT COALESCE(StartModifiers, ''), "
                "COALESCE(StatusEffectDuration, 0) "
                "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
                (granted_id,),
            ).fetchone()
            if row is None:
                continue
            blocked = set()
            for entry in str(row[0]).split(";"):
                name, separator, raw = entry.partition(":")
                if not separator or name.strip().casefold() != (
                    "statuseffectimmunitygroup"
                ):
                    continue
                group = raw.partition(",")[0].strip().casefold()
                # ``stun#~`` and similar suffixes are selector syntax.
                group = group.split("#", 1)[0].strip()
                if group:
                    blocked.add(group)
            if not blocked:
                continue
            if duration is None:
                duration = float(row[1] or 0.0)
            if duration <= 0.0:
                continue
            return frozenset(blocked), duration
        return None

    @staticmethod
    def _load_victim_status_effects(
        database: sqlite3.Connection,
        value: str,
        *,
        default_trigger: str = "hit",
    ) -> tuple[VictimStatusEffectDefinition, ...]:
        """Resolve the generated skill column into client wire definitions.

        ``C`` is the application probability, ``D`` is the PvE duration in
        seconds (``DP`` is the separate PvP duration), and ``$N`` values are
        the modifier parameters stored in the status-effect detail array.
        """

        definitions = []
        for raw_entry in str(value or "").split(";"):
            parts = [
                part.strip()
                for part in raw_entry.split(",")
                if part.strip()
            ]
            if not parts:
                continue
            metadata: dict[str, str] = {}
            parameters: dict[int, float] = {}
            for token in parts[1:]:
                name, separator, raw_value = token.partition(":")
                if not separator:
                    continue
                metadata[name.upper()] = raw_value
                if not name.startswith("$"):
                    continue
                try:
                    index = int(name[1:])
                except ValueError:
                    continue
                numeric = raw_value.split("<", 1)[0].strip()
                try:
                    number = float(numeric)
                except ValueError:
                    reference = numeric.lstrip("$")
                    variable = database.execute(
                        "SELECT InitialValue FROM _Template_ActorVariables "
                        "WHERE Id=? LIMIT 1",
                        (reference,),
                    ).fetchone()
                    if variable is None:
                        # Some conditional item effects use resource ids as
                        # parameters. They are selectors, not wire floats.
                        continue
                    number = float(variable[0])
                if index >= 0:
                    parameters[index] = number
            effect_row = database.execute(
                "SELECT rowid - 1, COALESCE(StatusEffectDuration, 0), "
                "COALESCE(MaxStackSize, 1), COALESCE(AuraRadius, 0), "
                "COALESCE(AuraHeight, 0), COALESCE(AuraShape, ''), "
                "COALESCE(StartModifiers, ''), COALESCE(EffectHosts, 'Receiver'), "
                "COALESCE(AuraTargets, ''), COALESCE(ExclusivityType, 'Replace') "
                "FROM _Template_StatusEffect "
                "WHERE Id=? LIMIT 1",
                (parts[0],),
            ).fetchone()
            if effect_row is None:
                continue
            try:
                raw_chance = metadata.get("C", "1")
                chance_value = float(
                    raw_chance.split("<", 1)[0].strip()
                )
                if "<relative" in raw_chance.casefold():
                    chance = 1.0 * (1.0 + chance_value)
                elif "<absolute" in raw_chance.casefold():
                    chance = 1.0 + chance_value
                else:
                    chance = chance_value

                base_duration = max(0.0, float(effect_row[1] or 0.0))
                raw_duration = metadata.get(
                    "D", metadata.get("DP", "")
                )
                if not raw_duration:
                    duration_seconds = base_duration
                else:
                    duration_value = float(
                        raw_duration.split("<", 1)[0].strip()
                    )
                    if "<relative" in raw_duration.casefold():
                        duration_seconds = base_duration * (
                            1.0 + duration_value
                        )
                    elif "<absolute" in raw_duration.casefold():
                        duration_seconds = base_duration + duration_value
                    else:
                        duration_seconds = duration_value
                pvp_duration_seconds = None
                if "DP" in metadata:
                    raw_pvp = metadata["DP"]
                    amount = float(raw_pvp.split("<", 1)[0].strip())
                    if "<relative" in raw_pvp.casefold():
                        pvp_duration_seconds = base_duration * (1.0 + amount)
                    elif "<absolute" in raw_pvp.casefold():
                        pvp_duration_seconds = base_duration + amount
                    else:
                        pvp_duration_seconds = amount
                raw_delay = metadata.get("DE", "0")
                delay_seconds = float(
                    raw_delay.split("<", 1)[0].strip()
                )
            except ValueError:
                continue
            value_count = max(5, max(parameters, default=-1) + 1)
            definitions.append(
                VictimStatusEffectDefinition(
                    resource_id=parts[0],
                    effect_hosts=str(effect_row[7] or "Receiver"),
                    aura_targets=str(effect_row[8] or ""),
                    exclusivity_type=str(effect_row[9] or "Replace"),
                    pvp_duration_ticks=(
                        max(0, round(pvp_duration_seconds * SERVER_TICKS_PER_SECOND))
                        if pvp_duration_seconds is not None else None
                    ),
                    effect_id=int(effect_row[0]),
                    chance=max(0.0, min(1.0, chance)),
                    duration_ticks=max(
                        0, round(max(0.0, duration_seconds)
                                 * SERVER_TICKS_PER_SECOND)
                    ),
                    values=tuple(
                        parameters.get(index, 0.0)
                        for index in range(value_count)
                    ),
                    trigger=metadata.get(
                        "ON", default_trigger
                    ).strip().lower(),
                    delay_ticks=max(
                        0,
                        round(
                            max(0.0, delay_seconds)
                            * SERVER_TICKS_PER_SECOND
                        ),
                    ),
                    offset=tuple(
                        _metadata_float(metadata, key) for key in ("OX", "OY", "OZ")
                    ),
                    max_stack_size=max(1, int(effect_row[2] or 1)),
                    consume_on_max_stack=(
                        LocalCombatState._consumes_on_max_stack(
                            database,
                            parts[0],
                            str(effect_row[6] or ""),
                        )
                    ),
                    aura_radius=max(0.0, float(effect_row[3] or 0.0)),
                    aura_height=max(0.0, float(effect_row[4] or 0.0)),
                    aura_shape=str(effect_row[5] or ""),
                    modifier_kinds=frozenset(
                        raw_modifier.partition(":")[0].strip().casefold()
                        for raw_modifier in str(effect_row[6] or "").split(";")
                        if raw_modifier.partition(":")[1]
                    ),
                )
            )
        return tuple(definitions)

    @classmethod
    def _load_location_aura_effects(
        cls,
        database: sqlite3.Connection,
        value: str,
    ) -> tuple[VictimStatusEffectDefinition, ...]:
        """Resolve a location aura into the receiver effects it applies.

        The location effect itself is hosted by the predicted ground skill,
        not by a monster. ``AuraEffectId`` lists the actual effects received
        by enemies inside that location.
        """

        definitions = []
        for aura in cls._load_victim_status_effects(database, value):
            row = database.execute(
                "SELECT AuraEffectId, COALESCE(AuraTargets, '') "
                "FROM _Template_StatusEffect "
                "WHERE Id=? LIMIT 1",
                (aura.resource_id,),
            ).fetchone()
            child_ids = tuple(
                item.strip()
                for item in str(row[0] if row else "").split(";")
                if item.strip()
            )
            for child_id in child_ids:
                child = database.execute(
                    "SELECT rowid - 1, StatusEffectDuration, "
                    "COALESCE(MaxStackSize, 1), COALESCE(EffectHosts, '') "
                    "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
                    (child_id,),
                ).fetchone()
                if child is None:
                    continue
                if (
                    str(row[1] if row else "").casefold() == "creator"
                    or str(child[3] or "").casefold() == "causer"
                ):
                    continue
                child_duration = max(
                    0,
                    round(
                        max(0.0, float(child[1] or 0.0))
                        * SERVER_TICKS_PER_SECOND
                    ),
                )
                definitions.extend(
                    replace(
                        definition,
                        chance=aura.chance,
                        duration_ticks=(
                            child_duration or aura.duration_ticks
                        ),
                        values=aura.values,
                        trigger=aura.trigger,
                    )
                    for definition in cls._load_victim_status_effects(database, child_id)
                )
        return tuple(definitions)

    def player_user_effect_location_auras(
        self,
        definitions: tuple[VictimStatusEffectDefinition, ...],
    ) -> tuple[
        tuple[
            VictimStatusEffectDefinition,
            tuple[VictimStatusEffectDefinition, ...],
            float,
        ], ...
    ]:
        """Resolve immediate user-effect aura children for server combat.

        Shadow Stalker's critical-hit proc adds its damaging aura to the
        player. The status packet makes the client display it, while this
        resolver also creates the matching authoritative location effect.
        """

        if not definitions:
            return ()
        cached = self._user_effect_location_auras_cache.get(definitions)
        if cached is not None:
            return cached
        path = self.encounter.static_database_path
        results = []
        with sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True
        ) as database:
            for definition in definitions:
                for kind, arguments in self._status_modifier_program(
                    int(definition.effect_id)
                ):
                    if (
                        kind.casefold() != "actorstatuseffect"
                        or len(arguments) < 2
                        or arguments[0].casefold() not in {"addae", "addle"}
                    ):
                        continue
                    metadata = {
                        name.casefold(): value
                        for token in arguments[2:]
                        for name, separator, value in (token.partition(":"),)
                        if separator
                    }
                    if metadata.get("on", "immediate").casefold() not in {
                        "immediate", "immediatemanaged"
                    }:
                        continue
                    aura_id = self._clean_modifier_token(arguments[1])
                    aura_row = database.execute(
                        "SELECT COALESCE(AuraTargets, '') "
                        "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
                        (aura_id,),
                    ).fetchone()
                    if aura_row is None:
                        continue
                    if (str(aura_row[0] or "").casefold() != "enemies"
                            and arguments[0].casefold() != "addle"):
                        continue
                    aura_values = self._load_victim_status_effects(
                        database, aura_id, default_trigger="immediate"
                    )
                    receiver_effects = list(
                        self._load_location_aura_effects(database, aura_id)
                    )
                    if not aura_values:
                        continue
                    # ActorStatusEffect parameters can override the aura's
                    # authored delay. Keep the visual and its damage in step.
                    try:
                        delay_seconds = max(
                            0.0,
                            float(metadata.get("de", "0").split("<", 1)[0]),
                        )
                    except ValueError:
                        delay_seconds = 0.0
                    # Receiver children can immediately apply a second
                    # status (Desolation Impact -> Destruction Mark).
                    for receiver in tuple(receiver_effects):
                        for child_kind, child_arguments in self._status_modifier_program(receiver.effect_id):
                            if child_kind.casefold() != "actorstatuseffect" or len(child_arguments) < 2:
                                continue
                            if child_arguments[0].casefold() != "addae":
                                continue
                            nested = self._load_victim_status_effects(
                                database, ",".join(child_arguments[1:]), default_trigger="immediate",
                            )
                            receiver_effects.extend(e for e in nested if e.trigger in {"immediate", "immediatemanaged"})
                    results.append(
                        (
                            aura_values[0],
                            tuple({
                                effect.resource_id: effect
                                for effect in receiver_effects
                            }.values()),
                            delay_seconds,
                        )
                    )
        if len(self._user_effect_location_auras_cache) >= 1024:
            self._user_effect_location_auras_cache.clear()
        self._user_effect_location_auras_cache[definitions] = tuple(results)
        return tuple(results)

    @classmethod
    def _load_aura_owner_effects(
        cls,
        database: sqlite3.Connection,
        value: str,
        *,
        default_trigger: str = "hit",
    ) -> tuple[VictimStatusEffectDefinition, ...]:
        """Resolve the creator/causer children of an authored aura.

        An ``AuraTargets:Creator`` row is a delivery shell: the wearer-facing
        payload lives in its ``AuraEffectId`` child, so whoever created the
        aura -- a ground location or a caster buffing themselves -- receives
        the child rather than the shell.
        """

        definitions = []
        for aura in cls._load_victim_status_effects(
            database, value, default_trigger=default_trigger
        ):
            row = database.execute(
                "SELECT AuraEffectId, COALESCE(AuraTargets, '') "
                "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
                (aura.resource_id,),
            ).fetchone()
            for child_id in tuple(
                item.strip()
                for item in str(row[0] if row else "").split(";")
                if item.strip()
            ):
                child = database.execute(
                    "SELECT rowid - 1, StatusEffectDuration, "
                    "COALESCE(MaxStackSize, 1), COALESCE(EffectHosts, ''), "
                    "COALESCE(StartModifiers, '') "
                    "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
                    (child_id,),
                ).fetchone()
                if child is None:
                    continue
                if not (
                    str(row[1] if row else "").casefold() == "creator"
                    or str(child[3] or "").casefold() == "causer"
                ):
                    continue
                child_duration = max(
                    0,
                    round(
                        max(0.0, float(child[1] or 0.0))
                        * SERVER_TICKS_PER_SECOND
                    ),
                )
                definitions.append(VictimStatusEffectDefinition(
                    resource_id=child_id,
                    effect_id=int(child[0]),
                    chance=aura.chance,
                    duration_ticks=child_duration or aura.duration_ticks,
                    values=aura.values,
                    trigger=aura.trigger,
                    max_stack_size=max(1, int(child[2] or 1)),
                    # What it modifies: without them a SkillResourceRegeneration
                    # child (Banner of War's) never refreshed the owner's vitals
                    # and its rage kept draining in the banner (2026-10-21).
                    modifier_kinds=frozenset(
                        raw.partition(":")[0].strip().casefold()
                        for raw in str(child[4] or "").split(";")
                        if raw.partition(":")[1]
                    ),
                    creator_aura=(
                        aura.resource_id
                        if str(row[1] if row else "").casefold() == "creator"
                        and aura.aura_radius > 0.0
                        else ""
                    ),
                ))
        return tuple(definitions)

    def _load_monster_attack_effects(
        self, definition: MonsterAttackDefinition
    ) -> tuple[VictimStatusEffectDefinition, ...]:
        cached = self._monster_attack_effects.get(definition.resource_id)
        if cached is not None:
            return cached
        with sqlite3.connect(
            f"file:{self.encounter.static_database_path.as_posix()}?mode=ro",
            uri=True,
        ) as database:
            effects = self._load_victim_status_effects(
                database, definition.victim_status_effects
            )
        self._monster_attack_effects[definition.resource_id] = effects
        return effects

    def _load_monster_user_effects(
        self, definition: MonsterAttackDefinition
    ) -> tuple[VictimStatusEffectDefinition, ...]:
        cached = self._monster_user_effects.get(definition.resource_id)
        if cached is not None:
            return cached
        with sqlite3.connect(
            f"file:{self.encounter.static_database_path.as_posix()}?mode=ro",
            uri=True,
        ) as database:
            effects = self._load_victim_status_effects(
                database,
                definition.user_status_effects,
                default_trigger="skillstart",
            )
            strikes = self._load_monster_aura_strikes(
                database, definition.user_status_effects
            )
        self._monster_aura_strikes[definition.resource_id] = strikes
        self._monster_user_effects[definition.resource_id] = effects
        return effects

    def _load_monster_aura_strikes(
        self, database: sqlite3.Connection, value: str
    ) -> dict[str, MonsterAuraStrike]:
        """Find the one-shot damaging enemy auras among user effects.

        SEE: MonsterAuraStrike. Ally/creator/summoner auras, children
        without a health-taking start instruction (heals, buffs, slows) and
        persistent auras whose children tick (fire_aura D:5, Khalys's beam)
        are left out; an aura with a duration whose children only strike on
        start lands once (e_ks_boss_eye_360_aura D 1.6:
        CurrHealthPoints:-0.8,relative, 80% of maximum health).
        """

        strikes: dict[str, MonsterAuraStrike] = {}
        for entry in str(value or "").split(";"):
            entry = entry.strip()
            if not entry:
                continue
            for aura in self._load_victim_status_effects(
                database, entry, default_trigger="skillstart"
            ):
                if (
                    aura.aura_radius <= 0.0
                    or aura.aura_targets.strip().casefold() != "enemies"
                ):
                    continue
                children = self._load_location_aura_effects(database, entry)
                if aura.duration_ticks > 0 and any(
                    self._status_health_programs(child, ticks=True)
                    for child in children
                ):
                    continue
                programs = tuple(
                    program
                    for child in children
                    for program in self._status_health_programs(
                        child, ticks=False
                    )
                    if program[1] > 0.0
                )
                if programs:
                    strikes[aura.resource_id] = MonsterAuraStrike(
                        resource_id=aura.resource_id,
                        radius=aura.aura_radius,
                        delay_ticks=aura.delay_ticks,
                        damage_programs=programs,
                    )
        return strikes

    def _monster_aura_strikes_for(
        self, definition: MonsterAttackDefinition
    ) -> dict[str, MonsterAuraStrike]:
        self._load_monster_user_effects(definition)
        return self._monster_aura_strikes.get(definition.resource_id, {})

    @staticmethod
    def _clean_modifier_token(value: str) -> str:
        """Remove generated UI labels and comparison annotations."""

        return str(value or "").split("#", 1)[0].split("<", 1)[0].strip()

    def _modifier_amount(
        self,
        database: sqlite3.Connection | None,
        value: str,
        details: tuple[float, ...],
    ) -> float | None:
        """Resolve a literal, ``$N`` detail, or ActorVariable constant."""

        token = self._clean_modifier_token(value)
        if not token:
            return None
        if not token.startswith("$"):
            try:
                return float(token)
            except ValueError:
                return None
        reference = token[1:]
        if reference.isdigit():
            index = int(reference)
            return float(details[index]) if index < len(details) else 0.0
        key = reference.casefold()
        if key not in self._actor_variable_values and database is not None:
            row = database.execute(
                "SELECT InitialValue FROM _Template_ActorVariables "
                "WHERE lower(Id)=? LIMIT 1",
                (key,),
            ).fetchone()
            return float(row[0]) if row is not None else None
        return self._actor_variable_values.get(key)

    def _status_modifier_program(
        self, effect_id: int
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Return one parsed StartModifiers program from the primed catalog."""

        effect_id = int(effect_id)
        cached = self._status_effect_modifier_programs.get(effect_id)
        if cached is not None:
            return cached
        _interval, _tick, encoded = self._status_effect_runtime_program(
            effect_id
        )
        parsed = tuple(
            (
                kind.strip(),
                tuple(value.strip() for value in arguments.split(",")),
            )
            for raw in str(encoded).split(";")
            for kind, separator, arguments in (raw.partition(":"),)
            if separator and kind.strip()
        )
        self._status_effect_modifier_programs[effect_id] = parsed
        return parsed

    def status_effect_disabled_features(
        self, effect_id: int
    ) -> frozenset[str]:
        """Return the ActorFeature names one effect turns off."""

        effect_id = int(effect_id)
        cached = self._status_effect_disabled_features.get(effect_id)
        if cached is not None:
            return cached
        _interval, _ticks, encoded = self._status_effect_runtime_program(
            effect_id
        )
        result = parse_disabled_actor_features(encoded)
        self._status_effect_disabled_features[effect_id] = result
        return result

    @staticmethod
    def _nested_modifier_details(
        arguments: tuple[str, ...], parent_details: tuple[float, ...]
    ) -> tuple[float, ...]:
        indexed = {}
        for token in arguments[2:]:
            name, separator, raw_value = token.partition(":")
            if not separator or not name.startswith("$"):
                continue
            try:
                index = int(name[1:])
            except ValueError:
                continue
            value_token = LocalCombatState._clean_modifier_token(raw_value)
            try:
                if value_token.startswith("$"):
                    parent_index = int(value_token[1:])
                    value = float(parent_details[parent_index])
                else:
                    value = float(value_token)
            except (ValueError, IndexError):
                value = 0.0
            indexed[index] = value
        count = max(5, max(indexed, default=-1) + 1)
        return tuple(indexed.get(index, 0.0) for index in range(count))

    def _expand_status_modifier_instructions(
        self,
        instructions: list,
        explicit_effect_ids: frozenset[int] = frozenset(),
    ) -> None:
        """Append nested ActorStatusEffect programs once, without DB I/O."""

        seen = set()
        cursor = 0
        while cursor < len(instructions) and cursor < 256:
            kind, arguments, stack_count, details = instructions[cursor]
            cursor += 1
            if kind.casefold() != "actorstatuseffect" or len(arguments) < 2:
                continue
            effect_id = self._status_effect_ids.get(
                self._clean_modifier_token(arguments[1])
            )
            if effect_id is None or int(effect_id) in explicit_effect_ids:
                continue
            child_details = self._nested_modifier_details(
                arguments, tuple(details)
            )
            key = (int(effect_id), int(stack_count), child_details)
            if key in seen:
                continue
            seen.add(key)
            instructions.extend(
                (child_kind, child_arguments, stack_count, child_details)
                for child_kind, child_arguments
                in self._status_modifier_program(effect_id)
            )

    @staticmethod
    def _apply_numeric_modifier(
        current: float,
        amount: float,
        operation: str,
        stack_count: int,
    ) -> float:
        scaled = float(amount) * max(1, int(stack_count))
        if str(operation).casefold() == "relative":
            return current * (1.0 + scaled)
        return current + scaled

    def _merge_status_effect_modifier(
        self,
        database: sqlite3.Connection,
        current: VictimStatusEffectDefinition,
        modifier: VictimStatusEffectDefinition,
        encoded: str,
        stack_count: int,
    ) -> VictimStatusEffectDefinition:
        """Apply generated ``modUE/modVE`` field operations to one effect."""

        metadata = {}
        for token in str(encoded).split(",")[1:]:
            name, separator, raw_value = token.partition(":")
            if separator:
                metadata[name.upper()] = raw_value
        chance = current.chance
        if "C" in metadata:
            raw = metadata["C"]
            amount = self._modifier_amount(database, raw, ())
            if amount is not None:
                if "<relative" in raw.casefold():
                    chance *= 1.0 + amount * stack_count
                elif "<absolute" in raw.casefold():
                    chance += amount * stack_count
                else:
                    chance = amount * stack_count
        def modified_duration(ticks, raw):
            if raw is None:
                return ticks
            amount = self._modifier_amount(database, raw, ())
            if amount is None:
                return ticks
            if "<relative" in raw.casefold():
                return round(ticks * (1.0 + amount * stack_count))
            if "<absolute" in raw.casefold():
                return ticks + round(amount * SERVER_TICKS_PER_SECOND * stack_count)
            return round(amount * SERVER_TICKS_PER_SECOND)

        duration_ticks = modified_duration(
            current.duration_ticks, metadata.get("D", metadata.get("DP"))
        )
        pvp_duration_ticks = current.pvp_duration_ticks
        if pvp_duration_ticks is not None or "DP" in metadata:
            pvp_duration_ticks = max(0, modified_duration(
                current.duration_ticks if pvp_duration_ticks is None else pvp_duration_ticks,
                metadata.get("DP"),
            ))
        values = list(current.values)
        if len(values) < len(modifier.values):
            values.extend(0.0 for _ in range(len(modifier.values) - len(values)))
        for name, raw in metadata.items():
            if not name.startswith("$"):
                continue
            try:
                index = int(name[1:])
            except ValueError:
                continue
            if index >= len(modifier.values):
                continue
            amount = modifier.values[index]
            if "<relative" in raw.casefold():
                values[index] *= 1.0 + amount * stack_count
            elif "<absolute" in raw.casefold():
                values[index] += amount * stack_count
            else:
                values[index] = amount * stack_count
        return replace(
            current,
            chance=max(0.0, min(1.0, chance)),
            duration_ticks=max(0, duration_ticks),
            pvp_duration_ticks=pvp_duration_ticks,
            values=tuple(values),
            trigger=(
                modifier.trigger if "ON" in metadata else current.trigger
            ),
        )

    @classmethod
    def _skill_scope_matches(
        cls,
        geometry: PlayerSkillGeometry,
        values: tuple[str, ...],
    ) -> bool:
        wanted = {
            cls._clean_modifier_token(value).casefold()
            for value in values
            if cls._clean_modifier_token(value)
        }
        if not wanted:
            return False
        scopes = {
            geometry.resource_id.casefold(),
            "allplayerskills",
            f"{geometry.char_class.casefold()}_skills",
            *(value.casefold() for value in geometry.groups),
        }
        return bool(scopes & wanted)

    @staticmethod
    def _active_ammunition_types(
        modifier_sources: tuple[object, ...],
    ) -> frozenset[str]:
        return frozenset(
            LocalCombatState._clean_modifier_token(argument).casefold()
            for source in modifier_sources
            if str(getattr(source, "kind", "")).casefold()
            == "ammunitiontype"
            for argument in tuple(getattr(source, "arguments", ()))
            if LocalCombatState._clean_modifier_token(argument)
        )

    def _ammunition_can_hit(
        self,
        actor_id: int,
        ammunition_types: frozenset[str],
    ) -> bool:
        required = self.monster_ammo_vulnerabilities.get(int(actor_id))
        return not required or bool(required & ammunition_types)

    @staticmethod
    def _modifier_source_cache_key(source: object) -> tuple[object, ...]:
        """Return an immutable semantic key for one modifier source."""

        if isinstance(source, int):
            return ("legacy", int(source))
        if hasattr(source, "effect_id"):
            return (
                "effect",
                int(getattr(source, "effect_id", -1)),
                int(getattr(source, "field_18", 0)),
                tuple(float(value) for value in getattr(source, "values", ())),
            )
        if hasattr(source, "kind") and hasattr(source, "arguments"):
            return (
                "modifier",
                str(getattr(source, "kind", "")).casefold(),
                tuple(str(value) for value in getattr(source, "arguments", ())),
            )
        # Unknown sources are not expected in production, but retaining their
        # type and stable representation is safer than coalescing them.
        return ("opaque", type(source).__qualname__, repr(source))

    def _modified_player_skill_geometry(
        self,
        geometry: PlayerSkillGeometry,
        modifier_sources: tuple[object, ...],
    ) -> PlayerSkillGeometry:
        """Apply active talent, equipment, and item-set modifier programs.

        A source may be a wire ``StatusEffectEntry``, a legacy integer effect
        id, or an already parsed item-set modifier. Status-effect stack values
        matter: rank ten is one effect with ``field_18 == 9``, not ten ids.
        """

        if not modifier_sources:
            return geometry
        cache_key = (
            geometry,
            tuple(
                self._modifier_source_cache_key(source)
                for source in modifier_sources
            ),
        )
        cached = self._modified_player_geometry_cache.get(cache_key)
        if cached is not None:
            return cached
        attack_range = geometry.attack_range
        hit_range = geometry.hit_range
        bullet_count = geometry.bullet_count
        lifetime = geometry.projectile_lifetime
        velocity = geometry.projectile_velocity
        impact_count = geometry.impact_count
        damage_modifier = geometry.damage_modifier
        resource_cost_fraction = geometry.resource_cost_fraction
        resource_gain_fraction = geometry.resource_gain_fraction
        timing_multiplier = geometry.skill_timing_multiplier
        trap_on_hit = geometry.trap_on_hit
        cleanse_groups = set(geometry.cleanse_groups)
        damage_weights: dict[str, float] = {}
        for raw in str(geometry.damage_type or "Physical").split(";"):
            token, separator, encoded_weight = raw.strip().partition(":")
            key = token.strip().casefold()
            if key not in DAMAGE_TYPE_IDS:
                continue
            try:
                weight = float(encoded_weight) if separator else 1.0
            except ValueError:
                weight = 1.0
            damage_weights[key] = damage_weights.get(key, 0.0) + weight
        if not damage_weights:
            damage_weights["physical"] = 1.0
        user_effects = list(geometry.user_status_effects)
        victim_effects = list(geometry.victim_status_effects)
        location_effects = list(geometry.location_status_effects)
        location_visual_effects = list(
            geometry.location_visual_effects
        )
        bullet_location_effects = list(
            geometry.bullet_location_effects
        )
        bullet_location_visual_effects = list(
            geometry.bullet_location_visual_effects
        )
        bullet_death_effects = list(geometry.bullet_death_effects)
        path = self.encounter.static_database_path

        with sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True
        ) as database:
            instructions: list[
                tuple[str, tuple[str, ...], int, tuple[float, ...]]
            ] = []
            seen_legacy_ids: set[int] = set()
            explicit_effect_ids = {
                int(source.effect_id)
                for source in modifier_sources
                if hasattr(source, "effect_id")
            }
            for source in modifier_sources:
                if hasattr(source, "kind") and hasattr(source, "arguments"):
                    source_kind = str(source.kind)
                    source_arguments = tuple(
                        str(value) for value in source.arguments
                    )
                    if source_kind.casefold() == "talentlevelbonus":
                        if len(source_arguments) < 2:
                            continue
                        try:
                            bonus_levels = max(
                                0,
                                int(round(float(source_arguments[0]))),
                            )
                        except ValueError:
                            bonus_levels = 0
                        talent_id = self._clean_modifier_token(
                            source_arguments[1]
                        )
                        row = database.execute(
                            "SELECT StackStatusEffectId FROM "
                            "_Template_SkillTalent WHERE Id=? LIMIT 1",
                            (talent_id,),
                        ).fetchone()
                        status_id = str(row[0] or "") if row else ""
                        effect_row = database.execute(
                            "SELECT StartModifiers FROM "
                            "_Template_StatusEffect WHERE Id=? LIMIT 1",
                            (status_id,),
                        ).fetchone()
                        if bonus_levels and effect_row is not None:
                            for raw in str(effect_row[0] or "").split(";"):
                                kind, separator, arguments = raw.partition(":")
                                if separator:
                                    instructions.append(
                                        (
                                            kind.strip(),
                                            tuple(
                                                value.strip()
                                                for value in arguments.split(",")
                                            ),
                                            bonus_levels,
                                            (),
                                        )
                                    )
                        continue
                    instructions.append(
                        (
                            source_kind,
                            source_arguments,
                            1,
                            (),
                        )
                    )
                    continue
                if hasattr(source, "effect_id"):
                    effect_id = int(source.effect_id)
                    stack_count = max(
                        1, int(getattr(source, "field_18", 0)) + 1
                    )
                    details = tuple(
                        float(value)
                        for value in getattr(source, "values", ())
                    )
                else:
                    effect_id = int(source)
                    if effect_id in seen_legacy_ids:
                        continue
                    seen_legacy_ids.add(effect_id)
                    stack_count = 1
                    details = ()
                program = self._status_modifier_program(effect_id)
                instructions.extend(
                    (kind, arguments, stack_count, details)
                    for kind, arguments in program
                )

            self._expand_status_modifier_instructions(
                instructions, frozenset(explicit_effect_ids)
            )
            for kind, arguments, stack_count, details in instructions:
                normalized_kind = kind.casefold()
                if normalized_kind == "traponhit" and len(arguments) >= 2:
                    if not self._skill_scope_matches(
                        geometry, (arguments[0],)
                    ):
                        continue
                    base_resource = self._clean_modifier_token(arguments[1])
                    ranked_resource = f"{base_resource}{stack_count}"
                    if database.execute(
                        "SELECT 1 FROM _Template_Trap WHERE Id=? LIMIT 1",
                        (ranked_resource,),
                    ).fetchone() is not None:
                        trap_on_hit = ranked_resource
                    elif (
                        len(arguments) >= 3
                        and database.execute(
                            "SELECT 1 FROM _Template_Trap WHERE Id=? LIMIT 1",
                            (self._clean_modifier_token(arguments[2]),),
                        ).fetchone() is not None
                    ):
                        trap_on_hit = self._clean_modifier_token(arguments[2])
                    else:
                        trap_on_hit = base_resource
                    continue
                if normalized_kind == "skillstatuseffectcleanse":
                    # SkillStatusEffectCleanse:<skill scope>,<group>,...
                    # (the PvP class talents add it to Frenzy Shout & co.).
                    if len(arguments) >= 2 and self._skill_scope_matches(
                        geometry, (arguments[0],)
                    ):
                        cleanse_groups.update(
                            self._clean_modifier_token(value).casefold()
                            for value in arguments[1:]
                            if self._clean_modifier_token(value)
                        )
                    continue
                if (
                    normalized_kind == "actorstatuseffect"
                    and len(arguments) >= 2
                ):
                    operation = arguments[0].casefold()
                    encoded = ",".join(arguments[1:])
                    if operation.endswith("le"):
                        nested = self._load_location_aura_effects(
                            database, encoded
                        )
                        location_effects.extend(nested)
                    else:
                        nested = self._load_victim_status_effects(
                            database,
                            encoded,
                            default_trigger="skillstart",
                        )
                        if stack_count > 1:
                            nested = tuple(
                                replace(
                                    effect,
                                    chance=min(
                                        1.0,
                                        effect.chance * stack_count,
                                    ),
                                    values=tuple(
                                        value * stack_count
                                        for value in effect.values
                                    ),
                                )
                                for effect in nested
                            )
                        user_effects.extend(nested)
                    continue
                if normalized_kind == "skilldamagetype":
                    if (
                        len(arguments) < 4
                        or not self._skill_scope_matches(
                            geometry, arguments[2:-1]
                        )
                    ):
                        continue
                    damage_key = self._clean_modifier_token(
                        arguments[-1]
                    ).casefold()
                    if damage_key not in DAMAGE_TYPE_IDS:
                        continue
                    amount = self._modifier_amount(
                        database, arguments[0], details
                    )
                    if amount is None:
                        continue
                    damage_weights[damage_key] = self._apply_numeric_modifier(
                        damage_weights.get(damage_key, 0.0),
                        amount,
                        arguments[1],
                        stack_count,
                    )
                    continue
                if normalized_kind == "targetadddamage":
                    # Fractured Planes elemental damage blessings are
                    # authored as TargetAddDamage on the persistent status.
                    # Treat the element as a scope and apply its absolute or
                    # relative amount to matching outgoing skill damage.
                    if len(arguments) < 3:
                        continue
                    target_elements = {
                        self._clean_modifier_token(value).casefold()
                        for value in arguments[2:]
                        if self._clean_modifier_token(value)
                    }
                    skill_elements = {
                        self._clean_modifier_token(value.partition(":")[0]).casefold()
                        for value in str(geometry.damage_type or "Physical").split(";")
                        if self._clean_modifier_token(value.partition(":")[0])
                    }
                    if not target_elements.intersection(skill_elements):
                        continue
                    amount = self._modifier_amount(database, arguments[0], details)
                    if amount is None:
                        continue
                    damage_modifier = self._apply_numeric_modifier(
                        damage_modifier, amount, arguments[1], stack_count
                    )
                    continue
                numeric_kinds = {
                    "bulletcount",
                    "bulletlifetime",
                    "bulletvelocity",
                    "bulletimpactcount",
                    "skilldamage",
                    "skillhitdamage",
                    "skillrange",
                    "skillhitrange",
                    "skilltiming",
                    "resourcecost",
                    "additionalskillcost",
                    "resourcegain",
                }
                if normalized_kind in numeric_kinds:
                    if (
                        len(arguments) < 3
                        or not self._skill_scope_matches(
                            geometry, arguments[2:]
                        )
                    ):
                        continue
                    amount = self._modifier_amount(
                        database, arguments[0], details
                    )
                    if amount is None:
                        continue
                    operation = arguments[1]
                    # Bullet topology is an unlock/shape change, not a
                    # per-rank scalar. For example rank two of Frozen Master
                    # unlocks +3 Ice Missile penetrations; its status entry
                    # continues to stack for the other per-rank modifiers.
                    # Multiplying the literal +3 by rank five produced 14
                    # penetrations and, with three missiles, 42 authoritative
                    # impacts from one basic cast. Apply structural topology
                    # instructions once per active source.
                    numeric_stack_count = (
                        1
                        if normalized_kind in {
                            "bulletcount", "bulletimpactcount"
                        }
                        else stack_count
                    )
                    if normalized_kind == "bulletcount":
                        bullet_count = round(
                            self._apply_numeric_modifier(
                                bullet_count,
                                amount,
                                operation,
                                numeric_stack_count,
                            )
                        )
                    elif normalized_kind == "bulletlifetime":
                        lifetime = self._apply_numeric_modifier(
                            lifetime, amount, operation, stack_count
                        )
                    elif normalized_kind == "bulletvelocity":
                        velocity = self._apply_numeric_modifier(
                            velocity, amount, operation, stack_count
                        )
                    elif normalized_kind == "bulletimpactcount":
                        impact_count = round(
                            self._apply_numeric_modifier(
                                impact_count,
                                amount,
                                operation,
                                numeric_stack_count,
                            )
                        )
                    elif normalized_kind in {
                        "skilldamage",
                        "skillhitdamage",
                    }:
                        damage_modifier = self._apply_numeric_modifier(
                            damage_modifier,
                            amount,
                            operation,
                            stack_count,
                        )
                    elif normalized_kind == "skillrange":
                        attack_range = self._apply_numeric_modifier(
                            attack_range,
                            amount,
                            operation,
                            stack_count,
                        )
                    elif normalized_kind == "skillhitrange":
                        hit_range = self._apply_numeric_modifier(
                            hit_range,
                            amount,
                            operation,
                            stack_count,
                        )
                    elif normalized_kind in {
                        "resourcecost", "additionalskillcost"
                    }:
                        resource_cost_fraction = self._apply_numeric_modifier(
                            resource_cost_fraction,
                            amount,
                            operation,
                            stack_count,
                        )
                    elif normalized_kind == "resourcegain":
                        resource_gain_fraction = self._apply_numeric_modifier(
                            resource_gain_fraction,
                            amount,
                            operation,
                            stack_count,
                        )
                    else:
                        timing_multiplier = self._apply_numeric_modifier(
                            timing_multiplier,
                            amount,
                            operation,
                            stack_count,
                        )
                    continue

                if normalized_kind == "skillpunchthrough":
                    if self._skill_scope_matches(geometry, arguments):
                        impact_count = max(impact_count, 0xFFFF)
                    continue

                if normalized_kind == "skillsequence":
                    if (
                        len(arguments) >= 3
                        and arguments[0].casefold() == "skillbulletid"
                        and self._skill_scope_matches(
                            geometry, (arguments[1],)
                        )
                    ):
                        row = database.execute(
                            "SELECT BulletRadius, ImpactRadius, Velocity, "
                            "LifeTime, ImpactCount, ImpactChildSpawnCount, "
                            "TargetSearchRadius, MotionType, StartOffset, "
                            "BulletLocationEffect, "
                            "BulletDeathLocationEffect, "
                            "BulletLocationEffectRate, "
                            "BulletLocationEffectStartDelay, "
                            "COALESCE(StaticCollision, 0) "
                            "FROM _Template_SkillBullet WHERE Id=? LIMIT 1",
                            (self._clean_modifier_token(arguments[2]),),
                        ).fetchone()
                        if row is not None:
                            start_offset = geometry.start_offset
                            if isinstance(row[8], bytes) and len(row[8]) == 16:
                                start_offset = tuple(
                                    struct.unpack("<4f", row[8])
                                )
                            geometry = replace(
                                geometry,
                                projectile_radius=max(0.0, float(row[0] or 0)),
                                impact_radius=max(0.0, float(row[1] or 0)),
                                target_search_radius=max(
                                    0.0, float(row[6] or 0)
                                ),
                                impact_child_spawn_count=max(
                                    0, int(row[5] or 0)
                                ),
                                motion_type=str(row[7] or "Straight"),
                                start_offset=start_offset,
                                bullet_location_effect_rate=max(
                                    0.0, float(row[11] or 0.0)
                                ),
                                bullet_location_effect_start_delay=max(
                                    0.0, float(row[12] or 0.0)
                                ),
                                static_collision=bool(row[13] or 0),
                            )
                            bullet_location_effects = list(
                                self._load_location_aura_effects(
                                    database, str(row[9] or "")
                                )
                            )
                            bullet_location_visual_effects = list(
                                self._load_victim_status_effects(
                                    database, str(row[9] or "")
                                )
                            )
                            bullet_death_effects = list(
                                self._load_location_aura_effects(
                                    database, str(row[10] or "")
                                )
                            )
                            velocity = max(0.0, float(row[2] or 0))
                            lifetime = max(0.0, float(row[3] or 0))
                            impact_count = max(1, int(row[4] or 1))
                    continue

                if (
                    normalized_kind != "skillstatuseffect"
                    or len(arguments) < 3
                    or not self._skill_scope_matches(
                        geometry, (arguments[1],)
                    )
                ):
                    continue
                operation = arguments[0].casefold()
                encoded = ",".join(arguments[2:])
                if operation.endswith("bdle"):
                    effects = self._load_location_aura_effects(
                        database, encoded
                    )
                    destination = bullet_death_effects
                elif operation.endswith("ble"):
                    effects = self._load_location_aura_effects(
                        database, encoded
                    )
                    destination = bullet_location_effects
                    visual_effects = self._load_victim_status_effects(
                        database, encoded
                    )
                    bullet_location_visual_effects.extend(visual_effects)
                elif operation.endswith("le"):
                    effects = self._load_location_aura_effects(
                        database, encoded
                    )
                    destination = location_effects
                    visual_effects = self._load_victim_status_effects(
                        database, encoded
                    )
                    for visual_effect in visual_effects:
                        visual_index = next(
                            (
                                index
                                for index, current in enumerate(
                                    location_visual_effects
                                )
                                if current.resource_id
                                == visual_effect.resource_id
                            ),
                            None,
                        )
                        if operation.startswith("mod"):
                            # ``modLE`` mutates an already-authored effect. It
                            # must not synthesize a missing location effect;
                            # only ``addLE`` is allowed to append one.
                            if visual_index is None:
                                continue
                            location_visual_effects[visual_index] = (
                                self._merge_status_effect_modifier(
                                    database,
                                    location_visual_effects[
                                        visual_index
                                    ],
                                    visual_effect,
                                    encoded,
                                    stack_count,
                                )
                            )
                        else:
                            location_visual_effects.append(visual_effect)
                else:
                    effects = self._load_victim_status_effects(
                        database, encoded,
                        default_trigger=("skillstart" if operation.endswith("ue") else "hit"),
                    )
                    destination = (
                        user_effects
                        if operation.endswith("ue")
                        else victim_effects
                    )
                for effect in effects:
                    existing_index = next(
                        (
                            index
                            for index, current in enumerate(destination)
                            if current.resource_id == effect.resource_id
                        ),
                        None,
                    )
                    if operation.startswith("mod"):
                        # A modifier whose target is absent is a no-op. The
                        # previous fallback appended it and turned conditional
                        # talent/item programs into brand-new area effects.
                        if existing_index is None:
                            continue
                        destination[existing_index] = (
                            self._merge_status_effect_modifier(
                                database,
                                destination[existing_index],
                                effect,
                                encoded,
                                stack_count,
                            )
                        )
                        continue
                    if stack_count > 1:
                        effect = replace(
                            effect,
                            chance=min(
                                1.0, effect.chance * stack_count
                            ),
                            values=tuple(
                                value * stack_count
                                for value in effect.values
                            ),
                        )
                    destination.append(effect)

        def latest_by_resource(values):
            result = {}
            for value in values:
                result[value.resource_id] = value
            return tuple(result.values())

        lifetime = max(0.0, lifetime)
        velocity = max(0.0, velocity)
        encoded_damage_types = ";".join(
            (
                key.title() if key != "darkmagic" else "DarkMagic"
            ) + f":{weight:g}"
            for key, weight in damage_weights.items()
            if weight > 1e-9
        ) or "Physical"
        result = replace(
            geometry,
            attack_range=max(0.0, attack_range),
            hit_range=max(0.0, hit_range),
            bullet_count=max(1, bullet_count),
            projectile_lifetime=lifetime,
            projectile_velocity=velocity,
            projectile_range=(
                velocity * lifetime
                if lifetime > 0.0
                else geometry.projectile_range
            ),
            impact_count=max(1, impact_count),
            damage_modifier=max(0.0, damage_modifier),
            skill_timing_multiplier=max(0.01, timing_multiplier),
            damage_type=encoded_damage_types,
            user_status_effects=latest_by_resource(user_effects),
            victim_status_effects=latest_by_resource(victim_effects),
            location_status_effects=latest_by_resource(location_effects),
            location_visual_effects=latest_by_resource(
                location_visual_effects
            ),
            bullet_location_effects=latest_by_resource(
                bullet_location_effects
            ),
            bullet_location_visual_effects=latest_by_resource(
                bullet_location_visual_effects
            ),
            bullet_death_effects=latest_by_resource(
                bullet_death_effects
            ),
            trap_on_hit=trap_on_hit,
            resource_cost_fraction=max(0.0, resource_cost_fraction),
            resource_gain_fraction=max(0.0, resource_gain_fraction),
            cleanse_groups=frozenset(cleanse_groups),
        )
        # Bound memory even when development sessions repeatedly alter socket
        # and talent loadouts.  Dicts preserve insertion order, so this is a
        # constant-time FIFO eviction with no maintenance in the battle loop.
        if len(self._modified_player_geometry_cache) >= 2048:
            self._modified_player_geometry_cache.pop(
                next(iter(self._modified_player_geometry_cache))
            )
        self._modified_player_geometry_cache[cache_key] = result
        return result

    def player_skill_resource_fractions(
        self,
        skill_index: int,
        *,
        modifier_sources: tuple[object, ...] = (),
    ) -> tuple[float, float]:
        geometry = self._load_player_skill_geometry(skill_index)
        if geometry is None:
            return (0.0, 0.0)
        geometry = self._modified_player_skill_geometry(
            geometry, tuple(modifier_sources)
        )
        return (
            max(0.0, float(geometry.resource_cost_fraction)),
            max(0.0, float(geometry.resource_gain_fraction)),
        )

    def player_skill_ammo_cost(self, skill_index: int) -> int:
        """Essences one accepted cast of this skill spends (``AmmoCost``)."""

        geometry = self._load_player_skill_geometry(skill_index)
        return 0 if geometry is None else max(0, int(geometry.ammo_cost))

    def player_skill_cleanse_groups(
        self,
        skill_index: int,
        *,
        modifier_sources: tuple[object, ...] = (),
    ) -> frozenset[str]:
        """Status groups one cast of this skill removes from its caster."""

        geometry = self._load_player_skill_geometry(skill_index)
        if geometry is None:
            return frozenset()
        return self._modified_player_skill_geometry(
            geometry, tuple(modifier_sources)
        ).cleanse_groups

    def player_status_resource_delta(
        self,
        definitions: tuple[VictimStatusEffectDefinition, ...],
        *,
        maximum_resource: float,
    ) -> float:
        """Resolve immediate ``CurrSkillResource`` modifier programs."""

        total = 0.0
        path = self.encounter.static_database_path
        with sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True
        ) as database:
            for definition in definitions:
                if "currskillresource" not in definition.modifier_kinds:
                    continue
                for kind, arguments in self._status_modifier_program(
                    int(definition.effect_id)
                ):
                    if (
                        kind.casefold() != "currskillresource"
                        or not arguments
                    ):
                        continue
                    amount = self._modifier_amount(
                        database, arguments[0], tuple(definition.values)
                    )
                    if amount is None:
                        continue
                    operation = (
                        arguments[1].casefold()
                        if len(arguments) > 1 else "absolute"
                    )
                    total += (
                        float(maximum_resource) * float(amount)
                        if operation == "relative" else float(amount)
                    )
        return total

    def player_skill_speed_multiplier(
        self,
        *,
        class_name: str,
        inventory,
        modifier_sources: tuple[object, ...] = (),
        player_stats=None,
        level: int = 100,
    ) -> float:
        """Quantize the authoritative raw speed to the client's animation rate.

        Retain item boundaries and active status conditions from the stat
        calculator; flattening the equipment here scales every item by every
        other item's enchantments and activates event-only child buffs.
        """
        if player_stats is None:
            from dso_server.gameplay.player_combat_stats import calculate_player_combat_stats
            from dso_server.protocol.inventory_codec import InventoryInfo

            player_stats = calculate_player_combat_stats(
                self.encounter.static_database_path,
                class_name=class_name,
                level=level,
                inventory=inventory if inventory is not None else InventoryInfo(),
                modifier_sources=modifier_sources,
            )
        return effective_attack_speed(player_stats.attack_speed)

    def _active_monster_modifier_sources(
        self,
        actor_id: int,
        *,
        now: float | None = None,
    ) -> tuple[VictimStatusEffectDefinition, ...]:
        current_time = time.perf_counter() if now is None else float(now)
        actor_id = int(actor_id)
        active = self.monster_status_effects.get(actor_id)
        if not active:
            return ()
        for resource_id, (_definition, expires_at) in tuple(active.items()):
            if expires_at is not None and expires_at <= current_time:
                del active[resource_id]
                self.monster_status_damage.pop(
                    (actor_id, resource_id), None
                )
        if not active:
            self.monster_status_effects.pop(actor_id, None)
            return ()
        return tuple(definition for definition, _expires_at in active.values())

    def _status_groups_for_effect(self, effect_id: int) -> frozenset[str]:
        """Return process-cached generated status groups for one effect row."""

        return self._status_effect_groups.get(int(effect_id), frozenset())

    def monster_has_status_group(
            self, actor_id: int, groups, *, now: float | None = None) -> bool:
        """Test active victim state using authored status groups.

        The first observation of an effect id resolves two small DB columns;
        every subsequent hit is a dictionary lookup. This keeps HitFrost and
        future group-driven jewel triggers accurate without scanning names or
        querying SQLite in the steady-state battle loop.
        """

        wanted = frozenset(str(value).casefold() for value in groups)
        return any(
            bool(wanted & self._status_groups_for_effect(source.effect_id))
            for source in self._active_monster_modifier_sources(
                actor_id, now=now
            )
        )

    def status_sources_have_group(self, sources, groups) -> bool:
        """Test arbitrary active effect entries against authored groups."""

        wanted = frozenset(str(value).casefold() for value in groups)
        return any(
            bool(
                wanted
                & self._status_groups_for_effect(
                    int(getattr(source, "effect_id", -1))
                )
            )
            for source in tuple(sources)
            if int(getattr(source, "effect_id", -1)) >= 0
        )

    def _monster_actions_disabled(
        self, actor_id: int, *, now: float | None = None
    ) -> bool:
        """Honor authored Movement/RegularSkills feature locks.

        Freeze, stun, roots with a full slow, and equivalent future status
        effects all use these database modifiers.  Reading the program keeps
        the battle loop independent of individual effect resource names.
        """

        reason = self._monster_action_lock_reason(actor_id, now=now)
        self._record_control_transition(actor_id, reason)
        return reason is not None

    def _monster_action_lock_reason(
        self, actor_id: int, *, now: float | None = None
    ) -> str | None:
        """Name the effect currently locking this actor out, if any."""

        current_time = time.perf_counter() if now is None else float(now)
        if self._monster_action_lock_cache_at != current_time:
            self._monster_action_lock_cache_at = current_time
            self._monster_action_lock_cache.clear()
        actor_id = int(actor_id)
        if actor_id in self._monster_action_lock_cache:
            return self._monster_action_lock_cache[actor_id]
        sources = self._active_monster_modifier_sources(
            actor_id, now=current_time
        )
        if not sources:
            self._monster_action_lock_cache[actor_id] = None
            return None
        reason = None
        for source in sources:
            for kind, arguments in self._status_modifier_program(
                source.effect_id
            ):
                lowered = tuple(value.casefold() for value in arguments)
                if (
                    kind.casefold() == "actorfeature"
                    and lowered
                    and lowered[0] == "off"
                    and any(
                        value in {"movement", "skills", "regularskills"}
                        for value in lowered[1:]
                    )
                ):
                    reason = f"{source.resource_id}:ActorFeature"
                    break
                if (
                    kind.casefold() == "speed"
                    and len(arguments) >= 3
                    and "movement" in lowered[2:]
                ):
                    amount = self._modifier_amount(
                        None, arguments[0], source.values
                    )
                    if amount is not None and amount <= -0.999:
                        reason = f"{source.resource_id}:Speed"
                        break
            if reason is not None:
                break
        self._monster_action_lock_cache[actor_id] = reason
        return reason

    def _record_control_transition(
        self, actor_id: int, reason: str | None
    ) -> None:
        """Note when an actor is locked out of acting, and by what.

        The server's own answer to "why is this monster not responding".
        Without it, a disabled actor and a healthy actor whose packets never
        arrived look identical from the log.
        """

        actor_id = int(actor_id)
        previous = self._monster_control_reason.get(actor_id)
        if previous == reason:
            return
        if reason is None:
            self._monster_control_reason.pop(actor_id, None)
        else:
            self._monster_control_reason[actor_id] = reason
        self._control_transitions.append((actor_id, reason))

    def drain_monster_control_transitions(
        self,
    ) -> tuple[tuple[int, str | None], ...]:
        transitions = tuple(self._control_transitions)
        self._control_transitions.clear()
        return transitions

    def disabled_monster_actor_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._monster_control_reason))

    def _monster_is_mind_controlled(
        self, actor_id: int, *, now: float | None = None
    ) -> bool:
        return any(
            effect.resource_id == "skill_mindcontrol_effect"
            for effect in self._active_monster_modifier_sources(
                actor_id, now=now
            )
        )

    def _summon_status_scale(self, actor_id, kind, scope, *, now=None):
        """Apply authored relative debuffs to a minion's speed or damage."""
        relative = 0.0
        for source in self._active_monster_modifier_sources(actor_id, now=now):
            for modifier_kind, arguments in self._status_modifier_program(source.effect_id):
                if (modifier_kind.casefold() != kind.casefold() or len(arguments) < 2
                        or arguments[1].casefold() != "relative"):
                    continue
                scopes = {value.casefold() for value in arguments[2:]}
                if scopes and scope.casefold() not in scopes:
                    continue
                amount = self._modifier_amount(None, arguments[0], source.values)
                if amount is not None:
                    relative += amount * max(1, int(source.stack_count))
        return max(0.0, 1.0 + relative)

    def _monster_skill_damage_multiplier(
            self, actor_id: int, *, now: float | None = None) -> float:
        """Resolve active relative ``SkillDamage`` monster debuffs.

        Socket aura children carry their concrete values in the effect entry,
        so the steady-state attack path only parses a tiny cached modifier
        string and never opens SQLite.
        """

        multiplier = 1.0
        for source in self._active_monster_modifier_sources(actor_id, now=now):
            _interval, _ticks, start = self._status_effect_runtime_program(
                source.effect_id
            )
            for raw in str(start).split(";"):
                kind, separator, encoded = raw.partition(":")
                if not separator or kind.strip().casefold() != "skilldamage":
                    continue
                arguments = tuple(
                    value.strip() for value in encoded.split(",")
                )
                if (
                    len(arguments) < 2
                    or not any(
                        value.casefold() == "monsterskills"
                        for value in arguments[2:]
                    )
                ):
                    continue
                raw_amount = arguments[0]
                amount = None
                if raw_amount.startswith("$") and raw_amount[1:].isdigit():
                    index = int(raw_amount[1:])
                    if index < len(source.values):
                        amount = float(source.values[index])
                else:
                    try:
                        amount = float(raw_amount)
                    except ValueError:
                        pass
                if amount is None:
                    continue
                if arguments[1].casefold() == "relative":
                    multiplier *= max(0.0, 1.0 + amount)
                else:
                    multiplier = max(0.0, multiplier + amount)
        return max(0.0, multiplier)

    def _mind_control_taunt_radius(self) -> float:
        cached = getattr(self, "_mind_control_taunt_radius_value", None)
        if cached is not None:
            return float(cached)
        path = self.encounter.static_database_path
        with sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True
        ) as database:
            row = database.execute(
                "SELECT AuraRadius FROM _Template_StatusEffect "
                "WHERE Id='skill_mindcontrol_taunt_aura' LIMIT 1"
            ).fetchone()
        cached = max(1.0, float(row[0] or 8.0)) if row else 8.0
        self._mind_control_taunt_radius_value = cached
        return cached

    def _record_monster_status_effects(
        self,
        actor_id: int,
        definitions: tuple[VictimStatusEffectDefinition, ...],
        *,
        now: float,
        source_actor_id: int = 1,
        player_damage_range: tuple[float, float] = (1.0, 1.0),
        skill_index: int = 0,
    ) -> None:
        active = self.monster_status_effects.setdefault(int(actor_id), {})
        for definition in definitions:
            if self.monster_status_effect_blocked(
                actor_id, definition.resource_id, now=now
            ):
                continue
            damage_program = self.monster_status_damage_program(definition)
            interval = damage_program[0] if damage_program is not None else 0.0
            duration_seconds = (
                definition.duration_ticks / SERVER_TICKS_PER_SECOND
                if definition.duration_ticks > 0
                else max(1.0 / SERVER_TICKS_PER_SECOND, interval)
            )
            expires_at = float(now) + duration_seconds
            active[definition.resource_id] = (
                definition,
                expires_at,
            )
            self._arm_monster_status_immunity(
                actor_id, definition.resource_id, expires_at=expires_at
            )
            key = (int(actor_id), definition.resource_id)
            if damage_program is None:
                self.monster_status_damage.pop(key, None)
                continue
            interval, coefficient, damage_type, relative = damage_program
            previous = self.monster_status_damage.get(key)
            self.monster_status_damage[key] = _MonsterStatusDamage(
                definition=definition,
                source_actor_id=int(source_actor_id),
                player_damage_range=(
                    float(player_damage_range[0]),
                    float(player_damage_range[1]),
                ),
                skill_index=int(skill_index),
                interval_seconds=interval,
                coefficient=coefficient,
                damage_type=damage_type,
                relative=relative,
                next_at=(
                    previous.next_at
                    if previous is not None
                    else float(now) + interval
                ),
                expires_at=expires_at,
                stack_count=(
                    min(
                        max(1, int(definition.max_stack_size)),
                        previous.stack_count + 1,
                    )
                    if previous is not None
                    else 1
                ),
            )

    def _arm_monster_status_immunity(
        self, actor_id: int, resource_id: str, *, expires_at: float
    ) -> None:
        """Open this effect's authored immunity window at its expiry."""

        _groups, immunity = self._status_effect_content(resource_id)
        if immunity is None:
            return
        blocked, duration = immunity
        windows = self.monster_status_immunities.setdefault(int(actor_id), {})
        for group in blocked:
            existing = windows.get(group)
            end = float(expires_at) + float(duration)
            if existing is not None and existing[1] >= end:
                continue
            windows[group] = (float(expires_at), end)

    def accepted_monster_status_effects(
        self, actor_id: int, definitions, *, now: float
    ) -> tuple:
        """Drop victim effects an authored immunity window currently refuses.

        Applied before the resolution is built, not only before it is
        recorded: the client derives its own crowd control from the stack
        occurrences it receives, so an effect the server refuses must not
        reach the wire either.
        """

        if VICTIM_STATUS_EFFECTS_DISABLED or STATUS_EFFECTS_DISABLED:
            return ()
        return tuple(
            definition
            for definition in definitions
            if not self.monster_status_effect_blocked(
                actor_id, definition.resource_id, now=now
            )
        )

    def monster_status_effect_blocked(
        self, actor_id: int, resource_id: str, *, now: float
    ) -> bool:
        """Report whether an authored immunity currently refuses this effect."""

        windows = self.monster_status_immunities.get(int(actor_id))
        if not windows:
            return False
        current = float(now)
        for group, (start, end) in tuple(windows.items()):
            if current >= end:
                del windows[group]
        if not windows:
            self.monster_status_immunities.pop(int(actor_id), None)
            return False
        groups, _immunity = self._status_effect_content(resource_id)
        if not groups:
            return False
        return any(
            start <= current < end
            for group, (start, end) in windows.items()
            if group in groups
        )

    def monster_incoming_damage_multiplier(
        self,
        actor_id: int,
        damage_type: str,
        *,
        now: float | None = None,
    ) -> float:
        """Apply active armor/resistance-break programs to incoming damage.

        The status-effect database expresses armor as Physical Resistance and
        elemental breaks as Resistance scoped to one or more damage types.
        Values are carried by the active victim effect (``$0`` etc.), so the
        same evaluator covers Singularity and every class' break effects.
        """

        sources = self._active_monster_modifier_sources(actor_id, now=now)
        if not sources:
            return 1.0
        wanted = {
            entry.partition(":")[0].strip().casefold()
            for entry in str(damage_type or "Physical").split(";")
            if entry.strip()
        }
        if "andermagic" in wanted:
            wanted.add("darkmagic")
        if "darkmagic" in wanted:
            wanted.add("andermagic")
        multiplier = 1.0
        for source in sources:
            for kind, arguments in self._status_modifier_program(
                source.effect_id
            ):
                if kind.casefold() not in {"armor", "resistance"}:
                    continue
                if len(arguments) < 3:
                    continue
                scopes = {
                    self._clean_modifier_token(value).casefold()
                    for value in arguments[2:]
                }
                if wanted.isdisjoint(scopes) and "all" not in scopes:
                    continue
                amount = self._modifier_amount(
                    None, arguments[0], source.values
                )
                if amount is None:
                    continue
                if arguments[1].casefold() == "relative":
                    # A -30% resistance program makes this hit 30% more
                    # effective. Positive defensive buffs reduce it.
                    multiplier *= max(
                        0.05, 1.0 - float(amount)
                    )
                else:
                    multiplier *= max(
                        0.05, 1.0 - float(amount) / 100.0
                    )
        return max(0.05, min(10.0, multiplier))

    @staticmethod
    def _point_segment_distance_2d(
        point_x: float,
        point_z: float,
        start_x: float,
        start_z: float,
        end_x: float,
        end_z: float,
    ) -> tuple[float, float]:
        '''Return distance to a 2D segment and its clamped segment parameter.'''

        segment_x = end_x - start_x
        segment_z = end_z - start_z
        length_squared = segment_x * segment_x + segment_z * segment_z
        if length_squared <= 1e-12:
            return math.hypot(point_x - start_x, point_z - start_z), 0.0
        parameter = max(
            0.0,
            min(
                1.0,
                (
                    (point_x - start_x) * segment_x
                    + (point_z - start_z) * segment_z
                ) / length_squared,
            ),
        )
        closest_x = start_x + segment_x * parameter
        closest_z = start_z + segment_z * parameter
        return math.hypot(point_x - closest_x, point_z - closest_z), parameter

    @classmethod
    def _swept_monster_ray_proximity(
        cls,
        origin: tuple[float, float, float],
        aim_x: float,
        aim_z: float,
        maximum_range: float,
        published: tuple[float, float, float],
        current: tuple[float, float, float],
    ) -> tuple[float, float]:
        '''Return forward distance and miss distance for two swept segments.'''

        cast_x = aim_x * maximum_range
        cast_z = aim_z * maximum_range
        movement_x = current[0] - published[0]
        movement_z = current[2] - published[2]
        relative_x = published[0] - origin[0]
        relative_z = published[2] - origin[2]
        denominator = cast_x * movement_z - cast_z * movement_x
        if abs(denominator) > 1e-12:
            cast_parameter = (
                relative_x * movement_z - relative_z * movement_x
            ) / denominator
            movement_parameter = (
                relative_x * cast_z - relative_z * cast_x
            ) / denominator
            if (
                0.0 <= cast_parameter <= 1.0
                and 0.0 <= movement_parameter <= 1.0
            ):
                return cast_parameter * maximum_range, 0.0

        cast_end_x = origin[0] + cast_x
        cast_end_z = origin[2] + cast_z
        published_distance, published_cast_parameter = (
            cls._point_segment_distance_2d(
                published[0], published[2],
                origin[0], origin[2], cast_end_x, cast_end_z,
            )
        )
        current_distance, current_cast_parameter = (
            cls._point_segment_distance_2d(
                current[0], current[2],
                origin[0], origin[2], cast_end_x, cast_end_z,
            )
        )
        origin_distance, _ = cls._point_segment_distance_2d(
            origin[0], origin[2],
            published[0], published[2], current[0], current[2],
        )
        end_distance, _ = cls._point_segment_distance_2d(
            cast_end_x, cast_end_z,
            published[0], published[2], current[0], current[2],
        )
        distance, forward = min(
            (
                (published_distance, published_cast_parameter * maximum_range),
                (current_distance, current_cast_parameter * maximum_range),
                (origin_distance, 0.0),
                (end_distance, maximum_range),
            )
        )
        return forward, distance

    def _bounded_published_monster_position(
        self,
        monster: MonsterCombatant,
        current: tuple[float, float, float],
    ) -> tuple[float, float, float]:
        '''Return at most one publish interval of client-visible movement.'''

        published = self.monster_last_published_positions.get(
            monster.actor_id, current
        )
        dx = published[0] - current[0]
        dz = published[2] - current[2]
        distance = math.hypot(dx, dz)
        maximum_sweep = (
            max(0.0, monster.run_speed)
            * MONSTER_MOVEMENT_PUBLISH_INTERVAL_TICKS
            / SERVER_TICKS_PER_SECOND
        )
        if distance <= maximum_sweep + 1e-6:
            return published
        if distance <= 1e-12 or maximum_sweep <= 0.0:
            return current
        ratio = maximum_sweep / distance
        return (
            current[0] + dx * ratio,
            current[1] + (published[1] - current[1]) * ratio,
            current[2] + dz * ratio,
        )

    def _viewer_monster_ray_proximity(
        self,
        origin: tuple[float, float, float],
        aim_x: float,
        aim_z: float,
        maximum_range: float,
        monster: MonsterCombatant,
        current: tuple[float, float, float],
        collision_radius: float,
        visible_position: tuple[float, float, float] | None = None,
    ) -> tuple[float, float]:
        """Collide with authoritative movement and this viewer's last snapshot.

        The shared published-position state advances before movement is fanned
        out. A viewer whose replaceable datagram was skipped can still render
        an older transform. Keep that exact submitted transform as another
        point collision instead of sweeping the complete old-to-current path,
        which could fabricate hits across route corners.
        """

        published = self._bounded_published_monster_position(monster, current)
        candidates = [
            self._swept_monster_ray_proximity(
                origin,
                aim_x,
                aim_z,
                maximum_range,
                published,
                current,
            )
        ]
        if visible_position is not None:
            visible = tuple(float(value) for value in visible_position)
            candidates.append(
                self._swept_monster_ray_proximity(
                    origin,
                    aim_x,
                    aim_z,
                    maximum_range,
                    visible,
                    visible,
                )
            )
        colliding = tuple(
            value
            for value in candidates
            if value[1] <= max(0.0, float(collision_radius)) + 1e-9
        )
        if colliding:
            return min(colliding, key=lambda value: (value[0], value[1]))
        return min(candidates, key=lambda value: (value[1], value[0]))

    def _client_visible_projectile_distance(
        self,
        player_position: tuple[float, float, float],
        direction: tuple[float, float, float, float],
        geometry: PlayerSkillGeometry,
        monster: MonsterCombatant,
        visible_position: tuple[float, float, float] | None = None,
    ) -> float:
        """Return the ray collision distance against the visible move sweep.

        Target acquisition already accepts the capsule between the last
        published transform and the newer authoritative transform. Scheduling
        against only the latter made the hit arrive after the projectile had
        crossed the monster position the client was actually rendering.
        """

        direction_length = math.hypot(direction[0], direction[2])
        maximum_range = max(
            geometry.attack_range,
            geometry.projectile_range,
        )
        if direction_length <= 1e-6 or maximum_range <= 0.0:
            return self._distance(
                player_position,
                self.monster_positions[monster.actor_id],
            )
        aim_x = direction[0] / direction_length
        aim_z = direction[2] / direction_length
        current = self.monster_positions[monster.actor_id]
        collision_radius = monster.collision_radius + max(
            0.25,
            geometry.projectile_radius,
            geometry.player_width / 2.0,
        )
        forward, _miss_distance = self._viewer_monster_ray_proximity(
            player_position,
            aim_x,
            aim_z,
            maximum_range,
            monster,
            current,
            collision_radius,
            visible_position,
        )
        return max(0.0, min(maximum_range, forward))

    def _skill_target(
        self,
        player_position: tuple[float, float, float],
        direction: tuple[float, float, float, float],
        geometry: PlayerSkillGeometry,
    ) -> MonsterCombatant | None:
        targets = self._skill_targets(
            player_position, direction, geometry
        )
        return targets[0] if targets else None

    def _skill_targets(
        self,
        player_position: tuple[float, float, float],
        direction: tuple[float, float, float, float],
        geometry: PlayerSkillGeometry,
        eligible_actor_ids: frozenset[int] | None = None,
        visible_actor_positions: (
            dict[int, tuple[float, float, float]] | None
        ) = None,
    ) -> tuple[MonsterCombatant, ...]:
        """Return every collision candidate in projectile travel order."""

        living = [
            self.monsters[actor_id]
            for actor_id in self.living_actor_ids
            if (
                actor_id not in self.returning_home_actor_ids
                and (
                    eligible_actor_ids is None
                    or actor_id in eligible_actor_ids
                )
            )
        ]
        if not living:
            return ()

        direction_length = math.hypot(direction[0], direction[2])
        if direction_length <= 1e-6:
            return ()

        aim_x = direction[0] / direction_length
        aim_z = direction[2] / direction_length
        # AttackRange is the authored cast range. Bullet lifetime controls the
        # visual object and can extend that range through modifiers, but a
        # shorter lifetime must not silently truncate a valid client cast.
        maximum_range = max(
            geometry.attack_range, geometry.projectile_range
        )
        if maximum_range <= 0.0:
            return ()
        targeting = geometry.targeting_type.casefold()
        actor_targeted = targeting == 'actor'
        is_directional_area = targeting in {"angle", "cone"}
        candidates = []
        for monster in living:
            position = self.monster_positions[monster.actor_id]
            dx = position[0] - player_position[0]
            dz = position[2] - player_position[2]
            distance = math.hypot(dx, dz)
            forward = dx * aim_x + dz * aim_z
            # HitRange is an area-skill radius (and is often equal to the
            # whole 25-unit cast range on mage missiles). It is not the
            # lateral width of a projectile ray.
            collision_radius = monster.collision_radius + max(
                0.25,
                geometry.projectile_radius,
                geometry.player_width / 2.0,
            )
            if is_directional_area:
                if (
                    forward < 0.0
                    or distance > maximum_range + collision_radius
                ):
                    continue
                lateral = abs(dx * aim_z - dz * aim_x)
                if targeting == "angle":
                    half_width = max(0.0, forward) * math.tan(
                        math.radians(
                            max(0.0, geometry.angle_degrees) / 2.0
                        )
                    )
                else:
                    progress = min(
                        1.0,
                        max(0.0, forward) / max(maximum_range, 1e-6),
                    )
                    half_width = (
                        geometry.player_width / 2.0
                        + (
                            geometry.end_width - geometry.player_width
                        )
                        / 2.0
                        * progress
                    )
                if lateral > max(0.0, half_width) + collision_radius:
                    continue
                miss_distance = lateral
            else:
                # Authoritative AI continues at 25 Hz even when this viewer's
                # replaceable movement datagram is skipped. Test both the
                # bounded map publish sweep and that viewer's delivered point.
                forward, miss_distance = self._viewer_monster_ray_proximity(
                    player_position,
                    aim_x,
                    aim_z,
                    maximum_range,
                    monster,
                    position,
                    collision_radius,
                    (
                        visible_actor_positions.get(monster.actor_id)
                        if visible_actor_positions is not None
                        else None
                    ),
                )
                if miss_distance > collision_radius:
                    continue
            candidates.append(
                (forward, miss_distance, monster.actor_id, monster)
            )
        candidates.sort(key=lambda value: value[:3])
        return tuple(value[3] for value in candidates)

    def _stationary_directional_targets(
        self,
        origin: tuple[float, float, float],
        direction: tuple[float, float, float, float],
        geometry: PlayerSkillGeometry,
    ) -> tuple[MonsterCombatant, ...]:
        """Return actors in a fixed cone without projectile-ray padding.

        Projectile collision includes the projectile/player radii. A
        stationary sustained turret already has a complete width authored by
        PlayerWidth and EndWidth, so adding those radii again nearly doubles
        its lateral reach. Only the victim collision capsule extends the
        authored area.
        """

        direction_length = math.hypot(direction[0], direction[2])
        maximum_range = max(
            geometry.attack_range,
            geometry.hit_range,
        )
        if direction_length <= 1e-6 or maximum_range <= 0.0:
            return ()
        aim_x = direction[0] / direction_length
        aim_z = direction[2] / direction_length
        targeting = geometry.targeting_type.casefold()
        candidates = []
        for actor_id in self.living_actor_ids:
            if actor_id in self.returning_home_actor_ids:
                continue
            monster = self.monsters[actor_id]
            position = self.monster_positions[actor_id]
            dx = position[0] - origin[0]
            dz = position[2] - origin[2]
            distance = math.hypot(dx, dz)
            forward = dx * aim_x + dz * aim_z
            if (
                forward < -monster.collision_radius
                or distance > maximum_range + monster.collision_radius
            ):
                continue
            lateral = abs(dx * aim_z - dz * aim_x)
            if targeting == "angle":
                half_width = max(0.0, forward) * math.tan(
                    math.radians(
                        max(0.0, geometry.angle_degrees) / 2.0
                    )
                )
            else:
                progress = min(
                    1.0,
                    max(0.0, forward) / max(maximum_range, 1e-6),
                )
                half_width = (
                    geometry.player_width / 2.0
                    + (
                        geometry.end_width - geometry.player_width
                    )
                    / 2.0
                    * progress
                )
                if (
                    half_width <= 0.0
                    and geometry.angle_degrees > 0.0
                ):
                    half_width = max(0.0, forward) * math.tan(
                        math.radians(
                            geometry.angle_degrees / 2.0
                        )
                    )
            if lateral > max(0.0, half_width) + monster.collision_radius:
                continue
            candidates.append(
                (max(0.0, forward), lateral, actor_id, monster)
            )
        candidates.sort(key=lambda value: value[:3])
        return tuple(value[3] for value in candidates)

    @staticmethod
    def _stationary_directional_position_is_target(
        origin: tuple[float, float, float],
        direction: tuple[float, float, float, float],
        geometry: PlayerSkillGeometry,
        position: tuple[float, float, float],
        *,
        collision_radius: float = 0.75,
    ) -> bool:
        """Apply a stationary cone/strip to a non-monster actor position."""

        direction_length = math.hypot(direction[0], direction[2])
        maximum_range = max(geometry.attack_range, geometry.hit_range)
        if direction_length <= 1e-6 or maximum_range <= 0.0:
            return False
        aim_x = direction[0] / direction_length
        aim_z = direction[2] / direction_length
        dx = position[0] - origin[0]
        dz = position[2] - origin[2]
        distance = math.hypot(dx, dz)
        forward = dx * aim_x + dz * aim_z
        radius = max(0.0, float(collision_radius))
        if forward < -radius or distance > maximum_range + radius:
            return False
        lateral = abs(dx * aim_z - dz * aim_x)
        if geometry.targeting_type.casefold() == "angle":
            half_width = max(0.0, forward) * math.tan(
                math.radians(max(0.0, geometry.angle_degrees) / 2.0)
            )
        else:
            progress = min(
                1.0, max(0.0, forward) / max(maximum_range, 1e-6)
            )
            half_width = (
                geometry.player_width / 2.0
                + (geometry.end_width - geometry.player_width)
                / 2.0
                * progress
            )
            if half_width <= 0.0 and geometry.angle_degrees > 0.0:
                half_width = max(0.0, forward) * math.tan(
                    math.radians(geometry.angle_degrees / 2.0)
                )
        return lateral <= max(0.0, half_width) + radius

    def _separate_destination(
        self,
        actor_id: int,
        destination: tuple[float, float, float],
        player_position: tuple[float, float, float],
        occupied: list[tuple[float, float, float]],
    ) -> tuple[float, float, float]:
        """Keep independently routed actors from converging into one model."""

        clearance = self.monsters[actor_id].collision_radius * 2.0 + 0.15
        result = destination
        for other in occupied:
            dx = result[0] - other[0]
            dz = result[2] - other[2]
            separation = math.hypot(dx, dz)
            if separation >= clearance:
                continue
            if separation > 1e-6:
                away_x, away_z = dx / separation, dz / separation
            else:
                # At identical melee destinations, use a deterministic
                # tangent around the player rather than teleporting outward.
                radial_x = result[0] - player_position[0]
                radial_z = result[2] - player_position[2]
                radial_length = math.hypot(radial_x, radial_z)
                if radial_length <= 1e-6:
                    radial_x, radial_z, radial_length = 1.0, 0.0, 1.0
                sign = -1.0 if actor_id & 1 else 1.0
                away_x = -radial_z / radial_length * sign
                away_z = radial_x / radial_length * sign
            correction = clearance - separation
            result = (
                result[0] + away_x * correction,
                destination[1],
                result[2] + away_z * correction,
            )
        return result

    def _advance_on_cached_route(
        self,
        actor_id: int,
        start: tuple[float, float, float],
        goal: tuple[float, float, float],
        travel: float,
        server_tick: int,
        *,
        home: tuple[float, float, float],
        territory_radius: float,
    ) -> tuple[float, float, float]:
        """Advance on a cached A* route and refresh it only when necessary."""

        tick = int(server_tick) & 0xFFFFFFFF
        route_state = self.monster_routes.get(actor_id)
        route_age = (
            (tick - route_state.refreshed_tick) & 0xFFFFFFFF
            if route_state is not None
            else 0xFFFFFFFF
        )
        if (
            (route_state is None
             or route_age >= round(SERVER_TICKS_PER_SECOND)
             or self._distance(route_state.goal, goal) >= 1.5)
            and self._route_refresh_budget > 0
        ):
            # Large maps can have several nearby actors whose route searches
            # each visit thousands of navigation triangles. Stagger searches
            # across game ticks instead of producing one CPU spike.
            self._route_refresh_budget -= 1
            try:
                vertical_offset = self.monster_navigation_offsets.get(
                    actor_id, 0.0
                )
                route = self.encounter.navigation.find_route(
                    (
                        start[0],
                        start[1] - vertical_offset,
                        start[2],
                    ),
                    (
                        goal[0],
                        goal[1] - vertical_offset,
                        goal[2],
                    ),
                    search_center=(
                        home[0],
                        home[1] - vertical_offset,
                        home[2],
                    ),
                    max_search_distance=territory_radius,
                )
                waypoints = tuple(
                    (
                        point[0],
                        point[1] + vertical_offset,
                        point[2],
                    )
                    for point in route.points[1:]
                )
            except NavigationError:
                # Cache a failed search for one second as well. Re-running the
                # same impossible route at 25 Hz was the dominant map-server
                # CPU load in the recorded 662-monster encounter.
                waypoints = ()
            route_state = _MonsterRouteState(
                goal=goal,
                waypoints=waypoints,
                next_waypoint=0,
                refreshed_tick=tick,
            )
            self.monster_routes[actor_id] = route_state

        if route_state is None:
            return start
        remaining = max(0.0, float(travel))
        current = start
        while (
            remaining > 1e-8
            and route_state.next_waypoint < len(route_state.waypoints)
        ):
            waypoint = route_state.waypoints[route_state.next_waypoint]
            dx = waypoint[0] - current[0]
            dz = waypoint[2] - current[2]
            distance = math.hypot(dx, dz)
            if distance <= 1e-5:
                route_state.next_waypoint += 1
                continue
            if distance <= remaining:
                current = waypoint
                remaining -= distance
                route_state.next_waypoint += 1
                continue
            ratio = remaining / distance
            current = (
                current[0] + dx * ratio,
                current[1] + (waypoint[1] - current[1]) * ratio,
                current[2] + dz * ratio,
            )
            remaining = 0.0
        return current

    def ground_monster_position(
        self,
        actor_id: int,
        position: tuple[float, float, float],
        *,
        reference_position: tuple[float, float, float] | None = None,
        max_horizontal_snap: float | None = None,
    ) -> tuple[float, float, float]:
        """Clamp X/Z to navigation and Y to the rendered collision terrain."""

        vertical_offset = self.monster_navigation_offsets.get(
            int(actor_id), 0.0
        )
        try:
            projected = self.encounter.navigation.project_position(
                (
                    float(position[0]),
                    float(position[1]) - vertical_offset,
                    float(position[2]),
                ),
                max_horizontal_distance=max_horizontal_snap,
            )
        except NavigationError:
            if reference_position is not None:
                return reference_position
            raise
        if self.encounter.terrain is not None:
            try:
                return self.encounter.terrain.project_position(
                    projected,
                    reference_height=float(
                        reference_position[1]
                        if reference_position is not None
                        else position[1]
                    ),
                )
            except CollisionTerrainError:
                if reference_position is not None:
                    return reference_position
        return (
            projected[0],
            projected[1] + vertical_offset,
            projected[2],
        )

    def ground_loot_position(
        self,
        actor_id: int,
        position: tuple[float, float, float],
    ) -> tuple[float, float, float]:
        """Place a drop only on the killed monster's walkable component.

        Nearest-triangle projection without a snap limit can move a scattered
        item through a wall, across a mesh gap, or onto a decorative island.
        If the small scatter offset is not directly projectable on the same
        component, keep the drop at the monster's valid position.
        """

        actor_id = int(actor_id)
        fallback = self.monster_positions[actor_id]
        navigation = self.encounter.navigation
        try:
            if not navigation.is_reachable(
                fallback,
                position,
                max_horizontal_snap=1.5,
            ):
                return fallback
            projected = navigation.project_position(
                position,
                max_horizontal_distance=1.5,
            )
        except NavigationError:
            return fallback
        if self.encounter.terrain is not None:
            try:
                return self.encounter.terrain.project_position(
                    projected,
                    reference_height=float(fallback[1]),
                )
            except CollisionTerrainError:
                return fallback
        vertical_offset = self.monster_navigation_offsets.get(actor_id, 0.0)
        return (
            projected[0],
            projected[1] + vertical_offset,
            projected[2],
        )

    def assign_monster_targets(self, actor_ids, candidates):
        """Choose a visible, living player per monster; keep nearby targets stable.

        Candidates contain (player actor ID, world position, visible monsters).
        Position conversion and session eligibility belong to the caller.
        """
        result = {}
        for actor_id in actor_ids:
            position = self.monster_positions[actor_id]
            monster = self.monsters[actor_id]
            choices = []
            for player_id, world, visible in candidates:
                health = self.player_health.get(player_id)
                if actor_id not in visible or (health is not None and health[0] <= 0):
                    continue
                distance = math.hypot(world[0] - position[0], world[2] - position[2])
                choices.append((distance, player_id, world))
            if not choices:
                self.monster_target_actor_ids.pop(actor_id, None)
                continue
            nearest = min(choices)
            current = self.monster_target_actor_ids.get(actor_id)
            retained = next((choice for choice in choices if choice[1] == current), None)
            if (retained is not None and retained[0] <= monster.escape_range
                    and retained[0] <= nearest[0] + 2.0):
                nearest = retained
            self.monster_target_actor_ids[actor_id] = nearest[1]
            result[actor_id] = (nearest[1], nearest[2])
        return result

    def advance_monsters(
        self,
        player_position: tuple[float, float, float],
        *,
        elapsed_seconds: float,
        server_tick: int,
        eligible_actor_ids: (
            set[int] | frozenset[int] | None
        ) = None,
        now: float | None = None,
        targets_by_actor=None,
    ) -> tuple[MonsterMovement, ...]:
        """Advance living monsters along the map navigation mesh.

        A* chooses connected triangles inside each actor's database-owned
        TravelDistance around its spawn. AggroRange acquires a target,
        EscapeRange releases it, and ranged monsters retain firing distance.
        """

        # Client Move requests are relative to the IPAM translation. Combat,
        # spawn placement and PathEngine all operate in absolute map space.
        player_position = self.encounter.coordinate_frame.wire_to_world(
            player_position
        )
        elapsed = max(0.0, min(0.5, float(elapsed_seconds)))
        # Two searches keep ordinary two-monster groups synchronized while
        # still spreading larger vicinity groups across several 25 Hz ticks.
        self._route_refresh_budget = 2
        current_time = time.perf_counter() if now is None else float(now)
        movements = []
        occupied: list[tuple[float, float, float]] = []
        actors = tuple(actor_id for actor_id in self.living_actor_ids
                       if eligible_actor_ids is None or actor_id in eligible_actor_ids)
        # Rotate access to the bounded A* budget so low actor IDs cannot starve
        # newly visible packs while refreshing their own paths.
        if actors:
            cursor = self._route_actor_cursor % len(actors)
            actors = actors[cursor:] + actors[:cursor]
            self._route_actor_cursor = (cursor + 2) % len(actors)
        viewer_position = player_position
        for actor_id in actors:
            # Each actor's own target below (a viewer, or the monster a mind
            # controlled one turns on) must not leak into the next actor's.
            player_position = viewer_position
            monster = self.monsters[actor_id]
            home = monster.position
            start = self.monster_positions[actor_id]
            previous_yaw = self.monster_yaws[actor_id]
            was_moving = self.monster_moving[actor_id]
            # Mounted trap actors keep their authored transform. Separation
            # and route grounding must not drag a stationary tower off its
            # pedestal, even when a player or another monster overlaps it.
            if monster.run_speed <= 0.0:
                self.monster_moving[actor_id] = False
                occupied.append(start)
                continue
            scripted_goal = self.scripted_monster_targets.get(actor_id)
            if (
                scripted_goal is not None
                and self._distance(start, scripted_goal) <= 18.0
            ):
                # The last section of the F4203 authored ram route ends at the
                # arena gate, where the collision/nav mesh may not expose a
                # connected triangle to A*. Keep the ram on its fixed approach
                # and let the gate impact trigger finish the sequence.
                dx = scripted_goal[0] - start[0]
                dz = scripted_goal[2] - start[2]
                distance = math.hypot(dx, dz)
                step = min(float(monster.run_speed) * elapsed, distance)
                if distance > 1e-5 and step > 1e-6:
                    ratio = step / distance
                    destination = (
                        start[0] + dx * ratio,
                        start[1] + (scripted_goal[1] - start[1]) * ratio,
                        start[2] + dz * ratio,
                    )
                    self.monster_positions[actor_id] = destination
                    yaw = math.atan2(-dx, -dz)
                    self.monster_yaws[actor_id] = yaw
                    self.monster_moving[actor_id] = True
                    if self._monster_movement_publish_due(
                        actor_id,
                        server_tick,
                        moving=True,
                        was_moving=was_moving,
                        position=destination,
                    ):
                        movements.append(MonsterMovement(
                            actor_id,
                            destination,
                            self.encounter.coordinate_frame.world_to_wire(
                                destination
                            ),
                            yaw,
                            True,
                            int(server_tick) & 0xFFFFFFFF,
                        ))
                    occupied.append(destination)
                    continue
                self.monster_moving[actor_id] = False
                occupied.append(start)
                if was_moving and self._monster_movement_publish_due(
                    actor_id,
                    server_tick,
                    moving=False,
                    was_moving=True,
                    position=start,
                ):
                    movements.append(MonsterMovement(
                        actor_id,
                        start,
                        self.encounter.coordinate_frame.world_to_wire(start),
                        previous_yaw,
                        False,
                        int(server_tick) & 0xFFFFFFFF,
                    ))
                continue
            direct_distance = self._distance(start, player_position)
            home_distance = self._distance(start, home)
            player_home_distance = self._distance(player_position, home)
            returning_home = actor_id in self.returning_home_actor_ids
            if targets_by_actor is not None:
                target = targets_by_actor.get(actor_id)
                player_position = target[1] if target is not None else home
                direct_distance = self._distance(start, player_position)
                player_home_distance = self._distance(player_position, home)
                if target is None and not returning_home:
                    if home_distance > MONSTER_HOME_ARRIVAL_DISTANCE:
                        self._set_returning_home(
                            actor_id, True, server_tick, now=current_time,
                            reason="no target left for it",
                        )
                        returning_home = True
                    else:
                        self.monster_routes.pop(actor_id, None)
                        self.monster_moving[actor_id] = False
                        if was_moving:
                            movements.append(MonsterMovement(
                                actor_id, start, self.encounter.coordinate_frame.world_to_wire(start),
                                previous_yaw, False, int(server_tick) & 0xFFFFFFFF))
                        occupied.append(start)
                        continue
            if self._monster_is_mind_controlled(actor_id, now=current_time):
                # Mind control turns it against its own kind for the effect's
                # 10 s: it walks to the nearest uncontrolled monster within its
                # aggro range (maybe_start_monster_attacks already retargets
                # its blows) and leaves the players alone. It kept chasing the
                # player, so it never reached anything it could strike.
                reach = max(1.0, float(monster.aggro_range))
                enemies = sorted(
                    (self._distance(start, self.monster_positions[other]), other)
                    for other in self.living_actor_ids
                    if other != actor_id
                    and other in self.monster_positions
                    and not self._monster_is_mind_controlled(
                        other, now=current_time
                    )
                )
                enemies = [entry for entry in enemies if entry[0] <= reach]
                if not enemies:
                    self.monster_routes.pop(actor_id, None)
                    self.monster_moving[actor_id] = False
                    if was_moving:
                        movements.append(MonsterMovement(
                            actor_id, start, self.encounter.coordinate_frame.world_to_wire(start),
                            previous_yaw, False, int(server_tick) & 0xFFFFFFFF))
                    occupied.append(start)
                    continue
                player_position = self.monster_positions[enemies[0][1]]
                direct_distance = enemies[0][0]
                player_home_distance = self._distance(player_position, home)
            preferred_stop_distance = self._preferred_stop_distance(monster)
            if actor_id in self.scripted_monster_targets:
                # Scripted actors (such as the F4203 battering ram) are moving
                # to a fixed world target, not approaching a player to attack.
                # Their attack range must not leave them standing short of the
                # authored impact point.
                preferred_stop_distance = 0.0
            # A shooter with a wall between it and its target walks on to
            # find a line of sight instead of standing off behind the wall.
            if (
                preferred_stop_distance > MONSTER_STOP_DISTANCE
                and self._monster_shot_blocked(
                    monster.attack, start, player_position
                )
            ):
                preferred_stop_distance = MONSTER_STOP_DISTANCE
            # The authored territory, widened by the pursuit grace, and then
            # the distance at which the actor stops walking back and resets.
            pursuit_radius = (
                monster.travel_distance + MONSTER_CHASE_GRACE_DISTANCE
            )
            reset_radius = pursuit_radius + MONSTER_RESET_GRACE_DISTANCE

            # The recovery window measures time without progress, not total
            # return duration: a valid walk can legitimately take much longer.
            # Broken routes still have bounded immunity and eventually reset.
            if returning_home:
                started_at = self.returning_home_started_at.setdefault(
                    actor_id, current_time
                )
                progress = self.returning_home_progress.setdefault(actor_id, home_distance)
                if home_distance < progress - 0.10:
                    self.returning_home_progress[actor_id] = home_distance
                    self.returning_home_started_at[actor_id] = current_time
                    started_at = current_time
                if (
                    current_time - started_at
                    >= MONSTER_RETURN_HOME_TIMEOUT_SECONDS
                ):
                    self._reset_monster_to_spawn(actor_id, server_tick)
                    occupied.append(home)
                    movements.append(
                        MonsterMovement(
                            actor_id,
                            home,
                            self.encounter.coordinate_frame.world_to_wire(home),
                            0.0,
                            False,
                            int(server_tick) & 0xFFFFFFFF,
                        )
                    )
                    continue

            # Encounter construction rejects all authored/procedural homes in
            # travel-stone safe zones. Keep this defensive guard for cached
            # encounters and developer fixtures: returning to an invalid home
            # otherwise alternates add/remove MonsterReturnHome every tick.
            if self._near_protected_waypoint(home):
                self.monster_routes.pop(actor_id, None)
                if returning_home:
                    self._set_returning_home(actor_id, False, server_tick)
                self.monster_moving[actor_id] = False
                occupied.append(start)
                if was_moving and self._monster_movement_publish_due(
                    actor_id,
                    server_tick,
                    moving=False,
                    was_moving=True,
                    position=start,
                ):
                    movements.append(MonsterMovement(
                        actor_id,
                        start,
                        self.encounter.coordinate_frame.world_to_wire(start),
                        previous_yaw,
                        False,
                        int(server_tick) & 0xFFFFFFFF,
                    ))
                continue

            # Hard leash, evaluated before the crowd-control and safe-zone
            # branches below. Those all end in ``continue``, so leaving the
            # reset behind them would let a stunned or rooted actor be parked
            # outside its territory indefinitely. The home guard above still
            # runs first: an actor whose spawn is invalid must not reset onto
            # it.
            if home_distance > reset_radius:
                self._reset_monster_to_spawn(actor_id, server_tick)
                occupied.append(home)
                movements.append(
                    MonsterMovement(
                        actor_id,
                        home,
                        self.encounter.coordinate_frame.world_to_wire(home),
                        0.0,
                        False,
                        int(server_tick) & 0xFFFFFFFF,
                    )
                )
                continue

            if (
                not returning_home
                and self._near_protected_waypoint(start)
            ):
                self._set_returning_home(
                    actor_id, True, server_tick, now=current_time,
                    reason="it entered a travel stone's safe zone",
                )
                returning_home = True

            # Travel stones are hard safe-zone obstacles, not merely points
            # that make an actor return after it has already entered.  A
            # player standing inside one must not attract every nearby actor
            # to the boundary where repeated A* corrections make them circle.
            if (
                not returning_home
                and self._near_protected_waypoint(player_position)
            ):
                self.monster_routes.pop(actor_id, None)
                if home_distance > MONSTER_HOME_ARRIVAL_DISTANCE:
                    self._set_returning_home(
                        actor_id, True, server_tick, now=current_time,
                        reason="the player is in a travel stone's safe zone",
                    )
                    returning_home = True
                else:
                    self.monster_moving[actor_id] = False
                    occupied.append(start)
                    if was_moving:
                        movements.append(MonsterMovement(
                            actor_id,
                            start,
                            self.encounter.coordinate_frame.world_to_wire(start),
                            previous_yaw,
                            False,
                            int(server_tick) & 0xFFFFFFFF,
                        ))
                    continue

            if self._monster_actions_disabled(actor_id, now=current_time):
                self.monster_routes.pop(actor_id, None)
                self.monster_moving[actor_id] = False
                occupied.append(start)
                if was_moving:
                    movements.append(
                        MonsterMovement(
                            actor_id,
                            start,
                            self.encounter.coordinate_frame.world_to_wire(
                                start
                            ),
                            previous_yaw,
                            False,
                            int(server_tick) & 0xFFFFFFFF,
                        )
                    )
                continue

            # Soft leash: give up because the target is gone, not because of
            # an invisible line. Distance from home used to end the chase on
            # its own, which -- together with the clamped goal below -- is what
            # collected packs along the territory boundary: they ran out, were
            # turned around, re-aggroed from home, and ran out again. Only
            # being outrun, or the player leaving the reset envelope entirely,
            # sends an actor home now. Distance from home is handled by the
            # hard reset above.
            if (
                not returning_home
                and home_distance > MONSTER_HOME_ARRIVAL_DISTANCE
                and (
                    direct_distance > monster.escape_range
                    or player_home_distance > reset_radius
                )
            ):
                self._set_returning_home(
                    actor_id, True, server_tick, now=current_time,
                    reason=(
                        f"outrun ({direct_distance:.1f} m > escape range "
                        f"{monster.escape_range:.1f})"
                        if direct_distance > monster.escape_range
                        else (
                            f"player {player_home_distance:.1f} m from its home "
                            f"> reset radius {reset_radius:.1f}"
                        )
                    ),
                )
                returning_home = True

            # Only an actor that is already home may idle here. Anything that
            # has left its spawn walks or resets back above rather than
            # freezing wherever the player happened to outrange it.
            if (
                not returning_home
                and actor_id not in self.scripted_monster_targets
                and home_distance <= MONSTER_HOME_ARRIVAL_DISTANCE
                and (
                    player_home_distance > MONSTER_SIMULATION_RADIUS
                    or direct_distance > MONSTER_SIMULATION_RADIUS
                )
            ):
                self.monster_routes.pop(actor_id, None)
                self.monster_moving[actor_id] = False
                continue

            if (
                returning_home
                and home_distance <= MONSTER_HOME_ARRIVAL_DISTANCE
            ):
                destination = home
                self.monster_positions[actor_id] = destination
                self.monster_health[actor_id] = monster.maximum_health
                self.monster_moving[actor_id] = False
                self._set_returning_home(actor_id, False, server_tick)
                occupied.append(destination)
                if was_moving or self._distance(start, destination) > 1e-5:
                    movements.append(
                        MonsterMovement(
                            actor_id,
                            destination,
                            self.encounter.coordinate_frame.world_to_wire(
                                destination
                            ),
                            previous_yaw,
                            False,
                            int(server_tick) & 0xFFFFFFFF,
                        )
                    )
                continue

            is_boss = str(monster.rank_id or "").casefold().startswith("rank_boss")
            max_acq = (
                max(monster.aggro_range, monster.travel_distance)
                if is_boss
                else MONSTER_ACQUISITION_RADIUS
            )
            engaged = (
                returning_home
                or actor_id in self.scripted_monster_targets
                or home_distance > MONSTER_HOME_ARRIVAL_DISTANCE
                or direct_distance
                <= min(
                    monster.aggro_range,
                    monster.travel_distance,
                    max_acq,
                )
            )
            if not engaged:
                self.monster_routes.pop(actor_id, None)
                if was_moving:
                    self.monster_moving[actor_id] = False
                    movements.append(
                        MonsterMovement(
                            actor_id,
                            start,
                            self.encounter.coordinate_frame.world_to_wire(
                                start
                            ),
                            previous_yaw,
                            False,
                            int(server_tick) & 0xFFFFFFFF,
                        )
                    )
                occupied.append(start)
                continue

            movement_elapsed = elapsed
            motion_unblock_at = self.monster_motion_unblock_at.get(actor_id)
            if motion_unblock_at is not None:
                if current_time < motion_unblock_at:
                    # No chase, separation or facing correction while the client
                    # is holding this actor in its cast animation.
                    occupied.append(start)
                    continue
                # A tick can straddle the unlock. Only spend the part after it;
                # a delayed worker must not integrate the time spent casting.
                movement_elapsed = min(elapsed, max(0.0, current_time - motion_unblock_at))
                self.monster_motion_unblock_at.pop(actor_id, None)

            if returning_home:
                goal = home
                travel = min(monster.run_speed * movement_elapsed, home_distance)
            else:
                # Pursue the player directly. This used to be clamped to the
                # pursuit sphere, which made the goal a point on an invisible
                # boundary: every monster whose player stood outside its
                # territory walked to that surface and stopped, so packs
                # collected along it doing nothing. The reset radius above is
                # what bounds pursuit now.
                goal = self._clamp_to_territory(
                    player_position,
                    home,
                    reset_radius,
                )
                goal_distance = self._distance(start, goal)
                travel = min(
                    monster.run_speed * movement_elapsed,
                    max(
                        0.0,
                        goal_distance
                        - preferred_stop_distance,
                    ),
                )
            if travel <= 1e-6:
                destination = start
                separated = False
                if not returning_home:
                    candidate = self._separate_destination(
                        actor_id, start, player_position, occupied
                    )
                    candidate = self._clamp_to_territory(
                        candidate,
                        home,
                        reset_radius,
                    )
                    correction_x = candidate[0] - start[0]
                    correction_z = candidate[2] - start[2]
                    correction = math.hypot(correction_x, correction_z)
                    maximum_correction = monster.run_speed * movement_elapsed
                    if correction > maximum_correction > 0.0:
                        ratio = maximum_correction / correction
                        candidate = (
                            start[0] + correction_x * ratio,
                            candidate[1],
                            start[2] + correction_z * ratio,
                        )
                        correction = maximum_correction
                    if correction > 1e-5:
                        candidate = self.ground_monster_position(
                            actor_id,
                            candidate,
                            reference_position=start,
                            max_horizontal_snap=max(
                                0.25, maximum_correction + 0.10
                            ),
                        )
                        grounded_correction = math.hypot(
                            candidate[0] - start[0],
                            candidate[2] - start[2],
                        )
                        if (
                            grounded_correction > 1e-5
                            and grounded_correction
                            <= maximum_correction + 1e-6
                            and not self._segment_intersects_protected_waypoint(
                                start, candidate
                            )
                        ):
                            destination = candidate
                            self.monster_positions[actor_id] = destination
                            separated = True
                            self.monster_yaws[actor_id] = math.atan2(
                                -(destination[0] - start[0]),
                                -(destination[2] - start[2]),
                            )
                if (
                    not separated
                    and not returning_home
                    and direct_distance <= monster.attack.attack_range
                ):
                    dx = player_position[0] - destination[0]
                    dz = player_position[2] - destination[2]
                    if math.hypot(dx, dz) > 1e-6:
                        self.monster_yaws[actor_id] = math.atan2(-dx, -dz)
                occupied.append(destination)
                yaw_changed = abs(
                    math.atan2(
                        math.sin(self.monster_yaws[actor_id] - previous_yaw),
                        math.cos(self.monster_yaws[actor_id] - previous_yaw),
                    )
                ) > math.radians(1.0)
                self.monster_moving[actor_id] = separated
                # Send exactly one stop/turn correction, not one stationary
                # MoveCommand for every actor on every 25 Hz tick.
                if (
                    (separated or was_moving or yaw_changed)
                    and self._monster_movement_publish_due(
                        actor_id,
                        server_tick,
                        moving=separated,
                        was_moving=was_moving,
                        position=destination,
                    )
                ):
                    movements.append(
                        MonsterMovement(
                            actor_id,
                            destination,
                            self.encounter.coordinate_frame.world_to_wire(
                                destination
                            ),
                            self.monster_yaws[actor_id],
                            separated,
                            int(server_tick) & 0xFFFFFFFF,
                        )
                    )
                continue
            try:
                destination = self._advance_on_cached_route(
                    actor_id,
                    start,
                    goal,
                    travel,
                    server_tick,
                    home=home,
                    territory_radius=reset_radius,
                )
            except NavigationError:
                destination = start
            if not returning_home:
                destination = self._separate_destination(
                    actor_id, destination, player_position, occupied
                )
                destination = self._clamp_to_territory(
                    destination,
                    home,
                    reset_radius,
                )
                if self._segment_intersects_protected_waypoint(
                    start, destination
                ):
                    if home_distance > MONSTER_HOME_ARRIVAL_DISTANCE:
                        self._set_returning_home(
                            actor_id, True, server_tick, now=current_time,
                            reason="its path crosses a travel stone's safe zone",
                        )
                    destination = start
            maximum_step = monster.run_speed * movement_elapsed
            destination = self.ground_monster_position(
                actor_id,
                destination,
                reference_position=start,
                max_horizontal_snap=max(0.25, maximum_step + 0.10),
            )
            dx = destination[0] - start[0]
            dz = destination[2] - start[2]
            displacement = math.hypot(dx, dz)
            # Collision separation is a steering correction.  It must never
            # move an actor farther than its database run speed permits. Use
            # the complete movement budget, not only the remaining forward
            # route distance: near melee standoff the latter approaches zero
            # and previously prevented overlapping capsules from ever
            # separating again.
            if displacement > maximum_step and displacement > 1e-6:
                ratio = maximum_step / displacement
                destination = (
                    start[0] + dx * ratio,
                    destination[1],
                    start[2] + dz * ratio,
                )
                destination = self.ground_monster_position(
                    actor_id,
                    destination,
                    reference_position=start,
                    max_horizontal_snap=0.10,
                )
                dx = destination[0] - start[0]
                dz = destination[2] - start[2]
                displacement = math.hypot(dx, dz)
                if displacement > maximum_step + 1e-6:
                    destination = start
                    dx = 0.0
                    dz = 0.0
                    displacement = 0.0
            moved = displacement > 1e-5
            yaw = math.atan2(-dx, -dz) if moved else 0.0
            if moved:
                self.monster_positions[actor_id] = destination
                self.monster_yaws[actor_id] = yaw
            reached_home = (
                returning_home
                and self._distance(destination, home)
                <= MONSTER_HOME_ARRIVAL_DISTANCE
            )
            if reached_home and self._distance(start, home) <= travel + 1e-6:
                destination = home
                self.monster_positions[actor_id] = home
                self.monster_health[actor_id] = monster.maximum_health
                self._set_returning_home(actor_id, False, server_tick)
                moved = False
            self.monster_moving[actor_id] = moved
            occupied.append(destination)
            if (
                (moved or was_moving or reached_home)
                and self._monster_movement_publish_due(
                    actor_id,
                    server_tick,
                    moving=moved,
                    was_moving=was_moving,
                    position=destination,
                    force=reached_home,
                )
            ):
                movements.append(
                    MonsterMovement(
                        actor_id,
                        destination,
                        self.encounter.coordinate_frame.world_to_wire(destination),
                        yaw if moved else previous_yaw,
                        moved,
                        int(server_tick) & 0xFFFFFFFF,
                    )
                )
        for movement in movements:
            self.monster_last_movement_publish_ticks[
                int(movement.actor_id)
            ] = int(movement.server_tick) & 0xFFFFFFFF
            self.monster_last_published_positions[
                int(movement.actor_id)
            ] = movement.world_position
        return tuple(movements)

    def start_bullet_skill(
        self,
        skill: BulletSkill,
        player_position: tuple[float, float, float],
        *,
        now: float | None = None,
        attack_speed_multiplier: float = 1.0,
    ) -> PendingPlayerHit | None:
        player_position = self.encounter.coordinate_frame.wire_to_world(
            player_position
        )
        geometry = self._load_player_skill_geometry(skill.skill_index)
        if geometry is None:
            return None
        monster = self._skill_target(
            player_position, skill.direction, geometry
        )
        if monster is None:
            return None
        target_position = self.monster_positions[monster.actor_id]
        distance = self._distance(player_position, target_position)
        # Attack speed changes the animation/emit phase, but it does not
        # change the projectile template's world-space Velocity. Treating the
        # complete hit time as attack-speed-scaled makes the same fireball
        # travel faster whenever gear changes. The collision is reached at
        # the surfaces of the projectile and monster capsules, not at the
        # monster's center.
        attack_speed_multiplier = max(
            0.1, min(10.0, float(attack_speed_multiplier))
        )
        emit_delay = projectile_emit_delay(geometry, attack_speed_multiplier)
        travel_distance = max(
            0.0,
            distance
            - max(0.0, monster.collision_radius)
            - max(0.0, geometry.projectile_radius),
        )
        delay_seconds = emit_delay
        if geometry.projectile_velocity > 1e-6:
            delay_seconds += (
                travel_distance / geometry.projectile_velocity
            )
        else:
            delay_seconds += (
                geometry.hit_frame
                / SERVER_TICKS_PER_SECOND
                / attack_speed_multiplier
            )
        current_time = time.perf_counter() if now is None else float(now)
        pending = PendingPlayerHit(
            cast_id=int(skill.cast_id),
            target_actor_id=monster.actor_id,
            skill_index=skill.skill_index,
            due_at=current_time + delay_seconds,
            impact_tick=_player_impact_tick(
                skill.server_tick, delay_seconds
            ),
            distance=distance,
            travel_distance=travel_distance,
            emit_delay=emit_delay,
            victim_status_effects=geometry.victim_status_effects,
            damage_modifier=max(1e-6, geometry.damage_modifier),
            damage_type=geometry.damage_type,
        )
        self.pending_player_hits[pending.cast_id] = pending
        return pending

    def plan_entity_skill_hits(
        self, *, skill_index, player_position, projectile, direction=(0.0, 0.0, 0.0, 0.0),
        aim_angle=0.0, target_position=None, explicit_target_actor_ids=(),
        modifier_sources=(), eligible_actor_ids=None, visible_actor_positions=None,
        additional_entities=(), attack_speed_multiplier=1.0, now=None,
        geometry=None, area_radius_override=None, impact_delay_override=None,
        require_explicit_target=False,
    ):
        """Adapt map actors to the common world-space skill planner.

        Additional entities are already filtered for hostility by the session
        adapter. No actor is inserted into the monster or player health stores.
        """
        geometry = geometry or self._load_player_skill_geometry(skill_index)
        if geometry is None:
            return None
        if modifier_sources:
            geometry = self._modified_player_skill_geometry(geometry, modifier_sources)
        origin = self.encounter.coordinate_frame.wire_to_world(player_position)
        point = (self.encounter.coordinate_frame.wire_to_world(target_position)
                 if target_position is not None else None)
        if not projectile:
            direction = (-math.sin(aim_angle), 0.0, -math.cos(aim_angle), 0.0)
        visible = visible_actor_positions or {}
        eligible = (None if eligible_actor_ids is None else
                    set(eligible_actor_ids).union(visible, explicit_target_actor_ids))
        length = math.hypot(direction[0], direction[2])
        reach = max(geometry.attack_range, geometry.projectile_range)
        entities = []
        for actor_id in self.living_actor_ids:
            if actor_id in self.returning_home_actor_ids or (eligible is not None and actor_id not in eligible):
                continue
            monster = self.monsters[actor_id]
            position = self.monster_positions[actor_id]
            contact = None
            if projectile and length > 1e-6 and reach > 0:
                contact = self._viewer_monster_ray_proximity(
                    origin, direction[0] / length, direction[2] / length,
                    reach, monster, position, monster.collision_radius + max(
                        0.25, geometry.projectile_radius, geometry.player_width / 2),
                    visible.get(actor_id),
                )
            impact_count = geometry.impact_count
            if self.monster_status_effects.get(actor_id):
                impact_count = self._modified_player_skill_geometry(
                    geometry, self._active_monster_modifier_sources(actor_id, now=now),
                ).impact_count
            entities.append(CombatEntity(actor_id, position, monster.collision_radius,
                                         visible.get(actor_id), contact, impact_count, monster.target_radius))
        entities.extend(additional_entities)
        delay = (max(0.0, float(impact_delay_override)) if impact_delay_override is not None
                 else self._player_skill_impact_delay(geometry, attack_speed_multiplier))
        return plan_entity_skill(
            geometry, entities, origin=origin, direction=direction, projectile=projectile,
            point=point, explicit_targets=explicit_target_actor_ids,
            attack_speed=attack_speed_multiplier, impact_delay=delay,
            area_radius=max(0.0, area_radius_override or 0.0),
            require_explicit_target=require_explicit_target,
            range_tolerance=PLAYER_PROJECTILE_COLLISION_RADIUS,
            static_reach=(self._bullet_static_reach(geometry.start_offset)
                          if projectile and geometry.static_collision else None),
        )

    def _monster_shot_blocked(self, definition, start, target) -> bool:
        """Whether a monster bullet from ``start`` would stop at a wall
        before ``target`` (feet positions)."""
        if (
            definition is None
            or definition.command_id not in {
                BULLET_SKILL_COMMAND, TARGET_BULLET_SKILL_COMMAND,
            }
            or not getattr(definition, "static_collision", False)
            or str(definition.motion_type).casefold() in ORBITING_MOTION_TYPES
        ):
            return False
        reach = self._bullet_static_reach(definition.start_offset)
        if reach is None:
            return False
        return reach(start, target) + PLAYER_PROJECTILE_COLLISION_RADIUS < math.dist(
            tuple(start[:3]), tuple(target[:3])
        )

    def _map_static_line_of_sight(self) -> StaticLineOfSight | None:
        """The map's wall collision, built in the background at map load."""
        if not self._static_line_of_sight_loaded:
            thread = getattr(self, "_static_line_of_sight_thread", None)
            if thread is not None and thread is not threading.current_thread():
                thread.join()
            if not self._static_line_of_sight_loaded:
                self._load_static_line_of_sight()
        return self._static_line_of_sight

    def _load_static_line_of_sight(self) -> None:
        try:
            map_root = Path(self.encounter.static_database_path).parent.parent / "maps"
            map_id = str(self.encounter.map_id)
            self._static_line_of_sight = load_static_line_of_sight(
                map_root / f"{map_id}.map", map_root / f"{map_id}.col",
            )
        except (AttributeError, TypeError, OSError, CollisionTerrainError,
                UnicodeDecodeError, struct.error):
            # Development maps without a collision bundle keep open sight.
            self._static_line_of_sight = None
        self._static_line_of_sight_loaded = True

    def _bullet_static_reach(self, start_offset):
        """``static_reach`` for a bullet flying at its StartOffset height."""
        line_of_sight = self._map_static_line_of_sight()
        if line_of_sight is None:
            return None
        height = _bullet_flight_height(start_offset)

        def reach(start, end):
            return line_of_sight.clear_distance(
                (start[0], start[1] + height, start[2]),
                (end[0], end[1] + height, end[2]),
            )
        return reach

    def start_bullet_skill_hits(
        self,
        skill: BulletSkill,
        player_position: tuple[float, float, float],
        *,
        source_actor_id: int,
        now: float | None = None,
        attack_speed_multiplier: float = 1.0,
        explicit_target_actor_ids: tuple[int, ...] = (),
        modifier_effect_ids: tuple[int, ...] = (),
        eligible_actor_ids: tuple[int, ...] | None = None,
        visible_actor_positions: (
            dict[int, tuple[float, float, float]] | None
        ) = None,
        target_position: tuple[float, float, float] | None = None,
        hit_plan=None,
        player_damage_range: tuple[float, float] | None = None,
        player_critical_hit_chance: float = 0.0,
        player_critical_damage_multiplier: float = 2.0,
    ) -> tuple[PendingPlayerHit, ...]:
        """Schedule all authoritative impacts produced by one client cast.

        Skill ``BulletCount`` launches parallel projectiles. Bullet
        ``ImpactCount`` permits a projectile to collide with that many
        distinct actors along its trajectory, while ``ReproductionCount``
        follows the nearest targets inside ``TargetSearchRadius`` (the
        chain-lightning shape). These are separate database concepts and must
        not be collapsed into a single destination timer.
        """

        player_world = self.encounter.coordinate_frame.wire_to_world(
            player_position
        )
        geometry = hit_plan.geometry if hit_plan is not None else self._load_player_skill_geometry(skill.skill_index)
        if geometry is None:
            return ()
        if hit_plan is None:
            geometry = self._modified_player_skill_geometry(
                geometry, modifier_effect_ids
            )
        ammunition_types = self._active_ammunition_types(
            modifier_effect_ids
        )
        plan = hit_plan if hit_plan is not None else self.plan_entity_skill_hits(
            skill_index=skill.skill_index, player_position=player_position,
            projectile=True, direction=skill.direction, target_position=target_position,
            explicit_target_actor_ids=explicit_target_actor_ids,
            eligible_actor_ids=eligible_actor_ids, visible_actor_positions=visible_actor_positions,
            attack_speed_multiplier=attack_speed_multiplier, now=now, geometry=geometry,
        )
        if plan is None:
            return ()
        geometry = plan.geometry
        current_time = time.perf_counter() if now is None else float(now)
        emit_delay = plan.emit_delay
        effective_projectile_velocity = plan.projectile_velocity
        forward_offset = plan.forward_offset
        scheduled = [impact.scheduling_tuple() for impact in plan.impacts
                     if impact.target.actor_id in self.monsters]

        hit_count_by_target: dict[int, int] = {}
        for (
            target,
            _projectile_index,
            _distance,
            _travel_distance,
            _delay,
            _validate_trajectory,
        ) in scheduled:
            hit_count_by_target[target.actor_id] = (
                hit_count_by_target.get(target.actor_id, 0) + 1
            )

        pending_hits = []
        source_actor_id = int(source_actor_id)
        for impact_index, (
            target,
            projectile_index,
            distance,
            travel_distance,
            delay_seconds,
            validate_trajectory,
        ) in enumerate(scheduled):
            damage_immune = not self._ammunition_can_hit(
                target.actor_id, ammunition_types
            )
            target_geometry = self._modified_player_skill_geometry(
                geometry,
                self._active_monster_modifier_sources(
                    target.actor_id,
                    now=current_time,
                ),
            )
            impact_effects = impact_status_effects(target_geometry, projectile=True)
            pending = PendingPlayerHit(
                cast_id=int(skill.cast_id),
                target_actor_id=target.actor_id,
                skill_index=skill.skill_index,
                due_at=current_time + delay_seconds,
                impact_tick=_player_impact_tick(
                    skill.server_tick, delay_seconds
                ),
                distance=distance,
                travel_distance=travel_distance,
                emit_delay=emit_delay,
                victim_status_effects=(
                    ()
                    if damage_immune
                    else impact_effects
                ),
                source_actor_id=source_actor_id,
                projectile_index=projectile_index,
                impact_index=impact_index,
                target_hit_count=hit_count_by_target[target.actor_id],
                user_status_effects=geometry.user_status_effects,
                origin=(
                    player_world[0]
                    + (
                        float(skill.direction[0])
                        / max(
                            1e-6,
                            math.hypot(
                                skill.direction[0], skill.direction[2]
                            ),
                        )
                    ) * forward_offset,
                    player_world[1] + float(geometry.start_offset[1]),
                    player_world[2]
                    + (
                        float(skill.direction[2])
                        / max(
                            1e-6,
                            math.hypot(
                                skill.direction[0], skill.direction[2]
                            ),
                        )
                    ) * forward_offset,
                ),
                direction=(
                    float(skill.direction[0])
                    / max(
                        1e-6,
                        math.hypot(
                            skill.direction[0], skill.direction[2]
                        ),
                    ),
                    float(skill.direction[2])
                    / max(
                        1e-6,
                        math.hypot(
                            skill.direction[0], skill.direction[2]
                        ),
                    ),
                ),
                emitted_at=current_time + emit_delay,
                spawn_sweep_distance=forward_offset,
                projectile_velocity=effective_projectile_velocity,
                projectile_radius=geometry.projectile_radius,
                maximum_range=(
                    max(
                        geometry.attack_range,
                        geometry.projectile_range,
                    )
                ),
                motion_type=(
                    geometry.motion_type
                    if validate_trajectory
                    else "Area"
                ),
                damage_modifier=max(0.0, target_geometry.damage_modifier),
                damage_type=target_geometry.damage_type,
                player_minimum_damage=(
                    max(0.0, float(player_damage_range[0]))
                    if player_damage_range is not None
                    else 0.0
                ),
                player_maximum_damage=(
                    max(0.0, float(player_damage_range[1]))
                    if player_damage_range is not None
                    else 0.0
                ),
                critical_hit_chance=max(
                    0.0, min(1.0, float(player_critical_hit_chance))
                ),
                critical_damage_multiplier=max(
                    1.0, float(player_critical_damage_multiplier)
                ),
                damage_immune=damage_immune,
            )
            key = (
                source_actor_id,
                pending.cast_id,
                pending.impact_index,
            )
            self.pending_runtime_player_hits[key] = pending
            pending_hits.append(pending)
        return tuple(pending_hits)

    def start_spatial_skill_hits(
        self,
        *,
        skill_index: int,
        cast_id: int,
        server_tick: int,
        player_position: tuple[float, float, float],
        source_actor_id: int,
        target_position: tuple[float, float, float] | None = None,
        aim_angle: float = 0.0,
        now: float | None = None,
        attack_speed_multiplier: float = 1.0,
        modifier_effect_ids: tuple[int, ...] = (),
        eligible_actor_ids: tuple[int, ...] | None = None,
        visible_actor_positions: (
            dict[int, tuple[float, float, float]] | None
        ) = None,
        explicit_target_actor_ids: tuple[int, ...] = (),
        player_damage_range: tuple[float, float] | None = None,
        player_critical_hit_chance: float = 0.0,
        player_critical_damage_multiplier: float = 2.0,
        hit_plan=None,
        area_radius_override: float | None = None,
        impact_delay_override: float | None = None,
        additional_victim_effects: tuple[
            VictimStatusEffectDefinition, ...
        ] = (),
    ) -> tuple[PendingPlayerHit, ...]:
        """Schedule actor, user, directional, or ground-area impacts."""

        geometry = hit_plan.geometry if hit_plan is not None else self._load_player_skill_geometry(skill_index)
        if geometry is None:
            return ()
        if hit_plan is None:
            geometry = self._modified_player_skill_geometry(
                geometry, modifier_effect_ids
            )
        ammunition_types = self._active_ammunition_types(
            modifier_effect_ids
        )
        player_world = self.encounter.coordinate_frame.wire_to_world(
            player_position
        )
        target_world = (
            self.encounter.coordinate_frame.wire_to_world(target_position)
            if target_position is not None
            else player_world
        )
        plan = hit_plan if hit_plan is not None else self.plan_entity_skill_hits(
            skill_index=skill_index, player_position=player_position, projectile=False,
            aim_angle=aim_angle, target_position=target_position,
            explicit_target_actor_ids=explicit_target_actor_ids,
            eligible_actor_ids=eligible_actor_ids, visible_actor_positions=visible_actor_positions,
            attack_speed_multiplier=attack_speed_multiplier, now=now, geometry=geometry,
            area_radius_override=area_radius_override, impact_delay_override=impact_delay_override,
        )
        if plan is None:
            return ()
        geometry = plan.geometry
        candidates = [(impact.distance, impact.target.actor_id, impact.target)
                      for impact in plan.impacts if impact.target.actor_id in self.monsters]
        delay = plan.impacts[0].delay if plan.impacts else 0.0
        current_time = time.perf_counter() if now is None else float(now)

        pending_hits = []
        for impact_index, (distance, _actor_id, monster) in enumerate(
            candidates
        ):
            damage_immune = not self._ammunition_can_hit(
                monster.actor_id, ammunition_types
            )
            target_geometry = self._modified_player_skill_geometry(
                geometry,
                self._active_monster_modifier_sources(
                    monster.actor_id,
                    now=current_time,
                ),
            )
            receiver_effects = impact_status_effects(
                target_geometry, projectile=False, additional=additional_victim_effects)
            pending = PendingPlayerHit(
                cast_id=int(cast_id),
                target_actor_id=monster.actor_id,
                skill_index=int(skill_index),
                due_at=current_time + delay,
                impact_tick=_player_impact_tick(server_tick, delay),
                distance=distance,
                travel_distance=0.0,
                emit_delay=0.0,
                victim_status_effects=(
                    ()
                    if damage_immune
                    else receiver_effects
                ),
                source_actor_id=int(source_actor_id),
                projectile_index=0,
                impact_index=impact_index,
                target_hit_count=1,
                user_status_effects=geometry.user_status_effects,
                origin=target_world,
                emitted_at=current_time,
                maximum_range=geometry.attack_range,
                motion_type="Area",
                # The same reach the victims were chosen with (combat_entities
                # plan_entity_skill's splash): a location or death effect's
                # aura wider than ImpactRadius picked them, so it spares only
                # a monster that left that wider ring.
                area_radius=max(
                    0.25, geometry.hit_range, geometry.impact_radius,
                    float(area_radius_override or 0.0),
                    max((
                        effect.aura_radius for effect in (
                            *geometry.location_status_effects,
                            *geometry.location_visual_effects,
                            *geometry.bullet_death_effects,
                        ) if effect.chance >= 1.0
                    ), default=0.0),
                ),
                damage_modifier=target_geometry.damage_modifier,
                damage_type=target_geometry.damage_type,
                player_minimum_damage=(
                    max(0.0, float(player_damage_range[0]))
                    if player_damage_range is not None
                    else 0.0
                ),
                player_maximum_damage=(
                    max(0.0, float(player_damage_range[1]))
                    if player_damage_range is not None
                    else 0.0
                ),
                critical_hit_chance=max(
                    0.0, min(1.0, float(player_critical_hit_chance))
                ),
                critical_damage_multiplier=max(
                    1.0, float(player_critical_damage_multiplier)
                ),
                damage_immune=damage_immune,
            )
            key = (
                pending.source_actor_id,
                pending.cast_id,
                pending.impact_index,
            )
            self.pending_runtime_player_hits[key] = pending
            pending_hits.append(pending)
        return tuple(pending_hits)

    def runtime_player_hit_remaining(
        self,
        source_actor_id: int,
        cast_id: int,
        impact_index: int,
        *,
        now: float | None = None,
        visible_actor_positions: (
            dict[int, tuple[float, float, float]] | None
        ) = None,
    ) -> float | None:
        """Return time to collision, cancelling a fixed projectile that misses."""

        key = (int(source_actor_id), int(cast_id), int(impact_index))
        pending = self.pending_runtime_player_hits.get(key)
        if pending is None:
            return None
        current_time = time.perf_counter() if now is None else float(now)
        if (
            pending.motion_type != "Straight"
            or pending.projectile_velocity <= 1e-6
        ):
            return max(0.0, pending.due_at - current_time)
        if current_time + 1e-6 < pending.due_at:
            return pending.due_at - current_time
        if (
            pending.target_actor_id not in self.living_actor_ids
            or pending.target_actor_id not in self.monsters
            or pending.target_actor_id not in self.monster_positions
        ):
            del self.pending_runtime_player_hits[key]
            return None
        target = self.monsters[pending.target_actor_id]
        current = self.monster_positions[pending.target_actor_id]
        visible_position = (
            visible_actor_positions.get(pending.target_actor_id)
            if visible_actor_positions is not None
            else None
        )
        radius = max(
            PLAYER_PROJECTILE_COLLISION_RADIUS,
            target.collision_radius + pending.projectile_radius,
        ) + PLAYER_PROJECTILE_HITBOX_TOLERANCE
        # Bullet StartOffset is a visual muzzle position in front of the
        # caster. A target between the player capsule and that muzzle has
        # already been crossed at emission; validating only the forward ray
        # from ``origin`` incorrectly treats that close target as being
        # behind the projectile. Test the authored player-to-muzzle sweep
        # before applying the ordinary travelling-projectile checks.
        if pending.spawn_sweep_distance > 1e-6:
            sweep_start = (
                pending.origin[0]
                - pending.direction[0] * pending.spawn_sweep_distance,
                pending.origin[1],
                pending.origin[2]
                - pending.direction[1] * pending.spawn_sweep_distance,
            )
            _forward, sweep_miss = self._viewer_monster_ray_proximity(
                sweep_start,
                pending.direction[0],
                pending.direction[1],
                pending.spawn_sweep_distance,
                target,
                current,
                radius,
                visible_position,
            )
            if sweep_miss <= radius:
                return 0.0

        _forward, miss_distance = self._viewer_monster_ray_proximity(
            pending.origin,
            pending.direction[0],
            pending.direction[1],
            max(0.0, pending.maximum_range),
            target,
            current,
            radius,
            visible_position,
        )
        if miss_distance > radius:
            del self.pending_runtime_player_hits[key]
            return None
        return 0.0

    def player_skill_user_effects(
        self,
        skill_index: int,
        *,
        cast_id: int,
        trigger: str,
        killed: bool = False,
        modifier_effect_ids: tuple[int, ...] = (),
    ) -> tuple[VictimStatusEffectDefinition, ...]:
        geometry = self._load_player_skill_geometry(skill_index)
        if geometry is None:
            return ()
        geometry = self._modified_player_skill_geometry(
            geometry, modifier_effect_ids
        )
        trigger = str(trigger).strip().lower()
        return tuple(
            definition
            for definition in geometry.user_status_effects
            if (
                definition.trigger == trigger
                or (
                    trigger == "hit"
                    and definition.trigger in {"", "hit"}
                )
                or (
                    trigger == "kill"
                    and definition.trigger in {"", "hit", "kill"}
                )
            )
            and self._effect_applies(
                definition, cast_id=cast_id, killed=killed
            )
        )

    def player_skill_location_owner_effects(
        self,
        skill_index: int,
        *,
        cast_id: int,
        target_count: int,
        modifier_effect_ids: tuple[int, ...] = (),
    ) -> tuple[VictimStatusEffectDefinition, ...]:
        """Return database-authored buffs applied by a location to its caster."""

        geometry = self._load_player_skill_geometry(skill_index)
        if geometry is None:
            return ()
        geometry = self._modified_player_skill_geometry(
            geometry, modifier_effect_ids
        )
        result = []
        for definition in geometry.location_owner_status_effects:
            # Held only inside its ground aura: apply_player_location_auras.
            if definition.creator_aura:
                continue
            if not self._effect_applies(
                definition, cast_id=cast_id, killed=False
            ):
                continue
            # Enemy-count auras such as Banner of War's battle frenzy use a
            # stackable causer-hosted child. Creator-only utility effects have
            # MaxStackSize=1 and are applied exactly once.
            repetitions = (
                min(max(0, int(target_count)), definition.max_stack_size)
                if definition.max_stack_size > 1
                else 1
            )
            result.extend(definition for _ in range(repetitions))
        return tuple(result)

    def player_skill_visual_modifier_effects(
        self,
        skill_index: int,
        modifier_sources: tuple[object, ...],
    ) -> tuple[object, ...]:
        """Return actor effects that select a non-default skill sequence.

        A remote player does not own the caster's talent model.  The native
        client therefore needs the small subset of active actor effects whose
        modifier program replaces ``SkillSequence:execute`` before it decodes
        the cast.  Sending every persistent talent effect caused invalid talent
        warnings and oversized snapshots; filtering here preserves only the
        presentation dependency for this exact skill.
        """

        geometry = self._load_player_skill_geometry(skill_index)
        if geometry is None or not modifier_sources:
            return ()
        result = []
        path = self.encounter.static_database_path
        with sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True
        ) as database:
            for source in modifier_sources:
                if not hasattr(source, "effect_id"):
                    continue
                effect_id = int(source.effect_id)
                program = self._status_effect_modifier_programs.get(effect_id)
                if program is None:
                    row = database.execute(
                        "SELECT StartModifiers FROM _Template_StatusEffect "
                        "WHERE rowid=? LIMIT 1",
                        (effect_id + 1,),
                    ).fetchone()
                    parsed = []
                    for raw in str(row[0] if row else "").split(";"):
                        kind, separator, arguments = raw.partition(":")
                        if not separator:
                            continue
                        parsed.append(
                            (
                                kind.strip(),
                                tuple(
                                    value.strip()
                                    for value in arguments.split(",")
                                ),
                            )
                        )
                    program = tuple(parsed)
                    self._status_effect_modifier_programs[effect_id] = program
                if any(
                    kind.casefold() == "skillsequence"
                    and len(arguments) >= 3
                    and arguments[0].casefold() == "execute"
                    and self._skill_scope_matches(
                        geometry, (arguments[1],)
                    )
                    for kind, arguments in program
                ):
                    result.append(source)
        return tuple(result)

    def _status_effect_runtime_program(
        self, effect_id: int
    ) -> tuple[float, str, str]:
        return self._status_effect_runtime_programs.get(
            int(effect_id), (1.0, "", "")
        )

    def _status_damage_programs(
        self,
        definition: VictimStatusEffectDefinition,
        *,
        ticks: bool,
    ) -> tuple[tuple[float, str, bool], ...]:
        """Decode generic CurrHealthPointsDmg modifier instructions."""

        _interval, tick_modifiers, start_modifiers = (
            self._status_effect_runtime_program(definition.effect_id)
        )
        encoded_program = tick_modifiers if ticks else start_modifiers
        result = []
        for encoded in str(encoded_program).split(";"):
            kind, separator, arguments_raw = encoded.partition(":")
            if (
                not separator
                or kind.strip().casefold() != "currhealthpointsdmg"
            ):
                continue
            arguments = tuple(
                value.strip() for value in arguments_raw.split(",")
            )
            if not arguments:
                continue
            amount = self._modifier_amount(
                None, arguments[0], definition.values
            )
            if amount is None or abs(amount) <= 1e-12:
                continue
            damage_type = next(
                (
                    value
                    for value in arguments[1:]
                    if value.casefold() in DAMAGE_TYPE_IDS
                ),
                "Physical",
            )
            for value in arguments[1:]:
                if value.casefold().startswith("damagedone"):
                    suffix = value[len("DamageDone"):]
                    if suffix.casefold() in DAMAGE_TYPE_IDS:
                        damage_type = suffix
            result.append(
                (
                    abs(float(amount)),
                    damage_type,
                    any(
                        value.casefold() == "relative"
                        for value in arguments[1:]
                    ),
                )
            )
        return tuple(result)

    def monster_status_damage_program(
        self, definition: VictimStatusEffectDefinition
    ) -> tuple[float, float, str, bool] | None:
        """Return interval/coefficient/type/mode for a monster DoT."""

        interval, _ticks, _start = self._status_effect_runtime_program(
            definition.effect_id
        )
        programs = self._status_damage_programs(definition, ticks=True)
        if not programs:
            return None
        coefficient, damage_type, relative = programs[0]
        return max(1.0 / SERVER_TICKS_PER_SECOND, interval), (
            coefficient
        ), damage_type, relative

    def status_effect_immediate_damage(
        self,
        actor_id: int,
        definitions: tuple[VictimStatusEffectDefinition, ...],
        player_damage_range: tuple[float, float],
        *,
        now: float,
    ) -> tuple[int, tuple[int, ...]]:
        """Resolve authored impact/explosion damage carried by effects."""

        average_damage = max(0.0, sum(player_damage_range) * 0.5)
        total = 0.0
        damage_types = []
        for definition in definitions:
            for coefficient, damage_type, relative in (
                self._status_damage_programs(definition, ticks=False)
            ):
                raw_damage = (
                    average_damage * coefficient if relative else coefficient
                )
                total += raw_damage * self.monster_incoming_damage_multiplier(
                    actor_id, damage_type, now=now
                )
                for value in damage_type_ids(damage_type):
                    if value not in damage_types:
                        damage_types.append(value)
        return max(0, int(math.ceil(total))), tuple(damage_types)

    def resolve_due_monster_status_damage(
        self,
        *,
        now: float | None = None,
        server_tick: int = 0,
    ) -> tuple[DamageResolution, ...]:
        """Apply every due database-authored DoT on its own exact cadence."""

        current_time = time.perf_counter() if now is None else float(now)
        resolutions = []
        for key, state in tuple(self.monster_status_damage.items()):
            actor_id, resource_id = key
            previous = self.monster_health.get(actor_id, 0)
            if previous <= 0:
                self.monster_status_damage.pop(key, None)
                continue
            # Do not manufacture a late tick after the effect lease ended.
            if state.next_at > state.expires_at + 1e-9:
                self.monster_status_damage.pop(key, None)
                continue
            emitted = 0
            while (
                previous > 0
                and current_time + 1e-9 >= state.next_at
                and state.next_at <= state.expires_at + 1e-9
                and emitted < 4
            ):
                average_damage = max(
                    0.0, sum(state.player_damage_range) * 0.5
                )
                raw_damage = (
                    average_damage * state.coefficient
                    if state.relative
                    else state.coefficient
                ) * max(1, state.stack_count)
                damage = max(
                    1,
                    int(
                        math.ceil(
                            raw_damage
                            * self.monster_incoming_damage_multiplier(
                                actor_id,
                                state.damage_type,
                                now=current_time,
                            )
                        )
                    ),
                )
                current, killed = _monster_health_after_damage(
                    self.monsters[actor_id], previous, damage
                )
                self.monster_health[actor_id] = current
                resolutions.append(
                    DamageResolution(
                        target_actor_id=actor_id,
                        attacker_actor_id=state.source_actor_id,
                        current_health=current,
                        maximum_health=self.monsters[actor_id].maximum_health,
                        damage=damage,
                        killed=killed,
                        position=self.monster_positions[actor_id],
                        server_tick=int(server_tick) & 0xFFFFFFFF,
                        hit_tags=damage_type_ids(state.damage_type),
                        skill_index=state.skill_index,
                        source_actor_id=state.source_actor_id,
                        periodic_damage=True,
                    )
                )
                previous = current
                emitted += 1
                state.next_at += state.interval_seconds
            if previous <= 0 or current_time >= state.expires_at:
                self.monster_status_damage.pop(key, None)
                if previous <= 0:
                    self.monster_status_effects.get(actor_id, {}).clear()
                    for other_key in tuple(self.monster_status_damage):
                        if other_key[0] == actor_id:
                            self.monster_status_damage.pop(other_key, None)
        return tuple(resolutions)

    def resolve_socket_periodic_auras(
        self,
        owner_actor_id: int,
        owner_position: tuple[float, float, float],
        periodic_auras,
        *,
        player_damage_range: tuple[float, float],
        player_incoming_damage_multipliers=(),
        player_invulnerable: bool = False,
        now: float | None = None,
        server_tick: int = 0,
    ) -> tuple[tuple[DamageResolution, ...], tuple[DamageResolution, ...]]:
        """Resolve due socket auras against nearby monsters and the causer.

        The caller supplies only auras whose authored cadence is due.  This
        method performs the spatial query against the active monster set and
        reuses ``CurrHealthPointsDmg`` parsing, mitigation, and periodic combat
        text from ordinary status effects.
        """

        current_time = time.perf_counter() if now is None else float(now)
        owner_actor_id = int(owner_actor_id)
        monster_results = []
        owner_results = []
        average_damage = max(0.0, sum(player_damage_range) * 0.5)
        incoming_by_type = {
            str(name).casefold(): max(0.0, float(value))
            for name, value in dict(player_incoming_damage_multipliers).items()
        }
        for aura in periodic_auras:
            radius = max(0.0, float(aura.aura_definition.aura_radius))
            if radius <= 0.0:
                continue
            hit_enemy = False
            for actor_id in self.living_actor_ids:
                monster = self.monsters[actor_id]
                if self._distance(
                    owner_position, self.monster_positions[actor_id]
                ) > radius + max(0.0, monster.collision_radius):
                    continue
                definitions = tuple(aura.receiver_definitions)
                damage, damage_types = self.status_effect_immediate_damage(
                    actor_id,
                    definitions,
                    player_damage_range,
                    now=current_time,
                )
                previous = self.monster_health.get(actor_id, 0)
                if previous <= 0 or damage <= 0:
                    continue
                current, killed = _monster_health_after_damage(
                    monster, previous, damage
                )
                self.monster_health[actor_id] = current
                hit_enemy = True
                monster_results.append(
                    DamageResolution(
                        target_actor_id=actor_id,
                        attacker_actor_id=owner_actor_id,
                        current_health=current,
                        maximum_health=monster.maximum_health,
                        damage=damage,
                        killed=killed,
                        position=self.monster_positions[actor_id],
                        server_tick=int(server_tick) & 0xFFFFFFFF,
                        hit_tags=damage_types or (5,),
                        skill_index=0,
                        victim_status_effects=definitions,
                        source_actor_id=owner_actor_id,
                        periodic_damage=True,
                    )
                )

            # The causer half is the price of a hit: Poisonous Thorns "also
            # deal 35% poison damage to you whenever it afflicts damage to an
            # enemy". Charging it on every pulse drained an idle wearer with
            # nothing in range, by a share of their own base damage.
            if not hit_enemy:
                continue
            self_damage = 0.0
            self_types = []
            for definition in tuple(aura.causer_definitions):
                for coefficient, damage_type, relative in (
                    self._status_damage_programs(definition, ticks=False)
                ):
                    raw = average_damage * coefficient if relative else coefficient
                    normalized = (
                        "darkmagic"
                        if damage_type.casefold() == "andermagic"
                        else damage_type.casefold()
                    )
                    self_damage += raw * incoming_by_type.get(normalized, 1.0)
                    self_types.extend(damage_type_ids(damage_type))
            requested = (
                0 if player_invulnerable
                else max(0, int(math.ceil(self_damage)))
            )
            if requested <= 0:
                continue
            health = self.player_health.setdefault(
                owner_actor_id,
                [self.player_maximum_health, self.player_maximum_health],
            )
            previous, maximum = health
            current = max(1, previous - requested)
            applied = previous - current
            if applied <= 0:
                continue
            health[0] = current
            if owner_actor_id == self.player_actor_id:
                self.player_current_health = current
            owner_results.append(
                DamageResolution(
                    target_actor_id=owner_actor_id,
                    attacker_actor_id=owner_actor_id,
                    current_health=current,
                    maximum_health=maximum,
                    damage=applied,
                    killed=False,
                    position=owner_position,
                    server_tick=int(server_tick) & 0xFFFFFFFF,
                    hit_tags=tuple(dict.fromkeys(self_types)) or (5,),
                    skill_index=0,
                    source_actor_id=owner_actor_id,
                    periodic_damage=True,
                )
            )
        return tuple(monster_results), tuple(owner_results)

    def player_status_heal_program(
        self,
        definition: VictimStatusEffectDefinition,
    ) -> tuple[float, float, int, float] | None:
        """Return interval/fraction/count/delay for a health-over-time effect."""

        interval, tick_modifiers, start_modifiers = (
            self._status_effect_runtime_program(definition.effect_id)
        )
        fraction = None
        starts_immediately = False
        for program_name, encoded_program in (
            ("start", start_modifiers),
            ("tick", tick_modifiers),
        ):
            if fraction is not None:
                break
            for encoded in str(encoded_program).split(";"):
                parts = [part.strip() for part in encoded.split(",")]
                if not parts or not parts[0].casefold().startswith(
                    "currhealthpoints:"
                ):
                    continue
                raw_value = parts[0].partition(":")[2].strip()
                if not any(
                    part.casefold() == "relative" for part in parts[1:]
                ):
                    continue
                if raw_value.startswith("$") and raw_value[1:].isdigit():
                    value_index = int(raw_value[1:])
                    if value_index >= len(definition.values):
                        continue
                    fraction = float(definition.values[value_index])
                else:
                    try:
                        fraction = float(raw_value)
                    except ValueError:
                        continue
                starts_immediately = program_name == "start"
                break
        if fraction is None or fraction <= 0.0:
            return None
        duration_seconds = (
            definition.duration_ticks / SERVER_TICKS_PER_SECOND
        )
        count = max(1, int(math.floor(duration_seconds / interval + 1e-6)))
        return (
            interval,
            fraction,
            count,
            (
                definition.delay_ticks / SERVER_TICKS_PER_SECOND
                - (interval if starts_immediately else 0.0)
            ),
        )

    def player_status_minion_auras(self, monster_template_id: str):
        """Resolve permanent auras of finite status-created actors.

        Cached per template: the static database does not change while the
        server runs, and this is asked at every summon spawn.
        """
        cache = getattr(self, "_player_status_minion_aura_cache", None)
        if cache is None:
            cache = self._player_status_minion_aura_cache = {}
        if monster_template_id not in cache:
            cache[monster_template_id] = self._load_player_status_minion_auras(monster_template_id)
        return cache[monster_template_id]

    def _load_player_status_minion_auras(self, monster_template_id: str):
        with sqlite3.connect(f"file:{self.encounter.static_database_path.as_posix()}?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT PermanentStatusEffect, LifeTime FROM _Template_Monster WHERE Id=?",
                (monster_template_id,),
            ).fetchone()
            if row is None or float(row[1] or 0) <= 0:
                return ()
            result = []
            for root in self._load_victim_status_effects(db, str(row[0] or "")):
                if root.aura_targets.casefold() not in {"enemies", "summoner"}:
                    continue
                aura = db.execute("SELECT AuraEffectId FROM _Template_StatusEffect WHERE Id=?", (root.resource_id,)).fetchone()
                if aura is None:
                    # A status effect the database lacks has no aura to apply.
                    continue
                children = self._load_victim_status_effects(db, str(aura[0] or ""), default_trigger="immediate")
                if children:
                    result.append((replace(root, duration_ticks=root.duration_ticks or round(float(row[1]) * SERVER_TICKS_PER_SECOND)), children))
            return tuple(result)

    def player_status_minion_specs(
        self,
        definition: VictimStatusEffectDefinition,
    ) -> tuple[tuple[str, int, tuple[float, float, float, float]], ...]:
        """Return generated ``Minion`` instructions owned by a status effect.

        Jewel procs use status-effect programs instead of Skill.Summons, so
        they never reach ``player_summon_definition``.  Parse the already
        cached runtime program here and feed the same authoritative summon
        lifecycle used by ordinary class skills.
        """

        _interval, _tick_modifiers, start_modifiers = (
            self._status_effect_runtime_program(definition.effect_id)
        )
        result = []
        for encoded in str(start_modifiers).split(";"):
            kind, separator, arguments = encoded.partition(":")
            if not separator or kind.strip().casefold() != "minion":
                continue
            values = tuple(value.strip() for value in arguments.split(","))
            if not values or not values[0]:
                continue
            try:
                amount = max(1, int(round(float(values[1]))))
            except (IndexError, ValueError):
                amount = 1
            coordinates = []
            for raw in values[2:6]:
                try:
                    coordinates.append(float(raw))
                except ValueError:
                    coordinates.append(0.0)
            coordinates.extend(0.0 for _ in range(4 - len(coordinates)))
            result.append(
                (values[0], amount, tuple(coordinates[:4]))
            )
        return tuple(result)

    def heal_player_relative(
        self, actor_id: int, fraction: float
    ) -> tuple[int, int]:
        """Apply one authoritative relative-health status-effect tick."""

        actor_id = int(actor_id)
        health = self.player_health.setdefault(
            actor_id,
            [self.player_maximum_health, self.player_maximum_health],
        )
        current, maximum = health
        if current <= 0:
            return 0, maximum
        recovered = max(1, int(round(maximum * max(0.0, float(fraction)))))
        current = min(maximum, current + recovered)
        health[0] = current
        if actor_id == self.player_actor_id:
            self.player_current_health = current
        return current, maximum

    def heal_player_amount(
        self, actor_id: int, amount: int
    ) -> tuple[int, int, int]:
        """Apply an exact authoritative heal and return its real recovery."""

        actor_id = int(actor_id)
        health = self.player_health.setdefault(
            actor_id,
            [self.player_maximum_health, self.player_maximum_health],
        )
        previous, maximum = health
        if previous <= 0:
            return 0, maximum, 0
        current = min(maximum, previous + max(0, int(amount)))
        health[0] = current
        if actor_id == self.player_actor_id:
            self.player_current_health = current
        return current, maximum, current - previous

    def reflect_player_damage(
        self,
        incoming: DamageResolution,
        *,
        player_actor_id: int,
        fraction: float,
    ) -> DamageResolution | None:
        """Reflect a fraction of a monster hit back to its attacker."""

        monster = self.monsters.get(int(incoming.attacker_actor_id))
        if monster is None or fraction <= 0.0:
            return None
        previous = self.monster_health.get(monster.actor_id, 0)
        if previous <= 0:
            return None
        damage = max(1, int(round(int(incoming.damage) * float(fraction))))
        current, killed = _monster_health_after_damage(
            monster, previous, damage
        )
        self.monster_health[monster.actor_id] = current
        return DamageResolution(
            target_actor_id=monster.actor_id,
            attacker_actor_id=int(player_actor_id),
            current_health=current,
            maximum_health=monster.maximum_health,
            damage=damage,
            killed=killed,
            position=self.monster_positions[monster.actor_id],
            server_tick=(int(incoming.server_tick) + 1) & 0xFFFFFFFF,
            hit_tags=(0,),
            skill_index=0,
            victim_status_effects=(),
            source_actor_id=int(player_actor_id),
        )

    @staticmethod
    def _modifier_source_values(
        source: object,
    ) -> tuple[int | None, int, tuple[float, ...]]:
        if hasattr(source, "effect_id"):
            return (
                int(source.effect_id),
                max(1, int(getattr(source, "field_18", 0)) + 1),
                tuple(
                    float(value)
                    for value in getattr(source, "values", ())
                ),
            )
        if isinstance(source, int):
            return int(source), 1, ()
        return None, 1, ()

    def player_incoming_damage_multiplier(
        self,
        modifier_sources: tuple[object, ...],
        *,
        damage_type: str = "Physical",
    ) -> float:
        """Apply active relative armor/resistance buffs to incoming damage.

        The server's compact combat model does not expose the client's full
        rating curve yet. Relative Resistance modifiers are therefore applied
        to the effective defense multiplier. This keeps support effects
        authoritative and monotonic while retaining the authored stack values.
        """

        wanted = str(damage_type).casefold()
        resistance_bonus = 0.0
        for source in modifier_sources:
            if (
                hasattr(source, "kind")
                and str(source.kind).casefold()
                in {"resistance", "armor"}
                and getattr(source, "arguments", ())
            ):
                arguments = tuple(
                    str(value) for value in source.arguments
                )
                scopes = {
                    self._clean_modifier_token(value).casefold()
                    for value in arguments[2:]
                    if self._clean_modifier_token(value)
                }
                if scopes and wanted not in scopes and not (
                    wanted == "physical" and "armor" in scopes
                ):
                    continue
                try:
                    amount = float(
                        self._clean_modifier_token(arguments[0])
                    )
                except ValueError:
                    amount = 0.0
                operation = (
                    arguments[1].casefold()
                    if len(arguments) > 1
                    else "absolute"
                )
                # Relative set bonuses are true defense multipliers.  The
                # absolute rating form needs the complete level rating curve
                # and is already represented client-side; do not pretend the
                # small pre-progression template scalar is a percentage.
                if operation == "relative":
                    resistance_bonus += max(0.0, amount)
                continue
            effect_id, stack_count, details = (
                self._modifier_source_values(source)
            )
            if effect_id is None:
                continue
            _interval, _tick, start_modifiers = (
                self._status_effect_runtime_program(effect_id)
            )
            for encoded in str(start_modifiers).split(";"):
                parts = [part.strip() for part in encoded.split(",")]
                if not parts or not parts[0].casefold().startswith(
                    "resistance:"
                ):
                    continue
                kinds = {part.casefold() for part in parts[2:]}
                if kinds and wanted not in kinds:
                    continue
                raw_value = parts[0].partition(":")[2].strip()
                if not raw_value.startswith("$") or not raw_value[1:].isdigit():
                    continue
                index = int(raw_value[1:])
                if index >= len(details):
                    continue
                if not any(
                    part.casefold() == "relative" for part in parts[1:]
                ):
                    continue
                resistance_bonus += max(
                    0.0,
                    float(details[index]) * max(1, stack_count),
                )
        return 1.0 / (1.0 + resistance_bonus)

    def maybe_start_player_summon_attacks(
        self,
        player_positions: dict[int, tuple[float, float, float]],
        *,
        server_tick: int,
        now: float | None = None,
        hostile_player_actor_ids_by_owner: (
            dict[int, frozenset[int] | set[int]] | None
        ) = None,
    ) -> tuple[MonsterAttack, ...]:
        """Start autonomous turret casts against nearby hostile actors."""

        current_time = time.perf_counter() if now is None else float(now)
        hostile_by_owner = hostile_player_actor_ids_by_owner or {}
        started = []
        for actor_id, summon in tuple(self.player_summons.items()):
            definition = summon.definition
            can_target_players = not (
                str(getattr(definition, "category", "")).casefold()
                == "mercenary"
                and str(self.encounter.map_id).casefold()
                in MERCENARY_PVP_SAFE_MAP_IDS
            )
            skill_index = definition.attack_skill_index
            database_skill_index = (
                definition.attack_skill_database_index
            )
            attack_skill_type = definition.attack_skill_type
            attack_interval_seconds = definition.attack_interval_seconds
            attack_skill_unblock_frame = (
                definition.attack_skill_unblock_frame
            )
            attack_motion_unblock_frame = (
                definition.attack_motion_unblock_frame
            )
            attack_loop_start_frame = definition.attack_loop_start_frame
            overheat_until = self.player_summon_overheated_until.get(
                actor_id, 0.0
            )
            if overheat_until <= current_time:
                self.player_summon_overheated_until.pop(actor_id, None)
                overheat_until = 0.0
            is_overheated = overheat_until > current_time
            if (
                is_overheated
                and definition.overheat_attack_skill_database_index
                is not None
            ):
                database_skill_index = (
                    definition.overheat_attack_skill_database_index
                )
                skill_index = database_skill_index
                attack_skill_type = definition.overheat_attack_skill_type
                attack_interval_seconds = (
                    definition.overheat_attack_interval_seconds
                )
                attack_skill_unblock_frame = (
                    definition.overheat_attack_unblock_frame
                )
                attack_motion_unblock_frame = (
                    definition.overheat_attack_motion_unblock_frame
                )
                attack_loop_start_frame = (
                    definition.overheat_attack_loop_start_frame
                )
            summon_skill_speed = max(
                0.01, self._summon_status_scale(
                    actor_id, "Speed", "Skills", now=current_time,
                ),
            )
            if (
                skill_index is None
                or self.player_summon_health.get(actor_id, [0])[0] <= 0
                or self._monster_actions_disabled(actor_id, now=current_time)
                or database_skill_index is None
                or actor_id in self.pending_player_summon_attacks
                or (
                    summon.expires_at is not None
                    and summon.expires_at <= current_time
                )
                or current_time < summon.attack_ready_at
                or (
                    actor_id in self.last_player_summon_attack_at
                    and current_time - self.last_player_summon_attack_at[actor_id]
                    < attack_interval_seconds / summon_skill_speed
                )
            ):
                continue
            geometry = self._load_player_skill_geometry(
                database_skill_index
            )
            if geometry is None:
                continue
            geometry = self._modified_player_skill_geometry(
                geometry, summon.modifier_sources
            )
            is_covering_fire = (
                geometry.resource_id == "minionskill_coveringfireturret"
            )
            is_stationary_sustained = (
                attack_skill_type == "Sustained"
                and geometry.targeting_type.casefold()
                in {"angle", "cone"}
            )
            fixed_direction = bool(getattr(definition, 'fixed_direction', False))
            if fixed_direction:
                yaw = summon.yaw
                direction = (-math.sin(yaw), 0.0, -math.cos(yaw), 0.0)
                reach = max(1.0, geometry.projectile_range or geometry.attack_range)
                ray = replace(geometry, attack_range=reach, hit_range=reach,
                              player_width=2 * geometry.projectile_radius,
                              end_width=2 * geometry.projectile_radius)
                targets = self._stationary_directional_targets(
                    summon.position, direction, ray,
                )[:max(1, geometry.impact_count)]
                target_actor_ids = tuple(target.actor_id for target in targets)
                target_actor_id = target_actor_ids[0] if target_actor_ids else 0
                target_position = tuple(
                    summon.position[i] + direction[i] * reach for i in range(3)
                )
            elif is_covering_fire:
                target_actor_id = summon.owner_actor_id
                target_position = player_positions.get(target_actor_id)
                if target_position is None:
                    continue
                if (
                    self._distance(summon.position, target_position)
                    > geometry.attack_range
                ):
                    continue
                target_actor_ids = (target_actor_id,)
            elif is_stationary_sustained:
                # Retail preserves the original ShiftedSkill aim for Machine
                # Gun's complete lifetime. It is a fixed forward cone, not an
                # autonomous 360-degree target seeker.
                yaw = summon.yaw
                direction = (
                    -math.sin(yaw),
                    0.0,
                    -math.cos(yaw),
                    0.0,
                )
                targets = self._stationary_directional_targets(
                    summon.position,
                    direction,
                    geometry,
                )
                hostile_player_ids = tuple(
                    target_actor_id
                    for target_actor_id in (
                        hostile_by_owner.get(summon.owner_actor_id, ())
                        if can_target_players else ()
                    )
                    if target_actor_id in player_positions
                    and self._stationary_directional_position_is_target(
                        summon.position,
                        direction,
                        geometry,
                        player_positions[target_actor_id],
                    )
                )
                target_actor_ids = tuple(
                    target.actor_id for target in targets
                ) + hostile_player_ids
                if target_actor_ids:
                    target_actor_id = target_actor_ids[0]
                    target_position = (
                        self.monster_positions[target_actor_id]
                        if target_actor_id in self.monsters
                        else player_positions[target_actor_id]
                    )
                else:
                    # Machine Gun continuously plays its sustained forward
                    # fire even with an empty cone. Damage remains empty until
                    # a hostile actor crosses that fixed cone.
                    target_actor_id = 0
                    reach = max(
                        geometry.attack_range,
                        geometry.hit_range,
                        geometry.projectile_range,
                        1.0,
                    )
                    target_position = (
                        summon.position[0] + direction[0] * reach,
                        summon.position[1],
                        summon.position[2] + direction[2] * reach,
                    )
            elif (
                attack_skill_type == "Melee"
                and geometry.targeting_type.casefold() == "radius"
                and (
                    definition.stationary
                    or getattr(definition, "movement_locked", False)
                )
            ):
                # A stationary summon's area strike (the Tesla Turret: Melee,
                # Radius, HitRange 3, its damage auras 3 m) hits every enemy
                # within its HitRange around itself. Its AttackRange (200)
                # only lets it act: picked as the one nearest monster within
                # that, it zapped a single monster up to 200 m away.
                monsters_in_area = [
                    (self._distance(summon.position, self.monster_positions[monster_actor_id]), monster_actor_id)
                    for monster_actor_id in self.living_actor_ids
                    if monster_actor_id not in self.returning_home_actor_ids
                ]
                players_in_area = [
                    (self._distance(summon.position, player_positions[target_actor_id]), target_actor_id)
                    for target_actor_id in hostile_by_owner.get(summon.owner_actor_id, ())
                    if can_target_players and target_actor_id in player_positions
                ]
                area = sorted(
                    [
                        (distance, monster_actor_id)
                        for distance, monster_actor_id in monsters_in_area
                        if distance <= geometry.hit_range
                        + max(0.0, self.monsters[monster_actor_id].collision_radius)
                    ]
                    + [
                        (distance, player_actor_id)
                        for distance, player_actor_id in players_in_area
                        if distance <= geometry.hit_range
                    ]
                )
                if not area:
                    continue
                target_actor_ids = tuple(actor for _distance, actor in area)
                target_actor_id = target_actor_ids[0]
                target_position = (
                    self.monster_positions[target_actor_id]
                    if target_actor_id in self.monsters
                    else player_positions[target_actor_id]
                )
            else:
                candidates = tuple(
                    (
                        self._distance(
                            summon.position,
                            self.monster_positions[monster_actor_id],
                        ),
                        monster_actor_id,
                    )
                    for monster_actor_id in self.living_actor_ids
                    if monster_actor_id not in self.returning_home_actor_ids
                ) + tuple(
                    (
                        self._distance(
                            summon.position,
                            player_positions[target_actor_id],
                        ),
                        target_actor_id,
                    )
                    for target_actor_id in hostile_by_owner.get(
                        summon.owner_actor_id, ()
                    )
                    if can_target_players
                    and target_actor_id in player_positions
                )
                if not candidates:
                    continue
                distance, target_actor_id = min(candidates)
                if distance > max(
                    geometry.attack_range,
                    geometry.hit_range,
                ):
                    continue
                target_position = (
                    self.monster_positions[target_actor_id]
                    if target_actor_id in self.monsters
                    else player_positions[target_actor_id]
                )
                target_actor_ids = (target_actor_id,)

            if not is_stationary_sustained and not fixed_direction:
                dx = target_position[0] - summon.position[0]
                dy = target_position[1] - summon.position[1]
                dz = target_position[2] - summon.position[2]
                length = math.sqrt(dx * dx + dy * dy + dz * dz)
                direction = (
                    (dx / length, dy / length, dz / length, 0.0)
                    if length > 1e-6
                    else (0.0, 0.0, -1.0, 0.0)
                )
                yaw = (
                    math.atan2(-dx, -dz)
                    if math.hypot(dx, dz) > 1e-6
                    else summon.yaw
                )
                if (
                    attack_skill_type == "Melee"
                    and geometry.targeting_type.casefold()
                    in {"angle", "cone"}
                ):
                    direction = (
                        -math.sin(yaw), 0.0, -math.cos(yaw), 0.0
                    )
                    directional_targets = self._stationary_directional_targets(
                        summon.position, direction, geometry
                    )
                    if directional_targets:
                        target_actor_ids = tuple(
                            target.actor_id
                            for target in directional_targets
                        )
                        target_actor_id = target_actor_ids[0]
                        target_position = self.monster_positions[
                            target_actor_id
                        ]
            else:
                length = self._distance(
                    summon.position, target_position
                )
            command_id = {
                "Ranged": BULLET_SKILL_COMMAND,
                "RangedTarget": TARGET_BULLET_SKILL_COMMAND,
                "Melee": SKILL_COMMAND,
                "Sustained": SUSTAINED_SKILL_COMMAND,
            }.get(attack_skill_type, 0)
            if attack_skill_type == 'MeleeTarget':
                command_id = SKILL_COMMAND
            emit_playback = (
                definition.attack_playback_enabled
                and bool(command_id)
                and (
                    command_id != SUSTAINED_SKILL_COMMAND
                    or current_time
                    - self.last_player_summon_playback_at.get(
                        actor_id, 0.0
                    )
                    >= (
                        attack_interval_seconds
                        if is_overheated
                        else definition.attack_playback_interval_seconds
                    )
                )
            )
            if not emit_playback:
                command_id = 0
            start_tick = int(server_tick) & 0xFFFFFFFF
            started_effect_ids = self.started_player_summon_effect_ids.get(
                actor_id, set()
            )
            skillstart_effects = tuple(
                effect
                for effect in geometry.user_status_effects
                if effect.effect_id not in started_effect_ids
                if effect.trigger in {"", "skillstart"}
                and self._effect_applies(
                    effect,
                    cast_id=start_tick + actor_id * 257,
                    killed=False,
                )
            )
            impact_delay = (
                geometry.bullet_emit_delay / summon_skill_speed
                + (
                    length / geometry.projectile_velocity
                    if geometry.projectile_velocity > 1e-6
                    else 0.0
                )
                if command_id
                in {BULLET_SKILL_COMMAND, TARGET_BULLET_SKILL_COMMAND}
                else geometry.hit_frame / SERVER_TICKS_PER_SECOND / summon_skill_speed
            )
            impact_tick = (
                start_tick
                + round(impact_delay * SERVER_TICKS_PER_SECOND)
            ) & 0xFFFFFFFF
            attack = MonsterAttack(
                attacker_actor_id=actor_id,
                target_actor_id=target_actor_id,
                skill_level=0,
                skill_index=skill_index,
                command_id=command_id,
                aim_angle=yaw,
                server_tick=start_tick,
                cast_id=0,
                impact_tick=impact_tick,
                hit_frame=round(geometry.hit_frame / summon_skill_speed),
                motion_unblock_frame=(
                    round((attack_motion_unblock_frame or geometry.hit_frame) / summon_skill_speed)
                ),
                skill_unblock_frame=(
                    round((attack_skill_unblock_frame or max(
                        geometry.hit_frame,
                        round(
                            attack_interval_seconds
                            * SERVER_TICKS_PER_SECOND
                        ),
                    )) / summon_skill_speed)
                ),
                position=(*summon.position, 1.0),
                target_position=target_position,
                direction=direction,
                projectile_radius=geometry.projectile_radius,
                variant=1,
                due_at=current_time + impact_delay,
                projectile_origin=summon.position,
                maximum_projectile_range=max(
                    geometry.attack_range,
                    geometry.projectile_range,
                ),
                motion_type=geometry.motion_type,
                user_status_effects=skillstart_effects,
                loop_start_frame=round(attack_loop_start_frame / summon_skill_speed),
                sustained_hit_range=max(
                    0, int(round(geometry.hit_range))
                ),
                execution_context_tick=(
                    (
                        start_tick
                        + SUSTAINED_SKILL_CONTEXT_OFFSET_TICKS
                    )
                    & 0xFFFFFFFF
                    if command_id == SUSTAINED_SKILL_COMMAND
                    else None
                ),
                bullet_count=getattr(geometry, "bullet_count", 1),
            )
            self.last_player_summon_attack_at[actor_id] = current_time
            if emit_playback:
                self.last_player_summon_playback_at[actor_id] = current_time
            if skillstart_effects:
                self.started_player_summon_effect_ids.setdefault(
                    actor_id, set()
                ).update(
                    effect.effect_id for effect in skillstart_effects
                )
            if target_actor_ids:
                target_due_times = ()
                if fixed_direction:
                    arrivals = []
                    for target in target_actor_ids:
                        travel = geometry.bullet_emit_delay + self._distance(
                            summon.position, self.monster_positions[target],
                        ) / max(.01, geometry.projectile_velocity)
                        arrivals.append((
                            target, current_time + travel,
                            (start_tick + round(travel * SERVER_TICKS_PER_SECOND)) & 0xffffffff,
                        ))
                    target_due_times = tuple(sorted(arrivals, key=lambda item: item[1]))
                    attack = replace(attack, due_at=target_due_times[0][1],
                                     impact_tick=target_due_times[0][2])
                self.pending_player_summon_attacks[actor_id] = (
                    _PendingPlayerSummonAttack(
                        attack,
                        geometry,
                        summon.player_damage_range,
                        target_actor_ids,
                        target_due_times,
                    )
                )
            if emit_playback or skillstart_effects:
                started.append(attack)
        return tuple(started)

    def _summon_owner_aura_enabled(self, summon, resource_id: str) -> bool:
        effect_id = self._status_effect_ids.get(resource_id)
        for source in summon.modifier_sources:
            if getattr(source, "effect_id", source) == effect_id:
                return True
            programs = (
                ((source.kind, source.arguments),)
                if hasattr(source, "kind") and hasattr(source, "arguments")
                else self._status_modifier_program(int(source.effect_id))
                if hasattr(source, "effect_id") else ()
            )
            if any(
                kind.casefold() == "actorstatuseffect"
                and len(arguments) >= 2
                and self._clean_modifier_token(arguments[1]) == resource_id
                for kind, arguments in programs
            ):
                return True
        return False

    def _apply_summon_causer_effects(
            self, summon, geometry, *, now: float, server_tick: int,
            pvp: bool = False) -> tuple[PlayerSupportApplication, ...]:
        """Apply Causer-hosted hit effects to the minion, including lethal hits."""
        actor_id = int(summon.actor_id)
        applied = []
        active = self.monster_status_effects.get(actor_id, {})
        for effect in geometry.victim_status_effects:
            if effect.effect_hosts.casefold() != "causer":
                continue
            if pvp and "disabledmatchpvp" in self._status_groups_for_effect(effect.effect_id):
                continue
            if not self._effect_applies(effect, cast_id=server_tick + actor_id, killed=False):
                continue
            previous = active.get(effect.resource_id)
            count = (
                previous[0].stack_count if previous and previous[1] > now else 0
            )
            applied.append(replace(
                effect, stack_count=min(max(1, effect.max_stack_size), count + 1),
            ))
        if not applied:
            return ()
        heat = next((e for e in applied if e.resource_id == "mercenary_ember_knight_heat"), None)
        if heat is not None:
            self.player_summon_heat_hits[actor_id] = heat.stack_count
            if (
                heat.stack_count >= heat.max_stack_size
                and self._summon_owner_aura_enabled(summon, "mercenary_ember_knight_heatmax_aura")
            ):
                until = now + heat.duration_ticks / SERVER_TICKS_PER_SECOND
                if self.player_summon_overheated_until.get(actor_id, 0.0) <= now:
                    effect_id = getattr(summon.definition, "overheat_attack_skill_effect_id", None)
                    if effect_id is not None:
                        applied.append(VictimStatusEffectDefinition(
                            resource_id="mercenary_ember_knight_overheated", effect_id=effect_id,
                            chance=1.0, duration_ticks=heat.duration_ticks,
                            values=(0.0,) * 5, trigger="immediate",
                        ))
                self.player_summon_overheated_until[actor_id] = until
        self._record_monster_status_effects(
            actor_id, tuple(applied), now=now, source_actor_id=actor_id,
        )
        return (PlayerSupportApplication(actor_id, actor_id, server_tick, tuple(applied)),)

    def resolve_due_player_summon_attacks(
        self,
        *,
        now: float | None = None,
        player_positions: (
            dict[int, tuple[float, float, float]] | None
        ) = None,
        server_tick: int = 0,
        hostile_player_actor_ids_by_owner: (
            dict[int, frozenset[int] | set[int]] | None
        ) = None,
        player_damage_multipliers: dict[int, float] | None = None,
        lethal_player_actor_ids_by_owner: (
            dict[int, frozenset[int] | set[int]] | None
        ) = None,
    ) -> tuple[
        tuple[DamageResolution, ...],
        tuple[PlayerSupportApplication, ...],
    ]:
        """Resolve autonomous turret damage and player support projectiles."""

        current_time = time.perf_counter() if now is None else float(now)
        hostile_by_owner = hostile_player_actor_ids_by_owner or {}
        player_damage_multipliers = player_damage_multipliers or {}
        lethal_by_owner = lethal_player_actor_ids_by_owner or {}
        damage_resolutions = []
        support_applications = []
        due_items = [
            (actor_id, pending, None)
            for actor_id, pending in tuple(
                self.pending_player_summon_attacks.items()
            )
        ] + [
            (actor_id, pending, expired)
            for actor_id, (expired, pending) in tuple(
                self.orphaned_player_summon_attacks.items()
            )
        ]
        for actor_id, pending, expired in due_items:
            attack = pending.attack
            if current_time < attack.due_at:
                continue
            if expired is None:
                del self.pending_player_summon_attacks[actor_id]
            else:
                del self.orphaned_player_summon_attacks[actor_id]
            if pending.target_due_times:
                due = tuple(item for item in pending.target_due_times if item[1] <= current_time)
                remaining = tuple(item for item in pending.target_due_times if item[1] > current_time)
                if remaining:
                    rest = replace(
                        pending, attack=replace(attack, due_at=remaining[0][1],
                                                impact_tick=remaining[0][2]),
                        target_actor_ids=tuple(item[0] for item in remaining),
                        target_due_times=remaining,
                    )
                    if expired is None:
                        self.pending_player_summon_attacks[actor_id] = rest
                    else:
                        self.orphaned_player_summon_attacks[actor_id] = (expired, rest)
                pending = replace(pending, target_actor_ids=tuple(item[0] for item in due))
            summon = expired or self.player_summons.get(actor_id)
            if expired is None and (
                    summon is None or self.player_summon_health.get(actor_id, [0])[0] <= 0
                    or self._monster_actions_disabled(actor_id, now=current_time)):
                continue
            if attack.target_actor_id == summon.owner_actor_id:
                effects = tuple(
                    effect
                    for effect in pending.geometry.victim_status_effects
                    if self._effect_applies(
                        effect,
                        cast_id=attack.server_tick + actor_id * 257,
                        killed=False,
                    )
                )
                if effects:
                    support_applications.append(
                        PlayerSupportApplication(
                            actor_id,
                            summon.owner_actor_id,
                            attack.impact_tick,
                            effects,
                        )
                    )
                continue

            is_sustained_directional = (
                attack.command_id == SUSTAINED_SKILL_COMMAND
                and pending.geometry.targeting_type.casefold()
                in {"angle", "cone"}
            )
            for target_actor_id in pending.target_actor_ids:
                target = self.monsters.get(target_actor_id)
                if target is None:
                    if (
                        str(getattr(summon.definition, "category", "")).casefold()
                        == "mercenary"
                        and str(self.encounter.map_id).casefold()
                        in MERCENARY_PVP_SAFE_MAP_IDS
                    ):
                        continue
                    if (
                        target_actor_id
                        not in hostile_by_owner.get(summon.owner_actor_id, ())
                        or player_positions is None
                        or target_actor_id not in player_positions
                    ):
                        continue
                    if (
                        is_sustained_directional
                        and not self._stationary_directional_position_is_target(
                            summon.position,
                            attack.direction,
                            pending.geometry,
                            player_positions[target_actor_id],
                        )
                    ):
                        continue
                    health = self.player_health.get(target_actor_id)
                    if health is None or int(health[0]) <= 0:
                        continue
                    previous, maximum_health = health
                    base_damage = sum(pending.player_damage_range) * 0.5 * (
                        self._summon_status_scale(actor_id, "Damage", "Min", now=current_time)
                        + self._summon_status_scale(actor_id, "Damage", "Max", now=current_time)
                    ) * 0.5 * self._monster_skill_damage_multiplier(actor_id, now=current_time)
                    damage = max(0, int(round(
                        base_damage
                        * max(0.0, pending.geometry.damage_modifier)
                        * max(
                            0.0,
                            float(player_damage_multipliers.get(
                                target_actor_id, 1.0
                            )),
                        )
                    )))
                    lethal_allowed = target_actor_id in lethal_by_owner.get(
                        summon.owner_actor_id, ()
                    )
                    current = max(0 if lethal_allowed else 1, int(previous) - damage)
                    killed = lethal_allowed and current == 0
                    damage = int(previous) - current
                    if damage <= 0:
                        continue
                    health[0] = current
                    if expired is None:
                        support_applications.extend(self._apply_summon_causer_effects(
                            summon, pending.geometry, now=current_time,
                            server_tick=attack.impact_tick, pvp=True,
                        ))
                    effects = tuple(
                        effect
                        for effect in pending.geometry.victim_status_effects
                        if effect.effect_hosts.casefold() != "causer"
                        if self._effect_applies(
                            effect,
                            cast_id=(
                                attack.server_tick
                                + actor_id * 257
                                + target_actor_id
                            ),
                            killed=killed,
                        )
                    )
                    damage_resolutions.append(DamageResolution(
                        target_actor_id=target_actor_id,
                        attacker_actor_id=actor_id,
                        current_health=current,
                        maximum_health=int(maximum_health),
                        damage=damage,
                        killed=killed,
                        position=player_positions[target_actor_id],
                        server_tick=attack.impact_tick,
                        hit_tags=damage_type_ids(
                            pending.geometry.damage_type
                        ),
                        skill_index=attack.skill_index,
                        victim_status_effects=effects,
                        source_actor_id=summon.owner_actor_id,
                    ))
                    continue
                if (
                    self.monster_health.get(target.actor_id, 0) <= 0
                    or target.actor_id in self.returning_home_actor_ids
                ):
                    continue
                if getattr(summon.definition, 'fixed_direction', False):
                    reach = max(1.0, pending.geometry.projectile_range)
                    ray = replace(
                        pending.geometry, attack_range=reach, hit_range=reach,
                        player_width=2 * pending.geometry.projectile_radius,
                        end_width=2 * pending.geometry.projectile_radius,
                    )
                    if not self._stationary_directional_position_is_target(
                        summon.position, attack.direction, ray,
                        self.monster_positions[target.actor_id],
                        collision_radius=target.collision_radius,
                    ):
                        continue
                elif (
                    is_sustained_directional
                    and not self._stationary_directional_position_is_target(
                        summon.position,
                        attack.direction,
                        pending.geometry,
                        self.monster_positions[target.actor_id],
                        collision_radius=target.collision_radius,
                    )
                ):
                    continue
                previous = self.monster_health[target.actor_id]
                base_damage = sum(pending.player_damage_range) * 0.5 * (
                        self._summon_status_scale(actor_id, "Damage", "Min", now=current_time)
                        + self._summon_status_scale(actor_id, "Damage", "Max", now=current_time)
                    ) * 0.5 * self._monster_skill_damage_multiplier(actor_id, now=current_time)
                damage = max(
                    0,
                    int(
                        round(
                            base_damage
                            * max(
                                0.0,
                                pending.geometry.damage_modifier,
                            )
                        )
                    ),
                )
                preliminary_effects = tuple(
                    effect
                    for effect in pending.geometry.victim_status_effects
                    if self._effect_applies(
                        effect,
                        cast_id=(
                            attack.server_tick
                            + actor_id * 257
                            + target.actor_id
                        ),
                        killed=False,
                    )
                )
                immediate_damage, immediate_types = (
                    self.status_effect_immediate_damage(
                        target.actor_id,
                        preliminary_effects,
                        pending.player_damage_range,
                        now=current_time,
                    )
                )
                damage += immediate_damage
                current, killed = _monster_health_after_damage(
                    target, previous, damage
                )
                if damage <= 0:
                    continue
                if expired is None:
                    support_applications.extend(self._apply_summon_causer_effects(
                        summon, pending.geometry, now=current_time,
                        server_tick=attack.impact_tick,
                    ))
                self.monster_health[target.actor_id] = current
                effects = tuple(
                    effect
                    for effect in pending.geometry.victim_status_effects
                    if effect.effect_hosts.casefold() != "causer"
                    if self._effect_applies(
                        effect,
                        cast_id=(
                            attack.server_tick
                            + actor_id * 257
                            + target.actor_id
                        ),
                        killed=killed,
                    )
                )
                if effects and not killed:
                    self._record_monster_status_effects(
                        target.actor_id,
                        effects,
                        now=current_time,
                        source_actor_id=summon.owner_actor_id,
                        player_damage_range=pending.player_damage_range,
                        skill_index=attack.skill_index,
                    )
                damage_resolutions.append(
                    DamageResolution(
                        target_actor_id=target.actor_id,
                        attacker_actor_id=actor_id,
                        current_health=current,
                        maximum_health=target.maximum_health,
                        damage=damage,
                        killed=killed,
                        position=self.monster_positions[
                            target.actor_id
                        ],
                        server_tick=attack.impact_tick,
                        hit_tags=tuple(dict.fromkeys(
                            damage_type_ids(pending.geometry.damage_type)
                            + immediate_types
                        )),
                        skill_index=attack.skill_index,
                        victim_status_effects=effects,
                        source_actor_id=summon.owner_actor_id,
                    )
                )
        for actor_id, summon in tuple(self.player_summons.items()):
            definition = summon.definition
            if current_time < summon.spawn_ready_at:
                continue
            owner_position = (
                player_positions.get(summon.owner_actor_id)
                if player_positions is not None
                else None
            )
            if (
                definition.heal_fraction <= 0.0
                or definition.heal_radius <= 0.0
                or owner_position is None
                or self._distance(summon.position, owner_position)
                > definition.heal_radius
                or current_time
                - self.last_player_summon_support_at.get(actor_id, 0.0)
                < definition.heal_interval_seconds
            ):
                continue
            self.last_player_summon_support_at[actor_id] = current_time
            self.heal_player_relative(
                summon.owner_actor_id, definition.heal_fraction
            )
            if definition.support_status_effects:
                support_applications.append(
                    PlayerSupportApplication(
                        actor_id,
                        summon.owner_actor_id,
                        int(server_tick) & 0xFFFFFFFF,
                        definition.support_status_effects,
                    )
                )
        return tuple(damage_resolutions), tuple(support_applications)

    @staticmethod
    def _effect_applies(
        definition: VictimStatusEffectDefinition,
        *,
        cast_id: int,
        killed: bool,
        critical_hit: bool = False,
        low_health: bool = False,
        frost_hit: bool = False,
        blocked: bool = False,
        event: str = "",
    ) -> bool:
        trigger = "".join(
            character
            for character in str(definition.trigger or "").casefold()
            if character.isalnum()
        )
        explicit_event = "".join(
            character
            for character in str(event or "").casefold()
            if character.isalnum()
        )
        if trigger in {"disabled", "never"}:
            return False
        if explicit_event and trigger and trigger != explicit_event:
            return False
        if trigger == "kill" and not killed:
            return False
        if trigger == "hitcritical" and not critical_hit:
            return False
        if trigger == "hitlowhealth" and not low_health:
            return False
        if trigger == "hitfrost" and not frost_hit:
            return False
        if trigger == "hitblocked" and not blocked:
            return False
        if trigger == "lowhealth" and not low_health:
            return False
        if trigger.startswith("maxstacksize") and not explicit_event:
            return False
        if trigger in {
            "pickuphealthglobe", "revive120"
        } and trigger != explicit_event:
            return False
        if trigger not in {
            "", "hit", "skillstart", "immediate", "immediatemanaged",
            "kill", "hitcritical", "hitlowhealth", "hitfrost",
            "hitblocked", "lowhealth", "pickuphealthglobe", "revive120",
        } and not trigger.startswith("maxstacksize"):
            return False
        if definition.chance <= 0.0:
            return False
        if definition.chance >= 1.0:
            return True
        # Stable per-cast roll keeps tests/replays deterministic without
        # globally coupling combat to Python's random module.
        roll_bits = (
            int(cast_id) * 1_103_515_245
            + int(definition.effect_id) * 12_345
        ) & 0xFFFFFFFF
        return roll_bits / 0x1_0000_0000 < definition.chance

    def resolve_player_hit(
        self,
        cast_id: int,
        *,
        now: float | None = None,
    ) -> DamageResolution | None:
        pending = self.pending_player_hits.get(int(cast_id))
        if pending is None:
            return None
        current_time = time.perf_counter() if now is None else float(now)
        if current_time < pending.due_at:
            return None
        del self.pending_player_hits[int(cast_id)]
        monster = self.monsters[pending.target_actor_id]
        previous = self.monster_health.get(monster.actor_id, 0)
        if previous <= 0:
            return None
        if pending.damage_immune:
            return DamageResolution(
                target_actor_id=monster.actor_id,
                attacker_actor_id=pending.source_actor_id,
                current_health=previous,
                maximum_health=monster.maximum_health,
                damage=0,
                killed=False,
                position=self.monster_positions[monster.actor_id],
                server_tick=pending.impact_tick,
                hit_tags=damage_type_ids(pending.damage_type),
                skill_index=pending.skill_index,
                victim_status_effects=(),
                source_actor_id=pending.source_actor_id,
                critical_hit=False,
                damage_immune=True,
            )
        defensive_multiplier = self.monster_incoming_damage_multiplier(
            monster.actor_id,
            pending.damage_type,
            now=current_time,
        )
        previous = self.monster_health[monster.actor_id]
        # The selected local character is level 100 while this recovered
        # entrance encounter is level 31.  Retail records an overkill hit and
        # clamps the authoritative health to zero, so one accepted cast is a
        # deterministic lethal transition here as well.
        # This legacy single-client helper predates runtime actor/cast keys
        # and remains a deterministic one-cast fixture. Multiplayer gameplay
        # uses ``resolve_runtime_player_hit`` below, which applies the actual
        # database/talent damage coefficient.
        damage = previous if pending.damage_modifier > 0.0 else 0
        current, killed = _monster_health_after_damage(
            monster, previous, damage
        )
        self.monster_health[monster.actor_id] = current
        applied_effects = tuple(
            definition
            for definition in pending.victim_status_effects
            if self._effect_applies(
                definition,
                cast_id=(
                    pending.cast_id
                    + pending.projectile_index * 257
                    + pending.impact_index
                ),
                killed=killed,
            )
        )
        applied_effects = self.accepted_monster_status_effects(
            monster.actor_id, applied_effects, now=current_time
        )
        # Different projectiles from one cast resolve in different timer
        # batches. Stack once for that cast/victim/effect across those batches.
        fresh_effects = []
        for definition in applied_effects:
            key = (pending.source_actor_id, pending.cast_id, monster.actor_id, definition.effect_id)
            previous_application = self._cast_status_applications.get(key)
            if (not pending.cast_id or previous_application is None
                    or current_time - previous_application >= 120.0):
                if pending.cast_id:
                    self._cast_status_applications[key] = current_time
                fresh_effects.append(definition)
        applied_effects = tuple(fresh_effects)
        if len(self._cast_status_applications) > 32768:
            self._cast_status_applications = {k: t for k, t in list(self._cast_status_applications.items())[-32768:]
                                             if current_time - t < 120.0}
        if not killed and applied_effects:
            self._record_monster_status_effects(
                monster.actor_id,
                applied_effects,
                now=current_time,
            )
        return DamageResolution(
            target_actor_id=monster.actor_id,
            attacker_actor_id=self.player_actor_id,
            current_health=current,
            maximum_health=monster.maximum_health,
            damage=damage,
            killed=killed,
            position=self.monster_positions[monster.actor_id],
            server_tick=pending.impact_tick,
            hit_tags=damage_type_ids(pending.damage_type),
            skill_index=pending.skill_index,
            victim_status_effects=applied_effects,
        )

    def resolve_runtime_player_hit(
        self,
        source_actor_id: int,
        cast_id: int,
        impact_index: int,
        *,
        now: float | None = None,
    ) -> DamageResolution | None:
        key = (int(source_actor_id), int(cast_id), int(impact_index))
        pending = self.pending_runtime_player_hits.get(key)
        if pending is None:
            return None
        current_time = time.perf_counter() if now is None else float(now)
        if current_time < pending.due_at:
            return None
        del self.pending_runtime_player_hits[key]
        monster = self.monsters[pending.target_actor_id]
        previous = self.monster_health.get(monster.actor_id, 0)
        if previous <= 0:
            return None
        # An area impact's victims were chosen at the cast; one that walked
        # out of it before it fell is not hurt (a meteor lands 2.6 s later and
        # struck monsters standing beside its crater).
        current = self.monster_positions.get(monster.actor_id)
        if (
            pending.motion_type == "Area"
            and pending.area_radius > 0.0
            and (
                current is None
                or self._distance(current, pending.origin)
                > pending.area_radius + monster.collision_radius
                + PLAYER_PROJECTILE_HITBOX_TOLERANCE
            )
        ):
            return None
        if pending.damage_immune:
            return DamageResolution(
                target_actor_id=monster.actor_id,
                attacker_actor_id=pending.source_actor_id,
                current_health=previous,
                maximum_health=monster.maximum_health,
                damage=0,
                killed=False,
                position=self.monster_positions[monster.actor_id],
                server_tick=pending.impact_tick,
                hit_tags=damage_type_ids(pending.damage_type),
                skill_index=pending.skill_index,
                victim_status_effects=(),
                source_actor_id=pending.source_actor_id,
                critical_hit=False,
                damage_immune=True,
            )
        defensive_multiplier = self.monster_incoming_damage_multiplier(
            monster.actor_id,
            pending.damage_type,
            now=current_time,
        )
        critical_hit = False
        if (
            pending.damage_modifier > 0.0
            and pending.player_maximum_damage > 0.0
        ):
            minimum = min(
                pending.player_minimum_damage,
                pending.player_maximum_damage,
            )
            maximum = max(
                pending.player_minimum_damage,
                pending.player_maximum_damage,
            )
            # A stable per-impact roll keeps packet captures and tests
            # reproducible without coupling combat threads to global RNG.
            roll_bits = (
                pending.cast_id * 1_103_515_245
                + pending.source_actor_id * 12_345
                + pending.projectile_index * 2_654_435_761
                + pending.impact_index * 97_531
            ) & 0xFFFFFFFF
            base_damage = minimum + (
                maximum - minimum
            ) * (roll_bits / 0x1_0000_0000)
            critical_roll_bits = (
                roll_bits * 1_664_525
                + 1_013_904_223
                + pending.skill_index * 257
            ) & 0xFFFFFFFF
            critical_hit = (
                pending.critical_hit_chance > 0.0
                and critical_roll_bits / 0x1_0000_0000
                < pending.critical_hit_chance
            )
            critical_multiplier = (
                pending.critical_damage_multiplier
                if critical_hit else 1.0
            )
            damage = scaled_skill_damage(
                base_damage, pending.damage_modifier,
                mitigation=defensive_multiplier, critical=critical_multiplier,
            )
        else:
            # Compatibility for standalone protocol fixtures which do not
            # own a character/inventory. Production casts always carry the
            # calculated player range above.
            damage = (
                max(
                    1,
                    math.ceil(
                        monster.maximum_health
                        * pending.damage_modifier
                        * defensive_multiplier
                        / max(1, pending.target_hit_count)
                    ),
                )
                if pending.damage_modifier > 0.0
                else 0
            )
        preliminary_effects = tuple(
            definition
            for definition in pending.victim_status_effects
            if self._effect_applies(
                definition,
                cast_id=(
                    pending.cast_id
                    + pending.projectile_index * 257
                    + pending.impact_index
                ),
                killed=False,
                critical_hit=critical_hit,
                low_health=(
                    max(0, previous - damage)
                    <= max(1, round(monster.maximum_health / 3.0))
                ),
                frost_hit=(
                    "ice" in str(pending.damage_type).casefold()
                    or "frost" in str(pending.damage_type).casefold()
                ),
            )
        )
        immediate_damage, immediate_types = self.status_effect_immediate_damage(
            monster.actor_id,
            preliminary_effects,
            (
                pending.player_minimum_damage,
                pending.player_maximum_damage,
            ),
            now=current_time,
        )
        damage += immediate_damage
        current, killed = _monster_health_after_damage(
            monster, previous, damage
        )
        self.monster_health[monster.actor_id] = current
        applied_effects = tuple(
            definition
            for definition in pending.victim_status_effects
            if self._effect_applies(
                definition,
                cast_id=(
                    pending.cast_id
                    + pending.projectile_index * 257
                    + pending.impact_index
                ),
                killed=killed,
                critical_hit=critical_hit,
                low_health=(
                    current
                    <= max(1, round(monster.maximum_health / 3.0))
                ),
                frost_hit=(
                    "ice" in str(pending.damage_type).casefold()
                    or "frost" in str(pending.damage_type).casefold()
                ),
            )
        )
        applied_effects = self.accepted_monster_status_effects(
            monster.actor_id, applied_effects, now=current_time
        )
        if not killed and applied_effects:
            self._record_monster_status_effects(
                monster.actor_id,
                applied_effects,
                now=current_time,
                source_actor_id=pending.source_actor_id,
                player_damage_range=(
                    pending.player_minimum_damage,
                    pending.player_maximum_damage,
                ),
                skill_index=pending.skill_index,
            )
        return DamageResolution(
            target_actor_id=monster.actor_id,
            attacker_actor_id=pending.source_actor_id,
            current_health=current,
            maximum_health=monster.maximum_health,
            damage=damage,
            killed=killed,
            position=self.monster_positions[monster.actor_id],
            server_tick=pending.impact_tick,
            hit_tags=tuple(dict.fromkeys(
                damage_type_ids(pending.damage_type) + immediate_types
            )),
            skill_index=pending.skill_index,
            victim_status_effects=applied_effects,
            cast_id=pending.cast_id,
            critical_hit=critical_hit,
        )

    def apply_bullet_skill(
        self,
        skill: BulletSkill,
        player_position: tuple[float, float, float],
    ) -> DamageResolution | None:
        """Resolve a cast synchronously for protocol/unit-test callers.

        Runtime gameplay uses :meth:`start_bullet_skill` and resolves at
        ``PendingPlayerHit.due_at``; this helper preserves the original public
        API for callers that do not own a scheduler.
        """

        pending = self.start_bullet_skill(
            skill, player_position, now=0.0
        )
        if pending is None:
            return None
        return self.resolve_player_hit(
            pending.cast_id, now=pending.due_at
        )

    def maybe_start_monster_attacks(
        self,
        player_position: tuple[float, float, float],
        *,
        server_tick: int,
        now: float | None = None,
        targets_by_actor=None,
        target_actor_id: int | None = None,
        eligible_actor_ids: (
            set[int] | frozenset[int] | None
        ) = None,
        enemy_positions: (
            dict[int, tuple[float, float, float]] | None
        ) = None,
    ) -> tuple[MonsterAttack, ...]:
        """Start database-selected casts when their cooldown permits.

        ``enemy_positions`` (player actor id -> world position) are the
        players a multi-bullet Shifted cast can also fall on
        (_monster_shifted_points); without it only the target is.
        """

        current_time = time.perf_counter() if now is None else float(now)
        target_actor_id = (
            self.player_actor_id
            if target_actor_id is None
            else int(target_actor_id)
        )
        player_world = self.encounter.coordinate_frame.wire_to_world(
            player_position
        )
        attacks = []
        living_actor_ids = self.living_actor_ids
        if eligible_actor_ids is not None:
            living_actor_ids = tuple(
                actor_id
                for actor_id in living_actor_ids
                if actor_id in eligible_actor_ids
            )
        if not living_actor_ids:
            return ()
        # Resolve mind-control ownership once for this simulation tick.
        # Re-querying every candidate for every attacker made the ordinary
        # no-mind-control case O(monsters^2), including repeated status-data
        # lookups in densely populated maps.
        mind_controlled_actor_ids = frozenset(
            actor_id
            for actor_id in living_actor_ids
            if self._monster_is_mind_controlled(
                actor_id, now=current_time
            )
        )
        start_index = self._monster_attack_cursor % len(living_actor_ids)
        ordered_actor_ids = (
            living_actor_ids[start_index:]
            + living_actor_ids[:start_index]
        )
        next_cursor = (start_index + 1) % len(living_actor_ids)
        for offset, actor_id in enumerate(ordered_actor_ids):
            if (
                eligible_actor_ids is not None
                and actor_id not in eligible_actor_ids
            ):
                continue
            monster = self.monsters[actor_id]
            if (
                self.encounter.map_id.casefold() == "f4203_boss"
                and monster.template_id.casefold() == "f4203_boss_06_ram"
            ):
                # The Crypt of Kings ram is scripted to break the arena door;
                # its retail target is a destroyable obstacle, never a player.
                continue
            tower_fire_burst = (
                monster.invulnerable
                and "fire_shooter" in monster.template_id.casefold()
                and is_tower_mechanism(
                    self.encounter.static_database_path,
                    self.encounter.map_id,
                    monster.template_id,
                )
            )
            pending_attack = self.pending_monster_attacks.get(actor_id)
            boss_projectile_in_flight = (
                pending_attack is not None
                and str(monster.rank_id or "").casefold().startswith("rank_boss")
                and pending_attack.command_id in {
                    BULLET_SKILL_COMMAND, TARGET_BULLET_SKILL_COMMAND,
                }
            )
            if (
                (
                    pending_attack is not None
                    and not tower_fire_burst
                    and not boss_projectile_in_flight
                )
                or (
                    tower_fire_burst
                    and current_time
                    < self.tower_fire_burst_cooldown_until.get(actor_id, 0.0)
                )
                or current_time < self.monster_skill_unblock_at.get(actor_id, 0.0)
                or actor_id in self.returning_home_actor_ids
                or self._monster_actions_disabled(
                    actor_id, now=current_time
                )
            ):
                continue
            if targets_by_actor is not None:
                target = targets_by_actor.get(actor_id)
                if target is None:
                    continue
                target_actor_id, player_world = target
            definitions = monster.attacks or (monster.attack,)
            if not monster.attack.resource_id:
                # Aura-only mounts and scene controllers have no cast skill.
                continue
            if not definitions or definitions[0] != monster.attack:
                definitions = (
                    monster.attack,
                    *tuple(
                        value
                        for value in definitions
                        if value.resource_id != monster.attack.resource_id
                    ),
                )
            difficulty = int(
                getattr(self.encounter, "scaling_difficulty", 0)
            )
            definitions = tuple(
                definition
                for definition in definitions
                if definition.min_difficulty <= difficulty
                <= definition.max_difficulty
            )
            if not definitions:
                continue
            maximum_attack_range = max(
                definition.attack_range for definition in definitions
            )
            monster_position = self.monster_positions[actor_id]
            home_distance = self._distance(
                monster_position, monster.position
            )
            is_boss = str(monster.rank_id or "").casefold().startswith("rank_boss")
            boss_range = max(monster.aggro_range, monster.travel_distance)
            acquisition_range = min(
                monster.aggro_range,
                monster.travel_distance,
                (
                    boss_range
                    if is_boss
                    else (
                        max(
                            MONSTER_ACQUISITION_RADIUS,
                            TOWER_FIRE_ACQUISITION_RANGE,
                        )
                        if tower_fire_burst
                        else MONSTER_ACQUISITION_RADIUS
                    )
                ),
            )
            if (
                home_distance <= MONSTER_HOME_ARRIVAL_DISTANCE
                and self._distance(player_world, monster_position)
                > acquisition_range
            ):
                continue
            simulation_radius = (
                max(
                    boss_range,
                    MONSTER_SIMULATION_RADIUS,
                )
                if is_boss
                else (
                    max(
                        MONSTER_SIMULATION_RADIUS,
                        TOWER_FIRE_ACQUISITION_RANGE,
                    )
                    if tower_fire_burst
                    else MONSTER_SIMULATION_RADIUS
                )
            )
            if (
                self._distance(player_world, monster.position)
                > simulation_radius
                or self._distance(player_world, monster_position)
                > simulation_radius
            ):
                continue
            attack_target_actor_id = target_actor_id
            attack_target_world = player_world
            controlled = actor_id in mind_controlled_actor_ids
            if controlled:
                candidates = [
                    (
                        self._distance(
                            monster_position,
                            self.monster_positions[candidate_id],
                        ),
                        candidate_id,
                    )
                    for candidate_id in living_actor_ids
                    if (
                        candidate_id != actor_id
                        and candidate_id not in mind_controlled_actor_ids
                    )
                ]
                candidates = [
                    value for value in candidates
                    if value[0] <= maximum_attack_range
                ]
                if not candidates:
                    continue
                _target_distance, attack_target_actor_id = min(candidates)
                attack_target_world = self.monster_positions[
                    attack_target_actor_id
                ]
            else:
                taunt_radius = self._mind_control_taunt_radius()
                taunts = [
                    (
                        self._distance(
                            monster_position,
                            self.monster_positions[candidate_id],
                        ),
                        candidate_id,
                    )
                    for candidate_id in living_actor_ids
                    if (
                        candidate_id != actor_id
                        and candidate_id in mind_controlled_actor_ids
                    )
                ]
                taunts = [
                    value for value in taunts
                    if (
                        value[0] <= taunt_radius
                        and value[0] <= maximum_attack_range
                    )
                ]
                if taunts:
                    _target_distance, attack_target_actor_id = min(taunts)
                    attack_target_world = self.monster_positions[
                        attack_target_actor_id
                    ]
            target_distance = self._distance(
                attack_target_world, monster_position
            )
            vertical_gap = abs(attack_target_world[1] - monster_position[1])
            eligible_definitions_list = []
            for definition in definitions:
                if (
                    definition.command_id not in {
                        BULLET_SKILL_COMMAND,
                        TARGET_BULLET_SKILL_COMMAND,
                    }
                    and vertical_gap > MONSTER_MELEE_VERTICAL_REACH
                ):
                    # A blow does not reach a player on a ledge above or
                    # below the monster; only projectiles fly there.
                    continue
                if (
                    definition.motion_type == "Straight"
                    and definition.projectile_velocity > 1e-6
                    and definition.projectile_lifetime > 1e-6
                ):
                    # A native cast range does not extend a straight bullet
                    # beyond its authored lifetime. Trap shooters often have
                    # AttackRange=100 while their V4/L10 projectile only
                    # travels 40 world units.
                    effective_range = min(
                        definition.attack_range,
                        definition.projectile_velocity
                        * definition.projectile_lifetime
                        + max(0.15, definition.projectile_radius)
                        + PLAYER_PROJECTILE_COLLISION_RADIUS,
                    )
                elif definition.support:
                    # A summon or a self-buff strikes nobody: its HitRange is
                    # no reach, only its AttackRange says when it is cast.
                    effective_range = definition.attack_range
                else:
                    # Melee skills commonly author a smaller AttackRange than
                    # HitRange (Bearach's strike is 4 vs. 5). Let the cast
                    # begin anywhere its authored hit can still connect.
                    effective_range = max(
                        definition.attack_range, definition.hit_range
                    )
                if target_distance > effective_range:
                    continue
                # A bullet that would strike a wall between them is not
                # fired: it flew through it doing nothing.
                if self._monster_shot_blocked(
                    definition, monster_position, attack_target_world
                ):
                    continue
                eligible_definitions_list.append(definition)
            eligible_definitions = tuple(eligible_definitions_list)
            skill_cursor = self._monster_skill_cursors.get(actor_id, 0)
            definition = self._monster_skill_choice(
                actor_id,
                definitions,
                eligible_definitions,
                skill_cursor,
                current_time,
            )
            if definition is None:
                continue
            # The previous cast owns the recovery lock checked above. Using
            # the newly selected skill's duration here starves long specials:
            # every intervening basic hit restarts the interval they require.
            minimum_attack_interval = 0.1
            if not is_boss:
                minimum_attack_interval = max(
                    minimum_attack_interval, monster.attack_interval
                )
            if (
                current_time - self.last_monster_attack_at[actor_id]
                + 1e-9
                < minimum_attack_interval
            ):
                continue
            dx = attack_target_world[0] - monster_position[0]
            dy = attack_target_world[1] - monster_position[1]
            dz = attack_target_world[2] - monster_position[2]
            direction_length = math.sqrt(dx * dx + dy * dy + dz * dz)
            direction = (
                (
                    dx / direction_length,
                    dy / direction_length,
                    dz / direction_length,
                    0.0,
                )
                if direction_length > 1e-6
                else (0.0, 0.0, -1.0, 0.0)
            )
            yaw = (
                math.atan2(-dx, -dz)
                if math.hypot(dx, dz) > 1e-6
                else self.monster_yaws[actor_id]
            )
            self.monster_yaws[actor_id] = yaw
            start_tick = int(server_tick) & 0xFFFFFFFF
            if definition.command_id in {
                BULLET_SKILL_COMMAND,
                TARGET_BULLET_SKILL_COMMAND,
            }:
                # Projectile impact is emit phase plus world-space flight.
                # HitFrame describes the animation's melee/contact frame and
                # must not be added a second time to a bullet's arrival.
                # Monster command timing in the retail traces is measured
                # actor-center to cast-time target. StartOffset belongs to
                # visual bullet placement and is already applied by the
                # client skill template.
                projectile_origin = monster_position
                travel_distance = direction_length
                # Many ranged monster skills author their visual release at
                # LoopStartFrame while leaving BulletEmitDelay at zero (for
                # example BeastmanStagShot uses frame 21).  Zero must not turn
                # those casts into instant projectiles.
                impact_delay = max(
                    definition.bullet_emit_delay,
                    definition.loop_start_frame
                    / SERVER_TICKS_PER_SECOND,
                )
                release_delay = impact_delay
                if definition.projectile_velocity > 1e-6:
                    impact_delay += travel_distance / (
                        definition.projectile_velocity
                    )
                # Retail serializes the projectile release frame in the
                # common execution context. Ranged templates commonly leave
                # HitFrame at zero and author that frame as LoopStartFrame
                # instead (BeastmanStagShot: 0/21). Sending zero skips the
                # visible cast/release animation even though impact timing is
                # otherwise correct.
                animation_hit_frame = (
                    definition.hit_frame
                    if definition.hit_frame > 0
                    else definition.loop_start_frame
                )
            elif definition.command_id == SHIFTED_SKILL_COMMAND:
                # A Shifted cast: its release at LoopStartFrame (the context's
                # hit frame), then its impact HitFrame later, at least one tick
                # (ShiftedSkillCommand's pre-impact delay; a zero-length phase
                # is a native client assert, presence_visuals).
                projectile_origin = monster_position
                impact_delay = (
                    definition.loop_start_frame + max(1, definition.hit_frame)
                ) / SERVER_TICKS_PER_SECOND
                animation_hit_frame = definition.loop_start_frame
            else:
                projectile_origin = monster_position
                impact_delay = (
                    definition.hit_frame / SERVER_TICKS_PER_SECOND
                )
                animation_hit_frame = definition.hit_frame
            impact_tick = (
                start_tick
                + round(impact_delay * SERVER_TICKS_PER_SECOND)
            ) & 0xFFFFFFFF
            attack = MonsterAttack(
                attacker_actor_id=actor_id,
                target_actor_id=attack_target_actor_id,
                # This legacy wire field is SkillCounter, not the authored
                # skill level. The client rejects a nonzero counter <= the
                # actor's last counter, so a fixed template level plays once
                # and silences every following attack. Autonomous casts use
                # zero, which explicitly bypasses that prediction-counter
                # check (as player-owned autonomous summons already do).
                skill_level=0,
                skill_index=definition.skill_manager_index,
                command_id=definition.command_id,
                aim_angle=yaw,
                server_tick=start_tick,
                # Keep autonomous casts distinguishable to the client. Reusing
                # zero for every attack lets the first animation play but can
                # cause later casts from the same actor to be treated as an
                # already-seen event while server-side damage still resolves.
                cast_id=(start_tick + actor_id) & 0xFFFFFFFF,
                impact_tick=impact_tick,
                hit_frame=animation_hit_frame,
                motion_unblock_frame=definition.motion_unblock_frame,
                skill_unblock_frame=definition.skill_unblock_frame,
                position=(*monster_position, 1.0),
                # A summon is cast on the caster's own place.
                target_position=(
                    monster_position
                    if definition.summon_template
                    else attack_target_world
                ),
                direction=direction,
                projectile_radius=definition.projectile_radius,
                # The recovered retail autonomous BulletSkill uses variant 1.
                # TargetSkill does not serialize this field.
                variant=1,
                due_at=current_time + impact_delay,
                projectile_origin=projectile_origin,
                maximum_projectile_range=(
                    definition.projectile_velocity
                    * definition.projectile_lifetime
                ),
                motion_type=definition.motion_type,
                user_status_effects=tuple(
                    effect
                    for effect in self._load_monster_user_effects(
                        definition
                    )
                    if (
                        effect.trigger in {"", "skillstart"}
                        and self._effect_applies(
                            effect,
                            cast_id=start_tick + actor_id * 257,
                            killed=False,
                        )
                    )
                ),
                definition=definition,
                bullet_count=definition.bullet_count,
            )
            if (
                attack.command_id in {
                    BULLET_SKILL_COMMAND,
                    TARGET_BULLET_SKILL_COMMAND,
                }
                and definition.motion_type.casefold() in HOMING_MOTION_TYPES
                and definition.projectile_velocity > 1e-6
                and definition.projectile_lifetime > 1e-6
                and not definition.support
            ):
                # A homing bullet is flown, not timed: it leaves at its
                # release and lands when it reaches its target, which may
                # outrun it until its LifeTime (trunc(LifeTime x 25) ticks,
                # as the client flies it) runs out. Timed from the cast-time
                # distance, Khalys's orbs struck a player running from them
                # while they were still metres behind (2026-10-23).
                attack = replace(
                    attack,
                    due_at=current_time + release_delay,
                    cast_at=current_time,
                    homing_expires_at=(
                        current_time + release_delay
                        + max(1.0, math.trunc(
                            definition.projectile_lifetime
                            * SERVER_TICKS_PER_SECOND
                        )) / SERVER_TICKS_PER_SECOND
                    ),
                )
            if (
                attack.command_id == SHIFTED_SKILL_COMMAND
                and definition.bullet_count > 1
                and not definition.support
                and definition.targeting_type.casefold()
                in MONSTER_GROUND_TARGETING_TYPES
                and attack_target_actor_id not in self.monsters
            ):
                points = self._monster_shifted_points(
                    actor_id,
                    definition,
                    monster_position,
                    attack_target_actor_id,
                    attack_target_world,
                    attack.cast_id,
                    enemy_positions,
                )
                attack = replace(
                    attack,
                    target_position=points[0],
                    extra_target_positions=points[1:],
                )
            self.last_monster_attack_at[actor_id] = current_time
            self.monster_skill_unblock_at[actor_id] = (
                current_time + attack.skill_unblock_frame / SERVER_TICKS_PER_SECOND
            )
            if attack.motion_unblock_frame > 0:
                self.monster_motion_unblock_at[actor_id] = max(
                    self.monster_motion_unblock_at.get(actor_id, 0.0),
                    current_time + attack.motion_unblock_frame / SERVER_TICKS_PER_SECOND,
                )
                # The reliable cast batch publishes this exact stationary
                # transform, superseding any chase snapshot from this tick.
                self.monster_moving[actor_id] = False
                self.monster_routes.pop(actor_id, None)
                self.monster_last_movement_publish_ticks[actor_id] = start_tick
                self.monster_last_published_positions[actor_id] = monster_position
            self._monster_skill_cursors[actor_id] = skill_cursor + 1
            if definition.cooldown > 0.0:
                self._monster_skill_ready_at.setdefault(actor_id, {})[
                    _cooldown_key(definition)
                ] = current_time + definition.cooldown
            if definition.self_buff and attack.user_status_effects:
                # Its effects (enrage: speed, skill speed, immunities) are on
                # the caster from its start; clients get them with the cast
                # (monster-skillstart-status-effects).
                self._record_monster_status_effects(
                    actor_id,
                    attack.user_status_effects,
                    now=current_time,
                    source_actor_id=actor_id,
                )
            self._start_monster_beam_auras(
                actor_id, attack, monster_position, yaw, current_time,
            )
            self._queue_monster_status_minions(
                actor_id, attack, monster_position, yaw, current_time,
            )
            if tower_fire_burst or boss_projectile_in_flight:
                previous = self.pending_monster_attacks.get(actor_id)
                if previous is not None:
                    self.pending_monster_projectile_attacks.setdefault(
                        actor_id, []
                    ).append(previous)
            if tower_fire_burst:
                remaining = self.tower_fire_burst_remaining.get(actor_id)
                if remaining is None:
                    self.tower_fire_burst_remaining[actor_id] = (
                        TOWER_FIRE_BURST_SHOTS - 1
                    )
                elif remaining <= 1:
                    self.tower_fire_burst_remaining.pop(actor_id, None)
                    self.tower_fire_burst_cooldown_until[actor_id] = (
                        current_time + TOWER_FIRE_BURST_COOLDOWN
                    )
                else:
                    self.tower_fire_burst_remaining[actor_id] = remaining - 1
            self.pending_monster_attacks[actor_id] = attack
            if (
                attack.command_id in {
                    BULLET_SKILL_COMMAND,
                    TARGET_BULLET_SKILL_COMMAND,
                }
                and definition.bullet_count > 1
                and definition.bullet_emit_delay > 0.0
                and not definition.support
            ):
                # Every bullet of a volley lands on its own: bullet k leaves
                # k x BulletEmitDelay after the first, from the same place
                # and on the same aim, and flies as long (Boss01Skill02's
                # three homing orbs 0.25 s apart, Medusa's two snakes 1 s
                # apart). The client draws them so (skills.rs spawn loop,
                # BulletEmitDelay apart: 0x8F0ADA). ImpactCount and
                # ReproductionCount are about several actors per bullet, not
                # repeats, and are left alone.
                for bullet in range(1, definition.bullet_count):
                    emit_offset = bullet * definition.bullet_emit_delay
                    self.pending_monster_projectile_attacks.setdefault(
                        actor_id, []
                    ).append(replace(
                        attack,
                        due_at=attack.due_at + emit_offset,
                        homing_expires_at=(
                            attack.homing_expires_at + emit_offset
                            if attack.homing_expires_at
                            else 0.0
                        ),
                        impact_tick=(
                            attack.impact_tick
                            + round(emit_offset * SERVER_TICKS_PER_SECOND)
                        ) & 0xFFFFFFFF,
                    ))
            for point in attack.extra_target_positions:
                # Each point of a multi-bullet Shifted cast is its own ground
                # hit, due with the first (one HitFrame for every bullet).
                self.pending_monster_projectile_attacks.setdefault(
                    actor_id, []
                ).append(replace(
                    attack, target_position=point, extra_target_positions=(),
                ))
            if (
                attack.user_status_effects
                and attack_target_actor_id not in self.monsters
            ):
                # SEE: MonsterAuraStrike. The aura's DE counts from the hit
                # frame (Heredur's second blow: 26 + 19 = frame 45, its
                # sequence's second shake at 44).
                strikes = self._monster_aura_strikes_for(definition)
                for effect in attack.user_status_effects:
                    strike = strikes.get(effect.resource_id)
                    if strike is None:
                        continue
                    delay_ticks = (
                        max(0, int(attack.hit_frame)) + strike.delay_ticks
                    )
                    self.pending_monster_projectile_attacks.setdefault(
                        actor_id, []
                    ).append(replace(
                        attack,
                        due_at=current_time
                        + delay_ticks / SERVER_TICKS_PER_SECOND,
                        impact_tick=(start_tick + delay_ticks) & 0xFFFFFFFF,
                        aura_strike=strike,
                    ))
            attacks.append(attack)
            next_cursor = (
                start_index + offset + 1
            ) % len(living_actor_ids)
            if len(attacks) >= MONSTER_ATTACK_START_BUDGET:
                break
        self._monster_attack_cursor = next_cursor
        return tuple(attacks)

    def _monster_shifted_points(
        self,
        actor_id: int,
        definition: MonsterAttackDefinition,
        monster_position: tuple[float, float, float],
        target_actor_id: int,
        target_world: tuple[float, float, float],
        cast_id: int,
        enemy_positions: dict[int, tuple[float, float, float]] | None,
    ) -> tuple[tuple[float, float, float], ...]:
        """Where a multi-bullet Shifted monster cast falls: one point each.

        CONTRACT: TargetingType RandomPosition: BulletCount points drawn
        uniformly within AttackRange of the caster, set on walkable ground,
        the same for the same cast. Radius/Point/Location: one point on the
        target, then one on each other living player within AttackRange of
        the caster, nearest first, at most BulletCount; never two on one
        player, so a lone player takes one meteor as before.
        EVIDENCE (data, static.db4 _Template_Skill): the skills come in
        pairs, a Radius one aimed at players and a RandomPosition one
        (e_dra_mystra_invisible_meteor_target / _random,
        e_ks_boss_eye_multi_meteor / _random_meteor_hidden,
        e_ks_01_cager_stunshot / _random, ShadowfatherPortalShockWave /
        Random); Radius ones mostly author BulletCount 5, a group's size, down
        to random monster enchantments (RME_cast_lightning_strike); their
        bullet sequences draw a single meteor at the point
        (boss_02_herald_skill_04_bullet, e_zahir_horseman_multimeteor_single_
        bullet), so N equal points stacked N meteors on one spot.
        UNVERIFIED: the retail server's own choice of points.
        """

        count = max(1, int(definition.bullet_count))
        if definition.targeting_type.casefold() == "randomposition":
            reach = max(0.0, float(definition.attack_range))
            points = []
            for index in range(count):
                angle = 2.0 * math.pi * deterministic_combat_roll(
                    0x52414E44, int(cast_id), int(actor_id), index, 0,
                )
                radius = reach * math.sqrt(deterministic_combat_roll(
                    0x52414E44, int(cast_id), int(actor_id), index, 1,
                ))
                point = (
                    monster_position[0] + radius * math.sin(angle),
                    monster_position[1],
                    monster_position[2] + radius * math.cos(angle),
                )
                try:
                    point = tuple(
                        float(value)
                        for value in (
                            self.encounter.navigation
                            .project_position_relative(
                                point, monster_position,
                            )
                        )
                    )
                except (
                    AttributeError,
                    NavigationError,
                    CollisionTerrainError,
                    ValueError,
                ):
                    pass
                points.append(point)
            return tuple(points)
        others = sorted(
            (distance, int(other_id), tuple(float(v) for v in position[:3]))
            for other_id, position in (enemy_positions or {}).items()
            if (
                int(other_id) != int(target_actor_id)
                and int(other_id) not in self.monsters
                and self.player_health.get(int(other_id), [1])[0] > 0
                and (
                    distance := self._distance(position, monster_position)
                ) <= definition.attack_range
            )
        )
        return (
            tuple(float(value) for value in target_world[:3]),
            *(position for _distance, _id, position in others[:count - 1]),
        )

    def _monster_skill_choice(
        self,
        actor_id: int,
        definitions,
        eligible_definitions,
        skill_cursor: int,
        current_time: float,
    ) -> MonsterAttackDefinition | None:
        """Choose a difficulty-valid, cooled-down monster skill."""

        ready_at = self._monster_skill_ready_at.get(actor_id)
        if ready_at is None:
            ready_at = {}
            for definition in definitions:
                low, high = definition.initial_cooldown
                if high <= 0.0:
                    continue
                key = definition.cooldown_category or definition.resource_id
                wait = self._monster_cooldown_random.uniform(low, high)
                ready_at[key] = max(
                    ready_at.get(key, 0.0), current_time + wait
                )
            self._monster_skill_ready_at[actor_id] = ready_at

        difficulty = int(getattr(self.encounter, "scaling_difficulty", 0))
        alive_ids = set(self.living_actor_ids)
        living_summons = len(
            self.monster_summoned_actor_ids.get(actor_id, set()) & alive_ids
        )
        monster = self.monsters.get(actor_id)
        is_boss = bool(
            monster
            and str(monster.rank_id or "").casefold().startswith("rank_boss")
        )
        primary = definitions[0] if definitions else None
        is_melee_primary = (
            primary is not None
            and primary.command_id in {SKILL_COMMAND, TARGET_SKILL_COMMAND, 0x49}
            and primary.attack_range <= 8.0
            and not primary.self_buff
            and not primary.summon_template
        )
        # A monster's first skill is not necessarily its basic attack. Some
        # bosses list a long-range special first (for example Balor's
        # player-targeted cast), which can otherwise suppress their melee
        # attack after direct travel to the map. Identify close, damaging
        # actor-targeted melee skills independently of their list position.
        basic_melees = tuple(
            definition
            for definition in eligible_definitions
            if is_boss
            and BOSS_BASIC_ATTACK_ROTATION
            and self._is_basic_monster_melee(definition)
        )
        basic_melee_ids = {id(definition) for definition in basic_melees}

        usable = tuple(
            definition
            for definition in eligible_definitions
            if definition.min_difficulty <= difficulty <= definition.max_difficulty
            and (
                id(definition) in basic_melee_ids
                or (not is_boss and is_melee_primary and definition is primary)
                or current_time + 1e-9 >= ready_at.get(
                    definition.cooldown_category or definition.resource_id, 0.0
                )
            )
            and not (
                definition.summon_template
                and definition.summon_limit > 0
                and living_summons + definition.summon_amount
                > definition.summon_limit
            )
        )
        if not usable:
            return None

        # A boss's summons and self-buffs come as soon as their cooldown
        # allows (their InitialCoolDown first: Heredur's skeletons after a few
        # seconds), outside its rotation below; the one ready the longest.
        supports = tuple(
            definition
            for definition in usable
            if is_boss
            and BOSS_BASIC_ATTACK_ROTATION
            and definition.support
            and definition.cooldown > 0.0
        )
        if supports:
            return min(
                supports,
                key=lambda definition: ready_at.get(_cooldown_key(definition), 0.0),
            )

        specials = tuple(
            skill
            for skill in usable
            if (
                skill not in basic_melees
                and skill.cooldown > 0.0
            )
        )

        available_basic_melees = tuple(
            definition
            for definition in basic_melees
            if definition in usable
        )
        if available_basic_melees:
            # Reserve every third boss attack selection for a ready special,
            # while keeping a close basic hit in the rotation even when its
            # authored cooldown would otherwise leave only ranged abilities.
            if specials and skill_cursor % 3 == 2:
                special = min(
                    specials,
                    key=lambda skill: ready_at.get(
                        skill.cooldown_category or skill.resource_id, 0.0
                    ),
                )
                return special
            return min(
                available_basic_melees,
                key=lambda skill: (
                    skill.cooldown,
                    max(skill.attack_range, skill.hit_range),
                    skill.skill_manager_index,
                ),
            )

        if specials:
            return min(
                specials,
                key=lambda skill: ready_at.get(
                    skill.cooldown_category or skill.resource_id, 0.0
                ),
            )
        return usable[skill_cursor % len(usable)]

    @staticmethod
    def _is_basic_monster_melee(
        definition: MonsterAttackDefinition,
    ) -> bool:
        """Whether this is a close, damaging, actor-targeted melee skill."""

        return (
            definition.command_id in {SKILL_COMMAND, TARGET_SKILL_COMMAND}
            and max(definition.attack_range, definition.hit_range) <= 12.0
            and definition.cooldown <= 2.0
            and definition.damage_modifier > 0.0
            and not definition.self_buff
            and not definition.summon_template
        )

    @staticmethod
    def _fixed_projectile_intersects_player(
        attack: MonsterAttack,
        player_position: tuple[float, float, float],
    ) -> bool:
        """Test the player capsule against a fixed cast-time trajectory."""

        origin_x, origin_y, origin_z = attack.projectile_origin
        direction_x = float(attack.direction[0])
        direction_z = float(attack.direction[2])
        direction_length = math.hypot(direction_x, direction_z)
        if direction_length <= 1e-6:
            return False
        direction_x /= direction_length
        direction_z /= direction_length
        target_dx = attack.target_position[0] - origin_x
        target_dz = attack.target_position[2] - origin_z
        path_length = math.hypot(target_dx, target_dz)
        maximum_range = max(0.0, float(attack.maximum_projectile_range))
        projectile_distance = math.dist(
            attack.projectile_origin, attack.target_position
        )
        collision_allowance = (
            max(0.15, float(attack.projectile_radius))
            + PLAYER_PROJECTILE_COLLISION_RADIUS
        )
        if (maximum_range > 0.0
                and projectile_distance > maximum_range + collision_allowance):
            return False
        player_dx = player_position[0] - origin_x
        player_dz = player_position[2] - origin_z
        along = player_dx * direction_x + player_dz * direction_z
        collision_radius = (
            max(0.15, float(attack.projectile_radius))
            + PLAYER_PROJECTILE_COLLISION_RADIUS
        )
        if (
            along < -collision_radius
            or along > path_length + collision_radius
        ):
            return False
        lateral = abs(
            player_dx * direction_z - player_dz * direction_x
        )
        if lateral > collision_radius:
            return False
        # The lane's height where it meets the player: from the origin's height
        # to the cast-time target's, along the flight.
        fraction = (
            min(1.0, max(0.0, along / path_length)) if path_length > 1e-6 else 1.0
        )
        lane_y = origin_y + (attack.target_position[1] - origin_y) * fraction
        return (
            player_position[1] - PROJECTILE_VERTICAL_SLACK
            <= lane_y
            <= player_position[1] + PLAYER_CAPSULE_HEIGHT + PROJECTILE_VERTICAL_SLACK
        )

    @staticmethod
    def _directional_skill_intersects_player(
        attack: MonsterAttack,
        player_position: tuple[float, float, float],
        definition: MonsterAttackDefinition,
    ) -> bool:
        """Test cast-time cone/angle geometry at the player's impact position."""

        origin_x, _origin_y, origin_z = attack.projectile_origin
        direction_x = float(attack.direction[0])
        direction_z = float(attack.direction[2])
        direction_length = math.hypot(direction_x, direction_z)
        if direction_length <= 1e-6:
            return False
        direction_x /= direction_length
        direction_z /= direction_length

        dx = player_position[0] - origin_x
        dz = player_position[2] - origin_z
        distance = math.hypot(dx, dz)
        forward = dx * direction_x + dz * direction_z
        collision_radius = PLAYER_PROJECTILE_COLLISION_RADIUS
        maximum_range = max(
            0.0,
            float(definition.attack_range),
            float(definition.hit_range),
        )
        if (
            forward < -collision_radius
            or distance > maximum_range + collision_radius
        ):
            return False

        lateral = abs(dx * direction_z - dz * direction_x)
        targeting_type = definition.targeting_type.casefold()
        if targeting_type == "angle":
            half_width = max(0.0, forward) * math.tan(
                math.radians(definition.angle_degrees / 2.0)
            )
        else:
            progress = min(
                1.0,
                max(0.0, forward) / max(maximum_range, 1e-6),
            )
            half_width = (
                definition.player_width / 2.0
                + (definition.end_width - definition.player_width)
                / 2.0
                * progress
            )
        return lateral <= max(0.0, half_width) + collision_radius

    def pending_monster_attack_items(self) -> tuple[tuple[int, MonsterAttack], ...]:
        """Snapshot active casts and independently travelling projectiles."""

        return (
            *self.pending_monster_attacks.items(),
            *((actor_id, attack)
              for actor_id, flights in self.pending_monster_projectile_attacks.items()
              for attack in flights),
        )

    def _remove_pending_monster_attack(
        self, actor_id: int, attack: MonsterAttack,
    ) -> None:
        actor_id = int(actor_id)
        if self.pending_monster_attacks.get(actor_id) is attack:
            self.pending_monster_attacks.pop(actor_id, None)
            return
        flights = self.pending_monster_projectile_attacks.get(actor_id)
        if flights is None:
            return
        for index, pending in enumerate(flights):
            if pending is attack:
                del flights[index]
                break
        if not flights:
            self.pending_monster_projectile_attacks.pop(actor_id, None)

    def _replace_pending_monster_attack(
        self, actor_id: int, attack: MonsterAttack, updated: MonsterAttack,
    ) -> None:
        actor_id = int(actor_id)
        if self.pending_monster_attacks.get(actor_id) is attack:
            self.pending_monster_attacks[actor_id] = updated
            return
        flights = self.pending_monster_projectile_attacks.get(actor_id, [])
        for index, pending in enumerate(flights):
            if pending is attack:
                flights[index] = updated
                return

    def _advance_homing_bullet(
        self,
        actor_id: int,
        attack: MonsterAttack,
        target_world: tuple[float, float, float],
        now: float,
    ) -> float | None:
        """Fly a homing bullet up to ``now``; the time it reached its target.

        It chases the target's current place at its Velocity, flat, one
        server tick at a time (the client's steer_bullet re-aims it every
        frame the same way), and reaches it within BulletRadius + the
        player's collision radius. ``None`` while it still flies, or once its
        LifeTime ran out (it is then dropped: a target that outran it takes
        nothing). Once released it flies on its own, so its caster may act
        again.
        UNVERIFIED: the 2018 client's homing step (turn rate, if any) was not
        traced; a pure pursuit at constant speed matches what the client draws.
        """

        definition = attack.definition or self.monsters[actor_id].attack
        speed = max(1e-6, float(definition.projectile_velocity))
        reach = (
            max(0.15, float(attack.projectile_radius))
            + PLAYER_PROJECTILE_COLLISION_RADIUS
        )
        x, _y, z = (
            attack.homing_position
            if attack.homing_position is not None
            else attack.projectile_origin[:3]
        )
        t = max(attack.due_at, attack.homing_at)
        end = min(float(now), attack.homing_expires_at)
        step = 1.0 / SERVER_TICKS_PER_SECOND
        reached_at = None
        while True:
            dx = target_world[0] - x
            dz = target_world[2] - z
            distance = math.hypot(dx, dz)
            if distance <= reach:
                reached_at = t
                break
            if t >= end - 1e-9:
                break
            dt = min(step, end - t)
            travel = speed * dt
            if travel >= distance - reach:
                reached_at = t + (distance - reach) / speed
                break
            x += dx / distance * travel
            z += dz / distance * travel
            t += dt
        if reached_at is not None:
            return reached_at
        if now >= attack.homing_expires_at:
            self._remove_pending_monster_attack(actor_id, attack)
            return None
        updated = replace(
            attack, homing_position=(x, target_world[1], z), homing_at=t,
        )
        if self.pending_monster_attacks.get(int(actor_id)) is attack:
            del self.pending_monster_attacks[int(actor_id)]
            self.pending_monster_projectile_attacks.setdefault(
                int(actor_id), []
            ).append(updated)
        else:
            self._replace_pending_monster_attack(actor_id, attack, updated)
        return None

    def _mark_monster_area_target_resolved(
        self, actor_id: int, attack: MonsterAttack, target_actor_id: int,
    ) -> None:
        """Record one struck target on an area attack, where it is held."""

        actor_id = int(actor_id)
        updated = replace(
            attack,
            resolved_target_actor_ids=(
                *attack.resolved_target_actor_ids, int(target_actor_id),
            ),
        )
        for index, pending in enumerate(
            self.pending_monster_projectile_attacks.get(actor_id, ())
        ):
            if pending is attack:
                self.pending_monster_projectile_attacks[actor_id][index] = (
                    updated
                )
                return
        self.pending_monster_attacks[actor_id] = updated

    def resolve_due_monster_attacks(
        self,
        player_position: tuple[float, float, float],
        *,
        now: float | None = None,
        player_actor_id: int | None = None,
        incoming_damage_multiplier: (
            float | dict[str, float] | tuple[tuple[str, float], ...]
        ) = 1.0,
        block_chance: float = 0.0,
        block_damage_reduction: float = 0.0,
        finalize_area_attacks: bool = False,
    ) -> tuple[DamageResolution, ...]:
        """Apply autonomous skill damage if the target remains in range."""

        current_time = time.perf_counter() if now is None else float(now)
        player_actor_id = (
            self.player_actor_id
            if player_actor_id is None
            else int(player_actor_id)
        )
        player_world = self.encounter.coordinate_frame.wire_to_world(
            player_position
        )
        resolutions = []
        for actor_id, attack in self.pending_monster_attack_items():
            if current_time < attack.due_at:
                continue
            if (
                self.monster_health.get(actor_id, 0) <= 0
                or actor_id in self.returning_home_actor_ids
            ):
                self.pending_monster_attacks.pop(actor_id, None)
                self.pending_monster_projectile_attacks.pop(actor_id, None)
                continue
            monster_position = self.monster_positions[actor_id]
            monster = self.monsters[actor_id]
            definition = attack.definition or monster.attack
            targets_monster = attack.target_actor_id in self.monsters
            target_world = (
                self.monster_positions[attack.target_actor_id]
                if targets_monster
                else player_world
            )
            is_directional_shape = (
                attack.command_id not in {
                    BULLET_SKILL_COMMAND,
                    TARGET_BULLET_SKILL_COMMAND,
                }
                and definition.targeting_type.casefold() in {"angle", "cone"}
            )
            is_ground_target_area = (
                attack.command_id == SHIFTED_SKILL_COMMAND
                and definition.targeting_type.casefold()
                in MONSTER_GROUND_TARGETING_TYPES
            )
            aura_strike = attack.aura_strike
            is_multi_target_area = (
                (
                    (is_directional_shape or is_ground_target_area)
                    and not definition.self_buff
                )
                or aura_strike is not None
            ) and not targets_monster
            homing = (
                attack.homing_expires_at > 0.0 and aura_strike is None
            )
            if (
                not targets_monster
                and attack.target_actor_id != player_actor_id
                and not is_multi_target_area
            ):
                if (
                    homing
                    and current_time
                    > attack.homing_expires_at + HOMING_ORPHAN_GRACE_SECONDS
                ):
                    # Its target left: it ran out with nobody flying it.
                    self._remove_pending_monster_attack(actor_id, attack)
                continue
            if (
                is_multi_target_area
                and player_actor_id in attack.resolved_target_actor_ids
            ):
                continue
            if homing:
                reached_at = self._advance_homing_bullet(
                    actor_id, attack, target_world, current_time
                )
                if reached_at is None:
                    continue
                self._remove_pending_monster_attack(actor_id, attack)
                attack = replace(
                    attack,
                    impact_tick=(
                        int(attack.server_tick)
                        + round(
                            (reached_at - attack.cast_at)
                            * SERVER_TICKS_PER_SECOND
                        )
                    ) & 0xFFFFFFFF,
                )
            if not is_multi_target_area:
                self._remove_pending_monster_attack(actor_id, attack)
            if (
                aura_strike is None
                and attack.definition is not None
                and attack.definition.summon_template
                and attack.target_actor_id == player_actor_id
            ):
                self.pending_monster_summons.append(MonsterSummonRequest(
                    caster_actor_id=int(actor_id),
                    template_id=attack.definition.summon_template,
                    amount=int(attack.definition.summon_amount),
                    limit=int(attack.definition.summon_limit),
                    position=tuple(float(v) for v in monster_position[:3]),
                    level=int(monster.level),
                ))
            if (
                aura_strike is None
                and attack.definition is not None
                and attack.definition.self_buff
            ):
                continue
            if aura_strike is None and definition.damage_modifier <= 0.0:
                continue
            if (
                targets_monster
                and self.monster_health.get(attack.target_actor_id, 0) <= 0
            ):
                continue
            if not targets_monster and self.player_health.get(
                player_actor_id, [self.player_maximum_health]
            )[0] <= 0:
                # Several impacts can be due in one tick. Only the first
                # lethal hit owns the death transition; discard later hits.
                continue
            if aura_strike is not None:
                # Centred on the caster where it stands when the blow lands.
                if (
                    self._distance(target_world, monster_position)
                    > aura_strike.radius + PLAYER_PROJECTILE_COLLISION_RADIUS
                ):
                    continue
            elif attack.command_id in {
                BULLET_SKILL_COMMAND,
                TARGET_BULLET_SKILL_COMMAND,
            }:
                if (
                    attack.motion_type == "Straight"
                    and not self._fixed_projectile_intersects_player(
                        attack, target_world
                    )
                ):
                    continue
                if (
                    getattr(definition, "static_collision", False)
                    and attack.motion_type.casefold()
                    not in ORBITING_MOTION_TYPES
                ):
                    # The bullet struck a wall between caster and target.
                    reach = self._bullet_static_reach(definition.start_offset)
                    if reach is not None and reach(
                        attack.projectile_origin, target_world
                    ) + PLAYER_PROJECTILE_COLLISION_RADIUS < math.dist(
                        attack.projectile_origin[:3], target_world[:3]
                    ):
                        continue
            elif is_directional_shape:
                if not self._directional_skill_intersects_player(
                    attack, target_world, definition
                ):
                    continue
            elif is_ground_target_area:
                if (
                    self._distance(target_world, attack.target_position)
                    > max(0.0, definition.hit_range)
                    + PLAYER_PROJECTILE_COLLISION_RADIUS
                ):
                    continue
            elif (
                self._distance(target_world, monster_position)
                > definition.hit_range
                or abs(target_world[1] - monster_position[1])
                > MONSTER_MELEE_VERTICAL_REACH
            ):
                continue
            if isinstance(incoming_damage_multiplier, (int, float)):
                defense_multiplier = max(
                    0.0, float(incoming_damage_multiplier)
                )
            else:
                defense_by_type = {
                    str(name).casefold(): max(0.0, float(value))
                    for name, value in dict(
                        incoming_damage_multiplier
                    ).items()
                }
                weighted = (
                    tuple(
                        (damage_type, coefficient)
                        for _kind, coefficient, damage_type
                        in aura_strike.damage_programs
                    )
                    if aura_strike is not None
                    else tuple(definition.damage_types)
                )
                total_weight = sum(
                    max(0.0, float(weight)) for _name, weight in weighted
                )
                defense_multiplier = (
                    sum(
                        max(0.0, float(weight))
                        * defense_by_type.get(
                            (
                                "darkmagic"
                                if str(name).casefold() == "andermagic"
                                else str(name).casefold()
                            ),
                            1.0,
                        )
                        for name, weight in weighted
                    ) / total_weight
                    if total_weight > 0.0
                    else 1.0
                )
            outgoing_multiplier = self._monster_skill_damage_multiplier(
                actor_id, now=current_time
            )
            if self._monster_is_mind_controlled(actor_id, now=current_time):
                controlled_effect = next(
                    (
                        effect
                        for effect in self._active_monster_modifier_sources(
                            actor_id, now=current_time
                        )
                        if effect.resource_id == "skill_mindcontrol_effect"
                    ),
                    None,
                )
                outgoing_multiplier += (
                    float(controlled_effect.values[0])
                    if (
                        controlled_effect is not None
                        and controlled_effect.values
                    )
                    else 1.0
                )
            raw_damage = (
                (monster.minimum_damage + monster.maximum_damage) / 2
                * outgoing_multiplier
                * defense_multiplier
                * (
                    sum(
                        coefficient
                        for _kind, coefficient, _damage_type
                        in aura_strike.damage_programs
                    )
                    if aura_strike is not None
                    else max(0.0, float(definition.damage_modifier))
                )
            )
            # Some authored fixed shooters omit MinDamage/MaxDamage. Their
            # visual projectile still hits, so do not silently turn that hit
            # into zero damage on the server.
            if (
                monster.invulnerable
                and "fire_shooter" in monster.template_id.casefold()
                and is_tower_mechanism(
                    self.encounter.static_database_path,
                    self.encounter.map_id,
                    monster.template_id,
                )
            ):
                raw_damage = max(1.0, raw_damage)
            damage = (
                max(1, int(round(raw_damage)))
                if raw_damage > 0.0
                else 0
            )
            if aura_strike is not None and not targets_monster:
                # Its own programs: x the damage basis, a share of the
                # player's maximum health, or an absolute amount.
                damage = self._monster_aura_program_damage(
                    actor_id,
                    aura_strike.damage_programs,
                    self.player_health.get(
                        player_actor_id,
                        [self.player_maximum_health,
                         self.player_maximum_health],
                    )[1],
                    incoming_damage_multiplier,
                    current_time,
                )
            if damage <= 0:
                continue
            if targets_monster:
                target = self.monsters[attack.target_actor_id]
                previous = self.monster_health[attack.target_actor_id]
                current, killed = _monster_health_after_damage(
                    target, previous, damage
                )
                if damage <= 0:
                    continue
                self.monster_health[attack.target_actor_id] = current
                victim_effects = tuple(
                    definition
                    for definition in self._load_monster_attack_effects(
                        definition
                    )
                    if self._effect_applies(
                        definition,
                        cast_id=(
                            int(attack.server_tick) + int(actor_id) * 257
                        ),
                        killed=killed,
                    )
                )
                if not killed and victim_effects:
                    self._record_monster_status_effects(
                        attack.target_actor_id,
                        victim_effects,
                        now=current_time,
                    )
                resolutions.append(DamageResolution(
                    target_actor_id=attack.target_actor_id,
                    attacker_actor_id=actor_id,
                    current_health=current,
                    maximum_health=target.maximum_health,
                    damage=damage,
                    killed=killed,
                    position=target_world,
                    server_tick=attack.impact_tick,
                    hit_tags=(0,),
                    skill_index=attack.skill_index,
                    victim_status_effects=victim_effects,
                ))
                continue
            health = self.player_health.setdefault(
                player_actor_id,
                [self.player_maximum_health, self.player_maximum_health],
            )
            previous, maximum_health = health
            self._player_block_roll_sequence = (
                self._player_block_roll_sequence + 1
            ) & _COMBAT_ROLL_MASK
            block_roll = deterministic_combat_roll(
                0x424C4F43,  # "BLOC" domain separator
                self._player_block_roll_sequence,
                int(attack.impact_tick),
                int(actor_id),
                int(player_actor_id),
                int(attack.skill_index),
            )
            blocked = (
                float(block_chance) > 0.0
                and block_roll
                < max(0.0, min(1.0, float(block_chance)))
            )
            if blocked:
                damage = max(
                    0,
                    int(round(
                        damage * (
                            1.0
                            - max(
                                0.0,
                                min(1.0, float(block_damage_reduction)),
                            )
                        )
                    )),
                )
            current = max(0, previous - damage)
            damage = previous - current
            if damage <= 0 and not blocked:
                if is_multi_target_area:
                    self._mark_monster_area_target_resolved(
                        actor_id, attack, player_actor_id
                    )
                continue
            self.note_player_attacked(player_actor_id, now=current_time)
            health[0] = current
            if player_actor_id == self.player_actor_id:
                self.player_current_health = current
            resolutions.append(
                DamageResolution(
                    target_actor_id=player_actor_id,
                    attacker_actor_id=actor_id,
                    current_health=current,
                    maximum_health=maximum_health,
                    damage=damage,
                    killed=current == 0,
                    position=player_world,
                    server_tick=attack.impact_tick,
                    hit_tags=(0,),
                    skill_index=attack.skill_index,
                    # The skill's victim effects ride its own hit; an aura
                    # strike's child carries only its damage.
                    victim_status_effects=() if aura_strike is not None
                    else tuple(
                        definition
                        for definition in self._load_monster_attack_effects(
                            definition
                        )
                        if self._effect_applies(
                            definition,
                            cast_id=(
                                int(attack.server_tick)
                                + int(actor_id) * 257
                            ),
                            killed=current == 0,
                        )
                    ),
                    blocked=blocked,
                )
            )
            if is_multi_target_area:
                self._mark_monster_area_target_resolved(
                    actor_id, attack, player_actor_id
                )
        if finalize_area_attacks:
            for actor_id, flights in tuple(
                self.pending_monster_projectile_attacks.items()
            ):
                for attack in tuple(flights):
                    definition = (
                        attack.definition or self.monsters[actor_id].attack
                    )
                    if attack.due_at <= current_time and (
                        attack.aura_strike is not None
                        or (
                            attack.command_id == SHIFTED_SKILL_COMMAND
                            and definition.targeting_type.casefold()
                            in MONSTER_GROUND_TARGETING_TYPES
                        )
                    ):
                        self._remove_pending_monster_attack(actor_id, attack)
            for actor_id, attack in tuple(self.pending_monster_attacks.items()):
                definition = attack.definition or self.monsters[actor_id].attack
                if (
                    attack.due_at <= current_time
                    and definition.targeting_type.casefold()
                    in {"angle", "cone", *MONSTER_GROUND_TARGETING_TYPES}
                    and attack.command_id not in {
                        BULLET_SKILL_COMMAND,
                        TARGET_BULLET_SKILL_COMMAND,
                    }
                ):
                    self._remove_pending_monster_attack(actor_id, attack)
        return tuple(resolutions)
