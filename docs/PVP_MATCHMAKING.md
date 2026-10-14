# PvP matchmaking

Queue → match found → accept → arena → best-of-three rounds → result overview,
reconstructed from a recorded retail session and implemented end to end.

Reference capture: `capture_20260909_000000_sample_05.log`. The account queued for a 1v1,
was matched after 8.6 s, accepted, played three rounds (loss, win, loss) and
read the end-of-match overview. A second session,
`capture_20260909_000000_sample_02.log`, provides a match the same account **won**, which
is what fixes the meaning of `PVPMatchOver`'s winning-team field.

## 1. Why this was blocked

Ids `0xC0`–`0xDA` had no class name in
[command_ids.json](command_ids.json), so the whole subsystem was a wall of
unnamed opcodes. The cause was in the extraction tool, not the client: the
scan matched `Commands::…Command` only, and every one of these classes is in a
`PVP::` namespace. Widening the pattern named all 28 of them at once — see
[COMMAND_IDS.md](COMMAND_IDS.md).

| id | class | direction |
| --- | --- | --- |
| `0x7F` | `Commands::HonorPointsChangedCommand` | S→C |
| `0xC1` | `PVP::PVPMatchOverCommand` | S→C |
| `0xC2` | `PVP::PVPMatchStartCommand` | S→C |
| `0xC3` | `PVP::PVPMatchCountdownCommand` | S→C |
| `0xC4` | `PVP::PVPMatchInfoCommand` | S→C |
| `0xC7` | `PVP::LeavePVPCommand` | C→S |
| `0xCB` | `PVP::SetBattlegroundIdCommand` | S→C |
| `0xCC` | `PVP::ShowPVPDeathDialogCommand` | S→C |
| `0xCE` | `PVP::JoinMatchQueueCommand` | C→S |
| `0xCF` | `PVP::JoinMatchQueueResponseCommand` | S→C |
| `0xD0` | `PVP::JoinMatchQueueInfoCommand` | S→C |
| `0xD1` | `PVP::AbortMatchQueueCommand` | C→S |
| `0xD2` | `PVP::JoinBattleGroundCommand` | S→C |
| `0xD3` | `PVP::JoinBattlegroundResponseCommand` | C→S |
| `0xD7` | `PVP::MatchMakingInfoCommand` | S→C |

## 2. The recorded flow

Times are from the capture; the client was on the hub map server until 02:06:05
and on a separate arena map server after it.

```
02:05:51.050  C→S  0x00F2  HonorLocalUserRankingInfo      (PvP tab opened)
02:05:51.140  S→C  0x00F3  ...Response
02:05:51.709  C→S  0x00CE  JoinMatchQueue      mode 0, kind 0
02:05:51.758  S→C  0x00D7 + 0x00D0            MatchMakingInfo + queue status
02:05:52.431  S→C  0x00D7 + 0x00D0            repeated, queued_players 1 → 2
   ... 8.1 s ...
02:06:00.558  S→C  0x00D2  JoinBattleGround    map "b1102_dm"   ← dialog opens
02:06:01.975  C→S  0x00D3  JoinBattlegroundResponse           ← accepted
02:06:02.162  S→C  0x0070  SwitchMap
02:06:05.756  S→C  0x0070  SwitchMap → 203.0.113.13:31570     ← arena server
02:06:07.471  S→C  0x00CB  SetBattlegroundId  "BestOf3_1v1_01_default"
02:06:07.494  S→C  0x00C4  PVPMatchInfo                       ← every ~2 s
02:06:11.902  S→C  0x00C3  PVPMatchCountdown
02:06:15.908  S→C  0x00C2  PVPMatchStart      tick 329, 3000
02:06:24.902       round 1 decided (the account died)
02:06:27.949  S→C  0x00C3  PVPMatchCountdown
02:06:31.948  S→C  0x00C2  PVPMatchStart      tick 730, 3000
02:06:39.063       round 2 decided (the account killed)
02:06:42.115  S→C  0x00C3  PVPMatchCountdown
02:06:46.103  S→C  0x00C2  PVPMatchStart      tick 1084, 3000
02:06:59.104       round 3 decided (the account died)
02:07:02.158  S→C  0x00C1  PVPMatchOver       winning team 1
02:07:05.722  C→S  0x00C7  LeavePVP
02:07:05.947  S→C  0x0070  SwitchMap back to the hub
```

Fixed intervals worth copying: **4 s** from every countdown to its start, ~3 s
from a decided round to the next countdown, and ~3 s from the last round to
`PVPMatchOver`.

## 3. Clocks

Rounds start at ticks 329, 730 and 1084 — gaps of 401 and 354 — against
wall-clock gaps of 16.04 s and 14.16 s. Both give **25 Hz**, i.e. 40 ms per
tick. Every `PVPMatchStart` and `PVPMatchInfo` carried a round duration of
**3000 ticks = two minutes**.

## 4. Reading the layouts

Guessing field layouts off a hex dump does not survive bit-packed commands.
The layouts here were read out of the client's own serializer and then checked
against the capture:

1. `command_ids.json` gives each class's vtable.
2. Slot 4 is `Encode`. Its body is flat: each member is either written inline
   through the bitstream vcall `[rax+0xa8]` — `WriteBits(&tmp, nbits)`, with
   the width in `r8d` — or handed to a small per-type helper.
3. Tracking which `this+offset` was touched immediately before each write
   recovers the field order and width.
4. The result is accepted only when decoding a captured message consumes its
   bit length **exactly**, ending on the batch terminator.

Two findings from step 1 that make everything else fall out:

- **`Network::Command::Encode` is a bare `ret`.** There is no shared header;
  a command's leading fields are its own members.
- **The batch framing is** `0x85`, then repeated *(uint16 command id + body +
  `0xFF`)*, all bit-packed. A body ending on a bool leaves the next id
  byte-misaligned, which is normal.

There *is* one shared trailer, though, and missing it was the first live bug
(see §7): an outer server-command wrapper owns a single trailing `uint32` actor
id, written after the body and before the terminator. Only classes that
override vtable slots 13/14 have it — 270 of 341 — and the PvP block is split
across both kinds:

| carries an actor | no actor |
| --- | --- |
| `JoinMatchQueueResponse` (0xCF), `ShowPVPDeathDialog` (0xCC), `HonorPointsChanged` (0x7F) | `MatchMakingInfo`, `JoinMatchQueueInfo`, `JoinBattleGround`, `SetBattlegroundId`, `PVPMatchInfo`, `PVPMatchCountdown`, `PVPMatchStart`, `PVPMatchOver` |

Every command in the right-hand column appears in a capture and round-trips
byte-for-byte, which is what confirms they carry nothing extra. The three on
the left never appear in any recording — which is exactly why the omission
survived into a live test. `dump_command_ids.py` now writes an `actor_scoped`
flag into `command_ids.json`, `_finish_server_command` refuses to encode a
command whose scoping disagrees with the module's table, and a test
cross-checks that table against the catalog.

`PVPMatchOver` is the shortest full check: `uint32 + uint32 + bool` = 65 bits,
plus the 8-bit id prefix, the 16-bit command id and the 8-bit terminator = 97,
which is the recorded bit length exactly.

`PVPMatchCountdownCommand` has **no members at all** — its `Encode` tail-jumps
to the empty base — so the whole command is `85 c300 ff`.

### The scoreboard

`PVPMatchInfo` (0xC4) is the one that matters: it drives both the in-match HUD
and the end-of-match overview. Layout, from `0x14099b7b0`:

```
uint32   round start tick
uint32   round duration ticks (3000)
uint32   team count, then per team (0x140a5efe4):
    uint32  team id
    uint32  rounds won
    uint32  score
    float   win chance
    uint32  player count, then per player (0x140a5eda8):
        uint32 x10   actor id, honor breakdown, rounds won/lost,
                     damage dealt, kills
        float        score
        uint32       rating
        uint16       level
        bool         enemy
        string       name
        uint32       team slot
        uint32       count + uint32[] stats
        uint32       deaths          <- written last, out of offset order
string   battleground id ("BestOf3_1v1_01_default")
uint32   stage
```

The trailing `deaths` field sits at object offset +0x28 but is serialized
**after** the stats array. That is not cosmetic: getting it wrong desynchronizes
every following row.

Labels came from watching the values move across the recorded match: rounds
won/lost tracked the 1-2 scoreline, kills reached 2 for the winner and 1 for the
loser, damage grew monotonically within each round, and the two teams' floats
were 0.371 and 0.629 — summing to one, hence a win chance.

## 5. Implementation

| file | role |
| --- | --- |
| [pvp_match_protocol.py](../../pvp_match_protocol.py) | codecs, enums, battleground tables |
| [pvp_match_state.py](../../pvp_match_state.py) | queue and match runtime, no sockets |
| [services/matchmaking.py](../services/matchmaking.py) | queue, dialogs, arena handoff |
| [services/battleground.py](../services/battleground.py) | rounds, scoreboard, completion |
| [gameplay_routes.py](../../gameplay_routes.py) | armed handoff routes, queued per address |

The server runs every map in one process, so the two-map-server handoff retail
performs collapses into one `_begin_map_transfer` per participant. Both players
land in the same instance because `_gameplay_instance_key` keys on the physical
map plus access group.

A battleground participant's session is re-bound **after** the arena map load,
from `_bind_battleground_session`, because the transfer replaces the socket:
the arriving session is a different object from the one that accepted.

### Leaving

Pressing leave, quitting, and dropping the connection are the same event: the
player concedes, and with one side gone the match is decided immediately and
awarded to the last team standing rather than running its rounds out against
nobody. `LeavePVP` and the presence-disconnect hook both funnel into
`_forfeit_battleground`.

The one case that must *not* count as leaving is the arena handoff itself: the
source socket is retired before the destination connects, so a participant who
has not yet `arrived` simply has their slot unbound and re-bound by the
arriving session.

### Deaths

Open-world PvP floors damage at one health, and that stays. A round cannot be
decided without a death, so `_pvp_match_opponents` grants lethal damage only
between opposing participants of a **running** round; combat resolution asks it
before applying the floor. Health is refilled at each round start.

### Honor

`HonorPointsChangedCommand` carries a career total, which is persisted on the
account (`honor_points`, alongside `pvp_matches_won` / `pvp_matches_lost`).
Retail's own award formula cannot be recovered from a single match — its
scoreboard showed a participation figure stepping 110 → 275 and a round-win
figure stepping 27 → 68 — so the schedule in `pvp_match_state.py` is the
server's own, shaped to land in the same range.

## 6. Open ends

- **Modes other than 1v1** are wired up to the point of queueing: team sizes,
  battleground ids and arena maps are tabulated for `BestOf5_5v5`,
  `CaptureTheFlag_5v5`, `AssaultTheFortress_5v5` and `LastManStanding_8ffa`,
  but only the 1v1 round rule (last team standing) is implemented. Capture the
  flag and assault need their own objective handling.
- **Group queueing** (`queue_kind` = 1) is decoded and accepted but treated as
  a solo entry; nothing reads the player's group yet.
- **Social groups do not protect arena opponents.** Outside a match, players
  in one group cannot harm each other even with PvP enabled. Inside a match
  the roster decides: `_battleground_hostility` compares the two sessions'
  match teams, so two grouped friends who queue into a 1v1 are opponents and
  the duel can end, while team-mates in a team mode stay friendly whether or
  not they are grouped. Before this, a grouped pair in the arena had mutual
  friendly-fire protection and the match could never be decided.
- **Honor rankings** now answer the overall and local-player queries, plus
  the all-time results table. Offline profiles and live career scores are
  included. Solo/team/daily/weekly history and PvE rankings still need their
  own score tracking; see [LEADERBOARDS.md](LEADERBOARDS.md).
- **Unlabelled scoreboard fields.** `MatchPlayer.score` (a float that climbed
  to 398 for one player and 5 for the other) and `honor_unknown` were not
  pinned down by one match. They round-trip correctly; their meaning is a
  guess.
- **Round-timeout tiebreak.** All six recorded rounds ended on a kill well
  inside the 3000-tick limit, so retail's rule for a round that runs out is
  unknown. The server awards it on damage dealt.
- **Match timers are hosted on a participant session**, because that is the
  scheduler gameplay deadlines already run on. Retiring that session cancels
  its timers, so `_arm_match_timer` re-hosts on a connected participant each
  time it arms. A dedicated match scheduler would be tidier.

## 7. Fixed after the first live test

Two defects showed up the first time this ran against two real clients.

**The client dropped `JoinMatchQueueResponse`.** `DSOClient.log` recorded
`Could not decode command's additional server data (ID: '207')` on both
clients, because 0xCF is one of the three commands here that carries the
wrapper's trailing actor id and the encoder was not writing it. A dropped
command leaves the client's bit reader mid-stream, so the rest of that batch
decodes as garbage — which is the likely route to the
`inGameWidget.isvalid()` assertion in `GetInventoryWidget` that killed one
client during the arena load, since the inventory commands in the
initialization batch are exactly what a corrupted reader would land in.

**A departed player left the match running.** Only `LeavePVP` forfeited; a
disconnect did not, so the match kept ticking and kept broadcasting a
scoreboard row whose session was gone — and that row carried **actor id 0**,
which is not a resolvable actor. Participants now capture their actor id on
arrival so the row survives them, and a disconnect forfeits.

## 8. Fixed after the second live test

**One player was dropped to character selection instead of the arena.** Armed
handoff routes were keyed by client IP alone, one slot per address. Two clients
on one machine share an address, so the second transfer overwrote the first:
one player was initialized as the other's character, and the other found no
route and fell through to character selection. Since a battleground is the
first feature that transfers two players at once, nothing had exercised this
before.

Routes are now a queue per address ([gameplay_routes.py](../../gameplay_routes.py)),
claimed at the `0x8a` hello — the first point where the session GUID resolves
to an account, and therefore the first point where one of several queued routes
can be attributed to the right client. `ID_NEW_INCOMING_CONNECTION` carries no
identity, so bring-up only asks *whether* the address has a route pending and
takes the oldest provisionally; the hello then claims the correct one and
rebinds the session.

**The round timer never moved.** The server does not own the clock the client
counts against: it learns it from the tick every `MoveCommand` carries and
advances it from wall time between moves. `PVPMatchStart` was carrying a
match-relative tick starting near zero, so the client computed a remaining time
against its own much larger clock and the HUD timer never ran. The round start
is now stamped per participant in that client's tick base and held fixed for
the round, so it does not jump as the two clocks drift.

## 9. Fixed after the third live test

**Both sides spawned on the same point.** The transfer used the map's single
default spawn. Arena maps author `_Instance_Entry` rows per side, tagged
`OwnerTeam` (`RedTeam`/`BlueTeam`), and **each side has two of them**:

| `EntryType` | role |
| --- | --- |
| `Entry` | a staging area off one end of the map, where the side lands on map load and waits out the countdown |
| `Respawn` | the position inside the arena proper, where the side actually fights |

Arriving at the staging entry is only half of it: retail then sends a
`ForceTransformCommand` (0x0071) in the **same batch as `PVPMatchCountdown`**
to move each player from staging into the arena. That command was sitting in
the recorded countdown batch all along, immediately after the countdown's
terminator, and decodes as three position floats, a heading, and the actor id
from the server-command wrapper:

```
7100 0000604100000000000034429e59d2bf 00000100 ff
     (14.0, 0.0, 45.0)  heading -1.6434   actor 65536
```

`(14.0, 0.0, 45.0)` with heading `-1.6434` is `BlueTeamRespawn` on
`b1102_dm` exactly, and the other recorded session's
`(35.0, 0.0, 71.0)` heading `-0.0559` is `RedTeamRespawn` — so the map data
reproduces retail's payload byte for byte. `load_local_team_entries` returns
both positions per side; the transfer uses the staging entry and the countdown
force-transforms into the arena.

**Players could not attack each other.** `gameplay_pvp_enabled` only satisfies
the *server's* hostility check. The client keeps its own copy — it is what
decides whether an opponent can be targeted at all — and updates it only from
the `PVPFlagCommand` response and the matching presence broadcast. Entering a
battleground now sends both, and the account's own flag is restored on the way
out.

**The result could fail to reach the arena.** `_finish_match` was gated on a
one-shot settlement claim covering the *whole* function, so if a forfeit
settled the match first, the result timer's later call returned before sending
anything and nobody saw the overview. The claim now covers only the account
credit; the scoreboard and `PVPMatchOver` always go to whoever is still in the
arena.

### What the client does with `PVPMatchOver`

Worth recording, because it settles the win/loss question. The handler at
`0x1402e42b0` stores the command's second `uint32` (+0x1c) into the PvP
manager at +0xe0 and moves it to state 4. The scoreboard then calls
`0x1402e26d8(manager, team)` once per team:

```
cmp dword ptr [rsi + 0xe0], ebp   ; stored winner vs this team index
setne bl
lea eax, [rbx + 1]                ; 1 = won, 2 = lost   (0 = tied)
```

So the field **is a team index**, compared per team, and `-1` means a draw —
which is what `PVP::ScoreboardWidget::SetNameOfTeam` turns into
`scorescreenTeamWon` / `scorescreenTeamLost` / `scorescreenTeamsTied`. The
server awards a forfeit to the last team standing and sends that index.
The personal result uses a different lookup: it compares the winner against
GamePlayer +0x18, initialized from NewPlayer +0x388. A resolvable scoreboard
row is sufficient for the honor display but does not populate that field.
See section 16 for the confirmed client path.

## 10. Fixed after the fourth live test

**No combat at all in the arena.** The server said so plainly:

```
accepted SkillCast index=1851, but produced no hit:
    the current map has no local combat encounter
```

`load_local_monster_encounter` returns `None` for a map that spawns nothing,
and world initialization reads that as "no combat here" -- it builds no
`LocalCombatState`, so no player health is registered and every cast is
rejected. That is the right answer for a town and exactly wrong for a
battleground, where the players *are* the combatants. It also silently
disabled the PvP hostility check, which requires both sides to share a combat
state.

`load_local_player_only_encounter` builds an encounter with an empty roster but
the map's real navigation, terrain and coordinate frame, and world
initialization uses it when the arriving session belongs to a match on that
map. Scoping it to a real match matters: a town spawns nothing either, and must
not gain a combat world it never had.

The encounter is **cached per map**, and that is load-bearing rather than an
optimization. The shared combat state is selected by encounter *identity*
(`peer.gameplay_monster_encounter is monster_encounter`), so two players
holding separately-built encounters would each get their own combat world and
could never touch each other.

**The countdown, its movement lock and the overview were all inert.** Not
because of the commands -- `85 c300 ff` is exactly what retail sends -- but
because the client never brought its PvP subsystem up on the map.

The 0x86 map load announces a **ruleset**, and the recorded arena load used a
value nothing else does:

```
02:06:06  b1102_dm   ruleset=14      <- the battleground
02:07:06  b1100_hub  ruleset=5       <- a hub
```

`gameplay_map_ruleset` derives its answer from `_Template_MapTemplate.AreaType`,
which only distinguishes `Hub` (5) from everything else (1) -- and the arena is
filed there as **`ClosedPVE`**, so no amount of reading that table produces 14.
The server was announcing `b1102_dm` as an ordinary PvE map, the client's PvP
manager was never attached, and every PvP command after that was delivered to
nobody. `battleground_ruleset` overrides it for maps in the battleground table;
the resulting map load is byte-identical to the recorded one.

Worth keeping in mind for the rest of the subsystem: `SetState` on the client
performs no transition validation, and the countdown handler is unconditional.
So a PvP command that appears to do nothing is almost always a command that
never reached its manager, not one the manager rejected.

## 11. Fixed after the fifth live test

**A cast with no targets crashed the ingress worker.** The summary log
dereferenced `pending_hits[0]` unconditionally, so a swing at empty ground --
or at an actor the caster may not harm -- raised `IndexError` out of
`_handle_spatial_skill`. Ordinary in an arena; now reported as `impact_in=n/a`.

**`SetBattlegroundId` now precedes the initialization batch.** Retail puts it
at the head of the arena's initialization, before `NewPlayer`. The client
builds its PvP UI while processing that batch, so the battleground manager has
to exist first; sent afterwards it arrives too late for the countdown and the
result panel to have anywhere to go.

**Scoreboard actor IDs must match each viewer's actor table.** The retail
allocator gave its local player actor 65536; our `NewPlayer` gives the local
player actor 1. Copying retail's numeric ID convention made our client unable
to find its own scoreboard row. `_match_info_for` now uses actor 1 for the
viewer and the presence actor IDs for opponents. See section 15.

## 12. Still open

- **Attacking another player's minions.** PvP target selection resolves
  candidates from the presence `combat_targets` list, which contains player
  sessions only, and the damage path is written against a target *session*.
  Summons live in `combat.player_summons` with their own health and are not
  reachable from either. This needs a real summon-damage path, not a filter
  change.
## 13. Fixed after the sixth live test

The evidence had a hole that four rounds of guessing kept falling into: the
capture scan only ever read the **first** command of each batch. Retail's
batches carry many, and scanning byte-aligned continuations after each `0xFF`
found two commands the server was not sending at all.

**`PVPMatchDescription` (0xCA) was missing.** Retail sends it *in the same
batch as* `SetBattlegroundId`, as the arena's very first message:

```
85 cb00 1600 "BestOf3_1v1_01_default" ff  ca00 80 000000 7f ...
                                          ^^^^ MatchDescription(True, 0)
```

`SetBattlegroundId` only names the battleground. This is what tells the
manager a match is running on it -- so with the id alone the client has a
battleground and no match, and the countdown, its movement lock and the result
panel all arrive at something not running one.

**Round revival and honor display required further corrections.**
`ActorStatsUpdate` changes health, but `ReviveCommand` is needed to end the
client's Death state. The 2026-09-09 capture confirms it is sent for both
players during countdown setup. The old assumption that the overview derives
honor gain from `rating` and career honor was unverified. We retain the
captured career metadata and send the actual awarded amount in each row's
`honor_total`, including the match-win bonus, before opening the overview.
See section 15 for the complete reset and result fixes.

## 14. Running the tests

```powershell
python -m unittest test_pvp_match_protocol test_pvp_match_state test_gameplay_routes
python -m unittest dso_server.tests.test_pvp_matchmaking
```

`dso_server/tests/` has no `__init__.py`, so a root `unittest discover` does
not reach it; name the module explicitly.

## 15. 2026-09-09 round and overview corrections

Reference: `capture_20260909_000000_sample_07.log`. Decode complete bit-packed batches;
`application_messages().command_id` identifies only their first command.

| Event | Round 1 | Round 2 | Round 3 |
| --- | --- | --- | --- |
| C3 countdown/setup | 01:05:02.480 | 01:05:18.920 | 01:05:52.966 |
| Message 5, argument 3 | 01:05:03.475 | 01:05:19.921 | 01:05:53.959 |
| Message 5, argument 2 | 01:05:04.476 | 01:05:20.915 | 01:05:54.955 |
| Message 5, argument 1 | 01:05:05.478 | 01:05:21.919 | 01:05:55.955 |
| C2 round start | 01:05:06.481 | 01:05:22.915 | 01:05:56.969 |

The visible **3?2?1** uses `MessageCommand` (0x89), message type 5, one kind-1
integer argument, actor wrapper 0. Each is 144 bits. C3 alone has no digits.
The complete setup lasts four seconds. Its final message is type 28 with the
one-based round number. Timer generations invalidate cancelled countdowns.

Each setup batch transforms both participants to their team respawns, sends
health/resource stats and `Revive` (0x6D, duration 0), then applies two-second
`pvp_respawn` and four-second `pvp_invulnerable`/`pvp_freeze` effects. Their
retail IDs are 6213/6177/6179; the local content IDs are 5928/5892/5894. These
are resolved from authored names rather than copied across content versions.
The C2 batch includes the zero-duration `pvp_cooldown_reset` (retail 6214,
local 5929). Zero duration still executes its client cooldown-reset modifier.

The server now cleanses transient self effects, all incoming victim effects,
and persistent debuffs for survivors and corpses before restoring full health.
It cancels PvP hits and periodic damage from the prior round, emits explicit
status removals to the owner and peers, and preserves permanent character
bonuses and non-debuff consumables. Revival occurs during setup; the new round
starts with setup locks explicitly removed. Movement, casts and incoming damage
are rejected while a match is outside its running phase. Delayed PvP damage
also carries its originating match/round so it cannot hit a later life.

In this capture the local actor is 65536 on team 1; the opponent is 65538 on
team 0. Each row's trailing team field is its actual team (0 or 1), not a
constant 1. The final C1 is `MatchOver(0, 1, False)`: team 1 wins. Our server
uses the same winner index for both viewers and maps the local scoreboard row
to actor 1 on each socket. Each final row's `honor_total` equals the account
award, including the existing server schedule's winner bonus. Career honor is
credited once; the final scoreboard and new total precede C1. The retail honor
formula is not inferred from this single match.

Regression coverage in `tests/test_pvp_matchmaking.py` exercises both winners,
both viewers, exact honor gains, duplicate settlement, revival at countdown
setup, survivor/corpse cleansing, peer notifications, explicit setup effects,
the zero-duration cooldown reset, countdown cancellation, and stale damage.

## 16. Personal result, spell teardown, and persistent player corpses

The 2026-09-09 follow-up exposed three remaining problems. The previous
scoreboard corrections fixed honor display, but did not fix personal victory.

### The personal result reads NewPlayer's team

The client path is now verified directly in `dro_client64-patch5.exe`:

- `ClientPlayerManager::HandleNewPlayerCommand` at `0x14020f828` reads command
  +0x388 at `0x14020fe66` and writes GamePlayer +0x18 at `0x14020fe74`.
- The personal result function at `0x1402e27b8` obtains the local GamePlayer
  and compares its +0x18 against the winner stored at manager +0xe0
  (`0x1402e28b2` / `0x1402e28b5`). It returns 1 for victory and 2 for loss.
- `0x1402e1f30` uses that result to select the `pvpVictory`/loss label.

The supplied capture's initial embedded NewPlayer (01:04:53.747) has actor
65536 and **field_388 = 1**. Its final MatchOver names winning team 1. Our
bootstrap kept field_388 at its constructor default, **0xFFFFFFFF**, for both
players. No scoreboard actor-ID or row-team change could repair that mismatch.
`_encode_initialization_new_player` now fills the field from the participant's
assigned team when initializing its matching arena map. Town initialization
retains the default. Tests serialize and decode NewPlayer and compare that
team with MatchOver for both viewers and both possible winners.

### Round setup removes map-owned spells

Before restoring players, the round controller now cancels participant spell
timers, removes their pending hit records, expires owned summons and ground
effects, removes traps, and clears stale ground-aura memberships. It publishes
DiscardMonster, DiscardLocationEffect, and DiscardTrap through the existing
presence paths. Already-expired summon actors awaiting delayed visual teardown
are also discarded; active objects owned by unrelated actors are preserved.
Delayed summon and ground-effect creation carries its originating match/round,
so callbacks queued before cancellation cannot recreate an old cast later.

### Kill's animation_time also controls corpse lifetime

The three captured player Kill records contain:

| Time | Victim actor | animation_time | field_4c | field_78 |
| --- | --- | --- | --- | --- |
| 01:05:15.873 | 65538 | 2160000 | 0 | false |
| 01:05:49.915 | 65536 | 2160000 | 0 | false |
| 01:06:02.639 | 65538 | 2160000 | 0 | false |

The server previously sent 300 for players and monsters alike. Player death
transactions now use the captured long corpse lifetime for both the local
victim and remote viewers. Monsters retain their existing short lifetime.
Player actors stay registered, and the round's Revive ends the death state.

`tests/test_battleground_polish.py` covers these paths, including real encoded
NewPlayer and death packets, both viewers' world-discard packets, preserved
unrelated spell actors, and callbacks arriving from a previous round.


## 17. Player death positions and PvP status application

The long corpse lifetime in section 16 matched retail, but did not fix the
reported immediate disappearance. The remaining fault was the position in PvP
Kill: target discovery returns Move-relative coordinates, while Kill carries
world coordinates. `ClientGameWorld::HandleKillCommand` (0x1401c71b8) takes
command +0x60 at 0x1401c73c7 and passes it directly to the actor motion controller
at 0x1401c77d4. It does not add the map origin.

For b1102_dm the origin is (26.1557388, 1.0718040, 45.5). A player at world
(25.49, 0, 53.57) was therefore killed at relative (-0.6657388, -1.0718040, 8.07),
placing the body elsewhere and below the arena. Direct and periodic PvP damage
now sample the victim's latest movement and convert to world coordinates before
building the resolution. Movement/target acquisition remain in relative space.

The status audit corrected these independent faults:

- Victim debuffs now participate in the player's own combat stat/modifier
  snapshot, so outgoing-damage reductions affect their attacks. The snapshot
  can be read under the existing session lock without re-entry.
- Status-only skills preserve zero authored damage and still apply effects.
- Caster item-set victim procs are resolved at impact and sent to all viewers;
  viewers no longer inject bonuses from their own equipment into the hit.
  Periodic ticks do not reapply those hit procs.
- Status immunity is checked at impact. The loader retains DP separately from
  D, including modifier operations; PvP player and summon impacts select DP.
- A victim receives its impact/status transaction even after moving outside
  the caster's activity vicinity.
- Periodic damage is bound to the victim's installed status instance and source.
  Cleansing, expiry, or replacement stops that instance's damage; a stale timer
  cannot remove its replacement from round-cleanup tracking.

`tests/test_pvp_player_effects.py` exercises scheduling, real presence fan-out,
local/remote actor mapping, encoded death positions after movement, real authored
stun/weakening effects, immunity, separate PvP duration modifiers, and DoT
replacement. These are server/protocol regressions; rendering still needs a
live two-player check.


## 18. Lethal player-owned minions

Autonomous summon damage previously used `max(1, previous - damage)` and always
emitted `killed=False` for player targets. The simulation now accepts explicit
lethal target sets per summon owner, prepared outside the combat lock using the
same death policy as direct player spells and DoTs: opposing players in a running
arena round, or players on normal maps where respawn is allowed. Other maps keep
the nonlethal floor. Hostility and damage-feature gates exclude friendly,
invulnerable, dead, and countdown-protected targets before the simulation runs.

Lethal summon resolutions retain the summon as the attacking actor and its owner
as the source. Both viewers receive the existing world-position Hit/Kill path.
Arena damage and kills are reported for the owner, ending the round through the
normal match callback. Dead targets cannot be killed twice by multiple summons,
and lethal hits do not schedule new victim DoTs.

`tests/test_summon_pvp_kills.py` covers real wolves and turrets, encoded deaths on
both clients, owner kill/damage credit and round completion, nonlethal maps,
countdown/immunity/friendly protection, and already-dead targets.


## 19. Countdown movement authority

The movement handler used to return immediately when Movement was disabled,
leaving its later correction branch unreachable. The server held its position,
but the local client could continue predicting movement during grace time.

Blocked Move commands now keep the held position, update the observed client
tick, and send a stationary Move plus a reliable world-space ForceTransform.
The stopped Move has zero velocity/duration and the request's current tick;
replaying the previous moving command would continue the movement being blocked.
Rejected coordinates do not enter presence, vicinity, mount, or loot processing.
The arena phase remains authoritative even if the client's freeze has expired.

The ingress context also includes match identity, round index, and phase. Input
received during countdown cannot wait in the queue and become legal when the
round starts. Fresh movement is accepted normally after the start transition.
`tests/test_battleground_movement.py` verifies both players at all three digits,
expired freeze state, cancelled velocity, normal movement after start, and the
queued-input boundary.


## 20. Player spells against enemy minions

Player-owned summons were absent from player spell target discovery and had no
mutable health pool. Their friendly placement was also sent unchanged to every
viewer. Enemy minions now join the player-spell candidate list through their
owner's session, with the same geometry, range, hostility, and round checks.
They remain separate from ordinary map monsters, preventing friendly fire from
the PvE target list and preventing player/minion health or match-score confusion.

`player_summon_health` tracks current/max HP from the generated spawn stats.
Targeted and area hits, zero-damage status skills, and periodic damage address
the minion actor. Status sources are stored against that actor in the owner's
viewer cache; they do not become owner debuffs. Authored controls also enter the
minion simulation: stun blocks movement/attacks, relative slows alter speed,
weakening alters damage, and resistance debuffs affect incoming damage. PvP
status durations are used, and damage-over-time callbacks retain actor identity
so a removed minion cannot redirect a queued hit to its owner.

Spawn packets retain summon ownership/category and the owner's arena team.
Enemy viewers receive hostile monster placement (0x10001, relation 2), while
friendly viewers receive the existing player-owned placement (0x10004, relation
0). Existing minions receive faction updates when PvP opt-in changes; late
spawn snapshots use the current health pool. Hit/status packets reach the owner
and registered viewers even outside the caster's activity vicinity.

Minion kills use the existing Kill and authored delayed-despawn lifecycle, stop
attacks/status state, and remove the health pool. They never mark the owner dead,
award a player kill, end an arena round, or generate world-monster loot.
`tests/test_enemy_minion_targets.py` covers targeted projectiles, area attacks,
friendly protection, status-only skills, DoTs, stun/weakening behavior, faction
changes, stale impacts, and minion death during a live arena round.


## 21. Summon deployment before autonomous actions

Summon attacks previously backdated the first cooldown, allowing a cast in the
same simulation frame as NewMonster. Each actor now has an absolute readiness
deadline measured from materialization, separate from the player's pre-spawn
SummonSpawnDelay. Haste and slow affect subsequent cooldowns, not deployment.

The minimum comes from the monster's StartupDuration. For the bundled client,
conex turret Spawn clips (characters/dwarf_turret_conex_animations.nax3 and
characters/dwarf_turret_tesla_conex_animations.nax3) contain 26 keys at 40 ms:
1.04 seconds. Explicit AnimationHijack tracks in sequences/spawn.pbxml hold
woodwrath_spawn for 25 frames, dwarf_taunt_spawn for 14, and
minion_nian_cny2026_spawn for 10, at 25 fps. These small content overrides are
kept in the summon definition loader; the running server needs no client asset
installation. Sound/light/particle tails do not extend the action lock.

Attack readiness takes the maximum of deployment, authored initial attack delay,
and the sustained-skill initial delay. Tesla still waits 1.5 seconds. Mobile
summons and passive healing also wait for deployment. Aura lifecycle packets
continue normally so spawn visuals remain intact. Tests in
`tests/test_summon_startup.py` cover readiness boundaries, haste/slow, subsequent
cooldowns, movement, support, sustained delays, and removal before readiness.


## 22. Extra projectiles from item sets and status modifiers

The Heredur/Sigrismarr mage bonus installs
`item_pw_sigrismarr_mage_bonus_ice_missile`, whose program is
`BulletCount:2.0,absolute,iceball`. It adds two projectiles to Ice Missile's
base count of one. The PvP geometry loader already applied this modifier, but
the scheduler ignored the resulting bullet count and queued only one impact
per cast/target. PvE already enumerated all three projectiles.

The shared entity planner now enumerates each modified projectile. The PvP
health adapter includes its index in the pending hit/timer key. Damage, critical/block rolls, status-effect chances and
socket hit events retain that projectile identity. Each impact still rechecks
hostility, immunity, actor life and arena round identity. Non-projectile casts
retain one hit per selected target. Chained/reproduced projectiles are planned
separately from parallel projectiles, across the same eligible entities.

`tests/test_pvp_projectile_sets.py` evaluates the real equipped Heredur item pair,
checks three damage/status packets against a hostile player, validates enemy
minion damage, and verifies all three PvE impacts with the same set modifier.
It also covers removal of the bonus, independent status rolls, immunity between
impacts, and non-projectile casts.


## 23. Shared entity skill planning

`combat_entities.py` owns direct player-cast geometry, projectile counts,
piercing/contact budgets, splash deduplication, reproduced/chain targets,
area selection, and impact timing. Its immutable `CombatEntity` snapshots use
world coordinates and collision radii without depending on session or health
storage. Receiver-effect assembly and direct damage scaling also live here.

The production bullet and spatial handlers create one `EntitySkillPlan` for
map monsters, hostile players, and enemy player-owned minions together. Both
health adapters consume impacts from that exact plan. A monster in front of a
player can therefore consume a non-piercing projectile's collision; piercing
can hit both. Splash and chains can cross entity kinds without receiving a
second, independent collision budget. Location owner effects count all planned
targets, including players and enemy minions.

`LocalCombatState.plan_entity_skill_hits` supplies map-monster snapshots and
preserves bounded viewer-delivered movement contacts. The PvP session adapter
supplies hostile player/minion snapshots outside session/combat nested locks.
Both include target status modifiers that alter impact count. Neither adapter
inserts entities into another kind's health pool.

Health storage, target defenses, impact-time immunity, status instances,
replication, death/loot, and arena scoring remain in their existing adapters.
This refactor covers direct player casts; autonomous summon attacks, traps,
and periodic status damage retain their existing lifecycles. It is not evidence
of the retail server's internal architecture.

`tests/test_combat_entities.py` covers mixed monster/player collision budgets
through the production cast handler, explicit targeting, piercing, splash
uniqueness, chain target selection, point-impact timing, and area selection.
The existing PvE, PvP, item-set, status, minion, and matchmaking tests exercise
the health/lifecycle adapters with the shared planner.
