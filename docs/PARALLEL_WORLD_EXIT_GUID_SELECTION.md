# Parallel World exit variation selection

Parallel World Q1 progression and difficulty-specific portal labels are now
confirmed working in game.

**Fixed:** the Parallel World boss-arena reward chests were invisible even
though the server state and outgoing packets were correct.
`Game::ClientPropManager::HandlePropInfoCommand` (`0x1402206b8`) only creates a
prop entity when the entry's GUID is empty (`0x1402208e9` tests the GUID,
`0x14021f2c4` is the create-from-template path). A non-empty GUID goes through
`LookupPropEntity` (`0x1402220cc`), and when the scene has no entity with that
GUID the entry is silently dropped at `0x1402209f1`. The authored chest GUIDs
are quest-controlled and never materialize for a player without the active
reward task, so both the initialization snapshot and the map-entry replay were
ignored.

Each of the nine boss arenas in the Q1-Q27 chain (`pw003_grimmag_01_boss`,
`pw006_arachna_01_boss`, `pw009_heredur_01_boss`, `pw012_bearach_01_boss`,
`pw015_khalys_boss_01`, `pw018_herold_01_boss`, `pw021_sigrismarr_01_boss`,
`pw024_medusa_01_boss`, `pw027_gorga_01_boss`) authors its reward chests as
`MapVariation` nodes. The server now rewrites every `pw_run_lootchest` /
`pw_run_lootchest_gold` prop to a fresh prop id with a zero GUID, using the
non-quest `t000_state_chest_loot_04/05` visual twins, and keeps its authored
transform. Grimmag's chest, for example, stays at `-10.04, 0.28, 44.88`, about
5 m from the boss; the other arenas likewise use their own authored positions
rather than being moved to the map entry.

The variant tag comes from the map's `.mvr` `MapVariation` family. Most arenas
author three rows for tiers 5, 7 and 9 (Infernal, Merciless, Bloodshed);
`pw024_medusa_01_boss` authors eleven (0-10). PropInfo sends keep only the row
whose variation matches the player's `gameplay_map_difficulty`, so one chest
appears per viewer instead of every difficulty variant stacking.

Each arena's `<map>_spawn_chest` EventSet is enabled in the local baseline so
the chest is present in the initial prop snapshot. The props are replayed after
map entry and again after the boss-spawn transition, matching the timing that
made the Q1 chest appear reliably in the loaded scene. Boss death adds the same
EventSet to `boss_reward_chests_unlocked`, which is the only gate on
interaction; the chest remains visible while the boss is alive.
`tests/test_grimmag_reward_chest.py` now covers all nine arenas: zero-GUID and
fresh-id rewrite, one authored position per arena, tier-specific selection in
the initialization snapshot, and boss-death unlocking of the matching EventSet.

The three tier exits in Grimmagstone and Alliance Fortress share a position.
Filtering only `ExitInfo.field_10`, or filtering only the final update after
`EventUpdate`, does not prevent the other variants from being instantiated.

Native `dro_client64.exe` behavior inspected for this fix:

- `Commands::ExitInfoCommand` handler at `0x1401cad08` inserts or updates entries
  in the existing cache. Entries absent from a later batch are not deleted.
- `ClientGameWorld::GetExitInfo` at `0x1401c54fc` returns the cached entry by Name.
- `EntityEventController::UpdateEntities` at `0x1401fe020`, specifically
  `0x1401fe339` through `0x1401fe40e`, reads an InteractExit's Name, looks up
  ExitInfo, and compares its GUID to the scene variation's GUID. A match forces
  that variant into the entity creation path without reading `field_10`.
  Nonmatching/missing variations continue through the ParentNodeIndex and
  MapVariationManager selection checks.
- `ClientGameWorld::UpdateExitState` at `0x1401dce7c` reads the scene entity's
  authored ExitURL to derive its displayed label; changing another exit's
  enabled state does not rewrite this entity's destination.

Therefore the selected tier's GUID must be the only tier GUID ever cached,
starting with initialization while the gate is still closed, and including
the closed ExitInfo snapshot before the event activation edge. Sending all
three GUIDs once and then sending only the selected tier is too late.

`filter_parallel_world_exits_for_access` removes other variants from every
initialization and event-stage payload. It preserves unrelated exits such as
the return to Cardhun. The regression test merges initialization and subsequent
ExitInfo entries and checks the GUID set at event activation for all three
difficulties on both maps, including late joins.

Windows UDP sockets also use `SO_EXCLUSIVEADDRUSE`. Multiple older run.py
processes had remained alive with overlapping UDP binds due to SO_REUSEADDR,
so different services could be served by different code versions.
