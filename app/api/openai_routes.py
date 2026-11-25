from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from ..accounts.router import AccountRouter
from ..upstream import ide_chat, web_chat
from ..upstream.ide_chat import (
    TextAccumulator,
    ToolBlockFilter,
    UpstreamError,
    map_usage,
)

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


@dataclass(frozen=True)
class ApiAuth:
    key_id: str = ""
    name: str = ""
    scopes: tuple[str, ...] = ("chat", "models")
    static: bool = True

    def allows(self, scope: str) -> bool:
        return self.static or scope in self.scopes


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str
    messages: list[dict[str, Any]]
    stream: bool = False
    tools: Optional[list[Any]] = None
    tool_choice: Any = None
    parallel_tool_calls: Optional[bool] = None
    reasoning_effort: Optional[str] = None
    trae_max_mode: Optional[bool] = None
    max_mode: Optional[bool] = None


def _sse(payload: Mapping[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _tool_call_id(index: int, name: str, arguments: Any, namespace: str = "") -> str:
    raw = (
        f"{namespace}:{index}:{name}:"
        + json.dumps(arguments, sort_keys=True, ensure_ascii=False)
    )
    return "call_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _normalize_finish_reason(reason: str) -> str:
    reason = str(reason or "").strip().lower()
    if reason in {"max_tokens", "max_output_tokens", "model_output_truncated"}:
        return "length"
    if reason in {"stop", "length", "content_filter", "tool_calls"}:
        return reason
    return "stop"


_REASONING_RANKS = {
    "minimal": 0,
    "low": 0,
    "light": 0,
    "medium": 1,
    "high": 2,
    "xhigh": 3,
    "extra_high": 3,
    "max": 4,
    "ultra": 4,
}


def _nearest_reasoning_level(
    requested: str, levels: list[str], default: str = ""
) -> str:
    """Map a client effort onto the model's supported levels.

    Workbuddy-style auto-downgrade is preferable to a hard 400 when a client
    asks for ``light`` on a high-only model, or ``extra_high`` on a model that
    tops out at ``high``.
    """

    requested = str(requested or "").strip()
    levels = [str(level) for level in levels if str(level).strip()]
    if not levels:
        return requested
    if requested in levels:
        return requested
    if not requested:
        return default if default in levels else levels[0]
    target = _REASONING_RANKS.get(requested.lower(), 1)
    ranked = [
        (_REASONING_RANKS.get(level.lower(), 1), index, level)
        for index, level in enumerate(levels)
    ]
    lower = [item for item in ranked if item[0] <= target]
    if lower:
        return max(lower, key=lambda item: (item[0], item[1]))[2]
    return min(ranked, key=lambda item: (item[0], item[1]))[2]


def _should_failover_status(status_code: int) -> bool:
    return status_code in {0, 401, 403, 429} or status_code >= 500


def _as_uuid(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, "trae-mouth-explicit:" + text))


def _message_fingerprint_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif part.get("type") in {"image_url", "image"}:
                    parts.append(json.dumps(part, sort_keys=True, ensure_ascii=False))
        return "".join(parts)
    if isinstance(content, Mapping):
        return json.dumps(content, sort_keys=True, ensure_ascii=False)
    return "" if content is None else str(content)


def _conversation_key(payload: ChatRequest, request: Request) -> tuple[str, bool]:
    """Return a stable per-conversation key and whether the client supplied it.

    OpenAI Chat Completions has no standard conversation id, so clients that
    resend the full history are identified by their first user message.  This
    keeps the upstream conversation/session id stable across tool-result turns,
    which is required for the model to consume the previous tool call instead
    of issuing it again.
    """

    extras = getattr(payload, "model_extra", None) or {}
    metadata = extras.get("metadata")
    header_values = (
        request.headers.get("x-conversation-id"),
        request.headers.get("x-session-id"),
        request.headers.get("x-trae-session-id"),
        request.headers.get("x-prompt-cache-key"),
    )
    body_values = (
        extras.get("conversation_id"),
        extras.get("conversationId"),
        extras.get("session_id"),
        extras.get("sessionId"),
        extras.get("prompt_cache_key"),
        extras.get("promptCacheKey"),
    )
    metadata_values = ()
    if isinstance(metadata, Mapping):
        metadata_values = (
            metadata.get("conversation_id"),
            metadata.get("session_id"),
            metadata.get("prompt_cache_key"),
        )
    for value in header_values + body_values + metadata_values:
        if isinstance(value, str) and value.strip():
            explicit = _as_uuid(value)
            if explicit:
                return explicit, True

    first_user = ""
    system_text = ""
    for message in payload.messages:
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "").lower()
        text = _message_fingerprint_text(message)
        if role == "system" and not system_text:
            system_text = text
        elif role == "user" and not first_user:
            first_user = text
    user_key = str(extras.get("user") or extras.get("safety_identifier") or "")
    model_key = str(payload.model or "").strip().lower()
    for suffix in (":1m", "-1m", "__max"):
        if model_key.endswith(suffix):
            model_key = model_key[: -len(suffix)]
            break
    material = "\x1f".join(
        (
            "trae-mouth-conversation-v1",
            model_key,
            user_key,
            first_user,
            "" if first_user else system_text,
        )
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, material)), False


def _requested_account_id(payload: ChatRequest, request: Request) -> str:
    extras = getattr(payload, "model_extra", None) or {}
    for value in (
        request.headers.get("x-account-id"),
        request.headers.get("x-trae-account-id"),
        extras.get("account_id"),
        extras.get("accountId"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class OpenAIStreamTranslator:
    def __init__(
        self,
        model: str,
        *,
        tools_expected: bool = False,
        reasoning_as_content: bool = True,
        tool_schemas: Optional[Mapping[str, Mapping[str, Any]]] = None,
        strict_tool_args: bool = True,
        tools_required: bool = False,
    ) -> None:
        self.model = model
        self.id = "chatcmpl-" + uuid.uuid4().hex
        self.created = int(time.time())
        self.reasoning = TextAccumulator()
        self.response = TextAccumulator()
        self.filter = ToolBlockFilter(allow_tool_calls=tools_expected)
        self.visible = ""
        self.calls: list[dict[str, Any]] = []
        self._call_keys: set[str] = set()
        self._emitted_call_indexes: set[int] = set()
        self.usage: Optional[dict[str, Any]] = None
        self.finish_reason = "stop"
        self.started = False
        self.emitted_content = False
        self.emitted_reasoning = False
        self.saw_done = False
        self.queue_continue = False
        self.content_filtered = False
        self.tools_expected = tools_expected
        self.reasoning_as_content = reasoning_as_content
        self.tool_schemas = dict(tool_schemas or {})
        self.strict_tool_args = strict_tool_args
        self.tools_required = tools_required

    def _chunk(
        self,
        delta: Mapping[str, Any],
        *,
        finish_reason: Optional[str] = None,
        usage: Optional[dict[str, Any]] = None,
    ) -> str:
        payload: dict[str, Any] = {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "delta": dict(delta),
                    "finish_reason": finish_reason,
                }
            ],
        }
        if usage is not None:
            payload["usage"] = usage
        return _sse(payload)

    def _start(self, chunks: list[str]) -> None:
        if not self.started:
            self.started = True
            chunks.append(self._chunk({"role": "assistant"}))

    def _add_call(
        self,
        name: str,
        arguments: Any,
        call_id: str = "",
    ) -> Optional[dict[str, Any]]:
        schema = self.tool_schemas.get(name)
        if schema is not None:
            try:
                arguments = ide_chat.normalize_tool_arguments(
                    name,
                    arguments,
                    schema,
                    strict=self.strict_tool_args,
                )
            except ValueError as exc:
                raise UpstreamError(str(exc), status_code=502) from exc
        elif not isinstance(arguments, Mapping):
            arguments = {"value": arguments}
        key = json.dumps(
            {"name": name, "arguments": arguments}, sort_keys=True, ensure_ascii=False
        )
        if key in self._call_keys:
            return None
        self._call_keys.add(key)
        call = {
            "id": call_id or _tool_call_id(len(self.calls), name, arguments, self.id),
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(
                    arguments, ensure_ascii=False, separators=(",", ":")
                ),
            },
        }
        self.calls.append(call)
        return call

    def _native_calls(self, data: Mapping[str, Any]) -> None:
        raw = data.get("tool_calls")
        if not isinstance(raw, list):
            raw = data.get("tool_call_info")
            if isinstance(raw, Mapping):
                raw = [raw]
        if not isinstance(raw, list):
            return
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            function = item.get("function") if isinstance(item.get("function"), Mapping) else item
            name = str(
                function.get("name") or item.get("name") or item.get("tool_call_name") or ""
            ).strip()
            if not name:
                continue
            arguments = function.get(
                "arguments", item.get("arguments", item.get("input", {}))
            )
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    pass
            call_id = str(
                item.get("id") or item.get("tool_call_id") or item.get("toolCallId") or ""
            )
            self._add_call(name, arguments, call_id)

    def _emit_visible(self, chunks: list[str]) -> None:
        new_calls: list[dict[str, Any]]
        visible, new_calls = self.filter.feed(self.response.text)
        if visible.startswith(self.visible):
            delta = visible[len(self.visible) :]
            self.visible = visible
        elif self.visible.startswith(visible):
            delta = ""
        else:
            common = 0
            limit = min(len(self.visible), len(visible))
            while common < limit and self.visible[common] == visible[common]:
                common += 1
            delta = visible[common:]
            self.visible = visible
        if delta:
            self._start(chunks)
            self.emitted_content = True
            chunks.append(self._chunk({"content": delta}))
        for call in new_calls:
            added = self._add_call(
                str(call.get("name") or ""), call.get("arguments") or {}
            )
            if added is None:
                continue
            index = len(self.calls) - 1
            if index in self._emitted_call_indexes:
                continue
            self._start(chunks)
            self._emitted_call_indexes.add(index)
            chunks.append(
                self._chunk(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": added["id"],
                                "type": "function",
                                "function": added["function"],
                            }
                        ]
                    }
                )
            )

    def feed(self, event: Mapping[str, Any]) -> list[str]:
        chunks: list[str] = []
        name = str(event.get("event") or "")
        data = event.get("data")
        if not isinstance(data, Mapping):
            return chunks
        if name == "error":
            message = str(data.get("message") or data.get("error") or "upstream error")
            code = data.get("code")
            if code not in (None, "", 0):
                message = f"{message} (code {code})"
            raise UpstreamError(message)
        if name == "token_usage":
            self.usage = map_usage(data.get("usage") or data)
            return chunks
        if name == "request_wait_in_queue":
            return chunks
        if name == "queue_continue":
            # The upstream asked the client to retry the same queued request.
            # Do not silently finish with an empty assistant turn.
            self.queue_continue = True
            return chunks
        if name == "content_security":
            self.content_filtered = True
            return chunks
        if name == "done" or data.get("event") == "done":
            self.saw_done = True
            reason = str(data.get("finish_reason") or "")
            if reason:
                self.finish_reason = _normalize_finish_reason(reason)
            return chunks
        if name in {"workflow_finish", "turn_completion"}:
            # Agent/Solo streams can terminate with these events instead of
            # the chat_v3 ``done`` event.
            self.saw_done = True
            reason = str(data.get("finish_reason") or "")
            if reason:
                self.finish_reason = _normalize_finish_reason(reason)
            return chunks
        if name in {"text_delta"}:
            delta = self.response.add_delta(data.get("delta"))
            if delta:
                self._emit_visible(chunks)
            return chunks
        if name in {"reasoning_delta"}:
            delta = self.reasoning.add_delta(data.get("delta"))
            if delta:
                self._start(chunks)
                self.emitted_reasoning = True
                chunks.append(self._chunk({"reasoning_content": delta}))
            return chunks
        if name == "extra_info":
            self._feed_extra_info(data, chunks)
            return chunks
        if name == "thought":
            fragment = (
                data.get("thought")
                or data.get("reasoning_content")
                or data.get("content")
                or data.get("text")
            )
            if isinstance(fragment, str) and fragment:
                if fragment.startswith(self.reasoning.text):
                    delta = self.reasoning.add(fragment)
                else:
                    delta = self.reasoning.add_delta(fragment)
                if delta:
                    self._start(chunks)
                    self.emitted_reasoning = True
                    chunks.append(self._chunk({"reasoning_content": delta}))
            return chunks
        if name == "output":
            phase = str(data.get("phase") or "").lower()
            fragment = data.get("response")
            if isinstance(fragment, str) and fragment:
                if phase in {"thinking", "reasoning", "thought", "analysis"}:
                    delta = self.reasoning.add_delta(fragment)
                    if delta:
                        self._start(chunks)
                        self.emitted_reasoning = True
                        chunks.append(self._chunk({"reasoning_content": delta}))
                else:
                    self.response.add_delta(fragment)
                    self._emit_visible(chunks)
            reasoning = data.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                delta = self.reasoning.add_delta(reasoning)
                if delta:
                    self._start(chunks)
                    self.emitted_reasoning = True
                    chunks.append(self._chunk({"reasoning_content": delta}))
            self._native_calls(data)
            if data.get("usage"):
                self.usage = map_usage(data.get("usage"))
            return chunks
        if name == "tool_call":
            self._native_calls(data)
            return chunks
        return chunks

    def _feed_extra_info(self, data: Mapping[str, Any], chunks: list[str]) -> None:
        """Use the final reasoning snapshot only when no delta was streamed."""

        if self.reasoning.text:
            return
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            return
        try:
            parsed = json.loads(content)
        except ValueError:
            return
        if not isinstance(parsed, Mapping):
            return
        reasoning = parsed.get("reasoning_content")
        if not isinstance(reasoning, str) or not reasoning:
            return
        self.reasoning.text = reasoning
        self._start(chunks)
        self.emitted_reasoning = True
        chunks.append(self._chunk({"reasoning_content": reasoning}))

    def _finalize_calls(self) -> str:
        visible, new_calls = self.filter.feed(self.response.text)
        incomplete = self.filter.incomplete
        if incomplete:
            if ide_chat.TOOL_START.startswith(incomplete):
                # A bare partial marker is only worth failing on when the model
                # clearly started writing the call marker.  A trailing "<" is
                # ordinary text, not an interrupted tool call.
                if len(incomplete) >= 8:
                    raise UpstreamError(
                        "tool call output was interrupted before its payload"
                    )
            else:
                salvaged = ide_chat._parse_tool_block(incomplete)
                if salvaged:
                    new_calls.extend(salvaged)
                else:
                    raise UpstreamError(
                        "tool call output was truncated before completion"
                    )
        if self.filter.native_incomplete:
            native = self.filter.native_incomplete
            salvaged = ide_chat._parse_dsml_calls(native)
            if not salvaged:
                salvaged = ide_chat._parse_json_tool_calls(native)
            if salvaged:
                new_calls.extend(salvaged)
            else:
                raise UpstreamError(
                    "native tool call output was truncated before completion"
                )
        delta = ""
        if visible.startswith(self.visible):
            delta = visible[len(self.visible) :]
        elif not self.visible.startswith(visible):
            common = 0
            limit = min(len(self.visible), len(visible))
            while common < limit and self.visible[common] == visible[common]:
                common += 1
            delta = visible[common:]
        self.visible = visible
        for call in new_calls:
            self._add_call(str(call.get("name") or ""), call.get("arguments") or {})
        return delta

    def finalize(self) -> list[str]:
        chunks: list[str] = []
        delta = self._finalize_calls()
        if delta:
            self._start(chunks)
            self.emitted_content = True
            chunks.append(self._chunk({"content": delta}))
        if not self.started:
            self._start(chunks)
        for index, call in enumerate(self.calls):
            if index in self._emitted_call_indexes:
                continue
            self._emitted_call_indexes.add(index)
            chunks.append(
                self._chunk(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": call["id"],
                                "type": "function",
                                "function": call["function"],
                            }
                        ]
                    }
                )
            )
        if not self.saw_done:
            if self.content_filtered:
                pass
            elif self.queue_continue:
                raise UpstreamError(
                    "upstream requested a queued retry before completing the turn",
                    status_code=503,
                )
            else:
                raise UpstreamError(
                    "upstream stream ended before the done event", status_code=502
                )
        if self.tools_required and not self.calls:
            raise UpstreamError("model did not call a required tool")
        if (
            self.reasoning_as_content
            and not self.calls
            and not self.emitted_content
            and not self.content_filtered
            and bool(self.reasoning.text.strip())
        ):
            # Some reasoning-only models put the entire useful answer in the
            # reasoning channel.  Clients that only render ``content`` would
            # otherwise show an empty reply.  Mirror the final reasoning as
            # content only when there is no answer text and no tool call.
            self._start(chunks)
            self.visible = self.reasoning.text
            self.emitted_content = True
            chunks.append(self._chunk({"content": self.reasoning.text}))
        if (
            self.tools_expected
            and not self.calls
            and not self.emitted_content
            and not self.emitted_reasoning
            and not self.content_filtered
        ):
            raise UpstreamError("model returned neither text nor a tool call")
        finish = (
            "tool_calls"
            if self.calls
            else ("content_filter" if self.content_filtered else self.finish_reason or "stop")
        )
        if finish not in {"stop", "length", "content_filter", "tool_calls"}:
            finish = "stop"
        if not self.emitted_content and not self.calls and not self.emitted_reasoning:
            chunks.append(self._chunk({"content": ""}))
        chunks.append(
            self._chunk({}, finish_reason=finish, usage=self.usage)
        )
        chunks.append("data: [DONE]\n\n")
        return chunks

    def message(self) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": self.visible if self.visible else None,
        }
        if self.reasoning.text.strip():
            message["reasoning_content"] = self.reasoning.text
        if self.calls:
            message["tool_calls"] = self.calls
        return message

    def result(self) -> dict[str, Any]:
        finish = (
            "tool_calls"
            if self.calls
            else ("content_filter" if self.content_filtered else self.finish_reason or "stop")
        )
        if finish not in {"stop", "length", "content_filter", "tool_calls"}:
            finish = "stop"
        return {
            "id": self.id,
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": self.message(),
                    "finish_reason": finish,
                }
            ],
            "usage": self.usage,
        }


def create_openai_router(
    *,
    config: Any,
    accounts: Any,
    api_keys: Any,
    refresher: Any,
    transport: Any,
    ensure_device: Callable[[Any, str, Any], Any],
    model_catalog_for: Callable[[str, bool], dict[str, Any]],
    account_router: Optional[AccountRouter] = None,
) -> APIRouter:
    router = APIRouter()
    if account_router is None:
        account_router = AccountRouter(
            accounts,
            affinity_ttl=config.conversation_affinity_ttl,
            cooldown=config.account_cooldown,
        )

    def request_ip(request: Request) -> str:
        host = request.client.host if request.client else ""
        if config.trust_proxy:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                host = forwarded.split(",")[0].strip()
        return host

    def authorize(request: Request) -> ApiAuth:
        supplied = ""
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            supplied = header[7:].strip()
        if not supplied:
            supplied = request.headers.get("x-api-key", "").strip()
        keys = [item.strip() for item in config.api_key.split(",") if item.strip()]
        if supplied and any(hmac.compare_digest(supplied, key) for key in keys):
            return ApiAuth(name="static", static=True)
        if supplied:
            record, error = api_keys.authorize(supplied, request_ip(request))
            if record is not None:
                return ApiAuth(
                    key_id=record.id,
                    name=record.name,
                    scopes=tuple(
                        item.strip()
                        for item in record.scopes.split(",")
                        if item.strip()
                    ),
                    static=False,
                )
            if "quota" in error:
                raise HTTPException(status_code=429, detail=error)
            if "ip not allowed" in error:
                raise HTTPException(status_code=403, detail=error)
            raise HTTPException(status_code=401, detail=error or "invalid api key")
        if config.api_key:
            raise HTTPException(status_code=401, detail="invalid api key")
        host = request_ip(request)
        if host in LOOPBACK_HOSTS or host.startswith("127."):
            return ApiAuth(name="local", static=True)
        raise HTTPException(
            status_code=403,
            detail="TRAE_MOUTH_API_KEY is not configured; remote access denied",
        )

    def prepare_account(record: Any) -> tuple[Any, Any, Any]:
        secrets = accounts.get_secrets(record.id)
        now = int(time.time())
        expiry = secrets.token_expires_at
        if expiry and expiry <= now + 60 and secrets.refresh_token:
            if expiry <= now:
                refresher.refresh(record.id)
                record = accounts.get(record.id) or record
                secrets = accounts.get_secrets(record.id)
            else:
                # Keep the current valid token and refresh off the request path.
                threading.Thread(
                    target=refresher.refresh,
                    args=(record.id,),
                    name=f"account-refresh-{record.id}",
                    daemon=True,
                ).start()
        if not secrets.token:
            raise HTTPException(status_code=503, detail="account has no token")
        device = ensure_device(accounts, record.id, config)
        return record, secrets, device

    def _resolve(
        account_id: str,
        model: str,
        prefer_max: bool,
        channel: str = "",
    ):
        catalog = model_catalog_for(account_id, False)
        resolved = ide_chat.resolve_model(
            catalog, model, prefer_max=prefer_max, channel=channel
        )
        if resolved:
            return catalog, resolved
        catalog = model_catalog_for(account_id, True)
        resolved = ide_chat.resolve_model(
            catalog, model, prefer_max=prefer_max, channel=channel
        )
        return catalog, resolved

    async def _preflight(
        stream: AsyncIterator[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        buffered: list[dict[str, Any]] = []
        timeout = max(10, int(getattr(config, "first_token_timeout", 120) or 120))
        meaningful = {
            "output",
            "text_delta",
            "reasoning_delta",
            "thought",
            "tool_call",
            "done",
            "content_security",
        }
        for _ in range(20):
            try:
                event = await asyncio.wait_for(stream.__anext__(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise UpstreamError(
                    f"upstream first token timeout after {timeout}s",
                    status_code=504,
                ) from exc
            except StopAsyncIteration:
                return buffered, False
            buffered.append(event)
            name = str(event.get("event") or "")
            if name == "error":
                data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
                message = str(data.get("message") or data.get("error") or "upstream error")
                raise UpstreamError(message, status_code=502)
            if name == "queue_continue":
                raise UpstreamError(
                    "upstream requested a queued retry before first token",
                    status_code=503,
                )
            if name in meaningful:
                return buffered, True
        return buffered, True

    async def _with_heartbeat(
        stream: AsyncIterator[dict[str, Any]],
    ) -> AsyncIterator[dict[str, Any]]:
        interval = max(1, int(getattr(config, "stream_heartbeat", 15) or 15))
        iterator = stream.__aiter__()
        pending: Optional[asyncio.Task[dict[str, Any]]] = None
        while True:
            if pending is None:
                pending = asyncio.create_task(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield {"event": "__heartbeat__", "data": {}}
                continue
            task, pending = pending, None
            try:
                event = task.result()
            except StopAsyncIteration:
                return
            yield event

    @router.get("/v1/models")
    def list_models(
        request: Request,
        account_id: str = "",
        auth: ApiAuth = Depends(authorize),
    ) -> dict[str, Any]:
        if not auth.allows("models"):
            raise HTTPException(status_code=403, detail="api key scope denied")
        if account_id:
            record = accounts.get(account_id)
            if record is None:
                raise HTTPException(status_code=404, detail="account not found")
            records = [record]
        else:
            records = account_router.active()
            if not records:
                raise HTTPException(status_code=503, detail="no active account")
        models: dict[str, dict[str, Any]] = {}
        group_order = (
            ["ide"]
            if getattr(config, "global_ide", True)
            else ["remote", "work", "ide"]
        )
        for record in records:
            try:
                catalog = model_catalog_for(record.id, False)
            except Exception:
                continue
            groups = catalog.get("groups") or {}
            for group_key in group_order:
                group = groups.get(group_key)
                if not isinstance(group, Mapping):
                    continue
                for model in group.get("models") or []:
                    config_name = str(model.get("config_name") or "")
                    if not config_name:
                        continue
                    web = group_key in web_chat.WEB_CHANNELS
                    model_ids = (
                        [f"{group_key}/{config_name}"] if web else [config_name]
                    )
                    for model_id in model_ids:
                        existing = models.get(model_id)
                        if existing:
                            existing["trae"]["endpoints"] = sorted(
                                set(existing["trae"]["endpoints"]) | {group_key}
                            )
                            existing["trae"]["account_ids"] = sorted(
                                set(existing["trae"]["account_ids"]) | {record.id}
                            )
                            continue
                        models[model_id] = {
                            "id": model_id,
                            "object": "model",
                            "created": int(catalog.get("fetched_at") or time.time()),
                            "owned_by": "trae",
                            "trae": {
                                "display_name": model.get("display_name"),
                                "channel": group_key,
                                "endpoints": [group_key],
                                "tools_supported": not web,
                                "functions": model.get("functions") or [],
                                "context_window": model.get("context_window"),
                                "max_context": model.get("max_context"),
                                "max_output": model.get("max_output"),
                                "multimodal": bool(model.get("multimodal")),
                                "max_mode": bool(model.get("max_mode")),
                                "reasoning": model.get("reasoning") or {},
                                "provider_models": model.get("provider_models") or [],
                                "account_ids": [record.id],
                            },
                        }
        return {"object": "list", "data": sorted(models.values(), key=lambda item: item["id"])}

    def _record_usage(
        *,
        request_id: str,
        auth: ApiAuth,
        account_id: str,
        model: str,
        status: int,
        usage: Any,
        first_token_ms: int,
        started: float,
        error: str = "",
        source_ip: str = "",
        client: str = "",
        reasoning_effort: str = "",
        context_window: int = 0,
    ) -> None:
        payload = usage if isinstance(usage, Mapping) else {}
        prompt_tokens = int(payload.get("prompt_tokens") or 0)
        completion_tokens = int(payload.get("completion_tokens") or 0)
        total_tokens = int(
            payload.get("total_tokens") or prompt_tokens + completion_tokens
        )
        latency_ms = int((time.time() - started) * 1000)
        try:
            api_keys.add_event(
                request_id=request_id,
                api_key_id=auth.key_id,
                account_id=account_id,
                model=model,
                endpoint="/v1/chat/completions",
                status=status,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                first_token_ms=first_token_ms,
                latency_ms=latency_ms,
                error=error,
                source_ip=source_ip,
                client=client,
                reasoning_effort=reasoning_effort,
                context_window=context_window,
            )
            api_keys.record_usage(
                auth.key_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
        except Exception:
            logger.exception("failed to record api usage")

    @router.post("/v1/chat/completions")
    async def chat_completions(
        payload: ChatRequest,
        request: Request,
        auth: ApiAuth = Depends(authorize),
    ) -> Any:
        if not auth.allows("chat"):
            raise HTTPException(status_code=403, detail="api key scope denied")
        started = time.time()
        request_id = "req_" + uuid.uuid4().hex
        client_ip = request_ip(request)
        client_name = (
            request.headers.get("x-trae-client", "").strip()
            or request.headers.get("x-client-name", "").strip()
            or "api"
        )
        first_token_ms = 0
        conversation_key, explicit_key = _conversation_key(payload, request)
        requested_account = _requested_account_id(payload, request)
        prefer_max = True
        model_channel, _ = ide_chat.parse_model_channel(payload.model)
        channel = model_channel or (
            str(request.headers.get("x-trae-channel") or "").strip().lower()
        )
        if getattr(config, "global_ide", False):
            _, payload.model = ide_chat.parse_model_channel(payload.model)
            channel = "ide"
        requested_effort = str(payload.reasoning_effort or "").strip()
        preferred = requested_account or account_router.affinity(conversation_key) or ""
        active = account_router.active()
        if not active:
            _record_usage(
                request_id=request_id,
                auth=auth,
                account_id="",
                model=payload.model,
                status=503,
                usage=None,
                first_token_ms=0,
                started=started,
                error="no active account",
            )
            raise HTTPException(status_code=503, detail="no active account")
        failovers = max(0, int(config.account_failover_limit or 0))
        max_attempts = min(len(active), failovers + 1)
        exclude: set[str] = set()
        refreshed: set[str] = set()
        attempts = 0
        last_error: Optional[BaseException] = None
        last_catalog: dict[str, Any] = {}

        def retry_after_auth_error(record: Any, status_code: int) -> bool:
            """Refresh an account once on 401/403 and let the loop retry it."""
            if status_code not in {401, 403} or record.id in refreshed:
                return False
            refreshed.add(record.id)
            result = refresher.refresh(record.id, force=True)
            if not result.ok:
                return False
            exclude.discard(record.id)
            return True

        while attempts < max_attempts:
            record = account_router.choose(
                preferred=preferred if attempts == 0 and not exclude else "",
                exclude=exclude,
            )
            if record is None:
                break
            exclude.add(record.id)
            catalog, resolved = _resolve(
                record.id, payload.model, prefer_max, channel
            )
            last_catalog = catalog
            if not resolved:
                continue

            attempts += 1
            entry = resolved.get("entry") or {}
            context_window = int(
                entry.get("max_context") or entry.get("context_window") or 0
            )
            reasoning = entry.get("reasoning") or {}
            effective_effort = ""
            if requested_effort and reasoning.get("supported"):
                levels = [str(level) for level in reasoning.get("levels") or []]
                effective_effort = _nearest_reasoning_level(
                    requested_effort,
                    levels,
                    str(reasoning.get("default") or ""),
                )
            if explicit_key:
                session_id = conversation_key
            else:
                session_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        "trae-mouth-session:"
                        + record.id
                        + "\x1f"
                        + conversation_key,
                    )
                )

            account_router.acquire(record.id)
            stream: Optional[AsyncIterator[dict[str, Any]]] = None
            keep_stream = False
            try:
                try:
                    record, secrets, device = prepare_account(record)
                except HTTPException as exc:
                    last_error = exc
                    if _should_failover_status(exc.status_code):
                        account_router.mark_failure(
                            record, exc.status_code, str(exc.detail)
                        )
                    continue

                web = bool(resolved.get("web"))
                if web and payload.tools and payload.tool_choice != "none":
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "work/design/remote channels do not support downstream "
                            "tool calls; use the default IDE model id for tools"
                        ),
                    )
                if web:
                    stream = web_chat.stream_chat(
                        config,
                        secrets.token,
                        record.user_id,
                        resolved,
                        payload.messages,
                        transport=transport,
                    )
                else:
                    body = ide_chat.build_body(
                        resolved,
                        payload.messages,
                        tools=payload.tools,
                        tool_choice=payload.tool_choice,
                        parallel_tool_calls=payload.parallel_tool_calls,
                        session_id=session_id,
                        reasoning_effort=effective_effort,
                        max_tool_result_chars=int(config.tool_result_max_chars or 0),
                        max_history_chars=int(config.history_max_chars or 0),
                        max_history_messages=int(config.history_max_messages or 0),
                        include_completed_calls=bool(config.tool_duplicate_warning),
                    )
                    stream = ide_chat.stream_raw(
                        config,
                        secrets.token,
                        record.user_id,
                        device,
                        resolved,
                        body,
                        transport=transport,
                    )
                translator = OpenAIStreamTranslator(
                    payload.model,
                    tools_expected=(
                        not web
                        and bool(payload.tools)
                        and payload.tool_choice != "none"
                    ),
                    reasoning_as_content=bool(config.reasoning_as_content),
                    tool_schemas=(
                        ide_chat.tool_schema_map(payload.tools)
                        if payload.tools and not web
                        else {}
                    ),
                    strict_tool_args=bool(config.strict_tool_args),
                    tools_required=(
                        not web
                        and bool(payload.tools)
                        and payload.tool_choice == "required"
                    ),
                )
                trae_meta = {
                    "endpoint": resolved.get("endpoint"),
                    "channel": resolved.get("channel") or resolved.get("endpoint"),
                    "transport": "web" if web else "raw",
                    "agent_type": resolved.get("agent_type"),
                    "config_name": resolved.get("config_name"),
                    "provider_model": resolved.get("model_name"),
                    "function": resolved.get("raw_chat_function"),
                    "max_mode": bool(resolved.get("max_mode_applied")),
                    "session_id": session_id,
                    "account_id": record.id,
                    "reasoning_effort_requested": requested_effort,
                    "reasoning_effort_applied": effective_effort,
                    "failover": attempts > 1,
                    "attempts": attempts,
                }

                if not payload.stream:
                    try:
                        async for event in stream:
                            for _chunk in translator.feed(event):
                                pass
                        translator.finalize()
                    except UpstreamError as exc:
                        last_error = exc
                        if _should_failover_status(exc.status_code):
                            if retry_after_auth_error(record, exc.status_code):
                                attempts -= 1
                                continue
                            account_router.mark_failure(
                                record, exc.status_code, str(exc)
                            )
                            continue
                        _record_usage(
                            request_id=request_id,
                            auth=auth,
                            account_id=record.id,
                            model=payload.model,
                            status=exc.status_code,
                            usage=translator.usage,
                            first_token_ms=first_token_ms,
                            started=started,
                            error=str(exc),
                        )
                        raise HTTPException(
                            status_code=exc.status_code, detail=str(exc)
                        ) from exc
                    result = translator.result()
                    result["trae"] = trae_meta
                    account_router.bind(conversation_key, record.id)
                    _record_usage(
                        request_id=request_id,
                        auth=auth,
                        account_id=record.id,
                        model=payload.model,
                        status=200,
                        usage=result.get("usage"),
                        first_token_ms=first_token_ms,
                        started=started,
                        source_ip=client_ip,
                        client=client_name,
                        reasoning_effort=effective_effort,
                        context_window=context_window,
                    )
                    return result

                try:
                    buffered, ok = await _preflight(stream)
                except UpstreamError as exc:
                    last_error = exc
                    if _should_failover_status(exc.status_code):
                        if retry_after_auth_error(record, exc.status_code):
                            attempts -= 1
                            continue
                        account_router.mark_failure(
                            record, exc.status_code, str(exc)
                        )
                        continue
                    _record_usage(
                        request_id=request_id,
                        auth=auth,
                        account_id=record.id,
                        model=payload.model,
                        status=exc.status_code,
                        usage=translator.usage,
                        first_token_ms=first_token_ms,
                        started=started,
                        error=str(exc),
                    )
                    raise HTTPException(
                        status_code=exc.status_code, detail=str(exc)
                    ) from exc
                if not ok:
                    last_error = UpstreamError(
                        "upstream stream ended before the first token",
                        status_code=502,
                    )
                    account_router.mark_failure(record, 502, str(last_error))
                    continue

                account_router.bind(conversation_key, record.id)
                keep_stream = True

                async def generator() -> AsyncIterator[str]:
                    nonlocal first_token_ms
                    status_code = 200
                    error_text = ""

                    def emit(chunks: list[str]) -> AsyncIterator[str]:
                        nonlocal first_token_ms
                        for chunk in chunks:
                            if not first_token_ms:
                                first_token_ms = int(
                                    (time.time() - started) * 1000
                                )
                            yield chunk

                    try:
                        for event in buffered:
                            for chunk in emit(translator.feed(event)):
                                yield chunk
                        async for event in _with_heartbeat(stream):
                            if event.get("event") == "__heartbeat__":
                                yield ": ping\n\n"
                                continue
                            for chunk in emit(translator.feed(event)):
                                yield chunk
                        for chunk in emit(translator.finalize()):
                            yield chunk
                    except UpstreamError as exc:
                        status_code = exc.status_code
                        error_text = str(exc)
                        account_router.mark_failure(
                            record, exc.status_code, str(exc)
                        )
                        yield _sse(
                            {
                                "error": {
                                    "message": str(exc),
                                    "type": "upstream_error",
                                }
                            }
                        )
                        yield "data: [DONE]\n\n"
                    except BaseException:
                        status_code = 499
                        error_text = "client disconnected"
                        raise
                    finally:
                        try:
                            await stream.aclose()
                        finally:
                            account_router.release(record.id)
                            _record_usage(
                                request_id=request_id,
                                auth=auth,
                                account_id=record.id,
                                model=payload.model,
                                status=status_code,
                                usage=translator.usage,
                                first_token_ms=first_token_ms,
                                started=started,
                                error=error_text,
                                source_ip=client_ip,
                                client=client_name,
                                reasoning_effort=effective_effort,
                                context_window=context_window,
                            )

                headers = {
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "X-Trae-Model": str(resolved.get("config_name") or ""),
                    "X-Trae-Provider-Model": str(
                        resolved.get("model_name") or ""
                    ),
                    "X-Trae-Max-Mode": (
                        "1" if resolved.get("max_mode_applied") else "0"
                    ),
                    "X-Trae-Session-Id": session_id,
                    "X-Trae-Account-Id": record.id,
                    "X-Trae-Failover": "1" if attempts > 1 else "0",
                }
                return StreamingResponse(
                    generator(),
                    media_type="text/event-stream",
                    headers=headers,
                )
            finally:
                if not keep_stream:
                    if stream is not None:
                        await stream.aclose()
                    account_router.release(record.id)

        if attempts == 0:
            names: list[str] = []
            for key in ("ide", "solo"):
                for model in (
                    last_catalog.get("groups", {}).get(key, {}) or {}
                ).get("models", []):
                    names.append(str(model.get("config_name") or ""))
            detail = (
                f"model {payload.model!r} is not available on IDE/Solo raw transport; "
                f"available examples: {', '.join(names[:20])}"
            )
            _record_usage(
                request_id=request_id,
                auth=auth,
                account_id="",
                model=payload.model,
                status=400,
                usage=None,
                first_token_ms=0,
                started=started,
                error=detail,
            )
            raise HTTPException(status_code=400, detail=detail)
        if isinstance(last_error, UpstreamError):
            _record_usage(
                request_id=request_id,
                auth=auth,
                account_id="",
                model=payload.model,
                status=last_error.status_code,
                usage=None,
                first_token_ms=0,
                started=started,
                error=str(last_error),
            )
            raise HTTPException(
                status_code=last_error.status_code,
                detail=str(last_error),
            ) from last_error
        if isinstance(last_error, HTTPException):
            _record_usage(
                request_id=request_id,
                auth=auth,
                account_id="",
                model=payload.model,
                status=last_error.status_code,
                usage=None,
                first_token_ms=0,
                started=started,
                error=str(last_error.detail),
            )
            raise last_error
        detail = (
            f"all accounts failed: {last_error}"
            if last_error is not None
            else "all accounts failed"
        )
        _record_usage(
            request_id=request_id,
            auth=auth,
            account_id="",
            model=payload.model,
            status=503,
            usage=None,
            first_token_ms=0,
            started=started,
            error=detail,
        )
        raise HTTPException(status_code=503, detail=detail)

    return router
