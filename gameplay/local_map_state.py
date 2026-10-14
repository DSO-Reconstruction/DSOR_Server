"""Build typed gameplay entity state from the client/server map database."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
import sqlite3
import struct
from pathlib import Path

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
    ExitInfo,
    ExitInfoEntry,
    ServerCommand,
)


LOCAL_NPC_ACTOR_BASE = 0x10000
# Client actor ids are generational (generation in the high word, slot in the
# low word; every retail id is 0x1xxxx).  The local player is sent as actor 1
# and the client keeps it as slot 1 of generation 1, i.e. 0x10001, before any
# map entity arrives.  A NewNPC with that id is refused ("ActorManager::
# RegisterActor(): There is an existing actor with id 65537", then "Could not
# factor new NPC"), which is what hid the tutorial's ranger (row 2) and, in
# Kingshill, the arena merchant.  NPC ids therefore skip that slot.
LOCAL_PLAYER_CLIENT_ACTOR_ID = LOCAL_NPC_ACTOR_BASE + 1
LOCAL_ARENA_MERCHANT_ACTOR_BASE = 0x20000


def npc_actor_id_for_row(row_id: int) -> int:
    """The stable actor id of ``_Instance_NPC`` row ``row_id`` (1-based)."""

    actor_id = LOCAL_NPC_ACTOR_BASE + int(row_id) - 1
    if actor_id >= LOCAL_PLAYER_CLIENT_ACTOR_ID:
        actor_id += 1
    return actor_id
BANKER_NPC_CATEGORY = 17
JEWELLER_NPC_CATEGORY = 1
PVP_MERCHANT_NPC_CATEGORY = 14
NPC_CATEGORY_BY_PROFESSION = {
    "alchemist": 0,
    "jewelry": JEWELLER_NPC_CATEGORY,
    "jeweller": JEWELLER_NPC_CATEGORY,
    "jeweler": JEWELLER_NPC_CATEGORY,
    "smith": 4,
    "priest": 7,
    "monger": 8,
    "arena_merchant": PVP_MERCHANT_NPC_CATEGORY,
    "equip": 15,
    "crafting": 16,
    "banker": BANKER_NPC_CATEGORY,
    "mount_trader": 20,
}
LOCAL_PROP_ID_BASE = 0x10000
KINGSCITY_MAP_ID = "a0200_kingscity"
PARALLEL_WORLDS_HUB_MAP_ID = "pw000_hub"
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
    # The Parallel Worlds hub authors each portal behind a permanent,
    # map-local EventSet.  Enable only Grimmagstone for the local baseline;
    # the other Parallel Worlds portals remain gated until their own unlock
    # flags are explicitly supplied.
    PARALLEL_WORLDS_HUB_MAP_ID: frozenset({
        "pw000_unlock_pw001_exit",
    }),
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


def load_map_coordinate_frame(
    static_database_path: str | Path, map_id: str
) -> MapCoordinateFrame:
    """Read the map-space origin used by the native MoveCommand quantizer."""

    static_database_path = Path(static_database_path)
    path = static_database_path.parent.parent / "maps" / f"{map_id}.map"
    data = path.read_bytes()[:44]
    if len(data) < 44 or data[:4] != b"MOSD" or data[8:12] != b"IPAM":
        raise ValueError(f"map {map_id!r} has an invalid IPAM header: {path}")
    # The IPAM transform starts with scale/rotation, then its float4 world
    # translation at +0x1c. MoveCommand's native encoder subtracts precisely
    # this position before its 1/128-unit signed quantization.
    return MapCoordinateFrame(tuple(struct.unpack_from("<3f", data, 0x1C)))


def _open_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
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


def event_set_enabled(declarations: str, active_flags) -> bool:
    """Positive EventFlags are alternative activation influences.

    Client EventSystem::CheckEventInfluence (0x14097fe14) handles each
    event independently; Grima and the workbench list alternative unlocks.
    Negative declarations continue to hide content while their event runs.
    """
    pairs = [tuple(part.strip().casefold() for part in value.split("=", 1))
             for value in declarations.split(";") if "=" in value]
    required = {name for name, value in pairs if name and value == "true"}
    forbidden = {name for name, value in pairs if name and value != "true"}
    return (not required or bool(required & set(active_flags))) and not (forbidden & set(active_flags))


def _enabled_event_set_ids(
    database: sqlite3.Connection,
    active_event_flags: frozenset[str],
) -> frozenset[int]:
    active_flags = {
        str(value).casefold() for value in active_event_flags
    }
    return frozenset(
        int(row[0])
        for row in database.execute(
            "SELECT Id, EventFlags FROM _Instance_EventSets"
        )
        if event_set_enabled(str(row[1] or ""), active_flags)
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
        )
        # Hiding the NPC is enough to disable the whole debug vendor: the
        # client never sees it, and its shop cannot resolve without it.
        vendor_enabled = debug_vendor_enabled()
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
                continue
            position, rotation = _transform(bytes(row[10]))
            definitions.append(
                LocalNPCDefinition(
                    # Keep active actor ids stable across event rotations.
                    actor_id=npc_actor_id_for_row(int(row[0])),
                    row_id=int(row[0]),
                    instance_guid=bytes(row[1]),
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
    """Return the collision-free session actor assigned to one map NPC."""

    if definition.profession.casefold() == "arena_merchant":
        return LOCAL_ARENA_MERCHANT_ACTOR_BASE + int(definition.row_id)
    return int(definition.actor_id)


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
        template_id=(
            "" if is_banker else definition.shop_id or definition.profession
        ),
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
                    prop_id=LOCAL_PROP_ID_BASE + int(row[0]) - 1,
                    template_id=str(row[2] or ""),
                    instance_guid=bytes(row[1]),
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
            exits.append(
                LocalMapExit(
                    instance_guid=bytes(row[0]),
                    exit_id=str(row[1] or ""),
                    name=str(row[2] or row[1] or ""),
                    destination_map_id=str(row[3] or "").strip(),
                    destination_entry_name=str(row[4] or "").strip(),
                    position=position,
                    picking_range=max(1.0, float(row[6] or 0.0)),
                    active=str(row[7] or "").casefold() == "active",
                    event_set_row=event_set_row,
                )
            )
        return tuple(exits)
    finally:
        database.close()


def load_local_map_initialization(
    static_database_path: str | Path,
    map_id: str,
    *,
    npc_count: int | None = None,
    required_professions: tuple[str, ...] = ("banker",),
    active_event_flags: frozenset[str] = frozenset(),
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
        include_inactive_event_sets=False,
        active_event_flags=active_event_flags,
    )
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

        exits = []
        for definition in load_local_map_exits(
            static_database_path,
            map_id,
            active_event_flags=active_event_flags,
        ):
            exits.append(
                ExitInfoEntry(
                    # ExitInfoCommand's client handler passes this string to
                    # ClientGameWorld::UpdateExitState(), which looks up the
                    # concrete scene entity by Name. InteractExit.Id is often
                    # the shared template "exit_general_01" and belongs only
                    # in the later client UnlockMapCommand.
                    definition.name,
                    definition.destination_map_id,
                    int(definition.active),
                    definition.instance_guid,
                )
            )
        return tuple(commands), ExitInfo(tuple(exits))
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
