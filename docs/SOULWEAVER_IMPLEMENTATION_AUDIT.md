# Soulweaver / Necro class audit

This audit covers class id `4` (`necro`) in the current server content. The
data review used `data/db/static.db4`, the shipped English client skill and
talent text, and REA's static table inspection. Evidence references from the
REA session: database schema `ev_89c23b74f29c863a3169b23ec99b8f9168be33a6ea4bc5836b2bc047a569c1f7`,
`_Template_Skill` inspection `ev_81eaee2d4ea87425ce4d890c5af44fbdd18f9da1d7bf177cc1bf040a2771832b`,
and the earlier `_Template_SkillTalent`, `_Template_Talents`, and
`_Template_Talent` inspections `ev_c54e67abb4da9596babde3f168f92ce3bb8e34fe9929695f62d6296314203069`,
`ev_582770f984c111d0876e3913c3674e239245bb94ff60fa7df86cdcd21121cbb9`,
and `ev_b9a20d5d03e173a83a27daf6f6aa164fbe92183986cdb384f0912d0b4654555d`.

REA evidence establishes the shipped data and descriptions. It does not prove
that every animation, hit frame, talent combination, or damage formula has
been reproduced in a live client session. The attached server log is from
before the Necro core-equipment bootstrap mapping was added: its
`KeyError: 'necro'` at `protocol/inventory_codec.py` is fixed in the current
working tree. That log does not contain a Necro cast or a Soul Energy event.

## Soul Energy

| Rule | Server behavior |
|---|---|
| Pool | Soulweaver only; maximum `50`, read from `MaxSoulResource`; starts at the saved value, clamped to the maximum. No passive regeneration is authored. |
| Nearby enemy death | `+1` for each enemy that dies within 15 world units. PvE deaths are handled from the map kill event. A nearby opposing player killed in an arena also grants the base `+1`. |
| Proximity radius | 15 units is an implementation estimate based on the Skeleton Archer's authored attack range; the localized text only says “nearby”. Confirm this range in game. |
| Wraith/skeleton death | `+10` when an owned `minion_necro_skeleton_*` actually dies to a hostile hit or is sacrificed by Army of the Dead. Lifetime expiry and owner-range cleanup do not count as deaths. |
| Soul Globe chance | Wraith Master rank adds the authored extra chance on nearby PvE enemy deaths. Because the server has no ground Soul Globe actor/pickup path, a successful one-energy globe is automatically credited to the pool. |
| Direct mark proc | Wraith Master has a 15% chance on the authored Mortis mark application to grant `+5`. |
| Soul Call node 05_01 | The skill adds `+10` immediately when that talent effect is active. |
| Army node 06_02 | Its eight-second summon-cooldown effect ticks `+1` Soul Energy per second and halves Archer/Warrior/Mage summon cooldowns. The 06_01 variant extends the Army aura and doubles its tick cadence. |
| Spending | `_Template_Skill.SoulCost` is normalized against a 100-point cost scale: Skeleton Warrior costs `10`; Skeleton Mage costs `20`. Payment is atomic with the skill-resource debit, so a failed cast spends neither pool. |
| Overflow | Energy above 50 becomes one `necro_soul_affinity` stack per excess point, capped at 50 stacks. Each stack gives the authored `+1%` summon damage for five seconds. |
| Replication/save | Soul Energy is sent in the third `ActorStatsUpdate`/`InventoryInfo` resource field, mirrored in inventory state, saved with the account, and preserved over map transitions and respawns. |

The client text describes Soul Globe pickup and nearby death rewards. The
server currently auto-credits a successful one-energy globe rather than
spawning a visible ground item. This keeps the authoritative resource working
but is a presentation difference.

## Active skills

All 15 Necro skill rows have static data and are looked up through the shared
skill geometry, status-effect, cooldown, projectile, or summon paths. “Mana”
below means the class's secondary skill-resource pool.

| Unlock | Skill | Authored cost / cooldown | Effect and class interaction |
|---:|---|---|---|
| 1 | Bone Spikes | `0.05` resource gain; no cooldown | Ranged basic attack. Builds Bone Shield and carries Bone/Mortis mark interactions through its skill effects. |
| 3 | Skeleton Archer | `0.30` Mana; 5 s | Summons `minion_necro_skeleton_archer`; its hit/death bonuses depend on active talent nodes. |
| 5 | Soul Bolt | Free basic attack | Nether Magic projectile; applies/uses Unholy Pact mark and has marked-target and pierce talents. |
| 7 | Bone Spear | `0.30` Mana; 2 s | Ranged multi-ammo attack, damage and slow; Bone Shield interactions. |
| 9 | Wraith Decoy | `0.20` Mana; 10 s | Teleport/decoy, stun or cleanse/immunity/speed effects from its selected node. |
| 11 | Bone Prison | `0.50` Mana; 50 s | Ground trap/aura. Talent choices switch between stun, instant cast, and physical/armor-break behavior. |
| 13 | Skeleton Warrior | `0.30` Mana + `10` Soul; 10 s | Summons `minion_necro_skeleton_warrior`; its node can add summon capacity, healing, or player maximum health. |
| 15 | Death Hound | `0.50` Mana; 15 s | Summon/paired behavior; Bone/Mortis/Wraith specializations alter its duration, number, attack behavior, and damage. |
| 17 | Skeleton Mage (Bone Mage) | `0.50` Mana + `20` Soul; 15 s | Summons `minion_necro_skeleton_mage`; piercing, summon cap, damage, and damage type depend on node/mastery. |
| 19 | Death's Gaze | `0.30` Mana; 20 s | Cone hit; applies mark at rank 10. Mortis nodes add stun, slow, or a target damage reduction. |
| 21 | Bone Reforge | `0.20` Mana; 45 s | Self buff/heal/Bone Shield. Per rank raises health/healing/shield; Wraith mastery can add attack speed for Wraiths. |
| 23 | Soul Call | `0.50` Mana; 30 s | Five-second heal/buff for player and summons. Node 05_01 also restores 10 Soul; other nodes increase summon attack/damage or grant brief immunity. |
| 25 | Bone Storm | `0.80` Mana; 60 s | Multi-hit attack using Bone Shield stacks. Reforge ranks add duration; node 06_01 preserves shield and adds damage. |
| 27 | Army of the Dead | `0.50` Mana; 20 s; 15 ammo | Eight-second ground aura; node 06_02 adds Soul ticks and summon cooldown reduction. It sacrifices owned skeletons unless node 06_03 is selected. |
| 29 | Shadow of Mortis | `0.80` Mana; 50 s | Eight-second damage aura; Death Hound ranks and Death's Gaze ranks improve it; 06-series nodes change mark detonation/attack pattern. |

The base rows use the same broad PvE/PvP difficulty range (`MinDifficulty=0`,
`MaxDifficulty=10`); the data has no separate class-wide PvP skill table.
Status effects carry separate `DP` durations where provided. Skill rank
effects include the authored damage, cooldown, range, pierce, mark, duration,
and summon-cap growth described in the client text.

## Skill talent nodes

The six three-node branches are mutually exclusive within each branch. All 18
node status effects are marked `DisabledMatchPvP` in the shipped data.

| Branch | Node 01 | Node 02 | Node 03 |
|---|---|---|---|
| 01 — Bone Spikes / Archer / Soul Bolt | **Massive Boning:** faster Bone Spikes; Archer hits restore Mana per enemy; Soul Bolt restores Mana. | **Bone-A-Palooza:** Bone Spikes chains twice more; +1 Archer; Soul Bolt gains damage vs marked enemies. | **Boned Up:** Bone Spikes grants Bone Shield; active Archer gives a chance for nearby deaths to grant Soul; Soul Bolt pierces two more targets. |
| 02 — Bone Spear / Wraith Decoy | **Spry Bones:** Bone Spear cooldown reduction; Decoy cleanses debuffs and grants brief immunity. | **Bone Idle:** Bone Spear adds slow/attack-speed effects; Decoy explodes for 200% base damage and stuns. | **Bone Crusher:** Bone Spear pierces three more targets; Decoy grants movement speed. |
| 03 — Bone Prison / Warrior / Death Hound | **Skin and Bones:** Bone Prison is instant; Warrior hits heal; Mortis build extends Hound duration. | **Bone Crypt:** Prison stun extension; +1 Warrior; Mortis build adds a Hound. | **Bone Cleaver:** Prison deals physical damage and reduces armor instead of stunning; Wraith build boosts player maximum health while Warrior is active; Mortis Hound becomes passive and boosts Nether damage. |
| 04 — Mage / Death's Gaze | **Bonehead Move:** Mage pierces more; Mortis Gaze stuns marked targets. | **Wishbone:** +1 Mage; Mortis Gaze slows. | **Bone Deposit:** active Mage boosts damage; Mortis Gaze reduces target damage. |
| 05 — Bone Reforge / Soul Call | **Boning Up:** Reforge's Bone Shield damage doubles without consuming the shield; Soul Call grants +10 Soul. | **Bone-Deep:** Reforge adds shield stacks; Soul Call boosts Wraith attack speed and damage. | **Bone Appétit:** Reforge raises maximum health/resistance; Soul Call makes Wraiths briefly invulnerable. |
| 06 — Bone Storm / Army / Shadow | **Bone Synergy:** Storm preserves Bone Shield and adds damage; Army lasts longer/fires faster; Shadow detonates marks in an area. | **Lazy Bones:** Storm duration increases; Army grants +1 Soul per second and halves skeleton-summon cooldowns; Shadow attacks faster. | **Bone Saver:** Storm damage is reduced; Army does not sacrifice summons and uses a fixed damage bonus; Shadow narrows its attack for higher damage/repeat chance. |

## Class passives and masteries

| Passive tree / mastery | Authored effect |
|---|---|
| Wisdom — Soul Distillation | Adds Mana capacity. This is an Ancient Wisdom/Knowledge passive, not Soul Energy regeneration. |
| Wisdom — Soul Siphon | Hitting an enemy while below 33% health restores Mana. |
| Wisdom — Soul Surge | Critical hits can grant the temporary critical-value effect, subject to its cooldown. |
| Group aura — Ice resistance | Shares Ice resistance in the authored group radius (20 units). |
| Group aura — Critical damage | Shares critical damage in the authored group radius (20 units). |
| Group aura — Resource maximum | Shares maximum Mana in the authored group radius (20 units). |
| Bone Master | Physical resistance/penetration; Death Hound presence adds physical damage; marks reduce armor and can explode/spread on death; converts Bone Mage damage toward Physical. Ranks raise armor and Bone Shield damage. |
| Mortis Master | Nether Magic resistance/penetration; converts Bone Spikes and Warrior damage toward Nether Magic; adds Bone Spikes mark chance and a Reforge Nether buff. Ranks raise resistance/penetration and mark reduction. |
| Wraith Master | Elemental resistance/penetration; direct mark chance can grant +5 Soul; Death Hound presence boosts Wraith damage and Reforge affects Wraith attack speed. Ranks increase resistance/penetration and extra Soul Globe chance. |
| Spell Mastery | Elemental damage, resistance, and resistance reduction; ranks add short Spell Haste stacks and cooldown/duration interactions. |
| Summoning Mastery | Summon damage and summon growth; +2 Bone Mage ranks and +1 Shadow/Reforge ranks; higher ranks improve summon damage/movement, owner survivability, Army damage, and Wraith Decoy cooldown. |

## PvE / PvP boundary

* PvE enemy deaths award nearby Soul Energy. In PvP, an opposing player kill
  awards only the base nearby `+1`; the Archer and Wraith Master extra-drop
  chances are suppressed because their nodes/mastery are disabled in matches.
* The 18 skill nodes and Bone/Mortis/Wraith masteries have the static
  `DisabledMatchPvP` tag. PvP runtime filtering now excludes these effects
  from server modifier calculations and removes already-active copies on
  arena entry. Their saved selections are retained for the next PvE map.
* Spell Mastery and Summoning Mastery do not carry that flag and remain active.
  Arena-wide rules also disable Knowledge and Group passive trees, including
  Soul Distillation/Siphon/Surge and the three group auras.
* Soul costs remain on Skeleton Warrior and Skeleton Mage skill rows. Soul
  persists through map transitions and respawn; there is no arena-only refund
  or free-cast override in the inspected data. PvP resource pacing therefore
  depends on saved Soul and nearby player deaths.

## Server coverage and open verification

The current implementation now has an authoritative third resource pool,
Soul costs, immediate and timed Soul modifiers, cap/overflow handling, PvE
nearby-kill gains, PvP player-kill gains, PvE/PvP skeleton-death gains, Army
sacrifice, and the class equipment bootstrap mapping. PvP modifier filtering
follows the static `DisabledMatchPvP` talent metadata.

Two data/engine limits remain visible in this review:

1. “Nearby” is not assigned a numeric radius in the locale text. The 15-unit
   value is inferred from the Skeleton Archer range and needs a retail-client
   comparison.
2. Army of the Dead's UI/damage metadata refers to the actor variable
   `necro_wraith_summon_count`. A source search did not find a server writer for
   that variable. Skeleton sacrifice and Soul reward now run server-side, but
   the damage scaling against the exact active summon count still needs a live
   comparison.

REA's Ghidra-backed binary decompilation was not available in the earlier
session because the REA worker did not inherit `GHIDRA_INSTALL_DIR`; the
client install path (`E:\\ghidra_12.2_PUBLIC`) was supplied. This audit uses
REA's static database inspection and shipped localization rather than claiming
binary-level confirmation. A Necro login, casts, Army sacrifices, Soul spend,
over-cap stacks, and arena round should be exercised in game before calling
every behavior fully verified.
