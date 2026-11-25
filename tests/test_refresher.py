from __future__ import annotations

import json
import threading
import time

import httpx

from app.accounts import importer
from app.accounts.models import TokenBundle
from app.accounts.refresher import TokenRefresher
from app.accounts.store import STATUS_ACTIVE, STATUS_RELOGIN, AccountStore
from app.db import Database
from app.security import SecretBox
from tests.conftest import auth_payload


def make_store(tmp_path) -> AccountStore:
    db = Database(tmp_path / "refresh.db")
    db.migrate()
    return AccountStore(db, SecretBox(b"r" * 32))


def ok_transport(counter: list[int] | None = None, delay: float = 0.0):
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        if counter is not None:
            with lock:
                counter.append(1)
        if delay:
            time.sleep(delay)
        payload = {
            "Result": {
                "Token": "new-token",
                "RefreshToken": "new-refresh",
                "TokenExpireAt": "2099-01-01T00:00:00.000Z",
                "RefreshExpireAt": "2099-06-01T00:00:00.000Z",
                "ClientID": "ono9krqynydwx5",
                "UserInfo": json.dumps({"UserID": "user-1"}),
            }
        }
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def test_refresh_success(tmp_path) -> None:
    store = make_store(tmp_path)
    _, storage = auth_payload()
    record, _ = store.add_imported(importer.import_from_storage(storage))
    store.update_tokens(
        record.id,
        TokenBundle(
            token="old-token",
            refresh_token="refresh-1",
            token_expires_at=int(time.time()) + 60,
            refresh_expires_at=int(time.time()) + 86400,
        ),
    )
    refresher = TokenRefresher(store, transport=ok_transport())
    result = refresher.refresh(record.id)
    assert result.ok and not result.skipped
    secrets = store.get_secrets(record.id)
    assert secrets.token == "new-token"
    assert secrets.refresh_token == "new-refresh"
    assert secrets.token_expires_at == 4070908800


def test_refresh_single_flight(tmp_path) -> None:
    store = make_store(tmp_path)
    _, storage = auth_payload()
    record, _ = store.add_imported(importer.import_from_storage(storage))
    store.update_tokens(
        record.id,
        TokenBundle(
            token="old-token",
            refresh_token="refresh-1",
            token_expires_at=int(time.time()) + 10,
            refresh_expires_at=int(time.time()) + 86400,
        ),
    )
    counter: list[int] = []
    refresher = TokenRefresher(store, transport=ok_transport(counter, delay=0.2))
    results: list = []

    def call() -> None:
        results.append(refresher.refresh(record.id))

    threads = [threading.Thread(target=call) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(counter) == 1
    assert all(result.ok for result in results)
    assert any(not result.skipped for result in results)
    assert sum(1 for result in results if result.skipped) >= 1


def test_refresh_invalid_marks_relogin(tmp_path) -> None:
    store = make_store(tmp_path)
    _, storage = auth_payload()
    record, _ = store.add_imported(importer.import_from_storage(storage))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid refresh token")

    refresher = TokenRefresher(store, transport=httpx.MockTransport(handler))
    result = refresher.refresh(record.id, force=True)
    assert not result.ok
    record = store.get(record.id)
    assert record.status == STATUS_RELOGIN


def test_refresh_without_refresh_token(tmp_path) -> None:
    store = make_store(tmp_path)
    _, storage = auth_payload(refresh_token="")
    record, _ = store.add_imported(importer.import_from_storage(storage))
    refresher = TokenRefresher(store, transport=ok_transport())
    result = refresher.refresh(record.id, force=True)
    assert result.ok and result.skipped
    assert store.get(record.id).status == STATUS_ACTIVE

    store.update_tokens(
        record.id,
        TokenBundle(
            token="old",
            refresh_token="",
            token_expires_at=int(time.time()) - 10,
        ),
    )
    result = refresher.refresh(record.id, force=True)
    assert not result.ok
    assert store.get(record.id).status == STATUS_RELOGIN


def test_due_accounts(tmp_path) -> None:
    store = make_store(tmp_path)
    _, storage = auth_payload()
    record, _ = store.add_imported(importer.import_from_storage(storage))
    store.update_tokens(
        record.id,
        TokenBundle(
            token="old",
            refresh_token="refresh-1",
            token_expires_at=int(time.time()) + 60,
            refresh_expires_at=int(time.time()) + 86400,
        ),
    )
    refresher = TokenRefresher(store, transport=ok_transport(), refresh_window=1800)
    due = refresher.due_accounts()
    assert [row.id for row in due] == [record.id]
    assert store.get(record.id).status == STATUS_ACTIVE
