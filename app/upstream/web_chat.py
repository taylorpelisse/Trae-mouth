from __future__ import annotations

import json
from typing import Any, AsyncIterator, Mapping, Optional

import httpx

from ..config import Config
from .catalog import web_headers
from .ide_chat import TextAccumulator, UpstreamError

WEB_CHANNELS = {"remote", "work", "design"}


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, Mapping):
                continue
            if str(block.get("type") or "").lower() in {
                "reasoning",
                "thinking",
                "reasoning_text",
            }:
                continue
            text = block.get("text") or block.get("content")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    if isinstance(value, Mapping):
        text = value.get("text") or value.get("content")
        return text if isinstance(text, str) else ""
    return ""


def _message_text(value: Any, role: str) -> str:
    text = _text(value)
    if role == "system":
        return f"[System]\n{text}" if text else ""
    if role == "assistant":
        return f"[Assistant]\n{text}" if text else ""
    return text


def _flatten_messages(messages: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "user")
        text = _message_text(message.get("content"), role)
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            for call in calls:
                if not isinstance(call, Mapping):
                    continue
                function = (
                    call.get("function")
                    if isinstance(call.get("function"), Mapping)
                    else call
                )
                name = str(function.get("name") or "tool")
                arguments = function.get("arguments") or "{}"
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                call_id = str(call.get("id") or call.get("tool_call_id") or "")
                text += (
                    f"\n[Client Tool Call: {call_id} {name}]"
                    f"\nArguments: {arguments}"
                )
        if role == "tool":
            call_id = str(
                message.get("tool_call_id") or message.get("toolCallId") or ""
            )
            name = str(message.get("name") or "tool")
            text = (
                f"[Client Tool Result: {call_id} {name}]\n{text}"
            )
        if text:
            items.append({"type": "text", "data": {"content": text}})
    return items


def _common_params(user_id: str, mode: str) -> str:
    return json.dumps(
        {
            "language": "zh-cn",
            "app_language": "zh-CN",
            "quality": "stable",
            "app_version": "1.0.0.1229",
            "web_id": "",
            "user_identity": "Free",
            "is_freshman": "0",
            "biz_user_id": user_id,
            "user_unique_id": user_id,
            "scope": "marscode-cn",
            "tenant": "marscode",
            "region": "cn",
            "aiRegion": "cn",
            "is_privacy_mode": 0,
            "privacy_mode": "off",
            "solo_chat_mode": mode,
        },
        ensure_ascii=False,
    )


def build_session_body(
    resolved: Mapping[str, Any],
    messages: list[Mapping[str, Any]],
    *,
    user_id: str,
) -> dict[str, Any]:
    channel = str(resolved.get("channel") or resolved.get("endpoint") or "remote")
    mode = "work" if channel == "work" else "code"
    content = _flatten_messages(messages)
    query = json.dumps(content, ensure_ascii=False)
    entry = resolved.get("entry")
    raw = entry.get("raw") if isinstance(entry, Mapping) else None
    custom_model = raw if isinstance(raw, Mapping) and raw else None
    model_name = str(resolved.get("config_name") or resolved.get("model_name") or "")
    initial: dict[str, Any] = {
        "chat_session_id": "",
        "content": content,
        "query": query,
        "model_name": model_name,
        "agent_type": str(resolved.get("agent_type") or ""),
        "model_selection_strategy": "manual",
        "common_params": _common_params(user_id, mode),
    }
    if custom_model is not None:
        initial["custom_model"] = custom_model
    return {
        "mode": mode,
        "environment_id": "default",
        "initial_message": initial,
        "env": "remote",
        "auto_create_project": False,
        "origin": "web",
    }


def _normalize_event(name: str, data: Mapping[str, Any]) -> str:
    value = str(data.get("event") or data.get("type") or name or "").strip().lower()
    return value.replace("-", "_")


def _plan_snapshots(data: Mapping[str, Any]) -> tuple[str, str]:
    content = _text(data.get("content"))
    reasoning = str(
        data.get("reasoning_content")
        or data.get("reasoningContent")
        or data.get("reasoning")
        or ""
    )
    thought = str(data.get("thought") or "")
    if content:
        return reasoning or thought, content
    if reasoning:
        return reasoning, thought
    if thought:
        return "", thought
    return "", ""


class _State:
    def __init__(self) -> None:
        self.reasoning = TextAccumulator()


def _emit(
    reasoning_state: _State,
    answer_state: TextAccumulator,
    reasoning: str,
    answer: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    delta = reasoning_state.reasoning.add(reasoning)
    if delta:
        events.append(
            {
                "event": "output",
                "data": {"response": delta, "phase": "thinking"},
            }
        )
    delta = answer_state.add(answer)
    if delta:
        events.append({"event": "output", "data": {"response": delta}})
    return events


async def stream_chat(
    config: Config,
    token: str,
    user_id: str,
    resolved: Mapping[str, Any],
    messages: list[Mapping[str, Any]],
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> AsyncIterator[dict[str, Any]]:
    headers = web_headers(config, token, user_id)
    timeout = httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0)
    client = httpx.AsyncClient(timeout=timeout, transport=transport)
    try:
        body = build_session_body(resolved, messages, user_id=user_id)
        response = await client.post(
            f"{config.web_api_base}/chat_sessions",
            headers=headers,
            json=body,
        )
        if response.status_code >= 400:
            detail = response.text[:500]
            raise UpstreamError(
                f"web session HTTP {response.status_code}: {detail}",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError("web session returned invalid JSON", 502) from exc
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            data = payload if isinstance(payload, Mapping) else {}
        session_id = str(data.get("chat_session_id") or "")
        message_id = str(data.get("message_id") or "")
        if not session_id or not message_id:
            raise UpstreamError("web session missing ids", status_code=502)
        url = (
            f"{config.web_api_base}/chat_sessions/{session_id}/events"
            f"?reply_to_message_id={message_id}"
        )
        async with client.stream("GET", url, headers=headers) as stream:
            if stream.status_code >= 400:
                detail = (await stream.aread()).decode("utf-8", "replace")[:500]
                raise UpstreamError(
                    f"web events HTTP {stream.status_code}: {detail}",
                    status_code=stream.status_code,
                )
            event_name = ""
            states: dict[str, _State] = {}
            answer_state = TextAccumulator()
            message_state = _State()
            saw_done = False
            async for raw_line in stream.aiter_lines():
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                raw_payload = line[5:].strip()
                if raw_payload == "[DONE]":
                    event_name = ""
                    saw_done = True
                    yield {"event": "done", "data": {}}
                    break
                try:
                    event_data = json.loads(raw_payload)
                except ValueError:
                    event_name = ""
                    continue
                if not isinstance(event_data, Mapping):
                    event_name = ""
                    continue
                name = _normalize_event(event_name, event_data)
                event_name = ""
                if name in {"heartbeat", "keepalive", "ping", "status_changed"}:
                    continue
                if name in {"platform_timing", "timing_events", "model_config"}:
                    continue
                if name == "error":
                    yield {"event": "error", "data": dict(event_data)}
                    continue
                if name == "token_usage":
                    yield {"event": "token_usage", "data": dict(event_data)}
                    continue
                if name == "done":
                    saw_done = True
                    done = dict(event_data)
                    done.setdefault("finish_reason", "stop")
                    yield {"event": "done", "data": done}
                    continue
                tool_info = event_data.get("tool_call_info")
                if isinstance(tool_info, Mapping):
                    tool_name = str(tool_info.get("name") or "")
                    params = tool_info.get("params")
                    if tool_name == "finish" and isinstance(params, Mapping):
                        summary = str(params.get("summary") or "")
                        if summary:
                            for item in _emit(
                                message_state, answer_state, "", summary
                            ):
                                yield item
                if name == "plan_item":
                    plan_id = str(event_data.get("id") or "plan-0")
                    state = states.setdefault(plan_id, _State())
                    reasoning, answer = _plan_snapshots(event_data)
                    for item in _emit(state, answer_state, reasoning, answer):
                        yield item
                    continue
                if name in {
                    "message",
                    "assistant_message",
                    "agent_message",
                    "response",
                    "text",
                    "output",
                }:
                    reasoning, answer = _plan_snapshots(event_data)
                    for item in _emit(
                        message_state, answer_state, reasoning, answer
                    ):
                        yield item
                    continue
            if not saw_done:
                yield {"event": "done", "data": {"finish_reason": "stop"}}
    except httpx.HTTPError as exc:
        raise UpstreamError(
            f"web transport interrupted: {exc}", status_code=502
        ) from exc
    finally:
        await client.aclose()
