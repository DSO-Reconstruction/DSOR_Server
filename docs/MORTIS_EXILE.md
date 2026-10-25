# Mortis' Exile (`pw214_mortis_boss`)

The arena controller is `services/mortis_arena.py`; content selection and late
viewer spawn filtering live in `gameplay/mortis_arena.py`. It applies only to
Mortis' Exile, not the legacy Mortis or event arenas. Client assets remain
read-only.

## Encounter flow

1. Map entry creates the bone dragon and the additional dragon as dormant,
   invulnerable statues. The three invisible freeze/gate controllers are not
   published as combat actors.
2. `BOSS_start_trigger` starts the native map events. Removing the bone
   dragon's initial freeze plays its authored resurrection. The server also
   waits its 8.36-second startup before permitting damage and attacks.
3. Killing the bone dragon starts `boss_mortis_phase_02_trigger_spawn` after
   the authored three-second delay. One tier-appropriate alternative pack
   spawns. Every actual actor in that pack must die before the mounted phase.
   The old static elimination selector contains obsolete mob names and is
   deliberately replaced by the event-owned actor count.
4. `boss_mortis_mounted_spawn` creates one mounted Mortis at one authored
   location. The four locations are alternatives, not four simultaneous bosses.
5. Mounted Mortis' death starts the unbound event after four seconds. The
   unbound actor waits for the death incident's `DE:6.36`, then appears at the
   authored relative offset from the mounted actor's actual death position.
6. The unbound form's first damage incident creates its skeletons and starts
   the gate pack event. At one-third health, its low-health incident creates
   the knights and releases the additional dragon. Each incident runs once.
7. Gate skeletons surviving their authored lifetime despawn and become rippers;
   surviving rippers become mages. Killing them prevents the transformation.
   These despawns do not grant kill rewards. The white mage's ground aura heals
   living Mortis by the authored 12% when he enters its eight-metre area.
8. Unbound Mortis' death grants the existing `SPECIAL_mortis_unbound_lootlist`
   loot through the normal kill path. Native `BOSS_death` events reveal the
   exits. Remaining adds and dormant actors are retired without extra rewards.
   The return stone's symbolic `home` destination resolves to Cardhun
   (`pw000_hub`) at its travel stone, independently of the last visited town.
   The final form also guarantees one Imperial (`_n`) gem
   from each of the 13 ordinary families: ruby, onyx, amethyst, cyanite,
   diamond, zircon, emerald, rhodolite, and the five elemental diamonds
   (fire, ice, lightning, poison, and andermagic). This bonus applies only in
   Mortis' Exile, bypasses random drop rolls, and preserves ordinary loot.

## Spawn selection

The extracted map stores 1,530 intermission spawn rows across ten tiers and
eight alternative packs per tier. Gate packs have six choices on tiers 0-5
and seven on tiers 6-9. Selection follows these parent groups and their
authored weights, with a stable instance seed. It works when optional `.mvr`
files are absent. The gate packs' off-arena staging transforms are translated
to the gate entrance because the server does not replay the client's staging
walk animation.

## Skills and replication

The final form loads Scythe Strike, Charge, Scythenado and Grave Digger. Charge
uses a destination-bearing `TargetPointBulletSkillCommand`, checks navigation,
damages players along its lane once, and commits the same movement endpoint on
the server. Grave Digger uses `ShiftedSkillCommand` and the existing ground
aura runtime. Both include nonempty destination arrays.

Replayed spawn snapshots omit already released dragon freezes. Event-created
actors carry the current spawn tick. Retired actors remain at zero health;
the normal vicinity reset must not resurrect completed waves.

## Validation

`tests/test_mortis_arena.py` drives real extracted content through the service
event path, including awakening, tier selection, full-wave clear, death timing,
health incidents, survivor transformations, healing, reachable add positions,
skill serialization, damage and final cleanup. Related boss population,
projectile, ground skill and movement tests cover shared behavior.

`tests/test_mortis_gem_loot.py` verifies all 13 exact Imperial gem rewards,
including elemental resistances, one-item quantities, inventory pickup, and
restriction of the bonus to the final Mortis form in this arena.

The user also confirmed the encounter in the live client.
