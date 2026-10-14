"""Map-authored companion spawn volumes, battle phases and NPC handoffs.

Location auras, Minion programs, named waypoints, hidden-skill unlocks and
ON:DIE Despawn auras all come from shipped data. The tutorial capture
20260909_000000_sample_10 verifies their wire lifecycle and ordering.
"""
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
import sqlite3
import struct

from dso_server.gameplay.local_map_state import map_database_path
from dso_server.gameplay.map_event_state import TriggerVolumeDefinition
from dso_server.protocol.socket_minion_protocol import load_socket_minion_definition


@dataclass(frozen=True)
class CompanionHandoff:
    companion_id: str
    defeated_id: str
    injury_delay: float
    radius: float
    height: float
    corpse_seconds: float


@lru_cache(maxsize=4)
def load_companion_handoffs(database_path: str | Path):
    """Resolve targeted companion Despawn auras caused by monster death."""
    path = Path(database_path)
    result = []
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT Id, IncidentLocationStatusEffects FROM _Template_Monster "
                          "WHERE TRIM(COALESCE(IncidentLocationStatusEffects,''))<>''").fetchall()
        for defeated_id, encoded in rows:
            for spec in str(encoded).split(";"):
                parts = [p.strip() for p in spec.split(",")]
                options = dict(p.split(":", 1) for p in parts[1:] if ":" in p)
                if options.get("ON", "").casefold() != "die":
                    continue
                row = db.execute(
                    "SELECT aura.AuraEffectAffectedTemplateId, aura.AuraRadius, aura.AuraHeight, "
                    "child.StartModifiers, companion.DeathDuration FROM _Template_StatusEffect AS aura "
                    "JOIN _Template_StatusEffect AS child ON child.Id=aura.AuraEffectId "
                    "JOIN _Template_Monster AS companion ON companion.Id=aura.AuraEffectAffectedTemplateId "
                    "WHERE aura.Id=? AND companion.BehaviorType='CompanionStrategy'", (parts[0],)).fetchone()
                if row is None or "despawn" not in str(row[3]).casefold().strip(";").split(";"):
                    continue
                result.append(CompanionHandoff(str(row[0]), str(defeated_id),
                    max(0.0, float(options.get("DE", 0))), max(0.0, float(row[1] or 0)),
                    max(0.0, float(row[2] or 0)), max(0.0, float(row[4] or 0))))
    return tuple(result)


@dataclass(frozen=True)
class MapCompanionSpawn:
    volume: TriggerVolumeDefinition
    definition: object
    level: int
    damage: tuple[float, float]


@lru_cache(maxsize=256)
def load_map_companion_spawns(database_path: str | Path, map_id: str):
    """Resolve location aura -> receiver status -> friendly Minion programs.

    Only CompanionStrategy actors belong here. Hostile minions and unrelated
    location damage effects must not acquire a player owner accidentally.
    """
    path = Path(database_path)
    map_path = map_database_path(path, map_id)
    if not map_path.is_file():
        return ()
    with sqlite3.connect(f"file:{map_path.as_posix()}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT rowid, Id, EventSetRow, Transform, StatusEffectId "
                          "FROM _Instance_LocationEffect").fetchall()
        waypoints = db.execute("SELECT PathName, Transform FROM _Instance_Waypoint "
                               "WHERE COALESCE(EventSetRow,-1)=-1 ORDER BY WayPointIndex").fetchall()
    result = []
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as db:
        for row_id, instance_id, event_set, transform, aura_id in rows:
            aura = db.execute("SELECT AuraEffectId FROM _Template_StatusEffect WHERE Id=?",
                              (aura_id,)).fetchone()
            if aura is None:
                continue
            for effect_id in str(aura[0] or "").split(";"):
                effect = db.execute("SELECT StartModifiers FROM _Template_StatusEffect WHERE Id=?",
                                    (effect_id.strip(),)).fetchone()
                for modifier in str((effect[0] or "") if effect else "").split(";"):
                    kind, _, args = modifier.partition(":")
                    if kind.casefold() != "minion":
                        continue
                    values = args.split(",")
                    monster_id = values[0].strip()
                    monster = db.execute(
                        "SELECT BehaviorType, CharLevel, MinDamage, MaxDamage, Skills, AttackInterval, PathName "
                        "FROM _Template_Monster WHERE Id=?", (monster_id,)).fetchone()
                    if monster is None or str(monster[0]).casefold() != "companionstrategy":
                        continue
                    definition = load_socket_minion_definition(path, monster_id, int(values[1]),
                                                               (0.0, 0.0, 0.0, 0.0))
                    # Hidden cinematic skills (e.g. Vithrandir's shield) are
                    # unlocked by later scripts, never the basic combat cast.
                    attack = None
                    for skill in str(monster[4]).replace(";", ",").split(","):
                        attack = db.execute(
                            "SELECT rowid-1, SkillType, SkillUnblockFrame, LoopStartFrame, "
                            "MotionUnblockFrame FROM _Template_Skill WHERE Id=? "
                            "AND COALESCE(Hidden,0)=0 AND DamageModifier>0",
                            (skill.partition(":")[0].strip(),)).fetchone()
                        if attack:
                            break
                    if definition is None or attack is None:
                        continue
                    interval = max(0.2, float(monster[5]), int(attack[2])/25.0)
                    definition = replace(
                        definition, category=f"map-companion:{monster_id}",
                        skill_resource_id=f"map-companion:{monster_id}",
                        attack_skill_database_index=int(attack[0]), attack_skill_index=int(attack[0]),
                        attack_skill_type=str(attack[1]), attack_skill_unblock_frame=int(attack[2]),
                        attack_loop_start_frame=int(attack[3]), attack_motion_unblock_frame=int(attack[4]),
                        attack_interval_seconds=interval, attack_playback_interval_seconds=interval,
                        attack_playback_enabled=True, follows_owner=True, map_companion=True,
                        owner_despawn_range=float("inf"), lifetime_seconds=0.0,
                        patrol_points=tuple(struct.unpack("<16f", matrix)[12:15]
                                            for name, matrix in waypoints
                                            if name and name == monster[6]),
                    )
                    volume = TriggerVolumeDefinition(int(row_id), str(instance_id), int(event_set),
                                                     True, "companion", struct.unpack("<16f", transform))
                    result.append(MapCompanionSpawn(volume, definition, int(monster[1]),
                                                    (float(monster[2]), float(monster[3]))))
    return tuple(result)


@lru_cache(maxsize=256)
def load_companion_combat_phases(database_path, map_id, monster_id):
    """Recover the damage cast unlocked by a companion's behavior aura."""
    path = Path(database_path)
    with sqlite3.connect(f"file:{map_database_path(path, map_id).as_posix()}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT rowid, Id, EventSetRow, Transform, StatusEffectId "
                          "FROM _Instance_LocationEffect WHERE COALESCE(EventSetRow,-1)=-1").fetchall()
    phases = []
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as db:
        row = db.execute("SELECT Skills FROM _Template_Monster WHERE Id=?", (monster_id,)).fetchone()
        skills = {s.partition(":")[0].strip() for s in str(row[0]).replace(";", ",").split(",")}
        for row_id, instance_id, event_set, matrix, aura_id in rows:
            row = db.execute("SELECT child.StartModifiers FROM _Template_StatusEffect AS aura "
                             "JOIN _Template_StatusEffect AS child ON child.Id=aura.AuraEffectId "
                             "WHERE aura.Id=?", (aura_id,)).fetchone()
            for modifier in str((row[0] or "") if row else "").split(";"):
                kind, _, args = modifier.partition(":")
                if kind.casefold() != "unlockhiddenskills":
                    continue
                for skill in args.split(","):
                    if skill.strip() not in skills:
                        continue
                    attack = db.execute(
                        "SELECT rowid-1, SkillType, SkillUnblockFrame, LoopStartFrame, MotionUnblockFrame "
                        "FROM _Template_Skill WHERE Id=? AND SkillType='Ranged' AND DamageModifier>0",
                        (skill.strip(),)).fetchone()
                    if attack is not None:
                        volume = TriggerVolumeDefinition(int(row_id), str(instance_id), int(event_set),
                                                         True, "companion-phase", struct.unpack("<16f", matrix))
                        phases.append((volume, tuple(attack)))
                        break
    return tuple(phases)


def companion_combat_phase(database_path, map_id, definition, position):
    for volume, attack in load_companion_combat_phases(database_path, map_id,
                                                      definition.monster_template_id):
        if not volume.contains(position):
            continue
        return replace(definition, attack_skill_index=int(attack[0]),
                       attack_skill_database_index=int(attack[0]), attack_skill_type=str(attack[1]),
                       attack_skill_unblock_frame=int(attack[2]), attack_loop_start_frame=int(attack[3]),
                       attack_motion_unblock_frame=int(attack[4]))
    return definition
