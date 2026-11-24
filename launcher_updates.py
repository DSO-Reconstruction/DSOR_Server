"""GitHub activity and optional-Git server update helpers for the launcher."""

from __future__ import annotations

import json
import os
import filecmp
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
import zipfile
from pathlib import Path, PurePosixPath
from urllib import request as urlrequest


GITHUB_API = "https://api.github.com"
GITHUB_ORG = "DSO-Reconstruction"
SERVER_REPO = "DSOR_Server"
USER_AGENT = "DSOR-Launcher"
REVISION_FILE = ".dsor_server_revision"
MANIFEST_FILE = ".dsor_server_manifest.json"


def _get_json(url: str):
    request = urlrequest.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    with urlrequest.urlopen(request, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


def latest_server_commit() -> dict:
    payload = _get_json(
        f"{GITHUB_API}/repos/{GITHUB_ORG}/{SERVER_REPO}/commits/main"
    )
    commit = payload.get("commit", {})
    author = commit.get("author", {})
    return {
        "branch": "main",
        "sha": str(payload.get("sha", "")),
        "message": str(commit.get("message", "")),
        "date": str(author.get("date", "")),
        "url": payload.get("html_url", ""),
    }


def is_server_root(repo_dir: Path) -> bool:
    return (
        (repo_dir / "app.py").is_file()
        and (repo_dir / "setup_client_assets.py").is_file()
        and (repo_dir / "data").is_dir()
    )


def _git_head(repo_dir: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=8,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def local_head(repo_dir: Path) -> str | None:
    """Return the Git revision or the revision recorded by an archive update."""
    try:
        revision = (repo_dir / REVISION_FILE).read_text(encoding="ascii").strip()
    except OSError:
        revision = ""
    if revision:
        return revision
    return _git_head(repo_dir)


def _backup_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return base / "DSORLauncher" / "update-backups" / stamp


def _safe_relative_path(repo_dir: Path, relative: str) -> Path:
    posix = PurePosixPath(relative)
    if (
        posix.is_absolute()
        or not posix.parts
        or any(part in ("", ".", "..") or ":" in part for part in posix.parts)
    ):
        raise ValueError(f"Unsafe path in GitHub update archive: {relative!r}")
    target = repo_dir.joinpath(*posix.parts)
    if not target.resolve().is_relative_to(repo_dir.resolve()):
        raise ValueError(f"Update path escapes the server folder: {relative!r}")
    return target


def _load_previous_manifest(repo_dir: Path) -> set[str]:
    try:
        payload = json.loads((repo_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    files = payload.get("files", []) if isinstance(payload, dict) else []
    return {item for item in files if isinstance(item, str)}


def _archive_update(repo_dir: Path, commit_sha: str) -> str:
    if not is_server_root(repo_dir):
        raise RuntimeError("Launcher folder is not a DSOR_Server installation.")
    if not commit_sha or any(char not in "0123456789abcdef" for char in commit_sha.lower()):
        raise ValueError("GitHub returned an invalid server revision.")

    repo_dir = repo_dir.resolve()
    previous_files = _load_previous_manifest(repo_dir)
    archive_url = (
        f"https://api.github.com/repos/{GITHUB_ORG}/{SERVER_REPO}"
        f"/zipball/{commit_sha}"
    )

    with tempfile.TemporaryDirectory(prefix=".dsor-update-", dir=repo_dir) as temp_name:
        work_dir = Path(temp_name)
        archive_path = work_dir / "server.zip"
        request = urlrequest.Request(
            archive_url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
        )
        with urlrequest.urlopen(request, timeout=120) as response, archive_path.open("wb") as target:
            shutil.copyfileobj(response, target)

        extracted = work_dir / "extracted"
        extracted.mkdir()
        installed_files: set[str] = set()
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            roots = {
                PurePosixPath(item.filename).parts[0]
                for item in entries if PurePosixPath(item.filename).parts
            }
            if len(roots) != 1:
                raise ValueError("GitHub update archive has an unexpected folder layout.")
            archive_root = next(iter(roots))
            for item in entries:
                path = PurePosixPath(item.filename)
                if item.is_dir() or len(path.parts) <= 1:
                    continue
                if path.parts[0] != archive_root:
                    raise ValueError("GitHub update archive contains multiple root folders.")
                relative = PurePosixPath(*path.parts[1:]).as_posix()
                _safe_relative_path(extracted, relative)
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValueError(f"Symbolic links are not allowed in update archives: {relative}")
                destination = extracted.joinpath(*PurePosixPath(relative).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
                installed_files.add(relative)

        backup_root = work_dir / "backup"
        saved_root = _backup_root() / repo_dir.name
        saved_files: set[str] = set()
        changed: list[str] = []
        removed: list[str] = []
        metadata_names = (REVISION_FILE, MANIFEST_FILE)
        try:
            for relative in sorted(installed_files):
                source = extracted.joinpath(*PurePosixPath(relative).parts)
                target = _safe_relative_path(repo_dir, relative)
                if target.is_file():
                    if not target.is_symlink() and not filecmp.cmp(target, source, shallow=False):
                        saved = saved_root.joinpath(*PurePosixPath(relative).parts)
                        saved.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(target, saved)
                        saved_files.add(relative)
                    backup = backup_root.joinpath(*PurePosixPath(relative).parts)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
                changed.append(relative)

            for relative in sorted(previous_files - installed_files, reverse=True):
                target = _safe_relative_path(repo_dir, relative)
                if not target.is_file():
                    continue
                saved = saved_root.joinpath(*PurePosixPath(relative).parts)
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
                saved_files.add(relative)
                backup = backup_root.joinpath(*PurePosixPath(relative).parts)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                target.unlink()
                removed.append(relative)

            for name in metadata_names:
                target = repo_dir / name
                if target.is_file():
                    backup = backup_root / name
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
            revision_tmp = work_dir / "revision.tmp"
            revision_tmp.write_text(commit_sha + "\n", encoding="ascii")
            os.replace(revision_tmp, repo_dir / REVISION_FILE)
            manifest_tmp = work_dir / "manifest.tmp"
            manifest_tmp.write_text(
                json.dumps({"revision": commit_sha, "files": sorted(installed_files)}, indent=2)
                + "\n",
                encoding="utf-8",
            )
            os.replace(manifest_tmp, repo_dir / MANIFEST_FILE)
            if saved_files:
                saved_root.mkdir(parents=True, exist_ok=True)
                (saved_root / "backup-manifest.json").write_text(
                    json.dumps({"revision": commit_sha, "files": sorted(saved_files)}, indent=2)
                    + "\n",
                    encoding="utf-8",
                )
        except Exception:
            for relative in reversed(changed + removed):
                target = _safe_relative_path(repo_dir, relative)
                backup = backup_root.joinpath(*PurePosixPath(relative).parts)
                if backup.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, target)
                else:
                    target.unlink(missing_ok=True)
            for name in metadata_names:
                backup = backup_root / name
                target = repo_dir / name
                if backup.is_file():
                    shutil.copy2(backup, target)
                else:
                    target.unlink(missing_ok=True)
            raise

    return commit_sha


def apply_server_update(repo_dir: Path, branch: str, commit_sha: str) -> str:
    """Fast-forward a clean Git checkout, otherwise install a GitHub archive."""
    repo_dir = repo_dir.resolve()
    git_revision = _git_head(repo_dir)
    if git_revision:
        status = subprocess.run(
            ["git", "-C", str(repo_dir), "status", "--porcelain"],
            check=True, capture_output=True, text=True, timeout=8,
        )
        has_archive_revision = (repo_dir / REVISION_FILE).is_file()
        if status.stdout.strip() or has_archive_revision:
            return _archive_update(repo_dir, commit_sha)
        subprocess.run(
            ["git", "-C", str(repo_dir), "fetch", "origin", branch],
            check=True, capture_output=True, text=True, timeout=120,
        )
        subprocess.run(
            ["git", "-C", str(repo_dir), "merge", "--ff-only", "FETCH_HEAD"],
            check=True, capture_output=True, text=True, timeout=120,
        )
        (repo_dir / REVISION_FILE).unlink(missing_ok=True)
        return _git_head(repo_dir) or commit_sha
    return _archive_update(repo_dir, commit_sha)
