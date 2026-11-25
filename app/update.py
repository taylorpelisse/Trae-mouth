from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
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

STATE_FILE = "state.json"
# 已结束的更新记录保留一天，供页面刷新后展示结果
TERMINAL_TTL = 86400
JOB_STATUSES = ("downloading", "installing", "restarting", "done", "failed")
ACTIVE_STATUSES = ("downloading", "installing", "restarting")
# 安装阶段的进度：解压占 0-40，其余由 update_apply.py 按步骤推进到 100
EXTRACT_START = 0
EXTRACT_END = 40

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


# -- progress state ---------------------------------------------------------
# 进度保存在 data/update/state.json，页面刷新或换设备后读到的都是同一份进度。


def _state_path(config: Any) -> Path:
    return Path(config.data_dir) / "update" / STATE_FILE


def _write_job(path: Path, job: dict[str, Any]) -> None:
    job["updated_at"] = int(time.time())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(job, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        # 进度只用于展示，写失败不影响更新本身
        pass


def _read_job(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("status") not in JOB_STATUSES:
        return None
    if (
        data["status"] not in ACTIVE_STATUSES
        and time.time() - float(data.get("updated_at") or 0) > TERMINAL_TTL
    ):
        return None
    return data


def read_job(config: Any) -> dict[str, Any] | None:
    return _read_job(_state_path(config))


def clear_job(config: Any) -> None:
    """页面确认过结果后清除记录；进行中的更新不能被清除。"""
    path = _state_path(config)
    job = _read_job(path)
    if job and job.get("status") not in ACTIVE_STATUSES:
        try:
            path.unlink()
        except OSError:
            pass


def reconcile(config: Any, current: str) -> dict[str, Any] | None:
    """服务启动时核对没走到头的更新：版本到位记为完成，否则记为失败。"""
    path = _state_path(config)
    job = _read_job(path)
    if not job or job.get("status") not in ACTIVE_STATUSES:
        return job
    if _version(current) >= _version(str(job.get("target") or "")):
        job.update(status="done", percent=100, detail="", error="")
    else:
        job.update(
            status="failed",
            percent=None,
            detail="",
            error=(
                "服务重启后版本未变化，请查看 data/update/update.log"
                if job.get("status") == "restarting"
                else "更新过程中服务被中断，请重新更新"
            ),
        )
    _write_job(path, job)
    return job


def _progress(
    job: dict[str, Any],
    path: Path,
    status: str,
    percent: int | None,
    detail: str = "",
) -> None:
    job.update(status=status, percent=percent, detail=detail)
    _write_job(path, job)


def _fetch_checksum(client: httpx.Client, url: str) -> str:
    response = client.get(url)
    response.raise_for_status()
    match = re.search(r"\b([0-9a-fA-F]{64})\b", response.text)
    return match.group(1).lower() if match else ""


def _download(
    client: httpx.Client,
    url: str,
    target: Path,
    on_progress: Any = None,
) -> str:
    digest = hashlib.sha256()
    total = 0
    with client.stream("GET", url) as response:
        response.raise_for_status()
        try:
            expected = int(response.headers.get("content-length", "") or 0)
        except ValueError:
            expected = 0
        with target.open("wb") as handle:
            for chunk in response.iter_bytes(65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_ZIP_BYTES:
                    raise ValueError("release archive is too large")
                handle.write(chunk)
                digest.update(chunk)
                # 下载完还要校验，所以最多报到 99
                if on_progress is not None and expected > 0:
                    on_progress(min(99, total * 100 // expected))
    return digest.hexdigest()


def _extract(archive_path: Path, destination: Path, on_progress: Any = None) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(archive_path) as archive:
        entries = [item for item in archive.infolist() if not item.is_dir()]
        for index, item in enumerate(entries, start=1):
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
            if on_progress is not None:
                span = EXTRACT_END - EXTRACT_START
                on_progress(EXTRACT_START + span * index // len(entries))


def _prepare(
    config: Any,
    info: dict[str, Any],
    job: dict[str, Any],
    path: Path,
) -> tuple[Path, Path]:
    update_dir = Path(config.data_dir) / "update"
    update_dir.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"[^A-Za-z0-9._-]+", "-", str(info["latest"]).lstrip("vV")) or "latest"
    archive_path = update_dir / f"release-{tag}.zip"
    staging = update_dir / f"staging-{tag}"
    try:
        with httpx.Client(
            timeout=120, follow_redirects=True, headers=_headers()
        ) as client:
            actual = _download(
                client,
                info["zip_url"],
                archive_path,
                lambda done: _progress(job, path, "downloading", done),
            )
            expected = _fetch_checksum(client, info["sha_url"])
    except httpx.HTTPError as exc:
        archive_path.unlink(missing_ok=True)
        status = getattr(getattr(exc, "response", None), "status_code", None)
        raise ValueError(
            f"下载安装包失败（HTTP {status}），请稍后重试"
            if status
            else "连接更新服务器失败，请检查网络后重试"
        ) from exc
    if not expected or actual != expected:
        archive_path.unlink(missing_ok=True)
        raise ValueError("安装包校验失败，请重新更新")
    if staging.exists():
        shutil.rmtree(staging)
    _progress(job, path, "installing", EXTRACT_START, "正在解压")
    _extract(
        archive_path,
        staging,
        lambda done: _progress(job, path, "installing", done, "正在解压"),
    )
    return archive_path, staging


def _launch(
    config: Any,
    archive_path: Path,
    staging: Path,
) -> None:
    updater = Path(config.root) / "app" / "update_apply.py"
    if not updater.exists():
        raise RuntimeError("updater script missing")
    update_dir = Path(config.data_dir) / "update"
    runner = update_dir / "apply.py"
    shutil.copyfile(updater, runner)
    log_path = update_dir / "update.log"
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
        "--state",
        str(update_dir / STATE_FILE),
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
    with log_path.open("ab", buffering=0) as log:
        return subprocess.Popen(
            command,
            cwd=str(config.root),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            **kwargs,
        )


def _run(config: Any, info: dict[str, Any], job: dict[str, Any]) -> None:
    global _applying
    path = _state_path(config)
    try:
        archive_path, staging = _prepare(config, info, job, path)
        process = _launch(config, archive_path, staging)
    except Exception as exc:
        job.update(status="failed", percent=None, detail="", error=str(exc)[:300])
        _write_job(path, job)
        _applying = False
        return

    # 安装脚本在本进程仍在服务时替换文件，装完再结束本进程并拉起新进程，
    # 所以安装进度可以一直被页面读到。正常情况下走不到 wait 之后。
    _progress(job, path, "installing", EXTRACT_END, "正在替换文件")
    try:
        code: object = process.wait(timeout=600)
    except subprocess.TimeoutExpired:
        code = "超时"
    except Exception:
        return
    job.update(
        status="failed",
        percent=None,
        detail="",
        error=f"安装脚本异常退出（{code}），请重新更新",
    )
    _write_job(path, job)
    _applying = False


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

    job: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "target": str(info["latest"]),
        "from": current,
        "status": "downloading",
        "percent": 0,
        "detail": "",
        "error": "",
        "started_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    _write_job(_state_path(config), job)
    threading.Thread(
        target=_run, args=(config, info, job), name="trae-mouth-update", daemon=True
    ).start()
    return {
        "ok": True,
        "started": True,
        "current": current,
        "latest": info["latest"],
        "job": job,
    }
