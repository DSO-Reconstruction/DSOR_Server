"""Serve current-generation static-database queries from an older client build.

The legacy-client fork (``dso_server_legacy``) runs these same protocol modules
against a ``static.db4`` from an earlier content generation, where tables were
consolidated differently and columns come and go. ``tools/audit_legacy_schema.py``
enumerates the gaps; this module is what call sites use to survive them.

Two rules keep this safe to put in the shared modules:

* **The current schema must be unaffected.** When every requested column is
  present, :func:`projection` returns exactly the column list it was given, so
  the emitted SQL is identical to what the call site used to build by hand.
* **Absence is expressed as NULL, not as a missing key.** Padding with
  ``NULL AS <name>`` keeps ``row["Name"]`` valid, so the existing
  ``value or default`` guards absorb the gap without a second code path. Call
  sites that coerce without a guard -- a bare ``float(row[...])`` -- still need
  an explicit check, because ``None`` is not a number.

Where a column was renamed rather than dropped, pass ``aliases`` to map the
current name onto the legacy one. Where a whole table was consolidated into an
ancestor, the call site has to branch on :func:`table_exists`, because that is a
change of meaning rather than of spelling.
"""

from __future__ import annotations

import sqlite3

__all__ = [
    "columns",
    "table_exists",
    "projection",
    "has_columns",
    "map_template_query",
    "map_template_row_query",
]

# ``_Template_MapConfig`` is the legacy ancestor of ``_Template_MapTemplate``.
# It also carries the difficulty and access columns that the current build moved
# out into ``_Template_MapDifficulty`` and ``_Template_MapAccessGroup``.
MAP_TEMPLATE_TABLE = "_Template_MapTemplate"
MAP_CONFIG_TABLE = "_Template_MapConfig"
MAP_TEMPLATE_ALIASES = {"MapLevel": "RecommendedLevel"}


def columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    """Column names of ``table``, or an empty set when it does not exist."""

    # PRAGMA cannot be parameterised, and the table names here are module-level
    # literals rather than anything reaching this from a client.
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return frozenset(str(row[1]) for row in rows)


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    """Whether ``table`` exists, as a table or as a compatibility view."""

    return bool(columns(connection, table))


def has_columns(
    connection: sqlite3.Connection, table: str, *names: str
) -> bool:
    """Whether every named column is present on ``table``."""

    present = columns(connection, table)
    return all(name in present for name in names)


def projection(
    connection: sqlite3.Connection,
    table: str,
    wanted: tuple[str, ...],
    *,
    aliases: dict[str, str] | None = None,
) -> str:
    """Build a select list for ``wanted`` that an older schema can still serve.

    Present columns are named as given. A column named in ``aliases`` whose
    source exists is selected through it. Anything else becomes ``NULL AS name``
    so the result rows keep their shape.
    """

    present = columns(connection, table)
    if not present:
        raise LookupError(f"{table} does not exist in this database")
    parts = []
    for name in wanted:
        if name in present:
            parts.append(name)
            continue
        source = (aliases or {}).get(name)
        if source and source in present:
            parts.append(f"{source} AS {name}")
            continue
        parts.append(f"NULL AS {name}")
    return ", ".join(parts)


def map_template_query(
    connection: sqlite3.Connection, wanted: tuple[str, ...]
) -> str:
    """A complete SELECT serving map templates from either generation.

    Neither build is one row per map, and they vary along different axes. The
    current ``_Template_MapTemplate`` varies by region -- 570 rows over 321
    distinct ``MapTemplate`` values, the duplicates being regional variants added
    after the legacy client shipped. The legacy ``_Template_MapConfig`` has no
    ``Region`` at all and varies by difficulty instead: 1,031 rows over 232 maps,
    keyed by a per-variant ``Id`` the current schema does not have.

    So the current table is passed through untouched, keeping its regional rows,
    while the legacy one collapses to its lowest-difficulty variant per map.
    Selecting ``Difficulty = 0`` instead would look equivalent and quietly lose
    the 36 maps whose variants all start above zero; the ``MIN(Difficulty)``
    aggregate is what makes the other bare columns resolve to that same row.

    Callers that need every variant must not use this -- it is for the lookups
    that ask what a map *is*, not which difficulties it offers.
    """

    if table_exists(connection, MAP_TEMPLATE_TABLE):
        return f"SELECT {', '.join(wanted)} FROM {MAP_TEMPLATE_TABLE}"
    selected = projection(
        connection, MAP_CONFIG_TABLE, wanted, aliases=MAP_TEMPLATE_ALIASES
    )
    return (
        f"SELECT {selected}, MIN(Difficulty) AS _base_difficulty "
        f"FROM {MAP_CONFIG_TABLE} GROUP BY MapTemplate"
    )


def map_template_row_query(
    connection: sqlite3.Connection, wanted: tuple[str, ...]
) -> str:
    """A single-map lookup, taking ``MapTemplate`` as its one parameter.

    The same two-generation problem as :func:`map_template_query`, but for the
    ``WHERE MapTemplate=? LIMIT 1`` shape. On the legacy table the difficulty
    variants are ordered so the lowest is returned, matching the representative
    that the grouped query picks.

    ``AreaType`` is worth calling out: it is registered in the current build's
    ``_Attributes`` and absent from the legacy one, and it appears in neither
    ``_Template_MapConfig`` nor the per-map ``_Instance_MapConfig``. Nothing in
    the legacy content tree carries it, so a caller asking for it will read NULL
    and must decide what that means rather than assume a default.
    """

    if table_exists(connection, MAP_TEMPLATE_TABLE):
        return (
            f"SELECT {', '.join(wanted)} FROM {MAP_TEMPLATE_TABLE} "
            "WHERE MapTemplate=? LIMIT 1"
        )
    selected = projection(
        connection, MAP_CONFIG_TABLE, wanted, aliases=MAP_TEMPLATE_ALIASES
    )
    return (
        f"SELECT {selected} FROM {MAP_CONFIG_TABLE} "
        "WHERE MapTemplate=? ORDER BY Difficulty LIMIT 1"
    )
