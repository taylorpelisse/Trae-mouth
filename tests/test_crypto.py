from __future__ import annotations

import pytest

from app.security import (
    SecretBox,
    hash_password,
    jwt_expiry,
    jwt_user_id,
    normalize_timestamp,
    verify_password,
)
from tests.conftest import make_jwt


def test_secret_box_roundtrip_and_tamper() -> None:
    box = SecretBox(b"k" * 32)
    blob = box.encrypt("super-secret-token")
    assert "super-secret-token" not in blob
    assert box.decrypt(blob) == "super-secret-token"
    tampered = blob[:-2] + ("AA" if blob[-2:] != "AA" else "BB")
    with pytest.raises(Exception):
        box.decrypt(tampered)


def test_password_hash_roundtrip() -> None:
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("scrypt$")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("correct horse battery staple", "garbage")


def test_password_minimum_length() -> None:
    with pytest.raises(ValueError):
        hash_password("short")


def test_normalize_timestamp_formats() -> None:
    assert normalize_timestamp("2026-12-04T07:47:19.018Z") == 1796370439
    assert normalize_timestamp(1796370439) == 1796370439
    assert normalize_timestamp(1796370439000) == 1796370439
    assert normalize_timestamp("1796370439") == 1796370439
    assert normalize_timestamp("") is None
    assert normalize_timestamp("not a date") is None


def test_jwt_helpers() -> None:
    token = make_jwt({"data": {"id": "u-42"}, "exp": 4102444800})
    assert jwt_user_id(token) == "u-42"
    assert jwt_expiry(token) == 4102444800
    assert jwt_user_id("not-a-jwt") == ""
