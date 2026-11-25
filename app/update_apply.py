from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SKIP_TOP = {"data", ".git", "node_modules", "__pycache__", ".pytest_cache"}
SKIP_NAMES = {".env", "master.key", "admin-password.txt", "trae_mouth.lock", ".zcode.md"}


def log(log_path: Path, message: str) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
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


def swap_dir(new_dir: Path, target: Path) -> None:
    old = target.with_name(target.name + ".old")
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


def apply_tree(staging: Path, root: Path, log_path: Path) -> None:
    app_new = staging / "app"
    if app_new.is_dir():
        swap_dir(app_new, root / "app")
        log(log_path, "swapped app/")

    dist_new = staging / "admin-web" / "dist"
    if dist_new.is_dir():
        swap_dir(dist_new, root / "admin-web" / "dist")
        log(log_path, "swapped admin-web/dist/")

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
        copy_file(source, root / relative, log_path)
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
    args = parser.parse_args()

    root = Path(args.root).resolve()
    staging = Path(args.staging).resolve()
    archive = Path(args.archive)
    log_path = (
        Path(args.log)
        if args.log
        else root / "data" / "update" / "update.log"
    )

    log(log_path, f"updater started pid={args.pid} root={root}")
    wait_for_exit(max(0, args.pid))
    try:
        apply_tree(staging, root, log_path)
        log(log_path, "update applied")
    except Exception as exc:
        log(log_path, f"update failed: {exc}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        start_server(root, args.python, log_path)
    except Exception as exc:
        log(log_path, f"restart failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
