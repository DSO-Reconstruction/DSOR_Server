"""Database-driven population policy for boss encounter maps."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import sqlite3

from dso_server.gameplay.map_hazards import is_map_hazard


# A few event maps contain only a generic portal or a removed legacy phase
# template. Their concrete encounter actor still exists in the same static
# database. Keep these exceptional content aliases here instead of teaching
# the population builder about event-specific names.
_BOSS_TEMPLATE_OVERRIDES = {
    "e_bgh_boss_balor": "boss_balor_bgh_single",
    "e_bgh_boss_guardian_black": "bgh_guardian_boss_andermagic",
    "e_bgh_boss_herald": "boss_herald_bgh_single",
    "e_bgh_boss_heredur": "boss_heredur_bgh_single",
    "e_bgh_boss_mortis": "boss_mortis_mounted_bgh_single",
    "e_bgh_boss_nefertari": "boss_nefertari_bgh_single",
    "e_bgh_boss_sargon": "boss_sargon_bgh_single",
    "e_bos_mortis_pw_boss": "boss_mortis_mounted",
}

# This is the Between-Group-Hunt portal selector, not an encounter arena. Its
# authored boss rows are menu previews for the destination choices and must
# never be materialized as local combat actors.
_BOSS_SELECTOR_MAPS = {"e_bgh_boss_all"}

# These arenas reveal their boss only when the player reaches its entrance.
APPROACH_BOSS_MAPS = frozenset({"a0407_boss", "c2310_boss", "a0404_culttower03"})


@lru_cache(maxsize=32)
def boss_entrance_presentation(
    static_database_path: str | Path, template_id: str,
) -> tuple[str, float]:
    """Keep scaling combat stats while using the authored entrance model."""

    visual_id = str(template_id)
    candidates = (
        "a0406_normal_g_miniboss_03_bear_q_goddessOfHunt03_scale",
        "a0503_g_miniboss_02_witch_q_FoulArtefact05_scale",
        "c2310_boss_snake_scale",
        "a0404_normal_boss_cultistleader_scale",
    )
    if visual_id in candidates:
        visual_id = visual_id.removesuffix("_scale")
    path = Path(static_database_path)
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as database:
        row = database.execute(
            "SELECT COALESCE(StartupDuration, 0) FROM _Template_Monster WHERE Id=?",
            (visual_id,),
        ).fetchone()
    duration = max(0.0, float(row[0])) if row else 0.0
    return visual_id, duration


@dataclass(frozen=True)
class MapEncounterPolicy:
    """Static map classification used by the monster population builder."""

    area_type: str = ""
    map_type: str = ""

    @property
    def is_boss_room(self) -> bool:
        return self.area_type.casefold() == "boss"

    @property
    def permits_density_population(self) -> bool:
        # Boss-map SpawnArea rows drive encounter staging and arena bounds.
        # Treating them as ordinary density areas duplicates the authored boss
        # over the complete room and can start many boss skill streams at once.
        return not self.is_boss_room


@lru_cache(maxsize=512)
def load_map_encounter_policy(
    static_database_path: str | Path,
    map_id: str,
) -> MapEncounterPolicy:
    """Resolve a map's encounter policy from ``_Template_MapTemplate``."""

    path = Path(static_database_path)
    with sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True
    ) as database:
        row = database.execute(
            """
            SELECT COALESCE(AreaType, ''), COALESCE(MapType, '')
            FROM _Template_MapTemplate
            WHERE LOWER(MapTemplate)=LOWER(?)
            ORDER BY rowid
            LIMIT 1
            """,
            (str(map_id),),
        ).fetchone()
    if row is None:
        if "boss" in str(map_id).casefold():
            return MapEncounterPolicy("Boss", "Standard")
        return MapEncounterPolicy()
    return MapEncounterPolicy(str(row[0]), str(row[1]))


def _skill_ids(raw: str) -> tuple[str, ...]:
    return tuple(
        token.strip().partition(":")[0].strip()
        for token in str(raw or "").replace(";", ",").split(",")
        if token.strip().partition(":")[0].strip()
    )


@lru_cache(maxsize=4096)
def _monster_profile(
    static_database_path: str | Path,
    template_id: str,
) -> tuple[str, str, str] | None:
    path = Path(static_database_path)
    with sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True
    ) as database:
        row = database.execute(
            "SELECT COALESCE(Rank, ''), COALESCE(Skills, ''), "
            "COALESCE(SummonMonsterId, '') "
            "FROM _Template_Monster WHERE Id=? "
            "AND COALESCE(Obsolete, 0)=0 LIMIT 1",
            (str(template_id),),
        ).fetchone()
    return None if row is None else tuple(str(value) for value in row)


def _is_concrete_boss(
    static_database_path: str | Path,
    template_id: str,
) -> bool:
    profile = _monster_profile(static_database_path, template_id)
    return bool(
        profile is not None
        and profile[0].casefold().startswith("rank_boss")
        and profile[1].strip()
    )


def _status_effect_minions(
    static_database_path: str | Path,
    skill_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Resolve server-side ``Minion`` modifiers used by boss spawners."""

    if not skill_ids:
        return ()
    path = Path(static_database_path)
    result = []
    with sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True
    ) as database:
        for skill_id in skill_ids:
            row = database.execute(
                "SELECT COALESCE(UserStatusEffects, '') "
                "FROM _Template_Skill WHERE Id=? LIMIT 1",
                (skill_id,),
            ).fetchone()
            if row is None:
                continue
            for effect_spec in str(row[0]).split(";"):
                effect_id = effect_spec.partition(",")[0].strip()
                if not effect_id:
                    continue
                effect = database.execute(
                    "SELECT COALESCE(StartModifiers, '') "
                    "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
                    (effect_id,),
                ).fetchone()
                if effect is None:
                    continue
                for modifier in str(effect[0]).split(";"):
                    kind, separator, arguments = modifier.partition(":")
                    if not separator or kind.strip().casefold() != "minion":
                        continue
                    minion_id = arguments.partition(",")[0].strip()
                    if minion_id and minion_id not in result:
                        result.append(minion_id)
    return tuple(result)


def _direct_status_effect_minions(
    static_database_path: str | Path,
    effect_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Resolve ``Minion`` programs from already-known status effects.

    Legacy normal boss rooms commonly place a suicide/staging monster whose
    *IncidentActorStatusEffects* points at a same-named status effect.  The
    concrete normal boss lives in that effect, not in the staging skill's
    UserStatusEffects.  Skipping this second hop made the explicit scaling
    actor look like the only usable boss in those rooms.
    """

    if not effect_ids:
        return ()
    path = Path(static_database_path)
    result = []
    with sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True
    ) as database:
        for effect_id in effect_ids:
            row = database.execute(
                "SELECT COALESCE(StartModifiers, '') "
                "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
                (str(effect_id),),
            ).fetchone()
            if row is None:
                continue
            for modifier in str(row[0]).split(";"):
                kind, separator, arguments = modifier.partition(":")
                if not separator or kind.strip().casefold() != "minion":
                    continue
                minion_id = arguments.partition(",")[0].strip()
                if minion_id and minion_id not in result:
                    result.append(minion_id)
    return tuple(result)


@lru_cache(maxsize=4096)
def _monster_status_effect_ids(
    static_database_path: str | Path,
    template_id: str,
) -> tuple[str, ...]:
    path = Path(static_database_path)
    with sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True
    ) as database:
        row = database.execute(
            "SELECT COALESCE(PermanentStatusEffect, ''), "
            "COALESCE(IncidentActorStatusEffects, '') "
            "FROM _Template_Monster WHERE Id=? LIMIT 1",
            (str(template_id),),
        ).fetchone()
    values = [str(template_id)]
    if row is not None:
        for encoded in row:
            values.extend(
                entry.partition(",")[0].strip()
                for entry in str(encoded).split(";")
                if entry.partition(",")[0].strip()
            )
    return tuple(dict.fromkeys(values))


@lru_cache(maxsize=4096)
def resolve_boss_template_id(
    static_database_path: str | Path,
    template_id: str,
) -> str:
    """Materialize a concrete boss hidden behind a map staging actor."""

    path = Path(static_database_path)
    template_id = str(template_id)
    profile = _monster_profile(path, template_id)
    if profile is None:
        return ""
    rank, skills, summoned = profile
    if rank.casefold().startswith("rank_boss") and skills.strip():
        return template_id

    for candidate in _skill_ids(summoned):
        if _is_concrete_boss(path, candidate):
            return candidate
    for candidate in _status_effect_minions(path, _skill_ids(skills)):
        if _is_concrete_boss(path, candidate):
            return candidate
    for candidate in _direct_status_effect_minions(
        path, _monster_status_effect_ids(path, template_id)
    ):
        if _is_concrete_boss(path, candidate):
            return candidate

    # New Moon's map rows use a generic SuicideSpawner and encode the spawned
    # witch in the staging actor's own resource name.
    if "_spawner" in template_id.casefold():
        candidate = template_id.replace("_spawner", "")
        if _is_concrete_boss(path, candidate):
            return candidate
    return ""


@lru_cache(maxsize=4096)
def is_boss_spawn_template(
    static_database_path: str | Path,
    template_id: str,
) -> bool:
    """Bosses and staging actors belong to authored points, not density pools."""

    profile = _monster_profile(static_database_path, template_id)
    return bool(
        profile is not None
        and (
            profile[0].casefold().startswith("rank_boss")
            or resolve_boss_template_id(static_database_path, template_id)
        )
    )


def density_spawn_templates(
    static_database_path: str | Path,
    template_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Keep supporting monsters without copying a boss or its spawner.

    Statue traps are fixed scenery placed by the map, never density mobs.
    """

    return tuple(
        template_id for template_id in template_ids
        if not is_boss_spawn_template(static_database_path, template_id)
        and not is_map_hazard(static_database_path, template_id)
    )


def _select_boss_spawn_candidates(
    static_database_path: str | Path,
    map_id: str,
    rows: tuple[tuple, ...],
    *,
    scaling_difficulty: int = 0,
) -> tuple[tuple, ...]:
    """Select one authored boss phase and resolve its staging actors.

    Map databases retain mutually exclusive EventSet versions of an
    encounter. Spawning rows from every EventSet simultaneously creates every
    phase and difficulty variant. The ordinary group is preferred when it
    contains a concrete boss; otherwise the first authored event group that
    does is selected. Other rows in that same group are preserved as the
    encounter's intentional adds or mechanics.
    """

    policy = load_map_encounter_policy(static_database_path, map_id)
    rows = tuple(rows)
    if not rows:
        return rows
    if not policy.is_boss_room:
        # Story dungeons also place staging actors whose death status creates
        # the real boss. Preserve every row and event gate; only resolve the
        # actor template, just as in dedicated boss rooms.
        return tuple(
            (*row[:2], resolve_boss_template_id(static_database_path, str(row[2]))
             or row[2], *row[3:])
            for row in rows
        )
    if str(map_id).casefold() in _BOSS_SELECTOR_MAPS:
        return ()

    groups: dict[int, list[tuple]] = {}
    for row in rows:
        groups.setdefault(int(row[3]), []).append(row)
    ordered_event_sets = sorted(
        groups, key=lambda value: (value != -1, value)
    )
    wants_scaling = int(scaling_difficulty) > 0
    candidates_by_event_set = []
    for event_set in ordered_event_sets:
        ordinary_bosses = []
        scaling_bosses = []
        non_boss_rows = []
        for row in groups[event_set]:
            authored_template_id = str(row[2])
            resolved = resolve_boss_template_id(
                static_database_path, authored_template_id
            )
            if resolved:
                row = (*row[:2], resolved, *row[3:])
            template_id = str(row[2])
            if not _is_concrete_boss(static_database_path, template_id):
                non_boss_rows.append(row)
                continue
            authored_key = authored_template_id.casefold()
            is_scaling_variant = (
                resolved == authored_template_id
                and any(
                    marker in authored_key
                    for marker in ("scaling", "_scale", "_heroic", "_pw_")
                )
            )
            (scaling_bosses if is_scaling_variant else ordinary_bosses).append(
                row
            )
        candidates_by_event_set.append(
            (non_boss_rows, ordinary_bosses, scaling_bosses)
        )

    # Search every authored phase for the requested difficulty before falling
    # back. Returning the first ordinary event immediately made later scaling
    # phases unreachable on maps that store them in separate EventSets.
    for exact_match in (True, False):
        for non_boss_rows, ordinary_bosses, scaling_bosses in (
            candidates_by_event_set
        ):
            preferred = scaling_bosses if wants_scaling else ordinary_bosses
            fallback = ordinary_bosses if wants_scaling else scaling_bosses
            selected_bosses = preferred if exact_match else fallback
            if not selected_bosses:
                continue
            selected = list(non_boss_rows)
            concrete_bosses: set[str] = set()
            for row in selected_bosses:
                boss_key = str(row[2]).casefold()
                if boss_key in concrete_bosses:
                    continue
                concrete_bosses.add(boss_key)
                selected.append(row)
            return tuple(selected)

    override = _BOSS_TEMPLATE_OVERRIDES.get(str(map_id).casefold())
    if override and _is_concrete_boss(static_database_path, override):
        anchor = rows[0]
        return ((anchor[0], anchor[1], override, -1),)
    return rows


@lru_cache(maxsize=1024)
def _preferred_boss_templates(
    static_database_path: str | Path, map_id: str, scaling_difficulty: int,
) -> tuple[frozenset[str], frozenset[str]]:
    """Choose the encounter once across both baseline and event-gated rows."""

    from dso_server.gameplay.local_map_state import map_database_path

    path = map_database_path(static_database_path, map_id)
    if not path.is_file():
        return frozenset(), frozenset()
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as database:
        spawn_sets = {
            int(row_id) for row_id, flags in database.execute(
                "SELECT Id, EventFlags FROM _Instance_EventSets"
            )
            if "boss_spawn=true" in {
                token.strip().replace(" ", "").casefold()
                for token in str(flags or "").split(";")
            }
        }
        rows = tuple(database.execute(
            "SELECT rowid, Transform, NpcId, COALESCE(EventSetRow, -1) "
            "FROM _Instance_SpawnPoint "
            "WHERE TRIM(COALESCE(NpcId, ''))<>'' "
            "AND COALESCE(AlliedToPlayer, 0)=0 ORDER BY rowid"
        ))
    rows = tuple(row for row in rows if int(row[3]) == -1 or int(row[3]) in spawn_sets)
    # Only reconcile normal/scaling alternatives of the entry encounter.
    # Later scripted phase bosses keep their own event-owned lifecycles.
    if not any(
        resolve_boss_template_id(static_database_path, str(row[2])) == str(row[2])
        and any(marker in str(row[2]).casefold() for marker in ("scaling", "_scale"))
        for row in rows
    ):
        return frozenset(), frozenset()
    alternatives = frozenset(
        resolved.casefold() for row in rows
        if (resolved := resolve_boss_template_id(static_database_path, str(row[2])))
    )
    selected = _select_boss_spawn_candidates(
        static_database_path, map_id, rows,
        scaling_difficulty=scaling_difficulty,
    )
    preferred = frozenset(
        str(row[2]).casefold() for row in selected
        if _is_concrete_boss(static_database_path, str(row[2]))
    )
    return preferred, alternatives


def select_boss_spawn_rows(
    static_database_path: str | Path, map_id: str, rows: tuple[tuple, ...],
    *, scaling_difficulty: int = 0,
) -> tuple[tuple, ...]:
    """Keep baseline loading and event spawning on the same boss variant.

    Selecting from each active subset independently admitted the normal boss
    at map load and a second scaling boss when BOSS_spawn opened later.
    """

    if str(map_id).casefold() == "pw214_mortis_boss":
        # These invisible suicide actors drive the retail staging graph.
        # Mortis' server controller owns their freeze/gate transitions instead.
        from dso_server.gameplay.mortis_arena import CONTROLLERS
        rows = tuple(row for row in rows if str(row[2]).casefold() not in CONTROLLERS)
    if load_map_encounter_policy(static_database_path, map_id).is_boss_room:
        preferred, alternatives = _preferred_boss_templates(
            static_database_path, map_id, int(scaling_difficulty)
        )
        if preferred:
            rows = tuple(
                row for row in rows
                if not (resolved := resolve_boss_template_id(
                    static_database_path, str(row[2])
                )) or resolved.casefold() not in alternatives
                or resolved.casefold() in preferred
            )
    return _select_boss_spawn_candidates(
        static_database_path, map_id, rows,
        scaling_difficulty=scaling_difficulty,
    )
