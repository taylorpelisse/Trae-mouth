from __future__ import annotations

import base64
import json
import time

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.accounts import importer
from app.accounts.device_import import load_device_override
from app.accounts.device_info import build_device_info, build_device_proof
from app.accounts.models import build_device, normalize_device
from app.accounts.refresher import TokenRefresher
from app.accounts.store import AccountStore
from app.db import Database
from app.security import SecretBox
from tests.conftest import auth_payload


def test_build_device_has_full_profile() -> None:
    device = build_device()
    assert device.device_name.startswith("DESKTOP-")
    assert device.device_model
    assert device.device_manufacturer
    assert device.cpu_brand
    assert device.cpu_count > 0
    assert device.memory >= 8 * 1024**3
    assert device.os_name == "windows"
    assert device.os_version.startswith("Windows")
    assert device.os_release.startswith("10.")
    assert device.public_key.startswith("-----BEGIN PUBLIC KEY-----")
    assert device.private_key_pem.startswith("-----BEGIN PRIVATE KEY-----")
    assert "private_key_pem" not in device.to_dict()
    assert device.to_dict()["public_key"]


def test_normalize_upgrades_legacy_device() -> None:
    legacy = {
        "machine_id": "a" * 32,
        "device_id": "1234567890123456789",
        "brand": "82WM",
        "cpu": "AMD",
        "device_type": "windows",
        "os_version": "Windows 11 Pro",
        "app_version": "0.1.7",
        "ide_version": "3.4.4",
        "ide_version_code": "20260212",
        "web_id": "123",
    }
    device = normalize_device(legacy)
    assert device.machine_id == "a" * 32
    assert device.device_id == "1234567890123456789"
    assert device.device_model == "82WM"
    assert device.device_name
    assert device.cpu_brand
    assert device.memory > 0
    assert device.public_key
    assert device.private_key_pem


def test_device_info_shape() -> None:
    device = build_device()
    info = build_device_info(device)
    assert info["DeviceID"] == device.device_id
    assert info["MachineID"] == device.machine_id
    assert info["PlatformCode"] == "IDE_PC"
    assert info["DeviceType"] == "PC"
    assert info["DeviceName"] == device.device_name
    assert info["DeviceModel"] == device.device_model
    assert info["DeviceBrand"] == device.device_manufacturer
    assert info["DeviceCPU"] == device.cpu_brand
    assert info["OSInfo"] == "windows"
    assert info["OSVersion"] == device.os_version
    assert info["DevicePublicKey"] == device.public_key


def test_device_proof_verifies() -> None:
    device = build_device()
    path = "/trae/api/v3/oauth/ExchangeToken"
    proof = build_device_proof(
        "POST", path, "client-1", "refresh-1", device.private_key_pem
    )
    message = "\n".join(
        [
            "POST",
            path,
            "client-1",
            "refresh-1",
            str(proof["Timestamp"]),
            proof["Nonce"],
        ]
    )
    public_key = serialization.load_pem_public_key(device.public_key.encode("ascii"))
    public_key.verify(
        base64.b64decode(proof["Signature"]),
        message.encode("utf-8"),
        ec.ECDSA(hashes.SHA256()),
    )


def test_store_roundtrip_device_and_key(tmp_path) -> None:
    db = Database(tmp_path / "device.db")
    db.migrate()
    store = AccountStore(db, SecretBox(b"d" * 32))
    _, storage = auth_payload()
    record, _ = store.add_imported(importer.import_from_storage(storage))
    device = store.get_device(record.id)
    assert device is not None
    assert device.device_name
    assert device.device_model
    assert device.public_key
    private_key = store.get_device_private_key(record.id)
    assert private_key.startswith("-----BEGIN PRIVATE KEY-----")
    derived_public = (
        serialization.load_pem_private_key(
            private_key.encode("ascii"), password=None
        )
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    assert derived_public == device.public_key


def test_refresh_uses_native_device_info(tmp_path) -> None:
    db = Database(tmp_path / "native.db")
    db.migrate()
    store = AccountStore(db, SecretBox(b"n" * 32))
    _, storage = auth_payload()
    record, _ = store.add_imported(importer.import_from_storage(storage))
    from app.accounts.models import TokenBundle

    store.update_tokens(
        record.id,
        TokenBundle(
            token="old",
            refresh_token="refresh-1",
            token_expires_at=int(time.time()) + 60,
            refresh_expires_at=int(time.time()) + 86400,
        ),
    )
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path, "body": json.loads(request.content)})
        return httpx.Response(
            200,
            json={
                "Result": {
                    "Token": "native-token",
                    "RefreshToken": "native-refresh",
                    "TokenExpireAt": "2099-01-01T00:00:00.000Z",
                    "RefreshExpireAt": "2099-06-01T00:00:00.000Z",
                    "ClientID": "ono9krqynydwx5",
                    "UserInfo": json.dumps({"UserID": "user-1"}),
                }
            },
        )

    refresher = TokenRefresher(
        store,
        transport=httpx.MockTransport(handler),
        use_device_info=True,
    )
    result = refresher.refresh(record.id)
    assert result.ok
    assert seen and seen[0]["path"] == "/trae/api/v3/oauth/ExchangeToken"
    body = seen[0]["body"]
    assert body["DeviceInfo"]["DeviceModel"]
    assert body["DeviceInfo"]["DeviceName"]
    assert body["DeviceProof"]["Signature"]
    assert body["DeviceProof"]["Nonce"]
    assert body["ClientSecret"] == ""
    assert store.get_secrets(record.id).token == "native-token"


def test_load_device_override(config, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.accounts.device_import._read_runtime_identity", lambda: {}
    )
    monkeypatch.setattr("app.accounts.device_import._read_tiny_device", lambda: {})
    base = build_device()
    data = {
        "machine_id": "e0f0f3da38ecd8d1778ccd6f15850bac",
        "device_id": "2886260666583616355",
        "device_name": "LENOVO-2022C",
        "device_brand": "Windows",
        "device_model": "",
        "os_info": "Windows 11 Pro",
        "os_version": "Windows 11",
        "platform_code": "IDE_PC",
        "device_type": "PC",
        "app_version": "2.3.102387",
        "public_key": base.public_key,
        "private_key": base.private_key_pem,
    }
    path = config.data_dir / "device-main.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    device = load_device_override(config)
    assert device is not None
    assert device.machine_id == data["machine_id"]
    assert device.device_id == data["device_id"]
    assert device.device_name == "LENOVO-2022C"
    assert device.device_manufacturer == "LENOVO"
    assert device.os_version == "Windows 11 Pro"
    assert device.public_key == base.public_key
    assert device.private_key_pem == base.private_key_pem
