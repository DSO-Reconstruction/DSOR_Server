"""Audited CMS configuration for the Mystic Cube drop chance and contents."""

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

from dso_server.gameplay.mystic_cube_config import (
    CONTENT_KEYS,
    CONTENT_LABELS,
    CUBE_TIER_IDS,
    CUBE_TIERS,
    MAX_DIFFICULTY,
    normalize_config,
    valid_drop_chance,
)
from dso_server.paths import DEFAULT_MYSTIC_CUBE_CONFIG_PATH


class MysticCubeConfigConflict(ValueError):
    pass


class MysticCubeAdministration:
    def __init__(self, service, config_path=None):
        self.service = service
        self.config_path = Path(config_path or DEFAULT_MYSTIC_CUBE_CONFIG_PATH)
        self._lock = threading.RLock()

    def _read_config(self):
        try:
            raw = self.config_path.read_bytes()
        except FileNotFoundError:
            return {}, hashlib.sha256(b"").hexdigest()
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError("mystic cube configuration must be a JSON object")
        return document, hashlib.sha256(raw).hexdigest()

    def _write_config(self, document, revision):
        temporary = None
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", delete=False,
                dir=self.config_path.parent, prefix=".cms-mystic-cube-",
                suffix=".json",
            ) as output:
                temporary = Path(output.name)
                json.dump(document, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            if self._read_config()[1] != revision:
                raise MysticCubeConfigConflict(
                    "mystic cube settings changed in another CMS session; refresh and try again"
                )
            if self.config_path.exists():
                shutil.copy2(
                    self.config_path,
                    self.config_path.with_suffix(self.config_path.suffix + ".bak"),
                )
            os.replace(temporary, self.config_path)
            temporary = None
        except OSError as exc:
            raise ValueError(f"could not save mystic cube settings: {exc}") from exc
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

    def overview(self):
        with self._lock:
            document, revision = self._read_config()
        snapshot = normalize_config(document)
        names = self._difficulty_names()
        stored_tiers = snapshot["contents"]["tiers"]
        return {
            "revision": revision,
            "default": snapshot["drop_chance"]["default"],
            "difficulties": [
                {
                    "tier": tier,
                    "name": names.get(tier, f"Difficulty {tier}"),
                    "drop_chance": snapshot["drop_chance"]["difficulties"][str(tier)],
                }
                for tier in range(MAX_DIFFICULTY + 1)
            ],
            "contents": {
                "default": snapshot["contents"]["default"],
                "keys": [
                    {"key": key, "label": CONTENT_LABELS[key]}
                    for key in CONTENT_KEYS
                ],
                "tiers": [
                    {
                        "template_id": template_id,
                        "label": label,
                        "weights": stored_tiers.get(
                            template_id, snapshot["contents"]["default"]
                        ),
                        "overridden": template_id in stored_tiers,
                    }
                    for template_id, label in CUBE_TIERS
                ],
            },
        }

    @staticmethod
    def _clean_chance(value, label):
        cleaned = valid_drop_chance(value)
        if cleaned is None:
            raise ValueError(f"{label} must be between 0 and 100%")
        return cleaned

    @staticmethod
    def _clean_weights(values, label):
        if not isinstance(values, dict):
            raise ValueError(f"{label} weights must be an object")
        unknown = set(values) - set(CONTENT_KEYS)
        if unknown:
            raise ValueError(f"unknown content weights: {', '.join(sorted(unknown))}")
        missing = set(CONTENT_KEYS) - set(values)
        if missing:
            raise ValueError(f"{label} weights need every content category")
        cleaned = {}
        for key in CONTENT_KEYS:
            value = values[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{CONTENT_LABELS[key]} weight must be a whole number")
            if not 0 <= value <= 100:
                raise ValueError(f"{CONTENT_LABELS[key]} weight must be between 0 and 100")
            cleaned[key] = value
        if sum(cleaned.values()) != 100:
            raise ValueError(f"{label} weights must total 100%")
        return cleaned

    def update(self, actor, reason, payload):
        chance = payload.get("drop_chance")
        if not isinstance(chance, dict):
            raise ValueError("drop chance must be an object")
        default_chance = self._clean_chance(
            chance.get("default"), "default drop chance"
        )
        stored_difficulties = chance.get("difficulties")
        if not isinstance(stored_difficulties, dict):
            raise ValueError("difficulty drop chances must be an object")
        difficulties = {
            str(tier): self._clean_chance(
                stored_difficulties.get(str(tier)),
                f"difficulty {tier} drop chance",
            )
            for tier in range(MAX_DIFFICULTY + 1)
        }

        contents = payload.get("contents")
        if not isinstance(contents, dict):
            raise ValueError("contents weights must be an object")
        default_weights = self._clean_weights(contents.get("default"), "default")
        stored_tiers = contents.get("tiers", {})
        if not isinstance(stored_tiers, dict):
            raise ValueError("tier weights must be an object")
        tiers = {}
        for template_id, weights in stored_tiers.items():
            if template_id not in CUBE_TIER_IDS:
                raise ValueError(f"unknown cube tier: {template_id}")
            tiers[template_id] = self._clean_weights(
                weights, f"{template_id} tier"
            )

        with self._lock:
            _document, revision = self._read_config()
            if payload.get("revision") != revision:
                raise MysticCubeConfigConflict(
                    "mystic cube settings changed in another CMS session; refresh and try again"
                )
            updated = {
                "drop_chance": {
                    "default": default_chance,
                    "difficulties": difficulties,
                },
                "contents": {"default": default_weights, "tiers": tiers},
            }
            self._write_config(updated, revision)
            new_revision = self._read_config()[1]
        self.service.repository.audit(
            actor, "mystic_cube.update", "global",
            {
                "drop_chance": {
                    "default": default_chance,
                    "difficulties": difficulties,
                },
                "contents": {"default": default_weights, "tiers": tiers},
                "reason": reason,
            },
        )
        return {"ok": True, "revision": new_revision}
