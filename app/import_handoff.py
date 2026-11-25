from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from .config import Config

FILENAME = "last_import_code.json"


def handoff_paths(config: Config) -> list[Path]:
    paths = [config.data_dir / FILENAME]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        paths.append(Path(local) / "TraeMouth" / FILENAME)
    return paths


def write(config: Config, code: str, expires_at: int, relay: str) -> None:
    payload = {
        "code": code,
        "expires_at": int(expires_at),
        "relay": relay,
        "created_at": int(time.time()),
    }
    body = json.dumps(payload, ensure_ascii=False)
    for path in handoff_paths(config):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + ".tmp")
            temp.write_text(body, encoding="utf-8")
            os.replace(temp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError:
            continue


def read(config: Config) -> Optional[dict[str, Any]]:
    for path in handoff_paths(config):
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if int(data.get("expires_at") or 0) <= int(time.time()):
            continue
        if data.get("code"):
            return data
    return None


def clear(config: Config, code: str = "") -> None:
    for path in handoff_paths(config):
        try:
            if not path.exists():
                continue
            if code:
                try:
                    data = json.loads(path.read_text("utf-8"))
                except (OSError, ValueError):
                    data = {}
                if data.get("code") and data.get("code") != code:
                    continue
            path.unlink()
        except OSError:
            continue


def clear_expired(config: Config) -> None:
    now = int(time.time())
    for path in handoff_paths(config):
        try:
            data = json.loads(path.read_text("utf-8"))
            if int(data.get("expires_at") or 0) <= now:
                path.unlink()
        except (OSError, ValueError):
            continue
