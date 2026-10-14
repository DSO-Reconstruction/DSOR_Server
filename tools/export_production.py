"""Export server source, docs and tests without client assets or live state.

Run with Python 3.12+: python tools/export_production.py --name dso-server-production
Only the new releases/<name> directory and its ZIP/checksum are written.
Existing destinations are never overwritten. No network access is needed.
"""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import runpy
import shutil
import sys
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("services", "protocol", "gameplay", "persistence", "runtime")
ROOT_FILES = ("app.py", "paths.py", "run.py", "run.bat", "__init__.py", "__main__.py", "conftest.py")
DEVELOPMENT_TOOLS = ("audit_character_stats.py", "install_admin_effects.py", "export_production.py", "sanitize_release.py")
REFERENCE_DATE = "2026-09-09 00:00:00"
IGNORED_DIRECTORIES = {"__pycache__", ".pytest_cache", ".git", ".venv"}
EXCLUSIONS = [
    "live_debugger/; editor/agent settings; Git history",
    "todo/; development caches and compiled Python files",
    "tools other than required test helpers and the release exporter; data/tools/; data/exports/; profiling binaries and client artwork",
    "data/diagnostics/; data/logs/; data/state/; caches and bytecode",
    "account/player databases, sessions, social state and all backups/WAL/SHM files",
    "old event runs (replaced with an empty schedule unless --keep-events is used)",
    "all data/maps/ and data/navigation/ content, plus data/db/static.db4; supplied by the operator",
    "legacy/prototype files outside the standalone server directory; local run.md",
]

README = """# DSO server production fork

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

EVENT_SCHEDULE_DESCRIPTION

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
"""

CONTENT_INSTRUCTIONS = {
    "data/maps/README.txt": """COPY CLIENT MAP CONTENTS HERE

Copy the contents of the compatible client's export_win32/maps/ folder into
this folder (data/maps/). Do not create a second maps/ folder inside it.
The server needs the .db4, .map and .col files for each installed map, e.g.:
  data/maps/a0200_kingscity.db4
  data/maps/a0200_kingscity.map
  data/maps/a0200_kingscity.col

Use the same client content revision as data/db/static.db4.
Do not copy backup files (.bak, *backup*, WAL or SHM sidecars).
Map assets are not included in this release.
""",
    "data/navigation/README.txt": """COPY CLIENT NAVIGATION CONTENTS HERE

Copy the contents of the compatible client's export_win32/navigation/ folder
into this folder (data/navigation/), preserving its map subdirectories.
Do not create a second navigation/ folder inside it. For example:
  data/navigation/a0200_kingscity/a0200_kingscity.tok

Also copy export_win32/tables/navshapes.bin from the client to:
  data/navigation/navshapes.bin
This file comes from tables/, not the client's navigation/ folder.

The server uses the .tok meshes and navshapes.bin. Client preprocess files
(.pcr, .ppp, .pus, .path) are not required by the server.
Use the same client content revision as the maps and static database.
Do not copy backup files. Navigation assets are not included in this release.
""",
    "data/db/README.txt": """COPY THE CLIENT STATIC DATABASE HERE

Copy the compatible client's export_win32/db/static.db4 to:
  data/db/static.db4

Use the same static.db4 content revision on the client and server, together
with matching maps and navigation files. Protocol indexes depend on row order.
Do not copy .bak files, *backup* files or WAL/SHM sidecars.

The static database is not included in this release. Do not copy development
accounts or players: accounts.sqlite3 and players.sqlite3 are created by the
server on first use.
""",
}


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def selected_files() -> list[Path]:
    files = [ROOT / name for name in ROOT_FILES]
    for package in PACKAGES:
        files.extend((ROOT / package).glob("*.py"))
    for pattern in ("*.html", "*.css", "*.js"):
        files.extend((ROOT / "services/admin_static").glob(pattern))
    files.extend(ROOT / name for name in (
        "data/quests.xml", "data/catalogs/chest_contents.json",
    ))
    for folder in ("docs", "tests"):
        files.extend(path for path in (ROOT / folder).rglob("*")
                     if path.is_file()
                     and not IGNORED_DIRECTORIES.intersection(path.relative_to(ROOT).parts)
                     and path.suffix not in (".pyc", ".pyo"))
    files.extend(ROOT / "tools" / name for name in DEVELOPMENT_TOOLS)
    return sorted(set(files))


def validate_sources(project: Path) -> int:
    sources = sorted(project.rglob("*.py"))
    for source in sources:
        relative = source.relative_to(project)
        is_test = relative.parts[0] == "tests"
        is_runtime = relative.parts[0] in PACKAGES or (len(relative.parts) == 1 and relative.name in ROOT_FILES)
        allowed = sys.stdlib_module_names | {"dso_server"}
        if is_test:
            allowed |= {"pytest"}
        package = ".".join(("dso_server", *relative.parts[:-1]))
        content = source.read_text(encoding="utf-8-sig")
        compile(content, str(relative), "exec")
        for node in ast.walk(ast.parse(content)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = importlib.util.resolve_name(
                    "." * node.level + (node.module or ""), package,
                ) if node.level else node.module
                names = [module]
                if module == "dso_server":
                    names.extend(module + "." + alias.name for alias in node.names)
            else:
                continue
            for name in names:
                if name.split(".")[0] not in allowed:
                    raise ValueError(f"Unexpected source import: {relative}: {name}")
                if is_runtime and name.startswith(("dso_server.tests", "dso_server.tools")):
                    raise ValueError(f"Runtime depends on development code: {relative}: {name}")
                if name.startswith("dso_server."):
                    target = project.joinpath(*name.split(".")[1:])
                    if not target.with_suffix(".py").is_file() and not (target / "__init__.py").is_file():
                        raise ValueError(f"Missing runtime module: {relative}: {name}")
    return len(sources)


def write_release_metadata(project: Path, keep_events: bool) -> None:
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    metadata = metadata.replace('    "data/events.json",', '    "data/events.json",\n    "data/quests.xml",')
    metadata = metadata.replace('    "data/maps/*.eve",\n', "").replace('    "data/maps/*.mvr",\n', "")
    metadata = metadata.replace('    "data/navigation/*",\n    "data/navigation/**/*",',
                                '    "data/navigation/navshapes.bin",\n    "data/navigation/**/*.tok",')
    metadata = metadata.replace('    "data/db/static.db4",',
                                '    "data/db/README.txt",\n    "data/maps/README.txt",\n'
                                '    "data/navigation/README.txt",\n    "data/db/static.db4",')
    tomllib.loads(metadata)
    (project / "pyproject.toml").write_text(metadata.rstrip() + "\n", encoding="utf-8")
    entry = (project / "__main__.py").read_text(encoding="utf-8")
    anchor = "from dso_server.app import main"
    assert entry.count(anchor) == 1
    if '# Release defaults; explicit operator settings take precedence.' not in entry:
        entry = entry.replace(anchor, '# Release defaults; explicit operator settings take precedence.\n'
                              'os.environ.setdefault("DSO_REQUIRE_LOGIN", "1")\n'
                              'os.environ.setdefault("DSO_DEBUG_VENDOR", "0")\n\n' + anchor)
    (project / "__main__.py").write_text(entry, encoding="utf-8")
    launcher = (project / "run.bat").read_text(encoding="utf-8")
    launcher = launcher.replace("see docs\\OPERATIONS.md.", "see README.md.")
    launcher = launcher.replace("echo   set DSO_VERBOSE_PACKET_LOGGING=1\n", "")
    launcher = launcher.replace("echo   set DSO_FORCE_LOOT_DROPS=1\n", "")
    (project / "run.bat").write_text(launcher, encoding="utf-8")
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    ignore += "\ndata/diagnostics/\ndata/state/\ndata/tools/\ndata/exports/\nreleases/\n*.bak\n*.sqlite3*\n"
    ignore += ("\n# Client assets are supplied locally; keep only copy instructions in Git.\n"
               "data/maps/*\n!data/maps/README.txt\n"
               "data/navigation/*\n!data/navigation/README.txt\ndata/db/static.db4*\n")
    (project / ".gitignore").write_text(ignore, encoding="utf-8")
    if keep_events:
        shutil.copyfile(ROOT / "data/events.json", project / "data/events.json")
        event_text = "`data/events.json` preserves the configured source event schedule."
    else:
        (project / "data/events.json").write_text('{\n  "events": []\n}\n', encoding="utf-8")
        event_text = "`data/events.json` starts with an empty schedule. Configure event runs in the CMS."
    (project / "README.md").write_text(README.replace("EVENT_SCHEDULE_DESCRIPTION", event_text), encoding="utf-8")
    for relative, instructions in CONTENT_INSTRUCTIONS.items():
        destination = project / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(instructions, encoding="utf-8")


def finalize_release(project: Path, archive: Path, checksum: Path) -> None:
    """Validate and package a prepared release; refuse to overwrite its ZIP."""
    source_count = validate_sources(project)
    for relative in ("data/maps", "data/navigation", "data/db"):
        if sorted(p.relative_to(project / relative).as_posix()
                  for p in (project / relative).rglob("*")) != ["README.txt"]:
            raise ValueError(f"Only README.txt may be shipped in {relative}")
    for relative, instructions in CONTENT_INSTRUCTIONS.items():
        if (project / relative).read_text(encoding="utf-8") != instructions:
            raise ValueError(f"Copy instructions differ: {relative}")
    files = sorted(p for p in project.rglob("*") if p.is_file() and p.name != "RELEASE_MANIFEST.json")
    for path in files:
        name = path.name.lower()
        if "backup" in name or ".bak" in name or name.endswith(("-wal", "-shm")):
            raise ValueError(f"Unexpected backup or database sidecar: {path}")
    entries = [{"path": p.relative_to(project).as_posix(), "size": p.stat().st_size, "sha256": digest(p)} for p in files]
    copied_sources = [p for p in selected_files() if p.name not in ("__main__.py", "run.bat")]
    sanitization_path = project / "SANITIZATION.json"
    sanitization = json.loads(sanitization_path.read_text(encoding="utf-8")) if sanitization_path.is_file() else None
    if sanitization is None:
        for source in copied_sources:
            if digest(source) != digest(project / source.relative_to(ROOT)):
                raise ValueError(f"Export differs from retained source: {source}")
    schedule = json.loads((project / "data/events.json").read_text(encoding="utf-8"))
    manifest = {
        "format": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "kind": "production source fork; client assets supplied separately",
        "event_schedule": "empty" if schedule == {"events": []} else "configured",
        "file_count_excluding_manifest": len(entries), "bytes_excluding_manifest": sum(e["size"] for e in entries),
        "excluded": EXCLUSIONS,
        "required_external_content": list(CONTENT_INSTRUCTIONS),
        "sanitization": sanitization,
        "development_files": {"docs": sum(e["path"].startswith("docs/") for e in entries),
                              "tests_and_fixtures": sum(e["path"].startswith("tests/") for e in entries),
                              "tools": list(DEVELOPMENT_TOOLS), "pytest_configuration": "included"},
        "export_only_changes": ["release README and ignore rules", "launcher defaults require login and disable debug vendors",
                                "package metadata includes quests and asset-copy instructions; pytest setup retained",
                                "map/navigation/static database content omitted; copy instructions supplied instead",
                                "export text and roster fixtures anonymized; editorial dates normalized",
                                "run.bat help refers to release README and omits testing overrides", "event schedule policy above"],
        "validation": {"compiled_and_import_checked_python_files": source_count,
                       "client_assets_and_backups_absent": "passed", "copy_instructions": "passed",
                       "retained_source_sha256_comparison": "verified before documented sanitization" if sanitization else "passed",
                       "runtime_initialization_and_content_checks": "skipped: required client assets intentionally omitted",
                       "network_listeners_started": False, "client_gameplay_or_load_test": "not run"},
        "files": entries,
    }
    (project / "RELEASE_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("Creating ZIP and verifying archived file hashes", flush=True)
    shipped = sorted(p for p in project.rglob("*") if p.is_file())
    archive_time = datetime.strptime(sanitization["reference_date"], "%Y-%m-%d %H:%M:%S").timetuple()[:6] if sanitization else None
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in shipped:
            name = f"{project.name}/{path.relative_to(project).as_posix()}"
            if archive_time:
                info = zipfile.ZipInfo(name, date_time=archive_time)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                bundle.writestr(info, path.read_bytes(), compresslevel=6)
            else:
                bundle.write(path, name)
    with zipfile.ZipFile(archive) as bundle:
        if set(bundle.namelist()) != {f"{project.name}/{p.relative_to(project).as_posix()}" for p in shipped}:
            raise ValueError("Archive file inventory differs from release")
        for path in shipped:
            with bundle.open(f"{project.name}/{path.relative_to(project).as_posix()}") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() != digest(path):
                    raise ValueError(f"Archive mismatch: {path}")
    checksum.write_text(f"{digest(archive)}  {project.name}.zip\n", encoding="ascii")
    print(json.dumps({"directory": str(project), "archive": str(archive), "files_including_manifest": len(shipped),
                      "uncompressed_MiB": round(sum(p.stat().st_size for p in shipped) / 1024**2, 2),
                      "archive_MiB": round(archive.stat().st_size / 1024**2, 2)}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="dso-server-production-" + REFERENCE_DATE[:10])
    parser.add_argument("--keep-events", action="store_true", help="Keep the source event schedule instead of starting empty")
    parser.add_argument("--reference-date", default=REFERENCE_DATE, help="Editorial/ZIP reference timestamp (YYYY-MM-DD HH:MM:SS)")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.name):
        parser.error("name must be a simple directory name")
    releases = ROOT / "releases"
    project = releases / args.name
    archive = releases / (args.name + ".zip")
    checksum = releases / (args.name + ".zip.sha256")
    if any(p.exists() for p in (project, archive, checksum)):
        parser.error("destination already exists; choose another --name")
    sources = selected_files()
    project.mkdir(parents=True)
    for source in sources:
        if source.is_symlink() or not source.resolve().is_relative_to(ROOT):
            raise ValueError(f"Source must be a project-owned regular file: {source}")
        before = source.stat()
        target = project / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or digest(source) != digest(target):
            raise ValueError(f"Source changed during export: {source}")
    write_release_metadata(project, args.keep_events)
    sanitizer = runpy.run_path(str(ROOT / "tools/sanitize_release.py"))
    sanitizer["sanitize_release"](project, args.reference_date, rules_path=ROOT / "tools/release_redactions.json")
    print(f"Copied {len(sources)} files; validating release", flush=True)
    finalize_release(project, archive, checksum)


if __name__ == "__main__":
    main()
