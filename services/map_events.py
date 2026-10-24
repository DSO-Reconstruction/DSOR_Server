"""Map-local scripted events: signals, event transitions, props, triggers.

The static data is described in ``docs/GAME_EVENTS.md`` (map-local section)
and ``docs/PROP_AND_TRIGGER_COMMANDS.md``.  One :class:`MapEventRuntime` is
shared by every session on a map instance, keyed like the combat state.
World happenings reach this service either directly (client commands) or
through the presence callback (``("map_signal", ...)`` and
``("monsters_killed", ...)``) from static combat and movement code.
"""

from __future__ import annotations

import math
import random
import sqlite3
import struct
import time
from dataclasses import replace
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.services.endless_tower_buffs import start_tower_buffs, tower_kills
from dso_server.gameplay.endless_tower_effects import endless_tower_player_buffs
from dso_server.gameplay.fractured_planes_layout import (
    TOWER_AURA_RATE_MULTIPLIER,
    is_tower_controller, is_tower_hardware, is_tower_layout,
    is_tower_mechanism,
)
from dso_server.gameplay.local_map_state import (
    LOCAL_NPC_ACTOR_BASE,
    active_local_map_event_flags,
    _exit_info_entries_for_access,
    filter_parallel_world_exits_for_access,
    load_local_map_exits,
    load_local_npc_definitions,
    local_npc_actor_id,
    local_npc_info,
    map_database_path,
    native_scene_exit_variation_entries,
)
from dso_server.gameplay.boss_encounter_policy import (
    APPROACH_BOSS_MAPS,
    is_boss_spawn_template,
    load_map_encounter_policy,
    select_boss_spawn_rows,
)
from dso_server.gameplay.event_spawn_areas import event_area_spawn_rows
from dso_server.gameplay.local_monster_state import (
    _grounded_spawn_position,
    _load_monster,
    _load_runtime_definition,
    _spawn_group,
    load_local_player_only_encounter,
)
from dso_server.gameplay.map_collision_terrain import CollisionTerrainError
from dso_server.gameplay.map_event_state import (
    CLIENT_REPORTED_SIGNAL_TYPES,
    GatedSpawnRow,
    PARALLEL_WORLD_REWARD_CHEST_INSTANCE_IDS,
    WIRE_PROP_OPERATION,
    MapEventRuntime,
    MapLocalEventDefinition,
    PropRuntimeState,
    SignalContext,
    TriggerVolumeDefinition,
    ZERO_GUID,
    load_map_event_content,
    load_map_signal_catalog,
    map_key,
    prop_visible_to_player,
)
from dso_server.gameplay.quest_content import (
    QUEST_STATE_ACCEPTED,
    active_tasks,
)
from dso_server.gameplay.pathengine_navigation import NavigationError
from dso_server.protocol.character_selection import CLASS_NAMES
from dso_server.protocol.combat_protocol import (
    CombatBatch,
    Kill,
    KillServerCommand,
    encode_combat_batch,
)
from dso_server.protocol.gameplay_protocol import (
    PROP_INFO_COMMAND,
    ActorsEnterVicinity,
    ActorsEnterVicinityBatch,
    ActorPlacement,
    Message,
    MessageArgument,
    NPCBatch,
    NewNPC,
    PropInfo,
    PropInfoEntry,
    encode_actors_enter_vicinity_batch,
    encode_npc_batch,
    encode_message_batch,
    encode_screen_message,
)
from dso_server.protocol.player_init_protocol import (
    EXIT_INFO_COMMAND,
    KICK_OUT_TO_HUB_LIMIT_TIME_LEFT_COMMAND,
    OBSTACLE_UPDATE_COMMAND,
    VARIATION_INFO_COMMAND,
    EventUpdate,
    EventUpdateEntry,
    ExitInfo,
    ExitInfoEntry,
    KickOutToHubLimitTimeLeft,
    ObstacleState,
    ObstacleUpdate,
    ServerCommand,
    VariationInfo,
    encode_event_update,
    encode_endless_tower_progress,
    encode_endless_tower_buffs,
    encode_kill_monster_get_total_scores,
    command_available,
    GET_ENDLESS_TOWER_ALL_BUFFS_RESULT_COMMAND,
    KILL_MONSTER_GET_TOTAL_SCORES_COMMAND,
    encode_server_command_batch,
)
from dso_server.protocol.prop_protocol import (
    PROP_INTERACTION_COMMAND,
    PROP_STATE_ACTIVE,
    PROP_STATE_USED,
    QUEST_TRIGGER_SIGNAL_COMMAND,
    PropUpdate,
    decode_prop_interaction,
    decode_quest_trigger_signal,
    encode_discard_prop_batch,
    encode_prop_update_batch,
)
from dso_server.protocol.commerce_protocol import (
    _new_item,
    load_purchase_item_definition,
)
from dso_server.protocol.world_loot_protocol import WorldLootDrop
from dso_server.gameplay.quest_content import load_quest_catalog
from dso_server.paths import DEFAULT_QUESTS_PATH


@lru_cache(maxsize=8192)
def _monster_kill_score(static_database_path: str, template_id: str) -> int:
    """Return the static score granted when a monster template is killed."""

    try:
        with sqlite3.connect(f"{static_database_path}?mode=ro", uri=True) as database:
            row = database.execute(
                "SELECT AddScoreForKill FROM _Template_Monster WHERE Id=? LIMIT 1",
                (template_id,),
            ).fetchone()
    except (OSError, sqlite3.Error) as exc:
        _log(
            f"kill-score lookup failed for {template_id!r}: "
            f"{type(exc).__name__}: {exc}"
        )
        return 0
    if row is None:
        _log(f"kill-score template {template_id!r} is missing from static data")
        return 0
    return max(0, int(row[0] or 0))


@lru_cache(maxsize=128)
def _nefertari_death_despawn_timing(
        static_database_path: str, template_id: str):
    """Read authored death/despawn durations for Nefertari actors."""

    if "nefertari" not in str(template_id).casefold():
        return None
    try:
        with sqlite3.connect(
            f"file:{Path(static_database_path).as_posix()}?mode=ro",
            uri=True,
        ) as database:
            row = database.execute(
                "SELECT DeathDuration, DespawnDuration, DeathSequence, "
                "DespawnSequence FROM _Template_Monster WHERE Id=? LIMIT 1",
                (str(template_id),),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    if row is None:
        return None
    death_duration = max(0.0, float(row[0] or 0.0))
    despawn_duration = max(0.0, float(row[1] or 0.0))
    death_sequence = str(row[2] or "").casefold()
    despawn_sequence = str(row[3] or "").casefold()
    if death_duration <= 0.0:
        return None
    if (
        not despawn_sequence
        or despawn_sequence == death_sequence
        or despawn_duration <= 0.0
    ):
        despawn_duration = 0.0
    return death_duration, despawn_duration


@lru_cache(maxsize=32)
def _endless_tower_first_clear_rewards(static_database_path: str,
                                       access_id: str):
    """Parse one Endless Tower floor's authored first-clear reward.

    ``_Template_EndlessTower.FirstClearReward`` is an ``&``-separated list of
    ``Loot:<item>,<amount>`` and currency entries (``RC:`` for Andermant).
    Fractured Planes reuses the same floor access rows, so the reward is keyed
    by the floor's ``MapAccess``.
    """

    access_id = str(access_id or "").strip()
    if not access_id:
        return ()
    try:
        with sqlite3.connect(
            f"file:{static_database_path}?mode=ro", uri=True
        ) as database:
            row = database.execute(
                "SELECT FirstClearReward FROM _Template_EndlessTower "
                "WHERE MapAccess=? LIMIT 1",
                (access_id,),
            ).fetchone()
    except (OSError, sqlite3.Error) as exc:
        _log(
            f"first-clear reward lookup failed for {access_id!r}: "
            f"{type(exc).__name__}: {exc}"
        )
        return ()
    if row is None:
        _log(f"no Endless Tower row for floor access {access_id!r}")
        return ()
    definitions = []
    for entry in str(row[0] or "").split("&"):
        entry = entry.strip()
        if not entry:
            continue
        kind, separator, body = entry.partition(":")
        if not separator:
            _log(f"ignored malformed first-clear reward {entry!r} on {access_id!r}")
            continue
        kind = kind.strip().casefold()
        try:
            if kind == "loot":
                item_id, comma, raw_amount = body.rpartition(",")
                if not comma or not item_id.strip():
                    raise ValueError("missing loot item or amount")
                definitions.append(RewardDefinition(
                    f"endless-tower:{access_id}", item_id.strip(),
                    max(1, int(raw_amount)),
                ))
            elif kind == "rc":
                definitions.append(RewardDefinition(
                    f"endless-tower:{access_id}", "money_rc", max(1, int(body)),
                ))
            else:
                _log(
                    f"unsupported first-clear reward {entry!r} on {access_id!r}"
                )
        except ValueError as exc:
            _log(
                f"ignored first-clear reward {entry!r} on {access_id!r}: {exc}"
            )
    return tuple(definitions)


@lru_cache(maxsize=128)
def _fractured_planes_hazards(static_database_path: str, map_id: str):
    """Load damaging Fractured Planes location effects for a physical floor."""

    if not map_key(map_id).startswith("h0009_endlesstower_01_sm_"):
        return ()
    path = map_database_path(static_database_path, map_id)
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
            rows = database.execute(
                "SELECT rowid, EventSetRow, Transform, StatusEffectId "
                "FROM _Instance_LocationEffect "
                "WHERE lower(StatusEffectId) LIKE '%kill_aura' "
                "OR lower(StatusEffectId) LIKE '%_fire_aura' "
                "OR lower(StatusEffectId) LIKE '%_trap_damage%'"
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        _log(
            f"Fractured Planes hazard load failed on {map_id}: "
            f"{type(exc).__name__}: {exc}"
        )
        return ()
    result = []
    for row_id, event_set, transform, effect_id in rows:
        try:
            matrix = tuple(struct.unpack("<16f", bytes(transform)))
        except (TypeError, struct.error):
            continue
        result.append((
            TriggerVolumeDefinition(
                int(row_id), str(effect_id), int(event_set), False,
                "fractured-plane-hazard", matrix,
            ),
            str(effect_id),
        ))
    return tuple(result)


# How far a trap/debuff aura may sit from the room it belongs to. Authored
# auras sit on the corridor in front of their room, a few tens of units away
# from the density area that populates it.
HAZARD_ROOM_BIND_RADIUS = 60.0


# A fixed tower that hurts the player through an aura rather than a skill.
# ``e_dra_fortress_normal_powernode`` is the Fractured Planes laser: its
# ``Skills`` column is empty, so it never casts anything. Its authored
# ``dragan_rotation_aura_small_01`` status supplies the tower's native effect
# and remains on the actor. The server applies level-scaled
# ``MinDamage``/``MaxDamage`` through the tighter contact check below.
TOWER_AURA_MIN_INTERVAL = 0.5
# The authored visual is a rotating star beam (1 unit wide, 360 degree sweep),
# not a damaging 8-unit sphere. Keep the native effect for rendering and use
# its beam dimensions for the server hit test.
# Every authored player character has CapsuleRadius=0.5 in the static client
# data. Treat the blue beam as a finite 3D segment and test the distance from
# that segment to the player's capsule center; the earlier point-only check
# missed legitimate contacts whenever the character capsule touched the beam
# while its origin remained just outside the visual strip.
TOWER_PLAYER_COLLISION_RADIUS = 0.5
# Character skeletons place the pick/collision body roughly one unit above
# the actor origin. Extend the segment check along that vertical capsule axis
# before measuring separation, so standing on a floor laser can make contact.
TOWER_PLAYER_CAPSULE_HALF_HEIGHT = 1.0
TOWER_BEAM_CHECK_INTERVAL = 0.05


@lru_cache(maxsize=256)
def _tower_aura_spec(
    static_database_path: str, template_id: str,
) -> tuple[float, float, float, float, float] | None:
    """Return ``(range, tick, width, rotation_speed, start_angle)``.

    The authored aura also contains a broad target sphere. Damage follows only
    the visible rotating star beam using its range, width and rotation data.
    """

    try:
        with sqlite3.connect(
            f"file:{static_database_path}?mode=ro", uri=True
        ) as database:
            row = database.execute(
                "SELECT PermanentStatusEffect FROM _Template_Monster "
                "WHERE Id=? LIMIT 1",
                (str(template_id),),
            ).fetchone()
            if row is None:
                return None
            ids = tuple(
                value.strip() for value in str(row[0] or "").split(";")
                if value.strip()
            )
            if not ids:
                return None
            placeholders = ",".join("?" * len(ids))
            best = None
            for (
                aura_id, targets, radius, tick_rate, beam_width,
                rotation_speed, start_angle,
            ) in database.execute(
                "SELECT Id, COALESCE(AuraTargets, ''), COALESCE(AuraRadius, 0), "
                "COALESCE(StatusEffectTickRate, 1.0), "
                "COALESCE(AuraStarBeamWidth, 0), "
                "COALESCE(AuraRotationSpeed, 0), "
                "COALESCE(AuraStartAngle, 0) "
                f"FROM _Template_StatusEffect WHERE Id IN ({placeholders})",
                ids,
            ):
                wanted = str(targets or "").casefold()
                if "enem" not in wanted and "all" not in wanted:
                    continue
                radius = float(radius or 0.0)
                beam_width = float(beam_width or 0.0)
                if radius <= 0.0 or beam_width <= 0.0:
                    continue
                interval = max(
                    TOWER_AURA_MIN_INTERVAL,
                    float(tick_rate or 1.0) / TOWER_AURA_RATE_MULTIPLIER,
                )
                candidate = (
                    radius,
                    interval,
                    beam_width,
                    float(rotation_speed or 0.0),
                    float(start_angle or 0.0),
                )
                if best is None or candidate[0] > best[0]:
                    best = candidate
            return best
    except (OSError, sqlite3.Error) as exc:
        _log(f"tower aura lookup failed for {template_id!r}: {exc}")
        return None


@lru_cache(maxsize=64)
def _fractured_planes_hazard_rooms(static_database_path: str, map_id: str):
    """Bind every authored hazard EventSet to the room whose monsters clear it.

    ``_Instance_LocationEffect`` rows carry an ``EventSetRow``, and that set
    exists for no other reason than to hold the effect: it gates no spawn row,
    prop, or trigger volume on any tower layout. Retail releases the set from a
    room-clear or prop-interaction signal, but Fractured Planes fills those
    rooms from shared density areas, so the authored selector never fires and a
    kill aura, fire aura, or ``noskill_aura`` would stay on for the whole run.

    Each hazard is therefore bound to the nearest authored density cluster,
    which is the room it guards. Returns ``(event_set_row, room volumes)``
    pairs. A hazard with no room in reach keeps an empty volume tuple: nothing
    can prove that area safe, so only the floor-clear sweep retires it.
    """

    path = map_database_path(static_database_path, map_id)
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as database:
            effects = tuple(database.execute(
                "SELECT COALESCE(EventSetRow, -1), Transform "
                "FROM _Instance_LocationEffect"
            ))
            areas = tuple(database.execute(
                "SELECT rowid, COALESCE(EventSetRow, -1), "
                "COALESCE(SpawnListId, ''), Transform FROM _Instance_SpawnArea"
            ))
    except (OSError, sqlite3.Error) as exc:
        _log(f"Fractured Planes hazard binding failed on {map_id}: {exc}")
        return ()
    anchors: dict[int, list[tuple[float, float]]] = {}
    for event_set, transform in effects:
        event_set = int(event_set)
        if event_set < 0:
            continue
        try:
            matrix = struct.unpack("<16f", bytes(transform))
        except (TypeError, struct.error):
            continue
        anchors.setdefault(event_set, []).append((matrix[12], matrix[14]))
    if not anchors:
        return ()
    # One room is one stack of co-located density areas (``Fire_1``,
    # ``Ice_1``, ... all share a transform).
    rooms: dict[tuple[float, float], list[TriggerVolumeDefinition]] = {}
    for row_id, event_set, name, transform in areas:
        try:
            matrix = tuple(struct.unpack("<16f", bytes(transform)))
        except (TypeError, struct.error):
            continue
        rooms.setdefault(
            (round(matrix[12], 1), round(matrix[14], 1)), []
        ).append(TriggerVolumeDefinition(
            int(row_id), str(name), int(event_set), False,
            "fractured-plane-hazard-room", matrix,
        ))
    if not rooms:
        return tuple((event_set, ()) for event_set in sorted(anchors))
    bindings = []
    for event_set, points in sorted(anchors.items()):
        centre = (
            sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points),
        )
        nearest = min(
            rooms,
            key=lambda key: (key[0] - centre[0]) ** 2 + (key[1] - centre[1]) ** 2,
        )
        if math.hypot(nearest[0] - centre[0], nearest[1] - centre[1]) > HAZARD_ROOM_BIND_RADIUS:
            bindings.append((event_set, ()))
            continue
        bindings.append((event_set, tuple(rooms[nearest])))
    return tuple(bindings)


def _fractured_planes_floor_number_from_map(map_id: str) -> int:
    prefix = "h0009_endlesstower_01_sm_"
    key = map_key(map_id)
    suffix = key.removeprefix(prefix)
    return int(suffix) if key.startswith(prefix) and suffix.isdecimal() else 0


@lru_cache(maxsize=128)
def _fractured_planes_floor_regions(static_database_path: str, map_id: str,
                                    room_event_rows: tuple[tuple[int, str, str], ...],
                                    room_start_props: tuple[tuple[str, tuple[float, float, float]], ...] = ()):
    """Map authored room starts to their nearby spawn-area volumes.

    Tower room waves are pre-materialized from shared density areas. Those
    rows use EventSetRow=-1, so looking up only areas attached to the room
    EventSet misses blockers and can leave a trap gate open indefinitely.
    Prefer explicit EventSet associations, then map the room to the nearest
    authored room-start prop in this physical map.
    """

    floor = _fractured_planes_floor_number_from_map(map_id)
    if not floor:
        return ()
    path = map_database_path(static_database_path, map_id)
    row_to_room = {
        row_id: (room, end_signal)
        for row_id, room, end_signal in room_event_rows
    }
    regions = {}
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as database:
            spawn_areas = tuple(database.execute(
                "SELECT rowid, COALESCE(EventSetRow, -1), "
                "COALESCE(SpawnListId, ''), Transform FROM _Instance_SpawnArea"
            ))
            for row_id, event_set, name, transform in spawn_areas:
                room_data = row_to_room.get(int(event_set))
                if room_data is None:
                    continue
                room, signal = room_data
                matrix = tuple(struct.unpack("<16f", bytes(transform)))
                regions.setdefault(signal, {})[matrix] = TriggerVolumeDefinition(
                    int(row_id), str(name), int(event_set), False,
                    "fractured-plane-room", matrix,
                )
            # Most floors store their shared enemy density areas with
            # EventSetRow=-1. Their start props still provide reliable room
            # coordinates, allowing the corresponding already-materialized
            # enemies to be found and force-published to the player.
            area_matrices = tuple(
                (int(row_id), str(name),
                 tuple(struct.unpack("<16f", bytes(transform))))
                for row_id, _event_set, name, transform in spawn_areas
            )
            for room_event_set, room, signal in room_event_rows:
                if regions.get(signal):
                    continue
                prop_id = f"h0009_endlesstower_{room}_start".casefold()
                prop_position = next((
                    position for instance_id, position in room_start_props
                    if instance_id.casefold() == prop_id
                ), None)
                if prop_position is None or not area_matrices:
                    continue
                nearest = min(
                    area_matrices,
                    key=lambda area: (
                        (area[2][12] - prop_position[0]) ** 2
                        + (area[2][14] - prop_position[2]) ** 2
                    ),
                )
                center = (nearest[2][12], nearest[2][14])
                if (
                    (center[0] - prop_position[0]) ** 2
                    + (center[1] - prop_position[2]) ** 2
                    > 45.0 ** 2
                ):
                    continue
                for row_id, name, matrix in area_matrices:
                    if (
                        abs(matrix[12] - center[0]) > 0.25
                        or abs(matrix[14] - center[1]) > 0.25
                    ):
                        continue
                    regions.setdefault(signal, {})[matrix] = TriggerVolumeDefinition(
                        row_id, name, int(room_event_set), False,
                        "fractured-plane-room", matrix,
                    )
    except (OSError, sqlite3.Error, TypeError, struct.error) as exc:
        _log(f"Fractured Planes room-area load failed on {map_id}: {exc}")
        return ()
    return tuple((signal, tuple(volumes.values())) for signal, volumes in regions.items())


@lru_cache(maxsize=8)
def _fractured_planes_floor_one_regions(static_database_path: str):
    """Floor one has hand-authored room labels instead of per-room event sets."""

    path = map_database_path(static_database_path, "h0009_endlesstower_01_sm_01")
    suffixes = {
        "fire_1": "01", "fire_2": "01", "ice_1": "01",
        "sliver_gold": "05", "ocean": "05",
        "light": "03", "dragon": "03", "shadow": "03",
    }
    regions = {}
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as database:
            for row_id, name, transform in database.execute(
                "SELECT rowid, SpawnListId, Transform FROM _Instance_SpawnArea"
            ):
                suffix = suffixes.get(str(name).casefold())
                if suffix is None:
                    continue
                matrix = tuple(struct.unpack("<16f", bytes(transform)))
                regions.setdefault(suffix, {})[matrix] = TriggerVolumeDefinition(
                    int(row_id), str(name), -1, False,
                    "fractured-plane-room", matrix,
                )
    except (OSError, sqlite3.Error, TypeError, struct.error) as exc:
        _log(f"Fractured Planes floor-one room-area load failed: {exc}")
        return ()
    return tuple(
        (f"h0009_endlesstower_01_monster_{suffix}_end_signal", tuple(volumes.values()))
        for suffix, volumes in regions.items()
    )


def _fractured_planes_room_event_rows(runtime) -> tuple[tuple[int, str, str], ...]:
    """Return room spawn rows with the exact authored clear-signal name."""

    result = []
    for row_id, event_set in runtime.content.event_sets.items():
        for event_key in event_set.events:
            marker = "_room_"
            if not event_key.casefold().startswith("h0009_endlesstower_01_room_"):
                continue
            room = event_key.rsplit(marker, 1)[-1]
            if room:
                event = runtime.catalog.event(event_key)
                end_signal = (
                    event.end_signals[0]
                    if event is not None and event.end_signals
                    else f"{event_key}_end_signal"
                )
                # Floors 2-100 reuse one of ten physical maps. The active map
                # event namespace comes from its authored event key (for
                # example ``..._01_room_06``), not the logical floor number
                # or the physical map suffix (``sm_06``). Using either floor
                # number here emits an end signal no authored trap gate listens
                # to. Some rooms (such as room 08) also use a special authored
                # end signal, so take it from the event definition when present.
                result.append((
                    int(row_id),
                    f"{int(room):02d}" if room.isdecimal() else room,
                    end_signal,
                ))
    return tuple(result)


MAP_EVENT_LOG_PREFIX = f"[{GAMEPLAY_SERVICE_NAME}] map-event"
# How far a gated spawn transform may be moved to reach walkable ground.
EVENT_SPAWN_SNAP_DISTANCE = 4.0
# Authored rows whose template already stands within this distance are the
# same actor, materialized by the initial population (boss rooms bypass the
# event gate so the boss exists on entry).
EVENT_SPAWN_DUPLICATE_RADIUS = 1.5
_FORCE_ANNOUNCE_EVENT_ACTORS = frozenset({
    "a0001_champion_undead_mage_01",
    "a0005_champion_grimmag_01_q_waytogrimmagstone_03",
    "f4203_boss_06_ram",
    # Fyeborough's story sentinel is gated by a quest event and can already
    # exist in the encounter before that event releases it. Force its native
    # NewMonster registration when the EventSet opens so the client does not
    # show only the authored spawn effect while omitting the actor model.
    "pw207_sentinel_shadow_father_01_q_way_to_village_02",
    # The Lythira spawn transform is the boss's authored arena position and
    # can be outside the ordinary vicinity radius from the entry point.
    "fp_boss_01_lythira",
})
_TUTORIAL_CULTIST_TEMPLATE = "a0001_champion_undead_mage_01"
_PARALLEL_WORLD_EXIT_BOSSES = {
    "pw001_grimmagstone_01_dun": (
        "pw001_01_leader_undeadmage_heroic", "pw001_spawn_exit",
    ),
    "pw002_alliance_fortress_01_dun": (
        "pw002_01_leader_warrior_heroic", "pw002_spawn_exit",
    ),
    "pw004_hagastove_grotto_01_dun": (
        "pw004_01_leader_meanie_summoner_heroic", "pw004_spawn_exit",
    ),
    "pw005_caverns_loxley_01_dun": (
        "pw005_01_leader_troll_heroic", "pw005_spawn_exit",
    ),
    "pw007_stonekeep_01_dun": (
        "pw007_01_leader_undead_mage_heroic", "pw007_spawn_exit",
    ),
    "pw008_kingscrypt_01_dun": (
        "pw008_01_leader_ranger_heroic", "pw008_spawn_exit",
    ),
    "pw010_wildcave_01_dun": (
        "pw010_01_leader_stag_heroic", "pw010_spawn_exit",
    ),
    "pw011_eternal_grove_01_dun": (
        "pw011_01_leader_boar_heroic", "pw011_spawn_exit",
    ),
    "pw013_wizard_garden_01_dun": (
        "pw013_01_leader_jabbax_priest_heroic",
        "pw013_spawn_exit_to_pw014",
    ),
    "pw014_cult_tower_01_dun": (
        "pw014_01_leader_swamp_thing_heroic",
        "pw014_spawn_exit_to_pw015",
    ),
    "pw016_teganswall_01_dun": (
        "pw016_01_leader_tank", "pw016_spawn_exit",
    ),
    "pw017_dragonslair_01_dun": (
        "pw017_01_leader_dragon_brood_warrior_01", "pw017_spawn_exit",
    ),
    "pw019_hailstone_mountains_01_dun": (
        "pw019_01_leader_lightning_minion_01", "pw019_spawn_exit",
    ),
    "pw020_eternal_watch_01_dun": (
        "pw020_01_leader_soulless_norseman_01", "pw020_spawn_exit",
    ),
    "pw022_mount_suvius_01_dun": (
        "pw022_leader_centaur_01", "pw022_spawn_exit",
    ),
    "pw023_atlantis_above_water_01_dun": (
        "pw023_leader_beastman_lobster_01", "pw023_spawn_exit",
    ),
    "pw025_ship_graveyard_01_dun": (
        "", "pw025_spawn_exit",
    ),
    "pw026_heart_of_atlantis_01_dun": (
        "", "pw026_spawn_exit",
    ),
}
_PARALLEL_WORLD_BOSS_REWARD_CHESTS = {
    "pw003_grimmag_01_boss": (
        "boss_grimmag", "pw003_spawn_chest",
    ),
    "pw006_arachna_01_boss": (
        "boss_arachna", "pw006_spawn_chest",
    ),
    "pw009_heredur_01_boss": (
        "boss_heredur", "pw009_spawn_chest",
    ),
    "pw012_bearach_01_boss": (
        "boss_bearach", "pw012_spawn_chest",
    ),
    "pw015_khalys_boss_01": (
        "boss_khalys", "pw015_spawn_chest",
    ),
    "pw018_herold_01_boss": (
        "boss_herald", "pw018_spawn_chest",
    ),
    "pw021_sigrismarr_01_boss": (
        "boss_sigrismarr", "pw021_spawn_chest",
    ),
    "pw024_medusa_01_boss": (
        "boss_medusa", "pw024_spawn_chest",
    ),
    "pw027_gorga_01_boss": (
        "boss_gorga", "pw027_spawn_chest",
    ),
}
_BLOOD_CHEST_GEM_TEMPLATES = (
    "gem_amethyst_e", "gem_cyanit_e", "gem_diamond_e",
    "gem_onyx_e", "gem_ruby_e",
)
_BLOOD_CHEST_LOOT_LIST = "chest_lootlist_bloodshed"


def _log(message: str) -> None:
    print(f"{MAP_EVENT_LOG_PREFIX} {message}")


@lru_cache(maxsize=4)
def _exit_access_difficulties(database_path: str) -> dict[str, int]:
    path = Path(database_path).resolve().as_uri()
    with closing(sqlite3.connect(f"{path}?mode=ro", uri=True)) as database:
        return {
            str(access_id): int(difficulty or 0)
            for access_id, difficulty in database.execute(
                "SELECT Id, Difficulty FROM _Template_MapAccessGroup"
            )
        }


def _prop_info_entry(
    state, *, game_tick: int = 0, operation: str | None = None,
) -> PropInfoEntry:
    """One PropInfo record in the retail shape (docs/PROP_AND_TRIGGER_COMMANDS.md §2).

    ``operation`` is the quest switch id the viewer sees the prop under
    (``""`` for a plain prop); by default the definition's own switch id.
    """

    definition = state.definition
    if operation is None:
        operation = definition.quest_switch_id
    indication = 0xFF
    if operation:
        catalog = load_quest_catalog(DEFAULT_QUESTS_PATH)
        binding = next((b for b in catalog.switches_for_prop(definition.instance_id)
                        if b.switch.switch_id == operation), None)
        quest = catalog.quest(binding.quest_id) if binding else None
        indication = quest.quest_type_id if quest else 6
    return PropInfoEntry(
        prop_id=definition.prop_id,
        template_id=definition.template_id,
        instance_guid=definition.instance_guid,
        field_20=0xFFFFFFFF,
        field_24=int(state.state) & 0xFF,
        field_28=int(game_tick) & 0xFFFFFFFF,
        position=definition.position,
        rotation=definition.rotation,
        # Quest targets carry their switch id and indication 6, as retail's
        # tutorial props did (docs/QUEST_PROTOCOL.md §8).
        field_48=operation or WIRE_PROP_OPERATION,
        field_50="",
        field_58=0,
        field_5c=indication,
        field_60=0xFFFFFFFF,
    )


def _session_game_tick(s: Session) -> int:
    return int(getattr(s, "gameplay_chase_server_tick", 0) or 0) & 0xFFFFFFFF


def _send_prop_info(viewer: Session, states, *, label: str) -> None:
    """Retail creates and refreshes mid-map props with PropInfo, never NewProp.

    Quest-controlled props are filtered per viewer (services/quests.py), and
    map-variation props are limited to the viewer's difficulty tier so
    overlapping authoring variants do not stack in one scene.
    """

    states = tuple(
        state for state in states
        if prop_visible_to_player(state.definition, viewer)
    )
    personalized = tuple(_prop_state_for_viewer(viewer, state) for state in states)
    _send_prop_info_entries(
        viewer, CharacterService._quest_prop_entries(viewer, personalized), label=label,
    )


def _prop_state_for_viewer(viewer: Session, state: PropRuntimeState) -> PropRuntimeState:
    """Return this player's view of an individually used prop."""

    definition = state.definition
    if not definition.individual_usage:
        return state
    runtime = getattr(viewer, "gameplay_map_event_runtime", None)
    if runtime is None:
        return state
    key = (
        _individual_prop_player_id(viewer), int(definition.prop_id),
    )
    with runtime.lock:
        used = key in runtime.individual_prop_usage
    return (
        PropRuntimeState(definition, PROP_STATE_USED)
        if used else state
    )


def _individual_prop_player_id(viewer: Session) -> int:
    """Stable per-character key, including across a reconnect to the instance."""

    character_id = getattr(viewer, "selected_character_id", None)
    if character_id is None:
        character = getattr(viewer, "gameplay_character", None)
        character_id = getattr(character, "character_id", None)
    if character_id is None:
        character_id = getattr(viewer, "gameplay_remote_actor_id", 0)
    if character_id:
        return int(character_id)
    return id(viewer)


def _send_prop_info_entries(viewer: Session, pairs, *, label: str) -> None:
    """Send ``(state, operation)`` pairs as PropInfo records, 16 per command."""

    tick = _session_game_tick(viewer)
    entries = tuple(
        _prop_info_entry(state, game_tick=tick, operation=operation)
        for state, operation in pairs
    )
    for start in range(0, len(entries), 16):
        chunk = entries[start:start + 16]
        vicinity_packet, _ = encode_actors_enter_vicinity_batch(
            ActorsEnterVicinityBatch(
                ActorsEnterVicinity(tuple(entry.prop_id for entry in chunk)),
                actor_id=0,
                commands=(),
            )
        )
        CharacterService._send_world_loot_state_batch(
            viewer, vicinity_packet, len(vicinity_packet) * 8,
            label=f"{label}-actor-vicinity",
        )
        packet, packet_bits = encode_server_command_batch(
            (
                ServerCommand(
                    PROP_INFO_COMMAND,
                    PropInfo(chunk),
                    LOCAL_PLAYER_ACTOR_ID,
                ),
            ),
            include_terminator=True,
        )
        CharacterService._send_world_loot_state_batch(
            viewer, packet, packet_bits, label=label
        )


class MapEventService:
    """Signal evaluation and event-set transitions for one map instance."""

    def _is_fractured_planes_run(self, s: Session) -> bool:
        """Recover the Fractured Planes marker after entry reconnects."""

        if getattr(s, "gameplay_fractured_planes_run", False):
            return True
        access_id = str(getattr(s, "gameplay_map_access_id", "") or "")
        if not access_id.casefold().startswith("h0009_endlesstower_floor_"):
            return False
        try:
            active = CharacterService._event_route_satisfies_map_gate(
                CharacterService._event_catalog(self), access_id,
            )
            if active:
                s.gameplay_fractured_planes_run = True
            return bool(active)
        except (OSError, ValueError, sqlite3.Error):
            return False

    def _clear_fractured_planes_floor_one_traps(
            self, s: Session, runtime: MapEventRuntime) -> None:
        """Disable the legacy Endless Tower hazard sets on the FP clear gate."""

        with runtime.lock:
            if "fracturedplanes_floor_001_traps_cleared" in runtime.fired_once:
                return
            runtime.fired_once.add("fracturedplanes_floor_001_traps_cleared")
        # The hazard sets are split across three monster-end events and two
        # trap-removal events in the reused Tower map. FP uses different room
        # templates, so its authored room signals never reliably reach them.
        for suffix in ("01", "03", "05"):
            self._emit_map_signal(
                s, runtime,
                f"h0009_endlesstower_01_monster_{suffix}_end_signal",
            )
        for room in ("02", "06"):
            self._emit_map_signal(
                s, runtime,
                f"h0009_endlesstower_01_room_{room}_interact_signal",
            )
        _log("Fractured Planes floor 001 trap event sets disabled")

    def _release_fractured_planes_hazards(
            self, s: Session, runtime: MapEventRuntime, combat, *,
            population, alive, force: bool = False) -> None:
        """Retire trap and debuff auras whose room no longer holds enemies.

        ``force`` skips the room test and clears every remaining hazard, which
        is what the floor-clear gate uses.
        """

        bindings = _fractured_planes_hazard_rooms(
            str(combat.encounter.static_database_path), runtime.map_id,
        )
        if not bindings:
            return
        # Only retire sets whose sole purpose is the effect itself. A set that
        # also gates a spawn row, prop, or trigger volume is authored content
        # with its own release path, and turning it off here would desync it.
        content = runtime.content
        gated_rows = (
            {row.event_set_row for row in content.spawn_rows}
            | {prop.event_set_row for prop in content.props}
            | {volume.event_set_row for volume in content.trigger_volumes}
        )
        released = []
        for event_set, volumes in bindings:
            if event_set in gated_rows:
                continue
            with runtime.lock:
                if event_set not in runtime.enabled_event_sets:
                    continue
            if not force:
                members = {
                    actor_id for actor_id, monster in population
                    if not is_tower_mechanism(
                        combat.encounter.static_database_path, runtime.map_id,
                        monster.template_id,
                    )
                    if not monster.rank_id.casefold().startswith("rank_boss")
                    if any(volume.contains((
                        monster.position[0], volume.matrix[13], monster.position[2],
                    )) for volume in volumes)
                }
                # An empty room means the authored density never populated this
                # spot (a miniboss arena, a corridor prop), so leave the hazard
                # to the floor-clear sweep instead of guessing.
                if not members or members & alive:
                    continue
            released.append(event_set)
        if not released:
            return
        with runtime.lock:
            removed = runtime.suppress_event_sets(released)
        if removed:
            _log(
                "Fractured Planes trap event sets released on "
                f"{runtime.map_id}: {sorted(removed)}"
            )
            # Suppressing an EventSet only changes the server's spawn/hazard
            # view. The client still needs the authored map-local event that
            # turns the corresponding flag on, or its trap visuals remain
            # latched even though the server has retired their damage. Floor
            # layouts encode these release events as forbidden flags on the
            # active hazard sets (for example, sm_05's
            # ``...closepermtrap_01`` event).
            release_signals = set()
            for event_set in removed:
                definition = content.event_sets.get(event_set)
                if definition is None:
                    continue
                for event_id in definition.forbidden:
                    event = runtime.catalog.event(event_id)
                    if event is None or not event.applies_to(runtime.map_id):
                        continue
                    release_signals.update(event.start_signals)
            for signal_id in sorted(release_signals):
                self._emit_map_signal(s, runtime, signal_id)

    def _schedule_nefertari_death_despawn(
            self, s: Session, combat, dispatches, viewers) -> None:
        """Play authored Nefertari despawn sequences after their death clips."""

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        scheduler = getattr(runtime, "scheduler", None)
        if runtime is None or scheduler is None or combat is None:
            return
        visible_viewers = tuple(
            viewer
            for viewer in viewers
            if viewer.gameplay_combat_state is combat
        )
        if not visible_viewers:
            return
        for resolution, _label, _loot in dispatches:
            if not resolution.killed:
                continue
            actor_id = int(resolution.target_actor_id)
            with combat.lock:
                monster = combat.monsters.get(actor_id)
                template_id = str(
                    getattr(monster, "template_id", "") or ""
                )
            timing = _nefertari_death_despawn_timing(
                str(combat.encounter.static_database_path), template_id,
            )
            if timing is None:
                continue
            death_duration, despawn_duration = timing
            interested = tuple(
                viewer
                for viewer in visible_viewers
                if actor_id in viewer.gameplay_spawned_monster_actor_ids
            )
            if not interested:
                continue
            with runtime.lock:
                timers = getattr(
                    runtime, "nefertari_despawn_timers", None,
                )
                if timers is None:
                    timers = {}
                    runtime.nefertari_despawn_timers = timers
                if actor_id in timers:
                    continue
                timer = scheduler.timer(
                    max(0.05, death_duration),
                    self._begin_nefertari_despawn,
                    args=(
                        runtime, combat, actor_id,
                        tuple(float(value) for value in resolution.position),
                        interested, despawn_duration,
                    ),
                )
                timers[actor_id] = timer
            timer.start()

    def _begin_nefertari_despawn(
            self, runtime, combat, actor_id, position, viewers,
            despawn_duration) -> None:
        """Switch the dead Nefertari actor to its authored vanish sequence."""

        if not self._map_event_runtime_is_live(runtime):
            return
        with runtime.lock:
            timers = getattr(runtime, "nefertari_despawn_timers", {})
            timers.pop(int(actor_id), None)
        with combat.lock:
            if combat.monster_health.get(int(actor_id), 0) > 0:
                return
            position = tuple(
                float(value)
                for value in combat.monster_positions.get(
                    int(actor_id), position,
                )
            )
        if float(despawn_duration) <= 0.0:
            self._finish_nefertari_despawn(
                runtime, combat, int(actor_id), viewers,
            )
            return
        sessions = tuple(
            viewer
            for viewer in viewers
            if viewer.connected
            and viewer.gameplay_combat_state is combat
            and getattr(viewer, "gameplay_map_event_runtime", None) is runtime
        )
        if not sessions:
            return
        tick = max(
            int(getattr(viewer, "gameplay_chase_server_tick", 0) or 0)
            for viewer in sessions
        )
        command = KillServerCommand(
            Kill(
                server_tick=tick,
                hit_tags=(),
                animation_time=max(1, int(math.ceil(despawn_duration * 25))),
                field_4c=0,
                killer_actor_id=0,
                position=position,
                skill_id=0,
                projectile_id=0xFFFFFFFF,
                field_78=True,
            ),
            int(actor_id),
        )
        packet, packet_bits = encode_combat_batch(CombatBatch((command,)))
        for viewer in sessions:
            CharacterService._send_world_loot_state_batch(
                viewer, packet, packet_bits,
                label="nefertari-authored-despawn",
            )
        timer = runtime.scheduler.timer(
            max(0.05, float(despawn_duration)),
            self._finish_nefertari_despawn,
            args=(runtime, combat, int(actor_id), sessions),
        )
        timer.start()

    def _finish_nefertari_despawn(
            self, runtime, combat, actor_id, viewers) -> None:
        """Remove actors after the authored Nefertari despawn animation."""

        if not self._map_event_runtime_is_live(runtime):
            return
        packet, packet_bits = encode_discard_monster_batch((int(actor_id),))
        for viewer in viewers:
            if (
                viewer.connected
                and viewer.gameplay_combat_state is combat
                and getattr(viewer, "gameplay_map_event_runtime", None)
                is runtime
            ):
                CharacterService._send_world_loot_state_batch(
                    viewer, packet, packet_bits,
                    label="nefertari-despawn-discard",
                )

    def _update_fractured_planes_clear_state(self, s: Session, monsters) -> None:
        """Resolve room clears and the spawned Rift Lord independently."""

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        combat = getattr(s, "gameplay_combat_state", None)
        if runtime is None or combat is None or not self._is_fractured_planes_run(s):
            return
        with combat.lock:
            population = tuple(combat.monsters.items())
            alive = frozenset(combat.alive_actor_ids)
        # Geometry follows the physical layout reused by floors 11, 21, ...,
        # not the logical floor number shown in the portal.
        map_floor = _fractured_planes_floor_number_from_map(runtime.map_id)
        if map_floor:
            if map_floor == 1:
                regions = _fractured_planes_floor_one_regions(
                    str(combat.encounter.static_database_path)
                )
            else:
                regions = _fractured_planes_floor_regions(
                    str(combat.encounter.static_database_path), runtime.map_id,
                    _fractured_planes_room_event_rows(runtime),
                    tuple(
                        (str(prop.instance_id), tuple(prop.position))
                        for prop in runtime.content.props
                    ),
                )
            for signal, volumes in regions:
                # Use authored home positions: a chased monster still belongs
                # to its room. Spawn areas are upright; their Y bounds describe
                # editor volumes, not the final collision-floor height.
                # Every density room is materialized when the map loads, so a
                # living member really is standing in the room even if this
                # client has not walked close enough to receive it yet. Only
                # fixed hardware is ignored: it can never be killed, and the
                # trap must not wait for it. A dead Nefertari corpse remains in
                # this population too, but its zero HP keeps it out of
                # ``alive``.
                members = {
                    actor_id for actor_id, monster in population
                    if not is_tower_mechanism(
                        combat.encounter.static_database_path, runtime.map_id,
                        monster.template_id,
                    )
                    if any(volume.contains((
                        monster.position[0], volume.matrix[13], monster.position[2],
                    )) for volume in volumes)
                }
                if not members:
                    continue
                blockers = members & alive
                if blockers:
                    blocked_by = tuple(
                        (
                            actor_id,
                            combat.monsters[actor_id].template_id,
                            tuple(round(float(value), 2) for value in
                                  combat.monster_positions[actor_id]),
                        )
                        for actor_id in sorted(blockers)
                    )
                    _log(
                        f"Fractured Planes room still has live actors: "
                        f"{signal}, blockers={blocked_by!r}"
                    )
                    # Encounter populations are loaded before the client asks
                    # for nearby actor models. A living room member can
                    # therefore block the trap gate while its NewMonster has
                    # never reached this client (for example after a distant
                    # spawn or a missed vicinity request). Publish only the
                    # actual room blockers so the player can find and kill
                    # them instead of leaving an invisible objective behind.
                    groups = {}
                    for actor_id in blockers:
                        group = combat.group_for_actor(actor_id)
                        if group is not None:
                            groups.setdefault(id(group), (group, set()))[1].add(
                                int(actor_id)
                            )
                    for viewer in self._map_event_sessions(runtime):
                        for group, actor_ids in groups.values():
                            self._announce_runtime_group(
                                viewer, combat, group,
                                force_actor_ids=actor_ids,
                            )
                    continue
                with runtime.lock:
                    if signal in runtime.fired_once:
                        continue
                    runtime.fired_once.add(signal)
                _log(f"Fractured Planes room cleared: {signal}, actors={len(members)}")
                self._emit_map_signal(s, runtime, signal)
                if map_floor == 1:
                    # Floor 1's two trap-removal EventSets are wired to
                    # PlayerInteractedProp signals for their nearby room
                    # starts, while the kill waves use monster_*_end signals.
                    # Fractured Planes populates those rooms dynamically, so
                    # the retail static-template selector never emits the
                    # interaction signal after the last room mob dies. Bind
                    # each cleared density region to its authored trap gate.
                    trap_unlock_signal = {
                        "h0009_endlesstower_01_monster_01_end_signal":
                            "h0009_endlesstower_01_room_02_interact_signal",
                        "h0009_endlesstower_01_monster_05_end_signal":
                            "h0009_endlesstower_01_room_06_interact_signal",
                    }.get(signal.casefold())
                    if trap_unlock_signal:
                        self._emit_map_signal(s, runtime, trap_unlock_signal)
                        _log(
                            "Fractured Planes floor 001 cleared-room trap "
                            f"unlock: {trap_unlock_signal}"
                        )

        # Trap and debuff auras are released room by room, so a cleared room
        # never keeps a kill aura or a ``noskill_aura`` on the path to the next
        # one. Once every active enemy is down the whole floor is swept: the
        # remaining hazards are the ones whose authored release never fires
        # (miniboss arenas, corridor props) and they must not outlive the run.
        living = {
            actor_id for actor_id, monster in population
            if actor_id in alive
            if not is_tower_mechanism(
                combat.encounter.static_database_path, runtime.map_id,
                monster.template_id,
            )
            if not monster.rank_id.casefold().startswith("rank_boss")
        }
        self._release_fractured_planes_hazards(
            s, runtime, combat,
            population=population, alive=alive,
            force=not living,
        )

        with runtime.lock:
            phases = {
                key for key in runtime.running
                if key.startswith("fractured_levelmap_bossappear_tower_")
            }
            if map_key(runtime.map_id) == "fp0001_boss_01":
                if "fp_boss_01_lythira_spawn" in runtime.running:
                    phases.add("fp_boss_01_lythira_spawn")
            phase_actor_ids = set().union(*(
                runtime.event_set_actor_ids.get(row, set())
                for row, event_set in runtime.content.event_sets.items()
                if event_set.required & phases
            ))
        if not phases:
            # ``fp0001_boss_01`` keeps the Rift Lord in its load-time
            # population, so she can be killed before the arena hit trigger
            # that announces her ever runs. Fall back to the killed boss actor
            # itself, but only once the floor really has nothing left to fight,
            # so a premature boss kill cannot skip the trash.
            if not map_floor and map_key(runtime.map_id) != "fp0001_boss_01":
                return
            boss_ids = {
                actor_id for actor_id, monster in population
                if monster.rank_id.casefold().startswith("rank_boss")
            }
            if not boss_ids or boss_ids & alive or living:
                return
        else:
            boss_ids = {
                actor_id for actor_id, monster in population
                if actor_id in phase_actor_ids
                and monster.rank_id.casefold().startswith("rank_boss")
            }
            killed_ids = {int(monster.actor_id) for monster in monsters}
            if not boss_ids & killed_ids or boss_ids & alive:
                return
        # EventSet changes control scene entities; the native objective panel
        # and floor picker require their own completion/progression commands.
        for phase in phases:
            definition = runtime.catalog.event(phase)
            if definition is not None:
                self._stop_map_local_event(runtime, definition)
        self._complete_fractured_planes_floor(runtime)
        self._emit_map_signal(s, runtime, "h0009_endlesstower_boss_spawn_exit_signal")
        if map_floor == 1:
            self._clear_fractured_planes_floor_one_traps(s, runtime)
        # Nothing may still hurt the player on the way to the opened exit.
        self._release_fractured_planes_hazards(
            s, runtime, combat,
            population=population, alive=alive, force=True,
        )
        _log(f"Fractured Planes Rift Lord defeated: actors={sorted(boss_ids)}; exit opened")

    @staticmethod
    def _fractured_planes_floor_number(s: Session) -> int:
        access_id = str(getattr(s, "gameplay_map_access_id", "") or "").casefold()
        prefix = "h0009_endlesstower_floor_"
        if access_id.startswith(prefix) and access_id[len(prefix):].isdecimal():
            return int(access_id[len(prefix):])
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        physical = str(getattr(runtime, "map_id", "")).casefold()
        prefix = "h0009_endlesstower_01_sm_"
        if physical.startswith(prefix) and physical[len(prefix):].isdecimal():
            return int(physical[len(prefix):])
        return 0

    @staticmethod
    def _fractured_planes_progress(s: Session) -> tuple[int, int]:
        """Use the persisted per-character claim map for native floor progress."""

        with s.flow_lock:
            purchases = dict(getattr(s, "gameplay_timed_offer_purchases", {}))
        claimed = 0
        prefix = "endless-tower-first-clear:h0009_endlesstower_floor_"
        for key, count in purchases.items():
            suffix = str(key).removeprefix(prefix)
            if str(key).startswith(prefix) and suffix.isdecimal() and count:
                claimed = max(claimed, min(100, int(suffix)))
        cleared = max(claimed, min(100, int(purchases.get(
            "endless-tower-cleared-floor", 0,
        ))))
        return cleared, claimed

    def _send_fractured_planes_progress(self, s: Session) -> None:
        cleared, claimed = self._fractured_planes_progress(s)
        packet, packet_bits = encode_endless_tower_progress(
            cleared, claimed, actor_id=LOCAL_PLAYER_ACTOR_ID,
        )
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label="fractured-planes-floor-progress",
        )

    def _send_fractured_planes_effect_choices(self, s: Session) -> None:
        with s.flow_lock:
            character = s.gameplay_character
        if character is None:
            return
        if not command_available(GET_ENDLESS_TOWER_ALL_BUFFS_RESULT_COMMAND):
            return
        selection = endless_tower_player_buffs(
            self.static_database_path, "",
            character_id=int(character.character_id),
        )
        packet, bits = encode_endless_tower_buffs(
            selection.choices, actor_id=LOCAL_PLAYER_ACTOR_ID,
        )
        CharacterService._send_world_loot_state_batch(
            s, packet, bits, label="fractured-planes-effect-choices",
        )

    def _complete_fractured_planes_floor(self, runtime) -> None:
        with runtime.lock:
            if runtime.fractured_planes_floor_completed:
                return
            runtime.fractured_planes_floor_completed = True
            timer = runtime.fractured_planes_limit_timer
            runtime.fractured_planes_limit_timer = None
        if timer is not None:
            timer.cancel()
        # The native timer handler treats zero as success: it calls the
        # widget's phase-three setter (pf_phase_three = "Level completed").
        # Keep the actual run deadline for the next floor's remaining time.
        packet, packet_bits = encode_server_command_batch((ServerCommand(
            KICK_OUT_TO_HUB_LIMIT_TIME_LEFT_COMMAND,
            KickOutToHubLimitTimeLeft(0), 1,
        ),))
        for viewer in self._map_event_sessions(runtime):
            floor = self._fractured_planes_floor_number(viewer)
            if not 1 <= floor <= 100:
                continue
            with viewer.flow_lock:
                purchases = viewer.gameplay_timed_offer_purchases
                purchases["endless-tower-cleared-floor"] = max(
                    floor, int(purchases.get("endless-tower-cleared-floor", 0)),
                )
            CharacterService._request_account_save(viewer, "Fractured Planes floor cleared")
            self._send_fractured_planes_progress(viewer)
            CharacterService._send_world_loot_state_batch(
                viewer, packet, packet_bits, label="fractured-planes-floor-completed",
            )
            _log(f"Fractured Planes floor {floor:03d} completed; native floor selection unlocked")

    def _grant_fractured_planes_first_clear(self, s: Session) -> None:
        """Grant the cleared floor's first-completion reward, once per player.

        The reward is delivered only after the client confirms the portal's
        first-clear prompt. The claim is recorded in the persisted timed-offer
        claim map, which makes "first completion" per character rather than
        per run.
        """

        access_id = str(getattr(s, "gameplay_map_access_id", "") or "")
        rewards = _endless_tower_first_clear_rewards(
            self.static_database_path, access_id
        )
        if not rewards:
            _log(f"no first-clear reward available for {access_id!r}")
            self._send_fractured_planes_progress(s)
            return
        claim_key = f"endless-tower-first-clear:{access_id.casefold()}"
        with s.flow_lock:
            claimed = int(
                getattr(s, "gameplay_timed_offer_purchases", {}).get(
                    claim_key, 0
                )
            )
        if claimed:
            _log(f"first-clear reward already claimed for {access_id!r}")
            self._send_fractured_planes_progress(s)
            return
        try:
            CharacterService._deliver_rewards(
                self, s, rewards,
                claim_keys=(claim_key,),
                label="fractured-planes-first-clear",
            )
        except Exception as exc:
            _log(
                f"first-clear reward failed on {access_id!r}: "
                f"{type(exc).__name__}: {exc}"
            )
            return
        _log(
            f"granted {access_id!r} first-clear reward "
            f"({len(rewards)} entr{'y' if len(rewards) == 1 else 'ies'})"
        )
        self._send_fractured_planes_progress(s)

    def _handle_fractured_planes_first_clear_acceptance(
            self, s: Session, payload: bytes) -> None:
        """Grant the floor reward when the client accepts its portal prompt."""

        with s.flow_lock:
            s.gameplay_client_command_state[0x0174] = bytes(payload)
        CharacterService._accept_gameplay_state_command(s, 0x0174)
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if (
            runtime is None
            or not self._is_fractured_planes_run(s)
            or not runtime.fractured_planes_floor_completed
        ):
            _log("ignored first-clear acceptance outside a completed Fractured Planes floor")
            return
        self._grant_fractured_planes_first_clear(s)

    def _broadcast_fractured_planes_score(
            self, runtime: MapEventRuntime, score: int) -> None:
        """Mirror the instance's kill score to every Fractured Planes panel.

        The panel bar and its ``%0.f%%`` label are drawn by the client from
        ``KillMonsterGetTotalScoresCommand``; without it the widget keeps its
        authored default (a full bar and the literal ``Text`` label).
        """

        if not command_available(KILL_MONSTER_GET_TOTAL_SCORES_COMMAND):
            return
        packet, packet_bits = encode_kill_monster_get_total_scores(
            max(0, int(score)), actor_id=LOCAL_PLAYER_ACTOR_ID,
        )
        for viewer in self._map_event_sessions(runtime):
            try:
                CharacterService._send_world_loot_state_batch(
                    viewer, packet, packet_bits,
                    label="fractured-planes-score",
                )
            except Exception as exc:
                _log(
                    "fractured-planes score send failed: "
                    f"{type(exc).__name__}: {exc}"
                )

    # ------------------------------------------------------------------
    # Runtime registry
    # ------------------------------------------------------------------

    def _map_event_registry(self) -> dict:
        registry = getattr(self, "gameplay_map_event_runtimes", None)
        if registry is None:
            registry = {}
            self.gameplay_map_event_runtimes = registry
        return registry

    def _attach_map_event_runtime(
            self, s: Session, *, map_id: str, initial_flags,
            monster_level, scaling_difficulty: int,
            fresh_instance: bool) -> MapEventRuntime | None:
        """Join the instance's runtime, or start one for a new instance."""

        try:
            content = load_map_event_content(self.static_database_path, map_id)
            catalog = load_map_signal_catalog(self.static_database_path)
        except Exception as exc:
            _log(
                f"content unavailable for {map_id!r}: "
                f"{type(exc).__name__}: {exc}"
            )
            with s.flow_lock:
                s.gameplay_map_event_runtime = None
                s.gameplay_trigger_volumes_inside = set()
                s.gameplay_trigger_volumes_last_position = None
            s.gameplay_quest_props_visible = set()
            return None
        key = CharacterService._gameplay_instance_key(s)
        if CharacterService._is_fractured_planes_run(self, s):
            s.gameplay_fractured_planes_run = True
            key = (*key, "fracturedplanes")
        flags = active_local_map_event_flags(map_id, frozenset(initial_flags))
        with self.gameplay_world_lock:
            registry = self._map_event_registry()
            runtime = registry.get(key)
            if runtime is None or fresh_instance:
                runtime = MapEventRuntime(
                    content,
                    catalog,
                    instance_key=key,
                    initial_flags=flags,
                    monster_level=monster_level,
                    scaling_difficulty=int(scaling_difficulty),
                )
                runtime.scheduler = s.gameplay_deadline_scheduler
                registry[key] = runtime
        with s.flow_lock:
            s.gameplay_map_event_runtime = runtime
            s.gameplay_trigger_volumes_inside = set()
            s.gameplay_trigger_volumes_last_position = None
            s.gameplay_quest_props_visible = set()
        return runtime

    def _map_event_runtime_is_live(self, runtime: MapEventRuntime) -> bool:
        with self.gameplay_world_lock:
            return self._map_event_registry().get(runtime.instance_key) is runtime

    def _map_event_sessions(self, runtime: MapEventRuntime) -> tuple[Session, ...]:
        """Every initialized session that shares ``runtime``."""

        return tuple(
            session
            for session in self._map_session_candidates(runtime.map_id)
            if (
                session.connected
                and session.gameplay_mode
                and session.gameplay_initialization_sent
                and session.gameplay_presence_ready
                and not session.gameplay_presence_departed
                and getattr(session, "gameplay_map_event_runtime", None) is runtime
            )
        )

    @staticmethod
    def _map_event_prop_info(s: Session) -> PropInfo | None:
        """The instance's published props, in the PropInfo shape."""

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            return None
        with runtime.lock:
            states_by_id = {
                state.definition.prop_id: state
                for state in runtime.published_props()
            }
            if map_key(runtime.map_id) == "pw205_helios_dun":
                # Retail gates these reusable stones behind a quest-phase
                # EventSet. They are ordinary map resources and must remain
                # present regardless of quest progress or which event fired.
                # Register all authored stone props into the runtime as well
                # as publishing them, so their actor ids remain interactable.
                for definition in runtime.content.props:
                    if not (
                        str(definition.instance_id).casefold().startswith(
                            "pw200_umbra_stone_"
                        )
                        and str(definition.loot_list_id).casefold()
                        in {"chest_umbra_stone_01", "chest_umbra_stone_02"}
                    ):
                        continue
                    state = runtime.props.get(definition.prop_id)
                    always_visible_definition = replace(
                        definition,
                        event_set_row=-1,
                        instance_guid=ZERO_GUID,
                    )
                    if state is None:
                        state = PropRuntimeState(always_visible_definition)
                    else:
                        state = PropRuntimeState(
                            always_visible_definition, state.state,
                        )
                    # Remove the EventSet link in the runtime copy so later
                    # map-event transitions cannot unpublish these persistent
                    # resources. A zero GUID asks the client to instantiate
                    # each prop at its authored map transform.
                    runtime.props[definition.prop_id] = state
                    states_by_id[definition.prop_id] = state
            states = tuple(states_by_id.values())
        states = tuple(
            state for state in states
            if prop_visible_to_player(state.definition, s)
        )
        states = tuple(_prop_state_for_viewer(s, state) for state in states)
        tick = _session_game_tick(s)
        return PropInfo(tuple(
            _prop_info_entry(state, game_tick=tick, operation=operation)
            for state, operation
            in CharacterService._quest_prop_entries(s, states)
        ))

    @staticmethod
    def _running_map_event_entries(s: Session) -> tuple[EventUpdateEntry, ...]:
        """EventUpdate entries for the instance's running map-local events."""

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            return ()
        with runtime.lock:
            definitions = runtime.running_definitions()
        # Retail sends every map-local entry with event id 0; the client
        # matches map-local events by name.
        return tuple(
            EventUpdateEntry(
                event_id=0,
                event_name=definition.event_id,
                flags=(True, True, False, False),
                starts_at=None,
                ends_at=None,
                tags=(),
            )
            for definition in definitions
        )

    @staticmethod
    def _map_event_encounter_fallback(
            static_database_path, map_id: str, *, scaling_difficulty: int = 0):
        """A player-only encounter for maps whose monsters all come from events."""

        try:
            content = load_map_event_content(static_database_path, map_id)
        except Exception:
            return None
        if (
            not content.spawn_rows
            and map_key(map_id) != "pw208_lost_woods_flute_dun"
        ):
            return None
        # Artaya's Clearing has a quest demon wave controlled by map events,
        # but the extracted map database contains no gated monster rows. It
        # still needs an empty combat world so quest-controlled fallback
        # spawns can be materialized after PlayerReady.
        return replace(
            load_local_player_only_encounter(static_database_path, map_id),
            scaling_difficulty=max(0, int(scaling_difficulty)),
        )

    # ------------------------------------------------------------------
    # Signal evaluation
    # ------------------------------------------------------------------

    def _fire_map_signal(self, s: Session, event_type: str, params) -> None:
        """Evaluate every server signal rule of ``event_type`` for ``s``."""

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            return
        self._flush_due_map_event_starts(runtime)
        kind = str(event_type).casefold()
        character = s.gameplay_character
        class_name = (
            CLASS_NAMES.get(int(getattr(character, "class_id", -1)), "")
            if character is not None else ""
        )
        player_level = max(1, int(getattr(character, "level", 1) or 1))
        present_templates: frozenset[str] = frozenset()
        alive_templates: frozenset[str] = frozenset()
        dead_fraction = 0.0
        if kind in ("monsterseliminated", "monsterseliminatedpercentage",
                    "playerkilledmonster"):
            combat = s.gameplay_combat_state
            if combat is not None:
                with combat.lock:
                    present, alive = combat.monster_template_counts()
                present_templates = frozenset(present)
                alive_templates = frozenset(alive)
                if (
                    kind == "monsterseliminatedpercentage"
                    and self._is_fractured_planes_run(s)
                    and _fractured_planes_floor_number_from_map(runtime.map_id)
                ):
                    # The tower map keeps dormant/event-gated actors in the
                    # combat table. They are not part of the live floor count
                    # until their set is activated.
                    active = tuple(
                        actor_id for actor_id in combat.active_monster_actor_ids
                        if not is_tower_mechanism(
                            combat.encounter.static_database_path, runtime.map_id,
                            combat.monsters[actor_id].template_id,
                        )
                    )
                    if active:
                        dead = sum(
                            combat.monster_health.get(actor_id, 0) <= 0
                            for actor_id in active
                        )
                        dead_fraction = dead / len(active)
                else:
                    total = sum(present.values())
                    if total:
                        dead_fraction = 1.0 - sum(alive.values()) / total
        quests_accepted, quests_solved, tasks_done = (
            CharacterService._quest_signal_facts(self, s)
        )
        if kind == "playerhittrigger":
            CharacterService._quest_on_triggers(self, s, params)
        with s.flow_lock:
            unlocked_maps = frozenset(s.gameplay_unlocked_maps)
        if kind == "playerenteredmap":
            # A map instance can outlive the session that first entered it.
            # Restore any quest-gated exits for this character from the same
            # authored signal conditions used by map initialization.
            try:
                durable_exit_flags = CharacterService._quest_exit_reveal_flags(
                    self, s, runtime.map_id,
                )
            except Exception:
                durable_exit_flags = frozenset()
            for flag in durable_exit_flags:
                definition = runtime.catalog.event(flag)
                if definition is None:
                    continue
                with runtime.lock:
                    already_active = flag in runtime.active_flags()
                if not already_active:
                    self._start_map_local_event(runtime, definition)
            # The reusable Telepolos Umbra Stones share an EventSet with the
            # Hero of Light quest phase that hides them. That client-only
            # activation has no authored end signal, so a shared map instance
            # can retain it after the quest is turned in. Clear the stale
            # phase before evaluating the solved-quest deactivation signal.
            if (
                map_key(runtime.map_id) == "pw205_helios_dun"
                and "pw205-the-hero-of-light-02-n" in quests_solved
            ):
                definition = runtime.catalog.event(
                    "pw205_the_hero_of_light_02_activate_umbra_stones"
                )
                if definition is not None:
                    self._stop_map_local_event(runtime, definition)
        with runtime.lock:
            running = runtime.active_flags()
        context = SignalContext(
            map_id=runtime.map_id,
            params=frozenset(str(value).casefold() for value in params),
            running_events=running,
            player_level=player_level,
            class_name=class_name,
            player_count=max(1, len(self._map_event_sessions(runtime))),
            present_templates=present_templates,
            alive_templates=alive_templates,
            dead_fraction=dead_fraction,
            quests_accepted=quests_accepted,
            quests_solved=quests_solved,
            tasks_done=tasks_done,
            unlocked_maps=unlocked_maps,
        )
        rules = runtime.catalog.evaluate(kind, context)
        # The sixth Hunt for a Legend quest still uses the sandhills authored
        # for quest 05. Its map-entry signal is guarded by quest 05 being
        # accepted, so players who have already turned in that step never
        # activate the EventSet containing the quest 06 sandhill props.
        # Keep that authored phase available while its follow-up quest is
        # active, without changing the quest data or its progression.
        sandhill_signal = (
            "h0004_hunt_for_a_legend_05_activate_sandhills_start_signal"
        )
        if (
            kind == "playerenteredmap"
            and map_key(runtime.map_id) == "h0004_temple_dun"
            and "h0001-hunt-for-a-legend-06-n" in quests_accepted
            and not any(rule.signal_id.casefold() == sandhill_signal for rule in rules)
        ):
            _log(
                "starting the Hunt for a Legend sandhill phase for the active "
                "quest 06 on h0004_temple_dun"
            )
            self._emit_map_signal(s, runtime, sandhill_signal)
        if (
            kind == "monsterseliminated"
            and _fractured_planes_floor_number_from_map(runtime.map_id)
            and self._is_fractured_planes_run(s)
        ):
            # Static template selectors can match only part of a dynamically
            # populated room. Its traps are governed by the room's actual
            # actors in _update_fractured_planes_clear_state instead.
            floor = _fractured_planes_floor_number_from_map(runtime.map_id)
            regions = (
                _fractured_planes_floor_one_regions(str(self.static_database_path))
                if floor == 1 else _fractured_planes_floor_regions(
                    str(self.static_database_path), runtime.map_id,
                    _fractured_planes_room_event_rows(runtime),
                    tuple(
                        (str(prop.instance_id), tuple(prop.position))
                        for prop in runtime.content.props
                    ),
                )
            )
            regional_signals = {signal for signal, _volumes in regions}
            rules = tuple(rule for rule in rules if rule.signal_id not in regional_signals)
        if not rules:
            if kind in {"playerenteredmap", "playerendquesttask"}:
                self._ensure_pw209_kinza_leader(s, runtime)
                self._ensure_pw207_sunlairs_notes_mob(s, runtime)
                self._ensure_pw209_sultans_enforcer02_mobs(s, runtime)
                self._ensure_pw209_slade_throne_room(s, runtime)
                self._ensure_pw000_senbu_boss(s, runtime)
            return
        for rule in rules:
            if rule.event_type in (
                "monsterseliminatedpercentage",
                "playerkillmonstergetscore",
            ):
                with runtime.lock:
                    if rule.signal_id in runtime.fired_once:
                        continue
                    runtime.fired_once.add(rule.signal_id)
            _log(
                f"{event_type} {sorted(context.params)!r} on "
                f"{runtime.map_id} fired signal {rule.signal_id!r}"
            )
            self._emit_map_signal(s, runtime, rule.signal_id)
        if kind in {"playerenteredmap", "playerendquesttask"}:
            self._ensure_pw209_kinza_leader(s, runtime)
            self._ensure_pw207_sunlairs_notes_mob(s, runtime)
            self._ensure_pw209_sultans_enforcer02_mobs(s, runtime)
            self._ensure_pw209_slade_throne_room(s, runtime)
            self._ensure_pw000_senbu_boss(s, runtime)

    def _ensure_pw209_kinza_leader(self, s: Session, runtime) -> None:
        """Keep the Kinza route leader visible for its active kill objective."""

        if map_key(runtime.map_id) != "pw207_chamber_dun":
            return
        accepted, _solved, tasks_done = CharacterService._quest_signal_facts(
            self, s,
        )
        quest_id = "pw209-way-to-kinza-01-n"
        clear_task = f"{quest_id}:pw209-way-to-kinza-01-clear-01"
        kill_task = f"{quest_id}:pw209-way-to-kinza-01-kill-01"
        if (
            quest_id not in accepted
            or clear_task not in tasks_done
            or kill_task in tasks_done
        ):
            return
        definition = runtime.catalog.event(
            "pw209_way_to_kinza_01_spawn_leader",
        )
        if definition is None:
            _log(
                "Kinza route leader event is missing on pw207_chamber_dun"
            )
            return
        with runtime.lock:
            active = definition.key in runtime.running
            pending = definition.key in runtime.pending_start_tokens
        if not active and pending:
            return
        if not active:
            _log(
                "reconciling the active Kinza route kill task by starting its "
                "leader event on pw207_chamber_dun"
            )
            self._start_map_local_event(runtime, definition)
            return
        event_sets = tuple(sorted({
            int(value.row)
            for value in runtime.content.event_sets_for(definition.key)
        }))
        if not event_sets:
            _log(
                "Kinza route leader event has no spawn EventSet on "
                "pw207_chamber_dun"
            )
            return
        sessions = self._map_event_sessions(runtime)
        if s not in sessions:
            sessions = (*sessions, s)
        self._spawn_event_monsters(runtime, sessions, event_sets)

    def _ensure_pw207_sunlairs_notes_mob(self, s: Session, runtime) -> None:
        """Replay or restore the one-off demon for Sunlair's Notes page 01."""

        if map_key(runtime.map_id) != "pw207_chamber_dun":
            return
        accepted, _solved, tasks_done = CharacterService._quest_signal_facts(
            self, s,
        )
        quest_id = "pw207-sunlairs-notes-01-n"
        task = f"{quest_id}:pw207-sunlairs-notes-01-collect-01"
        if quest_id not in accepted or task in tasks_done:
            return
        template_id = "pw207_normal_demon_damage_dealer_quest_01"
        map_path = map_database_path(self.static_database_path, runtime.map_id)
        try:
            with closing(sqlite3.connect(
                f"file:{map_path.as_posix()}?mode=ro", uri=True,
            )) as database:
                row = database.execute(
                    """
                    SELECT rowid, Transform
                    FROM _Instance_SpawnPoint
                    WHERE lower(COALESCE(Id, ''))=?
                       OR lower(COALESCE(_ID, ''))=?
                    ORDER BY CASE WHEN COALESCE(EventSetRow, -1)=-1 THEN 0 ELSE 1 END,
                             rowid
                    LIMIT 1
                    """,
                    (template_id, template_id),
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            _log(
                f"cannot read Sunlair's Notes spawn point: "
                f"{type(exc).__name__}: {exc}"
            )
            return
        if row is None:
            _log(
                f"Sunlair's Notes spawn point for {template_id!r} is missing"
            )
            return
        try:
            matrix = struct.unpack("<16f", bytes(row[1]))
            position = tuple(float(matrix[index]) for index in (12, 13, 14))
        except (TypeError, ValueError, struct.error) as exc:
            _log(
                f"Sunlair's Notes spawn point has invalid transform: {exc}"
            )
            return
        sessions = self._map_event_sessions(runtime)
        if s not in sessions:
            sessions = (*sessions, s)
        self._spawn_event_monsters(
            runtime,
            sessions,
            (),
            extra_rows=(GatedSpawnRow(
                -1_700_000 - int(row[0]), -1, template_id, position,
            ),),
        )

    def _ensure_pw209_sultans_enforcer02_mobs(
            self, s: Session, runtime: MapEventRuntime,
    ) -> None:
        """Spawn the four missing demons for the active Anbir clear objective."""

        if map_key(runtime.map_id) != "pw209_kinza_dun":
            return
        quest_id = "pw209-the-sultans-enforcers-02-n"
        quest = CharacterService._quest_catalog(self).quest(quest_id)
        progress = next((
            state
            for key, state in (
                getattr(s, "gameplay_quest_log", None) or {}
            ).items()
            if str(key).casefold() == quest_id
        ), None)
        clear_task_active = bool(
            quest is not None
            and progress is not None
            and progress.state == QUEST_STATE_ACCEPTED
            and any(
                task.title.casefold()
                == "pw209-the-sultans-enforcers-02-clear-01"
                and any(
                    event.kind.casefold() == "regioncleared"
                    and event.value.casefold()
                    == "pw209-the-sultans-enforcers-02-n.s_"
                    "pw209-the-sultans-enforcers-02-clear-01clearregion"
                    for event in task.events
                )
                for task in active_tasks(quest, progress)
            )
        )
        if not clear_task_active:
            return

        marker = "pw209_sultans_enforcer_02_mobs_materialized"
        with runtime.lock:
            if marker in runtime.fired_once:
                return
            runtime.fired_once.add(marker)

        templates = (
            "pw209_normal_demon_fat_01_q_sultans_enforcer_02",
            "pw209_normal_demon_skinny_01_q_sultans_enforcer_02",
            "pw209_normal_demon_damage_dealer_01_q_sultans_enforcer_02",
            "pw209_normal_demon_tank_01_q_sultans_enforcer_02",
        )
        # Anbir is authored at (45, 0, -3). These grounded locations surround
        # him and remain clear of his collision capsule.
        positions = (
            (35.0, 0.0, -3.0),
            (45.0, 0.0, -12.0),
            (53.0, 0.0, -3.0),
            (45.0, 0.0, 5.0),
        )
        spawn_rows = []
        for index, (template, position) in enumerate(
            zip(templates, positions, strict=True),
        ):
            if _load_runtime_definition(
                self.static_database_path, template, 0,
            ) is None:
                _log(
                    f"Kinza Enforcer 02 template {template!r} has no "
                    "runtime definition"
                )
                continue
            spawn_rows.append(GatedSpawnRow(
                -2_400_000 - index, -1, template, position,
            ))
        sessions = self._map_event_sessions(runtime)
        if s not in sessions:
            sessions = (*sessions, s)
        combat = self._map_event_combat_state(sessions)
        if combat is None:
            _log("cannot ensure Kinza Enforcer 02 demons: no combat state")
            with runtime.lock:
                runtime.fired_once.discard(marker)
            return
        if spawn_rows:
            self._spawn_event_monsters(
                runtime, sessions, (), extra_rows=tuple(spawn_rows),
            )
        _log(
            f"ensured {len(spawn_rows)} quest demon(s) around Anbir for "
            "pw209-the-sultans-enforcers-02-clear-01"
        )

    def _ensure_pw209_slade_throne_room(
            self, s: Session, runtime: MapEventRuntime,
    ) -> None:
        """Reconcile the open-door and Slade spawns for the final Kinza quest."""

        if map_key(runtime.map_id) != "pw209_kinza_dun":
            return
        quest_id = "pw209-slades-final-chapter-01-n"
        quest = CharacterService._quest_catalog(self).quest(quest_id)
        progress = next((
            state
            for key, state in (
                getattr(s, "gameplay_quest_log", None) or {}
            ).items()
            if str(key).casefold() == quest_id
        ), None)
        slade_task_active = bool(
            quest is not None
            and progress is not None
            and progress.state == QUEST_STATE_ACCEPTED
            and any(
                task.title.casefold()
                == "pw209-slades-final-chapter-01-kill-02"
                for task in active_tasks(quest, progress)
            )
        )
        if not slade_task_active:
            return

        sessions = self._map_event_sessions(runtime)
        if s not in sessions:
            sessions = (*sessions, s)
        for event_id in (
            "pw209_slades_final_chapter_01_open_throne_room",
            "pw209_slades_final_chapter_01_spawn_slade",
        ):
            definition = runtime.catalog.event(event_id)
            if definition is None or not definition.applies_to(runtime.map_id):
                _log(f"Kinza throne-room event {event_id!r} is unavailable")
                continue
            with runtime.lock:
                active = definition.key in runtime.running
                pending = definition.key in runtime.pending_start_tokens
            if not active and not pending:
                _log(
                    f"reconciling {event_id} for the active Slade kill task"
                )
                self._start_map_local_event(runtime, definition)
                with runtime.lock:
                    active = definition.key in runtime.running
            if event_id.endswith("spawn_slade") and active:
                event_sets = tuple(sorted({
                    int(value.row)
                    for value in runtime.content.event_sets_for(definition.key)
                    if int(value.row) in runtime.enabled_event_sets
                }))
                if event_sets:
                    self._spawn_event_monsters(runtime, sessions, event_sets)

    def _ensure_pw000_senbu_boss(
            self, s: Session, runtime: MapEventRuntime,
    ) -> None:
        """Restore Senbu's event spawn while its quest kill task is active."""

        if map_key(runtime.map_id) != "g0017_templecity_md_04":
            return
        quest_id = "pw000-introduction-01-n"
        quest = CharacterService._quest_catalog(self).quest(quest_id)
        progress = next((
            state
            for key, state in (
                getattr(s, "gameplay_quest_log", None) or {}
            ).items()
            if str(key).casefold() == quest_id
        ), None)
        senbu_task_active = bool(
            quest is not None
            and progress is not None
            and progress.state == QUEST_STATE_ACCEPTED
            and any(
                task.title.casefold() == "pw000-introduction-01-kill-02"
                for task in active_tasks(quest, progress)
            )
        )
        if not senbu_task_active:
            return

        event_id = "g0017_spawn_boss"
        definition = runtime.catalog.event(event_id)
        sessions = self._map_event_sessions(runtime)
        if s not in sessions:
            sessions = (*sessions, s)
        if definition is not None and definition.applies_to(runtime.map_id):
            with runtime.lock:
                active = definition.key in runtime.running
                pending = definition.key in runtime.pending_start_tokens
            if not active and not pending:
                _log(
                    "reconciling g0017_spawn_boss for the active Senbu kill task"
                )
                self._start_map_local_event(runtime, definition)
        else:
            _log(f"Panoltiaco Senbu event {event_id!r} is unavailable")

        # The authored spawn can be missed if the event flags were not restored
        # for this instance. Use its map-authored spawn rows directly while the
        # quest target is active; _spawn_event_monsters is idempotent and still
        # associates the live boss with the correct event set.
        event_sets = tuple(
            int(value.row)
            for value in runtime.content.event_sets_for(event_id)
        )
        spawn_rows = runtime.content.spawn_rows_for_sets(event_sets)
        if spawn_rows:
            self._spawn_event_monsters(
                runtime, sessions, (), extra_rows=spawn_rows,
            )
        else:
            _log("Panoltiaco Senbu spawn point is missing from map content")

    def _emit_map_signal(self, s: Session, runtime: MapEventRuntime,
                         signal_id: str) -> None:
        catalog = runtime.catalog
        key = str(signal_id).casefold()
        signal_keys = [key]
        fractured_run = CharacterService._is_fractured_planes_run(self, s)
        if (
            fractured_run
            and _fractured_planes_floor_number_from_map(runtime.map_id) == 1
            and key.startswith("h0009_endlesstower_01_room_")
            and key.endswith("_end_signal_01")
        ):
            # Percentage-clear rows append _01, but the corresponding local
            # room events listen to the unsuffixed signal name.
            signal_keys.append(key[:-3])
        with runtime.lock:
            runtime.signal_log.append((time.time(), key))
        for event_signal in signal_keys:
            for event_key in catalog.events_by_end_signal.get(event_signal, ()):
                definition = catalog.event(event_key)
                if definition is not None and definition.applies_to(runtime.map_id):
                    self._stop_map_local_event(runtime, definition)
            for event_key in catalog.events_by_start_signal.get(event_signal, ()):
                definition = catalog.event(event_key)
                if definition is not None and definition.applies_to(runtime.map_id):
                    self._schedule_map_local_event_start(runtime, definition)
        CharacterService._quest_on_signal(self, s, signal_id)

    def _fractured_planes_floor_blockers(
            self, s: Session, runtime: MapEventRuntime,
    ) -> tuple[bool, tuple[int, ...]]:
        """Return ``(has_floor_monsters, living_blockers)`` for the boss gate.

        The authored gate is a kill-score threshold, and several tower floors
        only spawn monsters without an ``AddScoreForKill`` value, so the score
        can never reach it. The server therefore resolves the gate when the
        floor has nothing left to fight.

        ``combat.active_monster_actor_ids`` cannot answer that on its own. It
        is shared by the map but derived from per-session visibility, so it can
        be empty because the player walked away and everything was culled, and
        it can hold an actor no client currently has. A monster nobody can see
        cannot be killed, so it must never hold the floor closed; a visible
        living monster always must. Visibility decides, not activity.
        """

        combat = getattr(s, "gameplay_combat_state", None)
        if combat is None:
            return False, ()
        static_path = str(combat.encounter.static_database_path)
        visible: set[int] = set()
        for viewer in self._map_event_sessions(runtime):
            with viewer.flow_lock:
                if viewer.gameplay_combat_state is combat:
                    # ``gameplay_spawned_monster_actor_ids`` is reserved
                    # before packet encoding and can include a mob the client
                    # never received. The sent ledger is the strongest
                    # server-side evidence that the actor was published.
                    visible.update(viewer.gameplay_new_monster_sent_actor_ids)
        with combat.lock:
            floor_mobs = tuple(
                actor_id
                for actor_id, monster in combat.monsters.items()
                if not monster.rank_id.casefold().startswith("rank_boss")
                and not is_tower_mechanism(
                    static_path, runtime.map_id, monster.template_id,
                )
            )
            blockers = tuple(
                actor_id
                for actor_id in floor_mobs
                if combat.monster_health.get(actor_id, 0) > 0
                and actor_id in visible
            )
        return bool(floor_mobs), blockers

    def _resolve_fractured_planes_floor_clear(
            self, s: Session, runtime: MapEventRuntime, *, score_rules=None,
            log_blockers: bool = True,
    ) -> bool:
        """Open the boss once nothing killable is left on the floor."""

        if score_rules is None:
            score_rules = tuple(
                rule
                for rule in runtime.catalog.rules_by_event_type.get(
                    "playerkillmonstergetscore", ()
                )
                if not rule.maps or map_key(runtime.map_id) in rule.maps
            )
        if not score_rules:
            return False
        has_floor_monsters, blockers = self._fractured_planes_floor_blockers(
            s, runtime,
        )
        if not has_floor_monsters or blockers:
            if blockers and log_blockers:
                combat = getattr(s, "gameplay_combat_state", None)
                with combat.lock:
                    descriptions = tuple(
                        (
                            int(actor_id),
                            combat.monsters[actor_id].template_id,
                            tuple(round(float(value), 2) for value in
                                  combat.monster_positions.get(actor_id, (0, 0, 0))),
                        )
                        for actor_id in blockers if actor_id in combat.monsters
                    )
                _log(
                    "Fractured Planes boss gate waiting for visible living "
                    f"mobs on {runtime.map_id}: {descriptions!r}"
                )
            return False
        with runtime.lock:
            if runtime.pending_start_tokens:
                return False
            clear_rules = tuple(
                rule for rule in score_rules
                if rule.signal_id not in runtime.fired_once
            )
            if not clear_rules:
                return False
            runtime.fired_once.update(
                rule.signal_id for rule in clear_rules
            )
        for rule in clear_rules:
            _log(
                f"Fractured Planes floor clear on {runtime.map_id}: resolving "
                f"{rule.signal_id!r} after all killable non-boss monsters were "
                f"defeated"
            )
            self._emit_map_signal(s, runtime, rule.signal_id)
        # Clear each active room after the final spawned monster dies so all
        # floor-specific traps and blockers receive their authored end signals.
        floor_number = _fractured_planes_floor_number_from_map(runtime.map_id)
        room_rows = _fractured_planes_room_event_rows(runtime)
        room_regions = (
            _fractured_planes_floor_regions(
                str(self.static_database_path), runtime.map_id,
                room_rows,
                tuple(
                    (str(prop.instance_id), tuple(prop.position))
                    for prop in runtime.content.props
                ),
            )
            if floor_number > 1 else _fractured_planes_floor_one_regions(
                str(self.static_database_path)
            )
        )
        # Some layouts (notably sm_03, sm_05, sm_06, sm_08 and sm_09) store
        # room events but omit either room-start props or EventSet-linked
        # SpawnAreas. A spatial lookup alone then returns no signal at all.
        # On the final full-clear edge, end every authored room event so its
        # trap/door transition runs even when that layout has no region data.
        clear_signals = list(signal for signal, _volumes in room_regions)
        if floor_number > 1:
            clear_signals.extend(signal for _row, _room, signal in room_rows)
        for signal in dict.fromkeys(clear_signals):
            self._emit_map_signal(s, runtime, signal)
        return True

    def _on_monsters_killed(self, s: Session, monsters) -> None:
        monsters = tuple(monsters)
        tower_kills(self, s, monsters)
        templates = tuple(dict.fromkeys(
            str(getattr(monster, "template_id", "")).casefold()
            for monster in monsters
            if str(getattr(monster, "template_id", ""))
        ))
        if not templates:
            return
        CharacterService._quest_on_monsters_killed(self, s, monsters)
        CharacterService._achievement_on_monsters_killed(self, s, monsters)
        CharacterService._on_map_companion_boss_defeated(self, s, monsters)
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        score_rules = ()
        if runtime is not None:
            if CharacterService._is_fractured_planes_run(self, s):
                with runtime.lock:
                    runtime.fractured_planes_kill_seen = True
            score_rules = tuple(
                rule
                for rule in runtime.catalog.rules_by_event_type.get(
                    "playerkillmonstergetscore", ()
                )
                if not rule.maps or map_key(runtime.map_id) in rule.maps
            )
            if score_rules:
                static_path = Path(self.static_database_path).resolve().as_uri()
                scored_templates = tuple(
                    (
                        str(getattr(monster, "template_id", "")).casefold(),
                        _monster_kill_score(
                            static_path,
                            str(getattr(monster, "template_id", "")).casefold(),
                        ),
                    )
                    for monster in monsters
                    if str(getattr(monster, "template_id", ""))
                )
                score_gained = sum(score for _template, score in scored_templates)
                with runtime.lock:
                    runtime.kill_score += score_gained
                    current_score = runtime.kill_score
                thresholds = tuple(
                    int(value)
                    for rule in score_rules
                    for value in rule.params
                    if str(value).isdigit()
                )
                next_threshold = min(
                    (value for value in thresholds if value > current_score),
                    default=None,
                )
                target_text = (
                    str(next_threshold) if next_threshold is not None
                    else ",".join(map(str, sorted(set(thresholds))))
                )
                _log(
                    f"kill-score progress on {runtime.map_id}: "
                    f"gained={score_gained}, total={current_score}, "
                    f"next={target_text}, monsters={scored_templates!r}"
                )
                if score_gained:
                    self._fire_map_signal(
                        s, "PlayerKillMonsterGetScore", (str(current_score),)
                    )
                    if CharacterService._is_fractured_planes_run(self, s):
                        self._broadcast_fractured_planes_score(
                            runtime, current_score
                        )
        self._fire_map_signal(s, "PlayerKilledMonster", templates)
        self._unlock_parallel_world_boss_reward_chest(
            runtime, templates,
        )
        self._open_parallel_world_exit_after_boss_kill(
            s, runtime, templates,
        )
        self._fire_map_signal(s, "MonstersEliminated", templates)
        self._fire_map_signal(s, "MonstersEliminatedPercentage", ())
        self._update_fractured_planes_clear_state(s, monsters)
        # Process kill/elimination and room-clear signals before using the
        # all-mobs-dead fallback. A cleared wave can schedule the next wave
        # through those signals; checking first would open the boss gate in
        # the short gap between waves. Delayed wave starts leave a pending
        # token, and immediate starts materialize their actors before this
        # final gate check.
        if (
            runtime is not None
            and score_rules
            and CharacterService._is_fractured_planes_run(self, s)
            and _fractured_planes_floor_number_from_map(runtime.map_id)
        ):
            self._resolve_fractured_planes_floor_clear(
                s, runtime, score_rules=score_rules,
            )

    def _unlock_parallel_world_boss_reward_chest(
            self, runtime: MapEventRuntime | None, templates: tuple[str, ...],
    ) -> None:
        """Allow interaction with the arena reward chests after the boss dies.

        Their PropInfo is published on map entry alongside the boss. This
        runtime flag keeps the visible chest from being consumed too early.
        """

        if runtime is None:
            return
        gate = _PARALLEL_WORLD_BOSS_REWARD_CHESTS.get(map_key(runtime.map_id))
        if gate is None or gate[0] not in templates:
            return
        with runtime.lock:
            runtime.boss_reward_chests_unlocked.add(gate[1])
        _log(
            f"boss reward chest unlocked on {runtime.map_id} after "
            f"{gate[0]} was defeated"
        )

    def _ensure_parallel_world_boss_reward_chest_visible(
            self, runtime: MapEventRuntime | None,
    ) -> None:
        """Publish the arena's authored chest props as soon as it loads."""

        if runtime is None:
            return
        gate = _PARALLEL_WORLD_BOSS_REWARD_CHESTS.get(map_key(runtime.map_id))
        if gate is None:
            return
        definition = runtime.catalog.event(gate[1])
        if definition is None or not definition.applies_to(runtime.map_id):
            _log(
                f"reward chest event {gate[1]!r} is missing for "
                f"{runtime.map_id}"
            )
            return
        self._schedule_map_local_event_start(runtime, definition)

    def _publish_parallel_world_boss_reward_chest_props(
            self, s: Session, runtime: MapEventRuntime | None,
    ) -> None:
        """Resend arena chest props after the client has entered the map.

        The chest EventSet is enabled in the initialization snapshot, so the
        event-start transition has no newly enabled props to publish. Some
        clients process that initial PropInfo before the arena scene is ready;
        explicitly replay the props after PlayerEnteredMap to make the chest
        visible in the loaded scene.
        """

        if runtime is None:
            return
        gate = _PARALLEL_WORLD_BOSS_REWARD_CHESTS.get(map_key(runtime.map_id))
        if gate is None:
            return
        with runtime.lock:
            states = tuple(
                state for state in runtime.published_props()
                if state.definition.instance_id.casefold()
                in PARALLEL_WORLD_REWARD_CHEST_INSTANCE_IDS
                and prop_visible_to_player(state.definition, s)
            )
        if not states:
            _log(
                f"no published reward chest props found on {runtime.map_id}"
            )
            return
        placements = "; ".join(
            f"{state.definition.instance_id}->"
            f"{state.definition.template_id}@{tuple(round(axis, 2) for axis in state.definition.position)}"
            for state in states
        )
        _send_prop_info(
            s, states, label="parallel-world-boss-reward-chest"
        )
        _log(
            f"replayed {len(states)} reward chest prop(s) to player "
            f"on {runtime.map_id} after map entry: {placements}"
        )

    def _open_parallel_world_exit_after_boss_kill(
            self, s: Session, runtime: MapEventRuntime | None,
            templates: tuple[str, ...],
    ) -> None:
        """Start the next-map exit event after its gatekeeper or map clear."""

        if runtime is None:
            return
        with s.flow_lock:
            if getattr(s, "gameplay_exit_transition_started", False):
                return
        gate = _PARALLEL_WORLD_EXIT_BOSSES.get(map_key(runtime.map_id))
        if gate is None:
            return
        boss_template, event_key = gate
        if boss_template:
            if boss_template not in templates:
                return
        else:
            # The final two Atlantis maps use authored clear events instead
            # of a single leader actor. Their exit event is client-only, so
            # start it server-side once the encounter has no living enemies.
            combat = getattr(s, "gameplay_combat_state", None)
            if combat is None:
                return
            _present, alive = combat.monster_template_counts()
            if alive:
                return
        with runtime.lock:
            already_started = (
                event_key in runtime.running
                or event_key in runtime.pending
            )
        if already_started:
            # The authored kill/quest signal already ran the transition,
            # including the staged ExitInfo -> EventUpdate -> active ExitInfo
            # sequence. Replaying an active edge here does not create a new
            # entity and can make the scene gate disappear without a portal.
            return
        _log(
            f"Parallel World gatekeeper defeated on {runtime.map_id}: "
            f"starting exit event {event_key!r}"
        )
        definition = runtime.catalog.event(event_key)
        if definition is not None:
            self._schedule_map_local_event_start(runtime, definition)

    @staticmethod
    def _notify_monsters_killed(s: Session, monsters) -> None:
        """Raise the kill toward the server from static combat code."""

        callback = getattr(s, "gameplay_presence_callback", None)
        if callback is None or not monsters:
            return
        try:
            callback(s, ("monsters_killed", tuple(monsters)))
        except Exception as exc:
            _log(f"kill notification failed: {type(exc).__name__}: {exc}")

    def _restore_map_unlocks(self, s: Session, persisted_state) -> None:
        from dso_server.gameplay.map_unlocks import restored_map_unlocks, saved_quest_map_unlocks
        s.gameplay_unlocked_maps = restored_map_unlocks(persisted_state, (
            s.gameplay_map, s.gameplay_map_access_id, s.gameplay_map_access_group_id,
        )) | saved_quest_map_unlocks(self.static_database_path, persisted_state)
        s.gameplay_admin_all_maps = bool(getattr(persisted_state, "admin_all_maps", False))

    def _on_map_entered(self, s: Session) -> None:
        with s.flow_lock:
            discoveries = {
                map_key(value) for value in (
                    s.gameplay_map, s.gameplay_map_access_id,
                    s.gameplay_map_access_group_id,
                ) if value
            }
            newly_discovered = discoveries - s.gameplay_unlocked_maps
            s.gameplay_unlocked_maps.update(discoveries)
        if newly_discovered:
            CharacterService._request_account_save(s, "map discovered")
        self._send_fractured_planes_progress(s)
        self._send_fractured_planes_effect_choices(s)
        start_tower_buffs(self, s)
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            return
        if CharacterService._is_fractured_planes_run(self, s):
            s.gameplay_fractured_planes_run = True
            with runtime.lock:
                if runtime.fractured_planes_limit_deadline is None:
                    runtime.fractured_planes_limit_deadline = (
                        getattr(
                            s, "gameplay_fractured_planes_deadline", None
                        )
                        or time.perf_counter() + 8 * 60
                    )
                s.gameplay_fractured_planes_deadline = (
                    runtime.fractured_planes_limit_deadline
                )
                seconds_left = 0 if runtime.fractured_planes_floor_completed else max(
                    0,
                    math.ceil(
                        runtime.fractured_planes_limit_deadline
                        - time.perf_counter()
                    ),
                )
            packet, packet_bits = encode_server_command_batch((
                ServerCommand(
                    KICK_OUT_TO_HUB_LIMIT_TIME_LEFT_COMMAND,
                    KickOutToHubLimitTimeLeft(seconds_left),
                    1,
                ),
            ))
            CharacterService._send_world_loot_state_batch(
                s, packet, packet_bits, label="fractured-planes-limit-time"
            )
            self._schedule_fractured_planes_limit_tick(runtime)
            self._schedule_fractured_planes_tower_tick(runtime)
            # Publish the instance's running kill score so the panel leaves its
            # authored default (a full bar with the literal ``Text`` label) and
            # starts filling as monsters die.
            with runtime.lock:
                entry_score = runtime.kill_score
            if command_available(KILL_MONSTER_GET_TOTAL_SCORES_COMMAND):
                score_packet, score_bits = encode_kill_monster_get_total_scores(
                    entry_score, actor_id=LOCAL_PLAYER_ACTOR_ID,
                )
                CharacterService._send_world_loot_state_batch(
                    s, score_packet, score_bits, label="fractured-planes-score"
                )
        access_group = str(getattr(s, "gameplay_map_access_group_id", "") or "")
        access_id = str(getattr(s, "gameplay_map_access_id", "") or "")
        self._fire_map_signal(
            s, "PlayerEnteredMap", (runtime.map_id, access_group, access_id)
        )
        self._fire_map_signal(
            s, "PlayerEnteredMapId", (access_group, access_id, runtime.map_id)
        )
        # Retail requires generator interactions before its ram mechanism
        # spawns the machine that breaks the boss arena entrance. The server
        # does not implement those custom prop modules, so complete that step
        # when a player enters this arena.
        self._spawn_f4203_ram_on_entry(s, runtime)
        if (
            CharacterService._is_fractured_planes_run(self, s)
            and map_key(runtime.map_id) == "fp0001_boss_01"
            and self._fractured_planes_floor_number(s) % 10 == 0
        ):
            # Every tenth Fractured Planes floor routes through the separate
            # Lythira arena. Start its authored gated boss spawn on entry so
            # the client does not have to cross an unreliable trigger volume.
            self._fire_map_signal(
                s, "PlayerHitTrigger", ("fp_boss_01_lythira_spawn_trigger",)
            )
            boss_event = runtime.catalog.event("fp_boss_01_lythira_spawn")
            if boss_event is None:
                _log(
                    "Fractured Planes floor 010 boss event is missing from "
                    "the loaded map-signal catalog"
                )
            elif not boss_event.applies_to(runtime.map_id):
                _log(
                    "Fractured Planes floor 010 boss event does not apply to "
                    f"{runtime.map_id!r}; catalog maps={sorted(boss_event.maps)!r}"
                )
            else:
                with runtime.lock:
                    event_running = boss_event.key in runtime.running
                    event_pending = boss_event.key in runtime.pending
                    enabled_sets = tuple(sorted(runtime.enabled_event_sets))
                if not event_running and not event_pending:
                    # Keep the authored delay, but do not depend on the
                    # trigger-to-event signal lookup being present in every
                    # extracted static database.
                    self._schedule_map_local_event_start(runtime, boss_event)
                elif event_running:
                    # A shared map runtime may already have started this event
                    # before the current player's initialization completed.
                    # Replay its live boss actor to the joining session.
                    self._spawn_event_monsters(runtime, (s,), enabled_sets)
                    _log(
                        "Fractured Planes floor 010 boss event was already "
                        "running; replayed its active spawn sets"
                    )
            _log(
                f"ensured Fractured Planes boss event for floor "
                f"{self._fractured_planes_floor_number(s):03d}"
            )
        if (
            CharacterService._is_fractured_planes_run(self, s)
            and map_key(runtime.map_id) == "h0009_endlesstower_01_sm_01"
        ):
            # This reused Tower scene does not contain the Fractured Planes
            # entrance stone as a server prop. Start room 01 on entry so its
            # objective, percentage-clear rule, and trap cleanup are active.
            self._emit_map_signal(
                s, runtime, "h0009_endlesstower_01_room_01_start_signal"
            )
        combat = getattr(s, "gameplay_combat_state", None)
        if runtime.map_id.casefold() == "h0008_boss":
            # The Lantern 09 quest must let the player place the lamp before
            # the Djinn encounter starts. Its authored prop signal starts the
            # same event after interaction; only direct arena arrivals need
            # the entry fallback.
            lantern_quest_id = "h0001-hunt-for-the-lantern-09-n"
            lantern_quest = CharacterService._quest_catalog(self).quest(
                lantern_quest_id
            )
            lantern_progress = next((
                progress
                for quest_id, progress in (getattr(s, "gameplay_quest_log", None) or {}).items()
                if str(quest_id).casefold() == lantern_quest_id
            ), None)
            lamp_task_active = bool(
                lantern_quest is not None
                and lantern_progress is not None
                and lantern_progress.state == QUEST_STATE_ACCEPTED
                and any(
                    task.title.casefold()
                    == "h0001-hunt-for-the-lantern-09-interact-02"
                    for task in active_tasks(lantern_quest, lantern_progress)
                )
            )
            if not lamp_task_active:
                djinn_event = runtime.catalog.event("h0008_start_bossfight_01")
                if djinn_event is not None and djinn_event.applies_to(runtime.map_id):
                    self._schedule_map_local_event_start(runtime, djinn_event)
            else:
                _log(
                    "deferred Djinn encounter on entry for the active Lantern 09 "
                    "lamp-placement objective"
                )
        with runtime.lock:
            entrance_started = "boss_spawn" in runtime.running
        if entrance_started:
            self._publish_entrance_bosses(runtime, (s,))
        CharacterService._quest_on_map_entered(self, s)
        self._ensure_fyeborough_soul_collector(s, runtime)
        self._ensure_artaya_clearing_demons(s, runtime)
        self._ensure_pw209_sultans_enforcer02_mobs(s, runtime)
        self._ensure_pw209_slade_throne_room(s, runtime)
        self._ensure_pw000_senbu_boss(s, runtime)
        CharacterService._achievement_on_map_entered(
            self, s, (runtime.map_id, access_group, access_id),
        )
        CharacterService._restore_map_companion_handoffs(self, s)
        if s.gameplay_last_move is not None:
            CharacterService._check_map_companions(s, s.gameplay_last_move.relative_position)
        # Publish after map entry signals, boss actor replay, and quest state
        # synchronization so the arena chest appears with its boss in the
        # fully loaded client scene.
        self._ensure_parallel_world_boss_reward_chest_visible(runtime)
        self._publish_parallel_world_boss_reward_chest_props(s, runtime)

    def _ensure_fyeborough_soul_collector(
            self, s: Session, runtime: MapEventRuntime,
    ) -> None:
        """Start or replay the Soul Collector for its active story objective.

        The authored spawn is released by two map signals. If the player
        completes the lantern step while already in Fyeborough, or the entry
        signal arrives before the quest state is synchronized, those signals
        can be missed and the quest is left waiting for a monster that was
        never materialized. The accepted kill task is the durable condition
        for restoring this one-shot encounter on map entry or task progress.
        """

        if map_key(runtime.map_id) != "pw207_village_dun":
            return
        quest_id = "pw207-way-to-village-02-n"
        quest = CharacterService._quest_catalog(self).quest(quest_id)
        progress = next((
            state
            for key, state in (
                getattr(s, "gameplay_quest_log", None) or {}
            ).items()
            if str(key).casefold() == quest_id
        ), None)
        kill_task_active = bool(
            quest is not None
            and progress is not None
            and progress.state == QUEST_STATE_ACCEPTED
            and any(
                task.title.casefold()
                == "pw207-way-to-village-02-kill-01"
                and any(
                    event.kind.casefold() == "kill"
                    and event.value.casefold()
                    == "pw207_sentinel_shadow_father_01_q_way_to_village_02"
                    for event in task.events
                )
                for task in active_tasks(quest, progress)
            )
        )
        if not kill_task_active:
            return

        event = runtime.catalog.event(
            "pw207_way_to_village_02_spawn_shadow_father"
        )
        if event is None or not event.applies_to(runtime.map_id):
            _log(
                "cannot ensure Fyeborough Soul Collector: authored map event "
                "is missing or does not apply to pw207_village_dun"
            )
            return
        with runtime.lock:
            running = event.key in runtime.running
            pending = event.key in runtime.pending
        if not running and not pending:
            self._start_map_local_event(runtime, event)
        if running or not pending:
            # A shared map instance can have the encounter active while its
            # actor was created before this player's session became ready.
            # _spawn_event_monsters replays existing matching actors and
            # creates any missing ones without duplicating active spawns.
            with runtime.lock:
                enabled_sets = tuple(sorted(
                    definition.row
                    for definition in runtime.content.event_sets_for(event.key)
                    if definition.row in runtime.enabled_event_sets
                ))
            self._spawn_event_monsters(runtime, (s,), enabled_sets)
        _log(
            "ensured Fyeborough Soul Collector for active "
            "pw207-way-to-village-02 kill objective"
        )

    def _ensure_artaya_clearing_demons(
            self, s: Session, runtime: MapEventRuntime,
    ) -> None:
        """Materialize Lost in the Woods' missing Artaya demon wave.

        The extracted quest refers to four demon templates, and the map has a
        quest event set named ``pw208_lost_woods_03_spawn_demons``. However,
        that event set has no monster or spawn-point rows in the map database,
        so entering Artaya's Clearing can leave the ClearRegion objective with
        no enemies to kill. Use the objective's own four templates and place
        one of each near its authored explore trigger while the task is active.
        """

        if map_key(runtime.map_id) != "pw208_lost_woods_flute_dun":
            return
        quest_id = "pw208-lost-woods-03-n"
        quest = CharacterService._quest_catalog(self).quest(quest_id)
        progress = next((
            state
            for key, state in (
                getattr(s, "gameplay_quest_log", None) or {}
            ).items()
            if str(key).casefold() == quest_id
        ), None)
        templates = (
            "pw208_normal_demon_fat_01",
            "pw208_normal_demon_skinny_01",
            "pw208_normal_demon_damage_dealer_01",
            "pw208_normal_demon_tank_01",
        )
        clear_task_active = bool(
            quest is not None
            and progress is not None
            and progress.state == QUEST_STATE_ACCEPTED
            and any(
                task.title.casefold() == "pw208-lost-woods-03-clear-01"
                and any(
                    event.kind.casefold() == "regioncleared"
                    and event.value.casefold()
                    == "pw208-lost-woods-03-n.s_pw208-lost-woods-03-clear-01clearregion"
                    for event in task.events
                )
                for task in active_tasks(quest, progress)
            )
        )
        if not clear_task_active:
            return

        marker = "pw208_lost_woods_03_quest_demons_materialized"
        with runtime.lock:
            if marker in runtime.fired_once:
                return
            runtime.fired_once.add(marker)

        sessions = self._map_event_sessions(runtime)
        if s not in sessions:
            sessions = (*sessions, s)
        combat = self._map_event_combat_state(sessions)
        if combat is None:
            _log("cannot ensure Artaya demons: no combat state on the instance")
            with runtime.lock:
                runtime.fired_once.discard(marker)
            return

        with combat.lock:
            alive_templates = {
                monster.template_id.casefold()
                for actor_id, monster in combat.monsters.items()
                if combat.monster_health.get(actor_id, 0) > 0
            }
        # The quest's explore trigger sits at (117, 0, 20). Spread the wave
        # around it and away from the four allied beastman anchors at x=127-133.
        positions = (
            (111.0, 0.0, 16.0),
            (117.0, 0.0, 16.0),
            (111.0, 0.0, 24.0),
            (117.0, 0.0, 24.0),
        )
        spawn_rows = []
        for index, (template, position) in enumerate(
            zip(templates, positions, strict=True),
        ):
            if template.casefold() in alive_templates:
                continue
            if _load_runtime_definition(self.static_database_path, template, 0) is None:
                _log(f"Artaya demon template {template!r} has no runtime definition")
                continue
            spawn_rows.append(GatedSpawnRow(
                -2_200_000 - index, -1, template, position,
            ))
        if spawn_rows:
            self._spawn_event_monsters(runtime, sessions, (), extra_rows=tuple(spawn_rows))
        _log(
            f"ensured {len(spawn_rows)} missing demon(s) for active "
            "pw208-lost-woods-03-clear-01 objective in Artaya's Clearing"
        )

    # ------------------------------------------------------------------
    # Client-reported signals
    # ------------------------------------------------------------------

    def _spawn_map_chest_loot(self, s: Session, definition) -> int:
        """Materialize supported map chest rewards as world loot.

        The client export names CHEST_lootlist_BLOODSHED but does not include
        the server-side loot-list rows. Keep this fallback deliberately small
        and use item templates that are present in the shipped static DB.
        """

        loot_list_id = str(definition.loot_list_id or definition.chest_id).casefold()
        if loot_list_id in {"chest_umbra_stone_01", "chest_umbra_stone_02"}:
            specifications = [(
                "ingredient_elemental_essence_crafting_light", 5,
            )]
        elif loot_list_id == "chest_pw199_demon_herb":
            # Canol Alley's eight one-shot herb props feed a recipe that needs
            # ten demon herbs. The client export has the loot-list reference
            # but omits its server-side contents, so award two per prop.
            specifications = [("demon_herb", 2)]
        elif loot_list_id == _BLOOD_CHEST_LOOT_LIST:
            specifications = [
                ("money_rc", 10),
            ]
            gem_count = random.SystemRandom().randint(4, 5)
            specifications.extend(
                (random.SystemRandom().choice(_BLOOD_CHEST_GEM_TEMPLATES), 1)
                for _ in range(gem_count)
            )
        else:
            _log(
                f"no local loot table for chest {definition.instance_id!r} "
                f"({loot_list_id!r}); no rewards spawned"
            )
            return 0
        try:
            drops = []
            for template_id, amount in specifications:
                item_definition = load_purchase_item_definition(
                    self.static_database_path, template_id,
                )
                item = _new_item(item_definition, 0, amount)
                item.field_100 = tuple(float(value) for value in definition.position)
                auto_pick_up = "All" if item_definition.item_category == "Currency" else "None"
                drops.append(WorldLootDrop(
                    item, item_definition.item_category, auto_pick_up,
                ))
        except (OSError, ValueError, sqlite3.Error) as exc:
            _log(
                f"cannot build rewards for chest {definition.instance_id!r}: "
                f"{type(exc).__name__}: {exc}"
            )
            return 0
        instance_key = CharacterService._gameplay_instance_key(s)
        map_id = str(s.gameplay_map or "")
        with self.gameplay_world_lock:
            counters = getattr(self, "gameplay_world_next_item_ids", None)
            if counters is None:
                counters = self.gameplay_world_next_item_ids = {}
            with s.flow_lock:
                first = max(
                    int(getattr(s, "gameplay_next_item_id", RUNTIME_ITEM_ID_BASE)),
                    int(counters.get(instance_key, RUNTIME_ITEM_ID_BASE)),
                )
            end = first + len(drops)
            counters[instance_key] = end
            counters[map_id] = max(end, int(counters.get(map_id, RUNTIME_ITEM_ID_BASE)))
            with s.flow_lock:
                for offset, drop in enumerate(drops):
                    item_id = first + offset
                    drop.item.item_id = item_id
                    drop.item.field_18 = item_id & 0xFFFF
                    s.gameplay_world_loot[item_id] = drop
                s.gameplay_next_item_id = max(
                    int(getattr(s, "gameplay_next_item_id", RUNTIME_ITEM_ID_BASE)),
                    end,
                )
        CharacterService._reconcile_world_loot_visibility(
            s, label="map-chest-loot",
        )
        _log(
            f"spawned {len(drops)} reward item(s) from "
            f"{definition.instance_id!r} for player session"
        )
        return len(drops)

    def _handle_staging_signal(self, s: Session, payload: bytes) -> None:
        CharacterService._handle_simple_gameplay_command(s, payload, 0x0113)
        decoded = s.gameplay_client_command_state.get(0x0113)
        signal_id = str(getattr(decoded, "signal_id", "") or "")
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None or not signal_id:
            return
        rule = runtime.catalog.rule(signal_id)
        if rule is None or not rule.enabled:
            return
        if rule.signal_type not in CLIENT_REPORTED_SIGNAL_TYPES:
            _log(
                f"ignored client report of server signal {signal_id!r}"
            )
            return
        _log(f"client reported signal {rule.signal_id!r} on {runtime.map_id}")
        self._emit_map_signal(s, runtime, rule.signal_id)
        # Fractured Planes replaces the Endless Tower entrance prop, while
        # the reused room event still waits for the tower's old prop id.
        # Bridge the real stones interaction into the authored room-start
        # signal so the level objective and its clear/trap events can run.
        if (
            CharacterService._is_fractured_planes_run(self, s)
            and map_key(runtime.map_id) == "h0009_endlesstower_01_sm_01"
            and rule.signal_id.casefold()
            == "fractured_levelmap_tower01_stones_01"
        ):
            self._emit_map_signal(
                s, runtime, "h0009_endlesstower_01_room_01_start_signal"
            )

    def _handle_quest_trigger_signal(self, s: Session, payload: bytes) -> None:
        request = decode_quest_trigger_signal(
            payload, getattr(s, "current_message_bit_length", None)
        )
        with s.flow_lock:
            s.gameplay_client_command_state[QUEST_TRIGGER_SIGNAL_COMMAND] = request
        CharacterService._accept_gameplay_state_command(
            s, QUEST_TRIGGER_SIGNAL_COMMAND
        )
        _log(f"client reported trigger {request.trigger_id!r}")
        self._fire_map_signal(s, "PlayerHitTrigger", (request.trigger_id,))

    @staticmethod
    def _apply_fractured_planes_hazard_damage(
            s: Session, runtime: MapEventRuntime, position) -> None:
        """Apply server-side damage for active Fractured Planes trap auras."""

        access_id = str(getattr(s, "gameplay_map_access_id", "") or "")
        is_fractured_run = (
            bool(getattr(s, "gameplay_fractured_planes_run", False))
            or access_id.casefold().startswith("h0009_endlesstower_floor_")
        )
        if not is_fractured_run or not _fractured_planes_floor_number_from_map(runtime.map_id):
            return
        combat = getattr(s, "gameplay_combat_state", None)
        if combat is None:
            return
        static_path = str(combat.encounter.static_database_path)
        hazards = _fractured_planes_hazards(
            static_path, runtime.map_id,
        )
        if not hazards:
            return
        with runtime.lock:
            active_flags = runtime.active_flags()
            # Recompute from the authoritative event flags at the damage
            # point.  ``enabled_event_sets`` is also used for scene/spawn
            # publication and may lag an event transition while a chained
            # platform signal is being processed; hazard damage must follow
            # the current event state immediately.
            active_sets = set(
                runtime.content.enabled_event_set_rows(active_flags)
            )
            cached_sets = set(runtime.enabled_event_sets)
        matching = tuple(
            (volume, effect_id)
            for volume, effect_id in hazards
            if volume.event_set_row in active_sets and volume.contains(position)
        )
        if not matching:
            return
        now = time.perf_counter()
        hazard_id = min(volume.row_id for volume, _effect in matching)
        with s.flow_lock:
            if s.gameplay_combat_state is not combat or s.gameplay_map_event_runtime is not runtime:
                return
            cooldowns = getattr(
                s, "gameplay_fractured_hazard_next_at", None,
            )
            if cooldowns is None:
                cooldowns = {}
                s.gameplay_fractured_hazard_next_at = cooldowns
            if now < float(cooldowns.get(hazard_id, 0.0)):
                return
            cooldowns[hazard_id] = now + 1.0
            inventory = s.gameplay_inventory
            actor_id = int(
                getattr(s, "gameplay_remote_actor_id", 0) or combat.player_actor_id
            )
        if active_sets != cached_sets:
            _log(
                f"Fractured Planes hazard state reconciled on {runtime.map_id}: "
                f"event_sets={tuple(sorted(active_sets))}, "
                f"cached_sets={tuple(sorted(cached_sets))}, "
                f"active_events={tuple(sorted(active_flags))}"
            )
        _log(
            f"Fractured Planes hazard hit on {runtime.map_id}: "
            f"actor={actor_id}, hazards="
            f"{tuple((volume.row_id, volume.event_set_row, effect_id) for volume, effect_id in matching)}, "
            f"active_event_sets={tuple(sorted(active_sets))}"
        )
        if inventory is None:
            return
        with combat.lock:
            health = combat.player_health.get(actor_id)
            if health is None or int(health[0]) <= 0:
                return
            previous, maximum = int(health[0]), int(health[1])
            damage = max(1, int(math.ceil(maximum * 0.02)))
            current = max(0, previous - damage)
            applied = previous - current
            health[0] = current
            if actor_id == int(combat.player_actor_id):
                combat.player_current_health = current
        if applied <= 0:
            return
        CharacterService._cancel_respawn_protection(s)
        with s.flow_lock:
            if s.gameplay_inventory is inventory:
                inventory.health_points = float(current)
            server_tick = int(server_time() // 40) & 0xFFFFFFFF
        resolution = DamageResolution(
            target_actor_id=CharacterService._viewer_actor_id(s, actor_id),
            attacker_actor_id=0,
            current_health=current,
            maximum_health=maximum,
            damage=applied,
            killed=current <= 0,
            position=tuple(float(value) for value in position),
            server_tick=server_tick,
            hit_tags=(0,),
            skill_index=0,
            source_actor_id=0,
            periodic_damage=True,
        )
        CharacterService._send_combat_resolution(
            s, resolution, label="fractured-planes-path-trap",
            include_item_set_procs=False,
        )

    @staticmethod
    def _apply_tower_aura_damage(
            s: Session, runtime: MapEventRuntime, position,
            *, previous_position=None, previous_position_at=None) -> None:
        """Damage a player standing inside a fixed tower's aura.

        ``e_dra_fortress_normal_powernode`` is the Fractured Planes laser. It
        has no ``Skills`` row, so the ordinary monster attack path never fires
        for it: the floor's most visible hazard used to deal exactly nothing.
        Its permanent aura supplies the native rotating laser effect. The
        server applies the tower's level-scaled ``MinDamage``/``MaxDamage``
        average when the player intersects the blue beam, instead of applying
        damage anywhere in the aura's 8-unit target sphere.

        Damage follows the rotating star beam's authored range and width. Its
        beam cross-section is checked in 3D so it does not hit players standing
        above or below the visible blue strip.
        """

        combat = getattr(s, "gameplay_combat_state", None)
        if combat is None:
            return
        if not is_tower_layout(runtime.map_id):
            return
        encounter = combat.encounter
        static_path = str(encounter.static_database_path)
        now = time.perf_counter()
        with combat.lock:
            towers = tuple(
                (
                    actor_id,
                    monster,
                    float(combat.monster_yaws.get(actor_id, 0.0)),
                )
                for actor_id, monster in combat.monsters.items()
                if is_tower_mechanism(
                    static_path, runtime.map_id, monster.template_id,
                )
                and combat.monster_health.get(actor_id, 0) > 0
            )
        if not towers:
            return
        world = (
            float(position[0]), float(position[1]), float(position[2])
        )
        # Permanent monster effects carry StartTick=0 in NewMonster's status
        # snapshot. The client rotates this aura from that shared game clock,
        # not from the moment each viewer's reliable spawn packet is sent.
        # ``server_time`` is the same 25 Hz clock used for status StartTick.
        phase_now = server_time() / 1000.0
        samples = [(world, phase_now)]
        if previous_position is not None:
            start = tuple(float(value) for value in previous_position[:3])
            distance = math.dist(start, world)
            start_at = float(previous_position_at or phase_now)
            spatial_samples = math.ceil(distance / 0.20)
            temporal_samples = math.ceil(
                max(0.0, phase_now - start_at)
                / TOWER_BEAM_CHECK_INTERVAL
            )
            sample_count = min(
                128, max(1, spatial_samples, temporal_samples)
            )
            samples = [
                (
                    tuple(
                        start[axis]
                        + (world[axis] - start[axis]) * (index / sample_count)
                        for axis in range(3)
                    ),
                    start_at
                    + (phase_now - start_at) * (index / sample_count),
                )
                for index in range(sample_count + 1)
            ]
        hits = []
        for actor_id, monster, base_yaw in towers:
            spec = _tower_aura_spec(static_path, monster.template_id)
            if spec is None:
                continue
            radius, interval, beam_width, rotation_speed, start_angle = spec
            centre = monster.position
            viewer_beam_started_at = getattr(
                s, "gameplay_tower_beam_started_at", {}
            )
            # The permanent aura status snapshot carries StartTick=0; this
            # value is shared by all viewers, regardless of spawn packet time.
            beam_started_at = viewer_beam_started_at.get(int(actor_id), 0.0)
            # Monster yaw zero points along -Z in the game coordinate frame.
            # Compare the player's capsule center to the closest point on the
            # finite beam segment. This includes the authored player capsule
            # radius without expanding the whole 8-unit aura sphere.
            beam_radius = beam_width * 0.5
            contact_distance = beam_radius + TOWER_PLAYER_COLLISION_RADIUS
            beam_touched = False
            contact = None
            for sample, sample_at in samples:
                if sample_at < beam_started_at:
                    continue
                beam_yaw = (
                    base_yaw
                    + math.radians(start_angle)
                    + math.radians(
                        rotation_speed * (sample_at - beam_started_at)
                    )
                )
                dx = sample[0] - float(centre[0])
                dy = sample[1] - float(centre[1])
                dz = sample[2] - float(centre[2])
                direction_x = -math.sin(beam_yaw)
                direction_z = -math.cos(beam_yaw)
                along = max(
                    0.0,
                    min(radius, dx * direction_x + dz * direction_z),
                )
                offset_x = dx - direction_x * along
                offset_z = dz - direction_z * along
                lateral_distance = math.hypot(offset_x, offset_z)
                vertical_gap = max(
                    0.0,
                    abs(dy) - TOWER_PLAYER_CAPSULE_HALF_HEIGHT,
                )
                distance = math.sqrt(
                    lateral_distance ** 2 + vertical_gap ** 2
                )
                if distance <= contact_distance:
                    beam_touched = True
                    contact = (
                        sample_at,
                        sample,
                        distance,
                        contact_distance,
                    )
                    break
            if not beam_touched:
                continue
            hits.append((actor_id, monster, interval, contact))
        if not hits:
            return
        applied_any = False
        for actor_id, monster, interval, _contact in hits:
            with s.flow_lock:
                if (
                    s.gameplay_combat_state is not combat
                    or s.gameplay_map_event_runtime is not runtime
                ):
                    return
                cooldowns = getattr(s, "gameplay_tower_aura_next_at", None)
                if cooldowns is None:
                    cooldowns = {}
                    s.gameplay_tower_aura_next_at = cooldowns
                if now < float(cooldowns.get(actor_id, 0.0)):
                    continue
                cooldowns[actor_id] = now + interval
                inventory = s.gameplay_inventory
                actor = int(
                    getattr(s, "gameplay_remote_actor_id", 0)
                    or combat.player_actor_id
                )
            if inventory is None:
                return
            damage = max(
                1,
                int(round(
                    (float(monster.minimum_damage)
                     + float(monster.maximum_damage)) / 2.0
                )),
            )
            with combat.lock:
                health = combat.player_health.get(actor)
                if health is None or int(health[0]) <= 0:
                    continue
                previous, maximum = int(health[0]), int(health[1])
                current = max(0, previous - damage)
                applied = previous - current
                health[0] = current
                if actor == int(combat.player_actor_id):
                    combat.player_current_health = current
            if applied <= 0:
                continue
            applied_any = True
            CharacterService._cancel_respawn_protection(s)
            with s.flow_lock:
                if s.gameplay_inventory is inventory:
                    inventory.health_points = float(current)
                server_tick = int(server_time() // 40) & 0xFFFFFFFF
            CharacterService._send_combat_resolution(
                s,
                DamageResolution(
                    target_actor_id=CharacterService._viewer_actor_id(s, actor),
                    attacker_actor_id=0,
                    current_health=current,
                    maximum_health=maximum,
                    damage=applied,
                    killed=current <= 0,
                    position=tuple(float(value) for value in position),
                    server_tick=server_tick,
                    hit_tags=(0,),
                    skill_index=0,
                    source_actor_id=0,
                    periodic_damage=True,
                ),
                label="fractured-planes-tower-aura",
                include_item_set_procs=False,
            )
        if applied_any:
            contact_summary = tuple(
                (
                    tower_id,
                    round(contact[2], 3),
                    round(contact[3], 3),
                )
                for tower_id, _monster, _interval, contact in hits
            )
            _log(
                f"Fractured Planes tower aura hit on {runtime.map_id}: "
                f"actor={actor}, position="
                f"{tuple(round(value, 3) for value in world)}, "
                f"contacts={contact_summary}"
            )

    @staticmethod
    def _check_trigger_volumes(
            s: Session, wire_position, *, previous_wire_position=None,
            previous_wire_position_at=None) -> None:
        """Detect trigger-volume entry from the player's own movement."""

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            return
        frame = getattr(s, "gameplay_coordinate_frame", None)
        if frame is None:
            combat = getattr(s, "gameplay_combat_state", None)
            if combat is None:
                return
            frame = combat.encounter.coordinate_frame
        world = frame.wire_to_world(wire_position)
        previous_world = (
            frame.wire_to_world(previous_wire_position)
            if previous_wire_position is not None
            else None
        )
        MapEventService._apply_fractured_planes_hazard_damage(
            s, runtime, world,
        )
        MapEventService._apply_tower_aura_damage(
            s, runtime, world, previous_position=previous_world,
            previous_position_at=previous_wire_position_at,
        )
        if not runtime.content.trigger_volumes:
            return
        with runtime.lock:
            volumes = runtime.active_trigger_volumes()
        inside = {
            volume.row_id for volume in volumes if volume.contains(world)
        }
        with s.flow_lock:
            previous = set(getattr(s, "gameplay_trigger_volumes_inside", ()))
            previous_position = getattr(
                s, "gameplay_trigger_volumes_last_position", None
            )
            if previous_position is None:
                # The first movement packet can already be beyond a trigger
                # volume (especially after a map transfer). Sweep the path
                # from the authored entry position so that the initial
                # tutorial and other entry triggers cannot be skipped.
                previous_position = getattr(s, "gameplay_spawn_position", None)
            s.gameplay_trigger_volumes_inside = inside
            s.gameplay_trigger_volumes_last_position = world
        entered = inside - previous
        if previous_position is not None:
            entered.update(
                volume.row_id
                for volume in volumes
                if volume.row_id not in previous
                and volume.intersects_segment(previous_position, world)
            )
        if not entered:
            return
        # Both the volume id (server signal rules) and its TriggeredEventId
        # (quest explore tasks) are raised.
        trigger_ids = tuple(dict.fromkeys(
            value
            for volume in volumes if volume.row_id in entered
            for value in (volume.trigger_id, volume.triggered_event_id)
            if value
        ))
        _log(f"player entered trigger volume(s) {trigger_ids!r}")
        callback = getattr(s, "gameplay_presence_callback", None)
        if callback is not None:
            callback(s, ("map_signal", "PlayerHitTrigger", trigger_ids))

    def _handle_prop_interaction(self, s: Session, payload: bytes) -> None:
        request = decode_prop_interaction(
            payload, getattr(s, "current_message_bit_length", None)
        )
        with s.flow_lock:
            s.gameplay_client_command_state[PROP_INTERACTION_COMMAND] = request
        CharacterService._accept_gameplay_state_command(
            s, PROP_INTERACTION_COMMAND
        )
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            _log(f"PropInteraction {request!r} without a map runtime")
            return
        reward_gate = _PARALLEL_WORLD_BOSS_REWARD_CHESTS.get(
            map_key(runtime.map_id)
        )
        if reward_gate is not None:
            with runtime.lock:
                candidate = runtime.props.get(int(request.prop_id))
                chest_definition = (
                    candidate.definition if candidate is not None else None
                )
                chest_unlocked = reward_gate[1] in runtime.boss_reward_chests_unlocked
            if (
                chest_definition is not None
                and chest_definition.instance_id.casefold()
                in PARALLEL_WORLD_REWARD_CHEST_INSTANCE_IDS
                and not chest_unlocked
            ):
                # Keep the chest visible and restore its closed state after a
                # premature click. Do this before IndividualUsage consumes
                # the one-shot prop.
                update = PropUpdate(
                    prop_id=chest_definition.prop_id,
                    state=PROP_STATE_ACTIVE,
                    actor_id=LOCAL_PLAYER_ACTOR_ID,
                    counter=_session_game_tick(s),
                    state_changed=True,
                    operation=WIRE_PROP_OPERATION,
                    task_index=0,
                    keep_visual_state=False,
                )
                packet, packet_bits = encode_prop_update_batch(
                    (update,), actor_id=0,
                )
                s.send_reliable_ordered(
                    (packet, packet_bits), track=True,
                    label="boss-chest-locked",
                )
                _send_prop_info(
                    s,
                    (PropRuntimeState(chest_definition, PROP_STATE_ACTIVE),),
                    label="boss-chest-locked-info",
                )
                _log(
                    f"locked boss reward chest {chest_definition.instance_id!r} "
                    f"until {reward_gate[0]} dies"
                )
                return
        with runtime.lock:
            existing = runtime.props.get(int(request.prop_id))
            definition = existing.definition if existing is not None else None
            if definition is not None and definition.individual_usage:
                usage_key = (
                    _individual_prop_player_id(s), int(definition.prop_id),
                )
                if (
                    existing.state == PROP_STATE_ACTIVE
                    and usage_key not in runtime.individual_prop_usage
                ):
                    runtime.individual_prop_usage.add(usage_key)
                    result = (
                        PropRuntimeState(definition, PROP_STATE_USED), True,
                    )
                else:
                    result = (
                        PropRuntimeState(definition, PROP_STATE_USED), False,
                    )
            else:
                result = runtime.interact(request.prop_id)
        if result is None:
            _log(
                f"PropInteraction for unknown prop {request.prop_id} "
                f"({request.operation!r}) on {runtime.map_id}"
            )
            return
        state, flipped = result
        definition = state.definition
        quest_operation = CharacterService._quest_prop_operation(s, definition)
        if quest_operation is None:
            _log(
                f"PropInteraction on quest prop {definition.instance_id!r} "
                "that is not live for this player; ignored"
            )
            return
        _log(
            f"prop {definition.instance_id!r} (id {definition.prop_id}, "
            f"operation {request.operation!r}) used; state now {state.state}"
            f"{' (changed)' if flipped else ''}"
        )
        if flipped:
            self._spawn_pw022_gorgon_from_bust(s, runtime, definition)
        # Retail's reply to a click (docs/PROP_AND_TRIGGER_COMMANDS.md §6):
        # the clicking player's actor in the body, the new state, the game
        # tick, every flag false, trailing actor 0, then a one-record
        # PropInfo refreshing the same prop.
        remote_actor_id = int(getattr(s, "gameplay_remote_actor_id", 0) or 0)
        viewers = (
            (s,) if definition.individual_usage
            else self._map_event_sessions(runtime) or (s,)
        )
        for viewer in viewers:
            if CharacterService._quest_prop_operation(viewer, definition) is None:
                continue
            update = PropUpdate(
                prop_id=definition.prop_id,
                state=int(state.state),
                actor_id=LOCAL_PLAYER_ACTOR_ID if viewer is s else remote_actor_id,
                counter=_session_game_tick(viewer),
                state_changed=False,
                operation=WIRE_PROP_OPERATION,
                task_index=0,
                keep_visual_state=False,
            )
            packet, packet_bits = encode_prop_update_batch((update,), actor_id=0)
            viewer.send_reliable_ordered(
                (packet, packet_bits), track=True, label="prop-update"
            )
            _send_prop_info(viewer, (state,), label="prop-update-info")
        if flipped and (definition.loot_list_id or definition.chest_id):
            self._spawn_map_chest_loot(s, definition)
        if state.state == PROP_STATE_ACTIVE or flipped:
            CharacterService._quest_on_prop_interacted(
                self, s, definition.instance_id,
                # A prop can be reused by several quest tasks. Its definition
                # stores only one authored switch ID, while the live switch
                # depends on this player's active quest state. Resolve that
                # switch dynamically so later tasks receive their own event.
                quest_operation or request.operation,
            )
            self._fire_map_signal(
                s, "PlayerInteractedProp",
                (definition.instance_id, definition.template_id),
            )
            CharacterService._achievement_on_prop_interacted(
                self, s, definition.instance_id, definition.template_id,
            )

    def _spawn_pw022_gorgon_from_bust(self, s, runtime, definition) -> None:
        """Spawn the guardian linked to a one-shot Mount Suvius bust prop."""

        if map_key(runtime.map_id) != "pw022_mount_suvius_01_dun":
            return
        prop_id = str(definition.instance_id or "").casefold()
        prop_suffix = prop_id.removeprefix("pw022_gorgon_busts_spawn_")
        if (
            not prop_id.startswith("pw022_gorgon_busts_spawn_")
            or len(prop_suffix) != 2
            or not prop_suffix.isdigit()
        ):
            return
        try:
            map_path = map_database_path(self.static_database_path, runtime.map_id)
            with closing(sqlite3.connect(
                f"file:{Path(self.static_database_path).as_posix()}?mode=ro", uri=True
            )) as static_database:
                state_object = static_database.execute(
                    "SELECT COALESCE(MonsterId, ''), COALESCE(SpawnPoint, '') "
                    "FROM _Template_StateObject WHERE Id=? LIMIT 1",
                    (definition.template_id,),
                ).fetchone()
            if state_object is None:
                _log(f"Gorgon bust template {definition.template_id!r} was not found")
                return
            monster_id, spawn_point = (str(value or "").strip() for value in state_object)
            if not monster_id or not spawn_point:
                _log(
                    f"Gorgon bust template {definition.template_id!r} has no "
                    "monster or spawn-point binding"
                )
                return
            with closing(sqlite3.connect(
                f"file:{map_path.as_posix()}?mode=ro", uri=True
            )) as map_database:
                anchor = map_database.execute(
                    "SELECT rowid, Transform FROM _Instance_SpawnPoint "
                    "WHERE lower(COALESCE(_ID, ''))=? "
                    "OR lower(COALESCE(Id, ''))=? "
                    "OR lower(COALESCE(Name, ''))=? "
                    "ORDER BY rowid LIMIT 1",
                    (spawn_point.casefold(),) * 3,
                ).fetchone()
            if anchor is None:
                _log(
                    f"Gorgon bust {definition.instance_id!r} spawn point "
                    f"{spawn_point!r} was not found on {runtime.map_id}"
                )
                return
            try:
                matrix = struct.unpack("<16f", bytes(anchor[1]))
                position = (float(matrix[12]), float(matrix[13]), float(matrix[14]))
            except (TypeError, ValueError, struct.error) as exc:
                _log(
                    f"Gorgon bust {definition.instance_id!r} has an invalid "
                    f"spawn transform: {exc}"
                )
                return
            if _load_runtime_definition(self.static_database_path, monster_id, 0) is None:
                _log(f"Gorgon bust monster template {monster_id!r} is unavailable")
                return
            row = GatedSpawnRow(
                -2_000_000 - int(anchor[0]), -1, monster_id, position,
            )
            sessions = self._map_event_sessions(runtime)
            if s not in sessions:
                sessions = (*sessions, s)
            self._spawn_event_monsters(runtime, sessions, (), extra_rows=(row,))
            _log(
                f"Gorgon bust {definition.instance_id!r} spawned {monster_id!r} "
                f"at {position!r}"
            )
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            _log(
                f"Gorgon bust spawn failed for {definition.instance_id!r}: "
                f"{type(exc).__name__}: {exc}"
            )

    def _spawn_f4203_ram_on_entry(self, s, runtime) -> None:
        """Start Crypt of Kings' scripted ram run toward the arena gate."""

        if map_key(runtime.map_id) != "f4203_boss":
            return
        try:
            # The arena door belongs to the boss06_phase_0 EventSet. The
            # scripted ram bypasses the retail generator interaction, so start
            # that authored phase here as well; otherwise the obstacle is not
            # instantiated and clients reject the ram's state update.
            gate_event = runtime.catalog.event("boss06_phase_0")
            if gate_event is None:
                _log("Crypt of Kings boss gate event boss06_phase_0 is missing")
                return
            with runtime.lock:
                gate_event_running = gate_event.key in runtime.running
            if not gate_event_running:
                self._start_map_local_event(runtime, gate_event)

            map_path = map_database_path(self.static_database_path, runtime.map_id)
            with closing(sqlite3.connect(
                f"file:{map_path.as_posix()}?mode=ro", uri=True
            )) as map_database:
                mechanism = map_database.execute(
                    "SELECT COALESCE(MechanismId, '') "
                    "FROM _Instance_StateObject "
                    "WHERE lower(COALESCE(_Level, ''))=? "
                    "AND lower(COALESCE(Id, ''))='boss06_ram_spawn_socket' "
                    "ORDER BY rowid LIMIT 1",
                    (map_key(runtime.map_id),),
                ).fetchone()
                route = map_database.execute(
                    "SELECT rowid, Transform FROM _Instance_Waypoint "
                    "WHERE lower(COALESCE(_Level, ''))=? "
                    "AND lower(COALESCE(PathName, ''))='ram' "
                    "ORDER BY WayPointIndex, rowid",
                    (map_key(runtime.map_id),),
                ).fetchall()
                door = map_database.execute(
                    "SELECT EventSetRow, COALESCE(Name, '') "
                    "FROM _Instance_Obstacle "
                    "WHERE lower(COALESCE(_Level, ''))=? "
                    "AND lower(COALESCE(Id, ''))='state_boss_door_01' "
                    "ORDER BY rowid LIMIT 1",
                    (map_key(runtime.map_id),),
                ).fetchone()
            if mechanism is None or not route or door is None:
                _log(f"Crypt of Kings ram route or boss door is missing on {runtime.map_id}")
                return
            with closing(sqlite3.connect(
                f"file:{Path(self.static_database_path).as_posix()}?mode=ro", uri=True
            )) as static_database:
                monster = static_database.execute(
                    "SELECT COALESCE(MonsterId, '') "
                    "FROM _Template_PropMechanism WHERE Id=? LIMIT 1",
                    (str(mechanism[0] or "").strip(),),
                ).fetchone()
            monster_id = str(monster[0] or "").strip() if monster else ""
            if not monster_id:
                _log(
                    f"Crypt of Kings ram mechanism {mechanism[0]!r} has no monster"
                )
                return
            try:
                start_matrix = struct.unpack("<16f", bytes(route[0][1]))
                goal_matrix = struct.unpack("<16f", bytes(route[-1][1]))
                position = tuple(float(start_matrix[i]) for i in (12, 13, 14))
                goal_position = tuple(float(goal_matrix[i]) for i in (12, 13, 14))
            except (TypeError, ValueError, struct.error) as exc:
                _log(f"Crypt of Kings ram route has invalid transform: {exc}")
                return
            if _load_runtime_definition(self.static_database_path, monster_id, 0) is None:
                _log(f"Crypt of Kings ram monster template {monster_id!r} is unavailable")
                return
            row = GatedSpawnRow(
                -2_100_000 - int(route[0][0]), -1, monster_id, position,
            )
            sessions = self._map_event_sessions(runtime)
            if s not in sessions:
                sessions = (*sessions, s)
            self._spawn_event_monsters(runtime, sessions, (), extra_rows=(row,))

            combat = self._map_event_combat_state(sessions)
            if combat is None:
                _log(f"Crypt of Kings ram has no combat state on {runtime.map_id}")
                return
            with combat.lock:
                ram_actor_ids = tuple(sorted(
                    actor_id for actor_id, actor in combat.monsters.items()
                    if actor.template_id.casefold() == monster_id.casefold()
                    and combat.monster_health.get(actor_id, 0) > 0
                ))
                if not ram_actor_ids:
                    _log(f"Crypt of Kings ram actor {monster_id!r} was not materialized")
                    return
                ram_actor_id = ram_actor_ids[0]
                combat.scripted_monster_targets[ram_actor_id] = goal_position

            with runtime.lock:
                runtime.f4203_ram_actor_id = ram_actor_id
                runtime.f4203_ram_goal = goal_position
                runtime.f4203_ram_door_event_set_row = int(door[0])
                runtime.f4203_ram_door_obstacle_id = (
                    str(door[1] or "").strip() or "door_01"
                )
                should_start_path_check = not (
                    runtime.f4203_ram_path_started
                    or runtime.f4203_ram_gate_opened
                )
                runtime.f4203_ram_path_started = True
                gate_already_open = runtime.f4203_ram_gate_opened
                impact_already_started = runtime.f4203_ram_impact_started
            if gate_already_open:
                self._open_f4203_ram_gate(runtime, sessions)
                return
            if impact_already_started:
                self._publish_f4203_ram_obstacle_state(
                    runtime, sessions, 1, label="f4203-ram-gate-break",
                )
                return
            if should_start_path_check:
                scheduler = getattr(runtime, "scheduler", None)
                if scheduler is None:
                    _log("Crypt of Kings ram route has no map scheduler")
                    return
                timer = scheduler.timer(
                    0.25, self._check_f4203_ram_impact, args=(runtime,),
                )
                timer.start()
            _log(
                f"Crypt of Kings ram started its authored run: "
                f"actor={ram_actor_id}, from={position!r}, to={goal_position!r}"
            )
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            _log(
                f"Crypt of Kings ram spawn failed on {runtime.map_id}: "
                f"{type(exc).__name__}: {exc}"
            )

    def _check_f4203_ram_impact(self, runtime) -> None:
        if not self._map_event_runtime_is_live(runtime):
            return
        with runtime.lock:
            if runtime.f4203_ram_gate_opened:
                return
            actor_id = runtime.f4203_ram_actor_id
            goal = runtime.f4203_ram_goal
        sessions = self._map_event_sessions(runtime)
        combat = self._map_event_combat_state(sessions)
        if actor_id is None or goal is None or combat is None:
            return
        with combat.lock:
            position = combat.monster_positions.get(actor_id)
            alive = combat.monster_health.get(actor_id, 0) > 0
        if not alive or position is None:
            return
        # The authored final waypoint is several metres outside the door's
        # obstacle origin. The ram can also be stopped by the gate collision
        # before reaching that exact point, so treat the approach zone as the
        # impact point instead of waiting for an unreachable 3 m radius.
        if math.hypot(position[0] - goal[0], position[2] - goal[2]) <= 6.0:
            self._start_f4203_ram_impact(runtime, sessions)
            _log(
                f"Crypt of Kings ram struck the boss gate at "
                f"{tuple(round(value, 2) for value in position)!r}"
            )
            return
        scheduler = getattr(runtime, "scheduler", None)
        if scheduler is not None:
            timer = scheduler.timer(
                0.25, self._check_f4203_ram_impact, args=(runtime,),
            )
            timer.start()

    def _start_f4203_ram_impact(self, runtime, sessions) -> None:
        with runtime.lock:
            if runtime.f4203_ram_impact_started or runtime.f4203_ram_gate_opened:
                return
            runtime.f4203_ram_impact_started = True
        # `goinginactive` plays the authored five-second metal-gate collapse
        # animation. Complete to `inactive` afterward so the opening cannot
        # get stranded if a client misses the animation's final transition.
        self._publish_f4203_ram_obstacle_state(
            runtime, sessions, 1, label="f4203-ram-gate-impact",
        )
        scheduler = getattr(runtime, "scheduler", None)
        if scheduler is None:
            self._open_f4203_ram_gate(runtime, sessions)
            return
        timer = scheduler.timer(
            5.0, self._open_f4203_ram_gate, args=(runtime, tuple(sessions)),
        )
        timer.start()

    def _publish_f4203_ram_obstacle_state(
            self, runtime, sessions, state: int, *, label: str) -> None:
        with runtime.lock:
            obstacle_id = runtime.f4203_ram_door_obstacle_id or "door_01"
        packet, packet_bits = encode_server_command_batch((
            ServerCommand(
                OBSTACLE_UPDATE_COMMAND,
                ObstacleUpdate((ObstacleState(obstacle_id, state),)),
                LOCAL_PLAYER_ACTOR_ID,
            ),
        ))
        for viewer in sessions:
            CharacterService._send_world_loot_state_batch(
                viewer, packet, packet_bits, label=label,
            )

    def _open_f4203_ram_gate(self, runtime, sessions) -> None:
        with runtime.lock:
            door_event_set_row = runtime.f4203_ram_door_event_set_row
            runtime.f4203_ram_gate_opened = True
            runtime.initial_flags = frozenset(
                flag for flag in runtime.initial_flags
                if flag != "boss06_phase_0"
            )
            if door_event_set_row is not None:
                runtime.suppress_event_sets((door_event_set_row,))
        update = EventUpdate((EventUpdateEntry(
            event_id=0,
            event_name="boss06_phase_0",
            flags=(False, True, False, False),
            starts_at=None,
            ends_at=None,
            tags=(),
        ),))
        packet, packet_bits = encode_event_update(update)
        for viewer in sessions:
            CharacterService._send_world_loot_state_batch(
                viewer, packet, packet_bits, label="f4203-ram-gate-break"
            )
        self._publish_f4203_ram_obstacle_state(
            runtime, sessions, 2, label="f4203-ram-gate-open",
        )

    # ------------------------------------------------------------------
    # Event transitions
    # ------------------------------------------------------------------

    def _schedule_map_local_event_start(
            self, runtime: MapEventRuntime,
            definition: MapLocalEventDefinition) -> None:
        wave = definition.wave_label is not None
        delay = max(5.0, definition.start_delay) if wave else definition.start_delay
        if delay <= 0.0:
            self._start_map_local_event(runtime, definition)
            return
        scheduler = getattr(runtime, "scheduler", None)
        if scheduler is None:
            self._start_map_local_event(runtime, definition)
            return
        with runtime.lock:
            if definition.key in runtime.running or definition.key in runtime.pending:
                return
            token = object()
            deadline = time.perf_counter() + delay
            handle = scheduler.timer(
                delay,
                self._run_delayed_map_event_start,
                args=(runtime, definition, token),
            )
            handle.absolute_deadline = deadline
            runtime.pending[definition.key] = handle
            runtime.pending_start_deadlines[definition.key] = deadline
            runtime.pending_start_tokens[definition.key] = token
        _log(
            f"{definition.event_id} starts on {runtime.map_id} in "
            f"{delay:.1f}s"
        )
        handle.start()
        if wave:
            for seconds in range(5, 0, -1):
                remaining = deadline - seconds - time.perf_counter()
                if remaining <= 0:
                    self._run_wave_countdown(runtime, definition, token)
                else:
                    countdown = scheduler.timer(
                        remaining, self._run_wave_countdown,
                        args=(runtime, definition, token),
                    )
                    countdown.absolute_deadline = deadline - seconds
                    countdown.start()

    def _run_wave_countdown(self, runtime, definition, token) -> None:
        if not self._map_event_runtime_is_live(runtime):
            return
        with runtime.lock:
            if runtime.pending_start_tokens.get(definition.key) is not token:
                return
            # Read the real deadline so a delayed callback cannot replay old
            # digits or announce a wave that was cancelled and rescheduled.
            seconds = math.ceil(
                runtime.pending_start_deadlines[definition.key] - time.perf_counter()
            )
            if not 1 <= seconds <= 5:
                return
            if runtime.wave_countdown_digits.get(definition.key) == seconds:
                return
            runtime.wave_countdown_digits[definition.key] = seconds
        # The client's native countdown (0x1405e5c7c) displays this integer
        # without changing match state. No zero/"Go" PvP message is sent.
        self._broadcast_wave_message(
            runtime, encode_message_batch(
                Message(5, (MessageArgument(1, seconds),)), actor_id=0,
            ), label="map-wave-countdown",
        )

    def _schedule_fractured_planes_limit_tick(self, runtime) -> None:
        with runtime.lock:
            deadline = runtime.fractured_planes_limit_deadline
            scheduler = runtime.scheduler
            if (
                deadline is None
                or runtime.fractured_planes_floor_completed
                or scheduler is None
                or runtime.fractured_planes_limit_timer is not None
            ):
                return
            delay = min(1.0, max(0.0, deadline - time.perf_counter()))
            handle = scheduler.timer(
                delay,
                self._run_fractured_planes_limit_tick,
                args=(runtime,),
            )
            handle.absolute_deadline = time.perf_counter() + delay
            runtime.fractured_planes_limit_timer = handle
        handle.start()

    def _run_fractured_planes_limit_tick(self, runtime) -> None:
        with runtime.lock:
            runtime.fractured_planes_limit_timer = None
            deadline = runtime.fractured_planes_limit_deadline
            if deadline is None or runtime.fractured_planes_floor_completed:
                return
            seconds_left = max(0, math.ceil(deadline - time.perf_counter()))
            floor_clear_check_armed = runtime.fractured_planes_kill_seen
        packet, packet_bits = encode_server_command_batch((
            ServerCommand(
                KICK_OUT_TO_HUB_LIMIT_TIME_LEFT_COMMAND,
                KickOutToHubLimitTimeLeft(seconds_left),
                1,
            ),
        ))
        for viewer in self._map_event_sessions(runtime):
            try:
                with runtime.lock:
                    if runtime.fractured_planes_floor_completed:
                        return
                with viewer.flow_lock:
                    combat = viewer.gameplay_combat_state
                    move = viewer.gameplay_last_move
                    position = viewer.gameplay_spawn_position
                # Reconcile the boss gate on the normal floor heartbeat too.
                # A final kill notification can be missed during a disconnect
                # or actor retirement; without this check the score bar can
                # remain below its authored threshold forever even though the
                # player cleared every visible slot.
                if (
                    combat is not None
                    and floor_clear_check_armed
                    and _fractured_planes_floor_number_from_map(runtime.map_id)
                ):
                    self._resolve_fractured_planes_floor_clear(
                        viewer, runtime, log_blockers=False,
                    )
                if combat is not None and move is not None:
                    position = combat.encounter.coordinate_frame.wire_to_world(
                        move.relative_position
                    )
                if seconds_left and position is not None:
                    self._apply_fractured_planes_hazard_damage(
                        viewer, runtime, position,
                    )
                CharacterService._send_world_loot_state_batch(
                    viewer, packet, packet_bits,
                    label="fractured-planes-limit-time",
                )
            except Exception as exc:
                _log(
                    "fractured-planes limit timer send failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        if seconds_left:
            self._schedule_fractured_planes_limit_tick(runtime)

    def _schedule_fractured_planes_tower_tick(self, runtime) -> None:
        with runtime.lock:
            scheduler = runtime.scheduler
            if (
                runtime.fractured_planes_limit_deadline is None
                or runtime.fractured_planes_floor_completed
                or scheduler is None
                or runtime.fractured_planes_tower_timer is not None
            ):
                return
            handle = scheduler.timer(
                TOWER_BEAM_CHECK_INTERVAL,
                self._run_fractured_planes_tower_tick,
                args=(runtime,),
            )
            runtime.fractured_planes_tower_timer = handle
        handle.start()

    def _run_fractured_planes_tower_tick(self, runtime) -> None:
        with runtime.lock:
            runtime.fractured_planes_tower_timer = None
            active = (
                runtime.fractured_planes_limit_deadline is not None
                and not runtime.fractured_planes_floor_completed
            )
        if not active:
            return
        sessions = self._map_event_sessions(runtime)
        if not sessions:
            return
        for viewer in sessions:
            with viewer.flow_lock:
                combat = viewer.gameplay_combat_state
                move = viewer.gameplay_last_move
                position = viewer.gameplay_spawn_position
            if combat is None or position is None:
                continue
            if move is not None:
                position = combat.encounter.coordinate_frame.wire_to_world(
                    move.relative_position
                )
            try:
                self._apply_tower_aura_damage(viewer, runtime, position)
            except Exception as exc:
                _log(
                    "Fractured Planes tower beam tick failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        self._schedule_fractured_planes_tower_tick(runtime)

    def _broadcast_wave_message(self, runtime, packet, *, label) -> None:
        for viewer in self._map_event_sessions(runtime):
            try:
                CharacterService._send_world_loot_state_batch(
                    viewer, *packet, label=label,
                )
            except Exception as exc:
                _log(f"{label} send failed: {type(exc).__name__}: {exc}")

    def _flush_due_map_event_starts(self, runtime: MapEventRuntime) -> None:
        # Reward conditions must see elapsed deadlines even if a scheduler is
        # busy or its original session disconnected. Never restart a timer for
        # a late joiner to an existing dungeon instance.
        now = time.perf_counter()
        with runtime.lock:
            due = tuple((key, runtime.pending_start_tokens.get(key))
                        for key, deadline in runtime.pending_start_deadlines.items()
                        if deadline <= now)
        for key, token in due:
            definition = runtime.catalog.event(key)
            if definition is not None:
                self._start_map_local_event(runtime, definition, pending_token=token)

    def _run_delayed_map_event_start(
            self, runtime: MapEventRuntime,
            definition: MapLocalEventDefinition, token: object) -> None:
        if not self._map_event_runtime_is_live(runtime):
            return
        try:
            self._start_map_local_event(runtime, definition, pending_token=token)
        except Exception as exc:
            _log(
                f"delayed start of {definition.event_id} failed: "
                f"{type(exc).__name__}: {exc}"
            )

    def _run_map_local_event_stop(
            self, runtime: MapEventRuntime,
            definition: MapLocalEventDefinition) -> None:
        if not self._map_event_runtime_is_live(runtime):
            return
        try:
            self._stop_map_local_event(runtime, definition)
        except Exception as exc:
            _log(
                f"timed stop of {definition.event_id} failed: "
                f"{type(exc).__name__}: {exc}"
            )

    def _start_map_local_event(
            self, runtime: MapEventRuntime,
            definition: MapLocalEventDefinition, *, pending_token=None) -> None:
        with runtime.lock:
            if (pending_token is not None
                    and runtime.pending_start_tokens.get(definition.key) is not pending_token):
                return
            transition = runtime.begin_event(definition)
        if transition is None:
            return
        _log(
            f"{definition.event_id} started on {runtime.map_id}: "
            f"enabled sets {transition.enabled_sets}, "
            f"disabled sets {transition.disabled_sets}"
        )
        self._apply_map_event_transition(runtime, transition)
        # if definition.wave_label is not None:
        #     self._broadcast_wave_message(
        #         runtime, encode_screen_message(definition.wave_label),
        #         label="map-wave-start",
        #     )
        scheduler = getattr(runtime, "scheduler", None)
        if definition.duration > 0.0 and scheduler is not None:
            handle = scheduler.timer(
                definition.duration,
                self._run_map_local_event_stop,
                args=(runtime, definition),
            )
            handle.start()

    def _stop_map_local_event(
            self, runtime: MapEventRuntime,
            definition: MapLocalEventDefinition) -> None:
        with runtime.lock:
            transition = runtime.end_event(definition)
        if transition is None:
            return
        _log(
            f"{definition.event_id} stopped on {runtime.map_id}: "
            f"enabled sets {transition.enabled_sets}, "
            f"disabled sets {transition.disabled_sets}"
        )
        self._apply_map_event_transition(runtime, transition)

    def _apply_map_event_transition(self, runtime: MapEventRuntime,
                                    transition) -> None:
        sessions = self._map_event_sessions(runtime)
        if transition.enabled_sets:
            # Cache newly gated exits without activating them. The event-set
            # controller creates their scene entities on EventUpdate; sending
            # active=True before that edge can hide the closed gate while the
            # portal entity is still absent. The normal refresh below marks
            # them active after the entity has been created.
            self._publish_map_exits(
                runtime, sessions,
                event_sets=transition.enabled_sets,
                active_override=False,
                update_session_state=False,
            )
        if transition.announce:
            self._broadcast_map_event_update(runtime, sessions, transition)
        if transition.disabled_sets:
            self._retire_event_monsters(runtime, sessions, transition.disabled_sets)
            self._unpublish_event_props(runtime, sessions, transition.disabled_sets)
        if transition.enabled_sets:
            self._publish_event_props(runtime, sessions, transition.enabled_sets)
            self._spawn_event_monsters(runtime, sessions, transition.enabled_sets)
            self._spawn_event_npcs(runtime, sessions, transition.enabled_sets)
        if transition.enabled_sets or transition.disabled_sets:
            self._publish_map_exits(runtime, sessions)
        if transition.active and transition.event.key == "boss_spawn":
            self._publish_entrance_bosses(runtime, sessions)
            if map_key(runtime.map_id) in _PARALLEL_WORLD_BOSS_REWARD_CHESTS:
                # The arena boss is spawned by its BOSS_spawn EventSet after
                # the client enters. Publish the reward props again after
                # _spawn_event_monsters has sent the boss actor, so the chest
                # is created in the loaded scene alongside the boss.
                for viewer in sessions:
                    self._publish_parallel_world_boss_reward_chest_props(
                        viewer, runtime,
                    )
                _log(
                    f"published boss reward chests after boss spawn on "
                    f"{runtime.map_id} to {len(sessions)} player(s)"
                )

    def _publish_entrance_bosses(self, runtime, sessions) -> None:
        """Release baseline encounter actors only after their entrance signal."""
        if runtime.map_id.casefold() not in APPROACH_BOSS_MAPS:
            return
        combat = self._map_event_combat_state(sessions)
        if combat is None:
            return
        with combat.lock:
            boss_ids = tuple(
                actor_id for actor_id, monster in combat.monsters.items()
                if monster.rank_id.casefold().startswith("rank_boss")
                and combat.monster_health.get(actor_id, 0) > 0
            )
        for viewer in sessions:
            if not viewer.gameplay_presence_ready:
                continue
            for actor_id in boss_ids:
                group = combat.group_for_actor(actor_id)
                if group is not None:
                    self._announce_runtime_group(viewer, combat, group, force_actor_ids=boss_ids)

    # ------------------------------------------------------------------
    # Effects
    # ------------------------------------------------------------------

    def _broadcast_map_event_update(self, runtime, sessions, transition) -> None:
        definition = transition.event
        # A map-local EventUpdate must describe the full state of the map's
        # event flags. Sending only currently running events leaves any event
        # that ended earlier latched on the client, which can keep an EventSet
        # (and its trap effects) visible after a later platform interaction.
        with runtime.lock:
            active_flags = set(runtime.active_flags())
            catalog = runtime.catalog
            referenced = set(runtime.content.referenced_events)
            referenced.update(runtime.running)
            referenced.add(definition.key)
        definitions = {}
        target_map = map_key(runtime.map_id)
        for event_key in referenced:
            event = catalog.event(event_key)
            if event is not None and (
                target_map in event.maps
                # Some extracted local events have no explicit map list even
                # though this map's EventSet references them or its runtime
                # starts them (for example boss06_phase_0 and BOSS_death_dtu).
                # Preserve those entries, but never include events explicitly
                # authored for another map: the client rejects that update.
                or not event.maps
            ):
                definitions[event.key] = event
        entries = tuple(
            EventUpdateEntry(
                # Retail encodes every map-local event with id 0; the client
                # resolves the local controller from the event name. Sending
                # the catalog id here makes full snapshots disagree with the
                # targeted portal update and can leave gated exits unsynced.
                event_id=0,
                event_name=event.event_id,
                flags=(event_key in active_flags, True, False, False),
                starts_at=None,
                ends_at=None,
                tags=(),
            )
            for event_key, event in sorted(definitions.items())
        )
        update = EventUpdate(tuple(entries))
        packet, packet_bits = encode_event_update(update)
        for viewer in sessions:
            CharacterService._send_world_loot_state_batch(
                viewer, packet, packet_bits, label="map-event-update"
            )
        _log(
            f"published map-event state update on {runtime.map_id}: "
            f"changed={definition.event_id} active={transition.active}, "
            f"snapshot_events={len(entries)}, active_events="
            f"{tuple(sorted(key for key in definitions if key in active_flags))}, "
            f"sessions={len(sessions)}"
        )

    def _publish_event_props(self, runtime, sessions, event_sets) -> None:
        with runtime.lock:
            published = runtime.publish_props(
                runtime.select_props(runtime.content.props_for_sets(event_sets))
            )
        if not published:
            return
        _log(
            f"revealed {len(published)} prop(s) on {runtime.map_id}: "
            + ", ".join(sorted({
                state.definition.template_id for state in published
            })[:6])
        )
        for viewer in sessions:
            _send_prop_info(viewer, published, label="map-event-prop-info")

    def _unpublish_event_props(self, runtime, sessions, event_sets) -> None:
        with runtime.lock:
            prop_ids = tuple(
                prop_id for prop_id, state in runtime.props.items()
                if state.definition.event_set_row in set(event_sets)
            )
            removed = runtime.unpublish_props(prop_ids)
        if not removed:
            return
        _log(f"hid {len(removed)} prop(s) on {runtime.map_id}")
        for viewer in sessions:
            packet, packet_bits = encode_discard_prop_batch(
                removed, actor_id=LOCAL_PLAYER_ACTOR_ID
            )
            CharacterService._send_world_loot_state_batch(
                viewer, packet, packet_bits, label="map-event-discard-prop"
            )

    def _publish_map_exits(
            self, runtime, sessions, *, event_sets=None,
            active_override=None, update_session_state=True) -> None:
        with runtime.lock:
            flags = runtime.active_flags()
        try:
            definitions = tuple(
                definition
                for definition in load_local_map_exits(
                    self.static_database_path,
                    runtime.map_id,
                    active_event_flags=flags,
                )
                if definition.active and definition.destination_map_id
            )
        except Exception as exc:
            _log(f"exit refresh failed: {type(exc).__name__}: {exc}")
            return
        parallel_world_boss_map = map_key(runtime.map_id) in _PARALLEL_WORLD_EXIT_BOSSES
        tiers = _exit_access_difficulties(str(self.static_database_path)) if parallel_world_boss_map else {}
        available = (
            {
                tiers[d.destination_map_id] for d in definitions
                if d.destination_map_id in tiers
            }
            if parallel_world_boss_map else set()
        )
        if event_sets is not None:
            wanted_event_sets = {int(value) for value in event_sets}
            definitions = tuple(
                d for d in definitions
                if d.event_set_row in wanted_event_sets
            )
        if not definitions:
            return
        for viewer in sessions:
            with viewer.flow_lock:
                if getattr(viewer, "gameplay_exit_transition_started", False):
                    continue
            # The runtime's scaling difficulty is used for monster stats and
            # may combine the selected map difficulty with a scaling slider.
            # Exit labels must follow the actual access group chosen for this
            # session, which is stored separately on the session.
            selected_access_id = str(
                getattr(viewer, "gameplay_map_access_id", "") or ""
            )
            selected_tier = (
                int(tiers.get(
                    selected_access_id,
                    getattr(
                        viewer, "gameplay_map_difficulty",
                        runtime.scaling_difficulty,
                    ),
                ))
                if parallel_world_boss_map else 0
            )
            if available and selected_tier not in available:
                _log(
                    f"no boss exit tier {selected_tier} for selected access "
                    f"{selected_access_id!r} on {runtime.map_id}; "
                    f"available={sorted(available)}"
                )
            exit_entries = []
            active_viewer_definitions = []
            # Filter before the closed-gate snapshot too. The native client
            # merges ExitInfo into its Name/GUID cache; a nonselected GUID
            # cached even once allows that overlapping variation to spawn.
            viewer_definitions = filter_parallel_world_exits_for_access(
                self.static_database_path,
                runtime.map_id,
                definitions,
                selected_map_access_id=selected_access_id,
                selected_map_difficulty=selected_tier,
            )
            native_exit_entries = native_scene_exit_variation_entries(
                runtime.map_id, viewer_definitions,
            )
            native_exit_guids = {
                entry.instance_guid for entry in native_exit_entries
            }
            if native_exit_entries and active_override is not False:
                # Some authored exits start as inactive and have no client
                # entity yet. ExitInfo only updates the cache; VariationInfo
                # creates the gate after EventUpdate activates its scene set.
                # The client records each processed GUID and ignores repeat
                # creation requests even if a later event removed the entity.
                # The pre-event closed snapshot must only prime ExitInfo.
                packet, packet_bits = encode_server_command_batch((
                    ServerCommand(
                        VARIATION_INFO_COMMAND,
                        VariationInfo(native_exit_entries),
                        0,
                    ),
                ), include_terminator=True)
                CharacterService._send_world_loot_state_batch(
                    viewer, packet, packet_bits,
                    label="map-event-native-exit-variation-info",
                )
                if map_key(runtime.map_id) == "h0001_hub":
                    _log(
                        f"published native exit creation on {runtime.map_id}: "
                        f"event_sets={tuple(entry.event_set_row for entry in native_exit_entries)}"
                    )
            for definition in viewer_definitions:
                if (
                    active_override is False
                    and definition.instance_guid in native_exit_guids
                ):
                    # This is the pre-EventUpdate snapshot. The client has
                    # not created these native scene exits yet, so asking it
                    # to update them now only produces an UpdateExitState
                    # "entity not found" warning. The post-event refresh
                    # publishes them after VariationInfo creates the entity.
                    continue
                is_active = definition.active
                if active_override is not None:
                    entry_active = active_override
                else:
                    entry_active = is_active
                if is_active:
                    active_viewer_definitions.append(definition)
                exit_entries.append((definition, entry_active))
            if parallel_world_boss_map:
                # Boss rooms have a separate staged exit flow. Keep its
                # explicit per-entry active override unchanged.
                exit_info_entries = tuple(
                    ExitInfoEntry(
                        definition.name,
                        definition.destination_map_id,
                        int(entry_active),
                        definition.instance_guid,
                    )
                    for definition, entry_active in exit_entries
                )
            elif active_override is None:
                exit_info_entries = _exit_info_entries_for_access(
                    self.static_database_path,
                    runtime.map_id,
                    definitions,
                    viewer_definitions,
                )
            else:
                exit_info_entries = tuple(
                    ExitInfoEntry(
                        definition.name,
                        definition.destination_map_id,
                        int(entry_active),
                        definition.instance_guid,
                    )
                    for definition, entry_active in exit_entries
                )
            exit_info = ExitInfo(exit_info_entries)
            packet, packet_bits = encode_server_command_batch(
                (ServerCommand(
                    EXIT_INFO_COMMAND, exit_info, 0,
                ),),
                include_terminator=True,
            )
            with viewer.flow_lock:
                if viewer.gameplay_map != runtime.map_id:
                    continue
                if update_session_state:
                    viewer.gameplay_map_exits = tuple(active_viewer_definitions)
            CharacterService._send_world_loot_state_batch(
                viewer, packet, packet_bits, label="map-event-exit-info"
            )
            if parallel_world_boss_map:
                _log(
                    f"boss exits for {getattr(viewer, 'addr', '?')}: access="
                    f"{getattr(viewer, 'gameplay_map_access_id', '')!r} "
                    f"difficulty={selected_tier}; wire_guids={len(exit_entries)}; "
                    f"stage={'closed' if active_override is False else 'open'}; active="
                    f"{[d.destination_map_id for d in active_viewer_definitions]}"
                )
        _log(
            f"refreshed exits on {runtime.map_id} for "
            f"{len(sessions)} session(s)"
        )

    def _map_event_combat_state(self, sessions):
        for session in sessions:
            combat = getattr(session, "gameplay_combat_state", None)
            if combat is not None:
                return combat
        return None

    def _spawn_event_npcs(self, runtime, sessions, event_sets) -> None:
        """Create the NPCs of newly enabled event sets.

        Retail created the tutorial's spellweaver with ``NewNPC`` and its
        ``NPCInfo`` when the ``a0001_start_spellweaver`` map-local event
        started (tutorial capture, 16:05:23); the map initialization only
        carries NPCs whose event set is enabled by the global event flags.
        The NPCInfo is decorated per viewer with that player's quest offers.
        """

        wanted = {int(value) for value in event_sets}
        try:
            definitions = tuple(
                definition
                for definition in load_local_npc_definitions(
                    self.static_database_path, runtime.map_id,
                    include_inactive_event_sets=True,
                )
                if int(getattr(definition, "event_set_row", -1)) in wanted
            )
        except Exception as exc:
            _log(f"event NPCs unavailable on {runtime.map_id}: {type(exc).__name__}: {exc}")
            return
        if not definitions:
            return
        _log(
            f"revealed {len(definitions)} NPC(s) on {runtime.map_id}: "
            + ", ".join(sorted({d.template_id for d in definitions})[:6])
        )
        for viewer in sessions:
            with viewer.flow_lock:
                announced = set(viewer.gameplay_announced_npc_actor_ids)
                viewer.gameplay_announced_npc_actor_ids.update(
                    local_npc_actor_id(definition)
                    for definition in definitions
                )
            commands = []
            new_actor_ids = []
            for definition in definitions:
                actor_id = local_npc_actor_id(definition)
                # ActorManager::RegisterActor() rejects a duplicate id and
                # DrasaClientHandler then drops the rest of this batch, which
                # would take the quest offers with it.
                if actor_id not in announced:
                    new_actor_ids.append(actor_id)
                    commands.append(NewNPC(
                        template_id=definition.template_id,
                        instance_guid=definition.instance_guid,
                        placement=ActorPlacement(
                            LOCAL_NPC_ACTOR_BASE, actor_id, 1, False, False
                        ),
                        rotation=definition.rotation,
                        position=definition.position,
                        enabled=True,
                        actor_id=actor_id,
                    ))
                commands.append(CharacterService._quest_decorate_npc_info(
                    self, viewer,
                    local_npc_info(definition, actor_id=actor_id),
                    definition.template_id,
                ))
            # Register dynamic NPC ids in their own reliable-ordered frame.
            # Map-entry actors use this same ordering; keeping registration
            # inside the NewNPC batch can leave late-created actors visible
            # without a client interaction target.
            if new_actor_ids:
                vicinity_packet, vicinity_bits = encode_actors_enter_vicinity_batch(
                    ActorsEnterVicinityBatch(
                        ActorsEnterVicinity(tuple(new_actor_ids)),
                        actor_id=0,
                        commands=(),
                    )
                )
                viewer.send_reliable_ordered(
                    vicinity_packet,
                    track=True,
                    label="map-event-npc-actor-vicinity",
                )
            packet, packet_bits = encode_npc_batch(
                NPCBatch(tuple(commands)), include_actor_vicinity=False,
            )
            CharacterService._send_world_loot_state_batch(
                viewer, packet, packet_bits, label="map-event-new-npc"
            )

    def _quest_spawn_monsters(self, s: Session, specifications) -> None:
        """Materialize quest ``<Spawn>`` entries at their authored map points."""

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None or not specifications:
            return
        try:
            difficulty = int(getattr(
                s, "gameplay_map_difficulty", runtime.scaling_difficulty,
            ))
        except (TypeError, ValueError):
            difficulty = int(runtime.scaling_difficulty)
        selected = []
        seen_specs = set()
        for specification in specifications:
            if specification.difficulty:
                try:
                    if int(specification.difficulty) != difficulty:
                        continue
                except (TypeError, ValueError):
                    continue
            key = (
                str(specification.template_id).casefold(),
                str(specification.location).casefold(),
            )
            if key not in seen_specs:
                seen_specs.add(key)
                selected.append(specification)
        if not selected:
            return

        path = map_database_path(self.static_database_path, runtime.map_id)
        with sqlite3.connect(path) as database:
            locations = {}
            for specification in selected:
                location = str(specification.location).casefold()
                if location in locations:
                    continue
                rows = database.execute(
                    """
                    SELECT rowid, ParentNodeIndex, Transform
                    FROM _Instance_SpawnPoint
                    WHERE lower(COALESCE(Name, ''))=?
                       OR lower(COALESCE(_ID, ''))=?
                    ORDER BY rowid
                    """,
                    (location, location),
                ).fetchall()
                points = []
                for row_id, parent_index, transform in rows:
                    try:
                        matrix = struct.unpack("<16f", bytes(transform))
                    except (TypeError, ValueError, struct.error):
                        continue
                    points.append((
                        int(row_id), int(parent_index or 0),
                        (float(matrix[12]), float(matrix[13]), float(matrix[14])),
                    ))
                if points:
                    # A few PW maps reuse one location name for difficulty
                    # variants that sit in different regions. Use the same
                    # ordered-parent selection as density-area variants.
                    parents = {}
                    for point in points:
                        parents.setdefault(point[1], point)
                    if 0 in parents:
                        locations[location] = parents[0]
                    else:
                        variants = [parents[key] for key in sorted(parents)]
                        index = min(
                            max(0, int(runtime.scaling_difficulty)),
                            len(variants) - 1,
                        )
                        locations[location] = variants[index]

        spawn_rows = []
        for index, specification in enumerate(selected):
            location = str(specification.location).casefold()
            point = locations.get(location)
            if point is None:
                _log(
                    f"quest spawn point {specification.location!r} was not "
                    f"found on {runtime.map_id}"
                )
                continue
            row_id, _parent_index, position = point
            if _load_runtime_definition(
                self.static_database_path, specification.template_id, 0,
            ) is None:
                _log(
                    f"quest spawn template {specification.template_id!r} "
                    "has no runtime definition"
                )
                continue
            spawn_rows.append(GatedSpawnRow(
                -1_000_000 - row_id - index,
                -1,
                str(specification.template_id),
                position,
            ))
        if spawn_rows:
            sessions = self._map_event_sessions(runtime)
            if s not in sessions:
                sessions = (*sessions, s)
            self._spawn_event_monsters(
                runtime, sessions, (), extra_rows=tuple(spawn_rows),
            )

    def _spawn_event_monsters(
            self, runtime, sessions, event_sets, *, extra_rows=(),
    ) -> None:
        rows = (*runtime.gated_spawn_rows(event_sets), *tuple(extra_rows))
        # Scene controllers are event-graph objects, not renderable combatants.
        # In particular, publishing one as a NewMonster can leave the client
        # with a registered actor that has no effect module and abort the rest
        # of the initial spawn batch.
        rows = tuple(
            row for row in rows
            if not is_tower_controller(
                self.static_database_path, runtime.map_id, row.template_id
            )
        )
        authored_rotations = {row.row_id: row.rotation for row in rows}
        # An EventSet keeps one row per difficulty variant of its boss (the
        # Heredur arena's BOSS_spawn set holds ten ``boss_heredur_scaling``
        # rows and the spawner).  The load-time population already picks one
        # phase through the boss policy; the event path must too, or every
        # variant walks in at once.
        selected = select_boss_spawn_rows(
            self.static_database_path,
            runtime.map_id,
            tuple(
                (row.row_id, row.position, row.template_id, row.event_set_row)
                for row in rows
            ),
            scaling_difficulty=runtime.scaling_difficulty,
        )
        rows = tuple(
            GatedSpawnRow(
                int(r[0]), int(r[3]), str(r[2]), tuple(r[1]),
                authored_rotations.get(int(r[0]), 0.0),
            )
            for r in selected
        )
        combat = self._map_event_combat_state(sessions)
        if combat is None:
            _log(
                f"{len(rows)} gated spawn row(s) on {runtime.map_id} but no "
                "combat state on the instance"
            )
            return
        encounter = combat.encounter
        database_path = encounter.static_database_path
        rows += event_area_spawn_rows(runtime, encounter, event_sets)
        # The authored cultist animset maps Death to cultist_steal_staff.
        # Its idle is start_idle; the staff theft uses its scripted despawn
        # transition, not with an extra Grimmag/China scene carrier.
        if not rows:
            # Endless Tower floors 2-100 keep their room density areas
            # unassigned (EventSetRow=-1), so their monsters are already in
            # the shared encounter but are not owned/announced by the room's
            # gated spawn set. When the room event opens, bind and publish the
            # living actors in the area nearest that room's authored start
            # prop. This makes the trap objective's blockers visible and
            # keeps their combat/clear state tied to the right room.
            if _fractured_planes_floor_number_from_map(runtime.map_id) > 1:
                room_rows = _fractured_planes_room_event_rows(runtime)
                props = tuple(
                    (str(prop.instance_id), tuple(prop.position))
                    for prop in runtime.content.props
                )
                regions = dict(_fractured_planes_floor_regions(
                    str(database_path), runtime.map_id, room_rows, props,
                ))
                row_rooms = {
                    int(row_id): (room, signal)
                    for row_id, room, signal in room_rows
                }
                for event_set in event_sets:
                    room_info = row_rooms.get(int(event_set))
                    if room_info is None:
                        continue
                    room, signal = room_info
                    volumes = regions.get(signal, ())
                    if not volumes:
                        continue
                    with combat.lock:
                        room_actor_ids = tuple(sorted(
                            int(actor_id)
                            for actor_id, monster in combat.monsters.items()
                            if combat.monster_health.get(actor_id, 0) > 0
                            and not is_tower_mechanism(
                                database_path, runtime.map_id, monster.template_id,
                            )
                            and any(volume.contains((
                                monster.position[0], volume.matrix[13],
                                monster.position[2],
                            )) for volume in volumes)
                        ))
                        if room_actor_ids:
                            combat.activate_monsters(room_actor_ids)
                    if not room_actor_ids:
                        continue
                    with runtime.lock:
                        runtime.event_set_actor_ids.setdefault(
                            int(event_set), set()
                        ).update(room_actor_ids)
                    groups = {}
                    for actor_id in room_actor_ids:
                        group = combat.group_for_actor(actor_id)
                        if group is not None:
                            groups.setdefault(id(group), (group, set()))[1].add(
                                actor_id
                            )
                    for viewer in sessions:
                        with viewer.flow_lock:
                            ready = viewer.gameplay_presence_ready
                        if ready:
                            for group, actor_ids in groups.values():
                                self._announce_runtime_group(
                                    viewer, combat, group,
                                    force_actor_ids=actor_ids,
                                )
                    _log(
                        f"Fractured Planes room {room} activated and "
                        f"published {len(room_actor_ids)} authored monster(s) "
                        f"for {signal}: {room_actor_ids}"
                    )
            return
        with combat.lock:
            alive_actor_ids = set(combat.alive_actor_ids)
            alive_bosses_by_template: dict[str, list[int]] = {}
            for aid in alive_actor_ids:
                m = combat.monsters.get(aid)
                if m is not None and is_boss_spawn_template(database_path, m.template_id):
                    alive_bosses_by_template.setdefault(m.template_id.casefold(), []).append(aid)

            existing_by_row = {}
            for row in rows:
                existing = tuple(sorted(
                    set(combat.find_monsters(
                        row.template_id, row.position,
                        radius=EVENT_SPAWN_DUPLICATE_RADIUS,
                    )) & alive_actor_ids
                ))
                if not existing and is_boss_spawn_template(database_path, row.template_id):
                    existing = tuple(sorted(alive_bosses_by_template.get(row.template_id.casefold(), ())))
                existing_by_row[row.row_id] = existing

            pending = tuple(
                row for row in rows if not existing_by_row[row.row_id]
            )
            actor_ids = combat.allocate_runtime_actor_ids(len(pending))
        # A map can materialize an event-gated spawn while assembling its
        # initial world.  In that case the duplicate guard correctly avoids a
        # second actor, but the actor still needs ownership by this EventSet so
        # it can be announced, counted for the objective, and retired later.
        # Register only live matching actors; a dead actor at the authored
        # point must not suppress a fresh event spawn.
        if any(existing_by_row.values()):
            with runtime.lock:
                for row in rows:
                    actor_ids_for_row = existing_by_row[row.row_id]
                    if actor_ids_for_row:
                        runtime.event_set_actor_ids.setdefault(
                            int(row.event_set_row), set()
                        ).update(actor_ids_for_row)
        if not pending:
            # A map-local event can start from a trigger while the player's
            # first 0x85 initialization is still being assembled. In that
            # window there are no ready sessions to receive the spawn, but
            # the event transition still needs to materialize its actors in
            # the shared combat state. The initialization path calls this
            # method again once PlayerReady has been queued; replay the
            # already materialized event groups to that viewer then.
            with runtime.lock:
                event_actor_ids = set().union(*(
                    runtime.event_set_actor_ids.get(int(event_set), set())
                    for event_set in event_sets
                )) if event_sets else set()
            groups = []
            seen_groups = set()
            actor_positions = {}
            for actor_id in sorted(event_actor_ids):
                # The map loader can materialize gated bosses as part of the
                # encounter's authored population. Resolve both authored and
                # runtime groups so the duplicate guard can still publish
                # that existing actor when its event starts.
                group = combat.group_for_actor(actor_id)
                if group is None or id(group) in seen_groups:
                    continue
                seen_groups.add(id(group))
                groups.append(group)
                with combat.lock:
                    actor_positions[actor_id] = combat.monster_positions.get(
                        actor_id
                    )
            for viewer in sessions:
                with viewer.flow_lock:
                    ready = viewer.gameplay_presence_ready
                if ready:
                    for group in groups:
                        # Existing in server combat state does not mean the
                        # client received NewMonster. The announcement helper
                        # reserves each sent ID under the viewer's flow lock.
                        self._announce_runtime_group(viewer, combat, group)
            matched_actor_ids = tuple(sorted(set().union(*(
                set(actor_ids_for_row)
                for actor_ids_for_row in existing_by_row.values()
            ))))
            _log(
                f"{len(rows)} gated spawn row(s) on {runtime.map_id} already "
                f"materialized as actor(s) {matched_actor_ids} at "
                f"{actor_positions!r}; replayed actor state to "
                f"{sum(1 for viewer in sessions if viewer.gameplay_presence_ready)} "
                "ready session(s)"
            )
            return
        monsters = []
        definitions = []
        spawned_rows = []
        for row, actor_id in zip(pending, actor_ids, strict=True):
            position = row.position
            scripted_cultist = (
                row.template_id.casefold() == _TUTORIAL_CULTIST_TEMPLATE
            )
            mounted = is_tower_mechanism(
                database_path, runtime.map_id, row.template_id,
            )
            # Lythira's level-100 template is authored with coefficient-like
            # values (50 HP, 6-8 damage) but carries CharLevel=100. Treating
            # those as already-scaled legacy stats strips the level curve and
            # leaves the tenth-floor boss with only a few hundred HP. Keep the
            # full player-level curve for this boss, like level-one templates.
            coefficient_stats = (
                mounted
                or row.template_id.casefold() == "fp_boss_01_lythira"
            )
            # The cinematic's staff, portal and root motion are authored
            # against this exact transform, not a navigable combat position.
            if (
                not scripted_cultist and not mounted
                and not is_boss_spawn_template(database_path, row.template_id)
            ):
                try:
                    position = _grounded_spawn_position(
                        encounter.navigation,
                        encounter.terrain,
                        position,
                        reference_position=position,
                        max_horizontal_distance=EVENT_SPAWN_SNAP_DISTANCE,
                    )
                except (NavigationError, CollisionTerrainError, ValueError):
                    position = tuple(float(value) for value in row.position)
            runtime_definition = _load_runtime_definition(
                database_path, row.template_id, actor_id, stationary=mounted,
            )
            if runtime_definition is None:
                _log(f"no runtime template for {row.template_id!r}; skipped")
                continue
            if (
                runtime.map_id.casefold() == "f4203_boss"
                and row.template_id.casefold() == "f4203_boss_06_ram"
            ):
                # The ram follows its authored path far beyond the template's
                # ordinary chase leash; its fixed target is the boss gate.
                runtime_definition = replace(
                    runtime_definition,
                    run_speed=max(4.0, float(runtime_definition.run_speed)),
                    escape_range=max(220.0, float(runtime_definition.escape_range)),
                    travel_distance=max(220.0, float(runtime_definition.travel_distance)),
                )
            try:
                monster = _load_monster(
                    database_path,
                    row.template_id,
                    actor_id,
                    position,
                    monster_level=runtime.monster_level,
                    scaling_difficulty=runtime.scaling_difficulty,
                    coefficient_stats=coefficient_stats,
                    non_targetable=is_tower_hardware(
                        database_path, runtime.map_id, row.template_id
                    ),
                )
            except ValueError as exc:
                _log(f"cannot load {row.template_id!r}: {exc}")
                continue
            if mounted:
                monster = replace(monster, field_a0=float(row.rotation))
            if scripted_cultist:
                monster = replace(monster, field_a0=float(row.rotation))
                _log(
                    f"tutorial cultist authored placement: "
                    f"position={monster.position!r}, yaw={row.rotation:.6f}"
                )
            monsters.append(monster)
            definitions.append(runtime_definition)
            spawned_rows.append(row)
        if not monsters:
            return
        group = _spawn_group(database_path, tuple(monsters))
        ammo = combat.load_ammo_vulnerabilities(
            database_path, group.spawn_batch.commands
        )
        with combat.lock:
            added = combat.add_runtime_monsters(
                group, definitions, ammo_vulnerabilities=ammo
            )
        with runtime.lock:
            for row, monster in zip(spawned_rows, monsters, strict=True):
                runtime.event_set_actor_ids.setdefault(
                    row.event_set_row, set()
                ).add(int(monster.actor_id))
        _log(
            f"spawned {len(added)} monster(s) on {runtime.map_id}: "
            + ", ".join(sorted({monster.template_id for monster in monsters}))
        )
        for viewer in sessions:
            with viewer.flow_lock:
                ready = viewer.gameplay_presence_ready
            if ready:
                self._announce_runtime_group(viewer, combat, group)

        scene_actor_ids = tuple(
            monster.actor_id
            for row, monster in zip(spawned_rows, monsters, strict=True)
            if row.template_id.casefold() == _TUTORIAL_CULTIST_TEMPLATE
        )
        if scene_actor_ids:
            scheduler = getattr(runtime, "scheduler", None)
            if scheduler is None:
                _log("cannot start tutorial cultist scene: map scheduler is unavailable")
            else:
                # Publish the authored actor before switching its animation
                # from start_idle to the authored staff-theft sequence.
                handle = scheduler.timer(
                    0.35,
                    self._start_tutorial_cultist_sequence,
                    args=(runtime, combat, tuple(sessions), scene_actor_ids),
                )
                handle.start()
                _log(
                    f"queued tutorial cultist despawn sequence after actor "
                    f"initialization for {scene_actor_ids!r}"
                )

    def _start_tutorial_cultist_sequence(
            self, runtime, combat, sessions, scene_actor_ids) -> None:
        if not self._map_event_runtime_is_live(runtime):
            return
        with combat.lock:
            tick = max(
                (
                    int(getattr(viewer, "gameplay_chase_server_tick", 0) or 0)
                    for viewer in sessions
                ),
                default=0,
            )
            # Native KillCommand +0x78 maps to Messaging::Kill::SetDespawn.
            # Despawn bypasses the client's ordinary Death clip and loads
            # despawn/a0001_cultist_death, whose hijacked Death track plays
            # cultist_steal_staff. No damage types: this is a scripted scene.
            scene_commands = tuple(
                KillServerCommand(
                    Kill(
                        server_tick=tick,
                        hit_tags=(),
                        animation_time=600,
                        field_4c=0,
                        killer_actor_id=0,
                        position=tuple(combat.monster_positions[actor_id]),
                        skill_id=0,
                        projectile_id=0xFFFFFFFF,
                        field_78=True,
                    ),
                    int(actor_id),
                )
                for actor_id in scene_actor_ids
                if actor_id in combat.monster_positions
            )
        if scene_commands:
            packet, packet_bits = encode_combat_batch(
                CombatBatch(scene_commands)
            )
            stun_effects = ()
            database_path = Path(combat.encounter.static_database_path)
            try:
                with sqlite3.connect(
                    f"file:{database_path.as_posix()}?mode=ro", uri=True
                ) as database:
                    stun_effects = combat._load_location_aura_effects(
                        database, "a0001_tutorial_sequence_stun_aura"
                    )
            except Exception as exc:
                _log(
                    "could not load tutorial cultist stun effect: "
                    f"{type(exc).__name__}: {exc}"
                )
            for viewer in sessions:
                with viewer.flow_lock:
                    if viewer.gameplay_combat_state is not combat:
                        continue
                CharacterService._send_world_loot_state_batch(
                    viewer, packet, packet_bits,
                    label="tutorial-cultist-scripted-despawn",
                )
                if stun_effects:
                    CharacterService._send_socket_actor_applications(
                        viewer,
                        ((LOCAL_PLAYER_ACTOR_ID, None, stun_effects),),
                        server_tick=tick,
                        label="tutorial-cultist-sequence-stun",
                    )
            if stun_effects:
                _log(
                    "applied authored tutorial cultist stun to "
                    f"{len(sessions)} session(s) for "
                    f"{max(effect.duration_ticks for effect in stun_effects)} "
                    "ticks"
                )
            else:
                _log("tutorial cultist stun aura resolved to no receiver effects")
        with combat.lock:
            retired_scene_ids = combat.retire_monsters(scene_actor_ids)
        with runtime.lock:
            for actor_ids_for_set in runtime.event_set_actor_ids.values():
                actor_ids_for_set.difference_update(retired_scene_ids)
        _log(
            f"started authored tutorial cultist despawn sequence for "
            f"actor(s) {retired_scene_ids!r}"
        )

    def _announce_runtime_group(
            self, viewer: Session, combat, group, *, force_actor_ids=()) -> None:
        """Offer a freshly spawned group to one viewer, like a MoveCommand does."""

        with viewer.flow_lock:
            if viewer.gameplay_combat_state is not combat:
                return
            encounter = viewer.gameplay_monster_encounter
            move = viewer.gameplay_last_move
            announced = set(viewer.gameplay_announced_monster_actor_ids)
            spawned = set(viewer.gameplay_spawned_monster_actor_ids)
            fractured_run = bool(
                getattr(viewer, "gameplay_fractured_planes_run", False)
            )
        if encounter is None:
            return
        if move is not None:
            player_world = encounter.coordinate_frame.wire_to_world(
                move.relative_position
            )
        else:
            spawn = getattr(viewer, "gameplay_spawn_position", None)
            if spawn is None:
                return
            player_world = tuple(float(value) for value in spawn)
        pending_entrance = CharacterService._pending_entrance_actor_ids(viewer, combat)
        with combat.lock:
            alive = set(combat.alive_actor_ids) - pending_entrance
            positions = dict(combat.monster_positions)
            scripted_actor_ids = {
                actor_id
                for actor_id in group.actor_id_set & alive
                if (
                    combat.monsters.get(actor_id) is not None
                    and (
                        combat.monsters[actor_id].template_id.casefold()
                        in _FORCE_ANNOUNCE_EVENT_ACTORS
                        or (
                            load_map_encounter_policy(
                                combat.encounter.static_database_path,
                                combat.encounter.map_id,
                            ).is_boss_room
                            and combat.monsters[actor_id].rank_id.casefold()
                            .startswith("rank_boss")
                        )
                        or is_tower_hardware(
                            combat.encounter.static_database_path,
                            combat.encounter.map_id,
                            combat.monsters[actor_id].template_id,
                        )
                        or (
                            fractured_run
                            and combat.monsters[actor_id].rank_id.casefold()
                            == "rank_boss_endlesstower"
                        )
                    )
                )
            }
        candidates = (group.actor_id_set & alive) - announced
        # Tower bosses already carry the native gold minimap icon. Publish
        # their actor at spawn regardless of distance so that icon is visible
        # across the map. Reserve NewMonster below to avoid duplicate actors.
        # Fixed tower hardware behaves the same way: a power node or shooter
        # that an event unlocks has to appear where it was authored, not when
        # the player happens to walk into it.
        forced = (
            (candidates | (scripted_actor_ids - spawned)) & scripted_actor_ids
        ) | (
            set(int(value) for value in force_actor_ids)
            & group.actor_id_set
            & alive
            - spawned
        )
        groups = list(CharacterService._monster_groups_within_radius(
            None,
            world_position=player_world,
            actor_ids=candidates - forced,
            positions=positions,
            radius=MONSTER_VISIBILITY_ENTER_RADIUS,
            extra_groups=(group,),
        ))
        # Scripted story actors and arena bosses must appear when their event
        # starts even outside the ordinary monster visibility radius.
        if forced:
            scripted_group = group.runtime_subset(
                forced, positions=positions
            )
            if scripted_group is not None:
                groups.append(scripted_group)
        if not groups:
            return
        with viewer.flow_lock:
            if viewer.gameplay_combat_state is not combat:
                return
            for subset in groups:
                viewer.gameplay_announced_monster_actor_ids.update(subset.actor_ids)
            viewer.gameplay_vicinity_announced = True
            # Scripted story actors must not depend on the client's optional
            # ActorRequest round trip to become visible. Reserve them before
            # announcing vicinity so a simultaneous request cannot enqueue a
            # duplicate NewMonster packet.
            scripted_spawn_ids = forced.difference(
                viewer.gameplay_spawned_monster_actor_ids
            )
            viewer.gameplay_spawned_monster_actor_ids.update(
                scripted_spawn_ids
            )
            viewer.gameplay_requested_actor_ids.update(scripted_spawn_ids)
            if scripted_spawn_ids:
                viewer.gameplay_monsters_spawned = True
        scripted_group = None
        if scripted_spawn_ids:
            scripted_group = next(
                (
                    subset for subset in groups
                    if scripted_spawn_ids.issubset(subset.actor_id_set)
                ),
                None,
            )
        for subset in groups:
            CharacterService._send_monster_vicinity(
                viewer, combat, subset,
                label="map-event-monster-vicinity",
            )
        if scripted_spawn_ids and scripted_group is not None:
            spawn_group = scripted_group.runtime_subset(
                scripted_spawn_ids, positions=positions
            )
            if spawn_group is not None:
                batch = CharacterService._new_monster_batch_for_viewer(
                    viewer, combat, spawn_group.spawn_batch,
                    label="map-event-forced-monster-spawn",
                )
            else:
                batch = None
            if batch is not None:
                packet, packet_bits = encode_monster_spawn_batch(batch)
                if len(packet) > TAIL_FRAG_SIZE:
                    viewer.send_reliable_ordered_split(
                        packet,
                        total_bit_length=packet_bits,
                        track=True,
                        label_prefix="map-event-forced-monster-spawn",
                    )
                else:
                    viewer.send_reliable_ordered(
                        (packet, packet_bits),
                        track=True,
                        label="map-event-forced-monster-spawn",
                    )
                with combat.lock:
                    combat.activate_monsters(scripted_spawn_ids)
                    # A blocker can die after the alive snapshot above but
                    # before this retry's NewMonster is delivered. Do not let
                    # that late snapshot leave an idle actor over the newer
                    # Kill command; end the ordered stream with a discard.
                    dead_after_spawn = tuple(
                        actor_id
                        for actor_id in scripted_spawn_ids
                        if combat.monster_health.get(actor_id, 0) <= 0
                    )
                    delivered_positions = {
                        actor_id: combat.monster_positions[actor_id]
                        for actor_id in scripted_spawn_ids
                        if actor_id in combat.monster_positions
                        and actor_id not in dead_after_spawn
                    }
                if dead_after_spawn:
                    with viewer.flow_lock:
                        if viewer.gameplay_combat_state is combat:
                            viewer.gameplay_spawned_monster_actor_ids.difference_update(
                                dead_after_spawn
                            )
                            CharacterService._forget_published_monsters(
                                viewer, dead_after_spawn
                            )
                            viewer.gameplay_announced_monster_actor_ids.difference_update(
                                dead_after_spawn
                            )
                            viewer.gameplay_requested_actor_ids.difference_update(
                                dead_after_spawn
                            )
                            for actor_id in dead_after_spawn:
                                viewer.gameplay_delivered_monster_positions.pop(
                                    actor_id, None
                                )
                            viewer.gameplay_monsters_spawned = bool(
                                viewer.gameplay_spawned_monster_actor_ids
                            )
                            viewer.gameplay_vicinity_announced = bool(
                                viewer.gameplay_announced_monster_actor_ids
                            )
                    viewer.send_reliable_ordered(
                        encode_discard_monster_batch(dead_after_spawn),
                        track=True,
                        label="map-event-monster-spawn-race-discard",
                    )
                    _log(
                        "discarded blocker(s) killed during map-event "
                        f"NewMonster retry: {dead_after_spawn!r}"
                    )
                with viewer.flow_lock:
                    if viewer.gameplay_combat_state is combat:
                        viewer.gameplay_delivered_monster_positions.update(
                            delivered_positions
                        )
                # Event-owned bosses and blockers bypass the ordinary
                # ActorRequest path. That path normally starts the map's
                # chase loop after NewMonster is delivered; without the same
                # wake-up here, these actors stay idle until a player casts a
                # skill (whose combat handler happens to restart the worker).
                if delivered_positions:
                    CharacterService._start_monster_chase(viewer)
                _log(
                    f"sent forced NewMonster state "
                    f"{tuple(sorted(scripted_spawn_ids))} to {viewer.addr!r}"
                )
        if forced:
            _log(
                f"force-published actor(s) {tuple(sorted(forced))} "
                f"to {viewer.addr!r}"
            )

    def _retire_event_monsters(self, runtime, sessions, event_sets) -> None:
        combat = self._map_event_combat_state(sessions)
        if combat is None:
            return
        wanted = set(int(value) for value in event_sets)
        rows = runtime.content.spawn_rows_for_sets(wanted)
        with runtime.lock:
            actor_ids = set()
            for event_set in wanted:
                actor_ids.update(runtime.event_set_actor_ids.pop(event_set, ()))
        with combat.lock:
            for row in rows:
                actor_ids.update(combat.find_monsters(
                    row.template_id, row.position,
                    radius=EVENT_SPAWN_DUPLICATE_RADIUS,
                ))
            retired = combat.retire_monsters(tuple(sorted(actor_ids)))
        if not retired:
            return
        _log(f"retired {len(retired)} monster(s) on {runtime.map_id}")
        retired_set = set(retired)
        for viewer in sessions:
            with viewer.flow_lock:
                if viewer.gameplay_combat_state is not combat:
                    continue
                discard = tuple(sorted(
                    retired_set & viewer.gameplay_spawned_monster_actor_ids
                ))
                viewer.gameplay_spawned_monster_actor_ids.difference_update(retired_set)
                CharacterService._forget_published_monsters(viewer, retired_set)
                viewer.gameplay_announced_monster_actor_ids.difference_update(retired_set)
                viewer.gameplay_requested_actor_ids.difference_update(retired_set)
                for actor_id in retired_set:
                    viewer.gameplay_delivered_monster_positions.pop(actor_id, None)
            if discard:
                viewer.send_reliable_ordered(
                    encode_discard_monster_batch(discard),
                    track=True,
                    label="map-event-discard-monster",
                )


__all__ = ["MapEventService"]
