# Sequences: hit, spawn, despawn, skill, statuseffect

What the client's `sequences/*.pbxml` files contain, how they are addressed
from `static.db4`, and which parts of them the server needs to care about.

Scope: `hit`, `spawn`, `despawn`, plus the two largest packs `skill` and
`statuseffect` (§8–§10), with `death.pbxml` pulled in where the data forced it
(see §5). Decoded with [pbxml_to_xml.py](../../pbxml_to_xml.py) — 113 / 255 /
168 / 1219 / 1369 entries respectively, all decoding cleanly.

**Short version: these are client-side presentation timelines.** They carry
animation, VFX, sound, lighting and camera-shake instructions and contain no
authoritative gameplay state. The server's interest in them is limited to
*addressing* (which sequence name to send) and *timing* — and the timing it
needs lives in `static.db4`, not in the sequence files (§6).

---

## 1. Container format

Same `KCAP`/`LMXB` pack as `quests.pbxml`, documented in
[pbxml_to_xml.py](../../pbxml_to_xml.py). One difference matters when decoding:

A quest document has a single `<Quest>` child, so flattening a pack into one
tree is lossless. A **sequence document has three children** —
`<TrackList>`, `<GlobalParameter>` and `<GUIParameter>` — all under a
`<TrackEditor>` root. Flattening interleaves them, so regroup on the
`packEntry` attribute that the converter attaches. Entry names are
`<folder>/<name>`, e.g. `hit/default_hit_fire`.

---

## 2. Document shape

```
TrackEditor
├── TrackList
│   └── Track                (id, mute, displayChilds, displayName, childs)
│       └── TrackBar         (name, guid, type, mute, startFrame, endFrame)
│           └── Attribute    (name, label, type, value, min, max, default, smartControl)
│               ├── Attribute    — nested, for vector/struct values (x,y,z,w)
│               └── Graph        — animated value
│                   └── BezierSpline  (startPoint, startCP, endPoint, endCP, …)
├── GlobalParameter          (playLength, framesPerSecond, repeat, numOfRepeats, …)
└── GUIParameter             (editor viewport state — not gameplay)
```

`GUIParameter` is level-editor viewport state (`translation`, `scale`) and can
be ignored entirely.

An `Attribute` is either a leaf (`value`) or a container — nested `Attribute`
children for vectors, or a `Graph` of `BezierSpline` segments when
`isAnimated="True"`. Element counts across the three packs: 23732 `Attribute`,
2002 `TrackBar`, 2546 `Track`, 838 `BezierSpline`.

---

## 3. TrackBar types

The `type` attribute is what a track bar actually *does*. 13 distinct types
across these three packs:

| type | hit | spawn | despawn | what it does |
| --- | ---: | ---: | ---: | --- |
| `PlaySoundTrackBar` | 115 | 231 | 189 | `soundname`, `volume`, `soundpos`, `ambientsound` |
| `DrasaGraphicsObjectTrackBar` | 2 | 234 | 162 | spawns a VFX object — `graphicsname`, `graphicstrans`, `goreflag` |
| `AnimationHijackTrackBar` | 52 | 134 | 150 | takes over animation — `animName`, `animTrack`, `fadein`, `fadeout`, `animSpeed` |
| `ColorShaderParameterTrackBar` | 110 | 5 | 4 | shader tint — `shaderParameter`, `color`, `intensity` |
| `AttachedGraphicsTrackBar` | 81 | 26 | 8 | VFX bound to a bone — `graphicsName`, `jointName`, `posOffset`, `rotOffset` |
| `PointLightTrackBar` | 2 | 51 | 32 | `color`, `intensity`, `range`, `shadow`, `flicker` |
| `ObserverCameraShakeTrackBar` | — | 168 | 67 | `intensity`, `range`, `startdelay` |
| `HideEntityTrackBar` | — | 38 | 94 | hides the entity mesh |
| `SkillPhaseTrackBar` | — | 27 | 7 | `eventName`, `eventFrame` |
| `CharacterTrackBar` | — | 5 | 6 | swaps in a character object |
| `AmbienceBubbleTrackBar` | — | 1 | — | post-processing (fog, bloom, saturation) |
| `ImpulseTrackBar` | — | — | 1 | physics impulse — `intensity`, `impulseDir` |

Every one of these is presentation. **No track bar type carries damage,
health, loot, or any other authoritative value.**

`SkillPhaseTrackBar` is the one type that *could* be a gameplay sync point, and
in these three packs it is not: all 34 instances have `eventName="empty"` and
`eventFrame="0"`. It is inert in `skill.pbxml` and `statuseffect.pbxml` too —
see §8.

---

## 4. Timing

All 536 sequences run at **25 fps**, so duration in seconds is
`playLength / 25`.

| pack | n | min | median | max | looping |
| --- | ---: | ---: | ---: | ---: | --- |
| `hit` | 113 | 0.20 s | 0.80 s | 2.08 s | 0 |
| `spawn` | 255 | 0.00 s | 3.00 s | 17.72 s | 4 |
| `despawn` | 168 | 0.80 s | 6.00 s | 60.00 s | 1 |

The shape matches intent: hits are sub-second flashes, spawns are a few
seconds, despawns are long fade-outs.

---

## 5. Addressing: how `static.db4` names a sequence

`_Template_Monster` carries 13 sequence columns, all storing a **bare name**
with no folder prefix (e.g. `stonegolem_death`, `default_hit_physical`):

`PhysicalHitSequence`, `FireHitSequence`, `IceHitSequence`,
`LightningHitSequence`, `PoisonHitSequence`, `DarkMagicHitSequence`,
`CriticalHitSequence`, `HeavyHitSequence`, `DeathSequence`, `DespawnSequence`,
`SummonSequence`, `SpecialSpawnSequence`, `ResurrectSequence`.

All 7964 monsters populate the hit, death and despawn columns.

### The registry

`sequences.freg` is the global index — 4637 lines of
`<folder>/<name> <packfile>`, spanning 49 packs. It is plain text and is the
authoritative name → pack mapping. (`statuseffect.pbxml` and `skill.pbxml`
dominate it with 1369 and 1219 entries; `death` 334, `spawn` 255, `despawn`
168, `hit` 113.)

Resolving every monster column against it by bare name:

| column | resolves | folder used |
| --- | ---: | --- |
| all 8 hit columns | 100% | `hit/` exclusively |
| `SpecialSpawnSequence` | 132/134 (98%) | `spawn/` |
| `ResurrectSequence` | 11/11 (100%) | `spawn/` |
| `SummonSequence` | 78/81 (96%) | `spawn/` |
| `DeathSequence` | 249/250 (99%) | `death/` and `despawn/` |
| `DespawnSequence` | 194/196 (98%) | `despawn/` and `death/` |

**Note the resolution is not strictly folder-scoped.** 121 names exist in
*both* `death/` and `despawn/`, and 52 `DespawnSequence` values are absent from
`despawn/` entirely. Of those 52, **44 are simply the monster's own
`DeathSequence` reused** — the designer pointed despawn at the death sequence —
and 8 are different names that still only exist under `death/`. Overall,
150 of 352 distinct `(DeathSequence, DespawnSequence)` pairs are identical.

So a server or tool resolving these should **look the bare name up in the
column's expected folder first, then fall back to a global registry lookup**.
Folder-only lookup silently fails for ~24% of `DespawnSequence` values.

Two names (`guardian_death_andermagic`, `guardian_death_poison`) resolve
nowhere — content gaps, consistent with the unresolvable tails seen elsewhere
in this data.

### `_Template_Sequence` is a different thing

`_Template_Sequence` (903 rows, columns `Id`, `Name`, `Placeholder`,
`SequenceIsPlaying`, `EntityTriggerRadius`, `SequencerFileName`) covers
**world/cutscene** sequences placed in maps. Its `SequencerFileName` holds the
full `<folder>/<name>` registry key. It does **not** index monster
hit/death/despawn sequences: 0 of 250 `DeathSequence` values appear in either
its `Id` or `Name` column. Don't reach for it when resolving monster sequences.

---

## 6. What the server actually needs

**Do not derive gameplay timing from `playLength`.** The `*Duration` columns in
`_Template_Monster` are independent gameplay timers, and they disagree with the
sequence lengths far more often than they agree:

| comparison | equal | DB shorter | DB longer | DB is 0 |
| --- | ---: | ---: | ---: | ---: |
| `DeathDuration` vs death sequence (281 pairs) | 24 | 221 | 27 | 2 |
| `DespawnDuration` vs despawn sequence (250 pairs) | 34 | 45 | 8 | 161 |

`DeathDuration` is usually *shorter* than the animation, and `DespawnDuration`
is zero for 161 of 250 distinct sequences. These are authoritative
server-side timers; the sequence is free to keep playing client-side.
Examples: `default_wisp_death` — DB 6.0 s vs sequence 2.0 s;
`skeleton_warrior_death` — DB 8.0 s vs sequence 2.8 s.

**Practical consequence:** the server sends the monster template id and a
state change; the client looks up the sequence name in its own copy of
`static.db4` and plays it. The server never needs to parse a `.pbxml`. It needs
the `*Duration` columns to schedule corpse removal and respawn, and nothing
else from this subsystem.

---

## 7. Hit sequences encode material, not damage element

This is the most easily-misread part of the data. Despite there being one
column per damage type, **the sequence name varies by what the monster is made
of** — `bones`, `metal`, `redblood`, `aquablood`, `aquaslime`, `wood` — with
suffixes `_physical`, `_small`, `_crit`, `_noanim` as modifiers.

Measured across all 7964 monsters:

- **99%** have all five elemental columns (`Fire`, `Ice`, `Lightning`,
  `Poison`, `DarkMagic`) set to the *same* value.
- **92%** have `PhysicalHitSequence` equal to that elemental value plus a
  `_physical` suffix.
- Only 45% have `CriticalHitSequence` equal to `PhysicalHitSequence`.

A skeleton warrior, for example:

```
PhysicalHitSequence  default_hit_bones_metal_physical
FireHitSequence      default_hit_bones_metal
IceHitSequence       default_hit_bones_metal
PoisonHitSequence    default_hit_bones_metal
CriticalHitSequence  default_hit_bones_metal_physical
```

The practical distinction is **physical vs non-physical**, not fire vs ice —
the `_physical` variant adds impact/gore VFX, the plain variant omits it.

Confirming this from the other side: the hit flash colour is *identical*
across every `default_hit_*` sequence — `ColorShaderParameterTrackBar` with
`shaderParameter="HitColor"` and RGBA `(193, 0, 0, 255)`, frames 0–5. There is
no per-element colour coding. What differs between materials is the
`DrasaGraphicsObjectTrackBar` particle effect and the impact sound.

---

## 8. `skill.pbxml`

1219 sequences, 5355 track bars. Three new bar types appear
(`TransformTrackBar`, and in statuseffect `ItemAppearanceTrackBar` /
`AttachedCharacterTrackBar`), but the composition is otherwise the same mix of
sound, VFX, animation, light and camera shake.

**Every one of the 1219 sequences runs at 25 fps** — no exceptions. This
independently corroborates the `SERVER_TICKS_PER_SECOND` assumption already
baked into `local_combat_state.py`.

### SkillPhaseTrackBar is inert — the earlier open question, answered

`skill.pbxml` contains 1218 `SkillPhaseTrackBar` instances, essentially one per
sequence. **All 1218 have `eventName="empty"`.** `eventFrame` takes only two
values (`0` on 1025, `1000` on 193), and the bar's own `startFrame` is always 0.

Combined with the 34 inert instances in hit/spawn/despawn and 17 in
statuseffect, that is **1269 of 1269 skill-phase markers carrying no event
name**. The hypothesis that sequences hide gameplay sync points is dead: these
files are presentation-only across the board, including the two largest packs.

Authoritative skill timing lives in `_Template_Skill` instead, in the same
25 Hz frame space: `HitFrame`, `SkillUnblockFrame`, `MotionUnblockFrame`,
`InvincibilityFrames`, `LoopStartFrame`, `ScaleSkillTiming`, plus
`BulletEmitDelay` and `SummonSpawnDelay` in seconds. The server already reads
all of these (`local_combat_state.py`).

---

## 9. `*Sequence` vs `*SequenceMapping`

A naming rule holds across both `_Template_Skill` and `_Template_StatusEffect`:

| suffix | meaning | registry resolution |
| --- | --- | --- |
| `…Sequence` | a direct sequence name | **98–100%** |
| `…SequenceMapping` | an indirection key, *not* a sequence name | 8–50% |

Measured:

| column | distinct | resolves | folder |
| --- | ---: | ---: | --- |
| `Skill.SkillImpactSequence` | 253 | 252 (99%) | `skill/` |
| `StatusEffect.DoneSequence` | 181 | 181 (100%) | `statuseffect/` |
| `StatusEffect.TickSequence` | 56 | 56 (100%) | `statuseffect/` |
| `StatusEffect.StopSequence` | 180 | 179 (99%) | `statuseffect/` |
| `StatusEffect.StartSequence` | 807 | 797 (98%) | `statuseffect/` |
| `Skill.ExecuteSequenceMapping` | 558 | 47 (8%) | — |
| `StatusEffect.StartSequenceMapping` | 4 | 2 (50%) | — |

`ExecuteSequenceMapping` is populated on all 1957 skills, so it matters. Its
values are CamelCase keys with an `_execute` suffix
(`AnderworldCreatureShot_execute`) while pack entries are snake_case
(`anderworld_creature_shot`). Applying that transform — strip `_execute`, split
on case and letter→digit boundaries, lowercase — lifts resolution from
**8% to 80% (448/558)**.

That is a naming *convention*, not the lookup itself. The authoritative mapping
table is not in `static.db4` (these values appear in no other column) and not
as a literal string anywhere under `export_win32/`. It is presumably resolved
in the client binary, plausibly against `_Template_Monster.AnimSet`. Treat the
transform as a heuristic for tooling, not as ground truth.

`Pre-` and `PostExecuteSequenceMapping` are populated on only 4 skills each and
can be ignored.

---

## 10. `statuseffect.pbxml`

1369 sequences, 3518 track bars, over 6421 `_Template_StatusEffect` rows.

The DB exposes a clean **four-phase lifecycle**, each phase with its own
sequence column:

```
StartSequence  →  TickSequence (repeating)  →  StopSequence  →  DoneSequence
                  RestartTickSequence
```

with the timing owned by `StatusEffectDuration`, `StatusEffectDurationPvP`,
`StatusEffectTickRate`, `StatusEffectRealTime`, `ResetRunTime`, `MaxStackSize`
and `TickModifiers`. The separate PvP duration column matters: effect length is
not a single number.

This mirrors what a server must model for buffs/debuffs anyway, so the sequence
pack is confirmation of the lifecycle shape rather than a source of new rules.
`AttachedGraphicsTrackBar` is unusually common here (757 bars) because status
effects bind their VFX to character joints.

---

## 11. Open questions

- **`ExecuteSequenceMapping` resolution.** The real lookup table (§10). The
  80% transform is a convention, not the mechanism.
- **`goreflag`** appears on graphics track bars and is presumably the
  low-violence content toggle; not verified.
- **`HeavyHitSequence`** has only 3 distinct values and `HeavyHitDuration` is 0
  for half of them; what triggers a "heavy hit" server-side is not established.
- **`eventFrame=1000`** on 193 skill-phase bars — with `eventName` empty
  everywhere, the value's purpose is unclear. Possibly a disabled/sentinel
  marker.
