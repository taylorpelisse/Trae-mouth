from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.accounts.models import build_device
from app.upstream import ide_chat


def test_text_accumulator_handles_delta_and_cumulative() -> None:
    acc = ide_chat.TextAccumulator()
    assert acc.add("The") == "The"
    assert acc.add(" user") == " user"
    assert acc.text == "The user"
    assert acc.add("The user wants") == " wants"
    assert acc.add("The user") == ""
    assert acc.text == "The user wants"


def test_text_accumulator_delta_keeps_repeated_chunks() -> None:
    acc = ide_chat.TextAccumulator()
    assert acc.add_delta("a") == "a"
    assert acc.add_delta("a") == "a"
    assert acc.text == "aa"
    assert acc.add_delta("bc") == "bc"
    assert acc.text == "aabc"


def test_tool_block_filter_exposes_incomplete_payload() -> None:
    flt = ide_chat.ToolBlockFilter()
    visible, calls = flt.feed(
        'before<trae_mouth_tool_call>{"name":"get_weather","arguments":{"city":"Bei'
    )
    assert visible == "before"
    assert calls == []
    assert flt.incomplete.startswith('{"name":"get_weather"')

    visible, calls = flt.feed(
        'before<trae_mouth_tool_call>{"name":"get_weather","arguments":{"city":"Beijing"}}'
        "</trae_mouth_tool_call>"
    )
    assert visible == "before"
    assert calls == [{"name": "get_weather", "arguments": {"city": "Beijing"}}]
    assert flt.incomplete == ""


def test_tool_block_filter_recovers_legacy_text_call() -> None:
    flt = ide_chat.ToolBlockFilter()
    text = (
        "I will read it now.\n"
        '[client tool call] id=call_1 name=read_file arguments={"path":"/tmp/a.txt"}\n'
    )
    visible, calls = flt.feed(text)
    assert visible == "I will read it now.\n"
    assert calls == [{"name": "read_file", "arguments": {"path": "/tmp/a.txt"}}]


def test_tool_block_filter_recovers_native_dsml_call() -> None:
    flt = ide_chat.ToolBlockFilter()
    text = (
        "<｜｜DSML｜｜ calls>\n"
        '<｜｜DSML｜｜ invoke name="read_big_file">\n'
        '<｜｜DSML｜｜ parameter name="arguments" string="false">{}</｜｜DSML｜｜ parameter>\n'
        "</｜｜DSML｜｜ invoke>\n"
        "</｜｜DSML｜｜ calls>"
    )
    visible, calls = flt.feed(text)
    assert visible == ""
    assert calls == [{"name": "read_big_file", "arguments": {}}]


def test_tool_block_filter_recovers_native_tool_call_json() -> None:
    flt = ide_chat.ToolBlockFilter()
    text = (
        "checking\n"
        '<tool_call>{"name":"get_weather","arguments":{"city":"Beijing"}}</tool_call>'
    )
    visible, calls = flt.feed(text)
    assert visible == "checking\n"
    assert calls == [{"name": "get_weather", "arguments": {"city": "Beijing"}}]


def test_tool_block_filter_can_disable_tool_parsing() -> None:
    flt = ide_chat.ToolBlockFilter(allow_tool_calls=False)
    text = '<trae_mouth_tool_call>{"name":"x","arguments":{}}</trae_mouth_tool_call>'
    visible, calls = flt.feed(text)
    assert visible == text
    assert calls == []


def test_tool_result_failed_detects_json_error() -> None:
    message = {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "run",
        "content": '{"status":"failed","message":"boom"}',
    }
    converted = ide_chat.convert_messages([message])
    text = converted[0]["content"][0]["text"]
    assert "status=failed" in text


def test_convert_messages_preserves_assistant_reasoning() -> None:
    converted = ide_chat.convert_messages(
        [
            {
                "role": "assistant",
                "content": "done",
                "reasoning_content": "the reasoning chain",
            }
        ]
    )
    assert converted[0]["reasoning_content"] == "the reasoning chain"
    assert converted[0]["reasoning"] == "the reasoning chain"


def test_normalize_tool_arguments_repairs_defaults_and_types() -> None:
    schema = {
        "type": "object",
        "properties": {
            "x": {"type": "integer"},
            "mode": {"type": "string", "enum": ["fast", "safe"]},
            "count": {"type": "integer", "default": 1},
        },
        "required": ["x"],
    }
    normalized = ide_chat.normalize_tool_arguments(
        "tool", {"x": "7", "mode": "FAST"}, schema
    )
    assert normalized == {"x": 7, "mode": "fast", "count": 1}
    assert ide_chat.normalize_tool_arguments("tool", {"x": True}, schema)["x"] == 1
    with pytest.raises(ValueError):
        ide_chat.normalize_tool_arguments("tool", {}, schema)


def test_convert_messages_truncates_large_tool_result() -> None:
    content = "A" * 1000 + "NEEDLE" + "B" * 1000
    converted = ide_chat.convert_messages(
        [{"role": "tool", "tool_call_id": "call_1", "name": "read", "content": content}],
        max_tool_result_chars=500,
    )
    text = converted[0]["content"][0]["text"]
    assert "tool result truncated" in text
    assert len(text) < 900


def test_compact_history_keeps_system_and_recent() -> None:
    system = {"role": "system", "content": "system rules"}
    old_user = {"role": "user", "content": "old " * 100}
    old_assistant = {"role": "assistant", "content": "old answer " * 100}
    recent = {"role": "user", "content": "recent"}
    compacted = ide_chat._compact_history(
        [system, old_user, old_assistant, recent], max_chars=40, max_messages=0
    )
    assert compacted[0] is system
    assert compacted[-1] is recent
    assert old_user not in compacted
    assert old_assistant not in compacted


def test_compact_history_keeps_matching_tool_call() -> None:
    system = {"role": "system", "content": "system rules"}
    call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read", "arguments": "{}"},
            }
        ],
    }
    result = {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "read",
        "content": "result",
    }
    compacted = ide_chat._compact_history(
        [system, call, result], max_chars=1000, max_messages=1
    )
    assert [message["role"] for message in compacted] == [
        "system",
        "assistant",
        "tool",
    ]


def test_completed_tool_call_reminder_skips_failed_results() -> None:
    call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read", "arguments": '{"path":"a"}'},
            }
        ],
    }
    success = {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "read",
        "content": "ok",
    }
    failed = {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "read",
        "content": "Error: no such file",
    }
    reminder = ide_chat._completed_tool_call_reminder([call, success])
    assert "Completed client tool calls" in reminder
    assert '"name":"read"' in reminder
    assert ide_chat._completed_tool_call_reminder([call, failed]) == ""


def test_large_tool_catalog_compacts_prompt(monkeypatch) -> None:
    monkeypatch.setenv("TRAE_MOUTH_MAX_TOOL_SCHEMA_CHARS", "2048")
    tools = [
        {
            "type": "function",
            "function": {
                "name": f"tool_{index}",
                "description": "A very verbose tool description " * 20,
                "parameters": {
                    "type": "object",
                    "properties": {
                        f"field_{index}": {
                            "type": "string",
                            "description": "long field description " * 10,
                        }
                    },
                    "required": [f"field_{index}"],
                },
            },
        }
        for index in range(20)
    ]
    prompt = ide_chat.build_tool_prompt(tools)
    assert "compact input signatures" in prompt
    assert '"available_tools"' in prompt
    assert '"parameters"' in prompt
    assert '"required"' in prompt
    assert "<trae_mouth_tool_call>" in prompt


def test_tool_block_filter_extracts_and_holds_partial() -> None:
    flt = ide_chat.ToolBlockFilter()
    visible, calls = flt.feed("before<trae_mouth")
    assert visible == "before"
    assert calls == []
    full = (
        "before<trae_mouth_tool_call>"
        '{"name":"get_weather","arguments":{"city":"Beijing"}}'
        "</trae_mouth_tool_call>after"
    )
    visible, calls = flt.feed(full)
    assert visible == "beforeafter"
    assert calls == [{"name": "get_weather", "arguments": {"city": "Beijing"}}]
    visible, calls = flt.feed(full)
    assert visible == "beforeafter"
    assert calls == []


def test_convert_messages_builds_tool_prompt_and_history() -> None:
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Weather?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city":"Beijing"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "get_weather",
            "content": "sunny",
        },
    ]
    converted = ide_chat.convert_messages(
        messages,
        tools=[
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
    )
    assert converted[0]["role"] == "system"
    system_text = converted[0]["content"][0]["text"]
    assert "You are helpful." in system_text
    assert "<trae_mouth_tool_call>" in system_text
    assert not any(
        "[client tool call]" in (item.get("content") or [{}])[0].get("text", "")
        for item in converted
        if item.get("role") == "assistant" and item.get("content")
    )
    tool_result = next(
        item
        for item in converted
        if item["role"] == "user" and "Client tool result" in item["content"][0]["text"]
    )
    assert "status=succeeded" in tool_result["content"][0]["text"]
    assert 'arguments={"city":"Beijing"}' in tool_result["content"][0]["text"]
    assert "sunny" in tool_result["content"][0]["text"]


def test_resolve_model_prefers_max_variant() -> None:
    catalog = {
        "groups": {
            "ide": {
                "models": [
                    {
                        "config_name": "glm-5.4",
                        "display_name": "GLM-5.4",
                        "provider_models": ["glm-5.4__dev", "glm-5.4__max"],
                        "functions": ["chat_v3"],
                        "config_source": 1,
                    }
                ]
            }
        }
    }
    normal = ide_chat.resolve_model(catalog, "glm-5.4")
    assert normal and normal["model_name"] == "glm-5.4__dev"
    assert normal["max_mode_applied"] is False
    maxed = ide_chat.resolve_model(catalog, "glm-5.4", prefer_max=True)
    assert maxed and maxed["model_name"] == "glm-5.4__max"
    assert maxed["max_mode_applied"] is True
    suffix = ide_chat.resolve_model(catalog, "glm-5.4:1m")
    assert suffix and suffix["model_name"] == "glm-5.4__max"
    assert ide_chat.resolve_model(catalog, "missing") is None


def test_resolve_model_exact_config_beats_display_alias() -> None:
    catalog = {
        "groups": {
            "ide": {
                "models": [
                    {
                        "config_name": "glm-5.2_advisor_doubao",
                        "display_name": "GLM-5.2",
                        "provider_models": ["glm-5.2__dev"],
                        "functions": ["chat_v3"],
                    },
                    {
                        "config_name": "glm-5.2",
                        "display_name": "GLM-5.2",
                        "provider_models": ["glm-5.2__dev"],
                        "functions": ["chat_v3"],
                    },
                ]
            }
        }
    }
    resolved = ide_chat.resolve_model(catalog, "glm-5.2")
    assert resolved["config_name"] == "glm-5.2"


def test_resolve_model_max_can_use_solo_when_ide_has_no_max() -> None:
    catalog = {
        "groups": {
            "ide": {
                "models": [
                    {
                        "config_name": "glm-5.4",
                        "display_name": "GLM-5.4",
                        "provider_models": ["glm-5.4__dev"],
                        "functions": ["chat_v3"],
                    }
                ]
            },
            "solo": {
                "models": [
                    {
                        "config_name": "glm-5.4",
                        "display_name": "GLM-5.4",
                        "provider_models": ["glm-5.4__dev", "glm-5.4__max"],
                        "functions": ["solo_agent"],
                    }
                ]
            },
        }
    }
    resolved = ide_chat.resolve_model(catalog, "glm-5.4", prefer_max=True)
    assert resolved["endpoint"] == "solo"
    assert resolved["model_name"] == "glm-5.4__max"
    assert resolved["raw_chat_function"] == "solo_agent"


def test_build_body_uses_prompt_tools_not_native_tools() -> None:
    resolved = {
        "config_name": "glm-5.4",
        "model_name": "glm-5.4__max",
        "raw_chat_function": "chat_v3",
        "config_source": 1,
    }
    body = ide_chat.build_body(
        resolved,
        [{"role": "user", "content": "hello"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "ping",
                    "description": "Ping",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        session_id="sid",
    )
    assert body["config_name"] == "glm-5.4"
    assert body["model_name"] == "glm-5.4__max"
    assert body["raw_chat_function"] == "chat_v3"
    assert body["stream"] is True
    assert "tools" not in body
    assert "<trae_mouth_tool_call>" in body["messages"][0]["content"][0]["text"]


def test_map_usage() -> None:
    usage = ide_chat.map_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "reasoning_tokens": 5,
            "cache_read_input_tokens": 3,
        }
    )
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 20
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 5
    assert usage["prompt_tokens_details"]["cached_tokens"] == 3


def test_stream_raw_converts_read_error_to_upstream_error() -> None:
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: output\ndata: {"response":"x"}\n\n'
            raise httpx.ReadError("connection dropped")

    class BrokenTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=BrokenStream(),
            )

    config = SimpleNamespace(
        model_gateway="https://example.invalid",
        ide_version="3.4.22",
        ide_version_code="20261118",
    )
    resolved = {
        "config_name": "test-model",
        "model_name": "test-model__max",
        "raw_chat_function": "chat_v3",
        "config_source": 1,
    }

    async def collect() -> list[dict]:
        events = []
        async for event in ide_chat.stream_raw(
            config,
            "token",
            "user-1",
            build_device(),
            resolved,
            {},
            transport=BrokenTransport(),
        ):
            events.append(event)
        return events

    with pytest.raises(ide_chat.UpstreamError):
        asyncio.run(collect())


def test_stream_raw_falls_back_to_v1() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/v2/llm_raw_chat"):
            return httpx.Response(404, text="not found")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'event: output\ndata: {"response":"OK"}\n\n'
                'event: done\ndata: {"finish_reason":"stop"}\n\n'
            ),
        )

    config = SimpleNamespace(
        model_gateway="https://example.invalid",
        ide_version="3.4.22",
        ide_version_code="20261118",
    )
    resolved = {
        "config_name": "test-model",
        "model_name": "test-model__dev",
        "raw_chat_function": "chat_v3",
        "config_source": 1,
    }

    async def collect() -> list[dict]:
        events = []
        async for event in ide_chat.stream_raw(
            config,
            "token",
            "user-1",
            build_device(),
            resolved,
            {},
            transport=httpx.MockTransport(handler),
        ):
            events.append(event)
        return events

    events = asyncio.run(collect())
    assert paths == [
        "/api/ide/v2/llm_raw_chat",
        "/api/ide/v1/llm_raw_chat",
    ]
    assert events[0]["data"]["response"] == "OK"
    assert events[-1]["event"] == "done"
