from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from ..config import Config
from .models import DEVICE_SPECS, DeviceSpec, DeviceProfile, build_device


def _candidate_paths(config: Config) -> list[Path]:
    paths: list[Path] = []
    env_path = os.environ.get("TRAE_DEVICE_JSON", "").strip()
    if env_path:
        paths.append(Path(env_path).expanduser())
    paths.append(config.data_dir / "device-main.json")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        paths.append(Path(local) / "trae-reverse" / "device-main.json")
        paths.append(Path(local) / "TraeMouth" / "device-main.json")
    roaming = os.environ.get("APPDATA")
    if roaming:
        paths.append(Path(roaming) / "Trae CN" / "device-main.json")
    return paths


def _pick_spec(data: dict[str, Any], cpu_manufacturer: str = "") -> DeviceSpec:
    model = str(data.get("device_model") or "").strip().lower()
    if model:
        for spec in DEVICE_SPECS:
            if spec.model.lower() == model:
                return spec
    hint = " ".join(
        str(data.get(key) or "")
        for key in ("device_name", "device_manufacturer", "device_brand")
    ).lower()
    matches = [
        spec
        for spec in DEVICE_SPECS
        if not cpu_manufacturer
        or spec.cpu_manufacturer.lower() == cpu_manufacturer.lower()
    ]
    for spec in matches:
        if spec.manufacturer.lower().split()[0] in hint:
            return spec
    return matches[0] if matches else DEVICE_SPECS[0]


def _read_tiny_device() -> dict[str, Any]:
    roaming = os.environ.get("APPDATA")
    if not roaming:
        return {}
    path = Path(roaming) / "Trae CN" / "aha" / "TinyStorage"
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    value = (data.get("tiny_storage_data") or {}).get("aha.device.device_id")
    if not isinstance(value, str) or not value:
        return {}
    try:
        from .importer import decrypt_storage_value

        payload = json.loads(decrypt_storage_value(value))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


_RUNTIME_CACHE: dict[str, Any] = {"at": 0.0, "value": {}}


def _read_runtime_identity() -> dict[str, str]:
    now = time.time()
    if now - float(_RUNTIME_CACHE.get("at") or 0) < 60:
        return dict(_RUNTIME_CACHE.get("value") or {})
    roaming = os.environ.get("APPDATA")
    if not roaming:
        return {}
    root = Path(roaming) / "Trae CN" / "logs"
    if not root.exists():
        return {}
    candidates = sorted(
        root.rglob("*.log"), key=lambda path: path.stat().st_mtime, reverse=True
    )
    for path in candidates[:80]:
        try:
            text = path.read_text("utf-8", errors="ignore")[:2_000_000]
        except OSError:
            continue
        if "ICUBE_MACHINE_ID" not in text:
            continue
        machine = re.search(r'"ICUBE_MACHINE_ID":"([0-9a-fA-F]{64})"', text)
        computer = re.search(r'"COMPUTERNAME":"([^"]+)"', text)
        app_version = re.search(r'"ICUBE_BUILD_VERSION":"([^"]+)"', text)
        ide_version = re.search(r'"ICUBE_APP_VERSION":"([^"]+)"', text)
        processor = re.search(r'"PROCESSOR_IDENTIFIER":"([^"]+)"', text)
        cpu_count = re.search(r'"NUMBER_OF_PROCESSORS":"(\d+)"', text)
        cpu_manufacturer = ""
        if processor:
            value = processor.group(1).lower()
            if "genuineintel" in value:
                cpu_manufacturer = "Intel"
            elif "authenticamd" in value:
                cpu_manufacturer = "AMD"
        if machine:
            value = {
                "machine_id": machine.group(1),
                "device_name": computer.group(1) if computer else "",
                "app_version": app_version.group(1) if app_version else "",
                "ide_version": ide_version.group(1) if ide_version else "",
                "cpu_manufacturer": cpu_manufacturer,
                "cpu_count": cpu_count.group(1) if cpu_count else "",
            }
            _RUNTIME_CACHE["at"] = now
            _RUNTIME_CACHE["value"] = value
            return dict(value)
    return {}


def load_device_override(config: Config) -> Optional[DeviceProfile]:
    data: dict[str, Any] = {}
    for path in _candidate_paths(config):
        if not path.exists():
            continue
        try:
            loaded = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(loaded, dict):
            data = loaded
            break
    tiny = _read_tiny_device()
    runtime = _read_runtime_identity()
    if not data and not tiny and not runtime:
        return None
    spec = _pick_spec(data, runtime.get("cpu_manufacturer", ""))
    device = build_device(
        app_version=config.native_app_version,
        ide_version=config.ide_version,
        ide_version_code=config.ide_version_code,
        brand=spec.model,
    )
    if runtime.get("cpu_count"):
        try:
            device.cpu_count = int(runtime["cpu_count"])
        except ValueError:
            pass
    device.machine_id = str(
        runtime.get("machine_id") or data.get("machine_id") or device.machine_id
    )
    device.device_id = str(
        tiny.get("device_id_str") or data.get("device_id") or device.device_id
    )
    device.device_name = str(
        data.get("device_name")
        or runtime.get("device_name")
        or os.environ.get("COMPUTERNAME")
        or device.device_name
    )
    if runtime.get("app_version"):
        device.app_version = runtime["app_version"]
    if data.get("device_model"):
        device.device_model = str(data["device_model"])
        device.brand = device.device_model
    else:
        device.device_model = spec.model
        device.brand = spec.model
    if data.get("device_manufacturer"):
        device.device_manufacturer = str(data["device_manufacturer"])
    else:
        device.device_manufacturer = spec.manufacturer
    if data.get("device_brand") and str(data["device_brand"]).lower() not in {
        "windows",
        "pc",
        "desktop",
    }:
        device.device_manufacturer = str(data["device_brand"])
    if data.get("device_cpu"):
        device.cpu_brand = str(data["device_cpu"])
        device.cpu_model = device.cpu_brand
    device.os_name = "windows"
    device.device_type = device.os_name
    if data.get("os_info"):
        device.os_version = str(data["os_info"])
    if data.get("platform_code"):
        device.platform_code = str(data["platform_code"])
    if data.get("device_type"):
        device.device_class = str(data["device_type"])
    if data.get("public_key"):
        device.public_key = str(data["public_key"])
    else:
        device.public_key = ""
    if data.get("private_key"):
        device.private_key_pem = str(data["private_key"])
    else:
        device.private_key_pem = ""
    return device
