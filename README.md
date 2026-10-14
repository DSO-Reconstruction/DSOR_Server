# DSO server production fork

Release source with operator-supplied client assets. Requires **64-bit Python 3.12+**;
the runtime uses only the Python standard library.

## Run

First add the client assets described in `data/maps/README.txt`,
`data/navigation/README.txt` and `data/db/README.txt`. The server cannot start
without the required content. Then, from this directory on Windows:

```powershell
py -3.12-64 run.py
```

`run.bat` also works; `run.bat --log` saves console output under `data/logs/`.
On Linux use `python3 run.py`. Paths are relative to the package, independent
of the current working directory. Ctrl+C or SIGTERM requests graceful shutdown.

The release launcher defaults to `DSO_REQUIRE_LOGIN=1` and `DSO_DEBUG_VENDOR=0`,
including on loopback. Explicit environment overrides still take precedence.
Accounts and characters start empty; register through the game launcher/account
API and create characters through the normal client flow.

To accept remote players, set `DSO_HOST=0.0.0.0` and `DSO_PUBLIC_HOST` to the
address clients can reach before starting. The default bind is `127.0.0.1`.
The process needs permission to bind UDP port 80 on platforms that restrict it.

| Service | Default port |
| --- | --- |
| Game authentication gateway | UDP 80 |
| Login/map handoff | UDP 2190 |
| Chat | UDP 2191 |
| Character selection/gameplay | UDP 2192 |
| Launcher registration/login API | HTTP 8080 |
| Admin CMS (loopback by default) | HTTP 8081 |

## Configuration and administration

| Variable | Purpose/default |
| --- | --- |
| `DSO_HOST` | Game bind address; `127.0.0.1` |
| `DSO_PUBLIC_HOST` | Client-facing address, especially behind NAT |
| `DSO_ACCOUNT_HOST`, `DSO_ACCOUNT_PORT` | Account API bind/port; game-dependent bind, 8080 |
| `DSO_ACCOUNT_SERVICE` | Account API enabled by default |
| `DSO_ADMIN_HOST`, `DSO_ADMIN_PORT` | CMS bind/port; `127.0.0.1`, 8081 |
| `DSO_ADMIN_SERVICE` | CMS enabled with the account service by default |
| `DSO_ADMIN_SECURE_COOKIE` | Set `1` when serving CMS through HTTPS |
| `DSO_PLAYER_DATABASE` | Character database; `data/db/players.sqlite3` |
| `DSO_ACCOUNT_REGISTRY` | Account/authentication/admin database; `data/db/accounts.sqlite3` |
| `DSO_SOCIAL_STATE_FILE` | Guild/group state; `data/state/social-states.json` |
| `DSO_EVENT_SCHEDULE` | Event configuration; `data/events.json` |
| `DSO_CHAT_AUDIT_LOG` | Chat audit output; `data/logs/chat-service.log` |
| `DSO_ACCOUNT_SAVE_INTERVAL_SECONDS` | Periodic player persistence; 15 seconds |

The CMS supports accounts, characters, item/currency bookings, events, sessions,
live admin actions and audit history. To grant an existing account the first CMS
role, install the fork as an editable package, then run:

```powershell
py -3.12-64 -m pip install -e .
py -3.12-64 -m dso_server.services.admin --grant-admin YourUsername
```

Use the same `DSO_ACCOUNT_REGISTRY` override as the server, if configured.
The command does not create an account. `--revoke-admin YourUsername` removes
access. For remote CMS access use an HTTPS reverse proxy and secure cookies;
the built-in HTTP listener does not provide TLS.

## Content and persistence

The map and navigation directories contain only copy instructions. The static
database and all backups are excluded. Before launching or building an installed
package, copy assets from the compatible client revision as follows:

| Client source | Server destination |
| --- | --- |
| `export_win32/db/static.db4` | `data/db/static.db4` |
| Contents of `export_win32/maps/` | `data/maps/` |
| Contents of `export_win32/navigation/` | `data/navigation/` |
| `export_win32/tables/navshapes.bin` | `data/navigation/navshapes.bin` |

Keep navigation subdirectories intact, for example
`data/navigation/a0200_kingscity/a0200_kingscity.tok`. Do not add another
`maps/` or `navigation/` wrapper directory. Do not copy backup files.
Server and client must use the same static database revision: wire indexes
depend on its row order. Debug vendors are hidden by the release launcher.

Quest definitions (`data/quests.xml`) and the chest catalog are included.
Account/player databases and runtime output directories are created as needed;
they are not seeded from the development server.

`data/events.json` starts with an empty schedule. Configure event runs in the CMS.

Stop the running server before backing up or migrating its account/player
databases, social state and event schedule, so pending database writes drain.
This export is a fresh fork, not a backup of existing players or accounts.

## Development and tests

The complete `docs/` and `tests/` source folders are included, with test fixtures,
the root `conftest.py`, pytest configuration and the helpers imported by tests.
Start with [Architecture](docs/ARCHITECTURE.md), [Operations](docs/OPERATIONS.md),
[Concurrency](docs/CONCURRENCY.md) and the
[Spell implementation contract](docs/SPELL_IMPLEMENTATION_CONTRACT.md).
Feature documents describe the protocol contracts and related regression tests.
Some documents describe earlier development setups; this README defines the
export's launcher defaults and the client assets that must be supplied separately.

From the exported source directory:

```powershell
py -3.12-64 -m pip install -e ".[test]"
py -3.12-64 -m pytest
```

Use `python3` on Linux. Add the required client assets before running the full
suite; gameplay and relocation tests require the matching static database, maps
and navigation. To check source boundaries and runtime utilities without those
assets, run:

```powershell
py -3.12-64 -m pytest tests/test_standalone.py -k "not relocated"
py -3.12-64 -m pytest tests/test_gc_tuning.py tests/test_log_stream.py tests/test_stall_attribution.py
```

`tools/audit_character_stats.py` and `tools/install_admin_effects.py` are retained
because tests import them. `tools/export_production.py` can generate another
source export with docs, tests and the same asset exclusions; choose an unused
`--name`. Make changes against the documented contracts, then run the relevant
regression tests. Test fixtures stay under `tests/`; runtime modules must not
depend on them. Documentation, tests and development tools are included in the
source ZIP, while the installed runtime package retains its explicit package list.

## Export scope and verification

`RELEASE_MANIFEST.json` lists every shipped file with its size and SHA-256,
exclusions, export-only changes and validation results. This source tree contains
entry points, five runtime packages, admin web assets, quests, the chest catalog,
an event schedule, client-copy instructions, documentation, tests and their helpers.
The small runtime worker-observation, watchdog and logging modules are retained
because the application, garbage collector, admin status and map services use
them. The separate debugger, diagnostic output, unrelated tools, caches, backups,
live state and client-only files are excluded.

The export changes only launcher defaults, package metadata and documentation,
plus the selected event-schedule policy and omitted client assets. Gameplay
implementation and retained content are copied unchanged. The original working
tree is not rewritten.

Validation covers Python syntax/import ownership, asset exclusions, copy
instructions and archive file hashes. Runtime initialization and content checks
are skipped because the client assets are intentionally absent. Tests and fixtures
are preserved for development after those assets are supplied.

`SANITIZATION.json` describes export-only anonymization and date normalization.
Personal labels, identifying paths, captured roster names/IDs and public endpoint
examples are replaced with synthetic values. Editorial dates and capture metadata
use the chosen reference date; executable test dates, expiry boundaries and wire
timing values remain intact. Normalized dates do not claim when the original
captures or validation runs happened. The manifest records the actual generation
time; ZIP entry timestamps use the reference date.

Made by **S**
