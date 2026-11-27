from __future__ import annotations

import hashlib
from typing import Any, Optional

import httpx

from ..accounts.models import DeviceProfile
from ..config import Config

DESKTOP_UA = "Trae/2.3.105403"

# 签到接口按设备号限流（返回 9074「参与用户太多」），所以签到不用登录时的设备号，
# 而是每个账号单独推导一个设备号，被限后换一代。9095 表示该设备号已为别的账号签过。
CHECKIN_DEVICE_BRAND = "LENOVO 82WM"
CHECKIN_RATE_LIMIT_CODES = {9074, 9095}
# 设备号必须是 16 位十进制数字，这里取模到这个量级
CHECKIN_DEVICE_SPACE = 10_000_000_000_000_000


def checkin_device_id(identity: str, generation: int = 0) -> str:
    """账号绑定的 16 位签到设备号；换一代就是一个全新的设备号。"""

    seed = identity if generation <= 0 else f"{identity}|{generation}"
    value = int.from_bytes(hashlib.blake2s(seed.encode("utf-8"), digest_size=8).digest(), "big")
    return f"{value % CHECKIN_DEVICE_SPACE:016d}"


def checkin_headers(token: str, device_id: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Cloud-IDE-JWT {token}",
        "x-device-id": device_id,
        "x-device-brand": CHECKIN_DEVICE_BRAND,
        "x-device-type": "windows",
    }


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
    headers: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    url = config.trae_auth_host.rstrip("/") + path
    timeout = httpx.Timeout(connect=15.0, read=30.0, write=15.0, pool=15.0)
    try:
        async with httpx.AsyncClient(
            timeout=timeout, transport=transport
        ) as client:
            response = await client.post(
                url, headers=headers or _headers(config, token, device), json=body
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
            "rate_limited": code in CHECKIN_RATE_LIMIT_CODES,
            "message": message or ("" if ok else f"HTTP {response.status_code}"),
            "data": data,
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status": 0,
            "code": None,
            "rate_limited": False,
            "message": f"{type(exc).__name__}: {exc}"[:300],
            "data": None,
        }


async def session_usage(
    config: Config,
    token: str,
    session_id: str,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict[str, Any]:
    """回查一次会话的真实账单。

    实测同一会话重发大前缀时，流式 token_usage 的 cache_read 报 0，
    账单接口却报 75008 命中。缓存折扣只在账单里，用请求时的
    conversation_id 查，只读不写。
    """

    session_id = str(session_id or "").strip()
    if not session_id:
        return {"ok": False, "message": "session_id is required", "data": None}
    url = (
        config.usage_api_host.rstrip("/")
        + "/api/v1/commercial/get_session_usage"
    )
    timeout = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
    try:
        async with httpx.AsyncClient(
            timeout=timeout, transport=transport
        ) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Cloud-IDE-JWT {token}",
                    "Content-Type": "application/json",
                },
                json={"session_id": session_id},
            )
        if response.status_code != 200:
            return {
                "ok": False,
                "status": response.status_code,
                "message": f"HTTP {response.status_code}",
                "data": None,
            }
        try:
            payload = response.json()
        except ValueError:
            return {"ok": False, "status": 200, "message": "invalid json", "data": None}
        if not isinstance(payload, dict):
            return {"ok": False, "status": 200, "message": "empty payload", "data": None}
        code = payload.get("code")
        if code not in (None, 0, "0"):
            return {
                "ok": False,
                "status": 200,
                "message": str(payload.get("message") or payload.get("msg") or code),
                "data": None,
            }
        # 账单可能包在 data 里，也可能直接在顶层
        group = payload.get("user_usage_group_by_session")
        if not isinstance(group, dict):
            nested = payload.get("data")
            if isinstance(nested, dict):
                group = nested.get("user_usage_group_by_session")
                if not isinstance(group, dict):
                    group = nested
        if not isinstance(group, dict):
            return {"ok": False, "status": 200, "message": "no usage group", "data": None}
        extra = group.get("extra_info")
        extra = extra if isinstance(extra, dict) else {}
        return {
            "ok": True,
            "status": 200,
            "data": {
                "session_id": str(group.get("session_id") or ""),
                "model_name": str(group.get("model_name") or ""),
                "credits": _number(group.get("credits_float")),
                "input_tokens": _int(extra.get("input_token")),
                "output_tokens": _int(extra.get("output_token")),
                "cached_tokens": _int(extra.get("cache_read_token")),
                "cache_write_tokens": _int(extra.get("cache_write_token")),
            },
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status": 0,
            "message": f"{type(exc).__name__}: {exc}"[:200],
            "data": None,
        }


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _number(value: Any) -> float:
    try:
        return round(float(value or 0), 4)
    except (TypeError, ValueError):
        return 0.0


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
    identity: str,
    *,
    generation: int = 0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict[str, Any]:
    return await post(
        config,
        token,
        None,
        "/trae/api/v2/ug/checkin_credits/status",
        {},
        transport=transport,
        headers=checkin_headers(token, checkin_device_id(identity, generation)),
    )


async def checkin_claim(
    config: Config,
    token: str,
    identity: str,
    *,
    generation: int = 0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict[str, Any]:
    return await post(
        config,
        token,
        None,
        "/trae/api/v2/ug/checkin_credits/claim",
        {},
        transport=transport,
        headers=checkin_headers(token, checkin_device_id(identity, generation)),
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
