# Map Content Runtime Diagnostics

Use this sequence when a map has missing monsters, invisible monsters,
non-triggering map events, or a missing gate. These symptoms can share a
map-initialization failure, but they do not all have the same cause.

## 1. Confirm the authored content

- Resolve the physical map ID and its map database with
  `map_database_path()`.
- For a monster, inspect `_Instance_SpawnPoint` (`NpcId`, `Rank`, `Transform`,
  `EventSetRow`, and `ParentNodeIndex`) and `_Instance_SpawnArea` (`SpawnListId`,
  density, event set, and transform).
- A blank `NpcId` may be a scripted anchor. Trace its location/name through
  quest `<Spawn>` entries, linked state objects, and map-event definitions
  before treating it as a normal monster row.
- Confirm the runtime template exists and that the encounter loader actually
  selects the row. Explicit encounter definitions must supplement active map
  rows; they must not silently replace them.

## 2. Follow event-set state

- `EventSetRow=-1` is unconditional. For a nonnegative row, resolve its
  `_Instance_EventSets.EventFlags` against the instance's active quest, access,
  and scheduled event flags.
- Confirm the transition is applied by `MapEventRuntime`, then check the
  resulting `enabled_event_sets` and the rows returned by
  `gated_spawn_rows()` / `props_for_sets()`.
- Check variations and scaling selection as well. A valid row can be excluded
  because its difficulty variation is not selected.
- Keep the flag, EventSet, and content-row checks separate: an event can start
  while its expected row is absent, and a row can exist while its event never
  enables the row.

## 3. Confirm server materialization and client visibility

- Compare the generated encounter with the selected authored rows, including
  final positions and actor IDs.
- Server monsters use `map_monster_actor_base()`. It reads the highest
  `_Instance_SpawnPoint` row and places server IDs after the client's authored
  scene range; runtime map-event actors use the same combat allocator.
- Check navigation reachability from the map entry. Disconnected PathEngine
  components can reject a valid quest spawn before actor allocation. Inspect
  narrow authored portals before moving the spawn or bypassing reachability.
- If the server has the actor but the client does not render it, inspect the
  `NewMonster`/vicinity/request path and the client's actor registration log.
  A collision is proven only when IDs overlap or the client rejects duplicate
  registration; a missing actor alone does not prove an ID collision.

## 4. Trace gates separately

- Start with the source map's active `_Instance_MapExit` entry: destination,
  EventSet, GUID, and selected variation.
- Confirm the loaded scene contains the corresponding exit entity. Some maps
  have a valid exit row but omit its gate entity from the scene; those maps
  need a `VariationInfo` entry for the exit GUID through
  `native_scene_exit_variation_entries()`.
- For event-locked gates, verify that the exit row's EventSet becomes active
  and that the correct GUID/variation is sent to the client. Mob spawning and
  gate materialization can fail independently even when they use the same
  map-event flags.

## Evidence-backed examples

- `b1201_pve`: an explicit entrance encounter suppressed an unconditional
  Hunted Goods guardian. The encounter now appends active authored rows.
- `c2102_dun`: a narrow authored navigation portal split the Cooled Lava
  Cyclops from the entry component. The map uses a lower portal-width cutoff.
- `a0500_canyon`: the Arachna's Refuge exit existed in the exit table, but its
  gate entity was missing from the loaded scene. The scene now receives the
  selected exit by GUID.
- `a0700_hidden_village_dun`, `e_khs_01_dun_01`, and `e_easter_dun_01`: each
  had unconditional SpawnAreas but no template pool, so the loader created no
  encounter. Their pools now use monster templates already present in the
  corresponding map/event quest data.
- `e_bos_mortis_pw_boss`: the boss selector returned a four-column row for a
  five-column SpawnPoint schema. Preserving the source row shape prevents the
  encounter loader from failing on this boss map.

## Whole-catalog source audit

The current audit covered all 322 unique `_Template_MapTemplate` entries and
all 296 map databases with hostile SpawnPoint/SpawnArea content. The three
catalog entries without a physical map database are `debug_map_bosses`,
`home`, and `preview`, so they could not be checked as physical scenes. Of the
296 content maps, the encounter loader
materialized 217 encounters. The other 79 are event/boss/quest-driven maps or
have no unconditional monster rows; reviewed examples include boss rooms
controlled by `MapEventRuntime`, color/time-trial gated `e_doe_map_02`, and the
legacy disabled quest map `j0002_lava_dun`. No loader exceptions remained.

The audit also checked authored actor-ID ranges, duplicate IDs, NPC ID overlap,
active exit structure, and EventSet references. It found no allocator starts
inside authored SpawnPoint IDs, duplicate encounter actor IDs, malformed active
exits, or dangling EventSet references in the inspected databases. These are
source/data checks; per the task scope, native-client render validation was not
required.
