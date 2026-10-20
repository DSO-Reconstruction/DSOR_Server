"""Helpers for keeping local DSO client preferences stable across launches."""

from pathlib import Path
import os
import re


_COOLDOWN_VISUALIZATION = re.compile(
    r'(<Gameplay\b[^>]*\bcooldownVisualization\s*=\s*["\'])[^"\']*(["\'])',
    re.DOTALL,
)

_ALL_COOLDOWN_TIMERS = "2"


def enable_all_cooldown_timers(client_working_dir: Path) -> None:
    """Set the client's cooldown display mode to All before starting it.

    The client can fail to read/write its settings.xml while starting or closing.
    Some client builds read settings from the per-user Temp DSOClient folder,
    even when the executable is launched from another client directory. Update
    both locations so the preference is stable across launch paths.
    """
    settings_paths = {
        client_working_dir.parent / "settings.xml",
        Path(os.path.expandvars(r"%LOCALAPPDATA%\Temp\DSOClient\settings.xml")),
    }
    for settings_path in settings_paths:
        if not settings_path.is_file():
            continue

        try:
            contents = settings_path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise RuntimeError(f"Could not read client settings: {settings_path}") from exc
        updated, count = _COOLDOWN_VISUALIZATION.subn(
            rf"\g<1>{_ALL_COOLDOWN_TIMERS}\g<2>", contents, count=1
        )
        if count == 0:
            raise RuntimeError(
                f"Could not find Gameplay cooldownVisualization in {settings_path}"
            )
        temporary_path = settings_path.with_name(settings_path.name + ".tmp")
        try:
            temporary_path.write_text(updated, encoding="utf-8")
            os.replace(temporary_path, settings_path)
        except OSError as exc:
            raise RuntimeError(f"Could not save client settings: {settings_path}") from exc
        finally:
            temporary_path.unlink(missing_ok=True)
