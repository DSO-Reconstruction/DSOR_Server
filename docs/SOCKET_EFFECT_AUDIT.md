# Jewel and rune socket effect coverage

This document describes the production socket pipeline backed by the bundled
`data/db/static.db4`. It covers both `EquipmentJewel` and `EquipmentRune`
items, including recursively attached status-effect programs.

## Catalogue contract

The current database contains 573 socket item programs, 1,725 root modifier
instructions, 269 status rules, 102 nested user effects, and 20 group aura
programs. `SocketEffectDatabase.unsupported_catalogue_entries()` walks both
the item program and every recursively referenced `ActorStatusEffect`. The
test suite requires both unsupported and unresolved result sets to be empty.
This makes a future database update fail visibly when it introduces a new
modifier, operation, trigger, or unresolved status resource.

The recursively referenced runtime graph currently uses these server-owned
behaviors: actor feature locks; nested statuses; cooldown and resource-cost
scalars; critical, damage, health, resource, resistance, regeneration, and
speed modifiers; direct and periodic health changes; minions; skill damage;
status-group immunity; and explicit status removal.

## Inventory and equipment

- Jewel trinkets accept jewel items; rune trinkets accept rune items.
- Insertion and removal preserve sparse socket positions and stack identity.
- The client receives an immediate item refresh after every mutation.
- Reconciliation adds/removes permanent socket statuses and refreshes player
  maxima, regeneration, damage, defenses, and skill modifier sources.
- Socket stats participate in the same cached equipment/talent/item-set
  calculation used for authoritative PvE and PvP damage.

## Runtime events

| Database trigger | Authoritative source |
| --- | --- |
| `Hit` | resolved PvE/PvP impact |
| `HitCritical` | deterministic authoritative critical result |
| `HitFrost` | resolved frost/freeze status groups |
| `HitLowHealth` | successful hit while the source is below one-third health |
| `HitBlocked` | monster or PvP block roll on the defending character |
| `Kill` | lethal player-owned PvE impact |
| `LowHealth` | transition below one-third health |
| `OFF:NotLowHealth` | removal transition after any recovery above one-third |
| `PickUpHealthGlobe` | successful health-sphere pickup |
| `MaxStackSize...` | chained when the authored effect reaches its stack cap |
| `ImmediateManaged` | equipment reconciliation/periodic aura manager |
| `Revive120` | registered in the shared dispatcher for the player revive lifecycle |
| `Disabled` | deliberately inactive content, as authored in the database |

Event chances use deterministic per-impact rolls. Per-rule cooldown deadlines
are bounded session state. Ghost Smoke uses the localized critical-hit contract
instead of the erroneous `Hit` database tag, and Blessing of the Stars uses
its documented 60-second cooldown.

## Special effects

- Jewel of Scorching Ray heals the blocking character by its authored maximum
  health fraction in both PvE and PvP.
- Frost-hit heals, kill regeneration, health-globe buffs, low-health skill
  modifiers, blocking stack explosions, and revive buffs use the same typed
  runtime status path.
- Ghost Smoke disables incoming damage and ordinary monster aggro while its
  status is active. Actor-feature movement and regular-skill locks are also
  enforced at the server request boundary.
- Status-effect immunity groups and stun-immunity duration extensions are
  applied before PvE/PvP debuffs are accepted.
- Poison auras tick through authoritative combat, while snow wolves and dragon
  minions use the ordinary summon lifecycle, navigation, targeting, damage,
  vicinity synchronization, and despawn logic.
- Group proc auras are delivered only to nearby members of the same group and
  refresh health/resource statistics when required.
- Gem Fortune, currency/progression drop amounts, and group talent progression
  bonuses are applied in the cached loot-generation path.

## Performance design

All status programs, status groups, effect IDs, actor variables, socket item
programs, item rows, and combat stat rows are immutable process-wide caches
keyed by the database revision. Hit, block, critical, and monster update paths
do not query SQLite and do not scan the 573-item catalogue. Event dispatch is
an indexed tuple lookup. Nested expansion is bounded to 256 instructions,
geometry results use the existing LRU, and natural status expiry refreshes
vitals once without creating another per-effect timer thread.

Relevant executable coverage lives in `test_socket_effect_protocol.py`,
`test_gem_protocol.py`, `test_player_combat_stats.py`,
`test_world_loot_protocol.py`, and `test_local_combat_state.py`.
