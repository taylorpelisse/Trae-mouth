from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..security import jwt_user_id, normalize_timestamp
from .models import (
    AccountProfile,
    DeviceProfile,
    ImportedAccount,
    TokenBundle,
    build_device,
)

STORAGE_KEY = "iCubeAuthInfo://icube.cloudide"
SERVER_DATA_KEY = "iCubeServerData://icube.cloudide"
DEFAULT_CLIENT_ID = "ono9krqynydwx5"
DEFAULT_AUTH_HOST = "https://api.trae.cn"

# Container salts used by the Trae desktop client's "tc" storage format.
_SALT_1 = bytes([
    82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
    124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
    84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78,
    8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37,
])
_SALT_2 = bytes([
    31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
    96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
    160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
    23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125,
])
_SALT_3 = bytes([
    191, 192, 216, 250, 122, 246, 220, 97, 31, 254, 98, 27, 8, 72, 71, 176,
    135, 99, 96, 18, 127, 101, 203, 104, 211, 102, 191, 125, 37, 72, 150, 156,
    51, 229, 121, 35, 17, 153, 141, 177, 110, 131, 150, 128, 172, 255, 254, 6,
    18, 140, 55, 62, 236, 249, 135, 64, 135, 12, 117, 4, 89, 149, 168, 209,
])
_SALT_4 = bytes([
    246, 204, 26, 232, 232, 70, 129, 109, 223, 146, 169, 242, 23, 241, 105, 145,
    50, 196, 165, 42, 254, 120, 3, 54, 244, 207, 209, 85, 53, 6, 138, 106,
    175, 148, 31, 204, 186, 186, 165, 182, 87, 142, 49, 10, 39, 110, 26, 154,
    86, 56, 173, 125, 18, 64, 198, 225, 99, 99, 83, 82, 191, 134, 76, 170,
])


class ImportError_(RuntimeError):
    pass


def _xor(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right))


def _unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data.endswith(bytes([pad]) * pad):
        return data[:-pad]
    raise ImportError_("invalid PKCS#7 padding")


def decrypt_storage_value(value: str) -> str:
    """Decrypt one ``tc`` container from Trae's storage.json."""
    try:
        buffer = base64.b64decode(value, validate=False)
    except Exception as exc:
        raise ImportError_("credential blob is not valid base64") from exc
    if len(buffer) < 38 + 16:
        raise ImportError_("credential blob is truncated")

    header = buffer[:6]
    random_bytes = buffer[6:38]
    ciphertext = buffer[38:]

    if header[:2] == b"tc" and header[2:6] == b"\x05\x10\x00\x00":
        salt = _xor(_SALT_1, _SALT_2)
    elif header == bytes([18, 57, 32, 32, 2, 3]):
        salt = _xor(_SALT_3, _SALT_4)
    else:
        raise ImportError_(f"unsupported credential container: {header.hex()}")

    first = hashlib.sha512(random_bytes).digest()
    final = hashlib.sha512(first + salt).digest()
    key, iv = final[:16], final[16:32]

    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(ciphertext) + decryptor.finalize()
    stored_hash, body = plain[:64], _unpad(plain[64:])
    if hashlib.sha512(body).digest() != stored_hash:
        raise ImportError_("credential hash check failed")

    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ImportError_("credential payload is not UTF-8") from exc


def _decode_auth_value(value: str) -> dict[str, Any]:
    if not value:
        raise ImportError_("credential value is empty")
    if value.lstrip().startswith("{"):
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ImportError_("plaintext credential is not valid JSON") from exc
    else:
        data = json.loads(decrypt_storage_value(value))
    if not isinstance(data, dict):
        raise ImportError_("credential payload is not an object")
    return data


def candidate_user_dirs() -> list[tuple[str, Path]]:
    home = Path.home()
    if sys.platform == "win32":
        roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        bases = {
            "cn": roaming / "Trae CN" / "User",
            "solo": roaming / "TRAE SOLO CN" / "User",
            "sg": roaming / "Trae" / "User",
            "solo-sg": roaming / "TRAE SOLO" / "User",
        }
    elif sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        bases = {
            "cn": support / "Trae CN" / "User",
            "solo": support / "TRAE SOLO CN" / "User",
            "sg": support / "Trae" / "User",
            "solo-sg": support / "TRAE SOLO" / "User",
        }
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        bases = {
            "cn": config / "Trae CN" / "User",
            "solo": config / "TRAE SOLO CN" / "User",
            "sg": config / "Trae" / "User",
            "solo-sg": config / "TRAE SOLO" / "User",
        }
    return [(edition, path) for edition, path in bases.items() if path.exists()]


def read_storage_file(path: str | Path) -> dict[str, Any]:
    storage_path = Path(path)
    if storage_path.is_dir():
        storage_path = storage_path / "globalStorage" / "storage.json"
    if not storage_path.exists():
        raise ImportError_(f"storage.json not found: {storage_path}")
    try:
        data = json.loads(storage_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImportError_(f"cannot read storage.json: {exc}") from exc
    if not isinstance(data, dict):
        raise ImportError_("storage.json root is not an object")
    return data


def extract_plan(storage: dict[str, Any]) -> str:
    raw = storage.get(SERVER_DATA_KEY)
    if not raw:
        return ""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    entitlement = data.get("entitlementInfo")
    if isinstance(entitlement, dict):
        for key in ("identityStr", "identity_name", "identityName"):
            value = entitlement.get(key)
            if value:
                return str(value)
    return ""


def parse_auth(
    auth: dict[str, Any],
    *,
    storage: dict[str, Any] | None = None,
    edition: str = "cn",
    default_client_id: str = DEFAULT_CLIENT_ID,
    default_host: str = DEFAULT_AUTH_HOST,
    device: DeviceProfile | None = None,
    app_version: str = "0.1.7",
    ide_version: str = "3.4.22",
    ide_version_code: str = "20261118",
    source: str = "local_import",
) -> ImportedAccount:
    token = str(auth.get("token") or auth.get("Token") or "").strip()
    if not token:
        raise ImportError_("credential payload has no access token")

    refresh_token = str(
        auth.get("refreshToken") or auth.get("RefreshToken") or ""
    ).strip()
    user_id = str(auth.get("userId") or auth.get("UserID") or "").strip()
    if not user_id:
        user_id = jwt_user_id(token)
    if not user_id:
        raise ImportError_("credential payload has no user id")

    account = auth.get("account")
    account = account if isinstance(account, dict) else {}
    username = str(
        account.get("username")
        or account.get("screenName")
        or account.get("name")
        or ""
    ).strip()
    avatar_url = str(account.get("avatar_url") or account.get("avatarUrl") or "").strip()

    host = str(auth.get("host") or default_host).strip().rstrip("/")
    if "mchost.guru" in host or not host:
        host = default_host

    client_id = str(
        auth.get("clientID")
        or auth.get("ClientID")
        or auth.get("clientId")
        or default_client_id
    ).strip()

    fusion = auth.get("uidFusionInfo")
    fusion = fusion if isinstance(fusion, dict) else {}
    web_id = str(fusion.get("newUid") or user_id).strip()

    token_expires_at = normalize_timestamp(
        auth.get("expiredAt") or auth.get("TokenExpireAt")
    )
    refresh_expires_at = normalize_timestamp(
        auth.get("refreshExpiredAt") or auth.get("RefreshExpireAt")
    )

    if device is None:
        device = build_device(
            app_version=app_version,
            ide_version=ide_version,
            ide_version_code=ide_version_code,
            web_id=web_id,
        )
    elif not device.web_id:
        device.web_id = web_id

    plan = extract_plan(storage or {})

    return ImportedAccount(
        profile=AccountProfile(
            user_id=user_id,
            username=username,
            avatar_url=avatar_url,
            edition=edition,
            host=host,
            client_id=client_id,
            plan=plan,
            source=source,
            metadata={
                "scope": account.get("scope", ""),
                "region": auth.get("userRegion") or {},
                "old_uid": fusion.get("oldUid", ""),
                "new_uid": fusion.get("newUid", ""),
            },
        ),
        tokens=TokenBundle(
            token=token,
            refresh_token=refresh_token,
            token_expires_at=token_expires_at,
            refresh_expires_at=refresh_expires_at,
        ),
        device=device,
    )


def import_from_storage(
    storage: dict[str, Any],
    *,
    edition: str = "cn",
    default_client_id: str = DEFAULT_CLIENT_ID,
    default_host: str = DEFAULT_AUTH_HOST,
    app_version: str = "0.1.7",
    ide_version: str = "3.4.22",
    ide_version_code: str = "20261118",
    source: str = "local_import",
) -> ImportedAccount:
    raw = storage.get(STORAGE_KEY)
    if not raw:
        raise ImportError_(f"{STORAGE_KEY} not found in storage.json")
    if not isinstance(raw, str):
        raise ImportError_("credential value is not a string")
    auth = _decode_auth_value(raw)
    return parse_auth(
        auth,
        storage=storage,
        edition=edition,
        default_client_id=default_client_id,
        default_host=default_host,
        app_version=app_version,
        ide_version=ide_version,
        ide_version_code=ide_version_code,
        source=source,
    )


def import_from_path(
    path: str | Path,
    *,
    edition: str = "cn",
    **kwargs: Any,
) -> ImportedAccount:
    return import_from_storage(read_storage_file(path), edition=edition, **kwargs)


def import_from_encrypted_value(
    value: str,
    *,
    edition: str = "cn",
    **kwargs: Any,
) -> ImportedAccount:
    auth = _decode_auth_value(value)
    return parse_auth(auth, edition=edition, **kwargs)


def discover_local_accounts(
    **kwargs: Any,
) -> list[tuple[str, str, ImportedAccount]]:
    """Return (edition, storage_path, account) for every readable local install."""
    found: list[tuple[str, str, ImportedAccount]] = []
    for edition, user_dir in candidate_user_dirs():
        storage_path = user_dir / "globalStorage" / "storage.json"
        if not storage_path.exists():
            continue
        try:
            storage = read_storage_file(storage_path)
            if STORAGE_KEY not in storage:
                continue
            account = import_from_storage(storage, edition=edition, **kwargs)
            found.append((edition, str(storage_path), account))
        except Exception:
            continue
    return found
