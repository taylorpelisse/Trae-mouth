from __future__ import annotations

import sqlite3
import time

from app.accounts import importer
from app.accounts.models import AccountProfile, ImportedAccount, TokenBundle, build_device
from app.accounts.store import (
    STATUS_ACTIVE,
    STATUS_COOLDOWN,
    STATUS_RELOGIN,
    AccountStore,
)
from app.db import Database
from app.security import SecretBox
from tests.conftest import auth_payload


def make_store(tmp_path) -> tuple[AccountStore, Database, SecretBox]:
    db = Database(tmp_path / "store.db")
    db.migrate()
    box = SecretBox(b"z" * 32)
    return AccountStore(db, box, default_concurrency=3), db, box


def test_add_and_dedupe(tmp_path) -> None:
    store, db, _ = make_store(tmp_path)
    payload, storage = auth_payload()
    account = importer.import_from_storage(storage)
    record, created = store.add_imported(account)
    assert created
    assert record.user_id == "user-1"
    assert record.label == ""
    assert record.last_refreshed_at is None
    assert record.concurrency_limit == 3

    second = importer.import_from_storage(storage)
    second.tokens.token = "new-token"
    second.profile.username = "renamed"
    second.device.machine_id = "should-not-replace"
    record2, created2 = store.add_imported(second, label="主账号")
    assert not created2
    assert record2.label == "主账号"
    assert record2.username == "renamed"
    assert record2.id == record.id
    assert len(store.list()) == 1
    secrets = store.get_secrets(record.id)
    assert secrets.token == "new-token"
    assert store.get_device(record.id).machine_id == account.device.machine_id


def test_secrets_are_encrypted_at_rest(tmp_path) -> None:
    store, db, _ = make_store(tmp_path)
    payload, storage = auth_payload(token="PLAINTEXT-TOKEN-MARKER")
    store.add_imported(importer.import_from_storage(storage))
    conn = sqlite3.connect(str(db.path))
    try:
        rows = conn.execute("SELECT token_enc, refresh_token_enc FROM account_secrets").fetchall()
    finally:
        conn.close()
    assert rows
    assert "PLAINTEXT-TOKEN-MARKER" not in rows[0][0]
    assert "refresh-1" not in rows[0][1]
    assert rows[0][0].startswith("v1.")


def test_public_record_has_no_token(tmp_path) -> None:
    store, _, _ = make_store(tmp_path)
    _, storage = auth_payload()
    record, _ = store.add_imported(importer.import_from_storage(storage))
    public = record.to_public()
    assert "token" not in public
    assert "refresh_token" not in public
    assert public["has_refresh_token"] is True


def test_status_cooldown_and_relogin(tmp_path) -> None:
    store, _, _ = make_store(tmp_path)
    _, storage = auth_payload()
    record, _ = store.add_imported(importer.import_from_storage(storage))
    store.set_status(record.id, STATUS_COOLDOWN, "quota", cooldown_until=int(time.time()) + 60)
    assert store.get(record.id).status == STATUS_COOLDOWN
    store.set_status(record.id, STATUS_RELOGIN, "bad refresh")
    assert store.get(record.id).status == STATUS_RELOGIN
    # A fresh import clears relogin_required.
    store.add_imported(importer.import_from_storage(storage))
    assert store.get(record.id).status == STATUS_ACTIVE


def test_import_code_single_use(tmp_path) -> None:
    store, _, _ = make_store(tmp_path)
    code, expires_at = store.create_import_code(ttl=60)
    assert expires_at > time.time()
    assert store.validate_import_code(code)
    assert store.consume_import_code(code)
    assert not store.consume_import_code(code)
    assert not store.validate_import_code(code)
    assert not store.consume_import_code("bogus")


def test_delete_cascades(tmp_path) -> None:
    store, db, _ = make_store(tmp_path)
    _, storage = auth_payload()
    record, _ = store.add_imported(importer.import_from_storage(storage))
    store.delete(record.id)
    assert store.get(record.id) is None
    conn = sqlite3.connect(str(db.path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM account_secrets").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM account_devices").fetchone()[0] == 0
    finally:
        conn.close()


def test_events_are_recorded(tmp_path) -> None:
    store, _, _ = make_store(tmp_path)
    _, storage = auth_payload()
    record, _ = store.add_imported(importer.import_from_storage(storage))
    events = store.events(record.id)
    assert events and events[0]["event"] == "account_imported"


def test_manual_import_derives_device(tmp_path) -> None:
    store, _, _ = make_store(tmp_path)
    from tests.conftest import make_jwt

    token = make_jwt({"data": {"id": "manual-user"}, "exp": 4102444800})
    record, created = store.add_manual(token=token, user_id="manual-user")
    assert created
    device = store.get_device(record.id)
    assert device and device.machine_id


def test_manual_without_user_id_uses_profile(tmp_path) -> None:
    store, _, _ = make_store(tmp_path)
    imported = ImportedAccount(
        profile=AccountProfile(user_id="explicit"),
        tokens=TokenBundle(token="t"),
        device=build_device(),
    )
    record, created = store.add_imported(imported)
    assert created and record.user_id == "explicit"
