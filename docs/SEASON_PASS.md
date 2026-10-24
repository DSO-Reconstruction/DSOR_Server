# Season Pass

## When the client shows it

The client runs its Season Pass (`Managers::ClientSeasonPassManager`, the
in-game Season Pass button and window) while the event named by
`_Globals.SeasonPassEventId` is active. In the release261 static database that
is `event_season_ticket`, a global, non-map-local event. Each run of that event
in the event schedule (`data/events.json`) is one season; its event instance ID
identifies the season.

The client learns about the season from the ordinary event packets:

| Packet | Carries |
| --- | --- |
| `EventUpdate` (player init, and pushed when the CMS or the schedule watcher starts or ends a run) | The run's instance ID, name, start and end. The window's countdown uses the end time. An ended run is sent once as inactive. |
| `GetSeasonPassRewardsInfoResultCommand` | Season level, total experience, activated ticket types (1 gold, 2 red, 3 double gold, 4 double red) and claimed reward IDs. Sent during player init, in reply to `GetSeasonPassRewardsInfoCommand`, after level purchases, claims and ticket activation, and when a season starts or ends while the character is online. |
| `GetSeasonPassMissionsInfoResultCommand` | `(mission ID, progress)` for every mission. Sent in reply to `GetSeasonPassMissionsInfoCommand`, after progress changes, and when a season starts or ends online. |

Reward contents, level thresholds and missions are **not** sent: the client
reads `_Template_SeasonPassRewards` and `_Template_SeasonPassMissions` from its
own static.db4.

Client requests (`protocol/season_pass_protocol.py`): `GetSeasonPassRewardsInfo`,
`GetSeasonPassMissionsInfo`, `AddSeasonPassExp` (buy experience, 10 Andermant
each) and `GetSeasonPassAward` (claim; reward ID = lane × 100 + level, lanes
0 free, 1/3 gold, 2/4 red).

## Server rules

- Missions, the daily login mission and experience purchases count only while a
  season runs. Claims and reward-info queries are answered at any time.
- Saved progress records the season it belongs to (`season_pass.season_id`).
  When a character meets a different season, level, experience, mission progress
  and claimed rewards start over; activated tickets carry over. Progress saved
  before seasons were tracked (no `season_id`) joins the first season it meets.
- Resuming an ended run (same instance ID) keeps the progress made in it.
  Starting a new run starts a new season.

## Rewards

Reward edits from the CMS are stored in `data/season_pass.json`
(`DSO_SEASON_PASS_CONFIG`), keyed by reward set and level:

```json
{"rewards": {"standard": {"2": {"exp": 450, "gold": "Loot:season_coin,777"}}}}
```

`standard` is `SeasonPassAward1`–`3` (free, gold, red); `alternate` is
`SeasonPassAward4`–`6`, used by clients whose table has them while
`event_season_ticket_2026_temp` runs. `exp` is the total experience a level
needs. Only fields that differ from static.db4 are stored, so the file survives
a static.db4 refresh.

The server applies the file whenever it grants a reward or computes a level.
So the client shows the same rewards:

- every CMS edit is also written in place into the client databases listed in
  `DSO_CLIENT_STATIC_DB` (separate paths with `;` on Windows), with a dated
  `.season-pass-YYYYMMDD.bak` backup beside each; restart the client to see it.
  After replacing a client's static.db4, use **Write rewards to client
  databases** on the Season Pass page.
- dsor clients download a static.db4 with the edits applied from the account
  service (`services/content.py`); the server's own file is not modified.
