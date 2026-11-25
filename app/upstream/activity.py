from __future__ import annotations

from typing import Any, Optional

import httpx

from ..accounts.models import DeviceProfile
from ..config import Config

DESKTOP_UA = "Trae/2.3.105403"


def _headers(
    config: Config, token: str, device: Optional[DeviceProfile]
) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Cloud-IDE-JWT {token}",
        "User-Agent": DESKTOP_UA,
        "x-rust-request-timeout": "30000",
        "x-app-version": config.ide_version,
        "x-device-id": device.device_id if device else "",
        "x-device-type": (
            (device.os_name or device.device_type or "windows")
            if device
            else "windows"
        ),
    }
    if device:
        brand = device.device_model or device.brand
        if brand:
            headers["x-device-brand"] = brand
        if device.os_version:
            headers["x-os-version"] = device.os_version
    return headers


async def post(
    config: Config,
    token: str,
    device: Optional[DeviceProfile],
    path: str,
    body: dict[str, Any],
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict[str, Any]:
    url = config.trae_auth_host.rstrip("/") + path
    timeout = httpx.Timeout(connect=15.0, read=30.0, write=15.0, pool=15.0)
    try:
        async with httpx.AsyncClient(
            timeout=timeout, transport=transport
        ) as client:
            response = await client.post(
                url, headers=_headers(config, token, device), json=body
            )
        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text[:500]}
        code = data.get("code") if isinstance(data, dict) else None
        ok = response.status_code == 200 and code in (None, 0)
        message = ""
        if isinstance(data, dict):
            message = str(data.get("message") or data.get("msg") or "")
        return {
            "ok": ok,
            "status": response.status_code,
            "code": code,
            "message": message or ("" if ok else f"HTTP {response.status_code}"),
            "data": data,
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status": 0,
            "code": None,
            "message": f"{type(exc).__name__}: {exc}"[:300],
            "data": None,
        }


async def credit_usage(
    config: Config,
    token: str,
    device: Optional[DeviceProfile],
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict[str, Any]:
    return await post(
        config,
        token,
        device,
        "/trae/api/v2/pay/ide_user_ent_usage",
        {"require_usage": True, "req_source": 1},
        transport=transport,
    )


async def checkin_status(
    config: Config,
    token: str,
    device: Optional[DeviceProfile],
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict[str, Any]:
    return await post(
        config,
        token,
        device,
        "/trae/api/v2/ug/checkin_credits/status",
        {"req_source": 1},
        transport=transport,
    )


async def checkin_claim(
    config: Config,
    token: str,
    device: Optional[DeviceProfile],
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict[str, Any]:
    return await post(
        config,
        token,
        device,
        "/trae/api/v2/ug/checkin_credits/claim",
        {"req_source": 1},
        transport=transport,
    )


async def user_info(
    config: Config,
    token: str,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict[str, Any]:
    url = config.trae_auth_host.rstrip("/") + "/cloudide/api/v3/trae/GetUserInfo"
    timeout = httpx.Timeout(connect=15.0, read=30.0, write=15.0, pool=15.0)
    try:
        async with httpx.AsyncClient(
            timeout=timeout, transport=transport
        ) as client:
            response = await client.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "x-cloudide-token": token,
                },
                json={"ReqSource": "IDE"},
            )
        data = response.json() if response.status_code == 200 else None
        result = data.get("Result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            return {
                "ok": False,
                "status": response.status_code,
                "message": "empty Result",
                "data": None,
            }
        return {"ok": True, "status": response.status_code, "data": result}
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "ok": False,
            "status": 0,
            "message": f"{type(exc).__name__}: {exc}"[:300],
            "data": None,
        }
