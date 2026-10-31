"""Audited CMS configuration for map Possible Rewards drop chances."""

from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading

from dso_server.paths import DEFAULT_LOOT_RATES_CONFIG_PATH
from dso_server.protocol.world_loot_protocol import (
    DEFAULT_POSSIBLE_REWARD_RATES,
    POSSIBLE_REWARD_RATE_KEYS,
)


RATE_LABELS = {
    "map_reward": "Listed map items",
    "unique_set": "Map and region Unique/Set items",
    "gem": "Gem",
    "rune": "Rune (elite mobs)",
    "ingredient": "Basic ingredients",
    "elite_ingredient": "Elite ingredients",
    "jewel": "Normal Jewel (Parallel World chest)",
}


class LootRateConflict(ValueError):
    pass


class LootRateAdministration:
    def __init__(self, service, config_path=None):
        self.service = service
        self.config_path = Path(config_path or DEFAULT_LOOT_RATES_CONFIG_PATH)
        self._lock = threading.RLock()

    def _read_config(self):
        try:
            raw = self.config_path.read_bytes()
        except FileNotFoundError:
            raw = b""
            document = {"defaults": {}, "overrides": {}}
        else:
            document = json.loads(raw)
            if not isinstance(document, dict):
                raise ValueError("loot-rate configuration must be a JSON object")
            document.setdefault("defaults", {})
            document.setdefault("overrides", {})
            if not isinstance(document["defaults"], dict) or not isinstance(
                document["overrides"], dict
            ):
                raise ValueError("loot-rate defaults and overrides must be objects")
        return document, hashlib.sha256(raw).hexdigest()

    def _write_config(self, document, revision):
        temporary = None
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", delete=False,
                dir=self.config_path.parent, prefix=".cms-loot-rates-",
                suffix=".json",
            ) as output:
                temporary = Path(output.name)
                json.dump(document, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            if self._read_config()[1] != revision:
                raise LootRateConflict(
                    "loot rates changed in another CMS session; refresh and try again"
                )
            backup = None
            if self.config_path.exists():
                backup = self.config_path.with_suffix(self.config_path.suffix + ".bak")
                shutil.copy2(self.config_path, backup)
            os.replace(temporary, self.config_path)
            temporary = None
        except OSError as exc:
            raise ValueError(f"could not save loot rates: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _validate_rates(values, *, allow_empty=False):
        if not isinstance(values, dict):
            raise ValueError("rates must be an object")
        unknown = set(values) - set(POSSIBLE_REWARD_RATE_KEYS)
        if unknown:
            raise ValueError(f"unknown loot-rate categories: {', '.join(sorted(unknown))}")
        result = {}
        for key, value in values.items():
            if allow_empty and (value is None or value == ""):
                result[key] = None
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{RATE_LABELS[key]} rate must be a number")
            try:
                numeric = float(value)
            except (OverflowError, ValueError) as exc:
                raise ValueError(f"{RATE_LABELS[key]} rate must be a number") from exc
            if not math.isfinite(numeric) or not 0 <= numeric <= 100:
                raise ValueError(f"{RATE_LABELS[key]} rate must be between 0 and 100%")
            result[key] = round(numeric, 2)
        return result

    def _access_rows(self):
        with closing(sqlite3.connect(
            f"file:{self.service.static_database_path.resolve().as_posix()}?mode=ro",
            uri=True,
        )) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                """
                SELECT access.Id AS access_id,
                       access.AccessGroup AS access_group,
                       access.Difficulty AS difficulty,
                       map.MapTemplate AS map_id,
                       map.MapName AS map_name,
                       map.Region AS region,
                       access.RewardItems AS reward_items
                FROM _Template_MapAccessGroup AS access
                LEFT JOIN _Template_MapTemplate AS map
                  ON map.AccessGroup=access.AccessGroup
                WHERE COALESCE(access.RewardItems, '') <> ''
                GROUP BY access.Id
                ORDER BY map.MapName, access.AccessGroup, access.Difficulty, access.Id
                """)]

    def overview(self, query="", access_id=""):
        with self._lock:
            document, revision = self._read_config()
        defaults = dict(DEFAULT_POSSIBLE_REWARD_RATES)
        defaults.update(self._validate_rates(document.get("defaults", {})))
        overrides = document.get("overrides", {})
        selected_overrides = overrides.get(access_id, {}) if access_id else {}
        if not isinstance(selected_overrides, dict):
            selected_overrides = {}
        maps = []
        total = 0
        needle = str(query or "").strip().casefold()
        if needle:
            for row in self._access_rows():
                searchable = " ".join(str(row.get(key) or "") for key in (
                    "access_id", "access_group", "map_id", "map_name", "difficulty",
                )).casefold()
                if needle not in searchable:
                    continue
                total += 1
                if len(maps) < 100:
                    maps.append({key: row.get(key) for key in (
                        "access_id", "access_group", "difficulty", "map_id",
                        "map_name", "region",
                    )})
        selected = next((row for row in self._access_rows()
                         if row["access_id"] == access_id), None) if access_id else None
        return {
            "revision": revision,
            "defaults": defaults,
            "categories": [
                {"key": key, "label": RATE_LABELS[key]}
                for key in POSSIBLE_REWARD_RATE_KEYS
            ],
            "maps": maps,
            "total": total,
            "selected": ({key: selected.get(key) for key in (
                "access_id", "access_group", "difficulty", "map_id",
                "map_name", "region",
            )} if selected else None),
            "selected_overrides": selected_overrides,
        }

    def update(self, actor, reason, payload):
        defaults = self._validate_rates(payload.get("defaults"))
        if set(defaults) != set(POSSIBLE_REWARD_RATE_KEYS):
            raise ValueError("all default loot-rate categories are required")
        access_id = payload.get("access_id", "")
        access_id = str(access_id or "")
        access_rates = self._validate_rates(
            payload.get("access_rates", {}), allow_empty=True
        )
        if access_id and access_id not in {
            row["access_id"] for row in self._access_rows()
        }:
            raise ValueError("selected map difficulty is not in static.db4")
        if access_rates and not access_id:
            raise ValueError("select a map difficulty before saving overrides")
        with self._lock:
            document, revision = self._read_config()
            if payload.get("revision") != revision:
                raise LootRateConflict(
                    "loot rates changed in another CMS session; refresh and try again"
                )
            overrides = document.get("overrides", {})
            if not isinstance(overrides, dict):
                overrides = {}
            if access_id:
                current = dict(overrides.get(access_id, {}))
                for key, value in access_rates.items():
                    if value is None:
                        current.pop(key, None)
                    else:
                        current[key] = value
                if current:
                    overrides[access_id] = current
                else:
                    overrides.pop(access_id, None)
            updated = {"defaults": defaults, "overrides": overrides}
            self._write_config(updated, revision)
            new_revision = self._read_config()[1]
        changed = {
            "defaults": defaults,
            "access_id": access_id,
            "access_rates": access_rates,
            "reason": reason,
        }
        self.service.repository.audit(actor, "loot_rates.update", access_id or "global", changed)
        return {"ok": True, "revision": new_revision}
