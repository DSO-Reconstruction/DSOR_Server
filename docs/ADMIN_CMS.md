# Admin CMS

The game application starts a separate HTTP admin service at
**http://127.0.0.1:8081**. The launcher account API continues to use port 8080.
Run the server normally with `py -3.12-64 run.py` (or `python run.py`).
The CMS has no third-party dependencies or frontend build step.

## Administrator access

Sign in with the same username and password used by the game launcher.
An enabled account owning a saved character with `admin_privileges=True`
already has access. Explicit CMS roles override that inherited access.
Normal accounts, disabled accounts, and game login tokens cannot access CMS APIs.

To give an existing account CMS access without an administrator character,
run this from the directory containing `dso_server`, using the same account
database override as the game server if applicable:

```powershell
py -3.12-64 -m dso_server.services.admin --grant-admin YourUsername
```

Use `--revoke-admin YourUsername` to deny access, including inherited access.
`--registry path/to/accounts.sqlite3` explicitly selects the account database.
These commands do not create accounts or change passwords. Local role changes
are audited. The Accounts page can manage roles after the first admin signs in.
CMS roles do not change in-game character powers or name colors.

## Pages

| Page | Functions |
| --- | --- |
| Overview | Service health, ports, connections, uptime, account/character counts, pending bookings, runtime diagnostics |
| Accounts | Account detail pages with owned characters, settings, scoped sessions and bookings; rename, reset password, enable/disable login and manage CMS access |
| Characters | Full pages for overview, searchable inventory containers, item details, progression, structured state, live effects/map transfers, bookings and raw diagnostics |
| Bookings | Full-page recipient selection, template search, equipment configuration and review; inspect delivery history/errors and cancel pending grants |
| Events | Search global event templates, start new runs, deactivate or reactivate retained runs, inspect settings and audit history |
| Season Pass | Start, reschedule, end or resume seasons; edit each level's experience and free/gold/red rewards; write rewards into client databases; list missions |
| Item catalog | Search actual static database templates and start item bookings |
| Sessions | Inspect issued game sessions and revoke all login sessions for an account |
| Audit log | Search durable actor, action, target, reason and change history |

Navigation follows **Account ? Character ? Inventory ? Item**. Breadcrumbs and
character tabs keep account context; inventory search/container filters survive
opening an item and returning to the inventory. Account settings, booking
creation/review, character inspection, item templates and audit details are full
pages with browser back/forward navigation. Every audited action opens a
confirmation dialog after you press its button; the dialog asks for the audit
reason (3 to 500 characters; Enter confirms, Shift+Enter adds a line). Going back
leaves the page as it was. There are no data-heavy popup editors.

Character inspection shows the most recent saved snapshot, with online presence
reported separately. Refresh pages to fetch current data. Lists are paginated.
Account changes revoke issued game and CMS sessions. Existing game connections
are not forcibly disconnected; disabling an account prevents subsequent login.
Administrators cannot disable or demote themselves through the CMS.

## Bookings and durability

Choose an account, then one of its characters; starting from an account or
character page carries that recipient into the booking. Choose a currency or
search the item catalog, configure the grant, and review the complete booking;
**Create booking** asks for the reason. The server verifies the selected account and
character still match.
Characters must have entered the game at least once so a saved state exists.
Supported currencies are Andermant, gold (the game's base unit), Badge of Honor,
Draken, Glyphs of Power and the named keyed wallet currencies.
Non-equipment items expose only stack size, limited to the template's maximum
and 1,000 units per booking. Currency bookings allow up to 1,000,000,000.
Currency templates and instant consumables are excluded from item bookings.

Equipment is booked one item at a time and supports:

- Item level within the template's level bounds.
- Common through Epic rarity for ordinary templates. Unique, Set and Mythic
  templates retain their authored rarity.
- Open sockets, limited by the template's socket progression at the chosen level.
- A supported enchantment tier.
- Random enchantment roll quality, or a fixed percentage from 0 to 100.

The game's normal inventory allocator and enchantment generator create the
item, including compatible class/slot enchantments and economy values. Sockets
are opened empty. Unsupported combinations (such as random enchantments on a
crafting base without compatible affinities) are rejected before queuing.
Equipment options are persisted and audited with the booking and included in
idempotency checks. Existing booking tables are migrated additively; older rows
receive an empty options object and retain their original base-item/multiple-stack
delivery behavior.

Bookings attempt immediate delivery when the character is online and fully loaded.
The game client receives native inventory or wallet updates and a booking
notification. Acknowledging the notification never grants the reward again.
Offline or transitioning characters receive pending grants before their next
initialization (login or map entry). Full bags, changed ownership, a busy inventory,
or currency overflow leave the booking pending with a visible error. The booking
detail page offers **Retry live delivery** and cancellation with an audit reason.
Already-applied grants cannot be cancelled, including grants awaiting a persistence
acknowledgement after an error.

HTTP handlers prepare detached mutations. The existing game-service thread checks
the current session, map generation, inventory, wallets and reward activity before
committing the grant. Conflicting gameplay changes cause preparation to retry;
a repeatedly busy character keeps the booking queued. Ordinary rewards are not
blocked or moved to another thread. No database or network work occurs under the
session flow lock.

The browser sends an idempotency key to protect retries of the same submission.
Bookings, roles and audit records live in `accounts.sqlite3` alongside the game
accounts. A reward and its receipt are saved together in the character's
`players.sqlite3` document before the booking is acknowledged. If that
acknowledgement is interrupted, retrying finds the receipt and avoids duplicating
the reward. Delivery also checks that character ownership has not changed.

New characters start without a mount and discover travel destinations by visiting
maps. Authored `UnlockingQuest` requirements grant access when the quest is turned
in. Existing collection ownership and discovered maps are preserved.

## Live character actions

Open **Accounts ? account ? character ? Live actions**. The character must be
online and fully loaded. The page shows the current map and active admin presets:

- Movement speed +100%, equivalent to `!speed`.
- Attack speed +100%.
- Stun immunity for the native `Disabled` effect group.
- **Unlock all maps**, equivalent to `!unlockmaps [character name]`. This persistent
  per-character override grants travel and quest-gated map access without changing
  quest progress or marking tutorial maps as visited. Event availability still
  applies. Change maps or relog to refresh the client travel menu.

Enable and disable are explicit and idempotent; repeated requests do not stack
copies. Effects use the game's native status packets, persist across logins, and
can be removed from this same page. Other status effects are preserved.

**Change map** searches installed maps and authored map-access variants. Choose
a destination, optionally set a valid scaling difficulty, and confirm with a
reason. The normal SwitchMap/handoff flow handles the transfer and destination
spawn. Like `!map`, this administrative action bypasses event restrictions and
travel costs. Unknown/uninstalled destinations and stale sessions are rejected.
Every live action records its administrator, reason, requested parameters and
outcome in the audit log. Refresh the page after the destination finishes loading.

Admin teleports send the gameplay `0x85` SwitchMap command with an explicit
login-service address and character selection disabled.
The packet is serialized before source-map activity stops, so an invalid address
cannot leave the character flagged as transferring. The source UDP connection
stays alive for reliable delivery; the destination route uses the entry spawn.

Transfer diagnostics separately log `sent ... SwitchMapCommand` and
`acknowledged ...-switch-map datagram`. Arming a route alone does not establish
that the command reached the client. An acknowledgement establishes datagram
receipt, but an earlier missing ordered frame can still delay execution. The
existing login and gameplay route-claim logs identify each subsequent reconnect.
The 2026-09-09 fast-travel stall came from the preceding `prop-info-response`:
Kingscity's active events expanded it to 15 records / 1290 payload bytes, sent
as a single 1304-byte datagram. The live client ACKed SwitchMap while the server
kept retrying that earlier reply, so ordered delivery never reached the switch. Prop requests
now use the same fragmentation-aware sender as mid-map prop updates. Coverage
replays that event configuration, loses/reorders fragments, and verifies delivery
of both switch envelopes after the complete prop reply. Separate tests exercise
chat/CMS and both authenticated reconnect hops. All 202 related tests passed.
Live verification after restarting is recorded in
`data/logs/server_20260909_000000_sample_13.stdout.log`: normal travel from Kingscity to
`a0007_pastures_pve` at 02:44:14 completed player initialization at 02:44:18;
`!map a0201_basicarena` at 02:44:26 completed player initialization at 02:44:30.
Both traversed the login and gameplay reconnects successfully, with no repeated
`prop-info-response` delivery failure.

## Active game events

Open **Events** to inspect active, upcoming and inactive runs. **Add event** searches
actual global templates from the game database and starts a fresh runtime instance
immediately. You can set collectible drop chance/amount and default participation.
Choose **3d**, **7d** (default), **14d** or **31d**, a **Custom date** with time,
or **Until deactivated** when adding/reactivating a run. Durations are exact
24-hour days from activation; custom times use the browser's local timezone
and are saved in UTC. The resulting end time appears in the list and detail page.
An active run's **Save end date** action changes its deadline without resetting
its start time or progress; duration shortcuts count from saving that change.
Past dates and conflicting duration/date options are rejected.

Open a run's full detail page to **Deactivate** it or **Activate** it again. Every
action requires an audit reason and confirmation. Deactivation removes the event
from the active set without deleting player progress or claimed rewards.
Reactivation keeps the same instance ID and progress. Adding a new run allocates
a new ID and starts fresh progress. A template cannot have two active/upcoming
runs through the CMS. Map-local and client-owned scripts remain game-managed.

Changes update the existing `DSO_EVENT_SCHEDULE` file (`data/events.json` by
default). The complete candidate is validated with the gameplay parser, then
replaced atomically. Unknown configuration fields are preserved. A schedule
revision prevents one administrator's stale page from overwriting another edit.
Requests and completed changes are recorded in the account database audit log.
Changes survive server restarts and do not modify the static database.

Online characters receive native event, participation, progress, announcement and
buff updates through the game-service command queue. While the CMS service runs,
a schedule watcher also publishes scheduled starts/ends and removes expired event
buffs automatically, even with no browser open. Saved deadlines survive restarts.
Timed runs use scheduled state so gameplay stops granting drops/access at expiry.
Event-owned buffs are reconciled without removing unrelated effects. Drops, travel gates and newly
opened event shops use the updated catalog. The result reports online refreshes
and any deferred sessions. Disconnected/loading characters pick up the schedule
on their next map entry. Event NPCs and scenery load on map entry; existing shared
map instances may need to be recreated for their authored world content to change.
The CMS does not forcibly teleport players or discard a populated map instance.

## Season Pass

The client shows its Season Pass while the event named by
`_Globals.SeasonPassEventId` (`event_season_ticket`) runs; each run is a season.
**Season Pass > Season** starts a new run (31 days preselected), changes its end,
ends it, or resumes the most recent ended run. These use the event schedule, so
they appear on the Events page too. A new season starts every character over at
level 0 (activated tickets carry over); resuming keeps the run's progress.
Missions and experience purchases count only while a season runs.

**Rewards** lists all levels with the experience they need and the free, Gold
ticket and Red ticket rewards. Editing a level stores only what differs from
static.db4 in `data/season_pass.json`; claims use it immediately. Because the
client draws the reward tiles from its own static.db4, each edit is also written
into the databases listed in `DSO_CLIENT_STATIC_DB` (restart the client to see
it), and dsor clients download the edited table from the account service.
See [SEASON_PASS.md](SEASON_PASS.md) for the packets and progress rules.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `DSO_ADMIN_SERVICE` | Enabled when account HTTP is enabled | Set `0` to disable CMS |
| `DSO_ADMIN_HOST` | `127.0.0.1` | Dedicated bind address; `0.0.0.0` enables network access |
| `DSO_ADMIN_PORT` | `8081` | Dedicated HTTP port; must differ from the account HTTP port |
| `DSO_ADMIN_SECURE_COOKIE` | `0` | Set `1` when serving the CMS through HTTPS |
| `DSO_ACCOUNT_REGISTRY` | `data/db/accounts.sqlite3` | Shared game accounts, CMS roles, bookings and audit |
| `DSO_PLAYER_DATABASE` | `data/db/players.sqlite3` | Shared saved character state |
| `DSO_SEASON_PASS_CONFIG` | `data/season_pass.json` | Season Pass reward edits |
| `DSO_CLIENT_STATIC_DB` | Not set | Client `static.db4` files that receive Season Pass reward edits (`;`-separated on Windows) |

For remote use, put the CMS behind an HTTPS reverse proxy and set
`DSO_ADMIN_SECURE_COOKIE=1`; forward the original Host header. The backend
listener remains HTTP. With plain HTTP, passwords and cookies travel unencrypted.
The CMS uses four-hour, HttpOnly, SameSite=Strict cookies, checks account access
on every request, rate-limits failed logins, and requires a CSRF token plus
same-origin requests for writes. It never accepts the account API's service token
or game session GUID as CMS credentials. CMS sessions expire on process restart.
No passwords, password hashes, or game bearer tokens are returned by CMS APIs.

Stopping the game server stops and drains the CMS before closing shared stores.
Use the CMS within the game application so bookings share its authoritative state;
the admin module's command-line entry point manages roles only.
