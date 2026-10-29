# Ranger Skill Audit

**Date:** 2026-10-29  
**Scope:** All 18 live Ranger skill entries in the server's static skill catalogue, including alternate variants.

## Findings and fixes

### Deadly Blow reach

`mightystab` and `mightystab_light` were authored with 1.75 m attack and hit ranges. That left the actor-targeted attacks with an unusually small effective reach, so the server now resolves both variants at 5 m. Deadly Blow measures horizontal contact using the same 10 m vertical tolerance as the existing area-skill query. The earlier 1.5 m cutoff was too restrictive for terrain offsets. Tests cover a 6 m horizontal hit with a 4 m height offset and rejection beyond the shared 10 m vertical tolerance.

### Marked-hit talent trigger

The Ranger Concentration effect uses the `HitMarked` trigger, which the shared hit-effect filter did not accept. The runtime now checks whether the monster was marked before the current hit and evaluates that trigger for both PvE and PvP hit paths. A hit that applies Mark does not incorrectly count as hitting an already-marked target.

### Hornet Swarm damage area

Hornet Swarm's owner status created a client-visible aura, but its direct owner-attached aura was not expanded into a server damage area. The server now applies its authored receiver effect around the Ranger, follows the Ranger as they move, avoids duplicate damage on aura refresh, and stops applying the effect after targets leave or the aura expires.

### Adrenaline attack-speed child

Adrenaline's movement-speed status schedules an attack-speed child through `TickModifiers`, but the player status ticker did not send actor-status-effect children. The ticker now delivers this authored child effect. A service-level test exercises the scheduled tick.

## Skill-by-skill coverage

The audit enumerated all live Ranger entries and checked their authored identity, geometry, damage or utility role, and relevant runtime effects. Damaging entries were cast through the server hit resolver and checked for health loss. Utility entries were checked for their intended non-damaging cast behavior, status effects, traps, summons, or owner buffs.

| Skill entry | Audit focus |
|---|---|
| `stab` | Melee angle damage and reach |
| `stab_SetLight2026_trig` | Hidden low-health variant identification; intentionally zero direct damage |
| `markshot` | Ranged damage, Mark application, and marked-hit talent trigger |
| `whirlwind` | Area damage, corrected 2.5m spin radius (overriding DB 10m dash distance) |
| `trap` | Trap placement and detonation damage/effects |
| `mightyshot` | Ranged damage |
| `explosiveshot` | Projectile explosion damage and corrected 3.5m radius (overriding DB 25m arrow flight distance) |
| `stunshot` | Ground radius, stun and Mark; no direct damage is authored |
| `jump` | Movement skill; no direct damage is authored |
| `adrenalin` | Owner buffs and delayed attack-speed child |
| `birdofprey` | Ranged-target damage |
| `multishot` | Cone damage |
| `multishot_Chimera` | Alternate cone damage variant |
| `packofwolves` | Summon creation and wolf movement/attacks |
| `mightystab` | Deadly Blow damage, actor targeting, cooldown reset on kill, and corrected reach |
| `mightystab_light` | Alternate Deadly Blow damage and corrected reach |
| `thorntrail` | Damage, persistent poison/slow/armor effects, and area behavior |
| `hornetcloud` | Owner-following area damage and effect lifetime |
| `summontree` | Tree summon, owner healing, and monster taunt |

## Blade Dance and Explosive Arrow Radius Fixes

1. **Blade Dance (`whirlwind`) Hit Radius**:
   - In `_Template_Skill`, `whirlwind` had `AttackRange: 10.0` (charge/dash distance) and `HitRange: 10.0`.
   - Because `TargetingType` is `Radius`, the server previously used `radius = 10.0m`, damaging all monsters in a 10m (11.5m with collision) 360-degree circle around the player.
   - Set `PLAYER_SKILL_HIT_RANGE_OVERRIDES["whirlwind"] = 2.5` (matching authored `Angle: 2.0` and aura 2.0m). `attack_range` remains 10.0m for dash allowance, but damage is constrained to 2.5m close melee spin.

2. **Explosive Arrow (`explosiveshot`) Explosion Radius**:
   - In `_Template_Skill`, `explosiveshot` had `AttackRange: 25.0` (flight distance) and `HitRange: 25.0`.
   - In `combat_entities.py`, point bullet impacts previously evaluated `max(radius, splash_radius)`, causing ground-targeted explosive shots to explode in a 25.0m map-wide radius.
   - Set `PLAYER_SKILL_HIT_RANGE_OVERRIDES["explosiveshot"] = 3.5` (matching `skill_explosiveshot_explosion_aura` radius of 3.5m) and updated point bullet impact evaluation in `combat_entities.py`. Arrow flight distance remains up to full range, while explosion damage is constrained to 3.5m around impact.

## Deadly Blow "Patlayıcı Vuruş" (Exploding Hit) Talent Fix

1. **Root Cause**:
   - Talent `ranger_talent_mightystab_explosion` modifies `mightystab`'s `LocationStatusEffects` (`modLE`), activating `ranger_talent_mightystab_explosion_aura` with chance 1.0, 3.5m radius, and receiver effect `ranger_talent_mightystab_explosion_effect` (180.0% physical damage).
   - In `_Template_StatusEffect`, `ranger_talent_mightystab_explosion_aura` has `StatusEffectDuration: 0.0` because an explosion is instantaneous. `LocalCombatState.start_player_location_visuals` previously dropped any location visual with `duration_ticks <= 0`, preventing instantaneous auras from ever being created.
   - Additionally, hit-triggered location auras were never scheduled when impacts resolved in `PlayerProjectileService._resolve_runtime_player_hit_batch`.
   - When cast-time location visuals were dispatched in `player_combat.py`, `receiver_status_effects` and `player_damage_range` were not passed, and on-hit auras were incorrectly attempted at cast time instead of impact time.

2. **Fixes Applied**:
   - In `gameplay/local_combat_state.py`:
     - `start_player_location_visuals` now accepts instantaneous location auras with `aura_radius > 0.0` and `duration_ticks <= 0`, assigning them a clean 10-tick (0.4s) lifecycle to play their VFX and apply aura damage.
     - Automatically resolves `receiver_status_effects` via `_load_location_aura_effects` if omitted by a caller.
     - Added `player_skill_location_receiver_effects` helper.
   - In `services/player_combat.py`:
     - Cast-time location visuals filter out hit-triggered auras (`normalize_socket_trigger(v.trigger) not in ('hit', 'kill', 'hitcritical')`).
     - Passes `receiver_status_effects` and `player_damage_range`.
   - In `services/player_projectiles.py`:
     - When a hit resolves on an enemy, checks for talent-unlocked hit-triggered location auras (`normalize_socket_trigger(v.trigger) in ('', 'hit')`).
     - Schedules the explosion visual aura at `resolution.position` with its receiver effects (`ranger_talent_mightystab_explosion_effect`) and wearer damage range.
     - Immediately applies the location aura and dispatches autonomous player-owned damage resolutions to all enemies within 3.5 meters.

## Validation

The focused Ranger and supporting combat regression set passed:

```text
208 passed, 17 subtests passed
```

This includes Ranger audit, projectile contact, skill timing, client skill reach, PvP player effects, local combat state, summon/area behavior, status modifiers, and cast-input coverage. A full `TargetSkill` packet for Deadly Blow also resolves and deals damage at 6 m horizontal distance with 4 m of vertical separation in the service-level test.

The repository's full legacy suite was not used as a clean bill of health: an earlier broad legacy selection includes stale skill-ID and summon-fixture expectations that fail independently of these Ranger changes. The focused suite verifies the server-side behaviors listed above. A live-client session is still needed to confirm the final in-game feel of the increased Deadly Blow reach and visual timing.
