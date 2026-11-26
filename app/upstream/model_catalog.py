from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

import httpx

from ..accounts.models import DeviceProfile
from ..config import Config
from .catalog import ide_headers, web_headers

WEB_GROUPS = {
    "solo_agent_remote": ("remote", "Remote"),
    "solo_work_remote": ("work", "Work"),
    "solo_design_remote": ("design", "Solo Web"),
}
IDE_GROUPS = {
    "chat_v3": ("ide", "IDE Agent"),
    "solo_agent": ("solo", "Solo"),
    "builder_v3": ("solo", "Solo"),
}


def _int(value: Any) -> Optional[int]:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# OpenAI reasoning_effort 的取值，按强度从低到高
OPENAI_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def openai_effort(level: Any, ranks: Mapping[str, int]) -> str:
    """上游档位名换成 OpenAI 写法：light → low、extra_high → xhigh。

    ranks 是请求侧的档位强度表，保证对外声明的写法发回来时
    仍落在原来那个上游档位上。
    """
    text = str(level or "").strip().lower()
    if not text:
        return ""
    if text in OPENAI_EFFORTS:
        return text
    rank = ranks.get(text)
    if rank is None:
        return text
    # 同一档里有多个写法时取较强的那个：light 对应 low，而不是接近关闭的 minimal
    return next(
        (name for name in reversed(OPENAI_EFFORTS) if ranks.get(name) == rank), text
    )


def to_openai_model(
    model: Mapping[str, Any],
    model_id: str,
    *,
    tools: bool,
    ranks: Mapping[str, int],
    created: Any = None,
) -> dict[str, Any]:
    """目录里的一个模型 → /v1/models 的条目。

    外层是 OpenAI 标准的四个字段；上下文、模态、推理档位这些
    能力字段 OpenAI 没有定义，按 OpenRouter 的通行写法给出。
    """
    reasoning = model.get("reasoning") or {}
    efforts: list[str] = []
    for level in reasoning.get("levels") or []:
        effort = openai_effort(level, ranks)
        if effort and effort not in efforts:
            efforts.append(effort)
    context = _int(model.get("max_context")) or _int(model.get("context_window"))
    parameters = ["reasoning_effort"] if efforts else []
    if tools:
        parameters += ["tools", "tool_choice", "parallel_tool_calls"]
    entry: dict[str, Any] = {
        "id": model_id,
        "object": "model",
        "created": int(created or time.time()),
        "owned_by": "trae",
        "name": str(model.get("display_name") or model_id),
        "context_length": context,
        "architecture": {
            "input_modalities": ["text", "image"] if model.get("multimodal") else ["text"],
            "output_modalities": ["text"],
        },
        "top_provider": {
            "context_length": context,
            "max_completion_tokens": _int(model.get("max_output")),
        },
        "supported_parameters": sorted(parameters),
    }
    if efforts:
        entry["reasoning"] = {
            "supported": True,
            "supported_efforts": efforts,
            "default_effort": openai_effort(reasoning.get("default"), ranks) or None,
        }
    return entry


@dataclass
class ModelVariant:
    model_name: str
    max_tokens: Optional[int] = None
    prompt_max_tokens: Optional[int] = None
    usage_cost: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "max_tokens": self.max_tokens,
            "prompt_max_tokens": self.prompt_max_tokens,
            "usage_cost": self.usage_cost,
        }


@dataclass
class ModelEntry:
    config_name: str
    display_name: str
    endpoint: str
    functions: list[str] = field(default_factory=list)
    provider_models: list[str] = field(default_factory=list)
    context_window: Optional[int] = None
    max_context: Optional[int] = None
    max_output: Optional[int] = None
    multimodal: bool = False
    reasoning_supported: bool = False
    reasoning_levels: list[str] = field(default_factory=list)
    default_reasoning: str = ""
    reasoning_source: str = ""
    max_mode: bool = False
    config_source: int = 1
    usage_cost: Optional[float] = None
    source: str = ""
    variants: list[ModelVariant] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_name": self.config_name,
            "display_name": self.display_name,
            "endpoint": self.endpoint,
            "functions": self.functions,
            "provider_models": self.provider_models,
            "context_window": self.context_window,
            "max_context": self.max_context,
            "max_output": self.max_output,
            "multimodal": self.multimodal,
            "reasoning": {
                "supported": self.reasoning_supported,
                "levels": self.reasoning_levels,
                "default": self.default_reasoning,
                "source": self.reasoning_source,
            },
            "max_mode": self.max_mode,
            "config_source": self.config_source,
            "usage_cost": self.usage_cost,
            "source": self.source,
            "variants": [variant.to_dict() for variant in self.variants],
            "raw": self.raw,
        }

    def merge(self, other: "ModelEntry") -> None:
        for function in other.functions:
            if function not in self.functions:
                self.functions.append(function)
        for model in other.provider_models:
            if model not in self.provider_models:
                self.provider_models.append(model)
        for variant in other.variants:
            if not any(
                existing.model_name == variant.model_name
                for existing in self.variants
            ):
                self.variants.append(variant)
        for attr in ("context_window", "max_context", "max_output"):
            left = getattr(self, attr)
            right = getattr(other, attr)
            if right is not None and (left is None or right > left):
                setattr(self, attr, right)
        self.multimodal = self.multimodal or other.multimodal
        self.max_mode = self.max_mode or other.max_mode
        self.reasoning_supported = (
            self.reasoning_supported or other.reasoning_supported
        )
        for level in other.reasoning_levels:
            if level not in self.reasoning_levels:
                self.reasoning_levels.append(level)
        if not self.default_reasoning:
            self.default_reasoning = other.default_reasoning
        if not self.reasoning_source:
            self.reasoning_source = other.reasoning_source
        if self.usage_cost is None:
            self.usage_cost = other.usage_cost
        if not self.raw and other.raw:
            self.raw = other.raw


def _web_entry(
    raw: dict[str, Any], function: str, endpoint: str
) -> Optional[ModelEntry]:
    config_name = str(
        raw.get("config_name") or raw.get("name") or raw.get("model_name") or ""
    ).strip()
    if not config_name:
        return None
    context = _int((raw.get("context_window_tokens") or {}).get("dev"))
    max_context = _int((raw.get("context_window_tokens") or {}).get("max"))
    if not max_context:
        max_context = context
    reasoning = raw.get("reasoning_effort_config") or {}
    levels = [
        str(level)
        for level in (reasoning.get("options") or [])
        if str(level).strip()
    ]
    features = _json(raw.get("features"))
    rate = (
        (features.get("consumption_rate") or {}).get("data") or {}
    ).get("rate")
    return ModelEntry(
        config_name=config_name,
        display_name=str(raw.get("display_name") or config_name),
        endpoint=endpoint,
        functions=[function],
        context_window=context,
        max_context=max_context,
        max_output=None,
        multimodal=bool(raw.get("multimodal")),
        reasoning_supported=bool(reasoning.get("support_thinking")),
        reasoning_levels=levels,
        default_reasoning=str(reasoning.get("default_level") or ""),
        reasoning_source="web" if levels else "",
        max_mode=bool(raw.get("max_mode"))
        or bool(max_context and max_context >= 950_000),
        config_source=int(raw.get("config_source") or 1),
        usage_cost=_float(rate),
        source="web",
        raw=raw,
    )


def _ide_entry(raw: dict[str, Any], function: str, endpoint: str) -> Optional[ModelEntry]:
    if str(raw.get("usage") or "") != "chat_completion":
        return None
    config_name = str(raw.get("config_name") or raw.get("name") or "").strip()
    if not config_name:
        return None
    display = raw.get("display_config") or {}
    window = raw.get("context_window_tokens") or {}
    context = _int(window.get("dev"))
    max_window = _int(window.get("max"))
    variants: list[ModelVariant] = []
    for item in raw.get("model_detail_list") or []:
        if not isinstance(item, dict) or not item.get("model_name"):
            continue
        variants.append(
            ModelVariant(
                model_name=str(item["model_name"]),
                max_tokens=_int(item.get("max_tokens")),
                prompt_max_tokens=_int(item.get("prompt_max_tokens")),
                usage_cost=_float(item.get("model_entitlement_usage_cost")),
            )
        )
    max_output = max(
        (variant.max_tokens for variant in variants if variant.max_tokens),
        default=None,
    )
    prompt_max = max(
        (variant.prompt_max_tokens for variant in variants if variant.prompt_max_tokens),
        default=None,
    )
    max_context = max(
        (value for value in (context, max_window, prompt_max) if value),
        default=None,
    )
    capability = str(display.get("model_capability") or "")
    reasoning = raw.get("reasoning_effort_config") or {}
    reasoning_levels = [
        str(level)
        for level in (reasoning.get("options") or [])
        if str(level).strip()
    ]
    reasoning_default = str(reasoning.get("default_level") or "")
    reasoning_supported = bool(reasoning.get("support_thinking"))
    if not reasoning and capability == "reasoning_model":
        reasoning_supported = True
    default_variant = next(
        (variant for variant in variants if variant.model_name.endswith("__dev")),
        variants[0] if variants else None,
    )
    return ModelEntry(
        config_name=config_name,
        display_name=str(display.get("display_name") or config_name),
        endpoint=endpoint,
        functions=[function],
        provider_models=[variant.model_name for variant in variants],
        context_window=context,
        max_context=max_context,
        max_output=max_output,
        multimodal=bool(display.get("multimodal")),
        reasoning_supported=reasoning_supported,
        reasoning_levels=reasoning_levels,
        default_reasoning=reasoning_default,
        reasoning_source="ide" if reasoning_levels else "",
        max_mode=bool(display.get("max_mode"))
        or bool(max_window and max_window >= 950_000)
        or bool(context and context >= 950_000),
        config_source=int(raw.get("config_source") or 1),
        usage_cost=default_variant.usage_cost if default_variant else None,
        source="ide",
        variants=variants,
    )


def _fetch_web(
    client: httpx.Client,
    config: Config,
    token: str,
    user_id: str,
    function: str,
) -> list[dict[str, Any]]:
    url = (
        f"{config.web_api_base}/models?functions={function}&show_custom_model=true"
    )
    response = client.get(url, headers=web_headers(config, token, user_id))
    response.raise_for_status()
    data = response.json()
    groups = (data.get("data") or {}).get("list") or []
    if isinstance(groups, dict):
        groups = [groups]
    for group in groups:
        if str(group.get("function") or "") == function:
            return [model for model in group.get("models") or [] if isinstance(model, dict)]
    models: list[dict[str, Any]] = []
    for group in groups:
        models.extend(
            [model for model in group.get("models") or [] if isinstance(model, dict)]
        )
    return models


def _fetch_ide(
    client: httpx.Client,
    config: Config,
    token: str,
    user_id: str,
    device: DeviceProfile,
    function: str,
) -> list[dict[str, Any]]:
    body = {
        "function": function,
        "config_names": None,
        "need_prompt": False,
        "current_config_info": None,
        "poly_prompt": True,
        "mode_type": None,
        "agent_type": None,
    }
    response = client.post(
        f"{config.model_gateway}/api/ide/v1/get_detail_param",
        headers=ide_headers(config, token, user_id, device, function),
        json=body,
    )
    response.raise_for_status()
    data = response.json()
    rows = data.get("config_info_list") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        nested = data.get("data") if isinstance(data, dict) else None
        rows = nested.get("config_info_list") if isinstance(nested, dict) else []
    return [row for row in rows or [] if isinstance(row, dict)]


# 官方客户端用一个批量接口一次取回全部功能的目录；逐个功能查询只作为退路
IDE_BATCH_PATH = "/api/ide/v1/batch_get_detail_param"


def _fetch_ide_all(
    client: httpx.Client,
    config: Config,
    token: str,
    user_id: str,
    device: DeviceProfile,
    functions: list[str],
) -> dict[str, list[dict[str, Any]]]:
    rows_by_function: dict[str, list[dict[str, Any]]] = {}
    try:
        response = client.post(
            f"{config.model_gateway}{IDE_BATCH_PATH}",
            headers=ide_headers(
                config, token, user_id, device, "batch_get_detail_param"
            ),
            json={
                "functions": functions,
                "access_type": None,
                "agent_type": None,
                "current_config_info": None,
                "mode_type": None,
                "Request": None,
            },
        )
        response.raise_for_status()
        data = response.json()
        groups = data.get("function_configs") if isinstance(data, dict) else None
        for item in groups or []:
            if not isinstance(item, dict):
                continue
            function = str(item.get("function") or "")
            rows = item.get("config_info_list")
            if function and isinstance(rows, list):
                rows_by_function[function] = [
                    row for row in rows if isinstance(row, dict)
                ]
    except Exception:
        rows_by_function = {}
    missing = [function for function in functions if function not in rows_by_function]
    if not missing:
        return rows_by_function
    # 批量接口不可用或缺了某个功能：缺的部分退回逐个查询
    for function in missing:
        rows_by_function[function] = _fetch_ide(
            client, config, token, user_id, device, function
        )
    return rows_by_function


def fetch_catalog(
    config: Config,
    token: str,
    user_id: str,
    device: DeviceProfile,
    *,
    transport: Optional[httpx.BaseTransport] = None,
    include_web: bool = True,
) -> dict[str, Any]:
    """拉取一个账号的模型目录。

    IDE 部分一次批量请求取回；``include_web`` 为 False 时不请求 Web 目录，
    由调用方沿用上次的 Web 部分。Web 目录只给非 IDE 通道和推理档位提示用，
    每次要 3 个请求，没必要跟着 IDE 部分一起刷新。
    """
    errors: dict[str, str] = {}
    group_meta: dict[str, dict[str, Any]] = {}
    group_entries: dict[str, dict[str, ModelEntry]] = {}
    web_reasoning: dict[str, ModelEntry] = {}

    def prepare(endpoint: str, label: str, function: str) -> dict[str, ModelEntry]:
        meta = group_meta.setdefault(
            endpoint, {"label": label, "functions": []}
        )
        if function not in meta["functions"]:
            meta["functions"].append(function)
        return group_entries.setdefault(endpoint, {})

    with httpx.Client(timeout=30, transport=transport) as client:
        if include_web:
            for function, (endpoint, label) in WEB_GROUPS.items():
                entries = prepare(endpoint, label, function)
                try:
                    rows = _fetch_web(client, config, token, user_id, function)
                except Exception as exc:
                    errors[endpoint] = str(exc)[:300]
                    continue
                for raw in rows:
                    entry = _web_entry(raw, function, endpoint)
                    if not entry:
                        continue
                    if entry.config_name in entries:
                        entries[entry.config_name].merge(entry)
                    else:
                        entries[entry.config_name] = entry
                    if entry.reasoning_levels:
                        web_reasoning.setdefault(entry.config_name, entry)

        functions = list(IDE_GROUPS)
        try:
            rows_by_function = _fetch_ide_all(
                client, config, token, user_id, device, functions
            )
        except Exception as exc:
            for endpoint, _label in IDE_GROUPS.values():
                errors[endpoint] = str(exc)[:300]
            rows_by_function = {}
        for function, (endpoint, label) in IDE_GROUPS.items():
            entries = prepare(endpoint, label, function)
            for raw in rows_by_function.get(function, []):
                entry = _ide_entry(raw, function, endpoint)
                if not entry:
                    continue
                if entry.reasoning_supported:
                    hint = web_reasoning.get(entry.config_name)
                    if hint:
                        # IDE 的 options 常常只剩 high 一项，官方 App 的完整档位
                        # （lite / high / extra_high）只在 Web 目录里，所以档位
                        # 一律以 Web 为准，不因 IDE 已有档位而跳过。
                        entry.reasoning_levels = list(hint.reasoning_levels)
                        entry.default_reasoning = hint.default_reasoning
                        entry.reasoning_source = "web_catalog"
                if entry.config_name in entries:
                    entries[entry.config_name].merge(entry)
                else:
                    entries[entry.config_name] = entry

    groups: dict[str, dict[str, Any]] = {}
    for endpoint, meta in group_meta.items():
        models = sorted(
            group_entries.get(endpoint, {}).values(),
            key=lambda item: item.display_name.lower(),
        )
        groups[endpoint] = {
            "label": meta["label"],
            "functions": meta["functions"],
            "models": [model.to_dict() for model in models],
            "model_count": len(models),
        }
    return {
        "groups": groups,
        "errors": errors,
        "fetched_at": int(time.time()),
        "model_count": sum(group["model_count"] for group in groups.values()),
    }


# 每个账号的有效期在设定值上下浮动这个比例，避免全部账号同时到期
TTL_JITTER = 0.2


class CatalogCache:
    def __init__(self, ttl: int = 300) -> None:
        self.ttl = max(10, int(ttl))
        self._lock = threading.Lock()
        # account_id -> (写入时间, 目录, 有效期浮动系数)
        self._data: dict[str, tuple[float, dict[str, Any], float]] = {}
        # 上次连 Web 目录一起完整拉取的时间
        self._web_at: dict[str, float] = {}
        self._refreshing: set[str] = set()

    def get_stale(self, account_id: str) -> Optional[dict[str, Any]]:

        with self._lock:
            cached = self._data.get(account_id)
        return cached[1] if cached else None

    def web_fresh(self, account_id: str, ttl: int) -> bool:
        with self._lock:
            fetched_at = self._web_at.get(account_id)
        return bool(fetched_at) and time.time() - fetched_at < ttl

    def begin_refresh(self, account_id: str) -> bool:
        with self._lock:
            if account_id in self._refreshing:
                return False
            self._refreshing.add(account_id)
            return True

    def end_refresh(self, account_id: str) -> None:
        with self._lock:
            self._refreshing.discard(account_id)

    def get(self, account_id: str, force: bool = False) -> Optional[dict[str, Any]]:
        if force:
            return None
        now = time.time()
        with self._lock:
            cached = self._data.get(account_id)
        if not cached:
            return None
        fetched_at, value, factor = cached
        if now - fetched_at > self.ttl * factor:
            # Keep the expired value available to get_stale() for
            # stale-while-revalidate; a background refresh replaces it.
            return None
        return value

    def put(
        self, account_id: str, value: dict[str, Any], *, full: bool = True
    ) -> None:
        with self._lock:
            self._data[account_id] = (
                time.time(),
                value,
                1 + random.uniform(-TTL_JITTER, TTL_JITTER),
            )
            if full:
                self._web_at[account_id] = time.time()

    def invalidate(self, account_id: str) -> None:
        with self._lock:
            self._data.pop(account_id, None)
            self._web_at.pop(account_id, None)
