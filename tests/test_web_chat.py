from __future__ import annotations

import asyncio
import json

import httpx

from app.upstream import ide_chat, web_chat


def web_catalog() -> dict:
    return {
        "groups": {
            "ide": {
                "models": [
                    {
                        "config_name": "ide-model",
                        "display_name": "IDE Model",
                        "functions": ["chat_v3"],
                        "provider_models": ["ide-model__dev"],
                    }
                ]
            },
            "work": {
                "models": [
                    {
                        "config_name": "Work Model",
                        "display_name": "Work Model",
                        "functions": ["solo_work_remote"],
                        "provider_models": [],
                        "raw": {"name": "Work Model", "display_name": "Work Model"},
                    }
                ]
            },
            "design": {
                "models": [
                    {
                        "config_name": "Design Model",
                        "display_name": "Design Model",
                        "functions": ["solo_design_remote"],
                        "provider_models": [],
                        "raw": {"name": "Design Model"},
                    }
                ]
            },
        }
    }


def test_resolve_web_channels() -> None:
    catalog = web_catalog()
    assert ide_chat.parse_model_channel("work/Work Model") == (
        "work",
        "Work Model",
    )
    assert ide_chat.parse_model_channel("plain") == ("", "plain")
    assert ide_chat.parse_model_channel("glm-5.2:1m") == ("", "glm-5.2:1m")

    default = ide_chat.resolve_model(catalog, "ide-model")
    assert default and default["channel"] == "ide" and not default["web"]

    work = ide_chat.resolve_model(catalog, "work/Work Model")
    assert work and work["web"]
    assert work["agent_type"] == "solo_work_remote"
    assert work["raw_chat_function"] == "solo_work_remote"

    design = ide_chat.resolve_model(catalog, "design/Design Model")
    assert design and design["channel"] == "design"

    fallback = ide_chat.resolve_model(catalog, "Work Model")
    assert fallback and fallback["channel"] == "work"


def test_session_body_and_flatten() -> None:
    resolved = ide_chat.resolve_model(web_catalog(), "work/Work Model")
    assert resolved
    body = web_chat.build_session_body(
        resolved,
        [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "calling",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {"name": "lookup", "arguments": {"q": "x"}},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "lookup", "content": "ok"},
        ],
        user_id="user-1",
    )
    assert body["mode"] == "work"
    assert body["initial_message"]["agent_type"] == "solo_work_remote"
    assert body["initial_message"]["model_name"] == "Work Model"
    assert body["initial_message"]["custom_model"]["name"] == "Work Model"
    assert body["initial_message"]["model_selection_strategy"] == "manual"
    text = body["initial_message"]["query"]
    assert "[System]" in text
    assert "[Client Tool Call: c1 lookup]" in text
    assert "[Client Tool Result: c1 lookup]" in text


def test_stream_normalizes_web_events(config) -> None:
    events = [
        ("plan_item", {"id": "p1", "reasoning_content": "The"}),
        ("plan_item", {"id": "p1", "reasoning_content": "The answer"}),
        (
            "plan_item",
            {"id": "p1", "thought": "OK", "reasoning_content": "The answer"},
        ),
        (
            "token_usage",
            {"prompt_tokens": 3, "completion_tokens": 2},
        ),
        ("done", {"finish_reason": "stop"}),
    ]
    payload = "".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat_sessions"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"chat_session_id": "s1", "message_id": "m1"},
                },
            )
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=payload,
            )
        return httpx.Response(404)

    resolved = ide_chat.resolve_model(web_catalog(), "work/Work Model")
    assert resolved

    async def collect():
        return [
            event
            async for event in web_chat.stream_chat(
                config,
                "token",
                "user-1",
                resolved,
                [{"role": "user", "content": "hi"}],
                transport=httpx.MockTransport(handler),
            )
        ]

    result = asyncio.run(collect())
    outputs = [
        event["data"]
        for event in result
        if event.get("event") == "output"
    ]
    assert any(item.get("phase") == "thinking" for item in outputs)
    assert "".join(
        str(item.get("response") or "")
        for item in outputs
        if item.get("phase") != "thinking"
    ) == "OK"
    assert any(event.get("event") == "token_usage" for event in result)
    assert result[-1]["event"] == "done"
