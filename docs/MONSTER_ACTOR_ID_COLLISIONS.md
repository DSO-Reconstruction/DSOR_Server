# Monster actor ID collisions

## Why a spawned monster can be invisible

The server assigns locally spawned monsters actor IDs beginning at `0x30000`
(`LOCAL_MONSTER_ACTOR_BASE`). The client also creates actors from objects in
the map scene. On some maps, those client-owned scene actors use the same
`0x30000` range. Actor IDs identify entries in the client's actor table, so a
server monster that reuses an ID already claimed by a scene actor can be
discarded, hidden, or associated with the wrong actor. The server can still
report that the monster spawned and quests can still list it as a target; that
does not prove the client created a visible monster model.

This is why the issue appears repeatedly in different maps: the ID allocator is
shared, but each map has its own authored scene actors and spawn layout. A fix
for one map is not evidence that every map uses the same IDs.

## The fix

`map_monster_actor_base()` now checks the physical map's
`_Instance_SpawnPoint` table and starts server monsters at the next `0x1000`
actor-ID block above the highest authored scene row. For example, a map with
457 rows starts server monsters at `0x31000`; a map with more than 4096 rows
gets a higher block. The same allocator is used for initial encounters and
later map-event spawns, so the rule applies across maps instead of relying on
an ever-growing list of quest-specific map IDs. The map data is read-only and
the maximum row is cached per database path.

Boss rooms and tower layouts retain a minimum offset for known client actors
that are not represented by SpawnPoint rows. Explicit reservations for special
actors such as tutorial bosses remain in place. `_CLIENT_SCENE_ACTOR_ID_MAPS`
is retained for those legacy cases, but ordinary maps with authored scene rows
no longer need to be listed there.

The `g0013_` map family used by Mühürlü Mezar is currently routed to the
secondary range so its Toltac High Shamans do not share the ordinary scene
actor range. Keep any new map-specific rule narrow and based on evidence for
that map.

The allocator must remain above all authored scene rows and preserve explicit
reserved actor IDs. Keep actor IDs stable within a map, and check that the
selected range does not overlap other server actors or a reserved protocol
range.

## How to diagnose it

1. Identify the physical gameplay map ID from the runtime session or map logs.
   Do not infer it solely from the localized map name or a quest label.
2. Confirm that the server loaded the expected monster template and produced
   an encounter/spawn actor. Record its actor ID and final position.
3. Check the map's client-owned scene actors and authored `SpawnPoint` rows for
   IDs in the same range. Evidence can include the client's actor-registration
   diagnostics, an ID shown in a crash/assertion, or a controlled test where
   moving only that map's server monsters to `0x31000` makes them render.
4. Confirm the server sends the monster spawn to the client. If no spawn was
   produced, investigate the event set, spawn row, quest gating, or map loader
   first; changing actor IDs cannot create a missing spawn.
5. If the spawn is sent with a unique ID and still does not render, inspect
   position/grounding, template graphics and status-effect modules, packet
   ordering, and the client's log. Invisible mobs have causes besides ID
   collisions.
6. Use the shared allocator for authored scene-row collisions. Add a map
   exception only when evidence shows a client-owned actor that is not
   represented by a SpawnPoint row. Then validate in the client that the mob
   renders, can be targeted, and counts toward its quest.

## Related fixes

`docs/INVISIBLE_MOB_SPAWNS.md` records other map-specific invisible-mob cases,
including the Wild Forest Crypt Exit, small-crypt captain, and Misty Ridge
guardian. Those cases also document when proactive `NewMonster` publication is
needed; that publication issue is separate from an actor ID collision.
