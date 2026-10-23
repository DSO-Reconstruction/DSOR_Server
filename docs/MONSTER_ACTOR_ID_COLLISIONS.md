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

For a map whose client scene reserves IDs in the ordinary monster range, start
server monsters at `0x31000` instead. Register the exact physical map ID in
`_CLIENT_SCENE_ACTOR_ID_MAPS` in `gameplay/local_monster_state.py`. The
`map_monster_actor_base()` helper then returns the secondary base for that map;
other maps keep the original `0x30000` base. Boss rooms and approach arenas
also use the secondary base through their existing policy.

The `g0013_` map family used by Mühürlü Mezar is currently routed to the
secondary range so its Toltac High Shamans do not share the ordinary scene
actor range. Keep any new map-specific rule narrow and based on evidence for
that map.

Do not shift every map to `0x31000` speculatively. Keep actor IDs stable within
a map, and check that the selected range does not overlap other server actors
or a reserved protocol range.

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
6. Add only the confirmed physical map to the map-specific secondary-range
   rule. Then validate in the client that the mob renders, can be targeted, and
   counts toward its quest. Keep the fix uncommitted until that live check is
   confirmed.

## Related fixes

`docs/INVISIBLE_MOB_SPAWNS.md` records other map-specific invisible-mob cases,
including the Wild Forest Crypt Exit, small-crypt captain, and Misty Ridge
guardian. Those cases also document when proactive `NewMonster` publication is
needed; that publication issue is separate from an actor ID collision.
