# Jewel audit — October 29, 2026

All 317 jewel templates were individually inventoried. Catalogue extraction does not establish that every behavior works in live gameplay.

## Runtime corrections

- Stackable jewel procs retain their occurrence count instead of resetting to zero on every activation. This fixes the shared stack bookkeeping; per-jewel stack-limit behavior still requires runtime checks.

- Completed the missing damage-range field in meteor application records.
- The mage Eternal Scorn (`jewel_buff_xmas_event_mage`) now has a deterministic end-to-end event regression check: an Ice Missile hit starts the 5 m location aura and a mob inside it takes 150% of the supplied damage range as Ice damage. Live client/map behavior remains to be confirmed.
- Ranger Eternal Scorn (`jewel_buff_xmas_event_ranger`) now has a regression check confirming its meteor location comes from the successful Multishot target's authoritative world position. Repeated location procs are limited by the temporary 2-second jewel proc cooldown.
- PvP impacts now provide the triggering skill identity to jewel dispatch.
- Jewel cooldown children are now honored by the same mechanism as rune cooldowns. No current jewel cooldown child was found to be affected by this correction.
- `HitFrost` was incorrectly dispatched to the attacker when their target was frozen. It now dispatches to the wearer only when that wearer is already frozen and takes damage, in both PvE and PvP. Outgoing attacks against frozen targets no longer trigger Frozen Heart healing.
- Triggered equipment-jewel status effects now have a 2-second minimum proc cooldown for temporary testing, including location effects such as Ranger Eternal Scorn. Authored cooldowns longer than 2 seconds are preserved.

## Runtime coverage flags requiring investigation

- `debuff_cc_mark / status-modifier / SkillHitDamage`
- `debuff_cc_mark / status-modifier / SkillPunchThrough`
- `debuff_cc_mark / status-modifier / SupportScore`
- `item_pw_herald_warrior_bonus_new / status-modifier / SkillTiming`
- `jewel_dra_dwarf_posbomb_physdmg / status-modifier / TargetAddDamage`
- `jewel_dra_mage_frozenorb_switch / status-modifier / SkillStatusEffect`
- `jewel_dra_ranger_deadlyblow_armorpen / status-modifier / TargetReduceResistance`
- `jewel_dra_warrior_mightybash_trig_effect / status-modifier / TriggerSkillDamage`
- `jewel_dra_warrior_seismicslam_physdmg / status-modifier / TargetAddDamage`
- `jewel_dragon_ranger_mightystab_dmg_reset / status-modifier / CurrStatusEffectDuration`

Unresolved effect references: 0

## Validation limits

Focused jewel, item-set, and location-aura checks: 39 tests passed, including coverage that Eternal Scorn's Ice Missile proc starts its location aura and deals 150% Ice damage to a mob inside it, Frozen Heart only procs on the wearer-side condition, and wearer-buff procs respect the 2-second test cooldown. The incoming-damage routing is implemented for PvE and PvP. Broader combat checks: 76 passed and 73 failed. The causes of those failures, including database version and skill-index assumptions, have not yet been fully isolated. Complete jewel correctness is therefore NOT established; live client behavior still requires in-game verification.

## Individual catalogue inventory

| Jewel template | Trigger / skill scope / target | Result |
| --- | --- | --- |
| `jewel_armor_critRating_damage_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_critRating_damage_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_critRating_damage_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_critRating_damage_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_critRating_damage_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_damage_critRating_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_damage_critRating_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_damage_critRating_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_damage_critRating_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_damage_critRating_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_hitpoints_critRating_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_hitpoints_critRating_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_hitpoints_critRating_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_hitpoints_critRating_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_hitpoints_critRating_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_hitpoints_damage_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_hitpoints_damage_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_hitpoints_damage_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_hitpoints_damage_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_hitpoints_damage_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_resistAll_critRating_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_resistAll_critRating_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_resistAll_critRating_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_resistAll_critRating_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_resistAll_critRating_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_resistAll_damage_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_resistAll_damage_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_resistAll_damage_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_resistAll_damage_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_resistAll_damage_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_resistAll_hitpoints_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_resistAll_hitpoints_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_resistAll_hitpoints_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_resistAll_hitpoints_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_armor_resistAll_hitpoints_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_aura_damage_fire_onHitSkill_dwarf_common` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_fire_onHitSkill_dwarf_epic` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_fire_onHitSkill_dwarf_magic` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_fire_onHitSkill_dwarf_rare` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_fire_onHitSkill_dwarf_uncommon` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_fire_onHitSkill_mage_common` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_fire_onHitSkill_mage_epic` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_fire_onHitSkill_mage_magic` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_fire_onHitSkill_mage_rare` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_fire_onHitSkill_mage_uncommon` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_lightning_onHitSkill_ranger_common` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_lightning_onHitSkill_ranger_epic` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_lightning_onHitSkill_ranger_magic` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_lightning_onHitSkill_ranger_rare` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_lightning_onHitSkill_ranger_uncommon` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_lightning_onHitSkill_warrior_common` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_lightning_onHitSkill_warrior_epic` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_lightning_onHitSkill_warrior_magic` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_lightning_onHitSkill_warrior_rare` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_lightning_onHitSkill_warrior_uncommon` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_aura_damage_poison_common` | immediatemanaged / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_aura_damage_poison_epic` | immediatemanaged / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_aura_damage_poison_magic` | immediatemanaged / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_aura_damage_poison_rare` | immediatemanaged / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_aura_damage_poison_uncommon` | immediatemanaged / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_bgh_dwarf` | default / SimpleShot / location / %100; default / TurretCombat / user / %100 | Inventoried; live behavior unverified |
| `jewel_bgh_key_drop_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_bgh_key_drop_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_bgh_key_drop_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_bgh_mage` | default / frostwind / user / %100 | Inventoried; live behavior unverified |
| `jewel_bgh_ranger` | default / explosiveshot / victim / %100 | Inventoried; live behavior unverified |
| `jewel_bgh_warrior` | default / defiance / user / %100 | Inventoried; live behavior unverified |
| `jewel_boss_gem_drop_common` | immediatemanaged / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_boss_gem_drop_epic` | immediatemanaged / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_boss_gem_drop_magic` | immediatemanaged / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_boss_gem_drop_rare` | immediatemanaged / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_boss_gem_drop_uncommon` | immediatemanaged / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_GroupHpReg_maxResource_onHit_common` | hit / player_skills / user / %10 | Inventoried; live behavior unverified |
| `jewel_buff_GroupHpReg_maxResource_onHit_epic` | hit / player_skills / user / %20 | Inventoried; live behavior unverified |
| `jewel_buff_GroupHpReg_maxResource_onHit_magic` | hit / player_skills / user / %15 | Inventoried; live behavior unverified |
| `jewel_buff_GroupHpReg_maxResource_onHit_rare` | hit / player_skills / user / %17.5 | Inventoried; live behavior unverified |
| `jewel_buff_GroupHpReg_maxResource_onHit_uncommon` | hit / player_skills / user / %12.5 | Inventoried; live behavior unverified |
| `jewel_buff_GroupResourceReg_maxHealthpoints_onHit_common` | hit / player_skills / user / %10 | Inventoried; live behavior unverified |
| `jewel_buff_GroupResourceReg_maxHealthpoints_onHit_epic` | hit / player_skills / user / %20 | Inventoried; live behavior unverified |
| `jewel_buff_GroupResourceReg_maxHealthpoints_onHit_magic` | hit / player_skills / user / %15 | Inventoried; live behavior unverified |
| `jewel_buff_GroupResourceReg_maxHealthpoints_onHit_rare` | hit / player_skills / user / %17.5 | Inventoried; live behavior unverified |
| `jewel_buff_GroupResourceReg_maxHealthpoints_onHit_uncommon` | hit / player_skills / user / %12.5 | Inventoried; live behavior unverified |
| `jewel_buff_HealthGlobeDamage_common` | pickuphealthglobe / global / user / %100; pickuphealthglobe / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_HealthGlobeDamage_epic` | pickuphealthglobe / global / user / %100; pickuphealthglobe / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_HealthGlobeDamage_magic` | pickuphealthglobe / global / user / %100; pickuphealthglobe / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_HealthGlobeDamage_rare` | pickuphealthglobe / global / user / %100; pickuphealthglobe / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_HealthGlobeDamage_uncommon` | pickuphealthglobe / global / user / %100; pickuphealthglobe / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_blocked_energy_common` | hitblocked / global / user / %100; maxstacksizejewelbuffblockedenergy / global / location / %100; maxstacksizejewelbuffblockedenergy / global / location / %100; maxstacksizejewelbuffblockedenergy / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_blocked_energy_epic` | hitblocked / global / user / %100; maxstacksizejewelbuffblockedenergy / global / location / %100; maxstacksizejewelbuffblockedenergy / global / location / %100; maxstacksizejewelbuffblockedenergy / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_blocked_energy_magic` | hitblocked / global / user / %100; maxstacksizejewelbuffblockedenergy / global / location / %100; maxstacksizejewelbuffblockedenergy / global / location / %100; maxstacksizejewelbuffblockedenergy / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_blocked_energy_rare` | hitblocked / global / user / %100; maxstacksizejewelbuffblockedenergy / global / location / %100; maxstacksizejewelbuffblockedenergy / global / location / %100; maxstacksizejewelbuffblockedenergy / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_blocked_energy_uncommon` | hitblocked / global / user / %100; maxstacksizejewelbuffblockedenergy / global / location / %100; maxstacksizejewelbuffblockedenergy / global / location / %100; maxstacksizejewelbuffblockedenergy / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_critical_attackspeed_onHitCritical_common` | hitcritical / player_skills / user / %20 | Inventoried; live behavior unverified |
| `jewel_buff_critical_attackspeed_onHitCritical_epic` | hitcritical / player_skills / user / %40 | Inventoried; live behavior unverified |
| `jewel_buff_critical_attackspeed_onHitCritical_magic` | hitcritical / player_skills / user / %30 | Inventoried; live behavior unverified |
| `jewel_buff_critical_attackspeed_onHitCritical_rare` | hitcritical / player_skills / user / %35 | Inventoried; live behavior unverified |
| `jewel_buff_critical_attackspeed_onHitCritical_uncommon` | hitcritical / player_skills / user / %25 | Inventoried; live behavior unverified |
| `jewel_buff_dtu_weaponmod_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_dtu_weaponmod_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_dtu_weaponmod_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_dtu_weaponmod_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_dtu_weaponmod_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_flames_epic` | hitlowhealth / player_skills / victim / %5; hitlowhealth / player_skills / user / %5; hitlowhealth / player_skills / location / %5; hitlowhealth / player_skills / location / %5; hitlowhealth / frozenorb / bullet / %5; hitlowhealth / frozenorb / bulletdeath / %5; hitlowhealth / thorntrail / bullet / %5; hitlowhealth / MachineGunTurret / location / %5; hitlowhealth / CombatTurret / location / %5; hitlowhealth / CombatTurret / location / %5; hitlowhealth / CoveringFireTurret / location / %5; hitlowhealth / TeslaTurret / location / %5; hitlowhealth / Grenade / bulletdeath / %5; hitlowhealth / ShrapnelShot / location / %5; hitlowhealth / JackInTheBox / location / %5; hitlowhealth / arcanemanifestation / location / %5; hitlowhealth / explosiveshot / location / %5; hitlowhealth / hornetcloud / user / %5 | Inventoried; live behavior unverified |
| `jewel_buff_flames_magic` | hitlowhealth / player_skills / victim / %5; hitlowhealth / player_skills / user / %5; hitlowhealth / player_skills / location / %5; hitlowhealth / player_skills / location / %5; hitlowhealth / frozenorb / bullet / %5; hitlowhealth / frozenorb / bulletdeath / %5; hitlowhealth / thorntrail / bullet / %5; hitlowhealth / MachineGunTurret / location / %5; hitlowhealth / CombatTurret / location / %5; hitlowhealth / CombatTurret / location / %5; hitlowhealth / CoveringFireTurret / location / %5; hitlowhealth / TeslaTurret / location / %5; hitlowhealth / Grenade / bulletdeath / %5; hitlowhealth / ShrapnelShot / location / %5; hitlowhealth / JackInTheBox / location / %5; hitlowhealth / arcanemanifestation / location / %5; hitlowhealth / explosiveshot / location / %5; hitlowhealth / hornetcloud / user / %5 | Inventoried; live behavior unverified |
| `jewel_buff_flames_rare` | hitlowhealth / player_skills / victim / %5; hitlowhealth / player_skills / user / %5; hitlowhealth / player_skills / location / %5; hitlowhealth / player_skills / location / %5; hitlowhealth / frozenorb / bullet / %5; hitlowhealth / frozenorb / bulletdeath / %5; hitlowhealth / thorntrail / bullet / %5; hitlowhealth / MachineGunTurret / location / %5; hitlowhealth / CombatTurret / location / %5; hitlowhealth / CombatTurret / location / %5; hitlowhealth / CoveringFireTurret / location / %5; hitlowhealth / TeslaTurret / location / %5; hitlowhealth / Grenade / bulletdeath / %5; hitlowhealth / ShrapnelShot / location / %5; hitlowhealth / JackInTheBox / location / %5; hitlowhealth / arcanemanifestation / location / %5; hitlowhealth / explosiveshot / location / %5; hitlowhealth / hornetcloud / user / %5 | Inventoried; live behavior unverified |
| `jewel_buff_halloween_ghostEffect_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_halloween_ghostEffect_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_halloween_ghostEffect_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_halloween_ghostEffect_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_halloween_ghostEffect_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_heal_onHitFrost_common` | hitfrost / wearer takes damage while frozen / user / %100 | Trigger routing corrected; live healing amount and visuals unverified |
| `jewel_buff_heal_onHitFrost_epic` | hitfrost / wearer takes damage while frozen / user / %100 | Trigger routing corrected; live healing amount and visuals unverified |
| `jewel_buff_heal_onHitFrost_magic` | hitfrost / wearer takes damage while frozen / user / %100 | Trigger routing corrected; live healing amount and visuals unverified |
| `jewel_buff_heal_onHitFrost_rare` | hitfrost / wearer takes damage while frozen / user / %100 | Trigger routing corrected; live healing amount and visuals unverified |
| `jewel_buff_heal_onHitFrost_uncommon` | hitfrost / wearer takes damage while frozen / user / %100 | Trigger routing corrected; live healing amount and visuals unverified |
| `jewel_buff_health_reg_common` | kill / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_health_reg_epic` | kill / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_health_reg_magic` | kill / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_health_reg_rare` | kill / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_health_reg_uncommon` | kill / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_lords_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_lords_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_lords_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_pirate_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_pirate_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_pirate_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_pirate_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_pirate_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_revenge_common` | revive120 / global / user / %100; revive120 / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_revenge_epic` | revive120 / global / user / %100; revive120 / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_revenge_magic` | revive120 / global / user / %100; revive120 / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_revenge_rare` | revive120 / global / user / %100; revive120 / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_revenge_uncommon` | revive120 / global / user / %100; revive120 / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_dwarf_common` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_dwarf_epic` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_dwarf_magic` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_dwarf_rare` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_dwarf_uncommon` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_mage_common` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_mage_epic` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_mage_magic` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_mage_rare` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_mage_uncommon` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_ranger_common` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_ranger_epic` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_ranger_magic` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_ranger_rare` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_ranger_uncommon` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_warrior_common` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_warrior_epic` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_warrior_magic` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_warrior_rare` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_skills_movement_warrior_uncommon` | lowhealth / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_snow_epic` | hitcritical / player_skills / user / %3 | Inventoried; live behavior unverified |
| `jewel_buff_snow_magic` | hitcritical / player_skills / user / %1 | Inventoried; live behavior unverified |
| `jewel_buff_snow_rare` | hitcritical / player_skills / user / %2 | Inventoried; live behavior unverified |
| `jewel_buff_stun_immunity_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_stun_immunity_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_stun_immunity_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_stun_immunity_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_stun_immunity_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_buff_stunned_common` | disabled / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_stunned_epic` | disabled / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_stunned_magic` | disabled / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_stunned_rare` | disabled / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_stunned_uncommon` | disabled / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_buff_xmas_event_dwarf` | hit / Flamethrower / location / %5 | Inventoried; live behavior unverified |
| `jewel_buff_xmas_event_mage` | hit / iceball / location / %5 | End-to-end server event verified at 150% Ice on landing; live behavior unverified |
| `jewel_buff_xmas_event_ranger` | hit / multishot / location / %5 | Inventoried; live behavior unverified |
| `jewel_buff_xmas_event_warrior` | hit / mightybash / location / %5 | Inventoried; live behavior unverified |
| `jewel_dragon_03_sp_dwarf` | default / TurretCombat / user / %100; default / TurretTesla / user / %100; default / TurretMachineGun / user / %100; default / TurretCoveringFire / user / %100 | Inventoried; live behavior unverified |
| `jewel_dragon_03_sp_dwarf_01` | default / TurretCombat / user / %100; default / TurretTesla / user / %100; default / TurretMachineGun / user / %100; default / TurretCoveringFire / user / %100 | Inventoried; live behavior unverified |
| `jewel_dragon_03_sp_dwarf_02` | default / PositionBombShot / victim / %100; default / HeavyShot / location / %100; default / HeavyShot / location / %0; immediatemanaged / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_dragon_03_sp_mage` | default / frozenorb / victim / %50 | Inventoried; live behavior unverified |
| `jewel_dragon_03_sp_mage_01` | default / lightningstrike / user / %100; default / lightningstrike / user / %100 | Inventoried; live behavior unverified |
| `jewel_dragon_03_sp_mage_02` | default / frozenorb / user / %100; immediatemanaged / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_dragon_03_sp_ranger` | default / stab / victim / %100; default / mightystab / user / %100 | Inventoried; live behavior unverified |
| `jewel_dragon_03_sp_ranger_01` | default / stab / victim / %100; default / markshot / victim / %100; default / whirlwind / victim / %100; default / trap / victim / %100; default / mightyshot / victim / %100; default / explosiveshot / victim / %100; default / stunshot / victim / %100; default / birdofprey / victim / %100; default / jump / victim / %100; default / adrenalin / victim / %100; default / multishot / victim / %100; default / packofwolves / victim / %100; default / mightystab / victim / %100; default / thorntrail / victim / %100; default / hornetcloud / victim / %100; default / summontree / victim / %100 | Inventoried; live behavior unverified |
| `jewel_dragon_03_sp_ranger_02` | default / mightystab / victim / %40; default / mightystab / user / %100 | Inventoried; live behavior unverified |
| `jewel_dragon_03_sp_warrior` | default / seismicslam / user / %100; default / seismicslam / victim / %100 | Inventoried; live behavior unverified |
| `jewel_dragon_03_sp_warrior_01` | default / defiance / user / %100 | Inventoried; live behavior unverified |
| `jewel_dragon_03_sp_warrior_02` | default / seismicslam / user / %100; default / mightybash / victim / %100 | Inventoried; live behavior unverified |
| `jewel_easter_event_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_easter_event_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_easter_event_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_easter_event_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_easter_event_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_hitpoints_damage_critRating_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_hitpoints_damage_critRating_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_hitpoints_damage_critRating_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_hitpoints_damage_critRating_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_hitpoints_damage_critRating_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_ingredient_find_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_ingredient_find_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_ingredient_find_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_ingredient_find_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_ingredient_find_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_pve_event_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_pve_event_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_pve_event_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_pve_event_new_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_pve_event_new_unique` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_pve_event_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_pve_event_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resistAll_damage_critRating_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resistAll_damage_critRating_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resistAll_damage_critRating_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resistAll_damage_critRating_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resistAll_damage_critRating_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resistAll_hitpoints_critRating_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resistAll_hitpoints_critRating_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resistAll_hitpoints_critRating_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resistAll_hitpoints_critRating_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resistAll_hitpoints_critRating_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resistAll_hitpoints_damage_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resistAll_hitpoints_damage_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resistAll_hitpoints_damage_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resistAll_hitpoints_damage_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resistAll_hitpoints_damage_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resource_cost_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resource_cost_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resource_cost_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resource_cost_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_resource_cost_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_skill_cooldown_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_skill_cooldown_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_skill_cooldown_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_skill_cooldown_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_skill_cooldown_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_1h_damage_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_1h_damage_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_1h_damage_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_1h_damage_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_1h_damage_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_2h_damage_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_2h_damage_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_2h_damage_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_2h_damage_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_2h_damage_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_armor_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_armor_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_armor_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_armor_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_armor_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_damage_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_damage_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_damage_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_damage_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_damage_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_health_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_health_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_health_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_health_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_stat_health_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_summer_event_epic` | hitblocked / global / user / %10 | Inventoried; live behavior unverified |
| `jewel_summer_event_magic` | hitblocked / global / user / %10 | Inventoried; live behavior unverified |
| `jewel_summer_event_rare` | hitblocked / global / user / %10 | Inventoried; live behavior unverified |
| `jewel_unique_critRating_BuffCritDmgAttackSpeed_common` | hitcritical / player_skills / user / %20 | Inventoried; live behavior unverified |
| `jewel_unique_critRating_BuffCritDmgAttackSpeed_epic` | hitcritical / player_skills / user / %30 | Inventoried; live behavior unverified |
| `jewel_unique_critRating_BuffCritDmgAttackSpeed_magic` | hitcritical / player_skills / user / %30 | Inventoried; live behavior unverified |
| `jewel_unique_critRating_BuffCritDmgAttackSpeed_rare` | hitcritical / player_skills / user / %30 | Inventoried; live behavior unverified |
| `jewel_unique_critRating_BuffCritDmgAttackSpeed_uncommon` | hitcritical / player_skills / user / %25 | Inventoried; live behavior unverified |
| `jewel_unique_critRating_BuffGhostEffect_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_unique_critRating_BuffGhostEffect_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_unique_critRating_BuffGhostEffect_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_unique_critRating_BuffGhostEffect_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_unique_critRating_BuffGhostEffect_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageFireOnHit_common_dwarf` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageFireOnHit_common_mage` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageFireOnHit_epic_dwarf` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageFireOnHit_epic_mage` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageFireOnHit_magic_dwarf` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageFireOnHit_magic_mage` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageFireOnHit_rare_dwarf` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageFireOnHit_rare_mage` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageFireOnHit_uncommon_dwarf` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageFireOnHit_uncommon_mage` | default / JewelAreaDamageFire / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageLightningOnHit_common_ranger` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageLightningOnHit_common_warrior` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageLightningOnHit_epic_ranger` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageLightningOnHit_epic_warrior` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageLightningOnHit_magic_ranger` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageLightningOnHit_magic_warrior` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageLightningOnHit_rare_ranger` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageLightningOnHit_rare_warrior` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageLightningOnHit_uncommon_ranger` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_AreaDamageLightningOnHit_uncommon_warrior` | default / JewelAreaDamageLightning / victim / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_GroupHpReg_IncreasedResource_common` | hit / player_skills / user / %10 | Inventoried; live behavior unverified |
| `jewel_unique_damage_GroupHpReg_IncreasedResource_epic` | hit / player_skills / user / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_GroupHpReg_IncreasedResource_magic` | hit / player_skills / user / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_GroupHpReg_IncreasedResource_rare` | hit / player_skills / user / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_GroupHpReg_IncreasedResource_uncommon` | hit / player_skills / user / %12.5 | Inventoried; live behavior unverified |
| `jewel_unique_damage_GroupResourceReg_IncreasedHp_common` | hit / player_skills / user / %10 | Inventoried; live behavior unverified |
| `jewel_unique_damage_GroupResourceReg_IncreasedHp_epic` | hit / player_skills / user / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_GroupResourceReg_IncreasedHp_magic` | hit / player_skills / user / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_GroupResourceReg_IncreasedHp_rare` | hit / player_skills / user / %15 | Inventoried; live behavior unverified |
| `jewel_unique_damage_GroupResourceReg_IncreasedHp_uncommon` | hit / player_skills / user / %12.5 | Inventoried; live behavior unverified |
| `jewel_unique_damage_HealthGlobeDamageBuff_common` | pickuphealthglobe / global / user / %100; pickuphealthglobe / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_unique_damage_HealthGlobeDamageBuff_epic` | pickuphealthglobe / global / user / %100; pickuphealthglobe / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_unique_damage_HealthGlobeDamageBuff_magic` | pickuphealthglobe / global / user / %100; pickuphealthglobe / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_unique_damage_HealthGlobeDamageBuff_rare` | pickuphealthglobe / global / user / %100; pickuphealthglobe / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_unique_damage_HealthGlobeDamageBuff_uncommon` | pickuphealthglobe / global / user / %100; pickuphealthglobe / global / user / %100 | Inventoried; live behavior unverified |
| `jewel_unique_hitpoints_HpReg_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_unique_hitpoints_HpReg_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_unique_hitpoints_HpReg_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_unique_hitpoints_HpReg_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_unique_hitpoints_HpReg_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_unique_hitpoints_ResourceAbsolute_common` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_unique_hitpoints_ResourceAbsolute_epic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_unique_hitpoints_ResourceAbsolute_magic` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_unique_hitpoints_ResourceAbsolute_rare` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_unique_hitpoints_ResourceAbsolute_uncommon` | Static stat / conditional modifier | Inventoried; live behavior unverified |
| `jewel_unique_resistAll_HealOnFrozenHit_common` | hitfrost / wearer takes damage while frozen / user / %100 | Trigger routing corrected; live healing amount and visuals unverified |
| `jewel_unique_resistAll_HealOnFrozenHit_epic` | hitfrost / wearer takes damage while frozen / user / %100 | Trigger routing corrected; live healing amount and visuals unverified |
| `jewel_unique_resistAll_HealOnFrozenHit_magic` | hitfrost / wearer takes damage while frozen / user / %100 | Trigger routing corrected; live healing amount and visuals unverified |
| `jewel_unique_resistAll_HealOnFrozenHit_rare` | hitfrost / wearer takes damage while frozen / user / %100 | Trigger routing corrected; live healing amount and visuals unverified |
| `jewel_unique_resistAll_HealOnFrozenHit_uncommon` | hitfrost / wearer takes damage while frozen / user / %100 | Trigger routing corrected; live healing amount and visuals unverified |
