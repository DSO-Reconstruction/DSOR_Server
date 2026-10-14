# Wanted Thief mission board

The map server handles `GetWantedThiefInfoCommand` (`0x0164`) and ordinary
`AcceptWantedThiefMissionCommand` (`0x0166`, slots 1..3), returning
`GetWantedThiefInfoResultCommand` (`0x0165`). IDs resolve by client class name
through `DSO_COMMAND_IDS`. Board state lives in the character's `wanted_thief`
account document, alongside the ordinary quest log.

The initial board uses the four `_Template_WantedMissionClassify` pools and
`_Template_WantedMissionAll.WantedMissionWeight`. Mission numbers are the
numeric **Id** values, not SQLite row numbers. All 63 definitions resolve to
`wantedrepeatable` quests in `data/quests.xml`.

Current database settings are 10 ordinary attempts, 5 fourth-slot attempts,
30 Andermant for a single refresh, 100 for refresh-all, and 500 for the
fourth-slot purchase. These values are loaded from `_Globals`.

Opening the board reuses saved offers. The server resets daily counters at
midnight UTC and retains mission selections/statuses. UTC follows the client
countdown (`0x1404c8ce4`, which uses `GetSystemTime`); retaining selections is
the local server policy, not established by a midnight retail capture.

## Client visibility and the Loading label

The shipped schedule now activates `event_wanted_thief_silent` (runtime ID
65537). The client checks this event in `0x1404a5910`; `0x1404c99e4` uses the
result to enable the Wanted tab. Sending a board alone does not activate it.
The event's authored `Silent` category suppresses server event announcements.
Characters must also reach level 10; normal acceptance respects the event's
excluded maps and level bounds. Custom schedules must enable this event too.

The UI contains **two** `LoadingLabel` elements. Wanted's field at widget
`+0x5b8` displays `gui.window.dailychallenges_wantedNotUnlockedMessage` and is
visible below level 10 (`0x1404c7618`). Daily Challenges' field at `+0x508`
displays `gui.window.questlog_dailyAchiShuffleMessage` while its challenge
list is empty (`0x1404bdfdc`). Despite the shared localization prefix, they
belong to different tabs and consume different commands. Daily Challenges
uses `0x0083`/`0x0094`/`0x0095`; its separate loading and selection
implementation is documented in [DAILY_CHALLENGES.md](DAILY_CHALLENGES.md).

Each Wanted response logs `Wanted Thief reply 0x0165` with the mission IDs,
statuses, and counters. After deploying the Python changes, restart the
server and re-enter the map to receive the event initialization update.
The user has confirmed that the Wanted Thief board now loads.

## Verified protocol

| Command | Client body after `8b` and little-endian command ID |
| --- | --- |
| `0x0164` GetWantedThiefInfo | Empty |
| `0x0166` AcceptWantedThiefMission | One uint32 slot; UI passes index + 1 |
| `0x0167` RefreshWantedThiefMission | One uint32 selection |

Response `0x0165`, after the application header:

| Client object offset | Wire value |
| --- | --- |
| `+0x20` | uint32 ordinary attempts remaining |
| `+0x24` | uint32 fourth-slot attempts remaining |
| `+0x28` | uint32 count, then uint32 mission IDs |
| `+0x48` | uint32 count, then uint32 slot statuses |
| `+0x68` | DateTime: year, day, hour, minute, second, millisecond, month, weekday; eight uint32s |
| Additional server data | uint32 actor, **0** in the retail response |

The arrays each have four elements. The DateTime is fixed-width, not a third
counted collection. Zero fields represent the default inactive VIP timestamp.
The fourth card uses a separate counter and purchase button; ordinary premium
membership must not be treated as Wanted VIP without entitlement evidence.

Binary evidence from `dro_client64-patch5.exe`:

- `0x140947230`: command ID `0x0164`.
- `0x140941614`: request encoder resolves to an empty stub.
- `0x14093b26c`: response decoder; `0x14091e3e0` reads a counted uint32 array.
- `0x140a5551c` / `0x140a55630`: DateTime decoder / encoder.
- `0x140285f9c`: stores the response in `ClientWantedManager`.
- `0x1404c7618`: draws four cards; status 1 shows progress and hides acceptance.
- `0x1404a2ab0`: normal acceptance sends a one-based slot.
- `0x140582778`: the fourth-card buy button checks `WantedVIPBuyRC`, then
  invokes acceptance for slot 4.
- `0x1404afe20`: refresh animation skips slots whose status is 1.

## Retail evidence and outstanding behavior

User capture `capture_20260909_000000_sample_12.log`:

- `12:08:27.727`: outbound `8b6401` (24 bits).
- `12:08:27.769`: inbound `0x85` batch with `0x0165` (704 bits): counters
  `(7, 5)`, mission IDs `(23, 54, 52, 7)`, statuses `(1, 1, 1, 0)`, zero
  DateTime and actor, then batch terminator `ff`.
- The subsequent PvP refresh/accept requests are **Daily Challenges**
  (`0x0094`), not Wanted missions. They do not establish Wanted slot changes.

The response body is reproduced byte for byte by the protocol regression
test (the server uses the equivalent single-command `0x84` envelope).

The earlier sibling capture `capture_20260909_000000_sample_11.log` contains the Wanted
acceptances:

| Request time | Slot | Accepted quest | Remaining | Statuses |
| --- | --- | --- | --- | --- |
| 12:05:51.131 | 1 | k0003-wanted-point-23 | 9 | 1,0,0,0 |
| 12:05:52.449 | 2 | pw207-wanted-point-54 | 8 | 1,1,0,0 |
| 12:05:52.935 | 3 | pw206-wanted-point-52 | 7 | 1,1,1,0 |

Each reply first sends `QuestUpdate` (accepted), NPC/chapter state, then
`0x0165`. These board replies occur inside batches, at bit offset 20718 of
21414-bit messages; searching only their leading command misses them.
The fourth-slot counter stays 5 and all four mission IDs stay unchanged.
Tests replay this sequence through gameplay dispatch, verify quest and board
persistence together, and ensure repeated or invalid acceptance spends nothing.
Direct generic quest acceptance cannot bypass the board's attempt counter.

Wanted refresh charging (`0x0167`), fourth-slot purchases, and completion/
replacement transitions still need a corresponding retail action sequence.
Refresh remains unimplemented; fourth-slot acceptance returns the unchanged
board without charging. Accepted quests enter the ordinary quest engine,
but Wanted completion/replacement is not wired up yet. The supplied captures
establish ordinary acceptance, not the entire Wanted mission lifecycle.
