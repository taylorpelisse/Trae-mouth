from __future__ import annotations

import base64
import copy
import json

import pytest

from app.accounts import importer
from tests.conftest import auth_payload, make_tc_container


def test_decrypt_and_import_storage() -> None:
    payload, storage = auth_payload()
    account = importer.import_from_storage(storage)
    assert account.profile.user_id == "user-1"
    assert account.profile.username == "tester"
    assert account.profile.plan == "Pro"
    assert account.profile.host == "https://api.trae.cn"
    assert account.profile.client_id == "ono9krqynydwx5"
    assert account.tokens.token == payload["token"]
    assert account.tokens.refresh_token == "refresh-1"
    assert account.tokens.token_expires_at == 4070908800
    assert account.device.machine_id
    assert account.device.web_id == "999000111"


def test_plaintext_storage_is_supported() -> None:
    payload, _ = auth_payload()
    storage = {
        importer.STORAGE_KEY: json.dumps(payload),
        importer.SERVER_DATA_KEY: json.dumps(
            {"entitlementInfo": {"identityStr": "Free"}}
        ),
    }
    account = importer.import_from_storage(storage)
    assert account.profile.user_id == "user-1"
    assert account.profile.plan == "Free"


def test_encrypted_value_import() -> None:
    payload, _ = auth_payload()
    account = importer.import_from_encrypted_value(make_tc_container(payload))
    assert account.tokens.token == payload["token"]


def test_bad_container_raises() -> None:
    with pytest.raises(importer.ImportError_):
        importer.decrypt_storage_value(base64.b64encode(b"nonsense").decode())


def test_hash_mismatch_raises() -> None:
    payload, storage = auth_payload()
    raw = base64.b64decode(storage[importer.STORAGE_KEY])
    mutated = bytearray(raw)
    mutated[-1] ^= 0xFF
    with pytest.raises(importer.ImportError_):
        importer.decrypt_storage_value(base64.b64encode(mutated).decode())


def test_missing_token_raises() -> None:
    payload, storage = auth_payload()
    broken = copy.deepcopy(payload)
    broken.pop("token")
    storage = dict(storage)
    storage[importer.STORAGE_KEY] = make_tc_container(broken)
    with pytest.raises(importer.ImportError_):
        importer.import_from_storage(storage)


def test_read_storage_file_from_user_dir(tmp_path) -> None:
    payload, storage = auth_payload()
    user_dir = tmp_path / "User"
    (user_dir / "globalStorage").mkdir(parents=True)
    (user_dir / "globalStorage" / "storage.json").write_text(
        json.dumps(storage), encoding="utf-8"
    )
    account = importer.import_from_path(user_dir)
    assert account.profile.user_id == "user-1"
