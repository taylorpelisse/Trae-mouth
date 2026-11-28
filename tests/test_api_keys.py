from __future__ import annotations

import time

import httpx
from fastapi.testclient import TestClient

from app.api_keys import ApiKeyStore
from app.db import Database
from app.main import create_app
from tests.test_api import make_client as make_admin_client, setup_admin
from tests.test_openai_api import make_client as make_chat_client, prepare, sse


def make_store(tmp_path) -> tuple[Database, ApiKeyStore]:
    db = Database(tmp_path / "keys.db")
    db.migrate()
    return db, ApiKeyStore(db)


def test_api_key_store_lifecycle(tmp_path) -> None:
    _, store = make_store(tmp_path)
    record, token = store.create(
        name="client-a", scopes="chat,models", allowed_ips="127.0.0.1"
    )
    assert token.startswith("tm_")
    assert record.prefix == token[:12]
    assert store.find_by_token(token).id == record.id  # type: ignore[union-attr]

    found, error = store.authorize(token, "127.0.0.1")
    assert found is not None and error == ""
    denied, error = store.authorize(token, "10.1.2.3")
    assert denied is None and "ip" in error

    updated = store.update(record.id, {"disabled": True})
    assert updated is not None and updated.disabled
    denied, error = store.authorize(token, "127.0.0.1")
    assert denied is None and "disabled" in error

    store.update(record.id, {"disabled": False})
    assert store.reset_usage(record.id) is not None


def test_api_key_quotas_and_expiry(tmp_path) -> None:
    _, store = make_store(tmp_path)
    record, token = store.create(name="quota", quota_tokens=10)
    assert record.quota_tokens == 10
    store.record_usage(record.id, prompt_tokens=4, completion_tokens=3)
    found, error = store.authorize(token, "127.0.0.1")
    assert found is not None and error == ""
    store.record_usage(record.id, prompt_tokens=2, completion_tokens=2)
    denied, error = store.authorize(token, "127.0.0.1")
    assert denied is None and "token quota" in error

    record2, token2 = store.create(name="requests", quota_requests=1)
    store.record_usage(record2.id, total_tokens=1)
    denied, error = store.authorize(token2, "127.0.0.1")
    assert denied is None and "request quota" in error

    expired, token3 = store.create(
        name="expired", expires_at=int(time.time()) - 1
    )
    assert expired.id
    denied, error = store.authorize(token3, "127.0.0.1")
    assert denied is None and "expired" in error


def test_api_key_usage_events(tmp_path) -> None:
    _, store = make_store(tmp_path)
    record, token = store.create(name="events")
    store.add_event(
        request_id="req_1",
        api_key_id=record.id,
        account_id="acc_1",
        model="test-model",
        endpoint="/v1/chat/completions",
        status=200,
        prompt_tokens=3,
        completion_tokens=5,
        total_tokens=8,
        first_token_ms=120,
        latency_ms=900,
    )
    store.add_event(
        request_id="req_2",
        api_key_id=record.id,
        model="test-model",
        status=500,
        error="boom",
    )
    events = store.events(api_key_id=record.id)
    assert len(events) == 2
    assert events[0]["request_id"] == "req_2"
    summary = store.usage_summary(api_key_id=record.id)
    assert summary["rows"]
    requests = sum(int(row["requests"]) for row in summary["rows"])
    errors = sum(int(row["errors"]) for row in summary["rows"])
    total_tokens = sum(int(row["total_tokens"]) for row in summary["rows"])
    assert requests == 2
    assert errors == 1
    assert total_tokens == 8

    removed = store.purge_events(int(time.time()) + 60)
    assert removed == 2
    assert store.events(api_key_id=record.id) == []
    assert token


def test_usage_by_request_is_scoped_to_api_key(config) -> None:
    content = sse(
        ("output", {"response": "ok"}),
        ("done", {"finish_reason": "stop"}),
    )
    app, client = make_chat_client(config, content, [])
    with client:
        headers = setup_admin(client)
        prepare(app, client)
        first = client.post(
            "/admin/api/keys", json={"name": "first"}, headers=headers
        ).json()
        second = client.post(
            "/admin/api/keys", json={"name": "second"}, headers=headers
        ).json()
        store = app.state.api_keys
        store.add_event(
            request_id="req_usage_1",
            api_key_id=first["key"]["id"],
            model="test-model",
            status=200,
            prompt_tokens=100,
            completion_tokens=5,
            total_tokens=105,
        )
        auth_first = {"Authorization": f"Bearer {first['token']}"}
        auth_second = {"Authorization": f"Bearer {second['token']}"}

        # 账单还没回查：照常返回 200，由调用方决定是否重试
        pending = client.get("/v1/usage/req_usage_1", headers=auth_first)
        assert pending.status_code == 200, pending.text
        body = pending.json()
        assert body["prompt_tokens"] == 100
        assert body["cached_tokens"] == 0
        assert body["billing_ok"] is False

        store.attach_billing("req_usage_1", cached_tokens=80, credits=1.5)
        settled = client.get("/v1/usage/req_usage_1", headers=auth_first).json()
        assert settled["cached_tokens"] == 80
        assert settled["billing_ok"] is True
        assert settled["total_tokens"] == 105
        assert settled["object"] == "usage.event"

        # 别的 key 查不到，用 404 表达，不泄露记录是否存在
        assert (
            client.get("/v1/usage/req_usage_1", headers=auth_second).status_code == 404
        )
        assert (
            client.get("/v1/usage/req_missing", headers=auth_first).status_code == 404
        )


def test_db_key_auth_and_scopes(config) -> None:
    config.api_key = "static-key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="unexpected request")

    app = create_app(config, transport=httpx.MockTransport(handler))
    client = TestClient(app)
    with client:
        headers = setup_admin(client)
        unauthorized = client.get("/v1/models")
        assert unauthorized.status_code == 401
        prepare(app, client)

        created = client.post(
            "/admin/api/keys",
            json={"name": "models-only", "scopes": "models"},
            headers=headers,
        )
        assert created.status_code == 200, created.text
        models_token = created.json()["token"]
        key_id = created.json()["key"]["id"]

        models = client.get(
            "/v1/models", headers={"Authorization": f"Bearer {models_token}"}
        )
        assert models.status_code == 200, models.text
        assert isinstance(models.json()["data"], list)

        denied = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {models_token}"},
            json={
                "model": "missing",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert denied.status_code == 403

        ip_key = client.post(
            "/admin/api/keys",
            json={"name": "ip", "allowed_ips": "10.0.0.0/8"},
            headers=headers,
        ).json()["token"]
        blocked = client.get(
            "/v1/models", headers={"Authorization": f"Bearer {ip_key}"}
        )
        assert blocked.status_code == 403

        patched = client.patch(
            f"/admin/api/keys/{key_id}",
            json={"disabled": True},
            headers=headers,
        )
        assert patched.status_code == 200
        disabled = client.get(
            "/v1/models", headers={"Authorization": f"Bearer {models_token}"}
        )
        assert disabled.status_code == 401


def test_chat_records_usage(config) -> None:
    content = sse(
        ("output", {"response": "Hello"}),
        ("token_usage", {"prompt_tokens": 3, "completion_tokens": 4}),
        ("done", {"finish_reason": "stop"}),
    )
    seen: list[dict] = []
    app, client = make_chat_client(config, content, seen)
    with client:
        headers = setup_admin(client)
        prepare(app, client)
        record, token = app.state.api_keys.create(name="usage-client")
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200, response.text

        events = client.get(
            f"/admin/api/usage/events?api_key_id={record.id}"
        ).json()["events"]
        assert len(events) == 1
        event = events[0]
        assert event["status"] == 200
        assert event["api_key_id"] == record.id
        assert event["account_id"]
        assert event["model"] == "test-model"
        assert event["total_tokens"] == 7
        assert event["first_token_ms"] == 0

        summary = client.get(
            f"/admin/api/usage?api_key_id={record.id}"
        ).json()
        assert summary["rows"][0]["requests"] == 1
        assert summary["rows"][0]["total_tokens"] == 7
        keys = {item["id"]: item for item in summary["keys"]}
        assert keys[record.id]["used_requests"] == 1
        assert keys[record.id]["used_tokens"] == 7
        assert headers


def test_admin_key_crud(config) -> None:
    client = make_admin_client(config)
    with client:
        headers = setup_admin(client)
        created = client.post(
            "/admin/api/keys", json={"name": "crud"}, headers=headers
        )
        assert created.status_code == 200
        key_id = created.json()["key"]["id"]
        token = created.json()["token"]
        assert token.startswith("tm_")

        listed = client.get("/admin/api/keys").json()["keys"]
        assert [item["id"] for item in listed] == [key_id]

        patched = client.patch(
            f"/admin/api/keys/{key_id}",
            json={"name": "renamed", "quota_requests": 10},
            headers=headers,
        )
        assert patched.json()["key"]["name"] == "renamed"
        assert patched.json()["key"]["quota_requests"] == 10

        reset = client.post(
            f"/admin/api/keys/{key_id}/reset", headers=headers
        )
        assert reset.status_code == 200
        assert reset.json()["key"]["used_requests"] == 0

        deleted = client.delete(
            f"/admin/api/keys/{key_id}", headers=headers
        )
        assert deleted.status_code == 200
        assert client.get("/admin/api/keys").json()["keys"] == []
        assert client.delete(
            f"/admin/api/keys/{key_id}", headers=headers
        ).status_code == 404
