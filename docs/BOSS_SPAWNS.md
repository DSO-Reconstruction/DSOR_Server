# Boss population and authored spawn locations

The 2026-09-09 investigation reproduced density-generated boss copies on
story and event dungeons, including `a0702_kingscity_past_dun`. The map content
already supplies the boss locations; the extra copies came from server fallback
population generation.

## Cause and correction

`_area_template_pool` used a map's fixed actors when no map-prefixed normal
monster templates existed. This included bosses. `AreaType=Boss` prevented that
on dedicated boss rooms, but the affected examples are `ClosedPVE` dungeons.
Several event template families also differ from their map names, making this
fallback common there.

Density pools now exclude every `Rank_boss*` template and staging actors that
resolve to a concrete boss. The filter applies to map pools, explicit list
mappings, fixed-actor fallbacks, and scripted event selectors. Fixed spawn
points and their event gates remain authoritative. The disconnected-navigation
replacement pass can place each boss template at most once.

An empty `SpawnListId` no longer falls back to fixed actors. Such areas retain
their priority exclusion geometry, including Kingscity's `no_spawn_entrance`
and `no_spawn_boss_area`, but contribute no generated population or quota.

Kingscity's only `a0702` monster template is Sargon. Its `skeleton`, `spider`,
and `beast_hound` lists now use the corresponding `reactivation_normal_*`
templates. This species mapping is inferred from the shipped list and template
names and the map's existing reactivation actors. Original server spawn-list
membership and random weights are not included in the client assets.

## Boss spawn locations

`data/maps/<map>.db4` contains `_Instance_SpawnPoint` rows with `NpcId`,
`Transform`, and `EventSetRow`. `Transform` is a 4-by-4 little-endian float matrix;
elements 12, 13, and 14 give world X, Y, and Z. These are actual actor placements,
not merely minimap symbols. Navigation and collision may correct their height.

Kingscity has exactly one `a0702_dun_boss_sargon_shadow_spawnpoint`, at
**(136, 14, -6)**, with no event gate (`EventSetRow=-1`). It has no rows in
`_Instance_PositionMarker` or `_Instance_MiniMapMarker`. Its nearby
`a0702_reactivation_10_explore` trigger is centered at `(137, 12, -2)`; that is a
quest trigger, not a second boss placement.

| Map | Boss template | Authored world position (X, Y, Z) | EventSetRow |
| --- | --- | --- | --- |
| `a0702_kingscity_past_dun` | `a0702_dun_boss_sargon_shadow` | `(136, 14, -6)` | -1 |
| `e_chr_challenge_dun_01` | `e_chr_2015_boss_krampus_challenge_01` | `(177.289, 6.089, -63.516)` | -1 |
| `e_chr_challenge_dun_02` | `e_chr_2015_boss_krampus_challenge_02` | `(95.384, 7, -72.771)` | -1 |
| `e_chr_challenge_dun_03` | `e_chr_2015_boss_krampus_challenge_03` | `(59.891, 6.753, -73.381)` | -1 |
| `e_b1205_dun_chr_2025_01` | `e_chr_boss_big_paws_mom` | `(28.784, -0.714, 139.153)` | -1 |
| `e_dra_03_2025_dun_01` | `dra_03_boss_dragan_2025` | `(0, -0.988, 7.788)` | -1 |
| `e_easter_dun_01` | `e_easter_01_dun_boss_troll_01` | `(32.630, 0.557, 70.199)` | 1106 |
| `e_fullmoon_varholm_blood_dun` | `e_ful_02_varholm_bloodmoon_00_leader_mage_01` | `(-93.456, -1.822, -11.184)` | 1132 |
| `e_hal_01_castle_dun` | `e_hal_01_boss_gwenfara_phase_01` | `(145, 48, -68)` | 154 |

A nonnegative event-set row means the point belongs to an event-controlled
population. Multiple rows can represent intentional stages or difficulty
variants. For example, Darkwood has Wendigo flee locations and separate final
battle event sets. These must retain their event gating; globally deleting all
but one boss row would break the encounter sequence.

## Scope of the content audit

118 shipped map databases contain a hostile boss-ranked spawn point. At the
default difficulty, 18 maps have active density areas, allow density population,
and lack a normal map-prefixed pool. This is an exposure list, not a claim that
all bosses are active immediately on entry:

- `a0702_kingscity_past_dun`
- `e_b1205_dun_chr_2012_02`, `e_b1205_dun_chr_2024`, `e_b1205_dun_chr_2025_01`
- `e_chr_challenge_dun_01`, `e_chr_challenge_dun_02`, `e_chr_challenge_dun_03`
- `e_dra_03_2025_dun_01`
- `e_dra_03_dun_gloomypastures_01`, `e_dra_03_dun_ironforestnight_01`
- `e_dra_03_dun_jail_01`, `e_dra_03_dun_mystranight_01`
- `e_dra_03_dun_storage_01`, `e_dra_03_dun_wildforestnight_01`
- `e_easter_dun_01`, `e_fullmoon_varholm_blood_dun`
- `e_hal_01_darkwood_dun`, `e_sum_dun_01`

A follow-up scan checked 815 selected area pools across the 118 boss-containing
maps, making all event-set rows eligible for inspection at default difficulty.
No resulting pool contained a boss or resolvable boss staging actor. Other
maps' missing retail spawn-list memberships still use the existing non-boss
fallback; this change does not reconstruct all event support compositions.

## Verification

Default population generation before and after the correction:

| Map | Boss count before | Boss count after |
| --- | ---: | ---: |
| `a0702_kingscity_past_dun` | 124 | 1 |
| `e_chr_challenge_dun_01` | 150 | 1 |
| `e_chr_challenge_dun_02` | 112 | 1 |
| `e_chr_challenge_dun_03` | 179 | 1 |
| `e_b1205_dun_chr_2025_01` | 18 | 1 |
| `e_dra_03_2025_dun_01` | 176 | 1 |

Kingscity now has one Sargon, nine authored guardian spiders, and 148 generated
normal supporting monsters under the default configuration. No generated
support stands in either named no-spawn volume.

The regression suite verifies normal/scaling boss counts and anchor positions,
support retention, empty-list exclusion, boss staging actors in every pool
source, event selector filtering, disconnected-navigation replacement, and
preservation of explicit story phase rows. Run with:

```powershell
py -3.12-64 -m pytest tests/test_boss_spawn_population.py tests/test_monster_population_scale.py tests/test_map_events.py tests/test_event_waves_and_vendors.py tests/test_map_companions.py tests/test_dungeon_runtime_fixes.py tests/test_local_combat_state.py tests/test_dungeon_quests.py -q
```

211 tests passed across these suites. This verifies generated server state;
interactive client playtesting has not been performed. Restart the server to
replace previously cached encounter populations.
