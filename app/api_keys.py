from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Optional

from .db import Database
from .security import hash_token, new_token, now_ts


@dataclass
class ApiKeyRecord:
    id: str
    name: str
    note: str
    prefix: str
    scopes: str
    allowed_ips: str
    quota_tokens: int
    quota_requests: int
    used_tokens: int
    used_requests: int
    created_at: int
    updated_at: int
    last_used_at: Optional[int]
    expires_at: Optional[int]
    disabled: bool

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "note": self.note,
            "prefix": self.prefix,
            "scopes": [
                item.strip()
                for item in self.scopes.split(",")
                if item.strip()
            ],
            "allowed_ips": self.allowed_ips,
            "quota_tokens": self.quota_tokens,
            "quota_requests": self.quota_requests,
            "used_tokens": self.used_tokens,
            "used_requests": self.used_requests,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
            "expires_at": self.expires_at,
            "disabled": self.disabled,
        }


def _record_from_row(row: Any) -> ApiKeyRecord:
    return ApiKeyRecord(
        id=str(row["id"]),
        name=str(row["name"]),
        note=str(row["note"]) if "note" in row.keys() else "",
        prefix=str(row["prefix"]),
        scopes=str(row["scopes"]),
        allowed_ips=str(row["allowed_ips"]),
        quota_tokens=int(row["quota_tokens"]),
        quota_requests=int(row["quota_requests"]),
        used_tokens=int(row["used_tokens"]),
        used_requests=int(row["used_requests"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        last_used_at=int(row["last_used_at"]) if row["last_used_at"] is not None else None,
        expires_at=int(row["expires_at"]) if row["expires_at"] is not None else None,
        disabled=bool(row["disabled"]),
    )


def _ip_allowed(allowed_ips: str, client_ip: str) -> bool:
    rules = [item.strip() for item in str(allowed_ips or "").split(",") if item.strip()]
    if not rules:
        return True
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for rule in rules:
        try:
            if "/" in rule:
                if address in ipaddress.ip_network(rule, strict=False):
                    return True
            elif address == ipaddress.ip_address(rule):
                return True
        except ValueError:
            continue
    return False


def _normalize_scopes(value: str) -> str:
    seen: list[str] = []
    for item in str(value or "").split(","):
        scope = item.strip()
        if scope and scope not in seen:
            seen.append(scope)
    return ",".join(seen) or "chat,models"


class ApiKeyStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        *,
        name: str,
        note: str = "",
        scopes: str = "chat,models",
        allowed_ips: str = "",
        quota_tokens: int = 0,
        quota_requests: int = 0,
        expires_at: Optional[int] = None,
    ) -> tuple[ApiKeyRecord, str]:
        key_id = "key_" + new_token(12)
        token = "tm_" + new_token(32)
        now = now_ts()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO api_keys(id, name, note, key_hash, prefix, scopes, allowed_ips, "
                "quota_tokens, quota_requests, used_tokens, used_requests, created_at, "
                "updated_at, expires_at, disabled) VALUES(?,?,?,?,?,?,?,?,?,0,0,?,?,?,0)",
                (
                    key_id,
                    name.strip(),
                    note.strip(),
                    hash_token(token),
                    token[:12],
                    _normalize_scopes(scopes),
                    allowed_ips,
                    max(0, int(quota_tokens)),
                    max(0, int(quota_requests)),
                    now,
                    now,
                    expires_at,
                ),
            )
        record = self.get(key_id)
        if record is None:
            raise RuntimeError("api key insert failed")
        return record, token

    def list(self) -> list[ApiKeyRecord]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM api_keys ORDER BY created_at DESC"
            ).fetchall()
        return [_record_from_row(row) for row in rows]

    def get(self, key_id: str) -> Optional[ApiKeyRecord]:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE id = ?", (key_id,)
            ).fetchone()
        return _record_from_row(row) if row else None

    def find_by_token(self, token: str) -> Optional[ApiKeyRecord]:
        if not token:
            return None
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE key_hash = ?", (hash_token(token),)
            ).fetchone()
        return _record_from_row(row) if row else None

    def authorize(
        self, token: str, client_ip: str
    ) -> tuple[Optional[ApiKeyRecord], str]:
        record = self.find_by_token(token)
        if record is None:
            return None, "invalid api key"
        if record.disabled:
            return None, "api key disabled"
        if record.expires_at and record.expires_at <= now_ts():
            return None, "api key expired"
        if not _ip_allowed(record.allowed_ips, client_ip):
            return None, "api key ip not allowed"
        if record.quota_requests and record.used_requests >= record.quota_requests:
            return None, "api key request quota exceeded"
        if record.quota_tokens and record.used_tokens >= record.quota_tokens:
            return None, "api key token quota exceeded"
        return record, ""

    def update(self, key_id: str, fields: dict[str, Any]) -> Optional[ApiKeyRecord]:
        allowed = {
            "name",
            "note",
            "scopes",
            "allowed_ips",
            "quota_tokens",
            "quota_requests",
            "expires_at",
            "disabled",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if updates:
            if "scopes" in updates:
                updates["scopes"] = _normalize_scopes(str(updates["scopes"]))
            if "name" in updates:
                updates["name"] = str(updates["name"]).strip()
            if "note" in updates:
                updates["note"] = str(updates["note"]).strip()[:200]
            if "disabled" in updates:
                updates["disabled"] = 1 if updates["disabled"] else 0
            updates["updated_at"] = now_ts()
            assignments = ", ".join(f"{key} = ?" for key in updates)
            with self.db.tx() as conn:
                conn.execute(
                    f"UPDATE api_keys SET {assignments} WHERE id = ?",
                    (*updates.values(), key_id),
                )
        return self.get(key_id)

    def delete(self, key_id: str) -> None:
        with self.db.tx() as conn:
            conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))

    def reset_usage(self, key_id: str) -> Optional[ApiKeyRecord]:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE api_keys SET used_tokens = 0, used_requests = 0, "
                "updated_at = ? WHERE id = ?",
                (now_ts(), key_id),
            )
        return self.get(key_id)

    def record_usage(
        self,
        key_id: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        if not key_id:
            return
        total = int(total_tokens or (prompt_tokens + completion_tokens) or 0)
        now = now_ts()
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE api_keys SET used_tokens = used_tokens + ?, "
                "used_requests = used_requests + 1, last_used_at = ?, "
                "updated_at = ? WHERE id = ?",
                (total, now, now, key_id),
            )

    def add_event(
        self,
        *,
        request_id: str,
        api_key_id: str = "",
        account_id: str = "",
        model: str = "",
        endpoint: str = "",
        status: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        first_token_ms: int = 0,
        latency_ms: int = 0,
        error: str = "",
        source_ip: str = "",
        client: str = "",
        reasoning_effort: str = "",
        context_window: int = 0,
        cached_tokens: int = 0,
        credits: float = 0.0,
        conversation_key: str = "",
    ) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO usage_events(request_id, api_key_id, account_id, model, "
                "endpoint, status, prompt_tokens, completion_tokens, total_tokens, "
                "first_token_ms, latency_ms, error, source_ip, client, "
                "reasoning_effort, context_window, cached_tokens, credits, "
                "conversation_key, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    api_key_id,
                    account_id,
                    model,
                    endpoint,
                    int(status),
                    int(prompt_tokens),
                    int(completion_tokens),
                    int(total_tokens),
                    int(first_token_ms),
                    int(latency_ms),
                    str(error or "")[:1000],
                    str(source_ip or "")[:64],
                    str(client or "")[:64],
                    str(reasoning_effort or "")[:32],
                    int(context_window or 0),
                    int(cached_tokens or 0),
                    float(credits or 0.0),
                    str(conversation_key or "")[:200],
                    now_ts(),
                ),
            )

    def attach_billing(
        self, request_id: str, *, cached_tokens: int = 0, credits: float = 0.0
    ) -> None:
        """把账单回查的结果补到已记录的那条用量上。

        同时标记 billing_ok：命中率统计只算查到的行，否则"查不到"会被
        当成"没命中"，把整体命中率拉低。
        """

        if not request_id:
            return
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE usage_events SET cached_tokens = ?, credits = ?, "
                "billing_ok = 1 WHERE request_id = ?",
                (int(cached_tokens or 0), float(credits or 0.0), request_id),
            )

    def settle_billing(
        self,
        session_id: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        credits: float = 0.0,
    ) -> dict[str, float]:
        """把账单的会话累计值换算成本次请求的增量。

        账单接口按会话返回累计用量，直接记录会让同一会话的每个请求都记一遍
        总额。这里记住上一次的累计值，只返回这次新增的部分。
        """

        if not session_id:
            return {"cached_tokens": float(cached_tokens), "credits": float(credits)}
        now = now_ts()
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT input_tokens, output_tokens, cached_tokens, credits "
                "FROM billing_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            previous = (
                dict(row)
                if row is not None
                else {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_tokens": 0,
                    "credits": 0.0,
                }
            )
            conn.execute(
                "INSERT INTO billing_state(session_id, input_tokens, output_tokens, "
                "cached_tokens, credits, updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "input_tokens=excluded.input_tokens, "
                "output_tokens=excluded.output_tokens, "
                "cached_tokens=excluded.cached_tokens, "
                "credits=excluded.credits, updated_at=excluded.updated_at",
                (
                    session_id,
                    int(input_tokens or 0),
                    int(output_tokens or 0),
                    int(cached_tokens or 0),
                    float(credits or 0.0),
                    now,
                ),
            )
        return {
            "input_tokens": max(
                0, int(input_tokens or 0) - int(previous["input_tokens"] or 0)
            ),
            "cached_tokens": max(
                0, int(cached_tokens or 0) - int(previous["cached_tokens"] or 0)
            ),
            "credits": max(
                0.0,
                round(float(credits or 0.0) - float(previous["credits"] or 0.0), 4),
            ),
        }

    def events(
        self,
        *,
        limit: int = 100,
        api_key_id: str = "",
        account_id: str = "",
        model: str = "",
    ) -> list[dict[str, Any]]:
        clauses = []
        values: list[Any] = []
        if api_key_id:
            clauses.append("api_key_id = ?")
            values.append(api_key_id)
        if account_id:
            clauses.append("account_id = ?")
            values.append(account_id)
        if model:
            clauses.append("model = ?")
            values.append(model)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        values.append(max(1, min(1000, int(limit))))
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM usage_events" + where + " ORDER BY id DESC LIMIT ?",
                tuple(values),
            ).fetchall()
        return [dict(row) for row in rows]

    def usage_summary(
        self,
        *,
        days: int = 7,
        api_key_id: str = "",
        account_id: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        since = now_ts() - max(1, min(3650, int(days))) * 86400
        clauses = ["created_at >= ?"]
        values: list[Any] = [since]
        if api_key_id:
            clauses.append("api_key_id = ?")
            values.append(api_key_id)
        if account_id:
            clauses.append("account_id = ?")
            values.append(account_id)
        if model:
            clauses.append("model = ?")
            values.append(model)
        where = " WHERE " + " AND ".join(clauses)
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT date(created_at, 'unixepoch', 'localtime') AS day, "
                "api_key_id, account_id, model, "
                "COUNT(*) AS requests, "
                "SUM(prompt_tokens) AS prompt_tokens, "
                "SUM(completion_tokens) AS completion_tokens, "
                "SUM(total_tokens) AS total_tokens, "
                "SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS errors "
                "FROM usage_events" + where + " "
                "GROUP BY day, api_key_id, account_id, model "
                "ORDER BY day DESC, requests DESC",
                tuple(values),
            ).fetchall()
        return {
            "since": since,
            "days": days,
            "rows": [dict(row) for row in rows],
        }

    def purge_events(self, before: int) -> int:
        with self.db.tx() as conn:
            cursor = conn.execute(
                "DELETE FROM usage_events WHERE created_at < ?", (int(before),)
            )
        return int(cursor.rowcount or 0)
