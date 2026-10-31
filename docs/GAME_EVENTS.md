# Game events

The runtime event authority is split deliberately:

- `static.db4` remains the canonical definition source for `_Template_Events`,
  `_Template_EventProgressBar`, `_Template_Item`, map targets, rewards, shops,
  status-effect drop modifiers, and map EventSets.
- `dso_server/data/events.json` selects runtime event instances and schedules.
  It is safe to edit this file without modifying the client database.
- `players.sqlite3` stores each character's progress, claimed reward keys, and
  participation state through the existing asynchronous account writer.

## Schedule configuration

Each entry requires a unique positive `event_id` and a database `template_id`.
`starts_at` and `ends_at` are UTC ISO-8601 timestamps. `state: scheduled` uses
those timestamps; `active`, `upcoming`, and `expired` are explicit operator
overrides. Changing an `event_id` starts a new per-character event instance.

Optional controls are:

- `tags`: aliases used by map `_Instance_EventSets.EventFlags` and quest event gates.
- `participation_default`: whether a character participates without opting in.
- `collectible_drop_chance` and `collectible_amount`: base drop controls.
- `collectible_maps` and `collectible_ranks`: additional server restrictions.

Set `DSO_EVENT_SCHEDULE` to load a different schedule file.

## Runtime behavior

Character selection and map initialization send typed `EventUpdate`,
`EventProgressInfo`, and `EventParticipationInfo` snapshots. Client
`ActiveEvents` refreshes receive the same current snapshots, while
`SetEventParticipation` changes and persists the character's preference.

The number before `/` in `EventCollectibles` is the progress represented by a
single drop. Monster drops also apply every active authored
`DropAmount:<value>,relative|absolute,<item>` status modifier, including `$N`
parameters supplied by the concrete status-effect instance. Event collectibles
remain character-bound because they use the existing per-session loot allocator.

A collectible drops where its event lives: a schedule's `collectible_maps`,
then the item's `Region` matched against the map and the killed monster, then
the maps the event owns. `data/catalogs/event_item_regions.json` authors that
`Region` for items the client leaves blank (Glimpse of the Past, the Larcenous
Dark Dwarves, Heroes Assemble). An event with none of these drops everywhere
only if it names a `Loot` list, as Monster Hunt's worthy opponents do; otherwise
its progress does not come from kills (Live Stream's bonus codes).

Crossing a reward threshold is atomic in character state. Item rewards use the
normal inventory delivery and item-received notifications; class-conditioned
rewards select the current class. `EventRewardCurrency` credits Draken. Claimed
reward keys prevent duplicate delivery across pickup, map transfer, and relog.

Every `TravelItem` is resolved from `_Template_Item.TravelItemTarget`. Operation
`0x13` arms the authored cast; the subsequent `MapTravelCommand` must match that
target before one item is consumed. Required level, `EventId`, `ActiveEvents`,
and `OnlyActiveDuringEvent` are enforced. Event-gated maps and map EventSet exits
are unavailable when none of their required events is active. Admin `!map`
intentionally bypasses the event gate for development.

The event-preview **Start** button sends a named-entry `TravelCommand`. Its
map/entry pair must match an active event's `TeleportTarget` and the event's
player-level window. It arrives at that map entry without requiring a prior
visit; map event and quest gates still apply. Events without a target fall back
to Kingscity's normal spawn. See [MAP_AND_CUSTOMIZATION.md](MAP_AND_CUSTOMIZATION.md)
for the native packet layout and handoff checks.

Event offers automatically use the active template IDs when shops are built.
Upcoming events are advertised but do not enable drops, offers, maps, or travel
items until their start time.

Quest XML `event` requirements also gate NPC offers, direct acceptance, and
signal-started quests. Any one semicolon-separated event ID may satisfy the
requirement. Eligibility reads the current schedule and running map-local
events, so expired map-entry flags cannot keep quests available. Existing
accepted quests retain their progress and turn-in behavior after event expiry;
see [QUEST_PROTOCOL.md](QUEST_PROTOCOL.md#event-quest-availability).

`_Template_Events.EventStatusEffects` names the durable buffs an active event
grants (`xpgain,$0:0.25` on `xp_bonus_event_01` is a 25% XP bonus; a `$N:value`
token sets parameter `N` of the effect named before it). Initialization
resolves them for every active event whose level window includes the
character, adds them to the persistent status set so combat, XP awards and the
client buff bar see them, and excludes them from the saved account state.
They are rebuilt on every map entry. CMS event actions also reconcile event-owned
buffs and push event/progress/participation snapshots to ready online characters.
While the CMS service runs, its schedule watcher refreshes online characters at
scheduled boundaries and detects manual schedule edits within a minute. Without
the CMS, buffs retain the existing refresh on map entry or relog. See [CMS event management](ADMIN_CMS.md#active-game-events)
for activation, deactivation, retained progress and map-instance refresh behavior.

Each active event is also announced once per login session with a
`NotificationCommand` popup on the first map initialization, keyed on the login
session GUID so map transfers stay quiet and a relog announces again. See
[EVENT_NOTIFICATIONS.md](EVENT_NOTIFICATIONS.md) for the client contract and
what still needs confirming on the live client.

## Map-local scripted events

Dungeons script themselves with three tables, all read-only content:

1. `_Template_Signal` is the trigger table: *when `EventType` happens with
   `EventParam`, and the conditions hold, emit `SignalId`*. Kill signals name
   `_Template_Monster` ids, prop signals name `_Instance_StateObject._ID`s,
   trigger signals name `_Instance_TriggerVolume` ids, map-entry signals name
   maps or access groups. `MonstersEliminated` means every listed template
   that exists on the map is dead; `PlayerKilledMonster` fires on any single
   kill; `MonstersEliminatedPercentage` compares the dead fraction of the
   whole hostile population with its parameter.
2. `_Template_Events` rows with `MapLocal=1` consume signals through
   `StartSignalId` / `EndSignalId`, with `MapEventStartDelay` and an
   auto-ending `MapEventDuration` in seconds.
3. Each map database gates its placed rows behind `_Instance_EventSets`
   flags (`BOSS_death=True;`) referenced by every row's `EventSetRow`.

`gameplay/map_event_state.py` loads the catalog and each map's gated content;
`services/map_events.py` owns one `MapEventRuntime` per map instance (keyed
like the combat state, attached to every session as
`gameplay_map_event_runtime`). The runtime evaluates only `ServerSignal` and
`MapSignal` rows; `ClientToServerSignal` and `ClientSignal` rows are accepted
when the client reports them through `StagingSignalCommand` (0x0113).

World happenings reach the evaluator from:

- kills: the two `_award_monster_kill_xp` sites raise
  `("monsters_killed", ...)` through the presence callback;
- movement: `_check_trigger_volumes` tests the player's world position against
  the unit-cube trigger boxes on every `MoveCommand` and reports each entry
  once; `QuestTriggerSignalCommand` (0x0125) is accepted as well;
- map entry: `PlayerEnteredMap` / `PlayerEnteredMapId` after presence;
- props: `PropInteractionCommand` (0x0039) looks the prop up by id, answers
  the way retail does (a `PropUpdateCommand` with the clicker's actor in the
  body, state 3 for a used one-shot prop, the game tick, every flag false,
  trailing actor 0, then a one-record `PropInfo` refresh) and raises
  `PlayerInteractedProp` with the instance id.

Initial map population and runtime transitions use the same event-set rule:
all `=True` flags must be active and every `=False` flag must be absent. This
includes baseline NPCs/props/exits in sets containing only negative flags.
Signals also evaluate quest predicates and `MapUnlocked` / `MapNotUnlocked`.
Map discovery is recorded per character on entry and persists through travel
and relogging; the development setting that exposes every fast-travel map
never advances these signal conditions.

When a map-local event starts or stops, the affected event sets are
recomputed and the instance receives:

- `EventUpdateCommand` with one entry, event id 0 and the map-local flag,
  to every session, for every started or stopped event (retail's exact
  mid-map shape; `NewEvent`/`DiscardEvent` are never sent);
- `NewMonster` actors for the gated `_Instance_SpawnPoint` rows, registered
  on the shared `LocalCombatState` (`add_runtime_monsters`) and announced
  through the normal vicinity path; rows whose template already stands on the
  transform (boss rooms materialize their boss on entry) are skipped;
- `PropInfoCommand` records for gated state objects (scene GUID) and
  state-object spawn points (zero GUID), which is how retail creates props
  mid-map; `DiscardPropCommand` hides them again. Only interactive, chest,
  and signal-referenced objects are published, always with an empty
  operation string, as retail sends even for interactive props;
- a fresh `ExitInfoCommand` and `gameplay_map_exits`, so exits revealed by a
  boss kill become usable.

Late joiners receive the running events in their initialization
`EventUpdate`, the published props through `PropInfo`, and the revealed exits.
Everything logs with the `map-event` prefix.

Not implemented yet: chest loot on interaction (`LootListId`), prop
respawn timers (`RespawnTime` on state-object spawn points), `OncePerAccount`,
and general damage/status location effects (companion Minion volumes are supported).


### Story companions and staging bosses (2026-09-09)

`gameplay/map_companions.py` resolves `_Instance_LocationEffect` volumes through
`AuraEffectId` and `StartModifiers:Minion` to `CompanionStrategy` monsters.
`services/map_companions.py` evaluates those volumes on entry and movement,
respects their EventSets, and claims one companion per owner/template in the
map combat state. Departure cleanup releases the claim; a defeated story
handoff prevents the combat companion from respawning in that map instance.

These actors reuse the summon combat/delivery machinery, with retail category
2, friendly monster faction `0x10001`, relationship 0 and the player owner.
They retain authored level/rank stats, wait for StartupDuration, follow along
navigation routes, and attack nearby hostiles. They do not disappear at the
40-unit construction leash. Named waypoints place them in the boss formation;
behavior auras unlock the authored boss-facing projectile skill. Hidden
cinematic skills are not selected as their initial combat attack.

The ordinary-map population path now resolves concrete bosses behind staging
actors just as the dedicated boss-room path does. It preserves row locations
and EventSets. In the fortress this replaces the invulnerable, untargetable
Grimmag spawner with the real quest boss and its combat skills. This uses the
existing direct boss materialization policy; it does not replay the staging
actor's full intro choreography.

Death handoffs are data driven: `IncidentLocationStatusEffects` with `ON:DIE`
and a targeted companion `Despawn` aura supply the delay, victim template and
radius. A native Hit/Kill retires that companion, using its DeathDuration,
while the ordinary boss-kill signal reveals the NPC and later the exit.
Timers check map incarnation and combat identity. On re-entry, persisted
completed kill tasks restore the earned handoff event without new kill credit
or rewards.

Retail evidence, `capture_20260909_000000_sample_10.log`:

- 16:08:40.350: tomb Vithrandir (`a0004_companion_spellweaver_01`).
- 16:10:29.496: fortress Vithrandir (`a0005_companion_spellweaver_01`).
- 16:11:33.292: real Grimmag, separate from the staging actor.
- 16:12:36.101: Grimmag Hit 0/1112 and Kill, corpse duration 350 ticks.
- 16:12:38.658: Vithrandir Hit 0/452 and Kill, corpse duration 150 ticks.
  His position matches the second `mentor_waypoint`. The authored boss
  death aura has DE=2.5 and radius 11, targeting that companion explicitly.
- 16:12:52.570: the wounded quest NPC appears, matching the 16.5-second
  `a0005_start_fortress_npc_01` event delay.
- Accepting `a0005-WayToGrimford-01-*` enables the Grimford exit.

`tests/test_map_companions.py` exercises both maps, actor wire fields, following,
attacks and formation changes, all four class quest chains through real NPC
requests, re-entry, duplicate notifications and stale callbacks. This does
not implement every escort/protect script: arbitrary hold-ground objectives,
non-companion location effects and full cinematic skill sequencing remain
outside this support. Client animation playback still requires an in-game
validation pass.


## Event-driven spawn areas

Event activation now materializes newly enabled `_Instance_SpawnArea` density
populations as well as fixed `_Instance_SpawnPoint` actors. Positions use the
existing navigation/terrain projection, and runtime actor IDs are associated
with their event set for retirement and duplicate prevention. Areas are selected
within the newly enabled sets so an earlier wave with the same transform cannot
mask the next wave through difficulty-variant deduplication.

Client data contains SpawnListId references without the original server spawn
list table. The event path reconstructs eligible pools from the running wave's
authored `MonstersEliminated` selectors, rank and spawn-list tokens, falling back
to the ordinary map population pool where needed. Exact retail random weighting
is not available in these assets. Invisible countdown markers without combat
skills remain presentation-only; their absence no longer prevents wave monsters
from spawning. The anniversary random sequence is regression-tested through
all five clears and reward activation.

The anniversary arena's final wave can produce a 2,703-byte monster visibility
transaction (69 actors), exceeding the 1,232-byte client payload budget. All
three visibility paths (event spawn, movement discovery, and peer sync) now
use the same MTU-aware reliable sender. Sending this transaction unsplit left
it unacknowledged and blocked subsequent ordered replies, including reward
chests. The regression uses the actual Painful final-wave population, loses
and retransmits a fragment, and verifies delivery of the main reward chest,
one guardian chest, two champion chests, and one leader chest. The full random
wave sequence checks those exact five chests at every authored difficulty
(0 through 9).

Wave events now broadcast the final **5, 4, 3, 2, 1** seconds to initialized
players sharing the map instance, followed by **Wave N** when the event starts
and its monsters are spawned. Wave delays have a five-second minimum; longer
authored delays remain intact. Numbered wave IDs (including difficulty and A/B
suffixes) supply N. Unnumbered monster/boss wave events use **Monster wave** or
**Boss wave**. Countdown marker and wave completion events do not announce waves.

Both spawn timers and countdowns use the same monotonic deadline. Cancelled,
replaced, or already-started events ignore stale callbacks, and delayed
callbacks skip expired digits. Late joiners receive subsequent ticks without
resetting the instance's timer. Countdown digits use `MessageCommand` type 5,
one kind-1 integer, actor 0 (native handler `0x1405e5c7c`). The wave label uses
`ServerToClientMsgCommand` (`0x0170`): int32 type 0, message string, empty
auxiliary string, actor 0. Its decoder is `0x14093e6f4`; `ClientMessageManager`
at `0x1401d9978` routes type 0 to the nonmodal `MessageWidget` queue. Types 1/2
would open dialogs and are not used. Labels are currently server-supplied English.

Density pools exclude bosses and boss staging actors, including when a wave
selector contains them. Fixed spawn points retain their event gates. See
[BOSS_SPAWNS.md](BOSS_SPAWNS.md) for the duplication investigation, authored boss
coordinates, and population regressions.
