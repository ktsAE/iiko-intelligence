"""Stage and verify stable GitHub releases alongside the running Windows connector."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from urllib.error import URLError
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

from . import __version__, config
from .database import write_lock

REPOSITORY = "ktsAE/iiko-intelligence"
LATEST = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
ARCHIVE = "iiko-intelligence-windows-x64.zip"
TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
DAY = 24 * 3600
RETRY = 15 * 60
MAX_BYTES = 160 * 1024 * 1024


def version(tag: str) -> tuple[int, ...]:
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError("Unexpected release version.")
    return tuple(map(int, match.groups()))


def managed() -> bool:
    return os.name == "nt" and (config.PROJECT_ROOT / "runtime/python.exe").is_file() and not (config.PROJECT_ROOT / ".git").exists()


def read_state(folder: Path) -> dict:
    try:
        value = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(folder: Path, state: dict) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder, delete=False) as output:
        temporary = Path(output.name)
        json.dump(state, output)
    try:
        temporary.replace(folder / "state.json")
    finally:
        temporary.unlink(missing_ok=True)


def selected(folder: Path, base: str, state: dict) -> str:
    tag = state.get("selected")
    if (isinstance(tag, str) and TAG.fullmatch(tag) and version(tag) > version(base)
            and (folder / tag / "runtime/python.exe").is_file()):
        return tag
    return base


def dispatch(module: str) -> None:
    """Keep the original command path working for clients and Windows shortcuts."""
    if not managed() or config.CODE_ROOT != config.PROJECT_ROOT:
        return
    folder = config.PROJECT_ROOT / ".iiko-updates"
    base = "v" + __version__
    tag = selected(folder, base, read_state(folder))
    if tag != base:
        runner = folder / tag / "runtime/python.exe"
        raise SystemExit(subprocess.call([str(runner), "-m", "iiko_intelligence." + module, *sys.argv[1:]]))


def update_status() -> dict:
    current = "v" + __version__
    if not managed():
        return {"running": current, "status": "unmanaged"}
    folder = config.PROJECT_ROOT / ".iiko-updates"
    state = read_state(folder)
    ready = selected(folder, current, state)
    result = {k: state[k] for k in ("latest", "checked_at", "reason") if k in state}
    result.update(running=current, status=state.get("status", "not_checked"))
    if ready != current:
        result.update(status="restart_required", ready=ready,
                      message="Обновление готово. Откройте новую локальную сессию Codex в этой папке.")
    elif result["status"] == "ready":
        result.update(status="updated", message=f"Коннектор обновлён до {current}.")
    return result


def with_update_notice(result: dict) -> dict:
    status = update_status()
    if status["status"] == "restart_required":
        result["software_update"] = status
    return result


def latest_release(open_url=urlopen) -> dict:
    request = Request(LATEST, headers={"User-Agent": "iiko-intelligence-updater", "Accept": "application/vnd.github+json"})
    with open_url(request, timeout=15) as response:
        data = response.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("Release metadata exceeds the size limit.")
    release = json.loads(data)
    if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
        raise ValueError("GitHub did not confirm a stable release.")
    version(release.get("tag_name", ""))
    return release


def download(release: dict, target: Path, open_url=urlopen) -> None:
    tag = release["tag_name"]
    version(tag)
    assets = [asset for asset in release["assets"] if asset.get("name") == ARCHIVE]
    if len(assets) != 1:
        raise ValueError("The release has no unique Windows package.")
    asset = assets[0]
    url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{ARCHIVE}"
    expected, size = asset.get("digest", ""), asset.get("size", 0)
    if (asset.get("browser_download_url") != url or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected)
            or type(size) is not int or not 0 < size <= MAX_BYTES):
        raise ValueError("The release package has no valid size, URL or SHA-256 digest.")
    digest = hashlib.sha256()
    with open_url(url, timeout=45) as source, target.open("wb") as output:
        while block := source.read(128 * 1024):
            if output.tell() + len(block) > size:
                raise ValueError("Release size mismatch.")
            output.write(block)
            digest.update(block)
        if output.tell() != size or f"sha256:{digest.hexdigest()}" != expected:
            raise ValueError("Release verification failed.")


def extract(archive: Path, work: Path, tag: str) -> Path:
    with ZipFile(archive) as bundle:
        seen = set()
        total = 0
        for member in bundle.infolist():
            name = member.filename.rstrip("/")
            parts = name.split("/")
            if (not parts or parts[0] != "iiko-intelligence" or "\\" in name or ":" in name
                    or any(not part or part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved() for part in parts)
                    or stat.S_ISLNK(member.external_attr >> 16) or name.casefold() in seen):
                raise ValueError("Unexpected path in release archive.")
            seen.add(name.casefold())
            total += member.file_size
            if total > 640 * 1024 * 1024 or len(seen) > 30000:
                raise ValueError("Release contents exceed the size limit.")
        bundle.extractall(work)
    app = work / "iiko-intelligence"
    project = tomllib.loads((app / "pyproject.toml").read_text(encoding="utf-8"))
    if (project["project"]["name"] != "iiko-intelligence-trial" or "v" + project["project"]["version"] != tag
            or project.get("tool", {}).get("iiko", {}).get("update_layout") != 1
            or not (app / "runtime/python.exe").is_file()):
        raise ValueError("Package identity, version or update layout mismatch.")
    return app


def probe(candidate: Path, tag: str) -> None:
    # Directly verify the candidate runtime and a stdio report against the same read-only database.
    code = ("from iiko_intelligence import __version__,config; from iiko_intelligence.connect import verify_connection; "
            "import sys; assert 'v'+__version__ == sys.argv[1]; "
            "assert config.PROJECT_ROOT == config.CODE_ROOT.parent.parent; "
            "verify_connection(config.MANAGER_DATABASE)")
    result = subprocess.run([str(candidate / "runtime/python.exe"), "-c", code, tag], cwd=candidate,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
    if result.returncode:
        raise ValueError("The new version failed its local MCP check.")


def refresh(root: Path, current: str, *, force=False, fetch=latest_release, retrieve=download, verify=probe) -> dict:
    folder = root / ".iiko-updates"
    folder.mkdir(exist_ok=True)
    # SQLite releases this lock when a process exits, including an interrupted download.
    try:
        with write_lock(folder / "update"):
            state = read_state(folder)
            now = time.time()
            delay = DAY if state.get("status") in {"current", "ready"} else RETRY
            if not force and 0 <= now - state.get("attempt", 0) < delay:
                return state
            installed = selected(folder, current, state)
            state.update(status="checking", attempt=now)
            state.pop("reason", None)
            save_state(folder, state)
            try:
                release = fetch()
                tag = release["tag_name"]
                state.update(latest=tag, checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
                if version(tag) <= version(installed):
                    state["status"] = "current"
                else:
                    state["status"] = "downloading"
                    save_state(folder, state)
                    candidate = folder / tag
                    # A terminated run can leave a complete but unselected candidate.
                    if candidate.exists():
                        shutil.rmtree(candidate)
                    with tempfile.TemporaryDirectory(dir=folder, prefix="stage-") as directory:
                        work = Path(directory)
                        archive = work / "release.zip"
                        retrieve(release, archive)
                        extract(archive, work, tag).rename(candidate)
                    try:
                        verify(candidate, tag)
                    except Exception:
                        shutil.rmtree(candidate, ignore_errors=True)
                        raise
                    state.update(selected=tag, status="ready")
            except (OSError, URLError, KeyError, TypeError, ValueError, BadZipFile, subprocess.SubprocessError) as exc:
                state.update(status="check_failed", reason=str(exc)[:300])
            save_state(folder, state)
            return state
    except sqlite3.OperationalError:
        return read_state(folder)


def start_checks() -> None:
    if not managed() or os.environ.get("IIKO_NO_UPDATE_CHECK") == "1":
        return

    def poll():
        while True:
            try:
                refresh(config.PROJECT_ROOT, "v" + __version__)
            except (OSError, ValueError, sqlite3.Error):
                pass  # Local reporting remains available when the update folder cannot be written.
            time.sleep(RETRY)

    threading.Thread(target=poll, daemon=True, name="iiko-release-check").start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Check the official iiko Intelligence release and prepare an update.")
    parser.parse_args()
    if not managed():
        parser.exit(1, "Use this command in the installed Windows project.\n")
    refresh(config.PROJECT_ROOT, "v" + __version__, force=True)
    status = update_status()
    print(json.dumps(status, ensure_ascii=False))
    if status["status"] == "check_failed":
        raise SystemExit(1)


if __name__ == "__main__":
    dispatch("updates")
    main()
