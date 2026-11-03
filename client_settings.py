"""Helpers for keeping local DSO client preferences stable across launches."""

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
import shutil
import sqlite3


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


# The old-style item art, shown only while the game runs. The client picks the
# rarity backgrounds by texture name itself (_Template_IconConfig.
# RarityControlled -> icons_toolkit/bg_rarity_<rarity>), so those are texture
# swaps; the frames are item database data. Both are undone on exit.
_OLD_BACKGROUNDS = ("common", "uncommon", "magic", "rare", "epic", "unique")

_OLD_FRAMES = {
    "icons_toolkit/frame_slim_basic": "icons_toolkit/frame_basic",
    "icons_toolkit/frame_slim_unique": "icons_toolkit/frame_unique",
    "icons_toolkit/frame_slim_set": "icons_toolkit/frame_unique",
    "icons_toolkit/frame_slim_mythic": "icons_toolkit/frame_unique",
}

_JOURNAL_SUFFIX = ".dso_old_visuals.json"
_BACKUP_SUFFIX = ".dsobak"

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass(frozen=True)
class OldItemVisuals:
    """A live old-style art patch; the journal on disk is its undo log."""

    db_path: Path
    journal_path: Path


def client_data_root(client_working_dir: Path) -> Path | None:
    """Return the client's export_win32 tree that holds db/ and textures/."""
    for candidate in (client_working_dir / "export_win32", client_working_dir):
        if (candidate / "db" / "static.db4").is_file():
            return candidate
    return None


def _load_journal(path: Path) -> dict | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _write_journal(path: Path, document: dict) -> None:
    temporary_path = path.with_name(path.name + ".tmp")
    temporary_path.write_text(
        json.dumps(document, indent=2, sort_keys=True), encoding="utf-8"
    )
    os.replace(temporary_path, path)


def _pid_running(pid: int) -> bool:
    """Return whether a process with this pid is still running."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _background_target(root: Path, rarity: str) -> Path:
    return root / "textures" / "icons_toolkit" / f"bg_rarity_{rarity}.dds"


def _background_paths(root: Path, rarity: str) -> tuple[Path, Path] | None:
    """Return (old-style source, live target) for one rarity background."""
    target = _background_target(root, rarity)
    old = target.with_name(f"bg_rarity_{rarity}_old.dds")
    if old.is_file() and target.is_file():
        return old, target
    return None


def apply_old_item_visuals(client_working_dir: Path) -> OldItemVisuals | None:
    """Show the old item frames and rarity backgrounds until the client exits.

    Returns the handle restore_old_item_visuals() needs, or None when the
    client's data is not where we expect it; a launch never fails over this.
    The item database is copied whole before it is edited, so restoring puts
    back the exact bytes the client came with.
    """
    root = client_data_root(client_working_dir)
    if root is None:
        return None
    db_path = root / "db" / "static.db4"
    journal_path = db_path.with_name(db_path.name + _JOURNAL_SUFFIX)
    db_backup = db_path.with_name(db_path.name + _BACKUP_SUFFIX)

    leftover = _load_journal(journal_path)
    if leftover is not None:
        if _pid_running(int(leftover.get("client_pid") or 0)):
            # An earlier launch still owns the patch; leave its art alone.
            return None
        restore_old_item_visuals(OldItemVisuals(db_path, journal_path))

    backgrounds = [
        rarity
        for rarity in _OLD_BACKGROUNDS
        if _background_paths(root, rarity) is not None
    ]
    try:
        _write_journal(
            journal_path, {"client_pid": 0, "backgrounds": backgrounds}
        )
        if not db_backup.exists():
            shutil.copy2(db_path, db_backup)
        with closing(sqlite3.connect(db_path)) as db:
            db.executemany(
                'UPDATE "_Template_IconConfig" SET FrameTexture=? '
                "WHERE FrameTexture=?",
                [(new, old) for old, new in _OLD_FRAMES.items()],
            )
            db.commit()
        for rarity in backgrounds:
            paths = _background_paths(root, rarity)
            if paths is None:
                continue
            old, target = paths
            backup = target.with_name(target.name + _BACKUP_SUFFIX)
            if not backup.exists():
                shutil.copy2(target, backup)
            shutil.copy2(old, target)
    except (OSError, sqlite3.Error):
        # The journal stays behind so the next launch undoes what was written.
        return None
    return OldItemVisuals(db_path, journal_path)


def discard_old_item_visuals(client_working_dir: Path) -> None:
    """Undo an old-style patch left behind by an earlier launch, if any."""
    root = client_data_root(client_working_dir)
    if root is None:
        return
    db_path = root / "db" / "static.db4"
    journal_path = db_path.with_name(db_path.name + _JOURNAL_SUFFIX)
    journal = _load_journal(journal_path)
    if journal is None:
        return
    if _pid_running(int(journal.get("client_pid") or 0)):
        return
    restore_old_item_visuals(OldItemVisuals(db_path, journal_path))


def remember_client_process(visuals: OldItemVisuals, pid: int) -> None:
    """Record the client's pid so a second launch cannot undo a live patch."""
    journal = _load_journal(visuals.journal_path)
    if journal is None:
        return
    journal["client_pid"] = int(pid)
    try:
        _write_journal(visuals.journal_path, journal)
    except OSError:
        pass


def restore_old_item_visuals(visuals: OldItemVisuals) -> None:
    """Put the client's own item database and rarity backgrounds back."""
    journal = _load_journal(visuals.journal_path)
    if journal is None:
        return

    root = visuals.db_path.parent.parent
    restored = True
    db_backup = visuals.db_path.with_name(visuals.db_path.name + _BACKUP_SUFFIX)
    try:
        if db_backup.is_file():
            shutil.copy2(db_backup, visuals.db_path)
            db_backup.unlink(missing_ok=True)
    except OSError:
        restored = False

    for rarity in journal.get("backgrounds") or ():
        target = _background_target(root, str(rarity))
        backup = target.with_name(target.name + _BACKUP_SUFFIX)
        try:
            if backup.is_file():
                shutil.copy2(backup, target)
                backup.unlink(missing_ok=True)
        except OSError:
            restored = False

    if restored:
        try:
            visuals.journal_path.unlink(missing_ok=True)
        except OSError:
            pass
