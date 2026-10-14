# Actor-targeted melee reach

Dragonknight `angrystrike` (Rage Attack) is wire skill 1831. Its database
AttackRange and HitRange are both 1.75 units. Those values remain authored data.

The client uses **3D center distance minus the target navigation shape radius**
when deciding whether a targeted cast can begin. The server previously added
`_Template_Monster.CapsuleRadius` instead. Many monsters have a 0.3-unit capsule,
while their `ActorShapeId` selects a much larger navigation footprint. The client
could therefore stop moving and swing while the server rejected every hit.

`data/navigation/navshapes.bin` is the unchanged 89-byte client export from
`export_win32/tables/navshapes.bin`. The parser follows the native octagon:
truncate width/depth to centimeters, take vertex zero `(width / 3, depth)` with
integer division, then divide its Euclidean radius by 100. The resulting world
radii are approximately small 0.525, medium 1.053, large 1.581 and giant 4.215.
`default` aliases `small`. For example, Rage Attack reaches a small monster at
2.275 center distance and a medium monster at 2.803, instead of stopping at 2.05
for either monster's 0.3 physics capsule.

`MonsterCombatant.target_radius` carries this footprint through initial and
runtime monster creation. Actor-targeted melee uses it; projectile and area
collision continue to use their existing physics geometry. Melee accepts either
the current authoritative target position or its last delivered position, without
sweeping across the intervening path. Before the first MoveCommand, cast origins
come from the player's actual spawn rather than the encounter's monster centroid.

Native evidence (current client):

- `Skills::ClientSkillValidator::Validate`, 0x14032bed0, obtains ActorShapeId radii
  with 0x14095dfb4 and calls the range validator at 0x140a1b750.
- 0x140a1b750 compares center distance minus target radius with AttackRange.
  Its distance helper 0x140cb3d98 includes height; planar distance would be wrong.
- `PathEngineLoaderUtil::LoadShapes`, 0x1407d3588, reads the shape table and
  measures vertex zero. `CreateShape`, 0x1407d236c, constructs the octagon.
- `PathEngineServer::GetShapeRadius`, 0x1407c1438, divides by 100 world units.

The corresponding decompilations are in `data/logs/melee-*-native.txt`.
`tests/test_melee_target_range.py` exercises native radius boundaries, actual
TargetSkillCast scheduling/damage, stale movement, height, initial spawn, invalid
targets and preservation of projectile collision behavior.
