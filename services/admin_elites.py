"""Audited CMS configuration for dungeon Guardian/Champion/Leader counts."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading

from dso_server.gameplay.dungeon_elites import (
    MAX_DIFFICULTY,
    MAX_TARGET,
    TARGET_KEYS,
    normalize_config,
)
from dso_server.paths import DEFAULT_DUNGEON_ELITES_CONFIG_PATH
from dso_server.runtime import file_revisions
from dso_server.services.admin_live import map_catalog


class EliteConfigConflict(ValueError):
    pass


class DungeonEliteAdministration:
    def __init__(self, service, config_path=None):
        self.service = service
        self.config_path = Path(config_path or DEFAULT_DUNGEON_ELITES_CONFIG_PATH)
        self._lock = threading.RLock()

    def _read_config(self):
        try:
            raw = self.config_path.read_bytes()
        except FileNotFoundError:
            return {}, hashlib.sha256(b"").hexdigest()
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError("dungeon elite configuration must be a JSON object")
        return document, hashlib.sha256(raw).hexdigest()

    def _write_config(self, document, revision):
        temporary = None
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", delete=False,
                dir=self.config_path.parent, prefix=".cms-dungeon-elites-",
                suffix=".json",
            ) as output:
                temporary = Path(output.name)
                json.dump(document, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            if self._read_config()[1] != revision:
                raise EliteConfigConflict(
                    "dungeon elite counts changed in another CMS session; refresh and try again"
                )
            if self.config_path.exists():
                shutil.copy2(
                    self.config_path,
                    self.config_path.with_suffix(self.config_path.suffix + ".bak"),
                )
            os.replace(temporary, self.config_path)
            temporary = None
            # Encounter loads read the last observed revision; take the save at once.
            file_revisions.invalidate(self.config_path)
        except OSError as exc:
            raise ValueError(f"could not save dungeon elite counts: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _difficulty_names(self):
        with closing(sqlite3.connect(
            f"file:{self.service.static_database_path.resolve().as_posix()}?mode=ro",
            uri=True,
        )) as database:
            return {
                int(row[0]): str(row[1])
                for row in database.execute(
                    "SELECT Id, Name FROM _Template_MapDifficulty"
                )
            }

    def _spawnable_maps(self):
        return {
            row["map_id"]: row["type"]
            for row in map_catalog(self.service.static_database_path)
            if row["map_id"] == row["physical_map"]
        }

    def overview(self, query="", map_id=""):
        with self._lock:
            document, revision = self._read_config()
        snapshot = normalize_config(document)
        names = self._difficulty_names()
        needle = str(query or "").strip().casefold()
        maps = []
        total = 0
        for map_id_candidate, map_type in sorted(self._spawnable_maps().items()):
            if needle and needle not in map_id_candidate.casefold() and needle not in map_type.casefold():
                continue
            total += 1
            if len(maps) < 100:
                maps.append({"map_id": map_id_candidate, "type": map_type})
        selected = str(map_id or "")
        return {
            "revision": revision,
            "difficulties": [
                {
                    "tier": tier,
                    "name": names.get(tier, f"Difficulty {tier}"),
                    **snapshot["difficulties"][str(tier)],
                }
                for tier in range(MAX_DIFFICULTY + 1)
            ],
            "include_event_maps": snapshot["include_event_maps"],
            "maps": maps,
            "total": total,
            "selected": selected,
            "selected_targets": snapshot["maps"].get(selected, {}),
        }

    @staticmethod
    def _clean_targets(values):
        if not isinstance(values, dict):
            raise ValueError("elite counts must be an object")
        result = {}
        for key in TARGET_KEYS:
            value = values.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} must be a whole number")
            if not 0 <= value <= MAX_TARGET:
                raise ValueError(f"{key} must be between 0 and {MAX_TARGET}")
            result[key] = value
        return result

    def update(self, actor, reason, payload):
        difficulties = payload.get("difficulties")
        if not isinstance(difficulties, dict):
            raise ValueError("difficulties must be an object")
        cleaned = {
            str(tier): self._clean_targets(difficulties.get(str(tier)))
            for tier in range(MAX_DIFFICULTY + 1)
        }
        include_event_maps = payload.get("include_event_maps", False)
        if not isinstance(include_event_maps, bool):
            raise ValueError("include_event_maps must be true or false")
        map_id = str(payload.get("map_id") or "")
        map_targets = payload.get("map_targets")
        if map_id and map_id not in self._spawnable_maps():
            raise ValueError("map is not a spawnable map template")
        if not map_id and map_targets is not None:
            raise ValueError("select a map before saving a map override")
        with self._lock:
            document, revision = self._read_config()
            if payload.get("revision") != revision:
                raise EliteConfigConflict(
                    "dungeon elite counts changed in another CMS session; refresh and try again"
                )
            maps = normalize_config(document)["maps"]
            if map_id:
                if map_targets is None:
                    maps.pop(map_id, None)
                else:
                    maps[map_id] = self._clean_targets(map_targets)
            updated = {
                "difficulties": cleaned,
                "maps": maps,
                "include_event_maps": include_event_maps,
                # Not edited here yet: a saved count keeps the setting.
                "keep_authored": normalize_config(document)["keep_authored"],
            }
            self._write_config(updated, revision)
            new_revision = self._read_config()[1]
        self.service.repository.audit(
            actor, "dungeon_elites.update", map_id or "global",
            {
                "difficulties": cleaned,
                "include_event_maps": include_event_maps,
                "map_id": map_id,
                "map_targets": maps.get(map_id) if map_id else None,
                "reason": reason,
            },
        )
        return {"ok": True, "revision": new_revision}
