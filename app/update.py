from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

REPO = "taylorpelisse/Trae-mouth"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
CACHE_TTL = 86400
MAX_ZIP_BYTES = 200 * 1024 * 1024
MAX_EXTRACT_BYTES = 300 * 1024 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024
USER_AGENT = "Trae-Mouth-Updater"

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "data": None}
_apply_lock = threading.Lock()
_applying = False

_SKIP_TOP_DIRS = {"data", ".git", "node_modules", "__pycache__", ".pytest_cache"}
_SKIP_EXACT = {".env", "master.key", "admin-password.txt", "trae_mouth.lock", ".zcode.md"}


def _version(value: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+(?:\.\d+)*)", str(value or "").strip().lstrip("vV"))
    if not match:
        return ()
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _headers() -> dict[str, str]:
    return {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}


def check_update(current: str, *, force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get("data")
        if not force and cached is not None and now - float(_cache["at"]) < CACHE_TTL:
            return dict(cached)

    result: dict[str, Any] = {
        "ok": False,
        "current": current,
        "latest": "",
        "available": False,
        "error": "",
    }
    try:
        with httpx.Client(
            timeout=10, follow_redirects=True, headers=_headers()
        ) as client:
            response = client.get(RELEASES_URL)
            response.raise_for_status()
            data = response.json()
        latest = str(data.get("tag_name") or "").strip()
        assets = data.get("assets") if isinstance(data.get("assets"), list) else []
        zip_asset = next(
            (item for item in assets if str(item.get("name", "")).endswith(".zip")),
            None,
        )
        sha_asset = next(
            (
                item
                for item in assets
                if str(item.get("name", "")).endswith(".zip.sha256")
            ),
            None,
        )
        latest_version = _version(latest)
        result.update(
            {
                "ok": True,
                "latest": latest,
                "available": bool(
                    latest_version and latest_version > _version(current)
                ),
                "notes": str(data.get("body") or "")[:20000],
                "release_url": str(data.get("html_url") or ""),
                "published_at": str(data.get("published_at") or ""),
                "zip_url": str((zip_asset or {}).get("browser_download_url") or ""),
                "sha_url": str((sha_asset or {}).get("browser_download_url") or ""),
            }
        )
        if result["available"] and (not result["zip_url"] or not result["sha_url"]):
            result["available"] = False
            result["error"] = "release assets missing"
    except Exception as exc:
        result["error"] = str(exc)[:300]

    with _cache_lock:
        _cache["at"] = time.monotonic()
        _cache["data"] = result
    return dict(result)


def _fetch_checksum(client: httpx.Client, url: str) -> str:
    response = client.get(url)
    response.raise_for_status()
    match = re.search(r"\b([0-9a-fA-F]{64})\b", response.text)
    return match.group(1).lower() if match else ""


def _download(client: httpx.Client, url: str, target: Path) -> str:
    digest = hashlib.sha256()
    total = 0
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_bytes(65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_ZIP_BYTES:
                    raise ValueError("release archive is too large")
                handle.write(chunk)
                digest.update(chunk)
    return digest.hexdigest()


def _extract(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(archive_path) as archive:
        for item in archive.infolist():
            if item.is_dir():
                continue
            name = item.filename.replace("\\", "/")
            parts = [part for part in name.split("/") if part not in ("", ".")]
            if not parts or any(part == ".." for part in parts):
                continue
            if parts[0] in _SKIP_TOP_DIRS:
                continue
            if name.startswith("tools/workspace/") or name in _SKIP_EXACT:
                continue
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode):
                continue
            if item.file_size > MAX_FILE_BYTES:
                raise ValueError(f"release file is too large: {name}")
            total += item.file_size
            if total > MAX_EXTRACT_BYTES:
                raise ValueError("release contents are too large")
            target = destination.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, 65536)


def _prepare(config: Any, info: dict[str, Any]) -> tuple[Path, Path]:
    update_dir = Path(config.data_dir) / "update"
    update_dir.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"[^A-Za-z0-9._-]+", "-", str(info["latest"]).lstrip("vV")) or "latest"
    archive_path = update_dir / f"release-{tag}.zip"
    staging = update_dir / f"staging-{tag}"
    with httpx.Client(
        timeout=120, follow_redirects=True, headers=_headers()
    ) as client:
        actual = _download(client, info["zip_url"], archive_path)
        expected = _fetch_checksum(client, info["sha_url"])
    if not expected or actual != expected:
        archive_path.unlink(missing_ok=True)
        raise ValueError("release checksum mismatch")
    if staging.exists():
        shutil.rmtree(staging)
    _extract(archive_path, staging)
    return archive_path, staging


def apply_update(config: Any, current: str) -> dict[str, Any]:
    global _applying
    with _apply_lock:
        if _applying:
            return {"ok": False, "error": "update already running"}
        _applying = True

    info = check_update(current, force=True)
    if not info.get("available"):
        _applying = False
        return {
            "ok": False,
            "error": info.get("error") or "no_update",
            "current": current,
            "latest": info.get("latest", ""),
        }

    try:
        archive_path, staging = _prepare(config, info)
        updater = Path(config.root) / "app" / "update_apply.py"
        if not updater.exists():
            raise RuntimeError("updater script missing")
        update_dir = Path(config.data_dir) / "update"
        runner = update_dir / "apply.py"
        shutil.copyfile(updater, runner)
        log_path = update_dir / "update.log"
        log = log_path.open("ab", buffering=0)
        command = [
            sys.executable,
            "-I",
            str(runner),
            "--root",
            str(config.root),
            "--staging",
            str(staging),
            "--archive",
            str(archive_path),
            "--pid",
            str(os.getpid()),
            "--python",
            sys.executable,
            "--log",
            str(update_dir / "update.log"),
        ]
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True
        try:
            subprocess.Popen(
                command,
                cwd=str(config.root),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                close_fds=True,
                **kwargs,
            )
        finally:
            log.close()
    except Exception:
        _applying = False
        raise

    threading.Timer(2.5, lambda: os._exit(0)).start()
    return {
        "ok": True,
        "started": True,
        "current": current,
        "latest": info["latest"],
    }
