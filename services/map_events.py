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

from dso_server.runtime import progression_queue, resident_content
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
    exit_display_map_id,
    filter_parallel_world_exits_for_access,
    load_local_map_exits,
    load_local_npc_definitions,
    load_local_spawn_position,
    local_npc_actor_id,
    local_npc_info,
    map_database_path,
    native_scene_exit_variation_entries,
)
from dso_server.gameplay.boss_encounter_policy import (
    APPROACH_BOSS_MAPS,
    is_boss_spawn_template,
    load_map_encounter_policy,
    resolve_boss_template_id,
    select_boss_spawn_rows,
)
from dso_server.gameplay.event_spawn_areas import event_area_spawn_rows
from dso_server.gameplay.event_system import completed_achievement_ids
from dso_server.gameplay.local_monster_state import (
    _bgh_boss_health_multiplier,
    _grounded_spawn_position,
    _load_monster,
    _load_runtime_definition,
    _spawn_group,
    is_new_year_dungeon_inert_helper,
    load_local_player_only_encounter,
)
from dso_server.gameplay.map_collision_terrain import CollisionTerrainError
from dso_server.gameplay.map_hazards import is_map_hazard
from dso_server.gameplay.map_event_state import (
    BLOODSHED_DIFFICULTY,
    CLIENT_REPORTED_SIGNAL_TYPES,
    DTU_ARENA_REWARD_CHEST_IDS,
    GatedSpawnRow,
    NEW_MOON_RECALL_ALTAR_IDS,
    PARALLEL_WORLD_REWARD_CHEST_INSTANCE_IDS,
    SARGON_SHADOWFORT_ALTAR_EVENT_PREFIX,
    SARGON_SHADOWFORT_EXIT_EVENT_KEYS,
    SARGON_SHADOWFORT_MAP_ID,
    SARGON_SHADOWFORT_SENTINEL_EVENT,
    SARGON_SHADOWFORT_SENTINEL_SIGNAL,
    SARGON_SHADOWFORT_SENTINEL_TEMPLATE,
    SARGON_THRONE_ROOM_MAP_ID,
    deduplicate_parallel_world_gatekeeper_rows,
    _dungeon_resource_category,
    WIRE_PROP_OPERATION,
    MapEventRuntime,
    MapLocalEventDefinition,
    MapPropDefinition,
    PropRuntimeState,
    SignalContext,
    TriggerVolumeDefinition,
    ZERO_GUID,
    is_dtu_boss_arena_map,
    load_map_event_content,
    load_map_signal_catalog,
    map_key,
    prop_visible_to_player,
)
from dso_server.gameplay.quest_content import (
    QUEST_STATE_ACCEPTED,
    QUEST_STATE_CLOSED,
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
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.inventory_economy_protocol import (
    refresh_inventory_economy_values,
)
from dso_server.protocol.world_loot_protocol import (
    WorldLootDrop,
    _difficulty_drop_chances,
    _threshold_value,
    possible_reward_rates,
    roll_possible_reward_gems,
    roll_new_equipment_enchantments,
    prime_world_loot_cache,
)
from dso_server.gameplay.prop_loot import (
    CHEST_DROP_SPACING,
    chest_drop_positions,
    load_prop_loot_catalog,
    roll_prop_loot,
)
from dso_server.gameplay.quest_content import load_quest_catalog
from dso_server.paths import DEFAULT_QUESTS_PATH
from dso_server.gameplay.mortis_arena import select_spawn_rows as select_mortis_spawn_rows
from dso_server.services.mortis_arena import arena as mortis_arena


_BGH_ALL_BOSS_SEQUENCE = {
    "e_nightmare_portal": "boss_heredur_bgh",
    "boss_heredur_bgh": "boss_sargon_bgh",
    "boss_sargon_bgh": "boss_herald_bgh",
    "boss_herald_bgh": "boss_nefertari_bgh",
    "boss_nefertari_bgh": "boss_balor_bgh",
    "boss_balor_bgh": "boss_mortis_mounted_bgh",
}
_BGH_SINGLE_BOSS_PORTALS = {
    "e_bgh_boss_balor": (
        "boss_balor_bgh_single", "e_nightmare_portal_balor",
    ),
    "e_bgh_boss_guardian_black": (
        "bgh_guardian_boss_andermagic", "e_nightmare_portal_guardian_black",
    ),
    "e_bgh_boss_herald": (
        "boss_herald_bgh_single", "e_nightmare_portal_herald",
    ),
    "e_bgh_boss_heredur": (
        "boss_heredur_bgh_single", "e_nightmare_portal_heredur",
    ),
    "e_bgh_boss_mortis": (
        "boss_mortis_mounted_bgh_single", "e_nightmare_portal_mortis",
    ),
    "e_bgh_boss_nefertari": (
        "boss_nefertari_bgh_single", "e_nightmare_portal_nefertari",
    ),
    "e_bgh_boss_sargon": (
        "boss_sargon_bgh_single", "e_nightmare_portal_sargon",
    ),
}
_BGH_OLD_GLORY_RING_TEMPLATES = frozenset({
    "warrior_unique_ring_bgh", "mage_unique_ring_bgh",
    "ranger_unique_ring_bgh", "dwarf_unique_ring_bgh",
})


_MORTIS_MIRROR_ATTACK_TEMPLATES = {
    "warrior": "uniskel_pw212_elysium_hero_03_1_template",
    # The Elysium mage and ranger quest templates have no useful primary
    # attack (the mage's only loaded skill is a zero-damage vortex; the
    # ranger is stationary and inherits a companion-only shot). Use the
    # player-class animations and attack set from active hostile templates.
    "mage": "uniskel_e_anniversary_helios_guardian_mage_01_1_template",
    "ranger": "uniskel_e_anniversary_helios_guardian_ranger_01_1_template",
    "dwarf": "uniskel_dwarf_e_ks_infested_leader_dwarf_1_template",
}

GATHERABLE_MULTITOOL_TEMPLATE = "crafting_stateobject_key"
GATHERABLE_BROKEN_MULTITOOL_TEMPLATE = "crafting_stateobject_key_broken"
GATHERABLE_AUTO_PICKUP_DELAY_SECONDS = 0.8
EASTER_HUB_EGG_RESPAWN_SECONDS = 1.0


def _multitool_resource_category(definition) -> str:
    """Category of an authored herb/ore node that consumes a Multitool."""

    category = _dungeon_resource_category(definition)
    loot_list = str(definition.loot_list_id or "").casefold()
    instance_id = str(definition.instance_id or "").casefold()
    if category == "herb" and (
        loot_list.startswith("main_herb_ingredient")
        or (
            instance_id == "ingredient_tree_01"
            and loot_list == "chest_lootlist_ingredient_tree"
        )
    ):
        return category
    if category == "ore" and (
        loot_list.startswith("main_ore_ingredient")
        or (
            instance_id == "ingredient_geode_01"
            and loot_list == "chest_lootlist_ingredient_geode"
        )
    ):
        return category
    return ""


@lru_cache(maxsize=4)
def _gatherable_ingredient_templates(
    static_database_path: str, category: str, rarity_tier: int,
) -> tuple[str, ...]:
    """Live herb/ore templates for the selected map difficulty band."""

    prefix = f"{str(category).casefold()}_ingredient_"
    path = Path(static_database_path).resolve()
    try:
        with closing(sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True,
        )) as database:
            rows = database.execute(
                "SELECT ItemTemplateId FROM _Template_IngredientBag "
                "WHERE COALESCE(IsLive, 0)=1 "
                "AND lower(COALESCE(ItemTemplateId, '')) LIKE ? "
                "AND IngredientBagRarity=? "
                "ORDER BY ItemTemplateId",
                (prefix + "%", f"Rarity_{int(rarity_tier):02d}"),
            ).fetchall()
    except sqlite3.Error:
        return ()
    return tuple(sorted({
        str(row[0]).strip() for row in rows if str(row[0] or "").strip()
    }))


@lru_cache(maxsize=64)
def _mortis_mirror_client_template(
    static_database_path: str, attack_template: str,
) -> str:
    """Use the class template id the client can resolve to a localized name."""

    try:
        database_uri = Path(static_database_path).resolve().as_uri()
        with sqlite3.connect(
            f"{database_uri}?mode=ro", uri=True,
        ) as database:
            row = database.execute(
                "SELECT CharacterSet FROM _Template_Monster "
                "WHERE Id=? LIMIT 1",
                (attack_template,),
            ).fetchone()
            character_template = str(row[0] or "") if row else ""
            if character_template and database.execute(
                "SELECT 1 FROM _Template_Monster WHERE Id=? LIMIT 1",
                (character_template,),
            ).fetchone():
                return character_template
    except (OSError, sqlite3.Error, ValueError) as exc:
        _log(
            f"Mortis mirror client template lookup failed for "
            f"{attack_template!r}: {type(exc).__name__}: {exc}"
        )
    return attack_template


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
def _monster_death_despawn_timing(
        static_database_path: str, template_id: str):
    """Return the authored death animation duration for a monster."""

    try:
        with sqlite3.connect(
            f"file:{Path(static_database_path).as_posix()}?mode=ro",
            uri=True,
        ) as database:
            row = database.execute(
                "SELECT DeathDuration FROM _Template_Monster "
                "WHERE Id=? LIMIT 1",
                (str(template_id),),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    if row is None:
        return None
    death_duration = max(0.0, float(row[0] or 0.0))
    if death_duration <= 0.0:
        return None
    return death_duration


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


@lru_cache(maxsize=16)
def _darkwood_wendigo_hazards(static_database_path: str, map_id: str):
    """Load the authored Wendigo challenge damage fields in Darkwood."""

    if map_key(map_id) != "e_hal_01_darkwood_dun":
        return ()
    path = map_database_path(static_database_path, map_id)
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
            rows = database.execute(
                "SELECT rowid, COALESCE(EventSetRow, -1), Transform, Id, "
                "StatusEffectId FROM _Instance_LocationEffect "
                "WHERE Id='e_hal_01_wood_challenge_damage' "
                "AND StatusEffectId='e_hal_01_wood_challenge_damage_aura'"
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        _log(f"Wendigo hazard load failed on {map_id}: {exc}")
        return ()
    result = []
    for row_id, event_set, transform, hazard_id, effect_id in rows:
        try:
            matrix = tuple(struct.unpack("<16f", bytes(transform)))
        except (TypeError, struct.error):
            continue
        result.append((
            TriggerVolumeDefinition(
                int(row_id), str(hazard_id), int(event_set), False,
                "halloween-wendigo-hazard", matrix,
            ),
            str(effect_id),
        ))
    return tuple(result)


@lru_cache(maxsize=8)
def _new_year_trap_hazards(static_database_path: str, map_id: str):
    """Load Rocketmen's authored player-damage trap boxes.

    This map has the LocationEffect volumes and lever event sets, but no
    _Instance_Trap records. Treat each player aura box as the missing server
    trigger volume, activated only while its lever's EventSet is enabled.
    """

    if map_key(map_id) != "e_nye_dungeon_01":
        return ()
    path = map_database_path(static_database_path, map_id)
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
            rows = database.execute(
                "SELECT rowid, COALESCE(EventSetRow, -1), Transform, Id "
                "FROM _Instance_LocationEffect "
                "WHERE lower(Id) LIKE 'e_nye_trap_%_player_aura'"
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        _log(f"Rocketmen trap load failed on {map_id}: {exc}")
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
                "rocketmen-trap", matrix,
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
# The Darkwood Wendigo encounter exposes all three ritual circles together.
# Their shared authored set is retired only after every distinct circle has
# consumed its 10-key cost, then the final boss event is started.
_DARKWOOD_WENDIGO_MAP = "e_hal_01_darkwood_dun"
_DARKWOOD_WENDIGO_RITUAL_EVENT = "e_hal_01_darkwood_ghost_fire_01_spawn"
_DARKWOOD_WENDIGO_RITUAL_EVENT_SET = 1248
_DARKWOOD_WENDIGO_FIRE_TEMPLATE = "e_hal_wendigo_fire_01"
_DARKWOOD_WENDIGO_BOSS_SIGNAL = (
    "e_hal_01_darkwood_ghost_fire_03_end_signal_01"
)
_DARKWOOD_WENDIGO_QUEST_TRIGGERS = (
    "halchexpl01", "halchexpl02", "halchexpl03",
)
_SARGON_THRONE_ROOM_MAP = SARGON_THRONE_ROOM_MAP_ID
_SARGON_MAIN_QUEST = "pw210-way-to-sargon-01-n"
_SARGON_SECOND_PHASE_MONSTER_TEMPLATES = frozenset({
    "boss_sargon_big",
    "boss_sargon_throne",
    "boss_sargon_soul_form",
})
_SARGON_SECOND_PHASE_START_SIGNAL = (
    "boss_sargon_second_phase_start_signal_01"
)
_SARGON_EVENT_ONLY_SECOND_PHASE_START_SIGNAL = (
    "boss_sargon_second_phase_start_signal_02"
)
# Statue turrets this close to a defeated boss guard its arena: Big Paws'
# two turrets stand 12 m from him, the nearest corridor statues over 60 m.
ARENA_HAZARD_RADIUS = 25.0
_FORCE_ANNOUNCE_EVENT_ACTORS = frozenset({
    "a0001_champion_undead_mage_01",
    "a0005_champion_grimmag_01_q_waytogrimmagstone_03",
    "e_easter_01_boss_egg_easter",
    "f4203_boss_06_ram",
    # Fyeborough's story sentinel is gated by a quest event and can already
    # exist in the encounter before that event releases it. Force its native
    # NewMonster registration when the EventSet opens so the client does not
    # show only the authored spawn effect while omitting the actor model.
    "pw207_sentinel_shadow_father_01_q_way_to_village_02",
    # The Lythira spawn transform is the boss's authored arena position and
    # can be outside the ordinary vicinity radius from the entry point.
    "fp_boss_01_lythira",
    # The Shadowfort Sentinel has a native skull minimap icon. Announce it
    # when its event set spawns it so every player sees the marker immediately.
    "pw210_sentinel_shadow_father_01",
})
_TUTORIAL_CULTIST_TEMPLATE = "a0001_champion_undead_mage_01"


def _is_easter_dungeon_festival_egg(definition) -> bool:
    """Whether an authored dungeon prop is one of Easter's clickable eggs."""

    template_id = str(getattr(definition, "template_id", "")).casefold()
    return (
        template_id.startswith("e_easter_01_egg_")
        or template_id.startswith("e_easter_01_dun_egg_")
    )


def _is_kingshill_spring_egg(definition) -> bool:
    """Whether an authored prop is one of Kingshill's respawning eggs."""

    template_id = str(getattr(definition, "template_id", "")).casefold()
    return (
        template_id.startswith("e_easter_01_hub_egg_")
        or template_id == "e_easter_2025_spring_egg"
    )


def _a_mighty_threat_goblin_template(map_id: object) -> str:
    """Fallback treasure-monster template when no map row identifies one."""

    map_key_value = str(map_id or "").casefold()
    if map_key_value.startswith("c"):
        return "c0000_helios_dungeons_treasure_monster_01"
    return "a0000_duria_dungeons_treasure_monster_01"


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
    "pw010_wildcave_01_dun": (
        "pw010_01_leader_stag_heroic", "pw010_spawn_exit",
    ),
    "pw011_eternal_grove_01_dun": (
        "pw011_01_leader_boar_heroic_02", "pw011_spawn_exit",
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

# Exit events that stay closed for the current run and must not be restored
# from a previously discovered destination. Kingscrypt is included here even
# though its boss entrance is opened by the authored key-and-lock quest, not
# by killing a map gatekeeper.
_PARALLEL_WORLD_EXIT_EVENT_KEYS = {
    map_id: event_key
    for map_id, (_boss_template, event_key)
    in _PARALLEL_WORLD_EXIT_BOSSES.items()
}
_PARALLEL_WORLD_EXIT_EVENT_KEYS[
    "pw008_kingscrypt_01_dun"
] = "pw008_spawn_exit"

# Several map exit tables contain one scene exit per selected tier. Kingscrypt
# still needs this filtering even though its exit is quest-prop-gated.
_PARALLEL_WORLD_TIERED_EXIT_MAPS = frozenset(_PARALLEL_WORLD_EXIT_EVENT_KEYS)
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

_STELLAR_GOLD_FINAL_BOSSES = frozenset({
    "e_stego_boss_destructor_gold",
    "e_stego_boss_herald_gold",
    "e_stego_boss_nefertari_gold",
    "e_stego_boss_sigrismarr_gold",
})
_BLOOD_CHEST_GEM_TEMPLATES = (
    "gem_amethyst_e", "gem_cyanit_e", "gem_diamond_e",
    "gem_onyx_e", "gem_ruby_e",
)
_BLOOD_CHEST_LOOT_LIST = "chest_lootlist_bloodshed"

# The DTU superchest tiers use authored, fixed item lists selected below by
# the static chest instance suffix (01-04). Wooden shares the bronze rewards.
_DTU_SUPERCHEST_POOLS = {
    "chest_lootlist_e_bos_superchest_wood": "bronze",
    "chest_lootlist_e_bos_superchest_copper": "bronze",
    "chest_lootlist_e_bos_superchest_silver": "silver",
    "chest_lootlist_e_bos_superchest_gold": "gold",
}
_DTU_CLASS_UNIQUES = {
    "warrior": (
        "warrior_unique_amulet_undefeatables_set",
        "warrior_unique_weaponmodifier_undefeatables",
    ),
    "mage": (
        "mage_unique_amulet_undefeatables_set",
        "mage_unique_weaponmodifier_undefeatables",
    ),
    "ranger": (
        "ranger_unique_amulet_undefeatables_set",
        "ranger_unique_weaponmodifier_undefeatables",
    ),
    "dwarf": (
        "dwarf_unique_amulet_undefeatables_set",
        "dwarf_unique_weaponmodifier_undefeatables",
    ),
}
_DTU_CLASS_ADORNMENTS = {
    class_name: f"{class_name}_unique_weaponmodifier_undefeatables"
    for class_name in ("warrior", "mage", "ranger", "dwarf")
}
_DTU_CLASS_GUARD_CLOAKS = {
    class_name: f"{class_name}_base_backitem_defense_crafted"
    for class_name in ("warrior", "mage", "ranger", "dwarf")
}
_DTU_CLASS_STRENGTH_CLOAKS = {
    class_name: f"{class_name}_base_backitem_offense_crafted"
    for class_name in ("warrior", "mage", "ranger", "dwarf")
}
_DTU_CLASS_UNDEFEATABLE_CLOAKS = {
    class_name: f"{class_name}_unique_backitem_e_bos"
    for class_name in ("warrior", "mage", "ranger", "dwarf")
}
_DTU_CLASS_PANDORA_AMULETS = {
    class_name: f"{class_name}_unique_amulet_undefeatables_set"
    for class_name in ("warrior", "mage", "ranger", "dwarf")
}
_DTU_CLASS_SILVER_GEAR = {
    class_name: (
        f"{class_name}_basic_gloves_02",
        f"{class_name}_basic_helmet_02",
        f"{class_name}_basic_shoulders_02",
        "all_basic_amulet_02",
    )
    for class_name in ("warrior", "mage", "ranger", "dwarf")
}
_DTU_CLASS_HEROIC_GEAR = {
    class_name: _DTU_CLASS_SILVER_GEAR[class_name]
    for class_name in ("warrior", "mage", "ranger", "dwarf")
}
_DTU_CLASS_SET_WEAPONMODS = {
    class_name: f"{class_name}_unique_weaponmodifier_undefeatables_set"
    for class_name in ("warrior", "mage", "ranger", "dwarf")
}
_DTU_CLOAK_INGREDIENTS = tuple(
    f"boss_ingredient_dtu_rarity_{rarity:02d}"
    for rarity in range(1, 6)
)
_DTU_PETS = (
    "e_bos_01_pet_heredur",
    "e_bos_01_pet_heredur_uncommon",
    "e_bos_01_pet_heredur_rare",
    "e_bos_01_pet_heredur_epic",
    "e_bos_02_pet_bearach",
    "e_bos_02_pet_medusa",
)

# Parallel World reward chests only drop the unupgraded (Common) jewels that
# belong to this mode. The two Flower Jewels are class-specific, while the
# other four are shared across every class.
_PARALLEL_WORLD_CLASS_JEWELS = {
    "warrior": "jewel_unique_damage_AreaDamageLightningOnHit_common_warrior",
    "ranger": "jewel_unique_damage_AreaDamageLightningOnHit_common_ranger",
    "mage": "jewel_unique_damage_AreaDamageFireOnHit_common_mage",
    "dwarf": "jewel_unique_damage_AreaDamageFireOnHit_common_dwarf",
}
_PARALLEL_WORLD_SHARED_JEWELS = (
    "jewel_unique_damage_HealthGlobeDamageBuff_common",  # Amplified Healing
    "jewel_skill_cooldown_common",                       # Focus
    "jewel_buff_critical_attackspeed_onHitCritical_common",  # Rage
    "jewel_resource_cost_common",                        # Relentlessness
)


def _parallel_world_reward_jewel_candidates(class_name: str) -> tuple[str, ...]:
    """The Common jewel pool for one Parallel World class."""

    class_jewel = _PARALLEL_WORLD_CLASS_JEWELS.get(
        str(class_name or "").casefold()
    )
    return ((class_jewel,) if class_jewel else ()) + _PARALLEL_WORLD_SHARED_JEWELS


def _log(message: str) -> None:
    print(f"{MAP_EVENT_LOG_PREFIX} {message}")


_NEW_YEAR_MACHINE_BABIES = frozenset(
    f"e_nye_normal_dun_spawner_bomb_var_{index:02d}"
    for index in range(1, 5)
)


def _is_new_year_machine_baby(template_id: object) -> bool:
    return str(template_id or "").casefold() in _NEW_YEAR_MACHINE_BABIES


def _is_new_year_undestroyable_tower(map_id: object, template_id: object) -> bool:
    return (
        map_key(map_id) == "e_nye_dungeon_01"
        and str(template_id or "").casefold()
        == "h0009_endlesstower_01_room_fire_shooter"
    )


_NEW_YEAR_MACHINE_DELAYS = {
    1: (30, 30, 30, 30, 30),
    2: (210, 150, 120, 90, 35),
    3: (390, 330, 300, 270, 40),
    4: (580, 520, 490, 460, 45),
}


def _new_year_machine_delay(runtime, definition, sessions) -> float:
    key = str(getattr(definition, "key", "") or "").casefold()
    prefix = "e_nye_activate_spawner_bomb_var_"
    if not key.startswith(prefix) or "_message" in key or "_remove_" in key:
        return float(definition.start_delay)
    suffix = key[len(prefix):]
    if len(suffix) < 2 or not suffix[:2].isdigit():
        return float(definition.start_delay)
    machine = int(suffix[:2])
    if machine not in _NEW_YEAR_MACHINE_DELAYS:
        return float(definition.start_delay)
    players = min(5, max(1, len(sessions)))
    expected = (
        f"{prefix}{machine:02d}"
        if players == 5 or machine == 1
        else f"{prefix}{machine:02d}_delay_{players}_player"
    )
    return float(
        _NEW_YEAR_MACHINE_DELAYS[machine][players - 1]
        if key == expected else definition.start_delay
    )


def _next_new_year_machine_event(runtime, machine: int, players: int):
    if machine not in _NEW_YEAR_MACHINE_DELAYS:
        return None
    base = f"e_nye_activate_spawner_bomb_var_{machine:02d}"
    event_key = (
        base if players >= 5
        else f"{base}_delay_{max(1, players)}_player"
    )
    return runtime.catalog.event(event_key)


def _new_year_machine_event_stage(event_key: object) -> int:
    key = str(event_key or "").casefold()
    prefix = "e_nye_activate_spawner_bomb_var_"
    if not key.startswith(prefix) or "_message" in key or "_remove_" in key:
        return 0
    suffix = key[len(prefix):]
    return int(suffix[:2]) if len(suffix) >= 2 and suffix[:2].isdigit() else 0


def _new_year_machine_spawn_row(runtime, machine: int):
    if machine not in _NEW_YEAR_MACHINE_DELAYS:
        return None
    template = f"e_nye_normal_dun_spawner_bomb_var_{machine:02d}"
    row = next((
        candidate for candidate in runtime.content.spawn_rows
        if str(candidate.template_id).casefold() == template
    ), None)
    if row is None:
        return None
    return next(iter(runtime.gated_spawn_rows((row.event_set_row,))), None)


def _is_darkwood_wendigo_wave_start_signal(signal_id: object) -> bool:
    """Whether a signal starts one of the movement-triggered Wendigo waves."""

    key = str(signal_id or "").casefold()
    for stage in (1, 2, 3):
        for branch in ("", "b", "c"):
            prefix = (
                f"e_hal_01_darkwood_ghost_fire_{stage:02d}{branch}"
                "_start_signal_"
            )
            if key.startswith(prefix) and key[len(prefix):] in {
                "01", "02", "03", "04", "05", "client",
            }:
                return True
    return False


def _darkwood_wendigo_ritual_prop(runtime, definition):
    """Classify one of the three simultaneously available ritual circles."""

    if (
        map_key(runtime.map_id) != _DARKWOOD_WENDIGO_MAP
        or str(getattr(definition, "template_id", "") or "").casefold()
        != _DARKWOOD_WENDIGO_FIRE_TEMPLATE
    ):
        return None
    if (
        int(getattr(definition, "event_set_row", -1))
        != _DARKWOOD_WENDIGO_RITUAL_EVENT_SET
        or str(getattr(definition, "key_id", "") or "").casefold()
        != "e_hal_01_challenge_wendigo_fire_key"
        or int(getattr(definition, "key_amount", 0) or 0) != 10
    ):
        return False
    return True


def _darkwood_wendigo_factory_prop(runtime, definition) -> bool:
    """Whether the ritual's scene prop needs a factory-created wire entity.

    The Darkwood map already owns these scene objects. Binding PropInfo to the
    authored GUID can hit the client's duplicate-PropId assertion; PropUpdate
    alone does not register an interactive GameProp. Publish a zero-GUID prop
    at the same transform instead, while EventUpdate continues to control the
    authored scene visuals.
    """

    return bool(
        map_key(runtime.map_id) == _DARKWOOD_WENDIGO_MAP
        and int(getattr(definition, "event_set_row", -1))
        == _DARKWOOD_WENDIGO_RITUAL_EVENT_SET
        and str(getattr(definition, "template_id", "") or "").casefold()
        == _DARKWOOD_WENDIGO_FIRE_TEMPLATE
        and str(getattr(definition, "kind", "") or "").casefold() == "scene"
    )


def _darkwood_wendigo_ritual_prop_ids(runtime) -> frozenset[int]:
    """Return this map instance's selected three ritual-circle prop ids."""

    return frozenset(
        int(definition.prop_id)
        for definition in runtime.select_props(
            runtime.content.props_for_sets(
                (_DARKWOOD_WENDIGO_RITUAL_EVENT_SET,),
            )
        )
        if (
            int(definition.event_set_row)
            == _DARKWOOD_WENDIGO_RITUAL_EVENT_SET
            and str(definition.template_id or "").casefold()
            == _DARKWOOD_WENDIGO_FIRE_TEMPLATE
        )
    )


@lru_cache(maxsize=4)
def _exit_access_difficulties(database_path: str) -> dict[str, int]:
    path = resident_content.resolved(database_path).as_uri()
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


def _send_prop_info(
    viewer: Session, states, *, label: str,
    filter_map_variations: bool = True,
) -> None:
    """Retail creates and refreshes mid-map props with PropInfo, never NewProp.

    Quest-controlled props are filtered per viewer (services/quests.py), and
    map-variation props are limited to the viewer's difficulty tier so
    overlapping authoring variants do not stack in one scene.
    """

    states = tuple(states)
    if filter_map_variations:
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
    summer_claim_key = _summer_solstice_reward_chest_claim_key(
        viewer, definition,
    )
    if summer_claim_key:
        with viewer.flow_lock:
            claimed = bool(
                getattr(viewer, "gameplay_timed_offer_purchases", {}).get(
                    summer_claim_key, 0,
                )
            )
        if claimed:
            return PropRuntimeState(definition, PROP_STATE_USED)
    dtu_claim_key = _dtu_superchest_claim_key(viewer, definition)
    if dtu_claim_key:
        with viewer.flow_lock:
            claimed = bool(
                getattr(viewer, "gameplay_timed_offer_purchases", {}).get(
                    dtu_claim_key, 0,
                )
            )
        if claimed:
            return PropRuntimeState(definition, PROP_STATE_USED)
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


def _dtu_superchest_claim_key(viewer: Session, definition) -> str:
    """Stable per-event, per-arena-location claim key for Circus chests."""

    if (
        map_key(getattr(
            getattr(viewer, "gameplay_map_event_runtime", None),
            "map_id", "",
        )) != "e_bos_arena_01"
        or str(definition.loot_list_id or "").casefold() not in {
            "chest_lootlist_e_bos_superchest_wood",
            "chest_lootlist_e_bos_superchest_copper",
            "chest_lootlist_e_bos_superchest_silver",
            "chest_lootlist_e_bos_superchest_gold",
        }
    ):
        return ""
    instance_id = str(definition.instance_id or "").casefold()
    location = instance_id.rsplit("_", 1)[-1]
    if location not in {"01", "02", "03", "04"}:
        return ""
    catalog = getattr(viewer, "gameplay_event_catalog", None)
    if catalog is None:
        return ""
    event = next((
        value for value in catalog.active_events()
        if value.template.resource_id.casefold() == "boss_bonus_event_02"
    ), None)
    if event is None:
        return ""
    return f"dtu-superchest:{int(event.event_id)}:{location}"


def _summer_solstice_reward_chest_claim_key(viewer: Session, definition) -> str:
    """Persist Kalodoro reward-chest usage across map and server sessions."""

    if map_key(getattr(
        getattr(viewer, "gameplay_map_event_runtime", None), "map_id", "",
    )) != "e_sum_dun_02":
        return ""
    instance_id = str(definition.instance_id or "").casefold()
    if instance_id not in {
        "e_sum_01_reward_pet_chest",
        "e_sum_01_reward_mount_chest",
        "e_sum_01_reward_jewel_chest",
        "e_sum_01_reward_gem_chest",
        "e_sum_01_reward_rune_chest",
    }:
        return ""
    return f"summer-solstice-reward-chest:{instance_id}"


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

    def _schedule_monster_death_despawn(
            self, s: Session, combat, dispatches, viewers) -> None:
        """Remove map monsters when their authored death clips finish."""

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
            timing = _monster_death_despawn_timing(
                str(combat.encounter.static_database_path), template_id,
            )
            if timing is None:
                continue
            death_duration = timing
            interested = tuple(
                viewer
                for viewer in visible_viewers
                if actor_id in viewer.gameplay_spawned_monster_actor_ids
            )
            if not interested:
                continue
            with runtime.lock:
                timers = getattr(
                    runtime, "monster_despawn_timers", None,
                )
                if timers is None:
                    timers = {}
                    runtime.monster_despawn_timers = timers
                if actor_id in timers:
                    continue
                timer = scheduler.timer(
                    max(0.05, death_duration),
                    self._begin_monster_despawn,
                    args=(
                        runtime, combat, actor_id,
                        tuple(float(value) for value in resolution.position),
                        interested,
                    ),
                )
                timers[actor_id] = timer
            timer.start()

    def _begin_monster_despawn(
            self, runtime, combat, actor_id, position, viewers) -> None:
        """Finish the dead monster lifecycle after its death clip."""

        if not self._map_event_runtime_is_live(runtime):
            return
        with runtime.lock:
            timers = getattr(runtime, "monster_despawn_timers", {})
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
        # DeathDuration already covers the death clip. A second Kill does
        # not select DespawnSequence: its animation_time is another corpse
        # lifetime, which leaves the finished death model visible longer.
        self._finish_monster_despawn(
            runtime, combat, int(actor_id), viewers,
        )

    def _finish_monster_despawn(
            self, runtime, combat, actor_id, viewers) -> None:
        """Remove actors after the authored monster death animation."""

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
                    label="monster-death-discard",
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
            CharacterService._ranking_record_fractured_planes_floor(
                self, viewer, floor, runtime.fractured_planes_limit_deadline)
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
            monster_level, scaling_difficulty: int, map_difficulty: int = 0,
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
        if map_key(map_id) == "e_sum_minidun_chestroom_01":
            # Chest-game flags belong to this minigame run. Some entry
            # snapshots carry them as active and expose all reward chests
            # before the player makes a selection.
            flags = frozenset(
                flag for flag in flags
                if not flag.startswith("e_sum_dun_01_chest_game_")
            )
        gate = _PARALLEL_WORLD_EXIT_BOSSES.get(map_key(map_id))
        if gate is not None:
            flags = flags - {gate[1]}

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
                    map_difficulty=int(map_difficulty),
                )
                runtime.scheduler = s.gameplay_deadline_scheduler
                registry[key] = runtime
            if map_key(map_id) == "e_sum_minidun_chestroom_01":
                # EventSet 1748 is authored as an OR over the ending flag and
                # every reward flag, so starting the ending event exposes all
                # 31 chest groups. Keep the aggregate row disabled; selected
                # per-chest EventSets remain independently usable.
                runtime.suppress_event_sets((1748,))
        if map_key(map_id) == "e_nye_dungeon_01":
            # The authored trap levers all sit behind EventSet 1546, which is
            # normally unavailable until the encounter is ending. Keep the
            # twelve level-selected lever props usable and map-visible from
            # entry; their own interaction signals still control trap state.
            with runtime.lock:
                lever_definitions = runtime.select_props(
                    runtime.content.props_for_sets((1546,))
                )
                for definition in lever_definitions:
                    if not str(definition.instance_id).casefold().startswith(
                        "e_nye_trigger_object_bomb_trap_"
                    ):
                        continue
                    previous = runtime.props.get(definition.prop_id)
                    visible_definition = replace(
                        definition,
                        event_set_row=-1,
                        instance_guid=ZERO_GUID,
                    )
                    runtime.props[definition.prop_id] = PropRuntimeState(
                        visible_definition,
                        previous.state if previous is not None else 0,
                    )
            if fresh_instance:
                first_machine = runtime.catalog.event(
                    "e_nye_activate_spawner_bomb_var_01",
                )
                if first_machine is not None:
                    # The boss-start trigger is deeper in the map. The first
                    # machine's 30-second clock begins as soon as the map
                    # instance is created, so it cannot be killed early.
                    self._schedule_map_local_event_start(
                        runtime, first_machine,
                    )
                else:
                    _log("first Machine Baby activation event is missing")
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
            # Register all three Darkwood circles as factory props. The two
            # authored scene GUIDs can hit the client's duplicate-PropId
            # assertion, while PropUpdate alone does not register an
            # interactive GameProp.
            for prop_id, state in tuple(states_by_id.items()):
                if not _darkwood_wendigo_factory_prop(
                    runtime, state.definition,
                ):
                    continue
                states_by_id[prop_id] = PropRuntimeState(
                    replace(state.definition, instance_guid=ZERO_GUID),
                    state.state,
                )
            if (
                map_key(runtime.map_id) == "b1205_crafting_dun"
                and "server_b1205_crafting_leader_present"
                not in runtime.initial_flags
            ):
                # These two harvesting nodes failed to materialize on some
                # client map builds when PropInfo referenced the scene GUID.
                # A zero GUID makes the client instantiate the authored prop
                # template at the supplied transform, while preserving the
                # same server prop ids used by PropInteraction.
                for prop_id, state in tuple(states_by_id.items()):
                    if str(state.definition.instance_id).casefold() not in {
                        "ingredient_tree_01", "ingredient_geode_01",
                    }:
                        continue
                    states_by_id[prop_id] = PropRuntimeState(
                        replace(state.definition, instance_guid=ZERO_GUID),
                        state.state,
                    )
            # Event-controlled Easter eggs can be hidden by the client's
            # scene EventSet despite being published by the server. Factory-
            # create active egg props so they remain visible and clickable.
            for prop_id, state in tuple(states_by_id.items()):
                if _is_easter_dungeon_festival_egg(state.definition):
                    states_by_id[prop_id] = PropRuntimeState(
                        replace(state.definition, instance_guid=ZERO_GUID),
                        state.state,
                    )
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

    def _spawn_easter_secret_lair_daran(self, s, runtime) -> None:
        """Spawn Easter's Daran once when the Secret Lair trigger is entered."""

        template_id = "e_easter_01_dun_boss_troll_01"
        fired_key = "easter_secret_lair_daran_spawned"
        with runtime.lock:
            if fired_key in runtime.fired_once:
                return
            trigger = next((
                volume for volume in runtime.content.trigger_volumes
                if (
                    str(volume.triggered_event_id).casefold()
                    == "secretlaireaster"
                    or str(volume.trigger_id).casefold()
                    == "secretlaireaster"
                )
            ), None)
            runtime.fired_once.add(fired_key)
        if trigger is not None:
            matrix = trigger.matrix
            position = (
                float(matrix[12]) + 4.0,
                float(matrix[13]),
                float(matrix[14]),
            )
        else:
            with s.flow_lock:
                frame = getattr(s, "gameplay_coordinate_frame", None)
                last_move = getattr(s, "gameplay_last_move", None)
            if frame is None or last_move is None:
                with runtime.lock:
                    runtime.fired_once.discard(fired_key)
                return
            origin = frame.wire_to_world(last_move.relative_position)
            position = (float(origin[0]) + 4.0, float(origin[1]), float(origin[2]))
        sessions = self._map_event_sessions(runtime)
        if s not in sessions:
            sessions = (*sessions, s)
        self._spawn_event_monsters(
            runtime, sessions, (),
            extra_rows=(GatedSpawnRow(
                -1_100_001, -1, template_id, position,
            ),),
        )
        _log(
            f"spawned Easter Daran in Secret Lair on {runtime.map_id} "
            f"at {tuple(round(value, 2) for value in position)}"
        )

    @staticmethod
    def _sargon_shadowfort_leader_waves_cleared(s, runtime) -> bool:
        """Return true after all three selected altar leader groups are dead."""

        selected_events = frozenset(
            runtime.sargon_shadowfort_selected_altar_event_keys
        )
        if len(selected_events) != 3:
            return False
        with runtime.lock:
            if not selected_events.issubset(runtime.active_flags()):
                return False
            leader_spawn_rows_by_event = {
                event_key: tuple(
                    row
                    for row in runtime.content.spawn_rows_for_sets(
                        event_set.row
                        for event_set in runtime.content.event_sets_for(
                            event_key
                        )
                    )
                    if row.template_id.casefold().startswith("pw210_leader_")
                )
                for event_key in selected_events
            }
            actor_ids_by_row = {
                int(row): set(actor_ids)
                for row, actor_ids in runtime.event_set_actor_ids.items()
            }
        if any(not rows for rows in leader_spawn_rows_by_event.values()):
            return False
        combat = getattr(s, "gameplay_combat_state", None)
        if combat is None:
            return False
        with combat.lock:
            alive = set(combat.alive_actor_ids)
            monsters = combat.monsters
            for rows in leader_spawn_rows_by_event.values():
                for row in rows:
                    actor_ids = actor_ids_by_row.get(
                        int(row.event_set_row), set(),
                    )
                    leader_actor_ids = {
                        actor_id for actor_id in actor_ids
                        if (
                            (monster := monsters.get(actor_id)) is not None
                            and monster.template_id.casefold()
                            == row.template_id.casefold()
                        )
                    }
                    if not leader_actor_ids or leader_actor_ids.intersection(alive):
                        return False
        return True

    def _fire_map_signal(
            self, s: Session, event_type: str, params, *,
            synthetic_dead_templates=(),
    ) -> None:
        """Evaluate every server signal rule of ``event_type`` for ``s``."""

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            return
        self._flush_due_map_event_starts(runtime)
        kind = str(event_type).casefold()
        if (
            kind == "playerhittrigger"
            and map_key(runtime.map_id) == "i0010_physical_big_dun"
            and any(str(value).casefold() == "secretlaireaster" for value in params)
        ):
            event_catalog = getattr(s, "gameplay_event_catalog", None)
            if event_catalog is None:
                try:
                    event_catalog = CharacterService._event_catalog(self)
                except (OSError, ValueError, sqlite3.Error):
                    event_catalog = None
            if (
                event_catalog is not None
                and "easter_event_01" in event_catalog.active_template_ids()
            ):
                self._spawn_easter_secret_lair_daran(s, runtime)
        if kind == "playerkilledmonster":
            params = tuple(str(value).casefold() for value in params)
            bgh_single = _BGH_SINGLE_BOSS_PORTALS.get(
                map_key(runtime.map_id)
            )
            if bgh_single is not None and bgh_single[0] in params:
                # The server materializes the single boss directly, while the
                # retail close-map rule is authored against its portal actor.
                params = (*params, bgh_single[1])
        if kind == "playerenteredmap":
            mortis = mortis_arena(self, runtime)
            if mortis is not None:
                mortis.resume()
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
                present_templates = frozenset(present) | frozenset(
                    str(value).casefold()
                    for value in synthetic_dead_templates
                )
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
            # Ashen Plains' Varnok exit is authored behind a client-only
            # trigger gated by ce2025-main-quest-11. The optional "World of
            # Varnok" chain can independently direct the player to that boss.
            # Make the authored local event authoritative whenever either
            # boss objective is active, including map re-entry and acceptance
            # while already in Ashen Plains.
            if map_key(runtime.map_id) == "ce202504":
                quest_log = getattr(s, "gameplay_quest_log", {}) or {}
                boss_quest_active = any(
                    str(quest_id).casefold() in {
                        "ce2025-main-quest-12",
                        "ce2025-side-quest-16",
                    }
                    and progress.state == QUEST_STATE_ACCEPTED
                    for quest_id, progress in quest_log.items()
                )
                if boss_quest_active:
                    varnok_event = runtime.catalog.event(
                        "ce_2025_varnok_tower_open_client",
                    )
                    if (
                        varnok_event is not None
                        and varnok_event.applies_to(runtime.map_id)
                    ):
                        with runtime.lock:
                            already_active = varnok_event.key in runtime.active_flags()
                        if not already_active:
                            self._start_map_local_event(runtime, varnok_event)
                        _log(
                            "ensured Varnok tower entrance for active boss quest "
                            f"on {runtime.map_id}"
                        )
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
        if kind == "playerkilledmonster":
            self._reveal_a_mighty_threat_chest_on_kill(
                s, runtime, context.params,
            )
        rules = runtime.catalog.evaluate(kind, context)
        if map_key(runtime.map_id) == SARGON_SHADOWFORT_MAP_ID:
            sentinel_rules = tuple(
                rule for rule in rules
                if rule.signal_id.casefold()
                == SARGON_SHADOWFORT_SENTINEL_SIGNAL
            )
            if sentinel_rules:
                if self._sargon_shadowfort_leader_waves_cleared(s, runtime):
                    with runtime.lock:
                        runtime.sargon_shadowfort_sentinel_unlocked = True
                else:
                    _log(
                        "blocked Sargon Shadowfort Sentinel signal before all "
                        "three selected altar leader waves were defeated"
                    )
                    rules = tuple(
                        rule for rule in rules
                        if rule.signal_id.casefold()
                        != SARGON_SHADOWFORT_SENTINEL_SIGNAL
                    )
        if map_key(runtime.map_id) == _DARKWOOD_WENDIGO_MAP:
            blocked_wendigo_rules = tuple(
                rule for rule in rules
                if _is_darkwood_wendigo_wave_start_signal(rule.signal_id)
            )
            if blocked_wendigo_rules:
                _log(
                    "ignored ungated Wendigo wave start(s) on "
                    f"{runtime.map_id}: "
                    f"{tuple(rule.signal_id for rule in blocked_wendigo_rules)!r}; "
                    "the three ritual circles own boss activation"
                )
                rules = tuple(
                    rule for rule in rules
                    if not _is_darkwood_wendigo_wave_start_signal(
                        rule.signal_id
                    )
                )
        if (
            kind == "playerkilledmonster"
            and map_key(runtime.map_id) in {
                "e_bgh_boss_all", "e_bgh_boss_guardian_all",
            }
            and any(
                str(template).startswith("e_nightmare_portal")
                for template in context.params
            )
        ):
            # In the combined and four-guardian arenas the portal opens the
            # boss sequence; its static CloseSignal must not terminate the run
            # at the first kill.
            rules = tuple(
                rule for rule in rules
                if rule.signal_id.casefold() not in {
                    "close_map_e_bgh_boss_all",
                    "close_map_e_bgh_boss_guardian_all",
                }
            )
        if (
            map_key(runtime.map_id) == "e_bos_arena_01"
            and kind == "playerkilledmonster"
        ):
            # These seven level-entry signals all match the same final arena
            # boss. At high character levels they all fire on one kill and
            # expose every authored gate. Circus uses its four-boss random
            # gate progression below instead.
            rules = tuple(
                rule for rule in rules
                if not rule.signal_id.casefold().startswith(
                    "e_bos_02_boss_entry_start_lv"
                )
            )
        if map_key(runtime.map_id) == "pw214_mortis_boss":
            # The legacy selector names obsolete starter variants and can
            # clear after only the archers die. Count the actual wave actors.
            rules = tuple(rule for rule in rules if rule.signal_id.casefold()
                          != "boss_mortis_mounted_spawn_start_signal")
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
        tidal_entry_signal = "k0003_entry_loi_start_signal"
        if (
            kind == "playerenteredmap"
            and map_key(runtime.map_id) == "k0002_greylingforest"
            and "k0001-what-lies-within" in quests_accepted
            and not any(
                rule.signal_id.casefold() == tidal_entry_signal
                for rule in rules
            )
        ):
            # The authored Tidal Wildwood door only listens for the unrelated
            # An Old Curse quest. What Lies Within also sends players there;
            # let its accepted quest start the same entry-door event.
            door_rule = runtime.catalog.rule(tidal_entry_signal)
            if door_rule is not None:
                rules = (*rules, door_rule)
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

    def _reveal_a_mighty_threat_chest_on_kill(
            self, s: Session, runtime: MapEventRuntime,
            killed_templates: frozenset[str],
    ) -> None:
        """Publish the selected Sentinel/Goblin chest when its actor dies.

        Retail's map-local fast-travel event also reveals this prop, but the
        exported event signal is not consistent across all regional Sentinel
        and treasure-monster templates. Publish the authored chest directly
        from the authoritative kill callback as a reliable fallback.
        """

        if not runtime.content.a_mighty_threat_candidate_templates(
            runtime.catalog,
        ):
            return
        with runtime.lock:
            selected = runtime.scaling_dungeon_sentinel_spawn
            if (
                selected is None
                or str(selected[1]).casefold() not in killed_templates
                or runtime.a_mighty_threat_chest_revealed
            ):
                return
            chest_sets = frozenset(
                int(prop.event_set_row)
                for prop in runtime.content.props
                if "sentinel_lootchest_" in (
                    f"{prop.instance_id} {prop.template_id}"
                ).casefold()
                and int(prop.event_set_row) >= 0
            )
            if not chest_sets:
                return
        sessions = self._map_event_sessions(runtime)
        if s not in sessions:
            sessions = (*sessions, s)
        combat = self._map_event_combat_state(sessions)
        death_position = None
        if combat is not None:
            with combat.lock:
                dead_sentinels = tuple(
                    combat.monster_positions.get(int(actor_id), monster.position)
                    for actor_id, monster in combat.monsters.items()
                    if str(monster.template_id).casefold()
                    == str(selected[1]).casefold()
                    and int(combat.monster_health.get(actor_id, 0)) <= 0
                )
            if dead_sentinels:
                death_position = tuple(float(axis) for axis in dead_sentinels[0])
        with runtime.lock:
            if runtime.a_mighty_threat_chest_revealed:
                return
            runtime.a_mighty_threat_chest_revealed = True
            if death_position is not None:
                runtime.a_mighty_threat_position = death_position
        self._publish_event_props(runtime, sessions, chest_sets)
        _log(
            f"A Mighty Threat revealed its chest on {runtime.map_id} after "
            f"{selected[1]!r} died at "
            f"{death_position if death_position is not None else 'authored spawn'}; "
            f"event sets={tuple(sorted(chest_sets))}"
        )

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

    @staticmethod
    def _sargon_main_quest_completed(s: Session) -> bool:
        with s.flow_lock:
            return any(
                str(quest_id).casefold() == _SARGON_MAIN_QUEST
                and int(getattr(progress, "state", -1))
                == QUEST_STATE_CLOSED
                for quest_id, progress in (
                    getattr(s, "gameplay_quest_log", {}) or {}
                ).items()
            )

    def _emit_map_signal(self, s: Session, runtime: MapEventRuntime,
                         signal_id: str) -> None:
        catalog = runtime.catalog
        key = str(signal_id).casefold()
        if map_key(runtime.map_id) == SARGON_SHADOWFORT_MAP_ID:
            if key == SARGON_SHADOWFORT_SENTINEL_SIGNAL:
                with runtime.lock:
                    sentinel_unlocked = runtime.sargon_shadowfort_sentinel_unlocked
                if not sentinel_unlocked:
                    _log("blocked Sargon Shadowfort Sentinel spawn before altar clear")
                    return
            if key in {
                "pw210_sargon_open_exit_to_sargon_start_signal_01",
                "pw210_sargon_open_exit_to_sargon_event_only_start_signal_01",
            }:
                with runtime.lock:
                    sentinel_defeated = runtime.sargon_shadowfort_sentinel_defeated
                if not sentinel_defeated:
                    _log("blocked Sargon Shadowfort boss gate before Sentinel death")
                    return
        if map_key(runtime.map_id) == _SARGON_THRONE_ROOM_MAP:
            if key == _SARGON_EVENT_ONLY_SECOND_PHASE_START_SIGNAL:
                # The quest-incomplete branch's Soul Form is not part of this
                # two-phase encounter. Incomplete runs end after the first
                # Sargon; completed runs use the regular phase event below.
                _log(
                    "ignored Sargon's quest-incomplete Soul Form branch; "
                    "the second phase requires the main quest"
                )
                return
            if key == _SARGON_SECOND_PHASE_START_SIGNAL:
                with runtime.lock:
                    first_phase_defeated = (
                        runtime.sargon_throne_first_phase_defeated
                    )
                if not first_phase_defeated:
                    _log(
                        "blocked Sargon's reincarnation phase before "
                        "the first Sargon was defeated"
                    )
                    return
                if not self._sargon_main_quest_completed(s):
                    _log(
                        "ignored Sargon's reincarnation phase because "
                        "The Terrible is Back is not complete"
                    )
                    return
                with runtime.lock:
                    runtime.sargon_throne_second_phase_authorized = True
        if (
            map_key(runtime.map_id) == "e_nye_dungeon_01"
            and key in {
                f"e_nye_loot_trigger_interact_start_signal_{index:02d}"
                for index in range(1, 5)
            }
        ):
            crate_number = int(key.rsplit("_", 1)[-1])
            # Each unlocked Fireworks Crate can reveal its authored Liberated
            # Greed Fiend chest, whose EventSet and spawn point are already
            # tied to this interaction event in the map data. Start that event
            # directly after the chance roll so the chest EventSet/prop is
            # published authoritatively instead of depending on a second
            # signal-catalog lookup.
            if random.random() >= 0.45:
                _log(
                    "Professor Jullov Fireworks Crate did not reveal a "
                    f"Liberated Greed Fiend chest ({key})"
                )
                return
            chest_event = runtime.catalog.event(
                f"e_nye_loot_trigger_interact_{crate_number:02d}"
            )
            if chest_event is not None:
                self._start_map_local_event(runtime, chest_event)
                _log(
                    "Professor Jullov Fireworks Crate revealed a "
                    f"Liberated Greed Fiend chest ({key})"
                )
                return
            _log(
                "Liberated Greed Fiend chest event is missing for "
                f"Professor Jullov Fireworks Crate ({key})"
            )
        if (
            map_key(runtime.map_id) == "e_sum_minidun_chestroom_01"
            and (
                key == "e_sum_dun_01_chest_game_activate_chests_start_signal"
                or (
                    key.startswith(
                        "e_sum_dun_01_chest_game_activate_chest_"
                    ) and key.endswith("_start_signal")
                )
            )
        ):
            # Retail binds the brazier's picker_done signal to all 31 reward
            # events at once. The server banks only the selected unlock flags
            # in _bank_underground_chamber_chests instead.
            _log(
                "ignored bulk Underground Chamber reward signal "
                f"{key!r}; selected chest flags own the payout"
            )
            return
        if (
            map_key(runtime.map_id) == "pw001_grimmagstone_01_dun"
            and key in {
                "pw001_spawn_exit_signal",
                "pw001_spawn_exit_signal_b",
            }
        ):
            # Client-side kill/quest signals can otherwise activate the exit
            # event before Raazghul is defeated. The authoritative boss-kill
            # path starts it directly after validating the killed template.
            _log(f"ignored non-authoritative Grimmagstone exit signal {key!r}")
            return

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
                if map_key(runtime.map_id) == SARGON_SHADOWFORT_MAP_ID:
                    event_key_folded = str(event_key).casefold()
                    if (
                        event_key_folded.startswith(
                            SARGON_SHADOWFORT_ALTAR_EVENT_PREFIX
                        )
                        and event_key_folded
                        not in runtime.sargon_shadowfort_selected_altar_event_keys
                    ):
                        _log(
                            "ignored unselected Sargon Shadowfort altar event "
                            f"{event_key_folded!r}"
                        )
                        continue
                    if (
                        event_key_folded == SARGON_SHADOWFORT_SENTINEL_EVENT
                        and not runtime.sargon_shadowfort_sentinel_unlocked
                    ):
                        continue
                    if (
                        event_key_folded in SARGON_SHADOWFORT_EXIT_EVENT_KEYS
                        and not runtime.sargon_shadowfort_sentinel_defeated
                    ):
                        continue
                definition = catalog.event(event_key)
                if definition is not None and definition.applies_to(runtime.map_id):
                    self._schedule_map_local_event_start(runtime, definition)
        if (
            map_key(runtime.map_id) == "e_sum_minidun_arena_02"
            and key in {
                "e_sum_dun_01_arena_off_ending_start_signal",
                "e_sum_dun_01_arena_off_ending_start_signal_01",
            }
        ):
            # Meanie Rumble's authored chest activation signals all listen for
            # the death of this invisible timer actor. The final boss kill
            # already determines when the ending begins; mirror that missing
            # actor only after the real ending signal so chest unlocks remain
            # gated by completing the waves.
            self._stop_summer_solstice_waves(
                runtime, "e_sum_dun_01_arena_off_wave_",
            )
            self._fire_map_signal(
                s, "MonstersEliminated",
                ("e_sum_01_arena_off_invisible_timer",),
                synthetic_dead_templates=(
                    "e_sum_01_arena_off_invisible_timer",
                ),
            )
        # The retail minigame's invisible timer actors are intentionally not
        # instantiated as combatants by this server. They emit the final
        # signal after the one-minute round, which stops the timed waves and
        # activates the authored reward chest sets. Reproduce that clock here
        # for both Solstice arenas so the ending does not depend on a missing
        # invisible monster template.
        arena_start_signals = {
            "e_sum_minidun_arena_01": "e_sum_dun_01_arena_def_arena_start_signal",
        }
        map_id = map_key(runtime.map_id)
        if (
            map_id in arena_start_signals
            and key == arena_start_signals[map_id]
        ):
            scheduler = getattr(runtime, "scheduler", None)
            with runtime.lock:
                timer_key = f"summer_solstice_arena_end:{map_id}"
                timers = getattr(runtime, "summer_solstice_timers", None)
                if timers is None:
                    timers = set()
                    runtime.summer_solstice_timers = timers
                should_start_timer = timer_key not in timers
                if should_start_timer:
                    timers.add(timer_key)
            if should_start_timer and scheduler is not None:
                # The laser's native aura rotates independently of player
                # movement. Poll its authored beam geometry during the round
                # so a stationary player is hit when the blue beam sweeps
                # across them, rather than relying on a proximity aura or a
                # movement packet to notice contact.
                with runtime.lock:
                    runtime.summer_solstice_laser_active = True
                beam_timer = scheduler.timer(
                    TOWER_BEAM_CHECK_INTERVAL,
                    self._run_summer_solstice_laser_tick,
                    args=(runtime,),
                )
                beam_timer.start()
                timer = scheduler.timer(
                    60.0,
                    self._finish_summer_solstice_defense,
                    args=(runtime,),
                )
                timer.start()
        CharacterService._quest_on_signal(self, s, signal_id)

    def _run_summer_solstice_laser_tick(self, runtime) -> None:
        """Check the rotating Endurance laser against players every 50 ms."""

        with runtime.lock:
            active = bool(
                getattr(runtime, "summer_solstice_laser_active", False)
            )
            scheduler = getattr(runtime, "scheduler", None)
        if (
            not active
            or scheduler is None
            or not self._map_event_runtime_is_live(runtime)
            or map_key(runtime.map_id) != "e_sum_minidun_arena_01"
        ):
            return
        for viewer in self._map_event_sessions(runtime):
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
                MapEventService._apply_tower_aura_damage(
                    viewer, runtime, position,
                )
            except Exception as exc:
                _log(
                    "Summer Solstice laser beam tick failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        with runtime.lock:
            if not getattr(runtime, "summer_solstice_laser_active", False):
                return
            timer = scheduler.timer(
                TOWER_BEAM_CHECK_INTERVAL,
                self._run_summer_solstice_laser_tick,
                args=(runtime,),
            )
        timer.start()

    def _stop_summer_solstice_waves(self, runtime, event_prefix: str) -> None:
        """Cancel pending wave spawns and retire active wave EventSets."""

        for definition in runtime.catalog.events.values():
            if (
                definition.applies_to(runtime.map_id)
                and definition.key.startswith(event_prefix)
            ):
                self._stop_map_local_event(runtime, definition)

    def _finish_summer_solstice_defense(self, runtime) -> None:
        """Resolve missing invisible timer actors at the end of survival."""

        if not self._map_event_runtime_is_live(runtime):
            return
        if map_key(runtime.map_id) != "e_sum_minidun_arena_01":
            return
        with runtime.lock:
            runtime.summer_solstice_laser_active = False
        sessions = self._map_event_sessions(runtime)
        if not sessions:
            return
        with sessions[0].flow_lock:
            combat = sessions[0].gameplay_combat_state
            player_actor_id = int(
                getattr(sessions[0], "gameplay_remote_actor_id", 0)
                or getattr(combat, "player_actor_id", 0)
            )
        player_survived = True
        if combat is not None:
            with combat.lock:
                player_health = combat.player_health.get(player_actor_id)
                if player_health is not None and int(player_health[0]) <= 0:
                    player_survived = False
        try:
            self._stop_summer_solstice_waves(
                runtime, "e_sum_dun_01_arena_def_wave_",
            )
            if not player_survived:
                self._fire_map_signal(
                    sessions[0], "MonstersEliminated",
                    ("e_sum_01_arena_def_invisible_ending",),
                    synthetic_dead_templates=(
                        "e_sum_01_arena_def_invisible_ending",
                    ),
                )
                _log(
                    f"Summer Solstice survival ended without chests on "
                    f"{runtime.map_id}: player did not survive the round"
                )
                return
            # Retail uses one invisible actor per chest tier and one ending
            # actor. Those templates are absent from static.db4, so resolve
            # their server-side MonstersEliminated signals at the survival
            # deadline. The authored signal rules still decide which chest
            # events and map sets become active.
            for index in range(1, 8):
                self._fire_map_signal(
                    sessions[0], "MonstersEliminated",
                    (f"e_sum_01_arena_def_invisible_chest_timer_{index:02d}",),
                    synthetic_dead_templates=(
                        f"e_sum_01_arena_def_invisible_chest_timer_{index:02d}",
                    ),
                )
            self._fire_map_signal(
                sessions[0], "MonstersEliminated",
                ("e_sum_01_arena_def_invisible_ending",),
                synthetic_dead_templates=(
                    "e_sum_01_arena_def_invisible_ending",
                ),
            )
            _log(
                f"resolved missing Summer Solstice survival timers on "
                f"{runtime.map_id}; unlocked all seven chest tiers and ended "
                "the 60-second round"
            )
        except Exception as exc:
            _log(
                f"Summer Solstice arena ending failed on {runtime.map_id}: "
                f"{type(exc).__name__}: {exc}"
            )

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

    def _maybe_open_circus_random_gate(
            self, s: Session, runtime: MapEventRuntime | None,
            templates: tuple[str, ...],
    ) -> None:
        """Open one difficulty-matched Circus exit after four boss kills."""

        if runtime is None or map_key(runtime.map_id) != "e_bos_arena_01":
            return
        boss_templates = frozenset({
            "e_bos_02_miniboss_bloodmage",
            "e_bos_02_miniboss_heredur_normal",
            "e_bos_02_miniboss_arachnae_normal",
            "e_bos_02_miniboss_magotina",
            "e_bos_02_miniboss_karabossa",
            "e_bos_02_miniboss_bearach_normal",
            "e_bos_02_miniboss_horseman",
            "e_bos_02_miniboss_nefertari",
            "e_bos_02_miniboss_medusa_normal",
        })
        killed_bosses = boss_templates.intersection(
            str(template).casefold() for template in templates
        )
        if not killed_bosses:
            return
        with runtime.lock:
            runtime.circus_bosses_defeated.update(killed_bosses)
            if (
                len(runtime.circus_bosses_defeated) < 4
                or runtime.circus_random_exit_guid is not None
            ):
                return

        character = getattr(s, "gameplay_character", None)
        player_level = max(1, int(getattr(character, "level", 1) or 1))
        map_tier = max(
            0, int(getattr(s, "gameplay_map_difficulty", 0) or 0),
        )
        # Select the highest level-entry event for this character. Resolve its
        # EventSet rows from the loaded map content rather than hardcoding
        # numeric rows: the server log showed level_50 activates row 643 while
        # the old hardcoded mapping selected exit candidates from row 644.
        level_events = (
            (27, "e_bos_02_level_27_boss_entry_01"),
            (31, "e_bos_02_level_31_boss_entry_01"),
            (37, "e_bos_02_level_37_boss_entry_01"),
            (43, "e_bos_02_level_43_boss_entry_01"),
            (48, "e_bos_02_level_48_boss_entry_01"),
            (50, "e_bos_02_level_50_boss_entry_01"),
        )
        eligible_events = tuple(
            event for minimum_level, event in level_events
            if player_level >= minimum_level
        )
        if not eligible_events:
            _log(
                f"Circus random gate unavailable below level "
                f"{level_events[0][0]} for player level {player_level}"
            )
            return
        selected_event = eligible_events[-1]
        event = runtime.catalog.event(selected_event)
        if event is None:
            _log(f"Circus level event {selected_event!r} is missing")
            return
        event_rows = frozenset(
            int(definition.row)
            for definition in runtime.content.event_sets_for(event.key)
        )
        if not event_rows:
            _log(f"Circus level event {selected_event!r} has no EventSet")
            return
        try:
            exits = tuple(
                definition for definition in load_local_map_exits(
                    self.static_database_path,
                    runtime.map_id,
                    include_inactive_event_sets=True,
                )
                if (
                    int(definition.event_set_row) in event_rows
                    and definition.destination_map_id
                )
            )
        except Exception as exc:
            _log(
                f"Circus gate candidates unavailable: "
                f"{type(exc).__name__}: {exc}"
            )
            return
        if not exits:
            _log(
                f"Circus level event {selected_event!r} EventSets "
                f"{tuple(sorted(event_rows))} have no exits"
            )
            return

        def destination_tier(definition):
            suffix = str(definition.destination_map_id).rsplit("_diff_", 1)
            if len(suffix) != 2 or not suffix[1].isdigit():
                return None
            return int(suffix[1])

        exact_tier = tuple(
            definition for definition in exits
            if destination_tier(definition) == map_tier
        )
        tier_candidates = exact_tier or tuple(
            definition for definition in exits
            if destination_tier(definition) is not None
        )
        if not tier_candidates:
            _log(
                f"Circus EventSets {tuple(sorted(event_rows))} "
                "have no tiered boss exits"
            )
            return
        if not exact_tier:
            nearest = min(
                abs(destination_tier(definition) - map_tier)
                for definition in tier_candidates
            )
            tier_candidates = tuple(
                definition for definition in tier_candidates
                if abs(destination_tier(definition) - map_tier) == nearest
            )
        unique_candidates = {
            bytes(definition.instance_guid): definition
            for definition in tier_candidates
        }
        selected_exit = random.choice(tuple(unique_candidates.values()))
        selected_row = int(selected_exit.event_set_row)
        with runtime.lock:
            if runtime.circus_random_exit_guid is not None:
                return
            runtime.circus_random_exit_guid = bytes(
                selected_exit.instance_guid
            )
        self._schedule_map_local_event_start(runtime, event)
        # The level-entry event can be started by this same boss-kill signal
        # before the four-boss counter chooses its gate. Its first exit refresh
        # therefore saw no selected GUID. Publish again after selection when
        # that authored event is already active; delayed starts will publish
        # through their normal transition once they fire.
        with runtime.lock:
            event_active = event.key in runtime.active_flags()
        if event_active:
            sessions = self._map_event_sessions(runtime)
            if s not in sessions:
                sessions = (*sessions, s)
            self._publish_map_exits(
                runtime, sessions, event_sets=event_rows,
            )
        _log(
            f"opened one Circus boss gate after "
            f"{len(runtime.circus_bosses_defeated)} boss kills: "
            f"{selected_exit.exit_id} -> {selected_exit.destination_map_id} "
            f"(difficulty {map_tier}, EventSet {selected_row})"
        )

    def _on_monsters_killed(self, s: Session, monsters) -> None:
        monsters = tuple(monsters)
        from dso_server.services.monster_summons import cleanup_monster_summons
        cleanup_monster_summons(self, s, monsters)
        tower_kills(self, s, monsters)
        templates = tuple(dict.fromkeys(
            str(getattr(monster, "template_id", "")).casefold()
            for monster in monsters
            if str(getattr(monster, "template_id", ""))
        ))
        if not templates:
            return
        if (
            runtime := getattr(s, "gameplay_map_event_runtime", None)
        ) is not None and map_key(runtime.map_id) == "e_nye_dungeon_01":
            self._record_new_year_machine_kills(s, runtime, monsters, templates)
        if (getattr(s, "gameplay_match", None) is not None
                and getattr(s, "gameplay_match_callback", None) is not None):
            # Battleground objectives (Assault the Fortress structures);
            # the authored signals below still drive the map's events.
            s.gameplay_match_callback(s, ("match_monsters_killed", monsters))
        CharacterService._quest_on_monsters_killed(self, s, monsters)
        CharacterService._achievement_on_monsters_killed(self, s, monsters)
        CharacterService._ranking_on_monsters_killed(self, s, monsters)
        CharacterService._on_map_companion_boss_defeated(self, s, monsters)
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if (
            runtime is not None
            and map_key(runtime.map_id) == _SARGON_THRONE_ROOM_MAP
            and "boss_sargon" in templates
        ):
            with runtime.lock:
                runtime.sargon_throne_first_phase_defeated = True
            _log("Sargon Throne Room first phase defeated")
        if (
            runtime is not None
            and map_key(runtime.map_id) == SARGON_SHADOWFORT_MAP_ID
            and SARGON_SHADOWFORT_SENTINEL_TEMPLATE in templates
        ):
            with runtime.lock:
                if runtime.sargon_shadowfort_sentinel_unlocked:
                    runtime.sargon_shadowfort_sentinel_defeated = True
                    _log(
                        "Sargon Shadowfort Sentinel defeated; boss gate unlocked"
                    )
        score_rules = ()
        mortis = mortis_arena(self, runtime)
        if mortis is not None:
            mortis.killed(monsters)
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
                static_path = resident_content.resolved(self.static_database_path).as_uri()
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
        if (
            runtime is not None
            and map_key(runtime.map_id) == _SARGON_THRONE_ROOM_MAP
            and "boss_sargon" in templates
            and not self._sargon_main_quest_completed(s)
        ):
            # Without the main quest, phase one is the final encounter. The
            # retail BOSS_death signal otherwise only fires for the second
            # form, which this branch intentionally never spawns.
            for event_key in ("boss_start", "boss_spawn"):
                definition = runtime.catalog.event(event_key)
                if definition is not None:
                    self._stop_map_local_event(runtime, definition)
            death_event = runtime.catalog.event("boss_death")
            if death_event is not None:
                self._start_map_local_event(runtime, death_event)
        self._advance_castle_gwenfara(s, runtime, templates)
        if (
            runtime is not None
            and map_key(runtime.map_id) == "e_hal_01_darkwood_dun"
            and "e_hal_01_wood_quest_leader_ripper_01" in templates
        ):
            # The return exit is authored as always active, rather than as a
            # boss-death EventSet. Republish its current access-tier variant
            # after the quest kill so the client can use the Oldfield route.
            sessions = self._map_event_sessions(runtime)
            if s not in sessions:
                sessions = (*sessions, s)
            self._publish_map_exits(runtime, sessions)
        if (
            runtime is not None
            and map_key(runtime.map_id) == "e_dra_03_dun_02"
            and frozenset(templates).intersection({
                "boss_dragan", "e_dra_03_boss_dragan",
            })
        ):
            # Great Hall's return exit and repeatable relic chest share the
            # authored finish-battle EventSet. Some Dragan boss death paths
            # bypass its ClientSignal, so guarantee that the same authored
            # finish event runs from the authoritative lethal-kill callback.
            finish_event = runtime.catalog.event(
                "e_dra_2nd_floor_finish_battle",
            )
            if finish_event is not None:
                self._schedule_map_local_event_start(runtime, finish_event)
                _log(
                    "ensured Great Hall finish event after Dragan defeat; "
                    "publishing return exit and reward chest"
                )
            else:
                _log(
                    "Great Hall Dragan was defeated but the authored finish "
                    "event is missing from the map event catalog"
                )
        self._advance_bgh_all_boss_sequence(s, runtime, monsters)
        self._maybe_open_circus_random_gate(s, runtime, templates)
        if (
            runtime is not None
            and map_key(runtime.map_id) == "e_fullmoon_varholm_dun"
            and frozenset(templates).intersection({
                "e_ful_02_varholm_night_00_miniboss_werewolf_01",
                "e_ful_02_varholm_night_01_miniboss_werewolf_01",
                "e_ful_02_varholm_night_02_miniboss_werewolf_01",
            })
            and CharacterService._varholm_bloodmoon_portal_unlocked(self, s)
        ):
            # The extracted portal signal also requires the quest to remain
            # accepted. A character who already completed the Vargulf task
            # can still summon and kill Vargulf, but that ClientSignal no
            # longer matches. Start the same authored portal event from this
            # authoritative kill edge so the exit is materialized after the
            # boss dies, never when a fresh map instance is entered.
            portal_event = runtime.catalog.event(
                "e_ful_02_bloodmoon_portal",
            )
            if portal_event is not None:
                self._schedule_map_local_event_start(runtime, portal_event)
                _log(
                    "unlocked Bloodmoon portal after eligible Vargulf kill "
                    f"on {runtime.map_id} for character "
                    f"{getattr(s.gameplay_character, 'character_id', 0)}"
                )
        self._unlock_parallel_world_boss_reward_chest(
            runtime, templates,
        )
        self._open_parallel_world_exit_after_boss_kill(
            s, runtime, templates,
        )
        self._fire_map_signal(s, "MonstersEliminated", templates)
        self._ensure_dark_dwarf_harbor_admiral_spawn(runtime, templates)
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

    def _record_new_year_machine_kills(
            self, s: Session, runtime: MapEventRuntime,
            monsters, templates: tuple[str, ...]) -> None:
        killed = _NEW_YEAR_MACHINE_BABIES.intersection(templates)
        if not killed:
            return
        with runtime.lock:
            runtime.new_year_machine_babies_defeated.update(killed)
            defeated = len(runtime.new_year_machine_babies_defeated)
            if runtime.new_year_encounter_cleared:
                return
            if defeated < 4:
                next_stage = int(next(iter(killed)).rsplit("_", 1)[-1]) + 1
                pending = ()
            else:
                next_stage = 0
                runtime.new_year_encounter_cleared = True
                spawn_sets = {
                    int(row.event_set_row)
                    for row in runtime.content.spawn_rows
                    if int(row.event_set_row) >= 0
                }
                runtime.suppress_event_sets(spawn_sets)
                pending = tuple(runtime.pending.values())
                runtime.pending.clear()
                runtime.pending_start_deadlines.clear()
                runtime.pending_start_tokens.clear()
                runtime.wave_countdown_digits.clear()
        if defeated < 4:
            sessions = self._map_event_sessions(runtime)
            next_event = _next_new_year_machine_event(
                runtime, next_stage, len(sessions),
            )
            if next_event is not None:
                _log(
                    f"Rocketmen Machine Baby defeated on {runtime.map_id}: "
                    f"{defeated}/4; activating machine {next_stage} now"
                )
                self._start_map_local_event(runtime, next_event)
            else:
                _log(
                    f"Rocketmen Machine Baby defeated on {runtime.map_id}: "
                    f"{defeated}/4; next activation event was not found"
                )
            return
        for handle in pending:
            cancel = getattr(handle, "cancel", None)
            if cancel is not None:
                cancel()

        sessions = self._map_event_sessions(runtime)
        self._publish_new_year_worldboss_loot_prop(runtime, sessions)
        combat = self._map_event_combat_state(sessions)
        if combat is None:
            return
        with combat.lock:
            living = tuple(
                (int(actor_id), monster)
                for actor_id, monster in combat.monsters.items()
                if combat.monster_health.get(actor_id, 0) > 0
            )
            actor_ids = tuple(sorted(actor_id for actor_id, _monster in living))
        from dso_server.services.monster_summons import cleanup_monster_summons
        cleanup_monster_summons(
            self, s, tuple(monster for _actor_id, monster in living),
        )
        with combat.lock:
            retired = set(combat.retire_monsters(actor_ids))
        if retired:
            for viewer in sessions:
                with viewer.flow_lock:
                    if viewer.gameplay_combat_state is not combat:
                        continue
                    discard = tuple(sorted(
                        retired & viewer.gameplay_spawned_monster_actor_ids
                    ))
                    viewer.gameplay_spawned_monster_actor_ids.difference_update(retired)
                    CharacterService._forget_published_monsters(viewer, retired)
                    viewer.gameplay_announced_monster_actor_ids.difference_update(retired)
                    viewer.gameplay_requested_actor_ids.difference_update(retired)
                    for actor_id in retired:
                        viewer.gameplay_delivered_monster_positions.pop(actor_id, None)
                if discard:
                    viewer.send_reliable_ordered(
                        encode_discard_monster_batch(discard),
                        track=True,
                        label="rocketmen-encounter-clear",
                    )
        _log(
            "Rocketmen encounter cleared after all four Machine Babies; "
            f"retired {len(retired)} living mob(s) and suppressed "
            f"{len(spawn_sets)} spawn EventSet(s)"
        )

    def _publish_new_year_worldboss_loot_prop(
            self, runtime: MapEventRuntime, sessions) -> None:
        """Activate the authored rocket-pile prop as the final Worldboss loot."""

        with runtime.lock:
            source = next((
                definition for definition in runtime.content.props
                if definition.instance_id.casefold()
                == "e_nye_01_rocketpile_01"
            ), None)
            if source is None:
                _log(
                    "could not activate New Year Worldboss loot prop: "
                    "authored e_nye_01_rocketpile_01 placement was not found"
                )
                return
            definition = replace(
                source,
                instance_guid=ZERO_GUID,
                event_set_row=-1,
                one_shot=True,
                loot_list_id="lootlist_e_nye_worldboss_pile",
                chest_id="",
                signal_referenced=False,
                workbench=False,
                quest_switch_id="",
                key_id="",
                key_amount=0,
            )
            state = PropRuntimeState(definition, PROP_STATE_ACTIVE)
            runtime.props[definition.prop_id] = state
        for viewer in sessions:
            _send_prop_info(
                viewer, (state,), label="new-year-worldboss-loot-prop",
            )
        _log(
            "activated authored New Year Worldboss loot prop "
            "e_nye_01_rocketpile_01 "
            f"at {tuple(round(axis, 2) for axis in state.definition.position)}; "
            f"published to {len(sessions)} player(s)"
        )

    def _ensure_dark_dwarf_harbor_admiral_spawn(
            self, runtime: MapEventRuntime | None,
            killed_templates: tuple[str, ...]) -> None:
        """Start Harbor's authored Admiral event after both marker champions die.

        The retail ``MonstersEliminated`` signal is still evaluated above.
        Track the two marker kills on the shared map runtime as a fallback so
        despawning corpses or imported spawn rows cannot lose the transition.
        """

        if (
            runtime is None
            or map_key(runtime.map_id) != "e_viscanium_submarine_01"
        ):
            return
        anchors = frozenset({
            "e_ide_02_harbor_champion_engineer_01",
            "e_ide_02_harbor_champion_dark_dwarf_gunner",
        })
        killed = anchors.intersection(
            str(template).casefold() for template in killed_templates
        )
        if not killed:
            return
        with runtime.lock:
            runtime.dark_dwarf_harbor_champions_defeated.update(killed)
            if (
                not anchors.issubset(
                    runtime.dark_dwarf_harbor_champions_defeated
                )
                or runtime.dark_dwarf_harbor_admiral_event_scheduled
            ):
                return
            runtime.dark_dwarf_harbor_admiral_event_scheduled = True
        definition = runtime.catalog.event("dark_dwarf_map2_boss_spwan")
        if definition is None or not definition.applies_to(runtime.map_id):
            with runtime.lock:
                runtime.dark_dwarf_harbor_admiral_event_scheduled = False
            _log(
                "Dark Dwarf Harbor Admiral spawn event is missing from the "
                "map event catalog"
            )
            return
        self._schedule_map_local_event_start(runtime, definition)
        _log(
            "started Dark Dwarf Harbor Admiral event after both marker "
            "champions were defeated"
        )

    def _advance_castle_gwenfara(
            self, s: Session, runtime: MapEventRuntime | None,
            templates: tuple[str, ...],
    ) -> None:
        """Spawn the next Gwenfara form as soon as the current one dies."""
        if runtime is None or map_key(runtime.map_id) != "e_hal_01_castle_dun":
            return
        if "e_hal_01_boss_gwenfara_phase_01" in templates:
            next_phase = 2
        elif "e_hal_01_boss_gwenfara_phase_02" in templates:
            next_phase = 3
        else:
            return
        boss_row = next((
            row for row in runtime.content.spawn_rows
            if row.template_id.casefold()
            == "e_hal_01_boss_gwenfara_phase_01"
        ), None)
        if boss_row is None:
            _log("Creepy Castle Gwenfara phase spawnpoint is missing")
            return
        marker = f"castle_gwenfara_phase_0{next_phase}_spawned"
        with runtime.lock:
            if marker in runtime.fired_once:
                return
            runtime.fired_once.add(marker)
        sessions = self._map_event_sessions(runtime)
        if s not in sessions:
            sessions = (*sessions, s)
        next_template = f"e_hal_01_boss_gwenfara_phase_0{next_phase}"
        self._spawn_event_monsters(
            runtime, sessions, (),
            extra_rows=(replace(
                boss_row,
                row_id=-2_000_100 - next_phase,
                template_id=next_template,
            ),),
        )
        _log(
            f"Creepy Castle Gwenfara phase 0{next_phase - 1} defeated; "
            f"spawned {next_template} immediately"
        )

    def _advance_bgh_all_boss_sequence(
            self, s: Session, runtime: MapEventRuntime | None, monsters,
    ) -> None:
        """Spawn the next authored boss in the combined BGH arena."""

        if runtime is None or map_key(runtime.map_id) != "e_bgh_boss_all":
            return
        combat = getattr(s, "gameplay_combat_state", None)
        if combat is None:
            return
        killed = tuple(
            monster for monster in monsters
            if str(getattr(monster, "template_id", "")).casefold()
            in _BGH_ALL_BOSS_SEQUENCE
        )
        if not killed:
            return
        for dead in killed:
            dead_template = str(dead.template_id).casefold()
            next_template = _BGH_ALL_BOSS_SEQUENCE.get(dead_template)
            if next_template is None:
                continue
            with runtime.lock:
                if next_template in runtime.bgh_sequence_spawned:
                    continue
                runtime.bgh_sequence_spawned.add(next_template)
            with combat.lock:
                position = tuple(
                    float(value)
                    for value in combat.monster_positions.get(
                        int(dead.actor_id), dead.position,
                    )
                )
            sequence_index = tuple(_BGH_ALL_BOSS_SEQUENCE.values()).index(
                next_template
            )
            row = GatedSpawnRow(
                row_id=2_000_000 + sequence_index,
                event_set_row=-1,
                template_id=next_template,
                position=position,
            )
            sessions = self._map_event_sessions(runtime)
            if s not in sessions:
                sessions = (*sessions, s)
            timer_factory = getattr(
                getattr(runtime, "scheduler", None), "timer", None,
            ) or getattr(s, "gameplay_deadline_timer", None)
            if not callable(timer_factory):
                _log(
                    "Big Game Hunt boss sequence has no deadline scheduler; "
                    f"spawning {next_template!r} immediately"
                )
                self._spawn_event_monsters(
                    runtime, sessions, (), extra_rows=(row,),
                )
                continue
            timer = timer_factory(
                1.5,
                self._spawn_bgh_sequence_boss,
                args=(runtime, s, row, dead_template, next_template),
            )
            timer.start()
            _log(
                f"Big Game Hunt combined arena queued {next_template!r} "
                f"1.5s after {dead_template!r} died"
            )

    def _spawn_bgh_sequence_boss(
            self, runtime, fallback_session, row, previous_template,
            next_template,
    ) -> None:
        """Spawn a chained Blazing Inferno boss after its short pause."""

        if not self._map_event_runtime_is_live(runtime):
            return
        sessions = self._map_event_sessions(runtime)
        if not sessions:
            with fallback_session.flow_lock:
                still_in_instance = (
                    fallback_session.connected
                    and getattr(
                        fallback_session, "gameplay_map_event_runtime", None,
                    ) is runtime
                )
            if still_in_instance:
                sessions = (fallback_session,)
        if not sessions:
            _log(
                f"Big Game Hunt delayed spawn of {next_template!r} "
                "cancelled because the arena has no active players"
            )
            return
        self._spawn_event_monsters(
            runtime, sessions, (), extra_rows=(row,),
        )
        _log(
            f"Big Game Hunt combined arena spawned {next_template!r} "
            f"after {previous_template!r}"
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
            chest_states = tuple(
                state for state in runtime.published_props()
                if state.definition.instance_id.casefold()
                in PARALLEL_WORLD_REWARD_CHEST_INSTANCE_IDS
            )
            for state in chest_states:
                state.state = PROP_STATE_ACTIVE
        viewers = self._map_event_sessions(runtime)
        for viewer in viewers:
            for state in chest_states:
                self._restore_prop_for_viewer(
                    viewer, state.definition, label="boss-chest-unlocked",
                )
        _log(
            f"boss reward chest unlocked on {runtime.map_id} after "
            f"{gate[0]} was defeated; refreshed {len(chest_states)} "
            f"chest prop(s) for {len(viewers)} player(s)"
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

    def _publish_dtu_arena_event_props(
            self, runtime: MapEventRuntime | None, viewers,
            *, event_sets=(),
    ) -> None:
        """Replay DTU props after their EventUpdate has reached the client.

        The event bonus chest set can be active in the initial snapshot, which
        is sent before the client finishes loading the map. Boss altar props
        and the post-kill reward chests are also event-created scene entities;
        replaying their tier-selected PropInfo after the transition avoids
        losing the first packet while the scene catches up.
        """

        if runtime is None:
            return
        map_id = map_key(runtime.map_id)
        circus = map_id == "e_bos_arena_01"
        boss_room = is_dtu_boss_arena_map(map_id)
        if not circus and not boss_room:
            return
        wanted_sets = {int(row) for row in event_sets}
        with runtime.lock:
            states = tuple(
                state for state in runtime.published_props()
                if (
                    not wanted_sets
                    or int(state.definition.event_set_row) in wanted_sets
                )
                and (
                    (
                        circus
                        and (
                            state.definition.template_id.casefold().startswith(
                                "e_bos_02_super_chest",
                            )
                            or state.definition.instance_id.casefold().startswith(
                                "e_bos_arena_",
                            )
                        )
                    )
                    or (
                        boss_room
                        and state.definition.template_id.casefold()
                        in DTU_ARENA_REWARD_CHEST_IDS
                    )
                )
            )
        if not states:
            return
        circus_superchests = tuple(
            state.definition.instance_id
            for state in states
            if circus
            and state.definition.template_id.casefold().startswith(
                "e_bos_02_super_chest",
            )
        )
        sent_props = 0
        for viewer in viewers:
            visible = tuple(
                state for state in states
                if boss_room
                or prop_visible_to_player(state.definition, viewer)
            )
            if visible:
                sent_props += len(visible)
                _send_prop_info(
                    viewer, visible, label="dtu-event-prop-replay",
                    # Runtime.select_props has already selected exactly one
                    # MapVariation tier for this instance. Applying the
                    # session's separate difficulty filter a second time can
                    # discard that chest tier and leave the arena empty.
                    filter_map_variations=not boss_room,
                )
        _log(
            f"replayed {len(states)} DTU event prop(s) on {runtime.map_id}; "
            f"event_sets={tuple(sorted(wanted_sets))} viewers={len(viewers)} "
            f"sent={sent_props}"
            + (
                f" circus_superchests={circus_superchests}"
                if circus else ""
            )
            + (
                " boss_reward_chests="
                + ";".join(
                    f"{state.definition.instance_id}@"
                    f"{tuple(round(axis, 2) for axis in state.definition.position)}"
                    for state in states
                )
                if boss_room else ""
            )
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
            if event_key in runtime.parallel_world_gatekeepers_defeated:
                return
            runtime.parallel_world_gatekeepers_defeated.add(event_key)
            already_started = (
                event_key in runtime.running
                or event_key in runtime.pending
                or event_key in runtime.initial_flags
            )
            runtime.initial_flags = runtime.initial_flags - {event_key}
        if already_started:
            # A quest/client signal may have started the event before the
            # boss died, possibly before any ready viewer received its exit.
            # Publish a real inactive edge before rebuilding the portal.
            definition = runtime.catalog.event(event_key)
            if definition is not None:
                self._stop_map_local_event(runtime, definition)
        _log(
            f"Parallel World gatekeeper defeated on {runtime.map_id}: "
            f"starting exit event {event_key!r}"
        )
        if map_key(runtime.map_id) == "pw001_grimmagstone_01_dun":
            runtime.grimmagstone_gatekeeper_defeated = True
        definition = runtime.catalog.event(event_key)
        if definition is not None:
            self._schedule_map_local_event_start(runtime, definition)

    @staticmethod
    def _notify_monsters_killed(s: Session, monsters) -> None:
        """Raise the kill toward the server from static combat code.

        The kill's consequences (quests, achievements, daily challenges,
        rankings, map-event kill signals, companions) run on the progression
        queue, so the hit that killed the monster -- and the loot it drops --
        does not wait for them.
        """

        callback = getattr(s, "gameplay_presence_callback", None)
        if callback is None or not monsters:
            return
        monsters = tuple(monsters)
        if progression_queue.defer(
            CharacterService._raise_monsters_killed, s, monsters,
            label="monster kills", guard=progression_queue.same_map_instance(s),
        ):
            return
        CharacterService._raise_monsters_killed(s, monsters)

    @staticmethod
    def _raise_monsters_killed(s: Session, monsters) -> None:
        callback = getattr(s, "gameplay_presence_callback", None)
        if callback is None:
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

    def _reconcile_newmoon_city_portals(self, s: Session, runtime) -> None:
        """Close stale New Moon boss portals when the scheduled event has ended."""

        if map_key(runtime.map_id) != "e_newmoon_blakborg_city_dun":
            return
        try:
            active_events = CharacterService._event_catalog(
                self,
            ).active_template_ids()
        except Exception as exc:
            _log(f"could not check New Moon portal schedule: {exc}")
            return
        if "newmoon_event_02" in active_events:
            return

        sessions = self._map_event_sessions(runtime)
        if s not in sessions:
            sessions = (*sessions, s)
        # A runtime can outlive the event schedule that initialized it. Stop
        # the quest portal events as well as removing the old global flag, so
        # neither EventUpdate nor ExitInfo can leave the boss doors visible.
        portal_event_ids = (
            "e_new_02_witch_a_06_portal",
            "e_new_02_witch_b_06_portal",
        )
        for event_id in portal_event_ids:
            definition = runtime.catalog.event(event_id)
            if definition is None:
                continue
            with runtime.lock:
                active = definition.key in runtime.running
                pending = definition.key in runtime.pending_start_tokens
            if active or pending:
                self._stop_map_local_event(runtime, definition)

        with runtime.lock:
            enabled, disabled = runtime.replace_global_flags(
                {"newmoon_event_02", *portal_event_ids}, frozenset(),
            )
        if disabled:
            self._retire_event_monsters(runtime, sessions, disabled)
            self._unpublish_event_props(runtime, sessions, disabled)
        if enabled:
            self._publish_event_props(runtime, sessions, enabled)
            self._spawn_event_monsters(runtime, sessions, enabled)
            self._spawn_event_npcs(runtime, sessions, enabled)
        if enabled or disabled:
            self._publish_map_exits(
                runtime, sessions, disabled_event_sets=disabled,
            )
            _log(
                f"reconciled expired New Moon flags on {runtime.map_id}: "
                f"enabled={enabled}, disabled={disabled}"
            )

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
        CharacterService._ranking_on_map_entered(self, s)
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            return
        self._reconcile_newmoon_city_portals(s, runtime)
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
        self._schedule_sargon_shadowfort_gate_refresh(s, runtime)
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
        if map_key(runtime.map_id) == "e_sum_dun_02":
            # Kalodoro's entry rules replace each base trader with its active
            # discount NPC after the initial map NPCInfo batch. Re-send quest
            # metadata once those synchronous event transitions have settled,
            # so quest markers don't remain attached only to hidden base NPCs.
            CharacterService._quest_refresh_npc_infos(self, s)
        self._ensure_fyeborough_soul_collector(s, runtime)
        self._ensure_artaya_clearing_demons(s, runtime)
        self._ensure_pw209_sultans_enforcer02_mobs(s, runtime)
        self._ensure_pw209_slade_throne_room(s, runtime)
        self._ensure_pw000_senbu_boss(s, runtime)
        # Achievements and the season pass recount the whole collection,
        # equipment and skins on entry; the player's first inputs on the map
        # must not wait behind them.
        map_keys = (runtime.map_id, access_group, access_id)
        if not progression_queue.defer(
            CharacterService._achievement_on_map_entered, self, s, map_keys,
            label="map-entry achievements",
            guard=progression_queue.same_character(s),
        ):
            CharacterService._achievement_on_map_entered(self, s, map_keys)
        # Pays what was reached before rewards existed or did not fit the bags.
        if not progression_queue.defer(
            CharacterService._grant_stickerbook_rewards, self, s,
            label="sticker-book rewards",
            guard=progression_queue.same_character(s),
        ):
            CharacterService._grant_stickerbook_rewards(self, s)
        CharacterService._restore_map_companion_handoffs(self, s)
        if s.gameplay_last_move is not None:
            CharacterService._check_map_companions(s, s.gameplay_last_move.relative_position)
        # Publish after map entry signals, boss actor replay, and quest state
        # synchronization so the arena chest appears with its boss in the
        # fully loaded client scene.
        self._ensure_parallel_world_boss_reward_chest_visible(runtime)
        self._publish_parallel_world_boss_reward_chest_props(s, runtime)
        if map_key(runtime.map_id) == "e_bos_arena_01":
            self._publish_dtu_arena_event_props(runtime, (s,))
        event_catalog = getattr(s, "gameplay_event_catalog", None)
        if event_catalog is None:
            try:
                event_catalog = CharacterService._event_catalog(self)
            except (OSError, ValueError, sqlite3.Error):
                event_catalog = None
        if event_catalog is not None:
            self._reconcile_easter_giant_egg(
                runtime, (s,), event_catalog,
            )

    def _schedule_sargon_shadowfort_gate_refresh(
            self, s: Session, runtime: MapEventRuntime) -> None:
        """Reapply the locked gate after the client has loaded the map scene."""

        if map_key(runtime.map_id) != SARGON_SHADOWFORT_MAP_ID:
            return
        scheduler = getattr(runtime, "scheduler", None)
        if scheduler is None:
            return
        with runtime.lock:
            if runtime.sargon_shadowfort_sentinel_defeated:
                return
        handle = scheduler.timer(
            1.0,
            self._refresh_sargon_shadowfort_gate_state,
            args=(s, runtime),
        )
        handle.start()

    def _refresh_sargon_shadowfort_gate_state(
            self, s: Session, runtime: MapEventRuntime) -> None:
        if not self._map_event_runtime_is_live(runtime):
            return
        with s.flow_lock:
            if (
                not s.connected
                or not s.gameplay_presence_ready
                or s.gameplay_map != runtime.map_id
                or getattr(s, "gameplay_map_event_runtime", None) is not runtime
            ):
                return
        with runtime.lock:
            if runtime.sargon_shadowfort_sentinel_defeated:
                return
        self._publish_map_event_snapshot(
            runtime,
            (s,),
            changed_event="sargon_shadowfort_gate_entry_refresh",
            active=False,
        )
        _log("reapplied closed Sargon Shadowfort boss gate after map load")

    def _reconcile_easter_giant_egg(self, runtime, sessions, catalog) -> None:
        """Spawn one event Giant Egg in each active dungeon instance."""

        event_set = -1901
        template_id = "e_easter_01_boss_egg_easter"
        current_map = map_key(runtime.map_id)
        active = (
            runtime.content.is_dungeon
            and not current_map.startswith(("e_easter_dun_", "pw"))
            and "easter_event_01" in catalog.active_template_ids()
        )
        if active:
            egg_sets = {
                int(prop.event_set_row)
                for prop in runtime.content.props
                if _is_easter_dungeon_festival_egg(prop)
                and int(prop.event_set_row) >= 0
            }
            if egg_sets:
                self._publish_event_props(
                    runtime, sessions, egg_sets, refresh_existing=True,
                )
        combat = self._map_event_combat_state(sessions)
        if combat is None:
            return
        if not active:
            self._retire_event_monsters(runtime, sessions, (event_set,))
            with runtime.lock:
                runtime.fired_once.discard("easter_giant_egg_spawned")
                runtime.fired_once.discard("easter_giant_egg_spawning")
            return

        with runtime.lock:
            if (
                "easter_giant_egg_spawned" in runtime.fired_once
                or "easter_giant_egg_spawning" in runtime.fired_once
            ):
                return
            runtime.fired_once.add("easter_giant_egg_spawning")
        try:
            with combat.lock:
                alive = set(combat.alive_actor_ids)
                existing = tuple(
                    actor_id for actor_id, monster in combat.monsters.items()
                    if actor_id in alive
                    and monster.template_id.casefold() == template_id
                )
                candidates = tuple(
                    tuple(float(value) for value in combat.monster_positions.get(
                        actor_id, monster.position,
                    ))
                    for actor_id, monster in combat.monsters.items()
                    if actor_id in alive
                    and not monster.rank_id.casefold().startswith("rank_boss")
                    and monster.template_id.casefold() != template_id
                )
            if existing:
                with runtime.lock:
                    runtime.event_set_actor_ids.setdefault(event_set, set()).update(
                        existing
                    )
                    runtime.fired_once.add("easter_giant_egg_spawned")
                return
            if not candidates or _load_runtime_definition(
                self.static_database_path, template_id, 0, stationary=True,
            ) is None:
                return

            spawn_position = None
            for anchor in random.sample(candidates, len(candidates)):
                offsets = [(4.0, 0.0), (-4.0, 0.0), (0.0, 4.0), (0.0, -4.0)]
                random.shuffle(offsets)
                for dx, dz in offsets:
                    try:
                        spawn_position = _grounded_spawn_position(
                            combat.encounter.navigation,
                            combat.encounter.terrain,
                            (anchor[0] + dx, anchor[1], anchor[2] + dz),
                            reference_position=anchor,
                            max_horizontal_distance=8.0,
                        )
                    except (CollisionTerrainError, NavigationError, ValueError):
                        continue
                    if spawn_position is not None:
                        break
                if spawn_position is not None:
                    break
            if spawn_position is None:
                return

            sessions = tuple(sessions) or self._map_event_sessions(runtime)
            row = GatedSpawnRow(
                -1_901_000, event_set, template_id, spawn_position,
            )
            self._spawn_event_monsters(runtime, sessions, (), extra_rows=(row,))
            with runtime.lock:
                spawned = bool(runtime.event_set_actor_ids.get(event_set))
                if spawned:
                    runtime.fired_once.add("easter_giant_egg_spawned")
                else:
                    runtime.fired_once.discard("easter_giant_egg_spawned")
        finally:
            with runtime.lock:
                runtime.fired_once.discard("easter_giant_egg_spawning")

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
        if _is_easter_dungeon_festival_egg(definition):
            # Map-content refreshes can keep a previously registered prop
            # definition alive after the event publisher has assigned the
            # Festival Egg table. Resolve from the egg template at click time
            # too, so the actual reward roll cannot fall back to the retail
            # scaling-list id (which has no authored server loot rows).
            loot_list_id = (
                "chest_e_easter_egg_island"
                if map_key(getattr(s, "gameplay_map", ""))
                == "e_easter_dun_01"
                else "chest_e_easter_egg_normal"
            )
        is_bgh_mythic_chest = (
            loot_list_id == _BLOOD_CHEST_LOOT_LIST
            and map_key(getattr(s, "gameplay_map", "")) in {
                "e_bgh_boss_all", "e_bgh_boss_guardian_all",
            }
        )
        is_parallel_world_boss_chest = (
            str(definition.instance_id).casefold()
            in PARALLEL_WORLD_REWARD_CHEST_INSTANCE_IDS
        )
        parallel_world_jewel_chance = None
        equipment_rewards_by_template = {}
        dragan_gate_claim_update = None
        class_name = ""
        dtu_event_progress_reward = 0
        with s.flow_lock:
            chest_character = s.gameplay_character
            player_level = max(
                1, int(getattr(chest_character, "level", 1) or 1)
            )
            class_name = CLASS_NAMES.get(
                int(getattr(chest_character, "class_id", 0) or 0), ""
            ).casefold()
        if is_parallel_world_boss_chest:
            tier = int(definition.variation_difficulty or 0)
            if tier >= BLOODSHED_DIFFICULTY:
                materi_amount = 10_000
            elif tier >= 7:
                materi_amount = 5_000
            else:
                materi_amount = 2_000
            materi_definition = load_purchase_item_definition(
                self.static_database_path, "materi_fragment_01",
            )
            specifications = []
            remaining_materi = materi_amount
            while remaining_materi > 0:
                stack = min(
                    remaining_materi,
                    max(1, int(materi_definition.max_stack_size)),
                )
                specifications.append(("materi_fragment_01", stack))
                remaining_materi -= stack
            with s.flow_lock:
                access_id = str(getattr(s, "gameplay_map_access_id", "") or "")
                character = s.gameplay_character
                player_level = max(1, int(getattr(character, "level", 1) or 1))
                class_name = CLASS_NAMES[
                    int(getattr(character, "class_id", 0))
                ].lower()
            rng = random.SystemRandom()
            # The CMS jewel rate is the source of truth for these chest
            # jewels. Apply the selected map difficulty's privileged-loot
            # factor in the same way as other Possible Rewards categories.
            jewel_rates = possible_reward_rates(access_id)
            _ordinary_chance, parallel_world_jewel_chance = (
                _difficulty_drop_chances(
                    prime_world_loot_cache(self.static_database_path),
                    tier,
                    monster_drop=jewel_rates["jewel"],
                    privileged_drop=jewel_rates["jewel"],
                )
            )
            gem_count = rng.randint(1, 2)
            try:
                gem_templates = roll_possible_reward_gems(
                    self.static_database_path, access_id, gem_count, rng,
                )
            except (OSError, ValueError, sqlite3.Error) as exc:
                _log(
                    f"cannot roll Parallel World chest gems: "
                    f"{type(exc).__name__}: {exc}"
                )
                gem_templates = ()
            specifications.extend((template_id, 1) for template_id in gem_templates)
            # Use the selected map difficulty's authored equipment rarity
            # weights for each of the one or two class-appropriate items.
            try:
                equipment_rarity_weights = (
                    prime_world_loot_cache(self.static_database_path)
                    .rarity_weights_by_difficulty.get(tier, (0, 0, 1, 0, 0))
                )
                rarity_indices = (2, 3, 4)  # magic, rare, epic
                rarity_weights = tuple(
                    equipment_rarity_weights[index]
                    for index in rarity_indices
                )
                item_count = rng.randint(1, 2)
                from dso_server.protocol.chest_loot_protocol import (
                    roll_chest_contents,
                )
                from dso_server.protocol.world_loot_protocol import (
                    EQUIPMENT_RARITIES,
                )

                for _ in range(item_count):
                    rarity_index = rng.choices(
                        rarity_indices, weights=rarity_weights, k=1,
                    )[0]
                    rarity = EQUIPMENT_RARITIES[rarity_index].casefold()
                    rolled = roll_chest_contents(
                        self.static_database_path,
                        f"all_class_all_equip_{rarity}_01",
                        player_level=player_level,
                        class_name=class_name,
                        rng=rng,
                    )
                    for reward in rolled.rewards:
                        specifications.append((
                            reward.definition.template_id,
                            int(reward.amount),
                        ))
                        equipment_rewards_by_template.setdefault(
                            reward.definition.template_id, [],
                        ).append(reward)
            except (OSError, ValueError, sqlite3.Error) as exc:
                _log(
                    f"cannot roll Parallel World chest equipment: "
                    f"{type(exc).__name__}: {exc}"
                )
        else:
            specifications = None
        is_a_mighty_sentinel_chest = (
            "sentinel_lootchest_" in (
                f"{definition.instance_id} {definition.template_id}"
            ).casefold()
        )
        is_dtu_superchest = loot_list_id in _DTU_SUPERCHEST_POOLS
        is_dtu_boss_reward_chest = loot_list_id in {
            "chest_e_bos_normal", "chest_e_bos_golden",
        }
        if is_parallel_world_boss_chest:
            pass
        elif is_dtu_boss_reward_chest:
            # The arena exports name the one-key and ten-key chest lists but
            # carry no server-side rows. Use the requested Bloodshed reward
            # amounts as the top-tier baseline and scale lower tiers down.
            rng = random.SystemRandom()
            tier = max(0, min(9, int(
                definition.variation_difficulty
                if definition.variation_difficulty is not None
                else getattr(s, "gameplay_map_difficulty", 0)
            )))
            triumphant = loot_list_id == "chest_e_bos_golden"
            scale = ((tier + 1) / 10.0) * (1.0 if triumphant else 0.1)
            dtu_event_progress_reward = max(1, round(100 * scale))
            specifications = [
                ("money_vc_gold", max(1, round(999 * scale))),
                ("e_chr_2012_event_currency", max(1, round(100 * scale))),
                ("money_rc", max(1, round(100 * scale))),
                # chest_contents_descriptions.json maps the displayed Gold-
                # embroidered Silk Fabric name to these five difficulty tiers.
                (f"boss_ingredient_dtu_rarity_{min(5, 1 + tier // 2):02d}", 1),
            ]
            with s.flow_lock:
                access_id = str(getattr(s, "gameplay_map_access_id", "") or "")
                character = s.gameplay_character
                class_name = (
                    CLASS_NAMES.get(int(character.class_id), "").casefold()
                    if character is not None else ""
                )
            try:
                gems = roll_possible_reward_gems(
                    self.static_database_path, access_id, 1, rng,
                )
            except (OSError, ValueError, sqlite3.Error) as exc:
                _log(
                    f"cannot roll DTU boss-chest gem: "
                    f"{type(exc).__name__}: {exc}"
                )
                gems = ()
            specifications.extend((template_id, 1) for template_id in gems)
            if rng.random() < 0.10:
                specifications.append((
                    "pet_ingredient_crystal_03",
                    max(1, round(1 * scale)),
                ))
            if rng.random() < 0.10:
                specifications.append((
                    "jewel_resource_cost_common", 1,
                ))
            if rng.random() < 0.10:
                specifications.append((
                    f"unique_gem_dtu_weaponmod_buff_{min(3, tier // 2):02d}",
                    1,
                ))
            if rng.random() < 0.20 and class_name in _DTU_CLASS_ADORNMENTS:
                specifications.append((
                    _DTU_CLASS_ADORNMENTS[class_name], 1,
                ))
            _log(
                f"rolled DTU {'Triumphant' if triumphant else 'Undefeatable'} "
                f"boss chest on {s.gameplay_map} at difficulty {tier}; "
                f"rewards={tuple(specifications)} "
                f"event_progress={dtu_event_progress_reward}"
            )
        elif is_dtu_superchest:
            # Circus Monstrorum superchest loot is pinned to the four chest
            # instances in screenshot order. Reopening a chest always yields
            # its same authored reward list; no weighted/random selection.
            instance_id = str(definition.instance_id or "").casefold()
            suffix = instance_id.rsplit("_", 1)[-1]
            if suffix not in {"01", "02", "03", "04"}:
                suffix = "01"
            with s.flow_lock:
                character = s.gameplay_character
                class_name = CLASS_NAMES[
                    int(getattr(character, "class_id", 0))
                ].lower()

            def class_item(mapping, fallback=None):
                return mapping.get(class_name, fallback)

            if loot_list_id in {
                "chest_lootlist_e_bos_superchest_copper",
                "chest_lootlist_e_bos_superchest_wood",
            }:
                # The Copper post contains two captures. Pair them across the
                # four static chest instances in order (01/03, 02/04). The
                # undocumented Wooden tier uses this lower bronze reward set.
                capture = "01" if suffix in {"01", "03"} else "02"
                if capture == "01":
                    specifications = [
                        ("gem_diamond_poison_b", 2),
                        ("gem_block_b", 1),
                        ("gem_diamond_fire_b", 1),
                        ("gem_diamond_andermagic_b", 1),
                        ("gem_diamond_andermagic_c", 1),
                        ("gem_cyanit_b", 1),
                        ("gem_amethyst_b", 1),
                        ("gem_ruby_b", 1),
                        ("gem_faster_run_b", 1),
                        ("gem_onyx_c", 1),
                        ("gilded_clover", 5),
                        ("pet_little_arachna", 1),
                        ("all_basic_ring_02", 1),
                    ]
                else:
                    specifications = [
                        ("gem_diamond_fire_b", 1),
                        ("gem_cyanit_b", 1),
                        ("gem_ruby_b", 1),
                        ("gem_faster_run_b", 1),
                        ("gem_onyx_c", 1),
                        ("gem_diamond_andermagic_b", 1),
                        ("gilded_clover", 5),
                        ("premium_teaser_one_week_consumable_7d", 1),
                    ]
                adornment = class_item(_DTU_CLASS_ADORNMENTS)
                guard_cloak = class_item(_DTU_CLASS_GUARD_CLOAKS)
                pandora_weapon = class_item(_DTU_CLASS_SET_WEAPONMODS)
                if adornment:
                    specifications.append((adornment, 1))
                if guard_cloak:
                    specifications.append((guard_cloak, 1))
                if capture == "02" and pandora_weapon:
                    specifications.append((pandora_weapon, 1))
            elif loot_list_id == "chest_lootlist_e_bos_superchest_silver":
                silver = {
                    "01": [
                        ("gem_cyanit_b", 1), ("gem_diamond_fire_b", 1),
                        ("gilded_clover", 5), ("CLASS_STRENGTH_CLOAK", 1),
                        ("CLASS_GLOVES", 1),
                        ("deluxe_teaser_one_week_consumable_7d", 1),
                        ("gem_diamond_b", 1),
                        ("gem_diamond_andermagic_b", 1),
                        ("CLASS_SET_WEAPONMOD", 1), ("gem_block_b", 1),
                        ("gem_amethyst_b", 1),
                    ],
                    "02": [
                        ("gem_diamond_andermagic_h", 1),
                        ("gem_cyanit_b", 1), ("gem_amethyst_b", 1),
                        ("gem_diamond_andermagic_c", 1),
                        ("gem_faster_run_h", 1),
                        ("CLASS_UNDEFEATABLE_CLOAK", 1),
                        ("CLASS_SET_WEAPONMOD", 1),
                        ("gem_diamond_andermagic_b", 2),
                        ("gilded_clover", 5), ("CLASS_SHOULDERS", 1),
                        ("gem_diamond_b", 1),
                    ],
                    "03": [
                        ("gem_ruby_c", 1), ("gem_cyanit_b", 1),
                        ("CLASS_LONG_WEAPON", 1),
                        ("gem_diamond_ice_b", 1),
                        ("jewel_buff_GroupResourceReg_maxHealthpoints_onHit_common", 1),
                        ("gem_faster_run_b", 1),
                        ("gem_diamond_lightning_b", 2),
                        ("all_basic_amulet_02", 1),
                    ],
                    "04": [
                        ("gem_onyx_b", 1), ("gem_diamond_lightning_b", 1),
                        ("jewel_buff_GroupHpReg_maxResource_onHit_common", 1),
                        ("CLASS_PANDORA_AMULET", 1),
                        ("gem_diamond_andermagic_b", 2),
                        ("CLASS_HELMET", 1), ("gem_diamond_b", 1),
                        ("gem_block_b", 1), ("gem_diamond_fire_c", 1),
                    ],
                }
                replacements = {
                    "CLASS_STRENGTH_CLOAK": class_item(_DTU_CLASS_STRENGTH_CLOAKS),
                    "CLASS_UNDEFEATABLE_CLOAK": class_item(_DTU_CLASS_UNDEFEATABLE_CLOAKS),
                    "CLASS_SET_WEAPONMOD": class_item(_DTU_CLASS_SET_WEAPONMODS),
                    "CLASS_PANDORA_AMULET": class_item(_DTU_CLASS_PANDORA_AMULETS),
                    "CLASS_GLOVES": f"{class_name}_basic_gloves_02",
                    "CLASS_SHOULDERS": f"{class_name}_basic_shoulders_02",
                    "CLASS_HELMET": f"{class_name}_basic_helmet_02",
                    "CLASS_LONG_WEAPON": {
                        "ranger": "ranger_basic_2h_bow_02",
                        "warrior": "warrior_basic_2h_sword_02",
                        "mage": "mage_basic_2h_staff_02",
                        "dwarf": "dwarf_basic_2h_rifle_02",
                    }.get(class_name),
                }
                specifications = [
                    (replacements.get(template_id, template_id), amount)
                    for template_id, amount in silver[suffix]
                    if not template_id.startswith("CLASS_")
                    or replacements.get(template_id)
                ]
            else:  # Heroic / Gold Chest
                heroic = {
                    "01": [
                        ("gilded_clover", 20),
                        ("gem_diamond_andermagic_b", 1),
                        ("gem_diamond_lightning_c", 1),
                        ("gem_diamond_ice_b", 1),
                        ("gem_faster_run_b", 1),
                        ("CLASS_GLOVES", 1),
                        ("deluxe_teaser_two_weeks", 1),
                        ("gem_diamond_ice_b", 1),
                        ("CLASS_SHOULDERS", 1),
                        ("gem_diamond_h", 1),
                        ("gem_diamond_andermagic_b", 1),
                    ],
                    "02": [
                        ("gem_diamond_b", 3), ("gem_block_h", 1),
                        ("gem_diamond_lightning_b", 1),
                        ("gem_block_i", 1), ("gem_attackspeed_c", 1),
                        ("gem_ruby_b", 1),
                        ("CLASS_PANDORA_AMULET", 1),
                        ("CLASS_LONG_WEAPON", 1),
                    ],
                    "03": [
                        ("gem_cyanit_b", 2), ("gem_attackspeed_h", 1),
                        ("CLASS_SHIELD", 1), ("gem_diamond_fire_b", 1),
                        ("gem_block_c", 1), ("e_ide_02_crafting_gem", 1),
                        ("gem_diamond_b", 1),
                        ("gem_diamond_andermagic_b", 1),
                    ],
                    "04": [
                        ("gem_diamond_h", 1),
                        ("CLASS_SET_WEAPONMOD", 1),
                        ("gem_diamond_ice_b", 2),
                        ("CLASS_STRENGTH_CLOAK", 1),
                        ("gem_diamond_andermagic_b", 1),
                        ("unique_gem_crafting", 1), ("gem_block_b", 1),
                        ("gem_diamond_poison_c", 1),
                        ("CLASS_UNDEFEATABLE_CLOAK", 1),
                        ("gem_diamond_fire_b", 1),
                    ],
                }
                replacements = {
                    "CLASS_SET_WEAPONMOD": class_item(_DTU_CLASS_SET_WEAPONMODS),
                    "CLASS_PANDORA_AMULET": class_item(_DTU_CLASS_PANDORA_AMULETS),
                    "CLASS_STRENGTH_CLOAK": class_item(_DTU_CLASS_STRENGTH_CLOAKS),
                    "CLASS_UNDEFEATABLE_CLOAK": class_item(_DTU_CLASS_UNDEFEATABLE_CLOAKS),
                    "CLASS_GLOVES": f"{class_name}_basic_gloves_02",
                    "CLASS_SHOULDERS": f"{class_name}_basic_shoulders_02",
                    "CLASS_LONG_WEAPON": {
                        "ranger": "ranger_basic_rh_longbow_03",
                        "warrior": "warrior_basic_2h_sword_02",
                        "mage": "mage_basic_2h_staff_02",
                        "dwarf": "dwarf_basic_2h_rifle_02",
                    }.get(class_name),
                    "CLASS_SHIELD": {
                        "ranger": "ranger_basic_lh_defensequiver_02",
                        "warrior": "warrior_basic_lh_shield_02",
                        "mage": "mage_basic_lh_book_02",
                        "dwarf": "dwarf_basic_lh_absorber_02",
                    }.get(class_name),
                }
                specifications = [
                    (replacements.get(template_id, template_id), amount)
                    for template_id, amount in heroic[suffix]
                    if not template_id.startswith("CLASS_")
                    or replacements.get(template_id)
                ]
            _log(
                f"loaded fixed DTU superchest {loot_list_id} "
                f"rewards for {instance_id}: {tuple(specifications)}"
            )
        elif is_a_mighty_sentinel_chest:
            try:
                difficulty = int(getattr(
                    s, "gameplay_map_difficulty", 0,
                ) or getattr(
                    getattr(s, "gameplay_map_event_runtime", None),
                    "scaling_difficulty", 0,
                ) or 0)
            except (TypeError, ValueError):
                difficulty = 0
            key_count = 1 + max(0, min(9, difficulty)) // 2
            specifications = [("scaling_dungeon_boss_key_01", key_count)]
        elif loot_list_id in {"chest_umbra_stone_01", "chest_umbra_stone_02"}:
            specifications = [(
                "ingredient_elemental_essence_crafting_light", 5,
            )]
        elif loot_list_id == "chest_pw199_demon_herb":
            # Canol Alley's eight one-shot herb props feed a recipe that needs
            # ten demon herbs. The client export has the loot-list reference
            # but omits its server-side contents, so award two per prop.
            specifications = [("demon_herb", 2)]
        elif is_bgh_mythic_chest:
            rng = random.SystemRandom()
            try:
                tier_value = definition.variation_difficulty
                if tier_value is None:
                    with s.flow_lock:
                        tier_value = max(
                            int(getattr(s, "gameplay_map_difficulty", 0) or 0),
                            int(getattr(s, "gameplay_scaling_difficulty", 0) or 0),
                        )
                tier = max(0, min(9, int(tier_value)))
                with s.flow_lock:
                    character = s.gameplay_character
                    player_level = max(
                        1, int(getattr(character, "level", 1) or 1),
                    )
                    class_name = CLASS_NAMES[
                        int(getattr(character, "class_id", 0))
                    ].lower()
                    access_id = str(
                        getattr(s, "gameplay_map_access_id", "") or ""
                    ) or str(getattr(s, "gameplay_map", "") or "")

                # Each category becomes a separate world-loot object. The
                # shared chest placement helper spreads every object around
                # the opener, so the guaranteed reward types do not overlap.
                specifications = [
                    (
                        "money_vc_gold",
                        max(1, round(
                            player_level * rng.uniform(2.8, 4.0)
                            * (tier + 1) / 10.0
                        )),
                    ),
                    ("money_rc", 10 * (tier + 1)),
                ]
                gems = roll_possible_reward_gems(
                    self.static_database_path, access_id, 1, rng,
                )
                if not gems:
                    # BGH map access records may omit the retail Possible
                    # Rewards gem descriptor. Keep the chest's gem reward
                    # guaranteed and choose from the rank band authored for
                    # the selected difficulty.
                    from dso_server.protocol.world_loot_protocol import (
                        BOSS_GEM_RANKS_BY_DIFFICULTY,
                    )

                    loot_cache = prime_world_loot_cache(
                        self.static_database_path,
                    )
                    gem_ranks = BOSS_GEM_RANKS_BY_DIFFICULTY[tier]
                    gem_candidates = tuple(
                        definition.template_id
                        for rank in gem_ranks
                        for definition in loot_cache.standard_gems_by_rank.get(
                            rank, (),
                        )
                    )
                    if gem_candidates:
                        gems = (rng.choice(gem_candidates),)
                specifications.extend((template_id, 1) for template_id in gems)

                from dso_server.protocol.chest_loot_protocol import (
                    roll_chest_contents,
                )
                from dso_server.protocol.world_loot_protocol import (
                    EQUIPMENT_RARITIES,
                )

                difficulty_weights = (
                    prime_world_loot_cache(self.static_database_path)
                    .rarity_weights_by_difficulty.get(tier, (0, 0, 1, 0, 0))
                )
                rarity_indices = (2, 3, 4)
                rarity_weights = tuple(
                    difficulty_weights[index] for index in rarity_indices
                )
                if not any(rarity_weights):
                    rarity_weights = (0, 0, 1)
                rarity_index = rng.choices(
                    rarity_indices, weights=rarity_weights, k=1,
                )[0]
                rarity = EQUIPMENT_RARITIES[rarity_index].casefold()
                equipment = roll_chest_contents(
                    self.static_database_path,
                    f"all_class_all_equip_{rarity}_01",
                    player_level=player_level,
                    class_name=class_name,
                    rng=rng,
                )
                for reward in equipment.rewards:
                    template_id = reward.definition.template_id
                    specifications.append((template_id, int(reward.amount)))
                    equipment_rewards_by_template.setdefault(
                        template_id, [],
                    ).append(reward)

                entries = load_prop_loot_catalog().entries(loot_list_id)
                if entries:
                    specifications.extend(
                        self._roll_map_chest_loot(s, entries)
                    )
            except (OSError, ValueError, sqlite3.Error) as exc:
                _log(
                    f"cannot roll Big Game Hunt Mythic Chest: "
                    f"{type(exc).__name__}: {exc}"
                )
                specifications = []
        elif loot_list_id == _BLOOD_CHEST_LOOT_LIST:
            specifications = [
                ("money_rc", 10),
            ]
            gem_count = random.SystemRandom().randint(4, 5)
            specifications.extend(
                (random.SystemRandom().choice(_BLOOD_CHEST_GEM_TEMPLATES), 1)
                for _ in range(gem_count)
            )
        elif loot_list_id in {
            "chest_lootlist_magic", "chest_lootlist_rare",
            "chest_lootlist_epic",
        }:
            # The item database exposes these chests' possible contents but
            # not the retail server's loot-list rows. Use the documented
            # guaranteed equipment/essence rewards, roll a gem bag, and keep
            # the tier's Andermant jackpot as a rare bonus.
            from dso_server.protocol.chest_loot_protocol import roll_chest_contents

            tier = {
                "chest_lootlist_magic": ("magic", 1_000, "ammunition_damage_common"),
                "chest_lootlist_rare": ("rare", 10_000, "ammunition_damage_rare"),
                "chest_lootlist_epic": ("epic", 100_000, "ammunition_damage_epic"),
            }[loot_list_id]
            rarity, jackpot_amount, essence_id = tier
            rng = random.SystemRandom()
            with s.flow_lock:
                character = s.gameplay_character
            class_name = CLASS_NAMES[int(getattr(character, "class_id", 0))].lower()
            equipment = roll_chest_contents(
                self.static_database_path,
                f"all_class_all_equip_{rarity}_01",
                player_level=max(1, int(getattr(character, "level", 1))),
                class_name=class_name,
                rng=rng,
            )
            specifications = [
                (reward.definition.template_id, int(reward.amount))
                for reward in equipment.rewards
            ]
            specifications.append((essence_id, 100))
            if rng.random() < 0.25:
                gem_bag = roll_chest_contents(
                    self.static_database_path,
                    "lootbox_gems_d",
                    player_level=max(1, int(getattr(character, "level", 1))),
                    class_name=class_name,
                    rng=rng,
                )
                specifications.extend(
                    (reward.definition.template_id, int(reward.amount))
                    for reward in gem_bag.rewards
                )
            if rng.random() < 0.25:
                specifications.append(("money_vc_gold", 1))
            # The regular return is 20% of the 50 RC-per-lockpick baseline
            # (five lockpicks cost 250 RC, so the normal return is 50 RC).
            # A jackpot replaces that regular return.
            andermant_reward = (
                jackpot_amount if rng.random() < 0.01
                else round(0.20 * 50 * max(0, int(definition.key_amount)))
            )
            if andermant_reward:
                specifications.append(("money_rc", andermant_reward))
        else:
            dragan_elemental_chest_loot_lists = {}
            for element in ("fire", "ice", "lightning", "poison"):
                for chest_number in (1, 2, 3):
                    dragan_elemental_chest_loot_lists[
                        f"e_dra_fortress_rewardchest_{element}_{chest_number:02d}"
                    ] = f"chest_e_dra_fortress_rewardchest_{element}"
                dragan_elemental_chest_loot_lists[
                    f"e_dra_fortress_rewardchest_{element}_04"
                ] = (
                    f"chest_e_dra_fortress_rewardchest_{element}_repeatable"
                )
            dragan_elemental_final_chests = {
                instance_id: loot_list_id
                for instance_id, loot_list_id
                in dragan_elemental_chest_loot_lists.items()
                if instance_id.endswith("_04")
            }
            dragan_chest_instance_id = str(
                definition.instance_id
            ).casefold()
            dragan_elemental_chest_loot_list = (
                dragan_elemental_chest_loot_lists.get(
                    dragan_chest_instance_id
                )
            )
            is_dragan_elemental_relic_chest = (
                map_key(getattr(s, "gameplay_map", ""))
                == "e_dra_03_dun_01"
                and dragan_elemental_chest_loot_list is not None
                and loot_list_id == dragan_elemental_chest_loot_list
            )
            is_dragan_elemental_reward_chest = (
                is_dragan_elemental_relic_chest
                and dragan_chest_instance_id.endswith("_04")
            )
            catalog_loot_list_id = (
                "CHEST_e_dra_fortress_rewardchest_repeatable"
                if is_dragan_elemental_reward_chest
                else (
                    "CHEST_e_dra_fortress_rewardchest_elemental_quest"
                    if is_dragan_elemental_relic_chest else loot_list_id
                )
            )
            try:
                entries = load_prop_loot_catalog().entries(
                    catalog_loot_list_id
                )
            except (OSError, ValueError) as exc:
                _log(f"prop loot catalog unavailable: {type(exc).__name__}: {exc}")
                entries = None
            if entries is None:
                _log(
                    f"no local loot table for chest {definition.instance_id!r} "
                    f"({loot_list_id!r}); no rewards spawned"
                )
                specifications = []
            else:
                specifications = list(self._roll_map_chest_loot(s, entries))
                if loot_list_id == "lootlist_e_nye_worldboss_pile":
                    rng = random.SystemRandom()
                    with s.flow_lock:
                        tier = max(0, min(9, int(
                            getattr(s, "gameplay_map_difficulty", 0) or 0
                        )))
                        access_id = str(
                            getattr(s, "gameplay_map_access_id", "") or ""
                        ) or str(getattr(s, "gameplay_map", "") or "")

                    from dso_server.protocol.world_loot_protocol import (
                        BOSS_GEM_RANKS_BY_DIFFICULTY,
                        EQUIPMENT_RARITIES,
                        MYSTIC_CUBE_TEMPLATES_BY_DIFFICULTY,
                    )

                    # Premium/Deluxe membership earns two or three cubes;
                    # the cube rarity follows the map difficulty ladder.
                    from dso_server.services.loot_presence import (
                        mystic_cube_drop_is_eligible,
                    )

                    if mystic_cube_drop_is_eligible(s):
                        cube = MYSTIC_CUBE_TEMPLATES_BY_DIFFICULTY[tier]
                        specifications.extend(
                            (cube, 1) for _ in range(rng.randint(2, 3))
                        )

                    # Roll two or three gems from this access's authored gem
                    # band, with the boss difficulty band as a fallback for
                    # event access records that omit Possible Rewards gems.
                    gem_count = rng.randint(2, 3)
                    gems = roll_possible_reward_gems(
                        self.static_database_path, access_id, gem_count, rng,
                    )
                    if len(gems) < gem_count:
                        loot_cache = prime_world_loot_cache(
                            self.static_database_path,
                        )
                        gem_candidates = tuple(
                            item.template_id
                            for rank in BOSS_GEM_RANKS_BY_DIFFICULTY[tier]
                            for item in loot_cache.standard_gems_by_rank.get(
                                rank, (),
                            )
                        )
                        if gem_candidates:
                            gems = (
                                *gems,
                                *(
                                    rng.choice(gem_candidates)
                                    for _ in range(gem_count - len(gems))
                                ),
                            )
                    specifications.extend((template_id, 1) for template_id in gems)

                    # Three items on lower tiers, four from Infernal, and
                    # five from Bloodshed. Guarantee a randomized Magic/Rare/
                    # Legendary mix, then randomize any extra items as well.
                    item_count = 3 + min(2, tier // 4)
                    rarity_indices = (2, 3, 4)
                    rarity_rolls = list(rarity_indices)
                    rng.shuffle(rarity_rolls)
                    rarity_rolls.extend(
                        rng.choice(rarity_indices)
                        for _ in range(item_count - len(rarity_rolls))
                    )
                    from dso_server.protocol.chest_loot_protocol import (
                        roll_chest_contents,
                    )

                    for rarity_index in rarity_rolls:
                        rarity = EQUIPMENT_RARITIES[rarity_index].casefold()
                        equipment = roll_chest_contents(
                            self.static_database_path,
                            f"all_class_all_equip_{rarity}_01",
                            player_level=player_level,
                            class_name=class_name,
                            rng=rng,
                        )
                        for reward in equipment.rewards:
                            template_id = reward.definition.template_id
                            specifications.append((
                                template_id, int(reward.amount),
                            ))
                            equipment_rewards_by_template.setdefault(
                                template_id, [],
                            ).append(reward)

                    # Roll each mercenary core independently, one at a time.
                    # More than one core may drop from a single Rocket Pile.
                    for core in (
                        "item_ingredient_mercenary_necklace_core",
                        "item_ingredient_mercenary_cloak_core",
                        "item_ingredient_mercenary_belt_core",
                    ):
                        if rng.random() < 0.05:
                            specifications.append((core, 1))

                    # One random Soul Destruction mercenary recipe at 20%.
                    if rng.random() < 0.20:
                        specifications.append((rng.choice((
                            "recipe_mercenary_soul_destruction_belt",
                            "recipe_mercenary_soul_destruction_cloak",
                            "recipe_mercenary_soul_destruction_necklace",
                        )), 1))
                    _log(
                        "rolled New Year Worldboss pile bonus rewards: "
                        f"difficulty={tier}, specifications="
                        f"{tuple(specifications)!r}"
                    )
                is_cny_nian_reward_chest = (
                    loot_list_id == "cny_boss_nian_box_reward"
                    and map_key(getattr(s, "gameplay_map", ""))
                    == "e_cny_boss_02"
                )
                if is_cny_nian_reward_chest:
                    from dso_server.protocol.chest_loot_protocol import (
                        roll_chest_contents,
                    )
                    from dso_server.protocol.world_loot_protocol import (
                        EQUIPMENT_RARITIES,
                    )

                    rng = random.SystemRandom()
                    with s.flow_lock:
                        tier_value = max(
                            int(definition.variation_difficulty or 0),
                            int(getattr(s, "gameplay_map_difficulty", 0) or 0),
                            int(getattr(s, "gameplay_scaling_difficulty", 0) or 0),
                        )
                    tier = max(0, min(9, int(tier_value)))
                    rarity_weights = (
                        prime_world_loot_cache(self.static_database_path)
                        .rarity_weights_by_difficulty.get(
                            tier, (0, 25, 25, 25, 25),
                        )
                    )
                    # Keep the event chest's equipment above Common. Its
                    # rarity rises with the selected difficulty's authored
                    # Uncommon/Magic/Rare/Epic weights (Epic is the client's
                    # Legendary tier in this build).
                    rarity_indices = (1, 2, 3, 4)
                    rarity_roll_weights = tuple(
                        rarity_weights[index] for index in rarity_indices
                    )
                    if not any(rarity_roll_weights):
                        rarity_roll_weights = (1, 1, 1, 1)
                    rarity_boxes = tuple(
                        f"all_class_all_equip_{EQUIPMENT_RARITIES[index].casefold()}_01"
                        for index in rarity_indices
                    )
                    for _ in range(rng.randint(2, 4)):
                        rarity_box = rng.choices(
                            rarity_boxes,
                            weights=rarity_roll_weights,
                            k=1,
                        )[0]
                        equipment = roll_chest_contents(
                            self.static_database_path,
                            rarity_box,
                            player_level=player_level,
                            class_name=class_name,
                            rng=rng,
                        )
                        for reward in equipment.rewards:
                            template_id = reward.definition.template_id
                            specifications.append(
                                (template_id, int(reward.amount))
                            )
                            equipment_rewards_by_template.setdefault(
                                template_id, [],
                            ).append(reward)

                if (
                    is_dragan_elemental_relic_chest
                    and not is_dragan_elemental_reward_chest
                ):
                    from dso_server.protocol.chest_loot_protocol import (
                        roll_chest_contents,
                    )

                    rng = random.SystemRandom()
                    # The three quest-gated chests in each elemental area
                    # share the 2-3 equipment reward. The repeatable final
                    # chest has its own smaller gem/Pearl table. Cap equipment
                    # at Epic (the client's Legendary tier).
                    rarity_boxes = (
                        "all_class_all_equip_uncommon_01",
                        "all_class_all_equip_magic_01",
                        "all_class_all_equip_rare_01",
                        "all_class_all_equip_epic_01",
                    )
                    for _ in range(rng.randint(2, 3)):
                        equipment = roll_chest_contents(
                            self.static_database_path,
                            rng.choice(rarity_boxes),
                            player_level=player_level,
                            class_name=class_name,
                            rng=rng,
                        )
                        for reward in equipment.rewards:
                            template_id = reward.definition.template_id
                            specifications.append(
                                (template_id, int(reward.amount))
                            )
                            equipment_rewards_by_template.setdefault(
                                template_id, [],
                            ).append(reward)

                    if is_dragan_elemental_reward_chest:
                        # Each of the four repeatable final Castle chests gives
                        # each special prize a 10% roll. Any unclaimed prize is
                        # guaranteed on the fourth distinct final chest.
                        gate_map_id = map_key(getattr(s, "gameplay_map", ""))
                        chest_key = (
                            f"{gate_map_id}:{dragan_chest_instance_id}"
                        )
                        valid_chests = {
                            f"e_dra_03_dun_01:{instance_id}"
                            for instance_id in dragan_elemental_final_chests
                        }
                        event_catalog = getattr(
                            s, "gameplay_event_catalog", None
                        )
                        dragan_event = next((
                            event for event in (
                                event_catalog.active_events()
                                if event_catalog is not None else ()
                            )
                            if event.template.resource_id.casefold()
                            == "dragan_event_03"
                        ), None)
                        if chest_key in valid_chests and dragan_event is not None:
                            with s.flow_lock:
                                progress = copy.deepcopy(
                                    getattr(
                                        s, "gameplay_event_progress", {}
                                    ) or {}
                                )
                            claims_by_event = dict(
                                progress.get(
                                    "dragan_gate_chest_claims", {}
                                ) or {}
                            )
                            event_key = str(int(dragan_event.event_id))
                            state = dict(
                                claims_by_event.get(event_key, {}) or {}
                            )
                            opened = set(
                                str(value)
                                for value in state.get("opened", ())
                            )
                            claimed = set(
                                str(value)
                                for value in state.get("claimed", ())
                            )
                            prize_items = (
                                ("premium_teaser_one_week_consumable_7d", 1),
                                ("money_rc", 5000),
                                ("melt_tool_90d", 1),
                            )
                            # The four final chests are repeatable. Track which
                            # distinct chests the player has opened so any
                            # still-unclaimed special prize is guaranteed on
                            # the fourth distinct chest, while common rewards
                            # and the normal per-opening chance keep working
                            # on repeat visits to the same chest.
                            opened.add(chest_key)
                            for template_id, amount in prize_items:
                                if (
                                    template_id not in claimed
                                    and (len(opened) >= len(valid_chests)
                                         or rng.random() < 0.10)
                                ):
                                    specifications.append(
                                        (template_id, amount)
                                    )
                                    claimed.add(template_id)
                            claims_by_event[event_key] = {
                                "opened": sorted(opened),
                                "claimed": sorted(claimed),
                            }
                            progress["dragan_gate_chest_claims"] = (
                                claims_by_event
                            )
                            dragan_gate_claim_update = progress
        if (
            is_parallel_world_boss_chest
            and parallel_world_jewel_chance is not None
        ):
            jewel_rng = random.SystemRandom()
            if jewel_rng.random() < float(parallel_world_jewel_chance):
                candidates = _parallel_world_reward_jewel_candidates(
                    class_name
                )
                if candidates:
                    specifications.append(
                        (jewel_rng.choice(candidates), 1)
                    )
        if not specifications:
            _log(f"chest {definition.instance_id!r} rolled no rewards")
            return 0
        # Retail drops a chest's loot around the clicking player, not the
        # chest (see gameplay/prop_loot.py).
        with s.flow_lock:
            encounter = s.gameplay_monster_encounter
            last_move = s.gameplay_last_move
            character = s.gameplay_character
            socket_evaluation = s.gameplay_socket_effect_evaluation
        is_new_year_worldboss_pile = (
            map_key(getattr(s, "gameplay_map", "")) == "e_nye_dungeon_01"
            and loot_list_id == "lootlist_e_nye_worldboss_pile"
        )
        player_level = max(1, int(getattr(character, "level", 1)))
        equipment_difficulty = int(definition.variation_difficulty or 0)
        if is_new_year_worldboss_pile:
            equipment_difficulty = max(
                equipment_difficulty,
                int(getattr(s, "gameplay_map_difficulty", 0) or 0),
            )
        origin = tuple(float(value) for value in definition.position)
        if encounter is not None and last_move is not None:
            origin = encounter.coordinate_frame.wire_to_world(
                last_move.relative_position
            )
        positions = chest_drop_positions(
            origin,
            len(specifications),
            spacing=(
                0.9
                if is_bgh_mythic_chest or is_new_year_worldboss_pile
                else CHEST_DROP_SPACING
            ),
        )
        try:
            item_rows = prime_world_loot_cache(self.static_database_path).items_by_id
            drops = []
            for (template_id, amount), position in zip(specifications, positions):
                if socket_evaluation is not None:
                    absolute, relative = socket_evaluation.drop_amount_bonus(
                        template_id
                    )
                    amount = max(
                        1,
                        int(round(
                            (int(amount) + absolute)
                            * max(0.0, 1.0 + relative)
                        )),
                    )
                item_definition = load_purchase_item_definition(
                    self.static_database_path, template_id,
                )
                is_bgh_old_glory_ring = (
                    is_bgh_mythic_chest
                    and template_id.casefold()
                    in _BGH_OLD_GLORY_RING_TEMPLATES
                )
                item_level = (
                    100 if is_bgh_old_glory_ring else player_level
                )
                item_level = min(
                    max(
                        int(item_definition.minimum_item_level),
                        int(item_level),
                    ),
                    max(1, int(item_definition.maximum_item_level)),
                )
                item = _new_item(
                    item_definition, 0, amount, item_level,
                )
                if item_definition.slot_types:
                    item.item_level = item_level
                item.ground_position = position
                reward_queue = equipment_rewards_by_template.get(template_id)
                if reward_queue:
                    reward = reward_queue.pop(0)
                    item.item_level = item_level
                    if reward.rarity is not None:
                        item.rarity = int(reward.rarity)
                    if reward.appearance:
                        item.appearance = str(reward.appearance)
                    item_row = item_rows.get(template_id, {})
                    socket_count = _threshold_value(
                        str(item_row.get("MinSocketCount") or "0"),
                        item.item_level,
                    )
                    item.socket_count = socket_count
                    item.socketed_templates = [""] * socket_count
                    item.needs_identification = bool(
                        int(item.rarity) > 0
                        and item_row.get("NeedsIdentify", False)
                    )
                    reward_inventory = InventoryInfo(items=[item])
                    roll_new_equipment_enchantments(
                        self.static_database_path,
                        reward_inventory,
                        (int(item.item_id),),
                        class_name=class_name,
                        map_difficulty=equipment_difficulty,
                        rng=random.SystemRandom(),
                    )
                    refresh_inventory_economy_values(
                        self.static_database_path,
                        reward_inventory,
                        item_ids=(int(item.item_id),),
                        force_melt_value=True,
                    )
                    item = reward_inventory.items[0]
                    item.ground_position = position
                # The Key of Prowess is authored as AutoPickUp=Privileged in
                # static.db4, which the server's proximity collector does not
                # treat as an unconditional pickup. A Mighty Threat chest keys
                # should go straight into the bag once the chest is opened.
                auto_pick_up = (
                    "All"
                    if template_id.casefold() == "scaling_dungeon_boss_key_01"
                    else str(
                        item_rows.get(template_id, {}).get("AutoPickUp")
                        or (
                            "All"
                            if item_definition.item_category == "Currency"
                            else "None"
                        )
                    )
                )
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
        with s.gameplay_world_loot_position_lock:
            if is_bgh_mythic_chest or is_new_year_worldboss_pile:
                drops = list(
                    CharacterService._separate_world_loot_positions(
                        s, drops,
                        minimum_spacing=(
                            0.9 if is_new_year_worldboss_pile else None
                        ),
                    )
                )
            with self.gameplay_world_lock:
                counters = getattr(self, "gameplay_world_next_item_ids", None)
                if counters is None:
                    counters = self.gameplay_world_next_item_ids = {}
                with s.flow_lock:
                    first = max(
                        int(getattr(
                            s, "gameplay_next_item_id", RUNTIME_ITEM_ID_BASE,
                        )),
                        int(counters.get(instance_key, RUNTIME_ITEM_ID_BASE)),
                    )
                end = first + len(drops)
                counters[instance_key] = end
                counters[map_id] = max(
                    end, int(counters.get(map_id, RUNTIME_ITEM_ID_BASE)),
                )
                with s.flow_lock:
                    for offset, drop in enumerate(drops):
                        item_id = first + offset
                        drop.item.item_id = item_id
                        drop.item.field_18 = item_id & 0xFFFF
                        s.gameplay_world_loot[item_id] = drop
                    s.gameplay_next_item_id = max(
                        int(getattr(
                            s, "gameplay_next_item_id", RUNTIME_ITEM_ID_BASE,
                        )),
                        end,
                    )
        CharacterService._reconcile_world_loot_visibility(
            s, label="map-chest-loot",
        )
        if dragan_gate_claim_update is not None:
            with s.flow_lock:
                current_progress = copy.deepcopy(
                    getattr(s, "gameplay_event_progress", {}) or {}
                )
                current_progress["dragan_gate_chest_claims"] = (
                    dragan_gate_claim_update.get("dragan_gate_chest_claims", {})
                )
                s.gameplay_event_progress = current_progress
            CharacterService._request_account_save(
                s, "dragan gate chest rewards claimed",
            )
        # Chest drops are placed around the player. Run the server-side
        # proximity pickup immediately so AutoPickUp=All rewards (including
        # this chest's key stack) are collected without requiring another
        # movement packet from the client.
        if last_move is not None:
            CharacterService._pickup_nearby_auto_loot(
                s, last_move.relative_position,
            )
        _log(
            f"spawned {len(drops)} reward item(s) from "
            f"{definition.instance_id!r} for player session"
        )
        if dtu_event_progress_reward:
            with s.flow_lock:
                progress_callback = getattr(
                    s, "gameplay_event_progress_callback", None,
                )
            if callable(progress_callback):
                try:
                    progressed = progress_callback(
                        s,
                        collectible_id="e_bos_progress_item_t0",
                        amount=dtu_event_progress_reward,
                    )
                except Exception as exc:
                    _log(
                        f"cannot grant DTU event progress from "
                        f"{definition.instance_id!r}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                else:
                    if not progressed:
                        _log(
                            f"DTU chest progress collectible was not accepted "
                            f"for {definition.instance_id!r} "
                            f"(amount={dtu_event_progress_reward})"
                        )
            else:
                _log(
                    f"DTU chest progress callback unavailable for "
                    f"{definition.instance_id!r}"
                )
        return len(drops)

    def _roll_map_chest_loot(self, s: Session, entries) -> tuple[tuple[str, int], ...]:
        """Roll a catalog chest list for the clicking player.

        Event progress entries drop the page item this player is collecting
        for each event whose progress applies on the map, the same scope
        monster kills use.
        """

        with s.flow_lock:
            character = s.gameplay_character
            map_id = str(s.gameplay_map or "")
            access_id = str(getattr(s, "gameplay_map_access_id", None) or "")
            event_map_id = str(
                getattr(s, "gameplay_map_access_group_id", None)
                or access_id or map_id
            )
            participation = dict(s.gameplay_event_participation)
            progress = dict(getattr(s, "gameplay_event_progress", None) or {})
            completed_achievements = completed_achievement_ids(
                getattr(s, "gameplay_achievements", None)
            )
            event_catalog = s.gameplay_event_catalog
            active_costume = str(
                getattr(s, "gameplay_active_costume", "") or ""
            )
            class_name = (
                CLASS_NAMES.get(int(character.class_id), "").casefold()
                if character is not None else ""
            )
        if event_catalog is None:
            event_catalog = CharacterService._event_catalog(self)
        player_level = max(1, int(getattr(character, "level", 1) or 1))
        collectibles = event_catalog.map_event_collectibles(
            map_id=event_map_id,
            map_aliases=(map_id, access_id),
            player_level=player_level,
            participation=participation,
            progress=progress,
            completed_achievements=completed_achievements,
        )
        map_difficulty = max(
            0, int(getattr(s, "gameplay_map_difficulty", 0) or 0)
        )
        difficulty_drop_factors = prime_world_loot_cache(
            self.static_database_path
        ).difficulty_drop_factors.get(map_difficulty, (1.0, 1.0))
        return roll_prop_loot(
            entries,
            player_level=player_level,
            rng=random.SystemRandom(),
            event_collectibles=collectibles,
            class_name=class_name,
            map_id=map_id,
            map_aliases=(event_map_id, access_id),
            map_difficulty=map_difficulty,
            active_costume=active_costume,
            privileged_difficulty_multiplier=(
                difficulty_drop_factors[0] * difficulty_drop_factors[1]
            ),
        )

    @staticmethod
    def _prop_keys_missing(s: Session, definition) -> int:
        """How many of the prop's KeyId the player lacks for one use."""

        if not definition.spends_keys:
            return 0
        with s.flow_lock:
            inventory = s.gameplay_inventory
            carried = sum(
                int(item.amount) for item in getattr(inventory, "items", ())
                if item.template_id == definition.key_id
            )
        return max(0, int(definition.key_amount) - carried)

    @staticmethod
    def _spend_prop_keys(
        s: Session, definition, *, claim_key: str = "",
    ) -> bool:
        """Take KeyAmount x KeyId for one use and publish the bag change.

        Retail kept the key stack's item id and sent the reduced stack
        (e_sar_chest_key 302 -> 301, same id) before the chest's loot.
        """

        with s.flow_lock:
            purchases = dict(
                getattr(s, "gameplay_timed_offer_purchases", {})
            )
            if claim_key and purchases.get(claim_key):
                _log(
                    f"refused already-claimed DTU superchest "
                    f"{definition.instance_id!r} ({claim_key})"
                )
                return False
            inventory = s.gameplay_inventory
            if inventory is None:
                return False
            builder = InventoryMutationBuilder(inventory, s.gameplay_next_item_id)
            try:
                builder.consume_template(
                    definition.key_id, int(definition.key_amount),
                    preserve_item_ids=True,
                )
            except ValueError as exc:
                _log(f"cannot spend keys for {definition.instance_id!r}: {exc}")
                return False
            mutation = builder.finish()
            s.gameplay_inventory = mutation.inventory
            s.gameplay_next_item_id = max(
                int(s.gameplay_next_item_id), int(mutation.next_item_id),
            )
            if claim_key:
                purchases[claim_key] = 1
                s.gameplay_timed_offer_purchases = purchases
        packet, packet_bits = encode_server_command_batch(tuple(
            ServerCommand(INVENTORY_INFO_COMMAND, record, LOCAL_PLAYER_ACTOR_ID)
            for record in (mutation.delta, mutation.final)
        ), include_terminator=True)
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label="prop-key-inventory",
        )
        CharacterService._request_account_save(
            s,
            "DTU superchest claimed" if claim_key else "chest key spent",
            flush=bool(claim_key),
        )
        return True

    def _use_gatherable_multitool(self, s: Session) -> bool:
        """Exchange one Multitool for a Broken Multitool on a resource node."""

        try:
            broken_definition = load_purchase_item_definition(
                self.static_database_path,
                GATHERABLE_BROKEN_MULTITOOL_TEMPLATE,
            )
        except (AttributeError, OSError, ValueError, sqlite3.Error) as exc:
            _log(f"cannot load Broken Multitool definition: {type(exc).__name__}: {exc}")
            return False
        with s.flow_lock:
            inventory = s.gameplay_inventory
            if inventory is None:
                return False
            builder = InventoryMutationBuilder(inventory, s.gameplay_next_item_id)
            try:
                builder.consume_template(
                    GATHERABLE_MULTITOOL_TEMPLATE, 1, preserve_item_ids=True,
                )
                builder.add_item(broken_definition, 1, item_level=1)
            except ValueError as exc:
                _log(f"cannot exchange gatherable Multitool: {exc}")
                return False
            mutation = builder.finish()
            s.gameplay_inventory = mutation.inventory
            s.gameplay_next_item_id = max(
                int(s.gameplay_next_item_id), int(mutation.next_item_id),
            )
        packet, packet_bits = encode_server_command_batch(tuple(
            ServerCommand(INVENTORY_INFO_COMMAND, record, LOCAL_PLAYER_ACTOR_ID)
            for record in (mutation.delta, mutation.final)
        ), include_terminator=True)
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label="gatherable-multitool-inventory",
        )
        CharacterService._request_account_save(s, "gatherable Multitool used")
        return True

    def _spawn_gatherable_drop(
        self, s: Session, definition, template_id: str, amount: int = 1,
    ) -> bool:
        """Spawn one authored gatherable ingredient as world loot."""

        try:
            item_definition = load_purchase_item_definition(
                self.static_database_path, template_id,
            )
            if not item_definition.ingredient_bag:
                _log(f"gatherable template {template_id!r} is not an Ingredient Bag item")
                return False
            with s.flow_lock:
                character = s.gameplay_character
                encounter = s.gameplay_monster_encounter
                last_move = s.gameplay_last_move
                origin = tuple(float(value) for value in definition.position)
                if encounter is not None and last_move is not None:
                    origin = encounter.coordinate_frame.wire_to_world(
                        last_move.relative_position
                    )
                level = max(1, int(getattr(character, "level", 1) or 1))
            position = chest_drop_positions(origin, 1)[0]
            item = _new_item(
                item_definition, 0, max(1, min(10, int(amount))), level,
            )
            item.ground_position = position
            # Show the spawned material briefly, then let the normal automatic
            # pickup path collect it into its Ingredient Bag destination.
            drop = WorldLootDrop(item, item_definition.item_category, "All")
        except (OSError, ValueError, sqlite3.Error, TypeError) as exc:
            _log(f"cannot build gatherable drop {template_id!r}: {type(exc).__name__}: {exc}")
            return False

        instance_key = CharacterService._gameplay_instance_key(s)
        map_id = str(s.gameplay_map or "")
        with self.gameplay_world_lock:
            counters = getattr(self, "gameplay_world_next_item_ids", None)
            if counters is None:
                counters = self.gameplay_world_next_item_ids = {}
            with s.flow_lock:
                item_id = max(
                    int(getattr(s, "gameplay_next_item_id", RUNTIME_ITEM_ID_BASE)),
                    int(counters.get(instance_key, RUNTIME_ITEM_ID_BASE)),
                )
                counters[instance_key] = item_id + 1
                counters[map_id] = max(item_id + 1, int(counters.get(map_id, RUNTIME_ITEM_ID_BASE)))
                item.item_id = item_id
                item.field_18 = item_id & 0xFFFF
                s.gameplay_world_loot[item_id] = drop
                s.gameplay_next_item_id = max(
                    int(getattr(s, "gameplay_next_item_id", RUNTIME_ITEM_ID_BASE)),
                    item_id + 1,
                )
        CharacterService._reconcile_world_loot_visibility(
            s, label="gatherable-resource-drop",
        )
        with s.flow_lock:
            generation = int(s.gameplay_chase_generation)
        try:
            timer = s.gameplay_deadline_timer(
                GATHERABLE_AUTO_PICKUP_DELAY_SECONDS,
                self._auto_pickup_gatherable_drop,
                args=(s, item_id, generation),
            )
            timer.daemon = True
            timer.start()
        except (AttributeError, RuntimeError) as exc:
            _log(f"cannot schedule gatherable auto-pickup: {type(exc).__name__}: {exc}")
        _log(f"spawned gatherable ingredient {template_id!r} as ground loot")
        return True

    @staticmethod
    def _auto_pickup_gatherable_drop(
        s: Session, item_id: int, generation: int,
    ) -> None:
        """Run delayed pickup only while the same drop and map are active."""

        with s.flow_lock:
            if (
                int(s.gameplay_chase_generation) != int(generation)
                or int(item_id) not in s.gameplay_world_loot
            ):
                return
        # This delayed callback belongs to one gatherable drop the player
        # just created by interacting with the node. Pick up that exact item
        # through the normal inventory flow after it has been visible briefly;
        # relying on a later movement scan can strand it if the player does
        # not move again after the prop interaction.
        CharacterService._pickup_world_item(s, int(item_id), automatic=True)

    @staticmethod
    def _restore_prop_for_viewer(s: Session, definition, *, label: str) -> None:
        """Put a prop the client already opened back to its active state."""

        update = PropUpdate(
            prop_id=definition.prop_id,
            state=PROP_STATE_ACTIVE,
            actor_id=LOCAL_PLAYER_ACTOR_ID,
            counter=_session_game_tick(s),
            state_changed=True,
            operation=WIRE_PROP_OPERATION,
            task_index=0,
            keep_visual_state=False,
        )
        packet, packet_bits = encode_prop_update_batch((update,), actor_id=0)
        s.send_reliable_ordered((packet, packet_bits), track=True, label=label)
        if definition.instance_guid == ZERO_GUID:
            _send_prop_info(
                s, (PropRuntimeState(definition, PROP_STATE_ACTIVE),),
                label=f"{label}-info",
            )

    @staticmethod
    def _show_used_prop_for_viewer(s: Session, definition, *, label: str) -> None:
        """Refresh a per-character one-shot prop as already used."""

        update = PropUpdate(
            prop_id=definition.prop_id,
            state=PROP_STATE_USED,
            actor_id=LOCAL_PLAYER_ACTOR_ID,
            counter=_session_game_tick(s),
            state_changed=True,
            operation=WIRE_PROP_OPERATION,
            task_index=0,
            keep_visual_state=False,
        )
        packet, packet_bits = encode_prop_update_batch((update,), actor_id=0)
        s.send_reliable_ordered((packet, packet_bits), track=True, label=label)
        if definition.instance_guid == ZERO_GUID:
            _send_prop_info(
                s, (PropRuntimeState(definition, PROP_STATE_USED),),
                label=f"{label}-info",
            )

    def _schedule_easter_hub_egg_respawn(
            self, runtime, definition, *, player_id: int) -> None:
        """Reactivate a broken Kingshill Spring Egg after its cooldown."""

        if (
            map_key(runtime.map_id) != "a0200_kingscity"
            or not _is_kingshill_spring_egg(definition)
        ):
            return
        scheduler = getattr(runtime, "scheduler", None)
        if scheduler is None:
            _log("Kingshill Spring Egg respawn has no map deadline scheduler")
            return
        prop_id = int(definition.prop_id)
        individual_usage = bool(definition.individual_usage)
        timer_key = (
            (int(player_id), prop_id) if individual_usage else prop_id
        )
        with runtime.lock:
            state = runtime.props.get(prop_id)
            if individual_usage:
                if timer_key not in runtime.individual_prop_usage:
                    return
            elif state is None or state.state != PROP_STATE_USED:
                return
            timers = runtime.easter_hub_egg_respawn_timers
            if timer_key in timers:
                return
            timer = scheduler.timer(
                EASTER_HUB_EGG_RESPAWN_SECONDS,
                self._respawn_easter_hub_egg,
                args=(
                    runtime, prop_id,
                    int(player_id) if individual_usage else None,
                ),
            )
            timers[timer_key] = timer
        timer.start()

    def _respawn_easter_hub_egg(
            self, runtime, prop_id: int, player_id: int | None = None) -> None:
        """Restore one Kingshill egg and notify every player on that map."""

        if not self._map_event_runtime_is_live(runtime):
            return
        prop_id = int(prop_id)
        individual_usage = player_id is not None
        timer_key = (
            (int(player_id), prop_id) if individual_usage else prop_id
        )
        with runtime.lock:
            runtime.easter_hub_egg_respawn_timers.pop(timer_key, None)
            state = runtime.props.get(prop_id)
            if (
                state is None
                or map_key(runtime.map_id) != "a0200_kingscity"
                or not _is_kingshill_spring_egg(state.definition)
            ):
                return
            if individual_usage:
                usage_key = (int(player_id), prop_id)
                if usage_key not in runtime.individual_prop_usage:
                    return
                runtime.individual_prop_usage.discard(usage_key)
            else:
                if state.state != PROP_STATE_USED:
                    return
                state.state = PROP_STATE_ACTIVE
            definition = state.definition
        viewers = self._map_event_sessions(runtime)
        if individual_usage:
            viewers = tuple(
                viewer for viewer in viewers
                if _individual_prop_player_id(viewer) == int(player_id)
            )
        for viewer in viewers:
            update = PropUpdate(
                prop_id=definition.prop_id,
                state=PROP_STATE_ACTIVE,
                actor_id=LOCAL_PLAYER_ACTOR_ID,
                counter=_session_game_tick(viewer),
                state_changed=True,
                operation=WIRE_PROP_OPERATION,
                task_index=0,
                keep_visual_state=False,
            )
            packet, packet_bits = encode_prop_update_batch(
                (update,), actor_id=0,
            )
            viewer.send_reliable_ordered(
                (packet, packet_bits), track=True,
                label="kingshill-spring-egg-respawn",
            )
            if definition.instance_guid == ZERO_GUID:
                _send_prop_info(
                    viewer,
                    (PropRuntimeState(definition, PROP_STATE_ACTIVE),),
                    label="kingshill-spring-egg-respawn-info",
                )
        _log(
            f"Kingshill Spring Egg prop {prop_id} respawned after "
            f"{EASTER_HUB_EGG_RESPAWN_SECONDS:.0f}s"
            + (f" for character {player_id}" if individual_usage else "")
        )

    def _handle_staging_signal(self, s: Session, payload: bytes) -> None:
        CharacterService._handle_simple_gameplay_command(s, payload, 0x0113)
        decoded = s.gameplay_client_command_state.get(0x0113)
        signal_id = str(getattr(decoded, "signal_id", "") or "")
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None or not signal_id:
            return
        # Grimmagstone's scene can report the exit edge as the ritual starts.
        # The authoritative gatekeeper-death handler opens this per-run exit;
        # neither a client report nor its saved-quest variant proves a kill.
        if map_key(runtime.map_id) == "pw001_grimmagstone_01_dun" and signal_id.casefold() in {
            "pw001_spawn_exit_signal", "pw001_spawn_exit_signal_b",
        }:
            _log(f"ignored premature client exit signal {signal_id!r}")
            return
        rule = runtime.catalog.rule(signal_id)
        if rule is None or not rule.enabled:
            return
        if (
            map_key(runtime.map_id) == _DARKWOOD_WENDIGO_MAP
            and _is_darkwood_wendigo_wave_start_signal(rule.signal_id)
        ):
            _log(
                f"ignored client-reported Wendigo wave start "
                f"{rule.signal_id!r}; activate the fire prop instead"
            )
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
    def _apply_darkwood_wendigo_hazard_damage(
            s: Session, runtime: MapEventRuntime, position) -> None:
        """Apply the map-authored 80% HP damage tick before Wendigo dies.

        The static map gates these three location effects with EventSet 158
        (``BOSS_death=False``). The client publishes their visuals, but the
        server movement path did not apply their damage program, so crossing
        the guarded area was harmless on the authoritative character state.
        """

        if map_key(runtime.map_id) != "e_hal_01_darkwood_dun":
            return
        combat = getattr(s, "gameplay_combat_state", None)
        if combat is None:
            return
        # The three Summoning Slates grant mutually exclusive, timed
        # Salamander Blaze effects. The client-side aura visuals do not stop
        # this server-authored contact damage, so honor the active effect here
        # before testing the lethal fields.
        try:
            from dso_server.gameplay.status_runtime_catalog import (
                load_status_runtime_catalog,
            )
            status_catalog = load_status_runtime_catalog(
                combat.encounter.static_database_path,
            )
            immunity_ids = frozenset(
                effect_id
                for name, effect_id in status_catalog.effect_ids.items()
                if name.casefold().startswith(
                    "e_hal_01_wood_challenge_damage_aura_effect_immunity_"
                )
            )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            immunity_ids = frozenset()
        if immunity_ids:
            now = time.perf_counter()
            with s.flow_lock:
                active_immunity = any(
                    int(entry.effect_id) in immunity_ids
                    and (
                        expires_at is None
                        or float(expires_at) > now
                    )
                    for entry, expires_at
                    in tuple(s.gameplay_runtime_status_effects.values())
                )
            if active_immunity:
                return
        hazards = _darkwood_wendigo_hazards(
            str(combat.encounter.static_database_path), runtime.map_id,
        )
        if not hazards:
            return
        with runtime.lock:
            active_sets = set(
                runtime.content.enabled_event_set_rows(runtime.active_flags())
            )
        matching = tuple(
            (volume, effect_id)
            for volume, effect_id in hazards
            if volume.event_set_row in active_sets and volume.contains(position)
        )
        if not matching:
            return

        hazard_id = min(volume.row_id for volume, _effect in matching)
        now = time.perf_counter()
        with s.flow_lock:
            if (
                s.gameplay_combat_state is not combat
                or s.gameplay_map_event_runtime is not runtime
            ):
                return
            cooldowns = getattr(s, "gameplay_darkwood_hazard_next_at", None)
            if cooldowns is None:
                cooldowns = {}
                s.gameplay_darkwood_hazard_next_at = cooldowns
            if now < float(cooldowns.get(hazard_id, 0.0)):
                return
            cooldowns[hazard_id] = now + 1.0
            inventory = s.gameplay_inventory
            actor_id = int(
                getattr(s, "gameplay_remote_actor_id", 0)
                or combat.player_actor_id
            )
        if inventory is None:
            return

        from dso_server.services.talent_events import (
            dispatch_talent_event, spare_player,
        )
        with combat.lock:
            health = combat.player_health.get(actor_id)
            if health is None or int(health[0]) <= 0:
                return
            previous, maximum = int(health[0]), int(health[1])
            # The authored aura ticks every second with
            # ``CurrHealthPoints:-0.8,relative``.
            damage = max(1, int(math.ceil(maximum * 0.8)))
            current = max(0, previous - damage)
            applied = previous - current
            health[0] = current
            if actor_id == int(combat.player_actor_id):
                combat.player_current_health = current
            death_saved = current <= 0 and spare_player(s, combat, actor_id)
            current = int(health[0])
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
            s, resolution, label="darkwood-wendigo-challenge-hazard",
            include_item_set_procs=False,
        )
        _log(
            f"Wendigo challenge hazard hit on {runtime.map_id}: "
            f"actor={actor_id}, area={hazard_id}, damage={applied}, "
            f"event_sets={tuple(sorted(active_sets))}"
        )
        if death_saved:
            dispatch_talent_event(s, "nohealth", server_tick=server_tick)

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
        from dso_server.services.talent_events import (
            dispatch_talent_event, spare_player,
        )
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
            death_saved = current <= 0 and spare_player(s, combat, actor_id)
            current = int(health[0])
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
        if death_saved:
            dispatch_talent_event(s, "nohealth", server_tick=server_tick)

    @staticmethod
    def _apply_tower_aura_damage(
            s: Session, runtime: MapEventRuntime, position,
            *, previous_position=None, previous_position_at=None) -> None:
        """Damage a player standing inside a fixed tower's aura.

        ``e_dra_fortress_normal_powernode`` is used as a laser in Fractured
        Planes and the Summer Solstice Arena. It has no ``Skills`` row, so the
        ordinary monster attack path never fires for it: the visible hazard
        otherwise deals nothing.
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
        summer_arena = map_key(runtime.map_id) == "e_sum_minidun_arena_01"
        if not is_tower_layout(runtime.map_id) and not summer_arena:
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
                if (
                    is_tower_mechanism(
                        static_path, runtime.map_id, monster.template_id,
                    )
                    or (
                        summer_arena
                        and monster.template_id.casefold()
                        == "e_dra_fortress_normal_powernode"
                    )
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
            # In Arena of Endurance, the player reported taking damage while
            # merely near the visible blue line. The capsule expansion made
            # the effective beam twice as wide (the log's 0.959 distance was
            # accepted against a 1.0 threshold). Match the visible strip in
            # this arena; keep the capsule allowance for the older tower map.
            contact_distance = beam_radius + (
                0.0 if summer_arena else TOWER_PLAYER_COLLISION_RADIUS
            )
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
            from dso_server.services.talent_events import (
                dispatch_talent_event, spare_player,
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
                death_saved = current <= 0 and spare_player(s, combat, actor)
                current = int(health[0])
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
                label=(
                    "summer-solstice-laser"
                    if summer_arena else "fractured-planes-tower-aura"
                ),
                include_item_set_procs=False,
            )
            if death_saved:
                dispatch_talent_event(s, "nohealth", server_tick=server_tick)
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
                f"{'Summer Solstice laser' if summer_arena else 'Fractured Planes tower aura'} "
                f"hit on {runtime.map_id}: "
                f"actor={actor}, position="
                f"{tuple(round(value, 3) for value in world)}, "
                f"contacts={contact_summary}"
            )

    @staticmethod
    def _apply_summer_solstice_sandstorm_damage(
            s: Session, runtime: MapEventRuntime, position) -> None:
        """Apply the authored Sandstorm aura when a player enters its radius."""

        if map_key(runtime.map_id) != "e_sum_minidun_arena_01":
            return
        combat = getattr(s, "gameplay_combat_state", None)
        if combat is None:
            return
        world = tuple(float(value) for value in position[:3])
        with combat.lock:
            hazards = tuple(
                (int(actor_id), combat.monsters[actor_id], tuple(
                    float(value) for value in combat.monster_positions.get(
                        actor_id, combat.monsters[actor_id].position,
                    )[:3]
                ))
                for actor_id in combat.active_monster_actor_ids
                if actor_id in combat.monsters
                and combat.monster_health.get(actor_id, 0) > 0
                and combat.monsters[actor_id].template_id.casefold() in {
                    "e_sum_01_arena_def_normal_sandstorm_01",
                    "e_sum_01_arena_def_normal_sandstorm_02",
                }
            )
        touching = tuple(
            (actor_id, hazard_position)
            for actor_id, _monster, hazard_position in hazards
            if math.hypot(
                world[0] - hazard_position[0],
                world[2] - hazard_position[2],
            ) <= 2.0
            and abs(world[1] - hazard_position[1]) <= 1.0
        )
        if not touching:
            return
        now = time.perf_counter()
        actor_id, damage_position = min(
            touching,
            key=lambda item: math.dist(world, item[1]),
        )
        with s.flow_lock:
            if (
                s.gameplay_combat_state is not combat
                or s.gameplay_map_event_runtime is not runtime
            ):
                return
            cooldowns = getattr(
                s, "gameplay_summer_sandstorm_next_at", None,
            )
            if cooldowns is None:
                cooldowns = {}
                s.gameplay_summer_sandstorm_next_at = cooldowns
            if now < float(cooldowns.get(actor_id, 0.0)):
                return
            cooldowns[actor_id] = now + 1.0
            inventory = s.gameplay_inventory
            player_actor_id = int(
                getattr(s, "gameplay_remote_actor_id", 0)
                or combat.player_actor_id
            )
        if inventory is None:
            return
        from dso_server.services.talent_events import (
            dispatch_talent_event, spare_player,
        )
        with combat.lock:
            health = combat.player_health.get(player_actor_id)
            if health is None or int(health[0]) <= 0:
                return
            previous, maximum = int(health[0]), int(health[1])
            # e_zah_01_sandstorm_aura_effect is authored as
            # CurrHealthPoints:-0.3,relative and ticks once per second.
            damage = max(1, int(math.ceil(maximum * 0.3)))
            current = max(0, previous - damage)
            applied = previous - current
            health[0] = current
            if player_actor_id == int(combat.player_actor_id):
                combat.player_current_health = current
            death_saved = current <= 0 and spare_player(
                s, combat, player_actor_id,
            )
            current = int(health[0])
        if applied <= 0:
            return
        CharacterService._cancel_respawn_protection(s)
        with s.flow_lock:
            if s.gameplay_inventory is inventory:
                inventory.health_points = float(current)
            server_tick = int(server_time() // 40) & 0xFFFFFFFF
        resolution = DamageResolution(
            target_actor_id=CharacterService._viewer_actor_id(
                s, player_actor_id,
            ),
            attacker_actor_id=actor_id,
            current_health=current,
            maximum_health=maximum,
            damage=applied,
            killed=current <= 0,
            position=damage_position,
            server_tick=server_tick,
            hit_tags=(0,),
            skill_index=0,
            source_actor_id=actor_id,
            periodic_damage=True,
        )
        CharacterService._send_combat_resolution(
            s, resolution, label="summer-solstice-sandstorm",
            include_item_set_procs=False,
        )
        _log(
            f"Summer Solstice sandstorm hit on {runtime.map_id}: "
            f"actor={player_actor_id}, hazard={actor_id}, damage={applied}"
        )
        if death_saved:
            dispatch_talent_event(s, "nohealth", server_tick=server_tick)

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
        MapEventService._apply_darkwood_wendigo_hazard_damage(
            s, runtime, world,
        )
        MapEventService._apply_summer_solstice_sandstorm_damage(
            s, runtime, world,
        )
        MapEventService._apply_fractured_planes_hazard_damage(
            s, runtime, world,
        )
        MapEventService._apply_new_year_trap_damage(s, runtime, world)
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

    @staticmethod
    def _apply_new_year_trap_damage(
            s: Session, runtime: MapEventRuntime, position) -> None:
        if map_key(runtime.map_id) != "e_nye_dungeon_01":
            return
        combat = getattr(s, "gameplay_combat_state", None)
        if combat is None:
            return
        hazards = _new_year_trap_hazards(
            str(combat.encounter.static_database_path), runtime.map_id,
        )
        with runtime.lock:
            active_sets = set(runtime.enabled_event_sets)
        matching = tuple(
            volume for volume, _effect in hazards
            if volume.event_set_row in active_sets and volume.contains(position)
        )
        if not matching:
            return
        hazard_id = min(volume.row_id for volume in matching)
        now = time.perf_counter()
        with s.flow_lock:
            if s.gameplay_combat_state is not combat:
                return
            cooldowns = getattr(s, "gameplay_new_year_trap_next_at", None)
            if cooldowns is None:
                cooldowns = {}
                s.gameplay_new_year_trap_next_at = cooldowns
            if now < float(cooldowns.get(hazard_id, 0.0)):
                return
            cooldowns[hazard_id] = now + 1.0
            inventory = s.gameplay_inventory
            actor_id = int(
                getattr(s, "gameplay_remote_actor_id", 0)
                or combat.player_actor_id
            )
        if inventory is None:
            return
        from dso_server.services.talent_events import dispatch_talent_event, spare_player
        with combat.lock:
            health = combat.player_health.get(actor_id)
            if health is None or int(health[0]) <= 0:
                return
            previous, maximum = int(health[0]), int(health[1])
            current = max(0, previous - max(1, int(math.ceil(maximum * 0.02))))
            applied = previous - current
            health[0] = current
            if actor_id == int(combat.player_actor_id):
                combat.player_current_health = current
            death_saved = current <= 0 and spare_player(s, combat, actor_id)
            current = int(health[0])
        if applied <= 0:
            return
        CharacterService._cancel_respawn_protection(s)
        with s.flow_lock:
            if s.gameplay_inventory is inventory:
                inventory.health_points = float(current)
            server_tick = int(server_time() // 40) & 0xFFFFFFFF
        CharacterService._send_combat_resolution(
            s,
            DamageResolution(
                target_actor_id=CharacterService._viewer_actor_id(s, actor_id),
                attacker_actor_id=0,
                current_health=current,
                maximum_health=maximum,
                damage=applied,
                killed=current <= 0,
                position=tuple(float(value) for value in position),
                server_tick=server_tick,
                hit_tags=(0,), skill_index=0, source_actor_id=0,
                periodic_damage=True,
            ),
            label="rocketmen-trap",
            include_item_set_procs=False,
        )
        _log(
            f"Rocketmen trap triggered: actor={actor_id}, "
            f"effect={tuple(effect for _volume, effect in matching)!r}, "
            f"damage={applied}"
        )
        if death_saved:
            dispatch_talent_event(s, "nohealth", server_tick=server_tick)

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
        if (getattr(s, "gameplay_match", None) is not None
                and getattr(s, "gameplay_match_callback", None) is not None):
            # A battleground claims its own props (fortress control points).
            with runtime.lock:
                candidate = runtime.props.get(int(request.prop_id))
            if candidate is not None and s.gameplay_match_callback(
                    s, ("match_prop_interaction", candidate.definition)):
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
                self._restore_prop_for_viewer(
                    s, chest_definition, label="boss-chest-locked",
                )
                _log(
                    f"locked boss reward chest {chest_definition.instance_id!r} "
                    f"until {reward_gate[0]} dies"
                )
                return
        with runtime.lock:
            keyed = runtime.props.get(int(request.prop_id))
            keyed_first_use = bool(
                keyed is not None
                and keyed.definition.spends_keys
                and keyed.state == PROP_STATE_ACTIVE
                and (
                    not keyed.definition.individual_usage
                    or (
                        _individual_prop_player_id(s),
                        int(keyed.definition.prop_id),
                    ) not in runtime.individual_prop_usage
                )
            )
            gatherable_first_use = bool(
                keyed is not None
                and _multitool_resource_category(keyed.definition)
                and keyed.state == PROP_STATE_ACTIVE
                and (
                    not keyed.definition.individual_usage
                    or (
                        _individual_prop_player_id(s),
                        int(keyed.definition.prop_id),
                    ) not in runtime.individual_prop_usage
                )
            )
            wendigo_ritual_prop = (
                _darkwood_wendigo_ritual_prop(runtime, keyed.definition)
                if keyed_first_use and keyed is not None else None
            )
            wendigo_prop_unavailable = bool(
                wendigo_ritual_prop is True
                and keyed is not None
                and (
                    runtime.darkwood_wendigo_boss_spawned
                    or int(keyed.definition.prop_id)
                    in runtime.darkwood_wendigo_used_prop_ids
                    or int(keyed.definition.prop_id)
                    in runtime.darkwood_wendigo_reserved_prop_ids
                )
            )
        dtu_superchest_claim_key = ""
        summer_reward_claim_key = (
            _summer_solstice_reward_chest_claim_key(
                s, keyed.definition,
            ) if keyed is not None else ""
        )
        if summer_reward_claim_key:
            with s.flow_lock:
                already_claimed = bool(
                    getattr(s, "gameplay_timed_offer_purchases", {}).get(
                        summer_reward_claim_key, 0,
                    )
                )
            if already_claimed:
                self._show_used_prop_for_viewer(
                    s, keyed.definition, label="summer-reward-chest-already-claimed",
                )
                _log(
                    f"refused already-claimed Kalodoro Summer Solstice chest "
                    f"{keyed.definition.instance_id!r}"
                )
                return
        if (
            keyed is not None
            and str(keyed.definition.loot_list_id or "").casefold()
            in _DTU_SUPERCHEST_POOLS
            and map_key(runtime.map_id) == "e_bos_arena_01"
        ):
            dtu_superchest_claim_key = _dtu_superchest_claim_key(
                s, keyed.definition,
            )
            if not dtu_superchest_claim_key:
                self._restore_prop_for_viewer(
                    s, keyed.definition, label="dtu-superchest-event-inactive",
                )
                _log(
                    f"refused DTU superchest {keyed.definition.instance_id!r}: "
                    "no active boss_bonus_event_02 instance"
                )
                return
            with s.flow_lock:
                already_claimed = bool(
                    getattr(s, "gameplay_timed_offer_purchases", {}).get(
                        dtu_superchest_claim_key, 0,
                    )
                )
            if already_claimed:
                self._show_used_prop_for_viewer(
                    s, keyed.definition, label="dtu-superchest-already-claimed",
                )
                _log(
                    f"refused already-claimed DTU superchest "
                    f"{keyed.definition.instance_id!r} "
                    f"({dtu_superchest_claim_key})"
                )
                return
        if wendigo_ritual_prop is False:
            _log(
                f"refused malformed Darkwood Wendigo ritual prop "
                f"{keyed.definition.instance_id!r} on event set "
                f"{keyed.definition.event_set_row}"
            )
            self._restore_prop_for_viewer(
                s, keyed.definition, label="wendigo-prop-invalid",
            )
            return
        if wendigo_ritual_prop is True:
            if wendigo_prop_unavailable:
                self._show_used_prop_for_viewer(
                    s, keyed.definition, label="wendigo-ritual-complete",
                )
                _log(
                    f"refused already-used or completed Wendigo ritual prop: "
                    f"{keyed.definition.instance_id!r}"
                )
                return
        if keyed_first_use:
            missing = self._prop_keys_missing(s, keyed.definition)
            if missing:
                # Amphorae cost KeyAmount x KeyId (e_sar_chest_key) per use.
                self._restore_prop_for_viewer(
                    s, keyed.definition, label="prop-key-missing",
                )
                _log(
                    f"prop {keyed.definition.instance_id!r} needs "
                    f"{keyed.definition.key_amount}x {keyed.definition.key_id!r}; "
                    f"player is {missing} short"
                )
                return
        new_moon_recall_row = None
        if keyed_first_use and keyed is not None and (
            str(keyed.definition.instance_id).casefold()
            in NEW_MOON_RECALL_ALTAR_IDS
        ):
            new_moon_recall_row = self._new_moon_recall_spawn_row(
                runtime, keyed.definition,
            )
            if new_moon_recall_row is None:
                with runtime.lock:
                    live = runtime.props.get(int(request.prop_id))
                    if live is not None:
                        live.state = PROP_STATE_ACTIVE
                self._restore_prop_for_viewer(
                    s, keyed.definition, label="new-moon-recall-unavailable",
                )
                return
        gatherable_template = ""
        if gatherable_first_use:
            category = _multitool_resource_category(keyed.definition)
            with s.flow_lock:
                map_difficulty = max(
                    0, int(getattr(s, "gameplay_map_difficulty", 0) or 0),
                )
            # The ten retail difficulties are paired into the five ingredient
            # rarity bands: Normal/Painful through Bloodshed/PW Bloodshed.
            rarity_tier = min(5, map_difficulty // 2 + 1)
            candidates = _gatherable_ingredient_templates(
                str(self.static_database_path), category, rarity_tier,
            )
            if (
                category == "herb"
                and str(keyed.definition.instance_id).casefold()
                == "ingredient_tree_01"
            ):
                candidates = tuple(sorted(set((
                    *candidates,
                    *_gatherable_ingredient_templates(
                        str(self.static_database_path), "yarn", rarity_tier,
                    ),
                ))))
            with s.flow_lock:
                inventory = s.gameplay_inventory
                carried_multitools = sum(
                    int(item.amount) for item in getattr(inventory, "items", ())
                    if item.template_id == GATHERABLE_MULTITOOL_TEMPLATE
                )
            if carried_multitools < 1:
                self._restore_prop_for_viewer(
                    s, keyed.definition, label="gatherable-multitool-missing",
                )
                _log(
                    f"gatherable prop {keyed.definition.instance_id!r} needs "
                    f"1x {GATHERABLE_MULTITOOL_TEMPLATE!r}"
                )
                return
            if not candidates:
                self._restore_prop_for_viewer(
                    s, keyed.definition, label="gatherable-rewards-unavailable",
                )
                _log(
                    f"no live {category!r} Ingredient Bag templates found "
                    f"for map difficulty {map_difficulty} (rarity {rarity_tier})"
                )
                return
            gatherable_template = random.SystemRandom().choice(candidates)
            try:
                gatherable_definition = load_purchase_item_definition(
                    self.static_database_path, gatherable_template,
                )
            except (OSError, ValueError, sqlite3.Error) as exc:
                _log(f"cannot load gatherable item: {type(exc).__name__}: {exc}")
                self._restore_prop_for_viewer(
                    s, keyed.definition, label="gatherable-rewards-unavailable",
                )
                return
            if not gatherable_definition.ingredient_bag:
                self._restore_prop_for_viewer(
                    s, keyed.definition, label="gatherable-rewards-unavailable",
                )
                return
        wendigo_rejected = False
        wendigo_rejected_used = False
        with runtime.lock:
            existing = runtime.props.get(int(request.prop_id))
            definition = existing.definition if existing is not None else None
            quest_operation = (
                CharacterService._quest_prop_operation(s, definition)
                if definition is not None else ""
            )
            if quest_operation is None:
                _log(
                    f"PropInteraction on quest prop {definition.instance_id!r} "
                    "that is not live for this player; ignored"
                )
                return
            # Quest progression is per character even when a plain quest
            # prop's template omits IndividualUsage. Keep its shared state
            # available to other eligible players; the quest log consumes
            # this placement only for the clicker's current switch.
            # Loot chests retain their authored instance-wide use rules.
            quest_usage = bool(
                definition is not None and quest_operation
                and (definition.one_shot or definition.individual_usage)
                and not (definition.loot_list_id or definition.chest_id)
            )
            if (
                wendigo_ritual_prop is True
                and definition is not None
                and (
                    runtime.darkwood_wendigo_boss_spawned
                    or int(definition.prop_id)
                    in runtime.darkwood_wendigo_used_prop_ids
                    or int(definition.prop_id)
                    in runtime.darkwood_wendigo_reserved_prop_ids
                )
            ):
                result = (
                    PropRuntimeState(definition, PROP_STATE_USED), False,
                )
                wendigo_rejected = True
                wendigo_rejected_used = True
            elif definition is not None and (definition.individual_usage or quest_usage):
                usage_key = (
                    _individual_prop_player_id(s), int(definition.prop_id),
                )
                if (
                    existing.state == PROP_STATE_ACTIVE
                    and (quest_usage or usage_key not in runtime.individual_prop_usage)
                ):
                    if not quest_usage:
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
            if result is not None and result[1]:
                if wendigo_ritual_prop is True:
                    runtime.darkwood_wendigo_reserved_prop_ids.add(
                        int(definition.prop_id),
                    )
                CharacterService._quest_record_prop_usage(
                    s, definition, quest_operation,
                )
        if wendigo_rejected:
            if wendigo_rejected_used:
                self._show_used_prop_for_viewer(
                    s, definition, label="wendigo-ritual-already-used",
                )
            else:
                self._restore_prop_for_viewer(
                    s, definition, label="wendigo-ritual-not-ready",
                )
            _log(
                f"refused already-used or completed Wendigo ritual prop: "
                f"{definition.instance_id!r}"
            )
            return
        if result is None:
            _log(
                f"PropInteraction for unknown prop {request.prop_id} "
                f"({request.operation!r}) on {runtime.map_id}"
            )
            return
        state, flipped = result
        definition = state.definition
        if flipped and summer_reward_claim_key:
            with s.flow_lock:
                purchases = dict(
                    getattr(s, "gameplay_timed_offer_purchases", {})
                )
                already_claimed = bool(purchases.get(summer_reward_claim_key, 0))
                if not already_claimed:
                    purchases[summer_reward_claim_key] = 1
                    s.gameplay_timed_offer_purchases = purchases
            if already_claimed:
                if definition.individual_usage:
                    with runtime.lock:
                        runtime.individual_prop_usage.discard((
                            _individual_prop_player_id(s),
                            int(definition.prop_id),
                        ))
                self._show_used_prop_for_viewer(
                    s, definition, label="summer-reward-chest-race-claimed",
                )
                return
            CharacterService._request_account_save(
                s, "Summer Solstice reward chest claimed", flush=True,
            )
        if (
            flipped and definition.spends_keys
            and not self._spend_prop_keys(
                s, definition, claim_key=dtu_superchest_claim_key,
            )
        ):
            # The keys left the bag between the check and the use: undo it.
            with runtime.lock:
                if wendigo_ritual_prop is True:
                    runtime.darkwood_wendigo_reserved_prop_ids.discard(
                        int(definition.prop_id),
                    )
                if definition.individual_usage:
                    runtime.individual_prop_usage.discard((
                        _individual_prop_player_id(s), int(definition.prop_id),
                    ))
                else:
                    live = runtime.props.get(int(definition.prop_id))
                    if live is not None:
                        live.state = PROP_STATE_ACTIVE
            self._restore_prop_for_viewer(s, definition, label="prop-key-missing")
            return
        wendigo_boss_ready = False
        wendigo_used_count = 0
        if flipped and wendigo_ritual_prop is True:
            with runtime.lock:
                runtime.darkwood_wendigo_reserved_prop_ids.discard(
                    int(definition.prop_id),
                )
                runtime.darkwood_wendigo_used_prop_ids.add(
                    int(definition.prop_id),
                )
                expected_prop_ids = _darkwood_wendigo_ritual_prop_ids(runtime)
                wendigo_used_count = len(
                    runtime.darkwood_wendigo_used_prop_ids
                    & expected_prop_ids
                )
                if len(expected_prop_ids) != 3:
                    _log(
                        f"Darkwood Wendigo ritual expects 3 fire props, "
                        f"found {len(expected_prop_ids)} on {runtime.map_id}"
                    )
                elif (
                    expected_prop_ids
                    and expected_prop_ids.issubset(
                        runtime.darkwood_wendigo_used_prop_ids,
                    )
                    and not runtime.darkwood_wendigo_boss_spawned
                ):
                    # Claim the boss release while holding the shared map
                    # lock so simultaneous third clicks cannot spawn it twice.
                    runtime.darkwood_wendigo_boss_spawned = True
                    wendigo_boss_ready = True
        if flipped and gatherable_template and not self._use_gatherable_multitool(s):
            # A concurrent inventory change may have consumed the last tool
            # after the preflight check. Restore the node so no reward is lost.
            with runtime.lock:
                if definition.individual_usage:
                    runtime.individual_prop_usage.discard((
                        _individual_prop_player_id(s), int(definition.prop_id),
                    ))
                else:
                    live = runtime.props.get(int(definition.prop_id))
                    if live is not None:
                        live.state = PROP_STATE_ACTIVE
            self._restore_prop_for_viewer(
                s, definition, label="gatherable-multitool-missing",
            )
            return
        _log(
            f"prop {definition.instance_id!r} (id {definition.prop_id}, "
            f"operation {request.operation!r}) used; state now {state.state}"
            f"{' (changed)' if flipped else ''}"
        )
        if flipped:
            self._schedule_easter_hub_egg_respawn(
                runtime, definition,
                player_id=_individual_prop_player_id(s),
            )
            self._spawn_pw022_gorgon_from_bust(s, runtime, definition)
        if flipped and new_moon_recall_row is not None:
            self._spawn_new_moon_boss_from_recall_altar(
                s, runtime, definition, new_moon_recall_row,
            )
        # Retail's PropUpdate state flip: the clicking player's actor in the
        # body, the new state, current game tick, flags false, trailing actor
        # 0. Server-created zero-GUID props also get a PropInfo refresh below.
        remote_actor_id = int(getattr(s, "gameplay_remote_actor_id", 0) or 0)
        viewers = (
            (s,) if definition.individual_usage or quest_usage
            else self._map_event_sessions(runtime) or (s,)
        )
        for viewer in viewers:
            if (
                viewer is not s
                and CharacterService._quest_prop_operation(viewer, definition) is None
            ):
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
            # A scene prop with its authored GUID is already bound to the
            # client entity. Replaying PropInfo makes LookupPropEntity try to
            # assign a second prop id to that same entity (the client's
            # "Entity already had a prop assigned" assertion). PropUpdate is
            # the retail state flip; keep PropInfo refreshes for server-created
            # zero-GUID props, which need their factory-backed refresh path.
            if definition.instance_guid == ZERO_GUID:
                _send_prop_info(viewer, (state,), label="prop-update-info")
        if flipped and wendigo_ritual_prop is True:
            _log(
                f"Wendigo ritual circle {wendigo_used_count}/3 activated; "
                f"spent {definition.key_amount}x {definition.key_id}"
            )
            if 1 <= wendigo_used_count <= len(
                _DARKWOOD_WENDIGO_QUEST_TRIGGERS
            ):
                # The retail quest's three ExploreTasks still reference the
                # old sequential wave EventSets. The custom ritual now shows
                # all three props together, so those gated map volumes never
                # raise halchexpl02/03. Advance the corresponding discovery
                # task only after this distinct circle was successfully used.
                CharacterService._quest_on_triggers(
                    self, s,
                    (_DARKWOOD_WENDIGO_QUEST_TRIGGERS[
                        wendigo_used_count - 1
                    ],),
                )
        if wendigo_boss_ready:
            ritual_event = runtime.catalog.event(
                _DARKWOOD_WENDIGO_RITUAL_EVENT,
            )
            if ritual_event is not None:
                self._stop_map_local_event(runtime, ritual_event)
            self._emit_map_signal(
                s, runtime, _DARKWOOD_WENDIGO_BOSS_SIGNAL,
            )
            _log(
                f"all three Wendigo ritual circles were used on {runtime.map_id}; "
                f"started boss event with {_DARKWOOD_WENDIGO_BOSS_SIGNAL!r}"
            )
        if flipped and gatherable_template:
            self._spawn_gatherable_drop(
                s, definition, gatherable_template,
                random.SystemRandom().randint(1, 10),
            )
        elif (
            flipped
            and map_key(runtime.map_id) == "e_sum_minidun_chestroom_01"
            and definition.template_id.casefold().startswith(
                "e_sum_01_minigame_chest_picker_"
            )
        ):
            # Picking a floor plate only banks a choice; its contents are
            # revealed by the authored chest/brazier flow. Dropping the plate
            # loot immediately would bypass the Chamber's risk mechanic.
            pass
        elif (
            flipped
            and map_key(runtime.map_id) == "e_sum_dun_02"
            and definition.template_id.casefold() in {
                "e_sum_01_minigame_fishing_01_yellow",
                "e_sum_01_minigame_fishing_01_red",
                "e_sum_01_minigame_fishing_01_blue",
            }
        ):
            # Fishing vats have no LootListId in static.db4 even though their
            # OperationId is the retail fishing operation. The server must
            # resolve that operation after charging the authored Fishing Rod.
            self._spawn_map_chest_loot(
                s, replace(
                    definition,
                    loot_list_id="CHEST_lootlist_e_sum_fishing_vat",
                ),
            )
        elif flipped and (definition.loot_list_id or definition.chest_id):
            self._spawn_map_chest_loot(s, definition)
        if state.state == PROP_STATE_ACTIVE or flipped:
            self._grant_prop_ammunition_buff(s, runtime, definition)
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
            if (
                map_key(runtime.map_id) == "e_sum_minidun_chestroom_01"
                and definition.template_id.casefold()
                == "e_sum_01_minigame_chest_picker_fault"
            ):
                self._fail_underground_chamber_chest_game(runtime)
            elif (
                map_key(runtime.map_id) == "e_sum_minidun_chestroom_01"
                and definition.template_id.casefold()
                == "e_sum_01_minigame_chest_picker_done"
            ):
                self._bank_underground_chamber_chests(runtime)
            elif (
                flipped
                and map_key(runtime.map_id)
                == "e_sum_minidun_chestroom_01"
                and definition.template_id.casefold().startswith(
                    "e_sum_01_minigame_chest_picker_"
                )
            ):
                # PlayerInteractedProp has already activated this plate's
                # unlock_chest_N event, so a hit banks this and all earlier
                # choices before the player can select another plate.
                self._roll_underground_chamber_plate_trigger(
                    runtime, definition.template_id,
                )
            CharacterService._achievement_on_prop_interacted(
                self, s, definition.instance_id, definition.template_id,
            )

    @staticmethod
    def _underground_chamber_chest_rows(runtime) -> dict[int, int]:
        """Map each reward EventSet row to its authored picker number."""

        chest_rows = {}
        for number in range(1, 32):
            event_key = f"e_sum_dun_01_chest_game_activate_chest_{number:02d}"
            for event_set in runtime.content.event_sets_for(event_key):
                if event_key in event_set.required:
                    chest_rows[int(event_set.row)] = number
        return chest_rows

    def _bank_underground_chamber_chests(
            self, runtime, *, reason: str = "brazier") -> None:
        """Reveal selected rewards and retire every unselected chest."""

        if map_key(runtime.map_id) != "e_sum_minidun_chestroom_01":
            return
        marker = "underground_chamber_chests_closed"
        with runtime.lock:
            if marker in runtime.fired_once:
                return
            flags = runtime.active_flags()
            selected = {
                int(flag.rsplit("_", 1)[-1])
                for flag in flags
                if flag.startswith("e_sum_dun_01_chest_game_unlock_chest_")
                and flag.rsplit("_", 1)[-1].isdigit()
            }
            selected = {number for number in selected if 1 <= number <= 31}
            runtime.fired_once.add(marker)
            runtime.fired_once.add("underground_chamber_chests_banked")
        # Preserve the authored ending transition so the picker field closes.
        # Its aggregate EventSet 1748 is suppressed when the runtime attaches,
        # preventing that phase from opening all reward chests.
        ending = runtime.catalog.event("e_sum_dun_01_chest_game_ending")
        if ending is not None:
            self._start_map_local_event(runtime, ending)
        chest_rows = self._underground_chamber_chest_rows(runtime)
        hidden_rows = {
            row for row, number in chest_rows.items()
            if number not in selected
        }
        picker_rows = {
            int(prop.event_set_row)
            for prop in runtime.content.props
            if int(prop.event_set_row) >= 0
            and prop.template_id.casefold().startswith(
                "e_sum_01_minigame_chest_picker_"
            )
        }

        # The authored brazier signals start these events. Ensure the selected
        # reward gates are active, then make every other chest unavailable.
        for number in sorted(selected):
            event = runtime.catalog.event(
                f"e_sum_dun_01_chest_game_activate_chest_{number:02d}"
            )
            if event is not None:
                self._start_map_local_event(runtime, event)
        rows_to_hide = hidden_rows | picker_rows
        if rows_to_hide:
            runtime.suppress_event_sets(rows_to_hide)
            self._unpublish_event_props(
                runtime, self._map_event_sessions(runtime), rows_to_hide,
            )
        _log(
            f"Underground Chamber rewards revealed by {reason}: "
            f"selected={tuple(sorted(selected))}, "
            f"hidden reward sets={len(hidden_rows)}, "
            f"closed picker sets={len(picker_rows)}"
        )

    def _roll_underground_chamber_plate_trigger(
            self, runtime, plate_template_id: str) -> None:
        """Each selected plate has a 25% chance to resolve the run."""

        if map_key(runtime.map_id) != "e_sum_minidun_chestroom_01":
            return
        template = str(plate_template_id or "").casefold()
        if not template.startswith("e_sum_01_minigame_chest_picker_"):
            return
        with runtime.lock:
            if "underground_chamber_chests_closed" in runtime.fired_once:
                return
        triggered = random.SystemRandom().random() < 0.25
        _log(
            f"Underground Chamber plate {template!r} "
            f"trigger roll={'hit' if triggered else 'safe'} (25%)"
        )
        if triggered:
            self._fail_underground_chamber_chest_game(runtime)

    def _fail_underground_chamber_chest_game(self, runtime) -> None:
        """Resolve the trigger chest: keep selected rewards, hide the rest."""

        if map_key(runtime.map_id) != "e_sum_minidun_chestroom_01":
            return
        with runtime.lock:
            if "underground_chamber_chests_closed" in runtime.fired_once:
                return
        self._bank_underground_chamber_chests(runtime, reason="trigger chest")

    def _grant_prop_ammunition_buff(self, s, runtime, definition) -> None:
        """Mirror authored state-object ammunition buffs into server combat."""

        map_id = map_key(runtime.map_id)
        template_id = str(definition.template_id or "").casefold()
        wendigo_buff_effects = {
            "e_hal_01_wood_challenge_buff_01":
                "e_hal_01_wood_challenge_damage_aura_effect_immunity_01",
            "e_hal_01_wood_challenge_buff_02":
                "e_hal_01_wood_challenge_damage_aura_effect_immunity_02",
            "e_hal_01_wood_challenge_buff_03":
                "e_hal_01_wood_challenge_damage_aura_effect_immunity_03",
        }
        if (
            map_id == "e_hal_01_darkwood_dun"
            and template_id in wendigo_buff_effects
        ):
            self._grant_darkwood_wendigo_buff(
                s, definition, wendigo_buff_effects[template_id],
            )
            return
        expected = None
        if (
            map_id == "pw007_stonekeep_01_dun"
            and template_id == "pw007_mill"
        ):
            expected = ("pw007_miller_attack_buff", "AmmoType06")
        elif (
            map_id in {
                "e_newmoon_blakborg_white_dun",
                "e_newmoon_blakborg_black_dun",
            }
            and template_id in {"e_new_02_pot_white_01", "e_new_02_pot_black_01"}
        ):
            # Fairy Wood in either realm's cauldron grants 120 seconds of the
            # exact AmmoType04 required by Karabossa and Magotina.
            expected = ("e_new_01_fairy_buff", "AmmoType04")
        if expected is None:
            return
        expected_effect, expected_ammo = expected
        try:
            database_uri = Path(self.static_database_path).resolve().as_uri()
            with closing(sqlite3.connect(
                f"{database_uri}?mode=ro", uri=True,
            )) as database:
                state = database.execute(
                    "SELECT COALESCE(StatusEffects, '') "
                    "FROM _Template_StateObject WHERE Id=? LIMIT 1",
                    (definition.template_id,),
                ).fetchone()
                if state is None:
                    return
                effect_names = tuple(
                    token.strip().split(",", 1)[0]
                    for token in str(state[0] or "").replace(";", ",").split(",")
                    if token.strip()
                )
                if expected_effect.casefold() not in {
                    name.casefold() for name in effect_names
                }:
                    return
                effect = database.execute(
                    "SELECT rowid - 1, COALESCE(StatusEffectDuration, 0) "
                    "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
                    (expected_effect,),
                ).fetchone()
            if effect is None:
                return
            effect_id, duration_seconds = int(effect[0]), max(
                0.0, float(effect[1] or 0.0),
            )
            from dso_server.gameplay.status_modifier_runtime import (
                load_status_modifier_catalog,
            )
            modifiers = load_status_modifier_catalog(
                self.static_database_path,
            ).definitions.get(effect_id)
            if modifiers is None or not any(
                str(instruction.kind).casefold() == "ammunitiontype"
                and any(
                    str(argument).casefold() == expected_ammo.casefold()
                    for argument in instruction.arguments
                )
                for instruction in modifiers.start_modifiers
            ):
                _log(
                    f"state-object buff {expected_effect!r} does not carry "
                    f"{expected_ammo}"
                )
                return
            with s.flow_lock:
                active_entries = [
                    entry
                    for entry in s.gameplay_persistent_status_effects.values()
                ]
                active_entries.extend(
                    entry for entry, expires_at
                    in s.gameplay_runtime_status_effects.values()
                    if expires_at is None or float(expires_at) > time.perf_counter()
                )
                if any(int(entry.effect_id) == effect_id for entry in active_entries):
                    return
                duration_ticks = max(
                    1, int(math.ceil(duration_seconds * SERVER_TICKS_PER_SECOND)),
                )
                start_tick = _session_game_tick(s) & 0xFFFFFFFF
                instance_id = int(s.gameplay_next_status_effect_instance_id)
                s.gameplay_next_status_effect_instance_id += 1
                entry = StatusEffectEntry(
                    effect_id=effect_id,
                    instance_id=instance_id,
                    end_tick=(start_tick + duration_ticks) & 0xFFFFFFFF,
                    tick_interval=SERVER_TICKS_PER_SECOND,
                    start_tick=start_tick,
                    stack_index=0,
                    total_duration=duration_ticks,
                    source_level=100,
                    source_actor_id=LOCAL_PLAYER_ACTOR_ID,
                    update_timing=False,
                    stacked=False,
                    permanent=False,
                    has_details=True,
                )
                expires_at = time.perf_counter() + duration_ticks / float(
                    SERVER_TICKS_PER_SECOND
                )
                s.gameplay_runtime_status_effects[instance_id] = (
                    entry, expires_at,
                )
            value = StatusEffect(False, (entry,))
            s.send_reliable_ordered(
                encode_status_effect_batch(value, actor_id=LOCAL_PLAYER_ACTOR_ID),
                track=True,
                label="state-object-ammunition",
            )
            # Summon attacks run on the map simulation thread and retain the
            # owner's modifier snapshot from summon time. Refresh it now so
            # already-active Shadow Souls also acquire the Fairy Fire gate.
            combat = getattr(s, "gameplay_combat_state", None)
            if combat is not None:
                owner_actor_id = int(
                    getattr(s, "gameplay_remote_actor_id", 0)
                    or combat.player_actor_id
                )
                with s.flow_lock:
                    modifier_sources = tuple(
                        CharacterService._active_skill_modifier_sources(s)
                    )
                with combat.lock:
                    for actor_id, summon in tuple(combat.player_summons.items()):
                        if int(summon.owner_actor_id) == owner_actor_id:
                            combat.player_summons[actor_id] = replace(
                                summon, modifier_sources=modifier_sources,
                            )
            callback = getattr(s, "gameplay_presence_callback", None)
            if callback is not None:
                callback(s, ("status_effects", (value,)))
            _log(
                f"granted state-object vulnerability ammo to player "
                f"(effect {effect_id}, {duration_ticks} ticks)"
            )
        except (OSError, sqlite3.Error, TypeError, ValueError, AttributeError) as exc:
            _log(f"could not grant Stonekeep Miller ammunition: {exc}")

    def _grant_darkwood_wendigo_buff(
            self, s: Session, definition, expected_effect: str) -> None:
        """Apply the selected Summoning Slate's authored Salamander Blaze."""

        try:
            from dso_server.gameplay.status_runtime_catalog import (
                load_status_runtime_catalog,
            )
            immunity_effect_ids = frozenset(
                effect_id
                for name, effect_id in load_status_runtime_catalog(
                    self.static_database_path,
                ).effect_ids.items()
                if name.casefold().startswith(
                    "e_hal_01_wood_challenge_damage_aura_effect_immunity_"
                )
            )
            database_uri = Path(self.static_database_path).resolve().as_uri()
            with closing(sqlite3.connect(
                f"{database_uri}?mode=ro", uri=True,
            )) as database:
                state = database.execute(
                    "SELECT COALESCE(StatusEffects, '') "
                    "FROM _Template_StateObject WHERE Id=? LIMIT 1",
                    (definition.template_id,),
                ).fetchone()
                effect = database.execute(
                    "SELECT rowid - 1, COALESCE(StatusEffectDuration, 0) "
                    "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
                    (expected_effect,),
                ).fetchone()
            state_effects = tuple(
                token.strip().split(",", 1)[0]
                for token in str(state[0] or "").replace(";", "|").split("|")
                if token.strip()
            ) if state is not None else ()
            if (
                effect is None
                or expected_effect.casefold() not in {
                    name.casefold() for name in state_effects
                }
            ):
                _log(
                    f"Darkwood Summoning Slate {definition.template_id!r} "
                    f"does not author {expected_effect!r}"
                )
                return
            effect_id = int(effect[0])
            duration_seconds = max(0.0, float(effect[1] or 0.0))
            if duration_seconds <= 0:
                _log(f"Darkwood buff {expected_effect!r} has no duration")
                return
            duration_ticks = max(
                1, int(math.ceil(duration_seconds * SERVER_TICKS_PER_SECOND)),
            )
            now = time.perf_counter()
            with s.flow_lock:
                runtime_effects = s.gameplay_runtime_status_effects
                removals = []
                for instance_id, (entry, expires_at) in tuple(
                    runtime_effects.items()
                ):
                    if int(entry.effect_id) in immunity_effect_ids:
                        # The retail effects are mutually exclusive; clear an
                        # older slate's server timer before installing the
                        # newly selected duration.
                        runtime_effects.pop(instance_id, None)
                        removals.append(entry)
                start_tick = _session_game_tick(s) & 0xFFFFFFFF
                instance_id = int(s.gameplay_next_status_effect_instance_id)
                s.gameplay_next_status_effect_instance_id += 1
                entry = StatusEffectEntry(
                    effect_id=effect_id,
                    instance_id=instance_id,
                    end_tick=(start_tick + duration_ticks) & 0xFFFFFFFF,
                    tick_interval=SERVER_TICKS_PER_SECOND,
                    start_tick=start_tick,
                    stack_index=0,
                    total_duration=duration_ticks,
                    source_level=100,
                    source_actor_id=LOCAL_PLAYER_ACTOR_ID,
                    update_timing=False,
                    stacked=False,
                    permanent=False,
                    has_details=True,
                )
                runtime_effects[instance_id] = (
                    entry, now + duration_seconds,
                )
            status_entries = tuple(
                CharacterService._consumable_cleanup_entry(
                    old, server_tick=start_tick,
                )
                for old in removals
            ) + (entry,)
            value = StatusEffect(False, status_entries)
            s.send_reliable_ordered(
                encode_status_effect_batch(
                    value, actor_id=LOCAL_PLAYER_ACTOR_ID,
                ),
                track=True,
                label="darkwood-salamander-blaze",
            )
            callback = getattr(s, "gameplay_presence_callback", None)
            if callback is not None:
                callback(s, ("status_effects", (value,)))
            _log(
                f"granted Salamander Blaze from {definition.instance_id!r}: "
                f"effect={expected_effect}, duration={duration_seconds:.0f}s"
            )
        except (OSError, sqlite3.Error, TypeError, ValueError, AttributeError) as exc:
            _log(f"could not grant Darkwood Salamander Blaze: {exc}")

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

    def _new_moon_recall_spawn_row(self, runtime, definition):
        """Resolve the authored witch spawner and its recall altar position."""

        map_id = map_key(runtime.map_id)
        realm = {
            "e_newmoon_blakborg_white_dun": "white",
            "e_newmoon_blakborg_black_dun": "black",
        }.get(map_id)
        instance_id = str(definition.instance_id or "").casefold()
        if (
            realm is None
            or instance_id not in NEW_MOON_RECALL_ALTAR_IDS
        ):
            return None
        try:
            with closing(sqlite3.connect(
                f"{Path(self.static_database_path).resolve().as_uri()}?mode=ro",
                uri=True,
            )) as database:
                state_object = database.execute(
                    "SELECT COALESCE(MonsterId, ''), COALESCE(SpawnPoint, '') "
                    "FROM _Template_StateObject WHERE Id=? LIMIT 1",
                    (definition.template_id,),
                ).fetchone()
            if state_object is None:
                raise ValueError(
                    f"state-object template {definition.template_id!r} is missing"
                )
            monster_id, spawn_point = (
                str(value or "").strip() for value in state_object
            )
            expected_spawner = f"e_new_02_miniboss_witch_{realm}_spawner_01"
            if monster_id.casefold() != expected_spawner:
                raise ValueError(
                    f"unexpected {realm} recall spawner {monster_id!r}"
                )
            if not spawn_point:
                raise ValueError("state-object template has no recall spawn point")
            boss_template = (
                resolve_boss_template_id(self.static_database_path, monster_id)
                or monster_id
            )
            map_path = map_database_path(self.static_database_path, runtime.map_id)
            with closing(sqlite3.connect(
                f"file:{map_path.as_posix()}?mode=ro", uri=True,
            )) as database:
                anchor = database.execute(
                    "SELECT rowid, Transform FROM _Instance_SpawnPoint "
                    "WHERE lower(COALESCE(_ID, ''))=? "
                    "OR lower(COALESCE(Id, ''))=? "
                    "OR lower(COALESCE(Name, ''))=? "
                    "ORDER BY rowid LIMIT 1",
                    (spawn_point.casefold(),) * 3,
                ).fetchone()
            if anchor is None:
                raise ValueError(f"spawn point {spawn_point!r} is missing")
            matrix = struct.unpack("<16f", bytes(anchor[1]))
            position = tuple(float(matrix[index]) for index in (12, 13, 14))
            if _load_runtime_definition(
                self.static_database_path, boss_template, 0,
            ) is None:
                raise ValueError(f"boss template {boss_template!r} is unavailable")
            return GatedSpawnRow(
                -2_200_000 - int(definition.row_id), -1,
                boss_template, position,
            )
        except (OSError, sqlite3.Error, TypeError, ValueError, struct.error) as exc:
            _log(
                f"New Moon recall altar {definition.instance_id!r} cannot spawn: "
                f"{type(exc).__name__}: {exc}"
            )
            return None

    def _spawn_new_moon_boss_from_recall_altar(
            self, s, runtime, definition, spawn_row) -> None:
        """Spawn the witch boss bound to a clicked New Moon recall altar."""

        sessions = self._map_event_sessions(runtime)
        if s not in sessions:
            sessions = (*sessions, s)
        self._spawn_event_monsters(
            runtime, sessions, (), extra_rows=(spawn_row,),
        )
        _log(
            f"New Moon recall altar {definition.instance_id!r} requested "
            f"{spawn_row.template_id!r} at {spawn_row.position!r}"
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

    def _publish_stellar_gold_barrier_open(self, runtime, sessions) -> None:
        packet, packet_bits = encode_server_command_batch((
            ServerCommand(
                OBSTACLE_UPDATE_COMMAND,
                # ObstacleUpdate addresses the obstacle's authored Name, not
                # its map row ID or _ID. The DB4 names this gate "ed_barrier".
                ObstacleUpdate((ObstacleState("ed_barrier", 2),)),
                LOCAL_PLAYER_ACTOR_ID,
            ),
        ))
        for viewer in sessions:
            CharacterService._send_world_loot_state_batch(
                viewer, packet, packet_bits, label="stellar-gold-barrier-open",
            )
        _log(
            f"opened Stellar Gold arena obstacle on {runtime.map_id} "
            f"for {len(sessions)} player(s)"
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
        if (
            map_key(runtime.map_id) == "e_fullmoon_varholm_dun"
            and definition.key == "e_ful_02_vargulf_fight"
        ):
            # The Vial interaction is the summon action; retail's ten-second
            # authored delay leaves the player waiting after the ritual.
            delay = 0.0
        elif (
            map_key(runtime.map_id) == _SARGON_THRONE_ROOM_MAP
            and str(definition.key).casefold() in {
                "boss_sargon_second_phase",
                "boss_sargon_second_phase_damage_aura",
            }
        ):
            # Sargon starts his reincarnation phase four seconds after phase
            # one dies; keep its damage aura on the same phase boundary.
            delay = 4.0
        else:
            delay = max(5.0, definition.start_delay) if wave else definition.start_delay
            if map_key(runtime.map_id) == "e_nye_dungeon_01":
                delay = _new_year_machine_delay(
                    runtime, definition, self._map_event_sessions(runtime),
                )
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
        if (
            map_key(runtime.map_id) == "e_nye_dungeon_01"
            and _new_year_machine_event_stage(definition.key) == 1
            and not self._map_event_sessions(runtime)
        ):
            # The first Machine Baby timer is anchored to instance creation,
            # which can precede the entrant becoming presence-ready. Do not
            # consume the event transition before there is a combat/session
            # context to materialize the Baby; retry this same due event
            # without restarting its 30-second countdown.
            scheduler = getattr(runtime, "scheduler", None)
            if scheduler is not None:
                with runtime.lock:
                    if runtime.pending_start_tokens.get(definition.key) is not token:
                        return
                    handle = scheduler.timer(
                        0.5,
                        self._run_delayed_map_event_start,
                        args=(runtime, definition, token),
                    )
                    runtime.pending[definition.key] = handle
                handle.start()
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
        # Keep flag mutation and its world publication in the same order as
        # global schedule reconciliation. This separate reentrant gate also
        # permits a transition to synchronously trigger another local event.
        with runtime.transition_lock:
            self._start_map_local_event_locked(
                runtime, definition, pending_token=pending_token,
            )

    def _start_map_local_event_locked(
            self, runtime: MapEventRuntime,
            definition: MapLocalEventDefinition, *, pending_token=None) -> None:
        if map_key(runtime.map_id) == SARGON_SHADOWFORT_MAP_ID:
            event_key = str(definition.key).casefold()
            with runtime.lock:
                sentinel_unlocked = runtime.sargon_shadowfort_sentinel_unlocked
                sentinel_defeated = runtime.sargon_shadowfort_sentinel_defeated
            if (
                event_key == SARGON_SHADOWFORT_SENTINEL_EVENT
                and not sentinel_unlocked
            ):
                _log("blocked Sargon Shadowfort Sentinel event before altar clear")
                return
            if (
                event_key in SARGON_SHADOWFORT_EXIT_EVENT_KEYS
                and not sentinel_defeated
            ):
                _log("blocked Sargon Shadowfort boss gate before Sentinel death")
                return
        if map_key(runtime.map_id) == _SARGON_THRONE_ROOM_MAP:
            event_key = str(definition.key).casefold()
            with runtime.lock:
                first_phase_defeated = (
                    runtime.sargon_throne_first_phase_defeated
                )
                second_phase_authorized = (
                    runtime.sargon_throne_second_phase_authorized
                )
            if event_key in {"boss_start", "boss_spawn"} and first_phase_defeated:
                _log(
                    f"blocked Sargon {event_key} restart after first-phase death"
                )
                return
            if event_key in {
                "boss_sargon_second_phase",
                "boss_sargon_second_phase_damage_aura",
            } and not (first_phase_defeated and second_phase_authorized):
                _log(
                    f"blocked Sargon event {event_key!r} before authorized phase 2"
                )
                return
        if map_key(runtime.map_id) == "e_sum_minidun_chestroom_01":
            event_key = definition.key.casefold()
            if event_key == "e_sum_dun_01_chest_game_ending":
                with runtime.lock:
                    closing_allowed = bool(
                        {
                            "underground_chamber_chests_banked",
                            "underground_chamber_chests_failed",
                        } & runtime.fired_once
                    )
                if not closing_allowed:
                    _log(
                        "blocked Underground Chamber ending before the "
                        "brazier or a failed picker"
                    )
                    return
            elif event_key == "e_sum_dun_01_chest_game_activate_chests":
                _log(
                    "blocked bulk Underground Chamber chest activation; "
                    "rewards are selected individually"
                )
                return
            elif event_key.startswith(
                "e_sum_dun_01_chest_game_activate_chest_"
            ):
                suffix = event_key.rsplit("_", 1)[-1]
                if suffix.isdigit():
                    unlock_flag = (
                        "e_sum_dun_01_chest_game_unlock_chest_"
                        f"{int(suffix):02d}"
                    )
                    with runtime.lock:
                        selected_after_brazier = (
                            "underground_chamber_chests_banked"
                            in runtime.fired_once
                            and unlock_flag in runtime.active_flags()
                        )
                    if not selected_after_brazier:
                        _log(
                            "blocked unselected Underground Chamber reward "
                            f"event {event_key!r} before banking"
                        )
                        return
        gate = _PARALLEL_WORLD_EXIT_BOSSES.get(map_key(runtime.map_id))
        if (
            gate is not None
            and definition.key == gate[1]
            and definition.key not in runtime.parallel_world_gatekeepers_defeated
        ):
            _log(f"blocked Parallel World exit {definition.key!r} before verified gate clear")
            return
        if (
            map_key(runtime.map_id) == "pw001_grimmagstone_01_dun"
            and definition.key == "pw001_spawn_exit"
            and not runtime.grimmagstone_gatekeeper_defeated
        ):
            _log("blocked Grimmagstone exit activation before gatekeeper death")
            return
        with runtime.lock:
            if (pending_token is not None
                    and runtime.pending_start_tokens.get(definition.key) is not pending_token):
                return
            transition = runtime.begin_event(definition)
        if transition is None:
            return
        if (
            map_key(runtime.map_id) == _SARGON_THRONE_ROOM_MAP
            and str(definition.key).casefold()
            == "boss_sargon_second_phase"
        ):
            with runtime.lock:
                runtime.sargon_throne_second_phase_started = True
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
        with runtime.transition_lock:
            self._stop_map_local_event_locked(runtime, definition)

    def _stop_map_local_event_locked(
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
            extra_spawn_rows = ()
            if (
                transition.active
                and map_key(runtime.map_id) == "e_nye_dungeon_01"
            ):
                machine_stage = _new_year_machine_event_stage(
                    transition.event.key,
                )
                machine_row = _new_year_machine_spawn_row(
                    runtime, machine_stage,
                )
                if machine_row is not None:
                    extra_spawn_rows = (machine_row,)
            if (
                transition.active
                and map_key(runtime.map_id) == "e_fullmoon_varholm_dun"
                and transition.event.key == "e_ful_02_vargulf_fight"
            ):
                fight_set = next((
                    int(row)
                    for row in transition.enabled_sets
                    if transition.event.key in (
                        runtime.content.event_sets.get(int(row)).events
                        if runtime.content.event_sets.get(int(row)) is not None
                        else ()
                    )
                ), None)
                spawn_path = map_database_path(
                    self.static_database_path, runtime.map_id,
                )
                try:
                    with closing(sqlite3.connect(
                        f"file:{spawn_path.as_posix()}?mode=ro", uri=True,
                    )) as database:
                        spawnpoint = database.execute(
                            """SELECT rowid, Transform FROM _Instance_SpawnPoint
                               WHERE lower(COALESCE(_ID, ''))=?
                               ORDER BY rowid LIMIT 1""",
                            (
                                "e_ful_02_varholm_night_00_miniboss_werewolf_01_spawnpoint",
                            ),
                        ).fetchone()
                except (OSError, sqlite3.Error) as exc:
                    _log(f"could not read Vargulf spawnpoint: {exc}")
                    spawnpoint = None
                if fight_set is not None and spawnpoint is not None:
                    try:
                        matrix = struct.unpack(
                            "<16f", bytes(spawnpoint[1]),
                        )
                    except (TypeError, struct.error):
                        matrix = ()
                else:
                    matrix = ()
                if fight_set is not None and len(matrix) == 16:
                    # The authored fight sets contain the ranged add waves but
                    # no Vargulf SpawnPoint. Materialize the boss with the
                    # first fight phase at the map's authored boss spawnpoint,
                    # rather than at the altar prop. Attach it to the set so
                    # the boss-death signal retires it with the adds.
                    extra_spawn_rows = (GatedSpawnRow(
                        -2_000_001,
                        fight_set,
                        "e_ful_02_varholm_night_00_miniboss_werewolf_01",
                        tuple(float(value) for value in matrix[12:15]),
                        math.atan2(float(matrix[8]), float(matrix[0])),
                    ),)
                else:
                    _log(
                        "could not add Vargulf boss spawn: missing fight "
                        f"set or authored spawnpoint on {runtime.map_id} "
                        f"(fight_set={fight_set}, spawnpoint={spawnpoint is not None})"
                    )
            self._spawn_event_monsters(
                runtime, sessions, transition.enabled_sets,
                extra_rows=extra_spawn_rows,
            )
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
        if transition.active and transition.event.key == "boss_death":
            self._silence_arena_hazards(runtime, sessions)
        if (
            transition.active
            and map_key(runtime.map_id) == "e_stego_arena_01"
            and transition.event.key == "e_stego_barrier_removal_01"
        ):
            # EventUpdate removes the visible EventSet, but the client's
            # obstacle controller retains collision until state 2 is sent.
            self._publish_stellar_gold_barrier_open(runtime, sessions)
        if (
            map_key(runtime.map_id) == "e_bos_arena_01"
            and transition.enabled_sets
        ) or (
            is_dtu_boss_arena_map(runtime.map_id)
            and transition.active
            and transition.event.key in {"boss_death", "boss_death_dtu"}
        ):
            boss_room = is_dtu_boss_arena_map(runtime.map_id)
            if boss_room:
                # EventSet 157 owns the DTU post-boss chest rows. The death
                # event can arrive with no newly enabled rows when the set is
                # already active, so explicitly republish its props after the
                # client has processed the event transition.
                self._publish_event_props(
                    runtime, sessions, (157,), refresh_existing=True,
                )
            self._publish_dtu_arena_event_props(
                runtime, sessions,
                event_sets=() if boss_room else transition.enabled_sets,
            )
        mortis = mortis_arena(self, runtime)
        if mortis is not None:
            mortis.transition(transition)

    def _silence_arena_hazards(self, runtime, sessions) -> None:
        """Stop the statue turrets guarding a boss once it is defeated.

        The Winter Summit's two turrets flank Big Paws on rows no EventSet
        switches, so BOSS_death (which opens the arena's exits) has nothing to
        retire them with. Silence the statue traps around each defeated boss.
        """
        combat = self._map_event_combat_state(sessions)
        if combat is None:
            return
        with combat.lock:
            arenas = tuple(
                combat.monster_positions[actor_id]
                for actor_id, monster in combat.monsters.items()
                if str(monster.rank_id).casefold().startswith("rank_boss")
                and combat.monster_health.get(actor_id, 0) <= 0
            )
            silenced = set()
            for position in arenas:
                silenced.update(combat.silence_hazards_near(
                    position, ARENA_HAZARD_RADIUS,
                ))
        if silenced:
            _log(
                f"silenced {len(silenced)} arena turret(s) after the boss "
                f"died on {runtime.map_id}"
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
        self._publish_map_event_snapshot(
            runtime,
            sessions,
            changed_event=transition.event.event_id,
            active=transition.active,
            include_event_key=transition.event.key,
        )

    def _publish_map_event_snapshot(
            self, runtime, sessions, *, changed_event: str, active: bool,
            include_event_key: str | None = None) -> None:
        # A map-local EventUpdate must describe the full state of the map's
        # event flags. Sending only currently running events leaves any event
        # that ended earlier latched on the client, which can keep an EventSet
        # (and its trap effects) visible after a later platform interaction.
        with runtime.lock:
            active_flags = set(runtime.active_flags())
            catalog = runtime.catalog
            referenced = set(runtime.content.referenced_events)
            referenced.update(runtime.running)
            if include_event_key:
                referenced.add(str(include_event_key).casefold())
        definitions = {}
        target_map = map_key(runtime.map_id)
        # A Mighty Threat's map-clear EventSet contains alternative
        # Sentinel/Goblin positions. The server selects and publishes one
        # actor; activating the same flag in the client makes it instantiate
        # every authored candidate locally. Keep the flag active in the
        # server runtime for quest and prop handling, but suppress it on the
        # client for these maps on every snapshot.
        candidate_spawn_events = (
            runtime.content.a_mighty_threat_spawn_event_keys(catalog)
        )
        suppressed_client_events = active_flags & set(
            candidate_spawn_events
        )
        if target_map == "e_nye_dungeon_01":
            machine_events = {
                event_key for event_key in active_flags
                if _new_year_machine_event_stage(event_key) > 0
            }
            # The native EventSet creates Machine Babies before the server's
            # NewMonster actor arrives. Mercenaries can target that transient
            # client-side copy and kill it before authoritative spawn. Keep
            # the runtime flag for quest/state logic, but let the server's
            # explicit spawn packet create the only attackable actor.
            suppressed_client_events.update(machine_events)
        if (
            target_map == "e_stego_arena_01"
            and "e_stego_boss_spawn_01" in active_flags
        ):
            # The final-boss EventSet contains four alternatives. The server
            # selects and publishes one actor; activating this flag on the
            # client would instantiate all four authored candidates locally.
            suppressed_client_events.add("e_stego_boss_spawn_01")
        if target_map == _SARGON_THRONE_ROOM_MAP and "boss_death" in active_flags:
            # BOSS_death is attached to all six secret-room destinations and
            # each difficulty copy in this scene. Toggling it client-side
            # reveals every overlapping portal; the server creates one room.
            suppressed_client_events.add("boss_death")
        client_active_flags = active_flags - suppressed_client_events
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
                flags=(event_key in client_active_flags, True, False, False),
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
            f"changed={changed_event} active={active}, "
            f"snapshot_events={len(entries)}, active_events="
            f"{tuple(sorted(key for key in definitions if key in client_active_flags))}, "
            f"client_event_suppressed={tuple(sorted(suppressed_client_events))}, "
            f"sessions={len(sessions)}"
        )

    def _publish_event_props(
            self, runtime, sessions, event_sets, *, refresh_existing=False) -> None:
        with runtime.lock:
            authored_definitions = runtime.content.props_for_sets(event_sets)
            if runtime.content.is_dungeon:
                # Dungeon Easter eggs use the scaling loot id, which has no
                # retail loot rows in the client export. Resolve it to the
                # event's Festival Egg table before selecting variants so
                # duplicate difficulty copies at one spot collapse to one
                # clickable prop.
                festival_egg_loot_id = (
                    "CHEST_e_easter_EGG_ISLAND"
                    if map_key(runtime.map_id) == "e_easter_dun_01"
                    else "CHEST_e_easter_EGG_NORMAL"
                )
                authored_definitions = tuple(
                    replace(
                        definition,
                        loot_list_id=festival_egg_loot_id,
                    )
                    if _is_easter_dungeon_festival_egg(definition)
                    else definition
                    for definition in authored_definitions
                )
            definitions = runtime.select_props(authored_definitions)
            easter_eggs = tuple(
                definition for definition in definitions
                if _is_easter_dungeon_festival_egg(definition)
            )
            if easter_eggs and runtime.content.is_dungeon:
                # These maps author each logical egg once per difficulty
                # variation, sometimes at distinct transforms. Keep one
                # stable copy for each egg template in this map instance.
                chosen_by_template = {}
                for definition in sorted(
                    easter_eggs, key=lambda candidate: candidate.row_id,
                ):
                    chosen_by_template.setdefault(
                        definition.template_id.casefold(), definition,
                    )
                chosen_ids = {
                    definition.prop_id
                    for definition in chosen_by_template.values()
                }
                definitions = tuple(
                    definition for definition in definitions
                    if not _is_easter_dungeon_festival_egg(definition)
                    or definition.prop_id in chosen_ids
                )
                easter_eggs = tuple(
                    definition for definition in definitions
                    if _is_easter_dungeon_festival_egg(definition)
                )
            if easter_eggs and runtime.content.is_dungeon:
                try:
                    entry_position = load_local_spawn_position(
                        self.static_database_path, runtime.map_id,
                    )
                except (OSError, ValueError, sqlite3.Error):
                    entry_position = None
                if entry_position is not None:
                    # The authored Easter eggs are often placed deep inside
                    # the dungeon. Put the event-controlled copies near the
                    # player entry so the event is immediately discoverable.
                    # Keep the authored prop ids and loot bindings intact.
                    egg_positions = {
                        definition.prop_id: (
                            float(entry_position[0]) + (
                                8.0 + 3.0 * math.sqrt(index)
                            ) * math.cos(index * 2.399963229728653),
                            float(entry_position[1]),
                            float(entry_position[2]) + (
                                8.0 + 3.0 * math.sqrt(index)
                            ) * math.sin(index * 2.399963229728653),
                        )
                        for index, definition in enumerate(sorted(
                            easter_eggs,
                            key=lambda candidate: candidate.prop_id,
                        ))
                    }
                    definitions = tuple(
                        replace(
                            definition,
                            position=egg_positions[definition.prop_id],
                        )
                        if definition.prop_id in egg_positions
                        else definition
                        for definition in definitions
                    )
            definitions = tuple(
                replace(definition, instance_guid=ZERO_GUID)
                if _is_easter_dungeon_festival_egg(definition)
                else definition
                for definition in definitions
            )
            if map_key(runtime.map_id) == "e_nye_dungeon_01":
                # The Greed Fiend chest is also named by a legacy quest
                # Switch. Its appearance is gated by opening a Fireworks
                # Crate, not by accepting that quest; clear the quest binding
                # on this runtime copy so _quest_prop_entries sends it to all
                # players and PropInteraction treats it as an ordinary chest.
                definitions = tuple(
                    replace(definition, quest_switch_id="")
                    if (
                        definition.template_id.casefold()
                        == "treasure_monster_e_nye"
                        and definition.loot_list_id.casefold()
                        == "lootlist_treasure_monster_e_nye"
                    ) else definition
                    for definition in definitions
                )
                for definition in definitions:
                    existing = runtime.props.get(definition.prop_id)
                    if (
                        existing is not None
                        and definition.template_id.casefold()
                        == "treasure_monster_e_nye"
                    ):
                        existing.definition = replace(
                            existing.definition, quest_switch_id="",
                        )
            discarded_easter_prop_ids = ()
            if easter_eggs and runtime.content.is_dungeon:
                event_set_rows = {
                    int(definition.event_set_row)
                    for definition in easter_eggs
                }
                previous_states = {
                    int(prop_id): state
                    for prop_id, state in runtime.props.items()
                    if (
                        _is_easter_dungeon_festival_egg(state.definition)
                        and int(state.definition.event_set_row) in event_set_rows
                    )
                }
                selected_easter_ids = {
                    int(definition.prop_id) for definition in easter_eggs
                }
                # Keep the selected prop IDs registered on the client and
                # refresh their transforms with PropInfo. Discarding those
                # IDs and recreating them in the same update can make the
                # client drop the factory props entirely. Only retire the
                # excess difficulty copies.
                discarded_easter_prop_ids = tuple(sorted(
                    set(previous_states) - selected_easter_ids
                ))
                # Remove every old copy from the runtime, then reinsert only
                # the selected set while preserving its per-prop state.
                runtime.unpublish_props(previous_states)
                published = runtime.publish_props(
                    definition for definition in definitions
                    if not _is_easter_dungeon_festival_egg(definition)
                )
                for definition in easter_eggs:
                    previous = previous_states.get(int(definition.prop_id))
                    state = PropRuntimeState(
                        definition,
                        previous.state if previous is not None
                        else PROP_STATE_ACTIVE,
                    )
                    runtime.props[int(definition.prop_id)] = state
                published = (*published, *(
                    runtime.props[int(definition.prop_id)]
                    for definition in easter_eggs
                ))
            else:
                published = runtime.publish_props(definitions)
            if refresh_existing:
                # Replay authoritative states after a partial global refresh;
                # preserve already-used props rather than resetting them.
                published = tuple(
                    runtime.props[d.prop_id] for d in definitions
                    if d.prop_id in runtime.props
                )
            elif easter_eggs:
                published = tuple(
                    runtime.props[d.prop_id] for d in definitions
                    if d.prop_id in runtime.props
                )
        if not published:
            return
        if discarded_easter_prop_ids:
            packet, packet_bits = encode_discard_prop_batch(
                discarded_easter_prop_ids, actor_id=LOCAL_PLAYER_ACTOR_ID,
            )
            for viewer in sessions:
                CharacterService._send_world_loot_state_batch(
                    viewer, packet, packet_bits,
                    label="easter-egg-prop-refresh-discard",
                )
        _log(
            f"revealed {len(published)} prop(s) on {runtime.map_id}: "
            + ", ".join(sorted({
                state.definition.template_id for state in published
            })[:6])
        )
        sentinel_chests = tuple(
            state for state in published
            if "sentinel_lootchest_" in (
                f"{state.definition.instance_id} {state.definition.template_id}"
            ).casefold()
        )
        if sentinel_chests:
            _log(
                f"A Mighty Threat chest placement on {runtime.map_id}: "
                + "; ".join(
                    f"{state.definition.template_id}@{tuple(round(axis, 2) for axis in state.definition.position)}"
                    for state in sentinel_chests
                )
            )
        for viewer in sessions:
            prop_info_states = tuple(
                PropRuntimeState(
                    replace(state.definition, instance_guid=ZERO_GUID),
                    state.state,
                )
                if _darkwood_wendigo_factory_prop(
                    runtime, state.definition,
                ) else state
                for state in published
            )
            if prop_info_states:
                _send_prop_info(
                    viewer, prop_info_states, label="map-event-prop-info",
                )

    def _unpublish_event_props(self, runtime, sessions, event_sets) -> None:
        with runtime.lock:
            event_set_rows = {int(row) for row in event_sets}
            states = tuple(
                state for state in runtime.props.values()
                if int(state.definition.event_set_row) in event_set_rows
            )
            prop_ids = tuple(state.definition.prop_id for state in states)
            removed = runtime.unpublish_props(prop_ids)
        if not removed:
            return
        _log(f"hid {len(removed)} prop(s) on {runtime.map_id}")
        # DiscardProp removes the client's prop-id lookup but can leave the
        # scene entity's PropId attribute attached. If an authored scene prop
        # is revealed again, PropInfo then resolves its GUID to that entity
        # and asserts that no prop id has been assigned. EventUpdate already
        # hides authored scene objects, so only discard zero-GUID props that
        # the server factory-created and which have no scene entity to retain.
        # Ritual scene props are factory-created on the wire and must also be
        # discarded when their EventSet closes.
        scene_prop_ids = {
            state.definition.prop_id
            for state in states
            if state.definition.instance_guid != ZERO_GUID
            and not _darkwood_wendigo_factory_prop(
                runtime, state.definition,
            )
        }
        discard_ids = tuple(
            prop_id for prop_id in removed if prop_id not in scene_prop_ids
        )
        if not discard_ids:
            return
        for viewer in sessions:
            packet, packet_bits = encode_discard_prop_batch(
                discard_ids, actor_id=LOCAL_PLAYER_ACTOR_ID
            )
            CharacterService._send_world_loot_state_batch(
                viewer, packet, packet_bits, label="map-event-discard-prop"
            )

    def _publish_map_exits(
            self, runtime, sessions, *, event_sets=None,
            active_override=None, update_session_state=True,
            disabled_event_sets=()) -> None:
        disabled_rows = frozenset(int(row) for row in disabled_event_sets)
        shadowfort_locked_rows = frozenset()
        if map_key(runtime.map_id) == SARGON_SHADOWFORT_MAP_ID:
            with runtime.lock:
                sentinel_defeated = runtime.sargon_shadowfort_sentinel_defeated
            if not sentinel_defeated:
                shadowfort_locked_rows = frozenset(
                    event_set.row
                    for event_key in SARGON_SHADOWFORT_EXIT_EVENT_KEYS
                    for event_set in runtime.content.event_sets_for(event_key)
                )
                disabled_rows |= shadowfort_locked_rows
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
            if shadowfort_locked_rows:
                definitions = tuple(
                    definition for definition in definitions
                    if definition.event_set_row not in shadowfort_locked_rows
                )
            definitions = self._select_a_mighty_threat_waypoint_exits(
                runtime, definitions,
            )
        except Exception as exc:
            _log(f"exit refresh failed: {type(exc).__name__}: {exc}")
            return
        parallel_world_tiered_exit_map = (
            map_key(runtime.map_id) in _PARALLEL_WORLD_TIERED_EXIT_MAPS
        )
        tiers = (
            _exit_access_difficulties(str(self.static_database_path))
            if parallel_world_tiered_exit_map else {}
        )
        available = (
            {
                tiers[d.destination_map_id] for d in definitions
                if d.destination_map_id in tiers
            }
            if parallel_world_tiered_exit_map else set()
        )
        if event_sets is not None:
            wanted_event_sets = {int(value) for value in event_sets}
            definitions = tuple(
                d for d in definitions
                if d.event_set_row in wanted_event_sets
            )
        if not definitions and not disabled_rows:
            return
        for viewer in sessions:
            with viewer.flow_lock:
                if getattr(viewer, "gameplay_exit_transition_started", False):
                    continue
                if disabled_rows and (
                    viewer.gameplay_map != runtime.map_id
                    or getattr(viewer, "gameplay_map_event_runtime", None) is not runtime
                ):
                    continue
                generation = getattr(viewer, "gameplay_chase_generation", None)
                previous_exits = tuple(getattr(viewer, "gameplay_map_exits", ()))
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
                if parallel_world_tiered_exit_map else 0
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
            if map_key(runtime.map_id) == _SARGON_THRONE_ROOM_MAP:
                with runtime.lock:
                    secret_room_group = (
                        runtime.sargon_throne_secret_room_group
                    )
                viewer_definitions = tuple(
                    definition for definition in viewer_definitions
                    if definition.destination_map_id.casefold() == "home"
                    or (
                        secret_room_group is not None
                        and definition.destination_map_id.casefold().startswith(
                            secret_room_group
                        )
                    )
                )
            if map_key(runtime.map_id) == "e_bos_arena_01":
                with runtime.lock:
                    chosen_circus_exit = runtime.circus_random_exit_guid
                viewer_definitions = tuple(
                    definition for definition in viewer_definitions
                    if chosen_circus_exit is not None
                    and bytes(definition.instance_guid) == chosen_circus_exit
                )
            bloodmoon_portal_locked = (
                map_key(runtime.map_id) == "e_fullmoon_varholm_dun"
                and not CharacterService._varholm_bloodmoon_portal_unlocked(
                    self, viewer,
                )
            )
            if bloodmoon_portal_locked:
                viewer_definitions = tuple(
                    definition for definition in viewer_definitions
                    if not definition.exit_id.casefold().startswith(
                        "varholm_to_bloodmoon_"
                    )
                )
            native_exit_entries = native_scene_exit_variation_entries(
                runtime.map_id, viewer_definitions,
            )
            native_exit_guids = {
                entry.instance_guid for entry in native_exit_entries
            }
            if native_exit_entries and active_override is not False:
                if disabled_rows:
                    with viewer.flow_lock:
                        if (
                            viewer.gameplay_map != runtime.map_id
                            or getattr(viewer, "gameplay_map_event_runtime", None) is not runtime
                            or getattr(viewer, "gameplay_chase_generation", None) != generation
                            or getattr(viewer, "gameplay_exit_transition_started", False)
                        ):
                            continue
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
                elif map_key(runtime.map_id) == "e_dra_03_dun_01":
                    _log(
                        f"published Dragan Battleground entrance on "
                        f"{runtime.map_id}: "
                        f"destinations={tuple(sorted({
                            definition.destination_map_id
                            for definition in viewer_definitions
                            if definition.instance_guid in native_exit_guids
                        }))}"
                    )
                elif map_key(runtime.map_id) == "e_bos_arena_01":
                    _log(
                        f"published selected Circus boss gate entity on "
                        f"{runtime.map_id}: "
                        f"guids={tuple(entry.instance_guid.hex() for entry in native_exit_entries)} "
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
            if parallel_world_tiered_exit_map:
                # Parallel World exits have one selected variation per tier.
                # Keep the staged scene-creation flow for every such map.
                # Boss rooms also have a separate staged exit flow; keep its
                # explicit per-entry active override unchanged.
                exit_info_entries = tuple(
                    ExitInfoEntry(
                        definition.name,
                        exit_display_map_id(
                            runtime.map_id, definition.destination_map_id,
                        ),
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
                        exit_display_map_id(
                            runtime.map_id, definition.destination_map_id,
                        ),
                        int(entry_active),
                        definition.instance_guid,
                    )
                    for definition, entry_active in exit_entries
                )
            if disabled_rows:
                # ExitInfo merges into the client's cache. Close only exits
                # this viewer actually knew; publishing arbitrary inactive
                # rows would also cache unselected difficulty variations.
                active_guids = {
                    definition.instance_guid for definition in active_viewer_definitions
                }
                exit_info_entries += tuple(
                    ExitInfoEntry(
                        definition.name,
                        exit_display_map_id(
                            runtime.map_id, definition.destination_map_id,
                        ),
                        0,
                        definition.instance_guid,
                    )
                    for definition in previous_exits
                    if definition.event_set_row in disabled_rows
                    and definition.instance_guid not in active_guids
                )
            if bloodmoon_portal_locked:
                # ExitInfo merges into the client cache, so an exit opened by
                # another player on this shared map must be explicitly closed
                # for this character rather than merely omitted on refresh.
                exit_info_entries += tuple(
                    ExitInfoEntry(
                        definition.name,
                        exit_display_map_id(
                            runtime.map_id, definition.destination_map_id,
                        ),
                        0,
                        definition.instance_guid,
                    )
                    for definition in previous_exits
                    if definition.exit_id.casefold().startswith(
                        "varholm_to_bloodmoon_"
                    )
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
                if disabled_rows and (
                    getattr(viewer, "gameplay_map_event_runtime", None) is not runtime
                    or getattr(viewer, "gameplay_chase_generation", None) != generation
                    or getattr(viewer, "gameplay_exit_transition_started", False)
                ):
                    continue
            CharacterService._send_world_loot_state_batch(
                viewer, packet, packet_bits, label="map-event-exit-info"
            )
            if update_session_state:
                with viewer.flow_lock:
                    if (viewer.gameplay_map == runtime.map_id
                            and getattr(viewer, "gameplay_chase_generation", None) == generation
                            and not getattr(viewer, "gameplay_exit_transition_started", False)):
                        viewer.gameplay_map_exits = tuple(active_viewer_definitions)
            if parallel_world_tiered_exit_map:
                _log(
                    f"tiered Parallel World exits for {getattr(viewer, 'addr', '?')}: access="
                    f"{getattr(viewer, 'gameplay_map_access_id', '')!r} "
                    f"difficulty={selected_tier}; wire_guids={len(exit_entries)}; "
                    f"stage={'closed' if active_override is False else 'open'}; active="
                    f"{[d.destination_map_id for d in active_viewer_definitions]}"
                )
        _log(
            f"refreshed exits on {runtime.map_id} for "
            f"{len(sessions)} session(s)"
        )

    @staticmethod
    def _select_a_mighty_threat_waypoint_exits(runtime, definitions):
        """Keep the fast-travel waypoint at the selected Sentinel location.

        Scaling maps author one fast-travel exit at each possible Sentinel
        point. The map event activates their shared EventSet, so forwarding all
        active exits can place the waypoint at an unrelated authored point.
        Keep the exit cluster closest to the single Sentinel selected for this
        map run, while leaving the other exits and difficulty variants alone.
        """

        content = runtime.content
        catalog = runtime.catalog
        if not content.a_mighty_threat_candidate_templates(catalog):
            return tuple(definitions)
        with runtime.lock:
            selected = runtime.scaling_dungeon_sentinel_spawn
            enabled_sets = frozenset(runtime.enabled_event_sets)
        if selected is None:
            return tuple(definitions)
        spawn = next((
            row for row in content.spawn_rows
            if int(row.row_id) == int(selected[0])
        ), None)
        if spawn is None:
            return tuple(definitions)
        with runtime.lock:
            target_position = runtime.a_mighty_threat_position or spawn.position
        fast_travel_sets = frozenset(
            int(row)
            for row, event_set in content.event_sets.items()
            if any(
                "scaling_map_fast_travelpoint_start" in str(event).casefold()
                for event in event_set.events
            )
        )
        fast_travel_sets &= enabled_sets
        candidates = tuple(
            definition for definition in definitions
            if int(definition.event_set_row) in fast_travel_sets
        )
        if not candidates:
            return tuple(definitions)
        nearest = min(
            math.hypot(
                float(definition.position[0]) - float(target_position[0]),
                float(definition.position[2]) - float(target_position[2]),
            )
            for definition in candidates
        )
        selected_candidates = tuple(
            definition for definition in candidates
            if math.hypot(
                float(definition.position[0]) - float(target_position[0]),
                float(definition.position[2]) - float(target_position[2]),
            ) <= nearest + 1.0
        )
        selected_guids = {
            definition.instance_guid for definition in selected_candidates
        }
        result = tuple(
            definition for definition in definitions
            if int(definition.event_set_row) not in fast_travel_sets
            or definition.instance_guid in selected_guids
        )
        _log(
            f"A Mighty Threat selected waypoint on {runtime.map_id} at "
            f"{target_position!r}; kept {len(selected_candidates)} exit "
            f"variant(s) from {len(candidates)} authored candidate(s)"
        )
        return result

    def _map_event_combat_state(self, sessions):
        for session in sessions:
            combat = getattr(session, "gameplay_combat_state", None)
            if combat is not None:
                return combat
        return None

    def _hide_event_npcs(self, runtime, sessions, event_sets) -> None:
        """Disable registered event NPCs while retaining their actor identities."""

        wanted = frozenset(int(row) for row in event_sets)
        if not wanted:
            return
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
            _log(f"event NPC hide failed on {runtime.map_id}: {type(exc).__name__}: {exc}")
            return
        for viewer in sessions:
            with viewer.flow_lock:
                if (
                    viewer.gameplay_map != runtime.map_id
                    or getattr(viewer, "gameplay_map_event_runtime", None) is not runtime
                    or getattr(viewer, "gameplay_exit_transition_started", False)
                ):
                    continue
                generation = getattr(viewer, "gameplay_chase_generation", None)
                announced = frozenset(viewer.gameplay_announced_npc_actor_ids)
            commands = tuple(
                replace(local_npc_info(definition), enabled=False)
                for definition in definitions
                if local_npc_actor_id(definition) in announced
            )
            if not commands:
                continue
            packet, packet_bits = encode_npc_batch(
                NPCBatch(commands), include_actor_vicinity=False,
            )
            with viewer.flow_lock:
                if (
                    viewer.gameplay_map != runtime.map_id
                    or getattr(viewer, "gameplay_map_event_runtime", None) is not runtime
                    or getattr(viewer, "gameplay_chase_generation", None) != generation
                    or getattr(viewer, "gameplay_exit_transition_started", False)
                ):
                    continue
            CharacterService._send_world_loot_state_batch(
                viewer, packet, packet_bits, label="map-event-hide-npc"
            )

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
        if (
            map_key(runtime.map_id) == "e_nye_dungeon_01"
            and runtime.new_year_encounter_cleared
        ):
            return
        gated_rows = runtime.gated_spawn_rows(event_sets)
        extra_rows = tuple(extra_rows)
        if map_key(runtime.map_id) == "e_nye_dungeon_01":
            gated_rows = tuple(
                row for row in gated_rows
                if not _is_new_year_machine_baby(row.template_id)
            )
        rows = (*gated_rows, *extra_rows)
        if map_key(runtime.map_id) == _SARGON_THRONE_ROOM_MAP:
            with runtime.lock:
                phase_two_released = (
                    runtime.sargon_throne_first_phase_defeated
                    and runtime.sargon_throne_second_phase_authorized
                    and runtime.sargon_throne_second_phase_started
                )
            if not phase_two_released:
                blocked_rows = tuple(
                    row for row in rows
                    if str(row.template_id).casefold()
                    in _SARGON_SECOND_PHASE_MONSTER_TEMPLATES
                )
                if blocked_rows:
                    blocked_ids = tuple(
                        (int(row.row_id), str(row.template_id))
                        for row in blocked_rows
                    )
                    rows = tuple(
                        row for row in rows
                        if str(row.template_id).casefold()
                        not in _SARGON_SECOND_PHASE_MONSTER_TEMPLATES
                    )
                    _log(
                        "blocked Sargon phase-2 spawn before the first phase "
                        f"and quest authorization: rows={blocked_ids!r}"
                    )
        rows = select_mortis_spawn_rows(self.static_database_path, runtime, rows)
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
        # Resolve this map's actor templates from its fast-travelpoint kill
        # rules. Standard scaling maps and Qaizah use different EventSet
        # names, but both signals list the one Sentinel and regional Goblin.
        a_mighty_candidates = runtime.content.a_mighty_threat_candidate_templates(
            runtime.catalog,
        )
        a_mighty_spawn_sets = (
            runtime.content.a_mighty_threat_spawn_event_set_rows(
                runtime.catalog,
            )
        )
        special_rows = tuple(
            row for row in rows
            if int(row.event_set_row) in a_mighty_spawn_sets
            and str(row.template_id).casefold() in a_mighty_candidates
        )
        sentinel_rows = tuple(
            row for row in special_rows
            if "treasure_monster" not in str(row.template_id).casefold()
        )
        if special_rows:
            with runtime.lock:
                selected_sentinel = runtime.scaling_dungeon_sentinel_spawn
                if selected_sentinel is None:
                    goblin_rows = tuple(
                        row for row in special_rows
                        if "treasure_monster" in str(row.template_id).casefold()
                    )
                    goblin_templates = tuple(sorted(
                        template for template in a_mighty_candidates
                        if "treasure_monster" in template
                    ))
                    if sentinel_rows:
                        point = random.choice(sentinel_rows)
                        template_id = point.template_id
                        if goblin_templates and random.random() < 0.15:
                            # The treasure monster replaces the Sentinel at
                            # the chosen Sentinel location. Its separate
                            # authored rows are alternate map positions, not
                            # a second location to use for the replacement.
                            template_id = random.choice(goblin_templates)
                    else:
                        point = random.choice(goblin_rows or special_rows)
                        template_id = point.template_id
                    selected_sentinel = (int(point.row_id), str(template_id))
                    runtime.scaling_dungeon_sentinel_spawn = selected_sentinel
            selected_row_id, selected_template_id = selected_sentinel
            selected_row = next(
                (row for row in special_rows if int(row.row_id) == selected_row_id),
                special_rows[0],
            )
            a_mighty_selected_position = selected_row.position
            # These map-clear EventSets also contain regular rows and broad
            # density areas at every possible Sentinel location. Keep only
            # the selected actor and the small companion pack physically
            # around it; otherwise a single map-clear signal can repopulate
            # the whole map, sometimes hundreds of actors on Qaizah maps.
            companion_rows = []
            companion_keys = set()
            for row in rows:
                if int(row.event_set_row) not in a_mighty_spawn_sets:
                    continue
                if row.template_id.casefold() in a_mighty_candidates:
                    continue
                distance = math.hypot(
                    float(row.position[0]) - float(a_mighty_selected_position[0]),
                    float(row.position[2]) - float(a_mighty_selected_position[2]),
                )
                if distance > 15.0:
                    continue
                key = (
                    int(row.event_set_row), row.template_id.casefold(),
                    tuple(round(float(value), 2) for value in row.position),
                )
                if key in companion_keys:
                    continue
                companion_keys.add(key)
                companion_rows.append(row)
            rows = tuple(
                row for row in rows
                if int(row.event_set_row) not in a_mighty_spawn_sets
            ) + tuple(companion_rows) + (
                replace(selected_row, template_id=selected_template_id),
            )
            _log(
                f"A Mighty Threat on {runtime.map_id} selected one actor "
                f"{selected_template_id!r} at spawn row {selected_row_id}; "
                f"kept {len(companion_rows)} nearby companion row(s) and "
                f"suppressed {len(special_rows) - 1} alternate fixed row(s)"
            )
        if map_key(runtime.map_id) == "e_stego_arena_01":
            final_boss_rows = tuple(
                row for row in rows
                if int(row.event_set_row) == 1587
                and row.template_id.casefold() in _STELLAR_GOLD_FINAL_BOSSES
            )
            if final_boss_rows:
                with runtime.lock:
                    selected_boss = runtime.stellar_gold_final_boss_spawn
                    if selected_boss is None:
                        selected_row = random.choice(final_boss_rows)
                        selected_boss = (
                            int(selected_row.row_id),
                            selected_row.template_id.casefold(),
                        )
                        runtime.stellar_gold_final_boss_spawn = selected_boss
                rows = tuple(
                    row for row in rows
                    if row.template_id.casefold() not in _STELLAR_GOLD_FINAL_BOSSES
                    or (
                        int(row.row_id) == selected_boss[0]
                        and row.template_id.casefold() == selected_boss[1]
                    )
                )
                _log(
                    f"Stellar Gold selected final boss {selected_boss[1]!r} "
                    f"from {len(final_boss_rows)} candidate(s) on {runtime.map_id}"
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
        if map_key(runtime.map_id) == "i0010_physical_big_dun":
            # The exported Trial of the Elders scene omits the MVR links for
            # its repeated guardian leader anchors. The event therefore sees
            # alternate difficulty rows as separate spawns; collapse them by
            # authored position before checking the live combat state.
            leader_prefixes = (
                "i0010_event_leader_guardian_physical",
                "i0010_pw_leader_guardian_physical",
            )
            leader_positions = []
            deduplicated_rows = []
            for row in rows:
                template_id = row.template_id.casefold()
                is_trial_leader = template_id.startswith(leader_prefixes)
                if is_trial_leader and any(
                    math.hypot(
                        float(row.position[0]) - x,
                        float(row.position[2]) - z,
                    ) <= EVENT_SPAWN_DUPLICATE_RADIUS
                    for x, z in leader_positions
                ):
                    continue
                deduplicated_rows.append(row)
                if is_trial_leader:
                    leader_positions.append((
                        float(row.position[0]), float(row.position[2]),
                    ))
            removed_leaders = len(rows) - len(deduplicated_rows)
            rows = tuple(deduplicated_rows)
            if removed_leaders:
                _log(
                    f"collapsed {removed_leaders} stacked Trial of the "
                    f"Elders leader row(s) on {runtime.map_id}"
                )
        # Collapse stacked variation rows only after the Mortis pack and boss
        # difficulty selections: those pick specific rows out of the stacks
        # (Mortis phase 2 keeps 1530 rows in EventSet 4216), so collapsing
        # them at load time left the selections nothing to spawn.
        rows = deduplicate_parallel_world_gatekeeper_rows(runtime.map_id, rows)
        combat = self._map_event_combat_state(sessions)
        if combat is None:
            _log(
                f"{len(rows)} gated spawn row(s) on {runtime.map_id} but no "
                "combat state on the instance"
            )
            return
        encounter = combat.encounter
        database_path = encounter.static_database_path
        area_rows = event_area_spawn_rows(
            runtime, encounter, event_sets,
            a_mighty_threat_event_sets=a_mighty_spawn_sets,
            a_mighty_threat_position=(
                a_mighty_selected_position if special_rows else None
            ),
            a_mighty_threat_max_adds=5,
        )
        rows += area_rows
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
            scripted_mortis_mirror = row.template_id.casefold() in {
                "pw212_sentinel_uniskel_mirror_self",
                "pw212_sentinel_uniskel_dwarf_mirror_self",
            }
            hazard = is_map_hazard(database_path, row.template_id)
            mounted = is_tower_mechanism(
                database_path, runtime.map_id, row.template_id,
            )
            preserve_trial_authored_position = (
                map_key(runtime.map_id) == "i0010_physical_big_dun"
                and (
                    row.template_id.casefold().startswith((
                        "i0010_event_leader_guardian_physical",
                        "i0010_pw_leader_guardian_physical",
                    ))
                    or row.template_id.casefold()
                    == "e_easter_01_dun_boss_troll_01"
                )
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
                not scripted_cultist and not mounted and not hazard
                and not preserve_trial_authored_position
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
            mirror_owner = (
                next((
                    viewer for viewer in sessions
                    if getattr(viewer, "gameplay_character", None) is not None
                ), None)
                if scripted_mortis_mirror else None
            )
            mirror_visual_template = None
            runtime_definition = _load_runtime_definition(
                database_path,
                row.template_id,
                actor_id,
                stationary=(
                    mounted or hazard or scripted_mortis_mirror
                    or is_new_year_dungeon_inert_helper(row.template_id)
                    or row.template_id.casefold()
                    == "e_easter_01_boss_egg_easter"
                ),
            )
            if scripted_mortis_mirror and mirror_owner is not None:
                class_name = CLASS_NAMES.get(
                    int(mirror_owner.gameplay_character.class_id), "warrior",
                ).casefold()
                attack_template = _MORTIS_MIRROR_ATTACK_TEMPLATES.get(
                    class_name,
                    _MORTIS_MIRROR_ATTACK_TEMPLATES["warrior"],
                )
                mirror_combat = _load_runtime_definition(
                    database_path, attack_template, actor_id,
                )
                if mirror_combat is not None:
                    runtime_definition = mirror_combat
                    mirror_visual_template = _mortis_mirror_client_template(
                        str(database_path), attack_template,
                    )
            if runtime_definition is None:
                _log(f"no runtime template for {row.template_id!r}; skipped")
                continue
            if (
                map_key(runtime.map_id) == "e_nye_dungeon_01"
                and _is_new_year_machine_baby(row.template_id)
            ):
                # The four Machine Babies are fixed map machines. Their
                # authored coordinates are gameplay anchors, not patrol starts.
                runtime_definition = replace(runtime_definition, run_speed=0.0)
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
                    health_multiplier=_bgh_boss_health_multiplier(
                        runtime.map_id, row.template_id,
                    ),
                    non_targetable=(
                        hazard or is_tower_hardware(
                            database_path, runtime.map_id, row.template_id,
                        )
                        or _is_new_year_undestroyable_tower(
                            runtime.map_id, row.template_id,
                        )
                    ),
                    rank_override=(
                        "rank_sentinel"
                        if map_key(runtime.map_id) == "e_nye_dungeon_01"
                        and _is_new_year_machine_baby(row.template_id)
                        else ""
                    ),
                )
            except ValueError as exc:
                _log(f"cannot load {row.template_id!r}: {exc}")
                continue
            if scripted_mortis_mirror and mirror_owner is not None:
                # NewMonster carries an optional name override after its rank
                # string. Use the player's name, while the base class template
                # lets the client resolve a localized title instead of showing
                # an untranslated *_1_template key in the boss bar.
                mirror_name = str(mirror_owner.gameplay_character.name or "")
                if mirror_name:
                    monster = replace(monster, field_f0=mirror_name)
                if mirror_visual_template:
                    # Preserve the authored Mortis id in server combat and
                    # quest state while the client renders the class model and
                    # resolves its localized title.
                    monster = replace(
                        monster, template_id=mirror_visual_template,
                    )
            if mounted:
                monster = replace(monster, field_a0=float(row.rotation))
            if scripted_cultist:
                monster = replace(monster, field_a0=float(row.rotation))
                _log(
                    f"tutorial cultist authored placement: "
                    f"position={monster.position!r}, yaw={row.rotation:.6f}"
                )
            if map_key(runtime.map_id) == "pw214_mortis_boss":
                monster = replace(monster, field_c4=max(
                    (int(getattr(s, "gameplay_chase_server_tick", 0) or 0) for s in sessions),
                    default=0,
                ) & 0xFFFFFFFF)
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
            # The class-matched template above is client-facing only. Keep
            # kill and quest bookkeeping attached to the authored mirror id.
            for row, monster in zip(spawned_rows, monsters, strict=True):
                if row.template_id.casefold() not in {
                    "pw212_sentinel_uniskel_mirror_self",
                    "pw212_sentinel_uniskel_dwarf_mirror_self",
                }:
                    continue
                registered = combat.monsters.get(int(monster.actor_id))
                if registered is not None:
                    combat.monsters[int(monster.actor_id)] = replace(
                        registered, template_id=str(row.template_id),
                    )
        mortis = mortis_arena(self, runtime)
        if mortis is not None:
            mortis.spawned(combat, monsters)
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
                            map_key(combat.encounter.map_id)
                            == "e_viscanium_submarine_01"
                            and combat.monsters[actor_id].rank_id.casefold()
                            in {
                                "rank_champion", "rank_leader",
                                "rank_sentinel",
                            }
                        )
                        or (
                            map_key(combat.encounter.map_id)
                            == "e_stego_arena_01"
                            and combat.monsters[actor_id].rank_id.casefold()
                            == "rank_sentinel"
                        )
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
