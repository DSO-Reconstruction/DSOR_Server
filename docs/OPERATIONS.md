# Operations

## Prerequisites

- **Python 3.12 or newer, 64-bit.** Use `py -3.12-64` on Windows if the
  default `python` selects a 32-bit interpreter. Gameplay deadlines use a
  bounded scheduler; the content database, navigation meshes, and runtime
  state still require a 64-bit address space.
- The package-owned content bundle:
  `dso_server\data\db\static.db4`, `dso_server\data\maps\`, and
  `dso_server\data\navigation\`

The server binds to loopback by default and should remain on `127.0.0.1`
during local reconstruction work.

The client and server must load byte-identical `static.db4` files whenever a
content row is changed. After a database migration, compare SHA-256 hashes of
`dso_server\data\db\static.db4` and the client's
`export_win32\db\static.db4` before launching either process. Skill and status
effect wire indexes are array positions in that shared content revision.

## Extract client bundles as CDN `.nz` files

`tools\unpack_dso_bundles.py` reads the client's raw
`bundles_optional_bundleN.nb._<md5>` / `bundles_required_bundleN.nb._<md5>`
cache files and writes every indexed resource as an individual CDN-compatible
`.nz` file in one flat directory. It converts the original path separators to
underscores and appends the resource MD5, for example
`export_win32_maps_a0001_start.map.<md5>.nz`. Each output has the `__ZN` size
header and zlib payload used by the client cache. It verifies the embedded
MD5 while writing the resource.

```powershell
py -3.12-64 tools\unpack_dso_bundles.py C:\path\to\DSOClient -o C:\path\to\DSOClient
py -3.12-64 tools\unpack_dso_bundles.py C:\path\to\bundle0.nb.<md5>.nz -o C:\path\to\DSOClient
```

Passing a folder processes recognized bundle files in that folder and all of
its subfolders. Use `--list` to list entries without writing `.nz` files. Only
resources in bundles already present in the cache are emitted; the script
does not download missing bundles. It also checks `.nbi` metadata against the
bundle files, preserves standalone cached `__ZN` CDN objects already named as
`.nz` files or legacy `._<md5>` sidecars, and rebuilds the local TOC tree from
the available bundle/resource index. For a folder input, the new root TOC is
self-consistent and its hash is written to `__root.txt` (including
`dlcache\__root.txt` when present); it does not require the original CDN TOC
hashes, such as `7d70…`. Pass `--runtime-cache C:\path\to\DSOClient\dlcache`
when preparing a fully local game cache: this also writes each decoded resource
at the hierarchical path the client reads directly, alongside the flat `.nz`
objects and rebuilt TOCs.

`setup_client_assets.py` performs this bundle step automatically when the
client cache is missing the required local assets. By default it writes the
flat `.nz` objects, TOCs and decoded hierarchy into the repository's
`data/client_unpacked/` (git-ignored), reading the client folder only. With
`--client-cache` it writes them beside the downloaded bundles and into
`DSOClient\dlcache` instead (backing up `__root.txt` as
`__root.txt.before-unpack`), for the official client to run without a CDN.
Either way it then continues with the normal server
asset extraction into the repository's `data/` directory. The official client
must have already downloaded the required `.nb` bundles; setup does not fetch
them or start a CDN service.

## Start and stop

From the standalone `dso_server` directory:

```powershell
py -3.12-64 run.py
```

From its containing directory (or after an editable install),
`py -3.12-64 -m dso_server` also works. All default file paths are anchored to
the package directory, regardless of the working directory.

`Ctrl+C` or `SIGTERM` triggers application shutdown. Shutdown stops the
autosaver, closes all four UDP sockets, cancels chase/mount/projectile timers,
and drains the player-database writer.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DSO_HOST` | `127.0.0.1` | bind address; `0.0.0.0` accepts remote players |
| `DSO_PUBLIC_HOST` | unset | address clients are redirected to between services and told to dial by the launcher; required with a `0.0.0.0` bind behind NAT, otherwise the bind address (or the host's primary address for a wildcard bind) is advertised |
| `DSO_REQUIRE_LOGIN` | on for a non-loopback bind, off for loopback | login enforcement: clients must log in through the account service first, and a hello that presents no live session GUID is dropped. The address gate alone would admit everyone behind a shared NAT or VPN exit |
| `DSO_STATIC_DB` | `dso_server\data\db\static.db4` | static game database |
| `DSO_PUBLISH_STATIC_DB` | `1` | the account service publishes that database to dsor clients (`GET /api/content`, see below); `0` stops it |
| `DSO_PLAYER_DATABASE` | `dso_server\data\db\players.sqlite3` | durable character profiles and gameplay state |
| `DSO_SOCIAL_STATE_FILE` | `data/state/social-states.json` | guild/group state |
| `DSO_CHAT_AUDIT_LOG` | `data/logs/chat-service.log` | chat audit output |
| `DSO_CHEST_CONTENTS_MAP` | `data/catalogs/chest_contents.json` | runtime chest loot catalog |
| `DSO_ACCOUNT_SAVE_INTERVAL_SECONDS` | `15` | periodic snapshot interval |
| `DSO_NEW_CHARACTER_MAP` | `a0001_start_tutorial_dun` | map a newly created character first spawns in (the retail tutorial; set `a0200_kingscity` to start in Kingshill) |
| `DSO_FORCE_LOOT_DROPS` | `0` | all-rarity loot stress-test override |
| `DSO_DAILY_LOGIN_REWARDS_ENABLED` | `1` | daily and premium reward feature switch |
| `DSO_PLAYER_HITBOX_TOLERANCE` | `0.20` | bounded straight-projectile collision tolerance |
| `DSO_CONSUME_ESSENCES` | `1` | spend the active essence's `AmmoCost` on every accepted cast; `0` keeps essences permanent |
| `DSO_SKILL_PROJECTILE_RADIUS_BONUS` | `iceball=0.15` | extra collider radius per player skill, `skill=units;skill=units`, added to the authored bullet radius when its geometry loads |
| `DSO_MONSTER_CHASE_GRACE_DISTANCE` | `18` | pursuit allowance beyond a template's authored `TravelDistance` |
| `DSO_MONSTER_RESET_GRACE_DISTANCE` | `25` | extra distance past the pursuit radius before an actor resets to spawn |
| `DSO_MONSTER_CHASE_IDLE_INTERVAL_SECONDS` | `0.5` | map simulation tick period while nothing needs advancing |
| `DSO_MONSTER_CHASE_IDLE_RETIRE_SECONDS` | `30` | continuous idle time before the map worker retires (`0` never retires) |
| `DSO_MONSTER_ORPHAN_GRACE_SECONDS` | `1.0` | how long a map instance with spawned monsters, player summons or a wounded player may go without a simulation thread before the watchdog's supervisor starts one |
| `DSO_MONSTER_SUPERVISOR_INTERVAL_SECONDS` | `1.0` | how often that supervisor runs; it keeps running with `DSO_STALL_WATCHDOG_SECONDS=0` |
| `DSO_LOG_TIMESTAMPS` | `1` | prefix every log line with wall-clock time; also makes lines atomic |
| `DSO_LOG_ASYNC` | `1` | write console output from one writer thread so a stalled `screen`/SSH console never blocks a gameplay thread; dropped lines are counted and reported |
| `DSO_LOG_QUEUE_LINES` | `20000` | bound on queued console lines while the console is not draining |
| `DSO_GAMEPLAY_INITIALIZATION_WORKERS` | `1` | workers running local-player initialization (100 ms to seconds) off the UDP receive thread; range 1..8 |
| `DSO_GC_POLICY` | `managed` | `managed` freezes the startup heap, disables automatic full garbage collections and schedules them (see below); `default` leaves CPython's collector alone |
| `DSO_GC_FULL_COLLECTION_SECONDS` | `300` | how often the managed collector runs a collection, preferring a moment when no map is simulating |
| `DSO_GC_MAX_DEFERRAL_SECONDS` | `900` | how long a due collection may wait for the maps to go quiet before running anyway |
| `DSO_GC_UNFREEZE_EVERY_CYCLES` | `6` | every this many managed collections the frozen heap is walked as well and re-frozen |
| `DSO_MONSTER_ENCOUNTER_CACHE_SIZE` | `12` | generated map populations kept resident (about 30-40 MiB each); a miss costs 1-1.6 s of generation |
| `DSO_SPAWN_AREA_DENSITY_SCALE` | `0.85` | multiplier on every spawn area's authored `SpawnPerMeter`; `1.0` restores the database population |
| `DSO_SPAWN_AREA_MAX_TOTAL_MONSTERS` | `162` | whole-map cap on the density-driven population (was 650, and 768 before that); big maps hit it, so they carry a quarter of the previous population |
| `DSO_EVENT_NOTIFICATION_KIND` | `7` | notification kind used to announce active events on the first map join (`_Template_Notifications` row order; 6 is the daily-reward popup, 8 the persistent event popup) |
| `DSO_MONSTER_HEARTBEAT_SECONDS` | `1.0` | how often each map reports what its simulation published |
| `DSO_DISABLE_VICTIM_STATUS_EFFECTS` | `0` | diagnostic: monsters receive no debuffs or crowd control |
| `DSO_DISABLE_STATUS_EFFECTS` | `0` | diagnostic: no status effects at all, in either direction |
| `DSO_RELIABLE_RETRANSMIT_BURST_LIMIT` | `16` | overdue reliable datagrams re-sent per session per 20 Hz pass |
| `DSO_RELIABLE_STUCK_WARNING_SECONDS` | `5` | report a reliable datagram that a still-talking client has not acknowledged for this long |
| `DSO_MAX_SESSIONS` | `4096` | maximum sessions per UDP service; offline discovery pings do not allocate sessions |
| `DSO_HANDSHAKE_TIMEOUT_SECONDS` | `10` | expire connections that never finish their handshake |
| `DSO_RELIABLE_PENDING_LIMIT` | `4096` | hard pending reliable datagram limit per peer; exceeding it retires the stream |
| `DSO_RELIABLE_PENDING_BYTES` | `8388608` | hard pending reliable byte limit per peer |
| `DSO_RELIABLE_ACK_TIMEOUT_SECONDS` | `30` | retire reliable streams that cannot acknowledge their outstanding output, even while receiving other traffic |
| `DSO_UDP_RECEIVE_BUFFER_BYTES` | `1048576` | UDP `SO_RCVBUF`; `0` keeps the OS default |
| `DSO_UDP_SEND_BUFFER_BYTES` | `1048576` | UDP `SO_SNDBUF`; `0` keeps the OS default |
| `DSO_STALL_WATCHDOG_SECONDS` | `0.5` | report whole-process stalls over this length; `0` disables |
| `DSO_STALL_WATCHDOG_INTERVAL_SECONDS` | `0.1` | watchdog sampling period |
| `DSO_STALL_WATCHDOG_TRACEBACK_SECONDS` | `1.0` | stall length that also dumps every thread's stack |
| `DSO_VERBOSE_PACKET_LOGGING` | `0` | per-command diagnostic logging |

Set testing overrides to `0` for production-like balance.

Gameplay commands use a bounded per-session FIFO, including inventory and
social operations. Durable saves still complete before those handlers return,
but their waits do not run on the UDP receive thread. Character state, profile,
and wallet changes from one gameplay snapshot share a single writer transaction.
Social updates also retain durable-return semantics; JSON serialization and
atomic file replacement run outside the membership lock.

Administrative mutations run on a separate worker with a 64-command queue.
Targeted commands share the session mutation gate with gameplay, initialization,
trade, and retirement, so moving work off reception preserves state ordering.

UDP payloads drain through two sender workers with per-peer FIFO ordering and
deadline-based pacing. Each service bounds queued output to 32,768 datagrams or
64 MiB, and each peer to 4,096 datagrams or 8 MiB. ACKs bypass payload pacing.
Queue overflow retires an ordered stream instead of silently dropping its frames.

Honor rankings share a snapshot for one second and cache at most eight sorted
boards. A requested page only encodes its own rows. Chat audit records use a
background writer with a 4,096-record / 4 MiB buffer; overload drops new audit
records and counts/reports the loss. Logs rotate at 10 MiB with three backups.
Accepted audit records drain on normal shutdown; write failures are reported.

Class and appearance lookup results are cached for the lifetime of their
`StaticGameDatabase`. Content replacement code must call
`invalidate_content_cache()` or construct a new instance. Combat caches are
scoped to the loaded encounter; restart/reload encounters after static-content
changes. These caches do not poll the filesystem on each cast.

## The dsor client's static database

Status effects travel by row (`_Template_StatusEffect` wire id = rowid - 1), so a
client must hold this server's exact `static.db4`. After
`tools/install_admin_effects.py` (or any other edit, or another build's database)
the official client's copy has to be updated by hand. The dsor client (the Rust
client) fetches it itself when it signs in, from two account-service
endpoints that nothing else calls:

| Endpoint | What |
| --- | --- |
| `GET /api/content` | `{"version": 1, "static_db": {"sha256", "size", "packed_size"}}`, or `null` when nothing is published |
| `POST /api/content/static-db` | `{session_id}`: the database packed as the CDN packs files (`__ZN`, u32 size, zlib), header `X-Content-SHA256` |

- It is the file the game services load (`DSO_STATIC_DB`), read whole between
  two identical `stat` calls (a download never mixes two revisions while a
  tool rewrites it). It is hashed and packed once per revision: about 0.6 s and
  7.5 MB for the release261 database. Edits are picked up on the next request,
  with no restart.
- Only a signed-in player can download it (a live session, 401 otherwise), and
  each address at most 6 times per 15 minutes (429 with `retry_after`).
- `/api/status` and every endpoint the launcher uses are unchanged.
- The client compares the sha256 with its copy, downloads only on a
  difference, checks size, sha256 and the SQLite header, and replaces its
  `game-data/export_win32/db/static.db4` in one step.

## Monster leash

Monsters use two thresholds rather than one boundary.

- **Pursuit radius** â€” `TravelDistance + DSO_MONSTER_CHASE_GRACE_DISTANCE`.
  Monsters chase freely inside it. Pursuit is not clamped to this sphere: the
  goal used to be projected onto it, so an actor whose player stood outside
  its territory walked to that surface and stopped, and packs collected along
  the boundary doing nothing.
- **Reset radius** â€” pursuit radius plus
  `DSO_MONSTER_RESET_GRACE_DISTANCE`. Past it an actor does not walk home. It
  snaps to its spawn with full health, cleared route, dropped aggro, and no
  queued swings or inbound player impacts.

A chase ends because the target is gone â€” the actor was outrun past its
`EscapeRange`, or the player left the reset envelope â€” not because of distance
from home alone. Distance from home is what the reset handles.

Both the walk back and the snap raise `MonsterReturnHome`, whose wire id and
duration are read from `_Template_StatusEffect` rather than hardcoded (the
client keys effects by that table's row order, so a fixed id silently drifts
when the table is edited). A reset clears the actor's status effects except
those whose row sets `KeepOnWayHome`.

The reset is evaluated before the crowd-control and safe-zone branches so a
stunned or rooted actor cannot be parked outside its territory indefinitely.

## Verification

Run from the standalone `dso_server` directory:

```powershell
py -3.12-64 -m pytest
```

The local suite covers gameplay regressions, checks that imports stay within
this project, and initializes a copied application in an isolated interpreter
using temporary player/account databases. It does not bind server ports.

Listener check:

```powershell
Get-NetUDPEndpoint |
  Where-Object LocalPort -In 80,2190,2191,2192 |
  Sort-Object LocalPort |
  Select-Object LocalAddress,LocalPort,OwningProcess
```

Every listener must use `127.0.0.1` and the same server process.

## State safety

`dso_server\data\db\players.sqlite3` contains the character roster, inventory,
equipment allocation, currencies, position/map access, all quickslot bars,
talent presets, active effects, sticker ownership, active costume/pet, mount
state, and each account's membership type/active flag/UTC expiry. New
characters receive Deluxe for five calendar years by default; the persisted
expiry is reused across relogs and map transfers. `data/state/social-states.json`
contains guild/group state.

Currency documents use descriptive fixed keys (`andermant`, `gold`,
`badge_of_honor`, `draken`, and `glyph_of_power`) plus a `game` object whose
keys are stable server names such as `materi_fragment` and `kitchen_coin`.
The loader still accepts the old `rc/vc/gc/ec/uc/arena` representation and
migrates it on the next save. Do not replace the descriptive names with the
misleading retail resource IDs; `protocol/currency_protocol.py` owns that mapping.

Stop the server before copying `players.sqlite3` so its WAL has been drained,
then back up it and `data/state/social-states.json` before testing migrations or
destructive inventory changes. Startup reads the character roster only from
SQLite: it never seeds JSON/captured characters or renumbers persisted IDs.
A fresh database remains empty until character creation. The periodic saver publishes immutable
snapshots to a dedicated, coalescing SQLite writer; packet handlers never
wait for a commit.

## Combat authority

Player hit damage is calculated server-side from the character's
`_Template_XPLevels.BaseDamage`, equipped item/enchantment/socket programs,
active talent and item-set modifiers, active pet and runtime status effects,
weapon factors, and the cast skill's database damage coefficient. The static
combat rows are cached at startup; projectile impact handlers do not query
SQLite or derive damage from the target monster's health.

## Diagnosing a ping spike

A `[StallWatchdog] process stalled` line in
`data/diagnostics/monster-stalls.log` ends with what the process was doing
while the watchdog could not run: its CPU time over the stall, the host's
steal time, the longest garbage-collector pause, and one of three verdicts.
"garbage-collection pause" and "the process was busy" are server-side;
"the process was mostly not running" means the host did not schedule the
guest (VM steal, swap) and no server change will fix it. Player
initialization no longer runs on the UDP receive thread, so a map entry
should not appear as a ping spike for other players; if one does, look for
`initialization for ... waited` lines, which mean several players entered at
once and queued behind the single initialization worker.

## Diagnosing a combat freeze

A client reporting frozen monsters has two causes that produce identical
ordinary logs, and opposite fixes. Decide which one before changing anything.

The `StallWatchdog` line printed at startup arms a thread that sleeps 100 ms
and measures the wall time that actually passed. A pure-Python thread that
asks for 100 ms and observes seconds was not descheduled by chance: the whole
interpreter stopped, and every other thread stopped with it.

- **`[StallWatchdog] process stalled Ns` appears during the freeze.** The
  server really did stop. Stalls over
  `DSO_STALL_WATCHDOG_TRACEBACK_SECONDS` also dump every thread's stack; the
  thread still inside the same call is the cause. Note that ordinary
  CPU-heavy Python does *not* do this â€” the interpreter yields every switch
  interval, costing other threads only tens of milliseconds. A whole-process
  stall means one long C call that never released the GIL.
- **Nothing is reported during the freeze.** The server was running normally
  and the fault is delivery. Look for a gap in the client's reliable-ordered
  stream: RakNet holds every later ordered command behind a missing ordering
  index, and holds sequenced monster movement stamped past that gap as well,
  so movement, attacks and damage all stop together while the server stays
  healthy. `slow UDP dispatch`, `slow UDP send` and `reliable backlog` lines
  around the freeze are the supporting evidence.

Both readings are useful; an empty watchdog log is a result, not a
non-result. Note its limit: it detects stalls of the *whole process*. A single
blocked thread -- including the one that owns a map's simulation -- leaves it
silent, so a quiet watchdog does not mean every thread was running.

Before either branch, establish the scope, which narrows the search faster
than any log:

| observation | meaning |
| --- | --- |
| the player still moves, monsters do not | the client is fine; monster state or monster packets |
| **every** client on that map freezes together | shared map state, not per-client delivery |
| one client freezes, others do not | that client's delivery |

All monsters freezing for all clients at once points at the map's single
simulation thread. That thread takes each viewer's `outbound_lock` in turn to
publish movement, so one client holding its own lock stalls the entire map.
`[RakNet] ... waited Nms for the outbound lock` names the stalled thread and
the client responsible; `... held the outbound lock ... for Nms` names the
holder. Movement itself is now dropped rather than waited on, so this reports
contention instead of freezing the map.

`[DrasaOnlineMapServer] actor N locked out of Movement/RegularSkills by ...`
covers the other possibility: that the server deliberately disabled those
actors. Lines there mean crowd control, not a stall.

Two more lines separate the remaining causes:

- `restarted the orphaned monster simulation of <map>`: the instance had
  spawned monsters, summons or a wounded player but no simulation thread
  for `DSO_MONSTER_ORPHAN_GRACE_SECONDS`. Everyone on it saw the monsters
  stop while travel, chat and casting kept working. The event is also
  written to `monster-stalls.log`; the worker samples there cannot show it,
  because there is no worker to sample.
- `reliable '<label>' (N bytes, A attempts) unacknowledged for Ns although
  <client> is still sending`: that one client's ordered stream is held
  behind a datagram it never accepts, so everything the server sends it
  stops, not only monsters. `split oversized reliable '<label>'` names a
  batch that used to produce such a datagram and is now fragmented.

A reproduction on 2026-09-09 took the second branch: the watchdog stayed
silent through a client-visible freeze, so the server never stopped. The log
showed the trigger immediately before it -- seven `BulletSkill` casts within a
few frames, most with nine impacts, four sharing one `impact_in=0.361s`
deadline. That burst resolves into one flood of reliable-ordered combat
traffic, and anything the receive window drops stalls the client's ordered
stream until retransmission repairs it.

Recovery used to be self-defeating: the whole overdue backlog was re-sent in a
single pass, which cannot arrive sooner than its oldest missing frame and
re-floods the window that just dropped it. `RELIABLE_RETRANSMIT_BURST_LIMIT`
now paces recovery and re-sends genuinely oldest-first.

## Monster crowd control

Two authored effects turn an actor off entirely --
`ActorFeature:off,Movement,RegularSkills`, meaning it neither moves nor
attacks:

| effect | duration | reached from |
| --- | --- | --- |
| `debuff_cc_stun` | 5 s | `debuff_cc_charge` (lightning skills) stacking to 50 |
| `debuff_cc_freeze` | 10 s | `debuff_cc_frost` (ice skills) stacking to 50 |

This is not specific to one spell. Every damaging mage skill carries a
crowd-control debuff: `iceball`, `frozenorb` and `frostwind` apply
`debuff_cc_frost`; `lightningstrike` and `chainlightning` apply
`debuff_cc_charge`; `frostnova` and `frostwind` apply `debuff_cc_freeze`
outright. Whichever spell is used, the rotation ends in the same feature
lock. The client derives the stack chain itself from the occurrence the
server sends, so the server never emits the final effect directly.

Retail paces this through `StopModifiers`: both effects grant
`debuff_cc_stun_immunity` when they expire, and its
`StatusEffectImmunityGroup` entries refuse the groups that would re-apply
them. The server implemented no `StopModifiers` at all, so an actor could be
re-disabled the instant the previous control lapsed and stayed off for as
long as a caster kept stacking. That is now honoured: refused effects are
dropped before they are recorded *and* before they reach the wire, since the
client stacks whatever it receives.

Both chains share the authored `Stun` group, so the immunity cross-applies --
a lapsed lightning stun also holds off an ice freeze. Without that, a mixed
rotation walks straight through the diminishing returns.

The stacking rate was the actual amplifier. The occurrence the server sends is
what the client counts towards `MaxStackSize`, and the server used to advance
it once per *impact*: a twelve-impact cast pushed one monster twelve stacks
closer to being disabled, so ordinary spam reached a threshold of fifty that
is meant to need dedicated stacking. A cast now advances a victim's stack once
per effect however many of its impacts clip that actor, matching the "once per
cast, not once per resolution" rule the item-set procs already use. Repeat
applications within one cast are also no longer re-sent, which is what
produced the client's `Effect already present!` storm.

Note that the server never emits the final crowd control itself, so
server-side immunity cannot block it -- only the stack occurrence reaching the
client's threshold can. Skills that apply `debuff_cc_freeze` outright, such as
`frostnova`, are unaffected and still freeze as authored.

### Is the simulation still producing?

Each populated map reports once a second:

```
[01:48:10.123] [DrasaOnlineMapServer] simulation heartbeat: 25.0 ticks/s,
    48.2 movements/s, 3.0 attacks/s, 12 active actor(s), 0 locked
```

Read it across the client's frozen window. Sustained `~25 ticks/s` with
movements flowing means the simulation ran and produced output the client did
not show -- a delivery or client-side fault, and no amount of work on the
simulation will help. Ticks or movements dropping to zero means the map
genuinely stopped, and the surrounding lines say why.

Server log lines are timestamped and written atomically per line, so they can
be aligned against `DSOClient.log` directly. Do not correlate by position in
the file; concurrent threads print out of order.

### A populated map that simulates nothing

The heartbeat can show a map ticking normally while publishing nothing:

```
24.4 ticks/s, 137.7 movements/s, 5.6 attacks/s, 28 active actor(s), 0 locked
25.6 ticks/s,   0.0 movements/s, 0.0 attacks/s, 25 active actor(s), 0 locked
```

Ticking, nothing locked, producing nothing. A whole window like that prints
the tick's inputs alongside it:

```
[..] map simulated nothing for a whole window: 24 active actor(s), 0 locked;
     aggro_targets=0, combat_targets=1, living=24, ordinary=0,
     spawned=24, untargetable=0
```

Note the window, not the tick. Movement publication is throttled to one frame
per actor every `DSO_MONSTER_MOVEMENT_PUBLISH_TICKS` (default 4), so an
individual tick that publishes nothing is ordinary and a per-tick version of
this check fires constantly on a healthy map.

If the counts are all healthy -- aggro targets present, sets populated -- then
the tick had targets and still produced nothing, and the cause is inside
`advance_monsters`/`maybe_start_monster_attacks` rather than in their inputs.

| reading | meaning |
| --- | --- |
| `combat_targets=0` | presence returned no players for the map |
| `spawned=0` | the viewer's registered actor set does not intersect the living one |
| `ordinary=0` | every living actor was claimed by summon taunts |
| `untargetable=N` | players were removed from aggro (death, revival, an explicit `Targetable` lock) |

Rate limited to one line per second per map, and silent whenever the tick
produced anything.

### Damage immunity is not untargetability

An authored defensive proc disables only the `Damage` feature: incoming hits
do nothing, but the actor is still there and monsters keep chasing and
swinging. Removing such a player from the aggro set instead leaves every
monster on the map with no target, so the tick produces zero movements and
zero attacks for the effect's duration -- for every viewer, whatever spell is
being cast, with nothing logged as wrong.

That was the reported combat freeze. `unique_gem_ce2023_offensive` carries
`SkillStatusEffect:addUE,player_skills,ce2023_jewel_offensive_effect,C:0.08,
D:3.0,ON:Hit`, giving an 8%-per-hit, three-second `ActorFeature:off,Damage`
buff; with several impacts per cast it fires constantly while attacking.

Only an explicit `Targetable` lock, or the movement+skills lock that death,
revival and hiding use, removes a player from aggro. Any future effect that
should make its owner unattackable must say so with `Targetable`, not by
disabling `Damage`.

### Bisecting status effects

When server-side logging cannot rule the subsystem in or out -- which is the
normal case, because the client derives crowd control locally from the stack
occurrence rather than being told about it -- remove it and re-test:

```powershell
$env:DSO_DISABLE_VICTIM_STATUS_EFFECTS = '1'   # monsters get no debuffs
$env:DSO_DISABLE_STATUS_EFFECTS = '1'          # nothing, either direction
```

Both print a `[Diagnostic]` banner at startup, so a log always states which
mode produced it. Damage, deaths and loot are deliberately untouched: a run
where the symptom disappears because combat stopped would prove nothing.

- Symptom gone with victim effects off -> it is the debuff/crowd-control
  chain. Narrow by skill, since `frostnova` and `frostwind` apply
  `debuff_cc_freeze` outright while `iceball` reaches it by stacking.
- Symptom gone only with *all* effects off -> a caster self-buff or aura,
  not the victim chain.
- Symptom persists with everything off -> status effects are not involved and
  the search moves elsewhere.

## Troubleshooting

- Port bind failure: locate and stop the previous process before restarting.
- Black screen on map travel: verify the selected access group resolves to a
  physical local map and that the 2190 handoff was armed.
- A cosmetic appears in a shop after ownership: confirm its source item is in
  `_Template_StickerbookItem.Items`.
- Client rejects an actor/item: inspect low 16-bit manager indexes; full
  server IDs are not valid substitutes for bounded client indices.

## Administrator chat commands

Commands begin with `!`, are authorized from the selected character's
`admin_privileges` flag, and return through a private `System` whisper.

| Command | Result |
| --- | --- |
| `!help` | list available commands |
| `!unlockmaps [character name]` | permanently unlock all maps for yourself or an online character; refresh the travel menu by changing maps or relogging |
| `!item <templateId> [amount]` | grant 1-1000 items through normal inventory mutation |
| `!map <mapId> [difficulty]` | persist and transfer through the normal map handoff |
| `!where` | show the current map and character position |
| `!speed [character name]` | toggle +100% movement speed for yourself or a named online character |
| `!stress <amount> [templateId]` | load test: spawn 1-500 hostile monsters in rings around you, inside the 18.5 m announcement radius, copying the map's commonest ordinary monster unless a template is named; they fight and drop loot as usual, never respawn, and `!killmobs` clears them |
| `!shutdown [minutes]` | broadcast the native maintenance timer and stop gracefully (default: 5 minutes) |

Command text is never broadcast to map, region, group, or guild chat.

`!speed` toggles the issuing character's bonus; `!speed ExamplePlayer` targets that
online character (names are case-insensitive and may contain spaces). The
bonus stays with the character across map changes and relogs until an admin
toggles it off. Other movement bonuses, pets, mounts, and consumables remain
intact. The command responds privately with the affected character and state.

Chat and the CMS use three dedicated permanent status effects:
`admin_movement_speed`, `admin_attack_speed`, and `admin_stun_immunity`.
Each uses an existing buff icon and its own instance, so the effects can be
combined and removed independently. They persist through map changes, relogs,
and death. Previously saved admin toggles using the `pvp_talent_*` templates
migrate on map initialization; native timed buffs and talent stacks are retained.
Live additions use the character clock and a 25-tick interval; initialization
rebases all three admin effects to the new map's clock.

The normal `MovementSpeed` attribute caps at 13. The admin bonus uses native
`Speed:5,absolute,MovementBonus`, which adds 100% of the shipped player base
speed (5) after that cap, subject to the movement-command limit below.
It does not multiply the final equipped speed.
The ordinary speed cap remains unchanged. `MovementBonusSpeed` has its own
native limit of 50 and is also used by mounts. The client calculation is
`MovementSpeed + MovementBonusSpeed` (`ActorAttributeModule::GetMovementSpeed`,
`0x140142278`); `Speed::Apply` (`0x1408aee98`) supports the bonus channel only
with an absolute modifier. Attack speed retains normal native stacking/caps.

The movement command has a separate hard limit of 4 times base speed (20
movement units per second for current player classes). Mounts add their speed
to the bonus channel too: capped equipment (13), a black bear (6), and the
full admin bonus (5) would yield 24 / 5 = 4.8 and trigger the client's clamp
warning. The server now reduces only the excess admin bonus to fit that limit
(1 instead of 5 in this example). Slower characters retain the full bonus;
dismounting or removing speed gear restores it automatically. Reconciliation
also runs for talents, cosmetics, status/vitality updates and initialization.
The toggle remains enabled in chat/CMS while limited, and saved characters
are recalculated before their initial status packet. No client data update
is needed. The unused fifth effect parameter tags limited admin instances,
so unrelated custom effects are not mistaken for the toggle.

Native evidence: `ClientMovementProperty::SendMovement` (`0x140142fe4`)
divides `GetMovementSpeed` by `GetBaseMovementSpeed` (`0x1401421ac`) and clamps
the result to the float at `0x14113e224` (4.0). Regression tests in
`tests/test_admin_mounted_speed.py` cover both mount/toggle orders, low-level
and capped gear, live equipment changes, combat dismounting and mounted relogs.

Install the matching content and status titles before starting either process:

```powershell
py -3.12-64 tools/install_admin_effects.py --client-db C:/game-client/export_win32/db/static.db4
py -3.12-64 tools/install_admin_effects.py --client-db C:/game-client/export_win32/db/static.db4 --apply
```

The first command previews changes. The installer appends new status rows,
preserves existing wire IDs, backs up changed files, synchronizes both static
databases, and adds admin titles to every installed language (English fallback).
It refuses to overwrite divergent client content. Restart both server and client
after installation. Tests cover installation safety/idempotence, native modifier
contracts, independent CMS toggles, packets, and saved-character migration.
Packet and persistence tests pass; actual movement still requires an in-client check.

`!shutdown 10` announces maintenance to all initialized gameplay sessions on
both map endpoints and stops the application ten minutes later. Minutes must
be a whole number from 0 to 1440; `!shutdown 0` requests immediate shutdown.
Reissuing the command replaces the deadline and updates all countdowns. The
deadline belongs to the application, so the administrator can log out without
cancelling it. Players joining or changing maps receive the remaining duration
after their initialization completes. Shutdown follows the same session-save
and database-drain path as `Ctrl+C`.

The client-facing notification is **`Commands::MessageCommand` (`0x0089`),
message type 0, with one kind-1 integer argument containing seconds**. It is
sent with actor context zero. `ClientGameWorld::HandleMessageCommand` at
`0x1401c842c` stores that duration and restarts the local timer; its update
function at `0x1401d39b0` subtracts elapsed seconds and displays
`gui.window.masterServerShutdownMessageMin`. No `AcpServerShutdownCommand`
or `AcpSendMessageToPlayerCommand` is needed for this notification. Native
analysis is recorded in `data/diagnostics/shutdown-start.txt` and
`data/diagnostics/shutdown-handler.txt`.

