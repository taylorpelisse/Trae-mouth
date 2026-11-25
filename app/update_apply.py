from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

SKIP_TOP = {"data", ".git", "node_modules", "__pycache__", ".pytest_cache"}
SKIP_NAMES = {".env", "master.key", "admin-password.txt", "trae_mouth.lock", ".zcode.md"}
# 拷贝其余文件占 70-95，接在解压（0-40）和两次目录替换之后
COPY_START = 70
COPY_END = 95


def log(log_path: Path, message: str) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def write_state(path: Path, **changes: Any) -> None:
    # 与 app/update.py 共用同一份进度文件；脚本独立运行，不引用 app 包
    try:
        data = json.loads(path.read_text("utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            return
        data.update(changes, updated_at=int(time.time()))
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, ValueError):
        pass


def wait_for_exit(pid: int, timeout: float = 90.0) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        try:
            import ctypes

            synchronize = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
            if handle:
                ctypes.windll.kernel32.WaitForSingleObject(
                    handle, int(timeout * 1000)
                )
                ctypes.windll.kernel32.CloseHandle(handle)
                return
            time.sleep(3)
            return
        except Exception:
            time.sleep(3)
            return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)


def stop_server(pid: int, log_path: Path) -> None:
    # 更新期间旧进程继续提供服务，装完才结束它
    if pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
    except OSError as exc:
        log(log_path, f"stop pid={pid} failed: {exc}")
    wait_for_exit(pid)


def swap_dir(new_dir: Path, target: Path) -> None:
    old = target.with_name(target.name + ".old")
    target.parent.mkdir(parents=True, exist_ok=True)
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        os.replace(target, old)
    try:
        os.replace(new_dir, target)
    except Exception:
        if old.exists() and not target.exists():
            os.replace(old, target)
        raise
    shutil.rmtree(old, ignore_errors=True)


def copy_file(source: Path, target: Path, log_path: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for _ in range(3):
        try:
            shutil.copy2(source, target)
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"copy failed: {source} -> {target}: {last_error}")


def apply_tree(
    staging: Path, root: Path, log_path: Path, progress: Callable[[int, str], None]
) -> None:
    app_new = staging / "app"
    if app_new.is_dir():
        progress(55, "正在替换 app/")
        swap_dir(app_new, root / "app")
        log(log_path, "swapped app/")

    dist_new = staging / "admin-web" / "dist"
    if dist_new.is_dir():
        progress(70, "正在替换 admin-web/dist/")
        swap_dir(dist_new, root / "admin-web" / "dist")
        log(log_path, "swapped admin-web/dist/")

    files: list[tuple[Path, Path]] = []
    for source in staging.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(staging)
        parts = relative.parts
        if not parts or parts[0] in SKIP_TOP or parts[0] == "app":
            continue
        if parts[:2] == ("admin-web", "dist"):
            continue
        if parts[:2] == ("tools", "workspace"):
            continue
        if source.name in SKIP_NAMES:
            continue
        files.append((source, root / relative))
    for index, (source, target) in enumerate(files, start=1):
        copy_file(source, target, log_path)
        span = COPY_END - COPY_START
        progress(COPY_START + span * index // max(len(files), 1), "正在拷贝文件")
    log(log_path, "copied remaining files")


def start_server(root: Path, python: str, log_path: Path) -> None:
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    with log_path.open("ab", buffering=0) as handle:
        subprocess.Popen(
            [python, "run.py"],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            close_fds=True,
            **kwargs,
        )
    log(log_path, "server restarted")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--staging", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--pid", type=int, default=0)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--log", default="")
    parser.add_argument("--state", default="")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    staging = Path(args.staging).resolve()
    archive = Path(args.archive)
    log_path = (
        Path(args.log)
        if args.log
        else root / "data" / "update" / "update.log"
    )
    state_path = Path(args.state) if args.state else None

    def progress(percent: int, detail: str = "", status: str = "installing") -> None:
        if state_path is not None:
            write_state(
                state_path, status=status, percent=percent, detail=detail, error=""
            )

    log(log_path, f"updater started pid={args.pid} root={root}")
    error = ""
    try:
        apply_tree(staging, root, log_path, progress)
        log(log_path, "update applied")
    except Exception as exc:
        error = f"安装失败：{str(exc)[:200]}"
        log(log_path, f"update failed: {exc}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass
    if error:
        # 保留失败原因，启动后的版本核对不会再覆盖它
        if state_path is not None:
            write_state(state_path, status="failed", percent=None, detail="", error=error)
    else:
        progress(100, "正在重启服务", "restarting")
    stop_server(max(0, args.pid), log_path)
    try:
        start_server(root, args.python, log_path)
    except Exception as exc:
        log(log_path, f"restart failed: {exc}")
        if state_path is not None and not error:
            write_state(state_path, status="failed", error=f"重启失败：{str(exc)[:200]}")
        return 1
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
