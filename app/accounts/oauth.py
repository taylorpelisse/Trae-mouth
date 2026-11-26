from __future__ import annotations

import json
from typing import Any, Mapping, Optional

import httpx

from ..security import (
    jwt_user_id,
    normalize_timestamp,
)
from .importer import ImportError_
from .device_info import build_device_info, build_device_proof
from .models import (
    AccountProfile,
    DeviceProfile,
    ImportedAccount,
    TokenBundle,
    normalize_device,
    new_web_id,
)


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _pick(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _normalize_exchange(
    body: Any,
    *,
    client_id: str,
    refresh_token: str,
    user_id: str,
    endpoint: str,
) -> tuple[Optional[dict[str, Any]], str]:
    if not isinstance(body, dict):
        return None, "ExchangeToken response is not an object"
    result = body.get("Result") or body.get("result") or body
    if not isinstance(result, dict):
        return None, "ExchangeToken result is not an object"
    token = str(_pick(result, "Token", "token", "AccessToken", "accessToken") or "")
    if not token:
        metadata = body.get("ResponseMetadata") or {}
        error = metadata.get("Error") if isinstance(metadata, dict) else None
        return None, "did not return a token" + (f": {error}" if error else "")
    user_info = _json_obj(result.get("UserInfo") or result.get("userInfo"))
    return (
        {
            "token": token,
            "refresh_token": str(
                _pick(result, "RefreshToken", "refreshToken") or refresh_token
            ),
            "token_expires_at": normalize_timestamp(
                _pick(result, "TokenExpireAt", "tokenExpireAt")
            ),
            "refresh_expires_at": normalize_timestamp(
                _pick(result, "RefreshExpireAt", "refreshExpireAt")
            ),
            "client_id": str(_pick(result, "ClientID", "clientId") or client_id),
            "user_id": str(
                _pick(user_info, "UserID", "userId")
                or _pick(result, "UserID", "userId")
                or user_id
            ),
            "user_info": user_info,
            "endpoint": endpoint,
        },
        "",
    )


DEVICE_MISMATCH_CODE = "20403"
DEVICE_MISMATCH_MARKER = "device not match"


def device_mismatch(response_status: int, body: Any) -> bool:
    """上游判定「令牌与设备不匹配」：这个账号的令牌不是用当前设备密钥签发的。"""
    if response_status != 401 or not isinstance(body, dict):
        return False
    metadata = body.get("ResponseMetadata") or {}
    error = metadata.get("Error") if isinstance(metadata, dict) else None
    if not isinstance(error, dict):
        return False
    code = str(error.get("Code") or "")
    message = str(error.get("Message") or "")
    return code == DEVICE_MISMATCH_CODE or DEVICE_MISMATCH_MARKER in message.lower()


def exchange_refresh_token(
    refresh_token: str,
    *,
    client_id: str = "",
    user_id: str = "",
    host: str = "https://api.trae.cn",
    transport: Optional[httpx.BaseTransport] = None,
    timeout: float = 30.0,
    device: Optional[DeviceProfile] = None,
    private_key_pem: str = "",
    ide_version: str = "",
    use_device_info: bool = True,
    skip_native: bool = False,
) -> dict[str, Any]:
    """Exchange a Trae refresh token for a Cloud-IDE-JWT.

    Uses the native device-bound exchange first when a device key is available,
    then falls back to the legacy web exchange.  ``skip_native`` goes straight
    to the legacy exchange for accounts known not to be bound to this device.
    """
    if not refresh_token:
        raise ImportError_("refresh token is empty")
    base = (host or "https://api.trae.cn").rstrip("/")
    errors: list[str] = []
    native_error = ""
    with httpx.Client(timeout=timeout, transport=transport) as client:
        if (
            use_device_info
            and not skip_native
            and device
            and private_key_pem
            and device.public_key
        ):
            native_path = "/trae/api/v3/oauth/ExchangeToken"
            try:
                payload = {
                    "ClientID": client_id,
                    "ClientSecret": "",
                    "RefreshToken": refresh_token,
                    "DeviceInfo": build_device_info(device),
                    "DeviceProof": build_device_proof(
                        "POST",
                        native_path,
                        client_id,
                        refresh_token,
                        private_key_pem,
                    ),
                    "IDEVersion": ide_version or device.app_version,
                }
            except Exception as exc:
                errors.append(f"native proof failed: {exc}")
                native_error = f"native proof failed: {exc}"
            else:
                app_version = ide_version or device.app_version
                response = client.post(
                    base + native_path,
                    json=payload,
                    headers={"User-Agent": f"Trae/{app_version}"} if app_version else None,
                )
                body: Any = None
                try:
                    body = response.json()
                except ValueError:
                    body = None
                if response.status_code < 400 and body is not None:
                    parsed, error = _normalize_exchange(
                        body,
                        client_id=client_id,
                        refresh_token=refresh_token,
                        user_id=user_id,
                        endpoint="native",
                    )
                    if parsed:
                        return parsed
                    errors.append(f"native: {error}")
                    native_error = f"native: {error}"
                else:
                    detail = response.text[:300] if body is None else ""
                    native_error = (
                        f"native HTTP {response.status_code}: {detail}".rstrip()
                    )
                    if device_mismatch(response.status_code, body):
                        native_error = "native: token device not match"
                    errors.append(native_error)

        legacy_url = f"{base}/cloudide/api/v3/trae/oauth/ExchangeToken"
        payload = {
            "ClientID": client_id,
            "RefreshToken": refresh_token,
            "ClientSecret": "-",
            "UserID": user_id,
        }
        response = client.post(legacy_url, json=payload)
        body = None
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code < 400 and body is not None:
            parsed, error = _normalize_exchange(
                body,
                client_id=client_id,
                refresh_token=refresh_token,
                user_id=user_id,
                endpoint="legacy",
            )
            if parsed:
                # 记下原生接口为什么没用上，方便判断这个账号是否绑定到了本设备
                parsed["native_error"] = native_error
                return parsed
            errors.append(f"legacy: {error}")
        else:
            detail = response.text[:300] if body is None else ""
            errors.append(f"legacy HTTP {response.status_code}: {detail}".rstrip())
    raise ImportError_("; ".join(errors) or "ExchangeToken failed")


def _callback_token(callback: Mapping[str, Any]) -> tuple[str, str, Optional[int], Optional[int], str]:
    user_jwt = _json_obj(callback.get("userJwt") or callback.get("userJwt".lower()))
    token = str(_pick(user_jwt, "Token", "token") or "")
    refresh_token = str(
        _pick(user_jwt, "RefreshToken", "refreshToken")
        or callback.get("refreshToken")
        or callback.get("data")
        or ""
    )
    token_expiry = normalize_timestamp(
        _pick(user_jwt, "TokenExpireAt", "tokenExpireAt")
        or callback.get("expiredAt")
    )
    refresh_expiry = normalize_timestamp(
        _pick(user_jwt, "RefreshExpireAt", "refreshExpireAt")
        or callback.get("refreshExpireAt")
    )
    client_id = str(
        _pick(user_jwt, "ClientID", "clientId")
        or callback.get("clientID")
        or callback.get("clientId")
        or callback.get("client_id")
        or ""
    )
    return token, refresh_token, token_expiry, refresh_expiry, client_id


def import_from_callback(
    callback: Mapping[str, Any],
    *,
    device_data: Mapping[str, Any] | None = None,
    default_client_id: str = "",
    default_host: str = "https://api.trae.cn",
    app_version: str = "0.1.7",
    ide_version: str = "3.4.22",
    ide_version_code: str = "20261118",
    transport: Optional[httpx.BaseTransport] = None,
) -> ImportedAccount:
    """Turn raw Trae OAuth callback parameters into an account record."""
    token, refresh_token, token_expiry, refresh_expiry, client_id = _callback_token(
        callback
    )
    user_info = _json_obj(callback.get("userInfo"))
    user_id = str(
        _pick(user_info, "UserID", "userId", "user_id")
        or callback.get("userId")
        or jwt_user_id(token)
        or ""
    )
    if not token and refresh_token:
        exchanged = exchange_refresh_token(
            refresh_token,
            client_id=client_id or default_client_id,
            user_id=user_id,
            host=str(callback.get("host") or default_host),
            transport=transport,
        )
        token = exchanged["token"]
        refresh_token = exchanged["refresh_token"]
        token_expiry = exchanged["token_expires_at"] or token_expiry
        refresh_expiry = exchanged["refresh_expires_at"] or refresh_expiry
        client_id = exchanged["client_id"] or client_id
        user_id = exchanged["user_id"] or user_id
        user_info = exchanged["user_info"] or user_info
    if not token:
        raise ImportError_("OAuth callback did not contain a token or refresh token")
    if not user_id:
        user_id = jwt_user_id(token)
    if not user_id:
        raise ImportError_("cannot determine account user id")

    username = str(
        _pick(user_info, "ScreenName", "screenName", "username", "UserName", "name")
        or ""
    )
    avatar_url = str(
        _pick(user_info, "AvatarUrl", "avatarUrl", "avatar_url", "Avatar") or ""
    )
    web_id = str(
        _pick(user_info, "WebId", "webId") or new_web_id()
    )
    region = _pick(user_info, "Region", "region")
    host = str(callback.get("host") or default_host).rstrip("/")
    if "mchost.guru" in host or not host:
        host = default_host

    device: DeviceProfile = normalize_device(
        device_data,
        app_version=app_version,
        ide_version=ide_version,
        ide_version_code=ide_version_code,
    )
    if not device.web_id:
        device.web_id = web_id

    return ImportedAccount(
        profile=AccountProfile(
            user_id=user_id,
            username=username,
            avatar_url=avatar_url,
            edition="cn",
            host=host,
            client_id=client_id or default_client_id,
            plan="",
            source="web_oauth",
            metadata={
                "web_id": web_id,
                "region": region or "",
                "scope": callback.get("scope") or "",
                "login_trace_id": callback.get("login_trace_id") or "",
            },
        ),
        tokens=TokenBundle(
            token=token,
            refresh_token=refresh_token,
            token_expires_at=token_expiry,
            refresh_expires_at=refresh_expiry,
        ),
        device=device,
    )
