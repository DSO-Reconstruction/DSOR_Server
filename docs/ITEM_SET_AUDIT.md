# Item set implementation audit

Are all 237 item sets in `static.db4` actually working on this server?

> **Status after the proc fix: no set is entirely dead any more.**
>
> | | before | after |
> | --- | ---: | ---: |
> | sets with at least one live rule | 215 | **228** |
> | fully implemented sets | 97 | **185** |
> | entirely unimplemented sets | 13 | **0** |
> | modifiers implemented | 72% | **88%** |
>
> `SkillStatusEffect` procs **and** `ON:Kill` / `ON:Hit` actor triggers are now
> resolved by [item_set_procs.py](../../item_set_procs.py) and applied from the
> combat hooks. What remains unimplemented needs game systems this server does
> not have at all (§3.2), not more plumbing.
>
> Two corrections to the original audit are folded in below: `SkillDamage` and
> friends were never broken (they are handled at cast time in
> `local_combat_state.py`, which the first pass did not check), and `CoolDown`
> has no server-side system to modify at all.

Reproduce with [audit_item_sets.py](../../audit_item_sets.py):

```
python audit_item_sets.py            # summary
python audit_item_sets.py --broken   # sets with no live rule (now none)
python audit_item_sets.py --set set_dwarf_bgh_heredur_intensified
```

---

## 1. Headline numbers

| | sets |
| --- | ---: |
| fully implemented | **185** (was 97) |
| partially implemented | **43** (was 118) |
| entirely unimplemented | **0** (was 13) |
| no modifiers at all | 9 |
| failed to parse | 0 |

| modifier status | count | share | why |
| --- | ---: | ---: | --- |
| **stat** | 581 | 61% | reaches `calculate_player_combat_stats` ✅ |
| **proc** | 118 | 12% | `SkillStatusEffect` — applied via `item_set_procs` ✅ |
| **immediate** | 72 | 7% | applied by `reconcile_effects` ✅ |
| **trigger** | 42 | 4% | `ON:Kill` / `ON:Hit` — applied via `item_set_procs` ✅ |
| **cast** | 23 | 2% | per-skill, applied in `local_combat_state` ✅ |
| deferred | 58 | 6% | triggers with no server-side event ❌ |
| unknown | 31 | 3% | no branch anywhere ❌ |
| cosmetic | 17 | 1% | `Localize` etc., no gameplay expected — |
| client | 10 | 1% | `CoolDown` — client-side system, server has none — |

---

## 2. What works, and proof

Two paths are live. Both were verified end-to-end against the real mage
inventory in `dso_server/data/db/players.sqlite3` (level 100, six active set
tiers):

```
no set sources          max damage  6,432,317
+ set modifiers                     7,504,370   (+16.7%)
+ immediate effects too             7,933,191   (+23.3%)
```

**Path 1 — set attribute modifiers.** `player_init_state.py` passes the whole
`ItemSetEvaluation.modifiers` tuple as `modifier_sources`, and
`_status_instructions` converts anything with `.kind`/`.arguments` straight into
a `CombatModifier`. So `Damage`, `Resistance`, `CriticalValue`,
`MaxHealthPoints`, `Speed`, `MaxSkillResource`, `BlockValue`, `Armor`,
`LifeLeech`, `DamageReflection`, `HealthPointsRegeneration` and
`DamageFactorPvE` all land.

> Note: the `ItemSetEvaluation.attribute_modifiers` *field* is indeed never
> read — but that is a red herring. The same modifiers arrive through the
> full `.modifiers` tuple, which is what gets passed.

**Path 2 — immediate actor effects.** `ActorStatusEffect:AddAE,<effect>,ON:Immediate`
is turned into a real status effect by `reconcile_effects`, and that effect's
own `StartModifiers` are then fed through the same stat pipeline. This is why
the Stormbound Attunement buff works.

---

## 3. What does not work

### 3.1 `SkillStatusEffect` — 118 modifiers — **FIXED**

Previously parsed into `ItemSetEvaluation.skill_effect_rules` and never read,
which is why 13 sets were entirely dead: their whole identity is procs.

Now implemented in [item_set_procs.py](../../item_set_procs.py):

1. `collect_rules()` turns `skill_effect_rules` into typed `SetProcRule`s
   (operation, skill group, effect id, chance).
2. `rules_for_skill()` matches against the cast skill. A skill's `Groups`
   column contains its own id, so one comparison covers both "this exact
   skill" and "any skill in this family".
3. `roll()` applies the authored `C:` probability.
4. `build_definitions()` reuses
   `LocalCombatState._load_victim_status_effects` rather than reimplementing
   duration/stack/aura resolution, so procs behave identically to a skill's
   own `VictimStatusEffects`.

Applied in two places, because the targets diverge:

| target | share of procs | where |
| --- | ---: | --- |
| `addUE` / `modUE` (caster) | 60% | `player_combat.py` on cast, `player_projectiles.py` on impact — split by the rule's authored `ON:` |
| `addVE` / `modVE` (victim) | 37% | `combat_resolution.py`, in `_victim_effect_entries_by_target` |
| `addLE` (location) | 2% | resolved but not yet applied |

The proc is rolled **once per skill index**, not once per resolution — a
multi-target cast otherwise rolls the same bonus separately for every enemy it
clips, which is not what the authored chances assume.

Three corrections to the first version of this resolver, all found while
tracing why *Chillingly Cold* granted no Cold Chill stacks:

1. **The rule's own metadata was discarded.** `build_definitions()` re-encoded
   every rule as `<effect>,C:1.0`, so `D:`, `$N:` and — worst — `ON:` never
   reached the parser. Each definition silently inherited the effect row's own
   duration and the parser's default trigger, which is how an `ON:Kill` bonus
   became indistinguishable from an on-cast one. `SetProcRule.encoded` now
   carries the whole authored tail.
2. **Caster rules default to `skillstart`, not `hit`.** A skill's own
   `UserStatusEffects` column defaults that way and `addUE` *is* that column.
   Resolving them on the impact path left every set whose trigger skill deals
   no damage permanently inert — Battle Cry, Adrenaline and Dwarf-in-the-Box,
   i.e. three of the four *Chillingly Cold* variants.
3. **`AuraTargets:Creator` shells were applied instead of their payload.**
   Several sets name an aura as the caster effect; the buff the wearer is meant
   to receive is that aura's `AuraEffectId` child, hosted on the creator.
   Applying the shell alone delivered a one-second empty aura and nothing else.
   `LocalCombatState._load_aura_owner_effects` (the generalized
   `_load_location_owner_effects`) now resolves the child alongside the shell.

The full *Chillingly Cold* chain is a good worked example, because each link is
a different mechanism:

```
cast Frost Nova
  -> 4) SkillStatusEffect:addUE,frostnova,chillingly_cold_2025_aura_mage
       aura shell, 1s, AuraTargets:Creator
  -> chillingly_cold_2025_trigger_mage        (30s buff on the caster)
       StartModifiers: modUE,<every mage skill>,chr2025_set_coldchill,C:1.0,D:5.0
  -> every mage skill's own UserStatusEffects entry
       chr2025_set_coldchill,C:0.0,On:skillstart   -- authored disabled, raised to C:1.0
  -> chr2025_set_coldchill                    (5s, MaxStackSize 30, +5% damage/stack)
  -> at the accepted 30th stack the server dispatches the class-matched
       icy_wrath_2025_mage max-stack effect and applies its authored
       StopStatusEffect cleanup to the server runtime ledger
  -> Icy Wrath's modVE,player_skills rule enables the frozen_plain_aura_2025
       victim-effect placeholder on the class's authored combat skills
  -> the server resolves the timed aura's frozen_plain_effect_2025 child
       around the struck monster, including its periodic Ice damage
```

The same flow is implemented for Ranger (`Adrenalin`), Warrior (`Battlecry`),
and Dwarf (`JackInTheBox`). The 3-piece `mh_rings_set_aura` is also resolved
server-side: its `mh_rings_set_buff` child is refreshed on nearby hostile
monsters, where `SkillDamage:$0,relative,MonsterSkills` participates in the
authoritative monster-skill damage calculation. Permanent equipment sources
renew the short monster-side lease while equipped; timed aura sources remain
bounded by their authored expiry.

The `mod` operations are genuinely modifications here, not disguised adds:
every class skill already authors `chr2025_set_coldchill` at `C:0.0`. That is
why `_modified_player_skill_geometry` is right to treat a `mod` whose target is
absent as a no-op.

The tier's other half, `CoolDown:0.6,relative,frostnova`, needs nothing here:
the client owns cooldowns and reads the same item-set table itself (§3.3).

### 3.2 Non-immediate `ActorStatusEffect` — 100 modifiers — **42 FIXED**

`ItemSetDatabase` applies only `ON:Immediate`/`ON:immediatemanaged`; everything
else landed in `actor_effect_rules` unread. Breaking the 100 down by trigger
shows most of the remainder is not a plumbing problem:

| count | trigger | status |
| ---: | --- | --- |
| 37 | `kill` | **implemented** — fires on lethal resolutions |
| 5 | `hit` | **implemented** — fires on any damaging resolution |
| 24 | `pickuphealthglobe` | no health-globe system exists server-side |
| 16 | *(none)* | all `modAE` — see below |
| 4 | `hitblocked` | no block resolution |
| 4 | `lowhealth` | needs a health-threshold watcher |
| 4 | `pickupingredientitem` | no pickup event |
| 4 | `costskillresource` | no resource-spend hook |
| 2 | `disabled`, `maxstacksizeearthquake` | special-cased mechanics |

**`modAE` is not an event rule.** All 16 no-trigger entries use it, e.g.
`ActorStatusEffect:modAE,item_ammon_set_invigorate,$1:0.15`. It *edits the
parameters of an already-applied effect* — the "the damage buff of X is
increased to Y" tier-upgrade pattern — and is deliberately excluded from
trigger collection rather than mis-fired on kill.

**25 of the 42 kill/hit effects declare `StatusEffectDuration 0`** (the
`item_pw_mortis_set_*` family). Those are instantaneous variable writes, not
timed buffs; sending one as a status effect makes the client log *"ignoring
non-permanent effect with total-duration 0"*. They are filtered out in
`resolve_for_trigger` rather than downstream, so the omission is visible in one
place. Applying them properly needs a non-status-effect path.

### 3.3 Kinds with no branch anywhere — 31 modifiers

The first version of this audit put 64 modifiers here by checking only
`player_combat_stats.py`. That was wrong: `local_combat_state.py` handles a
second family of **per-skill** modifiers at cast time, scoped by skill id via
`_skill_scope_matches` — `SkillDamage`, `SkillHitDamage`, `SkillRange`,
`SkillTiming`, `BulletCount`, `TalentLevelBonus`, `SkillSequence` and others.
Those were never broken.

What genuinely has no implementation:

| count | kind | what it should do |
| ---: | --- | --- |
| 16 | `QuestDropFactor` | boost quest item drops |
| 4 | `HealthGlobeBonus` | health globe value |
| 4 | `AdditionalSkillCost` | extra resource cost |
| 4 | `IgnoreSkillCost` | free casts |
| 2 | `ResourceCost` | resource cost change |
| 1 | `SummonAnotherMonsterAmount` | extra summons |

Separately, **`CoolDown` (10 modifiers) is not a server concern at all.** The
server never reads `_Template_Skill.CoolDown` and enforces no cooldowns; the
client owns that system and reads the same item-set table itself. These are
counted as `client`, not broken.

---

## 4. Previously dead sets — now live

These 13 had no working gameplay modifier at all; every one was proc-only, so
all of them are fixed by §3.1:

```
set_cny2026_dwarf                     set_dwarf_sewers_02       set_ranger_sewers_02
set_cny2026_ranger                    set_mage_sewers_02        set_warrior_sewers_02
set_cny2026_warrior                   set_ranger_bgh_heredur
set_dwarf_bgh_heredur                 set_ranger_bgh_heredur_intensified
set_dwarf_bgh_heredur_intensified     set_warrior_bgh_heredur
                                      set_warrior_bgh_heredur_intensified
```

What they promise, from the shipped locale — now actually delivered:

| set | tooltip promise |
| --- | --- |
| `set_dwarf_bgh_heredur_intensified` | *"60% chance that hits with Quick Shot cause your currently active …"* |
| `set_warrior_sewers_02` | *"Every time you use a skill there is a 5% chance Poisonous Strength will be granted"* |
| `set_cny2026_dwarf` | *"While Iron Dwarf is active, your Maximum Health is increased by 500%…"* |

Worth noting the class asymmetry this removes: the dwarf, ranger and warrior
`bgh_heredur` variants were dead while the **mage** one worked, because the
mage version is built from stat modifiers and the others from procs. Players
wearing "the same" set got very different value depending on class.

## 5. The worst partial sets

Sets that work, but where most bonus lines are silently missing:

| set | live | dead |
| --- | ---: | ---: |
| `set_dragan_03_2025_ranger` | 4 | **20** |
| `set_dragan_03_2025_dwarf` | 2 | 9 |
| `set_dragan_03_2025_mage` | 4 | 6 |
| `set_dwarf_alliance_set` | 1 | 5 |
| `set_dwarf_ammon_T1` | 1 | 5 |
| `set_dwarf_dragan_event_lvl45` | 2 | 5 |
| `set_dwarf_pw_mortis` | 2 | 5 |
| `set_mage_alliance_set` | 1 | 5 |

These are worse than the fully-dead ones from a player's perspective: the set
"works" — stats move, the tooltip is intact — so there is no signal that four
fifths of its bonus text is decoration.

---

## 6. What to fix, in order

1. ~~Consume `skill_effect_rules`~~ — **done**, §3.1.
2. ~~Honour `ON:Kill` / `ON:Hit` triggers~~ — **done**, §3.2.
3. **Health globes** — 24 trigger modifiers plus 4 `HealthGlobeBonus` stat
   modifiers, the largest single remaining item. Needs a pickup entity and
   event; nothing exists today.
4. **Zero-duration trigger effects** — 25 kill/hit effects that write actor
   variables instead of applying a buff. Needs a non-status-effect application
   path (§3.2).
5. **`modAE` parameter editing** — 16 modifiers; a tier-upgrade mechanic that
   rewrites an applied effect's `$N` values.
6. **Apply `addLE` procs** — 3 modifiers; they resolve today but nothing places
   a location effect.
7. Long tail: `QuestDropFactor`, `hitblocked`, `lowhealth`, the skill-cost
   kinds.

`CoolDown` and `SkillDamage` were on this list in the first draft and have been
removed: the former is client-owned, the latter already worked.

Items 1 and 2 share infrastructure with the quest engine's event hooks
(see [QUEST_FEASIBILITY.md](QUEST_FEASIBILITY.md) §3) — the same "on kill / on
hit / on skill use" dispatch points serve both.

---

## 7. Method and limits

Classification is static: each modifier's `kind` and `ON:` trigger are compared
against the kinds the stats pipeline branches on (grepped from
`player_combat_stats.py`) and the trigger guard in `item_set_protocol.py`.
The two live paths were then confirmed numerically with a real inventory.

Not verified:

- **Whether each live modifier is numerically *correct***, only that it moves
  the stat. A wrong scaling factor would still count as "working" here.
- **Client-side display.** `ActorStatsUpdate` (`0x007B`) is defined but sent by
  no service, so the client derives its own displayed numbers; a modifier can be
  authoritative server-side and still not show in the character panel.
- **Live equip.** `inventory_map.py` re-evaluates sets and reconciles effects
  but never recomputes combat stats, so set changes made while logged in may not
  take effect until reconnect. This affects all sets, not just new ones.
