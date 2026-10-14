# Prop, map-event, and quest-trigger commands

Wire layouts and client behaviour for the commands that flip levers, reveal
chests, and report trigger hits. The layouts were recovered from
`dro_client64-patch5.exe` (the modern build). Two retail sessions captured
on 2026-09-09 confirm `PropInfo`, `PropRequest`, `PropInteraction`,
`PropUpdate`, map-local `EventUpdate`, and the client-to-server framing byte
for byte (§11, §12). `NewProp`, `DiscardProp`, `QuestTriggerSignal`, `NewEvent`,
and `DiscardEvent` never appeared on the wire; their layouts remain
static-analysis results, and the dungeon session shows retail did not need
the first two to spawn props mid-map.

| Command | Id | Direction | Client side | Server today |
| --- | --- | --- | --- | --- |
| `PropInfoCommand` | `0x0036` | S→C | `ClientPropManager::HandlePropInfoCommand` | implemented, `encode_prop_info` |
| `NewPropCommand` | `0x0034` | S→C | `ClientPropManager::HandleNewPropCommand` | **missing** |
| `DiscardPropCommand` | `0x0035` | S→C | `ClientPropManager` handler at `0x1402169c0` | **missing** |
| `PropUpdateCommand` | `0x0037` | S→C | `ClientPropManager` handler at `0x140218464` | **missing** |
| `PropRequestCommand` | `0x0038` | C→S | `ClientEventSystem` (map-event start) | implemented, `_handle_prop_request` |
| `PropInteractionCommand` | `0x0039` | C→S | `NetworkCommandCreatorProperty` at `0x140170428` | **missing** |
| `QuestTriggerSignalCommand` | `0x0125` | C→S | `ClientQuestManager` at `0x140225968` | **missing** |
| `NewEventCommand` | `0x00DB` | server-internal | **no client handler** | do not send |
| `DiscardEventCommand` | `0x00DC` | server-internal | **no client handler** | do not send |
| `EventUpdateCommand` | `0x00DD` | S→C | `ClientEventSystem` handler at `0x1401f19c8` | implemented |

Every command above is `actor_scoped: true` except the two event commands.
For the server-to-client ones that means the body is followed by the
trailing `uint32` actor described in [COMMAND_IDS.md §5](COMMAND_IDS.md).
Client-to-server commands carry **no** trailing actor: the retail client
sends `0x8B`, the `u16` id, and exactly the decoder's fields (§11). The
decoders are byte-for-byte identical
in the legacy release228 client (`flatten_decoder_x64.py --diff`), so the
layouts apply to both forks.

## 1. Wire conventions used below

Everything is RakNet bit-packed, so a field may start at any bit offset.
The repo's `BitWriter` in `protocol/character_selection.py` already produces
every primitive:

| type | encoding |
| --- | --- |
| `u32`, `i32` | 32 bits, little-endian (`write_uint32_le`) |
| `f32` | 32-bit IEEE float, little-endian (`write_float`) |
| `u8`, `enum8` | 8 bits (`write_uint8`); enums are signed bytes, `-1` is `0xFF` |
| `bool` | 1 bit (`write_bool`) |
| `str`, `atom` | `u16` LE byte length, then UTF-8 bytes (`write_string`); an *atom* is read the same way and interned as a `Util::StringAtom` |
| `guid` | `u16` LE length (always 16), then 16 raw bytes; the client byte-swaps the first `u32` and two `u16` into a `Win32Guid`. Send the map database `Guid` blob verbatim, as `encode_prop_info` does |
| `float3` | three `f32` (x, y, z); the client sets w = 1.0 |
| `[T]*` | `u32` count, then `count` records of `T` |

Server-to-client framing is `0x84` (or `0x85` for a batch), `u16` LE
command id, body, then the trailing `u32` actor for actor-scoped commands
(`_write_command_header` / `_write_command_actor` in
`protocol/gameplay_commands.py`). Client-to-server framing is `0x8B`, `u16`
LE command id, body, and nothing else; `decode_simple_gameplay_command`
already treats `PropRequestCommand` as fieldless on that basis.

Two reader details matter for validation. The `enum8` readers range-check:
the prop-state reader rejects the whole command unless the byte is in
`-1..3`, and the quest-indication reader maps anything outside `-1..13` to
`-1`. A rejected decode leaves the bit reader mid-stream, so every command
batched behind it is garbage (see [COMMAND_IDS.md §5](COMMAND_IDS.md)).

## 2. The shared prop record

`PropInfoCommand`, `NewPropCommand`, and `PropUpdateCommand` all carry the
same twelve values, but in **three different orders**. The names below are
derived from what `ClientPropManager` does with each value; the
`field_xx` names are the ones `PropInfoEntry` in
`protocol/gameplay_protocol.py` already uses.

| name | `PropInfoEntry` | type | what the client does with it |
| --- | --- | --- | --- |
| prop id | `prop_id` | `u32` | `Util::Id<GameProp>`; echoed back in `PropInteractionCommand`. **The client uses only the low 16 bits as the slot**: `ClientPropManager::RegisterPropEntity` (`0x1402197c5`) and the world's prop lookup (`0x1409ec90e`) both `movzx` the low word before indexing, so two live props whose ids differ only above bit 15 overwrite each other. Allocate every prop of a map from one sequence, as retail does from `0x10000`; the server's `spawn_prop_id` in `gameplay/map_event_state.py` places spawn-point props after the last scene rowid for that reason |
| template id | `template_id` | `atom` | key into `TemplateManager`'s `Dictionary<StringAtom, Game::PropData>`, loaded from `_Template_StateObject.Id`. Unknown ids log `Prop template data of id '%s' not found` and reject the command |
| instance guid | `instance_guid` | `guid` | if any byte is non-zero, `LookupPropEntity` binds the prop to the scene entity with that `_Instance_StateObject.Guid` (asserts the entity has no `Attr::PropId` yet); if all 16 bytes are zero, the client factory-creates a new entity from the template at the given transform |
| variant | `field_20` | `i32` | `-1` = none. `0..3` select the visual state name `blue`, `red`, `yellow`, `green` instead of the state name (`ClientPropManager` at `0x1402196d0`); used for coloured levers/crystals |
| state | `field_24` | `enum8` | `Game::PropState::Id`: `0` inactive, `1` active, `2` cooldown, `3` inactive (second alias); anything else is `invalidstate`. Drives the entity's staging state (`SwitchActiveState`) and highlight |
| game tick | `field_28` | `u32` | stored at `GameProp+0x64` next to the state; not read by any `ClientPropManager` handler. Retail fills it with the map's current game tick at send time (about 25 per second: the same prop was sent with 250, 300, 333, 350, and 650 over 16 s, and two records in one command carry the same value); the city session sent `0` |
| position | `position` | `float3` | absolute map position (the handler asserts `pos.w() > 0`) |
| rotation | `rotation` | `f32` | yaw in radians; the handler builds the matrix from `sin`/`cos` of it |
| operation | `field_48` | `str` / `atom` | operation name stored at `GameProp+0xb8`. Non-empty makes the prop interactive: the client raises `PropOperationProgressChanged`, and **this exact string is what `PropInteractionCommand` sends back**. Retail template values live in `_Instance_StateObject.InteractOperation` / `OperationId`. For quest interact targets retail fills it with `<quest_id>_<task_title>` (for example `a0002-WayToGrimmagstone-02-n_a0002-WayToGrimmagstone-02-collect-02`, tutorial capture of 2026-09-09), which is how the interaction is attributed to a quest task; see [QUEST_PROTOCOL.md §8](QUEST_PROTOCOL.md) |
| quest id | `field_50` | `str` / `atom` | quest whose guidance icon the prop shows, stored at `GameProp+0xc8` |
| task index | `field_60` | `i32` | quest task index, stored at `GameProp+0xd0`; `-1` = none |
| quest indication | `field_5c` | `enum8` | `-1..13`, stored at `GameProp+0xc0`; passed to `ClientQuestManager` to pick the overhead icon (`ui/icon_questionmark`, `ui/icon_exclamationmark`, `ui/icon_questmark_exclamation_repeatable`) and colour. Only evaluated when *operation* is non-empty and *state* is `1` |
| extra byte | `field_58` | `u8` | `PropInfo` only; not read by the handler. Send `0` |

Retail-shaped defaults, from the working crafting-workbench path in
`gameplay/local_map_state.py` and confirmed by every retail record in §11 and §12:
variant `-1`, state `1`, game tick `0` (city) or the current tick (dungeon),
empty operation and quest strings, extra byte `0`, indication `-1`, task
`-1`. Quest interact targets differ: operation `<quest_id>_<task_title>`,
indication `6`, quest id still empty (tutorial capture of 2026-09-09). Retail sent the empty operation string even for interactive props
(eggs, herbs, altars), and the client still sent `PropInteraction` for them.

Static content behind the record:

- `_Template_StateObject` (2683 rows) is the template table; every row has
  `State = 'active'`. Columns of interest: `OneShot`, `IndividualUsage`,
  `QuestControlled`, `Permanent`, `ShowAlways`, `InteractOperation`,
  `OperationId`, `MechanismId`, `ChestId`, `KeyId`, `KeyAmount`, `LootListId`.
- `_Instance_StateObject` in each map `db4` has the same columns plus
  `Guid`, `Transform`, `EventSetRow`. Across the 390 maps: 16 895 objects
  with no interact operation, 3 821 `OpenChestWidget`, and a few dozen
  crafting windows. `QuestControlled = 1` marks 1 460 quest props (the six
  `g0003_DefeatBalor_*` altars, lanterns, and chests are among them). The
  local server publishes those per player, only while the quest task that
  switches them is open (QUEST_PROTOCOL.md §9); `ShowAlways = 1` objects
  stay visible as client-side scenery in the meantime.
- `_Instance_StateObjectSpawnPoint` (25 816 rows) places props that are not
  in the scene at load time (`StateObjectId`, `SpawnCount`, `RespawnTime`,
  `DespawnTime`, `SpawnImmediately`). Those are the ones a server reveals
  with `NewPropCommand` and a zero GUID.
- `_Template_PropOperation` (12 rows, all PvP capture channels) and
  `_Template_PropMechanism` (43 rows, spawners) back `OperationId` and
  `MechanismId`; `TemplateManager::LoadPropOperations` reads them.

## 3. `PropInfoCommand` (`0x0036`)

Already implemented; listed for the field order, because the other two
commands differ from it.

```
u32 count
count × PropInfo record:
    u32   prop_id
    atom  template_id
    guid  instance_guid
    i32   variant          (field_20)
    enum8 state            (field_24)
    u32   counter          (field_28)
    float3 position
    f32   rotation
    atom  operation        (field_48)
    atom  quest_id         (field_50)
    u8    extra            (field_58)
    enum8 quest_indication (field_5c)
    i32   task_index       (field_60)
u32 actor
```

Element reader `0x140a52714`, array reader `0x14091f0ac` (count is checked
against `1 000 000`). For each record the handler looks the prop id up; a
known prop only gets its state/counter refreshed, an unknown one is created
exactly as `NewPropCommand` would create it.

The client sends `PropRequestCommand` (the three bytes `8b 38 00`, no body,
no actor) during map initialization, which `_handle_prop_request` in
`services/inventory_interactions.py` already answers, and again from
`ClientEventSystem` when a map-local event starts, together with
`NPCRequestCommand` (`0x0029`), at `0x1401f2f20`.

## 4. `NewPropCommand` (`0x0034`)

Decoder `0x14093ce6c`, handler `Game::ClientPropManager::HandleNewPropCommand`
(`clientpropmanager.cc:0x124`).

```
u32   prop_id            +0x20
atom  template_id        +0x28
guid  instance_guid      +0x30
i32   variant            +0x40   (default -1)
enum8 state              +0x44   (-1..3, else the decode fails)
u32   counter            +0x48
str   operation          +0x50   (Util::String, not an atom)
atom  quest_id           +0x90
i32   task_index         +0x98
enum8 quest_indication   +0x9c
float3 position          +0xa0
f32   rotation           +0xb0
u32   actor
```

Handler behaviour, in order:

1. If a prop with `prop_id` already exists, the command is **silently
   accepted and ignored** (`return 0xff00`). Re-sending is safe.
2. The template is resolved; a miss logs
   `ClientPropManager: Prop template data of id '%s' not found.` and rejects.
3. `GameWorld::NewProp(id, PropData, guid, variant, state, matrix)` creates
   the `GameProp`; failure logs `Could not factor new prop with prop id (%u)`.
4. The scene entity is bound (`LookupPropEntity` by GUID) or created from the
   template at `position`/`rotation`, and registered with
   `LocationMapManager`.
5. With a non-empty `operation` the client raises
   `PropOperationProgressChanged`, and if `state == 1` it also raises the
   quest-guidance attributes from `quest_id`/`task_index`/`quest_indication`
   and highlights the entity. `operation`, `quest_id`, `task_index`, and
   `quest_indication` are stored on the prop for later interactions.

Retail did not use this command in the dungeon session (§11): every prop
that appeared mid-map, including freshly spawned eggs and herbs, arrived as
a **one-record `PropInfoCommand`** coalesced into whatever `0x85` batch the
server was emitting that tick. `PropInfo` creates unknown props exactly as
`NewProp` would, so a server can reveal a chest either way; `NewProp` is the
single-record form with a different field order.

What the server needs to send it: a fresh `prop_id` (the existing local
props use `LOCAL_PROP_ID_BASE + rowid`), the template `_ID`, the spawn
point's `Transform` (or the scene object's `Guid` when revealing an object
that is already in the scene), and the state. For the Balor reward chests the
map rows are `_Instance_StateObjectSpawnPoint` entries whose `StateObjectId`
names a `t000_general/state_chest_loot_*` template; send a zero GUID so the
client builds the chest itself.

## 5. `DiscardPropCommand` (`0x0035`)

One-field decoder, handler at `0x1402169c0`.

```
u32 prop_id   +0x20
u32 actor
```

An unknown id logs `Received DiscardPropCommand with invalid prop id (%u)!`
and rejects. Otherwise the client removes the scene entity through
`EntityManager` and unregisters the prop; `Prop with id (%u) could not be
removed!` is the failure log. Use it to hide chests and levers mid-map; a
follow-up `NewPropCommand` with the same id is accepted again afterwards.

## 6. `PropUpdateCommand` (`0x0037`)

Decoder `0x14093dacc`, handler at `0x140218464`. This is the state flip.

```
u32   prop_id            +0x20
i32   variant            +0x24   (default -1; read through a local, not a lea,
                                  so flatten_decoder_x64.py mislabels it +0x40)
u32   actor_id           +0x28   (staging signal source, see below)
enum8 state              +0x2c
u32   counter            +0x30
bool  state_changed      +0x34
str   operation          +0x38
str   quest_id           +0x78
i32   task_index         +0xb8
enum8 quest_indication   +0xbc
u32   unknown_c0         +0xc0   -> GameProp+0x168
f32   unknown_c4         +0xc4   -> GameProp+0x164
bool  operation_changed  +0xc8
i32   unknown_cc         +0xcc   -> GameProp+0x160
u32   unknown_d0         +0xd0   -> GameProp+0x16c
bool  keep_visual_state  +0xd4
u32   actor
```

Handler behaviour:

1. Unknown `prop_id` is silently accepted; a prop without an entity logs
   `WARNING: Received PropUpdateCommand where entity is missing!`.
2. If `actor_id` is not the invalid-id constant and names a known world
   actor, `StagingSystem::…(actor_id, template data)` runs, i.e. the update
   is attributed to that actor for staging/animation purposes. Send `0` or
   the interacting player's actor id.
3. `variant`, `state`, `counter`, and the four `unknown_*` values are
   written straight onto the prop.
4. `keep_visual_state == false` sends `SwitchActiveState` with the name from
   `variant`/`state` (`inactive`, `active`, `cooldown`, or the colour name),
   which is what plays the lever/chest animation. Send `false` for a real
   state change.
5. `state_changed == true` raises `Messaging::PropStateChanged` for UI and
   quest listeners.
6. `operation_changed == true` (with a non-empty stored operation) raises
   `PropOperationProgressChanged`.
7. For interactive props the two strings and the quest values replace the
   stored ones, and the quest-guidance/highlight logic from `NewProp` runs
   again (guidance shown only when operation is non-empty and `state == 1`).

Retail's answer to a click on an `e_anniversary_dragon_egg` (§12), 53 ms
after the `PropInteractionCommand`, bit-aligned out of its batch:

```
ff 3700                                   separator, PropUpdate
0f000100                                  prop_id     65551
ffffffff                                  variant     -1
8f000100                                  actor_id    65679 (the clicking player)
03                                        state       3 (inactive)
4d010000                                  game tick   333
0                                         state_changed   false
0000 0000                                 operation "", quest_id ""
00000000                                  task_index  0
ff                                        quest_indication -1
00000000 00000000                         unknown_c0, unknown_c4
0                                         operation_changed false
00000000 00000000                         unknown_cc, unknown_d0
0                                         keep_visual_state false
00000000                                  trailing actor 0
ff                                        next separator
```

So a real state flip is: same id, `variant -1`, `actor_id` = the player,
the new `state`, the current game tick, `state_changed false`, empty
strings, `task_index 0`, `unknown_*` zero, both remaining bools `false`,
and trailing actor `0` (unlike `PropInfo`, which carries the player's
actor). `keep_visual_state false` is what plays the animation. The server
followed the update with a one-record `PropInfo` for the same prop
(`state 3`, tick `350`) and, twelve seconds later, another with `state 1`
when the egg respawned.

The `unknown_c0..d0` block is stored at `GameProp+0x160..0x16c` and is not
read by any handler in `ClientPropManager`; it most likely feeds the
operation progress bar (the PvP capture channels are the only retail
`_Template_PropOperation` users). Zero is safe.

## 7. `PropInteractionCommand` (`0x0039`, client → server)

Encoder `0x140944670`, decoder `0x14093da5c`.

```
0x8B, u16 0x0039
u32 prop_id     +0x20
str operation   +0x28   (u16 length + UTF-8)
```

No trailing actor: the encoder writes exactly these two fields, and every
client-to-server command in the 2026-09-09 capture is framed that way (§11).
The sender stores the local player's actor id in the command object, but it
is not serialized; the server identifies the player from the connection.

The sender is a `NetworkCommandCreatorProperty` handler at `0x140170428`,
reached from `HandleMessage` when the player clicks an entity:

- Only entities whose `Category` attribute is `StateObject` take this path;
  `NPC` goes to the NPC request/encounter commands and anything else logs
  `Interaction with entity of category '%s' not possible!`. Destroyable props
  (`_Template_DestroyableProp`, `NewDestroyableCommand` `0x003A`) are
  attacked, not interacted with.
- `prop_id` is the prop's id as delivered by `PropInfo`/`NewProp`.
- `operation` is the prop's stored operation string, i.e. exactly what the
  server put into `field_48` / `operation`. An empty string is sent for
  props without an operation, so the server can key on either the id or the
  string.
- After sending, the client sets a byte on the local player
  (`ClientGameWorld+0x551 = 1`), an "interaction pending" flag. The server
  is expected to answer with a `PropUpdateCommand` for that prop (and any
  loot/chest UI command the operation implies); nothing else clears the
  client's view of the prop.

Retail bytes (§12), sent when the player clicked an egg:

```
8b 3900 0f000100 0000      PropInteraction, prop_id 65551, operation ""
```

The operation string was empty because the `PropInfo` that created the egg
carried an empty operation, and the client still sent the interaction. The
server replied within 53 ms with `PropUpdateCommand` (state `3`), an
`InventoryInfoCommand` and `ItemInfo` (the egg's reward), and a one-record
`PropInfo` for the same prop.

This is the `PlayerInteractedProp` source for the signal engine
(`_Template_Signal`: 712 `ServerSignal` and 177 `ClientSignal` rows use it,
`EventParam` = the `_Instance_StateObject._ID`). The server has to map
`prop_id` back to the map row it created the prop from.

## 8. `QuestTriggerSignalCommand` (`0x0125`, client → server)

Encoder `0x140944bdc`, decoder `0x14093ded4`.

```
0x8B, u16 0x0125
atom trigger_id   +0x20
```

Sent by `ClientQuestManager` (`0x140225968`) from its `Game::TriggerEvent`
subscription: the event carries the trigger name atom and the actor id. The
actor id is copied into the command object but, as with every
client-to-server command, only the atom is serialized; the command means
"the local player entered trigger volume Y". Trigger volumes come from
`_Instance_TriggerVolume` (`Id`, `TriggeredEventCategory`, `TriggeredEventId`,
`PerPlayer`); across all maps: 449 `Quest`, 781 `Hint`, 20 `CTFTeamBase`, and
104 uncategorised volumes. `Game::TriggerVolumeEventController` is the
client-side object that watches them.

The signal engine's `PlayerHitTrigger` rows are split 210 `ServerSignal` /
211 `ClientSignal`, so retail evidently evaluated some trigger hits on the
server from positions and let the client report others. A server that
already tracks player positions can compute every hit itself and ignore this
command; it is only needed if trigger evaluation is delegated to the client.

Two companions, for completeness (both actor-scoped):

- `QuestTriggerIndicationRequestCommand` `0x008F` (C→S): `atom quest_id`,
  `u32 task_index`.
- `QuestTriggerIndicationInfoCommand` `0x0090` (S→C):
  `atom quest_id`, `i32 task_index`, then
  `[u32 count][atom trigger_id, float3 position]*`. The handler only accepts
  it when `quest_id`/`task_index` match the pending request and the actor
  is valid (`Received QuestTriggerIndicationInfoCommand from server with
  invalid actor id (%u)!`).

## 9. `NewEventCommand` (`0x00DB`) and `DiscardEventCommand` (`0x00DC`)

Layouts, from decoders `0x1409718e0` and `0x140970c84`:

```
NewEventCommand     = one EventUpdate entry (no count), no trailing actor
DiscardEventCommand = u32 event_id,                    no trailing actor

EventUpdate entry (element reader 0x140a568b8, 0x78 bytes):
    u32  event_id
    atom event_name
    bool flags[0]   active
    bool flags[1]   map_local
    bool flags[2]
    bool flags[3]
    if not flags[1]:
        i32 × 6  starts_at   (calendar fields)
        i32 × 6  ends_at
        [u32 count][atom]*  tags
```

**The client does not handle either command.** Their RTTI objects
(`0x1413992e0`, `0x141399340`) are referenced only by the command-factory
registration, the creator, and the `__ImplementClass` records, whereas
`EventUpdateCommand`'s RTTI has the extra reference from the
`ClientEventSystem` handler subscription at `0x1401f1a10`. They also have no
"called with command of wrong type" log string, and their `Create()` bodies
have no callers. They sit in the same module as `KillMapServerCommand`,
`ForkCommand`, `MapServerInfoCommand`, and `MasterServerStopped`, i.e. they
are master-server ↔ map-server traffic. Sending them to a client produces a
decoded-but-unhandled command at best.

Mid-map event start/stop therefore goes through `EventUpdateCommand`, which
the repo already encodes (`encode_event_update` in
`protocol/player_init_protocol.py`). The handler at `0x1401f19c8` gives the
flag semantics the repo's `EventUpdateEntry.flags` tuple needs:

- `flags[1] == true`: **map-local event**. The entry is matched by name
  against the map's `_Instance_Events`; a miss logs
  `Received event update command with invalid map local event (%s)` and
  rejects. `flags[0]` then starts (`ClientEventSystem::StartMapEvent`) or
  stops the map event, which is what flips `_Instance_EventSets` scene sets.
  No times or tags are read.
- `flags[1] == false`: global event. Unknown ids are created from
  `_Template_Events` (`invalid event template (%s)` rejects), known ids are
  updated with `flags[0]` as the active state, the two calendar times, and
  the tags. `flags[2]` and `flags[3]` are forwarded to two setters
  (`0x140987564`, `0x1409875d0`) that the repo currently sends as `false`;
  they look like the participation/advertised toggles but were not traced.
- When a map-local event starts, the client immediately sends
  `NPCRequestCommand` and `PropRequestCommand`, so the server's `PropInfo`
  response must already include the props the event reveals.

Retail confirmation (§12): every mid-map event in the dungeon session was
an `EventUpdateCommand` with **one entry, `event_id 0`, flags
`(active, true, false, false)`**, no times and no tags, inside an ordinary
`0x85` batch: `difficulty_extra_spawn` on each map load, then `BOSS_spawn`
and `BOSS_start` on entering the boss map, `BOSS_start` again with
`active = false`, and `BOSS_death`, `BOSS_death_dtu`, and
`ed_activates_exit` in one batch when the boss died. The names are the
`_Instance_Events.Id` values of the map. `NewEvent`/`DiscardEvent` never
appeared.

## 10. Reproducing the numbers

```
py -3.12-64 tools/flatten_decoder_x64.py --build modern Commands::NewPropCommand \
    Commands::PropUpdateCommand Commands::PropInteractionCommand \
    Commands::QuestTriggerSignalCommand Commands::NewEventCommand
py -3.12-64 tools/flatten_decoder_x64.py --diff Commands::PropUpdateCommand
```

Reader addresses that the flattener cannot name by itself: `0x140c9a694`
u32, `0x140c9a2cc` i32 (callers default the local to `-1`), `0x140c9a140`
f32, `0x140c9a394` float3, `0x140c9a1e8` guid, `0x140c9a4ac` string,
`0x140c9a568` string atom, `0x140a510c8` prop-state enum, `0x140a51eb8`
quest-indication enum, `0x140c9a634` u8, `0x140c99fd0` bool.

Handler decompilations are in `game files/commands/` (`new prop command`,
`discard prop command`, `prop update command`, `prop info command`,
`event update command`, `quest trigger indication info command`). Send sites
were found by scanning `.text` for `call` references to each class's
`Create()` body and for RIP-relative references to its RTTI object.

## 11. Retail evidence: `capture_20260909_000000_sample_08.log`

A retail session recorded with the packet logger
(`logs-handler/logs/capture_20260909_000000_sample_08.log`, outside this repo). Two
game-server hosts carried 1 569 and 1 115 application messages; the busiest
host in the file is HTTP asset traffic and irrelevant. The player started
in a city with a crafting workbench, used a travel item to
`g0012_temple_heart_01`, and never clicked a lever or chest, so the capture
contains **no** `NewProp`, `DiscardProp`, `PropUpdate`, `PropInteraction`,
`QuestTriggerSignal`, `NewEvent`, `DiscardEvent`, or `EventUpdate` message.

What it does confirm:

**`PropInfoCommand`**, eleven times, all identical, inside an `0x85` batch
with the terminator `ff`:

```
85 3600                                   batch, PropInfo
01000000                                  1 record
00000100                                  prop_id      65536
2000 "t000_state_crafting_workbench_01"   template_id
1000 7bf1ff1c e76742a1 98550be3 224c5db8  instance_guid (16 bytes, verbatim)
ffffffff                                  variant      -1
01                                        state        active
00000000                                  counter      0
c9309841 00000000 41d69940                position (19.02, 0.0, 4.81)
0fbe7bbe                                  rotation -0.2458
0000 0000                                 operation "", quest_id ""
00                                        extra byte
ff                                        quest_indication -1
ffffffff                                  task_index  -1
85010100                                  trailing actor 65925 (the player)
ff                                        batch terminator
```

`decode_prop_info`'s `_read_prop_info_body` consumes it with zero unread
bits. Every default in §2 matches, and retail sends the player's own actor
id as the trailing actor, not `0` as `_handle_prop_request` currently does.
Retail prop ids start at `0x10000`.

**Client-to-server framing.** Every outgoing command is `0x8B`, `u16` id,
then exactly the fields the decoder reads, with no trailing actor:
`PropRequestCommand` is `8b 38 00` (24 bits), `NPCRequestCommand` is
`8b 29 00` + `u32 0`, `ActorRequestCommand` is `8b 1c 00` + `u32 actor`,
`UseTravelItemCommand` is `8b 79 00` + `u32` + `str` (304 bits for a
29-character map name). This is why §7 and §8 carry no actor field.

**`StagingSignalCommand` (`0x0113`)**, eleven times, client to server:
`str signal_id`, `u8 1`, `u32 actor`. The signals were offer/staging names
such as `DeluxeOff50FirstEntrancePrincecampSignal` and
`PremiumOff50LoggedInSignal`, with the actor id inside the body
(`0x10000` on the first host, `0x10185` on the second). Unlike the
commands above, its decoder reads the actor as part of the generated body.
It is the client half of the staging/signal system that
`_Instance_TriggerVolume` and `_Template_StageTrigger` feed, and is the
likely carrier for client-evaluated staging triggers; quest triggers use
`QuestTriggerSignalCommand` instead.

**`PropRequest` timing.** The client sent it four times on the first host
and five on the second, always paired with `NPCRequestCommand`; the second
host answered every one with the full `PropInfo` list (eleven batches in
total, some re-sent). No `EventUpdateCommand` occurs in this session, so the
map-event-start resend from §3 is still only a static-analysis result.

## 12. Retail evidence: `capture_20260909_000000_sample_09.log`

A 77-second dungeon run: `a0202_kingscrypt01`, `a0203_kingscrypt02`
(both on map host `203.0.113.11`), then `a0204_boss` (`203.0.113.12`), ending
with the boss kill. Exits were taken with the client-to-server
`UnlockMapCommand` (`0x0060`, exit id string such as
`a0202_kingscrypt01_exit_to_a0203_kingscrypt02_00`). The player's actor id
changed per map (65679, 65766, 65536), and retail prop ids restarted at
`0x10000` on every map.

Almost every server command here is coalesced into `0x85` batches whose
first command is something else (`ActorsLeftVicinity`, `StatusEffect`,
`GauntletNotificationResponse`, `InventoryInfo`), so the capture parser's
per-message command id never shows them. They were found by searching each
message at all eight bit alignments for `ff` + id and decoding at that bit
position; a decode counts only when it ends exactly on the next `ff`.

| command | occurrences | notes |
| --- | --- | --- |
| `PropInfoCommand` | 38 batches | 10-record list for kingscrypt01 (altars, bodies, `fracturedplanes_rift` in state 0), 8-record list for kingscrypt02 (gargoyles), 1-record `HeredursSarcophagus` on the boss map, plus one- and two-record lists for props spawning mid-map (`e_anniversary_dragon_egg`, `e_easter_01_egg_*`, `herb_health_regeneration`) |
| `PropRequestCommand` | 32 | `8b 38 00`; the client sends it in bursts after each map load and the server answers each one, which is why the same 10-record list appears four and ten times in consecutive batches |
| `PropInteractionCommand` | 1 | `8b 39 00 0f000100 0000` (§7) |
| `PropUpdateCommand` | 1 | the egg, 53 ms after the interaction (§6) |
| `EventUpdateCommand` | 9 | all map-local, `event_id 0` (§9) |
| `StagingSignalCommand` | 19 | offer/reminder signals, as in the first capture |
| `NewProp`, `DiscardProp`, `QuestTriggerSignal`, `NewEvent`, `DiscardEvent` | 0 | searched at every alignment |

Observations that changed the text above:

- `field_28` is the map's current **game tick** (about 25 per second), not
  a counter: the egg was sent with ticks 250, 300, 333 (PropUpdate), 350,
  and 650, and two records in one command share the value.
- Props are actors: the interacted egg's id `65551` also appears in the
  `ActorsEnterVicinity` lists, and prop ids share the actor id space.
- Retail sent `PropInfo`, never `NewProp`, for props spawning mid-map, and
  never `DiscardProp` for props that vanished; the vicinity commands and
  the per-prop `PropInfo` re-sends did that work.
- Retail `PropInfo` carries the player's actor as the trailing actor; the
  `PropUpdate` carried `0`.
- The `fracturedplanes_rift` record was sent with `state 0` (inactive),
  the only non-active state in the initial lists.

