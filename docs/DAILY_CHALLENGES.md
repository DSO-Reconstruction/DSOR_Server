# Daily Challenges

Daily Challenges and Wanted Thief use separate commands and state. Daily's
loading label stays visible while its objective list is empty. Initialization
previously sent an empty `TimedChallengesInfoCommand` (`0x0083`); it now sends
four offers (Cooking, General, PvP, Boss) and seven bonus objectives for
characters at level 10 or above.

Definitions and weighted selection come from `_Template_DailyChallenges` in
`static.db4`. Offers respect character class, character/wisdom level, completed
quests, and active event gates. Initial offers use tier 1. The character's
`daily_challenges` account document persists the board and selections across
relogs and map changes. The local reset policy starts a fresh board at midnight
UTC on the next initialization or selection; no midnight retail capture has
established the server's exact rollover behavior.

## Protocol evidence

The user-provided `capture_20260909_000000_sample_12.log` includes Daily PvP refreshes
and acceptance, as well as the separate Wanted board request. Relevant Daily
commands:

| ID | Direction | Body |
| --- | --- | --- |
| `0x0083` TimedChallengesInfo | Server to client | uint64, then three counted challenge maps |
| `0x0094` DailyChallengeSelection | Client to server | string template ID, one refresh bit |
| `0x0095` DailyChallengeProgress | Server to client | Progress updates; not emitted yet |

The three `0x0083` collections are daily objectives, bonus objectives, and RC
spender offers. The codec's historical `weekly` and `monthly` field names refer
to the latter two collections, not calendar periods. Each entry contains the
map key, template ID, uint8 state, uint32 progress, and six uint32 timestamp
fields. Server commands carry the player actor context. The capture uses zero
for the initial uint64 and all objective timestamps; this server does likewise.
RC spender offers are independent and remain empty locally.

At `12:08:29.658`, the captured request refreshes `pvp_winmatches_tier2`:

```text
8b940014007076705f77696e6d6174636865735f746965723280
```

Its actual length is **201 bits**, not 208. The final bit is true for refresh
and false for acceptance. The reply at `12:08:29.700` replaces that offer with
`pvp_playmatches_tier2` and resends the full board, preserving the other three
accepted challenges. Later refreshes rotate through kill/win objectives;
acceptance at `12:08:33.790` sends the same format with a false bit.

`tests/fixtures/daily_challenges_retail.json` stores the 17,336-bit reply for
exact decode/encode regression coverage. The earlier `12:05` capture shows
four tier-1 offers at state 0, followed by acceptance requests. The later
initialization preserves accepted offers at state 2. Client function
`0x14019caa8` also changes state 0 to 2 when selecting a challenge.

## Implemented behavior and limits

`services/daily_challenges.py` handles `0x0094` independently of Wanted:

- Acceptance marks an available offer as accepted (state 2).
- Refresh chooses a different eligible template in the same category/tier.
- Refresh costs `_Globals.DailyAchievementReshuffleCosts` (currently 40
  Andermant), or zero with an active, unexpired membership whose authored
  `DailyChallengeFreeReshuffle` privilege is enabled. Premium and Deluxe have
  this privilege. Client refresh handler `0x1404bc90c` checks both values.
- Accepted, unknown, repeated, stale-day, and unaffordable requests do not
  spend currency. Replacement and debit are committed together under the
  session lock; persistence and network sends run outside it.
- A full board response resynchronizes the client after either action.

Objective progress, completion rewards, bonus payouts, and advancement to the
next tier are **not wired up yet**. This implements loading, acceptance, and
refresh rather than the entire Daily Challenges lifecycle. Wanted has its own
remaining work documented in [WANTED_THIEF.md](WANTED_THIEF.md).

After deploying, restart the map server and log in again or re-enter the map
to receive the populated initialization packet. Tests exercise the real
initialization builder, retail bytes, persistence, eligibility, membership
charging, and duplicate/concurrent requests; rendering still needs a client
retry.
