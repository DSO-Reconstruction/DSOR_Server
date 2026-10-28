"""Build typed gameplay entity state from the client/server map database."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
from functools import lru_cache
import hashlib
import math
import os
import sqlite3
import struct
from pathlib import Path

from dso_server.runtime import resident_content
from dso_server.protocol.gameplay_protocol import (
    ActorPlacement,
    NewNPC,
    NPCInfo,
    PropInfo,
    PropInfoEntry,
)
from dso_server.protocol.player_init_protocol import (
    EXIT_INFO_COMMAND,
    NEW_NPC_COMMAND,
    NPC_INFO_COMMAND,
    VARIATION_INFO_COMMAND,
    ExitInfo,
    ExitInfoEntry,
    ServerCommand,
    VariationInfo,
    VariationInfoEntry,
)
from dso_server.gameplay.map_collision_terrain import load_map_collision_terrain


LOCAL_NPC_ACTOR_BASE = 0x10000
# Client actor ids are generational (generation in the high word, slot in the
# low word; every retail id is 0x1xxxx).  The local player is sent as actor 1
# and the client keeps it as slot 1 of generation 1, i.e. 0x10001, before any
# map entity arrives.  A NewNPC with that id is refused ("ActorManager::
# RegisterActor(): There is an existing actor with id 65537", then "Could not
# factor new NPC"), which is what hid the tutorial's ranger (row 2) and, in
# Kingshill, the arena merchant.  NPC ids therefore skip that slot.
LOCAL_PLAYER_CLIENT_ACTOR_ID = LOCAL_NPC_ACTOR_BASE + 1


def npc_actor_id_for_row(row_id: int) -> int:
    """The stable actor id of ``_Instance_NPC`` row ``row_id`` (1-based)."""

    actor_id = LOCAL_NPC_ACTOR_BASE + int(row_id) - 1
    if actor_id >= LOCAL_PLAYER_CLIENT_ACTOR_ID:
        actor_id += 1
    return actor_id


# The wire profession code is the index of this list.  Client release261
# keeps the names as one contiguous .rdata pool at 0x140ebe4cf..0x140ebe640,
# and UI::NpcWidget switches on the index (`cmp r15d, 0x11` -> "openBank" at
# 0x1404ad9ec).  A profession that is not listed here is announced as 0xFF,
# which builds no widget button at all, so the vendor looks inert.  Note that
# the compare chain at 0x14017eb50 numbers these same names differently;
# those values are internal and never go on the wire.
NPC_PROFESSIONS = (
    "alchemist",
    "jewelry",
    "weapon",
    "armor",
    "smith",
    "simple_smith",
    "essence",
    "priest",
    "monger",
    "mage_skilltrainer",
    "warrior_skilltrainer",
    "ranger_skilltrainer",
    "dwarf_skilltrainer",
    "niwalk_skilltrainer",
    "arena_merchant",
    "equip",
    "crafting",
    "banker",
    "tailor",
    "event_manager",
    "mount_trader",
    "privileged_trader",
    "gambler_trader",
    "fragment_trader",
    "lucky_sphere_trader",
    "food_recycling",
    "rank_of_planar_fissure",
    "map_access_planar_fissure",
)
NPC_CATEGORY_BY_PROFESSION = {
    name: index for index, name in enumerate(NPC_PROFESSIONS)
}
LOCAL_PROP_ID_BASE = 0x10000
KINGSCITY_MAP_ID = "a0200_kingscity"
PARALLEL_WORLDS_HUB_MAP_ID = "pw000_hub"
TUTORIAL_DUNGEON_MAP_ID = "a0001_start_tutorial_dun"
TUTORIAL_CULTIST_EVENT_FLAG = "a0001_start_dun_undead_mage_01"
BASELINE_MAP_EVENT_FLAGS = {
    # These are permanent account/world unlocks on the local development
    # realm, not rotating game events.  Keeping them explicit makes Gnob,
    # Petyax, Thabo, Zumpe, and the fairground herald available while the
    # remaining event-set NPCs still follow ActiveEvents.
    KINGSCITY_MAP_ID: frozenset({
        "a0200_unlock_fairground",
        "a0200_crafting_npc_exchange",
        "a0200_unlock_thabo",
        "a0200_unlock_zumpe",
        "a0200_unlock_fairground_npc",
    }),
    # The Stillwater Bay travel stone is authored behind this permanent map
    # event set. Enable it on every entry so the stone is present before the
    # client renders the map's interactive objects.
    "g0001_beach_01_dun": frozenset({"g0001_travelstone"}),
    # The classic Parallel Worlds hub authors each chapter portal behind a
    # permanent, map-local EventSet. Keep all of those entrances available
    # alongside Grimmagstone so every chapter can be played through locally.
    PARALLEL_WORLDS_HUB_MAP_ID: frozenset({
        "pw000_unlock_pw001_exit",
        "pw000_unlock_pw004_exit",
        "pw000_unlock_pw007_exit",
        "pw000_unlock_pw010_exit",
        "pw000_unlock_pw013_exit",
        "pw000_unlock_pw016_exit",
        "pw000_unlock_pw019_exit",
        "pw000_unlock_pw022_exit",
        "pw000_unlock_pw025_exit",
    }),
    # Parallel World boss-reward chests must be present in the initial prop
    # snapshot. Their map-local events still start on entry, but relying only
    # on that later EventUpdate can miss the scene props during map loading.
    # Interaction stays locked server-side until the arena boss dies.
    "pw003_grimmag_01_boss": frozenset({"pw003_spawn_chest"}),
    "pw006_arachna_01_boss": frozenset({"pw006_spawn_chest"}),
    "pw009_heredur_01_boss": frozenset({"pw009_spawn_chest"}),
    "pw012_bearach_01_boss": frozenset({"pw012_spawn_chest"}),
    "pw015_khalys_boss_01": frozenset({"pw015_spawn_chest"}),
    "pw018_herold_01_boss": frozenset({"pw018_spawn_chest"}),
    "pw021_sigrismarr_01_boss": frozenset({"pw021_spawn_chest"}),
    "pw024_medusa_01_boss": frozenset({"pw024_spawn_chest"}),
    "pw027_gorga_01_boss": frozenset({"pw027_spawn_chest"}),
}


# The Kingshill debug vendor sells uniques, mythics, top-tier gems and
# boosters for one gold each, so it must not be reachable on a published
# server. It lives in the map database like any other NPC; this switch is what
# hides it, and it hides the NPC itself, which also stops its shop resolving.
# Default: on for a loopback bind, off for anything reachable from a network.
# DSO_DEBUG_VENDOR forces either, and the rows can stay installed while off.
# Both debug vendors: the stock needs eight category tabs and the client
# shows six, so it is split over two NPCs standing together.
DEBUG_VENDOR_NPC_IDS = ("a0200_debug_vendor", "a0200_debug_vendor_two")
DEBUG_VENDOR_NPC_ID = DEBUG_VENDOR_NPC_IDS[0]
DEBUG_VENDOR_SHOP_ID = "debug_vendor_shop"


def debug_vendor_enabled() -> bool:
    """Whether the debug vendor is served, read fresh so tests can vary it."""

    setting = os.getenv("DSO_DEBUG_VENDOR")
    if setting is not None:
        return setting.strip().lower() in ("1", "true", "yes", "on")
    return os.getenv("DSO_HOST", "127.0.0.1") in (
        "127.0.0.1", "localhost", "::1",
    )


@dataclass(frozen=True)
class LocalNPCDefinition:
    """One deterministic local actor and its map-database NPC metadata."""

    actor_id: int
    row_id: int
    instance_guid: bytes
    instance_id: str
    template_id: str
    shop_id: str
    profession: str
    graphics: str
    actor_shape_id: str
    character_set: str
    event_id: str
    position: tuple[float, float, float]
    rotation: float
    event_set_row: int = -1


@dataclass(frozen=True)
class LocalMapExit:
    """One active map exit and the destination entry it targets."""

    instance_guid: bytes
    exit_id: str
    name: str
    destination_map_id: str
    destination_entry_name: str
    position: tuple[float, float, float]
    picking_range: float
    active: bool
    # Non-negative rows belong to an event set.  Those exits exist in the map
    # database catalog but not in the ordinary scene until that event is
    # activated, so advertising them makes UpdateExitState look up entities
    # which the client never instantiated.
    event_set_row: int = -1

    def contains(
        self,
        position: tuple[float, float, float],
        *,
        margin: float = 0.0,
    ) -> bool:
        radius = max(1.0, float(self.picking_range)) + max(
            0.0, float(margin)
        )
        return math.hypot(
            float(position[0]) - self.position[0],
            float(position[2]) - self.position[2],
        ) <= radius


@dataclass(frozen=True)
class MapCoordinateFrame:
    """Convert absolute map-world positions to quantized MoveCommand space.

    NewActor commands, skill execution context, collision, and navigation use
    absolute map coordinates. MoveCommand subtracts the center stored in the
    map's IPAM header before quantizing its three position components.
    """

    origin: tuple[float, float, float]

    def world_to_wire(
        self, position: tuple[float, float, float]
    ) -> tuple[float, float, float]:
        return tuple(
            float(position[axis]) - self.origin[axis] for axis in range(3)
        )

    def wire_to_world(
        self, position: tuple[float, float, float]
    ) -> tuple[float, float, float]:
        return tuple(
            float(position[axis]) + self.origin[axis] for axis in range(3)
        )


def map_database_path(static_database_path: str | Path, map_id: str) -> Path:
    static_database_path = Path(static_database_path)
    return static_database_path.parent.parent / "maps" / f"{map_id}.db4"


@lru_cache(maxsize=None)
def _active_client_map_database_path(map_id: str) -> Path | None:
    """Return a map DB only when the running client's TOC selects that file.

    Answered once per map: map entries looked it up thousands of times, and
    each lookup probed the client's download cache on disk.
    """

    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    map_name = str(map_id)
    if Path(map_name).name != map_name or "/" in map_name or "\\" in map_name:
        return None
    maps_dir = (
        Path(local_app_data)
        / "Temp" / "DSOClient" / "dlcache" / "export_win32" / "maps"
    )
    toc = maps_dir / "__toc"
    database = maps_dir / f"{map_name}.db4"
    if not toc.is_file() or not database.is_file():
        return None
    expected = f"export_win32/maps/{map_name}.db4|f|"
    try:
        if any(line.startswith(expected) for line in toc.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()):
            return database
    except OSError:
        return None
    return None


@lru_cache(maxsize=128)
def _active_client_instance_guids(
    map_id: str,
    table_name: str,
    identity_columns: tuple[str, ...],
    server_path: str,
    server_mtime_ns: int,
    client_path: str,
    client_mtime_ns: int,
) -> tuple[tuple[int, bytes], ...]:
    """Map instance row IDs to active-client GUIDs when authored data agrees.

    Client builds regenerate instance GUIDs. Use a client GUID only when the
    same row still has the same authored identity, template, transform and
    event set on both sides; otherwise retain the server's authored record.
    """

    del server_mtime_ns, client_mtime_ns  # They participate in the cache key.
    if (not table_name.startswith("_Instance_")
            or not table_name.replace("_", "").isalnum()
            or any(not column.replace("_", "").isalnum()
                   for column in identity_columns)):
        return ()
    server = _open_read_only(Path(server_path))
    try:
        client = _open_read_only(Path(client_path))
        quoted_identity = ", ".join(f'"{column}"' for column in identity_columns)
        server_rows = {
            int(row[0]): row[1:]
            for row in server.execute(
                f'SELECT rowid, {quoted_identity} FROM "{table_name}"'
            )
        }
        client_rows = {
            int(row[0]): row[1:]
            for row in client.execute(
                f'SELECT rowid, "Guid", {quoted_identity} '
                f'FROM "{table_name}"'
            )
        }
        result = []
        for row_id, authored in server_rows.items():
            runtime = client_rows.get(row_id)
            if runtime is None:
                continue
            guid, *runtime_identity = runtime
            if tuple(authored) == tuple(runtime_identity) and len(guid or b"") == 16:
                result.append((row_id, bytes(guid)))
        return tuple(result)
    except sqlite3.Error:
        return ()
    finally:
        server.close()
        if "client" in locals():
            client.close()


def active_client_instance_guid_overrides(
    static_database_path: str | Path,
    map_id: str,
    table_name: str,
    identity_columns: tuple[str, ...],
) -> dict[int, bytes]:
    client_path = _active_client_map_database_path(map_id)
    if client_path is None:
        return {}
    server_path = map_database_path(static_database_path, map_id)
    if not server_path.is_file():
        return {}
    try:
        server_stat = server_path.stat()
        client_stat = client_path.stat()
    except OSError:
        return {}
    return dict(_active_client_instance_guids(
        str(map_id), table_name, tuple(identity_columns),
        str(server_path.resolve()),
        server_stat.st_mtime_ns,
        str(client_path.resolve()),
        client_stat.st_mtime_ns,
    ))


def load_map_coordinate_frame(
    static_database_path: str | Path, map_id: str
) -> MapCoordinateFrame:
    """Read the map-space origin used by the native MoveCommand quantizer."""

    static_database_path = Path(static_database_path)
    path = static_database_path.parent.parent / "maps" / f"{map_id}.map"
    data = resident_content.read_bytes(path)[:44]
    if len(data) < 44 or data[:4] != b"MOSD" or data[8:12] != b"IPAM":
        raise ValueError(f"map {map_id!r} has an invalid IPAM header: {path}")
    # The IPAM transform starts with scale/rotation, then its float4 world
    # translation at +0x1c. MoveCommand's native encoder subtracts precisely
    # this position before its 1/128-unit signed quantization.
    return MapCoordinateFrame(tuple(struct.unpack_from("<3f", data, 0x1C)))


def _open_read_only(path: Path) -> sqlite3.Connection:
    if not resident_content.is_file(path):
        raise FileNotFoundError(f"map database not found: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def active_local_map_event_flags(
    map_id: str,
    active_event_flags: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Combine scheduled game events with permanent local-world unlocks."""

    return frozenset({
        *(str(value).casefold() for value in active_event_flags),
        *BASELINE_MAP_EVENT_FLAGS.get(str(map_id), frozenset()),
    })


def _event_set_declarations(declarations: str) -> list[tuple[str, str]]:
    return [tuple(part.strip().casefold() for part in value.split("=", 1))
            for value in str(declarations or "").split(";") if "=" in value]


def event_set_required_flags(declarations: str) -> frozenset[str]:
    """The flag names an EventSet row is waiting for.

    ``testEvent2=True`` means the row appears once that flag is active;
    ``...=False`` rows are hidden content and declare nothing to wait for.
    """

    return frozenset(
        name
        for name, value in _event_set_declarations(declarations)
        if name and value == "true"
    )


def effective_event_set_declarations(
    map_id: str, row_id: int, declarations: str,
) -> str:
    """Apply server-owned map behavior without editing the extracted map DB.

    The tutorial cultist (Nefertari's staff-theft scene) is authored behind
    ``a0001_start_dun_undead_mage_01``. The retail map marks its EventSet as
    active while that event is *not* running, which places the actor in the
    encounter before the high-hall trigger. On the server it must be gated by
    the positive event flag so the authored scene starts the actor exactly
    when the player enters the high hall.
    """

    if (
        str(map_id).casefold() != TUTORIAL_DUNGEON_MAP_ID
        or int(row_id) != 2
    ):
        return str(declarations or "")
    pairs = _event_set_declarations(declarations)
    if any(
        name == TUTORIAL_CULTIST_EVENT_FLAG and value == "false"
        for name, value in pairs
    ):
        return f"{TUTORIAL_CULTIST_EVENT_FLAG}=True;"
    return str(declarations or "")


def event_set_enabled(declarations: str, active_flags) -> bool:
    """Positive EventFlags are alternative activation influences.

    Client EventSystem::CheckEventInfluence (0x14097fe14) handles each
    event independently; Grima and the workbench list alternative unlocks.
    Negative declarations continue to hide content while their event runs.
    """
    pairs = _event_set_declarations(declarations)
    required = {name for name, value in pairs if name and value == "true"}
    forbidden = {name for name, value in pairs if name and value != "true"}
    return (not required or bool(required & set(active_flags))) and not (forbidden & set(active_flags))


def _parse_event_set_rows(
    rows, map_id: str,
) -> tuple[tuple[int, frozenset[str], frozenset[str]], ...]:
    parsed = []
    for row_id, declarations in rows:
        pairs = _event_set_declarations(
            effective_event_set_declarations(map_id, int(row_id), declarations)
        )
        parsed.append((
            int(row_id),
            frozenset(name for name, value in pairs if name and value == "true"),
            frozenset(name for name, value in pairs if name and value != "true"),
        ))
    return tuple(parsed)


@lru_cache(maxsize=512)
def _cached_event_set_rows(
    path: str, map_id: str,
) -> tuple[tuple[int, frozenset[str], frozenset[str]], ...]:
    """``(row, required, forbidden)`` for every ``_Instance_EventSets`` row.

    Each map database carries the whole ~2,000-row EventSets table. One map
    entry re-read and re-parsed it 20+ times under the session's mutation
    gate (about 0.4 s warm, the bulk of an ingress stall). Cached per file
    like the monster encounter cache: map data is swapped with the server
    stopped.
    """

    database = _open_read_only(Path(path))
    try:
        return _parse_event_set_rows(
            database.execute("SELECT Id, EventFlags FROM _Instance_EventSets"),
            map_id,
        )
    finally:
        database.close()


def _event_set_rows_for(
    database: sqlite3.Connection, map_id: str,
) -> tuple[tuple[int, frozenset[str], frozenset[str]], ...]:
    path = resident_content.database_path(database)
    if not path:
        # An in-memory database (tests) has no file identity to cache by.
        return _parse_event_set_rows(
            database.execute("SELECT Id, EventFlags FROM _Instance_EventSets"),
            map_id,
        )
    return _cached_event_set_rows(path, map_id)


def load_local_event_set_flags(
    static_database_path: str | Path,
    map_id: str,
) -> dict[int, frozenset[str]]:
    """Map each ``_Instance_EventSets`` row to the flags that reveal it."""

    path = map_database_path(static_database_path, map_id)
    return {
        row_id: required
        for row_id, required, _forbidden in _cached_event_set_rows(str(path), map_id)
    }


def _enabled_event_set_ids(
    database: sqlite3.Connection,
    active_event_flags: frozenset[str],
    *,
    map_id: str | None = None,
) -> frozenset[int]:
    active_flags = frozenset(
        str(value).casefold() for value in active_event_flags
    )
    # Same rule as event_set_enabled: positive flags are alternatives,
    # negative ones hide the row while their event runs.
    return frozenset(
        row_id
        for row_id, required, forbidden in _event_set_rows_for(database, map_id or "")
        if (not required or required & active_flags) and not (forbidden & active_flags)
    )


def _transform(value: bytes) -> tuple[tuple[float, float, float], float]:
    if len(value) != 64:
        raise ValueError(f"map transform has {len(value)} bytes, expected 64")
    matrix = struct.unpack("<16f", value)
    position = (matrix[12], matrix[13], matrix[14])
    rotation = math.atan2(matrix[8], matrix[0])
    return position, rotation


def load_local_npc_definitions(
    static_database_path: str | Path,
    map_id: str,
    *,
    npc_count: int | None = None,
    include_inactive_event_sets: bool = True,
    active_event_flags: frozenset[str] = frozenset(),
) -> tuple[LocalNPCDefinition, ...]:
    """Load deterministic actor/shop/placement metadata for a whole map."""

    path = map_database_path(static_database_path, map_id)
    database = _open_read_only(path)
    try:
        query = """
            SELECT rowid, Guid, _ID, Id, ShopId, Profession, Graphics,
                   ActorShapeId, CharacterSet, EventId, Transform,
                   EventSetRow
            FROM _Instance_NPC
            ORDER BY rowid
        """
        parameters: tuple[int, ...] = ()
        if npc_count is not None:
            query += " LIMIT ?"
            parameters = (int(npc_count),)
        enabled_event_sets = _enabled_event_set_ids(
            database,
            active_local_map_event_flags(map_id, active_event_flags),
            map_id=map_id,
        )
        if os.getenv("DSO_NPC_TRACE") == "1":
            print(
                f"[npc-trace] {map_id} include_inactive={include_inactive_event_sets} "
                f"enabled_sets={sorted(enabled_event_sets)} "
                f"flags={sorted(active_local_map_event_flags(map_id, active_event_flags))}"
            )
        # Hiding the NPC is enough to disable the whole debug vendor: the
        # client never sees it, and its shop cannot resolve without it.
        vendor_enabled = debug_vendor_enabled()
        client_guid_overrides = active_client_instance_guid_overrides(
            static_database_path,
            str(map_id),
            "_Instance_NPC",
            ("_ID", "Id", "Transform", "EventSetRow"),
        )
        if client_guid_overrides and os.getenv("DSO_NPC_TRACE") == "1":
            print(
                f"[npc-trace] {map_id} using {len(client_guid_overrides)} "
                "TOC-selected client NPC scene GUIDs"
            )
        definitions = []
        for row in database.execute(query, parameters):
            # Matched on the instance id: the vendor shares its template with
            # the merchant it was cloned from, so matching the template would
            # hide that one too.
            if not vendor_enabled and str(row[2] or "") in DEBUG_VENDOR_NPC_IDS:
                continue
            event_set_row = int(
                row[11] if row[11] is not None else -1
            )
            if (
                event_set_row >= 0
                and not include_inactive_event_sets
                and event_set_row not in enabled_event_sets
            ):
                if os.getenv("DSO_NPC_TRACE") == "1":
                    print(
                        "[npc-trace]   row "
                        f"{row[0]} '{str(row[2] or '')}' "
                        f"set {event_set_row} DROPPED"
                    )
                continue
            position, rotation = _transform(bytes(row[10]))
            if (
                str(map_id).casefold() == "pw206_temple_dun"
                and int(row[0]) == 2
                and str(row[2] or "").casefold()
                == "pw206_quest_lortac_shaman_01"
                and event_set_row == 3910
            ):
                # The Call for Freedom's Melahuac is authored just beyond a
                # blocked Materi barrier. Its Explore trigger is four metres
                # earlier at (94, 3, 64), so place the NPC at that reachable
                # point for the talk objective instead of behind the wall.
                position = (94.0, 3.0, 64.0)
            if (
                str(map_id).casefold() == "f4100_hub"
                and str(row[2] or "").casefold()
                == "f4100_quest_guard_captain"
            ):
                # Commander Slavkoff is authored slightly below the rendered
                # ground in the Myrdosch hub. Keep his X/Z placement but lift
                # the NPC root onto the closest collision surface so the
                # client does not clip him into the platform.
                terrain = load_map_collision_terrain(
                    path.with_suffix(".map"), path.with_suffix(".col")
                )
                ground_heights = terrain.heights_at(
                    position,
                    minimum_height=position[1] - 0.25,
                    maximum_height=position[1] + 0.25,
                )
                if ground_heights:
                    ground_y = min(
                        ground_heights,
                        key=lambda height: abs(height - position[1]),
                    )
                    if abs(ground_y - position[1]) >= 0.05:
                        position = (position[0], ground_y, position[2])
            definitions.append(
                LocalNPCDefinition(
                    # Keep active actor ids stable across event rotations.
                    actor_id=npc_actor_id_for_row(int(row[0])),
                    row_id=int(row[0]),
                    instance_guid=client_guid_overrides.get(
                        int(row[0]), bytes(row[1])
                    ),
                    instance_id=str(row[2] or ""),
                    template_id=str(row[3] or ""),
                    shop_id=str(row[4] or "").strip(),
                    profession=str(row[5] or ""),
                    graphics=str(row[6] or ""),
                    actor_shape_id=str(row[7] or ""),
                    character_set=str(row[8] or ""),
                    event_id=str(row[9] or ""),
                    position=position,
                    rotation=rotation,
                    event_set_row=event_set_row,
                )
            )
        if os.getenv("DSO_NPC_TRACE") == "1":
            print(
                "[npc-trace]   SENT "
                + ", ".join(
                    f"row{d.row_id}/actor{d.actor_id}/{d.instance_id}"
                    f"[{d.profession or '-'}="
                    f"{NPC_CATEGORY_BY_PROFESSION.get(d.profession.casefold(), 0xFF):#04x}]"
                    for d in definitions
                )
            )
        return tuple(definitions)
    finally:
        database.close()


def load_local_npc_definition(
    static_database_path: str | Path,
    map_id: str,
    actor_id: int,
    *,
    include_inactive_event_sets: bool = True,
    active_event_flags: frozenset[str] = frozenset(),
) -> LocalNPCDefinition | None:
    """Resolve a deterministic local actor to its complete map NPC row."""

    definitions = load_local_npc_definitions(
        static_database_path,
        map_id,
        include_inactive_event_sets=include_inactive_event_sets,
        active_event_flags=active_event_flags,
    )
    requested_actor_id = int(actor_id)
    return next(
        (
            definition
            for definition in definitions
            if requested_actor_id in {
                int(definition.actor_id),
                local_npc_actor_id(definition),
            }
        ),
        None,
    )


def local_npc_actor_id(definition: LocalNPCDefinition) -> int:
    """Return the row-ordered actor id shared by all map NPC professions."""

    # The client and the working DRASA path use one generation-1 actor
    # sequence for every NPC, including arena merchants. Moving those
    # merchants to generation 2 changes the low-word slot the client uses for
    # prop lookup and lets later props overwrite NPC/prop registrations.
    return int(definition.actor_id)


def local_prop_actor_base(
    static_database_path: str | Path, map_id: str,
) -> int:
    """Choose the first actor id after every NPC authored for this map.

    NPCs and props are announced together in ActorsEnterVicinity and share the
    client's actor-id space. Reserve event-gated NPC rows too, because those
    actors may be created later by a map event.
    """

    definitions = load_local_npc_definitions(
        static_database_path,
        map_id,
        include_inactive_event_sets=True,
    )
    next_slot = max(
        LOCAL_PLAYER_CLIENT_ACTOR_ID & 0xFFFF,
        max(
            (
                local_npc_actor_id(definition) & 0xFFFF
                for definition in definitions
            ),
            default=-1,
        ),
    ) + 1
    if next_slot >= 0x10000:
        raise ValueError(f"map {map_id!r} has no free prop actor slots")
    return LOCAL_PROP_ID_BASE + next_slot


def local_npc_info(
    definition: LocalNPCDefinition,
    *,
    actor_id: int | None = None,
) -> NPCInfo:
    """Build the client-facing NPC role descriptor for a map NPC.

    Retail identifies bankers with the otherwise empty NPCInfo category 17.
    Ordinary merchants continue to identify their static shop/template in the
    string field and request their offers separately through command 0x0059.
    """

    profession = definition.profession.casefold()
    is_banker = profession == "banker"
    category = NPC_CATEGORY_BY_PROFESSION.get(profession, 0xFF)
    return NPCInfo(
        # NPCInfo's first string is the map row's ShopId. Profession travels
        # separately in category; substituting it here makes the client look
        # up a nonexistent shop template for quest/event NPCs.
        template_id=("" if is_banker else definition.shop_id or ""),
        interactions=(),
        flags=(),
        string_sets=(),
        links=(),
        enabled=True,
        category=category,
        actor_id=(
            local_npc_actor_id(definition)
            if actor_id is None
            else int(actor_id)
        ),
    )


def load_local_crafting_props(
    static_database_path: str | Path,
    map_id: str,
) -> PropInfo:
    """Create every active database-backed crafting workbench on a map.

    These objects are not NPCs.  Retail publishes them through PropInfo before
    PlayerReady; without that command the scene model exists but has no
    network prop and its client-local ``Open*CraftingWindow`` operation never
    fires.
    """

    path = map_database_path(static_database_path, map_id)
    prop_actor_base = local_prop_actor_base(static_database_path, map_id)
    client_guid_overrides = active_client_instance_guid_overrides(
        static_database_path,
        str(map_id),
        "_Instance_StateObject",
        (
            "_ID", "Id", "Transform", "EventSetRow", "InteractOperation",
            "OperationId", "State",
        ),
    )
    database = _open_read_only(path)
    try:
        entries = []
        rows = database.execute(
            """
            SELECT rowid, Guid, _ID, Transform
            FROM _Instance_StateObject
            WHERE State='active'
              AND InteractOperation<>''
              AND (
                    InteractOperation LIKE 'OpenCraftingWindow%'
                 OR InteractOperation LIKE 'OpenFilterCraftingWindow:%'
              )
            ORDER BY rowid
            """
        )
        for row in rows:
            position, rotation = _transform(bytes(row[3]))
            entries.append(
                PropInfoEntry(
                    # Retail preserves the StateObject scene index. Filtering
                    # the query and then enumerating it made every workbench
                    # point at a different local object.
                    prop_id=prop_actor_base + int(row[0]) - 1,
                    template_id=str(row[2] or ""),
                    instance_guid=client_guid_overrides.get(
                        int(row[0]), bytes(row[1])
                    ),
                    field_20=0xFFFFFFFF,
                    field_24=1,
                    field_28=0,
                    # PropInfo carries the absolute StateObject transform.
                    # Subtracting the IPAM origin displaced the interactive
                    # prop from the visible scene workbench.
                    position=position,
                    rotation=rotation,
                    field_48="",
                    field_50="",
                    field_58=0,
                    field_5c=0xFF,
                    field_60=0xFFFFFFFF,
                )
            )
        return PropInfo(tuple(entries))
    finally:
        database.close()


def load_local_map_exits(
    static_database_path: str | Path,
    map_id: str,
    *,
    include_inactive_event_sets: bool = False,
    active_event_flags: frozenset[str] = frozenset(),
) -> tuple[LocalMapExit, ...]:
    """Load exits instantiated by the currently supported base-map scene.

    ``_Instance_InteractExit`` also contains event-only rows.  The server does
    not advertise inactive rows because doing so makes
    ``ClientGameWorld::UpdateExitState()`` look up a scene entity which was
    never activated. Active event flags are matched against the map's
    ``_Instance_EventSets.EventFlags`` declarations.
    """

    path = map_database_path(static_database_path, map_id)
    database = _open_read_only(path)
    try:
        exits = []
        enabled_event_sets = _enabled_event_set_ids(
            database,
            active_local_map_event_flags(map_id, active_event_flags),
            map_id=map_id,
        )
        for row in database.execute(
            """
            SELECT Guid, Id, Name, ExitURL, EntryName, Transform,
                   PickingRange, State, EventSetRow
            FROM _Instance_InteractExit
            ORDER BY rowid
            """
        ):
            event_set_row = int(row[8] if row[8] is not None else -1)
            if (
                event_set_row >= 0
                and not include_inactive_event_sets
                and event_set_row not in enabled_event_sets
            ):
                continue
            position, _rotation = _transform(bytes(row[5]))
            destination_map_id = str(row[3] or "").strip()
            destination_entry_name = str(row[4] or "").strip()
            if (
                str(map_id).casefold() == "f4102_dun"
                and str(row[1] or "").casefold() == "exit_key_lock"
                and destination_map_id.casefold() == "f4201_pve"
                and "f4102_train_repair" in active_local_map_event_flags(
                    map_id, active_event_flags
                )
            ):
                # Completing the railroad repair quest switches the authored
                # locked portal to the usable Yildirimdbi route.
                row_active = True
            else:
                row_active = str(row[7] or "").casefold() == "active"
            exits.append(
                LocalMapExit(
                    instance_guid=bytes(row[0]),
                    exit_id=str(row[1] or ""),
                    name=str(row[2] or row[1] or ""),
                    destination_map_id=destination_map_id,
                    destination_entry_name=destination_entry_name,
                    position=position,
                    picking_range=max(1.0, float(row[6] or 0.0)),
                    active=(row_active and (
                        event_set_row < 0 or event_set_row in enabled_event_sets
                    )),
                    event_set_row=event_set_row,
                )
            )
        return tuple(exits)
    finally:
        database.close()


_TIERED_MAP_EXIT_TARGET_GROUPS = {
    # These maps author a separate scene exit for every access difficulty.
    # Crypt of Kings has both a return gate and a boss gate.
    "g0001_beach_01_dun": ("g0002_jungle_01_dun",),
    "g0002_jungle_01_dun": ("g0001_beach_01_dun",),
    "f4202_dun": ("f4203_boss", "f4203_boss_scale"),
    "a0202_kingscrypt01": ("a0203_kingscrypt02",),
    "a0203_kingscrypt02": ("a0202_kingscrypt01", "a0204_boss"),
    # Duria's cult-tower route also authors one scene exit per difficulty.
    "a0402_culttower01": ("a0403_culttower02",),
    "a0403_culttower02": ("a0402_culttower01", "a0404_culttower03"),
    # Tegan's Wall, Tegansruh, and the dragon lair have separate exits for
    # each access difficulty, including the return route to Tegan's Refuge.
    "a0505_dun": ("a0600_dragonfortress",),
    "a0600_dragonfortress": (
        "a0505_dun", "a0601_defiledsanctum", "a0602_dragonslair01",
    ),
    "a0601_defiledsanctum": ("a0600_dragonfortress",),
    "a0602_dragonslair01": (
        "a0600_dragonfortress", "a0603_dragonslair02",
    ),
    "a0603_dragonslair02": (
        "a0602_dragonslair01", "a0604_heraldsslair",
    ),
    # Eternal Watch and Hjalgrimur author matching exits for every available
    # difficulty; select the matching scene gate in both directions.
    "b1202_dun": ("b1203_boss",),
    "b1203_boss": ("b1202_dun",),
    # Temple Region and Lost Temple author parallel exits for every tier.
    "c2301_dun": ("c2310_boss",),
    "c2310_boss": ("c2301_dun",),
    # Time Rift and Atlantis Above Water also author matching exits per tier.
    "c2302_dun": ("c2303_dun",),
    "c2303_dun": ("c2302_dun", "c2304_boss"),
    # The Spiky Valley treasure-cave gate has one overlapping scene exit per
    # difficulty. Only expose the exit matching the current Valley access.
    "h0006_valley_dun": ("h0007_treasure_cave_dun",),
    # The cave authors matching difficulty variations for its return gate and
    # for its internal routes and the quest gate to Djinn's Room.
    "h0007_treasure_cave_dun": (
        "h0006_valley_dun", "h0007_treasure_cave_dun", "h0008_boss",
    ),
    # Djinn's Room also has one overlapping return-gate variation per tier.
    "h0008_boss": ("h0007_treasure_cave_dun",),
    "pw001_grimmagstone_01_dun": ("pw002_alliance_fortress_01_dun",),
    "pw002_alliance_fortress_01_dun": ("pw003_grimmag_01_boss",),
    "pw004_hagastove_grotto_01_dun": ("pw005_caverns_loxley_01_dun",),
    "pw005_caverns_loxley_01_dun": ("pw006_arachna_01_boss",),
    "pw007_stonekeep_01_dun": ("pw008_kingscrypt_01_dun",),
    "pw008_kingscrypt_01_dun": ("pw009_heredur_01_boss",),
    "pw010_wildcave_01_dun": ("pw011_eternal_grove_01_dun",),
    "pw011_eternal_grove_01_dun": ("pw012_bearach_01_boss",),
    "pw013_wizard_garden_01_dun": ("pw014_cult_tower_01_dun",),
    "pw014_cult_tower_01_dun": ("pw015_khalys_boss_01",),
    "pw016_teganswall_01_dun": ("pw017_dragonslair_01_dun",),
    "pw017_dragonslair_01_dun": ("pw018_herold_01_boss",),
    "pw019_hailstone_mountains_01_dun": ("pw020_eternal_watch_01_dun",),
    "pw020_eternal_watch_01_dun": ("pw021_sigrismarr_01_boss",),
    "pw022_mount_suvius_01_dun": ("pw023_atlantis_above_water_01_dun",),
    "pw023_atlantis_above_water_01_dun": ("pw024_medusa_01_boss",),
    "pw025_ship_graveyard_01_dun": ("pw026_heart_of_atlantis_01_dun",),
    "pw026_heart_of_atlantis_01_dun": ("pw027_gorga_01_boss",),
    # Cardhun's Suburb authors a separate exit at the same scene gate for
    # every Parallel World destination difficulty. Keep only the tier that
    # matches the Suburb access selected by this player.
    "pw200_cardhun_dun": (
        "pw201_library_dun",
        "pw203_norseland_01_dun",
        "pw203_norseland_02_dun",
        "pw205_helios_dun",
        "pw206_temple_02_dun",
        "pw206_temple_dun",
        "pw207_village_02_dun",
        "pw207_village_dun",
    ),
    # Parallel World maps reached from Cardhun author one overlapping return
    # exit per Cardhun access difficulty. Select the matching Suburb gate on
    # entry so the native client can instantiate a single return portal.
    "pw201_library_dun": ("pw200_cardhun_dun",),
    "pw203_norseland_01_dun": ("pw200_cardhun_dun",),
    "pw203_norseland_02_dun": ("pw200_cardhun_dun",),
    "pw205_helios_dun": ("pw200_cardhun_dun",),
    "pw206_temple_dun": ("pw200_cardhun_dun",),
    "pw206_temple_02_dun": ("pw200_cardhun_dun",),
    # Fyeborough authors both a tiered Cardhun return gate and a Tywyll Dail
    # gate for each difficulty at shared scene positions. Select both routes
    # using the current Fyeborough access tier.
    "pw207_village_dun": (
        "pw200_cardhun_dun", "pw208_rangervillage_dun",
    ),
    "pw207_village_02_dun": (
        "pw200_cardhun_dun", "pw208_rangervillage_dun",
    ),
    # Tidal Wildwood has matching return exits for each Greyling difficulty.
    "k0003_tidalwildwood": ("k0002_greylingforest",),
}


def _filter_materi_swirl_exits(
    static_database_path, map_id, exits, selected_map_access_id,
    selected_map_difficulty,
):
    """Choose one Lingering Memory and difficulty per authored swirl spot.

    Every spot contains all dungeon/tier variations. Caching all their GUIDs
    in ExitInfo makes the client instantiate overlapping portals when the
    swirl event starts. Keep the selection stable across event refreshes.
    """
    candidates = tuple(
        exit for exit in exits
        if exit.destination_map_id.casefold().startswith("pw202_mini_dun_")
        and "_spot_" in exit.name.casefold()
    )
    if not candidates:
        return exits
    destinations = sorted({exit.destination_map_id for exit in candidates})
    with closing(_open_read_only(Path(static_database_path))) as database:
        source = database.execute(
            "SELECT Difficulty FROM _Template_MapAccessGroup WHERE Id=?",
            (str(selected_map_access_id),),
        ).fetchone()
        difficulty = int(source[0] or 0) if source else int(selected_map_difficulty or 0)
        placeholders = ",".join("?" for _ in destinations)
        metadata = {
            str(access_id): (str(group), int(tier or 0))
            for access_id, group, tier in database.execute(
                "SELECT Id, AccessGroup, Difficulty FROM _Template_MapAccessGroup "
                f"WHERE Id IN ({placeholders})", destinations,
            )
        }
    spots = {}
    for exit in candidates:
        spot = exit.name.casefold().rsplit("_spot_", 1)[1]
        spots.setdefault(spot, []).append(exit)
    selected = set()
    for spot, variations in spots.items():
        eligible = [
            exit for exit in variations
            if metadata.get(exit.destination_map_id, (None, None))[1] == difficulty
        ]
        if not eligible:
            # Preserve a usable portal when an extracted dungeon lacks this
            # tier, without advertising all its other variations.
            eligible = variations
        chosen = min(eligible, key=lambda exit: (
            abs(metadata.get(exit.destination_map_id, (None, 0))[1] - difficulty),
            hashlib.sha256(
                f"{map_id}:{spot}:{exit.destination_map_id}".encode()
            ).digest(),
            exit.name,
        ))
        selected.add(chosen.instance_guid)
    candidate_guids = {exit.instance_guid for exit in candidates}
    return tuple(
        exit for exit in exits
        if exit.instance_guid not in candidate_guids
        or exit.instance_guid in selected
    )


def _tiered_exit_clusters(static_database_path, exits):
    """Find same-position exits that only differ by destination difficulty.

    Map databases repeat an exit for each access tier. Rather than maintaining
    a map-by-map allowlist, identify those variants from the access-group
    catalog and their shared entry/scene position. Distinct gates to the same
    dungeon remain separate because they do not share that scene location.
    """
    destinations = {exit.destination_map_id for exit in exits}
    if not destinations:
        return ()
    placeholders = ",".join("?" for _ in destinations)
    with closing(_open_read_only(static_database_path)) as database:
        rows = database.execute(
            "SELECT Id, AccessGroup, Difficulty FROM _Template_MapAccessGroup "
            f"WHERE Id IN ({placeholders})",
            tuple(destinations),
        )
        metadata = {
            str(access_id): (str(access_group), int(difficulty or 0))
            for access_id, access_group, difficulty in rows
        }

    # Include the canonical difficulty-zero access when a scene exit uses the
    # shortened base map URL instead of its explicit `_00` access ID.
    for destination, (group, difficulty) in tuple(metadata.items()):
        if destination.endswith("_00"):
            metadata.setdefault(destination[:-3], (group, difficulty))

    by_group_and_entry = {}
    for definition in exits:
        group, difficulty = metadata.get(
            definition.destination_map_id, (None, None),
        )
        if group is None:
            continue
        key = (group, definition.destination_entry_name)
        by_group_and_entry.setdefault(key, []).append((definition, difficulty))

    clusters = []
    for variants in by_group_and_entry.values():
        pending = list(variants)
        while pending:
            seed, _ = pending.pop(0)
            cluster = [(seed, metadata[seed.destination_map_id][1])]
            remaining = []
            for candidate, difficulty in pending:
                if all(
                    abs(candidate.position[axis] - seed.position[axis]) <= 1.0
                    for axis in range(3)
                ):
                    cluster.append((candidate, difficulty))
                else:
                    remaining.append((candidate, difficulty))
            pending = remaining
            if len({difficulty for _, difficulty in cluster}) > 1:
                clusters.append(tuple(cluster))
    return tuple(clusters)


def _filter_tiered_exit_clusters(
    static_database_path, exits, selected_difficulty,
):
    """Keep only the selected access tier at each shared authored gate."""
    clusters = _tiered_exit_clusters(static_database_path, exits)
    if not clusters:
        return exits
    selected_guids = set()
    clustered_guids = set()
    for cluster in clusters:
        difficulties = {difficulty for _, difficulty in cluster}
        chosen_difficulty = (
            selected_difficulty
            if selected_difficulty in difficulties
            else min(difficulties, key=lambda value: abs(value - selected_difficulty))
        )
        for definition, difficulty in cluster:
            clustered_guids.add(definition.instance_guid)
            if difficulty == chosen_difficulty:
                selected_guids.add(definition.instance_guid)
    return tuple(
        definition for definition in exits
        if definition.instance_guid not in clustered_guids
        or definition.instance_guid in selected_guids
    )


def filter_parallel_world_exits_for_access(
    static_database_path: str | Path,
    map_id: str,
    exits: tuple[LocalMapExit, ...],
    *,
    selected_map_access_id: str = "",
    selected_map_difficulty: int | None = None,
) -> tuple[LocalMapExit, ...]:
    """Keep only destination variations matching the selected map difficulty."""

    exits = _filter_materi_swirl_exits(
        static_database_path, map_id, exits, selected_map_access_id,
        selected_map_difficulty,
    )
    source_difficulty = None
    if selected_map_access_id:
        with closing(_open_read_only(static_database_path)) as database:
            source = database.execute(
                "SELECT Difficulty FROM _Template_MapAccessGroup WHERE Id=?",
                (str(selected_map_access_id),),
            ).fetchone()
        if source is not None:
            source_difficulty = int(source[0] or 0)
    if source_difficulty is None and selected_map_difficulty is not None:
        source_difficulty = int(selected_map_difficulty)
    if source_difficulty is not None:
        exits = _filter_tiered_exit_clusters(
            static_database_path, exits, source_difficulty,
        )
    target_groups = _TIERED_MAP_EXIT_TARGET_GROUPS.get(
        str(map_id).casefold()
    )
    if target_groups is None:
        return exits
    with closing(_open_read_only(static_database_path)) as database:
        source = database.execute(
            "SELECT Difficulty FROM _Template_MapAccessGroup WHERE Id=?",
            (str(selected_map_access_id),),
        ).fetchone()
        if source is None and selected_map_difficulty is None:
            return exits
        selected_difficulty = (
            int(source[0] or 0) if source is not None
            else int(selected_map_difficulty)
        )
        placeholders = ",".join("?" for _ in target_groups)
        destination_difficulties = {
            str(access_id): int(difficulty or 0)
            for access_id, difficulty in database.execute(
                "SELECT Id, Difficulty FROM _Template_MapAccessGroup "
                f"WHERE AccessGroup IN ({placeholders})", target_groups,
            )
        }
    # The authored difficulty-zero exit uses a `_00` URL, while the access
    # group catalog names that destination without the suffix.
    for target in target_groups:
        if target in destination_difficulties:
            destination_difficulties.setdefault(
                f"{target}_00", destination_difficulties[target],
            )
    # EntityEventController selects a variation by cached Name/GUID, even
    # when ExitInfo.field_10 is zero. Never cache the other tier GUIDs: the
    # client merges subsequent ExitInfo batches instead of replacing them.
    return tuple(
        exit for exit in exits
        if exit.destination_map_id not in destination_difficulties
        or destination_difficulties[exit.destination_map_id] == selected_difficulty
    )


def _exit_info_entries_for_access(
    static_database_path: str | Path,
    map_id: str,
    all_exits: tuple[LocalMapExit, ...],
    selected_exits: tuple[LocalMapExit, ...],
) -> tuple[ExitInfoEntry, ...]:
    """Publish only exits with an entity in the currently loaded scene.

    ExitInfo asks the client to update a named scene entity. Sending every
    difficulty alias makes it look up names for portals that are not in this
    map variation, generating an UpdateExitState warning for each absent
    entity. The selected variation is materialized separately through
    VariationInfo where needed, so only its authored name belongs here.
    """
    del static_database_path, map_id, all_exits
    return tuple(
        ExitInfoEntry(
            definition.name,
            definition.destination_map_id,
            int(definition.active),
            definition.instance_guid,
        )
        for definition in selected_exits
    )


_NATIVE_SCENE_EXIT_TARGETS = {
    # The K-series maps have no authored return-gate entities in their base
    # scenes. Their extracted map DBs include the added gates by GUID.
    "k0002_greylingforest": ("k0001_maincity",),
    "k0003_tidalwildwood": ("k0002_greylingforest",),
    # Hiraja's quest gate must be materialized after its scene event starts.
    "h0001_hub": ("h0006_valley_dun",),
    # Cardhun's authored hub gates are absent from the loaded scene. The
    # PW199 gate shares its quest EventSet with Annagreth Everly; create it
    # only after that EventSet is activated.
    "pw000_hub": ("pw199_cardhun_dun", "pw200_cardhun_dun"),
    # The Cardhun city return gate is also absent from the loaded scene.
    "pw200_cardhun_dun": ("pw000_hub",),
    # Parallel World destinations reached from Cardhun have their return
    # exits in the map database but omit the corresponding gate entity from
    # the loaded scene. Materialize the selected Suburb return gate by GUID.
    "pw201_library_dun": ("pw200_cardhun_dun",),
    "pw203_norseland_01_dun": ("pw200_cardhun_dun",),
    "pw203_norseland_02_dun": ("pw200_cardhun_dun",),
    "pw205_helios_dun": ("pw200_cardhun_dun",),
    "pw206_temple_dun": ("pw200_cardhun_dun",),
    "pw206_temple_02_dun": ("pw200_cardhun_dun",),
    "pw207_village_dun": ("pw200_cardhun_dun",),
    "pw207_village_02_dun": ("pw200_cardhun_dun",),
    # Tywyll Dail's Twisted Woods entrance is present in the exit table but
    # missing from the loaded scene. Create only the selected difficulty gate.
    "pw208_rangervillage_dun": ("pw208_lost_woods_dun_01",),
    # The Frog River Delta gate is authored in the exit table but missing
    # from the loaded Stillwater Bay scene; create the selected tier entity.
    "g0001_beach_01_dun": ("g0002_jungle_01_dun",),
    # The return gate is also absent from the Frog River Delta scene.
    "g0002_jungle_01_dun": ("g0001_beach_01_dun",),
    # The Temple of Ice gate is authored in the exit table but missing from
    # the loaded Cradle of Frost scene; create its exit entity by GUID.
    "g0005_temple_ice_01_dun": ("g0005_temple_ice_02_dun",),
    # Railroad repair reveals the authored gate from Piritlidag to Yildirimdbi.
    "f4102_dun": ("f4201_pve",),
    # Yildirimdbi's authored return exit leads back to Piritlidag.
    "f4201_pve": ("f4102_dun",),
    # The boss teleportarium exits are authored but missing from the scene.
    "f4202_dun": ("f4203_boss",),
    "a0202_kingscrypt01": ("a0203_kingscrypt02",),
    "a0203_kingscrypt02": ("a0202_kingscrypt01", "a0204_boss"),
    "a0402_culttower01": ("a0403_culttower02",),
    "a0403_culttower02": ("a0402_culttower01", "a0404_culttower03"),
    "a0505_dun": ("a0600_dragonfortress",),
    "a0600_dragonfortress": (
        "a0505_dun",
        "a0601_defiledsanctum",
        "a0602_dragonslair01",
    ),
    "a0601_defiledsanctum": ("a0600_dragonfortress",),
    "a0602_dragonslair01": (
        "a0600_dragonfortress",
        "a0603_dragonslair02",
    ),
    "a0603_dragonslair02": (
        "a0602_dragonslair01",
        "a0604_heraldsslair",
    ),
    "b1202_dun": ("b1203_boss",),
    "b1203_boss": ("b1202_dun",),
    "c2301_dun": ("c2310_boss",),
    "c2310_boss": ("c2301_dun",),
    "c2302_dun": ("c2303_dun",),
    "c2303_dun": ("c2302_dun", "c2304_boss"),
}


def native_scene_exit_variation_entries(
    map_id: str,
    exits: tuple[LocalMapExit, ...],
) -> tuple[VariationInfoEntry, ...]:
    """Build client entities for active exits absent from the loaded scene."""

    targets = _NATIVE_SCENE_EXIT_TARGETS.get(str(map_id).casefold(), ())
    return tuple(
        VariationInfoEntry(
            definition.instance_guid,
            definition.event_set_row
            if str(map_id).casefold() in {"h0001_hub", "pw000_hub"}
            else -1,
        )
        for definition in exits
        if (
            definition.active
            and any(
                definition.destination_map_id.casefold().startswith(target)
                for target in targets
            )
            # Piritlidag's extracted map contains repeated generic exits at
            # this position. The key-lock row is the authored gate entity;
            # materialize only that GUID to avoid stacking duplicate portals.
            and (
                str(map_id).casefold() != "f4102_dun"
                or definition.name.casefold() == "f4102_exit_to_f4201"
            )
        )
    )


def load_local_map_initialization(
    static_database_path: str | Path,
    map_id: str,
    *,
    npc_count: int | None = None,
    required_professions: tuple[str, ...] = ("banker",),
    active_event_flags: frozenset[str] = frozenset(),
    include_inactive_event_sets: bool = False,
    selected_map_difficulty: int | None = None,
    selected_map_access_id: str | None = None,
) -> tuple[tuple[ServerCommand, ...], ExitInfo]:
    """Return every map NPC service and all active map exits.

    Actor IDs are session-local.  Template IDs, GUIDs and transforms are read
    from the shared map DB instead of copied from the live-server capture.  A
    legacy capture-derived limit initialized only the first thirteen actors;
    Kingshill has more than one hundred NPC rows, leaving later merchants and
    dialogue providers visible in the scene but without a network actor.  The
    optional limit remains available to diagnostics, while production sends
    the complete local map catalog.
    """

    all_definitions = load_local_npc_definitions(
        static_database_path,
        map_id,
        include_inactive_event_sets=include_inactive_event_sets,
        active_event_flags=active_event_flags,
    )
    if str(map_id).casefold() == "f4100_hub":
        # Slavkoff is the hub's primary quest captain and is late in the large
        # 185-NPC scene catalog. Register him before the decorative NPC batch.
        all_definitions = tuple(sorted(
            all_definitions,
            key=lambda definition: (
                0 if definition.instance_id.casefold()
                == "f4100_quest_guard_captain" else 1,
                definition.row_id,
            ),
        ))
    required = {value.casefold() for value in required_professions}
    definitions = tuple(
        definition
        for index, definition in enumerate(all_definitions)
        if npc_count is None
        or index < int(npc_count)
        or definition.profession.casefold() in required
    )
    path = map_database_path(static_database_path, map_id)
    database = _open_read_only(path)
    try:
        commands = []
        for definition in definitions:
            actor_id = local_npc_actor_id(definition)
            new_npc = NewNPC(
                template_id=definition.template_id,
                instance_guid=definition.instance_guid,
                placement=ActorPlacement(
                    LOCAL_NPC_ACTOR_BASE, actor_id, 1, False, False
                ),
                rotation=definition.rotation,
                position=definition.position,
                enabled=True,
                actor_id=actor_id,
            )
            npc_info = local_npc_info(definition, actor_id=actor_id)
            commands.append(ServerCommand(NEW_NPC_COMMAND, new_npc, actor_id))
            commands.append(ServerCommand(NPC_INFO_COMMAND, npc_info, actor_id))

        all_exit_definitions = load_local_map_exits(
            static_database_path,
            map_id,
            active_event_flags=active_event_flags,
            include_inactive_event_sets=include_inactive_event_sets,
        )
        exit_definitions = filter_parallel_world_exits_for_access(
            static_database_path,
            map_id,
            all_exit_definitions,
            selected_map_access_id=selected_map_access_id or "",
            selected_map_difficulty=selected_map_difficulty,
        )
        exits = _exit_info_entries_for_access(
            static_database_path,
            map_id,
            all_exit_definitions,
            exit_definitions,
        )
        native_exit_entries = native_scene_exit_variation_entries(
            map_id, exit_definitions,
        )
        if native_exit_entries:
            # ExitInfo only updates existing entities. The client handler for
            # VariationInfo (release261_192 0x1401d4ce0) factory-creates missing
            # entities by their map GUID. EventSetRow -1 is unconditional in
            # ClientEventSystem::IsGameObjectActive (0x1401fba74). Event-bound
            # exits retain their row and are sent after EventUpdate by the
            # initialization command builder.
            commands.append(ServerCommand(
                VARIATION_INFO_COMMAND,
                VariationInfo(native_exit_entries),
                0,
            ))
        return tuple(commands), ExitInfo(exits)
    finally:
        database.close()


def load_local_spawn_position(
    static_database_path: str | Path,
    map_id: str,
) -> tuple[float, float, float]:
    """Resolve the player entry transform for a locally hosted map.

    Default initialization and fast travel use the authored Respawnpoint.
    Clicked map exits are intentionally separate: ``load_local_entry_position``
    resolves the destination entry named by that concrete exit.
    """

    path = map_database_path(static_database_path, map_id)
    database = _open_read_only(path)
    try:
        row = database.execute(
            """
            SELECT Transform FROM _Instance_Entry
            ORDER BY CASE
                WHEN _ID='Respawnpoint'
                  OR Id='Respawnpoint'
                  OR Name='Respawnpoint' THEN 0
                WHEN EntryType='Respawn' THEN 1
                ELSE 2
            END, rowid
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            raise ValueError(f"map {map_id} has no player entry transform")
        position, _rotation = _transform(bytes(row[0]))
        return position
    finally:
        database.close()


def load_local_travel_point_position(
    static_database_path: str | Path,
    map_id: str,
) -> tuple[float, float, float]:
    """Resolve the authored travel-stone position, falling back to Respawnpoint.

    World-map and travel-stone destinations should arrive beside the map's
    general travel point. A few older/event maps have no travel-point row, so
    retain their authored player entry as a safe fallback.
    """

    path = map_database_path(static_database_path, map_id)
    database = _open_read_only(path)
    try:
        row = database.execute(
            """
            SELECT Transform FROM _Instance_TravelPoint
            WHERE lower(COALESCE(State, 'active'))='active'
            ORDER BY CASE WHEN lower(COALESCE(Name, ''))='general_travel_point'
                          THEN 0 ELSE 1 END, rowid
            LIMIT 1
            """
        ).fetchone()
        if row is not None:
            position, _rotation = _transform(bytes(row[0]))
            return position
    except sqlite3.Error:
        # Some legacy map databases do not contain _Instance_TravelPoint.
        pass
    finally:
        database.close()
    return load_local_spawn_position(static_database_path, map_id)


def load_local_entry_position(
    static_database_path: str | Path,
    map_id: str,
    entry_name: str,
) -> tuple[float, float, float]:
    """Resolve the destination transform named by an exit's EntryName."""

    normalized_name = str(entry_name or "").strip()
    if not normalized_name:
        return load_local_spawn_position(static_database_path, map_id)

    path = map_database_path(static_database_path, map_id)
    database = _open_read_only(path)
    try:
        row = database.execute(
            """
            SELECT Transform
            FROM _Instance_Entry
            WHERE Name=? OR Id=? OR _ID=?
            ORDER BY CASE
                WHEN Name=? THEN 0
                WHEN Id=? THEN 1
                ELSE 2
            END, rowid
            LIMIT 1
            """,
            (
                normalized_name,
                normalized_name,
                normalized_name,
                normalized_name,
                normalized_name,
            ),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"map {map_id!r} has no entry named {normalized_name!r}"
            )
        position, _rotation = _transform(bytes(row[0]))
        return position
    finally:
        database.close()


@dataclass(frozen=True)
class TeamBattlegroundEntry:
    """One side's two authored positions on a battleground map.

    Arena maps place ``_Instance_Entry`` rows per side, tagged with an
    ``OwnerTeam`` such as ``RedTeam``/``BlueTeam`` and an ``EntryType``:

    * ``Entry`` is a **staging** position off to one end of the map. It is
      where the side lands on map load and waits out the countdown.
    * ``Respawn`` is the position inside the arena proper that the side is
      moved to when a round begins, and returns to between rounds.

    Retail uses both: the recorded 1v1 dropped the player at the team entry
    on arrival, then sent a ``ForceTransformCommand`` to the team respawn in
    the same batch as ``PVPMatchCountdown``. Its two force-transform targets,
    ``(14, 0, 45)`` and ``(35, 0, 71)``, are exactly ``BlueTeamRespawn`` and
    ``RedTeamRespawn`` on ``b1102_dm``.
    """

    team: str
    entry_position: tuple[float, float, float]
    entry_heading: float
    arena_position: tuple[float, float, float]
    arena_heading: float


def load_local_team_entries(
    static_database_path: str | Path,
    map_id: str,
) -> tuple[TeamBattlegroundEntry, ...]:
    """Per-team staging and arena positions, ordered by team name.

    A match indexes the result by team number. Either position falls back to
    the other when a map authors only one of them, and a map with no team
    entries at all yields an empty tuple.
    """

    path = map_database_path(static_database_path, map_id)
    database = _open_read_only(path)
    try:
        rows = database.execute(
            """
            SELECT OwnerTeam, Transform, EntryType
            FROM _Instance_Entry
            WHERE OwnerTeam IS NOT NULL AND TRIM(OwnerTeam) <> ''
            ORDER BY rowid
            """
        ).fetchall()
    except sqlite3.DatabaseError:
        return ()
    finally:
        database.close()
    staging: dict[str, tuple[tuple[float, float, float], float]] = {}
    arena: dict[str, tuple[tuple[float, float, float], float]] = {}
    for owner_team, transform, entry_type in rows:
        team = str(owner_team or "").strip()
        if not team or transform is None:
            continue
        try:
            placement = _transform(bytes(transform))
        except (struct.error, ValueError):
            continue
        kind = str(entry_type or "").strip().casefold()
        if kind == "respawn":
            arena.setdefault(team, placement)
        else:
            staging.setdefault(team, placement)
    return tuple(
        TeamBattlegroundEntry(
            team,
            *(staging.get(team) or arena[team]),
            *(arena.get(team) or staging[team]),
        )
        for team in sorted(set(staging) | set(arena))
    )


def load_local_npc_shop_id(
    static_database_path: str | Path,
    map_id: str,
    actor_id: int,
    *,
    include_inactive_event_sets: bool = True,
    active_event_flags: frozenset[str] = frozenset(),
) -> str | None:
    """Resolve any generated local NPC actor id to its map-database shop."""

    definition = load_local_npc_definition(
        static_database_path,
        map_id,
        actor_id,
        include_inactive_event_sets=include_inactive_event_sets,
        active_event_flags=active_event_flags,
    )
    if definition is None:
        return None
    shop_id = str(definition.shop_id or "").strip()
    return shop_id or None
