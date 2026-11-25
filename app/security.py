from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class SecretBox:
    """AES-256-GCM envelope used for credentials at rest."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("master key must be 32 bytes")
        self._aes = AESGCM(key)

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        nonce = os.urandom(12)
        ciphertext = self._aes.encrypt(nonce, plaintext.encode("utf-8"), b"trae-mouth")
        return "v1." + b64e(nonce + ciphertext)

    def decrypt(self, blob: str) -> str:
        if not blob:
            return ""
        if not blob.startswith("v1."):
            raise ValueError("unsupported secret envelope")
        raw = b64d(blob[3:])
        if len(raw) < 13:
            raise ValueError("truncated secret envelope")
        plaintext = self._aes.decrypt(raw[:12], raw[12:], b"trae-mouth")
        return plaintext.decode("utf-8")


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P, b64e(salt), b64e(digest)
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$", 5)
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=b64d(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(b64d(hash_b64)),
        )
        return hmac.compare_digest(digest, b64d(hash_b64))
    except Exception:
        return False


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def decode_jwt_payload(token: str) -> dict[str, Any]:
    if not token or token.count(".") < 2:
        return {}
    try:
        part = token.split(".")[1]
        payload = json.loads(b64d(part).decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def jwt_user_id(token: str) -> str:
    payload = decode_jwt_payload(token)
    for value in (
        payload.get("data"),
        payload,
    ):
        if isinstance(value, dict):
            for key in ("id", "user_id", "userId", "sub"):
                if value.get(key) not in (None, ""):
                    return str(value[key])
    return ""


def jwt_expiry(token: str) -> Optional[int]:
    payload = decode_jwt_payload(token)
    for key in ("exp", "expire", "expiredAt"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value / 1000) if value > 10**11 else int(value)
    return None


def now_ts() -> int:
    return int(time.time())


def normalize_timestamp(value: Any) -> Optional[int]:
    """Accept ISO-8601, epoch seconds, or epoch milliseconds."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        if number > 10**11:
            number /= 1000.0
        return int(number)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        number = float(raw)
        if number > 10**11:
            number /= 1000.0
        return int(number)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None
