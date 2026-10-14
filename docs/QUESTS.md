# Quest System

How Drakensang Online quests are defined, triggered, and completed.

Everything here was derived from the shipped client data:
`export_win32/quests/quests.pbxml` (1758 quests, decoded with
[pbxml_to_xml.py](pbxml_to_xml.py)) cross-referenced against
`data/db/static.db4` and the per-map `data/maps/*.db4`. Claims about structure
were checked against the whole corpus rather than sampled — where a rule holds
without exception, the exception count is stated. Where the data does not
answer a question, this document says so instead of guessing.

---

## 1. Where the data lives

| What | Where | Format |
| --- | --- | --- |
| Quest definitions | `export_win32/quests/quests.pbxml` | PACK archive of BXML documents |
| Quest name index | `export_win32/quests/quests.freg` | plain text, `<questId> quests.pbxml` per line |
| Item / monster / NPC / signal templates | `data/db/static.db4` | SQLite |
| Per-map object instances (props, trigger volumes) | `data/maps/<map>.db4` | SQLite, `_Instance_*` tables |
| Quest text (titles, descriptions) | `export_win32/language/<lang>/db.quest*.xml` | XML locale tokens |

`quests.pbxml` holds one BXML document per quest. Each document's root is
`<Quests>` wrapping exactly one `<Quest>`. The pack entry name equals the quest
id in 1751 of 1758 cases; the 7 exceptions are authoring typos (e.g. pack entry
`test-HoldGround` contains id `Test-HoldGround`) and are preserved as a
`packEntry` attribute by the converter.

The binary container format is documented in the module docstring of
[pbxml_to_xml.py](pbxml_to_xml.py).

---

## 2. Document shape

```
Quests
└── Quest                     (1758)
    ├── Reward                (11133)
    ├── Drop                  (156 — quest-level grants)
    └── Task                  (5285)
        ├── Guard             — preconditions (before <Return>)
        ├── Return            — the task's completion flag
        ├── Guard             — completion conditions (after <Return>)
        ├── <offer element>   — Switch / Dialog / Barter / HoldGround / ClearRegion / Pay
        │   └── Guard         — when this offer is available
        └── Event             — credit rules
            ├── Guard         — when this event counts
            ├── Return        — flag/counter it increments
            ├── Drop          — items it spawns
            ├── Spawn         — monsters it spawns
            └── Failure       — marks the task failed
```

Element totals across the corpus: `Guard` 27896, `Return` 19203, `Event`
14003, `Reward` 11133, `Drop` 6185, `Task` 5285, `Switch` 1017, `Dialog` 433,
`Barter` 379, `Spawn` 322, `Config` 299, `ClearRegion` 138, `HoldGround` 85,
`Failure` 85, `Pay` 19.

The format has no element text — everything is attributes.

---

## 3. The flag machine

This is the whole engine. Quest progress is a **per-quest bag of named
counters**. Three primitives operate on it:

- **`<Return value="F" [amount="N"]/>`** — increment counter `F` (by `N`,
  default 1).
- **`<Guard type="has|hasnot" value="F" [amount="N"]/>`** — passes when `F >= N`
  (`has`, default `N`=1) or `F < N` (`hasnot`).
- Everything else decides *when* those fire.

The flag namespace is per-quest and self-contained. Across all 1758 quests,
exactly **one** guard references a flag that no `Return` in the same quest
produces — and that flag
(`k0001-foolish-enterprise-interactInteract`, in quest `k0001-foolish-enterprise`)
is produced by no other quest either. It is a dead guard: the task it gates can
never complete. Treat it as an authoring bug in the shipped data, not as
evidence of a cross-quest mechanism.

Otherwise there are no cross-quest flag reads — quest-to-quest dependencies use
`required` and the signal system (§6) instead.

### Guard placement is meaningful

Within a `<Task>`, guards before the `<Return>` and guards after it play
different roles, and the split is exact:

- **Before `<Return>` — preconditions.** For all 2802 tasks that declare
  `preTasks`, the set of pre-`Return` guard values is *exactly*
  `{<preTask>_fin}`. **0 mismatches.** The `preTasks` attribute is a
  denormalised copy of these guards, presumably so the client can grey out
  not-yet-available task text without evaluating the flag bag.
- **After `<Return>` — completion conditions.** All 5115 post-`Return` guards
  reference a counter belonging to the task's own title. **100%.**

### The `_fin` convention

Every task has exactly one direct `<Return>`, and its value is always
`<task title>_fin`. **5285 of 5285 tasks, no exceptions.** So:

> A task completes when its post-`Return` guards pass; completing it sets
> `<title>_fin`; downstream tasks gate on that flag.

Counters are named `<task title><suffix>` where the suffix is task-type
specific (see §4).

### Worked example

```xml
<Task title="a0001-start-tutorial-02-kill-01" tasktype="KillTask"
      preTasks="a0001-start-tutorial-02-explore-01" group="True">
  <Guard type="has"    value="a0001-start-tutorial-02-explore-01_fin"/>   <!-- precondition -->
  <Return              value="a0001-start-tutorial-02-kill-01_fin"/>      <!-- sets on completion -->
  <Guard type="has"    value="a0001-start-tutorial-02-kill-01Kill" amount="1"/>  <!-- completion -->
  <Event type="kill"   value="a0001_champion_anderworld_creature_openexit">
    <Guard type="has"    value="a0001-start-tutorial-02-explore-01_fin"/> <!-- task is live -->
    <Guard type="hasnot" value="a0001-start-tutorial-02-kill-01_fin"/>    <!-- not already done -->
    <Return              value="a0001-start-tutorial-02-kill-01Kill"/>    <!-- credit the kill -->
  </Event>
</Task>
```

Read it as: once the explore task is done, kills of that monster increment
`…Kill`; at 1 the task completes and sets `…_fin`; the `hasnot …_fin` guard
stops further kills from over-crediting.

### Two-stage collection

Collect tasks chain two events — the kill drops an item, picking it up counts:

```xml
<Task title="…-collect-01" tasktype="CollectTask">
  <Return value="…-collect-01_fin"/>
  <Guard type="has" value="…-collect-01Collected" amount="8"/>
  <Event type="kill" value="a0003_gen_thing_roots_q_WayToGrimmagstone_02">
    <Guard type="hasnot" value="…-collect-01_fin"/>
    <Return value="…-collect-01Kill"/>
    <Drop chance="1" value="quest_a0002_WayToGrimmagstone_02_wood" amount="1" inventory="false"/>
  </Event>
  <Event type="collect" value="quest_a0002_WayToGrimmagstone_02_wood">
    <Return value="…-collect-01Collected"/>
  </Event>
</Task>
```

`Drop@inventory` decides whether the item hits the ground (`false`, 5684 of
6185) or goes straight to the bag (`true`, 501). `Drop@chance` is a
probability in `0..1`.

---

## 4. Task types

13 task types. Each pairs a *credit event* with an optional *offer element*
that makes the interaction available in the world:

| tasktype | n | offer element | event type(s) | completion counter |
| --- | ---: | --- | --- | --- |
| `CompleteTask` | 1668 | — | `encounter` | `Interact` |
| `CollectTask` | 846 | `Switch` | `kill`, `collect`, `interact` | `Collected` |
| `KillTask` | 688 | — | `kill` | `Kill` |
| `InteractTask` | 592 | `Switch` | `interact` | `Interact` |
| `TalkTask` | 433 | `Dialog` | `talk` | `DialogTalked` |
| `DeliveryTask` | 379 | `Barter` | `deliver` | `DeliveredLoot` |
| `ExploreTask` | 370 | — | `trigger` | `_<triggerId>Explored` |
| `ClearRegionTask` | 138 | `ClearRegion` | `regioncleared` | `ClearRegion` |
| `HoldGroundTask` | 85 | `HoldGround` | `holdgroundsucceeded` / `holdgroundfailed` | `HoldGround` |
| `CraftingTask` | 27 | — | `crafting` | `Crafted` |
| `VictoryTask` | 27 | — | `victory` | `BattleWon` |
| `PayTask` | 19 | `Pay` | `pay` | `PayedCurrency` |
| `BattleTask` | 13 | — | `battle` | `BattleJoined` |

The offer elements declare the world-side affordance and carry their own
guards controlling when it appears:

- **`Switch`** — an interactable prop (`@prop`), optionally consuming an item
  (`@item`, `@amount`, `@consume`).
- **`Dialog`** — a talk option on NPC `@actor`.
- **`Barter`** — a hand-in on NPC `@actor` of `@item` × `@amount`.
  `@reply` controls the reply dialogue; `@onlyShow` verifies ownership without consuming items.
- **`Pay`** — a currency hand-in (`@currency`, `@amount`) — 16 of 19 use
  `materi_fragment_01`.
- **`HoldGround`** — a defend-the-NPC encounter around `@actor`, configured by
  child `<Config value=… difficulty=…/>` rows, one per difficulty tier 0–9.
- **`ClearRegion`** — clear `@monster` (a `;`-separated list) on `@map`.

`<Failure/>` appears only inside `holdgroundfailed` events (85 of 85) and marks
the task failed rather than progressed.

### Task attributes

| attribute | n | meaning |
| --- | ---: | --- |
| `title` | 5285 | task id; the root of every flag name |
| `tasktype` | 5285 | see table above |
| `map` | 5285 | map the task is performed on |
| `showGuidance` | 5285 | show waypoint/marker (`True` 4879) |
| `endSignal` | 3883 | signal(s) emitted on completion; `;`-separated |
| `group` | 3550 | credit shared with the group (`True` 2502) |
| `preTasks` | 2802 | `;`-separated prerequisite task titles |
| `targetPosition` | 3574 | position marker for guidance (mostly empty) |
| `crafting` | 27 | `;`-separated recipe ids |

---

## 5. Quest attributes and rewards

| attribute | n | notes |
| --- | ---: | --- |
| `id` | 1758 | unique; no collisions |
| `type` | 1758 | 14 values — `world` 511, `local` 466, `event` 353, `eventrepeatable` 131, `repeatable` 112, `wantedrepeatable` 63, `heroic` 33, `pvp` 32, `crafting` 24, + 5 rarer |
| `giver` / `goal` | 1758 | NPC that offers / completes the quest |
| `map` | 1756 | map the quest belongs to |
| `level`, `minLevel`, `maxLevel` | 1758 | level gating |
| `required` | 1308 | `;`-separated prerequisite quest ids |
| `requiredOperator` | 1308 | `ANY` 1259 / `ALL` 49 — how `required` combines |
| `followup` | 904 | next quest in the chain |
| `startSignal` / `stopSignal` | 1758 | see §6 |
| `stagedTasks` | 1758 | `true` (601) = tasks run in sequence; mirrors the `preTasks` chain |
| `repetitionType` | 1088 | `default` 1046, `daily` 38, `weekly` 4 |
| `chapterId` | 1758 | → `_Template_Chapter`, which carries `ActId` |
| `event` | 414 | `;`-separated event ids gating seasonal quests |
| `class` | 68 | class restriction incl. negations (`notdwarf`) |
| `boundToMap`, `deletable`, `enabledLive`, `levelRangeOnly` | 1758 | flags |
| `resets` | 8 | `;`-separated quest ids reset alongside this one |
| `versionfilter` | 23 | region gating (`china;korea`) |

`grindLevel` is `0` and `hasCompendiumText` is `false` on every quest — both are
dead fields in this build.

### Rewards

`<Reward>` has 6 types:

| type | n | shape |
| --- | ---: | --- |
| `loot` | 4762 | `@value` item id, `@amount`, `@class`, `@rarity` — one row per class, so a quest lists the same reward 4× |
| `rc` | 1758 | Andermant (premium currency), `@value` = amount |
| `vc` | 1758 | gold, `@value` = amount |
| `xp` | 1758 | fixed XP |
| `dynamicXP` | 1081 | XP scaled to player level |
| `statuseffect` | 16 | `@value` → `_Template_StatusEffect`, `@permanent` |

Every quest carries exactly one `rc`, one `vc` and one `xp` row (verified on all
1758; often `0`). `@toPlayerLevel="True"` (164 rows) scales the loot item to the
player's level.

Quest-level `<Drop>` (156 rows, e.g. the crafting tutorial) grants items on
quest *acceptance* rather than from an event.

---

## 6. Triggering: the signal system

Quests do not poll. `_Template_Signal` in `static.db4` (4635 rows) is a
conditional event bus, and quests subscribe to it by name.

A signal row is *"when EVENT happens, and all these conditions hold, emit
SIGNALID"*:

| column group | columns |
| --- | --- |
| trigger | `EventType`, `EventParam`, `Map`, `InTriggerId` |
| quest state | `QuestAccepted`, `QuestNotAccepted`, `QuestSolved`, `QuestNotSolved`, `QuestTaskDone`, `QuestTaskNotDone` |
| world state | `EventRunning`, `EventNotRunning`, `MapUnlocked`, `MapNotUnlocked` |
| player gating | `MinLevel`, `MaxLevel`, `CharClass`, `MinFame`, `MaxFame`, `MinGrind`, `MaxGrind`, `PlayerAmount`, `OncePerAccount` |
| output | `SignalId`, `SignalType`, `Enabled` |

`EventType` (top values): `MonstersEliminated` 978, `PlayerInteractedProp` 889,
`PlayerEnteredMap` 884, `PlayerHitTrigger` 421, `PlayerKilledMonster` 416,
`PlayerOpenedQuest` 406, `PlayerEnteredMapId` 244, `PlayerClosedQuest` 89,
`MonstersEliminatedPercentage` 87, `PlayerEndQuestTask` 77.

`SignalType`: `ServerSignal` 2948, `ClientSignal` 1411,
`ClientToServerSignal` 275, `MapSignal` 1.

`QuestTaskDone` / `QuestTaskNotDone` use `<questId>:<taskTitle>`.

### Example

```
SignalId         a0001_enter_map_quest_signal_dwarf
SignalType       ServerSignal
EventType        PlayerHitTrigger
EventParam       a0001_enter_map_quest_signal_trigger
Map              a0001_start_tutorial_dun
CharClass        dwarf
QuestNotAccepted a0001-start-tutorial-01-d
```

A dwarf walks into the trigger volume in the tutorial map; if they have not
already accepted `a0001-start-tutorial-01-d`, the signal fires. That quest
declares `startSignal="a0001_enter_map_quest_signal_dwarf"`, so it auto-starts.
The `QuestNotAccepted` condition is what makes the trigger idempotent.

The same mechanism runs in reverse via `stopSignal` (quest-level) and
`endSignal` (task-level, `;`-separated), which close quests and tasks.

---

## 7. Cross-references into static.db4

Measured resolution of every id class the quest files reference. Multi-value
fields were split on `;` first.

| quest field | resolves against | rate |
| --- | --- | ---: |
| `Dialog@actor` | `_Template_NPC` | 246/246 (100%) |
| `Barter@actor` | `_Template_NPC` | 162/162 (100%) |
| `HoldGround@actor` | `_Template_NPC` | 68/68 (100%) |
| `Pay@actor` | `_Template_NPC` | 8/8 (100%) |
| `Switch@item` | `_Template_Item` | 145/145 (100%) |
| `Config@value` | `_Template_HoldGround.HoldGroundId` | 90/90 (100%) |
| `Quest@chapterId` | `_Template_Chapter` | 24/24 (100%) |
| `Task@targetPosition` | `_Template_PositionMarker` | 6/6 (100%) |
| `Reward[statuseffect]@value` | `_Template_StatusEffect` | 7/7 (100%) |
| `Quest@giver` / `@goal` | `_Template_NPC` | 443/445, 430/433 (99%) |
| `Drop@value` | `_Template_Item` | 969/972 (99%) |
| `Event[collect]@value` | `_Template_Item` | 726/727 (99%) |
| `Event[encounter]@value` | `_Template_NPC` | 402/405 (99%) |
| `Barter@item` | `_Template_Item` | 303/306 (99%) |
| `Quest@event` | `_Template_Events` | 70/71 (98%) |
| `Switch@prop` | `_Template_StateObject` (+ prop tables) | 811/823 (98%) |
| `Event[kill]@value` | `_Template_Monster` | 2775/2855 (97%) |
| `Reward[loot]@value` | `_Template_Item` | 542/565 (95%) |
| `ClearRegion@map` | `_Template_MapTemplate.MapTemplate` | 93/100 (93%) |
| `Spawn@location` | `_Template_SpawnPoint` | 34/37 (91%) |
| `Event[trigger]@value` | `_Instance_TriggerVolume.TriggeredEventId` (map db4s) | 279/308 (90%) |
| `Spawn@value` | `_Template_Monster` | 49/54 (90%) |
| `Quest@map` | `_Template_MapTemplate.MapTemplate` | 163/202 (80%) |
| `Quest@startSignal` | `_Template_Signal.SignalId` | 41/52 (78%) |
| `Task@map` | `_Template_MapTemplate.MapTemplate` | 208/284 (73%) |
| `Task@endSignal` | `_Template_Signal.SignalId` | 16/22 (72%) |

### Tables that are not keyed on `Id`

These will silently return nothing if queried with `SELECT Id` — this cost real
debugging time twice:

- `_Template_MapTemplate` → **`MapTemplate`**
- `_Template_Signal` → **`SignalId`**
- `_Template_HoldGround` → **`HoldGroundId`**
- `_Instance_TriggerVolume` (map db4s) → **`TriggeredEventId`**

### Why the gaps

The shortfalls are not parse errors; they are content the shipped
`static.db4` does not carry:

- **Regional variants** — most `Quest@map` / `Task@map` misses are `_china`
  suffixed maps (`a0001_start_tutorial_dun_china`), matching the 23 quests with
  `versionfilter="china;korea"`.
- **Test and legacy content** — `TestServerSignal`, `dummy_quest_state_object_02`.
- **`Task@crafting` resolves only 16/89** against `_Template_CraftingRecipes`.
  The rest (`backitem_creation_crafting_defensive_a_dwarf`, …) look like recipe
  *group* or rule ids rather than recipe rows; the correct target table has not
  been identified.

---

## 8. Implementing this server-side

The minimum viable model:

1. **Per-character, per-quest counter bag** — `dict[quest_id, dict[flag, int]]`.
   Nothing else is needed; there is no separate task-state enum in the data.
2. **On accepting a quest** — grant quest-level `<Drop>` rows, mark accepted.
3. **On a world event** (kill, collect, interact, talk, deliver, trigger, …) —
   for each accepted quest, for each task, for each `<Event>` whose `type` and
   `value` match, evaluate its guards; if all pass, apply its `<Return>`,
   `<Drop>`, `<Spawn>`, and `<Failure>` children.
4. **Re-evaluate task completion** — a task is complete when its post-`Return`
   guards pass; apply its `<Return>` (`<title>_fin`) and emit `endSignal`.
5. **Quest completion** — when every task's `_fin` is set. Grant `<Reward>`
   rows filtered to the character's class, then offer `followup`.
6. **Offer visibility** — a `Switch`/`Dialog`/`Barter`/`Pay` affordance is
   shown when its own guards pass; this is what drives the NPC dialogue list
   and interactable props.

Ordering note: guards are evaluated against the counter bag *before* the
event's returns are applied, otherwise a `hasnot <title>_fin` guard on the
same event that completes the task would misfire.

Feasibility of wiring this into this server, including the client protocol
surface, is assessed in [QUEST_FEASIBILITY.md](QUEST_FEASIBILITY.md).

---

## 9. Open questions

- **Crafting recipe groups.** What table `Task@crafting` keys into.
- **Wire protocol.** This document covers the *data model* only. The nine
  client-side quest commands and how to recover their opcodes are covered in
  [QUEST_FEASIBILITY.md](QUEST_FEASIBILITY.md); the field layouts are not yet
  fully reversed.
- **`Return@amount` semantics.** 228 rows carry `amount` (e.g.
  `DeliveredLoot amount="20"`). Increment-by-N is the natural reading and fits
  the paired `Guard@amount` thresholds, but this has not been confirmed against
  client behaviour.
- **`questRowNumber` / `questNumber`** appear to drive quest-log ordering
  within a chapter; not verified.
