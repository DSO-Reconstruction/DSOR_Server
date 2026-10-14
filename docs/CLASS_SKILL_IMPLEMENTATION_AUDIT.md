# Player class skill implementation audit

Audit date: 2026-09-09

## Scope and method

This audit covers every live player skill in the synchronized `static.db4` game database: 71 skills in total (20 dwarf, 19 mage, 16 ranger, and 16 warrior/dragonknight skills). For every row, the server now verifies that it can materialize runtime geometry. Every skill whose database row declares `Summons=1` must also materialize a summon definition. This is enforced by `LocalCombatStateTests.test_every_live_player_skill_has_runtime_geometry_and_summons` so a future database or parser change cannot silently drop a class skill.

The audit checked the complete database-driven effect chain, not only the primary damage number:

- targeting shape, range, cast/impact frames, bullet velocity/lifetime, projectile count, impact count, and explosion/location effects;
- user, victim, bullet-location, bullet-death, and location status effects, including effects introduced or changed by active talent and item/set modifiers;
- direct damage, critical damage, damage-over-time, healing-over-time, taunt, mind control, armor/resistance modifiers, immobilization, resource/cooldown, skill timing, punch-through, and summoned actors;
- player damage layers from equipment, enchantments, sockets, item sets, talents, pets, and active status effects;
- incoming player mitigation from physical armor and the Fire, Ice, Lightning, Poison, and Dark Magic resistance channels.

Client-owned visual/animation/resource/cooldown modifiers remain driven by the native status-effect engine after the server sends the authored status effect. Health changes, damage, life leech, reflection, summons, taunts, control state, and timed damage/healing are authoritative on the server.

Every future spell change must satisfy the separate [spell implementation contract](SPELL_IMPLEMENTATION_CONTRACT.md). In particular, a locally correct damage result is not considered a complete implementation unless remote playback, talent substitutions, equipped item/set modifiers, active status effects, spawned-world lifecycle, and PvP/PvE targeting are checked together.

## Changes made during this audit

- Ranger `thorntrail` (Thicket of Thorns) now materializes its database-authored bullet trail at the 0.267-second cadence, projects every segment to navigable terrain, publishes the persistent location effects to current and late-joining peers, and applies the aura's poison DoT, movement slow, and armor reduction when monsters enter a segment. Talent ranks modify those same parsed effects before creation.
- Ranger `explosiveshot` now uses the resolved death/location aura radius for its same-impact splash instead of treating the projectile as a single-target hit. Marked-target and talent/item/set substitutions remain in the common modifier path.
- Ranger `packofwolves` now creates three separate, terrain-projected wolf actors. All three use the shared autonomous summon combat and visibility lifecycle.
- Ranger `whirlwind` (Blade Dance) now co-frames the caster's Move command with remote skill playback; charge skills that teleport the caster still suppress the stale Move frame.
- Dwarf `Mine` now uses the native `NewTrap`, `TrapTripped`, and `DiscardTrap` lifecycle recovered from the client serializer. Talent ranks select the ranked trap resource (for example `dwarf_mine10`) and the trigger resolves through the ordinary damage/status/talent/item-set pipeline.
- Dwarf target-point projectiles (`PositionBombShot`/C14 Micro-Rocket and the matching area casts) now include the generated command's trailing server hit-range field. Omitting it made remote clients reject command 73 and rendered the projectile invisible.
- Projectile explosions now also resolve area radii hosted on database-authored death/location auras. This covers Ranger Explosive Arrow and Dwarf Grenade/C14 without per-skill hard-coded radii.
- Machine Gun Turret emits its sustained forward firing playback on schedule even when no target is available; damage remains conditional on valid targets in front of the stationary turret.
- Construction world effects and actors are bound to their originating cast. Owner-range expiry removes only that turret's effect, runs the authored despawn duration/animation, and then unregisters the actor; another turret using a related effect is not removed.
- Mage `iceball` (Ice Missile) carries a 0.15-unit collider bonus on top of its authored 0.25 bullet radius (`DSO_SKILL_PROJECTILE_RADIUS_BONUS`); live play showed the server missing targets the client visibly clipped. Other projectiles keep their authored radius.
- Essences are consumed: each accepted cast spends the skill's `AmmoCost` (1 for Magic Missile, 2 for Ice Missile, 10 for the mage manifestations) from the active slot's stack, publishes the reduced stack, and drops the item and its damage factor when the stack runs out. Zero-cost skills such as teleport spend nothing; battlegrounds spend nothing.
- Damage essences in the primary essence slot now participate in the full stat calculation. The current database tiers resolve to 1.25x, 1.5x, 2x, 3x, 4x, and 7x PvE damage. Event ammunition types are also matched against monster `AmmoVulnerability` effects so special essences can damage otherwise untargetable encounters.
- TargetPoint server playback, native traps, trail auras, explosion splash, multi-wolf summons, idle Machine Gun playback, owner-range teardown, essence tiers, and ammunition vulnerability all have focused protocol/combat regressions.

- Dragonknight `frenzyshout` (Dragon Hide): implemented the authored `skill_frenzyshout_buff_lifeleech` program. While active, 20% of damage caused by the listed Dragonknight damage skills is restored as health. Healing is capped at maximum health and is synchronized through the normal health transition path.
- Dragonknight `spikedShield`: implemented the authored `DamageReflection` modifier (80% with the current database value) for monster hits. Reflected damage uses the ordinary combat-resolution path, including death/loot handling.
- Monster-to-player damage now resolves the monster skill's damage-type weights against the target character's complete physical armor and per-element resistance values. Equipment, gems, enchantments, item sets, talents, pets, and active status effects are included.
- Base regeneration remains authoritative and sends a live health transition with an empty combat-text id, so the HUD stays synchronized without displaying passive-heal numbers. Spell, item, pet, and item-set healing still sends the appropriate healing indication.
- The native player-template base regeneration is 1% of maximum health per second. This avoids the character sheet rounding the previous 0.83% value to `0`; the synchronized client and server database copies carry the same value.
- Dwarf turrets are explicitly stationary even though their monster templates contain a non-zero `RunSpeed`; they are excluded from summon navigation.
- Native world effects now use `NewLocationEffect` and `DiscardLocationEffect`, including database-authored radius/height and viewer-relative ownership. This restores the retail teardown trigger for turret and persistent ground-effect visuals instead of leaving them as metadata-only `LocationEffectInfo` snapshots.

## Dragonknight / warrior (16 of 16)

| Skill | Audit result |
|---|---|
| `angrystrike` | Geometry, direct damage, talent/item modifiers, and Dragon Hide life-leech scope verified. |
| `mightyswing` | Melee area geometry, damage, and attached effects verified. |
| `bloody360` | Radial damage and attached victim/user effects verified. |
| `mighty360` | Radial damage and attached effects verified. |
| `stuncharge` | Movement/impact timing, damage, and control effects verified. |
| `warshout` | User buff/resource/cooldown status chain verified. |
| `battlecry` | User buff/resource/cooldown status chain verified. |
| `frenzyshout` (Dragon Hide) | Resistance buffs verified; 20% skill-scoped life leech newly made server-authoritative. |
| `seismicslam` | Directional/ranged impact geometry and victim effects verified. |
| `laceratingstrike` | Direct damage and victim effects verified. |
| `enragingleap` | Movement/area impact and attached effects verified. |
| `defiance` | User defensive/resource effects verified. |
| `mightybash` | Melee damage and attached effects verified. |
| `earthquake` | Location area, repeated effects, and Dragon Hide life-leech scope verified. |
| `true_earthquake` | Location area and repeated effect chain verified. |
| `spikedShield` | Damage and defensive effects verified; 80% reflected damage newly made server-authoritative. |

## Spellweaver / mage (19 of 19)

| Skill | Audit result |
|---|---|
| `magicmissile` | Projectile range/travel, damage, and talent effects verified. |
| `fireball` | Projectile impact/explosion and victim effects verified. |
| `iceball` | Projectile impact, cold/control effects, and modifiers verified. |
| `frozenorb` | Multi-impact projectile/location effects verified. |
| `frostnova` | Player-centred area and control effects verified. |
| `teleport` | Movement and user-effect chain verified. |
| `lightningstrike` | Ground-target impact timing, area damage, and effects verified. |
| `frostwind` | Cone/area control and its support modifiers for other ice skills verified. |
| `chainlightning` | Chained/multi-target projectile behavior and effects verified. |
| `arcanemanifestation` | Summon definition, taunt/hostile interaction, and lifecycle verified. |
| `firemanifestation` | Summon definition, damage/effect path, and lifecycle verified. |
| `arcanetorrent` | Projectile timing, damage, and effects verified. |
| `arcanevortex` (Singularity) | Location area, timed armor/resistance debuff, and effect expiry verified. |
| `meteor` | Ground-target cast/impact delay, area damage, and explosion effects verified. |
| `mindcontrol` | Mind-control state, target allegiance, and damage modifier path verified. |
| `arcanecharge` | Movement/impact geometry, damage, and attached status effects verified. |
| `icecharge` | Movement/impact geometry and cold effects verified. |
| `firetorrent` | Sustained/cone geometry, timing, and effects verified. |
| `balllightning` | Projectile travel/multi-impact behavior and effects verified. |

## Ranger / steam mechanic ranger (16 of 16)

| Skill | Audit result |
|---|---|
| `stab` | Melee geometry, damage, mark interaction, and effects verified. |
| `markshot` | Projectile/mark application and modifiers verified. |
| `whirlwind` | Movement/area damage and effects verified. |
| `trap` | Location trigger/control/damage effects verified. |
| `mightyshot` | Projectile travel, marked-target bonus, and effects verified. |
| `explosiveshot` | Projectile impact/explosion, marked-target bonus, and effects verified. |
| `stunshot` | Projectile timing and control effects verified. |
| `jump` | Movement/landing area and effects verified. |
| `adrenalin` | User resource/speed/cooldown effects verified. |
| `birdofprey` | Projectile/summoned visual path, marked-target bonus, and effects verified. |
| `multishot` (Scatter Shot) | Front cone, hit-frame delayed multi-target damage, marked-target bonus, and effects verified. |
| `packofwolves` (Wild Pack) | Three-wolf summon definition, nearby autonomous attacks, and lifecycle verified. |
| `mightystab` | Melee damage, marked-target bonus, and effects verified. |
| `thorntrail` | Location trail, repeated damage/control, and expiry verified. |
| `hornetcloud` | Location/damage-over-time effect chain and expiry verified. |
| `summontree` (Tree of Life) | Summon definition, local taunt, periodic player healing, and lifecycle verified. |

### Ranger arrow release timing

`markshot` (Hunting Arrow, the level-1 arrow) has `BulletEmitDelay=0`,
`HitFrame=0`, and `LoopStartFrame=5`. Its arrow is released after the bow draw,
0.20 seconds at normal attack speed. `mightyshot` (Precision Shot) releases
at frame 9, or 0.36 seconds. Both arrows travel at the authored 21 units/s.

Player projectile scheduling previously counted only `BulletEmitDelay`, so
these zero-delay rows applied damage and death before the client arrow
arrived. The shared projectile planner and legacy cast path now use
`max(BulletEmitDelay, LoopStartFrame / 25)` before adding collision-distance
travel time, matching the existing monster release rule. Attack speed scales
the draw phase; it does not accelerate the projectile. `SkillTiming`
modifiers retain their existing scaling. Remote visual deadlines for misses
include the same release phase.

Regressions exercise lethal Hunting Arrow and Precision Shot hits at close
and longer range, point-blank shots, normal/doubled attack speed, explicit
emission delays, timing modifiers, and missed-arrow visual lifetimes.

### Remaining ranger timing audit

The follow-up code/data audit covers all 16 ranger skills. Confirmed timing
mismatches are covered by `tests/test_ranger_skill_timing.py`:

- **Scatter Shot (`multishot`)** had a hard-coded zero hit delay despite
  `HitFrame=9`. The complete cone now resolves after 0.36 seconds at normal
  speed; attack speed scales that cast phase.
- **Net (`stunshot`)** uses a shifted cast with a 10-frame launch phase and
  a following 15-frame impact phase. Damage/control waited only 15 frames,
  0.40 seconds before the outgoing animation's deadline. The damage and
  visual paths now share both phases: 1.00 second total. The shared fix also
  aligns other shifted skills; long falling phases retain their authored
  duration while the preceding cast phase can scale.
- **Thicket of Thorns (`thorntrail`)** scheduled its first segment 0.24 seconds
  from cast start, before the 0.60-second release phase. Trail samples now
  start after release, follow the projectile's position at their authored
  cadence, and stop within its range. Attack speed and `SkillTiming` modifiers
  are applied to the appropriate phases. Timers retain the cast's original
  clock anchor so preparation work does not add a second delay.
- **Hunting Trap (`trap`)** was created and armed at cast start. Creation now
  waits for frame 4 (0.16 seconds), then begins the native trap lifecycle.
  Leaving the map or changing PvP rounds invalidates a pending placement.
- **Modified arrow visuals** used the original lifetime on a miss even when
  `SkillTiming` changed projectile velocity. Missed-arrow visual deadlines
  now scale the flight phase consistently with actual impacts.

| Skill resource | Checked timing at normal speed |
|---|---|
| `stab` | 5-frame contact; scales with attack speed |
| `markshot` | 5-frame release plus flight; earlier arrow fix retained |
| `whirlwind` | 3-frame contact; ignores attack-speed scaling |
| `trap` | 4-frame placement, followed by proximity triggering |
| `mightyshot` | 9-frame release plus flight |
| `explosiveshot` | 9-frame release plus flight; splash shares arrival |
| `stunshot` | 10-frame cast plus 15-frame impact; unscaled |
| `jump` | 12-frame contact; unscaled |
| `adrenalin` | 6-frame contact metadata; authored user effects use their own triggers |
| `birdofprey` | 15-frame release plus homing travel; unscaled |
| `multishot` | 9-frame cone contact; scales with attack speed |
| `packofwolves` | Authored zero spawn delay, then the existing 0.10-second deployment gate |
| `mightystab` | 5-frame targeted contact; scales with attack speed |
| `thorntrail` | 15-frame release, then time/position matched trail samples |
| `hornetcloud` | 8-frame contact metadata; aura triggers retain their authored lifecycle |
| `summontree` | Authored zero spawn delay, then the existing 1.00-second deployment gate before support |

Validation uses authored client data, the outgoing playback fields, and
server regression tests. It does not replace an in-game visual check of
every skill and talent combination.

## Dwarf / steam mechanic (20 of 20)

| Skill | Audit result |
|---|---|
| `IronDwarfEnd` | Iron Dwarf state removal and status transition verified. |
| `IronDwarf` | Transformation/user status, animation set, stats, and hidden-skill unlock path verified. |
| `SimpleShot` | Projectile range/travel, damage, and modifiers verified. |
| `HeavyShot` | Projectile timing, impact/explosion, and effects verified. |
| `PositionBombShot` | Ground-target projectile/area impact and effects verified. |
| `Mine` | Location trigger, explosion, and effects verified. |
| `Grenade` | Projectile travel, explosion, and effects verified. |
| `Trail` | Location trail, repeated effects, and expiry verified. |
| `TurretCombat` | Stationary summon, authored delay/lifetime, targeting, attacks, aura, and range despawn verified. |
| `TurretTesla` | Stationary summon, authored delay/lifetime, area attacks/effects, aura, and range despawn verified. |
| `TurretMachineGun` | Stationary forward-facing summon, sustained attacks, aura, and range despawn verified. |
| `SteamConductor` | Timed 2% healing ticks, support effects, and combat text path verified. |
| `JackInTheBox` | Summon definition, taunt/confusion behavior, and lifecycle verified. |
| `TurretCoveringFire` | Stationary summon, support/resistance effects, attacks, and lifecycle verified. |
| `HoverJump` | Movement/landing behavior and user effects verified. |
| `ShrapnelShot` | Directional area/projectile impacts and effects verified. |
| `Flamethrower` | Forward cone, sustained/instant impact timing, and effects verified. |
| `IronFlamethrower` | Iron Dwarf cone/timing and effects verified. |
| `IronDwarfPrimarySkill` | Transformed primary projectile/damage and effects verified. |
| `IronDwarfMovementSkill` | Transformed movement/impact and effects verified. |

## Regression coverage

The focused combat suite covers life leech, reflection, armor/elemental mitigation, healing spheres, base regeneration, waypoint exclusion, return-home grace, stationary turrets, summon support, spell geometry, status effects, projectile timing, and safe-login relocation. The database-wide skill test additionally fails if any live class skill loses geometry or any declared summon loses its runtime definition.
