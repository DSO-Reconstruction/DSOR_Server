# Server feature gap audit — 2026-10-26

Compared `C:\game-client` with the current working tree at
`C:\server`. The original audit was read-only;
implementation follow-ups and their verification are recorded below.

The initial gaps were stale quest content, incomplete live event/world updates,
authoritative gameplay rules and progression, multiplayer PvP, social
management, and newer activity-specific workflows.
The server already implements much of the core game, so treating whole systems
such as quests, crafting, events, trading, or PvP as absent would be misleading.

## Baseline and confidence

- Client: packaged executables/assets, not source code. `dro_client64.exe`
  contains `release261_1024`; SHA-256
  `5ccb022ed3637cd977900d20cfbe1c802caf9250ec52877064442508438d3db6`.
  This exactly matches the executable documented in `docs/COMMAND_IDS.md:20`.
- Protocol: `docs/command_ids_rel261_1024.json`, the server default at
  `protocol/command_ids.py:34`. It contains 371 registered class names. This
  includes requests, responses, internal commands, and obsolete/debug surfaces;
  it is not a denominator for a feature-completion percentage.
- Client `export_win32/db/static.db4` and server `data/db/static.db4` are
  byte-identical: 68,861,952 bytes, 198 tables, SHA-256
  `4cbcf141ecb0e06f7784496a3c77150c149889d1d67a12399107922916ca29ba`.
- Both map directories contain 391 `.db4` files with identical filenames and
  SHA-256 hashes. Quest content differs: the current client has 1,768 quests
  versus 1,758 in the server's default `data/quests.xml`.
- Findings use current code, read-only database queries, client UI assets, and
  isolated runtime probes. Command-name searches were checked against numeric
  dispatch branches: for example, tower requests `0x0170` and `0x0174` really
  are implemented despite not appearing by class name in the dispatcher.
- No live client playthrough was performed. Shipped UI/content demonstrates
  a client contract, but does not prove every retained activity is currently
  accessible. Such uncertainty is identified below.

Priorities are implementation recommendations: **P1** affects ordinary play,
authority, or existing progression; **P2** completes important secondary
systems; **P3** needs product decisions or further client investigation.

## Prioritized findings

### 1. P1 — Current quest content is missing or stale

**Implemented:** `data/quests.xml` now matches all 1,768 current-client quests.
The ten Fractured Planes quests and four Summer changes below are included.
Restore trims removed trailing-task bits while retaining counters, completion
timestamps, and closed/cancelled states. Already delivered Summer quests become
ready for normal turn-in; restore never grants or repeats a reward. Future
interior task removal or reordering still needs an explicit identity migration.
The following evidence describes the original content drift.

**Confirmed content drift.** In-memory decoding of the supplied client's
`export_win32/quests/quests.pbxml`, followed by structural XML comparison
(tags, attributes, and child order, ignoring whitespace), found ten missing
quests and four changed quests. There are no server-only quests.

All ten missing quests are level-100, live-enabled Fractured Planes quests:

- `fracturedplanes-main-01-01`
- `fracturedplanes-main-02-01` through `fracturedplanes-main-02-04`
- `fracturedplanes-main-03-01` through `fracturedplanes-main-03-04`
- `fracturedplanes-main-04-01`

They form linked introduction/progression chains involving Aurelion and Kale;
the last is tied to `event_fracturedplanes` and `fp0001_boss_01`.

| Changed quest | Difference from current client |
| --- | --- |
| `e-sum-effect-quest-n` | Server awards `costume_effect_summerevent`; current client defines `vanity_groundmarker_40`. Server XML line 46299. |
| `e-sum-wolf-quest-new-n` | Server awards `epicmount_wolf_summer`; current client defines `costume_effect_summer2026`. Server also retains a removed final completion task. XML line 47071. |
| `e-sum-quest-2024` | Server retains a final completion/NPC-encounter requirement removed from the client. XML line 46985. |
| `e-sum-wolf-quest-new-n-jewel` | Server retains a final completion/NPC-encounter requirement removed from the client. XML line 47028. |

The runtime defaults to this stale XML through `paths.py:18` and
`services/quests.py:257`. Existing tower mechanics do not supply the missing
quest chains. Synchronize definitions from the current client, review saved
progress compatibility for changed task layouts, and verify offer/accept/
progress/reward flows. Do not blindly replace persisted quest progress.

### 2. P1 — Authoritative player skill cooldowns

**Implemented:** all six player cast request variants now check server-owned
cooldown categories and casting recovery before payment or combat side effects.
Authored cooldowns, scoped/stacked talent modifiers, attack-speed timing, and
forced escape casts are respected. Failed resource payment rolls back the
reservation. Delayed projectiles, multi-hit resolution, and summon attacks keep
their existing execution paths.

Cooldown deadlines survive character saves, map handoffs, and reconnects using
epoch timestamps in persistence and monotonic deadlines while online. Accepted
`ActiveCoolDown`/`ActorCoolDownReset` programs update remaining cooldowns;
Singularity's managed aura applies its authored periodic reduction and stops
when the player leaves. Round starts reset authoritative deadlines and casting
recovery. The following evidence describes the original enforcement gap.

**Partial implementation.** The server validates ownership, feature locks,
resource payment, and essence spending, but does not enforce a player skill's
cooldown or shared cooldown category before accepting a cast.

Evidence: `services/player_combat.py:157`, `:247`, `:354` and the spatial-skill
path; player skill geometry loading at `gameplay/local_combat_state.py:2641`
does not load `CoolDown`/`CoolDownCategory`. The monster path does enforce
cooldowns, so it should not be confused with player enforcement. Current
`_Template_Skill` data includes Wild Pack at 60 seconds, Iron Dwarf at 30
seconds, and Teleport at 5 seconds.

Implement server-owned per-skill/category deadlines and casting cadence,
including talent/effect modifications and explicit reset effects. Verify that
repeated premature requests neither execute nor consume resources, while
legitimate cooldown resets and haste still work. This is a code-level finding,
not a claim that a live modified-client exploit was exercised.

### 3. P1 — Multiplayer PvP rules and round completion

**Objective modes and matchmaking (2026-10-27): implemented.**
Capture the flag (`c2103_ctf`, ruleset 8) and Assault the Fortress
(`f4103_atf`, ruleset 13) are playable and queueable
(`gameplay/pvp_objectives.py`, `services/battleground_objectives.py`,
`services/battleground_fortress.py`). CTF: flags are world items taken by
touch (`ItemAutoPickUpRadius`) or PickupItem, carrier banner effect,
PVPState HUD line, MessageCommand 12–22, captures as team score, 3 captures
or timeout (leader wins, level score = draw), authored respawn delays with the
death dialog's Respawn button, 30 s flag reset (local choice). Fortress:
monsters can now belong to a team (`LocalCombatState.monster_teams`; no team =
hostile to everyone, unchanged), shown hostile/friendly per viewer; stand-ins
and towers are driven by the authored MonstersEliminated signals; the
drawbridge and gun controls are stage-gated; stage clear sends message 24,
PVPState 9, StageInfo and the `PVPRoundTimeModifier` time bonus; control
points capture in 10 s and spawn rammers/bombs for the attackers. Matchmaking
keeps parties on one team, refuses oversized or too-wide parties with the
client's own events, and applies `PlayerXpLevelSearchRange` /
`PlayerPvPLevelSearchRange` with `PlayerRatingMaxWaitTime` widening. Wins feed
`CaptureTheFlag_5v5`, `AssaultTheFortress_6v6`, `BestOf3_1v1`, and
`BestOf5_3v3` for the team deathmatch (same map and ruleset). FFA now loads
with ruleset 9 as its settings row says (live check needed). Not implemented:
siege units attacking structures, path following, bomb damage (its skill has
no DamageModifier), the 0xD4/0xD5 group invite flow, auto-balance, match
achievements, honor pools, arena item spawns. Tests: `tests/test_pvp_*.py`.

**Implemented for elimination modes:** round deaths are retained, duplicate
callbacks cannot award another round, and each new round restores eligibility.
FFA uses eight independent teams and the eight authored arena spawn slots,
with solo career settlement. Timeouts use damage from the current round.
CTF and fortress queue requests are rejected until their objective workflows
exist; this does not hide their client UI. Party-preserving/rating matchmaking
and objective modes remain open work. The evidence below describes the
original defects.

**Partial implementation with reproduced defects.** Queues, invitations,
transfers, combat, rounds, and honor settlement exist. The generic team logic
does not correctly implement all advertised modes.

- Eight-player FFA is configured as one eight-member team. The registry assigns
  all players team 0, while `services/battleground.py:616` only allows opponents
  with different teams. An isolated registry/service probe returned no legal
  opponents between two FFA participants.
- `gameplay/pvp_match_state.py:248` checks surviving teams using only the latest
  victim in its dead set (`:265`). It does not retain previous round deaths.
  A service-level probe killed all five members of one team through
  `_handle_match_event`; their deaths were recorded, but the round remained
  running and `_finish_match_round` was never called. The production caller
  at `services/battleground.py:666` relies on this result.
- Capture-the-flag and fortress are accepted mode configurations, but their
  objective lifecycles are not implemented by the elimination-round engine.
- Match formation is FIFO by mode and splits individual players sequentially
  (`gameplay/pvp_match_state.py:374`). Party-preserving matching and level/rating
  matching are absent; exact retail matchmaking policy has not been recovered.

First fix persistent per-round elimination state and FFA hostility/scoring.
Then implement separate flag and fortress objective rules, including respawn,
scores, timeout resolution, and cleanup. Until those rules exist, the available
mode list should reflect the modes that can actually be completed correctly.

### 4. P1 — Achievement progression, rewards, and account scope

**Progression batch implemented; remaining sources listed below.** Achievement
item rewards now carry persisted pending/granted receipts. The inventory grant,
completion state, and any shared account receipt reach SQLite in one transaction
before publication. Full bags retain pending rewards, retried on subsequent
events/map entry. Class-specific reward selectors resolve to the character's
authored item. All 140 positive-amount reward definitions resolve successfully
(141 rows name a reward, but one has zero amount).

All 38 account-scoped definitions now share progress across characters. Account
events are serialized, old copies migrate by their highest progress rather than
being summed, and shared completions survive character deletion. Existing
completion stamps/unlocks are retained; historical completions do not receive
retroactive rewards from previously automatic development unlocks.

Natural hooks now cover crafting, equipped-item kills and equipment snapshots,
event collectibles, rune/pet/mount use, stickerbook collection, guild membership,
keyed currency collection, open-world PvP kills, and duel wins. Login reconciles
class levels, equipped items, and the stickerbook. Snapshot events cannot be
farmed by repeating the same equip/login action. Crafting counts executions,
not the number of produced items. These changes live in
`gameplay/achievement_content.py`, `services/achievements.py`, the corresponding
mutation services, and the account/player database persistence modules.

The initial audit found the following 273 definitions without event evaluators:

| Unsupported definition kind | Enabled rows |
| --- | ---: |
| Crafting | 86 |
| Kill rules using `nomob` | 75 |
| ItemKill | 30 |
| EquipItem | 16 |
| EventProgressCollected | 16 |
| Use | 12 |
| UnlockSkin | 9 |
| Stickerbook | 9 |
| PvP_Kill / PvP_Match | 9 |
| EquipRarity / GuildMembers / VeteranLevelUnlock | 9 |
| CollectedGameCurrency / HighScoreRanking | 2 |

The follow-up probe corrects the old Kill breakdown: 74 use `nomob` and one has
an empty parameter. The current engine indexes 847 of 923 enabled definitions;
this measures evaluator coverage, not proof that every authored objective is
reachable through implemented gameplay.

**Follow-up (2026-10-27):** 848 of 923 enabled definitions now have an
evaluator; the 75 without one are the `nomob`/empty-Param legacy Kill rows,
which stay **retired by decision** (as in retail): they never progress or
complete, and progress held by old saves is kept but cannot complete them.
Kill events also carry `template:difficulty`, so the 43 authored
difficulty-qualified Kill rows can progress. HighScoreRanking matches
`1 <= rank <= param` and is fed by ranking settlement. PvP_Match values are
normalised by battleground mode. VeteranLevelUnlock reads
`_Template_VeteranLevel` (account age in whole anniversaries + count of level
100 characters). UnlockSkin counts an authoritative, persisted
`AccountState.unlocked_skins` collection built from held items' rendered skins
(validated against the retail capture: 60 of 60 derived skins were in retail's
unlocked list). Some authored `pw_difficulty_*`/`kill_defeat_mortis_*` targets
remain unreachable with current content (PW Mortis spawns use a template Name,
no difficulty-10 access row). Tests: `tests/test_achievement_sources.py`.

**Deferred by decision — skin catalog enforcement:** initialization still
sends every visible class skin in `ItemCustomizationInfo`, so all skins stay
usable for free. The collected set (`unlocked_skins`) only drives the
UnlockSkin achievements. Enforcing it later means: send the collected set at
init (`services/player_init_world.py`), validate apply-skin requests, add the
SkinConsumable use path with `ItemCustomizationUpdate` (bool + atom) and the
`skinAlreadyUnlocked` message, answer skin purchases (304 skins have an
`OfferId`) with `SkinPurchaseResponse`, and grant skin rewards
(`veteran_level_02` lists SkinConsumables with ItemAmount 0).

Remaining sources: those 75 unspecified Kill targets, one HighScoreRanking
definition, nine UnlockSkin definitions requiring authoritative skin ownership,
and three VeteranLevelUnlock definitions without a veteran-unlock action.
Three PvP_Match rows target unavailable 3v3, flag, and fortress modes; only the
authored duel mode is credited. The 5v5 queue is not treated as the old 3v3 mode.
Level-rally achievements observe actual event progress; automatic conversion of
level gains into rally progress has not been invented.

### 5. P1 — Recipe discovery and loot-table crafting outputs

**Implemented for current authored content.** New and persisted-empty recipe
books remain empty. Consuming a live RecipeItem or RecipeConsumable adds its
unlock resource and publishes RecipeBookInfo; consumption and the book share a
durable save. Existing saved unlocks remain intact. Current content supplies
153 unlock resources (108 RecipeItem and 45 RecipeConsumable). Workbench
requests enforce the recipe book, required achievement, live flag, and level,
including the enchantment-transfer entry path. Initialization no longer grants
the 72 recipe-gate achievements.

Rune extensions now use the authored level-100 Divination Stone: one consumed
stone unlocks slot 18, and a second unlocks slot 19. New characters start with
five rune trinkets. Extra-slot access is persisted separately from achievements,
so legacy occupied/unlocked slots remain usable after unequipping and relogging
without granting new achievement points. Startup no longer grants the two rune
achievements automatically. The ordinary Use hook advances them when a stone
is actually consumed.

All 2,574 live ResultIsLootTable crafting rows are opal dismantles. They now
return the three exact component gems from `_Template_OpalCrafting`. The client
Opal Divider text (`language/en/db.upgradeitem.xml`) explicitly describes this
three-gem result; creation dust is not returned. No random drop weights are
invented. Unknown/malformed table families fail before consuming ingredients.
These rows have no recipe-book reference, so their count is not a percentage
of visible recipes.

`gameplay/crafting_outputs.py`, `services/recipe_progression.py`, and
`services/rune_progression.py` implement these paths. Direct results, opal
creation, and enchantment transfer are preserved. Capacity, payment, execution
count, input ownership, duplicate IDs, and stale concurrent snapshots are
checked before commit. Reward packets follow the originating crafting/use
packet so older allocation snapshots cannot erase the newly granted items.

### 6. P1 — Item-set bonuses do not receive all existing gameplay events

**Event and program lifecycle batch implemented.** Current data contains 253
sets and 1,076 parsed set modifiers. The initial resolver accepted only hit
and kill, excluding the following authored actor rules:

| Trigger | Rules |
| --- | ---: |
| Health-globe pickup | 24 |
| Block | 4 |
| Low health | 4 |
| Ingredient pickup | 4 |
| Resource spending | 4 |
| Maximum-stack events | 10 |

These events now feed item sets from the existing authoritative transitions,
including when no jewel/socket proc is equipped. Pickup events follow successful
consumption; resource events require an actual paid debit. Rejected/free casts
do not fire them. Low-health effects stop on the authored OFF transition, and
max-stack events fire only when an accepted occurrence crosses its cap.

`gameplay/item_set_procs.py` now preserves D/DP/DE, detail parameters, target,
and OFF metadata, including active-tier and live status `modAE` changes.
`services/item_set_events.py` routes player/location effects and delayed starts.
Temple torso alternatives count as one set part, allowing its real five-piece
tier to activate. Its conditional guardian rule joins the currently equipped
torso's authored UniqueModifier only while the enabling set effect is active.
CNY's real twentieth Angry Strike stack unlocks its hidden skill once and
removes that unlock when its parent ends.

The initial audit's "25 instantaneous effects" description was too broad:
16 rows are native Persistent Mortis roots, and nine have explicit timed
overrides despite zero template duration. `services/item_set_programs.py` now
keeps those lifetimes distinct. Counted Mortis variables cap at 50, managed
children contribute once, and player state is never stored in a shared map
variable. Start/Stop/Done cleanup, the authored terminal self-death, timer
cancellation, and generation checks prevent stale callbacks from affecting a
replacement map. Set removal clears its triggered effects. Still-equipped
skill-derived effects survive unrelated equipment refreshes.
Accepted roots, managed children, vitals, and expiry timers finish updating
even when an outbound packet fails; the original transport error is then
returned. A failed child send cannot leave unsaved or unremovable set bonuses.

The six concrete native-persistent Mortis sources/children use the existing
status-entry/name persistence via `services/item_set_persistence.py`. Login
restores counts without replaying programs, prunes unequipped/orphan sources
before computing vitals, and does not resurrect effects removed by death.

This is not a claim that every complex set is complete. Black Dwarf's downstream
minion-specific feedback, targeted modifications, and aura-duration behavior
still need their own implementation/verification. Its event registrations and
max-stack dispatch are now connected; full minion behavior is a separate gap.
The current data specifically requires `AuraTargets:Minion` with template
allowlists, delayed eight-second overheat programs, turret-specific damage and
victim effects, feedback to the caster, and `StatusEffectGroupDuration` with
`RunTimeUpdate` for TurretDespawn/TurretVisual. Mercenary Ember Knight's existing
overheat handling does not implement these Black Dwarf turret rules.

**Black Dwarf follow-up (2026-10-27): implemented.** `gameplay/minion_auras.py`
and `services/minion_auras.py` add the generic rules: `AuraTargets:Minion`
auras give children only to the owner's own minions on the template allowlist
within `AuraRadius`; delayed `addAE ... DE:` programs bound to one turret
incarnation (the 8 s overheat); minion-hosted auras passing children to nearby
own minions (Tesla absorb); `$N:<ActorVariable>` resolved from the host turret
or the owner; `Despawn` in Done/Stop programs; owner feedback gated on a living
matching minion; and `StatusEffectGroupDuration ... RunTimeUpdate` rescaling
turret lifetimes and despawn auras. All four tiers of the dwarf set (Lightning
conversion, overheat per turret type, 10/20/30-stack effects including the
Tesla absorb explosion) and the warrior Defiance hammer despawn run through
the real code paths (`tests/test_black_dwarf_turrets.py`). Open: the client may
not extend its own despawn-aura display when the server lengthens a lifetime
(needs a live check), and viewers joining later do not receive turret effects
applied before they arrived. The mercenary SoulLava `ember_knight_slag_aura`,
also a set Minion aura, now enables Kain's Smolder per the data (untested end
to end).

**Set skill rules (2026-10-27).** The caster's skill inputs already include
every equipped set `SkillStatusEffect` rule (all 2,017 rule/skill pairs carry
the rule's chance), so the proc path no longer applies them a second time:
that had doubled stacks (CNY Angry Strike reached its 20-stack unlock in 10
casts) and rolled chances twice (30% became about 64%). Viewers' copies of a
hit are built only from what the caster resolved, so a watching player's own
sets no longer add procs to someone else's hit. Effects hosted by the caster
(`EffectHosts: Causer`) in skill victim columns now land on the caster once
per hit instead of on the monster; this includes the on-hit self-buffs of
Mighty Stab, Frost Wind and Ice Charge. Tests: `tests/test_item_set_skill_rules.py`,
`tests/test_black_dwarf_combat_resolution.py`.

### 7. P1 — Quest defense encounters and status-effect rewards

**Defense runtime and all 16 status-effect rewards implemented; exported
defense content remains incomplete.** The corpus contains 85 defense tasks
across 80 quests (78 tasks are enabled in live content), referencing 90 configs.
The actual resolver can construct 49 tasks across 47 quests; 48 tasks work for
every declared map/config. The remaining 36 cannot currently be constructed.
Ten referenced maps are absent, and some configs contain missing defenders,
missing wave monsters, malformed spawn tokens, or missing placements.
`f4202` DestroyWorldsHeart06 is partially supported: its scaled configs work,
while its normal config names a missing monster template.

**Content follow-up (2026-10-27):** every unrunnable task was checked against
the client's own `LoadHoldGroundData` parser (0x140898ea8: `;` then `:`
tokens via strtok, only two-part tokens become spawns). Two were resolver
limitations and now run: `h0000-history-of-hiraja-07` (a malformed wave token
is a blank entry, as in the client) and `pw208-hide-and-seek-03` (hosted on
`pw208_lost_woods_dun_04`, which carries Sibyl, the markers and the quest's
spawn area; its ClearRegion is relocated by the same evidence rule, and a
corpus check over 5,316 tasks x 391 maps admits only that case). Result:
54 runnable tasks; 31 remain unrunnable (25 reference maps that exist only in
the legacy client or not at all, 6 have authored typos or missing
templates/markers). Unbuildable `local` defenses have no story fallback; three
such defenses blocked whole chains and are unblocked by explicit content
corrections (`CONTENT_CORRECTIONS` in `gameplay/holdground.py`, user decision):
"Afterglow" IV-V (`ce2025-side-quest-15`/`16`: `hg_marker010` -> `hg_marker10`),
"Search for the Truth" (`h0000-history-of-hiraja-04` to `08`: `hg_merker10` ->
`hg_marker10`), and "Crash Landing" (`h0003-deep-fall-01`/`02`: each `Guardian`
entry draws a random monster from h0003_oasis_dun's regular pool at wave start).
All three chains are tested end to end in `tests/test_holdground_chains.py`.
Difficulty 8 (6 for RailroadBarrier) has no authored
config; it is left as is by decision (probably a planned, unfinished tier).
Tests: `tests/test_holdground_content_coverage.py`.

`gameplay/holdground.py` resolves native `_Template_HoldGround` waves and map
markers before starting anything. `services/quest_holdgrounds.py` owns the
stationary, damageable defender, duplicate-preserving wave spawns, authored
timers/empty-wave pauses, completion after the final surviving attacker, and
cleanup on failure, cancellation, departure, or stale callbacks. Wave monsters
pursue their own defender through the shared combat simulation. Group-enabled
objectives retain eligible participants from the same instance at encounter
start and revalidate each attempt before granting terminal credit.

Current-client binary inspection confirms `DefendCommand` carries a u32 NPC
actor ID followed by the hold-ground string, and NPC interaction type 5 starts
it. The handler validates the live quest/guards, selected active NPC, map, and
eight-metre range. Valid setups remain pending until this interaction; the old
world-story automatic-success fallback remains only for unavailable setups.
Blank Config difficulty is the native wildcard, with exact tiers preferred.
Authored `<Failure/>` now produces native failed state 4; a fresh acceptance
clears the previous attempt's tokens/counters. No invented waves or placements
replace missing content.

All 16 quest status reward rows resolve seven authored effects through the
existing persistent effect system. Their duration, real-time/offline clock,
KeepOnDeath, Replace/Deny, and ResetRunTime rules remain authoritative.
`services/quest_status_rewards.py` stages items, currency, XP, effect changes,
and quest closure together, saves before publication, and completes vitality
and achievement updates even if a send fails. Full inventory/invalid content
leaves the quest ready for retry. Previously closed quests are not paid again.

### 8. P1 — Food recycling

**Implemented from the current client contract.** Client
`_Template_NPC.Id='cooking_npc_stellan'` has `Profession='food_recycling'` and
`ShopId='cooking_currency'`; `_Globals.CookCurrency='money_cc'`.
`export_win32/ui/foodrecyclingwindow.bxml` contains `ButtonSalvage`.

`SalvageCookItemCommand` (`0x0032`) now routes through
`protocol/food_recycling_protocol.py` and `services/food_recycling.py`. Binary
inspection confirms a u32 count followed by item IDs, with no amount or NPC
field. The widget has nine slots and computes each whole-stack payout as
`PriceCC * amount`. Eligibility is positive PriceCC, not the generic Consumable
category: the three currently authored prepared dishes are IngredientItems,
with unit yields 2, 3, and 50. The same authored predicate also includes two
currency templates; the server does not substitute a guessed food-name filter.

The exchange requires an active recycling NPC within the established eight-metre
interaction radius. It validates the whole batch, ownership/storage access,
duplicate IDs, positive amounts and wallet limits before consuming anything.
Inventory and cooking currency are saved before output, and the earned-currency
achievement is committed with its receipt. Response ordering is inventory delta,
GameCurrencyChanged, final allocations, then any deferred achievement rewards.
Spent-input replay and concurrent duplicate submissions cannot regrant currency.

### 9. P2 — Guild/group management and ignored players

**Implemented (2026-10-27).** All eleven management requests are routed
(`SOCIAL_MANAGEMENT_COMMANDS` -> `SocialService._handle_social_management`):
guild kick/rename/message of the day, group kick/edit, join requests and their
leader prompt, multiple invites (limit 10), and candidate lists. Every
server->client layout was checked against the client decoders. Kicks require a
strictly higher rank (client rule at 0x1402762a8); guild rename charges the
authored `_Globals` GuildEditName RC/VC fee, and guild foundation now charges
the authored GuildFoundation fee shown by the same dialog (it was free before).
Persisted ignores (IgnoredList add/remove/refresh, IgnoredStatus at login and
inside GuildStatus) block whispers and channel lines for the ignoring player,
and group/guild invitations, join requests, buddy adds and trade requests in
either direction. Invitation answers now require a ledger entry issued by the
server (one use, 300 s, token checked); this closed two forgery paths — the
invitation response and the notification echo in `services/commerce.py`,
which had added members straight from client JSON. Tests:
`tests/test_social_management_*.py`. Not implemented: guild promote/demote,
group leader `/promote`, the legacy 2190 route, and the group finder's empty
leader name/level fields. The original findings follow.

**Partial implementation.** Creation, invitations/acceptance, leave, lists,
basic group finder, buddies, chat membership, and persistence exist.

Missing routed operations include guild kick, guild rename, guild message of
the day, group kick/edit, join-request/candidate management, and multiple
invites. `services/social.py:480` only implements create/leave/list for guild
management; `:541` does the equivalent for groups. Other invite handlers do
not supply these missing management operations.

`IgnoredListCommand` also has no handler or persisted ignored-user set.
`protocol/social_protocol.py:775` encodes an empty ignored list, and its decoder
at `:828` rejects a nonempty one.

Implement each operation with membership/leader authorization, persistence,
result responses, and broadcasts. Persisted ignores must also be consulted by
chat and invitation delivery, rather than only appearing in the social UI.

### 10. P2 — Leaderboards beyond career honor

**Implemented (2026-10-27).** Every `Ranking::*` query and the Fractured Planes
high-score queries (`0x16D`/`0x172`) return real data: daily/weekly honor
(baselines per UTC period), PvP season and per-mode boards, nine Parallel World
boards (solo/group best run from first map to the authored end boss), PvE
overall (`_Template_LeaderBoardOverallranking` points), and Fractured Planes
highest floor plus remaining time. `persistence/ranking_state.py`
(`ranking.sqlite3` beside the players DB) stores scores, period history,
settlement receipts and entitlements. A 30 s tick settles expired
`pvp_seasons_event`, `pve_season_event` and `event_fracturedplanes_rank_start`
seasons exactly once from the three authored reward tables, delivering online
or at next login and keeping full-inventory rewards pending; Fractured Planes
settlement feeds `highscoreranking`. Parallel World runs score mainly by
speed (authored par times and `_Globals.Gauntlet*` points), as retail did per
player report: each completed map is recorded as it completes, so a run that
ends early keeps a lower score, and finishing all maps plus the end boss
always outranks any unfinished run. The absolute scale is a local choice (the
retail formula is not in the client), so league thresholds and league rewards
are not granted. Not implemented: rank-change notices
(`0xEE`/`0xFB`) and the retail Gauntlet run protocol. Details:
`docs/LEADERBOARDS.md`. The original findings follow.

**Partial implementation.** Career honor, offline/live profiles, pagination,
class and guild filters exist. `services/ranking.py:128` populates only board
4; the other supported board selectors return empty/unranked results.

**Friends filtering implemented:** `services/ranking.py` reads the requester's
persisted buddy list, includes self and offline friends, and ignores references
without a ranking record. Friends/guild filters form a deduplicated union;
the all filter retains precedence. Relationship changes apply on the next query
even while score records are cached. Class filtering, score ordering, and ranks
apply to the filtered board. The original isolated probe returned only self
despite a saved buddy; the new service tests reproduce and cover that case.

Only four honor request types are routed
(`protocol/ranking_protocol.py:30`). Parallel-world requests `0x00F6`/`0x00F8`
and high-score requests `0x016D`/`0x0172` are absent. The current database ships
20 `_Template_HighScoreRankRewards`, six `_Template_OverallPVPRankingRewards`,
and 12 `_Template_OverallRankingRewards` rows; no runtime settlement consumes
those tables.

Remaining work is to record the required board histories, implement PvE and
high-score queries, and add scheduled, idempotent ranking settlement.
Tower floor progress, score display, buffs, and first-clear rewards already
exist in `services/map_events.py:1405` through `:1567`; those are separate from
competitive leaderboard rewards.

### 11. P2 — Newer activity-specific lifecycles

**Implemented (2026-10-27)** in `services/seasonal_activities.py`,
`protocol/seasonal_activity_protocol.py` and `persistence/seasonal_state.py`:
egg incubation (start consumes the Growth item, authored feed cost, hatch roll
on a 10000 scale), wishing well and selective chests (opened client-side from
item categories 0x3E/0x40), anniversary cake milestones (server-wide total of
crafted `anniversary_cook_cake_01` during `anniversary_event`, per-character
claims), the 2024 and 2026 comeback calendars, and the 2026 recall link
(returner = previous save older than 30 days during the 2026 event; local
rule from the activation-code text). Claims are idempotent and saved before
publication. Persisted incubations are sent in a separate batch after
PlayerReady; `UserHatchEggsInfo` stays out of the login batch until a real
1024 login confirms it. Cakes and comeback stay closed until their events are
scheduled. Not implemented: recharge/double rewards, the recall widget's daily
tasks, payment sign-in `0x153`–`0x155`. The original findings follow.

These are backed by current UI and authored database content, with no matching
gameplay handlers. The dispatcher falls through to its unimplemented-command
path at `services/dispatch.py:670`. Normal daily login/activity sign-in,
general event scheduling, and ordinary reward delivery already exist.

| Activity | Current client evidence | Missing server workflow |
| --- | --- | --- |
| Egg incubation | Two `_Template_HatchEggs` rows; `ui/growthwindow.bxml`; `OperateHatchEggsCommand` `0x015F` | Persisted incubation/feed/hatch state, timers, costs, outcomes. Bootstrap uses empty `UserHatchEggsInfo` at `protocol/player_init_protocol.py:2676`; existing hatch reward lookup is not the lifecycle. |
| Wishing well / magic lamp | 15 `_Template_WishingWell` rows; three `WishingWell` item templates; `ui/giftofthelampgod.bxml`; `SelectWishingWellMsgCommand` `0x0160` | Selection validation, consumption, reward delivery, persisted claim state. Generic chest categories omit `WishingWell`. |
| Anniversary cake milestones | Five `_Template_CraftingCakes` rows; `ui/cakeinfo.bxml`; info/claim requests `0x0150`/`0x0152` | Milestone accounting and idempotent claims. Ordinary crafting and anniversary reward containers are already supported. |
| Returnee sign-in and 2026 recall | Two 14-row comeback calendars; `ui/camebacksignin.bxml`, `recalledsignin.bxml`, `exclusiveactivationcode.bxml`; original `0x0158`/`0x015A`, 2026 `0x015B`/`0x015D`, user/bind/reward `0x0179`–`0x017C` | Eligibility, calendar/points state, binding rules, claims. Exact inactivity/eligibility policy may require server design or retail evidence. |

Choose which activities to enable before implementing their complete lifecycle;
the presence of retained assets does not mean every activity must be active.

### 12. P2 — Additional combat/content semantics

**Implemented (2026-10-27).** Finite-LifeTime monster summons expire through
`MonsterSummonLifecycle` (DiscardMonster to every viewer, no kill/XP/loot/quest
credit, retired with their caster, never revived for late joiners); this also
applies to Gorga's snakes, Arachna's spiders, Grimmag's twister, the Herald's
fire minion and Materi whirls, which were permanent before. Set modifier kinds
are now compared case-insensitively (none of the new rules fired before).
QuestDropFactor doubles side-quest drops deterministically (Dragan sets; the
"chance" text belongs to the Parrot pet's `event_quest_drop` C:0.2).
HealthGlobeBonus adds percentage points to the authored 20% sphere heal and
restores the skill-resource fraction (now published to the client).
`AdditionalSkillCost:-1.0` lowers essence use by one (minimum 1) and
`IgnoreSkillCost:0.15` is a 15% free cast, rolled once after acceptance. The
five SummonMonster* talent modifiers are implemented, which enables Bond of
Friends (one big wolf) and the CNY tier-3 Nian, and Dwarf "Double Action".
Tests: `tests/test_set_status_modifiers.py`, `tests/test_monster_summon_lifetime.py`,
`tests/test_summon_another_monster.py`. The original findings follow.

- **Temporary monster status minions:**
  `gameplay/local_combat_state.py:4106` skips any status-summoned monster with
  nonzero `LifeTime`. Khalys bullet portals (about 3.9 seconds) and illusions
  (24 seconds) are explicit examples. Add expiration, ownership, despawn, and
  cleanup; existing permanent summons and other Khalys mechanics work.
- **Set upgrades:** the original 16 skipped `modAE` rules now feed active-tier
  actor-rule evaluation, including live status overlays (finding 6). Complex
  minion-targeted changes still need the downstream behavior described there.
- **Modifier semantics:** no runtime branch was found for current set
  `QuestDropFactor` (16 rules), `HealthGlobeBonus` (4), `IgnoreSkillCost` (4),
  or `SummonAnotherMonsterAmount` (1; CNY2026 ranger extra wolf).
- **Essence-set cost reduction:** four rules use one-argument
  `AdditionalSkillCost:-1.0`; the parser path at
  `gameplay/local_combat_state.py:6122` requires at least three arguments and
  discards them. Existing scoped resource-cost modification is separate.

### 13. P1 — Live global-event activation leaves NPC/exit state incomplete

User-reported symptoms: event map states change, but some NPCs require a rejoin
to appear; some event entrances remain invisible, including a Sargon entrance
from Kingscity. Code inspection and isolated probes confirmed a missing world
reconciliation step. **Implemented in the follow-up:** global schedule changes
now reconcile occupied map runtimes and refresh NPCs, props, monsters, and exits.
The exact rendering behavior still needs a live-client check. The diagnosis
below records the behavior before this fix.

**Shared update-path gap.** CMS changes and schedule boundaries both reach
`EventAdministration._publish` (`services/admin_events.py:309`) and
`refresh_session_events` (`services/admin_event_refresh.py:23`). This publishes
event flags, status effects, progress, participation, notifications, and season
pass changes. It does not update the shared map runtime's global flags/enabled
event sets, refresh NPC state, publish exits, or rebuild
`session.gameplay_map_exits`.

Those world updates exist in a separate map-local transition path:
`services/map_events.py:4318` calls NPC/prop/monster publication and
`_publish_map_exits`. Global activation does not invoke an equivalent path.
`MapEventRuntime.active_flags()` remains its initial flags plus running local
events (`gameplay/map_event_state.py:1462`, `:1480`). Attaching an existing
shared runtime also does not replace its initial flags
(`services/map_events.py:1606`).

An isolated test used the real event catalog and `refresh_session_events`
with a temporary schedule, activating the anniversary event while already in
Kingscity. Refresh returned success and sent event/state packets, but the
runtime did not acquire the anniversary flag, its enabled sets did not change,
and expected event-set rows 38 and 137 remained absent. Six newly eligible NPC
templates included Phestos and Grima. No NPC, prop, or exit refresh was emitted.

The reverse probe entered while anniversary was active, then expired the
temporary schedule. The catalog became inactive but the runtime retained the
anniversary flag and Phestos remained server-eligible. NPC eligibility unions
fresh schedule flags with the stale runtime flags at
`services/inventory_interactions.py:293`, so reconciliation must remove ended
global flags as well as add newly active ones.

**NPC registration nuance.** Production initialization deliberately announces
inactive-event NPCs too (`services/player_init_world.py:85`): Kingscity has
119 announced NPC actors with Sargon either off or on. Therefore, this should
not be described simply as "the server never sent NewNPC." A newly visible
scene entity may need its network/interaction state refreshed after the
event transition. Blindly resending all `NewNPC` commands risks duplicate actor
registration; `_spawn_event_npcs` already tracks announced IDs and refreshes
NPCInfo (`services/map_events.py:4692`). A live test must establish which actor
or scene attachment is absent for the reported NPCs.

**Exact Sargon exit found in current data:**

| Field | Value |
| --- | --- |
| Map/table/row | `a0200_kingscity.db4`, `_Instance_InteractExit`, rowid 16 |
| Exit ID | `a0200_kingscity_exit_to_g0001_beach_01_night_dun_00` |
| Destination | `g0001_beach_01_night_dun_00` |
| GUID bytes | `1adc941aa67344ac8cae045a69f5d468` |
| Position | `(4.475175, 1, -98.488586)` |
| EventSetRow | `99` |
| Visibility declaration | `_Instance_EventSets` rowid 100: `sargon_event=True;` |
| Authored exit state | `active` |
| Nearby same-event NPCs | `e_sar_quest_hardy`, `e_sar_merchant_jon` |

Production-style initialization (`include_inactive_event_sets=True`) emits
this exit with `ExitInfo.field_10=0` while Sargon is off, and `=1` while it is
on. The active-only authoritative exit list similarly contains zero versus
one matching entry. Difficulty/access filtering retains the enabled exit.
Live global activation sends no replacement ExitInfo or session exit list,
leaving the previously closed snapshot in place. Rejoining rebuilds that
snapshot with the new event flags. This establishes a concrete stale-state
path; client rendering was not exercised.

The `_Template_ExitList` copy has empty `EventFlags`, so its visibility cannot
be inferred from that table alone; the map's event-set declaration is needed.
Kingscity is not in the explicit native-exit VariationInfo target list
(`gameplay/local_map_state.py:1248`). Do not assume that list needs extending
until a correctly ordered event/ExitInfo refresh is tested in the client.

**Free-entry discrepancy:** the current `_Template_MapAccessGroup` row 1570
for this destination requires `e_sar_key_coffee_beans:10`. I found no separate
free Sargon entrance in Kingscity's current exit rows. The user's free farming
route may refer to a different entry or intended custom behavior; its identity
remains unresolved. Event visibility and ticket payment are separate, and the
visibility fix should not silently change this authored cost.

**Implemented behavior:** `services/global_event_refresh.py` runs a second
publication phase after the players receive their global EventUpdate snapshots.
`MapEventRuntime.replace_global_flags` updates shared event sets while preserving
local events, pending timers, quest/access flags, and suppressed sets. Existing
NPC actors receive refreshed NPCInfo without duplicate registration; expired
event NPCs are disabled. ExitInfo and the authoritative travel list are updated
together, including explicit closure of previously visible exits.

Mutation gates protect map membership during publication, and event transitions
are serialized against map-local start/stop callbacks. Deferred or failed
refreshes are retried by the schedule watcher without another schedule edit.
NPC/exit state can be replayed for each viewer independently. Tests in
`tests/test_global_event_world_refresh.py` cover repeated Sargon and anniversary
toggles with two players, scheduled start/expiry, departing viewers, delivery
failure on activation/deactivation, late joins, and preservation of unrelated
map state. Live rendering remains an integration check; the authored Sargon
entry cost is unchanged. Prop/monster cleanup retains the existing broadcast
helpers; their partial-send recovery is not covered by the NPC/exit retry fix.

## Investigate before calling these required features

- **Gauntlet run lifecycle:** commands for start, abort, result, and conversion
  remain in the client, alongside score globals. The server only responds to
  Gauntlet notifications (`services/inventory_interactions.py:100`). Establish
  whether the run UI is still reachable before prioritizing a new subsystem.
- **Legacy direct gameplay route:** `services/login_game.py:305` duplicates
  only part of the main dispatcher and omits many newer features. Determine
  whether direct gameplay through 2190 is still an intended supported mode.
  If so, share dispatch instead of maintaining divergent lists. This does not
  imply the normal map-service route lacks those features.
- **Other retained commands:** currency conversion, glyph removal, vouchers,
  payment/promotional calendars, and spectator commands need a current UI or
  capture-backed use case. A registered class alone is insufficient evidence.
- **`niwalk` skill rows:** 26 rows exist in current data, but that does not
  establish a currently supported fifth playable class.
- **Retail economy parity:** exact random loot probabilities and some event,
  commerce, and reward policies may be server-only data. Explicitly distinguish
  chosen local rules from recovered retail behavior.

## Existing implementation that should be retained

Do not restart these systems from scratch: accounts/character creation and
SQLite persistence; movement/maps/navigation; combat/resources/essences;
inventory/bank/equipment; shops, direct crafting and enchantment transfer;
quests and many map events; event scheduling/progress/rewards/travel;
daily challenges; season pass; Wanted Thief; collection/cosmetic operations;
basic social/chat/trading; duel PvP; career honor; tower floor progression;
admin CMS and live delivery. Each has runtime code and relevant regression
coverage, although that does not establish full retail parity.

Older audit documents mix useful evidence with superseded conclusions.
Current code does apply `addLE` skill geometry, spends player resources,
refreshes combat vitals after equipment changes, and sends actor stat updates.
The comments saying health globes/block/resource events do not exist are stale;
the missing integration was specifically in the item-set evaluator. The old
friend-persistence statement in `docs/LEADERBOARDS.md` has been corrected.

## Verification performed and limits

- Read-only binary hashes, SQLite schema/content queries, command/dispatch
  comparisons, current runtime parsers, and client UI string inspection.
- Full map-database hash comparison and in-memory structural comparison of
  decoded current-client quests against the server's XML.
- Isolated probes reproduced the FFA team issue, multiplayer round-end issue,
  and friends-filter issue without touching live account/player databases.
- Focused tests:
  `py -3.12-64 -B -m pytest -p no:cacheprovider tests/test_ranking.py tests/test_pvp_matchmaking.py tests/test_quests.py tests/test_group_popup.py -q`
  produced **110 passed, 3 failed**.
- The failures are existing test-contract/content mismatches: one references
  removed `MatchPlayer.team_slot`; one expects cooldown-reset ID 5929 where
  current content uses wire ID 6254; another treats ID 5894 as a cleanseable
  PvP debuff although current content maps it to a jack-in-the-box aura.
  These failures do not by themselves prove live cleanse/reset defects.
- `tests/test_achievements.py` and `tests/test_client_build_defaults.py` produced
  **13 passed**. These are focused results, not a full-suite pass.
- `conftest.py:9` defaults tests to builtin patch5 IDs and marker bits 0.
  Passing these tests is not proof of every release261_1024 serializer or UI
  flow. Add current-build wire/interaction checks as each gap is implemented.
- Event-visibility follow-up: isolated anniversary refresh and Sargon exit
  initialization probes reproduced the state mismatch above. Running
  `tests/test_map_local_event_updates.py` and
  `tests/test_event_waves_and_vendors.py` produced **14 passed, 10 failed**.
  All ten failures expect anniversary wave-start messages at
  `tests/test_event_waves_and_vendors.py:57`; their emitter is currently
  commented out at `services/map_events.py:4290`. They are separate from the
  reported global-event NPC/exit refresh gap. This was the pre-fix audit run.
- Implementation follow-up: all **14** cases in
  `tests/test_global_event_world_refresh.py` passed with
  `DSO_COMMAND_IDS=docs/command_ids_rel261_1024.json`. This covers the current
  client's event/NPC/exit command IDs, without claiming a live rendering test.
- Existing map-local event update and event travel suites passed **22** cases
  in a combined run with the first 11 new regressions (**33 passed**).
- The final CMS event suite produced **34 passed, 2 failed**. Both failures
  occur when their fixtures invoke `_toggle_admin_speed`: the static data
  lacks the custom admin buff (`admin buff data is missing; install it on the
  server and client`). They occur before the event refresh under test.
- Python syntax checks passed for all five implementation/test files.
- Priority-batch follow-up: **115 quest tests passed**, including 21 new
  content/migration cases. The real Fractured Planes introduction was offered,
  accepted, progressed, turned in, saved, and restored; replaying turn-in did
  not grant another reward. The imported XML matches the current client's
  1,768 quests structurally, ignoring formatting.
- The final combined combat/PvP/status/persistence/concurrency selection passed
  **179 tests**. This includes all six cast ingress variants, shared cooldowns,
  modifiers and resets, paid/unpaid exception boundaries, aura ticks/cleanup,
  reconnect continuity, multiplayer elimination, FFA scoring, and duplicate
  round-completion callbacks. Related old test fixtures now resolve skill and
  effect templates by name instead of obsolete database row numbers.
- All **66 new priority-batch regressions** also passed with
  `DSO_COMMAND_IDS=docs/command_ids_rel261_1024.json` and
  `DSO_SKILL_HIT_MARKER_BITS=1`, the supplied client's protocol configuration.
  Syntax validation passed for 22 implementation/regression files.
- These are targeted results, not a full-suite pass. The older
  `tests/test_ranger_skill_timing.py` suite still uses stale skill IDs (for
  example its Scatter Shot ID now names Arcane Charge); its broader run failed
  on those content assumptions. This batch does not rewrite that suite or
  claim live-client animation/rendering verification.
- Progression-batch final integration: **283 tests passed** across achievements,
  recipe/rune use, crafting, inventory, premium bags, persistence, session locks,
  PvP, and reward chests. The dismantling coverage test resolves every one of
  the 2,574 authored table results. The real tenth-craft path grants one recipe
  book, saves its receipt, and does not regrant it on replay/relog. Tests also
  cover pending full-bag rewards, class-selected items, cross-character counters
  and rewards, character deletion, legacy migration, blocked autosaves during
  reward staging, failures before save and after queueing, and packet ordering.
- All **80 new progression regressions** passed with
  `DSO_COMMAND_IDS=docs/command_ids_rel261_1024.json` and
  `DSO_SKILL_HIT_MARKER_BITS=1`. These cover the current protocol contract;
  live workbench, achievement-popup, recipe-book, and rune-slot UI checks remain.
- The social persistence failure test originally inspected temporary-file
  cleanup while its asynchronous writer was still retrying. Its assertion now
  runs after shutdown drains the writer, and the suite passes. A separate
  Season Pass CMS test, `test_reward_edit_is_granted_shown_and_audited`, still
  expects one changed row where current data returns two, before its gameplay
  path runs; this batch does not change that CMS behavior.
- Food/set batch integration: **359 tests passed** across the new features,
  combat/status delivery, resource spending, pickups, persistence, session
  locks, achievements, crafting, and PvP. Coverage includes the real Mortis
  stack/terminal programs, Temple guardian enablement, CNY's twentieth-stack
  unlock, counted-effect save/rejoin, stale map callbacks, full-batch food
  validation, spent-input replay, and accepted state surviving failed sends.
  The final child-scheduling generation guard passed all **40 set program/event
  tests** again. Syntax checks passed for 23 implementation/regression files.
- All **77 new food/set regressions** passed with
  `DSO_COMMAND_IDS=docs/command_ids_rel261_1024.json` and
  `DSO_SKILL_HIT_MARKER_BITS=1`. Live recycling-window, set-effect visuals, and
  event NPC/entrance rendering still require a running client check.
- An additional login/travel/persistence selection passed 52 cases with four
  existing fixture failures: two safe-login tests expect the tutorial hub
  instead of the current tutorial-dungeon repair, and two event-travel tests
  expect three datagrams instead of the current two. All four reproduced with
  the three new set-persistence hooks disabled. They are outside this batch;
  these targeted results do not claim a full-suite pass.
- Quest-defense/reward batch: **428 distinct targeted cases passed** across
  final selections of 288 quest/reward/persistence cases, 103 combat/simulation
  cases, and 40 defense cases (three defender-combat cases overlap). Tests cover
  the real five-wave/37-attacker RewriteHistory encounter, duplicate spawns,
  final-survivor gating, real defender damage, owner/defender death, native
  failure/reacceptance, NPC refresh guards, map departure, stale/duplicate
  timers, participant selection, late joiners, busy peer locks, and earned group
  results surviving owner departure. Packet/save failures retain durable quest
  results and do not grant a second reward.
- All **72 new defense/reward regressions** passed with the current client's
  `DSO_COMMAND_IDS=docs/command_ids_rel261_1024.json` and
  `DSO_SKILL_HIT_MARKER_BITS=1`. Every one of the 16 authored status rewards was
  exercised through turn-in and decoded effect output. Syntax validation passed
  for 21 implementation/test files. An additional daily-challenge run found an
  existing catalog-count fixture mismatch (1,480 expected; 1,499 current rows).
  This is targeted validation, not a full-suite pass. Exact retail wave timing,
  defense NPC visual handoff, and reward UI still need live-client verification.
- No live player database or event schedule was edited and no server restart
  was performed. The additive account-achievement table is created by the
  normal persistence initialization when this code is started.

## Suggested implementation order

1. Synchronize current quest definitions with saved-progress compatibility.
   **Implemented.**
2. Reconcile live global-event changes with NPCs, props, and exits, including
   Sargon's Kingscity entrance and repeated activation/deactivation.
   **Implemented; live-client verification remains.**
3. Enforce player cooldowns and fix PvP elimination/FFA correctness.
   **Implemented. Objective modes and richer matchmaking remain.**
4. Complete ordinary progression: achievement hooks/rewards, earned recipe
   unlocks, missing crafting outputs, and food recycling.
   **Achievement/recipe/dismantling/rune/food batch implemented. The explicitly
   listed achievement sources remain.**
5. Connect item-set events and instant programs to existing combat systems.
   **Event hooks and authored persistent/timed program lifecycles implemented;
   complex downstream minion behavior remains.**
6. Replace quest-defense shortcuts and finish skipped quest rewards.
   **Runtime and rewards implemented. Missing defense content remains; valid
   setups no longer use the story shortcut.**
7. Complete social management, friends rankings, and objective-based PvP.
   **Implemented 2026-10-27.**
8. Add PvE/high-score history and ranking reward settlement.
   **Implemented 2026-10-27.**
9. Implement selected seasonal activity lifecycles and remaining boss/set
   semantics, with client captures where rules are not fully recoverable.
   **Implemented 2026-10-27 (all four activities, temporary summons, set
   modifiers, Black Dwarf minions); live-client checks remain.**

This order fixes behavior in already-exposed gameplay before expanding the
number of supported activity screens. The audit itself was read-only; the
subsequent implementation addresses items 1–4, the event/program portion of
item 5, and the runtime/reward portion of item 6 above. The 2026-10-27
follow-up completes items 5–9 as noted in each section. What remains is
content that does not exist in the current data (34 defense tasks, some
authored achievement targets), the deferred skin-catalog enforcement and the
open items listed in sections 4, 7 and 11, and live-client verification of
everything added since this audit.
