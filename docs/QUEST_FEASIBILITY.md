# Quest support: feasibility assessment

**Verdict: yes, fully implementable.** No blocker remains.

> **Update — the opcodes have been recovered.** §2 below described the quest
> opcodes as the one open unknown. They are now known, along with the ids of all
> 273 command classes in the client. See §2.1 and
> [COMMAND_IDS.md](COMMAND_IDS.md).

The data model is fully solved and documented in [QUESTS.md](QUESTS.md). What
follows assesses the *engineering* gap.

---

## 1. What is already in place

| Requirement | Status | Evidence |
| --- | --- | --- |
| Quest content | **Done** | 1758 quests decoded to `quests.xml`, lossless |
| Quest semantics | **Done** | [QUESTS.md](QUESTS.md); rules verified 0-exception across corpus |
| Static data links | **Done** | 90–100% id resolution for every reference class |
| Quest text | **Available** | `export_win32/language/<lang>/db.quest*.xml` |
| Kill events | **Exists** | `combat_resolution.py` — `resolution.killed` → `KillServerCommand` |
| Map enter/exit | **Exists** | `map_transition.py` — `_begin_map_transfer`, `_handle_map_travel` |
| NPC interaction | **Exists** | `NPCRequestCommand` `0x0029`, `EncounterCommand` `0x0066` |
| Prop interaction | **Exists** | `PropRequestCommand` `0x0038`, `PropInfoCommand` `0x0036` |
| Barter / shops | **Exists** | `commerce.py`, `RequestOfferCommand` `0x0059` |
| Inventory / item grant | **Exists** | `inventory_map.py`, `inventory_codec.py` |
| Currency / XP grant | **Exists** | `currency_protocol.py`, XP state in `player_persistence.py` |
| Persistence | **Exists** | `account_state.py`, `player_persistence.py` |
| Command dispatch | **Exists** | `dispatch.py` — flat `if/elif` on command id, trivial to extend |

The server is substantially built out (~14k lines across 26 services). Quest
support is an *addition*, not a rewrite: it needs a new service plus hook calls
from existing event sites.

---

## 2. The one real unknown: quest opcodes

Nine quest commands exist in the client, recovered from RTTI strings in
`dro_client64-patch5.exe`:

```
Commands::QuestCommand
Commands::QuestUpdateCommand
Commands::QuestLogInfoCommand
Commands::QuestDisableCommand
Commands::QuestTriggerSignalCommand
Commands::QuestMonsterIndicationInfoCommand
Commands::QuestMonsterIndicationRequestCommand
Commands::QuestTriggerIndicationInfoCommand
Commands::QuestTriggerIndicationRequestCommand
```

### 2.1 The opcodes (recovered)

| id | command |
| --- | --- |
| `0x008A` (138) | `QuestCommand` |
| `0x008B` (139) | `QuestUpdateCommand` |
| `0x008C` (140) | `QuestLogInfoCommand` |
| `0x008D` (141) | `QuestMonsterIndicationRequestCommand` |
| `0x008E` (142) | `QuestMonsterIndicationInfoCommand` |
| `0x008F` (143) | `QuestTriggerIndicationRequestCommand` |
| `0x0090` (144) | `QuestTriggerIndicationInfoCommand` |
| `0x0091` (145) | `QuestDisableCommand` |
| `0x0125` (293) | `QuestTriggerSignalCommand` |

Eight of the nine form a contiguous `0x8A–0x91` block, which is itself a
consistency signal. Recovered by [dump_command_ids.py](../../dump_command_ids.py);
method and validation in [COMMAND_IDS.md](COMMAND_IDS.md).

The historical account of why this was hard is kept below.

Wire layouts and client behaviour for `QuestTriggerSignalCommand`, the
`PropInteraction`/`NewProp`/`DiscardProp`/`PropUpdate` commands, and the
server-internal `NewEvent`/`DiscardEvent` pair are in
[PROP_AND_TRIGGER_COMMANDS.md](PROP_AND_TRIGGER_COMMANDS.md). The nine quest
commands, the NPC dialog command, and the retail accept / progress / turn-in
sequences (verified against a captured tutorial run) are in
[QUEST_PROTOCOL.md](QUEST_PROTOCOL.md); the "Field layouts" guesses below
are superseded by it.

---

No quest opcode appeared in `gameplay_protocol.py`, and the retail capture in
`live server packet logs/` contains no quest traffic (checked: 0
quest-id-shaped strings across 2901 packets; that session never loaded a
character into the world).

### Why this is tractable

The client's command registry is a **fixed-size table**, revealed by this
template signature in the binary:

```
Network::CommandFactory<unsigned short, 369>::RegisterCommand(
    class Network::Command *(__cdecl *)(void), unsigned short)
```

So command ids are `unsigned short` indices into a 369-entry table, populated
by `RegisterCommand(creatorFn, id)` calls with literal ids. Dumping every
caller of `RegisterCommand` and reading the two arguments yields the
**complete opcode → command class map in a single pass** — not just the quest
ones.

This is directly actionable: the Ghidra project already exists at
`.reverse/dso-c.rep`, with headless scripts in `ghidra_scripts/`
(`DumpCallersByAddress.java`, `DumpStringReferenceFunctions.java`) that do
exactly this kind of extraction.

**Estimate: one focused Ghidra session.** The payoff is larger than quests —
it would also close out every other unmapped opcode in the protocol.

### Field layouts

Recoverable from the decompiled client handlers, four of which are already
extracted in `game files/commands/`. `HandleQuestUpdateCommand` alone already
reveals its shape through its own error strings and accessor calls:

- an **actor id** — *"Received QuestUpdateCommand from server with invalid actor id (%u)!"*
- a **quest id as a string** — *"quest info for id '%s' not found"*
- a **task index** — *"wrong task index received"*
- a **progress value** — `SetActiveQuestTaskProgress::SetTask(int)` / `SetProgress(int)`

That is `{actorId: uint32, questId: string, taskIndex: int, progress: int}` —
enough to start. `QuestLogInfoCommand` (the login-time bulk sync) needs the
same treatment.

Note the client holds `quests.pbxml` locally, so the server transmits **string
quest ids and integer task indices**, not quest content. That keeps the
payloads small and the server side simple.

---

## 3. Event source gap analysis

The quest engine needs 14 event types. This is the decisive practical question,
because it determines whether the work ships incrementally or all-or-nothing.

**It ships incrementally.** Cumulative quest coverage as each event source is
added (a quest counts only when *every* event it needs is supported):

| after adding | quests fully covered | share |
| --- | ---: | ---: |
| `kill` | 67 | 3% |
| `+ encounter` | 409 | 23% |
| `+ collect` | 628 | 35% |
| `+ interact` | 914 | 51% |
| `+ talk` | 1090 | 62% |
| `+ deliver` | 1310 | 74% |
| `+ trigger` | 1496 | 85% |
| `+ regioncleared` | 1597 | 90% |
| `+ crafting` | 1621 | 92% |
| `+ holdground` | 1701 | 96% |
| `+ victory`, `battle`, `pay` | 1758 | 100% |

Per-event-source status:

| event | quests needing it | server support |
| --- | ---: | --- |
| `encounter` | 1500 | NPC encounter flow exists (`0x0066`) |
| `kill` | 914 | **exists** — `resolution.killed` |
| `collect` | 575 | item pickup exists; needs a quest hook |
| `interact` | 488 | prop interaction exists (`0x0038`) |
| `talk` | 298 | needs dialogue offers (new) |
| `deliver` | 271 | barter exists in `commerce.py`; needs quest-scoped variant |
| `trigger` | 244 | needs trigger-volume evaluation (new) |
| `regioncleared` | 113 | needs region monster tracking (new) |
| `holdgroundsucceeded/failed` | 80 | needs wave encounter system (new) |
| `victory` / `battle` | 27 / 11 | PvP services exist (`pvp_combat.py`) |
| `crafting` | 24 | crafting partially present |
| `pay` | 19 | currency exists |

The three highest-value events (`encounter`, `kill`, `interact` — 51% of quests
combined) all sit on infrastructure that already works.

### Trigger volumes

`ExploreTask` needs 308 trigger ids. These resolve against the per-map
databases: `data/maps/<map>.db4` → `_Instance_TriggerVolume`, keyed on
**`TriggeredEventId`** — **279/308 (90%)**. The rest are `_china` variants and
test triggers. So the data exists; only the runtime check (is the player inside
volume X) is new work.

---

## 4. The signal engine

`_Template_Signal` (4635 rows) is the trigger bus that starts and stops quests
(§6 of [QUESTS.md](QUESTS.md)). It needs a real evaluator, but it is a
self-contained, table-driven one:

- 20 distinct `EventType` values, of which the top 6 (`MonstersEliminated`,
  `PlayerInteractedProp`, `PlayerEnteredMap`, `PlayerHitTrigger`,
  `PlayerKilledMonster`, `PlayerOpenedQuest`) cover ~4600 of the rows.
- Conditions are plain column comparisons against quest state, world event
  state, and player level/class/fame.

`PlayerEnteredMap` alone (884 rows) is enough to bootstrap the tutorial and
most hub quest offers, and the server already knows when a player enters a map.

---

## 5. Recommended sequence

1. **Dump the command registry** — one Ghidra pass over `RegisterCommand`
   callers. Yields all 9 quest opcodes *and* every other unmapped opcode.
   Everything else depends on this.
2. **Reverse `QuestLogInfoCommand` + `QuestUpdateCommand`** from the already
   extracted decompilations. These two alone give a visible, testable quest log.
3. **Build the flag engine** — pure logic over `quests.xml`, no protocol
   involved, unit-testable offline against the 1758 quests. This is the least
   risky part and can proceed in parallel with steps 1–2.
4. **Add a `quests.py` service** + persistence of the counter bag in
   `account_state.py`.
5. **Hook `kill` and `encounter`** → 23% of quests.
6. **Add `collect`, `interact`** → 51%.
7. **Signal engine with `PlayerEnteredMap` / `PlayerHitTrigger`** → automatic
   quest offering.
8. **`talk`, `deliver`, `trigger`** → 85%.
9. Long tail: `regioncleared`, `holdground`, crafting, PvP → 100%.

Steps 3–4 are the bulk of the logic and carry the least uncertainty. Step 1 is
the only true unknown, and it is bounded.

---

## 6. Risks

| Risk | Severity | Notes |
| --- | --- | --- |
| Opcode extraction fails in Ghidra | **Medium** | Fallback: instrument the client, or capture a retail session that actually enters the world — the current capture does not |
| Field layouts wrong in edge cases | Low | Iterative; client logs a clear error on malformed commands (`"called with command of wrong type"`) |
| `Return@amount` semantics assumed | Low | 228 rows affected; wrong reading only mis-scales delivery counters |
| Client-authoritative checks | Low–Medium | `ClientSignal` (1411 rows) suggests some conditions are evaluated client-side; needs care so server and client agree |
| `_china` / test content unresolvable | Negligible | ~20% of map refs, but that content is not served anyway |

No risk identified is a blocker; each has a fallback.

---

## 7. Bottom line

Full quest support is achievable. The hard intellectual work — understanding
the quest model — is already done and verified. What remains is:

- **one bounded reverse-engineering task** (the command registry) that pays off
  well beyond quests;
- **a pure-logic flag engine** that can be built and tested today with no
  protocol knowledge at all;
- **incremental event hooks**, over half of which attach to systems this server
  already implements.

The single most valuable next step is dumping the 369-entry command factory
table, because it unblocks quests and every other unmapped command at once.
