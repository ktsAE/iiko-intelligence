"""Stage and verify stable GitHub releases alongside the running Windows connector."""
from __future__ import annotations

import argparse
import hashlib
from http.client import HTTPException
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
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile

from . import __version__, config
from .database import write_lock

from .release_trust import (ARCHIVE, MAX_BYTES, MAX_ENTRIES, MAX_EXPANDED, MAX_RECEIPT, REPOSITORY,
                            SIGNATURE, TAG, file_hash, is_link, verify_receipt, verify_tree)

LATEST = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
DAY = 24 * 3600
RETRY = 15 * 60


_rejected_tag: str | None = None


class ReleaseRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if (parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or parsed.hostname not in
                {"github.com", "api.github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}):
            raise ValueError("Unexpected GitHub release redirect.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_release(url, *, timeout):
    return build_opener(ReleaseRedirect()).open(url, timeout=timeout)


def read_signature(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or is_link(info) or info.st_size > MAX_RECEIPT:
        raise ValueError("Invalid installed signature file.")
    with path.open("rb") as stream:
        return stream.read(MAX_RECEIPT + 1)


def version(tag: str) -> tuple[int, ...]:
    match = TAG.fullmatch(tag) if isinstance(tag, str) else None
    if not match:
        raise ValueError("Unexpected release version.")
    return tuple(map(int, match.groups()))


def managed() -> bool:
    return os.name == "nt" and (config.PROJECT_ROOT / "runtime/python.exe").is_file() and not (config.PROJECT_ROOT / ".git").exists()


def read_state(folder: Path) -> dict:
    try:
        with (folder / "state.json").open("rb") as state_file:
            raw = state_file.read(8193)
        value = json.loads(raw) if len(raw) <= 8192 else {}
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
    global _rejected_tag
    state = read_state(folder)
    tag = selected(folder, base, state)
    desired = state.get("selected")
    if tag == base and isinstance(desired, str) and TAG.fullmatch(desired) and version(desired) > version(base):
        _rejected_tag = desired
    if tag != base:
        try:
            receipt = verify_receipt(read_signature(folder / (tag + ".sig")), tag)
            verify_tree(folder / tag, receipt)
        except (OSError, ValueError):
            _rejected_tag = tag
            return
        runner = folder / tag / "runtime/python.exe"
        raise SystemExit(subprocess.call([str(runner), "-B", "-m", "iiko_intelligence." + module, *sys.argv[1:]]))


def update_status() -> dict:
    current = "v" + __version__
    if not managed():
        return {"running": current, "status": "unmanaged"}
    folder = config.PROJECT_ROOT / ".iiko-updates"
    state = read_state(folder)
    ready = selected(folder, current, state)
    result = {k: state[k] for k in ("latest", "checked_at", "reason") if k in state}
    result.update(running=current, status=state.get("status", "not_checked"))
    if _rejected_tag is not None and state.get("selected") == _rejected_tag:
        result.update(status="installed_files_rejected", message="Проверка файлов обновления не прошла. Используется исходная версия.")
    elif ready != current:
        result.update(status="restart_required", ready=ready,
                      message="Обновление готово. Откройте новую локальную сессию Codex в этой папке.")
    elif result["status"] == "ready":
        result.update(status="updated", message=f"Коннектор обновлён до {current}.")
    return result


def with_update_notice(result: dict) -> dict:
    status = update_status()
    if status["status"] in {"restart_required", "installed_files_rejected"}:
        result["software_update"] = status
    return result


def latest_release(open_url=open_release) -> dict:
    request = Request(LATEST, headers={"User-Agent": "iiko-intelligence-updater", "Accept": "application/vnd.github+json"})
    with open_url(request, timeout=15) as response:
        data = response.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("Release metadata exceeds the size limit.")
    try:
        release = json.loads(data)
    except RecursionError as exc:
        raise ValueError("Invalid GitHub release metadata.") from exc
    if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
        raise ValueError("GitHub did not confirm a stable release.")
    version(release.get("tag_name", ""))
    return release


def release_asset(release: dict, name: str, max_size: int) -> tuple[str, str, int]:
    tag = release["tag_name"]
    version(tag)
    entries = release.get("assets")
    if not isinstance(entries, list) or any(not isinstance(asset, dict) for asset in entries):
        raise ValueError("Invalid GitHub asset list.")
    assets = [asset for asset in entries if asset.get("name") == name]
    if len(assets) != 1:
        raise ValueError("The signed release is missing a required asset.")
    asset = assets[0]
    url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
    expected, size = asset.get("digest", ""), asset.get("size")
    if (asset.get("browser_download_url") != url or not isinstance(expected, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected) or type(size) is not int or not 0 < size <= max_size):
        raise ValueError("Invalid release asset metadata.")
    return url, expected, size


def download(release: dict, target: Path, open_url=open_release) -> bytes:
    if release.get("immutable") is not True:
        raise ValueError("Only immutable releases are eligible for automatic updates.")
    url, expected, size = release_asset(release, ARCHIVE, MAX_BYTES)
    sig_url, sig_digest, sig_size = release_asset(release, SIGNATURE, MAX_RECEIPT)
    with open_url(sig_url, timeout=15) as source:
        raw = source.read(MAX_RECEIPT + 1)
    if len(raw) != sig_size or "sha256:" + hashlib.sha256(raw).hexdigest() != sig_digest:
        raise ValueError("Invalid signature asset.")
    record = verify_receipt(raw, release["tag_name"])
    if record.size != size or "sha256:" + record.sha256 != expected:
        raise ValueError("GitHub metadata differs from the publisher's signed release.")
    digest = hashlib.sha256()
    with open_url(url, timeout=45) as source, target.open("wb") as output:
        while block := source.read(128 * 1024):
            if output.tell() + len(block) > size:
                raise ValueError("Release size mismatch.")
            output.write(block)
            digest.update(block)
        if output.tell() != size or f"sha256:{digest.hexdigest()}" != expected:
            raise ValueError("Release verification failed.")
    return raw


def extract(archive: Path, work: Path, tag: str) -> Path:
    with ZipFile(archive) as bundle:
        seen = set()
        total = 0
        for member in bundle.infolist():
            name = member.filename.rstrip("/")
            parts = name.split("/")
            if (member.filename != member.orig_filename or member.flag_bits & 1 or not parts or parts[0] != "iiko-intelligence" or "\\" in name or ":" in name
                    or any(ord(c) < 32 or c in '<>"|?*' for c in name)
                    or any(not part or part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved() for part in parts)
                    or stat.S_ISLNK(member.external_attr >> 16) or name.casefold() in seen):
                raise ValueError("Unexpected path in release archive.")
            seen.add(name.casefold())
            total += member.file_size
            if total > MAX_EXPANDED or len(seen) > MAX_ENTRIES:
                raise ValueError("Release contents exceed the size limit.")
        bundle.extractall(work)
    app = work / "iiko-intelligence"
    project = tomllib.loads((app / "pyproject.toml").read_text(encoding="utf-8"))
    if (project["project"]["name"] != "iiko-intelligence-trial" or "v" + project["project"]["version"] != tag
            or project.get("tool", {}).get("iiko", {}).get("update_layout") != 2
            or not (app / "runtime/python.exe").is_file()):
        raise ValueError("Package identity, version or update layout mismatch.")
    return app


def probe(candidate: Path, tag: str) -> None:
    # This is a functional check of already authenticated code. Use disposable demo data.
    code = ("from iiko_intelligence import __version__, config; from iiko_intelligence.seed import seed_demo; "
            "from iiko_intelligence.connect import verify_connection; from pathlib import Path; import sys; "
            "assert 'v'+__version__ == sys.argv[1] and config.PROJECT_ROOT == config.CODE_ROOT; "
            "db=Path(sys.argv[2]); seed_demo(db, days=7); verify_connection(db)")
    with tempfile.TemporaryDirectory(prefix="iiko-probe-") as tmp:
        env = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC") if k in os.environ}
        env.update(TEMP=tmp, TMP=tmp, USERPROFILE=tmp, LOCALAPPDATA=tmp, APPDATA=tmp, IIKO_NO_UPDATE_CHECK="1")
        result = subprocess.run([str(candidate / "runtime/python.exe"), "-B", "-c", code, tag, str(Path(tmp) / "demo.sqlite")],
                                cwd=tmp, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
    if result.returncode:
        raise ValueError("The new version failed its local MCP check.")


def refresh(root: Path, current: str, *, force=False, fetch=latest_release, retrieve=download, verify=probe) -> dict:
    folder = root / ".iiko-updates"
    folder.mkdir(exist_ok=True)
    if is_link(folder.lstat()):
        raise ValueError("Linked update storage is unsupported.")
    # SQLite releases this lock when a process exits, including an interrupted download.
    try:
        with write_lock(folder / "update"):
            state = read_state(folder)
            now = time.time()
            delay = DAY if state.get("status") in {"current", "ready"} else RETRY
            attempt = state.get("attempt", 0)
            if not force and isinstance(attempt, (int, float)) and 0 <= now - attempt < delay:
                return state
            installed = selected(folder, current, state)
            state.update(status="checking", attempt=now)
            state.pop("reason", None)
            save_state(folder, state)
            try:
                release = fetch()
                tag = release["tag_name"]
                available = version(tag)
                state.update(latest=tag, checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
                if available <= version(installed):
                    state["status"] = "current"
                else:
                    state["status"] = "downloading"
                    save_state(folder, state)
                    candidate = folder / tag
                    with tempfile.TemporaryDirectory(dir=folder, prefix="stage-") as directory:
                        work = Path(directory)
                        archive = work / "release.zip"
                        raw = retrieve(release, archive)
                        record = verify_receipt(raw, tag)
                        with archive.open("rb") as stream:
                            if archive.stat().st_size != record.size or file_hash(stream) != record.sha256:
                                raise ValueError("Package differs from the signed release.")
                        staged = extract(archive, work, tag)
                        verify_tree(staged, record)
                        verify(staged, tag)
                        verify_tree(staged, record)
                        if candidate.exists():
                            if is_link(candidate.lstat()):
                                raise ValueError("Linked release folder is unsupported.")
                            shutil.rmtree(candidate)
                        staged.rename(candidate)
                        signature = work / "publisher.sig"
                        signature.write_bytes(raw)
                        signature.replace(folder / (tag + ".sig"))
                    state.update(selected=tag, status="ready")
            except ValueError:
                # Never return remotely supplied strings (e.g. a malformed tag or a redirect URL) as chat instructions.
                state.update(status="check_failed", reason="Release validation failed; the installed version is still available.")
            except (OSError, URLError, KeyError, TypeError, BadZipFile, HTTPException, subprocess.SubprocessError):
                state.update(status="check_failed", reason="The signed update could not be downloaded or tested.")
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
