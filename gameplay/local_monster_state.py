"""Build deterministic local monster encounters from editable game data."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from functools import cached_property, lru_cache
import math
import os
import sqlite3
import struct
import threading
from pathlib import Path

from dso_server.runtime import resident_content
from dso_server.runtime.gc_tuning import freeze_long_lived_addition
from dso_server.protocol.combat_protocol import (
    BULLET_SKILL_COMMAND,
    SHIFTED_SKILL_COMMAND,
    SKILL_COMMAND,
    TARGET_BULLET_SKILL_COMMAND,
    TARGET_POINT_BULLET_SKILL_COMMAND,
    TARGET_SKILL_COMMAND,
)
from dso_server.protocol.gameplay_protocol import (
    ActorPlacement,
    ActorsEnterVicinity,
    ActorsEnterVicinityBatch,
    MonsterSpawnBatch,
    MonsterStat,
    NewMonster,
    NewMonsterServerCommand,
    StatusEffect,
    StatusEffectEntry,
    StatusEffectServerCommand,
)
from dso_server.gameplay.local_map_state import (
    MapCoordinateFrame,
    _enabled_event_set_ids,
    active_local_map_event_flags,
    load_map_coordinate_frame,
    load_local_spawn_position,
    map_database_path,
)
from dso_server.gameplay.map_collision_terrain import (
    CollisionTerrainError,
    MapCollisionTerrain,
    load_map_collision_terrain,
)
from dso_server.gameplay.fractured_planes_layout import (
    TOWER_ATTACK_RANGE_MULTIPLIER, TOWER_ATTACK_RATE_MULTIPLIER,
    TOWER_FIRE_BURST_INTERVAL, TOWER_FIRE_PROJECTILE_SPEED_MULTIPLIER,
    TOWER_FIRE_ACQUISITION_RANGE,
    TOWER_MIN_ATTACK_INTERVAL,
    is_tower_controller, is_tower_hardware, is_tower_layout,
    is_tower_mechanism,
    tower_density_templates,
)
from dso_server.gameplay.map_hazards import is_map_hazard
from dso_server.gameplay.pathengine_navigation import (
    NavigationError,
    NavigationRoute,
    PathEngineNavigationMesh,
    load_navigation_mesh,
)
from dso_server.gameplay.boss_encounter_policy import (
    density_spawn_templates,
    is_boss_spawn_template,
    load_map_encounter_policy,
    resolve_boss_template_id,
    select_boss_spawn_rows,
)
from dso_server.gameplay.map_event_state import (
    load_map_event_content,
    load_map_signal_catalog,
)



# The client map database names this server-owned spawn list but does not
# contain its list membership. The template and generated locations below are
# recovered fields from the live b1201 encounter, represented as ordinary
# state rather than an opaque packet replay.
_LOCAL_ENCOUNTERS = {
    "b1201_pve": {
        "spawn_list_id": "entrance_gnome_troll",
        "template_id": "b1201_s_gen_norsegnome",
        "actor_ids": (65703, 65704),
        "positions": (
            (-59.8599967956543, 2.0, 29.099998474121094),
            (-59.08686065673828, 2.0, 28.326862335205078),
        ),
        "trigger_radius": 24.0,
    },
}

# Torstrond's authored Inquisitor SpawnPoint has no NpcId, although its
# ungated guardian row is intended to create this renderable map actor.
_AUTHORED_SPAWNPOINT_TEMPLATE_OVERRIDES = {
    ("b1101_pve", "b1101_s_undead_mage_q_silentscreams06_spawnpoint"):
        "b1101_s_undead_mage_q_SilentScreams06",
}

# c2202_pve's extracted map database omits the four Gorgon Guard spawnpoints
# used by WarriorsDestiny-02. Keep the four cages playable without modifying
# the client map data by spawning those quest guards around the cage area.
_WARRIORS_DESTINY_GORGON_FALLBACK_SPAWNS = (
    (-12.0, 0.0, 10.0),
    (8.0, 0.0, 10.0),
    (-12.0, 0.0, -2.0),
    (8.0, 0.0, -8.0),
)
_WARRIORS_DESTINY_GORGON_TEMPLATE = (
    "c2202_normal_s_gorgon_q_WarriorsDestiny_02"
)

LOCAL_MONSTER_ACTOR_BASE = 0x00030000
_CLIENT_SCENE_ACTOR_ID_MAPS = frozenset({
    "a0008_crypt_dun",
    "a0302_wildforest",
    "a0303_wildcave",
    # The Immortals event maps register authored SpawnPoints in the ordinary
    # actor range; keep server-published quest mobs clear of those IDs.
    "ce202502",
    "ce202503",
    "ce202504",
    # Greyling Forest has eight authored SpawnPoints (quest Wisps, unique
    # creatures, and scene mobs). The client can register these in the normal
    # 0x30000 actor range, so server monsters must use the secondary range.
    "k0002_greylingforest",
    # Tidal Wildwood has hundreds of authored SpawnPoints, including its
    # Lord of Ashes encounter. The client scene can reserve these ordinary
    # actor IDs before server monsters are announced.
    "k0003_tidalwildwood",
    # Torsrond's client scene registers authored SpawnPoints in the ordinary
    # 0x30000 range. Keep server controlled quest actors such as Captain
    # Ulkell in the secondary actor range to avoid that collision.
    "b1101_pve",
    # Eternal Watch reserves ordinary actor ids for its six authored quest
    # SpawnPoints, including the Fate of the King frost-catchers.
    "b1202_dun",
    # Time Rift's client scene owns the ordinary range for its authored
    # Gorgon and Zorlob quest SpawnPoints. Keep server actors in the free range.
    "c2302_dun",
    # Temple of Eternity has authored quest SpawnPoints on separate walkable
    # islands; its client scene also registers actors in the ordinary range.
    "c2303_dun",
    # Tegansruh's client scene already registers an actor at 0x30000. Keep
    # authored server monsters such as Benkay in the secondary actor range.
    "a0601_defiledsanctum",
    # Misty Ridge has one authored guardian SpawnPoint at row 1, which
    # overlaps the first server monster actor slot on clients that register
    # scene SpawnPoints in the ordinary 0x30000 range.
    "f4101_pve",
    # Fjalnir's Cradle of Frost has its quest child at NPC row 1 while the
    # Ice Guardian occupies the first server monster slot. Keep the monster
    # out of slot 0 so its status effects cannot resolve to that NPC.
    "g0005_temple_ice_01_dun",
    # Fjalnir's Temple registers its authored guardian SpawnPoints in the
    # ordinary actor range. Keep the server-spawned Ice Guardians in the
    # secondary range so the client scene cannot reserve their actor ids.
    "g0005_temple_ice_02_dun",
    # Fjalnir's Cradle of Frost also registers authored guardian SpawnPoints
    # in the ordinary range; keep corrupted Ice Guardians out of those ids.
    "g0005_temple_ice_03_dun",
    # Fyrgon's Fire Path starts with authored Fire Guardian SpawnPoints in
    # the ordinary range. Keep server monsters out of the same actor ids.
    "g0007_temple_fire_01_dun",
    # The Death Cradle client scene registers its authored SpawnPoints in the
    # ordinary range. Keep server monsters in the secondary range so those
    # scene actors cannot mask mobs in the Blood Cascade and Skull Pit.
    "g0011_temple_death_01_dun",
    # Okeanos's Opal Temple has its water guardian at NPC row 1 while the
    # Toltac Shaman occupies monster slot 0. Keep them in separate slots.
    "g0006_temple_water_01_dun",
    # The Cradle of Life's client scene registers authored guardian
    # SpawnPoints in the ordinary actor range. Keep the Life Guardian and
    # server-spawned monsters in the secondary range to prevent collisions.
    "g0010_temple_life_01_dun",
    # Artaya's special chamber registers its three quest guardians as client
    # scene actors in the ordinary range. Keep their server actors separate.
    "g0015_temple_life_md_01",
    # Sunset has Ozzlan as authored SpawnPoint row 1. The client scene can
    # register that row at 0x30000, hiding the server's first monster actor.
    "g0004_corrupted_jungle_01_pve",
    # Pırıltıdağ's scene registers authored SpawnPoints in the ordinary
    # monster actor range too. The first row is Commander Slavkoff, so starting
    # server monsters at 0x30000 collides with his client scene actor and hides
    # the mob. Keep this map's server actors in the secondary range.
    "f4102_dun",
    # The Great Desert has a large authored SpawnPoint scene population, including
    # the Shadaigan quest enemies. Keep server monsters outside the scene's
    # ordinary actor ID range so those actors can be registered independently.
    "h0002_desert_pve",
    # Green Oasis has over 1,500 authored SpawnPoints; keep its server monsters
    # beyond the actor IDs occupied by the client scene.
    "h0003_oasis_dun",
    # Dikenli Valley has over 2,400 authored SpawnPoints, including the
    # quest-gated Qarinah hunters. Keep server-published monsters in the
    # secondary range so client scene actors cannot mask them.
    "h0006_valley_dun",
    # Cardhun's entrance and suburb scenes also register authored SpawnPoints
    # in the ordinary range. In particular, the suburb has 442 rows across
    # its difficulty variants, overlapping the server's sequential spawn IDs.
    "pw199_cardhun_dun",
    "pw200_cardhun_dun",
    # Brigavik's event-driven Materi storm waves, including Mardoll, are
    # authored SpawnPoints that the client registers in its ordinary actor
    # range. Keep server-published monsters in the secondary range so those
    # scene actors cannot mask the wave bosses.
    "pw203_norseland_01_dun",
    # Telepolos has 104 authored SpawnPoints. The client may register these
    # scene actors in the ordinary monster range, so keep its server density
    # population in the secondary range to prevent actor-id collisions.
    "pw205_helios_dun",
    # The Toltac temple has hundreds of authored scene SpawnPoints; keep the
    # server's quest and density mobs outside their ordinary ActorID range.
    "pw206_temple_dun",
    # Fyeborough's quest-gated Soul Collector is also an authored SpawnPoint.
    # The client may already register that scene actor at 0x30000 before the
    # server releases its EventSet; publishing it at the same ID makes
    # NewMonster fail with ActorManager::RegisterActor duplicate-id.
    "pw207_village_dun",
    # Sunlair's Chamber has authored scene SpawnPoints in the ordinary local
    # actor range. The fixed Sunlair's Notes demon otherwise receives 0x30000,
    # which the client scene can already own, leaving the quest marker visible
    # while NewMonster has no renderable model.
    "pw207_chamber_dun",
    # Kinza's 83 scene SpawnPoints can occupy the same ordinary range used by
    # server actors. Keep quest-specific Enforcer waves clear of those IDs.
    "pw209_kinza_dun",
    # Panoltiaco's scene registers authored SpawnPoints in the ordinary actor
    # range. Senbu's gated boss spawn can otherwise reuse a client-owned ID and
    # remain invisible even though the server considers it materialized.
    "g0017_templecity_md_04",
})


def map_monster_actor_base(
    map_id: str, *, static_database_path: str | Path = "data/db/static.db4",
) -> int:
    from dso_server.gameplay.boss_encounter_policy import (
        APPROACH_BOSS_MAPS,
        load_map_encounter_policy,
    )

    # Client scene spawners and boss room entrances can reserve 0x30000.
    # To prevent colliding with client scene actors or NPC row 1 (0x10000 -> slot 0),
    # all boss rooms and approach arenas use the secondary actor slot range (+ 0x1000).
    is_boss = (
        map_id.casefold() in APPROACH_BOSS_MAPS
        # These client scenes own ids in the ordinary 0x30000 range for
        # authored SpawnPoints. Keep server monsters in the secondary range,
        # as on boss arenas, so NewMonster cannot collide with them.
        or map_id.casefold() in _CLIENT_SCENE_ACTOR_ID_MAPS
        # Mühürlü Mezar's g0013 map family also uses client-owned scene actor
        # ids; keep its Toltac High Shamans in the server-only range.
        or map_id.casefold().startswith("g0013_")
        or load_map_encounter_policy(static_database_path, map_id).is_boss_room
    )
    return LOCAL_MONSTER_ACTOR_BASE + (0x1000 if is_boss else 0)

# Tower maps already register their first authored mechanism at 0x30000 on
# some client builds. Keep every server-published actor on those layouts in a
# separate, stable actor-id range to avoid colliding with the client actor.
TOWER_MONSTER_ACTOR_BASE = LOCAL_MONSTER_ACTOR_BASE + 0x1000
MONSTER_GROUP_SIZE = 8
MONSTER_GROUP_RADIUS = 24.0
MONSTER_SPAWN_MINIMUM_SPACING = 0.75
TUTORIAL_GRIMMAG_MAP_ID = "a0005_start_fortress_dun"
TUTORIAL_GRIMMAG_TEMPLATE_ID = (
    "a0005_champion_grimmag_01_q_waytogrimmagstone_03"
)
# The Grimmag-specific spawn point is the arena anchor. EventSet 30's nearby
# add spawns are outside the boss room, so their centroid is not a valid boss
# position.
TUTORIAL_GRIMMAG_ARENA_POSITION = (
    -10.568790435791016,
    0.05000000074505806,
    50.022220611572266,
)
# The client reports an existing registration at the first default
# local-monster ID (0x30000) on this map. Keep the server-controlled concrete
# boss out of that sequential range so NewMonster cannot reuse the ID.
TUTORIAL_GRIMMAG_RUNTIME_ACTOR_ID = LOCAL_MONSTER_ACTOR_BASE + 0x1000
# The client also reports an existing registration at the first default
# monster ID in Lythira's scene. Use a distinct ID, as in the tutorial arena.
FRACTURED_PLANES_BOSS_MAP_ID = "fp0001_boss_01"
FRACTURED_PLANES_BOSS_TEMPLATE_ID = "fp_boss_01_lythira"
FRACTURED_PLANES_BOSS_ACTOR_ID = LOCAL_MONSTER_ACTOR_BASE + 0x1000
# Eternal Grove's ClearRegion quest requires these two commanders, but the
# shipped map database has no SpawnPoint rows for them. Keep them on the
# single-layer walkable path near the map entry so the client can discover
# them without searching remote, vertically-overlapped map edges.
ETERNAL_GROVE_MAP_ID = "pw011_eternal_grove_01_dun"
ETERNAL_GROVE_MISSING_COMMANDER_SPAWNS: tuple[
    tuple[str, tuple[float, float, float]], ...
] = (
    ("pw011_02_champion_troll_heroic", (-64.9, 10.0, 73.6)),
    # The guidance marker is pw011_spawn_leader near (-82.42, -61.83).
    # This nearby point is on single-layer walkable ground, clear of the
    # leader object and existing actors, so the missing commander is visible
    # where the map directs the player to look.
    ("pw011_02_champion_satyr_ranged_heroic", (-85.4, 8.0, -60.8)),
)
# The client never renders the stag commander that the map authors on
# Eternal Grove's north-east terrace (23.25, 18.0, -17.76). The server
# simulates it, the quest marker points at it, and the actor payload is
# byte-identical to the sibling stag that does render, so the terrace
# placement is what the client refuses. Replace that single commander with
# a different champion model on the main level beside the map entry, where
# the player always starts and where the other commanders were seen.
ETERNAL_GROVE_COMMANDER_REPLACEMENTS: tuple[
    tuple[str, tuple[float, float, float], str, tuple[float, float, float]],
    ...,
] = (
    (
        "pw011_01_champion_beastman_stag_heroic",
        (23.25, 18.0, -17.76),
        "pw011_01_champion_thing_roots_heroic",
        (-60.0, 10.0, 74.0),
    ),
)
# The Eternal Grove client scene already owns the second local-monster actor
# slot (0x30001). A NewMonster that reuses that slot never produces a model,
# and the quest indication still points at the actor, so the commander looks
# present but empty. Leave the client-owned slot alone and hand the authored
# population the slots after it.
ETERNAL_GROVE_RESERVED_ACTOR_IDS = frozenset({LOCAL_MONSTER_ACTOR_BASE + 1})
TRAVEL_STONE_MONSTER_EXCLUSION_RADIUS = max(
    8.0,
    float(os.environ.get("DSO_TRAVEL_STONE_MONSTER_RADIUS", "12")),
)


def _spawn_spacing_cell(
        position: tuple[float, float, float]) -> tuple[int, int]:
    return (
        math.floor(position[0] / MONSTER_SPAWN_MINIMUM_SPACING),
        math.floor(position[2] / MONSTER_SPAWN_MINIMUM_SPACING),
    )


def _spawn_position_is_occupied(
        occupied: dict[tuple[int, int], list[tuple[float, float, float]]],
        position: tuple[float, float, float]) -> bool:
    """Enforce real X/Z separation, including adjacent spatial cells."""

    cell_x, cell_z = _spawn_spacing_cell(position)
    minimum_squared = MONSTER_SPAWN_MINIMUM_SPACING ** 2
    return any(
        (other[0] - position[0]) ** 2
        + (other[2] - position[2]) ** 2
        < minimum_squared
        for x in range(cell_x - 1, cell_x + 2)
        for z in range(cell_z - 1, cell_z + 2)
        for other in occupied.get((x, z), ())
    )


def _occupy_spawn_position(
        occupied: dict[tuple[int, int], list[tuple[float, float, float]]],
        position: tuple[float, float, float]) -> None:
    occupied.setdefault(_spawn_spacing_cell(position), []).append(position)


def _eternal_grove_commander_replacement(
        map_id: str,
        template_id: str,
        position: tuple[float, float, float],
) -> tuple[str, tuple[float, float, float]]:
    """Swap the commander the client refuses to render for a visible one."""

    if str(map_id).casefold() != ETERNAL_GROVE_MAP_ID:
        return template_id, position
    for (
        source_template,
        source_position,
        target_template,
        target_position,
    ) in ETERNAL_GROVE_COMMANDER_REPLACEMENTS:
        if (
            str(template_id).casefold() == source_template.casefold()
            and abs(float(position[0]) - source_position[0]) <= 0.75
            and abs(float(position[2]) - source_position[2]) <= 0.75
        ):
            return target_template, target_position
    return template_id, position


SPAWN_AREA_MONSTERS_ENABLED = (
    os.environ.get("DSO_SPAWN_AREA_MONSTERS", "1").strip().lower()
    not in {"0", "false", "no", "off"}
)
SPAWN_AREA_MAX_MONSTERS = max(
    0, int(os.environ.get("DSO_SPAWN_AREA_MAX_MONSTERS", "0"))
)
# Multiplier on every spawn area's authored SpawnPerMeter density.  1.0 is
# the database value; the default trims the generated population by about
# 15% per map, which is the reduction the populated-map work asked for.
# (0.35 with a 250 cap below gives the sparser maps of the xm1rth fork.)
SPAWN_AREA_DENSITY_SCALE = max(
    0.0, float(os.environ.get("DSO_SPAWN_AREA_DENSITY_SCALE", "0.85"))
)
# Whole-map cap on the density-driven population.  768 was the historical
# value; the live packed-map maps (a0303, a0203, a0302, a0406) all sat exactly
# on it, and so do most large maps: their authored density asks for thousands.
# 162 is a quarter of the previous 650, so the big, cap-bound maps carry four
# times fewer generated monsters; maps already under it are unchanged, and
# hand-placed spawn points (bosses, champions) are not counted against it.
SPAWN_AREA_MAX_TOTAL_MONSTERS = max(
    0, int(os.environ.get("DSO_SPAWN_AREA_MAX_TOTAL_MONSTERS", "162"))
)

# Spawn-list membership is server-owned and is not shipped in static.db4.
# These names are descriptive enough to recover the exact b1201 membership;
# other maps use their map-scoped normal monster templates as a deterministic
# fallback.
_SPAWN_LIST_TEMPLATE_OVERRIDES = {
    # The Immortals event's SpawnList registry is absent from static.db4.
    # These map lists explicitly name the Forlorns and Mindless Fiends needed
    # by the event's main-quest kill objectives.
    "ce202502": {
        "ce202502_mindless_drifters": (
            "ce2025_monster_mindless_drifters",
        ),
        # The server-side SpawnList registry is absent from static.db4.
        # Grayshade's four "shijieshouwei" density areas are the Homunculi
        # wave for Lost Souls; falling back to the map's fixed actors can
        # populate them with unrelated templates that the quest cannot count.
        "shijieshouwei": (
            "ce2025_monster_shijieshouwei",
        ),
    },
    "ce202503": {
        "mindless_drifters": (
            "ce2025_monster_mindless_drifters",
        ),
        "ce202502_mindless_fiends": (
            "ce2025_monster_mindless_fiends",
        ),
    },
    "ce202504": {
        "ce202502_mindless_fiends": (
            "ce2025_monster_mindless_fiends",
        ),
        # Ashen Plains uses this density list for the Evil Eyes required by
        # the Aether Tears quest. Its server-side SpawnList is not exported.
        "monster_eye_demon": (
            "ce2025_monster_eye_demon",
        ),
    },
    # The Dragan fortress database has authored main-hall density areas with
    # an 8-minute respawn interval, but its list_mainhall membership is not
    # included in static.db4. Populate those areas with the map's ordinary
    # fortress enemies; elemental guards remain gated by their own EventSets.
    'e_dra_03_dun_01': {
        'list_mainhall': (
            'e_dra_fortress_normal_skeleton_warrior_01',
            'e_dra_fortress_normal_skeleton_archer_01',
            'e_dra_fortress_normal_undead_ripper_01',
            'e_dra_fortress_normal_death_knight_01',
        ),
    },
    # This story map uses the shared reactivation family, not an a0702 normal
    # family. Its named lists identify the supporting species; the only a0702
    # template is Sargon, placed once by _Instance_SpawnPoint.
    'a0702_kingscity_past_dun': {
        'skeleton': (
            'reactivation_normal_skeleton_warrior_01',
            'reactivation_normal_skeleton_archer_01',
        ),
        'spider': ('reactivation_normal_minispider_01',),
        'beast_hound': ('reactivation_normal_beast_hound_01',),
    },
    'e_doe_map_02': {
        'general_spawn_white': (
            'e_doe_map_02_normal_essence_minion_white_01',
            'e_doe_map_02_normal_ice_thing_white_01',
        ),
        'general_spawn_green': (
            'e_doe_map_02_normal_essence_minion_green_01',
            'e_doe_map_02_normal_ice_thing_green_01',
        ),
        'general_spawn_blue': (
            'e_doe_map_02_normal_essence_minion_blue_01',
            'e_doe_map_02_normal_ice_thing_blue_01',
        ),
        'general_spawn_yellow': (
            'e_doe_map_02_normal_essence_minion_yellow_01',
            'e_doe_map_02_normal_ice_thing_yellow_01',
        ),
        'general_spawn_red': (
            'e_doe_map_02_normal_essence_minion_red_01',
            'e_doe_map_02_normal_ice_thing_red_01',
        ),
        'general_spawn_purple': (
            'e_doe_map_02_normal_essence_minion_andermagic_01',
            'e_doe_map_02_normal_ice_thing_purple_01',
        ),
    },
    # Piritlidag's melt rooms need the authored DragonsForge kill target;
    # quest-specific templates are excluded from the generic map pool.
    'f4202_dun': {
        'melt': ('f4202_normal_g_dark_dwarf_q_DragonsForge_04',),
        'scale_melt': ('f4202_normal_g_dark_dwarf_q_DragonsForge_04_scale',),
    },
    # Telepolos' first A War Conspiracy objective clears this authored
    # density list. Its three quest-specific templates are intentionally
    # excluded from the generic map pool by the `_q_` filter; without an
    # explicit list mapping, the ClearRegionTask's targets never spawn.
    'pw205_helios_dun': {
        'list_imperial_soldier_q_helios_war_01': (
            'pw205_normal_imperial_soldier_offensive_q_helios_war_01',
            'pw205_normal_imperial_soldier_defensive_q_helios_war_01',
            'pw205_normal_cerberus_q_helios_war_01',
        ),
    },
    # A New Clue (2/2) clears this spawn list. The Toltac guards are quest-only
    # templates excluded from the generic monster pool.
    'pw206_temple_dun': {
        'list_tribe_q_way_to_lortac_02': (
            'pw206_normal_evil_tribe_melee_02_q_way_to_lortac_02',
            'pw206_normal_evil_tribe_ranged_02_q_way_to_lortac_02',
        ),
        # The Call for Freedom (2/2) starts a separate event-set wave. Its
        # ClearRegionTask tracks these four shadow variants specifically.
        'list_shadow_q_free_lortac_02': (
            'pw206_normal_evil_tribe_melee_shadow_q_free_lortac_02',
            'pw206_normal_evil_tribe_ranged_shadow_q_free_lortac_02',
            'pw206_normal_clawstrider_shadow_q_free_lortac_02',
            'pw206_normal_shadow_corpser_q_free_lortac_02',
        ),
    },
    "b1201_pve": {
        "entrance_gnome_troll": (
            "b1201_s_gen_norsegnome",
            "b1201_s_gen_troll",
        ),
        "entrance_dungeon_iceminion_swampthing": (
            "b1201_s_gen_ice_minion",
            "b1201_s_gen_thing_swamp",
        ),
        "lumberjack_swampthing_gnome": (
            "b1201_s_gen_thing_swamp",
            "b1201_s_gen_norsegnome",
        ),
    },
}


# These three quest maps have density areas but no map-prefix monster
# templates or fixed monsters to seed their default spawn pools.
_K_SERIES_QUEST_REGION_MONSTERS = (
    "necro_defender",
    "blazing_wolf",
    "test_spirit_archer_01",
    "test_spirit_warrior_01",
    "monster_wisp",
    "monster_slime",
    "tainted_sludge",
    "lord_of_ashes",
)
_MAP_MONSTER_TEMPLATE_ADDITIONS = {
    "k0002_greylingforest": _K_SERIES_QUEST_REGION_MONSTERS,
    "k0003_tidalwildwood": _K_SERIES_QUEST_REGION_MONSTERS,
    "k0004_hallofparadoxes": _K_SERIES_QUEST_REGION_MONSTERS,
}


@dataclass(frozen=True)
class MonsterAttackDefinition:
    """Database-owned autonomous skill and its matching wire command."""

    resource_id: str
    command_id: int
    skill_level: int
    skill_manager_index: int
    hit_frame: int
    motion_unblock_frame: int
    skill_unblock_frame: int
    attack_range: float
    hit_range: float
    bullet_emit_delay: float
    projectile_velocity: float
    projectile_radius: float
    user_status_effects: str = ""
    victim_status_effects: str = ""
    projectile_lifetime: float = 0.0
    motion_type: str = "Straight"
    start_offset: tuple[float, float, float, float] = (
        0.0, 0.0, 0.0, 0.0
    )
    # _Template_SkillBullet.StaticCollision: the bullet stops at walls.
    static_collision: bool = False
    loop_start_frame: int = 0
    damage_types: tuple[tuple[str, float], ...] = (("Physical", 1.0),)
    # _Template_Skill CoolDown (seconds), InitialCoolDown ("3|5": a first wait
    # drawn in that range once the monster fights), CoolDownCategory (skills
    # sharing one timer) and MinDifficulty/MaxDifficulty (the map difficulties
    # the variant is used on: boss_khalys_base_melee 0-3, _melee_plus 4-10).
    cooldown: float = 0.0
    initial_cooldown: tuple[float, float] = (0.0, 0.0)
    cooldown_category: str = ""
    min_difficulty: int = 0
    max_difficulty: int = 99
    damage_modifier: float = 1.0
    # A Melee skill with no damage whose UserStatusEffects buff the monster
    # itself (heredur_bonus_enrage): played as a SkillCommand, its effects put
    # on the caster at its start, no blow.
    self_buff: bool = False
    # A summon (_Template_Skill Summons): SummonMonsterId (else the caster
    # template's, SummonCategory FromMonsterTemplate), that many per cast, at
    # most SummonMonsterLimit of the caster's alive at once. A Shifted one is
    # played as a ShiftedSkillCommand on the caster's place (Bearach's hounds,
    # Heredur's skeletons), a Melee one as a SkillCommand (Arachna's spiders).
    summon_template: str = ""
    summon_amount: int = 0
    summon_limit: int = 0
    bullet_count: int = 1
    targeting_type: str = "Actor"
    player_width: float = 0.0
    end_width: float = 0.0
    angle_degrees: float = 0.0
    movement_skill: bool = False

    @property
    def support(self) -> bool:
        """A skill that strikes nobody: a buff on the caster or a summon."""

        return self.self_buff or bool(self.summon_template)


@dataclass(frozen=True)
class MonsterRuntimeDefinition:
    actor_id: int
    attack: MonsterAttackDefinition
    run_speed: float
    aggro_range: float
    attack_interval: float
    capsule_radius: float
    escape_range: float
    travel_distance: float
    attacks: tuple[MonsterAttackDefinition, ...] = ()
    startup_duration: float = 0.0


@dataclass(frozen=True)
class MonsterSpawnGroup:
    actor_ids: tuple[int, ...]
    trigger_center: tuple[float, float, float]
    trigger_radius: float
    vicinity: ActorsEnterVicinityBatch
    spawn_batch: MonsterSpawnBatch

    @cached_property
    def actor_id_set(self) -> frozenset[int]:
        """Membership view of :attr:`actor_ids`, built once per group.

        Vicinity culling and discovery both intersect a group's members with a
        per-viewer set on every MoveCommand. Rebuilding these from the tuple
        each time cost one set allocation per group per packet, at ten packets
        per second per player.
        """

        return frozenset(int(actor_id) for actor_id in self.actor_ids)

    def runtime_subset(
        self,
        actor_ids: set[int] | frozenset[int],
        *,
        positions: dict[int, tuple[float, float, float]] | None = None,
    ) -> "MonsterSpawnGroup | None":
        """Return the currently living subset with authoritative transforms.

        A vicinity actor can be discarded and requested again without
        recreating the map-world monster. Reusing the original full group
        resurrects killed actors and places surviving actors at stale spawn
        transforms, which leaves the client's replacement actors detached
        from the authoritative combat state.
        """

        selected = tuple(
            actor_id
            for actor_id in self.actor_ids
            if int(actor_id) in actor_ids
        )
        if not selected:
            return None
        selected_set = set(selected)
        commands = []
        for command in self.spawn_batch.commands:
            if isinstance(command, NewMonsterServerCommand):
                actor_id = int(command.value.actor_id)
                if actor_id not in selected_set:
                    continue
                monster = command.value
                if positions is not None and actor_id in positions:
                    monster = replace(
                        monster,
                        position=tuple(
                            float(value)
                            for value in positions[actor_id]
                        ),
                    )
                commands.append(NewMonsterServerCommand(monster))
            elif (
                isinstance(command, StatusEffectServerCommand)
                and int(command.actor_id) in selected_set
            ):
                commands.append(command)
        return replace(
            self,
            actor_ids=selected,
            vicinity=ActorsEnterVicinityBatch(
                ActorsEnterVicinity(selected),
                actor_id=self.vicinity.actor_id,
                commands=self.vicinity.commands,
            ),
            spawn_batch=MonsterSpawnBatch(tuple(commands)),
        )

    def player_is_near(
        self,
        position: tuple[float, float, float],
    ) -> bool:
        return math.hypot(
            position[0] - self.trigger_center[0],
            position[2] - self.trigger_center[2],
        ) <= self.trigger_radius

    def every_spawn_is_near(
        self,
        position: tuple[float, float, float],
        maximum_distance: float,
    ) -> bool:
        positions = tuple(
            command.value.position
            for command in self.spawn_batch.commands
            if isinstance(command, NewMonsterServerCommand)
        )
        return bool(positions) and all(
            math.hypot(
                position[0] - spawn[0],
                position[2] - spawn[2],
            )
            <= float(maximum_distance)
            for spawn in positions
        )

    def runtime_actor_ids(
        self,
        actor_ids: set[int] | frozenset[int],
    ) -> tuple[int, ...]:
        """Return the members :meth:`runtime_subset` would keep."""

        return tuple(
            actor_id
            for actor_id in self.actor_ids
            if int(actor_id) in actor_ids
        )

    def runtime_spawns_are_near(
        self,
        position: tuple[float, float, float],
        maximum_distance: float,
        *,
        selected: set[int] | frozenset[int],
        positions: dict[int, tuple[float, float, float]] | None = None,
    ) -> bool:
        """``every_spawn_is_near`` for a subset, without building the subset.

        Rebuilding a group copies every spawn command, so answering this from
        the original group keeps a rejected group off the allocator entirely.
        """

        maximum_distance = float(maximum_distance)
        found = False
        for command in self.spawn_batch.commands:
            if not isinstance(command, NewMonsterServerCommand):
                continue
            actor_id = int(command.value.actor_id)
            if actor_id not in selected:
                continue
            spawn = (
                positions[actor_id]
                if positions is not None and actor_id in positions
                else command.value.position
            )
            if (
                math.hypot(
                    position[0] - spawn[0],
                    position[2] - spawn[2],
                )
                > maximum_distance
            ):
                return False
            found = True
        return found


@dataclass(frozen=True)
class LocalMonsterEncounter:
    map_id: str
    static_database_path: Path
    actor_ids: tuple[int, ...]
    trigger_center: tuple[float, float, float]
    trigger_radius: float
    navigation: PathEngineNavigationMesh
    terrain: MapCollisionTerrain | None
    coordinate_frame: MapCoordinateFrame
    attack: MonsterAttackDefinition
    run_speed: float
    aggro_range: float
    attack_interval: float
    capsule_radius: float
    vicinity: ActorsEnterVicinityBatch
    spawn_batch: MonsterSpawnBatch
    runtime_definitions: tuple[MonsterRuntimeDefinition, ...] = ()
    spawn_groups: tuple[MonsterSpawnGroup, ...] = ()
    fixed_monster_count: int = 0
    spawn_area_monster_count: int = 0
    spawn_area_target_count: int = 0
    protected_waypoints: tuple[
        tuple[tuple[float, float, float], float], ...
    ] = ()
    # Density volumes materialized even though their EventSet gate was still
    # closed. A later event start must not populate them a second time.
    prematerialized_spawn_area_rows: frozenset[int] = frozenset()
    # The map's difficulty, which picks the skill variants monsters use.
    scaling_difficulty: int = 0

    def player_is_near(self, position: tuple[float, float, float]) -> bool:
        position = self.coordinate_frame.wire_to_world(position)
        return any(
            group.player_is_near(position)
            for group in self.groups
        )

    @property
    def groups(self) -> tuple[MonsterSpawnGroup, ...]:
        if self.spawn_groups:
            return self.spawn_groups
        return (
            MonsterSpawnGroup(
                self.actor_ids,
                self.trigger_center,
                self.trigger_radius,
                self.vicinity,
                self.spawn_batch,
            ),
        )

    def nearby_groups(
        self,
        wire_position: tuple[float, float, float],
        announced_actor_ids: set[int],
        *,
        maximum_actor_distance: float | None = None,
        eligible_actor_ids: set[int] | frozenset[int] | None = None,
        positions: dict[int, tuple[float, float, float]] | None = None,
    ) -> tuple[MonsterSpawnGroup, ...]:
        world_position = self.coordinate_frame.wire_to_world(wire_position)
        result = []
        for original in self.groups:
            # Answer every rejection from the original group.  Building the
            # living subset first copied all hundred groups on every single
            # MoveCommand - roughly nine hundred dataclass replacements per
            # packet - only to discard the ones the player is nowhere near.
            #
            # The trigger-radius test is one hypot and rejects nearly every
            # group, so it runs before any set is built.  The predicates are
            # independent, so the order does not change which groups survive.
            if not original.player_is_near(world_position):
                continue
            if eligible_actor_ids is None:
                selected = set(original.actor_id_set)
            else:
                selected = original.actor_id_set & eligible_actor_ids
                if not selected:
                    continue
            if selected.issubset(announced_actor_ids):
                continue
            if maximum_actor_distance is not None:
                near = (
                    original.runtime_spawns_are_near(
                        world_position,
                        maximum_actor_distance,
                        selected=selected,
                        positions=positions,
                    )
                    if eligible_actor_ids is not None
                    else original.every_spawn_is_near(
                        world_position, maximum_actor_distance
                    )
                )
                if not near:
                    continue
            group = (
                original.runtime_subset(
                    eligible_actor_ids,
                    positions=positions,
                )
                if eligible_actor_ids is not None
                else original
            )
            if group is None:
                continue
            result.append(group)
        return tuple(result)

    def group_for_actor(self, actor_id: int) -> MonsterSpawnGroup | None:
        return next(
            (
                group for group in self.groups
                if int(actor_id) in group.actor_ids
            ),
            None,
        )

    def runtime_for_actor(self, actor_id: int) -> MonsterRuntimeDefinition:
        for definition in self.runtime_definitions:
            if definition.actor_id == int(actor_id):
                return definition
        return MonsterRuntimeDefinition(
            int(actor_id),
            self.attack,
            self.run_speed,
            self.aggro_range,
            self.attack_interval,
            self.capsule_radius,
            max(self.aggro_range, self.aggro_range * 1.5),
            max(35.0, self.aggro_range * 2.0),
            (self.attack,),
        )


def _open_read_only(path: Path) -> sqlite3.Connection:
    if not resident_content.is_file(path):
        raise FileNotFoundError(path)
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


# ``_Template_Monster.PvPMinionType`` of the Assault-the-Fortress structures
# and their summons (defense towers, the stage targets, rammer and bomb
# spawners, rammers, bombs). Every one of them authors ``Resistances='0'``,
# the skill-less stage targets and ``*_invincible`` stand-ins have no
# Skills, and the spawners' only skill is a summon -- each of which the
# ordinary loaders reject. The exceptions below are scoped to this exact
# type, so no other monster's loading changes.
PVP_PROGRESSION_MINION_TYPE = "MAIN_PVP_Progression"
_PVP_PROGRESSION_RESISTANCES = (
    "Fire:0;Ice:0;Lightning:0;Poison:0;DarkMagic:0"
)


@lru_cache(maxsize=512)
def is_pvp_progression_template(
    static_database_path: Path, template_id: str,
) -> bool:
    """Whether ``template_id`` is an Assault-the-Fortress structure/summon."""

    try:
        with _open_read_only(Path(static_database_path)) as database:
            row = database.execute(
                "SELECT COALESCE(PvPMinionType, '') FROM _Template_Monster "
                "WHERE Id=? LIMIT 1",
                (str(template_id),),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return False
    return row is not None and str(row[0]) == PVP_PROGRESSION_MINION_TYPE


def _parse_resistances(value: str) -> dict[str, float]:
    result = {}
    for entry in str(value or "").split(";"):
        if not entry:
            continue
        name, separator, number = entry.partition(":")
        if not separator:
            raise ValueError(f"invalid monster resistance entry {entry!r}")
        result[name] = float(number)
    required = ("Fire", "Ice", "Lightning", "Poison", "DarkMagic")
    missing = [name for name in required if name not in result]
    if missing:
        raise ValueError("monster is missing resistances: " + ", ".join(missing))
    return result


def _permanent_effect_specs(value: str) -> tuple[tuple[str, tuple[float, ...]], ...]:
    """Parse ``_Template_Monster.PermanentStatusEffect`` entries.

    The column uses semicolons between effects.  Optional comma-separated
    metadata includes ``$N:value`` parameters consumed by status modifiers;
    the other keys (for example ``C``, ``D`` and ``DP``) control server-side
    application rules and are not members of the modifier-value array.
    """

    result = []
    for raw_entry in str(value or "").split(";"):
        parts = [part.strip() for part in raw_entry.split(",") if part.strip()]
        if not parts:
            continue
        parameters: dict[int, float] = {}
        for token in parts[1:]:
            name, separator, raw_number = token.partition(":")
            if not separator or not name.startswith("$"):
                continue
            try:
                index = int(name[1:])
                number = float(raw_number)
            except ValueError as exc:
                raise ValueError(
                    f"invalid permanent status parameter {token!r}"
                ) from exc
            if index < 0:
                raise ValueError(f"negative permanent status parameter {token!r}")
            parameters[index] = number
        value_count = max(5, max(parameters, default=-1) + 1)
        values = tuple(parameters.get(index, 0.0) for index in range(value_count))
        result.append((parts[0], values))
    return tuple(result)


@lru_cache(maxsize=256)
def _load_permanent_effect_definitions(
    static_database_path: Path,
    template_id: str,
    strict: bool,
) -> tuple[tuple[int, tuple[float, ...], int], ...]:
    with _open_read_only(static_database_path) as database:
        row = database.execute(
            "SELECT PermanentStatusEffect FROM _Template_Monster "
            "WHERE Id=? LIMIT 1",
            (str(template_id),),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown monster template {template_id!r}")
        resolved = []
        for resource_id, values in _permanent_effect_specs(row[0]):
            effect_row = database.execute(
                "SELECT rowid - 1, COALESCE(StatusEffectDuration, 0) "
                "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
                (resource_id,),
            ).fetchone()
            if effect_row is None:
                if not strict:
                    continue
                raise ValueError(
                    f"monster {template_id!r} references unknown permanent "
                    f"status effect {resource_id!r}"
                )
            resolved.append(
                (
                    int(effect_row[0]),
                    values,
                    max(0, round(float(effect_row[1]) * 25.0)),
                )
            )
    return tuple(resolved)


def load_monster_permanent_status_effects(
    static_database_path: str | Path,
    template_id: str,
    actor_id: int,
    *,
    strict: bool = True,
    server_tick: int = 0,
) -> tuple[StatusEffectEntry, ...]:
    """Resolve a monster template's permanent effects into wire entries."""

    definitions = _load_permanent_effect_definitions(
        Path(static_database_path), str(template_id), bool(strict)
    )
    resolved = []
    start_tick = int(server_tick) & 0xFFFFFFFF
    for ordinal, (
        effect_id,
        values,
        duration_ticks,
    ) in enumerate(definitions, start=1):
        # Status effects use a separate instance-id namespace.  Deriving it
        # from actor/ordinal makes the generated spawn deterministic and
        # unique for every effect in the encounter.
        instance_id = ((int(actor_id) & 0xFFFFFF) << 8) | ordinal
        resolved.append(
            StatusEffectEntry(
                effect_id=effect_id,
                field_04=instance_id,
                field_0c=0,
                field_14=25,
                field_08=start_tick,
                field_18=0,
                field_10=duration_ticks,
                field_1c=100,
                field_20=int(actor_id),
                field_25=False,
                field_26=False,
                field_39=True,
                has_details=True,
                values=values,
            )
        )
    return tuple(resolved)


@lru_cache(maxsize=256)
def _load_monster_template_values(
    static_database_path: Path,
    template_id: str,
) -> tuple[
    float,
    float,
    float,
    float,
    float,
    str,
    int,
    str,
    float,
    float,
    float,
    float,
]:
    with _open_read_only(static_database_path) as database:
        row = database.execute(
            """
            SELECT HitPoints, Shield, Armor, MinDamage, MaxDamage,
                   Resistances, CharLevel, Rank
            FROM _Template_Monster WHERE Id=? LIMIT 1
            """,
            (template_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown monster template {template_id!r}")
        rank_row = database.execute(
            """
            SELECT HealthPointsFactor, MinDamageFactor, MaxDamageFactor,
                   ResistanceFactor
            FROM _Template_MonsterRanks WHERE Id=? LIMIT 1
            """,
            (row[7],),
        ).fetchone()
        if rank_row is None:
            raise ValueError(f"unknown monster rank {row[7]!r}")
    return (
        float(row[0]),
        float(row[1]),
        float(row[2]),
        float(row[3]),
        float(row[4]),
        str(row[5]),
        int(row[6]),
        str(row[7]),
        *(float(value) for value in rank_row),
    )


@lru_cache(maxsize=64)
def _load_monster_scaling_factors(
    static_database_path: Path,
    monster_level: int,
    scaling_difficulty: int,
) -> tuple[float, float, float, float, float, float, float, float]:
    """Return the level curve and difficulty factors shared by one encounter.

    Both rows depend only on the encounter's level and difficulty, so every
    monster in a map resolves the same pair.  Reading them per monster opened
    768 connections and ran 1,536 statements for one dense map.
    """

    with _open_read_only(static_database_path) as database:
        curve = database.execute(
            """
            SELECT ScalingMonsterHP, ScalingMonsterShield,
                   ScalingMonsterDamage, ResistancesProgression
            FROM _Template_MonsterLvlCurves
            WHERE Level=? LIMIT 1
            """,
            (int(monster_level),),
        ).fetchone()
        if curve is None:
            raise ValueError(
                f"monster level {monster_level} has no scaling curve"
            )
        factors = database.execute(
            """
            SELECT MonsterHP, MonsterShield, MonsterDMG,
                   DefenseMultiplier
            FROM _Template_DifficultyFactors
            WHERE CAST(Id AS INTEGER)=? LIMIT 1
            """,
            (int(scaling_difficulty),),
        ).fetchone()
        if factors is None:
            raise ValueError(
                f"unknown scaling difficulty {scaling_difficulty}"
            )
    return (
        *(float(value) for value in curve),
        *(float(value) for value in factors),
    )


def _load_monster(
    static_database_path: Path,
    template_id: str,
    actor_id: int,
    position: tuple[float, float, float],
    *,
    monster_level: int | None = None,
    scaling_difficulty: int = 0,
    coefficient_stats: bool = False,
    non_targetable: bool = False,
) -> NewMonster:
    (
        hit_points,
        shield,
        armor,
        min_damage,
        max_damage,
        resistance_text,
        level,
        rank,
        health_factor,
        min_factor,
        max_factor,
        resistance_factor,
    ) = _load_monster_template_values(
        static_database_path, str(template_id)
    )
    health_curve = 1.0
    shield_curve = 1.0
    damage_curve = 1.0
    resistance_curve = 1.0
    difficulty_health = 1.0
    difficulty_shield = 1.0
    difficulty_damage = 1.0
    difficulty_defense = 1.0
    if monster_level is not None:
        (
            health_curve,
            shield_curve,
            damage_curve,
            resistance_curve,
            difficulty_health,
            difficulty_shield,
            difficulty_damage,
            difficulty_defense,
        ) = _load_monster_scaling_factors(
            static_database_path,
            int(monster_level),
            int(scaling_difficulty),
        )
        # Legacy templates contain stats already evaluated at CharLevel.
        # Scaling templates use level-one coefficients. Applying the full
        # target curve to a levelled template multiplies its growth twice.
        #
        # Fixed tower hardware is the exception.  ``h0009_endlesstower_01_
        # room_fire_shooter`` carries CharLevel 100 with HitPoints 2 and
        # MinDamage 15, which cannot be "already evaluated at 100" - a level
        # 100 player has 450,000 base health.  Every other shooter in the same
        # instance (``e_dra_fortress_normal_powernode``, ``i0011_scaling_
        # ancestral_guard_poison_*``) is authored as a level-one coefficient
        # with CharLevel 1, so the fire shooter was the only tower whose
        # damage collapsed to 14-23 instead of 79,509-132,516.  Tower hardware
        # is invulnerable and stationary, so its health pool is cosmetic; the
        # damage figure is what the floor is tuned around.
        if level > 1 and not coefficient_stats:
            base = _load_monster_scaling_factors(static_database_path, level, 0)
            health_curve /= max(base[0], 1e-9)
            shield_curve /= max(base[1], 1e-9)
            damage_curve /= max(base[2], 1e-9)
            resistance_curve /= max(base[3], 1e-9)
        level = int(monster_level)

    health = (
        float(hit_points)
        * health_curve
        * health_factor
        * difficulty_health
    )
    if (str(resistance_text).strip() in {"", "0"}
            and is_pvp_progression_template(
                Path(static_database_path), str(template_id))):
        # The fortress structures author a bare '0': no elemental resistance.
        resistance_text = _PVP_PROGRESSION_RESISTANCES
    resistances = _parse_resistances(resistance_text)
    # ScaleMonsterForRank() unconditionally looks up these attributes. Retail
    # omits shield (8) and physical armor (20) from this particular snapshot
    # because its normal creation path pre-populates them from the template.
    # The reconstructed path must transmit them explicitly or the client's
    # Attribute dictionary asserts before the monster can finish spawning.
    stats = (
        MonsterStat(2, health),
        MonsterStat(
            8,
            float(shield)
            * shield_curve
            * health_factor
            * difficulty_shield,
        ),
        MonsterStat(9, 0.0),
        MonsterStat(10, 0.0),
        MonsterStat(
            11,
            float(min_damage)
            * damage_curve
            * min_factor
            * difficulty_damage,
        ),
        MonsterStat(
            12,
            float(max_damage)
            * damage_curve
            * max_factor
            * difficulty_damage,
        ),
        MonsterStat(
            20,
            float(armor)
            * resistance_curve
            * resistance_factor
            * difficulty_defense,
        ),
        MonsterStat(
            21,
            resistances["Fire"]
            * resistance_curve
            * resistance_factor
            * difficulty_defense,
        ),
        MonsterStat(
            22,
            resistances["Ice"]
            * resistance_curve
            * resistance_factor
            * difficulty_defense,
        ),
        MonsterStat(
            23,
            resistances["Lightning"]
            * resistance_curve
            * resistance_factor
            * difficulty_defense,
        ),
        MonsterStat(
            24,
            resistances["Poison"]
            * resistance_curve
            * resistance_factor
            * difficulty_defense,
        ),
        MonsterStat(
            25,
            resistances["DarkMagic"]
            * resistance_curve
            * resistance_factor
            * difficulty_defense,
        ),
    )
    integer_health = round(health)
    return NewMonster(
        template_id=template_id,
        stats=stats,
        field_88=integer_health,
        field_90=integer_health,
        field_98=0,
        field_9c=int(level),
        field_a0=0.0,
        position=tuple(float(component) for component in position),
        category=0,
        field_c4=0,
        placement=ActorPlacement(
            # ``0x00010004`` is the neutral/player-owned faction the client
            # refuses to target. Fixed tower hardware uses it so a laser or a
            # trap shooter stays a visible hazard the player can only avoid -
            # never a mob they can click and swing at.
            0x00010004 if non_targetable else 0x00010001,
            actor_id,
            2,
            False,
            False,
        ),
        field_d8=0,
        field_dc=True,
        field_e0=0xFFFFFFFF,
        field_e4=False,
        rank=str(rank),
        field_f0="",
        actor_id=actor_id,
    )


def load_player_summon_spawn(
    static_database_path: str | Path,
    template_id: str,
    actor_id: int,
    owner_actor_id: int,
    position: tuple[float, float, float],
    *,
    level: int,
    server_tick: int,
    yaw: float = 0.0,
    scaling_difficulty: int = 0,
    map_companion: bool = False,
    map_level_scaling: bool = False,
) -> MonsterSpawnGroup:
    """Build the friendly ``NewMonster`` lifecycle used by player summons.

    Retail represents dwarf turrets and similar constructs as category-one
    monster actors. Their ``0x00010004`` placement faction makes them
    player-owned, non-targetable actors; ``field_d8`` identifies the
    summoning player. ``field_e0`` is the PvP team id, not an interaction
    mask, and remains ``InvalidTeamId`` outside an explicit battleground.
    The ordinary monster template still supplies scaling stats and effects.
    """

    database_path = Path(static_database_path)
    monster = _load_monster(
        database_path,
        str(template_id),
        int(actor_id),
        tuple(float(value) for value in position),
        monster_level=(None if map_companion and not map_level_scaling
                       else max(1, int(level))),
        scaling_difficulty=max(0, int(scaling_difficulty)),
    )
    monster = replace(
        monster,
        category=2 if map_companion else 1,
        field_a0=float(yaw),
        field_c4=int(server_tick) & 0xFFFFFFFF,
        placement=ActorPlacement(
            0x00010001 if map_companion else 0x00010004,
            int(actor_id),
            0,
            False,
            False,
        ),
        field_d8=int(owner_actor_id),
        field_dc=True,
        field_e0=0xFFFFFFFF,
        field_e4=False,
    )
    return _spawn_group(
        database_path,
        (monster,),
        use_monster_spawn_tick=True,
    )


class _DirectNavigationMesh:
    """Fallback for maps whose optional PathEngine export is unavailable."""

    @staticmethod
    def is_reachable(
        start: tuple[float, float, float],
        goal: tuple[float, float, float],
        *,
        max_horizontal_snap: float | None = None,
    ) -> bool:
        """Accept finite direct positions when no component graph is present.

        Concrete collision terrain remains responsible for rejecting an
        out-of-bounds ground projection.  The fallback cannot compare
        PathEngine components, but it must implement the same navigation
        contract so ancillary loot placement cannot abort a resolved hit.
        """

        if (
            max_horizontal_snap is not None
            and float(max_horizontal_snap) < 0.0
        ):
            raise ValueError("max_horizontal_snap cannot be negative")
        return all(
            math.isfinite(float(component))
            for point in (start, goal)
            for component in point
        )

    @staticmethod
    def project_position(
        point: tuple[float, float, float],
        *,
        max_horizontal_distance: float | None = None,
    ) -> tuple[float, float, float]:
        if (
            max_horizontal_distance is not None
            and float(max_horizontal_distance) < 0.0
        ):
            raise ValueError("max_horizontal_distance cannot be negative")
        return tuple(float(value) for value in point)

    @staticmethod
    def project_position_relative(
        point: tuple[float, float, float],
        reference: tuple[float, float, float],
        *,
        max_horizontal_distance: float | None = None,
    ) -> tuple[float, float, float]:
        del reference
        return _DirectNavigationMesh.project_position(
            point,
            max_horizontal_distance=max_horizontal_distance,
        )

    @staticmethod
    def find_route(
        start: tuple[float, float, float],
        target: tuple[float, float, float],
        *,
        search_center: tuple[float, float, float] | None = None,
        max_search_distance: float | None = None,
    ) -> NavigationRoute:
        if (search_center is None) != (max_search_distance is None):
            raise ValueError(
                "search_center and max_search_distance must be supplied together"
            )
        if (
            search_center is not None
            and math.hypot(
                target[0] - search_center[0],
                target[2] - search_center[2],
            )
            > float(max_search_distance) + 1e-6
        ):
            raise NavigationError(
                "navigation goal lies outside the bounded search territory"
            )
        return NavigationRoute((start, target), (0,))

    @staticmethod
    def advance_toward(
        start: tuple[float, float, float],
        target: tuple[float, float, float],
        distance: float,
        *,
        search_center: tuple[float, float, float] | None = None,
        max_search_distance: float | None = None,
    ) -> tuple[float, float, float]:
        _DirectNavigationMesh.find_route(
            start,
            target,
            search_center=search_center,
            max_search_distance=max_search_distance,
        )
        dx = target[0] - start[0]
        dz = target[2] - start[2]
        length = math.hypot(dx, dz)
        if length <= 1e-6:
            return start
        amount = min(max(0.0, float(distance)), length)
        return (
            start[0] + dx / length * amount,
            start[1],
            start[2] + dz / length * amount,
        )


def _spawn_position(transform: bytes) -> tuple[float, float, float]:
    if len(transform) != 64:
        raise ValueError(
            f"monster spawn transform has {len(transform)} bytes, expected 64"
        )
    matrix = struct.unpack("<16f", transform)
    return (float(matrix[12]), float(matrix[13]), float(matrix[14]))


def _parse_damage_types(value: object) -> tuple[tuple[str, float], ...]:
    """Parse Skill.DamageType into normalized weighted damage channels."""

    parsed = []
    for encoded in str(value or "Physical").replace(",", ";").split(";"):
        encoded = encoded.strip()
        if not encoded:
            continue
        name, separator, raw_weight = encoded.partition(":")
        name = name.strip() or "Physical"
        try:
            weight = float(raw_weight) if separator else 1.0
        except ValueError:
            weight = 1.0
        if weight > 0.0:
            parsed.append((name, weight))
    return tuple(parsed) or (("Physical", 1.0),)


def _parse_cooldown_range(value: object) -> tuple[float, float]:
    """Parse InitialCoolDown values such as ``4`` or ``3|5`` seconds."""

    parts = [part.strip() for part in str(value or "").split("|")]
    try:
        values = [max(0.0, float(part)) for part in parts if part]
    except (TypeError, ValueError):
        return (0.0, 0.0)
    if not values or not all(math.isfinite(part) for part in values):
        return (0.0, 0.0)
    return min(values), max(values)


@lru_cache(maxsize=256)
def _load_runtime_template(
    static_database_path: Path,
    template_id: str,
) -> tuple[
    tuple[MonsterAttackDefinition, ...],
    float,
    float,
    float,
    float,
    float,
    float,
    float,
] | None:
    """Resolve reusable movement and autonomous attack template data."""

    with _open_read_only(static_database_path) as database:
        movement_row = database.execute(
            "SELECT RunSpeed, AggroRange, AttackInterval, Skills, "
            "CapsuleRadius, EscapeRange, TravelDistance, "
            "COALESCE(SummonMonsterId, ''), "
            "COALESCE(SummonMonsterAmount, 0), "
            "COALESCE(SummonMonsterLimit, 0), "
            "COALESCE(BehaviorType, ''), "
            "COALESCE(StartupDuration, 0), "
            "COALESCE(PvPMinionType, '') "
            "FROM _Template_Monster WHERE Id=? LIMIT 1",
            (str(template_id),),
        ).fetchone()
        if movement_row is None:
            return None
        skill_specs = tuple(
            entry.strip()
            for entry in str(movement_row[3] or "").replace(
                ";", ","
            ).split(",")
            if entry.strip()
        )
        candidates = []
        for ordinal, skill_spec in enumerate(skill_specs):
            skill_resource_id, separator, raw_skill_level = (
                skill_spec.partition(":")
            )
            skill_resource_id = skill_resource_id.strip()
            if not skill_resource_id:
                continue
            skill_row = database.execute(
                """
                SELECT s.rowid - 1, HitFrame, MotionUnblockFrame,
                       SkillUnblockFrame, AttackRange, HitRange,
                       SkillType, TargetingType,
                       COALESCE(s.BulletEmitDelay, 0),
                       COALESCE(b.Velocity, 0),
                       COALESCE(b.BulletRadius, 0),
                       COALESCE(s.UserStatusEffects, ''),
                       COALESCE(s.VictimStatusEffects, ''),
                       COALESCE(b.LifeTime, 0),
                       COALESCE(b.MotionType, 'Straight'),
                       b.StartOffset,
                       COALESCE(s.DamageModifier, 0),
                       COALESCE(s.HitStances, ''),
                       COALESCE(s.LoopStartFrame, 0),
                       COALESCE(s.ExecuteSequenceMapping, ''),
                       COALESCE(s.DamageType, 'Physical'),
                       COALESCE(s.CoolDown, 0),
                       COALESCE(s.InitialCoolDown, ''),
                       COALESCE(s.CoolDownCategory, ''),
                       COALESCE(s.MinDifficulty, 0),
                       COALESCE(s.MaxDifficulty, 99),
                       COALESCE(s.Summons, 0),
                       COALESCE(s.SummonMonsterId, ''),
                       COALESCE(s.SummonMonsterAmount, 0),
                       COALESCE(s.SummonMonsterLimit, 0),
                       COALESCE(s.BulletCount, 1),
                       COALESCE(s.TargetingType, 'Actor'),
                       COALESCE(s.PlayerWidth, 0),
                       COALESCE(s.EndWidth, 0),
                       COALESCE(s.Angle, 0),
                       COALESCE(s.SkillBulletId, ''),
                       COALESCE(b.StaticCollision, 0)
                FROM _Template_Skill AS s
                LEFT JOIN _Template_SkillBullet AS b
                  ON b.Id=s.SkillBulletId
                WHERE s.Id=? LIMIT 1
                """,
                (skill_resource_id,),
            ).fetchone()
            if skill_row is None:
                continue
            skill_type = str(skill_row[6])
            targeting_type = str(skill_row[7])
            skill_bullet_id = str(skill_row[35] or "").strip()
            has_bullet = bool(skill_bullet_id)
            mortis_charge = skill_resource_id.casefold() == "bossmortisunbound_charge"
            self_buff = False
            summon = (
                int(skill_row[26] or 0) != 0
                and float(skill_row[16]) <= 0.0
            )
            summon_template = summon_amount = summon_limit = None
            if summon:
                summon_template = str(skill_row[27] or "").strip() or str(movement_row[7] or "").strip()
                summon_amount = int(skill_row[28] or 0) or int(movement_row[8] or 0)
                summon_limit = int(skill_row[29] or 0) or int(movement_row[9] or 0)
                summon = bool(summon_template) and summon_amount > 0 and skill_type in ("Shifted", "Melee")
            # Charge and Jump use movement-specific command payloads we do not
            # model. Shifted ground-target skills are supported below, but
            # only for radius/point targeting (and RandomPosition: points the
            # server scatters around the caster), whose native command receives
            # a target point for every authored bullet. This avoids routing
            # movement skills through BulletSkillCommand with an empty point
            # array (the source of the client Math::point assertion). A summon
            # is cast on the caster's own place, whatever its targeting.
            if (not mortis_charge and skill_type.casefold() in {
                "charge", "chargethrough", "jump",
            }) or (
                skill_type.casefold() == "shifted"
                and has_bullet
                and not summon
                and targeting_type.casefold() not in {
                    "radius", "point", "location", "randomposition",
                }
            ):
                continue
            if mortis_charge:
                # ChargeThrough consumes a destination-point array. Its body
                # projectile must never use the point-free BulletSkill payload.
                command_id = TARGET_POINT_BULLET_SKILL_COMMAND
            elif skill_type == "Shifted" and (
                has_bullet or skill_resource_id.casefold() == "bossmortisunbound_gravedigger"
            ) and targeting_type.casefold() in {
                "radius", "point", "location", "randomposition",
            }:
                command_id = SHIFTED_SKILL_COMMAND
            elif summon and skill_type == "Shifted":
                command_id = SHIFTED_SKILL_COMMAND
            elif summon and skill_type == "Melee":
                command_id = SKILL_COMMAND
            elif (
                has_bullet
                and skill_type.casefold() == "rangedtargetpoint"
            ) or (
                has_bullet
                and targeting_type.casefold() in {"point", "location"}
                and skill_type.casefold() != "shifted"
            ):
                # These projectiles carry an explicit world point list in
                # TargetPointBulletSkillCommand. Sending them as BulletSkill
                # leaves that list uninitialized in the client; impact then
                # indexes an empty Array<Math::point> and asserts.
                command_id = TARGET_POINT_BULLET_SKILL_COMMAND
            elif has_bullet:
                # Ranged skills with a bullet require BulletSkillCommand on
                # the client. Radius-targeted Shifted skills instead carry
                # their ground-point array in ShiftedSkillCommand.
                if skill_type.casefold() == "shifted":
                    command_id = SHIFTED_SKILL_COMMAND
                elif (
                    targeting_type.casefold() in {"actor", "target"}
                    or skill_type == "RangedTarget"
                ):
                    command_id = TARGET_BULLET_SKILL_COMMAND
                else:
                    command_id = BULLET_SKILL_COMMAND
            elif skill_type == "Melee" and float(skill_row[16]) > 0.0:
                command_id = SKILL_COMMAND
            elif (
                skill_type == "Melee"
                and float(skill_row[16]) <= 0.0
                and str(skill_row[11] or "").strip()
            ):
                command_id = SKILL_COMMAND
                self_buff = True
            elif skill_type == "Ranged":
                command_id = BULLET_SKILL_COMMAND
            elif skill_type == "RangedTarget":
                command_id = TARGET_BULLET_SKILL_COMMAND
            elif skill_type == "MeleeTarget":
                command_id = TARGET_SKILL_COMMAND
            else:
                # Charge/ChargeThrough and remaining movement/scene skill
                # types use distinct client command payloads. Do not guess
                # ShiftedSkill here: malformed destinations can crash the
                # client while it builds its point array.
                continue
            skill_level = (
                max(0, int(raw_skill_level) - 1)
                if separator and raw_skill_level
                else 0
            )
            start_offset = (0.0, 0.0, 0.0, 0.0)
            if isinstance(skill_row[15], bytes) and len(skill_row[15]) == 16:
                start_offset = tuple(struct.unpack("<4f", skill_row[15]))
            candidates.append(
                (
                    (
                        0
                        if (
                            str(skill_row[19]).strip()
                            and str(skill_row[19]).strip().casefold()
                            != "empty_sequence"
                        )
                        else 1
                    ),
                    0 if float(skill_row[21] or 0.0) <= 0.0 else 1,
                    ordinal,
                    MonsterAttackDefinition(
                        resource_id=skill_resource_id,
                        command_id=command_id,
                        skill_level=skill_level,
                        skill_manager_index=int(skill_row[0]),
                        hit_frame=max(0, int(skill_row[1])),
                        motion_unblock_frame=max(0, int(skill_row[2])),
                        skill_unblock_frame=max(0, int(skill_row[3])),
                        attack_range=max(0.1, float(skill_row[4])),
                        hit_range=max(0.1, float(skill_row[5])),
                        bullet_emit_delay=max(0.0, float(skill_row[8])),
                        projectile_velocity=max(0.0, float(skill_row[9])),
                        projectile_radius=max(0.0, float(skill_row[10])),
                        user_status_effects=str(skill_row[11] or ""),
                        victim_status_effects=str(skill_row[12] or ""),
                        projectile_lifetime=max(
                            0.0, float(skill_row[13])
                        ),
                        motion_type=str(skill_row[14] or "Straight"),
                        start_offset=start_offset,
                        static_collision=bool(skill_row[36] or 0),
                        loop_start_frame=max(0, int(skill_row[18])),
                        damage_types=_parse_damage_types(skill_row[20]),
                        cooldown=max(0.0, float(skill_row[21] or 0.0)),
                        initial_cooldown=_parse_cooldown_range(skill_row[22]),
                        cooldown_category=str(skill_row[23] or "").strip(),
                        min_difficulty=int(skill_row[24] or 0),
                        max_difficulty=(
                            int(skill_row[25])
                            if skill_row[25] is not None else 99
                        ),
                        damage_modifier=max(0.0, float(skill_row[16] or 0.0)),
                        self_buff=self_buff,
                        summon_template=summon_template if summon else "",
                        summon_amount=max(0, int(summon_amount or 0)) if summon else 0,
                        summon_limit=max(0, int(summon_limit or 0)) if summon else 0,
                        bullet_count=max(1, int(skill_row[30] or 1)),
                        targeting_type=str(skill_row[31] or "Actor"),
                        player_width=max(0.0, float(skill_row[32] or 0.0)),
                        end_width=max(0.0, float(skill_row[33] or 0.0)),
                        angle_degrees=max(0.0, float(skill_row[34] or 0.0)),
                        movement_skill=mortis_charge,
                    ),
                )
            )
    if not candidates:
        return None
    # Prefer a command with a real execution sequence. Several monster rows
    # list an invisible helper skill before their authored ranged attack;
    # selecting the helper makes projectiles appear before any cast motion.
    attacks = tuple(
        entry[3]
        for entry in sorted(
            candidates, key=lambda entry: (entry[0], entry[1], entry[2])
        )
    )
    # Resource ids are stable while a few generated rows repeat the same skill
    # in comma/semicolon forms.  Rotate authored skills, not duplicate tokens.
    attacks = tuple({value.resource_id: value for value in attacks}.values())
    # A monster's own buffs ride with its attacks: the primary attack (its
    # approach range and interval) is a blow, and a monster with buffs only
    # fights as before, not at all.
    attacks = (
        tuple(value for value in attacks if not value.support)
        + tuple(value for value in attacks if value.support)
    )
    pvp_progression = str(movement_row[12]) == PVP_PROGRESSION_MINION_TYPE
    if attacks[0].support and not pvp_progression:
        # A fortress rammer/bomb spawner's only skill is its summon; it is
        # still its autonomous action (PvPSpawnerRammerSummon/BombsSummon).
        return None
    attack = attacks[0]
    static_strategies = {
        "staticstrategy", "noturnstaticstrategy",
        "sleeplessstaticstrategy", "boss04strategy",
        "boss03strategy",
    }
    if pvp_progression:
        # The spawners' NoTurnNoTargetStaticStrategy is a fixed structure.
        static_strategies = static_strategies | {"noturnnotargetstaticstrategy"}
    return (
        attacks,
        (
            0.0
            if str(movement_row[10]).casefold() in static_strategies
            else max(0.1, float(movement_row[0]))
        ),
        max(0.0, float(movement_row[1])),
        max(
            0.1,
            float(movement_row[2]),
            attack.motion_unblock_frame / 25.0,
            attack.skill_unblock_frame / 25.0,
        ),
        max(0.05, float(movement_row[4])),
        max(
            float(movement_row[1]),
            float(movement_row[5] or 0.0),
        ),
        max(
            max(float(movement_row[1]), 1.0),
            float(movement_row[6] or 0.0),
        ),
        max(0.0, float(movement_row[11])),
    )


def _load_runtime_definition(
    static_database_path: Path,
    template_id: str,
    actor_id: int,
    *,
    stationary: bool = False,
) -> MonsterRuntimeDefinition | None:
    """Resolve movement and all usable autonomous attacks for a monster."""

    values = _load_runtime_template(static_database_path, str(template_id))
    if values is None:
        # Damage dolls and mounted aura-only devices intentionally have no
        # Skills. Keep their authored actor/permanent effects in the world
        # without inventing movement or an autonomous attack.
        if (
            stationary
            or str(template_id).casefold().startswith("damage_doll")
            or str(template_id).casefold() == "e_dra_fortress_normal_powernode"
            # Fortress stage targets (drawbridge switch, gun controls) and
            # the ``*_invincible`` tower stand-ins author no Skills.
            or is_pvp_progression_template(
                Path(static_database_path), str(template_id))
        ):
            return MonsterRuntimeDefinition(
                actor_id=int(actor_id),
                attack=_PLAYER_ONLY_ATTACK,
                run_speed=0.0,
                aggro_range=0.0,
                attack_interval=0.0,
                capsule_radius=0.3,
                escape_range=0.0,
                travel_distance=0.0,
                attacks=(),
            )
        # This tutorial cultist is a scripted, untargetable story actor. Its
        # static template intentionally has no Skills, so the ordinary runtime
        # loader rejects it even though the map event needs to publish it for
        # the staff-theft sequence. Keep it inert in the combat simulation while
        # still allowing the event spawn to reach the client.
        if str(template_id).casefold() == "a0001_champion_undead_mage_01":
            return MonsterRuntimeDefinition(
                actor_id=int(actor_id),
                attack=_PLAYER_ONLY_ATTACK,
                run_speed=0.0,
                aggro_range=0.0,
                attack_interval=0.0,
                capsule_radius=0.3,
                escape_range=0.0,
                travel_distance=0.0,
                attacks=(),
            )
        return None
    (
        attacks,
        run_speed,
        aggro_range,
        attack_interval,
        capsule_radius,
        escape_range,
        travel_distance,
        startup_duration,
    ) = values
    attack = attacks[0]
    if stationary:
        # Tower hardware is a hazard, not a duelling opponent: shorten its
        # cycle so it pressures the player while they move, and let it acquire
        # a target from further away. ``hit_range`` stays authored on purpose -
        # it is the damage sphere at the impact point, so widening it splashes
        # players the projectile visibly missed.
        attack_interval = max(
            TOWER_MIN_ATTACK_INTERVAL,
            attack_interval / TOWER_ATTACK_RATE_MULTIPLIER,
        )
        attacks = tuple(
            replace(
                value,
                attack_range=value.attack_range * TOWER_ATTACK_RANGE_MULTIPLIER,
                projectile_velocity=(
                    value.projectile_velocity
                    * TOWER_FIRE_PROJECTILE_SPEED_MULTIPLIER
                    if "fire_shooter" in str(template_id).casefold()
                    else value.projectile_velocity
                ),
                motion_unblock_frame=(
                    max(1, round(TOWER_FIRE_BURST_INTERVAL * 25))
                    if "fire_shooter" in str(template_id).casefold()
                    else value.motion_unblock_frame
                ),
                skill_unblock_frame=(
                    max(1, round(TOWER_FIRE_BURST_INTERVAL * 25))
                    if "fire_shooter" in str(template_id).casefold()
                    else value.skill_unblock_frame
                ),
                loop_start_frame=(
                    max(1, round(TOWER_FIRE_BURST_INTERVAL * 25))
                    if "fire_shooter" in str(template_id).casefold()
                    else value.loop_start_frame
                ),
            )
            for value in attacks
        )
        if "fire_shooter" in str(template_id).casefold():
            attack_interval = TOWER_FIRE_BURST_INTERVAL
            aggro_range = max(aggro_range, TOWER_FIRE_ACQUISITION_RANGE)
            travel_distance = max(
                travel_distance, TOWER_FIRE_ACQUISITION_RANGE
            )
        attack = attacks[0]
    return MonsterRuntimeDefinition(
        actor_id=int(actor_id),
        attack=attack,
        run_speed=0.0 if stationary else run_speed,
        aggro_range=aggro_range,
        attack_interval=attack_interval,
        capsule_radius=capsule_radius,
        escape_range=escape_range,
        # Attack acquisition also uses this authored radius. Zero speed (and
        # the stationary movement guard) anchors the mount without disabling
        # its ranged attacks.
        travel_distance=travel_distance,
        attacks=attacks,
        startup_duration=startup_duration,
    )


def _group_dynamic_monsters(
    monsters: tuple[NewMonster, ...],
) -> tuple[tuple[NewMonster, ...], ...]:
    """Create bounded spatial vicinity groups for large map populations."""

    if not monsters:
        return ()
    cell_size = MONSTER_GROUP_RADIUS
    buckets: dict[tuple[int, int], list[NewMonster]] = {}
    order = {monster.actor_id: index for index, monster in enumerate(monsters)}
    for monster in monsters:
        cell = (
            math.floor(monster.position[0] / cell_size),
            math.floor(monster.position[2] / cell_size),
        )
        buckets.setdefault(cell, []).append(monster)
    remaining = {monster.actor_id for monster in monsters}
    groups = []
    for seed in monsters:
        if seed.actor_id not in remaining:
            continue
        remaining.remove(seed.actor_id)
        group = [seed]
        seed_cell = (
            math.floor(seed.position[0] / cell_size),
            math.floor(seed.position[2] / cell_size),
        )
        candidates = []
        for cell_x in range(seed_cell[0] - 1, seed_cell[0] + 2):
            for cell_z in range(seed_cell[1] - 1, seed_cell[1] + 2):
                for monster in buckets.get((cell_x, cell_z), ()):
                    if monster.actor_id not in remaining:
                        continue
                    distance = math.hypot(
                        monster.position[0] - seed.position[0],
                        monster.position[2] - seed.position[2],
                    )
                    if distance <= MONSTER_GROUP_RADIUS:
                        candidates.append(
                            (distance, order[monster.actor_id], monster)
                        )
        for _distance, _order, monster in sorted(candidates):
            distance = math.hypot(
                monster.position[0] - seed.position[0],
                monster.position[2] - seed.position[2],
            )
            group.append(monster)
            remaining.remove(monster.actor_id)
            if len(group) >= MONSTER_GROUP_SIZE:
                break
        groups.append(tuple(group))
    return tuple(groups)


def _spawn_group(
    static_database_path: Path,
    monsters: tuple[NewMonster, ...],
    *,
    use_monster_spawn_tick: bool = False,
) -> MonsterSpawnGroup:
    commands = []
    for monster in monsters:
        commands.append(NewMonsterServerCommand(monster))
        permanent_effects = load_monster_permanent_status_effects(
            static_database_path,
            monster.template_id,
            monster.actor_id,
            strict=False,
            server_tick=(
                int(monster.field_c4) if use_monster_spawn_tick else 0
            ),
        )
        # The retail client warns for an empty StatusEffectCommand. Monsters
        # without a PermanentStatusEffect need no clearing snapshot because
        # their actor id is new and cannot own stale effects.
        if permanent_effects:
            commands.append(
                StatusEffectServerCommand(
                    StatusEffect(True, permanent_effects),
                    monster.actor_id,
                )
            )
    center = (
        sum(monster.position[0] for monster in monsters) / len(monsters),
        sum(monster.position[1] for monster in monsters) / len(monsters),
        sum(monster.position[2] for monster in monsters) / len(monsters),
    )
    radius = max(
        14.0,
        max(
            math.hypot(
                monster.position[0] - center[0],
                monster.position[2] - center[2],
            )
            for monster in monsters
        ) + 12.0,
    )
    actor_ids = tuple(monster.actor_id for monster in monsters)
    return MonsterSpawnGroup(
        actor_ids=actor_ids,
        trigger_center=center,
        trigger_radius=radius,
        vicinity=ActorsEnterVicinityBatch(
            ActorsEnterVicinity(actor_ids), actor_id=0, commands=()
        ),
        spawn_batch=MonsterSpawnBatch(tuple(commands)),
    )


@dataclass(frozen=True)
class _MonsterSpawnArea:
    row_id: int
    parent_node_index: int
    event_set_row: int
    matrix: tuple[float, ...]
    spawn_list_id: str
    density: float
    priority: int
    # Areas built without them (tests, older callers) spawn no champions.
    min_champion_spawn: int = 0
    max_champion_spawn: int = 0

    @property
    def width(self) -> float:
        return math.hypot(self.matrix[0], self.matrix[2])

    @property
    def depth(self) -> float:
        return math.hypot(self.matrix[8], self.matrix[10])

    @property
    def horizontal_area(self) -> float:
        return abs(
            self.matrix[0] * self.matrix[10]
            - self.matrix[2] * self.matrix[8]
        )

    @property
    def center(self) -> tuple[float, float, float]:
        return (self.matrix[12], self.matrix[13], self.matrix[14])

    def contains(self, position: tuple[float, float, float]) -> bool:
        determinant = (
            self.matrix[0] * self.matrix[10]
            - self.matrix[2] * self.matrix[8]
        )
        if abs(determinant) <= 1e-6:
            return False
        dx = position[0] - self.matrix[12]
        dz = position[2] - self.matrix[14]
        local_x = (
            dx * self.matrix[10] - dz * self.matrix[8]
        ) / determinant
        local_z = (
            self.matrix[0] * dz - self.matrix[2] * dx
        ) / determinant
        return abs(local_x) <= 0.50001 and abs(local_z) <= 0.50001

    def candidate_positions(self) -> tuple[tuple[float, float, float], ...]:
        count = max(
            0,
            int(round(
                self.horizontal_area * self.density * SPAWN_AREA_DENSITY_SCALE
            )),
        )
        if SPAWN_AREA_MAX_MONSTERS:
            count = min(count, SPAWN_AREA_MAX_MONSTERS)
        if count <= 0:
            return ()
        width = max(0.01, self.width)
        depth = max(0.01, self.depth)
        columns = max(1, math.ceil(math.sqrt(count * width / depth)))
        rows = max(1, math.ceil(count / columns))
        result = []
        for ordinal in range(count):
            column = ordinal % columns
            row = ordinal // columns
            local_x = (column + 0.5) / columns - 0.5
            local_z = (row + 0.5) / rows - 0.5
            result.append(
                (
                    self.matrix[12]
                    + local_x * self.matrix[0]
                    + local_z * self.matrix[8],
                    self.matrix[13],
                    self.matrix[14]
                    + local_x * self.matrix[2]
                    + local_z * self.matrix[10],
                )
            )
        return tuple(result)


def _spawn_area_variant_signature(
    area: _MonsterSpawnArea,
) -> tuple[float, ...]:
    """Identify duplicated normal/scaling population families.

    Map databases keep one parent-node group per difficulty.  Each group has
    the same map-wide, lowest-priority volume but different density/list
    settings.  Loading every parent at once multiplied the population and
    concentrated the capped result near the entry.  The map-wide transform is
    a stable family key while independent authored regions remain untouched.
    """

    return tuple(
        round(area.matrix[index], 3)
        for index in (0, 2, 8, 10, 12, 14)
    )


def _select_spawn_area_variants(
    areas: tuple[_MonsterSpawnArea, ...],
    scaling_difficulty: int,
    difficulty_variations: Mapping[int, tuple[int, int]] | None = None,
) -> tuple[_MonsterSpawnArea, ...]:
    """Keep the unconditional areas and the instance's difficulty variant.

    An area whose parent node sits under a MapVariation difficulty node
    (``map_difficulty_variations``) belongs to the tier that node names; of
    each family only ``selected_variation_tier`` survives. Picking the
    n-th parent by sorted node index instead put c2303_dun's tier-6
    ``DIFFICULTY_list_mercyless`` area (``difficulty_extra_spawn``, opened by
    every map entry) on a normal instance: 286 extra monsters on top of the
    162 of the base population. The signature heuristic below remains for
    areas the .mvr does not place.
    """

    from dso_server.gameplay.map_event_state import selected_variation_tier

    variations = difficulty_variations or {}
    unconditional = tuple(
        area for area in areas if area.parent_node_index == 0
    )
    tiers: dict[int, set[int]] = {}
    for area in areas:
        variation = variations.get(area.parent_node_index)
        if area.parent_node_index and variation is not None:
            tiers.setdefault(variation[0], set()).add(variation[1])
    chosen_tiers = {
        family: selected_variation_tier(values, scaling_difficulty)
        for family, values in tiers.items()
    }
    selected_parents: set[int] = {
        area.parent_node_index
        for area in areas
        if area.parent_node_index
        and area.parent_node_index in variations
        and chosen_tiers[variations[area.parent_node_index][0]]
        == variations[area.parent_node_index][1]
    }
    by_parent: dict[int, list[_MonsterSpawnArea]] = {}
    for area in areas:
        if area.parent_node_index and area.parent_node_index not in variations:
            by_parent.setdefault(area.parent_node_index, []).append(area)

    families: dict[tuple[float, ...], list[int]] = {}
    for parent_node_index, parent_areas in by_parent.items():
        anchor = min(
            parent_areas,
            key=lambda area: (
                area.priority,
                -area.horizontal_area,
                area.row_id,
            ),
        )
        families.setdefault(
            _spawn_area_variant_signature(anchor), []
        ).append(parent_node_index)

    difficulty = max(0, int(scaling_difficulty))
    for parent_indices in families.values():
        ordered = sorted(parent_indices)
        selected_parents.add(ordered[min(difficulty, len(ordered) - 1)])
    return unconditional + tuple(
        area
        for area in areas
        if area.parent_node_index in selected_parents
    )


def _load_spawn_areas(
    database: sqlite3.Connection,
    *,
    enabled_event_sets: frozenset[int] = frozenset(),
    scaling_difficulty: int = 0,
    preload_gated: bool = False,
) -> tuple[_MonsterSpawnArea, ...]:
    """Read density volumes, honouring their EventSet gate.

    ``preload_gated`` keeps volumes whose gate is still closed. The Endless
    Tower layouts gate whole rooms behind ``h0009_endlesstower_01_room_NN``
    events that retail starts from a ``*_start`` prop interaction; Fractured
    Planes never performs that interaction, so those rooms would otherwise be
    permanently empty and their traps could never be cleared.
    """
    try:
        rows = database.execute(
            """
            SELECT rowid, COALESCE(ParentNodeIndex, 0),
                   COALESCE(EventSetRow, -1), Transform,
                   COALESCE(SpawnListId, ''), SpawnPerMeter, Priority,
                   COALESCE(MinChampionSpawn, 0),
                   COALESCE(MaxChampionSpawn, 0)
            FROM _Instance_SpawnArea
            WHERE COALESCE(SpawnPerMeter, 0)>0
              AND COALESCE(Priority, 0)<999
              AND COALESCE(RespawnTime, 0)<>0
            ORDER BY Priority DESC, rowid
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return ()
    result = []
    for (
        row_id,
        parent_node_index,
        event_set_row,
        raw_transform,
        spawn_list_id,
        density,
        priority,
        min_champion_spawn,
        max_champion_spawn,
    ) in rows:
        event_set_row = int(event_set_row)
        if (
            event_set_row >= 0
            and not preload_gated
            and event_set_row not in enabled_event_sets
        ):
            continue
        try:
            matrix = struct.unpack("<16f", bytes(raw_transform))
        except (TypeError, ValueError, struct.error):
            continue
        area = _MonsterSpawnArea(
            int(row_id),
            int(parent_node_index),
            event_set_row,
            tuple(float(value) for value in matrix),
            str(spawn_list_id),
            max(0.0, float(density)),
            int(priority),
            max(0, int(min_champion_spawn or 0)),
            max(0, int(max_champion_spawn or 0)),
        )
        if area.horizontal_area > 1e-4:
            result.append(area)
    active_signatures = {
        _spawn_area_variant_signature(area)
        for area in result
        if area.event_set_row >= 0
    }
    if active_signatures and not preload_gated:
        result = [
            area
            for area in result
            if (
                area.event_set_row >= 0
                or _spawn_area_variant_signature(area)
                not in active_signatures
            )
        ]
    difficulty_variations: Mapping[int, tuple[int, int]] = {}
    try:
        file_name = resident_content.database_path(database)
        if file_name:
            from dso_server.gameplay.map_event_state import (
                map_difficulty_variations,
            )

            difficulty_variations = map_difficulty_variations(
                Path(file_name)
            )
    except sqlite3.Error:
        pass
    return _select_spawn_area_variants(
        tuple(result), scaling_difficulty, difficulty_variations,
    )


def _grounded_spawn_position(
    navigation: PathEngineNavigationMesh | _DirectNavigationMesh,
    terrain: MapCollisionTerrain | None,
    position: tuple[float, float, float],
    *,
    reference_position: tuple[float, float, float] | None = None,
    max_horizontal_distance: float | None = None,
    prefer_navigation_height: bool = False,
) -> tuple[float, float, float]:
    reference = reference_position or position
    if not isinstance(navigation, PathEngineNavigationMesh):
        projected = tuple(float(component) for component in position)
    else:
        projected = navigation.project_position(
            position,
            max_horizontal_distance=max_horizontal_distance,
        )
    if terrain is not None:
        try:
            return terrain.project_position(
                projected,
                reference_height=(
                    projected[1] if prefer_navigation_height else reference[1]
                ),
            )
        except CollisionTerrainError:
            pass
    if isinstance(navigation, PathEngineNavigationMesh):
        return navigation.project_position_relative(
            position,
            reference,
            max_horizontal_distance=max_horizontal_distance,
        )
    return (projected[0], float(reference[1]), projected[2])


def _grounded_spawn_area_candidates(
    navigation: PathEngineNavigationMesh | _DirectNavigationMesh,
    terrain: MapCollisionTerrain | None,
    area: _MonsterSpawnArea,
    candidates: tuple[tuple[float, float, float], ...],
    *,
    excluded_areas: tuple[_MonsterSpawnArea, ...] = (),
    allowed_component: int | None = None,
    prefer_navigation_height: bool = False,
) -> tuple[tuple[float, float, float], ...]:
    """Place a density grid only on concrete walkable surface triangles."""

    if not isinstance(navigation, PathEngineNavigationMesh):
        if terrain is None:
            return ()
        grounded = []
        for position in candidates:
            try:
                projected = terrain.project_position(
                    position,
                    reference_height=area.center[1],
                )
            except CollisionTerrainError:
                continue
            if (
                area.contains(projected)
                and not any(
                    blocked.contains(projected)
                    for blocked in excluded_areas
                )
            ):
                grounded.append(projected)
        return tuple(grounded)
    if not candidates:
        return ()

    minimum_height = area.center[1] - abs(area.matrix[5]) * 0.5
    maximum_height = area.center[1] + abs(area.matrix[5]) * 0.5
    ground_reference_height = area.center[1]
    if area.spawn_list_id.casefold() == (
        "list_imperial_soldier_q_helios_war_01"
    ):
        # These two Telepolos volumes span multiple vertically-overlapping
        # city layers. Their raw grid points project to the playable street at
        # Y=0, but a fallback triangle can select the lower Y=-3 floor. Keep
        # this quest population on the same street level as its authored grid.
        ground_reference_height = 0.0
        minimum_height = -0.75
        maximum_height = 0.75
    target_count = len(candidates)
    positions = []
    occupied: dict[
        tuple[int, int], list[tuple[float, float, float]]
    ] = {}

    def append(position: tuple[float, float, float]) -> bool:
        if not area.contains(position) or any(
            blocked.contains(position) for blocked in excluded_areas
        ):
            return False
        # Spawn capacity is horizontal. Including Y allowed the same X/Z
        # point on several projected terrain samples, and quarter-unit cells
        # let dense invalid volumes collapse into one apparent spawn point.
        if _spawn_position_is_occupied(occupied, position):
            return False
        _occupy_spawn_position(occupied, position)
        positions.append(position)
        return True

    # Retain grid locations that are already on/very near ground. A tight
    # bound makes holes cheap to reject instead of performing a large nearest
    # triangle search for every generated monster.
    for raw_position in candidates:
        try:
            projected = navigation.project_position(
                raw_position,
                max_horizontal_distance=0.10,
            )
            if (
                allowed_component is not None
                and navigation.component_for_position(projected)
                != allowed_component
            ):
                continue
            if terrain is not None:
                projected = terrain.project_position(
                    projected,
                    reference_height=(
                        projected[1]
                        if prefer_navigation_height
                        else ground_reference_height
                    ),
                    minimum_height=minimum_height,
                    maximum_height=maximum_height,
                )
        except NavigationError:
            continue
        except CollisionTerrainError:
            continue
        append(projected)

    if len(positions) >= target_count:
        return tuple(positions[:target_count])

    # Database spawn volumes commonly overlap non-walkable scenery. Fill the
    # remainder from actual triangle centers inside that same volume instead
    # of piling every rejected grid point onto one navigation boundary.
    walkable = []
    for triangle_id, centroid in enumerate(navigation._centroids):
        if (
            allowed_component is not None
            and navigation._component_by_triangle[triangle_id]
            != allowed_component
        ):
            continue
        if not area.contains(centroid) or any(
            blocked.contains(centroid) for blocked in excluded_areas
        ):
            continue
        position = centroid
        if terrain is not None:
            try:
                position = terrain.project_position(
                    centroid,
                    reference_height=(
                        centroid[1]
                        if prefer_navigation_height
                        else ground_reference_height
                    ),
                    minimum_height=minimum_height,
                    maximum_height=maximum_height,
                )
            except CollisionTerrainError:
                continue
        walkable.append(position)
    walkable.sort(key=lambda value: (value[0], value[2], value[1]))
    needed = target_count - len(positions)
    if walkable and needed:
        preferred_indices = tuple(
            min(
                len(walkable) - 1,
                math.floor((ordinal + 0.5) * len(walkable) / needed),
            )
            for ordinal in range(needed)
        )
        for index in preferred_indices:
            append(walkable[index])
        if len(positions) < target_count:
            for position in walkable:
                append(position)
                if len(positions) >= target_count:
                    break
    return tuple(positions[:target_count])


@lru_cache(maxsize=64)
def _load_map_monster_templates(
    static_database_path: Path,
    map_id: str,
) -> tuple[str, ...]:
    map_prefix = map_id.split("_", 1)[0].lower()
    with _open_read_only(static_database_path) as database:
        map_row = database.execute(
            '''
            SELECT COALESCE(MapType, '')
            FROM _Template_MapTemplate
            WHERE LOWER(MapTemplate)=LOWER(?)
            ORDER BY rowid
            LIMIT 1
            ''',
            (str(map_id),),
        ).fetchone()
        map_prefix = (
            str(map_id).casefold()
            if (
                str(map_id).casefold().startswith('e_')
                or (
                    map_row is not None
                    and str(map_row[0]).casefold() == 'event'
                )
            )
            else map_prefix
        )
        rows = database.execute(
            """
            SELECT Id
            FROM _Template_Monster
            WHERE LOWER(Id) LIKE ?
              AND COALESCE(Obsolete, 0)=0
              AND Rank='Rank_monster'
              AND TRIM(COALESCE(Skills, ''))<>''
            ORDER BY Id
            """,
            (map_prefix + "%",),
        ).fetchall()
    excluded_fragments = (
        "_q_",
        "body_pile",
        "_grave",
        "dummy",
        "invisible",
    )
    templates = tuple(
        str(row[0])
        for row in rows
        if not any(
            fragment in str(row[0]).lower()
            for fragment in excluded_fragments
        )
    )
    if str(map_id).casefold() == "pw001_grimmagstone_01_dun":
        # Prefix matching also finds seasonal burning skeletons and the
        # boss's private adds. Neither belongs to ordinary density lists.
        templates = tuple(
            template for template in templates
            if not template.endswith("_pw_event")
            and "_boss_adds" not in template
            and "_guardian_" not in template
        )
    additions = _MAP_MONSTER_TEMPLATE_ADDITIONS.get(str(map_id).casefold(), ())
    return tuple(dict.fromkeys((*templates, *additions)))


def _area_template_pool(
    static_database_path: Path,
    map_id: str,
    area: _MonsterSpawnArea,
    fallback_templates: tuple[str, ...],
) -> tuple[str, ...]:
    # Empty lists include no_spawn_entrance and no_spawn_boss_area. Keep their
    # volumes for priority exclusion, but never populate them via a fallback.
    if not area.spawn_list_id.strip():
        return ()
    if is_tower_layout(map_id):
        return tower_density_templates(
            static_database_path, area.spawn_list_id, map_id
        )
    override = _SPAWN_LIST_TEMPLATE_OVERRIDES.get(map_id, {}).get(
        area.spawn_list_id
    )
    if override:
        return density_spawn_templates(static_database_path, tuple(override))
    map_templates = density_spawn_templates(
        static_database_path,
        _load_map_monster_templates(static_database_path, map_id),
    )
    return map_templates or density_spawn_templates(
        static_database_path, fallback_templates
    )


def _ensure_dungeon_elites(static_database_path, monsters, *, fixed_monster_count=0):
    """Distribute at least ten ranked enemies through a scaling dungeon."""
    elite_ranks = {'Rank_champion', 'Rank_leader', 'Rank_leader_no_entchantments'}
    needed = max(0, 10 - sum(m.rank in elite_ranks for m in monsters))
    candidates = [i for i, m in enumerate(monsters)
                  if i >= fixed_monster_count and m.rank == 'Rank_monster']
    # Some dungeons are entirely authored SpawnPoints. Their identities stay
    # intact; changing rank does not break template-based quest objectives.
    candidates += [i for i, m in enumerate(monsters)
                   if i < fixed_monster_count and m.rank == 'Rank_monster']
    if not needed or not candidates:
        return monsters
    count = min(needed, len(candidates))
    selected = [candidates[int((n + .5) * len(candidates) / count)] for n in range(count)]
    with _open_read_only(static_database_path) as database:
        factors = {str(r[0]): tuple(float(v) for v in r[1:]) for r in database.execute(
            'SELECT Id, HealthPointsFactor, MinDamageFactor, MaxDamageFactor, ResistanceFactor '
            'FROM _Template_MonsterRanks')}
    result = list(monsters)
    from dataclasses import replace
    for index in selected:
        original = result[index]
        rank = 'Rank_champion'
        old, new = factors[original.rank], factors[rank]
        ratios = {2: new[0]/old[0], 8: new[0]/old[0], 11: new[1]/old[1], 12: new[2]/old[2]}
        ratios.update({i: new[3]/old[3] for i in range(20, 26)})
        stats = tuple(replace(stat, value=stat.value * ratios.get(stat.kind, 1))
                      for stat in original.stats)
        result[index] = replace(original, stats=stats, rank=rank,
                                field_88=round(original.field_88 * new[0]/old[0]),
                                field_90=round(original.field_90 * new[0]/old[0]))
    return tuple(result)


def _navigation_for_map(
    static_database_path: Path,
    map_id: str,
) -> PathEngineNavigationMesh | _DirectNavigationMesh:
    navigation_path = (
        static_database_path.parent.parent
        / "navigation"
        / map_id
        / f"{map_id}.tok"
    )
    try:
        # These maps have authored connections narrower than the generic 1 m
        # portal cutoff. They are walkable in the client and join the map's
        # ground regions, so retain those connections for movement validation
        # and monster pathing.
        portal_width = {
            # This hub's path to J'Ibal/c2200_quest_eldest is split into
            # separate components at the generic cutoff; a 0.75 m
            # connection restores the authored walkable route.
            "c2200_hub": 0.75,
            # Fjalnir's Temple has an authored bridge whose walkable
            # PathEngine portals are narrower than 1 m. The default cutoff
            # isolates the player's approach triangle and movement validation
            # repeatedly snaps the character backward at the bridge.
            "g0005_temple_ice_02_dun": 0.8,
            # The Honor Barracks passage to B'Orlok is authored below 1 m.
            "c2202_pve": 0.9,
            # Cloud bridge connections are authored as very narrow PathEngine
            # portals. Keep every authored connection for player movement.
            "pw023_atlantis_above_water_01_dun": 0.0,
        }.get(str(map_id).casefold(), 1.0)
        return load_navigation_mesh(
            navigation_path, minimum_portal_width=portal_width,
        )
    except (OSError, ValueError):
        return _DirectNavigationMesh()


def _terrain_for_map(
    static_database_path: Path,
    map_id: str,
) -> MapCollisionTerrain | None:
    map_root = static_database_path.parent.parent / "maps"
    try:
        return load_map_collision_terrain(
            map_root / f"{map_id}.map",
            map_root / f"{map_id}.col",
        )
    except (OSError, CollisionTerrainError, UnicodeDecodeError, struct.error):
        # A few development/event maps do not ship a base collision bundle.
        # They retain the PathEngine-relative compatibility path.
        return None


def _load_dynamic_monster_encounter(
    static_database_path: Path,
    map_id: str,
    explicit_definition: dict | None = None,
    include_spawn_areas: bool = True,
    monster_level: int | None = None,
    scaling_difficulty: int = 0,
    player_position: tuple[float, float, float] | None = None,
    active_event_flags: frozenset[str] = frozenset(),
) -> LocalMonsterEncounter | None:
    """Load fixed and density-driven hostile population from a map database."""

    prefer_navigation_height = (
        str(map_id).casefold() == "g0011_temple_death_01_dun"
    )
    encounter_policy = load_map_encounter_policy(
        static_database_path, map_id
    )
    navigation = _navigation_for_map(static_database_path, map_id)
    terrain = _terrain_for_map(static_database_path, map_id)
    if player_position is None:
        try:
            player_position = load_local_spawn_position(
                static_database_path, map_id
            )
        except (OSError, ValueError):
            player_position = None
    allowed_component = None
    if (
        player_position is not None
        and isinstance(navigation, PathEngineNavigationMesh)
    ):
        try:
            allowed_component = navigation.component_for_position(
                player_position,
                max_horizontal_distance=3.0,
            )
        except NavigationError:
            # A handful of composite/event maps ship a PathEngine token for
            # a different level slice than their selected respawn entry.
            # Projecting authored actors onto that distant mesh can move them
            # hundreds of world units. Retain only concrete authored map
            # placements in that case; density candidates are accepted only
            # where the concrete world collision has a ground surface.
            navigation = _DirectNavigationMesh()
    with _open_read_only(
        map_database_path(static_database_path, map_id)
    ) as database:
        enabled_event_sets = _enabled_event_set_ids(
            database,
            active_local_map_event_flags(map_id, active_event_flags),
            map_id=map_id,
        )
        authored_fixed_rows = tuple(database.execute(
            """
            SELECT rowid, Transform, COALESCE(NpcId, ''),
                   COALESCE(EventSetRow, -1), COALESCE(_ID, ''),
                   COALESCE(Rank, '')
            FROM _Instance_SpawnPoint
            WHERE COALESCE(AlliedToPlayer, 0)=0
              AND (
                  TRIM(COALESCE(NpcId, ''))<>''
                  OR lower(COALESCE(Rank, '')) IN (
                      'rank_champion', 'rank_guardian', 'rank_leader',
                      'rank_leader_no_entchantments', 'rank_sentinel'
                  )
              )
            ORDER BY CASE WHEN COALESCE(EventSetRow, -1)=-1 THEN 0 ELSE 1 END,
                     rowid
            """
        ).fetchall())
        all_fixed_rows = []
        for row_id, transform, npc_id, event_set, object_id, _rank in authored_fixed_rows:
            template_id = str(npc_id or "").strip()
            if not template_id:
                template_id = _AUTHORED_SPAWNPOINT_TEMPLATE_OVERRIDES.get(
                    (str(map_id).casefold(), str(object_id or "").casefold()),
                    "",
                )
            if not template_id:
                # Some shipped PW map records put the monster template
                # directly in _ID and leave NpcId empty. Resolve only exact
                # template IDs; *_spawnpoint IDs are quest-gated anchors and
                # must wait for their task interaction.
                candidate = str(object_id or "").strip()
                if candidate and _load_runtime_definition(
                    static_database_path, candidate, 0,
                ) is not None:
                    template_id = candidate
            # Mount Suvius's Gorgon guardians are authored as blank-NpcId
            # SpawnPoints whose StateObject templates name them as the result
            # of clicking a bust. Keep them out of the map-entry population;
            # services.map_events materializes the matching guardian when its
            # one-shot bust is used.
            if (
                str(map_id).casefold() == "pw022_mount_suvius_01_dun"
                and template_id.casefold().startswith("pw022_guardian_gorgon_")
            ):
                continue
            if template_id:
                all_fixed_rows.append((
                    row_id, transform, template_id, event_set,
                ))
        all_fixed_rows = tuple(all_fixed_rows)
        # Tier-only rows: the bloody sludges exist in every dungeon's table
        # without an EventSet but belong to Bloodshed (tier 8) alone.
        from dso_server.gameplay.map_event_state import (
            BLOODSHED_DIFFICULTY,
            bloodshed_only_template,
            deduplicate_pw002_wave_three_warriors,
            load_map_event_content,
            load_map_signal_catalog,
        )
        if int(scaling_difficulty) < BLOODSHED_DIFFICULTY:
            all_fixed_rows = tuple(
                row for row in all_fixed_rows
                if not bloodshed_only_template(row[2])
            )
        all_fixed_rows = deduplicate_pw002_wave_three_warriors(
            map_id, all_fixed_rows,
        )
        # Invisible scene controllers are consumed by the client's map event
        # graph. Creating them as NewMonster actors gives them no renderable
        # model/effect module and can collide with the first real actor id.
        # They are not mobs and must never enter combat or objective counts.
        if is_tower_layout(map_id):
            all_fixed_rows = tuple(
                row for row in all_fixed_rows
                if not is_tower_controller(
                    static_database_path, map_id, str(row[2])
                )
            )
        # Fixed tower hardware stands on the map, it is not a reward. Power
        # nodes sit behind the boss-appear EventSet in the authored script, so
        # honouring that gate would make the arena's rotating towers appear
        # only once the Rift Lord does. Fractured Planes needs them from the
        # first step into the room, so their rows bypass the gate here; the
        # event-spawn path already treats a matching live actor as the same
        # actor instead of spawning a duplicate.
        tower_layout = is_tower_layout(map_id)
        fixed_rows = tuple(
            row
            for row in all_fixed_rows
            if (
                int(row[3]) == -1
                or int(row[3]) in enabled_event_sets
                or (
                    tower_layout
                    and is_tower_mechanism(
                        static_database_path, map_id, str(row[2])
                    )
                )
            )
        )
        selected_event_rows = tuple(
            row
            for row in fixed_rows
            if int(row[3]) >= 0
        )
        if encounter_policy.is_boss_room and not selected_event_rows:
            # A boss room whose boss waits behind an EventSet: only fall back
            # to the gated rows the map-event runtime will never enable. Rows
            # behind a server-driven event are spawned by that event; loading
            # them here as well creates duplicate bosses (notably Balor).
            try:
                content = load_map_event_content(static_database_path, map_id)
                scripted = content.server_driven_event_sets(
                    load_map_signal_catalog(static_database_path)
                )
            except Exception:
                scripted = frozenset()
            if scripted:
                scripted_boss_templates = set()
                for r in all_fixed_rows:
                    if int(r[3]) in scripted:
                        resolved = resolve_boss_template_id(static_database_path, str(r[2])) or str(r[2])
                        scripted_boss_templates.add(resolved.casefold())
                fixed_rows = tuple(
                    row for row in all_fixed_rows
                    if int(row[3]) not in scripted
                    and (resolve_boss_template_id(static_database_path, str(row[2])) or str(row[2])).casefold() not in scripted_boss_templates
                )
            else:
                fixed_rows = tuple(
                    row for row in all_fixed_rows
                    if int(row[3]) not in scripted
                )
        elif (
            not fixed_rows
            and encounter_policy.map_type.casefold() != 'event'
            # Grimmagstone has no ungated fixed mobs. Its fixed rows belong
            # to ritual waves, Raazghul, or seasonal events; density areas
            # provide the ordinary population. Never bypass those gates.
            and str(map_id).casefold() != 'pw001_grimmagstone_01_dun'
        ):
            fixed_rows = all_fixed_rows
        fixed_rows = select_boss_spawn_rows(
            static_database_path,
            map_id,
            tuple(fixed_rows),
            scaling_difficulty=scaling_difficulty,
        )
        spawn_areas = (
            _load_spawn_areas(
                database,
                enabled_event_sets=enabled_event_sets,
                scaling_difficulty=scaling_difficulty,
                preload_gated=is_tower_layout(map_id),
            )
            if (
                SPAWN_AREA_MONSTERS_ENABLED
                and include_spawn_areas
                and encounter_policy.permits_density_population
            )
            else ()
        )
        protected_waypoints = tuple(
            (
                _spawn_position(bytes(row[0])),
                # PickingRange describes cursor interaction, not the complete
                # monster-free footprint around the stone. Retail keeps an
                # broad sanctuary around travel objects. PickingRange covers
                # only cursor interaction; it is substantially smaller than
                # the no-combat region represented by the client minimap.
                max(
                    TRAVEL_STONE_MONSTER_EXCLUSION_RADIUS,
                    float(row[1] or 0.0) + 4.0,
                ),
            )
            for row in database.execute(
                "SELECT Transform, PickingRange "
                "FROM _Instance_TravelPoint "
                "WHERE lower(COALESCE(State, 'active'))='active'"
            )
            if isinstance(row[0], bytes) and len(row[0]) == 64
        )

    monsters: list[NewMonster] = []
    runtime_definitions: list[MonsterRuntimeDefinition] = []
    occupied_positions: dict[
        tuple[int, int], list[tuple[float, float, float]]
    ] = {}
    used_actor_ids: set[int] = set()
    actor_base = (
        TOWER_MONSTER_ACTOR_BASE
        if is_tower_layout(map_id)
        else map_monster_actor_base(map_id, static_database_path=static_database_path)
    )
    reserved_actor_ids = (
        ETERNAL_GROVE_RESERVED_ACTOR_IDS
        if str(map_id).casefold() == ETERNAL_GROVE_MAP_ID
        else frozenset()
    )
    # Claim the client-owned slots up front so neither the authored rows nor
    # the density allocator can hand them to a server monster.
    used_actor_ids.update(reserved_actor_ids)
    next_fixed_actor_id = actor_base

    def allocate_fixed_actor_id() -> int:
        """Return the next free actor id for the authored population."""

        nonlocal next_fixed_actor_id
        while next_fixed_actor_id in used_actor_ids:
            next_fixed_actor_id += 1
        result = next_fixed_actor_id
        next_fixed_actor_id += 1
        return result

    def inside_protected_waypoint(
        position: tuple[float, float, float],
    ) -> bool:
        """Return whether a spawn would overlap a travel-stone safe zone.

        The monster's authored home is also its return-home destination.  A
        home inside a protected waypoint therefore creates an actor which is
        forced into ReturnHome forever.  Reject the grounded position before
        registration so fixed, density, and fallback spawns all obey the same
        invariant.
        """

        return any(
            math.hypot(
                float(position[0]) - float(waypoint[0]),
                float(position[2]) - float(waypoint[2]),
            ) <= float(radius)
            for waypoint, radius in protected_waypoints
        )

    def append_monster(
        template_id: str,
        actor_id: int,
        position: tuple[float, float, float],
        *,
        resolve_projected_overlap: bool = False,
        max_navigation_snap_distance: float = 2.0,
        authored_yaw: float = 0.0,
        hazard: bool = False,
    ) -> bool:
        keep_story_boss_at_authored_position = (
            str(map_id).casefold() == TUTORIAL_GRIMMAG_MAP_ID
            and str(template_id).casefold() == TUTORIAL_GRIMMAG_TEMPLATE_ID
        )
        if int(actor_id) in used_actor_ids:
            return False
        mounted = is_tower_mechanism(static_database_path, map_id, template_id)
        # ``hazard``: a statue trap (gameplay/map_hazards.py) standing in a
        # wall column on its own navigation island. Keep its authored
        # placement and facing, and make it untargetable scenery.
        authored_boss = (
            encounter_policy.is_boss_room
            and is_boss_spawn_template(static_database_path, template_id)
        )
        # PathEngine owns connectivity/XZ. The transformed client collision
        # meshes own the final world Y; authored spawn transforms and spawn
        # volume centers are not reliable terrain samples.
        try:
            position = tuple(position) if mounted or hazard or authored_boss else _grounded_spawn_position(
                navigation,
                terrain,
                position,
                reference_position=position,
                max_horizontal_distance=max_navigation_snap_distance,
                prefer_navigation_height=prefer_navigation_height,
            )
            if (
                not keep_story_boss_at_authored_position
                and not mounted
                and not hazard
                and not authored_boss
                and allowed_component is not None
                and str(map_id).casefold() != "c2303_dun"
                and isinstance(navigation, PathEngineNavigationMesh)
                and navigation.component_for_position(position)
                != allowed_component
            ):
                return False
        except NavigationError:
            if not keep_story_boss_at_authored_position:
                return False
            # This isolated tutorial boss arena can be a different PathEngine
            # component from the map entry. Keep its authored X/Z placement,
            # but still project against collision terrain so a stale or
            # mismatched PathEngine token does not leave Grimmag below the
            # floor (or floating at the authored placeholder height).
            try:
                position = _grounded_spawn_position(
                    _DirectNavigationMesh(),
                    terrain,
                    position,
                    reference_position=position,
                )
            except NavigationError:
                # The direct fallback normally cannot raise NavigationError;
                # retain the authored transform if its implementation does.
                pass
        if inside_protected_waypoint(position):
            return False
        if not mounted and not hazard and _spawn_position_is_occupied(occupied_positions, position):
            if not resolve_projected_overlap:
                return False
            # Several density-grid points can project onto the same boundary
            # edge when a database spawn area extends beyond the walkable
            # polygon. Keep the database population without stacking actors at
            # one coordinate by probing a small grounded spiral nearby.
            resolved = False
            golden_angle = math.pi * (3.0 - math.sqrt(5.0))
            for attempt in range(1, 97):
                radius = 0.3 + 0.18 * ((attempt - 1) // 12)
                angle = attempt * golden_angle
                candidate = (
                    position[0] + math.cos(angle) * radius,
                    position[1],
                    position[2] + math.sin(angle) * radius,
                )
                try:
                    candidate = _grounded_spawn_position(
                        navigation,
                        terrain,
                        candidate,
                        reference_position=position,
                        max_horizontal_distance=1.0,
                        prefer_navigation_height=prefer_navigation_height,
                    )
                except NavigationError:
                    continue
                if (
                    terrain is not None
                    and not terrain.heights_at(candidate)
                ):
                    continue
                if (
                    allowed_component is not None
                    and isinstance(navigation, PathEngineNavigationMesh)
                    and navigation.component_for_position(candidate)
                    != allowed_component
                ):
                    continue
                if inside_protected_waypoint(candidate):
                    continue
                if _spawn_position_is_occupied(
                    occupied_positions, candidate
                ):
                    continue
                position = candidate
                resolved = True
                break
            if not resolved:
                return False
        runtime = _load_runtime_definition(
            static_database_path, str(template_id), int(actor_id),
            stationary=mounted,
        )
        if runtime is None:
            return False
        try:
            monster = _load_monster(
                static_database_path,
                str(template_id),
                int(actor_id),
                position,
                monster_level=monster_level,
                scaling_difficulty=scaling_difficulty,
                coefficient_stats=mounted,
                non_targetable=hazard or is_tower_hardware(
                    static_database_path, map_id, template_id
                ),
            )
        except ValueError:
            return False
        if is_tower_layout(map_id) or hazard:
            monster = replace(monster, field_a0=float(authored_yaw))
        _occupy_spawn_position(occupied_positions, position)
        used_actor_ids.add(int(actor_id))
        monsters.append(monster)
        runtime_definitions.append(runtime)
        return True

    if explicit_definition is not None:
        for actor_id, position in zip(
            explicit_definition["actor_ids"],
            explicit_definition["positions"],
            strict=True,
        ):
            if not append_monster(
                str(explicit_definition["template_id"]),
                int(actor_id),
                tuple(float(value) for value in position),
            ):
                raise ValueError(
                    f"invalid explicit monster actor {actor_id} on {map_id}"
                )
    else:
        # Base rows and the selected EventSet form one authored population.
        # Inactive EventSets were removed before boss-phase resolution.
        row_sets = (tuple(fixed_rows),)
        bloodshed_spawn_positions = set()
        # The Winter Summit authors its statue traps as four MapVariation
        # variants over the same column positions (which guardian stands
        # where). One statue per column; stacked variants would fire 4x.
        hazard_spawn_positions = set()
        for rows in row_sets:
            before = len(monsters)
            for _row_id, raw_transform, raw_template_id, _event_set in rows:
                try:
                    position = _spawn_position(bytes(raw_transform))
                except (TypeError, ValueError):
                    continue
                if (
                    str(map_id).casefold() == TUTORIAL_GRIMMAG_MAP_ID
                    and str(raw_template_id).casefold()
                    == TUTORIAL_GRIMMAG_TEMPLATE_ID
                ):
                    # Spawn Grimmag at the center of the last authored wave,
                    # not at the placeholder's distant staging position.
                    position = TUTORIAL_GRIMMAG_ARENA_POSITION
                is_bloodshed_sludge = bloodshed_only_template(raw_template_id)
                template_id, position = _eternal_grove_commander_replacement(
                    map_id, str(raw_template_id), position,
                )
                if is_bloodshed_sludge:
                    # The map tables duplicate each sludge spawn point twice
                    # with identical transforms. Keep one actor per authored
                    # location before applying the overlap recovery below.
                    position_key = (round(position[0], 3), round(position[2], 3))
                    if position_key in bloodshed_spawn_positions:
                        continue
                    bloodshed_spawn_positions.add(position_key)
                # Statues an EventSet switches (Eternal Watch's traps, the
                # Kranparus lairs', the Big Paws Mom room turrets) leave with
                # their set: map_events retires a disabled set's monsters by
                # template and authored position, which a statue keeps.
                hazard = is_map_hazard(static_database_path, template_id)
                if hazard:
                    position_key = (round(position[0], 1), round(position[2], 1))
                    if position_key in hazard_spawn_positions:
                        continue
                    hazard_spawn_positions.add(position_key)
                actor_id = allocate_fixed_actor_id()
                if (
                    str(map_id).casefold() == TUTORIAL_GRIMMAG_MAP_ID
                    and str(raw_template_id).casefold()
                    == TUTORIAL_GRIMMAG_TEMPLATE_ID
                ):
                    actor_id = TUTORIAL_GRIMMAG_RUNTIME_ACTOR_ID
                elif (
                    str(map_id).casefold() == FRACTURED_PLANES_BOSS_MAP_ID
                    and str(raw_template_id).casefold()
                    == FRACTURED_PLANES_BOSS_TEMPLATE_ID
                ):
                    actor_id = FRACTURED_PLANES_BOSS_ACTOR_ID
                placed = append_monster(
                    template_id, actor_id, position,
                    authored_yaw=math.atan2(
                        struct.unpack("<16f", bytes(raw_transform))[8],
                        struct.unpack("<16f", bytes(raw_transform))[0],
                    ) if is_tower_layout(map_id) or hazard else 0.0,
                    hazard=hazard,
                    # Bloodshed challenges author five sludges on every
                    # eligible dungeon map. A few maps overlap one sludge's
                    # transform with an earlier actor; keep the challenge
                    # count by resolving that collision to nearby walkable
                    # ground instead of silently dropping the sludge.
                    resolve_projected_overlap=is_bloodshed_sludge,
                    max_navigation_snap_distance=(
                        32.0 if is_bloodshed_sludge else 2.0
                    ),
                )
                if (
                    is_bloodshed_sludge
                    and not placed
                    and allowed_component is not None
                    and isinstance(navigation, PathEngineNavigationMesh)
                    and player_position is not None
                ):
                    # A few composite maps author a sludge on a disconnected
                    # navigation island. Keep the five-mob challenge playable
                    # by placing that sludge on nearby walkable ground in the
                    # player's connected component.
                    try:
                        candidates = navigation.component_positions(
                            allowed_component,
                            around=player_position,
                            minimum_distance=8.0,
                            maximum_distance=32.0,
                            limit=32,
                        )
                    except NavigationError:
                        candidates = ()
                    for candidate in candidates:
                        if append_monster(
                            str(raw_template_id), actor_id, candidate,
                            resolve_projected_overlap=True,
                        ):
                            break
            if len(monsters) > before:
                break

        if str(map_id).casefold() == ETERNAL_GROVE_MAP_ID:
            present_templates = {
                monster.template_id.casefold() for monster in monsters
            }
            for template_id, position in ETERNAL_GROVE_MISSING_COMMANDER_SPAWNS:
                if template_id.casefold() in present_templates:
                    continue
                actor_id = allocate_fixed_actor_id()
                placed = append_monster(
                    template_id,
                    actor_id,
                    position,
                    resolve_projected_overlap=True,
                )
                if (
                    not placed
                    and allowed_component is not None
                    and player_position is not None
                    and isinstance(navigation, PathEngineNavigationMesh)
                ):
                    try:
                        candidates = navigation.component_positions(
                            allowed_component,
                            around=player_position,
                            minimum_distance=6.0,
                            maximum_distance=24.0,
                            limit=128,
                        )
                    except NavigationError:
                        candidates = ()
                    for candidate in candidates:
                        if (
                            terrain is not None
                            and len(terrain.heights_at(candidate)) != 1
                        ):
                            continue
                        if append_monster(
                            template_id,
                            allocate_fixed_actor_id(),
                            candidate,
                            resolve_projected_overlap=True,
                        ):
                            placed = True
                            break
                if placed:
                    present_templates.add(template_id.casefold())

        if str(map_id).casefold() == "c2202_pve":
            existing_guards = sum(
                monster.template_id.casefold()
                == _WARRIORS_DESTINY_GORGON_TEMPLATE.casefold()
                for monster in monsters
            )
            for position in _WARRIORS_DESTINY_GORGON_FALLBACK_SPAWNS[
                existing_guards:
            ]:
                append_monster(
                    _WARRIORS_DESTINY_GORGON_TEMPLATE,
                    allocate_fixed_actor_id(),
                    position,
                    resolve_projected_overlap=True,
                )

    fixed_monster_count = len(monsters)
    authored_templates = tuple(
        dict.fromkeys(
            str(row[2])
            for row in fixed_rows
            if str(row[2] or "").strip()
        )
    )
    fallback_templates = (
        tuple(dict.fromkeys(
            monster.template_id for monster in monsters
        ))
        or authored_templates
    )
    next_actor_id = actor_base

    def allocate_actor_id() -> int:
        nonlocal next_actor_id
        while next_actor_id in used_actor_ids:
            next_actor_id += 1
        result = next_actor_id
        next_actor_id += 1
        return result

    # Assign existing hand-authored actors to the highest-priority containing
    # area so they count toward, rather than inflate, its density target.
    existing_by_area = {area.row_id: 0 for area in spawn_areas}
    for monster in monsters:
        containing = [
            area for area in spawn_areas if area.contains(monster.position)
        ]
        if containing:
            selected = max(
                containing, key=lambda area: (area.priority, -area.row_id)
            )
            existing_by_area[selected.row_id] += 1

    area_candidates = []
    area_template_pools = {}
    for area in spawn_areas:
        template_pool = _area_template_pool(
            static_database_path, map_id, area, fallback_templates,
        )
        if not template_pool:
            continue
        area_template_pools[area.row_id] = template_pool
        excluded_areas = tuple(
            other for other in spawn_areas if other.priority > area.priority
        )
        raw_candidates = tuple(
            position
            for position in area.candidate_positions()
            if not any(
                other.contains(position) for other in excluded_areas
            )
        )
        candidates = _grounded_spawn_area_candidates(
            navigation,
            terrain,
            area,
            raw_candidates,
            excluded_areas=excluded_areas,
            allowed_component=allowed_component,
            prefer_navigation_height=prefer_navigation_height,
        )
        candidates = tuple(
            position
            for position in candidates
            if not inside_protected_waypoint(position)
        )
        if candidates:
            area_candidates.append((area, candidates))

    spawn_area_target_count = sum(
        len(candidates) for _area, candidates in area_candidates
    )
    active_population_target = spawn_area_target_count
    if SPAWN_AREA_MAX_TOTAL_MONSTERS:
        fixed_outside_spawn_areas = max(
            0, fixed_monster_count - sum(existing_by_area.values())
        )
        active_population_target = min(
            active_population_target,
            max(
                0,
                SPAWN_AREA_MAX_TOTAL_MONSTERS
                - fixed_outside_spawn_areas,
            ),
        )

    quotas = [0] * len(area_candidates)
    if active_population_target and area_candidates:
        if active_population_target >= len(area_candidates):
            quotas = [1] * len(area_candidates)
            unassigned = active_population_target - len(area_candidates)
            weights = [
                max(0, len(candidates) - 1)
                for _area, candidates in area_candidates
            ]
        else:
            unassigned = active_population_target
            weights = [
                len(candidates)
                for _area, candidates in area_candidates
            ]
        weight_total = sum(weights)
        if weight_total:
            exact_shares = [
                unassigned * weight / weight_total for weight in weights
            ]
            additions = [math.floor(value) for value in exact_shares]
            for index, addition in enumerate(additions):
                quotas[index] += addition
            remainder = unassigned - sum(additions)
            fractions = sorted(
                range(len(exact_shares)),
                key=lambda index: (
                    exact_shares[index] - additions[index],
                    len(area_candidates[index][1]),
                    -index,
                ),
                reverse=True,
            )
            for index in fractions[:remainder]:
                quotas[index] += 1

    spawn_area_monster_count = 0
    for (area, candidates), quota in zip(
        area_candidates, quotas, strict=True
    ):
        quota = min(quota, len(candidates))
        if 0 < quota < len(candidates):
            candidates = tuple(
                candidates[
                    min(
                        len(candidates) - 1,
                        math.floor(
                            (ordinal + 0.5) * len(candidates) / quota
                        ),
                    )
                ]
                for ordinal in range(quota)
            )
        else:
            candidates = candidates[:quota]
        already_present = existing_by_area.get(area.row_id, 0)
        if already_present:
            candidates = candidates[min(already_present, len(candidates)):]
        template_pool = area_template_pools[area.row_id]
        for ordinal, position in enumerate(candidates):
            actor_id = allocate_actor_id()
            for offset in range(len(template_pool)):
                template_id = template_pool[
                    (area.row_id + ordinal + offset) % len(template_pool)
                ]
                if append_monster(
                    template_id,
                    actor_id,
                    position,
                    resolve_projected_overlap=True,
                    max_navigation_snap_distance=0.10,
                    authored_yaw=math.atan2(area.matrix[8], area.matrix[0]),
                ):
                    spawn_area_monster_count += 1
                    break

    if str(map_id).casefold() == "f4201_pve":
        # The authored tank_dwarf_dragon SpawnArea requests one or two
        # champions, but the retail SpawnList registry is absent from the
        # server data. The ordinary pool contains Rank_monster templates only,
        # so its Dragon Berserker commanders were silently omitted.
        for area in spawn_areas:
            if (
                area.spawn_list_id.casefold() != "tank_dwarf_dragon"
                or area.max_champion_spawn <= 0
            ):
                continue
            template_id = "f4201_s_champion_gen_dragon_berserker"
            if _load_runtime_definition(
                static_database_path, template_id, 0,
            ) is None:
                continue
            excluded_areas = tuple(
                other for other in spawn_areas
                if other.priority > area.priority
            )
            candidates = _grounded_spawn_area_candidates(
                navigation,
                terrain,
                area,
                area.candidate_positions(),
                excluded_areas=excluded_areas,
                allowed_component=allowed_component,
                prefer_navigation_height=prefer_navigation_height,
            )
            candidates = tuple(
                position for position in candidates
                if not inside_protected_waypoint(position)
            )
            if not candidates:
                continue
            # The map authors a minimum of one and a maximum of two for this
            # champion area; use its upper bound so the plural commander group
            # is consistently present in the server's map population.
            champion_count = min(area.max_champion_spawn, len(candidates))
            for ordinal in range(champion_count):
                index = min(
                    len(candidates) - 1,
                    math.floor(
                        (ordinal + 0.5) * len(candidates) / champion_count
                    ),
                )
                if append_monster(
                    template_id,
                    allocate_actor_id(),
                    candidates[index],
                    resolve_projected_overlap=True,
                ):
                    spawn_area_monster_count += 1

    # Some shipped maps place all authored spawn transforms on disconnected
    # event/scenery islands relative to the selected entry point. They remain
    # invalid for this instance. Populate a small, deterministic replacement
    # set from triangle centers on the player's actual walkable component
    # instead of snapping those invalid actors across impassable space.
    if (
        not monsters
        and authored_templates
        and player_position is not None
        and allowed_component is not None
        and isinstance(navigation, PathEngineNavigationMesh)
    ):
        safe_positions = navigation.component_positions(
            allowed_component,
            around=player_position,
            minimum_distance=8.0,
            maximum_distance=32.0,
            limit=(
                1
                if encounter_policy.is_boss_room
                else min(12, max(4, len(authored_templates) * 2))
            ),
        )
        # A non-boss map can also contain a unique boss. If all authored
        # points are unreachable, the replacement pass must not cycle that
        # boss through every safe position along with the supporting actors.
        unique_templates = {
            template_id for template_id in authored_templates
            if is_boss_spawn_template(static_database_path, template_id)
        }
        placed_unique_templates = set()
        for ordinal, position in enumerate(safe_positions):
            actor_id = allocate_actor_id()
            for offset in range(len(authored_templates)):
                template_id = authored_templates[
                    (ordinal + offset) % len(authored_templates)
                ]
                if template_id in placed_unique_templates:
                    continue
                if append_monster(
                    template_id,
                    actor_id,
                    position,
                    resolve_projected_overlap=True,
                ):
                    if template_id in unique_templates:
                        placed_unique_templates.add(template_id)
                    spawn_area_monster_count += 1
                    break

    if not monsters:
        return None

    if (monster_level is not None and encounter_policy.area_type.casefold() == 'closedpve'
            and not encounter_policy.is_boss_room):
        monsters = list(_ensure_dungeon_elites(static_database_path, tuple(monsters),
                                             fixed_monster_count=fixed_monster_count))

    grouped_monsters = _group_dynamic_monsters(tuple(monsters))
    spawn_groups = tuple(
        _spawn_group(static_database_path, group)
        for group in grouped_monsters
    )
    all_commands = tuple(
        command
        for group in spawn_groups
        for command in group.spawn_batch.commands
    )
    actor_ids = tuple(monster.actor_id for monster in monsters)
    trigger_center = (
        sum(monster.position[0] for monster in monsters) / len(monsters),
        sum(monster.position[1] for monster in monsters) / len(monsters),
        sum(monster.position[2] for monster in monsters) / len(monsters),
    )
    trigger_radius = max(
        group.trigger_radius
        + math.hypot(
            group.trigger_center[0] - trigger_center[0],
            group.trigger_center[2] - trigger_center[2],
        )
        for group in spawn_groups
    )
    primary = runtime_definitions[0]
    return LocalMonsterEncounter(
        map_id=map_id,
        static_database_path=static_database_path,
        actor_ids=actor_ids,
        trigger_center=trigger_center,
        trigger_radius=trigger_radius,
        navigation=navigation,
        terrain=terrain,
        coordinate_frame=load_map_coordinate_frame(
            static_database_path, map_id
        ),
        attack=primary.attack,
        run_speed=primary.run_speed,
        aggro_range=primary.aggro_range,
        attack_interval=primary.attack_interval,
        capsule_radius=primary.capsule_radius,
        vicinity=ActorsEnterVicinityBatch(
            ActorsEnterVicinity(actor_ids), actor_id=0, commands=()
        ),
        spawn_batch=MonsterSpawnBatch(all_commands),
        runtime_definitions=tuple(runtime_definitions),
        spawn_groups=spawn_groups,
        fixed_monster_count=fixed_monster_count,
        spawn_area_monster_count=spawn_area_monster_count,
        spawn_area_target_count=spawn_area_target_count,
        scaling_difficulty=max(0, int(scaling_difficulty)),
        protected_waypoints=protected_waypoints,
        prematerialized_spawn_area_rows=frozenset(
            area.row_id for area in spawn_areas if area.event_set_row >= 0
        ),
    )


# Encounters retain their map navigation mesh and generated actor population,
# about 30-40 MiB each.  A miss costs 1-1.6 s of GIL-bound generation, which a
# player entering that map may wait on.  Four entries was not enough for a
# live evening (seven maps in use); the 64-bit runtime can afford more.
MONSTER_ENCOUNTER_CACHE_SIZE = max(
    1, int(os.environ.get("DSO_MONSTER_ENCOUNTER_CACHE_SIZE", "12"))
)
_MONSTER_ENCOUNTER_CACHE: (
    "OrderedDict[tuple, LocalMonsterEncounter | None]"
) = OrderedDict()
_MONSTER_ENCOUNTER_CACHE_LOCK = threading.Lock()
# Generations already running, so concurrent callers wait for one build
# instead of each spending seconds producing an equivalent population.
_MONSTER_ENCOUNTER_BUILDS: "dict[tuple, threading.Event]" = {}


def _spawn_component_key(
    static_database_path: Path,
    map_id: str,
    player_position: tuple[float, float, float] | None,
) -> tuple:
    """Reduce a spawn position to what the generated population depends on.

    The position selects the walkable component the player entered on and -
    only for maps whose authored spawns are all unreachable from it - the
    fallback placement around that entry.  Everything else comes from the map
    database, so two entries on one component generate the same actors.

    Keying the cache on the raw float position instead missed on every real
    entry: re-entering two centimetres away, or relogging onto a stored
    position, rebuilt the whole population.  On a 768-monster map that is a
    multi-second stall on the single receive thread, during which the server
    reads no packets and acknowledges nothing.
    """

    if player_position is None:
        # The loader resolves the map's own default spawn, which is stable.
        return ("default",)
    navigation = _navigation_for_map(static_database_path, map_id)
    if not isinstance(navigation, PathEngineNavigationMesh):
        return ("direct",)
    try:
        return (
            "component",
            navigation.component_for_position(
                player_position, max_horizontal_distance=3.0
            ),
        )
    except NavigationError:
        # No reachable component: the loader then keeps only concrete authored
        # placements, which do not depend on where the player stood.
        return ("unreachable",)


def _load_cached_monster_encounter(
    static_database_path: Path,
    map_id: str,
    include_spawn_areas: bool,
    monster_level: int | None,
    scaling_difficulty: int,
    player_position: tuple[float, float, float] | None,
    active_event_flags: frozenset[str],
) -> LocalMonsterEncounter | None:
    normalized_event_flags = tuple(sorted(
        str(value).casefold() for value in active_event_flags
    ))
    key = (
        static_database_path,
        map_id,
        include_spawn_areas,
        monster_level,
        scaling_difficulty,
        normalized_event_flags,
        _spawn_component_key(static_database_path, map_id, player_position),
    )
    while True:
        with _MONSTER_ENCOUNTER_CACHE_LOCK:
            if key in _MONSTER_ENCOUNTER_CACHE:
                _MONSTER_ENCOUNTER_CACHE.move_to_end(key)
                return _MONSTER_ENCOUNTER_CACHE[key]
            in_flight = _MONSTER_ENCOUNTER_BUILDS.get(key)
            if in_flight is None:
                in_flight = threading.Event()
                _MONSTER_ENCOUNTER_BUILDS[key] = in_flight
                break
        # Someone is already generating this population.  Wait for it rather
        # than spending seconds building a second copy: a pre-warm started at
        # route-arming time is usually still running when the client asks to
        # be initialized.  A failed build leaves the key absent, so the next
        # waiter becomes the builder.
        in_flight.wait()
    try:
        # Generated outside the cache lock so one slow build cannot block a
        # peer entering an already-cached map.
        encounter = _load_dynamic_monster_encounter(
            static_database_path,
            map_id,
            _LOCAL_ENCOUNTERS.get(map_id),
            include_spawn_areas,
            monster_level,
            scaling_difficulty,
            player_position,
            frozenset(normalized_event_flags),
        )
        with _MONSTER_ENCOUNTER_CACHE_LOCK:
            _MONSTER_ENCOUNTER_CACHE[key] = encounter
            _MONSTER_ENCOUNTER_CACHE.move_to_end(key)
            while len(_MONSTER_ENCOUNTER_CACHE) > MONSTER_ENCOUNTER_CACHE_SIZE:
                _MONSTER_ENCOUNTER_CACHE.popitem(last=False)
        # A population lives for the rest of the process.  Keep its tens of
        # thousands of objects out of every later cyclic collection.
        freeze_long_lived_addition()
        return encounter
    finally:
        with _MONSTER_ENCOUNTER_CACHE_LOCK:
            _MONSTER_ENCOUNTER_BUILDS.pop(key, None)
        in_flight.set()


def monster_encounter_cache_size() -> int:
    """Return how many encounters the handoff cache currently retains."""

    with _MONSTER_ENCOUNTER_CACHE_LOCK:
        return len(_MONSTER_ENCOUNTER_CACHE)


def clear_monster_encounter_cache() -> None:
    """Drop every retained encounter population."""

    with _MONSTER_ENCOUNTER_CACHE_LOCK:
        _MONSTER_ENCOUNTER_CACHE.clear()


_PLAYER_ONLY_ATTACK = MonsterAttackDefinition(
    resource_id="",
    command_id=0,
    skill_level=0,
    skill_manager_index=0,
    hit_frame=0,
    motion_unblock_frame=0,
    skill_unblock_frame=0,
    attack_range=0.0,
    hit_range=0.0,
    bullet_emit_delay=0.0,
    projectile_velocity=0.0,
    projectile_radius=0.0,
)


_PLAYER_ONLY_ENCOUNTERS: dict[tuple[Path, str], LocalMonsterEncounter] = {}
_PLAYER_ONLY_ENCOUNTER_LOCK = threading.Lock()


def load_local_player_only_encounter(
    static_database_path: str | Path,
    map_id: str,
) -> LocalMonsterEncounter:
    """An encounter with no monsters, for a map where players fight players.

    ``load_local_monster_encounter`` returns ``None`` for a map that spawns
    nothing, and the initialization pipeline reads that as "no combat here at
    all" -- it builds no ``LocalCombatState``, so no player health is
    registered and every cast is rejected with *the current map has no local
    combat encounter*. That is the right answer for a town and the wrong one
    for a battleground, where the whole point is that the players are the
    combatants.

    This carries the same map geometry a populated encounter would --
    navigation, terrain and the coordinate frame all come from the map -- and
    simply has an empty roster.
    """

    static_database_path = Path(static_database_path)
    map_id = str(map_id)
    key = (static_database_path, map_id)
    with _PLAYER_ONLY_ENCOUNTER_LOCK:
        cached = _PLAYER_ONLY_ENCOUNTERS.get(key)
    if cached is not None:
        return cached
    encounter = LocalMonsterEncounter(
        map_id=map_id,
        static_database_path=static_database_path,
        actor_ids=(),
        trigger_center=(0.0, 0.0, 0.0),
        trigger_radius=0.0,
        navigation=_navigation_for_map(static_database_path, map_id),
        terrain=_terrain_for_map(static_database_path, map_id),
        coordinate_frame=load_map_coordinate_frame(
            static_database_path, map_id
        ),
        attack=_PLAYER_ONLY_ATTACK,
        run_speed=0.0,
        aggro_range=0.0,
        attack_interval=0.0,
        capsule_radius=0.0,
        vicinity=ActorsEnterVicinityBatch(
            ActorsEnterVicinity(()), actor_id=0, commands=()
        ),
        spawn_batch=MonsterSpawnBatch(()),
    )
    with _PLAYER_ONLY_ENCOUNTER_LOCK:
        # Everyone on one map must share one encounter object: the combat
        # state is keyed on its identity, and two players holding different
        # ones would each get their own combat world and never be able to
        # touch each other.
        return _PLAYER_ONLY_ENCOUNTERS.setdefault(key, encounter)


def load_local_monster_encounter(
    static_database_path: str | Path,
    map_id: str,
    *,
    include_spawn_areas: bool = True,
    monster_level: int | None = None,
    scaling_difficulty: int = 0,
    player_position: tuple[float, float, float] | None = None,
    active_event_flags: frozenset[str] = frozenset(),
) -> LocalMonsterEncounter | None:
    """Build fixed and spawn-area population, or ``None`` for safe maps."""

    return _load_cached_monster_encounter(
        Path(static_database_path),
        str(map_id),
        bool(include_spawn_areas),
        None if monster_level is None else int(monster_level),
        int(scaling_difficulty),
        (
            None
            if player_position is None
            else tuple(float(value) for value in player_position)
        ),
        frozenset(str(value).casefold() for value in active_event_flags),
    )
