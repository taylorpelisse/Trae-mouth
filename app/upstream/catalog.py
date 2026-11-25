from __future__ import annotations

import uuid
from typing import Any, Optional

import httpx

from ..accounts.models import DeviceProfile
from ..config import Config


def ide_headers(
    config: Config,
    token: str,
    user_id: str,
    device: DeviceProfile,
    function: str = "chat_v3",
    request_id: str = "",
) -> dict[str, str]:
    trace = uuid.uuid4().hex
    return {
        "Authorization": f"Cloud-IDE-JWT {token}",
        "X-Cloudide-Token": token,
        "X-Ide-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-App-Id": "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8",
        "X-App-Version": "default",
        "X-App-Version-Code": device.ide_version_code or config.ide_version_code,
        "X-Ide-Version": device.ide_version or config.ide_version,
        "X-Ide-Version-Code": device.ide_version_code or config.ide_version_code,
        "X-Ide-Version-Type": "stable",
        "X-Plugin-Channel": "desktop",
        "X-App-Function": function,
        "X-Ide-Function": function,
        "X-Request-Id": request_id or str(uuid.uuid4()),
        "X-Custom-Trace-Id": trace,
        "X-Flow-Traceparent": f"04-{trace}-{uuid.uuid4().hex[:16]}-01",
        "request-traffic-type": "prod",
        "X-Device-Cpu": device.cpu_manufacturer or device.cpu or "AMD",
        "X-Device-Id": device.device_id,
        "X-Machine-Id": device.machine_id,
        "X-Device-Brand": device.device_model or device.brand,
        "X-Device-Type": device.os_name or device.device_type or "windows",
        "X-Os-Version": device.os_version or "Windows 11 Pro",
        "X-Uid": user_id,
        "User-Agent": "",
    }


def web_headers(config: Config, token: str, user_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Cloud-IDE-JWT {token}",
        "Content-Type": "application/json",
        "X-Trae-Client-Type": "web",
        "X-Preferenced-Language": "zh-CN",
        "x-user-region": "CN",
        "Origin": config.trae_web_origin,
        "Referer": config.trae_web_origin + "/",
        "x-uid": user_id,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
        ),
    }


def _walk_models(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            models = value.get("models")
            if isinstance(models, list):
                function = value.get("function") or value.get("raw_chat_function") or ""
                for model in models:
                    if isinstance(model, dict):
                        item = dict(model)
                        item.setdefault("_function", function)
                        found.append(item)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return found


def fetch_web_models(
    config: Config,
    token: str,
    user_id: str,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    url = (
        f"{config.web_api_base}/models"
        "?functions=solo_agent_remote%2Csolo_work_remote%2Csolo_design_remote"
        "&show_custom_model=true"
    )
    with httpx.Client(timeout=30, transport=transport) as client:
        response = client.get(url, headers=web_headers(config, token, user_id))
    if response.status_code >= 400:
        return {
            "ok": False,
            "status": response.status_code,
            "error": response.text[:300],
            "models": [],
        }
    try:
        body = response.json()
    except ValueError:
        return {"ok": False, "status": response.status_code, "error": "invalid JSON", "models": []}
    models = _walk_models(body)
    names = sorted(
        {
            str(m.get("config_name") or m.get("name") or m.get("model_name") or "")
            for m in models
            if (m.get("config_name") or m.get("name") or m.get("model_name"))
        }
    )
    return {"ok": True, "status": response.status_code, "models": names}


def fetch_ide_configs(
    config: Config,
    token: str,
    user_id: str,
    device: DeviceProfile,
    function: str = "chat_v3",
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    url = f"{config.model_gateway}/api/ide/v1/get_detail_param"
    body = {
        "function": function,
        "config_names": None,
        "need_prompt": False,
        "current_config_info": None,
        "poly_prompt": True,
        "mode_type": None,
        "agent_type": None,
    }
    with httpx.Client(timeout=30, transport=transport) as client:
        response = client.post(
            url,
            headers=ide_headers(config, token, user_id, device, function),
            json=body,
        )
    if response.status_code >= 400:
        return {
            "ok": False,
            "status": response.status_code,
            "error": response.text[:300],
            "configs": [],
        }
    try:
        data = response.json()
    except ValueError:
        return {"ok": False, "status": response.status_code, "error": "invalid JSON", "configs": []}
    rows = data.get("config_info_list") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        nested = data.get("data") if isinstance(data, dict) else None
        rows = nested.get("config_info_list") if isinstance(nested, dict) else []
    configs: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        details = row.get("model_detail_list")
        details = details if isinstance(details, list) else []
        provider_ids = [
            str(d.get("model_name"))
            for d in details
            if isinstance(d, dict) and d.get("model_name")
        ]
        configs.append(
            {
                "config_name": row.get("config_name") or row.get("name") or "",
                "usage": row.get("usage") or "",
                "model_names": provider_ids,
            }
        )
    return {"ok": True, "status": response.status_code, "configs": configs}


def test_account(
    config: Config,
    token: str,
    user_id: str,
    device: DeviceProfile,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    web = fetch_web_models(config, token, user_id, transport=transport)
    ide = fetch_ide_configs(config, token, user_id, device, transport=transport)
    return {
        "ok": bool(web.get("ok") or ide.get("ok")),
        "web": web,
        "ide": ide,
    }
