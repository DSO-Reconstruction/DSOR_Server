# Leaderboards

The leaderboard window (`ui/leaderboards.bxml`, categories from
`_Template_LeaderBoardCategory`) has four trees. Every selector it can request
is answered by `services/ranking.py` from server-measured results:

| Tree / category | Client request -> response | Board and score |
| --- | --- | --- |
| PvP: Overall Rank | `0x00f0`/`0x00f4` -> `0x00f1`/`0x00f5` | Honor gained in the running PvP season; career honor when no season runs |
| PvP: Solo PvP, Team PvP | `0x00eb`/`0x00f2` board 0/1 -> `0x00ec`/`0x00f3` | Honor, wins, losses, draws, kills, deaths of settled matches in solo (1v1, 8 FFA) or team modes |
| PvP: Daily/Weekly Honor Points | board 2/3 | Career counters minus those at 00:00 UTC / Monday 00:00 UTC |
| PvP: Hall of Honor | board 4 | Career honor, wins, losses, kills, deaths |
| Parallel Worlds: Overall Rank | `0x00fc`/`0x00fe` -> `0x00fd`/`0x00ff` | Sum of `_Template_LeaderBoardOverallranking` points for each board place |
| Parallel Worlds: Grimmag .. Gorga | `0x00f6`/`0x00f8` -> `0x00f7`/`0x00f9` | Best recorded run per `_Template_ParallelWorldStructure` board, solo or group |
| Fractured Planes: Group / Ladder Ranking, Ancestral Ruins: Challenge Ranking | `GetHighScoreRankGroupData` / `GetHighScoreRankAllData` (`0x0172`/`0x016d` -> `0x0173`/`0x016e` in release261_1024) | Highest cleared floor, then run time remaining at that clear |

Command numbers resolve by class name through `DSO_COMMAND_IDS`. The
category-to-request mapping is the client's own: `0x14032c1ec` dispatches on the
selected category, `0x14031be18` maps PvP categories to honor boards
(Overall = overall commands, Solo 0, Team 1, Daily 2, Weekly 3, All Time 4),
`0x14031be60` maps PvE categories 2..10 to `PWLeaderBoardId` 0..8 and the
Overall entry to the overall commands, `0x14031bfb0` selects the solo/group
type, and Fractured Planes category 1 (Group) sends the group query while the
Ladder and Event "Challenge Ranking" tabs send the class-filtered all-data
query. The class selector is 0..3 for one class and 4 for all classes.

## Common list behaviour

Lists contain existing player profiles, including offline characters; live
sessions overlay pending saves. Equal scores are ordered by character ID.
Friends filtering uses the requester's persisted buddy list and includes the
requester; guild filtering uses actual membership; combined friends/guild
filters form a deduplicated union and the all filter takes precedence.
Relationship changes take effect on the next query even while score records
are cached for one second. Pages contain at most ten rows; the player page flag
centers the page on the requester and explicit offsets drive the
first/previous/next/last buttons. List ranks are zero-based, local-player
ranks one-based, `0xffffffff` means unranked.

Requests are read-only. `HonorRankingMatchUpdate`, `HonorRankingUpdate` and
`ParallelWorldUserRankingUpdate` have no client call sites (they were ranking
server inputs); they are routed to the ranking handler only to be rejected.

## Where scores come from

* **Honor**: career honor and match results persist in the player document
  (`honor_points`, `pvp_matches_won/lost`, `pvp_kills/deaths`). Daily, weekly
  and season boards subtract per-character baselines recorded when the period
  opened (`ranking_baselines`); characters created later start from zero, and
  only characters with activity in the period are listed.
* **Solo/Team PvP**: `BattlegroundService._finish_match` calls
  `_ranking_on_pvp_match_settled` once per participant inside its settlement
  claim; results accumulate per mode for all time and for the running season.
* **Parallel Worlds**: runs are tracked per character on the service (map
  transfers arrive on new sessions) from the authored structure maps:
  - entering the structure's first map starts a run (ending any other);
  - entering the next map in order completes the current map; its time is
    added and the partial result is recorded at once;
  - the `ParallelWorldEndTrigger` boss dying on the last map completes the
    run for every player in that instance;
  - any other arrival ends the run: a town or hub (leaving, respawning in
    town), another structure, or the run's current map again (only a relog
    or reconnect re-initializes it). Its result is whatever its completed
    maps already recorded. Arriving mid-structure starts no run, and kills
    count only on the run's current map.
  Every completed map is recorded when it completes, so a run cut short by
  a logout or a server restart keeps exactly its completed maps and never
  leaves an entry for an unfinished map. A run seen with another player in
  the instance goes to the group board. Each board keeps the best total per
  character and period (the shorter time breaks ties); a later map or the
  finished chain of the same attempt always scores more and replaces it.
* **Fractured Planes**: `MapEventService._complete_fractured_planes_floor`
  records the cleared floor and the seconds left on the run deadline the
  floors share; each character keeps the highest floor, then the most time
  left.

### Parallel World scoring (local formula)

Retail scored by how fast the chain of maps and the end boss were finished;
ending early still scored, far less than finishing. `GauntletManager::OnActivate`
(`0x140a31cc4`) loads the authored inputs — `GauntletTimePoints` (1200),
`GauntletMonsterKillPoints` (2400), `GauntletBossKillPoints` (480),
`GauntletCompletionBonus` (0.15, "+15% bonus points" for completing) and
`_Template_DifficultyFactors.GauntletScore` (2.0..15.0) — and each structure
authors its ordered maps (N = 3), `MonsterRequirement` and `DiffNTime`, the
par minutes for the whole chain at difficulty N. The client only shows them
as percentages; the combination below is a local choice
(`gameplay/leaderboards.py`):

```
F            = GauntletScore(difficulty) * 100        (points per percent)
speed(par,t) = min(1, par / t)                         ("Time %")
map k        = F * GauntletTimePoints * speed(par / N, seconds on map k)
monsters     = F * GauntletMonsterKillPoints * min(1, kills / MonsterRequirement)
               * completed / N * speed(par * completed / N, seconds so far)
boss         = F * GauntletBossKillPoints                          (finished only)
bonus        = (maps + monsters + boss) * GauntletCompletionBonus  (finished only)
finish       = F * (N * GauntletTimePoints + GauntletMonsterKillPoints)
               * (1 + speed(par, whole run seconds))               (finished only)
total        = maps + monsters + boss + bonus + finish
```

A map's seconds run from entering it to entering the next map (the last map
to the boss kill). The finish component alone exceeds the most an
unfinished run can reach (N - 1 maps of speed points and (N - 1)/N of the
monster points), so any finished run outranks every partial one; speed
scales every component except that floor, so below par a faster run gains
more than one that only kills more. Example, Grimmag at difficulty 5 (par
5 minutes): one map in 200 s 337,500; two maps 810,000; the best possible
partial 1,800,000; a finished run at 2,000 s per map 3,176,550, at 200 s
per map 5,850,900, at par 8,753,400.

The four row statistics are total, time (map speed points + finish), monster
and boss points (row reader `0x140aa7148`, table `0x1403207b4`); the local
summary is total score, run time in seconds through the last completed map
(`%02u:%02u:%02u`), monster kills and boss kills (`0x140318ee4`).

## Periods, history and settlement

Persistent state lives in `ranking.sqlite3` beside the player database
(`persistence/ranking_state.py`): scores per board and period, honor
baselines, periods, frozen history and settlement receipts/entitlements.

| Period family | Bounds | Settlement |
| --- | --- | --- |
| `honor.daily`, `honor.weekly` | UTC day, ISO week | History snapshot only |
| `pvp.season` | Scheduled `pvp_seasons_event` instance | `_Template_OverallPVPRankingRewards` by season-honor place |
| `pve.season` | Scheduled `pve_season_event` instance | `_Template_OverallRankingRewards` by PvE overall place (`BoardType` 0 solo, 1 group) |
| `fp.season` | Scheduled `_Globals.HighScoreRankingEventId` instance (`event_fracturedplanes_rank_start`) | `_Template_HighScoreRankRewards` by group place, plus the `highscoreranking` achievement event |

While a season event is active its boards show that season; otherwise they
show all-time results (which are always recorded alongside). Both season
templates carry `EventScheduleCategory` "LEADERBOARD SEASON" and advertise
exactly the boxes of the table settled for them.

A background tick (every 30 s once the character service is serving) opens
current periods, settles every open period whose day/week ended or whose
event instance is now `expired`, and retries pending deliveries for online
characters. A settlement writes the history rows of every board in the
family, one entitlement per rewarded character and the receipt in one SQLite
transaction; a replay is inert, an interrupted transaction leaves nothing and
is redone from the same frozen scores. A schedule entry deleted before its
end is not settled.

Delivery uses the character's claim ledger (`timed_offer_purchases`):
`ranking-reward:<board>:<period>` is written with the granted inventory by the
normal reward path (inventory update and item-received messages), the player
document is flushed, then the entitlement is marked delivered. Online
characters receive rewards at the tick; offline characters on their next map
entry; a full inventory keeps the entitlement pending (reason kept on the
receipt) and it is retried on later ticks and logins. Fractured Planes places
also feed `_achievement_feed(session, "highscoreranking", (place,))` exactly
once per character and period, guarded by a
`ranking-achievement:<board>:<period>` marker saved with the progress
(`fracturedplanes_rank3`: finish in the top three five times). Players with
no cleared floor are not ranked ("Players with a Highest Passed Floor of 0
cannot receive Group Ranking rewards").

## Verified wire layout

Honor layouts were reconstructed against `dro_client64-patch5.exe` (release261)
and remain identical in release261_1024:

- `0x140a136d8`: overall list request = start u32, count u32, context u32,
  filter u16, class u8. `ffffffff` with filter `0x10` asks for the player page.
- `0x140a1377c`: overall response = total u32, context u32, status u8,
  row count u32, rows (UTF-8 string, score u32, zero-based rank u32),
  page start u32, filter u16, class u8. Status 0 is used for successful reads.
- `0x140a636ec`: detailed rows = string, zero-based rank u32, six u32
  statistics (honor, wins, losses, draws, kills, deaths).
- `0x14031897c` and `0x140318ca8`: total and start feed the page controls;
  filters `0x20`, `0x40`, `0x80` carry explicit start offsets. `0x10` selects
  the local-player page. Low bits 1/2/4 select all/friends/guild.
- `0x140312d18`: list rank is displayed after adding one.
  `0x1403146dc` uses one-based local-player rank; `ffffffff` means unranked.
- `0x140a134e4`: detailed local response has eight u32 fields, a board u8,
  and one boolean bit. Its actor trailer is therefore not byte aligned.

Parallel World, overall PvE and high-score layouts (release261_1024 decoders):

- `ParallelWorldUserRankingInfo` `0x140a567d4`: i32 start, i32 count, u32
  context, u16 filter, enum8 board (-1..8), i8 class, i8 sort (rank score,
  time, monster, boss), enum8 type (-1..1).
- `ParallelWorldUserRankingResponse` `0x140a568c8`: i32 total, u32 context,
  i8 status, rows (string, i32 rank, 4 x u32), i32 start, u16 filter, enum8
  board, i8 class, i8 sort, enum8 type.
- `ParallelWorldLocalUserRankingInfo` `0x140a56660`: u32 context, enum8 board,
  enum8 type; response `0x140a566d4`: u32 context, score, best time, monster
  kills, boss kills (u32 each), enum8 board, enum8 type, i8 status.
- `OverallUserRankingInfo` `0x140a564a4`: i32 start, i32 count, u32 context,
  u16 filter, i8 class, enum8 type; response `0x140a56560`: i32 total, u32
  context, i8 status, rows (string, u32 score, i32 rank), i32 start, u16
  filter, i8 class, enum8 type.
- `OverallLocalUserRankingInfo` `0x140a563b4`: u32 context, enum8 type;
  response `0x140a56410`: u32 context, u32 rank, enum8 type, i8 status.
- `GetHighScoreRankAllData` `0x14097a8f8`: i32 class filter; result
  `0x14097a938`: i32 class filter (the client caches the list under it),
  u32[] highest floors, u32[] remaining seconds, string[] names.
- `GetHighScoreRankGroupData` `0x14097a9cc`: no body; result `0x14097a9e0`:
  the same three arrays. Element `i` becomes row `i + 1`
  (`0x1403180d4`/`0x140318314`); the client pages the whole list itself, so
  the server sends at most 999 entries (the last authored reward place).

Every server reply uses application packet `0x84` and appends actor ID 1 as
additional server data. Client requests carry no actor. Enumerated fields are
validated against the client's `-1..N` readers before a reply is built.

## Position/frame telemetry

The accompanying `0x00e9` is
`Commands::ClientCurrentPositionFrameInfoTrackingCommand`, not a ranking
request. The generated decoder at `0x140970a08` reads a character ID, one
double, five uint32 measurements, a counted array of six-uint32 samples, and
two trailing uint32 fields. The server validates and retains this diagnostic
record without applying it to authoritative movement or scores.

## English localization investigation (2026-09-09)

`C:/game-client/export_win32/language/en` contained only 75 XML files;
`C:/game-client/export_win32/language/en` contained 1,886.
The missing 1,811 files included 57 database localization files, 1,749 quest
files, and five other UI/input files. Other language folders still had their
travel-map XML. This explains the `LF:` fallback; the available evidence does
not identify the operation that removed the English files.

With user approval, the missing files were restored from that backup after
XML validation. Existing files were preserved and their SHA-256 hashes
verified unchanged; restored files were verified byte-for-byte against the
backup. The restoration manifest is
`data/diagnostics/english-localization-restore.json`.
`db.travelmap.item_misc_travelmapTitle` now resolves to `Universal Map`.
Restart the client to reload localization, and restart the server to load the
new ranking handlers. Live client UI rendering has not been tested here.
