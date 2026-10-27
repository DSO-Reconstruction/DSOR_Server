"""Authored quest defense waves and their map placements."""

from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
import math
from pathlib import Path
import sqlite3
import struct

from dso_server.gameplay.local_map_state import map_database_path, npc_actor_id_for_row


class UnsupportedHoldGround(ValueError):
    """The selected defense cannot be constructed from exported content."""


@dataclass(frozen=True)
class HoldGroundSpawn:
    marker_id: str
    template_id: str
    position: tuple[float, float, float]
    # Non-empty only for a corrected entry (CONTENT_CORRECTIONS): the
    # encounter picks one of these templates each time the wave starts.
    template_pool: tuple[str, ...] = ()


@dataclass(frozen=True)
class HoldGroundWave:
    number: int
    duration_seconds: float
    spawns: tuple[HoldGroundSpawn, ...]
    # Tokens the client's loader leaves as empty WaveInfo entries (see
    # parse_wave). They name no marker or template, so they spawn nothing.
    blank_entries: int = 0


@dataclass(frozen=True)
class HoldGroundPlan:
    config_id: str
    map_id: str
    npc_actor_id: int
    npc_template_id: str
    defender_template_id: str
    defender_level: int
    position: tuple[float, float, float]
    yaw: float
    waves: tuple[HoldGroundWave, ...]


# The map's ordinary spawnable monster pool (see map_monster_pool).
MAP_MONSTER_POOL = "<map monster pool>"

# User decision 2026-10-27: narrowly scoped corrections that unblock three
# local defense chains whose rows name a marker/template absent from every
# exported table (static.db4 and all map scenes). Keyed by the exact config,
# map, token kind and offending token; there is no general typo tolerance and
# every other config resolves exactly as authored.
CONTENT_CORRECTIONS = {
    # Afterglow (ce2025-side-quest-15): wave 3 names hg_marker010; ce202504
    # places hg_marker01..hg_marker10 only.
    ("ce2025_holdground_02", "ce202504", "marker", "hg_marker010"): "hg_marker10",
    # Search for the Truth (h0000-history-of-hiraja-04-n): waves 1, 3, 5, 6
    # and 8 name hg_merker10; h0004_temple_dun places hg_marker01..15.
    ("h0000_history_of_hiraja_04_holdground_01", "h0004_temple_dun", "marker",
     "hg_merker10"): "hg_marker10",
    # Crash Landing (h0003-deep-fall-01-n): waves 2, 4, 6, 8 and 9 name the
    # template "Guardian", which is only a rank/name value, never a template
    # id. Each such entry spawns one of the map's ordinary monsters.
    ("h0003_deep_fall_01_holdground_01", "h0003_oasis_dun", "template",
     "Guardian"): MAP_MONSTER_POOL,
}


def map_monster_pool(database_path, map_id):
    """The map's ordinary spawnable monster pool, as density areas use it.

    ``_load_map_monster_templates`` (map-prefixed, non-obsolete Rank_monster
    templates with skills, without quest ``_q_``/dummy/invisible/grave ids)
    filtered by ``density_spawn_templates`` (no bosses or their spawners) is
    the pool a non-tower map without a spawn-list override populates its
    regular spawn areas from. Holdground-only templates and templates without
    a combat program are excluded.
    """
    from dso_server.gameplay.boss_encounter_policy import density_spawn_templates
    from dso_server.gameplay.local_monster_state import (
        _load_map_monster_templates, _load_runtime_definition,
    )

    path = Path(database_path)
    return tuple(
        template for template in density_spawn_templates(
            path, _load_map_monster_templates(path, str(map_id)))
        if "holdground" not in template.casefold()
        and _load_runtime_definition(path, template, 0) is not None
    )


def parse_wave(text):
    """Split one ``_Template_HoldGround.WaveN`` cell like the client does.

    ``Managers::TemplateManager::LoadHoldGroundData`` (R261_1024
    0x140898ea8) splits the cell with ``Util::String::Tokenize(";")`` and
    each token again with ``Tokenize(":")``. Tokenize (0x140ce3d20) is a
    loop over MSVC ``strtok_s`` (0x140dcbd50): runs of separators collapse,
    empty tokens vanish and nothing is trimmed. Only a token with exactly
    two parts fills its ``WaveInfo`` entry as (marker, template)
    (``cmp [rbp+0xe8], 2`` at 0x1408995fd); any other token keeps a blank
    entry. Returns ``(pairs, blank_entries)``.
    """
    pairs, blank = [], 0
    for token in (part for part in str(text or "").split(";") if part):
        parts = [part for part in token.split(":") if part]
        if len(parts) == 2:
            pairs.append((parts[0], parts[1]))
        else:
            blank += 1
    return tuple(pairs), blank


def _signature(path):
    path = Path(path).resolve()
    try:
        stat = path.stat()
    except OSError as exc:
        raise UnsupportedHoldGround(f"missing defense content: {path.name}") from exc
    return str(path), stat.st_mtime_ns, stat.st_size


def _connect(signature):
    return closing(sqlite3.connect(f"file:{Path(signature[0]).as_posix()}?mode=ro", uri=True))


def load_holdground_plan(database_path, map_id, config_id, actor, *, npc_actor_id=None):
    """Resolve every spawn before accepting a defense; never invent a point.

    Repeated marker/template pairs are distinct monsters. Empty trailing wave
    columns sometimes retain editor-default durations and are not waves.
    Blank client entries (malformed tokens) spawn nothing; a well-formed
    entry naming an absent marker or template rejects the whole setup.
    """
    try:
        result = _load_holdground_plan(
            _signature(database_path),
            _signature(map_database_path(database_path, map_id)),
            str(map_id), str(config_id), str(actor),
            None if npc_actor_id is None else int(npc_actor_id),
        )
    except sqlite3.Error as error:  # not cached: may be transient
        raise UnsupportedHoldGround(f"unreadable defense content: {error}") from error
    if isinstance(result, str):
        raise UnsupportedHoldGround(result)
    return result


def holdground_actor_ids(database_path, map_id, actor):
    """Actor ids of every ``_Instance_NPC`` placement of ``actor`` in a map.

    Empty when the map is not exported or does not place the actor at all.
    """
    try:
        signature = _signature(map_database_path(database_path, map_id))
    except UnsupportedHoldGround:
        return ()
    return _holdground_actor_ids(signature, str(actor))


@lru_cache(maxsize=256)
def _holdground_actor_ids(map_signature, actor):
    try:
        with _connect(map_signature) as database:
            return tuple(npc_actor_id_for_row(row[0]) for row in database.execute(
                "SELECT rowid FROM _Instance_NPC WHERE Id=? OR _ID=? ORDER BY rowid",
                (actor, actor),
            ))
    except sqlite3.Error:
        return ()


@lru_cache(maxsize=256)
def _load_holdground_plan(static_signature, map_signature, map_id, config_id, actor, npc_actor_id):
    # Exported content is immutable for a file signature: cache rejections as
    # well, since offers re-validate a setup for every NPC information batch.
    try:
        return _build_holdground_plan(static_signature, map_signature, map_id,
                                      config_id, actor, npc_actor_id)
    except UnsupportedHoldGround as error:
        return str(error)


def _build_holdground_plan(static_signature, map_signature, map_id, config_id, actor, npc_actor_id):
    with _connect(static_signature) as database:
        database.row_factory = sqlite3.Row
        row = database.execute(
            "SELECT * FROM _Template_HoldGround WHERE HoldGroundId=?", (config_id,),
        ).fetchone()
        if row is None:
            raise UnsupportedHoldGround(f"unknown defense configuration {config_id!r}")
        defender = str(row["Defender"] or "").strip()
        defender_row = database.execute(
            "SELECT CharLevel FROM _Template_Monster WHERE Id=?", (defender,),
        ).fetchone()
        if defender_row is None:
            raise UnsupportedHoldGround(f"missing defender template {defender!r}")
        templates = {str(value[0]) for value in database.execute("SELECT Id FROM _Template_Monster")}
    with _connect(map_signature) as database:
        npcs = tuple(database.execute(
            "SELECT rowid, Id, Transform FROM _Instance_NPC WHERE Id=? OR _ID=?",
            (actor, actor),
        ))
        if npc_actor_id is not None:
            npcs = tuple(value for value in npcs if npc_actor_id_for_row(value[0]) == npc_actor_id)
        if len(npcs) != 1:
            raise UnsupportedHoldGround(f"defense NPC {actor!r} has {len(npcs)} matching placements")
        markers, ambiguous = {}, set()
        for marker, transform in database.execute("SELECT _ID, Transform FROM _Instance_PositionMarker"):
            marker = str(marker)
            if marker in markers:
                # Only fatal when a wave names it: unrelated map markers
                # (e.g. k0002_greylingforest's) do not affect this setup.
                ambiguous.add(marker)
            markers[marker] = transform
    waves = []
    final_wave = max((number for number in range(1, 10)
                      if str(row[f"Wave{number}"] or "").strip()), default=0)
    for number in range(1, final_wave + 1):
        pairs, blank = parse_wave(row[f"Wave{number}"])
        spawns = []
        for marker, template in pairs:
            marker = CONTENT_CORRECTIONS.get((config_id, map_id, "marker", marker), marker)
            pool = ()
            if CONTENT_CORRECTIONS.get((config_id, map_id, "template", template)) == MAP_MONSTER_POOL:
                pool = map_monster_pool(static_signature[0], map_id)
                if not pool:
                    raise UnsupportedHoldGround(f"empty monster pool for {map_id!r}")
            if marker in ambiguous:
                raise UnsupportedHoldGround(f"ambiguous defense marker {marker!r} in {config_id!r}")
            missing = ("marker" if marker not in markers else
                       "template" if not pool and template not in templates else "")
            if missing:
                raise UnsupportedHoldGround(
                    f"unresolved defense spawn {marker + ':' + template!r} in {config_id!r} "
                    f"(missing {missing})"
                )
            spawns.append(HoldGroundSpawn(marker, template, _transform(markers[marker])[0], pool))
        duration = float(row[f"Wave{number}Duration"] or 0)
        if not math.isfinite(duration) or duration < 0:
            raise UnsupportedHoldGround(f"invalid defense duration in {config_id!r}")
        waves.append(HoldGroundWave(number, duration, tuple(spawns), blank))
    if not waves:
        raise UnsupportedHoldGround(f"defense configuration {config_id!r} has no waves")
    npc_row, npc_template, transform = npcs[0]
    position, yaw = _transform(transform)
    return HoldGroundPlan(
        config_id, map_id, npc_actor_id_for_row(npc_row), str(npc_template),
        defender, max(1, int(defender_row[0] or 1)), position, yaw, tuple(waves),
    )


def _transform(raw):
    if raw is None or len(raw) != 64:
        raise UnsupportedHoldGround("invalid defense placement transform")
    values = struct.unpack("<16f", raw)
    if not all(math.isfinite(value) for value in values):
        raise UnsupportedHoldGround("nonfinite defense placement transform")
    return tuple(values[12:15]), math.atan2(values[8], values[0])
