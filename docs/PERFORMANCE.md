# Concurrency limits and performance

## Production latency review, October 2026

The receive path now queues gameplay mutations alongside movement and casts.
Disk durability remains part of the handler transaction, while packet reception
and acknowledgements continue independently. State/profile saves share one
database barrier. Password hashing no longer owns the account registry lock,
and social serialization/writes run outside the membership lock.
Administrative mutations also leave the receive thread and share session
transaction gates with gameplay, initialization, trades, and retirement.

Transport uses bounded, paced per-session outboxes; control ACKs bypass their
ordered-payload lock. Offline pings are stateless, abandoned handshakes expire,
and reliable streams have hard byte/count and acknowledgement deadlines.
ACK/NAK expansion has an aggregate work budget. See OPERATIONS.md for limits.

Monster separation uses a spatial index while preserving the previous actor
processing order, including corrections that move a candidate between cells.
Resource and remote-visual modifier resolution use primed RAM catalogs; trap
definitions and immutable equipment appearance queries are cached. Leaderboard
pages share short-lived rankings, and chat logging uses a bounded disk writer.

Isolated Python 3.12 probes (not production throughput measurements):

| Work | Before | After |
| --- | --- | --- |
| Sparse crowd separation, 600 actors | 21.30 ms | 1.88 ms |
| Sparse crowd separation, 1,000 actors | 61.40 ms | 3.21 ms |
| Tiny state/profile durable save, median | 46.00 ms, two barriers | 29.54 ms, one barrier |

Regression checks cover collision parity, absence of SQL in resource resolution,
cache invalidation, queue capacities/order, nonblocking ACKs, stalled audit I/O,
authentication races, and atomic state/profile persistence. Earlier performance
investigations below describe their measured historical implementations.

The final combined selection of affected regression suites passed all 305 tests,
including trade persistence after outbound failure, initialization/retirement
ordering, and administrative mutation gates. Python compilation also passed.

The full regression run during this review reported 1,439 passed, 211 failed,
and 3 expected failures. Of the failures, 206 reproduced exactly with the
pre-change combat/static/resource/appearance methods restored in an isolated
process, and two safe-login failures also reproduced with the previous save
method. The remaining three assert historical map-prop packet counts or a
superseded stickerbook packet label. Those unrelated expectations were not
changed to make this performance review pass. These results are regression
evidence, not a production load test.

> **Implementation status.** Items 1-5 of Section 7 are done; only the
> allocation-free retransmission early-out in item 6 remains. Measured results:
>
> | change | effect |
> | --- | --- |
> | 64-bit interpreter guard | server refuses to start 32-bit; the ~930-thread hard ceiling is gone |
> | map-indexed presence | presence scan **6-20x faster**, now O(peers-in-map) instead of O(all sessions) |
> | frozen-dataclass deepcopy removal | ~13 us saved per move packet |
> | per-thread DB connections | repeat opens **643x faster** (64 us -> 0.1 us) |
> | per-client backpressure (§9) | one lagging player no longer freezes a whole map's monsters |
> | tick target selection (§9) | 40-player tick **5.7 ms -> 3.5 ms**, all of it inside the shared combat lock |
> | vicinity scans (§9) | `nearby_groups` **2.1x**, ~50 us/move packet on a 330-monster map |
> | account snapshot off the session lock (§11) | `flow_lock` held **6.9 ms -> 0.006 ms** per snapshot (~1.3% of the call) |
> | coalesced skill-resource saves (§11) | snapshots on the cast path **~5/s/player -> 0.5/s/player** |
> | memoized ActorFeature gates (§11) | `_player_is_untargetable` **0.087 ms -> 0.027 ms**; no per-packet text parsing |
> | one viewer sweep per monster tick (§11) | non-owner `flow_lock` acquisitions **11 -> 6** per tick per viewer |
> | player initialization off the receive thread (§13) | map entry no longer stops every client's datagrams for **136 ms-1.6 s** |
> | managed garbage collection (§13) | **70-100 ms** whole-process pauses every 10-20 s under load -> one scheduled walk per 5-15 min |
> | non-blocking console writer (§13) | a stalled `screen`/SSH console can no longer block a gameplay thread on `write` |
> | encounter cache 4 -> 12 maps (§13) | a live evening's seven maps stay resident; no 1-1.6 s rebuilds on re-entry |
> | 15% smaller generated population (§13) | 768 -> 650 on capped maps, 330 -> 280 on `b1201_pve` |
>
> The numbers below are the original measurements that motivated the work and
> are kept for context; the "before" figures no longer describe the server.

## 2026-09-09 combat packet-pipeline release gate

The receive thread now ACKs, validates, and enqueues all six gameplay skill
command families instead of executing combat inline. One fair scheduler slice
drains at most eight casts from a bounded 512-entry per-session FIFO. Both the
normal map service on UDP/2192 and the optional legacy route on UDP/2190 use
this path. Exact frame bit lengths and the map/combat generation travel with
each queued command; stale work is rejected after transfer, replacement, or
disconnect.

Outgoing reliable-ordered traffic is atomic: ordering-index allocation,
pending-reliable registration, and sendto are one transaction. Split messages
retain that invariant across all fragments and any trailing command.
Incoming traffic independently deduplicates outer datagrams and encapsulated
reliable indexes, reassembles split messages, and holds ordered messages until
their predecessor arrives. A retransmitted cast therefore executes once.

Repeated player-stat evaluation now uses a bounded structural cache keyed by
database revision, class/level, equipment/enchant/socket state, and active
modifier state. The same-loadout calculation fell from about 2.1 ms to about
0.08 ms. Multi-target socket/jewel procs are evaluated once per cast: the
15-hit/15-receiver case fell from 225 to 1 reliable packet, 15 to 1 living-set
reads, 225 to 15 distance checks, and 5.172 ms to 0.857 ms per cast.

The current crowded-map release gate uses the complete 768-monster
a0303_wildcave encounter, ten players, one cast/player/second, four victims
per cast, and a five-second measured window. The 2026-09-09 run delivered
200/200 impacts with no pending work, no frame over 40 ms, frame p95 19.74 ms,
callback-runtime p95 3.11 ms, and 51.0 packets/s (7.45 KiB/s) per client.

What caps concurrent players on this server, and what is worth fixing first.

Every number here was **measured on this machine against this codebase**, not
estimated. Where something could not be measured without a load test, it is
labelled as such rather than guessed.

Measurement environment: Python 3.12.12, MinGW, **32-bit**, Windows 11.
Costs are per operation, `timeit`, warm.

---

## 1. Summary

| limit | ceiling | status |
| --- | --- | --- |
| OS threads in-process | ~934 on 32-bit | **removed** — startup now requires 64-bit (§2) |
| Address space | 2 GB on 32-bit | **removed** — same change |
| Presence broadcast | was O(all sessions) per move | **fixed** — now O(peers-in-map), 6–20× faster (§4) |
| Packet processing | ~17 k pkt/s at N=1000 | improved but still one receive thread + GIL (§3) |
| Socket I/O | ~63 k pkt/s | never the bottleneck |

**The binding constraint is now CPU on the single receive thread, not threads
or address space.** Combat still allocates one OS thread per projectile /
summon / chase timer, but a 64-bit interpreter raises that ceiling by orders of
magnitude, so it is a cost rather than a wall until §7 item 4 lands.

**What the practical ceiling is, honestly: unknown.** The model in §4 covers the
movement path only, and every other subsystem shares the same thread. Treat any
single number as an upper bound, not a prediction — see §8.

---

## 2. The hard limit: 32-bit Python — **fixed**

`dso_server/__main__.py` now refuses to start on a 32-bit interpreter and
explains why, with `DSO_ALLOW_32BIT=1` as an escape hatch. Launch with
`py -3.12-64 -m dso_server`.

The machine's default `python` is still the 32-bit MinGW build, so this guard
is what stops the server silently coming up in a configuration that breaks
combat under load. Note the test suite must also be run under 64-bit
(`py -3.12-64 -m pytest`); the 32-bit build has no `pytest` installed anyway.

The original finding, which the guard is based on:

The server ran on `C:/msys64/mingw32/bin/python.exe` — 32-bit, `sys.maxsize`
`0x7fffffff`. Measured by allocating `threading.Timer` objects until failure:

```
32-bit (current):  failed at 934 live timer threads: can't start new thread
64-bit (py -3.12-64): 6000 timer threads, no failure (test cap, not a limit)
```

This originally mattered because high-frequency combat, summon and PvP
deadlines used `threading.Timer`, where every pending callback owned a native
thread. That failure mode was a `RuntimeError` which surfaced as combat
silently breaking under load. Those paths now use the bounded monotonic
`GameplayDeadlineScheduler`: one deadline heap and ten fixed map workers per
service. Only low-frequency lifecycle, chat, customization and character
selection timers remain as `threading.Timer` call sites.

All relative gameplay deadlines and measurements use `time.perf_counter()`.
On the installed 64-bit Anaconda runtime, `time.monotonic()` is backed by
`GetTickCount64()` and advances only every 15.625 ms, while `perf_counter()`
uses `QueryPerformanceCounter()` at sub-microsecond resolution. The old clock
made projectile impacts and worker-latency measurements jump in 15/16 ms
steps. Do not mix the two clocks in pending-hit, cooldown, expiry, scheduler,
transport-timeout, or benchmark state.

**A 64-bit interpreter is already on the machine** (`py -3.12-64`,
`C:\ProgramData\anaconda3\python.exe`), which is why the guard can simply
refuse rather than degrade.

The old timer cancel cost was **3.1 µs**; native thread count and address space,
not cancellation itself, were the limiting factors.

---

## 3. Structural limit: one receive thread

`RakNetServer.start()` creates exactly two threads per service:

- `<service>-receive` — `recvfrom()` then `_dispatch()` **synchronously**
- `<service>-retransmit` — 20 Hz scan of all sessions

So all packet handling for all players on a service is serialised on one
thread, and the GIL means adding threads would not help CPU-bound work anyway.

Raw socket throughput is *not* the constraint:

```
recvfrom only              63,424 pkt/s
recvfrom + sendto echo     71,959 pkt/s
```

The limit is what happens between the two.

---

## 4. The O(N²): presence on every move — **fixed**

`_handle_gameplay_move` → `gameplay_presence_callback(s, 'move')` →
`_handle_presence_event`, which per move packet did:

1. `_active_gameplay_sessions(map_id=…)` — a full scan of **every session on
   the server**, not just the map, filtered by 7 predicates
2. `copy.deepcopy(session.gameplay_last_move)`
3. a `math.hypot` distance test per surviving peer

Steps 1 and 2 are both addressed. Measured (40 maps, uniform distribution):

| sessions | before | after | speedup | peers returned |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 0.0050 ms | 0.0009 ms | 6× | 2 |
| 500 | 0.0210 ms | 0.0017 ms | 12× | 12 |
| 1 000 | 0.0410 ms | 0.0025 ms | 16× | 24 |
| 2 000 | 0.0660 ms | 0.0042 ms | 16× | 49 |
| 5 000 | 0.1850 ms | 0.0092 ms | 20× | 124 |

Only ~2.5% of the scanned sessions were ever relevant, so with N players each
sending moves the total work was **O(N²)**. It is now O(peers-in-map).

### How the index stays correct

`PresenceIdentityService._map_session_candidates` keeps `{map: [sessions]}`,
invalidated by the token `(len(sessions), Session.map_generation)`.

Two design points are load-bearing, both learned by breaking them first:

- **`Session.gameplay_map` is a property that bumps a class-level counter.**
  The first attempt updated the index at each assignment site instead, and
  broke immediately: not every service that assigns `gameplay_map` inherits
  this mixin (`LoginGameServer` does not). Hooking the attribute means the
  index cannot desync, including from tests that assign it directly.
- **Buckets are lists, not sets.** Callers depend on sessions arriving in
  `self.sessions` insertion order — presence announcements are asserted in that
  order — and a set scrambles it. This surfaced as a deterministic failure in
  `test_departed_map_presence_is_removed_and_reannounced_on_reentry`.

The index is only a *hint*: it may over-include, because the caller re-checks
`gameplay_map` in its predicate. It must never under-include, which is what the
invalidation token guarantees.

Taking ~15 µs of fixed per-packet cost (dominated by two deepcopies) plus the
scan, at 10 moves/sec/player:

| N | required | capacity | load |
| ---: | ---: | ---: | ---: |
| 500 | 5 000 pkt/s | 28 000 pkt/s | 18% |
| 1 000 | 10 000 pkt/s | 17 800 pkt/s | 56% |
| 1 400 | 14 000 pkt/s | 13 700 pkt/s | **~100%** |

**Caveat, stated plainly:** this models the movement path only. Combat, loot,
inventory and chat share the same thread and were not benchmarked end-to-end,
so the real ceiling is lower — likely materially. A load test is the only way
to get a trustworthy number, and this document does not substitute for one.

---

## 5. `copy.deepcopy` in the hot path — **fixed for `Move`**

The costliest single line measured:

```
copy.deepcopy(Move)     6.411 µs
copy.copy(Move)         1.668 µs
plain reference         0.005 µs
encode_move(...)        0.363 µs
```

**Deepcopying a `Move` costs 18× more than encoding the packet it came from.**

`Move` is declared `@dataclass(frozen=True)` with eight `int` fields. It is
immutable and contains no mutable members, so the deepcopy cannot be protecting
against anything — a direct reference is equivalent and ~1 280× cheaper.

The move path did this **twice** per packet (once in `_handle_gameplay_move`,
once in `_handle_presence_event`).

**All nine `gameplay_last_move` deepcopies are gone.** `gameplay_last_move` only
ever holds a `Move` or `None` — checked against every assignment site — so the
replacement is provably behaviour-preserving.

The other 77 sites were left alone: they copy genuinely mutable state
(`gameplay_inventory` 7, `gameplay_character` 6, `gameplay_talent_categories`
4), where the copy is doing real work. Removing those needs per-site analysis,
not a blanket sweep.

---

## 6. Secondary costs

**Retransmit loop.** Runs every 50 ms and calls `due_retransmissions()` on every
session, which does `list(self.pending_reliable.items())` — an allocation per
session per tick even when nothing is pending. At N=1000 that is 20 000
allocations/sec of pure overhead, on a thread contending for the GIL.

**Per-session DB connections — fixed.** `readonly_db.py` caches one read-only
connection **per thread** per database:

```
fresh connect + close (68 MB db)   64.0 µs
cached connect                      0.099 µs      643× faster
```

Per-thread rather than one shared connection: a single connection would need
`check_same_thread=False` plus external serialisation, and this server runs
timer threads alongside the receive thread. Per-thread gets the same saving
with none of that risk.

`ItemSetDatabase._connect` and the item-set proc resolver both use it. Note
`with connection` is sqlite3's *transaction* context manager — it commits, it
does not close — so existing `with self._connect() as db` callers work
unchanged against a cached connection.

> A first version keyed the cache on `Path(path).resolve()`. That touches the
> filesystem and measured **slower than simply reopening the database**, making
> the whole thing a no-op at 1× "speedup". Keying on the path as given is what
> makes it fast. Worth remembering before adding normalisation to a hot cache.

The queries themselves were never the problem — `_Template_Skill` has a PK
index on `Id` and a lookup is 12.8 µs. What remains is that
`LocalCombatState._player_skill_geometry` is still a per-instance cache, so
identical lookups repeat across encounters. That is now the cheaper half.

`LocalCombatState` *is* correctly shared per encounter rather than per player —
that part is already right.

**Session lookup.** `_announce_remote_presence` does
`peer not in stale_sessions` (tuple membership) inside a loop over peers — O(P²)
on a small P, so minor, but free to fix with a set.

---

## 7. What to do, in order

1. ~~**Run on 64-bit Python.**~~ **Done** -- `dso_server/__main__.py` now
   refuses to start on a 32-bit interpreter with an explanation, overridable
   via `DSO_ALLOW_32BIT=1`. Original reasoning: Removes the ~934-thread hard ceiling and the 2 GB
   cap. Already installed; no code change. *Do this first — everything else is
   moot while a `RuntimeError` can kill combat.*
2. ~~**Delete the `deepcopy` calls on frozen dataclasses**~~ **Done** for the
   nine `Move` sites (`Move` is `@dataclass(frozen=True)` with eight ints, so
   the copy protected nothing). The remaining 77 sites target genuinely mutable
   state and were left alone. Original reasoning:, starting with the two
   in the move path. ~13 µs per move packet, zero behaviour change.
3. ~~**Index sessions by map.**~~ **Done** -- see `_map_session_candidates`.
   Original reasoning: Keep `dict[map_id, set[Session]]` maintained on
   join/leave/transfer so `_active_gameplay_sessions` stops scanning the whole
   server. This is what turns O(N²) into O(N × peers-in-map) and is the single
   biggest algorithmic win.
4. ~~**Replace high-frequency `threading.Timer` callbacks with a bounded
   scheduler.**~~ **Done** -- `GameplayDeadlineScheduler` drives a monotonic
   `heapq` and ten fixed workers for map gameplay. Projectile, delayed impact,
   summon, trap, PvP and combat-resolution deadlines no longer create an OS
   thread each.
5. ~~**Share one read-only sqlite connection**~~ **Done** -- `readonly_db.py`
   caches one connection *per thread* (a single shared connection would need
   `check_same_thread=False` plus serialisation across service and scheduler
   workers). Original reasoning: process-wide and hoist the skill
   geometry cache out of `LocalCombatState`.
6. **Make `due_retransmissions` allocation-free** when nothing is pending —
   early-out if the pending dict is empty.

Items 1–3 are small, low-risk, and address the two hard limits plus the
dominant algorithmic cost. Item 4 is the real architectural fix.

---

## 8. Not measured

Stated so the numbers above are not over-read:

- **No end-to-end load test was run.** Everything here is component-level
  measurement plus a model. The §4 table is a movement-only ceiling and should
  be treated as an upper bound, not a prediction.
- **Loot, inventory and chat paths** were inspected but not benchmarked.
  Combat was, later, at the level that matters for a busy map — the 25 Hz
  simulation tick and its lock-hold time — in §9. It is still not benchmarked
  end to end.
- **Memory per session** was not measured; `Session` carries a large number of
  attributes and several per-player dicts, but on 64-bit this is unlikely to
  bind before CPU does.
- **Network egress** — broadcast fan-out multiplies outbound packets by peers
  in vicinity; bandwidth was not modelled.


---

## 9. Populated-map lag spikes — **fixed**

Reported symptom: on busy maps, hits do not register instantly and monsters
"glitch out and stop being updated". Three separate causes, only one of which
is a throughput problem.

Measured on `b1201_pve` (48 spawn groups, 330 monsters), 64-bit, warm.

### 9.1 One lagging player froze the map — the "monsters stop updating" bug

`_monster_chase_tick` began with:

```python
if s.reliable_backlog_size() >= RELIABLE_BACKLOG_SOFT_LIMIT:
    return True          # skip the whole tick
```

`s` is the session that happens to *own* the instance's simulation. Reliable
backpressure is a property of one client's link, but this stopped movement,
attacks, status ticks and regeneration for **everyone on the map**. It fires in
practice — production logs show `paused monster packet production at reliable
backlog 83`.

The per-viewer filter that belongs here already existed: the `monster_activity`
presence event drops the batch for each individually backlogged viewer. So the
tick now runs normally whenever a presence callback is installed, and the
owner-wide pause is kept only for the callback-less standalone path, where the
owner is the only viewer and the two are equivalent.

Guarded by `tests/test_populated_map_simulation.py`.

### 9.2 Target selection was O(players x monsters) *inside the combat lock*

`min(aggro_targets, key=target_distance)` read `combat.living_actor_ids` once
per candidate player, and that property walks the whole encounter and builds a
fresh tuple on every read — 45 reads per tick at 40 players. The scan runs
while holding both the owner's `flow_lock` and the shared `combat.lock`, which
is the same lock a player's own hit acquires (`player_combat.py`,
`combat_resolution.py`). Lock-hold time here *is* hit latency for everyone else
on the map.

The living set and the monster transforms do not change during selection —
monsters advance after a target is chosen — so both are hoisted once per tick.
Ranking also only needs an ordering, so it compares squared ground distance and
skips the `_distance` call and the square root.

| players | tick before | tick after |
| ---: | ---: | ---: |
| 10 | 2.7 ms | 2.0 ms |
| 40 | 5.7 ms | 3.5 ms |

`_distance` calls per tick fell from 15,510 to 2,310. At 25 Hz the 40-player
case drops from ~14% to ~9% lock occupancy.

The scan is still O(players x monsters) by construction. A spatial index over
monster positions would remove that, and is the next thing to do here if
100-player maps become a target.

### 9.3 Vicinity scans on every MoveCommand

Both per-move scans rebuilt a `set` from each group's `actor_ids` tuple — 48
allocations per packet, at 10 packets/second/player:

| scan | before | after |
| --- | ---: | ---: |
| `nearby_groups` | 76.7 us | 36.3 us |
| `_cull_distant_monster_groups` | 89.4 us | 79.5 us |

`MonsterSpawnGroup.actor_id_set` is now a `cached_property`, and
`nearby_groups` runs its one-hypot trigger-radius test *before* building any
set. The predicates are independent, so the reordering does not change which
groups survive — it just stops paying for the ones the player is nowhere near.
`_cull_distant_monster_groups` gains little because its cost is the distance
checks over already-announced actors, not the set building.

Whole-handler cost for one MoveCommand in this map, single player: **185 us ->
82 us**.

### 9.4 What this does *not* fix

- **Broadcast fan-out.** Each move is relayed to every vicinity peer, so a map
  with P players costs O(P^2) sends. At 40 players the per-packet handler cost
  is dominated by that fan-out, and my measurements there are noisy
  (180-320 us across runs) — treat the 40-player row as an order of magnitude,
  not a figure.
- **Gameplay deadlines.** The bounded `GameplayDeadlineScheduler` now uses
  ten fixed map workers instead of one native thread per projectile,
  summon or PvP impact.
- The sustained synthetic capacity run in Section 10 now covers this path.
  A live-socket load test with real ACK/NAK behavior is still outstanding.

---

## 10. Sustained ten-player packed-map combat

The component timings above did not answer the reported question: can a
packed local map keep its 25 Hz simulation and authored impact deadlines while
ten clients fight at once? The reproducible benchmark is:

```powershell
py -3.12-64 tools\benchmark_multiplayer_combat.py
```

Use `--profile` to collect cumulative hot functions, but do not use that run
for capacity percentiles because profiler overhead is measurable. The default
run intentionally exits nonzero while either the frame or deadline criteria
fail. The default now matches production's one-leader-handle-per-cast design.
`--legacy-per-impact-handles` explicitly recreates the retired design for
comparison.

### 10.1 Workload and boundary

- Python 3.12 64-bit, two `GameplayDeadlineScheduler` workers.
- `b1201_pve`, all 330 populated-spawn actors active.
- Ten independent sessions on one shared encounter, 25 monster frames/second.
- Two seconds warm-up, then 250 measured frames over ten seconds.
- Each player casts once/second. Every cast has four targets at one authored
  deadline and every impact carries a timed victim status effect. This is the
  expensive AOE/status path that the earlier 10x10 correctness test omitted.
- Encoded RakNet datagrams go through the real `Session.send` and presence
  fan-out paths into a thread-safe in-memory socket.
- The transport acknowledges every reliable datagram at the next sample.
  Packet sizes and reliable backlog are real; network delay, loss and
  retransmission are intentionally excluded. A miss therefore comes from
  server CPU/GIL/scheduler work, not localhost networking.

All players see all 330 actors, and all ten casts share a burst deadline. This
is a packed-map capacity case, not an average-vicinity traffic prediction.

### 10.2 Unprofiled production-path result

| metric | p50 | p95 | max |
| --- | ---: | ---: | ---: |
| monster-frame processing | 15.926 ms | 29.346 ms | 70.131 ms |
| frame start lateness | 0.374 ms | 7.911 ms | 62.566 ms |
| scheduler impact lateness | 58 ms | 137.750 ms | 183 ms |
| impact callback runtime | 16 ms | 62 ms | 78 ms |

- 7/250 frames (2.8%) exceeded the 40 ms frame budget.
- 400/400 authoritative impacts were dispatched; no impact remained pending.
- 100 leader handles were scheduled and 100 callbacks entered the workers.
  There was zero unexplained scheduler loss, zero callback error, and zero
  pending work at shutdown.
- Scheduler queue peak: 10 handles. Per-client reliable backlog peak: 50.
- Per client over ten seconds: 734 UDP datagrams and 76,522 bytes, or
  73.4 packets/s and 7.65 KB/s. Aggregate: 7,340 datagrams and 765,220 bytes.

The median monster frame fits, but the capacity result is a **failure**:
status-bearing impacts are p95 137.750 ms late, over three 40 ms ticks. This
explains local ping fluctuation and delayed hit text without any packet loss.
The world is not deadlocked; it drains every job, but critical work waits too
long behind CPU-bound fan-out.

### 10.3 Retired per-impact handle comparison

Before adoption, the same workload was measured both with the old handle for
every impact and with one leader handle retaining all four pending impacts.
The latter is now the production implementation and the benchmark default.

| metric | retired per-impact | coalesced, now production |
| --- | ---: | ---: |
| handles scheduled | 400 | 100 |
| scheduler queue peak | 40 | 10 |
| frame p50 / p95 / max | 14.424 / 25.726 / 50.274 ms | 4.420 / 6.687 / 26.433 ms |
| frames over 40 ms | 3 | 0 |
| impact lateness p50 / p95 / max | 59 / 121 / 168 ms | 43 / 74 / 74 ms |
| impacts dispatched / lost | 400 / 0 | 400 / 0 |

Coalescing is material and preserves all 400 impacts, but it is not sufficient:
ten simultaneous status-AOE callbacks still serialize through two workers.
The latest default production run above remained p95 137.750 ms late. The
difference between coalesced samples also shows that this workload is sensitive
to host CPU/GIL contention, matching the reported occasional spikes.

### 10.4 Profile: the next bottleneck

The profiled legacy-handle run (same workload; timings inflated by the
profiler) attributes cumulative work as follows. Handle coalescing removes
redundant scheduler entries but does not remove these fan-out costs:

| path | cumulative time |
| --- | ---: |
| `_monster_chase_tick` (250 calls) | 3.934 s |
| `advance_monsters` | 1.141 s |
| `maybe_start_monster_attacks` | 0.950 s |
| presence event fan-out | 1.545 s |
| combat batch encoding | 0.931 s |
| combat-resolution send path | 0.897 s |
| victim status batch encoding | 0.607 s |

The next tail-latency bottleneck is therefore not socket I/O. It is repeated
per-viewer combat/status serialization while two workers resolve a synchronized
cast burst, alongside the shared monster tick. Same-deadline leader scheduling
is now landed. Safe follow-up work should:

1. profile which viewer-invariant command fragments can be encoded once while
   preserving viewer actor remapping and per-viewer effect instance ids;
2. spatially partition active monsters before doing more micro-optimization
   inside `advance_monsters`.

The frozen `Move` held in `gameplay_last_move` is now referenced directly
while building monster `owner_moves`; it is not deep-copied per viewer.
`test_monster_tick_does_not_deepcopy_immutable_moves` prevents that allocation
from returning.

### 10.5 Player combat-stat cache check

```powershell
py -3.12-64 tools\benchmark_player_combat_stats.py
```

This now measures both the structural fallback and the production session
cache against the persisted level-100 mage (220 inventory rows, 23 equipped
items). A 5,000-hit run measured structural cache hits at p50 0.0795 ms / p95
0.1087 ms and session hits at p50 0.0014 ms / p95 0.0015 ms, about 57x faster
at p50. The session recorded 5,000 hits, one miss, and one equipment
observation.

The cache is bounded to 16 snapshots per connected character. Inventory is
copy-on-write, so a repeated hit uses object identity; a newly assigned
inventory is structurally observed once. Bag-only moves and current
HP/resource changes retain the equipped-stat generation. Equipment, gems,
runes, jewels, talents, stat-bearing effects, effect stack/value changes, and
database revisions invalidate or naturally change the structural key. The
database file revision is checked at most once per second per active character
instead of issuing `Path.stat()` for every projectile impact. Since 2026-10-25
that check runs on a background thread and casts and monster ticks use the
revision already loaded: one `stat()` on a volume spanning two hard disks took
three seconds and froze the cast and the map's simulation, both waiting on it.

### 10.6 Five-second combat-freeze forensic result (2026-09-09)

> **Superseded as a diagnosis by §10.7.** The status-effect lifecycle findings
> below are correct and the implementation is retained, but they were not the
> cause of the reported freeze and did not fix it. Read §10.7 first.

The exact client freezes were not a five-second Python lock. In the fresh
iceball reproduction the server completed 53 hit-producing casts and 303
impacts, recorded only one 60.3 ms dispatch, had no reliable backlog or
traceback, and continued normally through disconnect. During the same interval
the client logged 35 consecutive
`debuff_cc_frost_applyFreeze already present` warnings. Historical logs show
the same storm for charge, poison, and burn, which rules out a mage-only fault.

The original explanation that one instance id could be refreshed safely was
incorrect. Reversing the client handler and decoding all 509 status entries in
the retail mage capture established the actual lifecycle:

- `has_details=true` constructs a new effect; retail allocates a fresh instance
  id on every application and never sets `field_25` on those records;
- the zero-based running-resource occurrence in `field_18` advances
  monotonically and is not gated or clamped by `MaxStackSize` (retail advances
  it even for `MaxStackSize=1`); this is also what distinguishes a detailed
  replacement from the still-live occurrence it supersedes;
- a separate no-details record with `field_25=false` removes every superseded
  instance id, leaving only the newest replacement active;
- `field_20` is the attacking/causing actor, not the victim; and
- retail orders Hit, the associated StatusEffect transitions, and Kill in one
  mixed reliable 0x85 transaction.

The server had done the opposite: every detailed reapplication reused the
currently active id (therefore issuing duplicate ADDs), pinned stack counters
at their threshold, assigned the victim as causer, and placed a standalone
status packet in front of the Hit packet. Victim, socket/item-set, self, aura,
and entity senders now use fresh-id replacement plus no-details cleanup. Combat
delivery uses the captured Hit -> StatusEffect -> Kill transaction order.

The first implementation still reset `field_18` to zero whenever the static
database row declared `MaxStackSize=1`. That is not retail behavior: a fresh
instance id with the same running occurrence is rejected by the client as an
already-present actor effect, after which the valid old-instance cleanup makes
the buff disappear on the next hit. Both victim and player self-effect
refreshes now advance the occurrence for every still-running resource. The
accompanying counted-occurrence flag must also be set for every positive
occurrence, including `MaxStackSize=1`; retail does not gate that flag on the
database stack cap. Advancing the counter without the flag causes the client
to reject the replacement before the old-instance cleanup.

A follow-up sweep found the same mistake in remote spell playback: each cast
re-sent its caster's persistent talent-VFX selector as a detailed command with
the same instance id. Viewers now keep a bounded `(source actor, instance id)`
mirror, create each selector once, reconcile talent-generation replacements,
and clear it on removal, departure, actor recreation, and map initialization.
Socket unequip/conditional-stop, return-home, and retained collector-effect
transitions were normalized to the same create/remove contract as well.

The same trace also exposed an independent amplification bug: structural
`BulletImpactCount:3` from Frozen Master was multiplied by talent rank, turning
Ice Missile penetration from 5 into 14. Combined with three projectiles, one
cast could schedule 42 impacts. Bullet count and impact-count topology changes
are now applied once per active source; ordinary damage/range/timing modifiers
remain rank-scaled.

Lethal-hit profiling also found repeated full item/enchantment candidate scans.
Revision-keyed bounded caches preserve RNG and packet bytes while reducing a
warm forced boss drop from p50 11.635 ms to 0.243 ms (about 48x). The packed
10-player / 768-monster / 200-impact gate now completes 200/200 impacts with
zero pending work, zero frame over 40 ms, frame p95 18.641 ms, and callback
runtime p95 2.477 ms.

### 10.7 The freeze was the simulation worker's lifetime — **fixed**

Section 10.6 concluded the freeze was a status-effect instance-id fault. That
work was correct on its own terms and is retained, but it did not fix the
reported symptom, and it was not the cause. The status-effect layer only
decides packet contents; it never touches thread lifetime.

The actual cause is that the per-map simulation worker is a thread that
*exits*, and almost nothing restarts it.

`_FixedRateChaseLoop._run` breaks whenever `_monster_chase_tick` returns
false. Two of those returns are ordinary quiet moments rather than the end of
the map instance:

- the shared-state emptiness check, when no viewer has a spawned monster;
- a failed `claim_monster_world`, which previously could not distinguish "a
  peer owns this instance" from "nobody has anything to own".

Both fire the instant an AoE clears the last visible pack: the impact fan-out
discards each killed actor from every viewer's spawned set. The thread then
exited, and none of the four `_start_monster_chase` call sites were on the
casting or damage path. Recovery depended entirely on the client happening to
request a newly announced actor. In `logs/server-20260909_000000_sample_03.out.log`,
**14 of 14** chase restarts are immediately preceded by `fulfilled
ActorRequest`; nothing else ever restarted the simulation in that session.

That accounts for monsters neither moving nor attacking. The third symptom —
monsters taking no damage — came from the same event. `_start_monster_chase`
bumped `gameplay_chase_generation`, which is the map-incarnation token every
in-flight projectile, summon, trap, status tick and PvP cast revalidates
before it commits (for example `player_projectiles.py`, first flow-lock
guard). Retiring and restarting the worker therefore discarded every impact
already in flight, silently: those guards are plain returns, not exceptions.

Nothing logs, because none of it is an error. Four session logs contain zero
`LockAcquisitionTimeout`, zero `LockOrderViolation`, zero slow guarded waits,
zero reliable backpressure and zero tick failures. The 5-second
`DSO_LOCK_TIMEOUT_SECONDS` default is a coincidence; the freeze duration is
the announce -> ActorRequest -> fulfil round trip.

The fix separates idleness from retirement and ownership from worker identity:

- `_monster_chase_tick` returns `MONSTER_TICK_IDLE` instead of retiring when
  there is nothing to advance. Pending player hits and monster attacks count
  as work, so a worker cannot retire with authoritative work queued.
- The loop keeps the thread alive at `DSO_MONSTER_CHASE_IDLE_INTERVAL_SECONDS`
  (0.5 s) and retires only after `DSO_MONSTER_CHASE_IDLE_RETIRE_SECONDS`
  (30 s, zero disables) of continuous idleness.
- `claim_monster_world` returns `None` for "nobody can own this" and `False`
  for "a peer owns this", so only the latter retires the thread.
- `_start_monster_chase` no longer bumps `gameplay_chase_generation`. Genuine
  incarnation changes (map transfer, vicinity cull to empty, session
  retirement) still do. Worker identity is now enforced by
  `_FixedRateChaseLoop._is_installed_worker`, which is object identity rather
  than a generation comparison.
- Both cast paths re-arm the simulation owner after their locks are released,
  so recovery no longer depends on an `ActorRequest`.
- The vicinity cull raises `monster_visibility_changed` whenever it cancels
  the owner, not only when it discarded actors. Announced-but-never-spawned
  actors made those two conditions differ, leaving the map with no owner and
  no re-election.

Guarded by `tests/test_monster_simulation_lifetime.py`. Three of its cases
were confirmed to fail against the previous implementation and pass against
this one; the loop-level cases pin the new idle and identity behaviour. The
packed 10-player / 768-monster gate still passes: 400/400 impacts dispatched,
zero impact loss, zero pending work, zero frame over 40 ms, frame p95
7.746 ms, callback runtime p95 2.690 ms, zero callback errors.

### 10.8 Longer-term scale-out design (see also §11)

The current bounded-cache and batching implementation passes the local packed
map gate. The following steps remain the preferred path for larger production
instances rather than allowing per-player or per-actor work to grow linearly.

The next production steps are:

1. Build a persistent per-map replication graph: grid/room cells own cached
   actor lists, dormant/dead actors leave the hot list, and connections gather
   only their current and adjacent cells. The current announced/spawned sets
   are the first layer, but map-wide reconciliation and repeated per-viewer
   filtering should be replaced by dirty cell snapshots.
2. Split simulation snapshots from serialization. Resolve combat once into an
   immutable map event, then reuse viewer-invariant Hit/Kill command fragments.
   Only local/remote actor remapping and per-connection status-effect instance
   bookkeeping should remain viewer-specific.
3. Keep critical impact/death work ahead of replaceable movement and ambient
   AI publication. Movement is coalesced, with reliable terminal stops (see section 12); authoritative
   damage, status transitions, and deaths remain reliable and ordered.
4. Own each dungeon instance in one dedicated worker process and route new
   instances through the gateway. This isolates stalls and uses multiple CPU
   cores; CPython threads cannot execute CPU-bound Python in parallel because
   of the GIL. Do not put the mutable map itself in a generic process pool:
   transfer compact commands/events across the process boundary and keep one
   authoritative owner.
5. Gate releases on a real-socket soak in addition to this immediate-ACK
   benchmark: zero lost impacts, zero callback errors, zero residual work,
   frame p95 below 40 ms, and impact-lateness p95 below 40 ms.

These choices follow the same principles as Epic's Replication Graph: retain
persistent actor lists, partition actors spatially, and share work between
connections rather than asking every actor about every connection each frame:
https://dev.epicgames.com/documentation/en-us/unreal-engine/replication-graph-in-unreal-engine

For CPU scale-out, Python's own guidance is to use multiprocessing or process
executors for CPU-bound work rather than expecting threads to bypass the GIL:
https://docs.python.org/3.12/library/threading.html

At deployment scale, an allocator/fleet layer can place each authoritative
instance in a separate game-server process and select by available room/player
capacity. Agones documents this allocation model and its capacity selectors:
https://agones.dev/site/docs/reference/gameserverallocation/

---

## 11. Session-lock contention on the combat hot path — **fixed**

Reported symptom, again: hits do not register instantly, and monsters
occasionally act on a stale player position as though the AI loop had crashed.
Section 10.7 fixed the case where the simulation thread genuinely *exited*.
This section is the remaining case, where every thread is alive and the
simulation heartbeat is a healthy 25 ticks/s, but work is queued behind one
lock.

### 11.1 Why one player's session lock is a whole-map problem

`Session.flow_lock` is non-reentrant and is taken by all of:

- the map's **single** 25 Hz simulation worker, once per viewer per tick, to
  read that viewer's spawned set, aura state and combat gates
  (`monster_combat.py`, `monster_tick_context.py`);
- every projectile deadline worker, to validate and commit an impact
  (`player_projectiles.py:168`, `:265`, `:286`);
- the movement/skill ingress workers, to apply the authoritative position
  (`world.py:222`);
- presence fan-out, to read `gameplay_last_move` per viewer.

So a long hold by *anything* on one player's session simultaneously delays
that player's hits, that player's movement, and — because the map worker
serializes over every viewer — the entire map's next simulation tick. This is
the mechanism behind all three reported symptoms, and it gets strictly worse
as a map fills up.

Production already proved it happens. From
`logs/server_20260909_000000_sample_04.stdout.log`:

```
[Concurrency] slow successful wait for session_flow:session[127.0.0.1:49973]:
  54.3ms; requester='DrasaCharacterService-receive';
  previous_owner='DrasaCharacterService-gameplay-deadline-7'
```

The UDP receive thread waited 54 ms for a session lock held by a deadline
worker. Nothing else in that log is an error.

Note the concurrency guards do not catch this: `assert_no_state_locks_held`
(`concurrency.py:465`) rejects socket/DB I/O under a state lock, but pure-CPU
work like `copy.deepcopy` is invisible to it.

### 11.2 The account snapshot deep-copied the account under that lock

`_capture_account_state` held `flow_lock` across five `copy.deepcopy` calls.
Measured against the persisted level-100 characters:

| operation | items | p50 | p95 |
| --- | ---: | ---: | ---: |
| `deepcopy(inventory)` | 251 | 5.61 ms | 6.49 ms |
| `deepcopy(inventory)` | 178 | 5.30 ms | 6.44 ms |
| whole `_capture_account_state` | 251 | 6.94 ms | 9.05 ms |

Two changes, neither of which weakens the snapshot:

- **`clone_inventory_info`** (`inventory_codec.py`) replaces the generic
  deepcopy. `ItemInfo` has exactly two mutable members and both hold immutable
  elements, so a shallow copy plus rebuilt containers is observationally
  identical: **6.55 ms → 0.44 ms (~15x)**.
- The critical section is now reference capture plus small container copies;
  the two deep structures are cloned **after** the lock is released. That is
  safe because both are copy-on-write — `gameplay_inventory` is *replaced* for
  every structural change (`InventoryMutationBuilder`, `equip_inventory_item`,
  `store_inventory_item` all return new objects), and the only in-place writes
  are the scalar health/resource fields that `_sync_inventory_live_health`
  overwrites from authoritative combat state anyway.

| `_capture_account_state` | before | after |
| --- | ---: | ---: |
| total | 6.94 ms | 0.49 ms |
| **held on `flow_lock`** | ~6.9 ms | **0.006 ms** (1.3% of the call) |

### 11.3 Combat raised that snapshot once per cast

`player_resources.py:207` fires `_request_account_save(s, "skill resource
changed")` for every skill cast. In the captured session that was **627 of 669
saves**, peaking at 6/second — roughly 42 ms/second of session lock per player,
against a 40 ms tick budget.

Skill resource is regenerating state: it is recomputed from live combat on the
next tick and bounded by its own maximum. `COALESCED_ACCOUNT_SAVE_REASONS`
(`context.py`) now rate limits exactly that reason to
`DSO_COALESCED_ACCOUNT_SAVE_SECONDS` (default 2 s). Nothing durable is
coalesced, `flush=True` bypasses it entirely, and the 15 s autosave plus every
lifecycle boundary (map transfer, disconnect, retirement) still capture the
live value.

### 11.4 The ActorFeature victim gates re-parsed content text per packet

`_player_victim_feature_locked` walked every active modifier source and did
`str().split(";")` + `partition` + `casefold` on raw `StartModifiers` text,
with no caching. It is called twice at the head of every `MoveCommand`
(`world.py:207`, `:212`), twice per cast, and — via
`_prepare_victim_feature_gates` — three times per candidate viewer on every
25 Hz tick.

Three layers now answer it:

- `parse_disabled_actor_features` (`local_combat_state.py`) is a pure function
  of the program string and is `lru_cache`d process-wide;
- `LocalCombatState.status_effect_disabled_features` memoizes per effect id;
- the per-player union is cached on the session against the sorted live
  effect-id tuple, which changes on exactly the events that change the answer.

`_prepare_victim_feature_gates` also derives both gates from **one** set per
session instead of asking three independent questions.

Separately, the gate path no longer builds the full modifier-source tuple at
all. It only ever needed effect ids, but it was materializing every
persistent/item-set/socket/runtime entry plus the item-set modifier list and a
`parse_socket_modifier` call per socket cast rule — none of which carry an
`effect_id`. `_active_status_effect_ids` walks ids directly and shares the
expiry sweep with the full build.

| call (level-100 loadout, 45 effects, 21 socket rules) | before | after |
| --- | ---: | ---: |
| `_player_victim_feature_locked("movement")` | 0.043 ms | **0.012 ms** |
| `_player_is_untargetable` | 0.087 ms | **0.027 ms** |

### 11.5 The monster tick swept every viewer's lock twice

Visibility and periodic socket auras were two separate passes over the same
sessions, and the victim gates added more. `_snapshot_monster_tick_viewers`
merges them into one critical section per viewer. Measured on the
five-viewer fixture, acquisitions in a single tick:

| | before | after |
| --- | ---: | ---: |
| non-owner viewer | 11 | **6** |
| owner | 16 | 11 |

The owner legitimately does more: it also carries the tick clock
snapshot/commit and presence fan-out for the whole map.

That merge also fixed a latent bug: a departed socket's periodic auras used to
keep a map out of its idle state with no viewer left to receive the ticks. The
liveness test now gates the aura contribution too.

### 11.6 Result and what is still open

`tools/benchmark_multiplayer_combat.py`, `a0303_wildcave`, all 768 actors,
10 s measured, immediate-ACK transport:

| players | frame p50 | frame p95 | frames over 40 ms |
| ---: | ---: | ---: | ---: |
| 1 | 7.9 ms | 10.3 ms | 0 |
| 10 | 11.5 ms | 14.3 ms | 0 |
| 40 | 15.3 ms | 36.7 ms | 12 / 250 |

Guarded by `tests/test_session_lock_hot_paths.py` (all four cases confirmed to
fail against the previous implementation via a temporary re-introduction
harness) and `tests/test_populated_map_simulation.py::
test_tick_takes_a_bounded_number_of_locks_per_viewer`.

**Still open, honestly.** The 40-player packed case is not fixed by this work
and was not expected to be: its p95 tick lateness is still hundreds of
milliseconds, and profiling now attributes the time to viewer fan-out
(`_send_combat_resolutions`, `_victim_effect_command_transactions`) and to
`advance_monsters`/`maybe_start_monster_attacks` inside `combat.lock`, which
are O(active monsters) — 2.3 ms and 2.0 ms respectively at 768 active actors,
i.e. ~107% of one 25 Hz budget on their own. Section 10.8 items 1 and 2 are
the fix for that, and they are structural, not another micro-optimization.



## 12. Monster AI and delivery corrections (2026-09-09)

The map tick now assigns a target per monster using that player's spawned
visibility set and living health state. It retains a current target within a
small distance margin to prevent repeated switching. Movement and attack
selection use the same assignments in one ordinary-monster pass; taunts keep
their existing precedence. The old single nearest player for the entire map
could divert a distant pack from the player standing next to it.

A* remains budgeted at two searches per movement pass. Actors rotate through
that budget, and an existing route remains usable while its refresh is deferred.
Repeated visibility reconciliation no longer resets an already-active actor's
publication history. Player movement cannot rewind the simulation publication
clock. These changes prevent avoidable path stalls and irregular publication.

The four-second return-home recovery window now measures time without progress,
rather than total time walking home. A valid longer route does not trigger a
teleport. A stalled actor still resets, and the outer hard leash remains. A
visibility pause retains the return state and pauses its recovery timer; a later
spawn restores the return-home status. Health is refilled upon reaching home.
Vicinity culling no longer changes the map-generation token used by pending
skills solely because it retires an idle simulation worker.

The first delivery revision put all walking snapshots on the reliable ordered
creation/attack stream. That introduced a regression: one lost movement packet
blocked later attacks, and unacknowledged walking filled the 32-datagram soft
limit. Coalescing only unsent state did not bound the already-submitted history.
The immediate-ACK benchmark failed to exercise that condition.

Walking snapshots now use unreliable-sequenced delivery again. Lost/superseded
walking samples are repaired by subsequent samples rather than retransmitting
obsolete positions. Final stops use reliable ordered delivery because an idle
actor may produce no later sample. Mixed batches are partitioned so a stopping
actor does not promote other actors' walking to reliable delivery. The publisher
retains only the latest unsent transform per actor during outbound contention or
backpressure, and retries final stops even without new movement. Reliable attacks
and repairs supersede older unsent transforms. Discarded actors leave the deferred
set. Spawn visibility is published only after NewMonster is submitted.

`tests/test_monster_ai_delivery.py` covers loss/reordering with retransmission,
congestion recovery, final stops, creation order, route budgeting, long returns,
visibility pauses, target selection, and clock regression. Existing populated-map
checks still constrain session-lock acquisitions and independent viewer progress.


Validation for this change: 257 focused AI, transport, concurrency, combat,
PvP, minion and summon tests plus 12 server integration tests passed. The broader
architecture suite reported its existing service-size limit failure in the
unchanged `combat_resolution.py` and `player_init_state.py` modules. Live-client
animation and real-link latency have not been validated by these tests.


### 12.1 Freeze regression verification

`tests/test_monster_movement_loss.py` reproduces the old failure before the fix:
a missing walking packet blocks the next attack, and continuous walking without
ACKs stops publishing after 32 packets. With the fix, 250 walking samples add no
reliable backlog/order slots, attacks bypass lost walking, and lost terminal
stops remain retransmittable. Tests also cover mixed batches, stale movement
following a stop, coalesced congestion recovery, and connected-pong framing.

The benchmark now accepts `--ack-delay-ms` and `--lose-first-reliable-every`;
the latter withholds selected first-attempt ACKs and exercises normal retries.
The recorded run used `a0303_wildcave`, all 768 actors active, one player, 80 ms
ACK delay, every fifth datagram's first reliable attempt unacknowledged, and ten
measured seconds. It continued moving and attacking at 25 Hz: p95 tick processing
12.602 ms, zero frames over 40 ms, reliable backlog peak 7, zero callback errors,
and 40/40 scheduled impacts dispatched. Full result: `monster_delivery_load_test.json`.
This is an in-memory server load test, not a measurement of live-client ping or
rendering. Packet-order/loss behavior is checked separately by the tests above.

After the correction, 275 focused tests and 12 server integration tests passed.


### 12.2 Continuing freeze investigation (2026-09-09)

The live ExamplePlayer Caverns freeze persists after the delivery changes above.
The player can still move and damage monsters while autonomous monster activity
stops; console heartbeat output may stop too. No live stack capture was available,
so the earlier transport benchmarks do **not** establish the cause of this freeze.

Three additional failure cases were reproduced in worker regression tests and fixed:

- The ownership check sat outside the tick exception handler. A session-lock
  timeout there terminated the map worker instead of retrying.
- An installed handle to an exited thread prevented `_start_monster_chase` from
  recovering the worker. Recovery now distinguishes an exited thread from a live
  thread or a slot reserved before thread startup. It preserves map generation.
- Restarting copied the player's last movement tick unconditionally. A stationary
  player's old packet could rewind simulation time. Restart now accepts only a
  forward client tick, including uint32 wraparound.

The application watchdog now samples map workers independently of its own thread
scheduling. It writes `dso_server/data/diagnostics/monster-stalls.log` directly,
without console output or session, combat, pending-packet, or outbound locks.
Worker stalls of at least one second include Python thread stacks, current tick
phase, progress counters, visibility/target state, reliable backlog and oldest
packet age. Stacks are rate limited to one report per worker per ten seconds.
Healthy workers are sampled every ten seconds; worker exits are recorded even
after the session clears its handle. The log rotates at 4 MiB and retains one
previous file. Existing `DSO_STALL_WATCHDOG_SECONDS=0` disables this monitoring too.

Regression coverage includes an actually blocked worker while the test holds its
session/outbound/pending locks and makes console output unavailable. The watchdog
captures the blocked callback in the file before the worker is released. No live
blocked worker is replaced automatically: that could run the map twice.

A separate 30-second in-memory check exercised the real background worker and
300 production Move requests plus 47 ActorRequests in `a0303_wildcave` (768 map
monsters). The player followed a projected circle, with 7?24 visible monsters,
one four-target cast per second, 80 ms simulated ACK delay and loss of every fifth
reliable packet's first transmission. Worker samples advanced at 25 ticks/s;
120/120 impacts resolved, no callback errors or watchdog stalls were recorded,
and reliable backlog peaked at 22. Move/ActorRequest handling had p95 1.417 ms,
maximum 4.639 ms. Details: `monster_worker_load_check.json`. This run used an
in-memory datagram sink and captured console, so it does not test client rendering,
actual RakNet reception, or the user's console environment. The live freeze still
needs the new diagnostic log from a reproduction.


### 12.3 Live lag and late damage (2026-09-09)

The next live report had no complete freezes, but monster updates fell as low as
4.9 ticks/s and spell inputs spent up to 794 ms queued. The pasted traceback
identified runtime buff expiry acquiring `map_combat` while a cast held
`session_flow`. The diagnostic file also showed clock lead increasing to 44 ticks
against recently received player movements (1.76 seconds at 25 Hz). Reliable
backlog in the reported slow-send samples was only 1?3 packets.

The fixes address these measured/reproduced paths:

- Monster time is derived from a monotonic wall-clock anchor rather than rounding
  each loop interval and forcing a minimum one-tick advance. Delayed/early tick
  pairs previously created extra ticks. The movement integration cap remains
  separate from clock time; uint32 wraparound, incoming clock advances and map
  generations are covered by tests.
- Bullet and spatial cast preparation snapshots session state, releases the lock
  for effect/stat/content calculations, then validates the session incarnation.
  Expiry rechecks leases under the session lock and reconciles vitals after release.
  Status changes preserve the equipment cache; a duplicate socket-vitals refresh
  was removed. Starting an already-installed map worker skips the presence query.
- Authored impact handles now retain absolute deadlines. Previously a 200 ms
  flight registered at time 100.0 and started after 150 ms of cast preparation
  executed at 100.35, rather than 100.2. PvE and PvP impacts now use their original
  due times; ordinary relative timers retain their existing semantics.
- Replaying the saved character's loadout uncovered a separate socket-minion
  failure: `SocketMinionDefinition` lacked `startup_duration_seconds`. A snow-wolf
  proc repeatedly threw from `advance_player_summons`, interrupting every map tick
  until the minion expired. Socket minions now provide the startup field and use
  the same deployment-duration calculation as skill-created minions.

`py -3.12 tools/benchmark_combat_latency.py` replays the saved Example Player 2 loadout
from the player database in read-only mode; all gameplay mutations stay in memory.
It uses 228 inventory items plus saved talents, set and socket effects, a real
background worker, production Move/BulletSkill handlers, and 100 iceball casts at
five casts/s. To ensure deterministic coverage of the failure, it invokes the
native snow-wolf socket-effect spawn path three seconds into the run. The map has
768 authored monsters, with 12 initially visible. Targets have enlarged health
pools so combat remains active throughout the test. The transport is an in-memory
datagram sink with acknowledgements each frame, and console output is captured.

The final replay had no worker/command errors, sustained 25 ticks/s and zero clock
lead in its one-second samples. Cast processing was p95 20.170 ms (maximum 57.365 ms,
including cold setup); impact callback lateness was p95 14.852 ms, maximum 21.949 ms
across 109 callbacks. These measure server processing and scheduling, not the
client's rendered hit timing. The complete report is
`monster_cast_latency_check.json`; the live client still needs retesting.


## 13. Ping spikes with several players on a populated map (2026-09-09)

Reported symptom: large ping spikes for everyone whenever several players
fight on a map with many monsters. Measured on this host (Linux, Python
3.12.14, 12 cores) with `tools/benchmark_populated_map.py`, a load harness
built from the test fixtures: the real `CharacterServer` composition, the real 25 Hz map worker, the real
ingress and deadline schedulers, an in-memory socket whose "client"
acknowledges every reliable datagram 60 ms later, N players standing in one
`a0303_wildcave` pack, each moving at 10 Hz through the ingress queue and
landing one four-target cast per second through the deadline workers. A
"receive-thread proxy" sleeps 5 ms in a loop and records how late it wakes,
which is exactly what a connected-ping reply suffers.

### 13.1 What the live diagnostics said

`data/diagnostics/monster-stalls.log` for 2026-09-09 held seven
whole-process stalls of 1.0-3.6 s. All 33 thread stacks sampled on recovery
were idle, no map worker existed at the time (no `MonsterWatchdog` samples in
that window), the chat log shows nobody online, and the host journal shows
only SSH brute-force noise. `/proc/stat` reports 43 s of hypervisor steal
since boot. Those stalls were not server work. The watchdog could not say so,
because it recorded only wall time; it now records process CPU time, host
steal and the longest collector pause across the stall and prints a verdict
(§13.4).

The same log's per-worker samples show healthy maps: p50 phase ages of a few
ms, 25 ticks/s, reliable backlog peaks under the soft limit.

### 13.2 Steady-state load is fine; two things are not

Grouped players, all seeing the same 36-43 monsters, 20 s runs:

| players | tick p50 / p95 / max | wake p95 / p99 / max | gen-2 GC pauses | per client |
| ---: | --- | --- | --- | --- |
| 1 | 4.1 / 7.7 / 21.0 ms | 4.0 / 5.2 / **80.1** ms | 1 x 86 ms | 59 pkt/s, 6.9 KiB/s |
| 5 | 4.5 / 7.8 / 21.7 ms | 3.9 / 5.5 / **90.7** ms | 2, 90.6 ms max | 126 pkt/s, 10.4 KiB/s |
| 10 | 5.7 / 9.9 / 25.7 ms | 4.6 / 7.5 / **96.7** ms | 1 x 96.6 ms | 180 pkt/s, 12.7 KiB/s |
| 10, heap frozen | 5.8 / 10.0 / 24.2 ms | 4.6 / 6.7 / **21.4** ms | none | 180 pkt/s |

The tick scales gently (fan-out is 7% of worker time at ten players) and the
p95 wake latency sits at the 5 ms GIL switch interval regardless of player
count. Every worst-case wake in the default runs coincides with a
generation-2 collection, and freezing the heap removes them: max wake
96.7 -> 21.4 ms, impact lateness max 49.5 -> 4.6 ms. A 1 ms switch interval
lowers wake p95 to 1.8 ms but raises tick p50 by 25%; it was not adopted.

**Player initialization on the receive thread.** `_send_local_player_
initialization` ran inline from the 0x006f handler on the service's only
socket-reading thread:

| case | cost |
| --- | ---: |
| town (`a0200_kingscity`), warm caches | 97-111 ms |
| dense map, encounter cached | 122-173 ms |
| dense map, encounter must be generated | **1306-1566 ms** |
| first initialization after process start | 634 ms |

Half of the warm cost is 40-46 SQLite queries; the cold cost is the 768-actor
population build. During that time the server read and acknowledged no
datagram from *any* client, so every player's ping rose by the full amount
each time anyone entered a map. The encounter cache held four maps; the
2026-09-09 evening used seven, so re-entries rebuilt populations. This is the
"massive" spike, and it scales with how many players are entering, dying and
travelling, and with how dense the destination is.

**Full garbage collections.** CPython's generation-2 collection walks every
tracked object with the GIL held. With four populations cached that walk is
70-100 ms (`gc.collect()` measured 86-101 ms at 410 k tracked objects), it
fired every 10-20 s under load, and nothing in the process runs while it does.

Cleared suspects, each measured: `ActorRequest` handling (0.37 ms each,
16.5 ms for a 40-actor pack, inline on the receive thread), A* routing
(p50 0.1-0.8 ms, p95 under 2.6 ms on the four densest meshes), movement
handling on the ingress workers (p50 1.0 ms, p95 1.7 ms at ten players),
impact lateness (p95 under 2 ms), and reliable backlog (peaks 22-28 of 32 at
a 60 ms ACK delay).

### 13.3 Fixes

1. **Initialization runs on its own worker.** `_schedule_local_player_
   initialization` hands the 0x006f work to a one-thread
   `GameplayDeadlineScheduler` (`DSO_GAMEPLAY_INITIALIZATION_WORKERS`, default
   1) on both the normal UDP/2192 route and the legacy 2190 route. One worker
   keeps initializations serialized exactly as before; the receive thread
   keeps answering pings and ACKs. The delayed 0x8d fallback already ran the
   same function from a timer thread, so the work was never bound to the
   receive thread. A session replaced before its turn is skipped, and an
   initialization that waited more than 0.5 s behind earlier entries is
   logged. Measured after the change: the 0x006f handler holds the receive
   thread for **0.15-0.37 ms** while the initialization itself takes 89 ms
   (dense map, cached), 594 ms (town, first entry) or 1153 ms (dense map,
   population generated) on the worker. Guarded by
   `tests/test_receive_thread_offload.py`.
2. **Managed garbage collection** (`runtime/gc_tuning.py`, on by default,
   `DSO_GC_POLICY=default` restores CPython's behaviour). At start the heap
   is frozen and automatic generation-2 collections are disabled; young
   generations keep their default cadence. A maintenance thread runs a
   collection every `DSO_GC_FULL_COLLECTION_SECONDS` (300), waiting up to
   `DSO_GC_MAX_DEFERRAL_SECONDS` (900) for every map worker to go idle first.
   Generated map populations are frozen as they are cached, so the ordinary
   managed collection walks only session state; every
   `DSO_GC_UNFREEZE_EVERY_CYCLES` (6) cycles the whole heap is walked and
   re-frozen so cycles among long-lived objects are still reclaimed. The
   ten-player run with this policy (interval shortened to 8 s to exercise it)
   showed the managed collection deferring while maps were busy and running
   once at the bound. Guarded by `tests/test_gc_tuning.py`.
3. **Non-blocking console writer.** `TimestampedStream` now queues completed
   lines to one writer thread (`DSO_LOG_ASYNC`, default on; queue bound
   `DSO_LOG_QUEUE_LINES`, default 20 000). Production prints to a `screen`
   pty read over SSH; when that stops draining, `write` blocked whichever
   thread printed under one lock, including the receive thread and the map
   worker. A full queue drops the newest line and reports the count when the
   console recovers. Guarded by `tests/test_log_stream.py`.
4. **Stall attribution.** A `[StallWatchdog] process stalled` line now ends
   with process CPU during the stall, host steal, the longest collector pause
   and a verdict: collector pause, GIL-bound work, or "the process was mostly
   not running" (host/VM scheduling). Guarded by
   `tests/test_stall_attribution.py`.
5. **Encounter cache 4 -> 12** (`DSO_MONSTER_ENCOUNTER_CACHE_SIZE`). Each
   cached population costs 30-40 MiB and a miss costs 1-1.6 s of GIL-bound
   generation that an entering player may wait on.
6. **Population trimmed about 15%** (`DSO_SPAWN_AREA_DENSITY_SCALE` 0.85 on
   every spawn area's authored density, `DSO_SPAWN_AREA_MAX_TOTAL_MONSTERS`
   768 -> 650). Hand-placed monsters and boss rooms are unchanged.

| map | before | after |
| --- | ---: | ---: |
| a0303_wildcave, a0203_kingscrypt02, a0302_wildforest, a0406_dun, a0301_witchhole | 768 | 650 |
| pw203_norseland_02_dun | 489 | 417 |
| b1202_dun | 528 | 448 |
| b1201_pve | 330 | 280 |
| a0001_start_tutorial_dun (fixed only) | 16 | 16 |

### 13.4 Still open, honestly

- The seven idle-server stalls on 2026-09-09 are consistent with hypervisor
  pauses but not proven; the attribution line will settle the next one.
- Real links were not measured. At a 150 ms RTT ten players' reliable
  backlog would cross the 32-datagram soft limit that this benchmark's 60 ms
  ACK delay kept at 22-28, and the fixed 250 ms retransmit interval has no
  RTT estimate, so a far client re-sends packets whose ACK is merely late.
- The warm initialization itself (about 100-170 ms, half of it SQLite) is
  now off the hot path but not faster; with one worker, three players
  entering together still initialize one after another.

## 14. Maps left without a simulation thread (2026-10-21)

Reported: monsters stop moving and attacking while the rest of the game --
map travel, chat, casting -- keeps working.

`monster-stalls.log` held 85 server runs (2026-09-09 to 2026-10-21): 1,172
worker samples, 118 worker exits and no worker that stopped progressing.
Every sample showed a single combat target, so none of those sessions had two
players on one map, and the log cannot show a map that has no worker at all.
Three ways to stop the monsters without stalling any thread were found.

- **The ownership rules disagreed.** `claim_monster_world` made every session
  on the instance eligible to own its simulation once any player summon
  existed; the re-elections on a visibility change and on departure ignored
  summons. With player A (lower character id) away from the monsters and B
  fighting, B's worker lost its claim to A, re-elected B, was refused by its
  own claim check and retired. B's ActorRequests, casts and summons were
  refused the same way, so the map stayed frozen until A acted or the summon
  was gone. Reproduced with the real worker. One election,
  `_monster_world_owner`, now serves all three paths and skips departed and
  disconnected sessions.
- **A start request could be lost.** `_start_monster_chase` refuses while a
  worker is installed, including one that has already decided to retire
  (idle timeout or hand-off) but has not released its slot yet. The request
  is now recorded under `flow_lock`, and the worker checks it under the same
  lock before it releases the slot.
- **Oversized reliable datagrams.** The `monster-skill` batch has no size
  cap: about 15 monsters starting an attack in one tick exceed the
  1,232-byte budget, 20 reach about 1.6 KB. The client drops such a datagram
  without acknowledging it (GAME_EVENTS.md), so it is retried forever and the
  client's ordered stream stops behind it, and a client that keeps sending is
  never timed out. `Session.send_reliable_ordered` now fragments any message
  whose datagram would exceed `RELIABLE_DATAGRAM_LIMIT`, the split-fragment
  size. A 15-hit player-owned DoT batch in the test suite (1,239 bytes) was
  one such message. The log also holds one instance of the symptom: a
  `prop-info-response` left unacknowledged for 108 s on 2026-09-28 while its
  client kept sending; its size was not recorded.

As a backstop the stall watchdog runs a supervisor once a second. An instance
whose players have spawned monsters, summons or regeneration but no live
simulation thread for `DSO_MONSTER_ORPHAN_GRACE_SECONDS` gets one started for
its elected owner, reported on the console and in `monster-stalls.log`. The
retransmit loop reports a reliable frame that a talking client leaves
unacknowledged for `DSO_RELIABLE_STUCK_WARNING_SECONDS`.

Still open: reliable sends from the map worker still wait on a viewer's
`outbound_lock` without a timeout, and an exception in a tick's publishing
phase still drops that tick's one-shot packets (attack starts, kills,
return-home transitions) for every viewer.

## 15. Datagrams the client never accepts or delivers (2026-10-24)

Reported: a player's world stops -- monsters, hits and replies -- while the
server keeps running and the client never acknowledges what it is sent.

A capture against the official servers (2026-07-20, outside the repo) pins
the client's side of the budget. The client offers MTU 1292 (a 1,264-byte
OpenConnectionRequest1) and the official server never sent a datagram over
1,256 bytes; the client acknowledged all 1,474 datagrams of 1,255-1,256
bytes. A 1,304-byte datagram was never acknowledged (ADMIN_CMS.md).
`RELIABLE_DATAGRAM_LIMIT` (1,232) stays the budget, and the character
service's 1,256-byte fragments match the capture. Paths that bypassed the
budget, or broke the ordered stream another way:

- **Multi-frame datagrams had no size check.** `send_multi_reliable_ordered`
  put every frame in one datagram: a character list of up to 1,232 bytes
  plus cmd 0x001b made up to 1,268 bytes. A batch that does not fit now goes
  through the oversized path, packed in order into as few datagrams as fit.
- **Trailing frames rode on a full final fragment.** Cmd 0x001b behind a
  character list whose last fragment is nearly full made up to 1,278 bytes.
  A trailer that does not fit now follows in its own datagram; the live
  rosters' ~2.2 KB lists still carry it in the last fragment, as captured.
- **Failed framing burned ordering indexes.** A batch whose second payload
  failed validation, or exceeded the frame's 16-bit bit length, kept the
  index its first frame had taken; a split whose trailer failed left its
  fragments without the last one. Either is a gap the client waits on
  forever. Payloads are now validated before any index is allocated.
- **Sequencing indexes never restarted.** RakNet resets them with every
  ordering index, and the official server does: (order 8, seq 1..8),
  (9, 0), (10, 0..11). Ours counted for the whole connection. The client
  sorts frames held behind a missing ordered frame by
  `hole * 2**20 + sequencing index`, so after 2**20 sequenced sends (the
  official server sent 23 per second: 6-12 hours on one map) a lost ordered
  frame left a stale sequenced frame at the head of that heap and stopped
  ordered delivery for good. Every datagram was still acknowledged, so
  nothing on the server noticed. `FrameBuilder._ord` restarts the index.
- **Retired clients were never told, and were kept alive.** `drop_session`
  sent a bare 0x15, which a connected RakNet peer ignores; it now sends a
  framed, unreliable ID_DISCONNECTION_NOTIFICATION. Any later datagram from
  that endpoint -- or from a client returning after the receive timeout, or
  from before a restart -- created a fresh session that acknowledged it and
  answered its pings, so the client's connection never timed out and the
  player sat in a world nothing updated. Only offline messages open a
  session now; other datagrams from an unknown endpoint are ignored, and the
  client's own timeout reports the loss.
- **Walking batches had no cap.** `DSO_MONSTER_MOVES_PER_PACKET` above 55
  made unreliable datagrams the client drops unseen (60 moves: 1,335 bytes).
  It is capped at 55.

Regressions: `test_reliable_datagram_budget.py`, `test_sequenced_frames.py`,
`test_retired_endpoints.py`.

The 2026-09-28 `prop-info-response` stall in section 14 was not a size
problem: that reply went out unsplit only up to 1,246 bytes. Its cause is
not established. That build still bound its UDP ports with `SO_REUSEADDR`,
under which a second server process on the same port can receive the
client's acknowledgements; `SO_EXCLUSIVEADDRUSE` prevents that now.

Still open: a NAK re-sends every listed datagram past its 100 ms grace at
once, unlike the timer path's `DSO_RELIABLE_RETRANSMIT_BURST_LIMIT`, so with
a round trip over 100 ms a burst loss is re-sent whole into the receive
window that dropped it. The retry interval is a fixed 250 ms, so a client
with a longer round trip receives every reliable datagram at least twice.
