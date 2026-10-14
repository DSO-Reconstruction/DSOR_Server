# Player death and respawn

Ordinary PvE/open-world PvP deaths send `KillCommand`, then schedule the local
`ShowDeathDialogCommand` after 58 server ticks (2.32 seconds), matching the
observed death-animation window in the Stonekeep capture. The dialog uses
the session deadline scheduler and is cancelled on revival, map initialization,
or disconnect; callback identity also prevents a previous death from opening
the dialog during a later life. Deadline validation runs under the session
lock, but packet delivery runs after releasing it: the production transport
rejects all sends while mutable state locks are held. A regression test keeps
the real scheduler, lock guard, and reliable framing, with only UDP delivery
replaced by an in-memory socket. Movement, skills, targeting, and further damage
are blocked while the player is dead. Temporary combat effects follow the
database's `KeepOnDeath` setting. Inventory/stat reconciliation preserves
zero HP until a respawn is accepted. Monster attacks can now reduce health
to zero; the old movement-test safety floor of one HP is removed. Later
impacts in the same tick are discarded for an already dead player. Healing
potions, health globes, passive regeneration, and healing-over-time cannot
silently revive a corpse. Death immediately zeros the inventory health snapshot.
Resource regeneration preserves zero HP on the wire, and queued pre-death
health regeneration cannot publish a heal after Kill.

The follow-up failure was caused by base regeneration accepting zero HP:
it healed the authoritative corpse and made all respawn requests fail the
"already alive" check. Resource updates could also republish the old full-health
inventory snapshot without ever sending Revive. The regression tests now
advance regeneration while the player waits on the death screen before
exercising all three respawn choices.

| Death-screen choice | Request flags | Destination | Cost |
| --- | --- | --- | --- |
| Last visited town | `false, false` | Last initialized hub | Free |
| Nearest exit | `true, false` | An authored entry inside the nearest active exit | `_Globals.RespawnCost` (40) |
| Same spot | `false, true` | Authoritative death position and heading | `_Globals.InstantRespawnCost` (120) |

An active, unexpired Premium or Deluxe membership makes both paid choices
free, as required by this server's respawn policy. The server checks and
deducts Andermants; the client never supplies the price. Both flags set,
malformed requests, living players, concurrent/duplicate requests, and
departing sessions are rejected. Failed destination or payment serialization
does not charge the player. Map health restoration and session publication
use separate lock scopes, as required by the runtime's strict prohibition on
nesting session and combat locks. Payment is checked again and committed with
session publication. A handoff before publication rolls back the revival and
never exposes a provisional charge to persistence. A committed revival is
persisted even if the socket fails during delivery. Paid revivals
send the spender-challenge update before the currency balance, as in retail.

`AccountState.last_visited_town` is stored in the existing state JSON and
survives map handoffs and restarts. Old saves without a town use the existing
nearest-hub lookup on their first town respawn. Entering a hub updates the
field. Town respawn uses the normal map transfer and its full-rest initialization.

In-place/exit revival restores full health, retains the current secondary
resource, updates both the authoritative spawn and Move, sends ForceTransform,
ActorStatsUpdate, and Revive, then applies the revival effects. Other players
in the instance receive the actor-scoped transform, health, and revival.
Both paid and membership-free revivals receive destination-specific effects:

| Destination | Animation | Damage protection |
| --- | --- | --- |
| Same spot | `resurrect_self` (4.32 seconds in local content) | `InvulnerablePremiumRespawn` (10 seconds) |
| Nearest portal | `map_respawn` (2 seconds) | `Invulnerable` (15 seconds) |

Movement and skills remain locked for the animation's authored duration.
Protection is independent of membership despite the same-spot effect's name.
Portal fallback preserves the spawn entry's heading as well as its position.
Same-spot revival dispatches the existing `revive120` socket-effect event.

Arena exclusion uses both the supported matchmaking map list and every map
in `_Template_BattlegroundSettings`, plus the dedicated `*_basicarena` maps.
This is necessary because arena templates can claim `OpenPVE` or `ClosedPVE`.
An active match is an additional exclusion regardless of map classification.
PvE boss/event maps containing “arena” in their names remain eligible.
Arena deaths receive no ordinary death dialog and cannot buy a revival.
The existing round controller waits for team elimination (immediate in 1v1)
and restores participants for the next round, including an explicit Revive.

## Packet evidence

Capture: `logs/capture_20260909_000000_sample_06.log`.

- `00:33:42.497`: `85810000000000ff`, 64 bits. ShowDeathDialog has no body;
  its generated actor wrapper is zero.
- `00:33:44.207`: `8b620080`, 26 bits. Respawn writes **two booleans**,
  not a numeric option. The serializer at `0x1409452f8` writes members
  `+0x20` and `+0x21`. The UI handlers at `0x1403dbd97`, `0x1403dbe80`,
  and `0x1403dbf56` select same spot, nearest exit, and town respectively.
- `00:33:44.263`: the 22,256-bit response begins with ForceTransform and
  reconciles status effects. ActorStatsUpdate is followed by Revive with
  duration zero and actor 65,930, then 50-tick and 375-tick effects, then
  another ActorStatsUpdate. The embedded Revive command is
  `6d00000000008a010100`; framed alone it is 96 bits including `0x85`/`0xff`.
- Revive's serializer at `0x14094541c` writes one `uint32` duration.
  The reversed handler removes the actor's Death state and dismisses the UI.

Runtime packets are generated from live state. Effect IDs are resolved by
resource name from the local database; captured actor IDs, effect indices,
balances, and bulk buff snapshots are not replayed.

### Stonekeep three-death capture

Capture: `capture_20260909_000000_sample_14.log`. Relevant application messages are
preserved in `tests/fixtures/player_respawn_retail.json`.

- `01:48:44.312`: map load names physical map `pw007_stonekeep_01_dun`
  and access variant `pw007_02_stonekeep_01_dun`.
- `01:48:53.557`: lethal Hit reports zero HP; Kill retains the player corpse
  for 2,160,000 ticks. The first ShowDeathDialog arrives at `01:48:55.872`,
  2.315 seconds later; retail repeats it while dead. The current server sends
  one reliable dialog after the initial animation window.
- `01:49:23.397`: same-spot request `8b620040` (26 bits). The response
  charges 120, restores 113,663 HP, and sends Revive with duration zero.
  Revival effects last 107 and 250 ticks. The local authored resurrection
  duration rounds to 108 ticks; the runtime uses local content by name.
- `01:49:54.549`: nearest-portal request `8b620080`. The response charges
  40 and moves to `(-78.649353, 2.250765, -19.364229)`, heading `-1.537853`.
  These match Stonekeep's authored spawn transform. Effects last 50 and
  375 ticks. Comparing the effect layout to local content identifies the
  animation/protection pairs above; captured numeric effect indices are
  build-specific and are not copied into runtime packets.
- `01:50:21.248`: town request `8b620000`; the next map load at
  `01:50:22.075` enters `pw000_hub`, matching NewPlayer's last-town field.

Tests: `tests/test_player_respawn.py` runs the captured requests through three
real monster lethals and command dispatch, including the strict map/session
locks, corpse lifetime, payments, destinations, and rollback. Combat guards
are covered in `tests/test_local_combat_state.py`; hub and arena behavior is
covered in `tests/test_hub_travel.py` and `tests/test_pvp_matchmaking.py`.
