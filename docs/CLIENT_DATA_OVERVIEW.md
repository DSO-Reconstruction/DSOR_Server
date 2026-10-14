# Client data: what we have, and how to use it

A consolidated view of the shipped Drakensang Online client data — what each
file is, what has been decoded, what it is authoritative for, and where it can
concretely improve this server.

Companion documents, each going deeper on one area:

| doc | covers |
| --- | --- |
| [QUESTS.md](QUESTS.md) | the quest data model, verified against all 1758 quests |
| [QUEST_FEASIBILITY.md](QUEST_FEASIBILITY.md) | whether quest support can be fully implemented |
| [SEQUENCES.md](SEQUENCES.md) | the five sequence packs and their DB linkage |
| [ARCHITECTURE.md](ARCHITECTURE.md) | the server itself |

---

## 1. The data landscape

| Source | Size | Format | Status |
| --- | --- | --- | --- |
| `data/db/static.db4` | 68 MB | SQLite, 195 tables | in use |
| `data/maps/*.db4` | — | SQLite, `_Instance_*` tables | partly used |
| `export_win32/quests/quests.pbxml` | 6.7 MB | PACK/BXML, 1758 quests | **decoded** |
| `export_win32/sequences/*.pbxml` | ~28 MB | PACK/BXML, 49 packs | **decoded** |
| `export_win32/language/<lang>/db.*.xml` | ~14 MB | XML, 33 063 tokens | **parsed** |
| `export_win32/sequences/sequences.freg` | 246 KB | plain text, 4637 rows | registry |
| `dro_client64-patch5.exe` | 21 MB | PE with debug symbols | partly reversed |

Two container formats were reverse-engineered this pass, both Nebula3 FourCCs
stored little-endian: **`KCAP`** ("PACK") wrapping **`LMXB`** ("BXML"). The
decoder is [pbxml_to_xml.py](../../pbxml_to_xml.py); the layout is in its module
docstring. Decoding is provably lossless — for every entry the set of strings in
the string table equals the set of tags/attribute names/values in the output,
with zero unused and zero out-of-table values.

---

## 2. What is authoritative where

The single most useful thing learned across this work is **which file owns which
fact**. Getting this wrong is the main way to build something subtly incorrect.

| Fact | Authoritative source | Not authoritative |
| --- | --- | --- |
| Quest structure, tasks, guards, rewards | `quests.pbxml` | — |
| Quest offer/close conditions | `_Template_Signal` | quest file alone |
| Item/monster/NPC stats | `static.db4` | — |
| Chest/box contents | **server-side loot lists** | UI tooltip text |
| Skill damage timing | `_Template_Skill` frame columns | skill sequences |
| Corpse/respawn timing | `_Template_Monster.*Duration` | despawn sequence length |
| Status effect duration | `_Template_StatusEffect` | statuseffect sequences |
| Trigger volumes | `data/maps/*.db4` `_Instance_TriggerVolume` | `static.db4` |
| Visual/audio presentation | `sequences/*.pbxml` | — |

**Sequences are presentation-only.** This was tested rather than assumed: across
all five packs examined, every one of **1269 `SkillPhaseTrackBar` markers has
`eventName="empty"`**. No track-bar type in any pack carries damage, health,
loot, or state. The server never needs to parse a `.pbxml` at runtime.

**Sequence length ≠ gameplay duration.** `DeathDuration` is *shorter* than the
death animation in 221 of 281 cases; `DespawnDuration` is `0` for 161 of 250.
The DB columns are the authoritative timers.

---

## 3. Traps worth knowing

Four tables are not keyed on `Id` and return nothing silently if you assume they
are. This cost debugging time twice:

- `_Template_MapTemplate` → **`MapTemplate`**
- `_Template_Signal` → **`SignalId`**
- `_Template_HoldGround` → **`HoldGroundId`**
- `_Instance_TriggerVolume` (map db4s) → **`TriggeredEventId`**

Two more naming rules:

- **`…Sequence` is a real name; `…SequenceMapping` is an indirection key.**
  Direct columns resolve 98–100% against `sequences.freg`; mapping columns
  resolve 8%.
- **Many list columns are `;`-separated** (`required`, `preTasks`, `endSignal`,
  `ClearRegion@monster`, `Barter@item`, `Quest@event`). Splitting first moves
  several cross-reference rates from ~75% to ~99%.

---

## 4. Where this improves the server

Ordered by value-per-effort. Items already handled are marked so — the server
already reads the skill frame columns and models status effects, so those are
excluded.

### 4.1 Quest support — the big one

Fully implementable; assessed in detail in
[QUEST_FEASIBILITY.md](QUEST_FEASIBILITY.md). Summary:

- The engine is small: a per-quest **bag of named counters**, three primitives
  (`Return` increments, `Guard` tests, everything else decides when). Rules
  verified with 0 exceptions across 5285 tasks.
- It is **pure logic** — buildable and unit-testable offline against
  `quests.xml` today, with no protocol knowledge at all.
- It ships incrementally. Coverage as each event source is wired up:
  `kill`+`encounter` → 23%, `+collect`+`interact` → 51%, `+talk`+`deliver` →
  74%, `+trigger` → 85%, remainder → 100%.
- The three highest-value events all sit on infrastructure that already works
  (`resolution.killed`, `EncounterCommand 0x0066`, `PropRequestCommand 0x0038`).

**Blocker:** the nine quest opcodes are unknown. See §4.2.

### 4.2 Dump the command factory table — **done**

> Completed. 273 command classes with ids 1–368 recovered and validated
> (35 exact agreements with the server's existing constants, 0 contradictions).
> See [COMMAND_IDS.md](COMMAND_IDS.md) and
> [command_ids.json](command_ids.json). The original
> reasoning is kept below.



The client registry is a fixed-size table:

```
Network::CommandFactory<unsigned short, 369>::RegisterCommand(
    Command *(__cdecl *)(void), unsigned short)
```

Command ids are indices into 369 slots, populated by `RegisterCommand(fn, id)`
calls with literal ids. Dumping every caller yields **the complete opcode →
command-class map in one pass** — not just the 9 quest commands, but every
unmapped opcode in the protocol. The Ghidra project (`.reverse/dso-c.rep`) and
suitable headless scripts (`ghidra_scripts/`) already exist.

This is the single highest-leverage task available. It unblocks quests and
everything else at once.

### 4.3 Correct respawn and corpse timing

Use `_Template_Monster.DeathDuration` / `DespawnDuration` rather than anything
derived from animation length. Given how often the two disagree (§2), any
implementation that guessed from visuals is wrong. `DespawnDuration = 0` on 161
of 250 sequences is a meaningful signal, not missing data.

### 4.4 Chest and reward contents

`data/catalogs/chest_contents.json` maps 3292 containers; 2577 have resolved contents.
Sources differ in trustworthiness and should not be blended:

- `_Template_Chest`, `_Template_Offergroup`, `_Template_WishingWell`,
  `_Template_HatchEggs`, `_Template_StickerbookItem` — **authoritative**,
  explicit contents.
- Client tooltip text — **evidence only**. It recovered contents for 424 of the
  926 loot-list containers, but it is authored marketing copy: vague, rounded,
  possibly stale. 915 parsed entries are genuinely ambiguous (e.g. class-specific
  set pieces) and carry candidate lists rather than a single id.

Chests nest — `t000_gold_chest` yields other containers — so any implementation
needs transitive expansion with chance propagation.

### 4.5 Localisation

33 063 locale tokens are parsed and indexed by `chest_ui_contents.py`, including
a 6383-entry display-name → template-id reverse index with rarity
disambiguation. Directly reusable for any feature needing item names, quest
text, or NPC dialogue.

### 4.6 Cheap correctness wins

- Use `sequences.freg` for sequence resolution instead of folder-scoped lookup
  — folder-only fails for ~24% of `DespawnSequence` values.
- Note `StatusEffectDurationPvP` exists separately from `StatusEffectDuration`.
- `_Template_Skill.ScaleSkillTiming` gates whether attack speed scales a skill's
  frame timing — already respected in `local_combat_state.py`, worth preserving.

---

## 5. Known-bad data in the shipped files

Real defects found while verifying, worth guarding against rather than
crashing on:

- **Dead quest guard.** `k0001-foolish-enterprise` guards on
  `k0001-foolish-enterprise-interactInteract`, which no `Return` in any quest
  produces. That task can never complete.
- **7 pack-name/id mismatches** in `quests.pbxml` (`test-HoldGround` contains id
  `Test-HoldGround`) — authoring typos, preserved as `packEntry`.
- **2 unresolvable sequences** (`guardian_death_andermagic`,
  `guardian_death_poison`).
- **122 offergroups** reference offer ids absent from `_Template_Offer`.
- **`grindLevel` and `hasCompendiumText`** are dead fields — constant across all
  1758 quests.

Roughly 20% of map references are `_china`/`_korea` regional variants absent
from this `static.db4`, matching the 23 quests carrying
`versionfilter="china;korea"`. Not errors — content this server does not serve.

---

## 6. Suggested order of work

1. **Dump the 369-entry command factory** (§4.2). Unblocks the most, benefits
   everything.
2. **Build the quest flag engine** (§4.1) — pure logic, no protocol dependency,
   can proceed in parallel with 1.
3. **Reverse `QuestLogInfoCommand` + `QuestUpdateCommand`** from the already
   extracted decompilations in `game files/commands/`.
4. **Wire `kill` + `encounter`** → 23% of quests live.
5. **Signal engine** with `PlayerEnteredMap` / `PlayerHitTrigger` → automatic
   quest offering.
6. Broaden event coverage toward 100%.

Steps 2 and 3 are independent of each other and of step 1's completion, so this
parallelises across more than one person.

---

## 7. Tooling produced

| tool | purpose |
| --- | --- |
| [pbxml_to_xml.py](../../pbxml_to_xml.py) | decode any `.pbxml` pack; `--split`, `--list` |
| [inspect_chest_contents.py](../../inspect_chest_contents.py) | container → item map, JSON + markdown |
| [chest_ui_contents.py](../../chest_ui_contents.py) | locale loader, tooltip parser, name→id resolver |
| [dump_command_ids.py](../../dump_command_ids.py) | recover the client's command id table; `--check`, `--grep` |
| [audit_item_sets.py](../../audit_item_sets.py) | classify every set modifier by whether the server implements it |
| [apply_stormbound_set.py](../../apply_stormbound_set.py) | example content migration; `--dry-run`, `--locale-only` |

All read the databases read-only and are safe to run against live data, with
one exception: `apply_stormbound_set.py --apply` **writes** `static.db4` and
overwrites the client's copy. It backs both up first and refuses to run twice
(see [SET_CONCEPT_STORMBOUND.md](SET_CONCEPT_STORMBOUND.md) §5 for why a second
run would corrupt status-effect row ids).
