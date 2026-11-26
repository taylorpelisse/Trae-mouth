from __future__ import annotations

import json

import httpx

from app.accounts.models import build_device
from app.api.openai_routes import _REASONING_RANKS
from app.upstream import model_catalog
from app.upstream.model_catalog import CatalogCache, fetch_catalog

RANKS = _REASONING_RANKS


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/api/remote/v1/models"):
        function = request.url.params.get("functions") or ""
        if function == "solo_agent_remote":
            model = {
                "name": "glm-5.4",
                "display_name": "GLM-5.4",
                "multimodal": True,
                "max_mode": True,
                "context_window_tokens": {"dev": 256000, "max": 1000000},
                "reasoning_effort_config": {
                    "support_thinking": True,
                    "options": ["light", "high"],
                    "default_level": "high",
                },
                "features": json.dumps(
                    {"consumption_rate": {"enable": True, "data": {"rate": 0.8}}}
                ),
                "config_source": 1,
            }
        elif function == "solo_work_remote":
            model = {
                "name": "glm-5.4",
                "display_name": "GLM-5.4",
                "multimodal": True,
                "max_mode": False,
                "context_window_tokens": {"dev": 256000, "max": 0},
                "reasoning_effort_config": {
                    "support_thinking": False,
                    "options": None,
                    "default_level": "",
                },
                "features": "{}",
                "config_source": 1,
            }
        else:
            model = {
                "name": "Doubao-Seed-2.1-Turbo",
                "display_name": "Seed-2.1-Turbo",
                "multimodal": True,
                "max_mode": False,
                "context_window_tokens": {"dev": 232768, "max": 0},
                "reasoning_effort_config": {
                    "support_thinking": False,
                    "options": None,
                    "default_level": "",
                },
                "features": "{}",
                "config_source": 1,
            }
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"list": [{"function": function, "models": [model]}]},
            },
        )

    if request.url.path.endswith("/api/ide/v1/get_detail_param"):
        body = json.loads(request.content)
        function = body.get("function")
        if function == "chat_v3":
            rows = [
                {
                    "config_name": "glm-5.4",
                    "config_source": 1,
                    "usage": "chat_completion",
                    "context_window_tokens": {"dev": 116000},
                    "display_config": {
                        "display_name": "GLM-5.4 IDE",
                        "multimodal": False,
                        "model_capability": "reasoning_model",
                        "max_mode": False,
                    },
                    "model_detail_list": [
                        {
                            "model_name": "glm-5.4__dev",
                            "max_tokens": 16000,
                            "prompt_max_tokens": 100000,
                            "model_entitlement_usage_cost": 1,
                        },
                        {
                            "model_name": "glm-5.4__max",
                            "max_tokens": 64000,
                            "prompt_max_tokens": 936000,
                            "model_entitlement_usage_cost": 1,
                        },
                    ],
                }
            ]
        else:
            rows = [
                {
                    "config_name": "solo-model",
                    "config_source": 1,
                    "usage": "chat_completion",
                    "context_window_tokens": {"dev": 200000},
                    "display_config": {
                        "display_name": "Solo Model",
                        "multimodal": True,
                        "model_capability": "chat_model",
                        "max_mode": False,
                    },
                    "model_detail_list": [
                        {
                            "model_name": "solo-model__dev",
                            "max_tokens": 32000,
                            "prompt_max_tokens": 168000,
                            "model_entitlement_usage_cost": 1,
                        }
                    ],
                }
            ]
        return httpx.Response(200, json={"config_info_list": rows})

    return httpx.Response(404, text="not found")


def test_fetch_catalog_groups_and_capabilities(config) -> None:
    device = build_device()
    catalog = fetch_catalog(
        config,
        "token",
        "user-1",
        device,
        transport=httpx.MockTransport(handler),
    )
    groups = catalog["groups"]
    assert set(groups) == {"ide", "solo", "remote", "work", "design"}
    assert catalog["model_count"] == 5

    ide = groups["ide"]["models"][0]
    assert ide["config_name"] == "glm-5.4"
    assert ide["context_window"] == 116000
    assert ide["max_output"] == 64000
    assert ide["max_context"] == 936000
    assert ide["multimodal"] is False
    assert ide["reasoning"]["supported"] is True
    assert ide["reasoning"]["levels"] == ["light", "high"]
    assert ide["reasoning"]["source"] == "web_catalog"
    assert ide["provider_models"] == ["glm-5.4__dev", "glm-5.4__max"]

    remote = groups["remote"]["models"][0]
    assert remote["context_window"] == 256000
    assert remote["max_context"] == 1000000
    assert remote["max_output"] is None
    assert remote["max_mode"] is True
    assert remote["reasoning"]["levels"] == ["light", "high"]
    assert remote["usage_cost"] == 0.8

    work = groups["work"]["models"][0]
    assert work["context_window"] == 256000
    assert work["max_mode"] is False
    assert work["reasoning"]["supported"] is False

    solo = groups["solo"]["models"][0]
    assert solo["max_output"] == 32000
    assert solo["multimodal"] is True
    assert groups["solo"]["functions"] == ["solo_agent", "builder_v3"]


def test_catalog_cache() -> None:
    cache = CatalogCache(ttl=60)
    assert cache.get("a") is None
    cache.put("a", {"ok": True})
    assert cache.get("a") == {"ok": True}
    assert cache.get("a", force=True) is None
    cache.invalidate("a")
    assert cache.get("a") is None


def test_catalog_cache_keeps_stale_value() -> None:
    cache = CatalogCache(ttl=10)
    cache.put("a", {"ok": True})
    cache._data["a"] = (0, {"ok": True})
    assert cache.get("a") is None
    assert cache.get_stale("a") == {"ok": True}
    assert cache.begin_refresh("a") is True
    assert cache.begin_refresh("a") is False
    assert cache.end_refresh("a") is None
    assert cache.begin_refresh("a") is True


def test_openai_effort_uses_openai_vocabulary() -> None:
    assert model_catalog.openai_effort("light", RANKS) == "low"
    assert model_catalog.openai_effort("LIGHT", RANKS) == "low"
    assert model_catalog.openai_effort("high", RANKS) == "high"
    assert model_catalog.openai_effort("extra_high", RANKS) == "xhigh"
    # 表里没有的写法原样保留，不臆造
    assert model_catalog.openai_effort("turbo", RANKS) == "turbo"
    assert model_catalog.openai_effort("", RANKS) == ""


def test_to_openai_model_follows_common_gateway_fields() -> None:
    model = {
        "config_name": "glm",
        "display_name": "GLM",
        "provider_models": ["glm__dev", "glm__max"],
        "context_window": 116000,
        "max_context": 1000000,
        "max_output": 64000,
        "multimodal": True,
        "usage_cost": 0.5,
        "reasoning": {
            "supported": True,
            "levels": ["light", "high", "extra_high"],
            "default": "high",
            "source": "ide",
        },
    }

    entry = model_catalog.to_openai_model(
        model, "glm", tools=True, ranks=RANKS, created=1700000000
    )

    assert entry == {
        "id": "glm",
        "object": "model",
        "created": 1700000000,
        "owned_by": "trae",
        "name": "GLM",
        "context_length": 1000000,
        "architecture": {
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
        },
        "top_provider": {"context_length": 1000000, "max_completion_tokens": 64000},
        "supported_parameters": [
            "parallel_tool_calls",
            "reasoning_effort",
            "tool_choice",
            "tools",
        ],
        "reasoning": {
            "supported": True,
            "supported_efforts": ["low", "high", "xhigh"],
            "default_effort": "high",
        },
    }
    text = json.dumps(entry)
    for private in (
        "account_ids",
        "provider_models",
        "usage_cost",
        "config_name",
        "config_source",
        "functions",
        "source",
    ):
        assert private not in text


def test_to_openai_model_without_reasoning_or_tools() -> None:
    model = {
        "config_name": "plain",
        "context_window": 32000,
        "reasoning": {"supported": False, "levels": [], "default": ""},
    }

    entry = model_catalog.to_openai_model(
        model, "work/plain", tools=False, ranks=RANKS
    )

    assert "reasoning" not in entry
    assert entry["supported_parameters"] == []
    assert entry["context_length"] == 32000
    assert entry["top_provider"]["max_completion_tokens"] is None
    assert entry["architecture"]["input_modalities"] == ["text"]
    # 没有展示名时用模型 id
    assert entry["name"] == "work/plain"
