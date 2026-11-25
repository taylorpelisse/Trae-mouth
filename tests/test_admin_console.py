from __future__ import annotations

import time

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_jwt
from tests.test_api import setup_admin
from tests.test_openai_api import make_client as make_chat_client, prepare, sse


def test_admin_console_read_endpoints(config) -> None:
    content = sse(
        ("output", {"response": "Hello"}),
        ("token_usage", {"prompt_tokens": 3, "completion_tokens": 4}),
        ("done", {"finish_reason": "stop"}),
    )
    seen: list[dict] = []
    app, client = make_chat_client(config, content, seen)
    with client:
        headers = setup_admin(client)
        account_id = prepare(app, client)
        app.state.accounts.update_profile(account_id, plan="Pro")
        app.state.api_keys.add_event(
            request_id="req_test",
            api_key_id="",
            account_id=account_id,
            model="test-model",
            endpoint="/v1/chat/completions",
            status=200,
            prompt_tokens=3,
            completion_tokens=4,
            total_tokens=7,
            first_token_ms=120,
            latency_ms=500,
        )

        status = client.get("/admin/api/status").json()
        assert status["initialized"] is True

        metrics = client.get("/admin/api/metrics?range=14d").json()
        assert metrics["range_days"] == 14
        assert len(metrics["trend"]) == 14
        assert metrics["usage"]["requests"] == 1
        assert metrics["usage"]["total_tokens"] == 7
        assert metrics["accounts"]["total"] >= 1

        models = client.get("/admin/api/models").json()
        assert models["models"]
        assert models["models"][0]["id"] == "test-model"
        assert "ide" in models["models"][0]["channels"]

        logs = client.get("/admin/api/logs?range=30d&limit=10").json()
        assert logs["items"][0]["model"] == "test-model"
        assert logs["items"][0]["total_tokens"] == 7
        assert logs["items"][0]["account_label"]

        instance = client.get("/admin/api/instance").json()
        assert instance["version"]
        assert instance["accounts"] >= 1

        concurrency = client.get("/admin/api/concurrency").json()
        assert account_id in concurrency["accounts"]

        settings = client.get("/admin/api/settings").json()
        assert settings["settings"]["version"]
        assert settings["settings"]["account_failover_limit"] == (
            app.state.config.account_failover_limit
        )
        assert settings["settings"]["first_token_timeout"] == (
            app.state.config.first_token_timeout
        )
        assert settings["settings"]["tool_result_max_chars"] == (
            app.state.config.tool_result_max_chars
        )
        assert settings["settings"]["history_max_chars"] == (
            app.state.config.history_max_chars
        )
        assert settings["settings"]["history_max_messages"] == (
            app.state.config.history_max_messages
        )
        patched = client.patch(
            "/admin/api/settings",
            headers=headers,
            json={"values": {"default_concurrency": 3}},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["changed"] == ["default_concurrency"]
        assert app.state.config.default_concurrency == 3

        patched = client.patch(
            "/admin/api/settings",
            headers=headers,
            json={
                "values": {
                    "account_failover_limit": 4,
                    "first_token_timeout": 90,
                    "tool_result_max_chars": 50000,
                    "history_max_chars": 500000,
                    "history_max_messages": 20,
                }
            },
        )
        assert patched.status_code == 200, patched.text
        assert set(patched.json()["changed"]) == {
            "account_failover_limit",
            "first_token_timeout",
            "tool_result_max_chars",
            "history_max_chars",
            "history_max_messages",
        }
        assert app.state.config.account_failover_limit == max(
            0, len(app.state.accounts.list()) - 1
        )
        assert app.state.config.first_token_timeout == 90
        assert app.state.config.tool_result_max_chars == 50000
        assert app.state.config.history_max_chars == 500000
        assert app.state.config.history_max_messages == 20

        credits = client.get("/admin/api/credits").json()
        assert credits["supported"] is True
        assert account_id in [item["account_id"] for item in credits["accounts"]]
        history = client.get("/admin/api/credits/history?range=week").json()
        assert history["series"]


def test_account_and_key_notes(config) -> None:
    client = TestClient(create_app(config, transport=httpx.MockTransport(lambda request: httpx.Response(500))))
    with client:
        headers = setup_admin(client)
        token = make_jwt({"data": {"id": "note-user"}, "exp": 4102444800})
        imported = client.post(
            "/admin/api/accounts/import/manual",
            headers=headers,
            json={"token": token, "user_id": "note-user", "label": "note-label"},
        )
        assert imported.status_code == 200, imported.text
        account_id = imported.json()["account"]["id"]
        patched = client.patch(
            f"/admin/api/accounts/{account_id}",
            headers=headers,
            json={"label": "new-label", "note": "local note"},
        )
        assert patched.status_code == 200
        assert patched.json()["account"]["label"] == "new-label"
        assert patched.json()["account"]["note"] == "local note"

        created = client.post(
            "/admin/api/keys",
            headers=headers,
            json={"name": "note-key", "note": "key note"},
        )
        assert created.status_code == 200
        key = created.json()["key"]
        assert key["note"] == "key note"
        updated = client.patch(
            f"/admin/api/keys/{key['id']}",
            headers=headers,
            json={"note": "changed note"},
        )
        assert updated.status_code == 200
        assert updated.json()["key"]["note"] == "changed note"
