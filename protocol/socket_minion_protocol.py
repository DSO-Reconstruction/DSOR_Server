"""Cached construction metadata for minions spawned by socket effects."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import sqlite3

from dso_server.gameplay.player_summon_lifecycle import (
    load_player_summon_aura,
    summon_startup_seconds,
)


SERVER_TICKS_PER_SECOND = 25.0
SOCKET_SUMMON_OWNER_RANGE = 40.0


@dataclass(frozen=True)
class SocketMinionDefinition:
    """Duck-compatible ``PlayerSummonDefinition`` for a status modifier."""

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
    aura: object | None
    location_visual_effects: tuple = ()
    move_speed: float = 0.0
    aggro_range: float = 0.0
    heal_fraction: float = 0.0
    heal_radius: float = 0.0
    heal_interval_seconds: float = 1.0
    taunt_radius: float = 0.0
    support_status_effects: tuple = ()
    stationary: bool = False
    startup_duration_seconds: float = 0.0
    follows_owner: bool = False
    map_companion: bool = False
    fixed_direction: bool = False
    patrol_points: tuple[tuple[float, float, float], ...] = ()
    overheat_attack_skill_database_index: int | None = None
    overheat_attack_skill_type: str = ""
    overheat_attack_interval_seconds: float = 0.0
    overheat_attack_unblock_frame: int = 0
    overheat_attack_motion_unblock_frame: int = 0
    overheat_attack_loop_start_frame: int = 0
    overheat_attack_skill_effect_id: int | None = None


def _skill_ids(encoded: str) -> tuple[str, ...]:
    return tuple(
        raw.partition(":")[0].strip()
        for raw in str(encoded or "").replace(";", ",").split(",")
        if raw.partition(":")[0].strip()
    )


@lru_cache(maxsize=128)
def load_socket_minion_definition(
    database_path: str | Path,
    monster_template_id: str,
    amount: int,
    offset: tuple[float, float, float, float],
) -> SocketMinionDefinition | None:
    """Resolve one generated ``Minion`` instruction without hot-path I/O."""

    path = Path(database_path).resolve()
    monster_template_id = str(monster_template_id).strip()
    if not path.is_file() or not monster_template_id:
        return None
    with sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True
    ) as database:
        row = database.execute(
            """
            SELECT COALESCE(LifeTime, 0), COALESCE(Skills, ''),
                   COALESCE(DespawnDuration, 0),
                   COALESCE(RunSpeed, 0), COALESCE(AggroRange, 0),
                   COALESCE(StartupDuration, 0), COALESCE(Graphics, ''),
                   COALESCE(StartAnimationState, ''), COALESCE(SummonSequence, '')
            FROM _Template_Monster WHERE Id=? LIMIT 1
            """,
            (monster_template_id,),
        ).fetchone()
        if row is None:
            return None
        attack_row = None
        for skill_id in _skill_ids(row[1]):
            attack_row = database.execute(
                """
                SELECT rowid - 1, COALESCE(SkillType, ''),
                       COALESCE(SkillUnblockFrame, 0),
                       COALESCE(HitFrame, 0),
                       COALESCE(LoopStartFrame, 0),
                       COALESCE(MotionUnblockFrame, 0),
                       COALESCE(ExecuteSequenceMapping, ''), COALESCE(CoolDown, 0)
                FROM _Template_Skill WHERE Id=? LIMIT 1
                """,
                (skill_id,),
            ).fetchone()
            if attack_row is not None:
                break
        aura = load_player_summon_aura(database, monster_template_id)
        overheat_attack_row = None
        overheat_effect_row = None
        if monster_template_id.casefold() == "mercenary_ember_knight_kain":
            overheat_attack_row = database.execute(
                "SELECT rowid - 1, COALESCE(SkillType, ''), "
                "COALESCE(SkillUnblockFrame, 0), "
                "COALESCE(LoopStartFrame, 0), "
                "COALESCE(MotionUnblockFrame, 0) "
                "FROM _Template_Skill "
                "WHERE Id='mercenaryskill_lava_01_attack_02' "
                "AND COALESCE(Hidden, 0)<>0 LIMIT 1"
            ).fetchone()
            overheat_effect_row = database.execute(
                "SELECT rowid - 1 FROM _Template_StatusEffect "
                "WHERE Id='mercenary_ember_knight_overheated' LIMIT 1"
            ).fetchone()
        fixed_direction = monster_template_id in {
            'minion_fp_s2_fire_shot', 'minion_fp_s3_light_shot', 'minion_fp_s4_ice_shot',
        }
        projectile_lifetime = 0.0
        if fixed_direction and attack_row is not None:
            bullet = database.execute(
                'SELECT COALESCE(b.LifeTime, 0) FROM _Template_Skill s '
                'JOIN _Template_SkillBullet b ON b.Id=s.SkillBulletId '
                'WHERE s.rowid=?', (int(attack_row[0]) + 1,),
            ).fetchone()
            projectile_lifetime = float(bullet[0] or 0) if bullet else 0.0

    attack_index = int(attack_row[0]) if attack_row is not None else None
    attack_type = str(attack_row[1]) if attack_row is not None else ""
    skill_unblock = max(0, int(attack_row[2])) if attack_row else 0
    hit_frame = max(0, int(attack_row[3])) if attack_row else 0
    loop_start = max(0, int(attack_row[4])) if attack_row else 0
    motion_unblock = max(0, int(attack_row[5])) if attack_row else 0
    sequence = str(attack_row[6]).strip() if attack_row else ""
    attack_interval = max(
        0.2,
        (
            hit_frame if attack_type == "Sustained" else skill_unblock
        ) / SERVER_TICKS_PER_SECOND,
    )
    if fixed_direction:
        # These short-lived emitters fire once. Keep their server source
        # alive until its bullets land, without turning it into a turret.
        attack_interval = max(
            projectile_lifetime + 1.0, attack_interval, float(attack_row[7] or 0.0),
        )
    count = max(1, int(amount))
    normalized_offset = tuple(float(value) for value in offset)
    if len(normalized_offset) != 4:
        normalized_offset = (0.0, 0.0, 0.0, 0.0)
    return SocketMinionDefinition(
        skill_index=attack_index or 0,
        skill_resource_id=f"socket_minion:{monster_template_id}",
        monster_template_id=monster_template_id,
        client_monster_template_id=monster_template_id,
        amount=count,
        limit=count,
        category=(f"socket:{monster_template_id}:{normalized_offset[3]}"
                  if fixed_direction else f"socket:{monster_template_id}"),
        spawn_delay_seconds=0.0,
        lifetime_seconds=max(0.0, float(row[0] or 0.0), projectile_lifetime + .5
                             if fixed_direction else 0.0),
        despawn_duration_seconds=max(0.0, float(row[2] or 0.0)),
        owner_despawn_range=(100.0 if fixed_direction else SOCKET_SUMMON_OWNER_RANGE),
        spawn_offset=normalized_offset,
        attack_skill_database_index=attack_index,
        attack_skill_index=attack_index,
        attack_skill_type=attack_type,
        attack_interval_seconds=attack_interval,
        attack_playback_interval_seconds=attack_interval,
        attack_playback_enabled=bool(
            sequence and sequence.casefold() != "empty_sequence"
        ),
        startup_duration_seconds=summon_startup_seconds(*row[5:9]),
        attack_initial_delay_seconds=0.0,
        attack_loop_start_frame=loop_start,
        attack_motion_unblock_frame=motion_unblock,
        attack_skill_unblock_frame=skill_unblock,
        aura=aura,
        fixed_direction=fixed_direction,
        stationary=fixed_direction,
        move_speed=(0.0 if monster_template_id.startswith("minion_holysoul_") else max(0.0, float(row[3] or 0.0))),
        aggro_range=max(0.0, float(row[4] or 0.0)),
        overheat_attack_skill_database_index=(
            int(overheat_attack_row[0])
            if overheat_attack_row is not None and overheat_effect_row is not None
            else None
        ),
        overheat_attack_skill_type=(
            str(overheat_attack_row[1]) if overheat_attack_row is not None else ""
        ),
        overheat_attack_interval_seconds=(
            max(0.2, int(overheat_attack_row[2]) / SERVER_TICKS_PER_SECOND)
            if overheat_attack_row is not None and overheat_effect_row is not None
            else 0.0
        ),
        overheat_attack_unblock_frame=(
            max(0, int(overheat_attack_row[2]))
            if overheat_attack_row is not None else 0
        ),
        overheat_attack_motion_unblock_frame=(
            max(0, int(overheat_attack_row[4]))
            if overheat_attack_row is not None else 0
        ),
        overheat_attack_loop_start_frame=(
            max(0, int(overheat_attack_row[3]))
            if overheat_attack_row is not None else 0
        ),
        overheat_attack_skill_effect_id=(
            int(overheat_effect_row[0])
            if overheat_attack_row is not None and overheat_effect_row is not None
            else None
        ),
    )


__all__ = ["SocketMinionDefinition", "load_socket_minion_definition"]
