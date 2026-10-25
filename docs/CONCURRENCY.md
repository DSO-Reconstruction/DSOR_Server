# Concurrency and map ownership

This document defines both the production target and the rules that apply
during the transition to it. The target is a single-writer actor for each map
instance. The current runtime still has shared mutable state, so guarded locks
make unsafe ownership crossings fail loudly instead of freezing the server.

## Why this exists

Gameplay currently enters the same state from several execution contexts:

- the RakNet receive thread queues player commands for per-session FIFO execution;
- fixed-rate map workers update monsters;
- deadline workers deliver delayed impacts and expirations;
- presence code publishes state to other sessions;
- the background database writer persists snapshots.

Queued gameplay handlers hold the session's plain `gameplay_mutation_lock`
transaction gate. This is separate from the short-lived `session_flow` and
`map_combat` state locks: handler I/O waits run on workers while reception
continues. Trade elects one executor under its registry lock, freezes accepted
offers, releases the registry lock, and reserves both participants' mutation
gates. Other trade commands observe `busy` and return instead of waiting for
the executor while retaining a participant gate. Session-targeted CMS actions
and final retirement persistence use the same gate.

Retiring endpoints remain reserved until their cleanup worker has finished the
final snapshot. Subsequent handshake packets retry after that boundary, so a
new session cannot initialize against an in-flight old snapshot. Arena phase
changes discard stale movement/casts while retaining queued inventory/social
requests; changing the map or retiring the session invalidates the whole FIFO.

A flow lock and a combat lock can therefore form a cycle even when each
individual method looks correct. The common failure shape is:

1. one thread owns a session and waits for map combat state;
2. another owns map combat state and invokes presence or transport code;
3. that code waits for the same session.

The process stays alive, but packets, monster movement, hits, and health
updates stop. A local client then reports a ping spike even though network
latency is not the cause.

## Production target: one MapRuntime writer

Every active map instance will have one `MapRuntime` actor. It is the only
execution context allowed to mutate that instance's gameplay state:

- local and remote player transforms and live health/resource values;
- monster lifecycle, transforms, aggro, skills, and status effects;
- projectiles, delayed impacts, ground effects, summons, and traps;
- vicinity membership and actor materialization state;
- per-player world loot and other map-scoped entities.

Other threads communicate with the runtime using immutable messages.

    receive thread -> MapCommand queue -> MapRuntime
                                        |
                                        +-> immutable network outbox
                                        +-> immutable persistence snapshots

The runtime drains a bounded amount of input each fixed tick, advances state
serially, and appends immutable output records. Socket workers send the
records after mutation has finished. The database writer consumes state
snapshots independently. Neither network backpressure nor SQLite latency may
block a map tick.

Commands must identify the session/character, map instance, and map
generation. A command from a previous map generation is rejected. Delayed
work is posted back to the same queue rather than mutating state from a
`Timer` or deadline callback.

Cross-map transfer is a lifecycle operation:

1. the source runtime publishes a final immutable player snapshot;
2. the coordinator removes the player from the source generation;
3. the destination runtime accepts a join command for its generation;
4. presence and initialization messages are emitted from destination state.

No source and destination map state is held simultaneously.

### Actor invariants

- There is exactly one writer for mutable state in a map instance.
- Map state methods run only on that runtime's thread.
- Commands and output events are immutable values, not references to `Session`
  or mutable combat objects.
- A tick has a work budget. Excess input remains queued and is measured.
- One slow client can lose or coalesce eligible updates, but cannot stall the
  map or another client.
- Persistence receives snapshots; it never reads live map objects.
- Actor callbacks do not call arbitrary services. Effects leave through
  explicit outbox/event types.

## Interim guarded-lock contract

The module `dso_server.concurrency` provides compatible instrumented `Lock`
and `RLock` wrappers. Strict enforcement is the default.

### Named domains

| Domain | Current owner | Wrapper | Protected state |
| --- | --- | --- | --- |
| `session_flow` | one `Session` | `InstrumentedLock` | connection phase, map handoff, initialization and other session transitions |
| `map_combat` | one `LocalCombatState` | `InstrumentedRLock` | mutable map combat, actors, projectiles, effects, and live player state |

The map lock remains reentrant only for compatibility with existing combat
helpers. Reentrancy is not an architectural permission to grow lock scopes.
The session lock remains non-reentrant.

Per-thread held-lock records include lock identity, domain, name, and recursive
depth. Records exist for diagnostics and policy checks; gameplay code must not
branch on them.

### Enforced acquisition rules

The following are rejected before acquisition in strict mode:

- `session_flow` while any `map_combat` lock is held;
- `map_combat` while any `session_flow` lock is held;
- a second, distinct `session_flow` lock while one is held.
- a second, distinct `map_combat` lock while one is held;
- recursive acquisition of a non-reentrant instrumented lock.

The rules are intentionally symmetric. Defining an allowed global order would
still permit callbacks to invert it elsewhere and would keep large,
cross-domain critical sections alive.

A same-lock `map_combat` reentry is supported. Reacquiring the non-reentrant
`session_flow` lock is rejected immediately, before it can wait.

`Session.send` also asserts that the caller holds neither state domain before
it enters transport framing, reliability bookkeeping, or socket I/O. This
runtime boundary catches indirect sends that a lexical architecture check
cannot see.

Non-blocking `acquire` remains compatible with the Python lock API and returns
`False` when unavailable. A blocking acquire never waits forever and never
returns `False`: timeout raises `LockAcquisitionTimeout`.

### Configuration

| Environment variable | Default | Behavior |
| --- | --- | --- |
| `DSO_CONCURRENCY_GUARDS` | `strict` | `strict` raises `LockOrderViolation`; `observe` records and logs the same violation, then permits the attempt |
| `DSO_LOCK_TIMEOUT_SECONDS` | `5.0` | maximum blocking acquisition wait; shorter caller timeouts are preserved |
| `DSO_GAMEPLAY_DEADLINE_WORKERS` | `10` | bounded map-gameplay delayed-impact/expiry worker count; valid range is 1..32 |
| `DSO_GAMEPLAY_INGRESS_WORKERS` | `4` | movement/skill ingress worker count, isolated from deadline callbacks; valid range is 1..32 |
| `DSO_GAMEPLAY_INITIALIZATION_WORKERS` | `1` | local-player initialization workers; the 0x006f handler no longer runs initialization on the receive thread; valid range is 1..8 |

The configured timeout must be positive and finite and is capped at 300
seconds. Invalid configuration fails during import.

Observe mode is an emergency compatibility tool, not a production setting. It
does not disable timeout bounds or diagnostics. It may allow a real deadlock
cycle to progress until the timeout catches one edge.

### Failure diagnostics

`LockOrderViolation` reports:

- the requested domain, human-readable lock name, and object identity;
- the requesting thread and all guarded locks it already holds;
- the acquisition stack.

`LockAcquisitionTimeout` reports:

- the wait bound and requested lock;
- requesting and last-known owning threads;
- the requester's guarded locks;
- Python tracebacks for every live thread, including each thread's guarded
  lock summary.

Do not catch either exception merely to continue gameplay. At a process
boundary, log the complete exception and fail/restart the affected operation
according to the service supervisor policy. Fix the ownership crossing shown
by the trace.

## Prohibited work inside guarded regions

New or modified code must not perform any of the following while holding
`session_flow` or `map_combat`:

- acquire the other domain or a different `session_flow` lock;
- send packets or wait for reliable transport/backpressure;
- open/query/commit SQLite, read files, or perform other blocking I/O;
- sleep, wait on an `Event`/`Future`, join a thread, or wait on a queue;
- invoke an arbitrary callback, presence hook, persistence hook, or plugin;
- start a `Timer` whose callback captures and mutates the guarded object;
- serialize an unbounded player/map payload;
- perform pathfinding or other work whose cost is not tightly bounded.

Small deterministic mutation and immutable snapshot construction are valid.
Prefer computing expensive data before acquisition, validating the relevant
generation/version under the lock, applying a small mutation, and releasing.

### Callback and I/O pattern

Never assume a callback is harmless because its current implementation is
short. It may later acquire a `Session`, consult presence, publish to peers, or
persist state.

Use this pattern:

1. under the owning domain, copy the minimum immutable input and record a
   generation/version;
2. release the lock;
3. run the callback, codec, path query, socket send, or database publication;
4. if a result must be committed, reacquire only the original domain and
   validate the generation/version before the small mutation.

For multi-session publication, build independent outbox records and release
all state before iterating recipients. Never acquire recipient flow locks as a
set.

## Transport pipeline invariants

The receive thread is transport-owned, not gameplay-owned:

- ACK every valid frameset immediately.
- Deduplicate both outer datagram sequences and encapsulated reliable indexes.
- Reassemble split messages before applying ordering-channel semantics.
- Deliver reliable-ordered application commands only in uint24 order.
- Copy gameplay skill commands into the bounded session FIFO; never execute a
  combat handler directly from the receive loop.
- Preserve the frame's exact bit length and reject queued work from a stale
  map/session generation.

The send side has the matching rule: allocate an ordering index, register its
reliable datagram, and perform the socket write while holding the session's
narrow outbound_lock. Compound/split output holds that lock for the complete
message. Service code may call only the atomic Session APIs; direct
FrameBuilder.reliable_ordered and FrameBuilder.send_split calls are
architecture-test failures.

Neither transport lock may be held while an application callback runs. State
locks may not be held when entering any send API. These two boundaries prevent
transport ordering and backpressure from becoming another edge in the
session-flow/map-combat lock graph.

## Staged migration

### Stage 0 - guard and expose

- Replace `Session.flow_lock` and `LocalCombatState.lock` with named wrappers.
- Run strict by default and keep bounded all-thread timeout diagnostics.
- Refactor every surfaced cross-domain site; do not add exceptions to policy.
- Add regressions for previously silent freezes.

### Stage 1 - isolate side effects

- Convert sends, presence callbacks, persistence, and expensive serialization
  under guarded regions into immutable snapshots/outbox records.
- Replace state-mutating `Timer` callbacks with queued commands.
- Give state transitions explicit map generation/version checks.

### Stage 2 - introduce MapRuntime

- Create one queue and fixed-rate owner per map instance.
- Route monster simulation and player combat commands through it first.
- Keep existing state models as actor-owned implementation details.
- Add queue depth, tick duration, command age, and dropped/coalesced update
  metrics.

### Stage 3 - move the complete map lifecycle

- Move presence, projectiles, summons, effects, loot, PvP, joins, leaves, and
  map transfer into actor commands/events.
- Make remote-player and vicinity snapshots derive from the same generation.
- Keep socket and persistence work strictly outside the actor.

### Stage 4 - remove shared mutation

- Delete external direct access to `LocalCombatState`.
- Remove `map_combat` locking after tests prove single-thread ownership.
- Retain thread-affinity assertions and bounded queues as production guards.
- Stress multiple maps and at least ten players per populated instance with
  projectile, summon, loot, reconnect, and map-transfer workloads.

## Current verification status

The transition guard is active in strict mode and the audited gameplay paths
no longer acquire `session_flow` and `map_combat` together. Session registry
replacement/removal is atomic, transport and autosave workers isolate a bad
session instead of terminating, and socket sends are forbidden while either
state domain is held. The ten-player packed-map benchmark is the release gate
for scheduler starvation as well as lost impacts.

This is deliberately described as **deadlock-resistant and fail-fast**, not
mathematically immune. By-construction immunity for map gameplay arrives only
when the `MapRuntime` migration removes shared map mutation and leaves one
writer per map generation. The guard remains useful afterward as a regression
alarm at transport and persistence boundaries.

### Primary references

- Python's `threading` documentation explains that a default lock acquisition
  can block until another thread releases it, that timed acquisition is
  supported, and that unmatched `RLock` recursion can deadlock:
  https://docs.python.org/3.12/library/threading.html
- Python's synchronized queues provide the supported thread-safe handoff used
  by the planned command/outbox ownership boundary:
  https://docs.python.org/3.12/library/queue.html
- Python explicitly documents executor deadlocks caused by workers waiting on
  each other or submitting-and-waiting in an exhausted pool. Map workers must
  therefore never synchronously await work submitted to their own pool:
  https://docs.python.org/3.12/library/concurrent.futures.html
- Python's `faulthandler` provides an independent watchdog mechanism capable
  of dumping all thread stacks when a process is deadlocked:
  https://docs.python.org/3.12/library/faulthandler.html

## Reviewer checklist

For every concurrency-affecting change, verify:

- [ ] Which execution context owns each mutable value?
- [ ] Does any path acquire both named domains or two `Session` flow locks?
- [ ] Are socket, SQLite, filesystem, waiting, pathfinding, and callbacks
      outside guarded mutation?
- [ ] Is data crossing a thread boundary immutable and detached from `Session`
      and `LocalCombatState`?
- [ ] Does delayed work enqueue a command instead of mutating state directly?
- [ ] Does the command carry and validate the correct map generation?
- [ ] Can one backlogged client delay another client or the map tick?
- [ ] Are queue depth, tick cost, timeout, and failure paths observable?
- [ ] Do tests run in strict mode and cover contention, reentry, map rejoin,
      disconnect, transfer, and shutdown?
- [ ] Is every failed blocking acquisition raised and logged with its complete
      diagnostic rather than ignored?


## CMS live commands

The CMS uses a bounded `AdminCommandQueue` on each game service. A local UDP
wake-up lets the existing receive/application thread drain one queued action;
there is no additional gameplay writer or polling worker. Requests that time out
before starting are cancelled; once started, their actual result is returned.
Session identity and map generation are checked again on execution.

Bookings perform database lookup, equipment generation and packet encoding on
detached snapshots in the HTTP handler. The game thread compares inventory
identity, wallets, item allocator, booking receipts and map context under
`flow_lock` before committing. The `_deliver_rewards` activity counter makes
CMS yield to off-thread chat/event rewards; it neither blocks nor reroutes those
rewards. Existing copy-on-write pickups and essence consumption remain guarded
by the flow lock. A conflict retries preparation, then leaves the booking pending
if the character is still busy. Publication and persistence happen after release.
The existing delivery/cancellation lock is held only by CMS/initialization callers,
never acquired by the queued game-thread commit.
