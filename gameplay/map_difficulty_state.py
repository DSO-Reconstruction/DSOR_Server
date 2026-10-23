"""Database-owned map-access selection and monster difficulty state.

Travel requests name a ``_Template_MapAccessGroup`` row.  Rows such as
``a0203_kingscrypt02_03`` are not physical map assets; they select difficulty
metadata for the physical ``AccessGroup`` map ``a0203_kingscrypt02``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class MapAccessSelection:
    request_map_id: str
    physical_map_id: str
    access_group_id: str
    map_difficulty: int
    scaling_difficulty: int
    monster_level: int | None

    @property
    def monster_scaling_difficulty(self) -> int:
        """Difficulty-factor row used by authoritative monster stats.

        Access-group travel encodes the selected tier in ``map_difficulty``
        (for example Bloodshed is 8), while slider-based encounters encode it
        in ``scaling_difficulty``.  Both select the same database factor curve;
        ignoring either value silently reduced those monsters to factor 0.
        """

        return max(0, int(self.map_difficulty), int(self.scaling_difficulty))

    @property
    def event_set_flags(self) -> frozenset[str]:
        '''EventSet resource names selected by this logical map route.'''

        return frozenset((str(self.access_group_id).casefold(),))


def _open_read_only(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _physical_map_for_access_group(
    database: sqlite3.Connection,
    access_group_id: str,
) -> str:
    '''Resolve a logical access group to its shared physical map assets.'''

    row = database.execute(
        '''
        SELECT MapTemplate
        FROM _Template_MapTemplate
        WHERE AccessGroup=?
        ORDER BY rowid
        LIMIT 1
        ''',
        (str(access_group_id),),
    ).fetchone()
    return str(row[0]) if row is not None else str(access_group_id)


def map_teleport_access_group_override(
    static_database_path: str | Path,
    map_id: str,
) -> str:
    """Return the access group exposed by a map's teleport access window."""

    with _open_read_only(static_database_path) as database:
        row = database.execute(
            """
            SELECT COALESCE(TeleportAccessGroupOverride, '')
            FROM _Template_MapTemplate
            WHERE MapTemplate=? OR AccessGroup=?
            ORDER BY CASE WHEN MapTemplate=? THEN 0 ELSE 1 END, rowid
            LIMIT 1
            """,
            (str(map_id), str(map_id), str(map_id)),
        ).fetchone()
    return str(row[0] or "") if row is not None else ""


def _default_heroic_access(
    database: sqlite3.Connection,
    physical_map_id: str,
) -> tuple[str, str, int] | None:
    """Return the first client-valid difficulty for a Heroic map.

    Heroic map databases expose a difficulty-zero staging row, but the
    client's gauntlet UI treats zero as ``InvalidMapDifficulty`` once the map
    is loaded. Retail enters these maps through a positive-difficulty access
    ID. Prefer difficulty one and retain a deterministic positive fallback.
    """

    row = database.execute(
        """
        SELECT Id, AccessGroup, Difficulty
        FROM _Template_MapAccessGroup
        WHERE AccessGroup=? AND CAST(Difficulty AS INTEGER)>0
        ORDER BY CASE WHEN CAST(Difficulty AS INTEGER)=1 THEN 0 ELSE 1 END,
                 CAST(Difficulty AS INTEGER), Id
        LIMIT 1
        """,
        (str(physical_map_id),),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1]), int(row[2])


def resolve_map_access(
    static_database_path: str | Path,
    request_map_id: str,
    *,
    player_level: int,
    scaling_difficulty: int = 0,
) -> MapAccessSelection:
    """Resolve a client travel choice to a physical map and monster level."""

    request_map_id = str(request_map_id).strip()
    if not request_map_id:
        raise ValueError("map access id cannot be empty")
    scaling_difficulty = int(scaling_difficulty)
    if scaling_difficulty < 0:
        raise ValueError("scaling difficulty cannot be negative")

    with _open_read_only(static_database_path) as database:
        row = database.execute(
            """
            SELECT mag.AccessGroup, mag.Difficulty,
                   COALESCE(mt.MapTemplate, mag.AccessGroup)
            FROM _Template_MapAccessGroup AS mag
            LEFT JOIN _Template_MapTemplate AS mt
              ON mt.AccessGroup=mag.AccessGroup
            WHERE mag.Id=?
            ORDER BY mt.rowid
            LIMIT 1
            """,
            (request_map_id,),
        ).fetchone()
        if row is None:
            direct_template = database.execute(
                """
                SELECT COALESCE(MapType, ''), COALESCE(AccessGroup, '')
                FROM _Template_MapTemplate
                WHERE MapTemplate=? LIMIT 1
                """,
                (request_map_id,),
            ).fetchone()
            direct_access_template = database.execute(
                '''
                SELECT MapTemplate
                FROM _Template_MapTemplate
                WHERE AccessGroup=?
                ORDER BY rowid
                LIMIT 1
                ''',
                (request_map_id,),
            ).fetchone()
            promoted = (
                (request_map_id, request_map_id, 0)
                if direct_template is None
                and direct_access_template is not None
                else _default_heroic_access(
                    database,
                    str(direct_template[1] or request_map_id),
                )
                if (
                    direct_template is not None
                    and str(direct_template[0]).casefold() == "heroic"
                )
                else None
            )
            if promoted is None:
                return MapAccessSelection(
                    request_map_id,
                    request_map_id,
                    request_map_id,
                    0,
                    scaling_difficulty,
                    None,
                )
            request_map_id, access_group_id, map_difficulty = promoted
            physical_map_id = _physical_map_for_access_group(
                database, access_group_id
            )
        else:
            access_group_id = str(row[0] or request_map_id)
            physical_map_id = str(row[2] or access_group_id)
            map_difficulty = int(row[1] or 0)

        template = database.execute(
            """
            SELECT MonsterScaleLevel, IgnoreDifficultySelection,
                   COALESCE(MapType, '')
            FROM _Template_MapTemplate
            WHERE AccessGroup=? OR MapTemplate=?
            ORDER BY CASE WHEN AccessGroup=? THEN 0 ELSE 1 END, rowid
            LIMIT 1
            """,
            (access_group_id, physical_map_id, access_group_id),
        ).fetchone()
        if (
            template is not None
            and str(template[2]).casefold() == "heroic"
            and map_difficulty <= 0
        ):
            promoted = _default_heroic_access(database, access_group_id)
            if promoted is None:
                raise ValueError(
                    f"heroic map {physical_map_id!r} has no positive "
                    "difficulty access"
                )
            request_map_id, access_group_id, map_difficulty = promoted
            physical_map_id = _physical_map_for_access_group(
                database, access_group_id
            )

        difficulty = database.execute(
            """
            SELECT MonsterLevelPlus
            FROM _Template_MapDifficulty
            WHERE CAST(Id AS INTEGER)=? LIMIT 1
            """,
            (map_difficulty,),
        ).fetchone()
        if difficulty is None:
            raise ValueError(
                f"map access {request_map_id!r} uses unknown difficulty "
                f"{map_difficulty}"
            )
        level_plus = int(difficulty[0] or 0)

        # A map is level-scaled when its access group exposes difficulty
        # choices or its template explicitly owns a scaling level.  The
        # selected MapDifficulty adds the exact offset seen in retail
        # (level 100 + difficulty 3's 15 = monster level 115).
        scaled = bool(
            map_difficulty
            or (
                template is not None
                and int(template[1] or 0) != 0
            )
        )
        monster_level = (
            max(1, int(player_level) + level_plus) if scaled else None
        )

        factor = database.execute(
            """
            SELECT 1 FROM _Template_DifficultyFactors
            WHERE CAST(Id AS INTEGER)=? LIMIT 1
            """,
            (scaling_difficulty,),
        ).fetchone()
        if factor is None:
            raise ValueError(
                f"unknown scaling difficulty {scaling_difficulty}"
            )

    return MapAccessSelection(
        request_map_id,
        physical_map_id,
        access_group_id,
        map_difficulty,
        scaling_difficulty,
        monster_level,
    )
