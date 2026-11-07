# Defeat the Undefeatable map audit

Scope: the eleven routes shown by the operator (Circus Monstrorum, Yolotl,
Teleportarium, Desecrated Sanctum, Tegan's Ceremonial Hall, Khalys' Elysium,
Hjalgrimur, Fortress Hall, Dragan's Refuge, Creepy Castle, and Djinn's
Chamber). The map/static databases are deployment assets and are ignored by
Git, so corrections below are implemented in the server's data-loading and
map-event compatibility layer.

## Per-map result

| Route | Physical map | Authored encounter chain | Audit result |
|---|---|---|---|
| Circus Monstrorum | `e_bos_arena_01` | Nine altar interactions select nine separate boss EventSets (655–663). The three shrine-clear signals reveal the next altar group; the boss kill closes the active fight. | The nine boss placements are separately gated and share the arena anchor intentionally. |
| Yolotl | `e_bos_02_balor_01` | `BOSS_start_trigger` → `BOSS_spawn` (159) → Balor → `BOSS_death`. | Boss row and trigger agree on the entrance lifecycle. Exit completion now also recognizes the actual Balor SpawnPoint template. |
| Teleportarium | `e_bos_02_teleportarium_01` | `BOSS_start_trigger` → `BOSS_spawn` (159) → Destructor → `BOSS_death`. | The only classic boss-room main SpawnPoint was unconditionally loaded (`EventSetRow=-1`). The server now treats its row as EventSet 159, matching the sibling rooms. |
| Desecrated Sanctum | `e_bos_02_gorga_01` | `BOSS_start_trigger` → `BOSS_spawn` (159) → Gorga → `BOSS_death`. | Boss gate and actual scaled Gorga template are covered. |
| Tegan's Ceremonial Hall | `e_bos_02_herald_01` | `BOSS_start_trigger` → `BOSS_spawn` (159); the 78 authored add placements use `BOSS_start` (279). | Main boss, add gate, and actual scaled Herald template are covered. Difficulty-family selection remains in the existing spawn selector. |
| Khalys' Elysium | `e_bos_02_khalys_01` | `BOSS_start_trigger` → `BOSS_spawn` (159) → Khalys → `BOSS_death`. | Boss gate and actual scaled Khalys template are covered. The separate Andermight wave point remains outside the boss gate by design. |
| Hjalgrimur | `e_bos_02_sigrismarr_01` | `BOSS_start_trigger` → `BOSS_spawn` (159) → Sigrismarr → `BOSS_death`. | Boss gate and actual scaled Sigrismarr template are covered. |
| Fortress Hall | `e_bos_grimmag_pw_boss` | `BOSS_start_trigger` → `BOSS_spawn` (159) → Grimmag → `BOSS_death`. | Boss gate and actual Grimmag template are covered. |
| Dragan's Refuge | `e_bos_02_dragan_01` | An ungated Dragan-spawner actor starts `e_bos_02_dragan_fight_01` after elimination; add EventSet 635 opens after its authored delay. Reward chests and the Kingshill exit use EventSet 157 (`BOSS_death`). | The generic boss-death signal already names both Dragan boss variants. The Kingshill exit signal omitted those templates, and its consuming event omitted Dragan's map; both links are now repaired. |
| Creepy Castle | `e_bos_02_gwenfara_01` | `BOSS_start_trigger` → Gwenfara phase 01 (159); pumpkin waves use EventSets 636 and 637; final phase is `e_bos_02_gwenfara_phase_02`. | Phase gates are kept intact. Final-phase template and map are now included in boss completion and Kingshill exit handling. |
| Djinn's Chamber | `e_bos_02_djinn_01` | Lamp interaction opens the phase chain (634); adds/lightning use 629–633; final phase is `h0008_boss_djinn_lamp_phase_04`. | Lamp-driven phases are kept intact. Final-phase template and map are now included in boss completion and Kingshill exit handling. |

## Event portal and chest comparison

- Bigpoint's 2026 FAQ describes the portal as opening after the Circus
  Monstrorum altar bosses, then lists ten portal bosses: Grimmag, Khalys,
  Herald, Sigrismarr, Gorga, Destructor, Balor, Djinn, Dragan, and Gwenfara.
  That matches the ten boss routes in the attached screenshots. The FAQ says
  Heart of Lava drops from those portal bosses. Its published schedule is
  June 25–July 8, 2026, so it is used here for encounter/content reference,
  not to alter the currently configured schedule.
- DSOFun's chest guide lists 40, 80, and 160 Keys of the Fearless for bronze,
  silver, and gold chests respectively. Circus Monstrorum's map rows use
  40/80/160 for copper/silver/gold and additionally include a 20-key wooden
  chest, so the three main tiers agree.
- Circus Monstrorum's chests are enabled by EventSet 62
  (`boss_bonus_event_02`). Their loot list IDs are
  `CHEST_lootlist_e_bos_SUPERCHEST_WOOD`, `_COPPER`, `_SILVER`, and `_GOLD`.
  These are now handled by the DTU superchest reward path in
  `services/map_events.py`, so they no longer fall through to the empty loot
  table path. The wood and copper chests use the bronze pool; silver and gold
  use their corresponding DSOFun pools. Its page omits odds and quantities,
  so each reward choice currently has equal odds and grants one item (except
  the documented two polished trapezoid gems). Random equipment, gems, yarn,
  and the boss-pet subset available in the local item database are rolled at
  opening time. The guide's two silver jackpot labels (“Support Stone” and
  “Feat Stone”) are mapped to the local DTU-specific common jewels
  `jewel_unique_damage_GroupHpReg_IncreasedResource_common` (Jewel of
  Encouragement) and
  `jewel_unique_damage_GroupResourceReg_IncreasedHp_common` (Jewel of
  Contribution), respectively.

## Server corrections

- Normalized the Teleportarium Destructor spawn row to EventSet 159 at both
  initial encounter load and event-driven spawn load. This preserves the
  authored entrance trigger and prevents a boss from bypassing it.
- Extended `e_bos_02_exit_kingshill_start_01` and
  `BOSS_death_start_signal` with the real boss templates used by the arena
  SpawnPoints. The generic boss-death signal remains unrestricted across all
  maps. The extracted Kingshill exit event listed Djinn, Dragan, and Gwenfara
  in its kill signal's map filter, but omitted those maps from the consuming
  event; the server now includes them.
- No mob health, damage, or add counts were guessed or retuned. The available
  map assets provide authored spawn placement and phase gates; arbitrary
  balance changes would need a target difficulty or observed gameplay defect.
- Implemented the Circus superchest reward choices using verified local item
  templates and DSOFun's bronze/silver/gold reward lists, including the two
  DTU encouragement/contribution jewels in the silver jackpot. Retail odds are not
  published there, and the bronze pet choice is limited to the six matching
  boss-pet templates present in the local database; live-client chest opening
  is still needed to confirm the full retail reward presentation.
- Added an 8% Heart of Lava drop to the final boss in each of the ten DTU boss
  routes. It is gated on the `boss_bonus_event_02` schedule and difficulty
  index 3 (Fatal) or higher; the chance and single-item stack stay fixed at
  higher difficulties.
- Added a guaranteed 1–3 Keys of Defeat, a 30% Feather of a Gryphon chance,
  and a 15% random Doll's Body lootbox chance to those ten final bosses.
- Replayed EventSet 157 after a DTU boss dies so arena reward chest props are
  sent even when the death transition reports no newly enabled set rows.
- Mapped the one-key normal and ten-key golden DTU arena chests to difficulty-
  scaled gold, Draken, Andermant, one map-tier gem, and the requested Newborn
  Heart, Relentlessness, Vanquisher, and class-specific adornment chances.
  Gold-embroidered Silk Fabric is guaranteed as one difficulty-tiered item.
  The current static item database still has no Insignia of Honor template.

## Verification boundary

The audit compares the extracted map rows, EventSet flags, signal selectors,
and server event/spawn paths. Python syntax compilation passed for the three
changed gameplay modules. No automated tests or live-client runs were
performed.
