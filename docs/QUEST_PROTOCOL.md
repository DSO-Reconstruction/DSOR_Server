# Quest protocol

Wire layouts and retail behaviour of the quest commands, recovered from
`dro_client64-patch5.exe` and verified against a retail tutorial run captured
on 2026-09-09 (`capture_20260909_000000_sample_10.log`: tutorial dungeon, start hub,
garden, tomb, fortress, Grimford hub; six quests accepted, five turned in,
two chapter transitions). Every layout below decodes that capture with zero
unread bits. Wire conventions (bit packing, string and framing rules) are in
[PROP_AND_TRIGGER_COMMANDS.md §1](PROP_AND_TRIGGER_COMMANDS.md).

| Command | Id | Direction | Purpose |
| --- | --- | --- | --- |
| `NPCRequestCommand` | `0x0029` | C→S | player clicked an NPC (`u32` actor) |
| `EncounterCommand` | `0x0066` | C→S | sent together with `NPCRequest` (`u32` actor) |
| `NPCInfoCommand` | `0x0028` | S→C | the NPC's dialog: quest offers, turn-ins, talk dialogs |
| `QuestCommand` | `0x008A` | C→S | accept / turn in / cancel a quest |
| `TalkCommand` | `0x0067` | C→S | player chose a talk dialog on an NPC |
| `QuestUpdateCommand` | `0x008B` | S→C | one quest change: task progress, task finished, quest state |
| `QuestLogInfoCommand` | `0x008C` | S→C | full quest log, sent in every map initialization |
| `QuestMonsterIndicationRequestCommand` | `0x008D` | C→S | quest tracker wants target monster positions |
| `QuestMonsterIndicationInfoCommand` | `0x008E` | S→C | those positions, resent every 1.2 s while tracked |
| `QuestTriggerIndicationRequestCommand` | `0x008F` | C→S | tracker wants explore-trigger positions |
| `QuestTriggerIndicationInfoCommand` | `0x0090` | S→C | those positions |
| `QuestDisableCommand` | `0x0091` | S→C | empty body; not observed |
| `QuestTriggerSignalCommand` | `0x0125` | C→S | trigger volume entered; not observed (see prop doc §8) |
| `ChapterProgressCommand` | `0x0093` | S→C | chapter counters, resent after every quest change |
| `ChapterUnlockedCommand` | `0x0092` | S→C | a new chapter opened |
| `UnlockMapCommand` | `0x0060` | C→S | player used a map exit |

All of them are actor-scoped: server-to-client bodies end with the `u32`
actor, client-to-server bodies carry no actor. The actor retail puts on
quest commands is the **player's** actor id (it changed per map: 65557,
65538, 65574, 65564, 69624), not `0`.

## 1. Opening a dialog: `NPCRequest`, `Encounter`, `NPCInfo`

Clicking an NPC sends `NPCRequestCommand` and `EncounterCommand` in the same
tick, both with the NPC's actor id. Retail answers with `NPCInfoCommand`
for that NPC, and also pushes `NPCInfo` for every NPC in the vicinity
whenever quest state changes, so the overhead markers stay right.

```
NPCInfoCommand (0x0028)
  str   npc_id                    ("" for quest-only NPCs, "simple_smith_hub_a0002" for a merchant)
  [u32 n] offers:      str id, u8 kind, str quest_id, u8 quest_type
  [u32 n] dialogs:     str dialog_id, bool
  [u32 n] barters: str id, [u16 n] item_template_strings, u16 amount, bool reply
  [u32 n] (unobserved): str, str, u32, bool
  bool  true
  u8    category       (0xFF for quest NPCs, 5 for the smith)
  u32   actor          (the NPC)
```

`offers` is what the dialog shows and what puts the marker over the NPC.
`kind` `0` is the NPC's quest dialog: a quest it offers (the starter before
the accept, the ranger's follow-up after the turn-in), or the goal dialog
while the quest's encounter task is open or the quest is finished. `kind`
`1` marks the NPC as the target of an **open** encounter task: the ranger
got `[0, 1]` for the first tutorial quest the moment it was accepted
(16:04:10.562), only `[0]` once the encounter finished the task
(16:04:33.381), and the hub spellweaver got `[0, 1]` for the garden quest
only when its talk task was done and the complete task opened (16:08:29),
nothing at the accept (16:05:47). That marker is what makes the client walk
the player into the NPC and send `Encounter`/`NPCRequest`. `kind` `2` is a
talk-task dialog; then `id` is the dialog id (`<quest>.s_<task>talkDialog`)
and the same id appears in `dialogs` with `false`. `kind` `3` is an item
hand-in: its id names a `<Barter>` and a matching record in `barters` supplies
the acceptable item templates, combined required amount, and reply-dialog flag.
The trailing byte is the authored quest type, also used in the prop record's
`field_5c`. The tutorial capture only had world/main-story quests (`6`);
side quests use `5`. Plain props use `-1`. The full native mapping is in
`gameplay/quest_content.py::QUEST_TYPE_IDS`. An NPC with nothing to say gets an
empty `offers` list. The barter and final arrays were empty in that capture.

Retail's timing: the `NPCInfo` reply came 30 to 70 ms after the request,
and the client showed the dialog only after receiving it.

Map initialization also includes the eligible quest offers in each NPC's
`NPCInfo`, so map markers are available before opening a dialog. Resolve
those records using `local_npc_actor_id`, the same actor assigned to `NewNPC`.
Arena merchants use `0x20000 + row_id`; looking them up by the static
`definition.actor_id` silently drops their initial PvP offers. Regression
coverage in `tests/test_quest_map_markers.py` compares initial packets with
NPC-request replies in two hubs, including level and repeatable-quest gates.

## 2. `QuestCommand` (`0x008A`, client → server)

```
0x8B, u16 0x008A
str   quest_id
u8    operation        0 accept, 1 turn in, 2 cancel
u8    argument         always 0 in the capture
```

The client has three senders (`0x1401717d5` writes 0, `0x14016f419` writes
1 and copies a byte into `argument`, `0x14016f24d` writes 2). The capture
shows 0 at every accept and 1 at every turn-in; 2 is the remaining sender,
which corresponds to the client's `CancelQuest` message. `argument` is only
set on the turn-in path; it is most likely the reward choice index and was
`0` for the tutorial quests, which offer no choice.

Retail sequence for a turn-in that offers a follow-up: `QuestCommand(1)`,
then 550 ms later `QuestCommand(0)` for the follow-up. The dialog stayed
open in between.

## 3. `QuestUpdateCommand` (`0x008B`, server → client)

The command is a change record keyed by the client's `QuestChange::Param`
enum: a subject byte, integer parameters, string parameters.

```
0x84/0x85, u16 0x008B
u8    subject
[u32 n] ints:   u8 key, u32 value
[u32 n] strs:   u8 key, str value
u32   actor            (the player)
```

| subject | meaning | parameters observed |
| --- | --- | --- |
| `0` | task progress | ints `1` task index, `3` progress, `4` target; str `0` quest id |
| `1` | task finished | ints `1` task index, `3` progress, `4` target; str `0` quest id |
| `2` | quest state changed | int `2` state; str `0` quest id, str `6` timestamp |
| `3` | no parameters; sent after every quest-prop interaction | none |
| `4` | accepted by the handler (`invalid subject type` above 4) | not observed |

The handler at `0x140221a1c` reads exactly those keys: subject 0 and 1 read
str 0 and ints 1, 3, 4 and raise `SetActiveQuestTaskProgress` /
`SetActiveQuestTaskFinished`; subjects 2 to 4 read int 5, str 0, str 6, and
int 2, then switch on the state. Key 5 was never sent.

**Quest state values** (`QuestState` in the client: `InvalidState`,
`QuestAcpAdded`, `QuestFinished`, `QuestClosed`, `QuestFailed`; the handler
rejects values above 4):

| state | observed when | timestamp string |
| --- | --- | --- |
| `0` | right after `QuestCommand(0)`: quest accepted | `0000-00-00 00:00:00` |
| `1` | last task finished: quest ready to turn in | `0000-00-00 00:00:00` |
| `2` | right after `QuestCommand(1)`: quest closed | completion time, `YYYY-MM-DD HH:MM:SS` UTC |
| `3` | server cancellation: remove the quest and its guidance | `0000-00-00 00:00:00` |
| `4` | authored failure; offer again and reset progress on fresh acceptance | `0000-00-00 00:00:00` |

Retail's sequences, from the capture:

- **Accept**: one `QuestUpdate(2, state 0)`, then `ChapterProgress`, then
  `NPCInfo` for every nearby NPC.
- **Collect / kill progress**: `QuestUpdate(0)` per pickup with `progress`
  counting up to `target` (8 roots collected, `[1,0],[3,1..8],[4,8]`).
- **Interact task**: `QuestUpdate(0)` for the interact task **and** an empty
  `QuestUpdate(3)` in the same batch, plus the prop's own `PropUpdate`.
- **Task done**: `QuestUpdate(1)` with the final counter; when it was the
  last task, `QuestUpdate(2, state 1)` in the same batch.
- **Turn in**: `QuestUpdate(2, state 2)` with the timestamp, then
  `ChapterProgress`, then `NPCInfo` refreshes; the follow-up's accept
  arrives as its own `QuestUpdate(2, state 0)` 500 ms later.
- **Talk task**: after `TalkCommand`, `QuestUpdate(1)` for that task index.

Task indices follow the quest's `<Task>` order in `quests.xml`; the
tutorial-02 quest finished tasks 0, 1, 2 in order, WayToGrimmagstone-02 has
five tasks (collect, interact, interact, talk, talk).

## 4. `QuestLogInfoCommand` (`0x008C`, server → client)

Sent inside every map initialization batch, after `ChapterProgress`, and
before the map's `EventUpdate`s. The `NewPlayer` batch of a fresh character
carried an empty list.

```
0x85, u16 0x008C
[u32 n] quests:
    str   quest_id
    u32   0            (never non-zero in the capture)
    u8    state        (0 accepted, 2 closed; same values as QuestUpdate)
    u16   finished_tasks   bitmask, bit i = task i done (0x1f = five tasks)
    [u32 16] progress  one counter per task slot (MaxTasksCount is 16)
    u32   0            (never non-zero)
    str   timestamp    completion time, or "0000-00-00 00:00:00"
u32   actor
```

Closed quests stay in the list with their completion time; the tutorial run
ended with seven entries. An in-progress quest carried `finished_tasks`
`0x7` and `progress[0..1] = 8, 5` while its later tasks were still open.
The handler logs `duplicate quest info` and `invalid quest info`, so ids
must be unique and known to the client's `quests.pbxml`.

## 5. Tracker indications

`QuestMonsterIndicationRequestCommand` (`0x008D`, C→S): `str quest_id`,
`u32 task_index`. Sent once when the player tracked the collect quest.

`QuestMonsterIndicationInfoCommand` (`0x008E`, S→C):

```
u32   tick             (map game tick, 25 Hz: 2455, 2485, 2515 ...)
str   quest_id
u32   task_index
[u32 n] monsters: u32 actor_id, str template_id, float3 position
u32   actor            (the player)
```

Retail resent it every 1.2 s (30 ticks) for as long as the tracker was
open, with the list shrinking as the target monsters died (6 → 5 → 4).

`QuestTriggerIndicationRequestCommand` (`0x008F`) has the same request body;
`QuestTriggerIndicationInfoCommand` (`0x0090`) is `str quest_id`,
`u32 task_index`, `[u32 n]{str trigger_id, float3 position}`, actor. Neither
occurred in the capture (the tutorial has no explore task).

## 6. `TalkCommand` (`0x0067`, client → server)

`u32 npc_actor`, `str dialog_id`. The dialog id is the `offers[].id` of
kind 2 from `NPCInfo`, e.g.
`a0002-WayToGrimmagstone-02-n.s_a0002-WayToGrimmagstone-02-talkDialog`.
Retail answered with `QuestUpdate(1)` for the talk task 1.3 s later (the
dialog text played first).

## 7. Chapters

`ChapterProgressCommand` (`0x0093`): two lists `[u32 n]{str id, u16, u16}`,
`active` then `done`, plus actor. Retail sent
`active = [("act_00_chapter_10", 7, k)]`, `done = [("act_00", 1, 0)]`, with
`k` counting turned-in quests 0 → 6; so the pair is (total, completed). It
follows every accept and turn-in.

`ChapterUnlockedCommand` (`0x0092`): `str chapter_id`, actor. Sent once,
`act_10_chapter_10`, right after the Grimford quest was accepted, and the
client immediately raised `StagingSignalCommand
("act_10_chapter_10_start_signal_01")`.

## 8. Map exits and the map-local events on the way

`UnlockMapCommand` (`0x0060`, C→S): `str destination_map`, `str
destination_access_id`, `str exit_id`, `bool false`. Retail sent it when the
player stepped on an exit (`a0002_start_hub`, `a0002_start_hub`,
`exit_general_01`), and the map switch followed 150 ms later.

Quest props are ordinary props (see the prop doc): the interact targets came
down in `PropInfo` with the operation string
`<quest_id>_<task_title>`, for example
`a0002-WayToGrimmagstone-02-n_a0002-WayToGrimmagstone-02-collect-02`, and
the client echoed exactly that string in `PropInteractionCommand`. That is
how the server knows which quest task an interaction belongs to. Those
records had state `1`, quest indication `6`, an empty quest id and task
`-1` (six `a0003_WayToGrimmagstone_02_bag_01` / `_bookstand_01` props,
one `PropInfo`, sent with the garden map's initialization).

The tutorial's scripted gates all ran through map-local `EventUpdate`s:
`a0001_start_dun_undead_mage_01..03` as each wave was cleared,
`a0001_start_dun_exit_01` on the last turn-in, `a0002_start_hub_01/_01_B/02/03`
as hub quests advanced, `a0003_start_garden_dark_ritual_01/02`, and
`a0005_start_fortress_01..04`, `_dun_show_khalys`, `_npc_01`. Those are the
quest-conditioned exit gates measured in the feasibility notes; the signal
rules that start them need quest state (`QuestAccepted`, `QuestSolved`,
`PlayerEndQuestTask`), which is what the quest service has to provide.

## 9. Implementation

Implemented on 2026-09-09 (retail flow of §1 to §8 replayed in
`tests/test_quests.py`):

| piece | where |
| --- | --- |
| quest definitions | `data/quests.xml` (copy of the decoded client file; `DSO_QUESTS_FILE` overrides), parsed by `gameplay/quest_content.py` into a `QuestCatalog` |
| task engine | `gameplay/quest_content.py`: every task is a token machine (`<Event>` → `<Return>` tokens under `<Guard>`s, task done when its `has` guard holds); `apply_quest_event` returns the counter changes the client needs. A task opens once its `preTasks` are finished; `stagedTasks` is a display hint only (retail progressed the staged garden quest's three collect tasks interleaved, 16:06–16:07 of the tutorial capture, and sent all their props with the map) |
| codecs | `protocol/quest_protocol.py` (byte-identical to the capture for QuestUpdate and QuestCommand) |
| service | `services/quests.py` (`QuestService` mixin): NPC offers, accept / turn-in / cancel, progress, rewards, tracking, chapter progress, log snapshots |
| event eligibility | `QuestCatalog.available` enforces the authored `event` attribute for NPC offers and acceptance, including signal-started quests; `QuestService._quest_active_events` reads the current schedule plus running map-local events |
| persistence | `AccountState.quest_log` (quest id → progress document), restored at map init |
| hooks | NPC request (encounter + offers), kills and cleared regions, pickups (via the presence callback), prop interactions (switch id, sent as the prop's operation string), trigger volumes (`TriggeredEventId`), fired map signals (self-starting quests) |
| event-set NPCs | NPCs whose `_Instance_NPC.EventSetRow` belongs to a map-local event are created with `NewNPC` + `NPCInfo` when that event starts (`_spawn_event_npcs`), as retail did for the tutorial's spellweaver when `a0001_start_dun_undead_mage_03` ("let npc appear") started at 16:05:23; late joiners get them with the map initialization |
| quest-controlled props | a prop a task `<Switch>` names is published **per player**, only while that quest is accepted, the task is open and the switch guards hold (`_quest_prop_operation`); accept, progress, turn-in and cancel reveal (`PropInfo`) or discard (`DiscardProp`) them (`_quest_sync_props`). Kingshill's Halloween clue boxes therefore stay client-side scenery for a player without `k5001-a-haunted-tale` |
| signal engine | `SignalRule` now evaluates `QuestAccepted` / `QuestSolved` / `QuestTaskDone` and their negations from the character's log; the service raises `PlayerOpenedQuest`, `PlayerEndQuestTask`, `PlayerClosedQuest` |

Ground quest `<Drop inventory="false">` items now use personal world loot and
credit only their owning quest on successful pickup; see [QUEST_DROPS.md](QUEST_DROPS.md).
The earlier inventory/drop, pay, spawn, defense, and status-reward gaps have
subsequent implementations described below. Defense configurations still need
valid exported maps, placements, and templates. Monster indications are sent
on request and on each change rather than every 30 ticks; quests with a giver
ignore their `startSignal` (only giver-less quests self-start).

### Quest categories and inventory delivery (2026-09-09)

NPC offers, encounter targets, talk/delivery options, turn-ins, and quest prop
markers now carry the authored type instead of always using main-story `6`.
Native `ServerQuestManager::LoadQuestsFromXML` (`0x1409a7a04`) maps types to:
repeatable 0, heroic-repeatable 1, event-repeatable 2, PvP-repeatable 3,
row-repeatable 4, local 5, world 6, heroic 7, event 8, PvP 9, crafting 10,
crafting-repeatable 11, wanted-repeatable 12, treasure-repeatable 13.
Quest progress states (accepted/finished/closed) are a separate enum.

All 379 authored `<Barter>` definitions are parsed, including prerequisites,
alternative item templates, counts, `reply`, and `onlyShow`. `NPCInfo` carries
both the delivery choice and its barter record, allowing the client to check
its current inventory. There is no requirement to have bought the item after
accepting: an already-owned item also qualifies. A completed vendor purchase
refreshes nearby NPC info. Map entry and NPC requests reconstruct the same
choices from the saved quest state.

`DeliverCommand` is `0x8B, u16 0x0068, u32 npc_actor, str barter_id` (native
writer `0x140940548`, reader `0x14093a2e8`). The native NPC menu builder
checks interaction kind 3, resolves its barter record, and enables the hand-in
button only when carried inventory meets the amount. The server checks the active local
NPC, open task and barter guards, and carried inventory. Banked, equipped,
and socketed items do not count. It combines alternative templates and stacks,
consumes exactly the authored amount, then commits inventory and quest tokens
together under the session lock. `onlyShow` tasks verify ownership and keep
the items; a full authoritative inventory record repairs client prediction.
Stale, repeated, unknown, or underfunded hand-ins cannot grant progress or
consume additional items. The quest still uses its normal reward turn-in.

`<Return amount="N">` now credits N tokens. Progress counters select the task's
own event token, rather than a preceding prerequisite guard. This matters for
bulk deliveries such as showing the crafting NPC 20 herbs.

`tests/test_quest_delivery.py` covers all quest-type mappings, side-quest props,
NPC barter wire records, real shop purchase and delivery of `gem_ruby_a` for
**Flawed Ruby**, pre-owned gems, partial/alternative stacks, show-only tasks,
invalid/repeated hand-ins, and restored completion. Live client interaction
still needs verification. Picked-up ground quest drops are now inventory items
and can satisfy these hand-ins. Direct-inventory quest grants that remain virtual
are still outside this delivery implementation.

### Event quest availability

The catalog contains 414 quests with `event` restrictions, naming 70 distinct
`_Template_Events` IDs. Semicolon-separated alternatives mean **any** matching
active event; whitespace and case are normalized (two Halloween quests have
trailing spaces in their event IDs). The quest type alone is not an event gate:
three restricted quests use the ordinary `repeatable` type.

Offers and acceptance use the current `data/events.json` schedule (or
`DSO_EVENT_SCHEDULE`), including active event tags. Upcoming, expired, and
unscheduled events do not enable quests. Direct `QuestCommand` acceptance and
giver-less signal starts share this check. Schedule edits and timestamp
boundaries take effect at the next offer/accept evaluation, without relogging;
NPC markers update through the existing NPC refresh/map initialization paths.
Map-entry snapshots must not keep an expired scheduled event available.
Running local events are also included: `e-bonus-hh-n` requires the local
`e_bonus_event_01`, rather than a scheduled global event.

This gate controls new acceptance, including repeatable quests. Already
accepted progress is retained and can still progress or be turned in when its
NPC/objectives are reachable; expiry does not cancel or reset quests.
`tests/test_quest_events.py` covers schedule boundaries, overrides/reloads,
alternative IDs, tags, local event start/stop, automatic starts, repeatables,
and preserving existing progress.

### Tutorial map handoff and NPC phases

The full `capture_20260909_000000_sample_10.log` confirms tutorial 3/3 is an
**encounter** with `a0002_start_hub_spellweaver`, the original hub Vithrandir.
At 16:05:34.550 the player requests `a0002_start_hub`; at 16:05:40.213 the
client sends `NPCRequest` (0x0029) and `Encounter` (0x0066) for actor 65536.
At 16:05:40.264 retail replies with task completion and finished quest state;
the turn-in follows at 16:05:40.964. The `_01` NPC variants belong to later
hub phases (the capture returns from the garden at 16:08:19.895).

Two shared map-state rules must agree with that sequence:

- Initial population and the event runtime both require **all** `=True`
  flags and the absence of **every** `=False` flag. The original hub NPCs
  belong to event set 11, which has only negative conditions; later phases
  use sets 13 and 15.
- Signal `MapUnlocked` / `MapNotUnlocked` conditions read persisted character
  discoveries (`AccountState.unlocked_maps`). Map entry records the physical
  map, access id and access group. Development fast-travel unlocks do not
  count as discoveries. In particular, `a0002_start_hub_04` cannot replace
  Vithrandir on the first arrival: it requires the fortress map to have been
  discovered. The garden-return phase also requires both its quest and the
  garden discovery.

Regression coverage in `tests/test_quests.py`, `tests/test_map_events.py`
and `tests/test_hub_travel.py` covers all four classes, the paired encounter
packets, turn-in/follow-up, later phases, travel/relog persistence and legacy
saves. Legacy saves without discovery history retain their saved location
and the current destination; earlier visits cannot be reconstructed.

## 10. What a server has to do

1. On map init, send `ChapterProgress`, `QuestLogInfo` (all known quests,
   closed ones included), then the map's events.
2. On `NPCRequest`/`Encounter`, answer `NPCInfo` with offers (kind 0),
   turn-ins (kind 1) and talk dialogs (kind 2) computed from `quests.xml`
   givers/goals, `required`/`followup` chains and the player's log.
3. On `QuestCommand(0)`: add the quest, send one batch of
   `QuestUpdate(2, state 0)`, `NPCInfo` for every live quest NPC, then
   `ChapterProgress` (retail 16:04:10.562); raise `PlayerOpenedQuest` for
   the signal engine.
4. Feed task progress from the existing event sites (kills, pickups,
   `PropInteraction` with the quest-scoped operation string, `Talk`,
   trigger volumes) as `QuestUpdate(0/1)`; when the last task finishes send
   `QuestUpdate(2, state 1)`; raise `PlayerEndQuestTask`.
5. On `QuestCommand(1)`: grant rewards through the existing inventory and
   currency paths, send `QuestUpdate(2, state 2)` with the UTC timestamp,
   `ChapterProgress`, `NPCInfo`s; raise `PlayerClosedQuest`; offer the
   follow-up.
6. Answer indication requests while a quest is tracked, every 30 ticks.
7. Persist the log in the shape of §4.

Open points: the meaning of `QuestCommand.argument`, subject `4`, int key
`5`, the two zero `u32`s in the log entry, and the `6` byte in `NPCInfo`
offers are not observable from this run; a quest with a reward choice, a
failed quest, and an explore task would settle them.

## 11. Achievements and chapter notifications

Achievements ride on `Achievements::AchievementInfoCommand` (0x0114), the
same aggregate the map initialization carries. Retail sends **one record
per command**, chained in `0x85` batches:

```
u32 count = 1
  string   achievement id            (_Template_Achievements.Id)
  u32      progress                  (Amount reached so far)
  u32 × 8  completion stamp: year, day, hour, minute, second, 0, ISO weekday, month
           (all zero while the achievement is open)
u32 actor (local player)
```

Tutorial run: `skin_unlock_amount_01 .. _09` counted up as progress-only
records, and `lore_enter_grimford_01` arrived as
`(1, 2026, 26, 13, 14, 58, 0, 6, 9)` in the Grimford initialization batch
(26 2026-09-09 was a Saturday, weekday 6). The client raises its
achievement popup from the completion stamp, so a record must be repeated
with the **same** stamp on every later login. `_Template_Notifications`
has no achievement or chapter kind; nothing else is sent.

`ChapterUnlocked` (0x0092, one string) accompanied the first quest accepted
in a chapter and never repeated for the chapter afterwards.

### Implementation

| piece | where |
| --- | --- |
| definitions | `_Template_Achievements` (EnabledLive rows) through `gameplay/achievement_content.py`; `Param` tokens are matched against the event value, `Amount` is the target, `Prerequisites` gate completion, `Points` feed the score rows |
| engine | `apply_achievement_event(kind, values, count)` for `Kill`, `Enter_Map` (map id, access group, access id), `Interact` (prop instance and template), `Pick_Up`, `Quest`, `Class_Level`, `ClearDungeon`; `Points` (`score`) and `UnlockAchievements` are recomputed after every completion |
| service | `services/achievements.py` (`AchievementService`): one record per change, sent only when progress or completion changes; completion stamps written once |
| persistence | `AccountState.achievements` (id → progress, completed_at) and `AccountState.chapters_unlocked`; restored with the quest log at map init |
| initialization | `_achievement_init_info` replays every persisted record; the recipe-gate achievements (`load_recipe_unlock_achievements`) are completed once on first login instead of being re-stamped with "now" on every login |
| chapters | `_chapter_unlock_commands` emits `ChapterUnlocked` only when the chapter is not in the persisted set, and never for the tutorial chapter `act_00_chapter_10`: retail's only announcement was `act_10_chapter_10`, and the client ships `notificationchapter_*` title textures for every other chapter but none for act 0 (announcing it just logs texture-load warnings) |
| hooks | map entered, monsters killed, prop interaction, item pickup (presence callback), quest turn-in, level-up (`_award_experience` and instant XP consumables via the presence callback) |

Not fed yet: `Crafting`, `ItemKill`, `EquipItem`, `EquipRarity`, `Use`,
`UnlockSkin`, `Stickerbook`, `Pvp_Kill`, `Pvp_Match`, `EventProgressCollected`,
`VeteranLevelUnlock`, `GuildMembers`, `CollectedGameCurrency`, and the
`Kill` rows whose parameter is `nomob`; `ClearDungeon` has a hook but no
caller until dungeon completion timing exists. Their persisted state is
kept and replayed, so adding a feed later needs no migration.


### Dungeon quest lifecycle (2026-09-09)

All 40 authored `boundToMap` flags are parsed. Eligible entry-triggered dungeon
quests start from authoritative map/access-group, level, prerequisite and event
facts, including entry signals the client normally reports. Repeated entry
reports retain current progress. Direct client acceptance cannot bypass the
start signal of an automatic dungeon quest.

A map-bound quest with no goal NPC closes automatically after its final task.
Task completion signals run first, revealing authored reward props; closure
then grants the normal currency, XP and loot rewards and emits `PlayerClosedQuest`.
Map-bound quests with a goal NPC still require that turn-in. Completion is
remembered per character and map instance, preventing another start signal from
paying the same run twice. A fresh instance permits repeatable challenges again.

Validated map transfers, character selection and disconnect cancel unfinished
map-bound quests. State `3` is the native client cancellation update: the handler
at `0x140221a1c` removes progress, clears tracking and shows `questCanceled`.
Manual cancellation still respects `deletable`; server lifecycle and authored
`stopSignal` cancellation do not require a manual-delete button. Ordinary quest
progress and closed history are preserved.

Persisted active runs carry a unique map-instance identifier. Stale runs from an
old instance, server restart or older save are removed before the initialization
quest snapshot. An explicit onward handoff to an open task with one authored map
preserves a multi-map quest, such as a parallel-world dungeon chain. A task with
several alternative maps (Bloodshed and sentinel challenges) remains one run in
its current dungeon; travelling to another listed dungeon cancels that run.

**A Bloody Reward** (`bloodshed-challenge-01-n`) starts on eligible Bloodshed
access groups at level 55+. Its clear-region task checks the actual living
Bloody Sludge population, including actors outside the player's visible set.
Group-enabled clear-region tasks update for every participant sharing the same
map runtime and combat population, even if the final killer has no active quest.
Lethal monster-versus-monster impacts also trigger this population check without
assigning personal kill credit. Shared-instance entry and monster-tracker requests
reconcile already-cleared populations; empty spawn tables cannot complete a task.
The tracker includes remaining clear-region monsters, uses their current server
positions, and refreshes on each relevant kill even before the task completes.
Clear-region monster lists are treated as multiple template IDs, so other dungeon
challenges wait for every listed kind to be eliminated.

The existing authored reward events produce five chests before 30 seconds,
four from 30 seconds, three from 60, two from 90, and one from 120 onward.
Timers begin on the instance's first eligible entry and are shared by later
joiners. Signal evaluation matures elapsed deadlines before checking reward
conditions, even if the timer callback is delayed or its original session left.
Chests already earned are not removed when later deadlines pass.

`dynamicXP` now uses the native `CalculateDynamicXPReward` formula
(`0x1409a359c`): the authored percentage of the current XP level's span, multiplied
by `QuestExperience`, rounded half-up and capped at remaining XP to level 100.
Native constants `0x140e352b4` and `0x140e4781c` are 100 and 0.5. Premium quest
bonuses are separate from monster `XPGain`. Bloodshed's value `1` awards 1% of a
level before quest bonuses; level-100 characters receive no dynamic XP.

`tests/test_dungeon_quests.py` covers entry eligibility, duplicate signals, actual
sludge kills, every reward boundary, delayed callbacks, cancellation wire data,
validated and rejected transfers, logout, stale saves, multi-map handoff, repeat
instances, late joiners, clear-region lists, shared and non-player final kills,
tracker recovery and dynamic XP. Live client validation
is still required for the new cancellation/completion flow.

### Defense encounters and status rewards (2026-10-26)

Current `dro_client64.exe` confirms NPC interaction kind **5** selects a
HoldGround (`0x1404af866`). Its source is the full authored binding id and its
target is the quest id. `DefendCommand` uses the normal command header followed
by `u32 npc_actor_id` and a u16-length UTF-8 `holdground_id` string (vtable
`0x14117a510`, serializer `0x140980100`, sender `0x140176284`). The server checks
quest/task guards, map, active NPC identity, and an eight-metre interaction range.

`<HoldGround>` retains its NPC, guards, and difficulty configs. An empty
difficulty is native wildcard -1 (`0x1409edf9c`), represented by `None` in the
Python model; exact tiers take precedence. `_Template_HoldGround` supplies the
defender and up to nine timed waves. Each wave token binds a map position marker
to a monster template; repeated tokens produce distinct enemies. Intermediate
empty waves preserve authored delays. Final success requires the last delay
and every owned wave enemy to be defeated while the defender remains alive.

Native `<Failure/>` marks the quest failed, state **4** (`0x1409eceec`;
acceptance/reset at `0x1409e0604`). Reacceptance replaces the old counters/tokens.
Runtime encounters are not saved as running fights: cancellation, disconnect,
map replacement, and owner/defender death stop their actors and timers. Earned
quest progress is saved, and stale callbacks cannot complete another attempt.
Group credit uses eligible same-instance attempts captured at encounter start.

The actual resolver supports 49/85 authored defense tasks (49/78 live tasks),
with 48 supported for all declared map/config variants. Missing exported maps
and broken authored references remain listed in the feature-gap audit. Supported
world defenses wait for DefendCommand; only unavailable story setups retain
the earlier automatic-success compatibility fallback.

All 16 authored status-reward rows resolve seven native effects. The reward
transaction stages quest closure with its XP, currency, items, and effects,
saves before publication, and retains native exclusivity, clock, expiry, and
KeepOnDeath behavior. Full bags or invalid content leave the quest finished
and ready to retry. A transport failure does not skip accepted rewards, vitality
changes, or achievement updates. Existing closed quests are not paid again.
