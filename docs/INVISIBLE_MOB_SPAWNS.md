# Diagnosing invisible map mobs

This guide covers authored monsters that are present in a map but do not render
in the client, especially when other monsters on the same map are visible. It
records the Wild Forest Crypt Exit correction and the earlier small-crypt fix
as examples of the same actor-registration failure.

## Wild Forest Crypt Exit case

Use the physical map ID from the loaded encounter, not a display name or the
map the monster template prefix appears to suggest. The Nefertari Guard in the
Wild Forest Crypt Exit area is on `a0302_wildforest`:

| Property | Value |
| --- | --- |
| Spawn table | `_Instance_SpawnPoint` |
| Template | `a0302_normal_s_undead_mage_q_CultPath01` |
| Rank | `Rank_guardian` |
| EventSetRow | `-1` (not event-gated) |
| Authored position (X, Y, Z) | `(106, -6, 19)` |
| Grounded server position | `(106, -6, 19)` |
| Map-entry position | approximately `(-32.37, -4.03, 17.54)` |
| Distance from entry | approximately `138.4` units |

The initial server actor ID was `0x30001`, in the range used by client-owned
scene SpawnPoints. The client could receive the map and other monsters while
failing to create this mob. The correction assigns Wild Forest monsters from
the secondary `0x31000` range, making this actor `0x31001` (`200705`). It also
proactively sends this authored guardian as a `NewMonster` when it enters the
player's vicinity. A 250-unit enter radius includes it at the Wild Forest
entry; the 275-unit exit radius provides hysteresis while visible.

The position is not below the map. At the final spawn X/Z coordinate, the map
collision data contains a ground surface at Y=`-6`, matching the mob position.

## Diagnosis workflow

1. **Confirm the physical map and exact actor.** Find the map's authored row in
   `data/maps/<physical-map-id>.db4`, usually `_Instance_SpawnPoint`. Compare
   exact template IDs case-insensitively. Do not infer the physical map from a
   region label, quest name, or template prefix.
2. **Check whether the row is active.** Read `Rank`, `NpcId`, `EventSetRow`,
   and `Transform`. A nonnegative `EventSetRow` may require an active map
   event. A blank `NpcId` can mean a scripted spawn anchor rather than a mob.
3. **Decode the authored transform.** `Transform` is a 4x4 little-endian
   float matrix; elements 12, 13, and 14 are world X, Y, and Z. Then inspect
   the final position in `LocalMonsterEncounter.spawn_batch`, because server
   grounding or overlap recovery can adjust it.
4. **Compare the final Y against collision ground.** Use the map's `.map` and
   `.col` data with `gameplay.map_collision_terrain.load_map_collision_terrain`
   and inspect `terrain.heights_at(final_position)`. Maps can have multiple
   surfaces at the same X/Z, so compare against the surface nearest the mob's
   reference Y rather than treating every lower surface as its floor.
5. **Check actor ID allocation.** `gameplay.local_monster_state.map_monster_actor_base`
   allocates server monsters. Some map scenes already use `0x30000` for
   authored SpawnPoints. Add a map to `_CLIENT_SCENE_ACTOR_ID_MAPS` only after
   confirming that its client scene owns that range. The secondary base is
   `0x31000`; do not change every map's base speculatively.
6. **Check the visibility and request path.** Normal monsters are discovered
   through movement and vicinity updates; they are not all necessarily sent
   in one packet when the map loads. Some clients announce an authored actor's
   vicinity but never send the follow-up `ActorRequest`. For affected authored
   actors, send `NewMonster` before `ActorsEnterVicinity` and reserve the actor
   ID to prevent a duplicate request from creating it twice. Include the
   actor in the selection even if it was already announced, provided this
   viewer has not received its `NewMonster` yet.
7. **Check runtime evidence.** The priority-spawn log should identify the map
   actor ID and final position, for example:

   ```text
   proactively sent authored priority NewMonster actor(s) (200705,) at ((106.0, -6.0, 19.0),)
   ```

   If the line is absent, check the loaded physical map, exact runtime template
   ID, alive state, announcement/spawn ledgers, radius, and whether the running
   process loaded the edited source. If it is present but the model remains
   invisible, inspect actor-ID collisions, template graphics/CharacterSet,
   packet ordering, and client logs.

## Similar known case

The small-crypt captain correction in commit `c8f50cc` uses the same two
principles: allocate actors outside the client scene's `0x30000` range and
proactively send the authored actor when it is near the viewer. Wildherz
Caverns (`a0303_wildcave`) uses the same secondary actor range for Shaman
Yachak. Keep these as map-specific registrations; a successful fix on one map
does not establish another map's physical ID, template, spawn position, or
client-owned actor ranges.

## Misty Ridge Great Gnome Shaman

Misty Ridge (`f4101_pve`) has one active authored guardian at row 1:
`f4101_normal_s_gnome_shaman_TheGreatElevator_02`, with no event gate. Its
authored transform is `(74, 28, 58)` and collision grounding places it at
`(74, 27, 58)`, on the terrain. The map entry is about 206 units away, so the
ordinary 18.5-unit vicinity threshold controls when it is published. Its
default actor ID was `0x30000`, the first slot potentially owned by the map
scene. The map now allocates server monsters from `0x31000` and proactively
sends this guardian before vicinity registration when it comes within range.

## Verification notes

For the Wild Forest correction, loading `a0302_wildforest` produces one
`CultPath01` guardian at actor ID `200705`, position `(106, -6, 19)`. A
vicinity-selection diagnostic from the map-entry position includes the actor
and encodes a nonempty `NewMonster` packet. The collision query returns the
floor at Y=`-6`. These checks validate local server data and packet selection;
interactive client verification still requires a server restart and a live
map visit.
