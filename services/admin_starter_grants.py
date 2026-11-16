"""Audited CMS configuration for new account & character starter grants."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Any

from dso_server.paths import DEFAULT_STARTER_CONFIG_PATH
from dso_server.runtime import file_revisions
from dso_server.gameplay.starter_config import (
    default_starter_config,
    normalize_starter_config,
    EQUIPMENT_SLOT_NAMES,
)
from dso_server.protocol.currency_protocol import GAME_CURRENCY_DEFINITIONS
from dso_server.protocol.membership_protocol import (
    NO_MEMBERSHIP,
    PREMIUM_MEMBERSHIP,
    DELUXE_MEMBERSHIP,
)


class StarterConfigConflict(ValueError):
    pass


class StarterGrantsAdministration:
    """Manages audited CMS configuration for starter membership, currencies and items."""

    def __init__(self, service, config_path=None):
        self.service = service
        self.config_path = Path(config_path or DEFAULT_STARTER_CONFIG_PATH)
        self._lock = threading.RLock()

    def _read_config(self) -> tuple[dict[str, Any], str]:
        try:
            raw = self.config_path.read_bytes()
        except FileNotFoundError:
            raw = b""
            document = default_starter_config()
        else:
            try:
                document = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"starter configuration is invalid JSON: {exc}") from exc
            if not isinstance(document, dict):
                raise ValueError("starter configuration must be a JSON object")
        return document, hashlib.sha256(raw).hexdigest()

    def _write_config(self, document: dict[str, Any], revision: str) -> str:
        temporary = None
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", delete=False,
                dir=self.config_path.parent, prefix=".cms-starter-",
                suffix=".json",
            ) as output:
                temporary = Path(output.name)
                json.dump(document, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())

            current_doc, current_rev = self._read_config()
            if current_rev != revision:
                raise StarterConfigConflict(
                    "Starter settings were modified in another CMS session; refresh and try again"
                )

            if self.config_path.exists():
                backup = self.config_path.with_suffix(self.config_path.suffix + ".bak")
                shutil.copy2(self.config_path, backup)

            os.replace(temporary, self.config_path)
            temporary = None
            file_revisions.invalidate(str(self.config_path.resolve()))
            return hashlib.sha256(self.config_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ValueError(f"could not save starter configuration: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def overview(self) -> dict[str, Any]:
        """Return the current starter configuration snapshot along with metadata."""
        with self._lock:
            document, revision = self._read_config()
        config = normalize_starter_config(document)

        currency_definitions = [
            {
                "name": d.name,
                "wire_id": d.wire_id,
                "label": d.name.replace("_", " ").title(),
                "default_balance": d.default_balance,
            }
            for d in GAME_CURRENCY_DEFINITIONS
        ]

        membership_options = [
            {"value": NO_MEMBERSHIP, "label": "None (No Membership)"},
            {"value": PREMIUM_MEMBERSHIP, "label": "Premium"},
            {"value": DELUXE_MEMBERSHIP, "label": "Deluxe"},
        ]

        slots_info = [
            {"slot": slot, "name": name}
            for slot, name in sorted(EQUIPMENT_SLOT_NAMES.items())
        ]

        return {
            "revision": revision,
            "config": config,
            "currency_definitions": currency_definitions,
            "membership_options": membership_options,
            "slots": slots_info,
        }

    def update(self, admin_username: str, reason: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Update starter configuration with audit logging."""
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")

        raw_payload = payload.get("body") if isinstance(payload.get("body"), dict) else payload
        revision = str(raw_payload.get("revision", ""))
        raw_config = raw_payload.get("config", raw_payload)
        clean_config = normalize_starter_config(raw_config)

        with self._lock:
            new_revision = self._write_config(clean_config, revision)

        if hasattr(self.service, "repository") and hasattr(self.service.repository, "record_audit"):
            self.service.repository.record_audit(
                admin_username,
                "update_starter_grants",
                "starter_config",
                clean_config,
                reason,
            )

        return {
            "ok": True,
            "revision": new_revision,
            "config": clean_config,
        }

    def reset(self, admin_username: str, reason: str, revision: str) -> dict[str, Any]:
        """Reset starter configuration to defaults with audit logging."""
        defaults = default_starter_config()
        with self._lock:
            new_revision = self._write_config(defaults, revision)

        if hasattr(self.service, "repository") and hasattr(self.service.repository, "record_audit"):
            self.service.repository.record_audit(
                admin_username,
                "reset_starter_grants",
                "starter_config",
                defaults,
                reason,
            )

        return {
            "ok": True,
            "revision": new_revision,
            "config": defaults,
        }
