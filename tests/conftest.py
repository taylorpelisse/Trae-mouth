from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_WORKSPACE_CACHE = ROOT.parent / "cache"
if _WORKSPACE_CACHE.exists():
    _PYCACHE = _WORKSPACE_CACHE / "pycache"
    _PYCACHE.mkdir(parents=True, exist_ok=True)
    sys.pycache_prefix = str(_PYCACHE)

from app.config import Config  # noqa: E402


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def make_jwt(payload: dict) -> str:
    header = b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    body = b64url(json.dumps(payload).encode())
    return f"{header}.{body}.sig"


def make_tc_container(payload: dict) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    from app.accounts import importer

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha512(body).digest()
    pad = 16 - (len(body) % 16)
    plain = digest + body + bytes([pad]) * pad
    random_bytes = os.urandom(32)
    salt = bytes(a ^ b for a, b in zip(importer._SALT_1, importer._SALT_2))
    first = hashlib.sha512(random_bytes).digest()
    final = hashlib.sha512(first + salt).digest()
    key, iv = final[:16], final[16:32]
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(plain) + encryptor.finalize()
    container = b"tc\x05\x10\x00\x00" + random_bytes + ciphertext
    return base64.b64encode(container).decode("ascii")


def auth_payload(
    *,
    token: str | None = None,
    user_id: str = "user-1",
    refresh_token: str = "refresh-1",
    username: str = "tester",
    plan: str = "Pro",
) -> tuple[dict, dict]:
    token = token or make_jwt(
        {"data": {"id": user_id}, "exp": 4102444800, "sub": user_id}
    )
    payload = {
        "token": token,
        "refreshToken": refresh_token,
        "expiredAt": "2099-01-01T00:00:00.000Z",
        "refreshExpiredAt": "2099-06-01T00:00:00.000Z",
        "userId": user_id,
        "host": "https://api.trae.cn",
        "account": {
            "username": username,
            "avatar_url": "https://example.invalid/a.png",
            "scope": "marscode",
        },
        "uidFusionInfo": {"oldUid": user_id, "newUid": "999000111"},
        "userRegion": {"region": "CN", "_aiRegion": "CN"},
    }
    storage = {
        "iCubeAuthInfo://icube.cloudide": make_tc_container(payload),
        "iCubeServerData://icube.cloudide": json.dumps(
            {"entitlementInfo": {"identityStr": plan}}
        ),
    }
    return payload, storage


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    return Config(
        root=ROOT,
        data_dir=tmp_path,
        db_path=tmp_path / "test.db",
        master_key=b"m" * 32,
        refresh_interval=99999,
        refresh_window=1800,
        session_ttl=3600,
        import_code_ttl=600,
        prewarm_catalogs=False,
    )
