from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.accounts.models import build_device
from app.api.openai_routes import OpenAIStreamTranslator, _preflight_stream
from app.main import create_app
from app.upstream.ide_chat import UpstreamError
from tests.conftest import make_jwt


def sse(*events: tuple[str, dict]) -> bytes:
    out = []
    for name, data in events:
        out.append(f"event: {name}")
        out.append("data: " + json.dumps(data, ensure_ascii=False))
        out.append("")
    return ("\n".join(out) + "\n").encode("utf-8")


def make_client(config, content: bytes, seen: list[dict]):
    config.api_key = config.api_key or "test-key"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/llm_raw_chat"):
            seen.append(json.loads(request.content))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=content,
            )
        return httpx.Response(500, text="unexpected request")

    app = create_app(config, transport=httpx.MockTransport(handler))
    client = TestClient(app)
    return app, client


def prepare(app, client, *, max_output: int = 64000) -> str:
    store = app.state.accounts
    token = make_jwt({"data": {"id": "user-1"}, "exp": 4102444800})
    record, _ = store.add_manual(
        token=token,
        user_id="user-1",
        refresh_token="refresh-1",
        token_expires_at=int(time.time()) + 86400,
        device=build_device(),
    )
    app.state.catalog_cache.put(
        record.id,
        {
            "groups": {
                "ide": {
                    "label": "IDE Agent",
                    "functions": ["chat_v3"],
                    "models": [
                        {
                            "config_name": "test-model",
                            "display_name": "Test Model",
                            "provider_models": [
                                "test-model__dev",
                                "test-model__max",
                            ],
                            "functions": ["chat_v3"],
                            "config_source": 1,
                            "context_window": 1000000,
                            "max_context": 1000000,
                            "max_output": max_output,
                            "multimodal": False,
                            "max_mode": True,
                            "reasoning": {
                                "supported": True,
                                "levels": ["light", "high"],
                                "default": "high",
                            },
                        }
                    ],
                    "model_count": 1,
                }
            },
            "errors": {},
            "fetched_at": int(time.time()),
            "model_count": 1,
        },
    )
    return record.id


def auth(config):
    config.api_key = "test-key"
    return {"Authorization": "Bearer test-key"}


def test_chat_nonstream_and_stream(config) -> None:
    content = sse(
        ("output", {"response": "Hel"}),
        ("output", {"response": "lo", "reasoning_content": "think"}),
        ("token_usage", {"prompt_tokens": 3, "completion_tokens": 4}),
        ("done", {"finish_reason": "stop"}),
    )
    seen: list[dict] = []
    app, client = make_client(config, content, seen)
    with client:
        prepare(app, client)
        response = client.post(
            "/v1/chat/completions",
            headers=auth(config),
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["choices"][0]["message"]["content"] == "Hello"
        assert body["choices"][0]["message"]["reasoning_content"] == "think"
        assert body["choices"][0]["finish_reason"] == "stop"
        assert body["usage"]["total_tokens"] == 7
        assert seen and seen[0]["model_name"] == "test-model__max"

        stream_response = client.post(
            "/v1/chat/completions",
            headers=auth(config),
            json={
                "model": "test-model",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert stream_response.status_code == 200
        text = stream_response.text
        assert '"content": "Hel"' in text
        assert '"content": "lo"' in text
        assert "data: [DONE]" in text


def test_chat_tool_calls_and_max_variant(config) -> None:
    block = (
        "<trae_mouth_tool_call>"
        '{"name":"get_weather","arguments":{"city":"Beijing"}}'
        "</trae_mouth_tool_call>"
    )
    content = sse(
        ("output", {"response": block[:20]}),
        ("output", {"response": block[20:]}),
        ("done", {"finish_reason": "stop"}),
    )
    seen: list[dict] = []
    app, client = make_client(config, content, seen)
    with client:
        prepare(app, client)
        response = client.post(
            "/v1/chat/completions",
            headers=auth(config),
            json={
                "model": "test-model",
                "trae_max_mode": True,
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "Weather",
                            "parameters": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                                "required": ["city"],
                            },
                        },
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        message = body["choices"][0]["message"]
        assert message["content"] is None
        assert message["tool_calls"][0]["function"]["name"] == "get_weather"
        assert (
            json.loads(message["tool_calls"][0]["function"]["arguments"])["city"]
            == "Beijing"
        )
        assert body["choices"][0]["finish_reason"] == "tool_calls"
        assert seen and seen[0]["model_name"] == "test-model__max"


def test_chat_requires_api_key(config) -> None:
    content = sse(("done", {}))
    app, client = make_client(config, content, [])
    with client:
        prepare(app, client)
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer wrong"},
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 401


def test_chat_session_id_is_stable_per_conversation(config) -> None:
    content = sse(
        ("output", {"response": "ok"}),
        ("done", {"finish_reason": "stop"}),
    )
    seen: list[dict] = []
    app, client = make_client(config, content, seen)
    with client:
        prepare(app, client)
        first = client.post(
            "/v1/chat/completions",
            headers=auth(config),
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "first question"}],
            },
        )
        second = client.post(
            "/v1/chat/completions",
            headers=auth(config),
            json={
                "model": "test-model",
                "messages": [
                    {"role": "user", "content": "first question"},
                    {"role": "assistant", "content": "ok"},
                    {"role": "user", "content": "follow up"},
                ],
            },
        )
        other = client.post(
            "/v1/chat/completions",
            headers=auth(config),
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "different conversation"}],
            },
        )
        assert first.status_code == second.status_code == other.status_code == 200
        first_session = first.json()["trae"]["session_id"]
        second_session = second.json()["trae"]["session_id"]
        other_session = other.json()["trae"]["session_id"]
        assert first_session == second_session
        assert other_session != first_session
        assert seen[0]["conversation_id"] == seen[1]["conversation_id"]
        assert seen[2]["conversation_id"] != seen[0]["conversation_id"]

        explicit = "11111111-1111-4111-8111-111111111111"
        explicit_response = client.post(
            "/v1/chat/completions",
            headers={**auth(config), "X-Conversation-Id": explicit},
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "client supplied id"}],
            },
        )
        assert explicit_response.status_code == 200
        assert explicit_response.json()["trae"]["session_id"] == explicit
        assert seen[3]["conversation_id"] == explicit


def test_chat_truncated_tool_call_reports_error(config) -> None:
    content = sse(
        (
            "output",
            {
                "response": (
                    '<trae_mouth_tool_call>{"name":"get_weather",'
                    '"arguments":{"city":"Bei'
                )
            },
        ),
        ("done", {"finish_reason": "stop"}),
    )
    app, client = make_client(config, content, [])
    tool = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Weather",
            "parameters": {"type": "object"},
        },
    }
    with client:
        prepare(app, client)
        response = client.post(
            "/v1/chat/completions",
            headers=auth(config),
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [tool],
            },
        )
        assert response.status_code == 502
        assert "truncated" in response.text or "interrupted" in response.text


def test_chat_missing_done_reports_error(config) -> None:
    content = sse(("output", {"response": "partial"}))
    app, client = make_client(config, content, [])
    with client:
        prepare(app, client)
        response = client.post(
            "/v1/chat/completions",
            headers=auth(config),
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 502
        assert "done" in response.text


def test_chat_queue_continue_reports_retryable_error(config) -> None:
    content = sse(("queue_continue", {}))
    app, client = make_client(config, content, [])
    with client:
        prepare(app, client)
        response = client.post(
            "/v1/chat/completions",
            headers=auth(config),
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 503
        assert "retry" in response.text


def test_chat_content_security_maps_to_filter(config) -> None:
    content = sse(
        ("content_security", {"message": "blocked"}),
        ("done", {"finish_reason": "stop"}),
    )
    app, client = make_client(config, content, [])
    with client:
        prepare(app, client)
        response = client.post(
            "/v1/chat/completions",
            headers=auth(config),
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["finish_reason"] == "content_filter"


def test_chat_account_header_selects_requested_account(config) -> None:
    content = sse(
        ("output", {"response": "ok"}),
        ("done", {"finish_reason": "stop"}),
    )
    app, client = make_client(config, content, [])
    with client:
        first_id = prepare(app, client)
        store = app.state.accounts
        token = make_jwt({"data": {"id": "user-2"}, "exp": 4102444800})
        second, _ = store.add_manual(
            token=token,
            user_id="user-2",
            refresh_token="refresh-2",
            token_expires_at=int(time.time()) + 86400,
            device=build_device(),
        )
        app.state.catalog_cache.put(
            second.id, dict(app.state.catalog_cache.get(first_id) or {})
        )
        response = client.post(
            "/v1/chat/completions",
            headers={**auth(config), "X-Account-Id": second.id},
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["trae"]["account_id"] == second.id


def test_chat_without_tools_keeps_tool_markup_as_content(config) -> None:
    raw = '<tool_call>{"name":"get_weather","arguments":{}}</tool_call>'
    content = sse(
        ("output", {"response": raw}),
        ("done", {"finish_reason": "stop"}),
    )
    app, client = make_client(config, content, [])
    with client:
        prepare(app, client)
        response = client.post(
            "/v1/chat/completions",
            headers=auth(config),
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "show an example"}],
            },
        )
        assert response.status_code == 200
        message = response.json()["choices"][0]["message"]
        assert message["content"] == raw
        assert not message.get("tool_calls")


def test_translator_streams_thinking_phase_and_thought_event() -> None:
    translator = OpenAIStreamTranslator("test-model")
    chunks: list[str] = []
    for event in (
        {"event": "output", "data": {"response": "plan", "phase": "thinking"}},
        {"event": "thought", "data": {"thought": " more"}},
        {"event": "output", "data": {"response": "answer", "phase": "answer"}},
        {"event": "done", "data": {"finish_reason": "stop"}},
    ):
        chunks.extend(translator.feed(event))
    chunks.extend(translator.finalize())
    reasoning = ""
    content = ""
    for chunk in chunks:
        if not chunk.startswith("data: {"):
            continue
        payload = json.loads(chunk[6:].strip())
        for choice in payload.get("choices") or []:
            delta = choice.get("delta") or {}
            if isinstance(delta.get("reasoning_content"), str):
                reasoning += delta["reasoning_content"]
            if isinstance(delta.get("content"), str):
                content += delta["content"]
    assert reasoning == "plan more"
    assert content == "answer"


def test_translator_mirrors_reasoning_when_answer_is_empty() -> None:
    translator = OpenAIStreamTranslator("test-model", reasoning_as_content=True)
    chunks: list[str] = []
    chunks.extend(
        translator.feed(
            {"event": "output", "data": {"reasoning_content": "only reasoning"}}
        )
    )
    chunks.extend(translator.feed({"event": "done", "data": {"finish_reason": "stop"}}))
    chunks.extend(translator.finalize())
    content = ""
    for chunk in chunks:
        if not chunk.startswith("data: {"):
            continue
        payload = json.loads(chunk[6:].strip())
        for choice in payload.get("choices") or []:
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content += delta["content"]
    assert content == "only reasoning"
    message = translator.message()
    assert message["content"] == "only reasoning"
    assert message["reasoning_content"] == "only reasoning"


def test_translator_does_not_mirror_reasoning_when_answer_exists() -> None:
    translator = OpenAIStreamTranslator("test-model", reasoning_as_content=True)
    translator.feed(
        {"event": "output", "data": {"reasoning_content": "think", "response": "answer"}}
    )
    translator.feed({"event": "done", "data": {"finish_reason": "stop"}})
    translator.finalize()
    assert translator.message()["content"] == "answer"


def test_reasoning_effort_downgrades_to_supported_level(config) -> None:
    content = sse(
        ("output", {"response": "ok"}),
        ("done", {"finish_reason": "stop"}),
    )
    seen: list[dict] = []
    app, client = make_client(config, content, seen)
    with client:
        prepare(app, client)
        response = client.post(
            "/v1/chat/completions",
            headers=auth(config),
            json={
                "model": "test-model",
                "reasoning_effort": "extra_high",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200, response.text
        assert seen[0]["reasoning_effort"] == "high"
        assert response.json()["trae"]["reasoning_effort_applied"] == "high"


def test_translator_repairs_tool_arguments() -> None:
    schema = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "required": ["x"],
    }
    translator = OpenAIStreamTranslator(
        "test-model",
        tools_expected=True,
        tool_schemas={"get": schema},
    )
    block = (
        "<trae_mouth_tool_call>"
        '{"name":"get","arguments":{"x":"7"}}'
        "</trae_mouth_tool_call>"
    )
    translator.feed({"event": "output", "data": {"response": block}})
    translator.feed({"event": "done", "data": {"finish_reason": "stop"}})
    translator.finalize()
    call = translator.message()["tool_calls"][0]
    assert json.loads(call["function"]["arguments"]) == {"x": 7}


def test_translator_requires_tool_call_when_requested() -> None:
    translator = OpenAIStreamTranslator(
        "test-model",
        tools_expected=True,
        tools_required=True,
    )
    translator.feed({"event": "output", "data": {"response": "plain answer"}})
    translator.feed({"event": "done", "data": {"finish_reason": "stop"}})
    with pytest.raises(UpstreamError):
        translator.finalize()


def test_translator_emits_tool_call_before_finalize() -> None:
    translator = OpenAIStreamTranslator("test-model", tools_expected=True)
    block = (
        "<trae_mouth_tool_call>"
        '{"name":"get","arguments":{"x":1}}'
        "</trae_mouth_tool_call>"
    )
    first = translator.feed({"event": "output", "data": {"response": block}})
    assert any('"tool_calls": [{' in chunk for chunk in first)
    translator.feed({"event": "done", "data": {"finish_reason": "stop"}})
    final = translator.finalize()
    assert not any('"tool_calls": [{' in chunk for chunk in final)


def test_chat_failover_and_affinity(config) -> None:
    config.api_key = "test-key"
    content = sse(
        ("output", {"response": "ok"}),
        ("done", {"finish_reason": "stop"}),
    )
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/llm_raw_chat"):
            seen_tokens.append(request.headers.get("authorization", ""))
            if len(seen_tokens) == 1:
                return httpx.Response(429, text="rate limited")
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=content,
            )
        return httpx.Response(500, text="unexpected")

    app = create_app(config, transport=httpx.MockTransport(handler))
    client = TestClient(app)
    with client:
        first_id = prepare(app, client)
        store = app.state.accounts
        token = make_jwt({"data": {"id": "user-2"}, "exp": 4102444800})
        second, _ = store.add_manual(
            token=token,
            user_id="user-2",
            refresh_token="refresh-2",
            token_expires_at=int(time.time()) + 86400,
            device=build_device(),
        )
        app.state.catalog_cache.put(
            second.id, dict(app.state.catalog_cache.get(first_id) or {})
        )
        payload = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "same conversation"}],
        }
        first_response = client.post(
            "/v1/chat/completions",
            headers=auth(config),
            json=payload,
        )
        assert first_response.status_code == 200, first_response.text
        body = first_response.json()
        assert body["trae"]["account_id"] == second.id
        assert body["trae"]["failover"] is True
        assert store.get(first_id).status == "cooldown"

        second_response = client.post(
            "/v1/chat/completions",
            headers=auth(config),
            json=payload,
        )
        assert second_response.status_code == 200, second_response.text
        assert second_response.json()["trae"]["account_id"] == second.id
        assert len(seen_tokens) == 3
        assert seen_tokens[1] == seen_tokens[2]


def test_preflight_treats_empty_completion_as_empty() -> None:
    async def events():
        yield {"event": "metadata", "data": {}}
        yield {"event": "output", "data": {"phase": "thinking"}}
        yield {"event": "done", "data": {}}

    buffered, ok = asyncio.run(_preflight_stream(events(), 5))
    assert ok is False
    assert buffered


def test_preflight_returns_on_first_payload() -> None:
    async def events():
        yield {"event": "metadata", "data": {}}
        yield {"event": "output", "data": {"response": "hi"}}
        yield {"event": "done", "data": {}}

    buffered, ok = asyncio.run(_preflight_stream(events(), 5))
    assert ok is True
    assert buffered[-1]["data"]["response"] == "hi"


def test_chat_empty_response_retries_without_cooldown(config) -> None:
    config.api_key = "test-key"
    empty = sse(("metadata", {}), ("done", {}))
    good = sse(("output", {"response": "ok"}), ("done", {"finish_reason": "stop"}))
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/llm_raw_chat"):
            seen_tokens.append(request.headers.get("authorization", ""))
            content = empty if len(seen_tokens) == 1 else good
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=content,
            )
        return httpx.Response(500, text="unexpected")

    app = create_app(config, transport=httpx.MockTransport(handler))
    client = TestClient(app)
    with client:
        first_id = prepare(app, client)
        store = app.state.accounts
        token = make_jwt({"data": {"id": "user-2"}, "exp": 4102444800})
        second, _ = store.add_manual(
            token=token,
            user_id="user-2",
            refresh_token="refresh-2",
            token_expires_at=int(time.time()) + 86400,
            device=build_device(),
        )
        app.state.catalog_cache.put(
            second.id, dict(app.state.catalog_cache.get(first_id) or {})
        )
        response = client.post(
            "/v1/chat/completions",
            headers=auth(config),
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "empty then ok"}],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["choices"][0]["message"]["content"] == "ok"
        assert response.json()["trae"]["account_id"] == second.id
        assert store.get(first_id).status == "active"
        assert len(seen_tokens) == 2
