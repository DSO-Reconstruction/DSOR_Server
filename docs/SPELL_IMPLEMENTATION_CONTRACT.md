# Spell implementation contract

This is the definition of done for every player spell. A spell is not complete merely because it changes health on the local client.

## Required checks

1. **Authoritative database shape**
   - Resolve the live `_Template_Skill` row and its `SkillBulletId`, targeting type, attack/hit range, angle or cone widths, cast/loop/hit/unblock frames, projectile velocity/lifetime/count/impact count, location/death effects, summon/trap data, and resource costs.
   - Do not replace an authored radius, timing, resource id, or packet type with a per-skill guess.

2. **Networking and remote playback**
   - Decode the client request into its exact generated command type and publish the matching server command to every current viewer.
   - Include the caster actor context, viewer-relative target ids, current movement frame when required, talent-aware visual modifier effects, and every generated additional-server-data field.
   - Verify late-entry state for persistent actors/effects and explicit spawn, impact, expiry, despawn, and discard lifecycle. Local-only VFX is a failed implementation.

3. **Talents**
   - Apply the active preset and rank stacks before resolving geometry, timing, damage, resource/cooldown changes, status substitutions, summons/traps, or VFX variants.
   - Confirm both mechanical and visual branches. A talent-specific effect must be visible to remote players as well as authoritative on the server.

4. **Items, item sets, pets, and active status effects**
   - Feed equipped item modifiers, complete set bonuses, pet effects, essences, and active status programs through the same modifier pipeline as talents.
   - Check skill-level additions, `SkillDamage`, `SkillStatusEffect`, `SkillSequence`, cooldown/resource modifiers, conditional target effects, and support effects.

5. **Combat semantics**
   - Respect PvE/PvP hostility, groups, ammunition vulnerabilities, armor/resistance channels, critical/direct versus periodic combat text, marks and other prerequisites, collision/range, projectile travel, repeated impacts, explosions, and ground auras.
   - Spawned entities use their authored targeting, taunt/support behavior, stationary/moving state, owner leash, lifetime, and animation lifecycle.

6. **Persistence and cleanup**
   - Temporary casts must not persist through map changes or after their authored lifetime. Persistent character statuses must survive only where their database/persistence rules permit.
   - Cancel timers and remove shared-map state on disconnect, map transfer, owner-range expiry, death, and replacement by a category-limited summon.

7. **Regression evidence**
   - Add a protocol round-trip or byte-exact capture test for a new packet layout.
   - Add combat-state tests for geometry, timing, targets, talent/item/status modifications, and lifecycle.
   - Include at least one remote-viewer assertion for a new command family and a late-entry/cleanup assertion for a persistent world object.

## Review rule

When any one of networking, talents, item/set/status interactions, authoritative mechanics, or lifecycle is unverified, document the gap explicitly and do not mark the spell as fully implemented in `CLASS_SKILL_IMPLEMENTATION_AUDIT.md`.
