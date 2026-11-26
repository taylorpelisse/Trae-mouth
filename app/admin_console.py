from __future__ import annotations

import builtins
import datetime as dt
import json
import os
import time
from typing import Any, AsyncIterator, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .accounts.router import AccountRouter
from .accounts.store import AccountStore, STATUS_ACTIVE
from .admin import AdminStore
from .api_keys import ApiKeyStore
from . import checkin as checkin_service
from . import maintenance as maintenance_service
from .config import Config
from .db import Database
from .security import now_ts
from .upstream.ide_chat import map_usage

SESSION_COOKIE = "tm_session"


class SettingsPatchBody(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class PlaygroundBody(BaseModel):
    model: str = ""
    message: str = ""
    account_id: str = ""
    one_m: bool = True
    reasoning_effort: str = ""
    stream: bool = True


class CheckinBody(BaseModel):
    account_id: str = ""


def _parse_range(value: str, default: int = 14) -> int:
    raw = str(value or "").strip().lower()
    if raw.endswith("d"):
        raw = raw[:-1]
    try:
        days = int(raw)
    except ValueError:
        return default
    return max(1, min(90, days))


def _day_labels(days: int) -> list[str]:
    today = dt.date.today()
    return [
        (today - dt.timedelta(days=offset)).isoformat()
        for offset in range(days - 1, -1, -1)
    ]


def create_admin_console_router(
    *,
    config: Config,
    db: Database,
    accounts: AccountStore,
    api_keys: ApiKeyStore,
    admin: AdminStore,
    refresher: Any,
    catalog_cache: Any,
    account_router: AccountRouter,
    model_catalog_for: Any,
    version: str,
    started_at: int,
    checkin: Any,
    maintenance: Any,
    transport: Any = None,
) -> APIRouter:
    router = APIRouter(prefix="/admin/api")

    def current_admin(request: Request) -> str:
        token = request.cookies.get(SESSION_COOKIE, "")
        user_id = admin.get_session(token)
        if not user_id:
            raise HTTPException(status_code=401, detail="login required")
        return user_id

    def settings_payload() -> dict[str, Any]:
        raw = {
            "upstream_mode": "ide",
            "upstream_base_url": config.model_gateway,
            "web_api_base": config.web_api_base,
            "upstream_timeout": 600,
            "upstream_connect_timeout": 30,
            "ide_version": config.ide_version,
            "ide_version_code": config.ide_version_code,
            "catalog_function": "chat_v3",
            "allow_unknown_models": False,
            "native_tools": config.native_tools,
            "send_max_tokens": False,
            "send_reasoning_effort": True,
            "sse_heartbeat": 0,
            "model_cache_ttl": config.model_catalog_ttl,
            "default_concurrency": config.default_concurrency,
            "account_max_concurrency": config.default_concurrency,
            "account_cooldown_seconds": config.account_cooldown,
            "account_failover_limit": config.account_failover_limit,
            "first_token_timeout": config.first_token_timeout,
            "session_sticky_ttl": config.conversation_affinity_ttl,
            "token_refresh_enabled": True,
            "token_refresh_margin": config.refresh_window,
            "telemetry_retention_days": 30,
            "refresh_interval": config.refresh_interval,
            "reasoning_as_content": config.reasoning_as_content,
            "strict_tool_args": config.strict_tool_args,
            "tool_duplicate_warning": config.tool_duplicate_warning,
            "global_ide": config.global_ide,
            "prewarm_catalogs": config.prewarm_catalogs,
            "max_request_bytes": config.max_request_bytes,
            "tool_result_max_chars": config.tool_result_max_chars,
            "history_max_chars": config.history_max_chars,
            "history_max_messages": config.history_max_messages,
            "model_gateway": config.model_gateway,
            "auth_host": config.trae_auth_host,
            "client_id": config.trae_client_id,
            "allow_local_import": config.allow_local_import,
            "checkin_auto": config.checkin_auto,
            "checkin_auto_time": config.checkin_auto_time,
            "account_stagger_seconds": config.account_stagger_seconds,
            "account_jitter_seconds": config.account_jitter_seconds,
            "token_refresh_time": config.token_refresh_time,
            "token_refresh_margin_hours": config.token_refresh_margin_hours,
            "keepalive_time": config.keepalive_time,
            "credit_refresh_times": config.credit_refresh_times,
            "version": version,
        }
        return raw

    def all_credits(refresh: bool = False) -> dict[str, Any]:
        """积分列表只读本地记录，不请求上游；带 refresh 时在后台错峰刷新一次。"""
        started = maintenance.start_manual("credits") if refresh else False
        records = [row for row in accounts.list() if row.status == STATUS_ACTIVE]
        items = []
        for record in records:
            cached = maintenance_service.cached_credit(db, record.id)
            items.append(
                {
                    "account_id": record.id,
                    "account_label": record.label or record.username or record.id,
                    "plan": record.plan or "",
                    "balance": cached["balance"],
                    "used_today": cached["used_today"],
                    "updated_at": cached["updated_at"],
                    "ok": True,
                    "message": "",
                }
            )
        available = sum(
            item["balance"] for item in items if isinstance(item["balance"], (int, float))
        )
        used_today = sum(
            item["used_today"]
            for item in items
            if isinstance(item["used_today"], (int, float))
        )
        return {
            "supported": True,
            "credits": round(available, 2) if available or items else None,
            "available": round(available, 2) if available or items else None,
            "used_today": round(used_today, 2),
            "today_used": round(used_today, 2),
            "updated_at": max((item["updated_at"] for item in items), default=0)
            or now_ts(),
            "refreshing": started or maintenance.is_running("credits"),
            "started": started,
            "accounts": [
                {
                    "account_id": item["account_id"],
                    "account_label": item["account_label"],
                    "plan": item["plan"],
                    "balance": item["balance"],
                    "used_today": item["used_today"],
                    "ok": item["ok"],
                    "message": item["message"],
                }
                for item in items
            ],
        }

    @router.get("/status")
    def status() -> dict[str, Any]:
        return {"initialized": admin.has_admin(), "version": version}

    @router.get("/metrics")
    def metrics(range: str = "14d", _: str = Depends(current_admin)) -> dict[str, Any]:
        days = _parse_range(range, 14)
        since = int(time.time()) - days * 86400
        labels = _day_labels(days)
        series: dict[str, dict[str, Any]] = {
            label: {
                "day": label,
                "requests": 0,
                "errors": 0,
                "total_tokens": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "first_token_total": 0,
                "first_token_count": 0,
            }
            for label in labels
        }
        with db.read() as conn:
            rows = conn.execute(
                "SELECT date(created_at, 'unixepoch', 'localtime') AS day, "
                "COUNT(*) AS requests, "
                "SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS errors, "
                "SUM(total_tokens) AS total_tokens, "
                "SUM(prompt_tokens) AS prompt_tokens, "
                "SUM(completion_tokens) AS completion_tokens, "
                "SUM(CASE WHEN first_token_ms > 0 THEN first_token_ms ELSE 0 END) AS ft_total, "
                "SUM(CASE WHEN first_token_ms > 0 THEN 1 ELSE 0 END) AS ft_count "
                "FROM usage_events WHERE created_at >= ? GROUP BY day",
                (since,),
            ).fetchall()
            today_row = conn.execute(
                "SELECT COUNT(*) AS requests, "
                "SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS errors, "
                "SUM(total_tokens) AS total_tokens "
                "FROM usage_events WHERE date(created_at, 'unixepoch', 'localtime') = ?",
                (dt.date.today().isoformat(),),
            ).fetchone()
        for row in rows:
            item = series.get(str(row["day"]))
            if not item:
                continue
            item["requests"] = int(row["requests"] or 0)
            item["errors"] = int(row["errors"] or 0)
            item["total_tokens"] = int(row["total_tokens"] or 0)
            item["prompt_tokens"] = int(row["prompt_tokens"] or 0)
            item["completion_tokens"] = int(row["completion_tokens"] or 0)
            item["first_token_total"] = int(row["ft_total"] or 0)
            item["first_token_count"] = int(row["ft_count"] or 0)
        trend = []
        total_first = 0
        total_first_count = 0
        for label in labels:
            item = series[label]
            total_first += item["first_token_total"]
            total_first_count += item["first_token_count"]
            trend.append(
                {
                    "day": label,
                    "requests": item["requests"],
                    "errors": item["errors"],
                    "total_tokens": item["total_tokens"],
                }
            )
        total_requests = sum(item["requests"] for item in series.values())
        total_errors = sum(item["errors"] for item in series.values())
        total_tokens = sum(item["total_tokens"] for item in series.values())
        prompt_tokens = sum(item["prompt_tokens"] for item in series.values())
        completion_tokens = sum(item["completion_tokens"] for item in series.values())
        records = accounts.list()
        now = now_ts()
        active = sum(1 for row in records if row.status == STATUS_ACTIVE)
        cooldown = sum(1 for row in records if row.status == "cooldown")
        relogin = sum(
            1
            for row in records
            if row.status in {"relogin_required", "invalid"}
        )
        disabled = sum(1 for row in records if row.status == "disabled")
        expiring = sum(
            1
            for row in records
            if row.token_expires_at and now < row.token_expires_at <= now + 7 * 86400
        )
        sticky_sessions = 0
        try:
            with db.read() as conn:
                sticky_sessions = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM conversation_accounts "
                        "WHERE updated_at >= ?",
                        (now - config.conversation_affinity_ttl,),
                    ).fetchone()[0]
                )
        except Exception:
            sticky_sessions = 0
        return {
            "range": range,
            "range_days": days,
            "generated_at": now,
            "accounts": {
                "total": len(records),
                "active": active,
                "cooldown": cooldown,
                "expiring": expiring,
                "relogin": relogin,
                "disabled": disabled,
                "archived": 0,
            },
            "usage": {
                "requests": total_requests,
                "errors": total_errors,
                "total_tokens": total_tokens,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "avg_first_token_ms": (
                    round(total_first / total_first_count, 1)
                    if total_first_count
                    else 0
                ),
                "success_rate": (
                    round((total_requests - total_errors) / total_requests * 100, 2)
                    if total_requests
                    else 100
                ),
            },
            "today": {
                "requests": int(today_row["requests"] or 0) if today_row else 0,
                "tokens": int(today_row["total_tokens"] or 0) if today_row else 0,
                "errors": int(today_row["errors"] or 0) if today_row else 0,
            },
            "trend": trend,
            "credits": {
                "available": None,
                "used_today": None,
                "changed": None,
                "supported": False,
            },
            "upstream": {
                "status": "正常" if active else "异常",
                "healthy_accounts": active,
                "cooldown_accounts": cooldown,
                "relogin_accounts": relogin,
                "sticky_sessions": sticky_sessions,
                "redis_mode": "noop",
            },
            "version": version,
        }

    @router.get("/models")
    def models(refresh: int = 0, _: str = Depends(current_admin)) -> dict[str, Any]:
        merged: dict[str, dict[str, Any]] = {}
        records = [
            row
            for row in accounts.list()
            if row.status == STATUS_ACTIVE
        ]
        for record in records:
            try:
                catalog = model_catalog_for(record.id, bool(refresh))
            except Exception:
                continue
            groups = catalog.get("groups") or {}
            for channel, group in groups.items():
                if not isinstance(group, dict):
                    continue
                if config.global_ide:
                    if channel != "ide":
                        continue
                elif channel not in {"remote", "work", "ide"}:
                    continue
                for entry in group.get("models") or []:
                    if not isinstance(entry, dict):
                        continue
                    config_name = str(entry.get("config_name") or "")
                    if not config_name:
                        continue
                    model_id = (
                        config_name
                        if channel == "ide"
                        else f"{channel}/{config_name}"
                    )
                    key = model_id.lower()
                    item = merged.setdefault(
                        key,
                        {
                            "id": model_id,
                            "name": entry.get("display_name") or config_name,
                            "series": str(entry.get("source") or ""),
                            "channels": [],
                            "context_window": None,
                            "max_context": None,
                            "max_output": None,
                            "prompt_max_tokens": None,
                            "reasoning_levels": [],
                            "default_reasoning_level": "",
                            "multimodal": False,
                            "tools_supported": False,
                            "max_mode": False,
                            "account_count": 0,
                            "provider_models": [],
                            "_accounts": set(),
                        },
                    )
                    if channel not in item["channels"]:
                        item["channels"].append(channel)
                    for field in ("context_window", "max_context", "max_output"):
                        value = entry.get(field)
                        if isinstance(value, (int, float)):
                            item[field] = max(item[field] or 0, int(value))
                    for variant in entry.get("variants") or []:
                        if not isinstance(variant, dict):
                            continue
                        prompt_max = variant.get("prompt_max_tokens")
                        if isinstance(prompt_max, (int, float)):
                            item["prompt_max_tokens"] = max(
                                item["prompt_max_tokens"] or 0, int(prompt_max)
                            )
                        provider = variant.get("model_name")
                        if provider and provider not in item["provider_models"]:
                            item["provider_models"].append(str(provider))
                    reasoning = entry.get("reasoning") or {}
                    for level in reasoning.get("levels") or []:
                        if level not in item["reasoning_levels"]:
                            item["reasoning_levels"].append(str(level))
                    if not item["default_reasoning_level"]:
                        item["default_reasoning_level"] = str(
                            reasoning.get("default") or ""
                        )
                    item["multimodal"] = bool(
                        item["multimodal"] or entry.get("multimodal")
                    )
                    item["max_mode"] = bool(
                        item["max_mode"] or entry.get("max_mode")
                    )
                    if channel in {"ide", "solo"}:
                        item["tools_supported"] = True
                    item["_accounts"].add(record.id)
        models = []
        for item in merged.values():
            item["account_count"] = len(item.pop("_accounts"))
            models.append(item)
        models.sort(key=lambda item: item["id"].lower())
        return {"models": models, "fetched_at": now_ts()}

    @router.get("/logs")
    def logs(
        range: str = "1d",
        limit: int = 50,
        offset: int = 0,
        key: str = "",
        model: str = "",
        ip: str = "",
        errors: bool = False,
        success: bool = False,
        _: str = Depends(current_admin),
    ) -> dict[str, Any]:
        days = _parse_range(range, 1)
        since = int(time.time()) - days * 86400
        clauses = ["created_at >= ?"]
        values: list[Any] = [since]
        if key:
            clauses.append("api_key_id = ?")
            values.append(key)
        if model:
            clauses.append("model LIKE ?")
            values.append(f"%{model}%")
        if ip:
            clauses.append("source_ip LIKE ?")
            values.append(f"%{ip}%")
        if errors:
            clauses.append("(status >= 400 OR error <> '')")
        if success:
            clauses.append("(status >= 200 AND status < 400 AND error = '')")
        where = " WHERE " + " AND ".join(clauses)
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        with db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM usage_events" + where
                + " ORDER BY id DESC LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM usage_events" + where, tuple(values)
                ).fetchone()[0]
            )
        account_labels = {row.id: (row.label or row.username) for row in accounts.list()}
        items = []
        for row in rows:
            items.append(
                {
                    "id": int(row["id"]),
                    "request_id": row["request_id"],
                    "time": int(row["created_at"]),
                    "key_id": row["api_key_id"],
                    "ip": row["source_ip"] if "source_ip" in row.keys() else "",
                    "account_id": row["account_id"],
                    "account_label": account_labels.get(row["account_id"])
                    or row["account_id"],
                    "endpoint": row["endpoint"],
                    "model": row["model"],
                    "reasoning_effort": (
                        row["reasoning_effort"]
                        if "reasoning_effort" in row.keys()
                        else ""
                    ),
                    "context_window": (
                        int(row["context_window"] or 0)
                        if "context_window" in row.keys()
                        else 0
                    ),
                    "first_token_ms": int(row["first_token_ms"] or 0),
                    "total_ms": int(row["latency_ms"] or 0),
                    "credits": None,
                    "prompt_tokens": int(row["prompt_tokens"] or 0),
                    "completion_tokens": int(row["completion_tokens"] or 0),
                    "total_tokens": int(row["total_tokens"] or 0),
                    "status": int(row["status"] or 0),
                    "error_code": row["error"] or "",
                }
            )
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @router.delete("/logs")
    def clear_logs(_: str = Depends(current_admin)) -> dict[str, Any]:
        removed = api_keys.purge_events(int(time.time()) + 1)
        return {"ok": True, "removed": removed}

    @router.get("/instance")
    def instance(_: str = Depends(current_admin)) -> dict[str, Any]:
        records = accounts.list()
        return {
            "version": version,
            "started_at": started_at,
            "uptime_seconds": max(0, int(time.time()) - started_at),
            "mode": "async",
            "pid": os.getpid(),
            "accounts": len(records),
            "active_accounts": sum(
                1 for row in records if row.status == STATUS_ACTIVE
            ),
            "database": str(config.db_path),
            "locked": True,
        }

    @router.get("/concurrency")
    def concurrency(_: str = Depends(current_admin)) -> dict[str, Any]:
        records = accounts.list()
        return {
            "default_limit": config.default_concurrency,
            "accounts": {
                row.id: {
                    "active": account_router.inflight(row.id),
                    "limit": row.concurrency_limit,
                }
                for row in records
            },
            "updated_at": now_ts(),
            "version": version,
        }

    # 这两个接口要启动后台任务，必须跑在事件循环里，所以是 async
    @router.get("/credits")
    async def credits(
        refresh: int = 0, _: str = Depends(current_admin)
    ) -> dict[str, Any]:
        return all_credits(bool(refresh))

    @router.post("/credits")
    async def refresh_credits(_: str = Depends(current_admin)) -> dict[str, Any]:
        return all_credits(True)

    @router.get("/credits/history")
    def credit_history(
        range: str = "week", _: str = Depends(current_admin)
    ) -> dict[str, Any]:
        today = dt.date.today()
        raw = str(range or "week").strip().lower()
        if raw in {"today", "1d"}:
            start = today
        elif raw in {"month", "30d"}:
            start = today.replace(day=1)
        elif raw in {"week", "7d"}:
            start = today - dt.timedelta(days=today.weekday())
        else:
            days = _parse_range(raw, 14)
            start = today - dt.timedelta(days=days - 1)
        labels = [
            (start + dt.timedelta(days=offset)).isoformat()
            for offset in builtins.range((today - start).days + 1)
        ]
        income = {label: 0.0 for label in labels}
        expense = {label: 0.0 for label in labels}
        with db.read() as conn:
            rows = conn.execute(
                "SELECT day, SUM(MAX(latest - baseline, 0)) AS amount "
                "FROM credit_daily WHERE day >= ? GROUP BY day",
                (start.isoformat(),),
            ).fetchall()
            for row in rows:
                if str(row["day"]) in expense:
                    expense[str(row["day"])] = float(row["amount"] or 0)
            rows = conn.execute(
                "SELECT date(created_at, 'unixepoch', 'localtime') AS day, "
                "SUM(amount) AS amount FROM credit_events "
                "WHERE created_at >= ? GROUP BY day",
                (int(time.time()) - (today - start).days * 86400,),
            ).fetchall()
            for row in rows:
                if str(row["day"]) in income:
                    income[str(row["day"])] = float(row["amount"] or 0)
        series = []
        total_income = 0.0
        total_expense = 0.0
        for label in labels:
            net = income[label] - expense[label]
            total_income += income[label]
            total_expense += expense[label]
            series.append(
                {
                    "date": label,
                    "income": round(income[label], 2),
                    "expense": round(expense[label], 2),
                    "net": round(net, 2),
                }
            )
        return {
            "range": range,
            "range_days": len(labels),
            "series": series,
            "totals": {
                "income": round(total_income, 2),
                "expense": round(total_expense, 2),
                "net": round(total_income - total_expense, 2),
            },
            "supported": True,
        }

    @router.post("/activity/checkin/account")
    async def checkin_account(
        body: CheckinBody, _: str = Depends(current_admin)
    ) -> dict[str, Any]:
        account = accounts.get(body.account_id)
        if not account:
            return {
                "account_id": body.account_id,
                "account_label": "",
                "ok": False,
                "already": False,
                "claimed": False,
                "rate_limited": False,
                "code": 0,
                "message": "account not found",
            }
        return await checkin.run(account)

    @router.get("/checkin/auto")
    def checkin_auto(_: str = Depends(current_admin)) -> dict[str, Any]:
        return checkin_service.AutoCheckin(checkin).status()

    @router.post("/accounts/refresh-all")
    async def refresh_all(_: str = Depends(current_admin)) -> dict[str, Any]:
        # 在后台逐个刷新令牌、资料和积分，账号之间按设置的间隔错峰
        started = maintenance.start_manual("sync")
        return {
            "ok": True,
            "success": True,
            "started": started,
            "running": True,
        }

    @router.get("/maintenance")
    def maintenance_status(_: str = Depends(current_admin)) -> dict[str, Any]:
        return maintenance.status()

    @router.patch("/settings")
    def patch_settings(
        body: SettingsPatchBody, _: str = Depends(current_admin)
    ) -> dict[str, Any]:
        changed: list[str] = []
        values = body.values or {}
        allowed = {
            "default_concurrency",
            "account_max_concurrency",
            "account_cooldown",
            "account_cooldown_seconds",
            "account_failover_limit",
            "first_token_timeout",
            "model_catalog_ttl",
            "refresh_interval",
            "reasoning_as_content",
            "strict_tool_args",
            "tool_duplicate_warning",
            "global_ide",
            "native_tools",
            "prewarm_catalogs",
            "max_request_bytes",
            "tool_result_max_chars",
            "history_max_chars",
            "history_max_messages",
            "model_gateway",
            "web_api_base",
            "checkin_auto",
            "checkin_auto_time",
            "account_stagger_seconds",
            "account_jitter_seconds",
            "token_refresh_time",
            "token_refresh_margin_hours",
            "keepalive_time",
            "credit_refresh_times",
        }
        unknown = [key for key in values if key not in allowed]
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"settings_not_writable: {unknown[0]}"
            )
        for key, value in values.items():
            if key in {"default_concurrency", "account_max_concurrency"}:
                number = max(1, int(value))
                config.default_concurrency = number
                accounts.default_concurrency = number
                for record in accounts.list():
                    accounts.set_concurrency(record.id, number)
            elif key in {"account_cooldown", "account_cooldown_seconds"}:
                number = max(30, int(value))
                config.account_cooldown = number
                account_router.cooldown = number
            elif key == "account_failover_limit":
                max_failover = max(0, len(accounts.list()) - 1)
                config.account_failover_limit = max(
                    0, min(max_failover, int(value))
                )
                values[key] = config.account_failover_limit
            elif key == "first_token_timeout":
                config.first_token_timeout = max(10, min(600, int(value)))
                values[key] = config.first_token_timeout
            elif key == "model_catalog_ttl":
                number = max(10, int(value))
                config.model_catalog_ttl = number
                catalog_cache.ttl = number
                setattr(catalog_cache, "ttl", number)
            elif key == "refresh_interval":
                config.refresh_interval = max(30, int(value))
                refresher.interval = config.refresh_interval
            elif key == "max_request_bytes":
                config.max_request_bytes = max(1024, int(value))
            elif key == "tool_result_max_chars":
                config.tool_result_max_chars = max(
                    1000, min(1_000_000, int(value))
                )
                values[key] = config.tool_result_max_chars
            elif key == "history_max_chars":
                config.history_max_chars = max(
                    10_000, min(10_000_000, int(value))
                )
                values[key] = config.history_max_chars
            elif key == "history_max_messages":
                config.history_max_messages = max(0, min(1000, int(value)))
                values[key] = config.history_max_messages
            elif key == "global_ide":
                config.global_ide = bool(value)
            elif key == "native_tools":
                config.native_tools = bool(value)
            elif key in {
                "reasoning_as_content",
                "strict_tool_args",
                "tool_duplicate_warning",
                "prewarm_catalogs",
            }:
                setattr(config, key, bool(value))
            elif key == "model_gateway":
                config.model_gateway = str(value).rstrip("/")
            elif key == "web_api_base":
                config.web_api_base = str(value).rstrip("/")
            elif key == "checkin_auto":
                config.checkin_auto = bool(value)
                values[key] = config.checkin_auto
            elif key == "checkin_auto_time":
                text = checkin_service.normalize_time(value)
                if not text:
                    raise HTTPException(
                        status_code=400, detail="签到时刻需写成 HH:MM，例如 09:00"
                    )
                config.checkin_auto_time = text
                values[key] = text
            elif key in {"account_stagger_seconds", "account_jitter_seconds"}:
                upper = 600 if key == "account_stagger_seconds" else 3600
                setattr(config, key, max(0, min(upper, int(value))))
                values[key] = getattr(config, key)
            elif key == "token_refresh_margin_hours":
                hours = int(value)
                if not 1 <= hours <= 24 * 14:
                    raise HTTPException(
                        status_code=400, detail="令牌刷新提前量需在 1 到 336 小时之间"
                    )
                config.token_refresh_margin_hours = hours
                config.refresh_window = hours * 3600
                refresher.refresh_window = config.refresh_window
                values[key] = hours
            elif key in {"token_refresh_time", "keepalive_time"}:
                # 留空表示关闭对应的定时任务
                text = "" if value in (None, "") else checkin_service.normalize_time(value)
                if value not in (None, "") and not text:
                    raise HTTPException(
                        status_code=400, detail="时刻需写成 HH:MM，留空表示关闭"
                    )
                setattr(config, key, text)
                values[key] = text
            elif key == "credit_refresh_times":
                slots = maintenance_service.normalize_times(value)
                if value not in (None, "") and not slots:
                    raise HTTPException(
                        status_code=400,
                        detail="积分刷新时刻需写成 HH:MM，多个用逗号隔开",
                    )
                config.credit_refresh_times = ",".join(slots)
                values[key] = config.credit_refresh_times
            changed.append(key)
        persist_map = {
            "account_max_concurrency": "default_concurrency",
            "account_cooldown_seconds": "account_cooldown",
        }
        persist_keys = {
            "global_ide",
            "native_tools",
            "default_concurrency",
            "account_cooldown",
            "account_failover_limit",
            "first_token_timeout",
            "model_catalog_ttl",
            "refresh_interval",
            "reasoning_as_content",
            "strict_tool_args",
            "tool_duplicate_warning",
            "prewarm_catalogs",
            "max_request_bytes",
            "tool_result_max_chars",
            "history_max_chars",
            "history_max_messages",
            "model_gateway",
            "web_api_base",
            "checkin_auto",
            "checkin_auto_time",
            "account_stagger_seconds",
            "account_jitter_seconds",
            "token_refresh_time",
            "token_refresh_margin_hours",
            "keepalive_time",
            "credit_refresh_times",
        }
        for key in changed:
            target = persist_map.get(key, key)
            if target in persist_keys:
                db.set_setting("config:" + target, json.dumps(values[key]))
        return {
            "ok": True,
            "success": True,
            "changed": changed,
            "persisted": True,
            "settings": settings_payload(),
        }

    @router.post("/playground/chat")
    async def playground_chat(
        body: PlaygroundBody, _: str = Depends(current_admin)
    ) -> Any:
        model = body.model.strip()
        message = body.message.strip()
        if not model:
            raise HTTPException(status_code=400, detail="invalid_model")
        if not message:
            raise HTTPException(status_code=400, detail="empty_message")
        if body.one_m and not model.lower().endswith((":1m", "-1m", "__max")):
            model = model + ":1m"
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key.split(',')[0].strip()}"
        if body.account_id:
            headers["X-Trae-Account-Id"] = body.account_id
        host = config.host
        if host in {"0.0.0.0", "::", "[::]"}:
            host = "127.0.0.1"
        url = f"http://{host}:{config.port}/v1/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": message}],
            "stream": True,
            "reasoning_effort": body.reasoning_effort or None,
        }
        timeout = httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0)
        client = httpx.AsyncClient(timeout=timeout)

        async def events() -> AsyncIterator[dict[str, Any]]:
            content = ""
            reasoning = ""
            tool_calls: dict[int, dict[str, Any]] = {}
            usage: Optional[dict[str, Any]] = None
            finish_reason = "stop"
            try:
                request = client.build_request(
                    "POST", url, headers=headers, json=payload
                )
                response = await client.send(request, stream=True)
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", "replace")
                    yield {"type": "error", "message": detail[:500]}
                    return
                try:
                    async for line in response.aiter_lines():
                        text = line.strip()
                        if not text.startswith("data:"):
                            continue
                        raw = text[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            data = json.loads(raw)
                        except ValueError:
                            continue
                        choices = data.get("choices") or []
                        if choices:
                            choice = choices[0]
                            delta = choice.get("delta") or {}
                            chunk = delta.get("content")
                            if isinstance(chunk, str) and chunk:
                                content += chunk
                                yield {"type": "text", "text": chunk}
                            thought = delta.get("reasoning_content")
                            if isinstance(thought, str) and thought:
                                reasoning += thought
                            for call in delta.get("tool_calls") or []:
                                index = int(call.get("index") or 0)
                                item = tool_calls.setdefault(index, {})
                                function = call.get("function") or {}
                                if function.get("name"):
                                    item["name"] = function["name"]
                                if function.get("arguments"):
                                    item["arguments"] = (
                                        item.get("arguments", "")
                                        + function["arguments"]
                                    )
                                yield {
                                    "type": "tool_call",
                                    "index": index,
                                    "name": item.get("name", ""),
                                    "arguments": item.get("arguments", ""),
                                }
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
                        if data.get("usage"):
                            usage = map_usage(data.get("usage"))
                finally:
                    await response.aclose()
            except httpx.HTTPError as exc:
                yield {"type": "error", "message": f"上游连接失败: {exc}"}
            finally:
                await client.aclose()
            result = {
                "content": content,
                "reasoning": reasoning,
                "tool_calls": list(tool_calls.values()),
                "usage": usage,
            }
            if not body.stream:
                yield {"type": "done", "result": result, "finish_reason": finish_reason}
                return
            yield {"type": "result", "result": result}
            if usage:
                yield {"type": "usage", **usage}
            yield {"type": "finish", "reason": finish_reason}

        if not body.stream:
            collected = [event async for event in events()]
            result = next(
                (
                    event["result"]
                    for event in collected
                    if event.get("type") == "done"
                ),
                None,
            )
            if result is None:
                error = next(
                    (event for event in collected if event.get("type") == "error"),
                    None,
                )
                raise HTTPException(
                    status_code=502,
                    detail=(error or {}).get("message", "playground failed"),
                )
            return result

        async def generator() -> AsyncIterator[str]:
            async for event in events():
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
