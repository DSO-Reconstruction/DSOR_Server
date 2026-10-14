# Set concept: **Stormbound Regalia**

A four-class mythic set built on the weapon + shoulders pieces that already
exist in `static.db4`. **This is a design concept, not an implementation** — no
rows have been written. Every id, group and modifier verb below was checked
against the shipped database so the design stays inside what the engine can
already express.

---

## 1. The pieces

All eight items exist and are currently bound to **no set**, so they are free to
claim.

| class | weapon (`RightHandSlot`) | shoulders (`ShoulderSlot`) |
| --- | --- | --- |
| mage | `mage_mythic_rh_staff_01` | `mage_mythic_shoulders_01` |
| ranger | `ranger_mythic_rh_longbow_01` | `ranger_mythic_shoulders_01` |
| warrior | `warrior_mythic_rh_sword_01` | `warrior_mythic_shoulders_01` |
| dwarf | `dwarf_mythic_rh_pistol_01` | `dwarf_mythic_shoulders_01` |

Note `*_mythic_rh_staff_01` only exists for the mage — the other classes' mythic
right-hand weapons are a longbow, a sword and a pistol respectively.

Two pieces means only the `2)` modifier tier can ever fire. §6 sketches
optional 3rd/4th pieces if the set should grow later; siblings already exist
(`mage_mythic_helmet_dragan_01`, `mage_mythic_backitem_dragan_01`).

---

## 2. The shared idea: **Static**

All four sets read and write one common resource so a mixed group amplifies
itself rather than four sets sitting side by side.

> **Static** — a debuff stack applied to enemies by any Stormbound wearer.
> `MaxStackSize: 10`. At 10 stacks the target **discharges**: a lightning nova
> in a small radius, stacks reset to 0.

Because Static lives on the *enemy*, a mage, a dwarf and a warrior hitting the
same pack all feed the same counter. Discharge is credited to whoever applied
the tenth stack. That is the multiplayer hook — the set gets better the more
Stormbound players are present, without any of them losing solo value.

---

## 3. Mage — **Stormcaller's Regalia**

The requested behaviour: chain lightning calls down extra bolts around what it
hits.

### 2-piece

> **Conduction** — `chainlightning` has a **35% chance** on hit to mark the
> target as a **Conduit** for 4 s. A Conduit calls down **3 bolts** on enemies
> within 5 m, with a **25% chance of a 4th**. Each bolt applies 1 Static.

Why chain lightning is a good anchor: it already carries
`ReproductionCount: 2` (its two chain jumps) and its own `chainlightning` skill
group, so the hook can target it precisely without catching every mage skill.

Grammar, using verbs the shipped sets already use:

```
2)SkillStatusEffect:addVE,chainlightning,set_stormbound_conduit,C:0.35#StormboundBonus01;
```

`set_stormbound_conduit` is a `_Template_StatusEffect` shaped like the existing
`skill_lightningstrike_lightning_field_aura`, which is exactly the
"leave a damaging area at a location" precedent:

| field | value | why |
| --- | --- | --- |
| `StatusEffectDuration` | `4.0` | conduit window |
| `StatusEffectTickRate` | `1.0` | one bolt volley per second |
| `AuraShape` / `AuraRadius` | `Sphere` / `5.0` | bolt search radius |
| `AuraTargets` | `Enemies` | |
| `AuraEffectId` | `set_stormbound_bolt` | the bolt itself |
| `StartSequence` | `pw019_lightning_field_aura` | existing lightning VFX to prototype with |
| `ExclusivityType` | `Replace` | re-hitting refreshes, never stacks |

The 3-vs-4 roll is a second `C:` on the fourth bolt rather than a random count,
so the floor is guaranteed and only the bonus bolt is luck:

```
StartModifiers: ActorStatusEffect:AddAE,set_stormbound_bolt,C:1.0,ON:Immediate;
                ActorStatusEffect:AddAE,set_stormbound_bolt,C:1.0,ON:Immediate;
                ActorStatusEffect:AddAE,set_stormbound_bolt,C:1.0,ON:Immediate;
                ActorStatusEffect:AddAE,set_stormbound_bolt,C:0.25,ON:Immediate;
```

### The cap

Uncapped, this compounds badly: 35% × every chain target × 4 s × 3–4 bolts/s.
There is **no per-player instance-count field** in `_Template_StatusEffect` —
`ExclusivityType` is only `Replace` / `Deny`, and `ExclusiveGroup` scopes to a
target, not to a caster. So a literal "max 3 Conduits" counter is not
expressible in data alone.

Instead, **let the cap fall out of timing.** Applying a Conduit also puts a
short internal cooldown on the caster:

```
set_stormbound_conduit_icd
  StatusEffectDuration : 1.35
  EffectHosts          : Causer
  ExclusiveGroup       : StormboundConduitICD
  ExclusivityType      : Deny
```

With a 4 s Conduit and a 1.35 s ICD, **at most 3 can ever be live at once** —
the cap is arithmetic, not bookkeeping. It also smooths the damage curve: on a
huge pack you get a steady 3 rather than a spike of twelve.

**Fallback if causer-side ICD gating turns out not to be supported** (see §8):
make the Conduit a single travelling mark instead — `EffectCauser: Originator`
with `ExclusivityType: Replace`, so each new chain-lightning hit *moves* your
Conduit rather than adding one. That caps at exactly 1, is guaranteed
expressible with fields that demonstrably exist, and has its own appeal — your
storm always follows your current target. Bolt count would go 4–5 to compensate.

### The extra bit — **Overcharge**

> When a Conduit target **dies**, the storm re-arcs: `chainlightning` gains
> **+2 chain jumps** for 5 s (`ReproductionCount` 2 → 4).

This is the cool part, and it is cheap: `ReproductionCount` is already the chain
count on the skill, and `ON:kill` triggers already appear in shipped
`VictimStatusEffects` (`item_anniversary_2h_weapons_aura,…,ON:kill`). It creates
a real play pattern — open on a pack, kill one conduit, and the next cast
whips through four targets instead of two, refreshing more Conduits. It ramps
on big packs and does nothing on a lone boss, which is a healthy trade.

---

## 4. The other three classes

Each hooks a signature skill that already has the right machinery, so nothing
needs inventing.

### Ranger — **Stormhunter's Regalia**

Anchor: `explosiveshot` (already carries `LocationStatusEffects`) and
`packofwolves` (already summons `minion_packofwolves`).

> **Tempest Volley** — `explosiveshot` has a **30% chance** to split on impact
> into **3 storm arrows** (**+1 at 25%**) that seek separate enemies within
> 8 m. Each applies 1 Static.
>
> **Stormpack** — while `packofwolves` is active, wolf hits apply 1 Static.

The wolves are the flavour win: a summon that exists purely as damage becomes a
Static engine, so the ranger contributes to group discharges while doing
nothing different mechanically.

### Warrior — **Stormbreaker's Regalia**

Anchor: `bloody360` (spin) and `defiance` (already carries
`LocationStatusEffects`).

> **Thunderclap** — `bloody360` has a **35% chance** per rotation to call down
> **3 bolts** (**+1 at 25%**) on enemies inside the spin. Each applies 1 Static.
>
> **Lightning Rod** — while `defiance` is active, every Static stack on nearby
> enemies adds damage reflection. Fully-charged enemies (10 Static) that
> discharge near you deal **no** damage to you.

This inverts the usual warrior problem: standing in the middle of a charged pack
becomes the *correct* play instead of a death sentence.

### Dwarf — **Stormwright's Regalia**

Anchors: the `TurretSkills` group (covers **all** turrets — Tesla, Combat,
Covering Fire, Machine Gun) and `SteamConductor`.

> **Overclock** — using turret skills banks **Charge**, up to **10**. When the
> Charge expires it **discharges**: a lightning nova whose damage scales with
> Charge, applying **Static equal to half the Charge** to everything hit.

*(Charge is hosted on the dwarf rather than on each turret — see the operation
keyword note in §5 for why.)*
>
> **Capacitor** — `SteamConductor` (6 s cooldown) **immediately discharges every
> active turret at its current Charge without killing them.** Turrets reset to
> 0 Charge and keep firing.

This is a *banking* mechanic, which makes the dwarf structurally different from
the other three rather than a fourth flavour of "bolts fall down": the mage
reacts to hits, the ranger to shots, the warrior to spins — the dwarf **decides
when the payoff happens.** Place turrets, let them wind up, then pop them all at
once with Steam Conductor. Holding for a full 10 stacks versus cashing out early
is a real choice, and it gives an otherwise passive class a burst button.

Every piece of it has shipped precedent:

| need | mechanism | precedent |
| --- | --- | --- |
| affect all turrets | hook group `TurretSkills` | 8 sets hook `SewersSkill` this way |
| build Charge | `MaxStackSize: 10`, `StatusEffectTickRate: 1.0` | `itemset_sewers_damage_buff` |
| discharge on expiry | `DoneModifiers` / `StopModifiers` → `ActorStatusEffect:AddAE,…,ON:Immediate` | `itemset_sewers_damage_buff` uses exactly this pair |
| manual trigger | hook group `SteamConductor`, `ON:skillstart` | `chr2025_set_coldchill,C:0.0,On:skillstart` |
| scale by stacks | `ActorVariable:$0,absolute,name` then read `$name` | `itemset_sewers_damage_buff` |

**Why Arc Grid was dropped.** The previous draft had turrets linking to each
other with damaging arcs between them. Two problems, both fatal: turret-to-turret
linking has no precedent in the shipped data and would need new engine support,
and — decisively — `TurretTesla` has **`SummonMonsterLimit: 1`**, so a dwarf can
only ever have one Tesla turret out. The mechanic could not have worked as
described regardless of engine support.

*(If a multi-turret fantasy is still wanted later, `JackInTheBox` is the one
dwarf summon with `SummonMonsterLimit: 10`.)*

---

## 5. The set rows

Sketched in the shipped `_Template_ItemSet` format
(`<pieces>)<Modifier>:<args>;|`), modelled on `set_warrior_sewers`:

```
Id:        set_stormbound_mage
SetParts:  mage_mythic_rh_staff_01;mage_mythic_shoulders_01;
Modifiers: 2)CriticalValue:5.820776,absolute,LevelModifier,CriticalFactorAbsolute;
           SkillStatusEffect:addVE,chainlightning,set_stormbound_conduit,C:0.35#StormboundBonus01;
           ActorStatusEffect:AddAE,set_stormbound_overcharge,C:1.0,ON:Kill#StormboundBonus02;|
```

with `set_stormbound_ranger` / `_warrior` / `_dwarf` following the same shape
against `explosiveshot` / `bloody360` / `ShrapnelShot`.

Verbs used — all present in shipped sets: `SkillStatusEffect` (110 uses),
`ActorStatusEffect` (167), `CriticalValue` (115), `Damage` (141),
`ActorVariable` (8), `Localize` (9).

### Status-effect rowids must be contiguous — run the migration exactly once

The client maps a status-effect **table row index** to an id
(`StatusEffectManager::StatusEffectTableRowToId`) and asserts
`rowIdx < effectInfos.Size()`. The server sends `rowid - 1`
(`ItemSetDatabase._effect_definition`). Those two only agree while
`_Template_StatusEffect` rowids run contiguously `1..N`.

Both tables declare `Id TEXT PRIMARY KEY ON CONFLICT REPLACE`, and **SQLite's
REPLACE deletes and re-inserts** — so a second run of the migration moves all 13
rows to the end and leaves 13 holes. Row count stays 6434 while max rowid
becomes 6447, the server starts sending ids up to 6446 into a 6434-entry array,
and the client dies on load:

```
*** NEBULA ASSERTION ***
expression: rowIdx < this->effectInfos.Size()
file: shared/statuseffects/statuseffectmanager.h  line: 306
```

**There is no safe in-place repair.** A global `VACUUM` would fix the holes but
also renumber the three tables that are legitimately non-contiguous in the
shipped database — `_Template_Offer` (15254 rows, max rowid 15256),
`_Template_CharacterBeard`, `_Template_CharTransfer` — breaking references into
them. The only correct recovery is to **restore the pre-migration backup and
apply once.**

`apply_stormbound_set.py` now enforces this: it refuses to run against a
database that already contains stormbound rows or that has non-contiguous
status-effect rowids, and asserts contiguity after writing.

### Localisation lives in three places, not one

A set needs entries in **three different files**, and getting one wrong shows up
only in-game. The client is helpful about it: a missing entry renders as the
raw lookup path, which names both the file and the expected key.

| what | file | category | key | failure looks like |
| --- | --- | --- | --- | --- |
| set display name | `db.itemset.xml` | `db.itemset.table` | `<setId>Title` | `LF:db.itemset.table_set_stormbound_mageTitle` |
| status effect name | `db.statuseffect.xml` | `db.statuseffect.table` | `<effectId>Title` | `LF:db.statuseffect.table_set_stormbound_attunementTitle` |
| bonus line description | `gui.statuseffects.xml` | `gui.statuseffects.modifier` | the `#Token` on the modifier | shows the raw token |

Both `Title` tables are title-only in the shipped data — 411/411 for itemset,
1800 of 1816 for statuseffect — so no `Desc` entries are needed.

If a status-effect row sets an explicit `LocaleId`, the lookup uses that name
instead of `<id>Title`; registering both costs nothing and avoids depending on
which path the client takes.

`apply_stormbound_set.py --locale-only` writes all three without touching the
databases, and `validate_titles()` refuses to write if any set or effect lacks
a display name.

### Operation keywords — the client validates these

The two modifier kinds take **different** operation vocabularies, and the
client rejects the entire set at load time if one is wrong:

```
(itemset.xml) - Failed to parse start modifiers for item set id '…'!
Unrecognized token 'addAE' found for SkillStatusEffect modifier!
```

| kind | accepted operations | meaning |
| --- | --- | --- |
| `SkillStatusEffect` | `addUE` / `AddUE`, `addVE`, `addLE`, `modUE`, `modVE` | **U**ser / **V**ictim / **L**ocation |
| `ActorStatusEffect` | `addAE` / `AddAE`, `modAE`, `addLE` | **A**ctor |

`SkillStatusEffect` has **no `AE` variant.** Its UE/VE/LE mirror the skill's own
`UserStatusEffects` / `VictimStatusEffects` / `LocationStatusEffects` columns —
that is the naming logic, and it is the reliable way to remember it.

This bit during implementation: `SkillStatusEffect:addAE` was written by false
analogy with `ActorStatusEffect:AddAE`, and the server's parser accepted it
silently because it is generic. Only the client caught it.
`apply_stormbound_set.py` now validates operation keywords against the shipped
vocabulary and refuses to write rather than relying on the client to catch it.

**Consequence for the dwarf.** `addUE` hosts the effect on the **caster**, so
Overclock's Charge banks on the dwarf while turret skills are used, rather than
riding on the turret entity. Mechanically equivalent for play — build Charge,
spend it with Capacitor — but the fiction is "the dwarf's capacitors charge",
not "each turret charges". Putting a stack on the turret itself would need
`EffectHosts: Summoner`-style routing and is not attempted here.

---

## 6. If it should grow past two pieces

Only the `2)` tier can fire with two items. Natural extensions, using pieces
that already exist:

| tier | suggestion |
| --- | --- |
| 3 | Static `MaxStackSize` 10 → 8 (faster discharges) |
| 4 | Discharge radius +50%, and discharge refunds resource |

---

## 7. Balance knobs

Everything tunable sits in one place, which is deliberate:

| knob | start | notes |
| --- | ---: | --- |
| proc chance | `C:0.35` | mage; 0.30 for the others since their anchors hit more targets |
| bonus-bolt chance | `C:0.25` | the "3 or 4" roll |
| conduit duration | `4.0 s` | shorter than the 5 s lightning-field precedent |
| **conduit ICD** | **`1.35 s`** | **with the 4 s duration this caps concurrent Conduits at 3** |
| bolt volley rate | `1.0 /s` | |
| Static cap | `10` | |
| overcharge | `+2 jumps, 5 s` | |
| turret Charge rate / cap | `1.0 /s` / `10` | dwarf; 10 s to full |
| Capacitor cooldown | `6.0 s` | inherited from `SteamConductor` — not a new knob |

The mage cap is now structural (§3) rather than a number to remember. The
remaining tuning risk is the dwarf: Capacitor's 6 s cooldown against a 10 s
wind-up means the optimal line is probably "cash out at ~6 Charge, repeatedly"
rather than ever holding for 10. If playtesting confirms that, either slow the
Charge rate to make 10 unreachable-by-design and honest, or add a bonus at full
Charge worth waiting for. Do not fix it by lengthening the cooldown — that just
makes the class feel worse between presses.

---

## 8. Not verified

Stated so this reads as the concept it is:

- **No rows were written.** Nothing here is in `static.db4`.
- **The `C:` field is read as a probability** from context (`C:1.0` on
  guaranteed effects, `C:0.2`-style values elsewhere). Not confirmed against
  client behaviour.
- **Whether a set modifier can raise `ReproductionCount`** is unverified. It is
  a skill column, not one of the modifier verbs seen in shipped sets, so
  Overcharge may need a different implementation — for example a status effect
  that grants a chain-jump bonus, if such a modifier exists.
- **Causer-side ICD gating** — the mage cap (§3) assumes a status effect hosted
  on the *caster* with `ExclusivityType: Deny` will block a re-proc while
  active. `EffectHosts: Causer` and `Deny` both exist in shipped data, but their
  use together as a proc gate is not confirmed. §3 gives a guaranteed-expressible
  fallback if it does not hold.
- **The bolt effect** `set_stormbound_bolt` is sketched, not specified —
  damage scaling, whether it crits, and whether it can hit the conduit itself
  all need deciding.
- **Whether a turret's expiry and its death** both fire `DoneModifiers` /
  `StopModifiers`. Overclock's discharge should happen either way; if only one
  path fires, killing a turret early would silently eat the Charge.

### Availability check: warrior `bloody360`

Checked because it was reported missing from the talent tree. **It is present
and usable** — no work needed:

| where | status |
| --- | --- |
| `_Template_SkillTalent` | `warrior_bloody360_talent` → skill `bloody360`, **unlock level 14**, max rank 10 |
| `_Template_Skill` | `IsLive = 1`, `Hidden = 0`, groups include `warrior_skills;bloody360` |
| server implementation | [CLASS_SKILL_IMPLEMENTATION_AUDIT.md](CLASS_SKILL_IMPLEMENTATION_AUDIT.md) — *"Radial damage and attached victim/user effects verified"* |
| new-character default bar | listed in `NEW_CHARACTER_QUICKBAR_SKILL_SLOTS[warrior]` |

`TalentDatabase` (`talent_protocol.py`) reads `_Template_SkillTalent` by id, so
the row is reachable by the live tree. The most likely explanation for it not
appearing is a character below level 14.
