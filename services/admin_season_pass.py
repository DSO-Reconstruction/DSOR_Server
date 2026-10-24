"""Audited CMS edits to the Season Pass: its reward table and client copies.

Seasons themselves are runs of the event named by ``_Globals.SeasonPassEventId``
and are started, rescheduled and ended through the event schedule
(``admin_events``). Reward edits are kept in ``DSO_SEASON_PASS_CONFIG``
(``data/season_pass.json``), which gameplay applies on top of static.db4 when
it grants rewards. The client draws the reward tiles from its own static.db4,
so every edit is also written into the client databases listed in
``DSO_CLIENT_STATIC_DB`` (separated by the OS path separator).
"""

from contextlib import closing
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading

from dso_server.gameplay.event_system import load_event_catalog
from dso_server.gameplay.season_pass import (
    MAX_SEASON_PASS_REWARD_AMOUNT, SEASON_PASS_TRACKS, apply_season_pass_overrides,
    effective_season_pass_rewards, normalize_season_pass_state, parse_season_pass_overrides,
    parse_season_pass_reward, read_season_pass_rewards, season_pass_event_id,
    season_pass_reward_set, write_season_pass_rewards,
)
from dso_server.paths import DEFAULT_SEASON_PASS_CONFIG_PATH
from dso_server.services.admin_items import item_catalog


_LOCK = threading.RLock()
ALTERNATE_REWARDS_EVENT = "event_season_ticket_2026_temp"


class SeasonPassConflict(ValueError):
    pass


def client_database_paths():
    raw = os.getenv("DSO_CLIENT_STATIC_DB", "")
    return tuple(Path(part.strip()) for part in raw.split(os.pathsep) if part.strip())


def _readonly(path):
    return sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)


class SeasonPassAdministration:
    def __init__(self, service, config_path=None, client_databases=None):
        self.service = service
        self.config_path = Path(config_path or DEFAULT_SEASON_PASS_CONFIG_PATH)
        self.client_databases = (client_database_paths() if client_databases is None
                                 else tuple(Path(path) for path in client_databases))

    # -- configuration file --------------------------------------------------

    def _read_config(self):
        try:
            raw = self.config_path.read_bytes()
        except FileNotFoundError:
            return {"rewards": {}}, ""
        document = json.loads(raw)
        parse_season_pass_overrides(document)
        document.setdefault("rewards", {})
        return document, hashlib.sha256(raw).hexdigest()

    def _write_config(self, document, revision):
        parse_season_pass_overrides(document)
        temporary = None
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", delete=False,
                    dir=self.config_path.parent, prefix=".cms-season-pass-", suffix=".json") as output:
                temporary = Path(output.name)
                json.dump(document, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            if self._read_config()[1] != revision:
                raise SeasonPassConflict("the Season Pass configuration changed; refresh and try again")
            os.replace(temporary, self.config_path)
            temporary = None
        except OSError as exc:
            raise ValueError(f"could not save the Season Pass configuration: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    # -- reward table ---------------------------------------------------------

    def _event_catalog(self):
        return load_event_catalog(self.service.static_database_path, self.service.events.schedule_path)

    def reward_set(self):
        alternate = ALTERNATE_REWARDS_EVENT in self._event_catalog().active_template_ids()
        with closing(_readonly(self.service.static_database_path)) as db:
            return season_pass_reward_set(db, alternate)

    def _tables(self, reward_set):
        with closing(_readonly(self.service.static_database_path)) as db:
            static = read_season_pass_rewards(db, reward_set)
        document, revision = self._read_config()
        overrides = parse_season_pass_overrides(document).get(reward_set, {})
        return static, overrides, apply_season_pass_overrides(static, overrides), document, revision

    def _names(self):
        return {row["template_id"]: row["name"] for row in item_catalog(self.service.static_database_path)}

    @staticmethod
    def _track(value, names):
        item_id, amount = parse_season_pass_reward(value)
        return {"reward": value, "item_id": item_id, "amount": amount, "name": names.get(item_id, item_id)}

    def rewards(self):
        reward_set = self.reward_set()
        static, overrides, effective, _, revision = self._tables(reward_set)
        names = self._names()
        rows = []
        for level, values in sorted(effective.items()):
            base = static[level]
            rows.append({
                "level": level, "exp": values[0], "static_exp": base[0], "edited": level in overrides,
                "tracks": {track: {**self._track(value, names), "static": self._track(original, names),
                                   "edited": value != original}
                           for track, value, original in zip(SEASON_PASS_TRACKS, values[1:], base[1:])},
            })
        return {"reward_set": reward_set, "rows": rows, "revision": revision,
                "databases": self.database_status()}

    def update_reward(self, actor, reason, level, payload):
        if type(level) is not int:
            raise ValueError("Season Pass level must be a whole number")
        with _LOCK:
            reward_set = self.reward_set()
            static, overrides, effective, document, revision = self._tables(reward_set)
            if payload.get("revision") != revision:
                raise SeasonPassConflict("the Season Pass configuration changed; refresh the page before saving")
            if level not in static:
                raise ValueError("Season Pass level not found")
            if payload.get("reset") is True:
                wanted = static[level]
            else:
                names = self._names()
                wanted = (self._experience(payload.get("exp"), level, effective),
                          *(self._reward(payload.get(track), track, names) for track in SEASON_PASS_TRACKS))
            before = effective[level]
            if wanted == before:
                raise ValueError("no changes to save")
            thresholds = [values[0] for _, values in sorted({**effective, level: wanted}.items())]
            if any(low >= high for low, high in zip(thresholds, thresholds[1:])):
                raise ValueError("experience thresholds must rise with every level; adjust the neighbouring levels first")
            entry = {name: value for name, value, original
                     in zip(("exp", *SEASON_PASS_TRACKS), wanted, static[level]) if value != original}
            levels = document["rewards"].setdefault(reward_set, {})
            if entry:
                levels[str(level)] = entry
            else:
                levels.pop(str(level), None)
            record = {"reason": reason, "reward_set": reward_set, "level": level,
                      "before": dict(zip(("exp", *SEASON_PASS_TRACKS), before)),
                      "after": dict(zip(("exp", *SEASON_PASS_TRACKS), wanted))}
            self.service.repository.audit(actor, "season_pass.reward.requested", level, record)
            self._write_config(document, revision)
            self.service.repository.audit(actor, "season_pass.reward", level, record)
            databases = self._write_clients(self._effective_tables())
            self.service.repository.audit(actor, "season_pass.reward.clients", level, {"databases": databases})
            return {"ok": True, "level": level, "databases": databases, "revision": self._read_config()[1]}

    @staticmethod
    def _experience(value, level, effective):
        if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
            raise ValueError("experience must be a whole number")
        # Thresholds must rise level by level, or level_for_experience skips levels.
        low = effective[level - 1][0] + 1 if level - 1 in effective else 0
        high = effective[level + 1][0] - 1 if level + 1 in effective else 0xFFFFFFFF
        if not low <= value <= high:
            raise ValueError(f"level {level} experience must be from {low:,} to {high:,} "
                             "to stay between its neighbouring levels")
        return value

    @staticmethod
    def _reward(value, track, names):
        if not isinstance(value, dict):
            raise ValueError(f"choose a {track} reward")
        item_id, amount = value.get("item_id"), value.get("amount")
        if not isinstance(item_id, str) or not item_id or "," in item_id or item_id != item_id.strip():
            raise ValueError(f"{track} reward needs an item template ID")
        if item_id not in names:
            raise ValueError(f"{track} reward item {item_id!r} is not in the static database")
        if type(amount) is not int or not 1 <= amount <= MAX_SEASON_PASS_REWARD_AMOUNT:
            raise ValueError(f"{track} reward amount must be from 1 to {MAX_SEASON_PASS_REWARD_AMOUNT:,}")
        return f"Loot:{item_id},{amount}"

    # -- client databases -------------------------------------------------------

    def _effective_tables(self):
        overrides = parse_season_pass_overrides(self._read_config()[0])
        with closing(_readonly(self.service.static_database_path)) as db:
            return effective_season_pass_rewards(db, overrides)

    @staticmethod
    def _client_tables(db, tables):
        """The reward sets this client has; a missing alternate set is skipped."""
        return {reward_set: rows for reward_set, rows in tables.items()
                if season_pass_reward_set(db, reward_set == "alternate") == reward_set}

    def database_status(self, tables=None):
        tables = self._effective_tables() if tables is None else tables
        result = []
        for path in self.client_databases:
            entry = {"path": str(path), "in_sync": False, "differences": None, "error": None}
            try:
                if not path.is_file():
                    raise ValueError("file not found")
                with closing(_readonly(path)) as db:
                    entry["differences"] = sum(
                        current.get(level) != values
                        for reward_set, rows in self._client_tables(db, tables).items()
                        for current in (read_season_pass_rewards(db, reward_set),)
                        for level, values in rows.items())
                entry["in_sync"] = entry["differences"] == 0
            except (OSError, sqlite3.Error, ValueError) as exc:
                entry["error"] = str(exc)
            result.append(entry)
        return result

    def _write_clients(self, tables):
        result = []
        for path in self.client_databases:
            entry = {"path": str(path), "changed": 0, "backup": None, "error": None}
            try:
                if not path.is_file():
                    raise ValueError("file not found")
                with closing(_readonly(path)) as db:
                    differs = any(read_season_pass_rewards(db, reward_set) != rows
                                  for reward_set, rows in self._client_tables(db, tables).items())
                if differs:
                    backup = path.with_name(f"{path.name}.season-pass-{datetime.now(timezone.utc):%Y%m%d}.bak")
                    if not backup.exists():
                        shutil.copy2(path, backup)
                        entry["backup"] = str(backup)
                    with closing(sqlite3.connect(path, timeout=5)) as db:
                        with db:
                            for reward_set, rows in self._client_tables(db, tables).items():
                                entry["changed"] += write_season_pass_rewards(db, reward_set, rows)
            except (OSError, sqlite3.Error, ValueError) as exc:
                entry["error"] = str(exc)
            result.append(entry)
        return result

    def sync_clients(self, actor, reason):
        with _LOCK:
            databases = self._write_clients(self._effective_tables())
            self.service.repository.audit(actor, "season_pass.clients.sync", "season_pass",
                                          {"reason": reason, "databases": databases})
            return {"ok": True, "databases": databases}

    # -- seasons and progress -------------------------------------------------

    def overview(self):
        template_id = season_pass_event_id(self.service.static_database_path)
        listing = self.service.events.listing()
        runs = sorted((row for row in listing["rows"] if row["template_id"].casefold() == template_id.casefold()),
                      key=lambda row: -row["event_id"])
        current = (next((row for row in runs if row["state"] == "active"), None)
                   or next((row for row in runs if row["state"] == "upcoming"), None))
        template = next((row for row in self.service.events.templates()
                         if row["template_id"].casefold() == template_id.casefold()), None)
        reward_set = self.reward_set()
        static, overrides, effective, _, _ = self._tables(reward_set)
        with closing(_readonly(self.service.static_database_path)) as db:
            missions = db.execute("SELECT COUNT(*) FROM _Template_SeasonPassMissions").fetchone()[0]
        return {"template_id": template_id, "name": template["name"] if template else template_id,
                "manageable": template is not None, "revision": listing["revision"],
                "current": current, "runs": runs, "reward_set": reward_set,
                "levels": len(effective), "max_experience": max(values[0] for values in effective.values()),
                "missions": missions, "edited_levels": len(overrides),
                "databases": self.database_status(),
                "players": self.player_summary(current["event_id"] if current and current["state"] == "active" else None,
                                               len(effective))}

    def player_summary(self, season_id, max_level):
        bound = unbound = finished = 0
        levels = []
        for profile in self.service.store.profiles():
            saved = self.service.store.get(profile.character_id)
            if saved is None or not saved.season_pass:
                continue
            state = normalize_season_pass_state(copy.deepcopy(saved.season_pass))
            if season_id is not None and state["season_id"] == season_id:
                bound += 1
                levels.append(state["level"])
                finished += state["level"] >= max_level
            elif not state["season_id"]:
                unbound += 1
        return {"in_season": bound, "unbound": unbound, "max_level": finished,
                "average_level": round(sum(levels) / len(levels), 1) if levels else 0}

    def missions(self):
        with closing(_readonly(self.service.static_database_path)) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute(
                "SELECT Id AS mission_id, Priority AS priority, Objective AS objective, "
                "SeasonPassSpecial AS special, ChallengeDifficulty AS difficulty, "
                "SeasonPassTargetValue AS target, SeasonPassExp AS exp, "
                "SeasonPassRefreshCategory AS category FROM _Template_SeasonPassMissions")]
        rows.sort(key=lambda row: (row["category"], row["priority"], int(row["mission_id"])))
        return {"rows": rows}
