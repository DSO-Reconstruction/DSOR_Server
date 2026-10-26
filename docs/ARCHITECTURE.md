# Server architecture

## Design goals

The runtime is organized around four rules:

1. Packet codecs contain serialization only.
2. Database-backed domain modules resolve editable game data.
3. Services coordinate one gameplay responsibility.
4. `ServerApplication` owns process lifecycle and shared stores.

This separates wire compatibility from gameplay behavior. A packet-layout
change should not require editing world or social orchestration, and a
gameplay rule should not be implemented by replaying a captured byte string.

## Runtime composition

`python run.py` from this directory (or `python -m dso_server` from its
containing directory) calls `dso_server.app.main`, which creates one
`ServerApplication`. The application owns:

| Class | Module | Port | Responsibility |
| --- | --- | ---: | --- |
| `GatewayLoginServer` | `services/gateway.py` | 80 | authentication redirect |
| `LoginGameServer` | `services/login_game.py` | 2190 | login and armed map handoff |
| `ChatServer` | `services/chat.py` | 2191 | chat transport and membership routing |
| `CharacterServer` | `services/character_server.py` | 2192 | character selection and gameplay |
| `AccountService` | `services/accounts.py` | 8080 | launcher account HTTP API |
| `AdminService` | `services/admin.py` | 8081 | admin CMS; shared accounts and queued bookings |
| `PeriodicAccountSaver` | `app.py` | n/a | interruptible periodic state snapshots |

`CharacterServer` is composed from focused service mixins:

| Module | Responsibility |
| --- | --- |
| `lifecycle` | connections, route arming, map bootstrap |
| `player_initialization` | initialization pipeline (see below) |
| `character_selection` | selection and character creation |
| `customization` | sticker book, mounts, costumes, pets |
| `world` | movement, vicinity, actors, world loot |
| `combat_resolution` | hit/kill/status serialization |
| `monster_combat` | chase, attack, return-home simulation |
| `player_combat` | skills, targeting, projectiles, impacts |
| `inventory_map` | inventory, NPCs, props, map transitions |
| `presence` | remote players and shared map state |
| `social` | queries, guilds, groups, invitations |
| `commerce` | shop offers, rewards, transactions |
| `talents` | presets, allocations, modifiers, PvP state |
| `dispatch` | command-to-service routing |

The mixins use `CharacterService`, a bound composition facade, for
cross-domain calls. This keeps service modules independently importable and
avoids circular imports. It is bound once to `CharacterServer` during package
initialization.

A service module stays under 1,100 lines. When one grows past that it is split
by responsibility, not by arbitrary cut, and the parts are recomposed through
mixin inheritance so the entry point and its call sites do not move.
The composed `services/character_server.py` binds those mixins together.

## Player initialization pipeline

`PlayerInitializationService` owns one entry point,
`_send_local_player_initialization`. It inherits three sibling modules and
drives their phases in order:

| Module | Phase |
| --- | --- |
| `player_initialization` | callback install, one-shot guard, orchestration |
| `player_init_state` | character, map access, inventory, `NewPlayer`, talent presets, durable status effects, session commit |
| `player_init_world` | map/encounter load, 0x85 batch construction and split send, follow-up snapshots, summary |
| `player_persistence` | account-state capture, asynchronous publication, and XP bounds |

`player_init_state` threads its phases through one `PlayerInitializationState`
and `player_init_world` returns one `PlayerInitializationWorld`, so no phase
depends on locals from another. The order is load-bearing: the session commit
must precede the map load, because command construction reads the stickerbook,
persistent status effects, and item-set effects back off the session.

Initialization costs 100-170 ms with a cached population and over a second
when one must be generated, so it runs on the service's dedicated
initialization worker (`_schedule_local_player_initialization`), never on the
single receive thread: while it ran there the service read no packets and
acknowledged nothing for any client. Generating a dense map's monster
population costs seconds, so `arm_gameplay` still
starts it on a worker while the client is still loading the map, and
`_resolve_map_entry_route` is shared by both paths so the pre-warm cannot
resolve a different encounter than the entry that follows. A build already in
flight is awaited rather than repeated, so a pre-warm that has not finished -
or two players entering one map together - still produces a single population.
Keep the encounter cache keyed on what the population actually depends on: the
map, its resolved difficulty and monster level, and the walkable component the
spawn position lands on. Keying it on the raw spawn position missed on every
real entry.

## Supporting layers

- `runtime/transport.py` owns RakNet framing, `Session`, retransmission,
  receive loops, and shutdown. Scheduling and diagnostics also live in `runtime/`.
- `runtime/context.py` owns shared configuration and protocol imports;
  `paths.py` anchors default paths to the project directory.
- `runtime/gc_tuning.py` owns the managed garbage-collection policy: the
  startup heap and cached map populations are frozen, automatic full
  collections are off, and a maintenance thread runs them when maps are quiet.
- `runtime/log_stream.py` owns console output: timestamped, line-atomic, and
  written by one thread so a stalled console never blocks gameplay.
- `protocol/` owns typed wire models, codecs, and protocol content adapters.
- `gameplay/` owns combat simulation, world state, navigation, and item rules.
- `persistence/account_state.py` owns in-memory player snapshots;
  `persistence/player_database.py` owns the WAL schema and coalescing writer.
- `persistence/character_roster.py` builds the roster exclusively from player
  profiles and saved gameplay state. It does not seed characters, rewrite IDs,
  or import JSON/captured rosters. Empty databases produce empty rosters.
- `persistence/static_game_database.py` resolves read-only client content;
  `persistence/readonly_db.py` caches read-only connections per thread.
- `protocol/world_loot_protocol.py` caches item/loot lookup tables at startup;
  monster deaths do not open SQLite connections.
- `gameplay/item_set_procs.py` resolves item-set triggers into combat effects.
- `data/` holds content, catalogs, persistent state, logs, and diagnostics.
- `tests/fixtures/` contains explicit test characters and captured packet
  fixtures. Runtime code and packaged builds exclude these fixtures.

See [README](../README.md) for the directory and data-file layout.

## Concurrency ownership

The deadlock-immune target is one single-writer `MapRuntime` actor per map
instance. Receive, deadline, transport, and persistence workers exchange
immutable commands, events, and snapshots with that owner; they never mutate
map state directly. During migration, named `session_flow` and `map_combat`
locks enforce a strict no-cross-domain contract and bounded acquisition
timeouts with all-thread diagnostics. Network/database I/O and arbitrary
callbacks are forbidden while either domain is held. See
[CONCURRENCY.md](CONCURRENCY.md) for the enforced rules, configuration,
migration stages, and reviewer checklist.

## Monster simulation and vicinity lifecycle

One fixed-rate worker owns each active map's 25 Hz monster simulation. It is
cancelled when ownership changes or the map becomes inactive; ticks do not
create additional threads.

PathEngine triangles are partitioned into connected components. A map entry
selects the component on which players can walk, and fixed spawns, density
spawns, item scatter, and projectile targets are rejected if they resolve to
another component. Composite maps whose navigation export is for a different
level slice do not snap actors across the map: only authored placements with
concrete collision ground are retained.

Vicinity sets are client registration state, while health and transforms are
authoritative map state. Re-entering a group rebuilds only its living subset
at current transforms. A dead actor cannot be recreated by a late
`ActorRequest`.

Reliable backpressure is per client and must stay that way. The simulation
worker belongs to one session but drives the whole instance, so it never skips
a tick because *its own* session is backlogged; `monster_activity` drops the
batch per viewer instead. The single-session path (no presence callback) is the
one exception, where owner and viewer are the same client. Anything the tick
does while holding `combat.lock` is latency for every player's hits, because
that is the lock the skill and hit paths take — see
[PERFORMANCE.md §9](PERFORMANCE.md).

The simulation's active set is the union of living monster IDs materialized
by all connected viewers in the instance. Every partial discard and session
departure reconciles that union, preventing invisible actors from retaining
navigation/attack work. Density candidates use a real X/Z minimum-distance
spatial index and, when collision terrain exists, must resolve to a concrete
collision surface.

Ground-skill `LocationEffectInfoCommand` data is snapshot state, not an
append-only event stream. Live effects are refreshed once per second; expiry,
map departure, and disconnect publish the remaining (possibly empty)
snapshot to every peer in the instance so the client can remove stale VFX.

## Groups and slot-addressed inventory

Group capacity lives in `social_state.GROUP_MEMBER_LIMIT` and is enforced in
`add_group_member`, not at invitation time. Invitations are asynchronous
notifications, so a group with one free seat can legitimately have several
outstanding and only the first acceptance can succeed. A rejected acceptance
answers the *accepting* player with `GroupingFailedCommand`; the
`GroupInvitationResponseCommand` status only ever reaches the inviter.
`GroupFullError` subclasses `ValueError` so existing handlers keep catching it.

Every slot-addressed inventory operation names one of the four
`Game::Inventory` containers, in the order storage, extra storage, bank, extra
bank: store `2..5`, move `6..0xa` (equipment first), split `0x1e..0x21`. The
tables live in `protocol/gameplay_protocol.py`. `Messaging::SplitStack` sends the amount
that **stays** in the source stack, not the amount that moves. Slot allocation
is authoritative state — a move that is not applied server-side survives only
until the next full `InventoryInfo`.

Essences occupy a slot pair: 35 is the active essence whose damage factor and
ammunition type apply to every cast, 36 parks the one the player switches to.
`active_equipment_slots` in `protocol/inventory_codec.py` is the single filter
that hides the parked essence from combat stats and the item-set evaluation.
An equipped item moved onto an equivalent occupied slot swaps with the item it
displaces (the essence switch, ring onto ring); an item equipped from a bag
sends the displaced item to its former bag slot. Slot 34 was an incorrect
secondary-essence allocation in an older server build; those allocations are
migrated to 36 at initialization. Every
accepted cast spends the active essence's `_Template_Skill.AmmoCost`
(`_consume_active_essence`): the stack shrinks by an `InventoryInfo` delta,
and when it is used up its allocation is dropped and the combat stats are
rebuilt so the damage factor stops. Battlegrounds spend nothing, and
`DSO_CONSUME_ESSENCES=0` restores permanent essences.

## Durable quickslots

Client `QuickSlotsCommand` (`0x0051`) edits one indexed 17-slot bar. Account
state stores the complete bar array, and initialization restores it with
server `QuickSlotsInfoCommand` (`0x0050`). Updating one index never replaces
the other saved bars.

## Dependency direction

```text
ServerApplication
  -> network services
     -> domain service mixins
        -> state/domain modules
           -> protocol codecs
```

Protocol codecs must not import network services. State modules must not
reach into active sockets. Service modules may send typed codec output but
must not embed capture-derived payload blobs.

## Standalone project boundary

Runtime services, domain modules, protocol codecs, and test fixtures are owned
by this package. Imports use `dso_server` or package-relative paths. Default
configuration, content, persistence, and log paths resolve inside this directory.
Explicit file arguments and environment overrides may select other locations.

`run.py` loads this package directly without adding its parent to `sys.path`.
`tests/test_standalone.py` checks imports and initializes a relocated copy with
the original repository unavailable to the interpreter.

## Adding a command

1. Model and test the body in the relevant `*_protocol.py`.
2. Add domain behavior to the owning service.
3. Route its command ID in `services/dispatch.py` and, if the legacy direct
   2190 gameplay route needs it, `services/login_game.py`.
4. Persist any durable state through `PlayerPersistenceService`, and restore
   it in the matching `player_init_state` phase.
5. Add a regression test using typed decode of the response.
6. Run the full suite and a packaged-entry-point smoke test.
