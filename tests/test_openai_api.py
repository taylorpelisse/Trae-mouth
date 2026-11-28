from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.accounts.models import build_device
from app.api.openai_routes import (
    OpenAIStreamTranslator,
    _REASONING_RANKS,
    _nearest_reasoning_level,
    _preflight_stream,
    _upstream_error_status,
)
from app.main import create_app
from app.upstream.ide_chat import UpstreamError
from app.upstream.model_catalog import openai_effort
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
    # 目录是全局快照，播种一次所有账号都能用
    app.state.catalog_cache.put(
        "ide",
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


def test_models_endpoint_returns_openai_compatible_fields(config) -> None:
    app, client = make_client(config, "", [])
    with client:
        prepare(app, client)
        response = client.get("/v1/models", headers=auth(config))

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["object"] == "list"
        entry = next(item for item in body["data"] if item["id"] == "test-model")
        assert set(entry) == {
            "id",
            "object",
            "created",
            "owned_by",
            "name",
            "context_length",
            "architecture",
            "top_provider",
            "supported_parameters",
            "reasoning",
        }
        assert entry["object"] == "model"
        assert entry["owned_by"] == "trae"
        assert entry["name"] == "Test Model"
        # 网关按最大上下文工作，对外声明的也是它
        assert entry["context_length"] == 1000000
        assert entry["architecture"]["input_modalities"] == ["text"]
        assert entry["top_provider"]["max_completion_tokens"] == 64000
        assert entry["supported_parameters"] == [
            "parallel_tool_calls",
            "reasoning_effort",
            "tool_choice",
            "tools",
        ]
        assert entry["reasoning"] == {
            "supported": True,
            "supported_efforts": ["low", "high"],
            "default_effort": "high",
        }
        # 账号池编号、内部路由字段和旧的 trae 扩展块都不出网关
        for private in ("account_ids", "provider_models", '"trae":', "functions"):
            assert private not in response.text


@pytest.mark.parametrize("level", ["light", "medium", "high", "extra_high", "xhigh"])
def test_advertised_efforts_resolve_back_to_upstream_levels(level) -> None:
    # 对外声明的写法发回网关后，必须仍落在原来的上游档位上
    assert _nearest_reasoning_level(openai_effort(level, _REASONING_RANKS), [level], "") == level


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


def test_chat_reports_cached_tokens_and_request_id(config) -> None:
    content = sse(
        ("output", {"response": "ok"}),
        (
            "token_usage",
            {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "cache_read_tokens": 40,
            },
        ),
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
        request_id = response.headers.get("X-Request-Id")
        assert request_id and request_id.startswith("req_")
        body = response.json()
        assert body["usage"]["prompt_tokens_details"]["cached_tokens"] == 40
        # 对外的缓存数也要落到内部账本，否则管理端永远是 0
        events = app.state.api_keys.events()
        assert events and events[0]["request_id"] == request_id
        assert events[0]["cached_tokens"] == 40


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
        # 对外暴露的会话 id 按对话稳定，客户端据此做粘性
        assert first_session == second_session
        assert other_session != first_session
        # 发给上游的会话 id 每次请求都不同：账单按会话结算，
        # 共用一个 id 只能拿到累计值，算不出单次成本
        assert seen[0]["conversation_id"] != seen[1]["conversation_id"]
        assert seen[2]["conversation_id"] != seen[0]["conversation_id"]
        assert seen[0]["conversation_id"] and seen[1]["conversation_id"]

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
        assert seen[3]["conversation_id"] != explicit


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


def test_missing_tool_arguments_are_passed_through() -> None:
    """缺必填字段不再中断整轮请求，调用原样下发由客户端报错。"""
    schema = {
        "type": "object",
        "properties": {"file_path": {"type": "string"}},
        "required": ["file_path"],
    }
    translator = OpenAIStreamTranslator(
        "test-model",
        tools_expected=True,
        tool_schemas={"Read": schema},
        strict_tool_args=True,
    )
    block = (
        "<trae_mouth_tool_call>"
        '{"name":"Read","arguments":{"path":"a.py"}}'
        "</trae_mouth_tool_call>"
    )
    translator.feed({"event": "output", "data": {"response": block}})
    translator.feed({"event": "done", "data": {"finish_reason": "stop"}})
    translator.finalize()
    calls = translator.message()["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "Read"
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "a.py"}


def test_unparseable_tool_arguments_are_request_errors() -> None:
    """参数连 JSON 都解析不出来时仍按请求错误处理。"""
    schema = {
        "type": "object",
        "properties": {"file_path": {"type": "string"}},
        "required": ["file_path"],
    }
    translator = OpenAIStreamTranslator(
        "test-model",
        tools_expected=True,
        tool_schemas={"Read": schema},
        strict_tool_args=True,
    )
    with pytest.raises(UpstreamError) as info:
        translator.feed(
            {
                "event": "output",
                "data": {
                    "tool_calls": [{"name": "Read", "arguments": "{not json"}],
                },
            }
        )
    assert info.value.status_code == 400
    assert "not valid JSON" in str(info.value)


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


def test_stream_multiple_tool_calls_in_one_turn() -> None:
    """一轮两个调用、且标记被拆到多个事件里，都要按两个 index 发出去。"""
    translator = OpenAIStreamTranslator("test-model", tools_expected=True)
    translator.feed(
        {
            "event": "output",
            "data": {"response": "<tool_call><function=read_file><parameter=path>a.t"},
        }
    )
    translator.feed(
        {
            "event": "output",
            "data": {
                "response": (
                    "xt</parameter></function>"
                    "<function=read_file><parameter=path>b.txt</parameter></function>"
                )
            },
        }
    )
    translator.feed({"event": "done", "data": {"finish_reason": "stop"}})
    chunks = translator.finalize()
    emitted = []
    for chunk in chunks:
        text = chunk[len("data: ") :].strip() if chunk.startswith("data: ") else chunk
        try:
            delta = json.loads(text)["choices"][0]["delta"]
        except (ValueError, KeyError, IndexError):
            continue
        emitted.extend(delta.get("tool_calls") or [])
    calls = translator.message()["tool_calls"]
    assert [call["function"]["name"] for call in calls] == ["read_file", "read_file"]
    assert [json.loads(call["function"]["arguments"]) for call in calls] == [
        {"path": "a.txt"},
        {"path": "b.txt"},
    ]
    assert len({call["id"] for call in calls}) == 2
    assert len(emitted) == 2
    assert sorted(item["index"] for item in emitted) == [0, 1]


def test_stream_unparseable_call_attempt_does_not_leak_markup() -> None:
    """写到一半的调用标记不能当正文漏给用户。"""
    translator = OpenAIStreamTranslator("test-model", tools_expected=True)
    translator.feed(
        {
            "event": "output",
            "data": {
                "response": (
                    "<tool_call><function=read_file><parameter=path>a.txt"
                    "</parameter><parameter=content>hel"
                )
            },
        }
    )
    translator.feed({"event": "done", "data": {"finish_reason": "stop"}})
    with pytest.raises(UpstreamError):
        translator.finalize()


def test_stream_truncated_call_keeps_prose_only() -> None:
    """有正文时只丢标记，正文照常输出。"""
    translator = OpenAIStreamTranslator("test-model", tools_expected=True)
    chunks = translator.feed(
        {
            "event": "output",
            "data": {
                "response": (
                    "好的，我来读。\n"
                    "<tool_call><function=read_file><parameter=path>a.txt"
                    "</parameter><parameter=content>hel"
                )
            },
        }
    )
    translator.feed({"event": "done", "data": {"finish_reason": "stop"}})
    chunks.extend(translator.finalize())
    body = "".join(chunks)
    assert "好的，我来读。" in body
    assert "<tool_call" not in body
    assert "<parameter" not in body
    assert not translator.message().get("tool_calls")


def test_undeclared_tool_names_are_dropped() -> None:
    """模型编造的工具名不能下发给客户端。"""
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    translator = OpenAIStreamTranslator(
        "test-model", tools_expected=True, tool_schemas={"read_file": schema}
    )
    translator.feed(
        {
            "event": "output",
            "data": {"response": '<tool_call>{"name":"tool_name","arguments":{}}</tool_call>'},
        }
    )
    translator.feed({"event": "done", "data": {"finish_reason": "stop"}})
    with pytest.raises(UpstreamError) as info:
        translator.finalize()
    assert "neither text nor a tool call" in str(info.value)


def test_parallel_wrapper_expands_to_declared_calls() -> None:
    """multi_tool_use.parallel 拆成真实调用。"""
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    translator = OpenAIStreamTranslator(
        "test-model", tools_expected=True, tool_schemas={"read_file": schema}
    )
    wrapper = {
        "name": "multi_tool_use.parallel",
        "arguments": {
            "tool_uses": [
                {
                    "recipient_name": "functions.read_file",
                    "parameters": {"path": "a.txt"},
                },
                {
                    "recipient_name": "functions.read_file",
                    "parameters": {"path": "b.txt"},
                },
            ]
        },
    }
    translator.feed(
        {
            "event": "output",
            "data": {"response": "<tool_call>" + json.dumps(wrapper) + "</tool_call>"},
        }
    )
    translator.feed({"event": "done", "data": {"finish_reason": "stop"}})
    translator.finalize()
    calls = translator.message()["tool_calls"]
    assert [call["function"]["name"] for call in calls] == ["read_file", "read_file"]
    assert [json.loads(call["function"]["arguments"]) for call in calls] == [
        {"path": "a.txt"},
        {"path": "b.txt"},
    ]


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


def test_upstream_param_error_is_request_level() -> None:
    assert (
        _upstream_error_status(
            {"code": 4001, "message": "We're sorry, the param is invalid."}
        )
        == 400
    )
    assert _upstream_error_status({"code": 9999, "message": "upstream down"}) == 502


def test_chat_stops_after_repeated_upstream_error(config) -> None:
    config.api_key = "test-key"
    config.account_failover_limit = 5
    repeated = sse(("error", {"code": 9999, "message": "same failure"}))
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/llm_raw_chat"):
            seen_tokens.append(request.headers.get("authorization", ""))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=repeated,
            )
        return httpx.Response(500, text="unexpected")

    app = create_app(config, transport=httpx.MockTransport(handler))
    client = TestClient(app)
    with client:
        first_id = prepare(app, client)
        store = app.state.accounts
        extra_ids: list[str] = []
        for index in (2, 3):
            token = make_jwt({"data": {"id": f"user-{index}"}, "exp": 4102444800})
            record, _ = store.add_manual(
                token=token,
                user_id=f"user-{index}",
                refresh_token=f"refresh-{index}",
                token_expires_at=int(time.time()) + 86400,
                device=build_device(),
            )
            extra_ids.append(record.id)
        response = client.post(
            "/v1/chat/completions",
            headers=auth(config),
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "same error"}],
            },
        )
        assert response.status_code == 502
        assert "same failure" in response.text
        assert len(seen_tokens) == 2
        assert store.get(first_id).status == "cooldown"
        assert store.get(extra_ids[0]).status == "active"
        assert store.get(extra_ids[1]).status == "active"


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


def collect_deltas(chunks: list[str]) -> tuple[str, list[tuple[str, dict]]]:
    content = ""
    calls: list[tuple[str, dict]] = []
    for chunk in chunks:
        if not chunk.startswith("data: {"):
            continue
        for choice in json.loads(chunk[6:].strip())["choices"]:
            delta = choice.get("delta") or {}
            content += delta.get("content") or ""
            for call in delta.get("tool_calls") or []:
                calls.append(
                    (call["function"]["name"], json.loads(call["function"]["arguments"]))
                )
    return content, calls


def translate(pieces: list[str], **options) -> tuple[str, list[tuple[str, dict]]]:
    translator = OpenAIStreamTranslator("test-model", **options)
    chunks: list[str] = []
    for piece in pieces:
        chunks.extend(translator.feed({"event": "output", "data": {"response": piece}}))
    translator.feed({"event": "done", "data": {"finish_reason": "stop"}})
    chunks.extend(translator.finalize())
    return collect_deltas(chunks)


def test_translator_delivers_call_whose_argument_quotes_dsml() -> None:
    sample = (
        '<||DSML||invoke name="Read">'
        '<||DSML||parameter name="file_path" string="true">a.py'
        "</||DSML||parameter></||DSML||invoke>"
    )
    block = (
        "我来改。\n< | | DSML | | tool_calls>\n"
        '< | | DSML | | invoke name="Edit">\n'
        "< | | DSML | | parameter name=\"file_path\" string=\"true\">"
        "tests/x.py</ | | DSML | | parameter>\n"
        "< | | DSML | | parameter name=\"old_string\" string=\"true\">"
        f"old = '{sample}'\n"
        "</ | | DSML | | parameter>\n"
        "< | | DSML | | parameter name=\"new_string\" string=\"true\">"
        "new</ | | DSML | | parameter>\n"
        "</ | | DSML | | invoke>\n</ | | DSML | | invoke>"
    )
    content, calls = translate(
        [block[:60], block[60:140], block[140:]],
        tools_expected=True,
        tool_schemas={
            "Edit": {
                "type": "object",
                "properties": {
                    name: {"type": "string"}
                    for name in ("file_path", "old_string", "new_string")
                },
                "required": ["file_path", "old_string", "new_string"],
            }
        },
        strict_tool_args=True,
    )
    assert content == "我来改。\n"
    assert calls == [
        (
            "Edit",
            {
                "file_path": "tests/x.py",
                "old_string": f"old = '{sample}'\n",
                "new_string": "new",
            },
        )
    ]


def test_translator_does_not_leak_held_marker_at_finalize() -> None:
    content, calls = translate(
        [
            "我把工具调用链路重新过一遍。\n\n",
            "< | | DS",
            "ML | | tool_",
            'calls>\n< | | DSML | | invoke name="Bash">',
            "< | | DSML | | parameter name=\"command\" string=\"true\">"
            "echo hi</ | | DSML | | parameter>",
            "\n</ | | DSML | | invoke>",
        ],
        tools_expected=True,
    )
    assert content == "我把工具调用链路重新过一遍。\n\n"
    assert calls == [("Bash", {"command": "echo hi"})]


def test_translator_does_not_leak_fullwidth_marker_split_across_chunks() -> None:
    # 线上真实分块：全角竖线的前半截单独一块，" calls>" 在下一块
    content, calls = translate(
        [
            "我来看一下 <｜｜DSML｜｜",
            " calls>\n"
            '<｜｜DSML｜｜ invoke name="Bash">'
            '<｜｜DSML｜｜ parameter name="command" string="true">'
            "dir C:\\Users\\24183\\.zcode\\v2"
            "</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>",
        ],
        tools_expected=True,
    )
    assert content == "我来看一下 "
    assert calls == [("Bash", {"command": r"dir C:\Users\24183\.zcode\v2"})]


def test_translator_does_not_leak_fullwidth_marker_without_prose() -> None:
    # 没有任何正文时，客户端不应收到记号文本
    content, calls = translate(
        [
            "<｜｜DSML｜｜",
            " calls>\n"
            '<｜｜DSML｜｜ invoke name="Read">'
            '<｜｜DSML｜｜ parameter name="file_path" string="true">a.py'
            "</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>",
        ],
        tools_expected=True,
    )
    assert content == ""
    assert calls == [("Read", {"file_path": "a.py"})]


def test_translator_keeps_quoted_call_marker_as_text() -> None:
    content, calls = translate(
        ["格式是 <tool_call> 这样写\n", "结束"], tools_expected=True
    )
    assert content == "格式是 <tool_call> 这样写\n结束"
    assert calls == []


def test_translator_truncated_native_call_is_not_account_fault() -> None:
    translator = OpenAIStreamTranslator("test-model", tools_expected=True)
    translator.feed(
        {
            "event": "output",
            "data": {
                "response": (
                    "<||DSML||tool_calls>"
                    '<||DSML||invoke name="Edit">'
                    '<||DSML||parameter name="new_string" string="true">half'
                )
            },
        }
    )
    translator.feed({"event": "done", "data": {"finish_reason": "stop"}})
    with pytest.raises(UpstreamError) as info:
        translator.finalize()
    assert "truncated" in str(info.value)
    assert info.value.account_fault is False


def test_model_output_errors_are_not_account_faults() -> None:
    required = OpenAIStreamTranslator(
        "test-model", tools_expected=True, tools_required=True
    )
    required.feed({"event": "output", "data": {"response": "plain answer"}})
    required.feed({"event": "done", "data": {"finish_reason": "stop"}})
    with pytest.raises(UpstreamError) as info:
        required.finalize()
    assert info.value.account_fault is False

    empty = OpenAIStreamTranslator("test-model", tools_expected=True)
    empty.feed({"event": "done", "data": {"finish_reason": "stop"}})
    with pytest.raises(UpstreamError) as info:
        empty.finalize()
    assert info.value.account_fault is False

    schema = {
        "type": "object",
        "properties": {"file_path": {"type": "string"}},
        "required": ["file_path"],
    }
    invalid = OpenAIStreamTranslator(
        "test-model",
        tools_expected=True,
        tool_schemas={"Read": schema},
        strict_tool_args=True,
    )
    with pytest.raises(UpstreamError) as info:
        invalid.feed(
            {
                "event": "output",
                "data": {
                    "tool_calls": [{"name": "Read", "arguments": "{not json"}],
                },
            }
        )
    assert info.value.status_code == 400
    assert info.value.account_fault is False


def test_upstream_error_with_embedded_status_is_classified() -> None:
    assert (
        _upstream_error_status(
            {
                "message": (
                    'model service is unavailable Error code: 400 - '
                    '{"code":"InvalidParameter"}'
                )
            }
        )
        == 400
    )
    assert _upstream_error_status({"message": "Error code: 404 - no model"}) == 400
    assert _upstream_error_status({"message": "Error code: 429 - busy"}) == 429
    assert _upstream_error_status({"message": "Error code: 401 - expired"}) == 401
    assert _upstream_error_status({"message": "Error code: 500 - down"}) == 502


def test_chat_bad_model_output_retries_without_cooldown(config) -> None:
    config.api_key = "test-key"
    truncated = sse(
        (
            "output",
            {
                "response": (
                    "<||DSML||tool_calls>"
                    '<||DSML||invoke name="Edit">'
                    '<||DSML||parameter name="new_string" string="true">half'
                )
            },
        ),
        ("done", {"finish_reason": "stop"}),
    )
    good = sse(("output", {"response": "ok"}), ("done", {"finish_reason": "stop"}))
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/llm_raw_chat"):
            seen_tokens.append(request.headers.get("authorization", ""))
            content = truncated if len(seen_tokens) == 1 else good
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
        response = client.post(
            "/v1/chat/completions",
            headers=auth(config),
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "edit it"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "Edit",
                            "parameters": {
                                "type": "object",
                                "properties": {"new_string": {"type": "string"}},
                                "required": ["new_string"],
                            },
                        },
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["choices"][0]["message"]["content"] == "ok"
        assert response.json()["trae"]["account_id"] == second.id
        assert store.get(first_id).status == "active"
        assert len(seen_tokens) == 2


def test_chat_stream_invalid_arguments_keep_account_active(config) -> None:
    content = sse(
        (
            "output",
            {"tool_calls": [{"name": "Read", "arguments": "{not json"}]},
        ),
        ("done", {"finish_reason": "stop"}),
    )
    app, client = make_client(config, content, [])
    tool = {
        "type": "function",
        "function": {
            "name": "Read",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
    }
    with client:
        first_id = prepare(app, client)
        with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=auth(config),
            json={
                "model": "test-model",
                "stream": True,
                "messages": [{"role": "user", "content": "read it"}],
                "tools": [tool],
            },
        ) as response:
            assert response.status_code == 200
            body = "".join(response.iter_text())
        assert '"error"' in body
        assert "not valid JSON" in body
        assert "data: [DONE]" in body
        assert app.state.accounts.get(first_id).status == "active"


def test_chat_request_level_upstream_error_does_not_cool_accounts(config) -> None:
    config.api_key = "test-key"
    bad = sse(
        (
            "error",
            {
                "message": (
                    "model service is unavailable Error code: 400 - "
                    '{"code":"InvalidParameter"}'
                )
            },
        )
    )
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/llm_raw_chat"):
            seen_tokens.append(request.headers.get("authorization", ""))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=bad,
            )
        return httpx.Response(500, text="unexpected")

    app = create_app(config, transport=httpx.MockTransport(handler))
    client = TestClient(app)
    with client:
        account_ids = [prepare(app, client)]
        store = app.state.accounts
        for index in (2, 3):
            token = make_jwt({"data": {"id": f"user-{index}"}, "exp": 4102444800})
            record, _ = store.add_manual(
                token=token,
                user_id=f"user-{index}",
                refresh_token=f"refresh-{index}",
                token_expires_at=int(time.time()) + 86400,
                device=build_device(),
            )
            account_ids.append(record.id)
        response = client.post(
            "/v1/chat/completions",
            headers=auth(config),
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 400
        assert "InvalidParameter" in response.text
        assert len(seen_tokens) == 1
        assert all(store.get(item).status == "active" for item in account_ids)


def _finish_reason_of(chunks: list[str]) -> str:
    for chunk in chunks:
        if not chunk.startswith("data: {"):
            continue
        for choice in json.loads(chunk[6:].strip())["choices"]:
            if choice.get("finish_reason"):
                return choice["finish_reason"]
    return ""


RUN_COMMAND_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "run_in_background": {"type": "boolean"},
    },
    "required": ["command"],
}


def test_translator_recovers_fenced_bare_json_call() -> None:
    translator = OpenAIStreamTranslator(
        "test-model",
        tools_expected=True,
        tool_schemas={"RunCommand": RUN_COMMAND_SCHEMA},
    )
    for piece in (
        "编译 v0.46:\n",
        "```json\n",
        '{"command":"gradle :app:assembleRelease","run_in_background":true}',
        "\n```",
    ):
        translator.feed({"event": "output", "data": {"response": piece}})
    translator.feed({"event": "done", "data": {"finish_reason": "stop"}})
    chunks = translator.finalize()
    content, calls = collect_deltas(chunks)
    assert calls == [
        ("RunCommand", {"command": "gradle :app:assembleRelease", "run_in_background": True})
    ]
    assert _finish_reason_of(chunks) == "tool_calls"
    # 非流式内容是最终展示给客户端的正文，围栏块应被剥掉。
    assert translator.result()["choices"][0]["message"]["content"] == "编译 v0.46:"


def test_translator_ambiguous_fenced_call_is_not_stop() -> None:
    schemas = {
        "Bash": {"properties": {"command": {"type": "string"}}, "required": ["command"]},
        "Shell": {"properties": {"command": {"type": "string"}}, "required": ["command"]},
    }
    translator = OpenAIStreamTranslator(
        "test-model", tools_expected=True, tool_schemas=schemas
    )
    chunks = translator.feed(
        {"event": "output", "data": {"response": '```json\n{"command": "ls"}\n```'}}
    )
    translator.feed({"event": "done", "data": {"finish_reason": "stop"}})
    chunks.extend(translator.finalize())
    content, calls = collect_deltas(chunks)
    assert calls == []
    assert "```json" in content
    assert _finish_reason_of(chunks) == "length"


def test_translator_recovers_call_from_reasoning() -> None:
    translator = OpenAIStreamTranslator(
        "test-model",
        tools_expected=True,
        tool_schemas={
            "Read": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            }
        },
    )
    translator.feed(
        {
            "event": "reasoning_delta",
            "data": {
                "delta": (
                    "<tool_call><function=Read><parameter=file_path>a.py"
                    "</parameter></function></tool_call>"
                )
            },
        }
    )
    translator.feed({"event": "done", "data": {"finish_reason": "stop"}})
    chunks = translator.finalize()
    _content, calls = collect_deltas(chunks)
    assert calls == [("Read", {"file_path": "a.py"})]
    assert _finish_reason_of(chunks) == "tool_calls"


def test_translator_dropped_truncated_call_is_length_not_stop() -> None:
    translator = OpenAIStreamTranslator("test-model", tools_expected=True)
    translator.feed(
        {
            "event": "output",
            "data": {"response": '先说一句\n<trae_mouth_tool_call>{"command": "half'},
        }
    )
    translator.feed({"event": "done", "data": {"finish_reason": "stop"}})
    chunks = translator.finalize()
    assert _finish_reason_of(chunks) == "length"


def test_translator_dropped_name_args_call_is_length_not_stop() -> None:
    translator = OpenAIStreamTranslator("test-model", tools_expected=True)
    translator.feed(
        {
            "event": "output",
            "data": {
                "response": (
                    "先说一句\n<tool_call><name>run_command</name>"
                    "<args><command>echo half"
                )
            },
        }
    )
    translator.feed({"event": "done", "data": {"finish_reason": "stop"}})
    chunks = translator.finalize()
    assert _finish_reason_of(chunks) == "length"


def test_translator_streams_plain_text_without_marker_overhead() -> None:
    translator = OpenAIStreamTranslator("test-model", tools_expected=True)
    text = "编译完成，下面开始测试。" * 10
    chunks: list[str] = []
    for char in text:
        chunks.extend(translator.feed({"event": "output", "data": {"response": char}}))
    translator.feed({"event": "done", "data": {"finish_reason": "stop"}})
    chunks.extend(translator.finalize())
    content, calls = collect_deltas(chunks)
    assert content == text
    assert calls == []
