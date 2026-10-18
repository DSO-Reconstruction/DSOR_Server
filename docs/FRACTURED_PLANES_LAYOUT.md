# Fractured Planes placement analysis

The server reads placement from the client's `rel261_192` map databases,
PathEngine navigation tokens, and transformed map collision meshes. No edits
to client `data/maps` files are required for these corrections.

## Coverage

The audit resolves all 100 logical floor access IDs through `resolve_map_access`.
They use nine normal layouts and `fp0001_boss_01` on every tenth floor.
`h0009_endlesstower_01_act` and `h0009_endlesstower_01_sm_10` also exist in
the assets, but none of the 100 current floor routes selects them.

All 12 assets are inventoried: 304 fixed spawn rows, 391 spawn-area variants,
43 location-effect volumes, 161 state objects, and 21 trigger volumes.
These are authored row counts, including dormant event variants, rather than
simultaneously active populations.

| Physical layout | Logical floors | Initial combatants | Initial fixed mechanisms |
| --- | --- | ---: | ---: |
| sm_01 | 1, 11, ..., 91 | 26 | 2 |
| sm_02 | 2, 12, ..., 92 | 21–22 | 2 |
| sm_03 | 3, 13, ..., 93 | 38 | 8 |
| sm_04 | 4, 14, ..., 94 | 11 | 5 |
| sm_05 | 5, 15, ..., 95 | 29 | 3 |
| sm_06 | 6, 16, ..., 96 | 36 | 0 |
| sm_07 | 7, 17, ..., 97 | 12 | 6 |
| sm_08 | 8, 18, ..., 98 | 28 | 6 |
| sm_09 | 9, 19, ..., 99 | 12 | 8 |
| fp0001_boss_01 | 10, 20, ..., 100 | 1 | 0 |

Counts above describe map entry, before room events materialize further actors
or the Rift Lord. They are not a replacement for the authored score thresholds.

## Confirmed defects and corrections

The previous density loader ignored `SpawnListId` and sampled the entire
`h0009` template namespace. This included `CharacterSet=invisible` trap helpers
and static shooters as ordinary kill-required enemies. A reconstruction of
sm_06 produced invisible ice-orbit and lightning-shooter actors inside regular
mob rooms. Re-sending those actors could never give them a visible model.

Density areas now use their named family (`Fire`, `Ice`, `Poison`, `Andermagic`,
`sliver_gold`, `ocean`, `machine`, `light`, `dragon`, `shadow`) and the client's
concrete `*_endlesstower` combat variants. Invisible controllers and static
shooters cannot enter these pools. The client distribution lacks the server
SpawnList registry, so original list membership, probabilities, and the split
between names such as `Fire_1` and `Fire_2` cannot be recovered exactly. Both
names currently use the corresponding verified visible tower family.

Mounted shooters and power nodes now preserve the fixed spawn point's exact
XYZ and yaw, including elevated pedestals and off-navmesh mounts. Previously,
the same grounding used for mobile enemies could lower or reject these actors,
and their orientation was reset to zero. Their runtime movement is disabled;
collision separation and pursuit cannot move the mount. Mobile enemies retain
navigation/collision grounding inside the authored density volumes.

Aura-only power nodes have no `Skills` field. They now receive an inert combat
runtime instead of being silently omitted. Their authored permanent status
effects, including the rotating visual, remain in NewMonster state.

Fixed mechanisms and invisible scene controllers are excluded from room-clear
blockers, percentage population, and the all-monsters-cleared fallback. Room
geometry is selected by physical layout, including the reuse of sm_01 on
floors 11, 21, and later cycles. The speculative discard/recreate retry for
already-published living blockers was removed after confirming the template
selection defect.

## Trap, debuff, and floor-completion release

Every damaging `_Instance_LocationEffect` on a tower layout (`kill_aura`,
`_fire_aura`, `noskill_aura`, `_trap_damage`) sits behind one
`_Instance_EventSets` row, and that row exists for nothing else: on all ten
layouts it gates no spawn row, no state object, and no trigger volume. Retail
releases it from a room-clear `MonstersEliminated` selector or a
`PlayerInteractedProp` on the room's `*_start` marker.

Neither fires under Fractured Planes, because the rooms are filled from shared
density areas (`EventSetRow=-1`) and the start markers are never clicked. The
result was a kill aura, a fire aura, or a `noskill_aura` that stayed on for the
whole floor:

| Layout | Hazard sets | Authored release that never fired |
| --- | ---: | --- |
| sm_06 | 6 | `h0009_arena_06_room_closedebuff_signal_01..04` (props `_02/_04/_06/_08_start`) and two miniboss selectors |
| sm_07 | 2 | `h0009_arena_07_room_closedebuff_signal_01`, `..._torchlight_signal_01` |
| sm_09 | 2 | `h0009_arena_09_room_closedebuff_signal_02`, `..._monster_start_signal_01` |
| fp0001_boss_01 | 1 | `fp_boss_01_lythira_spawn_signal` before the arena trigger |

`_fractured_planes_hazard_rooms` binds each hazard set to the nearest authored
density cluster — the room it guards — and `_release_fractured_planes_hazards`
retires the set once that room has no living non-boss actor left. Sets that gate
real content are skipped, and a hazard with no room in reach (a miniboss arena,
a corridor prop, the boss map) is left to the floor-clear sweep, which runs when
every active enemy is down. The Rift Lord's death repeats the sweep with
`force=True` so nothing can damage the player on the way to the opened exit.

Retirement uses `MapEventRuntime.suppress_event_sets`, not a synthetic event
start: starting the authored release event would also spawn path blockers
(`h0009_arena_05_path_01_to_03_block`) or trigger `monster_spawn` rows that were
never meant to run.

Floor completion no longer depends on the boss-announce event. `fp0001_boss_01`
carries Lythira in its load-time population, so she can be killed before the
arena hit trigger ever runs; the clear pass now accepts a defeated `rank_boss`
actor directly once the floor has nothing else alive.

## Towers and rotating effects

Each fixed actor's raw transform, event-set ID, template profile, native skill
ranges, projectile data, and permanent aura geometry are exported to JSON.
The atlas shows the source positions, facing direction, and aura boundaries.
Location-effect volumes retain their full authored transform and EventSet gate.

For example, sm_01's fire shooters are at `(8.50557, 7.36664, 0.37056)` and
`(8.61312, 6.93159, -16.59862)`. Its rotating power nodes are at `(24, 7, -8)`,
`(12, 7, -14)`, and `(12, 7, -2)`. Sm_06's gated power nodes are at
`(244, 9, -12)`, `(234, 9, -18)`, and `(234, 9, -6)`.

`h0009_fire_shooter_02` has native AttackRange 1000 and HitRange 14, but its
straight projectile travels at 4 units/second for 10 seconds: geometric reach
is 40 units before collision radii. The server already caps straight-projectile
acquisition by lifetime; HitRange is not substituted for projectile travel.
`dragan_rotation_aura_small_01` supplies radius 8, rotation speed 20 degrees per
second, and the native `Sphere` aura shape. Its visual rotation does not imply
an invented server-side beam shape.

## Tower and laser damage

Two defects made the fixed hardware harmless.

**Tower damage collapsed on the fire shooter.** `h0009_endlesstower_01_room_
fire_shooter` carries `CharLevel = 100`, and `_load_monster` divides a levelled
template's curve by its own base curve on the assumption that its stats were
already evaluated at that level. For this template that assumption is false:
`HitPoints = 2` and `MinDamage = 15` cannot describe a level-100 monster when a
level-100 player has 450,000 base health. The shooter ended up with **14-23
damage**, while every other shooter on the same floor used level-one
coefficients:

| template | CharLevel | before | after |
| --- | --- | --- | --- |
| `h0009_endlesstower_01_room_fire_shooter` | 100 | 14-23 | **79,509-132,516** |
| `i0011_scaling_ancestral_guard_poison_area_01` | 1 | 106,012-212,025 | unchanged |
| `e_dra_fortress_normal_powernode` | 1 | 47,706-90,111 | unchanged |

`_load_monster(..., coefficient_stats=True)` now skips that division for
`is_tower_mechanism` templates on both the load-time and event-spawn paths.
Tower hardware is invulnerable and stationary, so only its damage figure is
observable; the other eight layouts keep the same numbers they had.

**The laser dealt nothing at all.** `e_dra_fortress_normal_powernode` is named
`Laser` in its template and uses `ui/icon_lasertower`, but its `Skills` column
is empty. It never casts, so the ordinary monster attack path never fired for
it, and the floor's most visible hazard did zero damage. Its damage lives
entirely in the permanent `dragan_rotation_aura_small_01` it wears:

- `AuraTargets = Enemies`, `AuraRadius = 8`, `AuraHeight = 100`,
  `AuraShape = Sphere`, `AuraStarBeamWidth = 1`,
  `AuraStarBeamAngle = 360`, `AuraRotationSpeed = 20`,
  `StatusEffectTickRate = 1.0`
- `AuraEffectId = dragan_rotation_auraBurn_01`, whose tick is
  `CurrHealthPoints:-0.6,relative` - a percentage burn that carries no level
  information and cannot scale with the floor.

`MapEventService._apply_tower_aura_damage` resolves that aura through
`_tower_aura_spec` and applies the tower's own level-scaled
`MinDamage`/`MaxDamage` average once per aura tick. At level 145 a power node
therefore hits for 68,908 and the figure follows `ScalingMonsterDamage` like
every other enemy on the floor. It runs from the floor's 1 Hz limit timer, so
standing still inside a beam keeps hurting, and from the movement path so
walking into one reacts immediately. The 27 laser nodes across the nine
layouts all report damage; sm_02 has none because it uses the
`i0011_scaling_ancestral_guard_poison_*` shooters instead, which cast normally.

## Duplicate NewMonster crashed the client

The client log for floor 2 ended in:

```
ActorManager::RegisterActor(): There is an existing actor with id 196608!
GameWorld::NewMonster(): faild to setup monster!
*** NEBULA ASSERTION ***
No valid effect module found on actor (Id: 65536, ... npc_aurelion_kingshill)
```

`196608` is `0x30000`, the first map-monster actor id, and every floor restarts
its ids there. The duplicate came from `_apply_actor_request`: when the client
asked for one actor in a proximity group, the server sent
`requested_group.spawn_batch`, which is the **whole group**. Tower hardware is
force-published on its own (`_announce_runtime_group`), and after the density
preload it shares groups with ordinary mobs, so requesting any mob in that
group re-sent NewMonster for the hardware that was already in the client. The
client refuses the second registration, fails to build the actor, and asserts
on the resulting module-less actor - the stale Kingshill NPC in the message is
collateral damage from the aborted spawn.

`WorldService._new_monster_batch_for_viewer` is now the single choke point for
every NewMonster batch. It keeps an explicit sent ledger
(`gameplay_new_monster_sent_actor_ids`, cleared per map) and drops any
NewMonster command for an actor id already published, returning `None` when
nothing new is left. `gameplay_spawned_monster_actor_ids` cannot serve as that
ledger: several paths add to it as a reservation *before* they encode, so it
answers "was this reserved" rather than "did the client receive it".

The ledger is only half of the invariant. It must shrink exactly when the
client is told to drop an actor, otherwise a monster that leaves the vicinity
and comes back can never be created again: the client has no such actor, the
server believes it already published one, and the mob stays alive and
invisible. That failure mode looks exactly like "the room is empty", "there is
an invisible monster left", and "I cleared the floor but no boss appeared" -
one unkillable actor keeps the clear gate closed forever.
`WorldService._forget_published_monsters` therefore runs in the same critical
section as every discard (`_cull_distant_monster_groups`, the late-ActorRequest
race discard, `_retire_event_monsters`, and the map-event spawn race discard).
The pair is the whole contract: publish once, forget on discard, publish again
afterwards.

## Rift Lord spawn conditions

Every floor is gated by an authored kill-score rule whose signal is also the
name of the map-local event that spawns the boss:

| Floor | Authored score | Signal / event | Boss actor | Position |
| --- | --- | --- | --- | --- |
| 01 | 315 | `fractured_levelMap_bossAppear_tower_01` | `endlesstower_boss_cherbim_01` | `17.0, 7.8, -8.1` |
| 02 | 185 | `..._tower_02` | `endlesstower_boss_cherbim_02` | `20.0, 8.0, -8.0` |
| 03 | 275 | `..._tower_03` | `endlesstower_boss_cherbim_03` | `94.0, 6.0, -40.0` |
| 04 | 220 | `..._tower_04` | `endlesstower_boss_cherbim_04` | `85.0, 6.0, 52.0` |
| 05 | 220 | `..._tower_05` | `endlesstower_boss_cherbim_05` | `106.0, 7.0, 24.0` |
| 06 | 265 | `..._tower_06` | `endlesstower_boss_cherbim_06` | `238.3, 9.6, -12.3` |
| 07 | 265 | `..._tower_07` | `endlesstower_boss_cherbim_07` | `238.8, 13.0, -14.2` |
| 08 | 220 | `..._tower_08` | `endlesstower_boss_cherbim_08` | `28.0, 8.0, -86.0` |
| 09 | 245 | `..._tower_09` | `endlesstower_boss_cherbim_09` | `30.7, 12.0, -118.9` |
| 10 | 200 | `..._tower_10` | final Rift Lord set | see `sm_10` |

The EventSet that holds the boss row requires that single flag, so the Rift
Lord is spawned by the map-local event with the same name, not by a room
trigger. Three conditions therefore open the fight:

1. the authored kill score reaches the floor threshold, **or**
2. every killable non-boss monster on the floor is dead (the tower floors
   select mobs dynamically and most of them carry no `AddScoreForKill`, so the
   score can never arrive), **and**
3. no delayed map-event start is still queued.

`MapEventService._resolve_fractured_planes_floor_clear` applies condition 2 and
runs on every kill, so it is evaluated exactly when the floor's population
changes. It is deliberately *not* run from the floor's 1 Hz timer: the density
preload materializes the whole floor in `combat.monsters` while only the rooms
around the player are published, so a timer-driven check would see "no living
monster nearby" at floor entry and open the boss before the run starts.
`_fractured_planes_floor_blockers` decides what "dead" means: a living monster
blocks the floor only if some client can actually see it, because a monster
nobody can see cannot be killed. Tower hardware (power nodes, trap shooters,
sentinels, invisible controllers) never blocks, which is why
`is_tower_mechanism` is excluded from the count.

Floor 2 additionally spawns 30 `a0301_normal_g_gen_meanie_rascal_01` minions
with the boss, and floors 1, 4, 6 and 9 spawn their power-node ring inside the
same EventSet, so the boss room keeps its hazards after the gate opens.

## Floor 6 closes its debuffs on prop interaction

`sm_06` carries four interactable `*_start` props (the exam pillars) and the
authored chain is:

| Prop template | Signal | Local event |
| --- | --- | --- |
| `h0009_endlesstower_02_start` | `h0009_arena_06_room_closedebuff_signal_01` | `..._room_sm06_closedebuff_01` |
| `h0009_endlesstower_04_start` | `..._closedebuff_signal_02` | `..._room_sm06_closedebuff_02` |
| `h0009_endlesstower_06_start` | `..._closedebuff_signal_03` | `..._room_sm06_closedebuff_03` |
| `h0009_endlesstower_08_start` | `..._closedebuff_signal_04` | `..._room_sm06_closedebuff_04` |

Each rule is a `PlayerInteractedProp` rule scoped to `sm_06` and matched on the
prop **template** id, and prop interaction fires it with
`(instance_id, template_id)`. If a click does not clear the debuff, check that
the prop was actually published (it is `always_present` in the map data) and
that its state transitioned - the signal is only fired for `PROP_STATE_ACTIVE`
or an actual state flip. The floor-wide sweep in
`_release_fractured_planes_hazards(force=...)` remains the backstop once the
last killable monster dies.

## Fixed hardware is always present and cannot be damaged

Retail places the arena's power nodes, fire shooters, and sentinels behind the
same `fractured_levelmap_bossappear_tower_NN` EventSet that announces the Rift
Lord, so a literal reading of the script makes the towers appear only after the
floor is nearly over. Fractured Planes needs them from the first step into the
room, because they are the floor's obstacle rather than its reward.

Three separate gates had to be opened for that to hold:

- `_load_spawn_areas` honours each density volume's `EventSetRow`. Fixed actors
  are filtered separately in `_load_dynamic_monster_encounter`, and there the
  tower layouts now accept a fixed row whose template is a
  `is_tower_mechanism` even when its EventSet is still closed. The event-spawn
  path already treats a matching live actor as the same actor, so nothing is
  duplicated later.
- `h0009_endlesstower_01_room_fire_shooter` and
  `e_dra_fortress_normal_powernode` have no `Skills` row in `static.db4`, so
  `_load_runtime_definition` rejected them and every load logged
  `no runtime template ... skipped`. Both the fixed-actor and the event-spawn
  path now pass `stationary=is_tower_mechanism(...)`, and a stationary actor
  without a runtime template resolves to an inert definition (zero run speed,
  no autonomous attack) instead of being dropped.
- `_announce_runtime_group` now publishes `is_tower_hardware` actors
  immediately rather than waiting for a proximity request, and
  `services/world.py` gives them a visibility radius of 100000 so neither the
  enter/exit radius culling nor `_cull_distant_monster_groups` ever removes
  them.

Damage is refused at the source. `MonsterCombatant.invulnerable` is set from
`is_tower_mechanism` for both construction sites, and
`_monster_health_after_damage` returns the previous health unchanged, so no
player skill, damage-over-time tick, summon, or scripted hit can take a power
node down. The 93 mechanisms across the nine tower layouts all report
`invulnerable=True` and survive a 10^9 damage hit with unchanged health.

### They are also not selectable

Invulnerable is not enough: the client still builds a targetable actor from the
`NewMonster` packet and offers it as a mob, so a laser looks like something to
attack. `_load_monster(..., non_targetable=True)` writes the
`0x00010004` placement faction instead of `0x00010001`. That is the same
neutral/player-owned faction the summon path uses for dwarf turrets, and the
client refuses to target it. The actor still exists, still renders, and still
applies its aura damage; the player simply cannot click it. Both the fixed
actor path and the event-spawn path pass
`non_targetable=is_tower_hardware(...)`, so controllers (which have no model)
and mobile floor monsters keep the hostile faction.

### Fire rate and reach

Only the *cycle* is accelerated. Reach is deliberately left at the authored
value, because reach is the part the player can see:

- the power node's native status provides the rotating blue beam. The server
  uses `AuraRadius`, `AuraStarBeamWidth`, `AuraStartAngle`, and
  `AuraRotationSpeed` to hit only players intersecting that beam; the broad
  aura target sphere alone does not cause damage;
- the fire shooter has a 1000-unit `AttackRange` but only a 14-unit
  `HitRange`. Widening `AttackRange` makes the tower open fire from further
  away (visible: it shoots at you); widening `HitRange` would splash players
  around the impact point (invisible).

The multipliers live in `gameplay/fractured_planes_layout.py` so the aura tick
and the autonomous attack runtime cannot drift apart:

| Constant | Value | Effect |
| --- | --- | --- |
| `TOWER_ATTACK_RATE_MULTIPLIER` | 2.0 | fire shooter cycle 0.600 s -> 0.300 s |
| `TOWER_ATTACK_RANGE_MULTIPLIER` | 1.6 | fire shooter `AttackRange` 1000 -> 1600 (acquisition only) |
| `TOWER_AURA_RATE_MULTIPLIER` | 2.0 | laser aura tick 1.0 s -> 0.5 s |
| `TOWER_MIN_ATTACK_INTERVAL` | 0.25 | hard floor, no tower fires faster |

`_load_runtime_definition(..., stationary=True)` applies the attack multipliers
to every usable attack of the actor but leaves `hit_range` untouched.
`_tower_aura_spec` reads the authored beam range, width, start angle, rotation
speed, and tick rate.

The laser hit test uses a rotating **3D beam segment**. A player inside the
8-unit aura sphere but outside the blue beam must not take damage; players on
another height must not be hit by the floor-level beam.

## Gated density rooms are pre-materialized

Floor 1's `light`/`dragon`/`shadow` room is gated by EventSet 2746, whose only
required event is a `playerinteractedprop` rule on
`h0009_endlesstower_01_room_05`. Fractured Planes fills its rooms from
`EventSetRow=-1` density and never clicks that prop, so the volume stayed empty:
`monster_03_end_signal` never fired and the trap beside it never lifted.

`_load_spawn_areas(..., preload_gated=is_tower_layout(map_id))` now materializes
every gated density volume at load time on the tower layouts. The rows are
recorded in `LocalMonsterEncounter.prematerialized_spawn_area_rows`, and
`event_area_spawn_rows` skips them, so a later event start cannot populate the
same volume a second time.

Floor 1 after the change holds 30 room monsters at their authored grid
positions — `(39|43|47, -41.8|-37.8|-33.8)` for the ice room,
`(39|43|47, 22|26|30)` for the ocean room, and
`(-20.4|-16.4|-12.4, -10.1|-6.1|-2.1)` for the shadow room — plus the eight
arena leaders and five mechanisms.

## Room membership ignores nothing that can still fight

The room-clear test in `_update_fractured_planes_clear_state` used to intersect
its members with `active_monster_actor_ids`, i.e. only monsters this client had
already been told about. Every density room is materialized at load, so a
living blocker that had not yet been announced was silently skipped and the
trap lifted over its head. Membership is now the authored home position of
every non-mechanism actor in the room; a room with a living member that has not
been rendered yet announces that member instead of opening.

## Reproduce the audit

```powershell
python tools/analyze_fractured_planes.py
```

Outputs are under `data/diagnostics/fractured_planes/`:

- `layout.json`: all floor routes, generated actor coordinates, source rows,
  events, navigation triangles, profiles, and skill/aura parameters.
- `index.html`: a local atlas with a floor selector and hoverable XYZ details.

The audit builds the 23 distinct initial placement variants, checks mobile
actors against concrete collision ground, verifies that density pools contain
no mechanisms/controllers, and checks enabled fixed actors against their raw
XYZ/yaw. It also exercises the actual event-spawn path for mounted actors on
all layouts, and advances the combat simulation to check that fixed actors
remain stationary. Mounted shooters are checked for successful in-range casts;
room-clear logic is exercised with ordinary enemies dead and mechanisms alive.
This is offline data/runtime verification; client rendering
and full floor completion still require an in-game run after server restart.

The trap release and floor completion paths were verified separately with a
throwaway harness that builds the real encounter and `MapEventRuntime` for each
physical layout, kills every non-mechanism actor through `_on_monsters_killed`,
then kills the Rift Lord. All 100 logical floors reported
`fractured_planes_floor_completed=True` with no hazard EventSet left enabled.

Fixed hardware was checked the same way: `LocalCombatState` is built from the
real encounter for each of the nine tower layouts, every actor whose template
satisfies `is_tower_mechanism` must carry `invulnerable=True`, and a 10^9 damage
hit must leave its health unchanged. The nine layouts hold 5, 6, 16, 11, 9, 7,
12, 12, and 15 mechanisms (93 total), all of which pass. Floor 1 was checked
separately for population: 30 room monsters sit on their authored density grid,
inside the `monster_01`, `monster_03`, and `monster_05` signal volumes, so all
three room clears are reachable without the prop interaction retail used.

Tower and laser damage was checked per layout: a player position intersecting
each rotating laser beam must produce a `DamageResolution` of the node's scaled
average (the same node reads 68,908 at level 145), a player elsewhere in the
aura sphere must not be hit, and a second call inside the tick window must not
stack. All 27
laser nodes across the layouts pass. The duplicate-spawn guard was checked by
feeding the same proximity group's spawn batch through
`_new_monster_batch_for_viewer` twice: the first call yields 8 NewMonster
commands, the second returns `None` and logs the suppressed actor ids.

The round-4 changes were verified with two throwaway harnesses:

- `_load_monster` for `e_dra_fortress_normal_powernode` returns
  `placement.field_00 == 0x00010001` by default and `0x00010004` with
  `non_targetable=True`; `_tower_aura_spec` returns `(8.0, 0.5)` - the authored
  radius with an accelerated tick; `_load_runtime_definition(stationary=True)`
  for the fire shooter returns `attack_interval 0.300` (from 0.600),
  `attack_range 1600.0` (from 1000.0) and an unchanged `hit_range 14.0`; and
  the sent ledger suppresses the second NewMonster for `196608` while
  `_forget_published_monsters` lets the same batch through again afterwards.
- `_fractured_planes_floor_blockers` was driven with synthetic combat states:
  an all-dead floor reports no blockers, one living *visible* monster blocks,
  one living monster that no session has published does not block, tower
  hardware never blocks alive or dead, and a floor whose active set was culled
  to empty still opens once its monsters are dead.
